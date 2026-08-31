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
| `praefix` | wie Flo hier angesprochen wird (`Flo`, `Bob`, …) |
| `bayern` | Flo redet hier boarisch |
| `schulden_pranger` | überfällige Posten (ohne Beträge) im Ansage-Kanal |
| `musik_max_queue`, `casino_max_einsatz` | Obergrenzen je Server |
| `wordle_channel`, `wordle_min_voice` | wohin das Wort des Tages geht und ab wie vielen Leuten im Voice |
| `levelup_ansagen`, `daily_erinnerung` | Level-Karten, Tagesbonus-Erinnerung |
| `icon_auto` | Server-Icon nach Tages-/Jahreszeit |
| `aktie_zaehlt` | ob Calls und Chat dieses Servers den $FLO-Kurs bewegen |
| `soundboard`, `join_sounds` | Sound-Brett und Begrüßungs-Sounds |
| `warn_limit`, `warn_timeout`, `timeout_standard`, `purge_max` | Moderations-Grenzen |

Gesetzt wird nur, was abweicht; der Rest folgt dem Standard aus der `.env`. Ein
bestehender Server verhält sich nach dem Update also **unverändert**.

### Eine Wahrheit für Discord und Panel

Alles aus diesem Katalog steht **automatisch** im Web-Panel — das Panel rendert
den Katalog, es gibt keine zweite Liste, die man vergessen könnte. Und
`Flo einstellung <name> <wert>` schreibt genau die Stelle, die auch der
Panel-Klick schreibt.

Drei Dinge liefen dem früher davon:

- **Soundboard** lag in einer eigenen `voicegags.json`, galt für **alle** Server
  gleich und war nur vom Bot-Besitzer umschaltbar — im Panel tauchte es gar
  nicht auf. Jetzt: je Server, `Server verwalten` genügt. Ein früher global
  abgeschaltetes Board wird beim ersten Start **einmalig übernommen**, damit es
  nicht stillschweigend wieder angeht.
- **Moderations-Grenzen** standen nur in der `.env`: global und erst nach einem
  Neustart wirksam. Jetzt je Server und sofort; die `.env` bleibt der Standard.
- **Join-Sounds** waren überhaupt nicht einstellbar.

Der Rückweg gilt auch: die **Funktions-Schalter** (Casino, Musik, KI …) gab es
nur im Panel. Jetzt auch im Chat:

```
flo funktionen              was auf diesem Server an ist
flo funktion casino aus     abschalten (global aus bleibt global aus)
```

> **Sofort statt nach dem Neustart.** Wer einen Wert nicht bei jedem Gebrauch
> liest, sondern sich ihn merkt, erfährt sonst nie von einer Änderung. Genau das
> war bei der **Lautstärke** so: sie wurde nur beim *Anlegen* eines Players
> gelesen, und Player werden nie weggeräumt — `flo ls 80` griff sofort, ein
> Panel-Klick nie. Module melden sich jetzt mit `guildcfg.horcht_auf(key, fn)`
> an und ziehen nach.

> **Rechte.** Eine Server-Einstellung zu ändern verlangt `Server verwalten` — auf
> jedem Weg. `bayern.py` hat sie vorher völlig ungeprüft umgelegt: ein
> beiläufiges „flo red mal bayerisch" hat für alle umgeschaltet. Ein Test hält
> für *alle* Module fest, dass vor `guildcfg.setzen` geprüft wird.

### Ansprache je Server

```
Flo einstellung praefix Bob      ab jetzt heißt Flo hier Bob
Flo einstellung praefix standard zurück zum Namen aus der .env
```

Wirkt **sofort**, ohne Neustart. Vorher ging das nicht: 21 Module hielten sich
beim Start ihre eigene Kopie von `BOT_NAME`, und der Trigger-Regex wurde einmal
beim Import gebaut. Jetzt ist `ai.py` die einzige Stelle, die den Namen kennt —
alle Module erben von `FeatureBasis`, wo `_bot_name` eine *Eigenschaft* ist, die
zur Laufzeit nachschaut. `bot.on_message` merkt sich per `ContextVar`, welcher
Server gerade dran ist; discord.py bearbeitet jedes Ereignis in einem eigenen
Task, also färbt das keinen anderen Server ein.

Die Ansprache muss **ein Wort** aus 2–32 Zeichen sein und darf keine
Regex-Sonderzeichen enthalten — sie wird zum Erkennungs-Muster. Die Aliasse aus
`BOT_ALIASES` (Standard: `Florian`) bleiben absichtlich global: das sind
Spitznamen der Person, keine Server-Einstellung.

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
python3 test_games_logic.py    # 280 Tests
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

## Wenn die KI nicht antwortet

```
bash k setup     einmalig - danach reicht ueberall:  k
k                Konfig, Netz, Modell, echter Aufruf + letzte Fehler
k n              vorher git pull
k l              nur das Log (KI)
k p              Panel-Zugang + das gewuerfelte Passwort
k m              Musik/Spotify pruefen
k r              Dienst neu starten
```

`tools_ki_check.py` fragt den Anbieter direkt und sagt in Klartext, woran es
liegt — statt eines Tracebacks, den auf dem Handy niemand liest. Der Schlüssel
wird dabei **nie** vollständig ausgegeben, auch nicht im Fehlerfall.

### Flo holt sich selbst wieder raus

Zwei Ausfälle passieren im Betrieb wirklich, und beide löst Flo inzwischen
allein — er sagt danach im Log, was dauerhaft in die `.env` gehört:

| Was passiert | Was Flo tut |
|---|---|
| Anbieter **mustert das Modell aus** (404) | holt die aktuelle Modell-Liste, nimmt das größte taugliche und macht weiter |
| **Cloudflare** sperrt die Client-Signatur (403, `error code: 1010`) | probiert andere Signaturen durch, bis eine durchkommt |
| **Ratenlimit** (429) oder Störung (5xx) | wiederholt mit wachsendem Abstand (0,8 s · 2,4 s · 6 s) |
| **Schlüssel abgelehnt** (401), Anfrage kaputt (400) | wiederholt **nicht** — das wird beim zweiten Mal nicht besser |

Wichtig zum Einordnen: **401 heißt Schlüssel, 403 mit `error code: 1010` heißt
Cloudflare.** Im zweiten Fall erreicht die Anfrage den Anbieter nie — Schlüssel
tauschen oder Modell umstellen bringt dort gar nichts. Kommt keine Signatur
durch, ist die IP des Anschlusses gesperrt; dann hilft kein Code, sondern nur
eine andere Route ins Netz.

### Statt einem Satz für alles

Früher fingen **vier** Stellen in `ai.py` ein nacktes `except Exception` ab und
gaben alle denselben Satz zurück — der Grund verschwand im Traceback. Jetzt gibt
es genau **einen** Weg zum Anbieter (`FloAI._chat`), dort steht die ganze
Politik, und jede Ursache hat ihren eigenen Satz im Chat. Ein Test hält fest,
dass es bei einem Weg bleibt.

Störungsmeldungen landen außerdem **nicht mehr im Kurzzeit-Gedächtnis**. Vorher
schrieb `bot.py` jede Antwort mit, also auch „Mein KI-Dienst antwortet gerade
nicht" — die ging als Gesprächsverlauf wieder ans Modell, das sie brav
nachplapperte.

Beim Start prüft Flo einmal wirklich nach (`ai.selbsttest()`). Vorher hing die
Meldung „KI-Feature aktiv" allein daran, dass ein Schlüssel in der `.env` stand.

