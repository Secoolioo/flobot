"""Voice-Clips, der Schnitt: Pakete platzieren, dekodieren, mischen (clipschnitt.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur clipschnitt      nur diese Tests

Die Platzier-Tests brauchen nichts ausser Python: die Opus-Pakete sind dort
Attrappen mit einem gueltigen TOC-Byte. Die echten Schnitte laufen mit der
echten libopus und dem echten ffmpeg und pruefen das Ergebnis mit ffprobe -
fehlt eins davon, kehren sie still zurueck (wie die ffmpeg-Tests der Musik).
Ein haengendes ffmpeg spielt der Python-Interpreter selbst (-c mit einem
Schlaf): kein Test fuehrt etwas aus einem Temp-Ordner aus - der darf auf einem
gehaerteten Server noexec sein.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module

import array
import contextlib
import json
import logging
import math
import stat
import subprocess
from unittest import mock

import discord

import clipschnitt
from clipschnitt import Auftrag, RenderFehler, Spurdaten

#: Ein Attrappen-Paket: TOC 0xFC = CELT, 20 ms, Stereo, ein Rahmen.
_RAHMEN = b"\xfc" + bytes(40)
_STILLE = b"\xf8\xff\xfe"
_BIS = 1000.0
_RATE = clipschnitt.RATE


# =============================================================================
# Helfer
# =============================================================================
def _schub(t0, n, *, strang=1, seq0=100, ts0=10_000, verzoegerung=0.03, jitter=None,
           rahmen=_RAHMEN):
    """n Pakete im 20-ms-Takt, gesendet ab t0 (monotonic), angekommen mit
    'verzoegerung' (+ jitter[k])."""
    pakete = []
    for k in range(n):
        d = verzoegerung + (jitter[k] if jitter else 0.0)
        pakete.append((t0 + k * 0.02 + d, strang, seq0 + k, ts0 + k * 960, rahmen))
    return pakete


def _positionen(schub):
    """[(absolute Position, ist_plc)] eines Schubs."""
    return [(schub.start + a, o is None) for a, o, _n in schub.rahmen]


@contextlib.contextmanager
def _faehig(stand=None, **werte):
    """Stellt den gemerkten ffmpeg-Befund fuer die Dauer des Blocks - und
    danach genau so zurueck, wie er war (auch None)."""
    alt = clipschnitt._FAEHIG
    if stand is None:
        stand = {"grund": "", "ffmpeg": shutil.which("ffmpeg") or "ffmpeg",
                 "nice": "", "normalize": True, "speechnorm": True, "limiter": True}
    stand = dict(stand, **werte)
    clipschnitt._FAEHIG = stand
    try:
        yield stand
    finally:
        clipschnitt._FAEHIG = alt


@contextlib.contextmanager
def _log(name="dcbot.clipschnitt"):
    """Faengt die Logzeilen eines Moduls (statt sie ins Testprotokoll zu
    kippen) - als Liste fertiger Saetze."""
    zeilen = []

    class Fang(logging.Handler):
        def emit(self, record):
            zeilen.append(record.getMessage())

    logger = logging.getLogger(name)
    fang = Fang(level=logging.DEBUG)
    alt = (logger.level, logger.propagate)
    logger.addHandler(fang)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield zeilen
    finally:
        logger.removeHandler(fang)
        logger.setLevel(alt[0])
        logger.propagate = alt[1]


@contextlib.contextmanager
def _ordner():
    """Ein frischer Temp-Ordner als tmp_basis - danach weg."""
    pfad = tempfile.mkdtemp(prefix="test-clipschnitt-")
    try:
        yield pfad
    finally:
        shutil.rmtree(pfad, ignore_errors=True)


def _opus_da():
    try:
        return bool(discord.opus.is_loaded() or discord.opus._load_default())
    except Exception:  # noqa: BLE001
        return False


def _echt_da():
    """libopus, ffmpeg mit MP3 und ffprobe - sonst wird der echte Test
    uebersprungen."""
    return (_opus_da() and clipschnitt.ffmpeg_pruefen() == ""
            and bool(shutil.which("ffprobe")))


def _sinus(freq, start, amp=9000, n=960):
    """20 ms Sinus, Stereo s16le."""
    werte = array.array("h")
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * freq * (start + i) / _RATE))
        werte.append(v)
        werte.append(v)
    return werte.tobytes()


def _echt(freq, von, bis_s, *, strang, seq0=100, ts0=5000, verzoegerung=0.03, dauer=8.0,
          stille_danach=0):
    """Echte Opus-Pakete: ein Ton von 'von' bis 'bis_s' (Sekunden im Clip)."""
    enc = discord.opus.Encoder()
    anfang = _BIS - dauer
    n = int(round((bis_s - von) / 0.02))
    pakete = []
    for k in range(n + stille_danach):
        opus = enc.encode(_sinus(freq, k * 960), 960) if k < n else _STILLE
        pakete.append((anfang + von + k * 0.02 + verzoegerung, strang, seq0 + k,
                       ts0 + k * 960, opus))
    return pakete


def _flo_echt(von, bis_s, dauer=8.0, freq=220):
    enc = discord.opus.Encoder()
    anfang = _BIS - dauer
    n = int(round((bis_s - von) / 0.02))
    return [(anfang + von + k * 0.02, enc.encode(_sinus(freq, k * 960, amp=7000), 960))
            for k in range(n)]


def _ffprobe(mp3, ordner):
    pfad = os.path.join(ordner, "probe.mp3")
    with open(pfad, "wb") as datei:
        datei.write(mp3)
    try:
        fertig = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:format_tags=title,artist:stream=sample_rate,channels",
             "-of", "json", pfad], capture_output=True, text=True, timeout=60)
    finally:
        os.remove(pfad)
    return json.loads(fertig.stdout or "{}")


def _pegel(mp3, ordner, rate=8000):
    """Die MP3 als Mono-Samples (8 kHz) - fuer Pegelmessungen."""
    pfad = os.path.join(ordner, "pegel.mp3")
    with open(pfad, "wb") as datei:
        datei.write(mp3)
    try:
        fertig = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", pfad, "-f", "s16le",
             "-ac", "1", "-ar", str(rate), "-"], capture_output=True, timeout=60)
    finally:
        os.remove(pfad)
    werte = array.array("h")
    werte.frombytes(fertig.stdout[:len(fertig.stdout) // 2 * 2])
    return werte


def _rms(werte, von, bis_s, rate=8000):
    stueck = werte[int(von * rate):int(bis_s * rate)]
    return math.sqrt(sum(v * v for v in stueck) / len(stueck)) if len(stueck) else 0.0


def _mp3_rahmen_ok(mp3):
    """Hinter dem ID3-Kopf muss ein MP3-Rahmen anfangen (Sync 0xFFE)."""
    pos = 0
    if mp3[:3] == b"ID3":
        groesse = 0
        for b in mp3[6:10]:
            groesse = (groesse << 7) | (b & 0x7F)
        pos = 10 + groesse
    return len(mp3) > pos + 4 and mp3[pos] == 0xFF and (mp3[pos + 1] & 0xE0) == 0xE0


# =============================================================================
# TOC und Platzieren (ohne libopus, ohne ffmpeg)
# =============================================================================
def test_clipschnitt_toc_rechnet_wie_libopus():
    """Die Rahmenlaenge kommt aus dem TOC-Byte - ohne libopus, aber mit
    demselben Ergebnis wie libopus selbst (wenn sie da ist: alle 256 TOCs in
    allen vier Rahmen-Formen gegengerechnet)."""
    r = clipschnitt.rahmen_samples
    assert r(_STILLE) == 960                     # Discords Stille: CELT 20 ms
    assert r(bytes([3 << 3])) == 2880            # SILK 60 ms
    assert r(bytes([16 << 3])) == 120            # CELT 2,5 ms
    assert r(bytes([(13 << 3) | 1])) == 1920     # Hybrid 20 ms, zwei Rahmen
    assert r(bytes([(31 << 3) | 3, 3])) == 2880  # CELT 20 ms, drei Rahmen
    assert r(bytes([(31 << 3) | 3, 7])) == 0     # 140 ms - mehr erlaubt Opus nicht
    assert r(bytes([(31 << 3) | 3])) == 0        # Code 3 ohne Zaehl-Byte
    assert r(bytes([(31 << 3) | 3, 0])) == 0     # null Rahmen
    assert r(b"") == 0 and r(None) == 0
    if not _opus_da():
        return
    dec = discord.opus.Decoder
    for toc in range(256):
        for zweites in (b"", b"\x01", b"\x02", b"\x05"):
            paket = bytes([toc]) + zweites + bytes(20)
            try:
                erwartet = dec.packet_get_nb_frames(paket) * dec.packet_get_samples_per_frame(paket)
            except discord.opus.OpusError:
                erwartet = 0
            if erwartet > 5760:
                erwartet = 0
            assert r(paket) == erwartet, (hex(toc), zweites, r(paket), erwartet)


def test_clipschnitt_platzieren_zieht_jitter_glatt():
    """±40 ms Netz-Jitter: nach Ankunft sortiert laegen die Rahmen kreuz und
    quer (Luecken und Ueberlappungen). Platziert liegen sie lueckenlos im
    20-ms-Raster, und der Schub sitzt hoechstens 20 ms neben der Sendezeit."""
    zufall = random.Random(7)
    jitter = [zufall.uniform(-0.04, 0.04) for _ in range(100)]
    jitter[13] = -0.04                           # das schnellste Paket
    pakete = _schub(991.0, 100, verzoegerung=0.06, jitter=jitter)
    zufall.shuffle(pakete)                       # kommen durcheinander an
    schuebe = clipschnitt.platzieren(pakete, _BIS, 10.0)
    assert len(schuebe) == 1, [s.start for s in schuebe]
    schub = schuebe[0]
    wahr = int((991.0 - (_BIS - 10.0)) * _RATE)
    assert abs(schub.start - wahr) <= 960, (schub.start, wahr)
    assert [a for a, _o, _n in schub.rahmen] == [k * 960 for k in range(100)]
    # Gegenprobe: nach Ankunft gelegt, waere das Raster zerrissen.
    nach_ankunft = sorted(int((t - 990.0) * _RATE) for t, *_r in pakete)
    abstaende = {b - a for a, b in zip(nach_ankunft, nach_ankunft[1:])}
    assert max(abstaende) > 1500 and min(abstaende) < 500


def test_clipschnitt_platzieren_pausen_bleiben_still():
    """Eine Sprechpause trennt zwei Schuebe - ob der Sender seinen Zeitstempel
    dabei weiterzaehlt oder nicht. Der zweite liegt dort, wo er ankam, und
    dazwischen steht nichts."""
    for weiterzaehlen in (True, False):
        erster = _schub(990.5, 50, seq0=100, ts0=10_000)
        ts_zweiter = 10_000 + 50 * 960 + (48_000 if weiterzaehlen else 0)
        zweiter = _schub(992.5, 50, seq0=150, ts0=ts_zweiter)
        schuebe = clipschnitt.platzieren(erster + zweiter, _BIS, 10.0)
        assert len(schuebe) == 2, weiterzaehlen
        a, b = schuebe
        assert a.start == int(round(0.53 * _RATE)) and a.ende == a.start + 50 * 960
        assert b.start == int(round(2.53 * _RATE)), (weiterzaehlen, b.start)
        assert b.start - a.ende == int(round(1.0 * _RATE))


def test_clipschnitt_platzieren_plc_nur_fuer_kurze_luecken():
    """1-3 verlorene Rahmen fuellt der Decoder (Platzhalter None an der
    richtigen Stelle), 10 verlorene bleiben still - und alles danach liegt
    trotzdem genau nach Zeitstempel."""
    for fehlen in (1, 2, 3, 10):
        pakete = [p for p in _schub(991.0, 50) if not 120 <= p[2] < 120 + fehlen]
        schuebe = clipschnitt.platzieren(pakete, _BIS, 10.0)
        assert len(schuebe) == 1, fehlen
        plc = [a for a, o, _n in schuebe[0].rahmen if o is None]
        if fehlen <= clipschnitt.PLC_MAX:
            assert plc == [(20 + k) * 960 for k in range(fehlen)], (fehlen, plc)
        else:
            assert plc == [], fehlen
        echte = {a for a, o, _n in schuebe[0].rahmen if o is not None}
        assert (20 + fehlen) * 960 in echte and 49 * 960 in echte
    # Fehlt eine Nummer, aber der Zeitstempel laeuft lueckenlos weiter (ein
    # Sender, der leere Pakete zaehlt), gibt es keinen Platz fuer Erfundenes:
    # kein Platzhalter, der sich mit dem naechsten echten Rahmen ueberlagert.
    pakete = _schub(991.0, 20) + [(991.0 + 20 * 0.02 + 0.03, 1, 121, 10_000 + 20 * 960, _RAHMEN)]
    schuebe = clipschnitt.platzieren(pakete, _BIS, 10.0)
    assert [o for _a, o, _n in schuebe[0].rahmen].count(None) == 0
    assert [a for a, _o, _n in schuebe[0].rahmen] == [k * 960 for k in range(21)]


def test_clipschnitt_platzieren_ueberlappung_rueckt_nach():
    """Zwei Straenge derselben Person (Neuverbindung, neue SSRC), deren Schaetzung
    sich ueberlappt: der spaetere rueckt ans Ende des frueheren, statt ihn zu
    ueberschreiben."""
    alt = _schub(991.0, 50, strang=1)
    neu = _schub(991.9, 50, strang=2, seq0=5, ts0=777_000)
    schuebe = clipschnitt.platzieren(alt + neu, _BIS, 10.0)
    assert [s.strang for s in schuebe] == [1, 2]
    assert schuebe[1].start == schuebe[0].ende
    assert schuebe[0].start == int(round(1.03 * _RATE))


def test_clipschnitt_platzieren_beschneidet_aufs_fenster():
    """Was vor dem Fenster liegt, faellt weg; ein Rahmen, der halb hineinragt,
    bleibt (das Schreiben schneidet ihn zu); ein Schub ganz ausserhalb kommt
    gar nicht erst vor."""
    gesamt = 10 * _RATE
    vorne = _schub(989.5, 50, strang=1)
    hinten = _schub(999.5, 50, strang=2, seq0=500)
    draussen = _schub(985.0, 50, strang=3, seq0=900)
    schuebe = clipschnitt.platzieren(vorne + hinten + draussen, _BIS, 10.0)
    assert sorted(s.strang for s in schuebe) == [1, 2]
    for schub in schuebe:
        for pos, _plc in _positionen(schub):
            assert -960 < pos < gesamt, (schub.strang, pos)
    v = next(s for s in schuebe if s.strang == 1)
    erster = _positionen(v)[0][0]
    assert -960 < erster < 0                     # ragt halb hinein
    h = next(s for s in schuebe if s.strang == 2)
    assert h.ende > gesamt and len(h.rahmen) < 50


def test_clipschnitt_platzieren_mehrere_straenge_je_mit_eigenem_anker():
    """Zwei Straenge, die sich nicht beruehren: jeder liegt an seiner Ankunft,
    jeder hat seine eigene Dekodier-Reihenfolge (nr)."""
    eins = _schub(991.0, 25, strang=11, ts0=1_000)
    zwei = _schub(995.0, 25, strang=22, seq0=7, ts0=9_999_000)
    schuebe = clipschnitt.platzieren(zwei + eins, _BIS, 10.0)
    assert [(s.strang, s.nr) for s in schuebe] == [(11, 0), (22, 0)]
    assert schuebe[0].start == int(round(1.03 * _RATE))
    assert schuebe[1].start == int(round(5.03 * _RATE))


def test_clipschnitt_platzieren_flos_pause_teilt_die_spur():
    """Flos eigene Spur hat keine echten Zeitstempel - flo_pakete zaehlt sie
    selbst. Pausiert die Musik, laeuft die Ankunft davon: neuer Schub an der
    richtigen Stelle, nichts dazwischen."""
    eigen = ([(990.0 + k * 0.02, _RAHMEN) for k in range(50)]
             + [(993.0 + k * 0.02, _RAHMEN) for k in range(50)])
    pakete = clipschnitt.flo_pakete(eigen)
    assert [p[2] for p in pakete[:3]] == [0, 1, 2]
    assert pakete[50][3] == 50 * 960
    schuebe = clipschnitt.platzieren(pakete, _BIS, 10.0)
    assert [s.start for s in schuebe] == [0, 3 * _RATE]
    assert schuebe[0].ende == 50 * 960
    # Muell in der Spur stoert nicht - auch keiner, an dem das Zaehlen der
    # Rahmen frueher den ganzen Schnitt sprengte (kein bytes, keine Zeit).
    assert clipschnitt.flo_pakete([None, (1.0,), (2.0, b""), (2.5, "x"), (2.6, 7),
                                   ("bald", _RAHMEN), (3.0, _RAHMEN)]) == [
        (3.0, 0, 6, 0, _RAHMEN)]
    # Die Reihenfolge (opus, t), wie voiceempfang seine Senke ruft, geht auch.
    assert clipschnitt.flo_pakete([(_RAHMEN, 5.0), (bytearray(_RAHMEN), 5.02)]) == [
        (5.0, 0, 0, 0, _RAHMEN), (5.02, 0, 1, 960, _RAHMEN)]
    gedreht = clipschnitt.flo_pakete([(opus, t) for t, opus in eigen])
    assert [s.start for s in clipschnitt.platzieren(gedreht, _BIS, 10.0)] == [0, 3 * _RATE]


def test_clipschnitt_platzieren_doppelt_und_muell():
    """Doppelt angekommene Pakete zaehlen einmal, kaputte Eintraege werden
    uebergangen, die Reihenfolge stellt seq her."""
    pakete = _schub(991.0, 20)
    pakete = pakete[::-1] + [pakete[5], None, (1, 2), (991.0, 1, "x", 5, _RAHMEN),
                             (991.0, 1, 999, 5, None)]
    schuebe = clipschnitt.platzieren(pakete, _BIS, 10.0)
    assert len(schuebe) == 1
    assert [a for a, _o, _n in schuebe[0].rahmen] == [k * 960 for k in range(20)]
    assert clipschnitt.platzieren([], _BIS, 10.0) == []
    assert clipschnitt.platzieren(None, _BIS, 10.0) == []


def test_clipschnitt_stimme_zaehlt_ohne_stille_und_plc():
    """Wie viel Stimme ein Clip hat (Ergebnis.stimm_sek und die Auswahl fuer
    Whisper): Stille-Rahmen am Satzende und PLC zaehlen nicht, und die
    Bereiche fuer Whisper enden beim letzten echten Wort."""
    pakete = _schub(991.0, 50) + _schub(992.0, 5, seq0=150, ts0=10_000 + 50 * 960,
                                        rahmen=_STILLE)
    pakete = [p for p in pakete if p[2] != 110]          # ein Rahmen verloren -> PLC
    schuebe = clipschnitt.platzieren(pakete, _BIS, 10.0)
    gesamt = 10 * _RATE
    assert clipschnitt.stimm_samples(schuebe, gesamt) == 49 * 960
    bereiche = clipschnitt.stimm_bereiche(schuebe, gesamt)
    assert bereiche == [(schuebe[0].start, schuebe[0].start + 50 * 960)]
    # Zwei Stuecke mit weniger als WHISPER_PAUSE dazwischen werden eins.
    nah = _schub(991.0, 10) + _schub(991.5, 10, seq0=300, ts0=99_000)
    bereiche = clipschnitt.stimm_bereiche(clipschnitt.platzieren(nah, _BIS, 10.0), gesamt)
    assert len(bereiche) == 1
    weit = _schub(991.0, 10) + _schub(992.0, 10, seq0=300, ts0=99_000)
    assert len(clipschnitt.stimm_bereiche(clipschnitt.platzieren(weit, _BIS, 10.0), gesamt)) == 2


# =============================================================================
# Bitrate, Titel, ffmpeg-Aufruf
# =============================================================================
def test_clipschnitt_bitrate_leiter():
    """Bei 9,5 MiB bekommt jede Laenge bis 300 s 192 kbps; kleine Budgets gehen
    die Leiter hinunter, und was nicht einmal mit 32 kbps passt, ist 0."""
    voll = int(9.5 * 1048576)
    for dauer in (30, 60, 300):
        assert clipschnitt.bitrate_fuer(dauer, voll) == 192, dauer
    assert clipschnitt.bitrate_fuer(300, 4 * 1048576) == 96
    assert clipschnitt.bitrate_fuer(60, 512 * 1024) == 64
    assert clipschnitt.bitrate_fuer(30, 512 * 1024) == 128
    assert clipschnitt.bitrate_fuer(300, 1048576) == 0
    assert clipschnitt.bitrate_fuer(300, 1_200_000) == 32     # knapp, ohne Luft
    assert clipschnitt.bitrate_fuer(0, 10) == 192
    # Nach einem zu grossen Versuch: die Stufe, die nach der Messung passt.
    assert clipschnitt.naechste_stufe(192, 200_000, 100_000) == 80
    assert clipschnitt.naechste_stufe(192, 101_000, 100_000) == 160
    assert clipschnitt.naechste_stufe(32, 200_000, 100_000) == 0
    for kbps in clipschnitt.LEITER:
        assert kbps in (192, 160, 128, 112, 96, 80, 64, 48, 40, 32)


def test_clipschnitt_titel_wird_gesaeubert():
    """Der ID3-Titel: einzeilig, ohne Steuerzeichen, hoechstens 80 Zeichen."""
    s = clipschnitt.titel_saeubern
    assert s("Clip\naus\t#voice\x00\x1b[31m 🎬") == "Clip aus #voice [31m 🎬"
    assert s("  ") == "Clip" and s(None) == "Clip"
    lang = s("Wort " * 40)
    assert len(lang) <= clipschnitt.TITEL_MAX and lang.endswith("…")
    assert s("Ünïcödé – ok") == "Ünïcödé – ok"
    assert "\u202e" not in s("links\u202erechts")     # Richtungs-Trick


def test_clipschnitt_mix_befehl_mit_und_ohne_filter():
    """Mit vollem ffmpeg: speechnorm je Stimme, Flo auf 0,6, amix ohne eigenes
    Teilen (normalize=0), Begrenzer, libmp3lame. Mit altem ffmpeg: ohne die
    Kuer, dafuer der Pegel nach amix zurueckgeholt. nice nur, wenn es da ist."""
    eingaben = [("/t/v0.raw", 1, False), ("/t/v1.raw", 1, False), ("/t/flo.raw", 2, True)]
    voll = {"ffmpeg": "/usr/bin/ffmpeg", "nice": "/usr/bin/nice", "normalize": True,
            "speechnorm": True, "limiter": True}
    befehl = clipschnitt.mix_befehl(eingaben, "/t/clip.mp3", 8.0, 192, "Clip 🎬", voll)
    assert befehl[:3] == ["/usr/bin/nice", "-n", "10"] and befehl[3] == "/usr/bin/ffmpeg"
    graph = befehl[befehl.index("-filter_complex") + 1]
    assert graph.count("speechnorm=") == 2 and "volume=0.6[f2]" in graph
    assert "amix=inputs=3:duration=longest:dropout_transition=0:normalize=0" in graph
    assert "alimiter=limit=0.89:level=false[m]" in graph
    assert befehl[befehl.index("-c:a") + 1] == "libmp3lame"
    assert befehl[befehl.index("-b:a") + 1] == "192k"
    assert befehl[befehl.index("-t") + 1] == "8.000"
    assert "title=Clip 🎬" in befehl and "artist=Flo" in befehl
    assert befehl.count("-i") == 3 and befehl[befehl.index("-ac") + 1] == "1"
    alt = dict(voll, nice="", normalize=False, speechnorm=False, limiter=False)
    befehl = clipschnitt.mix_befehl(eingaben, "/t/clip.mp3", 8.0, 96, "x", alt)
    graph = befehl[befehl.index("-filter_complex") + 1]
    assert befehl[0] == "/usr/bin/ffmpeg"
    assert "speechnorm" not in graph and "alimiter" not in graph and "normalize" not in graph
    assert "amix=inputs=3:duration=longest:dropout_transition=0,volume=3[m]" in graph
    einzeln = clipschnitt.mix_befehl(eingaben[:1], "/t/c.mp3", 8.0, 96, "x", alt)
    graph = einzeln[einzeln.index("-filter_complex") + 1]
    assert graph.endswith("amix=inputs=1:duration=longest:dropout_transition=0[m]"), graph


def test_clipschnitt_ffmpeg_pruefen_merkt_sich_den_befund():
    """Einmal nachgesehen, danach gemerkt. Pflicht ist nur libmp3lame; ohne
    normalize, speechnorm oder alimiter geht es trotzdem (dann ohne)."""
    aufrufe = []

    def antwort(befehl, **kw):
        aufrufe.append(befehl)
        teil = befehl[-1]
        aus = {"-encoders": b" A....D libmp3lame  MP3",
               "filter=amix": b"Filter amix\n   normalize  <boolean>",
               "filter=speechnorm": b"Filter speechnorm\n  Speech Normalizer.",
               "filter=alimiter": b"Filter alimiter"}.get(teil, b"")
        return SimpleNamespace(stdout=aus, stderr=b"", returncode=0)

    with _faehig(), mock.patch.object(clipschnitt.shutil, "which",
                                      lambda name: f"/opt/{name}"), \
            mock.patch.object(clipschnitt.subprocess, "run", antwort):
        assert clipschnitt.ffmpeg_pruefen(neu=True) == ""
        stand = clipschnitt._FAEHIG
        assert stand["normalize"] and stand["speechnorm"] and stand["limiter"]
        assert stand["ffmpeg"] == "/opt/ffmpeg" and stand["nice"] == "/opt/nice"
        assert len(aufrufe) == 4
        assert clipschnitt.ffmpeg_pruefen() == "" and len(aufrufe) == 4   # gemerkt

    def ohne_mp3(befehl, **kw):
        return SimpleNamespace(stdout=b" A....D aac", stderr=b"", returncode=0)

    def altes(befehl, **kw):
        aus = b" A....D libmp3lame" if befehl[-1] == "-encoders" else b"Unknown filter"
        return SimpleNamespace(stdout=b"", stderr=aus, returncode=0)

    def kaputt(befehl, **kw):
        raise OSError("exec format error")

    with _faehig(), mock.patch.object(clipschnitt.shutil, "which", lambda n: f"/opt/{n}"):
        with mock.patch.object(clipschnitt.subprocess, "run", ohne_mp3):
            assert "libmp3lame" in clipschnitt.ffmpeg_pruefen(neu=True)
        with mock.patch.object(clipschnitt.subprocess, "run", altes):
            assert clipschnitt.ffmpeg_pruefen(neu=True) == ""
            stand = clipschnitt._FAEHIG
            assert not (stand["normalize"] or stand["speechnorm"] or stand["limiter"])
        with mock.patch.object(clipschnitt.subprocess, "run", kaputt):
            assert clipschnitt.ffmpeg_pruefen(neu=True) == "ffmpeg antwortet nicht"
    with _faehig(), mock.patch.object(clipschnitt.shutil, "which", lambda n: None):
        assert clipschnitt.ffmpeg_pruefen(neu=True) == "ffmpeg fehlt"


# =============================================================================
# Echte Schnitte (libopus + ffmpeg)
# =============================================================================
def test_clipschnitt_echter_schnitt_zwei_stimmen_und_flo():
    """Der Ernstfall: 8 s, zwei Stimmen, Flos Musik mit Pause. Heraus kommt eine
    echte MP3 (Rahmen-Sync, ffprobe-Laenge 8 ±0,15 s, Titel und Kuenstler),
    unter dem Budget - und jede Spur liegt dort, wo sie hingehoert: wo nur
    Stille war, ist es still. Der Temp-Ordner ist danach weg."""
    if not _echt_da():
        return
    a = Spurdaten(1, "Anna", False, _echt(330, 0.5, 2.5, strang=7)
                  + _echt(330, 4.0, 6.0, strang=7, seq0=400, ts0=900_000))
    b = Spurdaten(2, "Bob", False, _echt(550, 2.0, 7.0, strang=9))
    flo = _flo_echt(0.0, 1.0) + _flo_echt(7.5, 8.0)
    budget = int(9.5 * 1048576)
    with _ordner() as basis:
        erg = clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=[a, b], flo=flo,
                                          budget=budget, titel="Clip aus #Gaming",
                                          tmp_basis=basis))
        assert os.listdir(basis) == [], os.listdir(basis)
        assert erg.kbps == 192 and erg.dauer == 8.0
        assert 0 < len(erg.mp3) <= budget and _mp3_rahmen_ok(erg.mp3)
        assert erg.whisper == {} and "bitrate_gesenkt" not in erg.hinweise
        assert abs(erg.stimm_sek[1] - 4.0) < 0.1 and abs(erg.stimm_sek[2] - 5.0) < 0.1
        probe = _ffprobe(erg.mp3, basis)
        laenge = float(probe["format"]["duration"])
        assert abs(laenge - 8.0) <= 0.15, laenge
        assert probe["format"]["tags"]["title"] == "Clip aus #Gaming"
        assert probe["format"]["tags"]["artist"] == "Flo"
        assert probe["streams"][0]["channels"] == 2
        assert probe["streams"][0]["sample_rate"] == "48000"
        werte = _pegel(erg.mp3, basis)
        laut = [_rms(werte, *f) for f in ((0.6, 0.9), (1.2, 1.9), (2.6, 3.9), (6.2, 6.9),
                                          (7.6, 7.9))]
        leise = _rms(werte, 7.15, 7.4)
        assert min(laut) > 500, laut
        assert leise < 50, (leise, laut)
        assert os.listdir(basis) == []


def test_clipschnitt_einzelner_sprecher_ohne_flo():
    """Nur eine Person, ohne Flos Spur (Einzel-Clip): amix mit einem Eingang."""
    if not _echt_da():
        return
    a = Spurdaten(5, "Solo", False, _echt(440, 1.0, 3.0, strang=1, dauer=4.0))
    with _ordner() as basis:
        erg = clipschnitt.rendern(Auftrag(bis=_BIS, dauer=4.0, stimmen=[a], flo=[],
                                          budget=int(9.5 * 1048576), titel="Solo",
                                          tmp_basis=basis))
        laenge = float(_ffprobe(erg.mp3, basis)["format"]["duration"])
        assert abs(laenge - 4.0) <= 0.15
        werte = _pegel(erg.mp3, basis)
        assert _rms(werte, 1.2, 2.8) > 500 and _rms(werte, 0.1, 0.8) < 50
        assert os.listdir(basis) == []


def _db(wert, bezug):
    return 20 * math.log10(max(wert, 1e-9) / bezug)


def test_clipschnitt_echter_schnitt_mit_altem_ffmpeg():
    """Der Rueckfall fuer ein ffmpeg ohne amix-normalize, speechnorm und
    alimiter (das Docker-Image hat 5.1) - nicht nur als Befehl geprueft,
    sondern echt gemischt: die Laenge stimmt, die Hinweise nennen, was fehlte,
    und eine Stimme allein ist so laut wie ihre Quelle. Das alte amix teilt
    jeden Eingang durch ihre Zahl, volume=N holt das zurueck - ohne laege sie
    hier 9 dB tiefer. Flos Musik liegt um FLO_PEGEL (0,6 = -4,4 dB) darunter."""
    if not _echt_da():
        return
    stimmen = [Spurdaten(1, "A", False, _echt(330, 0.5, 4.0, strang=7)),
               Spurdaten(2, "B", False, _echt(550, 5.0, 7.0, strang=9))]
    flo = _flo_echt(4.2, 4.9)
    with _ordner() as basis, _faehig(normalize=False, speechnorm=False, limiter=False):
        erg = clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=stimmen, flo=flo,
                                          budget=int(9.5 * 1048576), titel="alt",
                                          tmp_basis=basis))
        assert erg.hinweise == ["speechnorm_fehlt", "normalize_fehlt", "limiter_fehlt"]
        laenge = float(_ffprobe(erg.mp3, basis)["format"]["duration"])
        assert abs(laenge - 8.0) <= 0.15, laenge
        werte = _pegel(erg.mp3, basis)
        # Sinus mit Amplitude 9000 (Stimmen) bzw. 7000 (Flo): Effektivwert A/√2.
        # Mono wird beim Hochmischen auf Stereo um 3 dB leiser (Panorama-Mitte).
        stimme = _db(_rms(werte, 0.7, 3.8), 9000 / math.sqrt(2))
        assert -6 < stimme < 1, stimme
        assert -6 < _db(_rms(werte, 5.2, 6.8), 9000 / math.sqrt(2)) < 1
        musik = _db(_rms(werte, 4.3, 4.8), 7000 / math.sqrt(2))
        assert -7 < musik < -2, musik
        assert _rms(werte, 7.3, 7.9) < 50
        assert os.listdir(basis) == []


def test_clipschnitt_whisper_karte_passt_zu_den_schueben():
    """whisper=True: je Sprecher eine 16-kHz-Mono-MP3 nur aus seinen Stellen.
    Die Karte fuehrt jedes Stueck auf seine Clip-Zeit zurueck; Stille-Rahmen am
    Satzende gehoeren nicht dazu. Wer unter 1,5 s redet und Bots bekommen
    keinen Ton fuer Whisper."""
    if not _echt_da():
        return
    a = Spurdaten(1, "Anna", False, _echt(330, 0.5, 2.5, strang=7, stille_danach=5)
                  + _echt(330, 4.0, 6.0, strang=7, seq0=400, ts0=900_000))
    kurz = Spurdaten(2, "Kurz", False, _echt(550, 6.5, 7.5, strang=9))
    bot = Spurdaten(3, "Musikbot", True, _echt(660, 1.0, 5.0, strang=11))
    with _ordner() as basis:
        erg = clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=[a, kurz, bot], flo=[],
                                          budget=int(9.5 * 1048576), titel="w",
                                          whisper=True, tmp_basis=basis))
        assert os.listdir(basis) == []
        assert set(erg.whisper) == {1}, set(erg.whisper)
        wa = erg.whisper[1]
        assert len(wa.karte) == 2, wa.karte
        (a0, e0, c0), (a1, e1, c1) = wa.karte
        assert a0 == 0.0 and abs(e0 - 2.0) < 0.03 and abs(c0 - 0.53) < 0.01
        assert abs(a1 - (e0 + clipschnitt.WHISPER_PAUSE)) < 0.002
        assert abs((e1 - a1) - 2.0) < 0.03 and abs(c1 - 4.03) < 0.01
        assert wa.dauer == e1
        probe = _ffprobe(wa.mp3, basis)
        assert probe["streams"][0]["sample_rate"] == "16000"
        assert probe["streams"][0]["channels"] == 1
        assert abs(float(probe["format"]["duration"]) - wa.dauer) < 0.15
        werte = _pegel(wa.mp3, basis)
        assert _rms(werte, 0.2, 1.8) > 300 and _rms(werte, e0 + 0.08, a1 - 0.08) < 50


def test_clipschnitt_whisper_fehler_kostet_nur_das_transkript():
    """Scheitert der Ton fuer Whisper, kommt der Clip trotzdem - ohne
    Whisper-Material (nie fuer nur einen Teil der Sprecher) und mit Hinweis.
    Wird der Auftrag dagegen abgebrochen, waehrend der Whisper-Ton entsteht,
    bricht der ganze Schnitt ab."""
    if not _echt_da():
        return
    stimmen = [Spurdaten(1, "Anna", False, _echt(330, 0.5, 3.5, strang=7)),
               Spurdaten(2, "Bob", False, _echt(550, 4.0, 7.0, strang=9))]
    echt = clipschnitt._ffmpeg_laufen
    laeufe = []

    def zweiter_whisper_kaputt(befehl, frist, was):
        laeufe.append(was)
        if was == "Whisper" and laeufe.count("Whisper") == 2:
            raise RenderFehler("ffmpeg", "Code 1")
        return echt(befehl, frist, was)

    with _ordner() as basis, mock.patch.object(clipschnitt, "_ffmpeg_laufen",
                                               zweiter_whisper_kaputt), _log() as zeilen:
        erg = clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=stimmen, flo=[],
                                          budget=int(9.5 * 1048576), titel="w", whisper=True,
                                          tmp_basis=basis))
        assert erg.mp3 and erg.whisper == {} and "whisper_fehlt" in erg.hinweise
        assert laeufe == ["Mischen", "Whisper", "Whisper"]
        assert os.listdir(basis) == []
    assert any("Transkript fehlt" in z for z in zeilen), zeilen
    gemischt = {"ja": False}

    def merken(befehl, frist, was):
        erg = echt(befehl, frist, was)
        gemischt["ja"] = gemischt["ja"] or was == "Mischen"
        return erg

    with _ordner() as basis, mock.patch.object(clipschnitt, "_ffmpeg_laufen", merken):
        try:
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=stimmen, flo=[],
                                        budget=int(9.5 * 1048576), titel="w", whisper=True,
                                        tmp_basis=basis, abbruch=lambda: gemischt["ja"]))
            raise AssertionError("haette abbrechen muessen")
        except RenderFehler as fehler:
            assert (fehler.grund, fehler.meldung) == ("zeit", "abgebrochen")
        assert gemischt["ja"] and os.listdir(basis) == []


def test_clipschnitt_zu_gross_drueckt_die_bitrate():
    """Verschaetzt sich die Rechnung, kodiert der zweite Versuch mit der Stufe,
    die nach der gemessenen Groesse passt - und das Ergebnis ist im Budget."""
    if not _echt_da():
        return
    a = Spurdaten(1, "Anna", False, _echt(330, 0.0, 8.0, strang=7))
    laeufe = []
    echt = clipschnitt._ffmpeg_laufen

    def zaehlen(befehl, frist, was):
        laeufe.append(befehl[befehl.index("-b:a") + 1] if "-b:a" in befehl else was)
        return echt(befehl, frist, was)

    with _ordner() as basis, mock.patch.object(clipschnitt, "bitrate_fuer", lambda d, b: 192), \
            mock.patch.object(clipschnitt, "_ffmpeg_laufen", zaehlen):
        erg = clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=[a], flo=[],
                                          budget=70_000, titel="t", tmp_basis=basis))
        assert laeufe == ["192k", "64k"], laeufe
        assert erg.kbps == 64 and len(erg.mp3) <= 70_000
        assert "bitrate_gesenkt" in erg.hinweise
        assert os.listdir(basis) == []


def test_clipschnitt_zu_gross_nach_drei_versuchen():
    """Passt es auch nach VERSUCHE_MAX Versuchen nicht: zu_gross. Und was schon
    rechnerisch nicht passt, wird gar nicht erst kodiert."""
    if not _echt_da():
        return
    a = Spurdaten(1, "Anna", False, _echt(330, 0.0, 8.0, strang=7))
    laeufe = []
    echt = clipschnitt._ffmpeg_laufen

    def zaehlen(befehl, frist, was):
        laeufe.append(was)
        return echt(befehl, frist, was)

    with _ordner() as basis, mock.patch.object(clipschnitt, "LUFT", 3.0), \
            mock.patch.object(clipschnitt, "_ffmpeg_laufen", zaehlen):
        try:
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=[a], flo=[],
                                        budget=70_000, titel="t", tmp_basis=basis))
            raise AssertionError("haette zu_gross sein muessen")
        except RenderFehler as fehler:
            assert fehler.grund == "zu_gross"
        assert laeufe == ["Mischen"] * clipschnitt.VERSUCHE_MAX
        assert os.listdir(basis) == []
    laeufe.clear()
    with _ordner() as basis, mock.patch.object(clipschnitt, "_ffmpeg_laufen", zaehlen):
        try:
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=8.0, stimmen=[a], flo=[],
                                        budget=2_000, titel="t", tmp_basis=basis))
            raise AssertionError("haette zu_gross sein muessen")
        except RenderFehler as fehler:
            assert fehler.grund == "zu_gross"
        assert laeufe == [] and os.listdir(basis) == []


def test_clipschnitt_abbruch_mitten_im_dekodieren():
    """Der Abbruch-Schalter (Herunterfahren, Mikro aus) greift auch mitten in
    einer langen Spur - beim naechsten Halt alle FRIST_PRUEFEN_ALLE Rahmen."""
    if not _echt_da():
        return
    a = Spurdaten(1, "Anna", False, _echt(330, 0.0, 30.0, strang=7, dauer=30.0))
    gefragt = []

    def schalter():
        gefragt.append(1)
        return len(gefragt) > 2          # erst beim Dekodieren

    with _ordner() as basis:
        try:
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=30.0, stimmen=[a], flo=[],
                                        budget=int(9.5 * 1048576), titel="t",
                                        tmp_basis=basis, abbruch=schalter))
            raise AssertionError("haette abbrechen muessen")
        except RenderFehler as fehler:
            assert fehler.grund == "zeit" and fehler.meldung == "abgebrochen"
        assert len(gefragt) == 3 and os.listdir(basis) == []


def test_clipschnitt_spur_ist_luecken_datei_und_plc_rettet_kaputtes():
    """Geschrieben wird nur, wo Ton ist - davor und danach Nullen, die Datei
    hat trotzdem die volle Laenge. Ein kaputtes Paket wird verschleiert statt
    den Schnitt zu sprengen."""
    if not _opus_da():
        return
    mono = clipschnitt._Mono()
    assert len(clipschnitt._dekodieren(mono, b"\xff")) == 1920        # PLC
    assert len(clipschnitt._dekodieren(mono, None)) == 1920
    assert len(clipschnitt._dekodieren(discord.opus.Decoder(), _STILLE)) == 3840
    pakete = _echt(330, 5.0, 6.0, strang=1, dauer=10.0)
    schuebe = clipschnitt.platzieren(pakete, _BIS, 10.0)
    with _ordner() as basis:
        pfad = os.path.join(basis, "v.raw")
        frist = clipschnitt._Frist(30)
        clipschnitt.spur_schreiben(pfad, schuebe, 1, 10 * _RATE, frist)
        assert os.path.getsize(pfad) == 10 * _RATE * 2
        with open(pfad, "rb") as datei:
            roh = datei.read()
        start = schuebe[0].start * 2
        assert roh[:start] == bytes(start)
        assert roh[start + 2 * 960 * 50:] == bytes(len(roh) - start - 2 * 960 * 50)
        assert any(roh[start:start + 2 * 960 * 50])


# =============================================================================
# Fehlerwege und Aufraeumen (ohne echtes ffmpeg)
# =============================================================================
def test_clipschnitt_ffmpeg_kaputt_raeumt_trotzdem_auf():
    """Scheitert ffmpeg (hier: ein Programm, das immer mit 1 endet), kommt
    RenderFehler('ffmpeg') - und der Temp-Ordner ist trotzdem weg. Fehlt
    libopus, ist es derselbe Grund und dasselbe Aufraeumen."""
    falsch = shutil.which("false")
    if not falsch:
        return
    a = Spurdaten(1, "Anna", False, _schub(995.0, 50))
    with _ordner() as basis, _faehig(ffmpeg=falsch), _log() as zeilen:
        try:
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=10.0, stimmen=[a], flo=[],
                                        budget=int(9.5 * 1048576), titel="Anna & Bob",
                                        tmp_basis=basis))
            raise AssertionError("haette scheitern muessen")
        except RenderFehler as fehler:
            assert fehler.grund == "ffmpeg", fehler
        assert os.listdir(basis) == []
    if _opus_da():
        assert any("ffmpeg (Mischen) gescheitert, Code 1" in z for z in zeilen), zeilen
    assert not any("Anna" in z for z in zeilen), zeilen          # nur Zahlen ins Log


@contextlib.contextmanager
def _haengendes_ffmpeg(sekunden=30):
    """Statt ffmpeg ein Python, das nur schlaeft - der echte Interpreter mit
    -c, kein Skript im Temp-Ordner: der ist auf gehaerteten Servern oft
    noexec, und dann scheiterte der Test am Server statt am Code. Die
    ffmpeg-Argumente landen dabei harmlos in sys.argv. Liefert die Liste der
    gestarteten Prozesse (um zu pruefen, dass keiner ueberlebt)."""
    prozesse = []
    echt = subprocess.Popen

    def merken(*a, **kw):
        prozess = echt(*a, **kw)
        prozesse.append(prozess)
        return prozess

    schlaf = f"import time; time.sleep({sekunden})"
    with mock.patch.object(clipschnitt, "_vorne", lambda faehig: [sys.executable, "-c", schlaf]), \
            mock.patch.object(clipschnitt.subprocess, "Popen", merken):
        yield prozesse


def _tot(prozess):
    """Beendet UND eingesammelt (kein Zombie, keine Waise)."""
    return prozess.returncode is not None and prozess.returncode < 0


def test_clipschnitt_ffmpeg_haengt_wird_zeit():
    """Haengt ffmpeg, endet der Lauf an der Zeitgrenze mit 'zeit' - ffmpeg wird
    abgeschossen, und auch dann ist der Ordner weg."""
    if not _opus_da():
        return
    a = Spurdaten(1, "Anna", False, _schub(995.0, 20))
    with _ordner() as basis:
        start = time.monotonic()
        with _faehig(), _haengendes_ffmpeg() as prozesse, \
                mock.patch.object(clipschnitt, "FFMPEG_FRIST", 1.0), _log() as zeilen:
            try:
                clipschnitt.rendern(Auftrag(bis=_BIS, dauer=10.0, stimmen=[a], flo=[],
                                            budget=int(9.5 * 1048576), titel="t",
                                            tmp_basis=basis))
                raise AssertionError("haette abbrechen muessen")
            except RenderFehler as fehler:
                assert (fehler.grund, fehler.meldung) == ("zeit", "ffmpeg Mischen zu langsam")
        assert time.monotonic() - start < 10
        assert len(prozesse) == 1 and _tot(prozesse[0]), [p.returncode for p in prozesse]
        assert os.listdir(basis) == []
        assert any("nach 1 s abgebrochen" in z for z in zeilen), zeilen


def test_clipschnitt_abbruch_waehrend_ffmpeg_laeuft():
    """'Mikro aus' oder Herunterfahren, waehrend ffmpeg mischt: der Lauf wartet
    nicht bis zur Zeitgrenze (bis zu 90 s), sondern bricht beim naechsten Halt
    ab - und ffmpeg stirbt mit, statt als Waise weiterzurechnen."""
    if not _opus_da():
        return
    a = Spurdaten(1, "Anna", False, _schub(995.0, 20))
    with _ordner() as basis, _faehig(), _haengendes_ffmpeg() as prozesse, \
            mock.patch.object(clipschnitt, "FFMPEG_FRIST", 8.0):
        start = time.monotonic()
        try:
            # Der Schalter kippt, sobald ffmpeg laeuft - vorher (Dekodieren)
            # nicht: es geht genau um die Zeit IN ffmpeg.
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=10.0, stimmen=[a], flo=[],
                                        budget=int(9.5 * 1048576), titel="t",
                                        tmp_basis=basis, abbruch=lambda: bool(prozesse)))
            raise AssertionError("haette abbrechen muessen")
        except RenderFehler as fehler:
            assert (fehler.grund, fehler.meldung) == ("zeit", "abgebrochen"), fehler
        dauer = time.monotonic() - start
        assert dauer < 4, dauer
        assert len(prozesse) == 1 and _tot(prozesse[0]), [p.returncode for p in prozesse]
        assert os.listdir(basis) == []


def test_clipschnitt_frist_abbruch_und_leer():
    """Ohne Zeit: 'zeit'. Mit gezogenem Abbruch-Schalter: 'zeit' (abgebrochen);
    ein Schalter, der selbst wirft, bricht nichts ab. Nichts im Fenster oder
    Dauer 0: 'leer'. Immer ohne Reste."""
    a = Spurdaten(1, "Anna", False, _schub(995.0, 20))
    with _ordner() as basis, _faehig():
        def schnitt(**kw):
            werte = dict(bis=_BIS, dauer=10.0, stimmen=[a], flo=[], budget=10**7,
                         titel="t", tmp_basis=basis)
            werte.update(kw)
            try:
                clipschnitt.rendern(Auftrag(**werte))
            except RenderFehler as fehler:
                return fehler
            raise AssertionError("haette scheitern muessen")

        fehler = schnitt(frist=0)
        assert (fehler.grund, fehler.meldung) == ("zeit", "Frist abgelaufen")
        fehler = schnitt(abbruch=lambda: True)
        assert (fehler.grund, fehler.meldung) == ("zeit", "abgebrochen")

        def kaputter_schalter():
            raise RuntimeError("weg")
        fehler = schnitt(frist=0, abbruch=kaputter_schalter)
        assert fehler.meldung == "Frist abgelaufen"
        draussen = Spurdaten(2, "Weg", False, _schub(900.0, 20))
        assert schnitt(stimmen=[draussen]).grund == "leer"
        assert schnitt(stimmen=[], flo=[(900.0, _RAHMEN)]).grund == "leer"
        assert schnitt(dauer=0).grund == "leer"
        assert schnitt(stimmen={2: draussen}).grund == "leer"     # auch als dict
        assert os.listdir(basis) == []
    with _faehig(grund="ffmpeg fehlt"):
        try:
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=10.0, stimmen=[a], flo=[],
                                        budget=10**7, titel="t"))
            raise AssertionError("ohne ffmpeg kein Schnitt")
        except RenderFehler as fehler:
            assert fehler.grund == "ffmpeg" and fehler.meldung == "ffmpeg fehlt"
    assert RenderFehler("quatsch").grund == "ffmpeg"


def test_clipschnitt_unerwartetes_wird_renderfehler():
    """Ein kaputter Auftrag (hier: eine Spur ohne Pakete) oder ein
    Programmierfehler kommt als RenderFehler('ffmpeg') heraus - clips.py kennt
    nur die eine Fehlerart -, mit der Spur im Log und ohne Reste."""
    kaputt = SimpleNamespace(uid=1, bot=False)
    with _ordner() as basis, _faehig(), _log() as zeilen:
        try:
            clipschnitt.rendern(Auftrag(bis=_BIS, dauer=10.0, stimmen=[kaputt], flo=[],
                                        budget=10**7, titel="t", tmp_basis=basis))
            raise AssertionError("haette scheitern muessen")
        except RenderFehler as fehler:
            assert (fehler.grund, fehler.meldung) == ("ffmpeg", "AttributeError")
            assert isinstance(fehler.__cause__, AttributeError)
        assert os.listdir(basis) == []
    assert any("unerwartet gescheitert" in z for z in zeilen), zeilen


def test_clipschnitt_temp_ordner_ist_privat():
    """mkdtemp mit 0700, Praefix mit PID; CLIP_TMP_DIR wird angelegt und
    genutzt - und wenn das nicht geht, das System-Temp."""
    with _ordner() as basis:
        pfad = clipschnitt._tmp_anlegen(basis)
        name = os.path.basename(pfad)
        assert name.startswith(f"flo-clip-{os.getpid()}-")
        assert stat.S_IMODE(os.stat(pfad).st_mode) == 0o700
        os.rmdir(pfad)
        eigen = os.path.join(basis, "clips", "tmp")
        with mock.patch.dict(os.environ, {"CLIP_TMP_DIR": eigen}):
            assert clipschnitt._tmp_basis() == eigen and os.path.isdir(eigen)
        datei = os.path.join(basis, "datei")
        with open(datei, "w", encoding="utf-8") as f:
            f.write("x")
        with mock.patch.dict(os.environ, {"CLIP_TMP_DIR": os.path.join(datei, "unter")}), \
                _log() as zeilen:
            assert clipschnitt._tmp_basis() == tempfile.gettempdir()
        assert any("nicht nutzbar" in z for z in zeilen), zeilen


def test_clipschnitt_alte_reste_weg_nur_eigene():
    """Weg kommen nur 'flo-clip-*'-ORDNER: ohne PID (alt), mit toter PID oder
    der eigenen. Ein laufender fremder Prozess behaelt seinen; Dateien, Links
    und andere Namen bleiben - auch das Ziel eines Links."""
    with _ordner() as basis, _ordner() as draussen:
        def ordner(name):
            pfad = os.path.join(basis, name)
            os.mkdir(pfad)
            with open(os.path.join(pfad, "v0.raw"), "wb") as f:
                f.write(b"x")
            return pfad
        weg = [ordner("flo-clip-alt"), ordner("flo-clip-999999999-abc"),
               ordner(f"flo-clip-{os.getpid()}-eigen")]
        bleibt = [ordner(f"flo-clip-{os.getppid()}-laeuft"), ordner("flo-clipper"),
                  ordner("andere-flo-clip-1"), ordner("FLO-CLIP-gross")]
        with open(os.path.join(basis, "flo-clip-datei"), "w", encoding="utf-8") as f:
            f.write("x")
        os.symlink(draussen, os.path.join(basis, "flo-clip-link"))
        with open(os.path.join(draussen, "wichtig"), "w", encoding="utf-8") as f:
            f.write("x")
        assert clipschnitt.alte_reste_weg(basis) == 3
        for pfad in weg:
            assert not os.path.exists(pfad), pfad
        for pfad in bleibt:
            assert os.path.isdir(pfad), pfad
        assert os.path.isfile(os.path.join(basis, "flo-clip-datei"))
        assert os.path.islink(os.path.join(basis, "flo-clip-link"))
        assert os.path.isfile(os.path.join(draussen, "wichtig"))
        assert clipschnitt.alte_reste_weg(basis) == 0
        assert clipschnitt.alte_reste_weg(os.path.join(basis, "gibts-nicht")) == 0


def test_clips_bibliotheken_import_ohne_nebenwirkungen():
    """Beim Import von clipschnitt und clipstext passiert nichts: kein
    Prozess, kein Thread, keine Verbindung, keine geschriebene Datei, kein
    Client - erst wenn clips.py fragt. Gemessen in einem frischen Prozess, in
    dem die Abhaengigkeiten schon geladen sind und dann alles verwanzt wird."""
    wurzel = os.path.dirname(os.path.abspath(__file__))
    probe = (
        "import builtins, os, socket, subprocess, threading, sys\n"
        "import discord, ai, basis\n"
        "taten = []\n"
        "def wanze(name, echt):\n"
        "    def f(*a, **k):\n"
        "        taten.append(name)\n"
        "        return echt(*a, **k)\n"
        "    return f\n"
        "subprocess.Popen.__init__ = wanze('popen', subprocess.Popen.__init__)\n"
        "threading.Thread.start = wanze('thread', threading.Thread.start)\n"
        "socket.socket.connect = wanze('connect', socket.socket.connect)\n"
        "echt_open = builtins.open\n"
        "def offen(datei, modus='r', *a, **k):\n"
        "    if any(z in str(modus) for z in 'wax+'):\n"
        "        taten.append('schreiben')\n"
        "    return echt_open(datei, modus, *a, **k)\n"
        "builtins.open = offen\n"
        "import clipschnitt, clipstext\n"
        "assert clipschnitt._FAEHIG is None\n"
        "assert clipstext.transkriber._eigen is None\n"
        "print('TATEN', taten)\n")
    umgebung = dict(os.environ, FLO_TESTLAUF="1", PYTHONDONTWRITEBYTECODE="1",
                    DATA_DIR=tempfile.mkdtemp(prefix="flobot-importprobe-"))
    try:
        fertig = subprocess.run([sys.executable, "-c", probe], cwd=wurzel, env=umgebung,
                                capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(umgebung["DATA_DIR"], ignore_errors=True)
    assert fertig.returncode == 0, fertig.stderr[-2000:]
    assert "TATEN []" in fertig.stdout, fertig.stdout + fertig.stderr[-2000:]


if __name__ == "__main__":
    run(globals())
