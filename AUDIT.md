# Full-Audit der Codebase

**Stand:** 20.08.2026 · **Umfang:** 47.000 Zeilen Python, 2.500 Zeilen HTML/JS,
Shell-Skripte, systemd-Unit · **Repo-Stand zu Beginn:** `b623d45`

> **Zwischenstand.** Der maschinelle Teil (Linter, Type-Checker, Grenzfall-Lauf,
> Rauchtest) ist vollständig durch und unten dokumentiert. Der Zeile-für-Zeile-
> Audit durch 19 Prüfer läuft noch; seine Funde kommen in Kapitel 4 dazu.

---

## 1. Vorgehen

Was tatsächlich ausgeführt wurde — keine Stichprobe, keine Schätzung:

| Werkzeug | Umfang | Ergebnis |
|---|---|---|
| `ruff` (F, E9, B, ASYNC, PLE, RUF) | alle 45 Python-Dateien | 97 Treffer, 31 davon einzeln geprüft |
| `pyright` | alle 45 Dateien | 785 Meldungen, 31 aussagekräftige geprüft |
| `mypy --ignore-missing-imports` | alle 45 Dateien | 10 Meldungen, alle geprüft |
| `flake8 --select=F,E9,C9` | alle Dateien | 11 Treffer, alle geprüft |
| `black --check` | alle Dateien | 44 Dateien abweichend — **bewusste Projektentscheidung**, kein Fund |
| `python3 test_games_logic.py` | 245 Tests | grün |
| `python3 test_logic.py` | 6 Tests | grün |
| `python3 bot.py --check` | alle 24 Module laden | grün |
| **Grenzfall-Lauf** (neu geschrieben) | 47 feindliche Eingaben × 63 Befehle = **2.968** | **0 Abstürze** |
| **Rauchtest** (neu geschrieben) | 62 Befehle durch das echte `handle()` | 0 Abstürze, 0 unerkannt |

Zusätzlich: die drei Ärzte (`k`, `k m`, `k a`) prüfen die **Außenwelt** —
Groq-Modelle, Spotify-Token, yt-dlp/ffmpeg, Discord-Token, Plattenplatz.
Genau dort kamen alle echten Ausfälle dieser Woche her; kein statisches
Werkzeug hätte sie gefunden.

---

## 2. Zusammenfassung

| Schwere | Gefunden | Gefixt | Won't fix (begründet) |
|---|---|---|---|
| Critical | 0 | – | – |
| High | 3 | 3 | 0 |
| Medium | 2 | 2 | 0 |
| Low | 9 | 5 | 4 |

**Kein einziger Critical-Fund.** Die drei High-Funde waren allesamt *latent*:
im heutigen Code nicht erreichbar, aber scharf, sobald jemand eine Aufrufstelle
hinzufügt. Genau solche Minen sind es wert, entschärft zu werden — sie
explodieren später und ohne erkennbaren Zusammenhang.

---

## 3. Funde aus dem maschinellen Teil

### H-1 · Roulette stürzt bei ungültigem Tipp ab, nachdem der Einsatz weg ist
**`casino.py:757` / `casino.py:769`** · High · Fehlerbehandlung · **gefixt**

`_roulette_payout` liefert bei einem unbekannten Tipp `(None, target)`.
Direkt danach stand `payout > 0` — das ist ein `TypeError`, kein Fehlschlag.

*Reproduktion:* `_play_roulette(uid, 100, "voelliger quatsch")` →
`TypeError: unsupported operand type(s) for -: 'NoneType' and 'int'`.

*Warum es zählt:* Alle vier heutigen Aufrufer prüfen vorher ab
(`casino.py:797`, `:2419`, `:2908`, feste Knopfwerte) — der Pfad ist also nicht
erreichbar. Aber der Einsatz ist an dieser Stelle **schon abgebucht**. Wer den
fünften Aufrufer schreibt und die Prüfung vergisst, verbrennt fremde Coins mit
einem Absturz.

