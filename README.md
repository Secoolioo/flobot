# Flo

Deutschsprachiger Discord-Bot: Level & Coins, Aktie, Casino, Spiele, Musik,
Moderation — dazu ein Web-Panel zum Verwalten.

Läuft als systemd-Dienst (`flobot.service`), Konfiguration über `.env`.

---

## Betrieb

### Aktualisieren

```bash
cd /opt/flobot
git pull
sudo systemctl restart flobot
```

Oder ohne Terminal: im Web-Panel unter **Steuerung** auf **Update** — der Bot
zieht selbst `git pull --ff-only` und startet neu, wenn es wirklich neue Commits
gab.

### Wirtschaft zurücksetzen

```bash
./economy_reset.sh            # legt vorher ein Backup an und fragt nach
./economy_reset.sh --dry-run  # nur zeigen, was passieren würde
```

Zurückgesetzt werden Guthaben, Inventar, Titel, Streaks, Aktien-Depots, Luxus,
Thron und Schulden. **Behalten** bleiben Level/XP, Voice-Stunden, Chat-Nachrichten
und Namen. Der Zählspiel-Stand bleibt ebenfalls (da hängt kein Geld dran).

Das Skript weigert sich, während der Bot läuft.

### Tests

```bash
python3 test_games_logic.py    # 100 Tests
python3 test_logic.py          #   6 Tests
python3 bot.py --check         # lädt alle Module ohne zu verbinden
```

Kein pytest nötig — beide Dateien bringen ihren eigenen Runner mit.

### Aktie durchrechnen

```bash
python3 tools_aktien_sim.py    # 60 simulierte Tage, 8 Durchläufe
```

Kein Teil des Bots. Spielt Serverbetrieb mit Wochenenden, unterschiedlich vollen
Calls und toten Tagen durch und prüft acht Kriterien (siehe unten).

---

## Die $FLO-Aktie

Der Kurs folgt der **Server-Aktivität**, nicht dem Zufall.

| | |
|---|---|
| Jeder im Call | 1 Punkt |
| Jeder Livestream | +2,5 |
| Jede Kamera | +1,5 |
| Chat | zählt mit — **aber nur, solange jemand im Call ist** |
| Bots | zählen **nie** |

**Steigen:** mehr Aktivität → höheres Tempo, bis zum Deckel (`CEIL_FACTOR` ×
Zielkurs der aktuellen Aktivität).

**Fallen:** sobald der Call leer ist, sofort und gleichmäßig — 11 % je halber
Stunde (`IDLE_PER_30MIN`), ohne Anlauf.

**Der Boden ist beweglich.** Die Aktie ist so viel wert, wie der Server im
Schnitt lebendig ist (`grund_akt`, träger Mittelwert über 3 Tage). Wer jeden
Abend im Call sitzt, hält den Boden oben; bleibt es dauerhaft still, schmilzt
auch der Grundwert. Ohne das fiel der Kurs jede Nacht auf den Mindestwert
zurück — 13.296-fache Tagesspanne, kein Markt, nur ein Sägezahn.

Gemessen über 60 Tage × 8 Durchläufe:

| | |
|---|---|
| Boden / Spitze | ~2.700 / ~43.400 (Spanne 15×) |
| bester 6-Stunden-Handel | +1.006 % |
| schlimmster 6-Stunden-Handel | −77 % |
| über Nacht halten | −67 % |
| blind 7 Tage halten | −3 % (nichts geschenkt fürs Dabeisein) |
| Trefferquote eines 6h-Kaufs | 21 % (Timing entscheidet) |

Weitere Bremsen: Anteil-Limit 150 pro Person, gestaffelte Verkaufssteuer (bis
35 %), Tagesband, absoluter Höchstkurs, kein Kauf auf Pump.

---

## Wirtschaft

**Quellen:** Nachrichten (8–16, mit Cooldown), Voice (30/Minute, gedeckelt auf
14.400 am Tag ≙ 8 Stunden), Daily-Bonus mit Streak, Level-Ups, Aktien-Dividende,
Spiele und Casino.

