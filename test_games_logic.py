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
