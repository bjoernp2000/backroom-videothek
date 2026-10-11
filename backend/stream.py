"""Live-Streaming aus Telegram: Multi-Slot-Pipelining wie offizielle Clients.

Ein einzelner iter_download-Strang ist durch die Round-Trip-Zeit begrenzt (~0.3-1
MB/s zur Media-DC). Offizielle Telegram-Clients pfeifen deshalb viele Anfragen
gleichzeitig durch die Leitung. Genau das tut der Stream jetzt: 6 Arbeiter holen
je 1-MB-Slot parallel, der Consumer liefert sie in Dateireihenfolge ans Video
 weiter, inkl. Vor-Puffer, damit der Start ohne Wackeln loslaeuft.
"""
from __future__ import annotations

import asyncio
import threading
import time

from . import telegram

KB = 1024
MB = 1024 * KB
ALIGN = 4096
CHUNK = 512 * KB
SLOT = 2 * MB
WORKERS = 6
CAP_BYTES = 64 * MB
PRE_START = 2 * MB
PRE_START_S = 2.0

ACTIVE = [0]                 # offene Player-Streams -> Scanner drosselt sich
_act = threading.Lock()


def _count(delta: int) -> None:
    with _act:
        ACTIVE[0] = max(0, ACTIVE[0] + delta)


def _setup(chat_id: int, message_id: int):
    def _make():
        async def _go():
            c = telegram.client()
            if not c.is_connected():
                await c.connect()
            entity = await asyncio.get_event_loop().run_in_executor(
                None, telegram.resolve, int(chat_id))
            msg = await c.get_messages(entity, ids=int(message_id))
            if msg is None:
                raise RuntimeError("Nachricht nicht mehr auffindbar")
            if not getattr(msg, "media", None):
                raise RuntimeError("Kein Medium an der Nachricht")
            doc = getattr(msg.media, "document", None)
            return c, msg.media, int(getattr(doc, "size", 0) or 0)
        return _go()

    return telegram._runx(_make, 90)


class _Slots:
    """Slot-Pipeline: Arbeiter holen Slots, Consumer bekommt sie geordnet."""

    def __init__(self, nslots: int):
        self.n = nslots
        self.queues: dict[int, list] = {}
        self.seg_done: set[int] = set()
        self.next_slot = 0
        self.buf = 0
        self.out = 0
        self.stop = threading.Event()
        self.cv = threading.Condition()
        self.t0 = time.time()
        self.delivered = 0

    def take(self):
        with self.cv:
            if self.stop.is_set() or self.next_slot >= self.n:
                return None
            k = self.next_slot
            self.next_slot += 1
            return k

    def fail_slot(self, k: int):
        """Slot nach Fehler erneut anstellen (an den Anfang der Queue)."""
        with self.cv:
            self.next_slot = min(self.next_slot, k)
            self.cv.notify_all()

    async def apush(self, k: int, data: bytes) -> bool:
        while True:
            if self.stop.is_set():
                return False
            with self.cv:
                if self.stop.is_set():
                    return False
                # Segment 'out' kann der Consumer sofort weiterreichen -> immer erlaubt
                if k == self.out or self.buf + len(data) <= CAP_BYTES:
                    self.queues.setdefault(k, []).append(data)
                    self.buf += len(data)
                    self.cv.notify_all()
                    return True
            await asyncio.sleep(0.15)

    def finish(self, k: int):
        with self.cv:
            self.seg_done.add(k)
            self.cv.notify_all()

    def pop(self, deadline: float):
        """Nächstes Byte-Stück in Dateireihenfolge (Vor-Puffer bis deadline)."""
        with self.cv:
            while True:
                if self.stop.is_set():
                    return None
                if not self.delivered and time.time() - self.t0 > 60:
                    # Pipeline tot (FloodWait/verbrannte Verbindung): laut
                    # abbrechen statt still zu warten — Loader stellt neu an.
                    self.stop.set()
                    raise ConnectionError("no-bytes-60s")
                q = self.queues.get(self.out)
                if q:
                    first = self.out == 0 and time.time() >= deadline
                    if self.buf >= min(PRE_START, CAP_BYTES) or first or self.out > 0:
                        data = q.pop(0)
                        self.buf -= len(data)
                        self.delivered += 1
                        if not q and self.out in self.seg_done:
                            del self.queues[self.out]
                            self.out += 1
                        self.cv.notify_all()
                        return data
                elif self.out in self.seg_done:
                    self.out += 1
                    if self.out >= self.n:
                        return None
                    continue
                self.cv.wait(timeout=0.4)


