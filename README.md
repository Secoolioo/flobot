# Flo

Deutschsprachiger Discord-Bot: Level & Coins, Aktie, Casino, Spiele, Musik,
Moderation — dazu ein Web-Panel zum Verwalten.

Läuft als systemd-Dienst (`flobot.service`) **oder in Docker** — beides mit
denselben Daten, umschalten mit `k d` / `k s`. Konfiguration über `.env`,
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
| `clip_channel` | wohin die Voice-Clips gehen (ab Werk der Clip-Kanal des Hauptservers) |
| `clip_aufnahme` | ob Flo hier für `flo clip` mithören darf (ab Werk nur auf dem Hauptserver) |
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

Was beim Start gar nicht hochkam (kein ffmpeg, keine KI, ein Teil, der noch ein
Gerüst ist), steht dort als ⚪ *nicht geladen* — wie im Panel.

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
k n                           # git pull + neue Pakete (systemd) bzw. Image neu bauen (Docker)
```

Oder ohne Terminal: im Web-Panel unter **Steuerung** auf **Update** — der Bot
zieht selbst `git pull --ff-only`, installiert neue Pakete, wenn sich
`requirements.txt` geändert hat, und startet neu, wenn es wirklich neue Commits
gab. In Docker nennt der Knopf stattdessen `k n` (der Code steckt dort im Image).

> **Einmalig nach dem Update vom September 2026:** der alte Update-Knopf kannte
> noch keine neuen Pakete. Also einmal von Hand:
> `cd /opt/flobot && venv/bin/pip install -r requirements.txt && sudo systemctl restart flobot`.
> Ohne das bricht Flo beim Start mit einem klaren Satz ab (discord.py zu alt).

Zurück auf einen älteren Stand: `git log --oneline` und `git checkout <hash>`,
danach `k r`.

### Docker statt systemd

Flo läuft auch als Container — **zusätzlich** zum systemd-Dienst, nicht statt.
Beide benutzen denselben Datenordner (`/opt/flobot/data`) und dieselbe `.env`,
deshalb kann man jederzeit hin und her:

```bash
k d        # systemd-Dienst stoppen, Container bauen und starten
k s        # Container stoppen, systemd-Dienst wieder an
```

Von Hand geht es genauso:

```bash
sudo systemctl disable --now flobot
docker compose -f docker/compose.yaml up -d --build
```

Was dabei zu wissen ist:

- **Nie zwei Flos gleichzeitig.** Eine Sperre im Datenordner
  (`data/.flobot.lock`) sorgt dafür: ein zweiter Start (egal ob Dienst oder
  Container) schreibt „Flo läuft schon …“ ins Log, wartet eine Minute und
  beendet sich — ohne sich bei Discord anzumelden. Zwei Flos hießen doppelte
  Antworten und Daten, die sich gegenseitig überschreiben.
- **Dieselben Pfade.** Im Container liegt Flo ebenfalls unter `/opt/flobot`;
  Pfade in der `.env` (z. B. `YTDLP_COOKIES=/opt/flobot/data/cookies.txt`)
  stimmen in beiden. YouTube-Cookies gehören nach `data/` (`k y datei …` legt
  sie dort ab) — eine `cookies.txt` direkt neben `bot.py` sieht der Container
  nicht. Browser-Cookies (`k y browser …`) gehen im Container nicht.
- **Host-Netz.** Der Container benutzt das Netz des Servers: das Web-Panel
  bindet wie gewohnt an `WEBPANEL_HOST`, Dienste auf `127.0.0.1` (Proxy,
  PO-Token-Server, Ollama) bleiben erreichbar, und die Firewall des Servers
  gilt weiter.
- **Sauber beenden.** `docker stop`/`systemctl stop` sichern jetzt vorher alles
  (vorher gingen bis zu 60 s Wortzähler, Schulden und Gedächtnis verloren).
- **`k` weiß Bescheid.** Läuft der Container, zeigen `k l`, `k m`, `k r` & Co.
  automatisch dessen Log und fragen die Ärzte im Container.
- **Gesund?** `docker ps` zeigt `healthy`, solange Flo wirklich mit Discord
  verbunden ist.
- Nach Änderungen an der `.env`: `k r` (die Werte gelten ab dem Start).

> Seit diesem Update wird die `.env` **vor** allen Modulen geladen. Werte, die
> vorher still ignoriert wurden, wirken jetzt — vor allem `GUILD_ID`: auf dem
> Hauptserver sind Server-Icon-Automatik und $FLO-Aktie damit wieder von Haus
> aus an, so wie `Flo einstellungen` es beschreibt. Zeigt ein `DATA_DIR` in der
> `.env` auf einen leeren Ordner, startet Flo nicht (statt mit leeren Konten).

### Die Uhr des Servers

Auf der VM läuft die Hardware-Uhr auf Ortszeit, das System hält sie aber für
UTC (`timedatectl`: „RTC in local TZ: no“). Folge: nach jedem Booten steht die
Uhr zwei Stunden vor, ~15 s später stellt NTP sie zurück — und `systemctl
status flobot` zeigt „active since“ zwei Stunden in der Zukunft. Was Flo in
diesen Sekunden stempelt, läge ebenfalls in der Zukunft. Einmalig auf dem
Server:

```bash
sudo timedatectl set-local-rtc 1                   # Hardware-Uhr läuft auf Ortszeit
sudo systemctl enable systemd-time-wait-sync       # Flo startet erst mit gestellter Uhr
```

`flobot.service` wartet dafür auf `time-sync.target`. Ein Update (`k n`, der
Update-Knopf, `git pull`) bringt die Dienstdatei aber **nicht** an ihren Platz
— nach diesem Update also einmal:

```bash
sudo cp /opt/flobot/flobot.service /etc/systemd/system/ && sudo systemctl daemon-reload
```

`k alles` prüft die Uhr mit und meldet, wenn etwas davon fehlt: Hardware-Uhr,
NTP, ob der eingerichtete Dienst wirklich auf `time-sync.target` wartet und ob
`systemd-time-wait-sync` an ist. Flo selbst nimmt Zeitstempel, die Stunden in
der Zukunft liegen, nicht mehr ernst: Raub- und Schicht-Cooldown laufen dann
einmal normal lang, und der Aktien-Analyst fragt sofort neu.

Und falls die Uhr doch springt, während Flo schon läuft (`uhr.py`):

* **Kein Takt schläft zwei Stunden.** discord.py plant seine Loops nach der
  Wanduhr — nach dem Sprung zurück lag die nächste Runde von Giveaways,
  Voice-Wächter, Status und Aktie zwei Stunden in der Zukunft. `bot.py` flickt
  das beim Start (`uhr.zeitsprungfest_machen`): relative Loops planen ab
  *jetzt*. Ändert eine neue discord.py-Version das Innenleben, steht eine
  Warnung im Log, und es bleibt beim alten Verhalten — kein Absturz.
* **Nichts passiert zweimal.** Wer nach der Uhr plant (Lotto-Ziehung, Shop,
  Aktien-Chart, alles aus dem Server-Leben), merkt sich, was erledigt ist, als
  Schlüssel (`2026-10-21`, `2026-10`, `2026-W43`), der nur **vorwärts** zählt
  (`uhr.spaeter_als`, nie `!=`). Vorher wurde bei einem Sprung über den
  Monatswechsel derselbe Lotto-Monat zwei- bis dreimal ausgelost. Ein
  gespeicherter Schlüssel *weit* in der Zukunft (kaputte Datei, größerer
  Sprung) hält dagegen nichts an: mehr als einen Schritt voraus gilt er bei
  stabiler Uhr als kaputt (`uhr.glaubhaft`, eine Warnung im Log).
* **Erst planen, wenn die Uhr stimmt.** `uhr.stabil()` ist in den ersten fünf
  Minuten nach dem Start (`UHR_START_GNADE`) und fünf Minuten nach einem Sprung
  falsch; so lange wartet, wer nach der Uhr plant, und holt danach nach. Die
  Lotto-Ziehung nach einem Neustart über den Monatswechsel kommt so ein paar
  Minuten nach dem Start — nicht erst im nächsten 6-Stunden-Takt. Auf die Uhr
  schaut der Status-Takt alle 10 s; ein Sprung steht also gleich im Log.

Die Loops mit fester Uhrzeit (2-Uhr-Shop, 18-Uhr-Erinnerung) flickt `uhr.py`
nicht. Wachen sie nach einem Sprung zu früh auf, legt discord.py sie bis zur
Uhrzeit wieder hin. Startet Flo aber in den zwei Stunden davor, solange die
Uhr noch vorgeht (0–2 Uhr bzw. 16–18 Uhr), plant er die erste Runde für
morgen: an diesem Tag fallen die 2-Uhr-Runde (Shop **und** Vermögenssteuer)
bzw. die Erinnerung aus. Den Shop würfelt der Start ohnehin neu; die Steuer
fehlt dann einen Tag. Das war schon immer so.

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
python3 lauf.py                # alle Tests, alle Fehler auf einmal
python3 lauf.py --misch        # zufällige Reihenfolge (deckt Abhängigkeiten auf)
python3 lauf.py --nur musik    # nur ein Thema
python3 test_musik.py          # eine einzelne Themendatei
python3 bot.py --check         # prüft Login, Rechte, Bilder (meldet sich an!)
python3 werkzeug/kiprobe.py    # Flo gegen einen zickigen KI-Anbieter laufen lassen
docker compose -f docker/compose.yaml exec flobot python lauf.py   # Tests im Container
```

