"""Clip-Schnitt: aus mitgehoerten Opus-Paketen wird eine MP3 (fuer 'flo clip').

Was hier passiert
=================
clips.py haelt die letzten fuenf Minuten Voice als rohe Opus-Pakete im
Arbeitsspeicher. Will jemand einen Clip, baut clips.py daraus einen Auftrag
und reicht ihn hierher - in einem Worker-Thread (asyncio.to_thread), denn
Dekodieren und ffmpeg dauern Sekunden, und so lange darf der Event-Loop nicht
stehen. Deshalb gibt es hier NUR einfache Daten: keine discord-Objekte, kein
asyncio, kein Zustand ausser dem einmal gemerkten ffmpeg-Befund.

    1. platzieren()  legt die Pakete jeder Person auf die Zeitachse des Clips
                     (RTP-Zeitstempel innerhalb eines Redeschubs, Ankunftszeit
                     fuer den Schub als Ganzes - siehe dort)
    2. Dekodieren    je Person eine Mono-Rohdatei (Flo selbst: Stereo). Es
                     werden nur die Stellen mit Ton geschrieben, der Rest bleibt
                     ein Loch in der Datei und kostet keinen Plattenplatz.
    3. ffmpeg        mischt alle Spuren (amix), Flos eigene Musik leiser
                     (FLO_PEGEL), begrenzt (alimiter) und kodiert MP3
    4. Groesse       passt die MP3 nicht ins Budget (Discords Upload-Grenze),
                     geht die Bitrate runter - hoechstens VERSUCHE_MAX Versuche
    5. Whisper       auf Wunsch je Sprecher eine kleine 16-kHz-MP3 NUR aus den
                     Stellen, an denen er redet, mit einer Karte zurueck auf die
                     Clip-Zeit (die braucht clipstext.zeilen_bauen)

Warum ffmpeg mischt und nicht Python: es gibt kein numpy im Projekt, und
Python 3.13 (Produktion) hat kein audioop mehr. Pures Python braeuchte fuer
fuenf Minuten Ton mehrere Minuten - ffmpeg wenige Sekunden.

Fehler kommen als RenderFehler mit einem Grund heraus, den clips.py in einen
Satz uebersetzt: "leer" (nichts zu hoeren), "ffmpeg" (Werkzeug kaputt oder
fehlt), "zu_gross" (passt auch klein nicht ins Budget), "zeit" (Frist um oder
der Auftrag wurde abgebrochen). Frist und Abbruch-Schalter gelten auch, waehrend
ffmpeg rechnet: es wird dann sofort beendet - 'Mikro aus' oder das
Herunterfahren warten nie auf das Ende eines Fuenf-Minuten-Mixes.

Temporaere Dateien liegen in einem privaten Ordner ('flo-clip-<pid>-...',
Rechte 0700) und werden im finally geloescht - die MP3 geht nur als Bytes
zurueck. Ordner, die ein Absturz mitten im Schnitt liegen liess, raeumt
alte_reste_weg() beim Start und beim Herunterfahren weg; die PID im Namen
sorgt dafuer, dass dabei nie der laufende Schnitt eines ANDEREN Prozesses
verschwindet (ein Testlauf auf dem Server neben dem echten Bot).

Beim Import passiert nichts: kein ffmpeg-Aufruf, kein Thread, keine Datei.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field

import discord

log = logging.getLogger("dcbot.clipschnitt")

#: Discord-Voice laeuft immer mit 48 kHz.
RATE = 48000
#: Bytes je Sample und Kanal (s16le).
_BREITE = 2
#: Ein Standard-Rahmen: 20 ms bei 48 kHz. Gilt, wenn ein Paket kaputt ist.
RAHMEN_STANDARD = 960

# --- Wann ein neuer Redeschub beginnt (siehe platzieren) -----------------------
#: Der RTP-Zeitstempel springt um mehr als 0,2 s ueber das erwartete Ende.
SCHUB_TS_LUECKE = 0.2
#: Die Ankunft liegt 0,15 s spaeter, als der Zeitstempel weitergelaufen ist -
#: der Sender hat pausiert, ohne seinen Zeitstempel weiterzuzaehlen.
SCHUB_ANKUNFT_LUECKE = 0.15
#: Mehr als 50 Pakete fehlen am Stueck.
SCHUB_SEQ_LUECKE = 50
#: Bis zu so vielen verlorenen Rahmen fuellt der Decoder die Luecke selbst
#: (Packet Loss Concealment). Laengere Luecken bleiben still - erfundener Ton
#: ueber 60 ms klingt schlimmer als eine kurze Stille.
PLC_MAX = 3

# --- Mischung -------------------------------------------------------------------
#: Flos eigene Musik im Clip: hoerbar, aber unter den Stimmen.
FLO_PEGEL = 0.6
#: Gleicht leise und laute Sprecher an (ffmpeg ab 4.4).
_SPRACHNORM = "speechnorm=e=6:r=0.0005:l=1"
#: Kein Clipping, wenn zwei gleichzeitig reden: Grenze knapp unter 0 dBFS, und
#: level=false, damit der Begrenzer nicht selbst wieder hochregelt.
_BEGRENZER = "alimiter=limit=0.89:level=false"
#: Wer als Kuenstler im ID3-Kopf steht.
KUENSTLER = "Flo"
#: So lang darf der Titel im ID3-Kopf werden.
TITEL_MAX = 80

# --- Bitrate --------------------------------------------------------------------
#: Die Leiter, von oben nach unten. Bei 9,5 MiB bekommt jede Laenge bis 300 s
#: die oberste Stufe; die unteren sind fuer Server mit kleiner Upload-Grenze.
LEITER = (192, 160, 128, 112, 96, 80, 64, 48, 40, 32)
#: Sicherheitsabschlag auf die rechnerische Bitrate (ID3-Kopf, LAME-Rahmen).
LUFT = 0.95
#: So oft wird hoechstens kodiert, bevor "zu_gross" gilt.
VERSUCHE_MAX = 3
#: Laenger darf ein einzelner ffmpeg-Lauf nie dauern.
FFMPEG_FRIST = 90.0
#: So oft sieht ein laufendes ffmpeg nach Frist und Abbruch-Schalter - ein
#: 'Mikro aus' wartet so nie auf das Ende eines Fuenf-Minuten-Mixes.
FFMPEG_HALT_SEK = 0.25
#: Alle so viele Rahmen sieht das Dekodieren nach, ob die Frist noch reicht.
FRIST_PRUEFEN_ALLE = 500

# --- Ton fuer Whisper -----------------------------------------------------------
#: Wer kuerzer redet, bekommt kein Transkript (kostet nur Kontingent).
WHISPER_MIN_SEK = 1.5
#: Hoechstens so viele Sprecher je Clip (die mit dem meisten Ton zuerst).
WHISPER_MAX_SPRECHER = 8
#: Stille zwischen zwei Redestuecken in der Whisper-Datei - und zugleich die
#: Luecke, unter der zwei Stuecke einfach zusammenbleiben.
WHISPER_PAUSE = 0.4
WHISPER_RATE = 16000
WHISPER_KBPS = 48

#: Namensanfang der temporaeren Ordner. Dahinter kommt die PID.
PRAEFIX = "flo-clip-"


# =============================================================================
# Datenformen
# =============================================================================
@dataclass
class Spurdaten:
    """Was clips.py je Person aus dem Puffer reicht."""

    uid: int
    name: str
    bot: bool
    pakete: list              # [(t, strang, seq, ts, opus)] - seq/ts entfaltet


@dataclass
class Auftrag:
    """Ein Schnitt. 'bis' ist eine time.monotonic()-Zeit (das Fensterende),
    'dauer' die Laenge in Sekunden, 'budget' die Hoechstgroesse in Bytes."""

    bis: float
    dauer: float
    stimmen: list             # [Spurdaten]
    flo: list                 # [(t, opus)] - Flos eigener Ton; leer = ohne
    budget: int
    titel: str
    whisper: bool = False
    tmp_basis: "str | None" = None   # None = CLIP_TMP_DIR, sonst das System-Temp
    frist: float = 120.0
    # Zusatz (nicht im Entwurf): eine Funktion ohne Argumente; liefert sie
    # True, bricht der Schnitt beim naechsten Halt mit "zeit" ab. clips.py
    # haengt hier sein Abschalten an (Herunterfahren, Mikro aus).
    abbruch: object = None


@dataclass
class WhisperAudio:
    """Der Ton EINES Sprechers fuer Whisper: nur die Stellen, an denen er
    redet, dicht hintereinander. 'karte' fuehrt zurueck auf die Clip-Zeit:
    [(start_in_der_datei, ende_in_der_datei, start_im_clip)] in Sekunden."""

    mp3: bytes
    karte: list

    @property
    def dauer(self):
        """Laenge der Datei in Sekunden (das Ende des letzten Stuecks)."""
        return float(self.karte[-1][1]) if self.karte else 0.0


@dataclass
class Ergebnis:
    mp3: bytes
    kbps: int
    dauer: float
    stimm_sek: dict           # {uid: Sekunden mit Ton im Clip}
    whisper: dict             # {uid: WhisperAudio}
    hinweise: list = field(default_factory=list)   # kurze Schluessel fuers Log


class RenderFehler(Exception):
    """Der Schnitt ging nicht. grund: leer | ffmpeg | zu_gross | zeit."""

    GRUENDE = ("leer", "ffmpeg", "zu_gross", "zeit")

    def __init__(self, grund, meldung=""):
        self.grund = grund if grund in self.GRUENDE else "ffmpeg"
        self.meldung = meldung
        super().__init__(f"{self.grund}: {meldung}" if meldung else self.grund)


@dataclass
class Schub:
    """Ein Redeschub: zusammenhaengende Rahmen eines Strangs. 'start' ist die
    Position des ersten Rahmens in Samples ab Clip-Beginn; die Rahmen tragen
    ihren Abstand dazu: [(abstand, opus | None, samples)]. None heisst: hier
    fehlt ein Paket, der Decoder soll die Luecke verschleiern (PLC)."""

    strang: int
    nr: int                   # der wievielte Schub dieses Strangs (Dekodier-Reihenfolge)
    start: int
    rahmen: list

    @property
    def ende(self):
        return self.start + max((a + n for a, _o, n in self.rahmen), default=0)


# =============================================================================
# Platzieren
# =============================================================================
def rahmen_samples(opus):
    """Wie viele Samples (48 kHz) ein Opus-Paket traegt - aus dem TOC-Byte
    (RFC 6716, 3.1). Rechnet genau wie libopus' opus_packet_get_nb_frames mal
    opus_packet_get_samples_per_frame, braucht aber keine libopus: platzieren
    soll auch ohne sie laufen und testbar sein. 0 = kein gueltiges Paket."""
    if not opus:
        return 0
    toc = opus[0]
    if toc & 0x80:                          # CELT: 2,5 / 5 / 10 / 20 ms
        je = (RATE << ((toc >> 3) & 0x3)) // 400
    elif (toc & 0x60) == 0x60:              # Hybrid: 10 / 20 ms
        je = RATE // 50 if toc & 0x08 else RATE // 100
    else:                                   # SILK: 10 / 20 / 40 / 60 ms
        stufe = (toc >> 3) & 0x3
        je = RATE * 60 // 1000 if stufe == 3 else (RATE << stufe) // 100
    zahl = toc & 0x3
    if zahl == 0:
        rahmen = 1
    elif zahl != 3:
        rahmen = 2
    elif len(opus) < 2:
        return 0
    else:
        rahmen = opus[1] & 0x3F
    gesamt = je * rahmen
    # Opus erlaubt hoechstens 120 ms je Paket - alles darueber ist Muell.
    return gesamt if 0 < gesamt <= RATE * 120 // 1000 else 0


def ist_stille(opus):
    """Discords Stille-Rahmen (0xF8 0xFF 0xFE) und alles noch Kleinere: das
    schicken Clients am Ende jedes Satzes. Zaehlt nicht als Stimme."""
    return opus is None or len(opus) <= 3


def platzieren(pakete, bis, dauer, rate=RATE):
    """Legt die Pakete EINER Person auf die Zeitachse des Clips [bis-dauer, bis].

    pakete: [(t, strang, seq, ts, opus)] - t ist die Ankunft (monotonic),
    seq/ts sind schon entfaltet (kein Ueberlauf). Rueckgabe: [Schub], nach
    Start sortiert, auf das Fenster [0, dauer*rate) beschnitten.

    Warum nicht einfach nach Ankunftszeit: das Netz verzoegert jedes Paket
    unterschiedlich (Jitter). Innerhalb eines Redeschubs ist der
    RTP-Zeitstempel die Wahrheit - der Sender zaehlt ihn Sample fuer Sample.
    Nur WO der Schub als Ganzes liegt, sagt die Ankunft: der Anker ist das am
    wenigsten verzoegerte Paket, zurueckgerechnet auf den Schub-Anfang.

    Ein neuer Schub beginnt, wenn der Zeitstempel springt (Sprechpause mit
    weiterlaufender Uhr), die Ankunft dem Zeitstempel davonlaeuft (Pause ohne
    weiterlaufende Uhr) oder sehr viele Pakete am Stueck fehlen. Fehlen nur
    1-3 Rahmen, setzt der Decoder dort Verschleierung ein. Ueberlappen zwei
    Schuebe derselben Person (Schaetzfehler, Neuverbindung mit neuem Strang),
    rueckt der spaetere ans Ende des frueheren - sonst ueberschriebe er ihn.

    Flos eigene Spur geht durch dieselbe Funktion (siehe flo_pakete)."""
    fenster_start = bis - dauer
    gesamt = int(round(dauer * rate))
    je_strang = {}
    for paket in pakete or ():
        try:
            t, strang, seq, ts, opus = paket
            eintrag = (float(t), int(seq), int(ts), bytes(opus))
        except (TypeError, ValueError):
            continue
        je_strang.setdefault(strang, []).append(eintrag)

    schuebe = []
    for strang, liste in je_strang.items():
        liste.sort(key=lambda p: p[1])
        sauber = []
        for p in liste:
            if sauber and p[1] == sauber[-1][1]:
                continue                    # doppelt angekommen
            sauber.append(p)
        for nr, gruppe in enumerate(_schuebe_trennen(sauber, rate)):
            schub = _schub_bauen(strang, nr, gruppe, fenster_start, rate)
            if schub is not None:
                schuebe.append(schub)

    # Ueberlappungen derselben Person aufloesen: der spaetere rueckt nach.
    schuebe.sort(key=lambda s: (s.start, s.strang, s.nr))
    ende_bisher = None
    for schub in schuebe:
        if ende_bisher is not None and schub.start < ende_bisher:
            schub.start = ende_bisher
        ende_bisher = schub.ende if ende_bisher is None else max(ende_bisher, schub.ende)

    # Auf das Fenster beschneiden. Ein Rahmen, der nur halb hineinragt, bleibt
    # (das Schreiben schneidet ihn zurecht).
    raus = []
    for schub in schuebe:
        schub.rahmen = [r for r in schub.rahmen
                        if schub.start + r[0] + r[2] > 0 and schub.start + r[0] < gesamt]
        if schub.rahmen:
            raus.append(schub)
    return raus


def _schuebe_trennen(pakete, rate):
    """[(t, seq, ts, opus)] nach seq sortiert -> Liste von Schueben."""
    gruppen, aktuell = [], []
    for p in pakete:
        if aktuell:
            t0, s0, ts0, o0 = aktuell[-1]
            erwartet = ts0 + (rahmen_samples(o0) or RAHMEN_STANDARD)
            neu = (p[2] - erwartet > SCHUB_TS_LUECKE * rate
                   or (p[0] - t0) - (p[2] - ts0) / rate > SCHUB_ANKUNFT_LUECKE
                   or p[1] - s0 > SCHUB_SEQ_LUECKE)
            if neu:
                gruppen.append(aktuell)
                aktuell = []
        aktuell.append(p)
    if aktuell:
        gruppen.append(aktuell)
    return gruppen


def _schub_bauen(strang, nr, gruppe, fenster_start, rate):
    """Ein Schub aus seinen Paketen: Anker, Abstaende, PLC-Platzhalter."""
    ts_null = gruppe[0][2]
    anker = min(t - (ts - ts_null) / rate for t, _s, ts, _o in gruppe)
    start = int(round((anker - fenster_start) * rate))
    rahmen = []
    vorher = None                           # (seq, abstand, samples)
    for _t, seq, ts, opus in gruppe:
        abstand = ts - ts_null
        n = rahmen_samples(opus) or RAHMEN_STANDARD
        if vorher is not None:
            fehlen = seq - vorher[0] - 1
            if 1 <= fehlen <= PLC_MAX:
                for k in range(1, fehlen + 1):
                    luecke = vorher[1] + vorher[2] * k
                    if luecke >= abstand:
                        break               # Zeitstempel sagt: da ist kein Platz
                    rahmen.append((luecke, None, vorher[2]))
        rahmen.append((abstand, opus, n))
        vorher = (seq, abstand, n)
    return Schub(strang=strang, nr=nr, start=start, rahmen=rahmen) if rahmen else None


def flo_pakete(eigen):
    """Flos eigene Spur [(t, opus)] in die Form, die platzieren versteht:
    Strang 0, laufende Nummer als seq, die Samples als Zeitstempel. Pausiert
    die Musik, kommt kein Rahmen - die Ankunft laeuft davon, und platzieren
    beginnt dort einen neuen Schub.

    Verziehen wird die umgekehrte Reihenfolge (opus, t): so ruft der Abgriff
    in voiceempfang seine Senke auf (eigen_senke(opus, t)), und ein Dreher an
    dieser Naht kostete sonst JEDEN Clip mit Musik. Muell (kein bytes, keine
    Zeit) wird uebergangen."""
    raus = []
    ts = 0
    for k, eintrag in enumerate(eigen or ()):
        try:
            t, opus = eintrag
        except (TypeError, ValueError):
            continue
        if isinstance(t, (bytes, bytearray, memoryview)):
            t, opus = opus, t
        if not isinstance(opus, (bytes, bytearray, memoryview)) or not opus:
            continue
        try:
            t = float(t)
        except (TypeError, ValueError):
            continue
        opus = bytes(opus)
        raus.append((t, 0, k, ts, opus))
        ts += rahmen_samples(opus) or RAHMEN_STANDARD
    return raus


def stimm_samples(schuebe, gesamt):
    """Wie viele Samples dieser Schuebe echte Stimme sind (ohne Stille-Rahmen
    und ohne PLC), auf das Fenster beschnitten."""
    summe = 0
    for schub in schuebe:
        for abstand, opus, n in schub.rahmen:
            if ist_stille(opus):
                continue
            a = max(0, schub.start + abstand)
            e = min(gesamt, schub.start + abstand + n)
            if e > a:
                summe += e - a
    return summe


def stimm_bereiche(schuebe, gesamt, rate=RATE):
    """[(start, ende)] in Samples: wo diese Person wirklich redet - Stille-
    Rahmen am Rand eines Schubs zaehlen nicht, und was naeher als
    WHISPER_PAUSE beieinanderliegt, wird ein Stueck."""
    roh = []
    for schub in schuebe:
        stimme = [(schub.start + a, schub.start + a + n) for a, o, n in schub.rahmen
                  if not ist_stille(o)]
        if not stimme:
            continue
        a = max(0, min(s for s, _e in stimme))
        e = min(gesamt, max(e for _s, e in stimme))
        if e > a:
            roh.append((a, e))
    roh.sort()
    pause = int(WHISPER_PAUSE * rate)
    raus = []
    for a, e in roh:
        if raus and a - raus[-1][1] < pause:
            raus[-1] = (raus[-1][0], max(raus[-1][1], e))
        else:
            raus.append((a, e))
    return raus


# =============================================================================
# ffmpeg
# =============================================================================
#: Einmal nachgesehen, danach gemerkt: {"grund", "ffmpeg", "nice", "normalize",
#: "speechnorm", "limiter"}. None = noch nicht nachgesehen.
_FAEHIG = None


def ffmpeg_pruefen(neu=False):
    """Kann dieses ffmpeg, was der Schnitt braucht? Rueckgabe: "" oder der
    Grund, warum nicht. Einmal nachgesehen, danach gemerkt (neu=True: noch
    einmal).

    Pflicht ist nur libmp3lame. amix 'normalize' (ffmpeg 4.4+), speechnorm
    (4.4+) und alimiter sind Kuer: fehlt eins, mischt rendern ohne und merkt
    es in den Hinweisen. Im Docker-Image steckt ein aelteres ffmpeg (5.1) als
    auf dem Server (7.1) - deshalb gefragt und nicht vorausgesetzt."""
    global _FAEHIG
    if _FAEHIG is None or neu:
        _FAEHIG = _ffmpeg_messen()
    return _FAEHIG["grund"]


def _ffmpeg_messen():
    stand = {"grund": "", "ffmpeg": shutil.which("ffmpeg") or "",
             "nice": shutil.which("nice") or "",
             "normalize": False, "speechnorm": False, "limiter": False}
    if not stand["ffmpeg"]:
        stand["grund"] = "ffmpeg fehlt"
        return stand

    def frag(*argumente):
        try:
            fertig = subprocess.run([stand["ffmpeg"], "-hide_banner", *argumente],
                                    capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return (fertig.stdout or b"") + (fertig.stderr or b"")

    kodierer = frag("-encoders")
    if kodierer is None:
        stand["grund"] = "ffmpeg antwortet nicht"
        return stand
    if b"libmp3lame" not in kodierer:
        stand["grund"] = "ffmpeg ohne libmp3lame (kein MP3)"
        return stand
    stand["normalize"] = b"normalize" in (frag("-h", "filter=amix") or b"")
    stand["speechnorm"] = b"Filter speechnorm" in (frag("-h", "filter=speechnorm") or b"")
    stand["limiter"] = b"Filter alimiter" in (frag("-h", "filter=alimiter") or b"")
    log.info("Clips: ffmpeg kann MP3 (amix normalize %s, speechnorm %s, alimiter %s).",
             "ja" if stand["normalize"] else "nein",
             "ja" if stand["speechnorm"] else "nein",
             "ja" if stand["limiter"] else "nein")
    return stand


def _vorne(faehig):
    """Programmaufruf-Anfang: 'nice -n 10' (der Schnitt soll der Musik nie die
    CPU wegnehmen) und ffmpeg mit den immer gleichen Schaltern."""
    vorne = [faehig["nice"], "-n", "10"] if faehig.get("nice") else []
    return vorne + [faehig["ffmpeg"], "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]


def titel_saeubern(titel):
    """ID3-Titel: einzeilig, ohne Steuerzeichen, hoechstens TITEL_MAX Zeichen.
    ffmpeg bekommt ihn als eigenes Argument (keine Shell) - es geht nur darum,
    dass im Player nichts Kaputtes steht."""
    text = "".join(z if unicodedata.category(z)[0] != "C" else " " for z in str(titel or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > TITEL_MAX:
        text = text[:TITEL_MAX - 1].rstrip() + "…"
    return text or "Clip"


def mix_befehl(eingaben, ziel, dauer, kbps, titel, faehig):
    """Der ffmpeg-Aufruf fuers Mischen. eingaben: [(pfad, kanaele, ist_flo)]."""
    befehl = _vorne(faehig)
    for pfad, kanaele, _flo in eingaben:
        befehl += ["-f", "s16le", "-ar", str(RATE), "-ac", str(kanaele), "-i", pfad]
    ketten, marken = [], []
    for i, (_pfad, _kanaele, flo) in enumerate(eingaben):
        kette = "aformat=sample_fmts=fltp:channel_layouts=stereo"
        if flo:
            kette += f",volume={FLO_PEGEL}"
            marke = f"[f{i}]"
        else:
            if faehig.get("speechnorm"):
                kette += "," + _SPRACHNORM
            marke = f"[s{i}]"
        ketten.append(f"[{i}:a]{kette}{marke}")
        marken.append(marke)
    n = len(eingaben)
    mix = f"{''.join(marken)}amix=inputs={n}:duration=longest:dropout_transition=0"
    if faehig.get("normalize"):
        mix += ":normalize=0"
    elif n > 1:
        # Altes amix teilt jeden Eingang durch n - das holt den Pegel zurueck.
        mix += f",volume={n}"
    if faehig.get("limiter"):
        mix += "," + _BEGRENZER
    ketten.append(mix + "[m]")
    return befehl + [
        "-filter_complex", ";".join(ketten), "-map", "[m]", "-t", f"{dauer:.3f}",
        "-ar", str(RATE), "-ac", "2", "-c:a", "libmp3lame", "-b:a", f"{kbps}k",
        "-id3v2_version", "3", "-metadata", f"title={titel_saeubern(titel)}",
        "-metadata", f"artist={KUENSTLER}", ziel]


def bitrate_fuer(dauer, budget):
    """Die hoechste Stufe der Leiter, deren MP3 rechnerisch ins Budget passt
    (LUFT fuer Kopf und Rahmen). Bei 9,5 MiB bekommt jede Laenge bis 300 s
    192 kbps. 0 = nicht einmal die unterste Stufe passt."""
    if dauer <= 0:
        return LEITER[0]
    hoechstens = budget * 8 / dauer / 1000 * LUFT
    for kbps in LEITER:
        if kbps <= hoechstens:
            return kbps
    # Unter der untersten Stufe: ohne Luft ginge sie noch? Dann versuchen.
    return LEITER[-1] if LEITER[-1] * 1000 / 8 * dauer <= budget else 0


def naechste_stufe(kbps, groesse, budget):
    """Die MP3 war zu gross: eine Stufe, die nach der gemessenen Groesse passt -
    nicht nur die naechste, sonst kostete jede Stufe einen Versuch. 0 = keine."""
    ziel = kbps * budget / max(1, groesse) * LUFT
    for stufe in LEITER:
        if stufe < kbps and stufe <= ziel:
            return stufe
    return 0


class _Frist:
    """Die Frist des Auftrags plus sein Abbruch-Schalter."""

    def __init__(self, sekunden, abbruch=None):
        self.ende = time.monotonic() + max(0.0, float(sekunden))
        self.abbruch = abbruch

    def rest(self):
        return self.ende - time.monotonic()

    def pruefen(self):
        if self.abbruch is not None:
            try:
                weg = bool(self.abbruch())
            except Exception:  # noqa: BLE001 - ein kaputter Schalter bricht nichts ab
                weg = False
            if weg:
                raise RenderFehler("zeit", "abgebrochen")
        if self.rest() <= 0:
            raise RenderFehler("zeit", "Frist abgelaufen")


def _ffmpeg_laufen(befehl, frist, was):
    """Ein ffmpeg-Lauf mit Zeitgrenze. Fehler werden zu RenderFehler; im Log
    steht hoechstens das Ende von ffmpegs Fehlerausgabe (keine Namen, keine
    Inhalte - ffmpeg schreibt mit -loglevel error nur, was schiefging).

    Waehrend ffmpeg laeuft, sieht der Lauf alle FFMPEG_HALT_SEK nach Frist und
    Abbruch-Schalter (frueher blockierte subprocess.run bis zu FFMPEG_FRIST,
    und 'Mikro aus' oder das Herunterfahren mussten so lange warten). Wird
    abgebrochen, stirbt ffmpeg sofort - kein Prozess ueberlebt seinen Auftrag.
    communicate() leert dabei die Fehlerausgabe, ffmpeg haengt nie an einer
    vollen Pipe."""
    frist.pruefen()
    grenze = max(1.0, min(FFMPEG_FRIST, frist.rest()))
    ende = time.monotonic() + grenze
    try:
        prozess = subprocess.Popen(befehl, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("Clips: ffmpeg (%s) liess sich nicht starten: %s", was, exc)
        raise RenderFehler("ffmpeg", str(exc)) from exc
    try:
        while True:
            try:
                _aus, fehler = prozess.communicate(
                    timeout=max(0.01, min(FFMPEG_HALT_SEK, ende - time.monotonic())))
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= ende:
                log.warning("Clips: ffmpeg (%s) nach %.0f s abgebrochen.", was, grenze)
                raise RenderFehler("zeit", f"ffmpeg {was} zu langsam")
            frist.pruefen()
    except BaseException:
        _abschiessen(prozess)
        raise
    if prozess.returncode != 0:
        rest = (fehler or b"").decode("utf-8", "replace").strip()[-300:]
        log.warning("Clips: ffmpeg (%s) gescheitert, Code %s: %s", was, prozess.returncode, rest)
        raise RenderFehler("ffmpeg", f"Code {prozess.returncode}")


def _abschiessen(prozess):
    """Ein ffmpeg, das keiner mehr braucht: sofort beenden und einsammeln (kein
    Zombie). Haengt selbst das, bleibt er eben liegen - werfen darf hier nichts,
    sonst ginge der eigentliche Grund verloren."""
    try:
        prozess.kill()
    except OSError:
        pass
    try:
        prozess.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        log.debug("Clips: ffmpeg liess sich nicht einsammeln", exc_info=True)


# =============================================================================
# Dekodieren
# =============================================================================
class _Mono(discord.opus.Decoder):
    """Opus-Decoder mit EINEM Kanal: Discord-Clients schicken Stereo-Opus,
    gesprochen wird aber mono - libopus mischt beim Dekodieren selbst herunter.
    Halbiert Rechenzeit und Plattenplatz."""

    CHANNELS = 1
    SAMPLE_SIZE = _BREITE * CHANNELS
    FRAME_SIZE = discord.opus.Decoder.SAMPLES_PER_FRAME * SAMPLE_SIZE


def _decoder(kanaele):
    try:
        return _Mono() if kanaele == 1 else discord.opus.Decoder()
    except Exception as exc:  # noqa: BLE001 - OpusNotLoaded & Co.
        raise RenderFehler("ffmpeg", "libopus fehlt") from exc


def _dekodieren(decoder, opus):
    """Ein Rahmen -> PCM. Kaputt oder fehlend: Verschleierung (PLC)."""
    try:
        return decoder.decode(opus, fec=False)
    except discord.opus.OpusError:
        pass
    except Exception:  # noqa: BLE001 - z. B. ein Paket, das kein bytes ist
        log.debug("Clips: Rahmen nicht dekodierbar", exc_info=True)
    try:
        return decoder.decode(None, fec=False)
    except Exception:  # noqa: BLE001
        return b""


def spur_schreiben(pfad, schuebe, kanaele, gesamt, frist):
    """Dekodiert die Schuebe und schreibt sie an ihre Stelle in eine Rohdatei
    (s16le). Nur Stellen mit Ton werden geschrieben: seek + write, am Ende
    truncate auf die volle Laenge - der Rest ist ein Loch in der Datei
    (sparse) und kostet keinen Plattenplatz. Ein Decoder je Strang, in der
    Reihenfolge, in der der Strang gesendet hat. Gesprungen wird nur, wo ein
    Rahmen nicht direkt an den vorigen anschliesst - sonst sammelt der Puffer
    die Rahmen eines Satzes zu wenigen grossen Schreibvorgaengen."""
    breite = _BREITE * kanaele
    decoder = {}
    zaehler = 0
    ort = 0                                 # Byte, hinter dem zuletzt geschrieben wurde
    with open(pfad, "wb") as datei:
        for schub in sorted(schuebe, key=lambda s: (s.strang, s.nr)):
            dec = decoder.get(schub.strang)
            if dec is None:
                dec = decoder[schub.strang] = _decoder(kanaele)
            for abstand, opus, _n in schub.rahmen:
                zaehler += 1
                if zaehler % FRIST_PRUEFEN_ALLE == 0:
                    frist.pruefen()
                pcm = _dekodieren(dec, opus)
                if not pcm:
                    continue
                pos = schub.start + abstand
                if pos < 0:
                    pcm = pcm[-pos * breite:]
                    pos = 0
                platz = (gesamt - pos) * breite
                if platz <= 0 or not pcm:
                    continue
                if len(pcm) > platz:
                    pcm = pcm[:platz]
                if pos * breite != ort:
                    datei.seek(pos * breite)
                datei.write(pcm)
                ort = pos * breite + len(pcm)
        datei.truncate(gesamt * breite)
    return zaehler


# =============================================================================
# Temporaere Ordner
# =============================================================================
def _tmp_basis(basis=None):
    """Wo die temporaeren Ordner liegen: Auftrag > CLIP_TMP_DIR > System-Temp."""
    ordner = basis or os.getenv("CLIP_TMP_DIR", "").strip()
    if ordner:
        try:
            os.makedirs(ordner, mode=0o700, exist_ok=True)
            return ordner
        except OSError:
            log.warning("Clips: CLIP_TMP_DIR %r nicht nutzbar - nehme das System-Temp.", ordner)
    return tempfile.gettempdir()


def _tmp_anlegen(basis=None):
    """Ein frischer privater Ordner (mkdtemp legt ihn mit 0700 an)."""
    try:
        return tempfile.mkdtemp(prefix=f"{PRAEFIX}{os.getpid()}-", dir=_tmp_basis(basis))
    except OSError as exc:
        raise RenderFehler("ffmpeg", f"kein Temp-Ordner: {exc}") from exc


def _pid_aus(name):
    treffer = re.fullmatch(re.escape(PRAEFIX) + r"([0-9]{1,10})-.*", name)
    return int(treffer.group(1)) if treffer else 0


def _lebt(pid):
    """Laeuft dieser Prozess noch? Im Zweifel ja - dann bleibt sein Ordner."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def alte_reste_weg(basis=None):
    """Raeumt liegengebliebene 'flo-clip-*'-Ordner weg (Absturz mitten im
    Schnitt). clips ruft das beim Start und beim Herunterfahren.

    Geloescht wird nur, was sicher uns gehoert: ein Ordner (kein Link) mit dem
    Praefix, demselben Besitzer, und dessen Prozess nicht mehr laeuft - oder
    der eigene. Ein Testlauf neben dem echten Bot nimmt dem also nie den
    Schnitt weg. Rueckgabe: wie viele Ordner weg sind."""
    ordner = _tmp_basis(basis)
    try:
        namen = os.listdir(ordner)
    except OSError:
        return 0
    weg = 0
    for name in namen:
        if not name.startswith(PRAEFIX):
            continue
        pfad = os.path.join(ordner, name)
        try:
            if os.path.islink(pfad) or not os.path.isdir(pfad):
                continue
            if os.stat(pfad).st_uid != os.getuid():
                continue
        except OSError:
            continue
        pid = _pid_aus(name)
        if pid and pid != os.getpid() and _lebt(pid):
            continue
        shutil.rmtree(pfad, ignore_errors=True)
        if not os.path.exists(pfad):
            weg += 1
    if weg:
        log.info("Clips: %d liegengebliebene Schnitt-Ordner weggeraeumt.", weg)
    return weg


