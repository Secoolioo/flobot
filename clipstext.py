"""Clip-Text: Transkript, Pruefung, Flos Kommentar und die Bestenliste.

Alles, was an einem Voice-Clip Text ist (der Ton selbst entsteht in
clipschnitt.py, die Bedienung steckt in clips.py):

    Kontingent      Groqs Grenzen fuer Whisper (Anfragen je Minute/Tag,
                    Audio-Sekunden je Stunde/Tag) - mit etwas Abstand darunter
    Transkriber     schickt den Ton je Sprecher an Whisper (je_sprecher)
    segmente_lesen  Antwort -> [Segment], egal ob Objekt oder dict
    halluzination   Whisper erfindet auf Stille gern Saetze ("Untertitel im
                    Auftrag des ZDF", "Vielen Dank fuers Zuschauen") - raus
    zeilen_bauen    Segmente ueber die Karte aus clipschnitt zurueck auf die
                    Clip-Zeit, nach Zeit verzahnt, kurze Pausen zusammengelegt
                    (mit Hoechstlaenge: ein Monolog bleibt mehrere Zeilen)
    pruefen         Notsignal, Hetze, private Daten (auch ueber die Naht zweier
                    Zeilen) - Grundlage dafuer, ob der Clip ueberhaupt rausgeht
                    (clips.py entscheidet)
    transkript_text das Transkript fuers Embed: ohne Markdown, ohne Ping,
                    private Zeilen geschwaerzt, bei Ueberlaenge das Ende
    kommentar       ein Satz von Flo zum Clip (KI im Hintergrund, mit Grenze);
                    None, wenn nichts Brauchbares kam - dann ersatz_kommentar
    rangliste_embed 'flo clips': die Clips mit den meisten Flammen im Zeitraum

Wer was zu sehen bekommt: Groq bekommt den Ton der Sprecher (nur die Stellen,
an denen sie reden) und fuer den Kommentar den Text - mit Buchstaben statt
Namen (A, B, C). Ins Log kommen nur Zahlen, nie ein Wort aus dem Transkript.

Der Transkriber nimmt einen eigenen Zugang, wenn CLIP_WHISPER_KEY gesetzt ist
(CLIP_WHISPER_URL, Standard Groq), sonst den LLM-Client von ai.py - aber nur,
wenn der ohnehin zu Groq spricht; ein anderer Anbieter hat kein Whisper oder
rechnet anders ab. Sonst gibt es kein Transkript (CLIP_TRANSKRIPT=aus schaltet
es auch ausdruecklich ab). Das Modell: CLIP_WHISPER_MODEL.

Beim Import passiert nichts: kein Client, kein Netz, keine Datei.
"""

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

import discord

import ai

try:  # Ohne openai gibt es eben keinen eigenen Whisper-Zugang.
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - nur ohne Paket
    AsyncOpenAI = None

log = logging.getLogger("dcbot.clipstext")

GROQ_URL = "https://api.groq.com/openai/v1"
#: Standard fuer CLIP_WHISPER_MODEL: schnell, billig, kann Deutsch.
WHISPER_MODELL = "whisper-large-v3-turbo"
#: So viele Sprecher gleichzeitig bei Whisper.
WHISPER_PARALLEL = 2
#: So lange darf das ganze Transkript eines Clips dauern.
WHISPER_FRIST = 35.0
#: Zeitgrenzen des eigenen Clients: lesen 30 s, verbinden 5 s.
WHISPER_ZEIT = 30.0
WHISPER_VERBINDEN = 5.0
#: So lange wartet der Kommentar auf die KI.
KI_FRIST = 20.0
#: Zwei Saetze desselben Sprechers mit weniger Pause dazwischen: eine Zeile ...
ZUSAMMEN_SEK = 1.5
#: ... aber nur bis zu dieser Laenge und Spanne. Ohne Grenze wurde ein
#: Monolog EINE Riesenzeile: das Kuerzen nahm ihm das Ende, und ein einziger
#: privater Satz schwaerzte den ganzen Monolog.
ZEILE_ZUSAMMEN_MAX = 280
ZEILE_SPANNE_SEK = 20.0
#: So viele Zeichen je Seite sieht pruefen() ueber die Grenze zweier Zeilen
#: desselben Sprechers - eine Nummer, die genau dort zerfaellt ('0171' |
#: '2345678'), fiele sonst durch.
NAHT = 40
#: Platz fuers Transkript im Embed (Discord erlaubt 4096).
TRANSKRIPT_MAX = 3800
#: Laenger wird keine Zeile im Transkript - gekuerzt wird vorne, das Ende
#: bleibt. Trifft nur noch ein einzelnes, ueberlanges Whisper-Segment.
ZEILE_MAX = 600
ZENSIERT = "[privat – zensiert]"
#: Laenger wird Flos Kommentar nie.
KOMMENTAR_MAX = 300
#: So viel Transkript bekommt die KI fuer den Kommentar (das Ende zaehlt).
KOMMENTAR_EINGABE_MAX = 1500
FARBE = 0xE67E22

#: Wenn die KI aus ist oder nichts Brauchbares liefert.
ERSATZ_KOMMENTARE = (
    "Ich hab's mir angehört. Ich will die Minute zurück.",
    "Für die Nachwelt festgehalten. Die Nachwelt hat's nicht verdient.",
    "Kunst. Oder Lärm. Eher Lärm.",
    "Das ist jetzt für immer im Internet. Glückwunsch.",
    "Ich hab schon Besseres aus einer Waschmaschine gehört.",
)

#: Der Auftrag an die KI. Die Grenze (ai.FloAI._GUARDRAIL) kommt woertlich
#: dahinter - ai.generate laeuft ohne Persona und ohne Grenze.
KOMMENTAR_SYSTEM = (
    "Du bist {name}, ein frecher deutscher Discord-Bot mit losem Mundwerk. Jemand hat "
    "gerade einen Clip aus dem Voice-Chat gezogen; unten steht, was gesagt wurde. "
    "Kommentiere den Clip in GENAU EINEM Satz auf Deutsch, passiv-aggressiv – mach dich "
    "über das Gelaber lustig, nie über Stimme, Aussehen oder Herkunft. Zitiere niemanden "
    "wörtlich, nenn keine Namen. Keine Emojis, kein Vorwort. ")

