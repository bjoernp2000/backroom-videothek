"""Flask-API der Videothek."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from . import cache, config, curation, db, franchise, scanner, stream, telegram, tmdb as tmdbapi, sports, kiosk, music, tv
from .videothek_component import create_videothek_component
from .fsk_scan import fsk_manager, scan_manager, init_fsk_scan_system
from .settings_manager import settings_manager, init_settings_system

_hubs_cache = {"t": 0.0, "data": None}
_classics_cache: dict = {}
_series_cache: dict = {}
_people_cache = {"t": 0.0, "data": None}
_progress_cache = {"t": 0.0, "data": None}
_films_cache: dict = {}
_avgsrc_cache = {"t": 0.0, "v": 0.0}
_groups_cache = {"t": 0.0, "data": None}
_charts_cache: dict = {}
_bdays_cache: dict = {}
_dl_cache = {"t": 0.0, "data": None}
_facets_cache = {"t": 0.0, "data": None}
_peoplef_cache: dict = {}


_EXT_MIME = {".mkv": "video/x-matroska", ".mp4": "video/mp4", ".webm": "video/webm",
             ".avi": "video/x-msvideo", ".mov": "video/quicktime", ".m4v": "video/mp4",
             ".ts": "video/mp2t"}


def _mime(mi: dict) -> str:
    m = (mi.get("mime") or "").lower()
    if m.startswith("video/") and "mp4" not in m:
        return m
    ext = Path(mi.get("filename") or "").suffix.lower()
    return _EXT_MIME.get(ext, "video/mp4")


def _media_in_chosen(mid: int) -> bool:
    """Darf ein media_item ueberhaupt angespielt werden? Nur wenn seine Quelle
    in einer der vom User GEWAEHLTEN Gruppen liegt. Der Player zeigt IMMER nur
    die ausgewaehlten Gruppen — eine abgewaehlte Gruppe wird nirgendwo mehr
    abgespielt, auch nicht ueber eine direkt bekannte media_id."""
    mi = db.one("SELECT chat_id FROM media_items WHERE id=?", (mid,))
    if not mi:
        return False
    return bool(db.one("SELECT 1 FROM groups WHERE chat_id=? AND chosen=1", (mi["chat_id"],)))


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(config.FRONTEND), static_url_path="")
    db.init_db()
    cache.start_sweeper()    # fluichtige Wiedergabe-Caches nach Idle-Timeout loeschen
    cache.start_watchdog()   # haengenden Head-Loader automatisch neu starten
    scanner.resume_scans()   # laufende/queuende Scans nach Neustart fortsetzen
    scanner.resume_pending() # Downloads aus Warteschlange/.part neu aufnehmen
    scanner.start_pump()     # Fehler selbstlaeufig erneut versuchen
    scanner.start_live_sync(60)  # gewählte Telegram-Quelle regelmäßig nachziehen
    scanner.start_matcher()
    curation.start_credits_worker()   # Cast/Crew fuer Schauspieler-Seite nachziehen
    curation.start_bday_worker()      # Geburtstage dauerhaft aus TMDB nachladen
    franchise.start_worker()          # Collections/Firmen fuer Reihen-Hubs anreichern
    
    # Initialisiere Videothek-Komponente
    create_videothek_component(app)
    
    # Initialisiere FSK- und Scan-System
    init_fsk_scan_system()
    
    # Initialisiere Einstellungssystem
    init_settings_system()

    @app.get("/")
    def index():
        resp = send_from_directory(app.static_folder, "index.html")
        resp.headers["Cache-Control"] = "no-store"   # nie ein uraltes JS ausliefern
        return resp

    @app.get("/posters/<path:name>")
    def poster(name):
        return send_from_directory(str(config.POSTER_DIR), name)

    # ---------- Setup / Gruppen ----------
    @app.get("/api/status")
    def status():
        counts = db.one(
            """SELECT
                      (SELECT COUNT(DISTINCT m.film_id) FROM media_items m
                       JOIN groups g ON g.chat_id=m.chat_id
                       WHERE g.chosen=1 AND m.film_id IS NOT NULL) AS films,
                      (SELECT COUNT(*) FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
                       WHERE g.chosen=1) AS items,
                      (SELECT COUNT(*) FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
                       WHERE g.chosen=1 AND m.status='unmatched') AS unmatched,
                      (SELECT COUNT(*) FROM groups WHERE chosen=1) AS chosen,
                      (SELECT COUNT(*) FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
                       WHERE g.chosen=1 AND s.status IN ('queued','running')) AS scanning,
                      (SELECT COUNT(*) FROM downloads d JOIN media_items m ON m.id=d.media_id
                       JOIN groups g ON g.chat_id=m.chat_id
                       WHERE g.chosen=1 AND d.status='running') AS downloading""")
        return jsonify({
            "telegram": config.TG_OK, "tmdb": config.TMDB_OK,
            "tg_user": (config.CFG.get("tg") or {}).get("owner_username") or "",
            "download_dir": str(config.DOWNLOAD_DIR), **counts,
        })

    @app.get("/api/sports/wrestling")
    def sports_wrestling():
        try:
            limit = max(1, min(int(request.args.get("limit", 120)), 300))
        except (TypeError, ValueError):
            limit = 120
        return jsonify(sports.wrestling(limit))

    @app.get("/api/music")
    def music_catalog():
        source = request.args.get("source", "both")
        try:
            limit = max(1, min(int(request.args.get("limit", 48)), 100))
        except (TypeError, ValueError):
            limit = 48
        return jsonify(music.catalog(source, limit))
    @app.get("/api/tv/sources")
    def tv_sources():
        return jsonify({"ok": True, "sources": tv.sources()})

    @app.post("/api/tv/sources")
    def tv_source_add():
        data = request.get_json(force=True) or {}
        try:
            return jsonify({"ok": True, "source": tv.add_source(data.get("name", ""), data.get("playlist_url", ""), data.get("epg_url", ""))})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.delete("/api/tv/sources/<source_id>")
    def tv_source_delete(source_id):
        return jsonify({"ok": tv.remove_source(source_id)})

    @app.get("/api/tv/channels")
    def tv_channels():
        try:
            limit = max(1, min(int(request.args.get("limit", 500)), 1000))
        except (TypeError, ValueError):
            limit = 500
        return jsonify(tv.channels(limit))

    @app.get("/api/tv/epg")
    def tv_epg():
        return jsonify(tv.epg(request.args.get("channel_id", ""), request.args.get("source_id", "")))

    @app.post("/api/tv/play")
    def tv_play():
        data = request.get_json(force=True) or {}
        url = (data.get("stream_url") or "").strip()
        title = (data.get("title") or "TV").strip()[:180]
        if not tv._valid_url(url):
            return jsonify({"ok": False, "error": "Ungültige Stream-Adresse"}), 400
        try:
            from . import player
            player.play_video(url, title)
            return jsonify({"ok": True, "title": title})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"TV-Player konnte nicht gestartet werden: {exc}"}), 500

    @app.get("/api/kiosk")
    def kiosk_catalog():
        try:
            limit = max(1, min(int(request.args.get("limit", 48)), 200))
        except (TypeError, ValueError):
            limit = 48
        return jsonify(kiosk.catalog(limit))



    @app.get("/api/groups")
    def groups():
        now = time.time()
        if _groups_cache["data"] is not None and now - _groups_cache["t"] < 10:
            return _groups_cache["data"]
        _groups_cache["data"] = jsonify({"groups": db.q("SELECT * FROM groups ORDER BY title COLLATE NOCASE"),
                                         "telegram": config.TG_OK})
        _groups_cache["t"] = now
        return _groups_cache["data"]

    @app.post("/api/groups/refresh")
    def groups_refresh():
        try:
            rows = telegram.list_groups()
            return jsonify({"ok": True, "groups": len(rows)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 502

    @app.post("/api/groups/choose")
    def groups_choose():
        data = request.get_json(force=True)
        conn = db.get_db()
        conn.execute("UPDATE groups SET chosen=0")
        for cid in data.get("chat_ids", []):
            conn.execute("UPDATE groups SET chosen=1 WHERE chat_id=?", (int(cid),))
        conn.commit()
        after = {int(c) for c in data.get("chat_ids", [])}
        # Bei jeder Auswahländerung alle gewählten Gruppen ab der zuletzt
        # gespeicherten Telegram-Nachricht prüfen. So ist die Anzeige sofort
        # auf dem aktuellen Stand und nicht nur bei einer völlig neuen Gruppe.
        queued = scanner.start_scan(sorted(after), force=True) if after else {"queued": 0}
        return jsonify({"ok": True, "auto_scanned": int(queued.get("queued", 0)),
                        "chosen": len(after)})

    @app.post("/api/import/cc")
    def import_cc():
        from . import ccimport
        try:
            return jsonify({"ok": True, **ccimport.run_import()})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:250]}), 500

    @app.post("/api/scan")
    def scan():
        data = request.get_json(silent=True) or {}
        ids = data.get("chat_ids")
        force = bool(data.get("force"))
        if ids is not None:
            ids = [int(i) for i in ids]
        return jsonify(scanner.start_scan(ids, force=force))

    @app.post("/api/match/queue")
    def queue_match():
        """Stellt einen begrenzten Bestand alter indexed-Medien in die TMDB-Warteschlange."""
        data = request.get_json(silent=True) or {}
        try:
            limit = int(data.get("limit", 500))
            chat_ids = data.get("chat_ids")
            if chat_ids is not None:
                chat_ids = [int(cid) for cid in chat_ids]
            return jsonify({"ok": True, **scanner.queue_indexed(limit, chat_ids)})
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/review/reachability")
    def review_reachability():
        data = request.get_json(silent=True) or {}
        try:
            return jsonify({"ok": True, **scanner.check_reachability(int(data.get("limit", 25)), bool(data.get("force")))})
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/scan/state")
    def scan_state():
        return jsonify({"states": db.q("SELECT * FROM scan_state ORDER BY title COLLATE NOCASE")})

    @app.get("/api/progress")
    def progress():
        import time as _t
        now = _t.time()
        if _progress_cache["data"] is not None and now - _progress_cache["t"] < 2:
            return _progress_cache["data"]
        agg = db.one(
            """SELECT
                 (SELECT COUNT(*) FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
                  WHERE g.chosen=1 AND s.status='done') AS done,
                 (SELECT COUNT(*) FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
                  WHERE g.chosen=1 AND s.status='queued') AS queued,
                 (SELECT COUNT(*) FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
                  WHERE g.chosen=1 AND s.status='running') AS running,
                 (SELECT COUNT(*) FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
                  WHERE g.chosen=1 AND s.status='error') AS errored,
                 (SELECT COUNT(*) FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
                  WHERE g.chosen=1 AND s.status='skipped') AS skipped,
                 (SELECT COUNT(*) FROM groups WHERE chosen=1) AS chosen,
                 (SELECT COUNT(*) FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
                  WHERE g.chosen=1 AND m.film_id IS NULL) AS tmdb_wait,
                 (SELECT COUNT(*) FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
                  WHERE g.chosen=1) AS items,
                 (SELECT COUNT(DISTINCT m.film_id) FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
                  WHERE g.chosen=1 AND m.film_id IS NOT NULL) AS films""")
        run = db.q(
            """SELECT s.chat_id, s.title, s.messages_seen, s.videos_seen, s.started_at,
                      COALESCE(NULLIF(g.top_message,0), s.messages_seen) AS total
               FROM scan_state s LEFT JOIN groups g USING(chat_id)
               WHERE s.status='running'""")
        running = []
        for r in run:
            pct = min(100, round(r["messages_seen"] / max(1, r["total"]) * 100)) if r["total"] else 0
            elapsed = max(1, _t.time() - (r["started_at"] or _t.time()))
            rate = max(5, r["messages_seen"] / elapsed * 60)  # Mindestrate: Gruppenwechsel faellt nie auf 0
            eta_min = (r["total"] - r["messages_seen"]) / rate if r["total"] > r["messages_seen"] else 0
            running.append({"chat_id": r["chat_id"], "title": r["title"], "pct": pct,
                            "seen": r["messages_seen"], "total": r["total"],
                            "videos": r["videos_seen"], "rate": round(rate), "eta_min": round(eta_min)})
        recent = db.q(
            """SELECT s.title, s.messages_seen, s.videos_seen, s.last_scan_at
               FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
               WHERE g.chosen=1 AND s.status IN ('done','error')
               ORDER BY s.last_scan_at DESC LIMIT 6""")
        # Gesamt-Eta nur aus gewählten Gruppen berechnen.
        pend = db.one("SELECT COALESCE(SUM(CASE WHEN g.top_message>0 THEN g.top_message ELSE 1000 END),0) s "
                      "FROM scan_state st JOIN groups g USING(chat_id) "
                      "WHERE g.chosen=1 AND st.status IN ('queued','error')")["s"]
        rate_all = max(20, sum(x["rate"] for x in running) or 1)  # Mindestrate 20/min -> keine 52h-ETA
        try:
            cred = db.one("SELECT COUNT(DISTINCT film_id) c FROM film_people")["c"]
            cred_total = db.one(
                """SELECT COUNT(DISTINCT f.id) c FROM films f
                   WHERE f.tmdb_id IS NOT NULL AND f.kind IN ('movie','tv','series')""")["c"]
        except Exception:
            cred = cred_total = 0
        try:
            fr = franchise.progress()
        except Exception:
            fr = {"have": 0, "total": 0}
        _eta = round(pend / rate_all) if pend else 0
        _eta = min(_eta, 24 * 60)  # nie mehr als 24h anzeigen (sonst 52h-Schock)
        _progress_cache["data"] = jsonify({**agg, "running": running, "recent": recent,
                        "pending_msgs": pend, "eta_min": _eta,
                        "credits": {"have": cred, "total": cred_total},
                        "franchise": fr,
                        "now": _t.time()})
        _progress_cache["t"] = now
        return _progress_cache["data"]

    @app.get("/api/films/latest")
    def films_latest():
        """Hero: frisch in der Videothek ODER aktuellen Jahres, Trailer live holen.
        Nur Titel aus gewaehlten Gruppen — Gruppen-Auswahl schlaegt sofort durch."""
        from . import tmdb as _tmdb
        import datetime as _dt
        y = _dt.date.today().year
        rows = db.q(
            """SELECT * FROM films WHERE kind='movie' AND tmdb_id IS NOT NULL
               AND EXISTS(SELECT 1 FROM media_items ms WHERE ms.film_id=films.id
                          AND ms.chat_id IN (SELECT chat_id FROM groups WHERE chosen=1))
               ORDER BY CASE WHEN year>=? THEN 0 ELSE 1 END, COALESCE(year,0) DESC,
                        created_at DESC LIMIT 8""", (y - 1,))
        picks = [f for f in rows if f["backdrop_path"]][:5]
        if len(picks) < 5:
            picks = (picks + [f for f in rows if f not in picks])[:5]
        for f in picks:
            if f["trailer"] == "" and f["kind"] in ("movie", "tv"):
                try:
                    key = _tmdb.trailer_key(f["kind"], f["tmdb_id"])
                except Exception:
                    key = "none"
                db.w("UPDATE films SET trailer=? WHERE id=?", (key, f["id"]))
                f["trailer"] = key
        return jsonify({"films": picks})

    def _avg_sources():
        now = time.time()
        if now - _avgsrc_cache["t"] > 300:
            r = db.one("SELECT AVG(s) FROM (SELECT f.id, COUNT(DISTINCT m.id) s FROM films f "
                       "JOIN media_items m ON m.film_id=f.id JOIN groups g ON g.chat_id=m.chat_id "
                       "WHERE g.chosen=1 GROUP BY f.id)")
            _avgsrc_cache["v"] = round(float((r or {}).get("AVG(s)") or 0), 2)
            _avgsrc_cache["t"] = now
        return _avgsrc_cache["v"]

    # ---------- Bibliothek ----------
    @app.post("/api/pin")
    def api_pin():
        data = request.get_json(force=True) or {}
        newpin = str(data.get("pin") or "").strip()
        cfg = config.CFG
        if newpin:
            cfg["pin"] = newpin
        else:
            cfg.pop("pin", None)
        try:
            config.CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return jsonify({"ok": True, "set": bool(newpin)})

    @app.post("/api/pin/check")
    def api_pin_check():
        data = request.get_json(force=True) or {}
        pin = str(data.get("pin") or "").strip()
        stored = str(config.CFG.get("pin") or "")
        return jsonify({"ok": bool(stored) and pin == stored})

    
    @app.get("/api/films")
    def api_films():
        """Film-Bibliothek: alle Filme aus gewählten Gruppen (snapshot-Query)."""
        from backend import db, config

        limit = min(request.args.get("limit", 60, type=int), 5000)
        offset = request.args.get("offset", 0, type=int)
        genre = request.args.get("genre")
        kind = request.args.get("kind")
        qtext = (request.args.get("q") or "").strip()
        fsk = request.args.get("fsk")
        year = request.args.get("year", type=int)
        group = request.args.get("group")
        quality = request.args.get("quality")
        audio = request.args.get("audio")
        sort = request.args.get("sort", "title")

        # Ein Film kann mehrere media_items haben (verschiedene Quellen/Gruppen).
        # Daher: erst DISTINCT film_ids sammeln, dann Filme nach IDs laden.
        film_id_query = (
            "SELECT DISTINCT m.film_id FROM media_items m "
            "JOIN groups g ON g.chat_id = m.chat_id "
            "WHERE g.chosen = 1"
        )
        if genre:
            film_id_query += " AND f.genres LIKE ?"
        if kind:
            film_id_query += " AND f.kind = ?"
        if fsk:
            film_id_query += " AND f.fsk = ?"
        if year:
            film_id_query += " AND f.year = ?"

        # Die Parameter gehören in den Subquery – wir bauen ihn erst nach den Filtern.
        # Besser: alle Filter auf die äußere films-Selektion legen.
        # → Komplette Query neu:
        query = (
            "SELECT f.* FROM films f WHERE f.id IN ("
            "SELECT DISTINCT m.film_id FROM media_items m "
            "JOIN groups g ON g.chat_id = m.chat_id "
            "WHERE g.chosen = 1)"
        )
        params = []

        if genre:
            query += " AND f.genres LIKE ?"
            params.append(f"%{genre}%")
        if qtext:
            query += " AND (f.title LIKE ? OR f.original_title LIKE ? OR f.overview LIKE ?)"
            params.extend([f"%{qtext}%", f"%{qtext}%", f"%{qtext}%"])
        if kind:
            query += " AND f.kind = ?"
            params.append(kind)
        if fsk:
            query += " AND f.fsk = ?"
            params.append(fsk)
        if year:
            query += " AND f.year = ?"
            params.append(year)
        if group:
            query += " AND EXISTS (SELECT 1 FROM media_items mg JOIN groups gg ON gg.chat_id=mg.chat_id WHERE mg.film_id=f.id AND gg.chosen=1 AND gg.title=?)"
            params.append(group)
        if quality:
            query += " AND EXISTS (SELECT 1 FROM media_items mq JOIN groups gq ON gq.chat_id=mq.chat_id WHERE mq.film_id=f.id AND gq.chosen=1 AND (mq.quality=? OR mq.release_type=?))"
            params.extend([quality, quality])
        if audio:
            query += " AND EXISTS (SELECT 1 FROM media_items ma JOIN groups ga ON ga.chat_id=ma.chat_id WHERE ma.film_id=f.id AND ga.chosen=1 AND ma.audio_lang LIKE ?)"
            params.append('%' + audio + '%')

        count_query = query.replace("SELECT f.*", "SELECT COUNT(*) AS c", 1)
        total_row = db.one(count_query, params)
        total_count = int(total_row["c"] if total_row else 0)

        if sort == "year":
            query += " ORDER BY f.year DESC, f.title COLLATE NOCASE"
        elif sort == "rating":
            query += " ORDER BY f.rating DESC, f.title COLLATE NOCASE"
        else:
            query += " ORDER BY f.title COLLATE NOCASE"

        query += f" LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = db.q(query, params)

        poster_dir = config.POSTER_DIR
        result = []
        for f in rows:
            poster = f.get("poster_path")
            if poster:
                poster_file = os.path.join(poster_dir, os.path.basename(poster))
                if not os.path.exists(poster_file):
                    poster = None
            backdrop = f.get("backdrop_path")
            if backdrop:
                backdrop_file = os.path.join(poster_dir, os.path.basename(backdrop))
                if not os.path.exists(backdrop_file):
                    backdrop = None

            result.append({
                "id": f["id"],
                "kind": f["kind"],
                "title": f["title"],
                "year": f["year"],
                "rating": f["rating"],
                "poster_path": poster,
                "backdrop_path": backdrop,
                "genres": f.get("genres"),
                "overview": f.get("overview"),
                "tmdb_id": f["tmdb_id"],
                "fsk": f.get("fsk"),
                "quality": f.get("quality"),
            })

        #DETACH wird nicht mehr gebraucht
        return jsonify({"films": result, "limit": limit, "offset": offset, "total": total_count, "more": offset + len(result) < total_count})

    @app.get("/api/top10")
    def api_top10():
        """Echte Top-10-Listen aus allen gewählten Quellen, nicht aus dem ersten Alphabet-Chunk."""
        from backend import config
        base = """SELECT f.*, COUNT(m.id) sources FROM films f
                  JOIN media_items m ON m.film_id=f.id
                  JOIN groups g ON g.chat_id=m.chat_id
                  WHERE g.chosen=1 AND {where}
                  GROUP BY f.id
                  ORDER BY COALESCE(f.rating,0) DESC, sources DESC, COALESCE(f.year,0) DESC,
                           f.title COLLATE NOCASE LIMIT 10"""
        filters = {
            'series': "f.kind IN ('tv','series')",
            'movies': "f.kind='movie'",
            'horror': "f.kind='movie' AND lower(coalesce(f.genres,'')) LIKE '%horror%'",
            'doku': "f.kind='movie' AND lower(coalesce(f.genres,'')) LIKE '%dokumentar%'",
            'kids': "f.kind='movie' AND (lower(coalesce(f.genres,'')) LIKE '%animation%' OR lower(coalesce(f.genres,'')) LIKE '%familie%') "
                    "AND lower(coalesce(f.genres,'')) NOT LIKE '%horror%' "
                    "AND lower(coalesce(f.genres,'')) NOT LIKE '%thriller%'",
        }
        out = {}
        fsk_seen = set()
        for name, where in filters.items():
            rows = db.q(base.format(where=where))
            out[name] = []
            for f in rows:
                if not (f.get('fsk') or '').strip() and f.get('tmdb_id') and f.get('id') not in fsk_seen:
                    fsk_seen.add(f.get('id'))
                    try:
                        raw = tmdbapi.fsk(f.get('kind') or 'movie', int(f['tmdb_id'])) or ''
                        value = re.sub(r'(?i)^fsk\s*', '', raw).strip()
                        if value:
                            db.w("UPDATE films SET fsk=?, fsk_checked=1 WHERE id=?", (value, f['id']))
                            f['fsk'] = value
                    except Exception:
                        pass
                poster = f.get('poster_path')
                if poster:
                    poster = os.path.basename(str(poster).replace('\\\\', '/'))
                out[name].append({k: f.get(k) for k in (
                    'id','kind','title','year','rating','backdrop_path','genres','overview','tmdb_id','fsk','quality')}
                                        | {'poster_path': poster, 'sources': f.get('sources', 0)})
        return jsonify(out)

    @app.get("/api/pin/status")
    def api_pin_status():
        return jsonify({"set": bool(config.CFG.get("pin"))})

    @app.get("/api/kids")
    def api_kids_get():
        return jsonify({"max": int(config.CFG.get("kids_max_fsk") or 0)})

    @app.post("/api/kids")
    def api_kids_set():
        data = request.get_json(force=True) or {}
        try:
            mx = max(0, min(18, int(data.get("max") or 0)))
        except Exception:
            mx = 0
        cfg = config.CFG
        if mx:
            cfg["kids_max_fsk"] = mx
        else:
            cfg.pop("kids_max_fsk", None)
        try:
            config.CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return jsonify({"ok": True, "max": mx})

    @app.post("/api/fsk/all")
    def api_fsk_all():
        threading.Thread(target=scanner._fsk_all_worker, daemon=True,
                         name="fsk-all").start()
        return jsonify({"ok": True, "note": "FSK für den ganzen Bestand wird nachgeladen"})

    @app.post("/api/cleanup")
    def api_cleanup():
        """Sichere Datenbereinigung: Dubletten-Filme (gleicher norm. Titel+Jahr+kind,
        alle tmdb_id=None) zusammenfuehren. media_items umhaengen, Duplikate loeschen."""
        try:
            from . import cleanup
            return jsonify(cleanup.run())
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 500

    @app.get("/api/stats")
    def api_stats():
        """Aggregierte Bibliotheks-Statistiken fuer die Einstellungen."""
        try:
            return _stats_impl()
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({"ok": False, "error": str(e)[:300]}), 500

    def _stats_impl():
        def one(q, *a):
            r = db.one(q, *a)
            return r["c"] if r else 0
        films = one("SELECT COUNT(*) c FROM films")
        movies = one("SELECT COUNT(*) c FROM films WHERE kind='movie'")
        series = one("SELECT COUNT(*) c FROM films WHERE kind='series'")
        videos = one("SELECT COUNT(*) c FROM media_items")
        groups = one("SELECT COUNT(*) c FROM groups WHERE chosen=1")
        fsk_checked = one("SELECT COUNT(*) c FROM films WHERE fsk_checked IS NOT NULL")
        fsk_pending = one("SELECT COUNT(*) c FROM films WHERE fsk_checked IS NULL")
        fsk_dist = db.q("SELECT COALESCE(fsk,'') fsk, COUNT(*) c FROM films GROUP BY fsk")
        fsk_map = {r["fsk"]: r["c"] for r in fsk_dist}
        from collections import Counter
        gcount = Counter()
        for row in db.q("SELECT genres FROM films WHERE genres IS NOT NULL AND genres != ''"):
            for g in str(row["genres"]).split(","):
                g = g.strip()
                if g:
                    gcount[g] += 1
        genres = [{"name": k, "count": v} for k, v in gcount.most_common(8)]
        top = db.q("""SELECT title, year, rating FROM films
                      WHERE rating IS NOT NULL AND rating>0
                      ORDER BY rating DESC LIMIT 5""")
        import os
        db_size = 0
        for f in (config.DATA_DIR / "videothek.db", config.DATA_DIR / "group_content.sqlite3"):
            if f.exists():
                db_size += f.stat().st_size
        return jsonify({
            "films": films, "movies": movies, "series": series,
            "videos": videos, "groups": groups,
            "fsk_checked": fsk_checked, "fsk_pending": fsk_pending,
            "fsk_dist": fsk_map,
            "genres": [{"name": g["name"], "count": g["count"]} for g in genres],
            "top": [{"title": t["title"], "year": t["year"], "rating": t["rating"]} for t in top],
            "db_mb": round(db_size / 1048576, 1),
        })

    @app.get("/api/export")
    def api_export():
        """Film-Bibliothek als CSV exportieren (Backup/Analyse)."""
        import csv, io
        rows = db.q("""SELECT f.id, f.kind, f.title, f.year, f.rating, f.fsk, f.genres,
                              (SELECT COUNT(*) FROM media_items m WHERE m.film_id=f.id) AS quellen
                       FROM films f JOIN media_items m ON m.film_id=f.id
                       JOIN groups g ON g.chat_id=m.chat_id
                       WHERE g.chosen=1 GROUP BY f.id ORDER BY f.title COLLATE NOCASE""")
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=';')
        w.writerow(["id","kind","title","year","rating","fsk","genres","quellen"])
        for r in rows:
            w.writerow([r["id"], r["kind"], r["title"], r["year"], r["rating"], r["fsk"], r["genres"], r["quellen"]])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=videothek.csv"})

    @app.post("/api/shutdown")
    def api_shutdown():
        """Force-Beenden: App/Backend sofort hart stoppen (Nutzer-Wunsch)."""
        import os
        threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()
        return jsonify({"ok": True, "note": "Videothek wird beendet"})

    @app.post("/api/cache/clear")
    def api_cache_clear():
        """Temporaere Stream-/TDL-Caches leeren (freiwerdender Speicher)."""
        import shutil
        freed = 0
        for d in (config.DATA_DIR / "stream_cache", config.DATA_DIR / "tdl_cache"):
            if d.exists():
                for f in d.iterdir():
                    try:
                        sz = f.stat().st_size
                        f.unlink()
                        freed += sz
                    except Exception:
                        pass
        return jsonify({"ok": True, "freed_mb": round(freed / 1048576, 1)})

    @app.get("/api/fsk/coverage")
    def api_fsk_coverage():
        # Lueckenloser FSK-Nachweis: Verteilung der Freigaben + ungepruefte Filme
        dist = db.q(
            "SELECT COALESCE(NULLIF(f.fsk,''),'(keine)') fsk, COUNT(DISTINCT f.id) n "
            "FROM films f JOIN media_items m ON m.film_id=f.id "
            "JOIN groups g ON g.chat_id=m.chat_id "
            "WHERE g.chosen=1 GROUP BY fsk ORDER BY n DESC")
        pending = db.one(
            "SELECT COUNT(DISTINCT f.id) c FROM films f "
            "JOIN media_items m ON m.film_id=f.id "
            "JOIN groups g ON g.chat_id=m.chat_id "
            "WHERE g.chosen=1 AND f.fsk_checked IS NULL")["c"]
        return jsonify({"distribution": dist, "pending": pending})

    @app.get("/api/mylist")
    def api_mylist():
        rows = db.q("""SELECT f.id, f.kind, f.title, f.year, f.rating, f.poster_path,
                              f.backdrop_path, f.genres, f.overview
                       FROM mylist m JOIN films f ON f.id=m.film_id
                       ORDER BY m.added_at DESC""")
        return jsonify({"films": rows})

    @app.post("/api/mylist")
    def api_mylist_toggle():
        d = request.get_json(silent=True) or {}
        fid = d.get("film_id")
        if not fid:
            return jsonify({"ok": False})
        exists = db.one("SELECT 1 FROM mylist WHERE film_id=?", (fid,))
        if exists:
            db.w("DELETE FROM mylist WHERE film_id=?", (fid,))
            return jsonify({"ok": True, "in_list": False})
        db.w("INSERT INTO mylist(film_id, added_at) VALUES(?,?)", (fid, time.time()))
        return jsonify({"ok": True, "in_list": True})

    @app.post("/api/watch")
    def api_watch():
        d = request.get_json(silent=True) or {}
        fid = d.get("film_id")
        if fid:
            db.w("INSERT INTO watch_history(film_id, watched_at) VALUES(?,?) "
                 "ON CONFLICT(film_id) DO UPDATE SET watched_at=excluded.watched_at", (fid, time.time()))
        return jsonify({"ok": True})

    @app.post("/api/progress")
    def api_progress():
        """Playback-Position speichern (Netflix-Fortschrittsbalken). Das Frontend
        pollt den Player-Zustand und schreibt hier periodisch den Stand.
        mid = media_id (bevorzugt) oder film_id direkt; film_id wird daraus
        aufgeloest, damit nur eine Zuordnung herrscht."""
        d = request.get_json(silent=True) or {}
        mid = int(d.get("mid") or 0)
        fid = int(d.get("film_id") or 0)
        if not fid and mid:
            r = db.one("SELECT film_id FROM media_items WHERE id=?", (mid,))
            fid = int((r or {}).get("film_id") or 0)
        if not fid:
            return jsonify({"ok": False, "error": "unbekannter Film"}), 400
        pos = float(d.get("position") or 0)
        dur = float(d.get("duration") or 0)
        if dur <= 0:
            return jsonify({"ok": False, "error": "keine Dauer"}), 400
        _films_cache.clear()   # Fortschritt sofort auf frischen Karten sichtbar
        db.w("INSERT INTO progress(media_id, film_id, position, duration, updated_at) "
             "VALUES(?,?,?,?,?) "
             "ON CONFLICT(media_id) DO UPDATE SET "
             "position=excluded.position, duration=excluded.duration, updated_at=excluded.updated_at",
             (mid, fid, pos, dur, time.time()))
        # Erst ab ~90% gilt der Film als "gesehen" (Empfehlungs-/Ansehensquelle).
        # Davor ist es nur "in Bearbeitung" (Balken), kein Schauvermerk.
        if dur and pos / dur >= 0.9:
            db.w("INSERT INTO watch_history(film_id, watched_at) VALUES(?,?) "
                 "ON CONFLICT(film_id) DO UPDATE SET watched_at=excluded.watched_at",
                 (fid, time.time()))
        return jsonify({"ok": True})

    @app.get("/api/recommend")
    def api_recommend():
        """Mehrere Empfehlungs-Reihen wie Netflix: eine pro zuletzt gesehenem Titel."""
        watched = db.q("SELECT f.* FROM watch_history w JOIN films f ON f.id=w.film_id "
                       "ORDER BY w.watched_at DESC")
        # Deduplizieren: jeder TITEL nur EINE Empfehlungs-Reihe (Vaiana 2016 + 2026 = eine Reihe)
        _seen = set(); _uniq = []
        for _f in watched:
            _t = (_f.get("title") or "").strip().lower()
            if _t and _t not in _seen:
                _seen.add(_t); _uniq.append(_f)
            if len(_uniq) >= 4:
                break
        watched = _uniq
        rows_out = []
        for last in watched:
            genres = [g.strip() for g in (last.get("genres") or "").split(",") if g.strip()]
            if not genres:
                continue
            kind = last.get("kind") or "movie"
            ref_year = last.get("year") or 0
            # Relevanz-Score: gleiche Art (+2) + je geteiltes Genre (+1) + Jahresnaehe
            score = 0
            if kind == last.get("kind"):
                score += 2
            score += sum(1 for g in genres if g in (last.get("genres") or "").split(","))
            years_diff = abs(ref_year - last.get("year", 0))
            score += 2 if years_diff <= 2 else (1 if years_diff <= 5 else 0)
            rows_out.append((last, score))
        rows_out.sort(key=lambda x: x[1], reverse=True)
        return jsonify({"films": [r[0] for r in rows_out[:4]], "rows": [{"ref": r[0], "films": [r[0]]} for r in rows_out[:4]]})

    # ---------- FSK-Verwaltung ----------
    @app.get("/api/fsk/status")
    def fsk_status():
        """Gibt den FSK-Status der Bibliothek zurück"""
        try:
            counts = db.one(
                """SELECT 
                      COUNT(*) AS total,
                      COUNT(CASE WHEN fsk_checked = 1 THEN 1 END) AS checked,
                      COUNT(CASE WHEN fsk_checked IS NULL OR fsk_checked = 0 THEN 1 END) AS unchecked,
                      COUNT(CASE WHEN fsk IS NOT NULL AND fsk != '' THEN 1 END) AS with_fsk,
                      COUNT(CASE WHEN fsk IS NULL OR fsk = '' THEN 1 END) AS without_fsk
                   FROM films""")
            return jsonify({
                "ok": True,
                "counts": counts,
                "unchecked_films": fsk_manager.get_unchecked_films()[:20]  # Erste 20 ungeprüfte Filme
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/fsk/<int:film_id>")
    def fsk_get(film_id: int):
        """Gibt FSK-Informationen für einen Film zurück"""
        try:
            fsk_info = fsk_manager.get_fsk_for_film(film_id)
            if fsk_info:
                return jsonify({
                    "ok": True,
                    "fsk": {
                        "film_id": fsk_info.film_id,
                        "fsk": fsk_info.fsk,
                        "checked": fsk_info.fsk_checked,
                        "source": fsk_info.fsk_source,
                        "checked_at": fsk_info.checked_at
                    }
                })
            else:
                return jsonify({"ok": False, "error": "Film nicht gefunden"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/fsk/<int:film_id>")
    def fsk_set(film_id: int):
        """Setzt FSK-Informationen für einen Film"""
        try:
            data = request.get_json(force=True)
            fsk = data.get("fsk")
            source = data.get("source", "manual")
            
            if not fsk:
                return jsonify({"ok": False, "error": "FSK erforderlich"}), 400
            
            success = fsk_manager.set_fsk_for_film(film_id, fsk, source)
            if success:
                return jsonify({"ok": True, "message": "FSK erfolgreich gesetzt"})
            else:
                return jsonify({"ok": False, "error": "Fehler beim Setzen der FSK"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/fsk/batch")
    def fsk_batch():
        """Batch-Update von FSK-Informationen"""
        try:
            data = request.get_json(force=True)
            film_fsk_map = data.get("film_fsk_map", {})
            film_ids = list(film_fsk_map.keys())
            
            result = fsk_manager.batch_update_fsk(film_ids, film_fsk_map)
            return jsonify({"ok": True, "result": result})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/fsk/validate")
    def fsk_validate():
        """Validiert ob ein Film die erforderliche FSK erfüllen kann"""
        try:
            data = request.get_json(force=True)
            film_id = data.get("film_id")
            required_fsk = data.get("required_fsk")
            
            if not film_id or not required_fsk:
                return jsonify({"ok": False, "error": "Film-ID und erforderliche FSK erforderlich"}), 400
            
            compatible = fsk_manager.validate_fsk(film_id, required_fsk)
            return jsonify({
                "ok": True,
                "film_id": film_id,
                "required_fsk": required_fsk,
                "compatible": compatible
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    def fsk_coverage():
        """Gibt die FSK-Abdeckung der Bibliothek zurück"""
        try:
            dist = db.q(
                "SELECT COALESCE(NULLIF(f.fsk,''),'(keine)') fsk, COUNT(DISTINCT f.id) n "
                "FROM films f JOIN media_items m ON m.film_id=f.id "
                "JOIN groups g ON g.chat_id=m.chat_id "
                "WHERE g.chosen=1 GROUP BY fsk ORDER BY n DESC")
            pending = db.one(
                "SELECT COUNT(DISTINCT f.id) c FROM films f "
                "JOIN media_items m ON m.film_id=f.id "
                "JOIN groups g ON g.chat_id=m.chat_id "
                "WHERE g.chosen=1 AND f.fsk_checked IS NULL")["c"]
            return jsonify({"distribution": dist, "pending": pending})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ---------- Scan-Verwaltung ----------
    @app.post("/api/scan")
    def api_scan():
        """Startet einen Scan für eine Gruppe oder mehrere Gruppen"""
        try:
            data = request.get_json(silent=True) or {}
            ids = data.get("chat_ids")
            force = bool(data.get("force"))
            
            if ids is not None:
                ids = [int(i) for i in ids]
            
            return jsonify(scan_manager.start_scan(ids, force))
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/scan/state")
    def api_scan_state():
        """Gibt den Status aller Scans zurück"""
        try:
            return jsonify(scan_manager.get_scan_status())
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/scan/cancel")
    def api_scan_cancel():
        """Bricht einen laufenden Scan ab"""
        try:
            data = request.get_json(force=True)
            group_id = data.get("chat_id")
            
            if not group_id:
                return jsonify({"ok": False, "error": "Gruppen-ID erforderlich"}), 400
            
            success = scan_manager.cancel_scan(group_id)
            return jsonify({"ok": success, "message": "Scan abgebrochen" if success else "Scan konnte nicht abgebrochen werden"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/scan/stats")
    def api_scan_stats():
        """Gibt Scan-Statistiken zurück"""
        try:
            stats = db.one(
                """SELECT 
                      COUNT(*) AS total_scans,
                      COUNT(CASE WHEN status = 'done' THEN 1 END) AS completed_scans,
                      COUNT(CASE WHEN status = 'running' THEN 1 END) AS running_scans,
                      COUNT(CASE WHEN status = 'error' THEN 1 END) AS error_scans
                   FROM scan_state""")
            
            # Durchschnittliche Scan-Zeit
            avg_time = db.one(
                """SELECT AVG(julianday(last_scan_at) - julianday(started_at)) * 86400 AS avg_seconds 
                   FROM scan_state 
                   WHERE status = 'done' AND started_at IS NOT NULL AND last_scan_at IS NOT NULL"""
            )
            
            return jsonify({
                "ok": True,
                "stats": stats,
                "avg_time_seconds": avg_time["avg_seconds"] if avg_time else 0,
                "active_scans": len(scan_manager._active_scans)
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ---------- Einstellungen ----------
    @app.get("/api/settings")
    def api_settings():
        """Gibt alle Einstellungen zurück"""
        try:
            settings = settings_manager.get_all_settings()
            return jsonify({"ok": True, "settings": settings})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/settings/fsk")
    def api_get_fsk_settings():
        """Gibt FSK-Einstellungen zurück"""
        try:
            settings = settings_manager.get_fsk_settings()
            return jsonify({"ok": True, "fsk_settings": settings.to_dict()})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/settings/fsk")
    def api_set_fsk_settings():
        """Setzt FSK-Einstellungen"""
        try:
            data = request.get_json(force=True)
            if not data:
                return jsonify({"ok": False, "error": "Keine Daten erhalten"}), 400
            
            # Aktualisiere Einstellungen
            success = settings_manager.update_fsk_settings(**data)
            if success:
                return jsonify({"ok": True, "message": "FSK-Einstellungen gespeichert"})
            else:
                return jsonify({"ok": False, "error": "Fehler beim Speichern"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/settings/scan")
    def api_get_scan_settings():
        """Gibt Scan-Einstellungen zurück"""
        try:
            settings = settings_manager.get_scan_settings()
            return jsonify({"ok": True, "scan_settings": settings.to_dict()})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/settings/scan")
    def api_set_scan_settings():
        """Setzt Scan-Einstellungen"""
        try:
            data = request.get_json(force=True)
            if not data:
                return jsonify({"ok": False, "error": "Keine Daten erhalten"}), 400
            
            # Aktualisiere Einstellungen
            success = settings_manager.update_scan_settings(**data)
            if success:
                return jsonify({"ok": True, "message": "Scan-Einstellungen gespeichert"})
            else:
                return jsonify({"ok": False, "error": "Fehler beim Speichern"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.post("/api/settings/reset")
    def api_reset_settings():
        """Setzt alle Einstellungen auf Standardwerte zurück"""
        try:
            success = settings_manager.reset_to_defaults()
            if success:
                return jsonify({"ok": True, "message": "Einstellungen zurückgesetzt"})
            else:
                return jsonify({"ok": False, "error": "Fehler beim Zurücksetzen"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/settings/validate")
    def api_validate_settings():
        """Validiert aktuelle Einstellungen"""
        try:
            fsk_settings = settings_manager.get_fsk_settings()
            scan_settings = settings_manager.get_scan_settings()
            
            # Validierung der FSK-Einstellungen
            fsk_valid = True
            if fsk_settings.min_fsk and fsk_settings.max_fsk:
                fsk_valid = settings_manager.validate_fsk_range(
                    fsk_settings.min_fsk, 
                    fsk_settings.max_fsk
                )
            
            return jsonify({
                "ok": True,
                "validation": {
                    "fsk_range_valid": fsk_valid,
                    "fsk_settings": fsk_settings.to_dict(),
                    "scan_settings": scan_settings.to_dict()
                }
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ---------- End of Settings ----------

    @app.get("/api/series")
    def api_series():
        """Alle Serien aus der Datenbank."""
        from backend import db, config
        
        limit = request.args.get('limit', 12, type=int)
        q = request.args.get('q')
        
        query = """
            SELECT f.*, COUNT(m.id) AS eps,
                   COUNT(DISTINCT NULLIF(m.season, 0)) AS seasons,
                   MAX(m.date_utc) AS latest
            FROM films f
            JOIN media_items m ON m.film_id=f.id
            JOIN groups g ON g.chat_id=m.chat_id
            WHERE f.kind IN ('tv', 'series') AND g.chosen=1
        """
        params = []
        if q:
            query += " AND (f.title LIKE ? OR f.original_title LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%"])
        query += " GROUP BY f.id ORDER BY COALESCE(f.rating,0) DESC, COALESCE(MAX(m.date_utc),'') DESC, f.title COLLATE NOCASE LIMIT ?"
        rows = db.q(query, params + [limit])

        result = []
        poster_dir = config.POSTER_DIR
        
        for f in rows:
            poster = f.get('poster_path')
            if poster:
                poster_file = os.path.join(poster_dir, os.path.basename(poster))
                if not os.path.exists(poster_file):
                    poster = None
            
            backdrop = f.get('backdrop_path')
            if backdrop:
                backdrop_file = os.path.join(poster_dir, os.path.basename(backdrop))
                if not os.path.exists(backdrop_file):
                    backdrop = None
            
            result.append({
                'id': f.get('id'),
                'kind': f.get('kind'),
                'title': f.get('title'),
                'year': f.get('year'),
                'rating': f.get('rating'),
                'poster_path': poster,
                'backdrop_path': backdrop,
                'genres': f.get('genres'),
                'overview': f.get('overview'),
                'tmdb_id': f.get('tmdb_id'),
                'fsk': f.get('fsk'),
                'quality': f.get('quality'),
                'eps': f.get('eps', 0),
                'seasons': f.get('seasons', 0),
                'latest': f.get('latest'),
            })

        return jsonify({'series': result})

    @app.get("/api/series/<int:film_id>")
    def api_series_detail(film_id):
        from backend import db, config
        film = db.one("SELECT * FROM films WHERE id=? AND kind IN ('tv','series')", (film_id,))
        if not film:
            return jsonify({'error': 'Serie nicht gefunden'}), 404
        rows = db.q("""SELECT m.* FROM media_items m
                       JOIN groups g ON g.chat_id=m.chat_id
                       WHERE m.film_id=? AND g.chosen=1
                       ORDER BY COALESCE(m.season,0), COALESCE(m.episode,0), m.date_utc DESC""", (film_id,))
        grouped = {}
        for m in rows:
            season = int(m.get('season') or 0)
            episode = int(m.get('episode') or 0)
            grouped.setdefault(season, {}).setdefault(episode, []).append(m)
        seasons = []
        for season, eps in sorted(grouped.items()):
            episodes = []
            for episode, sources in sorted(eps.items()):
                m = sources[0]
                episodes.append({
                    'id': m.get('id'), 'season': season, 'episode': episode,
                    'filename': m.get('filename'), 'parsed_title': m.get('parsed_title'),
                    'caption': m.get('caption'), 'date_utc': m.get('date_utc'),
                    'size_bytes': m.get('size_bytes'), 'duration': m.get('duration'),
                    'status': m.get('status'), 'ep_name': m.get('parsed_title') or m.get('filename'),
                    'n_src': len(sources), 'src': [{'id': s.get('id'), 'filename': s.get('filename'),
                        'size_bytes': s.get('size_bytes'), 'status': s.get('status'),
                        'chat_title': s.get('chat_title')} for s in sources]
                })
            seasons.append({'season': season, 'episodes': episodes})
        poster = film.get('poster_path')
        if poster and not (config.POSTER_DIR / os.path.basename(poster)).exists():
            poster = None
        return jsonify({'film': {
            'id': film.get('id'), 'kind': film.get('kind'), 'title': film.get('title'),
            'year': film.get('year'), 'rating': film.get('rating'), 'genres': film.get('genres'),
            'overview': film.get('overview'), 'poster_path': poster,
            'backdrop_path': film.get('backdrop_path'), 'fsk': film.get('fsk'),
            'trailer': film.get('trailer', '')
        }, 'seasons': seasons, 'total_seasons': len(seasons), 'cast': [], 'streaming': []})

    @app.get("/api/films/<int:film_id>")
    def api_film_detail(film_id):
        from backend import db, config
        film = db.one("SELECT * FROM films WHERE id=?", (film_id,))
        if not film:
            return jsonify({"ok": False, "error": "Film nicht gefunden"}), 404
        poster = film.get('poster_path')
        if poster and not (config.POSTER_DIR / os.path.basename(poster)).exists():
            poster = None
        backdrop = film.get('backdrop_path')
        if backdrop and not (config.POSTER_DIR / os.path.basename(backdrop)).exists():
            backdrop = None
        items = db.q("SELECT m.*, f.title as film_title FROM media_items m JOIN films f ON f.id=m.film_id JOIN groups g ON g.chat_id=m.chat_id WHERE m.film_id=? AND g.chosen=1 ORDER BY m.date_utc DESC", (film_id,))
        similar = []
        if film.get('tmdb_id'):
            try:
                from . import tmdb
                similar = tmdb.similar(film['kind'], film['tmdb_id'])[:10]
            except Exception:
                pass
        cast = []
        cast_films = []
        try:
            from . import tmdb
            cd = tmdb.cast_and_crew(film['kind'], film['tmdb_id'])
            cast = cd.get('cast', [])[:10]
            cast_films = cd.get('cast_films', [])[:10]
        except Exception:
            pass
        streaming = []
        if film.get('tmdb_id'):
            try:
                from . import tmdb
                streaming = tmdb.streaming_providers(film['tmdb_id'])
            except Exception:
                pass
        return jsonify({
            "film": {
                'id': film['id'], 'kind': film['kind'], 'title': film['title'],
                'year': film['year'], 'rating': film['rating'],
                'poster_path': poster, 'backdrop_path': backdrop,
                'genres': film.get('genres'), 'overview': film.get('overview'),
                'tmdb_id': film['tmdb_id'], 'fsk': film.get('fsk'),
                'trailer': film.get('trailer', ''),
            },
            "items": [{
                'id': m['id'], 'filename': m.get('filename'),
                'parsed_title': m.get('parsed_title'), 'chat_id': m['chat_id'],
                'chat_title': m.get('chat_title'), 'topic_title': m.get('topic_title'),
                'date_utc': m.get('date_utc'), 'size_bytes': m.get('size_bytes'),
                'duration': m.get('duration'), 'link': m.get('link'),
                'season': m.get('season'), 'episode': m.get('episode'),
                'quality': m.get('quality'), 'codec': m.get('codec'),
                'audio_lang': m.get('audio_lang'), 'hdr': m.get('hdr'),
                'subtitles': m.get('subtitles'), 'release_type': m.get('release_type'),
                'width': m.get('width'), 'height': m.get('height'),
                'dl_status': m.get('status'), 'mime': m.get('mime'),
                'playable': m.get('playable'),
            } for m in items],
            "similar": similar, "cast": cast, "cast_films": cast_films,
            "streaming": streaming,
        })

    @app.get("/api/tmdb/search")
    def api_tmdb_search():
        query = (request.args.get("q") or "").strip()
        if len(query) < 2:
            return jsonify({"results": []})
        try:
            return jsonify({"results": tmdbapi.search_multi(query, limit=8)})
        except Exception as e:
            return jsonify({"results": [], "error": str(e)[:160]}), 502

    @app.post("/api/media/<int:mid>/assign")
    def api_media_assign(mid):
        media = db.one("SELECT id,filename,parsed_title FROM media_items WHERE id=?", (mid,))
        if not media:
            return jsonify({"ok": False, "error": "Medium nicht gefunden"}), 404
        data = request.get_json(silent=True) or {}
        conn = db.get_db()
        if data.get("clear"):
            conn.execute("UPDATE media_items SET film_id=NULL,status='unmatched' WHERE id=?", (mid,))
            conn.commit()
            return jsonify({"ok": True, "cleared": True})
        try:
            tmdb_id = int(data["tmdb_id"])
            kind = str(data.get("kind") or "movie")
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "tmdb_id und kind erforderlich"}), 400
        detail = tmdbapi.details(kind, tmdb_id)
        if not detail:
            return jsonify({"ok": False, "error": "TMDB-Titel nicht gefunden"}), 404
        film = db.one("SELECT id FROM films WHERE tmdb_id=? AND kind=?", (tmdb_id, detail["kind"]))
        if film:
            film_id = film["id"]
        else:
            film_id = db.w("""INSERT INTO films(kind,tmdb_id,title,original_title,year,genres,overview,
                              poster_path,backdrop_path,rating,created_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (detail["kind"], detail["tmdb_id"], detail["title"], detail["original_title"],
                            detail["year"], detail["genres"], detail["overview"], detail["poster_path"],
                            detail["backdrop_path"], detail["rating"], time.time()))
        conn.execute("UPDATE media_items SET film_id=?,status='matched',match_conf=1.0,match_reason='manual' WHERE id=?",
                     (film_id, mid))
        conn.commit()
        return jsonify({"ok": True, "film_id": film_id, "title": detail["title"]})

    @app.get("/api/review/media")
    def api_review_media():
        limit = max(1, min(request.args.get("limit", 100, type=int), 500))
        rows = db.q("""SELECT m.id,m.filename,m.parsed_title,m.parsed_year,m.status,m.match_conf,
                             m.match_reason,m.chat_title,m.size_bytes,m.reachability,m.reachability_error
                      FROM media_items m
                      WHERE m.status IN ('ambiguous','error') OR (m.film_id IS NULL AND m.status='indexed')
                         OR m.reachability IN ('missing','error')
                      ORDER BY m.id LIMIT ?""", (limit,))
        return jsonify({"items": rows, "count": len(rows)})

    @app.get("/api/review/duplicates")
    def api_review_duplicates():
        rows = db.q("""SELECT filename,size_bytes,COUNT(*) AS count,
                             GROUP_CONCAT(id) AS media_ids
                      FROM media_items
                      WHERE filename IS NOT NULL AND TRIM(filename)!=''
                      GROUP BY lower(filename),size_bytes HAVING COUNT(*)>1
                      ORDER BY count DESC LIMIT 500""")
        return jsonify({"duplicates": rows, "count": len(rows)})

    @app.get("/api/fill")
    def api_fill_get():
        genre = request.args.get("genre")
        try:
            from . import curation
            if genre:
                return jsonify(curation.fill_genre_tab(genre))
            return jsonify(curation.fill_all_tabs())
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 500

    @app.post("/api/fill")
    def api_fill_post():
        payload = request.get_json(silent=True) or {}
        genre = request.args.get("genre") or payload.get("genre")
        try:
            from . import curation
            if genre:
                return jsonify(curation.fill_genre_tab(genre))
            return jsonify(curation.fill_all_tabs())
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 500

    @app.get("/api/people/filter")
    def api_people_filter():
        role = request.args.get("role", "cast")
        q = request.args.get("q", "")
        try:
            from . import curation
            items = curation.search_people(role, q)
            return jsonify({"items": items})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:200]}), 500

    @app.get("/api/classics")
    def api_classics():
        """Klassiker aus der gewählten Bibliothek, chronologisch sortiert."""
        from backend import db, config

        page = max(1, request.args.get('page', 1, type=int))
        q = (request.args.get('q') or '').strip()
        per_page = 20
        offset = (page - 1) * per_page
        where = ["f.kind = 'movie'",
                 "(f.year <= 1990 OR f.rating >= 8.0)",
                 "EXISTS (SELECT 1 FROM media_items m JOIN groups g ON g.chat_id=m.chat_id "
                 "WHERE m.film_id=f.id AND g.chosen=1)"]
        params = []
        if q:
            where.append("(f.title LIKE ? OR f.original_title LIKE ?)")
            params.extend([f'%{q}%', f'%{q}%'])
        clause = ' AND '.join(where)
        rows = db.q(f"""SELECT f.* FROM films f WHERE {clause}
                       ORDER BY COALESCE(f.year,9999) ASC, COALESCE(f.rating,0) DESC,
                                f.title COLLATE NOCASE LIMIT ? OFFSET ?""",
                    params + [per_page, offset])
        total = db.one(f"SELECT COUNT(*) c FROM films f WHERE {clause}", params)['c']
        result = []
        for f in rows:
            poster = f.get('poster_path')
            if poster and not (config.POSTER_DIR / os.path.basename(poster)).exists():
                poster = None
            result.append({k: f.get(k) for k in (
                'id','kind','title','year','rating','backdrop_path','genres','overview','tmdb_id','fsk')}
                | {'poster_path': poster})
        return jsonify({'items': result, 'classics': result, 'total': total,
                        'page': page, 'pages': (total + per_page - 1) // per_page if total else 1,
                        'more': offset + len(result) < total})
    
    @app.get("/api/charts")
    def api_charts():
        """Charts: Top-Filme, Genre-Hubs etc."""
        from backend import db, config
        
        mode = request.args.get('mode', 'top')
        
        if mode == 'top':
            rows = db.q("""
                SELECT f.* FROM films f
                WHERE f.kind IN ('movie', 'tv', 'series')
                AND f.rating > 0
                AND EXISTS (SELECT 1 FROM media_items m 
                            JOIN groups g ON g.chat_id = m.chat_id 
                            WHERE m.film_id = f.id AND g.chosen = 1)
                ORDER BY f.rating DESC, f.year DESC
                LIMIT 20
            """)
        elif mode == 'hub':
            genre_rows = db.q("""
                SELECT DISTINCT TRIM(value) as genre, COUNT(*) as count
                FROM films, json_each('[' || REPLACE(genres, ',', '","') || ']') 
                WHERE genres IS NOT NULL AND genres != ''
                AND TRIM(value) != ''
                GROUP BY genre
                ORDER BY count DESC
                LIMIT 10
            """)
            if not genre_rows:
                # Fallback: Einfachere Methode
                genre_rows = []
                all_genres = {}
                for row in db.q("SELECT genres FROM films WHERE genres IS NOT NULL AND genres != ''"):
                    for g in row['genres'].split(','):
                        g = g.strip()
                        if g:
                            all_genres[g] = all_genres.get(g, 0) + 1
                genre_rows = [{'genre': g, 'count': c} for g, c in sorted(all_genres.items(), key=lambda x: -x[1])[:10]]
            return jsonify({'hubs': genre_rows})
        elif mode == 'byYear':
            year = request.args.get('year')
            if year:
                rows = db.q("""
                    SELECT f.* FROM films f
                    WHERE f.year = ?
                    AND f.kind IN ('movie', 'tv', 'series')
                    AND EXISTS (SELECT 1 FROM media_items m 
                                JOIN groups g ON g.chat_id = m.chat_id 
                                WHERE m.film_id = f.id AND g.chosen = 1)
                    ORDER BY f.rating DESC, f.title COLLATE NOCASE
                    LIMIT 20
                """, (year,))
            else:
                rows = []
        else:
            rows = []
        
        result = []
        poster_dir = config.POSTER_DIR
        
        for f in rows:
            poster = f.get('poster_path')
            if poster:
                poster_file = os.path.join(poster_dir, os.path.basename(poster))
                if not os.path.exists(poster_file):
                    poster = None
            
            backdrop = f.get('backdrop_path')
            if backdrop:
                backdrop_file = os.path.join(poster_dir, os.path.basename(backdrop))
                if not os.path.exists(backdrop_file):
                    backdrop = None
            
            result.append({
                'id': f.get('id'),
                'kind': f.get('kind'),
                'title': f.get('title'),
                'year': f.get('year'),
                'rating': f.get('rating'),
                'poster_path': poster,
                'backdrop_path': backdrop,
                'genres': f.get('genres'),
                'overview': f.get('overview'),
                'tmdb_id': f.get('tmdb_id'),
                'fsk': f.get('fsk'),
            })
        
        return jsonify({'charts': result, 'mode': mode})
    
    @app.get("/api/facets")
    def api_facets():
        """Genre-Facetten und Filter-Optionen."""
        from backend import db
        
        genres = db.q("""
            SELECT genres, COUNT(*) as count
            FROM films
            WHERE genres IS NOT NULL AND genres != ''
            AND EXISTS (SELECT 1 FROM media_items m 
                        JOIN groups g ON g.chat_id = m.chat_id 
                        WHERE m.film_id = films.id AND g.chosen = 1)
            GROUP BY genres
            ORDER BY count DESC
        """)
        
        years = db.q("""
            SELECT COALESCE(year, 0) as year, COUNT(*) as count
            FROM films
            WHERE year IS NOT NULL
            AND EXISTS (SELECT 1 FROM media_items m 
                        JOIN groups g ON g.chat_id = m.chat_id 
                        WHERE m.film_id = films.id AND g.chosen = 1)
            GROUP BY year
            ORDER BY year DESC
        """)
        
        fsk_dist = db.q("""
            SELECT COALESCE(NULLIF(fsk,''),'(keine)') as fsk, COUNT(*) as count
            FROM films
            WHERE EXISTS (SELECT 1 FROM media_items m 
                        JOIN groups g ON g.chat_id = m.chat_id 
                        WHERE m.film_id = films.id AND g.chosen = 1)
            GROUP BY fsk
            ORDER BY count DESC
        """)
        
        kind_dist = db.q("""
            SELECT kind, COUNT(*) as count
            FROM films
            WHERE EXISTS (SELECT 1 FROM media_items m 
                        JOIN groups g ON g.chat_id = m.chat_id 
                        WHERE m.film_id = films.id AND g.chosen = 1)
            GROUP BY kind
        """)
        
        return jsonify({
            'genres': [{'name': r['genres'], 'count': r['count']} for r in genres],
            'years': [{'year': r['year'], 'count': r['count']} for r in years],
            'fsk_distribution': [{'fsk': r['fsk'], 'count': r['count']} for r in fsk_dist],
            'kind_distribution': [{'kind': r['kind'], 'count': r['count']} for r in kind_dist],
        })
    
    @app.get("/api/library")
    def api_library():
        """Gesamte Bibliothek mit Filtern."""
        from backend import db, config
        
        genre = request.args.get('genre')
        fsk = request.args.get('fsk')
        year = request.args.get('year', type=int)
        kind = request.args.get('kind')
        sort = request.args.get('sort', 'title')
        limit = request.args.get('limit', 1200, type=int)
        offset = request.args.get('offset', 0, type=int)
        
        query = """
            SELECT f.* FROM films f
            WHERE EXISTS (SELECT 1 FROM media_items m 
                          JOIN groups g ON g.chat_id = m.chat_id 
                          WHERE m.film_id = f.id AND g.chosen = 1)
        """
        
        params = []
        
        if genre:
            query += " AND f.genres LIKE ?"
            params.append(f'%{genre}%')
        
        if fsk:
            query += " AND f.fsk = ?"
            params.append(fsk)
        
        if year:
            query += " AND f.year = ?"
            params.append(year)
        
        if kind:
            query += " AND f.kind = ?"
            params.append(kind)
        
        if sort == 'year':
            query += " ORDER BY f.year DESC, f.title COLLATE NOCASE"
        elif sort == 'rating':
            query += " ORDER BY f.rating DESC, f.year DESC"
        else:
            query += " ORDER BY f.title COLLATE NOCASE"
        
        query += f" LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        films = db.q(query, params)
        
        poster_dir = config.POSTER_DIR
        
        result = []
        for f in films:
            poster = f.get('poster_path')
            if poster:
                poster_file = os.path.join(poster_dir, os.path.basename(poster))
                if not os.path.exists(poster_file):
                    poster = None
            
            backdrop = f.get('backdrop_path')
            if backdrop:
                backdrop_file = os.path.join(poster_dir, os.path.basename(backdrop))
                if not os.path.exists(backdrop_file):
                    backdrop = None
            
            result.append({
                'id': f.get('id'),
                'kind': f.get('kind'),
                'title': f.get('title'),
                'year': f.get('year'),
                'rating': f.get('rating'),
                'poster_path': poster,
                'backdrop_path': backdrop,
                'genres': f.get('genres'),
                'overview': f.get('overview'),
                'tmdb_id': f.get('tmdb_id'),
                'fsk': f.get('fsk'),
            })
        
        offline = db.q("""SELECT d.media_id, m.film_id, m.filename, m.mime, d.path, f.title,
                              f.year, f.poster_path, f.backdrop_path, f.rating, f.genres
                       FROM downloads d JOIN media_items m ON m.id=d.media_id
                       LEFT JOIN films f ON f.id=m.film_id WHERE d.status='done' ORDER BY d.finished_at DESC""")
        total_bytes = sum(int((d.get('bytes_total') or 0)) for d in offline)
        return jsonify({'films': result, 'items': offline, 'count': len(offline), 'total_gb': total_bytes / 1e9, 'limit': limit, 'offset': offset})
    
    @app.get("/api/genre")
    def api_genre():
        """Verfügbare Genres für die Filterung."""
        from backend import db
        
        all_genres = set()
        for row in db.q("SELECT genres FROM films WHERE genres IS NOT NULL AND genres != ''"):
            for g in row['genres'].split(','):
                g = g.strip()
                if g:
                    all_genres.add(g)
        
        genres = [{'genre': g} for g in sorted(all_genres)]
        
        return jsonify({'genres': genres})

    # ---------- Fehlende Frontend-Flächen: bewusst leichtgewichtige Adapter ----------
    @app.get("/api/hubs")
    def api_hubs():
        from . import franchise
        curated = franchise.hubs()
        collections = franchise.all_collections()
        progress = franchise.progress()
        return jsonify({'hubs': curated, 'collections': collections, 'progress': progress})

    @app.get("/api/hub/<path:slug>")
    def api_hub(slug):
        genre = slug.replace('%20', ' ')
        films = db.q("""SELECT f.* FROM films f WHERE f.genres LIKE ? AND EXISTS(
            SELECT 1 FROM media_items m JOIN groups g ON g.chat_id=m.chat_id
            WHERE m.film_id=f.id AND g.chosen=1) ORDER BY COALESCE(f.year,0), f.title COLLATE NOCASE""", (f'%{genre}%',))
        hub = {'slug': genre, 'name': genre, 'icon': '📚', 'blurb': f'{len(films)} Titel'}
        return jsonify({'hub': hub, 'series': [{'name': genre, 'films': films}], 'single': [], 'total': len(films)})

    @app.get("/api/people")
    def api_people():
        people = []
        for p in curation.people_top(request.args.get('limit', 90, type=int)):
            d = dict(p)
            d['n'] = d.get('n') or d.get('films') or 0
            d['profile_local'] = d.get('profile_path') or ''
            people.append(d)
        return jsonify({'people': people, 'credits_progress': {'have': len(people), 'total': len(people)}})

    @app.get("/api/person/<int:pid>")
    def api_person(pid):
        data = curation.person(pid)
        return jsonify(data or {'error': 'Person nicht gefunden'}), (200 if data else 404)

    @app.get("/api/birthdays")
    def api_birthdays():
        from datetime import date, timedelta
        day = request.args.get('day', 'today')
        target = date.today() + timedelta(days=1 if day == 'tomorrow' else 0)
        md = target.strftime('-%m-%d')
        rows = db.q("SELECT id, name, profile_path, birthday FROM people WHERE birthday LIKE ? ORDER BY name", (f'%{md}',))
        items = [{'type': 'person', 'id': r['id'], 'name': r['name'], 'profile_local': r.get('profile_path') or '', 'birthday': r['birthday']} for r in rows]
        return jsonify({'items': items, 'date': target.strftime('%d.%m.%Y')})

    @app.get("/api/theaters")
    def api_theaters():
        try:
            from .theaters import list_theaters
            return jsonify(list_theaters())
        except Exception as exc:
            return jsonify({'items': [], 'providers': [], 'error': str(exc)[:200]})

    @app.get("/api/ticker")
    def api_ticker():
        return jsonify({'items': []})

    @app.get("/api/weather")
    def api_weather():
        return jsonify({'ok': False, 'weather': None})

    _player = {'mid': None, 'queue': [], 'pos': 0, 'time': 0, 'playing': False}

    @app.get("/api/player/state")
    def api_player_state():
        try:
            from . import player
            actual = player.player_state()
        except Exception as exc:
            actual = {'open': False, 'state': 'State.Error', 'time': 0, 'length': 0,
                      'buffering': False, 'embed': False, 'error': str(exc)[:160]}
        return jsonify({**_player, **actual, 'mid': _player.get('mid')})

    @app.post("/api/player/queue")
    def api_player_queue():
        payload = request.get_json(silent=True) or {}
        _player['queue'] = payload.get('mids') or []
        _player['pos'] = int(payload.get('pos') or 0)
        return jsonify({'ok': True})

    @app.get("/api/player/cmd")
    def api_player_cmd():
        cmd = request.args.get('cmd', '')
        arg = request.args.get('arg', type=int)
        if cmd == 'close':
            _player.update(mid=None, playing=False, time=0)
            try:
                from . import player
                player.stop_video()
            except Exception:
                pass
        elif cmd in ('toggle', 'seekto', 'seek'):
            try:
                from . import player
                player.player_command(cmd, arg)
            except Exception:
                pass
        return api_player_state()

    @app.get("/api/player/open")
    def api_player_open():
        mid = request.args.get('mid', type=int)
        mi = db.one("SELECT * FROM media_items WHERE id=?", (mid,)) if mid else None
        if not mi or not _media_in_chosen(mid):
            return jsonify({'ok': False, 'error': 'Medium nicht verfügbar'}), 404
        _player.update(mid=mid, time=0, playing=True)
        try:
            from . import player
            player.play_video(f"http://127.0.0.1:{config.PORT}/api/media/{mid}/stream", mi.get('filename') or '')
        except Exception as exc:
            return jsonify({'ok': False, 'error': f'Player konnte nicht gestartet werden: {exc}'}), 500
        return jsonify({'ok': True, 'mode': 'live', 'embed': True})

    @app.get("/api/player/close")
    def api_player_close():
        _player.update(mid=None, playing=False, time=0)
        try:
            from . import player
            player.stop_video()
        except Exception:
            pass
        return jsonify({'ok': True})

    @app.get("/api/media/<int:mid>/cachestate")
    def api_cache_state(mid):
        return jsonify(cache.state(mid))

    @app.get("/api/media/<int:mid>/stream")
    def api_media_stream(mid):
        mi = db.one("SELECT * FROM media_items WHERE id=?", (mid,))
        if not mi or not _media_in_chosen(mid):
            return jsonify({'error': 'Medium nicht verfügbar'}), 404
        total = int(mi.get('size_bytes') or 0)
        mime = _mime(mi)
        cache.ensure(mid, int(mi['chat_id']), int(mi['message_id']), total, mime)
        start = 0
        end = max(0, total - 1)
        raw = request.headers.get('Range', '')
        if raw.startswith('bytes='):
            spec = raw[6:].split(',', 1)[0]
            a, _, b = spec.partition('-')
            start = int(a or 0)
            end = min(end, int(b) if b else end)
        length = max(0, end - start + 1)
        def body():
            pos = start
            while pos <= end:
                chunk = cache.read(mid, pos, min(64 * 1024, end - pos + 1))
                if not chunk:
                    break
                pos += len(chunk)
                yield chunk
        response = Response(body(), status=206 if raw.startswith('bytes=') else 200, mimetype=mime)
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Length'] = str(length)
        response.headers['Content-Range'] = f'bytes {start}-{end}/{total}'
        return response

    @app.get("/api/downloads")
    def api_downloads():
        rows = db.q("""SELECT d.*, m.filename, m.film_id, f.title
                      FROM downloads d LEFT JOIN media_items m ON m.id=d.media_id
                      LEFT JOIN films f ON f.id=m.film_id ORDER BY d.id DESC LIMIT 500""")
        stats = {}
        for status in ('running', 'queued', 'paused', 'done', 'error', 'canceled'):
            q = db.one("SELECT COUNT(*) c, COALESCE(SUM(bytes_done),0) done FROM downloads WHERE status=?", (status,))
            stats[status] = {'count': int(q['c'] or 0), 'done': int(q['done'] or 0)}
        return jsonify({'downloads': rows, 'stats': stats, 'max_concurrent': 2})

    @app.get("/api/downloads/<int:did>")
    def api_download(did):
        row = db.one("""SELECT d.*, m.filename, m.film_id, f.title
                       FROM downloads d LEFT JOIN media_items m ON m.id=d.media_id
                       LEFT JOIN films f ON f.id=m.film_id WHERE d.id=?""", (did,))
        return jsonify(row or {'error': 'Download nicht gefunden'}), (200 if row else 404)

    @app.post("/api/downloads/<int:did>/<action>")
    def api_download_action(did, action):
        if action in ('cancel', 'pause'):
            db.w("UPDATE downloads SET status=? WHERE id=?", ('canceled' if action == 'cancel' else 'paused', did))
        elif action in ('resume', 'retry'):
            db.w("UPDATE downloads SET status='queued', error=NULL WHERE id=?", (did,))
        return jsonify({'ok': True})

    @app.post("/api/media/<int:mid>/download")
    def api_media_download(mid):
        mi = db.one("SELECT * FROM media_items WHERE id=?", (mid,))
        if not mi:
            return jsonify({'ok': False, 'error': 'Medium nicht gefunden'}), 404
        existing = db.one("SELECT id FROM downloads WHERE media_id=? AND status IN ('queued','running','done') ORDER BY id DESC LIMIT 1", (mid,))
        if existing:
            return jsonify({'ok': True, 'download_id': existing['id']})
        did = db.w("INSERT INTO downloads(media_id,path,status,bytes_total) VALUES(?,?,?,?)", (mid, '', 'queued', mi.get('size_bytes') or 0))
        return jsonify({'ok': True, 'download_id': did})

    return app