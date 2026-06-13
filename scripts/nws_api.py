#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""api.weather.gov client: alert fetch + feature → IncomingAlert conversion."""
import datetime
import re
import sys
import time

import requests

sys.path.insert(0, '/app/scripts')
import config
import nwws_parse
from ingest import IncomingAlert


def _session():
    s = requests.Session()
    s.headers.update({
        'User-Agent': config.env('API_USER_AGENT', 'nws-alert-dashboard'),
        'Accept': 'application/geo+json',
    })
    return s


_sess = None


def session():
    global _sess
    if _sess is None:
        _sess = _session()
    return _sess


def base_url():
    return config.env('API_BASE_URL', 'https://api.weather.gov').rstrip('/')


def parse_vtec(vtec_str: str):
    """P-VTEC string → dict (delegates to the shared parser in nwws_parse)."""
    out = nwws_parse.parse_vtec(vtec_str)
    return out[0] if out else None


def _iso_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def feature_to_incoming(feature: dict) -> IncomingAlert:
    p = feature.get('properties', {})
    params = p.get('parameters', {})

    vtec = None
    for v in params.get('VTEC', []):
        vtec = parse_vtec(v)
        if vtec:
            break
    if vtec is None and p.get('messageType') == 'Cancel':
        vtec = {'action': 'CAN', 'key': None}

    fips = {config.normalize_same(c) for c in p.get('geocode', {}).get('SAME', [])}
    ugc  = set(p.get('geocode', {}).get('UGC', []))

    expires = _iso_ts(p.get('ends')) or _iso_ts(p.get('expires'))

    return IncomingAlert(
        source='api',
        event_name=p.get('event') or 'Unknown Event',
        issue_ts=_iso_ts(p.get('sent')) or time.time(),
        eee=config.NWS_EVENT_TO_EEE.get(p.get('event', '')),
        vtec=vtec,
        native_id=p.get('id') or feature.get('id'),
        fips=fips,
        ugc=ugc,
        expires_ts=expires,
        headline=p.get('headline'),
        description=p.get('description'),
        instruction=p.get('instruction'),
        geometry=feature.get('geometry'),
        severity=p.get('severity'),
        onset_ts=_iso_ts(p.get('onset')) or _iso_ts(p.get('effective')),
    )


def get_active_alerts(zones: list) -> list:
    """Fetch active alerts for the given UGC zone/county ids."""
    url = f'{base_url()}/alerts/active'
    params = {'status': 'actual'}
    if zones:
        params['zone'] = ','.join(zones)
    resp = session().get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get('features', [])


def get_zone_geometry(zone_type: str, zone_id: str) -> dict:
    """Fetch a zone definition (county/forecast) with geometry GeoJSON."""
    url = f'{base_url()}/zones/{zone_type}/{zone_id}'
    resp = session().get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()
