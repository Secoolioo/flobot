# Fable-Bericht: Volltest & Arbeitsaufträge

Dieses Dokument ist der Arbeitsauftrag für die nächste Programmier-Runde.
Es enthält **keine** Änderungen — nur Messungen, bestätigte Fehler und fertige
Konzepte. Reihenfolge der Kapitel = empfohlene Reihenfolge der Umsetzung.

**Basislinie beim Test:** Commit `bb248b0`, `test_games_logic.py` 130 grün,
`test_logic.py` 6 grün, `bot.py --check` lädt alle Module. Getestet wurde mit
12 unabhängigen Prüf-Blickwinkeln plus gegnerischer Widerlegung jedes Fundes;
Kapitel 1.1–1.4 enthalten nur reproduzierte Fehler, Kapitel 1.5 die noch
offenen (klar markiert).

---

## Stand der Umsetzung

Der Bericht bleibt als **Protokoll des Testlaufs** unverändert stehen — er ist
die Begründung für die Änderungen, nicht ihre Beschreibung. Was daraus geworden
ist:

| Kapitel | Stand |
|---|---|
| 1.1 Musik (M1–M11) | umgesetzt · dazu eine Regression aus dieser Runde gefunden und behoben (der Watchdog stieß die aufgegebene Warteschlange alle 15 s neu an) |
| 1.2 Kritisch & Hoch (B1–B8) | umgesetzt |
| 1.3 / 1.4 Mittel & Niedrig | umgesetzt |
| 1.5 Ungeprüfte Funde | **alle 51 nachgeprüft** (50 bestätigt, 1 widerlegt), alle bestätigten behoben |
| 2 SoundCloud | umgesetzt (Tracks, Sets, Kurzlinks, direkte Audiodateien) |
| 3 Schuldbuch 2.0 | umgesetzt — Posten statt Paar-Saldo, Schuld nur mit Zustimmung, anteilige Tilgung, Kreditwürdigkeit, Verfall, Mahnstufen, Insolvenz, Migration |
| 4a Präfix je Server | umgesetzt (`ai.py` als einzige Autorität, `basis.FeatureBasis`, ContextVar) |
| 4b Panel | Katalog-Einträge nachgezogen, dazu Backup-Knopf und Panel-Protokoll |
| 4c Mehr-Server-Härtung | `words.json` je Server (mit Migration), Begrüßung beim Einladen, Grenzen dokumentiert |
| 4d OOP-Checkliste | gilt weiter — Punkt 1 ist jetzt `basis.FeatureBasis`, ein Test hält es wach |

**Bewusst nicht gebaut** (mit Begründung, statt still zu übergehen):

* *Begrüßung neuer Mitglieder* (`willkommen_channel`/`-text`, Kapitel 4b):
  braucht `on_member_join`, und das feuert nur mit dem privilegierten
  Members-Intent, den der Bot absichtlich nicht hat. Ein Schalter, der nichts
  tut, wäre schlimmer als keiner.
* *Debounce für `economy._flush()`* (Kapitel 1.5, Performance): mit dem
  kompakten JSON liegt ein Speichern auch bei 3.000 Nutzern bei ~10 ms statt
  42 ms. Ein Sammel-Speichern wäre ein Fenster, in dem Coins bei einem Absturz
  verloren gehen — erst nötig, wenn der Topf wirklich in die Tausende geht.
* *Go+-Erkennung bei SoundCloud* (Kapitel 2): SoundCloud liefert für solche
  Titel nur die 30-Sekunden-Vorschau, und es gibt kein Feld, an dem man das
  vorher sicher erkennt. Steht als Grenze im README.
* *Status-Weisheiten im Panel bearbeitbar* (Kapitel 4b): reine Kosmetik, im
  Vergleich zu Backup und Protokoll der geringste Nutzen.

---

## 1. Bestätigte Fehler

**Verfahren:** 12 unabhängige Prüf-Blickwinkel haben 92 Funde gemeldet, jeder
einzelne musste eine gegnerische Widerlegung mit eigener Reproduktion
überstehen. Ergebnis: **30 bestätigt**, **25 widerlegt** (nicht aufgeführt),
37 Prüfungen brachen am Nutzungslimit ab — davon habe ich **10 selbst
nachreproduziert und bestätigt**, einer ist durch einen bestätigten
Nachbar-Fund gedeckt, **26 bleiben ungeprüft** (Kapitel 1.5, vor dem Fixen
erst verifizieren). Unterm Strich: **41 belegte Fehler.**

---

### 1.1 MUSIK — Warum die Queue voll ist und nichts spielt (der gemeldete Fehler)

> Betreiber-Symptom, wörtlich: „Oft sind paar sachen in der queue und dann
> spielt er sie nicht, erst auf flo stop und dann flo nochmal [hilft]."

**ROOT CAUSE (kritisch): Der FFmpeg-Stall-Zombie.** `music.py:90-93/642`.
Bleibt der HTTP-Audio-Stream mitten im Song still stehen (hängendes
CDN/NAT — die Verbindung bleibt offen, es kommen nur keine Daten mehr),
wartet FFmpeg **endlos**: `_FFMPEG_BEFORE` setzt kein `-rw_timeout`, und die
`-reconnect*`-Flags greifen nur bei Fehler/EOF, nicht beim stillen Stall.
Der discord.py-Player-Thread blockiert dauerhaft in `source.read()`, der
`after`-Callback feuert **nie**, `is_playing()` bleibt **True**. Folgen:

