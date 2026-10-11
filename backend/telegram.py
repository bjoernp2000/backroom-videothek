"""Telethon-Bausteine: Gruppen auflisten, Entities, Links, Video-Metadaten."""
from __future__ import annotations

import asyncio
import threading
import time

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.utils import get_peer_id

from . import config

_CLIENT: TelegramClient | None = None
_LOOP: asyncio.AbstractEventLoop | None = None
_LOCK = threading.RLock()


def _make_client() -> TelegramClient:
    tg = config.CFG["tg"]
    return TelegramClient(StringSession(tg["session"]), int(tg["api_id"]), tg["api_hash"],
                          request_retries=3, connection_retries=3, flood_sleep_threshold=60)


def _ensure_loop():
    global _LOOP
    with _LOCK:
        if _LOOP is not None and _LOOP.is_running():
            return
        _LOOP = asyncio.new_event_loop()

        def run():
            asyncio.set_event_loop(_LOOP)
            _LOOP.run_forever()

        threading.Thread(target=run, daemon=True, name="tg-loop").start()


def _run(coro, timeout=120):
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=timeout)


def reconnect():
    """Verbindung erzwungen erneuern (totes Pipelining nach FloodWait)."""
    global _client
    _ensure_loop()
    try:
        c = _client
    except Exception:
        c = None
    if c is None:
        return
    async def _go():
        try:
            await c.disconnect()
        except Exception:
            pass
        try:
            await c.connect()
        except Exception:
            pass
    try:
        _run(_go(), 45)
    except Exception:
        pass


def _runx(make_coro, timeout=120):
    """Mit Constructor-ID-Selbstheilung: Telegram sendet mitunter Objekte eines
    neueren Layers ('Could not find the matching Constructor ID'); der TCP-Strom
    ist danach kontaminiert und alle Folgeaufrufe schlagen fehl. Dann Connection
    wegwerfen, frisch verbinden und die Coroutine neu bauen (Factory!).
    """
    global _CLIENT
    _ensure_loop()
    err = None
    for attempt in range(4):
        try:
            return asyncio.run_coroutine_threadsafe(make_coro(), _LOOP).result(timeout=timeout)
        except Exception as e:
            err = e
            if "Constructor ID" not in str(e) or attempt >= 2:
                raise
            time.sleep(1.5 * (attempt + 1))
            try:
                if _CLIENT is not None:
                    asyncio.run_coroutine_threadsafe(_CLIENT.disconnect(), _LOOP).result(15)
            except Exception:
                pass
            _CLIENT = None
            client()
    raise err


def submit(coro):
    """Coroutine auf dem TG-Loop starten, Future sofort zurueck (Background-Feed)."""
    _ensure_loop()
    return asyncio.run_coroutine_threadsafe(coro, _LOOP)


def client() -> TelegramClient:
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = _make_client()
        return _CLIENT


def connect() -> TelegramClient:
    c = client()
    if not c.is_connected():
        _run(c.connect(), 60)
    return c


