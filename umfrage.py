"""Flo Umfrage: echte Discord-Umfragen, die Flo selbst baut - und hinterher
auswertet.

Befehl (nach 'Flo'):
- umfrage <Thema>                    Flo denkt sich Frage + Antworten aus (KI)
- umfrage <Frage> | <A> | <B> | ...  genau so, wie du es schreibst (ohne KI)
- umfrage 48h <...>                  Laufzeit vorne dran: 6h, 2t/2d, 1w (max. 32 Tage)

Aliase: umfrage, poll, abstimmung, voting.

Warum echte Discord-Umfragen statt Reaktions-Zaehlerei: Discord zaehlt selbst,
jeder hat genau eine Stimme, das Ergebnis steht live unter der Frage, und die
Umfrage laeuft auch weiter, wenn Flo gerade neu startet.

Laeuft sie ab, postet Discord eine Ergebnis-Nachricht (Typ poll_result, Autor:
Flo). bot.py reicht die hierher (ergebnis()), und Flo sagt, was er davon haelt.

Kein Speicher, keine Datei: der Zustand ist nur die Abkuehlzeit je Nutzer.
"""

import datetime
import json
import logging
import os
import random
import re
import time

import discord

import ai
import basis
from basis import FeatureBasis

log = logging.getLogger("dcbot.umfrage")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

_CMDS = ("umfrage", "poll", "abstimmung", "voting")

# 'flo umfrage ist doof' ist keine Umfrage, sondern eine Meinung UEBER Umfragen -
# die beantwortet die KI. Steht direkt hinter dem Befehlswort ein solches Wort,
# ist es kein Befehl (die Regel aus dem Fehlgriff-Umbau: nur eindeutig).
_KEIN_BEFEHL = {
    "ist", "war", "sind", "waren", "wird", "wurde", "nervt", "nerven", "suckt",
    "stinkt", "klingt", "finde", "fand", "macht", "bringt", "hat", "hatte",
    "von", "vom", "gibts", "gibt's",
}

# Discords eigene Grenzen (discord.Poll): Frage 300, Antwort 55 Zeichen,
# 1-10 Antworten (eine Umfrage mit einer Antwort ist keine), 1-768 Stunden.
FRAGE_MAX = 300
ANTWORT_MAX = 55
ANTWORTEN_MIN = 2
ANTWORTEN_MAX = 10
STUNDEN_MAX = 768
STUNDEN_STANDARD = 24
COOLDOWN = 60   # Sekunden je Nutzer - Umfragen sind laut, der Kanal gehoert allen

# '48h', '2 tage', '1w' - NUR direkt hinter dem Befehlswort. Hinten im Satz waere
# '5 tage urlaub oder arbeit' eine Laufzeit von fuenf Tagen statt einer Frage.
_DAUER_RE = re.compile(
    r"^(\d{1,4})\s*(h|std\.?|stunden?|t|d|tage?|w|wo|wochen?)(?=\s|$|[:,.;])[\s:,.;]*",
    re.IGNORECASE)
_EINHEIT_STUNDEN = {"h": 1, "s": 1, "t": 24, "d": 24, "w": 168}

# Aufzaehlungszeichen, die die KI gern vor Antworten setzt ('1. Pizza', '- Doener').
_SPIEGELSTRICH_RE = re.compile(r"^\s*(?:[-*•·]|\d{1,2}[.)])\s*")

_BEGLEIT = (
    "Abstimmen, ihr Lappen. Enthaltung zählt als Feigheit.",
    "Los, klickt. Wer nicht abstimmt, hält hinterher die Fresse.",
    "Demokratie, ihr Pfeifen. Ausnahmsweise zählt eure Meinung mal.",
    "Stimmt ab, bevor ich das für euch entscheide.",
    "Einmal klicken schafft sogar ihr. Hoffe ich.",
)
_ANLEITUNG = (
    "🗳️ Umfrage? Dann sag auch worüber, du Genie:\n"
    "`{name} umfrage bestes Fast Food` – ich denk mir die Antworten aus\n"
    "`{name} umfrage Pizza oder Döner? | Pizza | Döner | Beides` – genau so\n"
    "`{name} umfrage 48h ...` – Laufzeit vorne dran (6h, 2t, 1w, max. 32 Tage)"
)
_ZU_WENIG = ("Eine Umfrage mit {n} Antwort? Das ist keine Umfrage, das ist eine "
             "Diktatur. Mindestens zwei, getrennt mit `|`.")
_HETZE = (
    "Über so einen Dreck lass ich nicht abstimmen. Such dir ein anderes Thema.",
    "Nee. Menschenfeindlichen Müll gibt's hier nicht mal als Umfrage.",
)
_KEIN_RECHT = ("Ich darf hier keine Umfragen posten. Gebt mir das Recht "
               "„Umfragen erstellen“, ihr Amateure.")
