"""Videothek EXE: echter App-Fenster-Modus (WebView2) mit eingebettetem Server."""
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

PORT = 8765


def _startup_log(message: str):
    try:
        p = ROOT / "data" / "_startup.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\\n")
    except Exception:
        pass


def port_free(p: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", p)) != 0


def _serve():
    try:
        _startup_log("server: start")
        from backend.app import create_app
        app = create_app()
        from waitress import serve
        _startup_log("server: waitress")
        serve(app, host="127.0.0.1", port=PORT, threads=12)
    except Exception:
        _startup_log("server: exception\\n" + traceback.format_exc())
        raise


def main():
    _startup_log(f"main: start frozen={getattr(sys, 'frozen', False)} root={ROOT}")
    if port_free(PORT):
        threading.Thread(target=_serve, daemon=True).start()
        for _ in range(60):
            if not port_free(PORT):
                break
            time.sleep(0.25)

    import webview
    win = webview.create_window(
        "Videothek", f"http://127.0.0.1:{PORT}/",
        width=1280, height=800, min_size=(760, 520),
        fullscreen=True, background_color="#0b0e14", text_select=True)

    def toggle_fs():
        try:
            win.toggle_fullscreen()
        except Exception:
            pass

    def quit_app():
        try:
            win.destroy()
        except Exception:
            os._exit(0)

    try:
        win.expose(toggle_fs)
        win.expose(quit_app)
    except Exception:
        pass
    try:
        from backend import embed
        embed.set_window(win)
        embed.start_poller()
    except Exception:
        pass
    webview.start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _startup_log("main: exception\\n" + traceback.format_exc())
        raise
