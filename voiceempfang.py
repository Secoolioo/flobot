"""Voice-Empfang: Flo hoert im Sprachkanal mit - Pakete rein, Opus-Rahmen raus.

Diese Datei kennt KEINE Clips. Sie macht aus den UDP-Paketen einer
Voice-Verbindung Opus-Rahmen je Sprecher, mit Ankunftszeit, und reicht sie an
eine Senke, die ihr jemand gibt (clips.py: Ringpuffer.einfuegen). Was davon
behalten, angekuendigt, geschnitten oder geloescht wird, entscheidet clips.py -
hier steht kein Text, kein Store, keine Einstellung.

discord.py 2.7 kann senden, aber nicht empfangen. Es bringt alles mit, was es
dafuer braucht, nur eben intern - und genau diese Stellen prueft kompatibel():

    VoiceConnectionState(hook=...)   jede JSON-Nachricht des Voice-Websockets
                                     (op 4, 5, 11, 13 ...) geht NACHDEM
                                     discord.py sie verarbeitet hat auch an
                                     EmpfangsClient._ws_hook
    add_socket_listener(cb)          der SocketReader-Thread reicht jedes
                                     UDP-Paket an cb (Empfaenger.roh)
    VoiceClient._get_voice_packet    sieht jeden eigenen Opus-Rahmen VOR der
                                     DAVE-Verschluesselung (Flos Musik im Clip)

Fehlt eine davon (discord.py-Update), bleibt der Empfang aus - mit einem Grund
im Log statt mit einem Traceback im Voice-Thread.

DER WEG EINES PAKETS
====================
    UDP -> roh()            SocketReader-Thread: mit Ankunftszeit ablegen und
                            den Loop EINMAL wecken. Sonst nichts.
        -> abarbeiten()     Event-Loop, je Paket:
             RTP-Kopf           RTCP, Video und Muell fliegen raus (gezaehlt)
             Opt-out            ist die SSRC schon zugeordnet und ihr Mensch
                                raus: weg, bevor auch nur der Transport aufgeht
             Transport          aead_xchacha20_poly1305_rtpsize mit dem
                                Schluessel aus op 4 (der alte gilt noch 5 s)
             SSRC -> Discord-ID aus op 5; unbekannt -> bis 2,5 s halten
             Opt-out            VOR DAVE: wer raus ist, wird nie entschluesselt
             DAVE               Ende-zu-Ende (davey). Klappt es noch nicht
                                (Schluessel kommt spaeter, Thread-Kollision),
                                bis 10 s nochmal versuchen - je SSRC streng in
                                Reihenfolge (siehe unten)
             seq/ts entfalten   16 bzw. 32 Bit laufen ueber
        -> senke(uid, strang, seq, ts, opus, t)
                                t ist IMMER die Ankunftszeit - auch nach dem
                                Halten und nach dem Nochmal-Versuch

    strang = verbindung_nr << 48 | generation << 32 | ssrc. Jede neue
    Verbindung (anderer Client, op 4 nach einem Neuaufbau) zaehlt
    verbindung_nr hoch, und die Generation einer SSRC zaehlt hoch, wenn ihr
    Mensch geht (op 13) oder sie jemand anderem gehoert (op 5): die Spuren
    werden nie ueber einen Neuaufbau oder einen neuen RTP-Strom hinweg
    vermischt, seq und ts fangen dort ja woanders an. Fuer clips ist strang
    nur ein Schluessel zum Gruppieren.

DAVE NUR VORWAERTS
==================
    davey nimmt je Absender nur Nonces nach vorn an: der ERSTE Rahmen, der
    aufgeht, legt die Untergrenze fest, ohne die aelteren als fehlend zu
    merken (davey 0.1.7, CipherManager; spaeter Uebersprungenes darf
    nachkommen). Geht ein juengerer Rahmen zuerst an davey, sind die aelteren
    fuer immer zu. Deshalb:
      - ein neuer DAVE-Stand (Sitzung, Version, bereit, Epoche - so kommen
        Schluessel an) schickt erst alles Wartende nochmal durch, in
        Reihenfolge, BEVOR ein neuer Rahmen an davey geht;
      - ein neuer Rahmen einer SSRC, von der noch etwas wartet, laesst erst
        das Wartende ran (aeltestes zuerst);
      - geht ein Rahmen auf, waehrend Aelteres desselben Absenders weiter
        scheitert, ist das Aeltere tot (alte Epoche) - es fliegt sofort raus,
        statt 10 s lang vergeblich probiert zu werden;
      - die Probe (SSRC ohne op 5) raet mit dem AELTESTEN DAVE-Rahmen zuerst.

DIE THREADS
===========
    SocketReader   roh(): deque.append + hoechstens ein call_soon_threadsafe
                   (und ein Zaehler, wenn der Eingang ueberlaeuft). Kein
                   Parsen, kein Krypto, kein Log - jede Millisekunde dort
                   fehlt dem naechsten Paket, und eine Ausnahme dort wuerde
                   discord.py nur ins Log kippen.
    AudioPlayer    EmpfangsClient._get_voice_packet -> eigen_senke(opus, t).
                   Die Senke darf nur anhaengen (deque, atomar).
    Event-Loop     alles andere: Websocket-Haken, Entschluesseln (auch DAVE -
                   dessen MLS-Zustand lebt ohnehin hier), Halten, Nochmal,
                   Senke. Deshalb braucht nichts hier ein Lock.

Wo was aufgerufen wird (alles aus clips.py, ausser es steht dabei):
    kompatibel()                 setup(): "" = es geht, sonst der Grund
    EmpfangsClient               music.verbindungs_optionen -> channel.connect(cls=...)
    Empfaenger(gid, senke, opt_out=...)
                                 beim Start einer Mithoer-Sitzung, dazu
                                 anmelden(gid, e) und e.anhaengen(vc)
    vc.eigen_senke = ...         Flos eigene Spur (None nimmt sie weg)
    e.abarbeiten()               vor einem Schnappschuss (was gerade ankam)
    e.takt()                     im Clips-Takt; solange etwas wartet, plant es
                                 sich zusaetzlich selbst alle TAKT_SEK
    e.vergessen(uid)             Opt-out: Gehaltenes dieses Menschen weg
    e.abhaengen(), abmelden(gid) Sitzung vorbei
    e.bei_verbindung             Rueckruf bei op 4 (clips gleicht dann ab). Wurde
                                 Flo verschoben, hat der Empfaenger sich da schon
                                 selbst abgehaengt: neuer Kanal, keine Ansage
    vc.aufwecken()               Discord schickt einem stummen Bot evtl. nichts
    vc.stand(), e.stand(), e.zaehler()
                                 'flo clip status' und die 5-Minuten-Logzeile
    vc._ws_hook                  discord.py, aus poll_event() (Loop-Thread)
    e.roh                        discord.py, aus dem SocketReader-Thread

Beim Import passiert nichts: kein Thread, keine Datei, kein Netz.
"""

import asyncio
import inspect
import logging
import re
import struct
import time
from collections import deque
from dataclasses import dataclass

import discord
from discord import voice_state as _voice_state

try:
    import nacl.secret as _nacl_secret
except ImportError:  # pragma: no cover - ohne PyNaCl gibt es auch kein Voice
    _nacl_secret = None

try:
    import davey
except ImportError:  # pragma: no cover - dito (discord.py verlangt davey)
    davey = None

log = logging.getLogger("dcbot.voiceempfang")

#: RTP-Nutzlasttyp, unter dem Discord Opus schickt. Video (und alles andere)
#: hat einen anderen und fliegt raus.
OPUS_PT = 0x78
#: Der Opus-Stille-Rahmen (discord.opus.OPUS_SILENCE): 20 ms nichts.
STILLE = b"\xf8\xff\xfe"
#: So enden DAVE-verschluesselte Rahmen (der "magic marker" am Rahmenende).
#: Ein Rahmen ohne ihn ist schlichtes Opus - erlaubt nur, solange DAVE aus ist
#: oder davey den Absender durchlaesst (Uebergang).
DAVE_MARKE = b"\xfa\xfa"
#: Die einzige Transport-Verschluesselung, die hier gelesen wird - die, die
#: discord.py als erste waehlt. Die anderen hat Discord 2024 abgeschaltet.
MODUS = "aead_xchacha20_poly1305_rtpsize"

