"""Pur-logische Tests fuer Casino-, Spiel-, Wort-Zaehler- und Admin-Logik.

Laufen OHNE Discord-Verbindung und ohne Zusatzpakete (gleicher Runner wie
test_logic.py):  python test_games_logic.py
"""

import asyncio
import os
import random
import time
from types import SimpleNamespace

import admin
import casino
import cmdnorm
import economy
import luxus
import render
import words


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


# --- Keno-Tabelle -----------------------------------------------------------------
def test_keno_tabelle():
    assert casino._KENO_TABLE[(1, 1)] == 3
    assert casino._KENO_TABLE[(8, 8)] == 1000
    assert (3, 1) not in casino._KENO_TABLE   # zu wenig Treffer -> nichts


# --- Wort-Zaehler ------------------------------------------------------------------
def test_words_tokenizer():
    toks = words._tokenize(
        "Hallo WELT! https://beispiel.de/pfad <@123> <#456> <a:emo:789> "
        "Pizza-Party äöüß 42 a zu")
    assert toks == ["hallo", "welt", "pizza", "party", "äöüß", "zu"]
    assert words._tokenize("") == []
    assert words._tokenize("1234 !!! ...") == []


def test_words_zaehlen():
    # Fake-Store: reine dict-Logik testen, ohne Datei (Zustand lebt in der Instanz).
    words.instance._store = type("S", (), {"data": {"words": {}, "total": 0, "msgs": 0}})()
    n = words._count_text("pizza pizza salat", "111")
    assert n == 3
    n = words._count_text("PIZZA!", "222")
    assert n == 1
    daten = words.instance._store.data
    assert daten["words"]["pizza"]["n"] == 3
    assert daten["words"]["pizza"]["u"] == {"111": 2, "222": 1}
    assert daten["words"]["salat"]["n"] == 1
    assert daten["total"] == 4 and daten["msgs"] == 2
    words.instance._store = None


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
def test_stocks_helpers():
    import stocks
    a, p, plus = stocks._format_change(110, 100)
    assert round(a) == 10 and round(p) == 10 and plus is True
    a, p, plus = stocks._format_change(90, 100)
    assert plus is False and round(p) == -10
    # None/Muell robust -> (None, None, True), kein Crash.
    assert stocks._format_change(None, 100) == (None, None, True)
    assert stocks._format_change("x", "y")[0] is None
    # Ticker-Erkennung.
    assert stocks._looks_like_ticker("AAPL")
    assert not stocks._looks_like_ticker("Apple Inc")


# --- Terraria-Wiki -------------------------------------------------------------
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


def test_admin_soundboard_toggle():
    import voicegags
    admin.setup()

    class FakeStore:
        def __init__(self):
            self.data = {"soundboard": True}
            self.saved = 0

        async def save(self):
            self.saved += 1

    fake = FakeStore()
    alt_store, alt_enabled = voicegags.instance._store, voicegags.instance._enabled
    voicegags.instance._store, voicegags.instance._enabled = fake, True
    try:
        assert voicegags.soundboard_enabled()
        # Owner schaltet aus -> Embed + persistiert + Schalter greift.
        antwort = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "soundboard aus")))
        assert antwort is not None and not isinstance(antwort, str)
        assert not voicegags.soundboard_enabled() and fake.saved == 1
        # Wieder an.
        asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "soundboard an")))
        assert voicegags.soundboard_enabled() and fake.saved == 2
        # 'soundboard' OHNE an/aus faellt durch (None) - voicegags zeigt das Board.
        assert asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "soundboard"))) is None
        # Fremde koennen nicht schalten.
        assert asyncio.run(admin.handle(_fake_msg(999, "soundboard aus"))) is None
    finally:
        voicegags.instance._store, voicegags.instance._enabled = alt_store, alt_enabled


def test_cmdnorm_admin_sicherheit():
    # Alltagswoerter, die 1 Tippfehler von Admin-Befehlen entfernt sind,
    # duerfen NICHT gekapert werden.
    for satz in ("nimmt das ernst", "profi tipp", "ansagen bitte"):
        assert cmdnorm.normalize(satz) is None, satz
    # Echte Vertipper werden weiterhin korrigiert.
    assert cmdnorm.normalize("admiin") == "admin"


