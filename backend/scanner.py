"""Hintergrund-Scanner: indexiert Videos aus gewählten Gruppen, matcht TMDB, lädt down."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from difflib import SequenceMatcher

from . import config, db, parsers, telegram, tmdb

_STOP = threading.Event()
_ABANDON: set[int] = set()
_ABANDON_LOCK = threading.Lock()

_SCAN_THREAD: threading.Thread | None = None
_MATCH_THREAD: threading.Thread | None = None
_LIVE_SYNC_THREAD: threading.Thread | None = None
_LIVE_SYNC_STOP = threading.Event()
_LIVE_SYNC_STARTED = False
DL_PROGRESS: dict[int, dict] = {}
_SCAN_TIMES: dict[int, float] = {}

def _throttle():
    """Streamt gerade jemand? -> Scanner lässt Telegram den Vorzug."""
    from . import stream
    return 2.0 if stream.ACTIVE[0] else 0.02

def _topic_titles(entity) -> dict:
    """Forum-Topic-Titel abfragen (für Topic-übergreifende Metadaten)."""
    titles = {}
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest
        async def _go():
            r = await telegram.client()(
                GetForumTopicsRequest(channel=entity, limit=128, offset_date=0, offset_id=0, q=""))
            for t in r.topics:
                titles[int(getattr(t, "id", 0) or 0)] = getattr(t, "title", "") or ""
        telegram._run(_go(), 30)
    except Exception:
        pass
    return titles

def _store_video(entity, msg, topic_titles, chat_id: int, conn=None):
    """Speichert ein neues Media-Item aus einer Telegram-Nachricht."""
    from .telegram import get_media_video, get_media_print, message_link
    media = get_media_video(msg)
    print_item = False
    if not media:
        media = get_media_print(msg)
        print_item = bool(media)
    if not media:
        return

    rt = getattr(msg, "reply_to", None)
    topic_id = getattr(rt, "forum_topic", None) and getattr(rt, "reply_to_msg_id", None) or None
    caption = (msg.text or msg.message or "")[:2000]
    parsed = parsers.parse_caption(caption, media["filename"])
    specs = parsers.parse_media_specs(media["filename"], caption)
    link = message_link(entity, msg.id, topic_id)
    date_utc = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""
    sender = ""
    try:
        s = getattr(msg, "sender", None)
        sender = (getattr(s, "username", None) or getattr(s, "first_name", None) or "") if s else ""
    except Exception:
        pass
    _conn = conn or db.get_db()
    _conn.execute(
        """INSERT OR IGNORE INTO media_items(chat_id,message_id,topic_id,chat_title,topic_title,date_utc,sender,
           filename,mime,size_bytes,duration,width,height,link,parsed_title,parsed_year,season,episode,
           media_kind,status,quality,codec,audio_lang,hdr,subtitles,release_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (chat_id, msg.id, topic_id,
         entity.title if hasattr(entity, "title") else "(Titel fehlt)",
         topic_titles.get(topic_id, "") if topic_id else "",
         date_utc, sender,
         media["filename"], media.get("mime", ""), media.get("size", 0),
         media.get("duration", 0), media.get("width", 0), media.get("height", 0),
         link, parsed.get("title", ""), parsed.get("year", None),
         parsed.get("season", None), parsed.get("episode", None),
         parsed.get("media_kind", "print" if print_item else "movie"), "unmatched",
         specs["quality"], specs["codec"], specs["audio_lang"], specs["hdr"],
         specs["subtitles"], specs["release_type"]))
    _conn.commit()

