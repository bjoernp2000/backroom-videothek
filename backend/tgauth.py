"""Telegram-Account-Verwaltung fuer die Videothek (Multi-Account + Switch).

Jeder Account = eigene tdl-Session-Datei unter data/accounts/<label>.json.
Login neuer Accounts ueber tdl (Handy+Code vom Code-Login), Speichern als
file-storage-Session, purzeln in einem Manifest. Ein Account ist aktiv —
tdltool nutzt dessen Session.

Bestehende Session (data/tdl_session.json) wird beim ersten Lauf einmalig als
Account "default" uebernommen, damit alles weiter funktioniert.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from . import config

ACCOUNTS_DIR = config.DATA_DIR / "accounts"
META_PATH = ACCOUNTS_DIR / "_meta.json"
LEGACY_SESSION = config.ROOT / "data" / "tdl_session.json"   # alte, feste Session

TDL = config.ROOT / "tools" / "bin" / "tdl.exe"

_LOCK = threading.Lock()
# laufende Login-Prozesse: label -> {"proc","pipe","t0"}
_PEND: dict[str, dict] = {}


def _meta() -> dict:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"accounts": {}, "active": None}


def _save(m):
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def _session_path(label: str) -> Path:
    return ACCOUNTS_DIR / f"{label}.json"


def _ensure_default():
    """Einmalig: bestehende tdl_session.json als Account 'default' uebernehmen."""
    with _LOCK:
        m = _meta()
        if LEGACY_SESSION.exists() and "default" not in m["accounts"]:
            ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
            dst = _session_path("default")
            dst.write_bytes(LEGACY_SESSION.read_bytes())
            m["accounts"]["default"] = {
                "file": "default.json", "name": "Default",
                "source": "legacy",
            }
            if not m.get("active"):
                m["active"] = "default"
            _save(m)


def list_accounts() -> list[dict]:
    _ensure_default()
    m = _meta()
    active = m.get("active")
    out = []
    for label, info in m.get("accounts", {}).items():
        row = dict(info)
        row["label"] = label
        row["active"] = label == active
        row["exists"] = _session_path(label).exists()
        if not row.get("name"):
            row["name"] = label
        out.append(row)
    return out


def active_session() -> Path | None:
    """Pfad der aktiven Session (oder None)."""
    _ensure_default()
    m = _meta()
    label = m.get("active")
    if not label:
        return None
    p = _session_path(label)
    return p if p.exists() else None


def active_account() -> dict | None:
    m = _meta()
    label = m.get("active")
    if not label:
        return None
    info = m.get("accounts", {}).get(label)
    return None if info is None else {**info, "label": label}


def set_active(label: str):
    with _LOCK:
        m = _meta()
        if label not in m.get("accounts", {}):
            raise KeyError(f"unbekannter Account: {label}")
        if not _session_path(label).exists():
            raise KeyError(f"Session fuer {label} fehlt")
        m["active"] = label
        _save(m)


def login_start(label: str, phone: str):
    """Startet einen tdl-Code-Login und reicht die Handynummer hinein.

    Der Prozess promptet danach nach dem Code; _PEND[label] haelt ihn fest.
    """
    with _LOCK:
        m = _meta()
        if label in m.get("accounts", {}) and _session_path(label).exists():
            raise RuntimeError(f"Account '{label}' existiert schon")
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        sf = _session_path(label)
        # tdl-Namespace je Account isolieren
        cmd = [str(TDL), "--storage", f"type=file,path={sf}",
               "--ns", label, "login", "--type", "code"]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        _PEND[label] = {"proc": proc, "t0": time.time(), "buf": ""}
    time.sleep(0.6)
    _write(label, f"{phone}\n")
    return {"label": label, "started": True}


def _write(label: str, s: str):
    pnd = _PEND.get(label)
    if pnd and pnd["proc"].stdin:
        pnd["proc"].stdin.write(s)
        pnd["proc"].stdin.flush()


def login_code(label: str, code: str):
    pnd = _PEND.get(label)
    if not pnd:
        raise RuntimeError("kein laufender Login fuer " + label)
    _write(label, f"{code}\n")
    proc = pnd["proc"]
    out, err = "", ""
    try:
        out, err = proc.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = pnd["buf"] + (proc.stdout.read() if proc.stdout else "")
    me = None
    with _LOCK:
        m = _meta()
        ok = proc.returncode == 0 and not any(
            w in (out or "").lower() for w in ("incorrect", "invalid code",
                                                "wrong", "error:"))
        if ok:
            # Anzeigename = das vom Nutzer gewaehlte Label (tdl hat kein whoami).
            me = {"name": label, "guest": True}
            m.setdefault("accounts", {})[label] = {
                "file": f"{label}.json", "name": label,
                "me": me,
            }
            _save(m)
        _PEND.pop(label, None)
    return {"label": label, "ok": ok, "who": (me if ok else None)}


def refresh_whoami(label: str) -> dict | None:
    with _LOCK:
        m = _meta()
        info = m.get("accounts", {}).get(label)
        if not info:
            return None
        return info.get("me") or {"name": label}


def logout(label: str):
    with _LOCK:
        m = _meta()
        if label not in m.get("accounts", {}):
            raise KeyError(label)
        p = _session_path(label)
        for suf in ("", "-wal", "-shm"):
            try:
                q = Path(str(p) + suf)
                if q.exists():
                    q.unlink()
            except OSError:
                pass
        del m["accounts"][label]
        if m.get("active") == label:
            m["active"] = next(iter(m["accounts"]), None)
        _save(m)