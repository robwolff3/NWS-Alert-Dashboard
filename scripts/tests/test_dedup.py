#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Cross-source dedup matrix. Runs against a throwaway SQLite DB with
notification delivery stubbed out.

  docker exec nwsalertdashboard python3 /app/scripts/tests/test_dedup.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, '/app/scripts')

import alerts as alertdb
import ingest as ing
from ingest import IncomingAlert

NOTIFICATIONS = []
PUSHES = []


def _fake_notify(title, body, priority=3, topic='nws', attach_path=None):
    NOTIFICATIONS.append({'title': title, 'body': body,
                          'priority': priority, 'topic': topic})
    return True


def _fake_push(title, body, priority, eee=''):
    PUSHES.append(title)


ing.notifier.send = _fake_notify
ing.pushdb.send_push = _fake_push

# This suite asserts on notification *counts*, so the map paths must not add
# any. Whether they do otherwise depends on cached map data being present
# (/alerts/mapdata): without it a radio alert's first notification carries no
# map, and the later-geometry follow-up fires as a second notification. Pin
# both off so the results do not depend on the host the tests run on.
os.environ['NOTIFY_MAP_ATTACH'] = 'false'
os.environ['NOTIFY_MAP_FOLLOWUP'] = 'false'

_PASS = 0
_FAIL = 0


def fresh_db():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    os.unlink(path)
    alertdb.DB_PATH = path
    alertdb._initialized = False
    NOTIFICATIONS.clear()
    PUSHES.clear()


def check(name, cond, detail=''):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f'  ok    {name}')
    else:
        _FAIL += 1
        print(f'  FAIL  {name}  {detail}')


def radio_tor(ts=None):
    return IncomingAlert(
        source='radio', event_name='Tornado Warning', eee='TOR',
        issue_ts=ts or time.time(), fips={'026163'},
        expires_ts=(ts or time.time()) + 2700,
        raw_text='SAME header text')


def api_tor(ts=None, action='NEW', etn='0042', geom=True):
    now = ts or time.time()
    return IncomingAlert(
        source='api', event_name='Tornado Warning', eee='TOR',
        issue_ts=now,
        vtec={'action': action, 'office': 'KDTX', 'phen': 'TO', 'sig': 'W',
              'etn': etn, 'key': f'KDTX.TO.W.{etn}.2026'},
        native_id=f'urn:oid:2.49.0.1.840.0.{etn}.{action}',
        fips={'026163'}, ugc={'MIC163'},
        expires_ts=now + 2700,
        headline='Tornado Warning issued for Wayne County MI',
        description='At 530 PM a confirmed tornado...',
        severity='Extreme',
        onset_ts=now,
        geometry={'type': 'Polygon', 'coordinates': [[[-83.5, 42.1], [-83.3, 42.1],
                  [-83.3, 42.3], [-83.5, 42.3], [-83.5, 42.1]]]} if geom else None)


def nwws_tor(ts=None, etn='0042', action='NEW'):
    now = ts or time.time()
    return IncomingAlert(
        source='nwws', event_name='Tornado Warning', eee='TOR',
        issue_ts=now,
        vtec={'action': action, 'office': 'KDTX', 'phen': 'TO', 'sig': 'W',
              'etn': etn, 'key': f'KDTX.TO.W.{etn}.2026'},
        native_id=f'KDTX-TORDTX-{int(now)}',
        fips={'026163'}, ugc={'MIC163'},
        expires_ts=now + 2700,
        raw_text='raw TOR product text')


def test_radio_then_api():
    print('radio → api (radio-first enrichment)')
    fresh_db()
    id1 = ing.ingest(radio_tor())
    id2 = ing.ingest(api_tor())
    check('same canonical row', id1 == id2, f'{id1} vs {id2}')
    check('exactly one notification', len(NOTIFICATIONS) == 1, NOTIFICATIONS)
    row = alertdb.get_alert(id1)
    check('geometry filled by api', bool(row['geometry']))
    check('description filled', bool(row['description']))
    check('vtec_key filled', row['vtec_key'] == 'KDTX.TO.W.0042.2026')
    check('sources has both', set(json.loads(row['sources'])) == {'radio', 'api'})
    check('first_source radio', row['first_source'] == 'radio')


def test_api_then_radio():
    print('api → radio (push beat the broadcast)')
    fresh_db()
    id1 = ing.ingest(api_tor())
    id2 = ing.ingest(radio_tor())
    check('same canonical row', id1 == id2)
    check('exactly one notification', len(NOTIFICATIONS) == 1, NOTIFICATIONS)
    row = alertdb.get_alert(id1)
    check('first_source api', row['first_source'] == 'api')
    check('rich text not overwritten', row['description'].startswith('At 530 PM'))


