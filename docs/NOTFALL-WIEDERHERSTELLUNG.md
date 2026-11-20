# Notfall-Wiederherstellung RyvenMC

Stand 2026-11-17. Server `DEINE_SERVER_IP`, Zugang per SSH als root (`ssh ryvenmc`, Owner-Key). Notfall-Zugang, falls SSH direkt nicht geht: Cloudflare-Tunnel `notfall.ryvenmc.de` (Service-Token, siehe `%APPDATA%\RyvenPanel\notfall.json`). Passwoerter stehen NUR in `Desktop\PWS\RyvenMC-Zugaenge.txt` (nie in Chat/Repo).

## 1. Wo liegen die Backups?

| Was | Wo | Wann | Aufbewahrung |
|---|---|---|---|
| Alle MariaDB-Datenbanken | `/opt/backups/db/all-<Datum>.sql.gz` | taeglich 04:30 (`ryven-dbbackup.timer`, Skript `/opt/ryvenpanel/backup-db.sh`) | 14 Tage |
| Finanzen (SQLite + Belege) | `/opt/backups/db/finanzen-<Datum>.tar.gz` | zusammen mit DB-Dump | 14 Tage |
| Minecraft-Welten/Server | `/opt/crafty/docker/backups/<server-id>/*.zip` (Crafty-Zeitplan "Nacht-Backup" 04:45) | taeglich | 3 je Server |
| Gesamter Server | Hoster-Backup (Kundenkonto des Hosters, Owner), geplant taeglich ca. 05:30 | taeglich | laut Hoster |
| Config-Sicherungen | `/opt/backups/*`, `*.bak-*` neben den Dateien | manuell vor Aenderungen | - |

Server-IDs: `8742f521-e4e4-4277-88cf-84b9e0b91660` Survival, `07f306a5-c2b1-49f6-857d-811073d3861d` CityBuild, `57ea3cca-9b4f-4d2f-917f-f4541b9f5dca` Velocity-Proxy, `0ae0b774-42cb-4fc2-af91-68c2ce3b1093` Lobby, `757c8a0d-cea4-4015-87b9-ac4409a1e4b4` Build.

Wichtig: Alle lokalen Backups liegen auf demselben Server. Gegen Totalverlust schuetzt nur das Hoster-Backup (Szenario D) - regelmaessig pruefen, dass es existiert.

## 2. Wer hat welche Schluessel (ohne Geheimnisse)

- Owner (Nils): SSH-Key `ryvenmc_nils_ed25519` (Hardware-Key), Cloudflare-Account, Hoster-Konto, Notfall-Token, Passwortdatei `Desktop\PWS`.
- Claude Code (Owner-PC): SSH-Key `claudecode_ryvenmc_ed25519` (Alias `ryvenmc-claude`).
- Freundin: SSH-Key im Owner-Auftrag; Entzug = Zeile in `/root/.ssh/authorized_keys` loeschen.
- DB-Root-Passwort: im Container `mariadb` als `MARIADB_ROOT_PASSWORD` (`/opt/db/docker-compose.yml`); Plugin-Zugaenge in `/opt/db/plugin-zugaenge.txt`.
- Crafty-Token/Gateway: `/etc/ryvenpanel/gateway.json`. Discord-Bot-Token: `/opt/ryvenlink-bot/config.json`.
- Diese Konfigurationsdateien stecken nicht im SQL-Dump; bei Neuaufbau aus dem Hoster-Backup holen.

## 3. Szenarien

Vorher immer: Welches Backup? `ls -lt /opt/backups/db/ | head`, Alter pruefen. Nie ohne Sicherung des aktuellen (kaputten) Zustands ueberschreiben.

### A) Einzelne Tabelle kaputt / versehentlich geloescht

Dump in Wegwerf-Container einspielen (ohne Netz, beruehrt Live nicht), nur die Tabelle exportieren, in Live einspielen.