**Groq mistet aus.** Am 17.06.2026 wurden `llama-3.3-70b-versatile` und
`llama-3.1-8b-instant` im Frei- und Entwicklertarif abgeschaltet — genau das war
die Ursache eines Ausfalls hier. Die Vorgaben zeigen deshalb auf Groqs eigene
Nachfolger-Empfehlungen: `openai/gpt-oss-120b` fürs Chatten (kann Werkzeuge, das
braucht `ask_flo` fürs Wetter) und `qwen/qwen3.6-27b` fürs Bild-Lesen (gpt-oss
kann keine Bilder; qwen3.6 nimmt Text, Bild und Video, max. 5 Bilder und 20 MB
pro Anfrage). Passiert es wieder, wechselt Flo selbst und schreibt die neue
Zeile für die `.env` ins Log.

| `.env` | was |
|---|---|
| `LLM_API_KEY` | Schlüssel beim Anbieter (Groq) |
| `LLM_BASE_URL` | Standard `https://api.groq.com/openai/v1` |
| `LLM_MODEL` | Chat-Modell (Standard `openai/gpt-oss-120b`) |
| `LLM_VISION_MODEL` | Bild-Modell (Standard `qwen/qwen3.6-27b`) |
| `LLM_USER_AGENT` | Client-Signatur — nur nötig, wenn Cloudflare blockt |
| `LLM_TEMPERATURE` | 0 = brav, ~1.2 = chaotisch (Standard 0.9) |
| `LLM_MAX_TOKENS` | Antwortlänge (Standard 800) |
| `LLM_REASONING_EFFORT` | `low`/`medium`/`high` — nur für Denk-Modelle wie `gpt-oss`. Antwortet Flo öfter „fällt mir nichts ein", steht das Budget im Denken statt im Reden. **Bei `gpt-oss` setzt Flo von sich aus `low`**, weil die Modelle sich beim Nachdenken selbst zahm reden |
| `BOT_PERSONA` | ersetzt die komplette Persönlichkeit. **Achtung:** ist die gesetzt, wirkt keine Änderung an der Standard-Persona mehr |
| `BOT_POLITIK` | `aus` nimmt Flo die politische Haltung |

### Wenn Flo zahm wird

Passiert das, ist es fast nie „das Modell ist halt so". Der Reihe nach:

1. **Der Titel.** Die Tonfall-Texte in `titles.py` waren früher eine
   *Zahmheits-Rampe*: je seltener der Titel, desto braver Flo — ganz oben stand
   wörtlich „Kein Roasten, keine fiesen Sprüche". Ausgerechnet die Stammgäste
   bekamen dadurch einen lieben Flo. Heute ändert die Seltenheit nur noch, **wie**
   geroastet wird, nie **ob**; ein Test hält das fest.
2. **`BOT_PERSONA` in der `.env`.** Steht die dort, ersetzt sie alles.
3. **Das Modell.** `openai/gpt-oss-*` prüft sich vor jeder Antwort selbst und
   verweigert deutlich öfter. Alternative: `LLM_MODEL=qwen/qwen3.6-27b`. Erst
   `bash k` laufen lassen — dort steht die echte Modell-Liste des Anbieters.
4. **Verweigert das Modell einen Roast**, nimmt Flo einen Fertig-Spruch — das
   sah aus wie Langeweile und stand früher in *keinem* Log. Heute schon:
   `bash k` zeigt `KI-Fehler: Roast verweigert`.

> Flos Haltung ist derb und politisch **rechts** — das ist eine Meinung und so
> gewollt. Die eine Grenze bleibt davon unberührt und ist nicht abschaltbar:
> keine Hetze gegen Herkunft, Hautfarbe, Religion, Geschlecht, sexuelle
> Orientierung oder Behinderung, keine Nazi-Verherrlichung, keine echten
> Drohungen, keine privaten Daten — und wer wirklich am Boden ist, wird nicht
> weitergeroastet. Ein Test prüft, dass diese Liste vollständig bleibt.

### Flos Gedächtnis

Flos Erinnerung reichte früher **12 Nachrichten und 20 Minuten** weit — danach
war alles weg. Jeden Tag dieselben Fragen, kein einziger Rückbezug auf gestern.
Jetzt baut er sich ein Gedächtnis auf, das Neustarts übersteht.

```
flo gedaechtnis        was Flo über dich weiß (und über den Server)
flo vergiss mich       löscht alles davon – sofort
flo vergiss @wer       nur mit „Server verwalten"-Recht
```

Wie es läuft: jede öffentliche Nachricht landet in einem kleinen Puffer je
Server (billig, synchron). Alle 10 Minuten geht der Puffer **gesammelt** an die
KI, die daraus kurze, dauerhafte Fakten zieht — „spielt Terraria", „hasst
Montage", „verliert jede Wette gegen Ben". Die fließen dann beiläufig in Flos
Antworten ein.

Was **nicht** passiert, und zwar mit Absicht:

| | |
|---|---|
| **Keine DMs** | nur was öffentlich im Server steht |
| **Nichts wandert zwischen Servern** | was hier gesagt wurde, weiß Flo dort nicht |
| **Keine privaten Daten** | die KI ist darauf verpflichtet — und weil eine Anweisung keine Zusicherung ist, filtert Flo danach *noch einmal* hart nach: Mail, Telefon, IBAN, Adresse, Links, Passwörter |
| **Nichts Unsichtbares** | jeder sieht und löscht sein eigenes Zeug |
| **Kein unbegrenztes Wachstum** | 25 Fakten je Person, 20 je Server. Oft Bestätigtes überlebt, Einmaliges fliegt zuerst raus; nach 120 Tagen ohne Bestätigung ist es weg |

Abschaltbar wie jedes Feature (`flo features` / Web-Panel, Schlüssel `gehirn`).
Taktrate über `GEHIRN_TICK_SECONDS` (Standard 600).

> Der Puffer wird **vor** dem KI-Aufruf geleert. Scheitert der Aufruf, sind die
> Nachrichten weg — das ist gewollt: sonst schickt ein dauerhaft kaputter Aufruf
> alle 10 Minuten dieselben 120 Zeilen und der Puffer nimmt nie wieder etwas
> Neues auf.

---

## Musik

```
Flo spiel <suchbegriff>        YouTube-Suche
Flo spiel <link>              YouTube, Spotify, SoundCloud, direkte Audiodatei
Flo pause / weiter / skip / stop / leave / queue
Flo ls 80                     Lautstärke (wird je Server gespeichert)
Flo history                   Verlauf der gespielten Songs
Flo nochmal 3                 Nummer 3 aus dem Verlauf nochmal
```

Dazu die normalen Sätze: „mach mal *X* an", „leg *X* auf", „hau *X* raus",
„mach die Musik aus". Fragen (*„spielst du …"*) sind bewusst **kein** Befehl.

### Verlauf: was lief zuletzt?

```
Flo history            Flo nochmal verlauf      Flo musik verlauf
Flo again history      Flo replay history       Flo nochmal history
```

Tippfehler sind eingerechnet (`nohmal history`, `nochmall verlauf`,
`nochmal histori`, `nochmal verlauv` …): ab sechs Zeichen zählt ein
Levenshtein-Abstand von 2 noch als Vertipper. Kürzere Wörter bleiben außen vor —
mit Abstand 2 auf kurze Wörter wäre plötzlich jeder zweite Befehl „Verlauf".

Die Liste zeigt **10 Songs pro Seite**, neuester ist **Nr. 1**, mit Interpret/
Titel, wer ihn angefordert hat und wie lange es her ist. Darunter ein
**Dropdown** (Klick spielt sofort) und **◀ ▶** zum Blättern; nach ~3 Minuten
werden die Knöpfe deaktiviert. Bedienen darf, wer den Befehl abgesetzt hat —
oder wer Nachrichten verwalten darf, genau wie bei den anderen Musik-Knöpfen.

Gespeichert wird in `data/musikverlauf.json`, **je Server**, die letzten
**100** Songs (`MUSIC_VERLAUF_MAX`); ältere fallen automatisch raus. Der
Verlauf überlebt Neustarts — der Player selbst hält nur die letzten 30 im
Arbeitsspeicher, und ausgerechnet nach einem Neustart fragt man „was lief
gestern?". Derselbe Song zweimal hintereinander (Neustart nach einem Stall,
Seek) ist **ein** Eintrag, sonst steht der Verlauf nach einer Störung voll
damit.

