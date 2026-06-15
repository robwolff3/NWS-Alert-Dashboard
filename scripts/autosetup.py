#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Location-based auto-setup.

If LOCATION=lat,lon is set, derives any of these that are NOT already set:
  FILTER_SAME_CODES   — county SAME code (PSSCCC)
  API_ZONES           — county UGC + forecast zone UGC
  FILTER_ZONES        — forecast zone UGC
  RADIO_FREQUENCY(+_FALLBACK) — 162.550 primary + the other six NWR channels;
      the silence-rotation scanner in run.sh then finds the active transmitter
      automatically (NOAA no longer publishes a machine-readable transmitter
      list, so scanning beats shipping a stale one).

Results go to /alerts/derived_config.json (shown in the UI) and to
/tmp/derived_env.sh, which run.sh sources before starting the sources.
Explicit env vars always win — this never overrides anything.
"""
import json
import os
import shlex
import sys
import time

import requests

sys.path.insert(0, '/app/scripts')
import config

DERIVED_JSON = '/alerts/derived_config.json'
DERIVED_ENV  = '/tmp/derived_env.sh'

NWR_CHANNELS = ['162.550', '162.400', '162.425', '162.450',
                '162.475', '162.500', '162.525']


def main():
    # Always write the env file so run.sh can source it unconditionally
    derived = {}
    loc = config.env('LOCATION').strip()

    if loc:
        try:
            lat, lon = [float(x) for x in loc.replace(' ', '').split(',')]
        except ValueError:
            print(f'autosetup: LOCATION={loc!r} is not lat,lon — ignoring', flush=True)
            lat = lon = None

        if lat is not None:
            try:
                r = requests.get(
                    f"{config.env('API_BASE_URL', 'https://api.weather.gov').rstrip('/')}"
                    f'/points/{lat:.4f},{lon:.4f}',
                    headers={'User-Agent': config.env('API_USER_AGENT',
                                                      'nws-alert-dashboard'),
                             'Accept': 'application/geo+json'},
                    timeout=20)
                r.raise_for_status()
                props = r.json().get('properties', {})

                county_ugc = (props.get('county') or '').rsplit('/', 1)[-1]
                zone_ugc   = (props.get('forecastZone') or '').rsplit('/', 1)[-1]
                cwa        = props.get('cwa')
                city = (props.get('relativeLocation') or {}) \
                    .get('properties', {}).get('city')

                # County name (e.g. "Wayne") for the header subtitle — /points
                # only gives the county zone URL, so fetch its name.
                county_name = None
                if props.get('county'):
                    try:
                        cr = requests.get(
                            props['county'],
                            headers={'User-Agent': config.env('API_USER_AGENT',
                                                              'nws-alert-dashboard'),
                                     'Accept': 'application/geo+json'},
                            timeout=15)
                        cr.raise_for_status()
                        county_name = cr.json().get('properties', {}).get('name')
                    except Exception:
                        pass

                same = None
                if county_ugc:
                    fips = config.ugc_county_to_fips(county_ugc)
                    if fips:
                        same = '0' + fips

                if not config.env('FILTER_SAME_CODES') and same:
                    derived['FILTER_SAME_CODES'] = same
                if not config.env('API_ZONES') and county_ugc:
                    derived['API_ZONES'] = ' '.join(
                        z for z in (county_ugc, zone_ugc) if z)
                if not config.env('FILTER_ZONES') and zone_ugc:
                    derived['FILTER_ZONES'] = zone_ugc
                if not config.env('RADIO_FREQUENCY'):
                    derived['RADIO_FREQUENCY'] = NWR_CHANNELS[0]
                    derived['RADIO_FREQUENCY_FALLBACK'] = ' '.join(NWR_CHANNELS[1:])

                info = {
                    'location': [lat, lon], 'near': city, 'cwa': cwa,
                    'county_ugc': county_ugc, 'county_name': county_name,
                    'forecast_zone': zone_ugc,
                    'same_code': same, 'derived': derived,
                    'updated': time.time(),
                }
                with open(DERIVED_JSON, 'w') as f:
                    json.dump(info, f, indent=2)
                print(f'autosetup: {city or loc} → county {county_ugc} '
                      f'(SAME {same}), zone {zone_ugc}, CWA {cwa}', flush=True)
                for k, v in derived.items():
                    print(f'autosetup: derived {k}={v}', flush=True)
                if 'RADIO_FREQUENCY' in derived:
                    print('autosetup: no transmitter list is published by NOAA — '
                          'frequency scan will find the active NWR channel '
                          '(silence rotation)', flush=True)
            except Exception as e:
                print(f'autosetup: lookup failed ({e}) — using existing config '
                      f'or cached derivation', flush=True)
                # Offline start: fall back to the last derivation if present
                try:
                    with open(DERIVED_JSON) as f:
                        derived = json.load(f).get('derived', {})
                    derived = {k: v for k, v in derived.items()
                               if not config.env(k)}
                except (OSError, ValueError):
                    derived = {}

    # run.sh sources this file, so shell-quote each value (defense-in-depth: a
    # derived value should never contain metacharacters, but never `eval` an
    # unquoted API-derived string). Lock perms — /tmp is world-writable.
    with open(DERIVED_ENV, 'w') as f:
        for k, v in derived.items():
            f.write(f'export {k}={shlex.quote(str(v))}\n')
    try:
        os.chmod(DERIVED_ENV, 0o600)
    except OSError:
        pass


if __name__ == '__main__':
    main()
