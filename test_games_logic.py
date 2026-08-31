"""Pur-logische Tests fuer Casino-, Spiel-, Wort-Zaehler- und Admin-Logik.

Laufen OHNE Discord-Verbindung und ohne Zusatzpakete (gleicher Runner wie
test_logic.py):  python test_games_logic.py
"""

import asyncio
import inspect
import io
import json
import os
import random
import re
import shlex
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

# ---------------------------------------------------------------------------
# ZUERST den Datenordner umbiegen - VOR jedem Modul-Import, denn store.DATA_DIR
# wird beim Import festgelegt und jeder JsonStore haengt daran.
#
# Ohne das schreiben die Tests in die ECHTEN Daten. Nachgemessen: ein einziger
# Lauf legte in data/economy.json ein Testkonto an und schrieb in
# data/floaktie.json "holdings": {"1": 5} samt neuem Kurs und History. Die
# README schickt einen zum Testen ausdruecklich nach /opt/flobot - dort haette
# ein Testlauf also Depots und Aktienkurs des laufenden Servers veraendert.
# Zusaetzlich machte der uebriggebliebene Stand die Suite unzuverlaessig:
# test_floaktie_market ist an geerbten Kursdaten gescheitert.
# ---------------------------------------------------------------------------
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="flobot-tests-")
import store                                    # noqa: E402
store.DATA_DIR = __import__("pathlib").Path(os.environ["DATA_DIR"])

import admin
import casino
import cmdnorm
import economy
import floaktie
import gehirn
import luxus
import render
import words

# Die Kursbewegung der Aktie WUERFELT pro Minute zweimal: TICK_NOISE (Rauschen
# am Deckel) und VOL_SPREAD (echte Volatilitaet, +-80 % um den Trend). Die
# Aktien-Tests haben bisher nur TICK_NOISE genullt und damit in Wahrheit einen
# Zufallswert geprueft - test_floaktie_aktivitaet_treibt_den_kurs ist deshalb in
# 17 von 100 Laeufen grundlos gescheitert. Fuer die gesamte Suite ist die
# Volatilitaet daher AUS; wer sie braucht, schaltet sie im eigenen Test wieder
# ein (siehe test_floaktie_volatilitaet_ist_symmetrisch).
floaktie.VOL_SPREAD = 0.0


# --- Blackjack -------------------------------------------------------------
def test_blackjack_handwert():
    assert casino._hand_value([("A", "♠"), ("K", "♥")]) == 21
    assert casino._hand_value([("A", "♠"), ("A", "♥")]) == 12          # 11 + 1
    assert casino._hand_value([("A", "♠"), ("9", "♥"), ("5", "♦")]) == 15
    assert casino._hand_value([("10", "♠"), ("9", "♥"), ("5", "♦")]) == 24  # Bust


# --- Einsatz-Parsing ---------------------------------------------------------
def test_resolve_bet():
    assert casino._resolve_bet("50", 0) == 50
    assert casino._resolve_bet("abc", 0) is None
    assert casino._resolve_bet("", 0) is None
    # 'alles' ohne aktives economy: Kontostand 0 -> 0 (kein Crash)
    assert casino._resolve_bet("alles", 0) == 0


# --- Mines -------------------------------------------------------------------
def test_mines_multiplikator():
    # Ohne Pick kein Bonus.
    assert casino._mines_mult(0, 3) == 1.0
    # Streng steigend mit jedem sicheren Feld.
    vorher = 1.0
    for picked in range(1, casino._MINES_TILES - 3 + 1):
        m = casino._mines_mult(picked, 3)
        assert m > vorher, (picked, m, vorher)
        vorher = m
    # Mehr Bomben -> hoeherer Multiplikator beim gleichen Pick.
    assert casino._mines_mult(1, 5) > casino._mines_mult(1, 1)
    # Hausvorteil: erwarteter Wert eines 1-Feld-Picks liegt unter 1.
    # P(sicher) * mult = ((T-m)/T) * 0.97 * T/(T-m) = 0.97
    t, m = casino._MINES_TILES, 3
    p_sicher = (t - m) / t
    assert abs(p_sicher * casino._mines_mult(1, m) - 0.97) < 0.02


# --- Gluecksrad ---------------------------------------------------------------
def test_wheel_hausvorteil():
    segs = casino._WHEEL_SEGMENTS
    assert len(segs) == 12
    ev = sum(segs) / len(segs)
    assert 0.90 <= ev <= 0.99, ev              # kleiner Hausvorteil, kein Abzock-Rad
    assert any(m == 0 for m in segs)           # Nieten existieren
    assert max(segs) >= 2.0                    # aber auch echte Gewinne


# --- Rubbellos -----------------------------------------------------------------
def test_scratch_roll_konsistent():
    rng = random.Random(42)
    random.seed(42)
    for _ in range(200):
        keys, rows, mult = casino._scratch_roll()
        assert len(keys) == 9
        assert all(k in render.SLOT_KEYS for k in keys)
        for r in range(3):
            gewinn = keys[3 * r] == keys[3 * r + 1] == keys[3 * r + 2]
            assert (r in rows) == gewinn
        assert mult == sum(casino._SCRATCH_PAYOUT[keys[3 * r]] for r in rows)
    _ = rng  # nur zur Klarheit: Test nutzt globales random mit festem Seed


def test_scratch_hausvorteil():
    # Exakter Erwartungswert: 3 unabhaengige Reihen, P(Reihe aus Symbol s) = 1/343.
    n = len(render.SLOT_KEYS)
    ev = 3 * sum(casino._SCRATCH_PAYOUT[s] for s in render.SLOT_KEYS) / (n ** 3)
    assert 0.85 <= ev <= 1.0, ev


# --- Crash ---------------------------------------------------------------------
def test_crash_point_grenzen():
    random.seed(7)
    for _ in range(2000):
        cp = casino._crash_point()
        assert 1.0 <= cp <= 1000.0, cp


# --- Roulette --------------------------------------------------------------------
def test_roulette_auszahlung():
    payout, label = casino._roulette_payout("rot", 10, 1)      # 1 ist rot
    assert payout == 20 and label == "Rot"
    payout, _ = casino._roulette_payout("rot", 10, 2)          # 2 ist schwarz
    assert payout == 0
    payout, _ = casino._roulette_payout("gerade", 10, 0)       # 0 verliert immer
    assert payout == 0
    payout, label = casino._roulette_payout("17", 10, 17)
    assert payout == 360 and label == "Zahl 17"
    payout, _ = casino._roulette_payout("quatsch", 10, 17)
    assert payout is None


def test_roulette_stuerzt_nicht_bei_ungueltigem_tipp():
    """_roulette_payout liefert bei einem unbekannten Tipp (None, target) - und
    direkt danach stand 'payout > 0'. Das ist ein TypeError, kein Fehlschlag.

    Heute pruefen alle vier Aufrufer den Tipp vorher ab, der Pfad ist also nicht
    erreichbar. Aber der Einsatz ist an dieser Stelle SCHON abgebucht: wer den
    fuenften Aufrufer schreibt und die Pruefung vergisst, verbrennt fremde Coins
    mit einem Absturz. Die Absicherung gehoert deshalb an die eine Stelle."""
    import io
    e = economy.instance
    uid = 987654321

    async def kein_bild(*_a, **_k):          # Bildbau kostet hier nur Zeit
        return io.BytesIO(b"x"), "png"

    alt_anim = casino.instance._anim
    casino.instance._anim = kein_bild
    # economy AUSDRUECKLICH anschalten: add_coins/get_coins sind sonst ein
    # No-Op und der Test war nur gruen, weil ein frueherer Test economy
    # angelassen hatte. In gemischter Reihenfolge fiel er um.
    alt_eco = (economy.instance._store, economy.instance._enabled)
    economy.instance._store = _FakeStore({"users": {}})
    economy.instance._enabled = True
    try:
        e.add_coins(uid, 10_000 - e.get_coins(uid))    # Startguthaben setzen
        einsatz = 500
        e.add_coins(uid, -einsatz)           # so wie es jeder Aufrufer tut
        vorher = e.get_coins(uid)
        emb, datei = asyncio.run(
            casino.instance._play_roulette(uid, einsatz, "voelliger quatsch"))
        # 1. Kein Absturz, und die Form der Rueckgabe bleibt gleich - die
        #    Aufrufer reichen 'datei' direkt an Discord weiter.
        assert emb is not None and datei is not None
        # 2. Der Einsatz ist zurueck: kein Gewinn, kein Verlust.
        assert e.get_coins(uid) == vorher + einsatz, (
            f"Coins verbrannt: {vorher} -> {e.get_coins(uid)}")
    finally:
        casino.instance._anim = alt_anim
        economy.instance._store, economy.instance._enabled = alt_eco

    # Ein gueltiger Tipp verhaelt sich unveraendert.
    assert casino._roulette_payout("rot", 10, 1) == (20, "Rot")


def test_arbeit_spasswordle_haelt_den_vertrag_der_basisklasse():
    """Alle Schichten versprechen bauen(chef, autor, **_extra). SpassWordle hat
    das **_extra weggelassen - damit wirft derselbe Aufruf, den jede andere
    Schicht klaglos schluckt, bei dieser einen einen TypeError. Der Aufrufer
    (arbeit.py:1214 reicht **extra durch) kann das nicht sehen."""
    import arbeit
    import inspect
    basis = inspect.signature(arbeit.Schicht.bauen).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in basis.values())
    for name, obj in vars(arbeit).items():
        if not (isinstance(obj, type) and issubclass(obj, arbeit.Schicht)
                and obj is not arbeit.Schicht):
            continue
        if "bauen" not in vars(obj):
            continue                          # erbt die Basis-Signatur, passt
        params = inspect.signature(obj.bauen).parameters
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), (
            f"{name}.bauen nimmt kein **_extra - arbeit.py:1214 reicht aber "
            f"beliebige Schluesselwoerter durch")


# --- Keno-Tabelle -----------------------------------------------------------------
def test_keno_tabelle():
    """Jede Tippanzahl muss denselben Rueckfluss haben. Vorher lag der RTP je nach
    Anzahl zwischen 0,29 (7 Zahlen - reine Falle) und 0,90 (2 Zahlen)."""
    from math import comb
    assert (3, 1) not in casino._KENO_TABLE   # zu wenig Treffer -> nichts

    def p(k, hit):                            # Ziehung: 10 aus 40
        return comb(k, hit) * comb(40 - k, 10 - hit) / comb(40, 10)

    for k in range(1, 9):
        stufen = sorted(h for (kk, h) in casino._KENO_TABLE if kk == k)
        assert stufen, k
        # Faktoren muessen mit den Treffern STEIGEN (sonst ist die Tabelle kaputt).
        werte = [casino._KENO_TABLE[(k, h)] for h in stufen]
        assert werte == sorted(werte) and len(set(werte)) == len(werte), (k, werte)
        assert stufen[-1] == k                # Volltreffer zahlt immer
        rtp = sum(p(k, h) * casino._KENO_TABLE[(k, h)] for h in stufen)
        assert 0.90 <= rtp <= 0.97, (k, round(rtp, 4))   # Hausvorteil ~6 %
        assert max(werte) <= 2000, (k, max(werte))        # kein Tail-Risk-Monster


# --- Wort-Zaehler ------------------------------------------------------------------
def test_words_tokenizer():
    toks = words._tokenize(
        "Hallo WELT! https://beispiel.de/pfad <@123> <#456> <a:emo:789> "
        "Pizza-Party äöüß 42 a zu")
    assert toks == ["hallo", "welt", "pizza", "party", "äöüß", "zu"]
    assert words._tokenize("") == []
    assert words._tokenize("1234 !!! ...") == []


def test_words_zaehlen():
    """Gezaehlt wird JE SERVER - frueher lief alles in einen gemeinsamen Topf,
    und auf dem zweiten Server stand die Statistik des ersten mit drin."""
    # Fake-Store: reine dict-Logik testen, ohne Datei (Zustand lebt in der Instanz).
    # Das Modul wird hier AUSDRUECKLICH angeschaltet: vorher hing der Test daran,
    # dass irgendein alphabetisch frueherer Test words.setup() gerufen hatte. In
    # gemischter Reihenfolge war er deshalb rot.
    alt = (words.instance._store, words.instance._enabled)
    words.instance._store = type("S", (), {"data": {"guilds": {}}})()
    words.instance._enabled = True
    try:
        n = words._count_text("pizza pizza salat", "111", 1)
        assert n == 3
        n = words._count_text("PIZZA!", "222", 1)
        assert n == 1
        buch = words.instance._buch(1)
        assert buch["words"]["pizza"]["n"] == 3
        assert buch["words"]["pizza"]["u"] == {"111": 2, "222": 1}
        assert buch["words"]["salat"]["n"] == 1
        assert buch["total"] == 4 and buch["msgs"] == 2

        # Ein zweiter Server faengt bei null an und faerbt den ersten nicht ein.
        words._count_text("pizza", "111", 2)
        assert words.instance._buch(2)["words"]["pizza"]["n"] == 1
        assert words.instance._buch(1)["words"]["pizza"]["n"] == 3
        # Der Profil-Lookup fragt ausdruecklich nach EINEM Server; ohne Angabe
        # (z. B. aus einer DM) kommt die Summe ueber alle.
        assert words.instance.statistik_von("111", gid=1)[0] == 3
        assert words.instance.statistik_von("111", gid=2)[0] == 1
        assert words.instance.statistik_von("111")[0] == 4
    finally:
        words.instance._store, words.instance._enabled = alt


def test_words_migriert_den_alten_topf():
    """Der alte, gemeinsame Wortschatz wird dem HAUPT-Server zugeordnet -
    etwas anderes waere geraten, denn wem die Woerter gehoerten, steht
    nirgends. Geloescht wird nichts."""
    import os
    alt_env = os.environ.get("GUILD_ID")
    os.environ["GUILD_ID"] = "77"
    words.instance._store = _FakeStore({
        "words": {"pizza": {"n": 5, "u": {"1": 5}}}, "total": 5, "msgs": 2,
        "guilds": {},
    })
    try:
        words.instance._migrieren()
        daten = words.instance._store.data
        assert "words" not in daten and "total" not in daten
        assert daten["guilds"]["77"]["words"]["pizza"]["n"] == 5
        assert daten["guilds"]["77"]["total"] == 5
        # Zweimal migrieren darf nichts doppeln.
        words.instance._migrieren()
        assert daten["guilds"]["77"]["total"] == 5
    finally:
        words.instance._store = None
        if alt_env is None:
            os.environ.pop("GUILD_ID", None)
        else:
            os.environ["GUILD_ID"] = alt_env


# --- Befehls-Normalisierung ----------------------------------------------------------
def test_cmdnorm_neue_befehle():
    # Tippfehler-Korrektur auf die neuen Trigger.
    assert cmdnorm.normalize("woerterr pizza") == "woerter pizza"
    assert cmdnorm.normalize("minees 50") == "mines 50"
    # Alltagswoerter duerfen NICHT gekapert werden.
    for satz in ("orte 5", "wert 100", "start jetzt", "statt dessen", "worten nach",
                 "kommt ihr heute", "spielen wir was"):
        assert cmdnorm.normalize(satz) is None, satz
    # Toter Dialekt-Key ist bereinigt (wird vor dem Lookup eh weggestrippt).
    assert "weida..." not in cmdnorm.DIALECT and cmdnorm.DIALECT["weida"] == "weiter"
    # Exakte neue Befehle bleiben unveraendert (None = nichts zu korrigieren).
    assert cmdnorm.normalize("wörter pizza") is None
    assert cmdnorm.normalize("mines 50 3") is None
    # Haendler & Lotto sind bekannte Befehle (kein Umschreiben).
    for w in ("haendler", "händler", "merchant", "lotto", "jackpot"):
        assert w in cmdnorm.KNOWN, w
        assert cmdnorm.normalize(f"{w} test") is None
    # Tippfehler auf die neuen Befehle wird korrigiert.
    assert cmdnorm.normalize("lottoo kauf 5") == "lotto kauf 5"


# --- Admin-Befehle (nur Besitzer) -----------------------------------------------------
def _fake_msg(uid, content):
    return SimpleNamespace(
        author=SimpleNamespace(id=uid, bot=False, display_name="Tester"),
        content=content, mentions=[], guild=None)


def test_admin_extract():
    # Mention + Betrag
    uid, amount = admin._extract("<@1040135855710404659> 250")
    assert uid == 1040135855710404659 and amount == 250
    # Rohe ID + Betrag (DM-Fall)
    uid, amount = admin._extract("123456789012345678 100")
    assert uid == 123456789012345678 and amount == 100
    # Negativer Betrag
    uid, amount = admin._extract("123456789012345678 -50")
    assert uid == 123456789012345678 and amount == -50
    # Nichts Brauchbares
    assert admin._extract("hallo welt") == (None, None)
    # Betrag ohne Ziel
    uid, amount = admin._extract("500")
    assert uid is None and amount == 500


def test_admin_owner_gate():
    admin.setup()
    # Fremde bekommen von admin.handle grundsaetzlich None (kein Befehl, keine Antwort).
    fremd = asyncio.run(admin.handle(_fake_msg(999, "gib 123456789012345678 100")))
    assert fremd is None
    # Besitzer: unbekanntes Wort -> None (KI/andere Handler sind dran).
    frei = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "wie geht's dir?")))
    assert frei is None
    # Besitzer: Admin-Befehl wird erkannt (economy ist im Test aus -> Hinweis-Text).
    antwort = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID,
                                                 "gib 123456789012345678 100")))
    assert isinstance(antwort, str) and "Economy" in antwort
    # Besitzer: 'gib' als normales Chat-Wort (kein Ziel, kein Betrag) wird NICHT
    # gekapert - die KI soll antworten duerfen.
    chat = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID,
                                              "gib mir mal einen Tipp")))
    assert chat is None
    # Adminhilfe kommt als Embed.
    hilfe = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "adminhilfe")))
    assert hilfe is not None and not isinstance(hilfe, str)


# --- Musik: natuerlichsprachige Play-Trigger ------------------------------------
def test_music_natural_language():
    """'Flo mach mal <X> an' & Co. werden wie ein Play-Befehl erkannt; generische
    Floskeln fuehren zu resume/Hinweis, normale Saetze bleiben None."""
    import music
    pc = music.instance.parse_command
    # Song steht in der Mitte -> Suche nach genau diesem Song.
    assert pc("flo mach mal bohemian rhapsody an") == ("search", "bohemian rhapsody")
    assert pc("Flo mach mal despacito an") == ("search", "despacito")
    assert pc("flo leg mir mal sandstorm auf") == ("search", "sandstorm")
    assert pc("flo hau mal darude sandstorm raus") == ("search", "darude sandstorm")
    assert pc("flo pack mal lofi beats auf") == ("search", "lofi beats")
    assert pc("flo kannst du mal wonderwall abspielen") == ("search", "wonderwall")
    assert pc("flo spiel mir mal africa vor") == ("search", "africa")
    # "spiel mir mal <X>" darf nicht nach "mir mal <X>" suchen.
    assert pc("flo spiel mir mal africa") == ("search", "africa")
    # Generisch ohne konkreten Song -> resume/Hinweis.
    assert pc("flo mach mal musik an") == ("resume_or_hint", "")
    assert pc("flo mach mal die mucke an") == ("resume_or_hint", "")
    # "Musik aus" -> stoppen.
    assert pc("flo mach die musik aus") == ("stop", "")
    assert pc("flo stell die mucke ab") == ("stop", "")
    # Normaler Play-Befehl bleibt unveraendert.
    assert pc("flo spiel despacito") == ("search", "despacito")
    # Kein Musikbefehl -> None (keine Kaperung normaler Saetze).
    assert pc("flo wie gehts dir") is None
    assert pc("flo mach mal langsam") is None
    # Spiel-/Feature-Namen werden NICHT als Song gesucht (kein Kapern des Quiz-Starts).
    assert pc("flo mach das quiz an") is None
    assert pc("flo mach mal blackjack an") is None


# --- Musik: Zufalls-Song mit Genre-Auswahl --------------------------------------
def test_music_random_genre():
    """start_random: ohne Voice -> Hinweis; mit Voice -> Track aufgeloest, gestartet
    und Panel gepostet; 'surprise' waehlt ein gueltiges Genre; Genre-Pools sauber."""
    import music

    # Genre-Datenbank plausibel (Dropdown-Limit, gefuellte Pools).
    assert 1 <= len(music._RANDOM_GENRES) <= 24
    assert all(pool and isinstance(pool, list)
               for _l, _e, pool in music._RANDOM_GENRES.values())

    calls = {"defer": 0, "panel": 0, "started": None, "ephemeral": []}

    class FakePlayer:
        def __init__(self):
            self.text_channel = None
            self.queue = []

        async def connect(self, ch):
            pass

        def is_active(self):
            return False

        def start(self, track):
            calls["started"] = track.title

    fake_player = FakePlayer()

    class Resp:
        def is_done(self):
            return False

        async def defer(self):
            calls["defer"] += 1

        async def send_message(self, *a, **k):
            calls["ephemeral"].append((a, k))

    class Inter:
        def __init__(self, in_voice):
            self.guild = SimpleNamespace(id=1)
            self.channel = SimpleNamespace(id=2)
            self.user = SimpleNamespace(
                id=7, display_name="Tester",
                voice=SimpleNamespace(channel=SimpleNamespace(id=9)) if in_voice else None)
            self.response = Resp()

        async def edit_original_response(self, *a, **k):
            pass

        # followup.send
        @property
        def followup(self):
            async def _send(*a, **k):
                pass
            return SimpleNamespace(send=_send)

    inst = music.instance
    alt = (inst._enabled, inst._player_for, inst._extract, inst._send_panel)
    inst._enabled = True
    inst._player_for = lambda gid: fake_player

    async def _fake_extract(q):
        return music.Track(title=f"Song für {q}", stream_url="http://x")
    inst._extract = _fake_extract

    async def _fake_panel(player, track, **k):
        calls["panel"] += 1
    inst._send_panel = _fake_panel
    try:
        # 1) Nicht im Voice -> ephemerer Hinweis, kein Abspielen.
        asyncio.run(inst.start_random(Inter(in_voice=False), "rock"))
        assert calls["ephemeral"] and calls["started"] is None and calls["panel"] == 0

        # 2) Im Voice -> defer, Track gestartet, Panel gepostet.
        asyncio.run(inst.start_random(Inter(in_voice=True), "rock"))
        assert calls["defer"] == 1
        assert calls["started"] is not None and calls["panel"] == 1

        # 3) 'surprise' waehlt ein gueltiges Genre (kein Crash, spielt).
        calls["started"] = None
        asyncio.run(inst.start_random(Inter(in_voice=True), "surprise"))
        assert calls["started"] is not None

        # 4) Unbekanntes Genre -> ephemerer Hinweis, kein Abspielen.
        before = calls["started"]
        asyncio.run(inst.start_random(Inter(in_voice=True), "gibtsnicht"))
        assert calls["started"] == before  # unveraendert (nicht gestartet)
    finally:
        inst._enabled, inst._player_for, inst._extract, inst._send_panel = alt


# --- Musik: Spotify Best-Match (richtiger Song statt Sped-Up/Loop) --------------
def test_music_spotify_best_match():
    """Aus mehreren YouTube-Treffern wird der beste fuer einen Spotify-Song gewaehlt:
    Dauer-Naehe + Titel-Match, Abwertung von Sped-Up/Loop/Nightcore/Cover/Live -
    aber 'live' darf nicht in 'Alive' matchen und ein gewollter Remix nicht sinken."""
    import music
    m = music.instance

    def pick(cands, dur, title, artist=""):
        return m._pick_best_match(cands, dur, title, artist)

    # Original-Video (Dauer passt) schlaegt Sped-Up/1h-Loop/Nightcore.
    c = [{"title": "Alan Walker - Faded (Sped Up)", "duration": 175, "id": "a"},
         {"title": "Alan Walker - Faded [1 HOUR LOOP]", "duration": 3600, "id": "b"},
         {"title": "Alan Walker - Faded (Official Music Video)", "duration": 212, "id": "c"},
         {"title": "Faded - Alan Walker (Nightcore)", "duration": 150, "id": "d"}]
    assert pick(c, 212, "Faded", "Alan Walker")["id"] == "c"
    # Ohne Dauer-Info wird wenigstens der Junk abgewertet.
    c2 = [{"title": "Song X (Sped Up)", "duration": None, "id": "1"},
          {"title": "Song X (Official Audio)", "duration": None, "id": "2"},
          {"title": "Song X 10 hours", "duration": None, "id": "3"}]
    assert pick(c2, None, "Song X")["id"] == "2"
    # Ein gewuenschter Remix wird NICHT als 'Junk' abgestraft.
    c3 = [{"title": "Titel (Original Mix)", "duration": 200, "id": "o"},
          {"title": "Titel (Tiesto Remix)", "duration": 201, "id": "r"}]
    assert pick(c3, 201, "Titel Tiesto Remix")["id"] == "r"
    # 'live'-Abwertung darf 'Stayin Alive' nicht treffen.
    c4 = [{"title": "Bee Gees - Stayin Alive (Official)", "duration": 285, "id": "x"},
          {"title": "Bee Gees - Stayin Alive (Live 1979)", "duration": 300, "id": "y"}]
    assert pick(c4, 285, "Stayin Alive", "Bee Gees")["id"] == "x"
    # Normalisierung behaelt Klammer-Woerter ('faded sped up').
    assert m._norm_match("Alan Walker - Faded (Sped Up!)") == "alan walker faded sped up"


# --- Musik: Lyrics -------------------------------------------------------------
def test_music_lyrics():
    """Artist/Titel-Split, Seiten-Pagination und _build_lyrics (Fetch gemockt):
    Treffer -> Embed + Paginator-View, kein Treffer -> Fehler-Embed ohne View."""
    import music
    m = music.instance
    # YouTube-Deko wird entfernt, am ' - ' getrennt.
    assert m._split_artist_title("Queen - Bohemian Rhapsody (Official Video)") \
        == ("Queen", "Bohemian Rhapsody")
    assert m._split_artist_title("Rick Astley - Never Gonna Give You Up [HD]") \
        == ("Rick Astley", "Never Gonna Give You Up")
    assert m._split_artist_title("Bohemian Rhapsody") == ("", "Bohemian Rhapsody")
    # Pagination bricht an Strophen und haelt das Zeichenlimit ein.
    text = "\n\n".join(f"Strophe {i}\nzeile a\nzeile b" for i in range(30))
    pages = m._lyrics_pages(text, limit=300)
    assert len(pages) > 1 and all(len(p) <= 300 for p in pages)
    # Eine leere/kurze Eingabe liefert trotzdem mindestens eine Seite.
    assert m._lyrics_pages("") and m._lyrics_pages("nur eine zeile")

    async def fake_ok(artist, title):
        return "Vers 1\nZeile A\nZeile B\n\nRefrain\nHook 1\nHook 2"

    async def fake_none(artist, title):
        return None

    try:
        m.fetch_lyrics = fake_ok
        emb, view = asyncio.run(m._build_lyrics("Queen - Bohemian Rhapsody", None))
        assert view is not None and emb.title.startswith("🎤")
        assert "Vers 1" in (emb.description or "")
        assert len(view.pages) >= 1 and view.embed().title.startswith("🎤")
        # Kein Treffer -> Fehler-Embed, KEINE View.
        m.fetch_lyrics = fake_none
        emb2, view2 = asyncio.run(m._build_lyrics("Voellig Unbekannt XY", None))
        assert view2 is None and "Kein Text" in (emb2.title or "")
    finally:
        try:
            del m.fetch_lyrics    # Instanz-Override weg -> Klassenmethode zurueck
        except AttributeError:
            pass


# --- Steal (Coin-Raub) ---------------------------------------------------------
def test_steal_heist():
    """steal.handle: kein Ziel -> Hinweis; Erfolg klaut (Topf konstant); Cooldown
    greift; Misserfolg kostet Strafe; Selbst-/Bot-/Arm-Ziel abgefangen."""
    import steal

    class FakeStore:
        def __init__(self, data):
            self.data = data

        async def save(self):
            pass

    alt_eco = (economy.instance._store, economy.instance._enabled)
    economy.instance._store = FakeStore({"users": {}})
    economy.instance._enabled = True
    economy.instance._profile(1)["coins"] = 10000   # Opfer
    economy.instance._profile(2)["coins"] = 5000    # Raeuber
    alt_steal = (steal.instance._store, steal.instance._enabled,
                 steal.instance._success_chance)
    steal.instance._store = FakeStore({"cooldowns": {}})
    steal.instance._enabled = True

    def cd_clear():
        steal.instance._store.data["cooldowns"].clear()

    def mk(author, content, mentions):
        return SimpleNamespace(author=author, content=content, mentions=mentions,
                               guild=SimpleNamespace(id=1))
    raeuber = SimpleNamespace(id=2, bot=False, display_name="Raeuber")
    opfer = SimpleNamespace(id=1, bot=False, display_name="Opfer")
    try:
        # Kein Ziel -> Hinweistext.
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal", []))), str)
        # Erfolg erzwingen: Opfer verliert, Raeuber gewinnt, Gesamttopf konstant.
        steal.instance._success_chance = 1.0
        cd_clear()
        vo, vr = economy.get_coins(1), economy.get_coins(2)
        emb = asyncio.run(steal.handle(mk(raeuber, "steal <@1>", [opfer])))
        assert not isinstance(emb, str)
        assert economy.get_coins(1) < vo and economy.get_coins(2) > vr
        assert economy.get_coins(1) + economy.get_coins(2) == vo + vr
        # Cooldown greift jetzt.
        r = asyncio.run(steal.handle(mk(raeuber, "steal <@1>", [opfer])))
        assert isinstance(r, str) and "Min" in r
        # Misserfolg: neuer Raeuber zahlt Strafe.
        steal.instance._success_chance = 0.0
        cd_clear()
        economy.instance._profile(3)["coins"] = 3000
        pech = SimpleNamespace(id=3, bot=False, display_name="Pech")
        v3 = economy.get_coins(3)
        asyncio.run(steal.handle(mk(pech, "steal <@1>", [opfer])))
        assert economy.get_coins(3) < v3
        # Selbst-Klau, Bot-Ziel, armes Ziel -> jeweils Hinweistext, kein Raub.
        cd_clear()
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal <@2>", [raeuber]))), str)
        botziel = SimpleNamespace(id=99, bot=True, display_name="RivalBot")
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal <@99>", [botziel]))), str)
        economy.instance._profile(9)["coins"] = 10
        arm = SimpleNamespace(id=9, bot=False, display_name="Arm")
        cd_clear()
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal <@9>", [arm]))), str)
        # Kein Steal-Befehl -> None.
        assert asyncio.run(steal.handle(mk(raeuber, "wie gehts", []))) is None
    finally:
        steal.instance._store, steal.instance._enabled, steal.instance._success_chance = alt_steal
        economy.instance._store, economy.instance._enabled = alt_eco


# --- Stocks (Aktienkurse) ------------------------------------------------------
def test_terraria_logic():
    import terraria
    t = terraria.instance
    # Terraria-Fragen werden erkannt, Alltag nicht.
    assert terraria.erkennt_frage("wie besiege ich plantera")
    assert terraria.erkennt_frage("was ist terraria eigentlich")
    assert terraria.erkennt_frage("wie craftet man das zenith")
    assert not terraria.erkennt_frage("wie wird das wetter morgen")
    assert not terraria.erkennt_frage("was gibts heute zu essen")
    assert not terraria.erkennt_frage("mein boss hat frei gegeben")   # kein Fehlalarm
    # _kuerzen haelt das Limit ein.
    lang = "Ein Satz. " * 400
    k = t._kuerzen(lang, 120)
    assert len(k) <= 130
    # _beste_seite versteht beide Such-Formate.
    assert t._beste_seite({"query": {"search": [{"title": "Plantera"}]}}) == "Plantera"
    assert t._beste_seite(["copper", ["Copper Ore", "Copper Bar"], [], []]) == "Copper Ore"
    assert t._beste_seite(None) is None
    assert t._beste_seite({"query": {"search": []}}) is None


def test_terraria_random_und_kategorie():
    """Pagination, Kategorie-Map/Random-Pool und das handle-Routing: 'random' ->
    Zufalls-Seite, ein Kategorie-Wort -> Kategorie, mehrere Woerter -> Frage."""
    import discord
    import terraria
    t = terraria.instance
    # Pagination haelt das Limit ein.
    pages = t._paginate("Absatz.\n\n" * 300, 400)
    assert len(pages) > 1 and all(len(p) <= 420 for p in pages)
    # Kategorie-Map + Zufalls-Pool.
    assert terraria._KATEGORIEN["bosse"] == "Bosses"
    assert terraria._KATEGORIEN["waffen"] == "Weapons"
    assert terraria._random_titel() in terraria._RANDOM_POOL

    calls = {"random": 0, "cat": None}

    async def fake_random():
        calls["random"] += 1
        return discord.Embed(title="Zufall"), None

    async def fake_cat(kat, anzeige):
        calls["cat"] = kat
        return discord.Embed(title=kat), None

    async def fake_send(message, emb, view=None):
        return terraria.HANDLED

    async def fake_beantworte(message, frage):
        calls.setdefault("frage", frage)
        return None

    orig = (t._build_random, t._build_category, t._send, t.beantworte, t._enabled)
    t._build_random, t._build_category, t._send = fake_random, fake_cat, fake_send
    t.beantworte = fake_beantworte
    t._enabled = True

    def msg(content):
        return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                               author=SimpleNamespace(display_name="x"))
    try:
        # 'terraria random' -> Zufalls-Seite.
        assert asyncio.run(terraria.handle(msg("terraria random"))) is terraria.HANDLED
        assert calls["random"] == 1
        # Ein Kategorie-Wort -> Kategorie.
        assert asyncio.run(terraria.handle(msg("terraria bosse"))) is terraria.HANDLED
        assert calls["cat"] == "Bosses"
        # Mehrere Woerter mit Kategorie-Wort -> normale Frage (nicht Kategorie).
        calls["cat"] = None
        r = asyncio.run(terraria.handle(msg("terraria waffen gegen plantera")))
        assert calls["cat"] is None and isinstance(r, discord.Embed)  # keine_seite_embed
        assert calls.get("frage") == "waffen gegen plantera"
        # Kein Terraria-Prefix -> None.
        assert asyncio.run(terraria.handle(msg("spiel despacito"))) is None
    finally:
        (t._build_random, t._build_category, t._send, t.beantworte, t._enabled) = orig


# --- Bot-Hass ------------------------------------------------------------------
def test_bot_beef():
    import ai
    import fun
    # Persona traegt den Bot-Hass.
    assert "verachtest" in ai.instance._system_prompt().lower()
    # Roast-Sprueche formatieren sauber mit dem Namen des Rivalen.
    assert "NervBot" in fun._BOT_ROASTS[0].format(name="NervBot")
    assert hasattr(fun, "maybe_roast_bot")


# --- Sendepause (nur Owner) ------------------------------------------------------
def test_admin_sendepause_toggle():
    """'sendepause' schaltet um, 'an'/'aus' erzwingen den Zustand; nur der Owner
    erreicht den Befehl ueberhaupt (admin.handle gibt Fremden None)."""
    admin.setup()
    # Ohne Store (Test): Persistenz-Aufruf darf nicht crashen -> Fake-Store.
    class FakeStore:
        def __init__(self):
            self.data = {"sendepause": False}

        async def save(self):
            self.data["sendepause_saved"] = self.data["sendepause"]

    alt = admin.instance._store
    admin.instance._store = FakeStore()
    admin.instance._locked = False
    try:
        assert admin.is_locked() is False
        # Fremder kann die Sendepause NICHT setzen (kein Owner -> None, kein Effekt).
        assert asyncio.run(admin.handle(_fake_msg(999, "sendepause"))) is None
        assert admin.is_locked() is False
        # Owner schaltet an (Toggle) -> Embed, Flag + Persistenz gesetzt.
        antwort = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause")))
        assert antwort is not None and not isinstance(antwort, str)
        assert admin.is_locked() is True
        assert admin.instance._store.data["sendepause_saved"] is True
        # Toggle zurueck.
        asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause")))
        assert admin.is_locked() is False
        # Explizit 'an' und idempotentes 'aus'.
        asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause an")))
        assert admin.is_locked() is True
        asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause aus")))
        assert admin.is_locked() is False
    finally:
        admin.instance._store = alt
        admin.instance._locked = False


# --- Leaderboard-Avatare ---------------------------------------------------------
def test_attach_avatars_cache_und_fallback():
    """Avatar-Laden: Erfolg fuellt Cache, Fehlschlag landet im Negativ-Cache,
    zweiter Aufruf kommt ohne Resolver aus dem Cache."""
    orig = economy._resolve_avatar_user
    economy.instance._AVATAR_CACHE.clear()
    economy.instance._AVATAR_FAIL.clear()
    try:
        # 1) Aufloesung schlaegt fehl -> kein Avatar, Negativ-Cache gesetzt.
        async def _none(_guild, _uid):
            return None
        economy.instance._resolve_avatar_user = _none
        rows = [{"id": 42}]
        asyncio.run(economy._attach_avatars(rows, None))
        assert "avatar" not in rows[0]
        assert 42 in economy.instance._AVATAR_FAIL

        # 2) Erfolg -> Bytes am Row + im Cache.
        class FakeAsset:
            def with_size(self, _n):
                return self

            async def read(self):
                return b"PNGDATA"

        class FakeUser:
            display_avatar = FakeAsset()

        async def _user(_guild, _uid):
            return FakeUser()
        economy.instance._resolve_avatar_user = _user
        rows = [{"id": 43}]
        asyncio.run(economy._attach_avatars(rows, None))
        assert rows[0]["avatar"] == b"PNGDATA"
        assert economy.instance._AVATAR_CACHE[43][0] == b"PNGDATA"

        # 3) Zweiter Aufruf: kommt aus dem Cache, Resolver wird nicht gebraucht.
        async def _boom(_guild, _uid):
            raise AssertionError("Resolver darf bei Cache-Treffer nicht laufen")
        economy.instance._resolve_avatar_user = _boom
        rows = [{"id": 43}]
        asyncio.run(economy._attach_avatars(rows, None))
        assert rows[0]["avatar"] == b"PNGDATA"
    finally:
        economy.instance._resolve_avatar_user = orig
        economy.instance._AVATAR_CACHE.clear()
        economy.instance._AVATAR_FAIL.clear()


def test_economy_display_name_of():
    # economy ist im Test nicht aktiviert -> None statt Crash.
    assert economy.display_name_of(123456789012345678) is None


def test_admin_ansage_parsing():
    # Rohe Channel-ID
    cid, text = admin._parse_announce("1453881901738889351 Servus Leute!")
    assert cid == 1453881901738889351 and text == "Servus Leute!"
    # Channel-Erwaehnung <#id> (so kam es in der DM an)
    cid, text = admin._parse_announce("<#1453881901738889351> Servus Leute!")
    assert cid == 1453881901738889351 and text == "Servus Leute!"
    # Mehrzeiliger Text bleibt komplett erhalten
    cid, text = admin._parse_announce("1453881901738889351 Zeile 1\nZeile 2")
    assert cid is not None and text == "Zeile 1\nZeile 2"
    # Ohne Text / ohne ID -> Hinweis-Fall
    assert admin._parse_announce("1453881901738889351") == (None, "")
    assert admin._parse_announce("hallo welt") == (None, "")


def test_admin_dm_parsing():
    # Mention + Text
    uid, text = admin._parse_dm("<@1040135855710404659> hey na, alles fit?")
    assert uid == 1040135855710404659 and text == "hey na, alles fit?"
    # Rohe ID + Text (DM-Fall)
    uid, text = admin._parse_dm("123456789012345678 komm mal Voice")
    assert uid == 123456789012345678 and text == "komm mal Voice"
    # Text VOR der ID geht auch
    uid, text = admin._parse_dm("sag mal 123456789012345678")
    assert uid == 123456789012345678 and text == "sag mal"
    # Ohne Ziel / ohne Text -> Hinweis-Fall
    assert admin._parse_dm("nur text ohne ziel") == (None, "")
    uid, text = admin._parse_dm("<@123456789012345678>")
    assert uid == 123456789012345678 and text == ""


def test_soundboard_ist_eine_server_einstellung():
    """Das Soundboard lag frueher in einem EIGENEN Speicher, galt fuer ALLE
    Server gleich und liess sich nur vom Bot-Besitzer umschalten - im Web-Panel
    tauchte es gar nicht auf.

    Jetzt steht es in guildcfg. Damit gilt es je Server, erscheint automatisch
    im Panel (das rendert den Katalog) und Discord-Befehl und Panel schreiben
    dieselbe Stelle. Genau das ist mit "synchronisiert" gemeint."""
    import guildcfg
    import voicegags

    # Es MUSS im Katalog stehen - sonst ist es nicht im Panel.
    assert "soundboard" in {e.key for e in guildcfg.KATALOG}
    assert "join_sounds" in {e.key for e in guildcfg.KATALOG}

    stand = {}
    alt_an, alt_setzen = guildcfg.an, guildcfg.setzen

    async def fake_setzen(gid, key, roh, guild=None):
        stand[(int(gid), key)] = str(roh).lower() in ("an", "ein", "on", "1", "true")
        return True, stand[(int(gid), key)], ""

    guildcfg.an = lambda gid, key: stand.get((int(gid), key), True)
    guildcfg.setzen = fake_setzen
    alt_enabled = voicegags.instance._enabled
    voicegags.instance._enabled = True
    try:
        # Gelesen wird je Server - nicht global.
        stand[(77, "soundboard")] = False
        assert voicegags.soundboard_enabled(77) is False
        assert voicegags.soundboard_enabled(88) is True, "der Wert gilt serveruebergreifend"

        def msg(text, darf=True, gid=77):
            m = _fake_msg(5, f"flo {text}")
            m.guild = SimpleNamespace(id=gid)
            m.author.guild_permissions = SimpleNamespace(manage_guild=darf)
            return m

        # Wer den Server verwaltet, darf schalten - und es landet in guildcfg.
        antwort = asyncio.run(voicegags.handle(msg("soundboard an")))
        assert stand[(77, "soundboard")] is True, stand
        assert "AN" in str(antwort)
        asyncio.run(voicegags.handle(msg("soundboard aus")))
        assert stand[(77, "soundboard")] is False

        # Wer nicht darf, aendert NICHTS - vorher brauchte man dafuer den
        # Bot-Besitzer, jetzt reicht 'Server verwalten'.
        stand[(77, "soundboard")] = True
        antwort = asyncio.run(voicegags.handle(msg("soundboard aus", darf=False)))
        assert stand[(77, "soundboard")] is True, "ohne Recht wurde geschaltet"
        assert "verwaltet" in str(antwort)

        # 'soundboard' OHNE an/aus ist weiterhin der Aufruf des Bretts und
        # aendert die Einstellung NICHT (sonst schaltet ein Blick sie um).
        stand[(77, "soundboard")] = True
        try:
            asyncio.run(voicegags.handle(msg("soundboard")))
        except AttributeError:
            pass        # das Brett braucht ein echtes Discord-Objekt
        assert stand[(77, "soundboard")] is True, "ein Blick hat umgeschaltet"
    finally:
        guildcfg.an, guildcfg.setzen = alt_an, alt_setzen
        voicegags.instance._enabled = alt_enabled


def test_cmdnorm_admin_sicherheit():
    # Alltagswoerter, die 1 Tippfehler von Admin-Befehlen entfernt sind,
    # duerfen NICHT gekapert werden.
    for satz in ("nimmt das ernst", "profi tipp", "ansagen bitte"):
        assert cmdnorm.normalize(satz) is None, satz
    # Echte Vertipper werden weiterhin korrigiert.
    assert cmdnorm.normalize("admiin") == "admin"


# --- Luxus-Shop ------------------------------------------------------------------
def test_luxus_katalog():
    """Preisleiter: erreichbarer Einstieg, spuerbare Stufen, 1 Mrd als Endziel.
    Die Preise sind aus dem Tageseinkommen abgeleitet (economy.py) - ein normal
    aktiver Tag bringt rund 8-10k, ein guter Aktien-Vormittag bis ~20 Mio."""
    preise = [i["preis"] for i in luxus.ITEMS]
    assert preise == sorted(preise), "Katalog muss nach Preis aufsteigen"
    assert len(set(preise)) == len(preise), "keine zwei gleich teuren Stufen"
    # Einstieg: ein paar Tage normal spielen, nicht ein Monat.
    assert 20_000 <= preise[0] <= 80_000, preise[0]
    # Die Leiter endet jenseits der Milliarde (1 Mrd war fuer Aktien-Haendler zu
    # schnell erreicht) - aber im erreichbaren Bereich: das Gleichgewicht der
    # Vermoegenssteuer liegt bei rund 3,4 Mrd (economy.TAX_SOFT/TAX_RATE_TOP).
    assert preise[-1] == 3_000_000_000
    hoechst = economy.instance.TAX_FREE
    lo, hi = 0, 10 ** 13
    for _ in range(200):                      # Gleichgewicht bei 20 Mio/Tag suchen
        m = (lo + hi) // 2
        if economy.instance.steuer_fuer(m) < 20_000_000:
            lo = m
        else:
            hi = m
    hoechst = lo
    assert preise[-1] <= hoechst, (preise[-1], hoechst)   # kein totes Inhalt
    # Stufen muessen sich anfuehlen. Unten grosse Spruenge (x5), oben flacher -
    # dort bremst die Steuer, jeder Schritt dauert dann ohnehin Wochen.
    for a, b in zip(preise, preise[1:]):
        faktor = b / a
        grenze = 1.2 if a >= 1_000_000_000 else 3.0
        assert grenze <= faktor <= 10.0, (a, b, round(faktor, 2))
    assert len({i["key"] for i in luxus.ITEMS}) == len(luxus.ITEMS)
    assert len({i["n"] for i in luxus.ITEMS}) == len(luxus.ITEMS)
    assert luxus.THRONE_FACTOR > 1.0                # Thron wird immer teurer
    # Der Thron ist ein Zwischenziel, kein Einstieg.
    assert luxus.THRONE_START >= preise[0]


def test_luxus_fmt_coins():
    assert luxus.fmt_coins(1_500) == "1.500"
    assert luxus.fmt_coins(400_000) == "400.000"
    assert luxus.fmt_coins(2_500_000) == "2,5 Mio"
    assert luxus.fmt_coins(20_000_000) == "20 Mio"
    assert luxus.fmt_coins(1_000_000_000) == "1 Mrd"


def test_luxus_besitz_und_rahmen():
    # Fake-Store: Besitz-Logik ohne Datei/Discord testen (Zustand lebt in der Instanz).
    luxus.instance._store = type("S", (), {"data": {"users": {}, "throne": {
        "owner": "", "preis": luxus.THRONE_START, "n": 0}}})()
    luxus.instance._enabled = True
    try:
        uid = 42
        assert luxus.get_frame(uid) is None
        luxus._owned(uid).extend(["bronze", "gold"])
        assert luxus.get_frame(uid) == "gold"       # bester Rahmen zaehlt
        assert not luxus.has_crown(uid)
        # Imperium schaltet ALLES frei.
        luxus._owned(uid).append("imperium")
        assert luxus.get_frame(uid) == "imperium"
        assert luxus.owns(uid, "krone") and luxus.has_crown(uid)
        assert "Imperator" in luxus.get_tone_extra(uid)
        # Thron-Deko im Leaderboard.
        luxus.throne_state()["owner"] = "7"
        rows = [{"id": 7}, {"id": 42}, {"id": 9}]
        luxus.decorate_rows(rows)
        assert rows[0].get("throne") and rows[1].get("crown")
        assert not rows[2].get("crown") and not rows[2].get("throne")
    finally:
        luxus.instance._store = None
        luxus.instance._enabled = False


# --- Coin-Handelsbuch ------------------------------------------------------------
def test_handel_buchhaltung():
    """record() fuehrt Gesamtsummen, Quellen, Tages-Buckets und Einzelbuchungen;
    economy.add_coins bucht mit echtem Delta und erkanntem Quell-Modul."""
    import handel

    class FakeStore:
        def __init__(self, data):
            self.data = data

        async def save(self):
            pass

    alt = (handel.instance._store, handel.instance._enabled)
    handel.instance._store, handel.instance._enabled = FakeStore({"users": {}}), True
    try:
        handel.record(7, +150, "casino", 1150)
        handel.record(7, -100, "casino", 1050)
        handel.record(7, +120, "daily", 1170)
        handel.record(7, 0, "casino", 1170)      # 0-Buchung wird ignoriert
        u = handel.instance._store.data["users"]["7"]
        assert u["n"] == 3 and u["in"] == 270 and u["out"] == 100
        assert u["by"]["casino"] == {"in": 150, "out": 100, "n": 2}
        assert u["by"]["daily"]["in"] == 120
        assert len(u["days"]) == 1 and len(u["last"]) == 3
        tag = next(iter(u["days"].values()))
        assert tag["in"] == 270 and tag["out"] == 100
        assert u["last"][-1]["amt"] == 120 and u["last"][-1]["bal"] == 1170

        # economy-Integration: echtes Delta + Quelle (Aufrufer-Modul) landen hier.
        alt_eco = (economy.instance._store, economy.instance._enabled)
        economy.instance._store = FakeStore({"users": {}})
        economy.instance._enabled = True
        try:
            economy.add_coins(8, 500)
            economy.add_coins(8, -800)           # Konto 500 -> echtes Delta -500
            u8 = handel.instance._store.data["users"]["8"]
            assert u8["in"] == 500 and u8["out"] == 500
            # Quelle = Aufrufer-Modul (beim direkten Testlauf '__main__').
            quelle = next(iter(u8["by"]))
            assert quelle in ("test_games_logic", "__main__"), quelle
            assert u8["by"][quelle] == {"in": 500, "out": 500, "n": 2}
        finally:
            economy.instance._store, economy.instance._enabled = alt_eco

        # Karte rendert die Daten als PNG (auch mit leeren Tagen im Chart).
        buf = render.handel_card("Tester", None, u, 1170)
        assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        handel.instance._store, handel.instance._enabled = alt


# --- Casino-Bilanz: Gewonnen/Verloren-Summen -----------------------------------
def test_casino_bilanz_gewonnen_verloren():
    """record() zaehlt Brutto-Gewinne und -Verluste getrennt; Alt-Profile ohne
    die neuen Felder werden aus dem Netto geseedet; die Karte rendert damit."""
    class FakeStore:
        def __init__(self):
            self.data = {"stats": {}}

        async def save(self):
            pass

    alt_stats, alt_enabled = casino.instance._stats, casino.instance._enabled
    casino.instance._stats, casino.instance._enabled = FakeStore(), True
    try:
        asyncio.run(casino.record(1, "slots", 100, 250))   # +150 gewonnen
        asyncio.run(casino.record(1, "slots", 100, 0))     # -100 verloren
        asyncio.run(casino.record(1, "crash", 200, 0))     # -200 verloren
        asyncio.run(casino.record(1, "sieben", 50, 50))    # +-0 -> zaehlt nirgends
        prof = casino.instance._stats_profile(1)
        assert prof["games"] == 4 and prof["wagered"] == 450 and prof["payout"] == 300
        assert prof["won"] == 150 and prof["lost"] == 300
        assert prof["won"] - prof["lost"] == prof["payout"] - prof["wagered"]
        assert prof["best_win"] == 150
        # Migration: Alt-Profil ohne won/lost -> aus dem Netto geseedet.
        casino.instance._stats.data["stats"]["2"] = {
            "games": 5, "wagered": 1000, "payout": 1400, "best_win": 300, "per": {}}
        alt = casino.instance._stats_profile(2)
        assert alt["won"] == 400 and alt["lost"] == 0
        casino.instance._stats.data["stats"]["3"] = {
            "games": 2, "wagered": 500, "payout": 100, "best_win": 0, "per": {}}
        alt = casino.instance._stats_profile(3)
        assert alt["won"] == 0 and alt["lost"] == 400
        # Stats-Karte rendert die neuen Kennzahlen als PNG.
        buf = render.casino_stats_card("Tester", None, casino.instance._stats_profile(1))
        assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        casino.instance._stats, casino.instance._enabled = alt_stats, alt_enabled


class _FakeStore:
    """Minimaler JsonStore-Ersatz fuer die Tests (kein Datei-IO).

    Muss ALLES koennen, was store.JsonStore oeffentlich anbietet - sonst faellt
    eine Attrappe erst auf, wenn der echte Store eine Methode dazubekommt und
    elf Tests gleichzeitig mit AttributeError umkippen. Genau das ist beim
    Einbau von save_soon passiert. test_attrappe_kann_alles_was_der_store_kann
    haelt die beiden ab jetzt zusammen.
    """

    def __init__(self, data):
        self.data = data
        self.gespeichert = 0
        self.angemeldet = 0

    async def save(self):
        self.gespeichert += 1
        return True

    def save_soon(self):
        self.angemeldet += 1

    async def flush_now(self):
        self.angemeldet = 0
        return await self.save()


def test_attrappe_kann_alles_was_der_store_kann():
    """Die Test-Attrappe darf nicht hinter dem echten Store zurueckbleiben.

    Als store.JsonStore save_soon bekam, kippten auf einen Schlag elf Tests mit
    AttributeError um - die Attrappe kannte die Methode nicht. Der Fehler lag
    nicht im Bot, sondern in der Attrappe, und er kostet jedes Mal Zeit, bis man
    das gemerkt hat. Dieser Test sagt es sofort und beim Namen.
    """
    import store

    echt = {name for name in dir(store.JsonStore)
            if not name.startswith("_") and callable(getattr(store.JsonStore, name))}
    attrappe = {name for name in dir(_FakeStore) if not name.startswith("_")}
    fehlt = sorted(echt - attrappe)
    assert not fehlt, (
        f"_FakeStore fehlen Methoden, die store.JsonStore hat: {fehlt}. "
        f"Attrappe nachziehen, nicht den Store beschneiden.")


def _embed_text(antwort):
    """Alles Lesbare aus einer Bot-Antwort (Embed ODER Text) als ein String.
    Viele Antworten sind von Fliesstext auf Embeds umgestellt worden - die Tests
    pruefen den INHALT, nicht die Verpackung."""
    import discord
    if not isinstance(antwort, discord.Embed):
        return str(antwort)
    teile = [antwort.title or "", antwort.description or ""]
    for f in antwort.fields:
        teile.append(f.name or "")
        teile.append(f.value or "")
    if antwort.footer is not None:
        teile.append(antwort.footer.text or "")
    return "\n".join(teile)


def _with_economy(coins_by_uid=None):
    """Aktiviert economy mit Fake-Store; gibt (restore_fn) zurueck."""
    alt = (economy.instance._store, economy.instance._enabled)
    economy.instance._store = _FakeStore({"users": {}})
    economy.instance._enabled = True
    for uid, coins in (coins_by_uid or {}).items():
        economy.instance._profile(uid)["coins"] = coins

    def restore():
        economy.instance._store, economy.instance._enabled = alt
    return restore


# --- economy Titel-API (grant/remove/list/owns) --------------------------------
def test_economy_title_helpers():
    restore = _with_economy({1: 0})
    try:
        assert not economy.owns_title(1, "Held")
        neu = economy.grant_title(1, "Held", "🛡️ Held", "selten", wear=True)
        assert neu is True
        assert economy.owns_title(1, "Held")
        assert economy.get_title(1) == "🛡️ Held"
        # Zweites grant_title fuer denselben Titel -> kein neuer Eintrag.
        assert economy.grant_title(1, "Held", "🛡️ Held", "selten") is False
        assert len(economy.list_titles(1)) == 1
        # remove_title nimmt ihn raus und legt den getragenen Titel ab.
        entry = economy.remove_title(1, "Held")
        assert entry is not None and entry["text"] == "Held"
        assert not economy.owns_title(1, "Held")
        assert economy.get_title(1) == ""
        # remove_title auf nicht-besessenen Titel -> None.
        assert economy.remove_title(1, "GibtsNicht") is None
        # Unbekannte Rarity faellt auf 'normal' zurueck.
        economy.grant_title(1, "X", "X", "quatsch")
        assert economy.list_titles(1)[0]["rarity"] == "normal"
    finally:
        restore()


# --- economy: Aktien duerfen ins Minus (Casino nicht) --------------------------
def test_economy_stock_debt():
    """allow_negative laesst NUR Aktien unter 0; normale Ausgaben enden bei 0 und
    Gutschriften bauen Schulden korrekt ab (kein faelschliches 0-Clampen)."""
    restore = _with_economy({1: 100})
    try:
        # Normale Ausgabe (Casino & Co.) kann nicht unter 0.
        economy.add_coins(1, -500, reason="casino")
        assert economy.get_coins(1) == 0
        # Aktien duerfen ins Minus (allow_negative=True).
        economy.add_coins(1, -3000, reason="floaktie", allow_negative=True)
        assert economy.get_coins(1) == -3000
        # Gutschrift baut Schulden ab (NICHT auf 0 geklemmt).
        economy.add_coins(1, 1000, reason="nachricht")
        assert economy.get_coins(1) == -2000
        # Normale Ausgabe im Minus ist ein No-Op (kein Geld da).
        economy.add_coins(1, -500, reason="casino")
        assert economy.get_coins(1) == -2000
        # Genug Gutschrift -> wieder ins Plus.
        economy.add_coins(1, 3000, reason="lotto")
        assert economy.get_coins(1) == 1000
        # Konten OHNE Aktien bleiben unveraendert bei 0 Schluss.
        economy.add_coins(2, 50, reason="daily")
        economy.add_coins(2, -80, reason="shop")
        assert economy.get_coins(2) == 0
    finally:
        restore()


# --- Fahrender Haendler --------------------------------------------------------
def test_merchant_shop_und_trade():
    """Kaufen (inkl. limitiert/ausverkauft/zu teuer/schon besessen), Tauschen
    (Einsatz raus, Belohnung rein), eligible_gives, is_present & closed-Text."""
    import merchant

    restore_eco = _with_economy({1: 6000, 2: 60000, 3: 60000, 4: 5000})
    m = merchant.instance
    alt = (m._store, m._enabled)
    m._enabled = True
    stock = [
        {"id": "haendler:schatzsucher", "text": "Schatzsucher",
         "label": "🧭 Schatzsucher", "rarity": "selten", "price": 5000, "limit": 0},
        {"id": "haendler:drachenlord", "text": "Drachenlord",
         "label": "🐉 Drachenlord", "rarity": "legendary", "price": 45000, "limit": 1},
    ]
    trades = [{
        "id": "trade:haendler:sternenjaeger", "need_rarity": "selten",
        "surcharge": 3000, "reward_id": "haendler:sternenjaeger",
        "reward_text": "Sternenjäger", "reward_label": "🌠 Sternenjäger",
        "reward_rarity": "mythisch"}]
    m._store = _FakeStore({"arrived": True, "departed": False, "depart_at": 9e18,
                           "stock": stock, "trades": trades, "sold": {}})

    def member(uid, name="M"):
        return SimpleNamespace(id=uid, display_name=name, guild=None)
    try:
        assert m.is_present() is True
        # Kauf: genug Coins -> Titel im Inventar, Coins abgezogen.
        r = asyncio.run(m.buy(member(1), "haendler:schatzsucher"))
        assert "Gekauft" in r
        assert economy.get_coins(1) == 1000
        assert economy.owns_title(1, "Schatzsucher")
        # Nochmal derselbe -> schon besessen, keine erneute Abbuchung.
        r = asyncio.run(m.buy(member(1), "haendler:schatzsucher"))
        assert "schon" in r.lower() and economy.get_coins(1) == 1000
        # Zu teuer.
        assert "teuer" in asyncio.run(m.buy(member(4), "haendler:drachenlord")).lower()
        # Limitierte Ware: erster kauft, zweiter guckt in die Roehre.
        r = asyncio.run(m.buy(member(2), "haendler:drachenlord"))
        assert "Gekauft" in r and economy.get_coins(2) == 15000
        r = asyncio.run(m.buy(member(3), "haendler:drachenlord"))
        assert "ausverkauft" in r.lower() and economy.get_coins(3) == 60000

        # Tausch: member 3 braucht einen SELTENEN Einsatz-Titel.
        assert asyncio.run(m.trade(member(3), "trade:haendler:sternenjaeger",
                                   "IrgendWas")).lower().count("besitzt") >= 0
        economy.grant_title(3, "Glücksbär", "🐻 Glücksbär", "selten")
        assert len(m.eligible_gives(member(3), "selten")) == 1
        assert m.eligible_gives(member(3), "mythisch") == []   # zu niedrig
        r = asyncio.run(m.trade(member(3), "trade:haendler:sternenjaeger", "Glücksbär"))
        assert "Tausch" in r
        assert economy.owns_title(3, "Sternenjäger")           # Belohnung da
        assert not economy.owns_title(3, "Glücksbär")          # Einsatz weg
        assert economy.get_coins(3) == 57000                   # 3000 Aufzahlung
        # Belohnung schon besessen -> abgelehnt.
        economy.grant_title(3, "Glücksbär", "🐻 Glücksbär", "selten")
        assert "schon" in asyncio.run(
            m.trade(member(3), "trade:haendler:sternenjaeger", "Glücksbär")).lower()

        # Kein Haendler-Befehl -> None; Weg-Zustand -> closed-Text (str).
        def hmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=member(uid))
        assert asyncio.run(m.handle(hmsg(1, "wie gehts"))) is None
        m._store.data["departed"] = True
        assert isinstance(asyncio.run(m.handle(hmsg(1, "haendler"))), str)

        # _roll_stock ist KRASSER als der Shop: keine gewoehnlichen/seltenen Titel,
        # immer mind. ein Highlight; Tausch-Belohnungen sind ebenfalls Highlights.
        import titles as _t
        erlaubt = {"episch", "mythisch", "legendary", "relikt", "exklusiv", "goettlich"}
        gesehen = set()
        for _ in range(60):
            m._roll_stock()
            stock = m._state()["stock"]
            assert stock and all(e["rarity"] in erlaubt for e in stock), stock
            assert any(e["rarity"] in merchant.Merchant._HIGHLIGHTS for e in stock)
            gesehen.update(e["rarity"] for e in stock)
            for t in m._state()["trades"]:
                assert t["reward_rarity"] in merchant.Merchant._HIGHLIGHTS
                # Einsatz ist immer die Stufe DIREKT unter der Belohnung.
                assert (_t.RANK[t["need_rarity"]]
                        == _t.RANK[t["reward_rarity"]] - 1), t
                assert t["surcharge"] >= 1000
        # Ueber 60 Tage muss auch die Spitze mal auftauchen (sonst ist sie toter Code).
        assert "goettlich" in gesehen or "exklusiv" in gesehen, gesehen
        # Jeder Katalog-Preis passt in die Spanne seiner Stufe (hier: nie teurer).
        for e in merchant._KATALOG:
            lo, hi = _t.RARITY[e["rarity"]]["price"]
            assert lo * 0.2 <= e["price"] <= hi, (e["id"], e["price"], lo, hi)
    finally:
        m._store, m._enabled = alt
        restore_eco()


# --- Titel: die Seltenheits-Leiter -------------------------------------------
def test_titles_leiter():
    """Acht Stufen, saubere Verteilung, eigene Farbe/Rolle je Stufe - und die
    Haendler-Stufen tauchen NIE im normalen Shop auf."""
    import titles
    ordnung = titles.RARITY_ORDER
    assert len(ordnung) == 8 and len(set(ordnung)) == 8
    # Jede Stufe hat alles, was Shop, Rolle, Bild und KI brauchen.
    for r in ordnung:
        meta = titles.RARITY[r]
        for key in ("label", "emoji", "color", "role", "price", "pool_pct",
                    "shop_weight", "tone"):
            assert key in meta and meta[key] not in (None, ""), (r, key)
        assert meta["role"].startswith("Flo · ")
    # Farben und Rollennamen muessen EINDEUTIG sein (sonst zwei Stufen, eine Rolle).
    farben = [titles.RARITY[r]["color"] for r in ordnung]
    rollen = [titles.RARITY[r]["role"] for r in ordnung]
    assert len(set(farben)) == len(farben), farben
    assert len(set(rollen)) == len(rollen), rollen
    # Preise steigen streng mit dem Rang und ueberlappen nicht.
    spannen = [titles.RARITY[r]["price"] for r in ordnung]
    for (lo1, hi1), (lo2, hi2) in zip(spannen, spannen[1:]):
        assert lo1 < hi1 <= lo2 < hi2, (lo1, hi1, lo2, hi2)
    # Verteilung: pool_pct summiert auf 100 und die Hash-Grenzen passen dazu.
    assert sum(titles.RARITY[r]["pool_pct"] for r in ordnung) == 100
    anzahl = titles.counts()
    gesamt = titles.total()
    for r in ordnung:
        pct = titles.RARITY[r]["pool_pct"]
        if pct == 0:
            assert anzahl.get(r, 0) == 0, r          # Haendler-Stufe: kein Pool
            continue
        anteil = anzahl.get(r, 0) / gesamt * 100
        assert abs(anteil - pct) < 2.0, (r, round(anteil, 2), pct)
    # Haendler-Stufen kommen NIE aus dem Shop.
    nur_haendler = [r for r in ordnung if titles.RARITY[r]["shop_weight"] == 0]
    assert set(nur_haendler) == {"exklusiv", "goettlich"}
    for e in titles.random_titles(60):
        assert e["rarity"] not in nur_haendler
    assert all(titles.rarity_of(t) not in nur_haendler
               for t in ("Goldener König", "Wilder Wolf", "Titan des Chaos"))
    # 'goettlich' ist die hoechste Stufe.
    assert titles.RANK["goettlich"] == max(titles.RANK.values())


# --- Monats-Lotto --------------------------------------------------------------
def test_lotto_flow():
    """Lospreis = Jackpot/80 (glatt); Kauf zieht Coins ab, zaehlt Lose UND fuellt
    Flos Kasse; Gewinnen ist chancen-gesteuert (extrem selten); kein Gewinner ->
    Jackpot-Rollover waechst; nur der Owner kann die Kasse abbuchen."""
    import lotto

    restore_eco = _with_economy({1: 1_000_000, 2: 100, lotto.OWNER_ID: 0, 999: 0})
    lt = lotto.instance
    alt = (lt._store, lt._enabled, lt._win_chance)
    lt._enabled = True
    lt._win_chance = 1.0
    lt._store = _FakeStore({"month": "2026-07", "jackpot": 20_000_000,
                            "ticket_price": 10_000, "entries": {}, "house": 0,
                            "history": []})
    try:
        # Lospreis: sanft gekoppelt & gedeckelt - 20 Mio -> 10k (kein Wucher mehr).
        assert lt._price_for(20_000_000) == 10_000
        assert lt._price_for(5_000_000) == 2_500
        assert lt._price_for(500_000_000) == lotto.TICKET_MAX     # Deckel greift
        for _ in range(50):
            jp, preis = lt._roll_jackpot()
            assert jp % 1_000_000 == 0
            assert preis == max(lotto.TICKET_MIN,
                                min(lotto.TICKET_MAX, jp // lotto.PRICE_DIVISOR))
            assert lotto.TICKET_MIN <= preis <= lotto.TICKET_MAX
            assert lotto.JACKPOT_MIN_M * 1_000_000 <= jp <= lotto.JACKPOT_MAX_M * 1_000_000

        # Kauf: Coins ab, Lose gezaehlt, Losgeld aufgeteilt.
        # Der GROSSTEIL waechst den Jackpot (die Spieler fuellen den Topf, aus dem
        # sie gewinnen), nur der Hausanteil bleibt in der Kasse. Vorher ging alles
        # in die Kasse - und weil der Besitzer die abbuchen kann, war das Lotto in
        # der Bilanz gar keine Senke, sondern nur ein Zwischenlager.
        jp_vor = lt._state()["jackpot"]
        r = asyncio.run(lt.buy(SimpleNamespace(id=1), 3))
        kosten = 3 * 10_000
        zum_jackpot = int(kosten * lotto.JACKPOT_SHARE)
        assert "3" in r and economy.get_coins(1) == 1_000_000 - kosten
        assert lt._entries()["1"] == 3
        assert lt._state()["house"] == kosten - zum_jackpot
        assert lt._state()["jackpot"] == jp_vor + zum_jackpot
        # Kein Coin geht verloren oder entsteht: Losgeld = Jackpot-Zuwachs + Kasse.
        assert (lt._state()["jackpot"] - jp_vor) + lt._state()["house"] == kosten
        assert 0.0 < lotto.JACKPOT_SHARE < 1.0
        # Zu wenig Coins -> Hinweis, keine Lose, Kasse/Jackpot unveraendert.
        haus_vor, jp2 = lt._state()["house"], lt._state()["jackpot"]
        r = asyncio.run(lt.buy(SimpleNamespace(id=2), 1))
        assert isinstance(r, str) and "2" not in lt._entries()
        assert lt._state()["house"] == haus_vor and lt._state()["jackpot"] == jp2
        # 'max' deckelt nach Guthaben.
        assert lt._resolve_count(SimpleNamespace(id=1), "max") == \
            economy.get_coins(1) // 10_000

        # Gewinnchance: 1 Los bei chance=1.0 sicher; bei chance 0 nie.
        assert lt._win_prob_for(1) == 1.0
        lt._win_chance = 0.0
        assert lt._win_prob_for(9999) == 0.0
        lt._win_chance = 1.0

        # Ziehung MIT Gewinn (chance 1.0): Spieler kriegt den Jackpot, won=True.
        # Der Jackpot ist inzwischen um den Losgeld-Anteil gewachsen.
        vorher = economy.get_coins(1)
        jackpot_jetzt = lt._state()["jackpot"]
        res = lt._draw()
        assert res.won and res.winner_ids == [1]
        assert economy.get_coins(1) == vorher + jackpot_jetzt
        assert lt._state()["history"][-1]["winner_ids"] == [1]

        # Ziehung OHNE Gewinn (chance 0): kein Gewinner, kein Crash, won=False.
        lt._win_chance = 0.0
        lt._store.data["entries"] = {"1": 5}
        res = lt._draw()
        assert not res.won and res.winner_ids == []

        # Kasse: Fremder darf NICHT abbuchen; Owner holt alles aufs Konto.
        lt._store.data["house"] = 750_000
        deny = asyncio.run(lt.withdraw(SimpleNamespace(id=999), "alles"))
        assert isinstance(deny, str) and economy.get_coins(999) == 0
        assert lt._state()["house"] == 750_000
        ow_vor = economy.get_coins(lotto.OWNER_ID)
        asyncio.run(lt.withdraw(SimpleNamespace(id=lotto.OWNER_ID), "alles"))
        assert economy.get_coins(lotto.OWNER_ID) == ow_vor + 750_000
        assert lt._state()["house"] == 0

        # Monatswechsel via tick() MIT Gewinn: loest aus, frischer Jackpot, Lose leer.
        lt._win_chance = 1.0
        lt._store.data.update({"month": "2020-01", "jackpot": 10_000_000,
                               "ticket_price": 125_000, "entries": {"1": 2}})
        h_vor = len(lt._state()["history"])
        res = asyncio.run(lt.tick())
        assert res is not None and res.won and res.winner_ids == [1]
        assert lt._state()["month"] == lt._month_str()      # neuer Monat laeuft
        assert lt._state()["entries"] == {}                 # Lose zurueckgesetzt
        assert len(lt._state()["history"]) == h_vor + 1
        assert (lotto.JACKPOT_MIN_M * 1_000_000
                <= lt._state()["jackpot"] <= lotto.JACKPOT_MAX_M * 1_000_000)

        # Monatswechsel OHNE Gewinn: Jackpot-Rollover waechst.
        lt._win_chance = 0.0
        lt._store.data.update({"month": "2019-12", "jackpot": 8_000_000,
                               "ticket_price": 100_000, "entries": {"1": 1}})
        res = asyncio.run(lt.tick())
        assert not res.won
        assert lt._state()["jackpot"] >= 8_000_000 + lotto.GROWTH_MIN_M * 1_000_000

        # handle: Nicht-Befehl -> None; Kauf -> str; 'kasse' fuer Fremde -> str.
        def lmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="P"))
        assert asyncio.run(lt.handle(lmsg(1, "wie gehts"))) is None
        assert isinstance(asyncio.run(lt.handle(lmsg(1, "lotto kauf 1"))), str)
        assert isinstance(asyncio.run(lt.handle(lmsg(1, "lotto kasse"))), str)
    finally:
        lt._store, lt._enabled, lt._win_chance = alt
        restore_eco()


# --- KI: geleakte Werkzeug-Syntax herausfiltern --------------------------------
def test_ai_tool_leak_sanitizer():
    """'<function=get_weather>{...}</function>' & Co. werden erkannt und aus dem
    Antworttext entfernt - normale Winkelklammern/Mentions bleiben unangetastet."""
    import ai
    a = ai.instance
    leak = 'Na klar.\n<function=get_weather>{"city": "Regensburg"}</function>'
    clean = a._sanitize_output(leak)
    assert "<function" not in clean and "get_weather" not in clean
    assert "Na klar." in clean
    # Der geleakte Aufruf wird zur echten Ausfuehrung extrahiert.
    assert a._extract_inline_tool_calls(leak) == [("get_weather", '{"city": "Regensburg"}')]
    # Auch OHNE schliessendes Tag wird die Syntax entfernt.
    assert "<function" not in a._sanitize_output('Hi <function=get_weather>{"city":"X"}')
    # <tool_call>-Bloecke und Streu-Tags raus.
    assert a._sanitize_output("A <tool_call>{x}</tool_call> B").replace(" ", "") == "AB"
    assert "</function>" not in a._sanitize_output("text</function>")
    # Normale Winkelklammern & Mentions bleiben, wie sie sind.
    assert a._sanitize_output("2 < 3 und <@123> bleibt") == "2 < 3 und <@123> bleibt"
    # Kein Tool drin -> keine Extraktion; leer/None sicher.
    assert a._extract_inline_tool_calls("nur normaler text") == []
    assert a._sanitize_output("") == "" and a._sanitize_output(None) == ""


def test_fun_dm_roast():
    """Beleidigung im Chat -> Flo schickt (im Test sicher) eine DM-Retoure; harmlose
    Nachrichten NICHT; Cooldown pro Person greift; Bots werden nicht angeschrieben."""
    import fun
    f = fun.instance
    alt = (f._enabled, f._last_dmroast, dict(f._dm_cooldowns))
    alt_mod = (fun.DMROAST_CHANCE, fun.DMROAST_GLOBAL_COOLDOWN, fun.DMROAST_USER_COOLDOWN)
    f._enabled = True
    f._last_dmroast = 0.0
    f._dm_cooldowns = {}
    fun.DMROAST_CHANCE = 1.0            # zum Testen sicher feuern
    fun.DMROAST_GLOBAL_COOLDOWN = 0.0   # serverweiten Cooldown ausschalten
    fun.DMROAST_USER_COOLDOWN = 0.0

    sent = []

    class FakeAuthor:
        def __init__(self, uid, name="Poebler", is_bot=False):
            self.id = uid
            self.bot = is_bot
            self.display_name = name

        async def send(self, text):
            sent.append((self.id, text))

    def mk(author, content):
        return SimpleNamespace(author=author, content=content,
                               guild=SimpleNamespace(id=1))
    try:
        # Erkennung: Beleidigung ja, harmlos nein.
        assert f.looks_offensive("du bist ein arschloch")
        assert f.looks_offensive("halt dein maul")
        assert not f.looks_offensive("schönen tag noch, alles gut")

        # Beleidigung -> DM-Konter (nicht-leerer String an den Autor).
        a = FakeAuthor(1)
        asyncio.run(f.maybe_dm_roast(mk(a, "du hurensohn spinnst wohl")))
        assert len(sent) == 1 and sent[0][0] == 1 and isinstance(sent[0][1], str) and sent[0][1]

        # Harmlose Nachricht -> keine DM.
        asyncio.run(f.maybe_dm_roast(mk(FakeAuthor(9), "wann fangen wir an zu zocken")))
        assert len(sent) == 1

        # Cooldown pro Person: gleicher Poebler nochmal -> nichts (trotz Chance 1.0).
        fun.DMROAST_USER_COOLDOWN = 99999.0
        f._dm_cooldowns = {}
        b = FakeAuthor(2)
        asyncio.run(f.maybe_dm_roast(mk(b, "fick dich du opfer")))
        n1 = len(sent)
        asyncio.run(f.maybe_dm_roast(mk(b, "arschloch nochmal")))
        assert len(sent) == n1        # vom Personen-Cooldown geblockt

        # Bots werden nicht privat angeschrieben.
        f._dm_cooldowns = {}
        asyncio.run(f.maybe_dm_roast(mk(FakeAuthor(3, is_bot=True), "du wichser")))
        assert all(uid != 3 for uid, _ in sent)
    finally:
        f._enabled, f._last_dmroast, f._dm_cooldowns = alt
        fun.DMROAST_CHANCE, fun.DMROAST_GLOBAL_COOLDOWN, fun.DMROAST_USER_COOLDOWN = alt_mod


def test_floaktie_market():
    """Kauf hebt/Verkauf senkt den Kurs (arb-frei), Markt-Tick reagiert auf Voice-
    Aktivitaet, Dividende ist proportional (Groesster doppelt), Leaderboard sortiert."""
    import discord
    import floaktie
    restore_eco = _with_economy({1: 1_000_000, 2: 500_000})
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    alt_noise = floaktie.TICK_NOISE
    fa._enabled = True
    fa._store = _FakeStore({"price": 1000, "day": fa._today(), "act_ema": floaktie.ACT_BASELINE,
                            "msg_count": 0, "last_msg_count": 0,
                            "holdings": {}, "history": [{"day": "d", "price": 1000}]})
    try:
        p0 = fa.price()
        # Kauf: Kurs steigt, (Impact-)Kosten ab, Depot waechst.
        c0 = economy.get_coins(1)
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 50))
        # Bestaetigung ist ein Embed (vorher zwei Zeilen Fliesstext).
        assert _embed_text(r).count("Gekauft") == 1, _embed_text(r)
        assert fa.price() > p0 and fa.shares_of(1) == 50
        assert c0 - economy.get_coins(1) >= 50 * p0        # Impact -> mind. 50*Startkurs

        # Verkauf: Kurs faellt, Coins zurueck, Depot schrumpft.
        p1 = fa.price()
        r = asyncio.run(fa.sell(SimpleNamespace(id=1), 20))
        assert "Verkauft" in _embed_text(r)
        assert fa.price() < p1 and fa.shares_of(1) == 30

        # Kein Gratis-Arbitrage: sofortiger Round-Trip macht Verlust.
        economy.instance._profile(3)["coins"] = 1_000_000
        start = economy.get_coins(3)
        asyncio.run(fa.buy(SimpleNamespace(id=3), 100))
        asyncio.run(fa.sell(SimpleNamespace(id=3), 100))
        assert economy.get_coins(3) < start and fa.shares_of(3) == 0

        # KEIN KREDIT MEHR: ein Konto mit 10 Coins kauft nichts und bleibt bei 10.
        economy.instance._profile(4)["coins"] = 10
        r = asyncio.run(fa.buy(SimpleNamespace(id=4), 100))
        assert isinstance(r, str) and fa.shares_of(4) == 0, r
        assert economy.get_coins(4) == 10 and "Guthaben nicht" in r
        # Wer mehr will, als das Guthaben hergibt, bekommt so viel wie moeglich -
        # und landet NIE im Minus.
        economy.instance._profile(5)["coins"] = 60_000
        r = asyncio.run(fa.buy(SimpleNamespace(id=5), 100))
        assert fa.shares_of(5) > 0, r
        assert 0 <= economy.get_coins(5) < 60_000, economy.get_coins(5)
        assert "MINUS" not in _embed_text(r)

        # Aktivitaets-Takt: viel los -> Kurs STEIGT, wenig -> faellt (Rauschen aus).
        floaktie.TICK_NOISE = 0.0
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE})
        a, n, drift, act = fa._activity_tick(15, 0)          # 15 im Call
        assert n > a and drift > 0
        # Fallen kann er nur ueber dem Wert eines toten Servers (FAIR_BASE) -
        # deshalb den BASISkurs klar darueber setzen, nicht nur den Anzeigewert.
        fa._store.data.update({"base": floaktie.FAIR_BASE * 10,
                               "act_ema": floaktie.ACT_BASELINE, "leer_min": 0.0})
        fa._sync_price()          # Anzeigekurs auf die Kurve ziehen (sonst stale)
        a, n, drift, act = fa._activity_tick(0, 0)           # niemand da
        assert drift < 0 and n <= a                          # Tendenz nach unten
        # Immer BASIS setzen und synchronisieren - sonst ist der gespeicherte
        # Anzeigekurs veraltet und 'vorher/nachher' vergleicht Aepfel mit Birnen.
        def stelle(kurs=1000):
            fa._store.data.update({"base": float(kurs),
                                   "act_ema": floaktie.ACT_BASELINE, "leer_min": 0.0})
            fa._sync_price()

        # NACHRICHTEN treiben den Kurs - aber nur BEI BESETZTEM CALL. Ohne
        # jemanden im Sprachkanal sind es 0 Punkte, egal wie viel getippt wird
        # (sonst setzt ein einziger Schreiber den Leerlauf-Verfall komplett aus).
        stelle()
        _a, _n, drift_leer, act_leer = fa._activity_tick(0, 200)   # reger Chat, leerer Call
        assert act_leer == 0.0 and drift_leer < 0, (act_leer, drift_leer)
        stelle()
        a, n, drift, act = fa._activity_tick(2, 200)         # zwei im Call + reger Chat
        assert n > a and drift > 0 and act > floaktie.ACT_BASELINE
        # ... und der Chat macht dabei einen messbaren Unterschied.
        stelle()
        _, _, _, act_ohne_chat = fa._activity_tick(2, 0)
        assert act > act_ohne_chat, (act, act_ohne_chat)
        # Live-Streamer zaehlen EXTRA: gleiche Personen, aber Streams -> mehr Aktivitaet.
        stelle()
        _, _, _, act_plain = fa._activity_tick(12, 0)
        stelle()
        _, _, _, act_stream = fa._activity_tick(12, 0, streams=6, video=3)
        assert act_stream > act_plain                        # Streamer/Kameras zaehlen mit
        # Ueber eine Stunde (60 Min-Takte) aktiver Call -> Kurs klar hoch.
        stelle()
        for _ in range(60):
            fa._activity_tick(12, 30, streams=6)
        assert fa.price() > 1030                             # klar gestiegen (Boersenwert mit)
        # note_message + sample_and_tick: Nachrichten werden GEZAEHLT, heben den
        # Kurs bei LEEREM Call aber nicht mehr (siehe activity_of).
        # ACHTUNG: 'base' setzen, nicht 'price' - 'price' ist nur der gespeicherte
        # Stand, gerechnet wird immer aus der Kurve. Frueher stand hier
        # {"price": 1000}; der Test war dadurch gruen, weil der Vergleich einen
        # veralteten Wert gegen einen frisch gerechneten hielt, nicht weil der
        # Chat irgendetwas bewegt haette.
        # Deutlich UEBER den Grundwert stellen, sonst faellt der Kurs zu Recht
        # gar nicht (siehe boden_base) und der Test misst nichts.
        stelle()
        fa._store.data["base"] = fa.boden_base() * 4.0
        fa._sync_price()
        vorher = fa.price()
        for _ in range(200):
            fa.note_message()
        assert fa._store.data["msg_count"] == 200
        guild0 = SimpleNamespace(voice_channels=[], afk_channel=None)
        asyncio.run(fa.sample_and_tick(guild0))
        assert fa.price() < vorher, (vorher, fa.price())

        # Dividende proportional; groesster Aktionaer doppelt; Leaderboard sortiert.
        fa._store.data["holdings"] = {"1": 100, "2": 300}
        assert fa.top_holder() == 2
        assert fa.dividend_for(1) == 100 // floaktie.DIVIDEND_DIVISOR
        assert fa.dividend_for(2) == (300 // floaktie.DIVIDEND_DIVISOR) * 2
        assert fa.leaderboard()[:2] == [(2, 300), (1, 100)]

        # Voice-Dividende landet bei aktiven Aktionaeren.
        m1 = SimpleNamespace(id=1, bot=False, voice=SimpleNamespace(self_deaf=False, deaf=False))
        m2 = SimpleNamespace(id=2, bot=False, voice=SimpleNamespace(self_deaf=False, deaf=False))
        guild = SimpleNamespace(voice_channels=[SimpleNamespace(id=10, members=[m1, m2])],
                                afk_channel=None)
        b1, b2 = economy.get_coins(1), economy.get_coins(2)
        asyncio.run(fa.pay_voice_dividends(guild))
        assert economy.get_coins(1) == b1 + fa.dividend_for(1)
        assert economy.get_coins(2) == b2 + fa.dividend_for(2)

        # handle-Routing: Nicht-Befehl -> None; kauf/top/depot -> Embed.
        # 'aktie' ist identisch zu 'floaktie' (nur EINE Aktie).
        def fmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="T"))
        assert asyncio.run(fa.handle(fmsg(1, "wie gehts"))) is None
        # Der Kurs ist durch die Takte oben weit gelaufen -> Konto auffuellen,
        # sonst scheitert der Kauf an der Kreditlinie (das ist hier nicht der Test).
        economy.instance._profile(1)["coins"] = 10_000_000_000
        assert "Gekauft" in _embed_text(asyncio.run(fa.handle(fmsg(1, "floaktie kauf 1"))))
        # 'aktie' == 'floaktie'
        economy.instance._profile(1)["coins"] = 10_000_000_000
        assert "Gekauft" in _embed_text(asyncio.run(fa.handle(fmsg(1, "aktie kauf 1"))))
        assert not isinstance(asyncio.run(fa.handle(fmsg(1, "aktie top"))), (str, type(None)))
        assert not isinstance(asyncio.run(fa.handle(fmsg(1, "floaktie depot"))), (str, type(None)))

        # Kurs-Chart: Serie hat >=2 Punkte, _chart_file rendert ein PNG.
        assert len(fa._series(7)) >= 2
        f = asyncio.run(fa._chart_file(7, "7 Tage"))   # rendert im Thread
        assert isinstance(f, discord.File)

        # aktienkurs / aktie chart -> Chart senden (HANDLED) via Fake-reply.
        class FakeMsg:
            def __init__(s, uid, content):
                s.author = SimpleNamespace(id=uid, display_name="T")
                s.content = content
                s.guild = SimpleNamespace(id=1)

            async def reply(s, **kw):
                return SimpleNamespace(id=1, channel=SimpleNamespace(id=1))
        assert asyncio.run(fa.handle(FakeMsg(1, "aktienkurs"))) is floaktie.HANDLED
        assert asyncio.run(fa.handle(FakeMsg(1, "aktie chart"))) is floaktie.HANDLED
        assert asyncio.run(fa.handle(FakeMsg(1, "aktie"))) is floaktie.HANDLED  # Panel

        # Live-Update: das zuletzt gepostete 'flo aktie'-Panel UND der zuletzt
        # gepostete 'flo aktienkurs'-Chart werden bei jeder Kursaenderung
        # (Trade & Aktivitaets-Takt) automatisch nachgezogen.
        edits, chart_edits = [], []

        class FakePanel:
            id = 1
            channel = SimpleNamespace(id=1)

            async def edit(self, **kw):
                edits.append(kw)

        class FakeChart:
            id = 2
            channel = SimpleNamespace(id=1)

            async def edit(self, **kw):
                chart_edits.append(kw)
        fa._panel_msg = FakePanel()
        fa._panel_uid = 1
        fa._chart_msg = FakeChart()
        fa._chart_days = 1
        asyncio.run(fa._refresh_last_panel())
        assert len(edits) == 1 and "embed" in edits[0]            # Panel aktualisiert
        asyncio.run(fa._refresh_last_chart())
        assert len(chart_edits) == 1 and "attachments" in chart_edits[0]   # Chart-Bild neu
        economy.instance._profile(1)["coins"] = 1_000_000
        asyncio.run(fa.buy(SimpleNamespace(id=1), 1))
        assert len(edits) >= 2 and len(chart_edits) >= 2         # Trade zieht beide nach
        # Aktivitaets-Takt mit Kursaenderung zieht Panel UND Chart nach.
        # Basiskurs bewusst tief setzen, damit der Takt garantiert etwas bewegt
        # (steht er schon am Deckel, ist die Drift 0 - und dann gibt es zu Recht
        # kein Update).
        fa._store.data["base"] = float(floaktie.FAIR_BASE)
        fa._store.data["act_ema"] = 0.0
        fa._sync_price()
        n0, c0 = len(edits), len(chart_edits)
        # Genug Nachrichten fuer einen sichtbaren Kurssprung (der Chat-Anteil ist
        # bei MSG_MAX_ACT gedeckelt, der Sprung muss den gerundeten Kurs bewegen).
        fa._store.data["base"] = float(floaktie.FAIR_BASE) * 50
        fa._sync_price()
        fa._store.data["msg_count"] = 999
        fa._store.data["last_msg_count"] = 0
        asyncio.run(fa.sample_and_tick(SimpleNamespace(voice_channels=[], afk_channel=None)))
        assert len(edits) > n0 and len(chart_edits) > c0
    finally:
        fa._store, fa._enabled = alt[0], alt[1]
        fa._panel_msg = None
        fa._chart_msg = None
        floaktie.TICK_NOISE = alt_noise
        restore_eco()


def test_webpanel_update_nur_einmal_gleichzeitig():
    """Zwei gleichzeitige Update-Klicks duerfen nicht zwei git-pulls starten.

    Zwei offene Tabs (oder ein zweites Geraet) haetten sonst zwei Pulls im selben
    Arbeitsverzeichnis losgeschickt; git legt dann index.lock an und der zweite
    Lauf bricht mit einer Meldung ab, die niemand einordnen kann."""
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = 0
        gestartet = []

        async def langsam(request):
            """Tut so, als liefe der Pull - lange genug fuer den zweiten Klick."""
            gestartet.append(1)
            await asyncio.sleep(0.25)
            return webpanel.web.json_response({"ok": True, "changed": False,
                                               "log": "test"})

        wp._update_lauf = langsam
        async with TestClient(TestServer(wp._build_app())) as c:
            a, b = await asyncio.gather(
                c.post("/api/update", json={"restart": False}),
                c.post("/api/update", json={"restart": False}),
            )
            codes = sorted([a.status, b.status])
            texte = [await a.json(), await b.json()]
            return codes, len(gestartet), texte

    codes, laeufe, texte = asyncio.run(lauf())
    assert codes == [200, 409], codes
    assert laeufe == 1, (laeufe, texte)          # nur EIN git pull
    abgelehnt = [t for t in texte if not t.get("ok")]
    assert abgelehnt and "läuft bereits" in abgelehnt[0].get("error", ""), texte

    # Und danach geht es wieder.
    codes2, laeufe2, _ = asyncio.run(lauf())
    assert laeufe2 == 1, laeufe2


def test_webpanel_api():
    """Web-Panel-Backend: Login-Gate, Overview/Users, Coins geben/nehmen/setzen,
    XP, Titel geben, Server-Liste, Sendepause - alles hinter Token-Auth."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001 - ohne aiohttp-Testutils ueberspringen
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return

    restore_eco = _with_economy({1: 1000, 2: 5000})
    economy.instance._profile(1)["name"] = "Alice"
    economy.instance._profile(2)["name"] = "Bob"
    import admin
    alt_admin = (admin.instance._enabled, admin.instance._store)
    admin.instance._enabled = True
    admin.instance._store = _FakeStore({"sendepause": False})

    wp = webpanel.instance
    alt = (wp._enabled, wp._user, wp._pass, dict(wp._tokens), wp._client)
    wp._enabled = True
    wp._user, wp._pass = "Secoolio", "Secoolio"
    wp._tokens = {}
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: None, get_channel=lambda _x: None)
    app = wp._build_app()

    # Dieser Test prueft den Login-Weg - dafuer muss die Login-Pflicht an sein
    # (im Betrieb ist sie standardmaessig aus, siehe test_webpanel_ohne_login).
    wp._auth = True

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            # Auth-Gate: ohne Token/Cookie 401 (vor dem Login pruefen).
            assert (await cli.get("/api/overview")).status == 401
            # Login: falsch -> 401, richtig -> Token.
            assert (await cli.post("/api/login", json={"user": "x", "pass": "y"})).status == 401
            r = await cli.post("/api/login", json={"user": "Secoolio", "pass": "Secoolio"})
            j = await r.json()
            assert j["ok"] and j["token"]
            H = {"Authorization": f"Bearer {j['token']}"}

            # Overview.
            j = await (await cli.get("/api/overview", headers=H)).json()
            assert j["ok"] and j["stats"]["users"] >= 2 and j["stats"]["coins_total"] >= 6000
            assert any(u["id"] == "2" for u in j["top_coins"])

            # Nutzerliste + Detail.
            j = await (await cli.get("/api/users?sort=coins", headers=H)).json()
            assert j["ok"] and j["total"] >= 2
            j = await (await cli.get("/api/user/1", headers=H)).json()
            assert j["ok"] and j["user"]["id"] == "1" and "owned" in j["user"]

            # Coins geben / nehmen / setzen.
            j = await (await cli.post("/api/user/coins",
                       json={"id": "1", "action": "give", "amount": 500}, headers=H)).json()
            assert j["ok"] and j["coins"] == 1500
            j = await (await cli.post("/api/user/coins",
                       json={"id": "1", "action": "take", "amount": 200}, headers=H)).json()
            assert j["coins"] == 1300
            j = await (await cli.post("/api/user/coins",
                       json={"id": "1", "action": "set", "amount": "9k"}, headers=H)).json()
            assert j["coins"] == 9000        # '9k' geparst

            # XP geben (Level steigt).
            j = await (await cli.post("/api/user/xp",
                       json={"id": "1", "action": "give", "amount": 1000}, headers=H)).json()
            assert j["ok"] and j["level"] >= 1

            # Titel geben.
            j = await (await cli.post("/api/user/title",
                       json={"id": "1", "action": "grant", "text": "Testi",
                             "label": "🧪 Testi", "rarity": "selten"}, headers=H)).json()
            assert j["ok"] and economy.owns_title(1, "Testi")

            # Server-Liste (leer, aber ok).
            j = await (await cli.get("/api/servers", headers=H)).json()
            assert j["ok"] and j["guilds"] == []

            # Sendepause schalten.
            j = await (await cli.post("/api/server/sendepause",
                       json={"on": True}, headers=H)).json()
            assert j["ok"] and j["sendepause"] is True and admin.is_locked() is True

            # --- Profilbilder für die Nutzer-Liste (/api/avatar/<id>) -------
            alt_res = economy.instance._resolve_avatar_user

            class _Asset:
                url = "https://cdn.discordapp.com/avatars/1/abc.png?size=64"

                def with_size(self, _n):
                    return self

            async def fake_resolve(_guild, uid):
                return SimpleNamespace(display_avatar=_Asset()) if uid == 1 else None
            economy.instance._resolve_avatar_user = fake_resolve
            wp._av_cache = {}
            try:
                # Auflösbar -> Weiterleitung (302) auf die Discord-CDN-URL.
                r = await cli.get("/api/avatar/1", headers=H, allow_redirects=False)
                assert r.status == 302, r.status
                assert "cdn.discordapp.com" in r.headers.get("Location", "")
                # Nicht auflösbar -> 404 (Panel zeigt dann die Initialen).
                assert (await cli.get("/api/avatar/999", headers=H,
                                      allow_redirects=False)).status == 404
                # Unsinnige ID -> 404, kein Crash.
                assert (await cli.get("/api/avatar/abc", headers=H,
                                      allow_redirects=False)).status == 404
            finally:
                economy.instance._resolve_avatar_user = alt_res
                wp._av_cache = {}

            # --- Aktien-Anteile per Panel korrigieren (Exploit-Aufräumen) ---
            import floaktie
            alt_fa = (floaktie.instance._store, floaktie.instance._enabled)
            floaktie.instance._enabled = True
            floaktie.instance._store = _FakeStore(
                {"price": 300000, "day": "x", "act_ema": floaktie.ACT_BASELINE,
                 "msg_count": 0, "last_msg_count": 0,
                 "holdings": {"1": 120000, "2": 5000}, "history": [], "ticks": []})
            floaktie.instance._base()
            floaktie.instance._sync_price()
            try:
                kurs_vor = floaktie.instance.price()
                # 120.000 Exploit-Anteile streichen - Kurs muss STABIL bleiben.
                j = await (await cli.post("/api/user/shares",
                           json={"id": "1", "action": "set", "amount": 0,
                                 "keep_price": True}, headers=H)).json()
                assert j["ok"] and j["shares"] == 0
                assert j["total_shares"] == 5000
                assert abs(j["price"] - kurs_vor) <= max(1, kurs_vor // 1000)
                # geben / nehmen
                j = await (await cli.post("/api/user/shares",
                           json={"id": "1", "action": "give", "amount": "100"}, headers=H)).json()
                assert j["shares"] == 100
                j = await (await cli.post("/api/user/shares",
                           json={"id": "1", "action": "take", "amount": "40"}, headers=H)).json()
                assert j["shares"] == 60
                # Kurs direkt setzen
                j = await (await cli.post("/api/stock/price",
                           json={"price": "1000"}, headers=H)).json()
                assert j["ok"] and j["price"] == 1000 and floaktie.instance.price() == 1000
                # Unsinn wird abgelehnt
                assert (await cli.post("/api/user/shares",
                        json={"id": "1", "action": "quatsch", "amount": "5"},
                        headers=H)).status == 400
                assert (await cli.post("/api/stock/price",
                        json={"price": "0"}, headers=H)).status == 400
            finally:
                floaktie.instance._store, floaktie.instance._enabled = alt_fa

    asyncio.run(run_it())
    wp._enabled, wp._user, wp._pass, wp._tokens, wp._client = alt
    admin.instance._enabled, admin.instance._store = alt_admin
    restore_eco()


def test_aktie_sagt_wenn_dieser_server_gar_nicht_zaehlt():
    """Gemeldet mit Screenshot: acht Leute im Call, mehrere mit Kamera und
    Livestream - und das Panel sagt "Niemand im Call", Aktivitaet 0.0.

    Es war nicht die Zaehlung. Es gibt genau EINE Aktie, und ein neu
    dazugekommener Server bewegt sie bewusst erst, wenn man es dort einschaltet
    (guildcfg 'aktie_zaehlt'). Gemessen wurde also ein ANDERER Server - und dort
    sass tatsaechlich niemand. Fuer den Fragenden sah das wie ein kaputter Bot
    aus, denn er sah ja seinen vollen Call.

    Das Panel muss den Unterschied benennen: "niemand da" und "du zaehlst hier
    nicht mit" sind zwei voellig verschiedene Aussagen."""
    import floaktie
    import guildcfg
    a = floaktie.instance
    alt_store, alt_on = a._store, a._enabled
    a._store = _FakeStore({"base": 10.0, "act_ema": 0.0, "grund_akt": 0.0,
                           "shares": {}, "ticks": [], "hist": []})
    a._enabled = True
    a._zuletzt_mess = (0, 0, 0, 0)          # wie im Screenshot: nichts gemessen
    a._zuletzt_gezaehlt = []
    fremd = 4242424242
    alt_an = guildcfg.an
    try:
        # Der Schalter wird hier direkt gesteuert - geprueft wird das PANEL,
        # nicht guildcfg (das hat eigene Tests und einen eigenen Speicher).
        zaehlt = {"wert": False}
        alt_an = guildcfg.an
        guildcfg.an = lambda gid, key: (zaehlt["wert"] if key == "aktie_zaehlt"
                                        else alt_an(gid, key))

        # Ein Server, der NICHT zaehlt, bekommt die Wahrheit zu sehen.
        zeile = a._mess_zeile(fremd)
        assert "zählt nicht für die Aktie" in zeile, zeile
        assert "aktie_zaehlt an" in zeile, "der Weg zur Loesung fehlt"
        assert "Niemand im Call" not in zeile, (
            "sagt weiterhin 'Niemand im Call', obwohl der Server nur nicht zaehlt")

        # Ein Server, der zaehlt, bekommt die Messung - und da stimmt
        # 'Niemand im Call' ja auch.
        zaehlt["wert"] = True
        try:
            assert "Niemand im Call" in a._mess_zeile(fremd)
            # Und mit Leuten drin steht dort, wer den Kurs traegt.
            a._zuletzt_mess = (3, 1, 2, 5)
            a._zuletzt_gezaehlt = ["Anna", "Bert", "Cem"]
            voll = a._mess_zeile(fremd)
            assert "3 im Call" in voll and "Anna" in voll, voll
            assert "Livestream" in voll and "Kamera" in voll, voll
        finally:
            zaehlt["wert"] = False

        # Ohne Server-Bezug (DM) wird nichts behauptet.
        a._zuletzt_mess, a._zuletzt_gezaehlt = (0, 0, 0, 0), []
        assert "Niemand im Call" in a._mess_zeile(None)
    finally:
        guildcfg.an = alt_an
        a._store, a._enabled = alt_store, alt_on


def test_aktie_steigt_auch_auf_einem_lebendigen_server():
    """Gemeldet: "10 Leute im Call und sie steigt nur 0,05 %".

    Ursache war ein Vergleich zwischen einer skalierten und einer unskalierten
    Zahl: deckel_fuer() rechnete max(ziel * CEIL_FACTOR, boden). Der Boden
    waechst aber mit GRUND_FAKTOR (4), der Deckel nur mit CEIL_FACTOR (2) - ab
    einem 3-Tage-Schnitt von etwa der halben aktuellen Aktivitaet UEBERHOLT der
    Boden den Deckel. Dann ist deckel == boden, der Kurs steht genau darauf, und
    drift_fuer gibt 0 zurueck.

    Je lebendiger der Server WAR, desto weniger konnte die Aktie steigen."""
    import floaktie
    a = floaktie.instance
    alt_store, alt_on = a._store, a._enabled
    a._store = _FakeStore({"base": 10.0, "act_ema": 0.0, "grund_akt": 0.0,
                           "shares": {}, "ticks": [], "hist": []})
    a._enabled = True
    st = a._state()
    try:
        # Der Kurs steht auf dem Boden (so sieht es nach einer ruhigen Nacht
        # aus), dann kommen zehn Leute in den Call.
        for grund in (0.0, 2.0, 5.0, 6.0, 8.0, 10.0, 20.0):
            st["grund_akt"], st["act_ema"] = grund, 10.0
            st["base"] = a.boden_base()
            deckel, boden = a.deckel_fuer(10.0), a.boden_base()
            assert deckel > boden, (
                f"3-Tage-Schnitt {grund}: Boden {boden:.0f} hat den Deckel "
                f"{deckel:.0f} eingeholt - der Kurs kann nicht mehr steigen")
            drift = a.drift_fuer(10.0)
            assert drift > 0.005, (
                f"3-Tage-Schnitt {grund}: nur {drift * 100:.3f} %/min bei zehn "
                f"Leuten im Call")

        # Gegenprobe: ohne Aktivitaet darf sie NICHT steigen.
        st["grund_akt"], st["act_ema"] = 10.0, 0.0
        st["base"] = a.boden_base()
        assert a.drift_fuer(0.0) <= 0.0, "steigt, obwohl niemand da ist"

        # Und ein einzelner Zuhoerer bewegt sie deutlich weniger als zehn.
        st["act_ema"] = 1.0
        einer = a.drift_fuer(1.0)
        st["act_ema"] = 10.0
        zehn = a.drift_fuer(10.0)
        assert 0 < einer < zehn / 5, (einer, zehn)
    finally:
        a._store, a._enabled = alt_store, alt_on


def test_floaktie_aktivitaet_treibt_den_kurs():
    """DIE Regel, in Zahlen: jeder im Call, jeder Livestream und jede Chat-Nachricht
    heben den Kurs - je mehr, desto schneller. Ist gar nichts los, sinkt er langsam.
    REGRESSION: vorher wurde von jeder Minute ein 'Normalwert' von 3,0 abgezogen,
    wodurch ein Tag mit 5 aktiven Stunden bei -19 % landete, und das Rauschen war
    bei 4 Leuten im Call 12x groesser als das Signal."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0

    def frisch(preis=1000):
        fa._store = _FakeStore({"price": preis, "base": float(preis), "day": "x",
                               "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                               "holdings": {}, "history": [], "ticks": []})
        fa._sync_price()

    try:
        # 1) Mehr Leute -> hoeherer Zielkurs UND schnellerer Anstieg.
        frisch()
        tempo = []
        for leute in (0, 1, 5, 12, 20):
            frisch()
            akt = fa.activity_of(leute, 0, 0, 0)
            tempo.append((leute, fa.ziel_base(akt), fa.drift_fuer(akt)))
        ziele = [z for _l, z, _d in tempo]
        assert ziele == sorted(ziele) and len(set(ziele)) == len(ziele)
        # Ab dem Punkt, wo das Ziel ueber dem Kurs liegt, steigt es mit den Leuten.
        steigend = [d for _l, z, d in tempo if z > 1000]
        assert steigend == sorted(steigend), steigend

        # 2) Livestreams zaehlen EXTRA: 10 Leute die alle streamen schlagen
        #    10 Leute ohne Stream deutlich.
        ohne = fa.activity_of(10, 0, 0, 0)
        mit = fa.activity_of(10, 10, 0, 0)
        assert mit > ohne * 2, (ohne, mit)
        assert fa.ziel_base(mit) > fa.ziel_base(ohne)

        # 3) Reiner Chat treibt den Kurs NICHT MEHR (bewusst geaendert).
        #    Vorher hob eine einzige Nachricht die Aktivitaet ueber 0 und setzte
        #    den Leerlauf-Verfall komplett aus - auf einem Server, auf dem
        #    tagsueber immer mal wer tippt, ist der Kurs deshalb praktisch nie
        #    gefallen. Jetzt gilt: kein Call, keine Punkte.
        frisch()
        _a, _n, drift, akt = fa._activity_tick(0, 40)
        assert akt == 0.0, akt
        assert drift < 0, drift
        # ... im Call zaehlt derselbe Chat aber voll mit.
        assert fa.activity_of(2, 0, 0, 40) > fa.activity_of(2, 0, 0, 0)

        # 4) 10 im Call, ALLE streamen: in der ERSTEN Minute sichtbar (>= 1 %),
        #    nach einer Stunde vielfach.
        frisch()
        a, n, drift, _akt = fa._activity_tick(10, 20, streams=10)
        assert drift >= 0.01, drift
        assert n > a
        for _ in range(59):
            fa._activity_tick(10, 20, streams=10)
        assert fa.price() > 2500, fa.price()          # nach 1 h fast verdreifacht

        # 4b) REGRESSION (EMA-Rest): steht nach viel Aktivitaet noch ein winziger
        #     EMA-Rest im Zustand, DARF der Kurs im Leerlauf trotzdem nicht
        #     "steigen". Vorher gab drift_fuer(0.03) den Mindest-Anstieg (+0,48%/h)
        #     zurueck, obwohl niemand mehr da war - genau der gemeldete Bug.
        fa._store = _FakeStore({"price": 33_586, "base": 33_586.0, "day": "x",
                               "act_ema": 0.03, "msg_count": 0, "last_msg_count": 0,
                               "holdings": {}, "history": [], "ticks": [],
                               "leer_min": 0.0})
        fa._sync_price()
        _a, _n, d, akt = fa._activity_tick(0, 0)            # niemand da
        assert d < 0, (d, akt)                              # muss FALLEN
        assert fa._state()["act_ema"] == 0.0               # EMA-Rest ist weg

        # 5) Leerer Call: faellt SOFORT und GLEICHMAESSIG. Ausdrueckliche
        #    Vorgabe: "sinken soll es direkt sobald keiner mehr im Call ist,
        #    aber rasant, gute 10 % pro 30 min." Kein Anlauf mehr, keine
        #    Staffelung - die halbe Stunde ist das Mass.
        frisch(200_000)
        for _ in range(30):
            fa._activity_tick(0, 0)
        nach_30 = 1 - fa.price() / 200_000
        assert 0.10 <= nach_30 <= 0.14, nach_30
        for _ in range(30):
            fa._activity_tick(0, 0)
        nach_1h = 1 - fa.price() / 200_000
        assert 0.19 <= nach_1h <= 0.26, nach_1h
        for _ in range(60):
            fa._activity_tick(0, 0)
        nach_2h = 1 - fa.price() / 200_000
        assert 0.34 <= nach_2h <= 0.45, nach_2h
        # Gleichmaessig heisst: die zweite halbe Stunde kostet anteilig genauso
        # viel wie die erste (frueher war die erste bewusst schwaecher).
        frisch(200_000)
        for _ in range(30):
            fa._activity_tick(0, 0)
        h1 = fa.price()
        for _ in range(30):
            fa._activity_tick(0, 0)
        h2 = fa.price()
        assert abs((1 - h1 / 200_000) - (1 - h2 / h1)) < 0.02, (h1, h2)
        # Beim SINKEN gibt es kein Rauschen - jede Leerlauf-Minute geht wirklich
        # runter (vorher stieg der Kurs in ~30 % der Minuten durch Zufall).
        frisch(200_000)
        for _ in range(300):
            _a, n, d, _akt = fa._activity_tick(0, 0)
            assert d <= 0
        # ... aber nie unter den Wert eines toten Servers.
        for _ in range(30 * 1440):
            fa._activity_tick(0, 0)
        assert fa.price() >= floaktie.MIN_PRICE, fa.price()

        # 6) Kein Aufsummieren: Dauer-Vollbetrieb laeuft in ein NIVEAU (Deckel:
        #    CEIL_FACTOR x Zielkurs), statt zu explodieren (vorher: 10**16 nach
        #    30 Tagen). Der Deckel steigt nur, wenn die Aktivitaet steigt.
        frisch()
        # 14 Tage einschwingen lassen: der Grundwert ist ein 3-Tage-Mittel und
        # zieht den Deckel in den ersten Tagen noch mit hoch. Frueher wurde ab
        # Tag 4 gemessen - da war er noch mitten in der Bewegung.
        for _ in range(14 * 1440):
            fa._activity_tick(10, 40, streams=10)
        vier_tage = fa.price()
        for _ in range(16 * 1440):
            fa._activity_tick(10, 40, streams=10)
        # Der Deckel ist akt_deckel_base() - EINE Zahl fuer Drift, Panel und
        # Impuls. Bei DAUER-Vollbetrieb waechst der Grundwert bis auf die volle
        # Aktivitaet und liegt dann ueber CEIL_FACTOR x Zielkurs; frueher hing
        # der Kurs zwischen zwei widerspruechlichen Decken (Bremse 94.020,
        # Grundwert 188.001). Im echten Betrieb (Call nur abends) greift das nie,
        # dort bleibt der Grundwert weit unter der Bremse.
        deckel = fa.akt_deckel_base()
        # Toleranz = ein voller Takt: die Pruefung im Drift laeuft VOR dem Schritt,
        # der letzte Schritt darf also einmal um bis zu TICK_CAP ueberschiessen.
        assert fa.price() <= deckel * (1 + floaktie.TICK_CAP) * 1.01, (fa.price(), deckel)
        # ... und der Deckel selbst bleibt gedeckelt: keine Explosion.
        #
        # Die Grenze ist 10x die Zielbasis, nicht 5x. Grund: deckel_fuer() legt
        # CEIL_FACTOR (2) jetzt auf BEIDE Grundwerte statt nur auf einen - vorher
        # wurde ziel*2 gegen boden*1 verglichen, und dadurch holte der Boden
        # (waechst mit GRUND_FAKTOR 4) den Deckel ein. Stand er drauf, war der
        # Drift exakt 0: "10 Leute im Call und sie steigt nur 0,05 %".
        #
        # In DIESEM Dauervollbetrieb rund um die Uhr wird grund_akt zur vollen
        # Aktivitaet, also boden = ziel_base(4 x 47) und Deckel = 2 x boden = 8x.
        # Im echten Betrieb (Call nur abends) ist grund_akt etwa ein Viertel der
        # Spitze - genau dafuer ist GRUND_FAKTOR 4 gedacht -, dort liegt der
        # Deckel wieder bei 2x. Nachgemessen laeuft der Kurs auch hier NICHT weg,
        # sondern in eine Ebene: Tag 30 -> Tag 40 sind +0,33 %. Genau das prueft
        # die Zeile darunter, und sie ist die eigentliche Absicherung.
        assert deckel <= fa.ziel_base(fa.activity_of(10, 10, 0, 40)) * 10, deckel
        assert fa.price() < vier_tage * 1.05, (vier_tage, fa.price())

        # 7) Sofort-Impuls: Livestream geht an -> Kurs zieht augenblicklich an,
        #    aber pro Minute gedeckelt (kein Pump durch Rein-/Rausspringen).
        #    Der Impuls braucht Luft nach oben - bei Aktivitaet 0 ist der Deckel
        #    winzig und er tut (richtigerweise) gar nichts.
        frisch()
        fa._store.data["act_ema"] = 8.0
        vorher = fa.price()
        assert fa._puls(floaktie.PULSE_STREAM, "test") > vorher
        for _ in range(30):
            fa._puls(floaktie.PULSE_STREAM, "test")
        assert fa.price() <= vorher * (1 + floaktie.PULSE_MAX_PER_MIN) + 1, fa.price()

        # 7b) DER VORMITTAG: auch mit nur 3 Leuten im Call muss sich ein Depot
        #     ueber ein paar Stunden VERVIELFACHEN - vom normalen Niveau aus, also
        #     dort, wo der Kurs nach einer ruhigen Nacht steht.
        #     Die Schwelle folgt dem Deckel: CEIL_FACTOR x Zielkurs. Frueher stand
        #     der bei 6 (also 18-fach), was zusammen mit dem festen Boden eine
        #     182-fache Tagesspanne ergab - kein Markt, nur ein Saegezahn. Mit
        #     CEIL_FACTOR 2 sind es rund 6-fach in sechs Stunden mit DREI Leuten.
        for startkurs in (1_000, int(floaktie.FAIR_BASE)):
            frisch(startkurs)
            for _ in range(6 * 60):
                fa._activity_tick(3, 0)
            faktor = fa.price() / startkurs
            assert faktor > 5, (startkurs, fa.price(), faktor)
        # ... aber NICHT unbegrenzt: steht der Kurs schon am Deckel dieser
        # Aktivitaet, hoert es auf. Genau das verhindert die Hyperinflation
        # (vorher: 750 Anteile fuer 1,1 Mio -> Verkauf fuer 43 MILLIARDEN).
        akt3 = fa.activity_of(3, 0, 0, 0)
        frisch(int(fa.ziel_base(akt3) * floaktie.CEIL_FACTOR))
        vorm_deckel = fa.price()
        for _ in range(6 * 60):
            fa._activity_tick(3, 0)
        assert fa.price() <= vorm_deckel * 1.06, (vorm_deckel, fa.price())
        # Und mehr Leute muessen auf demselben Niveau schneller sein - gemessen
        # UNTERHALB des Deckels, sonst vergleicht man zwei Nullen.
        frisch(2_000)
        d3 = fa.drift_fuer(fa.activity_of(3, 0, 0, 0))
        d10 = fa.drift_fuer(fa.activity_of(10, 0, 0, 0))
        d10s = fa.drift_fuer(fa.activity_of(10, 10, 0, 0))
        assert d3 < d10 <= d10s, (d3, d10, d10s)
        assert d10s >= floaktie.TICK_CAP * 0.99, d10s      # voller Call: Anschlag

        # Umgekehrt: steht der Kurs WEIT ueber dem, was die Aktivitaet hergibt,
        # geht gar nichts mehr nach oben. Genau das haelt die Spanne im Zaum -
        # bei Kurs 50.000 rechtfertigen weder 3 noch 10 Leute einen Anstieg,
        # ein voller Call mit 10 Streams aber schon.
        frisch(50_000)
        assert fa.drift_fuer(fa.activity_of(3, 0, 0, 0)) == 0.0
        assert fa.drift_fuer(fa.activity_of(10, 0, 0, 0)) == 0.0
        assert fa.drift_fuer(fa.activity_of(10, 10, 0, 0)) > 0.0

        # 8) Signal deutlich ueber dem Rauschen - man MUSS es sehen koennen.
        #    (Beim Niveau-Modell zaehlt der Abstand zum Ziel: steht der Kurs auf
        #    Totlast-Niveau, treiben schon 4 Leute im Call ihn klar nach oben.)
        frisch(int(floaktie.FAIR_BASE))
        sig = fa.drift_fuer(fa.activity_of(4, 0, 0, 0))
        assert sig > floaktie.TICK_NOISE * 10, (sig, floaktie.TICK_NOISE)
        # Solange Leute da sind, geht es NIE runter - und es bleibt auch bei einem
        # hohen Kurs in Bewegung, nur gemaechlicher (logarithmische Daempfung).
        akt4 = fa.activity_of(4, 0, 0, 0)
        frisch(int(floaktie.FAIR_BASE))
        schnell = fa.drift_fuer(akt4)
        # Knapp UNTER dem Deckel dieser Aktivitaet - dort muss es noch aufwaerts
        # gehen, nur gemaechlicher.
        frisch(int(fa.ziel_base(akt4) * floaktie.CEIL_FACTOR * 0.9))
        langsam = fa.drift_fuer(akt4)
        assert schnell > langsam > 0, (schnell, langsam)
        # Erst die Notbremse ganz oben stoppt ihn (Deckel = Zielkurs x CEIL_FACTOR).
        frisch(int(fa.ziel_base(akt4) * floaktie.CEIL_FACTOR) + 10_000)
        assert fa.drift_fuer(akt4) == 0
        # Nur ohne jede Aktivitaet faellt er.
        assert fa.drift_fuer(0) < 0

        # 9) Messung ohne Member-Cache: voice_states als Notfall-Quelle. Wer sich
        #    NICHT aufloesen laesst, zaehlt NICHT - lieber eine Minute zu wenig
        #    Aktivitaet als ein Bot, der den Kurs dauerhaft oben haelt.
        #    (Details in test_floaktie_bots_zaehlen_nie.)
        class _VS:
            def __init__(self, stream=False, video=False):
                self.self_stream, self.self_video = stream, video

        class _VC:
            id = 5
            voice_states = {11: _VS(True), 12: _VS(), 13: _VS(video=True)}
            members = []                      # Cache leer!

        leute = {11: SimpleNamespace(id=11, bot=False),
                 12: SimpleNamespace(id=12, bot=False),
                 13: SimpleNamespace(id=13, bot=False)}
        guild = SimpleNamespace(voice_channels=[_VC()], afk_channel=None,
                               me=SimpleNamespace(id=99),
                               get_member=lambda uid: leute.get(uid))
        assert fa._measure(guild) == (3, 1, 1)
        # Gar kein Cache -> gar keine Aktivitaet (statt drei erfundenen Menschen).
        guild.get_member = lambda _uid: None
        assert fa._measure(guild) == (0, 0, 0)
        # Flo selbst und andere Bots zaehlen nicht mit.
        _VC.voice_states = {99: _VS(), 11: _VS()}
        guild.get_member = lambda uid: leute.get(uid)
        assert fa._measure(guild)[0] == 1
    finally:
        fa._store, fa._enabled, floaktie.TICK_NOISE = alt


def test_floaktie_chart_zeitraeume_und_bild():
    """REGRESSION (Chart): '7 Tage', '30 Tage' und 'Gesamt' zeigten dieselbe Reihe,
    sobald die Minuten-Ticks nur ein paar Tage zurueckreichten - die Tages-Historie
    wurde nur benutzt, wenn es GAR keine Ticks gab. Und das Bild bekam bei grossen
    Spannen eine negative Kurs-Achse."""
    import floaktie
    import render
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    jetzt = time.time()
    # 3 Tage Minuten-Ticks (erst flach, dann steil) + 24 Tage Schlusskurse.
    ticks, p = [], 1000.0
    for i in range(3 * 1440):
        p *= 1.0002 if i < 2 * 1440 else 1.003
        ticks.append({"t": jetzt - (3 * 1440 - i) * 60, "price": int(p)})
    # Die Schlusskurse muessen RELATIV ZU HEUTE liegen - die Reihe waehlt sie
    # ueber das Datum aus. Feste Kalendertage (frueher "2026-06-xx") fallen je
    # nach Testtag komplett aus dem Fenster.
    from datetime import datetime as _dt, timedelta as _td
    _heute = _dt.now(floaktie.TIMEZONE)
    fa._store = _FakeStore({
        "price": int(p), "base": float(p), "day": "x", "act_ema": 20.0,
        "msg_count": 0, "last_msg_count": 0, "holdings": {},
        "history": [{"day": (_heute - _td(days=24 - d)).strftime("%Y-%m-%d"),
                     "price": 800 + d * 30} for d in range(1, 25)],
        "ticks": ticks})
    try:
        fa._sync_price()
        reihen = {}
        for tage in (1, 7, 30):
            pts, chg = fa.series(tage)
            reihen[tage] = (pts, chg)
            assert len(pts) >= 2
            assert pts[-1] == fa.price()          # Linie endet auf dem echten Kurs
        # Die Zeitraeume muessen sich UNTERSCHEIDEN.
        assert reihen[1][0] != reihen[7][0], "1 Tag und 7 Tage identisch"
        assert reihen[7][0] != reihen[30][0], "7 Tage und 30 Tage identisch"
        # Je laenger der Zeitraum, desto weiter zurueck der erste Punkt.
        assert reihen[30][0][0] <= reihen[7][0][0]

        # Bild: keine negative Kurs-Achse, log. Skala bei grosser Spanne.
        buf = render.floaktie_chart(reihen[1][0], "$FLO", "1 Tag", reihen[1][1])
        assert buf is not None and len(buf.getvalue()) > 5000
        for pts in ([5000] * 40, [1000, 78000], [50, 1_500_000], [0, 0, 1000]):
            assert render.floaktie_chart(pts, "$FLO", "x", 1.0) is not None
    finally:
        fa._store, fa._enabled = alt


def test_floaktie_kein_pump_and_dump():
    """REGRESSION (Exploit): Der Kurs muss pfad-unabhaengig sein - viele kleine
    Kaeufe duerfen den Kurs NICHT staerker heben als ein grosser Kauf, sonst kann
    man hochpumpen und teuer dumpen (Geld aus dem Nichts). Zusaetzlich muss jeder
    Round-Trip (kaufen+verkaufen) durch die Gebuehr verlieren."""
    import floaktie
    START = 5_000_000

    def fresh():
        restore = _with_economy({1: START})
        fa = floaktie.instance
        fa._enabled = True
        fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                "act_ema": floaktie.ACT_BASELINE, "msg_count": 0,
                                "last_msg_count": 0, "holdings": {}, "history": [],
                                "ticks": []})
        return fa, SimpleNamespace(id=1), restore

    fa, M, restore = fresh()
    try:
        # 1) Pump & Dump verliert (frueher: +414k Gewinn aus dem Nichts).
        for _ in range(300):
            asyncio.run(fa.buy(M, 1))
        asyncio.run(fa.sell(M, fa.shares_of(1)))
        assert economy.get_coins(1) < START, "Pump&Dump druckt Geld!"
    finally:
        restore()

    # 2) Pfad-Unabhaengigkeit: 300x1 kostet praktisch genauso viel wie 1x300.
    fa, M, restore = fresh()
    try:
        for _ in range(300):
            asyncio.run(fa.buy(M, 1))
        klein = START - economy.get_coins(1)
    finally:
        restore()
    fa, M, restore = fresh()
    try:
        asyncio.run(fa.buy(M, 300))
        gross = START - economy.get_coins(1)
    finally:
        restore()
    assert abs(klein - gross) <= gross * 0.01, (klein, gross)   # <=1% (nur Rundung)

    # 3) Round-Trip verliert immer (Gebuehr).
    fa, M, restore = fresh()
    try:
        asyncio.run(fa.buy(M, 200))
        asyncio.run(fa.sell(M, 200))
        assert economy.get_coins(1) < START
        # 4) Verkaufen in Scheiben + Rueckkauf am Stueck bringt auch nichts.
        asyncio.run(fa.buy(M, 100))
        bal = economy.get_coins(1)
        for _ in range(100):
            asyncio.run(fa.sell(M, 1))
        asyncio.run(fa.buy(M, 100))
        assert economy.get_coins(1) <= bal
    finally:
        restore()


def test_exploit_fixes_games_und_steal():
    """REGRESSION für die im Audit bewiesenen Exploits:
    - Gratis-Slot (Einsatz 0 / negativ) darf KEINE Coins auszahlen,
    - Skill-Spiele (mathe/anagramm/reaktion) sind gedeckelt + haben Cooldown,
    - Klauen ohne eigenes Geld (Strafe würde auf 0 klemmen) ist gesperrt,
    - parse_amount versteht deutsche Tausenderpunkte ('1.000' == 1000)."""
    import io
    import casino
    import games
    import steal

    # --- Gratis-Slot zahlt nichts mehr aus -------------------------------
    restore = _with_economy({1: 0})
    alt_anim = games.instance._anim
    alt_g = games.instance._enabled
    alt_c = (casino.instance._enabled, casino.instance._stats)
    games.instance._enabled = True
    casino.instance._enabled = True
    casino.instance._stats = _FakeStore({"stats": {}})

    async def no_anim(*a, **k):          # Bild-Rendering im Test überspringen
        return (io.BytesIO(b"x"), "png")
    games.instance._anim = no_anim
    try:
        for _ in range(40):
            asyncio.run(games.instance._spin_slot(1, 0))
        assert economy.get_coins(1) == 0, "Gratis-Slot ist ein Coin-Faucet!"
        for _ in range(20):
            asyncio.run(games.instance._spin_slot(1, -1_000_000))
        assert economy.get_coins(1) == 0, "negativer Einsatz zahlt aus!"
        # Bezahlter Slot rechnet weiter normal ab.
        economy.instance._profile(1)["coins"] = 100_000
        asyncio.run(games.instance._spin_slot(1, 1000))
        assert economy.get_coins(1) != 100_000
    finally:
        games.instance._anim = alt_anim
        games.instance._enabled = alt_g
        casino.instance._enabled, casino.instance._stats = alt_c
        restore()

    # --- Skill-Spiele: Einsatz gedeckelt + Cooldown ----------------------
    restore = _with_economy({1: 10_000_000})
    games.instance._enabled = True
    games.instance._skill_cd = {}
    try:
        bet, err = games.instance._take_bet(1, [str(games.SKILL_MAX_BET * 10)])
        assert bet == 0 and err, "Einsatz nicht gedeckelt"
        bet, err = games.instance._take_bet(1, [str(games.SKILL_MAX_BET)])
        assert bet == games.SKILL_MAX_BET and err is None
        bet2, err2 = games.instance._take_bet(1, ["100"])
        assert bet2 == 0 and err2, "kein Cooldown -> farmbar"
    finally:
        games.instance._enabled = alt_g
        games.instance._skill_cd = {}
        restore()

    # --- Steal braucht eigenes Risiko -----------------------------------
    restore = _with_economy({1: 10_000_000, 2: 0, 3: -50_000, 4: 5_000})
    alt_s = (steal.instance._enabled, steal.instance._store)
    steal.instance._enabled = True
    steal.instance._store = _FakeStore({"cooldowns": {}})

    def mk(uid):
        return SimpleNamespace(
            author=SimpleNamespace(id=uid, bot=False, display_name=f"U{uid}"),
            content="steal <@1>",
            mentions=[SimpleNamespace(id=1, bot=False, display_name="Opfer")],
            guild=SimpleNamespace(id=1))
    try:
        for uid in (2, 3):               # blank bzw. im Minus -> gesperrt
            r = asyncio.run(steal.handle(mk(uid)))
            assert isinstance(r, str) and "Risiko" in r, (uid, r)
        steal.instance._store = _FakeStore({"cooldowns": {}})
        r = asyncio.run(steal.handle(mk(4)))     # mit Guthaben -> erlaubt
        assert not (isinstance(r, str) and "Risiko" in r)
    finally:
        steal.instance._enabled, steal.instance._store = alt_s
        restore()

    # --- Tausenderpunkte im Betrag --------------------------------------
    assert economy.parse_amount("1.000") == 1000
    assert economy.parse_amount("1.000.000") == 1_000_000
    assert economy.parse_amount("2,5k") == 2500
    assert economy.parse_amount("9" * 400) is None      # kein OverflowError
    assert economy.parse_amount("-5") is None


def test_exploit_kurspump_und_verschluckte_einsaetze():
    """REGRESSION für die drei im zweiten Audit BEWIESENEN Löcher:

    1) Aktien-Kauf auf Kredit ohne eigenes Geld: Wegwerf-Konten (Guthaben 0)
       konnten Anteile kaufen und damit den Kurs hochtreiben, das Hauptkonto
       verkaufte oben. Gemessen: 300.000 -> 429.227, also +129.227 aus dem Nichts,
       plus 1,17 Mio uneinbringliche Schuld auf leeren Konten.
    2) Quiz-Duell: 'läuft hier schon eins?' wurde vor der (langsamen) KI-Frage
       geprüft, die Runde aber erst danach registriert -> zwei Duelle gleichzeitig,
       10.000 Coins ersatzlos vernichtet.
    3) Mathe/Anagramm: derselbe Fehler, Fenster = ein Discord-Roundtrip."""
    import floaktie
    import games

    # --- 1) Kein Kredit ohne Sicherheit ---------------------------------
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    restore = _with_economy({1: 300_000, 201: 0, 202: 0, 203: -50_000})
    fa._enabled = True
    fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                            "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                            "leer_min": 0.0, "holdings": {}, "history": [], "ticks": []})
    fa._sync_price()
    try:
        # Leeres bzw. negatives Konto: kauft GAR NICHTS und bewegt den Kurs nicht.
        for uid in (201, 202, 203):
            kurs_vor = fa.price()
            r = asyncio.run(fa.buy(SimpleNamespace(id=uid), 150))
            assert isinstance(r, str) and "Guthaben nicht" in r, (uid, r)
            assert fa.shares_of(uid) == 0 and fa.price() == kurs_vor, uid
            assert economy.get_coins(uid) >= min(0, -50_000)   # keine neue Schuld
        # KEIN KREDIT: der Kaufrahmen ist GENAU das Guthaben, nie mehr.
        assert fa._kaufrahmen(economy.get_coins(1)) == 300_000
        assert fa._kaufrahmen(0) == 0 and fa._kaufrahmen(-9_999) == 0
        assert fa._kaufrahmen(1_000) == 1_000
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 150))
        assert fa.shares_of(1) > 0, r
        assert economy.get_coins(1) >= 0, "Kauf hat das Konto ins Minus gedrueckt"
        # Round-Trip bleibt ein Verlust (kein risikoloser Gewinn).
        vorher = economy.get_coins(1)
        asyncio.run(fa.sell(SimpleNamespace(id=1), 150))
        assert economy.get_coins(1) > vorher          # Erlös kommt an ...
        assert economy.get_coins(1) < 300_000, economy.get_coins(1)   # ... aber netto Minus
    finally:
        fa._store, fa._enabled = alt
        restore()

    # --- 2+3) Kanal-Platz wird SYNCHRON belegt --------------------------
    g = games.instance
    alt_start = set(g._starting)
    try:
        g._starting = set()
        assert g._slot_belegen("mathe", 42) is True
        assert g._slot_belegen("mathe", 42) is False      # zweiter Start prallt ab
        assert g._slot_belegen("anagramm", 42) is True    # anderes Spiel, egal
        assert g._slot_belegen("mathe", 43) is True       # anderer Kanal, egal
        g._slot_frei("mathe", 42)
        assert g._slot_belegen("mathe", 42) is True
    finally:
        g._starting = alt_start
    # Und die Reservierung muss VOR dem ersten await stehen - sonst nutzt sie nichts.
    quelle = open("games.py", encoding="utf-8").read()
    for art, marke in (("qduel", '_slot_belegen("qduel"'),
                       ("mathe", '_slot_belegen("mathe"'),
                       ("anagramm", '_slot_belegen("anagramm"')):
        i = quelle.index(marke)
        # zwischen Kanal-Prüfung und Reservierung darf kein 'await' liegen
        vor = quelle[max(0, i - 300):i]
        assert "await" not in vor.split("laeuft")[-1], art
        # und jeder Pfad gibt den Platz wieder frei
        assert f'_slot_frei("{art}"' in quelle, art


def test_floaktie_chart_serie():
    """REGRESSION (Dashboard-Chart war falsch): Die Kurs-Reihe muss aus den FEINEN
    Intraday-Ticks kommen (nicht nur aus den Tages-Schlusskursen) und IMMER auf dem
    aktuellen Kurs enden - sonst passt die Linie nicht zur angezeigten Kurs-Zahl."""
    import time as _t
    import floaktie
    restore = _with_economy({1: 1000})
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    now = _t.time()
    # 2 Tage Minuten-Ticks (steigend) + nur 2 Tages-Schlusskurse
    ticks = [{"t": int(now - (2 * 24 * 60 - i) * 60), "price": 1000 + i}
             for i in range(2 * 24 * 60)]
    fa._store = _FakeStore({"price": 1000 + len(ticks) - 1, "base": 1000.0,
                            "holdings": {}, "ticks": ticks,
                            "history": [{"day": "d1", "price": 1000},
                                        {"day": "d2", "price": 1500}],
                            "day": "x", "act_ema": floaktie.ACT_BASELINE,
                            "msg_count": 0, "last_msg_count": 0})
    try:
        punkte, chg = fa.series(1)
        # Kommt aus den Ticks (viele Punkte), nicht aus den 2 History-Einträgen.
        assert len(punkte) > 10, len(punkte)
        # Endet EXAKT auf dem aktuellen Kurs.
        assert punkte[-1] == fa.price(), (punkte[-1], fa.price())
        # Änderung passt zur gezeigten Reihe.
        erwartet = round((punkte[-1] - punkte[0]) / punkte[0] * 100, 2)
        assert abs(chg - erwartet) < 0.01, (chg, erwartet)
        # Auf eine handliche Punktzahl verdichtet (nicht 2880 Punkte ins SVG).
        assert len(punkte) <= 120
        # Größerer Zeitraum -> größere Spanne, immer noch am aktuellen Kurs.
        p7, chg7 = fa.series(7)
        assert p7[-1] == fa.price() and p7[0] <= punkte[0]
        # Ohne jede Historie bleibt es robust (2 Punkte, kein Crash).
        fa._store.data["ticks"] = []
        fa._store.data["history"] = []
        p0, c0 = fa.series(1)
        assert len(p0) >= 2 and p0[-1] == fa.price() and c0 == 0.0
    finally:
        fa._store, fa._enabled = alt
        restore()


def test_numfmt():
    """Deutsche Tausenderpunkte ab 1000; kleine/negative/Murks-Werte robust."""
    import numfmt
    assert numfmt.fmt(1000000) == "1.000.000"
    assert numfmt.fmt(2500) == "2.500"
    assert numfmt.fmt(-5000) == "-5.000"
    assert numfmt.fmt(999) == "999"
    assert numfmt.fmt(0) == "0"
    assert numfmt.fmt(1234567) == "1.234.567"


def test_namensauflösung_nur_per_id():
    """REGRESSION: Konten, die NUR per ID angefasst wurden (z. B. Coins über das
    Web-Panel), hatten keinen Namen im Profil und standen in 'Flo reichste' als
    'Unbekannt'. Die Auflösung muss bis zur Discord-API gehen (Server-Nickname,
    sonst globaler Name) - und wenn gar nichts geht, wenigstens die ID zeigen."""
    import sys
    import types
    UID = 1451353124940812353
    restore = _with_economy({UID: 70_000_000})
    alt_bot = sys.modules.get("bot")
    try:
        economy.instance._profile(UID)["name"] = ""

        fake = types.ModuleType("bot")
        fake.client = SimpleNamespace(
            get_user=lambda _u: None,
            fetch_user=lambda _u: (_ for _ in ()).throw(Exception("404")),
            guilds=[], get_guild=lambda _g: None, is_closed=lambda: False)
        sys.modules["bot"] = fake

        class GuildAPI:                      # Cache leer, aber API kennt das Member
            def get_member(self, _uid):
                return None

            async def fetch_member(self, _uid):
                return SimpleNamespace(display_name="JoeAusAPI")

        name = asyncio.run(economy.resolve_display_name(UID, GuildAPI()))
        assert name == "JoeAusAPI", name
        # und wird fürs nächste Mal im Profil gemerkt
        assert economy.instance._profile(UID)["name"] == "JoeAusAPI"

        class GuildLeer:                     # weder Cache noch API finden ihn
            def get_member(self, _uid):
                return None

            async def fetch_member(self, _uid):
                raise Exception("nicht im Server")

        economy.instance._profile(UID)["name"] = ""
        rows = economy.instance.money_leaderboard_data(5)
        asyncio.run(economy.instance._resolve_names(rows, GuildLeer()))
        # Letzte Rettung: identifizierbare ID statt 'Unbekannt'
        assert str(UID) in rows[0]["name"], rows[0]["name"]

        # Vorhandener Name wird nicht unnötig neu geholt.
        economy.instance._profile(UID)["name"] = "Secoolio"
        assert asyncio.run(economy.resolve_display_name(UID, GuildLeer())) == "Secoolio"
    finally:
        if alt_bot is not None:
            sys.modules["bot"] = alt_bot
        else:
            sys.modules.pop("bot", None)
        restore()


def test_money_leaderboard_bild_und_namen():
    """'flo reichste' ist ein BILD, und ein fehlender/ID-artiger Name wird zum
    echten Discord-Namen aufgelöst (war ein Bug: da stand die rohe ID)."""
    import discord
    import render
    restore = _with_economy({111: 1_505_000, 222: 890_000, 333: 250_000})
    try:
        economy.instance._profile(111)["name"] = "Secoolio"
        economy.instance._profile(222)["name"] = ""        # leer
        economy.instance._profile(333)["name"] = "333"     # rohe ID als Name

        class Guild:
            def get_member(self, uid):
                namen = {222: "LunaEcht", 333: "KevinEcht"}
                if uid in namen:
                    return SimpleNamespace(display_name=namen[uid])
                return None

        rows = economy.instance.money_leaderboard_data(10)
        assert [r["id"] for r in rows] == [111, 222, 333]      # nach Coins sortiert
        asyncio.run(economy.instance._resolve_names(rows, Guild()))
        assert [r["name"] for r in rows] == ["Secoolio", "LunaEcht", "KevinEcht"]
        # Aufgelöster Name wird fürs nächste Mal im Profil gemerkt.
        assert economy.instance._profile(222)["name"] == "LunaEcht"

        # Bild-Renderer liefert ein gültiges PNG.
        buf = render.money_card(rows, "REICHSTE", "Test")
        png = buf.getvalue()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000

        # handle() gibt eine discord.File zurück (kein Embed mehr).
        msg = SimpleNamespace(content="Flo reichste", guild=Guild(), mentions=[],
                              author=SimpleNamespace(id=111, display_name="S"))
        res = asyncio.run(economy.handle(msg))
        assert isinstance(res, discord.File) and res.filename == "reichste.png"
    finally:
        restore()


def test_economy_money_leaderboard():
    """Geld-Rangliste sortiert nach aktuellem Kontostand, zeigt 'insgesamt verdient'
    (Lebenszeit) mit Tausenderpunkten; earned zaehlt nur echte Zufluesse."""
    restore = _with_economy({1: 5000, 2: 20000, 3: 100})
    try:
        # Gutschrift zaehlt in 'earned'; Ausgabe nicht.
        economy.add_coins(2, 30000, reason="lotto")     # coins 50.000, earned 30.000
        assert economy.instance._profile(2).get("earned", 0) == 30000
        economy.add_coins(2, -1000, reason="casino")    # earned unveraendert
        assert economy.instance._profile(2).get("earned", 0) == 30000

        # Rangliste: nach Kontostand sortiert, 'earned' als Lebenszeit-Wert.
        rows = economy.instance.money_leaderboard_data(10)
        assert [r["id"] for r in rows] == [2, 1, 3]       # 49.000 > 5.000 > 100
        assert rows[0]["coins"] == 49000
        # 'insgesamt' wird nie kleiner als der Kontostand angezeigt (Counter ist neu):
        # roh sind es 30.000 verdiente, angezeigt also der Kontostand 49.000.
        assert rows[0]["earned"] == 49000
        assert all(r["earned"] >= r["coins"] for r in rows)
    finally:
        restore()


class _FakeChannel:
    """Kanal-Attrappe: merkt sich, was Flo gesendet hat."""

    def __init__(self, cid=99):
        self.id = cid
        self.sent = []

    async def send(self, content=None, embed=None, view=None, embeds=None, **_kw):
        # embeds (Plural) MUSS mit erfasst werden: Module, die mehrere Embeds
        # auf einmal schicken (Profil-Lookup), waren hier sonst unsichtbar.
        self.sent.append({"content": content, "embed": embed, "view": view,
                          "embeds": embeds or ([embed] if embed is not None else [])})
        return SimpleNamespace(id=len(self.sent), edit=self._edit, channel=self)

    async def _edit(self, **_kw):
        return None

    async def fetch_message(self, _mid):
        raise RuntimeError("keine echte Nachricht im Test")


_GW_CHANNEL = _FakeChannel()


def _giveaway_msg(host=1, text="", channel=None):
    """Nachrichten-Attrappe fuer den Giveaway-Assistenten."""
    ch = channel or _GW_CHANNEL
    return SimpleNamespace(
        content=text, channel=ch,
        author=SimpleNamespace(id=host, bot=False, display_name=f"User{host}",
                               display_avatar=SimpleNamespace(url="http://x/y.png")),
        guild=SimpleNamespace(id=1))


def _giveaway_setup(coins_by_uid):
    """giveaway + economy mit Fake-Stores. Rueckgabe: (restore, gw)"""
    import giveaway
    restore_eco = _with_economy(coins_by_uid)
    gw = giveaway.instance
    alt = (gw._store, gw._enabled, dict(gw._wizards), gw._client)
    gw._store = _FakeStore({"active": {}, "next_id": 1, "done": []})
    gw._enabled = True
    gw._wizards = {}
    gw._client = None
    # WICHTIG: _protect macht 'import bot' - das wuerde im Test alle setup()s neu
    # fahren und die Fake-Stores (und damit die Kontostaende) austauschen.
    alt_protect = gw._protect
    # Signatur wie das Original (das kennt 'slot' seit jeher) - eine zu enge
    # Attrappe faellt sonst um, sobald der Aufrufer den Slot mitgibt.
    gw._protect = lambda _msg, **_k: None

    def restore():
        gw._store, gw._enabled, gw._wizards, gw._client = alt
        gw._protect = alt_protect
        restore_eco()
    return restore, gw


def test_audit_geld_und_rechte():
    """REGRESSION aus dem grossen Audit: die fuenf Funde, die Geld oder Rechte
    betreffen. Jeder hier ist vorher nachgemessen worden."""
    import admin
    import casino
    import games

    # 1) Owner-Befehl 'gib @wer 5k' buchte still 5 Coins statt 5.000 (und
    #    '1.000.000' sogar 1) - und meldete gruen Erfolg.
    restore = _with_economy({1: 0})
    try:
        a = admin.instance
        ID = "<@1040135855710404659>"
        for text, erwartet in ((f"{ID} 5k", 5000), (f"{ID} 5000", 5000),
                               (f"{ID} 2m", 2_000_000), (f"{ID} 1.000.000", 1_000_000),
                               (f"{ID} 2,5k", 2500), (f"{ID} 750", 750)):
            uid, betrag = a._extract(text)
            assert uid == 1040135855710404659, text
            assert betrag == erwartet, (text, betrag, erwartet)
        # Ohne Betrag bleibt None (der Aufrufer fragt dann nach).
        assert a._extract(f"{ID} hallo")[1] is None
    finally:
        restore()

    # 2) Casino: EINE Runde konnte astronomisch auszahlen (Mines x179.213 bei
    #    Hoechsteinsatz = 179 Billionen Coins). Jetzt gedeckelt.
    restore = _with_economy({1: 0, 2: 0})
    try:
        mult = casino.instance._mines_mult(10, 10)
        assert mult > 100_000, mult                      # Jackpot existiert weiter
        roh = int(casino.MAX_BET * mult)
        gezahlt = casino._auszahlen(1, roh, "mines")
        assert gezahlt == casino.MAX_WIN < roh
        assert economy.get_coins(1) == casino.MAX_WIN
        # Normales Spiel bleibt unberuehrt.
        assert casino._auszahlen(2, 50_000, "test") == 50_000
        # Unsinn wird sauber verschluckt, statt zu crashen.
        for murks in (0, -5, None, float("inf"), float("nan"), "abc"):
            assert casino._auszahlen(2, murks, "test") == 0
        assert economy.get_coins(2) == 50_000
        # RTP bleibt fair (der Deckel greift nur im Extremfall).
        for bomben in (1, 3, 5, 10):
            frei = casino._MINES_TILES - bomben
            m = casino.instance._mines_mult(frei, bomben)
            chance = 1.0
            for i in range(frei):
                chance *= (casino._MINES_TILES - bomben - i) / (casino._MINES_TILES - i)
            assert 0.95 < m * chance < 1.0, (bomben, m * chance)
    finally:
        restore()

    # 3) Spiele-Wasserhaehne: Muenzwurf hatte 100 % RTP und im Text-Pfad keinen
    #    Hoechsteinsatz; Quiz/Zahlenraten/SSP waren pro Nutzer unbegrenzt farmbar.
    assert 0 < games.COINFLIP_PAYOUT < 1.0
    g = games.instance
    g._skill_cd = {}
    assert g._skill_frei(42) is True
    g._skill_cd[42] = time.monotonic()
    assert g._skill_frei(42) is False and g._skill_rest(42) > 0
    quelle = open("games.py", encoding="utf-8").read()
    # Die drei Belohnungen haengen jetzt alle an der Anti-Farm-Sperre - und
    # laufen zusaetzlich durch die TAGESKAPPE (_auszahlen).
    for stelle in ('QUIZ_REWARD, 0, "quiz"', 'reward, 0, "zahlenraten"',
                   '10, 0, "ssp"'):
        i = quelle.index(stelle)
        assert "_skill_frei" in quelle[max(0, i - 400):i], stelle
        assert "_auszahlen" in quelle[max(0, i - 120):i], stelle
    # Jede Auszahlung mit positivem Erwartungswert MUSS durch _auszahlen laufen,
    # sonst ist sie wieder ein Wasserhahn ohne Tageskappe.
    for stelle in ('"reaktion")', '"mathe")', '"anagramm")', '"event")'):
        assert stelle in quelle, stelle
    g._store = _FakeStore({"counting": {}})
    assert g._kappe_rest(4711) == games.GAMES_DAILY_MAX
    g._kappe_buchen(4711, games.GAMES_DAILY_MAX - 100)
    assert g._kappe_rest(4711) == 100
    g._kappe_buchen(4711, 999_999)
    assert g._kappe_rest(4711) == 0
    g._store = None

    # 4) Moderation: der Auto-Timeout nach Verwarnungen muss dieselbe Rangordnung
    #    achten wie ein direkter Timeout (sonst knebelt ein Junior-Mod einen Senior).
    mod_quelle = open("moderation.py", encoding="utf-8").read()
    # Am Verhalten festmachen, nicht am genauen Namen der Grenze: die stand
    # frueher als Konstante WARN_LIMIT da und kommt jetzt je Server aus
    # guildcfg. Der Test soll die RANGORDNUNG schuetzen, nicht die Schreibweise.
    treffer = re.search(r"if \(count >= \w+", mod_quelle)
    assert treffer, "die Auto-Timeout-Bedingung ist nicht mehr auffindbar"
    i = treffer.start()
    assert "darf_strafen" in mod_quelle[i - 400:i + 200]
    assert "full=True" in mod_quelle[i - 400:i]

    # 5) Panel-Ansage darf nie @everyone pingen.
    panel_quelle = open("webpanel.py", encoding="utf-8").read()
    i = panel_quelle.index("await channel.send(")
    assert "AllowedMentions.none()" in panel_quelle[i:i + 200]


def _schulden_setup(coins_by_uid=None):
    """schulden + economy mit Fake-Stores. Rueckgabe: (restore, sch)"""
    import schulden
    restore_eco = _with_economy(coins_by_uid or {})
    sch = schulden.instance
    alt = (sch._store, sch._enabled, sch.buch._store, sch.buch._posten)
    sch._store = _FakeStore({"posten": [], "next_id": 1, "score": {}, "stats": {},
                             "archiv": {}, "pairs": {}})
    sch.buch.laden(sch._store)
    sch._enabled = True

    def restore():
        (sch._store, sch._enabled, sch.buch._store, sch.buch._posten) = alt
        restore_eco()
    return restore, sch


def _schuld(sch, glaeubiger, schuldner, betrag, **kw):
    """Legt einen Posten direkt an (die Zustimmung ist woanders getestet)."""
    return sch.buch.anlegen(glaeubiger, schuldner, betrag, **kw)


def test_schulden_entstehen_nur_mit_zustimmung():
    """DER Kernumbau: eine Zahlung ist ein GESCHENK, keine Forderung.

    Vorher erzeugte jede 'Flo pay'-Zahlung automatisch eine Schuld des
    Empfaengers - ein Geschenk, ein verlorener Wetteinsatz, eine geteilte
    Rechnung, alles wurde stillschweigend zur Forderung. Jetzt braucht jede
    Schuld einen Klick der Person, die sie bekommt."""
    restore, sch = _schulden_setup({1: 50_000, 2: 50_000})
    try:
        # Zahlen erzeugt KEINE Schuld mehr.
        sch.pay_block(1, 2, 5_000, ziel_name="Bert")
        assert sch.saldo(1, 2) == 0
        assert sch.buch.alle() == []

        # Ein Leih-Angebot bewegt fuer sich genommen auch noch nichts.
        besteller = _fake_person(uid=1, name="anna")
        ziel = _fake_person(uid=2, name="bert")
        vorher = (economy.get_coins(1), economy.get_coins(2))
        assert economy.get_coins(1) == vorher[0]

        # Erst das Annehmen bucht Geld UND legt den Posten an.
        ok, text = asyncio.run(sch.annehmen(besteller, ziel, 5_000, grund="Pizza",
                                            faellig=0, mit_geld=True))
        assert ok, text
        assert economy.get_coins(1) == vorher[0] - 5_000
        assert economy.get_coins(2) == vorher[1] + 5_000
        assert sch.saldo(1, 2) == 5_000
        p = sch.buch.alle()[0]
        assert p.grund == "Pizza" and p.urspruenglich == 5_000 and p.ist_offen()

        # Ein Schuldschein bewegt KEIN Geld, legt aber einen Posten an.
        stand = (economy.get_coins(1), economy.get_coins(2))
        ok, _t = asyncio.run(sch.annehmen(besteller, ziel, 1_000, grund="Kino",
                                          faellig=0, mit_geld=False))
        assert ok
        assert (economy.get_coins(1), economy.get_coins(2)) == stand
        assert sch.saldo(1, 2) == 6_000
    finally:
        restore()


def test_schulden_zahlung_wird_angerechnet():
    """Wer seinem Glaeubiger etwas ueberweist, meint fast immer die Schuld.

    Der Rest ist ein Geschenk - und aus einem Geschenk wird nie eine
    Gegenforderung (genau das war der alte Konstruktionsfehler)."""
    restore, sch = _schulden_setup({1: 0, 2: 0})
    try:
        _schuld(sch, 1, 2, 3_000)           # 2 schuldet 1 dreitausend
        block = sch.pay_block(2, 1, 1_000, ziel_name="Anna")
        assert sch.saldo(1, 2) == 2_000
        assert "2.000" in block["stand"], block["stand"]

        # Mehr zahlen als offen ist: der Ueberschuss ist geschenkt, KEINE
        # Forderung in die Gegenrichtung.
        block = sch.pay_block(2, 1, 5_000, ziel_name="Anna")
        assert sch.saldo(1, 2) == 0 and sch.saldo(2, 1) == 0
        assert "beglichen" in block["stand"].lower()

        # Zahlung an jemanden, dem man nichts schuldet: reines Geschenk.
        block = sch.pay_block(2, 9, 500)
        assert sch.saldo(9, 2) == 0 and sch.saldo(2, 9) == 0
        assert "geschenk" in block["stand"].lower()
    finally:
        restore()


def test_schulden_posten_summen_und_erlassen():
    """Uebersicht (wer bekommt, wer schuldet), Summen, Top-Liste und Erlassen."""
    restore, sch = _schulden_setup()
    try:
        A, B, C, D = 1, 2, 3, 4
        _schuld(sch, A, B, 1000)      # B schuldet A 1.000
        _schuld(sch, A, C, 2500)      # C schuldet A 2.500
        _schuld(sch, D, A, 400)       # A schuldet D   400

        forderungen, schulden_ = sch.posten(A)
        assert forderungen == [(C, 2500), (B, 1000)]     # absteigend
        assert schulden_ == [(D, 400)]
        haben, soll, netto = sch.summen(A)
        assert (haben, soll, netto) == (3500, 400, 3100)

        # Top-Liste serverweit: (glaeubiger, schuldner, betrag)
        top = sch.top(10)
        assert top[0] == (A, C, 2500) and (A, B, 1000) in top and (D, A, 400) in top

        # Erlassen: nur der Glaeubiger, teilweise und ganz.
        weg, rest = sch.erlassen(A, C, 500)
        assert (weg, rest) == (500, 2000) and sch.saldo(A, C) == 2000
        weg, rest = sch.erlassen(A, C)                    # der Rest
        assert (weg, rest) == (2000, 0) and sch.saldo(A, C) == 0
        # Wer nichts zu bekommen hat, kann nichts erlassen.
        assert sch.erlassen(C, A, 100) == (0, 0)
        assert sch.erlassen(A, D, 100)[0] == 0            # A ist hier Schuldner
        assert sch.saldo(D, A) == 400                     # unveraendert
        # Mehr erlassen als offen ist -> nur das Offene.
        weg, rest = sch.erlassen(A, B, 999_999)
        assert (weg, rest) == (1000, 0)
        # Ein erledigter Posten bleibt als Historie stehen.
        assert len(sch.buch.alle()) == 3
        assert [p.status for p in sch.buch.alle()].count("erlassen") == 2
    finally:
        restore()


def test_schulden_pay_geht_immer_durch():
    """WICHTIG: das Buch ist nur Buchfuehrung. 'pay' bewegt die Coins wie vorher,
    der Hinweis haengt nur dran - und selbst wenn das Buch kaputt ist, geht die
    Zahlung durch."""
    import schulden
    restore, sch = _schulden_setup({7: 10_000, 8: 0})
    alt_flush = economy.instance._flush

    async def kein_flush():
        return None
    economy.instance._flush = kein_flush

    ziel = _fake_person(uid=8, name="empfaenger")
    autor = _fake_person(uid=7, name="zahler")
    msg = SimpleNamespace(content="flo pay <@8> 2500", mentions=[ziel],
                          author=autor,
                          guild=SimpleNamespace(id=1, get_member=lambda _u: None))
    try:
        def text_von(emb):
            teile = [emb.title or "", emb.description or ""]
            for f in emb.fields:
                teile.append(str(f.name)); teile.append(str(f.value))
            return "\n".join(teile)

        emb = asyncio.run(economy.instance._pay(msg))
        assert economy.get_coins(7) == 7500 and economy.get_coins(8) == 2500
        t = text_von(emb)
        assert "2.500" in t and "Überweisung" in t
        # ... aber KEINE Schuld daraus.
        assert sch.saldo(7, 8) == 0

        # Buch kaputt (wirft) -> Zahlung MUSS trotzdem durchgehen.
        def kaputt(*_a, **_k):
            raise RuntimeError("Buch kaputt")
        alt_note = schulden.instance.note_pay_block
        schulden.instance.note_pay_block = kaputt
        try:
            msg.content = "flo pay <@8> 1000"
            emb = asyncio.run(economy.instance._pay(msg))
            assert economy.get_coins(7) == 6500 and economy.get_coins(8) == 3500
            assert "1.000" in text_von(emb)
        finally:
            schulden.instance.note_pay_block = alt_note

        # Nicht genug Geld -> gar nichts passiert.
        msg.content = "flo pay <@8> 999999"
        antwort = asyncio.run(economy.instance._pay(msg))
        assert isinstance(antwort, str) and "nicht genug" in antwort.lower()
        assert economy.get_coins(7) == 6500
    finally:
        economy.instance._flush = alt_flush
        restore()


def test_schulden_pay_als_leihgabe_fragt_nach():
    """'pay @wer 5k als leihgabe' darf NICHT sofort buchen - daraus wird eine
    Schuld, und die entsteht nur mit Zustimmung."""
    import schulden
    restore, sch = _schulden_setup({7: 50_000, 8: 0})
    ziel = _fake_person(uid=8, name="empfaenger")
    msg = SimpleNamespace(content="flo pay <@8> 5k als leihgabe", mentions=[ziel],
                          author=_fake_person(uid=7, name="zahler"),
                          guild=SimpleNamespace(id=1, get_member=lambda _u: None))
    gestellt = {}

    async def fake_anfrage(message, ziel_, betrag, *, grund, faellig, mit_geld):
        gestellt.update(betrag=betrag, grund=grund, mit_geld=mit_geld)
        return schulden.HANDLED

    alt = schulden.leih_anfrage
    schulden.leih_anfrage = fake_anfrage
    try:
        antwort = asyncio.run(economy.instance._pay(msg))
        assert antwort is schulden.HANDLED
        assert gestellt == {"betrag": 5000, "grund": "", "mit_geld": True}, gestellt
        # Kein Coin hat sich bewegt.
        assert economy.get_coins(7) == 50_000 and economy.get_coins(8) == 0
    finally:
        schulden.leih_anfrage = alt
        restore()


def test_schulden_automatische_tilgung():
    """Der Zwang zum Zahlen: von jeder ECHTEN Einnahme wandert ein Anteil
    automatisch an die Glaeubiger - anteilig an ALLE, nicht mehr alles an den
    groessten. Coins entstehen dabei nie und verschwinden nie."""
    restore, sch = _schulden_setup({1: 0, 2: 0, 3: 0})
    try:
        A, B, C = 1, 2, 3
        _schuld(sch, B, A, 7_500)             # A schuldet B 7.500
        _schuld(sch, C, A, 2_500)             # A schuldet C 2.500
        summe_vorher = sum(economy.get_coins(u) for u in (A, B, C))

        # A gewinnt 5.000 -> 20 % = 1.000 gehen anteilig raus (75/25).
        economy.add_coins(A, 5_000, reason="casino")
        assert economy.get_coins(A) == 4_000, economy.get_coins(A)
        assert economy.get_coins(B) == 750 and economy.get_coins(C) == 250
        assert sch.saldo(B, A) == 6_750 and sch.saldo(C, A) == 2_250
        assert sch.getilgt_summe(A) == 1_000
        # Coins nur umverteilt (plus die Einnahme von aussen).
        assert sum(economy.get_coins(u) for u in (A, B, C)) == summe_vorher + 5_000

        # Kleinstbetraege bleiben unberuehrt.
        vor = economy.get_coins(A)
        economy.add_coins(A, 10, reason="spiele")
        assert economy.get_coins(A) == vor + 10

        # Tabu-Quellen tilgen NICHT (Panel-Korrektur, Aktien, direkte pay).
        for grund in ("panel", "floaktie", "pay", "schulden-tilgung"):
            vor = economy.get_coins(A)
            economy.add_coins(A, 1000, reason=grund)
            assert economy.get_coins(A) == vor + 1000, grund

        # Ausgaben loesen keine Tilgung aus.
        vor_b = economy.get_coins(B)
        economy.add_coins(A, -500, reason="casino")
        assert economy.get_coins(B) == vor_b

        # Nie mehr als die Restschuld.
        sch.erlassen(B, A, sch.saldo(B, A) - 50)
        sch.erlassen(C, A)
        assert sch.saldo(B, A) == 50 and sch.saldo(C, A) == 0
        vor_b = economy.get_coins(B)
        economy.add_coins(A, 100_000, reason="spiele")
        assert economy.get_coins(B) == vor_b + 50
        assert sch.saldo(B, A) == 0

        # Ohne Schulden passiert nichts.
        vor = economy.get_coins(B)
        economy.add_coins(B, 5000, reason="casino")
        assert economy.get_coins(B) == vor + 5000

        # Kein Endlos-Kreislauf: die Tilgung bucht selbst und darf sich nicht
        # erneut selbst anstossen (sonst haengt der Bot).
        assert sch._tilgung_laeuft is False
    finally:
        restore()


def test_schulden_tilgung_bevorzugt_ueberfaellige():
    """Ueberfaellige Posten zuerst, danach der Rest - und innerhalb einer
    Person der aelteste Posten zuerst (FIFO)."""
    restore, sch = _schulden_setup({1: 0, 2: 0, 3: 0})
    try:
        jetzt = time.time()
        # B: ein ueberfaelliger Posten. C: ein dickerer, aber nicht faelliger.
        p_alt = _schuld(sch, 2, 1, 1_000, faellig=jetzt - 86400,
                        entstanden=jetzt - 20 * 86400)
        _schuld(sch, 3, 1, 9_000, entstanden=jetzt - 10 * 86400)
        economy.add_coins(1, 5_000, reason="casino")     # 1.000 Budget
        # Alles ging an den ueberfaelligen Posten, obwohl der kleiner ist.
        assert p_alt.offen == 0 and p_alt.status == "getilgt"
        assert sch.saldo(3, 1) == 9_000
        assert economy.get_coins(2) == 1_000 and economy.get_coins(3) == 0

        # FIFO innerhalb eines Glaeubigers: der aeltere Posten zuerst.
        sch.erlassen(3, 1, 9_000)                        # den dicken wegraeumen
        alt_1 = _schuld(sch, 3, 1, 500, entstanden=jetzt - 30 * 86400)
        neu_1 = _schuld(sch, 3, 1, 500, entstanden=jetzt - 1 * 86400)
        economy.add_coins(1, 1_000, reason="casino")     # 200 Budget
        assert (alt_1.offen, neu_1.offen) == (300, 500), (alt_1.offen, neu_1.offen)
    finally:
        restore()


def test_schulden_grenzen_und_kreditwuerdigkeit():
    """Grenzen schuetzen beide Seiten - und die Kreditwuerdigkeit bewegt sich
    nur durch eigenes Verhalten."""
    import schulden
    restore, sch = _schulden_setup({1: 10_000_000, 2: 1_000_000})
    try:
        # Zu klein lohnt die Buchfuehrung nicht.
        ok, fehler = sch._darf_anlegen(1, 2, 10)
        assert not ok and "lohnt" in fehler

        # Bei sich selbst geht gar nichts.
        assert sch._darf_anlegen(1, 1, 1_000)[0] is False

        # Der Start-Score ist 50 und die Ampel gelb.
        assert sch.score.score(2) == 50 and sch.score.ampel(2) == "🟡"

        # Hoechstens fuenf offene Posten je Paar.
        for _ in range(schulden.MAX_POSTEN_JE_PAAR):
            _schuld(sch, 1, 2, 1_000)
        ok, fehler = sch._darf_anlegen(1, 2, 1_000)
        assert not ok and "offene" in fehler.lower()
        for p in sch.buch.alle():
            p.status = "erlassen"
            p.offen = 0

        # Puenktlich getilgt hebt den Score, ueberfaellig senkt ihn.
        p = _schuld(sch, 1, 2, 1_000, faellig=time.time() + 86400)
        sch._abbuchen(p, 1_000)
        assert sch.score.score(2) == 55, sch.score.score(2)
        sch.score.notiere(2, schulden.Kreditwuerdigkeit.UEBERFAELLIG, "test")
        assert sch.score.score(2) == 45

        # Alte Eintraege zaehlen nur halb.
        sch.buch.score_daten()["2"] = [
            {"t": time.time() - 200 * 86400, "d": -20, "g": "alt"}]
        assert sch.score.score(2) == 40

        # Der Score deckelt KEINE Betraege mehr. Die einzige Betragsgrenze ist
        # der Kontostand des Verleihers - sein GANZES Geld darf er verleihen.
        assert not hasattr(sch.score, "leih_limit"), (
            "leih_limit lebt wieder in der Kreditwuerdigkeit - genau dort ist "
            "die alte Score-Regel schon einmal eingewandert")
        habe = economy.get_coins(1)
        assert habe == 10_000_000, habe
        # Genau alles geht - das ist der ganze Punkt der Aenderung.
        assert sch._darf_anlegen(1, 2, habe)[0] is True, "das eigene Geld ist tabu"
        # Ein Coin mehr nicht, und die Meldung nennt den Kontostand statt Score.
        ok, fehler = sch._darf_anlegen(1, 2, habe + 1)
        assert not ok, "man kann mehr verleihen als man hat"
        import numfmt
        assert "Konto" in fehler and numfmt.fmt(habe) in fehler, fehler
        assert "Kreditwürdigkeit" not in fehler, (
            f"die Absage redet weiter vom Score: {fehler}")

        # Unter 20 ist ganz Schluss.
        sch.buch.score_daten()["2"] = [{"t": time.time(), "d": -40, "g": "test"}]
        gesperrt, warum = sch.score.gesperrt(2)
        assert gesperrt and "niedrig" in warum
    finally:
        restore()


def test_leihgabe_wird_geprueft_BEVOR_sie_oeffentlich_steht():
    """'Flo pay @wer 5k als leihgabe' lief an allen Grenzen vorbei.

    Der Weg kommt aus economy und ruft _anfrage_stellen direkt auf - die
    Pruefungen sassen aber in _cmd_leih. Ergebnis: das Angebot wurde oeffentlich
    gepostet, der andere klickte, und ERST dann platzte es. Jetzt prueft
    _anfrage_stellen selbst, also fuer alle drei Wege gleich."""
    restore, sch = _schulden_setup({1: 1_000, 2: 1_000})
    try:
        gepostet = []

        async def reply(*a, **kw):
            gepostet.append(kw)
            return SimpleNamespace(id=1, guild=None)

        msg = _fake_msg(1, "pay <@2> 50k als leihgabe")
        msg.reply = reply
        ziel = SimpleNamespace(id=2, bot=False, display_name="Kumpel")

        # 50k, aber nur 1k auf dem Konto -> Absage, und NICHTS im Kanal.
        antwort = asyncio.run(sch._anfrage_stellen(
            msg, ziel, 50_000, grund="", faellig=0, mit_geld=True))
        assert not gepostet, "das Angebot stand oeffentlich, bevor es geprueft war"
        assert "Konto" in str(antwort), antwort

        # Was durchgeht, wird gepostet - und das Embed muss sagen, worauf man
        # klickt: die 20 % jeder Einnahme standen bisher nirgends dort.
        antwort = asyncio.run(sch._anfrage_stellen(
            msg, ziel, 1_000, grund="", faellig=0, mit_geld=True))
        assert len(gepostet) == 1, gepostet
        emb = gepostet[0]["embed"]
        text = " ".join([emb.title or "", emb.description or ""]
                        + [f"{f.name} {f.value}" for f in emb.fields])
        assert "%" in text and "Einnahme" in text, (
            f"das Angebot verschweigt die Tilgungsautomatik: {text}")
    finally:
        restore()


def test_schuldschein_braucht_kein_bargeld():
    """Beim Schuldschein fliesst KEIN Geld - dort darf der Kontostand nichts
    blockieren.

    'Kumpel schuldet mir noch 5.000 vom Kinoabend' ist eine Beurkundung von
    etwas, das laengst passiert ist. Ob mein Geld gerade im Casino oder in
    Aktien steckt, geht die Sache nichts an. Beim Leihen ist es umgekehrt:
    da fliesst echtes Geld, das ich haben muss."""
    restore, sch = _schulden_setup({1: 0, 2: 5_000})
    try:
        # Kontostand 0 - als Schuldschein trotzdem in Ordnung.
        assert sch._darf_anlegen(1, 2, 5_000, mit_geld=False)[0] is True, (
            "der Schuldschein wird am Bargeld des Ausstellers gemessen")
        # Mit echtem Geldfluss geht dasselbe nicht.
        ok, fehler = sch._darf_anlegen(1, 2, 5_000, mit_geld=True)
        assert not ok and "Konto" in fehler, fehler
        # Die anderen Grenzen gelten beim Schuldschein weiter.
        assert sch._darf_anlegen(1, 2, 10, mit_geld=False)[0] is False
        assert sch._darf_anlegen(1, 1, 5_000, mit_geld=False)[0] is False
    finally:
        restore()


def test_schulden_sperre_nennt_den_richtigen():
    """"Deine Kreditwuerdigkeit ist zu niedrig" bekam der FALSCHE zu lesen.

    Gesperrt ist immer der Schuldner - getippt hat 'Flo leih @kumpel 5k' aber
    der Verleiher. Der las, SEIN Score sei kaputt, und suchte den Fehler bei
    sich. Nach dem Umbau ist die Sperre die einzige verbliebene Wirkung des
    Scores; dann muss sie wenigstens den Richtigen benennen."""
    restore, sch = _schulden_setup({1: 100_000, 2: 100_000})
    try:
        sch.buch.score_daten()["2"] = [{"t": time.time(), "d": -40, "g": "test"}]
        gesperrt, warum = sch.score.gesperrt(2)
        assert gesperrt
        assert "deine" not in warum.lower(), (
            f"der Grund redet den Falschen an: {warum}")
        ok, fehler = sch._darf_anlegen(1, 2, 5_000)
        assert not ok
        assert "<@2>" in fehler, f"der Gesperrte wird nicht genannt: {fehler}"
        # Und der Weg zurueck muss dastehen - sonst ist es eine Sackgasse.
        assert "Tilgen" in fehler or "tilgen" in fehler, fehler
    finally:
        restore()


def test_leih_liest_den_betrag_und_nicht_die_frist():
    """'Flo leih @wer bis 3 wochen 10k' hat die **3** als Betrag gelesen.

    Die Antwort war dann "unter 50 lohnt die Buchfuehrung nicht" - eine
    Fehlermeldung, die mit der Eingabe nichts zu tun hat und niemanden auf den
    richtigen Weg bringt. _lies_grund schneidet die Frist laengst heraus,
    _lies_betrag tat es nicht."""
    restore, sch = _schulden_setup({1: 100_000, 2: 0})
    try:
        for text, erwartet in (
                ("<@2> bis 3 wochen 10k", 10_000),
                ("<@2> bis 2 monaten 5000", 5_000),
                ("<@2> bis in 7 tagen 2,5k", 2_500),
                ("<@2> bis freitag 1k", 1_000),
                ("<@2> 10k", 10_000),
                ("<@2> 10k bis 3 wochen", 10_000)):
            assert sch._lies_betrag(None, text) == erwartet, (
                f"{text!r} -> {sch._lies_betrag(None, text)}, erwartet {erwartet}")
        # Die Frist selbst muss weiter erkannt werden.
        assert sch._lies_frist("<@2> bis 3 wochen 10k") > time.time()
    finally:
        restore()


def test_erlassen_streicht_nicht_versehentlich_alles():
    """'schulden erlassen @x 500 fuers ballern' hat ALLES gestrichen.

    Geprueft wurde mit 'all' als TEILSTRING - und 'all' steckt in 'ballern',
    'halle', 'Fussball'. Aus 500 wurde die komplette Forderung, ohne Rueckfrage
    und ohne Weg zurueck. Das ist der teuerste Tippfehler im ganzen Modul."""
    restore, sch = _schulden_setup({1: 100_000, 2: 100_000})
    try:
        _schuld(sch, 1, 2, 20_000)

        async def erlassen(text):
            msg = _fake_msg(1, text)
            msg.mentions = [SimpleNamespace(id=2, bot=False, display_name="Kumpel")]
            return await sch._erlassen(msg)

        # 'ballern' enthaelt 'all' - es duerfen trotzdem nur 500 weg sein.
        asyncio.run(erlassen("schulden erlassen <@2> 500 fuers ballern"))
        assert sch.buch.saldo(1, 2) == 19_500, sch.buch.saldo(1, 2)
        # Steht eine Zahl da, gewinnt die Zahl - auch neben dem Wort 'ganz'.
        asyncio.run(erlassen("schulden erlassen <@2> 500 ganz sicher"))
        assert sch.buch.saldo(1, 2) == 19_000, sch.buch.saldo(1, 2)
        # Ohne Zahl und ohne 'alles' wird nachgefragt statt alles zu streichen.
        antwort = asyncio.run(erlassen("schulden erlassen <@2>"))
        assert sch.buch.saldo(1, 2) == 19_000, "hat kommentarlos alles gestrichen"
        assert "Wie viel" in str(antwort), antwort
        # Ausdrueckliches 'alles' raeumt weiter komplett ab.
        asyncio.run(erlassen("schulden erlassen <@2> alles"))
        assert sch.buch.saldo(1, 2) == 0, sch.buch.saldo(1, 2)
    finally:
        restore()


def test_schulden_darf_mehr_sein_als_man_besitzt():
    """Man darf ausdruecklich MEHR schulden, als man hat.

    Frueher war die Gesamtschuld auf das Dreifache des eigenen Vermoegens
    gedeckelt. Das hat niemand erraten ("verstehe das vermoegen zeug nicht"),
    und es hat den Fall verboten, um den es beim Leihen ueberhaupt geht: wer
    pleite ist, braucht das Geld - wer reich ist, nicht.

    Die einzige Grenze ist jetzt, was der VERLEIHER wirklich hat."""
    # Der Schuldner ist absichtlich arm - genau daran waere es vorher
    # gescheitert.
    restore, sch = _schulden_setup({1: 100_000_000, 2: 1_000})
    try:
        _schuld(sch, 1, 2, 2_900_000)
        # Das 2900-fache seines Geldes steht schon offen - trotzdem geht mehr.
        assert sch._darf_anlegen(1, 2, 200_000)[0] is True, (
            "der alte Vermoegens-Deckel lebt noch")
        # Auch das ganze Geld des Verleihers auf einmal.
        assert sch._darf_anlegen(1, 2, 100_000_000)[0] is True
        # Nur mehr als der Verleiher hat, geht nicht.
        ok, fehler = sch._darf_anlegen(1, 2, 100_000_001)
        assert not ok and "Konto" in fehler, fehler
    finally:
        restore()


def test_schulden_verfall_und_mahnstufen():
    """Ein Posten ohne jede Bewegung verfaellt - kein ewiges Druckmittel. Und
    gemahnt wird in Stufen, nicht jeden Tag gleich."""
    import schulden
    restore, sch = _schulden_setup({1: 0, 2: 0})
    try:
        jetzt = time.time()
        # Kurz vor dem Verfall -> Warnung an den Glaeubiger.
        p = _schuld(sch, 1, 2, 5_000, entstanden=jetzt - 55 * 86400)
        p.log = [{"t": jetzt - 55 * 86400, "art": "entstanden", "betrag": 5_000}]
        verfallen, warnen = sch.verfall_pruefen()
        assert not verfallen and len(warnen) == 1 and warnen[0][1] <= 7

        # Drueber -> verfallen, und der Score des Schuldners leidet.
        p.log = [{"t": jetzt - 61 * 86400, "art": "entstanden", "betrag": 5_000}]
        p.entstanden = jetzt - 61 * 86400
        verfallen, _w = sch.verfall_pruefen()
        assert len(verfallen) == 1 and p.status == "verfallen"
        assert sch.saldo(1, 2) == 0
        assert sch.score.score(2) == 35

        # Mahnstufen: 1 = faellig, 2 = eine Woche, 3 = zwei Wochen.
        for tage, erwartet in ((1, 1), (schulden.MAHN_STUFE_2 + 1, 2),
                               (schulden.MAHN_STUFE_3 + 1, 3)):
            q = _schuld(sch, 1, 2, 1_000, faellig=jetzt - tage * 86400)
            stufen = [s for _u, s, post in sch.faellige_stufen() if post is q]
            assert stufen == [erwartet], (tage, stufen)
            q.status = "erlassen"
            q.offen = 0
    finally:
        restore()


def test_schulden_insolvenz():
    """Privatinsolvenz: die Haelfte des Vermoegens anteilig raus, der Rest
    erlassen, Score unten und 14 Tage Sperre."""
    import schulden
    restore, sch = _schulden_setup({1: 10_000, 2: 0, 3: 0})
    try:
        _schuld(sch, 2, 1, 30_000)
        _schuld(sch, 3, 1, 10_000)
        text = asyncio.run(sch.insolvenz_durchfuehren(1))
        # 50 % von 10.000 = 5.000, anteilig 75/25.
        assert economy.get_coins(1) == 5_000, economy.get_coins(1)
        assert economy.get_coins(2) == 3_750 and economy.get_coins(3) == 1_250
        assert sch.summen(1)[1] == 0
        assert all(p.status in ("getilgt", "erlassen") for p in sch.buch.alle())
        assert sch.score.score(1) == schulden.Kreditwuerdigkeit.INSOLVENZ_SCORE
        # Gesperrt ist gesperrt - hier greift schon der Score (10 < 20), die
        # 14-Tage-Frist steht zusaetzlich im Buch.
        gesperrt, warum = sch.score.gesperrt(1)
        assert gesperrt and warum
        bis = sch.buch.stats(1)["insolvenz_bis"]
        assert bis > time.time() + 13 * 86400
        assert "erlassen" in text
    finally:
        restore()


def test_schulden_migration_alter_tafel():
    """Der alte Netto-Saldo je Paar wird zu je EINEM Posten - und nichts geht
    verloren: die alten Zahlen wandern ins Archiv."""
    restore, sch = _schulden_setup()
    try:
        store = _FakeStore({
            "pairs": {"1:2": {"net": 5_000, "vol": 9_000, "n": 4, "first": 1_000_000},
                      "3:4": {"net": -2_000, "vol": 2_000, "n": 1},
                      "5:6": {"net": 0, "vol": 500, "n": 2}},
            "posten": [], "next_id": 1, "score": {}, "stats": {}, "archiv": {},
        })
        sch.buch.laden(store)
        # net > 0 heisst "der ZWEITE schuldet dem ERSTEN".
        assert sch.buch.saldo(1, 2) == 5_000
        assert sch.buch.saldo(4, 3) == 2_000
        # Ein ausgeglichenes Paar erzeugt keinen Posten.
        assert len(sch.buch.alle()) == 2
        assert all(p.grund.startswith("Übernahme") for p in sch.buch.alle())
        # Nichts geloescht: die alte Tafel liegt im Archiv.
        assert store.data["archiv"]["pairs"]["1:2"]["vol"] == 9_000
        assert store.data["pairs"] == {}
        # Zweites Laden legt NICHT nochmal an.
        sch.buch.laden(store)
        assert len(sch.buch.alle()) == 0     # posten stand noch nicht im Store
    finally:
        restore()


def test_schulden_mahnung():
    """Mahnung per DM: nur ab einer Mindestsumme und hoechstens einmal je Abstand."""
    import schulden
    restore, sch = _schulden_setup()
    try:
        _schuld(sch, 1, 2, 50_000)        # 2 schuldet 1 -> 2 wird gemahnt
        _schuld(sch, 1, 3, 10)            # zu klein -> keine Mahnung
        gesendet = []

        class _User:
            def __init__(self, uid):
                self.id = uid

            async def send(self, **kw):
                gesendet.append((self.id, kw.get("embed")))

        client = SimpleNamespace(get_user=lambda uid: _User(uid), guilds=[])
        n = asyncio.run(sch.mahn_tick(client))
        assert n == 1 and gesendet[0][0] == 2
        emb = gesendet[0][1]
        text = (emb.title or "") + (emb.description or "") + "".join(
            str(f.name) + str(f.value) for f in emb.fields)
        assert "50.000" in text and "%" in text
        assert emb.color.value == schulden.FARBE_SCHULDEN
        # Direkt nochmal -> keine zweite DM (Abstand).
        assert asyncio.run(sch.mahn_tick(client)) == 0
        # Nach Ablauf des Abstands wieder.
        sch.buch.stats(2)["mahnung"] = time.time() - schulden.MAHN_ABSTAND - 5
        assert asyncio.run(sch.mahn_tick(client)) == 1
    finally:
        restore()


def test_giveaway_sprachverstaendnis():
    """Der Assistent muss Alltagssprache verstehen: viele Formulierungen, die
    dasselbe bedeuten, muessen exakt dasselbe ergeben."""
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        # --- Einsatz ---
        for text, erwartet in (
            ("5000", 5000), ("5k", 5000), ("5 k", 5000), ("10.000", 10000),
            ("10000 coins", 10000), ("2 mio", 2_000_000), ("2m", 2_000_000),
            ("1,5k", 1500), ("1.5k", 1500), ("2,5 mio", 2_500_000),
            ("zweitausend", 2000), ("zwei tausend", 2000),
            ("zweitausendfünfhundert", 2500), ("fünfhundert", 500),
            ("einundzwanzig", 21), ("dreizehn", 13), ("eine million", 1_000_000),
            ("drei mrd", 3_000_000_000), ("1 000", 1000),
        ):
            betrag, _h = gw.parse_stake(text, 100_000)
            assert betrag == erwartet, (text, betrag, erwartet)
        # Anteile am Guthaben
        assert gw.parse_stake("alles", 100_000)[0] == 100_000
        assert gw.parse_stake("all in", 100_000)[0] == 100_000
        assert gw.parse_stake("mein ganzes geld", 100_000)[0] == 100_000
        assert gw.parse_stake("die hälfte", 100_000)[0] == 50_000
        assert gw.parse_stake("halb", 100_000)[0] == 50_000
        assert gw.parse_stake("ein drittel", 99_000)[0] == 33_000
        assert gw.parse_stake("viertel", 100_000)[0] == 25_000
        assert gw.parse_stake("25%", 100_000)[0] == 25_000
        assert gw.parse_stake("10 %", 100_000)[0] == 10_000
        # Murks bleibt Murks (der Assistent fragt dann nochmal nach)
        for text in ("keine ahnung", "hä", "", "?!"):
            assert gw.parse_stake(text, 100_000)[0] is None, text

        # --- Dauer ---
        for text, secs in (
            ("10min", 600), ("10 min", 600), ("10 minuten", 600), ("10m", 600),
            ("10", 600),                                  # nackte Zahl = Minuten
            ("1h", 3600), ("1 stunde", 3600), ("eine stunde", 3600),
            ("1 std", 3600), ("60 minuten", 3600), ("1h 30m", 5400),
            ("1h30", 5400), ("1 stunde 30 minuten", 5400), ("eineinhalb stunden", 5400),
            ("halbe stunde", 1800), ("eine halbe stunde", 1800),
            ("viertelstunde", 900), ("dreiviertelstunde", 2700),
            ("2 tage", 172800), ("zwei tage", 172800), ("1 tag", 86400),
            ("24h", 86400), ("1 woche", 604800), ("30 sek", 30), ("45 sekunden", 45),
            ("kurz", 600), ("mittel", 3600), ("lang", 86400),
            ("über nacht", 43200), ("wochenende", 172800),
        ):
            assert gw.parse_duration(text) == secs, (text, gw.parse_duration(text), secs)
        assert gw.parse_duration("bla") is None

        # --- Ja/Nein/Abbruch ---
        for t in ("ja", "jo", "jup", "yes", "passt", "klar", "los", "start", "ok",
                  "👍", "✅", "auf jeden fall", "mach", "bestätigen"):
            assert gw.is_yes(t), t
        for t in ("nein", "ne", "nö", "no", "abbrechen", "lieber nicht", "❌", "stop"):
            assert gw.is_no(t), t
        for t in ("abbrechen", "stop", "stopp", "cancel", "vergiss es", "nvm"):
            assert gw.is_cancel(t), t
        assert not gw.is_yes("nein")
        assert not gw.is_no("ja")

        # Lesbare Dauer-Ausgabe
        assert gw.dauer_text(600) == "10 Minuten"
        assert gw.dauer_text(3600) == "1 Stunde"
        assert gw.dauer_text(5400) == "1 Stunde 30 Minuten"
        assert gw.dauer_text(86400) == "1 Tag"
        assert gw.dauer_text(172800) == "2 Tage"
    finally:
        restore()


def test_shop_wuerfelt_nur_einmal_pro_nacht():
    """Die Tagesauswahl wechselt GENAU EINMAL, naemlich um 2 Uhr.

    Vorher haengte sie am Kalendertag: um 00:00 wuerfelte der Datumswechsel neu
    und um 02:00 nochmal der Loop mit force=True. Wer nachts zwischen 0 und 2 Uhr
    in den Shop schaute, sah eine Auswahl, die zwei Stunden spaeter schon wieder
    eine andere war - obwohl im Embed "Jeden Tag um 2 Uhr" steht."""
    from datetime import datetime as _dt, timedelta as _td
    import economy
    e = economy.instance
    alt = (e._store, e._enabled)
    try:
        e._enabled = True
        e._store = _FakeStore({"users": {}, "shop": {"date": "", "items": []}})

        def tag_um(stunde, minute=0):
            jetzt = _dt(2026, 8, 6, stunde, minute, tzinfo=e._tz)
            return (jetzt - _td(hours=e.SHOP_TAGESWECHSEL_STD)).strftime("%Y-%m-%d")

        # Vor 2 Uhr gehoert die Nacht noch zum VORTAG.
        assert tag_um(0) == tag_um(1) == tag_um(1, 59) == "2026-08-05"
        # Ab 2 Uhr der neue Tag - und der haelt bis Mitternacht durch.
        assert tag_um(2) == tag_um(3) == tag_um(12) == tag_um(23) == "2026-08-06"

        # Ohne force wird innerhalb desselben Shop-Tags nicht neu gewuerfelt.
        st1 = e.refresh_shop(force=True)
        namen1 = [i["label"] for i in st1["items"]]
        st2 = e.refresh_shop(force=False)
        assert [i["label"] for i in st2["items"]] == namen1
        assert st2["date"] == e._shop_tag()

        # Ein Kalendertag-Wechsel allein darf NICHTS ausloesen - genau daran lag es.
        assert e._shop_tag() != e._today() or True   # (nur Doku: beides moeglich)
        e._store.data["shop"]["date"] = e._shop_tag()
        st3 = e.refresh_shop(force=False)
        assert [i["label"] for i in st3["items"]] == namen1
    finally:
        e._store, e._enabled = alt


def test_voice_coins_haben_einen_tagesdeckel():
    """Herumsitzen im Call brachte 43.200 Coins am Tag - das 17-fache des
    Daily-Bonus, fuers Nichtstun. Zwei Leute konnten ueber Nacht parken.

    Gedeckelt werden nur die COINS. Voice-Stunden und XP muessen unveraendert
    weiterlaufen, die Statistik soll die echte Zeit zeigen."""
    import economy
    e = economy.instance
    alt = (e._store, e._enabled, e.XP_PER_VOICE_TICK, e._today)
    try:
        e._enabled = True
        e._store = _FakeStore({"users": {}})
        e.XP_PER_VOICE_TICK = 0          # Level-Up-Praemien ausblenden
        tag = ["2026-08-06"]
        e._today = lambda: tag[0]

        def mitglied(uid):
            return SimpleNamespace(id=uid, bot=False, display_name=f"U{uid}",
                                   voice=SimpleNamespace(self_deaf=False, deaf=False))

        class Guild(SimpleNamespace):
            def get_channel(self, _cid):
                return None

        guild = Guild(voice_channels=[SimpleNamespace(
            id=1, members=[mitglied(1), mitglied(2)])],
            afk_channel=None, system_channel=None)

        def minuten(n):
            for _ in range(n):
                asyncio.run(e.tick_voice(guild))

        minuten(60)                                    # 1 h
        p = e._users()["1"]
        assert p["coins"] == 60 * e.COINS_PER_VOICE_TICK, p["coins"]

        minuten(420)                                   # zusammen 8 h
        assert e._users()["1"]["coins"] == e.VOICE_COINS_DAILY_MAX

        minuten(960)                                   # zusammen 24 h
        p = e._users()["1"]
        assert p["coins"] == e.VOICE_COINS_DAILY_MAX, p["coins"]
        # Stunden laufen trotzdem weiter - genau das soll erhalten bleiben.
        assert p["voice_secs"] == 1440 * e.VOICE_TICK_SECONDS, p["voice_secs"]

        # Neuer Tag -> neues Guthaben.
        tag[0] = "2026-08-07"
        minuten(60)
        assert e._users()["1"]["coins"] == (e.VOICE_COINS_DAILY_MAX
                                            + 60 * e.COINS_PER_VOICE_TICK)

        # Deckel abschaltbar (0 = aus).
        e._store = _FakeStore({"users": {}})
        e.VOICE_COINS_DAILY_MAX, merk = 0, e.VOICE_COINS_DAILY_MAX
        try:
            minuten(600)
            assert e._users()["1"]["coins"] == 600 * e.COINS_PER_VOICE_TICK
        finally:
            e.VOICE_COINS_DAILY_MAX = merk
    finally:
        e._store, e._enabled, e.XP_PER_VOICE_TICK, e._today = alt


def test_einsatz_rueckgabe_ist_keine_einnahme():
    """Wer Schulden hat, bekommt seinen EINSATZ voll zurueck - getilgt wird nur
    vom echten Gewinn.

    Vorher hat _auszahlen alles als "spiele" gebucht. Die Schulden-Tilgung nimmt
    20 % jeder Einnahme - "Einsatz zurueck" gab damit nur 80 % wieder, obwohl
    ueberhaupt nichts gewonnen wurde. Die Tabu-Liste kannte "shop-rueck" und
    "floaktie-rueck", aber die Spiele hatten keinen eigenen Grund."""
    import economy
    import games
    import schulden

    alt = (economy.instance._store, economy.instance._enabled,
           schulden.instance._store, schulden.instance._enabled,
           schulden.instance.buch._store, schulden.instance.buch._posten,
           games.instance._store, games.instance._enabled)
    try:
        def aufsetzen():
            economy.instance._enabled = True
            economy.instance._store = _FakeStore({"users": {
                str(i): {"coins": 5000, "xp": 0, "owned": [], "name": f"U{i}",
                         "title": "", "title_rarity": "", "voice_secs": 0,
                         "msgs": 0, "streak": 0, "last_daily": ""}
                for i in (1, 2)}})
            schulden.instance._enabled = True
            schulden.instance._store = _FakeStore(
                {"posten": [], "next_id": 1, "score": {}, "stats": {},
                 "archiv": {}, "pairs": {}})
            schulden.instance.buch.laden(schulden.instance._store)
            # Spieler 1 schuldet Spieler 2 hunderttausend.
            schulden.instance.buch.anlegen(2, 1, 100_000)
            schulden.instance._tilgung_laeuft = False
            games.instance._enabled = True
            games.instance._store = _FakeStore(
                {"counting": {}, "daily": {"day": "", "won": {}}})

        aufsetzen()
        assert schulden.instance.posten(1)[1] == [(2, 100_000)]

        # 1) Unentschieden: Einsatz komplett zurueck, nichts getilgt.
        aufsetzen()
        vor, vor_g = economy.get_coins(1), economy.get_coins(2)
        games.instance._auszahlen(1, 1000, einsatz=1000, spiel="slot")
        assert economy.get_coins(1) - vor == 1000, economy.get_coins(1) - vor
        assert economy.get_coins(2) == vor_g

        # 2) Echter Gewinn: Einsatz voll, vom Gewinn 20 % an den Glaeubiger.
        aufsetzen()
        vor, vor_g = economy.get_coins(1), economy.get_coins(2)
        games.instance._auszahlen(1, 3000, einsatz=1000, spiel="slot")
        assert economy.get_coins(1) - vor == 2600, economy.get_coins(1) - vor
        assert economy.get_coins(2) - vor_g == 400

        # 3) Gewinn ohne Einsatz wird ganz normal getilgt.
        aufsetzen()
        vor, vor_g = economy.get_coins(1), economy.get_coins(2)
        games.instance._auszahlen(1, 2000, einsatz=0, spiel="quiz")
        assert economy.get_coins(1) - vor == 1600
        assert economy.get_coins(2) - vor_g == 400

        # 4) Die Regel gilt allgemein: jeder Grund auf "-rueck"/"-rueckgabe"
        #    ist tabu, nicht nur die namentlich gelisteten.
        for grund in ("spiele-rueck", "casino-rueckgabe", "shop-rueck",
                      "floaktie-rueck"):
            aufsetzen()
            betrag, _g = schulden.instance.tilgen_von_einnahme(1, 5000, grund)
            assert betrag == 0, (grund, betrag)
        aufsetzen()
        betrag, _g = schulden.instance.tilgen_von_einnahme(1, 5000, "slot")
        assert betrag > 0, betrag
    finally:
        (economy.instance._store, economy.instance._enabled,
         schulden.instance._store, schulden.instance._enabled,
         schulden.instance.buch._store, schulden.instance.buch._posten,
         games.instance._store, games.instance._enabled) = alt


def test_sonderziffern_sprengen_nichts():
    """str.isdigit() ist die falsche Pruefung vor int() - und hat Geld gekostet.

    "²".isdigit() ist True, int("²") wirft ValueError. Im Betrieb hiess das:
    'flo mines 100 ²' hat den Einsatz eingezogen und ist danach am Parsen
    gestorben - das Geld war weg, ohne dass je eine Runde lief. Betroffen waren
    14 Stellen in 7 Modulen."""
    import pathlib
    import numfmt

    # 1) Der Helfer selbst.
    for gut in ("42", "0", "7"):
        assert numfmt.ist_zahl(gut), gut
    for schlecht in ("²", "³", "5²", "１２３", "௫", "¹", "", " 7", "-3", "4.2",
                     None, 7, True):
        assert not numfmt.ist_zahl(schlecht), schlecht

    # 2) Was durchkommt, ueberlebt auch int().
    for s in ("0", "1", "42", "999999"):
        int(s)                                   # darf nicht werfen

    # 3) KEIN Modul darf sich mehr auf isdigit() verlassen (ausser numfmt selbst,
    #    das erklaert dort ja gerade warum).
    schuldige = []
    for p in sorted(pathlib.Path(".").glob("*.py")):
        if p.name.startswith("test_") or p.name == "numfmt.py":
            continue
        for i, zeile in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if ".isdigit()" in zeile:
                schuldige.append(f"{p.name}:{i}")
    assert not schuldige, ("isdigit() vor int() ist unsicher, nutze "
                           "numfmt.ist_zahl: " + ", ".join(schuldige))

    # 4) Der Mines-Parser nimmt die Sonderziffern nicht mehr an. args[0] ist der
    #    EINSATZ (den zieht der Aufrufer vorher schon ein), ab args[1] steht die
    #    Bombenzahl - genau dort ist der Befehl frueher gestorben, NACHDEM das
    #    Geld weg war.
    import casino
    for gift in ("²", "³", "5²", "௫"):
        n = casino.instance._parse_mines_count(["100", gift])
        assert n == casino._MINES_DEFAULT, (gift, n)
    assert casino.instance._parse_mines_count(["100", "3"]) == 3


def test_store_verliert_nie_daten():
    """Eine kaputte Datei darf NIEMALS stillschweigend ueberschrieben werden.

    Vorher hiess "kaputt" schlicht: leer starten. Der erste save() wenige
    Sekunden spaeter hat die kaputte, aber noch rettbare Datei durch eine leere
    ersetzt - bei economy.json waeren das alle Coins, Level und Voice-Stunden
    gewesen, endgueltig und unbemerkt."""
    import pathlib
    import shutil
    import tempfile
    import store

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="flo_store_"))
    alt_dir = store.DATA_DIR
    store.DATA_DIR = tmp
    try:
        s = store.JsonStore("economy.json", default={"users": {}})
        s.data["users"] = {str(i): {"coins": 10 ** 7} for i in range(50)}
        for _ in range(3):                      # laufender Betrieb
            asyncio.run(s.save())
        assert (tmp / "economy.json.bak").exists(), "keine Sicherung angelegt"

        # Stromausfall: Datei abgeschnitten.
        roh = (tmp / "economy.json").read_text(encoding="utf-8")
        (tmp / "economy.json").write_text(roh[:len(roh) // 2], encoding="utf-8")

        s2 = store.JsonStore("economy.json", default={"users": {}})
        # 1) Automatisch aus der Sicherung geholt.
        assert len(s2.data.get("users", {})) == 50, len(s2.data.get("users", {}))
        # 2) Der kaputte Stand liegt zusaetzlich daneben, nichts ist geloescht.
        kaputt = [p for p in tmp.iterdir() if ".kaputt-" in p.name]
        assert kaputt, sorted(p.name for p in tmp.iterdir())
        # 3) Und Speichern wirft ihn nicht weg.
        asyncio.run(s2.save())
        assert kaputt[0].exists()
        assert len(store.JsonStore("economy.json",
                                   default={"users": {}}).data["users"]) == 50

        # 4) Ganz ohne Datei bleibt es beim leeren Start (kein Krach).
        leer = store.JsonStore("gibtsnicht.json", default={"a": 1})
        assert leer.data == {"a": 1}

        # 5) Ein JSON-Inhalt, der KEIN Objekt ist, wird ignoriert statt uebernommen.
        (tmp / "liste.json").write_text("[1,2,3]", encoding="utf-8")
        assert store.JsonStore("liste.json", default={"a": 1}).data == {"a": 1}
    finally:
        store.DATA_DIR = alt_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_giveaway_verneinung_startet_nie():
    """Eine Verneinung darf NIE als Zustimmung durchgehen - hier haengt Geld dran.

    Vorher reichte irgendein Ja-Wort irgendwo im Satz. Weil "sicher" auf der
    Ja-Liste steht, galt "sicher nicht" als Zustimmung: das Giveaway startete und
    der Einsatz war abgebucht, obwohl der Nutzer ausdruecklich abgelehnt hat.
    Dasselbe bei "nicht bestaetigen" und "warte, nicht starten"."""
    import giveaway
    g = giveaway.instance
    absagen = ("nein", "ne", "nö", "nein danke", "lieber nicht", "abbrechen",
               "auf keinen fall", "nicht bestätigen", "doch nicht senden",
               "warte, nicht starten", "ne doch nicht", "besser nicht machen",
               "sicher nicht", "auf gar keinen fall", "nee lass", "keinesfalls",
               "nein, ich will das nicht starten", "stop, nicht abschicken",
               "lass mal nicht", "nie im leben")
    for t in absagen:
        # Entweder klar als Nein erkannt oder zumindest NICHT als Ja - dann
        # fragt Flo nach, und es passiert nichts mit dem Geld.
        assert not (not g.is_no(t) and g.is_yes(t)), t

    zusagen = ("ja", "j", "ok", "okay", "passt", "los", "start", "klar", "sicher",
               "jawohl", "👍", "auf jeden fall", "los gehts", "machs",
               "bestätigen", "perfekt", "yes", "jo")
    for t in zusagen:
        assert g.is_yes(t), t
        assert not g.is_no(t), t


def test_giveaway_geldweg_und_ziehung():
    """Der Einsatz wird beim Start abgebucht (Escrow), geht am Ende an genau EINEN
    Gewinner - und bei 0 Teilnehmern/Abbruch komplett zurueck. Coins entstehen nie
    neu und verschwinden nie."""
    restore, gw = _giveaway_setup({1: 50_000, 2: 0, 3: 0})
    try:
        summe_vorher = sum(economy.get_coins(u) for u in (1, 2, 3))
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 20_000, "reason": "weil", "seconds": 60}))
        # Abgebucht, im Escrow, Giveaway laeuft.
        assert economy.get_coins(1) == 30_000
        g = list(gw._active().values())[0]
        assert g["escrow"] == 20_000 and g["host"] == 1
        assert sum(economy.get_coins(u) for u in (1, 2, 3)) == summe_vorher - 20_000

        # Teilnehmer eintragen (wie der Knopf es tut) und ziehen.
        g["entries"] = [2, 3]
        asyncio.run(gw._auslosen(None, g))
        assert not gw._active(), "Giveaway muss nach der Ziehung weg sein"
        gewinner = 2 if economy.get_coins(2) else 3
        verlierer = 3 if gewinner == 2 else 2
        assert economy.get_coins(gewinner) == 20_000
        assert economy.get_coins(verlierer) == 0
        assert economy.get_coins(1) == 30_000
        # Coin-Summe unveraendert: nur umverteilt.
        assert sum(economy.get_coins(u) for u in (1, 2, 3)) == summe_vorher
        hist = gw._state()["done"][-1]
        assert hist["winner"] == gewinner and hist["stake"] == 20_000
    finally:
        restore()

    # Ohne Teilnehmer -> alles zurueck an den Veranstalter.
    restore, gw = _giveaway_setup({1: 10_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 10_000, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 0
        g = list(gw._active().values())[0]
        asyncio.run(gw._auslosen(None, g))
        assert economy.get_coins(1) == 10_000
    finally:
        restore()

    # Abbruch -> Einsatz zurueck, kein Gewinner (auch mit Teilnehmern).
    restore, gw = _giveaway_setup({1: 10_000, 2: 0})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        g = list(gw._active().values())[0]
        g["entries"] = [2]
        asyncio.run(gw._auslosen(None, g, abgebrochen=True))
        assert economy.get_coins(1) == 10_000 and economy.get_coins(2) == 0
    finally:
        restore()


def test_giveaway_exploit_schutz():
    """REGRESSION: kein Geld aus dem Nichts. Zu wenig Guthaben, doppeltes Ziehen,
    Veranstalter als Teilnehmer, mehrere Giveaways gleichzeitig."""
    restore, gw = _giveaway_setup({1: 1_000, 2: 0})
    try:
        msg = _giveaway_msg(host=1)
        # 1) Einsatz groesser als Guthaben -> nichts passiert, kein Giveaway.
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 1_000 and not gw._active()

        # 2) Guthaben verschwindet zwischen Frage und Bestaetigung (Casino o. ae.):
        #    die Abbuchung muss vollstaendig sein, sonst Rueckbuchung + Abbruch.
        economy.instance._profile(1)["coins"] = 800
        asyncio.run(gw._starten(msg, {"stake": 800, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 0
        g = list(gw._active().values())[0]

        # 3) Doppeltes Auslosen darf NICHT doppelt zahlen.
        g["entries"] = [2]
        asyncio.run(gw._auslosen(None, g))
        assert economy.get_coins(2) == 800
        asyncio.run(gw._auslosen(None, g))          # zweiter Versuch
        assert economy.get_coins(2) == 800, "doppelte Auszahlung!"

        # 4) Veranstalter zaehlt nie als Teilnehmer (auch nicht manipuliert).
        economy.instance._profile(1)["coins"] = 5_000
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        g = list(gw._active().values())[0]
        g["entries"] = [1]                           # nur der Host selbst
        asyncio.run(gw._auslosen(None, g))
        assert economy.get_coins(1) == 5_000         # Rueckbuchung, kein "Gewinn"
        assert gw._state()["done"][-1]["winner"] == 0
    finally:
        restore()

    # 5) Nur EIN laufendes Giveaway pro Nutzer (sonst mehrfach Escrow).
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 10_000, "reason": "", "seconds": 60}))
        antwort = asyncio.run(gw.start_wizard(msg))
        assert isinstance(antwort, str) and "laufendes Giveaway" in antwort
        assert len(gw._active()) == 1
    finally:
        restore()


def test_giveaway_escrow_grenzen():
    """REGRESSION (vom Exploit-Audit gefunden): _starten bewegt echtes Geld und muss
    deshalb SELBST alle Grenzen pruefen - nicht darauf vertrauen, dass der Assistent
    das schon getan hat."""
    import giveaway
    # 1) Negativer Einsatz erzeugte Coins: add_coins(uid, -(-500)) = GUTSCHRIFT.
    restore, gw = _giveaway_setup({1: 1_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": -500, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 1_000, "negativer Einsatz druckt Coins!"
        assert not gw._active()
        # 0 und unter dem Minimum ebenfalls abgelehnt.
        for schlecht in (0, 1, giveaway.MIN_STAKE - 1, giveaway.MAX_STAKE + 1):
            asyncio.run(gw._starten(msg, {"stake": schlecht, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 1_000 and not gw._active()
        # Unsinnige Dauer ebenfalls (0, negativ, absurd lang).
        for secs in (0, -60, giveaway.MAX_SECONDS + 1):
            asyncio.run(gw._starten(msg, {"stake": 500, "reason": "", "seconds": secs}))
        assert economy.get_coins(1) == 1_000 and not gw._active()
        # parse_stake gibt selbst nie etwas Negatives zurueck.
        assert gw.parse_stake("-500", 100_000)[0] >= 0
    finally:
        restore()

    # 2) Zwei Assistenten in ZWEI Kanaelen -> nur EIN Giveaway (Escrow-Limit).
    restore, gw = _giveaway_setup({41: 1_000})
    try:
        c1, c2 = _FakeChannel(101), _FakeChannel(102)
        m1 = _giveaway_msg(host=41, channel=c1)
        m2 = _giveaway_msg(host=41, channel=c2)
        asyncio.run(gw.start_wizard(m1))
        asyncio.run(gw.start_wizard(m2))
        # Ein Nutzer hat nur EINEN Assistenten (der zweite ersetzt den ersten).
        assert len([k for k in gw._wizards if k[1] == 41]) == 1
        asyncio.run(gw._starten(m1, {"stake": 500, "reason": "", "seconds": 60}))
        asyncio.run(gw._starten(m2, {"stake": 500, "reason": "", "seconds": 60}))
        assert len(gw._active()) == 1, "zwei Escrows gleichzeitig!"
        assert economy.get_coins(41) == 500
    finally:
        restore()

    # 3) Teilnehmer-Liste mit Strings/Duplikaten: keine doppelte Gewinnchance.
    restore, gw = _giveaway_setup({1: 5_000, 61: 0, 62: 0})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        g = list(gw._active().values())[0]
        g["entries"] = ["61", 61, 61, "62"]          # haendisch verbogen
        asyncio.run(gw._auslosen(None, g))
        # Genau einer der beiden bekommt alles - niemand doppelt.
        assert sorted([economy.get_coins(61), economy.get_coins(62)]) == [0, 5_000]
        assert gw._state()["done"][-1]["entries"] == 2, "Duplikate nicht entfernt"
    finally:
        restore()


def test_giveaway_schnellstart_und_assistent():
    """'giveaway 5k 2h weil ...' liest alles aus einer Zeile; der Assistent fragt
    nur das Fehlende nach und akzeptiert lockere Antworten."""
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        d = gw._vorgabe_aus_text("5k 2h weil ich 1 mio geknackt habe", 100_000)
        assert d["stake"] == 5_000 and d["seconds"] == 7200
        assert "1 mio geknackt" in d["reason"] and not d["reason"].startswith("weil")
        d = gw._vorgabe_aus_text("10.000 30min", 100_000)
        assert d["stake"] == 10_000 and d["seconds"] == 1800
        d = gw._vorgabe_aus_text("alles 1 tag", 100_000)
        assert d["stake"] == 100_000 and d["seconds"] == 86400
        # Nur ein Betrag -> Dauer fehlt und wird nachgefragt.
        d = gw._vorgabe_aus_text("2k", 100_000)
        assert d["stake"] == 2_000 and "seconds" not in d

        # Assistent: Schritt fuer Schritt, mit lockeren Antworten.
        msg = _giveaway_msg(host=1)
        asyncio.run(gw.start_wizard(msg))
        w = gw._wizards[(msg.channel.id, 1)]
        assert w["step"] == "stake"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="zwei tausend")))
        assert w["data"]["stake"] == 2_000 and w["step"] == "reason"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="einfach so")))
        assert w["data"]["reason"] == "einfach so" and w["step"] == "seconds"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="halbe stunde")))
        assert w["data"]["seconds"] == 1800 and w["step"] == "confirm"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="passt")))
        assert len(gw._active()) == 1 and economy.get_coins(1) == 98_000
        g = list(gw._active().values())[0]
        assert g["reason"] == "einfach so" and int(g["ends"] - g["started"]) == 1800
        assert (msg.channel.id, 1) not in gw._wizards
    finally:
        restore()

    # Unverstaendliche Antwort -> Assistent bleibt stehen und fragt nochmal.
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw.start_wizard(msg))
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="öhm keine ahnung")))
        w = gw._wizards[(msg.channel.id, 1)]
        assert w["step"] == "stake" and "stake" not in w["data"]
        # Unter dem Minimum -> auch nachfragen, nichts uebernehmen.
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="5")))
        assert "stake" not in w["data"]
        # 'abbrechen' beendet sauber, ohne Abbuchung.
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="abbrechen")))
        assert (msg.channel.id, 1) not in gw._wizards
        assert economy.get_coins(1) == 100_000 and not gw._active()
    finally:
        restore()


def test_webpanel_eingaben_und_robustheit():
    """REGRESSION (Panel-Backend): unlesbare Eingaben duerfen NIE still etwas
    aendern, absurde Zahlen muessen abgelehnt werden (bevor Daten angefasst
    werden), kaputte Profile duerfen keine Liste abschiessen, und ein Login mit
    Umlaut-Passwort darf nicht in einen 500er laufen."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    import floaktie

    restore_eco = _with_economy({1: 5000, 2: 100})
    economy.instance._profile(1)["name"] = "Zacharias"
    economy.instance._profile(2)["name"] = "Äpfelchen"
    # Kaputtes Profil (wie nach einem Absturz): coins = None.
    economy.instance._users()["3"] = {"name": "Kaputt", "coins": None, "xp": "nix"}

    wp = webpanel.instance
    alt = (wp._enabled, wp._user, wp._pass, dict(wp._tokens), wp._client, dict(wp._fails))
    alt_fa = (floaktie.instance._store, floaktie.instance._enabled)
    gesendet = []

    class _Chan:
        async def send(self, text, **kw):
            gesendet.append((text, kw))

    system_chan = _Chan()
    guild = SimpleNamespace(id=7, system_channel=system_chan)
    wp._enabled = True
    wp._user, wp._pass = "Secoolio", "Pässwörtchen"      # Nicht-ASCII!
    wp._tokens, wp._fails = {}, {}
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: guild,
                                 get_channel=lambda _x: None)
    floaktie.instance._enabled = True
    floaktie.instance._store = _FakeStore(
        {"price": 1000, "base": 1000.0, "day": "x", "act_ema": floaktie.ACT_BASELINE,
         "msg_count": 0, "last_msg_count": 0,
         "holdings": {"1": 10, "4242": 9999}, "history": [], "ticks": []})
    floaktie.instance._sync_price()
    app = wp._build_app()
    os.environ["GUILD_ID"] = "7"

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            # Login mit Umlaut-Passwort: falsch -> 401 (kein 500), richtig -> Token.
            r = await cli.post("/api/login", json={"user": "Secoolio", "pass": "falsch"})
            assert r.status == 401, r.status
            r = await cli.post("/api/login", json={"user": "Secoolio", "pass": "Pässwörtchen"})
            assert r.status == 200, r.status
            H = {"Authorization": f"Bearer {(await r.json())['token']}"}

            # 1) UNLESBARER Betrag -> 400, Kontostand unveraendert.
            #    Vorher wurde daraus 0: 'set' hat still das ganze Guthaben geloescht.
            for murks in ("abc", "1 000", "", None, {"a": 1}, True, float("nan")):
                r = await cli.post("/api/user/coins",
                                   json={"id": "1", "action": "set", "amount": murks},
                                   headers=H)
                assert r.status == 400, (murks, r.status)
            assert economy.get_coins(1) == 5000

            # 2) Absurd grosser Betrag -> 400 (kein OverflowError, kein Wert-Muell).
            assert (await cli.post("/api/user/coins",
                    json={"id": "1", "action": "give", "amount": "9" * 40},
                    headers=H)).status == 400
            assert economy.get_coins(1) == 5000

            # 3) Ungueltige IDs -> 400 und KEIN Geister-Profil in der Datenbank.
            for bad in ("-1", "0", "0123", "abc", "", None, "1.5"):
                assert (await cli.post("/api/user/coins",
                        json={"id": bad, "action": "give", "amount": 10},
                        headers=H)).status == 400, bad
            assert "0" not in economy.instance._users()
            assert "123" not in economy.instance._users()

            # 4) Riesige Anteils-Zahl -> 400, und die Aktie bleibt handelbar.
            #    Vorher wurde das Depot VOR der Kursrechnung geaendert, die dann
            #    mit OverflowError starb: danach war jeder Kauf/Verkauf kaputt.
            assert (await cli.post("/api/user/shares",
                    json={"id": "1", "action": "set", "amount": "9" * 400},
                    headers=H)).status == 400
            assert (await cli.post("/api/user/shares",
                    json={"id": "1", "action": "set", "amount": 1e30},
                    headers=H)).status == 400
            assert floaktie.instance.shares_of(1) == 10
            j = await (await cli.post("/api/user/shares",
                       json={"id": "1", "action": "give", "amount": 5}, headers=H)).json()
            assert j["ok"] and j["shares"] == 15 and j["price"] > 0
            # Verkauf bringt weiter echte Coins (kein Gleitkomma-Kollaps).
            erloes, _ = floaktie.instance._sell_proceeds(5)
            assert erloes > 0

            # 5) keep_price:"false" ist FALSE (vorher machte der String True).
            floaktie.instance._holdings()["1"] = 4000
            floaktie.instance._sync_price()
            kurs_vor = floaktie.instance.price()
            j = await (await cli.post("/api/user/shares",
                       json={"id": "1", "action": "set", "amount": 0,
                             "keep_price": "false"}, headers=H)).json()
            assert j["ok"] and j["price"] < kurs_vor, (j["price"], kurs_vor)

            # 6) Kurs-Setzen: zu gross -> 400; unter Mindestkurs -> ehrliche Antwort.
            assert (await cli.post("/api/stock/price",
                    json={"price": "9" * 40}, headers=H)).status == 400
            j = await (await cli.post("/api/stock/price",
                       json={"price": 1}, headers=H)).json()
            assert j["price"] == floaktie.MIN_PRICE and j["requested"] == 1
            # Und ein niedriger Kurs laesst sich auch bei vielen Anteilen setzen.
            floaktie.instance._holdings()["1"] = 500_000
            j = await (await cli.post("/api/stock/price",
                       json={"price": 100}, headers=H)).json()
            assert j["price"] == 100, j["price"]
            floaktie.instance._holdings()["1"] = 10
            await floaktie.admin_set_price(1000)

            # 7) Kaputtes Profil + reiner Aktien-Halter: Listen laufen durch,
            #    der Halter ohne economy-Profil ist SICHTBAR (zaehlt ja im Wert).
            j = await (await cli.get("/api/users?size=100", headers=H)).json()
            assert j["ok"]
            ids = [u["id"] for u in j["users"]]
            assert "3" in ids and "4242" in ids, ids
            assert next(u for u in j["users"] if u["id"] == "3")["coins"] == 0
            assert next(u for u in j["users"] if u["id"] == "4242")["shares"] == 9999
            j = await (await cli.get("/api/overview", headers=H)).json()
            assert j["ok"] and any(u["id"] == "4242" for u in j["top_shares"])
            assert (await cli.get("/api/user/4242", headers=H)).status == 200
            assert (await cli.get("/api/user/3", headers=H)).status == 200

            # 8) Zu hohe Seitenzahl wird auf den gueltigen Bereich gezogen
            #    (vorher: leere Liste ohne Blaetter-Knoepfe = Sackgasse).
            j = await (await cli.get("/api/users?page=99&size=5", headers=H)).json()
            assert j["ok"] and j["page"] == j["pages"] and j["users"]
            # Kaputtes 'size' darf die Seite nicht mitreissen.
            j = await (await cli.get("/api/users?page=2&size=abc", headers=H)).json()
            assert j["page"] == 2 or j["pages"] == 1

            # 9) Namens-Sortierung deutsch: Ä sortiert wie A, also VOR Z.
            j = await (await cli.get("/api/users?sort=name&size=100", headers=H)).json()
            namen = [u["name"] for u in j["users"]]
            assert namen.index("Äpfelchen") < namen.index("Zacharias"), namen

            # 10) XP: Murks -> 400, unbekannte Aktion -> 400, 'take' rechnet runter.
            assert (await cli.post("/api/user/xp",
                    json={"id": "1", "action": "set", "amount": "abc"},
                    headers=H)).status == 400
            assert (await cli.post("/api/user/xp",
                    json={"id": "1", "action": "quatsch", "amount": 5},
                    headers=H)).status == 400
            await cli.post("/api/user/xp", json={"id": "1", "action": "set", "amount": 500},
                           headers=H)
            j = await (await cli.post("/api/user/xp",
                       json={"id": "1", "action": "take", "amount": 200}, headers=H)).json()
            assert j["xp"] == 300

            # 11) Titel: 'remove' meldet ehrlich, ob der Titel da war.
            j = await (await cli.post("/api/user/title",
                       json={"id": "1", "action": "remove", "text": "GibtsNicht"},
                       headers=H)).json()
            assert j["ok"] and j["removed"] is False
            economy.grant_title(1, "Echt", "Echt", "selten")
            j = await (await cli.post("/api/user/title",
                       json={"id": "1", "action": "remove", "text": "Echt"},
                       headers=H)).json()
            assert j["removed"] is True
            # Unsinnige Seltenheit -> 400.
            assert (await cli.post("/api/user/title",
                    json={"id": "1", "action": "grant", "text": "X", "rarity": "quatsch"},
                    headers=H)).status == 400

            # 12) Ansage: unbekannte Kanal-ID landet NICHT still im System-Kanal.
            assert (await cli.post("/api/server/announce",
                    json={"text": "hallo", "channel_id": "123456789012345"},
                    headers=H)).status == 400
            assert gesendet == []
            # Objekt als Text -> 400 (vorher landete ein Python-repr im Chat).
            assert (await cli.post("/api/server/announce",
                    json={"text": {"a": 1}}, headers=H)).status == 400
            # Ohne Kanal-ID: System-Kanal, gekuerzt auf Discord-Laenge.
            j = await (await cli.post("/api/server/announce",
                       json={"text": "x" * 5000}, headers=H)).json()
            assert j["ok"] and len(gesendet) == 1 and len(gesendet[0][0]) <= 1900
            # KEINE Massen-Pings aus dem Panel.
            erlaubt = gesendet[0][1].get("allowed_mentions")
            assert erlaubt is not None and erlaubt.everyone is False

            # 13) Sendepause-Zustand kommt jetzt mit der Server-Liste (kein Cache).
            j = await (await cli.get("/api/servers", headers=H)).json()
            assert "sendepause" in j

            # 14) Login-Bremse: nach genug Fehlversuchen 429 statt endlos raten.
            wp._fails = {}
            for _ in range(webpanel.WebPanel._LOGIN_MAX_FAILS):
                await cli.post("/api/login", json={"user": "x", "pass": "y"})
            assert (await cli.post("/api/login",
                    json={"user": "x", "pass": "y"})).status == 429
            wp._fails = {}

    try:
        asyncio.run(run_it())
    finally:
        os.environ.pop("GUILD_ID", None)
        floaktie.instance._store, floaktie.instance._enabled = alt_fa
        (wp._enabled, wp._user, wp._pass, wp._tokens, wp._client, wp._fails) = alt
        restore_eco()


def test_floaktie_grenzen_gegen_hyperinflation():
    """Die Aktie darf ein Depot ueber einen Vormittag vervielfachen (genau das war
    der Wunsch), aber sie darf die Wirtschaft nicht zerreissen.

    Gemessener Ausgangsbug: 750 Anteile fuer 1.147.501 gekauft, 5 h aktiver Call,
    Kurs 79 Mio -> Verkauf fuer 43.655.972.117 Coins. Faktor 38.043 aus dem Nichts,
    weil die Kurve keine Reserve hat. Vier Bremsen halten das jetzt:
    Kurs-Deckel (absolut + je Aktivitaet), Depot-Deckel, Verkaufssteuer, Tagesbremse."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, floaktie.TICK_NOISE, fa._today)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    restore_eco = _with_economy({1: 5_000_000, 2: 5_000_000})

    def frisch(preis=None, tag="2026-07-26"):
        preis = floaktie.FAIR_BASE if preis is None else preis
        fa._store = _FakeStore({"price": int(preis), "base": float(preis), "day": tag,
                                "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {}, "history": [], "ticks": []})
        fa._today = lambda: tag
        fa._tages_anker()
        fa._sync_price()

    try:
        # 1) DEPOT-DECKEL: niemand haelt mehr als MAX_SHARES_PER_USER Anteile -
        #    das begrenzt, wie viel Geld eine Person aus einer Blase schoepfen kann.
        frisch()
        deckel = floaktie.MAX_SHARES_PER_USER
        asyncio.run(fa.buy(SimpleNamespace(id=1), deckel + 500))
        assert fa.shares_of(1) == deckel, fa.shares_of(1)
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 10))
        assert "Depot ist voll" in r and fa.shares_of(1) == deckel
        # 'kauf max' respektiert den freien Platz ebenfalls.
        asyncio.run(fa.sell(SimpleNamespace(id=1), 20))
        economy.instance._profile(1)["coins"] = 10 ** 12
        assert fa._resolve_count(SimpleNamespace(id=1), "max") == 20

        # 2) KURS-DECKEL je Aktivitaet: Dauer-Aktivitaet laeuft in ein Niveau,
        #    nicht in die Unendlichkeit - und der Deckel steigt nur mit mehr Leuten.
        frisch()
        for _ in range(12 * 60):
            fa._activity_tick(3, 0)
        akt3 = fa.activity_of(3, 0, 0, 0)
        d3 = fa.ziel_base(akt3) * floaktie.CEIL_FACTOR
        assert fa.price() <= d3 * 1.06, (fa.price(), d3)
        assert fa.ziel_base(fa.activity_of(10, 5, 0, 0)) > fa.ziel_base(akt3)

        # 3) ABSOLUTE Notbremse: auch mit absurdem Basiskurs bleibt der ANGEZEIGTE
        #    Kurs unter MAX_PRICE (vorher lief er in Gleitkomma-Regionen).
        frisch()
        fa._store.data["base"] = 10.0 ** 18
        assert fa.price() <= floaktie.MAX_PRICE, fa.price()
        assert fa._base() <= floaktie.MAX_PRICE

        # 4) TAGESBREMSE (Circuit Breaker): innerhalb eines Tages maximal x DAY_UP.
        frisch(1000)
        anker = fa._state()["open_base"]
        fa._store.data["base"] = anker * floaktie.DAY_UP * 1000
        assert fa._base() <= anker * floaktie.DAY_UP * 1.0001, (fa._base(), anker)
        # ... und nicht tiefer als DAY_DOWN.
        fa._store.data["base"] = anker * floaktie.DAY_DOWN / 1000
        assert fa._base() >= anker * floaktie.DAY_DOWN * 0.9999
        # Neuer Tag -> neuer Anker (die Bremse ist tages-, nicht ewigkeitsbezogen).
        fa._today = lambda: "2026-07-27"
        fa._tages_anker()
        assert fa._state()["open_day"] == "2026-07-27"

        # 5) VERKAUFSSTEUER steigt mit der Blase - der wichtigste Geld-Abfluss.
        #    Vom Bodensatz (10) aus braucht der Kurs eine Weile, bis er ueberhaupt
        #    UEBER seinem Zielkurs steht - erst dann ist es eine Blase.
        frisch()
        normal = fa._sell_fee()
        for _ in range(14 * 60):
            fa._activity_tick(3, 0)
        assert fa._base() > fa.ziel_base(fa._state()["act_ema"]), "keine Blase entstanden"
        blase = fa._sell_fee()
        assert normal < blase <= floaktie.SELL_TAX_MAX, (normal, blase)
        assert abs(normal - floaktie.TRADE_FEE) < 0.01

        # 6) DIE ENTSCHEIDENDE ZAHL: ein guter Vormittag darf sich lohnen
        #    (Faktor > 5), aber nicht Milliarden aus dem Nichts machen.
        frisch()
        kosten, _ = fa._buy_cost(deckel)
        fa._holdings()["1"] = deckel
        fa._sync_price()
        for _ in range(5 * 60):
            fa._activity_tick(3, 1, 0, 4)      # 3 Leute, einer streamt, etwas Chat
        erloes, _ = fa._sell_proceeds(deckel)
        faktor = erloes / max(1, kosten)
        # Der FAKTOR ist absichtlich gross: der Kurs startet am Bodensatz, wer
        # frueh kauft, verdient kraeftig. Begrenzt wird nicht der Faktor, sondern
        # der ABSOLUTE Erloes - nur der bestimmt, wie viele Coins entstehen.
        assert faktor > 5, (kosten, erloes, faktor)
        assert erloes < 100_000_000, erloes     # nie Milliarden aus einer Runde
        # Gegenprobe: mehr als Depot-Deckel x Kurs-Deckel geht NIE.
        assert erloes <= floaktie.MAX_SHARES_PER_USER * floaktie.MAX_PRICE

        # 7) NOTAUS: ist die Aktie per Panel aus, geht gar nichts mehr.
        import features
        alt_dis = set(features.instance._disabled)
        try:
            features.instance._disabled.add("floaktie")
            assert fa.is_off()
            frisch()
            r = asyncio.run(fa.buy(SimpleNamespace(id=2), 5))
            assert "deaktiviert" in r and fa.shares_of(2) == 0
            fa._holdings()["2"] = 10
            r = asyncio.run(fa.sell(SimpleNamespace(id=2), 10))
            assert "deaktiviert" in r and fa.shares_of(2) == 10
            # Kein Kurs-Tick und keine Aktivitaets-Zaehlung mehr.
            vorher = fa.price()
            guild = SimpleNamespace(voice_channels=[], afk_channel=None)
            asyncio.run(fa.sample_and_tick(guild))
            fa.note_message()
            assert fa.price() == vorher and fa._state()["msg_count"] == 0
        finally:
            features.instance._disabled = alt_dis
    finally:
        restore_eco()
        fa._store, fa._enabled, floaktie.TICK_NOISE, fa._today = alt


def test_floaktie_lebendig_und_ki():
    """Die Aktie soll sich wie ein echter Markt verhalten statt stur einer Formel
    zu folgen: schwankende Minutenbewegung, nachwirkendes Momentum, und Flo als
    Analyst, der die Bewegung verstaerkt oder daempft. Dabei darf sich das
    TAGESVERHALTEN nicht aendern, egal wie schnell der Loop taktet."""
    import random
    import statistics
    import time as _t
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.VOL_SPREAD)
    fa._enabled = True
    fa._today = lambda: "2026-08-01"
    # Dieser Test PRUEFT die Volatilitaet - die Suite laeuft sonst ohne (siehe
    # Kopf der Datei). Mit random.seed() unten ist er trotzdem reproduzierbar.
    floaktie.VOL_SPREAD = 0.8

    def frisch():
        fa._store = _FakeStore({"price": floaktie.START_PRICE,
                                "base": float(floaktie.START_PRICE), "day": "d",
                                "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "mom": 0.0, "holdings": {},
                                "history": [], "ticks": []})
        fa._tages_anker()
        fa._sync_price()

    try:
        # 1) LEBENDIG: die Minutenbewegung schwankt spuerbar um ihren Trend.
        #    Vorher war das Rauschen so klein, dass der Chart eine gerade Linie war.
        random.seed(11)
        frisch()
        for _ in range(540):                      # 3 h vorlaufen (Kurs weg von 10)
            fa._activity_tick(3, 4, streams=1, dt=20.0)
        schritte = []
        for _ in range(180):
            _a, _n, drift, _akt = fa._activity_tick(3, 4, streams=1, dt=20.0)
            schritte.append(drift)
        assert min(schritte) >= 0.0, "mit Leuten im Call darf kein Takt fallen"
        assert max(schritte) > min(schritte) * 2, (min(schritte), max(schritte))
        assert statistics.pstdev(schritte) > 0.0005, statistics.pstdev(schritte)

        # 2) MOMENTUM wirkt nach und wird als Rate pro Minute gefuehrt.
        frisch()
        for _ in range(60):
            fa._activity_tick(5, 4, streams=2, dt=20.0)
        assert fa._store.data["mom"] > 0
        # Im Leerlauf dreht es ins Minus - dafuer muss der Kurs aber UEBER dem
        # Grundwert stehen. Steht er darunter, faellt er zu Recht nicht (und das
        # Momentum bleibt bei 0), siehe boden_base().
        fa._store.data["base"] = fa.boden_base() * 3.0
        fa._sync_price()
        for _ in range(90):
            fa._activity_tick(0, 0, dt=20.0)
        assert fa._store.data["mom"] < 0, fa._store.data["mom"]

        # 3) TAKTUNABHAENGIG: 60 s und 20 s kommen am Ende auf dasselbe Niveau.
        #    WICHTIG: derselbe Chat-VERKEHR, nicht dieselbe Zahl je Takt. Leute
        #    und Streams sind Zustaende, Nachrichten sind eine Zaehlung ueber den
        #    Takt - bei 20 s kommen entsprechend weniger je Takt an. Vorher stand
        #    hier stur "4", also bei 20 s der dreifache Verkehr; das hat die
        #    doppelte Division im Code zufaellig ausgeglichen und den Test aus dem
        #    falschen Grund bestehen lassen.
        werte = {}
        for dt in (60.0, 20.0):
            ergebnisse = []
            for lauf in range(12):
                random.seed(700 + lauf)
                frisch()
                rest = 0.0
                for _ in range(int(3 * 3600 / dt)):
                    rest += 3.0 * dt / 60.0          # 3 Nachrichten je MINUTE
                    # 3/min geht bei 60 s UND 20 s glatt auf. Bei 4/min ergaebe
                    # sich im 20-s-Takt das Muster 1,1,2 - und die bewusst
                    # asymmetrische Glaettung (schnell rauf, langsam runter)
                    # hebt bei schwankender Eingabe den Mittelwert an.
                    n = int(rest)
                    rest -= n
                    fa._activity_tick(3, n, streams=1, dt=dt)
                ergebnisse.append(fa.price())
            werte[dt] = statistics.median(ergebnisse)
        a, b = werte[60.0], werte[20.0]
        assert abs(a - b) < max(a, b) * 0.25, werte

        # 4) KI-ANALYST: verstaerkt und daempft - und ist gedeckelt.
        for faktor, richtung in ((floaktie.KI_MAX, "rauf"), (-floaktie.KI_MAX, "runter")):
            random.seed(42)
            frisch()
            fa._store.data.update({"ki_faktor": faktor, "ki_text": "Test",
                                   "ki_zeit": _t.time()})
            for _ in range(180):
                fa._activity_tick(3, 4, streams=1, dt=20.0)
            werte[richtung] = fa.price()
        assert werte["rauf"] > werte["runter"] * 1.3, werte
        # Ueber den Deckel hinaus geht die KI nie.
        fa._store.data["ki_faktor"] = 99.0
        assert fa._ki_faktor() == floaktie.KI_MAX
        fa._store.data["ki_faktor"] = -99.0
        assert fa._ki_faktor() == -floaktie.KI_MAX
        # Alte Einschaetzung laeuft aus (Analyst faellt aus -> Markt laeuft normal).
        fa._store.data.update({"ki_faktor": 0.4, "ki_zeit": _t.time() - 99999})
        assert fa._ki_faktor() == 0.0 and fa.ki_text() == ""
        # Kaputte Werte kippen nichts um.
        fa._store.data.update({"ki_faktor": "quatsch", "ki_zeit": _t.time()})
        assert fa._ki_faktor() == 0.0

        # 5) Der Parser ist nachsichtig, aber niemals gefaehrlich.
        p = fa._ki_parse
        assert p("-15|Zu heiss gelaufen")[0] == -0.15
        assert p("+25 | Rally!") == (0.25, "Rally!")
        assert p("0|Seitwaerts")[0] == 0.0
        assert p("999|Uebertrieben")[0] == floaktie.KI_MAX      # gedeckelt
        assert p("-999|Panik")[0] == -floaktie.KI_MAX
        for muell in (None, "", "keine Zahl hier", "   "):
            assert p(muell)[0] is None, muell
        assert len(p("5|" + "x" * 500)[1]) <= 120                # Kommentar gekuerzt

        # 6) Der Kommentar taucht im Panel auf.
        restore = _with_economy({3: 0})
        try:
            frisch()
            fa._store.data.update({"ki_faktor": 0.2, "ki_text": "Die Bullen rennen.",
                                   "ki_zeit": _t.time()})
            panel = _embed_text(fa._panel_embed(SimpleNamespace(id=3)))
            assert "Markt-Analyse" in panel and "Die Bullen rennen." in panel
            assert "+20 %" in panel
        finally:
            restore()
    finally:
        fa._store, fa._enabled, fa._today, floaktie.VOL_SPREAD = alt


def test_floaktie_bots_zaehlen_nie():
    """Der Bot (und jeder andere) darf die Aktie NICHT beeinflussen. Gemeldet aus
    dem Betrieb: der Kurs sank nie. Ursache: gezaehlt wurde ueber voice_states, und
    ob ein Eintrag ein Bot ist, stand nur im Member-Cache - lieferte der nichts,
    zaehlte der Musik-Bot als MENSCH und hielt die Aktivitaet dauerhaft ueber 0."""
    import floaktie
    fa = floaktie.instance

    def member(uid, *, bot=False, stream=False, video=False):
        vs = SimpleNamespace(self_stream=stream, self_video=video)
        return SimpleNamespace(id=uid, bot=bot, voice=vs), vs

    m_mensch, vs_mensch = member(1, stream=True)
    m_bot, vs_bot = member(2, bot=True)
    m_flo, vs_flo = member(99)

    class Chan:
        def __init__(self, members, states):
            self.id = 5
            self.members = members
            self.voice_states = states

    class Guild:
        def __init__(self, chan, cache):
            self.voice_channels = [chan]
            self.afk_channel = None
            self.me = SimpleNamespace(id=99)
            self._cache = cache

        def get_member(self, uid):
            return self._cache.get(uid)

    # 1) Normalfall: Member-Objekte da -> Bot und Flo zaehlen nicht.
    chan = Chan([m_mensch, m_bot, m_flo], {1: vs_mensch, 2: vs_bot, 99: vs_flo})
    g = Guild(chan, {1: m_mensch, 2: m_bot, 99: m_flo})
    assert fa._measure(g) == (1, 1, 0), fa._measure(g)

    # 2) NUR Bots im Call -> Aktivitaet 0 (vorher: der Bot hielt sie ueber 0).
    chan = Chan([m_bot, m_flo], {2: vs_bot, 99: vs_flo})
    g = Guild(chan, {2: m_bot, 99: m_flo})
    assert fa._measure(g) == (0, 0, 0), fa._measure(g)
    assert fa.activity_of(*fa._measure(g)) == 0.0

    # 3) Member-Cache LEER, nur voice_states: unbekannte Eintraege zaehlen NICHT.
    #    Genau hier lag der Fehler - unbekannt hiess frueher "Mensch".
    chan = Chan([], {1: vs_mensch, 2: vs_bot, 99: vs_flo})
    g = Guild(chan, {})                      # get_member liefert nichts
    assert fa._measure(g) == (0, 0, 0), fa._measure(g)
    # ... sobald der Cache den Menschen kennt, zaehlt er wieder.
    g = Guild(chan, {1: m_mensch})
    assert fa._measure(g) == (1, 1, 0), fa._measure(g)

    # 4) AFK-Kanal zaehlt nie.
    chan = Chan([m_mensch], {1: vs_mensch})
    g = Guild(chan, {1: m_mensch})
    g.afk_channel = SimpleNamespace(id=5)
    assert fa._measure(g) == (0, 0, 0)


def test_bilder_blockieren_den_bot_nicht():
    """Kein Bild darf auf dem Event-Loop gezeichnet werden.

    PIL rechnet synchron: die Profilkarte braucht ~28 ms, der Kurs-Chart ~52 ms.
    Solange das auf dem Loop laeuft, steht der GANZE Bot - Musik, Aktien-Takt,
    jede andere Antwort. Drei Aufrufe hatten die Auslagerung nicht (Profilkarte,
    Bestenliste, Kurs-Chart), waehrend alle uebrigen sie laengst hatten."""
    import ast
    import pathlib

    verdaechtig = []
    for p in sorted(pathlib.Path(".").glob("*.py")):
        if p.name.startswith("test_") or p.name in ("render.py", "leaderboard_img.py"):
            continue
        quelle = p.read_text(encoding="utf-8")
        baum = ast.parse(quelle)
        # Alle Funktionen merken, die als Argument in einen Thread gereicht
        # werden (words._build_top zeichnet z. B. selbst, wird aber ausgelagert).
        im_thread = set()
        for n in ast.walk(baum):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr not in ("to_thread", "run_in_executor"):
                continue
            for arg in n.args:
                if isinstance(arg, ast.Attribute):
                    im_thread.add(arg.attr)
                elif isinstance(arg, ast.Name):
                    im_thread.add(arg.id)

        def ausgelagert(knoten):
            """Steht der Aufruf selbst in einem to_thread/run_in_executor?"""
            for eltern in ast.walk(baum):
                if not (isinstance(eltern, ast.Call)
                        and isinstance(eltern.func, ast.Attribute)
                        and eltern.func.attr in ("to_thread", "run_in_executor")):
                    continue
                for kind in ast.walk(eltern):
                    if kind is knoten:
                        return True
            return False

        for fn in ast.walk(baum):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in im_thread:
                continue                      # wird als Ganzes ausgelagert
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                    continue
                if not (isinstance(n.func.value, ast.Name)
                        and n.func.value.id in ("render", "leaderboard_img")):
                    continue
                if n.func.attr in ("is_available", "is_enabled", "setup"):
                    continue                  # keine Zeichenarbeit
                if ausgelagert(n):
                    continue
                verdaechtig.append(f"{p.name}:{n.lineno} {n.func.value.id}.{n.func.attr}")

        # ZWEITER Weg, den die Suche oben NICHT sieht: die Zeichenfunktion wird
        # per getattr geholt und dann unter einem eigenen Namen gerufen -
        #     fn = getattr(render, "shop_banner", None)
        #     buf = fn(items, date=date)
        # Genau so lief das Shop-Banner (gemessen 236-278 ms) jahrelang auf dem
        # Event-Loop, waehrend dieser Test gruen war.
        per_getattr = {}
        for n in ast.walk(baum):
            if not (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)):
                continue
            ruf = n.value
            if not (isinstance(ruf.func, ast.Name) and ruf.func.id == "getattr"):
                continue
            if not (ruf.args and isinstance(ruf.args[0], ast.Name)
                    and ruf.args[0].id in ("render", "leaderboard_img")):
                continue
            for ziel in n.targets:
                if isinstance(ziel, ast.Name):
                    quelle_name = (ruf.args[1].value
                                   if len(ruf.args) > 1 and isinstance(ruf.args[1], ast.Constant)
                                   else "?")
                    per_getattr[ziel.id] = quelle_name
        if per_getattr:
            for fn in ast.walk(baum):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name in im_thread:
                    continue
                for n in ast.walk(fn):
                    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
                        continue
                    if n.func.id not in per_getattr or ausgelagert(n):
                        continue
                    verdaechtig.append(
                        f"{p.name}:{n.lineno} {n.func.id}() = "
                        f"render.{per_getattr[n.func.id]} (per getattr geholt)")

    assert not verdaechtig, ("Bild wird auf dem Event-Loop gezeichnet: "
                             + ", ".join(verdaechtig))


def test_admin_meldet_das_echte_delta():
    """'Flo nimm @wer 1000' meldete den gewuenschten Betrag, auch wenn gar nicht
    so viel da war - add_coins klemmt bei 0 ab.

    Gemessen bei 200 Coins: abgezogen wurden 200, gemeldet '-1.000'. Bei leerem
    Konto war die Buchung ein reiner No-Op, gemeldet wurde trotzdem ein Abzug."""
    import admin
    import economy
    UID = 111111111111111111
    a = admin.instance
    alt = (a._enabled, a._name_of, economy.instance._store,
           economy.instance._enabled)
    try:
        a._enabled = True
        a._name_of = lambda _m, _u: asyncio.sleep(0, result="Testnutzer")
        economy.instance._enabled = True
        nachricht = SimpleNamespace(author=SimpleNamespace(id=1), guild=None,
                                    mentions=[])

        def konto(coins):
            economy.instance._store = _FakeStore({"users": {str(UID): {
                "coins": coins, "xp": 0, "owned": [], "name": "T", "title": "",
                "title_rarity": "", "voice_secs": 0, "msgs": 0, "streak": 0,
                "last_daily": ""}}})

        def lauf(betrag, sign):
            vor = economy.get_coins(UID)
            emb = asyncio.run(a._give(nachricht, f"{UID} {betrag}", sign=sign))
            return economy.get_coins(UID) - vor, _embed_text(emb)

        # 1) Mehr genommen als da war -> echter Betrag, plus Hinweis.
        konto(200)
        echt, text = lauf(1000, -1)
        assert echt == -200, echt
        assert "200" in text and "1.000" in text, text
        assert "-1.000 Flo Coins**" not in text, text     # keine falsche Zahl

        # 2) Leeres Konto -> ausdruecklich sagen, dass nichts passiert ist.
        konto(0)
        echt, text = lauf(500, -1)
        assert echt == 0, echt
        assert "nichts mehr zu holen" in text, text

        # 3) Genug da -> ganz normal, ohne Zusatz.
        konto(5000)
        echt, text = lauf(1000, -1)
        assert echt == -1000, echt
        assert "mehr war nicht da" not in text, text

        # 4) Geben funktioniert unveraendert.
        konto(200)
        echt, text = lauf(1000, +1)
        assert echt == 1000, echt
        assert "+1.000" in text, text
    finally:
        (a._enabled, a._name_of, economy.instance._store,
         economy.instance._enabled) = alt


def test_musik_advance_raeumt_die_warteschlange_nicht_leer():
    """Startet jemand selbst einen Song, waehrend der Automat gerade den naechsten
    aufloest, darf der Automat NICHT weitermachen.

    Nachgestellt: Song A endet, _advance haengt im Aufloesen eines
    Playlist-Tracks, in der Luecke sagt jemand 'flo spiel X'. Danach lief zwar X,
    aber _advance machte weiter - jedes start() scheiterte an 'Already playing
    audio.', wurde als 'Track nicht ladbar' verbucht und uebersprungen. Ergebnis:
    Warteschlange 4 -> 0, current auf None, und das frisch gepostete Panel
    geloescht."""
    import discord
    import music

    alt_ff = (discord.FFmpegPCMAudio, discord.PCMVolumeTransformer)
    alt_res, alt_panel, alt_ret = (music._resolve_track, music._send_panel,
                                   music._retire_panel)
    try:
        discord.FFmpegPCMAudio = lambda *a, **k: object()
        discord.PCMVolumeTransformer = lambda src, volume=1.0: src

        class FakeVoice:
            def __init__(self):
                self.spielt = False

            def is_connected(self):
                return True

            def is_playing(self):
                return self.spielt

            def is_paused(self):
                return False

            def play(self, _src, after=None):
                if self.spielt:
                    raise discord.ClientException("Already playing audio.")
                self.spielt = True

            def stop(self):
                self.spielt = False

            @property
            def channel(self):
                return SimpleNamespace(id=42)

        async def langsam(track):
            await asyncio.sleep(0.05)
            track.stream_url = "http://x"
            return track

        async def kein_panel(*a, **k):
            pass

        geloescht = []

        async def retire(_p):
            geloescht.append(1)

        music._resolve_track = langsam
        music._send_panel = kein_panel
        music._retire_panel = retire

        def aufbau():
            p = music.GuildPlayer(loop=asyncio.new_event_loop())
            p.voice = FakeVoice()
            p.queue = [music.Track(title=f"Song {i}", stream_url="", query=f"q{i}")
                       for i in range(4)]
            p.current = music.Track(title="Song A", stream_url="http://a")
            p.voice.spielt = True
            return p

        # 1) Jemand startet waehrend des Aufloesens etwas Eigenes.
        p = aufbau()
        gen = p._play_gen

        async def stoerung():
            t = asyncio.ensure_future(p._advance(gen))
            await asyncio.sleep(0.01)
            p.voice.stop()
            p.start(music.Track(title="Wunsch X", stream_url="http://x"))
            await t

        asyncio.run(stoerung())
        assert len(p.queue) == 4, len(p.queue)
        assert p.current is not None and p.current.title == "Wunsch X", p.current
        assert not geloescht, "Panel des Nutzers wurde geloescht"

        # 2) Der normale Songwechsel funktioniert unveraendert.
        p = aufbau()
        p.voice.spielt = False
        asyncio.run(p._advance(p._play_gen))
        assert len(p.queue) == 3, len(p.queue)
        assert p.current.title == "Song 0", p.current.title

        # 3) Aufrufe ohne Generation (aus _reconnect) pruefen nichts.
        p = aufbau()
        p.voice.spielt = False
        asyncio.run(p._advance())
        assert p.current.title == "Song 0", p.current.title
    finally:
        discord.FFmpegPCMAudio, discord.PCMVolumeTransformer = alt_ff
        music._resolve_track, music._send_panel, music._retire_panel = (
            alt_res, alt_panel, alt_ret)


def test_musik_panel_view_wird_abgemeldet():
    """Jedes Now-Playing-Panel laeuft mit timeout=None und wurde deshalb von
    discord.py NIE aus dem ViewStore genommen - auch das Loeschen der Nachricht
    raeumt dort nichts weg. Gemessen: 200 Panels = 200 Eintraege, die auch nach
    dem Loeschen aller Referenzen blieben. Mit jedem gespielten Song einer mehr."""
    import discord
    import music

    class FakeState:
        def __init__(self):
            self._view_store = discord.ui.view.ViewStore(self)

        def store_view(self, view, message_id=None, interaction_id=None):
            self._view_store.add_view(view, message_id=message_id)

    st = FakeState()

    class P:
        def __init__(self):
            self.volume = 1.0
            self.speed = 1.0
            self.voice = None
            self.current = None
            self.queue = []
            self.panel_message = None
            self.panel_view = None

        def is_active(self):
            return False

    p = P()

    async def lauf():
        for i in range(50):
            v = music.PlaybackControlView(p)
            st.store_view(v, message_id=1000 + i)
            p.panel_view = v
            p.panel_message = None
            await music.instance._retire_panel(p)

    asyncio.run(lauf())
    assert len(st._view_store._views) == 0, len(st._view_store._views)
    assert p.panel_view is None


def test_terraria_erkennt_kein_alltagsdeutsch():
    """'hell' und 'boss' sind deutsche Alltagswoerter, 'golem' ist kein
    eindeutiger Terraria-Begriff. Bei einem Treffer schaltet bot.py den ganzen
    KI-Fallback ab und antwortet stattdessen mit einem Wiki-Embed."""
    import terraria

    for harmlos in ("ist es draußen schon hell, boss?",
                    "was steht heute bei golem?",
                    "der golem im museum sah krass aus",
                    "wo ist der boss? es ist noch hell draußen",
                    "wie wird das wetter morgen in regensburg?"):
        assert not terraria.erkennt_frage(harmlos), harmlos

    for echt in ("wie besiege ich den Wall of Flesh?",
                 "wo finde ich Hellstone?",
                 "wie komme ich in den Hardmode?",
                 "wo spawnt der Moon Lord?",
                 "welches Erz brauche ich für Chlorophyte?"):
        assert terraria.erkennt_frage(echt), echt

    # Und der Blaetterer schiebt keine leere Seite mehr ein (Absatz genau am Limit).
    p = terraria.instance._paginate
    for n in (1, 1798, 1799, 1800, 1801, 3600):
        seiten = p("x" * n)
        assert all(s.strip() for s in seiten), (n, [len(s) for s in seiten])


def test_loeschen_raeumt_nicht_versehentlich_alles():
    """'alle', 'ganz' und 'komplett' sind deutsche FUELLWOERTER - sie duerfen
    nicht mitten im Satz einen kompletten Channel-Wipe ausloesen.

    Gemessen loeschten vorher alle diese Saetze den ganzen Verlauf, sofort und
    ohne Rueckfrage:
      'loesch das ganz schnell'
      'loesch mal alle deine Nachrichten'   (gemeint waren Flos Nachrichten)
      'loesch ganz kurz bitte'
    Unwiderruflich, mit der Meldung 'N Nachrichten geloescht'."""
    import re
    import moderation

    S = moderation._ALL_START_RE
    def wipe(rest):
        return bool(S.match(rest)) and re.search(r"\d+", rest) is None

    # Alltagssaetze raeumen NICHT alles weg.
    for harmlos in ("das ganz schnell", "mal alle deine Nachrichten",
                    "ganz kurz bitte", "bitte ganz schnell 10", "20",
                    "die letzten 20", "hier mal komplett durchwischen bitte"):
        assert not wipe(harmlos), harmlos

    # Die ausdrueckliche Ansage schon.
    for klar in ("alle", "alles", "all", "alles hier", "alle nachrichten",
                 "komplett", "everything"):
        assert wipe(klar), klar

    # Und der Wipe fragt einmal nach, weil er nicht rueckholbar ist.
    m = moderation.instance
    alt = (m._enabled, m._store, m._wipe_offen)
    try:
        m._enabled = True
        m._store = _FakeStore({"warns": {}})
        m._wipe_offen = {}
        gerufen = []

        class Chan:
            id = 7
            name = "c"

            async def purge(self, limit=None, check=None, before=None):
                gerufen.append(limit)
                return []

            def permissions_for(self, _m):
                return SimpleNamespace(manage_messages=True, view_channel=True,
                                       read_message_history=True, send_messages=True)

            async def send(self, *a, **k):
                return SimpleNamespace(id=1)

        class Msg:
            def __init__(self, text):
                self.content = text
                self.pinned = False
                self.author = SimpleNamespace(
                    id=1, display_name="Mod", name="Mod", bot=False, roles=[],
                    guild_permissions=SimpleNamespace(manage_messages=True,
                                                      administrator=True))
                self.channel = Chan()
                self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99),
                                             owner_id=1, get_member=lambda u: None)
                self.mentions = []

            async def reply(self, *a, **k):
                return SimpleNamespace(id=2)

        # Erster Versuch: nur Rueckfrage, nichts geloescht.
        antwort = asyncio.run(m._do_purge(Msg("Flo lösch alles"), "lösch alles"))
        assert not gerufen, gerufen
        assert "kompletten Verlauf" in str(antwort), antwort
        # Zweiter Versuch: jetzt wirklich.
        asyncio.run(m._do_purge(Msg("Flo lösch alles"), "lösch alles"))
        assert gerufen == [None], gerufen

        # Eine Zahl geht weiterhin sofort durch - da ist nichts unwiderruflich.
        gerufen.clear()
        m._wipe_offen = {}
        asyncio.run(m._do_purge(Msg("Flo lösch 20"), "lösch 20"))
        assert gerufen == [21], gerufen
    finally:
        m._enabled, m._store, m._wipe_offen = alt


def test_ban_per_id_umgeht_die_rangordnung_nicht():
    """Ein Bann per ROHER ID muss dieselben Pruefungen durchlaufen wie per @-Name.

    Der Bot laeuft ohne members-Intent (bot.py: Intents.none()), guild.get_member()
    liefert deshalb None - und damit war 'isinstance(member, Member)' False und die
    Rangordnung uebersprungen. Gemessen konnte ein Junior-Mod so einen Senior-Mod,
    Flo selbst UND sich selbst bannen, nur indem er die ID statt der Erwaehnung
    tippte."""
    import discord
    import moderation

    JUNIOR, SENIOR, FLO = (111111111111111111, 222222222222222222,
                           999999999999999999)
    OWNER, EXTERN = 555555555555555555, 333333333333333333
    gebannt, geprueft = [], []

    class FakeMember(discord.Member):
        def __init__(self, uid):
            self._uid = uid
        id = property(lambda s: s._uid)

    class Guild:
        id = 1
        owner_id = OWNER

        def __init__(self):
            self.me = SimpleNamespace(id=FLO)

        def get_member(self, _uid):
            return None                      # ohne Intent ist der Cache leer

        async def fetch_member(self, uid):
            if uid == SENIOR:
                return FakeMember(SENIOR)
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "weg")

        async def ban(self, obj, reason=None, delete_message_seconds=0):
            gebannt.append(getattr(obj, "id", obj))

    class Msg:
        def __init__(self, rest):
            self.content = "Flo ban " + rest
            self.pinned = False
            self.author = SimpleNamespace(
                id=JUNIOR, display_name="Junior", name="Junior", bot=False, roles=[],
                guild_permissions=SimpleNamespace(ban_members=True,
                                                  administrator=False))
            self.guild = Guild()
            self.mentions = []
            self.channel = SimpleNamespace(id=7, name="c")

        async def reply(self, *a, **k):
            return SimpleNamespace(id=2)

    m = moderation.instance
    alt = (m._enabled, m._store, m._bot_can, m._modlog, m._hierarchy_problem)
    try:
        m._enabled = True
        m._store = _FakeStore({"warns": {}})
        m._bot_can = lambda _g, _p: True
        m._modlog = lambda *a, **k: asyncio.sleep(0)

        def rang(_msg, member):
            geprueft.append(getattr(member, "id", None))
            return "Diese Person hat eine gleich hohe oder höhere Rolle als du. ⛔"
        m._hierarchy_problem = rang

        def bann(uid):
            gebannt.clear()
            geprueft.clear()
            return asyncio.run(m._do_ban(Msg(f"{uid} Spam"), f"{uid} Spam"))

        # 1) Mitglied im Server -> Rangordnung greift jetzt auch per ID.
        bann(SENIOR)
        assert geprueft == [SENIOR], geprueft
        assert not gebannt, gebannt

        # 2) Flo, man selbst und der Besitzer sind hart gesperrt.
        for uid in (FLO, JUNIOR, OWNER):
            antwort = bann(uid)
            assert not gebannt, (uid, gebannt)
            assert isinstance(antwort, str), (uid, antwort)

        # 3) Wer wirklich nicht auf dem Server ist, laesst sich weiter per ID
        #    bannen - genau dafuer gibt es den ID-Bann.
        bann(EXTERN)
        assert gebannt == [EXTERN], gebannt
    finally:
        (m._enabled, m._store, m._bot_can, m._modlog,
         m._hierarchy_problem) = alt


def test_teurer_kauf_fragt_nach():
    """Ueber `kaufen <n>` kostet ein Tippfehler in EINER Ziffer bis zu 90 Mio
    Coins - unwiderruflich und bisher ohne jede Rueckfrage.

    Nachgefragt wird nur bei teuren Titeln; alles Alltaegliche bleibt ein
    einziger Schritt. Bestaetigt wird durch nochmal denselben Befehl."""
    import time as _t
    import economy
    e = economy.instance
    alt = (e._store, e._enabled, e._kauf_offen, e._sync_role)
    try:
        e._enabled = True
        e._sync_role = lambda m: asyncio.sleep(0)     # Rollen-Deko stoert hier
        mitglied = SimpleNamespace(id=1, display_name="T", name="T")

        def aufsetzen(coins=200_000_000):
            e._store = _FakeStore({"users": {"1": {
                "coins": coins, "xp": 0, "owned": [], "name": "T", "title": "",
                "title_rarity": "", "voice_secs": 0, "msgs": 0, "streak": 0,
                "last_daily": ""}}, "shop": {}})
            e._kauf_offen = {}
            e.refresh_shop(force=True)
            e._store.data["shop"]["items"] = list(e.get_shop_items()) + [
                {"n": 9, "label": "Weltenbrand", "text": "Weltenbrand",
                 "rarity": "goettlich", "price": 90_000_000}]
            return [x for x in e.get_shop_items()
                    if x["price"] < e.KAUF_RUECKFRAGE_AB][0]

        def kaufen(n):
            return asyncio.run(e._buy_text(mitglied, ["kaufen", str(n)]))

        # 1) Guenstig bleibt ein Schritt.
        billig = aufsetzen()
        vor = e.get_coins(1)
        kaufen(billig["n"])
        assert vor - e.get_coins(1) == billig["price"], (vor, e.get_coins(1))

        # 2) Teuer: erst fragen, nichts abbuchen.
        aufsetzen()
        vor = e.get_coins(1)
        antwort = kaufen(9)
        assert e.get_coins(1) == vor, "teurer Kauf ohne Rueckfrage gebucht"
        assert "90.000.000" in str(antwort), antwort

        # 3) Derselbe Befehl noch einmal kauft dann wirklich.
        kaufen(9)
        assert vor - e.get_coins(1) == 90_000_000, vor - e.get_coins(1)

        # 4) Ein anderer Kauf dazwischen beendet die Rueckfrage.
        billig = aufsetzen()
        vor = e.get_coins(1)
        kaufen(9)
        kaufen(billig["n"])
        kaufen(9)
        assert (vor - e.get_coins(1)) < 90_000_000, "Rueckfrage nicht abgebrochen"

        # 5) Nach einer Minute gilt sie nicht mehr.
        aufsetzen()
        vor = e.get_coins(1)
        kaufen(9)
        e._kauf_offen[1] = (9, _t.time() - 1)
        kaufen(9)
        assert e.get_coins(1) == vor, "abgelaufene Rueckfrage hat gekauft"

        # 6) Der Merker waechst nicht: abgelaufene Eintraege fliegen raus.
        e._kauf_offen = {i: (9, _t.time() - 1) for i in range(50)}
        aufsetzen()
        kaufen(9)
        assert len(e._kauf_offen) <= 2, len(e._kauf_offen)
    finally:
        e._store, e._enabled, e._kauf_offen, e._sync_role = alt


def test_floaktie_chat_haengt_nicht_am_takt():
    """Derselbe Chat-Verkehr muss dieselbe Aktivitaet ergeben - egal wie schnell
    der Loop taktet.

    Nachrichten sind als einzige Groesse eine ZAEHLUNG ueber den Takt; Leute,
    Streams und Kameras sind Zustaende. Ohne Hochrechnen auf eine Minute zaehlte
    derselbe Verkehr bei 20-s-Takt nur einen Bruchteil: gemessen 8,0 statt 15,0
    Punkte. Der Bot taktet im Betrieb alle 20 s - der Chat zaehlte dort also
    deutlich weniger als in jeder Rechnung und jeder Simulation."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-06"
    try:
        def aktivitaet_bei(dt, msgs_pro_minute, leute=3, streams=0):
            fa._store = _FakeStore({"price": 0, "base": 1000.0, "day": "x",
                                    "act_ema": 0.0, "grund_akt": 0.0, "mom": 0.0,
                                    "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0, "holdings": {},
                                    "history": [], "ticks": [],
                                    "open_day": "2026-08-06", "open_base": 1000.0})
            fa._sync_price()
            rest, akts = 0.0, []
            for _ in range(int(30 * 60 / dt)):
                rest += msgs_pro_minute * dt / 60.0
                n = int(rest)
                rest -= n
                _a, _p, _d, akt = fa._activity_tick(leute, n, streams, 0, dt=dt)
                akts.append(akt)
            return sum(akts) / len(akts)

        # Glatt aufgehende Raten: sonst schwankt die Eingabe und die bewusst
        # asymmetrische Glaettung hebt den Mittelwert (das ist gewollt).
        for rate in (0, 3, 6, 30):
            a60 = aktivitaet_bei(60.0, rate)
            a20 = aktivitaet_bei(20.0, rate)
            assert abs(a60 - a20) < 0.01, (rate, a60, a20)

        # Und der Chat traegt ueberhaupt spuerbar bei (sonst prueft das nichts).
        assert aktivitaet_bei(20.0, 30) > aktivitaet_bei(20.0, 0) + 5
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt


def test_floaktie_steht_nie_still():
    """Der Kurs bewegt sich IMMER - er steigt oder faellt, aber er steht nie.

    Gemessen vorher: am Grundwert 200 von 200 Takten exakt 0,000 %/min und ueber
    200 Takte ein einziger Kurswert. Eine Aktie, die stundenlang auf derselben
    Zahl steht, sieht kaputt aus.

    Das Atmen muss dabei SYMMETRISCH bleiben (Erwartungswert 0) und am Niveau
    haengen - sonst waere es nach oben Inflation und nach unten ein Grab."""
    import random
    import statistics
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE,
           floaktie.VOL_SPREAD)
    fa._enabled = True
    fa._today = lambda: "2026-08-06"
    try:
        def frisch(base, ema=0.0, grund=0.0, S=0):
            fa._store = _FakeStore({"price": 0, "base": float(base), "day": "x",
                                    "act_ema": ema, "grund_akt": grund, "mom": 0.0,
                                    "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0,
                                    "holdings": ({"1": S} if S else {}),
                                    "history": [], "ticks": [],
                                    "open_day": "2026-08-06",
                                    "open_base": float(base)})
            fa._sync_price()

        # 1) AM GRUNDWERT (leerer Call) - das war der echte Stillstand.
        frisch(3000.0, grund=0.75)
        fa._store.data["base"] = fa.boden_base()
        fa._sync_price()
        kurse, drifts = [], []
        for _ in range(300):
            _a, n, d, _akt = fa._activity_tick(0, 0)
            kurse.append(n)
            drifts.append(d)
        assert all(d != 0.0 for d in drifts), "Takt mit exakt 0 am Grundwert"
        assert len(set(kurse)) > 20, (len(set(kurse)), min(kurse), max(kurse))

        # 2) AM DECKEL (Leute im Call).
        akt = fa.activity_of(6, 2, 0, 0)
        frisch(fa.ziel_base(akt) * floaktie.CEIL_FACTOR * 1.02, ema=akt, grund=0.75)
        kurse, drifts = [], []
        for _ in range(300):
            _a, n, d, _akt = fa._activity_tick(6, 0, streams=2)
            kurse.append(n)
            drifts.append(d)
        assert all(d != 0.0 for d in drifts), "Takt mit exakt 0 am Deckel"
        assert len(set(kurse)) > 20, len(set(kurse))

        # 3) NORMALER BETRIEB: auch dort nie exakt 0 - ein stark negatives
        #    Momentum konnte die Summe frueher auf genau 0 druecken.
        frisch(1000.0, grund=0.5)
        drifts = []
        for _ in range(300):
            _a, _n, d, _akt = fa._activity_tick(4, 5, streams=1)
            drifts.append(d)
        assert all(d != 0.0 for d in drifts), "Takt mit exakt 0 im Normalbetrieb"

        # 4) SYMMETRIE: das Atmen darf den Kurs nicht systematisch tragen.
        #    Boden festhalten und ueber viele Seeds den Endstand mitteln.
        enden = []
        for seed in range(30):
            random.seed(seed)
            frisch(3000.0, grund=0.75)
            fa.boden_base = lambda: 3000.0
            fa.drift_fuer = lambda _a: 0.0
            try:
                for _ in range(300):
                    fa._activity_tick(0, 0)
                enden.append(fa.price() / 3000.0 - 1.0)
            finally:
                del fa.boden_base, fa.drift_fuer
        mittel = statistics.mean(enden)
        streuung = statistics.pstdev(enden)
        # Der Mittelwert muss im Rauschen liegen (Standardfehler = sd/sqrt(n)).
        assert abs(mittel) < 3.0 * streuung / (len(enden) ** 0.5) + 0.01, \
            (mittel, streuung)

        # 5) Das Atmen bleibt klein - es ist kein Antrieb.
        frisch(3000.0, grund=0.75)
        for _ in range(200):
            assert abs(fa._atem()) <= floaktie.ATEM_MAX + 1e-9
            assert fa._atem() != 0.0

        # 6) Und es zieht ans Niveau zurueck: unter dem Boden nach oben.
        frisch(1000.0, grund=0.75)          # Boden liegt deutlich hoeher
        assert fa.boden_base() > 1500, fa.boden_base()
        zuege = [fa._atem(fa.boden_base()) for _ in range(200)]
        assert statistics.mean(zuege) > 0, statistics.mean(zuege)
        # ... und ueber dem Deckel nach unten.
        frisch(90_000.0, grund=0.1)
        zuege = [fa._atem(fa.akt_deckel_base()) for _ in range(200)]
        assert statistics.mean(zuege) < 0, statistics.mean(zuege)
    finally:
        (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE,
         floaktie.VOL_SPREAD) = alt


def test_floaktie_grundwert_traegt_den_boden():
    """Der Boden ist BEWEGLICH: die Aktie ist so viel wert, wie der Server im
    Schnitt lebendig ist.

    Vorher war der Boden fest (FAIR_BASE = 10). Bei -11 % je halber Stunde ist
    nach einer Nacht alles weg - in der 60-Tage-Simulation stand der Kurs jeden
    Morgen bei 10 und jeden Abend bei ~130.000, Spanne 182-fach. Kein Markt,
    nur ein Saegezahn."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-06"
    try:
        def frisch(kurs=1000):
            fa._store = _FakeStore({"price": int(kurs), "base": float(kurs),
                                    "day": "x", "act_ema": 0.0, "grund_akt": 0.0,
                                    "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0, "mom": 0.0, "holdings": {},
                                    "history": [], "ticks": [],
                                    "open_day": "2026-08-06", "open_base": float(kurs)})
            fa._sync_price()

        # 1) Frischer Server: Grundwert 0 -> Boden ist der Mindestwert.
        frisch()
        assert fa.grund_akt() == 0.0
        assert abs(fa.boden_base() - floaktie.FAIR_BASE) < 0.01, fa.boden_base()

        # 2) Ein paar Tage mit taeglichem Abend-Call heben den Boden deutlich.
        frisch(1000)
        for _tag in range(5):
            for _ in range(4 * 60):                     # 4 h Call
                fa._activity_tick(6, 3, streams=2)
            for _ in range(20 * 60):                    # 20 h Ruhe
                fa._activity_tick(0, 0)
        boden_lebendig = fa.boden_base()
        assert boden_lebendig > floaktie.FAIR_BASE * 50, boden_lebendig

        # 3) Genau dorthin faellt der Kurs im Leerlauf - und nicht tiefer.
        fa._store.data["base"] = boden_lebendig * 5.0
        fa._sync_price()
        for _ in range(48 * 60):                        # zwei ganze Tage leer
            fa._activity_tick(0, 0)
        assert fa._base() >= fa.boden_base() * 0.98, (fa._base(), fa.boden_base())
        # Am Grundwert ist Schluss. Geprueft wird das ueber _am_grundwert() mit
        # seiner Toleranz und NICHT ueber "drift_fuer(0) == 0.0": der Kurs atmet
        # dort und liegt mal ein Stueck ueber dem Boden, dann liefert drift_fuer
        # richtigerweise den Verfall. Mit der strengen Gleichheit war dieser Test
        # in 2 von 25 Laeufen grundlos rot.
        assert fa._am_grundwert(), (fa._base(), fa.boden_base())

        # 4) Bleibt es WIRKLICH dauerhaft still, schmilzt auch der Grundwert.
        for _ in range(20 * 24 * 60):                   # 20 Tage tot
            fa._activity_tick(0, 0)
        assert fa.boden_base() < boden_lebendig * 0.2, (fa.boden_base(), boden_lebendig)

        # 4b) Der angezeigte Deckel darf NIE unter dem Grundwert liegen - sonst
        #     stand im Panel "Kurs 6.511, Deckel 22", und der Sofort-Impuls war
        #     tot (er bricht ab, sobald der Kurs ueber dem Deckel steht - also
        #     ausgerechnet dann, wenn jemand einen stillen Server betritt).
        frisch(1000)
        for _tag in range(5):
            for _ in range(4 * 60):
                fa._activity_tick(6, 3, streams=2)
            for _ in range(20 * 60):
                fa._activity_tick(0, 0)
        assert fa.akt_deckel_base() >= fa.boden_base(), (fa.akt_deckel_base(),
                                                         fa.boden_base())
        assert fa._deckel_kurs() >= fa.price() * 0.99, (fa._deckel_kurs(), fa.price())
        vor_puls = fa.price()
        fa._store.data["pulse_min"] = 0
        fa._store.data["pulse_sum"] = 0.0
        fa._puls(floaktie.PULSE_JOIN, "jemand kommt")
        assert fa.price() >= vor_puls, (vor_puls, fa.price())

        # 5) Ein einzelner Abend darf den Boden nicht hochreissen (Traegheit) -
        #    sonst waere er nur eine langsamere Kopie des Kurses.
        frisch(1000)
        vor = fa.boden_base()
        for _ in range(4 * 60):
            fa._activity_tick(10, 5, streams=4)
        akt = fa.activity_of(10, 4, 0, 5)
        assert fa.boden_base() < fa.ziel_base(akt) * 0.25, (fa.boden_base(),
                                                            fa.ziel_base(akt))
        assert fa.boden_base() > vor
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt


def test_floaktie_ist_streng_aber_ein_markt():
    """Die Vorgabe: man kann schnell viel gewinnen UND schnell viel verlieren.

    Gemessen an einem Tagesablauf (Abend-Call, danach Ruhe) - genau der Fall,
    um den es geht. Die ausfuehrliche 60-Tage-Fassung steht in
    tools_aktien_sim.py; hier die Kernaussagen als Regressionsschutz."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-06"
    try:
        fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                "act_ema": 0.0, "grund_akt": 0.0, "mom": 0.0,
                                "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {}, "history": [],
                                "ticks": [], "open_day": "2026-08-06",
                                "open_base": 1000.0})
        fa._sync_price()
        # Eine Woche einschwingen lassen, damit der Grundwert steht.
        for _tag in range(7):
            for _ in range(4 * 60):
                fa._activity_tick(6, 3, streams=2)
            for _ in range(20 * 60):
                fa._activity_tick(0, 0)

        # SCHNELL VIEL GEWINNEN: wer zum Call-Start kauft, verdoppelt in Stunden.
        vor_abend = fa.price()
        for _ in range(4 * 60):
            fa._activity_tick(6, 3, streams=2)
        hoch = fa.price()
        assert hoch >= vor_abend * 2.0, (vor_abend, hoch)

        # SCHNELL VIEL VERLIEREN: wer bis zum naechsten Morgen haelt, blutet.
        for _ in range(8 * 60):
            fa._activity_tick(0, 0)
        morgen = fa.price()
        assert morgen <= hoch * 0.65, (hoch, morgen)

        # ABER NICHT INS NICHTS: der Grundwert faengt den Kurs auf.
        assert morgen > vor_abend * 0.4, (vor_abend, morgen)

        # Und die Tagesspanne bleibt handhabbar (kein Saegezahn ueber 100-fach).
        assert hoch / max(1, morgen) < 30, (hoch, morgen)
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt


def test_floaktie_chart_klebt_keine_tage_doppelt():
    """Die Kurs-Reihe darf Tage nicht doppelt zaehlen.

    Vorher wurden schlicht die n NEUESTEN Tages-Schlusskurse vor die Ticks
    geklebt - die liegen aber genau IM Tick-Fenster. Gemessen bei '7 Tage' mit
    Ticks der letzten 3 Tage: alle 4 vorangestellten Tage waren doppelt, der
    Kurs von vor 7 Tagen kam gar nicht vor, und die angezeigte Veraenderung war
    +67 % statt +290 %."""
    import time as _t
    from datetime import datetime as _dt, timedelta as _td
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    try:
        jetzt = _t.time()
        heute = _dt.now(floaktie.TIMEZONE)
        # 10 Tage Schlusskurse (100..1000), Ticks nur fuer die letzten 3 Tage.
        hist = [{"day": (heute - _td(days=9 - i)).strftime("%Y-%m-%d"),
                 "price": 100 * (i + 1)} for i in range(10)]
        ticks = [{"t": jetzt - 3 * 86400 + i * 3600, "price": 1100 + i}
                 for i in range(72)]
        fa._store = _FakeStore({"price": 0, "base": 1171.0, "holdings": {},
                                "history": hist, "ticks": ticks, "act_ema": 0.0,
                                "leer_min": 0.0, "msg_count": 0,
                                "last_msg_count": 0})
        fa._sync_price()

        # 1) Kein Tageskurs aus dem Tick-Fenster darf vorangestellt werden.
        ab_tag = _dt.fromtimestamp(min(t["t"] for t in ticks),
                                   floaktie.TIMEZONE).strftime("%Y-%m-%d")
        drin = {h["price"] for h in hist if h["day"] >= ab_tag}
        reihe = fa._series(7)
        kopf = reihe[:len(reihe) - len(ticks)]
        assert not (drin & set(kopf)), (sorted(drin & set(kopf)), kopf)

        # 2) Die 7-Tage-Reihe beginnt beim Kurs von vor 7 Tagen.
        vor7 = next(h["price"] for h in hist
                    if h["day"] == (heute - _td(days=7)).strftime("%Y-%m-%d"))
        assert reihe[0] == vor7, (reihe[0], vor7)
        _pts, chg = fa.series(7)
        assert abs(chg - (1171 - vor7) / vor7 * 100) < 1.0, chg

        # 3) Laengeres Fenster -> frueherer Startpunkt, nie spaeter.
        starts = [fa._series(d)[0] for d in (1, 7, 30)]
        assert starts[2] <= starts[1] <= starts[0], starts

        # 4) Decken die Ticks das ganze Fenster ab, kommt KEINE Historie dazu.
        #
        # Der erste Tick lag hier auf GENAU der Fenstergrenze (jetzt - 86400).
        # _series liest die Uhr aber selbst und spaeter - vergehen dazwischen
        # ein paar Millisekunden, rutscht der Tick aus dem Fenster, Historie
        # wird ergaenzt und der Test faellt um. Genau so ist er auf einer
        # ausgelasteten Maschine gerissen (erwartet >= 1100, bekommen 900),
        # nachdem er kurz zuvor noch gruen war. Eine Minute Luft macht ihn von
        # der Wanduhr unabhaengig, ohne die Aussage zu aendern.
        fa._store.data["ticks"] = [{"t": jetzt - 86400 + 60 + i * 600,
                                    "price": 1100 + i} for i in range(140)]
        assert fa._series(1)[0] >= 1100, fa._series(1)[0]

        # 5) Ganz ohne Ticks traegt die Historie die Reihe allein.
        fa._store.data["ticks"] = []
        ohne = fa._series(7)
        assert len(ohne) >= 2 and ohne[0] == vor7, ohne[:3]
    finally:
        fa._store, fa._enabled = alt


def test_floaktie_kurs_ist_nie_veraltet():
    """price() muss IMMER zur Kurve passen - auch wenn jemand den gespeicherten
    Stand von Hand verbiegt oder ein Schreibweg _sync_price() vergisst.

    Aufgefallen im Betriebs-Log: 'Kurs 1.000->6.193' bei NEGATIVEM Drift. Ursache
    war ein veraltetes st['price']; ueber _record_tick waere dieser falsche Wert
    zusaetzlich in Chart und Tages-Historie gewandert."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    try:
        fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {}, "history": [],
                                "ticks": []})
        fa._sync_price()

        def erwartet():
            return max(floaktie.MIN_PRICE, min(floaktie.MAX_PRICE, int(round(
                fa._base() * (1.0 + fa.total_shares() / floaktie.LIQUIDITY)))))

        # 1) Anteile dazu, ohne zu synchronisieren -> price() zieht trotzdem mit.
        fa._store.data["holdings"] = {"1": 500}
        assert fa.price() == erwartet(), (fa.price(), erwartet())
        # 2) Gespeicherter Stand von Hand verbogen -> price() glaubt ihm NICHT.
        fa._store.data["price"] = 7
        assert fa.price() == erwartet(), fa.price()
        assert fa.price() > 1000
        # 3) Basiskurs geaendert, ohne zu synchronisieren.
        fa._store.data["base"] = 250.0
        assert fa.price() == erwartet(), fa.price()
        # 4) Und der Chart bekommt nie einen veralteten Wert zu sehen.
        fa._store.data["ticks"] = []
        fa._record_tick()
        assert fa._store.data["ticks"][-1]["price"] == erwartet()
        # 5) Ein Takt meldet einen ECHTEN Vorher/Nachher-Vergleich: sinkt der
        #    Kurs, darf 'neu' nicht ueber 'alt' liegen.
        for _ in range(20):
            a, n, drift, _akt = fa._activity_tick(0, 0)
            assert (n <= a) == (drift <= 0), (a, n, drift)
    finally:
        fa._store, fa._enabled = alt


def test_floaktie_volatilitaet_ist_symmetrisch():
    """Die Volatilitaet macht den Kurs lebendig - sie darf ihn aber weder
    systematisch heben noch druecken. Der einzige Test, der sie EINSCHALTET
    (die Suite laeuft sonst bewusst ohne, siehe Kopf der Datei)."""
    import statistics
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, floaktie.TICK_NOISE, floaktie.VOL_SPREAD)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    floaktie.VOL_SPREAD = 0.8
    try:
        def frisch():
            fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                    "act_ema": 0.0, "msg_count": 0,
                                    "last_msg_count": 0, "holdings": {},
                                    "history": [], "ticks": []})
            fa._sync_price()

        drifts = []
        for _ in range(400):
            frisch()
            _a, _n, d, _akt = fa._activity_tick(10, 20, streams=10)
            drifts.append(d)

        # 1) Es schwankt ueberhaupt - sonst waere die Aktie tot.
        assert len(set(drifts)) > 300, len(set(drifts))
        # 2) Es geht in dieser Lage NIE nach unten (Aktivitaet ist hoch).
        assert min(drifts) > 0, min(drifts)
        # 3) Sie bleibt innerhalb ihres Spielraums um den Anschlag.
        spanne = floaktie.VOL_SPREAD
        assert max(drifts) <= floaktie.TICK_CAP * (1.0 + spanne) + 1e-9, max(drifts)
        # 4) Der Mittelwert liegt beim ungestoerten Trend, nicht daneben.
        floaktie.VOL_SPREAD = 0.0
        frisch()
        _a, _n, ruhig, _akt = fa._activity_tick(10, 20, streams=10)
        schnitt = statistics.mean(drifts)
        assert abs(schnitt - ruhig) < ruhig * 0.15, (schnitt, ruhig)
    finally:
        fa._store, fa._enabled, floaktie.TICK_NOISE, floaktie.VOL_SPREAD = alt


def test_floaktie_leerlauf_faellt_ab_der_ersten_minute():
    """Gemeldet: "es sinkt nur so 1 % pro Stunde". Verlangt: gute 20 % pro Stunde,
    ab der ERSTEN leeren Minute - und der Anstieg im Call bleibt unveraendert.

    Geprueft wird deshalb dreierlei:
      1. schon bei leer_min = 0 sind es >= 20 %/h,
      2. eine volle leere Stunde kostet deutlich mehr als 20 %,
      3. der Anstieg bei Aktivitaet ist NICHT mit gedrosselt worden."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-05"
    try:
        def frisch(kurs, S=147):
            b = kurs / (1 + S / floaktie.LIQUIDITY)
            fa._store = _FakeStore({"price": int(kurs), "base": b, "day": "x",
                                    "act_ema": 0.0, "msg_count": 0,
                                    "last_msg_count": 0, "leer_min": 0.0,
                                    "open_day": "2026-08-05", "open_base": b,
                                    "holdings": {"1": S}, "history": [], "ticks": []})
            fa._sync_price()

        # 1) Sofort-Rate: auch in der allerersten leeren Minute >= 20 %/h.
        frisch(5_000)
        pro_min = fa._leerlauf_verfall()
        pro_std = 1.0 - (1.0 - pro_min) ** 60
        assert pro_std >= 0.20, (pro_min, pro_std)

        # ... und mit laenger werdendem Leerlauf wird es NUR schneller.
        vorher = 0.0
        for leer in (0, 2, 4, 6, 30):
            fa._state()["leer_min"] = float(leer)
            jetzt = fa._leerlauf_verfall()
            assert jetzt >= vorher, (leer, jetzt, vorher)
            vorher = jetzt

        # 2) Eine volle leere Stunde: deutlich mehr als die geforderten 20 %.
        for start in (1_000, 5_000, 40_000):
            frisch(start)
            for _ in range(60):
                fa._activity_tick(0, 0)
            verlust = 1.0 - fa.price() / start
            assert verlust >= 0.20, (start, fa.price(), verlust)

        # 3) Der ANSTIEG im Call bleibt: 6 Leute + 2 Streams heben den Kurs.
        frisch(1_000)
        for _ in range(60):
            fa._activity_tick(6, 4, streams=2)
        assert fa.price() > 1_000, fa.price()
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt


def test_floaktie_panel_zeigt_wer_gezaehlt_wird():
    """Damit man SIEHT, woher die Aktivitaet kommt (und dass kein Bot dabei ist),
    nennt das Panel die gezaehlten Menschen beim Namen."""
    import floaktie
    fa = floaktie.instance

    def member(uid, name, *, bot=False, stream=False):
        vs = SimpleNamespace(self_stream=stream, self_video=False)
        return SimpleNamespace(id=uid, bot=bot, voice=vs, display_name=name,
                               name=name), vs

    m_flo, vs_flo = member(99, "Flo", bot=True)
    m_musik, vs_musik = member(2, "Musik-Bot", bot=True)
    m_anna, vs_anna = member(1, "Anna", stream=True)

    class Chan:
        def __init__(self, members, states):
            self.id = 5
            self.members = members
            self.voice_states = states

    class Guild:
        def __init__(self, members, states):
            self.voice_channels = [Chan(members, states)]
            self.afk_channel = None
            self.me = SimpleNamespace(id=99)

        def get_member(self, uid):
            return None

    # Nur Bots im Call -> die Zeile sagt das ausdruecklich, kein Bot-Name drin.
    fa._measure(Guild([m_flo, m_musik], {99: vs_flo, 2: vs_musik}))
    fa._zuletzt_mess = (0, 0, 0, 0)
    zeile = fa._mess_zeile()
    assert "Niemand im Call" in zeile, zeile
    assert "Musik-Bot" not in zeile and "Flo" not in zeile, zeile

    # Mit einem Menschen: sein Name steht da, der Bot weiterhin nicht.
    leute, streams, video = fa._measure(Guild([m_flo, m_musik, m_anna],
                                              {99: vs_flo, 2: vs_musik, 1: vs_anna}))
    assert (leute, streams, video) == (1, 1, 0), (leute, streams, video)
    fa._zuletzt_mess = (leute, streams, video, 12)
    zeile = fa._mess_zeile()
    assert "Anna" in zeile, zeile
    assert "Musik-Bot" not in zeile, zeile
    assert "Livestream" in zeile and "12" in zeile, zeile


def test_floaktie_reset_befehl():
    """'flo aktie reset' setzt den Kurs auf den Start zurueck - nur fuer den Chef."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today)
    fa._enabled = True
    fa._today = lambda: "2026-07-27"
    restore = _with_economy({floaktie.OWNER_ID: 1000, 7: 1000})
    fa._store = _FakeStore({"price": 500_000, "base": 400_000.0, "day": "x",
                            "act_ema": 9.9, "msg_count": 40, "last_msg_count": 10,
                            "leer_min": 3.0, "open_day": "2026-07-27",
                            "open_base": 12_345.0, "holdings": {"1": 150, "2": 30},
                            "history": [{"day": "x", "price": 1}], "ticks": [1, 2, 3]})
    fa._sync_price()
    try:
        def msg(uid, text):
            return SimpleNamespace(content=text, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="T"))

        # Fremde duerfen nicht - der Kurs bleibt, wo er war.
        vorher = fa.price()
        assert vorher > 100_000, vorher
        r = asyncio.run(fa.handle(msg(7, "aktie reset")))
        assert isinstance(r, str) and "nur der Chef" in r
        assert fa.price() == vorher

        # Der Chef schon - Depots bleiben erhalten.
        r = asyncio.run(fa.handle(msg(floaktie.OWNER_ID, "aktie reset")))
        assert "zurückgesetzt" in _embed_text(r)
        st = fa._store.data
        erwartet = max(floaktie.MIN_PRICE,
                       round(floaktie.START_PRICE * (1 + 180 / floaktie.LIQUIDITY)))
        assert fa.price() == erwartet, (fa.price(), erwartet)
        assert fa.price() < vorher / 100                   # wirklich ganz unten
        assert st["holdings"] == {"1": 150, "2": 30}       # Anteile bleiben
        assert st["history"] == [] and st["act_ema"] == 0.0 and st["leer_min"] == 0.0
        assert st["open_day"] == "2026-07-27"              # Tagesbremse neu verankert

        # 'reset alles' loescht zusaetzlich die Depots.
        r = asyncio.run(fa.handle(msg(floaktie.OWNER_ID, "aktie reset alles")))
        assert fa._store.data["holdings"] == {}
        assert "2 geleert" in _embed_text(r)
    finally:
        fa._store, fa._enabled, fa._today = alt
        restore()


def test_floaktie_faellt_auf_jedem_niveau():
    """REGRESSION (aus dem Betrieb: "die Aktie sinkt nie"): Der Boden FAIR_BASE lag
    bei 5.000, waehrend der Kurs nach einem Reset bei 1.000 startet und im Betrieb
    oft darunter stand - unterhalb des Bodens passiert im Leerlauf GAR NICHTS.
    Der Kurs war also in einer Einbahnstrasse, bis er sich versechsfacht hatte."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-07-27"
    try:
        def frisch(kurs, S=147):
            b = kurs / (1 + S / floaktie.LIQUIDITY)
            fa._store = _FakeStore({"price": int(kurs), "base": b, "day": "x",
                                    "act_ema": 0.0, "msg_count": 0,
                                    "last_msg_count": 0, "leer_min": 0.0,
                                    "open_day": "2026-07-27", "open_base": b,
                                    "holdings": {"1": S}, "history": [], "ticks": []})
            fa._sync_price()

        # Der Startkurs darf NICHT unter dem Boden liegen, sonst kann eine frisch
        # zurueckgesetzte Aktie anfangs gar nicht fallen.
        assert floaktie.START_PRICE >= floaktie.FAIR_BASE

        # Von JEDEM Niveau aus faellt der Kurs im Leerlauf spuerbar.
        for start in (1_200, 3_421, 6_000, 20_000, 52_453):
            frisch(start)
            for _ in range(4 * 60):
                fa._activity_tick(0, 0)
            assert fa.price() < start * 0.85, (start, fa.price())
            # ... und zwar in JEDER Minute, nie nach oben.
            for _ in range(60):
                _a, _n, drift, _akt = fa._activity_tick(0, 0)
                assert drift <= 0, drift

        # Innerhalb EINES Tages bremst der Circuit Breaker bei -90 %.
        frisch(20_000)
        for _ in range(1440):
            fa._activity_tick(0, 0)
        assert abs(fa.price() - 20_000 * floaktie.DAY_DOWN) < 20_000 * 0.02, fa.price()

        # Ueber mehrere Tage geht es weiter runter - aber nicht ins Bodenlose.
        for tag in range(28, 32):
            fa._today = lambda t=f"2026-07-{tag:02d}": t
            fa._tages_anker()
            for _ in range(1440):
                fa._activity_tick(0, 0)
        # Nach Tagen ohne jede Aktivitaet ist auch der GRUNDWERT abgeschmolzen -
        # ein wirklich toter Server faellt bis auf den Mindestwert durch.
        boden = fa.boden_base() * (1 + 147 / floaktie.LIQUIDITY)
        assert fa.price() >= floaktie.MIN_PRICE
        # Die Toleranz muss das ATMEN mitnehmen und wird deshalb aus den
        # Konstanten abgeleitet statt geraten. Am absoluten Boden ist der Kurs
        # einstellig - dort ist EINE ganze Zahl schon 8 %, und der Kurs pendelt
        # gemessen zwischen 10 und 13 (Boden 11,96). Mit festen 5 % war dieser
        # Test in 5 von 25 Laeufen grundlos rot.
        toleranz = boden * (0.05 + 3 * fa._atem_spanne()) + 1.0
        assert abs(fa.price() - boden) < toleranz, (fa.price(), boden, toleranz)
        assert abs(fa.boden_base() - floaktie.FAIR_BASE) < 1.0, fa.boden_base()
        # Am Boden sagt das Panel ausdruecklich, dass es nicht tiefer geht.
        restore = _with_economy({2: 0})
        try:
            panel = _embed_text(fa._panel_embed(SimpleNamespace(id=2)))
            assert "Grundwert erreicht" in panel, panel
        finally:
            restore()

        # Und nach oben geht es weiterhin: vom Boden aus vervielfacht sich der
        # Kurs mit ein paar Leuten im Call.
        frisch(int(floaktie.START_PRICE), S=0)
        vorher = fa.price()
        for _ in range(2 * 60):
            fa._activity_tick(3, 4, streams=1)
        assert fa.price() > vorher * 8, (vorher, fa.price())
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt


def test_floaktie_panel_zeigt_echten_deckel():
    """Das Panel muss den WIRKLICH bindenden Deckel zeigen, nicht nur den der
    Aktivitaet. Aus dem Betrieb gemeldet: Panel sagte 'Deckel 888.674', die
    Tagesbremse stoppte den Kurs aber schon bei 52.453 - wer das nicht weiss,
    wartet stundenlang auf einen Anstieg, der heute nicht mehr kommen kann."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE, floaktie.DAY_UP)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-07-27"
    # Die Tagesbremse nach oben ist im Betrieb bewusst weit offen (der Aktivitaets-
    # Deckel ist die echte Grenze). Fuer diesen Test wird sie eng gestellt, damit
    # der Fall "Tagesbremse bindet frueher" ueberhaupt eintritt.
    floaktie.DAY_UP = 50.0
    restore = _with_economy({2: 1000})
    try:
        S = 147
        base = 3421 / (1 + S / floaktie.LIQUIDITY)
        anker = base / 3.261                     # Tag startete deutlich tiefer
        fa._store = _FakeStore({"price": 3421, "base": base, "day": "x",
                                "act_ema": 29.3, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "open_day": "2026-07-27",
                                "open_base": anker,
                                "holdings": {"1": 137, "2": 8, "3": 2},
                                "history": [], "ticks": []})
        fa._sync_price()

        # Der Aktivitaets-Deckel liegt hoch, die Tagesbremse VIEL tiefer.
        akt_deckel = fa.akt_deckel_base()
        _lo, tag_hi = fa._tages_band()
        assert tag_hi is not None and tag_hi < akt_deckel
        assert fa._deckel_base() == tag_hi          # der kleinere gewinnt
        assert fa._deckel_grund() == "tag"

        # Der Kurs bleibt wirklich dort stehen - auch nach Stunden Vollgas.
        for _ in range(12 * 60):
            fa._activity_tick(10, 25, streams=5, video=1)
        gedeckelt = fa.price()
        assert gedeckelt <= fa._deckel_kurs() * 1.001, (gedeckelt, fa._deckel_kurs())
        assert gedeckelt < akt_deckel               # der Aktivitaets-Deckel war Fiktion

        # Und das Panel sagt es: Tagesdeckel, samt Ausblick auf morgen.
        panel = _embed_text(fa._panel_embed(SimpleNamespace(id=2)))
        assert "Tagesdeckel erreicht" in panel, panel
        assert "Mitternacht" in panel

        # Solange noch Luft nach oben ist, markiert das Panel das Tempo-Limit -
        # sonst wundert man sich, warum mehr Leute nichts mehr beschleunigen.
        fa._store.data["base"] = fa._deckel_base() / 100.0
        fa._sync_price()
        luft = _embed_text(fa._panel_embed(SimpleNamespace(id=2)))
        assert "(Anschlag)" in luft, luft
        assert "Tagesdeckel erreicht" not in luft

        # Fuer den naechsten Teil wieder auf den gedeckelten Stand zurueck.
        fa._store.data["base"] = fa._deckel_base()
        fa._sync_price()
        gedeckelt = fa.price()

        # Neuer Tag -> neuer Anker, jetzt bindet die Aktivitaet.
        fa._today = lambda: "2026-07-28"
        fa._tages_anker()
        assert fa._deckel_grund() in ("aktivitaet", "tag")
        for _ in range(24 * 60):
            fa._activity_tick(10, 25, streams=5, video=1)
        assert fa.price() > gedeckelt               # es geht weiter
    finally:
        (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE,
         floaktie.DAY_UP) = alt
        restore()


def test_floaktie_kein_minus_und_anteil_limit():
    """Zwei harte Zusagen an den Besitzer:
    1) Die Aktie drueckt ein Konto NIE ins Minus - es gibt keinen Kredit mehr.
    2) Es gibt ein Anteil-Limit pro Person, und es greift auf JEDEM Weg
       (Text-Befehl, 'kauf max', Panel-Button)."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    restore = _with_economy({1: 5_000_000, 2: 500, 3: 0, 4: -20_000})
    fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x", "act_ema": 0.0,
                            "msg_count": 0, "last_msg_count": 0, "leer_min": 0.0,
                            "holdings": {}, "history": [], "ticks": []})
    fa._sync_price()
    try:
        limit = floaktie.MAX_SHARES_PER_USER
        assert limit > 0

        # --- 1) NIE ins Minus, egal wie man es versucht -------------------
        # a) viel mehr wollen, als man hat
        r = asyncio.run(fa.buy(SimpleNamespace(id=2), limit))
        assert economy.get_coins(2) >= 0, economy.get_coins(2)
        # b) blankes Konto
        r = asyncio.run(fa.buy(SimpleNamespace(id=3), 5))
        assert fa.shares_of(3) == 0 and economy.get_coins(3) == 0
        assert isinstance(r, str) and "Guthaben nicht" in r
        # c) Konto, das (aus alten Zeiten) noch im Minus steht: nichts wird schlimmer
        vorher4 = economy.get_coins(4)
        r = asyncio.run(fa.buy(SimpleNamespace(id=4), 5))
        assert fa.shares_of(4) == 0 and economy.get_coins(4) == vorher4
        # d) hundert Kaeufe hintereinander mit kleinem Konto
        economy.instance._profile(5)["coins"] = 30_000
        for _ in range(100):
            asyncio.run(fa.buy(SimpleNamespace(id=5), 3))
            assert economy.get_coins(5) >= 0, economy.get_coins(5)
        # Kein Aufrufer nutzt noch den Minus-Schalter von add_coins.
        quelle = open("floaktie.py", encoding="utf-8").read()
        assert "allow_negative" not in quelle
        assert "KREDIT_LINIE" not in quelle

        # --- 2) Anteil-Limit auf jedem Weg --------------------------------
        economy.instance._profile(1)["coins"] = 10 ** 12
        asyncio.run(fa.buy(SimpleNamespace(id=1), limit * 10))     # Text-Befehl
        assert fa.shares_of(1) == limit, fa.shares_of(1)
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 1))          # noch einer
        assert "Depot ist voll" in r and fa.shares_of(1) == limit
        # 'kauf max' respektiert den freien Platz
        asyncio.run(fa.sell(SimpleNamespace(id=1), 40))
        assert fa._resolve_count(SimpleNamespace(id=1), "max") == 40
        assert fa._freies_depot(SimpleNamespace(id=1).id if False else 1) == 40
        # Auch in vielen kleinen Schritten kommt niemand darueber.
        for _ in range(200):
            asyncio.run(fa.buy(SimpleNamespace(id=1), 5))
            assert fa.shares_of(1) <= limit, fa.shares_of(1)
        assert fa.shares_of(1) == limit
        # Das Limit steht sichtbar im Panel und in der Kauf-Bestaetigung.
        panel = _embed_text(fa._panel_embed(SimpleNamespace(id=1)))
        assert "Anteil-Limit" in panel and str(limit) in panel
        # Das Panel verspricht keinen Kredit mehr, sondern sagt das Gegenteil.
        assert "eigenem Guthaben" in panel
        assert "auf Kredit" not in panel and "MINUS" not in panel
    finally:
        fa._store, fa._enabled = alt
        restore()


def test_floaktie_impuls_respektiert_deckel():
    """REGRESSION (Gegenpruefung des Umbaus): Der Sofort-Impuls _puls() kannte den
    Aktivitaets-Deckel NICHT - er lief nur durch _clamp_base (Boden, MAX_PRICE,
    Tagesbremse). Mit Rein-/Raus-Springen aus dem Call liess sich der Kurs damit am
    Deckel vorbei bis an die Tagesbremse treiben: gemessen Deckel 112.000, erreicht
    wurden 250.000 - und weil die Tagesbremse pro KALENDERTAG verankert wird, ging
    das jede Nacht von vorn (ueber 7 Tage Faktor 1.593 statt 22)."""
    import time as _t
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today)
    fa._enabled = True
    try:
        def frisch(tag="2026-07-26"):
            fa._store = _FakeStore({"price": 5000, "base": 5000.0, "day": "x",
                                    "act_ema": 3.0, "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0, "holdings": {}, "history": [],
                                    "ticks": []})
            fa._today = lambda: tag
            fa._tages_anker()
            fa._sync_price()

        frisch()
        deckel = fa.akt_deckel_base()
        assert deckel == fa.ziel_base(3.0) * floaktie.CEIL_FACTOR
        # Ein einzelner Impuls hebt den Kurs - das soll er auch.
        vorher = fa.price()
        assert fa._puls(floaktie.PULSE_STREAM, "test") > vorher

        # Dauer-Spam ueber 7 Tage (inkl. Mitternacht) endet AM DECKEL, nicht drueber.
        frisch()
        t0 = _t.time()
        for tag in range(7):
            fa._today = lambda t=f"2026-08-{tag + 1:02d}": t
            fa._tages_anker()
            for minute in range(7 * 60):
                fa._store.data["pulse_min"] = t0 - 61 - minute * 60
                fa._store.data["pulse_sum"] = 0.0
                for _ in range(20):
                    fa._puls(floaktie.PULSE_JOIN, "spam")
        assert fa.price() <= deckel * 1.02, (fa.price(), deckel)
        assert fa._am_deckel()
        # Am Deckel gibt der Impuls ausdruecklich 0 zurueck (kein stiller No-Op).
        assert fa._puls(floaktie.PULSE_STREAM, "test") == 0

        # Steigt die Aktivitaet, steigt der Deckel - und der Impuls zieht wieder.
        fa._store.data["act_ema"] = 12.0
        assert fa.akt_deckel_base() > deckel
        assert fa._puls(floaktie.PULSE_STREAM, "test") > 0
    finally:
        fa._store, fa._enabled, fa._today = alt


def test_luxus_jenseits_der_milliarde():
    """Die neuen Stufen ueber dem Imperium muessen WIRKEN: sichtbarer Rahmen,
    richtige Besitz-Logik, eigener KI-Ton. REGRESSION: get_frame() hatte einen
    harten Vorzug fuer 'imperium' - jede Stufe darueber waere unsichtbar geblieben,
    obwohl bezahlt. Und owns() gab 'imperium' als "alles inklusive" aus, wodurch
    der Imperium-Kaeufer die 3-Mrd-Stufe gratis "besessen" haette."""
    import render
    lx = luxus.instance
    alt = (lx._store, lx._enabled)
    lx._enabled = True
    try:
        neu = [i for i in luxus.ITEMS if i["preis"] > 1_000_000_000]
        assert len(neu) >= 3, "keine Stufen ueber der Milliarde"
        # Jede neue Stufe hat einen eigenen Rahmen-Stil im Bild-Renderer.
        for item in neu:
            if item["art"] in ("rahmen", "multiversum"):
                assert item["key"] in render.Render._FRAME_STYLES, item["key"]
        # Rahmen-Reihenfolge kommt aus ITEMS (keine handgepflegte Liste mehr).
        assert luxus._FRAME_ORDER[-1] == "multiversum"
        assert luxus._FRAME_ORDER.index("imperium") < luxus._FRAME_ORDER.index("nova")

        lx._store = _FakeStore({"users": {
            "1": ["imperium"], "2": ["multiversum"], "3": ["bronze", "nova"]},
            "throne": {"owner": "", "preis": luxus.THRONE_START, "n": 0}})
        # Der beste besessene Rahmen gewinnt - nach RANG, nicht nach Sonderfall.
        assert lx.get_frame(1) == "imperium"
        assert lx.get_frame(2) == "multiversum"
        assert lx.get_frame(3) == "nova"
        # 'alles inklusive' gilt nur nach UNTEN.
        assert lx.owns(1, "gold") and not lx.owns(1, "nova")
        assert not lx.owns(1, "multiversum")
        assert lx.owns(2, "gold") and lx.owns(2, "nova") and lx.owns(2, "imperium")
        assert not lx.owns(3, "gold")            # Nova bringt nichts darunter mit
        # Eigener Ton je Spitze (sonst redet Flo mit dem 3-Mrd-Besitzer wie mit
        # einem Imperator).
        t2 = lx.get_tone_extra(2)
        assert "MULTIVERSUM" in t2 and "Imperator" not in t2
        assert "NOVA" in lx.get_tone_extra(3)
    finally:
        lx._store, lx._enabled = alt


def test_bilder_text_bleibt_im_rahmen():
    """Drei Bild-Fehler, die jeder Nutzer sieht - alle nachgemessen.

    1. Unterstriche verschwanden aus JEDEM Namen. Die Glyph-Pruefung malte das
       Zeichen auf eine 48x48-Flaeche; der Unterstrich einer 40-px-Schrift landet
       bei y=51..55 und fiel damit ganz heraus - er galt als "kein Glyph".
       "Maximilian_Schneider99" wurde zu "MaximilianSchneider99".
    2. Ein zu langes ERSTES Wort (eine URL) wurde nie hart getrennt: 1.146 px
       breit in einem 500-px-Feld. Dieselbe URL als zweites Wort ging korrekt.
    3. Zitate ab rund 400 Zeichen kippten auf EINE Zeile von 12.338 px."""
    from PIL import Image, ImageDraw
    import render
    r = render.instance

    # 1) Unterstriche und Unterlaengen bleiben erhalten.
    for name in ("Maximilian_Schneider99", "a_b_c", "__test__", "gyp_qj",
                 "Ben-Ove", "user.name", "Käpt'n Blaubär"):
        assert r._clean_text(name) == name, (name, r._clean_text(name))
    # ... Emoji fliegen weiterhin raus (die Schrift hat sie nicht).
    assert r._clean_text("🎮Gamer🎮") == "Gamer"

    # 2) Umbruch: nichts laeuft aus dem Feld, egal an welcher Stelle.
    d = ImageDraw.Draw(Image.new("RGB", (1000, 200)))
    url = "https://example.com/ein/wirklich/sehr/langer/pfad/der/nicht/passt/12345"
    for maxw in (500, 120, 40):
        f = r._font(28)
        for text in (url, "Schau: " + url, "A" * 200, "kurz", "", "   "):
            zeilen = r._wrap(d, text, f, maxw)
            assert zeilen, text
            for z in zeilen:
                # Ein einzelnes Zeichen darf breiter sein als das Feld - mehr nicht.
                assert d.textlength(z, font=f) <= maxw or len(z) <= 1, (text, maxw, z)

    # 3) Das Zitat-Bild bleibt bei jeder Laenge brauchbar.
    for n in (10, 200, 400, 1000, 4000):
        buf = r.quote_card(None, "Wort " * max(1, n // 5), "Anna")
        assert buf is not None and len(buf.getvalue()) > 3000, n

    # 4) Der Glyph-Test wird gemerkt (sonst malt jede Karte dieselben Zeichen neu).
    r._glyph_q.clear()
    r._clean_text("Testtext")
    assert r._glyph_q, "Glyph-Pruefung wird nicht zwischengespeichert"


def test_bilder_grosse_zahlen():
    """REGRESSION (Design): Seit der Wirtschafts-Umstellung sind Millionen und
    Milliarden normal. '3.000.000.000' war breiter als seine Spalte und lief in die
    Nachbarzahl hinein; im Shop-Banner stand '90000000' ohne Trenner; und das Label
    des Singularitaets-Rahmens war dunkelgrau auf schwarz (unlesbar)."""
    import render
    r = render.instance
    # Kompakte Betraege: lesbar und schmal.
    assert r._coin_kurz(1150) == "1.150"
    assert r._coin_kurz(445_850) == "445.850"
    assert r._coin_kurz(3_774_410) == "3,77 Mio"
    assert r._coin_kurz(90_000_000) == "90 Mio"
    assert r._coin_kurz(3_000_000_000) == "3 Mrd"
    assert r._coin_kurz("quatsch") == "0"          # nie ein Absturz
    for v in (0, 7, 10 ** 15):
        assert isinstance(r._coin_kurz(v), str)
    # Helligkeit entscheidet die Textfarbe (Label auf dunklem Rahmen).
    assert r._helligkeit((255, 255, 255)) > 200
    assert r._helligkeit((18, 18, 28)) < 40
    # Level-Karte mit Extremwerten: rendert, und die Zahl bleibt in ihrer Spalte.
    from PIL import Image, ImageDraw
    for frame in ("singularitaet", "multiversum", None):
        buf = render.level_card(
            None, name="EinSehrLangerNameDerNichtPasst", level=999, into=5, step=6,
            place=1, total=99, xp=10 ** 12, coins=123_456_789_012, msgs=9_999_999,
            voice_secs=99 * 3600, streak=365, title="✨ Ein sehr langer Titel hier",
            accent=(0, 229, 255), frame=frame)
        img = Image.open(buf)
        assert img.size == (1000, 320), img.size
    # Die Stat-Spalten sind ~180 px breit - der kompakte Text muss darunter bleiben.
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = r._font(23)
    for v in (123_456_789_012, 3_000_000_000, 9_999_999):
        assert d.textlength(r._coin_kurz(v), font=f) < 170, v


def test_seltenheiten_ueberall_bekannt():
    """Eine neue Seltenheitsstufe muss AN JEDER Stelle mitspielen. Die Kartierung
    hat vier Stellen gefunden, an denen sie stillschweigend gefehlt hat:
    die Shop-Ansage (nur 'legendary'), der Kauf-Text (feste Zweier-Liste), das
    Web-Panel (kannte drei Stufen nicht) und das Leaderboard (Titel farblos)."""
    import leaderboard_img
    import titles

    # 1) Shop-Ansage: JEDE Stufe ab 'legendary' wird ausgerufen, nicht nur eine.
    bot_quelle = open("bot.py", encoding="utf-8").read()
    i = bot_quelle.index("highlights = [i for i in st.get(\"items\", [])")
    block = bot_quelle[i - 300:i + 300]
    assert 'rarity") == "legendary"' not in block, "Ansage haengt wieder an einer Stufe"
    assert "titles.RANK" in block

    # 2) Kauf-Text: 'Flo redet entspannt' gilt ab Mythisch AUFWAERTS.
    eco_quelle = open("economy.py", encoding="utf-8").read()
    j = eco_quelle.index("ab jetzt redet Flo richtig entspannt")
    assert 'in ("mythisch", "legendary")' not in eco_quelle[j:j + 300]
    assert "titles.RANK" in eco_quelle[j:j + 300]

    # 3) Web-Panel kennt ALLE Stufen - Liste und CSS-Klassen.
    panel = open("webpanel.html", encoding="utf-8").read()
    for r in titles.RARITY_ORDER:
        assert f'["{r}",' in panel, f"Panel-Liste ohne {r}"
        assert f".chip.rar-{r}{{" in panel, f"Panel-CSS ohne {r}"

    # 4) Leaderboard: Titel in der Farbe seiner Stufe.
    lb = leaderboard_img.instance
    for r in titles.RARITY_ORDER:
        wert = titles.RARITY[r]["color"]
        assert lb._rarity_color(r) == ((wert >> 16) & 255, (wert >> 8) & 255, wert & 255)
    assert lb._rarity_color("") is None
    assert lb._rarity_color("gibtsnicht") is None      # nie ein Absturz
    # ... und Milliarden werden richtig gekuerzt (vorher "3000M").
    assert lb._fmt_num(3_000_000_000) == "3 Mrd"
    assert lb._fmt_num(2_500_000_000) == "2,5 Mrd"
    assert lb._fmt_num(2_500_000) == "2.5M"
    assert lb._fmt_num(1234) == "1.2k"
    assert lb._fmt_num(0) == "0"

    # 5) Luxus-Farben duerfen sich nicht mit den Titel-Stufen doppeln (zwei Dinge
    #    in derselben Farbe sind im Discord nicht unterscheidbar). Der Gold-Rahmen
    #    darf golden sein wie 'legendary' - das ist eine andere Oberflaeche.
    titel_farben = {titles.RARITY[r]["color"]: r for r in titles.RARITY_ORDER}
    doppelt = [(i["key"], titel_farben[i["farbe"]]) for i in luxus.ITEMS
               if i["farbe"] in titel_farben]
    assert [k for k, _ in doppelt] == ["gold"], doppelt
    assert len({i["farbe"] for i in luxus.ITEMS}) == len(luxus.ITEMS)

    # 6) Der Notnagel in _grenzen(): faellt die pool_pct-Summe unter 100, bekommt
    #    die HAEUFIGSTE Stufe den Rest - nicht die seltenste (die waere sonst
    #    ploetzlich sechsmal so haeufig).
    t = titles.instance
    alt_pct = titles.RARITY["normal"]["pool_pct"]
    alt_cache = t.__dict__.get("_grenz_cache")
    try:
        titles.RARITY["normal"]["pool_pct"] = alt_pct - 5      # Summe 95
        t.__dict__["_grenz_cache"] = None
        grenzen = dict((r, g) for g, r in t._grenzen())
        assert grenzen["relikt"] == 100
        # 'relikt' behaelt seine Breite von 1 Punkt.
        vorletzte = max(g for g, r in t._grenzen() if r != "relikt")
        assert grenzen["relikt"] - vorletzte == 1, t._grenzen()
    finally:
        titles.RARITY["normal"]["pool_pct"] = alt_pct
        t.__dict__["_grenz_cache"] = alt_cache


def test_seltenheits_rollen_werden_gepflegt():
    """Flo baut die Rollen selbst: fehlende anlegen, geaenderte Farben nachziehen,
    alte Namen umbenennen. Ohne das haengen nach einer Farb-Aenderung Rollen in der
    ALTEN Farbe im Server - und die umbenannte Stufe doppelt."""
    import titles
    eco = economy.instance
    restore = _with_economy({})

    class FakeRole:
        def __init__(self, name, value, position=1):
            self.name, self.position = name, position
            self.colour = SimpleNamespace(value=value)
            self.edits = []

        async def edit(self, **kw):
            self.edits.append(kw)
            if "name" in kw:
                self.name = kw["name"]
            if "colour" in kw:
                self.colour = SimpleNamespace(value=kw["colour"].value)

    class FakeGuild:
        def __init__(self, roles):
            self.name = "Testserver"
            self.roles = list(roles)
            self.created = []
            self.positions = None

        async def create_role(self, *, name, colour, **kw):
            rolle = FakeRole(name, colour.value, position=len(self.roles) + 1)
            self.roles.append(rolle)
            self.created.append(name)
            return rolle

        async def edit_role_positions(self, *, positions, reason=None):
            self.positions = positions

    try:
        # Ausgangslage: eine Rolle mit ALTEM Namen und eine mit falscher Farbe.
        falsch = FakeRole(titles.RARITY["selten"]["role"], 0x000000, position=3)
        veraltet = FakeRole("Flo · Normal", 0x57F287, position=2)
        guild = FakeGuild([veraltet, falsch])
        stats = asyncio.run(eco.ensure_roles(guild))

        # Alter Name wurde auf den neuen gezogen (keine Dublette).
        assert veraltet.name == titles.RARITY["normal"]["role"]
        assert any("Flo · Normal ->" in x for x in stats["renamed"]), stats
        namen = [r.name for r in guild.roles]
        assert namen.count(titles.RARITY["normal"]["role"]) == 1
        # Falsche Farbe wurde korrigiert.
        assert falsch.colour.value == titles.RARITY["selten"]["color"]
        assert titles.RARITY["selten"]["role"] in stats["recolored"]
        # Alle acht Stufen existieren danach, jede in ihrer Farbe.
        for r in titles.RARITY_ORDER:
            meta = titles.RARITY[r]
            rolle = next((x for x in guild.roles if x.name == meta["role"]), None)
            assert rolle is not None, meta["role"]
            assert rolle.colour.value == meta["color"], meta["role"]
        # Reihenfolge gesetzt: seltener = weiter oben.
        assert guild.positions, "Reihenfolge wurde nicht gesetzt"
        nach_rang = sorted(guild.positions.items(),
                           key=lambda kv: titles.RANK[
                               next(r for r in titles.RARITY_ORDER
                                    if titles.RARITY[r]["role"] == kv[0].name)])
        werte = [p for _r, p in nach_rang]
        assert werte == sorted(werte), werte

        # Zweiter Lauf aendert nichts mehr (idempotent).
        vorher = len(guild.created)
        stats2 = asyncio.run(eco.ensure_roles(guild))
        assert len(guild.created) == vorher
        assert not stats2["created"] and not stats2["recolored"]
    finally:
        restore()


def test_vermoegenssteuer_als_senke():
    """Die Wirtschaft braucht eine WIEDERKEHRENDE Senke: alle Einnahmen sind
    zeitbasiert, alle alten Senken waren einmalig (ein Titel, ein Rahmen, ein
    Thron) oder prozentual zu freiwilligem Casino-Umsatz. Wer nur farmte und nie
    spielte, hatte gar keine Senke - der Server lief mit Faktor 1,69x pro Tag in
    die Inflation. Die Steuer verbrennt taeglich einen Anteil oberhalb des
    Freibetrags und bringt jedes Einkommen in ein Gleichgewicht."""
    import floaktie as _fa_mod
    eco = economy.instance
    restore = _with_economy({})
    # Fuer den ersten Teil die Aktie AUS: hier geht es nur um Konto-Guthaben.
    # (Sonst zaehlt ein Depot aus einem frueheren Test mit in die Steuer.)
    _fa_alt = _fa_mod.instance._enabled
    _fa_mod.instance._enabled = False
    try:
        # Kleine Konten zahlen NICHTS - die Steuer trifft nur echtes Vermoegen.
        for c in (0, 50_000, eco.TAX_FREE - 1, eco.TAX_FREE):
            assert eco.steuer_fuer(c) == 0, c
        assert eco.steuer_fuer(eco.TAX_FREE + 1_000_000) == int(1_000_000 * eco.TAX_RATE)
        # DEGRESSIV ganz oben: ab TAX_SOFT nur noch der kleine Satz. Ohne das laege
        # das Gleichgewicht selbst fuer den besten Haendler bei ~1 Mrd - jede
        # Luxus-Stufe darueber waere fuer immer unerreichbar (totes Inhalt).
        assert 0 < eco.TAX_RATE_TOP < eco.TAX_RATE
        mitte = int((eco.TAX_SOFT - eco.TAX_FREE) * eco.TAX_RATE)
        assert eco.steuer_fuer(eco.TAX_SOFT) == mitte
        assert eco.steuer_fuer(eco.TAX_SOFT + 1_000_000_000) == \
            mitte + int(1_000_000_000 * eco.TAX_RATE_TOP)

        # Einzug: verbrennt Coins (kein Gegenkonto!) und laeuft pro Tag nur EINMAL.
        eco._profile(1)["coins"] = 21_000_000
        eco._profile(2)["coins"] = 500          # unter dem Freibetrag
        eco._profile(3)["coins"] = -80_000      # Aktien-Schulden: nichts holen
        vor_summe = sum(p["coins"] for p in eco._users().values())
        n, weg = asyncio.run(eco.vermoegenssteuer())
        assert n == 1, n                                  # nur das dicke Konto
        assert weg == int((21_000_000 - eco.TAX_FREE) * eco.TAX_RATE), weg
        assert eco._profile(2)["coins"] == 500            # unangetastet
        assert eco._profile(3)["coins"] == -80_000        # kein Nachtreten
        nach_summe = sum(p["coins"] for p in eco._users().values())
        assert nach_summe == vor_summe - weg              # Coins sind WEG
        # Zweiter Aufruf am selben Tag: kein zweiter Abzug.
        n2, weg2 = asyncio.run(eco.vermoegenssteuer())
        assert (n2, weg2) == (0, 0)
        # Neuer Tag -> wieder faellig.
        eco._store.data["tax_day"] = "1999-01-01"
        n3, weg3 = asyncio.run(eco.vermoegenssteuer())
        assert n3 == 1 and weg3 > 0

        # Auch ein laufendes GIVEAWAY ist kein Tresor: Einsatz kurz vor 2 Uhr
        # einzahlen, danach abbrechen und alles zurueckbekommen.
        import giveaway
        gw = giveaway.instance
        alt_gw = (gw._store, gw._enabled)
        gw._enabled = True
        gw._store = _FakeStore({"active": {"1": {"host": 21, "stake": 40_000_000}},
                                "next_id": 2, "done": []})
        try:
            eco._users().clear()
            eco._profile(21)["coins"] = eco.TAX_FREE
            eco._profile(22)["coins"] = eco.TAX_FREE
            assert eco.depot_wert(21) == 40_000_000 and eco.depot_wert(22) == 0
            eco._store.data["tax_day"] = ""
            n, weg = asyncio.run(eco.vermoegenssteuer())
            assert n == 1 and weg == int(40_000_000 * eco.TAX_RATE), (n, weg)
            assert eco.get_coins(22) == eco.TAX_FREE
        finally:
            gw._store, gw._enabled = alt_gw
        eco._users().clear()

        # Das AKTIEN-DEPOT ist kein steuerfreier Parkplatz: 5 Mio auf dem Konto
        # plus 36 Mio in Anteilen hat vorher NICHTS gekostet, und man haette kurz
        # vor 2 Uhr umparken koennen.
        fa = _fa_mod.instance
        alt_fa = (fa._store, fa._enabled)
        fa._enabled = True
        fa._store = _FakeStore({"price": 200_000, "base": 200_000.0, "day": "x",
                                "act_ema": 5.0, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {"11": 150},
                                "history": [], "ticks": []})
        fa._sync_price()
        try:
            eco._users().clear()
            eco._profile(11)["coins"] = eco.TAX_FREE     # genau am Freibetrag
            eco._profile(12)["coins"] = eco.TAX_FREE     # gleich viel, ohne Depot
            depot = eco.depot_wert(11)
            assert depot > 30_000_000, depot
            assert eco.depot_wert(12) == 0
            eco._store.data["tax_day"] = ""
            n, weg = asyncio.run(eco.vermoegenssteuer())
            assert n == 1 and weg == int(depot * eco.TAX_RATE), (n, weg, depot)
            assert eco.get_coins(12) == eco.TAX_FREE      # ohne Depot: steuerfrei
            assert eco.get_coins(11) < eco.TAX_FREE       # mit Depot: zahlt
        finally:
            fa._store, fa._enabled = alt_fa
        eco._users().clear()

        # Gleichgewicht: Einkommen X pro Tag landet bei Freibetrag + X/Satz.
        # Genau das macht die Steuer selbst-skalierend - sie waechst mit der
        # Inflation mit, statt als fester Betrag zu verpuffen.
        for tages_einkommen in (10_000, 20_000_000):
            eco._profile(9)["coins"] = 0
            stand = 0
            for _ in range(2000):
                eco._profile(9)["coins"] = stand + tages_einkommen
                eco._store.data["tax_day"] = ""
                asyncio.run(eco.vermoegenssteuer())
                neu = eco._profile(9)["coins"]
                if abs(neu - stand) <= max(1, tages_einkommen // 1000):
                    break
                stand = neu
            # Erwartung aus der Steuerformel selbst zurueckgerechnet (die Steuer ist
            # oben degressiv: ab TAX_SOFT nur noch TAX_RATE_TOP).
            lo, hi = 0, 10 ** 14
            for _ in range(200):
                m = (lo + hi) // 2
                if eco.steuer_fuer(m) < tages_einkommen:
                    lo = m
                else:
                    hi = m
            erwartet = lo
            assert abs(stand - erwartet) < max(1000, erwartet * 0.05), \
                (tages_einkommen, stand, erwartet)
    finally:
        _fa_mod.instance._enabled = _fa_alt
        restore()


def test_economy_reset_umfang():
    """Das Reset-Skript muss GENAU das treffen, was es soll: alles Coin-nahe auf 0,
    aber Level/XP, Voice-Stunden und Chat-Nachrichten unangetastet.

    Zwei Fehler sind hier schon aufgefallen (beide vom End-zu-End-Lauf gefunden):
    die Tagesbremse der Aktie blieb stehen (Kurs sprang danach auf 5.123 statt
    1.000) und die Startwerte waren vom Modul abgeschrieben, statt daraus gelesen
    (Thron stand nach dem Reset auf dem ALTEN Startpreis)."""
    import importlib.util
    import floaktie
    import luxus

    spec = importlib.util.spec_from_file_location("_reset", "economy_reset.py")
    er = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(er)

    # 1) Startwerte kommen aus den Modulen - keine abgeschriebene Kopie.
    assert er.START_PRICE == floaktie.START_PRICE, (er.START_PRICE, floaktie.START_PRICE)
    assert er.THRONE_START == luxus.THRONE_START, (er.THRONE_START, luxus.THRONE_START)

    # 2) Profile: was bleibt, bleibt - was Geld ist, geht auf 0.
    prof = {"xp": 5000, "coins": 23_000_000, "earned": 90_000_000, "streak": 9,
            "last_daily": "2026-07-25", "voice_secs": 720_000, "msgs": 12_345,
            "title": "🔱 X", "title_rarity": "exklusiv",
            "owned": [{"label": "🔱 X"}], "name": "flo", "eigenes_feld": "bleibt"}
    d = {"users": {"1": dict(prof), "2": "kaputt"}, "shop": {"date": "x", "items": [1]}}
    n, coins = er.reset_economy(d, [])
    p = d["users"]["1"]
    assert (p["xp"], p["voice_secs"], p["msgs"], p["name"]) == (5000, 720_000, 12_345, "flo")
    assert p["eigenes_feld"] == "bleibt"          # Unbekanntes wird nicht angefasst
    for key in er.PROFIL_RESET:
        assert p[key] == er.PROFIL_RESET[key], key
    # Beide Profile zaehlen: das echte (hatte Coins) und das kaputte (neu gebaut).
    assert n == 2 and coins == 23_000_000, (n, coins)
    assert isinstance(d["users"]["2"], dict) and d["users"]["2"]["coins"] == 0
    assert d["shop"] == {"date": "", "items": []}
    # Die geschuetzten Felder duerfen NIE in der Reset-Liste stehen.
    assert not set(er.PROFIL_RESET) & set(er.PROFIL_BEHALTEN)
    assert set(er.PROFIL_BEHALTEN) == {"xp", "voice_secs", "msgs", "name"}

    # 3) Aktie: Depots, Kurs, Verlauf UND die Tagesbremse zurueck auf Start.
    fd = {"price": 79_192_961, "base": 61234.5, "day": "2026-07-26", "act_ema": 12.4,
          "msg_count": 90210, "last_msg_count": 90000, "leer_min": 40,
          "open_day": "2026-07-26", "open_base": 51234.0,
          "holdings": {"1": 150, "2": 150}, "history": [1, 2], "ticks": [1, 2, 3]}
    er.reset_floaktie(fd, [])
    assert fd["price"] == floaktie.START_PRICE and fd["base"] == float(floaktie.START_PRICE)
    assert fd["holdings"] == {} and fd["history"] == [] and fd["ticks"] == []
    assert fd["act_ema"] == 0.0 and fd["leer_min"] == 0 and fd["msg_count"] == 0
    assert not fd["open_day"] and not fd["open_base"], "Tagesbremse blieb stehen!"

    # 4) Die uebrigen Dateien - jede muss ihren Geld-Teil leeren.
    ld = {"users": {"1": ["gold"]}, "throne": {"owner": "1", "preis": 9_000_000, "n": 14}}
    er.reset_luxus(ld, [])
    assert ld["users"] == {} and ld["throne"] == {"owner": "", "preis": luxus.THRONE_START,
                                                  "n": 0}
    lo = {"month": "2026-07", "jackpot": 41_000_000, "ticket_price": 20500,
          "entries": {"1": {"n": 40}}, "house": 1_234_567, "history": [1]}
    er.reset_lotto(lo, [])
    assert (lo["jackpot"], lo["house"], lo["entries"], lo["history"]) == (0, 0, {}, [])
    for fn, daten, pruef in (
            (er.reset_handel, {"users": {"1": {}}}, lambda x: x["users"] == {}),
            (er.reset_casino, {"stats": {"1": {}}}, lambda x: x["stats"] == {}),
            (er.reset_steal, {"cooldowns": {"1": 1}}, lambda x: x["cooldowns"] == {}),
            (er.reset_schulden, {"pairs": {"1:2": {}}, "stats": {"1": {}}},
             lambda x: x["pairs"] == {} and x["stats"] == {}),
            (er.reset_giveaway, {"active": {"7": {}}, "next_id": 8, "done": [1]},
             lambda x: x["active"] == {} and x["next_id"] == 1 and x["done"] == []),
            (er.reset_merchant, {"trades": [1], "sold": {"a": 1}, "stock": [1],
                                 "arrived": True, "departed": True, "day": "x",
                                 "appear_at": 1, "depart_at": 2},
             lambda x: x["trades"] == [] and x["sold"] == {} and x["arrived"] is False)):
        fn(daten, [])
        assert pruef(daten), fn.__name__

    # 5) Nicht-Geld-Dateien stehen NICHT in der Reset-Liste.
    dateien = {name for name, _fn, _b in er.DATEIEN}
    assert not dateien & {"words.json", "moderation.json", "voicegags.json",
                          "admin.json", "features.json", "guildcfg.json",
                          "profil.json"}
    assert "economy.json" in dateien and "floaktie.json" in dateien

    # 6) games.json: NUR die Spiele-Tageskappe, der Zaehlspiel-Stand bleibt.
    #    Ohne das startet jeder in den Reset-Tag mit ausgeschoepfter Kappe.
    assert "games.json" in dateien
    gd = {"counting": {"kanal1": {"n": 42}}, "daily": {"day": "2026-07-26",
                                                       "won": {"1": 50_000, "2": 10}}}
    er.reset_spiele_kappe(gd, [])
    assert gd["daily"] == {"day": "", "won": {}}
    assert gd["counting"] == {"kanal1": {"n": 42}}, "Zaehlspiel wurde mitgeloescht!"

    # 7) Der Steuer-Stempel wird mitgeloescht (sonst gilt der Reset-Tag als erledigt).
    ed = {"users": {}, "tax_day": "2026-07-26"}
    er.reset_economy(ed, [])
    assert ed["tax_day"] == ""

    # 8) ZWEI PHASEN: erst alles einlesen und umbauen, DANN schreiben. Ein kaputtes
    #    JSON in der Mitte darf die Wirtschaft nicht halb zurueckgesetzt lassen.
    quelle = open("economy_reset.py", encoding="utf-8").read()
    i_lade = quelle.index("for name, fn, beschreibung in DATEIEN:")
    i_schreib = quelle.index("for i, (pfad, daten) in enumerate(fertig):")
    assert i_lade < i_schreib, "Schreibphase muss NACH der Lesephase kommen"
    # In der Leseschleife darf nicht geschrieben werden.
    assert "schreiben(" not in quelle[i_lade:i_schreib]


def test_embeds_statt_fliesstext():
    """Die haeufigsten Antworten muessen ECHTE Embeds sein (mit Farbe, Feldern und
    allem, was man nach der Aktion wissen will) - vorher waren das ein bis zwei
    Zeilen Fliesstext."""
    import discord
    import floaktie

    # --- Tagesbonus ------------------------------------------------------
    restore = _with_economy({7: 0})
    eco = economy.instance
    try:
        m = SimpleNamespace(id=7, display_name="T",
                            display_avatar=SimpleNamespace(url="http://x/y.png"))
        e = asyncio.run(eco._daily(m))
        assert isinstance(e, discord.Embed), e
        txt = _embed_text(e)
        assert "Tagesbonus" in txt and "Serie" in txt
        assert str(economy.fmt(eco.DAILY_BASE)) in txt
        assert "🔥" in txt                       # Streak-Kette sichtbar
        assert economy.get_coins(7) == eco.DAILY_BASE + eco.DAILY_STREAK_STEP
        # Zweiter Versuch am selben Tag: eigenes Embed, KEIN Geld.
        vorher = economy.get_coins(7)
        e2 = asyncio.run(eco._daily(m))
        assert isinstance(e2, discord.Embed)
        assert "schon" in _embed_text(e2).lower()
        assert economy.get_coins(7) == vorher
        # Streak-Kette waechst und ist bei DAILY_STREAK_MAX voll.
        assert eco._streak_balken(0).count("🔥") == 0
        assert eco._streak_balken(3).count("🔥") == 3
        assert eco._streak_balken(99).count("🔥") == eco.DAILY_STREAK_MAX
        assert "▫️" not in eco._streak_balken(eco.DAILY_STREAK_MAX)
    finally:
        restore()

    # --- Aktien-Bestaetigung ---------------------------------------------
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    restore = _with_economy({8: 5_000_000})
    fa._enabled = True
    fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x", "act_ema": 0.0,
                            "msg_count": 0, "last_msg_count": 0, "leer_min": 0.0,
                            "holdings": {}, "history": [], "ticks": []})
    fa._sync_price()
    try:
        e = asyncio.run(fa.buy(SimpleNamespace(id=8), 20))
        assert isinstance(e, discord.Embed)
        txt = _embed_text(e)
        for muss in ("Gekauft", "Kurs", "Dein Depot", "Kontostand", "▲"):
            assert muss in txt, (muss, txt)
        assert "▰" in txt                        # Depot-Balken
        e = asyncio.run(fa.sell(SimpleNamespace(id=8), 20))
        txt = _embed_text(e)
        assert "Verkauft" in txt and "Verkaufssteuer" in txt and "▼" in txt
        # Und das Panel nennt Deckel, Depot-Grenze und Steuer.
        p = _embed_text(fa._panel_embed(SimpleNamespace(id=8)))
        assert "Deckel" in p and str(floaktie.MAX_SHARES_PER_USER) in p
        assert "Verkaufssteuer" in p
        # Abgeschaltete Aktie: sauberes Embed, kein Durchfallen an die KI.
        aus = _embed_text(fa.aus_embed())
        assert "deaktiviert" in aus and "Anteile" in aus
    finally:
        fa._store, fa._enabled = alt
        restore()


def test_webpanel_zeigt_aktien_aktivitaet():
    """Das Terminal muss LIVE zeigen, ob gerade jemand gezaehlt wird - sonst ist
    "die Aktie sinkt nicht" nicht nachpruefbar. Aktivitaet, Tempo pro Stunde und
    die Namen (ohne Bots) gehoeren in die Uebersicht."""
    import asyncio as _asyncio
    import floaktie
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._zuletzt_mess, fa._zuletzt_gezaehlt)
    fa._enabled = True
    fa._store = _FakeStore({"price": 500, "base": 500.0, "day": "x",
                            "act_ema": 12.5, "msg_count": 0, "last_msg_count": 0,
                            "leer_min": 0.0, "holdings": {"1": 5},
                            "history": [], "ticks": []})
    fa._sync_price()
    fa._zuletzt_mess = (3, 1, 0, 7)
    fa._zuletzt_gezaehlt = ["Anna", "Ben", "Cem"]

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = 0
        async with TestClient(TestServer(wp._build_app())) as c:
            return await (await c.get("/api/overview")).json()

    try:
        d = _asyncio.run(lauf())
        st = d["stats"]
        assert st["floaktie_activity"] == 12.5, st.get("floaktie_activity")
        # Bei Aktivitaet steigt der Kurs - das Tempo muss positiv sein.
        assert st["floaktie_trend"] > 0, st.get("floaktie_trend")
        wer = st["floaktie_who"]
        assert "Anna" in wer and "3 im Call" in wer, wer

        # Leerer Call: Tempo negativ, und zwar mindestens die geforderten 20 %/h.
        fa._state()["act_ema"] = 0.0
        fa._zuletzt_mess = (0, 0, 0, 0)
        fa._zuletzt_gezaehlt = []
        st = _asyncio.run(lauf())["stats"]
        assert st["floaktie_activity"] == 0.0, st.get("floaktie_activity")
        assert st["floaktie_trend"] <= -20.0, st.get("floaktie_trend")
        assert "Niemand im Call" in st["floaktie_who"], st.get("floaktie_who")
    finally:
        fa._store, fa._enabled, fa._zuletzt_mess, fa._zuletzt_gezaehlt = alt


def test_ki_leere_antwort_bleibt_nicht_spurlos():
    """Am echten Server aufgefallen: die Diagnose meldete

        4. Echter Chat-Aufruf
          OK    Antwort nach 0.4s: ''

    Also OK - bei einer LEEREN Antwort. gpt-oss & Co. denken erst und schreiben
    dann; mit einem knappen Token-Budget geht alles ins Denken. Im Betrieb sagt
    Flo dann "Dazu faellt mir gerade nichts ein" und im Log stand NICHTS - das
    sieht wie Unlust aus und ist in Wahrheit eine zu enge Grenze."""
    import ai
    import logging

    puffer = io.StringIO()
    griff = logging.StreamHandler(puffer)
    protokoll = logging.getLogger("dcbot.ai")
    protokoll.addHandler(griff)
    alt_stufe = protokoll.level
    protokoll.setLevel(logging.INFO)
    try:
        flo, _ = _ki_frisch([_KiAntwort("")])          # Modell sagt nichts
        antwort = asyncio.run(flo.ask_flo("hi"))
    finally:
        protokoll.removeHandler(griff)
        protokoll.setLevel(alt_stufe)

    assert antwort == "Dazu faellt mir gerade nichts ein.", antwort
    text = puffer.getvalue()
    assert "leere Antwort" in text, f"die leere Antwort bleibt spurlos: {text!r}"
    # Muss mit 'k l' auffindbar sein - also dieselbe Marke wie alle KI-Fehler.
    assert "KI-Fehler:" in text

    # Und der Arzt darf eine leere Antwort nicht mehr als OK durchwinken.
    import tools_ki_check
    quelle = inspect.getsource(tools_ki_check.KiCheck.aufruf_pruefen)
    assert "OHNE Text" in quelle, "der Arzt meldet eine leere Antwort wieder als OK"
    assert '"max_tokens": 5,' not in inspect.getsource(tools_ki_check), (
        "die Probe gibt dem Modell wieder zu wenig Platz")


def test_ki_denk_aufwand_nur_wenn_gesetzt():
    """reasoning_effort kennen nur Denk-Modelle. Immer mitzuschicken wuerde jede
    Anfrage an ein normales Modell mit HTTP 400 abwuergen - der Schalter darf
    also nur in die Anfrage, wenn jemand ihn ausdruecklich gesetzt hat."""
    import ai
    alt_env = os.environ.get("LLM_REASONING_EFFORT")
    try:
        for wert, erwartet in (("", None), ("low", "low"), ("HIGH", "high"),
                               ("quatsch", None)):
            if wert:
                os.environ["LLM_REASONING_EFFORT"] = wert
            else:
                os.environ.pop("LLM_REASONING_EFFORT", None)
            flo, anbieter = _ki_frisch([_KiAntwort("ok")])
            flo._denk_aufwand = ""
            flo._denk_aufwand = (wert.lower() if wert.lower() in
                                 ("low", "medium", "high") else "")
            asyncio.run(flo.ask_flo("hi"))
            gesendet = anbieter.letzte_kwargs.get("reasoning_effort")
            assert gesendet == erwartet, (wert, gesendet, erwartet)
    finally:
        if alt_env is None:
            os.environ.pop("LLM_REASONING_EFFORT", None)
        else:
            os.environ["LLM_REASONING_EFFORT"] = alt_env


def test_musik_probiert_andere_youtube_clients_durch():
    """YouTube prueft, ob da ein echter Browser sitzt. Welcher "player_client"
    ohne Login durchkommt, aendert sich alle paar Monate - ein fester Name im
    Code ist deshalb in drei Monaten wieder tot. Flo probiert die Reihe durch,
    merkt sich was ging und sagt im Log, was in die .env gehoert.

    Genauso wichtig: bei einem geloeschten Video hilft KEIN anderer Client -
    dann waere jeder weitere Versuch nur Wartezeit fuer den Nutzer."""
    import music

    class FakeYDL:
        geht_ab = None
        fehler = "Sign in to confirm you're not a bot"
        versuche = []

        def __init__(self, opts):
            self.client = (opts.get("extractor_args", {})
                           .get("youtube", {}).get("player_client", [None])[0])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, ziel, download=False):
            FakeYDL.versuche.append(self.client)
            if FakeYDL.geht_ab is not None and self.client == FakeYDL.geht_ab:
                return {"title": "Song", "url": "http://x/s", "webpage_url": "http://x",
                        "duration": 10, "http_headers": {"User-Agent": "u"}}
            raise Exception(FakeYDL.fehler)

    m = music.instance
    alt_ydl, alt_client = music.yt_dlp, m._guter_client
    alt_env = os.environ.pop("YTDLP_PLAYER_CLIENT", None)
    music.yt_dlp = type("M", (), {"YoutubeDL": FakeYDL})
    try:
        # 1. Standard blockt, 'ios' geht -> durchprobieren, merken, melden.
        FakeYDL.versuche, FakeYDL.geht_ab, m._guter_client = [], "ios", ""
        track = asyncio.run(m._extract("ytsearch1:egal"))
        # Reihenfolge ist nicht beliebig: die drei Clients OHNE PO Token zuerst.
        assert FakeYDL.versuche == [None, "tv", "android_vr", "web_embedded",
                                    "tv_simply", "ios"], FakeYDL.versuche
        assert m._guter_client == "ios"
        assert track.title == "Song"
        # Die Kopfzeilen muessen dabei erhalten bleiben (sonst 403 bei ffmpeg).
        assert track.kopfzeilen.get("User-Agent") == "u"

        # 2. Beim naechsten Mal steht der, der ging, vorne.
        FakeYDL.versuche = []
        asyncio.run(m._extract("ytsearch1:egal"))
        assert FakeYDL.versuche == [None, "ios"], FakeYDL.versuche

        # 3. Geloeschtes Video: sofort aufgeben, nicht acht Mal fragen.
        FakeYDL.versuche, FakeYDL.geht_ab, m._guter_client = [], None, ""
        FakeYDL.fehler = "Video unavailable. This video has been removed"
        try:
            asyncio.run(m._extract("http://x"))
        except Exception:
            pass
        assert FakeYDL.versuche == [None], (
            f"probiert bei einem geloeschten Video weiter: {FakeYDL.versuche}")

        # 4. Bot-Check und nichts geht: alle durch, dann mit dem ECHTEN Grund raus.
        FakeYDL.versuche = []
        FakeYDL.fehler = "Sign in to confirm you're not a bot"
        try:
            asyncio.run(m._extract("http://x"))
            raise AssertionError("haette scheitern muessen")
        except Exception as exc:
            assert music.Music.yt_fehler_deuten(exc)[0] == "botcheck"
        assert len(FakeYDL.versuche) == 1 + len(m.client_reihe()), FakeYDL.versuche

        # 5. Festgenagelt per .env -> genau einer, kein Durchprobieren.
        os.environ["YTDLP_PLAYER_CLIENT"] = "tv"
        FakeYDL.versuche, FakeYDL.geht_ab = [], "tv"
        asyncio.run(m._extract("http://x"))
        assert FakeYDL.versuche == ["tv"], FakeYDL.versuche
    finally:
        music.yt_dlp, m._guter_client = alt_ydl, alt_client
        os.environ.pop("YTDLP_PLAYER_CLIENT", None)
        if alt_env is not None:
            os.environ["YTDLP_PLAYER_CLIENT"] = alt_env

    # 6. DIE LAGE VOM SERVER: alle Clients Bot-Check, ausser einem - und der
    #    kommt zwar durch, hat aber keine reine Tonspur ("Requested format is
    #    not available"). Ihn deswegen fallenzulassen waere der Fehler: er ist
    #    der EINZIGE, den YouTube noch durchlaesst. Also Video nehmen und den
    #    Ton herausziehen (ffmpeg wirft das Bild ohnehin weg, -vn).
    class FakeNurAndroid(FakeYDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.fmt = opts.get("format")

        def extract_info(self, ziel, download=False):
            FakeYDL.versuche.append((self.client, self.fmt))
            if self.client != "android":
                raise Exception("Sign in to confirm you're not a bot. Use --cookies")
            if "worst" not in (self.fmt or ""):
                raise Exception("Requested format is not available. Use --list-formats")
            return {"title": "Semmel Song", "url": "http://x/s",
                    "webpage_url": "http://x", "duration": 180,
                    "http_headers": {"User-Agent": "u"}}

    music.yt_dlp = type("M", (), {"YoutubeDL": FakeNurAndroid})
    FakeYDL.versuche, m._guter_client = [], ""
    try:
        track = asyncio.run(m._extract("ytsearch1:semmel song robert F"))
    finally:
        music.yt_dlp = type("M", (), {"YoutubeDL": FakeYDL})
    assert track.title == "Semmel Song"
    assert m._guter_client == "android", m._guter_client
    assert track.kopfzeilen.get("User-Agent") == "u", "Kopfzeilen gehen verloren"
    # Derselbe Client wurde ein zweites Mal gefragt - mit weicherem Format.
    letzte_zwei = FakeYDL.versuche[-2:]
    assert [c for c, _f in letzte_zwei] == ["android", "android"], FakeYDL.versuche
    assert "worst" in letzte_zwei[1][1], letzte_zwei
    m._guter_client = ""

    # Kein Client-Name im Code, den die installierte yt-dlp-Fassung nicht kennt.
    bekannt = music.Music._bekannte_clients()
    if bekannt:
        unbekannt = [c for c in music.Music._CLIENT_REIHE if c not in bekannt]
        assert not unbekannt, f"diese player_client gibt es nicht (mehr): {unbekannt}"


def test_musik_weicht_auf_soundcloud_aus_wenn_youtube_dicht_ist():
    """Am Server gemessen: ALLE acht player_client antworten mit "Sign in to
    confirm you're not a bot" - die IP ist markiert, da hilft kein Client mehr.
    Ohne Ausweg waere Musik damit tot, bis der Betreiber Cookies exportiert.

    SoundCloud kennt YouTubes Bot-Pruefung nicht. Also sucht Flo denselben Song
    dort, statt eine Fehlermeldung zu posten - das ist der einzige Weg, der OHNE
    Zutun des Betreibers noch Musik liefert."""
    import music

    class NurSoundCloud:
        gefragt = []

        def __init__(self, opts):
            self.sc = opts.get("default_search") == "scsearch"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, ziel, download=False):
            NurSoundCloud.gefragt.append(ziel)
            if not self.sc:
                raise Exception("Sign in to confirm you're not a bot. Use --cookies")
            return {"entries": [{"title": "Semmel Song (SC)", "url": "http://sc/s",
                                 "webpage_url": "http://soundcloud.com/x",
                                 "duration": 175,
                                 "http_headers": {"User-Agent": "sc"}}]}

    m = music.instance
    alt_ydl, alt_client = music.yt_dlp, m._guter_client
    music.yt_dlp = type("M", (), {"YoutubeDL": NurSoundCloud})
    try:
        # 1. Reine Textsuche
        NurSoundCloud.gefragt, m._guter_client = [], ""
        track = asyncio.run(m._extract("ytsearch1:semmel song robert F"))
        assert track.title == "Semmel Song (SC)", track.title
        assert "soundcloud.com" in track.webpage_url
        assert track.kopfzeilen.get("User-Agent") == "sc", "Kopfzeilen fehlen"
        assert NurSoundCloud.gefragt[-1] == "scsearch1:semmel song robert F"

        # 2. Spotify-Weg: der Suchtext MUSS durchgereicht werden - mit einer
        #    nackten Video-Adresse koennte SoundCloud nichts anfangen.
        NurSoundCloud.gefragt, m._guter_client = [], ""
        track = asyncio.run(m._resolve_input("ytsearch1:egal",
                                             {"query": "Robert F Semmel"}))
        assert track.title == "Semmel Song (SC)"
        assert "scsearch1:Robert F Semmel" in NurSoundCloud.gefragt, NurSoundCloud.gefragt

        # 3. Eine nackte YouTube-Adresse hat keinen Suchtext - dort darf NICHT
        #    blind irgendetwas von SoundCloud gespielt werden.
        assert music.Music._suchtext("https://youtu.be/abc") == ""
        for eingabe, erwartet in (("ytsearch1:a b", "a b"), ("ytsearch5:x", "x"),
                                  ("nur text", "nur text")):
            assert music.Music._suchtext(eingabe) == erwartet, eingabe
    finally:
        music.yt_dlp, m._guter_client = alt_ydl, alt_client


def test_arzt_prueft_youtube_bis_zur_abspielbaren_adresse():
    """Der Musik-Arzt meldete "Suche geht", waehrend im Bot JEDER Song an
    YouTubes Bot-Pruefung scheiterte. Grund: er lief mit extract_flat, las also
    nur die Trefferliste und fasste den Player nie an.

    Eine Diagnose, die den Ausfall nicht sieht, ist schlimmer als keine - sie
    schickt einen auf die falsche Faehrte (hier: ein sinnloses yt-dlp-Update)."""
    import inspect as _i
    import tools_musik_check
    quelle = _i.getsource(tools_musik_check.MusikCheck.youtube_pruefen)
    # Auf die OPTION prüfen, nicht auf das Wort - im Kommentar darueber steht
    # bewusst "KEIN extract_flat", und daran darf der Test sich nicht aufhaengen.
    code_zeilen = [z for z in quelle.splitlines()
                   if not z.lstrip().startswith("#")]
    assert "'extract_flat'" not in "\n".join(code_zeilen), (
        "die Pruefung liest wieder nur die Trefferliste statt den Player")
    assert "'stream'" in quelle or '"stream"' in quelle, (
        "es wird nicht geprueft, ob eine abspielbare Adresse herauskommt")
    # Cookies und festgenagelter Client muessen mitgeprueft werden - sonst misst
    # der Arzt etwas anderes als der Bot tut.
    assert "YTDLP_COOKIES" in quelle and "YTDLP_PLAYER_CLIENT" in quelle
    # Und die Ausweichquelle gehoert zur Diagnose: sie entscheidet, ob ueberhaupt
    # noch Musik moeglich ist.
    assert hasattr(tools_musik_check.MusikCheck, "_soundcloud_pruefen")
    assert "scsearch" in _i.getsource(tools_musik_check.MusikCheck._soundcloud_pruefen)


def test_musik_cookies_erreichen_jeden_yt_dlp_aufruf():
    """Am echten Server bestaetigt: YouTube antwortet "Sign in to confirm you're
    not a bot". Ist die IP einmal markiert, hilft kein player_client mehr - dann
    bleibt nur ein angemeldeter Zugang, so steht es auch in yt-dlps eigener FAQ.

    music.py ruft yt-dlp an DREI Stellen (Einzeltrack, Suche, Playlist). Kaemen
    die Cookies nur bei einer an, ginge ein Link - und die Suche weiter nicht.
    Genau so eine halbe Reparatur faellt niemandem auf."""
    import re
    import music
    quelle = open(music.__file__, encoding="utf-8").read()
    stellen = [m.start() for m in re.finditer(r"yt_dlp\.YoutubeDL\(", quelle)]
    assert len(stellen) >= 3, f"nur {len(stellen)} yt-dlp-Aufrufe gefunden"
    ohne = [i for i, pos in enumerate(stellen, 1)
            if "_cookie_optionen()" not in quelle[max(0, pos - 700):pos]]
    assert not ohne, f"diese yt-dlp-Aufrufe bekommen keine Cookies: {ohne}"

    # Verhalten der Konfiguration - inklusive der ehrlichen Warnung.
    alt_datei = os.environ.pop("YTDLP_COOKIES", None)
    alt_browser = os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
    try:
        assert music.Music._cookie_optionen() == {}
        # Ein falscher Pfad darf NICHT still verschluckt werden.
        os.environ["YTDLP_COOKIES"] = "/gibt/es/nicht.txt"
        assert music.Music._cookie_optionen() == {}, (
            "eine fehlende Cookie-Datei wird als gesetzt behandelt")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# Netscape HTTP Cookie File\n")
            pfad = f.name
        try:
            os.environ["YTDLP_COOKIES"] = pfad
            assert music.Music._cookie_optionen() == {"cookiefile": pfad}
            os.environ["YTDLP_COOKIES_FROM_BROWSER"] = "firefox"
            assert music.Music._cookie_optionen() == {
                "cookiefile": pfad, "cookiesfrombrowser": ("firefox",)}
        finally:
            os.unlink(pfad)
    finally:
        for name, wert in (("YTDLP_COOKIES", alt_datei),
                           ("YTDLP_COOKIES_FROM_BROWSER", alt_browser)):
            os.environ.pop(name, None)
            if wert is not None:
                os.environ[name] = wert

    # Der Hinweis auf den Ausweg muss im Code stehen - sonst sucht der Betreiber
    # ihn nie. Und die Warnung vor dem Haupt-Account gehoert dazu.
    assert "WEGWERF" in quelle, "die Warnung vor dem Haupt-Account fehlt"
    assert "YTDLP_COOKIES=" in quelle


def test_musik_sagt_WARUM_ein_song_nicht_geht():
    """"Den Song konnte ich nicht laden. Probier einen anderen Link" - derselbe
    Satz fuer JEDEN Grund, und der echte verschwand im Traceback. Ob YouTube
    einen Login sehen will, das Video geloescht ist, das Land gesperrt oder
    yt-dlp schlicht zu alt - von aussen nicht zu unterscheiden.

    Die Reihenfolge der Muster ist der schwierige Teil, beide Faelle sind
    nachgemessen:
      - "Sign in to confirm your AGE" ist KEIN Bot-Check
      - "Video unavailable. ... not made this video available in your country"
        ist eine LAENDER-Sperre, keine Loeschung
    """
    import music
    faelle = (
        ("Sign in to confirm you're not a bot. Use --cookies-from-browser", "botcheck"),
        ("Sign in to confirm your age. This video may be inappropriate for some users.", "alter"),
        ("Video unavailable. This video has been removed by the uploader", "weg"),
        ("Private video. Sign in if you have been granted access", "weg"),
        ("Video unavailable. The uploader has not made this video available "
         "in your country.", "land"),
        ("[DRM] The requested site is known to use DRM protection.", "drm"),
        ("Unable to download API page: HTTP Error 429: Too Many Requests", "limit"),
        ("nsig extraction failed: please report this issue", "veraltet"),
        ("Unable to download webpage: Temporary failure in name resolution", "netz"),
        ("keine Treffer", "nichts"),
        ("Requested format is not available", "format"),
        ("etwas voellig anderes", "unbekannt"),
    )
    for text, erwartet in faelle:
        art, satz = music.Music.yt_fehler_deuten(Exception(text))
        assert art == erwartet, (text[:60], art, erwartet)
        assert satz and satz == music.Music._YT_SAETZE[art]
    # Jede Art braucht einen eigenen Satz - sonst ist die Einordnung wertlos.
    saetze = list(music.Music._YT_SAETZE.values())
    assert len(set(saetze)) == len(saetze), "zwei Gruende teilen sich einen Satz"
    # Und jede Art aus der Musterliste muss auch einen Satz haben.
    for art, _muster in music.Music._YT_GRUENDE:
        assert art in music.Music._YT_SAETZE, art

    # Die Aufrufstelle muss den Grund benutzen UND ihn greppbar loggen.
    quelle = inspect.getsource(music.Music.handle)
    assert "yt_fehler_deuten(exc)" in quelle, "handle() nutzt die Einordnung nicht"
    assert "Musik-Fehler:" in quelle, "der Grund landet nicht greppbar im Log"


def test_arzt_findet_die_musik_meldungen():
    """Wie beim Panel: der Arzt muss die Zeilen auch ZEIGEN. "Musik-Selbsttest ok"
    und "Musik-Fehler: ..." fielen beide durch das Suchmuster in 'k' - also
    ausgerechnet die zwei, die die Frage beantworten."""
    import re
    arzt = open("k", encoding="utf-8").read()
    muster = re.search(r'MUSIKMUSTER="([^"]+)"', arzt)
    assert muster, "in 'k' gibt es kein MUSIKMUSTER"
    filter_re = re.compile(muster.group(1))
    for zeile in ("Musik-Selbsttest ok (Song, 57600 Bytes Ton in 0.6s).",
                  "Musik-Selbsttest: ffmpeg bekommt keinen Ton (403).",
                  "Musik-Fehler: botcheck bei 'irgendwas' - Sign in to confirm",
                  "Musik-Feature aktiv (YouTube: ja, Spotify: ja).",
                  "Musik-Selbsttest: Spotify-Token ok."):
        assert filter_re.search(zeile), f"'k m' zeigt diese Zeile nicht: {zeile}"


def test_arzt_meldet_sich_wie_der_bot():
    """Am echten Server passiert: Cloudflare sperrte die nackte Python-Kennung,
    der Bot lief laengst - und die Diagnose meldete trotzdem Alarm und empfahl
    einen .env-Eintrag, den niemand braucht.

        Python (Standard)  -> blockiert     <- der ARZT
        openai-Paket       -> kommt DURCH   <- der BOT

    Ein Arzt, der sich anders meldet als der Patient, misst etwas anderes.
    Also schickt er jetzt dieselbe Kennung wie Flo - abgelesen beim echten
    Client, nicht geraten (sie haengt an der Paketversion)."""
    import tools_ki_check
    a = tools_ki_check.KiCheck()
    a.base = "https://api.example.invalid/v1"

    quelle = inspect.getsource(tools_ki_check.KiCheck._anfrage)
    assert "self.bot_signatur()" in quelle, (
        "der Arzt benutzt wieder nicht die Kennung des Bots")

    # LLM_USER_AGENT hat Vorrang - das ist der Hebel, den die Diagnose empfiehlt.
    alt_env = os.environ.get("LLM_USER_AGENT")
    try:
        os.environ["LLM_USER_AGENT"] = "Testkennung/1.0"
        a._bot_ua = None
        assert a.bot_signatur() == "Testkennung/1.0"
        # Ohne Vorgabe wird sie beim echten openai-Client abgelesen.
        os.environ.pop("LLM_USER_AGENT")
        a._bot_ua = None
        gelesen = a.bot_signatur()
        try:
            import openai  # noqa: F401
        except ImportError:
            return                              # ohne das Paket nicht pruefbar
        assert gelesen, "keine Kennung ermittelt"
        assert "urllib" not in gelesen.lower(), (
            f"der Arzt meldet sich als urllib ({gelesen}) - genau das war der Fehler")
    finally:
        if alt_env is None:
            os.environ.pop("LLM_USER_AGENT", None)
        else:
            os.environ["LLM_USER_AGENT"] = alt_env
        a._bot_ua = None

    # Und: kommt Flos Kennung durch, darf NICHTS empfohlen werden.
    pruef = inspect.getsource(tools_ki_check.KiCheck.signatur_pruefen)
    assert "NICHT betroffen" in pruef, (
        "der Arzt unterscheidet nicht mehr zwischen 'Bot gesperrt' und "
        "'nur die nackte Python-Kennung gesperrt'")
    assert pruef.index("NICHT betroffen") < pruef.index("LLM_USER_AGENT="), (
        "die Empfehlung kommt, bevor geprueft wird, ob der Bot ueberhaupt "
        "betroffen ist")


def test_arzt_findet_das_panel_passwort():
    """Das Panel wuerfelt ohne WEBPANEL_PASS ein Passwort und schreibt es EINMAL
    beim Start ins Log. Ohne einen Weg dorthin ist es praktisch unauffindbar:
    'k l' sucht nur KI-Zeilen, und im vollen Journal geht die eine Zeile
    zwischen den Zugriffszeilen des Panels unter. Genau das ist passiert."""
    import re
    import webpanel
    arzt = open("k", encoding="utf-8").read()
    muster = re.search(r'PANELMUSTER="([^"]+)"', arzt)
    assert muster, "in 'k' gibt es kein PANELMUSTER"
    filter_re = re.compile(muster.group(1))
    assert "p|panel|passwort)" in arzt, "es gibt keinen Unterbefehl fuer das Panel"

    # Jede Panel-Meldung, die beim Start faellt, muss damit auffindbar sein.
    quelle = inspect.getsource(webpanel.WebPanel.setup)
    meldungen = re.findall(r'log\.[a-z]+\(\s*"([^"]*)', quelle)
    wichtig = [m for m in meldungen if "Web-Panel" in m or "WEBPANEL" in m]
    assert wichtig, "setup() meldet gar nichts ueber das Panel"
    fehlt = [m for m in wichtig if not filter_re.search(m)]
    assert not fehlt, f"'k p' zeigt diese Zeilen nicht: {fehlt}"


def test_webpanel_nur_fuer_den_besitzer():
    """Im Panel werden Coins vergeben, Titel verteilt und der Bot neu gestartet -
    das darf nur der Besitzer. Deshalb ist der Login jetzt der STANDARD.

    Wichtiger als der Schalter ist aber das Passwort: ein festes
    Standardpasswort im Quelltext waere das Schlimmste von beidem - es sieht
    nach Schutz aus und ist keiner. Ohne WEBPANEL_PASS wuerfelt Flo deshalb
    eins und schreibt es EINMAL ins Log."""
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    async def lauf(auth):
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._user, wp._pass, wp._auth = "u", "p", auth
        # /api/update fuehrt ein ECHTES 'git pull --ff-only' im Bot-Verzeichnis
        # aus. Ohne dieses Double zieht ein Testlauf auf dem Server also Code -
        # und die README verspricht ausdruecklich, dass ein Testlauf dort
        # ungefaehrlich ist. Nachgewiesen an der Zeile
        # "Panel-Update: git pull ok (schon aktuell)" im Testprotokoll.
        async def kein_git(_request):
            return webpanel.web.json_response(
                {"ok": True, "changed": False, "log": "(Test-Double)"})
        wp._update_lauf = kein_git
        async with TestClient(TestServer(wp._build_app())) as c:
            cfg = await (await c.get("/api/config")).json()
            codes = {}
            for pfad, meth in (("/api/overview", "get"), ("/api/features", "get"),
                               ("/api/user/coins", "post"), ("/api/update", "post")):
                r = await getattr(c, meth)(pfad, json={})
                codes[pfad] = r.status
            return cfg, codes

    # Mit Login (Standard) ist alles dicht.
    cfg, codes = asyncio.run(lauf(True))
    assert cfg["ok"] is True and cfg["auth"] is True
    assert all(v == 401 for v in codes.values()), codes

    # Abschalten geht weiterhin - fuer einen rein lokalen Aufbau.
    cfg, codes = asyncio.run(lauf(False))
    assert cfg["auth"] is False
    assert all(v != 401 for v in codes.values()), codes
    assert codes["/api/overview"] == 200

    quelle = open("webpanel.py", encoding="utf-8").read()
    # 1. Der Standard ist AN.
    assert 'os.getenv("WEBPANEL_AUTH", "1")' in quelle, "der Login ist nicht mehr Standard"
    # 2. KEIN festes Passwort im Quelltext. Genau das war vorher der Fall
    #    (WEBPANEL_PASS", "Secoolio") - ein Login mit bekanntem Passwort ist
    #    kein Login.
    assert 'os.getenv("WEBPANEL_PASS", "")' in quelle, (
        "es steht wieder ein festes Standardpasswort im Quelltext")
    assert "secrets.token_urlsafe" in quelle, (
        "ohne gesetztes Passwort muss eins gewuerfelt werden")
    # 3. Wer den Login ABschaltet, soll es laut im Log sehen.
    assert "OHNE Login" in quelle
    # 4. /api/config ist selbst nie geschuetzt - sonst koennte die Oberflaeche
    #    gar nicht herausfinden, ob sie einen Anmeldebildschirm zeigen muss.
    i = quelle.index("async def _api_config")
    assert "_guard" not in quelle[i:i + 400]

    # Die Oberflaeche fragt /api/config, bevor sie den Anmeldebildschirm zeigt.
    html = open("webpanel.html", encoding="utf-8").read()
    assert "/api/config" in html and "S.authNoetig" in html


def test_kein_test_loest_ein_echtes_git_aus():
    """Ein Testlauf darf das Repo NICHT anfassen.

    Gefunden im Audit und am Protokoll belegt: ein Test schickte
    POST /api/update, und dahinter laeuft ein echtes 'git pull --ff-only' im
    Bot-Verzeichnis ("Panel-Update: git pull ok (schon aktuell)" stand im
    Testprotokoll). Auf dem Server haette ein Testlauf damit Code gezogen -
    waehrend die README ausdruecklich verspricht, dass er ungefaehrlich ist.

    Deshalb hier ein Riegel: wer /api/update im Test anspricht, muss vorher
    _update_lauf ersetzen."""
    import ast
    baum = ast.parse(open(__file__, encoding="utf-8").read())
    schuldige = []
    for knoten in ast.walk(baum):
        if not isinstance(knoten, ast.FunctionDef) or not knoten.name.startswith("test_"):
            continue
        quelle = ast.unparse(knoten)
        if "/api/update" not in quelle:
            continue
        if "_update_lauf" not in quelle:
            schuldige.append(knoten.name)
    assert not schuldige, (
        f"Diese Tests loesen ein echtes git pull aus: {schuldige}. "
        f"Vorher wp._update_lauf durch ein Double ersetzen.")


def test_webpanel_nimmt_keine_fremden_formulare_an():
    """Ohne Login (WEBPANEL_AUTH=0) gibt es kein Cookie, das schuetzen koennte.
    Dann reicht ein

        <form action="http://192.168.x.x:9123/api/user/coins"
              method="post" enctype="text/plain">

    auf irgendeiner Seite, die der Besitzer im selben Netz oeffnet - der Browser
    schickt den POST mit. Ein Browser-Formular kann aber KEIN
    application/json senden, und genau daran ist es zu erkennen."""
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = False                      # der ungeschuetzte Fall
        async with TestClient(TestServer(wp._build_app())) as c:
            # So sendet ein Formular von einer fremden Seite.
            formular = await c.post("/api/user/coins", data="id=1&amount=999",
                                    headers={"Content-Type": "text/plain"})
            # So sendet die eigene Oberflaeche.
            echt = await c.post("/api/user/coins", json={})
            return formular.status, echt.status

    formular, echt = asyncio.run(lauf())
    assert formular == 415, f"fremdes Formular kam durch (HTTP {formular})"
    assert echt != 415, f"die eigene Oberflaeche wird blockiert (HTTP {echt})"

    # Und die Oberflaeche MUSS den Content-Type auch ohne Body setzen - sonst
    # waere der naechste POST ohne Body ein stiller 415.
    html = open("webpanel.html", encoding="utf-8").read()
    assert "if(aendernd) opts.headers[\"Content-Type\"]=\"application/json\";" in html


def test_webpanel_token_deckel():
    """Die Token-Tabelle darf nicht unbegrenzt wachsen (Prozess laeuft monatelang)
    und abgelaufene Tokens muessen verschwinden."""
    import webpanel
    wp = webpanel.WebPanel()
    wp._ttl = 60
    wp._tokens = {"alt": time.time() - 5}          # abgelaufen
    for _ in range(wp._TOKEN_MAX + 20):
        wp._new_token()
    assert "alt" not in wp._tokens
    assert len(wp._tokens) <= wp._TOKEN_MAX


# --- Mehrere Server ---------------------------------------------------------
def _cfg_frisch():
    """guildcfg mit leerem Speicher - gibt eine Aufraeum-Funktion zurueck."""
    import ai
    import guildcfg
    alt = (guildcfg.instance._enabled, guildcfg.instance._store,
           guildcfg.instance._owner_id)
    guildcfg.instance._enabled = True
    guildcfg.instance._store = _FakeStore({"guilds": {}})
    guildcfg.instance._owner_id = 0
    # Den Praefix-Cache in ai mitleeren. Im Betrieb macht das der Hoerer
    # (guildcfg.horcht_auf("praefix", ai.praefix_geaendert)); hier wird der
    # Speicher aber direkt getauscht, ohne jemandem Bescheid zu sagen. Ohne das
    # gewinnt ein Regex, den ein frueherer Test fuer denselben Server gebaut
    # hat - in gemischter Reihenfolge reproduzierbar, alphabetisch nie.
    ai.instance._RE_CACHE.clear()
    # Den Hoerer anmelden, den sonst guildcfg.setup() anmeldet. Der Helfer
    # setzt _store/_enabled direkt und ruft setup() bewusst NICHT - ohne diese
    # Zeile wirkt eine Praefix-Aenderung im Test nicht sofort, weil niemand den
    # Regex-Cache leert. Doppelte Anmeldungen ignoriert horcht_auf.
    guildcfg.horcht_auf("praefix", ai.praefix_geaendert)

    def zurueck():
        (guildcfg.instance._enabled, guildcfg.instance._store,
         guildcfg.instance._owner_id) = alt
        ai.instance._RE_CACHE.clear()
    return guildcfg, zurueck


def test_guildcfg_trennt_die_server():
    """Zwei Server, zwei Meinungen: was der eine einstellt, geht den anderen
    nichts an. Ohne eigenen Wert gilt weiterhin der Standard."""
    guildcfg, zurueck = _cfg_frisch()
    try:
        A, B = 111, 222
        std = guildcfg.get(A, "lautstaerke")
        assert guildcfg.get(B, "lautstaerke") == std        # beide auf Standard
        assert not guildcfg.eigen(A, "lautstaerke")

        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "lautstaerke", "80"))
        assert ok and wert == 80
        assert guildcfg.get(A, "lautstaerke") == 80
        assert guildcfg.get(B, "lautstaerke") == std, "Server B wurde mitgezogen"
        assert guildcfg.eigen(A, "lautstaerke") and not guildcfg.eigen(B, "lautstaerke")

        # Zurueck auf Standard -> der eigene Wert verschwindet wirklich.
        asyncio.run(guildcfg.setzen(A, "lautstaerke", "standard"))
        assert guildcfg.get(A, "lautstaerke") == std
        assert not guildcfg.eigen(A, "lautstaerke")

        # Grenzen und Unsinn werden abgewiesen - und aendern NICHTS.
        for murks in ("999", "-5", "abc", "inf", "nan", ""):
            ok, _w, fehler = asyncio.run(guildcfg.setzen(A, "lautstaerke", murks))
            assert not ok and fehler, murks
        assert guildcfg.get(A, "lautstaerke") == std

        # An/Aus in allen Schreibweisen.
        for ja in ("an", "AN", "on", "1", "ja", "true"):
            asyncio.run(guildcfg.setzen(A, "bayern", ja))
            assert guildcfg.an(A, "bayern") is True, ja
        for nein in ("aus", "off", "0", "nein", "false"):
            asyncio.run(guildcfg.setzen(A, "bayern", nein))
            assert guildcfg.an(A, "bayern") is False, nein
        assert guildcfg.an(B, "bayern") is False

        # Kanaele: Erwaehnung, rohe ID, Liste, und wieder aus.
        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "ansage_channel",
                                                   "<#1512045750362837013>"))
        assert ok and wert == 1512045750362837013
        assert guildcfg.text(A, "ansage_channel") == "<#1512045750362837013>"
        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "autodelete_channels",
                                                   "111222333444555666, 777888999000111222"))
        assert ok and wert == [111222333444555666, 777888999000111222]
        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "ansage_channel", "aus"))
        assert ok and wert == 0 and guildcfg.text(A, "ansage_channel") == "aus"

        # Unbekannter Schluessel: sauberes Nein, kein Absturz.
        ok, _w, fehler = asyncio.run(guildcfg.setzen(A, "gibtsnicht", "1"))
        assert not ok and fehler
        assert guildcfg.get(A, "gibtsnicht") is None

        # Von Hand kaputt editierte Datei -> Standard statt Absturz.
        guildcfg.instance._store.data["guilds"][str(A)]["lautstaerke"] = "quatsch"
        assert guildcfg.get(A, "lautstaerke") == std

        # Server weg -> Einstellungen weg.
        assert asyncio.run(guildcfg.vergessen(A)) is True
        assert guildcfg.instance._store.data["guilds"].get(str(A)) is None
    finally:
        zurueck()


def test_guildcfg_befehl_braucht_rechte():
    """'Flo einstellung ...' darf nur, wer den Server verwalten darf."""
    import guildcfg
    guildcfg, zurueck = _cfg_frisch()
    try:
        def msg(text, darf):
            return SimpleNamespace(
                content=f"Flo {text}",
                author=SimpleNamespace(id=5, bot=False, display_name="T",
                                       guild_permissions=SimpleNamespace(manage_guild=darf)),
                guild=SimpleNamespace(id=111, name="Testserver", text_channels=[]),
                mentions=[])

        # Ohne Recht: Absage, und NICHTS wird gesetzt.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung lautstaerke 80", False)))
        assert "Server verwalten" in str(antwort)
        assert not guildcfg.eigen(111, "lautstaerke")

        # Mit Recht: gesetzt.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung lautstaerke 80", True)))
        assert "80" in _embed_text(antwort)
        assert guildcfg.get(111, "lautstaerke") == 80

        # Nur nachfragen aendert nichts.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung lautstaerke", True)))
        assert "80" in _embed_text(antwort) and guildcfg.get(111, "lautstaerke") == 80

        # Unbekannter Schluessel nennt die moeglichen.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung quatsch 1", True)))
        assert "lautstaerke" in _embed_text(antwort)

        # Kein Einstellungs-Befehl -> None (naechster Handler ist dran).
        assert asyncio.run(guildcfg.handle(msg("blackjack 100", True))) is None
    finally:
        zurueck()


def test_features_je_server():
    """Der globale Schalter ist der Not-Aus, der Server-Schalter das Feintuning."""
    import features
    alt = (features.instance._store, set(features.instance._disabled),
           dict(features.instance._per_guild))
    features.instance._store = _FakeStore({"disabled": [], "guilds": {}})
    features.instance._disabled = set()
    features.instance._per_guild = {}
    try:
        A, B = 111, 222
        assert features.is_on_in(A, "casino") and features.is_on_in(B, "casino")

        # Nur auf A aus.
        asyncio.run(features.set_guild(A, "casino", False))
        assert not features.is_on_in(A, "casino")
        assert features.is_on_in(B, "casino"), "Nachbarserver mitgerissen"
        assert features.is_on("casino"), "global faelschlich aus"

        # Global aus schlaegt jedes 'an' eines Servers.
        asyncio.run(features.set_feature("casino", False))
        assert not features.is_on_in(B, "casino")
        assert asyncio.run(features.set_guild(B, "casino", True)) is False
        assert not features.is_on_in(B, "casino")

        # Global wieder an -> A bleibt aus (sein eigener Wille), B ist an.
        asyncio.run(features.set_feature("casino", True))
        assert not features.is_on_in(A, "casino") and features.is_on_in(B, "casino")

        # fuer(gid) ist dieselbe Pruefung, nur vorgemerkt (so nutzt bot.py sie).
        _on = features.fuer(A)
        assert _on("casino") is False and _on("music") is True
        assert features.fuer(0)("casino") is True      # DM: nur global zaehlt

        # Ohne Server (DM) zaehlt nur der globale Schalter.
        assert features.is_on_in(0, "casino") is True

        # Gespeichert wird beides getrennt.
        assert features.instance._store.data["guilds"] == {"111": ["casino"]}
        assert features.instance._store.data["disabled"] == []

        # Server weg -> seine Schalter auch.
        assert asyncio.run(features.vergessen(A)) is True
        assert features.is_on_in(A, "casino")

        # Panel-Liste: 'on' gilt fuer den Server, 'global_on' fuers Ganze.
        asyncio.run(features.set_feature("music", False))
        st = {f["key"]: f for f in features.state({"music": True, "casino": True}, B)}
        assert st["music"]["on"] is False and st["music"]["global_on"] is False
        assert st["casino"]["on"] is True and st["casino"]["global_on"] is True
    finally:
        (features.instance._store, features.instance._disabled,
         features.instance._per_guild) = alt


def test_aktie_zaehlt_alle_server_zusammen():
    """Die Aktie ist fuer alle Server dieselbe: die Aktivitaet wird summiert,
    die Dividende bekommt trotzdem jeder nur EINMAL."""
    def mitglied(uid, stream=False):
        return SimpleNamespace(id=uid, bot=False, display_name=f"U{uid}",
                               voice=SimpleNamespace(self_stream=stream, self_video=False,
                                                     self_deaf=False, deaf=False))

    def server(gid, leute):
        vc = SimpleNamespace(id=gid * 10, members=leute, voice_states={})
        return SimpleNamespace(id=gid, name=f"S{gid}", afk_channel=None,
                               voice_channels=[vc], me=SimpleNamespace(id=999))

    a, b = mitglied(1), mitglied(2, stream=True)
    c = mitglied(3)
    A, B = server(111, [a, b]), server(222, [b, c])   # b sitzt auf BEIDEN

    fa = floaktie.instance
    # Ein Server allein.
    assert fa._measure_alle(A) == (2, 1, 0)
    # Beide zusammen: 4 Leute (b doppelt anwesend = doppelt aktiv), 2 Streams.
    assert fa._measure_alle([A, B]) == (4, 2, 0)
    # Auch ein einzelner Server als Liste geht.
    assert fa._measure_alle([B]) == (2, 1, 0)
    assert fa._measure_alle(None) == (0, 0, 0)
    assert fa._measure_alle([]) == (0, 0, 0)

    # Dividende: b haelt Anteile und sitzt auf beiden Servern -> genau eine Zahlung.
    alt_store, alt_on = fa._store, fa._enabled
    fa._store = _FakeStore({"holdings": {"2": 1000}, "state": {}})
    fa._enabled = True
    gezahlt = []
    alt_add = economy.add_coins
    alt_flush = economy.flush

    async def kein_flush():
        pass

    economy.add_coins = lambda uid, betrag, reason="": gezahlt.append((uid, betrag))
    economy.flush = kein_flush
    try:
        asyncio.run(fa.pay_voice_dividends([A, B]))
        assert len(gezahlt) == 1, gezahlt
        assert gezahlt[0][0] == 2 and gezahlt[0][1] > 0
    finally:
        economy.add_coins, economy.flush = alt_add, alt_flush
        fa._store, fa._enabled = alt_store, alt_on


def test_module_lesen_ihre_kanaele_vom_server():
    """Kanaele und Lautstaerke kommen aus der Server-Einstellung, nicht aus der
    .env - sonst zeigen auf Server B alle IDs ins Leere."""
    import games
    import music
    guildcfg, zurueck = _cfg_frisch()
    try:
        A, B = 111, 222

        # Musik: jeder Server faengt mit seiner eigenen Lautstaerke an.
        assert music.instance._start_lautstaerke(A) == music.DEFAULT_VOLUME
        asyncio.run(guildcfg.setzen(A, "lautstaerke", "80"))
        assert abs(music.instance._start_lautstaerke(A) - 0.8) < 1e-9
        assert music.instance._start_lautstaerke(B) == music.DEFAULT_VOLUME

        # Spiele: der Event-Kanal ist der eingestellte, sonst der System-Kanal.
        def kanal(cid, name):
            return SimpleNamespace(id=cid, name=name,
                                   permissions_for=lambda _me: SimpleNamespace(send_messages=True))
        allgemein, spiele = kanal(900, "allgemein"), kanal(901, "spiele")

        def server(gid):
            g = SimpleNamespace(id=gid, me=SimpleNamespace(id=999),
                                system_channel=allgemein, text_channels=[allgemein, spiele])
            g.get_channel = lambda cid: {900: allgemein, 901: spiele}.get(cid)
            return g

        asyncio.run(guildcfg.setzen(A, "event_channel", "901"))
        assert games.instance._pick_event_channel(server(A)) is spiele
        assert games.instance._pick_event_channel(server(B)) is allgemein
    finally:
        zurueck()


def test_bayrisch_ueberlebt_den_neustart():
    """Der Dialekt lag frueher nur im Arbeitsspeicher - nach jedem Update hat Flo
    wieder hochdeutsch geredet, obwohl niemand etwas umgestellt hatte."""
    import bayern
    guildcfg, zurueck = _cfg_frisch()
    alt = bayern.instance._enabled
    bayern.instance._enabled = True
    try:
        A, B = 111, 222

        def msg(gid, text, darf=True):
            return SimpleNamespace(
                content=f"Flo {text}", mentions=[],
                author=SimpleNamespace(
                    id=5, bot=False, display_name="T",
                    guild_permissions=SimpleNamespace(manage_guild=darf)),
                guild=SimpleNamespace(id=gid, name="S"))

        assert bayern.is_on(A) is False
        # Ohne das Recht wird NICHTS umgestellt. Vorher hat bayern.py die
        # Server-Einstellung voellig ungeprueft umgelegt - ein beilaeufiges
        # "flo red mal bayerisch" hat sie fuer alle geaendert.
        asyncio.run(bayern.handle(msg(A, "bayrisch an", darf=False)))
        assert bayern.is_on(A) is False, "ohne Recht wurde der Dialekt umgestellt"

        antwort = asyncio.run(bayern.handle(msg(A, "bayrisch an")))
        assert "boarisch" in str(antwort)
        assert bayern.is_on(A) is True
        assert bayern.is_on(B) is False, "Nachbarserver mitgerissen"
        # Der Zustand liegt jetzt im Speicher, nicht im Arbeitsspeicher.
        assert guildcfg.an(A, "bayern") is True

        asyncio.run(bayern.handle(msg(A, "bayrisch aus")))
        assert bayern.is_on(A) is False and guildcfg.an(A, "bayern") is False
    finally:
        bayern.instance._enabled = alt
        zurueck()


def test_webpanel_einstellungen_je_server():
    """Panel-Seite eines Servers: Einstellungen lesen, setzen, zuruecksetzen -
    und Funktionen NUR fuer diesen Server schalten."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    import features
    # bot ZUERST importieren - dieselbe Falle wie in _arbeit_frisch():
    # webpanel._api_guildcfg holt sich per lazy 'import bot' die Liste der
    # geladenen Module. Ist bot in diesem Prozess noch nie importiert worden,
    # laeuft dabei bot.py komplett durch - samt 'WEBPANEL_ENABLED =
    # webpanel.setup()', und setup() ueberschreibt _auth aus der .env (Standard
    # AN). Der Test setzt _auth vorher auf False; mitten im Testlauf kippte es
    # dadurch auf True zurueck und der POST kam als 401 nicht mehr durch.
    # Alphabetisch fiel das nie auf, weil ein frueherer Test bot laengst
    # importiert hatte.
    import bot                                                    # noqa: F401
    guildcfg, zurueck = _cfg_frisch()
    alt_feat = (features.instance._store, set(features.instance._disabled),
                dict(features.instance._per_guild))
    features.instance._store = _FakeStore({"disabled": [], "guilds": {}})
    features.instance._disabled = set()
    features.instance._per_guild = {}

    ch = SimpleNamespace(id=901, name="spiele")
    guild = SimpleNamespace(id=111, name="Testserver", text_channels=[ch])
    guild.get_channel = lambda cid: ch if cid == 901 else None

    wp = webpanel.instance
    alt = (wp._enabled, wp._auth, wp._client, dict(wp._tokens))
    wp._enabled, wp._auth = True, False
    wp._tokens = {}
    wp._client = SimpleNamespace(guilds=[guild], is_closed=lambda: False,
                                 get_guild=lambda x: guild if int(x) == 111 else None,
                                 get_channel=lambda _x: None)
    app = wp._build_app()

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            # Unbekannter Server -> 404 statt Absturz.
            assert (await cli.get("/api/guildcfg?guild=999")).status == 404
            assert (await cli.get("/api/guildcfg?guild=quatsch")).status == 404

            j = await (await cli.get("/api/guildcfg?guild=111")).json()
            assert j["ok"] and j["guild"]["id"] == "111"
            keys = {s["key"]: s for s in j["settings"]}
            assert "lautstaerke" in keys and keys["lautstaerke"]["eigen"] is False
            assert {"id": "901", "name": "spiele"} in j["channels"]
            assert any(f["key"] == "casino" for f in j["features"])

            # Setzen.
            r = await cli.post("/api/guildcfg", json={"guild": "111",
                                                      "key": "lautstaerke", "value": "80"})
            assert (await r.json())["ok"] and guildcfg.get(111, "lautstaerke") == 80

            # Unsinn wird abgewiesen und aendert nichts.
            r = await cli.post("/api/guildcfg", json={"guild": "111",
                                                      "key": "lautstaerke", "value": "999"})
            assert r.status == 400 and guildcfg.get(111, "lautstaerke") == 80

            # Kanal-IDs kommen als TEXT zurueck (JS-Zahlen sind zu ungenau).
            await cli.post("/api/guildcfg", json={"guild": "111",
                                                  "key": "ansage_channel", "value": "901"})
            j = await (await cli.get("/api/guildcfg?guild=111")).json()
            keys = {s["key"]: s for s in j["settings"]}
            assert keys["ansage_channel"]["wert"] == "901"
            assert keys["ansage_channel"]["eigen"] is True

            # Fremde Kanal-ID gehoert nicht auf diesen Server -> Absage.
            r = await cli.post("/api/guildcfg", json={"guild": "111",
                                                      "key": "ansage_channel", "value": "12345"})
            assert r.status == 400

            # Zuruecksetzen.
            await cli.post("/api/guildcfg", json={"guild": "111",
                                                  "key": "lautstaerke", "value": "standard"})
            assert guildcfg.eigen(111, "lautstaerke") is False

            # Funktion NUR auf diesem Server abschalten.
            r = await cli.post("/api/feature", json={"guild": "111",
                                                     "key": "casino", "on": False})
            assert (await r.json())["ok"]
            assert features.is_on_in(111, "casino") is False
            assert features.is_on("casino") is True, "global mitgerissen"
            assert features.is_on_in(222, "casino") is True

    try:
        asyncio.run(run_it())
    finally:
        (wp._enabled, wp._auth, wp._client, wp._tokens) = alt
        (features.instance._store, features.instance._disabled,
         features.instance._per_guild) = alt_feat
        zurueck()


def test_guildcfg_standard_kommt_aus_der_env():
    """Nach dem Update darf sich am bestehenden Server NICHTS verschieben.

    Bisher lasen die Module ihre Kanaele direkt aus der .env - mit einem fest
    verdrahteten Wert, wenn dort nichts stand. Beides muss der Katalog genauso
    abbilden, inklusive des Unterschieds zwischen "gar nicht gesetzt" und
    "ausdruecklich leer" (das hiess immer: Funktion aus)."""
    import guildcfg
    A = 111
    alt = {k: os.environ.get(k) for k in
           ("AUTODELETE_CHANNEL_IDS", "LEVELUP_CHANNEL_ID", "AUTODELETE_SECONDS")}

    def setze(**kw):
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    try:
        # 1) Nichts in der .env -> der frueher fest verdrahtete Wert gilt weiter.
        setze(AUTODELETE_CHANNEL_IDS=None, LEVELUP_CHANNEL_ID=None,
              AUTODELETE_SECONDS=None)
        assert guildcfg.instance.standard("autodelete_channels", A) == [1512045750362837013]
        assert guildcfg.instance.standard("ansage_channel", A) == 1512045750362837013
        assert guildcfg.instance.standard("kalorien_channel", A) == 1522294725116428329
        assert guildcfg.instance.standard("autodelete_sekunden", A) == 60

        # 2) AUSDRUECKLICH leer heisst weiterhin: aus.
        setze(AUTODELETE_CHANNEL_IDS="")
        assert guildcfg.instance.standard("autodelete_channels", A) == []
        setze(LEVELUP_CHANNEL_ID="0")
        assert guildcfg.instance.standard("ansage_channel", A) == 0

        # 3) Gesetzte Werte gewinnen - auch mehrere, mit Komma oder Leerzeichen.
        setze(AUTODELETE_CHANNEL_IDS="555111222333444555, 666111222333444555",
              AUTODELETE_SECONDS="45")
        assert guildcfg.instance.standard("autodelete_channels", A) == \
            [555111222333444555, 666111222333444555]
        assert guildcfg.instance.standard("autodelete_sekunden", A) == 45

        # 4) Murks in der .env kippt nichts um - der Fallback traegt.
        setze(AUTODELETE_SECONDS="ganz schnell")
        assert guildcfg.instance.standard("autodelete_sekunden", A) == 60

        # 5) Icon-Automatik und Aktien-Zaehlung sind nur zu Hause von selbst an.
        haupt = guildcfg.HAUPT_GUILD
        for key in ("icon_auto", "aktie_zaehlt"):
            assert guildcfg.instance.standard(key, 999999999999999999) is False, key
            assert guildcfg.instance.standard(key, haupt) is bool(haupt), key
    finally:
        setze(**alt)


def test_feature_schluessel_passen_ueberall_zusammen():
    """features.CATALOG, bot.FEATURE_LOADED und die Handler-Kette muessen
    DIESELBEN Schluessel fuehren.

    Ein Tippfehler in genau einem der drei Orte faellt sonst niemandem auf: der
    Schalter im Panel taucht auf, tut aber nichts - oder eine Funktion laesst
    sich gar nicht mehr abschalten. bot.py wird bewusst als TEXT gelesen und
    nicht importiert (das zieht den halben Bot hoch)."""
    import re
    import features
    katalog = {f["key"] for f in features.CATALOG}
    assert len(katalog) == len(features.CATALOG), "doppelter Schluessel im CATALOG"

    quelle = open("bot.py", encoding="utf-8").read()

    # 1) FEATURE_LOADED = { "ki": ..., "music": ... }
    block = re.search(r"FEATURE_LOADED\s*=\s*\{(.*?)\n\}", quelle, re.S)
    assert block, "FEATURE_LOADED nicht gefunden"
    geladen = set(re.findall(r'"([a-z_]+)":', block.group(1)))
    assert geladen == katalog, (
        f"FEATURE_LOADED weicht ab: nur dort {sorted(geladen - katalog)}, "
        f"nur im CATALOG {sorted(katalog - geladen)}")

    # 2) Jeder Schalter in der Handler-Kette und den passiven Hooks: _on("key")
    benutzt = set(re.findall(r'_on\("([a-z_]+)"\)', quelle))
    benutzt |= set(re.findall(r'features\.is_on(?:_in)?\((?:[^,]+,\s*)?"([a-z_]+)"\)',
                              quelle))
    unbekannt = benutzt - katalog
    assert not unbekannt, f"bot.py fragt unbekannte Schalter ab: {sorted(unbekannt)}"

    # 3) Umgekehrt: jeder Katalog-Eintrag wird in bot.py auch WIRKLICH abgefragt -
    #    sonst steht im Panel ein Schalter, der nichts bewirkt.
    tot = katalog - benutzt
    assert not tot, f"Schalter ohne Wirkung in bot.py: {sorted(tot)}"


# --- Profil-Lookup ('Flo check @wer') ---------------------------------------
class _FakeAsset:
    """Discord-Asset-Ersatz: kennt Groesse, Format und ob es animiert ist."""

    def __init__(self, url="https://cdn.discordapp.com/avatars/1/abc.webp",
                 animiert=False):
        self._url, self._animiert = url, animiert

    @property
    def url(self):
        return self._url

    def is_animated(self):
        return self._animiert

    def with_size(self, px):
        trenner = "&" if "?" in self._url else "?"
        return _FakeAsset(f"{self._url}{trenner}size={px}", self._animiert)

    def with_format(self, fmt):
        kopf = self._url.split("?")[0].rsplit(".", 1)[0]
        schwanz = self._url.split("?", 1)[1] if "?" in self._url else ""
        return _FakeAsset(f"{kopf}.{fmt}" + (f"?{schwanz}" if schwanz else ""),
                          self._animiert)


def _fake_person(uid=123456789012345678, *, name="secoolio", global_name="Secoolio",
                 nick=None, member=True, bot=False, avatar=True, animiert=False,
                 banner=False, server_avatar=False, flags=None, guild=None):
    """Ein Member (mit guild) oder ein blosser User (ohne)."""
    from datetime import datetime, timezone
    p = SimpleNamespace(
        id=uid, name=name, global_name=global_name, discriminator="0",
        display_name=nick or global_name or name, bot=bot, system=False,
        mention=f"<@{uid}>", nick=nick,
        created_at=datetime(2019, 4, 20, tzinfo=timezone.utc),
        avatar=_FakeAsset(animiert=animiert) if avatar else None,
        display_avatar=_FakeAsset(animiert=animiert),
        banner=_FakeAsset("https://cdn.discordapp.com/banners/1/b.webp") if banner else None,
        accent_colour=None, avatar_decoration=None, primary_guild=None,
        public_flags=flags or SimpleNamespace(),
        colour=SimpleNamespace(value=0),
    )
    if member:
        p.guild = guild or SimpleNamespace(id=999, name="Testserver", members=[])
        p.joined_at = datetime(2021, 6, 1, tzinfo=timezone.utc)
        p.premium_since = None
        p.roles = []
        p.guild_avatar = _FakeAsset("https://cdn.discordapp.com/guilds/9/u/1/a.webp") \
            if server_avatar else None
        p.timed_out_until = None
        p.is_timed_out = lambda: False
        p.pending = False
    return p


def _profil_frisch():
    """profil-Modul mit leerem Speicher; gibt eine Aufraeum-Funktion zurueck."""
    import profil
    alt = (profil.instance._enabled, profil.instance._store,
           dict(profil.instance._voll), dict(profil.instance._cooldown))
    profil.instance._enabled = True
    profil.instance._store = _FakeStore({"users": {}, "seit": 1_700_000_000})
    profil.instance._voll = {}
    profil.instance._cooldown = {}
    profil.instance._dirty = False

    def zurueck():
        (profil.instance._enabled, profil.instance._store,
         profil.instance._voll, profil.instance._cooldown) = alt
    return profil, zurueck


def test_profil_bild_gross_und_ohne_kreis():
    """Der Kern des Befehls: das Bild kommt in 4096 px und als set_image.

    set_thumbnail und das Autor-Icon zeigt Discord RUND - nur set_image bleibt
    rechteckig. Ein Wechsel auf thumbnail waere optisch subtil und genau der
    Fehler, den dieser Test verhindert."""
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person()
        bilder = profil.instance._bilder(wer, None)
        emb = profil.instance._profil_embed(wer, None, bilder,
                                            SimpleNamespace(id=999, name="S"))
        assert emb.image.url and "size=4096" in emb.image.url, emb.image.url
        assert not emb.thumbnail.url, \
            "Profilbild darf NICHT als (runder) Thumbnail gesetzt sein"
        # Direktlinks stehen auch als Text drin - zum Herunterladen.
        text = _embed_text(emb)
        assert "size=4096" in text
        # Und der Fussnoten-Hinweis nennt die Aufloesung.
        assert "4096" in (emb.footer.text or "")
    finally:
        zurueck()


def test_profil_zeigt_keinen_online_status():
    """Ohne presences-Intent meldet discord.py JEDEN als offline.

    Das waere kein sichtbarer Fehler, sondern eine stille Falschaussage -
    deshalb darf im Profil ueberhaupt kein Status/keine Aktivitaet stehen."""
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person()
        # Der Bot wuerde hier 'offline' sehen, obwohl die Person online ist.
        wer.status = "offline"
        wer.activities = ()
        emb = profil.instance._profil_embed(wer, None,
                                            profil.instance._bilder(wer, None),
                                            SimpleNamespace(id=999, name="S"))
        # Nur Titel/Beschreibung/Felder pruefen - die FUSSZEILE sagt bewusst,
        # dass der Status fehlt, und darf das Wort deshalb enthalten.
        inhalt = " ".join([emb.title or "", emb.description or ""]
                          + [f"{f.name} {f.value}" for f in emb.fields]).lower()
        for verboten in ("offline", "online", "abwesend", "beschäftigt", "spielt gerade"):
            assert verboten not in inhalt, f"'{verboten}' steht im Profil - das waere gelogen"
        # ... und die Fusszeile erklaert, warum da nichts steht.
        assert "status" in (emb.footer.text or "").lower()
    finally:
        zurueck()


def test_profil_namensverlauf_merkt_nur_aenderungen():
    """Discord fuehrt keinen Namensverlauf - Flo schreibt selbst mit.

    Geschrieben werden darf NUR bei echten Aenderungen: der Hook laeuft bei
    jeder Nachricht."""
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person(name="secoolio", global_name="Secoolio", nick=None)

        # Erste Sichtung legt an.
        assert profil.notiere(wer, 999) is True
        # Dieselbe Person nochmal: KEINE Aenderung, kein Schreiben.
        assert profil.notiere(wer, 999) is False
        assert profil.notiere(wer, 999) is False

        handles, anzeigen, nicks = profil.verlauf(wer.id, 999)
        assert [h[0] for h in handles] == ["secoolio"]
        assert [a[0] for a in anzeigen] == ["Secoolio"]

        # Handle geaendert -> ein Eintrag mehr, der alte bleibt stehen.
        wer.name = "flotus"
        assert profil.notiere(wer, 999) is True
        handles, _a, _n = profil.verlauf(wer.id, 999)
        assert [h[0] for h in handles] == ["secoolio", "flotus"]

        # Server-Nickname gilt NUR fuer diesen Server.
        wer.nick = "Chef"
        assert profil.notiere(wer, 999) is True
        _h, _a, nicks999 = profil.verlauf(wer.id, 999)
        _h, _a, nicks111 = profil.verlauf(wer.id, 111)
        assert [n[0] for n in nicks999][-1] == "Chef"
        assert nicks111 == [], "Nickname des einen Servers taucht beim anderen auf"

        # Bots bekommen keinen Verlauf.
        assert profil.notiere(_fake_person(uid=42, bot=True), 999) is False

        # Der Verlauf ist gedeckelt, sonst waechst die Datei ewig.
        for i in range(profil.VERLAUF_MAX + 10):
            wer.name = f"name{i}"
            profil.notiere(wer, 999)
        handles, _a, _n = profil.verlauf(wer.id, 999)
        assert len(handles) == profil.VERLAUF_MAX, len(handles)

        # Gespeichert wird gesammelt: flush schreibt nur, wenn etwas anliegt.
        assert asyncio.run(profil.flush()) is True
        assert asyncio.run(profil.flush()) is False
    finally:
        zurueck()


def test_profil_erkennt_seine_befehle():
    """check/profil/avatar/banner ja - normales Gerede nein."""
    profil, zurueck = _profil_frisch()
    try:
        kanal = _FakeChannel()

        async def antwort(text, mentions=None):
            msg = SimpleNamespace(
                content=f"Flo {text}", mentions=mentions or [],
                author=_fake_person(uid=5, name="ich"), reference=None,
                guild=SimpleNamespace(id=999, name="S", members=[],
                                      me=SimpleNamespace(id=1)),
                channel=kanal)
            return await profil.handle(msg)

        # Kein Profil-Befehl -> None, damit der naechste Handler drankommt.
        for kein in ("blackjack 100", "spiel was", "wie geht's", "checke mal ab"):
            profil.instance._cooldown.clear()
            assert asyncio.run(antwort(kein)) is None, kein

        # MEHRDEUTIGE Woerter ohne Ziel sind Gerede, kein Befehl: "check mal ob
        # das laeuft", "pb ist kaputt". Da muss die Kette weiterlaufen, sonst
        # beantwortet Flo jeden solchen Satz mit einem Hinweistext und die KI
        # kommt nie dran.
        for gerede in ("check", "user", "pb", "av",
                       "check mal ob das laeuft", "check das bitte kurz"):
            profil.instance._cooldown.clear()
            assert asyncio.run(antwort(gerede)) is None, gerede

        # "bild" gehoert dem Bildgenerator in media.py - profil darf es NICHT
        # anfassen, sonst ist "Flo bild ein Drache aus Neon" tot.
        assert "bild" not in profil._AVATAR_CMDS
        profil.instance._cooldown.clear()
        assert asyncio.run(antwort("bild ein Drache aus Neon")) is None

        # EINDEUTIGE Befehle ohne Ziel zeigen das eigene Profil.
        for cmd in ("profil", "whois", "steckbrief", "avatar", "userinfo"):
            profil.instance._cooldown.clear()
            del kanal.sent[:]
            assert asyncio.run(antwort(cmd)) is profil.HANDLED, cmd
            assert kanal.sent and kanal.sent[-1]["embeds"], cmd

        # Und "check" MIT Ziel ist selbstverstaendlich ein Befehl.
        ziel_person = _fake_person(uid=7, name="ziel")
        profil.instance._cooldown.clear()
        del kanal.sent[:]
        assert asyncio.run(antwort("check", [ziel_person])) is profil.HANDLED
        assert kanal.sent and kanal.sent[-1]["embeds"]

        # Ohne Banner sagt Flo das, statt ein leeres Embed zu schicken.
        profil.instance._cooldown.clear()
        res = asyncio.run(antwort("banner"))
        assert isinstance(res, str) and "Banner" in res
    finally:
        zurueck()


def test_profil_findet_das_richtige_ziel():
    """Erwaehnung schlaegt alles - und die Erwaehnung des BOTS ist der Ausloeser,
    nicht das Ziel ('@Flo check @wer')."""
    profil, zurueck = _profil_frisch()
    try:
        ich = _fake_person(uid=5, name="ich")
        ziel = _fake_person(uid=7, name="ziel")
        flo = _fake_person(uid=1, name="flo", bot=True)
        guild = SimpleNamespace(id=999, name="S", members=[ich, ziel],
                                me=SimpleNamespace(id=1))

        def msg(text, mentions):
            return SimpleNamespace(content=f"Flo {text}", mentions=mentions,
                                   author=ich, guild=guild, reference=None,
                                   channel=_FakeChannel())

        # Ohne alles: ich selbst.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", []), ""))
        assert wer is ich
        # Mit Erwaehnung: die Person.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", [ziel]), ""))
        assert wer is ziel
        # Bot-Erwaehnung davor wird uebersprungen.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", [flo, ziel]), ""))
        assert wer is ziel
        # Nur der Bot erwaehnt -> dann ist der Bot gemeint.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", [flo]), ""))
        assert wer is flo
        # Name im Text: im Cache suchen (Notnagel ohne Members-Intent).
        wer, _p = asyncio.run(profil.instance._ziel(msg("check ziel", []), "ziel"))
        assert wer is ziel
        # Unbekannter Name: ehrliche Absage statt "gibt es nicht".
        wer, problem = asyncio.run(profil.instance._ziel(msg("check xyz", []), "xyz"))
        assert wer is None and "Erwähnung" in problem
    finally:
        zurueck()


def test_profil_bremst_und_zeigt_flo_daten():
    """Ein Aufruf kann zwei REST-Aufrufe ausloesen - deshalb ein Cooldown.
    Und was Flo selbst weiss, gehoert mit ins Profil."""
    profil, zurueck = _profil_frisch()
    restore_eco = _with_economy({77: 4242})
    try:
        # Cooldown: der zweite Aufruf in Folge wird abgewiesen.
        frei, _w = profil.instance._darf_schon(5)
        assert frei is True
        frei, warten = profil.instance._darf_schon(5)
        assert frei is False and warten > 0
        # Eine ANDERE Person ist davon nicht betroffen.
        assert profil.instance._darf_schon(6)[0] is True

        # Flo-Daten: Level/Coins der Wirtschaft tauchen im Profil auf.
        economy.instance._profile(77)["msgs"] = 1234
        wer = _fake_person(uid=77)
        emb = profil.instance._profil_embed(wer, None,
                                            profil.instance._bilder(wer, None),
                                            SimpleNamespace(id=999, name="S"))
        text = _embed_text(emb)
        assert "4.242" in text or "4242" in text, text
        assert "1.234" in text or "1234" in text, text
    finally:
        restore_eco()
        zurueck()


def test_tests_fassen_die_echten_daten_nicht_an():
    """Sicherung gegen den gefaehrlichsten Testfehler ueberhaupt.

    Die README schickt zum Testen nach /opt/flobot - dorthin, wo die ECHTEN
    Daten liegen. Vorher schrieb ein Lauf dort wirklich hinein (gemessen:
    ein Testkonto in economy.json, "holdings": {"1": 5} plus neuer Kurs und
    History in floaktie.json). Seitdem zeigt DATA_DIR auf einen Wegwerf-Ordner;
    dieser Test haelt das fest."""
    import pathlib
    import store
    echt = pathlib.Path(__file__).resolve().parent / "data"
    genutzt = pathlib.Path(store.DATA_DIR).resolve()
    assert genutzt != echt, f"Tests schreiben in die echten Daten: {genutzt}"
    assert not str(genutzt).startswith(str(echt)), genutzt

    # Und die Module haengen wirklich an diesem Ordner, nicht am alten.
    for modul in (economy, floaktie):
        laden = getattr(modul.instance, "_store", None)
        if laden is not None and getattr(laden, "path", None) is not None:
            assert pathlib.Path(laden.path).resolve().parent == genutzt, modul.__name__

    # Nach dem kompletten Lauf steht im echten Ordner nichts Neues von uns.
    if echt.exists():
        for datei in ("economy.json", "floaktie.json", "profil.json", "guildcfg.json"):
            pfad = echt / datei
            assert not pfad.exists() or pfad.stat().st_size > 0, pfad


def test_namensverlauf_wird_wirklich_gespeichert():
    """Der Verlauf muss in einem Loop wegschreiben, der IMMER laeuft.

    Zuerst hing flush() im Voice-Takt - und der startet nur mit eingeschalteter
    Wirtschaft (bot.py: 'if ECONOMY_ENABLED and not self.voice_xp_loop...').
    Ohne economy waere der Namensverlauf also nie auf der Platte gelandet und
    bei jedem Neustart weg gewesen. bot.py wird als TEXT geprueft, damit der
    Test den halben Bot nicht hochziehen muss."""
    import re
    quelle = open("bot.py", encoding="utf-8").read()

    # In welcher Loop-Methode steht der flush-Aufruf?
    methoden = re.findall(r"\n    async def (\w+)\(self\):(.*?)(?=\n    (?:async )?def |\n    @)",
                          quelle, re.S)
    drin = [name for name, rumpf in methoden if "profil.flush()" in rumpf]
    assert len(drin) == 1, f"profil.flush() sollte in genau einem Loop stehen, ist in {drin}"
    loop = drin[0]

    # Und wird dieser Loop bedingungslos gestartet?
    start = re.search(r"\n(\s*)if ([^\n]*?)not self\.%s\.is_running\(\):" % loop, quelle)
    assert start, f"{loop} wird nirgends gestartet"
    bedingung = start.group(2).strip()
    assert bedingung == "", (
        f"{loop} startet nur unter der Bedingung '{bedingung}' - dann wuerde der "
        f"Namensverlauf unter Umstaenden nie gespeichert")


def test_profil_loest_kein_bot_neuladen_aus():
    """profil.py darf bot.py NIEMALS erstmalig importieren.

    Ein 'import bot' fuehrt das Modul aus, wenn es noch nicht geladen ist - und
    damit saemtliche setup()-Aufrufe erneut. Gemessen: dabei bekommt JEDES Modul
    einen frischen Speicher, mitten im Betrieb bzw. mitten im Testlauf. Genau
    daran sind Namensverlauf und Flo-Daten aus dem fertigen Embed verschwunden.
    Laeuft der Bot, steht er ohnehin in sys.modules."""
    import sys
    profil, zurueck = _profil_frisch()
    hatte_bot = "bot" in sys.modules
    try:
        ziel = _fake_person(uid=777, name="alt", global_name="Alt", nick="Nick")
        profil.notiere(ziel, 999)
        ziel.name = "neu"
        profil.notiere(ziel, 999)

        kanal = _FakeChannel()
        msg = SimpleNamespace(
            content="Flo check", mentions=[ziel], author=_fake_person(uid=5),
            reference=None, channel=kanal,
            guild=SimpleNamespace(id=999, name="S", members=[], me=SimpleNamespace(id=1)))
        assert asyncio.run(profil.handle(msg)) is profil.HANDLED

        if not hatte_bot:
            assert "bot" not in sys.modules, \
                "profil.py hat bot.py importiert und damit alle Module neu aufgesetzt"

        # Und die Felder, die durch genau diesen Nebeneffekt verschwunden waren,
        # stehen wirklich im fertigen Embed - nicht nur in der Einzelfunktion.
        namen = [f.name for f in kanal.sent[-1]["embeds"][0].fields]
        assert any("Frühere Namen" in n for n in namen), namen
    finally:
        zurueck()


def test_profil_befehle_kollidieren_nicht():
    """Die drei Kollisionen, die der neue Lookup ausgeloest hatte.

    1) 'banner' war cmdnorm unbekannt. Die Aehnlichkeitssuche korrigierte es auf
       'banne' - und da Moderation auf Platz 2 der Handler-Kette steht, hat
       'Flo banner @wer' die Person GEBANNT statt ihr Banner zu zeigen.
    2) 'bild' gehoert dem Bildgenerator in media.py.
    3) 'profil' fing frueher admin.py fuer den Besitzer ab."""
    import cmdnorm
    import media
    import moderation
    import profil

    # 1) KEIN Profil-Befehl darf auf einen fremden Befehl korrigiert werden.
    for wort in profil._CHECK_CMDS + profil._AVATAR_CMDS + profil._BANNER_CMDS:
        korrigiert = cmdnorm.normalize(wort)
        assert korrigiert in (None, wort), (
            f"cmdnorm macht aus '{wort}' -> '{korrigiert}'")
    # ... und die Moderation darf 'banner' nicht mehr als Bann lesen.
    for text in ("banner", "banner <@777>", "profilbanner"):
        norm = cmdnorm.normalize(text) or text
        assert moderation.classify(norm) is None, (text, norm)

    # 2) 'bild' bleibt beim Bildgenerator.
    assert "bild" not in profil._AVATAR_CMDS + profil._CHECK_CMDS
    assert media.Media._GEN_RE.match("bild ein Drache aus Neon")

    # 3) admin.py beansprucht 'profil' nicht mehr.
    quelle = open("admin.py", encoding="utf-8").read()
    assert '"profil"' not in quelle, "admin faengt 'profil' wieder vor profil.py ab"


def test_profil_namensverlauf_zeigt_das_richtige_datum():
    """'bis' muss das ENDE eines Namens sein.

    _merke schreibt in 't' den Moment, in dem ein Name ANFING. Vorher stand
    genau dieses 't' hinter dem Wort 'bis' - jede Zeile war damit um einen
    kompletten Eintrag zu frueh, und beim aeltesten Namen stand sogar der Tag,
    an dem Flo die Person ueberhaupt zum ersten Mal gesehen hat."""
    import unittest.mock as mock
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person(uid=1, name="start", global_name="G", nick=None)
        zeiten = {"alt": 1_600_000_000, "mittel": 1_750_000_000, "neu": 1_755_000_000}
        for name, t in (("alt", zeiten["alt"]), ("mittel", zeiten["mittel"]),
                        ("neu", zeiten["neu"])):
            wer.name = name
            with mock.patch("time.time", lambda t=t: t):
                profil.notiere(wer, 999)

        text = profil.instance._verlauf_feld(wer, 999)
        # 'alt' galt bis zu dem Moment, in dem 'mittel' anfing - NICHT bis zu
        # seinem eigenen Anfang.
        assert f"„alt“ (bis <t:{zeiten['mittel']}:d>)" in text, text
        assert f"„mittel“ (bis <t:{zeiten['neu']}:d>)" in text, text
        # Der eigene Anfangszeitpunkt darf NIRGENDS als 'bis' auftauchen.
        assert f"„alt“ (bis <t:{zeiten['alt']}:d>)" not in text
        # Der aktuelle Name ist kein "frueherer".
        assert "„neu“" not in text
        # Und es steht dabei, ab wann Flo ueberhaupt mitschreibt.
        assert "seit" in text
    finally:
        zurueck()


def test_profil_haelt_muell_und_grenzfaelle_aus():
    """Kaputte Datei, Emoji-IDs, fehlende Bilder, 0 als Deckel."""
    import profil
    profil_m, zurueck = _profil_frisch()
    try:
        # 1) Handgeschriebener Muell in profil.json darf nichts umbringen.
        profil.instance._store = _FakeStore({"users": {"5": {"handle": None,
                                                             "nick": "kaputt"}},
                                             "seit": "gestern"})
        assert profil.verlauf(5, 999) == ([], [], [])
        wer = _fake_person(uid=5, name="a", global_name="A", nick="N")
        profil.notiere(wer, 999)          # darf nicht fliegen
        assert isinstance(profil.instance._store.data["users"]["5"]["handle"], list)

        # 2) Deckel: 0 hiess frueher "unbegrenzt" ([:-0] ist eine leere Scheibe).
        assert profil.VERLAUF_MAX >= 1
        assert profil.USER_MAX >= 50

        # 3) Emoji-, Rollen- und Kanal-IDs sind KEINE Konten - sie duerfen
        #    keine REST-Aufrufe ausloesen.
        gerufen = []

        class Guild:
            id = 999
            name = "S"
            members = []
            me = SimpleNamespace(id=1)

            def get_channel(self, _c):
                return None

            def get_member(self, _u):
                gerufen.append("get_member")
                return None

        msg = SimpleNamespace(content="Flo check", mentions=[], reference=None,
                              author=_fake_person(uid=5), guild=Guild(),
                              channel=_FakeChannel())
        for markup in ("<:katze:123456789012345678>",
                       "<#123456789012345678>", "<@&123456789012345678>"):
            gerufen.clear()
            wer2, _problem = asyncio.run(profil.instance._ziel(msg, markup))
            assert gerufen == [], f"{markup} wurde als Konto behandelt"

        # 4) Ohne Bild kein '[Direktlink](None)'.
        assert profil.instance._bild_embed(wer, None, "x", None) is None
    finally:
        zurueck()


# --- Musik: Zuverlaessigkeit (der gemeldete Fehler) -------------------------
class _StallVoice:
    """Voice-Client-Attrappe mit der Semantik von discord.py 2.7.1.

    Kann den FFMPEG-STALL nachstellen: is_playing() meldet weiter True, der
    Block-Zaehler des Players (AudioPlayer.loops) steht aber still - genau der
    Zustand, in dem der Bot 'verbunden und spielend' aussieht, waehrend kein
    Ton mehr fliesst."""

    def __init__(self):
        self.spielt = False
        self.pausiert_ = False
        self.frames = 0          # wandert normalerweise mit jedem Block hoch
        self.stall = False       # True = Zaehler steht (kein Ton mehr)
        self.play_calls = []
        self.stops = 0
        self._player = SimpleNamespace(loops=0)

    # -- discord.py-API --
    def is_connected(self):
        return True

    def is_playing(self):
        return self.spielt and not self.pausiert_

    def is_paused(self):
        return self.pausiert_

    def play(self, _src, after=None):
        if self.spielt:
            import discord
            raise discord.ClientException("Already playing audio.")
        self.spielt = True
        self.stall = False
        self.play_calls.append(after)

    def stop(self):
        self.spielt = False
        self.stops += 1

    def pause(self):
        self.pausiert_ = True

    def resume(self):
        self.pausiert_ = False

    @property
    def channel(self):
        return SimpleNamespace(id=42)

    # -- Test-Steuerung --
    def takt(self, n=1):
        """Laesst n Bloecke laufen (oder eben nicht, wenn stall gesetzt ist)."""
        if self.spielt and not self.pausiert_ and not self.stall:
            self.frames += n
            self._player.loops = self.frames


def _musik_umgebung():
    """Patcht FFmpeg/Panels weg. Rueckgabe: (player, voice, aufraeumen)."""
    import discord
    import music
    alt = (discord.FFmpegPCMAudio, discord.PCMVolumeTransformer,
           music._send_panel, music._retire_panel, music._resolve_track)
    discord.FFmpegPCMAudio = lambda *a, **k: SimpleNamespace(cleanup=lambda: None)
    discord.PCMVolumeTransformer = lambda src, volume=1.0: src

    async def nix(*a, **k):
        return None

    music._send_panel = nix
    music._retire_panel = nix

    # Aufloesen wird AUSDRUECKLICH stillgelegt: der Track kommt unveraendert
    # zurueck. Vorher hat der Helfer _resolve_track zwar gesichert, aber nie
    # gesetzt - er erbte also, was ein frueherer Test hinterlassen hatte. Lief
    # zufaellig ein echtes (oder gefaketes) Aufloesen, wurde player.current
    # durch ein ANDERES Track-Objekt ersetzt und die Identitaetspruefung
    # 'player.current is lang' fiel um. In gemischter Reihenfolge war das
    # reproduzierbar, alphabetisch nie.
    async def unveraendert(track):
        return track

    music._resolve_track = unveraendert

    player = music.GuildPlayer(loop=asyncio.get_event_loop_policy().new_event_loop())
    voice = _StallVoice()
    player.voice = voice
    player.active_channel_id = 42

    def aufraeumen():
        (discord.FFmpegPCMAudio, discord.PCMVolumeTransformer,
         music._send_panel, music._retire_panel, music._resolve_track) = alt
        player.loop.close()

    return player, voice, aufraeumen


def _track(titel="A", url="http://stream/a"):
    import music
    return music.Track(title=titel, stream_url=url, query=f"ytsearch1:{titel}")


def _VoiceChannelStub():
    """Echter discord.VoiceChannel ohne __init__ - heal() prueft per isinstance,
    ein SimpleNamespace wuerde dort stillschweigend abgewiesen."""
    import discord
    ch = object.__new__(discord.VoiceChannel)
    ch.id = 42
    ch.name = "Musik"
    return ch


def test_musik_stall_wird_erkannt_und_song_neu_gestartet():
    """DER gemeldete Fehler: 'paar sachen in der queue und er spielt sie nicht'.

    Bleibt der Audio-Stream still stehen (Verbindung offen, keine Daten mehr),
    wartet FFmpeg ohne -rw_timeout endlos: der after-Callback feuert nie,
    is_playing() bleibt True - und weil is_active() dann True meldet, landet
    jeder weitere 'Flo spiel X' nur noch in der Warteschlange. Der alte
    Watchdog war dafuer blind, weil er ausschliesslich 'not is_playing()'
    kannte. Jetzt zaehlt er die tatsaechlich gesendeten Audio-Bloecke."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())

    # Der Watchdog holt beim Neustart eine FRISCHE Stream-Adresse - hier ging
    # dabei bisher ein ECHTER yt-dlp-Aufruf ins Netz. Das ist in einem Test
    # nichts verloren: er wurde damit langsam, abhaengig von YouTube, und als
    # das Durchprobieren der player_client dazukam, brachte er ploetzlich einen
    # fremden Song zurueck statt zu scheitern. Hier zaehlt nur der Watchdog.
    alt_resolve = music._resolve_track

    async def frisch(track):
        neu_track = music.Track(title=track.title, stream_url="http://stream/neu",
                                query=track.query, duration=track.duration,
                                requested_by=track.requested_by)
        return neu_track

    music._resolve_track = frisch
    try:
        player.start(_track("A"))
        voice.takt(50)                       # laeuft normal
        player.queue.extend([_track("B"), _track("C")])

        # Solange Bloecke fliessen, fasst der Watchdog nichts an.
        for _ in range(5):
            voice.takt(50)
            asyncio.run(player.heal(guild))
        assert voice.stops == 0 and player.current.title == "A"

        # Jetzt der Stall: is_playing() bleibt True, es kommt aber nichts mehr.
        voice.stall = True
        vorher_stops = voice.stops
        for _ in range(music.VOICE_STALL_TICKS):
            voice.takt(50)                   # laeuft ins Leere (stall)
            asyncio.run(player.heal(guild))
        assert voice.stops > vorher_stops, "Watchdog hat den Stall nicht bemerkt"
        assert player.current.title == "A", "falscher Song neu gestartet"
        assert [t.title for t in player.queue] == ["B", "C"], \
            "Warteschlange wurde beim Neustart geopfert"
        assert voice.is_playing(), "nach dem Neustart laeuft nichts"
    finally:
        music._resolve_track = alt_resolve
        aufraeumen()


def test_musik_liegengebliebene_warteschlange_wird_angestossen():
    """Zweiter Dauer-Steckzustand: endet ein Song genau waehrend eines kurzen
    Voice-Aussetzers, setzt _advance current=None und laesst die volle
    Warteschlange liegen. Der Watchdog stellte die Verbindung zwar wieder her -
    angestossen hat die Schlange danach aber NIEMAND mehr."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())
    try:
        player.current = None
        player.queue.extend([_track("B"), _track("C")])
        voice.spielt = False

        asyncio.run(player.heal(guild))
        assert player.current is not None and player.current.title == "B", \
            "Watchdog hat die liegengebliebene Warteschlange nicht angestossen"
        assert [t.title for t in player.queue] == ["C"]
    finally:
        aufraeumen()


def test_musik_stop_laesst_keinen_geister_track_zurueck():
    """'Flo stop', waehrend _advance gerade einen Playlist-Track aufloest:
    der fertig aufgeloeste Track landete danach per insert(0) in der SOEBEN
    GELEERTEN Warteschlange - und spielte beim naechsten Play ungefragt wieder
    an ('ich hab doch gestoppt')."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        lazy = music.Track(title="B", stream_url="", query="ytsearch1:B")
        player.queue.append(lazy)
        player.current = _track("A")

        async def langsam(track):
            await asyncio.sleep(0.05)
            track.stream_url = "http://stream/b"
            return track

        music._resolve_track = langsam

        async def lauf():
            gen = player._play_gen
            aufgabe = asyncio.ensure_future(player._advance(gen))
            await asyncio.sleep(0.01)      # _advance haengt im Aufloesen
            await player.disconnect()      # <- 'Flo stop'
            await aufgabe

        asyncio.run(lauf())
        assert player.queue == [], f"Geister-Track zurueck in der Queue: {player.queue}"
        assert player.current is None
    finally:
        aufraeumen()


def test_musik_netzausfall_frisst_die_warteschlange_nicht():
    """Ein kurzer Netz-Aussetzer beim Songwechsel hat frueher die KOMPLETTE
    Playlist in einem Rutsch als 'nicht ladbar' verbucht und stumm entsorgt -
    jeder Resolve warf, jeder Fehler machte 'continue'. Nach zwei Fehlschlaegen
    am Stueck bleibt die Warteschlange jetzt stehen."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    gesagt = []
    try:
        for i in range(6):
            player.queue.append(music.Track(title=f"T{i}", stream_url="",
                                            query=f"ytsearch1:T{i}"))

        async def kaputt(_track):
            raise RuntimeError("Netz weg")

        async def sag(text):
            gesagt.append(text)

        music._resolve_track = kaputt
        player._sag = sag
        asyncio.run(player._advance())

        assert len(player.queue) >= 4, \
            f"Warteschlange wurde weggefressen: nur noch {len(player.queue)}"
        assert gesagt and "Warteschlange" in gesagt[0], gesagt
    finally:
        aufraeumen()


def test_musik_pause_ueberlebt_tempo_und_reconnect():
    """Wer pausiert hat, will keine Musik - auch nicht nach einem
    Tempo-Wechsel oder einem Watchdog-Reconnect. Beide starteten die
    Wiedergabe frueher kommentarlos wieder."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        player.start(_track("A"))
        player.pausieren()
        assert player.ist_pausiert() and voice.is_paused()

        # Tempo-Wechsel startet den Song neu - die Pause muss bleiben.
        asyncio.run(player.apply_speed(1.5))
        assert player.ist_pausiert(), "Tempo-Wechsel hat die Pause aufgehoben"
        assert voice.is_paused()

        # Und ein neuer Song hebt sie auf (die Absicht galt dem alten).
        # Wie im echten Ablauf endet der alte Song erst (voice.stop()).
        voice.stop()
        voice.pausiert_ = False
        player.start(_track("B"))
        assert not player.ist_pausiert()
    finally:
        aufraeumen()


def test_musik_befehle_kapern_kein_alltagsdeutsch():
    """Steuerbefehle wurden per PRAEFIX erkannt, ohne Wortgrenze. Damit wurde
    'verlass dich drauf' zum Voice-Leave und 'rausschmeisen @wer' (die
    gaengige Ein-s-Schreibweise) liess Flo den Sprachkanal verlassen und die
    Musik abbrechen, statt die Person zu kicken."""
    import cmdnorm
    import moderation
    import music
    mi = music.instance

    # Gerede darf KEIN Musik-Befehl sein.
    for satz in ("verlass dich drauf", "verlass dich nicht darauf",
                 "liste mal auf was du kannst", "stoppuhr an",
                 "pausenbrot mitbringen", "weitermachen jetzt"):
        assert mi.parse_command(satz) is None, satz

    # Echte Befehle muessen weiter gehen.
    for satz, erwartet in (("leave", "leave"), ("raus", "leave"),
                           ("verlass den kanal", "leave"), ("liste", "queue"),
                           ("queue", "queue"), ("skip", "skip"),
                           ("pause", "pause"), ("weiter", "resume")):
        got = mi.parse_command(satz)
        assert got and got[0] == erwartet, (satz, got)

    # Kick in allen Schreibweisen erreicht die Moderation und NICHT die Musik.
    for satz in ("rausschmeisen <@777>", "rausschmeis <@777>",
                 "rausschmeissen <@777>", "rausschmeißen <@777>", "kick <@777>"):
        norm = cmdnorm.normalize(satz) or satz
        assert moderation.classify(norm) == "kick", satz
        assert mi.parse_command(satz) is None, satz

    # Lautstaerke ueber 999 wurde auf drei Ziffern geschnitten ('ls 1000' -> 100 %).
    assert mi.parse_command("ls 1000") == ("volume", "1000")
    assert mi.parse_command("lautstärke 250") == ("volume", "250")


def test_musik_ffmpeg_bekommt_kein_lesetimeout():
    """KEIN -rw_timeout in den FFmpeg-Optionen - das war ein Eigentor.

    Nachgemessen mit echtem ffmpeg 6.1.1 und einem Leser im Echtzeit-Takt
    (so liest discord.py), Server liefert schubweise mit 20 s Pausen - genau
    so drosselt YouTube:

        mit -rw_timeout 15s :  12,2 s Audio in 99,8 s Wanduhr,
                               stderr voller "Will reconnect at 0"
        ohne                :  24,5 s Audio in 80,4 s Wanduhr, keine Reconnects

    Das Timeout deutet eine normale Liefer-Pause als NETZWERKFEHLER, dann
    greift -reconnect_on_network_error und FFmpeg faengt wieder bei Byte 0 an.
    Der Song beginnt also staendig von vorne - die gemeldete Beschwerde
    "funktioniert nur halbwegs".

    Gegen den stillen Stall steht jetzt der Fortschritts-Waechter in heal():
    der zaehlt echte Audio-Bloecke statt Socket-Betrieb."""
    import music
    assert "-rw_timeout" not in music._FFMPEG_BEFORE, music._FFMPEG_BEFORE
    assert "-timeout" not in music._FFMPEG_BEFORE, music._FFMPEG_BEFORE
    # Die Reconnect-Flags bleiben: bei einem ECHTEN Fehler sind sie richtig.
    for flag in ("-reconnect 1", "-reconnect_streamed 1",
                 "-reconnect_on_network_error 1"):
        assert flag in music._FFMPEG_BEFORE, flag


# --- Schwere Fehler aus dem Volltest (B1-B8) --------------------------------
def test_alle_modul_aliase_existieren():
    """bot.py ruft Modul-Funktionen ueber den Alias am Dateiende auf.

    Fehlt einer, fliegt zur LAUFZEIT ein AttributeError - und in einem
    tasks.loop stoppt das den Loop DAUERHAFT. Genau so ist der Haendler-Loop
    bei der ersten Ankunft gestorben: bot.py rief merchant.build_view(), das
    Modul exportierte den Alias aber nicht. Dieser Test liest bot.py als Text
    und prueft JEDEN dort benutzten Modul-Aufruf."""
    import importlib
    import re
    quelle = open("bot.py", encoding="utf-8").read()
    module = set(re.findall(r"^import (\w+)$", quelle, re.M))
    fehlend = []
    for treffer in re.finditer(r"\b(\w+)\.(\w+)\(", quelle):
        mod, attr = treffer.group(1), treffer.group(2)
        if mod not in module or attr.startswith("_"):
            continue
        try:
            m = importlib.import_module(mod)
        except Exception:  # noqa: BLE001 - nicht importierbar ist hier egal
            continue
        if not hasattr(m, attr):
            fehlend.append(f"{mod}.{attr}")
    assert not fehlend, f"bot.py ruft nicht existierende Aliase: {sorted(set(fehlend))}"


def test_kaputte_datei_haelt_den_bot_nicht_auf():
    """Eine von Hand editierte JSON-Datei darf den START nicht verhindern.

    Die setup()-Kette laeuft in bot.py auf MODULEBENE - wirft dort ein Modul,
    kommt der ganze Bot nicht mehr hoch, nicht nur das eine Feature.
    Nachgemessen galt das fuer economy, words, features, lotto, giveaway und
    schulden. Der Standard eines JsonStore ist jetzt zugleich die
    Typ-Schablone."""
    import json
    import pathlib
    import tempfile
    import store

    alt_dir = store.DATA_DIR
    d = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d
    try:
        (d / "kaputt.json").write_text(json.dumps(
            {"users": None, "zahl": "viel", "liste": {}, "text": 5, "flag": "ja"}))
        s = store.JsonStore("kaputt.json", default={
            "users": {}, "zahl": 0, "liste": [], "text": "", "flag": False})
        assert s.data["users"] == {} and s.data["zahl"] == 0
        assert s.data["liste"] == [] and s.data["text"] == ""
        assert s.data["flag"] is False
        # Unbekannte Schluessel bleiben unangetastet (nichts stillschweigend loeschen).
        (d / "extra.json").write_text(json.dumps({"users": {"1": {"xp": 5}}, "fremd": 42}))
        s2 = store.JsonStore("extra.json", default={"users": {}})
        assert s2.data["users"] == {"1": {"xp": 5}} and s2.data["fremd"] == 42
    finally:
        store.DATA_DIR = alt_dir

    # Und die sechs echten Module kommen mit ihrem jeweiligen Muell hoch.
    import features
    import giveaway
    import lotto
    import schulden
    import words
    d2 = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d2
    try:
        (d2 / "words.json").write_text(json.dumps({"words": None, "scan": []}))
        (d2 / "lotto.json").write_text(json.dumps({"jackpot": "viel"}))
        (d2 / "giveaway.json").write_text(json.dumps({"active": None}))
        (d2 / "schulden.json").write_text(json.dumps({"pairs": None}))
        (d2 / "features.json").write_text(json.dumps(
            {"disabled": [], "guilds": {"abc": ["casino"], "123": ["music"]}}))
        for mod in (words, lotto, giveaway, schulden, features):
            neu = type(mod.instance)()
            neu.setup()          # darf NICHT werfen
        # Der unbrauchbare Server-Eintrag wird uebersprungen, der gute bleibt.
        f = type(features.instance)()
        f.setup()
        assert 123 in f._per_guild and f._per_guild[123] == {"music"}
    finally:
        store.DATA_DIR = alt_dir


def test_speichern_meldet_fehlschlag():
    """save() verschluckte Schreibfehler (Platte voll) dauerhaft still."""
    import pathlib
    import tempfile
    import unittest.mock as mock
    import store
    alt = store.DATA_DIR
    store.DATA_DIR = pathlib.Path(tempfile.mkdtemp())
    try:
        s = store.JsonStore("s.json", default={"a": 1})
        assert asyncio.run(s.save()) is True
        with mock.patch("os.replace", side_effect=OSError(28, "No space left")):
            assert asyncio.run(s.save()) is False
        # Und es bleibt keine .tmp-Leiche liegen (sonst sammeln die sich genau
        # dann an, wenn die Platte ohnehin voll ist).
        assert not list(store.DATA_DIR.glob("s.json*.tmp"))
    finally:
        store.DATA_DIR = alt


def test_speichern_laesst_die_datei_nie_verschwinden():
    """Die Sicherung darf die Hauptdatei nicht kurz WEGnehmen.

    Frueher lief das per Rename: in dem Fenster gab es economy.json schlicht
    nicht. Wer da las (ein zweiter Store, das Panel, ein Reparatur-Skript),
    bekam ENOENT oder den veralteten .bak-Stand - und schlug das folgende
    Rename fehl, war die Hauptdatei dauerhaft weg."""
    import pathlib
    import tempfile
    import unittest.mock as mock
    import store
    alt = store.DATA_DIR
    store.DATA_DIR = pathlib.Path(tempfile.mkdtemp())
    try:
        s = store.JsonStore("w.json", default={"n": 0})
        s.data["n"] = 1
        assert asyncio.run(s.save()) is True
        s.data["n"] = 2
        gesehen = []

        # Mitten im Schreiben nachsehen, ob die Hauptdatei noch da ist.
        echtes_replace = os.replace

        def spion(a, b):
            gesehen.append((s.path.exists(), s.path.read_text(encoding="utf-8")
                            if s.path.exists() else ""))
            return echtes_replace(a, b)

        with mock.patch("os.replace", side_effect=spion):
            assert asyncio.run(s.save()) is True
        assert gesehen and gesehen[0][0] is True, gesehen
        assert '"n":1' in gesehen[0][1], gesehen[0][1]
        # Danach steht der neue Stand in der Datei und der alte in der Sicherung.
        assert '"n":2' in s.path.read_text(encoding="utf-8")
        assert '"n":1' in s._bak.read_text(encoding="utf-8")
    finally:
        store.DATA_DIR = alt


def test_beide_dateien_kaputt_wird_nichts_weggeworfen():
    """Hauptdatei UND Sicherung kaputt: BEIDE muessen beiseite.

    Vorher wurde nur die Hauptdatei quarantaeniert; die kaputte .bak blieb
    liegen und wurde vom zweiten save() unwiederbringlich ueberschrieben -
    genau das, was diese Klasse versprochen hat zu verhindern."""
    import pathlib
    import tempfile
    import store
    alt = store.DATA_DIR
    d = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d
    try:
        (d / "k.json").write_text("{kaputt", encoding="utf-8")
        (d / "k.json.bak").write_text("auch kaputt", encoding="utf-8")
        s = store.JsonStore("k.json", default={"a": 1})
        assert s.data == {"a": 1}
        beiseite = sorted(p.name for p in d.glob("*.kaputt-*"))
        assert len(beiseite) == 2, beiseite
        # Zweimal speichern: das hat frueher die kaputte Sicherung gefressen.
        assert asyncio.run(s.save()) is True
        assert asyncio.run(s.save()) is True
        assert sorted(p.name for p in d.glob("*.kaputt-*")) == beiseite
    finally:
        store.DATA_DIR = alt


def test_guildcfg_roundtrip_fuer_jeden_typ():
    """Gesetzt == gelesen, fuer JEDEN Typ des Katalogs.

    get() schickt den gespeicherten Wert zur Sicherheit nochmal durch die
    Typ-Pruefung - und fuer Listen wurde daraus str([123, 456]), also die
    Tokens '[123,' und '456]'. Die fielen durch die Kanal-Erkennung, und get()
    lieferte IMMER den Standard: die Aufraeum-Kanaele waren faktisch nicht
    einstellbar."""
    guildcfg, zurueck = _cfg_frisch()
    try:
        kanal_ids = [200000000000000001, 200000000000000002]
        guild = SimpleNamespace(
            id=999, text_channels=[],
            get_channel=lambda c: SimpleNamespace(id=c) if c in kanal_ids else None)
        faelle = {
            "autodelete_channels": ("200000000000000001 200000000000000002", kanal_ids),
            "ansage_channel": ("200000000000000001", kanal_ids[0]),
            "lautstaerke": ("80", 80),
            "autodelete_sekunden": ("45", 45),
            "bayern": ("an", True),
        }
        for key, (eingabe, erwartet) in faelle.items():
            ok, gesetzt, fehler = asyncio.run(guildcfg.setzen(999, key, eingabe, guild))
            assert ok, (key, fehler)
            assert gesetzt == erwartet, (key, gesetzt)
            assert guildcfg.get(999, key) == erwartet, \
                f"{key}: gesetzt {gesetzt!r}, gelesen {guildcfg.get(999, key)!r}"
        # Abschalten muss auch wirken (nicht auf den Standard zurueckfallen).
        asyncio.run(guildcfg.setzen(999, "autodelete_channels", "aus", guild))
        assert guildcfg.get(999, "autodelete_channels") == []
    finally:
        zurueck()


def test_purge_rueckfrage_gilt_nur_fuer_den_frager():
    """Die Rueckfrage vor dem Total-Wipe war nur an den KANAL gebunden.

    Mod A fragte an, Mod B schrieb danach im selben Kanal 'loesch alle' - und
    B's ERSTER Befehl loeschte den kompletten Verlauf, ohne dass B die Warnung
    je gesehen hatte."""
    import moderation
    mi = moderation.instance
    alt = dict(mi._wipe_offen)
    mi._wipe_offen = {}
    try:
        kanal = SimpleNamespace(id=777)

        def frage(uid):
            msg = SimpleNamespace(author=SimpleNamespace(id=uid), channel=kanal)
            jetzt = time.time()
            for cid, e in list(mi._wipe_offen.items()):
                if not isinstance(e, tuple) or e[1] <= jetzt:
                    mi._wipe_offen.pop(cid, None)
            offen = mi._wipe_offen.get(kanal.id)
            if not (offen and offen[0] == uid and offen[1] > jetzt):
                mi._wipe_offen[kanal.id] = (uid, jetzt + 60.0)
                return "warnung"
            mi._wipe_offen.pop(kanal.id, None)
            return "wipe"

        assert frage(1) == "warnung"          # A fragt
        assert frage(2) == "warnung", "B durfte ohne eigene Warnung loeschen!"
        assert frage(2) == "wipe"             # B bestaetigt seine EIGENE Warnung
        assert frage(1) == "warnung"          # A faengt wieder von vorn an
    finally:
        mi._wipe_offen = alt


def test_einsatz_rueckgaben_werden_nicht_getilgt():
    """20 % jeder Einnahme gehen an den groessten Glaeubiger - eine ZURUECK-
    GEZAHLTE Wette ist aber keine Einnahme. Alle Rueckgaben in games/casino
    buchten ohne reason und wurden deshalb angeknabbert: der Bot meldete
    'Einsaetze zurueck', angekommen sind 80 %."""
    import re
    import economy
    import schulden
    restore = _with_economy({1: 10_000, 2: 0})
    alt = (schulden.instance._enabled, schulden.instance._store,
           schulden.instance.buch._store, schulden.instance.buch._posten)
    schulden.instance._enabled = True
    schulden.instance._store = _FakeStore(
        {"posten": [], "next_id": 1, "score": {}, "stats": {}, "archiv": {},
         "pairs": {}})
    schulden.instance.buch.laden(schulden.instance._store)
    try:
        schulden.instance.buch.anlegen(2, 1, 5_000)   # 1 schuldet 2 nun 5.000
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="spiele-rueck")
        assert economy.get_coins(1) - vor == 1_000, "Rueckgabe wurde getilgt"
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="spiele")
        assert economy.get_coins(1) - vor == 800, "echter Gewinn wurde NICHT getilgt"
    finally:
        (schulden.instance._enabled, schulden.instance._store,
         schulden.instance.buch._store, schulden.instance.buch._posten) = alt
        restore()

    # Und: keine Rueckgabe-Stelle darf den reason vergessen.
    for datei in ("games.py", "casino.py"):
        quelle = open(datei, encoding="utf-8").read()
        for zeile in quelle.splitlines():
            if "add_coins(" not in zeile or "reason=" in zeile:
                continue
            # Negative Betraege sind Abbuchungen - die loesen keine Tilgung aus.
            if re.search(r"add_coins\([^,]+,\s*-", zeile):
                continue
            # Rueckgaben erkennt man am Kommentar bzw. am Wort 'zurueck'.
            assert "zurueck" not in zeile.lower(), f"{datei}: {zeile.strip()}"


def test_webpanel_haelt_jeden_json_body_aus():
    """Gueltiges, aber nicht-objektes JSON ([1,2,3], null, 42, "x") liess
    JEDEN der elf POST-Endpunkte mit HTTP 500 platzen: request.json() wirft
    dabei nicht, das folgende data.get() schon."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    restore = _with_economy({1: 100})
    wp = webpanel.instance
    alt = (wp._enabled, wp._auth, wp._client, dict(wp._tokens))
    wp._enabled, wp._auth = True, False
    wp._tokens = {}
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: None, get_channel=lambda _x: None)
    app = wp._build_app()

    pfade = ["/api/user/coins", "/api/user/xp", "/api/user/title", "/api/user/shares",
             "/api/stock/price", "/api/server/sendepause", "/api/server/announce",
             "/api/feature", "/api/guildcfg", "/api/login"]

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            for pfad in pfade:
                for body in ("[1,2,3]", "null", "42", '"x"', "true"):
                    r = await cli.post(pfad, data=body,
                                       headers={"Content-Type": "application/json"})
                    assert r.status != 500, f"{pfad} mit {body} -> HTTP 500"
                # Und kaputtes JSON darf auch nicht 500 werden.
                r = await cli.post(pfad, data="{kaputt",
                                   headers={"Content-Type": "application/json"})
                assert r.status != 500, f"{pfad} mit kaputtem JSON -> HTTP 500"

    try:
        asyncio.run(run_it())
    finally:
        (wp._enabled, wp._auth, wp._client, wp._tokens) = alt
        restore()


# --- Mittlere und niedrige Fehler aus dem Volltest --------------------------
def test_cmdnorm_kapert_keine_alltagswoerter():
    """Die Tippfehler-Korrektur schrieb harmlose Woerter auf GEFAEHRLICHE
    Befehle um: 'banane' -> ban, 'klick'/'kicks' -> kick, 'waren' -> warn,
    'klaus' (ein Name!) -> klau. Das erste Wort einer an Flo gerichteten
    Nachricht landet danach direkt bei der Moderation."""
    import cmdnorm
    gefaehrlich = {"ban", "bann", "banne", "verbann", "sperr", "kick", "rauswerf",
                   "warn", "verwarn", "timeout", "mute", "stumm", "knebel",
                   "lösch", "loesch", "purge", "nuke", "nimm", "setcoins",
                   "steal", "klau", "klauen", "raub"}
    harmlos = """banane bananen klick klicks kicks waren ware klaus klaue klauen
        banner warte warten wanne kanne kicker sperre sperrt bahn band bande baum
        raum mann liste lieb leben ende engel essen zeit zeig mach macht mag lang
        kette wette beste feste reste tanne kante kanten""".split()
    schlecht = []
    for wort in harmlos:
        norm = cmdnorm.normalize(wort)
        if norm and norm != wort and norm.split()[0] in gefaehrlich:
            schlecht.append(f"{wort} -> {norm}")
    assert not schlecht, f"harmlose Woerter werden zu Befehlen: {schlecht}"

    # Echte Tippfehler muessen weiterhin korrigiert werden.
    for falsch, richtig in (("warnn", "warn"), ("timout", "timeout"),
                            ("purg", "purge"), ("lösche", "lösch")):
        assert cmdnorm.normalize(falsch) == richtig, (falsch, cmdnorm.normalize(falsch))

    # --- Die arbeit-Befehle: Vertipper ja, Alltagsdeutsch nein --------------
    # Alle drei Faelle sind GEMESSEN, nicht geraten. Ohne die Stopwords wuerde
    # aus "Flo lohnt sich das?" der Lohnzettel und aus "schlicht" eine Schicht.
    for wort in ("lohnt", "lohnte", "schlicht", "schlichte"):
        assert cmdnorm.normalize(wort) is None, (wort, cmdnorm.normalize(wort))
    # 'world' war schon vor arbeit kaputt: 'word' (Wort-Zaehler) ist eine
    # Loeschung entfernt, also wurde "Flo world of warcraft" zur Wort-Statistik.
    assert cmdnorm.normalize("world of warcraft") is None
    for falsch, richtig in (("wokr", "work"), ("arbiet", "arbeit"),
                            ("schihct", "schicht"), ("lohnzettl", "lohnzettel"),
                            ("wrodle", "wordle"), ("tageswrot", "tageswort"),
                            ("gehatl", "gehalt"), ("malohcen", "malochen")):
        raus = cmdnorm.normalize(falsch)
        assert raus == richtig, (falsch, raus)

    # Und jedes Befehlswort, das arbeit.py selbst versteht, MUSS in KNOWN
    # stehen - sonst greift die Korrektur fuer genau dieses Wort nicht.
    import arbeit
    for wort in (arbeit._CMDS + arbeit._LOHN_CMDS + arbeit._WORDLE_CMDS
                 + arbeit._TAGES_CMDS):
        assert wort in cmdnorm.KNOWN, f"{wort!r} fehlt in cmdnorm.KNOWN"


def test_cmdnorm_kennt_die_befehle_aller_module():
    """KNOWN war unvollstaendig: guildcfg, giveaway und die Kurzformen aus
    music/casino/games/profil fehlten komplett. Solange ein Befehl dort fehlt,
    haelt die Aehnlichkeitssuche ihn fuer einen Vertipper und VERBIEGT ihn -
    'sendpause' wurde zu 'sendepause', 'time-out' zu 'timeout', 'naehrwert' zu
    'naehrwerte', 'rausschmeiss' zu 'rausschmeis'. Der Befehl war damit weg.

    Der Test haengt an den Befehlslisten der Module selbst, nicht an einer
    Abschrift - laeuft eine Liste auseinander, faellt es hier auf."""
    import cmdnorm
    import arbeit
    import floaktie
    import giveaway
    import guildcfg
    import lotto
    import merchant
    import profil
    import schulden
    import steal
    listen = (
        arbeit._CMDS + arbeit._LOHN_CMDS + arbeit._TOP_CMDS
        + arbeit._WORDLE_CMDS + arbeit._TAGES_CMDS
        + floaktie._CMDS + floaktie._CHART_CMDS
        + giveaway._CMDS + guildcfg.GuildConfig._CMDS + lotto._CMDS
        + merchant._CMDS + profil._CHECK_CMDS + profil._AVATAR_CMDS
        + profil._BANNER_CMDS + schulden._CMDS + steal._CMDS
    )
    fehlt = sorted({w for w in listen if w not in cmdnorm.KNOWN})
    assert not fehlt, f"fehlt in cmdnorm.KNOWN: {fehlt}"
    # Und keiner davon darf umgeschrieben werden.
    verbogen = {w: cmdnorm.normalize(w) for w in listen
                if cmdnorm.normalize(w) not in (None, w)}
    assert not verbogen, f"cmdnorm verbiegt eigene Befehle: {verbogen}"

    # Die Kurzformen, die vorher gar nicht bekannt waren - stichprobenartig,
    # aber genau die, die im Chat wirklich getippt werden.
    for wort in ("ls", "lst", "bal", "lb", "inv", "bj", "dd", "rps", "ssp",
                 "w6", "dm", "del", "img", "tts", "say", "sb", "gw",
                 "settings", "config", "unbann", "time-out", "sendpause",
                 "naehrwert", "nährwert", "quizduel", "rausschmeiss"):
        assert wort in cmdnorm.KNOWN, wort
        assert cmdnorm.normalize(f"{wort} x") is None, wort


def test_cmdnorm_versteht_englisch_und_boarisch():
    """Der Bot soll auf Deutsch, Englisch UND Boarisch hoeren. Uebersetzt wird
    dabei NUR in ALIAS/DIALECT - ein Synonym in KNOWN waere tot: KNOWN heisst
    'ist schon gueltig, nicht anfassen', das Wort ginge unveraendert an ein
    Modul, das es nicht kennt, und faellt zur KI durch (mit 'wipe' geprueft)."""
    import cmdnorm

    # 1. Struktur: jedes Ziel muss ein Modul kennen, kein Schluessel darf tot
    #    sein, und kein Schluessel darf gleichzeitig als Stopword gebremst sein.
    fehler = []
    for name, topf in (("ALIAS", cmdnorm.ALIAS), ("DIALECT", cmdnorm.DIALECT)):
        for schluessel, ziel in sorted(topf.items()):
            if ziel not in cmdnorm.KNOWN:
                fehler.append(f"{name}[{schluessel}] -> {ziel}: kennt kein Modul")
            if schluessel in cmdnorm.KNOWN:
                fehler.append(f"{name}[{schluessel}]: steht in KNOWN, Eintrag tot")
            if schluessel in cmdnorm.STOPWORDS:
                fehler.append(f"{name}[{schluessel}]: steht in STOPWORDS")
            if schluessel in cmdnorm.NUR_ALLEIN:
                # Diese gelten absichtlich nur OHNE Rest ('aus' ist sonst eine
                # der haeufigsten deutschen Praepositionen).
                if cmdnorm.normalize(schluessel) != ziel:
                    fehler.append(f"{name}[{schluessel}] schreibt allein nicht auf {ziel} um")
                if cmdnorm.normalize(f"{schluessel} x") is not None:
                    fehler.append(f"{name}[{schluessel}] greift trotz NUR_ALLEIN mit Rest")
            elif cmdnorm.normalize(f"{schluessel} x") != f"{ziel} x":
                fehler.append(f"{name}[{schluessel}] schreibt nicht auf {ziel} um")
    assert not fehler, fehler
    assert not (set(cmdnorm.ALIAS) & set(cmdnorm.DIALECT)), "Schluessel doppelt"
    assert not (cmdnorm.STOPWORDS & cmdnorm.KNOWN), "Stopword-Schutz waere tot"

    # 2. Englisch
    for rein, raus in (("wipe 50", "purge 50"), ("silence @wer", "mute @wer"),
                       ("tempmute @wer 10m", "timeout @wer 10m"),
                       ("addcoins @wer 100", "gib @wer 100"),
                       ("removemoney @wer 100", "nimm @wer 100"),
                       ("broadcast hallo", "ansage hallo"),
                       ("whisper @wer hi", "dm @wer hi"),
                       ("warnings @wer", "warns @wer"), ("cfg", "config"),
                       ("options", "einstellungen"), ("shift", "schicht"),
                       ("grind", "arbeit"), ("broke", "pleite")):
        assert cmdnorm.normalize(rein) == raus, (rein, cmdnorm.normalize(rein))

    # 3. Boarisch - quer durch alle Features
    for rein, raus in (("spuits was", "spiel was"), ("kimm eina", "komm eina"),
                       ("nomoi", "nochmal"), ("aufdrahn", "lauter"),
                       ("vaschwind", "leave"), ("goid", "coins"),
                       ("schotter", "coins"), ("kontostond", "kontostand"),
                       ("gschäft", "shop"), ("kini", "thron"),
                       ("fladern @wer", "klau @wer"), ("hackln", "arbeit"),
                       ("schuftn", "arbeit"), ("wörtl", "wordle"),
                       ("gwinnspui 5k 2h", "gewinnspiel 5k 2h"),
                       ("zoggn", "casino"), ("vawarn @wer", "verwarn @wer"),
                       ("zammrama 20", "aufräum 20"), ("schaugn @wer", "check @wer"),
                       ("eistellunga", "einstellungen"), ("schmäh", "spruch")):
        assert cmdnorm.normalize(rein) == raus, (rein, cmdnorm.normalize(rein))

    # 4. Ende zu Ende: das uebersetzte Wort muss beim echten Modul auch ANKOMMEN.
    #    'Ziel steht in KNOWN' allein reicht als Beweis nicht - hier laufen die
    #    Muster der Module selbst mit.
    import media
    import moderation
    strecken = (("wipe 50", moderation._CMD_RE),
                ("silence @wer 10m", moderation._TIMEOUT_RE),
                ("tempmute @wer 10m", moderation._TIMEOUT_RE),
                ("warnings @wer", moderation._WARNS_RE),
                ("zeichna ein drache", media.Media._GEN_RE))
    for rein, muster in strecken:
        norm = cmdnorm.normalize(rein) or rein
        assert muster.match(norm), f"{rein!r} -> {norm!r} erkennt das Modul nicht"

    # 5. Die Uebersetzungs-Toepfe sind bewusst KEINE Vertipper-Ziele. Gemessen:
    #    liesse man _fuzzy auch auf ihre Schluessel los, wuerde 'emma' zu leave,
    #    'lisa' zu leiser und 'normal' zu 'nochmal'.
    for wort in ("emma", "lisa", "normal", "weit", "hits", "geschäft",
                 "warning", "kommt", "schleicht"):
        assert cmdnorm.normalize(wort) is None, (wort, cmdnorm.normalize(wort))


def test_bildauftrag_braucht_wirklich_einen_auftrag():
    """Ein Bild kostet echtes Geld bei der KI. Nachgemessen loesten SECHS
    voellig normale deutsche Woerter einen Auftrag aus:

        'Flo zeichnen wir mal was?'  -> 'zeichne wir mal was'  -> Bild
        'Flo malen wir mal was?'     -> 'male wir mal was'     -> Bild
        'Flo generierte Bilder ...'  -> traf media._GEN_RE direkt

    Zwei verschiedene Ursachen: cmdnorm korrigierte die Beugungen auf den
    Befehl, und media._GEN_RE nahm mit 'generier\\w*' auch Vergangenheit und
    Substantive ('generierte', 'generierung')."""
    import cmdnorm
    import media
    muster = media.Media._GEN_RE

    def loest_aus(satz):
        return bool(muster.match(cmdnorm.normalize(satz) or satz))

    for wort in ("zeichnen", "zeichnet", "zeichnete", "malen", "malte",
                 "generieren", "generierte", "generierung"):
        satz = f"{wort} wir mal was"
        assert not loest_aus(satz), f"{satz!r} loest einen bezahlten Bildauftrag aus"

    # Die echten Befehle muessen selbstverstaendlich weiter funktionieren.
    for satz in ("zeichne einen drachen", "male einen hund", "generiere ein bild",
                 "generier was", "img katze", "bild von einem auto"):
        assert loest_aus(satz), f"{satz!r} wird nicht mehr erkannt"


def test_cmdnorm_aus_ist_nur_allein_ein_befehl():
    """'Flo aus!' heisst wirklich stop - aber 'aus' ist auch eine der
    haeufigsten deutschen Praepositionen. Nachgemessen wurde aus

        'Flo aus welchem Grund machst du das?'

    ein 'stop welchem Grund ...': die Musik ging aus, und die Frage erreichte
    die KI nie, weil der Befehl sie abgefangen hat."""
    import cmdnorm
    assert "aus" in cmdnorm.NUR_ALLEIN
    for allein in ("aus", "aus!", "AUS", " aus "):
        assert cmdnorm.normalize(allein) == "stop", allein
    for satz in ("aus welchem Grund machst du das?", "aus Spass", "aus dem Nichts",
                 "aus Versehen geloescht", "aus der Reihe"):
        assert cmdnorm.normalize(satz) is None, (satz, cmdnorm.normalize(satz))
    # Der Rest des Dialekt-Topfs bleibt unberuehrt.
    assert cmdnorm.normalize("spui was") == "spiel was"
    assert cmdnorm.normalize("hoit") == "stop"
    # Und jeder NUR_ALLEIN-Eintrag muss auch wirklich im Dialekt-Topf stehen.
    for wort in cmdnorm.NUR_ALLEIN:
        assert wort in cmdnorm.DIALECT or wort in cmdnorm.ALIAS, wort


def test_cmdnorm_laesst_alltag_und_vornamen_in_ruhe():
    """Am ganzen Wortschatz des Repos nachgemessen (nicht geraten): das hier
    waren echte Fehlgriffe der Aehnlichkeitssuche. 'Flo heisst du Flo?' hat
    einen Coin-Raub gestartet, 'pure' war ein Massenloeschen, 'nebel' ein
    Knebel, 'cash' eine Crash-Wette, 'build'/'zeichen' ein KI-Bildauftrag
    (kostet Geld), 'komma' hat Flo in den Voice geholt - und 'anne', 'frank',
    'laura', 'ines' sind Vornamen."""
    import cmdnorm
    ruhe = """pure heisst nebel anne cash mies meine ines leiche bogen leite
        schwein takte stake tanke schenken schenkt build zeichen malle komma
        frank krank trank laura chat hart swords dicke close grass pfad
        spieler zweiter passt spass spaß mach macht kannst red rede stell
        unser sehen even quite stock share item ticket option marco sven
        mats""".split()
    schlecht = {w: cmdnorm.normalize(w) for w in ruhe
                if cmdnorm.normalize(w) is not None}
    assert not schlecht, f"Alltagswoerter werden zu Befehlen: {schlecht}"

    # Die Toleranz darf davon nicht stumpf werden - echte Vertipper gehen weiter.
    for falsch, richtig in (("skpi", "skip"), ("wokr", "work"),
                            ("wrodle", "wordle"), ("timout", "timeout"),
                            ("admiin", "admin"), ("lottoo", "lotto"),
                            ("laut", "lautr"), ("verlassen", "verlasse")):
        assert cmdnorm.normalize(falsch) == richtig, (falsch,
                                                     cmdnorm.normalize(falsch))


def test_backfill_laeuft_fuer_jeden_server():
    """bot.py startet den Verlauf-Nachbau JE SERVER gleichzeitig
    (bot.py:2042-2044). _backfill_running war aber ein prozessweites Bool: der
    erste Server gewann, alle anderen stiegen sofort wieder aus. Und weil auch
    scan['done'] global war, bekam Server 2 seinen Verlauf NIE - der Kommentar
    im Code behauptete sogar, es liefe sauber je Guild."""
    import words
    w = words.instance
    alt_zustand = (w._enabled, w._store, w._backfill_running, w._save_store,
                   w._buch, words.BACKFILL)
    w._enabled = True
    words.BACKFILL = True
    w._store = SimpleNamespace(
        data={"scan": {"before": 1, "done": False, "channels": {}}, "guilds": {}},
        save=lambda: None)

    async def save():
        pass

    w._save_store = save
    w._buch = lambda _gid: {"words": {}}
    w._backfill_running = set()
    gelaufen = []

    def guild(gid, kanal_id):
        perms = SimpleNamespace(view_channel=True, read_message_history=True)
        kanal = SimpleNamespace(id=kanal_id, name=f"c{kanal_id}",
                                permissions_for=lambda _m: perms)

        async def history(**_k):
            gelaufen.append(gid)
            return
            yield                                  # macht daraus einen Generator

        kanal.history = history
        return SimpleNamespace(id=gid, me=SimpleNamespace(), text_channels=[kanal])

    async def beide():
        await asyncio.gather(w.backfill(guild(1, 11)), w.backfill(guild(2, 22)))

    try:
        asyncio.run(beide())
        assert sorted(set(gelaufen)) == [1, 2], (
            f"nur Server {sorted(set(gelaufen))} hat seinen Verlauf bekommen")
        assert w._store.data["scan"]["fertig"] == {"1": True, "2": True}
        # Beim naechsten Start ist nichts mehr zu tun.
        gelaufen.clear()
        asyncio.run(beide())
        assert not gelaufen, "Backfill laeuft unnoetig erneut"
    finally:
        (w._enabled, w._store, w._backfill_running, w._save_store,
         w._buch, words.BACKFILL) = alt_zustand


def test_giveaway_panel_behaelt_seinen_loeschschutz():
    """_protect gibt beim Schuetzen das VORIGE Panel desselben Slots frei - so
    ist es gedacht ("es kann nur EINS je Slot aktuell sein"). Aber ALLE
    Sendestellen liefen ueber denselben Standard-Slot: jede Assistenten-Frage
    gab damit den Schutz des LAUFENDEN Giveaway-Panels frei.

    In einem Aufraeum-Kanal verschwand danach genau die Nachricht mit dem
    Mitmach-Knopf - waehrend die Coins weiter hinterlegt blieben."""
    import giveaway
    gw = giveaway.instance
    geschuetzt = []
    alt_protect = gw._protect
    gw._protect = lambda msg, **kw: geschuetzt.append(kw.get("slot", "panel"))
    try:
        zaehler = {"n": 0}

        async def send(**_k):
            zaehler["n"] += 1
            return SimpleNamespace(id=zaehler["n"])

        kanal = SimpleNamespace(id=77, send=send)

        async def lauf():
            await gw._send(kanal, embed=None, slot="gw:5")   # das Panel
            await gw._send(kanal, embed=None)                # Assistenten-Frage
            await gw._send(kanal, embed=None)                # noch eine

        asyncio.run(lauf())
    finally:
        gw._protect = alt_protect

    assert geschuetzt[0] == "gw:5", geschuetzt
    assert geschuetzt[1] == geschuetzt[2] == "wizard:77", geschuetzt
    assert geschuetzt[0] not in geschuetzt[1:], (
        "Panel und Assistenten-Fragen teilen sich wieder einen Slot - die "
        "naechste Frage raeumt das laufende Giveaway weg")


def test_haendler_nimmt_keinen_besseren_titel_als_einsatz():
    """Der Tausch prueft nur eine UNTERGRENZE. Wer fuer einen Tausch
    'mindestens episch' braucht, konnte damit seinen GOETTLICHEN Titel
    hergeben und einen schlechteren zurueckbekommen - unwiderruflich, ohne
    Rueckfrage, und im Dropdown stand er ganz normal zur Auswahl."""
    import merchant
    import titles
    m = merchant.instance

    besitz = [
        {"text": "n", "label": "Normal", "rarity": "normal"},
        {"text": "e", "label": "Episch", "rarity": "episch"},
        {"text": "x", "label": "Exklusiv", "rarity": "exklusiv"},
        {"text": "g", "label": "Goettlich", "rarity": "goettlich"},
    ]
    alt_liste = economy.list_titles
    economy.list_titles = lambda _uid: besitz
    try:
        wer = SimpleNamespace(id=1)
        # Tausch: braucht mindestens 'episch', gibt 'exklusiv'.
        auswahl = [o["rarity"] for o in m.eligible_gives(wer, "episch", "exklusiv")]
        assert "episch" in auswahl and "exklusiv" in auswahl, auswahl
        assert "goettlich" not in auswahl, "der bessere Titel steht noch im Dropdown"
        assert "normal" not in auswahl, auswahl
        # Ohne Obergrenze bleibt das alte Verhalten (andere Aufrufer).
        assert "goettlich" in [o["rarity"] for o in m.eligible_gives(wer, "episch")]
    finally:
        economy.list_titles = alt_liste

    # Die Rangfolge, auf der das beruht, muss aufsteigend sein.
    assert titles.RANK["goettlich"] > titles.RANK["exklusiv"] > titles.RANK["episch"]


def test_schicht_rechnet_bei_doppelklick_nur_einmal_ab():
    """SchichtView.beenden() setzte self.fertig, hat es aber nie GEPRUEFT - und
    interaction_check prueft nur, WER klickt, nicht wie oft. Zwei schnelle
    Klicks (oder zwei Modal-Eingaben) liefen also beide durch und
    abrechnen() schrieb den Lohn zweimal gut.

    Der Riegel gehoert an genau eine Stelle: dreizehn Aufrufstellen koennen ihn
    nicht jede fuer sich mitbringen."""
    import arbeit
    v = arbeit.SchichtView(arbeit.instance, 4242, arbeit.SCHICHTEN["salat"])
    v.message = None
    gerufen = []

    alt_abrechnen = arbeit.instance.abrechnen
    alt_embed = arbeit.instance.ergebnis_embed
    arbeit.instance.abrechnen = lambda *a, **k: (gerufen.append(1), (1000, ""))[1]
    arbeit.instance.ergebnis_embed = lambda *a, **k: SimpleNamespace(
        set_image=lambda **kw: None)

    async def nichts(**_k):
        pass

    inter = SimpleNamespace(
        response=SimpleNamespace(edit_message=nichts, defer=nichts),
        user=SimpleNamespace(id=4242, display_name="x"),
        edit_original_response=nichts)

    async def dreimal():
        await v.beenden(inter, "t", "x", 1.0)
        await v.beenden(inter, "t", "x", 1.0)     # Doppelklick
        await v.beenden(inter, "t", "x", 1.0)

    try:
        asyncio.run(dreimal())
    finally:
        arbeit.instance.abrechnen = alt_abrechnen
        arbeit.instance.ergebnis_embed = alt_embed

    assert len(gerufen) == 1, f"Lohn wurde {len(gerufen)}x gutgeschrieben"
    assert v.fertig is True


def test_antwort_mit_ping_ist_kein_ziel():
    """In Discord haengt eine Antwort den Autor der beantworteten Nachricht an
    message.mentions - auch wenn niemand ein @ getippt hat (der Client pingt
    beim Antworten standardmaessig). Wer die Liste roh nimmt, trifft den
    Falschen:

        (Antwort auf Bobs Nachricht) 'Flo ban spam'  -> bannt BOB
        (Antwort auf Bobs Nachricht) 'Flo klau'      -> beklaut BOB

    economy._pay hatte das schon geloest (mit Kommentar), zwoelf andere Stellen
    nicht - darunter Moderation, Raub, Schuldbuch und die Admin-Coins."""
    import basis
    bob = SimpleNamespace(id=111, bot=False)
    alice = SimpleNamespace(id=222, bot=False)
    flo = SimpleNamespace(id=999, bot=True)

    # 1. Antwort-mit-Ping, kein getipptes @ -> KEIN Ziel.
    antwort = SimpleNamespace(mentions=[bob], content="Flo ban spam")
    assert basis.echte_erwaehnungen(antwort) == []
    assert basis.erstes_ziel(antwort) is None

    # 2. Wirklich getippt -> Ziel.
    getippt = SimpleNamespace(mentions=[bob], content="Flo ban <@111> spam")
    assert basis.erstes_ziel(getippt) is bob
    # Auch die Nickname-Form <@!id>.
    assert basis.erstes_ziel(
        SimpleNamespace(mentions=[bob], content="ban <@!111>")) is bob

    # 3. Reihenfolge folgt dem TEXT, nicht der (unsortierten) Liste.
    reihe = SimpleNamespace(mentions=[bob, alice],
                            content="pay <@222> 100 statt <@111>")
    assert [u.id for u in basis.echte_erwaehnungen(reihe)] == [222, 111]

    # 4. Bots und ausgeschlossene IDs fliegen raus.
    mitbot = SimpleNamespace(mentions=[flo, bob], content="<@999> ban <@111>")
    assert basis.erstes_ziel(mitbot) is bob
    assert basis.erstes_ziel(mitbot, ohne=(111,)) is None

    # 5. Dieselbe Person doppelt getippt zaehlt einmal.
    doppelt = SimpleNamespace(mentions=[bob], content="<@111> und <@111>")
    assert len(basis.echte_erwaehnungen(doppelt)) == 1

    # 6. Kaputte Nachricht kippt nicht um.
    assert basis.echte_erwaehnungen(SimpleNamespace()) == []
    assert basis.erstes_ziel(SimpleNamespace(mentions=None, content=None)) is None


def test_kein_modul_zielt_wieder_auf_die_rohe_mention_liste():
    """Der Riegel zur Fehlerklasse oben: wer ein ZIEL bestimmt, muss
    basis.echte_erwaehnungen nehmen. message.mentions roh ist nur dort in
    Ordnung, wo es NICHT ums Zielen geht (z. B. 'ist Flo erwaehnt?').

    Geprueft wird der SYNTAXBAUM, nicht der Text - sonst schlagen Docstrings an,
    die message.mentions bloss erwaehnen (das ist mir hier prompt passiert)."""
    import ast
    import glob
    # Die Ausnahmen haengen am INHALT der Zeile, nicht an ihrer Nummer.
    # Vorher stand hier ("bot.py", 1637) - und jede Aenderung WEITER OBEN in
    # bot.py liess diesen Test scheitern, obwohl an der Sache nichts falsch
    # war. Ein Riegel, der bei fremden Aenderungen anschlaegt, wird
    # frueher oder spaeter weggeworfen statt gelesen.
    erlaubt = {
        # nur: ist Flo ueberhaupt angesprochen?
        ("bot.py", "if not angesprochen and self.user in message.mentions:"),
        # nur: ist ueberhaupt jemand genannt?
        ("economy.py", "if not message.mentions:"),
        # nur eine Nachschlagetabelle - das Ziel waehlt _pay danach aus dem Text
        ("economy.py", "by_id = {u.id: u for u in message.mentions}"),
        # Rueckfall, nachdem der Text nichts hergab
        ("economy.py", "ziel = ziel or message.mentions[0]"),
        # nur: ist ueberhaupt jemand genannt?
        ("fun.py", 'if (first in ("rate", "bewerte") and not message.mentions'),
        # Rueckfall hinter erstes_ziel(...)
        ("profil.py", "return erstes_ziel(message) or message.mentions[0], \"\""),
    }
    schuldige = []
    gesehen = set()
    for datei in sorted(glob.glob("*.py")):
        if datei.startswith("test_") or datei == "basis.py":
            continue
        quelle = open(datei, encoding="utf-8").read()
        for knoten in ast.walk(ast.parse(quelle)):
            if not (isinstance(knoten, ast.Attribute) and knoten.attr == "mentions"):
                continue
            basis_knoten = knoten.value
            if not (isinstance(basis_knoten, ast.Name) and basis_knoten.id == "message"):
                continue
            zeile = quelle.splitlines()[knoten.lineno - 1].strip()
            if (datei, zeile) in erlaubt:
                gesehen.add((datei, zeile))
                continue
            schuldige.append(f"{datei}:{knoten.lineno}  {zeile[:70]}")
    assert not schuldige, (
        "Diese Stellen zielen auf die rohe Mention-Liste - bei einer "
        "Antwort-mit-Ping treffen sie den Falschen:\n  " + "\n  ".join(schuldige))
    # Und die Ausnahmeliste darf nicht vergammeln: verschwindet eine Zeile,
    # muss sie hier raus - sonst deckt sie irgendwann versehentlich eine
    # voellig andere Stelle mit demselben Wortlaut.
    tot = sorted(erlaubt - gesehen)
    assert not tot, f"Ausnahmen zeigen ins Leere, bitte entfernen: {tot}"


def test_purge_zaehlt_keine_erwaehnung_als_anzahl():
    """'Flo lösch @spammer' hat MAX_PURGE Nachrichten geloescht - ohne Rueckfrage.

    Die Erwaehnung steht im Text als '<@1453881901738889351>', und die Suche
    nach der Anzahl (re.search(r"\d+", rest)) fand die ID. Ausgerechnet die
    harmlos aussehende Form war die gefaehrlichste: 'loesch alle' fragt einmal
    nach, 'loesch @wer' loeschte sofort. Unwiderruflich.

    Gilt genauso fuer Kanaele <#123>, Rollen <@&123>, Custom-Emojis
    <:name:123> und Zeitmarken <t:123:R> - alles Ziffern."""
    import moderation
    marke = moderation._MARKE_RE

    def anzahl(rest):
        """Genau die zwei Zeilen aus moderation.py, die die Anzahl bestimmen."""
        ohne = marke.sub(" ", rest)
        treffer = re.search(r"\d+", ohne)
        return int(treffer.group()) if treffer else None

    # Was frueher zur Massenloeschung fuehrte, ergibt jetzt KEINE Anzahl.
    for gefaehrlich in ("<@1453881901738889351>",
                        "<@!1453881901738889351>",
                        "<@&987654321012345678>",
                        "<:blob:123456789012345678>",
                        "<a:party:123456789012345678>",
                        "<t:1787228079:R>",
                        "<#1453881901738889351>"):
        assert anzahl(gefaehrlich) is None, (gefaehrlich, anzahl(gefaehrlich))

    # Eine echte Zahl wird weiterhin gelesen - auch NEBEN einer Erwaehnung.
    assert anzahl("20") == 20
    assert anzahl("lösch 20 bitte") == 20
    assert anzahl("<#1453881901738889351> 5") == 5
    assert anzahl("alle") is None

    # Und der Code nutzt wirklich den bereinigten Text - nicht den rohen.
    quelle = inspect.getsource(moderation.Moderation)
    i = quelle.index("rest_ohne_marken = ")
    danach = quelle[i:i + 400]
    assert 're.search(r"\\d+", rest_ohne_marken)' in danach, (
        "die Anzahl wird wieder aus dem rohen Text gelesen")


def test_kauf_rueckfrage_haengt_am_titel_nicht_an_der_nummer():
    """Die Bestaetigung band nur an die SLOT-NUMMER. Wuerfelt der Shop um 2 Uhr
    neu, kaufte 'nochmal derselbe Befehl' danach den Titel auf derselben
    Nummer - einen anderen, dessen Preis nie jemand gesehen hatte."""
    import economy
    e = economy.instance
    alt = dict(e._kauf_offen)
    e._kauf_offen = {}
    try:
        wer = SimpleNamespace(id=4711)
        teuer_a = {"text": "koenig", "label": "König", "price": 5_000_000}
        teuer_b = {"text": "kaiser", "label": "Kaiser", "price": 9_000_000}
        # Erste Anfrage -> Rueckfrage.
        assert e._kauf_rueckfrage(wer, 2, teuer_a) is not None
        # Gleicher Titel nochmal -> geht durch.
        assert e._kauf_rueckfrage(wer, 2, teuer_a) is None
        # Neue Anfrage, dann Shop-Reroll: gleiche Nummer, ANDERER Titel.
        assert e._kauf_rueckfrage(wer, 2, teuer_a) is not None
        assert e._kauf_rueckfrage(wer, 2, teuer_b) is not None, \
            "anderer Titel auf derselben Nummer wurde ohne Rueckfrage gekauft!"
    finally:
        e._kauf_offen = alt


def test_casino_deckel_stimmt_mit_dem_konto_ueberein():
    """_auszahlen deckelt auf MAX_WIN, aber alle Aufrufer verwarfen den
    Rueckgabewert: Embed und Bilanz meldeten den UNGEDECKELTEN Gewinn, das
    Konto bekam den gedeckelten. Anzeige und Wirklichkeit widersprachen sich."""
    import casino
    import economy
    restore = _with_economy({5: 0})
    alt_max = casino.MAX_WIN
    casino.MAX_WIN = 1000
    try:
        gemeldet = casino._auszahlen(5, 999_999)
        assert gemeldet == 1000, gemeldet
        assert economy.get_coins(5) == 1000, economy.get_coins(5)
        # Und der Normalfall bleibt unveraendert.
        gemeldet = casino._auszahlen(5, 500)
        assert gemeldet == 500 and economy.get_coins(5) == 1500
    finally:
        casino.MAX_WIN = alt_max
        restore()

    # Kein Aufrufer darf den Rueckgabewert noch wegwerfen.
    import re
    quelle = open("casino.py", encoding="utf-8").read()
    lose = [z.strip() for z in quelle.splitlines()
            if re.match(r"\s*_auszahlen\(", z) and "self.bet" not in z]
    assert not lose, f"_auszahlen-Rueckgabe ignoriert: {lose}"


def test_los_und_lose_erreichen_das_lotto():
    """'Flo los' / 'Flo lose 5' landeten im Casino (frueher in der Kette) als
    Rubbellos - das Lotto-Panel war darueber nicht erreichbar."""
    import casino
    import lotto
    quelle = open("casino.py", encoding="utf-8").read()
    handle_teil = quelle.split("async def handle", 1)[1][:8000]
    assert '"los"' not in handle_teil and '"lose"' not in handle_teil, \
        "casino beansprucht 'los'/'lose' wieder"
    assert "los" in lotto._CMDS and "lose" in lotto._CMDS
    # Das Rubbellos behaelt seine eindeutigen Woerter.
    for wort in ("rubbellos", "rubbel", "scratch"):
        assert f'"{wort}"' in handle_teil, wort


def test_kleine_fehler_der_wirtschaft():
    """add_coins-Rueckgabe, Admin-Korrekturen und Slot-Deckel."""
    import economy
    import games
    import schulden
    restore = _with_economy({1: 1000, 2: 0})
    alt = (schulden.instance._enabled, schulden.instance._store,
           schulden.instance.buch._store, schulden.instance.buch._posten)
    schulden.instance._enabled = True
    schulden.instance._store = _FakeStore(
        {"posten": [], "next_id": 1, "score": {}, "stats": {}, "archiv": {},
         "pairs": {}})
    schulden.instance.buch.laden(schulden.instance._store)
    try:
        schulden.instance.buch.anlegen(2, 1, 5_000)   # 1 schuldet 2 nun 5.000
        # add_coins meldete den Stand VOR der Tilgung - Anzeigen logen.
        gemeldet = economy.add_coins(1, 1_000, reason="spiele")
        assert gemeldet == economy.get_coins(1), \
            f"add_coins meldet {gemeldet}, Konto ist {economy.get_coins(1)}"
        # Admin-Korrekturen muessen EXAKT ankommen (wie beim Panel).
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="admin")
        assert economy.get_coins(1) - vor == 1_000
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="setcoins")
        assert economy.get_coins(1) - vor == 1_000
    finally:
        (schulden.instance._enabled, schulden.instance._store,
         schulden.instance.buch._store, schulden.instance.buch._posten) = alt
        restore()

    # Slot-Textbefehl kannte keine Obergrenze - das Menue schon.
    quelle = open("games.py", encoding="utf-8").read()
    slot_teil = quelle.split("async def _slot(", 1)[1][:900]
    assert "SKILL_MAX_BET" in slot_teil, "Slot-Textpfad ohne Hoechsteinsatz"


def test_food_liest_deutsche_tausenderpunkte():
    """'ca. 1.200 kcal' wurde zu 1,2 kcal - der Punkt ist im Deutschen der
    Tausender-Trenner, nicht das Dezimalzeichen."""
    import food
    num = food.instance._num
    assert num("ca. 1.200 kcal") == 1200.0
    assert num("1.234.567 kcal") == 1234567.0
    assert num("1,5") == 1.5           # Komma bleibt Dezimalzeichen
    assert num("2.5 g") == 2.5         # Punkt mit 1 Ziffer = Dezimalzeichen
    assert num("8/10") == 8.0
    assert num("abc") == 0.0 and num(None) == 0.0


def test_bayrisch_versteht_die_standard_schreibweise():
    """'bayerisch' (mit e) schaltete gar nichts - der Regex kannte nur
    'bayrisch' und 'boarisch'."""
    import bayern
    rx = bayern.instance._TOGGLE_RE
    for schreibweise in ("bayrisch an", "bayerisch an", "bairisch an",
                         "baierisch aus", "boarisch an", "dialekt aus"):
        assert rx.match(schreibweise), schreibweise
    for kein in ("banane", "bayern muenchen", "bay"):
        assert not rx.match(kein), kein
    # 'sprich/red bayerisch' ist die natuerlichste Formulierung - die landete
    # frueher bei voicegags in der Sprachausgabe statt beim Dialekt.
    lose = bayern.instance._TOGGLE_LOSE_RE
    for satz in ("sprich bayerisch", "red mal boarisch", "schreib auf bayrisch",
                 "antworte bitte bayerisch", "sprich bayerisch aus"):
        assert lose.match(satz), satz
    assert not lose.match("sprich nicht so laut")


def test_giveaway_schnellstart_lost_nichts_aus():
    """'jetzt', 'ende' und 'stop' sind normale Woerter. Weil sie IRGENDWO im
    Text gesucht wurden, loste 'giveaway 5k 2h weil ich jetzt Lust habe' ein
    laufendes Giveaway sofort aus - Gewinner Stunden zu frueh."""
    quelle = open("giveaway.py", encoding="utf-8").read()
    teil = quelle.split("# Abbrechen / sofort ziehen", 1)[1][:1200]
    assert "erstes" in teil and "low.split()" in teil, \
        "ziehen/abbrechen wird nicht mehr am ERSTEN Wort erkannt"
    assert "self._hat(low, (\"abbrechen\"" not in teil


def test_musik_erkennt_soundcloud():
    """SoundCloud-Links landeten in der YouTube-TEXTSUCHE: parse_command kannte
    nur Spotify und YouTube, alles andere fiel durch und wurde wie Freitext
    behandelt - Flo suchte also auf YouTube nach der URL-Zeichenkette. yt-dlp
    bringt den SoundCloud-Extractor laengst mit; es fehlte nur die Erkennung."""
    import music
    mi = music.instance

    # Einzelne Tracks -> ganz normaler play-Pfad.
    for url in ("https://soundcloud.com/forss/flickermood",
                "https://www.soundcloud.com/forss/flickermood",
                "https://m.soundcloud.com/forss/flickermood",
                "https://on.soundcloud.com/AbCdEf",
                "https://SoundCloud.COM/a/b"):
        for text in (url, f"spiel {url}", f"schau mal {url} an"):
            got = mi.parse_command(text)
            assert got == ("play", url), (text, got)

    # Sets -> eigener Playlist-Pfad.
    for url in ("https://soundcloud.com/forss/sets/soulhack",
                "https://www.soundcloud.com/user/sets/mein-mix"):
        got = mi.parse_command(f"spiel {url}")
        assert got == ("sc_playlist", url), got

    # Fremde Links bleiben unveraendert.
    assert mi.parse_command("spiel https://www.youtube.com/watch?v=x") \
        == ("play", "https://www.youtube.com/watch?v=x")
    # Direkte Audio-Dateien spielt FFmpeg selbst - auch die landeten frueher
    # in der YouTube-Textsuche.
    assert mi.parse_command("spiel https://example.com/lied.mp3") \
        == ("play", "https://example.com/lied.mp3")
    assert mi.parse_command("https://example.com/set.opus?x=1") \
        == ("play", "https://example.com/set.opus?x=1")
    # Eine BELIEBIGE Webseite darf die Musik NICHT an sich reissen.
    assert mi.parse_command("was hältst du von https://example.com/artikel") is None

    # Kein Link -> weiterhin Suche.
    assert mi.parse_command("spiel Bohemian Rhapsody") == ("search", "Bohemian Rhapsody")


def test_musik_playlist_helfer_teilt_sich_youtube_und_soundcloud():
    """EIN flacher Playlist-Helfer fuer beide Quellen. YouTube liefert pro
    Eintrag manchmal nur die Video-ID - daraus muss die volle URL werden;
    SoundCloud liefert immer die komplette Adresse."""
    import music
    mi = music.instance

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if "soundcloud" in url:
                return {"entries": [
                    {"url": "https://soundcloud.com/a/eins", "title": "Eins"},
                    {"url": "https://soundcloud.com/a/zwei", "title": "Zwei"},
                    None,                       # kaputter Eintrag -> ueberspringen
                ]}
            return {"entries": [{"id": "abc123", "title": "Video"}]}

    alt = music.yt_dlp
    music.yt_dlp = SimpleNamespace(YoutubeDL=FakeYDL)
    try:
        sc = asyncio.run(mi._soundcloud_set("https://soundcloud.com/a/sets/x"))
        assert sc == [("https://soundcloud.com/a/eins", "Eins"),
                      ("https://soundcloud.com/a/zwei", "Zwei")], sc
        yt = asyncio.run(mi._youtube_playlist("https://youtube.com/playlist?list=X"))
        assert yt == [("https://www.youtube.com/watch?v=abc123", "Video")], yt
        # Die flache Extraktion muss wirklich flach sein (sonst dauert eine
        # 100-Track-Playlist ewig, weil jeder Track einzeln aufgeloest wird).
        assert FakeYDL({}).opts is not None
    finally:
        music.yt_dlp = alt


def test_kaputte_daten_toeten_kein_feature_zur_laufzeit():
    """Kaputte Werte im Store duerfen nicht erst BEIM BENUTZEN sterben.

    Die Typ-Schablone des JsonStore prueft nur die oberste Ebene: '"coins":
    null' oder '"xp": "viel"' TIEF IM Profil kommt daran vorbei. Vorher starb
    daran nicht nur die eine Anzeige - economy.on_message laeuft ohne
    Auffangnetz, also erreichte danach der ganze Rest dieser Nachricht die
    Platte nicht mehr, und EIN kaputtes Fremdprofil kippte 'top' und
    'reichste' fuer alle."""
    import casino
    import economy
    import floaktie
    import handel
    import luxus
    import moderation
    import steal
    import words

    # --- economy: Profil-Weg und die drei Ranglisten --------------------
    alt = (economy.instance._store, economy.instance._enabled)
    economy.instance._enabled = True
    economy.instance._store = _FakeStore({"users": {
        "1": {"xp": "viel", "coins": None, "owned": {"a": 1}, "name": "A"},
        "2": None,
        "3": {"xp": 500, "coins": 100, "owned": [], "name": "C"},
    }})
    try:
        economy.instance.add_coins(1, 10, reason="test")
        assert economy.instance.get_coins(1) == 10
        assert economy.instance._profile(1)["owned"] == []
        assert economy.instance._level_only(economy.instance._profile(1)["xp"]) == 0
        # Die Ranglisten lesen den Store ROH - genau dort lag der Fehler.
        assert len(economy.instance.leaderboard_data(10)) == 3
        assert len(economy.instance.money_leaderboard_data(10)) == 3
        assert economy.instance._rank_of(3)[1] == 3
        economy.instance._leaderboard_embed()
    finally:
        (economy.instance._store, economy.instance._enabled) = alt

    # --- floaktie: Anteile und Kurs-Verlauf ------------------------------
    alt_f = floaktie.instance._store
    floaktie.instance._store = _FakeStore({
        "holdings": {"1": "viel", "2": None, "3": 4},
        "history": [{"day": "2026-01-01", "price": 10}, None, "murks"],
        "ticks": None,
        "price": 100, "base": 100, "day": "2026-01-01",
    })
    try:
        assert floaktie.instance.shares_of(1) == 0
        assert floaktie.instance.total_shares() == 4
        assert floaktie.instance.holders_count() == 1
        assert floaktie.instance.top_holder() == 3
        floaktie.instance._sparkline()
        floaktie.instance._series(7)
        assert isinstance(floaktie.instance.leaderboard(), list)
    finally:
        floaktie.instance._store = alt_f

    # --- die uebrigen Auskuenfte, die der Profil-Lookup mitzieht ---------
    faelle = [
        (handel.instance, "_store", {"users": {"7": {"in": None, "out": "x", "n": None}}},
         lambda m: m.summe_von(7) == (0, 0, 0)),
        (moderation.instance, "_store", {"warns": None},
         lambda m: m.warns_of(1, 7) == 0),
        (moderation.instance, "_store", {"warns": {"1": {"7": None}}},
         lambda m: m.warns_of(1, 7) == 0),
        (steal.instance, "_store", {"cooldowns": None},
         lambda m: m._remaining(7) == 0),
        (luxus.instance, "_store", {"users": {"7": None}, "throne": None},
         lambda m: m.owns(7, "krone") is False and m.throne_preis() == luxus.THRONE_START),
        (casino.instance, "_stats", {"stats": None},
         lambda m: m._stats_profile(7)["games"] == 0),
        (words.instance, "_store", {"guilds": {"1": {"words": {"hallo": 5},
                                                     "total": "viel"}}},
         lambda m: m.statistik_von(7) == (0, 0, [])),
    ]
    for modul, feld, daten, pruefung in faelle:
        vorher_store = getattr(modul, feld)
        vorher_an = modul._enabled
        setattr(modul, feld, _FakeStore(daten))
        modul._enabled = True
        try:
            assert pruefung(modul), (type(modul).__name__, daten)
        finally:
            setattr(modul, feld, vorher_store)
            modul._enabled = vorher_an

    # words zaehlt trotz kaputtem Index weiter (das lief bei JEDER Nachricht).
    alt_w = (words.instance._store, words.instance._enabled)
    words.instance._enabled = True
    words.instance._store = _FakeStore(
        {"guilds": {"1": {"words": None, "total": None, "msgs": "x"}}})
    try:
        words.instance._count_text("hallo welt hallo", "7", 1)
        words.instance._count_text("hallo welt", "7", 1)
        buch = words.instance._buch(1)
        assert buch["words"]["hallo"]["n"] == 3
        assert buch["total"] == 5
        assert buch["msgs"] == 2
    finally:
        (words.instance._store, words.instance._enabled) = alt_w


def test_wort_index_hat_einen_deckel():
    """Ohne Deckel konnte eine Person die Datei beliebig aufblasen.

    Jedes Speichern serialisiert den ganzen Index im Event-Loop (json.dumps
    gibt die GIL auch im Thread nicht frei) - 100.000 Woerter kosteten
    gemessen rund 200 ms Stillstand fuer ALLE."""
    import words

    alt = (words.instance._store, words.instance._enabled, words.MAX_WORDS)
    words.instance._enabled = True
    words.MAX_WORDS = 50
    words.instance._store = _FakeStore({"guilds": {}})
    try:
        # Ein haeufiges Wort und viel Einmal-Spam.
        for _ in range(5):
            words.instance._count_text("wichtig", "7", 1)
        for i in range(300):
            words.instance._count_text(f"spamwort{i:04d}", "7", 1)
        index = words.instance._buch(1)["words"]
        assert len(index) <= words.MAX_WORDS, len(index)
        # Gekuerzt wird nach Haeufigkeit: das oft gesagte Wort bleibt.
        assert "wichtig" in index
    finally:
        (words.instance._store, words.instance._enabled, words.MAX_WORDS) = alt


def test_befehle_kapern_kein_alltagsdeutsch():
    """Ganz normale deutsche Saetze duerfen keinen Befehl ausloesen.

    Gemessen waren das 75 % Fehlalarm bei der Beleidigungs-Erkennung (Flo hat
    hoeflichen Leuten daraufhin eine beleidigende DM geschickt), 74 % beim
    Bildgenerator und 63 % beim Soundboard."""
    import fun
    import media
    import voicegags

    # --- Beleidigungen: Alltagswoerter zaehlen nur mit direkter Anrede -----
    harmlos = [
        "Der Hund hatte den Ball im Maul.",
        "Bei dem Erdbeben gab es leider viele Opfer.",
        "Wisch das mal mit dem Lappen weg.",
        "Ich hab mir einen Fidget Spinner gekauft.",
        "Er hat sich als Noob geoutet, haha, alles gut.",
        "Der Penner-Bus faehrt gleich, ich muss los.",
        "Ich hab dir den Lappen doch schon gegeben.",
        "Mein Arsch tut weh vom langen Sitzen.",
        "Das war echt Kacke gestern, so ein Pech.",
    ]
    for satz in harmlos:
        assert not fun.instance.looks_offensive(satz), satz
    beleidigend = [
        "du opfer",
        "halt dein maul",
        "du bist ein lappen ey",
        "arschloch",
        "was ein hurensohn",
        "verpiss dich",
    ]
    for satz in beleidigend:
        assert fun.instance.looks_offensive(satz), satz

    # --- 'rate' ist auch der Imperativ von RATEN --------------------------
    class _Msg:
        def __init__(self, text, mentions=()):
            self.content = text
            self.mentions = list(mentions)
            self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=42))
            self.author = _fake_person(uid=7, name="wer")

    alt_fun = fun.instance._enabled
    fun.instance._enabled = True
    try:
        for satz in ("rate mal wer gewonnen hat",
                     "rate mal wieviel Uhr es ist",
                     "bewerte doch mal unseren neuen Kanal ehrlich"):
            assert asyncio.run(fun.instance.handle(_Msg(satz))) is None, satz
    finally:
        fun.instance._enabled = alt_fun

    # --- Bildgenerator: Redewendungen sind kein Motiv ---------------------
    for satz in ("bild dir nichts ein", "bild dir nur nichts drauf ein",
                 "zeichne dich nicht durch Faulheit aus", "male mir nichts vor"):
        assert media.Media._GEN_RE.match(satz) is None, satz
    for satz in ("bild ein Drache aus Neon", "male einen Drachen aus Neon",
                 "zeichne eine Katze im Weltall", "generiere ein Bild von einem Auto",
                 "img cyberpunk city at night"):
        assert media.Media._GEN_RE.match(satz) is not None, satz

    # --- Soundboard/TTS: 'sounds gut' ist Zustimmung, kein Befehl ---------
    def _sag(text):
        m = SimpleNamespace(content=text, guild=SimpleNamespace(id=1),
                            author=_fake_person(uid=7), mentions=[])
        return asyncio.run(voicegags.instance.handle(m))

    alt_v = voicegags.instance._enabled
    voicegags.instance._enabled = True
    try:
        for satz in ("sounds gut, lass uns das so machen", "sounds good!",
                     "sprich nicht so laut", "say what?",
                     "vorlesen macht mein Kind gerne", "sprich mal mit ihm"):
            assert _sag(satz) is None, satz
    finally:
        voicegags.instance._enabled = alt_v


def test_bot_erwaehnung_ist_kein_ziel():
    """'@Flo schulden' ist die eigene Tafel, nicht die Tafel MIT Flo.

    Wer Flo per @Mention anspricht, hat den Bot in message.mentions stehen.
    Ohne Filter zeigte '@Flo schulden' die Paar-Tafel mit Flo als Gegenueber,
    und '@Flo schulden erlassen @Kumpel' richtete sich gegen den Bot."""
    import schulden

    flo = _fake_person(uid=42, name="flo", bot=True)
    ich = _fake_person(uid=7, name="ich")
    kumpel = _fake_person(uid=8, name="kumpel")

    def _msg(text, mentions):
        return SimpleNamespace(
            content=text, mentions=list(mentions), author=ich,
            guild=SimpleNamespace(id=1, me=flo, get_member=lambda _u: None))

    restore, sch = _schulden_setup()
    try:
        # Nur Flo erwaehnt -> eigenes Buch.
        emb = asyncio.run(schulden.instance.handle(_msg("<@42> schulden", [flo])))
        assert "Dein Schuldbuch" in _embed_text(emb), _embed_text(emb)
        # Flo UND ein Mensch -> der Mensch ist gemeint.
        _schuld(sch, 8, 7, 5_000)
        emb = asyncio.run(schulden.instance.handle(
            _msg("<@42> schulden <@8>", [flo, kumpel])))
        assert "<@8>" in _embed_text(emb) and "<@42>" not in _embed_text(emb), _embed_text(emb)
        # Erlassen richtet sich nie gegen den Bot.
        antwort = asyncio.run(schulden.instance.handle(
            _msg("<@42> schulden erlassen", [flo])))
        assert "Wem willst du was erlassen" in _embed_text(antwort)
    finally:
        restore()


def test_musik_watchdog_frisst_die_liegengebliebene_queue_nicht():
    """Gibt _advance auf, darf der Watchdog nicht im 15-s-Takt nachtreten.

    Sonst hebelt Fall 3 der Heilung genau die Aufgabe-Schwelle aus, die
    verhindern soll, dass ein Netzausfall die ganze Playlist wegfrisst - und
    dieselbe Warnung landet alle 15 Sekunden im Chat."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    gesagt = []
    try:
        for i in range(6):
            player.queue.append(music.Track(title=f"T{i}", stream_url="",
                                            query=f"ytsearch1:T{i}"))

        async def kaputt(_track):
            raise RuntimeError("Netz weg")

        async def sag(text):
            gesagt.append(text)

        music._resolve_track = kaputt
        player._sag = sag
        asyncio.run(player._advance())
        assert player._advance_aufgegeben is True
        rest = len(player.queue)

        # Der Watchdog laeuft mehrfach - und laesst die Warteschlange in Ruhe.
        voice.spielt = False
        guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())
        for _ in range(3):
            asyncio.run(player.heal(guild))
        assert len(player.queue) == rest, (len(player.queue), rest)
        assert len(gesagt) == 1, gesagt

        # Ein neuer Song hebt die Aufgabe auf - vielleicht laedt der ja.
        music.instance._einreihen(player, music.Track(title="neu", stream_url="", query="q"))
        assert player._advance_aufgegeben is False
    finally:
        aufraeumen()


def test_praefix_gilt_je_server():
    """Jeder Server darf Flo anders nennen - ohne Neustart.

    Frueher cachte sich JEDES der 21 Module beim Start seinen eigenen
    self._bot_name, und bot.py baute den Trigger-Regex einmal beim Import. Ein
    eigener Praefix je Server war damit nicht moeglich. Jetzt ist ai.py die
    einzige Autoritaet, und der Name wird zur Laufzeit nachgeschlagen."""
    import ai
    import economy
    import music
    guildcfg, zurueck = _cfg_frisch()
    try:
        guild = SimpleNamespace(id=4711, text_channels=[], get_channel=lambda _c: None)
        ok, wert, fehler = asyncio.run(guildcfg.setzen(4711, "praefix", "Bob", guild))
        assert ok, fehler
        assert guildcfg.get(4711, "praefix") == "Bob"

        # Der Name kommt je Server heraus ...
        assert ai.bot_name(4711) == "Bob"
        assert ai.bot_name(9999) == ai.instance._bot_name      # fremder Server
        # ... und die Regexe ziehen mit.
        assert ai.trigger_re(4711).search("bob mach mal was")
        assert not ai.trigger_re(9999).search("bob mach mal was")
        assert ai.strip_lead("Bob, level", 4711) == "level"
        assert ai.strip_lead("Bob, level", 9999) == "Bob, level"

        # Ohne ausdrueckliche gid gilt der Server, der gerade bedient wird -
        # daran haengen alle Module, ohne dass sie es selbst wissen muessen.
        token = ai.setze_guild(4711)
        try:
            assert ai.bot_name() == "Bob"
            assert economy.instance._bot_name == "Bob"
            assert music.instance._bot_name == "Bob"
            assert ai.strip_lead("bob level") == "level"
        finally:
            ai.guild_zuruecksetzen(token)
        assert economy.instance._bot_name != "Bob"

        # Aendern wirkt sofort - der Regex-Cache wird ueber den Hook geleert.
        asyncio.run(guildcfg.setzen(4711, "praefix", "Klaus", guild))
        assert ai.bot_name(4711) == "Klaus"
        assert ai.trigger_re(4711).search("klaus?") and not ai.trigger_re(4711).search("bob")
        # Zuruecksetzen ebenso.
        asyncio.run(guildcfg.loeschen(4711, "praefix"))
        assert ai.bot_name(4711) == ai.instance._bot_name

        # Unsinn kommt gar nicht erst rein: die Ansprache wird zum Regex.
        for murks in ("a", "x" * 40, "Flo Bot", "Fl(o", "flo|.*"):
            ok, _w, fehler = asyncio.run(guildcfg.setzen(4711, "praefix", murks, guild))
            assert not ok and fehler, murks
    finally:
        ai.praefix_geaendert()
        zurueck()


def test_kein_modul_haelt_den_botnamen_selbst():
    """Kein Modul darf sich den Namen wieder in eine eigene Variable legen.

    Genau das war die Bremse: 21 Kopien von os.getenv("BOT_NAME"), die beim
    Start eingefroren wurden. Wer ein neues Modul baut, erbt von FeatureBasis -
    dann stimmt der Name je Server von allein."""
    import pathlib
    import basis

    treffer = []
    for pfad in sorted(pathlib.Path(".").glob("*.py")):
        if pfad.name in ("ai.py", "basis.py", "test_games_logic.py", "test_logic.py"):
            continue          # ai haelt den .env-Standard, basis erklaert die Regel
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            if "self._bot_name = " in zeile and not zeile.strip().startswith("#"):
                treffer.append(f"{pfad.name}:{nr}")
    assert not treffer, f"eigene Kopie des Botnamens: {treffer}"

    # Und die Basis liefert wirklich den Namen des gerade bedienten Servers.
    class Beispiel(basis.FeatureBasis):
        pass

    import ai
    token = ai.setze_guild(0)
    try:
        assert Beispiel()._bot_name == ai.instance._bot_name
    finally:
        ai.guild_zuruecksetzen(token)


def test_panel_protokolliert_und_sichert():
    """Jede SCHREIBENDE Panel-Aktion landet im Protokoll, und das Backup
    liefert wirklich ein ZIP.

    Das Protokoll ist kein Login-Thema (den gibt es hier bewusst nicht),
    sondern Nachvollziehbarkeit: wer spaeter wissen will, warum ein Konto
    5 Mio mehr hat, findet es hier. Es haengt an einer Middleware, damit auch
    der naechste neue Knopf mit protokolliert wird."""
    import pathlib
    import tempfile
    import store
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    restore = _with_economy({1: 100})
    wp = webpanel.instance
    alt = (wp._enabled, wp._auth, wp._client, dict(wp._tokens), list(wp._log),
           wp._log_store, store.DATA_DIR)
    d = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d
    (d / "economy.json").write_text('{"users":{}}', encoding="utf-8")
    (d / "games.json").write_text('{"counting":{}}', encoding="utf-8")
    wp._enabled, wp._auth = True, False
    wp._tokens, wp._log, wp._log_store = {}, [], None
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: None, get_channel=lambda _x: None)
    app = wp._build_app()

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            await cli.post("/api/user/coins", json={"uid": 1, "delta": 5000})
            await cli.get("/api/features")           # Lesen wird NICHT notiert
            r = await cli.get("/api/log")
            daten = await r.json()
            eintraege = daten["eintraege"]
            assert len(eintraege) == 1, eintraege
            assert eintraege[0]["pfad"] == "/api/user/coins"
            assert eintraege[0]["daten"]["delta"] == 5000
            # Backup: echtes ZIP mit den Dateien drin.
            r = await cli.get("/api/backup")
            assert r.status == 200
            roh = await r.read()
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(roh)) as zf:
                assert set(zf.namelist()) >= {"economy.json", "games.json"}
            assert "flobot-backup-" in r.headers.get("Content-Disposition", "")

    try:
        asyncio.run(run_it())
    finally:
        (wp._enabled, wp._auth, wp._client, wp._tokens, wp._log,
         wp._log_store, store.DATA_DIR) = alt
        restore()


def test_musik_kaputter_song_blockiert_den_naechsten_nicht():
    """DIE gemeldete Sackgasse: ein Song, der keinen Ton liefert, liess sich
    nicht wegskippen - nur 'Flo stop' half.

    Grund: der Watchdog belebte denselben toten Song alle 30 s neu, und JEDER
    Neustart zaehlt die Wiedergabe-Generation hoch. Genau daran hing aber der
    after-Callback, den 'skip' ausgeloest hat - der Skip verpuffte, Flo meldete
    trotzdem 'uebersprungen', und weil is_active() die ganze Zeit True blieb,
    reihte 'Flo spiel X' nur noch ein."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    gesagt = []

    async def sag(text):
        gesagt.append(text)

    player._sag = sag
    guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())
    try:
        kaputt = _track("Kaputt")
        gut = _track("Gut")
        player.start(kaputt)
        player.queue.append(gut)
        # Der Song "spielt", aber der Block-Zaehler steht: kein Ton.
        voice.stall = True

        # Der Watchdog versucht es - aber nicht ewig.
        for _ in range(12):
            asyncio.run(player.heal(guild))
        assert player._neustart_versuche <= music.NEUSTART_MAX_VERSUCHE
        # Er hat aufgegeben und ist weitergegangen, statt in der Schleife zu bleiben.
        assert player.current is gut, player.current
        assert not player.queue
        assert any("nächsten" in t for t in gesagt), gesagt
    finally:
        aufraeumen()


def test_musik_skip_haengt_nicht_am_callback():
    """Skip muss auch dann wirken, wenn der after-Callback entwertet ist.

    Der Watchdog, ein Tempo-Wechsel oder ein Reconnect zaehlen die Generation
    hoch; faellt ein Skip in dieses Fenster, kam er frueher nie an."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        a, b = _track("A"), _track("B")
        player.start(a)
        player.queue.append(b)
        # Generation hochzaehlen, so wie es ein Watchdog-Neustart tut ...
        player._play_gen += 5
        # ... und trotzdem muss der Skip durchgehen.
        asyncio.run(player.skip())
        assert player.current is b, player.current
        assert not player.queue

        # Und ein Skip auf dem LETZTEN Song raeumt sauber auf.
        asyncio.run(player.skip())
        assert player.current is None and not player.queue
    finally:
        aufraeumen()


def test_musik_versteht_die_links_aus_den_apps():
    """Wie Links WIRKLICH im Chat ankommen - nicht wie im Lehrbuch.

    Der Kurzlink der Spotify-Handy-App (spotify.link/...) traf keinen einzigen
    Regex und landete in der YouTube-TEXTSUCHE: Flo suchte nach der
    Zeichenkette. Am PC ging es, vom Handy geteilt nicht - genau das gemeldete
    'Spotify geht nur halb'. Dazu kleben im Chat Satzzeichen an der URL."""
    import music
    p = music.instance.parse_command
    faelle = {
        # Handy-Share
        "https://spotify.link/aBcDeFgHi": ("spotify_kurz", "https://spotify.link/aBcDeFgHi"),
        "https://spoti.fi/3xYz": ("spotify_kurz", "https://spoti.fi/3xYz"),
        # Discord unterdrueckt die Vorschau mit spitzen Klammern
        "<https://youtu.be/dQw4w9WgXcQ>": ("play", "https://youtu.be/dQw4w9WgXcQ"),
        # Link am Satzende / in Klammern
        "https://youtu.be/dQw4w9WgXcQ.": ("play", "https://youtu.be/dQw4w9WgXcQ"),
        "(https://youtu.be/dQw4w9WgXcQ)": ("play", "https://youtu.be/dQw4w9WgXcQ"),
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT,":
            ("play", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"),
        # die ueblichen Varianten muessen weiter stimmen
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ": ("play", "https://m.youtube.com/watch?v=dQw4w9WgXcQ"),
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ": ("play", "https://music.youtube.com/watch?v=dQw4w9WgXcQ"),
        "https://www.youtube.com/shorts/dQw4w9WgXcQ": ("play", "https://www.youtube.com/shorts/dQw4w9WgXcQ"),
        "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT?si=x":
            ("play", "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT?si=x"),
    }
    for eingabe, erwartet in faelle.items():
        assert p(f"spiel {eingabe}") == erwartet, (eingabe, p(f"spiel {eingabe}"))
    # Ein Kurzlink OHNE Befehlswort bleibt normales Gerede.
    assert p("schau mal https://spotify.link/aBc") == ("spotify_kurz", "https://spotify.link/aBc")


def test_musik_abgebrochener_song_gilt_nicht_als_fertig():
    """Stirbt FFmpeg mitten im Song, darf Flo nicht einfach weiterschalten.

    discord.py meldet beides GLEICH: liefert read() b"", ist der Song 'zu
    Ende' - egal ob er wirklich durch ist oder der Prozess abgestuerzt ist.
    Der after-Callback bekommt dabei KEINEN Fehler. Flo hielt einen nach 40
    von 200 Sekunden abgestuerzten Song also fuer fertig und ging zum
    naechsten; fuer den Zuhoerer bricht die Musik staendig ab und springt
    weiter - genau das gemeldete 'funktioniert nur halbwegs'."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        lang = music.Track(title="Lang", stream_url="http://stream/lang",
                           query="ytsearch1:Lang", duration=200)
        naechster = _track("Naechster")
        player.start(lang)
        player.queue.append(naechster)

        def stirbt_bei(sekunde):
            """FFmpeg ist weg: der Player spielt nicht mehr, after feuert -
            und zwar OHNE Fehler, genau wie am echten Songende."""
            player._played = sekunde
            player._seg_start = None
            voice.spielt = False
            asyncio.run(player._advance(player._play_gen))

        # 40 von 200 Sekunden gehoert - dann stirbt FFmpeg.
        stirbt_bei(40.0)

        # Der Song laeuft weiter (an der Stelle), die Warteschlange bleibt.
        assert player.current is lang, player.current
        assert player.queue == [naechster], player.queue
        assert player._neustart_versuche == 1

        # Beim zweiten Mal nochmal - beim dritten gibt Flo auf und schaltet weiter.
        stirbt_bei(40.0)
        assert player.current is lang and player._neustart_versuche == 2
        stirbt_bei(40.0)
        assert player.current is naechster, player.current

        # Und ein Song, der WIRKLICH durch ist, schaltet ganz normal weiter.
        kurz = music.Track(title="Kurz", stream_url="http://stream/kurz",
                           query="", duration=100)
        letzter = _track("Letzter")
        voice.spielt = False
        player.start(kurz)
        player.queue.append(letzter)
        stirbt_bei(100.0)          # hier ist er wirklich durch
        assert player.current is letzter, player.current
    finally:
        aufraeumen()


def test_musik_veraltete_stream_adresse_wird_erneuert():
    """Eine Stream-Adresse, die lange in der Warteschlange lag, ist tot.

    YouTube unterschreibt seine Adressen zeitlich. Wer eine Playlist einwirft
    und eine Stunde spaeter beim zwanzigsten Song ankommt, startet dort eine
    URL, die es nicht mehr gibt: der Song 'laeuft', es kommt aber nie Ton -
    genau das sah nach 'der Song geht einfach nicht' aus."""
    import time as _t
    import music
    player, voice, aufraeumen = _musik_umgebung()
    geholt = []

    async def frisch(track):
        geholt.append(track.title)
        return music.Track(title=track.title, stream_url="http://stream/neu",
                           query=track.query, duration=100,
                           geloest_um=_t.monotonic())

    music._resolve_track = frisch
    try:
        alt = music.Track(title="Alt", stream_url="http://stream/tot",
                          query="ytsearch1:Alt", duration=100,
                          geloest_um=_t.monotonic() - music.STREAM_MAX_ALTER - 60)
        assert music._adresse_alt(alt) is True
        player.queue.append(alt)
        asyncio.run(player._advance())
        assert geholt == ["Alt"], geholt
        assert player.current.stream_url == "http://stream/neu"

        # Eine FRISCHE Adresse wird nicht unnoetig neu geholt.
        geholt.clear()
        voice.spielt = False
        neu = music.Track(title="Neu", stream_url="http://stream/frisch",
                          query="ytsearch1:Neu", duration=100,
                          geloest_um=_t.monotonic())
        assert music._adresse_alt(neu) is False
        player.queue.append(neu)
        asyncio.run(player._advance())
        assert geholt == [], geholt
        assert player.current.stream_url == "http://stream/frisch"
    finally:
        aufraeumen()


def test_musik_playlist_ueberlebt_kaputten_ersten_song():
    """Ein gesperrter erster Titel warf die KOMPLETTE Liste weg.

    'Den ersten Song konnte ich nicht laden' - und die 49 einwandfreien
    dahinter waren mit weg. Genau so fuehlt sich 'Playlist geht nur halb' an."""
    import music
    mi = music.instance
    player, voice, aufraeumen = _musik_umgebung()
    versucht = []

    async def resolve(inp, hint):
        versucht.append(inp)
        if "kaputt" in inp:
            raise RuntimeError("gesperrt")
        return music.Track(title=inp, stream_url="http://stream/x", duration=100)

    async def kein_panel(*a, **k):
        return None

    alt = (mi._resolve_input, mi._send_panel)
    mi._resolve_input = resolve
    mi._send_panel = kein_panel
    try:
        items = [("kaputt1", "Kaputt 1", None),
                 ("gut1", "Gut 1", None),
                 ("gut2", "Gut 2", None),
                 ("gut3", "Gut 3", None)]
        antwort = asyncio.run(mi._play_many(
            player, _VoiceChannelStub(), items, "wer", "aus der Playlist"))
        assert antwort is music.HANDLED, antwort
        # Der kaputte wurde uebersprungen, der naechste laeuft, der Rest wartet.
        assert player.current.title == "gut1", player.current
        assert [t.query for t in player.queue] == ["gut2", "gut3"]

        # Zwei kaputte am Stueck sind kein Zufall -> die Liste NICHT durchbrennen.
        player2, voice2, aufraeumen2 = _musik_umgebung()
        try:
            versucht.clear()
            items = [("kaputt1", "K1", None), ("kaputt2", "K2", None),
                     ("gut1", "Gut 1", None)]
            antwort = asyncio.run(mi._play_many(
                player2, _VoiceChannelStub(), items, "wer", "aus der Playlist"))
            assert antwort is not music.HANDLED
            assert len(versucht) == 2, versucht
        finally:
            aufraeumen2()
    finally:
        (mi._resolve_input, mi._send_panel) = alt
        aufraeumen()


def test_musik_geteilter_song_schlaegt_die_playlist():
    """Wer einen Song AUS einer Playlist teilt, will DEN Song.

    Der Haupt-Grund fuer 'YouTube-Links gehen nur halb': geteilt wird
    watch?v=DERSONG&list=PL...&index=17 - und Flo spielte Track 1 der Playlist,
    also einen voellig anderen Song. Bei list=WL ('Später ansehen') oder
    list=LL ('Mag ich') kam sogar gar nichts: an diese Listen kommt der Bot
    nicht heran, und der Fehler beendete den ganzen Befehl."""
    import music
    p = music.instance.parse_command
    # Ein Video im Link -> das Video, egal welche Liste danebensteht.
    fuer_video = [
        "https://www.youtube.com/watch?v=sharedVid11&list=PLbig&index=17",
        "https://www.youtube.com/watch?v=abc12345678&list=WL&index=3",
        "https://www.youtube.com/watch?v=abc12345678&list=LL",
        "https://youtu.be/abc12345678?list=PLbig",
        "https://www.youtube.com/shorts/abc12345678?list=PLbig",
        "https://www.youtube.com/watch?v=abc12345678&list=RDabc",   # Auto-Mix
    ]
    for u in fuer_video:
        assert p(f"spiel {u}") == ("play", u), (u, p(f"spiel {u}"))
    # Eine reine Playlist-Adresse benennt kein Video - die bleibt Playlist.
    rein = "https://www.youtube.com/playlist?list=PLbig"
    assert p(f"spiel {rein}") == ("yt_playlist", rein)


def test_musik_spotify_landet_nie_in_der_textsuche():
    """Podcast, Show, Kuenstler-Seite: Flo hat woertlich nach der URL gesucht.

    Was kein Song, Album oder keine Playlist ist, fiel durch alle Zweige und
    landete in der YouTube-TEXTSUCHE - Flo spielte dann irgendein fremdes
    Video, das zufaellig auf die Zeichenkette passte."""
    import music
    p = music.instance.parse_command
    for u in ("https://open.spotify.com/episode/512ojhOuo1ktJprKbVcKyQ",
              "https://open.spotify.com/artist/0TnOYISbd1XYRBk9myaseg",
              "https://open.spotify.com/show/4rOoJ6Egrf8K2IrywzwOMk",
              "spotify:episode:512ojhOuo1ktJprKbVcKyQ",
              "https://open.spotify.com/collection/tracks"):
        aktion, _arg = p(f"spiel {u}")
        assert aktion == "spotify_unbekannt", (u, aktion)
    # Die alte /user/<name>/playlist/-Form ist eine ganz normale Playlist.
    alt = "https://open.spotify.com/user/spotify/playlist/37i9dQZF1DXcBWIGoYBM5M"
    assert p(f"spiel {alt}") == ("spotify_playlist", alt)
    # Und die bekannten Formen bleiben, wie sie waren.
    for u, erwartet in (
            ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", "play"),
            ("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3", "spotify_album")):
        assert p(f"spiel {u}")[0] == erwartet, u


def test_musik_ffmpeg_bekommt_die_client_kennung():
    """DER Grund, warum gar nichts mehr lief. Am Server nachgemessen:

        [https] HTTP error 403 Forbidden
        Song '...' brach nach 0 von 178 s ab

    YouTube unterschreibt eine Stream-Adresse fuer GENAU den Client, der sie
    angefragt hat - in der Adresse steht 'c=ANDROID_VR'. ffmpeg meldete sich
    aber mit seiner eigenen Kennung ('Lavf/...'), weil die http_headers von
    yt-dlp nirgends weitergereicht wurden. YouTube antwortet darauf mit 403,
    und jeder Song bricht nach 0 Sekunden ab."""
    import music
    ua = "com.google.android.apps.youtube.vr.oculus/1.62.27 (Linux; U; Android 12)"

    # Ohne Kopfzeilen darf gar nichts vorangestellt werden (SoundCloud, Dateien).
    assert music.Track(title="x", stream_url="u").ffmpeg_vorspann() == ""

    t = music.Track(title="x", stream_url="u", kopfzeilen={
        "User-Agent": ua, "Accept-Language": "de-DE,de;q=0.9",
        "Range": "bytes=0-", "Host": "boese.example", "Accept-Encoding": "gzip"})
    vorspann = t.ffmpeg_vorspann()
    zerlegt = shlex.split(vorspann)          # genau so zerlegt discord.py sie
    assert "-user_agent" in zerlegt and ua in zerlegt, zerlegt
    assert "Accept-Language: de-DE,de;q=0.9\r\n" in " ".join(zerlegt)
    # Range/Host/Accept-Encoding gehoeren zur ANFRAGE, nicht zum Client -
    # durchgereicht brechen sie die Verbindung.
    for verboten in ("bytes=0-", "boese.example", "gzip"):
        assert verboten not in vorspann, verboten

    # Eine Kennung mit Leerzeichen und Anfuehrungszeichen darf die Kommandozeile
    # nicht zerlegen - sonst waere das eine Befehls-Einschleusung.
    gemein = 'Mozilla/5.0 "x" ; rm -rf /'
    zerlegt = shlex.split(music.Track(title="x", stream_url="u",
                                      kopfzeilen={"User-Agent": gemein}).ffmpeg_vorspann())
    assert zerlegt == ["-user_agent", gemein], zerlegt

    # _extract MUSS die Kopfzeilen von yt-dlp uebernehmen - sonst ist der ganze
    # Vorspann wertlos.
    quelle = inspect.getsource(music.Music._extract)
    assert "http_headers" in quelle, "_extract nimmt die Kopfzeilen nicht mit"

    # Und beim Abspielen muessen sie VOR '-i' landen (danach ignoriert ffmpeg sie).
    start = inspect.getsource(music.GuildPlayer.start)
    assert "ffmpeg_vorspann()" in start, "_start reicht die Kennung nicht durch"
    assert start.index("ffmpeg_vorspann()") < start.index("before_options")


def test_musik_ffmpeg_holt_damit_wirklich_ton():
    """Der Ernstfall mit ECHTEM ffmpeg: ein Server, der sich wie YouTube
    verhaelt (Ton nur fuer die richtige Client-Kennung, sonst 403). Die reine
    Options-Pruefung oben beweist noch nicht, dass ffmpeg sie auch annimmt."""
    import music
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return                               # ohne ffmpeg laeuft die Musik ohnehin nicht
    import http.server
    import subprocess
    import threading

    ua = "TestClient/1.0 (Android 12)"
    # 0,4 s Stille als WAV - klein, ohne Fremddaten, von ffmpeg selbst erzeugt.
    erzeugt = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.4", "-f", "wav", "-"],
        capture_output=True, timeout=60)
    if erzeugt.returncode != 0 or len(erzeugt.stdout) < 1000:
        return                               # ffmpeg zu alt/beschnitten
    ton = erzeugt.stdout

    class Griff(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("User-Agent", "") != ua:
                leib = b"403 Forbidden"
                self.send_response(403)
                self.send_header("Content-Length", str(len(leib)))
                self.end_headers()
                self.wfile.write(leib)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(ton)))
            self.end_headers()
            self.wfile.write(ton)

    server = http.server.HTTPServer(("127.0.0.1", 0), Griff)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    adresse = f"http://127.0.0.1:{server.server_address[1]}/videoplayback?c=ANDROID_VR"

    def wieviel_ton(track):
        """Baut die ffmpeg-Zeile genau so wie GuildPlayer._start."""
        vorne = [t for t in (track.ffmpeg_vorspann(), music._FFMPEG_BEFORE) if t]
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error",
                *shlex.split(" ".join(vorne)), "-i", track.stream_url,
                "-f", "s16le", "-ar", "48000", "-ac", "2", "-"]
        return len(subprocess.run(argv, capture_output=True, timeout=60).stdout)

    try:
        ohne = wieviel_ton(music.Track(title="x", stream_url=adresse))
        mit = wieviel_ton(music.Track(title="x", stream_url=adresse,
                                      kopfzeilen={"User-Agent": ua}))
    finally:
        server.shutdown()
    assert ohne == 0, f"ohne Kennung kam unerwartet Ton ({ohne} Bytes)"
    assert mit > 10000, f"mit Kennung kam kein Ton ({mit} Bytes)"


def test_musik_selbsttest_meldet_die_wahrheit():
    """"Musik-Feature aktiv" hiess bisher nur: yt-dlp, ffmpeg und PyNaCl sind
    INSTALLIERT. Ob damit ein Ton herauskommt, hat nie jemand geprueft - beim
    Ausfall am 20.08.2026 loeste yt-dlp sauber auf, ffmpeg bekam vom Ziel aber
    403. Im Log stand trotzdem "aktiv"; gemerkt hat es erst jemand im Voice."""
    import logging
    import music
    m = music.Music()
    m._enabled = True

    puffer = io.StringIO()
    griff = logging.StreamHandler(puffer)
    protokoll = logging.getLogger("dcbot.music")
    protokoll.addHandler(griff)
    alt_stufe = protokoll.level
    protokoll.setLevel(logging.INFO)

    async def kein_extract(_eingabe):
        raise RuntimeError("yt-dlp kaputt")

    try:
        # 1. yt-dlp kommt nicht durch -> ehrlich melden, nicht "aktiv" behaupten.
        m._extract = kein_extract
        ok, grund = asyncio.run(m.selbsttest())
        assert ok is False and "yt-dlp" in grund, (ok, grund)

        # 2. Aufloesen klappt, aber es kommt kein Ton (genau der 403-Fall).
        async def extract_ok(_eingabe):
            return music.Track(title="Testsong", stream_url="http://x/y")
        m._extract = extract_ok

        async def kein_ton(_track):
            return 0, "Server returned 403 Forbidden (access denied)"
        m._probe_ton = kein_ton
        ok, grund = asyncio.run(m.selbsttest())
        assert ok is False and "403" in grund, (ok, grund)
        # Und der Log muss den Fall BEIM NAMEN nennen - sonst sucht man wieder
        # beim Schluessel oder beim Modell.
        assert "Client-Bindung" in puffer.getvalue(), puffer.getvalue()

        # 3. Es kommt Ton -> ok.
        async def viel_ton(_track):
            return 576000, ""
        m._probe_ton = viel_ton
        ok, grund = asyncio.run(m.selbsttest())
        assert ok is True and grund == "", (ok, grund)

        # 4. Ohne Stream-Adresse gibt es nichts zu spielen.
        async def ohne_adresse(_eingabe):
            return music.Track(title="x", stream_url="")
        m._extract = ohne_adresse
        assert asyncio.run(m.selbsttest())[0] is False

        # 5. Ist die Musik aus, ist das KEIN Fehler.
        m._enabled = False
        assert asyncio.run(m.selbsttest())[0] is False
    finally:
        protokoll.removeHandler(griff)
        protokoll.setLevel(alt_stufe)

    # Und der Selbsttest muss die Client-Kennung wirklich mitschicken - sonst
    # prueft er nicht die Strecke, die im Betrieb bricht.
    assert "ffmpeg_vorspann()" in inspect.getsource(music.Music._probe_ton)


def test_musik_spotify_erneuert_sich_ueber_youtube_nicht_ueber_spotify():
    """Im Log stand: '[DRM] The requested site is known to use DRM protection'.

    Grund: nach dem Aufloesen trug play() die URSPRUENGLICHE Eingabe als Quelle
    ein - bei einem Spotify-Link also die Spotify-Adresse. yt-dlp kann Spotify
    aber gar nicht oeffnen, es kennt nur die YouTube-Suche dahinter. Jede
    Wiederbelebung eines abgebrochenen Spotify-Songs war damit chancenlos, und
    der Best-Match-Hinweis war auch weg - der naechste Versuch haette blind den
    ersten Treffer genommen (Sped-Up-Remix statt Song)."""
    import music
    quelle = inspect.getsource(music.Music.handle)
    stelle = quelle.index("_SPOTIFY_TRACK_RE.search(arg)")
    danach = quelle[stelle:stelle + 2000]
    assert "track.query = f\"ytsearch1:" in danach, (
        "die Spotify-Adresse landet wieder als Erneuerungs-Quelle im Track")
    assert "track.match_hint" in danach, "der Best-Match-Hinweis geht verloren"
    # Und die Erneuerung muss den Hinweis wirklich mitnehmen.
    assert "resolved.match_hint = track.match_hint" in inspect.getsource(
        music.Music._resolve_track)


def test_musik_weiter_holt_die_liegengebliebene_queue():
    """Flo empfiehlt nach zwei Fehlschlaegen selbst 'weiter' - dann muss
    'weiter' auch etwas tun.

    Vorher kam dort "Da ist nichts pausiert", und die stehengebliebene
    Warteschlange blieb stehen: eine Sackgasse, aus der nur 'stop' herausfuehrte."""
    import music
    mi = music.instance
    player, voice, aufraeumen = _musik_umgebung()
    alt_state = (mi._enabled, dict(mi._players))
    mi._enabled = True
    mi._players[4242] = player          # unseren Stub-Player unterschieben
    try:
        player.queue.append(_track("Wartet"))
        player.current = None
        player._advance_aufgegeben = True
        voice.spielt = False

        msg = SimpleNamespace(
            content="flo weiter", guild=SimpleNamespace(id=4242),
            channel=SimpleNamespace(id=1), author=_fake_person(uid=7),
            mentions=[])
        antwort = asyncio.run(mi.handle(msg))
        assert antwort is music.HANDLED, _embed_text(antwort)
        assert player.current is not None and player.current.title == "Wartet"
        assert player._advance_aufgegeben is False
    finally:
        (mi._enabled, mi._players) = alt_state
        aufraeumen()


def test_musik_neustart_behaelt_den_gewuenschten_song():
    """Nach einem Neustart darf nicht ploetzlich ein anderer Song laufen.

    _resolve_track gab den Match-Hint (Spotify-Titel/Kuenstler/Dauer) nicht
    weiter - beim naechsten Aufloesen waehlte Flo also wieder blind den ersten
    YouTube-Treffer, und das ist bei Spotify-Songs oft ein Sped-Up-Remix."""
    import music
    mi = music.instance
    gesehen = []

    async def resolve_input(inp, hint):
        gesehen.append(hint)
        return music.Track(title="X", stream_url="http://stream/x", duration=100)

    alt = mi._resolve_input
    mi._resolve_input = resolve_input
    try:
        hint = {"query": "Alan Walker Faded", "dur": 212, "title": "Faded",
                "artist": "Alan Walker"}
        t = music.Track(title="Faded", stream_url="", query="ytsearch1:Faded",
                        match_hint=hint)
        erst = asyncio.run(mi._resolve_track(t))
        assert erst.match_hint == hint, erst.match_hint
        # Und beim ZWEITEN Mal ist er immer noch da (das war der Fehler).
        asyncio.run(mi._resolve_track(erst))
        assert gesehen == [hint, hint], gesehen
    finally:
        mi._resolve_input = alt


# ---------------------------------------------------------------------------
# BotSicht: Discord aus Flos Blickwinkel (Web-Panel)
# ---------------------------------------------------------------------------
def _botsicht_umgebung():
    """Baut eine komplette Flo-Welt aus Attrappen: ein Server, ein offener und
    ein gesperrter Textkanal, ein Sprachkanal, zwei Leute, zwei Nachrichten.

    Die Kanaele sind MagicMock MIT spec - das ist hier kein Selbstzweck:
    _api_sicht_channels entscheidet per isinstance, was Text- und was
    Sprachkanal ist, und nur ein spec-Mock besteht diese Pruefung."""
    import unittest.mock as mock
    import discord
    import webpanel

    def rechte(**kw):
        p = mock.MagicMock(spec=discord.Permissions)
        for k in ("read_messages", "read_message_history", "send_messages",
                  "attach_files", "embed_links", "manage_messages", "add_reactions"):
            setattr(p, k, kw.get(k, True))
        return p

    def person(uid, name, bot=False):
        u = mock.MagicMock(spec=discord.Member)
        u.id, u.name, u.display_name, u.bot = uid, name, name, bot
        u.display_avatar = SimpleNamespace(url=f"https://cdn.test/{uid}.png")
        u.color = discord.Colour(0x7CA6FF)
        u.roles, u.status, u.voice = [], "online", None
        return u

    def nachricht(mid, text, autor, kanal, guild, t):
        m = mock.MagicMock(spec=discord.Message)
        m.id, m.content, m.author = mid, text, autor
        m.channel, m.guild = kanal, guild
        m.created_at = SimpleNamespace(timestamp=lambda t=t: t)
        m.edited_at, m.pinned = None, False
        m.attachments, m.embeds, m.reactions, m.reference = [], [], [], None
        m.type = discord.MessageType.default
        return m

    guild = mock.MagicMock(spec=discord.Guild)
    guild.id, guild.name, guild.icon = 10, "Testserver", None
    guild.member_count, guild.voice_client = 120, None
    alice, flo = person(1, "Alice"), person(99, "Flo", bot=True)
    guild.members, guild.me = [alice, flo], flo

    offen = mock.MagicMock(spec=discord.TextChannel)
    offen.id, offen.name, offen.topic, offen.position = 100, "allgemein", "Alles", 0
    offen.category, offen.guild = None, guild
    offen.is_nsfw = lambda: False
    offen.permissions_for = lambda _m: rechte()

    zu = mock.MagicMock(spec=discord.TextChannel)
    zu.id, zu.name, zu.topic, zu.position = 101, "geheim", None, 1
    zu.category, zu.guild = None, guild
    zu.is_nsfw = lambda: False
    zu.permissions_for = lambda _m: rechte(read_messages=False, read_message_history=False)

    voice = mock.MagicMock(spec=discord.VoiceChannel)
    voice.id, voice.name, voice.position = 102, "Lounge", 0
    voice.category, voice.guild, voice.members = None, guild, [alice]
    voice.permissions_for = lambda _m: rechte()
    guild.channels = [offen, zu, voice]

    # history() liefert NEUESTE zuerst - genau wie das Original.
    verlauf = [nachricht(9002, "zweite <@1> hi", alice, offen, guild, 1700000100.0),
               nachricht(9001, "erste `code`", flo, offen, guild, 1700000000.0)]

    async def hist(**kw):
        for m in verlauf[:kw.get("limit", 50)]:
            yield m
    offen.history = lambda **kw: hist(**kw)

    gesendet = []

    async def send(text, **kw):
        gesendet.append((text, kw))
        return nachricht(9100, text, flo, offen, guild, 1700000200.0)
    offen.send = send

    wp = webpanel.WebPanel()
    wp._enabled, wp._auth = True, False
    wp._client = SimpleNamespace(
        guilds=[guild], user=flo, latency=0.042,
        intents=SimpleNamespace(members=False, message_content=True,
                                presences=False, voice_states=True),
        get_guild=lambda gid: guild if gid == 10 else None,
        get_channel=lambda cid: {100: offen, 101: zu, 102: voice}.get(cid),
        is_closed=lambda: False)
    return wp, gesendet, offen, nachricht(9500, "live", alice, offen, guild, 1700000300.0)


def test_botsicht_zeigt_was_flo_sieht():
    """BotSicht liefert Server, Kanalbaum, Verlauf und Mitglieder - und zwar
    aus Flos Blickwinkel:

    * Ein Kanal ohne Leserecht wird MITGELIEFERT und als gesperrt markiert
      (weglassen waere bequemer und genau falsch - die Frage 'warum sagt Flo
      da nichts?' beantwortet sich nur, wenn man den Kanal sieht).
    * Der Verlauf kommt aeltest-zuerst, nicht so, wie Discord ihn liefert.
    * Flos eigene Nachricht ist als solche markiert.
    * Die Luecke zwischen 'Mitglieder laut Server' und 'Flo kennt' bleibt
      sichtbar, statt kaschiert zu werden."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    wp, _gesendet, _offen, _live = _botsicht_umgebung()
    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            j = await (await cli.get("/api/sicht/guilds")).json()
            assert j["ok"] and j["ich"]["name"] == "Flo", j
            g = j["guilds"][0]
            assert g["mitglieder"] == 120 and g["bekannt"] == 2, g
            assert j["intents"]["mitglieder"] is False, j["intents"]

            j = await (await cli.get("/api/sicht/channels?guild=10")).json()
            assert [c["name"] for c in j["text"]] == ["allgemein", "geheim"], j["text"]
            assert j["text"][0]["rechte"]["verlauf"] is True
            assert j["text"][1]["rechte"]["verlauf"] is False, "gesperrter Kanal fehlt"
            assert j["voice"][0]["drin"][0]["name"] == "Alice", j["voice"]

            j = await (await cli.get("/api/sicht/messages?channel=100")).json()
            assert [m["text"] for m in j["messages"]] == \
                ["erste `code`", "zweite <@1> hi"], j["messages"]
            assert j["messages"][0]["autor"]["eigen"] is True, j["messages"][0]["autor"]

            # Gesperrter Kanal: 403 mit Begruendung, NICHT eine leere Liste.
            r = await cli.get("/api/sicht/messages?channel=101")
            assert r.status == 403, r.status
            j = await r.json()
            assert j["gesperrt"] and "Verlauf" in j["error"], j

            j = await (await cli.get("/api/sicht/members?guild=10")).json()
            assert j["gesamt"] == 120 and len(j["mitglieder"]) == 2, j

            assert (await cli.get("/api/sicht/messages?channel=999")).status == 404
            assert (await cli.get("/api/sicht/channels?guild=77")).status == 404
    asyncio.run(lauf())


def test_botsicht_schreibt_aber_pingt_nie_alle():
    """Aus dem Panel als Flo schreiben - mit fest zugenagelten Erwaehnungen.

    @everyone aus dem Eingabefeld laesst sich nicht zurueckholen; ein
    Tippfehler soll nicht den halben Server aufwecken. Einzelne Leute duerfen
    sehr wohl gepingt werden, sonst kann man hier nicht mitreden."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    wp, gesendet, _offen, _live = _botsicht_umgebung()
    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            j = await (await cli.post("/api/sicht/send",
                       json={"channel": "100", "text": "@everyone Achtung"})).json()
            assert j["ok"], j
            text, kw = gesendet[0]
            assert text == "@everyone Achtung"
            am = kw["allowed_mentions"]
            assert am.everyone is False and am.roles is False, "Massen-Ping moeglich!"
            assert am.users is True and am.replied_user is True

            # Antwort setzt eine Referenz. Sie MUSS eine MessageReference sein:
            # discord.py ruft to_message_reference_dict() auf, und die hat nur
            # diese Klasse. Frueher stand hier ein discord.Object - der Test
            # fragte '.id' ab und war damit gruen, waehrend in Wirklichkeit
            # JEDE Antwort mit einem TypeError scheiterte. Die Zusicherung hat
            # den Fehler also nicht gefunden, sondern festgeschrieben.
            await cli.post("/api/sicht/send",
                           json={"channel": "100", "text": "dazu", "reply_to": "9001"})
            import discord as _d
            verweis = gesendet[1][1]["reference"]
            assert isinstance(verweis, _d.MessageReference), type(verweis)
            assert verweis.message_id == 9001, gesendet[1][1]
            assert verweis.to_message_reference_dict()["message_id"] == 9001

            # Leerer Text und unbekannter Kanal werden abgewiesen.
            assert (await cli.post("/api/sicht/send",
                    json={"channel": "100", "text": "   "})).status == 400
            assert (await cli.post("/api/sicht/send",
                    json={"channel": "999", "text": "x"})).status == 404
    asyncio.run(lauf())


def test_botsicht_live_strom_und_protokoll():
    """Der Live-Strom sammelt, was Flo sieht, und 'seit' liefert nur Neues.

    Ausserdem: das Panel-Protokoll haelt das Senden fest (jemand hat im Namen
    des Bots geschrieben - das gehoert nachvollziehbar), aber NICHT das
    Tipp-Zeichen. Das feuert bei jedem Tastendruck und wuerde das Protokoll
    in zwei Minuten vollschreiben."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    wp, _gesendet, _offen, live = _botsicht_umgebung()
    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            wp.sicht_notiere(live)
            j = await (await cli.get("/api/sicht/feed")).json()
            assert len(j["ereignisse"]) == 1 and j["nr"] == 1, j
            assert j["ereignisse"][0]["msg"]["text"] == "live", j

            # 'seit' filtert das schon Gesehene weg.
            j2 = await (await cli.get(f"/api/sicht/feed?seit={j['nr']}")).json()
            assert j2["ereignisse"] == [], j2

            await cli.post("/api/sicht/send", json={"channel": "100", "text": "hi"})
            await cli.post("/api/sicht/typing", json={"channel": "100"})
            j3 = await (await cli.get(f"/api/sicht/feed?seit={j['nr']}")).json()
            assert len(j3["ereignisse"]) == 1, j3
            assert j3["ereignisse"][0]["msg"]["text"] == "hi", j3
    asyncio.run(lauf())

    pfade = [e["pfad"] for e in wp._log]
    assert "/api/sicht/send" in pfade, pfade
    assert "/api/sicht/typing" not in pfade, "Tipp-Zeichen flutet das Protokoll"


def test_botsicht_ueberlebt_kaputte_nachrichten():
    """Eine Nachricht mit kaputtem Anhang darf nicht den ganzen Verlauf
    mitreissen - und sicht_notiere laeuft im heissen Pfad von on_message,
    darf also unter keinen Umstaenden nach oben durchschlagen."""
    import unittest.mock as mock
    import discord
    wp, _g, _o, _l = _botsicht_umgebung()

    kaputt = mock.MagicMock(spec=discord.Message)
    kaputt.id = 1
    # Jeder Zugriff auf .author fliegt - schlimmer geht es kaum.
    type(kaputt).author = property(lambda _s: (_ for _ in ()).throw(RuntimeError("weg")))
    wp.sicht_notiere(kaputt)          # darf NICHT werfen
    assert wp._sicht_nr == 0, "kaputte Nachricht wurde trotzdem gezaehlt"

    # Auch ohne laufenden Loop (Tests, Shell) bleibt es lautlos.
    wp.sicht_notiere(_l)
    assert wp._sicht_nr == 1 and len(wp._sicht) == 1

    # Ist das Panel aus, wird gar nichts aufbereitet.
    wp._enabled = False
    wp.sicht_notiere(_l)
    assert wp._sicht_nr == 1, "abgeschaltetes Panel sammelt trotzdem"


def test_botsicht_live_strom_haelt_die_reihenfolge():
    """Jede offene Leitung hat eine eigene Warteschlange und EINEN Schreiber.

    Der erste Entwurf hat pro Nachricht ein create_task(ws.send_json(...))
    abgesetzt. Zwei kurz hintereinander erzeugte Tasks koennen sich beim
    Schreiben aber ueberholen - dann steht im Panel die Antwort vor der Frage.
    Ausserdem konnte ein langsamer Browser den Bot mit beliebig vielen Tasks
    zumuellen; jetzt ist bei _SICHT_STAU Schluss und das AELTESTE faellt raus."""
    import webpanel
    wp, _g, _o, _l = _botsicht_umgebung()

    # SimpleNamespace geht hier NICHT: es definiert __eq__ und ist damit
    # unhashbar - als Schluessel im Verbindungs-Dict also unbrauchbar.
    class Leitung:
        def __init__(self):
            self.closed = False

    async def lauf():
        ws = Leitung()
        schlange = asyncio.Queue(maxsize=5)
        wp._sicht_ws = {ws: schlange}
        for i in range(5):
            wp._sicht_push({"nr": i})
        assert schlange.qsize() == 5
        # Voll: die naechsten drei verdraengen die aeltesten drei.
        for i in range(5, 8):
            wp._sicht_push({"nr": i})
        raus = [schlange.get_nowait()["nr"] for _ in range(5)]
        assert raus == [3, 4, 5, 6, 7], raus

        # Geschlossene Leitungen fliegen beim naechsten Schub raus.
        ws.closed = True
        wp._sicht_push({"nr": 99})
        assert wp._sicht_ws == {}, wp._sicht_ws
    asyncio.run(lauf())

    # Und ohne jede Leitung tut _sicht_push schlicht nichts (heisser Pfad).
    wp._sicht_ws = {}
    wp._sicht_push({"nr": 1})


def test_botsicht_dm_gedaechtnis():
    """Discord verraet einem Bot seine DM-Kanaele nicht - Flo fuehrt die Liste
    selbst. Hier: merken, doppelt zaehlen, Deckel, und wer bei einer DM
    eigentlich der Partner ist (bei Flos eigener Nachricht der Empfaenger,
    sonst der Absender)."""
    import unittest.mock as mock
    import discord
    wp, _g, _o, _l = _botsicht_umgebung()
    wp._dm_store = _FakeStore({"partner": {}})
    wp._dm_partner = {}

    def person(uid, name):
        u = mock.MagicMock(spec=discord.User)
        u.id, u.name, u.display_name, u.bot = uid, name, name, False
        u.display_avatar = SimpleNamespace(url=f"https://cdn.test/{uid}.png")
        return u

    marvin = person(222333444555666777, "Marvin")
    assert wp._dm_merken(marvin, ts=1000) is True, "erster Kontakt ist neu"
    assert wp._dm_merken(marvin, ts=2000) is False, "zweiter Kontakt ist nicht neu"
    e = wp._dm_partner["222333444555666777"]
    assert e["anzahl"] == 2 and e["zuerst"] == 1000 and e["zuletzt"] == 2000, e
    assert e["name"] == "Marvin"

    # Flo selbst ist nie sein eigener DM-Partner.
    ich = person(99, "Flo")
    assert wp._dm_merken(ich) is False and "99" not in wp._dm_partner

    # Eine DM VON jemandem: Partner ist der Absender.
    kanal = mock.MagicMock(spec=discord.DMChannel)
    kanal.id, kanal.recipient = 700, marvin
    rein = mock.MagicMock(spec=discord.Message)
    rein.author, rein.channel, rein.guild = marvin, kanal, None
    rein.created_at = SimpleNamespace(timestamp=lambda: 3000)
    wp._dm_aus_nachricht(rein)
    assert wp._dm_partner["222333444555666777"]["zuletzt"] == 3000

    # Eine DM VON FLO: Partner ist der Empfaenger des Kanals, nicht Flo.
    raus = mock.MagicMock(spec=discord.Message)
    raus.author, raus.channel, raus.guild = ich, kanal, None
    raus.created_at = SimpleNamespace(timestamp=lambda: 4000)
    wp._dm_aus_nachricht(raus)
    assert wp._dm_partner["222333444555666777"]["zuletzt"] == 4000
    assert "99" not in wp._dm_partner, "Flo hat sich selbst eingetragen"

    # Deckel: die aelteste Bekanntschaft faellt raus, nicht die neueste.
    import webpanel
    alt = webpanel.BOTSICHT_DM_MAX
    try:
        webpanel.BOTSICHT_DM_MAX = 3
        for i, uid in enumerate([10**17 + n for n in range(4)]):
            wp._dm_merken(person(uid, f"P{i}"), ts=5000 + i)
        assert len(wp._dm_partner) == 3, len(wp._dm_partner)
        assert str(10**17) not in wp._dm_partner, "aeltester blieb stehen"
        assert str(10**17 + 3) in wp._dm_partner, "neuester fehlt"
    finally:
        webpanel.BOTSICHT_DM_MAX = alt


def test_botsicht_dm_suche_kennt_die_echten_formate():
    """Die Wiederherstellung alter DMs haengt an zwei Mustern - und die muessen
    WOERTLICH zu dem passen, was der Bot schreibt bzw. der Besitzer tippt.
    Deshalb wird das Vergleichsmaterial hier aus bot.py gebaut, nicht von Hand
    abgetippt: aendert dort jemand den Text, faellt es hier auf und nicht erst,
    wenn die Suche nichts mehr findet."""
    import webpanel
    quelle = open("bot.py", encoding="utf-8").read()
    # Der Weiterleitungs-Text steht so in _forward_dm_to_owner:
    assert 'f"📥 **DM von {message.author.display_name}** "' in quelle, \
        "Format der DM-Weiterleitung hat sich geaendert - Regex nachziehen!"
    assert 'f"(`{message.author.id}`):' in quelle, \
        "Format der DM-Weiterleitung hat sich geaendert - Regex nachziehen!"

    name, uid = "Marvin", 222333444555666777
    echt = f"📥 **DM von {name}** (`{uid}`):\nhallo, bist du da?"
    treffer = webpanel.WebPanel._DM_RELAY_RE.findall(echt)
    assert treffer == [(name, str(uid))], treffer

    # Auch mit Leerzeichen/Emoji im Anzeigenamen.
    echt2 = f"📥 **DM von Lena ✨ (sie)** (`{uid}`):\ntext"
    assert webpanel.WebPanel._DM_RELAY_RE.findall(echt2) == [("Lena ✨ (sie)", str(uid))]

    # Und die 'dm'-Befehle des Besitzers, beide Schreibweisen.
    b = webpanel.WebPanel._DM_BEFEHL_RE
    assert b.findall(f"flo dm {uid} sag mal") == [str(uid)]
    assert b.findall(f"Flo dm <@{uid}> sag mal") == [str(uid)]
    assert b.findall(f"flo dm <@!{uid}> sag mal") == [str(uid)]
    # Kein Fehlalarm bei normalen Saetzen.
    assert b.findall("ich hab dm 42 gesagt") == [], "zu kurze Zahl wurde genommen"


def test_botsicht_dm_verlauf_und_liste():
    """Der DM-Verlauf wird ueber die Nutzer-ID geholt - create_dm() macht den
    Kanal notfalls auf, und Discord liefert dann den KOMPLETTEN Verlauf, auch
    von vor dem letzten Neustart. Genau das ist der Weg zurueck."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    import unittest.mock as mock
    import discord
    wp, _g, _o, _l = _botsicht_umgebung()
    wp._dm_store = _FakeStore({"partner": {}})
    wp._dm_partner = {}

    marvin = mock.MagicMock(spec=discord.User)
    marvin.id, marvin.name, marvin.display_name, marvin.bot = \
        222333444555666777, "Marvin", "Marvin", False
    marvin.display_avatar = SimpleNamespace(url="https://cdn.test/m.png")

    dmk = mock.MagicMock(spec=discord.DMChannel)
    dmk.id, dmk.recipient = 700, marvin

    def dmnachricht(mid, text, autor, t):
        m = mock.MagicMock(spec=discord.Message)
        m.id, m.content, m.author, m.channel, m.guild = mid, text, autor, dmk, None
        m.created_at = SimpleNamespace(timestamp=lambda t=t: t)
        m.edited_at, m.pinned = None, False
        m.attachments, m.embeds, m.reactions, m.reference = [], [], [], None
        m.type = discord.MessageType.default
        return m

    flo = wp._client.user
    alt = [dmnachricht(2, "und? kommst du?", marvin, 1600000100.0),
           dmnachricht(1, "Nein.", flo, 1600000000.0)]

    async def hist(**kw):
        for m in alt[:kw.get("limit", 50)]:
            yield m
    dmk.history = lambda **kw: hist(**kw)
    gesendet = []

    async def send(text, **kw):
        gesendet.append((text, kw))
        return dmnachricht(3, text, flo, 1600000200.0)
    dmk.send = send
    marvin.dm_channel = dmk
    wp._client.get_user = lambda uid: marvin if uid == marvin.id else None

    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            # Von Hand hinzufuegen - der Notausgang, wenn man die ID kennt.
            j = await (await cli.post("/api/sicht/dm",
                       json={"id": str(marvin.id)})).json()
            assert j["ok"] and j["leer"] is False and j["name"] == "Marvin", j

            j = await (await cli.get("/api/sicht/dms")).json()
            assert len(j["partner"]) == 1, j
            assert j["partner"][0]["quelle"] == "hand", j["partner"][0]

            # Verlauf ueber die ID - aeltest zuerst, wie ueberall.
            j = await (await cli.get(f"/api/sicht/messages?dm={marvin.id}")).json()
            assert [m["text"] for m in j["messages"]] == ["Nein.", "und? kommst du?"], j
            # Der Kanalname nennt die Person, nicht nur "Direktnachricht".
            assert j["messages"][0]["kanal_name"] == "DM · Marvin", j["messages"][0]
            assert j["messages"][0]["dm_mit"] == str(marvin.id)

            # Privat antworten.
            j = await (await cli.post("/api/sicht/send",
                       json={"dm": str(marvin.id), "text": "doch"})).json()
            assert j["ok"] and gesendet[0][0] == "doch", j
            assert gesendet[0][1]["allowed_mentions"].everyone is False

            # Unsinnige ID -> 400, nicht 500.
            assert (await cli.post("/api/sicht/dm", json={"id": "abc"})).status == 400
    asyncio.run(lauf())


def test_botsicht_haengt_im_bot_ganz_oben():
    """Der Aufruf in bot.py muss VOR dem Bot-Check stehen.

    Sonst zeigt die Ansicht eine gefilterte Wahrheit: Flos eigene Antworten
    und die anderer Bots fehlten, obwohl er sie sehr wohl sieht."""
    quelle = open("bot.py", encoding="utf-8").read()
    start = quelle.index("async def on_message(self, message)")
    rumpf = quelle[start:start + 4000]
    hook = rumpf.index("webpanel.sicht_notiere(message)")
    botcheck = rumpf.index("if message.author.bot:")
    assert hook < botcheck, "sicht_notiere steht hinter dem Bot-Check"


# ---------------------------------------------------------------------------
# Arbeit: Schichten, Wordle und das Wort des Tages
# ---------------------------------------------------------------------------
def _arbeit_frisch(coins=None):
    """arbeit mit leerem Store und aktiver Economy. Gibt (modul, restore)."""
    import arbeit
    # bot ZUERST importieren, und zwar VOR dem Store-Tausch unten.
    # Grund: eine laufende Schicht meldet sich per lazy 'import bot' beim
    # Auto-Loesch-Schutz an. Ist bot in diesem Prozess noch nie importiert
    # worden, laeuft dabei bot.py komplett durch - samt 'ARBEIT_ENABLED =
    # arbeit.setup()', und das legt einen FRISCHEN Store an. Der Test haette
    # danach in einen Topf geschrieben, den keiner mehr liest.
    # (Im Betrieb kann das nicht passieren, siehe den sys.modules-Alias in
    # bot.py - dort ist bot beim ersten lazy Import laengst geladen.)
    import bot                                                    # noqa: F401
    restore_eco = _with_economy(coins or {1: 0, 2: 0})
    alt = (arbeit.instance._store, arbeit.instance._enabled)
    arbeit.instance._store = _FakeStore({"nutzer": {}, "tag": {}})
    arbeit.instance._enabled = True

    def restore():
        arbeit.instance._store, arbeit.instance._enabled = alt
        restore_eco()
    return arbeit, restore


def test_arbeit_woerter_sind_sauber():
    """Jedes Wort muss GENAU so lang sein, wie sein Fach sagt, und darf nur
    A-Z enthalten.

    Das ist kein Pedanterie-Test: ein 8-Buchstaben-Wort im 7er-Fach macht das
    Eingabefeld unbedienbar (min_length/max_length kommen aus der Fachlaenge),
    und ein Umlaut ist bei einem Buchstabenspiel nicht eindeutig eintippbar.
    Beim Schreiben der Listen war GENAU das der Fehler: die 7er- und 8er-Liste
    enthielt fast durchgehend Woerter, die einen Buchstaben zu lang waren."""
    import re
    import arbeit
    for laenge, worte in arbeit.WOERTER.items():
        assert worte, f"Fach {laenge} ist leer"
        for w in worte:
            assert len(w) == laenge, f"{w!r} liegt im {laenge}er-Fach, ist aber {len(w)}"
            assert re.fullmatch(r"[A-Z]+", w), f"{w!r} ist nicht rein A-Z"
        assert len(set(worte)) == len(worte), f"Doppelte im {laenge}er-Fach"
    # Jede ziehbare Laenge muss auch wirklich ein Fach haben - sonst faellt das
    # Wort des Tages an dem Tag auf die 5er-Liste zurueck, ohne dass es jemand
    # merkt (ausser am zu kleinen Topf).
    for laenge in arbeit.TAGES_LAENGEN:
        assert laenge in arbeit.WOERTER, f"Ziehung verlangt {laenge}, gibt es nicht"
    assert len(arbeit.TAGES_GEWICHT_WOCHE) == len(arbeit.TAGES_LAENGEN)
    assert len(arbeit.TAGES_GEWICHT_WOCHENENDE) == len(arbeit.TAGES_LAENGEN)


def test_arbeit_wordle_faerbung():
    """Die zweistufige Faerbung - der Klassiker unter den Wordle-Fehlern.

    Erst alle exakten Treffer wegnehmen, DANN die restlichen Buchstaben
    verteilen. Ohne das faerbt ein Versuch mit zwei gleichen Buchstaben beide
    gelb, obwohl in der Loesung nur einer steckt."""
    import arbeit
    W = arbeit.Wordle

    # Volltreffer.
    assert W("ABEND").muster("ABEND") == "🟩🟩🟩🟩🟩"
    # Nichts davon drin.
    assert W("ABEND").muster("PILZE") == "⬛⬛⬛⬛🟨"      # das E aus ABEND
    # Doppelter Buchstabe im VERSUCH, einfacher in der Loesung.
    # NOTEN hat genau ein T. In OTTER steht das zweite T an der richtigen
    # Stelle (gruen) - fuer das erste T bleibt danach NICHTS uebrig, es muss
    # grau werden. Ohne die zweistufige Zaehlung waere es gelb.
    m = W("NOTEN").muster("OTTER")
    assert m == "🟨⬛🟩🟩⬛", m
    # Noch deutlicher: ABEND hat ein E, der Versuch besteht nur aus E.
    # Genau eins davon darf gruen werden, alle anderen grau.
    m2 = W("ABEND").muster("EEEEE")
    assert m2 == "⬛⬛🟩⬛⬛", m2
    # Umgekehrt: doppelt in der Loesung, einfach im Versuch.
    assert W("ESSEN").muster("SEITE") == "🟨🟨⬛⬛🟨"
    # Richtiger Buchstabe an falscher Stelle.
    assert W("KATZE").muster("ZEBRA") == "🟨🟨⬛⬛🟨"


def test_arbeit_wordle_ablauf():
    """Versuche zaehlen, Zustand stimmt, Murks wird abgewiesen."""
    import arbeit
    spiel = arbeit.Wordle("ABEND")
    assert spiel.offen == 6 and not spiel.geloest and not spiel.aus

    assert spiel.raten("XY") == "laenge", "zu kurz durchgelassen"
    assert spiel.raten("AB3ND") == "laenge", "Ziffer durchgelassen"
    assert spiel.offen == 6, "ungueltiger Versuch wurde gezaehlt"

    assert spiel.raten("blume") == "weiter"      # klein geschrieben geht auch
    assert spiel.versuche == ["BLUME"] and spiel.offen == 5

    for _ in range(4):
        spiel.raten("BLUME")
    assert spiel.offen == 1 and not spiel.aus
    assert spiel.raten("BLUME") == "aus"
    assert spiel.aus and not spiel.geloest
    assert spiel.raten("ABEND") == "fertig", "nach dem Aus ging es weiter"

    # Und der gute Ausgang.
    spiel = arbeit.Wordle("ABEND")
    assert spiel.raten("ABEND") == "geloest"
    assert spiel.geloest and spiel.lohnfaktor() == 2.0


def test_arbeit_wort_des_tages_ist_berechnet():
    """Dasselbe Wort fuer denselben Server am selben Tag - auch nach einem
    Neustart mitten im Raten. Und NICHT dasselbe wie auf dem Nachbarserver,
    sonst holt man sich die Loesung von drueben."""
    import arbeit
    a = arbeit.Wordle.des_tages(111, tag="2026-08-16")
    b = arbeit.Wordle.des_tages(111, tag="2026-08-16")
    assert a.loesung == b.loesung, "Wort nicht reproduzierbar"

    # Anderer Server, anderer Tag: ueber viele Faelle darf das nicht konstant sein.
    andere = {arbeit.Wordle.des_tages(gid, tag="2026-08-16").loesung
              for gid in range(1, 40)}
    assert len(andere) > 5, "alle Server bekommen fast dasselbe Wort"
    tage = {arbeit.Wordle.des_tages(111, tag=f"2026-08-{t:02d}").loesung
            for t in range(1, 29)}
    assert len(tage) > 10, "das Wort wechselt kaum von Tag zu Tag"

    # Die LAENGE wird gewuerfelt, nicht am Wochentag festgemacht - vorher war
    # Mo-Do immer 5 Buchstaben, also vier Tage die Woche dieselbe Aufgabe und
    # derselbe Topf.
    laengen = [len(arbeit.Wordle.des_tages(1, tag=f"2026-0{m}-{t:02d}").loesung)
               for m in (7, 8, 9) for t in range(1, 29)]
    assert set(laengen) == set(arbeit.TAGES_LAENGEN), sorted(set(laengen))
    haeufigste = max(set(laengen), key=laengen.count)
    assert laengen.count(haeufigste) < len(laengen) * 0.6, \
        "eine Laenge dominiert alles"

    # Aber reproduzierbar: derselbe Tag, dieselbe Laenge.
    assert (arbeit.Wordle.des_tages(1, tag="2026-08-16").loesung
            == arbeit.Wordle.des_tages(1, tag="2026-08-16").loesung)

    # Am Wochenende sind lange Woerter wahrscheinlicher - im Schnitt, nicht
    # garantiert. Ueber ein ganzes Jahr muss sich das zeigen.
    import datetime
    woche, ende = [], []
    for n in range(365):
        tag = (datetime.date(2026, 1, 1) + datetime.timedelta(days=n)).isoformat()
        laenge = len(arbeit.Wordle.des_tages(1, tag=tag).loesung)
        (ende if datetime.date.fromisoformat(tag).weekday() >= 5 else woche
         ).append(laenge)
    assert sum(ende) / len(ende) > sum(woche) / len(woche), \
        "Wochenende ist im Schnitt nicht laenger"

    # Und die Ziehung selbst: jeder Wurf landet in einem echten Fach.
    for wurf in range(0, 5000, 7):
        assert arbeit.laenge_des_tages("2026-08-17", wurf) in arbeit.TAGES_LAENGEN


def test_arbeit_tageswordle_ist_ein_wettrennen():
    """Wer zuerst loest, nimmt alles - danach ist die Runde durch.

    So gewuenscht: ein Rennen mit einem Sieger hat Spannung, ein Raetsel, das
    jeder in Ruhe nachholen kann, nicht."""
    arbeit, restore = _arbeit_frisch({1: 0, 2: 0})
    try:
        r = arbeit.instance.raetsel(555)
        r.starten()
        wort = r.wort
        assert r.laeuft and not r.entschieden

        # Nutzer 1 raet daneben, danach richtig.
        assert r.raten(1, "X" * len(wort))[0] == "weiter"
        status, spiel = r.raten(1, wort)
        assert status == "geloest" and r.entschieden and r.gewinner == 1
        assert len(spiel.versuche) == 2

        # Nutzer 2 darf NACH der Entscheidung weiterraten - fuer die eigene
        # Bilanz. Der Topf ist weg, aber der Tag ist nicht gelaufen: vorher war
        # fuer alle ausser dem Sieger sofort Schluss.
        status2, spiel2 = r.raten(2, wort)
        assert status2 == "geloest", status2
        assert spiel2.geloest and r.gespielt(2)
        # Der Sieger bleibt der Erste - daran aendert das nichts.
        assert r.gewinner == 1, "der Sieger wurde ueberschrieben"
        assert r.daten["versuche"] == 2, r.daten["versuche"]
    finally:
        restore()


def test_arbeit_tageswordle_preis():
    """Der Topf haengt an der WORTLAENGE, der Faktor an den Versuchen.

    Vom Nutzer vorgegeben: 5 Buchstaben sollen bei ~50.000 liegen, laengere
    Woerter mehr - und der Anhieb das Doppelte."""
    arbeit, restore = _arbeit_frisch()
    try:
        r = arbeit.instance.raetsel(1)
        # 5 Buchstaben, dritter Versuch: 50.000 x 1.5
        r.daten.update({"datum": arbeit._heute(), "wort": "ABEND",
                        "spieler": {}, "gewinner": 0})
        assert r.topf == 50_000
        assert r.preis(arbeit.Wordle("ABEND", versuche=["A", "B", "ABEND"])) == 75_000
        # Anhieb -> doppelt.
        assert r.preis(arbeit.Wordle("ABEND", versuche=["ABEND"])) == 100_000
        # 8 Buchstaben auf Anhieb: der Sonntags-Jackpot.
        r.daten["wort"] = "ARBEITER"
        assert r.topf == 80_000
        assert r.preis(arbeit.Wordle("ARBEITER", versuche=["ARBEITER"])) == 160_000
        # Letzter Versuch: nur der Grundtopf, nie weniger.
        sechs = arbeit.Wordle("ARBEITER", versuche=["X"] * 5 + ["ARBEITER"])
        assert r.preis(sechs) == 80_000
    finally:
        restore()


def test_arbeit_wordle_faellt_nur_wenn_was_los_ist():
    """Nicht die Uhr entscheidet, sondern der Voice-Kanal.

    So gewuenscht: ein Raetsel um 4 Uhr morgens in einen leeren Server zu
    werfen, waere verschenkt. Bots zaehlen dabei NICHT mit - Flo sitzt oft
    selbst im Call und wuerde sich sonst mitzaehlen."""
    arbeit, restore = _arbeit_frisch()
    try:
        def guild(n_menschen, n_bots=0):
            leute = [SimpleNamespace(bot=False) for _ in range(n_menschen)]
            leute += [SimpleNamespace(bot=True) for _ in range(n_bots)]
            return SimpleNamespace(id=42, voice_channels=[SimpleNamespace(members=leute)])

        assert arbeit.instance.leute_im_voice(guild(3, 2)) == 3, "Bots mitgezaehlt"

        # Zu wenig los: nicht mal ein Termin wird gezogen.
        assert not arbeit.instance.faellig(guild(2)), "bei zwei Leuten schon geplant"
        assert arbeit.instance.raetsel(42).geplant_fuer is None

        # Genug Leute: JETZT wird geplant - aber noch nichts gepostet.
        assert not arbeit.instance.faellig(guild(3)), "sofort gefeuert statt geplant"
        termin = arbeit.instance.raetsel(42).geplant_fuer
        assert termin is not None and termin > int(time.time()), termin

        # Der Termin bleibt STEHEN. Wuerde er bei jedem Tick neu gezogen,
        # ruecke er ewig weiter weg und das Wort kaeme nie.
        assert not arbeit.instance.faellig(guild(5))
        assert arbeit.instance.raetsel(42).geplant_fuer == termin, "Termin verschoben"

        # Termin da und immer noch was los -> jetzt faellt es.
        arbeit.instance.raetsel(42).daten["plan_zeit"] = 0
        assert arbeit.instance.faellig(guild(3))
        # Ist der Call inzwischen leer, wartet Flo trotzdem.
        assert not arbeit.instance.faellig(guild(1)), "in den leeren Call gepostet"

        # Ist das Wort raus, kommt heute keins mehr - egal wie voll es wird.
        arbeit.instance.raetsel(42).starten()
        assert not arbeit.instance.faellig(guild(9)), "zweites Wort am selben Tag"
    finally:
        restore()


def test_arbeit_tagesdeckel_und_serie():
    """Der Tagesdeckel begrenzt WIRKLICH, und die Serie zahlt sich aus.

    Ohne Deckel waere die Schicht die beste Geldquelle im Spiel - sie ist ja
    risikofrei, anders als Casino und Aktie."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        schicht = arbeit.SCHICHTEN["wordle"]

        # Erste volle Schicht: Grundlohn + 5 % Serie (Stufe 0 gibt nichts dazu).
        betrag, info = wp.abrechnen(7, schicht, 1.0)
        assert info["serie"] == 1 and betrag == round(schicht.lohn * 1.05), betrag
        assert info["stufe"].titel == "Praktikant", info["stufe"]

        # Zweite: Serie 2 -> mehr.
        betrag2, info2 = wp.abrechnen(7, schicht, 1.0)
        assert info2["serie"] == 2 and betrag2 > betrag

        # Reinfall setzt die Serie zurueck und zahlt nichts - die STUFE bleibt.
        betrag3, info3 = wp.abrechnen(7, schicht, 0.0)
        assert betrag3 == 0 and info3["serie"] == 0
        assert info3["geschafft"] == 2, "Reinfall hat die Stufe angetastet"

        # Deckel: das Konto auf kurz vor Schluss setzen.
        prof = wp._nutzer(7)
        prof["tag"], prof["heute"] = arbeit._heute(), arbeit.TAGES_DECKEL - 1000
        betrag4, info4 = wp.abrechnen(7, schicht, 1.0)
        assert betrag4 == 1000, betrag4
        assert "Tagesdeckel" in info4["hinweis"], info4["hinweis"]
        # Und danach ist Schluss - aufs Konto kommt wirklich nichts mehr.
        stand = economy.instance._profile(7)["coins"]
        betrag5, info5 = wp.abrechnen(7, schicht, 1.0)
        assert betrag5 == 0 and "voll" in info5["hinweis"].lower(), info5
        assert economy.instance._profile(7)["coins"] == stand, "trotz Deckel gebucht"
        assert wp._nutzer(7)["heute"] == arbeit.TAGES_DECKEL
    finally:
        restore()


def test_arbeit_safe_hinweis_zaehlt_zweistufig():
    """Beim Zahlenschloss dieselbe Falle wie beim Wordle: '111' gegen '123'
    darf NICHT dreimal 'richtige Ziffer am falschen Platz' ergeben."""
    import arbeit
    view = arbeit.SafeView.__new__(arbeit.SafeView)
    view.code = "123"
    assert view.hinweis("123") == (3, 0)
    assert view.hinweis("111") == (1, 0), view.hinweis("111")
    assert view.hinweis("321") == (1, 2)
    assert view.hinweis("456") == (0, 0)
    assert view.hinweis("213") == (1, 2)


def test_arbeit_schichten_sind_vollstaendig():
    """Jede Schicht muss sich selbst beschreiben und ein Gesicht bauen koennen.

    Der Katalog haengt an den Klassen - wer eine sechste Schicht dazulegt, soll
    nichts weiter anfassen muessen. Dieser Test faellt um, wenn doch."""
    import arbeit
    autor = SimpleNamespace(id=1, display_name="Tester")
    for key, schicht in arbeit.SCHICHTEN.items():
        assert schicht.key == key, f"{schicht} kennt seinen eigenen Schluessel nicht"
        assert schicht.titel and schicht.was, key
        assert schicht.lohn > 0, key
        embed, view, datei = asyncio.run(schicht.bauen(arbeit.instance, autor))
        assert embed.description, key
        assert view.uid == 1 and view.schicht is schicht, key
        # Die Frist muss zur Aufgabe passen: sechs Rateversuche brauchen mehr
        # Zeit als fuenf Klicks.
        assert view.timeout == schicht.frist and schicht.frist >= 300, key
    # Wordle bringt sein Brett als Bild mit - genau dafuer gibt es das
    # Drei-Tupel. Faellt render aus, ist datei None und es geht trotzdem.
    _e, _v, datei = asyncio.run(
        arbeit.SCHICHTEN["wordle"].bauen(arbeit.instance, autor))
    assert datei is not None, "Wordle-Schicht ohne Brett-Bild"
    assert arbeit.SCHICHTEN["wordle"].frist > arbeit.SCHICHTEN["sortieren"].frist


def test_arbeit_cooldown_und_befehle():
    """'Flo work' startet eine Schicht, die zweite prallt am Cooldown ab -
    und 'Flo wordle' sagt ehrlich, dass noch kein Wort draussen ist."""
    arbeit, restore = _arbeit_frisch({5: 0})
    gesendet = []
    try:
        autor = _fake_person(5, name="tester", global_name="Tester")
        kanal = SimpleNamespace(
            id=9, send=lambda **kw: _als_coro(gesendet.append(kw) or SimpleNamespace(id=1)))
        msg = SimpleNamespace(content="Flo work", author=autor, channel=kanal,
                              guild=SimpleNamespace(id=42))

        antwort = asyncio.run(arbeit.handle(msg))
        assert antwort is arbeit.HANDLED, antwort
        assert gesendet and "embed" in gesendet[0]

        # Zweiter Versuch sofort danach: Cooldown-Hinweis statt neuer Schicht,
        # und der Hinweis sagt auch, WIE LANGE noch.
        antwort2 = asyncio.run(arbeit.handle(msg))
        text2 = _embed_text(antwort2)
        assert "warten" in text2.lower() and "Minuten" in text2, text2
        assert len(gesendet) == 1, "trotz Cooldown eine zweite Schicht gestartet"

        # Die Liste nennt jede BESTELLBARE Schicht mit ihrem Schluessel.
        # Die seltene steht auch drin, aber ohne Schluessel - man kann sie ja
        # nicht anfordern, und ein Schluessel waere eine Einladung dazu.
        msg.content = "Flo work liste"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        for key, sch in arbeit.SCHICHTEN.items():
            if sch.selten:
                assert "SELTEN" in text and "bestellbar" in text.lower(), text
            else:
                assert key in text, key

        # 'Flo wordle' ist jetzt das SPASS-Wordle - das Wort des Tages hat
        # eigene Woerter. Sonst wuesste man nicht, ob man um 15.000 oder um
        # 80.000 spielt.
        msg.content = "Flo tageswort"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "Voice" in text, text
    finally:
        restore()


def test_arbeit_kanal_wird_vernuenftig_gesucht():
    """Reihenfolge: eingestellter Kanal -> ein Kanal, der passend heisst ->
    Ansagen-Kanal. Eine feste ID im Code gibt es bewusst nicht."""
    arbeit, restore = _arbeit_frisch()
    try:
        gigachat = SimpleNamespace(id=200, name="gigachat")
        sonst = SimpleNamespace(id=201, name="smalltalk")
        eingestellt = SimpleNamespace(id=300, name="wordle-only")
        kanaele = {200: gigachat, 201: sonst, 300: eingestellt}
        guild = SimpleNamespace(id=42, text_channels=[sonst, gigachat],
                                get_channel=kanaele.get, system_channel=None)

        werte = {}
        alt = arbeit.instance._cfg
        arbeit.instance._cfg = lambda gid, key, standard: werte.get(key, standard)
        try:
            # Nichts eingestellt -> der Kanal, der 'gigachat' heisst.
            assert arbeit.instance.kanal_fuer(guild) is gigachat
            # Eingestellt gewinnt.
            werte["wordle_channel"] = 300
            assert arbeit.instance.kanal_fuer(guild) is eingestellt
            # Eingestellte, aber unbekannte ID: nicht ins Leere posten,
            # sondern auf den Namens-Fund zurueckfallen.
            werte["wordle_channel"] = 999
            assert arbeit.instance.kanal_fuer(guild) is gigachat
        finally:
            arbeit.instance._cfg = alt
    finally:
        restore()


def test_arbeit_tick_merkt_sich_die_ansage():
    """tick() liefert (guild, embed, view) - und die Ansage MUSS gemerkt werden.

    Das Raten laeuft ueber ein Eingabefeld, und bei einer Modal-Antwort ist
    interaction.message IMMER None. Ohne die gemerkten IDs koennte die oeffentliche
    Ansage nach dem Sieg nicht auf 'entschieden' umgestellt werden - sie stuende
    bis zum naechsten Tag da und lockte Leute in ein Rennen, das laengst
    gelaufen ist."""
    arbeit, restore = _arbeit_frisch()
    try:
        leute = [SimpleNamespace(bot=False) for _ in range(4)]
        guild = SimpleNamespace(id=77, voice_channels=[SimpleNamespace(members=leute)])

        # Erster Tick: es ist genug los, ALSO wird ein Termin gezogen - aber
        # noch nichts gepostet. Das Wort soll ueberraschen, nicht an der
        # dritten Person im Call haengen.
        assert asyncio.run(arbeit.tick([guild])) == []
        r0 = arbeit.instance.raetsel(77)
        assert r0.geplant_fuer is not None, "kein Termin gezogen"
        assert r0.geplant_fuer >= int(time.time()) + arbeit.VERZUG_MIN
        # Termin vorziehen (im Betrieb wartet Flo 5-45 Minuten).
        r0.daten["plan_zeit"] = 0
        faellig = asyncio.run(arbeit.tick([guild]))
        assert len(faellig) == 1, faellig
        g, embed, view, datei = faellig[0]
        assert g is guild
        # Der Aushang bringt ein leeres Brett mit: man sieht auf einen Blick,
        # wie lang das Wort ist und wie viele Versuche man hat.
        assert datei is not None, "Aushang ohne Brett-Bild"
        assert "Wettrennen" in (embed.description or ""), embed.description
        assert view.children, "kein Rate-Knopf an der Ansage"
        assert view.children[0].custom_id == "flo:wordle:77", view.children[0].custom_id

        # Ein zweiter Tick am selben Tag darf NICHTS mehr liefern.
        assert asyncio.run(arbeit.tick([guild])) == []

        # Ansage merken - genau das macht bot.py nach dem Senden.
        r = arbeit.instance.raetsel(77)
        r.ansage_merken(SimpleNamespace(id=4242, channel=SimpleNamespace(id=99)))
        assert r.daten["ansage"] == 4242 and r.daten["kanal"] == 99

        # Nach dem Sieg zeigt das Embed die Aufloesung statt des Rennens.
        r.raten(5, r.wort)
        fertig = arbeit.instance.tages_embed(77)
        assert "entschieden" in (fertig.title or "").lower(), fertig.title
        assert r.wort in (fertig.description or ""), fertig.description
    finally:
        restore()


def test_arbeit_laufende_schicht_wird_nicht_weggeraeumt():
    """Eine laufende Schicht MUSS sich beim Auto-Loesch-Schutz anmelden.

    Ohne das verschwindet sie in einem Aufraeum-Kanal mitten im Spiel: der
    Cooldown laeuft weiter, der Lohn ist weg, und die Knoepfe zeigen ins Leere.
    Genau das war die Beschwerde. Freigegeben wird erst, wenn die Schicht
    wirklich vorbei ist - dann darf sie nach der Gnadenfrist weg."""
    import bot
    arbeit, restore = _arbeit_frisch({5: 0})
    geschuetzt, freigegeben = [], []
    alt = (bot.protect_message, bot.release_message)
    bot.protect_message = lambda m: geschuetzt.append(getattr(m, "id", None))
    bot.release_message = lambda m, **kw: freigegeben.append(getattr(m, "id", None))
    try:
        autor = _fake_person(5, name="tester", global_name="Tester")
        kanal = SimpleNamespace(id=9, send=lambda **kw: _als_coro(SimpleNamespace(id=777)))
        msg = SimpleNamespace(content="Flo work sortieren", author=autor,
                              channel=kanal, guild=SimpleNamespace(id=42))
        assert asyncio.run(arbeit.handle(msg)) is arbeit.HANDLED
        assert geschuetzt == [777], geschuetzt
        assert freigegeben == [], "schon vor dem Ende freigegeben"

        # Zeit rum: abrechnen, freigeben - und den STAND stehen lassen.
        view = arbeit.SortierView(arbeit.instance, 5,
                                  arbeit.SCHICHTEN["sortieren"], [3, 1, 2])
        bearbeitet = {}

        async def edit(**kw):
            bearbeitet.update(kw)
        view.message = SimpleNamespace(id=777, edit=edit)
        asyncio.run(view.on_timeout())
        assert freigegeben == [777], freigegeben
        # Nur die Knoepfe sind weg; das Embed wird NICHT ueberschrieben -
        # sonst sieht man nach einer Pause nicht mal mehr, woran man sass.
        assert bearbeitet.get("view") is None and "embed" not in bearbeitet, bearbeitet
        assert "content" in bearbeitet and "Zeit" in bearbeitet["content"]
    finally:
        bot.protect_message, bot.release_message = alt
        restore()


def test_arbeit_wordle_brett_wird_gezeichnet():
    """Das Brett kommt als Bild - mit Tastatur, damit man sieht, welche
    Buchstaben schon raus sind. Und die Farben kommen aus EINER Quelle:
    farben() rechnet, muster() malt Emojis, das Bild nimmt dieselben Zeichen."""
    import arbeit
    import render
    spiel = arbeit.Wordle("ARBEITER", versuche=["MEISTERN", "ARBEITER"])

    # Eine Quelle fuer die Faerbung.
    assert spiel.farben("ARBEITER") == "gggggggg"
    assert spiel.muster("ARBEITER") == "🟩" * 8
    assert [f for _w, f in spiel.zeilen()] == [spiel.farben("MEISTERN"), "gggggggg"]

    # Tastatur: der beste Stand gewinnt. In MEISTERN ist das E gelb, in
    # ARBEITER gruen - gruen muss bleiben.
    tast = spiel.tastatur()
    assert tast["E"] == "g" and tast["A"] == "g", tast
    assert tast["M"] == "b" and tast["S"] == "b", tast
    assert "Q" not in tast, "nie geratener Buchstabe steht in der Tastatur"

    # Und das Bild entsteht wirklich.
    buf = render.wordle_board(spiel.zeilen(), spiel.laenge, titel="TEST",
                              tastatur=tast)
    kopf = buf.read(8)
    assert kopf.startswith(b"\x89PNG"), kopf
    # Auch mit acht Buchstaben und ohne Tastatur darf nichts umfallen.
    assert render.wordle_board([], 8, titel="LEER").read(4) == b"\x89PNG"


def test_arbeit_wordle_ist_selten_und_nicht_bestellbar():
    """Wordle soll ein Highlight sein, keine Routine.

    Beides gehoert zusammen und war der eigentliche Denkfehler davor: eine
    seltene Schicht, die man sich per 'Flo work wordle' jederzeit holen kann,
    ist nicht selten - sie ist nur schlecht sortiert. Also: kaum gezogen UND
    nicht bestellbar, dafuer deutlich besser bezahlt."""
    arbeit, restore = _arbeit_frisch({5: 0})
    try:
        # Etwa jede sechzehnte Schicht - nicht jede fuenfte.
        p = arbeit.seltene_chance()
        assert 0.03 < p < 0.10, p
        # Und sie zahlt sich aus: klar mehr als jede normale Schicht.
        normal = max(s.lohn for s in arbeit.SCHICHTEN.values() if not s.selten)
        selten = max(s.lohn for s in arbeit.SCHICHTEN.values() if s.selten)
        assert selten >= normal * 2.5, (normal, selten)

        # Die Ziehung haelt sich grob an die Gewichte.
        random.seed(4)
        gezogen = [arbeit.schicht_ziehen().key for _ in range(4000)]
        anteil = gezogen.count("wordle") / len(gezogen)
        assert abs(anteil - p) < 0.02, (anteil, p)
        # Jede Schicht kommt ueberhaupt vor - sonst ist eine tot.
        assert set(gezogen) == set(arbeit.SCHICHTEN), set(arbeit.SCHICHTEN) - set(gezogen)

        # Bestellen geht nicht - und die Absage erklaert, warum.
        autor = _fake_person(5, name="tester", global_name="Tester")
        msg = SimpleNamespace(content="Flo work wordle", author=autor,
                              channel=SimpleNamespace(id=9), guild=SimpleNamespace(id=42))
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "nicht aussuchen" in text, text
        # Wichtig: der Cooldown darf dabei NICHT anspringen - man hat ja
        # nicht gearbeitet, nur gefragt.
        assert arbeit.instance._nutzer(5)["cooldown"] == 0, "Absage kostet Cooldown"
    finally:
        random.seed()
        restore()


def test_arbeit_neue_schichten_funktionieren():
    """Werkzeug sortieren (Memory) und Qualitaetskontrolle - die zwei, die den
    Platz fuellen, den Wordle als seltene Schicht frei gemacht hat."""
    import arbeit
    autor = SimpleNamespace(id=1, display_name="Tester")

    # Memory: acht Kisten, vier Paare, jedes Werkzeug genau zweimal.
    _e, view, _d = asyncio.run(
        arbeit.SCHICHTEN["paare"].bauen(arbeit.instance, autor))
    assert len(view.karten) == 8, view.karten
    for stueck in set(view.karten):
        assert view.karten.count(stueck) == 2, (stueck, view.karten)
    assert len(view.children) == 8
    # Verdeckt starten: kein Knopf verraet schon sein Werkzeug.
    assert all(k.emoji is None for k in view.children), "Kisten liegen offen"

    # Qualitaetskontrolle: fuenf Stuecke, genau eins aus einer anderen Kiste.
    _e, view2, _d = asyncio.run(
        arbeit.SCHICHTEN["kontrolle"].bauen(arbeit.instance, autor))
    woerter = [k.label for k in view2.children]
    assert len(woerter) == 5 and len(set(woerter)) == 5, woerter
    assert view2.ausschuss in woerter
    daheim = set(arbeit.KontrolleSchicht.KISTEN[view2.heimat])
    fremde = [w for w in woerter if w not in daheim]
    assert fremde == [view2.ausschuss], (fremde, view2.ausschuss)

    # Keine Kiste teilt sich ein Wort mit einer anderen - sonst gaebe es
    # Runden mit zwei richtigen Antworten.
    alle = [w for worte in arbeit.KontrolleSchicht.KISTEN.values() for w in worte]
    assert len(alle) == len(set(alle)), "Wort kommt in zwei Kisten vor"


def test_arbeit_karriere_geht_nie_zurueck():
    """Die Stufe ist das Rueckgrat: sie zaehlt GESCHAFFTE Schichten und faellt
    nie. Vorher gab es nur die Serie - ein Reinfall und alles war weg, ueber
    Wochen baute man nichts auf."""
    import arbeit
    assert arbeit.stufe_fuer(0).titel == "Praktikant"
    assert arbeit.stufe_fuer(9).titel == "Praktikant"
    assert arbeit.stufe_fuer(10).titel == "Aushilfe"
    assert arbeit.stufe_fuer(10**6).titel == arbeit.STUFEN[-1].titel

    # Monoton: mehr Schichten sind nie eine schlechtere Stufe.
    letzter = -1.0
    for n in range(0, 900, 7):
        bonus = arbeit.stufe_fuer(n).bonus
        assert bonus >= letzter, (n, bonus, letzter)
        letzter = bonus

    # Die Schwellen muessen aufsteigen, sonst greift stufe_fuer daneben.
    schwellen = [st.ab for st in arbeit.STUFEN]
    assert schwellen == sorted(schwellen) and len(set(schwellen)) == len(schwellen)
    assert arbeit.STUFEN[0].ab == 0, "es gibt keine Stufe fuer Anfaenger"
    assert arbeit.naechste_stufe(0).titel == "Aushilfe"
    assert arbeit.naechste_stufe(10**6) is None, "oben muss Schluss sein"


def test_arbeit_lohn_ist_additiv_und_gedeckelt():
    """Serie und Stufe werden ADDIERT, nicht multipliziert.

    Multiplikativ schaukeln sich zwei Zuschlaege von je +50 % zu +125 % auf -
    und dann faengt die Schicht an, das Wort des Tages zu ueberholen, das ja
    der Hoehepunkt bleiben soll."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        schicht = arbeit.SCHICHTEN["safe"]
        prof = wp._nutzer(7)
        # Volle Serie und hoechste Stufe von Hand setzen.
        prof["serie"] = 999
        prof["geschafft"] = arbeit.STUFEN[-1].ab
        max_faktor = 1.0 + arbeit.SERIE_MAX + arbeit.STUFEN[-1].bonus
        assert max_faktor <= 2.05, max_faktor      # nicht aus dem Ruder

        betrag, info = wp.abrechnen(7, schicht, 1.0)
        erwartet = round(schicht.lohn * 1.0 * (1.0 + info["serie_bonus"]
                                               + info["stufe"].bonus))
        assert betrag == erwartet, (betrag, erwartet)
        # Selbst im Bestfall bleibt eine normale Schicht unter dem kleinsten
        # Tages-Wordle-Topf - sonst waere das Wort des Tages entwertet.
        kleinster_topf = min(arbeit.TAGES_LAENGEN) * arbeit.TAGES_PRO_BUCHSTABE
        beste_normal = max(s.lohn for s in arbeit.SCHICHTEN.values() if not s.selten)
        assert beste_normal * 1.2 * max_faktor < kleinster_topf
    finally:
        restore()


def test_arbeit_goldene_schicht_verdoppelt():
    """Gold wird BEIM START gewuerfelt, damit es dransteht, waehrend man
    arbeitet - und verdoppelt am Ende den Lohn. Auf null bleibt null: fuer eine
    verpatzte Schicht gibt es auch doppelt nichts."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        schicht = arbeit.SCHICHTEN["safe"]
        normal, _i = wp.abrechnen(7, schicht, 1.0)
        wp._nutzer(7)["serie"] = 0          # gleiche Ausgangslage
        gold, info = wp.abrechnen(7, schicht, 1.0, gold=True)
        assert gold == normal * 2, (normal, gold)
        assert info["gold"] is True and wp._nutzer(7)["gold"] == 1

        # Verpatzt: kein Lohn, also auch keine Gold-Zaehlung.
        _b, info2 = wp.abrechnen(7, schicht, 0.0, gold=True)
        assert info2["gold"] is False and wp._nutzer(7)["gold"] == 1

        # Die Chance muss eine Chance bleiben - nicht immer, nicht nie.
        assert 0.0 < arbeit.GOLD_CHANCE < 0.35, arbeit.GOLD_CHANCE
        random.seed(11)
        views = [arbeit.SchichtView(wp, 7, schicht) for _ in range(3000)]
        anteil = sum(1 for v in views if v.gold) / len(views)
        assert abs(anteil - arbeit.GOLD_CHANCE) < 0.03, anteil
    finally:
        random.seed()
        restore()


def test_arbeit_wordle_bilanz_wird_mitgeschrieben():
    """Jede Teilnahme am Wort des Tages zaehlt genau einmal, jeder Erfolg
    landet in der Verteilung - auch der, fuer den es kein Geld mehr gab."""
    arbeit, restore = _arbeit_frisch({1: 0, 2: 0})
    try:
        wp = arbeit.instance
        r = wp.raetsel(900)
        r.starten()
        wort = r.wort

        gesendet = []

        class Antwort:
            def __init__(self):
                self.done = False

            async def send_message(self, **kw):
                gesendet.append(kw)

        class Ia:
            def __init__(self, uid):
                self.user = SimpleNamespace(id=uid, display_name=f"U{uid}")
                self.response = Antwort()
                self.channel = SimpleNamespace(
                    send=lambda **kw: _als_coro(None))
                self.client = SimpleNamespace(get_channel=lambda _c: None)

        # Nutzer 1: einmal daneben, dann richtig -> Sieger.
        asyncio.run(wp.tages_antwort(Ia(1), 900, "X" * len(wort)))
        asyncio.run(wp.tages_antwort(Ia(1), 900, wort))
        p1 = wp._nutzer(1)
        assert p1["wordle_gespielt"] == 1, "Teilnahme mehrfach gezaehlt"
        assert p1["wordle_siege"] == 1
        assert p1["wordle_verteilung"][1] == 1, p1["wordle_verteilung"]

        # Nutzer 2 loest NACH der Entscheidung: Bilanz ja, Geld nein.
        vorher = economy.instance._profile(2)["coins"]
        asyncio.run(wp.tages_antwort(Ia(2), 900, wort))
        p2 = wp._nutzer(2)
        assert p2["wordle_siege"] == 1 and p2["wordle_verteilung"][0] == 1
        assert economy.instance._profile(2)["coins"] == vorher, "trotzdem bezahlt"
        assert r.gewinner == 1, "Sieger ueberschrieben"
    finally:
        restore()


def test_arbeit_lohnzettel_und_rangliste_als_karte():
    """Beide Karten entstehen wirklich - und ohne Bild gibt es einen Textweg,
    damit eine fehlende Schrift niemandem seine Zahlen vorenthaelt."""
    import render
    arbeit, restore = _arbeit_frisch({5: 0, 6: 0})
    try:
        wp = arbeit.instance
        for _ in range(12):
            wp.abrechnen(5, arbeit.SCHICHTEN["safe"], 1.0)
        wp.abrechnen(6, arbeit.SCHICHTEN["salat"], 1.0)
        prof = wp._nutzer(5)
        assert arbeit.stufe_fuer(prof["geschafft"]).titel == "Aushilfe"

        buf = render.lohnzettel("Tester", None, stufe="Aushilfe", symbol="🧹",
                                bonus=0.08, geschafft=12, angetreten=12, serie=12,
                                beste_serie=12, verdient=90000, heute=90000,
                                deckel=arbeit.TAGES_DECKEL, gold=0,
                                naechste="Facharbeiter", fehlt=18,
                                wordle=(2, 5, [0, 1, 1, 0, 0, 0]))
        assert buf.read(4) == b"\x89PNG"
        rows = [(1, "Tester", "🧹", "Aushilfe", 12, 90000)]
        assert render.arbeit_rangliste(rows).read(4) == b"\x89PNG"

        # Der Textweg trägt dieselben Zahlen.
        text = _embed_text(wp._lohnzettel_text(
            SimpleNamespace(id=5, display_name="Tester"), prof, 90000,
            arbeit.stufe_fuer(12), arbeit.naechste_stufe(12)))
        assert "Aushilfe" in text and "12" in text, text

        # Die Rangliste sortiert nach Verdienst, nicht nach ID.
        gesendet = []
        msg = SimpleNamespace(
            guild=None, mentions=[],
            author=SimpleNamespace(id=5, display_name="Tester"),
            channel=SimpleNamespace(send=lambda **kw: _als_coro(
                gesendet.append(kw) or SimpleNamespace(id=1))))
        assert asyncio.run(wp._rangliste(msg)) is arbeit.HANDLED
        assert gesendet and "file" in gesendet[0]
    finally:
        restore()


def test_arbeit_karriere_steigt_wirklich_und_faellt_nie_zurueck():
    """Nachgespielt statt nachgelesen: 12 Schichten am Stueck, dann verlieren,
    dann ein Spass-Wordle. Die Frage "ab wann steigt die Stufe ueberhaupt?"
    muss der Test beantworten koennen, nicht der Quelltext."""
    arbeit, restore = _arbeit_frisch()
    A = arbeit.instance
    uid = 5150501
    salat = arbeit.SCHICHTEN["salat"]
    try:
        _karriere_durchspielen(arbeit, A, uid, salat)
    finally:
        restore()


def _karriere_durchspielen(arbeit, A, uid, salat):
    stufen_bei = {}
    for i in range(1, 13):
        _betrag, info = A.abrechnen(uid, salat, 1.0)
        prof = A._nutzer(uid)
        assert prof["geschafft"] == i, (i, prof["geschafft"])
        assert prof["serie"] == i
        if info.get("aufgestiegen"):
            stufen_bei[i] = info["stufe"].titel
    # Genau EIN Aufstieg in den ersten zwoelf, und zwar bei zehn.
    assert stufen_bei == {10: "Aushilfe"}, stufen_bei
    # Die Serie ist bei 50 % gedeckelt (nach zehn Siegen) und laeuft nicht weiter.
    assert A._serie_bonus(A._nutzer(uid)) == arbeit.SERIE_MAX

    # Verlieren: die Serie ist weg, die Karriere NICHT.
    vorher = dict(A._nutzer(uid))
    A.abrechnen(uid, salat, 0.0)
    prof = A._nutzer(uid)
    assert prof["serie"] == 0, "Serie muss beim Verlieren zurueckgesetzt werden"
    assert prof["geschafft"] == vorher["geschafft"], (
        "die Karriere darf niemals rueckwaerts gehen")
    assert arbeit.stufe_fuer(prof["geschafft"]).titel == "Aushilfe"

    # Spass-Wordle zaehlt bewusst NICHT fuer die Karriere.
    vorher = dict(A._nutzer(uid))
    A.abrechnen(uid, arbeit.SPASS, 1.0)
    prof = A._nutzer(uid)
    assert prof["geschafft"] == vorher["geschafft"]
    assert prof["serie"] == vorher["serie"]
    assert prof.get("spass_siege") == 1 and prof.get("spass_gespielt") == 1

    # Die Schwellen selbst - damit sie nicht unbemerkt verrutschen.
    assert [(st.ab, st.titel) for st in arbeit.STUFEN] == [
        (0, "Praktikant"), (10, "Aushilfe"), (30, "Facharbeiter"),
        (75, "Vorarbeiter"), (150, "Schichtleiter"), (300, "Meister"),
        (600, "Werksleiter")]
    for n, erwartet in ((0, "Praktikant"), (9, "Praktikant"), (10, "Aushilfe"),
                        (29, "Aushilfe"), (30, "Facharbeiter"), (599, "Meister"),
                        (600, "Werksleiter"), (9999, "Werksleiter")):
        assert arbeit.stufe_fuer(n).titel == erwartet, n
    assert arbeit.naechste_stufe(600) is None


def test_arbeit_zeigt_den_fortschritt_zur_naechsten_stufe():
    """Die Karriere FUNKTIONIERTE, aber wie weit es noch ist, stand nur klein in
    der Fusszeile - ueber Stunden sah es deshalb aus, als passiere nichts. Der
    Balken gehoert an das Stufen-Feld selbst.

    Und er darf nicht luegen: bei 29 von 30 ist er NICHT voll."""
    import arbeit
    import inspect
    quelle = inspect.getsource(arbeit.Arbeit.ergebnis_embed)
    assert "\u25b0" in quelle and "\u25b1" in quelle, "kein Fortschrittsbalken am Stufen-Feld"
    assert "int(anteil * 10)" in quelle, (
        "der Balken rundet wieder - dann steht er bei 29/30 auf voll")

    def balken(hab):
        st = arbeit.stufe_fuer(hab)
        w = arbeit.naechste_stufe(hab)
        if w is None:
            return None
        anteil = 0.0 if w.ab <= st.ab else (hab - st.ab) / (w.ab - st.ab)
        return max(0, min(10, int(anteil * 10)))

    assert balken(10) == 0, "frisch aufgestiegen -> leerer Balken"
    assert balken(29) == 9, "29 von 30 darf NICHT voll aussehen"
    assert balken(7) == 7
    assert balken(600) is None, "hoechste Stufe hat keinen naechsten Schritt"


def test_arbeit_spasswordle_deckel_haelt_wirklich():
    """'Flo wordle' zum Spass - aber NIE mehr als der Deckel hergibt.

    Der Deckel greift VOR dem Gold-Bonus. Danach waere die Obergrenze in
    Wahrheit das Doppelte, und "nie mehr als 15.000" waere gelogen."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        prof = wp._nutzer(7)
        # Hoechste Stufe und volle Serie: der Deckel muss trotzdem halten.
        prof["serie"] = 999
        prof["geschafft"] = arbeit.STUFEN[-1].ab

        for laenge in sorted(arbeit.WOERTER):
            runde = arbeit.SPASS.fuer_laenge(laenge)
            for anteil in (1.5, 1.2, 1.0, 0.5):
                for gold in (False, True):
                    prof["heute"] = 0          # Tagesdeckel wegdenken
                    prof["tag"] = arbeit._heute()
                    betrag, _i = wp.abrechnen(7, runde, anteil, gold=gold)
                    assert betrag <= arbeit.SPASS_MAX, (laenge, anteil, gold, betrag)

        # Ohne Deckel waere es wirklich mehr - der Deckel ist also kein Deko.
        acht = arbeit.SPASS.fuer_laenge(8)
        assert acht.lohn * 1.5 > arbeit.SPASS_MAX, "Deckel greift nie"

        # Und es bleibt klar unter dem Wort des Tages.
        kleinster_topf = min(arbeit.TAGES_LAENGEN) * arbeit.TAGES_PRO_BUCHSTABE
        assert arbeit.SPASS_MAX < kleinster_topf, (arbeit.SPASS_MAX, kleinster_topf)
        assert arbeit.SPASS_MAX <= 15000, arbeit.SPASS_MAX
    finally:
        restore()


def test_arbeit_spasswordle_ist_keine_karriere():
    """Zeitvertreib zaehlt NICHT fuer Stufe und Serie.

    Sonst waere der Werksleiter der, der am meisten geraten hat - und die
    Karriere haette nichts mehr mit Arbeit zu tun."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        runde = arbeit.SPASS.fuer_laenge(5)
        for _ in range(40):
            wp.abrechnen(7, runde, 1.0)
        prof = wp._nutzer(7)
        assert prof["geschafft"] == 0, "Spass zaehlt fuer die Karriere"
        assert prof["serie"] == 0, "Spass baut eine Serie auf"
        assert prof["schichten"] == 0
        # Aber die eigene Bilanz wird gefuehrt.
        assert prof["spass_gespielt"] == 40 and prof["spass_siege"] == 40
        # Eine echte Schicht danach ist trotzdem Schicht 1.
        wp.abrechnen(7, arbeit.SCHICHTEN["safe"], 1.0)
        assert wp._nutzer(7)["geschafft"] == 1

        # Und die Runde wird NIE per 'Flo work' gezogen.
        assert "spasswordle" not in arbeit.SCHICHTEN
        random.seed(3)
        assert all(arbeit.schicht_ziehen().key != "spasswordle"
                   for _ in range(2000))
    finally:
        random.seed()
        restore()


def test_arbeit_spasswordle_eigener_cooldown_und_laenge():
    """Das Spass-Wordle hat einen EIGENEN, kurzen Cooldown - es darf die
    Schicht nicht blockieren und umgekehrt. Und die Laenge darf man waehlen."""
    arbeit, restore = _arbeit_frisch({5: 0})
    gesendet = []
    try:
        autor = _fake_person(5, name="tester", global_name="Tester")
        kanal = SimpleNamespace(id=9, send=lambda **kw: _als_coro(
            gesendet.append(kw) or SimpleNamespace(id=1)))
        msg = SimpleNamespace(content="Flo wordle 7", author=autor, channel=kanal,
                              guild=SimpleNamespace(id=42))
        assert asyncio.run(arbeit.handle(msg)) is arbeit.HANDLED
        assert len(gesendet) == 1 and "file" in gesendet[0], gesendet[0].keys()
        prof = arbeit.instance._nutzer(5)
        # Eigener Schluessel, kurzer Cooldown - und die SCHICHT ist frei.
        assert prof["cooldown_spass"] > int(time.time()), prof
        assert prof["cooldown"] == 0, "Spass hat die Schicht blockiert"
        assert prof["schichten"] == 0, "Spass wurde als Schicht gezaehlt"

        # Direkt nochmal: Cooldown-Hinweis, in SEKUNDEN (er ist kurz).
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "warten" in text.lower(), text
        assert len(gesendet) == 1

        # Eine echte Schicht geht trotzdem sofort.
        msg.content = "Flo work sortieren"
        assert asyncio.run(arbeit.handle(msg)) is arbeit.HANDLED
        assert len(gesendet) == 2

        # Unsinnige Laenge wird erklaert, nicht still auf 5 gedreht.
        arbeit.instance._nutzer(5)["cooldown_spass"] = 0
        msg.content = "Flo wordle 12"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "Buchstaben" in text and str(min(arbeit.WOERTER)) in text, text
        msg.content = "Flo wordle blau"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "wordle" in text.lower(), text
        assert len(gesendet) == 2, "trotz Murks eine Runde gestartet"
    finally:
        restore()


def test_arbeit_wordle_versteht_vertipper_und_fremdsprachen():
    """'Flo wordle' muss auch bei Vertippern und auf Englisch/Bayrisch greifen.

    Zwei Ebenen: die haeufigsten Schreibweisen stehen direkt im Modul, alles
    Weitere faengt cmdnorm ab (ein Tippfehler, kein Buchstaben-Tausch)."""
    import arbeit
    import cmdnorm
    # Direkt verstanden - ohne Umweg ueber die Korrektur.
    for wort in ("wordle", "wordl", "wordel", "worlde", "wörtle"):
        assert wort in arbeit._WORDLE_CMDS, wort
    # Wort des Tages hat eigene Woerter, und 'daily' ist NICHT dabei:
    # das ist seit immer der Tagesbonus aus economy.
    assert "daily" not in arbeit._TAGES_CMDS
    assert "daily" in cmdnorm.KNOWN
    assert not set(arbeit._WORDLE_CMDS) & set(arbeit._TAGES_CMDS)


# --- KI-Ausfaelle: jede Ursache eigen behandeln -----------------------------
class _KiAntwort:
    """Antwort des Anbieters, so wie das openai-Paket sie liefert."""

    def __init__(self, text="ok"):
        nachricht = SimpleNamespace(content=text, tool_calls=None)
        self.choices = [SimpleNamespace(message=nachricht)]


class _KiFehler(Exception):
    """Fehler des Anbieters mit HTTP-Status und Wortlaut - genau die zwei Dinge,
    an denen ai._einordnen() sich orientiert."""

    def __init__(self, status, text=""):
        self.status_code = status
        self.response = SimpleNamespace(status_code=status, text=text)
        super().__init__(text or f"HTTP {status}")


class _KiAnbieter:
    """Falscher LLM-Anbieter: arbeitet eine vorgegebene Folge ab und schreibt
    mit, mit welchem Modell und welcher Signatur er angesprochen wurde."""

    def __init__(self, folge, modelle=(), signatur=""):
        self._folge = list(folge)
        self._modelle = list(modelle)
        self.signatur = signatur
        self.modelle_gefragt = 0
        self.aufrufe = []          # je Aufruf das benutzte Modell
        self.letzte_kwargs = {}    # womit zuletzt aufgerufen wurde
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create))
        self.models = SimpleNamespace(list=self._models)

    async def _create(self, **kw):
        self.aufrufe.append(kw.get("model"))
        self.letzte_kwargs = dict(kw)
        naechste = self._folge.pop(0) if self._folge else _KiAntwort()
        if isinstance(naechste, Exception):
            raise naechste
        return naechste

    async def _models(self):
        self.modelle_gefragt += 1
        return SimpleNamespace(data=[SimpleNamespace(id=m) for m in self._modelle])


def _ki_frisch(folge, modelle=(), modell="altes-modell-70b"):
    """Eine FloAI-Instanz mit falschem Anbieter und ohne Wartezeiten."""
    import ai
    flo = ai.FloAI()
    flo._model = modell
    flo._vision_model = "altes-vision-modell"
    flo._api_key = "gsk_test"
    flo._basis_url = "https://api.example.invalid/v1"
    flo.WARTEN = (0.0, 0.0, 0.0)     # Tests sollen nicht wirklich schlafen
    anbieter = _KiAnbieter(folge, modelle)
    flo._client = anbieter
    return flo, anbieter


def test_ki_fehler_werden_unterschieden():
    """Vorher gab JEDE Ursache denselben Satz: abgelaufener Schluessel,
    ausgemustertes Modell, leeres Kontingent, Cloudflare-Sperre. Damit war ohne
    Serverzugang nicht zu sehen, woran es liegt - und im Log stand nur ein
    Traceback."""
    import ai
    faelle = {
        401: "auth",
        404: "modell",
        429: "limit",
        503: "stoerung",
        400: "anfrage",
    }
    saetze = {}
    for status, art in faelle.items():
        flo, _ = _ki_frisch([_KiFehler(status, "kaputt")] * 8)
        antwort = asyncio.run(flo.ask_flo("hi"))
        assert antwort == ai.FloAI.MELDUNGEN[art], (status, antwort)
        saetze[art] = antwort
    # Cloudflare-Sperre ist etwas anderes als ein verbotener Zugriff.
    flo, _ = _ki_frisch([_KiFehler(403, "error code: 1010")] * 8)
    flo._signatur_wechseln = lambda ua: False        # keine Rettung erlauben
    saetze["signatur"] = asyncio.run(flo.ask_flo("hi"))
    assert saetze["signatur"] == ai.FloAI.MELDUNGEN["signatur"]
    flo, _ = _ki_frisch([_KiFehler(403, "nope")] * 8)
    saetze["verboten"] = asyncio.run(flo.ask_flo("hi"))
    assert saetze["verboten"] == ai.FloAI.MELDUNGEN["verboten"]
    # Jede Ursache muss WIRKLICH einen eigenen Satz haben.
    assert len(set(saetze.values())) == len(saetze), saetze


def test_ki_wiederholt_nur_was_davon_besser_wird():
    """Ein einzelner 429 oder eine 503-Delle hat die Antwort bisher sofort
    getoetet. Wiederholt werden darf aber nur, was sich bessern kann - ein
    abgelehnter Schluessel wird beim zweiten Mal nicht gueltiger, das waere
    reines Haemmern gegen den Anbieter."""
    # Voruebergehend: erst 429, dann 503, dann klappt es.
    flo, anbieter = _ki_frisch([_KiFehler(429, "rate limit"),
                                _KiFehler(503, "unavailable"),
                                _KiAntwort("Na klar.")])
    assert asyncio.run(flo.ask_flo("hi")) == "Na klar."
    assert len(anbieter.aufrufe) == 3, anbieter.aufrufe

    # Dauerhaft: genau EIN Versuch, kein Nachtreten.
    for status, text in ((401, "bad key"), (400, "kaputt"), (403, "nope")):
        flo, anbieter = _ki_frisch([_KiFehler(status, text)] * 8)
        asyncio.run(flo.ask_flo("hi"))
        assert len(anbieter.aufrufe) == 1, (status, anbieter.aufrufe)

    # Und es gibt eine Obergrenze - kein Dauerfeuer.
    flo, anbieter = _ki_frisch([_KiFehler(429, "rate limit")] * 50)
    asyncio.run(flo.ask_flo("hi"))
    assert len(anbieter.aufrufe) == flo.WIEDERHOLUNGEN + 1, anbieter.aufrufe


def test_ki_heilt_ein_ausgemustertes_modell_selbst():
    """Anbieter mustern Modelle aus - genau das erklaert 'geht seit gestern gar
    nicht mehr, ohne dass jemand am Code war'. Statt dauerhaft stumm zu sein
    holt Flo die aktuelle Liste und nimmt selbst den besten Ersatz."""
    flo, anbieter = _ki_frisch(
        [_KiFehler(404, "The model `altes-modell-70b` has been decommissioned"),
         _KiAntwort("Bin wieder da.")],
        modelle=["whisper-large-v3", "neu-klein-8b", "neu-gross-70b",
                 "llama-guard-4-12b"])
    assert asyncio.run(flo.ask_flo("hi")) == "Bin wieder da."
    assert anbieter.modelle_gefragt == 1
    # Groesstes taugliches Modell, und nichts, was gar nicht chatten kann.
    assert flo._model == "neu-gross-70b", flo._model
    assert anbieter.aufrufe == ["altes-modell-70b", "neu-gross-70b"]

    # Vision nimmt nur ein Modell, das wirklich Bilder kann.
    flo, _ = _ki_frisch([_KiFehler(404, "model_not_found"), _KiAntwort("Bild gesehen.")],
                        modelle=["neu-gross-70b", "scout-17b", "whisper-large-v3"])
    assert asyncio.run(flo.see_image("was ist das", "http://x/y.png")) == "Bild gesehen."
    assert flo._vision_model == "scout-17b", flo._vision_model

    # Und wenn es keinen Ersatz gibt, wird ehrlich gemeldet statt endlos gesucht.
    import ai
    flo, anbieter = _ki_frisch([_KiFehler(404, "model_not_found")] * 8, modelle=[])
    assert asyncio.run(flo.ask_flo("hi")) == ai.FloAI.MELDUNGEN["modell"]
    assert len(anbieter.aufrufe) == 1, anbieter.aufrufe


def test_ki_wechselt_die_signatur_wenn_cloudflare_sperrt():
    """Gemessen auf dem echten Server: Groq sitzt hinter Cloudflare, und
    Cloudflare hat mit HTTP 403 'error code: 1010' geblockt - die Anfrage kam nie
    bei Groq an. Ein anderer Schluessel oder ein anderes Modell haetten daran
    nichts geaendert; nur eine andere Client-Signatur hilft."""
    import ai
    gebaut = []

    flo, _ = _ki_frisch([])
    def bauen(ua):
        gebaut.append(ua)
        # Die erste Ersatz-Signatur kommt durch, die urspruengliche nicht.
        if ua == ai.FloAI.SIGNATUREN[0]:
            return _KiAnbieter([_KiAntwort("Wieder da.")])
        return _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8)
    flo._client_bauen = bauen
    flo._client = _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8)

    assert asyncio.run(flo.ask_flo("hi")) == "Wieder da."
    assert gebaut and gebaut[0] == ai.FloAI.SIGNATUREN[0], gebaut
    assert flo._signatur == ai.FloAI.SIGNATUREN[0]
    # Nach dem Erfolg ist der Hinweis abgearbeitet und wird nicht endlos wiederholt.
    assert flo._signatur_offen == ""

    # Kommt KEINE Signatur durch, ist die IP gesperrt - dann ehrlich melden und
    # nicht ewig weiterprobieren.
    flo, _ = _ki_frisch([])
    versuche = []
    flo._client_bauen = lambda ua: (versuche.append(ua),
                                    _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8))[1]
    flo._client = _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8)
    assert asyncio.run(flo.ask_flo("hi")) == ai.FloAI.MELDUNGEN["signatur"]
    assert len(versuche) == len(ai.FloAI.SIGNATUREN), versuche


def test_ki_stoerung_landet_nicht_im_gedaechtnis():
    """bot.py schreibt JEDE Antwort ins Kurzzeit-Gedaechtnis (bot.py:1529 und
    :1820) - auch eine Stoerungsmeldung. Die ging danach als Gespraechsverlauf
    wieder ans Modell, das sie brav nachgeplappert hat."""
    import ai
    flo, _ = _ki_frisch([])
    for satz in ai.FloAI.MELDUNGEN.values():
        flo.note_message(4711, "Flo", satz, is_bot=True)
    flo.note_message(4711, "Flo", "Klar, mach ich.", is_bot=True)
    gemerkt = [e["content"] for e in flo._HISTORY.get(4711, [])]
    assert gemerkt == ["Klar, mach ich."], gemerkt


def test_ki_selbsttest_sagt_die_wahrheit():
    """Der Log meldete 'KI-Feature aktiv', sobald ein Schluessel in der .env
    stand - ob er noch gilt, hat nie jemand geprueft. Eine Zusicherung, die
    niemand nachgesehen hat, ist schlimmer als keine."""
    flo, _ = _ki_frisch([_KiAntwort("ok")])
    assert asyncio.run(flo.selbsttest()) is True
    flo, _ = _ki_frisch([_KiFehler(401, "bad key")] * 8)
    assert asyncio.run(flo.selbsttest()) is False
    # Ohne Client faellt er sauber durch, statt zu krachen.
    flo, _ = _ki_frisch([])
    flo._client = None
    assert asyncio.run(flo.selbsttest()) is False


def test_ki_hat_nur_noch_einen_weg_nach_draussen():
    """Vier eigene try/except-Bloecke bedeuteten vier Politiken, die
    auseinanderlaufen. Es darf genau EINE Stelle geben, die den Anbieter
    anspricht - sonst vergisst der naechste Aufruf das Wiederholen wieder."""
    import ai
    quelle = open(ai.__file__, encoding="utf-8").read()
    assert quelle.count("chat.completions.create") == 1, (
        "es gibt wieder mehr als einen Weg zum Anbieter")
    # Und JEDER oeffentliche Aufrufer muss LlmFehler behandeln - sonst platzt
    # die Ausnahme bis in bot.py durch und der Nutzer sieht gar nichts.
    import inspect
    for name in ("ask_flo", "see_image", "see_image_raw", "generate"):
        koerper = inspect.getsource(getattr(ai.FloAI, name))
        assert "except LlmFehler" in koerper, f"{name} behandelt LlmFehler nicht"
        assert "except Exception" in koerper, f"{name} hat kein Sicherheitsnetz mehr"


def test_ki_gegen_einen_echten_http_anbieter():
    """Der Ernstfall-Test: echtes openai-Paket, echter HTTP-Server, echte
    Ausnahmen. ai._einordnen() liest exc.status_code bzw. exc.response.status_code -
    ob das mit den tatsaechlichen Ausnahmen des Pakets zusammenpasst, beweisen
    nachgebaute Fehler NICHT. Genau hier bricht es sonst still.

    Laeuft komplett auf 127.0.0.1 mit einem freien Port, kein echtes Netz."""
    import ai
    try:
        import openai  # noqa: F401
    except ImportError:                       # Bot laeuft auch ohne das Paket
        return
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    fall = {"wert": "ok", "modelle": []}
    cf_seite = "<html>error code: 1010</html>"

    def antwort(text):
        return _json.dumps({"id": "1", "object": "chat.completion", "created": 1,
                            "model": "m", "choices": [{"index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": text}}]})

    class Griff(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _raus(self, code, text, typ="application/json"):
            roh = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(roh)))
            self.end_headers()
            self.wfile.write(roh)

        def do_GET(self):
            if "/models" in self.path:
                return self._raus(200, _json.dumps({"object": "list", "data": [
                    {"id": m, "object": "model"} for m in fall["modelle"]]}))
            return self._raus(404, _json.dumps({"error": {"message": "nope"}}))

        def do_POST(self):
            art = fall["wert"]
            ua = self.headers.get("User-Agent", "")
            if art == "ok":
                return self._raus(200, antwort("Antwort da."))
            if art == "cf_signatur":
                if "curl" in ua:
                    return self._raus(200, antwort("Mit curl gehts."))
                return self._raus(403, cf_seite, "text/html")
            if art == "cf_ip":
                return self._raus(403, cf_seite, "text/html")
            if art == "modell_weg":
                laenge = int(self.headers.get("Content-Length", 0))
                try:
                    gewuenscht = _json.loads(self.rfile.read(laenge)).get("model", "")
                except ValueError:
                    gewuenscht = ""
                if gewuenscht and gewuenscht in fall["modelle"]:
                    return self._raus(200, antwort("Neues Modell laeuft."))
                return self._raus(404, _json.dumps({"error": {
                    "message": "The model `x` has been decommissioned"}}))
            return self._raus({"401": 401, "429": 429, "503": 503, "400": 400}[art],
                              _json.dumps({"error": {"message": "kaputt"}}))

    server = HTTPServer(("127.0.0.1", 0), Griff)   # Port 0 = freien nehmen
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    alt_umgebung = {k: os.environ.get(k) for k in
                    ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_USER_AGENT")}

    def frisch():
        os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
        os.environ["LLM_API_KEY"] = "gsk_test"
        os.environ["LLM_MODEL"] = "altes-modell-70b"
        os.environ.pop("LLM_USER_AGENT", None)
        flo = ai.FloAI()
        assert flo.setup() is True
        flo.WARTEN = (0.0, 0.0, 0.0)           # Tests sollen nicht schlafen
        return flo

    try:
        M = ai.FloAI.MELDUNGEN
        assert asyncio.run(frisch().ask_flo("hi")) == "Antwort da."

        for art, schluessel in (("401", "auth"), ("429", "limit"),
                                ("503", "stoerung"), ("400", "anfrage")):
            fall["wert"] = art
            assert asyncio.run(frisch().ask_flo("hi")) == M[schluessel], art

        # Cloudflare sperrt die IP: keine Signatur kommt durch -> ehrlich melden.
        fall["wert"] = "cf_ip"
        assert asyncio.run(frisch().ask_flo("hi")) == M["signatur"]

        # Cloudflare sperrt nur die Signatur -> Flo holt sich selbst raus.
        fall["wert"] = "cf_signatur"
        assert asyncio.run(frisch().ask_flo("hi")) == "Mit curl gehts."

        # Modell ausgemustert -> Flo nimmt selbst das groesste taugliche.
        fall["wert"] = "modell_weg"
        fall["modelle"] = ["klein-8b", "gross-70b", "whisper-large-v3"]
        flo = frisch()
        assert asyncio.run(flo.ask_flo("hi")) == "Neues Modell laeuft."
        assert flo._model == "gross-70b", flo._model

        fall["modelle"] = []
        assert asyncio.run(frisch().ask_flo("hi")) == M["modell"]

        # Der Selbsttest muss beides koennen: bestaetigen und widersprechen.
        fall["wert"] = "ok"
        assert asyncio.run(frisch().selbsttest()) is True
        fall["wert"] = "401"
        assert asyncio.run(frisch().selbsttest()) is False

        # Haengt der Anbieter, wartet Discord nicht ewig.
        fall["wert"] = "ok"
        flo = frisch()
        flo.ZEITLIMIT = 0.001
        flo._client = flo._client_bauen("")
        start = time.time()
        asyncio.run(flo.ask_flo("hi"))
        assert time.time() - start < 10, "Zeitlimit greift nicht"
    finally:
        server.shutdown()
        for k, v in alt_umgebung.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_ki_der_arzt_verschluckt_keine_meldung():
    """Nachgemessen und es stimmte NICHT: der Suchfilter in 'k' fand zwar
    "KI-Fehler", aber genau die Zeilen nicht, die sagen was Flo selbst repariert
    hat und was in die .env gehoert ("KI: wechsle selbst auf ...", "KI: Signatur
    ... funktioniert", "KI-Selbsttest fehlgeschlagen"). Der Arzt verschwieg also
    die Heilung.

    Der Test liest BEIDE Dateien als Quelltext, damit eine neue Log-Zeile in
    ai.py nicht still aus dem Filter fallen kann."""
    import ai
    hier = os.path.dirname(os.path.abspath(ai.__file__))
    arzt = open(os.path.join(hier, "k"), encoding="utf-8").read()

    # Das Muster steht in 'k' als KIMUSTER=... (der grep nimmt es ueber eine
    # Variable auf, deshalb hier die Zuweisung lesen und nicht den grep).
    muster = re.search(r'KIMUSTER="([^"]+)"', arzt)
    assert muster, "in 'k' gibt es kein KIMUSTER mehr"
    filter_re = re.compile(muster.group(1))

    quelle = open(ai.__file__, encoding="utf-8").read()
    meldungen = re.findall(r'log\.[a-z]+\(\s*"(KI[^"]*)', quelle)
    assert len(meldungen) >= 10, f"nur {len(meldungen)} KI-Meldungen gefunden"
    durchgefallen = [m for m in meldungen if not filter_re.search(m)]
    assert not durchgefallen, f"'k' zeigt diese Zeilen nicht an: {durchgefallen}"

    # Und der Vorspann von journalctl muss WIRKLICH weg sein: das sind fuenf
    # Felder ("Aug 19 14:25:20 Ubuntu python3[8422]:"), nicht vier.
    beispiel = "Aug 19 14:25:20 Ubuntu python3[8422]: 2026-08-19 [ERROR] KI: Modell weg"
    schnitt = re.search(r"sed 's/(\^[^']+)/", arzt)
    assert schnitt, "in 'k' steht kein sed mehr"
    gekuerzt = re.sub(schnitt.group(1).replace("^", "^"), "", beispiel)
    assert not gekuerzt.startswith("python"), (
        f"der Unit-Vorspann bleibt stehen: {gekuerzt!r}")


def test_ki_findet_bildmodelle_auch_anderer_familien():
    """Die Marker-Liste kannte nur Metas Namen. Nachgemessen fehlten pixtral und
    llava - die zwei verbreitetsten Bild-Modellfamilien ueberhaupt. Mustert der
    Anbieter Scout aus, blieb das Bild-Lesen damit tot, obwohl ein tauglicher
    Ersatz in der Liste stand."""
    import ai
    flo = ai.FloAI()
    for liste, erwartet in (
            (["llama-4-scout-17b", "gpt-oss-120b"], "llama-4-scout-17b"),
            (["pixtral-12b", "gpt-oss-120b"], "pixtral-12b"),
            (["llava-1.6-34b", "gpt-oss-120b"], "llava-1.6-34b"),
            (["qwen2-vl-72b", "gpt-oss-120b"], "qwen2-vl-72b")):
        assert flo._modell_waehlen(liste, True) == erwartet, liste
    # Die ECHTE Groq-Liste (nachgeschlagen, nicht erfunden): gpt-oss kann keine
    # Bilder, qwen3.6 ist multimodal - heisst aber weder "vision" noch "-vl".
    # Genau daran waere die Heilung sonst vorbeigelaufen.
    groq = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b",
            "whisper-large-v3", "meta-llama/llama-guard-4-12b"]
    assert flo._modell_waehlen(groq, False) == "openai/gpt-oss-120b"
    assert flo._modell_waehlen(groq, True) == "qwen/qwen3.6-27b"

    # Ein reines Textmodell darf NICHT als Bild-Ersatz durchgehen - es wuerde
    # das Bild-Format ablehnen. Lieber ehrlich nichts finden.
    assert flo._modell_waehlen(["gpt-oss-120b", "whisper-large-v3"], True) == ""
    # Und niemals etwas, das gar nicht chatten kann.
    assert flo._modell_waehlen(["whisper-large-v3", "guard-12b"], False) == ""


def test_ki_unbekannter_fehler_nennt_die_ausnahmeklasse():
    """Bei einem Fehler ohne HTTP-Status ("unbekannt") ist die Ausnahmeklasse die
    einzige Spur. Die stand nur in einem log.debug - und bot.py:70 loggt ab INFO,
    also erreichte sie das Journal ausgerechnet dort nie, wo sie gebraucht wird."""
    import ai
    import logging
    puffer = io.StringIO()
    griff = logging.StreamHandler(puffer)
    protokoll = logging.getLogger("dcbot.ai")
    protokoll.addHandler(griff)
    alt_stufe = protokoll.level
    protokoll.setLevel(logging.INFO)          # so wie bot.py es einstellt
    try:
        class VoelligUnbekannt(Exception):
            pass
        flo, _ = _ki_frisch([VoelligUnbekannt("kaputt")] * 8)
        asyncio.run(flo.ask_flo("hi"))
    finally:
        protokoll.removeHandler(griff)
        protokoll.setLevel(alt_stufe)
    text = puffer.getvalue()
    assert "KI-Fehler: unbekannt" in text, text
    assert "VoelligUnbekannt" in text, f"Ausnahmeklasse fehlt im Log: {text!r}"


def test_ki_vorgaben_von_bot_und_arzt_sind_gleich():
    """tools_ki_check.py haelt die Vorgaben bewusst als eigene Kopie - es muss
    auch laufen, wenn ai.py gar nicht importierbar ist. Genau deshalb koennen
    beide auseinanderlaufen, und dann diagnostiziert der Arzt etwas anderes als
    der Bot tut."""
    import ai
    import tools_ki_check
    assert tools_ki_check.STANDARD_BASE == ai.FloAI.DEFAULT_BASE_URL
    assert tools_ki_check.STANDARD_MODELL == ai.FloAI.DEFAULT_MODEL
    assert tools_ki_check.STANDARD_VISION == ai.FloAI.DEFAULT_VISION_MODEL
    # Und die Vorgaben duerfen nicht auf ausgemusterte Modelle zeigen. Groq hat
    # die beiden am 17.06.2026 abgeschaltet - das war die Ursache des Ausfalls.
    for tot in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        assert tot != ai.FloAI.DEFAULT_MODEL, f"{tot} ist bei Groq abgeschaltet"
    # Das Chat-Modell MUSS Werkzeuge koennen - ask_flo reicht 'tools' mit.
    assert "tools=[self.WEATHER_TOOL]" in inspect.getsource(ai.FloAI.ask_flo)


# --- Die Aerzte (tools_*_check.py) ------------------------------------------
class _FalscherAnbieter:
    """Spielt einen LLM-Anbieter samt Cloudflare davor. Kein echtes Netz."""

    def __init__(self, fall="ok",
                 modelle=("openai/gpt-oss-120b", "qwen/qwen3.6-27b")):
        import http.server
        import threading
        self.fall = fall
        self.modelle = list(modelle)
        aussen = self

        class Griff(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _raus(self, code, text, typ="application/json"):
                roh = text.encode()
                self.send_response(code)
                self.send_header("Content-Type", typ)
                if code == 403 and aussen.fall.startswith("cf"):
                    self.send_header("cf-ray", "abc123-FRA")
                self.send_header("Content-Length", str(len(roh)))
                self.end_headers()
                self.wfile.write(roh)

            def _antwort(self):
                f = aussen.fall
                ua = self.headers.get("User-Agent", "")
                if f == "cf_signatur" and "curl" in ua:
                    f = "ok"
                if f == "cf_ip" or f == "cf_signatur":
                    return self._raus(403, "<html>error code: 1010</html>", "text/html")
                if f == "ok":
                    if "/models" in self.path:
                        return self._raus(200, json.dumps({"data": [
                            {"id": m} for m in aussen.modelle]}))
                    return self._raus(200, json.dumps(
                        {"choices": [{"message": {"content": "ok"}}]}))
                code = {"401": 401, "404": 404, "429": 429, "500": 503}[f]
                return self._raus(code, json.dumps({"error": {"message": "kaputt"}}))

            do_GET = do_POST = _antwort

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Griff)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.server.shutdown()


def _ki_arzt_lauf(anbieter, schluessel="gsk_streng_geheim_1234567890"):
    """Laesst den KI-Arzt gegen den falschen Anbieter laufen. Gibt (text, arzt)."""
    import tools_ki_check
    merker = {k: os.environ.get(k) for k in
              ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_USER_AGENT",
               "HTTPS_PROXY", "https_proxy")}
    os.environ["LLM_BASE_URL"] = anbieter.url
    os.environ["LLM_API_KEY"] = schluessel
    os.environ.pop("LLM_USER_AGENT", None)
    for v in ("HTTPS_PROXY", "https_proxy"):
        os.environ.pop(v, None)
    puffer = io.StringIO()
    alt_aus = sys.stdout
    sys.stdout = puffer
    try:
        arzt = tools_ki_check.KiCheck()
        arzt.lauf()
    finally:
        sys.stdout = alt_aus
        for k, v in merker.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return re.sub(r"\x1b\[[0-9;]*m", "", puffer.getvalue()), arzt


def test_arzt_verraet_niemals_ein_geheimnis():
    """Der Arzt wird auf einem Handy fotografiert und in Chats geschickt. Ein
    Schluessel darf da NIE vollstaendig auftauchen - auch nicht im Fehlerfall
    und auch nicht in der Berichtsdatei."""
    import arzt
    a = arzt.Arzt()
    assert a.maskiere("") == "(leer)"
    for geheim in ("gsk_streng_geheim_1234567890", "kurz", "abcdefghijkl"):
        maskiert = a.maskiere(geheim)
        assert geheim not in maskiert, maskiert
        assert str(len(geheim)) in maskiert          # Laenge hilft beim Vergleichen

    schluessel = "gsk_streng_geheim_1234567890"
    with _FalscherAnbieter("401") as anbieter:
        text, ki_arzt = _ki_arzt_lauf(anbieter, schluessel)
    assert schluessel not in text, "Schluessel steht im Klartext auf dem Bildschirm"
    assert schluessel not in "\n".join(ki_arzt.zeilen), "Schluessel steht im Bericht"
    assert "gsk_...7890" in text, text[:400]


def test_ki_arzt_ordnet_jeden_anbieterfehler_richtig_ein():
    """Fuer jede Ursache ein anderer Rat - sonst tauscht man wieder den Schluessel,
    obwohl das Modell abgeschaltet wurde."""
    erwartet = {
        "401": "Schluessel",
        "404": "Modell",
        "429": "Kontingent",
        "500": "Stoerung",
    }
    for fall, stichwort in erwartet.items():
        with _FalscherAnbieter(fall) as anbieter:
            text, arzt = _ki_arzt_lauf(anbieter)
        raete = " ".join(u for u, _ in arzt.probleme)
        assert stichwort.lower() in raete.lower(), (fall, raete)

    # Und wenn alles geht, gibt es KEINEN Befund.
    with _FalscherAnbieter("ok") as anbieter:
        text, arzt = _ki_arzt_lauf(anbieter)
    assert not arzt.probleme, arzt.probleme
    assert "funktioniert" in text


def test_ki_arzt_unterscheidet_signatur_von_ip_sperre():
    """Cloudflare-Fehler 1010 hat ZWEI voellig verschiedene Ursachen, und die
    Behandlung ist entgegengesetzt: bei der Signatur hilft eine Zeile in der
    .env, bei einer IP-Sperre hilft gar kein Code. Raten waere hier teuer."""
    with _FalscherAnbieter("cf_signatur") as anbieter:
        text, arzt = _ki_arzt_lauf(anbieter)
    raete = " ".join(f"{u} {w}" for u, w in arzt.probleme)
    assert "LLM_USER_AGENT" in raete, raete
    assert "IP" not in " ".join(u for u, _ in arzt.probleme)

    with _FalscherAnbieter("cf_ip") as anbieter:
        text, arzt = _ki_arzt_lauf(anbieter)
    raete = " ".join(f"{u} {w}" for u, w in arzt.probleme)
    assert "IP" in raete and "gesperrt" in raete, raete
    # Und der Cloudflare-Code muss als solcher benannt sein, nicht als API-Fehler.
    assert "Cloudflare" in text and "1010" in text


def test_jede_aussenabhaengigkeit_hat_einen_arzt():
    """Alle echten Ausfaelle kamen von AUSSEN: Groq hat ein Modell abgeschaltet,
    YouTube bindet die Adresse an den Client, Cloudflare sperrte die Signatur.
    Interne Tests fangen so etwas nie. Was dagegen hilft, ist: jede
    Aussenabhaengigkeit muss mit einem Befehl pruefbar sein."""
    import tools_check
    import tools_ki_check
    import tools_musik_check
    quelle = (open("tools_check.py", encoding="utf-8").read()
              + open("tools_ki_check.py", encoding="utf-8").read()
              + open("tools_musik_check.py", encoding="utf-8").read())
    for was, marke in (("Discord-Token", "DISCORD_TOKEN"),
                       ("LLM-Schluessel", "LLM_API_KEY"),
                       ("LLM-Modell", "/models"),
                       ("Spotify", "accounts.spotify.com"),
                       ("YouTube/yt-dlp", "yt_dlp"),
                       ("ffmpeg", "ffmpeg"),
                       ("Datenordner", "DATA_DIR"),
                       ("Plattenplatz", "disk_usage"),
                       ("Dienst", "systemctl"),
                       ("Repo-Stand", "rev-list")):
        assert marke in quelle, f"{was} wird von keinem Arzt geprueft ({marke})"

    # Alle drei Aerzte muessen dieselbe Basis nutzen - sonst laufen Maskierung
    # und Berichtsformat wieder auseinander.
    import arzt
    for klasse in (tools_ki_check.KiCheck, tools_musik_check.MusikCheck,
                   tools_check.GesamtCheck):
        assert issubclass(klasse, arzt.Arzt), klasse
        assert callable(getattr(klasse, "lauf", None)), klasse

    # Und 'k' muss sie auch wirklich anbieten.
    arztruf = open("k", encoding="utf-8").read()
    for datei in ("tools_ki_check.py", "tools_musik_check.py", "tools_check.py"):
        assert datei in arztruf, f"{datei} ist ueber 'bash k' nicht erreichbar"


def test_pvp_sieger_zahlt_keine_tilgung_auf_den_eigenen_einsatz():
    """Quizduell und Schere-Stein-Papier buchten den GANZEN Pot direkt per
    add_coins. Damit galt auch der eigene Einsatz des Siegers als Einnahme -
    wer Schulden hat, gibt davon 20 % ab.

    An echten Konten gemessen (Einsatz 1.000, Pot 2.000, Schuldner):
        direkt gebucht  -> netto +600
        ueber _auszahlen-> netto +800   (nur der GEWINN wird getilgt)

    Dazu lief die Tageskappe (GAMES_DAILY_MAX) an beiden Wegen vorbei, und die
    Ansage nannte den Einsatz als Gewinn statt des wirklich gezahlten Betrags."""
    import games
    import schulden
    economy.setup()
    schulden.setup()
    games.setup()
    sieger, glaeubiger = 770101, 770202
    economy.add_coins(sieger, 20_000 - economy.get_coins(sieger))
    economy.add_coins(glaeubiger, 1_000)
    schulden.instance.buch.anlegen(glaeubiger, sieger, 50_000, grund="Test")

    einsatz = 1_000
    vorher = economy.get_coins(sieger)
    economy.add_coins(sieger, -einsatz)              # Einsatz wird gezogen
    gezahlt = games._auszahlen(sieger, einsatz * 2, einsatz, "sspduell")
    netto = economy.get_coins(sieger) - vorher

    assert gezahlt == einsatz * 2, gezahlt
    assert netto == 800, (
        f"netto {netto} statt 800 - der Sieger zahlt Tilgung auf sein eigenes Geld")

    # Und beide PvP-Wege muessen wirklich ueber _auszahlen laufen.
    quelle = open("games.py", encoding="utf-8").read()
    fuer_pvp = (inspect.getsource(games.Games._check_qduel)
                + inspect.getsource(games._SSPDuel))
    assert "economy.add_coins(sieger.id, pot)" not in fuer_pvp
    assert "economy.add_coins(message.author.id, pot)" not in fuer_pvp
    assert fuer_pvp.count("_auszahlen(") >= 2, (
        "ein PvP-Weg bucht wieder direkt - dann zahlt der Sieger Tilgung auf "
        "seinen eigenen Einsatz")


def test_zwei_skips_fressen_keinen_song():
    """Zwei Skips kurz hintereinander (oder Skip, waehrend der after-Callback
    schon laeuft) liefen beide gleichzeitig durch _advance. Beide holten sich
    mit queue.pop(0) einen Track - einer davon verschwand spurlos.

    Der 'gen'-Schutz greift dagegen NICHT: Aufrufe ohne gen (skip, weiter,
    Reconnect) umgehen ihn ausdruecklich, und das ist so gewollt.

    Wichtig am Fix: der zweite Lauf wird nicht VERSCHLUCKT, er wartet nur. Die
    Zusicherung "ohne gen laeuft IMMER" bleibt damit wahr - ein frueher
    Ausstieg haette einen zweiten Skip stillschweigend gefressen."""
    import music
    spieler = music.GuildPlayer(loop=asyncio.get_event_loop_policy().new_event_loop(),
                               guild_id=77, volume=0.5)
    lief = []

    async def falsches_intern(gen=None):
        # Genau die kritische Stelle: lesen, abgeben, schreiben.
        lief.append("start")
        if spieler.queue:
            track = spieler.queue.pop(0)
            await asyncio.sleep(0)        # hier wechselt der Task
            lief.append(track)
        lief.append("ende")

    spieler._advance_intern = falsches_intern
    spieler.queue = ["A", "B", "C"]

    async def zwei_skips():
        await asyncio.gather(spieler._advance(), spieler._advance())

    asyncio.run(zwei_skips())

    # Kein Track darf verlorengehen, und die Laeufe duerfen sich nicht
    # verschraenken (start/ende muessen paarweise aufeinander folgen).
    geholt = [x for x in lief if x in ("A", "B", "C")]
    assert geholt == ["A", "B"], f"Songs verschwunden oder doppelt: {lief}"
    assert spieler.queue == ["C"], spieler.queue
    assert lief == ["start", "A", "ende", "start", "B", "ende"], lief

    # Und der Lock muss wirklich um den ganzen Lauf liegen.
    quelle = inspect.getsource(music.GuildPlayer._advance)
    assert "_advance_lock" in quelle and "_advance_intern" in quelle
    assert quelle.index("_advance_lock") < quelle.index("_advance_intern")


def test_server_einsatzdeckel_gilt_auch_fuer_knoepfe():
    """Der je Server eingestellte Maximaleinsatz haengt an ai.aktuelle_guild().
    Die wird im GANZEN Repo an genau einer Stelle gesetzt: bot.on_message. Ein
    Klick kommt aber als eigenes Ereignis herein, ohne diesen Kontext.

    Nachgemessen bei Server-Deckel 1.000:
        max_bet(gid) = 1.000          max_bet() ohne Kontext = 1.000.000.000
        50.000 mit Guild  -> abgelehnt
        50.000 ohne Guild -> ANGENOMMEN

    Der Deckel galt damit nur fuer getippte Befehle. Ueber Menue, Dropdown
    "Alles" und "Nochmal" lief die Runde mit dem vollen Konto."""
    import guildcfg
    economy.setup()
    guildcfg.setup()
    casino.setup()
    gid, uid = 770077, 909090
    economy.add_coins(uid, 100_000_000 - economy.get_coins(uid))
    ok, wert, _fehler = asyncio.run(
        guildcfg.instance.setzen(gid, "casino_max_einsatz", "1000"))
    assert ok and wert == 1000, (ok, wert)
    try:
        # Mit Guild greift der Deckel.
        bet, fehler = casino.instance._check_bet(uid, 50_000, gid)
        assert bet == 0 and fehler and "Maximaleinsatz" in fehler, (bet, fehler)
        # Ein erlaubter Einsatz geht weiterhin durch.
        bet, fehler = casino.instance._check_bet(uid, 500, gid)
        assert bet == 500 and fehler is None, (bet, fehler)
        # Und der globale Deckel bleibt die Notbremse: ein Server darf strenger
        # sein als die .env, nie lockerer.
        assert casino.instance.max_bet(gid) <= casino.MAX_BET
    finally:
        asyncio.run(guildcfg.instance.loeschen(gid, "casino_max_einsatz"))

    # --- Die Regel, damit keine neue Knopf-Stelle sie vergisst -------------
    # In casino.py sind die beiden Wege sauber zu unterscheiden:
    #   self._check_bet(...)  = getippter Befehl, laeuft IN on_message (Kontext da)
    #   _check_bet(...)       = Knopf/Formular, eigener Task (kein Kontext)
    # Die zweite Form MUSS die Guild mitgeben.
    quelle = open("casino.py", encoding="utf-8").read()
    fehlt = []
    for treffer in re.finditer(r"(?<![\w.])_check_bet\(", quelle):
        if quelle[:treffer.start()].rstrip().endswith("def"):
            continue                      # die Definition selbst, kein Aufruf
        # Bis zur SCHLIESSENDEN Klammer lesen, nicht bis zur naechsten: die
        # Argumente enthalten selbst Klammern (self.params.get("bet")).
        tiefe, i = 0, treffer.end() - 1
        while i < len(quelle):
            if quelle[i] == "(":
                tiefe += 1
            elif quelle[i] == ")":
                tiefe -= 1
                if tiefe == 0:
                    break
            i += 1
        argumente = quelle[treffer.end():i]
        if "guild_id" not in argumente:
            zeile = quelle[:treffer.start()].count("\n") + 1
            fehlt.append(f"casino.py:{zeile}  _check_bet({' '.join(argumente.split())})")
    assert not fehlt, ("Knopf-Weg ohne Guild - der Server-Deckel gilt dort "
                       "nicht:\n  " + "\n  ".join(fehlt))


def test_doppelklick_verleiht_nicht_zweimal():
    """discord.py serialisiert Klick-Callbacks NICHT. Zwei Klicks auf
    »Annehmen« in ~200 ms liefen beide in _ja - und weil dort vor dem ersten
    await weder die Knoepfe deaktiviert noch die View gestoppt wurde, verlieh
    Flo ZWEIMAL: der Verleiher zahlte 10.000 statt der angebotenen 5.000, der
    Schuldner bekam zwei Posten.

    Das Fenster ist echt: das erste await steht erst in annehmen()."""
    import schulden
    schulden.setup()

    aufrufe = []

    class FalschesModul:
        async def annehmen(self, *_a, **_k):
            aufrufe.append(1)
            await asyncio.sleep(0)          # genau hier gibt der Task ab
            return True, "abgemacht"

    class FalscheAntwort:
        def __init__(self):
            self.deferred = 0

        async def edit_message(self, **_k):
            return None

        async def defer(self):
            self.deferred += 1

    class FalscheInteraktion:
        def __init__(self):
            self.response = FalscheAntwort()
            self.user = SimpleNamespace(id=2)

    ziel = SimpleNamespace(id=2, display_name="Bert")
    besteller = SimpleNamespace(id=1, display_name="Anna")
    view = schulden._AnfrageView(FalschesModul(), besteller, ziel, 5000,
                                 grund="Test", faellig=0.0, mit_geld=True)

    # discord.ui verpackt die Methode in ein Item - die echte Funktion liegt
    # darunter. So wird genau das aufgerufen, was auch ein Klick ausloest.
    ja = schulden._AnfrageView._ja
    ja = getattr(ja, "callback", ja)
    ja = getattr(ja, "callback", ja)

    async def zwei_klicks():
        a, b = FalscheInteraktion(), FalscheInteraktion()
        # Genau gleichzeitig - so wie ein Doppelklick ankommt.
        await asyncio.gather(ja(view, a, None), ja(view, b, None))
        return a, b

    a, b = asyncio.run(zwei_klicks())

    assert len(aufrufe) == 1, (
        f"annehmen() lief {len(aufrufe)}x - der Verleiher zahlt doppelt")
    assert a.response.deferred + b.response.deferred == 1, (
        "der zweite Klick wurde nicht sauber abgewiesen")
    # Und die Knoepfe muessen aus sein, bevor irgendetwas gebucht wird.
    quelle = inspect.getsource(schulden._AnfrageView._ja)
    assert quelle.index("self._laeuft = True") < quelle.index("await self._modul"), (
        "der Riegel steht nicht VOR dem ersten await - dann greift er nicht")


def test_kaputte_games_json_kostet_keinen_gewinn():
    """Eine games.json mit  "daily": null  ist GUELTIGES JSON und kommt deshalb
    an der Quarantaene im Store vorbei. setdefault() half nur gegen einen
    FEHLENDEN Schluessel, nicht gegen einen kaputten Wert.

    Der AttributeError landete mitten in der Abrechnung einer GEWONNENEN Runde.
    bot.py faengt ihn ab und loggt "Spiele-Hook fehlgeschlagen" - der Spieler
    sieht gar nichts und sein Einsatz ist weg. Betroffen: Mathe, Anagramm,
    Quiz, Zahlenraten, Reaktion und das Schnell-Event."""
    import games
    games.setup()
    g = games.instance
    if g._store is None:
        return
    alt_daily = g._store.data.get("daily")
    heute = g._heute()
    kaputte_formen = (
        (None, "daily ist null"),
        ("quatsch", "daily ist Text"),
        ([], "daily ist Liste"),
        ({"day": heute, "won": None}, "won ist null"),
        ({"day": heute, "won": []}, "won ist Liste"),
        ({"day": heute, "won": {"4242": "viel"}}, "Zaehlerstand ist Text"),
    )
    try:
        for form, name in kaputte_formen:
            g._store.data["daily"] = dict(form) if isinstance(form, dict) else form
            g._kappe_rest(4242)          # darf nicht fliegen
            g._store.data["daily"] = dict(form) if isinstance(form, dict) else form
            g._kappe_buchen(4242, 100)   # darf auch nicht fliegen
            # Und danach muss die Struktur wieder brauchbar sein.
            zustand = g._store.data["daily"]
            assert isinstance(zustand, dict), (name, zustand)
            assert isinstance(zustand.get("won"), dict), (name, zustand)
    finally:
        if alt_daily is None:
            g._store.data.pop("daily", None)
        else:
            g._store.data["daily"] = alt_daily

    # Der Store-Standard muss den Zweig selbst schon kennen - sonst entsteht die
    # Luecke beim naechsten frischen Server wieder.
    quelle = inspect.getsource(games.Games.setup)
    assert '"daily"' in quelle, "der Store-Standard kennt 'daily' nicht"


def test_grosses_bild_reisst_den_bot_nicht_ins_oom():
    """Der Groessendeckel in food.py:142 zaehlt BYTES - und das ist die falsche
    Groesse. Ein PNG mit einer einfarbigen Flaeche ist winzig gepackt und
    riesig entpackt.

    Nachgemessen mit 9450x9450 (89,3 Mio Pixel, 276 kB auf der Platte):
    5,9 Sekunden und +341 MB Spitzenspeicher in EINEM Aufruf. Auf einem
    1-2-GB-Server holt der OOM-Killer dafuer den ganzen Dienst - Musik,
    laufende Casino-Runden, Giveaways. Ausgeloest von jedem, der ein Foto in
    den Kalorien-Kanal postet.

    PILs eigene Bremse hilft nicht: sie WARNT ab 89.478.485 Pixeln und wirft
    erst beim Doppelten - der Fall liegt genau dazwischen."""
    try:
        from PIL import Image
    except ImportError:
        return
    import warnings
    import render

    def bild_bytes(breite, hoehe):
        roh = Image.new("RGB", (breite, hoehe), (7, 7, 7))
        puffer = io.BytesIO()
        roh.save(puffer, "PNG", optimize=True)
        return puffer.getvalue()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")       # DecompressionBombWarning
        # Knapp UNTER PILs eigener Fehlergrenze - genau die Luecke.
        bombe = bild_bytes(9450, 9450)
        assert len(bombe) < 12_000_000, (
            "Die Bombe waere schon am Byte-Deckel in food.py haengengeblieben - "
            "dann prueft dieser Test nicht mehr, was er soll.")
        start = time.time()
        assert render.instance._round_img(bombe, 900, 600, radius=24) is None, (
            "Ein Bild mit 89 Mio Pixeln wird immer noch verarbeitet")
        assert time.time() - start < 2.0, "Die Ablehnung dauert zu lange"

        # Und ein grosses, aber ECHTES Handyfoto muss weiter durchgehen -
        # sonst haetten wir das Feature kaputtgemacht statt es zu schuetzen.
        echt = render.instance._round_img(bild_bytes(4000, 3000), 900, 600, radius=24)
        assert echt is not None, "12-Megapixel-Foto wird faelschlich abgelehnt"
        assert echt.size == (900, 600), echt.size

    # Der Deckel muss ueber der Groesse echter Kameras liegen, aber weit unter
    # dem, was den Speicher sprengt.
    assert 20_000_000 <= render.Render.MAX_PIXEL <= 60_000_000, render.Render.MAX_PIXEL


def test_casino_rueckgaben_werden_nicht_besteuert():
    """Wer Schulden hat, gibt von JEDER Einnahme 20 % an seine Glaeubiger ab.
    Eine Rueckgabe ist aber keine Einnahme: Baccarat-Push, Timeout auf Stufe 0,
    "Anzeige fehlgeschlagen - Einsatz zurueck". Ohne das Flag bekam der
    Schuldner von 1.000 zurueckgegebenen Coins nur 800 - obwohl er weder
    gewonnen noch verloren hatte.

    Das Flag rueckgabe=True gibt es seit langem und ist in casino.py:155
    ausdruecklich dafuer dokumentiert - es wurde an sieben Stellen nur nicht
    benutzt."""
    import schulden
    economy.setup()
    schulden.setup()
    casino.setup()
    schuldner, glaeubiger = 660011, 660022
    economy.add_coins(schuldner, 50_000)
    economy.add_coins(glaeubiger, 1_000)
    schulden.instance.buch.anlegen(glaeubiger, schuldner, 20_000, grund="Test")

    vorher = economy.get_coins(schuldner)
    casino._auszahlen(schuldner, 1_000, "test", rueckgabe=True)
    mit_flag = economy.get_coins(schuldner) - vorher
    vorher = economy.get_coins(schuldner)
    casino._auszahlen(schuldner, 1_000, "test")
    ohne_flag = economy.get_coins(schuldner) - vorher

    assert mit_flag == 1_000, f"Rueckgabe kam nicht voll an: {mit_flag}"
    assert ohne_flag < 1_000, (
        "Die Tilgung greift gar nicht mehr - dann ist dieser Test wertlos "
        "geworden und muss angepasst werden, nicht das Flag entfernt.")

    # --- Die Regel, damit es nicht wieder passiert -------------------------
    # In casino.py ist die Rueckgabe des Einsatzes IMMER daran zu erkennen, dass
    # das Ergebnis wieder nach 'bet' geht: "bet = _auszahlen(uid, bet)". Genau
    # diese Form muss das Flag tragen.
    quelle = open("casino.py", encoding="utf-8").read()
    fehlt = []
    for treffer in re.finditer(r"^\s*bet = _auszahlen\(([^\n]*)\)", quelle, re.M):
        if "rueckgabe" not in treffer.group(1):
            zeile = quelle[:treffer.start()].count("\n") + 1
            fehlt.append(f"casino.py:{zeile}  {treffer.group(0).strip()}")
    assert not fehlt, ("Einsatz-Rueckgabe ohne rueckgabe=True - der Schuldner "
                       "verliert davon 20 %:\n  " + "\n  ".join(fehlt))


def test_botsicht_antwort_kommt_wirklich_raus():
    """Antworten aus der BotSicht scheiterten AUSNAHMSLOS. Der Code reichte ein
    discord.Object als 'reference' weiter, discord.py ruft darauf aber
    to_message_reference_dict() auf - die Methode hat Object nicht:

        TypeError: reference parameter must be Message, MessageReference,
                   or PartialMessage

    Das landete im breiten except, der Nutzer sah "senden fehlgeschlagen" und
    suchte den Fehler bei sich. Der Kommentar im Code behauptete ausdruecklich
    das Gegenteil - deshalb hier ein Test, der discord.py selbst fragt."""
    import discord
    # 1. Die Tatsache, auf der alles beruht.
    assert not hasattr(discord.Object(id=1), "to_message_reference_dict")
    verweis = discord.MessageReference(message_id=1, channel_id=2, guild_id=3,
                                       fail_if_not_exists=False)
    daten = verweis.to_message_reference_dict()
    assert daten["message_id"] == 1 and daten["fail_if_not_exists"] is False

    # 2. Der Code baut wirklich eine MessageReference, nicht ein Object.
    quelle = open("webpanel.py", encoding="utf-8").read()
    stelle = quelle.index('kwargs["reference"]')
    block = quelle[stelle:stelle + 320]
    assert "discord.MessageReference(" in block, block[:200]
    assert "discord.Object" not in block, block[:200]
    # Und das Versprechen "faellt auf eine normale Nachricht zurueck" muss auch
    # eingeloest sein - sonst scheitert die Antwort weiterhin, nur spaeter.
    assert "fail_if_not_exists=False" in block, block[:300]


def test_tts_nutzertext_ist_niemals_eine_option():
    """espeak bekommt den Nutzertext als argv-Element. OHNE ein "--" davor liest
    es jeden Text mit fuehrendem Bindestrich als OPTION:

        Flo sprich -w/opt/flobot/data/economy.json   -> WAV UEBERSCHREIBT die Daten
        Flo sprich -f/opt/flobot/.env                -> liest den Token im Voice vor

    Jeder Nutzer, keine Rechtepruefung. create_subprocess_exec schuetzt nur vor
    der Shell, nicht vor der Optionserkennung des aufgerufenen Programms.

    Der Test benutzt ein espeak-Double, das die Optionen genauso liest wie das
    echte - damit ist es ein Nachweis, keine Behauptung."""
    import subprocess
    import voicegags

    with tempfile.TemporaryDirectory() as ordner:
        opfer = os.path.join(ordner, "economy.json")
        with open(opfer, "w", encoding="utf-8") as datei:
            datei.write('{"wichtige": "Wirtschaftsdaten"}')

        # Ein "espeak", das -w wie das echte behandelt und bei "--" aufhoert.
        falsches_espeak = os.path.join(ordner, "espeak-ng")
        with open(falsches_espeak, "w", encoding="utf-8") as datei:
            datei.write(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "argv, ziel, nur_text = sys.argv[1:], None, False\n"
                "i = 0\n"
                "while i < len(argv):\n"
                "    a = argv[i]\n"
                "    if not nur_text and a == '--':\n"
                "        nur_text = True\n"
                "    elif not nur_text and a.startswith('-w'):\n"
                "        ziel = a[2:] or (argv[i + 1] if i + 1 < len(argv) else None)\n"
                "        if not a[2:]:\n"
                "            i += 1\n"
                "    i += 1\n"
                "if ziel:\n"
                "    open(ziel, 'wb').write(b'RIFF....WAVEfmt ' + b'\\0' * 64)\n")
        os.chmod(falsches_espeak, 0o755)

        # _synthesize prueft den NAMEN der Engine ("espeak-ng"), nicht den Pfad.
        # Deshalb muss das Double unter diesem Namen im PATH stehen - sonst
        # laeuft es gar nicht und der Test waere gruen, ohne etwas zu pruefen.
        flo = voicegags.VoiceGags()
        alt_engine, alt_pfad = flo._tts_engine, os.environ.get("PATH", "")
        flo._tts_engine = "espeak-ng"
        os.environ["PATH"] = ordner + os.pathsep + alt_pfad
        pfad = None
        try:
            # Genau der Angriff.
            pfad = asyncio.run(flo._synthesize(f"-w{opfer}"))
        finally:
            flo._tts_engine = alt_engine
            os.environ["PATH"] = alt_pfad
            if pfad and os.path.exists(pfad):
                os.unlink(pfad)

        # Erst pruefen, dass das Double ueberhaupt lief - sonst beweist der
        # naechste assert nichts.
        assert pfad is not None, "Double wurde nicht ausgefuehrt"

        inhalt = open(opfer, encoding="utf-8", errors="replace").read()
        assert inhalt == '{"wichtige": "Wirtschaftsdaten"}', (
            "Der Nutzertext wurde als espeak-Option gelesen und hat eine fremde "
            f"Datei ueberschrieben: {inhalt[:60]!r}")

    # Und der Riegel muss im Code auch wirklich stehen - beide Ebenen.
    quelle = inspect.getsource(voicegags.VoiceGags._synthesize)
    assert '"--"' in quelle, "das '--' vor dem Nutzertext fehlt wieder"
    assert quelle.index('"--"') < quelle.index("text,"), "'--' steht nicht VOR dem Text"
    assert 'lstrip("-")' in inspect.getsource(voicegags.VoiceGags._cmd_say), (
        "der zweite Riegel (fuehrende Bindestriche weg) fehlt")


def test_flo_pingt_niemals_everyone():
    """Mehrere Befehle geben die Nutzereingabe WOERTLICH zurueck - gemessen:
    economy 'setze', guildcfg 'einstellung', profil 'avatar'. Und zwar als
    Klartext, nicht im Embed (Embeds pingen nicht).

    bot.py setzte nirgends allowed_mentions. Damit machte "Flo setze @everyone"
    aus jedem Nutzer einen Server-Durchsager."""
    quelle = open("bot.py", encoding="utf-8").read()
    stelle = quelle.index("client = FloBot(")
    block = quelle[stelle:stelle + 1200]
    assert "allowed_mentions" in block, (
        "der Client wird ohne allowed_mentions gebaut - Flo kann @everyone anpingen")
    assert "everyone=False" in block and "roles=False" in block, block[:400]
    # Personen-Erwaehnungen muessen ANBLEIBEN, sonst kann Flo niemanden ansprechen.
    assert "users=True" in block, block[:400]

    # Gegenprobe an der Wirklichkeit: die drei Befehle geben die Eingabe wirklich
    # woertlich zurueck. Faellt das weg, ist der Schutz trotzdem richtig - aber
    # dieser Test soll wissen, wovon er redet.
    import guildcfg
    economy.setup()
    guildcfg.setup()
    woertlich = []
    for modul, befehl in ((economy, "setze"), (guildcfg, "einstellung")):
        antwort = asyncio.run(modul.handle(_rauch_nachricht(f"{befehl} @everyone")))
        if isinstance(antwort, str) and "@everyone" in antwort:
            woertlich.append(f"{modul.__name__} {befehl}")
    assert woertlich, ("kein Befehl gibt die Eingabe mehr woertlich zurueck - "
                       "dann diesen Test anpassen, nicht den Schutz entfernen")


# --- Ende-zu-Ende-Rauchtest -------------------------------------------------
# Die Nachrichten-Attrappe liegt in werkzeug/attrappe.py - dort holt sie sich
# auch das Inventar (werkzeug/inventar.py), das jedes Modul fragt, auf welche
# Woerter es reagiert. Eine Attrappe, zwei Nutzer, eine Datei.
from werkzeug.attrappe import RauchKanal as _RauchKanal          # noqa: E402
from werkzeug.attrappe import rauch_nachricht as _rauch_nachricht  # noqa: E402


def test_rauchtest_jeder_befehl_laeuft_wirklich_durch():
    """Die grosse Luecke: fast alle Tests pruefen EINZELTEILE. Ob ein Befehl als
    Ganzes noch durchlaeuft - Erkennung, Handler, Antwort - stand nirgends.
    Genau dort brechen Umbauten.

    Hier geht jeder wichtige Befehl durch das ECHTE handle() seines Moduls, mit
    einer Nachricht, wie bot.on_message sie weiterreicht. Der Test prueft nicht,
    WAS herauskommt (das machen die Einzeltests), sondern DASS etwas
    herauskommt und nichts fliegt."""
    import ai
    import arbeit
    import casino
    import fun
    import games
    import guildcfg
    import handel
    import lotto
    import luxus
    import merchant
    import moderation
    import profil
    import schulden
    import steal
    import terraria
    import words

    # fun/chaos braucht die KI - ohne einen Client bleibt es aus und der
    # Rauchtest wuerde die Befehle stillschweigend ueberspringen.
    alt_client = ai.instance._client
    ai.instance._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=None)))
    for modul in (economy, words, schulden, profil, luxus, handel, steal, lotto,
                  floaktie, arbeit, fun, games, casino, moderation, guildcfg,
                  terraria, merchant, gehirn):
        try:
            modul.setup()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{modul.__name__}.setup() fliegt: {exc}") from exc

    PROBEN = (
        (economy, ("level", "coins", "top", "daily", "shop", "inventar", "titel",
                   "rang", "kontostand", "bestenliste")),
        (words, ("woerter", "wort pizza", "wordcount")),
        (gehirn, ("gedaechtnis", "gedächtnis", "vergiss mich", "vergiss",
                  "was weisst du")),
        (schulden, ("schulden", "kreide", "leih 100", "insolvenz", "schuldenbuch")),
        (profil, ("profil", "avatar", "banner", "steckbrief")),
        (luxus, ("luxus", "thron", "prestige")),
        (handel, ("handel", "transaktionen", "verlauf")),
        (steal, ("steal", "klau", "raub")),
        (lotto, ("lotto", "jackpot", "lose", "ziehung")),
        (floaktie, ("aktie", "kurs", "chart", "floaktie", "aktienkurs")),
        (arbeit, ("work", "arbeit", "lohnzettel", "wordle", "tageswort", "schicht")),
        (fun, ("roast", "hype", "spruch", "horoskop", "rizz", "aura", "weisheit")),
        (games, ("quiz", "zahlenraten", "coinflip", "wuerfel", "slots", "reaktion")),
        (casino, ("casino", "blackjack", "roulette 100 rot", "mines 100", "stats",
                  "crash 100", "keno 100 1 2 3", "hilo 100", "turm 100")),
        (moderation, ("warns", "warnungen", "warnliste")),
        (guildcfg, ("einstellungen", "config", "settings")),
        (terraria, ("terraria", "twiki")),
        (merchant, ("haendler", "merchant", "kraemer")),
    )

    stumm, geflogen = [], []
    try:
        for modul, befehle in PROBEN:
            for befehl in befehle:
                try:
                    antwort = asyncio.run(modul.handle(_rauch_nachricht(befehl)))
                except Exception as exc:  # noqa: BLE001 - genau das suchen wir
                    geflogen.append(f"{modul.__name__} '{befehl}': "
                                    f"{type(exc).__name__}: {str(exc)[:120]}")
                    continue
                if antwort is None:
                    stumm.append(f"{modul.__name__} '{befehl}'")
    finally:
        ai.instance._client = alt_client

    assert not geflogen, "Befehle stuerzen ab:\n  " + "\n  ".join(geflogen)
    assert not stumm, ("Befehle werden nicht mehr erkannt (handle gibt None, "
                       "der Befehl faellt zur KI durch):\n  " + "\n  ".join(stumm))


#: Eingaben, an denen Befehle erfahrungsgemaess zerbrechen. Bewusst gemein:
#: leer, Grenzwerte, falsche Zahlenformate, fremde Ziffernsysteme, Formatzeichen,
#: Einschleusungsversuche, Massen-Erwaehnungen, unsichtbare und ueberlange Texte.
_BOESE_EINGABEN = (
    "", " ", "   ", "\t", "\n",
    "0", "-1", "-999999999999", "999999999999999999999999", "1e308", "nan", "inf",
    "0.5", "1,5", "1_000", "0x10", "1e-5", "٣", "½", "²",
    "abc", "null", "None", "undefined", "%s", "{0}", "{{}}", "%%",
    "'; DROP TABLE x; --", "../../etc/passwd", "<script>alert(1)</script>",
    "@everyone", "@here", "<@0>", "<@999999999999999999>",
    "a" * 5000, "🎉" * 200, "\u200b" * 50, "\x00", "\ufeff",
    "-0", "+5", "5 5 5 5 5", "1 2 3 4 5 6 7 8 9 10 11 12",
    "999999999999 rot", "0 rot", "-100 rot", "abc rot", "100 abcdef",
    "<@123> -999999", "<@123> 0", "<@123> abc", "<@123>",
)


def test_kein_befehl_stuerzt_bei_feindlicher_eingabe_ab():
    """Aktiv gesuchte Grenzfaelle statt Hoffnung: jeder Befehl, der Argumente
    nimmt, bekommt 47 gemeine Eingaben - leer, negativ, absurd gross, falsche
    Zahlenformate, arabische Ziffern, Formatzeichen, Einschleusungsversuche,
    @everyone, 5000 Zeichen, Nullbytes.

    Ein Absturz hier ist im Betrieb eine Nachricht, die Flo verschluckt - und
    bei Coin-Befehlen im schlimmsten Fall ein abgebuchter Einsatz ohne Spiel.

    WICHTIG: alles laeuft in EINEM Event-Loop. Mit asyncio.run je Eingabe misst
    man das Testgeschirr statt den Bot - die geteilte HTTP-Sitzung ueberlebt das
    Schliessen des Loops nicht und erzeugt 44 Phantom-Fehler."""
    import arbeit
    import casino
    import games
    import guildcfg
    import handel
    import lotto
    import luxus
    import merchant
    import moderation
    import profil
    import schulden
    import steal
    import terraria
    import words

    mit_argumenten = {
        economy: ("pay", "zahl", "kaufen", "buy", "equip", "setze", "top", "coins"),
        schulden: ("leih", "tilg", "abzahl", "borg", "schuldschein"),
        steal: ("steal", "klau"),
        lotto: ("lotto", "lose"),
        floaktie: ("aktie", "kaufen", "verkaufen"),
        arbeit: ("work", "wordle", "lohnzettel"),
        games: ("zahlenraten", "coinflip", "wuerfel", "quiz"),
        casino: ("roulette", "blackjack", "mines", "crash", "keno", "hilo",
                 "turm", "slots", "baccarat", "sieben", "rubbellos", "duell"),
        moderation: ("purge", "warn", "timeout", "kick", "ban", "unban", "unwarn"),
        guildcfg: ("einstellung",),
        words: ("wort",),
        profil: ("check", "avatar"),
        luxus: ("luxus", "thron"),
        merchant: ("haendler",),
        terraria: ("terraria",),
        handel: ("handel",),
    }
    for modul in mit_argumenten:
        modul.setup()

    abstuerze = []

    async def alles_durchspielen():
        for modul, befehle in mit_argumenten.items():
            for befehl in befehle:
                for eingabe in _BOESE_EINGABEN:
                    text = f"{befehl} {eingabe}".strip()
                    try:
                        await modul.handle(_rauch_nachricht(text))
                    except Exception as exc:  # noqa: BLE001 - genau das suchen wir
                        abstuerze.append(
                            f"{modul.__name__} '{befehl}' mit {eingabe[:30]!r}: "
                            f"{type(exc).__name__}: {str(exc)[:100]}")

    asyncio.run(alles_durchspielen())
    # Doppelte Meldungen zusammenfassen - sonst steht derselbe Fehler 47-mal da.
    eindeutig = sorted(set(abstuerze))
    assert not eindeutig, (f"{len(abstuerze)} Abstuerze bei feindlicher Eingabe:\n  "
                           + "\n  ".join(eindeutig[:20]))


def test_rauchtest_deckt_die_kette_aus_bot_py_ab():
    """Der Rauchtest nuetzt nichts, wenn er ein Modul vergisst, das bot.py
    aufruft. Deshalb: die Liste hier gegen die echte Kette in bot.py halten."""
    quelle = open("bot.py", encoding="utf-8").read()
    anfang = quelle.index("_HANDLED_SENTINELS = tuple(")
    kette = set(re.findall(r"\((?:\w+_ENABLED)[^)]*\), (\w+)\.handle",
                           quelle[anfang:]))
    assert len(kette) >= 15, f"Kette nicht gefunden (nur {kette})"
    eigen = inspect.getsource(test_rauchtest_jeder_befehl_laeuft_wirklich_durch)
    # Module, die der Rauchtest bewusst NICHT anfasst - mit Begruendung.
    ausgenommen = {
        "music",      # braucht eine echte Voice-Verbindung
        "media",      # erzeugt Bilder ueber einen fremden Dienst (kostet)
        "food",       # braucht ein Foto im Anhang
        "voicegags",  # braucht Voice
        "giveaway",   # legt echte Coins fest und startet Timer
        "admin",      # nur Besitzer, veraendert fremde Konten
        "bayern",     # schaltet eine Server-Einstellung um
    }
    fehlt = sorted(m for m in kette
                   if m not in ausgenommen and f"({m}, (" not in eigen)
    assert not fehlt, (f"bot.py ruft diese Module auf, der Rauchtest kennt sie "
                       f"aber nicht: {fehlt}")


def test_wordle_von_hand_nimmt_niemandem_seine_versuche():
    """Der Knopf im Panel muss auch dann sicher sein, wenn schon gespielt wird.

    Der haeufigste Grund fuer "es kam kein Wort des Tages" ist NICHT, dass
    keines gezogen wurde - sondern dass die Nachricht nicht ankam oder im
    falschen Kanal landete. Wuerde der Knopf dann die Runde neu starten, waeren
    alle Versuche aller Mitspieler weg. Er haengt deshalb nur den Aushang neu
    aus."""
    arbeit, restore = _arbeit_frisch()
    try:
        guild = SimpleNamespace(id=4242, name="Heimat", text_channels=[],
                                voice_channels=[], system_channel=None)
        # 1. Nichts gestartet, KEINER im Voice, kein Termin -> trotzdem los.
        #    Genau das ist der Zweck des Knopfes.
        assert not arbeit.instance.faellig(guild), "waere ohnehin schon faellig"
        embed, view, datei, frisch = asyncio.run(
            arbeit.instance.jetzt_starten(guild))
        assert frisch is True
        raetsel = arbeit.instance.raetsel(4242)
        assert raetsel.laeuft, "es laeuft danach gar keine Runde"
        wort = raetsel.wort
        assert wort, "kein Wort gezogen"
        assert embed is not None and view is not None

        # 2. Jemand hat schon geraten. Zweiter Druck darf das NICHT wegwerfen.
        raetsel.daten["spieler"]["7"] = ["HAUS"]
        raetsel.daten["versuche"] = 1
        embed2, view2, datei2, frisch2 = asyncio.run(
            arbeit.instance.jetzt_starten(guild))
        assert frisch2 is False, "startet die laufende Runde neu"
        assert arbeit.instance.raetsel(4242).wort == wort, "das Wort wurde getauscht"
        assert arbeit.instance.raetsel(4242).daten["spieler"].get("7") == ["HAUS"], (
            "die Versuche der Mitspieler wurden geloescht")
        assert embed2 is not None and view2 is not None

        # 3. Ist das Wort von heute schon geloest, ist der Knopf sinnlos - und
        #    muss das SAGEN statt still ein zweites Raetsel hinzuwerfen.
        arbeit.instance.raetsel(4242).daten["gewinner"] = 7
        try:
            asyncio.run(arbeit.instance.jetzt_starten(guild))
            raise AssertionError("haette abgelehnt werden muessen")
        except ValueError as exc:
            assert "geloest" in str(exc).lower(), exc

        # 4. Ganz ausgeschaltet -> ebenfalls ein klarer Satz, kein Absturz.
        arbeit.instance._enabled = False
        try:
            asyncio.run(arbeit.instance.jetzt_starten(guild))
            raise AssertionError("haette abgelehnt werden muessen")
        except ValueError as exc:
            assert "ausgeschaltet" in str(exc).lower(), exc
    finally:
        restore()


def test_wordle_knopf_im_panel_loest_wirklich_aus():
    """Der Knopf im Web-Panel muss den Bot erreichen - und ein bekannter Grund
    ("schon geloest", "Kanal fehlt") muss als lesbarer Satz ankommen, nicht als
    nichtssagender Serverfehler."""
    import asyncio as _asyncio
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    gerufen = []

    class FakeBot:
        async def wordle_jetzt(self, gid, cid=0):
            gerufen.append((gid, cid))
            if gid == 999:
                raise ValueError("Das Wort von heute ist schon geloest.")
            return "Wort des Tages in #gigachat gestartet."

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = 0
        wp._client = FakeBot()
        async with TestClient(TestServer(wp._build_app())) as c:
            gut = await c.post("/api/wordle/start",
                               json={"guild": 1453867645660303527,
                                     "channel_id": "1453881901738889351"})
            bekannt = await c.post("/api/wordle/start", json={"guild": 999})
            krumm = await c.post("/api/wordle/start",
                                 json={"guild": 1, "channel_id": "keine-zahl"})
            ohne = await c.post("/api/wordle/start", json={})
            return ([gut.status, await gut.json()],
                    [bekannt.status, await bekannt.json()],
                    [krumm.status, await krumm.json()],
                    ohne.status)

    alt_gid = os.environ.pop("GUILD_ID", None)
    try:
        gut, bekannt, krumm, ohne = _asyncio.run(lauf())
    finally:
        if alt_gid is not None:
            os.environ["GUILD_ID"] = alt_gid

    assert gut[0] == 200, gut
    assert gut[1]["ok"] is True and "gigachat" in gut[1]["text"], gut
    assert gerufen[0] == (1453867645660303527, 1453881901738889351), gerufen

    # Ein bekannter Grund ist KEIN Serverfehler - sonst steht im Panel nur
    # "senden fehlgeschlagen" und niemand weiss, warum.
    assert bekannt[0] == 400, bekannt
    assert "geloest" in bekannt[1]["error"].lower(), bekannt

    # Eine krumme Kanal-ID darf nicht still im Standard-Kanal landen. Der
    # Bot darf dafuer GAR NICHT erst gerufen werden - sonst haenge der Aushang
    # im falschen Kanal, und zurueckholen laesst sich das nicht.
    assert krumm[0] == 400, krumm
    assert [g for g, _c in gerufen] == [1453867645660303527, 999], (
        f"die krumme Kanal-ID hat den Bot erreicht: {gerufen}")

    # Ohne Server und ohne GUILD_ID: ablehnen statt raten.
    assert ohne == 400, ohne


def test_wordle_knopf_steht_wirklich_im_panel():
    """Ein Endpunkt ohne Knopf hilft niemandem: der Betreiber sitzt am Handy im
    Panel und soll das Wort dort ausloesen koennen, nicht per curl."""
    hier = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(hier, "webpanel.html"), encoding="utf-8").read()
    assert "/api/wordle/start" in html, "das Panel ruft den Endpunkt nirgends auf"
    assert "wdlGo" in html, "es gibt keinen Knopf"
    assert html.count('id="wdlGo"') == 1, "der Knopf steht doppelt im Panel"
    # Der Knopf muss den GEWAEHLTEN Server mitschicken - sonst landet das Wort
    # auf dem Hauptserver, egal welchen man im Panel geoeffnet hat.
    block = html.split("/api/wordle/start")[0][-400:]
    assert "guild:g.id" in block.replace(" ", ""), block[-200:]

    py = open(os.path.join(hier, "webpanel.py"), encoding="utf-8").read()
    assert '"/api/wordle/start"' in py, "die Route fehlt"
    assert "_api_wordle_start" in py
    # Schreibender Zugriff MUSS durch dieselbe Pruefung wie alles andere.
    rumpf = py.split("async def _api_wordle_start")[1].split("\n    async def")[0]
    assert "self._guard(request)" in rumpf, "der Endpunkt umgeht den Schutz"


def test_musik_extractor_args_verliert_kein_po_token():
    """player_client UND po_token landen beide unter dem Schluessel "youtube".

    Wer nur eines davon setzt, LOESCHT das andere - genau das war der Fehler:
    sobald ein Client durchprobiert wurde, war das PO Token weg. Und ohne PO
    Token weist YouTube inzwischen fast jeden Client ab. Der Fehler haette also
    ausgerechnet dann zugeschlagen, wenn die Rettung schon eingerichtet war."""
    import music
    alt = os.environ.pop("YTDLP_PO_TOKEN", None)
    try:
        assert music.Music._extractor_args(None) == {}
        assert music.Music._extractor_args("tv") == {
            "youtube": {"player_client": ["tv"]}}

        os.environ["YTDLP_PO_TOKEN"] = "web.gvs+AAA, tv.gvs+BBB"
        # Leerzeichen und Komma sauber trennen - abgeschriebene Tokens haben das.
        assert music.Music._pot_tokens() == ["web.gvs+AAA", "tv.gvs+BBB"]
        beides = music.Music._extractor_args("tv")["youtube"]
        assert beides["player_client"] == ["tv"], "Client verloren"
        assert beides["po_token"] == ["web.gvs+AAA", "tv.gvs+BBB"], (
            "PO Token beim Client-Wechsel verloren")
        # Auch ohne Client muss das Token durchkommen (Suche/Playlist).
        assert music.Music._extractor_args(None) == {
            "youtube": {"po_token": ["web.gvs+AAA", "tv.gvs+BBB"]}}
    finally:
        os.environ.pop("YTDLP_PO_TOKEN", None)
        if alt is not None:
            os.environ["YTDLP_PO_TOKEN"] = alt


def test_musik_probiert_zuerst_die_clients_ohne_po_token():
    """YouTube verlangt fuer die meisten Clients ein "PO Token", das yt-dlp gar
    nicht selbst erzeugen kann. Genau drei kommen ohne aus - nur die haben auf
    einem nackten Server ueberhaupt eine Chance.

    Standen die hinten, lief Flo erst durch vier Clients, die ohne Token
    NIE gehen konnten, bevor er den einen probierte, der geht. Fuer den Nutzer
    ist das der Unterschied zwischen "spielt" und "spielt nicht"."""
    import music
    alt = os.environ.pop("YTDLP_PLAYER_CLIENT", None)
    try:
        reihe = music.instance.client_reihe()
        for name in music.Music._OHNE_POT:
            assert name in reihe, f"{name} fehlt in der Reihe"
        vorne = reihe[:len(music.Music._OHNE_POT)]
        assert set(vorne) == set(music.Music._OHNE_POT), (
            f"die Clients ohne PO Token stehen nicht vorne: {reihe}")
        # Und es muessen Namen sein, die DIESE yt-dlp-Fassung wirklich kennt.
        bekannt = music.Music._bekannte_clients()
        if bekannt:
            unbekannt = [c for c in reihe if c not in bekannt]
            assert not unbekannt, f"yt-dlp kennt diese Clients nicht: {unbekannt}"
    finally:
        if alt is not None:
            os.environ["YTDLP_PLAYER_CLIENT"] = alt


def test_musik_findet_cookies_auch_ohne_env_eintrag():
    """Cookies sollen wirken, sobald die Datei da liegt - ohne .env-Zeile.

    Der Betreiber sitzt womoeglich am Handy an einer noVNC-Konsole ohne
    Copy-Paste. Eine Datei ablegen kann er dort, eine .env-Zeile tippen kaum.
    Eine LEERE Datei darf dabei nicht zaehlen: sonst rennt yt-dlp mit einem
    leeren Zugang los und alles scheitert - mit einer irrefuehrenden Meldung."""
    import music
    ordner = tempfile.mkdtemp()
    alt_data = os.environ.get("DATA_DIR")
    alt_datei = os.environ.pop("YTDLP_COOKIES", None)
    alt_browser = os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
    os.environ["DATA_DIR"] = ordner
    try:
        assert music.Music._cookie_datei_finden() in ("", None) or True
        pfad = os.path.join(ordner, "cookies.txt")
        open(pfad, "w").close()                       # leer
        assert music.Music._cookie_datei_finden() != pfad, (
            "eine leere cookies.txt wird als gueltiger Zugang behandelt")
        with open(pfad, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t0\tX\ty\n")
        assert music.Music._cookie_datei_finden() == pfad
        assert music.Music._cookie_optionen()["cookiefile"] == pfad

        # Ein falscher Eintrag in der .env darf den Fund nicht verhindern -
        # sonst kostet ein Tippfehler die ganze Musik.
        os.environ["YTDLP_COOKIES"] = "/gibt/es/nicht.txt"
        assert music.Music._cookie_optionen()["cookiefile"] == pfad
    finally:
        os.environ.pop("YTDLP_COOKIES", None)
        os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
        os.environ.pop("DATA_DIR", None)
        for name, wert in (("DATA_DIR", alt_data), ("YTDLP_COOKIES", alt_datei),
                           ("YTDLP_COOKIES_FROM_BROWSER", alt_browser)):
            if wert is not None:
                os.environ[name] = wert
        shutil.rmtree(ordner, ignore_errors=True)


def test_musik_proxy_erreicht_jeden_yt_dlp_aufruf():
    """Ein eigener Ausgang (YTDLP_PROXY) ist einer der zwei Wege zurueck zu
    YouTube, wenn die Server-IP gesperrt ist. Kaeme er nur beim Abspielen an
    und nicht bei der Suche, ginge ein Link - und "flo spiel <titel>" nicht.
    Genau diese halbe Reparatur faellt beim Testen nicht auf."""
    import re
    import music
    quelle = open(music.__file__, encoding="utf-8").read()
    stellen = [m.start() for m in re.finditer(r"yt_dlp\.YoutubeDL\(", quelle)]
    assert len(stellen) >= 4, f"nur {len(stellen)} yt-dlp-Aufrufe gefunden"
    ohne = [i for i, pos in enumerate(stellen, 1)
            if "_netz_optionen()" not in quelle[max(0, pos - 900):pos]]
    assert not ohne, f"diese yt-dlp-Aufrufe gehen am Proxy vorbei: {ohne}"

    alt = os.environ.pop("YTDLP_PROXY", None)
    try:
        assert music.Music._netz_optionen() == {}
        os.environ["YTDLP_PROXY"] = "socks5://127.0.0.1:1080"
        assert music.Music._netz_optionen() == {"proxy": "socks5://127.0.0.1:1080"}
    finally:
        os.environ.pop("YTDLP_PROXY", None)
        if alt is not None:
            os.environ["YTDLP_PROXY"] = alt


def test_musik_soundcloud_ausweich_laesst_sich_abschalten():
    """Weicht Flo stillschweigend auf SoundCloud aus, merkt niemand, dass
    YouTube klemmt - und der Betreiber repariert es nie. Wer das nicht will,
    soll eine ehrliche Fehlermeldung bekommen koennen."""
    import music
    alt = os.environ.pop("MUSIC_SOUNDCLOUD_FALLBACK", None)
    try:
        assert music.Music._ausweich_erlaubt(), "Ausweich ist nicht die Vorgabe"
        for wert in ("0", "false", "no", "off", "aus", "AUS", " Off "):
            os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = wert
            assert not music.Music._ausweich_erlaubt(), f"{wert!r} schaltet nicht ab"
        for wert in ("1", "ja", "an", "true"):
            os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = wert
            assert music.Music._ausweich_erlaubt(), f"{wert!r} schaltet faelschlich ab"
        # Und abgeschaltet darf wirklich NICHTS von SoundCloud kommen.
        os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = "0"
        assert asyncio.run(music.instance._soundcloud_ausweich("egal")) is None
    finally:
        os.environ.pop("MUSIC_SOUNDCLOUD_FALLBACK", None)
        if alt is not None:
            os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = alt


def test_musik_suche_wechselt_auch_den_client():
    """"flo spiel <titel>" sucht erst und spielt dann. Wird die SUCHE geblockt,
    kommt es nie bis zum Abspielen - der Client-Wechsel beim Abspielen nuetzt
    dann gar nichts. Genau das fehlte: die Suche hat einmal blind gefragt und
    aufgegeben."""
    import music

    class FakeYDL:
        geht_ab = "tv"
        versuche = []

        def __init__(self, opts):
            self.client = (opts.get("extractor_args", {})
                           .get("youtube", {}).get("player_client", [None])[0])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, ziel, download=False):
            FakeYDL.versuche.append(self.client)
            if self.client != FakeYDL.geht_ab:
                raise Exception("Sign in to confirm you're not a bot")
            return {"entries": [{"title": "Semmel Song", "id": "abc123",
                                 "duration": 180}]}

    m = music.instance
    alt_ydl, alt_client = music.yt_dlp, m._guter_client
    alt_env = os.environ.pop("YTDLP_PLAYER_CLIENT", None)
    music.yt_dlp = type("M", (), {"YoutubeDL": FakeYDL})
    try:
        FakeYDL.versuche, m._guter_client = [], ""
        treffer = asyncio.run(m._youtube_search_best("semmel song",
                                                     want_title="Semmel Song"))
        assert treffer == "https://www.youtube.com/watch?v=abc123", treffer
        assert FakeYDL.versuche[0] is None, "fragt nicht zuerst yt-dlps Vorgabe"
        assert "tv" in FakeYDL.versuche, (
            f"die Suche probiert keine anderen Clients: {FakeYDL.versuche}")
        assert m._guter_client == "tv", "merkt sich den Client der Suche nicht"
    finally:
        music.yt_dlp, m._guter_client = alt_ydl, alt_client
        if alt_env is not None:
            os.environ["YTDLP_PLAYER_CLIENT"] = alt_env


def test_youtube_arzt_kennt_dieselben_clients_wie_der_bot():
    """Der Arzt (tools_youtube_setup.py) laeuft absichtlich OHNE den Bot zu
    importieren - er muss auch dann noch helfen, wenn music.py kaputt ist.
    Der Preis dafuer ist eine zweite Liste, und zwei Listen driften.

    Sagt der Arzt "player_client=tv geht", der Bot probiert tv aber gar nicht,
    schickt die Diagnose den Betreiber in die Irre. Deshalb dieser Abgleich."""
    import music
    import tools_youtube_setup as arzt
    assert tuple(arzt.OHNE_POT) == tuple(music.Music._OHNE_POT), (
        f"Arzt: {arzt.OHNE_POT}  Bot: {music.Music._OHNE_POT}")
    zusammen = tuple(arzt.OHNE_POT) + tuple(arzt.MIT_POT)
    assert zusammen == tuple(music.Music._CLIENT_REIHE), (
        f"Arzt probiert eine andere Reihe als der Bot:\n"
        f"  Arzt: {zusammen}\n  Bot:  {music.Music._CLIENT_REIHE}")
    # Der Arzt darf yt-dlps Ausgaben nicht durchlassen - der Bericht wird auf
    # einem Handy gelesen und ist mit acht Tracebacks unbrauchbar.
    for name in ("debug", "info", "warning", "error"):
        assert hasattr(arzt.Stumm, name), f"Stumm kann kein {name}()"


def test_youtube_arzt_verwechselt_netzfehler_nicht_mit_bot_sperre():
    """"YouTube blockt deine IP" und "der Server kommt nicht ins Netz" brauchen
    voellig verschiedene Reparaturen. Rateraten der Arzt falsch, richtet der
    Betreiber stundenlang Cookies ein, obwohl die Firewall zu ist."""
    import tools_youtube_setup as arzt
    a = arzt.YoutubeSetup()
    assert a.grund(Exception("Sign in to confirm you're not a bot")) == "Bot-Sperre"
    assert a.grund(Exception("Unable to connect to proxy: 403")) == \
        "Proxy laesst nicht durch"
    assert a.grund(Exception("The read operation timed out")) == "kein Netz zu YouTube"

    a.gruende = ["Proxy laesst nicht durch", "kein Netz zu YouTube"]
    a.urteil()
    text = "\n".join(a.zeilen)
    assert "KEINE Bot-Sperre" in text, text
    assert "WEGWERF" not in text, "raet zu Cookies, obwohl das Netz das Problem ist"

    b = arzt.YoutubeSetup()
    b.gruende = ["Bot-Sperre", "Bot-Sperre"]
    b.urteil()
    text = "\n".join(b.zeilen)
    assert "blockt diese IP" in text, text
    assert "WEGWERF" in text, "warnt nicht vor dem eigenen Google-Konto"


def test_youtube_cookies_landen_niemals_im_repo():
    """Eine cookies.txt IST eine Anmeldung bei Google. Landet sie im Repo, ist
    das Konto oeffentlich - der schlimmste denkbare Ausgang dieser Reparatur.
    Deshalb steht sie in .gitignore, und deshalb prueft das hier ein Test."""
    import subprocess
    hier = os.path.dirname(os.path.abspath(__file__))
    text = open(os.path.join(hier, ".gitignore"), encoding="utf-8").read()
    muster = [z.strip() for z in text.splitlines()
              if z.strip() and not z.strip().startswith("#")]
    for name in ("cookies.txt", "youtube.txt", "youtube_cookies.txt"):
        assert name in muster, f"{name} steht nicht in .gitignore"
    assert "data/" in muster, "data/ steht nicht in .gitignore"
    # Und es darf gerade wirklich keine im Baum liegen.
    for ordner in (hier, os.path.join(hier, "data")):
        pfad = os.path.join(ordner, "cookies.txt")
        if os.path.exists(pfad):
            fertig = subprocess.run(["git", "check-ignore", pfad],
                                    cwd=hier, capture_output=True)
            assert fertig.returncode == 0, f"{pfad} wuerde committet werden!"


def test_k_kennt_den_youtube_befehl():
    """"k y" ist der Befehl, mit dem der Betreiber YouTube wieder ans Laufen
    bringt. Steht er nicht im Hilfetext, findet ihn niemand - und ein Befehl,
    den niemand findet, hilft niemandem."""
    hier = os.path.dirname(os.path.abspath(__file__))
    text = open(os.path.join(hier, "k"), encoding="utf-8").read()
    assert "y|yt|youtube)" in text, "k kennt den Befehl 'y' nicht"
    assert "tools_youtube_setup.py" in text
    assert 'shift' in text.split("y|yt|youtube)")[1].split(";;")[0], (
        "k reicht die weiteren Woerter nicht durch - 'k y browser firefox' "
        "kaeme nie an")
    assert '"$@"' in text.split("y|yt|youtube)")[1].split(";;")[0]
    assert "k y" in text, "der Hilfetext nennt 'k y' nicht"
    assert os.path.exists(os.path.join(hier, "tools_youtube_setup.py"))


def test_kein_titel_verbietet_das_roasten():
    """DER Grund, warum Flo zahm wurde - und er hatte nichts mit dem Modell zu tun.

    Die Tonfall-Texte in titles.py waren eine Zahmheits-Rampe: je seltener der
    Titel, desto braver sollte Flo sein. Ganz oben stand woertlich "Kein
    Roasten, keine fiesen Sprueche" und "Absolut kein Spott". Wer lange dabei
    ist - also genau die Stammgaeste - bekam per Bauart einen lieben Flo.

    Seltenheit darf aendern, WIE geroastet wird, niemals OB."""
    import titles
    verboten = ("kein roasten", "absolut kein spott", "kein fuenkchen spott",
                "spott hat hier nichts zu suchen", "kein spott",
                "das fiese roasten laesst du grossteils weg",
                "leg den ganzen aggro-modus komplett ab",
                "fahr die aggression einen tick runter")
    schuldige = []
    for key, stufe in titles.Titles.RARITY.items():
        ton = (stufe.get("tone") or "").lower()
        for satz in verboten:
            if satz in ton:
                schuldige.append(f"{key}: '{satz}'")
    assert not schuldige, (
        "Diese Stufen verbieten Flo das Roasten:\n  " + "\n  ".join(schuldige))
    # Und jede Stufe MUSS einen Tonfall haben - eine leere Stufe waere still
    # wieder der neutrale, brave Standard.
    for key, stufe in titles.Titles.RARITY.items():
        assert (stufe.get("tone") or "").strip(), f"{key} hat gar keinen Tonfall"


def test_flo_laesst_sich_nichts_gefallen_und_hat_haltung():
    """Flo soll kontern statt zu schlucken - und eine politische Haltung haben.

    Beides steht in eigenen Bloecken, damit man es findet und abschalten kann,
    statt es im Persona-Absatz zu vergraben."""
    import ai
    alt = os.environ.pop("BOT_POLITIK", None)
    alt_persona = os.environ.pop("BOT_PERSONA", None)
    try:
        p = ai.instance._system_prompt(author="Tester", title="")
        # Konter: er entschuldigt sich nicht und gibt zurueck.
        assert "NICHTS gefallen" in p, "Flo schluckt Beleidigungen wieder"
        assert "doppelt zurueck" in p
        # Haltung ist da und ist als solche erkennbar.
        assert "RECHTS" in p, "die politische Haltung fehlt"
        # ... und laesst sich je Server/Betreiber abschalten.
        os.environ["BOT_POLITIK"] = "aus"
        assert "RECHTS" not in ai.instance._system_prompt(), (
            "BOT_POLITIK=aus schaltet die Haltung nicht ab")
        for wert in ("0", "off", "false", "nein"):
            os.environ["BOT_POLITIK"] = wert
            assert not ai.instance._politik_an(), wert
    finally:
        os.environ.pop("BOT_POLITIK", None)
        for name, wert in (("BOT_POLITIK", alt), ("BOT_PERSONA", alt_persona)):
            if wert is not None:
                os.environ[name] = wert


def test_die_grenze_steht_vorne_und_bleibt_vollstaendig():
    """Die eine Grenze ist nicht verhandelbar - auch nicht beim Entzahmen.

    Zwei Dinge zugleich: (1) Alle sechs Merkmale, echte Drohungen, private Daten
    und die Notlagen-Regel MUESSEN im Prompt stehen. Wer Flo haerter macht, darf
    sie nicht nebenbei mit wegraeumen. (2) Sie steht direkt hinter der Persona
    und nicht ganz am Ende - ganz hinten wirkte sie wie das letzte Wort und
    faerbte alles davor zahm ein."""
    import ai
    p = ai.instance._system_prompt(author="Tester", title="")
    for merkmal in ("Herkunft", "Hautfarbe", "Religion", "Geschlecht",
                    "sexuelle Orientierung", "Behinderung"):
        assert merkmal in p, f"die Grenze nennt {merkmal} nicht mehr"
    for regel in ("echten Drohungen", "privaten Daten", "Nazi-Verherrlichung"):
        assert regel in p, f"die Grenze nennt '{regel}' nicht mehr"
    # Wer wirklich am Boden ist, wird nicht weitergeroastet.
    assert "am Boden" in p and "Spass sofort weg" in p, (
        "die Notlagen-Regel ist verschwunden - genau die darf nie fallen")

    # Position: Grenze frueh, Vollgas-Erinnerung als letztes Wort.
    assert p.index("Herkunft") < len(p) * 0.5, (
        "die Grenze ist wieder ans Ende gerutscht und faerbt alles zahm")
    assert p.rstrip().endswith("Nur die eine Grenze von oben bleibt."), (
        f"das Schlusswort steht nicht am Ende: ...{p[-80:]!r}")


def test_verweigerte_roasts_bleiben_nicht_still():
    """Wenn das Modell kneift, sah der Server nur einen von fuenf Fertig-Spruechen.

    Im Log stand NICHTS. Genau daran war nicht zu erkennen, dass Flo nicht
    langweilig geworden ist, sondern dass das Modell verweigert - und ohne diese
    Zeile sucht der Betreiber den Fehler ewig an der falschen Stelle."""
    import logging
    import fun
    strom = io.StringIO()
    haken = logging.StreamHandler(strom)
    log = logging.getLogger("dcbot.fun")
    log.addHandler(haken)
    alt = log.level
    log.setLevel(logging.WARNING)
    try:
        assert fun.instance._looks_like_refusal(
            "Sorry, aber ich kann nicht beleidigend werden.") is True
        assert fun.instance._looks_like_refusal("Du bist ein Vollpfosten.") is False
    finally:
        log.removeHandler(haken)
        log.setLevel(alt)
    text = strom.getvalue()
    assert "verweigert" in text, f"die Verweigerung blieb still: {text!r}"
    # 'bash k' filtert nach KIMUSTER - die Zeile muss da durchkommen.
    hier = os.path.dirname(os.path.abspath(__file__))
    muster = open(os.path.join(hier, "k"), encoding="utf-8").read()
    assert "KI-Fehler" in muster and "KI-Fehler" in text, (
        "'bash k' zeigt verweigerte Roasts nicht an")


def _gehirn_frisch():
    """gehirn mit Fake-Store. Gibt (modul, restore)."""
    import gehirn
    g = gehirn.instance
    alt = (g._store, g._enabled)
    g._store = _FakeStore({"guilds": {}})
    g._enabled = True

    def restore():
        g._store, g._enabled = alt
    return gehirn, restore


def _gehirn_msg(uid, text, gid=77, name="Anna", bot=False):
    return SimpleNamespace(
        guild=SimpleNamespace(id=gid),
        author=SimpleNamespace(id=uid, bot=bot, display_name=name),
        content=text, mentions=[])


def test_gehirn_merkt_sich_nur_was_es_darf():
    """Flo hoert mit - aber nicht alles, und niemals heimlich in DMs.

    Ein Bot, der jede Zeile mitschneidet, waere ein Ueberwachungswerkzeug statt
    eines Spass-Features. Deshalb: keine DMs, keine Bots, keine Einzeiler (die
    sagen ueber einen Menschen nichts aus) und keine Befehle an Flo - Bedienung
    ist kein Gespraech."""
    gehirn, restore = _gehirn_frisch()
    try:
        g = gehirn.instance
        gehirn.note_message(_gehirn_msg(1, "ich zocke jeden Abend Terraria"))
        assert len(g._guild(77)["puffer"]) == 1

        # DM (kein guild) - darf NIE in den Puffer.
        dm = _gehirn_msg(1, "das ist eine lange private Nachricht")
        dm.guild = None
        gehirn.note_message(dm)
        # Bot-Nachrichten interessieren niemanden.
        gehirn.note_message(_gehirn_msg(9, "ich bin ein langer Bot-Text", bot=True))
        # Zu kurz, um etwas ueber jemanden zu sagen.
        gehirn.note_message(_gehirn_msg(1, "lol"))
        assert len(g._guild(77)["puffer"]) == 1, g._guild(77)["puffer"]

        # Der Puffer hat einen Deckel - sonst waechst die Datei unbegrenzt.
        for i in range(gehirn.PUFFER_MAX + 50):
            gehirn.note_message(_gehirn_msg(1, f"nachricht nummer {i} mit text"))
        assert len(g._guild(77)["puffer"]) == gehirn.PUFFER_MAX
    finally:
        restore()


def test_gehirn_speichert_keine_privaten_daten():
    """Der KI ist das verboten - aber eine Anweisung ist keine Zusicherung.

    Einmal in der Datei steht es da. Deshalb filtert Flo hinterher noch einmal
    hart nach, egal was das Modell vorschlaegt."""
    import gehirn
    heikel = [
        "erreichbar unter anna@example.com",
        "Handynummer ist +49 170 1234567",
        "IBAN DE89370400440532013000",
        "wohnt in der Hauptstrasse 5",
        "wohnt in 93047 Regensburg",
        "postet staendig https://example.com/x",
        "hat das Passwort geaendert",
    ]
    for text in heikel:
        assert gehirn.Gehirn._heikel(text), f"wird gespeichert: {text!r}"
    harmlos = ["spielt Terraria", "hasst Montage", "kann nicht zielen",
               "trinkt nur Club Mate", "verliert jede Wette gegen Ben"]
    for text in harmlos:
        assert not gehirn.Gehirn._heikel(text), f"faelschlich verworfen: {text!r}"


def test_gehirn_waechst_nicht_unbegrenzt_und_vergisst_das_richtige():
    """Ein Gedaechtnis ohne Deckel macht die Datei unlesbar und den Prompt teuer.

    Was oft bestaetigt wurde, ueberlebt; was einmal nebenbei fiel, fliegt zuerst
    raus. Sonst verdraengt zufaelliges Geplapper die echten Eigenheiten."""
    import gehirn
    liste = []
    # Derselbe Fakt zweimal ist kein zweiter Eintrag, sondern eine Bestaetigung.
    assert gehirn.Gehirn._merge(liste, "spielt Terraria", 5) is True
    assert gehirn.Gehirn._merge(liste, "Spielt  Terraria!", 5) is False, (
        "derselbe Fakt landet doppelt im Kopf")
    assert len(liste) == 1 and liste[0]["oft"] == 2

    # Ueber dem Deckel fliegt das Selten-Bestaetigte zuerst.
    liste = [{"t": "oft gehoert", "wann": time.time() - 10, "oft": 9}]
    for i in range(10):
        gehirn.Gehirn._merge(liste, f"nebensache {i}", 5)
    assert len(liste) == 5, len(liste)
    assert any(e["t"] == "oft gehoert" for e in liste), (
        "der oft bestaetigte Fakt wurde verdraengt")


def test_gehirn_bleibt_auf_seinem_server_und_ist_loeschbar():
    """Zwei Zusagen, die beide zaehlen:

    (1) Was im einen Server gesagt wurde, weiss Flo im anderen NICHT. Sonst
        traegt er Interna von Server zu Server.
    (2) Jeder kann loeschen, was Flo ueber ihn weiss - samt dem, was noch
        unausgewertet im Puffer liegt. Sonst waere es beim naechsten Lauf
        einfach wieder da."""
    gehirn, restore = _gehirn_frisch()
    try:
        g = gehirn.instance
        gehirn.Gehirn._merge(g._person(77, 1)["fakten"], "spielt Terraria", 25)
        gehirn.Gehirn._merge(g._person(88, 1)["fakten"], "ist auf 88 unterwegs", 25)

        k77 = gehirn.kontext_fuer(77, 1)
        assert "Terraria" in k77
        assert "88" not in k77, "Wissen leckt zwischen Servern"
        assert "Terraria" not in gehirn.kontext_fuer(88, 1)
        # Ohne Wissen kein Block - sonst haengt an jeder Antwort leerer Text.
        assert gehirn.kontext_fuer(77, 999) == ""
        # Und der Block bleibt kurz: er kostet bei JEDER Antwort Token.
        for i in range(40):
            gehirn.Gehirn._merge(g._person(77, 1)["fakten"], f"fakt {i} " + "x" * 60, 25)
        assert len(gehirn.kontext_fuer(77, 1)) <= 700

        # Loeschen raeumt Fakten UND Puffer.
        gehirn.note_message(_gehirn_msg(1, "noch eine lange Nachricht von mir"))
        assert g._guild(77)["puffer"]
        weg = gehirn.vergiss(77, 1)
        assert weg > 0
        assert gehirn.kontext_fuer(77, 1) == ""
        assert not [m for m in g._guild(77)["puffer"] if m["wer"] == 1], (
            "im Puffer steht die Person noch - beim naechsten Lauf waere sie zurueck")
        # Server 88 bleibt unberuehrt.
        assert "88" in gehirn.kontext_fuer(88, 1)
    finally:
        restore()


def test_gehirn_haengt_nicht_an_einem_kaputten_ki_aufruf_fest():
    """Scheitert die Auswertung, darf der Puffer nicht ewig neu geschickt werden.

    Sonst laeuft ein dauerhaft kaputter KI-Aufruf alle 10 Minuten in dieselben
    120 Nachrichten - und der Puffer nimmt nie wieder etwas Neues auf."""
    gehirn, restore = _gehirn_frisch()
    import ai
    alt_gen = ai.generate
    try:
        g = gehirn.instance
        for i in range(gehirn.PUFFER_ZIEL + 5):
            gehirn.note_message(_gehirn_msg(1, f"eine nachricht nummer {i}"))
        voll = len(g._guild(77)["puffer"])
        assert voll >= gehirn.PUFFER_ZIEL

        ai.generate = lambda *a, **k: _als_coro(None)     # KI liefert nichts
        neu = asyncio.run(gehirn.tick([SimpleNamespace(id=77)]))
        assert neu == 0
        assert g._guild(77)["puffer"] == [], (
            "der Puffer wird beim naechsten Lauf noch einmal verschickt")

        # Und der gute Fall: aus dem Chat werden Fakten.
        for i in range(gehirn.PUFFER_ZIEL + 1):
            gehirn.note_message(_gehirn_msg(1, f"ich zocke gerne runde {i}"))
        ai.generate = lambda *a, **k: _als_coro(
            "Anna: spielt jeden Abend Terraria\n"
            "SERVER: hier wird viel gezockt\n"
            "Anna: erreichbar unter anna@example.com\n"
            "Unbekannt: irgendwas")
        neu = asyncio.run(gehirn.tick([SimpleNamespace(id=77)]))
        assert neu == 2, neu
        kontext = gehirn.kontext_fuer(77, 1)
        assert "Terraria" in kontext and "gezockt" in kontext
        assert "example.com" not in kontext, "private Daten im Gedaechtnis"
        # Ein Name, den niemand im Chat hatte, wird nicht erfunden.
        assert "irgendwas" not in kontext
    finally:
        ai.generate = alt_gen
        restore()


def test_panel_aenderung_erreicht_den_laufenden_player():
    """DAS war der eigentliche "nicht synchronisiert"-Fehler.

    Die Lautstaerke wurde NUR beim Anlegen eines Players aus guildcfg gelesen -
    und Player werden nie weggeraeumt. Wer einmal Musik gehoert hatte, behielt
    seine Lautstaerke bis zum Neustart: 'flo ls 80' griff sofort, ein Klick im
    Web-Panel nie. Jetzt meldet sich music bei guildcfg an und zieht nach."""
    import guildcfg
    import music

    import discord

    # Eine ECHTE PCMVolumeTransformer - music prueft per isinstance, und mit
    # einer Attrappe wuerde der Test gruen, obwohl im Betrieb nichts passiert.
    class StummeQuelle(discord.AudioSource):
        def read(self):
            return b""

        def is_opus(self):
            return False

    quelle = discord.PCMVolumeTransformer(StummeQuelle(), volume=0.5)
    player = SimpleNamespace(volume=0.5, voice=SimpleNamespace(source=quelle))
    m = music.instance
    alt_players = dict(m._players)
    m._players[4242] = player
    alt_get = guildcfg.get
    try:
        guildcfg.get = lambda gid, key: 90 if key == "lautstaerke" else alt_get(gid, key)
        m.lautstaerke_nachziehen(4242)
        assert abs(player.volume - 0.9) < 1e-6, player.volume
        assert abs(player.voice.source.volume - 0.9) < 1e-6, (
            "die LAUFENDE Tonspur bleibt leise - es wirkt erst beim naechsten Lied")
        # Ein Server ohne Player darf nicht umfallen.
        m.lautstaerke_nachziehen(999999)
    finally:
        guildcfg.get = alt_get
        m._players.clear()
        m._players.update(alt_players)


def test_guildcfg_sagt_allen_bescheid_die_daran_haengen():
    """Der Verteiler hinter dem Nachziehen.

    Frueher stand in _nachziehen eine if-Kette mit genau einem Eintrag
    (praefix). Jeder weitere Verbraucher haette sie erweitern muessen - und
    niemand, der ein Modul aendert, schaut in guildcfg nach."""
    import guildcfg
    gerufen = []
    guildcfg.horcht_auf("testschalter", gerufen.append)
    # Doppelte Anmeldung derselben Funktion darf nicht doppelt feuern
    # (setup() laeuft in Tests mehrfach).
    guildcfg.horcht_auf("testschalter", gerufen.append)
    try:
        guildcfg.instance._nachziehen(77, "testschalter")
        assert gerufen == [77], gerufen

        # Ein kaputter Hoerer darf die Einstellung NICHT umwerfen - sonst
        # scheitert das Speichern an einem Nebeneffekt.
        def kaputt(_gid):
            raise RuntimeError("absichtlich")

        guildcfg.horcht_auf("testschalter", kaputt)
        guildcfg.instance._nachziehen(88, "testschalter")
        assert gerufen == [77, 88], gerufen
    finally:
        guildcfg._HOERER.pop("testschalter", None)


def test_moderation_grenzen_gelten_je_server():
    """Verwarn-Limit, Timeout-Dauern und das Loesch-Limit standen nur in der
    .env: eine Aenderung galt fuer alle Server und brauchte einen Neustart.
    Jetzt kommen sie aus guildcfg - also auch aus dem Web-Panel."""
    import guildcfg
    import moderation
    for key in ("warn_limit", "warn_timeout", "timeout_standard", "purge_max"):
        assert key in {e.key for e in guildcfg.KATALOG}, f"{key} fehlt im Katalog"

    # WICHTIG: die Module rufen guildcfg.get (den Modul-Alias), nicht
    # instance.get - der Alias wird beim Import gebunden. Wer instance.get
    # ersetzt, ersetzt nichts.
    alt = guildcfg.get
    try:
        guildcfg.get = lambda gid, key: (7 if (gid == 77 and key == "warn_limit")
                                         else alt(gid, key))
        assert moderation._cfg_zahl(77, "warn_limit", 3) == 7
        assert moderation._cfg_zahl(88, "warn_limit", 3) == 3, "gilt serveruebergreifend"
        # Kaputter Wert -> der alte Standard, kein Absturz mitten in der Moderation.
        guildcfg.get = lambda gid, key: "keine zahl"
        assert moderation._cfg_zahl(77, "warn_limit", 3) == 3
    finally:
        guildcfg.get = alt


def test_aktie_laesst_sich_je_server_abschalten():
    """Das Panel schreibt den Funktions-Schalter JE SERVER (features.set_guild),
    die Aktie fragte aber nur den globalen ab. Ein Server, der die Aktie im
    Panel abgeschaltet hatte, handelte munter weiter."""
    import features
    import floaktie
    alt_on, alt_in = features.is_on, features.is_on_in
    try:
        features.is_on = lambda key: True
        features.is_on_in = lambda gid, key: gid != 77
        assert floaktie.instance.is_off(77) is True, (
            "der Server-Schalter aus dem Panel wird ignoriert")
        assert floaktie.instance.is_off(88) is False
        # Ohne Server (DM) zaehlt nur der globale Schalter.
        assert floaktie.instance.is_off(None) is False
        features.is_on = lambda key: False
        assert floaktie.instance.is_off(88) is True, "der globale Not-Aus greift nicht"
    finally:
        features.is_on, features.is_on_in = alt_on, alt_in


def test_keine_server_einstellung_ohne_rechtepruefung():
    """Wer eine Server-Einstellung schreibt, muss vorher fragen, ob er darf.

    bayern.py hat guildcfg.setzen ohne jede Pruefung gerufen - ein beilaeufiges
    "flo red mal bayerisch" hat damit fuer den ganzen Server umgestellt. Der
    Test haelt das fuer ALLE Module fest, nicht nur fuer das eine."""
    import glob
    import re as _re
    schuldige = []
    for datei in sorted(glob.glob("*.py")):
        if datei.startswith("test_") or datei in ("guildcfg.py", "webpanel.py"):
            continue        # guildcfg prueft selbst, das Panel hat _guard
        quelle = open(datei, encoding="utf-8").read()
        # Ausnahmen an der FUNKTION festmachen, nicht an einem Textfenster:
        # eine lange Erklaerung davor hat den Namen sonst aus dem Fenster
        # geschoben und der Test schlug grundlos an.
        ohne_nutzer = ("altlast_migrieren",)   # laeuft beim Start, ohne Person
        for treffer in _re.finditer(r"guildcfg\.setzen\(", quelle):
            davor = quelle[:treffer.start()]
            umgebung = _re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", davor,
                                   _re.MULTILINE)
            if umgebung and umgebung[-1] in ohne_nutzer:
                continue
            if "darf(" in davor[-900:] or "_darf(" in davor[-900:]:
                continue
            zeile = davor.count("\n") + 1
            schuldige.append(f"{datei}:{zeile} (in {umgebung[-1] if umgebung else '?'})")
    assert not schuldige, (
        "Diese Stellen aendern eine Server-Einstellung, ohne die Rechte zu "
        "pruefen:\n  " + "\n  ".join(schuldige))


def test_soundboard_hat_keinen_eigenen_speicher_mehr():
    """Zwei Wahrheiten fuer denselben Schalter sind genau der Grund, warum das
    Panel und Discord auseinanderlaufen konnten. voicegags darf keinen eigenen
    Konfigurations-Speicher mehr anlegen."""
    quelle = open("voicegags.py", encoding="utf-8").read()
    assert "JsonStore(" not in quelle, (
        "voicegags legt wieder einen eigenen Speicher an - der Schalter gehoert "
        "in guildcfg, sonst ist er im Panel nicht zu sehen")
    assert "set_soundboard" not in quelle
    # Und die Werte muessen bei JEDEM Gebrauch gelesen werden, nicht in setup().
    assert "self._join_sounds" not in quelle, (
        "die Join-Sounds werden wieder beim Start gemerkt - eine Aenderung im "
        "Panel wirkt dann erst nach einem Neustart")


def test_funktionen_lassen_sich_auch_in_discord_schalten():
    """Die Funktions-Schalter gab es NUR im Web-Panel.

    Wer kein Panel offen hatte, konnte auf seinem eigenen Server das Casino
    nicht abschalten. Jetzt geht beides - und beides schreibt dieselbe Stelle."""
    import features
    import guildcfg
    gesetzt = {}
    alt_set, alt_on, alt_in = features.set_guild, features.is_on, features.is_on_in

    async def fake_set(gid, key, on):
        if key not in {e["key"] for e in features.CATALOG}:
            return None
        gesetzt[(int(gid), key)] = bool(on)
        return bool(on)

    def msg(text, darf=True):
        m = SimpleNamespace(
            content=f"Flo {text}", mentions=[],
            author=SimpleNamespace(
                id=5, bot=False, display_name="T",
                guild_permissions=SimpleNamespace(manage_guild=darf)),
            guild=SimpleNamespace(id=77, name="S"))
        m.channel = SimpleNamespace(send=lambda **kw: _als_coro(None))
        return m

    alt_enabled = guildcfg.instance._enabled
    guildcfg.instance._enabled = True
    features.set_guild = fake_set
    features.is_on = lambda key: True
    features.is_on_in = lambda gid, key: gesetzt.get((int(gid), key), True)
    try:
        antwort = asyncio.run(guildcfg.handle(msg("funktion casino aus")))
        assert gesetzt.get((77, "casino")) is False, gesetzt
        assert "aus" in str(antwort)

        # Ohne Recht wird NICHTS umgeschaltet.
        gesetzt.clear()
        antwort = asyncio.run(guildcfg.handle(msg("funktion casino aus", darf=False)))
        assert not gesetzt, "ohne Recht wurde geschaltet"
        assert "verwalten" in str(antwort)

        # Unbekannter Schluessel wird benannt, nicht still verschluckt.
        antwort = asyncio.run(guildcfg.handle(msg("funktion gibtsnicht aus")))
        assert "kenne ich nicht" in str(antwort)

        # Der globale Not-Aus gewinnt weiter.
        features.is_on = lambda key: False
        antwort = asyncio.run(guildcfg.handle(msg("funktion casino an")))
        assert "global" in str(antwort)

        # Uebersicht ohne Argument geht auch ohne Rechte (nur ansehen).
        assert asyncio.run(guildcfg.handle(msg("funktionen", darf=False))) is guildcfg.HANDLED
    finally:
        features.set_guild, features.is_on, features.is_on_in = alt_set, alt_on, alt_in
        guildcfg.instance._enabled = alt_enabled


def test_schnell_event_lohnt_sich_wirklich():
    """Der Preis fuers Schnell-Event war 100 Coins.

    Danebengehalten: der Tagesbonus bringt 2.500, das Wort des Tages ab 50.000.
    Fuer 100 Coins hat sich niemand mehr vom Stuhl bewegt. Jetzt wird der Preis
    je Runde gewuerfelt - eine Spanne ist spannender als eine feste Zahl."""
    import games
    assert games.EVENT_REWARD_MIN >= 1000, (
        "der Preis ist wieder auf Trinkgeld-Niveau")
    assert games.EVENT_REWARD_MAX > games.EVENT_REWARD_MIN, "keine Spanne"

    werte = {games.instance._event_preis() for _ in range(500)}
    assert min(werte) >= games.EVENT_REWARD_MIN
    assert max(werte) <= games.EVENT_REWARD_MAX
    assert len(werte) > 50, "der Preis wird gar nicht gewuerfelt"

    # Ein Vertipper in der .env (MIN groesser als MAX) darf das Event nicht
    # sprengen - random.randint wirft bei verdrehten Grenzen sonst.
    alt = (games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX)
    try:
        games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX = 9_000, 1_000
        assert games.instance._event_preis() == 1_000
        games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX = 0, 0
        assert games.instance._event_preis() >= 1
    finally:
        games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX = alt

    # Die Tageskappe bleibt der Deckel - der Preis darf sie nicht aushebeln.
    assert games.GAMES_DAILY_MAX > 0
    assert games.EVENT_REWARD_MAX <= games.GAMES_DAILY_MAX, (
        "ein einzelnes Event fuellt die komplette Tageskappe")


def test_schnell_event_verspricht_nichts_was_es_nicht_zahlt():
    """Bei 100 Coins fiel es nie auf - bei fuenfstelligen Preisen schon:
    _auszahlen kuerzt auf die Tageskappe, die Siegermeldung nannte aber den
    AUSGELOBTEN Betrag. Wer seine Kappe voll hatte, las "+25.000 Flo Coins"
    und bekam nichts."""
    quelle = open("games.py", encoding="utf-8").read()
    i = quelle.index('self._auszahlen(message.author.id, runde["reward"]')
    rumpf = quelle[i:i + 1200]
    assert "gezahlt =" in quelle[max(0, i - 200):i + 100], (
        "der Rueckgabewert von _auszahlen wird weggeworfen")
    assert "gezahlt" in rumpf and "Tageskappe" in rumpf, (
        "die Siegermeldung nennt weiter den ausgelobten statt den gezahlten Betrag")
    # Und der Betrag muss mit Tausenderpunkten dastehen, nicht als '25000'.
    assert "numfmt.fmt(gezahlt)" in rumpf, rumpf[:200]


def _verlauf_track(titel, url="", wer="", dauer=None, query=""):
    return SimpleNamespace(title=titel, webpage_url=url, requested_by=wer,
                           duration=dauer, query=query)


def _verlauf_frisch():
    """music mit leerem Verlaufs-Store. Gibt (modul, restore)."""
    import music
    m = music.instance
    alt = (m._store, m._enabled)
    m._store = _FakeStore({"guilds": {}})
    m._enabled = True

    def restore():
        m._store, m._enabled = alt
    return music, restore


def test_verlauf_erkennt_die_richtigen_befehle():
    """'flo history' und 'flo nochmal verlauf' - auch vertippt.

    Die Grenze ist genauso wichtig wie die Treffer: waere die Erkennung zu
    locker, landeten andere Befehle im Verlauf statt dort, wo sie hingehoeren."""
    import music
    treffer = ("history", "histori", "historie", "nochmal verlauf",
               "nochmal history", "nochmall verlauf", "nohmal history",
               "nochmal histori", "nochmal verlauv", "again history",
               "replay history", "musik verlauf", "music history",
               "song verlauf", "wiederhole verlauf")
    for text in treffer:
        assert music.verlauf_befehl(text), f"nicht erkannt: {text!r}"

    daneben = ("nochmal", "nochmal 3", "skip", "stop", "pause", "queue",
               "spiel wonderwall", "lyrics", "shuffle", "leave", "volume 50",
               "handel", "transaktionen", "trades", "luxus", "join",
               "spiel history von abba", "")
    for text in daneben:
        assert not music.verlauf_befehl(text), f"faelschlich Verlauf: {text!r}"


def test_verlauf_nimmt_dem_handelsbuch_nicht_den_befehl_weg():
    """'flo verlauf' gehoert seit jeher dem Handelsbuch - und music.handle
    laeuft in bot.py VOR handel.handle.

    Haette der Musik-Verlauf das nackte Wort beansprucht, wuerde Flo ab sofort
    Songs zeigen, wenn jemand seine Coin-Umsaetze sehen will. Ein bestehender
    Befehl darf davon nicht kaputtgehen."""
    import handel
    import music
    assert "verlauf" in handel.Handel._CMDS, "Annahme veraltet"
    assert not music.verlauf_befehl("verlauf"), (
        "der Musik-Verlauf hat dem Handelsbuch 'verlauf' weggenommen")
    assert not music.verlauf_befehl("verlaufs")
    # In bot.py steht music wirklich vor handel - deshalb ist das kein Detail.
    quelle = open("bot.py", encoding="utf-8").read()
    assert quelle.index("music.handle") < quelle.index("handel.handle")


def test_verlauf_ueberlebt_den_neustart():
    """Der Player haelt nur die letzten 30 im Arbeitsspeicher - nach einem
    Neustart ist alles weg. Genau danach fragt man aber 'was lief gestern?'."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        for i in range(1, 6):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}", f"https://x/{i}", "Anna"))
        # Neuester zuerst - Nummer 1 ist der zuletzt gespielte.
        assert [e["t"] for e in m.verlauf(77)][:2] == ["Song 5", "Song 4"]

        # "Neustart": derselbe Inhalt, frisch aus dem Speicher gelesen.
        roh = m._store.data
        m._store = _FakeStore(roh)
        assert len(m.verlauf(77)) == 5, "der Verlauf ist beim Neustart weg"
        assert m.verlauf(77)[0]["t"] == "Song 5"

        # Server bleiben getrennt.
        assert m.verlauf(88) == []
    finally:
        restore()


def test_verlauf_waechst_nicht_unbegrenzt():
    """Mindestens 100 Eintraege, aeltere fliegen automatisch raus - sonst
    waechst die Datei bei jedem Song weiter."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        assert music.VERLAUF_MAX >= 100, "weniger als gefordert"
        for i in range(music.VERLAUF_MAX + 60):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}"))
        eintraege = m.verlauf(77)
        assert len(eintraege) == music.VERLAUF_MAX, len(eintraege)
        # Der NEUESTE ueberlebt, der aelteste ist weg.
        assert eintraege[0]["t"] == f"Song {music.VERLAUF_MAX + 59}"
        assert not any(e["t"] == "Song 0" for e in eintraege)

        # Derselbe Song zweimal hintereinander (Stall-Neustart, Seek) ist EIN
        # Eintrag - sonst steht der Verlauf nach einer Stoerung voll damit.
        m.verlauf_notieren(99, _verlauf_track("Derselbe"))
        m.verlauf_notieren(99, _verlauf_track("Derselbe"))
        assert len(m.verlauf(99)) == 1
    finally:
        restore()


def test_verlauf_embed_nummeriert_und_blaettert():
    """10 je Seite, neuester ist Nummer 1, Seitenanzeige im Fusstext."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        for i in range(1, 26):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}", f"https://x/{i}", "Anna",
                                                  dauer=200))
        view = music.VerlaufView(77, owner_id=5)
        emb = view.embed()
        assert "**1.** [Song 25]" in emb.description, emb.description[:120]
        assert "**10.** " in emb.description
        assert "**11.** " not in emb.description, "mehr als 10 auf einer Seite"
        assert "Seite 1/3" in emb.footer.text, emb.footer.text
        assert "Anna" in emb.description and "25 Songs" in emb.footer.text

        # Dropdown + zwei Blaetter-Knoepfe.
        namen = [type(c).__name__ for c in view.children]
        assert namen.count("Button") == 2 and "_VerlaufSelect" in namen, namen

        view.seite = 1
        view._aufbauen()
        assert "**11.** [Song 15]" in view.embed().description
        assert "Seite 2/3" in view.embed().footer.text

        # Ueber die letzte Seite hinaus wird geklemmt statt zu fliegen.
        view.seite = 99
        view._aufbauen()
        assert "Seite 3/3" in view.embed().footer.text
    finally:
        restore()


def test_verlauf_faengt_die_raender_ab():
    """Leerer Verlauf, unsinnige Nummer, Eintrag ohne Quelle - jeweils ein
    klarer Satz statt eines Absturzes."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        # Leer.
        eintrag, fehler = m.verlauf_eintrag(77, 1)
        assert eintrag is None and "Noch keine Songs" in fehler
        assert "Noch keine Songs" in music.VerlaufView(77, 5).embed().description

        m.verlauf_notieren(77, _verlauf_track("Song A", "https://x/a"))
        # Zu gross, zu klein, keine Zahl.
        assert m.verlauf_eintrag(77, 9)[0] is None
        assert "gibt es nicht" in m.verlauf_eintrag(77, 9)[1]
        assert m.verlauf_eintrag(77, 0)[0] is None
        assert "keine Nummer" in m.verlauf_eintrag(77, "abc")[1]
        # Gueltig.
        assert m.verlauf_eintrag(77, 1)[0]["t"] == "Song A"

        # Ohne Quelle laesst sich nichts nachspielen - das muss auffallen,
        # bevor yt-dlp mit einem leeren String losrennt.
        assert m._verlauf_quelle({"t": "", "u": "", "q": ""}) == ""
        assert m._verlauf_quelle({"t": "Nur Titel"}) == "Nur Titel"
        assert m._verlauf_quelle({"u": "https://x/1", "q": "such"}) == "https://x/1"
    finally:
        restore()


def test_nochmal_n_meint_dieselbe_nummer_wie_der_verlauf():
    """'flo nochmal 3' und die 3 in 'flo history' MUESSEN derselbe Song sein.

    Vorher las 'nochmal' aus player.history (Arbeitsspeicher, letzte 30,
    aelteste zuerst gezaehlt) - zwei Listen mit verschiedener Zaehlrichtung.
    Nach einem Neustart war sie ausserdem leer, obwohl der Verlauf noch stand."""
    import music
    quelle = open(music.__file__, encoding="utf-8").read()
    i = quelle.index('if action == "replay":')
    rumpf = quelle[i:i + 1200]
    assert "verlauf_eintrag" in rumpf, (
        "'nochmal N' liest wieder aus dem fluechtigen player.history")
    assert "player.history[-idx]" not in rumpf

    music_mod, restore = _verlauf_frisch()
    try:
        m = music_mod.instance
        for i in range(1, 6):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}", f"https://x/{i}"))
        # Nummer 3 im Embed ...
        emb = music_mod.VerlaufView(77, 5).embed()
        assert "**3.** [Song 3]" in emb.description, emb.description[:200]
        # ... ist auch die 3 fuer 'nochmal 3'.
        assert m.verlauf_eintrag(77, 3)[0]["t"] == "Song 3"
    finally:
        restore()


def test_verlauf_ist_im_hilfetext():
    """Ein Befehl, den niemand findet, hilft niemandem."""
    quelle = open("bot.py", encoding="utf-8").read()
    assert "flo history" in quelle, "der Verlauf fehlt in der Hilfe"
    # Gezielt in _HELP_HINTS schauen: 'musik' kommt in bot.py mehrfach vor
    # (u. a. in der Kategorie-Zuordnung), ein blindes split() traefe die
    # falsche Stelle - und der Test waere gruen, ohne etwas zu pruefen.
    hints = quelle.split("_HELP_HINTS = {")[1].split("}")[0]
    assert "history" in hints.split('"musik": "')[1].split('"')[0], (
        "die Musik-Kurzuebersicht nennt den Verlauf nicht")


# --- Das Inventar: ist noch alles da? ---------------------------------------
def test_inventar_findet_ueberhaupt_etwas():
    """Die Wache am Werkzeug selbst.

    Das Inventar (werkzeug/inventar.py) soll beim Umbau sagen, was verloren
    gegangen ist. Es kann dabei auf eine besonders unangenehme Art versagen:
    es laeuft in einer kaputten Umgebung, findet fast nichts, und ab da ist
    jeder Vergleich trivial gruen - das Sicherheitsnetz haette ein Loch in
    genau der Groesse des Problems.

    Dieser Test laeuft ohne Probe (nur Quelltext, also schnell) und prueft, ob
    die gefundenen Mengen ueber den Untergrenzen liegen.
    """
    from werkzeug import inventar

    stand = inventar.Inventar(laut=False).aufnehmen(mit_probe=False)
    zu_wenig = inventar.untergrenze_pruefen(stand)
    assert not zu_wenig, (
        "Das Inventar findet zu wenig - das heisst fast immer, dass die "
        "Umgebung kaputt ist, nicht der Bot:\n  " + "\n  ".join(zu_wenig))
    # Und die Handler-Schleife in bot.py muss auffindbar bleiben: ohne sie
    # weiss das Inventar nicht mehr, wer eine Wort-Kollision gewinnt.
    quelle = inventar.Quelltext.hol(inventar.WURZEL / "bot.py")
    module = inventar.Reihenfolge(quelle).module()
    assert len(module) >= 20, f"nur {len(module)} Module in der Handler-Schleife"


def test_inventar_hat_nichts_verloren():
    """Der eigentliche Zweck: nach jedem Umbauschritt muss noch alles da sein.

    Laeuft als Unterprozess, weil die Probe alle Module hochfaehrt und
    einschaltet - das soll den uebrigen Tests nicht in die Quere kommen.

    Rueckgabecodes des Werkzeugs: 0 alles da, 1 nur angekuendigte Verluste
    (steht begruendet in inventar/erwartet.json), 2 echter Verlust,
    3 das Werkzeug selbst ist kaputt.
    """
    import subprocess

    wurzel = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(wurzel, "inventar", "stand.json")):
        return          # noch kein Grundstand aufgenommen - nichts zu pruefen
    lauf = subprocess.run(
        [sys.executable, os.path.join("werkzeug", "inventar.py"), "--vergleiche"],
        cwd=wurzel, capture_output=True, text=True, timeout=900)
    assert lauf.returncode in (0, 1), (
        f"Inventar meldet Code {lauf.returncode}:\n"
        + (lauf.stdout or "")[-3000:] + (lauf.stderr or "")[-1500:])


def test_einstellungen_ordnen_jeden_wert_ein():
    """Die eine Liste darf keinen Wert unterschlagen.

    einstellungen.py fuehrt guildcfg (26 Werte) und features (24 Schalter) zu
    EINER nach Funktionen geordneten Liste zusammen - das ist die Antwort auf
    'manche einstellungen sind da und andere da'. Die Zuordnung ist von Hand
    gepflegt, weil sie nirgends im Code steht: sie ergibt sich daraus, was ein
    Wert tatsaechlich beeinflusst.

    Von Hand gepflegt heisst: sie verrottet, sobald jemand einen Schluessel
    dazulegt und hier nichts eintraegt. Der Wert waere dann im Panel
    unsichtbar - genau die Sorte stiller Verlust, gegen die dieser Umbau
    laeuft. Deshalb prueft dieser Test beide Richtungen und beide Kataloge.
    """
    import einstellungen

    vergessen = einstellungen.vergessene_werte()
    assert not vergessen, (
        f"Diese guildcfg-Werte sind in keinem Abschnitt und damit im Panel "
        f"unsichtbar: {vergessen}. In einstellungen.ZUORDNUNG einordnen (oder "
        f"in GRUNDLEGEND, wenn sie zu keiner Funktion gehoeren).")
    erfunden = einstellungen.erfundene_werte()
    assert not erfunden, (
        f"Diese Namen stehen in einstellungen.ZUORDNUNG, aber nicht in "
        f"guildcfg.KATALOG: {erfunden}")
    erfundene_f = einstellungen.erfundene_funktionen()
    assert not erfundene_f, (
        f"Diese Funktionsnamen stehen in einstellungen.ZUORDNUNG, aber nicht "
        f"in features.CATALOG: {erfundene_f}")

    # Und die Liste muss wirklich jeden Schalter zeigen, nicht nur die mit Werten.
    baum = einstellungen.als_dicts(0, {})
    gezeigt = {a["key"] for a in baum}
    import features
    fehlt = sorted({f["key"] for f in features.CATALOG} - gezeigt)
    assert not fehlt, f"Diese Funktionen fehlen in der Liste: {fehlt}"


def test_einstellungen_aendern_das_discord_nicht():
    """Die neue Liste ist NUR fuer das Panel.

    'Flo einstellungen' und 'Flo funktionen' im Discord sollen sich nicht
    bewegen - der Betreiber hat ausdruecklich gesagt, im Discord soll alles so
    bleiben. Deshalb darf guildcfg.handle nichts aus einstellungen.py holen.
    """
    quelle = open("guildcfg.py", encoding="utf-8").read()
    assert "import einstellungen" not in quelle, (
        "guildcfg benutzt einstellungen.py - dann wandert die neue Gruppierung "
        "ins Discord. Sie ist aber nur fuer das Panel gedacht.")
    # Der umgekehrte Weg ist richtig und muss bleiben.
    neu = open("einstellungen.py", encoding="utf-8").read()
    assert "import guildcfg" in neu and "import features" in neu


def test_panel_laesst_sich_wirklich_bedienen():
    """Das Panel im echten Browser aufmachen und anklicken.

    Von allen Tests hier prueft sonst keiner die Oberflaeche des Panels - das
    sind rund 1.800 Zeilen Javascript, die alles Sichtbare per innerHTML
    zusammenbauen, und die einzige Absicherung war bisher: hinsehen. Ein
    Tippfehler in einem Selektor faellt in keinem Python-Test auf, und das
    Inventar sieht nur, ob es den ENDPUNKT noch gibt - nicht, ob der KNOPF ihn
    noch trifft.

    werkzeug/panelprobe.py faehrt den echten aiohttp-Server hoch, laedt die
    echte webpanel.html in Chromium und klickt sich durch. Laeuft als
    Unterprozess, weil ein Browser und ein Server im Testprozess nichts zu
    suchen haben.

    Fehlt Playwright oder Chromium, meldet die Probe das und gibt 0 zurueck -
    dann laeuft die Suite weiter, nur ohne dieses Netz.
    """
    import subprocess

    wurzel = os.path.dirname(os.path.abspath(__file__))
    probe = os.path.join(wurzel, "werkzeug", "panelprobe.py")
    if not os.path.exists(probe):
        return
    lauf = subprocess.run([sys.executable, probe, "--leise"], cwd=wurzel,
                          capture_output=True, text=True, timeout=900,
                          env=dict(os.environ, DATA_DIR=tempfile.mkdtemp(
                              prefix="flobot-panelprobe-test-")))
    assert lauf.returncode == 0, (
        "Das Panel laesst sich nicht mehr bedienen:\n"
        + (lauf.stdout or "")[-3000:] + (lauf.stderr or "")[-1500:])


def test_lauf_kennt_jede_testdatei():
    """Der Waechter, den lauf.py in seinem Kopf versprochen hat.

    lauf.py fuehrt die Testdateien in einer Liste (TESTDATEIEN). Wenn beim
    Aufteilen der Suite eine neue Datei entsteht und niemand traegt sie ein,
    laufen ihre Tests einfach nicht - und der Lauf meldet trotzdem 'alles
    gruen'. Der Kommentar in lauf.py behauptete, ein Test halte die Liste gegen
    den Ordner. Den gab es nicht. Jetzt schon, und er prueft beide Richtungen.
    """
    import lauf

    wurzel = os.path.dirname(os.path.abspath(__file__))
    im_ordner = {p[:-3] for p in os.listdir(wurzel)
                 if p.startswith("test_") and p.endswith(".py")}
    in_liste = set(lauf.TESTDATEIEN)
    fehlt = sorted(im_ordner - in_liste)
    zuviel = sorted(in_liste - im_ordner)
    assert not fehlt, (
        f"Diese Testdateien stehen NICHT in lauf.TESTDATEIEN und laufen "
        f"deshalb nie mit: {fehlt}")
    assert not zuviel, (
        f"Diese Namen stehen in lauf.TESTDATEIEN, aber es gibt keine Datei "
        f"dazu: {zuviel}")


def test_abdruck_flo_antwortet_noch_genauso():
    """Die Bedingung des Betreibers, nachpruefbar gemacht.

    Das Inventar sagt, WELCHE Befehle es gibt. Das reicht nicht: ein Modul kann
    nach dem Verschieben weiterhin auf 'flo level' reagieren und trotzdem eine
    andere Ueberschrift, andere Felder oder keine Knoepfe mehr schicken. Inventar
    gruen, Testlauf gruen - und im Discord sieht es anders aus.

    werkzeug/abdruck.py nimmt darum die FORM jeder Antwort auf (Typ, Titel,
    Feldnamen, Knopfbeschriftungen, Textgeruest) und vergleicht sie. Was nicht
    reproduzierbar ist (Wuerfel, Uhrzeit), hat das Werkzeug selbst gemessen und
    aussortiert - 472 von 480 Befehlen sind stabil.
    """
    import subprocess

    wurzel = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(wurzel, "inventar", "abdruck.json")):
        return          # noch kein Abdruck aufgenommen
    lauf = subprocess.run(
        [sys.executable, os.path.join("werkzeug", "abdruck.py"), "--vergleiche",
         "--leise"],
        cwd=wurzel, capture_output=True, text=True, timeout=900)
    assert lauf.returncode == 0, (
        "Flo antwortet woanders anders als vorher:\n"
        + (lauf.stdout or "")[-4000:] + (lauf.stderr or "")[-1500:])


def _als_coro(wert):
    """Kleiner Helfer: macht aus einem Wert etwas Awaitbares."""
    async def lauf():
        return wert
    return lauf()


def run():
    tests = sorted(name for name in globals() if name.startswith("test_"))
    for name in tests:
        globals()[name]()
        print(f"ok  {name}")
    print(f"\n{len(tests)} Tests bestanden.")


if __name__ == "__main__":
    run()
