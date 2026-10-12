"""In-App-Player-Fenster: eigene Tk-Oberfläche, libVLC (VLC-Komponente) als Engine.

Ein Klick auf Ansehen -> das Fenster geöffnet, sofort spielbar — ganz gleich ob
MKV/HEVC/AC3/DTS. Kein externes vlc.exe, keine Fehlerdialogboxen: unser Fenster,
unsere Steuerung, nur die VLC-Engine steckt drin. Seek läuft über unsere
Range-fähige Stream-URL, lokal vorhandene Dateien direkt von Platte.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from pathlib import Path

_VLC_DIR = Path(r"C:\Program Files\VideoLAN\VLC")
_VLC_PLUGINS = _VLC_DIR / "plugins"
os.environ["VLC_PLUGIN_PATH"] = str(_VLC_PLUGINS)
os.environ["PATH"] = str(_VLC_DIR) + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory") and _VLC_DIR.exists():
    os.add_dll_directory(str(_VLC_DIR))

import tkinter as tk
from tkinter import ttk
import vlc

from . import cache, config, db, tdltool

_VLC_PLUGINS = Path(r"C:\Program Files\VideoLAN\VLC\plugins")

_Q: queue.Queue | None = None
_player = None
_T: threading.Thread | None = None
_START_LOCK = threading.Lock()
# Ein einziger libVLC-Player fuer das eingebettete App-Fenster. Der alte
# Player wird vor jedem Quellenwechsel synchron getrennt, damit kein alter
# HTTP-Stream den zweiten Start blockiert.
_VLC_INSTANCE = None
_EMBEDDED_MP = None
_EMBEDDED_MEDIA = None
_EMBEDDED_SRC = ""
_EMBEDDED_TITLE = ""
_EMBEDDED_LIVE = False
_LIVE_WATCHDOG_STARTED = False


def _fmt(ms: int) -> str:
    s = max(0, int(ms // 1000))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return (f"{h}:" if h else "") + f"{m:02d}:{s:02d}"


class NetflixStylePlayer:
    """Netflix-ähnlicher Player mit vollständigem Netflix-Design"""
    
    # Netflix-Designfarben
    BACKGROUND = "#141414"  # Dunkelgrau (Netflix-Hauptfarbe)
    CONTROLS_BG = "#1F1F1F"  # Noch dunkler für Controls
    TEXT_PRIMARY = "#FFFFFF"  # Weiß für Text
    TEXT_SECONDARY = "#B3B3B3"  # Grau für sekundären Text
    ACCENT = "#E50914"  # Netflix Rot für Highlights
    HOVER = "#F50000"  # Hover-Farbe
    TRANSPARENT = "transparent"
    
    # Netflix-ähnliche Schriftarten
    FONT_REGULAR = ("Segoe UI", 11)
    FONT_BOLD = ("Segoe UI", 11, "bold")
    FONT_MEDIUM = ("Segoe UI", 12, "bold")
    
    def __init__(self, root: tk.Tk, inst: "vlc.Instance"):
        self.root = root
        self.inst = inst
        self.mp = inst.media_player_new()
        self.title = ""
        self.current_media = None
        
        # Netflix-ähnliche Fensterkonfiguration
        root.configure(bg=self.BACKGROUND)
        root.geometry("1280x720")  # Netflix Standardauflösung
        root.minsize(480, 300)
        root.title("Videothek - Player")
        root.resizable(True, True)
        
        # Hauptframe für Video
        self.video_frame = tk.Frame(root, bg=self.BACKGROUND)
        self.video_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        # Video Canvas
        self.canvas = tk.Canvas(self.video_frame, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        # Überlagernde Steuerungsleiste
        self.control_bar = tk.Frame(root, bg=self.CONTROLS_BG, height=70)
        self.control_bar.pack(fill="x", side="bottom")
        self.control_bar.pack_propagate(False)
        
        # Widgets für Player-Controls
        self._setup_netflix_player_controls()
        
        # Status anzeigen
        self._setup_status_display()
        
        # UI-Verwaltung
        self._fs = False
        self._last_ui = 0.0
        self._ui_visible = True
        self._idle_timer = None
        
        # Tastaturbelegungen
        root.bind("f", lambda e: self.fullscreen())
        root.bind("F", lambda e: self.fullscreen())
        root.bind("<Escape>", lambda e: self.exit_fs_or_close())
        root.bind("<space>", lambda e: self.toggle())
        root.bind("<Left>", lambda e: self.skipto(-10))
        root.bind("<Right>", lambda e: self.skipto(10))
        root.bind("<Up>", lambda e: (self.vol.set(min(120, self.vol.get() + 5)),
                                     self.mp.audio_set_volume(self.vol.get())))
        root.bind("<Down>", lambda e: (self.vol.set(max(0, self.vol.get() - 5)),
                                       self.mp.audio_set_volume(self.vol.get())))
        root.bind("<Button-1>", self._toggle_ui)  # Klick auf Video versteckt/zeigt UI
        root.protocol("WM_DELETE_WINDOW", self.stop_close)
        
        # Hover-Effekte
        self._setup_hover_effects()
        
        # Aktualisierungsticker
        self._ticker()
        
    def _setup_hover_effects(self):
        """Netflix-ähnliche Hover-Effekte"""
        # Effekte für alle Buttons
        buttons = [self.b_play, self.b_rewind, self.b_forward, 
                  self.b_fullscreen, self.b_close, self.b_exit]
        
        for btn in buttons:
            try:
                btn.bind("<Enter>", lambda e, b=btn: self._on_button_hover(b, True))
                btn.bind("<Leave>", lambda e, b=btn: self._on_button_hover(b, False))
            except:
                pass  # Einige Elemente könnten noch nicht vorhanden sein
    
    def _on_button_hover(self, button, enter):
        """Hover-Effekt für Buttons"""
        if enter:
            button.config(bg="#333333" if button != self.b_play else self.ACCENT)
        else:
            button.config(bg=self.CONTROLS_BG)
    
    def _setup_netflix_player_controls(self):
        """Netflix-ähnliche Steuerung erstellen"""
        # Hauptsteuerungsframe
        controls_frame = tk.Frame(self.control_bar, bg=self.CONTROLS_BG)
        controls_frame.pack(fill="x", padx=20, pady=10)
        
        # Links: Play/Pause und Navigation
        left_controls = tk.Frame(controls_frame, bg=self.CONTROLS_BG)
        left_controls.pack(side="left")
        
        # Media Controls
        self.b_play = tk.Button(
            left_controls, 
            text="⏸",  # Start mit Pause-Icon
            command=self.toggle, 
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_PRIMARY,
            activebackground=self.ACCENT, 
            activeforeground="#FFFFFF",
            relief="flat", 
            font=self.FONT_BOLD, 
            padx=10,
            width=4,
            highlightthickness=0
        )
        self.b_play.pack(side="left", padx=(0, 10))
        
        # Zurück-Button
        self.b_rewind = tk.Button(
            left_controls,
            text="⏮", 
            command=lambda: self.skipto(-10),
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_SECONDARY,
            activebackground=self.CONTROLS_BG, 
            activeforeground=self.ACCENT,
            relief="flat", 
            font=("Segoe UI", 12), 
            padx=10,
            width=3,
            highlightthickness=0
        )
        self.b_rewind.pack(side="left", padx=(0, 10))
        
        # Vorwärts-Button
        self.b_forward = tk.Button(
            left_controls,
            text="⏭", 
            command=lambda: self.skipto(10),
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_SECONDARY,
            activebackground=self.CONTROLS_BG, 
            activeforeground=self.ACCENT,
            relief="flat", 
            font=("Segoe UI", 12), 
            padx=10,
            width=3,
            highlightthickness=0
        )
        self.b_forward.pack(side="left", padx=(0, 10))
        
        # Zeitanzeige
        time_frame = tk.Frame(left_controls, bg=self.CONTROLS_BG)
        time_frame.pack(side="left")
        
        self.time_label = tk.Label(
            time_frame, 
            text="00:00 / 00:00", 
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_SECONDARY, 
            font=self.FONT_REGULAR
        )
        self.time_label.pack(side="left")
        
        # Seek Bar (Netflix-Stil)
        self.seek_frame = tk.Frame(left_controls, bg=self.CONTROLS_BG)
        self.seek_frame.pack(side="left", padx=(15, 10), fill="x", expand=True)
        
        # Seek Slider mit Netflix-Effekten
        self.seek = tk.Scale(
            self.seek_frame,
            from_=0, 
            to=1000, 
            orient="horizontal", 
            showvalue=False,
            bg=self.CONTROLS_BG, 
            troughcolor="#333333", 
            highlightthickness=0,
            activebackground=self.ACCENT, 
            sliderrelief="flat", 
            length=260,
            width=8,
            highlightcolor="transparent"
        )
        self.seek.pack(fill="x", expand=True, pady=5)
        self.seek.bind("<ButtonRelease-1>", self._on_seek)
        
        # Rechts: Lautstärke und Vollbild
        right_controls = tk.Frame(controls_frame, bg=self.CONTROLS_BG)
        right_controls.pack(side="right")
        
        # Lautstärke und Vollbild 
        vol_frame = tk.Frame(right_controls, bg=self.CONTROLS_BG)
        vol_frame.pack(side="right", padx=(0, 10))
        
        # Lautstärke Icon
        self.vol_label = tk.Label(
            vol_frame, 
            text="🔊", 
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_SECONDARY, 
            font=("Segoe UI", 12)
        )
        self.vol_label.pack(side="left", padx=(0, 5))
        
        # Lautstärke Slider
        self.vol = tk.Scale(
            vol_frame,
            from_=0, 
            to=120, 
            showvalue=False, 
            orient="horizontal",
            bg=self.CONTROLS_BG, 
            troughcolor="#333333", 
            highlightthickness=0,
            activebackground=self.ACCENT, 
            sliderrelief="flat", 
            length=80,
            width=8,
            command=lambda v: self.mp.audio_set_volume(int(v)),
            highlightcolor="transparent"
        )
        self.vol.set(100)
        self.vol.pack(side="left", padx=(0, 5))
        
        # Vollbild Button
        self.b_fullscreen = tk.Button(
            right_controls,
            text="⛶", 
            command=self.fullscreen, 
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_SECONDARY,
            activebackground=self.CONTROLS_BG, 
            activeforeground=self.ACCENT,
            relief="flat", 
            font=("Segoe UI", 12), 
            padx=8,
            width=3,
            highlightthickness=0
        )
        self.b_fullscreen.pack(side="right", padx=(0, 5))
        
        # Beenden Button
        self.b_exit = tk.Button(
            right_controls,
            text="🚪", 
            command=self.exit_app, 
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_SECONDARY,
            activebackground=self.HOVER, 
            activeforeground="#FFFFFF",
            relief="flat", 
            font=("Segoe UI", 12), 
            padx=8,
            width=3,
            highlightthickness=0
        )
        self.b_exit.pack(side="right", padx=(0, 5))
        
        # Schließen Button
        self.b_close = tk.Button(
            right_controls,
            text="✕", 
            command=self.stop_close, 
            bg=self.CONTROLS_BG, 
            fg=self.TEXT_SECONDARY,
            activebackground=self.HOVER, 
            activeforeground="#FFFFFF",
            relief="flat", 
            font=("Segoe UI", 12), 
            padx=8,
            width=3,
            highlightthickness=0
        )
        self.b_close.pack(side="right")
        
        # Status indicator
        self.status_indicator = tk.Label(
            right_controls,
            text="",
            bg=self.CONTROLS_BG,
            fg=self.ACCENT,
            font=self.FONT_BOLD
        )
        self.status_indicator.pack(side="right", padx=(0, 10))
        
    def _setup_status_display(self):
        """Status-Anzeige für Player-Informationen"""
        # Informationen über den gerade gespielten Film
        self.info_frame = tk.Frame(self.control_bar, bg=self.CONTROLS_BG)
        self.info_frame.pack(side="left", padx=20, pady=10)
        
        self.title_label = tk.Label(
            self.info_frame,
            text="",
            bg=self.CONTROLS_BG,
            fg=self.TEXT_PRIMARY,
            font=self.FONT_MEDIUM
        )
        self.title_label.pack(side="left")
        
    def _toggle_ui(self, event=None):
        """UI ein/ausblenden bei Klick auf Video"""
        self._ui_visible = not self._ui_visible
        if self._ui_visible:
            # UI sichtbar machen
            self.control_bar.pack(fill="x", side="bottom")
            # Status anzeigen
            self._show_playing_status()
        else:
            # UI ausblenden
            self.control_bar.pack_forget()
            
    def _show_ui(self):
        """UI anzeigen"""
        if not self._ui_visible:
            self.control_bar.pack(fill="x", side="bottom")
            self._ui_visible = True
            self._show_playing_status()
            
    def _hide_ui(self):
        """UI ausblenden"""
        if self._ui_visible:
            self.control_bar.pack_forget()
            self._ui_visible = False
            
    def _show_playing_status(self):
        """Playing Status anzeigen"""
        self.status_indicator.config(text="Läuft")
        self.status_indicator.pack(side="right", padx=(0, 10))
        
    def _on_seek(self, event=None):
        """Seek-Funktion mit Netflix-Animation"""
        if self.mp:
            value = self.seek.get()
            # Einstellen der Seek-Position in Sekunden
            duration = self.mp.get_length()
            if duration > 0:
                new_time = (value / 1000) * duration
                self.mp.set_time(int(new_time))
                
    def _play(self, src: str, title: str):
        """Video abspielen mit Netflix-Design"""
        # EIN Fenster, EIN Bild: dem ALTEN Player sofort Anzeige + Ton entziehen,
        # dann den neuen starten. Das harte stop() des Alten läuft im
        # Hintergrund weiter (es blockiert bis libVLCs Input-Thread endet —
        # synchron würde das unsere UI einfrieren, asynchron sieht/hoert der
        # Nutzer aber nur noch den neuen Film). Das war der 'immer Ghost War'-
        # Bug: der alte Live-Player hielt das Fenster, während der neue noch
        # 'Opening' war.
        
        hwnd = self.canvas.winfo_id()
        self.title = title
        self.root.title(f"Videothek - {title or 'Video'}")
        
        # Titel in der Statuszeile anzeigen
        self.title_label.config(text=title or "Unbekannter Film")
        
        if not self.root.winfo_viewable():
            self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

        def _go():
            old = self.mp            # erst JETZT binden: letzter Player zaehlt
            try:
                old.set_hwnd(0)          # Anzeige sofort abgekoppelt
            except Exception:
                pass
            try:
                old.audio_set_mute(True)
            except Exception:
                pass
            m = (self.inst.media_new_path(src) if not src.startswith("http")
                 else self.inst.media_new_location(src))
            mp = self.inst.media_player_new()
            mp.set_media(m)
            try:
                mp.set_hwnd(hwnd)        # Ziel-hwnd VOR play (kein VLC-Eigenfenster)
            except Exception:
                pass
            mp.play()
            self.mp = mp
            # altes Media im Hintergrund zu Grabe tragen
            def _shutdown():
                try:
                    old.stop()
                except Exception:
                    pass
                try:
                    old.release()
                except Exception:
                    pass
            threading.Thread(target=_shutdown, daemon=True).start()

        self.root.after(0, _go)
        
    def toggle(self):
        """Play/Pause togglen mit Netflix-Effekt"""
        if self.mp:
            st = self.mp.get_state()
            if st in (vlc.State.Playing, vlc.State.Paused):
                self.mp.pause()
                self.b_play.config(text="▶")
                # Status anpassen
                if self._ui_visible:
                    self.status_indicator.config(text="Pause")
            else:
                self.mp.play()
                self.b_play.config(text="⏸")
                # Status anpassen
                if self._ui_visible:
                    self.status_indicator.config(text="Läuft")
                
    def fullscreen(self):
        """Vollbildmodus umschalten"""
        if self._fs:
            self.root.state('normal')
            self.root.geometry("1280x720")
            self._fs = False
        else:
            self.root.state('zoomed')
            self._fs = True
            
    def exit_fs_or_close(self):
        """Vollbild verlassen oder Fenster schließen"""
        if self._fs:
            self.fullscreen()
        else:
            self.stop_close()
            
    def skipto(self, seconds: int):
        """Zu einer bestimmten Zeit springen"""
        if self.mp:
            current_time = self.mp.get_time()
            new_time = current_time + (seconds * 1000)
            # Bounds checking
            duration = self.mp.get_length()
            if duration > 0:
                new_time = max(0, min(new_time, duration))
                self.mp.set_time(new_time)
                
    def stop_close(self):
        """Player stoppen und Fenster schließen"""
        if self.mp:
            self.mp.stop()
            self.mp.release()
        self.root.destroy()
        
    def exit_app(self):
        """Vollständige Anwendung beenden"""
        # Zuerst den Player schließen
        self.stop_close()
        # Zusätzliche Cleanup-Operationen
        try:
            # Beende alle Hintergrundthreads
            global _T
            if _T and _T.is_alive():
                _T.join(timeout=1)
        except Exception:
            pass
            
    def _ticker(self):
        """Aktualisierung des Player-Status mit Netflix-Bildschirm"""
        if self.mp:
            try:
                # Aktualisiere Zeitanzeige
                current = self.mp.get_time()
                duration = self.mp.get_length()
                
                if duration > 0:
                    # Berechne Prozentsatz
                    percent = (current / duration) * 1000
                    self.seek.set(int(percent))
                    
                    # Aktualisiere Labels
                    current_str = _fmt(current)
                    duration_str = _fmt(duration)
                    self.time_label.config(text=f"{current_str} / {duration_str}")
                    
                    # Automatisches Ausblenden der UI
                    if self._ui_visible and self._idle_timer is None:
                        self._idle_timer = self.root.after(3000, self._hide_ui)
                        
                # Überprüfen, ob das Video abgespielt wurde
                if self._ui_visible:
                    state = self.mp.get_state()
                    if state == vlc.State.Ended:
                        self.status_indicator.config(text="Beendet")
                        
            except Exception:
                # Fehler beim Update ignorieren
                pass
                
        self.root.after(100, self._ticker)
        
    def set_volume(self, volume: int):
        """Lautstärke setzen"""
        self.vol.set(volume)
        self.mp.audio_set_volume(volume)


def _play_embedded(src: str, title: str = "", live: bool = False):
    """Spielt den Stream direkt im eingebetteten Win32-VLC-Host ab."""
    global _VLC_INSTANCE, _EMBEDDED_MP, _EMBEDDED_MEDIA
    global _EMBEDDED_SRC, _EMBEDDED_TITLE, _EMBEDDED_LIVE
    from . import embed

    hwnd = embed.hwnd()
    if not hwnd:
        raise RuntimeError("eingebettete Videofläche ist nicht verfügbar")

    with _START_LOCK:
        old_mp = _EMBEDDED_MP
        old_media = _EMBEDDED_MEDIA
        _EMBEDDED_MP = None
        _EMBEDDED_MEDIA = None
        if old_mp is not None:
            try:
                old_mp.set_hwnd(0)
            except Exception:
                pass
            try:
                old_mp.stop()
            except Exception:
                pass
            try:
                old_mp.release()
            except Exception:
                pass
        if old_media is not None:
            try:
                old_media.release()
            except Exception:
                pass

        if _VLC_INSTANCE is None:
            _VLC_INSTANCE = vlc.Instance(
                "--no-video-title-show",
                "--network-caching=8000",
                "--live-caching=8000",
                "--file-caching=5000",
                "--http-reconnect",
                "--http-continuous",
            )
        media = _VLC_INSTANCE.media_new_location(src)
        if title:
            try:
                media.set_meta(vlc.Meta.Title, title)
            except Exception:
                pass
        mp = _VLC_INSTANCE.media_player_new()
        mp.set_media(media)
        mp.set_hwnd(int(hwnd))
        _EMBEDDED_MEDIA = media
        _EMBEDDED_MP = mp
        if mp.play() < 0:
            _EMBEDDED_MP = None
            _EMBEDDED_MEDIA = None
            mp.release()
            media.release()
            raise RuntimeError("libVLC konnte den Stream nicht starten")
        _EMBEDDED_SRC = src
        _EMBEDDED_TITLE = title
        _EMBEDDED_LIVE = live
        if live:
            _start_live_watchdog()
        return mp


def _start_live_watchdog():
    """Startet einen Live-HLS nach einem kurzfristigen VLC-Abbruch neu."""
    global _LIVE_WATCHDOG_STARTED
    if _LIVE_WATCHDOG_STARTED:
        return
    _LIVE_WATCHDOG_STARTED = True

    def watch():
        global _LIVE_WATCHDOG_STARTED
        try:
            while True:
                time.sleep(2)
                if not _EMBEDDED_LIVE:
                    return
                mp = _EMBEDDED_MP
                if mp is None:
                    continue
                state = str(mp.get_state())
                if state not in ("State.Ended", "State.Error"):
                    continue
                src, title = _EMBEDDED_SRC, _EMBEDDED_TITLE
                if src:
                    time.sleep(1)
                    try:
                        _play_embedded(src, title, live=True)
                    except Exception:
                        pass
        finally:
            _LIVE_WATCHDOG_STARTED = False

    threading.Thread(target=watch, daemon=True, name="live-hls-watchdog").start()


def stop_video():
    """Trennt den aktuell eingebetteten Stream vollständig."""
    global _EMBEDDED_MP, _EMBEDDED_MEDIA, _EMBEDDED_LIVE
    _EMBEDDED_LIVE = False
    with _START_LOCK:
        mp, media = _EMBEDDED_MP, _EMBEDDED_MEDIA
        _EMBEDDED_MP = None
        _EMBEDDED_MEDIA = None
        if mp is not None:
            try:
                mp.set_hwnd(0)
            except Exception:
                pass
            try:
                mp.stop()
            except Exception:
                pass
            try:
                mp.release()
            except Exception:
                pass
        if media is not None:
            try:
                media.release()
            except Exception:
                pass


def player_state() -> dict:
    """Liest den tatsächlichen libVLC-Zustand für die UI."""
    mp = _EMBEDDED_MP
    if mp is None:
        return {'open': False, 'state': 'State.NothingSpecial', 'time': 0,
                'length': 0, 'buffering': False, 'embed': False}
    try:
        state = str(mp.get_state())
        current = max(0, int(mp.get_time() or 0))
        length = max(0, int(mp.get_length() or 0))
        return {'open': True, 'state': state, 'time': current,
                'length': length, 'buffering': state in ('State.Opening', 'State.Buffering'),
                'embed': True}
    except Exception as exc:
        return {'open': True, 'state': 'State.Error', 'time': 0, 'length': 0,
                'buffering': False, 'embed': True, 'error': str(exc)[:160]}


def player_command(cmd: str, arg: int | None = None):
    mp = _EMBEDDED_MP
    if mp is None:
        return
    if cmd == 'toggle':
        mp.pause()
    elif cmd == 'seekto' and arg is not None:
        mp.set_time(max(0, int(arg)))
    elif cmd == 'seek' and arg is not None:
        mp.set_time(max(0, int(mp.get_time() or 0) + int(arg)))


def play_video(src: str, title: str = "", live: bool = False):
    """Startet den eingebetteten In-App-Player."""
    return _play_embedded(src, title, live=live)