# =============================================================================
# Der Schnitt
# =============================================================================
def rendern(auftrag):
    """Der ganze Schnitt - im Worker-Thread. Rueckgabe: Ergebnis, sonst
    RenderFehler. Nichts bleibt liegen: der Temp-Ordner geht im finally weg.

    Auch ein Fehler, mit dem niemand gerechnet hat (ein kaputter Auftrag, ein
    Programmierfehler), kommt als RenderFehler('ffmpeg') heraus - mit der
    ganzen Spur im Log. clips.py muss so nur eine Fehlerart kennen."""
    try:
        return _rendern(auftrag)
    except RenderFehler:
        raise
    except Exception as exc:  # noqa: BLE001 - siehe oben
        log.exception("Clips: Schnitt unerwartet gescheitert")
        raise RenderFehler("ffmpeg", type(exc).__name__) from exc


def _rendern(auftrag):
    frist = _Frist(auftrag.frist, auftrag.abbruch)
    grund = ffmpeg_pruefen()
    if grund:
        raise RenderFehler("ffmpeg", grund)
    faehig = dict(_FAEHIG)
    dauer = float(auftrag.dauer)
    gesamt = int(round(dauer * RATE))
    if gesamt <= 0:
        raise RenderFehler("leer", "Dauer 0")
    hinweise = []
    if not faehig.get("speechnorm"):
        hinweise.append("speechnorm_fehlt")
    if not faehig.get("normalize"):
        hinweise.append("normalize_fehlt")
    if not faehig.get("limiter"):
        hinweise.append("limiter_fehlt")
    start = time.monotonic()
    tmp = _tmp_anlegen(auftrag.tmp_basis)
    try:
        eingaben, stimm_sek, bereiche = [], {}, {}
        stimmen = auftrag.stimmen.values() if isinstance(auftrag.stimmen, dict) else auftrag.stimmen
        for i, spur in enumerate(stimmen or ()):
            frist.pruefen()
            schuebe = platzieren(spur.pakete, auftrag.bis, dauer)
            if not schuebe:
                continue
            pfad = os.path.join(tmp, f"v{i}.raw")
            spur_schreiben(pfad, schuebe, 1, gesamt, frist)
            eingaben.append((pfad, 1, False))
            stimm_sek[spur.uid] = round(stimm_samples(schuebe, gesamt) / RATE, 2)
            if auftrag.whisper and not spur.bot:
                bereiche[spur.uid] = (pfad, stimm_bereiche(schuebe, gesamt))
        if auftrag.flo:
            frist.pruefen()
            schuebe = platzieren(flo_pakete(auftrag.flo), auftrag.bis, dauer)
            if schuebe:
                pfad = os.path.join(tmp, "flo.raw")
                spur_schreiben(pfad, schuebe, 2, gesamt, frist)
                eingaben.append((pfad, 2, True))
        if not eingaben:
            raise RenderFehler("leer", "keine Spur im Fenster")

        mp3, kbps = _mischen(eingaben, tmp, dauer, auftrag, faehig, frist, hinweise)
        whisper = {}
        if auftrag.whisper:
            whisper = _whisper_ton(bereiche, stimm_sek, tmp, faehig, frist, hinweise)
        log.info("Clips: Schnitt fertig - %d Spur(en), %.0f s, %d kbps, %.2f MB, %.1f s gerechnet.",
                 len(eingaben), dauer, kbps, len(mp3) / 1048576, time.monotonic() - start)
        return Ergebnis(mp3=mp3, kbps=kbps, dauer=dauer, stimm_sek=stimm_sek,
                        whisper=whisper, hinweise=hinweise)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _mischen(eingaben, tmp, dauer, auftrag, faehig, frist, hinweise):
    """Mischen und kodieren - mit der Bitrate-Leiter. -> (mp3, kbps)"""
    budget = int(auftrag.budget)
    kbps = bitrate_fuer(dauer, budget)
    if not kbps:
        raise RenderFehler("zu_gross", f"{dauer:.0f} s passen nicht in {budget} Bytes")
    ziel = os.path.join(tmp, "clip.mp3")
    for versuch in range(1, VERSUCHE_MAX + 1):
        _ffmpeg_laufen(mix_befehl(eingaben, ziel, dauer, kbps, auftrag.titel, faehig),
                       frist, "Mischen")
        try:
            with open(ziel, "rb") as datei:
                mp3 = datei.read()
        except OSError as exc:
            raise RenderFehler("ffmpeg", "keine MP3 entstanden") from exc
        if not mp3:
            raise RenderFehler("ffmpeg", "leere MP3")
        if len(mp3) <= budget:
            if versuch > 1:
                hinweise.append("bitrate_gesenkt")
            return mp3, kbps
        neu = naechste_stufe(kbps, len(mp3), budget) if versuch < VERSUCHE_MAX else 0
        log.info("Clips: %d kbps ergaben %d Bytes (Budget %d) - %s.", kbps, len(mp3), budget,
                 f"neuer Versuch mit {neu} kbps" if neu else "aufgegeben")
        if not neu:
            break
        kbps = neu
    raise RenderFehler("zu_gross", f"auch {kbps} kbps passen nicht in {budget} Bytes")