#: Bestenliste: Zeitraum -> (Tage, Titel).
ZEITRAEUME = {
    "woche": (7, "🔥 Clips der Woche"),
    "monat": (30, "🔥 Clips des Monats"),
    "alle": (365, "🔥 Die besten Clips des Jahres"),
}
ZEITRAEUME["top"] = ZEITRAEUME["alle"]
BRETT_LEER = ("Keine Clips mit 🔥 in den letzten {tage} Tagen. Entweder war hier nix "
              "lustig, oder ihr habt keinen Geschmack.")
BRETT_MAX = 10

# --- Was Whisper auf Stille gern erfindet ---------------------------------------
# Die Saetze stammen aus den Untertiteln, mit denen Whisper trainiert wurde -
# deutsche Sender, YouTube-Abspanne. Auf einem leisen Stueck Voice kommen sie
# ohne jeden Bezug heraus. Hinter dem Sender steht ein \b mehr als im Entwurf:
# sonst war "Untertitel braucht keiner" wegen 'br' auch eine Halluzination.
# Ueber den Entwurf hinaus (kamen sonst durch): die Kurzform "Danke fuers
# Zusehen", der Vortrags-Abspann "Vielen Dank fuer Ihre Aufmerksamkeit", das
# Video-Outro und ein Sender mit Jahreszahl ("SWR 2021") oder Adresse.
_HALLUZINATION = tuple(re.compile(muster, re.IGNORECASE) for muster in (
    r"untertitel(ung)?\b.*\b(zdf|ard|wdr|swr|ndr|mdr|br|funk|amara)\b|untertitel im auftrag"
    r"|amara\.org",
    r"(vielen )?danke? (fürs|für's|für das|fuers|fuer's) zu(schauen|sehen|hören|hoeren)"
    r"|vielen dank (für|fuer) (ihre|eure|deine) aufmerksamkeit",
    r"(abonnier\w*) (den|meinen) kanal|bis zum nächsten mal"
    r"|wir sehen uns (im|beim) nächsten video|(euch|ihnen) (hat )?das video gefallen"
    r"|hat (euch|ihnen) das video gefallen|das video hat (euch|ihnen) gefallen"
    r"|copyright|©",
    r"^\W*[\[(*]?(musik|applaus|gelächter|lachen|stille|untertitel)[\])*]?\W*$|^\W*♪+\W*$",
    r"^\W*(zdf|ard|wdr|swr|ndr|mdr|br|hr|rbb|funk)\W+(19|20)\d\d\W*$"
    r"|\b(zdf|ard|wdr|swr|ndr|mdr|br|funk|daserste)\.de\b",
))
#: Kurze Hoeflichkeiten, die Whisper auf Stille ebenso gern erfindet - die im
#: Voice aber auch echt fallen. Sie fliegen deshalb nur raus, wenn Whisper
#: selbst schon ein wenig an Stille glaubt (no_speech_prob ab KURZ_STILLE).
_KURZ_VERDAECHTIG = re.compile(
    r"^\W*(und\s+)?(vielen\s+dank|danke(\s*schön|\s+sehr|\s+euch)?|ich\s+danke\s+(ihnen|euch|dir)"
    r"|tschüss?|ciao|auf\s+wiedersehen|bis\s+(bald|dann|später|morgen)"
    r"|das\s+war['’]?s(\s+für\s+(heute|jetzt))?)\W*$", re.IGNORECASE)
#: Grenzwerte aus Whispers eigenen Angaben je Segment.
STILLE_WAHRSCHEINLICH = 0.6     # no_speech_prob ab hier ...
STILLE_LOGPROB = -0.8           # ... zusammen mit so wenig Zuversicht
LOGPROB_MIN = -1.2              # darunter ist es geraten
KOMPRESSION_MAX = 2.4           # darueber wiederholt es sich (Schleife)
KURZ_STILLE = 0.3               # ab hier ist eine kurze Hoeflichkeit verdaechtig

# --- Wenn die KI den Kommentar verweigert ---------------------------------------
# ai.FloAI._VERWEIGERUNG_RE kennt mit Absicht nur englische Floskeln: im Chat
# beginnt bei Flo "ich kann dir da nicht helfen" oft einen Spruch. Beim
# Clip-Kommentar gibt es keinen Chat - verlangt ist EIN frecher Satz, und eine
# Absage stuende oeffentlich unter dem Clip ("Es tut mir leid, aber dabei kann
# ich nicht helfen."). Dann lieber der Ersatz. Gesucht wird nur am Satzanfang
# und nur mit Absage-Sinn (ich + kann/darf/werde/... + nicht + helfen/
# kommentieren/...): "Tut mir leid fuer eure Ohren" bleibt ein Kommentar.
_ABSAGE_RE = re.compile(
    r"^\W{0,3}(?:(?:(?:es\s+)?tut\s+mir\s+(?:\w+\s+)?leid|sorry|entschuldig\w*|leider)\b"
    r"[^.!?]{0,80}?\b)?"
    r"(?:ich\s+(?:kann|darf|werde|m[öo]chte|will)|(?:das|dazu|dabei|damit|hier(?:zu|bei)?|darauf"
    r"|diese[nmrs]?\s+\w+|so\s*was)\s+(?:kann|darf|werde|m[öo]chte|will)\s+ich"
    r"|(?:kann|darf|werde|m[öo]chte|will)\s+ich)\b"
    r"[^.!?]{0,60}?\b(?:nicht|nichts|keine?[nmrs]?)\b[^.!?]{0,40}?"
    r"\b(?:helfen|kommentieren|beurteilen|bewerten|sagen|[äa]u(?:ß|ss)ern|abgeben|nachkommen"
    r"|erf[üu]llen|unterst[üu]tzen|beitragen|machen|tun|schreiben|liefern|erstellen)\b"
    r"|\bals\s+(?:eine?\s+)?(?:ki|ai|sprachmodell|k[üu]nstliche\s+intelligenz)\b"
    r"|\bich\s+bin\s+(?:nur\s+)?(?:eine?\s+)?(?:ki|ai|sprachmodell)\b",
    re.IGNORECASE)


