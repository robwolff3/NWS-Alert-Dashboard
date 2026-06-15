# NWS Alert Dashboard
# Copyright (C) 2026 Rob Wolff <rob@borked.io>
# Licensed under the GNU General Public License v3.0 or later.

"""Shared alert persistence — SQLite-backed, safe for concurrent writers.

Multi-source schema: each row is one canonical alert that may have been seen
by any combination of sources (radio / nwws / api). Dedup and merge logic
lives in ingest.py; this module owns the schema and low-level queries.
"""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH   = os.environ.get('ALERTS_DB_PATH', '/alerts/alerts.db')
AUDIO_DIR = os.environ.get('ALERTS_AUDIO_DIR', '/alerts/audio')
MAPS_DIR  = os.environ.get('ALERTS_MAPS_DIR', '/alerts/maps')
_SIGNAL   = Path('/tmp/alerts_updated')

# Columns added by the multi-source rearchitecture (June 2026).
_MIGRATION_COLUMNS = [
    ('vtec_key',    'TEXT'),    # 'KDTX.TO.W.0042.2026'
    ('vtec_action', 'TEXT'),    # latest VTEC action seen (NEW/CON/EXT/CAN/...)
    ('api_id',      'TEXT'),    # api.weather.gov properties.id
    ('nwws_id',     'TEXT'),    # NWWS-OI product id
    ('sources',     'TEXT'),    # JSON {"radio": ts, "api": ts, ...}
    ('first_source', 'TEXT'),
    ('fips',        'TEXT'),    # JSON list of 6-digit PSSCCC codes
    ('ugc',         'TEXT'),    # JSON list of UGC codes
    ('headline',    'TEXT'),
    ('description', 'TEXT'),
    ('instruction', 'TEXT'),
    ('raw_product', 'TEXT'),    # NWWS raw text product
    ('geometry',    'TEXT'),    # GeoJSON geometry (storm polygon)
    ('severity',    'TEXT'),
    ('onset',       'REAL'),
    ('notified_at', 'REAL'),    # set once by whichever source notifies first
    ('map_file',    'TEXT'),    # '{id}.png' under /alerts/maps
    ('map_sent',    'INTEGER DEFAULT 0'),
    ('is_test',     'INTEGER DEFAULT 0'),
    ('updated_at',   'REAL'),               # last content revision time
    ('update_count', 'INTEGER DEFAULT 0'),  # number of content revisions seen
    ('renotified_at', 'REAL'),              # last re-notification (throttle clock)
    ('revisions',    'TEXT'),               # JSON array of prior-version snapshots
]


def _signal():
    """Touch a file so the SSE endpoint knows to push a fresh snapshot."""
    _SIGNAL.touch()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def new_alert_id() -> str:
    return f"{int(time.time())}-{uuid.uuid4().hex[:8]}"


_initialized = False


