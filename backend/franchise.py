"""Filmreihen-Hubs: Star Wars, 007, Marvel, DC, Disney, Netflix & Co.

Zuordnung ohne ID-Raten: pro Titel holt ein Worker von TMDB die
Produktionsfirmen und die Collection-Zugehoerigkeit (beides steht schon in der
Detail-Antwort). Ein Hub sammelt Titel ueber verifizierte Firmen-IDs ODER
Collection-Namens-Hinweise ("harry potter", "james bond", ...). Was TMDB nicht
liefert, taucht nicht auf — dafuer stimmt was da ist.
"""
from __future__ import annotations

import threading
import time

from . import db, tmdb

# slug -> Hub. companies: verifizierte TMDB-Firmen-IDs (search/company geprueft),
# hints: Kleinbuchstaben-Teilmengen des (deutschen!) Collection-Namens.
HUBS: list[dict] = [
    {"slug": "starwars", "name": "Star Wars", "icon": "⚔",
         "blurb": "Galaxie weit, weit entfernt — Saga, Ableger und Animanien.",
         "companies": [], "hints": ["star wars", "krieg der sterne", "clone wars"],
         "order": [1893, 1894, 1895, 11, 1891, 1892, 140607, 181808, 181812]},
        {"slug": "startrek", "name": "Star Trek", "icon": "🖖",
         "blurb": "Raumschiff Enterprise, Next Generation, Kelvin-Zeitachse — der Weltraum, unendliche Weiten.",
         "companies": [], "hints": ["star trek", "raumschiff enterprise", "next generation", "nächste generation", "kelvin"]},
    {"slug": "nfs", "name": "Need for Speed", "icon": "🏎",
     "blurb": "Vollgas auf der Leinwand — die Rennspiel-Verfilmung und ihre Ableger.",
     "companies": [], "hints": [], "title_hints": ["need for speed"]},
    {"slug": "bond", "name": "James Bond 007", "icon": "🔫",
     "blurb": "Geschüttelt, nicht gerührt — alle Einsätze des MI6.",
     "companies": [7576], "hints": ["james bond", "007"]},
    {"slug": "marvel", "name": "Marvel", "icon": "🦸",
     "blurb": "MCU, Spidey, X-Men und die Wege der Comicgötter.",
     "companies": [420, 13252], "hints":
         ["avengers", "rächern", "thor", "hulk", "iron man", "captain america",
          "spider", "guardians", "galaxie", "deadpool", "logan", "x-men",
          "black panther", "doctor strange", "eternals"]},
    {"slug": "dc", "name": "DC", "icon": "🦇",
     "blurb": "Batman, Superman, Suicide Squad — DCEU und Solo-Pfade.",
     "companies": [128064, 9993], "hints":
         ["batman", "superman", "suicide squad", "selbstmordkommando", "aquaman",
          "justice league", "gerechtigkeitsliga", "wonder woman", "shazam"]},
    {"slug": "disney", "name": "Disney", "icon": "🏰",
     "blurb": "Klassiker, Neuverfilmungen und das große Archiv der Maus.",
     "companies": [2], "hints":
         ["dschungelbuch", "fluch der karibik", "pirates of the caribbean",
          "könig der löwen", "king lion", "toy story", "cars", "cars —",
          "eiskalter held", "die eisprinzessin", "frozen"]},
    {"slug": "pixar", "name": "Pixar", "icon": "🧸",
     "blurb": "Trickfilm-Meilensteine mit Herz und Technik.",
     "companies": [3], "hints": []},
    {"slug": "netflix", "name": "Netflix", "icon": "📺",
     "blurb": "Netflix-Originals, die in deinen Gruppen liegen.",
     "companies": [178464, 198834, 185004], "hints": []},
    {"slug": "potter", "name": "Harry Potter", "icon": "🪄",
     "blurb": "Hogwarts und die Phantastischen Tierwesen.",
     "companies": [], "hints":
         ["harry potter", "tierwesen", "phantastic", "phantastisch"]},
    {"slug": "lotr", "name": "Herr der Ringe", "icon": "💍",
     "blurb": "Mittelerde: die Tolkien-Abenteuer von Peter Jackson.",
     "companies": [], "hints": ["herr der ringe", "hobbit", "lord of the rings"]},
    {"slug": "jurassic", "name": "Jurassic", "icon": "🦖",
     "blurb": "Dinos in Aktion — von Park bis Dominion.",
     "companies": [], "hints": ["jurassic", "dinosaurier"]},
    {"slug": "animation", "name": "Trickfilm-Hits", "icon": "🎨",
     "blurb": "Ice Age, Shrek, Kung Fu Panda & Co. — die Animations-Reihen.",
     "companies": [9383, 521], "hints":
         ["ice age", "eiszeit", "shrek", "kung fu panda", "madagascar", "madagaskar",
          "rio", "drachenzähmen", "how to train", "pingu", "grinch"]},
    {"slug": "fastfurious", "name": "Fast & Furious", "icon": "\U0001f3ce\ufe0f",
     "blurb": "Vollgas: die Fast-&-Furious-Saga, von Tokyo Drift bis Hobbs & Shaw.",
     "companies": [], "hints": ["fast and the furious", "fast & furious"],
     "title_hints": ["fast & furious", "fast and the furious"]},
    {"slug": "johnwick", "name": "John Wick", "icon": "\U0001f52b",
     "blurb": "Der Babayaga: Rache, Continental und die High Table.",
     "companies": [], "hints": ["john wick"]},
    {"slug": "missionimpossible", "name": "Mission: Impossible", "icon": "\U0001f3ac",
     "blurb": "Ethan Hunt und die schier unmoeglichen Missionen.",
     "companies": [], "hints": ["mission impossible", "mission: impossible"],
     "title_hints": ["mission impossible"]},
]
_BY_SLUG = {h["slug"]: h for h in HUBS}


