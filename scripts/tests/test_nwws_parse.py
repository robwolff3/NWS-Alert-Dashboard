#!/usr/bin/env python3
"""Parser unit tests against real archived products (fixtures/).

  docker exec nwsalertdashboard python3 /app/scripts/tests/test_nwws_parse.py
"""
import os
import sys

sys.path.insert(0, '/app/scripts')
import nwws_parse as np

FIX = os.path.join(os.path.dirname(__file__), 'fixtures')

_PASS = _FAIL = 0


def check(name, cond, detail=''):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f'  ok    {name}')
    else:
        _FAIL += 1
        print(f'  FAIL  {name}  {detail}')


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def test_tor():
    print('TORDTX (storm-based tornado warning)')
    text = load('TORDTX.txt')
    segs = np.split_segments(text)
    check('one segment', len(segs) == 1, len(segs))
    seg = segs[0]
    check('ugc counties', seg['ugc'] == {'MIC017', 'MIC157'}, seg['ugc'])
    check('purge time', seg['purge'] == '110115', seg['purge'])
    check('one vtec', len(seg['vtec']) == 1)
    v = seg['vtec'][0]
    check('vtec action NEW', v['action'] == 'NEW')
    check('vtec key', v['key'] == 'KDTX.TO.W.0017.2026', v['key'])
    check('vtec end ts set', v['end_ts'] is not None)
    geom = np.parse_latlon(text)
    check('polygon parsed', geom is not None)
    check('polygon closed', geom['coordinates'][0][0] == geom['coordinates'][0][-1])
    check('polygon ~9+1 pts', len(geom['coordinates'][0]) == 10,
          len(geom['coordinates'][0]))
    lon, lat = geom['coordinates'][0][0]
    check('first vertex sane', abs(lat - 43.67) < .01 and abs(lon + 83.35) < .01,
          (lon, lat))
    check('event name', np.extract_event_name(text) == 'Tornado Warning')
    ttaaii, cccc, ddhhmm, awipsid = np.parse_wmo_header(text)
    check('wmo header', (ttaaii, cccc, awipsid) == ('WFUS53', 'KDTX', 'TORDTX'),
          (ttaaii, cccc, awipsid))


def test_svs_can():
    print('SVSDTX (cancellation statement)')
    text = load('SVSDTX.txt')
    segs = np.split_segments(text)
    check('one segment', len(segs) == 1, len(segs))
    seg = segs[0]
    check('ugc', seg['ugc'] == {'MIC161'}, seg['ugc'])
    check('vtec CAN', seg['vtec'][0]['action'] == 'CAN')
    check('vtec key', seg['vtec'][0]['key'] == 'KDTX.SV.W.0070.2026',
          seg['vtec'][0]['key'])
    check('begin ts None (000000T0000Z)', seg['vtec'][0]['begin_ts'] is None)
    check('event name', np.extract_event_name(text) == 'Severe Weather Statement')


def test_svr():
    print('SVRDTX (severe thunderstorm warning)')
    text = load('SVRDTX.txt')
    segs = np.split_segments(text)
    check('one segment', len(segs) == 1)
    check('vtec SV.W', segs[0]['vtec'][0]['phen'] == 'SV'
          and segs[0]['vtec'][0]['sig'] == 'W')
    check('ugc MIC161', segs[0]['ugc'] == {'MIC161'}, segs[0]['ugc'])


def test_sps():
    print('SPSDTX (non-VTEC special weather statement)')
    text = load('SPSDTX.txt')
    segs = np.split_segments(text)
    check('segment found', len(segs) >= 1, len(segs))
    check('no vtec', all(not s['vtec'] for s in segs))
    check('event name', np.extract_event_name(text) == 'Special Weather Statement')


def test_wsw():
    print('WSWDTX (zone-based winter product)')
    text = load('WSWDTX.txt')
    segs = np.split_segments(text)
    check('segment found', len(segs) >= 1, len(segs))
    zone_ugcs = set().union(*[s['ugc'] for s in segs])
    check('zone UGCs (Z)', all(u[2] == 'Z' for u in zone_ugcs), zone_ugcs)


def test_ugc_ranges():
    print('UGC expansion edge cases')
    ugc, purge = np.expand_ugc_tokens(['MIC001>005', '163', '120515'])
    check('range expansion', ugc == {'MIC001', 'MIC002', 'MIC003', 'MIC004',
                                     'MIC005', 'MIC163'}, ugc)
    check('purge', purge == '120515')
    ugc, _ = np.expand_ugc_tokens(['MIZ068>070', 'OHZ001', '002'])
    check('multi-state + format switch',
          ugc == {'MIZ068', 'MIZ069', 'MIZ070', 'OHZ001', 'OHZ002'}, ugc)


def test_doubled_newlines():
    print('NWWS doubled-newline normalization')
    original = load('TORDTX.txt')
    doubled = original.replace('\n', '\n\n')
    segs = np.split_segments(np.normalize(doubled))
    check('still parses', len(segs) == 1 and segs[0]['ugc'] == {'MIC017', 'MIC157'})


if __name__ == '__main__':
    for fn in [test_tor, test_svs_can, test_svr, test_sps, test_wsw,
               test_ugc_ranges, test_doubled_newlines]:
        fn()
    print(f'\n{_PASS} passed, {_FAIL} failed')
    sys.exit(1 if _FAIL else 0)
