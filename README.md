# ▶ Videothek

Lokale Videothek auf dem PC: Datenbank aller Filme aus den Telegram-Gruppen,
die du auswählst — mit Postern (TMDB), Suche, Filtern, Links zu den Telegram-Posts
und Download-Button für die offline-Ansicht.

## Starten
Doppelklick auf `start_videothek.bat` (oder `launcher\start.pyw` mit `pythonw.exe`).
Der Browser öffnet sich automatisch auf http://127.0.0.1:8765 .

## So funktioniert's
1. **Gruppen**: Einmal „Gruppen aus Telegram aktualisieren“ — dann Häkchen bei den Gruppen setzen, die rein sollen, und „Scannen“.
2. Der Scanner liest nur Metadaten (kein Download beim Scannen), stellt neue Medien in die TMDB-Warteschlange und gleicht Titel automatisch ab (Poster, Jahr, Genre, Beschreibung).
3. **Filme**: Suchen/Filtern; ein Film kann mehrere Quellen (verschiedene Gruppen) haben.
4. **In Telegram** öffnet den Original-Post; **⬇ Herunterladen** holt das Video nach `data/downloads/<Gruppe>/`.

## Daten
- `data/videothek.sqlite3` — Datenbank
- `data/config.json` — Telegram-Session + TMDB-Key (wird beim ersten Start aus GH Infinity übernommen; lokale Datei, bitte nicht weitergeben)
- `data/posters/` — gecachte Film-Poster
- `data/downloads/` — heruntergeladene Videos

## Technik
Flask + Telethon + TMDB, Python 3.11, venv im Ordner. Backend unter `backend/`, Frontend eine Seite unter `frontend/index.html`.
