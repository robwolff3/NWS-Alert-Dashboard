#!/usr/bin/env python3
"""NWWS-OI XMPP client daemon (push source).

Connects to the NWS Weather Wire Service Open Interface, joins the product
MUC, filters incoming raw text products by AWIPS PIL + UGC/FIPS, parses VTEC
segments, and feeds matches through the shared ingest core.

Connection notes:
  - Two servers (College Park / Boulder); alternate on every reconnect.
  - XEP-0199 keepalive ping — NWWS connections are known to die silently.
  - Unique MUC nick per session so a stale ghost session can't block rejoin.

Replay mode (no XMPP, same code path — for testing):
  nwws_client.py --replay fixture1.txt [fixture2.txt ...]
"""
import argparse
import asyncio
import datetime
import random
import sys
import time

sys.path.insert(0, '/app/scripts')
import config
import nwws_parse
from ingest import IncomingAlert, ingest

NWWS_DOMAIN = 'nwws-oi.weather.gov'


def _accepted_pils():
    return set(config.env_list(
        'NWWS_PILS',
        'TOR SVR SVS FFW FFS FLW FLS FFA EWW SMW MWS SQW WSW NPW WCN WOU '
        'SEL SPS CEM CDW LAE EVI SPW HLS CFW'))


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def handle_product(attrs: dict, raw_text: str, log_only: bool = False) -> int:
    """Filter + parse one NWWS product; ingest matching segments.

    Returns the number of segments ingested.
    """
    awipsid = (attrs.get('awipsid') or '').strip()
    cccc    = (attrs.get('cccc') or '').strip()
    if not awipsid:
        return 0

    pil = awipsid[:3].upper()
    if pil not in _accepted_pils():
        return 0

    offices = set(config.env_list('NWWS_OFFICES'))
    if offices and cccc not in offices:
        return 0

    text = nwws_parse.normalize(raw_text)
    issue_ts = _iso_ts(attrs.get('issue')) or time.time()
    native_id = f"{cccc}-{awipsid}-{attrs.get('issue', int(issue_ts))}"

    my_fips  = config.filter_same_codes()
    my_zones = config.filter_zones()

    event_name_mnd = nwws_parse.extract_event_name(text)
    geometry = nwws_parse.parse_latlon(text)

    ingested = 0
    for seg in nwws_parse.split_segments(text):
        seg_fips = {config.normalize_same(f)
                    for u in seg['ugc']
                    for f in [config.ugc_county_to_fips(u)] if f}
        # Geographic gate: county FIPS overlap, or forecast-zone overlap
        if my_fips and not (seg_fips & my_fips):
            if not (my_zones and (seg['ugc'] & my_zones)):
                continue

        purge_ts = nwws_parse.purge_to_ts(seg['purge'], issue_ts)
        vtecs = seg['vtec'] or [None]
        for vtec in vtecs:
            if vtec:
                event_name = None
                eee = config.VTEC_TO_EEE.get((vtec['phen'], vtec['sig']))
                if eee:
                    # canonical display name via reverse NWS map
                    for name, code in config.NWS_EVENT_TO_EEE.items():
                        if code == eee:
                            event_name = name
                            break
                event_name = event_name or event_name_mnd or awipsid
                expires_ts = vtec['end_ts'] or purge_ts
            else:
                event_name = event_name_mnd or awipsid
                eee = config.NWS_EVENT_TO_EEE.get(event_name)
                expires_ts = purge_ts

            inc = IncomingAlert(
                source='nwws',
                event_name=event_name,
                issue_ts=issue_ts,
                eee=eee,
                vtec=vtec,
                native_id=native_id,
                fips=seg_fips,
                ugc=seg['ugc'],
                expires_ts=expires_ts,
                raw_text=text,
                geometry=geometry,
                onset_ts=(vtec or {}).get('begin_ts'),
            )
            if log_only:
                print(f'nwws[log-only]: would ingest {event_name} '
                      f"vtec={(vtec or {}).get('key')} ugc={sorted(seg['ugc'])}",
                      flush=True)
            else:
                ingest(inc)
            ingested += 1
    return ingested


# ── XMPP client ───────────────────────────────────────────────────────────────

