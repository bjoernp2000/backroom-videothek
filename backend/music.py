"""Quellen fuer Backroom Music.

Telegram wird nur aus gewählten Gruppen gelesen. Ohne Auswahl bleibt YouTube
als sichere Standardquelle aktiv.
"""
from __future__ import annotations

import re
from . import db

YOUTUBE_SOURCES = [
    {"id": "yt-music", "title": "YouTube Music", "kind": "youtube", "url": "https://music.youtube.com/", "note": "Musik und Playlists"},
    {"id": "yt-live", "title": "Live Sessions", "kind": "youtube", "url": "https://www.youtube.com/results?search_query=official+live+music+sessions", "note": "Offizielle Live-Auftritte"},
    {"id": "yt-classical", "title": "Classical", "kind": "youtube", "url": "https://www.youtube.com/results?search_query=official+classical+music", "note": "Klassik und Konzerte"},
    {"id": "yt-electronic", "title": "Electronic", "kind": "youtube", "url": "https://www.youtube.com/results?search_query=official+electronic+music+mix", "note": "Elektronische Musik"},
    {"id": "yt-hiphop", "title": "Hip-Hop", "kind": "youtube", "url": "https://www.youtube.com/results?search_query=official+hip+hop+music", "note": "Hip-Hop und Rap"},
    {"id": "yt-rock", "title": "Rock", "kind": "youtube", "url": "https://www.youtube.com/results?search_query=official+rock+music", "note": "Rock und Alternative"},
]

YOUTUBE_AUDIO_SOURCES = [
    {"id": "yt-audio-books", "title": "YouTube Hörspiele", "kind": "youtube", "url": "https://www.youtube.com/results?search_query=hörspiele+deutsch+playlist", "note": "Hörspiele und Hörspielserien"},
    {"id": "yt-audio-kids", "title": "Kinderhörspiele", "kind": "youtube", "url": "https://www.youtube.com/results?search_query=kinderhörspiele+playlist", "note": "Hörspiele für Kinder"},
]
_MUSIC_RE = re.compile(r"\b(musik|music|song|album|single|konzert|concert|live|audio|radio|mix|playlist|soundtrack|sänger|band)\b", re.I)


def sources() -> list[dict]:
    return db.q("""SELECT ms.source_url, ms.chat_id, ms.topic_id, ms.chosen,
                         COALESCE(g.title, 'Noch nicht aufgelöst') AS title
                    FROM music_sources ms LEFT JOIN groups g ON g.chat_id=ms.chat_id
                   ORDER BY ms.added_at""")


def add_source(source_url: str, chat_id: int | None = None, topic_id: int | None = None) -> dict:
    source_url = (source_url or "").strip()
    if not source_url.startswith("https://t.me/"):
        raise ValueError("Es sind nur Telegram-Links erlaubt.")
    db.w("INSERT OR REPLACE INTO music_sources(source_url,username,chat_id,topic_id,chosen,added_at) VALUES(?,?,?, ?,1,COALESCE((SELECT added_at FROM music_sources WHERE source_url=?),?))",
          (source_url, None, chat_id, topic_id, source_url, __import__('time').time()))
    return db.one("SELECT * FROM music_sources WHERE source_url=?", (source_url,))


def catalog(source: str = "both", limit: int = 48, area: str = "music") -> dict:
    source = source if source in {"telegram", "youtube", "both"} else "both"
    audio_area = area == "hörspiele"
    selected_sources = sources()
    selected_ids = [int(row["chat_id"]) for row in selected_sources if row.get("chosen") and row.get("chat_id") is not None]
    telegram_allowed = bool(selected_ids) and source in {"telegram", "both"}
    youtube_allowed = source in {"youtube", "both"}
    items = []
    if telegram_allowed:
        placeholders = ",".join("?" * len(selected_ids))
        rows = db.q(
            f"""SELECT m.id, m.chat_title, m.caption, m.filename, m.date_utc, m.link
                  FROM media_items m
                 WHERE m.media_kind='audio' AND m.chat_id IN ({placeholders})
                 ORDER BY COALESCE(m.date_utc,'') DESC, m.id DESC LIMIT ?""",
            [*selected_ids, max(1, min(int(limit) * 3, 200))])
        for row in rows:
            title = (row.get("caption") or row.get("filename") or "Hörspiel").strip()
            items.append({"id": "tg:" + str(row["id"]), "title": title.split("\n", 1)[0][:150],
                          "kind": "telegram", "source": row.get("chat_title") or "Telegram-Hörspielquelle",
                          "url": row.get("link") or "", "date": row.get("date_utc") or ""})
            if len(items) >= limit:
                break
    if youtube_allowed:
        youtube_sources = YOUTUBE_AUDIO_SOURCES if audio_area else YOUTUBE_SOURCES
        items.extend({**x, "source": "YouTube"} for x in youtube_sources)
    youtube_sources = YOUTUBE_AUDIO_SOURCES if audio_area else YOUTUBE_SOURCES
    return {"ok": True, "area": "hörspiele" if audio_area else "music", "source": source,
            "sources": selected_sources, "has_telegram": bool(selected_ids),
            "items": items[:max(1, min(int(limit), 100))],
            "youtube": youtube_sources if youtube_allowed else []}
