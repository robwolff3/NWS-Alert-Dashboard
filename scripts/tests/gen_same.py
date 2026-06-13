#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Generate a synthetic SAME/EAS alert as raw PCM (22050 Hz s16le mono).

Produces: 3× header bursts, attention-tone, a silent "voice message",
3× EOM bursts — decodable by multimon-ng, so the full pipeline
(recorder → multimon-ng → dsame3 → notify.py) can be tested without
a real broadcast.

Usage:
  gen_same.py [--header 'ZCZC-WXR-RWT-026163+0030-1641830-KTST/NWS-'] [--voice-secs 8] > out.raw
"""
import argparse
import datetime
import math
import struct
import sys

RATE  = 22050
BAUD  = 520.83
MARK  = 2083.3   # Hz, bit 1
SPACE = 1562.5   # Hz, bit 0
PREAMBLE = b'\xab' * 16


def afsk(data: bytes, amp: float = 0.7):
    """Phase-continuous AFSK samples for the given bytes (LSB first)."""
    samples = []
    phase = 0.0
    t_per_bit = 1.0 / BAUD
    bit_stream = []
    for byte in data:
        for i in range(8):
            bit_stream.append((byte >> i) & 1)
    total_secs = len(bit_stream) * t_per_bit
    n_samples = int(total_secs * RATE)
    for n in range(n_samples):
        t = n / RATE
        bit = bit_stream[min(int(t * BAUD), len(bit_stream) - 1)]
        freq = MARK if bit else SPACE
        phase += 2 * math.pi * freq / RATE
        samples.append(amp * math.sin(phase))
    return samples


def tone(freq: float, secs: float, amp: float = 0.5):
    return [amp * math.sin(2 * math.pi * freq * n / RATE)
            for n in range(int(secs * RATE))]


def silence(secs: float):
    return [0.0] * int(secs * RATE)


def main():
    ap = argparse.ArgumentParser()
    default_jjjhhmm = datetime.datetime.now(datetime.timezone.utc).strftime('%j%H%M')
    ap.add_argument('--header',
                    default=f'ZCZC-WXR-RWT-026163+0030-{default_jjjhhmm}-KTST/NWS-')
    ap.add_argument('--voice-secs', type=float, default=8.0)
    args = ap.parse_args()

    out = []
    header = PREAMBLE + args.header.encode('ascii')
    for _ in range(3):
        out += afsk(header)
        out += silence(1.0)
    out += silence(1.0)
    out += tone(1050.0, 8.0)          # NWR attention tone
    out += silence(args.voice_secs)   # "voice message"
    eom = PREAMBLE + b'NNNN'
    for _ in range(3):
        out += afsk(eom)
        out += silence(1.0)
    out += silence(2.0)

    buf = bytearray()
    for s in out:
        buf += struct.pack('<h', max(-32767, min(32767, int(s * 32767))))
    sys.stdout.buffer.write(bytes(buf))


if __name__ == '__main__':
    main()