def run_xmpp():
    import slixmpp

    user = config.env('NWWS_USER')
    pw   = config.env('NWWS_PASS')
    if not user or not pw:
        print('nwws: NWWS_USER/NWWS_PASS not set — exiting', flush=True)
        config.set_source_status('nwws', connected=False,
                                 last_error='credentials not set')
        sys.exit(0)

    servers  = config.env_list('NWWS_SERVERS',
                               'nwws-oi-cprk.weather.gov nwws-oi-bldr.weather.gov')
    room     = config.env('NWWS_ROOM', f'nwws@conference.{NWWS_DOMAIN}')
    resource = config.env('NWWS_RESOURCE', 'nwws')
    log_only = config.env_bool('NWWS_LOG_ONLY', False)

    class NWWSBot(slixmpp.ClientXMPP):
        def __init__(self):
            jid = f'{user}@{NWWS_DOMAIN}/{resource}'
            super().__init__(jid, pw)
            self.register_plugin('xep_0045')   # MUC
            self.register_plugin('xep_0199',   # keepalive ping
                                 pconfig={'keepalive': True, 'interval': 60,
                                          'timeout': 30})
            self.add_event_handler('session_start', self.on_start)
            self.add_event_handler('groupchat_message', self.on_message)
            self.add_event_handler('disconnected', self.on_disconnected)
            self.add_event_handler('failed_auth', self.on_failed_auth)
            self._stopping = False

        async def on_start(self, _):
            self.send_presence()
            await self.get_roster()
            nick = f'{user}-{random.randbytes(2).hex()}'
            try:
                await self.plugin['xep_0045'].join_muc_wait(
                    room, nick, maxstanzas=0, timeout=30)
                print(f'nwws: joined {room} as {nick}', flush=True)
                config.set_source_status('nwws', connected=True,
                                         last_error=None)
            except Exception as e:
                print(f'nwws: MUC join failed: {e}', flush=True)
                self.disconnect()

        def on_message(self, msg):
            x = msg.xml.find('{nwws-oi}x')
            if x is None or not x.text:
                return
            attrs = dict(x.attrib)
            try:
                n = handle_product(attrs, x.text, log_only=log_only)
                if n:
                    print(f"nwws: {attrs.get('cccc')}/{attrs.get('awipsid')} "
                          f'→ {n} segment(s) ingested', flush=True)
                config.set_source_status('nwws', connected=True,
                                         last_product_ts=time.time())
            except Exception as e:
                print(f"nwws: error handling {attrs.get('awipsid')}: {e}",
                      flush=True)

        def on_disconnected(self, _):
            config.set_source_status('nwws', connected=False)

        def on_failed_auth(self, _):
            print('nwws: AUTHENTICATION FAILED — check NWWS_USER/NWWS_PASS',
                  flush=True)
            config.set_source_status('nwws', connected=False,
                                     last_error='auth failed')
            self._stopping = True
            self.disconnect()

    backoff = 5
    server_idx = 0
    while True:
        host = servers[server_idx % len(servers)]
        server_idx += 1
        print(f'nwws: connecting to {host}:5222 ...', flush=True)
        bot = NWWSBot()
        try:
            bot.connect((host, 5222))
            bot.process(forever=True)   # returns on disconnect
        except Exception as e:
            print(f'nwws: connection error: {e}', flush=True)
        config.set_source_status('nwws', connected=False)
        if getattr(bot, '_stopping', False):
            print('nwws: fatal auth error — sleeping 1h before retry', flush=True)
            time.sleep(3600)
            backoff = 5
            continue
        print(f'nwws: disconnected — retrying other host in {backoff}s', flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


# ── Replay mode ───────────────────────────────────────────────────────────────

def run_replay(files: list, log_only: bool):
    total = 0
    for path in files:
        with open(path) as f:
            text = f.read()
        ttaaii, cccc, ddhhmm, awipsid = nwws_parse.parse_wmo_header(text)
        attrs = {
            'ttaaii': ttaaii or '',
            'cccc': cccc or 'KXXX',
            'awipsid': awipsid or '',
            'issue': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        n = handle_product(attrs, text, log_only=log_only)
        print(f'replay {path}: awipsid={awipsid} → {n} segment(s)', flush=True)
        total += n
    print(f'replay done: {total} segment(s) total', flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--replay', nargs='+', metavar='FILE',
                    help='parse product files through the ingest path and exit')
    ap.add_argument('--log-only', action='store_true',
                    help='parse and log, but do not ingest/notify')
    args = ap.parse_args()

    if args.replay:
        run_replay(args.replay, args.log_only)
    else:
        run_xmpp()