def ensure_schema() -> None:
    conn = db.get_db()
    fcols = [c["name"] for c in conn.execute("PRAGMA table_info(films)")]
    for col, decl in (("coll_id", "INTEGER"), ("coll_name", "TEXT DEFAULT ''"),
                      ("comps", "TEXT DEFAULT ''"), ("enr_at", "REAL DEFAULT 0"),
                      ("total_seasons", "INTEGER DEFAULT 0"), ("total_episodes", "INTEGER DEFAULT 0")):
        if col not in fcols:
            conn.execute(f"ALTER TABLE films ADD COLUMN {col} {decl}")
    conn.commit()


def _store(fid: int, d: dict) -> None:
    coll = d.get("belongs_to_collection") or {}
    comps = [int(c.get("id") or 0) for c in (d.get("production_companies") or []) if c.get("id")]
    db.w("UPDATE films SET coll_id=?, coll_name=?, comps=?, enr_at=? WHERE id=?",
         (int(coll.get("id") or 0) or None, coll.get("name") or "",
          "," + ",".join(map(str, comps)) + "," if comps else "", time.time(), fid))


def enrich_one(fid: int) -> bool:
    f = db.one("SELECT kind, tmdb_id FROM films WHERE id=?", (fid,))
    if not f or not f["tmdb_id"]:
        return False
    d = tmdb.details_full("movie" if f["kind"] == "movie" else "tv", int(f["tmdb_id"]))
    if not d:
        return False
    _store(fid, d)
    return True


_stop = threading.Event()


def _worker():
    while not _stop.is_set():
        try:
            rows = db.q(
                """SELECT id FROM films WHERE tmdb_id IS NOT NULL
                   AND (enr_at=0 OR enr_at IS NULL)
                   AND kind IN ('movie','tv','series') LIMIT 25""")
            if not rows:
                time.sleep(30)
                continue
            for r in rows:
                if _stop.is_set():
                    break
                try:
                    if not enrich_one(int(r["id"])):
                        db.w("UPDATE films SET enr_at=? WHERE id=?", (time.time(), r["id"]))
                except Exception:
                    db.w("UPDATE films SET enr_at=? WHERE id=?", (time.time(), r["id"]))
                time.sleep(0.06)
        except Exception:
            time.sleep(5)


def start_worker():
    ensure_schema()
    threading.Thread(target=_worker, daemon=True, name="franchise").start()


def _match_sql(hub: dict) -> tuple[str, list]:
    """WHERE-Fragment: Filme die zu diesem Hub passen (Firma ODER Collection-Hinweis
    ODER Titel-Hinweis). title_hints fangen Einzel-Franchises ohne TMDB-Collection
    (z.B. 'Need for Speed') ueber den Filmtitel ab."""
    parts, params = [], []
    for cid in hub.get("companies", []):
        parts.append("f.comps LIKE ?")
        params.append(f"%,{cid},%")
    for hint in hub.get("hints", []):
        parts.append("LOWER(COALESCE(f.coll_name,'')) LIKE ?")
        params.append(f"%{hint.lower()}%")
    for th in hub.get("title_hints", []):
        parts.append("LOWER(COALESCE(f.title,'')) LIKE ?")
        params.append(f"%{th.lower()}%")
    return "(" + " OR ".join(parts) + ")", params