def test_nwws_then_api_then_radio():
    print('nwws → api → radio (all three sources)')
    fresh_db()
    id1 = ing.ingest(nwws_tor())
    id2 = ing.ingest(api_tor())
    id3 = ing.ingest(radio_tor())
    check('one canonical row', id1 == id2 == id3, f'{id1},{id2},{id3}')
    check('exactly one notification', len(NOTIFICATIONS) == 1)
    row = alertdb.get_alert(id1)
    check('all three sources', set(json.loads(row['sources'])) == {'nwws', 'api', 'radio'})
    check('raw product kept', row['raw_product'] == 'raw TOR product text')
    check('api description merged', bool(row['description']))


def test_con_extends_no_notify():
    print('CON extends expiry without re-notifying')
    fresh_db()
    now = time.time()
    id1 = ing.ingest(api_tor(ts=now))
    upd = api_tor(ts=now + 300, action='CON')
    upd.expires_ts = now + 4500
    id2 = ing.ingest(upd)
    check('merged', id1 == id2)
    check('one notification', len(NOTIFICATIONS) == 1)
    row = alertdb.get_alert(id1)
    check('expiry extended', abs(row['expires_at'] - (now + 4500)) < 1,
          row['expires_at'])
    check('action recorded', row['vtec_action'] == 'CON')


def test_can_expires():
    print('CAN expires the row, never notifies')
    fresh_db()
    now = time.time()
    id1 = ing.ingest(api_tor(ts=now))
    ing.ingest(api_tor(ts=now + 600, action='CAN'))
    row = alertdb.get_alert(id1)
    check('expired now', row['expires_at'] <= time.time() + 1, row['expires_at'])
    check('still one notification', len(NOTIFICATIONS) == 1)
    fresh_db()
    ing.ingest(api_tor(action='CAN'))
    check('CAN for unknown alert ignored', len(alertdb.get_alerts(10)) == 0)
    check('CAN never notifies', len(NOTIFICATIONS) == 0)


def test_new_etn_separate_row():
    print('same county, new ETN → separate alert')
    fresh_db()
    now = time.time()
    id1 = ing.ingest(api_tor(ts=now, etn='0042'))
    id2 = ing.ingest(api_tor(ts=now + 60, etn='0043'))
    check('two rows', id1 != id2)
    check('two notifications', len(NOTIFICATIONS) == 2)


def test_different_county_no_match():
    print('same event, different county → separate alert')
    fresh_db()
    id1 = ing.ingest(radio_tor())
    other = api_tor()
    other.fips = {'026099'}
    other.ugc = {'MIC099'}
    id2 = ing.ingest(other)
    check('two rows', id1 != id2)


def test_radio_only_offline():
    print('radio only (offline) — single row, notified, no enrichment')
    fresh_db()
    id1 = ing.ingest(radio_tor())
    row = alertdb.get_alert(id1)
    check('notified', bool(row['notified_at']))
    check('no geometry', not row['geometry'])
    check('header kept', row['header_message'] == 'SAME header text')


def test_event_filter_silences():
    print('NOTIFY_EVENT_CODES suppresses notification but stores row')
    fresh_db()
    os.environ['NOTIFY_EVENT_CODES'] = 'SVR FFW'
    try:
        id1 = ing.ingest(radio_tor())
        row = alertdb.get_alert(id1)
        check('row stored', row is not None)
        check('not notified', not row['notified_at'])
        check('no notification sent', len(NOTIFICATIONS) == 0)
    finally:
        del os.environ['NOTIFY_EVENT_CODES']


def test_repeat_poll_idempotent():
    print('repeated API poll of same alert is a no-op')
    fresh_db()
    a = api_tor()
    id1 = ing.ingest(a)
    id2 = ing.ingest(api_tor())
    id3 = ing.ingest(api_tor())
    check('one row', id1 == id2 == id3)
    check('one notification', len(NOTIFICATIONS) == 1)


