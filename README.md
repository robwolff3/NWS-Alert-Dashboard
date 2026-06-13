# NWS Alert Dashboard

A self-hosted National Weather Service alert monitor with **three independent
alert sources**, cross-source deduplication, polygon maps, and flexible
notifications. One Docker container.

| Source | Transport | Latency | Works offline? | Needs |
|---|---|---|---|---|
| **NWWS-OI** | XMPP push from the NWS Weather Wire Service | seconds | no | [free NWS credentials](https://www.weather.gov/nwws/) |
| **NOAA Weather Radio** | RTL-SDR receiving NWR broadcasts (SAME/EAS decode) | seconds | **yes** | ~$30 RTL-SDR dongle |
| **NOAA REST API** | api.weather.gov polling | ≤ poll interval | no | nothing |

The same warning typically arrives via all three. The **ingest core** dedups
them (VTEC event tracking + SAME county/time heuristics), notifies **exactly
once** via whichever source delivered first, and merges the richer details
(headline, full text, storm polygon) into one alert record as they arrive.

**If your internet goes down, the radio path keeps working**: alerts are
decoded off the air, the broadcast audio is recorded, and the map falls back
to locally cached county boundaries. No cloud dependency in the warning path.

## Features

- **Web dashboard** (PWA): live-updating (SSE) active/historical alerts with
  source badges (RADIO / NWWS / API), full alert text, broadcast recordings,
  live radio stream, and interactive Leaflet maps served from a **local tile
  cache** — plus light/dark themes and per-source health chips (tuned
  frequency, NWWS, API) that show at a glance when a source is down
- **Broad event coverage**: all SAME/EAS codes plus common non-EAS advisories
  (excessive heat, extreme cold, red flag, dense fog, wind, …) that only
  arrive via the API/NWWS sources — tune the set in `FILTER_EVENT_CODES`
- **Alert maps**: per-alert PNG rendered offline (Pillow over cached OSM
  tiles) and attached to notifications; storm polygon when available, county
  boundaries otherwise
- **Notifications** via [Apprise](https://github.com/caronc/apprise) — ntfy
  (with per-event priority/topic routing), Discord, Telegram, Pushover,
  email, ~80 services — plus browser web-push with per-device event filters
- **MQTT publishing** for Home Assistant automations (optional)
- **Location auto-setup**: set `LOCATION=lat,lon` and the county SAME code,
  UGC zones, and a radio frequency scan list are derived automatically
- **Test alert button** to verify the whole chain without waiting for weather
- Frequency rotation with silence detection and primary failback, rolling
  audio recorder, Uptime Kuma heartbeat, retention policies

## Quick start (no SDR needed)

```bash
git clone https://github.com/YOURUSER/nws-alert-dashboard.git
cd nws-alert-dashboard
cp .env.example .env
# edit .env: set LOCATION=lat,lon, API_USER_AGENT contact, and a
# notification target (NTFY_* or NOTIFY_URLS); set RADIO_ENABLED=false
docker compose up -d --build
```

Open `http://host:8082`. To verify notifications and map rendering
end-to-end, uncheck **Hide test alerts** in the Alert History header to
reveal the **Send test alert** button, then click it.

## Adding the radio source

1. Plug in an RTL-SDR dongle (RTL2832U). Find it: `lsusb | grep -i RTL`
2. `cp compose.override.example.yaml compose.override.yaml` and set the
   device path; set `RTL_DEVICE` in `.env`
3. Set `RADIO_ENABLED=true`. Leave `RADIO_FREQUENCY` blank to scan all seven
   NWR channels until one decodes, or set your transmitter's frequency
   ([transmitter search](https://www.weather.gov/nwr/station_search))
4. `docker compose up -d --build --force-recreate`

Reception notes: keep `RADIO_SQUELCH=0` (squelch can swallow SAME bursts),
use `rtl_test -p` for the PPM correction, and expect the weekly RWT test
(Wednesdays) as your end-to-end confirmation.

The image pins `rtl-sdr 0.6.0-3` deliberately — the newer RTL-SDR Blog fork
silently ignores `-E deemp`, which breaks FM de-emphasis on NWR.

## Adding NWWS-OI

1. [Apply for credentials](https://www.weather.gov/nwws/) (free; can take
   30+ days)
2. Set `NWWS_ENABLED=true`, `NWWS_USER`, `NWWS_PASS`
3. Recommended: run the first day with `NWWS_LOG_ONLY=true` and watch
   `docker compose logs -f` to confirm product filtering before letting it
   notify

The client connects to either NWWS server (College Park / Boulder),
alternates hosts on reconnect, and keeps the session alive with XMPP pings.
While NWWS is connected the REST poller relaxes to `API_POLL_SECS`; when it
drops, polling tightens to `API_POLL_SECS_DEGRADED` automatically.

## Home Assistant (MQTT)

Set `MQTT_ENABLED=true` and broker details. Each notified or updated alert
publishes JSON to `nws-alerts/alert`, and a **retained** summary of active
alerts to `nws-alerts/active`.

```yaml
automation:
  - trigger:
      platform: mqtt
      topic: nws-alerts/alert
    condition: "{{ trigger.payload_json.priority >= 4 and not trigger.payload_json.is_test }}"
    action:
      service: notify.everyone
      data:
        title: "{{ trigger.payload_json.event_name }}"
        message: "{{ trigger.payload_json.headline }}"
```

## Architecture

```
RADIO: rtl_fm → recorder.py → multimon-ng → dsame3 ──→ notify.py ─┐
NWWS:  nwws_client.py (slixmpp MUC) → nwws_parse.py ──────────────┼→ ingest.py → SQLite
API:   api_poller.py (alerts/active) ─────────────────────────────┘      │
                            notify-once → Apprise (+map PNG) + web push + MQTT
web.py (Flask: SSE dashboard, Leaflet, /tiles) ←──────────────── SQLite
map_cache.py (one-time: zone GeoJSON + OSM tiles → ./alerts/mapdata)
```

Dedup: alerts carrying VTEC (NWWS, API) match on
`office.phen.sig.etn.year`; radio SAME decodes match heuristically on event
equivalence (SAME ↔ VTEC code mapping) + county FIPS overlap + a
`DEDUP_WINDOW_SECS` time window. The first source to land an alert claims
the notification atomically; later arrivals only enrich. A radio-first alert
triggers an immediate API poll, so headline/polygon usually merge in within
seconds — and a one-time map follow-up notification fires once the polygon
arrives.

All configuration is documented in [`.env.example`](.env.example).

## Testing

```bash
# Dedup matrix + parser tests (in the running container)
docker exec weatherradio python3 /app/scripts/tests/test_dedup.py
docker exec weatherradio python3 /app/scripts/tests/test_nwws_parse.py

# Full radio-path E2E: synthesizes a SAME weekly test and pipes it through
# recorder → multimon-ng → dsame3 → notify (use a test ntfy topic!)
docker exec -e NTFY_TOPIC_DEFAULT=nws-test weatherradio \
  bash /app/scripts/tests/test_inject.sh

# Replay archived NWS text products through the NWWS ingest path
docker exec weatherradio python3 /app/scripts/nwws_client.py \
  --replay /app/scripts/tests/fixtures/TORDTX.txt --log-only
```

## Notes

- **ntfy map attachments** require `attachment-cache-dir` in your ntfy
  server config; without it the dashboard automatically falls back to
  text-only notifications.
- The OSM tile cache is a small one-time regional fetch (~10–15 MB,
  throttled) consistent with the
  [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/);
  point `MAP_TILE_URL` at another provider or a self-hosted tile server if
  you need more.
- api.weather.gov requires a contact address in `API_USER_AGENT`.

## License

[GNU General Public License v3.0](LICENSE)
