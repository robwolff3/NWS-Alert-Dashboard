#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Pure parsing functions for raw NWS text products (NWWS-OI payloads).

No I/O, no env — unit-testable in isolation. Handles:
  - NWWS-OI doubled-newline normalization
  - segment splitting ($$ delimited, UGC-anchored)
  - UGC expansion (MIC001>005-163-120515-) with purge time
  - P-VTEC parsing
  - LAT...LON storm polygon → GeoJSON
  - MND event-name extraction for non-VTEC products (SPS, CEM, ...)
"""
import datetime
import re
import sys

sys.path.insert(0, '/app/scripts')
import config

# P-VTEC: /O.NEW.KDTX.TO.W.0042.260612T2130Z-260612T2200Z/
VTEC_RE = re.compile(
    r'([OTEX])\.'
    r'(NEW|CON|EXT|EXA|EXB|CAN|EXP|UPG|COR|ROU)\.'
    r'([A-Z]{4})\.'
    r'([A-Z]{2})\.'
    r'([WAYSFON])\.'
    r'(\d{4})\.'
    r'(\d{6}T\d{4}Z|000000T0000Z)-'
    r'(\d{6}T\d{4}Z|000000T0000Z)')

# A UGC line: tokens like MIC163, 099, 001>005, ending with ddHHMM purge + '-'
UGC_LINE_RE = re.compile(r'^[A-Z]{2}[CZ]\d{3}[->]')
UGC_CONT_RE = re.compile(r'^[\dA-Z>-]+-$')

LATLON_RE = re.compile(r'LAT\.\.\.LON((?:[\s\n]+\d{3,5})+)')


def vtec_ts(s: str):
    if s == '000000T0000Z':
        return None
    dt = datetime.datetime.strptime(s, '%y%m%dT%H%MZ').replace(
        tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def parse_vtec(text: str) -> list:
    """All P-VTEC strings in text → list of dicts (action/office/.../key)."""
    out = []
    for m in VTEC_RE.finditer(text):
        _cls, action, office, phen, sig, etn, begin, end = m.groups()
        begin_ts = vtec_ts(begin)
        end_ts   = vtec_ts(end)
        year = datetime.datetime.fromtimestamp(
            begin_ts or end_ts or datetime.datetime.now().timestamp(),
            datetime.timezone.utc).year
        out.append({
            'action': action, 'office': office, 'phen': phen, 'sig': sig,
            'etn': etn, 'begin_ts': begin_ts, 'end_ts': end_ts,
            'key': f'{office}.{phen}.{sig}.{etn}.{year}',
        })
    return out


def normalize(text: str) -> str:
    """Collapse the NWWS-OI doubled-newline quirk when it dominates."""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    doubles = text.count('\n\n')
    singles = text.count('\n') - 2 * doubles
    if doubles > singles:
        text = text.replace('\n\n', '\n')
    return text


def expand_ugc_tokens(tokens: list) -> tuple:
    """UGC tokens → (set of UGC ids, purge 'ddHHMM' or None).

    Tokens: 'MIC163' (full), '099' (same prefix), '001>005' (range),
    final 6-digit numeric token = purge time.
    """
    ugc = set()
    purge = None
    prefix = None

    def add(prefix, num):
        ugc.add(f'{prefix}{int(num):03d}')

    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r'^([A-Z]{2}[CZ])(\d{3})(?:>(\d{3}))?$', tok)
        if m:
            prefix = m.group(1)
            if m.group(3):
                for n in range(int(m.group(2)), int(m.group(3)) + 1):
                    add(prefix, n)
            else:
                add(prefix, m.group(2))
            continue
        m = re.match(r'^(\d{3})(?:>(\d{3}))?$', tok)
        if m and prefix:
            if m.group(2):
                for n in range(int(m.group(1)), int(m.group(2)) + 1):
                    add(prefix, n)
            else:
                add(prefix, m.group(1))
            continue
        if re.match(r'^\d{6}$', tok):
            purge = tok
    return ugc, purge


def parse_ugc_block(lines: list) -> tuple:
    """Consecutive UGC lines → (ugc set, purge). Lines must end with '-'."""
    tokens = []
    for line in lines:
        tokens.extend(line.strip().rstrip('-').split('-'))
    return expand_ugc_tokens(tokens)


def purge_to_ts(purge: str, ref_ts: float):
    """'ddHHMM' UTC → epoch nearest to ref_ts (handles month boundaries)."""
    if not purge:
        return None
    try:
        day, hh, mm = int(purge[:2]), int(purge[2:4]), int(purge[4:6])
    except ValueError:
        return None
    ref = datetime.datetime.fromtimestamp(ref_ts, datetime.timezone.utc)
    candidates = []
    for month_offset in (-1, 0, 1):
        y, mo = ref.year, ref.month + month_offset
        if mo < 1:
            y, mo = y - 1, 12
        elif mo > 12:
            y, mo = y + 1, 1
        try:
            candidates.append(datetime.datetime(
                y, mo, day, hh, mm, tzinfo=datetime.timezone.utc).timestamp())
        except ValueError:
            pass
    if not candidates:
        return None
    return min(candidates, key=lambda t: abs(t - ref_ts))


def parse_latlon(text: str):
    """LAT...LON block → GeoJSON Polygon (hundredths of degrees, lon west)."""
    m = LATLON_RE.search(text)
    if not m:
        return None
    nums = [int(n) for n in m.group(1).split()]
    if len(nums) < 6 or len(nums) % 2:
        return None
    coords = []
    for i in range(0, len(nums), 2):
        lat = nums[i] / 100.0
        lon = -(nums[i + 1] / 100.0)
        if lon > -30:        # 5-digit lon without leading 1 swallowed elsewhere
            lon -= 100.0     # not expected, defensive
        coords.append([lon, lat])
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return {'type': 'Polygon', 'coordinates': [coords]}


def split_segments(text: str) -> list:
    """Product → list of segment dicts: {ugc, purge, vtec(list), text}.

    A segment starts at a UGC line and ends at '$$' (or product end).
    """
    lines = text.split('\n')
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if UGC_LINE_RE.match(line):
            ugc_lines = [line]
            j = i + 1
            # UGC blocks may wrap; continuation lines are bare token runs
            # ending in '-' (e.g. '099-125-130200-'), never VTEC or a new UGC
            while j < len(lines):
                nxt = lines[j].strip()
                if nxt and UGC_CONT_RE.match(nxt) and not VTEC_RE.search(nxt) \
                        and not UGC_LINE_RE.match(nxt):
                    ugc_lines.append(nxt)
                    j += 1
                else:
                    break
            # find end of segment
            end = j
            while end < len(lines) and lines[end].strip() != '$$':
                end += 1
            seg_text = '\n'.join(lines[i:end])
            ugc, purge = parse_ugc_block(ugc_lines)
            segments.append({
                'ugc': ugc,
                'purge': purge,
                'vtec': parse_vtec(seg_text),
                'text': seg_text,
            })
            i = end + 1
        else:
            i += 1
    return segments


# Uppercased NWS event names for MND scanning (non-VTEC products)
_EVENT_NAMES_UPPER = {name.upper(): name for name in config.NWS_EVENT_TO_EEE}


def extract_event_name(text: str):
    """Find a known NWS event name in the MND header (first ~20 lines)."""
    for line in text.split('\n')[:20]:
        candidate = line.strip().rstrip('.').upper()
        if candidate in _EVENT_NAMES_UPPER:
            return _EVENT_NAMES_UPPER[candidate]
    return None


def parse_wmo_header(text: str):
    """Extract (ttaaii, cccc, ddhhmm, awipsid) from the product preamble."""
    ttaaii = cccc = ddhhmm = awipsid = None
    for line in text.split('\n')[:8]:
        line = line.strip()
        m = re.match(r'^([A-Z]{4}\d{2})\s+([A-Z]{4})\s+(\d{6})$', line)
        if m:
            ttaaii, cccc, ddhhmm = m.groups()
            continue
        if ttaaii and re.match(r'^[A-Z0-9]{4,6}$', line):
            awipsid = line
            break
    return ttaaii, cccc, ddhhmm, awipsid
