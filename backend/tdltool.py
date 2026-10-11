"""Schnelle Telegram-Downloads ueber tdl (TDLib) statt Telethon.

Der Kern der Videothek-Leistung: Telethon wird von Telegram auf ~0.3 MB/s
gedrosselt (kein Premium-Pfad), tdl/TDLib (was Telegram Desktop nutzt) laeuft
bei Premium mit ~5 MB/s. Ein Video wird per tdl in den lokalen Cache geladen;
der Player spielt aus der fertigen Datei - ruckelfrei.

Session: data/tdl_session.json wird aus dem auth_key der StringSession gebaut
(tools/tdl_build_session.py). WICHTIG: der t.me/c/<id>/<msg>-Link muss zur
chat_id passen - die Link-Spalte in der DB war teils falsch, hier wird der
Link IMMER aus chat_id + message_id frisch gebaut.
"""
from __future__ import annotations

import glob
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import config
from . import tgauth

ROOT = config.ROOT
TDL = ROOT / "tools" / "bin" / "tdl.exe"
SESSION = ROOT / "data" / "tdl_session.json"        # Alte, feste Session (Fallback)
CACHE = ROOT / "data" / "tdl_cache"          # fertige Downloads landen hier

_LOCK = threading.Lock()
_JOBS: dict[int, dict] = {}                  # mid -> Job-Dict

MB = 1024 * 1024


def _creds() -> tuple[Path, str]:
    """(Session-Pfad, Namespace) des aktiven Accounts; Fallback: alte Session."""
    try:
        sp = tgauth.active_session()
        aa = tgauth.active_account()
        if sp and aa:
            return Path(sp), aa.get("label", "default")
    except Exception:
        pass
    return SESSION, "default"


def available() -> bool:
    return TDL.exists() and _creds()[0].exists()


# --- tdl --serve: schneller HTTP-Stream (offiziell dokumentiert) ---
# tdl dl -u <link> --serve startet einen HTTP-Server, der die Telegram-Datei
# direkt streamt statt sie erst komplett zu downloaden. Range-Requests ans
# Dateiende (moov) werden sofort bedient (~0.4s) - das ist der schnelle
# TDLib-Kanal, den Telegram Desktop nutzt, nicht der gedrosselte Telethon-
# Stream. Genau das loest die "Film startet erst nach Minuten"-Folter.
_SERVE_PORT = 8099
_SERVE_PROC: subprocess.Popen | None = None
_SERVE_URL: str | None = None
_SERVE_LOCK = threading.Lock()


def _serve_url(chat_id: int, message_id: int) -> str:
    """t.me/c/<public>/<msg> - Link fuer tdl --serve."""
    return f"https://t.me/c/{_public_id(chat_id)}/{int(message_id)}"


def _wait_port(timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", _SERVE_PORT), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def serve_start(url: str | None = None) -> bool:
    """Startet den persistenten tdl --serve HTTP-Server.

    tdl --serve braucht die Message-URL beim Start, sonst serviert er nichts.
    Bei einer NEUEN URL wird der Server neu gestartet (eine Datei pro Server),
    damit jede abgespielte Datei serviert wird.
    """
    global _SERVE_PROC, _SERVE_URL
    with _SERVE_LOCK:
        if _SERVE_PROC is not None and _SERVE_PROC.poll() is None \
                and _SERVE_URL == url:
            return True
        # alte Instanz beenden, wenn andere URL oder tot
        if _SERVE_PROC is not None:
            try:
                _SERVE_PROC.terminate()
            except Exception:
                pass
            _SERVE_PROC = None
        if not available():
            return False
        sess, ns = _creds()
        cmd = [str(TDL), "dl", "--serve", "--port", str(_SERVE_PORT),
               "--threads", "8", "--ns", ns, "--storage", f"type=file,path={sess}"]
        if url:
            cmd += ["-u", url]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            _SERVE_PROC = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags)
            _SERVE_URL = url
        except Exception:
            _SERVE_PROC = None
            _SERVE_URL = None
            return False
        _SERVE_URL = url
        if not _wait_port():
            try:
                _SERVE_PROC.terminate()
            except Exception:
                pass
            _SERVE_PROC = None
            _SERVE_URL = None
            return False
        return True


