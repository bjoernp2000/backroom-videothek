"""Sichere Datenbereinigung: Dubletten-Filme zusammenfuehren (fuer Einstellungen)."""
from __future__ import annotations

import re
import unicodedata

from . import db


def _norm(t):
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t or "")
    t = t.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def run() -> dict:
    """Dubletten (gleicher norm. Titel+Jahr+kind, alle tmdb_id=None) zusammenfuehren.
    media_items auf die behaltene film_id umhaengen, Duplikat-films loeschen."""
    films = db.q("SELECT id, kind, tmdb_id, title, year FROM films")
    from collections import defaultdict
    groups = defaultdict(list)
    for f in films:
        n = _norm(f["title"])
        if len(n) >= 4:
            groups[(n, f["year"], f["kind"])].append(f)

    moved = deleted = 0
    conn = db.get_db()
    for group in groups.values():
        if len(group) < 2:
            continue
        if any(f["tmdb_id"] is not None for f in group):
            continue  # tmdb_id vorhanden -> nicht sicher
        # behalten: mehr media_items, sauberer Titel, niedrigere id
        def score(f):
            mc = db.one("SELECT COUNT(*) c FROM media_items WHERE film_id=?", (f["id"],))["c"]
            clean = 0 if "**" in (f["title"] or "") else 1
            return (mc, clean, -f["id"])
        keep = max(group, key=score)
        for f in group:
            if f["id"] == keep["id"]:
                continue
            cur = conn.execute("UPDATE media_items SET film_id=? WHERE film_id=?",
                               (keep["id"], f["id"]))
            moved += cur.rowcount
            try:
                conn.execute("DELETE FROM films WHERE id=?", (f["id"],))
                deleted += 1
            except Exception:
                pass
    conn.commit()
    return {"ok": True, "moved": moved, "deleted": deleted}
