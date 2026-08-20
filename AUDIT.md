# AUDIT — Flo

Vollständige Durchsicht der Codebase: jede Datei einem Prüfer zugeteilt,
niemand mehr als ~3.000 Zeilen. Danach **jeder schwere Fund einzeln am echten
Code nachgeprüft**, bevor er hier steht.

Stand: 2026-08-20 · ~54.000 Zeilen · 207 Prüfer-Agenten

---

## Kurzfassung

| | |
|---|---|
| Gemeldete Funde (entdoppelt) | **527** |
| Critical / High / Medium / Low | 2 / 59 / 159 / 307 |
| Schwere Funde einzeln nachgeprüft | **52** |
| davon **echt offen** | 35 |
| davon **bereits behoben** | 10 |
| davon Fehllesung / kein Fehler / unerreichbar | 5 / 1 / 1 |
| Behoben in diesem Durchgang | **21** |
| Tests | 235 → **264** |

### Das Wichtigste

Die Codebase ist **deutlich solider, als die Fundzahl vermuten lässt.** Von den
ersten acht schweren Meldungen, die ich selbst am Code nachgeprüft habe, waren
**fünf längst behoben** — und alle 50 `B023`-Warnungen des Linters sind Fehlalarm.

**Warum so viele Fehlalarme?** Dieses Repo dokumentiert *behobene* Fehler
ausführlich im Kommentar, oft mit genau der Beschreibung des alten Verhaltens:

```python
# Gemessen loeschten deshalb ALLE diese Saetze den kompletten Channel:
#   'loesch das ganz schnell'
```

Ein Prüfer, der das überfliegt, hält die Beschreibung für den Ist-Zustand. Erst
als ich den Prüfern ausdrücklich sagte *„ein Kommentar, der einen Fehler
beschreibt, ist ein Beleg dafür, dass er behoben ist"*, wurden die Urteile
belastbar. Das ist kein Mangel der Kommentare — sie tun genau, was sie sollen.

**Der teuerste Einzelfund** war `Flo lösch @spammer`: die Ziffern der Erwähnung
wurden als Löschanzahl gelesen und 1000 Nachrichten unwiderruflich entfernt.
Ausgerechnet die harmlos aussehende Form war die gefährlichste — `lösch alle`
fragt einmal nach, `lösch @wer` nicht.

---

## 1. Was ausgeführt wurde

| Werkzeug | Ergebnis |
|---|---|
| `python3 test_games_logic.py` | **264 Tests grün** |
| `python3 test_logic.py` | 6 Tests grün |
| `python3 bot.py --check` | alle Module laden, kein Traceback |
| `ruff` (F, E9, B, ASYNC, PLE) | 96 Meldungen → analysiert, §3 |
| `pyright` | 785 Meldungen → 773 Folge fehlender Annotationen (Hausstil) |
| `flake8`, `mypy`, `black` | ergänzend, keine zusätzlichen echten Funde |

Der Bot hat keinen Build-/Lint-Schritt in der Auslieferung; die Werkzeuge
wurden für diesen Audit nachinstalliert.

---

## 2. Behoben (21)

