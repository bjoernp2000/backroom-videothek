"""Curation: Schauspieler-Credits, Serien-Staffeln, Klassiker.

Cast/Crew kommen aus TMDB und liegen in eigenen Tabellen, damit die
Schauspieler-Seite ohne Netz schnell bleibt. Ein Hintergrund-Worker fuellt
die Credits fuer alle filme mit tmdb_id nach (kleine Chargen, TVQuadrat-freundlich).
"""
from __future__ import annotations

import threading
import time

from . import db, tmdb


def ensure_schema() -> None:
    conn = db.get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS people(
        id INTEGER PRIMARY KEY,             -- tmdb person id
        name TEXT, profile_path TEXT, bio TEXT DEFAULT '', birthday TEXT, place TEXT,
        also_known TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS film_people(
        film_id INTEGER REFERENCES films(id),
        person_id INTEGER,
        role TEXT,                          -- 'cast' | 'director' | 'writer' | 'producer'
        character TEXT DEFAULT '',
        ord INTEGER DEFAULT 0,
        PRIMARY KEY(film_id, person_id, role)
    );
    CREATE INDEX IF NOT EXISTS idx_fp_person ON film_people(person_id);
    CREATE INDEX IF NOT EXISTS idx_fp_film ON film_people(film_id);
    -- attempt-Log: Titel ohne TMDB-Credits nicht endlos wiederversuchen
    CREATE TABLE IF NOT EXISTS credit_state(
        film_id INTEGER PRIMARY KEY REFERENCES films(id), tried_at REAL
    );
    -- Folgenamen + -beschreibungen je Staffel (TMDB), dauerhaft gecacht
    CREATE TABLE IF NOT EXISTS episodes(
        film_id INTEGER REFERENCES films(id), season INTEGER, episode INTEGER,
        name TEXT DEFAULT '', overview TEXT DEFAULT '', still TEXT DEFAULT '',
        runtime INTEGER DEFAULT 0, air TEXT DEFAULT '',
        PRIMARY KEY(film_id, season, episode)
    );
    CREATE TABLE IF NOT EXISTS season_state(
            film_id INTEGER, season INTEGER, tried_at REAL,
            PRIMARY KEY(film_id, season)
        );
        -- Geburtstags-Nachzug: pro Person ein Versuch, gegen Endlos-Wiederholen
        CREATE TABLE IF NOT EXISTS bday_state(
            person_id INTEGER PRIMARY KEY REFERENCES people(id), tried_at REAL
        );
        """)
    conn.commit()


def _store_credits(film_id: int, kind: str, tmdb_id: int, conn=None) -> int:
    """Holt Cast+Crew von TMDB in die lokalen Tabellen. 0 = nichts verwertet.

    Mit conn (offene Transaktion) werden alle Writes gebatcht und erst vom
    Aufrufer committet; ohne conn wird jede Write einzeln committet (db.w).
    """
    if conn is not None:
        def _w(sql, params): conn.execute(sql, params)
    else:
        def _w(sql, params): db.w(sql, params)
    d = tmdb.credits(kind, tmdb_id)
    if not d:
        _w("INSERT OR REPLACE INTO credit_state(film_id,tried_at) VALUES(?,?)",
           (film_id, time.time()))
        return 0
    n = 0
    for i, c in enumerate((d.get("cast") or [])[:15]):
        pid = int(c.get("id") or 0)
        if not pid:
            continue
        _w("INSERT OR IGNORE INTO people(id,name,profile_path) VALUES(?,?,?)",
           (pid, c.get("name") or "", c.get("profile_path") or ""))
        _w("INSERT OR REPLACE INTO film_people(film_id,person_id,role,character,ord) VALUES(?,?,?,?,?)",
           (film_id, pid, "cast", c.get("character") or "", i))
        n += 1
    for crew in (d.get("crew") or []):
        dept = (crew.get("department") or "").lower()
        job = (crew.get("job") or "").lower()
        role = None
        if dept == "directing" and job == "director":
            role = "director"
        elif dept == "writing" and job in ("writer", "screenplay", "story", "novel", "characters"):
            role = "writer"
        elif dept == "production" and job in ("producer", "executive producer"):
            role = "producer"
        pid = int(crew.get("id") or 0)
        if role and pid:
            _w("INSERT OR IGNORE INTO people(id,name,profile_path) VALUES(?,?,?)",
               (pid, crew.get("name") or "", crew.get("profile_path") or ""))
            _w("INSERT OR REPLACE INTO film_people(film_id,person_id,role,character,ord) VALUES(?,?,?,?,?)",
               (film_id, pid, role, "", 0))
            n += 1
    if n == 0:   # Credits abgefragt, nichts Brauchbares -> nicht wiederkommen
        _w("INSERT OR REPLACE INTO credit_state(film_id,tried_at) VALUES(?,?)",
           (film_id, time.time()))
    return n


_credits_stop = threading.Event()


def _credits_worker():
    """Nachziehen, bis alles erfasst ist; dann schlafen bis neuer Bedarf."""
    while not _credits_stop.is_set():
        try:
            rows = db.q(
                """SELECT f.id, f.kind, f.tmdb_id FROM films f
                  WHERE f.tmdb_id IS NOT NULL AND f.kind IN ('movie','tv','series')
                    AND NOT EXISTS(SELECT 1 FROM film_people p WHERE p.film_id=f.id)
                    AND NOT EXISTS(SELECT 1 FROM credit_state cs
                                    WHERE cs.film_id=f.id AND cs.tried_at>?)
                  LIMIT 15""", (time.time() - 3 * 86400,))
            # TMDB-Calls OHNE offene Write-Transaktion: erst alle Netzwerk-Abfragen
            # sammeln, die Writes danach gebatcht in EINER Transaktion committen.
            # So haelt der Worker keine Write-Lock ueber die ~4.5s Netzwerkzeit,
            # die sonst alle anderen DB-Zugriffe im Server blockiert/verlangsamt.
            conn = db.get_db()
            for r in rows:
                if _credits_stop.is_set():
                    break
                try:
                    _store_credits(int(r["id"]), "movie" if r["kind"] == "movie" else "tv",
                                   int(r["tmdb_id"]), conn)
                except Exception:
                    pass
                time.sleep(0.2)   # TMDB-Takt schonen
            conn.commit()   # alle Writes des 15er-Batches in EINER Transaktion
            # Biografien fuer die popularsten Personen nachziehen (fuer die Seite)
            bios = db.q(
                """SELECT pe.id, COUNT(*) n FROM people pe JOIN film_people fp ON fp.person_id=pe.id
                   WHERE pe.bio='' GROUP BY pe.id ORDER BY n DESC LIMIT 3""")
            for b in bios:
                if _credits_stop.is_set():
                    break
                person_bio(int(b["id"]))
                time.sleep(0.15)
            if not rows and not bios:
                time.sleep(60)
        except Exception:
            time.sleep(5)


def start_credits_worker():
    ensure_schema()
    t = threading.Thread(target=_credits_worker, daemon=True, name="credits")
    t.start()


def stop_credits_worker():
    _credits_stop.set()


# ---------- Geburtstage: dauerhaft aus TMDB nachladen ----------

_bday_stop = threading.Event()


def ensure_birthdays(person_id: int) -> bool:
    """Geburtstag/Geburtsort einer Person nachholen und dauerhaft speichern.
    0.5s-Drossel gegen den TMDB-Zugriff; kehrt nie wg. Einzelner zurueck."""
    d = tmdb.person_details(person_id)
    if not d:
        db.w("INSERT OR REPLACE INTO bday_state(person_id,tried_at) VALUES(?,?)",
             (person_id, time.time()))
        return False
    db.w("UPDATE people SET birthday=?, place=? WHERE id=?",
         (d.get("birthday") or "", d.get("birthplace") or "", person_id))
    db.w("INSERT OR REPLACE INTO bday_state(person_id,tried_at) VALUES(?,?)",
         (person_id, time.time()))
    return bool(d.get("birthday"))


def _bday_worker():
    while not _bday_stop.is_set():
        try:
            # Beliebteste Personen OHNE Geburtstag, noch nicht vergeblich versucht.
            rows = db.q("""
                SELECT pe.id, pe.name,
                       (SELECT COUNT(*) FROM film_people fp WHERE fp.person_id=pe.id) AS n
                FROM people pe
                WHERE (pe.birthday IS NULL OR pe.birthday='')
                  AND NOT EXISTS(SELECT 1 FROM bday_state bs WHERE bs.person_id=pe.id)
                  AND EXISTS(SELECT 1 FROM film_people fp WHERE fp.person_id=pe.id)
                HAVING n>=1
                ORDER BY n DESC LIMIT 10""")
            if not rows:
                return  # alles mit Geburtstag befuehlt oder versucht -> fertig
            conn = db.get_db()
            for row in rows:
                if _bday_stop.is_set():
                    return
                try:
                    # TMDB-Call OHNE offene Transaktion (conn erst fuer die Writes nutzen)
                    d = tmdb.person_details(int(row["id"]))
                    if d:
                        conn.execute("UPDATE people SET birthday=?, place=? WHERE id=?",
                                     (d.get("birthday") or "", d.get("birthplace") or "", row["id"]))
                    conn.execute("INSERT OR REPLACE INTO bday_state(person_id,tried_at) VALUES(?,?)",
                                 (row["id"], time.time()))
                except Exception:
                    pass
                time.sleep(1.0)
            conn.commit()   # alle Writes des 10er-Batches in EINER Transaktion
        except Exception:
            time.sleep(5)
        time.sleep(2)


def start_bday_worker():
    ensure_schema()
    t = threading.Thread(target=_bday_worker, daemon=True, name="bday")
    t.start()


# ---------------- Auswertungen (fuer die Seiten) ----------------

def _chosen_ids() -> list[int]:
    return [int(x["id"]) for x in db.q(
        "SELECT f.id FROM films f JOIN media_items m ON m.film_id=f.id "
        "JOIN groups g ON g.chat_id=m.chat_id WHERE g.chosen=1 GROUP BY f.id")]


def ensure_credits(film_id: int) -> None:
    """Cast/Crew fuer einen Titel sofort nachholen, falls noch keine da sind."""
    have = db.one("SELECT COUNT(*) c FROM film_people WHERE film_id=?", (film_id,))
    if have and have["c"]:
        return
    st = db.one("SELECT tried_at FROM credit_state WHERE film_id=?", (film_id,))
    if st and time.time() - (st["tried_at"] or 0) < 86400:   # heute schon vergeblich
        return
    f = db.one("SELECT kind, tmdb_id FROM films WHERE id=?", (film_id,))
    if not f or not f["tmdb_id"]:
        return
    try:
        _store_credits(film_id, "movie" if f["kind"] == "movie" else "tv", int(f["tmdb_id"]))
    except Exception:
        pass


def series_list(search: str = "") -> list[dict]:
    """Serien (tv|series) mit Episoden-Zaehlung aus gewaehlten Gruppen, nach Neueste."""
    like = " AND (f.title LIKE ? OR f.original_title LIKE ?)" if search else ""
    params = (f"%{search}%", f"%{search}%") if search else ()
    rows = db.q(
        f"""SELECT f.id, f.tmdb_id, f.title, f.year, f.genres, f.overview, f.poster_path,
                   f.backdrop_path, f.rating, f.trailer,
                   COUNT(m.id) eps, COUNT(DISTINCT m.season) seasons,
                   MAX(m.date_utc) latest
            FROM films f JOIN media_items m ON m.film_id=f.id
            JOIN groups g ON g.chat_id=m.chat_id
            WHERE (f.kind='tv' OR f.kind='series') AND g.chosen=1{like}
            GROUP BY f.id ORDER BY latest DESC""", params)
    return [dict(r) for r in rows]


def series_detail(film_id: int) -> dict | None:
    f = db.one("SELECT * FROM films WHERE id=?", (film_id,))
    if not f:
        return None
    eps = db.q(
        """SELECT m.id, m.season, m.episode, m.filename, m.caption, m.parsed_title,
                  m.duration, m.size_bytes, m.status, m.date_utc, m.chat_title, g.chosen
          FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
          WHERE m.film_id=? ORDER BY m.season, m.episode""", (film_id,))
    seasons: dict[int, list] = {}
    for e in eps:
        if not e.get("chosen"):
            continue
        seasons.setdefault(int(e["season"] or 0), []).append(e)
    _fill_episode_meta(film_id, f, sorted(seasons))
    out = []
    for k, v in sorted(seasons.items()):
        # Mehrere Dateien derselben Folge (= mehrere Quellen/Gruppen) werden zu
        # EINEM Eintrag gruppiert. Primaer ist die groesste Datei (meist beste
        # Aufloesung); die restlichen Quellen liegen mit Groesse in e["src"].
        groups: dict[int, list] = {}
        for e in v:
            groups.setdefault(int(e["episode"] or 0), []).append(e)
        episodes = []
        for ep, srcs in sorted(groups.items()):
            srcs.sort(key=lambda x: (x["size_bytes"] or 0), reverse=True)
            p = srcs[0]  # groesste = primaere Quelle
            m = db.one("SELECT name, overview, still, runtime, air FROM episodes "
                       "WHERE film_id=? AND season=? AND episode=?",
                       (film_id, k, ep))
            if m:
                p["ep_name"], p["ep_overview"] = m["name"], m["overview"]
                p["ep_still"], p["ep_air"] = m["still"], m["air"]
                if not p.get("duration") and m["runtime"]:
                    p["duration"] = int(m["runtime"]) * 60
            p["src"] = [{
                "id": _s["id"], "filename": _s["filename"],
                "size_bytes": _s["size_bytes"], "status": _s.get("status"),
                "chat_title": _s["chat_title"], "date_utc": _s["date_utc"],
            } for _s in srcs]
            p["n_src"] = len(srcs)
            p["owned"] = True
            episodes.append(p)
        # Erschienene, aber nicht lokal vorhandene Folgen dieser Staffel mit anzeigen
        # (sichtbar, nicht abspielbar), damit Nutzer sieht, was fehlt.
        _all = db.q("SELECT episode, name, overview, still, air FROM episodes "
                    "WHERE film_id=? AND season=?", (film_id, k))
        if _all:
            _owned = {int(x.get("episode") or 0) for x in episodes}
            for _me in _all:
                _en = int(_me["episode"] or 0)
                if _en and _en not in _owned:
                    episodes.append({"id": 0, "season": k, "episode": _en,
                                     "ep_name": _me["name"], "ep_overview": _me["overview"],
                                     "ep_still": _me["still"], "ep_air": _me["air"],
                                     "owned": False, "missing": True})
            episodes.sort(key=lambda x: (x.get("episode") or 0))
        out.append({"season": k, "episodes": episodes})
    # Gesamtzahl der Staffeln/Episoden der Serie aus TMDB (in films gecacht), damit
    # das Frontend z.B. 'Staffel 5 von 6' statt irrefuehrend '1 Staffel' zeigt.
    total_seasons = int(f.get("total_seasons") or 0)
    total_eps = int(f.get("total_episodes") or 0)
    if not total_seasons and f.get("tmdb_id") and f["kind"] in ("tv", "series"):
        try:
            det = tmdb.details_full("tv", int(f["tmdb_id"]))
            if det:
                total_seasons = int(det.get("number_of_seasons") or 0)
                total_eps = int(det.get("number_of_episodes") or 0)
                try:
                    db.w("UPDATE films SET total_seasons=?, total_episodes=? WHERE id=?",
                         (total_seasons, total_eps, film_id))
                    f["total_seasons"], f["total_episodes"] = total_seasons, total_eps
                except Exception:
                    pass
        except Exception:
            pass
    # Staffeln der Serie, die lokal gar keine Folgen haben, als 'fehlt komplett' anzeigen,
    # damit der Nutzer sieht, dass es sie gibt (aber nicht abspielbar sind).
    if total_seasons and f.get("tmdb_id") and f["kind"] in ("tv", "series"):
        owned_seasons = {s["season"] for s in out}
        tid = int(f["tmdb_id"])
        for sn in range(1, total_seasons + 1):
            if sn in owned_seasons:
                continue
            try:
                _fill_episode_meta(film_id, f, [sn])
                meps = db.q("SELECT episode, name, overview, still, air FROM episodes "
                            "WHERE film_id=? AND season=?", (film_id, sn))
                eps = [{"id": 0, "season": sn, "episode": int(m["episode"] or 0),
                        "ep_name": m["name"], "ep_overview": m["overview"],
                        "ep_still": m["still"], "ep_air": m["air"],
                        "owned": False, "missing": True}
                       for m in meps if m["episode"]]
                if eps:
                    out.append({"season": sn, "episodes": sorted(eps, key=lambda x: x["episode"])})
            except Exception:
                pass
        out.sort(key=lambda s: s["season"])
    if f.get("tmdb_id") and not (f.get("fsk") or "").strip():
        try:
            _f = tmdb.fsk(f["kind"], int(f["tmdb_id"]))
            if _f:
                db.w("UPDATE films SET fsk=? WHERE id=?", (_f, film_id))
                f["fsk"] = _f
        except Exception:
            pass
    return {"film": dict(f), "seasons": out,
            "total_seasons": total_seasons, "total_episodes": total_eps}


def _fill_episode_meta(film_id: int, film: dict, wanted: list[int]) -> None:
    """Staffel-Metadaten (Folgennamen/-beschreibung) aus TMDB in episodes-Tabelle.

    Pro Staffel genau EIN Versuch (season_state), damit tote Episodennummern
    nicht bei jedem Oeffnen erneut gequetscht werden. Neue Staffeln (fehlende
    Nummer in wanted) werden nachgeladen, wenn sie noch nie probiert wurden.
    """
    if not film.get("tmdb_id"):
        return
    ensure_schema()
    tmdb_id = int(film["tmdb_id"])
    have = {int(r["season"]) for r in db.q(
        "SELECT DISTINCT season FROM episodes WHERE film_id=?", (film_id,))}
    tried = {int(r["season"]): (r["tried_at"] or 0) for r in db.q(
        "SELECT season, tried_at FROM season_state WHERE film_id=?", (film_id,))}
    for s in wanted:
        if s in have or (s in tried and time.time() - tried[s] < 3 * 86400) or s <= 0:
            continue
        db.w("INSERT OR REPLACE INTO season_state(film_id,season,tried_at) VALUES(?,?,?)",
             (film_id, s, time.time()))
        d = tmdb.season(tmdb_id, s)
        if not d:
            continue
        rows = [(film_id, s, int(ep.get("episode_number") or 0),
                 ep.get("name") or "", (ep.get("overview") or "").strip(),
                 ep.get("still_path") or "", int(ep.get("runtime") or 0),
                 ep.get("air_date") or "") for ep in d["episodes"]]
        for r in rows:
            db.w("""INSERT OR REPLACE INTO episodes(film_id,season,episode,name,overview,still,runtime,air)
                    VALUES(?,?,?,?,?,?,?,?)""", r)


def classics(page: int = 0, size: int = 60, search: str = "") -> dict:
    """Vor 2000 erschienen, gut bewertet, in gewaehlten Gruppen — nach Rating/Jahr."""
    like = " AND f.title LIKE ?" if search else ""
    rows = db.q(
        f"""SELECT f.id, f.tmdb_id, f.title, f.year, f.genres, f.rating, f.poster_path,
                   f.backdrop_path, f.overview, f.trailer, COUNT(m.id) cnt
            FROM films f JOIN media_items m ON m.film_id=f.id
            JOIN groups g ON g.chat_id=m.chat_id
            WHERE f.kind='movie' AND f.year BETWEEN 1900 AND 1999 AND g.chosen=1{like}
            GROUP BY f.id ORDER BY COALESCE(f.rating,0) DESC, f.year DESC LIMIT ?""",
        (f"%{search}%", size + 1) if search else (size + 1,))
    return {"items": rows[:size], "more": len(rows) > size}


def people_top(limit: int = 90) -> list[dict]:
    """Schauspieler mit den meisten Filmen in der eigenen Bibliothek."""
    return db.q(
        """SELECT pe.id, pe.name, pe.profile_path, COUNT(*) n
           FROM film_people fp JOIN people pe ON pe.id=fp.person_id
           WHERE fp.role='cast'
           GROUP BY pe.id ORDER BY n DESC LIMIT ?""", (limit,))


def person(pid: int) -> dict | None:
    p = db.one("SELECT * FROM people WHERE id=?", (pid,))
    if not p:
        return None
    films = db.q(
        """SELECT f.id, f.title, f.year, f.kind, f.poster_path, f.rating, f.overview,
                  fp.character, fp.role
           FROM film_people fp JOIN films f ON f.id=fp.film_id
           WHERE fp.person_id=?
             AND EXISTS(SELECT 1 FROM media_items m WHERE m.film_id=f.id
                        AND m.chat_id IN (SELECT chat_id FROM groups WHERE chosen=1))
           ORDER BY COALESCE(f.year, 9999) ASC""", (pid,))
    return {"person": dict(p), "films": [dict(x) for x in films]}


def person_bio(pid: int) -> bool:
    """Holt Person-Details (Bio, Profilbild) nach, wenn noch leer."""
    p = db.one("SELECT bio, profile_path FROM people WHERE id=?", (pid,))
    if p is None:
        return False
    if p["bio"] and p["profile_path"]:
        return True
    d = tmdb.person_details(pid)
    if not d:
        return False
    db.w("UPDATE people SET bio=?, profile_path=COALESCE(NULLIF(?, ''), profile_path),"
         "birthday=?, place=? WHERE id=?",
         (d.get("biography") or "", d.get("profile_path") or "", d.get("birthday") or "",
          d.get("birthplace") or "", pid))
    return True

def search_people(role: str = "cast", q: str = "") -> list[dict]:
    """Sucht Personen nach Rolle und optionalem Suchbegriff (für Filter-Akordeon)."""
    like = " AND pe.name LIKE ?" if q else ""
    params = (f"%{q}%",) if q else ()
    role_filter = "cast" if role == "cast" else "director" if role == "director" else "writer"
    rows = db.q(
        """SELECT pe.id, pe.name, pe.profile_path, COUNT(DISTINCT f.id) AS film_count
           FROM film_people fp JOIN people pe ON pe.id=fp.person_id
           JOIN films f ON f.id=fp.film_id
           WHERE fp.role=? AND f.kind IN ('movie','tv','series')
             AND EXISTS(SELECT 1 FROM media_items m WHERE m.film_id=f.id
                        AND m.chat_id IN (SELECT chat_id FROM groups WHERE chosen=1))
             """ + like + """
           GROUP BY pe.id
           ORDER BY CASE WHEN pe.name LIKE ? THEN 0 ELSE 1 END, film_count DESC
           LIMIT 50""",
        (role_filter, role_filter) + params + (f"%{q}%",) if q else (role_filter, role_filter))
    return [{"id": int(r["id"]), "name": r["name"], "profile_path": r["profile_path"],
             "films": r["film_count"]} for r in rows]

def fill_all_tabs() -> dict:
    """Füllt alle Genre-Tabs mit neuen Filmen (wird beim 'Alle Reiter befüllen' genutzt)."""
    from .scanner import start_fill_tabs
    try:
        start_fill_tabs()
        return {"ok": True, "note": "Scan gestartet — alle Reiter werden befüllt"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def fill_genre_tab(genre: str) -> dict:
    """Füllt einen bestimmten Genre-Tab (wird beim Genre-Scan genutzt)."""
    from .scanner import start_fill_tab
    try:
        start_fill_tab(genre)
        return {"ok": True, "note": f"Genre-Tab '{genre}' wird befüllt"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