**Senken:** Shop-Titel, Luxus, Thron, Händler, Lotto, Verkaufssteuer und die
tägliche **Vermögenssteuer** (Freibetrag 5 Mio, darüber 2 %, ab 400 Mio nur noch
0,4 %). Sie verbrennt die Coins — es gibt kein Gegenkonto.

**Schulden** werden automatisch getilgt: 20 % jeder echten Einnahme gehen an den
größten Gläubiger. Rückbuchungen sind ausgenommen (jeder Grund auf `-rueck`
oder `-rueckgabe`) — ein zurückgegebener Einsatz ist keine Einnahme.

Die Spiele haben eine **Tageskappe** für Netto-Gewinne; der Einsatz kommt immer
voll zurück.

---

## Web-Panel

Standardmäßig auf `0.0.0.0:9123`, **ohne Login** (`WEBPANEL_AUTH=1` schaltet ihn
wieder an). Übersicht mit Kennzahlen und Kurs-Chart, Nutzer verwalten (Coins,
XP, Titel, Anteile), Server steuern (Sendepause, Ansage, Features), Update-Knopf.

Buchende Knöpfe sind während der Anfrage gesperrt, damit ein Doppelklick nicht
doppelt bucht; `/api/update` lässt nur einen `git pull` gleichzeitig zu.

---

## Aufbau

Ein Modul je Feature, immer nach demselben Muster:

```python
class Feature:
    def setup(self): ...          # aus .env konfigurieren, an/aus entscheiden
    def is_enabled(self): ...
    async def handle(self, message): ...   # None = nicht zuständig

instance = Feature()              # Singleton am Dateiende
handle = instance.handle          # Modul-Aliase danach
```

`bot.py` hält die Handler-Kette (der erste, der nicht `None` liefert, gewinnt),
die Hintergrund-Loops und die Feature-Schalter. `features.py`, `FEATURE_LOADED`
und die Handler-Kette führen dieselben 21 Schlüssel — ein Test prüft das.

**Konventionen:** keine Typannotationen (außer in `@dataclass`, wo Python sie
erzwingt), Kommentare und Docstrings auf Deutsch, Zustand ausschließlich in
`store.JsonStore`.

### Wichtige Module

| Datei | wofür |
|---|---|
| `bot.py` | Handler-Kette, Loops, Lebenszyklus |
| `economy.py` | Level, Coins, Shop, Steuer |
| `floaktie.py` | die $FLO-Aktie |
| `casino.py` | Blackjack, Mines, Crash, Roulette, … |
| `games.py` | Quiz, Anagramm, Mathe, Duelle |
| `render.py` | alle Bilder (PIL) |
| `webpanel.py` / `webpanel.html` | Web-Panel |
| `store.py` | JSON-Speicher, atomar + Sicherung |
| `numfmt.py` | Zahlen-Formatierung und `ist_zahl()` |

### Daten

Alles unter `data/` (nicht im Git). `store.JsonStore` schreibt atomar
(tmp + fsync + replace) und zieht bei jedem Speichern den vorherigen Stand als
`.bak` mit. Ist eine Datei unlesbar, wird sie als `.kaputt-<Zeitstempel>`
beiseitegelegt und die Sicherung eingespielt — **nichts wird stillschweigend
überschrieben**.

---

## Konfiguration (Auszug)

| Variable | Standard | wofür |
|---|---|---|
| `DISCORD_TOKEN` | — | Pflicht |
| `GUILD_ID`, `OWNER_ID` | — | Server und Chef |
| `BOT_NAME` | `Flo` | Ansprache |
| `WEBPANEL_AUTH` | `0` | Login fürs Panel |
| `WEBPANEL_HOST` / `_PORT` | `0.0.0.0` / `9123` | Panel-Adresse |
| `FLOAKTIE_IDLE_30MIN` | `0.11` | Verfall je halber Stunde |
| `FLOAKTIE_CEIL` | `2` | wie weit über den Zielkurs |
| `FLOAKTIE_GRUND_FAKTOR` | `4` | wie stark der Grundwert trägt |
| `AUTODELETE_SWEEP_MAX` | `300` | Nachrichten je Aufräum-Runde |

Alle weiteren stehen als `os.getenv(...)` bei den Konstanten im jeweiligen Modul,
jeweils mit Kommentar, warum der Wert so gewählt ist.
