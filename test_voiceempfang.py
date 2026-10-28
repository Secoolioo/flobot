"""Voice-Empfang (voiceempfang.py): RTP, Transport, DAVE, Halten, Haken, Abzweigung.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

Die Pakete hier sind ECHT verschluesselt - mit PyNaCl, so wie Discord sie
schickt (aead_xchacha20_poly1305_rtpsize), und einmal mit discord.py's eigenem
Sendeweg. Wo libopus da ist, laeuft echtes Opus hin und zurueck. DAVE laeuft
zweimal: als Attrappe, die so streng ist wie davey (je Absender nur vorwaerts),
und ECHT mit davey in einer MLS-Gruppe, deren externen Sender (bei Discord der
Voice-Server) der Test selbst spielt. Netz gibt es keins.

    python test_voiceempfang.py      nur diese Datei
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module

import contextlib
import hashlib
import itertools
import logging
import math
import secrets
import struct
import subprocess
import threading
from unittest import mock

import davey
import discord
import nacl.exceptions
import nacl.secret

import voiceempfang as ve


# =============================================================================
# Helfer
# =============================================================================
_SCHLUESSEL = bytes(range(32))
_ANDERER = bytes(range(32, 64))


def _paket(opus, *, seq=1, ts=960, ssrc=4242, schluessel=_SCHLUESSEL, cc=0, ext=None,
           pad=0, nonce=1, marker=False, pt=ve.OPUS_PT, klar=None, p_bit=None):
    """Ein RTP-Paket, wie Discord es schickt - rtpsize, echtes PyNaCl.

    ext   Rumpf der Kopf-Erweiterung als 32-Bit-Woerter (der Kopf 0xBEDE+Laenge
          bleibt unverschluesselt und ist Teil der AAD, der Rumpf nicht)
    pad   so viele Fuellbytes (letztes Byte = ihre Zahl, P-Bit gesetzt)
    klar  ersetzt den ganzen Klartext (fuer unpassende Fuellangaben)
    """
    p_bit = bool(pad) if p_bit is None else p_bit
    b0 = 0x80 | (0x20 if p_bit else 0) | (0x10 if ext is not None else 0) | cc
    b1 = (0x80 if marker else 0) | pt
    kopf = struct.pack(">BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc)
    kopf += b"".join(struct.pack(">I", 7000 + i) for i in range(cc))
    rumpf = b""
    if ext is not None:
        kopf += struct.pack(">HH", 0xBEDE, len(ext))
        rumpf = b"".join(struct.pack(">I", w) for w in ext)
    if klar is None:
        klar = rumpf + opus + (bytes(pad - 1) + bytes([pad]) if pad else b"")
    n = struct.pack(">I", nonce)
    geheim = nacl.secret.Aead(schluessel).encrypt(klar, kopf, n + bytes(20)).ciphertext
    return kopf + geheim + n


_RTCP = bytes([0x81, 201]) + bytes(40)


class _Zustand:
    """VoiceConnectionState-Attrappe: Schluessel, Modus, DAVE - und wer lauscht."""

    def __init__(self, *, schluessel=_SCHLUESSEL, modus=ve.MODUS, dave=None, version=0):
        self.secret_key = list(schluessel)
        self.mode = modus
        self.dave_session = dave
        self.dave_protocol_version = version
        self.lauscher = []
        self.verbunden = True
        self.ws = None
        self.ssrc = 1

    def add_socket_listener(self, cb):
        self.lauscher.append(cb)

    def remove_socket_listener(self, cb):
        if cb in self.lauscher:
            self.lauscher.remove(cb)

    def is_connected(self):
        return self.verbunden


def _client(karte=None, *, zustand=None, loop=None, gid=77, kanal=42, stumm=False):
    """Ein EmpfangsClient ohne Socket-Thread (object.__new__) - so baut sich
    auch clips seine Attrappe."""
    vc = object.__new__(ve.EmpfangsClient)
    vc._empfang_vorbereiten()
    vc.ssrc_zu_uid.update(karte or {})
    vc._connection = zustand if zustand is not None else _Zustand()
    vc.loop = loop
    vc._player = None
    vc.channel = SimpleNamespace(id=kanal, members=[], guild=SimpleNamespace(
        id=gid, me=SimpleNamespace(voice=SimpleNamespace(mute=stumm))))
    return vc


class _Senke:
    """Merkt sich jeden Aufruf senke(uid, strang, seq, ts, opus, t)."""

    def __init__(self):
        self.pakete = []

    def __call__(self, uid, strang, seq, ts, opus, t):
        self.pakete.append((uid, strang, seq, ts, opus, t))

    def opus(self):
        return [p[4] for p in self.pakete]


class _Uhr:
    """Eine Uhr, die der Test stellt."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def vor(self, sekunden):
        self.t += sekunden


def _empfaenger(karte=None, *, zustand=None, opt_out=None, uhr_=None, gid=77):
    """Empfaenger an einem Attrappen-Client (ohne Loop: roh() weckt niemanden,
    der Test ruft abarbeiten() selbst). -> (e, vc, senke, uhr)."""
    senke = _Senke()
    uhr_ = uhr_ if uhr_ is not None else _Uhr()
    kw = {"uhr": uhr_}
    if opt_out is not None:
        kw["opt_out"] = opt_out
    e = ve.Empfaenger(gid, senke, **kw)
    vc = _client(karte, zustand=zustand, gid=gid)
    assert e.anhaengen(vc)
    return e, vc, senke, uhr_


@contextlib.contextmanager
def _angemeldet(gid, e):
    """Im Register, solange der Block laeuft - danach garantiert wieder raus."""
    ve.anmelden(gid, e)
    try:
        yield e
    finally:
        ve.abmelden(gid)


def _haken(vc, msg):
    """Eine Websocket-Nachricht durch den Haken - wie discord.py ihn ruft."""
    asyncio.run(vc._ws_hook(None, msg))


class _FalscheDave:
    """davey.DaveSession-Attrappe, so streng wie davey 0.1.7 (geprueft in
    test_empfang_dave_attrappe_ist_so_streng_wie_davey). Ein 'DAVE-Rahmen'
    ist hier opus + alt (1 Byte) + Nonce (4) + Absender-ID (8) + DAVE_MARKE.

      - aufgehen kann er nur mit dem Schluessel genau dieses Absenders
        (authentifiziert) und nie, wenn er 'alt' ist (aus einer Epoche, die
        dieser Empfaenger nie hatte);
      - je Absender nur vorwaerts: der ERSTE Erfolg legt die Nonce-Grenze
        fest, ohne Aelteres als fehlend zu merken; danach Uebersprungenes
        darf nachkommen (davey CipherManager: newest_processed_nonce,
        missing_nonces). Ein Fehlversuch aendert nichts."""

    def __init__(self, *, bereit=True, epoche=1, absender=0):
        self.ready = bereit
        self.epoch = epoche
        self.schluessel = set()     # fuer wen der Schluessel schon da ist
        self.durchlass = set()      # wessen unverschluesselte Rahmen durchduerfen
        self.aufrufe = []           # (uid, rahmen) je decrypt
        self.wirft = None           # Ausnahme, die decrypt gerade wirft
        self.absender = absender    # fuer encrypt_opus (Flo als Sender)
        self.neueste = {}           # uid -> hoechste Nonce, die aufging
        self.fehlend = {}           # uid -> uebersprungene Noncen (duerfen nachkommen)

    def decrypt(self, uid, art, rahmen):
        self.aufrufe.append((uid, rahmen))
        assert art is davey.MediaType.audio
        if self.wirft is not None:
            raise self.wirft
        if rahmen[-2:] != ve.DAVE_MARKE:
            if uid in self.durchlass:
                return rahmen
            raise ValueError("Failed to decrypt: UnencryptedWhenPassthroughDisabled")
        if uid not in self.schluessel:
            raise ValueError("Failed to decrypt: NoDecryptorForUser")
        alt, nonce, absender = struct.unpack(">BIQ", rahmen[-15:-2])
        neueste = self.neueste.get(uid)
        fehlend = self.fehlend.setdefault(uid, set())
        if neueste is not None and nonce <= neueste and nonce not in fehlend:
            raise ValueError("Failed to decrypt: NoValidCryptorFound (Nonce)")
        if alt or absender != uid:
            raise ValueError("Failed to decrypt: NoValidCryptorFound")
        if neueste is None:
            self.neueste[uid] = nonce
        elif nonce > neueste:
            fehlend.update(range(max(nonce - 1000, neueste + 1), nonce))
            self.neueste[uid] = nonce
        else:
            fehlend.discard(nonce)
        return rahmen[:-15]

    def can_passthrough(self, uid):
        return uid in self.durchlass

    def encrypt_opus(self, opus):
        return _dave_rahmen(opus, self.absender)


#: Die Nonce der Attrappe: steigt wie bei davey mit jedem Rahmen.
_NONCE = itertools.count(1)


def _dave_rahmen(opus, uid, *, alt=False, nonce=None):
    """Ein DAVE-Rahmen der Attrappe (siehe _FalscheDave)."""
    nonce = next(_NONCE) if nonce is None else nonce
    return opus + struct.pack(">BIQ", 1 if alt else 0, nonce, uid) + ve.DAVE_MARKE


# --- Echtes DAVE ohne Discord ----------------------------------------------------
# davey baut eine MLS-Gruppe nur mit Vorschlaegen, die ein "externer Sender"
# (bei Discord der Voice-Server) signiert hat. Den spielt dieser Test: ECDSA
# P-256 in reinem Python, die MLS-Nachrichten (RFC 9420) von Hand. Damit laeuft
# ECHTES davey Ende zu Ende - Verschluesseln, Gruppe, Epochen, Nonce-Grenze.
_P256_P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_P256_N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
_P256_G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
           0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def _p256_plus(a, b):
    if a is None or b is None:
        return a if b is None else b
    if a[0] == b[0]:
        if (a[1] + b[1]) % _P256_P == 0:
            return None
        s = (3 * a[0] * a[0] - 3) * pow(2 * a[1], -1, _P256_P) % _P256_P
    else:
        s = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P256_P) % _P256_P
    x = (s * s - a[0] - b[0]) % _P256_P
    return x, (s * (a[0] - x) - a[1]) % _P256_P


def _p256_mal(k, punkt):
    erg = None
    while k:
        if k & 1:
            erg = _p256_plus(erg, punkt)
        punkt = _p256_plus(punkt, punkt)
        k >>= 1
    return erg


