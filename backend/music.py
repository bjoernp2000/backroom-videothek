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
_MUSIC_RE = re.compile(r"\b(musik|music|song|album|single|konzert|concert|live|audio|radio|mix|playlist|soundtrack|sänger|band)\b", re.I)


def catalog(source: str = "both", limit: int = 48) -> dict:
    source = source if source in {"telegram", "youtube", "both"} else "both"
    groups = db.q("SELECT chat_id, title, chosen FROM groups ORDER BY title COLLATE NOCASE")
    chosen = [g for g in groups if int(g.get("chosen") or 0) == 1]
    # Ohne Quellenauswahl niemals Telegram-Inhalte vortäuschen: YouTube bleibt Standard.
    telegram_allowed = bool(chosen) and source in {"telegram", "both"}
    youtube_allowed = source in {"youtube", "both"} or not chosen
    items = []
    if telegram_allowed:
        rows = db.q(
            """SELECT m.id, m.chat_title, m.caption, m.filename, m.date_utc, m.link
                 FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
                WHERE g.chosen=1
                ORDER BY COALESCE(m.date_utc,'') DESC, m.id DESC LIMIT ?""", (max(1, min(int(limit) * 3, 200)),))
        for row in rows:
            title = (row.get("caption") or row.get("filename") or "").strip()
            if not title or not _MUSIC_RE.search(title):
                continue
            items.append({"id": "tg:" + str(row["id"]), "title": title.split("\n", 1)[0][:150],
                          "kind": "telegram", "source": row.get("chat_title") or "Telegram-Gruppe",
                          "url": row.get("link") or "", "date": row.get("date_utc") or ""})
            if len(items) >= limit:
                break
    if youtube_allowed:
        items.extend({**x, "source": "YouTube"} for x in YOUTUBE_SOURCES)
    return {"ok": True, "source": source, "chosen": chosen,
            "has_telegram": bool(chosen), "items": items[:max(1, min(int(limit), 100))],
            "youtube": YOUTUBE_SOURCES if youtube_allowed else []}
