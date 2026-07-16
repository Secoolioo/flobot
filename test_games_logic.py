"""Pur-logische Tests fuer Casino-, Spiel-, Wort-Zaehler- und Admin-Logik.

Laufen OHNE Discord-Verbindung und ohne Zusatzpakete (gleicher Runner wie
test_logic.py):  python test_games_logic.py
"""
from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import admin
import casino
import cmdnorm
import economy
import render
import words


# --- Blackjack -------------------------------------------------------------
def test_blackjack_handwert() -> None:
    assert casino._hand_value([("A", "♠"), ("K", "♥")]) == 21
    assert casino._hand_value([("A", "♠"), ("A", "♥")]) == 12          # 11 + 1
    assert casino._hand_value([("A", "♠"), ("9", "♥"), ("5", "♦")]) == 15
    assert casino._hand_value([("10", "♠"), ("9", "♥"), ("5", "♦")]) == 24  # Bust


# --- Einsatz-Parsing ---------------------------------------------------------
def test_resolve_bet() -> None:
    assert casino._resolve_bet("50", 0) == 50
    assert casino._resolve_bet("abc", 0) is None
    assert casino._resolve_bet("", 0) is None
    # 'alles' ohne aktives economy: Kontostand 0 -> 0 (kein Crash)
    assert casino._resolve_bet("alles", 0) == 0


# --- Mines -------------------------------------------------------------------
def test_mines_multiplikator() -> None:
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
def test_wheel_hausvorteil() -> None:
    segs = casino._WHEEL_SEGMENTS
    assert len(segs) == 12
    ev = sum(segs) / len(segs)
    assert 0.90 <= ev <= 0.99, ev              # kleiner Hausvorteil, kein Abzock-Rad
    assert any(m == 0 for m in segs)           # Nieten existieren
    assert max(segs) >= 2.0                    # aber auch echte Gewinne


# --- Rubbellos -----------------------------------------------------------------
def test_scratch_roll_konsistent() -> None:
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


def test_scratch_hausvorteil() -> None:
    # Exakter Erwartungswert: 3 unabhaengige Reihen, P(Reihe aus Symbol s) = 1/343.
    n = len(render.SLOT_KEYS)
    ev = 3 * sum(casino._SCRATCH_PAYOUT[s] for s in render.SLOT_KEYS) / (n ** 3)
    assert 0.85 <= ev <= 1.0, ev


# --- Crash ---------------------------------------------------------------------
def test_crash_point_grenzen() -> None:
    random.seed(7)
    for _ in range(2000):
        cp = casino._crash_point()
        assert 1.0 <= cp <= 1000.0, cp


# --- Roulette --------------------------------------------------------------------
def test_roulette_auszahlung() -> None:
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
def test_keno_tabelle() -> None:
    assert casino._KENO_TABLE[(1, 1)] == 3
    assert casino._KENO_TABLE[(8, 8)] == 1000
    assert (3, 1) not in casino._KENO_TABLE   # zu wenig Treffer -> nichts


# --- Wort-Zaehler ------------------------------------------------------------------
def test_words_tokenizer() -> None:
    toks = words._tokenize(
        "Hallo WELT! https://beispiel.de/pfad <@123> <#456> <a:emo:789> "
        "Pizza-Party äöüß 42 a zu")
    assert toks == ["hallo", "welt", "pizza", "party", "äöüß", "zu"]
    assert words._tokenize("") == []
    assert words._tokenize("1234 !!! ...") == []


def test_words_zaehlen() -> None:
    # Fake-Store: reine dict-Logik testen, ohne Datei.
    words._store = type("S", (), {"data": {"words": {}, "total": 0, "msgs": 0}})()
    n = words._count_text("pizza pizza salat", "111")
    assert n == 3
    n = words._count_text("PIZZA!", "222")
    assert n == 1
    daten = words._store.data
    assert daten["words"]["pizza"]["n"] == 3
    assert daten["words"]["pizza"]["u"] == {"111": 2, "222": 1}
    assert daten["words"]["salat"]["n"] == 1
    assert daten["total"] == 4 and daten["msgs"] == 2
    words._store = None


