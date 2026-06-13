#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Radio-source adapter, called by dsame3 per decoded SAME header:

  notify.py <ORG> <EEE> <PSSCCC> <TTTT> <JJJHHMM> <LLLLLLLL> <event> <MESSAGE>

Builds an IncomingAlert and hands it to the shared ingest core (which dedups
against NWWS/API arrivals, persists, and notifies exactly once), then spawns
process_alert.py to extract the broadcast audio for the canonical alert row.
"""
import datetime
import subprocess
import sys
import time

sys.path.insert(0, '/app/scripts')
import config
from ingest import IncomingAlert, ingest


def parse_issue_ts(jjjhhmm: str, now: float) -> float:
    """SAME issue time (UTC julian day + HHMM) → epoch. Falls back to now."""
    try:
        day, hh, mm = int(jjjhhmm[:3]), int(jjjhhmm[3:5]), int(jjjhhmm[5:7])
        year = datetime.datetime.now(datetime.timezone.utc).year
        dt = (datetime.datetime(year, 1, 1, hh, mm, tzinfo=datetime.timezone.utc)
              + datetime.timedelta(days=day - 1))
        ts = dt.timestamp()
        # Year-boundary: a December decode read in January (or vice versa)
        if ts - now > 180 * 86400:
            ts -= 365 * 86400
        elif now - ts > 180 * 86400:
            ts += 365 * 86400
        return ts
    except (ValueError, OverflowError):
        return now


def parse_purge_ts(tttt: str, base: float) -> float:
    """SAME purge field (HHMM duration) → expiry epoch."""
    try:
        return base + int(tttt[:2]) * 3600 + int(tttt[2:4]) * 60
    except (ValueError, IndexError):
        return base + 3600


def main():
    if len(sys.argv) < 9:
        print(f'usage: {sys.argv[0]} <ORG> <EEE> <PSSCCC> <TTTT> <JJJHHMM> '
              f'<LLLLLLLL> <event> <MESSAGE>', file=sys.stderr)
        sys.exit(1)

    org, eee, psscccc, tttt, jjjhhmm, station, event_name, message = sys.argv[1:9]
    now = time.time()

    fips = {config.normalize_same(c) for c in psscccc.split('-') if c.strip()}

    # The decode just happened; SAME issue time is when the originator issued
    # it (can lag the broadcast). Use wall clock for the alert window so the
    # audio extraction lines up with the recording timeline.
    issue_ts = now

    alert_id = ingest(IncomingAlert(
        source='radio',
        event_name=event_name,
        issue_ts=issue_ts,
        eee=eee,
        fips=fips,
        expires_ts=parse_purge_ts(tttt, issue_ts),
        raw_text=message,
    ))

    if not alert_id:
        return

    # Extract the broadcast audio in the background (detached so the dsame3
    # pipeline isn't blocked while we wait for the EOM marker).
    subprocess.Popen(
        [sys.executable, '/app/scripts/process_alert.py', alert_id, str(now)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


if __name__ == '__main__':
    main()
