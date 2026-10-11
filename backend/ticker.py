""""Ticker & Wetter" — News aus Film/Fernsehen (RSS) + aktuelle Temperatur.

News: mehrere kostenlose Film-/Hollywood-RSS-Feeds werden abgerufen, dedupliziert
und auf eine schlanke Ticker-Liste (Titel+Link) reduziert. Antwort 15 min gecacht,
damit nicht jeder /api/ticker-Aufruf das Netz trifft.

Wetter: Open-Meteo (kostenlos, kein API-Key), Koordinaten aus config
(wetter: {lat, lon, stadt}); 30 min gecacht.
"""
from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

from . import config, db

_CACHE_S = 15 * 60
_NEWS_CACHE: dict = {"at": 0.0, "items": []}
_W_CACHE: dict = {"at": 0.0, "data": None}

# Stabile, kostenlose Film-/TV-/Hollywood-Feeds (kein API-Key). Fallen einzelne
# aus, wird einfach darauf verzichtet — der Ticker bleibt selbstheilend.
_FEEDS = [
    "https://deadline.com/feed/",
    "https://variety.com/feed/",
    "https://www.screenrant.com/feed/",
    "https://www.hollywoodreporter.com/feed",
]
_UA = {"User-Agent": "Mozilla/5.0 (Videothek; local ticker)"}
_MAX_ITEMS = 60

# Anzahl Wörter, ab denen ein Titel als zu langer Original-Titel gilt und man
# auf ein Wortspiel/kurz zurückfällt — wir zeigen eh nur eine Zeile.
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    s = html.unescape(s or "")
    s = _RE_TAGS.sub(" ", s)
    s = _RE_WS.sub(" ", s).strip()
    return s


def _feed_items(url: str) -> list[dict]:
    try:
        r = requests.get(url, headers=_UA, timeout=12)
        root = ET.fromstring(r.content)
    except Exception:
        return []
    items = []
    # RSS: channel/item - Atom: feed/entry
    for node in root.iter():
        if node.tag.endswith("item") or node.tag.endswith("entry"):
            def _t(tag):
                e = node.find(tag)
                if e is not None and e.text:
                    return _clean(e.text)
                return ""
            title, link = _t("title"), _t("link")
            if link.startswith("{"):  # Atom-link hat Attribut href
                href = node.find("{http://www.w3.org/2005/Atom}link")
                if href is not None:
                    link = _clean(href.get("href") or "")
            if title and link:
                items.append({"title": title, "url": link})
    return items


def today_birthdays() -> list[dict]:
    """Personen mit Geburtstag heute (aus der dauerhaft gefuellten people-Tabelle)."""
    md = datetime.now().strftime("%m-%d")
    rows = db.q("""
        SELECT p.id, p.name, p.birthday, p.profile_path,
               (SELECT COUNT(DISTINCT fp.film_id) FROM film_people fp
                WHERE fp.person_id=p.id) AS films
        FROM people p
        WHERE p.birthday IS NOT NULL AND p.birthday!='' AND substr(p.birthday,6,5)=?
        ORDER BY p.name COLLATE NOCASE""", (md,))
    has = []
    for r in rows:
        with_img = db.one("SELECT 1 FROM people WHERE id=? AND profile_path IS NOT NULL AND profile_path!=''", (r["id"],))
        if r["name"]:
            has.append({"id": r["id"], "name": r["name"],
                        "birthday": r["birthday"], "films": r["films"],
                        "img": bool(with_img)})
    return has


# --- Fallback: bekannte Personen + historische Ereignisse des Tages ---
# Wenn heute niemand aus der Videothek Geburtstag hat, liefert die Wikipedia
# "On this day"-API (kostenlos, kein Key) bekannte Personen (births) und
# historische Ereignisse (events) des Datums. 24h gecacht.
_OTD_CACHE: dict = {"at": 0.0, "data": None}
_OTD_S = 24 * 3600


def _otd_fetch(md: str) -> list[dict]:
    """Wikipedia On-this-day: bekannte Personen + Ereignisse fuer MM-DD."""
    try:
        r = requests.get(
            f"https://api.wikimedia.org/feed/v1/wikipedia/de/onthisday/all/{md}",
            headers={"User-Agent": "Videothek/1.0 (local media app)"},
            timeout=8)
        r.raise_for_status()
        d = r.json()
    except Exception:
        return []
    out = []
    for x in (d.get("births") or [])[:6]:
        out.append({"type": "person", "name": x.get("text", "").split(",")[0].strip(),
                    "detail": f"geboren {x.get('year')}", "year": x.get("year")})
    for x in (d.get("events") or [])[:4]:
        out.append({"type": "event", "name": x.get("text", "")[:90],
                    "detail": f"Ereignis {x.get('year')}", "year": x.get("year")})
    return out


def today_events() -> list[dict]:
    """Geburtstage aus der Videothek; wenn leer, bekannte Personen + Ereignisse
    des Tages von Wikipedia (damit der Ticker nie leer ist)."""
    bd = today_birthdays()
    if bd:
        return bd
    now = time.time()
    if _OTD_CACHE["data"] and now - _OTD_CACHE["at"] < _OTD_S:
        return _OTD_CACHE["data"]
    md = datetime.now().strftime("%m/%d")
    data = _otd_fetch(md)
    _OTD_CACHE.update({"at": now, "data": data})
    return data


def get_news(force: bool = False) -> dict:
    now = time.time()
    if not force and _NEWS_CACHE["items"] and now - _NEWS_CACHE["at"] < _CACHE_S:
        return {"items": _NEWS_CACHE["items"], "birthdays": today_events(),
                "fetched": _NEWS_CACHE["at"]}
    seen, combined = set(), []
    for url in _FEEDS:
        for it in _feed_items(url):
            key = re.sub(r"[^a-z0-9]", "", (it["title"] or "").lower())[:60]
            if key and key not in seen:
                seen.add(key)
                combined.append(it)
    combined.sort(key=lambda x: len(x["title"]))
    combined = combined[:_MAX_ITEMS]
    _NEWS_CACHE.update({"at": now, "items": combined})
    return {"items": combined, "fetched": now, "birthdays": today_events()}


def get_weather(force: bool = False) -> dict:
    now = time.time()
    if not force and _W_CACHE["data"] and now - _W_CACHE["at"] < 30 * 60:
        return _W_CACHE["data"]
    wc = config.CFG.get("wetter") or {}
    lat, lon = wc.get("lat"), wc.get("lon")
    stadt = wc.get("stadt") or ""
    out = {"ok": False, "stadt": stadt}
    if not lat or not lon:
        out["error"] = "Koordinaten fehlen (config.json: wetter.lat/lon eingetragen)"
        _W_CACHE.update({"at": now, "data": out})
        return out
    try:
        r = requests.get("https://api.open-meteo.com/v1/forecast",
                         params={"latitude": lat, "longitude": lon,
                                 "current_weather": "true",
                                 "timezone": "auto"}, timeout=12)
        d = r.json()
        cw = d.get("current_weather") or {}
        out.update(ok=True, temp=cw.get("temperature"),
                   wmo=cw.get("weathercode"), time=cw.get("time"))
    except Exception as e:
        out["error"] = str(e)[:150]
    _W_CACHE.update({"at": now, "data": out})
    return out


# WMO Wettercode -> kurzes Icon/Label (für die Kopfzeile)
_WMO = {0: "☀️", 1: "🌤", 2: "⛅", 3: "☁️", 45: "🌫", 48: "🌫",
        51: "🌦", 53: "🌦", 55: "🌧", 61: "🌧", 63: "🌧", 65: "🌧",
        71: "🌨", 73: "🌨", 75: "❄️", 80: "🌦", 81: "🌧", 82: "⛈",
        95: "⛈", 96: "⛈", 99: "⛈"}


def weather_short(data: dict) -> str:
    if not data or not data.get("ok"):
        return ""
    icon = _WMO.get(int(data.get("wmo") or 0), "🌡")
    t = data.get("temp")
    tstr = f"{float(t):.0f}°" if t is not None else ""
    stadt = data.get("stadt") or ""
    return f"{icon} {stadt} {tstr}".strip()