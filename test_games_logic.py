"""Pur-logische Tests fuer Casino-, Spiel-, Wort-Zaehler- und Admin-Logik.

Laufen OHNE Discord-Verbindung und ohne Zusatzpakete (gleicher Runner wie
test_logic.py):  python test_games_logic.py
"""

import asyncio
import random
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


# --- Payments (Coins mit echtem Geld) ------------------------------------------
def test_payments_flow():
    """Befehlserkennung, Bestellung anlegen, Polling schreibt bei 'bezahlt' EINMAL
    gut (keine Doppel-Gutschrift), und alles ist aus, solange kein Key da ist."""
    import payments

    class FakeStore:
        def __init__(self, data):
            self.data = data

        async def save(self):
            pass

    # economy mit Fake-Store aktivieren.
    alt_eco = (economy.instance._store, economy.instance._enabled)
    economy.instance._store = FakeStore({"users": {}})
    economy.instance._enabled = True
    economy.instance._profile(5)["coins"] = 0

    p = payments.instance
    alt = (p._enabled, p._store, p._secret_key, p._stripe, p._notify)
    p._enabled = True
    p._secret_key = "sk_test_x"
    p._store = FakeStore({"orders": {}})

    async def fake_notify(uid, coins, neu):
        return None
    p._notify = fake_notify

    # Stripe-Antworten steuerbar machen.
    box = {"paid": False}

    async def fake_stripe(method, path, fields=None):
        if method == "POST" and path == "checkout/sessions":
            return {"id": "cs_test_1", "url": "https://checkout.stripe.com/pay/cs_test_1",
                    "payment_status": "unpaid"}
        if method == "GET" and path.startswith("checkout/sessions/"):
            return {"payment_status": "paid" if box["paid"] else "unpaid",
                    "status": "complete" if box["paid"] else "open"}
        return None
    p._stripe = fake_stripe

    try:
        # Befehlserkennung: aufladen/echtgeld/coins kaufen JA; coins allein NEIN.
        assert p._is_command("aufladen")
        assert p._is_command("echtgeld")
        assert p._is_command("coins kaufen")
        assert not p._is_command("coins")           # -> economy zeigt Kontostand
        assert not p._is_command("wie gehts")
        # Paket-Mathematik + Preisleiter (1-10 €, 100k Stufe = 1 €).
        assert payments._fmt(2_000_000) == "2.000.000"
        assert payments._euro(100) == "1,00 €" and payments._euro(1000) == "10,00 €"
        pk = payments.packages()
        assert pk["p1"]["coins"] == 100_000 and pk["p1"]["cents"] == 100
        assert pk["p5"]["coins"] == 1_000_000 and pk["p5"]["cents"] == 500
        assert pk["p10"]["coins"] == 2_250_000 and pk["p10"]["cents"] == 1000
        assert len(pk) == 10

        # Bestellung anlegen -> pending gemerkt, URL zurueck.
        url = asyncio.run(p.create_checkout(5, "Tester", "p1"))
        assert url and "checkout.stripe.com" in url
        orders = p._store.data["orders"]
        assert "cs_test_1" in orders and orders["cs_test_1"]["status"] == "pending"
        assert orders["cs_test_1"]["coins"] == 100_000

        # Poll, solange NICHT bezahlt -> keine Gutschrift.
        asyncio.run(p.poll_pending())
        assert economy.get_coins(5) == 0
        assert orders["cs_test_1"]["status"] == "pending"

        # Jetzt bezahlt -> genau 100.000 Coins gutgeschrieben, Status 'credited'.
        box["paid"] = True
        asyncio.run(p.poll_pending())
        assert economy.get_coins(5) == 100_000
        assert orders["cs_test_1"]["status"] == "credited"

        # Nochmal pollen -> KEINE Doppel-Gutschrift.
        asyncio.run(p.poll_pending())
        assert economy.get_coins(5) == 100_000

        # Owner-Uebersicht 'umsatz': zeigt den Kauf; Fremde bekommen None.
        def hmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="Owner"))
        emb = asyncio.run(payments.handle(hmsg(payments.OWNER_ID, "umsatz")))
        assert emb is not None and not isinstance(emb, str)   # discord.Embed
        assert asyncio.run(payments.handle(hmsg(999999, "umsatz"))) is None
    finally:
        (p._enabled, p._store, p._secret_key, p._stripe, p._notify) = alt
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


def run():
    tests = sorted(name for name in globals() if name.startswith("test_"))
    for name in tests:
        globals()[name]()
        print(f"ok  {name}")
    print(f"\n{len(tests)} Tests bestanden.")


if __name__ == "__main__":
    run()