def test_api_update_overwrites_and_records():
    print('API revision overwrites content, records a revision (no escalation)')
    fresh_db()
    now = time.time()
    id1 = ing.ingest(api_tor(ts=now))            # 'At 530 PM...', Extreme, geom A
    upd = api_tor(ts=now + 300, action='CON')    # same severity (Extreme)
    upd.description = 'At 600 PM the tornado was near...'
    upd.headline = 'Tornado Warning — updated'
    upd.geometry = {'type': 'Polygon', 'coordinates': [[[-83.45, 42.12],
                    [-83.35, 42.12], [-83.35, 42.28], [-83.45, 42.28], [-83.45, 42.12]]]}
    id2 = ing.ingest(upd)
    check('merged same row', id1 == id2)
    row = alertdb.get_alert(id1)
    check('description overwritten', row['description'].startswith('At 600 PM'))
    check('headline overwritten', row['headline'].endswith('updated'))
    check('geometry overwritten', '42.28' in row['geometry'])
    check('update_count is 1', row['update_count'] == 1, row['update_count'])
    revs = json.loads(row['revisions'] or '[]')
    check('one revision snapshot', len(revs) == 1, revs)
    check('prior description kept', bool(revs) and
          revs[0].get('description', '').startswith('At 530 PM'))
    check('not re-notified (no escalation)', len(NOTIFICATIONS) == 1, NOTIFICATIONS)
    # a radio decode is never a content revision
    ing.ingest(radio_tor(ts=now + 360))
    check('radio did not bump update_count',
          alertdb.get_alert(id1)['update_count'] == 1)


def test_escalation_renotifies():
    print('severity rise / PDS wording re-notifies (escalation mode)')
    fresh_db()
    os.environ['RENOTIFY_ON_UPDATE'] = 'escalation'
    try:
        now = time.time()
        init = api_tor(ts=now); init.severity = 'Severe'
        id1 = ing.ingest(init)
        upd = api_tor(ts=now + 300, action='CON')   # severity Extreme = escalation
        upd.description = 'This is now a PARTICULARLY DANGEROUS SITUATION...'
        id2 = ing.ingest(upd)
        check('merged', id1 == id2)
        check('re-notified once', len(NOTIFICATIONS) == 2, NOTIFICATIONS)
        check('escalated title', any('Escalated' in n['title'] for n in NOTIFICATIONS))
        check('renotified_at set', bool(alertdb.get_alert(id1)['renotified_at']))
    finally:
        del os.environ['RENOTIFY_ON_UPDATE']


def test_renotify_off_suppresses():
    print('RENOTIFY_ON_UPDATE=off suppresses but still overwrites + records')
    fresh_db()
    os.environ['RENOTIFY_ON_UPDATE'] = 'off'
    try:
        now = time.time()
        init = api_tor(ts=now); init.severity = 'Severe'
        ing.ingest(init)
        upd = api_tor(ts=now + 300, action='CON')   # would escalate if enabled
        id2 = ing.ingest(upd)
        check('no re-notification', len(NOTIFICATIONS) == 1, NOTIFICATIONS)
        row = alertdb.get_alert(id2)
        check('severity still overwritten', row['severity'] == 'Extreme')
        check('revision still recorded', row['update_count'] == 1)
    finally:
        del os.environ['RENOTIFY_ON_UPDATE']


def test_renotify_throttled():
    print('RENOTIFY_ON_UPDATE=all throttles a second update within the interval')
    fresh_db()
    os.environ['RENOTIFY_ON_UPDATE'] = 'all'
    try:
        now = time.time()
        id1 = ing.ingest(api_tor(ts=now))
        u1 = api_tor(ts=now + 10, action='CON'); u1.description = 'update one text'
        ing.ingest(u1)
        check('first revision re-notifies', len(NOTIFICATIONS) == 2, NOTIFICATIONS)
        u2 = api_tor(ts=now + 20, action='CON'); u2.description = 'update two text'
        ing.ingest(u2)
        check('second revision throttled', len(NOTIFICATIONS) == 2, NOTIFICATIONS)
        check('both revisions recorded',
              alertdb.get_alert(id1)['update_count'] == 2)
    finally:
        del os.environ['RENOTIFY_ON_UPDATE']


def test_cancel_with_revision_no_renotify():
    print('CAN carrying revised text never re-notifies (terminating action)')
    fresh_db()
    os.environ['RENOTIFY_ON_UPDATE'] = 'all'   # most permissive; still must suppress
    try:
        now = time.time()
        id1 = ing.ingest(api_tor(ts=now))
        can = api_tor(ts=now + 300, action='CAN')
        can.description = 'The Tornado Warning is cancelled.'
        ing.ingest(can)
        check('no re-notification on cancel', len(NOTIFICATIONS) == 1, NOTIFICATIONS)
        check('row expired', alertdb.get_alert(id1)['expires_at'] <= time.time() + 1)
    finally:
        del os.environ['RENOTIFY_ON_UPDATE']


