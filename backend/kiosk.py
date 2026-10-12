"""Datenzugriff fuer Backroom Kiosk.

Der Kiosk zeigt nur echte Printmedien aus vom Nutzer gewählten Telegram-Gruppen.
Ohne Auswahl oder ohne Printbestand liefert er stattdessen kostenlose News im Zeitungsformat.
"""
from __future__ import annotations

import re
from . import db
from . import ticker

_PRINT_EXTS = (".pdf", ".epub", ".mobi", ".cbz", ".cbr", ".djvu")
_PRINT_KINDS = {"print", "newspaper", "magazine", "book", "document"}


def _kind(title: str, media_kind: str | None) -> str:
    text = (title or "").lower()
    if media_kind in {"series", "tv"} or re.search(r"staffel|s\d{1,2}e\d{1,2}", text):
        return "magazine"
    if re.search(r"buch|book|roman|reader|literatur", text):
        return "book"
    return "newspaper"


def catalog(limit: int = 48) -> dict:
    chosen = db.q("""SELECT g.chat_id, g.title, ks.chosen
                     FROM kiosk_sources ks JOIN groups g ON g.chat_id=ks.chat_id
                    WHERE ks.chosen=1 ORDER BY g.title COLLATE NOCASE""")
    if not chosen:
        return {"ok": True, "chosen": [], "items": [], "has_sources": False,
                "fallback": _fallback_news(limit), "fallback_reason": "no_group"}
    rows = db.q(
        """SELECT m.id, m.chat_title, m.caption, m.filename, m.date_utc,
                  m.media_kind, m.link
             FROM media_items m JOIN kiosk_sources ks ON ks.chat_id=m.chat_id
                                  JOIN groups g ON g.chat_id=m.chat_id
            WHERE ks.chosen=1
              AND (LOWER(COALESCE(m.filename,'')) LIKE '%.pdf'
                OR LOWER(COALESCE(m.filename,'')) LIKE '%.epub'
                OR LOWER(COALESCE(m.filename,'')) LIKE '%.mobi'
                OR LOWER(COALESCE(m.filename,'')) LIKE '%.cbz'
                OR LOWER(COALESCE(m.filename,'')) LIKE '%.cbr'
                OR LOWER(COALESCE(m.filename,'')) LIKE '%.djvu'
                OR m.media_kind IN ('print','newspaper','magazine','book','document'))
            ORDER BY COALESCE(m.date_utc,'') DESC, m.id DESC LIMIT ?""",
        (max(1, min(int(limit), 200)),))
    items = []
    for row in rows:
        title = (row.get("caption") or row.get("filename") or "").strip()
        if not title:
            continue
        items.append({"id": row["id"], "title": title.split("\n", 1)[0][:180],
                      "category": _kind(title, row.get("media_kind")),
                      "source": row.get("chat_title") or "Telegram-Gruppe",
                      "date": row.get("date_utc") or "", "link": row.get("link") or "",
                      "print": True})
    if not items:
        return {"ok": True, "chosen": chosen, "items": [], "has_sources": True,
                "fallback": _fallback_news(limit), "fallback_reason": "no_print"}
    return {"ok": True, "chosen": chosen, "items": items, "has_sources": True,
            "fallback": [], "fallback_reason": ""}


def _fallback_news(limit: int) -> list[dict]:
    """Kostenlose RSS-News als ehrlicher Kiosk-Ersatz ohne Printquelle."""
    try:
        news = ticker.get_news().get("items") or []
    except Exception:
        news = []
    return [{"title": x.get("title", "Nachricht"), "source": "News",
             "category": "newspaper", "link": x.get("url", ""),
             "cover": "NEWS", "print": False}
            for x in news[:max(1, min(int(limit), 60))]]