def progress() -> dict:
    have = db.one("SELECT COUNT(*) c FROM films WHERE enr_at>0")["c"]
    total = db.one("""SELECT COUNT(*) c FROM films WHERE tmdb_id IS NOT NULL
                      AND kind IN ('movie','tv','series')""")["c"]
    return {"have": have, "total": total}


def all_collections() -> list[dict]:
    """ALLE TMDB-Collections mit >=2 Filmen in gewaehlten Gruppen — dynamisch,
    damit jede Reihe (John Wick, Matrix, Fast & Furious, ...) automatisch
    auftaucht, ohne sie einzeln in HUBS zu pflegen."""
    rows = db.q("""
        SELECT f.coll_id, f.coll_name,
               COUNT(DISTINCT f.id) n,
               SUM(x.sources) quellen
        FROM films f
        JOIN (SELECT m.film_id, COUNT(*) sources FROM media_items m
              JOIN groups g ON g.chat_id=m.chat_id WHERE g.chosen=1
              GROUP BY m.film_id) x ON x.film_id=f.id
        WHERE f.coll_id IS NOT NULL AND f.coll_name != ''
        GROUP BY f.coll_id HAVING n>=2
        ORDER BY n DESC, f.coll_name COLLATE NOCASE""")
    if not rows:
        return []
    # Poster fuer ALLE Collections in EINER Query statt 1 Query pro Collection (N+1-Fix)
    coll_ids = [r["coll_id"] for r in rows]
    ph = ",".join("?" * len(coll_ids))
    poster_rows = db.q(f"""SELECT f.coll_id, f.poster_path FROM films f
              JOIN media_items m ON m.film_id=f.id
              JOIN groups g ON g.chat_id=m.chat_id
              WHERE f.coll_id IN ({ph}) AND g.chosen=1 AND f.poster_path != ''
              GROUP BY f.id ORDER BY COALESCE(f.rating,0) DESC""", coll_ids)
    posters_by_coll: dict = {}
    for pr in poster_rows:
        posters_by_coll.setdefault(pr["coll_id"], []).append(pr["poster_path"])
    out = []
    for r in rows:
        out.append({"slug": f"coll-{r['coll_id']}", "name": r["coll_name"],
                    "icon": "🎞", "blurb": f"{r['n']} Teile in deiner Videothek",
                    "films": r["n"], "sources": r["quellen"] or 0,
                    "posters": posters_by_coll.get(r["coll_id"], [])[:4],
                    "coll_id": r["coll_id"]})
    return out


def _match_film_ids(hub: dict, films: list[dict]) -> list[int]:
    """Film-IDs, die zu einem Hub passen (Firma ODER Collection-Hinweis ODER
    Titel-Hinweis). Statt 13x SQL-LIKE-Scans ueber 30k Filme wird einmal in
    Python gematcht — identische Semantik wie _match_sql()."""
    comps = hub.get("companies", [])
    hints = [x.lower() for x in hub.get("hints", [])]
    thints = [x.lower() for x in hub.get("title_hints", [])]
    out = []
    for f in films:
        if comps:
            c = f["comps"] or ""
            if any(f",{cid}," in c for cid in comps):
                out.append(f["id"])
                continue
        if hints:
            cl = (f["coll_name"] or "").lower()
            if any(x in cl for x in hints):
                out.append(f["id"])
                continue
        if thints:
            ti = (f["title"] or "").lower()
            if any(x in ti for x in thints):
                out.append(f["id"])
    return out