def test_notify_body_never_empty():
    print('every notification carries a body (radio has no headline)')
    fresh_db()
    ing.ingest(radio_tor())
    check('one notification', len(NOTIFICATIONS) == 1)
    body = NOTIFICATIONS[0]['body']
    check('body is non-empty', bool(body and body.strip()), NOTIFICATIONS)
    check('body keeps the SAME header text', 'SAME header text' in body, body)
    check('body leads with the county line', body.startswith('MI - Wayne'), body)


def test_dismiss_survives_reingest():
    print('a dismissed alert stays hidden when the poller re-sees it')
    fresh_db()
    now = time.time()
    id1 = ing.ingest(api_tor(ts=now))
    check('visible before dismiss', len(alertdb.get_alerts(10)) == 1)

    check('dismiss reports a change', alertdb.dismiss_alert(id1) is True)
    check('second dismiss is a no-op', alertdb.dismiss_alert(id1) is False)
    check('hidden from the dashboard', len(alertdb.get_alerts(10)) == 0)
    check('row still in the DB', alertdb.get_alert(id1) is not None)
    check('still readable with include_dismissed',
          len(alertdb.get_alerts(10, include_dismissed=True)) == 1)

    # The next poll re-ingests the same active alert: it must merge into the
    # dismissed row rather than create a fresh, visible, re-notifying one.
    ing.ingest(api_tor(ts=now))
    check('no new row', len(alertdb.get_alerts(10, include_dismissed=True)) == 1)
    check('still hidden', len(alertdb.get_alerts(10)) == 0)
    check('no second notification', len(NOTIFICATIONS) == 1, NOTIFICATIONS)


def test_escalation_undismisses():
    print('an escalation un-hides a dismissed alert')
    fresh_db()
    now = time.time()
    id1 = ing.ingest(api_tor(ts=now))
    alertdb.dismiss_alert(id1)
    check('hidden', len(alertdb.get_alerts(10)) == 0)

    esc = api_tor(ts=now + 700)
    esc.description = 'This is a PARTICULARLY DANGEROUS SITUATION. Take cover now.'
    ing.ingest(esc)
    check('re-notified', len(NOTIFICATIONS) == 2, NOTIFICATIONS)
    check('visible again', len(alertdb.get_alerts(10)) == 1)
    check('dismissed_at cleared', alertdb.get_alert(id1)['dismissed_at'] is None)

def test_areas_line_is_bounded():
    print('the county line stays short enough for a push payload')
    fresh_db()
    import fips_lookup
    every = list(fips_lookup._load())
    check('nationwide list is capped',
          len(fips_lookup.format_grouped(every)) <= fips_lookup.MAX_TOTAL_CHARS + 40,
          len(fips_lookup.format_grouped(every)))
    check('per-state overflow is summarised',
          '+' in fips_lookup.format_grouped(every[:120]))
    check('short list is untouched',
          fips_lookup.format_grouped(['026163', '026125']) == 'MI - Oakland, Wayne')


def test_malformed_fips_is_dropped():
    print('a malformed FIPS code yields no county rather than a wrong one')
    import fips_lookup
    check('4-digit code rejected', fips_lookup.format_grouped('1049') == '',
          fips_lookup.format_grouped('1049'))
    check('non-numeric rejected', fips_lookup.format_grouped('abcde') == '')
    check('valid code still resolves',
          fips_lookup.format_grouped('021049') == 'KY - Clark')


def test_territories_resolve():
    print('territories resolve (NWS issues alerts for PR/VI/GU/AS/MP)')
    import fips_lookup
    check('Puerto Rico', fips_lookup.format_grouped('072127') == 'PR - San Juan',
          fips_lookup.format_grouped('072127'))
    check('Virgin Islands', fips_lookup.format_grouped('078030').startswith('VI - '))
    check('Guam', fips_lookup.format_grouped('066010') == 'GU - Guam')

if __name__ == '__main__':
    for fn in [test_radio_then_api, test_api_then_radio,
               test_nwws_then_api_then_radio, test_con_extends_no_notify,
               test_can_expires, test_new_etn_separate_row,
               test_different_county_no_match, test_radio_only_offline,
               test_event_filter_silences, test_repeat_poll_idempotent,
               test_api_update_overwrites_and_records, test_escalation_renotifies,
               test_renotify_off_suppresses, test_renotify_throttled,
               test_cancel_with_revision_no_renotify,
               test_notify_body_never_empty, test_dismiss_survives_reingest,
               test_escalation_undismisses, test_areas_line_is_bounded,
               test_malformed_fips_is_dropped, test_territories_resolve]:
        fn()
    print(f'\n{_PASS} passed, {_FAIL} failed')
    sys.exit(1 if _FAIL else 0)