def serve_stream(chat_id: int, message_id: int) -> str | None:
    """Stream-URL ueber den tdl --serve-Server, oder None wenn nicht moeglich."""
    if not serve_start(_serve_url(chat_id, message_id)):
        return None
    return f"http://127.0.0.1:{_SERVE_PORT}/{_public_id(chat_id)}/{int(message_id)}"


def serve_stop() -> None:
    global _SERVE_PROC
    with _SERVE_LOCK:
        if _SERVE_PROC is not None:
            try:
                _SERVE_PROC.terminate()
            except Exception:
                pass
            _SERVE_PROC = None


def _public_id(chat_id: int) -> int:
    """chat_id -> öffentliche ID fuer den t.me/c/<id>/-Link.

    Kanaele haben den -100-Praefix (z.B. -1004416838190) -> Link nutzt
    4416838190. Grundgruppen haben negatives Vorzeichen ohne -100 -> abs().
    """
    cid = int(chat_id)
    s = str(cid)
    if s.startswith("-100"):
        return int(s[4:])
    return abs(cid)


def link(chat_id: int, message_id: int) -> str:
    return f"https://t.me/c/{_public_id(chat_id)}/{int(message_id)}"


def job(mid: int):
    with _LOCK:
        return _JOBS.get(mid)


def _JOB_SET(j, **kw):
    for k, v in kw.items():
        j[k] = v


def _out(mid: int) -> Path:
    return CACHE / f"{mid}.mp4"


def _dlpath(message_id: int) -> Path:
    """tdl schreibt <msg>.tmp waehrend des Ladens, <msg> nach Fertigstellung."""
    plain = CACHE / str(message_id)
    if plain.exists():
        return plain
    tmp = CACHE / f"{message_id}.tmp"
    return tmp if tmp.exists() else plain


def growing(mid: int, message_id: int) -> Path:
    """Aktuelle (ggf. wachsende) tdl-Datei fuer progressives Abspielen.

    Liefert den Pfad, der gerade beschrieben wird (bzw. schon fertig ist),
    ohne etwas anzulegen. None, wenn noch gar nichts da ist.
    """
    p = CACHE / str(message_id)
    if p.exists():
        return p
    t = CACHE / f"{message_id}.tmp"
    if t.exists():
        return t
    # fertige <mid>.mp4 (nach Umbenennung)
    m = CACHE / f"{mid}.mp4"
    return m if m.exists() else None


def _monitor(mid: int, outfile: Path, proc: subprocess.Popen):
    """Beobachtet die wachsende Datei und aktualisiert den Fortschritt."""
    j = job(mid)
    prev_size = 0
    prev_t = time.time()
    last = 0
    while proc.poll() is None and j and not j.get("stop"):
        # tdl schreibt zuerst <msg>.tmp, benennt dann zu <msg> um -> immer neu
        # aufloesen, welcher Name gerade existiert.
        if outfile.name.endswith(".tmp") and (CACHE / outfile.name[:-4]).exists():
            outfile = CACHE / outfile.name[:-4]
        elif not outfile.exists():
            tmp2 = CACHE / f"{outfile.name}.tmp"
            if tmp2.exists():
                outfile = tmp2
        try:
            size = outfile.stat().st_size if outfile.exists() else 0
        except OSError:
            size = 0
        now = time.time()
        if size > last:
            last = size
        if now - prev_t >= 3:
            rate = (size - prev_size) / (now - prev_t) / MB
            prev_size, prev_t = size, now
        else:
            rate = j.get("speed") or 0.0
        with _LOCK:
            _JOB_SET(j, done=last, speed=round(rate, 2),
                     frac=round(min(1.0, last / max(1, j["total"])), 4))
        time.sleep(0.8)
    # sicherstellen, dass die finale Groesse in den Job wandert
    if j:
        try:
            size = outfile.stat().st_size if outfile.exists() else 0
        except OSError:
            size = 0
        with _LOCK:
            _JOB_SET(j, done=size,
                     frac=round(min(1.0, size / max(1, j["total"])), 4))