# --- Luxus-Shop ------------------------------------------------------------------
def test_luxus_katalog():
    preise = [i["preis"] for i in luxus.ITEMS]
    assert preise == sorted(preise), "Katalog muss nach Preis aufsteigen"
    assert preise[0] == 15_000                      # Einstieg erreichbar
    assert preise[-1] == 1_000_000_000              # das 1-Mrd-Endziel
    assert len({i["key"] for i in luxus.ITEMS}) == len(luxus.ITEMS)
    assert len({i["n"] for i in luxus.ITEMS}) == len(luxus.ITEMS)
    assert luxus.THRONE_FACTOR > 1.0                # Thron wird immer teurer


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
    """Minimaler JsonStore-Ersatz fuer die Tests (kein Datei-IO)."""

    def __init__(self, data):
        self.data = data

    async def save(self):
        pass


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

        # _roll_stock ist KRASSER als der Shop: nur mythisch/legendaer/exklusiv,
        # immer mind. ein Highlight; Tausch-Belohnungen sind legendaer/exklusiv.
        for _ in range(25):
            m._roll_stock()
            stock = m._state()["stock"]
            assert stock and all(e["rarity"] in ("mythisch", "legendary", "exklusiv")
                                 for e in stock)
            assert any(e["rarity"] in ("legendary", "exklusiv") for e in stock)
            for t in m._state()["trades"]:
                assert t["reward_rarity"] in ("legendary", "exklusiv")
    finally:
        m._store, m._enabled = alt
        restore_eco()


