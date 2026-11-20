# Signierte Updates

Ab der nächsten Version nach 1.5.8 installiert RyvenPanel ein Update nur, wenn es mit dem Release-Schlüssel
des Inhabers signiert ist. Wer nur den Server kontrolliert, kann damit keine fremde `RyvenPanel.exe` mehr verteilen.

## Verfahren

- Algorithmus: ECDSA P-256 mit SHA-256 (`ECDsaCng`, in .NET 4 enthalten, keine zusätzlichen DLLs).
- Signiert wird der Text `RyvenPanel|<version>|<sha256 klein>|<größe in bytes>` (UTF-8).
- Die Signatur steht als Base64 (64 Byte r||s) im Feld `signatur` der `version.json` in `/opt/ryvenpanel/dist`.
  Das Gateway reicht `version.json` unverändert über `GET /panel/update` weiter.
- Der öffentliche Schlüssel ist fest in `src/UpdateSignature.cs` (`PublicKey`) eingebaut.
- Das Panel prüft vor dem Angebot und noch einmal vor der Installation:
  1. die Signatur über Version, SHA-256 und Größe,
  2. dass die heruntergeladene Datei genau diese Größe und diesen SHA-256 hat.
  Schlägt eine Prüfung fehl, wird nichts installiert. Der Nutzer sieht eine deutsche Warnung.
  Das gilt für Teammitglieder (Gateway) und den Inhaber (SSH) gleichermaßen.
- Übergang: Apps bis 1.5.8 ignorieren das Feld. Neue Apps bieten nur Versionen an, die neuer sind als sie selbst.
  Daher stört die unsignierte 1.5.8 auf dem Server nicht.

## Privater Schlüssel

- Ort: `%APPDATA%\RyvenPanel\release-signing.key`. Die Datei ist mit DPAPI geschützt (Windows-Benutzer, Zusatz-Entropie).
  Nur derselbe Windows-Benutzer auf demselben PC kann sie entschlüsseln.
- Der Schlüssel wurde einmalig mit `sign-release.ps1 -Init` erzeugt. Er wird nie ausgegeben, nie hochgeladen und nie committet.
  `-Init` bricht ab, wenn schon ein Schlüssel existiert.
- Öffentlichen Schlüssel anzeigen: `.\sign-release.ps1 -PublicKey`.

### Offline-Sicherung (unbedingt einmal machen)

DPAPI-Dateien sind nach einer Windows-Neuinstallation oder auf einem anderen PC wertlos. Ohne Sicherung gilt dann:
Ein neuer Schlüssel braucht eine neue App-Version, und **alle Teammitglieder müssen das Setup neu installieren.**

1. `.\sign-release.ps1 -Backup E:\ryvenpanel-signing.bak` (USB-Stick)
   - Die Sicherung ist mit einem Passwort geschützt: mindestens 12 Zeichen, AES-256, PBKDF2 mit 200 000 Runden, HMAC.
2. Bewahre den Stick offline auf. Notiere das Passwort getrennt davon, z. B. im Passwortmanager.
3. Lege die Datei niemals in ein Repo, eine Cloud oder auf den Server.
4. Wiederherstellen auf einem neuen PC: `.\sign-release.ps1 -Restore E:\ryvenpanel-signing.bak`
   - Das Skript warnt, wenn der Schlüssel nicht zu `src/UpdateSignature.cs` passt.

## Release signieren

1. Erhöhe `src/Version.cs` und führe `.\build.ps1` aus.
   - Der Build signiert die neue `PXMX.exe` zur Kontrolle. Er prüft dabei gegen den eingebauten öffentlichen Schlüssel
     und schreibt `dist\RyvenPanel-<ver>.signatur.txt`.
   - Fehlt der Schlüssel, warnt der Build nur.
2. Starte die neue exe als Inhaber und bestätige „Update verteilen“.
   - `Updater.Publish` signiert vor dem Upload lokal und schreibt `signatur` in die `version.json`.
   - Fehlt der Schlüssel oder passt er nicht, wird nichts verteilt.
3. Manuelle Verteilung per scp:
   - Erzeuge die Signatur mit `.\sign-release.ps1 -Exe .\PXMX.exe`.
   - Trage sie als `"signatur"` in die `version.json` ein. Version, sha256 und size müssen exakt passen.

## Schlüssel tauschen (nur im Notfall, z. B. bei Verlust oder Kompromittierung)

1. Lege die alte Schlüsseldatei beiseite.
2. Führe `-Init` aus.
3. Trage den neuen öffentlichen Schlüssel in `UpdateSignature.PublicKey` ein.
4. Baue die App neu und verteile sie per Setup an alle.
   - Bestehende Apps akzeptieren nur den alten Schlüssel.
   - Deshalb lässt sich ein Schlüsseltausch nicht über die Update-Funktion verteilen.
