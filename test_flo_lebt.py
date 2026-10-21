"""Flo lebt: Launen (laune.py) und "Er haelt nach" (nachhaken.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_flo_lebt.py      nur diese Datei

Das Fundament legt hier nur das Geruest fest - der Teil "Flo lebt" fuellt die
Datei mit seinen Tests.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module

import laune
import nachhaken

#: Befehle fuer den gemeinsamen Rauchtest (test_fundament.py).
RAUCHPROBEN = (
    (laune, ()),
    (nachhaken, ()),
)


def test_flo_lebt_geruest_hat_die_schnittstelle():
    """bot.py und ai.py rufen diese Namen - es muss sie geben, auch bevor sie
    etwas tun. Die Prompt-Zeile ist ohne Laune leer, damit sie nichts am
    Prompt verschiebt."""
    for name in ("setup", "is_enabled", "handle", "tick", "nachricht", "jackpot",
                 "prompt_zeile", "status_zeile", "stand"):
        assert callable(getattr(laune, name, None)), name
    for name in ("setup", "is_enabled", "handle", "tick", "note_message",
                 "vielleicht_nachfragen", "vergiss", "zeilen_fuer"):
        assert callable(getattr(nachhaken, name, None)), name
    assert isinstance(laune.prompt_zeile(77), str)
    assert isinstance(nachhaken.zeilen_fuer(77, 1), list)
    assert isinstance(nachhaken.vergiss(77, 1), int)


if __name__ == "__main__":
    run(globals())
