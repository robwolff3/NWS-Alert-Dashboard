#!/bin/bash
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
# Pings the Uptime Kuma push URL every 90s when the configured sources look
# healthy. Radio mode: fresh WAV segments prove the SDR pipeline is alive.
# Radio-less mode: source_status.json freshness proves a poller/push source
# is alive.

touch /tmp/hb_ref

while true; do
    sleep 90

    [ -z "${UPTIME_PUSH_URL:-}" ] && continue

    STATUS="down"
    MSG="no signal"

    if [ "${RADIO_ENABLED:-true}" = "true" ]; then
        if find /recordings -name "*.wav" -newer /tmp/hb_ref | grep -q .; then
            STATUS="up"
            MSG="recording active"
        else
            MSG="no new recordings"
        fi
    else
        # Healthy if any source updated its status in the last 10 minutes
        if [ -f /tmp/source_status.json ] && \
           [ -n "$(find /tmp/source_status.json -mmin -10)" ]; then
            STATUS="up"
            MSG="sources active"
        else
            MSG="sources stale"
        fi
    fi
    touch /tmp/hb_ref

    # -G + --data-urlencode so values are properly encoded and appended whether
    # or not UPTIME_PUSH_URL already carries a query string.
    curl -sf -G "${UPTIME_PUSH_URL}" \
        --data-urlencode "status=${STATUS}" \
        --data-urlencode "msg=${MSG}" \
        --data-urlencode "ping=" >/dev/null 2>&1 || true
done