`Flo nochmal 3` meint dieselbe **3** wie die Liste — auch `nochmal nummer 3`
oder `nochmal nr. 3`. Vorher las `nochmal` aus dem flüchtigen Speicher des
Players, der andersherum zählte und nach einem Neustart leer war.

> ⚠️ **`Flo verlauf` allein bleibt beim Handelsbuch.** Das Wort gehört seit
> jeher `handel.py`, und `music.handle` läuft in der Kette **vor** `handel` —
> Flo würde also Songs zeigen, wenn jemand seine Coin-Umsätze sehen will. Ein
> Test hält das fest. Für die Musik gibt es `history` und alle
> Zwei-Wort-Formen.

**SoundCloud** läuft ohne Key und ohne Login — yt-dlp bringt den Extractor mit,
es fehlte nur die Erkennung. Erkannt werden einzelne Tracks
(`soundcloud.com/wer/was`), **Sets** (`/sets/…`, kommen als ganze Playlist in die
Warteschlange, bis `MAX_QUEUE`) und die Kurzlinks aus der App
(`on.soundcloud.com/…`) — den Redirect löst yt-dlp selbst auf. Ebenso spielt Flo
eine **direkt verlinkte Audiodatei** (`.mp3`, `.m4a`, `.opus`, `.flac`, …). Nicht
abspielbar sind Go+-Titel: SoundCloud gibt dafür nur die 30-Sekunden-Vorschau
heraus, und die spielt Flo dann auch — es gibt kein Signal, an dem man das vorher
sicher erkennt.

Andere Webseiten-Links fallen **absichtlich** durch: sonst würde jeder geteilte
Link im Chat einen Abspielversuch auslösen.

**Warum die Warteschlange früher hängenblieb:** FFmpeg wartet bei einem stillen
Stream ohne Timeout ewig. Dann feuert `after` nie, `is_playing()` bleibt `True`,
und jedes weitere `Flo spiel …` landet nur noch in der Queue — genau das
bekannte „Queue voll, spielt aber nicht". Dagegen jetzt zweierlei:

* `-rw_timeout` bricht ab, wenn keine Daten mehr kommen (die `-reconnect`-Flags
  allein reichen nicht — die brauchen einen Fehler oder EOF, und ein stiller
  Hänger ist keins von beidem).
* Ein Wächter zählt die tatsächlich abgespielten Frames
  (`AudioPlayer.loops` — das einzige ehrliche Signal, `position()` ist nur
  Wanduhr). Stehen sie zwei Takte still, startet Flo denselben Song an derselben
  Stelle neu; Warteschlange, Tempo und Pause bleiben erhalten.

Dazu heilt der Wächter zwei weitere Sackgassen: ein Song, der während eines
Voice-Ausfalls endet, und ein Geister-Track nach `stop`.

---

## Wenn YouTube blockt

YouTube prüft, ob ein echter Browser dahintersitzt. Welcher „player_client"
ohne Login durchkommt, **ändert sich alle paar Monate** — deshalb steht im Code
kein fester Name, sondern eine Reihe:

```
Musik: YouTube blockt (botcheck) mit client=Standard - versuche android_vr.
Musik: YouTube ging erst mit player_client='ios'.
       Dauerhaft machen mit  YTDLP_PLAYER_CLIENT=ios  in der .env.
```

Flo merkt sich, was funktioniert hat, und fängt beim nächsten Song damit an.
Nachgesetzt wird **nur** bei Bot-Prüfung, Format- und Veraltet-Fehlern — bei
einem gelöschten oder gesperrten Video hilft kein anderer Client, dort gibt er
sofort auf statt den Nutzer warten zu lassen.

Die Reihenfolge ist **nicht beliebig**: YouTube verlangt für die meisten
Clients inzwischen ein sogenanntes **PO Token**, das yt-dlp gar nicht selbst
erzeugen kann. Genau drei Clients kommen ohne aus — `tv`, `android_vr` und
`web_embedded` — und nur die haben auf einem nackten Server überhaupt eine
Chance. Deshalb stehen sie vorn.

Der Client-Wechsel gilt für **Abspielen, Suche und Playlists** gleichermaßen.
Das ist kein Detail: wird schon die *Suche* geblockt, kommt `Flo spiel <titel>`
nie bis zum Abspielen — ein Wechsel nur beim Abspielen hätte gar nichts genützt.

**Kommt kein Client mehr durch**, ist die IP markiert. Dann sucht Flo denselben
Song bei **SoundCloud** — das kennt YouTubes Bot-Prüfung nicht und ist der
einzige Weg, der ohne Zutun des Betreibers noch Musik liefert:

```
Musik: YouTube blockt komplett - spiele 'Semmel Song' von SoundCloud.
```

Wer das *nicht* will, weil ein stiller Umweg das eigentliche Problem verdeckt,
schaltet es mit `MUSIC_SOUNDCLOUD_FALLBACK=0` ab und bekommt stattdessen eine
ehrliche Fehlermeldung.

### YouTube wieder ans Laufen bringen: `k y`

```
k y                        was geht, was nicht, was tun
k y browser firefox        Cookies aus einem Browser auf DIESEM Rechner
k y datei /pfad/cookies.txt  eine exportierte cookies.txt einrichten
```

`k y` probiert jeden Client wirklich durch und sagt dann in einem Satz, woran
es liegt. Es unterscheidet dabei ausdrücklich **„YouTube blockt deine IP"** von
**„der Server kommt gar nicht erst ins Netz"** — das sind zwei völlig
verschiedene Reparaturen, und die falsche kostet einen Abend.

Es gibt drei Wege zurück:

| Weg | wie | Aufwand |
|---|---|---|
| **Cookies** eines angemeldeten Kontos | `k y browser firefox` oder `k y datei …` | klein |
| **PO-Token-Anbieter** | `pip install -U bgutil-ytdlp-pot-provider` + dessen Server | mittel, aber ohne Konto |
| **Eigener Ausgang** | `YTDLP_PROXY=socks5://127.0.0.1:1080` | je nachdem |

> ⚠️ **Nimm für Cookies einen Wegwerf-Account.** YouTube sperrt Konten, deren
> Cookies von einem Server aus benutzt werden — der Haupt-Account wäre dann weg.

Beim Export ist die Reihenfolge entscheidend, sonst sind die Cookies sofort
tot: **privates Fenster** öffnen → bei YouTube anmelden → im *selben* Tab
`youtube.com/robots.txt` aufrufen → Cookies exportieren (Format *Netscape*) →
das Fenster schließen, **ohne sich abzumelden**. YouTube dreht Cookies in
offenen Tabs sonst laufend weiter.

Eine `cookies.txt` muss **nicht** in die `.env`: liegt sie neben `bot.py` oder
in `data/`, findet Flo sie von selbst — praktisch, wenn man an einer
noVNC-Konsole ohne Copy-Paste sitzt. Eine *leere* Datei wird dabei übergangen,
statt yt-dlp mit einem leeren Zugang loslaufen zu lassen.

> 🔒 `cookies.txt` ist eine **Anmeldung bei Google**. Sie steht in `.gitignore`,
> und ein Test hält das fest.

| `.env` | was |
|---|---|
| `YTDLP_PLAYER_CLIENT` | einen Client festnageln (spart das Durchprobieren) |
| `YTDLP_COOKIES` | Pfad zur `cookies.txt` (sonst wird sie gesucht) |
| `YTDLP_COOKIES_FROM_BROWSER` | z. B. `firefox` — nur mit Browser auf dem Server |
| `YTDLP_PO_TOKEN` | PO Tokens von Hand, mehrere per Komma |
| `YTDLP_PROXY` | eigener Ausgang **nur** für yt-dlp (Discord/KI bleiben direkt) |
| `MUSIC_SOUNDCLOUD_FALLBACK` | `0` = lieber eine ehrliche Fehlermeldung |
| `MUSIC_VERLAUF_MAX` | wie viele gespielte Songs je Server erhalten bleiben (Standard 100) |

