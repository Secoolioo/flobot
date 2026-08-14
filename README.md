# Flo

Deutschsprachiger Discord-Bot: Level & Coins, Aktie, Casino, Spiele, Musik,
Moderation — dazu ein Web-Panel zum Verwalten.

Läuft als systemd-Dienst (`flobot.service`), Konfiguration über `.env`,
**Einstellungen je Server** über `Flo einstellungen` oder das Panel.

---

## Mehrere Server

Flo läuft auf allen Servern, auf die man ihn einlädt. Jeder Server stellt sich
selbst ein — Kanäle, Lautstärke, Bayrisch, welche Funktionen dort laufen.

```
Flo einstellungen                     alles anzeigen
Flo einstellung lautstaerke 80        setzen
Flo einstellung lautstaerke standard  wieder den Standard nehmen
```

Nur für die, die den Server **verwalten** dürfen. Im Panel geht dasselbe: unter
**Server** auf eine Karte klicken.

| Schlüssel | was |
|---|---|
| `ansage_channel` | wohin Flo von sich aus postet (Händler, Lotto, Shop) |
| `levelup_channel` | wohin die Level-Up-Karten gehen |
| `event_channel`, `zaehl_channel` | Zufalls-Events, Zählspiel |
| `kalorien_channel`, `modlog_channel` | Essensfotos, Moderations-Protokoll |
| `autodelete_channels`, `autodelete_sekunden` | was Flo aufräumt und ab wann |
| `lautstaerke` | womit die Musik hier anfängt (`flo ls 80` speichert das) |
| `bayern` | Flo redet hier boarisch |
| `icon_auto` | Server-Icon nach Tages-/Jahreszeit |
| `aktie_zaehlt` | ob Calls und Chat dieses Servers den $FLO-Kurs bewegen |

Gesetzt wird nur, was abweicht; der Rest folgt dem Standard aus der `.env`. Ein
bestehender Server verhält sich nach dem Update also **unverändert**.

**Was für alle Server gemeinsam gilt:** Coins, Level, Titel, Schulden, Lotto und
die $FLO-Aktie. Es gibt einen Topf und einen Kurs — wer auf zwei Servern ist, hat
dort dasselbe Konto (und bekommt die Voice-Dividende trotzdem nur einmal je Takt).
Zwei Sachen bleiben deshalb auf einem neuen Server erst mal **aus**: die
Icon-Automatik und die Aktien-Zählung. `GUILD_ID` sagt nur noch, wo Flo zu Hause
ist — dort sind beide von Haus aus an.

