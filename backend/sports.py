"""Lokale Sportquellen fuer Backroom Sports.

Die Wrestling-Playlists stammen aus dem Vorgaengerprojekt und werden nur als
verlinkte Quellen katalogisiert. Es werden keine Videos heruntergeladen.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from . import config, db

_WATCH_RE = re.compile(r"https?://(?:www\.)?youtube\.com/watch\?v=([\w-]+)")


def _playlist_files() -> list[Path]:
    root = config.DATA_DIR / "sports_sources"
    return sorted(root.glob("Wrestling *.m3u"))


def _playlist_name(path: Path) -> str:
    return path.stem.replace("Wrestling • International • ", "").strip()


def _youtube_items() -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for path in _playlist_files():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        pending: dict | None = None
        for line in lines:
            if line.startswith("#EXTINF:"):
                title = line.split(",", 1)[1].strip() if "," in line else "Wrestling-Video"
                group = re.search(r'group-title="([^"]+)"', line)
                logo = re.search(r'tvg-logo="([^"]+)"', line)
                pending = {"title": html.unescape(title), "league": _playlist_name(path),
                           "group": group.group(1) if group else "Wrestling",
                           "thumbnail": logo.group(1) if logo else ""}
            elif pending and line.startswith("http"):
                match = _WATCH_RE.search(line.strip())
                if match and match.group(1) not in seen:
                    vid = match.group(1)
                    pending.update({"id": "yt:" + vid, "video_id": vid,
                                    "url": "https://www.youtube.com/watch?v=" + vid,
                                    "source": "youtube", "source_label": "YouTube"})
                    items.append(pending)
                    seen.add(vid)
                pending = None
    return items


def _telegram_items(limit: int = 40) -> list[dict]:
    rows = db.q(
        """SELECT m.id, m.chat_id, m.message_id, m.chat_title, m.caption,
                  m.filename, m.link, m.date_utc
             FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
            WHERE g.chosen=1
              AND lower(COALESCE(m.caption,'') || ' ' || COALESCE(m.filename,''))
                  GLOB '*wrestl*'
            ORDER BY COALESCE(m.date_utc,'') DESC, m.id DESC LIMIT 400""")
    strong = re.compile(r"\b(?:wwe|aew|njpw|tna|nxt|wrestlemania|wrestling)\b", re.I)
    exclude = re.compile(r"ducktales|garp und|scooby|the wrestler", re.I)
    out = []
    for r in rows:
        title = r["caption"] or r["filename"] or "Wrestling"
        if not strong.search(title) or exclude.search(title):
            continue
        out.append({"id": "tg:" + str(r["id"]), "title": title,
                    "league": "Telegram Wrestling", "source": "telegram", "source_label": "Telegram",
                    "url": r["link"] or "", "chat_title": r["chat_title"] or "", "date": r["date_utc"] or "",
                    "message_id": r["message_id"]})
        if len(out) >= limit:
            break
    return out


def wrestling(limit: int = 120) -> dict:
    youtube = _youtube_items()
    telegram = _telegram_items()
    items = (telegram + youtube)[:max(1, min(int(limit), 300))]
    return {"ok": True, "category": "wrestling", "sources": {"telegram": len(telegram), "youtube": len(youtube)},
            "items": items, "source_files": len(_playlist_files())}
