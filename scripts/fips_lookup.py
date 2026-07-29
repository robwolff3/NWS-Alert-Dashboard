#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Reverse lookup for county FIPS codes, backed by scripts/data/fips_counties.json
(source: https://transition.fcc.gov/oet/info/maps/census/fips/fips.txt).

Accepts either plain 5-digit county FIPS ('21049') or the 6-digit SAME/PSSCCC
form used elsewhere in this project ('021049' — leading subdivision digit).
"""
import json
import sys
from pathlib import Path

DATA_PATH = Path(__file__).parent / 'data' / 'fips_counties.json'

_counties = None


def _load() -> dict:
    global _counties
    if _counties is None:
        with open(DATA_PATH, encoding='utf-8') as f:
            _counties = json.load(f)['counties']
    return _counties


def _normalize(code: str) -> str:
    """'021049' (PSSCCC) or '21049' -> '21049'."""
    code = code.strip()
    if len(code) == 6:
        code = code[1:]
    return code.zfill(5)


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
        entry = counties.get(_normalize(raw))
        if not entry:
            continue
        by_state.setdefault(entry['state'], set()).add(_display_name(entry['county']))

    return {state: sorted(names) for state, names in sorted(by_state.items())}


def format_grouped(codes) -> str:
    """'21049 21151 51113' -> 'KY - Clark, Madison\\nVA - Madison'."""
    return '\n'.join(f'{state} - {", ".join(names)}'
                      for state, names in lookup(codes).items())


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} FIPS [FIPS ...]', file=sys.stderr)
        sys.exit(1)
    print(format_grouped(sys.argv[1:]))
