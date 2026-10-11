"""TMDB-Anbindung: Suchen, Metadaten, Poster-Cache lokal."""
from __future__ import annotations

import hashlib
import io
import re
from difflib import SequenceMatcher
import time
import urllib.parse

import requests

from . import config

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p"
POSTER_SIZE = "w342"
UA = {"User-Agent": "Videothek/1.0 (local app)"}

_session = requests.Session()
_cache: dict[str, tuple[float, dict | None]] = {}


def _get(path: str, params: dict) -> dict | None:
    key = hashlib.sha1((path + "?" + urllib.parse.urlencode(sorted(params.items()))).encode()).hexdigest()
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < 6 * 3600:
        return hit[1]
    cfg = config.CFG
    headers = dict(UA)
    if cfg.get("tmdb_token"):
        headers["Authorization"] = "Bearer " + cfg["tmdb_token"]
        params = dict(params)
    else:
        params = dict(params, api_key=cfg.get("tmdb_key", ""))
    try:
        r = _session.get(BASE + path, params=params, headers=headers, timeout=12)
        data = r.json() if r.ok else None
    except Exception:
        data = None
    _cache[key] = (now, data)
    return data


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("&", " und ")   # '&', 'und' und 'and' sind gleich
    s = re.sub(r"\band\b", " und ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _ratio(a: str, b: str) -> float:
    """Aehnlichkeit zweier normalisierter Titel (Tippfehler-Toleranz)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def search(title: str, year: int | None = None):
    """-> {kind:'movie'|'tv', tmdb_id, title, original_title, year, genres, overview, poster_path, backdrop_path, rating}"""
    cfg = config.CFG
    if not (cfg.get("tmdb_key") or cfg.get("tmdb_token")):
        return None
    t = (title or "").strip()
    if not t:
        return None
    # Erstkandidat + TV, dann Filme; Jahr mit Toleranz wichten
    candidates = []
    for kind in ("movie", "tv"):
        d = _get(f"/search/{kind}", {"query": t, "language": "de-DE", "include_adult": "true"})
        for res in (d or {}).get("results", [])[:5]:
            ry = None
            for f in ("release_date", "first_air_date"):
                v = res.get(f) or ""
                if len(v) >= 4 and v[:4].isdigit():
                    ry = int(v[:4])
                    break
            candidates.append((kind, res, ry))
    if not candidates:
        # ein zweiter Versuch ohne Umlaute/Artikel
        t2 = re.sub(r"[äöüß]", lambda m: {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}[m.group(0)], t)
        if t2 != t:
            return search(t2, year)
        return None

    def score(item):
        kind, res, ry = item
        s = 0.0
        cn = _norm(res.get("title") or res.get("name") or "")
        tn = _norm(t)
        if cn == tn:
            s += 3
        elif _ratio(cn, tn) >= 0.85:
            s += 2   # Tippfehler-Toleranz ('ot he' vs 'of the')
        s += float(res.get("vote_count") or 0) ** 0.25 * 0.3
        s += float(res.get("popularity") or 0) * 0.002
        if year and ry:
            s += 2.5 if abs(ry - year) <= 1 else -2.0
        if kind == "movie":
            s += 0.6  # Videothek = Film-lastig
        return s

    candidates.sort(key=score, reverse=True)
    kind, res, ry = candidates[0]
    best = score(candidates[0])
    exact = _norm(res.get("title") or res.get("name") or "") == _norm(t)
    year_match = bool(year and ry and abs(ry - year) <= 1)
    typo = (not exact) and _ratio(_norm(res.get("title") or res.get("name") or ""), _norm(t)) >= 0.9
    det = _get(f"/{kind}/{res['id']}", {"language": "de-DE"}) or {}
    genres = ", ".join(str(g.get("name", "")) for g in (det.get("genres") or []) if isinstance(g, dict) and g.get("name"))
    release = det.get("release_date") or det.get("first_air_date") or ""
    y = int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else ry
    poster = det.get("poster_path") or res.get("poster_path") or ""
    # Kein Poster beim gematchten Eintrag -> in den Kandidaten einen mit Kunst suchen
    # (oft hat ein verwandter/richtigerer Eintrag ein Poster, der leere ist falsch/billig).
    # Besten Kandidaten mit Poster nach demselben Scoring nehmen.
    if not poster:
        bestp, bests = "", -1e9
        for kind2, res2, ry2 in candidates:
            if not res2.get("poster_path"):
                continue
            s2 = score((kind2, res2, ry2))
            # leichter Bonus fuer den gematchten Eintrag selbst, falls er doch Poster hat
            if s2 > bests:
                bests, bestp = s2, res2.get("poster_path")
        poster = bestp
    return {
        "kind": kind,
        "tmdb_id": int(res["id"]),
        "title": det.get("title") or det.get("name") or res.get("title") or res.get("name") or t,
        "original_title": det.get("original_title") or det.get("original_name") or "",
        "year": y,
        "genres": genres,
        "overview": det.get("overview") or "",
        "poster_path": poster,
        "backdrop_path": det.get("backdrop_path") or "",
        "rating": det.get("vote_average") or res.get("vote_average") or None,
        "conf": {"exact": exact, "year_match": year_match, "typo": typo, "score": best},
    }


def details(kind: str, tmdb_id: int) -> dict | None:
    """Lädt einen einzelnen TMDB-Datensatz für manuelle Zuordnung."""
    kind = "tv" if kind in ("tv", "series") else "movie"
    raw = _get(f"/{kind}/{int(tmdb_id)}", {"language": "de-DE"}) or {}
    if not raw or not raw.get("id"):
        return None
    release = raw.get("release_date") or raw.get("first_air_date") or ""
    genres = ", ".join(str(g.get("name", "")) for g in (raw.get("genres") or [])
                        if isinstance(g, dict) and g.get("name"))
    return {
        "kind": kind, "tmdb_id": int(raw["id"]),
        "title": raw.get("title") or raw.get("name") or "",
        "original_title": raw.get("original_title") or raw.get("original_name") or "",
        "year": int(release[:4]) if len(release) >= 4 and release[:4].isdigit() else None,
        "genres": genres, "overview": raw.get("overview") or "",
        "poster_path": raw.get("poster_path") or "",
        "backdrop_path": raw.get("backdrop_path") or "",
        "rating": raw.get("vote_average") or None,
    }


def search_multi(query: str, limit: int = 8):
    """Rohsuche fuer den Zuordnungs-Dialog: mehrere Kandidaten (Film + Serie)."""
    cfg = config.CFG
    if not (cfg.get("tmdb_key") or cfg.get("tmdb_token")):
        return []
    out = []
    for kind in ("movie", "tv"):
        d = _get(f"/search/{kind}", {"query": query, "language": "de-DE", "include_adult": "true"})
        for res in (d or {}).get("results", [])[:limit]:
            rel = res.get("release_date") or res.get("first_air_date") or ""
            out.append({
                "kind": kind,
                "tmdb_id": int(res["id"]),
                "title": res.get("title") or res.get("name") or "",
                "year": int(rel[:4]) if len(rel) >= 4 and rel[:4].isdigit() else None,
                "poster_path": res.get("poster_path") or "",
                "overview": (res.get("overview") or "")[:180],
            })
    out.sort(key=lambda r: (-(r["year"] or 0)))
    return out[:limit * 2]


def trailer_key(kind: str, tmdb_id: int) -> str:
    """Bester YouTube-Trailer-Key (Trailer vor Teaser, offiziell vor resto, 'de' ok)."""
    d = _get(f"/{kind}/{tmdb_id}/videos", {"language": "de-DE"})
    vids = (d or {}).get("results") or []
    score = {}
    for v in vids:
        if (v.get("site") or "").lower() != "youtube" or not v.get("key"):
            continue
        typ = (v.get("type") or "").lower()
        s = {"trailer": 3, "teaser": 2}.get(typ, 1)
        if v.get("official"):
            s += 0.5
        if (v.get("name") or "").lower().find("deuts") >= 0 or v.get("iso_639_1") == "de":
            s += 0.25
        score.setdefault(v["key"], 0)
        score[v["key"]] = max(score[v["key"]], s)
    if not score:
        return "none"
    return max(score, key=score.get)


def fsk(kind: str, tmdb_id: int) -> str:
    """Deutsche FSK-Freigabe: Movie release_dates DE, TV content_ratings DE.
    -> z.B. 'FSK 16' oder '' wenn keine DE-Zertifizierung existiert."""
    try:
        if kind in ("tv", "series"):
            d = _get(f"/tv/{tmdb_id}/content_ratings", {}) or {}
            for r in (d.get("results") or []):
                if (r.get("iso_3166_1") or "").upper() == "DE":
                    c = (r.get("rating") or "").strip()
                    if c:
                        return c
        else:
            d = _get(f"/movie/{tmdb_id}/release_dates", {}) or {}
            for r in (d.get("results") or []):
                if (r.get("iso_3166_1") or "").upper() == "DE":
                    for rd in (r.get("release_dates") or []):
                        c = (rd.get("certification") or "").strip()
                        if c and (c.isdigit() or c.lower().startswith("fsk")):
                            return c
    except Exception:
        pass
    return ''


def _save_webp(content: bytes, dest) -> bool:
    """Re-encodiert Bild-Bytes als WebP (q80) -> deutlich weniger Platz."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(content)).convert("RGB")
        img.save(dest, "WEBP", quality=80)
        return True
    except Exception:
        return False


def cache_url(url: str) -> str | None:
    """Laedt ein beliebiges Bild (z.B. Fanart-URL) in data/posters/ und gibt den
    lokalen Dateinamen zurueck; None wenn fehlgeschlagen."""
    if not url:
        return None
    try:
        import os
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", url.split("?")[0].split("/")[-1])
        name = name.rsplit(".", 1)[0] + ".webp"
        fp = os.path.join(config.POSTER_DIR, name)
        if os.path.exists(fp):
            return name
        r = _session.get(url, headers=UA, timeout=20)
        if not r.ok:
            return None
        if _save_webp(r.content, fp):
            return name
        return None
    except Exception:
        return None


def fanart_cover(kind: str, tmdb_id: int) -> str:
    """Beste Fanart.tv-Poster-URL (Movie: movieposter; TV: tvposter ueber tvdb_id).
    Leer wenn kein Key oder keine Kunst vorhanden."""
    key = (config.CFG.get("fanart_key") or "").strip()
    if not key:
        return ""
    try:
        if kind in ("tv", "series"):
            ext = _get(f"/tv/{tmdb_id}/external_ids", {}) or {}
            tvdb = ext.get("tvdb_id")
            if not tvdb:
                return ""
            d = _session.get(f"https://webservice.fanart.tv/v3/tv/{tvdb}",
                             params={"api_key": key}, headers=UA, timeout=15).json()
            items = d.get("tvposter") or []
        else:
            d = _session.get(f"https://webservice.fanart.tv/v3/movies/{tmdb_id}",
                             params={"api_key": key}, headers=UA, timeout=15).json()
            items = d.get("movieposter") or []
        best, bests = "", -1
        for it in items:
            s = int(it.get("likes") or 0)
            if it.get("url") and s > bests:
                bests, best = s, it["url"]
        return best
    except Exception:
        return ""


def cache_poster(poster_path: str) -> str | None:
    """Laedt Poster runter -> dateiname im posters/-Ordner; None wenn nichts."""
    if not poster_path:
        return None
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", poster_path.split("/")[-1]).rsplit(".", 1)[0] + ".webp"
    dest = config.POSTER_DIR / name
    if dest.exists() and dest.stat().st_size > 0:
        return name
    url = f"{IMG}/{POSTER_SIZE}{poster_path}"
    try:
        r = _session.get(url, headers=UA, timeout=15)
        if r.ok and r.content and _save_webp(r.content, dest):
            return name
    except Exception:
        pass
    return None


BACKDROP_SIZE = "w780"


def cache_backdrop(backdrop_path: str) -> str | None:
    """Laedt ein Fanart/Backdrop runter -> dateiname im posters/-Ordner; None wenn nichts.

    Deckt den 'kein Cover -> such auf Fanart'-Fall ab: wenn ein Titel kein Poster hat,
    wird sein Backdrop lokal gecacht und in backdrop_path gespeichert, damit die
    Frontend-Fanart-Kachel (coverEl) eine echte Datei statt einer 404-URL laedt.
    """
    if not backdrop_path:
        return None
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", backdrop_path.split("/")[-1]).rsplit(".", 1)[0] + ".webp"
    dest = config.POSTER_DIR / name
    if dest.exists() and dest.stat().st_size > 0:
        return name
    url = f"{IMG}/{BACKDROP_SIZE}{backdrop_path}"
    try:
        r = _session.get(url, headers=UA, timeout=15)
        if r.ok and r.content and _save_webp(r.content, dest):
            return name
    except Exception:
        pass
    return None


def credits(kind: str, tmdb_id: int) -> dict | None:
    """Cast + Crew eines Titels (Regie/Drehbuch inklusive)."""
    kind = "movie" if kind in ("movie", None) else "tv"
    d = _get(f"/{kind}/{tmdb_id}/credits", {"language": "de-DE"})
    if d and d.get("cast"):
        return d
    d2 = _get(f"/{kind}/{tmdb_id}/credits", {})
    return d2 if d2 and d2.get("cast") else None


def season(tmdb_id: int, n: int) -> dict | None:
    """Eine komplette Staffel mit Folgenamen/-beschreibungen (de)."""
    d = _get(f"/tv/{tmdb_id}/season/{n}", {"language": "de-DE"})
    if d and d.get("episodes"):
        return d
    d2 = _get(f"/tv/{tmdb_id}/season/{n}", {})
    return d2 if d2 and d2.get("episodes") else None


def details_full(kind: str, tmdb_id: int) -> dict | None:
    """Details inkl. belongs_to_collection + production_companies (fuer Reihen)."""
    kind = "movie" if kind in ("movie", None) else "tv"
    return _get(f"/{kind}/{tmdb_id}", {"language": "de-DE"})

def similar(kind: str, tmdb_id: int, limit: int = 10) -> list[dict]:
    """Ähnliche Titel (TMDB /movie/{id}/similar oder /tv/{id}/similar)."""
    k = "movie" if kind in ("movie", None) else "tv"
    d = _get(f"/{k}/{tmdb_id}/similar", {"language": "de-DE", "page": 1})
    out = []
    for r in (d or {}).get("results") or []:
        out.append({
            "id": int(r["id"]),
            "kind": k,
            "title": r.get("title") or r.get("name") or "",
            "year": (r.get("release_date") or r.get("first_air_date") or "")[:4],
            "poster_path": r.get("poster_path") or "",
            "backdrop_path": r.get("backdrop_path") or "",
            "rating": r.get("vote_average"),
            "genres": ", ".join(g.get("name", "") for g in (r.get("genre_ids") or []) if isinstance(g, dict)),
            "overview": (r.get("overview") or "")[:240],
        })
    return out[:limit]

def cast_and_crew(kind: str, tmdb_id: int) -> dict:
    """Cast+Crew incl. ähnlicher Filme des Hauptdarstellers (Spender-Funktion)."""
    c = credits(kind, tmdb_id) or {"cast": [], "crew": []}
    top_actor = None
    for a in (c.get("cast") or [])[:10]:
        if a.get("known_for_department") == "Acting" and a.get("profile_path"):
            top_actor = a
            break
    cast_films = []
    if top_actor and top_actor.get("id"):
        try:
            kf = known_for(top_actor["id"])
            cast_films = [{"id": 0, "title": x["title"], "year": x["year"], "poster_path": x["poster"]}
                          for x in kf if x["title"]]
        except Exception:
            pass
    return {
        "cast": [{"id": int(a.get("id") or 0), "name": a.get("name") or "",
                  "profile_path": a.get("profile_path") or "",
                  "character": a.get("character") or ""}
                 for a in (c.get("cast") or [])[:10] if a.get("id")],
        "crew": [{"id": int(cr.get("id") or 0), "name": cr.get("name") or "",
                  "job": cr.get("job") or "", "department": cr.get("department") or ""}
                 for cr in (c.get("crew") or []) if cr.get("id")],
        "cast_films": cast_films or [],
    }

def streaming_providers(kind: str, tmdb_id: int) -> list[dict]:
    """Streaming-Anbieter für einen Titel (Wrapper für watch_providers)."""
    providers = watch_providers(kind, tmdb_id)
    result = []
    for p in providers:
        logo = p.get("logo") or ""
        if logo and not logo.startswith("http"):
            logo = f"https://image.tmdb.org/t/p/w92{logo}"
        result.append({"name": p.get("name") or "", "logo": logo,
                       "kind": p.get("kind") or "flatrate"})
    return result


def watch_providers(kind: str, tmdb_id: int, region: str = "DE") -> list[dict]:
    """Streaming-Dienste, die einen Titel anbieten (TMDB watch/providers, DE).
    flatrate (Abonnement-Streaming) zuerst, dann rent/buy; dedupe. Logo = roher Pfad."""
    kind = "movie" if kind in ("movie", None) else "tv"
    d = _get(f"/{kind}/{tmdb_id}/watch/providers", {"watch_region": region})
    res = ((d or {}).get("results") or {}).get(region) or {}
    def _base(n):
        n = (n or "")
        for suf in (" with Ads", " Standard", " Store", " Amazon Channel",
                    " Amazon Video", " Video", " Premium", " Plus"):
            n = n.replace(suf, "")
        return n.strip() or n
    seen = set()
    out = []
    for bucket in ("flatrate", "rent", "buy"):
        for p in res.get(bucket) or []:
            name = p.get("provider_name") or ""
            base = _base(name)
            if not name or base in seen:
                continue
            seen.add(base)
            out.append({"name": base, "logo": p.get("logo_path") or "",
                        "kind": bucket})
    return out


def person_details(pid: int) -> dict | None:
    return _get(f"/person/{pid}", {"language": "de-DE"})


def known_for(pid: int) -> list[dict]:
    d = _get(f"/person/{pid}/combined_credits", {"language": "de-DE"}) or {}
    out = []
    for c in (d.get("cast") or []):
        out.append({"title": c.get("title") or c.get("name") or "", "year": (c.get("release_date") or c.get("first_air_date") or "")[:4],
                    "character": c.get("character") or "", "poster": c.get("poster_path") or ""})
    return out[:20]
