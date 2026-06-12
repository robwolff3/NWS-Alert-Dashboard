#!/usr/bin/env python3
"""Cross-source dedup matrix. Runs against a throwaway SQLite DB with
notification delivery stubbed out.

  docker exec weatherradio python3 /app/scripts/tests/test_dedup.py
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
    NOTIFICATIONS.append({'title': title, 'priority': priority, 'topic': topic})
    return True


def _fake_push(title, body, priority, eee=''):
    PUSHES.append(title)


ing.notifier.send = _fake_notify
ing.pushdb.send_push = _fake_push

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
    print('FILTER_EVENT_CODES suppresses notification but stores row')
    fresh_db()
    os.environ['FILTER_EVENT_CODES'] = 'SVR FFW'
    try:
        id1 = ing.ingest(radio_tor())
        row = alertdb.get_alert(id1)
        check('row stored', row is not None)
        check('not notified', not row['notified_at'])
        check('no notification sent', len(NOTIFICATIONS) == 0)
    finally:
        del os.environ['FILTER_EVENT_CODES']


def test_repeat_poll_idempotent():
    print('repeated API poll of same alert is a no-op')
    fresh_db()
    a = api_tor()
    id1 = ing.ingest(a)
    id2 = ing.ingest(api_tor())
    id3 = ing.ingest(api_tor())
    check('one row', id1 == id2 == id3)
    check('one notification', len(NOTIFICATIONS) == 1)


if __name__ == '__main__':
    for fn in [test_radio_then_api, test_api_then_radio,
               test_nwws_then_api_then_radio, test_con_extends_no_notify,
               test_can_expires, test_new_etn_separate_row,
               test_different_county_no_match, test_radio_only_offline,
               test_event_filter_silences, test_repeat_poll_idempotent]:
        fn()
    print(f'\n{_PASS} passed, {_FAIL} failed')
    sys.exit(1 if _FAIL else 0)
