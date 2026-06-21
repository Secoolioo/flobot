# Flo MC Bridge ⛏️

Winziger, abhängigkeitsfreier **Statistik-Exporter** für den Minecraft-Server,
damit der Discord-Bot **Flo** auf `flo mcleaderboard` die besten Server-Statistiken
(wer am meisten abgebaut hat, welche Blöcke, plus Spielzeit/Mob-Kills/Tode/Strecke)
als Minecraft-Style-Bild zeigen kann.

* **Keine Mod-/Plugin-Abhängigkeit:** nur JDK-Bordmittel → läuft mit jeder
  Server-Software (Vanilla, **Paper**, Spigot, Fabric, Forge) und ist
  **versionsunabhängig** (1.21.10 und alles davor/danach – das Stats-Dateiformat
  ist seit 1.13 stabil).
* **Sicher:** stellt die Stats über ein **token-geschütztes** Web-Interface bereit.
  Der **Discord-Token gehört NICHT hierher** – die Bridge nutzt ein eigenes,
  geteiltes Geheimnis (`token`).

Bot und MC-Server dürfen auf komplett verschiedenen Maschinen laufen – die Bridge
liegt beim **Minecraft-Server**, Flo holt sich die Daten per HTTP.

```
  Discord  ──►  Flo (VPS)  ──HTTP :4918 + Token──►  Flo MC Bridge (MC-Server)
                                                      └─ liest <welt>/stats/*.json
```

---

## 1. Die .jar bekommen

Im Repo liegt die fertig gebaute **`flo-mcbridge.jar`** schon dabei – einfach
nehmen. Selbst bauen (JDK 17+ nötig):

```bash
cd mcbridge
./build.sh                       # nutzt javac/jar aus dem PATH
# oder mit eigenem JDK:
JAVA_BIN=/pfad/zum/jdk/bin ./build.sh
```

## 2. Auf dem Minecraft-Server (Debian) installieren

```bash
sudo mkdir -p /opt/flo-mcbridge
sudo cp flo-mcbridge.jar /opt/flo-mcbridge/
sudo cp flo-mcbridge.properties.example /opt/flo-mcbridge/flo-mcbridge.properties
sudo nano /opt/flo-mcbridge/flo-mcbridge.properties     # Token + Pfade eintragen
```

Wichtige Felder in `flo-mcbridge.properties`:

| Feld | Bedeutung |
|------|-----------|
| `token` | **Pflicht.** Langes Zufalls-Geheimnis. Erzeugen: `openssl rand -hex 24` |
| `port` | HTTP-Port (Standard `4918`) |
| `server_dir` + `level_name` | Server-Ordner + Welt-Name (aus `server.properties`). Alternativ `world_dir` oder direkt `stats_dir`. |
| `server_name`, `mc_version` | Anzeige im Discord-Embed |

Als Dienst starten (läuft als der **Minecraft-User**, damit die Stat-Dateien
lesbar sind – ggf. `User=` in der Unit anpassen):

```bash
sudo cp flo-mcbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flo-mcbridge
sudo systemctl status flo-mcbridge          # sollte "laeuft auf http://0.0.0.0:4918" zeigen
```

Schneller Selbsttest auf dem Server:

```bash
curl localhost:4918/health                                  # {"ok":true,"players":N}
curl "localhost:4918/leaderboard?token=DEIN_TOKEN" | head    # JSON mit Spielern
```

**Firewall:** Port `4918` nur für die **Bot-Maschine** öffnen, z. B.:

```bash
sudo ufw allow from <BOT_VPS_IP> to any port 4918 proto tcp
```

## 3. Beim Bot eintragen (`.env` auf dem VPS)

```ini
MINECRAFT_ENABLED=1
MC_STATS_URL=http://<MC-SERVER-IP>:4918/leaderboard
MC_STATS_TOKEN=<dasselbe Token wie in der Bridge>
# Anzeige (optional – die Bridge liefert das auch selbst):
MC_SERVER_NAME=SUPA SIGMA SERVA
MC_VERSION=1.21.10
# Optional:
MC_LB_LIMIT=5            # wie viele Spieler im Bild (3–10)
MC_HTTP_TIMEOUT=8        # Sekunden
```

> Laufen Bot **und** Server ausnahmsweise auf derselben Maschine, kann Flo die
> Dateien auch direkt lesen – dann statt `MC_STATS_URL` einfach
> `MC_STATS_DIR=/pfad/zur/welt/stats` setzen (keine .jar nötig).

Danach den Bot neu starten:

```bash
sudo systemctl restart flobot
```

## 4. Im Discord benutzen

```
flo mcleaderboard
flo mc stats
flo minecraft top
```

→ Flo postet das Block-Style-Leaderboard. 🟩

---

### Endpunkte

| Methode | Pfad | Auth | Antwort |
|--------|------|------|---------|
| `GET` | `/health` | – | `{"ok":true,"players":N}` |
| `GET` | `/leaderboard` | `?token=` **oder** Header `X-Auth-Token` | `{server,mc_version,world,generated_at,players:[{name,uuid,stats}]}` |

Die Bridge liefert nur **Roh-Stats** – die gesamte Auswertung (Top-Miner, Summen,
Block-Aufschlüsselung) macht der Bot in `minecraft.py`. So gibt es genau **eine**
Aggregations-Logik, egal ob HTTP- oder Datei-Quelle.

### Hinweis zur Aktualität

Minecraft schreibt Spieler-Statistiken periodisch und beim Ausloggen/Speichern auf
die Festplatte. Die Bridge liest jeweils den zuletzt gespeicherten Stand – für ein
Leaderboard völlig ausreichend.
