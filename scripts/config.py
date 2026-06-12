#!/usr/bin/env python3
"""Shared configuration: env helpers, event-code tables, source status file.

Env vars are organized by source prefix (RADIO_*, NWWS_*, API_*) plus
cross-source groups (FILTER_*, NOTIFY_*, MQTT_*, MAP_*). Old pre-rearchitecture
names are detected at import and warned about — there are no compat shims.
"""
import json
import os
import tempfile
import time

SOURCE_STATUS_PATH = '/tmp/source_status.json'

# Old env names from the single-source era → their replacements.
_RENAMED_VARS = {
    'FREQUENCY': 'RADIO_FREQUENCY',
    'FREQUENCY_FALLBACK': 'RADIO_FREQUENCY_FALLBACK',
    'SILENCE_TIMEOUT_HOURS': 'RADIO_SILENCE_TIMEOUT_HOURS',
    'FAILBACK_HOURS': 'RADIO_FAILBACK_HOURS',
    'GAIN': 'RADIO_GAIN',
    'PPM': 'RADIO_PPM',
    'BANDWIDTH': 'RADIO_BANDWIDTH',
    'SQUELCH': 'RADIO_SQUELCH',
    'VOICE_MESSAGE_WAIT': 'RADIO_VOICE_WAIT',
    'HEADER_LEAD_SECS': 'RADIO_HEADER_LEAD_SECS',
    'EOM_TRAIL_SECS': 'RADIO_EOM_TRAIL_SECS',
    'SAME_CODES': 'FILTER_SAME_CODES',
    'EVENT_CODES': 'FILTER_EVENT_CODES',
    'PRIORITY_5_CODES': 'NOTIFY_PRIORITY_5_CODES',
    'PRIORITY_4_CODES': 'NOTIFY_PRIORITY_4_CODES',
    'PRIORITY_3_CODES': 'NOTIFY_PRIORITY_3_CODES',
    'PRIORITY_5_TOPIC': 'NTFY_PRIORITY_5_TOPIC',
    'PRIORITY_4_TOPIC': 'NTFY_PRIORITY_4_TOPIC',
    'PRIORITY_3_TOPIC': 'NTFY_PRIORITY_3_TOPIC',
    'VAPID_EMAIL': 'PUSH_VAPID_EMAIL',
    'WYOMING_HOST': None,   # removed entirely (transcription dropped)
    'WYOMING_PORT': None,
}


def warn_old_vars():
    for old, new in _RENAMED_VARS.items():
        if os.environ.get(old):
            if new:
                print(f"config: WARNING — env var {old} is no longer read; use {new}", flush=True)
            else:
                print(f"config: WARNING — env var {old} is obsolete and ignored", flush=True)


def env(name, default=''):
    return os.environ.get(name, default)


def env_int(name, default):
    try:
        return int(os.environ.get(name, '') or default)
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(os.environ.get(name, '') or default)
    except ValueError:
        return default


def env_bool(name, default=False):
    v = os.environ.get(name, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'on')


def env_list(name, default=''):
    """Space-separated list env var → list of strings."""
    return os.environ.get(name, default).split()


# ── Event code tables ─────────────────────────────────────────────────────────
# VTEC (phenomena, significance) → SAME EEE code. Only pairs with a real SAME
# equivalent are listed; everything else stays unmapped and is matched by
# NWS event name instead.
VTEC_TO_EEE = {
    ('TO', 'W'): 'TOR', ('TO', 'A'): 'TOA',
    ('SV', 'W'): 'SVR', ('SV', 'A'): 'SVA',
    ('FF', 'W'): 'FFW', ('FF', 'A'): 'FFA',
    ('FL', 'W'): 'FLW', ('FL', 'A'): 'FLA', ('FL', 'Y'): 'FLS',
    ('EW', 'W'): 'EWW',
    ('SQ', 'W'): 'SQW',
    ('MA', 'W'): 'SMW',
    ('SS', 'W'): 'SSW', ('SS', 'A'): 'SSA',
    ('HU', 'W'): 'HUW', ('HU', 'A'): 'HUA',
    ('TR', 'W'): 'TRW', ('TR', 'A'): 'TRA',
    ('TS', 'W'): 'TSW', ('TS', 'A'): 'TSA',
    ('WS', 'W'): 'WSW', ('WS', 'A'): 'WSA',
    ('BZ', 'W'): 'BZW',
    ('HW', 'W'): 'HWW', ('HW', 'A'): 'HWA',
    ('DS', 'W'): 'DSW',
    ('FZ', 'W'): 'FZW',
    ('CF', 'W'): 'CFW', ('CF', 'A'): 'CFA',
}
EEE_TO_VTEC = {}
for _pair, _eee in VTEC_TO_EEE.items():
    EEE_TO_VTEC.setdefault(_eee, _pair)