def _whisper_ton(bereiche, stimm_sek, tmp, faehig, frist, hinweise):
    """Je Sprecher (ab WHISPER_MIN_SEK, hoechstens WHISPER_MAX_SPRECHER, die
    mit dem meisten Ton zuerst) eine 16-kHz-Mono-MP3 nur aus den Stellen mit
    Stimme. Geht dabei etwas schief, gibt es GAR KEIN Whisper-Material: ein
    Transkript, dem ein Sprecher fehlt, waere schlimmer als keins."""
    wer = sorted((uid for uid in bereiche if stimm_sek.get(uid, 0) >= WHISPER_MIN_SEK),
                 key=lambda uid: -stimm_sek.get(uid, 0))[:WHISPER_MAX_SPRECHER]
    raus = {}
    try:
        for nr, uid in enumerate(wer):
            quelle, stuecke = bereiche[uid]
            if not stuecke:
                continue
            frist.pruefen()
            dicht = os.path.join(tmp, f"w{nr}.raw")
            karte = _dicht_schreiben(quelle, dicht, stuecke)
            ziel = os.path.join(tmp, f"w{nr}.mp3")
            befehl = _vorne(faehig) + [
                "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", dicht,
                "-ar", str(WHISPER_RATE), "-ac", "1", "-c:a", "libmp3lame",
                "-b:a", f"{WHISPER_KBPS}k", ziel]
            _ffmpeg_laufen(befehl, frist, "Whisper")
            with open(ziel, "rb") as datei:
                raus[uid] = WhisperAudio(mp3=datei.read(), karte=karte)
    except (RenderFehler, OSError) as exc:
        if getattr(exc, "meldung", "") == "abgebrochen":
            raise                       # abgebrochen heisst: der ganze Auftrag
        log.info("Clips: Ton fuer das Transkript fehlt (%s) - Clip kommt ohne.",
                 getattr(exc, "grund", type(exc).__name__))
        hinweise.append("whisper_fehlt")
        return {}
    return raus


def _dicht_schreiben(quelle, ziel, stuecke):
    """Kopiert die Stuecke [(start, ende)] (Samples, mono) dicht hintereinander,
    mit WHISPER_PAUSE Stille dazwischen. -> karte in Sekunden."""
    pause = bytes(int(WHISPER_PAUSE * RATE) * _BREITE)
    karte, pos = [], 0
    with open(quelle, "rb") as q, open(ziel, "wb") as z:
        for i, (a, e) in enumerate(stuecke):
            if i:
                z.write(pause)
                pos += len(pause) // _BREITE
            anfang = pos
            q.seek(a * _BREITE)
            rest = (e - a) * _BREITE
            while rest > 0:
                stueck = q.read(min(rest, 1 << 20))
                if not stueck:
                    break
                z.write(stueck)
                rest -= len(stueck)
                pos += len(stueck) // _BREITE
            karte.append((round(anfang / RATE, 3), round(pos / RATE, 3), round(a / RATE, 3)))
    return karte
