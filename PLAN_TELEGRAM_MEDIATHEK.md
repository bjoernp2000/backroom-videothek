# Telegram-Mediathek – Arbeitsplan

Stand: 2026-10-10

## Ziel

Die Videothek zeigt Medien aus ausgewählten Telegram-Gruppen in einer eigenen Oberfläche. Filme werden zuverlässig erkannt, mit TMDB verknüpft, als Varianten zusammengefasst und direkt über Telegram abgespielt oder lokal heruntergeladen.

## Bestandaufnahme

- Flask-Backend und Single-Page-Frontend sind vorhanden.
- SQLite-Datenbank: `data/videothek.sqlite3`.
- Telegram-Anbindung über Telethon ist vorhanden.
- TMDB-Suche, Poster-Cache, Filmseiten, Watchlist und Fortschritt sind vorhanden.
- Range-/Live-Streaming über den lokalen Backend-Endpunkt ist vorhanden.
- Rund 31.737 Filme und 137.790 Media-Items sind bereits gespeichert.
- 81.797 Media-Items sind gematcht; 55.993 stehen aktuell im Status `indexed`.
- 500 Gruppen sind ausgewählt.

## Festgestellte technische Risiken

1. Der Scanner speichert neue Media-Items als `indexed`, der Matcher verarbeitet aber nur `unmatched`. Neue Scans erreichen dadurch den TMDB-Matcher nicht.
2. `telegram.get_media_video()` liefert die Dateigröße unter `size`, der Scanner liest `size_bytes`. Neue Datensätze erhalten dadurch voraussichtlich Größe `0`; das gefährdet Range-Streaming und Download-Anzeige.
3. Die bestehende Datenbank und der laufende Inhalt dürfen nicht gelöscht oder neu aufgebaut werden.
4. Vor einer EXE-Auslieferung müssen Quellcode, laufender Backend-Prozess und ausgelieferte Oberfläche getrennt geprüft werden.

## Umsetzung in Arbeitspaketen

### P0 – Datenpfad stabilisieren

- [x] Bestand inventarisieren.
- [x] Neue Telegram-Medien als Matcher-Eingang markieren.
- [x] Dateigröße korrekt in `media_items.size_bytes` speichern.
- [x] Scan-Zähler für Medien nachvollziehbar aktualisieren.
- [x] Bestehende `indexed`-Datensätze kontrolliert in die Matcher-Warteschlange überführen.
- [x] Datenbankintegrität und Stichproben vor/nach Migration prüfen.
- [x] Batch-Endpunkt `/api/match/queue` mit Obergrenze 5000 ergänzt.
- [x] Erster kontrollierter Bestand von 5100 Medien eingereiht und Matcher-Lauf verifiziert.

### P1 – TMDB-Zuordnung professionell machen

- [x] Matcherstatus um `matched`, `ambiguous`, `error` und `no_match`-Begründungen erweitern.
- [x] Trefferkonfidenz und Begründung speichern.
- [x] Unsichere Treffer in einer Prüf-API und Prüfseite anzeigen.
- [x] Manuelle Zuordnung und Zuordnung löschen absichern.
- [ ] Serienepisoden an eine Serien-Film-ID hängen, ohne jede Episode als eigenen Film anzulegen.

### P2 – Medienvarianten und Datenqualität

- [x] Qualität, Codec, Sprache, HDR, Untertitel und Release-Typ aus Dateinamen extrahieren.
- [x] Varianten eines Films nach Film-ID gruppieren.
- [x] Varianteninformationen in der Film-Detailansicht anzeigen.
- [x] Dubletten nach Telegram-Quelle, Dateigröße und normalisiertem Dateinamen erkennen.
- [x] Defekte oder nicht mehr erreichbare Telegram-Nachrichten batchweise prüfen und markieren.
- [ ] Poster, Backdrop, FSK, Cast und Collections getrennt nachladen und Status speichern.

### P3 – Wiedergabe und Bibliothek

- [ ] Range-Streaming mit realer Media-ID und Browser-Range-Request verifizieren.
- [ ] Stream-Cache mit Größen-/Fehlergrenzen prüfen.
- [ ] Wiedergabeposition zuverlässig speichern.
- [ ] Filmseite um Variantenwahl, Sprache, Untertitel und Dateiinformationen ergänzen.
- [ ] Download-Warteschlange und Wiederaufnahme bis `done`/`error` durchtesten.

### P4 – Bedienung und Betrieb

- [x] Seiten für ungeklärte TMDB-Treffer, Scanfehler und fehlende Metadaten.
- [x] Scanfortschritt und letzte Fehler verständlich anzeigen.
- [x] Prüfseite für Dubletten und Telegram-Erreichbarkeit.
- [x] Suchfilter für Gruppe, Qualität, Sprache, Jahr, Genre und Status.
- [x] Oberfläche bleibt standardmäßig lokal gebunden; externe Freigabe wird nicht vorausgesetzt.
- [x] Logging ohne Telegram-Session, API-Key oder private Inhalte.

## Abnahmekriterien

- Ein neu gescanntes Video landet reproduzierbar in der Matcher-Warteschlange.
- Dateigröße, Dauer, Auflösung und MIME-Typ stimmen mit Telegram überein.
- Ein sicherer TMDB-Treffer wird automatisch verknüpft; ein unsicherer Treffer wird nicht still falsch zugeordnet.
- Ein vorhandenes Medium kann mit HTTP-Range direkt gestartet werden.
- Ein lokaler Download kann pausiert, fortgesetzt und nach Prozessneustart wieder aufgenommen werden.
- Vorhandene Filme, Poster, Watchlist und Fortschritte bleiben erhalten.
- Jede Behauptung „funktioniert“ wird durch Datenbank-, API- oder echten Wiedergabetest belegt.

## Aktueller Fokus

Zuerst werden die beiden nachgewiesenen P0-Datenpfadfehler behoben. Danach wird gegen die vorhandene Datenbank und einen isolierten Testpfad verifiziert, bevor UI-Ausbau oder EXE-Build beginnt.