- Jeder weitere `Flo spiel X` sieht `is_active() == True` und hängt den Song
  **nur an die Queue** — genau das gemeldete Bild.
- Der 15-s-Watchdog (`heal`, Z. 642) prüft nur `not vc.is_playing()`, hält
  sich für unzuständig und setzt `_stall_ticks` sogar auf 0. Einen
  Fortschritts-Check (`position()` bewegt sich nicht) gibt es nirgends.
- `Flo stop` → `disconnect()` killt FFmpeg (SIGKILL bewiesen), `Flo nochmal`
  → Replay aus der history auf frischem FFmpeg. **Exakt der Workaround des
  Betreibers — dabei geht die gesammelte Queue verloren.**

Beleg: mit **echtem FFmpeg und echtem discord.py 2.7.1** reproduziert
(lokaler HTTP-Server liefert 2 s Audio und hängt dann): nach 12 s weiterhin
`is_playing()=True`, `after`=nie, Player-Thread hängt nachweislich in
`player.py:399 _stdout.read`. Wichtige Präzisierung aus der Widerlegung:
nach `Flo skip` setzt discord.py `_player=None` → `is_playing()` wird False
→ der Watchdog verbindet nach 3 Stall-Ticks (~45 s) neu und die **Queue
bleibt erhalten** — aber es läuft wieder Song A, nicht der nächste, und ohne
Nutzereingriff bleibt der Zombie unbegrenzt bestehen.

**Fix-Richtung (fürs Umsetzen):**
1. `-rw_timeout 15000000` (µs) in `_FFMPEG_BEFORE` — FFmpeg bricht dann bei
   15 s Datenstille selbst ab, `after` feuert, `_advance` läuft normal.
2. Fortschritts-Watchdog in `heal()`: merkt sich `position()` je Tick; ist
   `is_playing()` True, aber die Position bewegt sich 2–3 Ticks nicht →
   Quelle stoppen und denselben Track an der Position neu starten
   (Queue **unangetastet**).
3. Test mit gefaktem Stall-VoiceClient, der beide Wege absichert.

**Weitere bestätigte Musik-Fehler** (jeder erklärt einen Teil der
Unzuverlässigkeit):