Prüfen lässt sich das alles mit `k y` und `k m`.

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

## Deutsch, Englisch, Boarisch — und Tippfehler

Flo versteht jeden Befehl in **vier Anläufen** (`cmdnorm.py`), immer nur am
ersten Wort und immer nur, wenn die Nachricht an Flo gerichtet ist:

| # | Topf | Beispiel | Wirkung |
|---|------|----------|---------|
| 1 | `KNOWN` (526) | `skip` | schon gültig → **nichts** anfassen |
| 2 | `ALIAS` (17) | `wipe 50` → `purge 50` | fremdsprachiges Synonym |
| 3 | `DIALECT` (98) | `hackln` → `arbeit` | boarisch/österreichisch |
| 4 | Tippfehler | `skpi` → `skip` | eine Einfügung/Löschung/Vertauschung |

**Warum ALIAS und nicht KNOWN?** `KNOWN` heißt *„ist schon ein gültiger Befehl,
schreib nichts um"*. Ein Synonym dort wäre tot: das Wort ginge unverändert an
ein Modul, das es nicht kennt, und fiele zur KI durch. Übersetzt wird nur in
`ALIAS`/`DIALECT`. Beide werden **exakt** verglichen und sind bewusst keine
Tippfehler-Ziele — nachgemessen zog das sonst Vornamen mit rein (`emma` →
`leave`, `lisa` → `leiser`) und `normal` wurde zu `nochmal`.

**Ersetzungen sind gesperrt.** Ein Buchstabe gegen einen anderen erzeugt viel zu
oft ein anderes echtes Wort (`hast`→`halt`, `plan`→`play`, `nice`→`dice`). Echte
Vertipper sind fast immer vertauschte, verdoppelte oder fehlende Buchstaben.

**`STOPWORDS` (184) sind die Bremse** — nachgemessen am ganzen Wortschatz des
Repos (13.882 Wörter), nicht geraten. Das waren echte Fehlgriffe:

| war | wurde zu | Schaden |
|-----|----------|---------|
| `Flo heisst du Flo?` | `heist` | **Coin-Raub** gestartet |
| `Flo pure ...` | `purge` | **Massenlöschen** |
| `Flo nebel` | `knebel` | **Timeout** |
| `Flo anne` (Vorname) | `banne` | **Bann** |
| `Flo cash` | `crash` | Crash-**Wette** |
| `Flo build …` | `bild` | KI-Bildauftrag (kostet) |
| `Flo komma` | `komm` | Flo in den Voice geholt |

Dieselbe Messung hat auch 16 **echte** Befehle gefunden, die `cmdnorm` verbogen
hat, weil sie in `KNOWN` fehlten (`sendpause`→`sendepause`, `time-out`→`timeout`,
`naehrwert`→`naehrwerte`, `rausschmeiss`→`rausschmeis` …). `guildcfg` und
`giveaway` standen mit **keinem** ihrer Befehle drin. Drei Tests halten das jetzt
fest, und sie hängen an den Befehlslisten der Module selbst — läuft eine Liste
auseinander, fällt es sofort auf.

---

## Wirtschaft

**Quellen:** Nachrichten (8–16, mit Cooldown), Voice (30/Minute, gedeckelt auf
14.400 am Tag ≙ 8 Stunden), Daily-Bonus mit Streak, Level-Ups, Aktien-Dividende,
Spiele, Casino und **Arbeit** (siehe unten).

**Senken:** Shop-Titel, Luxus, Thron, Händler, Lotto, Verkaufssteuer und die
tägliche **Vermögenssteuer** (Freibetrag 5 Mio, darüber 2 %, ab 400 Mio nur noch
0,4 %). Sie verbrennt die Coins — es gibt kein Gegenkonto.

Die Spiele haben eine **Tageskappe** für Netto-Gewinne; der Einsatz kommt immer
voll zurück.

Titel ab 1 Mio (`KAUF_RUECKFRAGE_AB`) fragen einmal nach: über `Flo kaufen <n>`
kostet ein Tippfehler in einer Ziffer sonst bis zu 90 Mio. Bestätigt wird durch
nochmal denselben Befehl.

---

## Arbeit

Casino ist Glück, die Aktie ist Timing — **Arbeit** ist der dritte Weg, und hier
zählt Können.

```
Flo work              Schicht antreten (zufällige Aufgabe)
Flo work safe         gezielt eine bestimmte Schicht
Flo work liste        was es gibt, mit Häufigkeit und Lohn
Flo wordle            Wordle zum Spaß, jederzeit (max. 15.000)
Flo wordle 7          … mit 7 Buchstaben
Flo tageswort         zum Wort des Tages (der fette Topf)
Flo lohnzettel        eigene Bilanz als Karte (auch: Flo lohnzettel @wer)
Flo work top          Werk-Rangliste
```

Es gibt **drei** Wordle-Sorten, und sie sind absichtlich klar getrennt:

| | wann | Topf |
|---|---|---|
| `Flo wordle` | jederzeit, 2 Min Pause | **max. 15.000** |
| ⭐ Wordle-**Schicht** | ~jede 18. `Flo work` | bis ~66.000 |
| **Wort des Tages** | 1× täglich im Kanal | 50.000–160.000 |

Das Spaß-Wordle hat einen **harten Deckel** — er greift *vor* dem Gold-Bonus,
sonst wäre die Obergrenze in Wahrheit das Doppelte und „nie mehr als 15.000"
gelogen. Es zählt außerdem **nicht** für Stufe und Serie: die Karriere soll für
Arbeit stehen, nicht für Zeitvertreib — sonst wäre der Werksleiter der, der am
meisten geraten hat. Eigener kurzer Cooldown, damit es die Schicht nicht
blockiert (und umgekehrt).

| Schicht | Aufgabe | Grundlohn | Anteil |
|---|---|---|---|
| `safe` | Zahlenschloss, Hinweise nach jedem Versuch | 7.500 | ~16 % |
| `salat` | Buchstabensalat entwirren, drei Versuche | 7.000 | ~16 % |
| `paare` | Werkzeug sortieren: vier Paare, wenige Griffe | 6.800 | ~16 % |
| `rechnen` | Fünf Aufgaben, eine falsche beendet die Schicht | 6.500 | ~16 % |
| `kontrolle` | Qualitätskontrolle: den Ausschuss finden, dreimal | 6.200 | ~16 % |
| `sortieren` | Zahlen in die richtige Reihenfolge klicken | 6.000 | ~16 % |
| ⭐ **Wordle** | Fünf Buchstaben, sechs Versuche | **22.000** | **~6 %** |

**Wordle ist die seltene Schicht.** Sie kommt etwa jede **18. Schicht** von
allein — und lässt sich **nicht bestellen**: `Flo work wordle` gibt eine
freundliche Absage statt der Schicht. Beides gehört zusammen; eine seltene
Aufgabe, die man sich jederzeit holen kann, ist nicht selten, sondern nur
schlecht sortiert. Dafür zahlt sie das Dreifache.

Dazu kommt die **Leistung** (wie schnell/sauber) und die **Serie**: jede
geschaffte Schicht in Folge legt 5 % drauf, gedeckelt bei +50 %. Ein Reinfall
setzt sie zurück — deshalb lohnt es sich, eine schwere Schicht zu Ende zu
bringen statt sie wegzuklicken. Gut gespielt liegen 15.000–20.000 pro Schicht
drin.