def _ecdsa(d, daten):
    """ECDSA P-256/SHA-256, DER-kodiert (so prueft MLS)."""
    e = int.from_bytes(hashlib.sha256(daten).digest(), "big")
    while True:
        k = secrets.randbelow(_P256_N - 1) + 1
        r = _p256_mal(k, _P256_G)[0] % _P256_N
        s = pow(k, -1, _P256_N) * (e + r * d) % _P256_N
        if r and s:
            zahlen = b"".join(b"\x02" + bytes([len(z)]) + z for z in (
                w.to_bytes(w.bit_length() // 8 + 1, "big") for w in (r, s)))
            return b"\x30" + bytes([len(zahlen)]) + zahlen


def _mls_vec(daten):
    """opaque<V> aus RFC 9420: Laenge als QUIC-Varint, dann die Bytes."""
    n = len(daten)
    if n < 64:
        return bytes([n]) + daten
    if n < 16384:
        return struct.pack(">H", 0x4000 | n) + daten
    return struct.pack(">I", 0x80000000 | n) + daten


class _EchteGruppe:
    """Eine echte DAVE-Gruppe (davey) mit diesem Test als externem Sender."""

    def __init__(self, kanal=4711):
        self.kanal = kanal
        self._d = secrets.randbelow(_P256_N - 1) + 1
        x, y = _p256_mal(self._d, _P256_G)
        self._aussen = (_mls_vec(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big"))
                        + struct.pack(">H", 1) + _mls_vec(b"\x00"))

    def sitzung(self, uid):
        """Eine neue davey-Sitzung (noch in keiner Gruppe)."""
        s = davey.DaveSession(1, uid, self.kanal)
        s.set_external_sender(self._aussen)
        return s

    def aufnehmen(self, wer, *neue):
        """Der externe Sender schlaegt vor, 'neue' aufzunehmen; 'wer' committet
        und verarbeitet seinen Commit (davey 0.1.7: ab da verschluesselt er mit
        dem Schluessel der neuen Epoche). -> der Welcome fuer die neuen."""
        nachrichten = b""
        for s in neue:
            # PublicMessage (RFC 9420, 6.2): Gruppe = Kanal, Epoche, Absender
            # "extern" Nr. 0, keine authenticated_data, Inhalt "proposal":
            # Add(KeyPackage). Signiert wird Version + Wire-Format + Inhalt.
            inhalt = (_mls_vec(self.kanal.to_bytes(8, "big")) + struct.pack(">Q", wer.epoch or 0)
                      + b"\x02" + struct.pack(">I", 0) + _mls_vec(b"") + b"\x02"
                      + struct.pack(">H", 1) + s.get_serialized_key_package())
            signatur = _ecdsa(self._d, _mls_vec(b"MLS 1.0 FramedContentTBS")
                              + _mls_vec(struct.pack(">HH", 1, 1) + inhalt))
            nachrichten += struct.pack(">HH", 1, 1) + inhalt + _mls_vec(signatur)
        cw = wer.process_proposals(davey.ProposalsOperationType.append, _mls_vec(nachrichten))
        wer.process_commit(cw.commit)
        return cw.welcome


def _echte_gruppe(*uids):
    """(gruppe, {uid: sitzung}) mit allen in EINER Epoche - oder None, wenn
    davey hier keine Gruppe baut (anderes davey): dann prueft der Test mit
    echtem DAVE nichts."""
    try:
        gruppe = _EchteGruppe()
        sitzungen = {u: gruppe.sitzung(u) for u in uids}
        erster, *rest = (sitzungen[u] for u in uids)
        willkommen = gruppe.aufnehmen(erster, *rest)
        for s in rest:
            s.process_welcome(willkommen)
        if not all(s.ready for s in sitzungen.values()):
            return None
        return gruppe, sitzungen
    except Exception:  # noqa: BLE001
        return None


def _uids(dave):
    return [u for u, _r in dave.aufrufe]


def _strang(verbindung, ssrc, generation=0):
    """So setzt der Empfaenger strang zusammen (fuer clips nur ein Schluessel)."""
    return (verbindung << 48) | (generation << 32) | ssrc


class _Fang(logging.Handler):
    """Faengt die Logzeilen von voiceempfang."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.zeilen = []

    def emit(self, record):
        self.zeilen.append((record.levelno, record.getMessage()))

    def __enter__(self):
        lg = logging.getLogger("dcbot.voiceempfang")
        self._alt = (lg.level, lg.propagate)
        lg.addHandler(self)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        return self

    def __exit__(self, *_a):
        lg = logging.getLogger("dcbot.voiceempfang")
        lg.removeHandler(self)
        lg.setLevel(self._alt[0])
        lg.propagate = self._alt[1]
        return False


def _opus_da():
    try:
        return bool(discord.opus.is_loaded() or discord.opus._load_default())
    except Exception:  # noqa: BLE001
        return False


# =============================================================================
# RTP-Kopf
# =============================================================================
def test_empfang_rtp_kopf_liest_alle_felder():
    """CSRCs, Erweiterung, Fuellung, Marker - und wo die AAD endet: 12 Bytes +
    4 je CSRC + 4 Bytes Erweiterungs-KOPF (der Rumpf ist mitverschluesselt)."""
    p = _paket(b"\xfc\x01\x02", seq=513, ts=123456, ssrc=99, cc=2, ext=[7, 8, 9],
               pad=4, marker=True)
    k = ve.rtp_kopf(p)
    assert (k.padding, k.erweiterung, k.cc, k.marker) == (True, True, 2, True)
    assert (k.pt, k.seq, k.ts, k.ssrc) == (ve.OPUS_PT, 513, 123456, 99)
    assert (k.aad_ende, k.ext_woerter) == (12 + 2 * 4 + 4, 3)
    schlicht = ve.rtp_kopf(_paket(b"\xfc"))
    assert (schlicht.padding, schlicht.erweiterung, schlicht.cc, schlicht.marker) == (
        False, False, 0, False)
    assert (schlicht.aad_ende, schlicht.ext_woerter) == (12, 0)
    # bytearray und memoryview gehen genauso (der Socket liefert bytes, aber sicher ist sicher)
    assert ve.rtp_kopf(bytearray(p)) == k and ve.rtp_kopf(memoryview(p)) == k


def test_empfang_rtp_kopf_verwirft_kurzes_und_muell():
    """Zu kurz, Version nicht 2, CSRCs oder Erweiterung passen nicht ins Paket,
    gar keine bytes: None - nie eine Ausnahme. Die IP-Discovery-Antwort
    (beginnt mit 0x00 0x02) ist auch kein RTP."""
    gut = _paket(b"\xfc")
    erweiterung_zu_lang = bytes([0x90 | 3, 0x78]) + bytes(30)
    for muell in (b"", b"\x80", gut[:31], bytes(64), b"\x40" + bytes(63),
                  b"\xc0" + bytes(63), b"\x00\x02" + bytes(72),
                  bytes([0x8F, 0x78]) + bytes(40), erweiterung_zu_lang,
                  None, "x" * 40, 12345, [0x80] * 40):
        assert ve.rtp_kopf(muell) is None, muell
    assert ve.ist_rtcp(_RTCP) and ve.ist_rtcp(bytes([0x80, 200, 0, 1]))
    assert ve.ist_rtcp(bytes([0x80, 204]))
    assert not ve.ist_rtcp(gut) and not ve.ist_rtcp(_paket(b"\xfc", marker=True))
    assert not ve.ist_rtcp(b"") and not ve.ist_rtcp(None) and not ve.ist_rtcp(b"\x80")


def test_empfang_rtcp_video_und_muell_fliegen_gezaehlt_raus():
    """RTCP (Empfangsberichte), Video (anderer Nutzlasttyp) und Unlesbares
    kommen nie bei der Senke an - aber jedes wird gezaehlt."""
    e, _vc, senke, _u = _empfaenger({4242: 5})
    for p in (_RTCP, _paket(b"\xfc\x01", pt=0x65), b"\x00\x02" + bytes(72),
              _paket(b"\xfc\x02")):
        e.roh(p)
    assert e.abarbeiten() == 4
    z = e.zaehler()
    assert (z["roh"], z["rtcp"], z["fremd"], z["kaputt"], z["gespeichert"]) == (4, 1, 1, 1, 1)
    assert senke.opus() == [b"\xfc\x02"]


def test_empfang_rtcp_heisst_192_bis_223_auch_feedback_und_xr():
    """RTCP sind wie in RFC 5761 alle Typen 192-223 - nicht nur die Berichte
    (200-204), auch Feedback (205, 206) und XR (207). Die zaehlen als rtcp,
    nicht als fremder Nutzlasttyp (das verfaelschte 'flo clip status')."""
    for typ in (192, 200, 201, 204, 205, 206, 207, 223):
        assert ve.ist_rtcp(bytes([0x81, typ]) + bytes(30)), typ
    for typ in (191, 224, ve.OPUS_PT, 0x80 | ve.OPUS_PT, 0x65, 0xE5):   # Opus/Video, mit Marker
        assert not ve.ist_rtcp(bytes([0x80, typ]) + bytes(30)), typ
    e, _vc, senke, _u = _empfaenger({4242: 5})
    for typ in (200, 201, 205, 206, 207):
        e.roh(bytes([0x81, typ]) + bytes(40))
    e.roh(_paket(b"\xfc\x01", marker=True))
    e.abarbeiten()
    z = e.zaehler()
    assert (z["rtcp"], z["fremd"], z["kaputt"], z["gespeichert"]) == (5, 0, 0, 1)


# =============================================================================
# Transport-Verschluesselung
# =============================================================================
def test_empfang_transport_echt_mit_erweiterung_csrcs_und_fuellung():
    """Echte PyNaCl-Pakete in allen Formen gehen auf; Erweiterungs-Rumpf und
    Fuellung sind danach weg, eine unpassende Fuellangabe schneidet nichts ab,
    ein Paket ohne Nutzlast ist None. Manipuliert (auch nur der Kopf - der ist
    AAD) -> CryptoError."""
    box = nacl.secret.Aead(_SCHLUESSEL)
    for kw in ({}, {"ext": [1, 2]}, {"cc": 3}, {"cc": 1, "ext": [5], "pad": 3}, {"pad": 1}):
        p = _paket(b"\xfc\xaa\xbb", **kw)
        assert ve.transport_entschluesseln(p, ve.rtp_kopf(p), box) == b"\xfc\xaa\xbb", kw
    for klar in (b"\xfc\xaa\x00", b"\xfc\xaa\x09"):      # 0 Fuellbytes / mehr als da sind
        p = _paket(b"", klar=klar, p_bit=True)
        assert ve.transport_entschluesseln(p, ve.rtp_kopf(p), box) == klar
    nur_erweiterung = _paket(b"", ext=[1])
    assert ve.transport_entschluesseln(nur_erweiterung, ve.rtp_kopf(nur_erweiterung),
                                       box) is None
    for stelle in (14, 3):                                # Geheimtext / Kopf (AAD)
        kaputt = bytearray(_paket(b"\xfc\x01"))
        kaputt[stelle] ^= 1
        try:
            ve.transport_entschluesseln(bytes(kaputt), ve.rtp_kopf(bytes(kaputt)), box)
        except nacl.exceptions.CryptoError:
            continue
        raise AssertionError(f"manipuliertes Byte {stelle} ging durch")


def test_empfang_versteht_discord_pys_eigenen_sendeweg():
    """Das Format ist kein Raten: ein Paket aus discord.py's eigenem
    _get_voice_packet (rtpsize) geht durch transport_entschluesseln auf."""
    sender = object.__new__(discord.VoiceClient)
    sender._incr_nonce = 41
    sender.sequence, sender.timestamp = 7, 4242
    sender._connection = SimpleNamespace(secret_key=list(_SCHLUESSEL), dave_session=None,
                                         can_encrypt=False, ssrc=99, mode=ve.MODUS)
    p = discord.VoiceClient._get_voice_packet(sender, b"\xfc\x10\x20")
    k = ve.rtp_kopf(p)
    assert (k.seq, k.ts, k.ssrc, k.pt, k.aad_ende) == (7, 4242, 99, ve.OPUS_PT, 12)
    assert ve.transport_entschluesseln(p, k, nacl.secret.Aead(_SCHLUESSEL)) == b"\xfc\x10\x20"


def test_empfang_falscher_schluessel_zaehlt_als_transportfehler():
    e, _vc, senke, _u = _empfaenger({4242: 5})
    kaputt = bytearray(_paket(b"\xfc\x01"))
    kaputt[14] ^= 1
    e.roh(bytes(kaputt))
    e.roh(_paket(b"\xfc\x02", schluessel=_ANDERER))
    e.roh(_paket(b"\xfc\x03"))
    e.abarbeiten()
    assert e.zaehler()["transport_fehler"] == 2
    assert senke.opus() == [b"\xfc\x03"]


def test_empfang_schluesselwechsel_alter_gilt_noch_fuenf_sekunden():
    """op 4 bringt einen neuen Schluessel: ab dem naechsten Stapel eine neue Box.
    Pakete, die noch mit dem alten unterwegs waren, gehen ALT_SCHLUESSEL_SEK
    lang weiter auf - danach nicht mehr."""
    uhr_ = _Uhr(1000.0)
    st = _Zustand()
    e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=st, uhr_=uhr_)
    e.roh(_paket(b"\xfc\x01", seq=1))
    e.abarbeiten()
    box_vorher = e._box
    st.secret_key = list(_ANDERER)                  # Neuaufbau: neuer Schluessel
    uhr_.vor(1.0)
    e.roh(_paket(b"\xfc\x02", seq=2))               # war noch unterwegs
    e.roh(_paket(b"\xfc\x03", seq=3, schluessel=_ANDERER))
    e.abarbeiten()
    assert e._box is not box_vorher and e.stand()["alter_schluessel"]
    assert senke.opus() == [b"\xfc\x01", b"\xfc\x02", b"\xfc\x03"]
    assert e.zaehler()["alter_schluessel"] == 1
    uhr_.vor(4.9)                                   # 4,9 s nach dem Wechsel: gilt noch
    e.roh(_paket(b"\xfc\x04", seq=4))
    e.abarbeiten()
    assert senke.opus()[-1] == b"\xfc\x04"
    uhr_.vor(0.2)                                   # 5,1 s: vorbei
    e.roh(_paket(b"\xfc\x05", seq=5))
    e.roh(_paket(b"\xfc\x06", seq=6, schluessel=_ANDERER))
    e.abarbeiten()
    assert senke.opus()[-1] == b"\xfc\x06" and len(senke.pakete) == 5
    assert e.zaehler()["transport_fehler"] == 1 and not e.stand()["alter_schluessel"]
    # Der Takt raeumt den alten Schluessel auch ohne neue Pakete ab.
    st.secret_key = list(_SCHLUESSEL)
    e.roh(_paket(b"\xfc\x07", seq=7))
    e.abarbeiten()
    assert e.stand()["alter_schluessel"]
    uhr_.vor(5.1)
    e.takt()
    assert not e.stand()["alter_schluessel"]
    # Faellt der Schluessel ganz weg (MISSING), gilt der vorige trotzdem noch kurz.
    st.secret_key = discord.utils.MISSING
    e.roh(_paket(b"\xfc\x08", seq=8))
    e.abarbeiten()
    assert senke.opus()[-1] == b"\xfc\x08" and e.zaehler()["kein_schluessel"] == 0


def test_empfang_ohne_schluessel_und_mit_fremdem_modus_nichts():
    """Vor op 4 (secret_key MISSING) und mit einer anderen Verschluesselung
    kommt nichts durch - gezaehlt, und der fremde Modus einmal im Log."""
    st = _Zustand()
    st.secret_key = discord.utils.MISSING
    e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=st)
    e.roh(_paket(b"\xfc\x01"))
    e.abarbeiten()
    assert e.zaehler()["kein_schluessel"] == 1 and senke.pakete == []
    st.secret_key = list(_SCHLUESSEL)
    st.mode = "xsalsa20_poly1305_lite"
    with _Fang() as fang:
        for _ in range(3):
            e.roh(_paket(b"\xfc\x02"))
            e.abarbeiten()
    assert e.zaehler()["modus_fremd"] == 3 and senke.pakete == []
    assert [z for z in fang.zeilen if "xsalsa20_poly1305_lite" in z[1]] == [
        (logging.INFO, "Empfang: Server 77 - Verschluesselung xsalsa20_poly1305_lite "
                       "kann ich nicht lesen.")]


# =============================================================================
# seq/ts entfalten
# =============================================================================
def test_empfang_entfalten_ueber_den_ueberlauf():
    """seq (16 Bit) und ts (32 Bit) laufen ueber; entfaltet wird mit der
    +-halber-Bereich-Regel - auch fuer Nachzuegler und Doppelte."""
    f = ve.entfalten
    assert f(None, 65535, 16) == 65535
    assert f(65535, 0, 16) == 65536
    assert f(65536, 65534, 16) == 65534              # verspaetet, vor dem Ueberlauf
    assert f(65536, 2, 16) == 65538
    assert f(3 * 65536 + 5, 3, 16) == 3 * 65536 + 3
    assert f(100, 100, 16) == 100
    assert f(2 ** 32 - 960, 0, 32) == 2 ** 32
    assert f(2 ** 32, 2 ** 32 - 1920, 32) == 2 ** 32 - 1920
    e, _vc, senke, _u = _empfaenger({4242: 5})
    for i, seq in enumerate((65534, 65535, 0, 1)):
        e.roh(_paket(bytes([0xfc, i]), seq=seq, ts=2 ** 32 - 1920 + i * 960))
    e.roh(_paket(b"\xfc\x09", seq=65535, ts=2 ** 32 - 960))     # doppelt/verspaetet
    e.abarbeiten()
    assert [p[2] for p in senke.pakete] == [65534, 65535, 65536, 65537, 65535]
    assert [p[3] for p in senke.pakete] == [2 ** 32 - 1920, 2 ** 32 - 960, 2 ** 32,
                                           2 ** 32 + 960, 2 ** 32 - 960]


# =============================================================================
# Halten (SSRC ohne Zuordnung)
# =============================================================================
def test_empfang_unbekannte_ssrc_wartet_auf_op5_mit_alter_zeit():
    """Ton, bevor op 5 sagt, wem die SSRC gehoert: gehalten. Kommt op 5, geht
    er weiter - mit der ORIGINALEN Ankunftszeit, nicht mit der von op 5."""
    uhr_ = _Uhr(1000.0)
    e, vc, senke, _u = _empfaenger({}, uhr_=uhr_)
    with _angemeldet(77, e):
        e.roh(_paket(b"\xfc\x01", seq=1))
        uhr_.vor(0.5)
        e.roh(_paket(b"\xfc\x02", seq=2))
        e.abarbeiten()
        assert senke.pakete == [] and e.stand()["gehalten"] == 2
        assert e.zaehler()["unbekannt"] == 2
        uhr_.vor(1.0)
        _haken(vc, {"op": 5, "d": {"user_id": "555", "ssrc": 4242, "speaking": 1}})
        assert [(p[0], p[4], p[5]) for p in senke.pakete] == [
            (555, b"\xfc\x01", 1000.0), (555, b"\xfc\x02", 1000.5)]
        assert vc.ssrc_zu_uid == {4242: 555} and e.stand()["gehalten"] == 0
        e.roh(_paket(b"\xfc\x03", seq=3))
        e.abarbeiten()
        assert senke.pakete[-1][0] == 555 and senke.pakete[-1][5] == 1001.5


def test_empfang_gehaltenes_kommt_vor_dem_neuen():
    """Kennt der Client die SSRC inzwischen, ohne dass es diesem Empfaenger
    gemeldet wurde: das naechste Paket bringt das Gehaltene zuerst mit. Ein op
    5 eines FREMDEN Clients gilt nicht."""
    e, vc, senke, _u = _empfaenger({})
    e.roh(_paket(b"\xfc\x01", seq=1))
    e.abarbeiten()
    assert e.ssrc_bekannt(4242, 5, client=_client()) == 0 and senke.pakete == []
    vc.ssrc_zu_uid[4242] = 5
    e.roh(_paket(b"\xfc\x02", seq=2))
    e.abarbeiten()
    assert senke.opus() == [b"\xfc\x01", b"\xfc\x02"]


def test_empfang_halten_verfaellt_und_hat_deckel():
    """Nach HALTEN_UNBEKANNT ist Gehaltenes weg (auch ein spaetes op 5 bringt
    es nicht zurueck). Je SSRC hoechstens HALTEN_UNBEKANNT_PAKETE (die
    aeltesten fallen), hoechstens HALTEN_UNBEKANNT_SSRCS SSRCs."""
    uhr_ = _Uhr(1000.0)
    e, _vc, senke, _u = _empfaenger({}, uhr_=uhr_)
    e.roh(_paket(b"\xfc\x01"))
    e.abarbeiten()
    uhr_.vor(2.4)
    e.takt()
    assert e.stand()["gehalten"] == 1
    uhr_.vor(0.2)
    e.takt()
    assert e.stand()["gehalten"] == 0 and e.zaehler()["unbekannt_verfallen"] == 1
    assert e.ssrc_bekannt(4242, 5) == 0 and senke.pakete == []
    zuviel = ve.HALTEN_UNBEKANNT_PAKETE + 10
    for i in range(zuviel):
        e.roh(_paket(bytes([0xfc, i % 256]), seq=i, ssrc=1))
    e.abarbeiten()
    assert e.stand()["gehalten"] == ve.HALTEN_UNBEKANNT_PAKETE
    assert e.zaehler()["unbekannt_voll"] == 10
    assert e._halten[1][0][1] == 10                     # die aeltesten sind raus
    for ssrc in range(2, 2 + ve.HALTEN_UNBEKANNT_SSRCS + 3):
        e.roh(_paket(b"\xfc", ssrc=ssrc))
    e.abarbeiten()
    assert e.stand()["gehalten_ssrcs"] == ve.HALTEN_UNBEKANNT_SSRCS
    assert e.zaehler()["unbekannt_voll"] == 10 + 4


# =============================================================================
# DAVE
# =============================================================================
def test_empfang_ohne_dave_geht_klartext_durch_markiertes_wartet():
    """DAVE aus (Version 0 oder keine Sitzung): schlichtes Opus geht durch, ein
    markierter Rahmen wartet - der Schluessel kann gleich kommen."""
    for version, dave in ((0, None), (1, None), (0, _FalscheDave())):
        st = _Zustand(dave=dave, version=version)
        e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=st)
        e.roh(_paket(b"\xfc\x01", seq=1))
        e.roh(_paket(_dave_rahmen(b"\xfc\x02", 5), seq=2))
        e.abarbeiten()
        assert senke.opus() == [b"\xfc\x01"], version
        z = e.zaehler()
        assert (z["dave_klar"], z["dave_nochmal"], e.stand()["nochmal"]) == (1, 1, 1)
        if dave is not None:
            assert dave.aufrufe == []                    # Version 0: davey bleibt aussen vor


def test_empfang_dave_rahmen_warten_bis_der_schluessel_kommt():
    """Markierte Rahmen ohne Schluessel warten. Nochmal versucht wird sofort,
    wenn sich (Version, bereit, Epoche) aendert, sonst hoechstens einmal je
    Sekunde - und geliefert mit der ORIGINALEN Ankunftszeit, in Reihenfolge."""
    uhr_ = _Uhr(1000.0)
    dave = _FalscheDave(epoche=1)
    st = _Zustand(dave=dave, version=1)
    e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=st, uhr_=uhr_)
    for i in range(3):
        e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 5), seq=10 + i, ts=960 * i))
        uhr_.vor(0.02)
    e.abarbeiten()
    assert senke.pakete == [] and e.stand()["nochmal"] == 3
    assert len({r for _uid, r in dave.aufrufe}) == 3   # jeder Rahmen einmal probiert
    n = len(dave.aufrufe)
    e.takt()                                          # erster Takt: noch nie nochmal -> Versuch
    assert len(dave.aufrufe) == n + 3
    n = len(dave.aufrufe)
    uhr_.vor(0.3)
    e.takt()
    assert len(dave.aufrufe) == n                     # gleicher Stand, < 1 s: Ruhe
    uhr_.vor(0.8)
    e.takt()
    assert len(dave.aufrufe) == n + 3                 # > 1 s: wieder, je Rahmen einmal
    dave.schluessel.add(5)
    dave.epoch = 2                                    # Schluessel kommt mit der Epoche
    uhr_.vor(0.1)
    e.takt()
    assert senke.opus() == [b"\xfc\x00", b"\xfc\x01", b"\xfc\x02"]
    assert [round(p[5], 3) for p in senke.pakete] == [1000.0, 1000.02, 1000.04]
    assert [p[2] for p in senke.pakete] == [10, 11, 12]
    z = e.zaehler()
    assert (z["dave_ok"], z["dave_nochmal"], e.stand()["nochmal"]) == (3, 3, 0)


def test_empfang_dave_sitzung_wird_je_stapel_frisch_gelesen():
    """discord.py legt bei op 4 evtl. eine neue DaveSession an - die gilt ab
    dem naechsten Stapel, und ein Wechsel loest sofort einen neuen Versuch aus."""
    uhr_ = _Uhr(1000.0)
    alt = _FalscheDave()
    st = _Zustand(dave=alt, version=1)
    e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=st, uhr_=uhr_)
    e.roh(_paket(_dave_rahmen(b"\xfc\x01", 5), seq=1))
    e.abarbeiten()
    e.takt()
    neu = _FalscheDave()
    neu.schluessel.add(5)
    st.dave_session = neu
    e.takt()                                          # anderes Objekt -> sofort
    e.roh(_paket(_dave_rahmen(b"\xfc\x02", 5), seq=2))
    e.abarbeiten()
    assert senke.opus() == [b"\xfc\x01", b"\xfc\x02"]
    assert len(alt.aufrufe) == 2 and len(neu.aufrufe) == 2


def test_empfang_dave_kollision_heisst_nochmal():
    """davey wirft "already borrowed", wenn der Player-Thread gerade
    verschluesselt (RuntimeError) - das ist kein Verlust, sondern ein Nochmal."""
    uhr_ = _Uhr(1000.0)
    dave = _FalscheDave()
    dave.schluessel.add(5)
    dave.wirft = RuntimeError("Already borrowed")
    st = _Zustand(dave=dave, version=1)
    e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=st, uhr_=uhr_)
    e.roh(_paket(_dave_rahmen(b"\xfc\x01", 5)))
    e.abarbeiten()
    e.takt()
    assert senke.pakete == [] and e.stand()["nochmal"] == 1
    dave.wirft = None
    uhr_.vor(1.1)
    e.takt()
    assert senke.opus() == [b"\xfc\x01"] and e.stand()["nochmal"] == 0


def test_empfang_dave_durchlass_nur_fuer_unmarkierte():
    """Scheitert decrypt, geht ein UNMARKIERTER Rahmen durch, wenn davey den
    Absender durchlaesst (Uebergang). Ein markierter nie - der wartet. Stille
    ist nie verschluesselt und geht immer durch, ohne davey zu fragen."""
    dave = _FalscheDave()
    dave.wirft = ValueError("Failed to decrypt: NoDecryptorForUser")
    st = _Zustand(dave=dave, version=1)
    e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=st)
    e.roh(_paket(b"\xfc\x01", seq=1))
    e.abarbeiten()
    assert senke.pakete == [] and e.stand()["nochmal"] == 1      # ohne Durchlass: warten
    dave.durchlass.add(5)
    e.roh(_paket(b"\xfc\x02", seq=2))
    e.roh(_paket(_dave_rahmen(b"\xfc\x03", 5), seq=3))
    e.abarbeiten()
    # Der neue Rahmen laesst erst den wartenden ran: beide, in Reihenfolge.
    assert senke.opus() == [b"\xfc\x01", b"\xfc\x02"] and e.zaehler()["dave_klar"] == 2
    assert e.stand()["nochmal"] == 1                  # der markierte wartet weiter
    e.takt()
    assert len(senke.pakete) == 2 and e.stand()["nochmal"] == 1
    n = len(dave.aufrufe)
    e.roh(_paket(ve.STILLE, seq=4))
    e.abarbeiten()
    assert senke.opus()[-1] == ve.STILLE and e.zaehler()["dave_klar"] == 3
    assert len(dave.aufrufe) == n + 1                 # nur der wartende Kopf, nicht die Stille
    assert e.stand()["nochmal"] == 1                  # Stille macht nichts "tot"
    dave.wirft = None                                 # davey selbst laesst Unmarkiertes durch:
    e.roh(_paket(b"\xfc\x05", seq=5))                 # auch das setzt keine Nonce-Grenze
    e.abarbeiten()
    assert senke.opus()[-1] == b"\xfc\x05" and e.zaehler()["dave_ok"] == 0
    assert e.stand()["nochmal"] == 1                  # der markierte wartet weiter


def test_empfang_dave_gibt_nach_zehn_sekunden_auf_und_deckelt():
    uhr_ = _Uhr(1000.0)
    dave = _FalscheDave()
    st = _Zustand(dave=dave, version=1)
    e, _vc, senke, _u = _empfaenger({4242: 5, 4343: 6}, zustand=st, uhr_=uhr_)
    e.roh(_paket(_dave_rahmen(b"\xfc\x01", 5), seq=1))
    e.abarbeiten()
    e.takt()
    uhr_.vor(9.9)
    e.takt()
    assert e.stand()["nochmal"] == 1
    uhr_.vor(0.2)
    e.takt()
    assert e.stand()["nochmal"] == 0 and e.zaehler()["dave_verworfen"] == 1
    dave.schluessel.add(5)
    dave.epoch = 9
    e.takt()
    assert senke.pakete == []                         # weg ist weg
    zuviel = ve.HALTEN_DAVE_PAKETE + 7
    for i in range(zuviel):
        e.roh(_paket(_dave_rahmen(bytes([0xfc, i % 256]), 6), seq=i, ssrc=4343))
    e.abarbeiten()
    assert e.stand()["nochmal"] == ve.HALTEN_DAVE_PAKETE
    assert e.zaehler()["dave_verworfen"] == 1 + 7
    assert e._nochmal[4343][0][3] == 7                # die aeltesten sind raus


def test_empfang_dave_attrappe_ist_so_streng_wie_davey():
    """Die Attrappe taugt nur, wenn sie davey gleicht. Dieselbe Folge am
    ECHTEN davey (eine Gruppe, die der Test selbst baut) und an ihr: je
    Absender nur vorwaerts - der erste Erfolg setzt die Grenze, ohne Aelteres
    als fehlend zu merken; danach Uebersprungenes darf nachkommen; mit einem
    fremden Absender geht nie etwas auf, und der Fehlversuch aendert nichts."""
    opus = [bytes([0xfc, i, 1, 2, 3]) for i in range(10)]
    folge = (3, 0, 1, 2, 4, 6, 5, 9, 7, 8)
    erwartet = [True, False, False, False, True, True, True, True, True, True]

    def muster(dave, anna, fremd, rahmen):
        erg = []
        for i in folge:
            for uid in (fremd, anna):
                try:
                    klar = dave.decrypt(uid, davey.MediaType.audio, rahmen[i])
                except ValueError:
                    klar = None
                if uid == fremd:
                    assert klar is None, "fremder Absender ging auf"
                else:
                    erg.append(klar == opus[i])
        return erg
    attrappe = _FalscheDave()
    attrappe.schluessel |= {11, 33}
    assert muster(attrappe, 11, 33, [_dave_rahmen(o, 11) for o in opus]) == erwartet
    echt = _echte_gruppe(11, 22, 33)
    if echt is None:
        return                                       # davey baut hier keine Gruppe
    _gruppe, s = echt
    assert muster(s[22], 11, 33, [s[11].encrypt_opus(o) for o in opus]) == erwartet


def test_empfang_dave_neuer_rahmen_laesst_erst_das_wartende_ran():
    """Der Schluessel kommt - mit neuer Epoche oder (falls davey das je ohne
    tut) still -, und schon im NAECHSTEN Stapel kommt ein neuer Rahmen. Ginge
    der zuerst an davey, setzte er die Nonce-Grenze, und die ganze Sekunde
    davor waere fuer immer zu. Also erst das Wartende, in Reihenfolge: alle 60
    kommen an, mit ihrer Zeit, nichts wird verworfen."""
    for mit_epoche in (True, False):
        uhr_ = _Uhr(1000.0)
        dave = _FalscheDave(epoche=1)
        e, _vc, senke, _u = _empfaenger({4242: 5}, zustand=_Zustand(dave=dave, version=1),
                                        uhr_=uhr_)
        for i in range(60):
            if i == 50:                               # nach 1 s Sprache kommt der Schluessel
                dave.schluessel.add(5)
                if mit_epoche:
                    dave.epoch = 2
            e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 5), seq=i, ts=960 * i))
            e.abarbeiten()
            uhr_.vor(0.02)
            if i == 49:
                assert e.stand()["nochmal"] == 50 and senke.pakete == []
        for _ in range(50):                           # 12,5 s Takt: nichts kommt nach
            uhr_.vor(0.25)
            e.takt()
        assert [p[2] for p in senke.pakete] == list(range(60)), mit_epoche
        assert [round(p[5] - 1000.0, 2) for p in senke.pakete] == [
            round(0.02 * i, 2) for i in range(60)]
        z = e.zaehler()
        assert (z["dave_ok"], z["dave_verworfen"], e.stand()["nochmal"]) == (60, 0, 0)


def test_empfang_dave_op5_laesst_erst_das_wartende_ran():
    """davey zaehlt je ABSENDER, nicht je SSRC. Wartet von Anna noch etwas auf
    SSRC 1 und kam ihr Schluessel inzwischen (neue Epoche), gibt auch op 5 ihr
    Gehaltenes auf SSRC 2 erst frei, nachdem das Wartende dran war."""
    dave = _FalscheDave()
    e, vc, senke, _u = _empfaenger({1: 5}, zustand=_Zustand(dave=dave, version=1))
    with _angemeldet(77, e):
        for i in range(6):
            e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 5), seq=i, ssrc=1 if i < 3 else 2))
        e.abarbeiten()
        assert (e.stand()["nochmal"], e.stand()["gehalten"]) == (3, 3)
        dave.schluessel.add(5)
        dave.epoch = 2
        _haken(vc, {"op": 5, "d": {"user_id": "5", "ssrc": 2}})
    assert senke.opus() == [bytes([0xfc, i]) for i in range(6)]
    assert (e.zaehler()["dave_verworfen"], e.stand()["nochmal"]) == (0, 0)


def test_empfang_dave_tote_rahmen_fliegen_sofort_raus():
    """Rahmen aus einer Epoche, die Flo nie hatte (er kam spaeter dazu), gehen
    nie auf. Sobald ein JUENGERER desselben Absenders aufgeht, steht das fest:
    sie fliegen sofort raus (gezaehlt) - statt 10 s lang jede Sekunde
    vergeblich probiert zu werden. Ein anderer Absender wartet weiter."""
    uhr_ = _Uhr(1000.0)
    dave = _FalscheDave()
    dave.schluessel.add(5)
    st = _Zustand(dave=dave, version=1)
    e, _vc, senke, _u = _empfaenger({4242: 5, 4343: 6}, zustand=st, uhr_=uhr_)
    for i in range(5):
        e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 5, alt=True), seq=i))
    e.roh(_paket(_dave_rahmen(b"\xfc\x66", 6), seq=1, ssrc=4343))    # 6: Schluessel fehlt
    e.abarbeiten()
    assert e.stand()["nochmal"] == 6
    e.roh(_paket(_dave_rahmen(b"\xfc\x05", 5), seq=5))
    e.abarbeiten()
    assert senke.opus() == [b"\xfc\x05"]
    assert (e.zaehler()["dave_verworfen"], e.stand()["nochmal"]) == (5, 1)
    n = len(dave.aufrufe)
    for _ in range(4):
        uhr_.vor(1.1)
        e.takt()
    assert len(dave.aufrufe) == n + 4                 # nur noch der eine von 6
    # Gemischt: von 6 warten lesbare, tote und wieder lesbare Rahmen. Sein
    # Schluessel kommt, und im naechsten Stapel gleich ein neuer Rahmen: der
    # neue DAVE-Stand laesst ZUERST alles Wartende ran (nicht nur bis zum
    # ersten toten) - die lesbaren kommen an, die toten fliegen raus.
    for i in range(3):
        e.roh(_paket(_dave_rahmen(bytes([0xfc, 0x70 + i]), 6, alt=True), seq=2 + i,
                     ssrc=4343))
    e.roh(_paket(_dave_rahmen(b"\xfc\x80", 6), seq=5, ssrc=4343))
    e.abarbeiten()
    assert e.stand()["nochmal"] == 5
    dave.schluessel.add(6)
    dave.epoch = 2
    e.roh(_paket(_dave_rahmen(b"\xfc\x81", 6), seq=6, ssrc=4343))
    e.abarbeiten()
    assert senke.opus() == [b"\xfc\x05", b"\xfc\x66", b"\xfc\x80", b"\xfc\x81"]
    assert (e.zaehler()["dave_verworfen"], e.stand()["nochmal"]) == (5 + 3, 0)


def test_empfang_dave_tot_ist_nur_was_derselbe_absender_davor_schickte():
    """'Tot' heisst: ein juengerer Rahmen DESSELBEN Absenders ging mit DAVE
    auf. Nicht tot macht: ein anderer Absender auf derselben SSRC (sie wechselte
    den Besitzer), ein unmarkierter Rahmen, den davey durchlaesst, und Stille -
    die setzen bei davey keine Nonce-Grenze."""
    dave = _FalscheDave()
    e, vc, senke, _u = _empfaenger({4343: 6}, zustand=_Zustand(dave=dave, version=1))
    e.roh(_paket(_dave_rahmen(b"\xfc\x60", 6), seq=1, ssrc=4343))      # 6: Schluessel fehlt
    e.roh(_paket(b"\xfc\x61", seq=2, ssrc=4343))                       # unmarkiert, kein Durchlass
    e.abarbeiten()
    assert e.stand()["nochmal"] == 2
    dave.durchlass.add(6)                                              # Uebergang beginnt ...
    dave.epoch = 2                                                     # ... mit neuem Stand
    e.roh(_paket(ve.STILLE, seq=3, ssrc=4343))
    e.abarbeiten()
    assert senke.opus() == [b"\xfc\x61", ve.STILLE] and e.stand()["nochmal"] == 1
    vc.ssrc_zu_uid[4343] = 7                                           # SSRC gehoert jetzt 7
    dave.schluessel.add(7)
    e.roh(_paket(_dave_rahmen(b"\xfc\x70", 7), seq=4, ssrc=4343))
    e.abarbeiten()
    assert senke.opus()[-1] == b"\xfc\x70"
    assert (e.zaehler()["dave_verworfen"], e.stand()["nochmal"]) == (0, 1)
    dave.schluessel.add(6)                                             # 6s Schluessel kommt doch
    dave.epoch = 3
    e.takt()
    assert senke.opus()[-1] == b"\xfc\x60" and e.stand()["nochmal"] == 0


def test_empfang_dave_neuer_rahmen_kostet_hoechstens_zwei_versuche():
    """Warten viele Rahmen einer SSRC (Schluessel fehlt), probiert ein neuer
    nur den aeltesten wartenden und sich selbst - nicht die ganze Schlange:
    sonst kostete jeder Rahmen bis zu 512 davey-Versuche (quadratisch)."""
    dave = _FalscheDave()
    e, _vc, _senke, _u = _empfaenger({4242: 5}, zustand=_Zustand(dave=dave, version=1))
    for i in range(100):
        e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 5), seq=i))
    e.abarbeiten()
    assert e.stand()["nochmal"] == 100
    n = len(dave.aufrufe)
    e.roh(_paket(_dave_rahmen(b"\xfc\xff", 5), seq=100))
    e.abarbeiten()
    assert len(dave.aufrufe) == n + 2 and e.stand()["nochmal"] == 101


def test_empfang_echtes_dave_spaet_dazu_nichts_geht_verloren():
    """ECHTES davey: Flo kommt in einen laufenden Call (Anna und Carl). Annas
    Rahmen aus der alten Epoche kann er nie lesen; die aus der neuen kommen
    an, BEVOR sein Welcome verarbeitet ist (warten), danach im selben Stapel
    weitere. Alles aus der neuen Epoche kommt an, in Reihenfolge und
    bytegleich; die alten fliegen sofort raus."""
    echt = _echte_gruppe(11, 33)
    if echt is None:
        return                                       # davey baut hier keine Gruppe
    gruppe, s = echt
    anna = s[11]
    flo = gruppe.sitzung(22)
    alt = [anna.encrypt_opus(bytes([0xfc, i, 9])) for i in range(5)]
    willkommen = gruppe.aufnehmen(anna, flo)          # ab jetzt verschluesselt Anna neu
    neu_opus = [bytes([0xfc, 100 + i, 9]) for i in range(20)]
    neu = [anna.encrypt_opus(o) for o in neu_opus]
    e, _vc, senke, _u = _empfaenger({4242: 11}, zustand=_Zustand(dave=flo, version=1))
    for i, r in enumerate(alt + neu[:10]):
        e.roh(_paket(r, seq=i, ts=960 * i))
    e.abarbeiten()
    assert senke.pakete == [] and e.stand()["nochmal"] == 15
    flo.process_welcome(willkommen)
    for i, r in enumerate(neu[10:], start=15):
        e.roh(_paket(r, seq=i, ts=960 * i))
    e.abarbeiten()
    e.takt()
    assert senke.opus() == neu_opus
    assert [p[2] for p in senke.pakete] == list(range(5, 25))
    z = e.zaehler()
    assert (z["dave_ok"], z["dave_verworfen"], e.stand()["nochmal"]) == (20, 5, 0)


def test_empfang_echtes_dave_probe_findet_den_absender():
    """ECHTES davey, SSRC ohne op 5: die Probe fragt Carl (geht nicht auf -
    DAVE ist authentifiziert) und Anna, mit Annas AELTESTEM Rahmen zuerst. Die
    aus der alten Epoche gehen mit niemandem auf und fliegen raus; ab dem
    ersten lesbaren kommt alles an."""
    echt = _echte_gruppe(11, 5)                       # Anna 11, Carl 5 (wird zuerst gefragt)
    if echt is None:
        return
    gruppe, s = echt
    anna = s[11]
    flo = gruppe.sitzung(22)
    alt = [anna.encrypt_opus(bytes([0xfc, i, 9])) for i in range(5)]
    flo.process_welcome(gruppe.aufnehmen(anna, flo))
    neu_opus = [bytes([0xfc, 100 + i, 9]) for i in range(40)]
    neu = [anna.encrypt_opus(o) for o in neu_opus]
    uhr_ = _Uhr(1000.0)
    e, vc, senke, _u = _empfaenger({}, zustand=_Zustand(dave=flo, version=1), uhr_=uhr_)
    vc.im_call = {5, 11}
    for i, r in enumerate(alt + neu):
        e.roh(_paket(r, seq=i, ts=960 * i, ssrc=9))
        uhr_.vor(0.02)
    e.abarbeiten()
    uhr_.vor(0.2)
    e.takt()
    assert vc.ssrc_zu_uid == {9: 11}
    assert senke.opus() == neu_opus
    z = e.zaehler()
    assert (z["geraten"], z["dave_ok"], z["dave_verworfen"]) == (1, 40, 5)


def test_empfang_opt_out_faellt_vor_dave_ohne_entschluesselung():
    """Wer raus ist, wird NIE entschluesselt - nicht direkt, nicht nach dem
    Halten, nicht beim Nochmal-Versuch. Wirft die Frage selbst, gilt er als raus."""
    dave = _FalscheDave()
    dave.schluessel |= {5, 6}
    raus = {5}
    st = _Zustand(dave=dave, version=1)
    e, vc, senke, _u = _empfaenger({4242: 5, 4343: 6}, zustand=st,
                                   opt_out=lambda uid: uid in raus)
    with _angemeldet(77, e):
        for i in range(3):
            e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 5), seq=i))
        e.roh(_paket(_dave_rahmen(b"\xfc\x09", 6), seq=1, ssrc=4343))
        e.abarbeiten()
        assert _uids(dave) == [6] and e.zaehler()["raus"] == 3
        assert [p[0] for p in senke.pakete] == [6]
        e.roh(_paket(_dave_rahmen(b"\xfc\x0a", 5), seq=5, ssrc=4444))   # noch unbekannt
        e.abarbeiten()
        _haken(vc, {"op": 5, "d": {"user_id": "5", "ssrc": 4444}})
        assert _uids(dave) == [6] and e.zaehler()["raus"] == 4
        dave.schluessel.discard(6)
        e.roh(_paket(_dave_rahmen(b"\xfc\x0b", 6), seq=2, ssrc=4343))
        e.abarbeiten()
        n = len(dave.aufrufe)
        raus.add(6)                                    # steigt aus, waehrend er wartet
        dave.epoch = 2
        e.takt()
        assert len(dave.aufrufe) == n and e.stand()["nochmal"] == 0
        assert e.zaehler()["raus"] == 5 and [p[0] for p in senke.pakete] == [6]

    def kaputt(_uid):
        raise RuntimeError("Store weg")
    e2, _vc2, senke2, _u2 = _empfaenger({4242: 5}, opt_out=kaputt)
    e2.roh(_paket(b"\xfc\x01"))
    e2.abarbeiten()
    assert senke2.pakete == [] and e2.zaehler()["raus"] == 1


def test_empfang_opt_out_bekannter_ssrc_nicht_einmal_transport():
    """Die SSRC steht im Klartext-Kopf. Ist sie schon zugeordnet und ihr
    Mensch raus, wird sein Paket nicht einmal transport-entschluesselt (ohne
    DAVE laege sonst kurz sein Klartext-Opus im Speicher). Eine unbekannte SSRC
    muss auf - wem sie gehoert, weiss erst op 5; dann faengt die Frage vor DAVE."""
    e, vc, senke, _u = _empfaenger({4242: 5}, opt_out=lambda uid: uid == 5)
    with mock.patch.object(ve, "transport_entschluesseln",
                           wraps=ve.transport_entschluesseln) as transport:
        e.roh(_paket(b"\xfc\x01"))
        e.roh(_paket(b"\xfc\x02", schluessel=_ANDERER))          # kaputt: egal, raus ist raus
        e.roh(_paket(b"\xfc\x03", ssrc=4343))                     # unbekannt: muss auf
        e.abarbeiten()
    assert transport.call_count == 1
    z = e.zaehler()
    assert (z["raus"], z["transport_fehler"], z["unbekannt"]) == (2, 0, 1)
    assert senke.pakete == [] and e.stand()["gehalten"] == 1
    # Erfaehrt der Client die SSRC, ohne es diesem Empfaenger zu melden: das
    # naechste Paket nimmt auch das Gehaltene von vorher mit raus.
    vc.ssrc_zu_uid[4343] = 5
    e.roh(_paket(b"\xfc\x04", ssrc=4343))
    e.abarbeiten()
    assert (e.zaehler()["raus"], e.stand()["gehalten"], senke.pakete) == (4, 0, [])


def test_empfang_vergessen_raeumt_gehaltenes_und_wartendes():
    """Opt-out mitten im Call: was von dem Menschen gehalten wird oder auf DAVE
    wartet, ist sofort weg - die anderen bleiben."""
    dave = _FalscheDave()
    st = _Zustand(dave=dave, version=1)
    e, vc, senke, _u = _empfaenger({1: 5, 2: 6}, zustand=st)
    e.roh(_paket(_dave_rahmen(b"\xfc\x01", 5), ssrc=1))
    e.roh(_paket(_dave_rahmen(b"\xfc\x02", 6), ssrc=2))
    e.roh(_paket(_dave_rahmen(b"\xfc\x03", 5), ssrc=3))      # SSRC 3 noch unbekannt
    e.abarbeiten()
    assert (e.stand()["nochmal"], e.stand()["gehalten"]) == (2, 1)
    vc.ssrc_zu_uid[3] = 5                                    # inzwischen bekannt
    e.vergessen(5)
    assert (e.stand()["nochmal"], e.stand()["gehalten"]) == (1, 0)
    dave.schluessel |= {5, 6}
    dave.epoch = 2
    e.takt()
    assert [p[0] for p in senke.pakete] == [6]


def test_empfang_probe_ordnet_ssrc_ohne_op5_zu():
    """Wer schon sprach, bevor Flo kam, bekommt evtl. nie ein op 5. Nach
    PROBE_AB wird geraten: der aelteste DAVE-Rahmen mit den Schluesseln der
    unzugeordneten Leute im Call (op 11/5 und wer im Kanal sitzt). Aufgehen
    kann er nur beim wahren Absender. Wer raus ist, wird nie gefragt - auch
    nicht, wenn es sein eigener Rahmen ist. ALLES kommt an (die Attrappe ist so
    streng wie davey: mit dem juengsten geraten, waere der Rest zu), jeder
    Rahmen mit seiner alten Zeit."""
    uhr_ = _Uhr(1000.0)
    dave = _FalscheDave()
    dave.schluessel |= {5, 6, 7, 8}
    st = _Zustand(dave=dave, version=1)
    e, vc, senke, _u = _empfaenger({}, zustand=st, uhr_=uhr_, opt_out=lambda u: u == 7)
    vc.im_call = {5, 6, 7}
    vc.channel.members = [SimpleNamespace(id=8)]
    for i in range(3):
        e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 8), seq=i, ssrc=9))
        uhr_.vor(0.02)
    e.roh(_paket(_dave_rahmen(b"\xfc\x77", 7), seq=1, ssrc=10))     # der, der raus ist
    e.abarbeiten()
    uhr_.vor(0.5)
    e.takt()
    assert dave.aufrufe == [] and senke.pakete == []         # noch zu frisch
    uhr_.vor(0.6)
    e.takt()
    assert vc.ssrc_zu_uid == {9: 8} and e.zaehler()["geraten"] == 1
    assert senke.opus() == [b"\xfc\x00", b"\xfc\x01", b"\xfc\x02"]
    assert [p[0] for p in senke.pakete] == [8, 8, 8]
    assert [round(p[5], 3) for p in senke.pakete] == [1000.0, 1000.02, 1000.04]
    assert 7 not in _uids(dave) and _uids(dave)[:3] == [5, 6, 8]
    assert e.stand()["gehalten"] == 1                        # SSRC 10 bleibt ungeraten
    uhr_.vor(2.0)
    e.takt()
    assert e.stand()["gehalten"] == 0 and 7 not in _uids(dave)
    assert vc.ssrc_zu_uid == {9: 8}


def test_empfang_probe_nie_im_durchlass_nie_unmarkiert_hoechstens_alle_5s():
    uhr_ = _Uhr(1000.0)
    dave = _FalscheDave()
    dave.schluessel.add(5)
    dave.durchlass.add(5)                     # im Durchlass ginge alles "auf"
    st = _Zustand(dave=dave, version=1)
    e, vc, _senke, _u = _empfaenger({}, zustand=st, uhr_=uhr_)
    vc.im_call = {5, 6}
    e.roh(_paket(_dave_rahmen(b"\xfc\x01", 5), seq=1, ssrc=9))
    e.roh(_paket(b"\xfc\x02", seq=1, ssrc=10))              # unmarkiert: nie raten
    e.abarbeiten()
    uhr_.vor(1.1)
    e.takt()
    assert _uids(dave) == [6] and vc.ssrc_zu_uid == {}       # 5 uebersprungen, 6 passt nicht
    e.roh(_paket(_dave_rahmen(b"\xfc\x03", 5), seq=2, ssrc=9))
    e.abarbeiten()
    uhr_.vor(0.3)
    e.takt()
    assert _uids(dave) == [6]                                # nicht schon wieder
    # Ohne bereite DAVE-Sitzung wird gar nicht geraten.
    dave2 = _FalscheDave(bereit=False)
    dave2.schluessel.add(5)
    e2, vc2, _s2, uhr2 = _empfaenger({}, zustand=_Zustand(dave=dave2, version=1))
    vc2.im_call = {5}
    e2.roh(_paket(_dave_rahmen(b"\xfc\x01", 5), ssrc=9))
    e2.abarbeiten()
    uhr2.vor(1.1)
    e2.takt()
    assert dave2.aufrufe == [] and vc2.ssrc_zu_uid == {}


def test_empfang_probe_raet_mit_dem_aeltesten_tote_davor_fliegen_raus():
    """Vor Flos Ankunft verschluesselte Rahmen (alte Epoche) gehen mit
    niemandem auf. Die Probe arbeitet sich vom aeltesten Rahmen vor, bis einer
    aufgeht: der ordnet die SSRC zu, alles danach kommt an, die toten davor
    fliegen gezaehlt raus. Je Takt hoechstens PROBE_VERSUCHE davey-Versuche."""
    uhr_ = _Uhr(1000.0)
    dave = _FalscheDave()
    dave.schluessel |= {5, 8}
    e, vc, senke, _u = _empfaenger({}, zustand=_Zustand(dave=dave, version=1), uhr_=uhr_)
    vc.im_call = {5, 8}
    for i in range(60):
        e.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 8, alt=i < 10), seq=i, ts=960 * i,
                     ssrc=9))
        uhr_.vor(0.02)
    e.abarbeiten()
    uhr_.vor(0.1)
    e.takt()
    assert vc.ssrc_zu_uid == {9: 8}
    assert [p[2] for p in senke.pakete] == list(range(10, 60))
    assert [round(p[5] - 1000.0, 2) for p in senke.pakete[:2]] == [0.2, 0.22]
    z = e.zaehler()
    assert (z["geraten"], z["dave_ok"], z["dave_verworfen"]) == (1, 50, 10)
    assert (e.stand()["nochmal"], e.stand()["gehalten"]) == (0, 0)
    # Probe: 10 tote x 2 Leute + 2 fuer den Treffer; danach je Rahmen einer.
    # Die toten davor werden nicht noch einmal probiert.
    assert len(dave.aufrufe) == 10 * 2 + 2 + 49
    # Der Deckel: 5 Leute, nur tote Rahmen - nach PROBE_VERSUCHE ist Schluss.
    dave2 = _FalscheDave()
    dave2.schluessel |= {1, 2, 3, 4, 5}
    e2, vc2, _s2, uhr2 = _empfaenger({}, zustand=_Zustand(dave=dave2, version=1))
    vc2.im_call = {1, 2, 3, 4, 5}
    for i in range(20):
        e2.roh(_paket(_dave_rahmen(bytes([0xfc, i]), 5, alt=True), seq=i, ssrc=9))
    e2.abarbeiten()
    uhr2.vor(1.1)
    with mock.patch.object(ve, "PROBE_VERSUCHE", 7):
        e2.takt()
    assert len(dave2.aufrufe) == 7 and vc2.ssrc_zu_uid == {}
    assert e2.stand()["gehalten"] == 20                     # nichts verloren, nur vertagt


# =============================================================================
# Der Websocket-Haken
# =============================================================================
def test_empfang_ws_haken_versteht_op_5_11_13_und_4():
    e, vc, _senke, _u = _empfaenger({})
    rueck = []
    e.bei_verbindung = rueck.append
    with _angemeldet(77, e):
        _haken(vc, {"op": 11, "d": {"user_ids": ["1", "2", 3]}})
        assert vc.im_call == {1, 2, 3}
        _haken(vc, {"op": 5, "d": {"user_id": "1", "ssrc": 100, "speaking": 1}})
        _haken(vc, {"op": 5, "d": {"user_id": "1", "ssrc": 101, "speaking": 0}})
        _haken(vc, {"op": 5, "d": {"user_id": "2", "ssrc": 200, "speaking": 1}})
        assert vc.ssrc_zu_uid == {100: 1, 101: 1, 200: 2}
        e.roh(_paket(b"\xfc", ssrc=100))
        e.abarbeiten()
        assert e.stand()["straenge"] == 1
        _haken(vc, {"op": 13, "d": {"user_id": "1"}})
        assert vc.ssrc_zu_uid == {200: 2} and vc.im_call == {2, 3}
        assert e.stand()["straenge"] == 0
        nr = e.verbindung_nr
        _haken(vc, {"op": 4, "d": {"mode": ve.MODUS}})               # Neuaufbau, selber Kanal
        assert vc.verbindungen == 1 and vc.ssrc_zu_uid == {200: 2}
        assert e.verbindung_nr == nr + 1 and rueck == [vc]
        vc.channel.id = 43                                           # verschoben
        _haken(vc, {"op": 4, "d": {}})
        assert vc.verbindungen == 2 and vc.ssrc_zu_uid == {} and vc.im_call == set()
        assert rueck == [vc, vc] and e.client is None              # neuer Kanal: ab
        _haken(vc, {"op": 12, "d": {"user_id": "9", "audio_ssrc": 900}})   # aelteres Format
        _haken(vc, {"op": 12, "d": {"user_id": "8", "audio_ssrc": 0}})     # 0 = kein Ton
        assert vc.ssrc_zu_uid == {900: 9}
    _haken(vc, {"op": 5, "d": {"user_id": "4", "ssrc": 400}})      # ohne Empfaenger: merkt es sich
    assert vc.ssrc_zu_uid[400] == 4


def test_empfang_ws_haken_wirft_nie():
    """Der Haken laeuft in discord.py's poll_event(): eine Ausnahme beendete den
    Poller der Verbindung. Unsinn, kaputte Felder, ein Empfaenger, der bei
    allem wirft, ein Client ohne Kanal - nichts davon kommt heraus."""
    _e, vc, _senke, _u = _empfaenger({})
    nachrichten = (
        None, "text", 5, [], {}, {"op": 5}, {"op": 5, "d": None}, {"op": 5, "d": []},
        {"op": 5, "d": {"ssrc": "x", "user_id": "y"}}, {"op": 5, "d": {"ssrc": -1, "user_id": 3}},
        {"op": 5, "d": {"ssrc": 2 ** 40, "user_id": 3}},
        {"op": 5, "d": {"ssrc": True, "user_id": 3}},
        {"op": 5, "d": {"ssrc": 7, "user_id": "1" * 30}}, {"op": 13, "d": {"user_id": None}},
        {"op": 11, "d": {"user_ids": None}}, {"op": 11, "d": {"user_ids": "123"}},
        {"op": 11, "d": {"user_ids": [None, "x", {}]}}, {"op": 4, "d": {}},
        {"op": "5", "d": {"ssrc": 1, "user_id": 1}},
        {"op": 12, "d": {"user_id": "9", "audio_ssrc": None}},
    )

    class _Bombe:
        def __getattr__(self, name):
            raise RuntimeError(name)

    for angemeldet in (None, _e, _Bombe()):
        if angemeldet is not None:
            ve.anmelden(77, angemeldet)
        try:
            for msg in nachrichten:
                _haken(vc, msg)
            _haken(vc, {"op": 5, "d": {"ssrc": 9, "user_id": "9"}})
            _haken(vc, {"op": 13, "d": {"user_id": "9"}})
            _haken(vc, {"op": 4, "d": {}})
        finally:
            ve.abmelden(77)
    assert vc.ssrc_zu_uid == {}                       # nur Gueltiges, und 9 ging wieder
    ohne = _client()
    ohne.channel = None
    _haken(ohne, {"op": 5, "d": {"ssrc": 9, "user_id": "9"}})
    _haken(ohne, {"op": 4, "d": {}})
    assert ohne.ssrc_zu_uid == {9: 9} and ohne.verbindungen == 1


class _WsAttrappe:
    """Das aiohttp-Websocket unter DiscordVoiceWebSocket."""

    def __init__(self):
        self.gesendet = []

    async def send_str(self, text):
        self.gesendet.append(text)


def _echter_client():
    """Ein EmpfangsClient mit dem ECHTEN __init__ - discord.py startet dabei
    seinen SocketReader-Thread (der Test haelt ihn danach an)."""
    verbindung = SimpleNamespace(loop=None, user=SimpleNamespace(id=1))
    bot = SimpleNamespace(_connection=verbindung)
    kanal = SimpleNamespace(id=42, guild=SimpleNamespace(id=77))
    return ve.EmpfangsClient(bot, kanal)


def _leser_stoppen(vc):
    """SocketReader anhalten. Mehrmals, weil run() beim Start _end loescht -
    ein stop() davor ginge verloren."""
    leser = vc._connection._socket_reader
    for _ in range(500):
        leser.stop()
        leser.join(0.01)
        if not leser.is_alive():
            return
    raise AssertionError("der SocketReader laeuft weiter")


def test_empfang_haken_haengt_wirklich_am_voice_websocket():
    """create_connection_state() baut den ECHTEN VoiceConnectionState mit dem
    Haken; discord.py's received_message ruft ihn nach op 5 und op 4 auf. Und
    anhaengen() landet beim echten SocketReader."""
    vc = _echter_client()
    try:
        st = vc._connection
        assert isinstance(st, discord.voice_state.VoiceConnectionState)
        assert st.hook == vc._ws_hook
        assert (vc.ssrc_zu_uid, vc.im_call, vc.verbindungen) == ({}, set(), 0)
        ws = discord.gateway.DiscordVoiceWebSocket(_WsAttrappe(), None, hook=st.hook)
        ws._connection = st
        st.ssrc = 1
        asyncio.run(ws.received_message(
            {"op": 5, "d": {"user_id": "77", "ssrc": 1234, "speaking": 1}}))
        assert vc.ssrc_zu_uid == {1234: 77}
        asyncio.run(ws.received_message({"op": 4, "d": {
            "mode": ve.MODUS, "secret_key": list(range(32)), "dave_protocol_version": 0}}))
        assert vc.verbindungen == 1 and st.mode == ve.MODUS
        assert st.secret_key == list(range(32))
        asyncio.run(ws.received_message({"op": 13, "d": None}))     # Unsinn: kein Wurf
        e = ve.Empfaenger(77, _Senke())
        assert e.anhaengen(vc) and e.roh in st._socket_reader._callbacks
        e.abhaengen()
        assert e.roh not in st._socket_reader._callbacks
    finally:
        _leser_stoppen(vc)


# =============================================================================
# An- und abhaengen, Neuaufbau
# =============================================================================
def test_empfang_anhaengen_ist_idempotent_und_wechselt_sauber():
    e = ve.Empfaenger(77, _Senke())
    senke = e.senke
    st1 = _Zustand()
    vc1 = _client({1: 5}, zustand=st1)
    assert e.anhaengen(vc1) and e.anhaengen(vc1)
    assert len(st1.lauscher) == 1 and e.verbindung_nr == 1     # nicht doppelt angemeldet
    st1.lauscher[0](_paket(b"\xfc\x01", ssrc=1, seq=500))
    st1.lauscher[0](_paket(b"\xfc\x02", ssrc=2))                # unbekannt -> gehalten
    e.abarbeiten()
    assert senke.pakete[0][1] == _strang(1, 1) and e.stand()["gehalten"] == 1
    st2 = _Zustand()
    vc2 = _client({1: 5}, zustand=st2)
    assert e.anhaengen(vc2)
    assert st1.lauscher == [] and len(st2.lauscher) == 1 and e.client is vc2
    assert e.verbindung_nr == 2
    assert (e.stand()["gehalten"], e.stand()["straenge"]) == (0, 0)
    st2.lauscher[0](_paket(b"\xfc\x03", ssrc=1, seq=1))
    e.abarbeiten()
    assert senke.pakete[-1][1:3] == (_strang(2, 1), 1)          # neuer Strang, frisch entfaltet
    st2.lauscher[0](_paket(b"\xfc\x04", ssrc=1, seq=2))         # liegt noch im Eingang
    e.abhaengen()
    e.abhaengen()
    assert st2.lauscher == [] and e.client is None and not e.stand()["angehaengt"]
    assert e.abarbeiten() == 0 and len(senke.pakete) == 2      # nie an die Senke geliefert
    vc2._connection = st3 = _Zustand()                          # neue Verbindung, selber Client
    assert e.anhaengen(vc2) and len(st3.lauscher) == 1 and e.verbindung_nr == 3
    vc2.channel.id = 99                                         # verschoben: neu, nicht doppelt
    assert e.anhaengen(vc2) and len(st3.lauscher) == 1 and e.verbindung_nr == 4
    assert e.anhaengen(vc2) and e.verbindung_nr == 4
    assert e.anhaengen(None) is False and e.client is vc2
    assert e.anhaengen(SimpleNamespace()) is False

    class _Stur(_Zustand):
        def add_socket_listener(self, cb):
            raise RuntimeError("geht nicht")
    assert e.anhaengen(_client(zustand=_Stur())) is False and e.client is None


def test_empfang_op4_desselben_clients_trennt_die_straenge():
    """Neuaufbau (op 4) am eigenen Client: was noch im Eingang liegt, gehoert
    zur alten Verbindung und kommt mit dem alten Strang; danach ein neuer. Ein
    fremder Client zaehlt nichts hoch - bei_verbindung hoert trotzdem beide."""
    e, vc, senke, _u = _empfaenger({1: 5})
    rueck = []
    e.bei_verbindung = rueck.append
    e.roh(_paket(b"\xfc\x01", ssrc=1, seq=10))
    e.client_verbunden(vc)
    e.roh(_paket(b"\xfc\x02", ssrc=1, seq=11))
    e.abarbeiten()
    assert [p[1] for p in senke.pakete] == [_strang(1, 1), _strang(2, 1)]
    fremd = _client()
    e.client_verbunden(fremd)
    assert e.verbindung_nr == 2 and rueck == [vc, fremd]

    def kaputt(_client_):
        raise RuntimeError("clips weg")
    e.bei_verbindung = kaputt
    e.client_verbunden(vc)                                     # wirft nicht
    assert e.verbindung_nr == 3


def test_empfang_dieselbe_ssrc_wieder_ist_ein_neuer_strang():
    """Geht jemand (op 13) und taucht dieselbe SSRC wieder auf - fuer ihn oder
    jemand anderen -, ist das ein neuer RTP-Strom mit eigenem seq/ts: nie
    derselbe Strang, sonst schnitte clips zwei Stroeme ineinander (es
    gruppiert nach strang und sortiert nach seq). Wechselt eine SSRC per op 5
    ohne op 13 den Besitzer, genauso. Ein Neuaufbau (op 4) beginnt frisch."""
    e, vc, senke, _u = _empfaenger({})
    with _angemeldet(77, e):
        _haken(vc, {"op": 5, "d": {"user_id": "5", "ssrc": 4242}})
        for seq in (40000, 40001):
            e.roh(_paket(b"\xfc\x01", seq=seq))
        e.abarbeiten()
        _haken(vc, {"op": 13, "d": {"user_id": "5"}})
        _haken(vc, {"op": 5, "d": {"user_id": "5", "ssrc": 4242}})   # derselbe, neu im Call
        e.roh(_paket(b"\xfc\x02", seq=100))
        e.abarbeiten()
        _haken(vc, {"op": 5, "d": {"user_id": "6", "ssrc": 4242}})   # ohne op 13: jemand anders
        for seq in (7, 8):
            e.roh(_paket(b"\xfc\x03", seq=seq))
        e.abarbeiten()
        _haken(vc, {"op": 4, "d": {}})                               # Neuaufbau, selber Kanal
        e.roh(_paket(b"\xfc\x04", seq=9))
        e.abarbeiten()
    assert [(p[0], p[1], p[2]) for p in senke.pakete] == [
        (5, _strang(1, 4242), 40000), (5, _strang(1, 4242), 40001),
        (5, _strang(1, 4242, 1), 100),
        (6, _strang(1, 4242, 2), 7), (6, _strang(1, 4242, 2), 8),
        (6, _strang(2, 4242), 9)]


def test_empfang_verschoben_kommt_nichts_aus_dem_neuen_kanal():
    """Flo wird in einen anderen Kanal verschoben: dort gab es keine Ansage.
    Mit op 4 im neuen Kanal haengt der Empfaenger sich selbst ab - kein Paket,
    kein gehaltenes und kein spaet per op 5 zugeordnetes erreicht die Senke,
    bis clips (nach der neuen Ansage) wieder anhaengt."""
    st = _Zustand()
    e, vc, senke, _u = _empfaenger({}, zustand=st)
    rueck = []
    e.bei_verbindung = rueck.append
    with _angemeldet(77, e):
        e.roh(_paket(b"\xfc\x01", ssrc=5))                     # noch aus dem alten Kanal
        e.abarbeiten()
        assert e.stand()["gehalten"] == 1
        vc.channel.id = 43
        _haken(vc, {"op": 4, "d": {}})
        assert e.client is None and st.lauscher == [] and rueck == [vc]
        e.roh(_paket(b"\xfc\x02", ssrc=6))                     # Nachzuegler des Lese-Threads
        assert e.abarbeiten() == 0
        _haken(vc, {"op": 5, "d": {"user_id": "60", "ssrc": 6}})
        _haken(vc, {"op": 5, "d": {"user_id": "50", "ssrc": 5}})
        assert senke.pakete == [] and e.stand()["gehalten"] == 0
        nr = e.verbindung_nr
        assert e.anhaengen(vc) and len(st.lauscher) == 1 and e.verbindung_nr == nr + 1
        e.roh(_paket(b"\xfc\x03", ssrc=6))
        e.abarbeiten()
        assert senke.opus() == [b"\xfc\x03"] and senke.pakete[0][0] == 60
        _haken(vc, {"op": 4, "d": {}})                          # Neuaufbau im NEUEN Kanal
        assert e.client is vc and e.verbindung_nr == nr + 2


# =============================================================================
# Threads und Loop
# =============================================================================
def test_empfang_roh_aus_dem_lese_thread_verliert_nichts():
    """roh() laeuft im SocketReader-Thread, alles andere im Loop. 2000 Pakete
    aus einem fremden Thread: alle kommen an, in Reihenfolge, verarbeitet im
    Loop-Thread - und die Weckrufe buendeln sich."""
    pakete = [_paket(bytes([0xfc, i % 256, i // 256]), seq=i, ts=960 * i)
              for i in range(1, 2001)]

    async def lauf():
        senke = _Senke()
        st = _Zustand()
        vc = _client({4242: 5}, zustand=st, loop=asyncio.get_running_loop())
        e = ve.Empfaenger(77, senke)
        assert e.anhaengen(vc) and len(st.lauscher) == 1
        lauscher = st.lauscher[0]
        weckrufe = []
        echt = e._wach

        def gezaehlt():
            weckrufe.append(threading.get_ident())
            echt()
        e._wach = gezaehlt

        def lesen():
            for p in pakete:
                lauscher(p)
        leser = threading.Thread(target=lesen, name="test-socket-reader")
        leser.start()
        frist = time.monotonic() + 20
        while len(senke.pakete) < len(pakete) and time.monotonic() < frist:
            await asyncio.sleep(0.005)
        leser.join(5)
        e.abhaengen()
        return senke, weckrufe

    senke, weckrufe = asyncio.run(lauf())
    assert [p[2] for p in senke.pakete] == list(range(1, 2001))
    assert {p[0] for p in senke.pakete} == {5}
    assert set(weckrufe) == {threading.get_ident()} and 0 < len(weckrufe) <= 2000


def test_empfang_weckruf_geht_nie_verloren():
    """Die Weckmarke faellt, BEVOR abgearbeitet wird. Kommt ein Paket an,
    nachdem der Stapel leer war, aber bevor der Weckruf zu Ende ist, weckt es
    neu - sonst laege es, bis zufaellig das naechste kommt (nach dem letzten
    Satz eines Calls: nie)."""
    async def lauf():
        senke = _Senke()
        e = ve.Empfaenger(77, senke)
        assert e.anhaengen(_client({4242: 5}, loop=asyncio.get_running_loop()))
        nachzuegler = [_paket(b"\xfc\x02", seq=2)]
        echt = e._takt_planen

        def planen():              # laeuft am Ende von abarbeiten(), nach dem Stapel
            echt()
            if nachzuegler:
                e.roh(nachzuegler.pop())
        e._takt_planen = planen
        e.roh(_paket(b"\xfc\x01", seq=1))
        frist = time.monotonic() + 3
        while len(senke.pakete) < 2 and time.monotonic() < frist:
            await asyncio.sleep(0.01)
        e.abhaengen()
        return senke.opus()

    assert asyncio.run(lauf()) == [b"\xfc\x01", b"\xfc\x02"]


def test_empfang_roh_wirft_nie_und_eingang_hat_deckel():
    """Ein geschlossener Loop stoert den Lese-Thread nicht. Kommt der Loop
    nicht hinterher, faellt das Aelteste raus (gezaehlt); je Weckruf hoechstens
    STAPEL_MAX Pakete."""
    tot = asyncio.new_event_loop()
    tot.close()
    e = ve.Empfaenger(77, _Senke())
    assert e.anhaengen(_client({4242: 5}, loop=tot))
    e.roh(_paket(b"\xfc"))
    e.roh(_paket(b"\xfc"))
    assert e.stand()["eingang"] == 2 and e.abarbeiten() == 2
    e2, _vc, senke, _u = _empfaenger({4242: 5})
    p = _paket(b"\xfc\x01")
    for _ in range(ve.EINGANG_MAX + 5):
        e2.roh(p)
    assert e2.zaehler()["ueberlauf"] == 5 and e2.stand()["eingang"] == ve.EINGANG_MAX
    assert e2.abarbeiten() == ve.STAPEL_MAX
    assert e2.stand()["eingang"] == ve.EINGANG_MAX - ve.STAPEL_MAX
    while e2.abarbeiten():
        pass
    assert len(senke.pakete) == ve.EINGANG_MAX


def test_empfang_takt_plant_sich_selbst_solange_etwas_wartet():
    """Mit echtem Loop: haelt der Empfaenger etwas, laeuft takt() von allein
    (TAKT_SEK) - ein spaetes op 5 findet die Pakete, und Abgelaufenes
    verschwindet ohne fremden Anstoss."""
    async def lauf():
        e = ve.Empfaenger(77, _Senke())
        vc = _client({}, loop=asyncio.get_running_loop())
        assert e.anhaengen(vc)
        e.roh(_paket(b"\xfc\x01"))
        await asyncio.sleep(0.05)
        gehalten = e.stand()["gehalten"]
        geplant = e._takt_griff is not None
        echt = e.takt
        takte = []

        def gezaehlt():
            takte.append(1)
            echt()
        e.takt = gezaehlt
        await asyncio.sleep(5 * ve.TAKT_SEK)        # Luft fuer einen langsamen Rechner
        e.abhaengen()
        return gehalten, geplant, len(takte), e._takt_griff

    gehalten, geplant, takte, griff = asyncio.run(lauf())
    assert gehalten == 1 and geplant and takte >= 2 and griff is None


# =============================================================================
# Die eigene Spur, Aufwecken, Auskunft
# =============================================================================
def test_empfang_eigene_spur_zweigt_vor_dave_ab_und_wirft_nie():
    """_get_voice_packet sieht jeden eigenen Opus-Rahmen; eigen_senke bekommt
    ihn als bytes mit monotonic-Zeit. Stille-Rahmen nicht. Eine werfende Senke
    stoert den Player nie. Gilt am Client wie an der Klasse."""
    unten = []

    def unterbau(_self, data):
        unten.append(bytes(data))
        return b"PAKET"
    vc = _client()
    spur = []
    with mock.patch.object(discord.VoiceClient, "_get_voice_packet", unterbau):
        assert vc._get_voice_packet(b"\xfc\x01") == b"PAKET"         # ohne Senke: nur durch
        vc.eigen_senke = lambda opus, t: spur.append((opus, t))
        vor = time.monotonic()
        assert vc._get_voice_packet(bytearray(b"\xfc\x02")) == b"PAKET"
        assert vc._get_voice_packet(ve.STILLE) == b"PAKET"
        assert [s[0] for s in spur] == [b"\xfc\x02"] and type(spur[0][0]) is bytes
        assert vor <= spur[0][1] <= time.monotonic()

        def bombe(_opus, _t):
            raise RuntimeError("Puffer kaputt")
        vc.eigen_senke = bombe
        assert vc._get_voice_packet(b"\xfc\x03") == b"PAKET"
        vc.eigen_senke = None
        andere = _client()
        with mock.patch.object(ve.EmpfangsClient, "eigen_senke",
                               lambda opus, t: spur.append((opus, t))):
            assert andere._get_voice_packet(b"\xfc\x04") == b"PAKET"
            assert vc._get_voice_packet(b"\xfc\x05") == b"PAKET"     # am Client weggenommen
    assert [s[0] for s in spur] == [b"\xfc\x02", b"\xfc\x04"]
    assert unten == [b"\xfc\x01", b"\xfc\x02", ve.STILLE, b"\xfc\x03", b"\xfc\x04", b"\xfc\x05"]
    assert ve.EmpfangsClient.eigen_senke is None


class _SprechWs:
    """ws.speak-Attrappe; 'spielt_danach' startet waehrenddessen Musik."""

    def __init__(self, vc=None, spielt_danach=False):
        self.gesagt = []
        self.vc = vc
        self.spielt_danach = spielt_danach

    async def speak(self, zustand):
        self.gesagt.append(zustand)
        if self.spielt_danach:
            self.vc._player = SimpleNamespace(is_playing=lambda: True)


def test_empfang_aufwecken_nur_wenn_nichts_laeuft():
    """Sprechen melden + fuenf Stille-Rahmen + nicht mehr sprechen - nur wenn
    verbunden, nichts spielt und Flo nicht vom Server stumm ist. Geht Musik
    waehrenddessen los, kein Rahmen und kein 'spreche nicht' mitten hinein."""
    vc = _client()
    gesendet = []
    vc.send_audio_packet = lambda data, encode=True: gesendet.append((data, encode))
    ws = _SprechWs()
    vc._connection.ws = ws
    assert asyncio.run(vc.aufwecken()) is True
    assert ws.gesagt == [discord.SpeakingState.voice, discord.SpeakingState.none]
    assert gesendet == [(ve.STILLE, False)] * ve.WECK_RAHMEN
    ws.gesagt.clear()
    gesendet.clear()
    vc._player = SimpleNamespace(is_playing=lambda: True)
    assert asyncio.run(vc.aufwecken()) is False and ws.gesagt == [] and gesendet == []
    vc._player = SimpleNamespace(is_playing=lambda: False, is_alive=lambda: True)  # pausiert
    assert asyncio.run(vc.aufwecken()) is False and ws.gesagt == [] and gesendet == []
    vc._player = SimpleNamespace(is_playing=lambda: False, is_alive=lambda: False)  # vorbei
    assert asyncio.run(vc.aufwecken()) is True and len(gesendet) == ve.WECK_RAHMEN
    gesendet.clear()
    vc._player = None
    vc._connection.verbunden = False
    assert asyncio.run(vc.aufwecken()) is False and gesendet == []
    vc._connection.verbunden = True
    vc.channel.guild.me.voice.mute = True
    assert asyncio.run(vc.aufwecken()) is False and gesendet == []
    vc.channel.guild.me.voice.mute = False
    mitten = _SprechWs(vc, spielt_danach=True)
    vc._connection.ws = mitten
    assert asyncio.run(vc.aufwecken()) is False
    assert mitten.gesagt == [discord.SpeakingState.voice] and gesendet == []
    vc._player = None
    vc._connection.ws = _SprechWs()

    def kaputt(_data, encode=True):
        raise OSError("Socket zu")
    vc.send_audio_packet = kaputt
    assert asyncio.run(vc.aufwecken()) is False


class _OpusQuelle(discord.AudioSource):
    """Eine Opus-Quelle, die nie versiegt (der Test haelt den Player an)."""

    def read(self):
        return b"\xfc\x01\x02"

    def is_opus(self):
        return True


async def _bis(bedingung, frist=5.0):
    """Im Loop warten, bis bedingung() stimmt - sonst AssertionError."""
    ende = time.monotonic() + frist
    while not bedingung():
        if time.monotonic() > ende:
            raise AssertionError("Bedingung nicht erreicht")
        await asyncio.sleep(0.005)


def test_empfang_aufwecken_nie_neben_einem_lebenden_player():
    """Mit dem ECHTEN discord.py-AudioPlayer: pausiert lebt sein Thread weiter
    (er hat fuenf Stille-Rahmen geschickt und wartet), und nach stop() schickt
    er noch fuenf, obwohl discord.py ihn schon vergessen hat (_player = None).
    sequence, timestamp und die Transport-Nonce zaehlt discord.py ohne Lock
    hoch - sendete aufwecken() daneben aus dem Loop-Thread, koennte eine Nonce
    doppelt vergeben werden. Also weckt es erst, wenn kein Player mehr lebt."""
    async def lauf():
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        vc = _client(loop=loop)
        vc.client = SimpleNamespace(loop=loop)
        vc._connection.ws = _SprechWs()
        gesendet = []                                # (aus dem Loop-Thread?, rahmen)
        festhalten, haengt, weiter = threading.Event(), threading.Event(), threading.Event()

        def senden(data, encode=True):
            im_loop = threading.get_ident() == loop_thread
            gesendet.append((im_loop, bytes(data)))
            if not im_loop and data == ve.STILLE and festhalten.is_set():
                haengt.set()
                weiter.wait(5)                       # der Player in seinen letzten Zuegen

        def aus_dem_loop():
            return [d for im_loop, d in gesendet if im_loop]

        def vom_player():
            return [d for im_loop, d in gesendet if not im_loop]
        vc.send_audio_packet = senden
        vc.play(_OpusQuelle())
        player = vc._player
        try:
            await _bis(lambda: len(vom_player()) >= 3)
            vc.pause()                               # 1) pausiert
            await _bis(lambda: vom_player().count(ve.STILLE) >= 5)
            pausiert = (await vc.aufwecken(), aus_dem_loop())
            festhalten.set()
            vc.resume()
            n = len(vom_player())
            await _bis(lambda: len(vom_player()) >= n + 2)
            vc.stop()                                # 2) gestoppt, schickt noch Stille
            await _bis(haengt.is_set)
            gestoppt = (vc._player, player.is_alive(), await vc.aufwecken(), aus_dem_loop())
            weiter.set()
            await _bis(lambda: not player.is_alive())
            danach = (await vc.aufwecken(), aus_dem_loop())   # 3) Thread zu Ende
        finally:
            weiter.set()
            player.stop()
            player.join(5)
        return pausiert, gestoppt, danach

    pausiert, gestoppt, danach = asyncio.run(lauf())
    assert pausiert == (False, [])
    assert gestoppt == (None, True, False, [])
    assert danach == (True, [ve.STILLE] * ve.WECK_RAHMEN)


def test_empfang_stand_und_zaehler_nur_zahlen():
    """'flo clip status' zeigt Zahlen - keine Namen, keine IDs."""
    dave = _FalscheDave(epoche=3)
    vc = _client({1: 987654321, 2: 987654321, 3: 123456789},
                 zustand=_Zustand(dave=dave, version=1))
    vc.im_call = {987654321, 123456789, 555555555}
    vc.verbindungen = 2
    assert vc.stand() == {"modus": ve.MODUS, "dave_version": 1, "dave_bereit": True,
                          "dave_epoche": 3, "zuordnungen": 3, "zugeordnet": 2,
                          "im_call": 3, "verbindungen": 2}
    leer = _client()
    leer._connection.mode = discord.utils.MISSING
    s = leer.stand()
    assert (s["modus"], s["dave_bereit"], s["dave_epoche"], s["dave_version"]) == (
        "", False, None, 0)
    e, _vc, _senke, _u = _empfaenger({4242: 987654321})
    e.roh(_paket(b"\xfc"))
    e.roh(_paket(b"\xfc", ssrc=1))
    e.abarbeiten()
    z = e.zaehler()
    assert set(z) == set(ve.ZAEHLER) | {"ueberlauf"}
    assert all(type(v) is int for v in z.values())
    st = e.stand()
    assert all(type(v) in (int, bool) for v in st.values())
    assert 987654321 not in z.values() and 987654321 not in st.values()
    assert (st["gehalten"], st["straenge"], st["angehaengt"]) == (1, 1, True)


def test_empfang_senke_wirft_der_rest_laeuft_weiter():
    """Wirft die Senke (Programmierfehler in clips), geht nur dieses Paket
    verloren - gezaehlt und EINMAL als Warnung im Log."""
    gut = []

    def senke(uid, strang, seq, ts, opus, t):
        if seq == 2:
            raise RuntimeError("kaputt")
        gut.append(seq)
    e = ve.Empfaenger(77, senke)
    assert e.anhaengen(_client({4242: 5}))
    with _Fang() as fang:
        for seq in (1, 2, 3, 2):
            e.roh(_paket(b"\xfc", seq=seq))
        e.abarbeiten()
    assert gut == [1, 3] and e.zaehler()["senke_fehler"] == 2
    assert e.zaehler()["gespeichert"] == 2
    assert [z[0] for z in fang.zeilen if "Senke" in z[1]] == [logging.WARNING]


# =============================================================================
# Register, Kompatibilitaet, Import
# =============================================================================
def test_empfang_register_anmelden_abmelden_fuer():
    e1, e2 = ve.Empfaenger(77, _Senke()), ve.Empfaenger(77, _Senke())
    try:
        ve.anmelden(77, e1)
        assert ve.fuer(77) is e1 and ve.fuer("77") is e1
        ve.anmelden(77, e2)
        assert ve.fuer(77) is e2
        assert ve.abmelden(77, e1) is None and ve.fuer(77) is e2   # spaetes Aufraeumen
        assert ve.abmelden(77) is e2 and ve.fuer(77) is None
        assert ve.fuer(None) is None and ve.fuer("x") is None and ve.abmelden("x") is None
    finally:
        ve._REGISTER.pop(77, None)
    assert 77 not in ve._REGISTER


def test_empfang_kompatibel_meldet_fehlende_innereien():
    """Ein discord.py-Update, das eine der inneren Stellen wegnimmt, schaltet
    den Empfang mit einem Grund ab - statt mit einem Traceback im Voice-Thread."""
    assert ve.kompatibel() == ""
    zustand = discord.voice_state.VoiceConnectionState

    def ohne_haken(self, voice_client):
        pass
    faelle = (
        (mock.patch.object(zustand, "__init__", ohne_haken), "hook"),
        (mock.patch.object(zustand, "add_socket_listener", None), "add_socket_listener"),
        (mock.patch.object(zustand, "remove_socket_listener", None), "remove_socket_listener"),
        (mock.patch.object(discord.VoiceClient, "_get_voice_packet", None), "_get_voice_packet"),
        (mock.patch.object(discord.VoiceClient, "create_connection_state", None),
         "create_connection_state"),
        (mock.patch.object(discord.VoiceClient, "supported_modes", ("xsalsa20_poly1305",)),
         ve.MODUS),
        (mock.patch.object(ve, "_nacl_secret", None), "Aead"),
        (mock.patch.object(ve, "davey", None), "davey"),
        (mock.patch.object(ve, "_voice_state", SimpleNamespace()), "VoiceConnectionState"),
    )
    for flicken, stichwort in faelle:
        with flicken:
            grund = ve.kompatibel()
        assert grund and stichwort in grund, (stichwort, grund)
    assert ve.kompatibel() == ""


def test_empfang_import_hat_keine_nebenwirkungen():
    """Beim Import: kein Thread, kein Eintrag im Register, keine Datei."""
    wurzel = os.path.dirname(os.path.abspath(__file__))

    def dateien():
        return {n for n in os.listdir(wurzel) if n != "__pycache__"}
    vorher = dateien()
    code = ("import threading\n"
            "n = threading.active_count()\n"
            "import voiceempfang\n"
            "assert threading.active_count() == n, threading.enumerate()\n"
            "assert voiceempfang._REGISTER == {}\n"
            "assert voiceempfang.EmpfangsClient.eigen_senke is None\n"
            "print('sauber')\n")
    lauf = subprocess.run([sys.executable, "-B", "-c", code], cwd=wurzel, capture_output=True,
                          text=True, timeout=120, env=dict(os.environ, FLO_TESTLAUF="1"))
    assert lauf.returncode == 0 and "sauber" in lauf.stdout, lauf.stderr[-2000:]
    assert dateien() == vorher


# =============================================================================
# Ende zu Ende
# =============================================================================
def test_empfang_echtes_opus_kommt_bytegleich_und_dekodierbar_an():
    """Echtes libopus: kodieren -> wie Flo selbst senden (EmpfangsClient mit
    Abzweigung, discord.py verschluesselt, seq und ts laufen dabei ueber) ->
    Empfaenger -> dieselben Opus-Bytes -> dekodierbar, und es ist Ton drin.
    Ohne DAVE, mit der Attrappe und mit ECHTEM davey (Anna sendet, Flo
    empfaengt, beide in einer Gruppe): die Abzweigung sieht den Rahmen VOR
    DAVE, der Empfaenger bekommt ihn nach DAVE wieder genau so."""
    if not _opus_da():
        return                                       # ohne libopus nichts zu pruefen
    kodierer = discord.opus.Encoder()
    rahmen = []
    for n in range(50):                              # 1 s Sinus, 440 Hz, stereo
        werte = []
        for i in range(960):
            s = int(8000 * math.sin(2 * math.pi * 440 * (n * 960 + i) / 48000))
            werte += (s, s)
        rahmen.append(kodierer.encode(struct.pack(f"<{len(werte)}h", *werte), 960))
    attrappe = _FalscheDave()
    attrappe.schluessel.add(9)
    faelle = [("ohne", None, None), ("attrappe", _FalscheDave(absender=9), attrappe)]
    echt = _echte_gruppe(9, 22)
    if echt is not None:
        faelle.append(("echt", echt[1][9], echt[1][22]))
    for art, sender_dave, dave in faelle:
        sender = _client()
        sender._connection = SimpleNamespace(
            secret_key=list(_SCHLUESSEL), dave_session=sender_dave,
            can_encrypt=sender_dave is not None, ssrc=4242, mode=ve.MODUS)
        sender._incr_nonce = 0
        sender.sequence = 65520
        sender.timestamp = 2 ** 32 - 960 * 20
        eigene = []
        sender.eigen_senke = lambda opus, t: eigene.append(opus)
        pakete = []
        for r in rahmen:
            sender.checked_add("sequence", 1, 65535)
            pakete.append(sender._get_voice_packet(r))
            sender.checked_add("timestamp", 960, 4294967295)
        assert eigene == rahmen, art                 # die eigene Spur: vor DAVE
        if dave is not None:
            assert pakete[0][-30:] != rahmen[0][-30:]
        e, _vc, senke, _u = _empfaenger({4242: 9}, zustand=_Zustand(
            dave=dave, version=0 if dave is None else 1))
        for p in pakete:
            e.roh(p)
        assert e.abarbeiten() == 50
        assert senke.opus() == rahmen, art
        assert [p[2] for p in senke.pakete] == list(range(65521, 65521 + 50))
        ts = [p[3] for p in senke.pakete]
        assert all(b - a == 960 for a, b in zip(ts, ts[1:]))
        z = e.zaehler()
        assert z["dave_klar" if dave is None else "dave_ok"] == 50, art
        assert z["gespeichert"] == 50
    dekoder = discord.opus.Decoder()
    pcm = [dekoder.decode(o) for o in senke.opus()]
    assert all(len(x) == 960 * 2 * 2 for x in pcm)
    mitte = struct.unpack(f"<{len(pcm[25]) // 2}h", pcm[25])
    assert max(abs(v) for v in mitte) > 1000


if __name__ == "__main__":
    run(globals())