_ZU_SCHNELL = "Chill mal, du hast gerade erst eine gemacht. Noch {s} s."
# Wenn die KI nichts Brauchbares liefert, bleibt es trotzdem eine Umfrage.
_ERSATZ_ANTWORTEN = (
    ("Ja, logisch", "Nein, du Lauch", "Ist mir scheißegal"),
    ("Absolut", "Auf keinen Fall", "Frag nicht so dumm"),
    ("Ja", "Nein", "Halt die Fresse, ich will nur das Ergebnis sehen"),
)

# Ergebnis-Kommentare, falls die KI nicht will oder kann.
_SIEG = (
    "„{sieger}“ gewinnt mit {stimmen} von {gesamt} Stimmen. Der Rest von euch hat "
    "halt keinen Geschmack.",
    "Entschieden: „{sieger}“ ({stimmen}/{gesamt}). Wer anders gestimmt hat, "
    "soll sich schämen gehen.",
    "„{sieger}“ holt es mit {stimmen} Stimmen. Überrascht mich bei euch "
    "Clowns null.",
)
_PATT = (
    "Unentschieden bei „{frage}“. Ihr kriegt nicht mal eine Mehrheit hin, ihr Lappen.",
    "Patt. {gesamt} Stimmen und trotzdem kein Ergebnis - typisch ihr.",
)
_NIEMAND = (
    "„{frage}“ ist vorbei - null Stimmen. Zu faul zum Klicken, ihr Pfeifen?",
    "Keine einzige Stimme. Ich frag euch nie wieder was, ihr Schlaftabletten.",
)


def _kappen(text, grenze):
    """Auf Discords Grenze kuerzen - mit '…' statt mitten im Wort abzubrechen."""
    text = " ".join(str(text or "").split())
    if len(text) <= grenze:
        return text
    kurz = text[:grenze - 1].rstrip()
    leer = kurz.rfind(" ")
    if leer > grenze // 2:
        kurz = kurz[:leer].rstrip(" ,;:-")
    return kurz + "…"


def _dauer_text(stunden):
    if stunden % 168 == 0:
        n = stunden // 168
        return f"{n} Woche" if n == 1 else f"{n} Wochen"
    if stunden % 24 == 0:
        n = stunden // 24
        return f"{n} Tag" if n == 1 else f"{n} Tage"
    return f"{stunden} Stunde" if stunden == 1 else f"{stunden} Stunden"