| # | Schwere | Fund | Stelle | Kern |
|---|---|---|---|---|
| M2 | hoch | Songende während kurzem Voice-Aussetzer | `music.py:578` | endet ein Song, während die Verbindung gerade weg ist (Kick, Voice-Server-Wechsel), setzt `_advance` `current=None` und lässt die **volle Queue liegen**; der Watchdog stellt die Verbindung wieder her, aber **niemand startet die Queue je wieder**. Zweiter permanenter Steckzustand. Fix: `heal()` muss „verbunden + nichts spielt + Queue voll" erkennen und `_advance` anstoßen. |
| M3 | hoch | Geister-Track nach `stop` | `music.py:589` | `Flo stop`, während `_resolve_track` gerade einen Lazy-Track auflöst: der gen-stale-Pfad legt den fertigen Track per `queue.insert(0, …)` in die **soeben geleerte** Queue zurück — der gestoppte Song kehrt beim nächsten Play ungefragt wieder. Fix: im stale-Pfad nur zurücklegen, wenn die Session noch dieselbe ist (`active_channel_id` gesetzt und gen-Abstand == 1 reicht nicht — Absicht „stop leert" muss gewinnen). |
| M4 | mittel | Netzausfall beim Songwechsel frisst die ganze Playlist | `music.py:592` | jeder fehlgeschlagene Resolve macht `continue` ohne Retry — ein kurzer DNS-Aussetzer verbucht **alle** Lazy-Tracks als „nicht ladbar", die Queue ist leer, im Chat steht nichts. Fix: bei Resolve-Fehlern Fehlerklasse unterscheiden, 1 Retry mit kurzem Backoff, und ab dem 2. Fehlschlag in Folge stoppen statt weiterfressen + Chat-Meldung. |
| M5 | mittel | Panel-Race bei Doppel-Skip | `music.py:2044` | überlappende `_send_panel` speichern das **veraltete** Panel; das neue bleibt als Zombie mit klickbaren Buttons stehen, die View (timeout=None) leckt. Fix: Sende-Generation prüfen, bevor `panel_message/panel_view` überschrieben wird. |
| M6 | mittel | `rausschmeisen` (ein s) → Musik-Leave statt Kick | `music.py:172` | Leave-Regex matcht Präfix `raus` ohne Wortgrenze; Moderation verlangt ß/ss; cmdnorm-KNOWN enthält den toten Stamm `rausschmei`. Flo verlässt den Sprachkanal und **bricht die Musik ab**, statt zu kicken. Fix: `\b` in den Leave-Regex, Ein-s-Variante in `_KICK_RE`. |
| M7 | niedrig | Tempo-Wechsel in der Pause hebt die Pause auf | — | Panel-Pause-Knopf zeigt danach falsch. |
| M8 | niedrig | Watchdog-Reconnect während Pause spielt ungefragt weiter | — | Pausenzustand muss den Reconnect überleben. |
| M9 | niedrig | `verlass dich drauf` → Leave | — | Steuerbefehl-Präfixe ohne Wortgrenze kapern normale Sätze. |
| M10 | niedrig | `flo ls 1000` setzt 100 % statt auf 200 zu klemmen | — | Regex schneidet auf 3 Ziffern. |
| M11 | niedrig | `flo pause` im Pausenzustand: „Ich spiele gerade nichts" | — | irreführende Antwort. |

---

### 1.2 Kritisch & Hoch (übrige Module)

**B1 (hoch, bot.py/merchant) — Erste Händler-Ankunft tötet den Loop
dauerhaft.** `bot.py:999`: ruft `merchant.build_view()` — **diesen
Modul-Alias gibt es nicht** (nur die Methode auf der Instanz). Der try
fängt nur `discord.HTTPException`; der `AttributeError` stoppt
`merchant_loop` für immer, die Ankunft wird nirgends angesagt, und weil
`arrived=True` schon gespeichert ist, kommt sie auch nach Neustart nicht.
_(Eigenverschulden aus der Mehr-Server-Runde — Alias `build_view = instance.build_view` in merchant.py ergänzen; zusätzlich in JEDEM Loop `except Exception` statt nur HTTPException.)_

**B2 (hoch, guildcfg) — Aufräum-Kanäle lassen sich nie wirksam setzen.**
`guildcfg.py:276`: `get()` re-validiert gespeicherte Werte über `_pruefe` —
für Typ `channels` wird die gespeicherte **Liste** dabei durch
`str([123, 456])` gejagt, die Klammer-Tokens fallen durch die Kanal-Prüfung,
und `get()` liefert **immer den Standard**. Die einzige channels-Einstellung
ist damit faktisch un-konfigurierbar; auf fremden Servern räumt der Sweep
gar nicht, auf dem Hauptserver ggf. den falschen Kanal.
_(Eigenverschulden. Fix: `_pruefe` muss Listen nativ akzeptieren [jedes Element einzeln durch `_kanal_id`], plus Roundtrip-Test setzen→get für JEDEN Typ.)_

**B3 (hoch, sechs Module) — Eine von Hand kaputte JSON-Datei verhindert den
kompletten Bot-Start.** Selbst nachreproduziert: `words.json` mit
`scan: []` (AttributeError), `features.json` mit nicht-numerischem
Guild-Schlüssel (ValueError — Eigenverschulden dieser Runde),
`economy.json` mit `users: null` (TypeError), `lotto.json` mit
`jackpot: "viel"`, `giveaway.json` mit `active: null`, `schulden.json` mit
`pairs: null`. Alle sechs werfen in `setup()`, und da die `setup()`-Kette in
bot.py auf Modulebene läuft, **startet der ganze Bot nicht mehr**.
_(Fix-Muster existiert schon: profil.py/guildcfg.py prüfen Typen beim Laden und fangen leer an — dasselbe Muster in alle Module. store.py könnte zusätzlich eine Typ-Schablone je Store anbieten.)_

**B4 (hoch, casino) — HiLo/Tower/DON verlieren den Einsatz ersatzlos.**
`casino.py:1962`: schlägt `edit_message` beim „Nochmal"-/Setup-Start fehl
(Rate-Limit, gelöschter Kanal), ist der Einsatz schon abgebucht, die neue
View wird nie registriert, ihr Timeout-Netz feuert nie — **kein Refund**.
Die Nachbar-Pfade (`_starten`, Mines, Blackjack) sichern genau das ab.
_(Fix: dieselbe try/refund-Klammer wie in `_starten` um alle edit_message-Startpfade.)_

**B5 (hoch, floaktie) — `admin_shares give` mit keep_price sprengt den
Markt.** `floaktie.py:860`: schreibt `base` **ungeklemmt** (0.0009), jeder
Lesezugriff läuft aber durch `_clamp_base` → Kurs springt ×11.111, ein
Unbeteiligter verkauft 150 Anteile für Milliarden aus dem Nichts.
_(Fix: base nach keep_price durch `_clamp_base` schicken und den Panel-Weg auf ADMIN_MAX begrenzen — plus Regressionstest mit Verkauf eines Unbeteiligten.)_

**B6 (hoch, moderation) — Purge-„alles"-Rückfrage ist nur kanal-, nicht
nutzergebunden.** `moderation.py:617`: A fragt an, B (anderer Mod) schreibt
danach im selben Kanal „lösch alle" — B's **erster** Befehl führt den
Total-Wipe aus, ohne dass B je die Warnung sah.
_(Fix: `_wipe_offen[channel_id] = (uid, ablauf)` und nur derselbe Nutzer darf bestätigen.)_

**B7 (hoch, webpanel) — Jeder POST-Endpunkt wirft HTTP 500 bei gültigem
Nicht-Objekt-JSON** (`[1,2,3]`, `null`, `42`): `request.json()` wirft dann
nicht, `data.get(...)` schon. Betrifft alle 11 POST-Endpunkte.
_(Fix: zentrale Hilfe `_json_objekt(request)` → dict oder 400.)_

**B8 (hoch, schulden/games) — Einsatz-Rückgaben werden bei Schuldnern um
20 % gekürzt.** Bestätigt über den games-Fund: Duell-/Reaktions-Timeouts
geben den Einsatz über `economy.add_coins(uid, bet)` **ohne reason** zurück
(games.py 397, 487 f., 651 f., 1544, 1653, 1732, 1749) — die Tilgung
behandelt das als Einnahme, obwohl `-rueck` laut Design ausgenommen ist.
Der Bot meldet „Einsätze zurück", tatsächlich kommen 80 % an.
_(Fix: JEDE Rückgabe mit reason `…-rueck` buchen; Suche nach `add_coins(` ohne reason als Test.)_

---

### 1.3 Mittel (übrige Module)

| Modul | Fund | Stelle | Kern + Fix-Richtung |
|---|---|---|---|
| economy | Kauf-Rückfrage bindet nur an die Slot-**Nummer** | `economy.py:1745` | nach dem 2-Uhr-Reroll kauft die Bestätigung einen **anderen** Titel, dessen Preis der Nutzer nie sah. Fix: Titel-Identität (text+preis) in `_kauf_offen` mitspeichern und vergleichen. |
| casino | MAX_WIN-Deckel: Embed & Bilanz melden den **ungedeckelten** Gewinn | `casino.py:599` | Konto bekommt korrekt 5 Mrd, Anzeige/record behaupten mehr. Fix: Rückgabewert von `_auszahlen` überall verwenden. |
| games | `flo slot 1000000` ohne Höchsteinsatz | `games.py:1044` | Textpfad umgeht `SKILL_MAX_BET` (5.000) **und** die Tageskappe. Fix: gleiche Klemme wie Coinflip-Textpfad. |
| giveaway | „jetzt"/„ende"/„stop" im Schnellstart kapern ziehen/abbrechen | `giveaway.py:979` | laufendes Giveaway wird Stunden zu früh ausgelost. Fix: ziehen/abbrechen nur als exaktes Erstwort, nicht als Substring im Schnellstart. |
| floaktie | Verkaufssteuer ignoriert den Grundwert | `floaktie.py:581` | am legitimen Boden nachts 35 % statt 2 % — `_sell_fee` misst gegen `ziel_base(act_ema)` statt wie `deckel_fuer()` gegen `max(…, boden_base())`. Fix: gleiche max()-Basis. |
| cmdnorm | weitere gefährliche Korrekturen | `cmdnorm.py:200` | `banane→ban`, `klick/kicks→kick`, `waren→warn` … Alltagswörter fehlen in STOPWORDS. Fix: STOPWORDS erweitern + Test „harmlose Wörter dürfen nie auf mod-Befehle korrigieren" (Liste im Test pflegen). |

Selbst nachreproduziert (Prüfung war am Limit abgebrochen):

| Modul | Fund | Beleg |
|---|---|---|
| profil | **USER_MAX verdrängt den NEUEN Nutzer statt des ältesten** | `_deckel()` läuft, solange der frische Eintrag noch `letzt=0` hat → sortiert zuerst → fliegt selbst raus. Bei vollem Verlauf wird nie wieder jemand Neues erfasst. _(Eigenverschulden dieser Runde; Fix: `letzt` vor `_deckel()` setzen.)_ |
| profil | **Cooldown-Reset + Stille bei unbekannter ID** | `Flo check <fremde ID>`: 1–2 echte REST-Aufrufe, dann `None` (KI antwortet irgendwas) und der Cooldown wird **zurückgesetzt** → beliebig oft wiederholbar. Fix: bei ID-Ziel ist es NIE Gerede — Fehlertext statt None, Cooldown behalten. |
| food | `_num("1.200") == 1.2` | deutsche Tausenderpunkte werden als Dezimalpunkt gelesen — „ca. 1.200 kcal" wird zu 1,2. Fix: Tausender-Heuristik wie in `economy.parse_amount`. |
| bayern | „bayerisch" wird nicht erkannt | Toggle-Regex kennt nur „bayrisch/boarisch" — die Standard-Schreibweise schaltet nichts. |

### 1.4 Niedrig (bestätigt)

- economy: `add_coins` gibt den Stand **vor** der Schulden-Tilgung zurück — Anzeigen lügen bei Schuldnern (`economy.py:394`).
- economy/schulden: `setcoins`/`gib` (reason `admin`) fehlen in `TILGUNG_TABU` — Admin-Korrekturen werden angeknabbert, obwohl `panel` genau dafür ausgenommen ist (`schulden.py:75`).
- economy: offene Inventar-View equipped einen inzwischen weggetauschten Titel (kein Besitz-Recheck im Dropdown, `economy.py:1900`).
- games: Slot-Textbefehl zahlt an der Tageskappe vorbei (siehe 1.3, gleicher Pfad).
- casino/lotto: `Flo los`/`Flo lose` erreichen nie das Lotto — Casino (früher in der Kette) deutet „lose 5" als 5-Coin-Rubbellos (`casino.py:457`). Fix: Kollisions-Entscheid dokumentieren oder Lotto-Wörter vor Casino prüfen.

### 1.5 Ungeprüfte Funde (Widerlegung am Limit abgebrochen — vor dem Fixen erst reproduzieren!)

**Robustheit (gleiche Familie wie B3, sehr wahrscheinlich echt):** korrupte
Einträge crashen zur Laufzeit statt beim Start — economy-Profile mit
falschen Typen (`add_coins`/`add_xp`), floaktie `holdings`/`history`,
games `counting`, handel `users`, luxus `throne`, moderation `warns`
(→ `warns_of` im Profil!), steal `cooldowns`, casino `stats`, words je
Nachricht. Dazu: kaputte `.bak` wird nicht quarantäniert; `save()`
verschluckt ENOSPC dauerhaft still; `render`-Karten werfen bei
None-Feldern.

**Performance:** `JsonStore.save()` blockiert den Event-Loop >50 ms ab
~3.000 economy-Nutzern; `words._save_store` bis 309 ms bei 100k Wörtern;
`schulden.mahn_tick` skaliert quadratisch (3,4 s bei 2.000 Paaren);
words-Index wächst ungedeckelt (4.000-Zeichen-Spam).

**Verhalten:** HANDLED-Durchrutscher würde `on_message` crashen (Kette
textlich prüfen); lotto/merchant-Panels werden per `protect_message` nie
wieder freigegeben (Leck im Schutz-Set); Fremd-DM-Weiterleitung verliert
Anhänge; `fun`-Regexe (`rate`/`bewerte`, `looks_offensive`) und
`media`-Redewendungen („bild dir nichts ein") kapern Alltagsdeutsch;
voicegags „Flo sounds gut …" öffnet das Soundboard; `@Flo schulden` zeigt
die Paar-Tafel mit dem **Bot** als Gegenüber.

### 1.6 Widerlegt

25 weitere Funde haben die gegnerische Reproduktion **nicht** überstanden
(z. B. „Slots zahlen an der Tageskappe vorbei" in der Menü-Variante,
diverse floaktie-Kauf-Grenzfälle, `parse_amount`-Präzision) und sind hier
bewusst nicht aufgeführt — wer sie doch angehen will, findet alle 92
Roh-Funde im Workflow-Journal des Testlaufs.

---

## 2. SoundCloud abspielen (Umsetzungsplan)

### Ist-Zustand — gemessen, nicht vermutet

```
"Flo spiel https://soundcloud.com/forss/flickermood"
   → parse_command liefert ('search', 'https://soundcloud.com/…')
   → _extract("ytsearch1:https://soundcloud.com/…")
   → YouTube-TEXTSUCHE nach der URL-Zeichenkette → falscher oder kein Treffer

"Flo https://soundcloud.com/forss/flickermood"   (nur der Link)
   → parse_command liefert None → Nachricht fällt an die KI durch

"Flo spiel https://soundcloud.com/forss/sets/soulhack"   (Playlist/"Set")
   → gleiche Fehlleitung in die YouTube-Suche
```

Ursache: die URL-Schleife in `parse_command` (music.py, um Zeile 1589) kennt
nur `open.spotify.com`, `youtube.com`/`youtu.be`. Alles andere fällt durch und
wird wie Freitext behandelt.

### Was zu bauen ist

Die gute Nachricht: **yt-dlp hat einen eingebauten SoundCloud-Extractor** —
ohne API-Key, ohne Login für öffentliche Tracks. `_extract(url)` kann einen
SoundCloud-Link **heute schon** auflösen; er kommt dort nur nie an. Der ganze
Umbau ist Erkennung + Routing, kein neuer Player-Code.

1. **Erkennung** (Modulkopf, neben den Spotify-Regexen):
   ```
   _SC_RE      = soundcloud.com / www.soundcloud.com / m.soundcloud.com / on.soundcloud.com
   _SC_SET_RE  = wie _SC_RE, Pfad enthält '/sets/'
   ```
   `on.soundcloud.com` sind Kurzlinks aus der App — **nicht selbst auflösen**,
   yt-dlp folgt dem Redirect von allein.

2. **Routing in `parse_command`** (in der URL-Schleife, vor dem Durchfallen):
   - Set-Link → neue Aktion `("sc_playlist", url)`
   - sonstiger SC-Link → ganz normal `("play", url)` — ab da läuft alles über
     den bestehenden `_extract`-Pfad (Track bekommt title/duration/thumbnail/
     stream_url wie bei YouTube; die FFmpeg-`-reconnect`-Härtung gilt mit).

3. **`sc_playlist`** — exakt nach dem Vorbild `_youtube_playlist` (Z. 1334):
   `extract_flat="in_playlist"`, `playlistend=MAX_QUEUE`, `ignoreerrors=True`,
   je Eintrag ein `_lazy_track(track_page_url, titel, …)`. SoundCloud liefert
   bei flat-Extraktion pro Eintrag die volle Track-URL im Feld `url`.
   **Ein** gemeinsamer Helfer `_flat_playlist(url)` für YouTube UND SoundCloud
   wäre die saubere OOP-Lösung (der YouTube-Sonderfall `watch?v=<id>`-Aufbau
   bleibt als kleiner Zweig darin).

4. **Kurzlink + Set-Zweifel:** ein `on.soundcloud.com`-Link kann auch ein Set
   sein — das sieht man erst nach dem Redirect. Lösung: als `play` starten;
   liefert `extract_info` ein `_type == "playlist"`, in den Set-Pfad wechseln
   (Eintrag in `_extract` prüfen statt raten).

5. **Fehlerfälle** (bewusst über die BESTEHENDE Behandlung laufen lassen):
   - privater Track / gelöscht / Geo-Sperre → `_extract` wirft → die normale
     „Track nicht ladbar"-Meldung greift, Queue läuft weiter.
   - SoundCloud-Go+-Titel liefern ohne Abo nur eine 30-s-Preview: erkennbar
     daran, dass die gelieferte Stream-Dauer ≈ 30 s ist, obwohl
     `info['duration']` größer ist → dann ehrlicher Hinweis („nur Vorschau —
     Titel ist Go+-exklusiv") statt kommentarlosem Abbruch nach 30 s.

6. **Tests** (ohne Netz): `parse_command`-Fälle (Track, Set, Kurzlink,
   m.soundcloud, URL mitten im Satz, Großschreibung), `sc_playlist`-Mechanik
   mit gefaktem yt-dlp-`extract_info`. **Kein neues Befehlswort nötig** —
   Links triggern über die URL-Erkennung, `cmdnorm.KNOWN` bleibt unberührt.

7. **README**: Musik-Abschnitt um SoundCloud ergänzen (Tracks, Sets,
   Kurzlinks; Go+-Einschränkung erwähnen).

---

## 3. Schulden-System 2.0 (Konzept — nur Logik, kein Code)

### Warum überhaupt

Der wunde Punkt des heutigen Systems steht in `record_pay` (schulden.py):
**jede** `Flo pay`-Zahlung erzeugt automatisch eine Forderung des Zahlers
gegen den Empfänger. Ein Geschenk, ein verlorener Wetteinsatz, eine geteilte
Rechnung — alles wird stillschweigend zur Schuld, ohne dass einer der beiden
das je so gemeint hat. Der Rest (Tilgung, Mahnung, Erlassen) ist solide
Buchführung auf diesem falschen Fundament.

### Grundsatz des Neuentwurfs

> **Eine Schuld entsteht nur noch durch Zustimmung — nie durch eine Zahlung
> allein.** Wer zahlt, schenkt. Wer leiht, leiht ausdrücklich.

### Die drei Wege, wie eine Schuld entsteht

1. **`Flo leih @wer 5k [Grund] [bis freitag]`** — der klassische Kredit.
   Flo postet eine Anfrage mit zwei Knöpfen; **erst wenn der Empfänger
   „Annehmen" drückt**, fließt das Geld UND der Posten entsteht. Lehnt er ab
   oder läuft die Anfrage ab (5 min), passiert gar nichts.
2. **`Flo pay @wer 5k als leihgabe`** — Zahlung, die der Zahler ausdrücklich
   als Leihgabe markiert. Auch hier: Bestätigungs-Knopf beim Empfänger,
   erst der Klick bucht Geld + Posten. Ohne Zusatz bleibt `pay` ein Geschenk
   (die Kreide-Tafel notiert es weiterhin fürs Handelsbuch, aber ohne
   Forderung).
3. **`Flo schuldschein @wer 5k [Grund]`** — Schuld OHNE Geldfluss („du
   schuldest mir noch vom Kino"). Der SCHULDNER muss per Knopf bestätigen —
   niemand kann einem Fremden per Befehl Schulden anhängen.

### Datenmodell (OOP)

Statt des Paar-Saldos (`pairs` mit einem `net`) werden **einzelne Posten**
geführt — nur so gibt es Fälligkeit, Grund und Historie je Schuld:

```
Posten (dataclass):
    id            laufende Nummer
    glaeubiger    User-ID
    schuldner     User-ID
    urspruenglich int   (nie verändert — für Anzeige "3k von 5k getilgt")
    offen         int   (>= 0; 0 = erledigt, bleibt für die Historie)
    grund         str   (max 100 Zeichen)
    entstanden    ts
    faellig       ts | 0    (0 = ohne Termin)
    status        offen | getilgt | erlassen | verfallen
    log           [(ts, art, betrag)]  gedeckelt wie heute (LOG_KEPT)

Schuldbuch (Klasse, Singleton wie überall):
    posten_von / posten_gegen / saldo(a, b)  — Saldo wird BERECHNET, nicht
    gespeichert (Summe der offenen Posten beider Richtungen)
```

Klassen: `Schuldbuch` (Aggregat + Persistenz), `Posten` (dataclass),
`Tilgungsplan` (verteilt eine Einnahme), `Mahnwesen` (Eskalation),
`Kreditwuerdigkeit` (Score). Befehls-Handler bleibt dünn und ruft nur diese an.

### Tilgung — fairer als heute

Heute: 20 % jeder echten Einnahme an den **größten** Gläubiger. Neu:

- 20 % jeder echten Einnahme (Tabu-Liste und `-rueck`-Ausnahme **unverändert
  übernehmen** — die ist getestet und richtig),
- aber **anteilig auf alle Gläubiger** nach offener Summe verteilt
  (Rundungsrest an den größten), Mindestbetrag 10 je Gläubiger, damit keine
  1-Coin-Überweisungen entstehen,
- innerhalb eines Gläubigers: **ältester Posten zuerst** (FIFO),
- überfällige Posten (faellig überschritten) bekommen **Vorrang** vor allen
  anderen: erst sie anteilig, dann der Rest.
- `Flo tilg @wer [betrag]` — freiwillige Sondertilgung bleibt jederzeit
  möglich (heute geht das nur über normales pay, das dann wieder eine
  Gegen-Forderung erzeugt — genau der Konstruktionsfehler).

### Kreditwürdigkeit (der „schlau"-Teil)

Ein Score 0–100 je Person, **nur aus dem eigenen Verhalten** berechnet:

```
Start 50.
+  pünktlich getilgter Posten (vor faellig bzw. < 14 Tage offen): +5
+  freiwillige Sondertilgung: +2
−  Posten überfällig: −10 (einmalig je Posten)
−  Posten verfallen/erlassen wegen Nichtzahlung: −15
Deckel 0/100, Verjährung: Einträge älter als 90 Tage zählen halb.
```

Wirkung (alles über guildcfg abschaltbar):
- **Leih-Limit** = der Kontostand des Gläubigers, unabhängig vom Score. Wer
  verleihen will, darf sein ganzes Geld verleihen (beim Schuldschein fließt
  keines, dort gilt die Grenze nicht).
- Anzeige im Profil-Lookup (`Flo check`) als Ampel (🟢/🟡/🔴) mit Zahl.
- Unter Score 20: neue Schuldscheine/Leihen gesperrt, bis getilgt wurde.

### Grenzen & Missbrauchsschutz

- nie gegen Bots, nie gegen sich selbst, Betrag ≥ 50 (unter Kleinkram lohnt
  die Buchführung nicht), je Paar max. 5 offene Posten, je Person max. 25.
- **Kein** Deckel auf die Gesamtschuld: man darf mehr schulden, als man
  besitzt. Gebremst wird über die Postenzahl, die Sperre unter Score 20, den
  Verfall und die Tilgungsautomatik.
- **Verfall statt Ewigkeit:** ein Posten ohne jede Bewegung seit 60 Tagen
  wird `verfallen` (Gläubiger bekommt eine letzte DM 7 Tage vorher). Kein
  ewiges Druckmittel, die Historie bleibt sichtbar.
- **Privatinsolvenz** (`Flo insolvenz`, mit Rückfrage): zahlt 50 % des
  aktuellen Vermögens anteilig an alle Gläubiger, alle Restposten werden
  `erlassen`, Score fällt auf 10, 14 Tage keine neuen Schulden. Ein ehrlicher
  Neuanfang statt Konto-Wechsel-Tricks.

### Mahnwesen — Eskalation statt Dauer-DM

1. Fällig überschritten → freundliche DM (wie heute, 1×/Tag, ab 1000).
2. 7 Tage überfällig → DM an BEIDE mit Tilgungsvorschlag.
3. 14 Tage überfällig → auf Wunsch des Servers (guildcfg-Schalter
   `schulden_pranger`, Standard AUS) eine neutrale Notiz im Ansage-Kanal.
   Niemals Beträge nennen, nur „X hat einen überfälligen Posten bei Y".

### Migration

`pairs` → Posten: je Paar mit `net != 0` **ein** Posten mit
`offen = |net|`, Grund `"Übernahme alte Kreide-Tafel"`, faellig 0, Score
aller Nutzer startet bei 50. Volumen/Statistik (`vol`, `n`) in ein
`archiv`-Feld übernehmen, nichts löschen. Ein `economy_reset` setzt wie
bisher auch das Schuldbuch zurück (Reset-Skript um `posten` erweitern —
NUR die Datei-Liste, Skript nie auf echten Daten laufen lassen).

### Was bewusst so bleibt

- Tabu-Liste + `-rueck`/`-rueckgabe`-Ausnahme der Tilgung (getestet, korrekt)
- „Buchführung wirft nie" — jede Schulden-Operation bleibt nicht-fatal
- Mahn-Abstand/Schwelle per .env, neue Schalter zusätzlich je Server (guildcfg)

---

## 4. Enterprise-Ausbau & OOP (Architektur-Aufträge)

Ziel: der Bot läuft auf **vielen Servern gleichzeitig**, jeder Server kann
alles Wesentliche selbst einstellen, und Änderungen wie „anderer Name/Präfix"
sind an EINER Stelle möglich. Die Mehr-Server-Basis existiert seit `e1c99a1`
(guildcfg, features je Server, Loops über `self.guilds`) — hier ist, was fehlt.

### 4a. Präfix/Ansprache je Server — die eine große OOP-Baustelle

Gemessen: **21 Module** cachen sich beim Start `self._bot_name =
os.getenv("BOT_NAME")`, und `bot.py` baut `_TRIGGER_RE = ai.trigger_re()`
**einmal beim Import**. Ein Präfix je Server ist damit heute unmöglich.

Plan (in dieser Reihenfolge):
1. `guildcfg`: neuer Schlüssel `praefix` (Typ text, Standard = BOT_NAME aus
   der .env, 2–32 Zeichen, keine Leerzeichen). Aliasse (`BOT_ALIASES`)
   bleiben global.
2. `ai.py` wird die **einzige Autorität** für Ansprache: `bot_name(gid=0)`,
   `names(gid)`, `lead_re(gid)`, `trigger_re(gid)` — je Guild kompiliert und
   gecacht (dict gid→regex, Invalidierung wenn guildcfg `praefix` ändert;
   guildcfg ruft dafür einen kleinen Hook `ai.praefix_geaendert(gid)`).
3. `ai.strip_lead(text)` → `ai.strip_lead(text, gid)`; bot.py reicht die
   Guild-ID durch (die Handler haben die Message ohnehin).
4. Die 21 `self._bot_name`-Caches werden **ersatzlos gestrichen**; überall,
   wo der Name in einer Antwort steht, stattdessen `ai.bot_name(gid)` zur
   Laufzeit. Das ist mechanisch, aber groß — je Modul ein Commit, Tests nach
   jedem.
5. `bot.py`: `_TRIGGER_RE` wird eine Funktion `self._angesprochen(message)`,
   die den Guild-Regex nutzt; DMs nehmen den globalen Namen.
6. `cmdnorm` ist präfix-neutral (arbeitet auf dem Text NACH strip_lead) —
   keine Änderung nötig, aber ein Test, der das festhält.

### 4b. Web-Panel: mehr Server-/Bot-Einstellungen

Die Server-Seite (Karte → Einstellungen) speist sich automatisch aus dem
guildcfg-Katalog — **jeder neue Katalog-Eintrag erscheint dort ohne
Panel-Arbeit**. Also: Einstellungen als Katalog-Einträge nachziehen:

| neuer Schlüssel | Typ | wofür |
|---|---|---|
| `praefix` | text | siehe 4a |
| `sprache_bayrisch` | — existiert (`bayern`) | — |
| `levelup_ansagen` | an_aus | Level-Up-Karten ganz aus je Server |
| `willkommen_channel` + `willkommen_text` | channel + text | Begrüßung neuer Mitglieder (Text mit `{name}`-Platzhalter) |
| `daily_erinnerung` | an_aus | Flo erinnert im Ansage-Kanal an den Daily |
| `schulden_pranger` | an_aus | siehe Kapitel 3 |
| `casino_max_einsatz` | zahl | Obergrenze je Server |
| `musik_max_queue` | zahl | Queue-Deckel je Server |

Dazu eine **Bot-Seite** (global, nur Besitzer) im Panel:
- BOT_NAME/Aliasse anzeigen (Änderung = .env, das Panel sagt das ehrlich),
- Status-Weisheiten bearbeiten (neuer JsonStore `weisheiten.json`,
  Fallback auf die eingebaute Liste),
- Daten-Sicherung: Knopf „Backup herunterladen" (ZIP aller data/*.json —
  reine Leseoperation),
- Audit-Log: jede schreibende Panel-Aktion (wer nicht bekannt — dann
  Zeitpunkt+Aktion+Ziel) in `panel_log.json`, letzte 200 Einträge, eigene
  Panel-Ansicht. Kein Login-Thema — nur Nachvollziehbarkeit.

### 4c. Mehr-Server-Härtung (der Rest des Weges)

- **words.json ist global**: der Wörter-Zähler mischt heute alle Server.
  Umbau auf `words`-Struktur je Guild (`{"guilds": {gid: {…}}}`) mit
  Migration (Bestand → Haupt-Guild). Gleiche Prüfung für `games.json`
  (Counting je Kanal ist ok, Tageskappen sind global — gewollt, da eine
  Wirtschaft).
- **Eine Wirtschaft bleibt Absicht** (ein Coin-Topf, eine Aktie). Das ist
  dokumentierte Entscheidung; ein optionales `wirtschaft_getrennt` wäre ein
  eigenes Großprojekt (User-Keying je Guild + Migration) — NICHT nebenbei.
- **Skalierungs-Grenzen dokumentieren**: ab ~2.500 Servern verlangt Discord
  Sharding (`discord.AutoShardedClient`) — der Wechsel ist eine Zeile plus
  Loop-Audit (alle `self.guilds`-Loops bleiben gültig). Member-Intent bleibt
  aus; alle Namens-/Avatar-Wege laufen bereits über die REST-Fallback-Ketten.
- **store.py-Durchsatz**: save() serialisiert synchron im Event-Loop. Für
  große Stores (words bei vielen Servern) Messwert erheben und ggf.
  `to_thread`-Serialisierung mit Snapshot-Kopie (Vorsicht: die heutige
  Synchronität ist eine bewusste Konsistenz-Entscheidung — erst messen,
  dann entscheiden).
- **on_guild_join-Begrüßung**: kurze DM an den Einlader/Owner mit
  `Flo einstellungen`-Hinweis (heute nur Log-Zeile).

### 4d. OOP-Checkliste für JEDES neue Modul (Lehren aus dieser Runde)

1. Klasse + `instance`-Singleton + Modul-Aliase am Dateiende — keine nackten
   Modulfunktionen mit Zustand.
2. Keine Typannotationen (außer @dataclass), deutsche Kommentare, Zustand nur
   über `store.JsonStore`.
3. **Jedes neue Befehlswort in `cmdnorm.KNOWN` eintragen** — sonst korrigiert
   die Tippfehler-Suche es auf einen fremden Befehl. Diese Runde hätte
   `Flo banner @wer` die Person **gebannt** (banner→banne→ban), weil das
   fehlte. Ein Test (`test_profil_befehle_kollidieren_nicht`) prüft das
   Muster — für neue Module erweitern.
4. Kollisionscheck gegen die Handler-Kette: Wer beansprucht das Erstwort
   vorher? (`bild` gehörte media.py; `profil` kollidierte mit admin.py.)
5. `setup()` übersteht eine von Hand kaputte JSON-Datei (Bot muss IMMER
   hochkommen).
6. Auskunfts-Funktionen für andere Module (`kurzprofil`, `warns_of`, …)
   verändern NIE den Store.
7. Tests in `test_games_logic.py` (eigener Runner), DATA_DIR-Isolation ist
   dort global gelöst — niemals umgehen.
8. Feature-Key: CATALOG (features.py) + FEATURE_LOADED (bot.py) + Handler-
   Kette müssen denselben Schlüssel führen — Test besteht schon, schlägt
   sonst an.

---

## 5. Arbeitsregeln für die Umsetzung

- `economy_reset` niemals auf echten Daten ausführen (nur Scratch/DRY_RUN).
- Web-Panel läuft bewusst ohne Login — Thema ist entschieden, nicht anfassen.
- Nach jedem Kapitel: beide Suiten + `bot.py --check`; bei Aktien-Berührung
  zusätzlich `tools_aktien_sim.py` (8 Kriterien).
- Push auf `claude/flo-bot-remove-dbd-nzdpj9` **und** `main`.
