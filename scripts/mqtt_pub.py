#!/usr/bin/env python3
"""Optional MQTT publishing for Home Assistant integration.

On each notification or merge-update, publishes:
  {MQTT_TOPIC}/alert    — full alert JSON (event kind 'new' or 'update')
  {MQTT_TOPIC}/active   — retained summary of all unexpired alerts

Short-lived connection per publish (alerts are rare; a persistent session
isn't worth the reconnect bookkeeping). Failures log and never block ingest.

Example HA automation trigger:
  trigger: {platform: mqtt, topic: nws-alerts/alert}
  condition: "{{ trigger.payload_json.priority >= 4 }}"
"""
import json
import sys
import time

sys.path.insert(0, '/app/scripts')
import alerts as alertdb
import config

_PUBLISH_FIELDS = ('id', 'eee', 'event_name', 'alert_time', 'expires_at',
                   'priority', 'headline', 'severity', 'first_source',
                   'sources', 'fips', 'ugc', 'is_test', 'vtec_key')


def _row_payload(row: dict, kind: str) -> dict:
    out = {k: row.get(k) for k in _PUBLISH_FIELDS}
    for k in ('sources', 'fips', 'ugc'):
        if isinstance(out.get(k), str):
            try:
                out[k] = json.loads(out[k])
            except ValueError:
                pass
    out['kind'] = kind
    out['has_geometry'] = bool(row.get('geometry'))
    return out


def _client():
    import paho.mqtt.client as mqtt
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # paho 2.x
    except (AttributeError, TypeError):
        client = mqtt.Client()                                  # paho 1.x
    user = config.env('MQTT_USER')
    if user:
        client.username_pw_set(user, config.env('MQTT_PASS'))
    client.connect(config.env('MQTT_HOST', 'mosquitto'),
                   config.env_int('MQTT_PORT', 1883), keepalive=15)
    return client


def publish(row: dict, kind: str):
    """Publish one alert event + refresh the retained active summary."""
    topic = config.env('MQTT_TOPIC', 'nws-alerts').rstrip('/')
    now = time.time()
    active = [
        _row_payload(r, 'active') for r in alertdb.get_alerts(100)
        if r.get('expires_at') and r['expires_at'] > now and not r.get('is_test')
    ]
    client = _client()
    try:
        client.loop_start()
        client.publish(f'{topic}/alert',
                       json.dumps(_row_payload(row, kind)), qos=1).wait_for_publish(10)
        client.publish(f'{topic}/active',
                       json.dumps({'count': len(active), 'alerts': active,
                                   'updated': now}),
                       qos=1, retain=True).wait_for_publish(10)
        print(f"mqtt: published {kind} for {row.get('id')} "
              f'({len(active)} active)', flush=True)
    finally:
        client.loop_stop()
        client.disconnect()
