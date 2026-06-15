# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**NWS Alert Dashboard** (directory and container named `nwsalertdashboard`): a
multi-source NWS alert monitor. Three independently toggleable sources —
NWWS-OI (XMPP push), NOAA Weather Radio (RTL-SDR + SAME decode), and the
NOAA REST API (polling) — feed one ingest core that dedups across sources,
notifies exactly once (Apprise + web push + optional MQTT), renders alert
polygon maps offline, and serves a Flask/SSE dashboard.

## Build & Run

```bash
# Rebuild image and recreate container (required after any script/Dockerfile change)
docker compose up -d --build --force-recreate

# View live logs (daemon output is prefixed: [web], [api], [nwws])
docker compose logs -f

# Restart without rebuilding (picks up .env changes only)
docker compose up -d --force-recreate
```

`compose.yaml` is generic (published on GitHub); host specifics (RTL-SDR
device, `borked` network) live in gitignored `compose.override.yaml`.

## Architecture

```
RADIO: rtl_fm → recorder.py → multimon-ng → tee /tmp/multimon.log → dsame3 --call notify.py ─┐
NWWS:  nwws_client.py (slixmpp MUC, host failover) → nwws_parse.py ─────────────────────────┼→ ingest.py
API:   api_poller.py (alerts/active; fast-poll when NWWS down; /tmp/poll_now trigger) ──────┘     │
                                              notify-once → notifier.py (Apprise) + push.py + mqtt_pub.py
web.py (Flask 8082: SSE, Leaflet, /tiles, test-alert) ←── alerts.py (SQLite /alerts/alerts.db, WAL)
map_cache.py (startup: zone GeoJSON + OSM tiles → /alerts/mapdata)   maps.py (Pillow PNG renderer)
```

`run.sh` is the orchestrator: runs `autosetup.py` (LOCATION → derived env),
supervises the web/api/nwws daemons with a restart loop, manages radio
frequency rotation (silence timeout → rotate, failback timer → primary), and
runs the radio pipeline in the foreground when `RADIO_ENABLED=true`.

## Ingest / dedup core (`ingest.py`)

- All sources construct an `IncomingAlert` and call `ingest()` → returns the
  canonical row id. Match order: (1) `vtec_key` = office.phen.sig.etn.year,
  (2) native id (`api_id`/`nwws_id`), (3) heuristic: EEE event equivalence
  (`config.VTEC_TO_EEE` / `NWS_EVENT_TO_EEE`) + county FIPS overlap + time
  window (`DEDUP_WINDOW_SECS`). Distinct VTEC keys never heuristic-match
  (new ETN = new alert).
- Match+write run inside `BEGIN IMMEDIATE`; notification is claimed
  atomically (`notified_at IS NULL` update) so exactly one source notifies.
- Merges union fips/ugc, extend expiry on CON/EXT, expire the row on
  CAN/EXP/UPG. Radio fills null fields only (never overwrites rich text);
  rich sources (NWWS/API) also *overwrite* revised content
  (`_CONTENT`: headline/description/instruction/geometry/severity) when NWS
  reissues an alert in place. A true revision (a prior non-null value changed)
  is snapshotted into the `revisions` JSON (newest-capped by
  `REVISION_HISTORY_MAX`) and bumps `update_count`/`updated_at`; the dashboard
  shows an UPDATED badge + collapsed revision history.
- A merge re-notifies only when `RENOTIFY_ON_UPDATE` (off | escalation | all,
  default escalation) matches a content revision — escalation = severity rank
  rise or newly-added "Tornado Emergency"/"Particularly Dangerous Situation"
  wording — throttled by `RENOTIFY_MIN_INTERVAL_SECS` (clock: `renotified_at`).
  Otherwise a merge stays silent, except one map follow-up when geometry
  arrives after notification (`NOTIFY_MAP_FOLLOWUP`).
- Radio decodes touch `/tmp/last_decode` (silence clock) and `/tmp/poll_now`
  (immediate API enrichment poll).

## Key invariants & gotchas

- Base image pins `rtl-sdr 0.6.0-3` from Debian bullseye — the newer
  RTL-SDR Blog fork silently ignores `-E deemp` (breaks FM de-emphasis).
- dsame3 is cloned+patched at build time (EOF handling, stdin default,
  faster_whisper import). Its `--command` placeholders feed notify.py:
  `{ORG} {EEE} {PSSCCC} {TTTT} {JJJHHMM} {LLLLLLLL} {event} {MESSAGE}`.
- numpy/sounddevice/soundfile/tqdm pip packages are required by dsame3
  top-level imports — do not remove.
- Whisper/Wyoming transcription was removed June 2026; `transcript` columns
  remain for legacy rows. Broadcast audio is still recorded
  (`process_alert.py`: EOM wait → sox merge → `/alerts/audio/{id}.wav`).
- `RADIO_SQUELCH` should stay 0 — squelch has caused missed SAME decodes.
- Keep maps offline-capable: `maps.py` must never fetch over the network;
  only `map_cache.py` downloads (throttled, OSM policy).
- Env vars are prefix-grouped; `config.py:_RENAMED_VARS` warns about
  pre-rearchitecture names. `.env.example` is the documented contract;
  `.env` is gitignored and holds real credentials.

## Testing

```bash
docker exec nwsalertdashboard python3 /app/scripts/tests/test_dedup.py        # dedup matrix
docker exec nwsalertdashboard python3 /app/scripts/tests/test_nwws_parse.py   # parser vs real fixtures
# Full radio E2E (synthesized SAME audio through the real pipeline):
docker exec -e NTFY_TOPIC_DEFAULT=nws-test nwsalertdashboard bash /app/scripts/tests/test_inject.sh
# NWWS replay through ingest without XMPP:
docker exec nwsalertdashboard python3 /app/scripts/nwws_client.py --replay /app/scripts/tests/fixtures/TORDTX.txt --log-only
```

Note: in `test_inject.sh` the audio pipes faster than real time, so the
spawned `process_alert.py` misses the EOM marker and waits the full
`RADIO_VOICE_WAIT` before saving audio — expected; real broadcasts arrive in
real time.

## State files (tmpfs)

`/tmp/last_decode` (silence clock) · `/tmp/current_freq` (web display) ·
`/tmp/multimon.log` (EOM detection) · `/tmp/audio_fifo` (live stream) ·
`/tmp/alerts_updated` (SSE signal) · `/tmp/poll_now` (API poll trigger) ·
`/tmp/source_status.json` (per-source health) · `/tmp/derived_env.sh`
(autosetup output)