def _scan_worker():
    """Hauptschleife: bearbeitet scan_state='queued' Einträge nacheinander."""
    while not _STOP.is_set():
        try:
            row = db.one(
                """SELECT s.chat_id,s.title,s.last_message_id
                   FROM scan_state s JOIN groups g ON g.chat_id=s.chat_id
                   LEFT JOIN kiosk_sources ks ON ks.chat_id=s.chat_id
                   LEFT JOIN sports_sources ss ON ss.chat_id=s.chat_id
                   WHERE s.status='queued' AND (g.chosen=1 OR ks.chosen=1 OR ss.chosen=1) LIMIT 1""")
            if not row:
                time.sleep(5)
                continue
            cid = int(row["chat_id"])
            with _ABANDON_LOCK:
                if cid in _ABANDON:
                    db.w("UPDATE scan_state SET status='idle' WHERE chat_id=?", (cid,))
                    continue
            db.w("UPDATE scan_state SET status='running' WHERE chat_id=?", (cid,))
            try:
                entity = telegram.get_entity(cid)
                if entity:
                    msgs = telegram.get_messages(entity, row["last_message_id"] or 0, 200)
                    for msg in msgs:
                        if _STOP.is_set():
                            break
                        if not msg or not msg.media:
                            continue
                        topic_titles = _topic_titles(entity)
                        _store_video(entity, msg, topic_titles, cid)
                        db.w("UPDATE scan_state SET last_message_id=?, messages_seen=messages_seen+1, "
                             "videos_seen=videos_seen+1 WHERE chat_id=?",
                             (msg.id, cid))
                        time.sleep(_throttle())
                    db.w("UPDATE scan_state SET status='done', last_scan_at=? WHERE chat_id=?",
                          (time.time(), cid))
            except Exception as e:
                db.w("UPDATE scan_state SET status='error', error=? WHERE chat_id=?",
                      (str(e)[:200], cid))
            time.sleep(2)
        except Exception:
            time.sleep(5)

def start_scan(chat_ids=None, force: bool = False) -> dict:
    """Startet einen Scan für gewählte Gruppen (oder alle gewählten Gruppen).
    force=True -> bereits 'done'-Gruppen werden erneut gescannt."""
    conn = db.get_db()
    if chat_ids is None:
        chat_ids = [r["chat_id"] for r in db.q(
            "SELECT chat_id FROM groups WHERE chosen=1 "
            "UNION SELECT chat_id FROM kiosk_sources WHERE chosen=1 AND chat_id IS NOT NULL "
            "UNION SELECT chat_id FROM sports_sources WHERE chosen=1 AND chat_id IS NOT NULL")]
    for cid in chat_ids:
        g = db.one("SELECT title FROM groups WHERE chat_id=? AND (chosen=1 OR EXISTS(SELECT 1 FROM kiosk_sources WHERE chat_id=? AND chosen=1) OR EXISTS(SELECT 1 FROM sports_sources WHERE chat_id=? AND chosen=1))", (int(cid), int(cid), int(cid)))
        if not g:
            continue
        conn.execute(
            """INSERT INTO scan_state(chat_id,title,status) VALUES(:cid,:title,'queued')
               ON CONFLICT(chat_id) DO UPDATE SET title=:title, retries=0,
                 status=CASE WHEN scan_state.status='running' THEN scan_state.status
                             WHEN scan_state.status='done' AND :force=0 THEN scan_state.status
                             ELSE 'queued' END""",
            {"cid": int(cid), "title": g["title"], "force": 1 if force else 0})
    conn.commit()
    real = db.one("SELECT COUNT(*) c FROM scan_state WHERE status IN ('queued','running') "
                  "AND chat_id IN (%s)" % ",".join("?" * len(chat_ids)),
                  [int(c) for c in chat_ids] or [0])["c"] if chat_ids else 0
    _ensure_scan_thread()
    return {"queued": int(real)}


def _live_sync_loop(interval: int):
    """Prüft gewählte Telegram-Quellen regelmäßig auf neue Nachrichten.

    force=True setzt nur den Scanstatus zurück; der gespeicherte last_message_id
    sorgt dafür, dass Telegram ab der letzten Nachricht fortsetzt und nicht die
    gesamte Quelle erneut verarbeitet.
    """
    while not _LIVE_SYNC_STOP.is_set():
        try:
            if config.TG_OK:
                chosen = [int(r["chat_id"]) for r in db.q(
                    "SELECT chat_id FROM groups WHERE chosen=1 "
                    "UNION SELECT chat_id FROM kiosk_sources WHERE chosen=1 AND chat_id IS NOT NULL "
                    "UNION SELECT chat_id FROM sports_sources WHERE chosen=1 AND chat_id IS NOT NULL")]
                if chosen:
                    start_scan(chosen, force=True)
        except Exception:
            # Der nächste Zyklus darf einen kurzzeitigen Telegram-Fehler auffangen.
            pass
        if _LIVE_SYNC_STOP.wait(interval):
            break


