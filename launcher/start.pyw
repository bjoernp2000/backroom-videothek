"""Videothek starten: Server hochfahren + Browser oeffnen. 1-Klick per Doppelklick."""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
PORT = 8765


def port_free(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", p)) != 0


def main():
    silent = "--nobrowser" in sys.argv
    if not port_free(PORT):
        if not silent:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return
    from backend.app import create_app
    app = create_app()
    if getattr(sys.modules.get("__main__"), "__name__", "") == "__main__":
        if not silent:
            threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(f"http://127.0.0.1:{PORT}/"))).start()
        from waitress import serve
        serve(app, host="127.0.0.1", port=PORT, threads=12)


if __name__ == "__main__":
    main()
