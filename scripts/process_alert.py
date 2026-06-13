#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Background audio extractor for radio-decoded alerts.

Called by notify.py as:
  process_alert.py <alert_id> <alert_start_epoch>

Watches multimon.log for the EOM (NNNN) marker to detect when the voice
message ends, then merges the rolling recorder segments covering the alert
window into a single WAV stored against the alert row. No transcription —
the NOAA REST API supplies the alert text when the internet is up; offline,
the recording itself is the record.
"""
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

MULTIMON_LOG = '/tmp/multimon.log'

sys.path.insert(0, '/app/scripts')
import alerts as alertdb
import config


def wait_for_nnnn(start_pos: int, timeout: float) -> Optional[float]:
    """Poll multimon.log for EOM (NNNN) from start_pos. Returns wall time when detected, None on timeout."""
    deadline = time.time() + timeout
    pos = start_pos
    while time.time() < deadline:
        try:
            with open(MULTIMON_LOG, 'r') as f:
                f.seek(pos)
                for line in f:
                    if 'EAS: NNNN' in line:
                        return time.time()
                pos = f.tell()
        except OSError:
            pass
        time.sleep(2)
    return None


def find_segments(window_start: float, window_end: float) -> list:
    recordings = Path(os.environ.get('RECORDINGS_DIR', '/recordings'))
    found = []
    for f in sorted(recordings.glob('seg_*.wav')):
        try:
            ts      = time.mktime(time.strptime(f.stem[4:19], '%Y%m%d_%H%M%S'))
            seg_end = ts + 60
            if seg_end >= window_start and ts <= window_end:
                found.append(f)
        except ValueError:
            pass
    return found


def resample_and_merge(wav_files: list, output_path: str,
                       trim_start: float = 0.0,
                       trim_duration: Optional[float] = None) -> bool:
    """Concatenate WAV segments, resample to 16 kHz mono, optionally trim, write to output_path."""
    if not wav_files:
        return False
    cmd = ['sox'] + [str(f) for f in wav_files] + ['-r', '16000', '-c', '1', output_path]
    if trim_start > 0.5 or trim_duration is not None:
        cmd += ['trim', f'{trim_start:.2f}']
        if trim_duration is not None and trim_duration > 0:
            cmd.append(f'{trim_duration:.2f}')
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"process_alert: sox error: {result.stderr.decode()}", flush=True)
        return False
    return True


def main():
    alert_id    = sys.argv[1]
    alert_start = float(sys.argv[2])

    wait        = config.env_int('RADIO_VOICE_WAIT', 180)
    # Seconds before alert_start to include (first SAME burst decodes ~1-2s after it starts)
    header_lead = config.env_int('RADIO_HEADER_LEAD_SECS', 5)
    # Seconds after first NNNN detection to include (EOM is 3 bursts, ~5s total)
    trail       = config.env_int('RADIO_EOM_TRAIL_SECS', 6)

    record_start = alert_start - header_lead

    # Capture log position before NNNN arrives so we don't match stale EOM lines
    try:
        log_pos = Path(MULTIMON_LOG).stat().st_size
    except OSError:
        log_pos = 0

    print(f"process_alert: alert {alert_id} — watching for EOM (timeout {wait}s)", flush=True)
    nnnn_time = wait_for_nnnn(log_pos, wait)

    if nnnn_time is not None:
        print(f"process_alert: EOM detected at +{nnnn_time - alert_start:.0f}s — waiting {trail}s for EOM to finish", flush=True)
        time.sleep(trail)
        eos_time = nnnn_time + trail
    else:
        eos_time = alert_start + wait
        print(f"process_alert: EOM timeout after {wait}s — using full window", flush=True)

    # Include a small buffer so segment boundary rounding doesn't drop audio
    segments = find_segments(record_start - 5, eos_time + 5)
    print(f"process_alert: found {len(segments)} audio segment(s)", flush=True)

    if not segments:
        return

    # Trim the merged audio to [record_start, eos_time] using sox
    first_seg_ts = time.mktime(time.strptime(segments[0].stem[4:19], '%Y%m%d_%H%M%S'))
    trim_start   = max(0.0, record_start - first_seg_ts)
    trim_dur     = max(0.0, eos_time - record_start)
    # If the window is implausibly short, skip trimming and use everything
    if trim_dur < 5:
        trim_start, trim_dur = 0.0, None

    audio_path = str(Path(alertdb.AUDIO_DIR) / f"{alert_id}.wav")
    tmp_path   = audio_path + '.tmp.wav'
    try:
        if resample_and_merge(segments, tmp_path, trim_start, trim_dur):
            Path(tmp_path).rename(audio_path)
            alertdb.update_alert_audio(alert_id, f"{alert_id}.wav")
            print(f"process_alert: saved audio for {alert_id}", flush=True)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