# --- Befehls-Normalisierung ----------------------------------------------------------
def test_cmdnorm_neue_befehle() -> None:
    # Tippfehler-Korrektur auf die neuen Trigger.
    assert cmdnorm.normalize("woerterr pizza") == "woerter pizza"
    assert cmdnorm.normalize("minees 50") == "mines 50"
    # Alltagswoerter duerfen NICHT gekapert werden.
    for satz in ("orte 5", "wert 100", "start jetzt", "statt dessen", "worten nach"):
        assert cmdnorm.normalize(satz) is None, satz
    # Exakte neue Befehle bleiben unveraendert (None = nichts zu korrigieren).
    assert cmdnorm.normalize("wörter pizza") is None
    assert cmdnorm.normalize("mines 50 3") is None


# --- Admin-Befehle (nur Besitzer) -----------------------------------------------------
def _fake_msg(uid: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        author=SimpleNamespace(id=uid, bot=False, display_name="Tester"),
        content=content, mentions=[], guild=None)


def test_admin_extract() -> None:
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


def test_admin_owner_gate() -> None:
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


# --- Leaderboard-Avatare ---------------------------------------------------------
def test_attach_avatars_cache_und_fallback() -> None:
    """Avatar-Laden: Erfolg fuellt Cache, Fehlschlag landet im Negativ-Cache,
    zweiter Aufruf kommt ohne Resolver aus dem Cache."""
    orig = economy._resolve_avatar_user
    economy._AVATAR_CACHE.clear()
    economy._AVATAR_FAIL.clear()
    try:
        # 1) Aufloesung schlaegt fehl -> kein Avatar, Negativ-Cache gesetzt.
        async def _none(_guild, _uid):
            return None
        economy._resolve_avatar_user = _none
        rows = [{"id": 42}]
        asyncio.run(economy._attach_avatars(rows, None))
        assert "avatar" not in rows[0]
        assert 42 in economy._AVATAR_FAIL

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
        economy._resolve_avatar_user = _user
        rows = [{"id": 43}]
        asyncio.run(economy._attach_avatars(rows, None))
        assert rows[0]["avatar"] == b"PNGDATA"
        assert economy._AVATAR_CACHE[43][0] == b"PNGDATA"

        # 3) Zweiter Aufruf: kommt aus dem Cache, Resolver wird nicht gebraucht.
        async def _boom(_guild, _uid):
            raise AssertionError("Resolver darf bei Cache-Treffer nicht laufen")
        economy._resolve_avatar_user = _boom
        rows = [{"id": 43}]
        asyncio.run(economy._attach_avatars(rows, None))
        assert rows[0]["avatar"] == b"PNGDATA"
    finally:
        economy._resolve_avatar_user = orig
        economy._AVATAR_CACHE.clear()
        economy._AVATAR_FAIL.clear()


def test_economy_display_name_of() -> None:
    # economy ist im Test nicht aktiviert -> None statt Crash.
    assert economy.display_name_of(123456789012345678) is None


def test_admin_ansage_parsing() -> None:
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


def test_cmdnorm_admin_sicherheit() -> None:
    # Alltagswoerter, die 1 Tippfehler von Admin-Befehlen entfernt sind,
    # duerfen NICHT gekapert werden.
    for satz in ("nimmt das ernst", "profi tipp", "ansagen bitte"):
        assert cmdnorm.normalize(satz) is None, satz
    # Echte Vertipper werden weiterhin korrigiert.
    assert cmdnorm.normalize("admiin") == "admin"


def run() -> None:
    tests = sorted(name for name in globals() if name.startswith("test_"))
    for name in tests:
        globals()[name]()
        print(f"ok  {name}")
    print(f"\n{len(tests)} Tests bestanden.")


if __name__ == "__main__":
    run()