#: Pakete einer SSRC ohne Zuordnung (op 5 kam noch nicht) so lange halten ...
HALTEN_UNBEKANNT = 2.5
#: ... hoechstens so viele je SSRC (gut 3 s Ton) ...
HALTEN_UNBEKANNT_PAKETE = 150
#: ... und hoechstens so viele SSRCs auf einmal.
HALTEN_UNBEKANNT_SSRCS = 50
#: So lange bekommt ein Rahmen, den DAVE (noch) nicht aufmacht, weitere Versuche ...
HALTEN_DAVE = 10.0
#: ... hoechstens so viele je SSRC (gut 10 s Ton) ...
HALTEN_DAVE_PAKETE = 512
#: ... und hoechstens so viele SSRCs auf einmal.
HALTEN_DAVE_SSRCS = 50
#: Deckel fuer den Eingang (roh -> abarbeiten). Kommt der Loop nicht
#: hinterher, faellt das Aelteste raus - gezaehlt als 'ueberlauf'.
EINGANG_MAX = 4096
#: Nach einem Schluesselwechsel (op 4) gilt der alte noch so lange: Pakete, die
#: unterwegs waren, sind noch mit ihm verschluesselt.
ALT_SCHLUESSEL_SEK = 5.0
#: Solange etwas gehalten wird oder auf DAVE wartet, plant sich takt() selbst
#: in diesem Abstand neu.
TAKT_SEK = 0.25
#: DAVE nochmal versuchen: sofort, wenn sich (Version, bereit, Epoche) geaendert
#: hat - sonst hoechstens so oft.
NOCHMAL_ABSTAND = 1.0
#: So viele Pakete je Weckruf, dann kommt erst der Rest des Loops wieder dran.
STAPEL_MAX = 1024
#: So viele Stille-Rahmen schickt aufwecken().
WECK_RAHMEN = 5
#: Raten statt warten: eine SSRC, die so lange ohne op 5 gehalten wird und
#: DAVE-Rahmen bringt, wird mit den Schluesseln der noch nicht zugeordneten
#: Leute im Call probehalber entschluesselt ...
PROBE_AB = 1.0
#: ... hoechstens so oft je SSRC ...
PROBE_ALLE = 5.0
#: ... mit hoechstens so vielen Leuten je Versuch ...
PROBE_MAX = 25
#: ... und hoechstens so vielen davey-Versuchen je Takt. Geraten wird mit dem
#: aeltesten DAVE-Rahmen zuerst; einer aus einer alten Epoche geht mit
#: niemandem auf und kostet einen Versuch je Kandidat (gemessen ~1,5 us).
PROBE_VERSUCHE = 1000

#: Kleinstes Paket, das ueberhaupt etwas tragen kann: RTP-Kopf + Nonce + Tag.
_RTP_MIN = 12 + 4 + 16

# Die Websocket-Opcodes, auf die der Haken hoert (discord.gateway.DiscordVoiceWebSocket).
_OP_SITZUNG = 4      # SESSION_DESCRIPTION: Modus, Schluessel, DAVE-Version
_OP_SPRICHT = 5      # SPEAKING: ssrc -> user_id
_OP_DA = 11          # CLIENTS_CONNECT: wer im Call ist
_OP_VERBUNDEN = 12   # CLIENT_CONNECT (aelteres Format: user_id + audio_ssrc)
_OP_WEG = 13         # CLIENT_DISCONNECT

#: Was zaehler() liefert - alle Schluessel gibt es immer, auch bei 0. clips
#: zieht davon die 60-s-Zahlen fuer 'flo clip status' ab.
ZAEHLER = (
    "roh",                  # Pakete aus dem Eingang verarbeitet
    "gespeichert",          # an die Senke gegeben
    "rtcp",                 # RTCP (Berichte, Feedback, XR) - kein Ton
    "fremd",                # anderer Nutzlasttyp (Video ...)
    "kaputt",               # kein lesbarer RTP-Kopf
    "modus_fremd",          # Verbindung nutzt eine andere Verschluesselung
    "kein_schluessel",      # noch kein Schluessel (vor op 4)
    "transport_fehler",     # Transport-Entschluesselung gescheitert
    "alter_schluessel",     # ging nur noch mit dem vorigen Schluessel
    "leer",                 # nach dem Entschluesseln nichts uebrig
    "unbekannt",            # SSRC ohne Zuordnung -> gehalten
    "unbekannt_verfallen",  # gehalten, nie zugeordnet, weg
    "unbekannt_voll",       # Haltedeckel erreicht, weg
    "geraten",              # SSRC per Probe-Entschluesselung zugeordnet
    "raus",                 # Opt-out - verworfen, BEVOR DAVE es sah
    "dave_ok",              # markierter Rahmen, mit DAVE entschluesselt
    "dave_klar",            # ohne DAVE durchgelassen (DAVE aus / Durchlass / Stille)
    "dave_nochmal",         # DAVE ging (noch) nicht -> wartet auf Nochmal
    "dave_verworfen",       # aufgegeben: nach HALTEN_DAVE, am Deckel, oder tot
                            # (ein juengerer desselben Absenders ging auf)
    "senke_fehler",         # die Senke hat geworfen
)


# --- Kleinteile ----------------------------------------------------------------
@dataclass(slots=True)
class RtpKopf:
    """Der Kopf eines RTP-Pakets - soweit der Empfang ihn braucht."""

    padding: bool       # P-Bit: am Ende der Nutzlast haengen Fuellbytes
    erweiterung: bool   # X-Bit: hinter den CSRCs steht eine Kopf-Erweiterung
    cc: int             # Zahl der CSRCs (je 4 Bytes)
    marker: bool
    pt: int             # Nutzlasttyp (Opus = OPUS_PT)
    seq: int            # 16 Bit, laeuft ueber
    ts: int             # 32 Bit, 48 kHz, laeuft ueber
    ssrc: int
    aad_ende: int       # bis hierher ist das Paket unverschluesselt (die AAD)
    ext_woerter: int    # Rumpf der Erweiterung in 32-Bit-Woertern (verschluesselt)


def rtp_kopf(p):
    """Den RTP-Kopf lesen - oder None, wenn es keiner ist (zu kurz, Version
    nicht 2, Erweiterung passt nicht ins Paket).

    rtpsize heisst: unverschluesselt (und als AAD mitgeprueft) sind die 12
    Bytes Kopf, die CSRCs und - falls das X-Bit steht - die 4 Bytes
    Erweiterungs-KOPF. Der Erweiterungs-RUMPF ist mitverschluesselt und steht
    vorne im Klartext (siehe transport_entschluesseln)."""
    try:
        if len(p) < _RTP_MIN or p[0] >> 6 != 2:
            return None
        b0, b1 = p[0], p[1]
        cc = b0 & 0x0F
        erweiterung = bool(b0 & 0x10)
        aad_ende = 12 + 4 * cc
        woerter = 0
        if erweiterung:
            if len(p) < aad_ende + 4:
                return None
            woerter = struct.unpack_from(">H", p, aad_ende + 2)[0]
            aad_ende += 4
        if len(p) < aad_ende + 4 + 16:          # Nonce + Tag muessen noch passen
            return None
        seq, ts, ssrc = struct.unpack_from(">HII", p, 2)
        return RtpKopf(bool(b0 & 0x20), erweiterung, cc, bool(b1 & 0x80), b1 & 0x7F,
                       seq, ts, ssrc, aad_ende, woerter)
    except Exception:  # noqa: BLE001 - None, Text, sonst was: kein RTP
        return None