Der Besitzer behält den Not-Aus: was im Panel **global** abgeschaltet ist, holt
kein Server-Admin zurück.

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
python3 test_games_logic.py    # 130 Tests
python3 test_logic.py          #   6 Tests
python3 bot.py --check         # lädt alle Module ohne zu verbinden
```

Kein pytest nötig — beide Dateien bringen ihren eigenen Runner mit. Sie laufen
in einem Wegwerf-Datenordner (`DATA_DIR` wird ganz oben umgebogen) und fassen
`data/` **nicht** an — ein Testlauf auf dem Server ist also ungefährlich.

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

Nachrichten werden dabei auf eine Minute hochgerechnet: sie sind als einzige
Größe eine *Zählung* über den Takt, alles andere ist ein Zustand. Ohne das hinge
die Aktivität an der Taktlänge (der Bot taktet alle 20 s).

**Steigen:** mehr Aktivität → höheres Tempo, bis zum Deckel. Den nennt
`deckel_fuer(aktivität)` — eine Zahl für Drift, Panel und Sofort-Impuls:
`CEIL_FACTOR` × Zielkurs, nie unter dem Grundwert.

**Fallen:** sobald der Call leer ist, sofort und gleichmäßig — 11 % je halber
Stunde (`IDLE_PER_30MIN`), ohne Anlauf.

Es gibt genau **eine** Aktie für alle Server. Welche davon den Kurs bewegen
dürfen, sagt `aktie_zaehlt`; ihre Aktivität wird zusammengezählt.

**Stillstand gibt es nicht.** Am Deckel und am Grundwert wäre der Trend
rechnerisch 0 — dort *atmet* der Kurs stattdessen: eine kleine Bewegung mit
Vorzeichen, nie exakt 0, im Mittel 0, mit sanftem Zug zurück zum Niveau
(`ATEM_MAX`, `ATEM_RUECK`). Bei kleinen Kursen wächst die Amplitude mit, sonst
würde die Bewegung von der ganzzahligen Anzeige weggerundet.

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

## Profil nachschlagen

```
Flo check @wer      ganzes Profil    (auch: profil, whois, userinfo, steckbrief)
Flo avatar @wer     nur das Bild     (auch: pb, pfp, profilbild, av)
Flo banner @wer     nur das Banner
```

Ohne Ziel bist du selbst gemeint; statt einer Erwähnung geht auch eine ID oder
eine Antwort auf eine Nachricht.

`check`, `user`, `pb` und `av` sind auch normales Deutsch. Steht kein Ziel
dahinter, hält Flo die Klappe und lässt die KI ran — „Flo check mal ob das
läuft" ist eben kein Befehl. Für dein eigenes Profil nimm `Flo profil`.
`bild` gehört weiter dem Bildgenerator (`Flo bild ein Drache aus Neon`).

Das **Profilbild kommt in 4096 px und unbeschnitten** — als `set_image`, denn nur
dort zeigt Discord es rechteckig; als Thumbnail wäre es der übliche Kreis.
Direktlinks (inkl. PNG) stehen dabei, animierte Bilder bleiben animiert.

Dazu: alle Namen, Konto-Alter, Beitritt, Boost, Rollen, Abzeichen, Server-Tag,
Timeout — und was Flo selbst weiß (Level, Coins, Nachrichten, Voice-Zeit,
$FLO-Anteile, Wörter, Verwarnungen).

**Zwei Dinge bewusst anders:**

*Kein Online-Status.* Ohne das `presences`-Intent meldet discord.py **jeden** als
offline. Das wäre kein sichtbarer Fehler, sondern eine stille Falschaussage —
also steht dort gar nichts.

*Namensverlauf führt Flo selbst.* Die Discord-API gibt keinen her, für niemanden,
mit keinem Intent. Flo vergleicht deshalb bei jeder Nachricht Handle,
Anzeigename und Server-Nickname und schreibt nur echte Änderungen mit
(`data/profil.json`). Der Verlauf beginnt also mit dem Tag, an dem die Funktion
aktiv wurde — das Profil schreibt dazu, seit wann Flo jemanden kennt.

Eine Erwähnung liefert übrigens ein vollständiges Member-Objekt, ohne einen
einzigen API-Aufruf — Discord schickt es bei jeder Nachricht mit. Nur das
globale Banner braucht `fetch_user`; das Ergebnis wird 30 min gepuffert, dazu
ein Cooldown von 4 s pro Person.

> **Merke für neue Befehle:** jedes neue Befehlswort gehört in `cmdnorm.KNOWN`.
> Sonst korrigiert die Tippfehler-Suche es auf einen fremden Befehl — `banner`
> wurde zu `banne`, und damit hätte `Flo banner @wer` die Person **gebannt**
> statt ihr Banner zu zeigen. Ein Test prüft das jetzt für alle Profil-Befehle.

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

Titel ab 1 Mio (`KAUF_RUECKFRAGE_AB`) fragen einmal nach: über `Flo kaufen <n>`
kostet ein Tippfehler in einer Ziffer sonst bis zu 90 Mio. Bestätigt wird durch
nochmal denselben Befehl.

---

## Web-Panel

Standardmäßig auf `0.0.0.0:9123`, **ohne Login** (`WEBPANEL_AUTH=1` schaltet ihn
wieder an). Übersicht mit Kennzahlen und Kurs-Chart, Nutzer verwalten (Coins,
XP, Titel, Anteile), Server steuern (Sendepause, Ansage, Features), Update-Knopf.

Ein Klick auf eine Server-Karte öffnet die Einstellungen **dieses** Servers:
Kanäle aus einer echten Auswahlliste, Schalter, Zahlenfelder — und die
Funktions-Schalter, die nur dort gelten.

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
und die Handler-Kette führen dieselben 22 Schlüssel — `test_feature_schluessel_
passen_ueberall_zusammen` prüft das in beide Richtungen und findet auch einen
Schalter, den niemand abfragt.

Alle Hintergrund-Loops laufen über `self.guilds`, nicht über einen festen
Server. Was für die ganze Wirtschaft gilt (Händler, Lotto, Shop-Highlights),
geht in den Ansagen-Kanal **jedes** Servers — mit einer eigenen View pro
Nachricht, denn eine View gehört zu genau einer.

**Konventionen:** keine Typannotationen (außer in `@dataclass`, wo Python sie
erzwingt), Kommentare und Docstrings auf Deutsch, Zustand ausschließlich in
`store.JsonStore`.

### Wichtige Module

| Datei | wofür |
|---|---|
| `bot.py` | Handler-Kette, Loops, Lebenszyklus |
| `guildcfg.py` | Einstellungen je Server |
| `profil.py` | Profil-Lookup + Namensverlauf |
| `cmdnorm.py` | Tippfehler-Korrektur — **jedes neue Befehlswort muss hier rein** |
| `features.py` | Funktions-Schalter (global + je Server) |
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
| `GUILD_ID` | — | Flos Hauptserver (optional, keine Sperre) |
| `OWNER_ID` | — | der Chef |
| `BOT_NAME` | `Flo` | Ansprache |
| `WEBPANEL_AUTH` | `0` | Login fürs Panel |
| `WEBPANEL_HOST` / `_PORT` | `0.0.0.0` / `9123` | Panel-Adresse |
| `FLOAKTIE_IDLE_30MIN` | `0.11` | Verfall je halber Stunde |
| `FLOAKTIE_CEIL` | `2` | wie weit über den Zielkurs |
| `FLOAKTIE_GRUND_FAKTOR` | `4` | wie stark der Grundwert trägt |
| `FLOAKTIE_ATEM_MAX` | `0.06` | wie stark der Kurs höchstens atmet |
| `FLOAKTIE_ATEM_RUECK` | `0.15` | Zug zurück zum Niveau je Minute |
| `AUTODELETE_SWEEP_MAX` | `300` | Nachrichten je Aufräum-Runde |

Alle weiteren stehen als `os.getenv(...)` bei den Konstanten im jeweiligen Modul,
jeweils mit Kommentar, warum der Wert so gewählt ist.