def _sequential(c, media, offset, total):
    """Kleine Dateien: ein Strang reicht."""
    from collections import deque

    q: deque = deque()
    cv = threading.Condition()
    stop = threading.Event()

    async def _feed():
        try:
            async for chunk in c.iter_download(media, offset=offset, chunk_size=CHUNK,
                                             file_size=total):
                data = bytes(chunk)
                with cv:
                    while len(q) > 64 and not stop.is_set():
                        cv.wait(0.2)
                    if stop.is_set():
                        break
                    q.append(data)
                    cv.notify_all()
        except Exception:
            pass
        finally:
            with cv:
                q.append(None)
                cv.notify_all()

    def _iter():
        try:
            while True:
                with cv:
                    while not q:
                        cv.wait(90)
                    item = q.popleft()
                    cv.notify_all()
                if item is None:
                    return
                yield item
        finally:
            stop.set()

    fut = telegram.submit(_feed())
    _count(+1)

    def _gen():
        try:
            yield from _iter()
        finally:
            fut.cancel()
            _count(-1)

    return _gen(), total


def remote_size(chat_id: int, message_id: int) -> int:
    """Liest die von Telegram gemeldete Originalgröße des Mediums."""
    _, _, total = _setup(chat_id, message_id)
    return int(total or 0)


def open_stream(chat_id: int, message_id: int, offset: int = 0):
    """(generator, total_bytes) — Bytes ab offset, parallel vorausgeladen."""
    c, media, total = _setup(chat_id, message_id)
    offset -= offset % ALIGN
    remaining = max(0, total - offset) if total else 0

    if remaining <= 40 * MB or not total:
        return _sequential(c, media, offset, total)

    nslots = -(-remaining // SLOT)
    state = _Slots(nslots)

    async def _worker():
        while not state.stop.is_set():
            k = state.take()
            if k is None:
                return
            o = offset + k * SLOT
            s = min(SLOT, total - o)
            got = 0
            tries = 0
            ok = False
            while tries < 5 and not state.stop.is_set():
                try:
                    async for chunk in c.iter_download(media, offset=o + got, chunk_size=CHUNK,
                                                     file_size=total):
                        data = bytes(chunk)
                        data = data[: max(0, s - got)]
                        if not data:
                            break
                        got += len(data)
                        if not await state.apush(k, data):
                            return
                        if got >= s:
                            ok = True
                            break
                    if ok or got >= s:
                        break
                except Exception:
                    tries += 1
                    await asyncio.sleep(0.8 + tries)
            if ok or got >= s:
                state.finish(k)
            else:
                # Worker kapituliert: Slot zurueckeinreichen statt Loch still zu
                # überspringen — Consumer wartet, andere Worker versuchen weiter.
                state.fail_slot(k)
                await asyncio.sleep(2.0)

    futures = [telegram.submit(_worker()) for _ in range(min(WORKERS, nslots))]

    def _iter():
        ready_deadline = time.time() + PRE_START_S  # Pre-Start max Xs, dann Teilmenge
        _count(+1)
        try:
            while True:
                data = state.pop(ready_deadline)
                ready_deadline = 0.0  # gilt nur fuer das erste Stueck
                if data is None:
                    return
                yield data
        finally:
            state.stop.set()
            with state.cv:
                state.cv.notify_all()
            for f in futures:
                f.cancel()
            _count(-1)

    return _iter(), total