**Cooldown 15 Minuten**, **Tagesdeckel 250.000**. Ohne den Deckel wäre die
Schicht die beste Geldquelle im Spiel — sie ist ja risikofrei.

### Karriere

Das Rückgrat: die **Stufe** zählt *geschaffte* Schichten und **fällt nie**. Die
Serie ist nach einem Reinfall weg — über Wochen baute man vorher gar nichts auf.

| ab … Schichten | Stufe | Zuschlag |
|---|---|---|
| 0 | 🧻 Praktikant | +0 % |
| 10 | 🧹 Aushilfe | +8 % |
| 30 | 🔧 Facharbeiter | +16 % |
| 75 | 📋 Vorarbeiter | +25 % |
| 150 | 🎖️ Schichtleiter | +34 % |
| 300 | 🏅 Meister | +42 % |
| 600 | 👑 Werksleiter | +50 % |

Serie und Stufe werden **addiert**, nicht multipliziert:
`Grundlohn × Leistung × (1 + Serie + Stufe)`. Multiplikativ schaukeln sich zwei
Zuschläge von je +50 % zu +125 % auf — und dann überholt die Schicht das Wort des
Tages, das der Höhepunkt bleiben soll. So liegt der Höchstfaktor bei **2,0**, und
selbst die beste normale Schicht bleibt unter dem kleinsten Tagestopf. Ein Test
rechnet das nach.

**🥇 Goldene Schicht:** jede Schicht hat ~8 % Chance, **doppelt** zu zahlen. Das
wird **beim Start** gewürfelt und steht dran, während man arbeitet — das ist der
halbe Spaß daran. Auf null bleibt null: für eine verpatzte Schicht gibt es auch
doppelt nichts.

`Flo lohnzettel` zeigt das als gerenderte Karte: Stufe, Fortschrittsbalken zur
nächsten, geschafft/angetreten, Serie samt Bestwert, Verdienst, Tagesstand,
goldene Schichten und die **Wordle-Bilanz** mit der Verteilung der Versuche.
`Flo work top` dasselbe als Rangliste. Fällt das Zeichnen aus, kommen dieselben
Zahlen als Text — eine fehlende Schrift darf niemandem seine Bilanz vorenthalten.

**Eine laufende Schicht wird nicht weggeräumt.** Sie meldet sich beim
Auto-Lösch-Schutz an (`bot.protect_message`) und wird erst freigegeben, wenn sie
wirklich vorbei ist. Vorher verschwand sie in einem Aufräum-Kanal mitten im
Spiel — der Cooldown lief weiter, der Lohn war weg. Läuft die Zeit ab, werden
nur die **Knöpfe** abgenommen; der Stand bleibt stehen, damit man noch sieht,
woran man saß. Die Frist hängt an der Aufgabe: Wordle 15 Minuten, Salat und
Safe 7, der Rest 5.

### Wort des Tages

Einmal am Tag legt Flo ein Wordle in den dafür eingestellten Kanal.

> **Von Hand auslösen:** Im Web-Panel steht unter *Server → Sofort auslösen* ein
> Knopf **„Wort des Tages jetzt starten"**. Er umgeht die Voice-Schwelle und den
> gewürfelten Termin — für den Fall, dass der Server sichtbar voll ist und
> trotzdem nichts kommt. Läuft an dem Tag schon eine Runde, wird sie **nicht**
> neu gestartet, sondern nur der Aushang noch einmal ausgehängt: niemandem
> werden seine Versuche genommen. Ist das Wort schon gelöst, sagt der Knopf das,
> statt ein zweites Rätsel hinzuwerfen. Das ist ein
**Wettrennen**: wer es als Erster knackt, nimmt den ganzen Topf, danach ist die
Runde durch und das Wort wird aufgelöst. Jeder rät für sich — die eigenen
Versuche sieht nur man selbst.

Das Brett ist ein **gerendertes Bild** (`render.wordle_board`), kein
Emoji-Salat: echte Kacheln in den bekannten Farben und darunter eine
**Tastatur**, die zeigt, welche Buchstaben grün, gelb oder durch sind. Das ist
die eigentliche Denkhilfe — vorher musste man sich merken, was man schon
ausgeschlossen hatte. Gezeichnet wird im Thread (`asyncio.to_thread`), sonst
stünde der Bot währenddessen still. Sind alle sechs Versuche weg, während das
Rennen noch läuft, bekommt man das Brett **verdeckt** zu sehen: die Farben ja,
die Buchstaben nicht — sonst könnte man die Lösung im Chat ausplaudern.

Die Färbung kommt aus **einer** Quelle: `Wordle.farben()` rechnet, `muster()`
macht Emojis daraus, das Bild nimmt dieselben Zeichen. Zwei Rechnungen für
dieselbe Sache wären garantiert irgendwann verschieden falsch.

Der Topf hängt an der **Wortlänge** — und die wird **jeden Tag neu gewürfelt**:

| Buchstaben | Grundtopf | auf Anhieb | Mo–Fr | Sa/So |
|---|---|---|---|---|
| 5 | 50.000 | 100.000 | 34 % | 16 % |
| 6 | 60.000 | 120.000 | 30 % | 26 % |
| 7 | 70.000 | 140.000 | 21 % | 30 % |
| 8 | **80.000** | **160.000** | 15 % | 28 % |

Vorher hing die Länge am Wochentag — Mo–Do also immer fünf Buchstaben, vier Tage
die Woche dieselbe Aufgabe und derselbe Topf. Jetzt weiß man morgens nicht, was
kommt. Am Wochenende verschiebt sich das Gewicht nach oben, garantiert ist nichts.

Der Faktor sinkt mit jedem Versuch (1× → ×2,0 … 6× → ×1,0), unter den Grundtopf
geht es nie. Die Wordle-**Schicht** aus `Flo work` zahlt trotz ihrer Seltenheit
nur einen Bruchteil davon — das Wort des Tages bleibt der Höhepunkt.

**Nach der Entscheidung darf man weiterraten** — für die eigene Bilanz, nicht
fürs Geld. Der Topf gehört dem Ersten, daran ändert das nichts; aber vorher war
für alle außer dem Sieger der Tag gelaufen, sobald jemand schneller war. Jetzt
zählt jeder Erfolg in die Wordle-Verteilung auf dem Lohnzettel.

**Wann es fällt, entscheidet nicht die Uhr, sondern der Server** — und dann noch
der Zufall:

1. Es müssen mindestens `wordle_min_voice` Leute (Standard **3**, Bots zählen
   nicht) in einem Sprachkanal sitzen. Ist an einem Tag nie was los, fällt das
   Wort aus — ein Rätsel um 4 Uhr morgens in einen leeren Server zu werfen wäre
   verschenkt.
2. Dann zieht Flo einen Zeitpunkt in den nächsten **5–45 Minuten** und wartet.
   Sonst hinge das Wort sichtbar an der dritten Person im Call, und man könnte
   es sich herbeiholen. Der Termin wird **einmal** gezogen und bleibt stehen —
   würde er bei jedem Tick neu gewürfelt, rückte er ewig weiter weg. Leert sich
   der Call vorher, wartet Flo, bis wieder genug da sind.

**Kanal einstellen** (sonst sucht Flo einen, der `gigachat` oder `wordle` heißt,
und nimmt notfalls den Ansagen-Kanal):

```
Flo einstellung wordle_channel <Kanal-ID>
Flo einstellung wordle_min_voice 3
```

Das Wort ist **berechnet**, nicht gewürfelt: derselbe Server bekommt an demselben
Tag immer dasselbe Wort — auch nach einem Neustart mitten im Raten — und zwei
Server bekommen verschiedene, damit man sich die Lösung nicht von nebenan holt.
Die Wortlisten enthalten bewusst **keine Umlaute und kein ß**: bei einem
Buchstabenspiel muss jeder Buchstabe eindeutig eintippbar sein.

---

## Das Schuldbuch

