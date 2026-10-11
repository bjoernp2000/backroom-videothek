"""Config-Layer: uebernimmt Telegram-Session + TMDB-Keys einmalig aus GH Infinity."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller-EXE: Daten neben der EXE, Assets aus dem Entarchiv
    ROOT = Path(sys.executable).resolve().parent
    ASSETS = Path(getattr(sys, "_MEIPASS", str(ROOT))) / "assets"
else:
    ROOT = Path(__file__).resolve().parent.parent
    ASSETS = ROOT
DATA_DIR = ROOT / "data"
POSTER_DIR = DATA_DIR / "posters"
DOWNLOAD_DIR = DATA_DIR / "downloads"
DB_PATH = DATA_DIR / "videothek.sqlite3"
CONFIG_PATH = DATA_DIR / "config.json"
FRONTEND = ASSETS / "frontend"
# Lokaler EXE-Betrieb (EXE-Liegt-Neben-Ordner = F:/Projekte/Videothek): das Live-
# frontend/ neben der EXE bevorzugen, damit Frontend-Aenderungen ohne stale Bundle
# ankommen. Fallback: eingebettetes assets/frontend aus dem PyInstaller-Archiv.
_live_fr = ROOT / "frontend"
if _live_fr.exists() and (_live_fr / "index.html").exists():
    FRONTEND = _live_fr

# Quellen aus dem Vorgaengerprojekt (nur Lesend, einmalig kopiert)
GH_OWNER_CFG = Path("F:/TG_BOT_PROJECTS/GH_INFINITY_PERSISTENT_DATA/owner_user_api.json")
GH_CINEFORGE_ENV = Path("F:/TG_BOT_PROJECTS/_app_betrieb/instances/cineforge/.env")

PORT = 8765


def _read_env_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def load_config() -> dict:
    """Laedt data/config.json; beim ersten Start wird aus GH Infinity uebernommen."""
    for d in (DATA_DIR, POSTER_DIR, DOWNLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)

    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}

    changed = False
    tg = cfg.get("tg") or {}
    if not (tg.get("api_id") and tg.get("api_hash") and tg.get("session")):
        if GH_OWNER_CFG.exists():
            try:
                src = json.loads(GH_OWNER_CFG.read_text(encoding="utf-8"))
                if src.get("api_id") and src.get("session"):
                    cfg["tg"] = {
                        "api_id": int(src["api_id"]),
                        "api_hash": src["api_hash"],
                        "session": src["session"],
                    }
                    changed = True
            except Exception:
                pass

    if not cfg.get("tmdb_key"):
        env = _read_env_file(GH_CINEFORGE_ENV)
        if env.get("TMDB_API_KEY"):
            cfg["tmdb_key"] = env["TMDB_API_KEY"]
            changed = True

    if not cfg.get("fanart_key"):
        env = _read_env_file(GH_CINEFORGE_ENV)
        if env.get("FANART_API_KEY"):
            cfg["fanart_key"] = env["FANART_API_KEY"]
            changed = True

    if changed:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            if os.name != "nt":
                CONFIG_PATH.chmod(0o600)
        except Exception:
            pass
    return cfg


CFG = load_config()

TG_OK = bool((CFG.get("tg") or {}).get("session"))
TMDB_OK = bool(CFG.get("tmdb_key"))
FANART_OK = bool(CFG.get("fanart_key"))
