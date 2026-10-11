# Videothek To-Go für Windows

Diese Variante läuft direkt von USB-Stick, externer SSD oder einem beliebigen lokalen Ordner.
Es ist keine Installation und kein Administratorrecht erforderlich.

## Start

`start_videothek_togo.bat` doppelklicken oder `Videothek-ToGo.exe` direkt starten.

Die veränderlichen Daten liegen ausschließlich in:

```text
data/
```

Dort werden Datenbank, Einstellungen, Poster, Downloads und Protokolle der To-Go-Version
angelegt. Die portable Version verwendet keine feste Projektlaufwerk-Adresse.

Die Anwendung bindet den lokalen Server nur an `127.0.0.1:8765`.

## Voraussetzungen

- Windows 10/11
- WebView2 Runtime für das eingebettete Fenster
- Internetzugang für öffentliche TV-Playlists, EPG, TMDB und optionale Quellen

## Öffentliche TV-Quellen

Die TV-Seite lädt standardmäßig öffentliche Quellen aus `backend/tv.py`:

- Pluto TV Deutschland mit XMLTV-EPG
- Free-TV Deutschland
- German TV M3U
- IPTV-org Deutschland

Die externen Streams und deren Rechte bleiben bei den jeweiligen Anbietern.

## Entfernen

Anwendung schließen und den gesamten Ordner löschen. Es bleiben keine Installations- oder
Registry-Einträge zurück.
