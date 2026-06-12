#!/usr/bin/env python3
"""Notification delivery via Apprise.

Two configuration paths, usable together:
  - NOTIFY_URLS: space-separated Apprise URLs (Discord, Telegram, Pushover,
    email, ... see https://github.com/caronc/apprise). Sent as-is; services
    without a priority concept just get title+body(+attachment).
  - NTFY_URL/NTFY_USER/NTFY_PASS shortcut: builds an ntfy Apprise URL per
    notification so the per-alert priority (1-5) and per-priority topic
    routing keep working exactly as before.

If neither is configured, notifications are logged and dropped.
"""
import sys
from urllib.parse import quote, urlparse

sys.path.insert(0, '/app/scripts')
from config import env

# ntfy priority ints → Apprise ntfy priority names
_NTFY_PRIORITY = {1: 'min', 2: 'low', 3: 'default', 4: 'high', 5: 'max'}


def _ntfy_shortcut_url(topic: str, priority: int):
    """Build an Apprise ntfy URL from the NTFY_* convenience vars."""
    base = env('NTFY_URL').strip()
    if not base:
        return None
    parsed = urlparse(base if '//' in base else f'https://{base}')
    scheme = 'ntfy' if parsed.scheme == 'http' else 'ntfys'
    auth = ''
    if env('NTFY_USER'):
        auth = f"{quote(env('NTFY_USER'), safe='')}:{quote(env('NTFY_PASS'), safe='')}@"
    host = parsed.netloc + (parsed.path.rstrip('/') if parsed.path != '/' else '')
    pri  = _NTFY_PRIORITY.get(priority, 'default')
    return f'{scheme}://{auth}{host}/{topic}?priority={pri}&tags=warning,loudspeaker'


def send(title: str, body: str, priority: int = 3, topic: str = 'nws',
         attach_path: str = None) -> bool:
    """Send to all configured targets. Returns True if at least one succeeded."""
    urls = env('NOTIFY_URLS').split()
    ntfy_url = _ntfy_shortcut_url(topic, priority)
    if ntfy_url:
        urls.append(ntfy_url)

    if not urls:
        print(f"notifier: no targets configured — dropping '{title}'", flush=True)
        return False

    try:
        import apprise
    except ImportError:
        print('notifier: apprise not installed — cannot send', flush=True)
        return False

    ap = apprise.Apprise()
    for u in urls:
        ap.add(u)

    ok = ap.notify(
        title=title,
        body=body or title,
        attach=attach_path if attach_path else None,
    )
    if not ok and attach_path:
        # e.g. ntfy server without an attachment cache — deliver text at least
        ok = ap.notify(title=title, body=body or title)
        print(f"notifier: attachment delivery failed — sent text-only "
              f"[{'ok' if ok else 'FAILED'}]", flush=True)
    n = len(urls)
    print(f"notifier: '{title}' (prio {priority}) → {n} target(s) "
          f"[{'ok' if ok else 'FAILED'}]"
          + (' +attachment' if attach_path else ''), flush=True)
    return bool(ok)
