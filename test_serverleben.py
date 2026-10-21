"""Server-Leben: Zitat der Woche (zitate.py) und Geburtstage (geburtstage.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_serverleben.py      nur diese Datei

Das Fundament legt hier nur das Geruest fest - der Teil "Server-Leben" fuellt
die Datei mit seinen Tests.
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import _Klick  # noqa: F401 - die privaten Helfer

import geburtstage
import zitate

#: Befehle fuer den gemeinsamen Rauchtest (test_fundament.py).
RAUCHPROBEN = (
    (zitate, ()),
    (geburtstage, ()),
)


def test_serverleben_geruest_hat_die_schnittstelle():
    """bot.py ruft diese Namen - es muss sie geben, auch bevor sie etwas tun.
    Der Rechtsklick antwortet schon jetzt: eine Interaktion ohne Antwort zeigt
    Discord als 'fehlgeschlagen'."""
    for name in ("setup", "is_enabled", "handle", "tick", "rechtsklick",
                 "ist_eigene_umfrage", "ergebnis"):
        assert callable(getattr(zitate, name, None)), name
    for name in ("setup", "is_enabled", "handle", "tick"):
        assert callable(getattr(geburtstage, name, None)), name
    klick = _Klick(uid=5)
    asyncio.run(zitate.rechtsklick(klick, SimpleNamespace(id=1, content="x")))
    assert klick.response.is_done(), klick.protokoll


if __name__ == "__main__":
    run(globals())