```
Flo leih @wer 5k Pizza bis freitag    Kredit anbieten
Flo pay @wer 5k als leihgabe          dasselbe, aus der Zahlung heraus
Flo schuldschein @wer 5k Kino         Schuld ohne Geldfluss
Flo schulden [@wer | top | erlassen @wer [x]]
Flo tilg @wer [betrag]                freiwillig abzahlen
Flo insolvenz                         Neuanfang (kostet 50 % + Score)
```

> **Eine Schuld entsteht nur durch Zustimmung.** Wer zahlt, schenkt. Wer leiht,
> leiht ausdrücklich.

Vorher machte **jede** `Flo pay`-Zahlung den Empfänger automatisch zum Schuldner
— ein Geschenk, ein verlorener Wetteinsatz, eine geteilte Rechnung, alles wurde
stillschweigend zur Forderung, ohne dass es einer der beiden so gemeint hatte.
Jetzt braucht jede Schuld einen **Klick der Person, die sie bekommt**: Flo postet
die Anfrage mit zwei Knöpfen, und erst „Annehmen" bewegt Geld und legt den Posten
an. Ohne Klick (oder nach 5 Minuten) passiert gar nichts. Beim Schuldschein
bestätigt der **Schuldner** — niemand kann einem Fremden per Befehl Schulden
anhängen.

Geführt werden **einzelne Posten**, kein Netto-Saldo je Paar. Nur so gibt es
Fälligkeit, Grund und Historie je Schuld. Der Saldo zwischen zwei Leuten wird
immer *berechnet* — ein gespeicherter Saldo wäre eine zweite Wahrheit, die
irgendwann von der ersten abweicht. Ein erledigter Posten bleibt stehen.

**Tilgung.** 20 % jeder echten Einnahme gehen automatisch an die Gläubiger —
**anteilig an alle** (früher bekam der größte alles, und wer bei drei Leuten in
der Kreide stand, sah bei zweien monatelang nichts), überfällige Posten zuerst,
innerhalb einer Person der älteste zuerst. Rückbuchungen sind ausgenommen (jeder
Grund auf `-rueck` oder `-rueckgabe`) — ein zurückgegebener Einsatz ist keine
Einnahme. Eine Zahlung an jemanden, dem man etwas schuldet, wird angerechnet;
was darüber hinausgeht, ist ein Geschenk und erzeugt **keine** Gegenforderung.

**Wie viel geht?** Genau eine Regel: **verleihen kannst du alles, was auf deinem
Konto liegt** — dein ganzes Geld, aber keinen Coin mehr. **Schulden** darfst du
ausdrücklich *mehr, als du besitzt*: wer pleite ist, braucht das Geld; wer reich
ist, nicht. Früher war beides am „Vermögen" festgemacht (Score-Prozent des
Verleihers je Posten, Gesamtschuld höchstens das Dreifache des eigenen
Vermögens) — zwei Regeln, die niemand erraten konnte und die genau den Fall
verboten, um den es beim Leihen geht.

Beim **Schuldschein** fließt kein Geld, dort gilt die Kontostand-Grenze nicht:
„Kumpel schuldet mir noch 5.000 vom Kinoabend" beurkundet etwas, das längst
passiert ist.

**Kreditwürdigkeit** (0–100, Start 50): pünktlich getilgt +5, freiwillige
Sondertilgung +2, überfällig −10 (einmal je Posten), ausgefallen −15. Was älter
als 90 Tage ist, zählt halb. Sie **begrenzt keine Beträge** — sie ist die Ampel
im Angebot und im Profil-Lookup (*wem* traue ich?), und unter 20 gibt es gar
nichts Neues.

**Grenzen:** mindestens 50 Coins, höchstens 5 offene Posten je Paar und 25 je
Person. Gebremst wird nicht über einen Deckel, sondern über die Zahl der Posten,
die Sperre unter 20, den Verfall nach 60 Tagen und die automatische Tilgung.

> Im Angebot steht seit dem Umbau ausdrücklich, worauf man klickt: ab der
> Zustimmung gehen **20 % jeder Einnahme** automatisch an den Gläubiger.

**Verfall statt Ewigkeit:** ein Posten ohne jede Bewegung ist nach 60 Tagen weg
(der Gläubiger bekommt 7 Tage vorher eine DM). Die Historie bleibt sichtbar.

**Mahnwesen in Stufen** statt Dauer-DM: fällig → freundliche DM; 7 Tage → DM an
beide mit Tilgungsvorschlag; 14 Tage → auf ausdrücklichen Wunsch des Servers
(`schulden_pranger`, Standard **aus**) eine neutrale Notiz im Ansage-Kanal —
niemals mit Beträgen.

**Privatinsolvenz** zahlt 50 % des Vermögens anteilig aus, erlässt den Rest,
setzt den Score auf 10 und sperrt 14 Tage. Ein ehrlicher Neuanfang statt
Konto-Wechsel-Tricks.

Alte Stände werden beim Start **migriert**: je Paar mit offenem Saldo entsteht
ein Posten „Übernahme alte Kreide-Tafel"; die alten Volumen-Zahlen wandern nach
`archiv` — gelöscht wird nichts.

---

## Web-Panel

Standardmäßig auf `0.0.0.0:9123`, **mit Login**. Ist kein `WEBPANEL_PASS`
gesetzt, würfelt Flo beim Start eins und schreibt es einmal ins Log — ein festes
Standardpasswort wäre das Schlimmste von beidem: es sieht nach Schutz aus und ist
keiner, weil es im Quelltext steht.

Ohne Login läuft es mit `WEBPANEL_AUTH=0`. Das ist ausdrücklich vorgesehen, aber
es heißt wörtlich: **jeder, der `9123` erreicht, kann Coins vergeben, Titel
verteilen und den Bot neu starten.** Wer das will, sollte zusätzlich
`WEBPANEL_HOST=127.0.0.1` setzen, damit das Panel nur vom selben Rechner aus
erreichbar ist.

(Bis hierher stand an dieser Stelle „ohne Login" und in der Tabelle unten
`WEBPANEL_AUTH` mit Standard `0`. Der Code sagt seit Längerem `1`. Wer sich auf
die README verlassen hat, hielt sein Panel für offen oder für geschlossen — je
nachdem, was er gerade brauchte.)

Übersicht mit Kennzahlen und Kurs-Chart, Nutzer verwalten (Coins,
XP, Titel, Anteile), Server steuern (Sendepause, Ansage, Features), Update-Knopf.

Ein Klick auf eine Server-Karte öffnet die Einstellungen **dieses** Servers:
Kanäle aus einer echten Auswahlliste, Schalter, Zahlenfelder — und die
Funktions-Schalter, die nur dort gelten.

Ein neuer Eintrag im `guildcfg`-Katalog erscheint dort **ohne Panel-Arbeit** —
die Server-Seite baut sich aus dem Katalog auf.

Unter **Steuerung** dazu:

* **Backup laden** — alle `data/*.json` als ZIP. Reine Leseoperation; dafür
  musste man bisher auf den Server.
* **Panel-Protokoll** — die letzten 200 *schreibenden* Aktionen mit Zeitpunkt,
  Pfad, Daten und Absender-IP (`data/panel_log.json`). Das hängt an einer
  Middleware, nicht an den elf Handlern einzeln — so ist auch der zwölfte Knopf
  erfasst, den jemand später nachrüstet. Das ist **kein** Login-Ersatz (den gibt
  es hier bewusst nicht), sondern Nachvollziehbarkeit: wer später wissen will,
  warum ein Konto 5 Mio mehr hat, findet es dort.

Buchende Knöpfe sind während der Anfrage gesperrt, damit ein Doppelklick nicht
doppelt bucht; `/api/update` lässt nur einen `git pull` gleichzeitig zu.

---

### Zugang