def ist_rtcp(p):
    """RTCP statt RTP? Ton ist da nie drin. Getrennt wird wie in RFC 5761: das
    zweite Byte 192-223 ist RTCP - Berichte (200-204) genauso wie Feedback und
    XR (205-207). Ein Opus-Paket mit Marker hat 0xF8 im zweiten Byte - kein
    Treffer."""
    try:
        return len(p) >= 2 and 192 <= p[1] <= 223
    except Exception:  # noqa: BLE001
        return False


def transport_entschluesseln(p, kopf, box):
    """aead_xchacha20_poly1305_rtpsize aufmachen -> die Nutzlast (bytes) oder
    None, wenn nichts uebrig bleibt.

        AAD        p[:aad_ende]
        Geheimtext p[aad_ende:-4]   (mit 16 Bytes Tag)
        Nonce      die letzten 4 Bytes + 20 Nullbytes

    Danach vorne den Erweiterungs-Rumpf abschneiden und - bei gesetztem P-Bit -
    hinten die Fuellbytes (ihre Zahl steht im letzten Byte; nur wenn sie
    passt). Wirft nacl.exceptions.CryptoError bei falschem Schluessel oder
    manipuliertem Paket - der Aufrufer zaehlt das."""
    aad = kopf.aad_ende
    klar = box.decrypt(bytes(p[aad:-4]), bytes(p[:aad]), bytes(p[-4:]) + bytes(20))
    if kopf.ext_woerter:
        klar = klar[kopf.ext_woerter * 4:]
    if kopf.padding and klar:
        fuell = klar[-1]
        if 0 < fuell <= len(klar):
            klar = klar[:-fuell]
    return klar or None


def entfalten(alt_ext, neu, bits):
    """Einen ueberlaufenden Zaehler (seq: 16 Bit, ts: 32 Bit) fortschreiben.

    alt_ext ist der bisher hoechste entfaltete Wert (None beim ersten Paket -
    dann gilt der rohe). Gewaehlt wird der Wert, der zu 'neu' passt und am
    naechsten an alt_ext liegt (+- halber Bereich): 65535 -> 0 wird 65536, ein
    verspaetetes 65534 nach der 0 wird 65534 und nicht 131070."""
    if alt_ext is None:
        return neu
    umfang = 1 << bits
    abstand = (neu - alt_ext) % umfang
    if abstand >= umfang >> 1:
        abstand -= umfang
    return alt_ext + abstand


def _id(wert):
    """Discord-ID oder SSRC aus einer Websocket-Nachricht (Zahl oder Ziffern als
    Text) - sonst None. True/False sind keine IDs, auch wenn Python sie fuer
    Zahlen haelt."""
    if isinstance(wert, bool):
        return None
    if isinstance(wert, int):
        return wert if wert >= 0 else None
    if isinstance(wert, str) and re.fullmatch(r"[0-9]{1,20}", wert):
        return int(wert)
    return None


def _schleife(client):
    """Der Event-Loop, in dem dieser Client lebt (discord.VoiceClient.loop) -
    sonst der laufende, sonst None (dann weckt roh() niemanden, und wer
    abarbeiten() direkt ruft, bekommt trotzdem alles)."""
    loop = getattr(client, "loop", None)
    if isinstance(loop, asyncio.AbstractEventLoop):
        return loop
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _kanal_von(client):
    """Die ID des Sprachkanals, in dem der Client gerade sitzt (oder None)."""
    return getattr(getattr(client, "channel", None), "id", None)


def _dave_kennung(st):
    """(Version, Sitzung, bereit, Epoche) - aendert sich davon etwas, lohnt ein
    neuer DAVE-Versuch sofort. davey kann beim Lesen "already borrowed" werfen,
    wenn der Player-Thread gerade verschluesselt - dann eben 'unbekannt'."""
    dave = getattr(st, "dave_session", None)
    try:
        bereit = bool(getattr(dave, "ready", False))
        epoche = getattr(dave, "epoch", None)
    except Exception:  # noqa: BLE001
        bereit, epoche = None, None
    return (getattr(st, "dave_protocol_version", 0), id(dave) if dave is not None else 0,
            bereit, epoche)


def kompatibel():
    """"" wenn discord.py, PyNaCl und davey die Stellen haben, auf die der
    Empfang baut - sonst der Grund (fuer die Logzeile in clips.setup).

    Geprueft wird, was hier wirklich benutzt wird: der hook-Parameter von
    VoiceConnectionState, add/remove_socket_listener, _get_voice_packet und
    create_connection_state am VoiceClient, dass discord.py rtpsize noch
    waehlen kann, nacl.secret.Aead und davey.MediaType."""
    try:
        zustand = getattr(_voice_state, "VoiceConnectionState", None)
        if zustand is None:
            return "discord.py hat keinen VoiceConnectionState mehr"
        try:
            parameter = inspect.signature(zustand.__init__).parameters
        except (TypeError, ValueError):
            return "VoiceConnectionState laesst sich nicht pruefen"
        if "hook" not in parameter:
            return "VoiceConnectionState nimmt keinen hook mehr"
        for name in ("add_socket_listener", "remove_socket_listener"):
            if not callable(getattr(zustand, name, None)):
                return f"VoiceConnectionState.{name} fehlt"
        for name in ("_get_voice_packet", "create_connection_state"):
            if not callable(getattr(discord.VoiceClient, name, None)):
                return f"VoiceClient.{name} fehlt"
        if MODUS not in (getattr(discord.VoiceClient, "supported_modes", ()) or ()):
            return f"discord.py waehlt {MODUS} nicht mehr"
        if getattr(_nacl_secret, "Aead", None) is None:
            return "PyNaCl ohne nacl.secret.Aead (zu alt?)"
        if getattr(davey, "MediaType", None) is None:
            return "davey fehlt (oder kennt MediaType nicht)"
    except Exception as exc:  # noqa: BLE001 - die Pruefung selbst darf nie werfen
        return f"Pruefung gescheitert ({type(exc).__name__})"
    return ""


# --- Wer gerade wo hoert ---------------------------------------------------------
#: gid -> Empfaenger. clips meldet an und ab; der Websocket-Haken fragt hier,
#: wem er eine Zuordnung (op 5) oder einen Neuaufbau (op 4) melden soll.
_REGISTER = {}


def anmelden(gid, empfaenger):
    """Diesen Empfaenger fuer den Server eintragen (ein vorheriger fliegt raus)."""
    _REGISTER[int(gid)] = empfaenger


def abmelden(gid, empfaenger=None):
    """Austragen. Mit 'empfaenger' nur, wenn genau der eingetragen ist - ein
    spaetes Aufraeumen wirft so keinen neueren raus. Gibt den Ausgetragenen
    zurueck (oder None)."""
    try:
        gid = int(gid)
    except (TypeError, ValueError):
        return None
    if empfaenger is None or _REGISTER.get(gid) is empfaenger:
        return _REGISTER.pop(gid, None)
    return None


def fuer(gid):
    """Der Empfaenger dieses Servers - oder None."""
    try:
        return _REGISTER.get(int(gid))
    except (TypeError, ValueError):
        return None