def get_media_video(msg) -> dict | None:
    """Video-/Filmdatei-Daten einer Nachricht, sonst None."""
    try:
        from telethon.tl.types import MessageMediaDocument
        if not getattr(msg, "media", None) or not isinstance(msg.media, MessageMediaDocument):
            return None
        doc = msg.media.document
        attrs = {type(a).__name__: a for a in (doc.attributes or [])}
        fname = getattr(attrs.get("DocumentAttributeFilename"), "file_name", "") or ""
        mime = (doc.mime_type or "").lower()
        is_video = "VideoAttribute" in attrs
        is_file = fname.lower().endswith((".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".wmv"))
        if not (is_video or mime.startswith("video/") or is_file):
            return None
        v = attrs.get("VideoAttribute")
        return {
            "filename": fname,
            "mime": mime or ("video/mp4" if is_video else ""),
            "size": int(doc.size or 0),
            "duration": int(getattr(v, "duration", 0) or 0) if v else 0,
            "width": int(getattr(v, "w", 0) or 0) if v else 0,
            "height": int(getattr(v, "h", 0) or 0) if v else 0,
        }
    except Exception:
        return None


def message_link(entity, msg_id: int, topic_id: int | None = None) -> str:
    """t.me-Link. Basic-Chats ohne Username haben keinen oeffentlichen Link."""
    try:
        username = getattr(entity, "username", None)
        peer = get_peer_id(entity)
        if username:
            base = f"https://t.me/{username}/{msg_id}"
        elif peer <= -1000000000000:  # Channel/Megagroup: -100-Präfix abtrennen
            base = f"https://t.me/c/{str(-peer)[4:]}/{msg_id}"
        else:
            return ""
        if topic_id and int(topic_id) != int(msg_id):
            base += f"?thread={topic_id}"
        return base
    except Exception:
        return ""


def list_groups() -> list[dict]:
    """Alle Gruppen/Kanal-Dialoge holen und in DB upserten (chat_id mit -100-Markierung)."""
    c = connect()

    async def _go():
        out = []
        async for d in c.iter_dialogs():  # ohne Limit: sonst fehlen die hinteren
            ent = d.entity
            cls = type(ent).__name__
            if cls == "Channel":
                ctype = "channel" if getattr(ent, "broadcast", False) else "megagroup"
            elif cls == "Chat":
                ctype = "group"
            else:
                continue  # PMs/Bots aussen vor
            out.append({
                "chat_id": get_peer_id(ent),
                "title": getattr(ent, "title", None) or "?",
                "username": getattr(ent, "username", None),
                "type": ctype,
                "is_forum": 1 if getattr(ent, "forum", False) else 0,
                "member_count": getattr(ent, "participants_count", None),
                "access_hash": getattr(ent, "access_hash", None),
                "top_message": int(getattr(d, "top_message", 0) or 0),
            })
        return out

    dialogs = _run(_go(), 240)
    import time as _t
    conn = db_get()
    now = _t.time()
    conn.execute("UPDATE groups SET last_seen=0")   # erst maerken, wer zurueckkommt
    for g in dialogs:
        conn.execute(
            """INSERT INTO groups(chat_id,title,username,type,is_forum,chosen,member_count,last_seen,access_hash,top_message)
               VALUES(:chat_id,:title,:username,:type,:is_forum,
                      COALESCE((SELECT chosen FROM groups WHERE chat_id=:chat_id),0),
                      :member_count,:last_seen,:access_hash,:top_message)
               ON CONFLICT(chat_id) DO UPDATE SET title=:title, username=:username,
                   type=:type, is_forum=:is_forum, member_count=:member_count,
                   last_seen=:last_seen, access_hash=excluded.access_hash,
                   top_message=excluded.top_message""",
            {**g, "last_seen": now},
        )
    conn.commit()
    # Gruppe nicht mehr in den Dialogen (verlassen/geloescht) -> automatisch
    # abwaelen: Bibliothek blendet sie sofort aus, laufenden Scan abbrechen.
    gone = [r[0] for r in conn.execute(
        "SELECT chat_id FROM groups WHERE last_seen=0 AND chosen=1").fetchall()]
    if gone:
        conn.execute("UPDATE groups SET chosen=0 WHERE last_seen=0")
        conn.commit()
        from . import scanner
        scanner.abandon(gone)
    from . import db
    return db.q("SELECT * FROM groups ORDER BY title COLLATE NOCASE")


def resolve(chat_id: int):
    """Entity aus chat_id: username -> stored access_hash -> Peer-Hash.

    Nach Server-Neustart fehlt der Session-Cache; ohne access_hash schickt
    Telethon Hash=0 und Telegram antwortet unparsebar ('Constructor ID').
    """
    from telethon.tl.types import PeerChannel, PeerChat
    g = db_get().execute(
        "SELECT chat_id, username, type, access_hash FROM groups WHERE chat_id=?",
        (int(chat_id),)).fetchone()
    cid = int(chat_id)
    _ensure_loop()
    c = client()
    if not c.is_connected():
        _run(c.connect(), 60)
    if g and g["username"]:
        return _runx(lambda: client().get_entity(g["username"]), 60)
    if cid <= -1000000000000:
        chan = -cid - 1000000000000
        if g and g["access_hash"]:
            from telethon.tl.types import InputChannel
            return _runx(lambda: client().get_entity(
                InputChannel(channel_id=chan, access_hash=int(g["access_hash"]))), 60)
        return _runx(lambda: client().get_entity(PeerChannel(chan)), 60)
    if cid < 0:
        return _runx(lambda: client().get_entity(PeerChat(-cid)), 60)
    return _runx(lambda: client().get_entity(PeerChannel(cid)), 60)


def get_entity(chat_id: int):
    """Kompatibler synchroner Entity-Zugriff für Scanner und Cache."""
    return resolve(int(chat_id))


def get_messages(entity, after_id: int = 0, limit: int = 200):
    """Liest Nachrichten nach after_id chronologisch aus einem Telegram-Dialog."""
    async def _go():
        c = client()
        if not c.is_connected():
            await c.connect()
        out = []
        async for msg in c.iter_messages(entity, min_id=int(after_id or 0), limit=int(limit), reverse=True):
            out.append(msg)
        return out
    return _run(_go(), max(120, int(limit) * 2))


def db_get():
    from . import db
    return db.get_db()
