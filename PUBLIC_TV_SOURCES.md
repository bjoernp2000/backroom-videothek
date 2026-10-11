# Backroom TV – freie und öffentliche Quellen

Backroom TV verwendet nur eigene Adapter und Standardformate. Kodi-Add-ons, Binärdateien,
Resolver oder Zugangsdaten aus KUS werden nicht eingebettet.

## Automatisch sichtbare Quellen

| Quelle | Typ | Herkunft | Hinweis |
|---|---|---|---|
| Pluto TV Deutschland | M3U + XMLTV | [BuddyChewChew/pluto](https://github.com/BuddyChewChew/pluto), [matthuisman EPG](https://github.com/matthuisman/i.mjh.nz) | öffentliche Playlist und EPG; Streams können regional sein und Token können ablaufen |
| Free-TV Deutschland | M3U8 | [Free-TV/IPTV](https://github.com/Free-TV/IPTV) | freie Sender nach den Regeln des Quellprojekts; einzelne Sender können regional blockiert sein |
| German TV M3U | M3U | [josxha/german-tv-m3u](https://github.com/josxha/german-tv-m3u) | täglich aktualisierte deutsche freie Senderliste |
| IPTV-org Deutschland | M3U | [iptv-org/iptv](https://github.com/iptv-org/iptv) | öffentlich gesammelte Sender; regionale Sperren und Ausfälle möglich |

Die URLs liegen als Konfiguration in `backend/tv.py` und werden nicht als geheime Werte
behandelt. Die eigentlichen Senderrechte bleiben bei den jeweiligen Rechteinhabern.

## Benutzerquellen

Zusätzlich können autorisierte HTTP-/HTTPS-M3U-/M3U8-Playlists und optionale XMLTV-EPGs
über die TV-Oberfläche eingetragen werden.

## Plex, Emby, Zattoo und Joyn

Diese Dienste werden nicht als kostenlose öffentliche Streams ausgegeben:

- ein eigener Plex- oder Emby-Server benötigt die Serveradresse und eine Berechtigung des Nutzers;
- Zattoo und Joyn können ein eigenes Konto oder regionale Berechtigungen verlangen;
- ein KUS-Add-on ist keine ausreichende Lizenz für die Übernahme seiner Resolver oder Streams.

Dafür sind getrennte, dokumentierte Adapter möglich, sobald die autorisierte API bzw. der
Nutzerzugang konfiguriert ist. Es werden keine fremden Tokens, Accounts oder privaten
Serveradressen mitgeliefert.

## Forkbarkeit

Das Backroom-TV-Backend ist unabhängig von Kodi und kann zusammen mit dem Projekt als
öffentliches GitHub-Repository beziehungsweise Fork veröffentlicht werden. Vor einer
Veröffentlichung müssen die projektspezifische Open-Source-Lizenz und die Lizenzhinweise
aller übrigen Projektbestandteile festgelegt werden; externe Playlists bleiben externe
Abhängigkeiten und werden nicht als eigene Inhalte weiterverteilt.