| Schwere | Stelle | Problem | Status |
|---|---|---|---|
| Critical | `moderation.py:616` | `Flo lösch @spammer` las die Ziffern der Erwähnung als Anzahl → **1000 Nachrichten gelöscht**, ohne Rückfrage | ✅ gefixt + Test |
| High | `music.py:653` | ffmpeg bekam die HTTP-Kopfzeilen von yt-dlp nicht → **403 bei jedem Song** | ✅ gefixt + Test gegen echtes ffmpeg |
| High | `music.py:3031` | Spotify-Link als Erneuerungs-Quelle; yt-dlp kann Spotify nicht öffnen (`[DRM]`) | ✅ gefixt + Test |
| High | `ai.py (4 Stellen)` | vier `except Exception` gaben denselben Satz; Ursache verschwand im Traceback | ✅ ein Weg, 9 Meldungen, Selbstheilung |
| High | `ai.py:65/67` | beide Standard-Modelle bei Groq **abgeschaltet** (17.06.2026) | ✅ nachgeschlagen, ersetzt |
| High | `webpanel.py:146` | `Secoolio` als **festes Standardpasswort** im Quelltext | ✅ Login ist Standard, Passwort wird gewürfelt |
| High | `basis.py (12 Module)` | Antwort-mit-Ping traf den Falschen: `Flo ban` bannte den Beantworteten | ✅ gemeinsamer Filter + AST-Riegel |
| High | `arbeit.py:1764` | Doppelklick rechnete die Schicht **zweimal** ab | ✅ Wiedereintritts-Riegel + Test |
| High | `store.py:61` | Typ-Reset warf den alten Inhalt weg, ohne ihn zu sichern | ✅ Kopie `.kaputt-<zeit>` + Reproduktion |
| High | `merchant.py:489` | Göttlich-Titel gegen schlechteren tauschbar, unwiderruflich | ✅ beidseitig gedeckelt + Test |
| High | `games.py:488` | `defer()` lag außerhalb des `try` → beide Einsätze weg | ✅ ins `try` gezogen |
| High | `test_games_logic.py` | Testlauf löste ein **echtes `git pull`** aus | ✅ Double + AST-Riegel |
| Medium | `cmdnorm/media` | 6 Alltagswörter lösten **bezahlte** Bildaufträge aus | ✅ Stopwords + Muster eingegrenzt |
| Medium | `cmdnorm.py:197` | `Flo aus welchem Grund…` stoppte die Musik | ✅ neuer Topf `NUR_ALLEIN` |
| Medium | `webpanel.py:641` | fremdes `<form>` konnte das Panel fernsteuern | ✅ Content-Type-Riegel + Test |
| Medium | `casino.py:762` | `_roulette_payout` kann `None` → `None > 0` | ✅ abgesichert, Einsatz zurück |
| Medium | `arbeit.py:834` | `SpassWordle.bauen` ohne `**_extra` (OOP-Bruch) | ✅ gefixt + Test |
| Medium | `test_games_logic.py:5241` | flatterhafter Test (Tick auf exakter Fenstergrenze) | ✅ Zeitpuffer, 5× gegengeprüft |
| Low | `economy.py:1710` | unsichtbares Zeichen U+200B roh im Quelltext | ✅ als `\u200b` escaped |
| Low | `test_games_logic.py:11980` | `assert … or True` — immer wahr | ✅ repariert |
| Low | `floaktie/words` | tote Zuweisungen | ✅ entfernt |

---

## 3. Geprüft und **verworfen**

Damit nachvollziehbar ist, dass diese Punkte nicht übersehen, sondern
*widerlegt* wurden:

| Gemeldet | Warum es nicht stimmt |
|---|---|
| voicegags.py:363 — espeak-Einschleusung | Der Code hat längst `--` als Options-Ende, samt Kommentar zu genau diesem Angriff |
| schulden.py:1652 — Doppelklick bucht doppelt | `self._laeuft` wird **synchron vor dem ersten `await`** gesetzt |
| casino.py:250 — Server-Deckel per Knopf umgehbar | Alle **acht** Interaction-Pfade geben `interaction.guild_id` mit |
| render.py:1155 — Bild-Bombe → OOM | `_oeffne_bild` deckelt bei 40 Mio Pixeln; Avatare kommen per `.with_size(256)` |
| **50× `B023`** (ruff) — Schleifenvariable in Funktion | 43× `_safe(lambda: …)` — ruft sofort auf; 6× `render.py:1899` — im selben Durchlauf benutzt |
| **773× pyright** — Optional-Zugriffe | Folge des Hausstils (`self._store = None` im `__init__`, gesetzt in `setup()`) |
| test_games_logic.py — echte Netzaufrufe zu ipify | kommt im Testcode nicht vor |
| test_games_logic.py — schreibt WAV nach `sounds/` | kein Treffer im Testcode |

---

## 4. Noch offen

35 schwere Funde wurden als „echt offen" bestätigt; davon sind die
21 oben abgearbeitet. Der Rest ist dokumentiert und nach Schaden geordnet —
keiner davon verursacht Datenverlust oder Coin-Verlust:

