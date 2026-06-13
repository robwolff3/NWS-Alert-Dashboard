#!/bin/bash
# Radio-path end-to-end test: synthesizes a SAME weekly-test alert (header,
# attention tone, silent voice window, EOM) and pipes it through the real
# pipeline (recorder → multimon-ng → dsame3 → notify.py).
# Run INSIDE the container:
#   docker exec -e NTFY_TOPIC_DEFAULT=nws-test nwsalertdashboard bash /app/scripts/tests/test_inject.sh
#
# Expect: one new alert row, one notification, and (after the EOM wait,
# ~RADIO_EOM_TRAIL_SECS) an audio file under /alerts/audio/.
set -euo pipefail

echo "Injecting synthetic RWT through the decode pipeline..."

python3 /app/scripts/tests/gen_same.py | \
    python3 /app/scripts/recorder.py /recordings | \
    multimon-ng -a EAS -t raw - 2>/dev/null | \
    tee -a /tmp/multimon.log | \
    python /app/dsame3/dsame.py \
        ${FILTER_SAME_CODES:+--same ${FILTER_SAME_CODES}} \
        ${FILTER_EVENT_CODES:+--event ${FILTER_EVENT_CODES}} \
        --skip_dependency \
        --call /app/scripts/notify.py \
        --command "{ORG}" "{EEE}" "{PSSCCC}" "{TTTT}" "{JJJHHMM}" "{LLLLLLLL}" "{event}" "{MESSAGE}" \
    > /dev/null

echo "Pipeline finished. Latest alert rows:"
python3 - <<'EOF'
import sys
sys.path.insert(0, '/app/scripts')
import alerts
for row in alerts.get_alerts(3):
    print(f"  {row['id']}  {row['eee']}  {row['event_name']}  "
          f"src={row.get('first_source')}  notified={bool(row.get('notified_at'))}  "
          f"audio={row.get('audio_file')}")
EOF
