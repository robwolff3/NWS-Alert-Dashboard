#!/usr/bin/env python3
#
# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.
#
"""Web Push subscription management and delivery."""
import base64
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

DB_PATH  = '/alerts/alerts.db'
KEY_DIR  = Path('/alerts')
PRIV_KEY = KEY_DIR / 'vapid_private.pem'
PUB_KEY  = KEY_DIR / 'vapid_public.txt'


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_push():
    """Create push_subscriptions table and generate VAPID keys if needed."""
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint     TEXT PRIMARY KEY,
                p256dh       TEXT NOT NULL,
                auth         TEXT NOT NULL,
                min_priority INTEGER NOT NULL DEFAULT 3,
                event_codes  TEXT,
                created_at   REAL NOT NULL
            )
        ''')
        try:
            c.execute('ALTER TABLE push_subscriptions ADD COLUMN event_codes TEXT')
        except Exception:
            pass  # column already exists
        c.commit()
    _ensure_keys()


def _ensure_keys():
    if PRIV_KEY.exists() and PUB_KEY.exists():
        return
    from py_vapid import Vapid
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    v = Vapid()
    v.generate_keys()
    v.save_key(str(PRIV_KEY))
    pub = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    PUB_KEY.write_text(base64.urlsafe_b64encode(pub).rstrip(b'=').decode())
    print('push: VAPID keys generated', flush=True)


def get_public_key() -> str:
    _ensure_keys()
    return PUB_KEY.read_text().strip()


def save_subscription(endpoint: str, p256dh: str, auth: str,
                      min_priority: int = 3, event_codes: Optional[list] = None):
    codes_json = json.dumps(event_codes) if event_codes is not None else None
    with _conn() as c:
        c.execute('''
            INSERT OR REPLACE INTO push_subscriptions
              (endpoint, p256dh, auth, min_priority, event_codes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (endpoint, p256dh, auth, int(min_priority), codes_json, time.time()))
        c.commit()


def get_subscription_prefs(endpoint: str) -> Optional[dict]:
    with _conn() as c:
        row = c.execute(
            'SELECT min_priority, event_codes FROM push_subscriptions WHERE endpoint = ?',
            (endpoint,)
        ).fetchone()
    if not row:
        return None
    return {
        'minPriority': row['min_priority'],
        'eventCodes': json.loads(row['event_codes']) if row['event_codes'] else None,
    }


def delete_subscription(endpoint: str):
    with _conn() as c:
        c.execute('DELETE FROM push_subscriptions WHERE endpoint = ?', (endpoint,))
        c.commit()


def send_push(title: str, body: str, priority: int, eee: str = ''):
    """Send to subscriptions that match by priority preset or custom event-code list."""
    try:
        with _conn() as c:
            all_subs = [dict(r) for r in c.execute('SELECT * FROM push_subscriptions').fetchall()]
    except Exception as e:
        print(f'push: db error: {e}', flush=True)
        return

    subs = []
    for sub in all_subs:
        if sub['event_codes']:
            if eee and eee in json.loads(sub['event_codes']):
                subs.append(sub)
        elif sub['min_priority'] <= priority:
            subs.append(sub)

    if not subs:
        return

    _ensure_keys()
    email = os.environ.get('PUSH_VAPID_EMAIL', 'admin@localhost')
    if not email.startswith('mailto:'):
        email = 'mailto:' + email
    payload = json.dumps({'title': title, 'body': body, 'priority': priority})

    from pywebpush import webpush, WebPushException
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub['endpoint'],
                    'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']},
                },
                data=payload,
                vapid_private_key=str(PRIV_KEY),
                vapid_claims={'sub': email},
                ttl=3600,
            )
            print(f"push: sent → {sub['endpoint'][:60]}…", flush=True)
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            print(f'push: WebPushException ({status}): {e}', flush=True)
            if status == 410:  # subscription expired/gone
                delete_subscription(sub['endpoint'])
        except Exception as e:
            print(f'push: error: {e}', flush=True)