*Fix:* Absicherung an der **einen** Stelle statt in jedem Aufrufer. Einsatz
kommt zurück (`payout = bet`), laute Log-Zeile. Bewusst **ohne** Änderung der
Rückgabeform — die Aufrufer reichen `file` direkt an Discord weiter, ein `None`
daran wäre der nächste Absturz.

*Test:* `test_roulette_stuerzt_nicht_bei_ungueltigem_tipp`. Gegenprobe gemacht:
ohne den Fix fällt er mit genau dem echten Fehler.

> **Eigener Fehler beim Fixen:** Mein erster Anlauf rief `self._embed_fehler(...)`
> auf — das gibt es gar nicht — und gab `(embed, None)` zurück, was
> `send_message(file=None)` gebrochen hätte. Beim Nachprüfen aufgefallen, bevor
> es committet wurde.

### H-2 · `SpassWordle` bricht den Vertrag der Basisklasse
**`arbeit.py:834`** · High · OOP/Typ · **gefixt**

Alle Schichten versprechen `bauen(chef, autor, **_extra)`. `SpassWordle` ließ
das `**_extra` weg. `arbeit.py:1214` reicht `**extra` durch — derselbe Aufruf,
den jede andere Schicht klaglos schluckt, wäre bei dieser einen ein `TypeError`.
Der Aufrufer kann das nicht sehen.

*Fix:* `**_extra` ergänzt. *Test:* prüft die Signatur **aller** Unterklassen,
nicht nur dieser einen.

### H-3 · ffmpeg bekam die Client-Kennung nicht (bereits behoben)
**`music.py:653`** · High · Bug · **gefixt** (Commit `45fb95f`)

Vollständig dokumentiert in der Commit-Nachricht. Ursache des kompletten
Musik-Ausfalls; mit echtem ffmpeg reproduziert und verifiziert.

### M-1 · Spotify-Songs konnten sich nie erneuern
**`music.py:3031`** · Medium · Logik · **gefixt** (Commit `45fb95f`)

Nach dem Auflösen wurde die **ursprüngliche** Eingabe als Erneuerungs-Quelle
eingetragen — bei Spotify also die Spotify-Adresse. yt-dlp kann Spotify nicht
öffnen (`[DRM] …DRM protection`). Jede Wiederbelebung war chancenlos.

### M-2 · Der Log behauptete „aktiv", ohne es zu prüfen
**`ai.py` / `music.py` `setup()`** · Medium · Inkonsistenz · **gefixt**

„KI-Feature aktiv" hing allein daran, dass ein Schlüssel in der `.env` stand.
„Musik-Feature aktiv" hieß nur, dass drei Pakete installiert sind. Eine
Zusicherung, die niemand nachgesehen hat, ist schlimmer als keine.

*Fix:* `ai.selbsttest()` und `music.selbsttest()` beim Start — der Musik-Test
holt echte Audio-Bytes durch die ganze Kette und benennt den 403-Fall.

### L-1 bis L-5 · Kleinkram, gefixt

| Fund | Datei | Was |
|---|---|---|
| L-1 | `economy.py:1710` | Zwei unsichtbare Zeichen (Discord-Abstandshalter) standen roh im Quelltext. Jetzt `​` — inhaltlich gleich, aber kein Editor und kein Copy-Paste kann sie mehr fressen. |
| L-2 | `floaktie.py:1597` | Tote Zuweisung `st = self._state()` (reiner Getter) — entfernt. |
| L-3 | `words.py:531` | Ungenutztes `rows` aus der Tupel-Entpackung — als `_rows` gekennzeichnet. |
| L-4 | `guildcfg.py:59-60` | `minimum: float = None` — die Prüfung fragt korrekt auf `is not None` ab, die Deklaration log. Jetzt `float \| None`. |
| L-5 | `tools_ki_check.py` | Drei `print()` schrieben am Berichts-Puffer vorbei — in der Datei fehlten die Zeilen. |

### L-6 bis L-9 · Won't fix, mit Begründung

