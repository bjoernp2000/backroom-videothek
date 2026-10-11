"""VLC-Videoflaeche direkt IM App-Fenster — kein Zusatzfenster mehr.

Mechanik: ein echtes Win32-Kindfenster des pywebview-Fensters liegt per
SetWindowPos(HWND_TOP) UEBER dem WebView2-Content, exakt auf das HTML-Rechteck
von #vbox. WS_EX_TRANSPARENT laesst Mausklicks hindurch zum HTML fallen
(Klick aufs Bild = Play/Pause ueber den #vbox-Listener). Ein Poller holt alle
~200 ms via evaluate_js das Rechteck (CSS-px * devicePixelRatio) — Resize und
Vollbild wandern automatisch mit; alle ~8 Runden wird TOP revidiert, falls
WebView2 sich beim Resizing wieder drueber legt.

Kein Python-Callback in der Nachrichtenpfad-Routine: die Fensterklasse nutzt
die NATIVE DefWindowProcW — NULL Risiko, dass eine ctypes-Callback-Signatur
den Prozess mit reisst. Das Host-Fenster lebt auf einem eigenen Thread mit
eigener Pumpe (Mal-Nachrichten laufen dort).

Laeuft die App ohne pywebview-Fenster (reiner Browser), liefert hwnd() None —
backend/player.py faellt dann auf das alte Tk-Fenster zurueck.
"""
from __future__ import annotations

import atexit
import ctypes
import json
import threading
import time
from ctypes import wintypes

_window = None            # pywebview Window
_owner_ready = threading.Event()
_owner_want: list = []    # Guard: Owner-Thread genau einmal starten
_hwnd_holder = {"h": None}
_lock = threading.Lock()
_last_rect = None
_tick = 0

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_CHILD = 0x40000000
WS_CLIPSIBLINGS = 0x04000000
WS_EX_TRANSPARENT = 0x00000020
HWND_TOP = 0
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_HIDEWINDOW = 0x0080
GCLP_HBRBACKGROUND = -10
BLACK_BRUSH = 4

_rect_js = "JSON.stringify((window.__vrect&&window.__vrect())||null)"
_CLASS = "GhvtEmbedHost"


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG)]


for _fn, _res, _args in (
    ("CreateWindowExW", wintypes.HWND,
     (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      wintypes.HWND, wintypes.HWND, wintypes.HINSTANCE, wintypes.LPVOID)),
    ("DefWindowProcW", ctypes.c_ssize_t,
     (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)),
    ("RegisterClassW", wintypes.ATOM, (ctypes.c_void_p,)),
    ("SetWindowPos", wintypes.BOOL,
     (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.c_int, wintypes.UINT)),
    ("SetWindowLongPtrW", ctypes.c_ssize_t,
     (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)),
    ("LoadCursorW", wintypes.HANDLE, (wintypes.HINSTANCE, wintypes.LPCWSTR)),
    ("GetMessageW", ctypes.c_int, (ctypes.POINTER(MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT)),
    ("TranslateMessage", wintypes.BOOL, (ctypes.POINTER(MSG),)),
    ("DispatchMessageW", ctypes.c_ssize_t, (ctypes.POINTER(MSG),)),
    ("PostMessageW", wintypes.BOOL, (wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)),
):
    _f = getattr(user32, _fn)
    _f.restype = _res
    _f.argtypes = _args

gdi32.GetStockObject.restype = wintypes.HANDLE
gdi32.GetStockObject.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

WM_CLOSE = 0x0010


def set_window(window):
    """Vom Launcher aufgerufen: das pywebview-Fenster, in das eingebettet wird."""
    global _window
    _window = window


def attached() -> bool:
    return _window is not None


def _owner_loop():
    """Host-Fenster hier erzeugen UND diese Thread-Pumpe laufen lassen."""
    try:
        form = int(_window.native.Handle.ToInt64())
        hinst = kernel32.GetModuleHandleW(None)
        defproc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
        if not user32.RegisterClassW(ctypes.byref(WNDCLASSW(
                0, defproc, 0, 0, hinst, 0,
                user32.LoadCursorW(0, None),
                gdi32.GetStockObject(BLACK_BRUSH), None, _CLASS))) \
                and kernel32.GetLastError() != 1410:
            return
        h = user32.CreateWindowExW(
            WS_EX_TRANSPARENT, _CLASS, "vthk-video",
            WS_CHILD | WS_CLIPSIBLINGS,
            0, 0, 1, 1, form, 0, hinst, None)
        if not h:
            return
        _hwnd_holder["h"] = int(h)
    finally:
        _owner_ready.set()
    msg = MSG()
    while user32.GetMessageW(ctypes.byref(msg), 0, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def hwnd() -> int | None:
    if _window is None:
        return None
    with _lock:
        if _hwnd_holder["h"] is None and not _owner_want:
            _owner_want.append(1)
            threading.Thread(target=_owner_loop, daemon=True,
                             name="embed-host").start()
    if not _owner_ready.wait(3.0):
        return None
    return _hwnd_holder["h"]


def place(x: float, y: float, w: float, h: float, dpr: float):
    global _last_rect, _tick
    box = (int(x * dpr), int(y * dpr), max(1, int(w * dpr)), max(1, int(h * dpr)))
    changed = box != _last_rect
    _last_rect = box
    _tick += 1
    if not changed and _tick % 8:
        return                       # unveraendert -> kein Zittern im Web-Rendering
    host = _hwnd_holder["h"]
    if host:
        user32.SetWindowPos(host, HWND_TOP, box[0], box[1], box[2], box[3],
                            SWP_NOACTIVATE | SWP_SHOWWINDOW)


def hide():
    global _last_rect
    _last_rect = None
    host = _hwnd_holder["h"]
    if host:
        user32.SetWindowPos(host, HWND_TOP, 0, 0, 1, 1,
                            SWP_NOACTIVATE | SWP_HIDEWINDOW)


def start_poller():
    """Haelt den Video-Host auf dem #vbox-Rechteck, solange die UI es zeigt."""

    def _loop():
        while True:
            time.sleep(0.2)
            if _window is None or _hwnd_holder["h"] is None:
                continue
            try:
                raw = _window.evaluate_js(_rect_js)
            except Exception:
                continue
            if not raw:
                hide()
                continue
            try:
                r = json.loads(raw)
            except Exception:
                continue
            if r:
                place(r["x"], r["y"], r["w"], r["h"], r.get("dpr", 1.0))
            else:
                hide()

    threading.Thread(target=_loop, daemon=True, name="embed-poller").start()
    atexit.register(dispose)


def info() -> dict:
    rect = None
    if _window is not None and _hwnd_holder["h"] is not None:
        try:
            raw = _window.evaluate_js(_rect_js)
            rect = json.loads(raw) if raw else None
        except Exception:
            rect = "js-error"
    return {"attached": attached(), "hwnd": bool(_hwnd_holder["h"]),
            "rect": rect or _last_rect}


def dispose():
    host = _hwnd_holder["h"]
    if host:
        try:
            user32.PostMessageW(host, WM_CLOSE, 0, 0)
        except Exception:
            pass