# api.weather.gov properties.event → SAME EEE. Used to route API/NWWS-first
# alerts through the EEE-based priority machinery and to match radio decodes.
NWS_EVENT_TO_EEE = {
    'Tornado Warning': 'TOR', 'Tornado Watch': 'TOA',
    'Severe Thunderstorm Warning': 'SVR', 'Severe Thunderstorm Watch': 'SVA',
    'Severe Weather Statement': 'SVS',
    'Flash Flood Warning': 'FFW', 'Flash Flood Watch': 'FFA',
    'Flash Flood Statement': 'FFS',
    'Flood Warning': 'FLW', 'Flood Watch': 'FLA',
    'Flood Advisory': 'FLS', 'Flood Statement': 'FLS',
    'Extreme Wind Warning': 'EWW',
    'Snow Squall Warning': 'SQW',
    'Special Marine Warning': 'SMW',
    'Storm Surge Warning': 'SSW', 'Storm Surge Watch': 'SSA',
    'Hurricane Warning': 'HUW', 'Hurricane Watch': 'HUA',
    'Hurricane Local Statement': 'HLS',
    'Tropical Storm Warning': 'TRW', 'Tropical Storm Watch': 'TRA',
    'Tsunami Warning': 'TSW', 'Tsunami Watch': 'TSA',
    'Winter Storm Warning': 'WSW', 'Winter Storm Watch': 'WSA',
    'Blizzard Warning': 'BZW',
    'High Wind Warning': 'HWW', 'High Wind Watch': 'HWA',
    'Dust Storm Warning': 'DSW',
    'Freeze Warning': 'FZW',
    'Coastal Flood Warning': 'CFW', 'Coastal Flood Watch': 'CFA',
    'Civil Emergency Message': 'CEM',
    'Civil Danger Warning': 'CDW',
    'Law Enforcement Warning': 'LEW',
    'Local Area Emergency': 'LAE',
    'Evacuation - Immediate': 'EVI', 'Evacuation Immediate': 'EVI',
    'Shelter In Place Warning': 'SPW',
    'Hazardous Materials Warning': 'HMW',
    'Nuclear Power Plant Warning': 'NUW',
    'Radiological Hazard Warning': 'RHW',
    'Fire Warning': 'FRW',
    'Earthquake Warning': 'EQW',
    'Volcano Warning': 'VOW',
    'Avalanche Warning': 'AVW', 'Avalanche Watch': 'AVA',
    'Special Weather Statement': 'SPS',
    '911 Telephone Outage Emergency': 'TOE',
    'Child Abduction Emergency': 'CAE',
}

# State/territory USPS abbreviation → 2-digit FIPS, for UGC↔FIPS conversion.
STATE_ABBR_TO_FIPS = {
    'AL': '01', 'AK': '02', 'AZ': '04', 'AR': '05', 'CA': '06', 'CO': '08',
    'CT': '09', 'DE': '10', 'DC': '11', 'FL': '12', 'GA': '13', 'HI': '15',
    'ID': '16', 'IL': '17', 'IN': '18', 'IA': '19', 'KS': '20', 'KY': '21',
    'LA': '22', 'ME': '23', 'MD': '24', 'MA': '25', 'MI': '26', 'MN': '27',
    'MS': '28', 'MO': '29', 'MT': '30', 'NE': '31', 'NV': '32', 'NH': '33',
    'NJ': '34', 'NM': '35', 'NY': '36', 'NC': '37', 'ND': '38', 'OH': '39',
    'OK': '40', 'OR': '41', 'PA': '42', 'RI': '44', 'SC': '45', 'SD': '46',
    'TN': '47', 'TX': '48', 'UT': '49', 'VT': '50', 'VA': '51', 'WA': '53',
    'WV': '54', 'WI': '55', 'WY': '56', 'PR': '72', 'VI': '78', 'GU': '66',
    'AS': '60', 'MP': '69',
}
FIPS_TO_STATE_ABBR = {v: k for k, v in STATE_ABBR_TO_FIPS.items()}


def ugc_county_to_fips(ugc: str):
    """'MIC163' → '26163'. Returns None for zone UGCs (MIZ075) and unknowns."""
    if len(ugc) != 6 or ugc[2] != 'C':
        return None
    state = STATE_ABBR_TO_FIPS.get(ugc[:2])
    return state + ugc[3:] if state else None


def fips_to_county_ugc(fips: str):
    """'26163' or '026163' (PSSCCC) → 'MIC163'. Returns None if unknown."""
    if len(fips) == 6:
        fips = fips[1:]  # strip SAME subdivision digit
    if len(fips) != 5:
        return None
    abbr = FIPS_TO_STATE_ABBR.get(fips[:2])
    return f'{abbr}C{fips[2:]}' if abbr else None


def normalize_same(code: str):
    """Normalize a SAME/FIPS code to 6-digit PSSCCC form ('26163' → '026163')."""
    code = code.strip()
    if len(code) == 5:
        return '0' + code
    return code


# ── Notification priority routing (EEE → priority, topic) ────────────────────

def priority_for_eee(eee: str):
    """Returns (priority 1-5, ntfy topic). Mirrors the old notify.py logic."""
    for p in (5, 4, 3):
        if eee and eee in env(f'NOTIFY_PRIORITY_{p}_CODES').split():
            topic = env(f'NTFY_PRIORITY_{p}_TOPIC', env('NTFY_TOPIC_DEFAULT', 'nws'))
            return p, topic
    return env_int('NTFY_PRIORITY_DEFAULT', 3), env('NTFY_TOPIC_DEFAULT', 'nws')


# ── Cross-source filters ──────────────────────────────────────────────────────

def filter_same_codes():
    """Configured SAME codes, normalized to 6-digit PSSCCC. Empty = accept all."""
    return {normalize_same(c) for c in env_list('FILTER_SAME_CODES')}


def filter_event_codes():
    """Accepted EEE codes. Empty = accept all."""
    return set(env_list('FILTER_EVENT_CODES'))


def filter_zones():
    """Accepted UGC zone ids (forecast zones, for zone-based NWWS/API products)."""
    return set(env_list('FILTER_ZONES'))


# ── Per-source status file (for web UI + degraded-mode polling) ───────────────

def get_source_status() -> dict:
    try:
        with open(SOURCE_STATUS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def set_source_status(source: str, **fields):
    """Merge fields into the named source's status entry (atomic write)."""
    status = get_source_status()
    entry = status.setdefault(source, {})
    entry.update(fields)
    entry['updated'] = time.time()
    fd, tmp = tempfile.mkstemp(dir='/tmp', prefix='srcstat.')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(status, f)
        os.replace(tmp, SOURCE_STATUS_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
