"""Voice-Clips, der Text: Transkript, Pruefung, Kommentar, Bestenliste (clipstext.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur clipstext      nur diese Tests

Kein Test hier fragt das Netz: Whisper ist eine Attrappe (_Whisper), die sich
jeden Aufruf merkt, und ai.generate ist durch _KiAttrappe ersetzt. Die Uhr des
Kontingents stellt _Uhr von Hand.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module

import contextlib
import json
import logging
from unittest import mock

import discord

import ai
import basis
import clipstext
from clipschnitt import WhisperAudio
from clipstext import Kontingent, Segment, Transkriber, Zeile


# =============================================================================
# Helfer
# =============================================================================
class _Uhr:
    """Wanduhr zum Stellen."""

    def __init__(self, jetzt=1_800_000_000.0):
        self.jetzt = jetzt

    def __call__(self):
        return self.jetzt

    def vor(self, sekunden):
        self.jetzt += sekunden


class _Whisper:
    """Statt des openai-Clients: client.audio.transcriptions.create(**kw).
    'antwort' bekommt die kw und liefert die Antwort - oder wirft."""

    def __init__(self, antwort):
        self.antwort = antwort
        self.aufrufe = []
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.aufrufe.append(kw)
        erg = self.antwort(kw)
        if asyncio.iscoroutine(erg):
            erg = await erg
        return erg


class _Fehler429(Exception):
    """So sieht ein openai.RateLimitError von aussen aus."""

    status_code = 429

    def __init__(self, warten="120"):
        super().__init__("Rate limit reached")
        self.response = SimpleNamespace(status_code=429, headers={"retry-after": warten})


class _KiAttrappe:
    """Statt ai.generate: liefert die vorgegebene Antwort und merkt sich jeden
    Aufruf - samt dem Server, der dabei 'dran' war (ai.aktuelle_guild)."""

    def __init__(self, antwort):
        self.antwort = antwort
        self.aufrufe = []
        self.gilden = []

    async def __call__(self, prompt, **kw):
        self.aufrufe.append((prompt, kw))
        self.gilden.append(ai.aktuelle_guild())
        if callable(self.antwort):
            erg = self.antwort(prompt)
            return await erg if asyncio.iscoroutine(erg) else erg
        return self.antwort


@contextlib.contextmanager
def _umgebung(**werte):
    """Umgebungsvariablen fuer die Dauer des Blocks (None = weg)."""
    with mock.patch.dict(os.environ):
        for name, wert in werte.items():
            if wert is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = wert
        yield


@contextlib.contextmanager
def _log():
    """Faengt die Logzeilen von clipstext - als Liste fertiger Saetze."""
    zeilen = []

    class Fang(logging.Handler):
        def emit(self, record):
            zeilen.append(record.getMessage())

    logger = logging.getLogger("dcbot.clipstext")
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


def _transkriber(antwort, uhr=None):
    fake = _Whisper(antwort)
    tr = Transkriber(Kontingent(uhr=uhr or _Uhr()))
    tr.client = lambda: fake
    return tr, fake


def _seg(text, start=0.0, ende=1.0, **kw):
    return Segment(start=start, ende=ende, text=text, **kw)


def _ki_an(antwort):
    """KI an, ai.generate durch die Attrappe ersetzt."""
    ki = _KiAttrappe(antwort)
    stapel = contextlib.ExitStack()
    stapel.enter_context(mock.patch.object(ai, "is_enabled", lambda: True))
    stapel.enter_context(mock.patch.object(ai, "generate", ki))
    return ki, stapel


_ZEILEN = [
    Zeile(t=1.0, uid=11, name="Anna", text="Bob, du hast die Pizza schon wieder verbrannt"),
    Zeile(t=3.5, uid=22, name="Bob", text="Die war doch nur ein bisschen schwarz"),
    Zeile(t=6.0, uid=11, name="Anna", text="Meine Nummer ist 0171 2345678, ruf an"),
]


# =============================================================================
# Segmente und Halluzinationen
# =============================================================================
def test_clipstext_segmente_aus_objekt_und_dict():
    """Die Antwort darf ein Objekt (openai-Paket) oder ein dict sein - es kommt
    dasselbe heraus. Kaputte Segmente fallen raus, Zahlen als Text gehen."""
    roh = [{"start": 0.0, "end": 1.2, "text": " Hallo  du ", "avg_logprob": -0.3,
            "no_speech_prob": 0.1, "compression_ratio": 1.2},
           {"start": "1.5", "end": 2.0, "text": "zwei"},
           {"end": 3.0, "text": "ohne Start"},
           {"start": 3.0, "end": 4.0, "text": "   "},
           "kaputt"]
    erwartet = [Segment(0.0, 1.2, "Hallo du", -0.3, 0.1, 1.2), Segment(1.5, 2.0, "zwei")]
    assert clipstext.segmente_lesen({"segments": roh}) == erwartet
    als_objekt = SimpleNamespace(segments=[SimpleNamespace(**s) if isinstance(s, dict) else s
                                           for s in roh])
    assert clipstext.segmente_lesen(als_objekt) == erwartet
    # Ganz ohne Segmente, aber mit Text: ein Segment ohne Zeiten.
    assert clipstext.segmente_lesen({"text": " Nur Text "}) == [Segment(0.0, 0.0, "Nur Text")]
    assert clipstext.segmente_lesen({"segments": [], "text": "egal"}) == []
    for muell in (None, "quatsch", 42, {}):
        assert clipstext.segmente_lesen(muell) == [], muell


def test_clipstext_segmente_aus_der_echten_openai_antwort():
    """Mit dem echten Antworttyp aus dem openai-Paket (wie Groq ihn liefert)."""
    try:
        from openai.types.audio import TranscriptionVerbose
    except ImportError:
        return
    antwort = TranscriptionVerbose.model_validate({
        "duration": 3.0, "language": "german", "text": "Hallo du",
        "segments": [{"id": 0, "seek": 0, "start": 0.0, "end": 1.5, "text": " Hallo du",
                      "tokens": [1, 2], "temperature": 0.0, "avg_logprob": -0.2,
                      "compression_ratio": 1.1, "no_speech_prob": 0.01}]})
    assert clipstext.segmente_lesen(antwort) == [Segment(0.0, 1.5, "Hallo du", -0.2, 0.01, 1.1)]


def test_clipstext_halluzinationen_fliegen_raus():
    """Whispers Zahlen an der Grenze, die typischen Abspann-Saetze und reine
    Geraeusch-Marken fliegen raus - echte Saetze bleiben, auch wenn sie
    'Musik' oder 'Untertitel' enthalten."""
    h = clipstext.halluzination
    # Grenzwerte: Stille UND wenig Zuversicht, sehr wenig Zuversicht, Schleife.
    assert h(_seg("Hallo", no_speech_prob=0.6, avg_logprob=-0.8))
    assert not h(_seg("Hallo", no_speech_prob=0.59, avg_logprob=-0.8))
    assert not h(_seg("Hallo", no_speech_prob=0.6, avg_logprob=-0.79))
    assert h(_seg("Hallo", avg_logprob=-1.21)) and not h(_seg("Hallo", avg_logprob=-1.2))
    assert h(_seg("Hallo", compression_ratio=2.41)) and not h(_seg("Hallo", compression_ratio=2.4))
    for leer in ("", "   ", "...", "?!", "—"):
        assert h(_seg(leer)), leer
    for erfunden in ("Untertitel im Auftrag des ZDF, 2021", "Untertitelung des ZDF für funk, 2017",
                     "Untertitel von Stephanie Geiges für ARD", "Untertitel: amara.org",
                     "Vielen Dank fürs Zuschauen!", "Vielen Dank für's Zuhören.",
                     "Danke fürs Zuschauen", "Abonniert den Kanal!", "Bis zum nächsten Mal.",
                     "Wir sehen uns im nächsten Video", "Copyright WDR 2020", "© 2021",
                     "[Musik]", "(Applaus)", "*Gelächter*", "Musik", "[Stille]", "♪♪♪",
                     " ♪ "):
        assert h(_seg(erfunden)), erfunden
    for echt in ("Alter, hast du das gesehen?", "Ich hab Musik gehört, die war richtig gut",
                 "Danke für die Pizza", "Untertitel braucht keiner", "Lass uns Musik machen",
                 "ok", "Der Bruder von dem ist bei der ARD"):
        assert not h(_seg(echt)), echt
    # Auch als dict und mit Muell in den Zahlen.
    assert h({"text": "[Musik]"}) and not h({"text": "Na?", "avg_logprob": "quatsch"})


def test_clipstext_halluzinationen_auch_die_kurzformen():
    """Was die erste Liste durchliess: "Danke fuers Zusehen", der Vortrags-
    Abspann, das Video-Outro, ein Sender mit Jahreszahl oder Adresse. Kurze
    Hoeflichkeiten ("Vielen Dank.", "Tschuess!") fallen nur, wenn Whisper
    selbst schon an Stille glaubt - echt gesagt bleiben sie stehen."""
    h = clipstext.halluzination
    for erfunden in ("Danke fürs Zusehen!", "Danke für's Zusehen.", "Vielen Dank fürs Zusehen",
                     "Vielen Dank für Ihre Aufmerksamkeit.", "Vielen Dank für eure Aufmerksamkeit!",
                     "Ich hoffe, euch hat das Video gefallen.", "Wir sehen uns beim nächsten Video!",
                     "SWR 2021", "ARD, 2019.", "Mehr Infos auf www.zdf.de",
                     "Untertitel der Amara.org-Community"):
        assert h(_seg(erfunden)), erfunden
    kurz = ("Vielen Dank.", "Danke schön.", "Dankeschön!", "Tschüss!", "Tschüs.", "Ciao!",
            "Ich danke Ihnen.", "Das war's für heute.", "Das war’s.", "Bis bald!",
            "Und tschüss.", "Auf Wiedersehen.")
    for erfunden in kurz:
        assert h(_seg(erfunden, no_speech_prob=0.3)), erfunden
        assert not h(_seg(erfunden, no_speech_prob=0.29)), erfunden        # echt gesagt
    for echt in ("Vielen Dank, dass du die Pizza geholt hast", "Tschüss Leute, ich muss los",
                 "Das war's mit dem Spiel, ich bin raus", "Danke fürs Mitnehmen",
                 "Der ZDF-Krimi gestern war Müll", "Ich hab 2021 beim SWR gearbeitet"):
        assert not h(_seg(echt, no_speech_prob=0.5)), echt


def test_clipstext_bereinigen_ohne_wiederholung():
    """Whisper in der Schleife: derselbe Satz direkt hintereinander zaehlt
    einmal - mit Abstand dazwischen darf er wiederkommen."""
    segmente = [_seg("Hallo!"), _seg("hallo"), _seg("HALLO."), _seg("[Musik]"),
                _seg("Tschüss"), _seg("Hallo")]
    assert [s.text for s in clipstext.bereinigen(segmente)] == ["Hallo!", "Tschüss", "Hallo"]
    assert clipstext.bereinigen(None) == []


# =============================================================================
# Zeilen, Pruefung, Transkript
# =============================================================================
def test_clipstext_zeilen_ueber_die_karte_verzahnt():
    """Jedes Segment geht ueber die Karte aus clipschnitt auf die Clip-Zeit.
    Die Sprecher werden nach Zeit verzahnt, kurze Pausen desselben Sprechers
    ergeben eine Zeile."""
    karten = {1: [(0.0, 2.0, 0.5), (2.4, 4.4, 4.0)],
              2: WhisperAudio(b"x", [(0.0, 5.0, 2.0)])}
    segmente = {1: [_seg("Erster Satz", 0.1, 1.9), _seg("Zweiter Satz", 2.5, 4.3)],
                2: [_seg("Zwischenruf", 0.5, 1.0), {"start": 1.2, "end": 1.8, "text": "noch was"}]}
    zeilen = clipstext.zeilen_bauen(segmente, karten, {1: "Anna", 2: "Bob"})
    assert [(z.t, z.uid, z.name, z.text) for z in zeilen] == [
        (0.6, 1, "Anna", "Erster Satz"), (2.5, 2, "Bob", "Zwischenruf noch was"),
        (4.1, 1, "Anna", "Zweiter Satz")]
    # Die Karte selbst: Pause -> naechstes Stueck (Anfang) / voriges (Ende).
    karte = karten[1]
    assert clipstext.clipzeit(2.2, karte) == 4.0
    assert clipstext.clipzeit(2.2, karte, ende=True) == 2.5
    assert clipstext.clipzeit(9.0, karte) == 6.0
    assert clipstext.clipzeit(1.5, []) == 1.5 and clipstext.clipzeit(-1, None) == 0.0
    # Zusammengelegt wird nur, wer direkt hintereinander redet.
    zeilen = clipstext.zeilen_bauen(
        {1: [_seg("a", 0.0, 1.0), _seg("c", 1.8, 2.0), _seg("d", 4.0, 5.0)],
         2: [_seg("b", 1.2, 1.5)]}, {}, {1: "A"})
    assert [(z.uid, z.text) for z in zeilen] == [(1, "a"), (2, "b"), (1, "c"), (1, "d")]
    assert zeilen[1].name == "?"
    assert clipstext.zeilen_bauen({}, {}, {}) == []
    assert clipstext.zeilen_bauen(None, None, None) == []


def test_clipstext_pruefen_findet_notlage_hetze_und_privates():
    """Notsignal je Sprecher, Hetze, private Daten je Zeile - und harmloses
    Gelaber findet nichts."""
    zeilen = [Zeile(0, 1, "A", "Alter, lass mal zocken"),
              Zeile(1, 2, "B", "ich will nicht mehr leben"),
              Zeile(2, 3, "C", "Ausländer raus"),
              Zeile(3, 4, "D", "meine Nummer ist 0171 2345678")]
    befund = clipstext.pruefen(zeilen)
    assert befund.notlage_uids == {2} and befund.hetze is True and befund.heikel == {3}
    leer = clipstext.pruefen(zeilen[:1])
    assert leer.notlage_uids == set() and leer.hetze is False and leer.heikel == set()
    assert clipstext.pruefen([]).hetze is False


def test_clipstext_transkript_ohne_ping_und_mit_ende():
    """Kein Markdown, kein Ping (auch nicht <@id> oder @everyone), private
    Zeilen geschwaerzt. Zu lang: das Ende bleibt, vorne steht '…'."""
    zeilen = [Zeile(0.4, 1, "Anna_*", "@everyone guckt mal **hier** <@123>"),
              Zeile(65.2, 2, "B@b", "meine Nummer ist 0171 2345678"),
              Zeile(70.0, 1, "Anna_*", "`code` und ~~weg~~ und @here")]
    text = clipstext.transkript_text(zeilen, zensiert={1})
    zeilen_text = text.split("\n")
    assert zeilen_text[0].startswith("`0:00` **Anna\\_\\*:** "), zeilen_text[0]
    assert "@\u200beveryone" in zeilen_text[0] and "<@\u200b123>" in zeilen_text[0]
    assert "\\*\\*hier\\*\\*" in zeilen_text[0]
    assert zeilen_text[1] == "`1:05` **B@\u200bb:** [privat – zensiert]"
    assert "0171" not in text and "\\~\\~weg\\~\\~" in zeilen_text[2]
    assert not re.search("@(?!\u200b)", text)
    # Lang: das Ende bleibt.
    viele = [Zeile(i, 1, "Anna", f"Satz Nummer {i} " + "bla " * 20) for i in range(60)]
    text = clipstext.transkript_text(viele, max_zeichen=1000)
    assert len(text) <= 1000 and text.startswith("…\n")
    assert text.split("\n")[-1] == clipstext.transkript_text(viele[-1:])
    assert "Satz Nummer 59" in text and "Satz Nummer 0 " not in text
    # Eine einzelne, ueberlange Zeile: Zeit und Name bleiben, vom Text das
    # Ende - nie ueber der Grenze, auch wenn Escapes den Text verlaengern.
    satz = "Anfang " + "bla " * 120 + "die **Pointe**"
    eine = clipstext.transkript_text([Zeile(1, 1, "Anna", satz)], max_zeichen=80)
    assert len(eine) <= 80 and eine.startswith("`0:01` **Anna:** …"), eine
    assert eine.endswith("die \\*\\*Pointe\\*\\*") and "Anfang" not in eine, eine
    # Nicht einmal Zeit und Name passen (oder die Zeile ist geschwaerzt):
    # vorne behalten, hinten gekappt - auch das nie ueber der Grenze.
    for zensiert in (set(), {0}):
        winzig = clipstext.transkript_text([Zeile(1, 1, "Anna", satz)], max_zeichen=12,
                                           zensiert=zensiert)
        assert len(winzig) <= 12 and winzig.endswith("…"), winzig
    # Eine ueberlange Zeile wird auf ZEILE_MAX gekuerzt - von vorne, der
    # Schluss bleibt.
    lang = clipstext.transkript_text([Zeile(1, 1, "Anna", "wort " * 1000 + "Schluss")])
    assert len(lang) < clipstext.ZEILE_MAX + 40 and lang.endswith(" Schluss"), lang[-40:]
    assert lang.startswith("`0:01` **Anna:** …wort")
    assert clipstext.transkript_text([]) == ""


def _monolog(n=70, privat_bei=3):
    """Ein Fuenf-Minuten-Monolog, wie Whisper ihn liefert: Segment an Segment,
    je 4 s, 0,2 s Pause - und in einem Satz eine Telefonnummer."""
    segmente, t = [], 0.0
    for i in range(n):
        text = f"Satz {i} und dann hab ich noch was erzählt"
        if i == privat_bei:
            text = "ruf mich an unter 0171 2345678"
        segmente.append(_seg(text, t, t + 4.0))
        t += 4.2
    return segmente, [(0.0, t, 0.0)]


def test_clipstext_monolog_bleibt_mehrere_zeilen():
    """Ein Monolog (der Normalfall bei 'flo clip 5m @wer') wird nicht EINE
    Riesenzeile: Zeilen haben eine Hoechstlaenge und -spanne. Dadurch bleibt
    im Transkript das Ende stehen, eine Telefonnummer schwaerzt nur ihren Satz
    (der steht immer allein), und die KI bekommt den Rest trotzdem zu sehen."""
    segmente, karte = _monolog()
    zeilen = clipstext.zeilen_bauen({1: segmente}, {1: karte}, {1: "Anna"})
    assert len(zeilen) > 10, len(zeilen)
    for zeile, naechste in zip(zeilen, zeilen[1:] + [None]):
        assert len(zeile.text) <= clipstext.ZEILE_ZUSAMMEN_MAX, len(zeile.text)
        ende = naechste.t if naechste else 70 * 4.2
        assert ende - zeile.t <= clipstext.ZEILE_SPANNE_SEK + 4.2, (zeile.t, ende)
    privat = [i for i, z in enumerate(zeilen) if "0171" in z.text]
    assert len(privat) == 1 and zeilen[privat[0]].text == "ruf mich an unter 0171 2345678"
    befund = clipstext.pruefen(zeilen)
    assert befund.heikel == set(privat), befund.heikel
    text = clipstext.transkript_text(zeilen, zensiert=befund.heikel)
    assert len(text) <= clipstext.TRANSKRIPT_MAX
    assert text.endswith("Satz 69 und dann hab ich noch was erzählt"), text[-80:]
    assert "0171" not in text and text.count(clipstext.ZENSIERT) <= 1
    prompt = clipstext._kommentar_prompt(zeilen, 300, befund.heikel)
    assert "Satz 69" in prompt and "0171" not in prompt
    anfang = clipstext._kommentar_prompt(zeilen[:privat[0] + 2], 30)
    assert "Satz 2 " in anfang and "Satz 4 " in anfang and "0171" not in anfang
    # Kurze Pausen desselben Sprechers werden weiter zusammengelegt.
    kurz = clipstext.zeilen_bauen({1: segmente[:3]}, {1: karte}, {1: "Anna"})
    assert len(kurz) == 1 and kurz[0].text.count("Satz") == 3


def test_clipstext_naht_zwischen_zwei_zeilen():
    """Zerfaellt eine Nummer oder ein Notsignal an der Grenze zweier Zeilen
    desselben Sprechers ("0171" | Pause | "2345678"), findet pruefen es
    trotzdem: beide Zeilen werden geschwaerzt, die KI sieht keine davon, der
    Sprecher gilt als in Not. Harmlose Nachbarn bleiben unberuehrt."""
    zeilen = [Zeile(0.0, 1, "Anna", "Meine Nummer ist 0171"),
              Zeile(2.0, 2, "Bob", "Moment, ich schreib mit"),
              Zeile(4.0, 1, "Anna", "2345678, ruf mich an"),
              Zeile(9.0, 1, "Anna", "und bring Chips mit")]
    befund = clipstext.pruefen(zeilen)
    assert befund.heikel == {0, 2}, befund.heikel
    text = clipstext.transkript_text(zeilen, zensiert=befund.heikel)
    assert "0171" not in text and "2345678" not in text and "Chips" in text
    prompt = clipstext._kommentar_prompt(zeilen, 30)
    assert "0171" not in prompt and "2345678" not in prompt and "Chips" in prompt
    # Steht die Angabe schon ganz in einer Zeile, bleibt der Nachbar lesbar.
    zeilen[0] = Zeile(0.0, 1, "Anna", "Meine Nummer ist 0171 2345678")
    assert clipstext.pruefen(zeilen).heikel == {0}
    not_ = [Zeile(0.0, 3, "Cem", "ehrlich, ich will nicht"),
            Zeile(3.0, 3, "Cem", "mehr leben, alles ist scheisse")]
    assert clipstext.pruefen(not_).notlage_uids == {3}
    assert clipstext.pruefen(not_[:1]).notlage_uids == set()
    ki, stapel = _ki_an("Spruch.")
    with stapel:
        assert asyncio.run(clipstext.kommentar(77, not_, 30)) is None
    assert ki.aufrufe == []
    # Verschiedene Sprecher bilden keine Naht.
    fremd = [Zeile(0.0, 1, "Anna", "Meine Nummer ist 0171"), Zeile(2.0, 2, "Bob", "2345678")]
    assert clipstext.pruefen(fremd).heikel == set()


# =============================================================================
# Transkriber
# =============================================================================
def test_clipstext_transkriber_fragt_whisper_richtig():
    """Der Aufruf: das Modell (CLIP_WHISPER_MODEL, sonst turbo), Deutsch,
    verbose_json mit Segment-Zeiten, Temperatur 0, die Datei als Tupel. Was
    zurueckkommt, ist schon ohne Halluzinationen; gebucht wird je Sprecher
    mindestens 10 s."""
    def antwort(kw):
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "Hallo"},
                             {"start": 1.0, "end": 2.0,
                              "text": "Untertitel im Auftrag des ZDF, 2021"}]}

    tr, fake = _transkriber(antwort)
    audio = {11: WhisperAudio(b"ID3-elf", [(0.0, 3.0, 1.0)]),
             22: WhisperAudio(b"ID3-zweiundzwanzig", [(0.0, 12.0, 0.0)]),
             33: WhisperAudio(b"", [(0.0, 5.0, 0.0)])}             # ohne Ton: nicht gefragt
    with _umgebung(CLIP_WHISPER_MODEL=None), _log() as zeilen:
        erg = asyncio.run(tr.je_sprecher(audio, frist=5))
    assert set(erg) == {11, 22} and [s.text for s in erg[11]] == ["Hallo"]
    # Ins Log kommen nur Zahlen - kein Wort aus dem Transkript, keine uid.
    assert any("Transkript fuer 2 Sprecher" in z for z in zeilen), zeilen
    assert not any("Hallo" in z or "11" in z or "22" in z for z in zeilen), zeilen
    assert len(fake.aufrufe) == 2
    for kw in fake.aufrufe:
        assert kw["model"] == "whisper-large-v3-turbo"
        assert kw["language"] == "de" and kw["response_format"] == "verbose_json"
        assert kw["timestamp_granularities"] == ["segment"] and kw["temperature"] == 0
    assert {kw["file"] for kw in fake.aufrufe} == {
        ("sprecher.mp3", b"ID3-elf", "audio/mpeg"),
        ("sprecher.mp3", b"ID3-zweiundzwanzig", "audio/mpeg")}
    assert tr.kontingent.heute() == (2, 22)
    with _umgebung(CLIP_WHISPER_MODEL="whisper-large-v3"):
        asyncio.run(tr.je_sprecher({11: audio[11]}, frist=5))
    assert fake.aufrufe[-1]["model"] == "whisper-large-v3"
    # Nichts zu tun, kein Client: None, und gefragt wird niemand.
    assert asyncio.run(tr.je_sprecher({}, frist=5)) is None
    assert asyncio.run(tr.je_sprecher({33: audio[33]}, frist=5)) is None
    tr.client = lambda: None
    assert asyncio.run(tr.je_sprecher({11: audio[11]}, frist=5)) is None
    assert len(fake.aufrufe) == 3


def test_clipstext_transkriber_hoechstens_zwei_gleichzeitig():
    """Semaphore(2): nie mehr als zwei Sprecher gleichzeitig bei Whisper - auch
    nicht im naechsten Event-Loop (die Semaphore wird je Loop neu gebaut)."""
    stand = {"jetzt": 0, "hoechstens": 0}

    async def langsam(kw):
        stand["jetzt"] += 1
        stand["hoechstens"] = max(stand["hoechstens"], stand["jetzt"])
        await asyncio.sleep(0.03)
        stand["jetzt"] -= 1
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "Moin"}]}

    tr, fake = _transkriber(langsam)
    audio = {uid: WhisperAudio(b"x", [(0.0, 2.0, 0.0)]) for uid in range(1, 6)}
    for _runde in range(2):
        erg = asyncio.run(tr.je_sprecher(audio, frist=5))
        assert set(erg) == set(audio)
    assert stand["hoechstens"] == clipstext.WHISPER_PARALLEL == 2
    assert len(fake.aufrufe) == 10


def test_clipstext_transkriber_frist_fehler_und_429():
    """Frist um, ein einziger Fehler, 429: immer None - nie ein halbes
    Transkript. Nach einem 429 ist das Kontingent fuer Retry-After gesperrt;
    ein volles Kontingent fragt gar nicht erst."""
    audio = {1: WhisperAudio(b"x", [(0.0, 2.0, 0.0)]), 2: WhisperAudio(b"y", [(0.0, 2.0, 0.0)])}

    async def ewig(kw):
        await asyncio.sleep(10)

    tr, _fake = _transkriber(ewig)
    start = time.monotonic()
    assert asyncio.run(tr.je_sprecher(audio, frist=0.2)) is None
    assert time.monotonic() - start < 3

    def einer_kaputt(kw):
        if kw["file"][1] == b"y":
            raise RuntimeError("Verbindung weg")
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "Hallo"}]}

    tr, fake = _transkriber(einer_kaputt)
    assert asyncio.run(tr.je_sprecher(audio, frist=5)) is None and len(fake.aufrufe) == 2
    assert not tr.kontingent.gesperrt()

    def voll(kw):
        raise _Fehler429("120")

    uhr = _Uhr()
    tr, fake = _transkriber(voll, uhr)
    with _log() as zeilen:
        assert asyncio.run(tr.je_sprecher(audio, frist=5)) is None
    assert any("120 s Pause" in z for z in zeilen), zeilen
    assert tr.kontingent.gesperrt() and not tr.kontingent.darf([5])
    uhr.vor(119)
    assert tr.kontingent.gesperrt()
    uhr.vor(2)
    assert not tr.kontingent.gesperrt()
    # Der 429 sperrt auch, wenn ein anderer Fehler vor ihm in der Liste steht.

    def erst_kaputt_dann_voll(kw):
        if kw["file"][1] == b"x":
            raise RuntimeError("Verbindung weg")
        raise _Fehler429("30")

    tr, fake = _transkriber(erst_kaputt_dann_voll, uhr)
    assert asyncio.run(tr.je_sprecher(audio, frist=5)) is None and len(fake.aufrufe) == 2
    assert tr.kontingent._gesperrt_bis == uhr.jetzt + 30
    # Volles Kontingent: der Client wird gar nicht erst gefragt.
    tr, fake = _transkriber(lambda kw: {"segments": []})
    tr.kontingent.buchen([1] * tr.kontingent.MINUTE_ANFRAGEN)
    assert asyncio.run(tr.je_sprecher(audio, frist=5)) is None and fake.aufrufe == []
    # Selbst ein Client, der sich nicht bauen laesst (kaputte CLIP_WHISPER_URL),
    # wirft nicht bis zu clips durch.

    def kaputter_client():
        raise ValueError("Adresse kaputt")

    tr.client = kaputter_client
    assert asyncio.run(tr.je_sprecher(audio, frist=5)) is None


def test_clipstext_transkriber_bricht_beim_ersten_fehler_ab():
    """Ein Fehler kostet das ganze Transkript - also werden die uebrigen
    Sprecher gar nicht erst hochgeladen, und was schon laeuft, wird
    abgebrochen (sonst gingen bis zu 8 x 1,8 MB Ton fuer nichts zu Groq, und
    nach einem 429 haemmerte der Rest weiter auf die gesperrte Schnittstelle).
    Der 429 sperrt trotzdem."""
    for fehler, gesperrt in ((RuntimeError("Verbindung weg"), False),
                             (_Fehler429("45"), True)):
        abgebrochen = []

        async def antwort(kw, fehler=fehler, abgebrochen=abgebrochen):
            if kw["file"][1] == b"1":
                raise fehler
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                abgebrochen.append(kw["file"][1])
                raise
            return {"segments": [{"start": 0.0, "end": 1.0, "text": "Moin"}]}

        uhr = _Uhr()
        tr, fake = _transkriber(antwort, uhr)
        audio = {uid: WhisperAudio(str(uid).encode(), [(0.0, 2.0, 0.0)]) for uid in range(1, 7)}
        start = time.monotonic()
        assert asyncio.run(tr.je_sprecher(audio, frist=5)) is None
        assert time.monotonic() - start < 0.4
        geschickt = [kw["file"][1] for kw in fake.aufrufe]
        assert geschickt[0] == b"1" and len(geschickt) <= 3, geschickt
        assert abgebrochen and set(abgebrochen) == set(geschickt[1:]), (abgebrochen, geschickt)
        assert tr.kontingent.gesperrt() is gesperrt
        if gesperrt:
            assert tr.kontingent._gesperrt_bis == uhr.jetzt + 45


def test_clipstext_transkriber_abbruch_von_aussen_raeumt_auf():
    """Wird der Clip-Auftrag selbst abgebrochen (Herunterfahren), geht der
    Abbruch durch - und keine Whisper-Anfrage laeuft verwaist weiter."""
    laufend = []

    async def antwort(kw):
        laufend.append(kw["file"][1])
        try:
            await asyncio.sleep(5)
        finally:
            laufend.remove(kw["file"][1])

    tr, _fake = _transkriber(antwort)
    audio = {uid: WhisperAudio(str(uid).encode(), [(0.0, 2.0, 0.0)]) for uid in range(1, 4)}

    async def ablauf():
        auftrag = asyncio.ensure_future(tr.je_sprecher(audio, frist=30))
        await asyncio.sleep(0.05)
        assert len(laufend) == 2                    # Semaphore(2)
        auftrag.cancel()
        try:
            await auftrag
            raise AssertionError("haette abgebrochen sein muessen")
        except asyncio.CancelledError:
            pass
        assert laufend == []

    asyncio.run(ablauf())


def test_clipstext_transkriber_waehlt_den_client():
    """Eigener Schluessel -> eigener Client (keine Wiederholungen, 30 s lesen,
    5 s verbinden, Standard Groq). Ohne: der von ai.py - aber nur, wenn der zu
    Groq spricht. CLIP_TRANSKRIPT=aus schaltet alles ab."""
    tr = Transkriber()
    with _umgebung(CLIP_WHISPER_KEY="gsk_test", CLIP_WHISPER_URL=None, CLIP_TRANSKRIPT=None):
        client = tr.client()
        try:
            assert client.max_retries == 0
            assert client.timeout.read == 30.0 and client.timeout.connect == 5.0
            assert str(client.base_url).startswith(clipstext.GROQ_URL)
            assert tr.client() is client                      # gemerkt
            assert tr.an() and tr.anbieter() == "Groq"
        finally:
            asyncio.run(tr.schliessen())
        assert tr._eigen is None
    with _umgebung(CLIP_WHISPER_KEY="k", CLIP_WHISPER_URL="https://whisper.example/v1"):
        try:
            assert tr.anbieter() == "eigen"
            assert str(tr.client().base_url).startswith("https://whisper.example/v1")
        finally:
            asyncio.run(tr.schliessen())
    with _umgebung(CLIP_WHISPER_KEY="k", CLIP_TRANSKRIPT="aus"):
        assert tr.client() is None and not tr.an() and tr.anbieter() == ""
    alt = (ai.instance._client, ai.instance._basis_url)
    zeichen = object()
    try:
        with _umgebung(CLIP_WHISPER_KEY=None, CLIP_TRANSKRIPT=None):
            ai.instance._client, ai.instance._basis_url = zeichen, "https://api.groq.com/openai/v1"
            assert tr.client() is zeichen and tr.anbieter() == "Groq"
            ai.instance._basis_url = "http://localhost:11434/v1"
            assert tr.client() is None and not tr.an() and tr.anbieter() == ""
            ai.instance._client, ai.instance._basis_url = None, "https://api.groq.com/openai/v1"
            assert tr.client() is None
    finally:
        ai.instance._client, ai.instance._basis_url = alt
    with _umgebung(CLIP_WHISPER_MODEL=None):
        assert tr.modell() == "whisper-large-v3-turbo"


# =============================================================================
# Kontingent
# =============================================================================
def test_clipstext_kontingent_grenzen():
    """15 Anfragen je Minute, 6000 Audio-Sekunden je Stunde, 1500 Anfragen und
    24000 Sekunden je Tag; jede Anfrage zaehlt mindestens 10 s."""
    uhr = _Uhr()
    k = Kontingent(uhr=uhr)
    assert k.darf([5] * 15)
    k.buchen()                                   # bucht, was darf() geprueft hat
    assert k.heute() == (15, 150)
    assert not k.darf([5])
    uhr.vor(121)
    assert k.darf([5]) and k.darf([])
    # Stunde: 6000 Sekunden.
    uhr, k = _Uhr(), None
    k = Kontingent(uhr=uhr)
    k.buchen([3000, 2990])
    uhr.vor(120)
    assert k.darf([10]) and not k.darf([11])
    uhr.vor(3600)
    assert k.darf([11])
    # Tag: 24000 Sekunden, ueber mehrere Stunden verteilt.
    uhr = _Uhr()
    k = Kontingent(uhr=uhr)
    for _stunde in range(4):
        k.buchen([5000])
        uhr.vor(3700)
    assert k.darf([4000]) and not k.darf([4001])
    # Tag: 1500 Anfragen.
    uhr = _Uhr()
    k = Kontingent(uhr=uhr)
    k.buchen([1] * 1500)
    uhr.vor(7200)
    assert not k.darf([1])
    uhr.vor(86400 - 7200 + 120)
    assert k.darf([1])
    # Eine Zahl statt Liste, Muell als Sekunden: zaehlt als 10 s.
    k = Kontingent(uhr=_Uhr())
    k.buchen(25)
    k.buchen(["quatsch"])
    assert k.heute() == (2, 35)


def test_clipstext_kontingent_sperre_und_uhrsprung():
    """Nach einem 429 die Pause (Retry-After, sonst 60 s, hoechstens ein Tag,
    mindestens 5 s). Springt die Uhr zurueck, zaehlen Eimer 'aus der Zukunft'
    nicht - sonst stuende das Kontingent zwei Stunden lang still."""
    uhr = _Uhr()
    k = Kontingent(uhr=uhr)
    for warten, erwartet in ((120, 120), (None, 60), ("quatsch", 60), (10**9, 86400), (1, 5)):
        k = Kontingent(uhr=uhr)
        k.sperren(warten)
        assert k._gesperrt_bis == uhr.jetzt + erwartet, (warten, k._gesperrt_bis - uhr.jetzt)
    k = Kontingent(uhr=uhr)
    k.buchen([1] * 15)
    assert not k.darf([1])
    uhr.vor(-7200)
    assert k.darf([1])


def test_clipstext_kontingent_speichern_und_laden():
    """als_dict -> JSON -> aus_dict ergibt denselben Stand; Muell wird
    uebergangen, eine gelogene Sperre (Tage in der Zukunft) gekappt, Eimer
    aelter als ein Tag fallen weg."""
    uhr = _Uhr()
    k = Kontingent(uhr=uhr)
    k.buchen([30, 40])
    uhr.vor(600)
    k.buchen([5])
    k.sperren(100)
    daten = json.loads(json.dumps(k.als_dict()))
    neu = Kontingent(uhr=uhr).aus_dict(daten)
    assert neu.heute() == k.heute() == (3, 80) and neu.gesperrt()
    for muell in (None, "x", [], {"eimer": "x"}, {"gesperrt_bis": "bald"},
                  {"eimer": {"a": [1, 2], "5": "x", "6": [None, 3], "7": []}}):
        leer = Kontingent(uhr=uhr).aus_dict(muell)
        assert leer.heute() == (0, 0) and not leer.gesperrt(), muell
    gelogen = Kontingent(uhr=uhr).aus_dict({"gesperrt_bis": uhr.jetzt + 10 * 86400})
    assert gelogen._gesperrt_bis == uhr.jetzt + Kontingent.SPERRE_MAX
    alt = Kontingent(uhr=uhr).aus_dict(
        {"eimer": {str(int((uhr.jetzt - 2 * 86400) // 60)): [5, 50]}})
    assert alt.heute() == (0, 0) and alt.als_dict()["eimer"] == {}


# =============================================================================
# Flos Kommentar
# =============================================================================
def test_clipstext_kommentar_mit_grenze_im_server_kontext():
    """Der Kommentar laeuft im Kontext des Servers, mit der Grenze im
    System-Prompt, als Hintergrundaufruf - und die KI sieht Buchstaben statt
    Namen (auch wo jemand den anderen anspricht), nie private Daten."""
    ki, stapel = _ki_an("„Wow, ein Krisengipfel über verkohlten Teig – Weltpolitik ist nichts "
                        "dagegen.“")
    with stapel:
        out = asyncio.run(clipstext.kommentar(77, _ZEILEN, 60))
    assert out == "Wow, ein Krisengipfel über verkohlten Teig – Weltpolitik ist nichts dagegen."
    prompt, kw = ki.aufrufe[0]
    assert ai.FloAI._GUARDRAIL in kw["system"]
    assert f"Du bist {ai.bot_name(77)}," in kw["system"]
    assert kw["hintergrund"] is True and kw["temperature"] == 1.0 and kw["max_tokens"] == 120
    assert ki.gilden == [77]
    assert "Anna" not in prompt and "Bob" not in prompt
    assert "A: B, du hast die Pizza" in prompt and "B: Die war doch" in prompt
    assert "0171" not in prompt and "1 min" in prompt


def test_clipstext_kommentar_filtert_unbrauchbares():
    """Verweigerung, Hilfsangebot/Notsignal, Hetze, private Daten, leer: kein
    Kommentar. @ wird entfernt, Ueberlanges gekuerzt, eine KI, die haengt
    oder wirft, kostet nur den Kommentar."""
    for antwort in ("I'm sorry, but I can't help with that.",
                    "Ruf die Telefonseelsorge an, echt jetzt.",
                    "Ausländer raus, so sieht's aus.",
                    "Ruf doch 0171 2345678 an, du Clown.",
                    "", None, "   ", "„“"):
        ki, stapel = _ki_an(antwort)
        with stapel:
            assert asyncio.run(clipstext.kommentar(77, _ZEILEN[:2], 30)) is None, antwort
        assert len(ki.aufrufe) == 1
    ki, stapel = _ki_an("@everyone schaut euch das an, peinlich.")
    with stapel:
        assert asyncio.run(clipstext.kommentar(77, _ZEILEN[:2], 30)) == (
            "everyone schaut euch das an, peinlich.")
    ki, stapel = _ki_an("Das ist lang. " * 60)
    with stapel:
        out = asyncio.run(clipstext.kommentar(77, _ZEILEN[:2], 30))
    assert out and len(out) <= clipstext.KOMMENTAR_MAX

    def kaputt(prompt):
        raise RuntimeError("weg")

    async def haengt(prompt):
        await asyncio.sleep(5)
        return "zu spaet"

    for antwort in (kaputt, haengt):
        ki, stapel = _ki_an(antwort)
        with stapel, mock.patch.object(clipstext, "KI_FRIST", 0.1):
            assert asyncio.run(clipstext.kommentar(77, _ZEILEN[:2], 30)) is None


def test_clipstext_kommentar_deutsche_absage_wird_ersatz():
    """Eine deutsche Absage der KI ist kein Kommentar - sie stuende sonst
    oeffentlich unter dem Clip. Ein frecher Satz, der nur aehnlich anfaengt
    ("Tut mir leid fuer eure Ohren"), bleibt einer."""
    for absage in ("Es tut mir leid, aber dabei kann ich nicht helfen.",
                   "Tut mir leid, dazu kann ich nichts sagen.",
                   "Ich kann diesen Clip nicht kommentieren.",
                   "Diesen Inhalt kann ich leider nicht bewerten.",
                   "Leider kann ich keinen Kommentar dazu abgeben.",
                   "Entschuldigung, aber ich werde mich nicht über andere lustig machen.",
                   "Ich möchte mich nicht über Menschen lustig machen.",
                   "Das kann ich so nicht kommentieren.",
                   "Sorry, ich darf dazu nichts sagen.",
                   "Als KI kann ich das nicht beurteilen.",
                   "Ich bin nur ein Sprachmodell und habe keine Meinung."):
        assert clipstext._kommentar_saeubern(absage) is None, absage
        ki, stapel = _ki_an(absage)
        with stapel:
            assert asyncio.run(clipstext.kommentar(77, _ZEILEN[:2], 30)) is None, absage
    for spruch in ("Tut mir leid für eure Ohren, das war Folter.",
                   "Sorry, aber das war der langweiligste Clip aller Zeiten.",
                   "Leider kann ich nicht mehr aufhören zu lachen – über euch.",
                   "Ich kann nicht glauben, dass ihr zehn Minuten über Pizza redet.",
                   "Wow, zehn Minuten Pizza-Debatte und keiner hat Hunger bekommen."):
        assert clipstext._kommentar_saeubern(spruch) == spruch, spruch


def test_clipstext_kommentar_nennt_niemanden():
    """Steht ein Anzeigename im Kommentar (kurze Namen ersetzt der Prompt
    nicht, sonst wuerde aus 'Er' jedes 'er'), gibt es den Ersatz statt eines
    Kommentars, der jemanden oeffentlich beim Namen vorfuehrt."""
    zeilen = [Zeile(1.0, 11, "Jo", "Jo, gib mal die Chips rüber"),
              Zeile(3.0, 22, "Bob", "Hol sie dir doch selbst")]
    for antwort, erwartet in (
            ("Jo will Chips, der andere will Ruhe – Drama in drei Akten.", None),
            ("BOB hat recht, holt euch eure Chips selbst.", None),
            ("Ein Streit um Chips, wie er im Buche steht.",
             "Ein Streit um Chips, wie er im Buche steht."),
            ("Jonas kriegt hier jedenfalls keine Chips.",                   # nur ganze Woerter
             "Jonas kriegt hier jedenfalls keine Chips.")):
        ki, stapel = _ki_an(antwort)
        with stapel:
            assert asyncio.run(clipstext.kommentar(77, zeilen, 30)) == erwartet, antwort
        prompt, _kw = ki.aufrufe[0]
        assert "Bob" not in prompt and "B: Hol sie dir" in prompt


def test_clipstext_kommentar_schweigt_bei_notlage_und_ohne_ki():
    """Kein Spott ueber jemanden am Boden: Notsignal im Clip oder gerade eben
    (basis.notlage_merken) -> kein Kommentar, die KI wird nicht einmal gefragt.
    Dasselbe bei Hetze im Clip, ohne KI und ohne verwertbaren Text."""
    ki, stapel = _ki_an("Spruch.")
    with stapel:
        notlage = _ZEILEN[:2] + [Zeile(8.0, 33, "Cem", "ich will nicht mehr leben")]
        assert asyncio.run(clipstext.kommentar(77, notlage, 30)) is None
        hetze = _ZEILEN[:2] + [Zeile(8.0, 33, "Cem", "Ausländer raus")]
        assert asyncio.run(clipstext.kommentar(77, hetze, 30)) is None
        privat = [Zeile(1.0, 11, "Anna", "meine Nummer ist 0171 2345678")]
        assert asyncio.run(clipstext.kommentar(77, privat, 30)) is None
        assert asyncio.run(clipstext.kommentar(77, [], 30)) is None
        assert asyncio.run(clipstext.kommentar(77, [Zeile(0, 1, "A", "  ")], 30)) is None
        assert asyncio.run(clipstext.kommentar(77, [object()], 30)) is None      # wirft nie
        basis.notlage_merken(22)
        try:
            assert asyncio.run(clipstext.kommentar(77, _ZEILEN[:2], 30)) is None
        finally:
            basis._notlage_seit.pop(22, None)
        assert ki.aufrufe == []
    ki = _KiAttrappe("Spruch.")
    with mock.patch.object(ai, "is_enabled", lambda: False), mock.patch.object(ai, "generate", ki):
        assert asyncio.run(clipstext.kommentar(77, _ZEILEN[:2], 30)) is None
    assert ki.aufrufe == []


def test_clipstext_ersatz_dauer_und_zeit():
    """Der Ersatz-Pool, die Dauer fuer Texte ('1:30 min') und die Zeitmarke im
    Transkript ('m:ss')."""
    for _ in range(20):
        assert clipstext.ersatz_kommentar() in clipstext.ERSATZ_KOMMENTARE
    d = clipstext.dauer_text
    assert [d(30), d(60), d(90), d(300), d(59.6), d(None), d("x")] == [
        "30 s", "1 min", "1:30 min", "5 min", "1 min", "0 s", "0 s"]
    z = clipstext.zeit_text
    assert [z(0), z(75), z(299.9), z(-3), z(None)] == ["0:00", "1:15", "4:59", "0:00", "0:00"]


# =============================================================================
# Bestenliste
# =============================================================================
def test_clipstext_rangliste_ordnung_und_leer():
    """Die meisten 🔥 zuerst, bei Gleichstand der neuere; ohne Flamme oder ohne
    Nachricht zaehlt keiner; hoechstens zehn. Links und Mentions statt Namen.
    Leer: der Spruch mit den Tagen des Zeitraums."""
    eintraege = {
        "aaaaaaaa": {"mid": 101, "cid": 5, "vc": 9, "ts": 1000, "von": 11, "dauer": 60,
                     "sprecher": [11], "stimmen": [1, 2]},
        "bbbbbbbb": {"mid": 102, "cid": 5, "vc": 9, "ts": 500, "von": 12, "dauer": 30,
                     "stimmen": [1, 2, 3, 4, 5]},
        "cccccccc": {"mid": 103, "cid": 5, "vc": 9, "ts": 2000, "von": 13, "dauer": 300,
                     "stimmen": [7, 8]},
        "dddddddd": {"mid": 104, "cid": 5, "vc": 9, "ts": 3000, "von": 14, "dauer": 60,
                     "stimmen": []},
        "eeeeeeee": {"cid": 5, "ts": 4000, "stimmen": [1]},
        "ffffffff": "kaputt",
    }
    def brett(liste, zeitraum):
        return clipstext.rangliste_embed(liste, zeitraum, 77, jetzt=5000)

    embed = brett(list(eintraege.items()), "woche")
    assert isinstance(embed, discord.Embed) and embed.title == "🔥 Clips der Woche"
    zeilen = embed.description.split("\n")
    assert len(zeilen) == 3
    assert zeilen[0].startswith("🥇 **5× 🔥**") and "channels/77/5/102" in zeilen[0]
    assert "Clip · 30 s" in zeilen[0] and "<#9>" in zeilen[0] and "<@12>" in zeilen[0]
    assert "<t:500:R>" in zeilen[0]
    assert zeilen[1].startswith("🥈") and "/103)" in zeilen[1] and "Clip · 5 min" in zeilen[1]
    assert zeilen[2].startswith("🥉") and "/101)" in zeilen[2]
    # Auch als blosse Liste von Eintraegen; der Titel folgt dem Zeitraum.
    assert brett(list(eintraege.values()), "monat").title == "🔥 Clips des Monats"
    for zeitraum in ("alle", "top", 365):
        assert "Jahres" in brett([], zeitraum).title
    assert brett([], 30).title == "🔥 Clips des Monats"
    for zeitraum, tage in (("woche", 7), ("monat", 30), ("alle", 365), ("quatsch", 7)):
        leer = brett([eintraege["dddddddd"]], zeitraum)
        assert leer.description == clipstext.BRETT_LEER.format(tage=tage), zeitraum
    viele = [{"mid": 200 + i, "cid": 5, "ts": 1 + i, "von": 1, "dauer": 30, "stimmen": [1]}
             for i in range(12)]
    zeilen = brett(viele, "woche").description.split("\n")
    assert len(zeilen) == 10 and zeilen[3].startswith("**4.**")
    assert "/211)" in zeilen[0]                                   # neuester zuerst



def test_clipstext_rangliste_nur_im_zeitraum():
    """Die Woche ist die Woche: Clips, die aelter sind als der Zeitraum, zaehlen
    nicht - auch wenn clips den ganzen Index reicht. Ohne Zeitstempel zaehlt
    ein Clip nie; ohne 'jetzt' gilt die Wanduhr."""
    jetzt = 1_800_000_000
    tag = 86400

    def clip(mid, alter, flammen):
        return {"mid": mid, "cid": 5, "ts": jetzt - alter, "von": 1, "dauer": 30,
                "stimmen": list(range(flammen))}

    eintraege = [clip(301, 2 * tag, 1), clip(302, 10 * tag, 5), clip(303, 40 * tag, 9),
                 clip(304, 400 * tag, 20), {"mid": 305, "cid": 5, "stimmen": [1, 2, 3]}]

    def mids(zeitraum, **kw):
        embed = clipstext.rangliste_embed(eintraege, zeitraum, 77, **kw)
        return re.findall(r"/5/([0-9]+)\)", embed.description or "")

    assert mids("woche", jetzt=jetzt) == ["301"]
    assert mids("monat", jetzt=jetzt) == ["302", "301"]
    assert mids("alle", jetzt=jetzt) == ["303", "302", "301"]
    assert mids(14, jetzt=jetzt) == ["302", "301"]
    leer = clipstext.rangliste_embed(eintraege[1:], "woche", 77, jetzt=jetzt)
    assert leer.description == clipstext.BRETT_LEER.format(tage=7)
    with mock.patch.object(clipstext.time, "time", lambda: jetzt + 6 * tag):
        assert mids("woche") == []
        assert mids("monat") == ["302", "301"]


if __name__ == "__main__":
    run(globals())
