#!/usr/bin/env python3
"""
Sits in the pipeline between rtl_fm and multimon-ng.
Passes raw PCM through to stdout unchanged while simultaneously
writing rolling 60-second WAV segments to /recordings.

Pipeline: rtl_fm | recorder.py | multimon-ng
"""
import os
import queue
import signal
import sys
import threading
import time
import wave
from pathlib import Path

SAMPLE_RATE   = 22050
CHANNELS      = 1
SAMPLE_WIDTH  = 2        # 16-bit
SEGMENT_SECS  = 60
KEEP_SEGMENTS = 12       # 12 minutes of rolling history
FIFO_PATH     = '/tmp/audio_fifo'
ALIVE_PATH    = '/tmp/radio_alive'   # touched while audio is flowing (radio health)

_shutdown = False


def _fifo_writer(audio_queue):
    """Background thread: waits for a browser listener, then streams PCM into the FIFO."""
    while True:
        try:
            # Blocks here until Flask opens the read end (browser connects)
            fd = os.open(FIFO_PATH, os.O_WRONLY)
        except OSError:
            time.sleep(1)
            continue
        try:
            with os.fdopen(fd, 'wb', buffering=0) as f:
                while True:
                    try:
                        chunk = audio_queue.get(timeout=1)
                    except queue.Empty:
                        continue
                    f.write(chunk)
        except (BrokenPipeError, OSError):
            pass
        # Reader disconnected — drain stale data and wait for the next connection

def _handle(sig, frame):
    global _shutdown
    _shutdown = True

signal.signal(signal.SIGTERM, _handle)
signal.signal(signal.SIGINT,  _handle)

def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else '/recordings')
    out_dir.mkdir(parents=True, exist_ok=True)

    bytes_per_sec = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH
    segment_bytes = SEGMENT_SECS * bytes_per_sec
    chunk_size    = bytes_per_sec // 10   # 100 ms

    # ~5 s ring buffer for the live stream; drop oldest if listener is slow
    audio_queue = queue.Queue(maxsize=50)
    t = threading.Thread(target=_fifo_writer, args=(audio_queue,), daemon=True)
    t.start()

    stdin  = sys.stdin.buffer
    stdout = sys.stdout.buffer

    wav          = None
    current_path = None
    written      = 0
    last_alive   = 0.0

    # Pre-populate from disk so restarts don't orphan old segments
    segments = sorted(out_dir.glob('seg_*.wav'))
    while len(segments) > KEEP_SEGMENTS:
        try:
            segments.pop(0).unlink()
        except OSError:
            pass

    while not _shutdown:
        data = stdin.read(chunk_size)
        if not data:
            break

        # Pass through to multimon-ng
        stdout.write(data)
        stdout.flush()

        # Radio liveness heartbeat — goes stale within seconds if rtl_fm dies,
        # so the dashboard can flag the radio as down (touch at most every ~3s)
        now = time.time()
        if now - last_alive > 3:
            try:
                os.utime(ALIVE_PATH, None)
            except OSError:
                try:
                    open(ALIVE_PATH, 'wb').close()
                except OSError:
                    pass
            last_alive = now

        # Feed live stream queue (non-blocking — drop if nobody is listening)
        try:
            audio_queue.put_nowait(data)
        except queue.Full:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                pass
            audio_queue.put_nowait(data)

        # Open a new segment file when needed
        if wav is None or written >= segment_bytes:
            if wav:
                wav.close()
                segments.append(current_path)
                while len(segments) > KEEP_SEGMENTS:
                    try:
                        segments.pop(0).unlink()
                    except OSError:
                        pass

            ts           = time.strftime('%Y%m%d_%H%M%S')
            current_path = out_dir / f'seg_{ts}.wav'
            n = 1
            while current_path.exists():
                current_path = out_dir / f'seg_{ts}_{n}.wav'
                n += 1
            wav          = wave.open(str(current_path), 'wb')
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_WIDTH)
            wav.setframerate(SAMPLE_RATE)
            written = 0

        wav.writeframes(data)
        written += len(data)

    if wav:
        wav.close()

if __name__ == '__main__':
    main()