# --- Der Client ------------------------------------------------------------------
class EmpfangsClient(discord.VoiceClient):
    """Ein discord.VoiceClient, der zusaetzlich zuhoert.

    Ohne Empfaenger verhaelt er sich exakt wie der normale: der Haken merkt
    sich nur, welche SSRC wem gehoert, und die Abzweigung fuer die eigene Spur
    ist ohne eigen_senke ein einziges 'is None'. Ob Flo taub sitzt, entscheidet
    weiter die Musik (connect-Flags) bzw. clips - nicht diese Klasse."""

    #: Flos eigene Spur: eigen_senke(opus, t). clips setzt sie je Client
    #: (Ringpuffer.eigen_anhaengen) und nimmt sie mit None wieder weg. Laeuft im
    #: AudioPlayer-Thread - die Senke darf nur anhaengen.
    eigen_senke = None
    #: Wie oft diese Verbindung aufgebaut wurde (op 4) - fuer 'flo clip status'.
    verbindungen = 0
    _kanal_id = None
    _abzweig_gemeldet = False
    _letzter_player = None

    def __init__(self, client, channel):
        super().__init__(client, channel)
        self._empfang_vorbereiten()

    def _empfang_vorbereiten(self):
        """Der eigene Zustand. Eigene Methode, damit eine Test-Attrappe
        (object.__new__, ohne Socket-Thread) ihn genauso bekommt."""
        #: SSRC -> Discord-ID, aus op 5. Gehoert dem Loop-Thread.
        self.ssrc_zu_uid = {}
        #: Wer laut op 11/5 im Call ist - nur zur Auskunft (stand, Probe).
        self.im_call = set()
        self.verbindungen = 0
        self._kanal_id = None
        #: Der zuletzt gestartete AudioPlayer (siehe _player_lebt).
        self._letzter_player = None

    def create_connection_state(self):
        """Wie discord.py, nur mit unserem Haken am Voice-Websocket."""
        return _voice_state.VoiceConnectionState(self, hook=self._ws_hook)

    # -- Websocket --------------------------------------------------------------
    async def _ws_hook(self, ws, msg):
        """Jede JSON-Nachricht des Voice-Websockets, NACHDEM discord.py sie
        verarbeitet hat (gateway.received_message). Laeuft in poll_event(): eine
        Ausnahme hier beendet den Poller der Verbindung - deshalb wird hier
        ALLES gefangen."""
        try:
            self._ws_nachricht(msg)
        except Exception:  # noqa: BLE001 - der Poller darf nie sterben
            log.debug("Empfang: Websocket-Nachricht nicht verstanden", exc_info=True)

    def _ws_nachricht(self, msg):
        op = msg.get("op")
        d = msg.get("d")
        if not isinstance(d, dict):
            return
        if op in (_OP_SPRICHT, _OP_VERBUNDEN):
            uid = _id(d.get("user_id"))
            ssrc = _id(d.get("ssrc" if op == _OP_SPRICHT else "audio_ssrc"))
            # SSRC 0 heisst "kein Ton" (op 12 meldet so auch Leute ohne Mikro).
            if uid is None or not ssrc or ssrc > 0xFFFFFFFF:
                return
            self.ssrc_zu_uid[ssrc] = uid
            self.im_call.add(uid)
            empfaenger = fuer(self._gid())
            if empfaenger is not None:
                empfaenger.ssrc_bekannt(ssrc, uid, client=self)
        elif op == _OP_WEG:
            uid = _id(d.get("user_id"))
            if uid is None:
                return
            for ssrc in [s for s, u in self.ssrc_zu_uid.items() if u == uid]:
                del self.ssrc_zu_uid[ssrc]
            self.im_call.discard(uid)
            empfaenger = fuer(self._gid())
            if empfaenger is not None:
                empfaenger.nutzer_weg(uid)
        elif op == _OP_DA:
            ids = d.get("user_ids")
            for roh in ids if isinstance(ids, (list, tuple)) else ():
                uid = _id(roh)
                if uid is not None:
                    self.im_call.add(uid)
        elif op == _OP_SITZUNG:
            self.verbindungen += 1
            kanal = getattr(self.channel, "id", None)
            # Anderer Kanal (verschoben): die alten SSRCs gehoeren zu einem
            # anderen Call. Neuaufbau im selben Kanal: sie bleiben gueltig.
            if self._kanal_id is not None and kanal != self._kanal_id:
                self.ssrc_zu_uid.clear()
                self.im_call.clear()
            self._kanal_id = kanal
            empfaenger = fuer(self._gid())
            if empfaenger is not None:
                empfaenger.client_verbunden(self)

    def _gid(self):
        try:
            return self.channel.guild.id
        except Exception:  # noqa: BLE001
            return None

    # -- eigene Spur --------------------------------------------------------------
    def _eigene_senke(self):
        """eigen_senke - am Client oder an der Klasse gesetzt, und zwar so, wie
        sie gesetzt wurde: eine schlichte Funktion an der KLASSE wuerde beim
        Lesen ueber self sonst an den Client gebunden und bekaeme ihn als
        erstes Argument. Am Client gesetzt gilt es auch als None (weggenommen)."""
        if "eigen_senke" in self.__dict__:
            return self.__dict__["eigen_senke"]
        for klasse in type(self).__mro__:
            if "eigen_senke" in klasse.__dict__:
                return klasse.__dict__["eigen_senke"]
        return None

    def _get_voice_packet(self, data):
        """AudioPlayer-Thread (und send_silence): jeder eigene Opus-Rahmen kommt
        hier VOR der DAVE-Verschluesselung vorbei - die Abzweigung fuer Flos
        Spur im Clip. Stille-Rahmen nicht: die sind kein Ton, nur Fuellung
        (fuenf am Ende jedes Songs, fuenf beim Aufwecken), und kaemen als
        Buendel ohne 20-ms-Takt. Die Abzweigung wirft nie; was discord.py
        selbst wirft, bleibt dessen Sache."""
        senke = self._eigene_senke()
        if senke is not None:
            try:
                if data != STILLE:
                    senke(bytes(data), time.monotonic())
            except Exception:  # noqa: BLE001 - der Player-Thread laeuft weiter
                if not self._abzweig_gemeldet:
                    self._abzweig_gemeldet = True
                    log.debug("Empfang: eigene Spur wirft", exc_info=True)
        return super()._get_voice_packet(data)

    # -- Aufwecken ----------------------------------------------------------------
    def play(self, *args, **kwargs):
        """Wie discord.py - merkt sich nur den Player (siehe _player_lebt)."""
        try:
            return super().play(*args, **kwargs)
        finally:
            self._letzter_player = getattr(self, "_player", None)

    def _player_lebt(self):
        """Lebt noch ein AudioPlayer-Thread dieses Clients - spielend,
        pausiert oder in seinen letzten Zuegen? Pausiert schickt er beim
        Anhalten fuenf Stille-Rahmen; nach stop() vergisst discord.py ihn
        sofort (_player = None), er schickt aber noch fuenf. Solange er lebt,
        sendet nur ER: sequence, timestamp und die Transport-Nonce zaehlt
        discord.py ohne Lock hoch, ein zweiter Sender aus dem Loop-Thread
        koennte eine Nonce doppelt vergeben."""
        for player in (getattr(self, "_player", None), self._letzter_player):
            try:
                if player is not None and player.is_alive():
                    return True
            except Exception:  # noqa: BLE001 - Attrappe ohne Thread
                continue
        return False

    def _server_stumm(self):
        try:
            stimme = self.channel.guild.me.voice
            return bool(stimme is not None and stimme.mute)
        except Exception:  # noqa: BLE001
            return False

    def _ruhe(self):
        """Nichts spielt, und kein Player-Thread sendet mehr."""
        return not self.is_playing() and not self._player_lebt()

    async def aufwecken(self):
        """Discord schickt einem Bot, der nie spricht, womoeglich keinen Ton.
        Einmal "spreche" melden, fuenf Stille-Rahmen, "spreche nicht" - aber nur,
        wenn verbunden, Flo nicht vom Server stummgeschaltet ist und KEIN
        Player-Thread mehr lebt (auch kein pausierter oder gerade gestoppter).
        Laeuft Musik, macht der Player das ohnehin, und ein fremdes "spreche
        nicht" mitten im Song waere falsch. -> True, wenn geweckt."""
        try:
            if not self.is_connected() or not self._ruhe() or self._server_stumm():
                return False
            ws = self.ws
            await ws.speak(discord.SpeakingState.voice)
            if not self._ruhe():            # waehrend des Wartens ging Musik los
                return False
            # Ab hier kein await: zwischen Pruefung und Senden startet niemand
            # einen Player (play() laeuft auch im Loop-Thread).
            for _ in range(WECK_RAHMEN):
                self.send_audio_packet(STILLE, encode=False)
            if self._ruhe():
                await ws.speak(discord.SpeakingState.none)
            return True
        except Exception:  # noqa: BLE001
            log.debug("Empfang: Aufwecken ging nicht", exc_info=True)
            return False

    def stand(self):
        """Fuer 'flo clip status': nur Zahlen und Schalter, keine Namen."""
        st = getattr(self, "_connection", None)
        dave = getattr(st, "dave_session", None)
        try:
            bereit = bool(getattr(dave, "ready", False))
            epoche = getattr(dave, "epoch", None)
        except Exception:  # noqa: BLE001 - davey "already borrowed"
            bereit, epoche = False, None
        modus = getattr(st, "mode", None)
        karte = getattr(self, "ssrc_zu_uid", None) or {}
        return {
            "modus": modus if isinstance(modus, str) else "",
            "dave_version": getattr(st, "dave_protocol_version", 0) or 0,
            "dave_bereit": bereit,
            "dave_epoche": epoche,
            "zuordnungen": len(karte),
            "zugeordnet": len(set(karte.values())),
            "im_call": len(getattr(self, "im_call", ()) or ()),
            "verbindungen": self.verbindungen,
        }


