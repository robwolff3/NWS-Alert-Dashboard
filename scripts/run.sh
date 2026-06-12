#!/bin/bash
set -euo pipefail

# ── Location auto-setup (derives SAME/zone/frequency vars when LOCATION set) ──
python3 /app/scripts/autosetup.py || true
[ -f /tmp/derived_env.sh ] && . /tmp/derived_env.sh

# ── Cross-source filters (dsame3 pre-filters the radio path) ─────────────────
SAME_ARGS=()
if [ -n "${FILTER_SAME_CODES:-}" ]; then
    SAME_ARGS=(--same ${FILTER_SAME_CODES})
fi

EVENT_ARGS=()
if [ -n "${FILTER_EVENT_CODES:-}" ]; then
    EVENT_ARGS=(--event ${FILTER_EVENT_CODES})
fi

RADIO_ENABLED="${RADIO_ENABLED:-true}"
NWWS_ENABLED="${NWWS_ENABLED:-false}"
API_ENABLED="${API_ENABLED:-true}"

echo "NWS Alert Dashboard starting"
echo "  Radio source : ${RADIO_ENABLED}"
echo "  NWWS-OI      : ${NWWS_ENABLED}"
echo "  API poller   : ${API_ENABLED}"
echo "  SAME codes   : ${FILTER_SAME_CODES:-all}"
echo "  Event codes  : ${FILTER_EVENT_CODES:-all}"

# Warn about pre-rearchitecture env var names still set
python3 -c "import sys; sys.path.insert(0, '/app/scripts'); import config; config.warn_old_vars()"

mkdir -p /recordings /alerts/audio /alerts/maps /alerts/mapdata
mkfifo /tmp/audio_fifo 2>/dev/null || true

# ── Background daemons (each owns its reconnect logic; we just restart on death)
supervise() {  # supervise <name> <cmd...>
    local name=$1; shift
    while true; do
        "$@" 2>&1 | sed -u "s/^/[$name] /" || true
        echo "[$name] exited — restarting in 10s"
        sleep 10
    done
}

supervise web python3 /app/scripts/web.py &

if [ "${API_ENABLED}" = "true" ] && [ -f /app/scripts/api_poller.py ]; then
    supervise api python3 /app/scripts/api_poller.py &
fi

if [ "${NWWS_ENABLED}" = "true" ] && [ -f /app/scripts/nwws_client.py ]; then
    supervise nwws python3 /app/scripts/nwws_client.py &
fi

if [ -f /app/scripts/map_cache.py ]; then
    python3 /app/scripts/map_cache.py &
fi

[ -n "${UPTIME_PUSH_URL:-}" ] && bash /app/scripts/heartbeat.sh &

# ── Radio pipeline ────────────────────────────────────────────────────────────
if [ "${RADIO_ENABLED}" != "true" ]; then
    echo "Radio source disabled — running without SDR"
    wait
    exit 0
fi

echo "  Frequencies  : ${RADIO_FREQUENCY}${RADIO_FREQUENCY_FALLBACK:+ ${RADIO_FREQUENCY_FALLBACK}} MHz"
echo "  PPM offset   : ${RADIO_PPM:-0}"
echo "  Gain         : ${RADIO_GAIN}"
echo "  Bandwidth    : ${RADIO_BANDWIDTH:-48k}"
echo "  Squelch      : ${RADIO_SQUELCH:-0}"

# Build ordered frequency list: primary first, then space-separated fallbacks
FREQUENCIES=("${RADIO_FREQUENCY}")
if [ -n "${RADIO_FREQUENCY_FALLBACK:-}" ]; then
    IFS=' ' read -ra _FALLBACKS <<< "${RADIO_FREQUENCY_FALLBACK}"
    for _f in "${_FALLBACKS[@]}"; do
        [ -n "$_f" ] && FREQUENCIES+=("$_f")
    done
fi

FREQ_IDX=0
SILENCE_TIMEOUT=$(( ${RADIO_SILENCE_TIMEOUT_HOURS:-24} * 3600 ))
FAILBACK_TIMEOUT=$(( ${RADIO_FAILBACK_HOURS:-72} * 3600 ))
DECODE_SENTINEL=/tmp/last_decode

if [ "${#FREQUENCIES[@]}" -gt 1 ]; then
    echo "  Freq switch  : after ${RADIO_SILENCE_TIMEOUT_HOURS:-24}h silence; failback after ${RADIO_FAILBACK_HOURS:-72}h"
fi

SQUELCH_ARGS=()
if [ "${RADIO_SQUELCH:-0}" != "0" ]; then
    SQUELCH_ARGS=(-l "${RADIO_SQUELCH}")
fi

[ -f "$DECODE_SENTINEL" ] || touch "$DECODE_SENTINEL"
FREQ_SWITCHED_AT=$(date +%s)

while true; do
    if [ "${#FREQUENCIES[@]}" -gt 1 ]; then
        NOW=$(date +%s)
        AGE=$(( NOW - $(stat -c %Y "$DECODE_SENTINEL") ))
        TIME_ON_FREQ=$(( NOW - FREQ_SWITCHED_AT ))

        if [ "$FREQ_IDX" -ne 0 ] && [ "$TIME_ON_FREQ" -gt "$FAILBACK_TIMEOUT" ]; then
            echo "On fallback for ${TIME_ON_FREQ}s (limit ${FAILBACK_TIMEOUT}s) — returning to primary ${FREQUENCIES[0]}MHz"
            FREQ_IDX=0
            FREQ_SWITCHED_AT=$NOW
            touch "$DECODE_SENTINEL"
        elif [ "$AGE" -gt "$SILENCE_TIMEOUT" ]; then
            NEXT_IDX=$(( (FREQ_IDX + 1) % ${#FREQUENCIES[@]} ))
            echo "No decode in ${AGE}s (limit ${SILENCE_TIMEOUT}s) — switching from ${FREQUENCIES[$FREQ_IDX]}MHz to ${FREQUENCIES[$NEXT_IDX]}MHz"
            FREQ_IDX=$NEXT_IDX
            FREQ_SWITCHED_AT=$NOW
            touch "$DECODE_SENTINEL"
        fi
    fi

    CURRENT_FREQ="${FREQUENCIES[$FREQ_IDX]}"
    echo "Tuning to ${CURRENT_FREQ}MHz"
    echo "${CURRENT_FREQ}" > /tmp/current_freq

    # recorder.py sits between rtl_fm and multimon-ng:
    # passes audio through to stdout and saves rolling WAV segments to /recordings
    rtl_fm -f "${CURRENT_FREQ}M" -M fm -s "${RADIO_BANDWIDTH:-48k}" -r 22050 -g "${RADIO_GAIN}" -E deemp "${SQUELCH_ARGS[@]}" ${RADIO_PPM:+-p ${RADIO_PPM}} - 2>/dev/null | \
        python3 /app/scripts/recorder.py /recordings | \
        multimon-ng -a EAS -t raw - 2>/dev/null | \
        tee -a /tmp/multimon.log | \
        python /app/dsame3/dsame.py \
            "${SAME_ARGS[@]}" \
            "${EVENT_ARGS[@]}" \
            --skip_dependency \
            --call /app/scripts/notify.py \
            --command "{ORG}" "{EEE}" "{PSSCCC}" "{TTTT}" "{JJJHHMM}" "{LLLLLLLL}" "{event}" "{MESSAGE}" \
        || true

    echo "Pipeline exited — restarting in 5s..."
    sleep 5
done
