"""'Im Kino' — aktuelle Kinostarte aus TMDB discover (region DE).

Keine eigene Suche: /discover/movie liefert Veroeffentlichungen im Zeitraum,
sortiert nach Popularitaet, mit Poster/Backdrop. Antwort wird 6 h gecacht.
Pro Titel prueft die App, ob er schon in der eigenen Videothek steckt
(verlinkt dann direkt auf ▶), sonst steht er als Wunsch auf der Liste.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

from . import db, tmdb

_CACHE_S = 6 * 3600
_CACHE: dict = {"at": 0.0, "items": []}
_STREAM_CACHE: dict = {"at": 0.0, "providers": []}

# Streaming-Anbieter (TMDB watch_providers DE) unter 'Im Kino' als Sektionen.
# Reihenfolge = Anzeige im Tab; Felder: id, name, icon (Emoji).
PROVIDERS = [
    {"id": 8, "name": "Netflix", "icon": "🅝"},
    {"id": 337, "name": "Disney+", "icon": "🄳"},
    {"id": 9, "name": "Prime Video", "icon": "🅿"},
    {"id": 350, "name": "Apple TV+", "icon": ""},
]

_TRAILER_CACHE: dict[int, str] = {}
_TRAILER_AT: dict[int, float] = {}


def _filmdict(r: dict) -> dict:
    y = (r.get("release_date") or "")[:4]
    return {"tmdb_id": int(r["id"]), "title": r.get("title") or "",
            "year": int(y) if y.isdigit() else None,
            "release_date": r.get("release_date") or "",
            "overview": r.get("overview") or "", "rating": r.get("vote_average"),
            "genres": "", "poster_path": r.get("poster_path") or "",
            "backdrop_path": r.get("backdrop_path") or ""}


def _trailer_key(tmdb_id: int) -> str:
    """YouTube-Trailer-Key je Kino-Titel, 6h gecacht (kein TMDB-Call pro Request)."""
    now = time.time()
    if tmdb_id in _TRAILER_CACHE and now - _TRAILER_AT.get(tmdb_id, 0) < _CACHE_S:
        return _TRAILER_CACHE[tmdb_id]
    try:
        key = tmdb.trailer_key("movie", tmdb_id)
    except Exception:
        key = "none"
    _TRAILER_CACHE[tmdb_id] = key
    _TRAILER_AT[tmdb_id] = now
    return key


def _fetch() -> list[dict]:
    if _CACHE["items"] and time.time() - _CACHE["at"] < _CACHE_S:
        return _CACHE["items"]
    today = date.today()
    frm = (today - timedelta(days=110)).isoformat()
    to = (today + timedelta(days=45)).isoformat()
    items: list[dict] = []
    for page in (1, 2, 3):
        d = tmdb._get("/discover/movie", {
            "region": "DE", "language": "de-DE",
            "primary_release_date.gte": frm, "primary_release_date.lte": to,
            "vote_count.gte": 30, "include_adult": "true",
            "sort_by": "popularity.desc", "page": page})
        for r in (d or {}).get("results", []):
            m = _filmdict(r)
            m["poster_local"] = tmdb.cache_poster(m["poster_path"]) or ""
            items.append(m)
        if not (d or {}).get("results"):
            break
    _CACHE.update({"at": time.time(), "items": items})
    return items


def _fetch_stream_providers() -> list[dict]:
    """Pro Anbieter die aktuellen Neuzugaenge (watch_providers=DE), 6h gecacht."""
    now = time.time()
    if _STREAM_CACHE["providers"] and now - _STREAM_CACHE["at"] < _CACHE_S:
        return _STREAM_CACHE["providers"]
    out = []
    for p in PROVIDERS:
        items = []
        try:
            d = tmdb._get("/discover/movie", {
                "region": "DE", "language": "de-DE",
                "with_watch_providers": p["id"], "watch_region": "DE",
                "sort_by": "release_date.desc", "include_adult": "true",
                "vote_count.gte": 20, "page": 1})
            for r in (d or {}).get("results", [])[:12]:
                m = _filmdict(r)
                m["poster_local"] = tmdb.cache_poster(m["poster_path"]) or ""
                items.append(m)
        except Exception:
            items = []
        out.append({**p, "items": items})
    _STREAM_CACHE.update({"at": now, "providers": out})
    return out


def list_theaters() -> dict:
    """Kinostarte + Streaming-Neustarts + Verlinkung in die eigene Bibliothek."""
    items = list(_fetch())
    providers = _fetch_stream_providers()
    all_ids = set(m["tmdb_id"] for m in items)
    for p in providers:
        all_ids.update(m["tmdb_id"] for m in p["items"])
    own: dict[int, int] = {}
    if all_ids:
        ids = list(all_ids)
        marks = ",".join("?" * len(ids))
        for r in db.q(
            f"""SELECT f.tmdb_id, f.id FROM films f
                WHERE f.tmdb_id IN ({marks}) AND f.kind='movie'
                  AND EXISTS(SELECT 1 FROM media_items m WHERE m.film_id=f.id
                        AND m.chat_id IN (SELECT chat_id FROM groups WHERE chosen=1))""", tuple(ids)):
            own[int(r["tmdb_id"])] = int(r["id"])
    # Groesste Quelle pro Film: Header soll nur zeigen, was flott startbar ist
    sizes: dict[int, int] = {}
    if own:
        ids2 = ",".join("?" * len(own))
        for r in db.q(
            f"""SELECT m.film_id, MAX(m.size_bytes) s FROM media_items m
                  WHERE m.film_id IN ({ids2})
                  GROUP BY m.film_id""", tuple(own.values())):
            sizes[int(r["film_id"])] = int(r["s"] or 0)
    for m in items:
        m["film_id"] = own.get(m["tmdb_id"])
        m["size_bytes"] = sizes.get(m["film_id"] or 0)
        m["trailer"] = _trailer_key(m["tmdb_id"])
    for p in providers:
        for m in p["items"]:
            m["film_id"] = own.get(m["tmdb_id"])
            m["size_bytes"] = sizes.get(m["film_id"] or 0)
            m["trailer"] = _trailer_key(m["tmdb_id"])
    return {"items": items, "providers": providers}