# --- Der Empfaenger ----------------------------------------------------------------
class Empfaenger:
    """Macht aus den UDP-Paketen EINES Voice-Clients Opus-Rahmen je Sprecher.

        gid       der Server (nur fuer Logzeilen)
        senke     senke(uid, strang, seq, ts, opus, t) - im Loop-Thread
        opt_out   opt_out(uid) -> True: dieser Mensch wird nie entschluesselt.
                  Wirft die Frage, gilt er als raus (im Zweifel fuer die
                  Privatsphaere).
        uhr       Zeit fuer Ankunft und Fristen (Tests stellen sie)

    Ueberlebt einen Client-Wechsel: anhaengen(neuer_client) loest vom alten.
    Haelt selbst nichts Dauerhaftes - der Puffer gehoert clips."""

    def __init__(self, gid, senke, *, opt_out=lambda uid: False, uhr=time.monotonic):
        self.gid = gid
        self.senke = senke
        self.opt_out = opt_out
        self.uhr = uhr
        #: Rueckruf bei op 4 eines Clients dieses Servers: bei_verbindung(client).
        #: clips gleicht dann ab (neuer Client -> anhaengen).
        self.bei_verbindung = None
        self.client = None
        #: Zaehlt jede neue Verbindung hoch - der obere Teil von 'strang'.
        self.verbindung_nr = 0
        self._loop = None
        self._zustand = None          # VoiceConnectionState, an dem roh haengt
        self._kanal = None            # in diesem Kanal wurde angehaengt
        self._eingang = deque(maxlen=EINGANG_MAX)
        self._geweckt = False
        self._verloren = 0            # schreibt NUR der Lese-Thread
        self._takt_griff = None
        self._zaehler = dict.fromkeys(ZAEHLER, 0)
        self._modus_gemeldet = False
        self._senke_gemeldet = False
        self._zuruecksetzen()

    def _zuruecksetzen(self):
        """Alles, was an genau EINER Verbindung haengt."""
        self._eingang.clear()
        self._geweckt = False
        self._schluessel_ref = None
        self._box = None
        self._box_alt = None
        self._box_alt_bis = 0.0
        self._entfaltet = {}          # strang -> [seq, ts, uid] (hoechste bisher)
        self._ssrc_stand = {}         # ssrc -> [uid oder None, generation]
        self._halten = {}             # ssrc -> deque[(t, seq, ts, rahmen)]
        self._nochmal = {}            # ssrc -> deque[(t, uid, strang, seq, ts, rahmen)]
        self._probe_um = {}           # ssrc -> wann zuletzt geraten
        #: DAVE-Stand, unter dem alles Wartende zuletzt probiert wurde
        self._dave_stand = None
        self._nochmal_um = float("-inf")

    # -- an- und abhaengen ------------------------------------------------------
    def anhaengen(self, client):
        """An diesen Client haengen - idempotent: derselbe Client mit derselben
        Verbindung im selben Kanal aendert nichts (zweimal angemeldet hiesse
        jedes Paket doppelt). Sonst (anderer Client, neue Verbindung,
        verschoben): vom alten loesen, neu anmelden, verbindung_nr hoch, Halte-
        und Entfaltungs-Zustand frisch. -> True, wenn er jetzt dranhaengt."""
        st = getattr(client, "_connection", None)
        if client is None or st is None:
            return False
        if client is self.client and st is self._zustand and _kanal_von(client) == self._kanal:
            return True
        self.abhaengen()
        try:
            st.add_socket_listener(self.roh)
        except Exception:  # noqa: BLE001
            log.debug("Empfang: Lauscher liess sich nicht anmelden", exc_info=True)
            return False
        self.client = client
        self._zustand = st
        self._kanal = _kanal_von(client)
        self._loop = _schleife(client)
        self.verbindung_nr += 1
        return True

    def abhaengen(self):
        """Loesen (idempotent): Lauscher ab, alles Liegende weg - es gehoert zu
        einer Verbindung, die hier nicht mehr zaehlt. Liefert NIE an die Senke:
        clips leert danach den Puffer, ein Nachzuegler kaeme zu spaet."""
        st = self._zustand
        self._zustand = None
        self.client = None
        self._kanal = None
        self._loop = None
        if st is not None:
            try:
                st.remove_socket_listener(self.roh)
            except Exception:  # noqa: BLE001
                log.debug("Empfang: Lauscher liess sich nicht abmelden", exc_info=True)
        griff = self._takt_griff
        self._takt_griff = None
        if griff is not None:
            griff.cancel()
        self._zuruecksetzen()

    def client_verbunden(self, client):
        """Loop-Thread, op 4 eines Clients dieses Servers. Ist es unserer, wurde
        die Verbindung neu aufgebaut: was noch im Eingang liegt, gehoert zur
        alten (erst abarbeiten), danach zaehlt verbindung_nr hoch - seq/ts und
        DAVE fangen dort neu an. Sitzt er dabei in einem ANDEREN Kanal als beim
        Anhaengen (verschoben), haengt der Empfaenger sich ab: dort gab es noch
        keine Ansage, also kommt aus diesem Kanal nichts in die Senke, bis clips
        angekuendigt und neu angehaengt hat. Dann bei_verbindung(client) - auch
        fuer einen fremden Client: ob der dran soll, entscheidet clips."""
        if client is not None and client is self.client:
            if _kanal_von(client) != self._kanal:
                self.abhaengen()
            else:
                self.abarbeiten()
                self.verbindung_nr += 1
                self._entfaltet.clear()
                self._ssrc_stand.clear()
                self._halten.clear()
                self._nochmal.clear()
                self._probe_um.clear()
                self._dave_stand = None
        rueckruf = self.bei_verbindung
        if rueckruf is not None:
            try:
                rueckruf(client)
            except Exception:  # noqa: BLE001
                log.debug("Empfang: Rueckruf bei op 4 gescheitert", exc_info=True)

    # -- SocketReader-Thread ----------------------------------------------------
    def roh(self, data):
        """SocketReader-Thread: das Paket mit Ankunftszeit ablegen und den Loop
        EINMAL wecken. Sonst nichts. Wirft nie (ein geschlossener Loop o. ae.
        darf den Lese-Thread nicht stoeren)."""
        try:
            eingang = self._eingang
            if len(eingang) >= EINGANG_MAX:
                self._verloren += 1           # der Loop kommt nicht hinterher
            eingang.append((data, self.uhr()))
            if not self._geweckt:
                loop = self._loop
                if loop is not None:
                    self._geweckt = True
                    loop.call_soon_threadsafe(self._wach)
        except Exception:  # noqa: BLE001
            pass

    def _wach(self):
        """Loop-Thread, von roh() geweckt. Die Marke faellt VOR dem Abarbeiten:
        was waehrenddessen ankommt, weckt neu - nichts bleibt liegen."""
        self._geweckt = False
        try:
            self.abarbeiten()
        except Exception:  # noqa: BLE001
            log.debug("Empfang: Abarbeiten gescheitert", exc_info=True)

    # -- Loop-Thread ------------------------------------------------------------
    def abarbeiten(self):
        """Loop-Thread (clips und Tests rufen es auch direkt): was im Eingang
        liegt, durch die Kette schicken. -> wie viele Pakete es waren."""
        client = self.client
        eingang = self._eingang
        if client is None:
            eingang.clear()
            return 0
        n = 0
        if eingang:
            # Schluessel, Modus und DAVE-Sitzung je Stapel frisch: op 4 kann sie
            # jederzeit tauschen (Neuaufbau, Serverwechsel). Kam ein DAVE-
            # Schluessel, ist erst das Wartende dran, dann der neue Stapel.
            st = getattr(client, "_connection", None)
            modus_ok = self._lage(st)
            self._dave_pruefen(st)
            karte = getattr(client, "ssrc_zu_uid", None)
            if not isinstance(karte, dict):
                karte = {}
            while eingang and n < STAPEL_MAX:
                data, t = eingang.popleft()
                n += 1
                try:
                    self._paket(data, t, st, modus_ok, karte)
                except Exception:  # noqa: BLE001 - ein Paket darf den Stapel nicht kippen
                    self._zaehler["kaputt"] += 1
                    log.debug("Empfang: Paket nicht verarbeitbar", exc_info=True)
            if eingang:
                self._gleich_weiter()
        self._takt_planen()
        return n

    def _lage(self, st):
        """Je Stapel: Modus pruefen, bei neuem Schluessel neue Box (die alte
        bleibt ALT_SCHLUESSEL_SEK als Rueckfall). -> ob der Modus passt."""
        jetzt = self.uhr()
        modus = getattr(st, "mode", None)
        modus_ok = modus == MODUS
        if not modus_ok and isinstance(modus, str) and modus and not self._modus_gemeldet:
            self._modus_gemeldet = True
            log.info("Empfang: Server %s - Verschluesselung %s kann ich nicht lesen.",
                     self.gid, modus)
        schluessel = getattr(st, "secret_key", None)
        if schluessel is not self._schluessel_ref:
            alt = self._box
            self._schluessel_ref = schluessel
            self._box = None
            if schluessel and _nacl_secret is not None:
                try:
                    self._box = _nacl_secret.Aead(bytes(schluessel))
                except Exception:  # noqa: BLE001
                    log.debug("Empfang: Schluessel unbrauchbar", exc_info=True)
            if alt is not None:
                self._box_alt = alt
                self._box_alt_bis = jetzt + ALT_SCHLUESSEL_SEK
        if self._box_alt is not None and jetzt > self._box_alt_bis:
            self._box_alt = None
        return modus_ok

    def _paket(self, data, t, st, modus_ok, karte):
        z = self._zaehler
        z["roh"] += 1
        if ist_rtcp(data):
            z["rtcp"] += 1
            return
        kopf = rtp_kopf(data)
        if kopf is None:
            z["kaputt"] += 1
            return
        if kopf.pt != OPUS_PT:
            z["fremd"] += 1
            return
        if not modus_ok:
            z["modus_fremd"] += 1
            return
        uid = karte.get(kopf.ssrc)
        if uid is not None and self._raus(uid):
            # Die SSRC steht im Klartext-Kopf: nicht einmal der Transport geht
            # auf. Was von ihr noch von vor der Zuordnung gehalten wird, auch weg.
            z["raus"] += 1 + len(self._halten.pop(kopf.ssrc, ()))
            return
        rahmen = self._transport(data, kopf)
        if rahmen is None:
            return
        if uid is None:
            self._halten_rein(kopf, rahmen, t)
            return
        if kopf.ssrc in self._halten:
            # Zugeordnet, aber von frueher liegt noch etwas: das zuerst.
            self._halten_los(kopf.ssrc, uid, st)
        self._weiter(uid, kopf.ssrc, kopf.seq, kopf.ts, rahmen, t, st)

    def _transport(self, data, kopf):
        """Mit dem aktuellen Schluessel aufmachen - und bis ALT_SCHLUESSEL_SEK
        nach einem Wechsel auch mit dem vorigen. -> Rahmen oder None (gezaehlt)."""
        z = self._zaehler
        boxen = [b for b in (self._box, self._box_alt) if b is not None]
        if not boxen:
            z["kein_schluessel"] += 1
            return None
        for box in boxen:
            try:
                rahmen = transport_entschluesseln(data, kopf, box)
            except Exception:  # noqa: BLE001 - CryptoError: falscher/alter Schluessel
                continue
            if box is self._box_alt:
                z["alter_schluessel"] += 1
            break
        else:
            z["transport_fehler"] += 1
            return None
        if not rahmen:
            z["leer"] += 1
            return None
        return rahmen

    def _raus(self, uid):
        try:
            return bool(self.opt_out(uid))
        except Exception:  # noqa: BLE001 - im Zweifel fuer die Privatsphaere
            log.debug("Empfang: Opt-out-Frage gescheitert - gilt als raus", exc_info=True)
            return True

    def _weiter(self, uid, ssrc, seq, ts, rahmen, t, st, klar=None):
        """Ab hier ist der Sprecher bekannt: Opt-out, DAVE, Senke. 'klar' ist
        ein schon entschluesselter Rahmen (aus der Probe).

        Wartet von dieser SSRC noch etwas auf DAVE, ist das zuerst dran - in
        Reihenfolge, bis eins scheitert (davey nimmt je Absender nur Nonces
        nach vorn, siehe Kopf der Datei). Geht der neue Rahmen dann auf,
        waehrend Aelteres davor weiter scheitert, ist das Aeltere tot."""
        z = self._zaehler
        if self._raus(uid):
            z["raus"] += 1                    # VOR DAVE - nie entschluesselt
            return
        strang = self._strang(ssrc, uid)
        if klar is not None:
            opus, art = klar, "ok"
        else:
            if ssrc in self._nochmal:
                self._schlange_versuchen(ssrc, st, alle=False)
            opus, art = self._dave(uid, rahmen, st)
            if opus is None:
                self._nochmal_rein(ssrc, (t, uid, strang, seq, ts, rahmen))
                return
        if art == "ok" and ssrc in self._nochmal:
            rest = self._tote_weg(self._nochmal.pop(ssrc), uid)
            if rest:
                self._nochmal[ssrc] = rest
        self._liefern(uid, strang, seq, ts, opus, t, art)

    def _strang(self, ssrc, uid):
        """strang = verbindung_nr << 48 | generation << 32 | ssrc. Die
        Generation einer SSRC zaehlt hoch, wenn ihr Mensch geht (op 13, siehe
        nutzer_weg) oder sie ploetzlich jemand anderem gehoert (op 5): kommt
        dieselbe SSRC wieder, ist es ein neuer RTP-Strom mit eigenem seq/ts,
        nie derselbe Strang."""
        stand = self._ssrc_stand.get(ssrc)
        if stand is None:
            stand = self._ssrc_stand[ssrc] = [uid, 0]
        elif stand[0] != uid:
            if stand[0] is not None:
                stand[1] += 1
            stand[0] = uid
        return (self.verbindung_nr << 48) | ((stand[1] & 0xFFFF) << 32) | ssrc

    def _dave(self, uid, rahmen, st):
        """DAVE-Stufe -> (opus, "ok"|"klar") oder (None, "") = spaeter nochmal.
        "ok" heisst: ein MARKIERTER Rahmen ging mit DAVE auf - nur das setzt
        bei davey die Nonce-Grenze des Absenders (siehe Kopf der Datei).

        Stille (STILLE) ist nie verschluesselt und geht immer durch. Ohne DAVE
        (Version 0 oder keine Sitzung) geht ein unmarkierter Rahmen durch, ein
        markierter wartet (der Schluessel kommt evtl. gleich). Mit DAVE:
        davey.decrypt - und scheitert das (kein Schluessel fuer den Absender,
        "already borrowed" weil der Player-Thread gerade verschluesselt), geht
        ein UNMARKIERTER Rahmen nur durch, wenn davey den Absender
        durchlaesst. Alles andere wartet."""
        dave = getattr(st, "dave_session", None)
        version = getattr(st, "dave_protocol_version", 0) or 0
        markiert = rahmen[-2:] == DAVE_MARKE
        if not markiert and (rahmen == STILLE or not version or dave is None):
            return rahmen, "klar"
        if not version or dave is None:
            return None, ""
        try:
            opus = dave.decrypt(uid, davey.MediaType.audio, rahmen)
        except Exception:  # noqa: BLE001 - ValueError, RuntimeError: nochmal
            opus = None
        if opus is not None:
            return bytes(opus), ("ok" if markiert else "klar")
        if not markiert:
            try:
                if dave.can_passthrough(uid):
                    return rahmen, "klar"
            except Exception:  # noqa: BLE001
                pass
        return None, ""

    def _liefern(self, uid, strang, seq, ts, opus, t, art):
        z = self._zaehler
        if not opus:
            z["leer"] += 1
            return
        z["dave_ok" if art == "ok" else "dave_klar"] += 1
        stand = self._entfaltet.get(strang)
        if stand is None:
            seq_x, ts_x = seq, ts
            self._entfaltet[strang] = [seq, ts, uid]
        else:
            seq_x = entfalten(stand[0], seq, 16)
            ts_x = entfalten(stand[1], ts, 32)
            if seq_x > stand[0]:
                stand[0] = seq_x
            if ts_x > stand[1]:
                stand[1] = ts_x
        try:
            self.senke(uid, strang, seq_x, ts_x, opus, t)
        except Exception:  # noqa: BLE001
            z["senke_fehler"] += 1
            if not self._senke_gemeldet:
                self._senke_gemeldet = True
                log.warning("Empfang: Server %s - die Senke wirft, Ton geht verloren.",
                            self.gid, exc_info=True)
            return
        z["gespeichert"] += 1

    # -- Halten (SSRC noch ohne Zuordnung) ----------------------------------------
    def _halten_rein(self, kopf, rahmen, t):
        z = self._zaehler
        schlange = self._halten.get(kopf.ssrc)
        if schlange is None:
            if len(self._halten) >= HALTEN_UNBEKANNT_SSRCS:
                z["unbekannt_voll"] += 1
                return
            schlange = self._halten[kopf.ssrc] = deque()
        if len(schlange) >= HALTEN_UNBEKANNT_PAKETE:
            schlange.popleft()
            z["unbekannt_voll"] += 1
        schlange.append((t, kopf.seq, kopf.ts, rahmen))
        z["unbekannt"] += 1

    def _halten_los(self, ssrc, uid, st):
        for t, seq, ts, rahmen in self._halten.pop(ssrc, None) or ():
            self._weiter(uid, ssrc, seq, ts, rahmen, t, st)

    def ssrc_bekannt(self, ssrc, uid, client=None):
        """Loop-Thread, op 5: diese SSRC gehoert jetzt jemandem. Was von ihr
        gehalten wurde, geht weiter - mit der ALTEN Ankunftszeit. Meldet ein
        anderer als unser Client, gilt das nicht fuer uns. -> wie viele."""
        if self.client is None or (client is not None and client is not self.client):
            return 0
        schlange = self._halten.get(ssrc)
        if not schlange:
            return 0
        n = len(schlange)
        st = getattr(self.client, "_connection", None)
        self._dave_pruefen(st)
        self._halten_los(ssrc, uid, st)
        self._takt_planen()
        return n

    def nutzer_weg(self, uid):
        """Loop-Thread, op 13: jemand hat den Call verlassen. Seine Entfaltung
        vergessen, und seine SSRCs bekommen eine neue Generation: kommt dieselbe
        SSRC wieder (er oder ein anderer), ist das ein neuer Strang. Was von ihm
        noch auf DAVE wartet, darf seine Frist zu Ende warten."""
        for strang in [s for s, stand in self._entfaltet.items() if stand[2] == uid]:
            del self._entfaltet[strang]
        for stand in self._ssrc_stand.values():
            if stand[0] == uid:
                stand[0] = None
                stand[1] += 1

    def vergessen(self, uid):
        """Opt-out: was von diesem Menschen gehalten wird oder auf DAVE wartet,
        sofort weg. (Was schon in der Senke liegt, raeumt clips selbst; was noch
        im Eingang liegt, haelt die Opt-out-Frage vor DAVE auf.)"""
        karte = getattr(self.client, "ssrc_zu_uid", None)
        if isinstance(karte, dict):
            for ssrc in [s for s, u in karte.items() if u == uid]:
                self._halten.pop(ssrc, None)
        for ssrc, schlange in list(self._nochmal.items()):
            rest = deque(e for e in schlange if e[1] != uid)
            if rest:
                self._nochmal[ssrc] = rest
            else:
                del self._nochmal[ssrc]

    # -- DAVE nochmal ----------------------------------------------------------------
    def _nochmal_rein(self, ssrc, eintrag):
        z = self._zaehler
        schlange = self._nochmal.get(ssrc)
        if schlange is None:
            if len(self._nochmal) >= HALTEN_DAVE_SSRCS:
                z["dave_verworfen"] += 1
                return
            schlange = self._nochmal[ssrc] = deque()
        if len(schlange) >= HALTEN_DAVE_PAKETE:
            schlange.popleft()
            z["dave_verworfen"] += 1
        schlange.append(eintrag)
        z["dave_nochmal"] += 1

    def _dave_pruefen(self, st):
        """Hat sich der DAVE-Stand geaendert (Sitzung, Version, bereit, Epoche -
        so kommen Schluessel an), geht alles Wartende sofort nochmal durch, in
        Reihenfolge und BEVOR ein neuer Rahmen an davey geht. Je Stapel, im
        Takt und bevor op 5 Gehaltenes freigibt."""
        kennung = _dave_kennung(st)
        if kennung == self._dave_stand:
            return
        self._dave_stand = kennung
        if self._nochmal:
            self._nochmal_um = self.uhr()
            self._nochmal_versuchen(st)

    def _nochmal_versuchen(self, st):
        """Alles Wartende einmal nochmal - je SSRC in Reihenfolge."""
        for ssrc in list(self._nochmal):
            self._schlange_versuchen(ssrc, st, alle=True)

    def _schlange_versuchen(self, ssrc, st, *, alle):
        """Die Nochmal-Schlange EINER SSRC in Reihenfolge an davey. 'alle':
        jeden Eintrag einmal (neuer DAVE-Stand, Takt); sonst nur, bis einer
        scheitert (vor einem neuen Rahmen derselben SSRC: was dahinter liegt,
        bleibt dahinter). Scheitert etwas, waehrend ein JUENGERER Rahmen
        desselben Absenders aufgeht, geht es nie mehr auf - weg damit."""
        z = self._zaehler
        schlange = self._nochmal.pop(ssrc, None)
        rest = deque()
        while schlange:
            eintrag = schlange.popleft()
            t, uid, strang, seq, ts, rahmen = eintrag
            if self._raus(uid):
                z["raus"] += 1
                continue
            opus, art = self._dave(uid, rahmen, st)
            if opus is None:
                rest.append(eintrag)
                if not alle:
                    break
                continue
            if art == "ok" and rest:
                rest = self._tote_weg(rest, uid)
            self._liefern(uid, strang, seq, ts, opus, t, art)
        if schlange:
            rest.extend(schlange)
        if rest:
            self._nochmal[ssrc] = rest

    def _tote_weg(self, schlange, uid):
        """Ein Rahmen dieses Absenders ging gerade mit DAVE auf: was von ihm
        davor noch wartet, laesst davey nie mehr durch (alte Epoche, oder
        unter der Nonce-Grenze). Zaehlen und weg -> was bleibt (andere
        Absender, falls die SSRC den Besitzer wechselte)."""
        bleibt = deque(e for e in schlange if e[1] != uid)
        self._zaehler["dave_verworfen"] += len(schlange) - len(bleibt)
        return bleibt

    # -- Takt ------------------------------------------------------------------------
    def takt(self):
        """Loop-Thread: Abgelaufenes wegwerfen (gehalten > HALTEN_UNBEKANNT, auf
        DAVE wartend > HALTEN_DAVE), DAVE nochmal versuchen - sofort, wenn sich
        (Version, bereit, Epoche) geaendert hat, sonst hoechstens einmal je
        NOCHMAL_ABSTAND -, unbekannte SSRCs proben. clips ruft es im eigenen
        Takt; solange etwas wartet, plant es sich selbst alle TAKT_SEK."""
        try:
            self._takt_innen()
        except Exception:  # noqa: BLE001
            log.debug("Empfang: Takt gescheitert", exc_info=True)
        self._takt_planen()

    def _takt_innen(self):
        jetzt = self.uhr()
        z = self._zaehler
        for ssrc, schlange in list(self._halten.items()):
            while schlange and jetzt - schlange[0][0] > HALTEN_UNBEKANNT:
                schlange.popleft()
                z["unbekannt_verfallen"] += 1
            if not schlange:
                del self._halten[ssrc]
        for ssrc, schlange in list(self._nochmal.items()):
            while schlange and jetzt - schlange[0][0] > HALTEN_DAVE:
                schlange.popleft()
                z["dave_verworfen"] += 1
            if not schlange:
                del self._nochmal[ssrc]
        for ssrc in [s for s in self._probe_um if s not in self._halten]:
            del self._probe_um[ssrc]
        if self._box_alt is not None and jetzt > self._box_alt_bis:
            self._box_alt = None
        if self.client is None:
            return
        st = getattr(self.client, "_connection", None)
        self._dave_pruefen(st)
        if self._nochmal and jetzt - self._nochmal_um >= NOCHMAL_ABSTAND:
            self._nochmal_um = jetzt
            self._nochmal_versuchen(st)
        if self._halten:
            self._proben(st, jetzt)

    def _takt_planen(self):
        """Solange etwas gehalten wird oder auf DAVE wartet: takt() in TAKT_SEK."""
        if self._takt_griff is not None or not (self._halten or self._nochmal):
            return
        loop = self._loop
        if loop is None:
            return
        try:
            if not loop.is_closed():
                self._takt_griff = loop.call_later(TAKT_SEK, self._takt_auto)
        except Exception:  # noqa: BLE001
            self._takt_griff = None

    def _takt_auto(self):
        self._takt_griff = None
        self.takt()

    def _gleich_weiter(self):
        """Der Stapel war voll: der Rest kommt im naechsten Loop-Durchgang."""
        loop = self._loop
        if loop is None:
            return
        try:
            self._geweckt = True
            loop.call_soon(self._wach)
        except Exception:  # noqa: BLE001
            self._geweckt = False

    # -- Raten statt warten ----------------------------------------------------------
    def _proben(self, st, jetzt):
        """Fuer Leute, die schon sprachen, bevor Flo kam, schickt Discord evtl.
        nie ein op 5. Haengt eine SSRC laenger als PROBE_AB mit DAVE-Rahmen in
        der Warteschleife, werden ihre DAVE-Rahmen probehalber mit den
        Schluesseln der noch nicht zugeordneten Leute im Call entschluesselt
        (hoechstens PROBE_MAX Leute, je SSRC alle PROBE_ALLE, je Takt
        hoechstens PROBE_VERSUCHE Versuche). DAVE ist authentifiziert:
        aufgehen kann ein Rahmen nur mit dem Schluessel des wahren Absenders.
        Deshalb nie im Durchlass (da ginge jeder "auf") und nie mit jemandem,
        der raus ist.

        Der AELTESTE DAVE-Rahmen zuerst: der erste, der aufgeht, setzt bei
        davey die Nonce-Grenze dieses Absenders - mit dem juengsten geraten,
        waeren alle davor fuer immer zu. Was davor mit niemandem aufging (alte
        Epoche, von bevor Flo im Call war), ist tot und fliegt raus."""
        dave = getattr(st, "dave_session", None)
        if dave is None or davey is None or not getattr(st, "dave_protocol_version", 0):
            return
        try:
            if not dave.ready:
                return
        except Exception:  # noqa: BLE001
            return
        karte = getattr(self.client, "ssrc_zu_uid", None)
        if not isinstance(karte, dict):
            return
        kandidaten = None
        versuche = 0
        z = self._zaehler
        for ssrc, schlange in list(self._halten.items()):
            if versuche >= PROBE_VERSUCHE:
                break
            if not schlange or jetzt - schlange[0][0] < PROBE_AB:
                continue
            if jetzt - self._probe_um.get(ssrc, float("-inf")) < PROBE_ALLE:
                continue
            if not any(e[3][-2:] == DAVE_MARKE for e in schlange):
                continue
            self._probe_um[ssrc] = jetzt
            if kandidaten is None:
                kandidaten = self._kandidaten(karte, dave)
            treffer = None
            for stelle, (_t, _seq, _ts, rahmen) in enumerate(schlange):
                if rahmen[-2:] != DAVE_MARKE:
                    continue
                for uid in kandidaten:
                    if versuche >= PROBE_VERSUCHE:
                        break
                    versuche += 1
                    try:
                        klar = dave.decrypt(uid, davey.MediaType.audio, rahmen)
                    except Exception:  # noqa: BLE001 - falscher Schluessel: weiter
                        continue
                    if klar:
                        treffer = (stelle, uid, bytes(klar))
                        break
                if treffer is not None or versuche >= PROBE_VERSUCHE:
                    break
            if treffer is None:
                continue
            stelle, uid, klar = treffer
            karte[ssrc] = uid
            z["geraten"] += 1
            kandidaten.remove(uid)
            for i, (t, seq, ts, r) in enumerate(self._halten.pop(ssrc)):
                if i < stelle and r[-2:] == DAVE_MARKE:
                    z["dave_verworfen"] += 1      # ging mit niemandem auf: tot
                    continue
                self._weiter(uid, ssrc, seq, ts, r, t, st, klar=klar if i == stelle else None)

    def _kandidaten(self, karte, dave):
        """Wer im Call ist und noch keine SSRC hat - ohne Flo selbst, ohne wen,
        der raus ist, und ohne wen, den davey gerade durchlaesst. Hoechstens
        PROBE_MAX, sortiert (die Reihenfolge ist so in Tests nachpruefbar)."""
        client = self.client
        leute = set(getattr(client, "im_call", ()) or ())
        try:
            for mitglied in getattr(client.channel, "members", ()) or ():
                leute.add(mitglied.id)
        except Exception:  # noqa: BLE001
            pass
        zugeordnet = set(karte.values())
        ich = getattr(getattr(client, "user", None), "id", None)
        kandidaten = []
        for uid in sorted(u for u in leute
                          if isinstance(u, int) and u not in zugeordnet and u != ich):
            if len(kandidaten) >= PROBE_MAX:
                break
            if self._raus(uid):
                continue
            try:
                if dave.can_passthrough(uid):
                    continue
            except Exception:  # noqa: BLE001
                continue
            kandidaten.append(uid)
        return kandidaten

    # -- Auskunft ----------------------------------------------------------------------
    def zaehler(self):
        """Wohin die Pakete seit dem Bau gingen - nur Zahlen (Schluessel: ZAEHLER
        und 'ueberlauf')."""
        werte = dict(self._zaehler)
        werte["ueberlauf"] = self._verloren
        return werte

    def stand(self):
        """Was gerade liegt - nur Zahlen und Schalter, keine IDs."""
        return {
            "angehaengt": self.client is not None,
            "verbindung": self.verbindung_nr,
            "eingang": len(self._eingang),
            "gehalten": sum(len(q) for q in self._halten.values()),
            "gehalten_ssrcs": len(self._halten),
            "nochmal": sum(len(q) for q in self._nochmal.values()),
            "straenge": len(self._entfaltet),
            "alter_schluessel": self._box_alt is not None,
        }