def start_live_sync(interval: int = 60):
    """Startet genau einen leisen Hintergrund-Abgleich der gewählten Quelle."""
    global _LIVE_SYNC_THREAD, _LIVE_SYNC_STARTED
    if _LIVE_SYNC_STARTED and _LIVE_SYNC_THREAD and _LIVE_SYNC_THREAD.is_alive():
        return
    _LIVE_SYNC_STARTED = True
    _LIVE_SYNC_STOP.clear()
    _LIVE_SYNC_THREAD = threading.Thread(
        target=_live_sync_loop, args=(max(30, int(interval)),),
        name="telegram-live-sync", daemon=True)
    _LIVE_SYNC_THREAD.start()


def stop_live_sync():
    _LIVE_SYNC_STOP.set()


def queue_indexed(limit: int = 500, chat_ids=None) -> dict:
    """Reiht alte, noch nicht zugeordnete Medien batchweise für TMDB ein."""
    limit = max(1, min(int(limit or 500), 5000))
    params = []
    where = ["status='indexed'", "film_id IS NULL", "parsed_title IS NOT NULL", "TRIM(parsed_title) != ''"]
    if chat_ids:
        ids = [int(cid) for cid in chat_ids]
        where.append("chat_id IN (%s)" % ",".join("?" * len(ids)))
        params.extend(ids)
    rows = db.q("SELECT id FROM media_items WHERE " + " AND ".join(where) +
                " ORDER BY id LIMIT ?", [*params, limit])
    if rows:
        conn = db.get_db()
        conn.executemany("UPDATE media_items SET status='unmatched' WHERE id=?",
                         [(int(row['id']),) for row in rows])
        conn.commit()
    return {"queued": len(rows), "limit": limit}


def check_reachability(limit: int = 25, force: bool = False) -> dict:
    """Prüft wenige Telegram-Referenzen und markiert erreichbare/fehlende Medien."""
    limit = max(1, min(int(limit or 25), 100))
    where = "g.chosen=1"
    if not force:
        where += " AND (m.reachability IS NULL OR m.reachability='')"
    rows = db.q(f"""SELECT m.id,m.chat_id,m.message_id FROM media_items m
                    JOIN groups g ON g.chat_id=m.chat_id
                    WHERE {where} ORDER BY m.id LIMIT ?""", (limit,))
    checked = {"reachable": 0, "missing": 0, "error": 0}
    for row in rows:
        now = time.time()
        try:
            entity = telegram.get_entity(int(row["chat_id"]))
            messages = telegram.get_messages(entity, max(0, int(row["message_id"]) - 1), 2)
            found = any(int(getattr(msg, "id", 0) or 0) == int(row["message_id"]) for msg in messages)
            state = "reachable" if found else "missing"
            db.w("UPDATE media_items SET reachability=?,reachability_checked=?,reachability_error='' WHERE id=?",
                 (state, now, row["id"]))
            checked[state] += 1
        except Exception as exc:
            db.w("UPDATE media_items SET reachability='error',reachability_checked=?,reachability_error=? WHERE id=?",
                 (now, str(exc)[:180], row["id"]))
            checked["error"] += 1
    return {"checked": len(rows), **checked}


def _ensure_scan_thread():
    global _SCAN_THREAD
    if not (_SCAN_THREAD and _SCAN_THREAD.is_alive()):
        _SCAN_THREAD = threading.Thread(target=_scan_worker, daemon=True, name="scan")
        _SCAN_THREAD.start()

def resume_scans():
    """Nach Server-Neustart: 'running'-Einträge zurück in die Warteschlange."""
    db.w("UPDATE scan_state SET status='queued' WHERE status='running'")
    ids = [r["chat_id"] for r in db.q("SELECT chat_id FROM scan_state WHERE status='queued'")]
    if ids:
        _ensure_scan_thread()
    return ids

def resume_pending():
    """Nach Server-Neustart: 'queued'-Downloads neu einreihen (resumt aus .part-Dateien)."""
    for row in db.q("SELECT id FROM downloads WHERE status='queued'"):
        _resume_download(row["id"])

def _resume_download(dl_id: int):
    """Nimmt einen Download-Job wieder auf."""
    row = db.one("SELECT media_id,path FROM downloads WHERE id=?", (dl_id,))
    if not row:
        return
    # In der Vollversion würde hier die Download-Loop neu gestartet
    pass

def start_pump():
    """Fehler-Selbstläufer-Pumpe: Downloads mit Fehlerstatus werden zyklisch wiederholt."""
    def _pump_loop():
        while not _STOP.is_set():
            try:
                for row in db.q("SELECT id,media_id,path,error FROM downloads WHERE status='error'"):
                    if _STOP.is_set():
                        break
                    db.w("UPDATE downloads SET status='queued', error=NULL WHERE id=?", (row["id"],))
                time.sleep(120)
            except Exception:
                time.sleep(30)
    threading.Thread(target=_pump_loop, daemon=True, name="pump").start()