```bash
PW=$(head -c 24 /dev/urandom | base64 | tr -dc A-Za-z0-9)
docker run -d --name restoretest --network none -e MARIADB_ROOT_PASSWORD="$PW" mariadb:11.4
sleep 20; zcat /opt/backups/db/all-DATUM.sql.gz | docker exec -i restoretest mariadb -uroot -p"$PW"
# Tabelle exportieren (DB und Tabelle anpassen):
docker exec restoretest mariadb-dump -uroot -p"$PW" luckperms luckperms_players > /tmp/tabelle.sql
# Live: erst aktuellen Stand sichern, dann Tabelle ersetzen (Dump enthaelt DROP TABLE + CREATE)
docker exec mariadb sh -c 'mariadb-dump -h127.0.0.1 -uroot -p"$MARIADB_ROOT_PASSWORD" luckperms luckperms_players' > /opt/backups/db/vorher-luckperms_players.sql
docker exec -i mariadb sh -c 'mariadb -h127.0.0.1 -uroot -p"$MARIADB_ROOT_PASSWORD" luckperms' < /tmp/tabelle.sql
docker rm -f restoretest; rm /tmp/tabelle.sql
```
Danach das betroffene Plugin bzw. den Server ueber Crafty neu starten (Daten im RAM koennen die Tabelle sonst wieder ueberschreiben).

### B) Ganze Datenbank oder alle Datenbanken verloren

```bash
cd /opt/db && docker compose ps            # laeuft der Container?
# Bei defektem Datenverzeichnis: Minecraft-Server ueber Crafty stoppen, dann
docker compose stop; mv /opt/db/data /opt/db/data.defekt-$(date +%F); mkdir /opt/db/data; docker compose up -d
sleep 30
zcat /opt/backups/db/all-DATUM.sql.gz | docker exec -i mariadb sh -c 'mariadb -h127.0.0.1 -uroot -p"$MARIADB_ROOT_PASSWORD"'
```
Der Dump enthaelt auch die `mysql`-Datenbank (Benutzer/Rechte der Plugins). Root geht nur per TCP (nicht per Socket). Datenverlust: alles seit 04:30 des Backup-Tages. Danach Checkliste (Abschnitt 4).

### C) Welt/Server beschaedigt

1. Server in Crafty stoppen.
2. Beschaedigte Welt sichern: `mv /opt/crafty/docker/servers/<id>/world /opt/crafty/docker/servers/<id>/world.defekt-$(date +%F)`
3. Backup pruefen und einspielen (Crafty "Backups -> Wiederherstellen" oder manuell):
```bash
ls -lt /opt/crafty/docker/backups/<id>/
python3 -c "import zipfile,sys;print(zipfile.ZipFile(sys.argv[1]).testzip())" /opt/crafty/docker/backups/<id>/DATEI.zip   # None = ok
unzip -l /opt/crafty/docker/backups/<id>/DATEI.zip | head      # Pfade ansehen
unzip /opt/crafty/docker/backups/<id>/DATEI.zip 'world/*' -d /opt/crafty/docker/servers/<id>/
chown -R --reference=/opt/crafty/docker/servers/<id>/server.properties /opt/crafty/docker/servers/<id>/world
```
Weitere Welt-Ordner (`world_nether`, ...) analog. Server in Crafty starten. Nie `docker stop` auf den Crafty-Container (killt alle Server nach 10 s).

### D) Server komplett verloren