# --- Titel: die neue EXKLUSIV-Stufe (nur beim Haendler) ------------------------
def test_titles_exklusiv_tier():
    """'exklusiv' ist die hoechste Stufe und taucht NIE im normalen Shop/Pool auf."""
    import titles
    assert "exklusiv" in titles.RARITY
    assert titles.RANK["exklusiv"] == max(titles.RANK.values())   # hoechster Rang
    assert titles.RARITY["exklusiv"]["shop_weight"] == 0
    assert titles.counts().get("exklusiv", 0) == 0                # kein Pool-Titel
    assert all(e["rarity"] != "exklusiv" for e in titles.random_titles(40))
    # rarity_of vergibt die Stufe grundsaetzlich nie.
    assert all(titles.rarity_of(t) != "exklusiv"
               for t in ("Goldener König", "Wilder Wolf", "Titan des Chaos"))


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

        # Kauf: Coins ab, Lose gezaehlt, Einsatz wandert in die Kasse.
        r = asyncio.run(lt.buy(SimpleNamespace(id=1), 3))
        assert "3" in r and economy.get_coins(1) == 1_000_000 - 3 * 10_000
        assert lt._entries()["1"] == 3
        assert lt._state()["house"] == 3 * 10_000        # Einsatz -> Flos Kasse
        # Zu wenig Coins -> Hinweis, keine Lose, Kasse unveraendert.
        r = asyncio.run(lt.buy(SimpleNamespace(id=2), 1))
        assert isinstance(r, str) and "2" not in lt._entries()
        assert lt._state()["house"] == 3 * 10_000
        # 'max' deckelt nach Guthaben.
        assert lt._resolve_count(SimpleNamespace(id=1), "max") == \
            economy.get_coins(1) // 10_000

        # Gewinnchance: 1 Los bei chance=1.0 sicher; bei chance 0 nie.
        assert lt._win_prob_for(1) == 1.0
        lt._win_chance = 0.0
        assert lt._win_prob_for(9999) == 0.0
        lt._win_chance = 1.0

        # Ziehung MIT Gewinn (chance 1.0): Spieler kriegt den Jackpot, won=True.
        vorher = economy.get_coins(1)
        res = lt._draw()
        assert res.won and res.winner_ids == [1]
        assert economy.get_coins(1) == vorher + 20_000_000
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
        assert "Gekauft" in r and fa.price() > p0 and fa.shares_of(1) == 50
        assert c0 - economy.get_coins(1) >= 50 * p0        # Impact -> mind. 50*Startkurs

        # Verkauf: Kurs faellt, Coins zurueck, Depot schrumpft.
        p1 = fa.price()
        r = asyncio.run(fa.sell(SimpleNamespace(id=1), 20))
        assert "Verkauft" in r and fa.price() < p1 and fa.shares_of(1) == 30

        # Kein Gratis-Arbitrage: sofortiger Round-Trip macht Verlust.
        economy.instance._profile(3)["coins"] = 1_000_000
        start = economy.get_coins(3)
        asyncio.run(fa.buy(SimpleNamespace(id=3), 100))
        asyncio.run(fa.sell(SimpleNamespace(id=3), 100))
        assert economy.get_coins(3) < start and fa.shares_of(3) == 0

        # Aktien auf KREDIT: zu wenig Coins -> trotzdem Kauf, Konto geht INS MINUS.
        economy.instance._profile(4)["coins"] = 10
        r = asyncio.run(fa.buy(SimpleNamespace(id=4), 100))
        assert isinstance(r, str) and fa.shares_of(4) == 100
        assert economy.get_coins(4) < 0 and "MINUS" in r

        # Aktivitaets-Takt: viel los -> Kurs STEIGT, wenig -> faellt (Rauschen aus).
        floaktie.TICK_NOISE = 0.0
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE})
        a, n, drift, act = fa._activity_tick(15, 0)          # 15 im Call
        assert n > a and drift > 0
        fa._store.data.update({"price": 5000, "act_ema": floaktie.ACT_BASELINE})
        a, n, drift, act = fa._activity_tick(0, 0)           # niemand da
        assert drift < 0 and n <= a                          # Tendenz nach unten
        # Auch VIELE NACHRICHTEN treiben den Kurs (msgs / MSG_DIVISOR).
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE})
        a, n, drift, act = fa._activity_tick(0, 200)         # reger Chat
        assert n > a and drift > 0 and act > floaktie.ACT_BASELINE
        # Live-Streamer zaehlen EXTRA: gleiche Personen, aber Streams -> mehr Aktivitaet.
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE})
        _, _, _, act_plain = fa._activity_tick(12, 0)
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE})
        _, _, _, act_stream = fa._activity_tick(12, 0, streams=6, video=3)
        assert act_stream > act_plain                        # Streamer/Kameras zaehlen mit
        # Ueber eine Stunde (60 Min-Takte) aktiver Call -> Kurs klar hoch.
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE})
        for _ in range(60):
            fa._activity_tick(12, 30, streams=6)
        assert fa.price() > 1030                             # klar gestiegen (Boersenwert mit)
        # note_message + sample_and_tick: Nachrichten fliessen in die Aktivitaet ein.
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE,
                               "msg_count": 0, "last_msg_count": 0, "day": fa._today()})
        for _ in range(200):
            fa.note_message()
        guild0 = SimpleNamespace(voice_channels=[], afk_channel=None)
        asyncio.run(fa.sample_and_tick(guild0))
        assert fa.price() > 1000                             # Chat allein hob den Kurs

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

        # handle-Routing: Nicht-Befehl -> None; kauf -> str; top/depot -> Embed.
        # 'aktie' ist identisch zu 'floaktie' (nur EINE Aktie).
        def fmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="T"))
        assert asyncio.run(fa.handle(fmsg(1, "wie gehts"))) is None
        assert isinstance(asyncio.run(fa.handle(fmsg(1, "floaktie kauf 1"))), str)
        assert isinstance(asyncio.run(fa.handle(fmsg(1, "aktie kauf 1"))), str)   # 'aktie' == 'floaktie'
        assert not isinstance(asyncio.run(fa.handle(fmsg(1, "aktie top"))), (str, type(None)))
        assert not isinstance(asyncio.run(fa.handle(fmsg(1, "floaktie depot"))), (str, type(None)))

        # Kurs-Chart: Serie hat >=2 Punkte, _chart_file rendert ein PNG.
        assert len(fa._series(7)) >= 2
        f = fa._chart_file(7, "7 Tage")
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
        n0, c0 = len(edits), len(chart_edits)
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

        # 3) Reiner Chat treibt ihn auch (ohne einen einzigen im Call).
        frisch()
        _a, _n, drift, akt = fa._activity_tick(0, 40)
        assert drift > 0 and akt > 0

        # 4) 10 im Call, ALLE streamen: in der ERSTEN Minute sichtbar (>= 1 %),
        #    nach einer Stunde vielfach.
        frisch()
        a, n, drift, _akt = fa._activity_tick(10, 20, streams=10)
        assert drift >= 0.01, drift
        assert n > a
        for _ in range(59):
            fa._activity_tick(10, 20, streams=10)
        assert fa.price() > 2500, fa.price()          # nach 1 h fast verdreifacht

        # 5) Toter Server: sinkt, aber LANGSAM (nie mehr als IDLE_CAP je Minute).
        frisch(5000)
        vorher = fa.price()
        for _ in range(60):
            fa._activity_tick(0, 0)
        gefallen = 1 - fa.price() / vorher
        assert 0 < gefallen < 0.15, gefallen
        # und ueber TAGE deutlich runter (rund -11 %/Tag), Richtung Wert eines
        # toten Servers - aber nie darunter.
        for _ in range(3 * 1440):
            fa._activity_tick(0, 0)
        drei_tage = fa.price()
        assert 3000 < drei_tage < 4200, drei_tage
        for _ in range(20 * 1440):
            fa._activity_tick(0, 0)
        assert fa.price() < drei_tage * 0.4, (drei_tage, fa.price())
        for _ in range(30 * 1440):
            fa._activity_tick(0, 0)
        assert fa.price() >= floaktie.MIN_PRICE, fa.price()

        # 6) Kein Aufsummieren: Dauer-Vollbetrieb laeuft in ein NIVEAU (Deckel:
        #    CEIL_FACTOR x Zielkurs), statt zu explodieren (vorher: 10**16 nach
        #    30 Tagen). Der Deckel steigt nur, wenn die Aktivitaet steigt.
        frisch()
        for _ in range(4 * 1440):
            fa._activity_tick(10, 40, streams=10)
        vier_tage = fa.price()
        for _ in range(20 * 1440):
            fa._activity_tick(10, 40, streams=10)
        akt = fa.activity_of(10, 10, 0, 40)
        deckel = fa.ziel_base(akt) * floaktie.CEIL_FACTOR
        assert fa.price() <= deckel * 1.02, (fa.price(), deckel)
        assert fa.price() < vier_tage * 1.05, (vier_tage, fa.price())

        # 7) Sofort-Impuls: Livestream geht an -> Kurs zieht augenblicklich an,
        #    aber pro Minute gedeckelt (kein Pump durch Rein-/Rausspringen).
        frisch()
        vorher = fa.price()
        assert fa._puls(floaktie.PULSE_STREAM, "test") > vorher
        for _ in range(30):
            fa._puls(floaktie.PULSE_STREAM, "test")
        assert fa.price() <= vorher * (1 + floaktie.PULSE_MAX_PER_MIN) + 1, fa.price()

        # 8) Signal deutlich ueber dem Rauschen - man MUSS es sehen koennen.
        #    (Beim Niveau-Modell zaehlt der Abstand zum Ziel: steht der Kurs auf
        #    Totlast-Niveau, treiben schon 4 Leute im Call ihn klar nach oben.)
        frisch(int(floaktie.FAIR_BASE))
        sig = fa.drift_fuer(fa.activity_of(4, 0, 0, 0))
        assert sig > floaktie.TICK_NOISE * 10, (sig, floaktie.TICK_NOISE)
        # Solange Leute da sind, geht es NIE runter: steht der Kurs schon weit
        # ueber seinem Wert, geht es hoechstens seitwaerts.
        frisch(5000)
        assert fa.drift_fuer(fa.activity_of(4, 0, 0, 0)) >= 0
        frisch(500_000)
        assert fa.drift_fuer(fa.activity_of(4, 0, 0, 0)) == 0
        # Nur ohne jede Aktivitaet faellt er.
        assert fa.drift_fuer(0) < 0

        # 9) Messung ohne Member-Cache: voice_states statt members.
        class _VS:
            def __init__(self, stream=False, video=False):
                self.self_stream, self.self_video = stream, video

        class _VC:
            id = 5
            voice_states = {11: _VS(True), 12: _VS(), 13: _VS(video=True)}
            members = []                      # Cache leer!

        guild = SimpleNamespace(voice_channels=[_VC()], afk_channel=None,
                               me=SimpleNamespace(id=99),
                               get_member=lambda _uid: None)
        assert fa._measure(guild) == (3, 1, 1)
        # Flo selbst und andere Bots zaehlen nicht mit.
        _VC.voice_states = {99: _VS(), 11: _VS()}
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
    fa._store = _FakeStore({
        "price": int(p), "base": float(p), "day": "x", "act_ema": 20.0,
        "msg_count": 0, "last_msg_count": 0, "holdings": {},
        "history": [{"day": f"2026-06-{d:02d}", "price": 800 + d * 30} for d in range(1, 25)],
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

    async def send(self, content=None, embed=None, view=None, **_kw):
        self.sent.append({"content": content, "embed": embed, "view": view})
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
    gw._protect = lambda _msg: None

    def restore():
        gw._store, gw._enabled, gw._wizards, gw._client = alt
        gw._protect = alt_protect
        restore_eco()
    return restore, gw


def _schulden_setup(coins_by_uid=None):
    """schulden + economy mit Fake-Stores. Rueckgabe: (restore, sch)"""
    import schulden
    restore_eco = _with_economy(coins_by_uid or {})
    sch = schulden.instance
    alt = (sch._store, sch._enabled)
    sch._store = _FakeStore({"pairs": {}})
    sch._enabled = True

    def restore():
        sch._store, sch._enabled = alt
        restore_eco()
    return restore, sch


def test_schulden_netto_saldo():
    """Kern der Kreide-Tafel: pro Personen-Paar EIN Netto-Saldo, Rueckzahlungen
    verrechnen sich automatisch - und die Reihenfolge der IDs ist egal."""
    restore, sch = _schulden_setup()
    try:
        A, B = 111, 222
        # A gibt B 1.000 -> B steht mit 1.000 bei A in der Kreide.
        vorher, nachher = sch.record_pay(A, B, 1000)
        assert (vorher, nachher) == (0, 1000)
        assert sch.saldo(A, B) == 1000
        assert sch.saldo(B, A) == -1000            # Gegenrichtung spiegelt

        # B zahlt 400 zurueck -> 600 bleiben offen.
        vorher, nachher = sch.record_pay(B, A, 400)
        assert (vorher, nachher) == (-1000, -600)  # aus SICHT von B
        assert sch.saldo(A, B) == 600

        # B zahlt 600 -> ausgeglichen.
        sch.record_pay(B, A, 600)
        assert sch.saldo(A, B) == 0 and sch.saldo(B, A) == 0

        # B zahlt 200 zu viel -> jetzt steht A bei B in der Kreide.
        sch.record_pay(B, A, 200)
        assert sch.saldo(B, A) == 200 and sch.saldo(A, B) == -200

        # Nur EIN Paar-Eintrag, egal in welcher Richtung gezahlt wurde.
        assert len(sch._pairs()) == 1
        saldo, n, vol, log_ = sch.paar_info(A, B)
        assert saldo == -200 and n == 4 and vol == 2200 and len(log_) == 4

        # Grosse IDs in umgekehrter Reihenfolge: gleiche Rechnung.
        C, D = 999999999999999999, 111111111111111111
        sch.record_pay(C, D, 5000)
        assert sch.saldo(C, D) == 5000 and sch.saldo(D, C) == -5000

        # Unsinn wird ignoriert (kein Eintrag, kein Crash).
        vorher_anzahl = len(sch._pairs())
        for von, an, betrag in ((A, A, 100), (A, B, 0), (A, B, -50), ("x", B, 10)):
            assert sch.record_pay(von, an, betrag) == (0, 0)
        assert len(sch._pairs()) == vorher_anzahl
    finally:
        restore()


def test_schulden_hinweis_texte():
    """Der Hinweis unter 'pay' muss die vier Faelle unterscheiden: neue Forderung,
    Teil-Rueckzahlung, Ausgleich und Umkehrung - und an eigene offene Posten
    erinnern. Er darf NIE eine Zahlung verhindern (er ist nur Text)."""
    restore, sch = _schulden_setup()
    try:
        A, B, C = 111, 222, 333
        economy.instance._profile(A)["name"] = "Anna"
        economy.instance._profile(B)["name"] = "Bert"
        economy.instance._profile(C)["name"] = "Cem"

        # 1) Neue Forderung
        t = sch.pay_hinweis(A, B, 1000, ziel_name="Bert")
        assert "Bert" in t and "1.000" in t and "Kreide" in t

        # 2) Teil-Rueckzahlung (B zahlt 400 an A)
        t = sch.pay_hinweis(B, A, 400, ziel_name="Anna")
        assert "offen" in t and "600" in t and "1.000" in t   # vorher/nachher

        # 3) Ausgleich
        t = sch.pay_hinweis(B, A, 600, ziel_name="Anna")
        assert "ausgeglichen" in t.lower()

        # 4) Umkehrung
        t = sch.pay_hinweis(B, A, 250, ziel_name="Anna")
        assert "Anna" in t and "250" in t

        # 5) Erinnerung an EIGENE offene Posten bei Dritten: C schuldet A und B,
        #    zahlt aber an jemand anderen.
        sch.record_pay(A, C, 5000)     # C schuldet A 5.000
        sch.record_pay(B, C, 3000)     # C schuldet B 3.000
        t = sch.pay_hinweis(C, 444, 100)
        assert "8.100" in t or "8.000" in t, t     # Summe der eigenen Posten
        assert "Anna" in t and "Bert" in t         # die zwei groessten
        # Wer selbst nichts offen hat, bekommt keine Erinnerungszeile.
        # (A schuldet B nach Schritt 4 tatsaechlich 250 - daher ein frischer Nutzer.)
        assert sch.summen(A)[1] > 0
        t = sch.pay_hinweis(777, 888, 100)
        assert "Bei dir selbst" not in t
    finally:
        restore()


def test_schulden_posten_summen_und_erlassen():
    """Uebersicht (wer bekommt, wer schuldet), Summen, Top-Liste und Erlassen."""
    restore, sch = _schulden_setup()
    try:
        A, B, C, D = 1, 2, 3, 4
        sch.record_pay(A, B, 1000)      # B schuldet A 1.000
        sch.record_pay(A, C, 2500)      # C schuldet A 2.500
        sch.record_pay(D, A, 400)       # A schuldet D   400

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
    finally:
        restore()


def test_schulden_pay_geht_immer_durch():
    """WICHTIG: die Tafel ist nur Anzeige. 'pay' bewegt die Coins wie vorher, der
    Hinweis haengt nur dran - und selbst wenn die Tafel kaputt ist, geht die
    Zahlung durch."""
    import schulden
    restore, sch = _schulden_setup({7: 10_000, 8: 0})
    alt_flush = economy.instance._flush

    async def kein_flush():
        return None
    economy.instance._flush = kein_flush

    class _Autor:
        id = 7
        display_name = "Zahler"
        bot = False

    class _Ziel:
        id = 8
        display_name = "Empfänger"
        bot = False

    ziel = _Ziel()
    msg = SimpleNamespace(content=f"flo pay <@8> 2500", mentions=[ziel],
                          author=_Autor(), guild=SimpleNamespace(id=1, get_member=lambda _u: None))
    try:
        antwort = asyncio.run(economy.instance._pay(msg))
        # Coins sind geflossen ...
        assert economy.get_coins(7) == 7500 and economy.get_coins(8) == 2500
        # ... und die Notiz haengt dran.
        assert "2.500" in antwort and "Kreide" in antwort
        assert sch.saldo(7, 8) == 2500

        # Zweite Zahlung: der Hinweis zeigt den gewachsenen Stand.
        msg.content = "flo pay <@8> 500"
        antwort = asyncio.run(economy.instance._pay(msg))
        assert "3.000" in antwort and sch.saldo(7, 8) == 3000
        assert economy.get_coins(7) == 7000

        # Tafel kaputt (wirft) -> Zahlung MUSS trotzdem durchgehen.
        def kaputt(*_a, **_k):
            raise RuntimeError("Tafel kaputt")
        alt_note = schulden.instance.note_pay
        schulden.instance.note_pay = kaputt
        try:
            msg.content = "flo pay <@8> 1000"
            antwort = asyncio.run(economy.instance._pay(msg))
            assert economy.get_coins(7) == 6000 and economy.get_coins(8) == 4000
            assert "1.000" in antwort
        finally:
            schulden.instance.note_pay = alt_note

        # Nicht genug Geld -> kein Eintrag auf der Tafel.
        vorher = sch.saldo(7, 8)
        msg.content = "flo pay <@8> 999999"
        antwort = asyncio.run(economy.instance._pay(msg))
        assert "nicht genug" in antwort.lower() and sch.saldo(7, 8) == vorher
    finally:
        economy.instance._flush = alt_flush
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
        async def send(self, text):
            gesendet.append(text)

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
            assert j["ok"] and len(gesendet) == 1 and len(gesendet[0]) <= 1900

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


def run():
    tests = sorted(name for name in globals() if name.startswith("test_"))
    for name in tests:
        globals()[name]()
        print(f"ok  {name}")
    print(f"\n{len(tests)} Tests bestanden.")


if __name__ == "__main__":
    run()