def start(mid: int, chat_id: int, message_id: int, total: int,
          filename: str) -> dict:
    """Startet den tdl-Download als Hintergrund-Thread. Liefert den Job."""
    with _LOCK:
        if mid in _JOBS and _JOBS[mid].get("status") in ("running", "queued"):
            return _JOBS[mid]
        CACHE.mkdir(parents=True, exist_ok=True)
        out = _out(mid)
        j = {"mid": mid, "status": "queued", "done": 0, "total": int(total or 0),
             "speed": 0.0, "frac": 0.0, "error": "", "path": str(out),
             "stop": False, "filename": filename or f"{mid}.mp4"}
        _JOBS[mid] = j

    def _run():
        with _LOCK:
            j["status"] = "running"
        # Haeufiger Testfehler: ein verwaister tdl.exe aus einem frueheren Lauf
        # laedt noch mit ALTEM Dateinamen und verschmutzt den Cache. Wir
        # arbeiten einer nach dem anderen -> vor jedem Start alles Beende.
        try:
            subprocess.run(["taskkill", "/F", "/IM", "tdl.exe"],
                           capture_output=True)
        except Exception:
            pass
        time.sleep(0.5)
        if Path(j["path"]).exists() and (total or 0) and \
                Path(j["path"]).stat().st_size >= total:
            j["status"] = "done"
            j["frac"] = 1.0
            return
        url = link(chat_id, message_id)
        # --template nutzt die MessageID als Dateinamen -> eindeutig pro Job,
        # dann umbenennen auf <mid>.mp4 (sonst heisst die Datei Dialog_usw.)
        _sess, _ns = _creds()
        cmd = [str(TDL), "--storage", f"type=file,path={_sess}",
               "--ns", _ns,
               "download", "-u", url, "-d", str(CACHE),
               "--template", "{{ .MessageID }}",
               "--disable-progress-ps", "--threads", "4"]
        dl = CACHE / str(message_id)          # tdl: --template {{ .MessageID }} = kein Suffix
        # Konsolenprogramm UNSICHTBAR starten: ohne CREATE_NO_WINDOW oeffnet
        # tdl.exe auf Windows ein eigenes Terminal-Fenster (haesslich).
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=flags)
        except Exception as ex:
            j["status"] = "error"
            j["error"] = str(ex)[:200]
            return
        _monitor(mid, dl, proc)
        rc = proc.wait()
        if j.get("stop"):
            j["status"] = "canceled"
            return
        # tdl schreibt <msg> (ohne Endung) -> auf <mid>.mp4 legen
        src = dl if dl.exists() else CACHE / f"{message_id}"
        dst = Path(j["path"])
        final_size = 0
        try:
            if src.exists():
                src.replace(dst)
                final_size = dst.stat().st_size if dst.exists() else 0
        except OSError:
            final_size = dst.stat().st_size if dst.exists() else 0
        if rc == 0 and (final_size >= max(1, total or 0) or final_size > 0) \
                and dst.exists():
            j["status"] = "done"
            j["frac"] = 1.0
        else:
            j["status"] = "error"
            j["error"] = j.get("error") or f"tdl rc={rc} ({final_size} Bytes)"

    threading.Thread(target=_run, daemon=True, name=f"tdl-{mid}").start()
    return j


def cancel(mid: int):
    with _LOCK:
        j = _JOBS.get(mid)
        if j:
            j["stop"] = True


def path(mid: int):
    """Pfad der fertigen Datei, sonst None."""
    j = _JOBS.get(mid)
    if j and j.get("status") == "done":
        p = Path(j["path"])
        return p if p.exists() else None
    p = _out(mid)
    return p if p.exists() else None