Das Panel verlangt einen **Login** — dort werden Coins vergeben, Titel verteilt
und der Bot neu gestartet. Das soll nur der Besitzer können.

| `.env` | was |
|---|---|
| `WEBPANEL_USER` | Benutzername (Standard `Secoolio`) |
| `WEBPANEL_PASS` | Passwort — **selbst setzen** |
| `WEBPANEL_AUTH=0` | Login abschalten (nur für rein lokale Aufbauten) |
| `WEBPANEL_HOST` | `127.0.0.1` = nur lokal erreichbar |

Ist **kein** `WEBPANEL_PASS` gesetzt, würfelt Flo beim Start eines und schreibt
es **einmal** ins Log:

```
Web-Panel: kein WEBPANEL_PASS gesetzt. Zugang fuer diesen Start -
           Benutzer 'Secoolio', Passwort: kJ8x…
Dauerhaft machen:  WEBPANEL_PASS=kJ8x…  in die .env
```

Ein festes Standardpasswort im Quelltext wäre das Schlimmste von beidem: es
sieht nach Schutz aus und ist keiner. Solange nichts in der `.env` steht, gilt
bei jedem Neustart ein neues — sichtbar mit `k l`.

**Server-Einstellungen bleiben davon unberührt:** jeder Server stellt sich
weiterhin selbst ein (`Flo einstellungen` in Discord, für alle mit
*Server verwalten*). Das Panel ist nur der Weg des Besitzers dorthin.

Zusätzlich nimmt das Panel bei verändernden Anfragen ausschließlich
`application/json` an. Ein Browser-Formular kann das nicht senden — damit läuft
ein `<form>` auf einer fremden Seite ins Leere, selbst wenn der Login aus ist.

## BotSicht

Der Reiter **BotSicht** ist Discord im Aufbau eines Discord-Clients — Server-
Leiste, Kanalliste, Chat, Mitglieder —, aber gezeigt wird darin **Flos**
Blickwinkel, nicht deiner. Der Unterschied ist der ganze Punkt:

| Was du siehst | Warum es so gezeigt wird |
|---|---|
| Kanäle **ohne** Leserecht stehen gesperrt in der Liste | Sie wegzulassen wäre bequemer und genau falsch — die Frage „warum sagt Flo da nichts?" beantwortet sich nur, wenn man den Kanal sieht |
| Oben je Kanal: **LESEN · SCHREIBEN · REAGIEREN · LÖSCHEN** | Flos echte Rechte in genau diesem Kanal, aus `permissions_for(guild.me)` |
| Die Mitgliederliste ist kurz, mit Hinweis „kennt hier 5 von 47" | Das Mitglieder-Intent ist aus — Flo kennt nur, wer im Chat oder Voice aufgetaucht ist. Das ist der Blickwinkel, kein Fehler |
| Wer gerade in welchem Sprachkanal sitzt, und wo Flo selbst hängt | `voice_states` sieht er wirklich |
| Nachrichten ohne Text mit Hinweis aufs Inhalts-Intent | Ohne `message_content` liest ein Bot fremde Nachrichten gar nicht |

**Eingreifen** geht auch: schreiben (als Flo, mit Antwort-Bezug), auf Nachrichten
reagieren, löschen soweit die Rechte reichen. Das Tipp-Zeichen im echten Kanal
läuft mit, während du schreibst — höchstens alle 8 Sekunden eine Anfrage.

**@everyone, @here und Rollen sind fest zugenagelt.** Ein Massen-Ping aus einem
Tippfehler im Eingabefeld lässt sich nicht zurückholen. Einzelne Leute und die
Person, auf die man antwortet, dürfen sehr wohl gepingt werden.

Der ⚡-Knopf ganz oben in der Server-Leiste öffnet den **Alles-Strom**: jede
Nachricht, die Flo gerade bekommt, über alle Server, in seiner Reihenfolge —
auch die von anderen Bots und seine eigenen. Gefüttert wird das aus `on_message`,
und zwar **vor** dem Bot-Check, sonst zeigte die Ansicht eine gefilterte Wahrheit.

Technisch: ein WebSocket auf `/api/sicht/ws` schiebt die Ereignisse, jede
Verbindung hat eine eigene Warteschlange mit genau einem Schreiber (sonst
überholen sich zwei Sende-Tasks und die Antwort steht vor der Frage). Kommt die
Leitung nicht zustande — Reverse-Proxy o. ä. —, fällt die Oberfläche sichtbar auf
Abfrage alle 2,5 s zurück (`/api/sicht/feed?seit=…`). Der Puffer im Bot fasst
`BOTSICHT_PUFFER` Nachrichten (Standard 400) und liegt **nur im RAM**: was Flo
sieht, ist flüchtig, ein Mitschnitt des Servers auf der Platte soll das nicht
werden.

### Direktnachrichten

Der ✉️-Knopf zeigt jedes private Gespräch, das Flo je geführt hat — lesen und
antworten inklusive. Dabei gibt es **eine harte Grenze von Discord**, die man
kennen muss:

> Discord hat für Bots **keinen** Weg, die eigenen DM-Kanäle aufzulisten.
> `private_channels` bleibt leer, im READY stehen keine privaten Kanäle, es gibt
> schlicht keinen solchen Endpunkt.

Was es sehr wohl gibt: **kennt man die Nutzer-ID, liefert Discord den kompletten
Verlauf** — auch von vor Jahren. Es fehlt nur das Verzeichnis, nicht der Inhalt.

Also führt Flo das Verzeichnis selbst (`data/botsicht_dms.json`), gefüttert aus
jeder DM, die durch `on_message` läuft, und aus `flo dm …`. Für alles, was
**vorher** passiert ist, gibt es drei Wege zurück:

| Knopf | Was er tut | Findet |
|---|---|---|
| **Suchen** | Liest deine Owner-DM durch. `_forward_dm_to_owner` hat jede Fremd-DM mit **Absender-ID** an dich weitergeleitet — dein Postfach ist damit ein Verzeichnis aller, die Flo je geschrieben haben. Dazu die `flo dm <id>`-Befehle in den Serverkanälen. | Fast alles, in Sekunden |
| **Gründlich** | Klopft zusätzlich **jede** Nutzer-ID ab, die Flo kennt (Server-Caches, Wirtschaftsprofile, Namensverlauf): DM-Kanal öffnen, nachsehen, ob dort etwas steht. | Alles — zwei Anfragen pro Person, dauert Minuten |
| **ID eingeben** | Der Notausgang: du kennst die Person, Flo nicht. | Genau dieses Gespräch |

Die Suche läuft nebenher mit Fortschrittsanzeige; der Bot arbeitet normal
weiter, und zwischen den Abfragen liegt eine Pause, damit das Rate-Limit ruhig
bleibt. Der Verlauf selbst kommt immer **frisch von Discord**, nie aus Flos
Gedächtnis — er reicht deshalb so weit zurück wie das Gespräch, über Neustarts
und Updates hinweg.

> ⚠️ Das Panel läuft standardmäßig ohne Login. Mit BotSicht heißt „wer den Port
> erreicht" jetzt auch: kann den kompletten Chat **und alle Direktnachrichten**
> mitlesen und im Namen des Bots schreiben. Der Port gehört ins eigene Netz —
> sonst `WEBPANEL_AUTH=1`.

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
`store.JsonStore`, und jede Feature-Klasse erbt von `basis.FeatureBasis` — nur
so stimmt die Ansprache je Server. Ein Test schlägt an, wenn ein Modul sich
`_bot_name` wieder selbst in eine Variable legt.

