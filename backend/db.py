"""SQLite-Schema + Thread-safe Connections fuer die Videothek."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .config import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups(
    chat_id INTEGER PRIMARY KEY,
    title TEXT, username TEXT, invite TEXT, type TEXT, is_forum INTEGER DEFAULT 0,
    chosen INTEGER DEFAULT 0, member_count INTEGER, last_seen REAL, access_hash INTEGER
);
CREATE TABLE IF NOT EXISTS films(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'movie',          -- movie | series
    tmdb_id INTEGER,
    title TEXT NOT NULL,
    original_title TEXT,
    year INTEGER,
    genres TEXT DEFAULT '',
    overview TEXT DEFAULT '',
    poster_path TEXT, backdrop_path TEXT,
    rating REAL,
    created_at REAL,
    UNIQUE(kind, tmdb_id, title, year)
);
CREATE TABLE IF NOT EXISTS media_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, message_id INTEGER, topic_id INTEGER,
    chat_title TEXT, topic_title TEXT,
    date_utc TEXT, sender TEXT,
    caption TEXT DEFAULT '', filename TEXT DEFAULT '',
    mime TEXT, size_bytes INTEGER, duration INTEGER, width INTEGER, height INTEGER,
    link TEXT,
    film_id INTEGER REFERENCES films(id),
    parsed_title TEXT, parsed_year INTEGER, season INTEGER, episode INTEGER,
    media_kind TEXT DEFAULT 'movie',             -- movie | episode | clip | other
    status TEXT DEFAULT 'indexed',               -- indexed | matched | unmatched
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_media_film ON media_items(film_id);
CREATE INDEX IF NOT EXISTS idx_media_chat ON media_items(chat_id);
CREATE INDEX IF NOT EXISTS idx_films_kind ON films(kind);
CREATE INDEX IF NOT EXISTS idx_films_year ON films(year);
CREATE INDEX IF NOT EXISTS idx_films_tmdb ON films(tmdb_id);
CREATE INDEX IF NOT EXISTS idx_films_rating ON films(rating);
CREATE TABLE IF NOT EXISTS downloads(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER REFERENCES media_items(id),
    path TEXT, status TEXT DEFAULT 'queued',    -- queued|running|done|error
    bytes_done INTEGER DEFAULT 0, bytes_total INTEGER DEFAULT 0,
    error TEXT, started_at REAL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS scan_state(
    chat_id INTEGER PRIMARY KEY,
    title TEXT, last_message_id INTEGER DEFAULT 0, last_scan_at REAL,
    status TEXT DEFAULT 'idle',                  -- idle|queued|running|done|error|skipped
    error TEXT, messages_seen INTEGER DEFAULT 0, videos_seen INTEGER DEFAULT 0,
    log TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kiosk_sources(
    chat_id INTEGER PRIMARY KEY,
    topic_id INTEGER,
    chosen INTEGER DEFAULT 1,
    added_at REAL
);
CREATE INDEX IF NOT EXISTS idx_kiosk_sources_chosen ON kiosk_sources(chosen);
CREATE TABLE IF NOT EXISTS sports_sources(
    source_url TEXT PRIMARY KEY,
    username TEXT,
    chat_id INTEGER,
    chosen INTEGER DEFAULT 1,
    added_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sports_sources_chat ON sports_sources(chat_id, chosen);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    cols = [c["name"] for c in conn.execute("PRAGMA table_info(groups)")]
    if "access_hash" not in cols:
        conn.execute("ALTER TABLE groups ADD COLUMN access_hash INTEGER")
    scols = [c["name"] for c in conn.execute("PRAGMA table_info(scan_state)")]
    if "started_at" not in scols:
        conn.execute("ALTER TABLE scan_state ADD COLUMN started_at REAL")
    if "total_messages" not in scols:
        conn.execute("ALTER TABLE scan_state ADD COLUMN total_messages INTEGER DEFAULT 0")
    if "retries" not in scols:
        conn.execute("ALTER TABLE scan_state ADD COLUMN retries INTEGER DEFAULT 0")
    gcols = [c["name"] for c in conn.execute("PRAGMA table_info(groups)")]
    if "top_message" not in gcols:
        conn.execute("ALTER TABLE groups ADD COLUMN top_message INTEGER DEFAULT 0")
    fcols = [c["name"] for c in conn.execute("PRAGMA table_info(films)")]
    for name, ddl in (("coll_id", "INTEGER"), ("coll_name", "TEXT"), ("comps", "TEXT DEFAULT ''")):
        if name not in fcols:
            conn.execute(f"ALTER TABLE films ADD COLUMN {name} {ddl}")
    fcols = [c["name"] for c in conn.execute("PRAGMA table_info(films)")]
    conn.execute("CREATE INDEX IF NOT EXISTS idx_films_coll ON films(coll_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_films_collname ON films(coll_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_films_comps ON films(comps)")
    if "cleaned" not in fcols:
        conn.execute("ALTER TABLE films ADD COLUMN cleaned INTEGER DEFAULT 0")
    if "trailer" not in fcols:
        conn.execute("ALTER TABLE films ADD COLUMN trailer TEXT DEFAULT ''")
    mcols = [c["name"] for c in conn.execute("PRAGMA table_info(media_items)")]
    if "playable" not in mcols:
        conn.execute("ALTER TABLE media_items ADD COLUMN playable TEXT DEFAULT ''")
    for name, ddl in (("quality", "TEXT DEFAULT ''"), ("codec", "TEXT DEFAULT ''"),
                      ("audio_lang", "TEXT DEFAULT ''"), ("hdr", "INTEGER DEFAULT 0"),
                      ("subtitles", "INTEGER DEFAULT 0"), ("release_type", "TEXT DEFAULT ''")):
        if name not in mcols:
            conn.execute(f"ALTER TABLE media_items ADD COLUMN {name} {ddl}")
    for name, ddl in (("match_conf", "REAL"), ("match_reason", "TEXT DEFAULT ''")):
        if name not in mcols:
            conn.execute(f"ALTER TABLE media_items ADD COLUMN {name} {ddl}")
    for name, ddl in (("reachability", "TEXT DEFAULT ''"), ("reachability_checked", "REAL"),
                      ("reachability_error", "TEXT DEFAULT ''")):
        if name not in mcols:
            conn.execute(f"ALTER TABLE media_items ADD COLUMN {name} {ddl}")
    fcols2 = [c["name"] for c in conn.execute("PRAGMA table_info(films)")]
    if "mtry" not in fcols2:      # letzter automatischer TMDB-Zuordnungsversuch
        conn.execute("ALTER TABLE films ADD COLUMN mtry REAL")
    _fcols3 = [c["name"] for c in conn.execute("PRAGMA table_info(films)")]
    if "fsk" not in _fcols3:          # deutsche Altersfreigabe (FSK)
        conn.execute("ALTER TABLE films ADD COLUMN fsk TEXT DEFAULT ''")
    _fcols4 = [c["name"] for c in conn.execute("PRAGMA table_info(films)")]
    if "quality" not in _fcols4:      # Qualitaets-Marker (uncut/unrated/4k/...)
        conn.execute("ALTER TABLE films ADD COLUMN quality TEXT DEFAULT ''")
    _fcols5 = [c["name"] for c in conn.execute("PRAGMA table_info(films)")]
    if "fsk_checked" not in _fcols5:  # FSK-Pruefstatus (lueckenloser Nachweis)
        conn.execute("ALTER TABLE films ADD COLUMN fsk_checked REAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS progress(
    media_id INTEGER PRIMARY KEY REFERENCES media_items(id),
    film_id INTEGER, position REAL, duration REAL, updated_at REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_progress_film ON progress(film_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS mylist(
        film_id INTEGER PRIMARY KEY REFERENCES films(id), added_at REAL)""")
    conn.execute("INSERT OR IGNORE INTO kiosk_sources(chat_id, chosen, added_at) VALUES(?,?,?)", (-1002142840116, 1, time.time()))
    conn.execute("INSERT OR IGNORE INTO kiosk_sources(chat_id, chosen, added_at) VALUES(?,?,?)", (-1001861728301, 1, time.time()))
    conn.execute("INSERT OR IGNORE INTO kiosk_sources(chat_id, chosen, added_at) VALUES(?,?,?)", (-1002101634592, 1, time.time()))
    conn.execute("INSERT OR IGNORE INTO kiosk_sources(chat_id, chosen, added_at) VALUES(?,?,?)", (-1002052651753, 1, time.time()))
    for source_url, username in (
        ("https://t.me/+cwHVE_xyOmNmYTYy", None),
        ("https://t.me/wweoldvdo", "wweoldvdo"),
        ("https://t.me/WWE_AEW_DL27", "WWE_AEW_DL27"),
        ("https://t.me/wwebackup1", "wwebackup1"),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO sports_sources(source_url,username,chosen,added_at) VALUES(?,?,1,?)",
            (source_url, username, time.time()))
        if username:
            conn.execute(
                "UPDATE sports_sources SET chat_id=(SELECT chat_id FROM groups WHERE lower(username)=lower(?) AND last_seen>0) "
                "WHERE source_url=? AND chat_id IS NULL", (username, source_url))
    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('created',?)", (str(time.time()),))
    conn.commit()


def q(sql: str, params=()) -> list[dict]:
    cur = get_db().execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def one(sql: str, params=()):
    cur = get_db().execute(sql, params)
    r = cur.fetchone()
    return dict(r) if r else None


def w(sql: str, params=()) -> int:
    conn = get_db()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid
