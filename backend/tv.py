"""Backroom-TV Quellen: M3U/M3U8 plus XMLTV, ohne Kodi-Abhängigkeit."""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import config

_SOURCES = config.DATA_DIR / "tv_sources.json"
_CACHE: dict[str, tuple[float, object]] = {}
_MAX_BYTES = 64 * 1024 * 1024
_DEFAULT_SOURCES = [
    {"id": "public-pluto-de", "name": "Pluto TV Deutschland · öffentlich", "type": "m3u",
     "playlist_url": "https://raw.githubusercontent.com/BuddyChewChew/pluto/main/pluto_de.m3u",
     "epg_url": "https://github.com/matthuisman/i.mjh.nz/raw/refs/heads/master/PlutoTV/all.xml.gz",
     "enabled": True, "public": True, "license": "MIT / externe Pluto-Inhalte"},
    {"id": "public-free-tv-de", "name": "Free-TV Deutschland · öffentlich", "type": "m3u",
     "playlist_url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_germany.m3u8",
     "epg_url": "", "enabled": True, "public": True, "license": "Quellprojekt"},
    {"id": "public-german-tv", "name": "German TV M3U · öffentlich", "type": "m3u",
     "playlist_url": "https://raw.githubusercontent.com/josxha/german-tv-m3u/main/german-tv.m3u",
     "epg_url": "", "enabled": True, "public": True, "license": "Quellprojekt"},
    {"id": "public-iptv-org-de", "name": "IPTV-org Deutschland · öffentlich", "type": "m3u",
     "playlist_url": "https://iptv-org.github.io/iptv/countries/de.m3u",
     "epg_url": "", "enabled": True, "public": True, "license": "Quellprojekt"},
]


def _valid_url(value: str) -> bool:
    p = urlparse((value or "").strip())
    return p.scheme in {"http", "https"} and bool(p.netloc)


def _load_sources() -> list[dict]:
    try:
        data = json.loads(_SOURCES.read_text(encoding="utf-8"))
        configured = data if isinstance(data, list) else []
    except (OSError, ValueError):
        configured = []
    configured_ids = {item.get("id") for item in configured if isinstance(item, dict)}
    return list(_DEFAULT_SOURCES) + [item for item in configured if item.get("id") not in configured_ids.intersection({x["id"] for x in _DEFAULT_SOURCES})]


def _save_sources(items: list[dict]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SOURCES.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def sources() -> list[dict]:
    return [{k: v for k, v in x.items() if k != "password"} for x in _load_sources()]


def add_source(name: str, playlist_url: str, epg_url: str = "") -> dict:
    name = (name or "TV-Quelle").strip()[:100]
    playlist_url = playlist_url.strip()
    epg_url = epg_url.strip()
    if not _valid_url(playlist_url):
        raise ValueError("Die M3U-Adresse muss eine gültige HTTP(S)-Adresse sein.")
    if epg_url and not _valid_url(epg_url):
        raise ValueError("Die EPG-Adresse muss eine gültige HTTP(S)-Adresse sein.")
    rows = _load_sources()
    source = {"id": f"src-{int(time.time() * 1000)}", "name": name,
              "type": "m3u", "playlist_url": playlist_url, "epg_url": epg_url,
              "enabled": True}
    rows.append(source)
    _save_sources(rows)
    _CACHE.clear()
    return {k: v for k, v in source.items() if k != "password"}


def remove_source(source_id: str) -> bool:
    rows = _load_sources()
    kept = [x for x in rows if x.get("id") != source_id]
    if len(kept) == len(rows):
        return False
    _save_sources(kept)
    _CACHE.clear()
    return True


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Backroom-TV/1.0"})
    with urllib.request.urlopen(req, timeout=12) as response:
        data = response.read(_MAX_BYTES + 1)
    if url.lower().endswith((".gz", ".gzip")):
        data = gzip.decompress(data)
    if len(data) > _MAX_BYTES:
        raise ValueError("Quelle ist größer als das erlaubte Limit.")
    return data


def _text_attr(line: str, key: str) -> str:
    match = re.search(rf'{re.escape(key)}="([^"]*)"', line, re.I)
    return match.group(1).strip() if match else ""


def _parse_m3u(source: dict, raw: bytes) -> list[dict]:
    lines = raw.decode("utf-8-sig", errors="replace").splitlines()
    out: list[dict] = []
    pending: dict | None = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF:"):
            title = line.split(",", 1)[1].strip() if "," in line else "Unbenannter Sender"
            pending = {"name": title or "Unbenannter Sender",
                       "group": _text_attr(line, "group-title") or "Ohne Gruppe",
                       "logo": _text_attr(line, "tvg-logo"),
                       "tvg_id": _text_attr(line, "tvg-id"),
                       "country": _text_attr(line, "tvg-country"),
                       "source_id": source["id"], "source": source["name"]}
        elif pending and not line.startswith("#"):
            url = urljoin(source["playlist_url"], line)
            if _valid_url(url):
                pending["stream_url"] = url
                out.append(pending)
            pending = None
    return out


def channels(limit: int = 500) -> dict:
    all_channels: list[dict] = []
    source_status = []
    for source in _load_sources():
        if not source.get("enabled", True):
            continue
        try:
            cache_key = "m3u:" + source["id"]
            cached = _CACHE.get(cache_key)
            if cached and time.time() - cached[0] < 300:
                items = cached[1]
            else:
                items = _parse_m3u(source, _fetch(source["playlist_url"]))
                _CACHE[cache_key] = (time.time(), items)
            all_channels.extend(items[: max(1, min(limit, 1000))])
            source_status.append({"id": source["id"], "name": source["name"], "ok": True, "count": len(items)})
        except Exception as exc:
            source_status.append({"id": source["id"], "name": source["name"], "ok": False, "count": 0, "error": str(exc)[:180]})
    return {"ok": True, "channels": all_channels[: max(1, min(limit, 1000))], "sources": sources(), "source_status": source_status}


def epg(channel_id: str = "", source_id: str = "", limit: int = 80) -> dict:
    source = next((x for x in _load_sources() if x.get("id") == source_id), None)
    if not source or not source.get("epg_url"):
        return {"ok": True, "programs": [], "available": False}
    try:
        root = ET.fromstring(_fetch(source["epg_url"]))
        programs = []
        for node in root.findall("programme"):
            if channel_id and node.get("channel") != channel_id:
                continue
            title = node.findtext("title", default="").strip()
            start = node.get("start", "").strip()
            stop = node.get("stop", "").strip()
            if title:
                programs.append({"channel": node.get("channel", ""), "title": title,
                                 "description": node.findtext("desc", default="").strip(),
                                 "start": start, "stop": stop})
            if len(programs) >= max(1, min(limit, 300)):
                break
        return {"ok": True, "available": bool(programs), "programs": programs}
    except Exception as exc:
        return {"ok": False, "available": False, "programs": [], "error": str(exc)[:180]}