Kein pytest nötig — `lauf.py` ist der Runner, und jede Themendatei läuft auch
allein. Alle laufen in einem Wegwerf-Datenordner (`DATA_DIR` wird in
`testhilfe.py` umgebogen, genau einmal für alle) und fassen `data/` **nicht**
an — ein Testlauf auf dem Server ist also ungefährlich.

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
k d / k s        zu Docker / zurueck zu systemd
```

Die häufigsten Gründe, warum Flo „plötzlich nicht antwortet“, fängt er seit
September 2026 selbst ab — nachstellbar mit `python3 werkzeug/kiprobe.py`:
leere Antwort, weil das Denk-Modell sein Budget verdacht hat (Flo fasst einmal
mit mehr Luft nach), kaputter Werkzeug-Aufruf (nochmal, dann ohne Werkzeug),
429 mit `Retry-After`, ein Tageslimit (dann wird bis zum Reset gar nicht erst
gefragt), ein hängender Anbieter (nach 30 s sagt Flo derb, dass die KI pennt),
eine gelöschte Frage und `@Flo` als Rolle erwähnt. In `k l` steht jeder dieser
Fälle mit `KI-Fehler:` davor. Gedächtnis und Aktien-Analyst laufen auf einem
eigenen Modell (`LLM_HINTERGRUND_MODEL`, auf Groq automatisch `gpt-oss-20b`) —
mit eigenem Kontingent, damit sie dem Chat nichts wegfressen.

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
| `LLM_MAX_TOKENS` | Antwortlänge, Standard **240** — der Deckel gegen Textwände. Höher setzen, wenn Flo öfter „fällt mir nichts ein" sagt |
| `LLM_MAX_TOKENS_BILD` | dasselbe für Bild-Kommentare (Standard 160) |
| `LLM_REASONING_EFFORT` | `low`/`medium`/`high` — nur für Denk-Modelle wie `gpt-oss`. Antwortet Flo öfter „fällt mir nichts ein", steht das Budget im Denken statt im Reden. **Bei `gpt-oss` setzt Flo von sich aus `low`**, weil die Modelle sich beim Nachdenken selbst zahm reden |
| `BOT_PERSONA` | ersetzt die komplette Persönlichkeit. **Achtung:** ist die gesetzt, wirkt keine Änderung an der Standard-Persona mehr |
| `BOT_POLITIK` | `aus` nimmt Flo die politische Haltung |

### Wenn Flo zahm wird

Passiert das, ist es fast nie „das Modell ist halt so". Der Reihe nach:

1. **Der Titel.** Die Tonfall-Texte in `titles.py` sind eine Rampe, und sie
   zeigt **nach unten**: kein Rang = am übelsten, göttlich = am angenehmsten.
   So war es von Anfang an gedacht. Zweimal wurde sie versehentlich umgedreht —
   einmal, weil ganz oben wörtlich „Kein Roasten, keine fiesen Sprüche" stand
   und die Stammgäste dadurch einen lieben Flo bekamen. Falsch war aber nicht
   die *Richtung*, sondern nur, dass die Rampe oben bis auf null ging. Heute
   gilt beides: die Seltenheit ändert **wie** geroastet wird, nie **ob**, und
   auf jeder Stufe endet es mit einem Seitenhieb. Neben jedem Text steht dafür
   eine Zahl (`haerte`), damit die Richtung nicht an der Deutung von Prosa
   hängt — `test_die_tonfall_rampe_zeigt_nach_unten` prüft sie.
2. **`BOT_PERSONA` in der `.env`.** Steht die dort, ersetzt sie alles.
3. **Die Länge.** Flo antwortet absichtlich in *einem* Satz — das steht
   zweimal im Prompt (vorne und als letztes Wort) und wird von `LLM_MAX_TOKENS`
   gedeckelt. Wer mehr will, setzt die Variable hoch; der Prompt bleibt dann
   trotzdem bei „ein Satz", also lieber auch dort eine Zeile ändern.
4. **Das Modell.** `openai/gpt-oss-*` prüft sich vor jeder Antwort selbst und
   verweigert deutlich öfter. Alternative: `LLM_MODEL=qwen/qwen3.6-27b`. Erst
   `bash k` laufen lassen — dort steht die echte Modell-Liste des Anbieters.
5. **Verweigert das Modell einen Roast**, nimmt Flo einen Fertig-Spruch — das
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

### Flos Launen

```
flo laune        wie Flo drauf ist – seit wann, warum, wie lange noch
```

Flo hat einen Tag. Einmal täglich würfelt er seine **Tageslaune** (30 % gut
gelaunt, 50 % normal genervt, 20 % mit dem falschen Fuß aufgestanden — der
hält aber nur bis Mittag), und was auf dem Server passiert, schiebt sie herum:

| Laune | Auslöser | hält |
|---|---|---|
| 😏 gut gelaunt | Tageslaune | den Tag |
| 🙄 normal genervt | Tageslaune (der Normalfall) | – |
| 😤 gereizt | ≥ 15 Ansprachen in 5 min · eine Person ≥ 6 in einer Minute · eine Ansprache mit Beleidigung · Tageslaune (falscher Fuß) | 45 min, weiterer Spam verlängert · der falsche Fuß 7–12 Uhr |
| 🤪 überdreht | Casino-Gewinn ≥ max(10 × Einsatz, 50.000) netto · Lotto-Jackpot · $FLO +15 % in 24 h | 60 min |
| 😒 beleidigt | 4 h schreibt keiner (gezählt nur 9–23 Uhr) · oder alle quatschen (≥ 20 Nachrichten), aber 8 h spricht ihn keiner an | bis ihn jemand anspricht (höchstens 12 h) |
| 🥱 müde | 1–7 Uhr | bis 7 Uhr |

Vorrang: ein Ereignis vor der Nacht vor der Tageslaune; überdreht schlägt
gereizt, gereizt schlägt beleidigt. Wer Flo aus dem Beleidigtsein holt — auch
mit `flo laune` —, bekommt es aufs Brot geschmiert („Bis eben war ich übrigens
beleidigt … Jetzt bist ja du da. Toll."). Und die KI-Antwort auf genau diese
Ansprache lässt es auch noch raushängen: im Prompt klingt „beleidigt — und
jetzt redet endlich mal einer mit dir" zwei Minuten nach (vor Nacht und
Tageslaune, nie vor einem neuen Ereignis). Ohne das wäre die Zeile nie bei
einer Antwort angekommen: bot.py zählt die Ansprache, bevor die KI antwortet.

**Was die Laune ändert — und was nicht.** Sie färbt nur den **Ton**: eine Zeile
im System-Prompt („kürzer angebunden, null Geduld"), hinter der Grenze und vor
dem Schlusswort, ohne Namen — und sie endet immer mit dem Satz, dass Grenze und
Notlagen-Regel trotzdem gelten. Flo bleibt in jeder Laune derselbe
passiv-aggressive Flo; gut gelaunt heißt fast gnädig, nicht nett. Bei einer
Notlage (in der Frage oder im Verlauf) kommt die Zeile gar nicht erst mit, und
keine Notlage zählt für eine Laune — weder als Spam noch als „hier ist was los".

**Status.** Der Bot-Status zeigt die Laune des Hauptservers im Wechsel mit den
Weisheiten („😤 Gereizt. Wer mich pingt, kriegt eine gedrückt."), eine neue
Laune gleich im nächsten Status-Takt. Normal genervt lässt die Weisheiten in
Ruhe. Jeder Wechsel steht genau einmal im Log (INFO).

**Uhr und Neustart.** Tageslaune und Nacht nur bei verlässlicher Uhr
(`uhr.stabil()`), der Tag läuft nur vorwärts — nach dem Sprung beim Booten wird
nicht zweimal gewürfelt. Das Ende eines Ereignisses steht als Zeitstempel in der
Datei; liegt es nach dem Sprung zu weit vorn, wird es auf die normale Dauer
gekappt. Die Zähler (Spam, Funkstille, wer wo zuletzt geschrieben hat) leben nur
im Speicher — nach einem Neustart fängt die Zählung neu an, und ein Jackpot von
jemandem, den Flo seit dem Start noch nicht gesehen hat, bleibt ohne Folgen
(gezählt wird auf dem Server, auf dem die Person zuletzt geschrieben hat).

Je Server abschaltbar (`flo features` / Panel, Schalter `launen`): dann weder
Ton noch Status. Daten: `data/launen.json` — Tageslaune, laufendes Ereignis und
höchstens ein Anzeigename als Schuldiger. Die Haken stecken in `casino.record`
und `lotto._draw` (je eine Zeile).

### Er hält nach

```
flo nachhaken          worauf Flo bei dir gerade wartet
flo nachhaken aus      nie wieder nachfragen – räumt alles Offene weg
flo nachhaken an       wieder einschalten
```

Wer „morgen schreib ich die Mathe-Klausur" sagt, wird hinterher gefragt —
einmal, passiv-aggressiv, als hätte Flo tagelang auf den Bericht gewartet:
„Na Anna, wie lief die Mathe-Klausur? Oder reden wir nicht drüber, weil's
wieder nix war?" Standardmäßig **an**; wer nicht will, schaltet es für sich ab
(je Server).

1. **Vorfilter** (jede Nachricht, synchron, ein paar Mikrosekunden): ein
   Zeitwort und ein Ich (ich, hab, muss, bin, wir …), 12–300 Zeichen, keine
   Frage, kein Bot, nur auf dem Server. Was durchkommt, ist ein **Kandidat** —
   nur im Speicher, höchstens 30 je Server, nach 6 Stunden weg. Ist auf dem
   Server der KI-Chat aus (Schalter `ki`), sammelt er keine Kandidaten — dann
   geht auch keine Chatzeile an die KI.
2. **Einordnen** (Alltags-Takt): je Server höchstens alle 10 Minuten **ein**
   KI-Aufruf mit bis zu 10 Zeilen, ohne Namen, als Hintergrund-Aufruf (nach
   einem 429 wartet er) mit der Grenze im System-Prompt. Die KI sagt je Zeile:
   echte Ankündigung? Thema („die Mathe-Klausur")? Heikel? Gespeichert wird
   **nur das Thema** — nie die Nachricht.
3. **Nachfragen:** ist der Termin da und schreibt die Person wieder etwas (nicht
   an Flo), antwortet Flo darauf mit **einem** Satz von der KI (taugt der nicht —
   Verweigerung, zu lang, Hilfe-Text, private Daten —, einer aus dem
   Ersatz-Pool), ohne Ping. Höchstens **einmal je Person und Tag** und **dreimal
   je Server und Tag** (der Server-Deckel zählt ohne Namen und hält auch, wenn
   jemand `flo vergiss mich` sagt); drei Tage nach dem Termin verfällt es. Nicht
   in einem Aufräum-Kanal (dort wäre die Frage nach einer Minute weg — und mit
   ihr die eine Nachfrage des Tages), nicht, wo Flo nicht schreiben darf, und
   **nicht vor Leuten, die die Ankündigung nie lesen konnten**: wer im
   Mod-Kanal, in einem privaten Thread oder einem NSFW-Kanal vom
   Vorstellungsgespräch erzählt, wird nicht im öffentlichen Chat danach gefragt
   (wie bei den Zitaten; gibt es den Kanal nicht mehr, auch nicht). Dann fragt
   die nächste Nachricht in einem passenden Kanal. Geht das Senden
   trotzdem schief, liegt die Sache wieder da. Die Frage landet im Chatverlauf
   der KI — wer darauf antwortet („war gut, ne 2"), redet mit einem Flo, der
   weiß, was er gefragt hat.

| gesagt | Flo fragt ab |
|---|---|
| morgen · übermorgen · in 3 Tagen · am 12.03. | dem Tag, 17 Uhr |
| am Freitag (auch: Freitagabend) | dem nächsten Freitag, 17 Uhr — an einem Freitag gesagt: in einer Woche |
| diesen Freitag | diesem Freitag — auch heute, wenn heute Freitag ist |
| nächsten Freitag · nächste Woche Freitag | Freitag der **nächsten Kalenderwoche** |
| nächste Woche | Mittwoch der nächsten Woche, 17 Uhr |
| am Wochenende | dem kommenden Samstag (samstags: Sonntag; sonntags ab 15 Uhr: das nächste Wochenende) |
| heute abend · heute nacht | dem nächsten Tag, 10 Uhr |
| später · nachher | in drei Stunden |

Steht mehr als ein Zeitwort drin, gilt das **genaueste** — ein Datum vor einem
Tag („am Freitag, den 13.11." ist der 13.11., nicht dieser Freitag), ein Tag vor
„nächste Woche"/„am Wochenende", die vor „später" —, und unter gleich genauen
der **späteste**: lieber einen Tag zu spät fragen als vor dem Ereignis. Ein
Wochentag nah bei „nächste Woche" liegt in der nächsten Woche. Ein Datum ohne Jahr, das
dieses Jahr schon vorbei ist, gilt fürs nächste (am 30.12. ist „am 02.01."
der Januar); mit Jahr in der Vergangenheit gilt nichts. Nie früher als zwei
Stunden nach der Ankündigung, nichts mehr als 60 Tage voraus. „Guten Morgen",
„heute morgen", „bis später", „letzten Freitag", „seit dem 12.03." und
„freitags" sind keine Termine.

**Was er nie tut.** Notlagen (`_NOTLAGE_RE`) weder merken noch nachfragen —
auch nicht, wenn im Kanal gerade jemand am Boden ist. Und wer selbst ein
Notsignal schreibt — egal in welchem Kanal, egal ob Nachhaken dort an ist —,
dessen offene Sachen sind sofort weg (auf jedem Server), und **72 Stunden**
lang merkt Flo sich nichts von ihm und fragt ihn nichts. Nicht nur „im Kanal
steht gerade was": der Kanalverlauf reicht 20 Minuten, ein „na, wie lief die
Prüfung?" am nächsten Tag wäre genauso falsch. Die Sperre lebt nur im Speicher;
auf der Platte steht nirgends, wer am Boden war. Heikles (Krankheit, Arzt,
OP, Therapie, Tod, Beerdigung, Trennung, Schwangerschaft, Polizei, Gericht,
Schulden, Jobverlust, Sex, Drogen, „mir geht's schlecht") lässt schon der
Vorfilter liegen; die KI sortiert nach (im Zweifel heikel — fehlt ihr Urteil,
zählt es als heikel), und ihr Thema wird noch einmal geprüft (private Daten wie
im Gedächtnis, Hetze). Nicht in der Sendepause, nicht bei Ansprache, nicht kurz
nach dem Start (die Uhr muss stimmen — sonst weiß er nicht, welcher Tag
„morgen" ist).

**Gedächtnis.** `flo gedaechtnis` zeigt unter „Worauf ich noch warte", was
offen ist; `flo vergiss mich` zählt es mit und löscht es. Kandidaten (noch nicht
eingeordnete Zeilen, nur im Speicher) gehen mit, zählen aber nicht — wie der
Puffer des Gedächtnisses; auch eine Zeile, die gerade bei der KI liegt, wird
danach nicht mehr gemerkt. Die
Abmeldung bleibt dabei stehen — sie ist ein Wunsch, kein Wissen über dich.
Löschen geht **immer**, auch wenn das Gedächtnis auf dem Server abgeschaltet
ist: weiß Flo von früher noch Fakten über dich, kommt bei `flo vergiss mich` die
übliche Rückfrage mit Knopf, und der Klick löscht Fakten **und** Offenes; ohne
Fakten räumt er das Offene sofort weg (ohne Rückfrage, es ist ja nur das).
`flo nachhaken aus` wirkt auch, wenn Nachhaken dort gerade aus ist — statt dass
die KI den Satz bekommt und alles liegen bleibt.

Braucht die KI (ohne sie lädt das Modul nicht). Schalter `nachhaken`. Daten:
`data/nachhaken.json` — je Server die offenen Sachen (Person, Anzeigename, Thema,
Zeitwort, Kanal, fällig ab, verfällt), wer heute schon gefragt wurde, wie oft
der Server heute schon dran war (ohne Namen), wer abgemeldet ist.

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

**Das Panel:** Cover, Titel, Fortschritt mit „endet in …" (zählt Discord selbst
live herunter), die nächsten drei Songs, Knöpfe und Tempo-Menü. Die Knöpfe
**funktionieren auch nach einem Neustart** von Flo. Pause, Skip, Stop, Loop,
Radio und Tempo darf nur, wer mit im Sprachkanal sitzt (Mods immer); Queue und
Lyrics jeder. Skip ab drei Zuhörern per Abstimmung (siehe unten). Den Titel
schreibt Flo als Status an den Sprachkanal, wenn er das Recht
„Sprachkanal-Status festlegen" hat. Panels von vor dem Update räumt das
Auto-Löschen weg.

**Soundboard:** `Flo soundboard` zeigt die eigenen Dateien aus `sounds/` als
Knöpfe und dazu Menüs mit den **Sounds des Servers** und den **Standard-Sounds von
Discord**. Die laufen über Discords eigenes Soundboard, also **über der Musik**
(braucht das Recht „Soundboard verwenden"); Datei-Sounds warten, bis die Musik
aus ist. Das Brett funktioniert auch nach einem Neustart.

**Schneller Start:** Flo verbindet sich schon, während er sucht (🔎 an der
Nachricht), lädt den nächsten Song vor, während der aktuelle läuft, und merkt
sich, welcher YouTube-Zugang geht. Ohne Musik oder allein im Kanal geht er nach
`MUSIC_IDLE_SEKUNDEN` (Standard 5 min); wirft ihn ein Mod raus, bleibt er draußen.

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

### Playlists

Eigene Wiedergabelisten **je Server** (`playlists.py`, `musikquellen.py`; Hilfe-
Seite „Playlists & Radio", `Flo hilfe playlist`). Sie hängen am Schalter `music`.

```
Flo playlist neu Zocken          Assistent: Songs reinkippen, dann „fertig"
Flo playlist zocken              abspielen (im Voice)       · auch „spiel die playlist zocken"
Flo playlist zocken mix          gemischt                    · auch „playlist mix zocken"
Flo playlists                    alle Playlists des Servers (Name · Songs · von · ×gespielt)
Flo playlist zocken zeig         Karte mit bis zu 25 Songs
Flo playlist zocken dazu         Assistent zum Anhängen      (Ersteller & Mods)
Flo playlist zocken raus 3       Song Nr. 3 rauswerfen       (Ersteller & Mods)
Flo playlist zocken umbenennen in Chill                      (Ersteller & Mods)
Flo playlist zocken löschen      mit Rückfrage               (Ersteller & Mods)
Flo queue speichern als Abend    laufender Song + Warteschlange als Playlist
```

**Der Assistent.** Nach `playlist neu <name>` (oder „neue playlist …") postet Flo
**eine** Statusnachricht, die er live mitschreibt (Zähler, die letzten zehn
Songs, Hinweise – höchstens alle zwei Sekunden bearbeitet). Hineinkippen geht
alles, gern zehn Zeilen auf einmal, Spiegelstriche und Nummern stören nicht:

* Titel als Text (`Daft Punk - One More Time`) – wird erst beim Abspielen gesucht,
* YouTube-Videos und -Playlists, SoundCloud-Tracks und -Sets (auch die Kurzlinks
  `on.soundcloud.com`), direkte Audiodateien,
* Spotify-Songs, -Alben und -Playlists, auch `spotify.link`-Kurzlinks – **ohne
  Spotify-Schlüssel** (siehe unten).

Doppeltes fliegt raus, mehrere Links in einer Zeile werden einzeln genommen.
An jeder Nachricht steht danach ✅ (alles drin), ⚠️ (teilweise, z. B. Dubletten)
oder ❌ (nichts zu holen – warum, steht in der Statusnachricht). Grenzen:
**200 Songs** je Liste, **50 Playlists** je Server, **10** je Person, Namen bis
40 Zeichen und fünf Wörter, höchstens **fünf** ganze Alben/Playlists je
Assistent mit je höchstens 50 Songs.

Fertig mit `fertig`, `stop`, `das wars`, `passt`, `speichern` … oder dem ✅-Knopf
– auch mit „Flo" davor: **`flo stop` schließt den Assistenten und stoppt NICHT
die Musik.** Das geht auch als letzte Zeile eines Pastes (`Song A`, `Song B`,
`flo fertig` in einer Nachricht); dort bricht `abbrechen` genauso ab. Wegwerfen
mit `abbrechen`, `vergiss es`, `doch nicht` oder 🗑. Was an Flo gerichtet ist
(„flo stop die musik", „flo wie spät ist es"), geht ganz normal an Befehle und
KI. Mehrzeilig mit „Flo" vorn („Flo, hier meine Songs:" + Liste) ist ein Paste,
wenn ohne diese erste Zeile mindestens zwei Songs übrig bleiben – die
angesprochene Zeile selbst wird nie ein Song (nur Links darin zählen);
„Flo" + Zeilenumbruch + „wie spät ist es?" geht an die KI. Künstler, die selbst
so heißen („Flo Rida - Low"), sind ein Song. Chat-Zeilen (Fragen, Überschriften
mit Doppelpunkt, lange Sätze, „lol", nur Emojis) bleiben draußen, und ein
Notsignal wird nie als Song geschluckt. Wer nach `fertig` noch etwas
hinterherschickt, bekommt ❌ und den Hinweis auf `playlist <name> dazu`.
Fünf Minuten ohne neue Songs: mit Songs speichert Flo von selbst („⏰ Du
pennst?"), ohne verwirft er. Ist `music` auf dem Server inzwischen aus, macht er
den Assistenten still zu (gespeichert wird trotzdem, aber ohne Ping). Beim
Herunterfahren wird gespeichert, was da ist – geht das nicht (Grenze erreicht),
steht der Grund in der Statusnachricht. Jeder hat höchstens **einen** offenen
Assistenten – ein neuer verwirft den alten. In der Sendepause steht er still,
auch seine Knöpfe.

> ⚠️ Solange ein Assistent offen ist, gewinnt er auch gegen den Blackjack-Tisch:
> `fertig` schließt dann den Assistenten, nicht die Blackjack-Hand.

**Die Karte.** Gespeichert wird die Statusnachricht zur Karte mit vier Knöpfen –
▶ Abspielen · 🔀 Gemischt · ✏️ Songs dazu · 🗑 Löschen –, die **auch nach einem
Neustart** funktionieren. Abspielen braucht einen Sprachkanal; eine **geparkte**
Warteschlange läuft dabei erst weiter, die Playlist stellt sich hinten an. Passt
nicht alles unter den Warteschlangen-Deckel, sagt Flo, wie viele reingingen –
der Rest bleibt in der Liste. Je Server wird immer nur **eine** Playlist auf
einmal eingereiht: wer klickt oder tippt, während der erste Song noch lädt,
bekommt „Geduld" statt die Liste doppelt in der Schlange. Im Aufräum-Kanal
bleibt die Karte zehn Minuten stehen.

**Spotify ohne Schlüssel.** Ohne `SPOTIFY_CLIENT_ID`/`_SECRET` liest Flo Titel,
Künstler, Dauer und ganze Songlisten aus Spotifys öffentlicher Einbettungsseite
(`open.spotify.com/embed/…`), notfalls nur den Titel über oEmbed. Das gilt auch
für `Flo spiel <Spotify-Song/Album>`. Gesucht wird auf YouTube wie mit Schlüssel
mit dem **Haupt**-Künstler und dem Titel; angezeigt werden alle Künstler. Den
Aufbau dieser Seite bestimmt Spotify – ob die Strecke auf dem Server geht, zeigt
`bash k m` (Schritt „Spotify ohne Schlüssel").

Gewollte Änderung an der Musik: `Flo spiel <Spotify-Playlist>` liest die
Einbettung jetzt über dieselbe Stelle wie der Assistent (`musikquellen`,
geteilte HTTP-Session, 15 s wie bisher) und bekommt dabei die Songdauer für
den Best-Match mit; `music._spotify_playlist_via_embed` ist nur noch eine dünne
Hülle mit der alten Rückgabe.

Daten: `data/playlists.json`. Die Befehlsgrammatik steht in `playlists._parse`:
nur was eindeutig ist, wird ein Befehl („flo playlist ist doof", „flo playlist
zocken ist weg" beantwortet die KI, „flo spiel playlist <gibt's nicht>" bleibt
eine YouTube-Suche). `Flo playlist <link>` erklärt, dass ein Link kein Name ist
(abspielen: `Flo spiel <link>`).

### Endlos-Radio

```
Flo radio            Flo radio an · radio aus     Flo endlos
Flo autoplay off     Flo mach mal das radio an    📻 am Panel
```

Läuft die Warteschlange leer, hört Flo nicht auf, sondern legt Ähnliches nach:
den **YouTube-Mix** (`RD<id>`) zum letzten Song, den ein **Mensch** gewünscht
hat — jeder dritte Nachschub geht vom letzten Radio-Song aus, sonst kreiste das
Radio ewig um denselben Mix. Ohne YouTube-ID (SoundCloud, Datei) sucht Flo den
Titel erst flach auf YouTube. Frisch nach einem Neustart, wenn der Player noch
nichts weiß, nimmt er den neuesten Song aus dem Verlauf.

Je Nachschub kommen **drei Songs**, lazy (aufgelöst wird erst beim Abspielen),
mit 📻 statt Namen als „gewünscht von". Rausgefiltert wird: der Ausgangssong,
alles, was das Radio in dieser Sitzung schon hatte (die letzten 200 Video-IDs),
jeder Titel, der gerade lief oder unter den letzten 50 im Verlauf steht (auch als
anderes Upload — „Official Video", „HD", „Lyrics" zählen nicht), Stunden-Loops,
Mixe, Zusammenschnitte und die schweren Varianten (Nightcore, Sped-Up, Cover,
Karaoke …) — außer der Ausgangssong ist selbst so einer.

* **Nacktes Wort schaltet um.** Drehen darf, wer mit im Voice sitzt, und jeder
  Mod. `Flo radio an`, wenn gar nichts läuft: Flo kommt in deinen Kanal, fängt
  sofort an und antwortet auf den Befehl. Kommt gerade nichts durch (YouTube
  oder Discord zickt), bleibt er drin und versucht es selbst noch einmal. Sagen
  zwei Leute gleichzeitig `radio an`, startet trotzdem nur einer. `Flo mach das
  radio aus` stoppt — wie eh und je — die ganze Musik; nur das Radio aus heißt
  `Flo radio aus` (oder 📻 am Panel).
* **Am Panel:** zweite Reihe 🔁 Loop · 📻 Radio (grün, wenn an). Die Kopfzeile
  zeigt „📻 Radio" (bei einem Radio-Song statt des Wünschers; „📻 vom Radio",
  wenn es inzwischen aus ist), das Notweg-Panel ein Feld „Radio". Radio-Songs
  tragen unter „Als Nächstes" und in der Queue ein 📻.
* **Kein Nachschub**, wenn nur noch Bots im Kanal sitzen (dann geht Flo nach der
  Leerlauf-Frist), die Schlange geparkt ist, Sendepause ist, die Musik auf dem
  Server aus ist — oder Discord gerade wackelt: dann holt der Wächter den
  Nachschub nach, sobald die Verbindung wieder steht. Ist dabei auch die
  Voice-Verbindung gerissen, holt er erst die zurück und legt dann nach.
* **Fehlschläge:** je Nachschub zwei Versuche, der zweite vom anderen
  Ausgangssong. Bringen beide nichts Neues, ist das Radio aus, mit Ansage (noch
  mal fragen brächte dasselbe). Ließ sich der Mix gar nicht laden — ein kurzer
  yt-dlp- oder DNS-Aussetzer —, versucht es der Wächter im nächsten Takt noch
  einmal; erst wenn auch das scheitert, ist Schluss.
* **Deckel:** höchstens `MUSIC_RADIO_MAX` (150) Radio-Songs je Sitzung, dann ist
  es aus. Der Leerlauf-Wächter sieht einen leeren Kanal nur, wenn Discords Cache
  ihn kennt — ohne Deckel spielte Flo dort sonst bis zum nächsten Neustart.
* Das Radio gehört zur **Sitzung**: `Flo stop`, ein Rauswurf oder der Leerlauf
  schalten es aus. Jede Änderung geht an die neustartfeste Warteschlange.
* Beim ersten Nachschub einer Sitzung sagt Flo es an. Eigene Wünsche landen
  hinter den (höchstens drei) Radio-Songs, die schon warten.
* In der Hilfe steht das Radio auf der Seite **Playlists & Radio** — die gibt
  es, sobald die Musik läuft, auch ohne Playlists.

### Neustartfeste Warteschlange

Ein Update, ein Absturz, `flo restart` — und hinterher sitzt Flo wieder im
Kanal und spielt weiter: **derselbe Song an derselben Stelle**, die Schlange
dahinter, Pause bleibt Pause, das Radio bleibt an (samt dem, was es schon
gespielt hat). Er meldet sich dabei: „🔁 Bin wieder da. Hab eure Warteschlange
mitgebracht – 5 Songs, ihr Undankbaren." und ein Panel mit „Wieder da – weiter
bei 1:23".

Zurück kommt er aber nur, wenn das jemand will:

* Der Stand ist höchstens `MUSIC_WIEDERHERSTELLEN_MIN` (20) Minuten alt — wer
  länger weg war, dem reißt Flo nicht ungefragt den Kanal auf.
* Im Sprachkanal sitzt noch ein **Mensch** (Bots zählen nicht; im Zweifel nicht).
* Server, Sprachkanal und der Schalter `music` gibt es dort noch.
* Sonst wird der Stand verworfen — je eine Logzeile, warum („keiner in 'Musik'",
  „23 Minuten alt", „Sprachkanal weg" …).

Einzelheiten:

* Der Song wird **frisch aufgelöst** (Stream-Adressen laufen nach Minuten ab und
  werden nie gespeichert), erst dann geht Flo in den Kanal — lädt er gar nicht
  und war er der einzige, bleibt Flo draußen. Sonst geht es mit dem nächsten
  weiter. Ein **Live-Stream** setzt am Live-Rand wieder ein, ein Song, der
  schon so gut wie durch war, wird übersprungen. Loop und Tempo kommen nicht mit
  (sie gehören zu genau einem laufenden Song).
* Während Flo zurückkommt, hält sich der Voice-Wächter raus. Sagt in diesen
  Sekunden jemand `flo stop` (oder `flo hau ab`), startet nichts — auch wenn
  Flo noch in keinem Kanal sitzt, und auch, wenn er schon drin ist und sich
  gerade meldet: dann geht er wieder raus, und es bleibt keine Schlange liegen.
* Spielt jemand selbst etwas, bevor Flo zurück ist — gleich nach dem Start oder
  während er lädt —, stellt sich der alte Stand **hinten an** (soweit
  `musik_max_queue` reicht), Flo bleibt, wo er ist, und sagt es: „Bin wieder
  da – und ihr habt natürlich schon ohne mich angefangen …". Sitzt Flo schon
  im Kanal, ohne dass etwas läuft (`flo komm`), geht es gleich mit dem alten
  Song an seiner Stelle los. Das gilt auch, wenn Flo erst später an diesem
  Server ist (mehrere Server nacheinander, die Uhr nach dem Booten): bis dahin
  bleibt der alte Stand unangetastet. Ein `flo spiel`, das nichts findet, ist
  kein `flo stop` — der Stand kommt trotzdem zurück.
* Lässt Discord ihn nicht in den Kanal, kommt die Schlange **geparkt** zurück
  (mit Hinweis) — `flo weiter` oder das nächste `flo spiel` machen dort weiter.
  Die Hinweise nennen den Namen, auf den Flo **auf diesem Server** hört (eigener
  Präfix), Panel und Ansagen laufen im Kontext des Servers.
* Eine **geparkte** Schlange (der Wächter kam nach einem Netz-Aussetzer nicht
  mehr in den Voice) überlebt den Neustart ebenfalls geparkt: Flo geht dafür in
  keinen Kanal und sagt nichts, `flo weiter` setzt sie im Kanal dessen fort, der
  es sagt — der Song vorne an seiner Stelle.
* In der **Sendepause** kommt die Schlange genauso geparkt und still zurück.
* Ein zurückgeholter Song zählt fürs Musik-Wrapped **nicht noch einmal** — er
  lief ja schon.
* `flo stop`, ein Rauswurf und der Leerlauf löschen den Stand: dann soll Flo
  nach dem Neustart gerade **nicht** zurückkommen.
* Gemerkt wird bei jeder Änderung (Start, Pause, Einreihen, Songwechsel,
  Verschieben, Radio-Schalter, Parken) — gebaut und geschrieben im 15-s-Takt
  des Voice-Wächters, Stelle und Zeitstempel jede Minute (deckt Absturz und
  `kill -9`), beim Beenden einmal vor dem Abmelden. Was danach beim Abmelden
  passiert (discord.py trennt die Voice, der Song „endet"), ändert den Stand
  nicht mehr.
* **Die Uhr:** das Alter misst die Auszeit („so stand es, als Flo noch lief").
  Auf dem Server springt die Uhr kurz nach dem Booten zwei Stunden zurück —
  ein Stand aus den Sekunden davor liegt danach bis zu 2,5 h in der Zukunft
  (gilt), und solange die Uhr nach dem Start noch nicht stabil ist, schaut Flo
  zwei Minuten lang nach, ob ein „zu alter" Stand nach dem Sprung doch frisch ist.
* Einmal je Prozess, fünf Sekunden nach dem Login — ein Reconnect ist kein
  Neustart. `MUSIKZUSTAND_ENABLED=0` schaltet es ab. Daten:
  `data/musikzustand.json`.

### Abstimmung zum Überspringen

Ab **drei Menschen** im Sprachkanal (Bots zählen nicht) braucht Skip eine
Mehrheit: `Menschen // 2 + 1` Stimmen — bei drei also zwei, bei vier drei. Gilt
für den ⏭️-Knopf am Panel und für `Flo skip` gleichermaßen.

