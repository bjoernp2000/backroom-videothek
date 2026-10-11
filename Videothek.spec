# -*- mode: python ; coding: utf-8 -*-
# Videothek.EXE — App-Fenster (WebView2), Server eingebettet.
# Daten (DB, Poster, Downloads, config) bleiben EXTERN neben der EXE.
import os
import sys
from pathlib import Path

project_root = Path(getattr(sys, "SPECPATH", os.getcwd())).resolve()

a = Analysis(
    [str(project_root / "launcher" / "app.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "frontend"), "assets/frontend"),
    ],
    hiddenimports=[
        "waitress", "flask", "requests", "PIL",
        "webview", "webview.platforms.edgechromium", "webview.platforms.winforms",
        "clr", "proxy_tools",
        "telethon", "telethon.events", "telethon.tl.functions.auth",
        "telethon.tl.functions.channels", "telethon.tl.functions.contacts",
        "telethon.tl.functions.messages", "telethon.tl.functions.users",
        "telethon.tl.types", "telethon.sessions.sqlite", "telethon.sessions.memory",
        "telethon.network.mtprotosender", "telethon.network.mtproxyprotocol",
        "telethon.network.connection.connection", "telethon.network.connection.tcpfull",
        "telethon.crypto.aes", "telethon.crypto.authkey", "telethon.crypto.tgscheme",
        "telethon.crypto.rsa", "telethon.crypto.coordinates",
        "telethon.extensions.binaryreader", "telethon.extensions.markdown",
        "telethon.extensions.html", "telethon.extensions.messagepersistence",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter.test", "unittest", "pydoc_data"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Videothek",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project_root / "assets" / "videothek.ico"),
)