class Umfrage(FeatureBasis):
    """Baut discord.Poll-Umfragen und kommentiert ihr Ergebnis."""

    def __init__(self):
        self._enabled = False
        self._stunden = STUNDEN_STANDARD
        self._zuletzt = {}   # uid -> monotonic; 'nie' = -inf (siehe Monotonic-Falle)

    def setup(self):
        if os.getenv("UMFRAGE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Umfragen aus (UMFRAGE_ENABLED=0).")
            return False
        try:
            stunden = int(os.getenv("UMFRAGE_STUNDEN", str(STUNDEN_STANDARD)))
        except ValueError:
            stunden = STUNDEN_STANDARD
        self._stunden = max(1, min(STUNDEN_MAX, stunden))
        self._enabled = True
        log.info("Umfragen aktiv (Standard-Laufzeit %d h).", self._stunden)
        return True

    def is_enabled(self):
        return self._enabled

    # --- Zerlegen ------------------------------------------------------------
    def zerlegen(self, text):
        """'umfrage 48h Pizza oder Doener' -> (stunden, rest, zu_lang) oder None.

        None heisst: kein Umfrage-Befehl, die KI ist dran. rest == "" heisst:
        nacktes 'umfrage' -> Anleitung."""
        teile = (text or "").strip().split(None, 1)
        if not teile or teile[0].lower().strip(".,;:!?") not in _CMDS:
            return None
        rest = teile[1].strip() if len(teile) > 1 else ""
        erstes = rest.split(None, 1)[0].lower().strip(".,;:!?") if rest else ""
        if erstes in _KEIN_BEFEHL:
            return None
        stunden, zu_lang = self._stunden, False
        treffer = _DAUER_RE.match(rest)
        if treffer:
            einheit = treffer.group(2).lower()[0]
            stunden = int(treffer.group(1)) * _EINHEIT_STUNDEN.get(einheit, 1)
            zu_lang = stunden > STUNDEN_MAX
            stunden = max(1, min(STUNDEN_MAX, stunden))
            rest = rest[treffer.end():].strip()
        return stunden, rest, zu_lang

    @staticmethod
    def bereinigen(frage, antworten):
        """Frage + Antworten auf Discords Grenzen bringen. None, wenn es danach
        keine zwei verschiedenen Antworten mehr gibt."""
        frage = _kappen(frage, FRAGE_MAX)
        sauber, gesehen = [], set()
        for antwort in antworten or ():
            antwort = _kappen(_SPIEGELSTRICH_RE.sub("", str(antwort or "")), ANTWORT_MAX)
            schluessel = antwort.casefold()
            if not antwort or schluessel in gesehen:
                continue
            gesehen.add(schluessel)
            sauber.append(antwort)
            if len(sauber) == ANTWORTEN_MAX:
                break
        if not frage or len(sauber) < ANTWORTEN_MIN:
            return None
        return frage, sauber

    @staticmethod
    def _json_lesen(text):
        """Das JSON aus der KI-Antwort fischen - auch mit Codeblock oder Vorwort."""
        treffer = re.search(r"\{.*\}", text or "", re.DOTALL)
        if not treffer:
            return None
        try:
            daten = json.loads(treffer.group(0))
        except (ValueError, TypeError):
            return None
        if not isinstance(daten, dict):
            return None
        frage = daten.get("frage") or daten.get("question")
        antworten = daten.get("antworten") or daten.get("answers")
        if not isinstance(frage, str) or not isinstance(antworten, list):
            return None
        return frage, [a for a in antworten if isinstance(a, (str, int, float))]

    async def _ki_entwurf(self, thema):
        """Frage + Antworten von der KI. None, wenn sie nichts Brauchbares liefert.

        ai.generate laeuft OHNE Persona und OHNE Guardrail (system= ersetzt
        beides) - deshalb kommt der Guardrail hier ausdruecklich mit."""
        if not ai.is_enabled():
            return None
        system = (
            f"Du bist {self._bot_name}, ein frecher deutscher Discord-Bot mit losem "
            "Mundwerk, und baust aus einem Thema eine Discord-Umfrage. Antworte NUR "
            'mit JSON, ohne Vorwort und ohne Codeblock: {"frage": "...", '
            '"antworten": ["...", "..."]}. Die Frage ist ein kurzer, frecher Satz '
            "(hoechstens 200 Zeichen). 3 bis 6 Antworten, jede hoechstens 40 Zeichen, "
            "alle verschieden, mindestens eine davon derb oder absurd. Alles auf "
            f"Deutsch. {ai.FloAI._GUARDRAIL}"
        )
        try:
            roh = await ai.generate(f"Thema: {thema[:300]}", system=system,
                                    temperature=0.9, max_tokens=220)
        except Exception:  # noqa: BLE001 - dann eben die Ersatz-Antworten
            log.debug("Umfrage: KI-Entwurf gescheitert", exc_info=True)
            return None
        entwurf = self._json_lesen(roh)
        if entwurf is None:
            if roh:
                log.info("Umfrage: KI lieferte kein brauchbares JSON: %r", roh[:120])
            return None
        return self.bereinigen(*entwurf)

    def _ersatz(self, thema):
        frage = thema.strip()
        if frage and frage[-1] not in "?!.":
            frage += "?"
        return self.bereinigen(frage[:1].upper() + frage[1:],
                               random.choice(_ERSATZ_ANTWORTEN))

    # --- Befehl --------------------------------------------------------------
    async def handle(self, message):
        """'umfrage ...' -> echte Discord-Umfrage. None = kein Umfrage-Befehl."""
        if not self._enabled or message.guild is None:
            return None
        zerlegt = self.zerlegen(ai.strip_lead(message.content or ""))
        if zerlegt is None:
            return None
        stunden, rest, zu_lang = zerlegt
        if not rest:
            return _ANLEITUNG.format(name=self._bot_name)

        uid = message.author.id
        warten = self._zuletzt.get(uid, float("-inf")) + COOLDOWN - time.monotonic()
        if warten > 0:
            return _ZU_SCHNELL.format(s=int(warten) + 1)

        # Flo stellt die Umfrage in SEINEM Namen - ueber Hetze laesst er nicht
        # abstimmen, egal ob die KI oder der Nutzer sie formuliert.
        if self._ist_hetze(rest):
            return random.choice(_HETZE)

        if "|" in rest:
            frage, *antworten = [t.strip() for t in rest.split("|")]
            entwurf = self.bereinigen(frage, antworten)
            if entwurf is None:
                n = len({a.casefold() for a in antworten if a})
                if not frage:
                    return "Und die Frage? Die kommt VOR den ersten `|`, du Held."
                return _ZU_WENIG.format(n=n)
        else:
            entwurf = await self._ki_entwurf(rest) or self._ersatz(rest)
            if entwurf is None:
                return _ANLEITUNG.format(name=self._bot_name)
        frage, antworten = entwurf
        if self._ist_hetze(" ".join([frage, *antworten])):
            return random.choice(_HETZE)

        rechte = self._rechte(message)
        if rechte is not None and not getattr(rechte, "send_polls", True):
            return _KEIN_RECHT

        umfrage = discord.Poll(question=frage,
                               duration=datetime.timedelta(hours=stunden))
        for antwort in antworten:
            umfrage.add_answer(text=antwort)
        text = f"{random.choice(_BEGLEIT)} ⏳ Läuft {_dauer_text(stunden)}."
        if zu_lang:
            text += " (Länger als 32 Tage kann Discord nicht, du Genie.)"
        self._zuletzt[uid] = time.monotonic()
        gesendet = await basis.antworte(message, text, poll=umfrage)
        if gesendet is None:
            # Nichts rausgegangen (basis.antworte hat es schon geloggt). Dann
            # blockiert die Abkuehlzeit nicht den naechsten Versuch - und Flo
            # sagt wenigstens, dass es nicht ging, statt still zu bleiben.
            self._zuletzt.pop(uid, None)
            return "Discord hat meine Umfrage gefressen. Versuch's nochmal."
        log.info("Umfrage von %s: %r (%d Antworten, %d h)",
                 getattr(message.author, "display_name", "?"), frage[:60],
                 len(antworten), stunden)
        return HANDLED

    @staticmethod
    def _rechte(message):
        try:
            return message.channel.permissions_for(message.guild.me)
        except Exception:  # noqa: BLE001 - Attrappen/DM: dann fragt Discord selbst
            return None

    @staticmethod
    def _ist_hetze(text):
        try:
            import fun
            return bool(fun.instance.ist_hetze(text))
        except Exception:  # noqa: BLE001 - ohne Erkennung entscheidet die KI-Grenze
            return False

    # --- Ergebnis ------------------------------------------------------------
    @staticmethod
    def ergebnis_daten(message):
        """Die Felder der poll_result-Nachricht: (frage, sieger, stimmen, gesamt).

        Discord schickt sie als Embed mit benannten Feldern; einen Sieger gibt
        es nur ohne Gleichstand."""
        felder = {}
        for embed in getattr(message, "embeds", None) or ():
            for feld in getattr(embed, "fields", None) or ():
                felder[str(feld.name)] = str(feld.value or "")

        def zahl(schluessel):
            try:
                return int(felder.get(schluessel) or 0)
            except ValueError:
                return 0
        return (felder.get("poll_question_text", ""),
                felder.get("victor_answer_text", ""),
                zahl("victor_answer_votes"), zahl("total_votes"))

    async def _ki_kommentar(self, frage, sieger, stimmen, gesamt):
        if not ai.is_enabled():
            return None
        system = (
            f"Du bist {self._bot_name}, ein frecher deutscher Discord-Bot mit losem "
            "Mundwerk. Eine Umfrage im Server ist gerade zu Ende. Kommentiere das "
            "Ergebnis in EINEM derben, spoettischen Satz auf Deutsch: nenn die "
            "Gewinner-Antwort und mach dich ueber die Leute lustig, die abgestimmt "
            f"haben. Keine Emojis, kein Vorwort, keine Aufzaehlung. {ai.FloAI._GUARDRAIL}"
        )
        try:
            out = await ai.generate(
                f"Umfrage: {frage[:300]}\nGewonnen hat: {sieger[:60]} "
                f"({stimmen} von {gesamt} Stimmen).",
                system=system, temperature=1.0, max_tokens=90)
        except Exception:  # noqa: BLE001
            return None
        if not out or ai.instance._ist_verweigerung(out):
            return None
        return ai.instance._kuerzen(out.strip())

    async def ergebnis(self, message):
        """Discord hat eine von Flos Umfragen beendet -> Flo sagt was dazu."""
        if not self._enabled:
            return False
        frage, sieger, stimmen, gesamt = self.ergebnis_daten(message)
        frage_kurz = _kappen(frage, 80) or "eure Umfrage"
        if gesamt <= 0:
            text = random.choice(_NIEMAND).format(frage=frage_kurz)
        elif not sieger:
            text = random.choice(_PATT).format(frage=frage_kurz, gesamt=gesamt)
        else:
            text = (await self._ki_kommentar(frage, sieger, stimmen, gesamt)
                    or random.choice(_SIEG).format(sieger=_kappen(sieger, 60),
                                                   stimmen=stimmen, gesamt=gesamt))
        keine = discord.AllowedMentions.none()
        try:
            try:
                await message.channel.send(
                    text, reference=message.to_reference(fail_if_not_exists=False),
                    allowed_mentions=keine)
            except discord.HTTPException:
                await message.channel.send(text, allowed_mentions=keine)
        except discord.HTTPException:
            log.warning("Umfrage-Ergebnis konnte nicht kommentiert werden.")
            return False
        log.info("Umfrage-Ergebnis kommentiert: %r -> %r (%d/%d)",
                 frage[:60], sieger[:40], stimmen, gesamt)
        return True


instance = Umfrage()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
ergebnis = instance.ergebnis
