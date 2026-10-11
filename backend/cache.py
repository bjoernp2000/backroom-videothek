"""Flüchtiger Wiedergabe-Cache: das Streaming-Rückgrat der Videothek.

Pro Video eine Sparse-Datei + EIN Loader-Strang, der Telegram genau einmal
anzapft:  HECK zuerst (4 MB — bei Scene-Releases sitzt der MP4-Index 'moov'
am Dateiende, ohne ihn öffnet kein Player), dann von VORN durch.
Reads (alle Range-Requests des Players) blockieren pro Stelle, bis der Loader
sie gedeckt hat — der Player sieht einen normalen HTTP-Stream, nie Null-Müll.

Bewährtes Design (Sep 2026): ein Vorwärts-Pass, kein Rückwärts-Herumspringen —
frühere 'folge dem Player-Wunsch'-Varianten haben dieselben MBs doppelt geladen
und den Fortschritt zum Stillstand gebracht.

Nichts bleibt dauerhaft: nach 6 h ohne Zugriff löscht der Sweeper die Datei.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import config, stream, telegram

KB = 1024
MB = 1024 * KB
IDLE_KILL_S = 6 * 3600
ALIGN = 4096
TAIL = 4 * MB                  # vorrangig geholt (MP4-Moov am Dateiende)
WAIT_MAX_S = 600               # max. Warten eines Reads auf neue Bytes

_DIR = config.DATA_DIR / "stream_cache"
_DIR.mkdir(exist_ok=True)

_EXT_BY_MIME = {"video/mp4": ".mp4", "video/quicktime": ".mp4",
                "video/x-matroska": ".mkv", "video/webm": ".webm",
                "video/x-msvideo": ".avi"}


def _path(mid: int, mime: str = "") -> Path:
    return _DIR / f"{mid}{_EXT_BY_MIME.get((mime or '').lower(), '.bin')}"


class _Entry:
    def __init__(self, mid: int, total: int, mime: str):
        self.mid = mid
        self.total = total
        self.path = _path(mid, mime)
        self.last = time.time()
        self.lock = threading.Lock()
        self.cv = threading.Condition()
        self.pos = 0
        self.tail_done = 0
        self.dead = False
        self.error = ""
        self.rate = 0.0
        self.bytes = 0
        self._samples = []
        self.last_adv = time.time()
        self.src = None
        self.mime = mime
        self.fp = open(self.path, "w+b")
        self.fp.seek(total - 1)
        self.fp.write(b"\0")
        self.fp.flush()

    def covered(self, p: int) -> bool:
        tail_start = max(0, self.total - TAIL)
        if p < tail_start:
            return self.pos > p
        return self.tail_done > p or self.pos >= self.total

    def write_at(self, off: int, data: bytes):
        with self.lock:
            self.fp.seek(off)
            self.fp.write(data)
            self.fp.flush()

    def read_at(self, off: int, n: int) -> bytes:
        with self.lock:
            self.fp.seek(off)
            return self.fp.read(n)

    def close(self):
        try:
            self.fp.close()
        except Exception:
            pass


class Loader(threading.Thread):
    """Ein Strang pro Video: Heck (Index) zuerst, dann Kopf bis unter das Heck."""

    def __init__(self, e: _Entry, chat_id: int, message_id: int, cap: int = 0):
        super().__init__(daemon=True)
        self.e = e
        self.chat_id = chat_id
        self.message_id = message_id
        self.stop = threading.Event()
        self.cap = cap
        self.stage = "tail"

    def _pump(self, off: int, end: int, attr: str):
        e = self.e
        while not self.stop.is_set() and off < end:
            off -= off % ALIGN
            try:
                gen, _ = stream.open_stream(self.chat_id, self.message_id, off)
            except Exception as ex:
                e.error = f"setup@{off // KB}k: {type(ex).__name__}: {str(ex)[:120]}"
                telegram.reconnect()
                time.sleep(3)
                continue
            p = off
            last = time.time()
            try:
                for chunk in gen:
                    if self.stop.is_set() or p >= end:
                        break
                    if time.time() - last > 90:
                        e.error = f"stale@{p // MB}mb"
                        break
                    data = chunk[: end - p]
                    if not data:
                        break
                    e.write_at(p, data)
                    p += len(data)
                    setattr(e, attr, p)
                    now = time.time()
                    e.last_adv = now
                    e.bytes += len(data)
                    e._samples.append((now, e.bytes))
                    if len(e._samples) > 400:
                        del e._samples[:-200]
                    old = next((x for x in e._samples if x[0] >= now - 20), e._samples[0])
                    if now - old[0] > 4:
                        e.rate = (e.bytes - old[1]) / (now - old[0]) / MB
                    last = now
                    with e.cv:
                        e.cv.notify_all()
            except Exception as ex:
                e.error = f"pump@{p // MB}mb: {type(ex).__name__}: {str(ex)[:120]}"
                time.sleep(1)
            if self.stop.is_set():
                try:
                    gen.close()
                except Exception:
                    pass
                return
            if p >= end:
                return
            off = p
            time.sleep(1.0)

    def run(self):
        e = self.e
        tail_start = max(0, e.total - TAIL)
        end = tail_start
        if self.cap:
            end = min(end, self.cap)

        # VLC liest bei MP4 unmittelbar den moov-Index am Dateiende. Kopf und
        # Ende müssen deshalb parallel verfügbar werden: nur Kopf zuerst lässt
        # den Demuxer beim ersten Seek in eine noch ungedeckte Range laufen.
        self.stage = "head+tail"
        tail_thread = threading.Thread(
            target=self._pump,
            args=(max(tail_start, e.tail_done), e.total, "tail_done"),
            daemon=True,
        )
        tail_thread.start()
        self._pump(0, end, "pos")
        if self.stop.is_set():
            return
        tail_thread.join()


_ENTRIES: dict[int, _Entry] = {}
_LOADERS: dict[int, Loader] = {}
_ENTRIES_LOCK = threading.Lock()


def ensure(mid: int, chat_id: int, message_id: int, total: int, mime: str = "",
           cap: int = 0) -> _Entry:
    """Stellt sicher, dass es einen Cache-Eintrag + Loader für diese ID gibt."""
    with _ENTRIES_LOCK:
        e = _ENTRIES.get(mid)
        if e is None:
            e = _Entry(mid, total, mime)
            _ENTRIES[mid] = e
        if mid not in _LOADERS or _LOADERS[mid].stop.is_set():
            ld = Loader(e, chat_id, message_id, cap)
            _LOADERS[mid] = ld
            ld.start()
        else:
            e.src = (chat_id, message_id)
    return e


def read(mid: int, off: int, n: int) -> bytes:
    """Liest n Bytes ab offset aus dem Cache (blockiert bis die Bytes da sind)."""
    with _ENTRIES_LOCK:
        e = _ENTRIES.get(mid)
    if e is None:
        return b""
    deadline = time.time() + WAIT_MAX_S
    while not e.covered(off + n - 1):
        now = time.time()
        if now >= deadline:
            return e.read_at(off, n) if e.covered(off) else b""
        remaining = deadline - now
        with e.cv:
            e.cv.wait(timeout=min(remaining, 1.0))
    return e.read_at(off, n)


def wait_tail(mid: int, timeout: float = 45.0) -> bool:
    """Wartet auf den MP4-Trailer, den VLC beim Öffnen zuerst sucht."""
    with _ENTRIES_LOCK:
        e = _ENTRIES.get(mid)
    if e is None:
        return False
    deadline = time.time() + timeout
    tail_start = max(0, e.total - TAIL)
    with e.cv:
        while e.tail_done < e.total and not e.error:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            e.cv.wait(timeout=min(remaining, 0.5))
    return e.tail_done >= e.total


def state(mid: int) -> dict:
    with _ENTRIES_LOCK:
        e = _ENTRIES.get(mid)
        ld = _LOADERS.get(mid)
    if e is None:
        return {'open': False, 'mid': mid, 'pos': 0, 'total': 0, 'done': 0, 'rate': 0, 'error': ''}
    return {
        'open': True, 'mid': mid, 'pos': e.pos, 'total': e.total,
        'done': e.bytes, 'rate': e.rate, 'tail_done': e.tail_done,
        'stage': getattr(ld, 'stage', '') if ld else '', 'error': e.error,
    }


def close(mid: int):
    with _ENTRIES_LOCK:
        ld = _LOADERS.pop(mid, None)
        if ld:
            ld.stop.set()
        e = _ENTRIES.pop(mid, None)
    if e:
        e.close()


def start_sweeper():
    """Löscht Cache-Dateien, die seit IDLE_KILL_S nicht genutzt wurden."""
    def _loop():
        while True:
            time.sleep(60)
            now = time.time()
            with _ENTRIES_LOCK:
                dead = [mid for mid, e in list(_ENTRIES.items())
                        if now - e.last > IDLE_KILL_S and not e.dead]
                for mid in dead:
                    close(mid)
    threading.Thread(target=_loop, daemon=True).start()


def start_watchdog():
    """Neutzt haengende Head-Loader, die in einem Telegram-await feststecken."""
    _STALL_HARD_S = 45
    def _restart_stalled(mid):
        with _ENTRIES_LOCK:
            e = _ENTRIES.get(mid)
            if e is None:
                return
            ld = _LOADERS.get(mid)
            if ld is None or ld.stop.is_set() or e.dead:
                return
            ld.stop.set()
            _LOADERS.pop(mid, None)
        try:
            with e.cv:
                e.cv.notify_all()
        except Exception:
            pass
        if e.src:
            chat, msg = e.src
            ensure(mid, chat, msg, e.total, e.mime, cap=0)

    def _loop():
        while True:
            time.sleep(10)
            now = time.time()
            with _ENTRIES_LOCK:
                stuck = [mid for mid, ld in list(_LOADERS.items())
                         if ld.stage == "head" and not ld.stop.is_set()
                         and not ld.e.dead and now - ld.e.last_adv > _STALL_HARD_S]
            for mid in stuck:
                _restart_stalled(mid)
    threading.Thread(target=_loop, daemon=True).start()
