"""Was die Module vom laufenden Bot brauchen - ohne bot.py zu importieren.

WOZU
====
Zehn Module machten bisher mitten in einer Funktion ein `import bot`, um an drei
Sachen zu kommen: den Client, den Loesch-Schutz und dessen Gegenstueck. Das war
kein Versehen, sondern Absicht - ein Import auf Modulebene waere ein Ring
(bot importiert die Module, die Module importieren bot), und Python haette ihn
beim Start abgewiesen.

Der Preis dafuer ist unangenehm:

  * `import bot` ist NUR im laufenden Bot ein Nachschlagen. Ueberall sonst - in
    Tests, in werkzeug/ - ist es ein echter Import, der bot.py von oben bis
    unten ausfuehrt. Und bot.py hat Nebenwirkungen. Eine davon ist
    webpanel.setup(), die WEBPANEL_AUTH neu aus der Umgebung liest: ein
    harmloser Panel-Aufruf machte damit aus einem Panel ohne Login mitten im
    Betrieb eines mit Login. Nachgemessen, nicht vermutet.

  * Der Bot laeuft als `python bot.py` und heisst dann `__main__`. Damit die
    zehn `import bot` ueberhaupt dasselbe Modul treffen, setzt bot.py sich per
    `sys.modules.setdefault("bot", ...)` selbst unter dem zweiten Namen ein.
    Das haelt, ist aber ein Trick, und beim Aufteilen von bot.py in ein Paket
    faellt er um.

Diese Datei loest beides auf. Sie importiert NICHTS aus dem Bot - deshalb kann
jeder sie importieren, auch auf Modulebene, ohne einen Ring zu bauen. bot.py
traegt sich beim Start ein; bis dahin (und in Tests, und in Werkzeugen) tun die
Funktionen hier schlicht nichts.

    laufzeit.anmelden(client=..., protect_message=..., release_message=...)

Absichtlich still statt laut: der Loesch-Schutz ist Komfort. Faellt er weg,
bleibt eine Spielnachricht ein paar Minuten laenger stehen. Dafuer den Aufrufer
mit einer Ausnahme zu behelligen waere unverhaeltnismaessig - genau darum stand
um jedes `import bot` schon vorher ein try/except.
"""

import logging

log = logging.getLogger("dcbot.laufzeit")

#: Der discord.Client, sobald bot.py ihn gebaut hat. Vorher None.
client = None


def _still(*_a, **_k):
    """Platzhalter, solange kein Bot laeuft. Tut nichts und sagt nichts."""
    return None


#: Meldet eine Nachricht beim Auto-Loesch-Schutz an (Spielrunde laeuft).
protect_message = _still

#: Hebt den Schutz nach einer Gnadenfrist wieder auf.
release_message = _still


def anmelden(**teile):
    """bot.py traegt hier beim Start seine Schnittstelle ein.

    Bewusst mit Namen statt Reihenfolge: wer eine Zeile dazunimmt, soll nicht
    aus Versehen etwas anderes ueberschreiben.
    """
    global client, protect_message, release_message
    for name, wert in teile.items():
        if name not in ("client", "protect_message", "release_message"):
            raise KeyError(f"laufzeit kennt '{name}' nicht")
        globals()[name] = wert
    log.debug("Laufzeit angemeldet: %s", ", ".join(sorted(teile)))


def abmelden():
    """Alles zuruecksetzen - fuer Tests, damit kein Bot durchschlaegt."""
    global client, protect_message, release_message
    client = None
    protect_message = _still
    release_message = _still


def laeuft():
    """True, wenn ein Bot sich angemeldet hat."""
    return client is not None
