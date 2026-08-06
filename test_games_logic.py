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
import floaktie
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
    """Minimaler JsonStore-Ersatz fuer die Tests (kein Datei-IO)."""

    def __init__(self, data):
        self.data = data

    async def save(self):
        pass


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
        for _ in range(4 * 1440):
            fa._activity_tick(10, 40, streams=10)
        vier_tage = fa.price()
        for _ in range(20 * 1440):
            fa._activity_tick(10, 40, streams=10)
        akt = fa.activity_of(10, 10, 0, 40)
        deckel = fa.ziel_base(akt) * floaktie.CEIL_FACTOR
        # Toleranz = ein voller Takt: die Pruefung im Drift laeuft VOR dem Schritt,
        # der letzte Schritt darf also einmal um bis zu TICK_CAP ueberschiessen.
        assert fa.price() <= deckel * (1 + floaktie.TICK_CAP) * 1.01, (fa.price(), deckel)
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
        for startkurs in (1_000, int(floaktie.FAIR_BASE)):
            frisch(startkurs)
            for _ in range(6 * 60):
                fa._activity_tick(3, 0)
            faktor = fa.price() / startkurs
            assert faktor > 8, (startkurs, fa.price(), faktor)
        # ... aber NICHT unbegrenzt: steht der Kurs schon am Deckel dieser
        # Aktivitaet, hoert es auf. Genau das verhindert die Hyperinflation
        # (vorher: 750 Anteile fuer 1,1 Mio -> Verkauf fuer 43 MILLIARDEN).
        akt3 = fa.activity_of(3, 0, 0, 0)
        frisch(int(fa.ziel_base(akt3) * floaktie.CEIL_FACTOR))
        vorm_deckel = fa.price()
        for _ in range(6 * 60):
            fa._activity_tick(3, 0)
        assert fa.price() <= vorm_deckel * 1.06, (vorm_deckel, fa.price())
        # Und mehr Leute muessen auf demselben Niveau schneller sein.
        frisch(50_000)
        d3 = fa.drift_fuer(fa.activity_of(3, 0, 0, 0))
        d10 = fa.drift_fuer(fa.activity_of(10, 0, 0, 0))
        d10s = fa.drift_fuer(fa.activity_of(10, 10, 0, 0))
        assert d3 < d10 < d10s or d10s >= floaktie.TICK_CAP * 0.99, (d3, d10, d10s)

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
    i = mod_quelle.index("if (count >= WARN_LIMIT")
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
        def text_von(emb):
            teile = [emb.title or "", emb.description or ""]
            for f in emb.fields:
                teile.append(str(f.name)); teile.append(str(f.value))
            return "\n".join(teile)

        emb = asyncio.run(economy.instance._pay(msg))
        # Coins sind geflossen ...
        assert economy.get_coins(7) == 7500 and economy.get_coins(8) == 2500
        # ... und die Karte zeigt Betrag + Kreide-Stand.
        t = text_von(emb)
        assert "2.500" in t and "Kreide" in t and "Überweisung" in t
        assert sch.saldo(7, 8) == 2500

        # Zweite Zahlung: die Karte zeigt den gewachsenen Stand.
        msg.content = "flo pay <@8> 500"
        emb = asyncio.run(economy.instance._pay(msg))
        assert "3.000" in text_von(emb) and sch.saldo(7, 8) == 3000
        assert economy.get_coins(7) == 7000

        # Tafel kaputt (wirft) -> Zahlung MUSS trotzdem durchgehen.
        def kaputt(*_a, **_k):
            raise RuntimeError("Tafel kaputt")
        alt_note = schulden.instance.note_pay_block
        schulden.instance.note_pay_block = kaputt
        try:
            msg.content = "flo pay <@8> 1000"
            emb = asyncio.run(economy.instance._pay(msg))
            assert economy.get_coins(7) == 6000 and economy.get_coins(8) == 4000
            assert "1.000" in text_von(emb)
        finally:
            schulden.instance.note_pay_block = alt_note

        # Nicht genug Geld -> kein Eintrag auf der Tafel.
        vorher = sch.saldo(7, 8)
        msg.content = "flo pay <@8> 999999"
        antwort = asyncio.run(economy.instance._pay(msg))
        assert isinstance(antwort, str) and "nicht genug" in antwort.lower()
        assert sch.saldo(7, 8) == vorher
    finally:
        economy.instance._flush = alt_flush
        restore()