1. Beim Hoster Backup/Snapshot einspielen (Owner, Hoster-Konto) oder neuen Server mit gleichem OS bestellen. Aendert sich die IP, DNS bei Cloudflare anpassen (`play`, SRV `_minecraft._tcp`); die Tunnel-Dienste laufen ueber cloudflared ohne feste IP.
2. Bei Neuaufbau: Docker + Compose installieren; `/opt/crafty`, `/opt/db`, `/opt/ryvenlink-bot`, `/opt/ryvenmc-web`, `/opt/ryvenpanel`, `/etc/ryvenpanel`, `/var/lib/ryvenpanel` aus dem Hoster-Backup zurueckholen; DB per Szenario B; Welten per Szenario C.
3. SSH-Keys eintragen (`/root/.ssh/authorized_keys`), Haertung (`/etc/ssh/sshd_config.d/10-ryvenmc-keyonly.conf`), fail2ban, Firewall (`/usr/local/sbin/ryvenpanel-fw apply`, Modus strict).
4. Bei Verdacht auf Kompromittierung alle Zugaenge rotieren (Root, Crafty, Crafty-Tokens, Discord-Bot-Token).
5. Checkliste Abschnitt 4.

## 4. Checkliste nach jeder Wiederherstellung

- [ ] `docker ps`: mariadb, crafty_container, ryvenlink-bot, caddy, adminer, cloudflared (Soll: `/etc/ryvenpanel/watchdog-containers.txt`).
- [ ] Crafty-Server: Velocity-Proxy `57ea3cca` zuerst, dann Survival, CityBuild, Lobby; Konsole ohne Fehler; Join per `play.ryvenmc.de` (Java) und Bedrock (19132).
- [ ] Proxy-Plugins vorhanden (`ls plugins/*.jar`: Geyser, Floodgate, NuVotifier, RyvenLink, RyvenCloud).
- [ ] Discord-Bot online, `/link` testen.
- [ ] Gateway: `systemctl status ryvenpanel-gateway`, Port 8444; `secure.ryvenmc.de` und Owner-App verbinden.
- [ ] Website `ryvenmc.de` laedt (Caddy, `/opt/ryvenmc-web`).
- [ ] Firewall: `iptables -S RYVEN-IN`, Modus strict; offen nur tcp 22, 80, 443, 25565, 19132, 8192, 8444 und udp 19132/19133/24454/24455; 8443 zu.
- [ ] cloudflared-Tunnel `ryvenmc` verbunden; `dash`, `db`, `secure`, `notfall` erreichbar (Cloudflare Access).
- [ ] Timer: `systemctl list-timers | grep ryven` (watchdog, dbbackup, finanzen, restorecheck).
- [ ] Watchdog: `/opt/ryvenpanel/watchdog.sh --dry-run` ohne Probleme.
- [ ] `/opt/ryvenpanel/restore-check.sh --dry-run` laeuft durch.
- [ ] Stichprobe im Spiel: Rang, Geld, PlayerPoints, Bans (`rl_punishments`).

## 5. Gemessene Wiederherstellungszeiten (Test 2026-11-17)

- DB-Restore aller 22 Datenbanken in Wegwerf-Container (mariadb:11.4, Dump ca. 1 MB): ca. 9 s inkl. Containerstart. Realistische RTO inkl. Stoppen, Einspielen, Pruefen: 10-15 Minuten.
- Finanzen: Entpacken + integrity_check unter 1 s.
- Welt-Restore: noch nicht gemessen, es existierte bisher kein Crafty-Backup (erster Lauf 04:45 am 2026-11-18). Danach `restore-check.sh --dry-run` wiederholen und Zeit hier eintragen.
- Gesamtserver aus Hoster-Backup: nicht getestet (Owner: einmal Probe-Wiederherstellung ausloesen).

## 6. Automatischer Restore-Check

`/opt/ryvenpanel/restore-check.sh` (Repo: `RyvenPanel/restore-check.sh`), Timer `ryven-restorecheck.timer` am 1. jedes Monats 06:30. Prueft: DB-Dump in Wegwerf-Container ohne Netz + Tabellen-/Zeilenvergleich mit Live (nur SELECT), Finanzen integrity_check, Zip-Test der neuesten Crafty-Backups, level.dat + Region-Datei des Survival-Backups lesbar. Ergebnis geht in Discord #compilance (gleicher Bot/Kanal wie Watchdog). Manuell: `restore-check.sh --dry-run` (postet nichts).
