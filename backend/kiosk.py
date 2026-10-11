"""Datenzugriff fuer Backroom Kiosk.

Der Kiosk zeigt nur Medien aus vom Nutzer gewählten Telegram-Gruppen.
Ohne Auswahl liefert die Funktion bewusst keine Medien.
"""
from __future__ import annotations

import re
from . import db


def _kind(title: str, media_kind: str | None) -> str:
    text = (title or "").lower()
    if media_kind in {"series", "tv"} or re.search(r"staffel|s\d{1,2}e\d{1,2}", text):
        return "magazine"
    if re.search(r"buch|book|roman|reader|literatur", text):
        return "book"
    return "newspaper"


def catalog(limit: int = 48) -> dict:
    groups = db.q("SELECT chat_id, title, chosen FROM groups ORDER BY title COLLATE NOCASE")
    chosen = [g for g in groups if int(g.get("chosen") or 0) == 1]
    if not chosen:
        return {"ok": True, "chosen": [], "items": [], "has_sources": False}
    rows = db.q(
        """SELECT m.id, m.chat_title, m.caption, m.filename, m.date_utc,
                  m.media_kind, m.link
             FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
            WHERE g.chosen=1
            ORDER BY COALESCE(m.date_utc,'') DESC, m.id DESC LIMIT ?""", (max(1, min(int(limit), 200)),))
    items = []
    for row in rows:
        title = (row.get("caption") or row.get("filename") or "").strip()
        if not title:
            continue
        items.append({"id": row["id"], "title": title.split("\n", 1)[0][:180],
                      "category": _kind(title, row.get("media_kind")),
                      "source": row.get("chat_title") or "Telegram-Gruppe",
                      "date": row.get("date_utc") or "", "link": row.get("link") or ""})
    return {"ok": True, "chosen": chosen, "items": items, "has_sources": True}