**Grenzen, dokumentiert statt geraten:** ab etwa 2.500 Servern verlangt Discord
Sharding — das ist ein Wechsel auf `discord.AutoShardedClient` plus ein Blick
über die Loops (die laufen alle schon über `self.guilds` und bleiben gültig).
Das Members-Intent bleibt aus; deshalb gibt es *keine* Begrüßung neuer
Mitglieder — `on_member_join` feuert ohne dieses Intent schlicht nie, und ein
Schalter, der nichts tut, wäre schlimmer als keiner. Alle Namens- und
Avatar-Wege laufen bereits über REST-Fallbacks.

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
| `music.py` | Voice, Warteschlange, YouTube/Spotify/SoundCloud |
| `render.py` | alle Bilder (PIL) |
| `webpanel.py` / `webpanel.html` | Web-Panel |
| `schulden.py` | Schuldbuch: Posten, Tilgung, Kreditwürdigkeit |
| `basis.py` | `FeatureBasis` — die Ansprache, an EINER Stelle |
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
| `WEBPANEL_AUTH` | `1` | Login fürs Panel (`0` = ohne, siehe oben) |
| `WEBPANEL_HOST` / `_PORT` | `0.0.0.0` / `9123` | Panel-Adresse |
| `BOTSICHT_PUFFER` | `400` | wie viele gesehene Nachrichten der Live-Strom vorhält |
| `BOTSICHT_DM_MAX` | `500` | wie viele DM-Bekanntschaften Flo sich merkt |
| `GAMES_EVENT_REWARD_MIN` / `_MAX` | `5000` / `25000` | Preis fürs Schnell-Event (je Runde gewürfelt) |
| `GAMES_EVENT_INTERVAL` / `GAMES_EVENT_CHANCE` | `300` / `0.15` | Takt und Chance je Tick |
| `GAMES_DAILY_MAX` | `50000` | Netto-Gewinn aus allen Spielen pro Tag und Person |
| `ARBEIT_GOLD_CHANCE` | `0.08` | Chance auf eine goldene (doppelt zahlende) Schicht |
| `WORDLE_SPASS_MAX` | `15000` | Höchstlohn je Spaß-Wordle |
| `WORDLE_VERZUG_MIN` / `_MAX` | `300` / `2700` | Zufallsfenster, bis das Wort des Tages fällt |
| `WORDLE_CHANNEL_ID` | — | Standard-Kanal fürs Wort des Tages (je Server überschreibbar) |
| `ARBEIT_COOLDOWN` | `900` | Sekunden zwischen zwei Schichten |
| `ARBEIT_TAGESDECKEL` | `250000` | wie viel Arbeit am Tag höchstens einbringt |
| `WORDLE_PRO_BUCHSTABE` | `10000` | Grundtopf je Buchstabe im Wort des Tages |
| `FLOAKTIE_IDLE_30MIN` | `0.11` | Verfall je halber Stunde |
| `FLOAKTIE_CEIL` | `2` | wie weit über den Zielkurs |
| `FLOAKTIE_GRUND_FAKTOR` | `4` | wie stark der Grundwert trägt |
| `FLOAKTIE_ATEM_MAX` | `0.06` | wie stark der Kurs höchstens atmet |
| `FLOAKTIE_ATEM_RUECK` | `0.15` | Zug zurück zum Niveau je Minute |
| `AUTODELETE_SWEEP_MAX` | `300` | Nachrichten je Aufräum-Runde |

Alle weiteren stehen als `os.getenv(...)` bei den Konstanten im jeweiligen Modul,
jeweils mit Kommentar, warum der Wert so gewählt ist.

---

## Testen und Nachzählen

Zwei Werkzeuge, die zusammen das Sicherheitsnetz bilden. Das eine fragt *läuft
der Code noch?*, das andere *ist noch alles da?* — beides braucht man, denn ein
grüner Testlauf beweist nur die getesteten Wege.

```
python lauf.py                    alle Tests, alle Fehler auf einmal
python lauf.py --misch            zufällige Reihenfolge (deckt Abhängigkeiten auf)
python lauf.py --nur musik        nur passende Testnamen
```

Die Testsuite liegt in 15 Themendateien (`test_musik.py`, `test_casino.py`, …);
die gemeinsamen Attrappen und der umgebogene Datenordner stehen **einmal** in
`testhilfe.py`. Keine Testdatei fasst `DATA_DIR` selbst an — sonst zeigten die
Speicher hinterher auf verschiedene Ordner.

Dazu drei Werkzeuge, die zusammen das Netz bilden. Sie beantworten drei
verschiedene Fragen, und keins ersetzt ein anderes:

| Werkzeug | Frage |
|---|---|
| `lauf.py` | Läuft der Code noch? |
| `werkzeug/inventar.py` | Ist noch **alles da**? |
| `werkzeug/abdruck.py` | Sieht es im Discord noch **genauso aus**? |
| `werkzeug/panelprobe.py` | Lässt sich das **Panel** noch bedienen? |
| `werkzeug/tempo.py` | Ist es **schneller** geworden — oder heimlich langsamer? |

```
python werkzeug/abdruck.py --vergleiche      Antwortform jedes Befehls
python werkzeug/panelprobe.py                Panel im echten Chromium anklicken
python werkzeug/tempo.py --vergleiche        heiße Pfade messen
```

`werkzeug/abdruck.py` nimmt die **Form** jeder Antwort auf — Typ, Überschrift,
Feldnamen, Knopfbeschriftungen, Textgerüst. Was sich nicht reproduzieren lässt
(Würfel, Uhrzeit), misst es selbst und sortiert es aus: 472 von 480 Befehlen
sind stabil, die 8 wackeligen stehen offen dabei.

`werkzeug/panelprobe.py` fährt den echten Panel-Server hoch, lädt die echte
Oberfläche in Chromium und klickt sich durch. Das Inventar sieht, ob es den
*Endpunkt* noch gibt — nicht, ob der *Knopf* ihn noch trifft.

`werkzeug/inventar.py` nimmt den Funktionsumfang **aus dem Code** auf — es
schreibt nichts ab, sondern liest den Quelltext (AST) und fragt zusätzlich die
laufenden Module: *reagierst du auf dieses Wort?* Gefragt werden alle Module,
nicht nur das erwartete; so kommen Kollisionen ans Licht.

```
python werkzeug/inventar.py --schreibe      Grundstand aufnehmen
python werkzeug/inventar.py --vergleiche    prüfen, ob noch alles da ist
python werkzeug/inventar.py --zeige         Stand und Kollisionen ansehen
python werkzeug/inventar.py --panel         vergessene und tote Panel-Knöpfe
python werkzeug/inventar.py --cmdnorm       messen, was die Tippfehler-Toleranz verbiegt
```

Rückgabecodes: `0` alles da · `1` nur angekündigte Verluste · `2` **echter
Verlust** · `3` das Werkzeug selbst ist kaputt.

Verglichen wird nach dem Grundsatz **was verlorengehen kann, ist ein Schlüssel;
was sich ändern darf, ist ein Wert**. Die Schlüssel sind bewusst besitzerlos:
`wordle` ist ein Befehl, egal welches Modul ihn beantwortet. Ein Umzug ist eine
Notiz, nur das Verschwinden ist ein Fehler.

Ein absichtlicher Wegfall gehört mit Grund und Datum in
`inventar/erwartet.json`. Der Eintrag gilt für **einen** `--schreibe`-Lauf und
ist danach verbraucht; ein Eintrag, der im Vergleich gar nicht auftaucht, ist
selbst ein Fehler; und ganze Kategorien lassen sich nicht abnicken.

Heutiger Stand: 536 Befehle, 39 Panel-Routen, 249 Katalogeinträge, 16 Loops,
426 Modul-Funktionen, 35 Frontend-Aufrufe.

## Weitere Dokumente
| `AUDIT.md` | Vollständige Durchsicht der Codebase: was gefunden, was behoben, was widerlegt |

| Datei | Inhalt |
|---|---|
| `TESTBERICHT.md` | Vollständiger Testdurchlauf mit allen Funden und deren Stand |
| `IDEEN.md` | Ausbau-Ideen, durchnummeriert, mit Aufwand und betroffenen Dateien |