* **Direkt** skippt, wer den Song **selbst gewünscht** hat („skippt den eigenen
  Wunsch. Peinlich, aber ehrlich."), jeder **Mod** (Admin, Server oder Kanäle
  verwalten, Leute verschieben — wie an den Knöpfen; „Mod-Hammer") und jeder,
  solange höchstens zwei zuhören. Kann Discords Mitgliederliste nicht gelesen
  werden oder ist sie leer, wird im Zweifel auch direkt geskippt.
* **Abstimmen** darf nur, wer im Voice sitzt — alle anderen bekommen eine Abfuhr.
  Jede Stimme zeigt sich im Panel („⏭️ 2/3 wollen weg"), am Knopf gibt's dazu
  einen Zettel nur für dich („Stimme gezählt. Noch 1."). Zweimal zählt nicht.
  Gezählt werden nur Stimmen von Leuten, die noch im Kanal sitzen.
* **Reicht es**, fliegt der Song raus, und Flo sagt es öffentlich: „Demokratie
  hat gesprochen: … {Wünscher}, dein Geschmack wurde gerade öffentlich
  abgewählt." Ein Klick, der in den Wechsel fällt, wirft nicht gleich den
  nächsten Song mit raus. Geht jemand, sinkt die Schwelle mit: reichen die
  Stimmen danach schon, genügt es, dass ein Wähler noch mal klickt.
* Verpasst Flo Discords 3-Sekunden-Frist für die Antwort auf den Knopf, gilt der
  Skip trotzdem — entschieden ist entschieden.
* Die Stimmen gelten **nur für den laufenden Song** — ein neuer Song fängt bei
  null an, ein Tempo-Wechsel oder Neustart desselben Songs nicht.

### Musik-Wrapped

```
Flo wrapped                    der laufende Monat als Karte (läuft noch)
Flo wrapped letzter monat      der Monat davor
Flo wrapped posten             nur der Chef: das Monats-Wrapped jetzt
```

Flo zählt mit, was läuft — je Server und Monat: welche Songs, wie oft, und
**wer** sie gewünscht hat. Daraus wird eine Karte (Top-5-Songs mit Balken,
Songs/Stunden/Radio, wer am meisten gewünscht hat) plus zwei Sätze, in denen
Flo über euren Geschmack herzieht und den DJ verspottet. `flo wrapped` geht
einmal pro Minute je Server, ohne Ping und ohne Coins.

**Am 1. ab 10 Uhr** postet Flo das Wrapped des Vormonats von selbst — in den
Kanal `wrapped_channel` (`flo einstellung`), sonst in die Ansagen, sonst in den
System-Kanal — **nie in einen Aufräum-Kanal** (dort wäre es samt Ping nach einer
Minute weg; auf dem Hauptserver sind die Ansagen ab Werk so einer). Wer die
meisten Songs gewünscht hat (Gleichstand: wessen Songs länger liefen), ist
**DJ des Monats**: wird als Einziger gepingt, bekommt `dj_belohnung` Coins
(Standard 5.000, höchstens 25.000; `0` = nur die Ehre) — und zwar **einmal im
Monat über alle Server**: Coins gelten überall, wer auf drei Servern DJ ist,
wird dreimal ausgerufen, aber einmal bezahlt (auf den anderen steht, dass er
schon woanders kassiert hat). Sonst druckte sich jeder mit ein paar
Privatservern Geld. Dazu mit `dj_rolle` (auf dem Hauptserver von Haus aus an,
sonst aus) die Rolle „🎧 DJ des Monats". Wer sie hat, merkt
sich Flo erst, wenn Discord sie wirklich vergeben hat — abgeben muss sie im
nächsten Monat genau der, auch wenn dazwischen ein Monat ohne Schalter lag; ein
Monat ohne DJ (unter 10 Songs) nimmt sie nur weg. Die Rolle braucht „Rollen
verwalten" (und Flos Rolle darüber); fehlt das, steht es im Log.

* **Gezählt wird, was startet** — außer einem Song, der in den ersten 30
  Sekunden weggeskippt oder weggestoppt wird (bei kurzen Songs: vor der
  Hälfte): den hat keiner gehört. Sonst skippte sich jeder in ein paar Minuten
  durch eine 200-Song-Playlist zum DJ des Monats (oder machte „spiel", „stop",
  „spiel", „stop"). „Stunden" ist die Länge der Songs, nicht
  die gehörte Zeit.
  Loop-Durchläufe, Tempo- und Wächter-Neustarts sind kein neuer Start; ein Song,
  der nach einem Neustart oder Netz-Aussetzer weiterläuft, zählt nicht doppelt.
* **Radio-Songs** zählen nur als Zahl („+ 14 vom Radio") — gewünscht hat sie
  keiner, also stehen sie weder in den Top-Songs noch beim DJ.
* Derselbe Song ist derselbe, egal mit welchem Link (YouTube-ID ohne `&t=`,
  `&list=`, `?si=`); ohne YouTube-ID die Adresse ohne Anhang, sonst der Titel.
  Je Monat höchstens 500 verschiedene Songs (der seltenste fliegt), 13 Monate
  bleiben stehen.
* **Genau einmal:** unter 10 gewünschten Songs gibt es kein Wrapped (und keinen
  DJ). Gepostet wird nur bei stabiler Uhr, und „erledigt" läuft nur vorwärts —
  springt die Uhr nach dem Booten zurück (vor 10 Uhr oder in den Vormonat),
  kommt nichts doppelt. Wer am 1. offline war, bekommt es **bis zum 7.** nach;
  in der Sendepause wartet es (im selben Fenster).
* **Coins genau einmal:** erst steht „erledigt" samt DJ auf der Platte, dann
  fließen die Coins (wie beim Giveaway). Bricht der Takt mittendrin ab, wird
  trotzdem zu Ende bezahlt; stirbt Flo genau dazwischen, fehlen dem DJ
  schlimmstenfalls die Coins — doppelt gibt's nie, auch nicht, wenn jemand
  „erledigt" von Hand aus der Datei kratzt.
* **Der Post kommt nach:** Post und Rolle sind vom Bezahlen getrennt. Bricht der
  Takt nach dem Bezahlen ab (er hat höchstens drei Minuten — KI, Karte und
  Hochladen kosten), lehnt Discord den Post ab oder gibt es keinen Kanal, holt
  der Takt nach, was fehlt — alle 30 Minuten, höchstens sechsmal und nur bis zum
  7. Der DJ wird dabei gepingt, die Coins stehen dabei, gezahlt wird nichts mehr.
* **Der Spruch** kommt von der KI (Hintergrund-Aufruf mit der festen Grenze, nur
  mit Schalter `ki` und nicht in der Sendepause), sonst aus einem Ersatz-Pool im
  selben Ton. Ein Songtitel oder Name mit Notsignal geht nie an die KI und nie
  in den Spruch; eine Antwort mit Notsignal, Hilfsangebot, privaten Daten oder
  Hetze wird verworfen. Ein DJ mit so einem Namen bleibt DJ — im Spruch heißt er
  nur „DJ". Hat der DJ selbst gerade ein Notsignal geschrieben (wie bei Zitat
  der Woche und Geburtstag): kein Ping, keine KI, kein Spott, nur eine neutrale
  Zeile — Coins und Rolle bekommt er trotzdem.
* `flo wrapped posten` (nur der Besitzer, zum Testen): steht das Wrapped des
  Vormonats noch aus (oder hing es), **ist** das der Monats-Post — mit Coins und
  Rolle, der Takt macht ihn danach nicht noch einmal. Sonst ein
  Wiederholungs-Post ohne Coins und ohne Ping. Ging der Post nicht raus, sagt
  Flo das, statt „ist raus" zu behaupten.
* Nur eindeutige Befehle: „Flo wrapped ist cool" beantwortet die KI.
  `MUSIKSTATS_ENABLED=0` schaltet es ab. Daten: `data/musikstats.json` (je
  Server, bis auf das DJ-Kassenbuch `bezahlt`: über alle Server, wer für welchen
  Monat zuletzt DJ-Coins bekam).

---

## Voice-Clips

```
Flo clip                   Länge (30 s · 1 min · 5 min) und Person per Knopf
Flo clip 1m @Bob           direkt: Länge und nur Bob (30s, 90, 1:30, 2 min, halbe minute …)
Flo clips · clips monat    die Clips mit den meisten 🔥 (Woche, Monat, alle = 5 Wochen)
Flo clip nein · ja         ich will nicht mitgeschnitten werden · wieder rein
Flo clip an · aus          Flo kommt lauschen · Mikro aus, Puffer weg
Flo clip status            Zahlen für Mods (nie Namen)
```

Flo hat die letzten **5 Minuten** eines Calls im Kopf — nur im Arbeitsspeicher,
laufend überschrieben. `flo clip` schneidet daraus eine **MP3** (30 s bis
5 min; eine nackte Zahl bis 5 sind Minuten, darüber Sekunden, unter 10 s wird
aufgerundet) und legt sie in den Clip-Kanal — mit 🔥 zum Abstimmen und 🗑️ zum
Löschen. Es zählt der Moment der Frage: wer im Dialog erst nach 40 Sekunden
klickt, bekommt trotzdem das, was davor lief. Hat Flo noch nicht so lange
zugehört, wird der Clip kürzer (und Flo sagt es).

**Wann Flo mithört.** Nur in einer echten Musik-Sitzung: Musik, Pause, Radio,
`flo komm`, die nach einem Neustart zurückgeholte Warteschlange — oder `flo clip
an` (dann bleibt Flo, solange Menschen im Kanal sind; sitzen nur noch Bots
drin, geht er wie sonst nach `MUSIC_IDLE_SEKUNDEN`). **Nie** bei einem kurzen
Soundboard-, TTS- oder Join-Sound-Besuch. Dazu muss gelten: Funktion `clips`
an, Einstellung `clip_aufnahme` an (ab Werk **nur auf dem Hauptserver**,
anderswo schaltet ein Admin sie ein), keine Sendepause, ffmpeg mit MP3 da, Flo
nicht vom Server taub gestellt, kein „Mikro aus" für diesen Kanal.

**Ohne Ansage keine Aufnahme.** Bevor Flo zuhört, postet er in den Text-Chat
des Voice-Kanals eine Ansage (was, wie lange, wohin, Groq) mit zwei Knöpfen:
**🙅 Ohne mich** und **🔇 Für alle aus**. Erst danach hebt er seine Taubheit
auf — das durchgestrichene Kopfhörer-Symbol ist die ehrliche Anzeige: taub =
hört nicht mit; hört er auf, stellt er sich wieder taub. Darf Flo dort nicht
schreiben, hört er nicht mit (und versucht es 10 Minuten lang nicht nochmal).
Steht in demselben Kanal schon eine Ansage, die jünger als 45 Minuten ist und
die es noch gibt, kommt keine zweite — ein Neuaufbau oder Neustart spammt
nicht. Wer später dazukommt, bekommt keine eigene Nachricht; die Ansage steht
im Chat (in einem Aufräum-Kanal bleibt sie stehen, solange Flo zuhört oder sie
jünger als 45 Minuten ist — danach
räumt sie das Auto-Löschen mit weg, und die nächste Runde kündigt neu an).
Verschwindet sie mitten in der Runde (`flo lösch 50` im Voice-Chat), postet Flo
sofort eine neue (ohne Meldung von Discord findet es der Takt nach spätestens
fünf Minuten) — und darf er das nicht, hört er auf. Höchstens eine neue je zehn
Minuten: löscht sie jemand (oder ein Moderations-Bot) gleich wieder, hört Flo
auf, statt den Chat mit Ansagen zu füllen — und sagt bei `flo clip` auch genau
das (Status-Grund `ansage_weg`), nicht „darf nicht schreiben".

**Raus.** 🙅 oder `flo clip nein` (auch `nein danke`, `bitte ohne mich`, `ich will
nicht` — und vertippt wie `flo cilp nein`; auch als Privatnachricht an Flo, die
dann nicht beim Chef landet): sofort und auf **jedem** Server — auch wo Clips
aus sind (sogar im ganzen Bot), der Knopf auch in der Sendepause. Pakete dieser Person werden
schon vor der DAVE-Entschlüsselung verworfen, was im Puffer lag, ist weg, offene
Dialoge vergessen sie, und nach dem Schnitt wird nochmal geprüft — bevor der Ton
zu Groq geht, unmittelbar vor dem Hochladen und gleich danach (dann kommt der
Post wieder weg). Wer raus ist, kann auch andere nicht clippen. Niemand kann
jemand anderen rausnehmen. Zurück mit `flo clip ja` (das nur richtig geschrieben).

**Für alle aus.** 🔇 oder `flo clip aus` (wer im Call sitzt, und Mods): Puffer
weg, Flo stellt sich wieder taub, laufende Schnitte und offene Dialoge werden
verworfen, und im Voice-Chat steht, wer es war. Das gilt, bis die Runde in
diesem Kanal vorbei ist (Flo ist raus und die Musik will nicht mehr hin) —
höchstens 12 Stunden, auch über einen Neustart. `flo clip an` hebt es auf und
kündigt neu an — frühestens eine Minute nach dem „aus" (sonst wird aus Hin- und
Herschalten Spam im Voice-Chat). Kam Flo nur zum Mithören und läuft nichts,
geht er gleich mit.
Ist Flo schon weg (oder sitzt woanders), verwerfen 🔇 und `flo clip aus` trotzdem,
was aus der Runde noch unterwegs ist — einen offenen Dialog, einen laufenden
Schnitt.

**Wer clippen darf.** Wer im Call sitzt oder im Puffer zu hören ist, und Mods.
Je Person alle 45 Sekunden, je Server 12 in der Stunde (ein Clip, der technisch
scheitert — ffmpeg, Upload abgelehnt —, zählt nicht mit); geschnitten wird
einer nach dem anderen (höchstens drei warten, dann „Eins nach dem anderen").

**Was im Clip ist.** Alle, die geredet haben — außer wer raus ist und wer gerade
ein Notsignal geschrieben hat (der fehlt still, ohne Begründung; fragt er selbst,
ist er drin, aber ohne Spruch). Flos eigene Musik läuft leiser mit. „Nur eine
Person" (`@wer` oder im Menü) heißt: nur deren Spur, ohne Flos Musik; Bots kann
man nicht einzeln clippen, und wer raus oder gerade am Boden ist, auch nicht —
mit derselben knappen Abfuhr für beides, damit niemand daraus ein Notsignal
liest. Kein Clip ohne menschliche Stimme — „nur meine Musik" gibt's auf YouTube.

**Transkript und Spruch.** Gibt es einen Whisper-Zugang (Groq, siehe
Konfiguration), bekommt der Clip ein Transkript („KI, ohne Gewähr") und einen
passiv-aggressiven Satz von Flo (KI im Hintergrund mit derselben Grenze wie im
Chat, Sprecher als A/B/C; ohne KI oder Transkript ein Spruch aus dem
Ersatz-Pool). Steht im Transkript ein Notsignal oder Hetze, **bleibt der Clip
liegen** (das Notsignal merkt sich Flo wie im Chat 72 Stunden, nur im
Speicher); private Daten werden im Transkript geschwärzt, der Ton bleibt.

**Wohin.** In den Kanal `clip_channel` (ab Werk der Clip-Kanal des
Hauptservers), sonst in die Ansagen, sonst in den System-Kanal — nur, wo Flo
schreiben **und Dateien hochladen** darf, **nie** in einen Aufräum-Kanal. Ist der
Voice privat (nicht jeder, der den Clip-Kanal liest, sieht den Voice), bleibt
der Clip im Voice-Chat dieses Kanals — oder es gibt keinen; dort bleibt er auch
stehen, wenn dieser Voice-Chat ein Aufräum-Kanal ist. Höchstens
`min(9,5 MiB, Upload-Grenze − 512 KiB)`; 5 Minuten passen mit 192 kbps hinein.
Kein Post pingt jemanden.

**Was gespeichert wird.** `data/clips.json`: wer raus ist, wann wo die Ansage
steht, „Mikro aus" je Kanal, das Whisper-Kontingent und ein Index der Clips
(Nachricht, Kanal, wer geclippt hat, wer drin ist, wer 🔥 gab) — **kein Ton, kein
Transkript**. Clips fliegen nach 35 Tagen aus dem Index (danach zählen ihre 🔥
nicht mehr; 🗑️ geht weiter — wer drin ist, steht im Post selbst), Ansagen nach 7. Der
Puffer liegt nur im Arbeitsspeicher (höchstens `CLIP_PUFFER_MB` je Server und
160 MiB insgesamt), die MP3 nur als Bytes; die Temp-Dateien des Schnitts
(privater Ordner, 0700) löscht Flo sofort, Reste beim Start und beim
Herunterfahren. Eine Clip-Sitzung kommt nach einem Neustart bewusst **nicht**
von allein zurück — nur, wenn die Musik zurückkommt (und dann ohne neue Ansage,
solange die alte noch steht).

**Dritte.** Für das Transkript bekommt Groq (bzw. `CLIP_WHISPER_URL`) den Ton der
Sprecher (nur die Stellen, an denen sie reden); für den Spruch bekommt der
eingestellte KI-Anbieter (`LLM_BASE_URL`, ab Werk Groq) den Text — mit
Buchstaben statt Namen. Die Ansage nennt Groq nur, wenn beides dort landet,
sonst „einen externen KI-Dienst"; `flo clip nein` deckt beides ab.
`CLIP_TRANSKRIPT=aus` schaltet das Transkript ab (dann gibt es auch keinen
KI-Spruch, nur einen aus dem Ersatz-Pool).

**Nachsehen.** `flo clip status` (Mods und der Chef) zeigt nur Zahlen: Sitzung
und Grund, Verbindung (Modus, DAVE, taub, geweckt), Pakete der letzten 60 s,
Puffer, Schnitt, Whisper heute. Im Journal: `bash k c` — alle fünf Minuten eine
Zeile je laufender Sitzung („… Pakete (… gespeichert, … DAVE-retry …)"), dazu
Warnungen, wenn Pakete ankommen und nichts im Puffer landet oder minutenlang
gar nichts kommt.

> **Rechtlich:** Opt-out mit Ansage ist die Entscheidung des Betreibers. Nach
> § 201 StGB und DSGVO ist das **keine Einwilligung** — wer Gespräche
> aufzeichnet, braucht im Zweifel die Zustimmung aller. Flo macht es so
> durchsichtig wie möglich (Ansage, Taub-Symbol, Opt-out überall, dauerhaft nur,
> was jemand mit `flo clip` rausschneidet), ersetzt aber keine Rechtsberatung.
> Ein Opt-in-Modus wäre der sichere Weg (IDEEN [78]).

Technik: `voiceempfang.py` (RTP, Transport- und DAVE-Entschlüsselung),
`clipschnitt.py` (Schnitt mit ffmpeg), `clipstext.py` (Whisper, Prüfung, Spruch,
Bestenliste), `clips.py` (alles andere). `CLIPS_ENABLED=0` schaltet es ab; ohne
Musik gibt es keine Clips. Nach einem discord.py- oder davey-Update zuerst
`python lauf.py --nur empfang` — der Empfang hängt an Innereien von discord.py
(steht beim Start im Log, falls etwas fehlt), und davey ist deshalb auf `<0.2`
gedeckelt.

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

## Umfragen und Rechtsklick

```
flo umfrage bestes Fast Food                  Flo denkt sich Frage + Antworten aus
flo umfrage Pizza oder Döner? | Pizza | Döner  genau so, wie du es schreibst
flo umfrage 48h ...                           Laufzeit vorne dran (6h, 2t, 1w; max. 32 Tage)
```

Das sind **echte Discord-Umfragen**: Discord zählt, jeder hat eine Stimme, und sie
laufen auch weiter, wenn Flo neu startet. Ist eine vorbei, sagt Flo, was er vom
Ergebnis hält. Über Hetze lässt er nicht abstimmen.

**Rechtsklick** (Apps-Menü) an einer Nachricht: *Flo, sag was dazu* und *Als
Zitat speichern* — an einer Person: *Roasten*. Dieselben Schalter wie im Chat
(KI, Zitate bzw. Chaos, Sendepause), 20 s Abkühlzeit je Person für die beiden
mit KI (ein Zitat speichern kostet keine und sperrt sie nicht). Angemeldet wird
beim Start, aber nur, wenn sich die Befehle geändert haben (`data/befehle.json`)
— alle drei immer, auch wenn ein Teil aus ist: sonst gäbe jedes An- und
Abschalten einen neuen Abgleich mit Discord.

**Rechte:** Umfragen brauchen „Umfragen erstellen“, der Rechtsklick den Scope
`applications.commands`. Den Einladungslink mit allen Rechten schreibt Flo ins
Log, wenn er auf keinem Server ist (`bot.invite_url()`). Wer Flo früher
eingeladen hat, lädt ihn einfach noch einmal ein — das ergänzt nur Rechte.

---

## Server-Leben

Was Flo von sich aus für den Server tut. Alles läuft über einen gemeinsamen
Takt (`alltag_loop`, `ALLTAG_TAKT`, Standard eine Minute); was fällig ist,
entscheidet jedes Modul selbst, nach der Uhr nur, wenn `uhr.stabil()` ist.
Hilfe-Seite: „Server-Leben" (`flo hilfe`).

### Zitat der Woche

```
Rechtsklick → Apps → Als Zitat speichern   eine Nachricht wird Kandidat
flo zitat speichern     (als Antwort auf die Nachricht) dasselbe, auch: zitat merken
flo zitate              die Kandidaten der nächsten Abstimmung, nummeriert
flo zitat der woche     der letzte Sieger
flo zitat weg 3         Kandidat 3 raus
flo zitat umfrage jetzt nur der Chef: die Abstimmung sofort (Testauslöser)
```

Wer etwas Dummes sagt, wird gespeichert — **nicht von sich selbst** („Eigenlob
stinkt. Lass das andere entscheiden."). Bestätigt wird nur dem, der speichert
(beim Rechtsklick unsichtbar für die anderen), dazu ein 📌 an der Nachricht.
Abgelehnt werden: Bot-Nachrichten, die eigene, leere (nur Bild/Sticker), unter
3 oder über 300 Zeichen, schon gespeicherte, Hetze, Links und private Daten
(Mail, Telefon, Adresse — dieselbe Prüfung wie beim Gedächtnis). Eine
Nachricht mit **Notsignal** (oder ein Name mit einem) wird neutral abgelehnt,
ohne Spruch und ohne Grund. **Aus dem Hinterzimmer** kommt nichts: Nachrichten
aus einem NSFW-Kanal, einem privaten Thread oder einem Kanal, den nicht jeder
lesen kann, der den Zitat-Kanal liest (Mod-Kanal, privater Kanal) — die
zitierte Person hat sich ihr Publikum nicht ausgesucht („Was im Hinterzimmer
gesagt wird, bleibt im Hinterzimmer."). Gemessen wird je Rolle (Bot-Rollen
zählen nicht), so geht es auch auf Servern, auf denen `@everyone` nichts sieht.
Dieselbe Prüfung gilt beim Start der Umfrage und in `flo zitate` (dort fehlt so
ein Kandidat im Wortlaut, seine Nummer bleibt frei). Höchstens 50 offene
Kandidaten je Server und 10 je Person, die speichert. Rausnehmen (`flo zitat weg <nr>`) darf, wer gespeichert
hat, **wer zitiert wurde**, und die Mods („Nachrichten verwalten").

**Sonntags ab 12 Uhr** startet Flo im Kanal `zitat_channel` (sonst in den
Ansagen, sonst im System-Kanal) eine echte Discord-Umfrage, 8 Stunden lang — „🏆 Zitat der Woche – wer
hat den größten Mist gelabert?". Bis zu **zehn** Kandidaten, höchstens **zwei**
derselben Person; sind es mehr, eine feste Zufallsauswahl (gleicher Server,
gleiche Woche, gleiches Ergebnis). Jede Antwort ist nummeriert und auf Discords
55 Zeichen gekürzt („3 · „Pizza ist ein…“ – Bob"), die vollen Zitate stehen im
Text darüber. Gibt es nur **einen** Kandidaten, gewinnt er konkurrenzlos (ohne
Umfrage); gibt es **keinen**, kommt eine derbe Zeile — aber nur, wenn hier in
den letzten vier Wochen überhaupt etwas gespeichert wurde. Fehlt Flo das Recht
„Umfragen erstellen", entscheidet er allein. Wer gerade ein Notsignal geschrieben
hat (Flo merkt sich 72 Stunden lang nur, *dass* — im Speicher, auch ohne KI),
kommt nicht zur Wahl.

**Nie in einen Aufräum-Kanal** (`autodelete_channels`): dort löscht Flo auch
seine eigenen Posts nach einer Minute — die Umfrage wäre weg, bevor einer
abstimmt, und Discord schickte nie ein Ergebnis. Auf dem Hauptserver ist der
Ansagen-Kanal ab Werk genau so einer; dann nimmt Flo den System-Kanal, und gibt
es keinen, läuft keine Umfrage (steht im Log, `flo zitat umfrage jetzt` sagt
es). Am besten `zitat_channel` einstellen.

**Das Ergebnis.** Ist die Umfrage vorbei, postet Discord eine Ergebnis-Nachricht;
die geht an `zitate`, nicht an den normalen Umfrage-Kommentar. Den Sieger liest
Flo aus ihren Feldern; bei **Gleichstand** holt er die Umfrage selbst und
entscheidet unter den Besten („Gleichstand, ich entscheide: …"), bei null
Stimmen gewinnt keiner. Der Sieger bekommt eine **Zitat-Karte** (wie das
Quote-Meme, mit Profilbild), zwei fiese Sätze von der KI (Hintergrund-Aufruf
mit Grenze; sonst aus dem Ersatz-Pool) und als **Einziger einen Ping**. Hat er
während der Abstimmung ein Notsignal geschrieben: keine KI, kein Spott, kein
Ping — nur die Karte und „Glückwunsch. Heute ausnahmsweise ohne Spruch." War Flo
offline, als das Ergebnis kam, holt der Takt die Umfrage 30 Minuten nach dem
Ende selbst und wertet sie aus, sobald Discord sie abgeschlossen hat. In der
**Sendepause** wird der Sieger festgehalten und danach gepostet; lehnt Discord
den Post ab, versucht Flo es noch ein paar Mal (ohne „Dateien anhängen" eben
ohne Karte).

**Genau einmal.** Gestartet wird nur bei verlässlicher Uhr, und „diese Woche
gelaufen" (ISO-Woche) läuft nur vorwärts und steht auf der Platte, **bevor** die
Umfrage rausgeht — nach dem Uhrsprung beim Booten kommt keine zweite. War Flo
sonntags weg, holt er die Abstimmung bis Montag 12 Uhr nach; danach wandern die
Kandidaten in die nächste Runde. Was nach dem Start gespeichert wird, gehört
schon zur nächsten. `flo zitat umfrage jetzt` zählt als die Runde — sonntags
kommt für diese Woche keine zweite; ohne Kandidaten verbraucht er nichts. Bleibt
das Senden der Umfrage **ohne Antwort** (Netz weg, Zeitüberschreitung), weiß
keiner, ob sie drüben ist: Flo schickt keine zweite, sondern sieht nach fünf
Minuten im Kanal nach (und nimmt ihr Ergebnis an, falls es vorher kommt). Ist
sie nicht da, ist die Runde wieder frei; kann er nicht nachsehen, gibt er sie
nach drei Tagen frei — die Kandidaten wandern dann weiter.

**Was sich für das Quote-Meme geändert hat.** `flo zitat <text>` und `flo zitat`
als Antwort machen weiter das Bild wie immer — außer, hinter `zitat` (oder
`zitate`, beide gelten gleich) steht **genau** eine dieser Formen; dann ist es ein
Befehl des Zitats der Woche, und ein Meme mit diesem Text gibt es nicht mehr.
Satzzeichen am Rand zählen nicht (`Zitat der Woche?`).

| Hinter `zitat`/`zitate` | Befehl |
|---|---|
| `der woche`, `dieser woche`, `letzte woche`, `der letzten woche`, `von letzter woche` | der letzte Sieger |
| `speichern`, `speicher`, `merken`, `merk` | als Antwort: speichern |
| `weg`, `raus`, `löschen`/`loeschen`, `entfernen`, `delete` — allein oder mit Nummer (`weg 3`, `löschen #2`) | Kandidat raus (ohne Nummer: „Welche Nummer?") |
| `umfrage jetzt`, `abstimmung jetzt`, `umfrage starten` | Testauslöser (für alle außer dem Chef: „Den Knopf drückt nur der Chef.") |
| nur hinter `zitate`: nichts, `liste`, `zeigen`, `anzeigen`, `kandidaten` | die Kandidaten |

Alles andere hinter `flo zitate` (etwa `flo zitate von goethe`) wurde früher per
Tippfehler-Korrektur zum Meme; heute antwortet darauf die KI.

Je Server abschaltbar (Schalter `zitate`, auch der Rechtsklick). Daten:
`data/zitate.json` — die Kandidaten der letzten sechs Wochen (Text, Name,
wer gespeichert hat), die laufende Abstimmung und die letzten 20 Sieger.

### Geburtstage

```
flo geburtstag 12.03.     eintragen – auch 12.3, 12. März, 1. mai, 29.02.
flo geburtstag            deinen zeigen (und wie lange noch)
flo geburtstag löschen    austragen (auch: lösche, entfernen, austragen, weg)
flo geburtstage           die nächsten zehn auf diesem Server
```

Ein Jahr dahinter wird ignoriert („Das Jahr will ich gar nicht wissen.").
**Nur für dich selbst** — mit einer Erwähnung dahinter gibt es eine Abfuhr, und
gespeichert wird nichts. Je Server getrennt. Gelöscht wird nur auf ein
eindeutiges Wort — „Flo, Geburtstag vergessen?" ist ein Vorwurf, kein Auftrag,
und mit Fragezeichen (`geburtstag löschen?`) löscht nichts.

Am Geburtstag **ab 9 Uhr** gratuliert Flo im Kanal `geburtstag_channel` (sonst
in den Ansagen, sonst im System-Kanal — nie in einem Aufräum-Kanal, dort wäre
der Glückwunsch samt Ping nach einer Minute weg): ein Ping **nur** für das Geburtstagskind, zwei passiv-aggressive
Sätze von der KI (Hintergrund-Aufruf mit Grenze; sonst „Alles Gute, Bob. Wieder
ein Jahr älter und immer noch nix erreicht.") und das Geschenk:
`geburtstag_coins` (Standard 2.500, höchstens 10.000, `0` = nur der
Glückwunsch). Mit `geburtstag_rolle` (Standard nur auf dem Hauptserver an, braucht
„Rollen verwalten") gibt es für den Tag die Rolle „🎂 Geburtstagskind"; Flo nimmt
sie am nächsten Tag wieder (per `fetch_member`, auch nach einem Neustart — wer
sie hat, steht in der Datei). Der 29.02. wird in Nicht-Schaltjahren am 28.02.
gefeiert. War Flo offline, holt er bis zu **zwei Tage** nach („Nachträglich,
weil ich offline war: …"); in der Sendepause wartet der Glückwunsch. Mit dem
ersten Eintrag auf einem Server gilt alles bis gestern als erledigt — wer da
seinen gestrigen Geburtstag einträgt, bekommt kein falsches „nachträglich".

**Coins gibt es nicht auf Zuruf** (sie gelten auf allen Servern):

* **Vorlauf:** bezahlt wird nur, wenn das Datum mindestens **14 Tage** vor dem
  Geburtstag eingetragen war. Wer es zwei Tage vorher einträgt, bekommt den
  Glückwunsch — mit einer Bemerkung statt Coins.
* **Ändern** höchstens alle **30 Tage**. Der erste Eintrag ist frei (ein
  Tippfehler darf gleich korrigiert werden); jede Änderung danach sperrt, und mit
  ihr fängt der Vorlauf neu an.
* **Einmal im Jahr** je Server gefeiert, egal wie oft das Datum wandert — und
  **einmal im Jahr bezahlt, über alle Server zusammen**: wer auf fünf Servern
  eingetragen ist, wird fünfmal gefeiert, aber einmal beschenkt („Coins gab's
  dieses Jahr schon auf einem anderen Server …"). Sonst druckte sich jeder mit
  ein paar Privatservern Geld.
* **Löschen hilft nicht:** „gefeiert" und die Sperre bleiben stehen, ein neuer
  Eintrag nach dem Löschen ist eine Änderung.
* Ein Server-Admin kann höchstens 10.000 einstellen (und für den DJ des Monats
  25.000). Ein höherer Wert, der von früher in der Datei steht, gilt nicht — dann
  der Standard.

Bezahlt wird nach „lock-before-pay": erst „gefeiert" auf die Platte, **dann** die
Coins — bricht der Takt ab, wird trotzdem zu Ende bezahlt, nie doppelt. Den Post
holt der Takt nach, wenn Discord ihn ablehnt. Wer nicht mehr auf dem Server ist,
bekommt nichts und fliegt aus der Liste (gefeiert und die Sperre bleiben). Ohne
Wirtschaft (Modul oder Schalter `economy` aus) nur der Glückwunsch.

**Nie zu einer Notlage:** ein Name mit Notsignal geht nicht an die KI, eine
KI-Zeile mit Notsignal fliegt raus, und wer gerade ein Notsignal geschrieben hat
(72 Stunden, nur im Speicher, auch ohne KI), bekommt einen Glückwunsch ohne
Spott und ohne KI.

Je Server abschaltbar (Schalter `geburtstage`). Daten: `data/geburtstage.json` —
Tag, Monat und Anzeigename, nie ein Jahr; dazu, über alle Server, in welchem
Jahr jemand zuletzt Geburtstags-Coins bekam.

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
und die Handler-Kette führen dieselben 30 Schlüssel — `test_feature_schluessel_
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
| `umfrage.py` | echte Discord-Umfragen + Ergebnis-Kommentar |
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
| `uhr.py` | Uhr, die Sprünge übersteht: Schlüssel, `stabil()`, zeitsprungfeste Loops |
| `playlists.py` / `musikquellen.py` | Playlists je Server, eingefügte Links und Zeilen |
| `musikzustand.py` | Warteschlange über den Neustart retten |
| `musikstats.py` | Musik-Wrapped, DJ des Monats |
| `laune.py` / `nachhaken.py` | Flos Launen, „er hält nach" |
| `zitate.py` / `geburtstage.py` | Zitat der Woche, Geburtstage |
| `clips.py` | Voice-Clips: Ansage, Opt-out, Puffer, Befehle, Dialog, Post, 🔥 |
| `voiceempfang.py` | Voice-Empfang: RTP, Transport- und DAVE-Entschlüsselung, je Sprecher |
| `clipschnitt.py` | Clip-Schnitt: Opus → Spuren → ffmpeg-Mix → MP3 unter dem Upload-Limit |
| `clipstext.py` | Whisper-Transkript, Prüfung (Notsignal/Hetze/Privates), Spruch, Bestenliste |

### Daten

Alles unter `data/` (nicht im Git). `store.JsonStore` schreibt atomar
(tmp + fsync + replace) und zieht bei jedem Speichern den vorherigen Stand als
`.bak` mit. Ist eine Datei unlesbar, wird sie als `.kaputt-<Zeitstempel>`
beiseitegelegt und die Sicherung eingespielt — **nichts wird stillschweigend
überschrieben**.

Neu dazu kommen mit den Teilen auf dem Fundament: `playlists.json`,
`musikzustand.json`, `musikstats.json`, `launen.json`, `nachhaken.json`,
`zitate.json` und `geburtstage.json` — alle je Server, nichts wandert zwischen
Servern. Ausnahme sind die beiden Kassenbücher (`"bezahlt"` in
`geburtstage.json` und `musikstats.json`): über alle Server, wer für welches
Jahr bzw. welchen Monat zuletzt Coins bekam — Coins gelten ja auch überall.
Wer die Daten eines Servers von Hand löscht, lässt sie stehen.

`clips.json` (Voice-Clips) hält nur, wer raus ist (über alle Server), wann wo
die Ansage steht, „Mikro aus" je Kanal, das Whisper-Kontingent und je Server
einen Index der Clips mit 🔥 — **keinen Ton und kein Transkript**.

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
| `MUSIC_VORLADEN` | `1` | nächsten Song schon während des aktuellen auflösen (keine Stille dazwischen) |
| `MUSIC_IDLE_SEKUNDEN` | `300` | so lange bleibt Flo ohne Musik oder allein im Voice (`0` = für immer) |
| `PLAYLIST_MAX_LISTEN` / `_PRO_PERSON` / `_SONGS` | `50` / `10` / `200` | Playlists je Server, je Person, Songs je Liste |
| `PLAYLIST_WIZARD_TIMEOUT` | `300` | so lange wartet der Playlist-Assistent auf Songs (Sekunden) |
| `PLAYLISTS_ENABLED` | `1` | `0` schaltet die Playlists ab (die Musik bleibt) |
| `MUSIKQUELLEN_FRIST` | `5` | so lange darf ein Titel-Abruf (oEmbed, Spotify-Einbettung) dauern (Sekunden; Alben/Playlists mindestens 15) |
| `MUSIC_RADIO_MAX` | `150` | so viele Songs legt das Endlos-Radio je Sitzung höchstens nach |
| `MUSIC_WIEDERHERSTELLEN_MIN` | `20` | so frisch muss eine Warteschlange sein, damit Flo nach einem Neustart zurückkommt (Minuten) |
| `MUSIKZUSTAND_ENABLED` / `MUSIKSTATS_ENABLED` | `1` | `0` schaltet die neustartfeste Warteschlange bzw. das Musik-Wrapped ab |
| `UMFRAGE_STUNDEN` | `24` | Standard-Laufzeit von `flo umfrage` (1–768) |
| `ZITATE_ENABLED` / `GEBURTSTAGE_ENABLED` | `1` | `0` schaltet das Zitat der Woche bzw. die Geburtstage ab |
| `CLIPS_ENABLED` | `1` | `0` schaltet die Voice-Clips ab (sie brauchen ohnehin die Musik) |
| `CLIP_CHANNEL_ID` | Clip-Kanal des Hauptservers | Standard für `clip_channel` (je Server überschreibbar) |
| `CLIP_TMP_DIR` | System-Temp | wo der Schnitt seine privaten Temp-Ordner anlegt |
| `CLIP_TRANSKRIPT` | an | `aus` = Clips ohne Whisper-Transkript |
| `CLIP_WHISPER_KEY` / `_URL` / `_MODEL` | — / Groq / `whisper-large-v3-turbo` | eigener Whisper-Zugang; ohne Schlüssel nimmt Flo den der KI, wenn die bei Groq ist |
| `CLIP_PUFFER_MB` | `48` | so viel Arbeitsspeicher darf der 5-Minuten-Puffer je Server belegen (1–160; insgesamt höchstens 160; Unlesbares gilt als 48) |
| `UHR_START_GNADE` | `300` | so lange nach dem Start traut Flo der Wanduhr noch nicht (Sekunden) |
| `ALLTAG_TAKT` | `60` | Takt fürs Server-Leben (Launen, Nachhaken, Zitate, Geburtstage, Wrapped) |

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

Die Testsuite liegt in 25 Themendateien (`test_musik.py`, `test_casino.py`, …);
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
