# RyvenPanel

**Kostenloses Windows-Control-Panel für Minecraft-Netzwerke mit [Crafty Controller](https://craftycontrol.com/).**
Ein Programm für alles, was man als Server-Owner täglich braucht – Server steuern, Spieler & Ränge verwalten,
Wirtschaft auswerten, Dateien, Terminal, Docker und Firewall – plus ein sicherer Zugang für dein Team.

> [!WARNING]
> **Dieses Panel ist speziell für das RyvenMC-Netzwerk angepasst – es ist kein Plug-and-Play-Panel für beliebige Server.**
>
> - **Ein KVM-Server ist Pflicht.** Du brauchst einen eigenen Linux-Server (KVM-VPS oder Root-Server) mit Root- und SSH-Zugriff.
>   Auf Minecraft-Hostern, Shared Hosting oder Webspace ohne Root/SSH funktioniert RyvenPanel **nicht**.
> - **Docker wird vorausgesetzt.** Crafty Controller und die Datenbank (MariaDB/MySQL) müssen als Docker-Container laufen.
> - **Eigene Plugins sind nötig, um an die Daten zu kommen.** Mehrere Bereiche lesen ihre Daten über eigene Plugins bzw. deren
>   Datenbank-Tabellen (z. B. *RyvenEco* für die Wirtschaft, LuckPerms mit MySQL für Ränge und Team-Rechte). Ohne diese Plugins
>   – oder eigene Plugins mit derselben Schnittstelle – bleiben diese Bereiche leer oder funktionieren nur eingeschränkt.
> - Pfade und Abläufe (z. B. `/opt/crafty/docker/servers`) sind auf den Aufbau von RyvenMC abgestimmt und müssen ggf. angepasst werden.
>
> Nutzung auf eigene Verantwortung, ohne Anspruch auf Support.

Entwickelt für das RyvenMC-Netzwerk, frei nutzbar für jeden (MIT-Lizenz).

## Download

👉 **[Neueste Version herunterladen](../../releases/latest)** – `RyvenPanel-Setup-x.y.z.exe` starten, fertig.

- Installation nur für den eigenen Windows-Benutzer, **keine Adminrechte** nötig
- Startmenü- und Desktop-Verknüpfung, Deinstallation über *Einstellungen → Apps*
- **Automatische Updates**: veröffentlicht der Owner eine neue Version, bekommt das ganze Team sie beim nächsten Start angeboten

## Funktionen

| Bereich | Was es kann |
|---|---|
| **Server-Steuerung** | Alle Crafty-Server mit Live-Status, Start/Stopp/Neustart, Backups, Konsole, Dateien, Metriken |
| **Spieler & VPN** | Wer ist online, Login-Verlauf, VPN/Proxy-Erkennung |
| **Nutzer & Ränge** | Money, Coins, LuckPerms-Ränge und Permissions, Bans |
| **Wirtschaft** | Geldmenge (Inflation), woher das Geld kommt, Top-Verdiener, Shop-Statistik, Preise ändern (EconomyShopGUI), Crate-Auswertung, Verkaufssteuer |
| **Terminal** | SSH-Konsole mit mehreren Tabs |
| **Dateien** | Datei-Manager für den ganzen Server (Upload/Download, Editor) |
| **Docker** | Container, Stacks, Images, Netzwerke |
| **Firewall** | Ports öffnen/schließen, IPs sperren (iptables) |
| **Team & Zugänge** | Team-Accounts mit Passwort, Rechte pro LuckPerms-Rang, IP-Freigaben, Protokoll |

### Team-Zugang
Teammitglieder bekommen **nie** deinen SSH-Schlüssel oder Crafty-Zugang. Ein kleiner Dienst auf dem Server
(`gateway/gateway.py`, wird aus dem Panel heraus installiert) prüft jede Aktion gegen die Rechte des Rangs,
bindet Accounts an IP-Adressen und protokolliert alles.

## Voraussetzungen

**PC:** Windows 10/11 (.NET Framework 4.8 ist bereits enthalten). Für Terminal, Dateien, Docker, Firewall und
Wirtschaft den Windows-Client „OpenSSH“ (*Einstellungen → Optionale Features*).

**Server (Pflicht):**
- **KVM-Server** (VPS oder Root-Server) mit Linux, Root- und SSH-Zugriff
- **Docker** mit Crafty Controller 4 und MariaDB/MySQL als Container
- Python 3 für den Team-Zugang
- LuckPerms mit MySQL/MariaDB-Speicher
- Eigene Plugins für den Datenzugriff, z. B. **RyvenEco** für die Wirtschafts-Auswertung

## Erste Schritte

1. **Owner:** Im Login-Fenster „Owner (Crafty)“ wählen, Crafty-Adresse (z. B. `https://dein-server:8443`) und Login eingeben.
2. Unter **Docker** einmal die SSH-Verbindung einrichten (Schlüssel wird erzeugt).
3. Unter **Team & Zugänge** den Team-Dienst installieren, Rechte pro Rang setzen und Accounts anlegen.
4. **Team:** Installer weitergeben – Mitglieder wählen „Team-Login“ und tragen die Adresse deines Servers ein.

## Updates veröffentlichen (Owner)

1. Versionsnummer in `src/Version.cs` erhöhen
2. `build.ps1` ausführen
3. Neue `RyvenPanel.exe` starten → „Update verteilen“ bestätigen

Das Update landet auf deinem Server (`/opt/ryvenpanel/dist`), das Team lädt es über den Team-Dienst
(gleiches, fest hinterlegtes Zertifikat wie beim Login) und prüft die SHA-256-Prüfsumme.

## Selbst bauen

Keine Installation nötig – gebaut wird mit dem C#-Compiler, der in Windows steckt:

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

Ergebnis: `RyvenPanel.exe` und `dist/RyvenPanel-Setup-<version>.exe`.

## Lizenz

[MIT](LICENSE) – kostenlos nutzbar, auch für eigene Server. Keine Garantie.