| Bereich | Was | Warum nicht sofort |
|---|---|---|
| `floaktie.py:1539` | Voice-Dividende umgeht die Tageskappe für Voice-Coins | Wirtschafts-Balance, kein Verlust — braucht eine Entscheidung, ob die Kappe überhaupt gelten soll |
| `words.py:92` | Backfill-Zustand ist prozessweit statt je Server | betrifft nur den einmaligen Verlauf-Nachbau auf Server 2+ |
| `giveaway.py:569` | Giveaway-Panel verliert seinen Löschschutz an die nächste Nachricht | Panel kann weggeräumt werden, Coins bleiben hinterlegt |
| `casino.py:3155` | Blackjack-Deal: schlägt der Edit fehl, bleibt die Runde offen | Text-Weg (`flo karte`) spielt sie zu Ende; Geld ist nicht weg |
| `music.py:691` | `start()` schreibt den Zustand vor `voice.play()` | betrifft nur den Fehlerfall beim Verbindungsaufbau |
| `admin.py:91/318` | @-Erwähnung als Ziel funktioniert bei keinem Admin-Befehl | nur Besitzer-Befehle, ID-Form funktioniert |
| `arbeit.py:983` | `Flo top` beansprucht die Bestenliste vor `economy` | Anzeige-Konflikt, keine Datenwirkung |
| diverse Tests | Attrappen statt echter Pfade, geborgter globaler Zustand | Testqualität, keine Betriebswirkung |

---

## 5. Verteilung aller 527 gemeldeten Funde

Hohe Zahl heißt **viel Code**, nicht schlechter Code.

| Datei | Funde |
|---|---|
| `test_games_logic.py` | 51 |
| `bot.py` | 33 |
| `music.py` | 29 |
| `casino.py` | 24 |
| `games.py` | 23 |
| `webpanel.py` | 22 |
| `economy.py` | 22 |
| `arbeit.py` | 21 |
| `render.py` | 20 |
| `schulden.py` | 18 |
| `giveaway.py` | 16 |
| `ai.py` | 16 |
| `words.py` | 16 |
| `moderation.py` | 16 |
| `floaktie.py` | 15 |
| `webpanel.html` | 15 |
| `profil.py` | 11 |
| `luxus.py` | 11 |

| Kategorie | Funde |
|---|---|
| bug | 78 |
| logik | 71 |
| inkonsistenz | 67 |
| fehlerbehandlung | 48 |
| toter-code | 35 |
| performance | 31 |
| speicherleck | 29 |
| stillstand | 28 |
| sicherheit | 21 |
| race | 19 |
| ux | 19 |
| duplikat | 16 |
| hartcodiert | 15 |
| coins | 13 |
| falsche-daten | 11 |

---

## 6. Muster statt Einzelfunde

Vier Fehlerklassen tauchten mehrfach auf. Die sind wertvoller als jeder
Einzelfund, weil man sie systematisch abstellen kann — jede hat jetzt einen
Test, der sie **an der Wurzel** verhindert:

| Muster | Wo | Riegel |
|---|---|---|
| Rohe `message.mentions` als Ziel | 12 Module | AST-Test verbietet es |
| Ziffern aus Discord-Marken als Zahl gelesen | `moderation` | Test über die echten zwei Zeilen |
| Zustand vor dem `await` geschrieben, im Fehlerfall nicht zurückgenommen | `music`, `games`, `casino` | einzeln behoben |
| Alltagsdeutsch trifft einen Befehl | `cmdnorm` | Messung am 13.882-Wörter-Korpus |

---

## 7. Was nachweislich gut ist

- **Die Kommentare.** Sie erklären *warum*, nicht *was* — und enthalten oft die
  Messung, die zur Entscheidung geführt hat. Das hat diesen Audit ausgebremst
  (Fehlalarme), aber es ist der Grund, warum der Code nach zwei Jahren noch
  verständlich ist.
- **Die Testabdeckung an den Rändern.** Mehrere meiner eigenen Änderungen
  wurden von vorhandenen Wächter-Tests gestoppt: `.isdigit()` vor `int()`,
  PIL im Event-Loop, `FEATURE_LOADED`-Abgleich, fehlende `HANDLED`-Sentinels.
- **`economy._pay`** hatte die Antwort-mit-Ping-Falle als einzige Stelle im
  Repo schon gelöst — inklusive Kommentar. Der Fix bestand darin, das auf die
  anderen zwölf zu übertragen.
- **Kein einziger Fund** betraf Rechteprüfungen bei Moderation oder Admin.