def init_db():
    global _initialized
    if _initialized:
        return
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(AUDIO_DIR).mkdir(parents=True, exist_ok=True)
    Path(MAPS_DIR).mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('''
            CREATE TABLE IF NOT EXISTS alerts (
                id                TEXT PRIMARY KEY,
                eee               TEXT,
                event_name        TEXT NOT NULL,
                alert_time        REAL NOT NULL,
                expires_at        REAL,
                priority          INTEGER NOT NULL,
                topic             TEXT,
                header_message    TEXT,
                transcript        TEXT,
                transcript_status TEXT NOT NULL DEFAULT 'skipped',
                audio_file        TEXT,
                created_at        REAL NOT NULL
            )
        ''')
        for col, decl in _MIGRATION_COLUMNS:
            try:
                c.execute(f'ALTER TABLE alerts ADD COLUMN {col} {decl}')
            except sqlite3.OperationalError:
                pass  # column already exists
        c.execute('CREATE INDEX IF NOT EXISTS idx_alerts_time ON alerts(alert_time)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_alerts_api ON alerts(api_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_alerts_vtec ON alerts(vtec_key)')
        c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_vtec_unique
                     ON alerts(vtec_key) WHERE vtec_key IS NOT NULL''')
        # Backfill pre-rearchitecture rows (radio was the only source then).
        c.execute('''
            UPDATE alerts
               SET first_source = 'radio',
                   sources      = json_object('radio', alert_time),
                   notified_at  = alert_time
             WHERE first_source IS NULL
        ''')
        c.commit()
    _initialized = True


def parse_expires_at(tttt: str, alert_time: float) -> float:
    """Convert SAME purge field (4-digit HHMM) to an expiry timestamp.

    SAME TTTT is HH (00-99) followed by MM (00-59). A garbled radio decode like
    '9999' would otherwise yield a multi-day 'active' alert that pollutes the
    candidate set and dashboard, so out-of-range values fall back to 1 h."""
    try:
        if len(tttt) < 4:
            raise ValueError('short')
        hh, mm = int(tttt[:2]), int(tttt[2:4])
        if not (0 <= hh <= 99 and 0 <= mm < 60) or (hh == 0 and mm == 0):
            raise ValueError('out of range')
        return alert_time + hh * 3600 + mm * 60
    except (ValueError, TypeError):
        return alert_time + 3600  # default 1 h if unparseable/garbled


# ── Row access for ingest (callers pass a live connection so that match +
#    write happen inside one BEGIN IMMEDIATE transaction) ─────────────────────

def transaction():
    """Connection with an immediate write lock; caller commits/closes."""
    init_db()
    conn = _conn()
    conn.execute('BEGIN IMMEDIATE')
    return conn


def find_by_vtec_key(conn, vtec_key: str):
    row = conn.execute('SELECT * FROM alerts WHERE vtec_key = ?', (vtec_key,)).fetchone()
    return dict(row) if row else None


def find_by_native_id(conn, column: str, value: str):
    assert column in ('api_id', 'nwws_id')
    row = conn.execute(f'SELECT * FROM alerts WHERE {column} = ?', (value,)).fetchone()
    return dict(row) if row else None


def find_candidates(conn, now: float, window_secs: int = 1800):
    """Rows that could heuristically match a new arrival: recent or unexpired.

    LIMIT is generous so a large outbreak (many simultaneous active warnings in
    the area) can't evict the true match from the candidate set and cause a
    duplicate row + duplicate notification."""
    rows = conn.execute(
        '''SELECT * FROM alerts
            WHERE alert_time > ? OR (expires_at IS NOT NULL AND expires_at > ?)
            ORDER BY alert_time DESC LIMIT 200''',
        (now - window_secs, now)
    ).fetchall()
    return [dict(r) for r in rows]


def insert_full(conn, row: dict):
    cols = ', '.join(row.keys())
    qs   = ', '.join('?' * len(row))
    conn.execute(f'INSERT INTO alerts ({cols}) VALUES ({qs})', list(row.values()))


def merge_alert(conn, alert_id: str, fields: dict):
    if not fields:
        return
    sets = ', '.join(f'{k} = ?' for k in fields)
    conn.execute(f'UPDATE alerts SET {sets} WHERE id = ?',
                 list(fields.values()) + [alert_id])


def claim_notification(conn, alert_id: str, now: float) -> bool:
    """Atomically claim the right to notify. True for exactly one caller."""
    cur = conn.execute(
        'UPDATE alerts SET notified_at = ? WHERE id = ? AND notified_at IS NULL',
        (now, alert_id))
    return cur.rowcount == 1


# ── Standalone helpers (own their connection) ─────────────────────────────────

def update_alert_audio(alert_id, audio_file):
    with _conn() as c:
        c.execute('UPDATE alerts SET audio_file = ? WHERE id = ?',
                  (audio_file, alert_id))
        c.commit()
    _signal()


def update_alert(alert_id, transcript, transcript_status, audio_file=None):
    """Legacy helper (pre-rearchitecture transcript flow)."""
    with _conn() as c:
        c.execute('''
            UPDATE alerts
               SET transcript=?, transcript_status=?, audio_file=?
             WHERE id=?
        ''', (transcript, transcript_status, audio_file, alert_id))
        c.commit()
    _signal()


def set_map_file(alert_id, map_file, map_sent=None):
    with _conn() as c:
        if map_sent is None:
            c.execute('UPDATE alerts SET map_file = ? WHERE id = ?', (map_file, alert_id))
        else:
            c.execute('UPDATE alerts SET map_file = ?, map_sent = ? WHERE id = ?',
                      (map_file, int(map_sent), alert_id))
        c.commit()
    _signal()


def get_alerts(limit=100):
    init_db()
    with _conn() as c:
        rows = c.execute(
            'SELECT * FROM alerts ORDER BY alert_time DESC LIMIT ?', (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_alert(alert_id):
    init_db()
    with _conn() as c:
        row = c.execute('SELECT * FROM alerts WHERE id=?', (alert_id,)).fetchone()
    return dict(row) if row else None


def cleanup():
    """Purge old audio/map files, expired test alerts, and old DB records."""
    audio_days = int(os.environ.get('AUDIO_RETAIN_DAYS', '365'))
    db_days    = int(os.environ.get('DB_RETAIN_DAYS', '0'))
    now        = time.time()
    init_db()

    if audio_days > 0:
        cutoff = now - audio_days * 86400
        with _conn() as c:
            rows = c.execute(
                'SELECT id, audio_file, map_file FROM alerts '
                'WHERE (audio_file IS NOT NULL OR map_file IS NOT NULL) AND alert_time < ?',
                (cutoff,)
            ).fetchall()
            for row in rows:
                for d, f in ((AUDIO_DIR, row['audio_file']), (MAPS_DIR, row['map_file'])):
                    if f:
                        try:
                            (Path(d) / f).unlink()
                        except OSError:
                            pass
            if rows:
                c.execute(
                    'UPDATE alerts SET audio_file = NULL, map_file = NULL WHERE alert_time < ?',
                    (cutoff,)
                )
                c.commit()
        print(f'cleanup: purged audio/maps for {len(rows)} alert(s) older than {audio_days}d', flush=True)

    # Test alerts live for 24 h
    with _conn() as c:
        test_rows = c.execute(
            'SELECT id, audio_file, map_file FROM alerts WHERE is_test = 1 AND alert_time < ?',
            (now - 86400,)
        ).fetchall()
        for row in test_rows:
            for d, f in ((AUDIO_DIR, row['audio_file']), (MAPS_DIR, row['map_file'])):
                if f:
                    try:
                        (Path(d) / f).unlink()
                    except OSError:
                        pass
        if test_rows:
            c.execute('DELETE FROM alerts WHERE is_test = 1 AND alert_time < ?', (now - 86400,))
            c.commit()
            print(f'cleanup: deleted {len(test_rows)} test alert(s)', flush=True)

    if db_days > 0:
        cutoff = now - db_days * 86400
        with _conn() as c:
            n = c.execute('SELECT COUNT(*) FROM alerts WHERE alert_time < ?', (cutoff,)).fetchone()[0]
            c.execute('DELETE FROM alerts WHERE alert_time < ?', (cutoff,))
            c.commit()
        print(f'cleanup: deleted {n} DB record(s) older than {db_days}d', flush=True)