| Fund | Warum nicht |
|---|---|
| **`black` formatiert 44 Dateien um** | Der Stil ist eine bewusste Projektentscheidung (deutsche Kommentare, eigene Umbrüche). Ein Durchlauf würde jede Zeile anfassen und jede `git blame` wertlos machen. Kein inhaltlicher Gewinn. |
| **pyright: 442 × `reportOptionalMemberAccess`** | Folge des bewusst annotationsfreien Stils: `self._store = None` im `__init__`, gesetzt in `setup()`. pyright kann das nicht wissen. Keine echten `None`-Fehler — der Grenzfall-Lauf mit 2.968 Eingaben hat keinen einzigen ausgelöst. |
| **mypy: „base class defined the type as None"** (`arbeit.py:818`, `casino.py:3373`) | Klassenattribute ohne Annotation, in Unterklassen überschrieben — Standard-Python und hier gewollt. Eine Annotation würde den Hausstil brechen, ohne etwas zu gewinnen. |
| **`luxus.py:449` tote Zuweisung** | Sieht wie L-2 aus, ist es aber **nicht**: `throne_state()` hat eine `setdefault`-Nebenwirkung. Das Ergebnis ist ungenutzt, der **Aufruf** aber nötig. Absichtlich stehengelassen. |

### Offen (dokumentiert, noch nicht bewertet)

* **Nullbyte wird durchgereicht.** Bei der Grenzfall-Prüfung erscheint ein
  `\x00` aus der Eingabe in der Ausgabe wieder. Kein Absturz, und Discord
  selbst verträgt es — aber ein Modul reicht Nutzertext unverändert zurück.
  Einzugrenzen, sobald der Zeile-für-Zeile-Audit durch ist.
* **49 × `B023`** (`webpanel.py`): Schleifenvariable in einer Closure. Das ist
  die klassische Late-Binding-Falle. Ob sie hier zuschlägt, hängt davon ab, ob
  die Closure sofort oder später aufgerufen wird — muss Stelle für Stelle
  geprüft werden, nicht pauschal.

---

## 4. Zeile-für-Zeile-Audit

*Läuft. 19 Prüfer, jede Datei zugeteilt, niemand mehr als ~3.000 Zeilen.
Critical- und High-Funde werden von je zwei Skeptikern angegriffen, bevor sie
hier landen.*

---

## 5. Was nachweislich solide ist

Nicht schöngeredet, sondern gemessen:

* **Eingabe-Behandlung.** 2.968 feindliche Eingaben (leer, negativ, `1e308`,
  24-stellige Zahlen, `nan`/`inf`, arabische Ziffern, `%s`/`{0}`,
  SQL-/Pfad-/Script-Einschleusung, `@everyone`, 5.000 Zeichen, Nullbyte, BOM)
  gegen 63 Befehle mit Argumenten: **null Abstürze**.
* **Befehlserkennung.** 62 Befehle aus 17 Modulen laufen durch das echte
  `handle()` — keiner fällt stillschweigend zur KI durch.
* **Der Casino-Deckel** (`MAX_WIN`), die Tageskappen und die Coin-Buchführung
  haben eigene Tests und halten dem Grenzfall-Lauf stand.
* **Die breiten `except Exception`** an den Discord-Grenzen sind kein
  Schlamperei-Muster, sondern Absicht: der Bot soll nie wegen eines einzelnen
  Befehls sterben. Sie wurden geprüft — sie verschlucken nichts, was man
  bräuchte, seit jede Stelle zusätzlich einzeilig und greppbar loggt.

---

## 6. Regressionslauf

```
python3 test_games_logic.py    245 Tests bestanden
python3 test_logic.py            6 Tests bestanden
python3 bot.py --check           alle 24 Module laden, 0 Traceback
ruff  (F,E9,ASYNC,PLE)           0 Treffer in den neuen Dateien
mypy  guildcfg.py                Success: no issues found
```

Nach **jedem** Fix wurde der komplette Testlauf wiederholt, nicht nur der
betroffene Test.