def hubs() -> list[dict]:
    # Alle Filme EINMAL laden und in Python matchen statt pro Hub einen
    # SQL-LIKE-Scan ueber 30k Zeilen (kalt ~8s -> ~1s). Die Quellen-Aggregation
    # (media_items x groups) wird ebenfalls EINMAL geladen statt pro Hub.
    films = db.q("SELECT id, comps, coll_name, title FROM films")
    src = {r["film_id"]: r["sources"] for r in db.q(
        """SELECT m.film_id, COUNT(*) sources FROM media_items m
           JOIN groups g ON g.chat_id=m.chat_id WHERE g.chosen=1
           GROUP BY m.film_id""")}
    out = []
    for h in HUBS:
        ids = _match_film_ids(h, films)
        if not ids:
            continue
        n = 0
        quellen = 0
        for fid in ids:
            s = src.get(fid)
            if s:
                n += 1
                quellen += s
        if n < 2:
            continue
        ph = ",".join("?" * len(ids))
        posters = db.q(
            f"""SELECT f.id, f.title, f.poster_path FROM films f
                JOIN media_items m ON m.film_id=f.id
                JOIN groups g ON g.chat_id=m.chat_id
                WHERE f.id IN ({ph}) AND g.chosen=1 AND f.poster_path != ''
                GROUP BY f.id ORDER BY COALESCE(f.rating,0) DESC LIMIT 4""", ids)
        out.append({"slug": h["slug"], "name": h["name"], "icon": h["icon"],
                    "blurb": h["blurb"], "films": n,
                    "sources": quellen, "posters": posters})
    return out


def hub_detail(slug: str, search: str = "") -> dict | None:
    # Dynamische Collection-Reihe (coll-<id>): direkt ueber die Collection-ID laden.
    if slug.startswith("coll-"):
        try:
            cid = int(slug.split("-", 1)[1])
        except ValueError:
            return None
        like = " AND f.title LIKE ?" if search else ""
        rows = db.q(
            f"""SELECT f.id, f.tmdb_id, f.title, f.year, f.kind, f.genres, f.overview,
                       f.poster_path, f.backdrop_path, f.rating, f.coll_id, f.coll_name, f.trailer,
                       COUNT(m.id) sources, MIN(m.date_utc) first_seen, MAX(m.date_utc) last_seen
                FROM films f JOIN media_items m ON m.film_id=f.id
                JOIN groups g ON g.chat_id=m.chat_id
                WHERE f.coll_id=? AND g.chosen=1{like}
                GROUP BY f.id ORDER BY f.year""",
            [cid] + ([f"%{search}%"] if search else []))
        if not rows:
            return None
        name = rows[0]["coll_name"] or "Filmreihe"
        return {"hub": {"slug": slug, "name": name, "icon": "🎞",
                        "blurb": f"{len(rows)} Teile in deiner Videothek"},
                "series": [{"collection": cid, "name": name, "films": rows}],
                "single": [], "total": len(rows)}
    h = _BY_SLUG.get(slug)
    if not h:
        return None
    where, params = _match_sql(h)
    like = " AND f.title LIKE ?" if search else ""
    rows = db.q(
        f"""SELECT f.id, f.tmdb_id, f.title, f.year, f.kind, f.genres, f.overview,
                   f.poster_path, f.backdrop_path, f.rating, f.coll_id, f.coll_name, f.trailer,
                   COUNT(m.id) sources, MIN(m.date_utc) first_seen, MAX(m.date_utc) last_seen
            FROM films f JOIN media_items m ON m.film_id=f.id
            JOIN groups g ON g.chat_id=m.chat_id
            WHERE {where} AND g.chosen=1{like}
            GROUP BY f.id ORDER BY f.year""", params + [f"%{search}%"] if search else params)
    series, single = {}, []
    for x in rows:
        if x["coll_id"]:
            s = series.setdefault(int(x["coll_id"]),
                                  {"collection": int(x["coll_id"]), "name": x["coll_name"], "films": []})
            s["films"].append(x)
        else:
            single.append(x)
    # Seh-Reihenfolge: pro Hub definierte "order" (tmdb_id-Liste) schlaegt Release-Jahr,
    # damit Neulinge die Saga in der beabsichtigten Story-Reihenfolge sehen (z.B. Star Wars 1-9).
    ordr = {int(t): i for i, t in enumerate(h.get("order") or [])}
    def _rank(f):
        tid = int(f["tmdb_id"]) if f["tmdb_id"] else -1
        return (ordr.get(tid, 999999), f["year"] or 9999)
    ordered = sorted(series.values(),
                     key=lambda s: min(_rank(f)[0] for f in s["films"]))
    for s in ordered:
        s["films"].sort(key=_rank)
    single.sort(key=lambda f: (f["year"] or 9999))
    return {"hub": {k: h[k] for k in ("slug", "name", "icon", "blurb")},
            "series": ordered, "single": single, "total": len(rows)}
