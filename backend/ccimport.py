"""Import aus vorhandenen GH-Infinity-Daten (CC): cineforge content_index + group_content.sqlite3.

Beide Quellen werden NUR LESEN geoeffnet; die Videothek-DB bekommt fertige media_items
(status='indexed'), die der TMDB-Matcher automatisch abarbeitet. Kein Telegram-Scan noetig.
"""
from __future__ import annotations

import sqlite3
import time

from . import db, parsers

CINEFORGE_DB = "F:/TG_BOT_PROJECTS/_app_betrieb/instances/cineforge/data/bot.sqlite3"
GROUP_CONTENT_DB = "F:/TG_BOT_PROJECTS/GH_INFINITY_PERSISTENT_DATA/group_content.sqlite3"

UPSERT_MEDIA = """
INSERT INTO media_items(chat_id,message_id,topic_id,chat_title,topic_title,date_utc,sender,
       caption,filename,mime,size_bytes,duration,width,height,link,film_id,
       parsed_title,parsed_year,season,episode,media_kind,status)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(chat_id,message_id) DO UPDATE SET
    caption=CASE WHEN excluded.caption!='' THEN excluded.caption ELSE media_items.caption END,
    topic_title=CASE WHEN excluded.topic_title!='' THEN excluded.topic_title ELSE media_items.topic_title END,
    parsed_title=CASE WHEN media_items.parsed_title='' OR media_items.parsed_title IS NULL
                      THEN excluded.parsed_title ELSE media_items.parsed_title END,
    parsed_year=COALESCE(media_items.parsed_year, excluded.parsed_year),
    link=CASE WHEN media_items.link='' THEN excluded.link ELSE media_items.link END
"""


def _conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _link(chat_id: int, message_id: int, topic_id=None) -> str:
    internal = str(chat_id)[4:] if str(chat_id).startswith("-100") else str(chat_id)
    url = f"https://t.me/c/{internal}/{message_id}"
    if topic_id and int(topic_id) != int(message_id):
        url += f"?thread={topic_id}"
    return url


def import_cineforge() -> int:
    """content_index: saubere Titel/Jahre + Ziel-Message — direkt als Quelle."""
    try:
        src = _conn(CINEFORGE_DB)
    except sqlite3.Error:
        return 0
    n = 0
    gtitles = {r["chat_id"]: r["title"] for r in db.q("SELECT chat_id,title FROM groups")}
    try:
        rows = src.execute(
            """SELECT title, year, target_chat_id, target_thread_id, message_id,
                      caption, filename, duration, width, height
               FROM content_index
               WHERE status='published' AND target_chat_id IS NOT NULL AND message_id IS NOT NULL"""
        ).fetchall()
    except sqlite3.Error:
        return 0
    for r in rows:
        p = parsers.parse_caption(r["caption"], r["filename"])
        title = (r["title"] or p["title"]).strip()
        # CineForge-Titel enthalten oft Genre-Anhaenge " - Action/Thriller -"
        title = title.rsplit(" - ", 1)[0].strip(" -") if " - " in title and "/" in title.rsplit(" - ", 1)[-1] else title
        db_w = db.get_db()
        db_w.execute(UPSERT_MEDIA, (
            int(r["target_chat_id"]), int(r["message_id"]), r["target_thread_id"],
            gtitles.get(int(r["target_chat_id"]), "TV-Gold"), "", "", "cineforge-bot",
            (r["caption"] or "")[:2000], r["filename"] or "", "video/mp4", 0,
            _int(r["duration"]), _int(r["width"]), _int(r["height"]),
            _link(int(r["target_chat_id"]), int(r["message_id"]), r["target_thread_id"]),
            None, title, _int(r["year"]) or None,
            p["season"], p["episode"], p["media_kind"], "indexed"))
        n += 1
    db.get_db().commit()
    return n


def import_group_content() -> int:
    """content_messages: alle Video-Dokumente der TV-Gold-Gruppen (Dateiname, Caption, Topic)."""
    try:
        src = _conn(GROUP_CONTENT_DB)
    except sqlite3.Error:
        return 0
    try:
        chats = {int(r["chat_id"]): (r["title"] or "") for r in src.execute("SELECT chat_id,title FROM content_groups")}
        topics = {}
        for r in src.execute("SELECT chat_id,topic_id,title FROM content_topics"):
            topics[(int(r["chat_id"]), int(r["topic_id"]))] = r["title"] or ""
        rows = src.execute(
            """SELECT chat_id, message_id, topic_id, date_utc, sender_name, text,
                      media_file_name, media_file_size, media_mime_type
               FROM content_messages
               WHERE has_media=1 AND media_type='MessageMediaDocument'
                 AND (lower(media_file_name) LIKE '%.mkv' OR lower(media_file_name) LIKE '%.mp4'
                   OR lower(media_file_name) LIKE '%.avi' OR lower(media_file_name) LIKE '%.webm'
                   OR lower(media_file_name) LIKE '%.mov' OR lower(media_file_name) LIKE '%.m4v'
                   OR lower(media_mime_type) LIKE 'video/%')"""
        ).fetchall()
    except sqlite3.Error:
        return 0
    n = 0
    for r in rows:
        chat_id = int(r["chat_id"])
        caption = (r["text"] or "")[:2000]
        fname = r["media_file_name"] or ""
        p = parsers.parse_caption(caption, fname)
        db_w = db.get_db()
        db_w.execute(UPSERT_MEDIA, (
            chat_id, int(r["message_id"]), r["topic_id"],
            chats.get(chat_id, "TV-Gold"), topics.get((chat_id, int(r["topic_id"] or 0)), ""),
            r["date_utc"] or "", r["sender_name"] or "",
            caption, fname, r["media_mime_type"] or "", int(r["media_file_size"] or 0), 0, 0, 0,
            _link(chat_id, int(r["message_id"]), r["topic_id"]),
            None, p["title"], p["year"], p["season"], p["episode"], p["media_kind"], "indexed"))
        n += 1
    db.get_db().commit()
    return n


def run_import() -> dict:
    db.init_db()
    t0 = time.time()
    try:
        cf = import_cineforge()
        gc = import_group_content()
    except Exception:
        try:
            db.get_db().rollback()  # nie halbe Transaktionen mit Lock stehen lassen
        except Exception:
            pass
        raise
    return {"cineforge": cf, "group_content": gc, "seconds": round(time.time() - t0, 1)}
