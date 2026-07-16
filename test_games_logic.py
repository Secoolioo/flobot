"""Pur-logische Tests fuer Casino-, Spiel- und Wort-Zaehler-Logik.

Laufen OHNE Discord-Verbindung und ohne Zusatzpakete (gleicher Runner wie
test_logic.py):  python test_games_logic.py
"""
from __future__ import annotations

import random

import casino
import cmdnorm
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


def run() -> None:
    tests = sorted(name for name in globals() if name.startswith("test_"))
    for name in tests:
        globals()[name]()
        print(f"ok  {name}")
    print(f"\n{len(tests)} Tests bestanden.")


if __name__ == "__main__":
    run()