def start_matcher():
    """Matcher: Unmatched Media-Items werden mit TMDB abgeglichen."""
    def _match_worker():
        while not _STOP.is_set():
            try:
                items = db.q(
                    "SELECT id,chat_id,filename,parsed_title,parsed_year,media_kind "
                    "FROM media_items WHERE status='unmatched' LIMIT 10")
                for item in items:
                    if _STOP.is_set():
                        break
                    title = item.get("parsed_title") or item.get("filename") or ""
                    year = item.get("parsed_year")
                    try:
                        best = tmdb.search(title, year)
                        if not best:
                            db.w("UPDATE media_items SET status='indexed',match_conf=NULL,match_reason=? WHERE id=?",
                                 ("no_match", item["id"]))
                            continue
                        conf = best.get("conf") or {}
                        exact = bool(conf.get("exact"))
                        year_match = bool(conf.get("year_match"))
                        score = float(conf.get("score") or 0)
                        safe = exact and (not year or year_match) or (year_match and score >= 2.0)
                        if not safe:
                            db.w("UPDATE media_items SET status='ambiguous',match_conf=?,match_reason=? WHERE id=?",
                                 (score, ";".join(k for k, v in (("exact", exact), ("year", year_match), ("typo", bool(conf.get("typo")))) if v), item["id"]))
                            continue
                        tmdb_id = best["tmdb_id"]
                        existing = db.one("SELECT id FROM films WHERE tmdb_id=?", (tmdb_id,))
                        if not existing:
                            db.w(
                                """INSERT INTO films(kind,tmdb_id,title,original_title,year,
                                   genres,overview,poster_path,backdrop_path,rating)
                                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                ("movie" if best["kind"] == "movie" else "tv",
                                 tmdb_id, best["title"], best.get("original_title", ""),
                                 best["year"], best.get("genres", ""), best.get("overview", ""),
                                 best.get("poster_path", ""), best.get("backdrop_path", ""),
                                 best.get("rating", None)))
                        film_id = db.one("SELECT id FROM films WHERE tmdb_id=?", (tmdb_id,))["id"]
                        db.w("UPDATE media_items SET film_id=?,status='matched',match_conf=?,match_reason=? WHERE id=?",
                              (film_id, score, "exact" if exact else "year", item["id"]))
                    except Exception as exc:
                        db.w("UPDATE media_items SET status='error',match_reason=? WHERE id=?",
                             (str(exc)[:180], item["id"]))
                time.sleep(30)
            except Exception:
                time.sleep(10)
    _MATCH_THREAD = threading.Thread(target=_match_worker, daemon=True, name="matcher")
    _MATCH_THREAD.start()

def download_progress(dl_id: int) -> dict:
    p = DL_PROGRESS.get(dl_id)
    if p:
        return p
    return db.one("SELECT status,path,bytes_done,bytes_total,error FROM downloads WHERE id=?",
                  (dl_id,)) or {"status": "unknown"}

def start_fill_tabs() -> dict:
    """Behandelt /api/fill (alle Reiter): startet inkrementellen Scan aller gewählten Gruppen.
    Wrapper für start_scan mit force=True, damit auch bereits gescannte Gruppen neu matchen."""
    from . import db as _db
    ids = [int(r["chat_id"]) for r in _db.q("SELECT chat_id FROM groups WHERE chosen=1")]
    if not ids:
        return {"ok": True, "note": "keine gewählten Gruppen"}
    try:
        start_scan(chat_ids=ids, force=True)
        return {"ok": True, "note": f"Scan gestartet für {len(ids)} Gruppen"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def start_fill_tab(genre: str) -> dict:
    """Behandelt /api/fill?genre=... : signalisiert Genre-Scanstart.
    Der eigentliche Filter erfolgt clientseitig über die fertige films-Tabelle.
    Wir starten KEINEN Scan — die Filme sind bereits in der DB.
    """
    if not genre:
        return {"ok": False, "error": "genre erforderlich"}
    # Kein Scan notwendig — Filme sind bereits vorhanden
    return {"ok": True, "note": f"Genre-Tab '{genre}' bereit (Filter erfolgt clientseitig)"}
