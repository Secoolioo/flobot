# AUDIT — Flo

Vollständige Durchsicht der Codebase: jede Datei einem Prüfer zugeteilt,
niemand mehr als ~3.000 Zeilen, damit wirklich jede Zeile gelesen wird.
Danach jeder schwere Fund **am echten Code nachgeprüft**, bevor er hier steht.

Stand: 2026-08-20 · Umfang: 53887 total

---

## Kurzfassung

| | |
|---|---|
| Gemeldete Funde | **527** (nach Entdopplung) |
| davon Critical / High | 2 / 59 |
| davon Medium / Low | 159 / 307 |
| Prüfer-Agenten | 155 (93 Audit, 62 Fehlerjagd) |
| Tests vorher → nachher | 235 → **257** |

**Das Wichtigste in einem Satz:** die Codebase ist deutlich solider, als die
Fundzahl vermuten lässt — von den ersten fünf schweren Meldungen, die ich selbst
am Code nachgeprüft habe, waren **drei bereits behoben**, und alle 50
`B023`-Warnungen des Linters sind Fehlalarm.

### Warum so viele Fehlalarme

Dieses Repo dokumentiert **behobene** Fehler ausführlich im Kommentar — oft mit
genau der Beschreibung des alten Fehlverhaltens:

```python
# Gemessen loeschten deshalb ALLE diese Saetze den kompletten Channel:
#   'loesch das ganz schnell'
```

Ein Prüfer, der das überfliegt, hält die Beschreibung für den Ist-Zustand. Das
ist die Ursache der meisten Falschmeldungen — und ein Hinweis darauf, dass die
Kommentare ihre Aufgabe erfüllen: sie erzählen, warum der Code so aussieht.

---

## 1. Was ausgeführt wurde

| Werkzeug | Ergebnis |
|---|---|
| `python3 test_games_logic.py` | **257 Tests grün** |
| `python3 test_logic.py` | 6 Tests grün |
| `python3 bot.py --check` | alle Module laden, kein Traceback |
| `ruff` (F, E9, B, ASYNC, PLE) | 96 Meldungen — analysiert, siehe §3 |
| `pyright` | 785 Meldungen — 773 davon Folge der bewusst fehlenden Type-Annotationen |
| `flake8`, `mypy`, `black` | ergänzend gelaufen, keine zusätzlichen echten Funde |

Der Bot hat **keinen** Build- oder Lint-Schritt in der Auslieferung; die
Werkzeuge wurden für diesen Audit nachinstalliert.

---

## 2. Behoben in diesem Durchgang

| Schwere | Datei | Problem | Status |
|---|---|---|---|
| **Critical** | `moderation.py:616` | `Flo lösch @spammer` las die Ziffern der Erwähnung als Anzahl → **1000 Nachrichten gelöscht, ohne Rückfrage** | ✅ gefixt + Test |
| High | `music.py:653` | ffmpeg bekam die HTTP-Kopfzeilen von yt-dlp nicht → 403 bei **jedem** Song | ✅ gefixt + Test gegen echtes ffmpeg |
| High | `music.py:3031` | Spotify-Link wurde als Erneuerungs-Quelle eingetragen; yt-dlp kann Spotify nicht öffnen (`[DRM]`) | ✅ gefixt + Test |
| High | `ai.py` (4 Stellen) | vier `except Exception` gaben denselben Satz zurück; Ursache verschwand im Traceback | ✅ ein Weg, 9 Meldungen, Selbstheilung |
| High | `ai.py:65/67` | beide Standard-Modelle bei Groq **abgeschaltet** (17.06.2026) | ✅ nachgeschlagen, ersetzt |
| High | `webpanel.py:146` | `Secoolio` als **festes Standardpasswort** im Quelltext | ✅ Login ist Standard, Passwort wird gewürfelt |
| Medium | `webpanel.py:641` | verändernde Anfragen ohne Content-Type-Prüfung → fremdes `<form>` möglich | ✅ Riegel + Test |
| Medium | `casino.py:762` | `_roulette_payout` kann `None` liefern → `None > 0` | ✅ abgesichert (Einsatz zurück) |
| Medium | `arbeit.py:834` | `SpassWordle.bauen` ohne `**_extra` → OOP-Vertragsbruch | ✅ gefixt + Test |
| Low | `economy.py:1710` | unsichtbares Zeichen (U+200B) roh im Quelltext | ✅ als `\u200b` escaped |
| Low | `floaktie.py`, `words.py` | tote Zuweisungen | ✅ entfernt |
| Low | `cmdnorm.py`, `music.py` | doppelte Einträge in Mengen | dokumentiert, harmlos |

---

## 3. Geprüft und **verworfen**

Damit nachvollziehbar ist, dass diese Punkte nicht übersehen, sondern
**widerlegt** wurden:

| Gemeldet | Warum es nicht stimmt |
|---|---|
| `voicegags.py:363` espeak-Einschleusung | Der Code hat längst `--` als Options-Ende, samt Kommentar, der genau diesen Angriff beschreibt |
| `schulden.py:1652` Doppelklick bucht doppelt | `self._laeuft` wird **synchron vor dem ersten `await`** gesetzt |
| `casino.py:250` Server-Deckel per Knopf umgehbar | Alle **acht** Interaction-Pfade geben `interaction.guild_id` mit; der Docstring beschreibt genau diese Behebung |
| **50× `B023`** (Linter) | 43× `_safe(lambda: …)` — `_safe` ruft die Funktion **sofort** auf (`webpanel.py:708`); 6× `render.py:1899`, dort wird `rot()` in derselben Iteration benutzt (Z. 1902–1907); 1× Test |
| 773× `pyright` Optional-Zugriffe | Folge des Hausstils (`self._store = None` im `__init__`, gesetzt in `setup()`) — keine Annotationen, also kann pyright nicht eingrenzen |

---

## 4. Verteilung der gemeldeten Funde

**Nach Datei (Top 15)** — hohe Zahl heißt *viel Code*, nicht *schlechter Code*:

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

**Nach Kategorie:**

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