def test_schulden_automatische_tilgung():
    """Der Zwang zum Zahlen: von jeder ECHTEN Einnahme wandert ein Anteil
    automatisch an den groessten Glaeubiger. Coins entstehen dabei nie und
    verschwinden nie - sie wechseln nur den Besitzer."""
    import schulden
    restore, sch = _schulden_setup({1: 0, 2: 0, 3: 0})
    try:
        A, B, C = 1, 2, 3
        sch.record_pay(B, A, 10_000)          # A schuldet B 10.000
        sch.record_pay(C, A, 4_000)           # A schuldet C  4.000
        assert sch.saldo(B, A) == 10_000 and sch.saldo(C, A) == 4_000
        summe_vorher = sum(economy.get_coins(u) for u in (A, B, C))

        # A gewinnt 1.000 im Casino -> 20 % gehen an den GROESSTEN Glaeubiger (B).
        economy.add_coins(A, 1000, reason="casino")
        assert economy.get_coins(A) == 800, economy.get_coins(A)
        assert economy.get_coins(B) == 200
        assert sch.saldo(B, A) == 9_800       # Schuld ist echt kleiner
        assert sch.saldo(C, A) == 4_000       # der kleinere Posten bleibt
        assert sch.getilgt_summe(A) == 200
        # Coins nur umverteilt (plus die 1.000 Einnahme von aussen).
        assert sum(economy.get_coins(u) for u in (A, B, C)) == summe_vorher + 1000

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

        # Getilgt wird immer beim GROESSTEN Posten - erst B (9.800), dann C.
        sch.erlassen(B, A, sch.saldo(B, A) - 3_000)   # B nur noch 3.000, C 4.000
        vor_c = economy.get_coins(C)
        economy.add_coins(A, 1000, reason="casino")
        assert economy.get_coins(C) == vor_c + 200    # jetzt ist C der groesste
        assert sch.saldo(C, A) == 3_800

        # Nie mehr als die Restschuld: alles bis auf 50 erlassen, dann gross gewinnen.
        sch.erlassen(C, A)                            # C komplett erlassen
        sch.erlassen(B, A, sch.saldo(B, A) - 50)      # bei B bleiben 50
        assert sch.saldo(B, A) == 50 and sch.saldo(C, A) == 0
        vor_b = economy.get_coins(B)
        economy.add_coins(A, 100_000, reason="spiele")
        assert economy.get_coins(B) == vor_b + 50     # exakt die Restschuld
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


def test_schulden_mahnung():
    """Mahnung per DM: nur ab einer Mindestsumme und hoechstens einmal je Abstand."""
    import schulden
    restore, sch = _schulden_setup()
    try:
        sch.record_pay(1, 2, 50_000)      # 2 schuldet 1 -> 2 wird gemahnt
        sch.record_pay(1, 3, 10)          # zu klein -> keine Mahnung
        gesendet = []

        class _User:
            def __init__(self, uid):
                self.id = uid

            async def send(self, **kw):
                gesendet.append((self.id, kw.get("embed")))

        client = SimpleNamespace(get_user=lambda uid: _User(uid))
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
        sch._stats(2)["mahnung"] = time.time() - schulden.MAHN_ABSTAND - 5
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
        # Im Leerlauf dreht es ins Minus.
        for _ in range(90):
            fa._activity_tick(0, 0, dt=20.0)
        assert fa._store.data["mom"] < 0

        # 3) TAKTUNABHAENGIG: 60 s und 20 s kommen am Ende auf dasselbe Niveau.
        werte = {}
        for dt in (60.0, 20.0):
            ergebnisse = []
            for lauf in range(12):
                random.seed(700 + lauf)
                frisch()
                for _ in range(int(3 * 3600 / dt)):
                    fa._activity_tick(3, 4, streams=1, dt=dt)
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
        boden = floaktie.FAIR_BASE * (1 + 147 / floaktie.LIQUIDITY)
        assert fa.price() >= floaktie.MIN_PRICE
        assert abs(fa.price() - boden) < boden * 0.05, (fa.price(), boden)
        # Am Boden sagt das Panel ausdruecklich, dass es nicht tiefer geht.
        restore = _with_economy({2: 0})
        try:
            panel = _embed_text(fa._panel_embed(SimpleNamespace(id=2)))
            assert "Bodensatz erreicht" in panel, panel
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
                          "admin.json", "features.json"}
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


def test_webpanel_ohne_login():
    """Das Panel laeuft standardmaessig OHNE Login (so gewuenscht) - aber der
    Riegel muss sich mit WEBPANEL_AUTH=1 wieder einschalten lassen, und die
    Oberflaeche muss vorher wissen, was Sache ist (/api/config)."""
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    async def lauf(auth):
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._user, wp._pass, wp._auth = "u", "p", auth
        async with TestClient(TestServer(wp._build_app())) as c:
            cfg = await (await c.get("/api/config")).json()
            codes = {}
            for pfad, meth in (("/api/overview", "get"), ("/api/features", "get"),
                               ("/api/user/coins", "post"), ("/api/update", "post")):
                r = await getattr(c, meth)(pfad, json={})
                codes[pfad] = r.status
            return cfg, codes

    # Ohne Login-Pflicht: /api/config sagt das, und nichts gibt mehr 401.
    cfg, codes = asyncio.run(lauf(False))
    assert cfg["ok"] is True and cfg["auth"] is False
    assert all(v != 401 for v in codes.values()), codes
    assert codes["/api/overview"] == 200

    # Mit WEBPANEL_AUTH=1 ist alles wieder dicht.
    cfg, codes = asyncio.run(lauf(True))
    assert cfg["auth"] is True
    assert all(v == 401 for v in codes.values()), codes

    # Der Standard ist AUS - und /api/config ist selbst nie geschuetzt, sonst
    # koennte die Oberflaeche gar nicht erst herausfinden, ob sie einen Login
    # anzeigen muss.
    quelle = open("webpanel.py", encoding="utf-8").read()
    assert 'os.getenv("WEBPANEL_AUTH", "0")' in quelle
    i = quelle.index("async def _api_config")
    assert "_guard" not in quelle[i:i + 400]
    # Und es wird laut gewarnt, wenn ohne Login gestartet wird.
    assert "OHNE LOGIN" in quelle

    # Die Oberflaeche fragt /api/config, bevor sie den Anmeldebildschirm zeigt.
    html = open("webpanel.html", encoding="utf-8").read()
    assert "/api/config" in html and "S.authNoetig" in html


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
