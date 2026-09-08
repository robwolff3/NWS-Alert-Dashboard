#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Reverse lookup for county FIPS codes, backed by scripts/data/fips_counties.json
(states + DC from https://transition.fcc.gov/oet/info/maps/census/fips/fips.txt;
PR/VI/GU/AS/MP from api.weather.gov county zones, so their names match the ones
NWS actually uses in alerts).

Accepts either plain 5-digit county FIPS ('21049') or the 6-digit SAME/PSSCCC
form used elsewhere in this project ('021049' — leading subdivision digit).
"""
import json
import sys
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / 'data' / 'fips_counties.json'

# Counties listed per state before the rest are summarised as '+N more'.
MAX_COUNTIES_PER_STATE = 8
# Hard ceiling on the whole string. Per-state truncation alone is unbounded
# across states, and this text is prepended to notification bodies.
MAX_TOTAL_CHARS = 400

_counties = None


def _load() -> dict:
    global _counties
    if _counties is None:
        with open(DATA_PATH, encoding='utf-8') as f:
            _counties = json.load(f)['counties']
    return _counties


def _normalize(code: str):
    """'021049' (PSSCCC) or '21049' -> '21049'. None if it is neither.

    Deliberately strict: zero-padding a short code silently invents a real
    county ('1049' would pad to '01049' = DeKalb AL), and a wrong county name
    on a warning is worse than no county line at all.

    (The 6->5 strip also appears in config.fips_to_county_ugc, which converts
    in the opposite direction to a UGC; sharing it would buy three lines at the
    cost of importing the env layer into this leaf module.)
    """
    code = code.strip()
    if len(code) == 6:
        code = code[1:]
    return code if len(code) == 5 and code.isdigit() else None


def _display_name(county: str) -> str:
    """'Clark County' -> 'Clark'. Leaves Parish/Borough/City/etc. suffixes as-is."""
    return county[:-len(' County')] if county.endswith(' County') else county


def lookup(codes) -> dict:
    """Space-delimited string or iterable of FIPS codes -> {state_abbr: [county, ...]},
    sorted by state abbreviation, counties sorted alphabetically. Unknown codes
    are silently skipped."""
    if isinstance(codes, str):
        codes = codes.split()

    counties = _load()
    by_state = {}
    for raw in codes:
        norm = _normalize(raw)
        entry = counties.get(norm) if norm else None
        if not entry:
            continue
        by_state.setdefault(entry['state'], set()).add(_display_name(entry['county']))

    return {state: sorted(names) for state, names in sorted(by_state.items())}


def format_grouped(codes, max_per_state: int = MAX_COUNTIES_PER_STATE) -> str:
    """'21049 21151 51113' -> 'KY - Clark, Madison\\nVA - Madison'.

    Long lists are truncated per state ('+7 more'): a multi-state watch can
    cover a hundred counties, and this string is prepended to notification
    bodies, where web push caps the encrypted payload at about 4 KB.
    """
    out = []
    for state, names in lookup(codes).items():
        shown = names[:max_per_state] if max_per_state else names
        extra = len(names) - len(shown)
        line = f'{state} - {", ".join(shown)}'
        if extra > 0:
            line += f' +{extra} more'
        out.append(line)

    text = '\n'.join(out)
    if len(text) > MAX_TOTAL_CHARS:
        kept = []
        for i, line in enumerate(out):
            if len('\n'.join(kept + [line])) > MAX_TOTAL_CHARS:
                kept.append(f'+{len(out) - i} more areas')
                break
            kept.append(line)
        text = '\n'.join(kept)
    return text


@lru_cache(maxsize=512)
def format_grouped_cached(codes_json: str) -> str:
    """format_grouped for a JSON-encoded code list. The dashboard rebuilds
    this for every alert on every SSE push, and the inputs repeat."""
    return format_grouped(json.loads(codes_json))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} FIPS [FIPS ...]', file=sys.stderr)
        sys.exit(1)
    print(format_grouped(sys.argv[1:]))
