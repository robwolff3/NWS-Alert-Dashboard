#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Canonical alert ingest: dedup across sources, merge, notify exactly once.

Every alert source (radio / nwws / api / test) builds an IncomingAlert and
calls ingest(). Matching strategy, in order:

  1. vtec_key   — office.phen.sig.etn.year (NWWS and API both carry VTEC)
  2. native id  — api_id / nwws_id (catches updates and retransmits)
  3. heuristic  — radio decodes have no VTEC: match on event equivalence
                  (SAME EEE ↔ VTEC/NWS-event mapping) + county FIPS overlap +
                  time proximity (DEDUP_WINDOW_SECS, default 600) or falling
                  inside the candidate's active window.

The first source to land an alert claims notification (atomic UPDATE on
notified_at); later sources merge in richer fields (headline, description,
polygon) and never re-notify — except a single optional map follow-up when
geometry arrives after the notification went out.
"""
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, '/app/scripts')
import alerts as alertdb
import config
import notifier
import push as pushdb

DECODE_SENTINEL = Path('/tmp/last_decode')
POLL_NOW        = Path('/tmp/poll_now')

# Fields a richer source may fill in when the row's value is NULL/empty.
_FILL_IF_NULL = ('headline', 'description', 'instruction', 'geometry',
                 'raw_product', 'severity', 'onset', 'vtec_key', 'api_id',
                 'nwws_id', 'eee')

# Content a rich source (NWWS/API) may *revise* in place when NWS reissues an
# alert. Radio never overwrites — it only fills nulls via _FILL_IF_NULL.
_CONTENT = ('headline', 'description', 'instruction', 'geometry', 'severity')
_SEVERITY_RANK = {'Minor': 1, 'Moderate': 2, 'Severe': 3, 'Extreme': 4}
_ESCALATION_RE = re.compile(
    r'tornado emergency|particularly dangerous situation', re.I)


@dataclass
class IncomingAlert:
    source: str                      # 'radio' | 'nwws' | 'api' | 'test'
    event_name: str
    issue_ts: float
    eee: Optional[str] = None        # SAME event code
    vtec: Optional[dict] = None      # {action, office, phen, sig, etn, key, end_ts}
    native_id: Optional[str] = None  # api properties.id or nwws product id
    fips: set = field(default_factory=set)   # 6-digit PSSCCC codes
    ugc: set = field(default_factory=set)
    expires_ts: Optional[float] = None
    headline: Optional[str] = None
    description: Optional[str] = None
    instruction: Optional[str] = None
    raw_text: Optional[str] = None
    geometry: Optional[dict] = None  # GeoJSON geometry
    severity: Optional[str] = None
    onset_ts: Optional[float] = None
    is_test: bool = False


def _derive_eee(a: IncomingAlert) -> Optional[str]:
    if a.eee:
        return a.eee
    if a.vtec:
        eee = config.VTEC_TO_EEE.get((a.vtec.get('phen'), a.vtec.get('sig')))
        if eee:
            return eee
    return config.NWS_EVENT_TO_EEE.get(a.event_name)


def _event_equivalent(a: IncomingAlert, eee_in: Optional[str], cand: dict) -> bool:
    cand_eee = cand.get('eee')
    if eee_in and cand_eee:
        return eee_in == cand_eee
    # Last resort: exact event name (covers unmapped events from rich sources)
    return bool(a.event_name) and a.event_name == cand.get('event_name')


def _fips_overlap(a: IncomingAlert, cand: dict) -> bool:
    if not a.fips:
        return True  # source had no geo info (shouldn't happen in practice)
    cand_fips = set(json.loads(cand['fips'])) if cand.get('fips') else set()
    if not cand_fips:
        return True
    return bool(a.fips & cand_fips)


def _time_match(a: IncomingAlert, cand: dict, window: int) -> bool:
    if abs(a.issue_ts - cand['alert_time']) <= window:
        return True
    onset   = cand.get('onset') or cand['alert_time']
    expires = cand.get('expires_at')
    return bool(expires and onset <= a.issue_ts <= expires)


def _find_match(conn, a: IncomingAlert, eee_in: Optional[str]) -> Optional[dict]:
    if a.vtec and a.vtec.get('key'):
        row = alertdb.find_by_vtec_key(conn, a.vtec['key'])
        if row:
            return row
    if a.native_id:
        col = 'api_id' if a.source == 'api' else 'nwws_id'
        row = alertdb.find_by_native_id(conn, col, a.native_id)
        if row:
            return row
    window = config.env_int('DEDUP_WINDOW_SECS', 600)
    in_key = a.vtec.get('key') if a.vtec else None
    # Candidate lookback must cover the dedup window so a configured window
    # larger than the default 1800s isn't silently capped.
    for cand in alertdb.find_candidates(conn, time.time(), max(1800, window)):
        if bool(cand.get('is_test')) != a.is_test:
            continue
        # Distinct VTEC events are distinct alerts, period (new ETN = new warning)
        if in_key and cand.get('vtec_key') and cand['vtec_key'] != in_key:
            continue
        if (_event_equivalent(a, eee_in, cand)
                and _fips_overlap(a, cand)
                and _time_match(a, cand, window)):
            return cand
    return None


def _merge_fields(a: IncomingAlert, cand: dict, eee_in: Optional[str]) -> dict:
    """Compute the UPDATE field dict for merging an arrival into a row."""
    fields = {}
    incoming = {
        'headline':    a.headline,
        'description': a.description,
        'instruction': a.instruction,
        'geometry':    json.dumps(a.geometry) if a.geometry else None,
        'raw_product': a.raw_text if a.source == 'nwws' else None,
        'severity':    a.severity,
        'onset':       a.onset_ts,
        'vtec_key':    a.vtec.get('key') if a.vtec else None,
        'api_id':      a.native_id if a.source == 'api' else None,
        'nwws_id':     a.native_id if a.source == 'nwws' else None,
        'eee':         eee_in,
    }
    for col in _FILL_IF_NULL:
        if incoming.get(col) and not cand.get(col):
            fields[col] = incoming[col]

    # Rich sources (NWWS/API) revise content in place when NWS reissues the
    # alert. Only overwrite a value that was already present and actually
    # changed — that is a true revision (a null→value fill above is mere
    # enrichment). Radio is excluded so a sparse decode never clobbers text.
    revised = []
    if a.source in ('nwws', 'api'):
        for col in _CONTENT:
            val = incoming.get(col)
            if val is None or not cand.get(col) or val == cand[col]:
                continue
            # Geometry differing only by re-serialization (key order, float
            # formatting) is not a real change — compare the parsed shapes.
            if col == 'geometry':
                try:
                    if a.geometry == json.loads(cand['geometry']):
                        continue
                except (ValueError, TypeError):
                    pass
            fields[col] = val
            revised.append(col)

    # Rich sources upgrade a radio-generic event name ("Tornado Warning" from
    # dsame3 is fine, but NWS names are authoritative for unmapped events).
    if a.source in ('nwws', 'api') and cand.get('first_source') == 'radio' \
            and a.event_name and a.event_name != cand.get('event_name'):
        fields['event_name'] = a.event_name

    # Union geo codes
    for col, new in (('fips', a.fips), ('ugc', a.ugc)):
        if new:
            merged = set(json.loads(cand[col])) if cand.get(col) else set()
            if not new <= merged:
                fields[col] = json.dumps(sorted(merged | new))

    # Source bookkeeping
    sources = json.loads(cand['sources']) if cand.get('sources') else {}
    if a.source not in sources:
        sources[a.source] = time.time()
        fields['sources'] = json.dumps(sources)

    # Time semantics
    action = (a.vtec or {}).get('action')
    if action:
        fields['vtec_action'] = action
    if action in ('CAN', 'EXP', 'UPG'):
        # v1 simplification: expire the whole row
        now = time.time()
        if not cand.get('expires_at') or cand['expires_at'] > now:
            fields['expires_at'] = now
    else:
        if a.issue_ts < cand['alert_time']:
            fields['alert_time'] = a.issue_ts
        if a.expires_ts and (not cand.get('expires_at') or a.expires_ts > cand['expires_at']):
            fields['expires_at'] = a.expires_ts

    return fields, revised


def _render_map(alert_row: dict) -> Optional[str]:
    """Render the alert map PNG if the map subsystem is available (phase 4)."""
    if not config.env_bool('MAP_ENABLED', True):
        return None
    try:
        import maps
    except ImportError:
        return None
    try:
        return maps.render_alert_map(alert_row)
    except Exception as e:
        print(f'ingest: map render failed: {e}', flush=True)
        return None


def _publish_mqtt(alert_row: dict, kind: str):
    """Publish to MQTT if enabled (phase 5)."""
    if not config.env_bool('MQTT_ENABLED', False):
        return
    try:
        import mqtt_pub
        mqtt_pub.publish(alert_row, kind)
    except Exception as e:
        print(f'ingest: mqtt publish failed: {e}', flush=True)


def _notify(alert_row: dict):
    title = alert_row['event_name']
    body  = (alert_row.get('headline') or alert_row.get('header_message')
             or alert_row['event_name'])
    if alert_row.get('is_test'):
        title = f'[TEST] {title}'

    attach = None
    map_file = _render_map(alert_row)
    if map_file and config.env_bool('NOTIFY_MAP_ATTACH', True):
        attach = str(Path(alertdb.MAPS_DIR) / map_file)
        alertdb.set_map_file(alert_row['id'], map_file,
                             map_sent=True)
    elif map_file:
        alertdb.set_map_file(alert_row['id'], map_file)

    notifier.send(title, body, alert_row['priority'], alert_row.get('topic') or 'nws',
                  attach_path=attach)
    try:
        pushdb.send_push(title, body, alert_row['priority'], alert_row.get('eee') or '')
    except Exception as e:
        print(f'ingest: web push failed: {e}', flush=True)
    _publish_mqtt(alert_row, 'new')


def _map_followup(alert_row: dict):
    """One-time map follow-up when geometry arrives after notification."""
    if not config.env_bool('NOTIFY_MAP_FOLLOWUP', True):
        return
    map_file = _render_map(alert_row)
    if not map_file:
        return
    alertdb.set_map_file(alert_row['id'], map_file, map_sent=True)
    notifier.send(f"{alert_row['event_name']} — Affected Area",
                  alert_row.get('headline') or '',
                  priority=2, topic=alert_row.get('topic') or 'nws',
                  attach_path=str(Path(alertdb.MAPS_DIR) / map_file))


def _is_escalation(cand: dict, fields: dict) -> bool:
    """A revision counts as an escalation when severity rank rises or the alert
    newly gains 'Tornado Emergency' / 'Particularly Dangerous Situation' wording."""
    new_sev = fields.get('severity')
    if new_sev and _SEVERITY_RANK.get(new_sev, 0) > _SEVERITY_RANK.get(cand.get('severity'), 0):
        return True
    new_text = ' '.join(filter(None, (fields.get('description'), fields.get('instruction'))))
    if new_text and _ESCALATION_RE.search(new_text):
        old_text = ' '.join(filter(None, (cand.get('description'), cand.get('instruction'))))
        if not _ESCALATION_RE.search(old_text or ''):
            return True
    return False


def _renotify_decision(cand: dict, fields: dict, row: dict, now: float, action):
    """Whether a revision should re-notify, and whether it's an escalation.
    Gated by RENOTIFY_ON_UPDATE (off|escalation|all), the row already having
    notified, not being a terminating action, not expired, the event filter,
    and a throttle interval."""
    mode = config.env('RENOTIFY_ON_UPDATE', 'escalation').strip().lower()
    if mode not in ('escalation', 'all') or not cand.get('notified_at'):
        return False, False
    # CAN/EXP/UPG expire the row in this same merge (expires_at = now, which the
    # strict < check below would miss) — never re-notify a just-killed alert.
    if action in ('CAN', 'EXP', 'UPG'):
        return False, False
    exp = row.get('expires_at')
    if exp and exp < now:
        return False, False
    events = config.notify_event_codes()
    if events and row.get('eee') not in events and not row.get('is_test'):
        return False, False
    escalation = _is_escalation(cand, fields)
    if mode == 'escalation' and not escalation:
        return False, False
    interval = config.env_int('RENOTIFY_MIN_INTERVAL_SECS', 600)
    if now - (cand.get('renotified_at') or 0) < interval:
        return False, False
    return True, escalation


def _renotify(row: dict, escalation: bool) -> bool:
    """Send an update notification for an in-place revision (Apprise + push),
    attaching a freshly rendered map for the current geometry when
    NOTIFY_MAP_ATTACH is on. Returns whether a map was sent, so the caller can
    skip the separate late-geometry follow-up. MQTT for the merge is published
    once by the shared merge path, not here."""
    label = '⚠ Escalated' if escalation else 'Updated'
    title = f"{label}: {row['event_name']}"
    if row.get('is_test'):
        title = f'[TEST] {title}'
    body = row.get('headline') or row.get('header_message') or row['event_name']
    attach = None
    if config.env_bool('NOTIFY_MAP_ATTACH', True):
        map_file = _render_map(row)
        if map_file:
            attach = str(Path(alertdb.MAPS_DIR) / map_file)
            alertdb.set_map_file(row['id'], map_file, map_sent=True)
    notifier.send(title, body, row['priority'], row.get('topic') or 'nws',
                  attach_path=attach)
    try:
        pushdb.send_push(title, body, row['priority'], row.get('eee') or '')
    except Exception as e:
        print(f'ingest: re-notify web push failed: {e}', flush=True)
    print(f"ingest: re-notified {row['id']} ({title})", flush=True)
    return attach is not None


def ingest(a: IncomingAlert) -> str:
    """Dedup/merge/insert an incoming alert. Returns the canonical row id."""
    config.set_source_status(a.source, last_alert_ts=time.time())
    eee_in = _derive_eee(a)
    action = (a.vtec or {}).get('action')
    now = time.time()

    conn = alertdb.transaction()
    try:
        cand = _find_match(conn, a, eee_in)
        if cand:
            fields, revised = _merge_fields(a, cand, eee_in)

            # Record a revision: snapshot the prior values, bump counters. Kept
            # (newest-capped) for the dashboard's collapsed revision history.
            if revised:
                snap = {'ts': now, 'action': action, 'source': a.source}
                for col in revised:
                    snap[col] = cand.get(col)
                history = []
                if cand.get('revisions'):
                    try:
                        history = json.loads(cand['revisions'])
                    except ValueError:
                        history = []
                history.append(snap)
                cap = config.env_int('REVISION_HISTORY_MAX', 12)
                fields['revisions'] = json.dumps(history[-cap:])
                fields['updated_at'] = now
                fields['update_count'] = (cand.get('update_count') or 0) + 1

            geometry_new = 'geometry' in fields
            row = {**cand, **fields}
            do_renotify, escalation = (False, False)
            if revised:
                do_renotify, escalation = _renotify_decision(cand, fields, row, now, action)
                if do_renotify:
                    fields['renotified_at'] = now
                    row['renotified_at'] = now

            alertdb.merge_alert(conn, cand['id'], fields)
            conn.commit()
            alert_id = cand['id']
            print(f"ingest: {a.source} merged into {alert_id} "
                  f"({row.get('event_name')}, +{sorted(fields.keys())})", flush=True)

            if a.source == 'radio':
                _touch_radio_sentinels()
            alertdb._signal()
            _publish_mqtt(row, 'update')

            renotify_mapped = False
            if do_renotify:
                renotify_mapped = _renotify(row, escalation)
            # Late geometry → one-time map follow-up, unless a map already went
            # out (at first notify, or just now via the re-notification).
            if (geometry_new and cand.get('notified_at')
                    and not cand.get('map_sent') and not renotify_mapped):
                _map_followup(row)
            return alert_id

        # No match → new row (unless it's a cancellation for something
        # we never saw — nothing to do then)
        if action in ('CAN', 'EXP', 'UPG'):
            conn.commit()
            print(f'ingest: {a.source} {action} for unknown alert — ignored', flush=True)
            return ''

        priority, topic = config.priority_for_eee(eee_in)
        alert_id = alertdb.new_alert_id()
        row = {
            'id':             alert_id,
            'eee':            eee_in,
            'event_name':     a.event_name,
            'alert_time':     a.issue_ts,
            'expires_at':     a.expires_ts,
            'priority':       priority,
            'topic':          topic,
            'header_message': a.raw_text if a.source == 'radio' else (a.headline or a.event_name),
            'transcript_status': 'skipped',
            'created_at':     now,
            'vtec_key':       a.vtec.get('key') if a.vtec else None,
            'vtec_action':    action,
            'api_id':         a.native_id if a.source == 'api' else None,
            'nwws_id':        a.native_id if a.source == 'nwws' else None,
            'sources':        json.dumps({a.source: now}),
            'first_source':   a.source,
            'fips':           json.dumps(sorted(a.fips)) if a.fips else None,
            'ugc':            json.dumps(sorted(a.ugc)) if a.ugc else None,
            'headline':       a.headline,
            'description':    a.description,
            'instruction':    a.instruction,
            'raw_product':    a.raw_text if a.source == 'nwws' else None,
            'geometry':       json.dumps(a.geometry) if a.geometry else None,
            'severity':       a.severity,
            'onset':          a.onset_ts,
            'is_test':        int(a.is_test),
        }
        alertdb.insert_full(conn, row)

        # Decide whether to notify (filters), then claim atomically.
        # Unmapped events (eee None) count as filtered when a filter is set —
        # otherwise minor non-EAS products (e.g. Beach Hazards) would notify.
        events = config.notify_event_codes()
        filtered = bool(events) and eee_in not in events and not a.is_test
        expired  = a.expires_ts is not None and a.expires_ts < now
        should_notify = not filtered and not expired
        claimed = should_notify and alertdb.claim_notification(conn, alert_id, now)
        conn.commit()
    finally:
        conn.close()

    print(f"ingest: {a.source} new alert {alert_id} ({a.event_name})"
          + (' [notifying]' if claimed else ' [silent]'), flush=True)

    if a.source == 'radio':
        _touch_radio_sentinels()
    alertdb._signal()

    if claimed:
        row['notified_at'] = now
        _notify(row)

    return alert_id


def _touch_radio_sentinels():
    """Reset the silence clock and request an immediate API enrichment poll."""
    DECODE_SENTINEL.touch()
    try:
        POLL_NOW.touch()
    except OSError:
        pass
