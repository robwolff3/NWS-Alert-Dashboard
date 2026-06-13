#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""NOAA REST API polling daemon (always-on safety net + radio enrichment).

Polls api.weather.gov/alerts/active for the configured zones every
API_POLL_SECS (default 120), dropping to API_POLL_SECS_DEGRADED (default 30)
while NWWS-OI is disconnected or disabled. A radio decode touches
/tmp/poll_now, which triggers an immediate fetch so the radio-first alert is
enriched with headline/description/polygon within seconds.

Every fetched feature goes through ingest() — dedup makes overlap with the
push sources harmless.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, '/app/scripts')
import config
import nws_api
from ingest import ingest

POLL_NOW = Path('/tmp/poll_now')


def nwws_healthy() -> bool:
    if not config.env_bool('NWWS_ENABLED', False):
        return False
    status = config.get_source_status().get('nwws', {})
    return bool(status.get('connected'))


def poll_once(zones: list):
    features = nws_api.get_active_alerts(zones)
    for feature in features:
        try:
            ingest(nws_api.feature_to_incoming(feature))
        except Exception as e:
            fid = feature.get('properties', {}).get('id', '?')
            print(f'api_poller: ingest failed for {fid}: {e}', flush=True)
    return len(features)


def main():
    zones = config.env_list('API_ZONES')
    if not zones:
        print('api_poller: API_ZONES not set — poller idle', flush=True)
        config.set_source_status('api', enabled=False, last_error='API_ZONES not set')
        while True:
            time.sleep(3600)

    print(f'api_poller: watching zones {zones}', flush=True)
    config.set_source_status('api', enabled=True)

    while True:
        try:
            n = poll_once(zones)
            config.set_source_status('api', enabled=True,
                                     last_success_ts=time.time(),
                                     active_count=n, last_error=None)
        except Exception as e:
            print(f'api_poller: poll failed: {e}', flush=True)
            config.set_source_status('api', enabled=True, last_error=str(e))

        interval = (config.env_int('API_POLL_SECS', 120) if nwws_healthy()
                    else config.env_int('API_POLL_SECS_DEGRADED', 30))

        # Sleep in 2s slices so a radio decode can trigger an immediate poll
        deadline = time.time() + interval
        while time.time() < deadline:
            if POLL_NOW.exists():
                try:
                    POLL_NOW.unlink()
                except OSError:
                    pass
                print('api_poller: poll_now trigger — fetching immediately', flush=True)
                break
            time.sleep(2)


if __name__ == '__main__':
    main()