# =============================================================================
# Datenformen
# =============================================================================
@dataclass
class Segment:
    """Ein Stueck Whisper-Antwort, Zeiten in Sekunden der Sprecher-Datei."""

    start: float
    ende: float
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 1.0


@dataclass
class Zeile:
    """Eine Zeile Transkript: t in Sekunden ab Clip-Beginn."""

    t: float
    uid: int
    name: str
    text: str


@dataclass
class Befund:
    """Was pruefen() im Transkript gefunden hat."""

    notlage_uids: set = field(default_factory=set)
    hetze: bool = False
    heikel: set = field(default_factory=set)   # Zeilennummern mit privaten Daten


# =============================================================================
# Kontingent
# =============================================================================
class Kontingent:
    """Groqs Whisper-Grenzen, mit Abstand darunter: 15 Anfragen je Minute,
    1500 je Tag, 6000 Audio-Sekunden je Stunde, 24000 je Tag. Jede Anfrage
    zaehlt mindestens 10 s - so rechnet Groq ab.

    Gezaehlt wird in Minuten-Eimern ({minute: [anfragen, sekunden]}): klein
    genug zum Speichern (clips legt als_dict() in clips.json), und die Fenster
    rechnen damit hoechstens eine Minute zu streng. Die Uhr ist die Wanduhr -
    das Kontingent muss einen Neustart ueberleben."""

    MINUTE_ANFRAGEN = 15
    TAG_ANFRAGEN = 1500
    STUNDE_SEK = 6000
    TAG_SEK = 24000
    MIN_SEK = 10
    #: Nach einem 429 ohne Angabe so lange Pause, hoechstens einen Tag.
    SPERRE_STANDARD = 60.0
    SPERRE_MAX = 86400.0

    def __init__(self, uhr=time.time):
        self._uhr = uhr
        self._eimer = {}          # minute (int) -> [anfragen, sekunden]
        self._gesperrt_bis = 0.0
        self._geprueft = []

    @classmethod
    def _sekunden(cls, sekunden_liste):
        if sekunden_liste is None:
            return []
        if isinstance(sekunden_liste, (int, float)):
            sekunden_liste = [sekunden_liste]
        raus = []
        for s in sekunden_liste:
            try:
                raus.append(max(float(cls.MIN_SEK), float(s)))
            except (TypeError, ValueError):
                raus.append(float(cls.MIN_SEK))
        return raus

    def _aufraeumen(self, jetzt):
        ab = int((jetzt - 86400) // 60)
        for minute in [m for m in self._eimer if m < ab]:
            del self._eimer[minute]

    def _summe(self, jetzt, zurueck):
        """(anfragen, sekunden) der letzten 'zurueck' Sekunden. Eimer aus der
        Zukunft (die Uhr ist zurueckgesprungen) zaehlen nicht."""
        ab = int((jetzt - zurueck) // 60)
        bis = int(jetzt // 60) + 1
        anfragen = sekunden = 0
        for minute, (n, sek) in self._eimer.items():
            if ab <= minute <= bis:
                anfragen += n
                sekunden += sek
        return anfragen, sekunden

    def gesperrt(self):
        return self._uhr() < self._gesperrt_bis

    def darf(self, sekunden_liste):
        """Passen diese Anfragen (je eine Sekundenzahl) jetzt noch hinein?"""
        liste = self._sekunden(sekunden_liste)
        self._geprueft = liste
        jetzt = self._uhr()
        if jetzt < self._gesperrt_bis:
            return False
        if not liste:
            return True
        anfragen, sekunden = len(liste), sum(liste)
        if self._summe(jetzt, 60)[0] + anfragen > self.MINUTE_ANFRAGEN:
            return False
        if self._summe(jetzt, 3600)[1] + sekunden > self.STUNDE_SEK:
            return False
        tag_n, tag_s = self._summe(jetzt, 86400)
        return tag_n + anfragen <= self.TAG_ANFRAGEN and tag_s + sekunden <= self.TAG_SEK

    def buchen(self, sekunden_liste=None):
        """Bucht die Anfragen. Ohne Angabe die, die zuletzt darf() geprueft hat."""
        liste = self._geprueft if sekunden_liste is None else self._sekunden(sekunden_liste)
        if not liste:
            return
        jetzt = self._uhr()
        self._aufraeumen(jetzt)
        eimer = self._eimer.setdefault(int(jetzt // 60), [0, 0.0])
        eimer[0] += len(liste)
        eimer[1] = round(eimer[1] + sum(liste), 1)

    def sperren(self, retry_after=None):
        """Nach einem 429: so lange gar nicht fragen."""
        try:
            pause = float(retry_after)
        except (TypeError, ValueError):
            pause = self.SPERRE_STANDARD
        pause = min(self.SPERRE_MAX, max(5.0, pause))
        self._gesperrt_bis = max(self._gesperrt_bis, self._uhr() + pause)
        log.info("Clips: Whisper-Kontingent erschoepft - %.0f s Pause.", pause)

    def heute(self):
        """(anfragen, sekunden) der letzten 24 Stunden - fuer 'flo clip status'."""
        anfragen, sekunden = self._summe(self._uhr(), 86400)
        return anfragen, round(sekunden)

    def als_dict(self):
        self._aufraeumen(self._uhr())
        return {"eimer": {str(m): [int(n), float(s)] for m, (n, s) in sorted(self._eimer.items())},
                "gesperrt_bis": round(self._gesperrt_bis, 1)}

    def aus_dict(self, daten):
        """Stand aus clips.json - Muell wird uebergangen, nie geworfen."""
        self._eimer = {}
        self._gesperrt_bis = 0.0
        if not isinstance(daten, dict):
            return self
        jetzt = self._uhr()
        eimer = daten.get("eimer")
        for minute, wert in (eimer.items() if isinstance(eimer, dict) else ()):
            try:
                n, sek = int(wert[0]), float(wert[1])
                minute = int(minute)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if n > 0 or sek > 0:
                self._eimer[minute] = [max(0, n), max(0.0, sek)]
        try:
            bis = float(daten.get("gesperrt_bis") or 0.0)
        except (TypeError, ValueError):
            bis = 0.0
        # Eine Sperre, die weiter als einen Tag in der Zukunft liegt, luegt
        # (Uhrsprung beim Booten) - dann gilt hoechstens die Hoechstdauer.
        self._gesperrt_bis = min(bis, jetzt + self.SPERRE_MAX)
        self._aufraeumen(jetzt)
        return self


# =============================================================================
# Whisper
# =============================================================================
def _schalter_an():
    wert = os.getenv("CLIP_TRANSKRIPT", "").strip().lower()
    return wert not in ("0", "aus", "nein", "false", "off", "no")


def _status(fehler):
    """HTTP-Status eines Fehlers (openai: status_code), sonst 0."""
    for name in ("status_code", "status"):
        wert = getattr(fehler, name, None)
        if isinstance(wert, int):
            return wert
    wert = getattr(getattr(fehler, "response", None), "status_code", None)
    return wert if isinstance(wert, int) else 0


def _retry_after(fehler):
    """Sekunden aus dem Retry-After-Kopf, sonst None."""
    try:
        kopf = getattr(getattr(fehler, "response", None), "headers", None) or {}
        return float(kopf.get("retry-after") or kopf.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None


class Transkriber:
    """Schickt den Ton je Sprecher an Whisper. Ein Fehler, egal welcher, heisst:
    kein Transkript - nie ein halbes, dem ein Sprecher fehlt."""

    def __init__(self, kontingent=None):
        self.kontingent = kontingent if kontingent is not None else Kontingent()
        self._eigen = None            # eigener Client (CLIP_WHISPER_KEY)
        self._eigen_sig = None        # (key, url), mit denen er gebaut wurde
        self._sperre = None           # Semaphore - je Event-Loop neu
        self._sperre_loop = None

    @staticmethod
    def _wahl():
        """("eigen", key, url) | ("ki", "", url) | None - ohne etwas zu bauen."""
        if not _schalter_an():
            return None
        key = os.getenv("CLIP_WHISPER_KEY", "").strip()
        if key:
            if AsyncOpenAI is None:
                return None
            return ("eigen", key, os.getenv("CLIP_WHISPER_URL", "").strip() or GROQ_URL)
        inst = ai.instance
        url = str(getattr(inst, "_basis_url", "") or "")
        if getattr(inst, "_client", None) is not None and "groq.com" in url:
            return ("ki", "", url)
        return None

    def an(self):
        """Gibt es ein Transkript? (fuer Ansage und Status)"""
        return self._wahl() is not None

    def anbieter(self):
        """"Groq", "eigen" (ein anderer Dienst) oder "" (aus)."""
        wahl = self._wahl()
        if wahl is None:
            return ""
        return "Groq" if "groq.com" in wahl[2] else "eigen"

    @staticmethod
    def modell():
        return os.getenv("CLIP_WHISPER_MODEL", "").strip() or WHISPER_MODELL

    def client(self):
        """Der Client fuer diesen Aufruf - der von ai.py immer frisch geholt
        (er wird bei einer Cloudflare-Sperre neu gebaut)."""
        wahl = self._wahl()
        if wahl is None:
            return None
        art, key, url = wahl
        if art == "ki":
            return ai.instance._client
        if self._eigen is None or self._eigen_sig != (key, url):
            zeit = WHISPER_ZEIT
            try:
                from openai import Timeout
                zeit = Timeout(WHISPER_ZEIT, connect=WHISPER_VERBINDEN)
            except ImportError:  # pragma: no cover - aeltere openai-Pakete
                pass
            # max_retries=0: Wiederholen hiesse bei einem 429 nur noch mehr 429.
            self._eigen = AsyncOpenAI(api_key=key, base_url=url, max_retries=0, timeout=zeit)
            self._eigen_sig = (key, url)
        return self._eigen

    async def schliessen(self):
        """Den eigenen Client zumachen (Herunterfahren)."""
        alt, self._eigen, self._eigen_sig = self._eigen, None, None
        if alt is not None:
            try:
                await alt.close()
            except Exception:  # noqa: BLE001 - beim Herunterfahren egal
                log.debug("Clips: Whisper-Client liess sich nicht schliessen", exc_info=True)

    def _semaphore(self):
        """Semaphore(WHISPER_PARALLEL), je Event-Loop eine - eine aus einem
        alten Loop wuerde im neuen werfen, sobald jemand warten muss."""
        loop = asyncio.get_running_loop()
        if self._sperre is None or self._sperre_loop is not loop:
            self._sperre = asyncio.Semaphore(WHISPER_PARALLEL)
            self._sperre_loop = loop
        return self._sperre

    async def je_sprecher(self, audio, frist=WHISPER_FRIST):
        """{uid: WhisperAudio} -> {uid: [Segment]} (schon ohne Halluzinationen)
        oder None: aus, Kontingent voll, Frist um, irgendein Fehler - beim
        ersten Fehler werden die uebrigen Sprecher gar nicht erst geschickt
        (oder abgebrochen). Wirft nie - nur ein Abbruch (CancelledError) geht
        durch. Das Kontingent prueft und bucht es selbst."""
        try:
            return await self._je_sprecher(audio, frist)
        except Exception as exc:  # noqa: BLE001 - nie ein halbes Transkript, nie ein Absturz
            log.info("Clips: Transkript gescheitert (%s).", type(exc).__name__)
            return None

    async def _je_sprecher(self, audio, frist):
        audio = {uid: wa for uid, wa in (audio or {}).items() if getattr(wa, "mp3", None)}
        if not audio:
            return None
        client = self.client()
        if client is None:
            return None
        sekunden = [getattr(wa, "dauer", 0.0) for wa in audio.values()]
        if not self.kontingent.darf(sekunden):
            log.info("Clips: Whisper-Kontingent reicht nicht - Clip ohne Transkript.")
            return None
        self.kontingent.buchen(sekunden)
        sperre = self._semaphore()
        modell = self.modell()

        async def einer(uid, wa):
            async with sperre:
                antwort = await client.audio.transcriptions.create(
                    model=modell, file=("sprecher.mp3", wa.mp3, "audio/mpeg"),
                    language="de", response_format="verbose_json",
                    timestamp_granularities=["segment"], temperature=0)
            return uid, bereinigen(segmente_lesen(antwort))

        start = time.monotonic()
        aufgaben = [asyncio.ensure_future(einer(uid, wa)) for uid, wa in audio.items()]
        try:
            await asyncio.wait(aufgaben, timeout=frist, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            # Nach dem ersten Fehler, nach der Frist und bei einem Abbruch von
            # aussen stirbt alles, was noch laeuft oder wartet: ein Transkript
            # mit Loch wird ohnehin verworfen, und jeder weitere Upload kostete
            # nur Kontingent - nach einem 429 obendrein den naechsten 429.
            for aufgabe in aufgaben:
                aufgabe.cancel()
            await asyncio.gather(*aufgaben, return_exceptions=True)
        fehler = [a.exception() for a in aufgaben
                  if not a.cancelled() and a.exception() is not None]
        if fehler:
            # Ein 429 sperrt - auch wenn er nicht der erste Fehler war.
            voll = [erg for erg in fehler if _status(erg) == 429]
            if voll:
                self.kontingent.sperren(_retry_after(voll[0]))
            log.info("Clips: Transkript gescheitert (%s).",
                     ", ".join(sorted({type(erg).__name__ for erg in fehler})))
            return None
        if any(a.cancelled() for a in aufgaben):
            log.info("Clips: Transkript nach %.0f s abgebrochen.", frist)
            return None
        log.info("Clips: Transkript fuer %d Sprecher in %.1f s.", len(aufgaben),
                 time.monotonic() - start)
        return dict(a.result() for a in aufgaben)


transkriber = Transkriber()


# =============================================================================
# Segmente
# =============================================================================
def _feld(ding, name, standard=None):
    """Ein Feld aus einem Objekt ODER einem dict."""
    if isinstance(ding, dict):
        return ding.get(name, standard)
    return getattr(ding, name, standard)


def _zahl(wert, standard):
    try:
        return float(wert)
    except (TypeError, ValueError):
        return standard


def _einzeilig(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def segmente_lesen(antwort):
    """Whispers Antwort (verbose_json) -> [Segment]. Nimmt das Objekt aus dem
    openai-Paket genauso wie ein rohes dict; Kaputtes wird uebergangen. Ganz
    ohne Segmente, aber mit Text: ein Segment ab 0 (ohne Zeiten)."""
    roh = _feld(antwort, "segments", None)
    raus = []
    for s in roh or ():
        start = _zahl(_feld(s, "start", None), None)
        text = _einzeilig(_feld(s, "text", ""))
        if start is None or not text:
            continue
        ende = _zahl(_feld(s, "end", start), start)
        raus.append(Segment(
            start=start, ende=max(start, ende), text=text,
            avg_logprob=_zahl(_feld(s, "avg_logprob", 0.0), 0.0),
            no_speech_prob=_zahl(_feld(s, "no_speech_prob", 0.0), 0.0),
            compression_ratio=_zahl(_feld(s, "compression_ratio", 1.0), 1.0)))
    if not raus and roh is None:
        text = _einzeilig(_feld(antwort, "text", "") if antwort is not None else "")
        if text:
            raus.append(Segment(start=0.0, ende=0.0, text=text))
    return raus


def halluzination(seg):
    """Hat Whisper das erfunden? Nach Whispers eigenen Zahlen (Stille,
    Zuversicht, Wiederholung) oder nach den typischen Abspann-Saetzen."""
    text = _einzeilig(_feld(seg, "text", ""))
    if not re.search(r"\w", text):
        return True                         # leer oder nur Satzzeichen
    stille = _zahl(_feld(seg, "no_speech_prob", 0.0), 0.0)
    zuversicht = _zahl(_feld(seg, "avg_logprob", 0.0), 0.0)
    if stille >= STILLE_WAHRSCHEINLICH and zuversicht <= STILLE_LOGPROB:
        return True
    if zuversicht < LOGPROB_MIN:
        return True
    if _zahl(_feld(seg, "compression_ratio", 1.0), 1.0) > KOMPRESSION_MAX:
        return True
    if stille >= KURZ_STILLE and _KURZ_VERDAECHTIG.search(text):
        return True
    return any(muster.search(text) for muster in _HALLUZINATION)


def _vergleich(text):
    return re.sub(r"\W+", " ", str(text or "").lower()).strip()


def bereinigen(segmente):
    """Ohne Halluzinationen und ohne direkte Wiederholungen (Whisper bleibt
    gern in einer Schleife haengen und sagt denselben Satz zehnmal)."""
    raus = []
    for seg in segmente or ():
        if halluzination(seg):
            continue
        if raus and _vergleich(raus[-1].text) == _vergleich(seg.text):
            continue
        raus.append(seg)
    return raus


def clipzeit(sek, karte, ende=False):
    """Sekunde in der Sprecher-Datei -> Sekunde im Clip, ueber die Karte aus
    clipschnitt ([(start_datei, ende_datei, start_clip)]). Liegt die Stelle in
    einer der eingefuegten Pausen, zaehlt fuer einen Anfang das naechste
    Stueck, fuer ein Ende das vorige."""
    if not karte:
        return max(0.0, float(sek))
    for i, (a, e, c) in enumerate(karte):
        if sek < a:
            if ende and i > 0:
                a0, e0, c0 = karte[i - 1]
                return c0 + (e0 - a0)
            return float(c)
        if sek <= e:
            return c + (sek - a)
    a, e, c = karte[-1]
    return c + (e - a)


def zeilen_bauen(seg_je_uid, karten, namen):
    """{uid: [Segment]} -> [Zeile], nach Clip-Zeit verzahnt. Saetze desselben
    Sprechers mit weniger als ZUSAMMEN_SEK Pause werden eine Zeile - aber nur
    bis ZEILE_ZUSAMMEN_MAX Zeichen und ZEILE_SPANNE_SEK Sekunden: ein Monolog
    bleibt mehrere Zeilen, damit transkript_text sein Ende behalten kann. Ein
    Satz mit privaten Daten bleibt immer eine eigene Zeile - geschwaerzt (und
    vor der KI versteckt) wird dann nur er, nicht der ganze Monolog.
    karten: {uid: karte} oder {uid: WhisperAudio}; namen: {uid: Anzeigename}."""
    roh = []
    for uid, segmente in (seg_je_uid or {}).items():
        karte = (karten or {}).get(uid)
        karte = getattr(karte, "karte", karte) or []
        for seg in segmente or ():
            text = _einzeilig(_feld(seg, "text", ""))
            start = _zahl(_feld(seg, "start", 0.0), 0.0)
            if not text:
                continue
            ende = _zahl(_feld(seg, "ende", _feld(seg, "end", start)), start)
            a = clipzeit(start, karte)
            e = max(a, clipzeit(ende, karte, ende=True))
            roh.append((a, e, uid, text))
    roh.sort(key=lambda r: (r[0], str(r[2])))
    zeilen = []
    ende_davor = beginn = 0.0
    privat_davor = False
    for a, e, uid, text in roh:
        privat = _heikel(text)
        if (zeilen and zeilen[-1].uid == uid and a - ende_davor < ZUSAMMEN_SEK
                and not privat and not privat_davor
                and len(zeilen[-1].text) + 1 + len(text) <= ZEILE_ZUSAMMEN_MAX
                and max(e, ende_davor) - beginn <= ZEILE_SPANNE_SEK):
            zeilen[-1].text = f"{zeilen[-1].text} {text}"
            ende_davor = max(ende_davor, e)
            continue
        zeilen.append(Zeile(t=round(a, 2), uid=uid, name=str((namen or {}).get(uid) or "?"),
                            text=text))
        ende_davor, beginn, privat_davor = e, a, privat
    return zeilen


# =============================================================================
# Pruefen und Text
# =============================================================================
def _notlage(text):
    return bool(ai.FloAI._NOTLAGE_RE.search(str(text or "")))


def _hetze(text):
    try:
        import fun
        return bool(fun.ist_hetze(text))
    except Exception:  # noqa: BLE001 - ohne Erkennung entscheidet die KI-Grenze
        return False


def _heikel(text):
    try:
        import gehirn
        return bool(gehirn.Gehirn._heikel(text))
    except Exception:  # noqa: BLE001
        return False


def pruefen(zeilen):
    """Notsignal (je Sprecher), Hetze, private Daten (je Zeile). clips.py
    entscheidet daraus: Notsignal oder Hetze -> der Clip bleibt liegen;
    private Daten -> die Zeile wird im Transkript geschwaerzt. Befund.heikel
    sind Nummern in GENAU dieser Liste - transkript_text muss dieselbe Liste
    bekommen.

    Notsignal und private Daten werden auch ueber die Naht zweier Zeilen
    desselben Sprechers gesucht (je NAHT Zeichen): "ich will nicht" | "mehr
    leben" oder "0171" | "2345678" stehen nach einer Pause in zwei Zeilen.
    Eine private Angabe, die erst an der Naht entsteht, schwaerzt beide Zeilen.
    Hetze nicht - deren Ausnahmen (Zitat, Meta) brauchen den ganzen Satz, ein
    Ausschnitt machte aus einer Beschwerde eine Anklage."""
    befund = Befund()
    zeilen = list(zeilen or ())
    vorige = {}                         # uid -> Nummer seiner letzten Zeile
    for i, zeile in enumerate(zeilen):
        text = _einzeilig(zeile.text)
        if _notlage(text):
            befund.notlage_uids.add(zeile.uid)
        if _hetze(text):
            befund.hetze = True
        if _heikel(text):
            befund.heikel.add(i)
        j = vorige.get(zeile.uid)
        if j is not None:
            links, rechts = _einzeilig(zeilen[j].text)[-NAHT:], text[:NAHT]
            naht = f"{links} {rechts}"
            if _notlage(naht):
                befund.notlage_uids.add(zeile.uid)
            if _heikel(naht) and not _heikel(links) and not _heikel(rechts):
                befund.heikel.update((j, i))
        vorige[zeile.uid] = i
    return befund


def _kappen(text, grenze, hinten=False):
    """Auf eine Laenge kuerzen - mit '…' statt mitten im Wort. hinten=True
    behaelt das Ende statt des Anfangs (im Transkript steht die Pointe hinten)."""
    text = _einzeilig(text)
    if len(text) <= grenze:
        return text
    if hinten:
        kurz = text[len(text) - max(0, grenze - 1):].lstrip()
        leer = kurz.find(" ")
        if 0 <= leer < grenze // 2:
            kurz = kurz[leer + 1:].lstrip(" ,;:-")
        return "…" + kurz
    kurz = text[:max(0, grenze - 1)].rstrip()
    leer = kurz.rfind(" ")
    if leer > grenze // 2:
        kurz = kurz[:leer].rstrip(" ,;:-")
    return kurz + "…"


def _sauber(text, laenge=ZEILE_MAX, hinten=False):
    """Fuer Discord: gekuerzt, ohne Markdown, ohne Ping."""
    return (discord.utils.escape_markdown(_kappen(text, laenge, hinten))
            .replace("@", "@\u200b"))


def zeit_text(sek):
    """Sekunden -> 'm:ss'."""
    sek = max(0, int(sek or 0))
    return f"{sek // 60}:{sek % 60:02d}"


def dauer_text(sek):
    """30 -> '30 s', 60 -> '1 min', 90 -> '1:30 min', 300 -> '5 min'."""
    try:
        sek = max(0, int(round(float(sek))))
    except (TypeError, ValueError):
        sek = 0
    if sek < 60:
        return f"{sek} s"
    if sek % 60 == 0:
        return f"{sek // 60} min"
    return f"{sek // 60}:{sek % 60:02d} min"


def _zeile_text(zeile, geschwaerzt, laenge=ZEILE_MAX):
    """Eine Zeile fuers Embed; vom Text hoechstens 'laenge' Zeichen (vor dem
    Escapen), und zwar das Ende."""
    name = _sauber(zeile.name, 32) or "?"
    text = ZENSIERT if geschwaerzt else _sauber(zeile.text, laenge, hinten=True)
    return f"`{zeit_text(zeile.t)}` **{name}:** {text}"


def transkript_text(zeilen, max_zeichen=TRANSKRIPT_MAX, zensiert=frozenset()):
    """Das Transkript fuers Embed: "`m:ss` **Name:** Text" je Zeile, ohne
    Markdown und ohne Ping (@ bekommt ein unsichtbares Leerzeichen). Zeilen in
    'zensiert' (Nummern aus pruefen() fuer DIESELBE Liste) werden geschwaerzt.
    Zu lang: das ENDE bleibt, vorne steht '…' - geclippt wird, was gerade
    passiert ist, die Pointe steht hinten. Das gilt auch fuer eine einzelne
    ueberlange Zeile."""
    zeilen = list(zeilen or ())
    teile = [_zeile_text(zeile, i in zensiert) for i, zeile in enumerate(zeilen)]
    if not teile:
        return ""
    ganz = "\n".join(teile)
    if len(ganz) <= max_zeichen:
        return ganz
    behalten, laenge = [], 2                # "…\n"
    for teil in reversed(teile):
        if laenge + len(teil) + 1 > max_zeichen:
            break
        behalten.append(teil)
        laenge += len(teil) + 1
    if behalten:
        return "…\n" + "\n".join(reversed(behalten))
    # Schon die letzte Zeile allein ist zu lang: Zeit und Name bleiben, vom
    # Text das Ende - so viel, wie samt Escapes hineinpasst (Escapes machen
    # den Text laenger, daher ein paar Anlaeufe).
    if len(zeilen) - 1 not in zensiert:
        laenge = max_zeichen
        for _anlauf in range(8):
            teil = _zeile_text(zeilen[-1], False, laenge)
            if len(teil) <= max_zeichen:
                return teil
            laenge -= len(teil) - max_zeichen
            if laenge < 2:
                break
    # Nicht einmal Zeit und Name passen: vorne behalten, hinten kappen - ohne
    # ein halbes Escape-Zeichen stehen zu lassen.
    return teile[-1][:max(0, max_zeichen - 1)].rstrip("\\") + "…"


# =============================================================================
# Flos Kommentar
# =============================================================================
_BUCHSTABEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _kommentar_prompt(zeilen, dauer, heikel=None):
    """Das Transkript fuer die KI: Buchstaben statt Namen - auch wo jemand
    einen anderen beim Namen nennt -, ohne Zeilen mit privaten Daten (heikel:
    ihre Nummern aus pruefen(), sonst selbst ermittelt), und bei Ueberlaenge
    das Ende. Leer = nichts Brauchbares."""
    if heikel is None:
        heikel = pruefen(zeilen).heikel
    buchstaben, namen = {}, {}
    for zeile in zeilen:
        if zeile.uid not in buchstaben:
            buchstaben[zeile.uid] = _BUCHSTABEN[len(buchstaben) % len(_BUCHSTABEN)]
            name = _einzeilig(zeile.name)
            if len(name) >= 3:
                namen[name] = buchstaben[zeile.uid]
    teile = []
    for i, zeile in enumerate(zeilen):
        text = _einzeilig(zeile.text)
        if not text or i in heikel:
            continue
        for name, buchstabe in namen.items():
            text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", buchstabe, text, flags=re.IGNORECASE)
        teile.append(f"{buchstaben[zeile.uid]}: {text}")
    rumpf = "\n".join(teile)
    if not rumpf:
        return ""
    if len(rumpf) > KOMMENTAR_EINGABE_MAX:
        rumpf = "…" + rumpf[-KOMMENTAR_EINGABE_MAX:]
    return f"Clip, {dauer_text(dauer)} lang. Was gesagt wurde:\n{rumpf}"


def _jemand_am_boden(zeilen, befund):
    """Notsignal im Clip (auch ueber eine Zeilen-Naht, siehe pruefen) oder
    gerade eben irgendwo (basis merkt es sich)? Dann kein Spott - clips.py
    prueft das auch, doppelt haelt besser."""
    if befund.notlage_uids:
        return True
    try:
        import basis
        kuerzlich = basis.kuerzlich_notlage
    except Exception:  # noqa: BLE001
        return False
    return any(kuerzlich(zeile.uid) for zeile in zeilen)


async def kommentar(gid, zeilen, dauer):
    """Ein Satz von Flo zum Clip - oder None (KI aus, nichts gesagt, Notsignal,
    Frist um, Ausgabe unbrauchbar). Laeuft im Kontext des Servers (Name, Grenze)
    und als Hintergrundaufruf (eigenes Modell, kein Wiederholen). Wirft nie."""
    try:
        out = _kommentar_saeubern(await _kommentar_holen(gid, zeilen, dauer))
        return None if out and _nennt_jemanden(out, zeilen) else out
    except Exception:  # noqa: BLE001 - dann eben der Ersatz (auch bei Frist)
        log.debug("Clips: KI-Kommentar gescheitert", exc_info=True)
        return None


def _nennt_jemanden(out, zeilen):
    """Steht der Anzeigename eines Sprechers im Kommentar? Die KI sieht nur
    Buchstaben - aber Namen unter drei Zeichen ersetzt _kommentar_prompt nicht
    (sonst wuerde aus 'Er' jedes 'er'), und die stehen dann womoeglich im Text.
    Oeffentlich unter dem Clip soll niemand beim Namen genannt werden."""
    for name in {_einzeilig(zeile.name) for zeile in zeilen or ()}:
        if re.search(r"\w", name) and re.search(
                rf"(?<!\w){re.escape(name)}(?!\w)", out, flags=re.IGNORECASE):
            return True
    return False


def _kommentar_saeubern(out):
    """Was die KI liefert, kommt nur durch, wenn es keine Verweigerung (auf
    Englisch wie in ai.py, auf Deutsch nach _ABSAGE_RE), kein Notsignal/
    Hilfsangebot, keine Hetze und keine privaten Daten ist - ohne
    Anfuehrungszeichen, ohne @, gekuerzt."""
    out = _einzeilig(out).strip("\"'„“”‚‘’»«").strip()
    if (not out or ai.instance._ist_verweigerung(out) or _ABSAGE_RE.search(out)
            or ai.instance.ist_notlage(out, out) or _hetze(out) or _heikel(out)):
        return None
    out = out.replace("@", "").strip()
    return ai.instance._kuerzen(out, KOMMENTAR_MAX) if out else None


async def _kommentar_holen(gid, zeilen, dauer):
    """Die rohe Antwort der KI - oder None, bevor sie ueberhaupt gefragt wird."""
    zeilen = [z for z in (zeilen or ()) if _einzeilig(z.text)]
    if not zeilen:
        return None
    befund = pruefen(zeilen)
    if befund.hetze or _jemand_am_boden(zeilen, befund):
        return None
    if not ai.is_enabled():
        return None
    with ai.guild_kontext(gid):
        prompt = _kommentar_prompt(zeilen, dauer, befund.heikel)
        if not prompt:
            return None
        system = KOMMENTAR_SYSTEM.format(name=ai.bot_name(gid)) + ai.FloAI._GUARDRAIL
        return await asyncio.wait_for(ai.generate(
            prompt, system=system, temperature=1.0, max_tokens=120, hintergrund=True),
            KI_FRIST)


def ersatz_kommentar():
    """Ein Spruch aus dem Ersatz-Pool."""
    return random.choice(ERSATZ_KOMMENTARE)


# =============================================================================
# Bestenliste
# =============================================================================
_PLAETZE = ("🥇", "🥈", "🥉")


def _ganz(wert):
    try:
        return int(wert or 0)
    except (TypeError, ValueError):
        return 0


def rangliste_embed(eintraege, zeitraum, gid, jetzt=None):
    """'flo clips': die Clips mit den meisten 🔥, bei Gleichstand der neuere
    zuerst, hoechstens BRETT_MAX. eintraege: die Index-Eintraege aus clips.json
    ({mid, cid, vc, ts, von, dauer, sprecher, stimmen}) - als Liste oder als
    (id, eintrag)-Paare; ohne Flamme zaehlt keiner. zeitraum: 'woche', 'monat',
    'alle'/'top' oder eine Zahl Tage. Es zaehlen nur Clips aus dem Zeitraum
    (ts ab jetzt minus so viele Tage; jetzt = Wanduhr, fuer Tests stellbar) -
    clips darf also den ganzen Index reichen, die Woche bleibt die Woche.
    Links und Mentions statt Namen: clips speichert keine Namen, und eine
    Mention im Embed pingt nie."""
    if isinstance(zeitraum, (int, float)) and not isinstance(zeitraum, bool):
        tage = max(1, int(zeitraum))
        titel = ZEITRAEUME["woche"][1] if tage <= 7 else (
            ZEITRAEUME["monat"][1] if tage <= 31 else ZEITRAEUME["alle"][1])
    else:
        tage, titel = ZEITRAEUME.get(str(zeitraum or "woche").lower(), ZEITRAEUME["woche"])
    ab = (time.time() if jetzt is None else float(jetzt)) - tage * 86400
    gute = []
    for eintrag in eintraege or ():
        if isinstance(eintrag, tuple) and len(eintrag) == 2:
            eintrag = eintrag[1]
        if not isinstance(eintrag, dict):
            continue
        stimmen = eintrag.get("stimmen") or []
        if (isinstance(stimmen, list) and stimmen and _ganz(eintrag.get("mid"))
                and _zahl(eintrag.get("ts"), 0.0) >= ab):
            gute.append(eintrag)
    gute.sort(key=lambda e: (-len(e.get("stimmen") or []), -_ganz(e.get("ts"))))
    embed = discord.Embed(title=titel, color=FARBE)
    if not gute:
        embed.description = BRETT_LEER.format(tage=tage)
        return embed
    zeilen = []
    for platz, eintrag in enumerate(gute[:BRETT_MAX], 1):
        marke = _PLAETZE[platz - 1] if platz <= len(_PLAETZE) else f"**{platz}.**"
        link = (f"https://discord.com/channels/{_ganz(gid)}/{_ganz(eintrag.get('cid'))}/"
                f"{_ganz(eintrag.get('mid'))}")
        teile = [f"{marke} **{len(eintrag.get('stimmen') or [])}× 🔥** · "
                 f"[Clip · {dauer_text(eintrag.get('dauer'))}]({link})"]
        if _ganz(eintrag.get("vc")):
            teile.append(f"aus <#{_ganz(eintrag.get('vc'))}>")
        if _ganz(eintrag.get("von")):
            teile.append(f"von <@{_ganz(eintrag.get('von'))}>")
        if _ganz(eintrag.get("ts")):
            teile.append(f"<t:{_ganz(eintrag.get('ts'))}:R>")
        zeilen.append(" · ".join(teile))
    embed.description = "\n".join(zeilen)
    return embed
