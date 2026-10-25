"""Server-Leben: Zitat der Woche (zitate.py) und Geburtstage (geburtstage.py).

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python test_serverleben.py      nur diese Datei
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _Klick, _cfg_frisch, _http_fehler, _with_economy)

import contextlib
import io
import logging
from datetime import datetime
from unittest import mock

import discord

import ai
import features
import geburtstage
import guildcfg
import laufzeit
import uhr
import zitate

#: Befehle fuer den gemeinsamen Rauchtest (test_fundament.py).
RAUCHPROBEN = (
    (zitate, ("zitate", "zitat der woche", "zitate der woche", "zitat weg 1",
              "zitat weg", "zitat speichern", "zitat umfrage jetzt")),
    (geburtstage, ("geburtstag", "geburtstag 12.03.", "geburtstag 12. März 1999",
                   "birthday 29.02.", "bday 31.02.", "geburtstage", "geburtstag löschen")),
)


def test_serverleben_geruest_hat_die_schnittstelle():
    """bot.py ruft diese Namen - es muss sie geben. Der Rechtsklick antwortet
    immer: eine Interaktion ohne Antwort zeigt Discord als 'fehlgeschlagen'."""
    for name in ("setup", "is_enabled", "handle", "tick", "rechtsklick",
                 "ist_eigene_umfrage", "ergebnis"):
        assert callable(getattr(zitate, name, None)), name
    for name in ("setup", "is_enabled", "handle", "tick"):
        assert callable(getattr(geburtstage, name, None)), name
    klick = _Klick(uid=5)
    asyncio.run(zitate.rechtsklick(klick, SimpleNamespace(id=1, content="x")))
    assert klick.response.is_done(), klick.protokoll
    import cmdnorm
    for wort in zitate._CMDS + geburtstage._CMDS:
        assert wort in cmdnorm.KNOWN, wort
    assert "geburtstag" in cmdnorm.ZUSTAND


# =============================================================================
# Gemeinsame Helfer
# =============================================================================
class _Logfang(logging.Handler):
    """Faengt die Logzeilen eines Moduls (statt sie ins Testprotokoll zu kippen)."""

    def __init__(self, name):
        super().__init__(level=logging.DEBUG)
        self.name_ = name
        self.zeilen = []

    def emit(self, record):
        self.zeilen.append(record.getMessage())

    def __enter__(self):
        lg = logging.getLogger(self.name_)
        self.alt = (lg.level, lg.propagate)
        lg.addHandler(self)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        return self

    def __exit__(self, *_a):
        lg = logging.getLogger(self.name_)
        lg.removeHandler(self)
        lg.setLevel(self.alt[0])
        lg.propagate = self.alt[1]
        return False


@contextlib.contextmanager
def _frisch(modul, datei, daten=None):
    """modul.setup() mit einem ECHTEN JsonStore im Test-Datenordner - leer oder
    mit 'daten' als Datei von vorher. Danach alles wie vorher (auch die Datei
    weg: der Rauchtest soll nichts Fremdes finden). Liefert (kern, logzeilen)."""
    kern = modul.instance
    alt = dict(vars(kern))
    pfad = store.DATA_DIR / datei

    def wegraeumen():
        for weg in (pfad, pfad.with_name(pfad.name + ".bak")):
            if weg.exists():
                weg.unlink()
    wegraeumen()
    if daten is not None:
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(json.dumps(daten), encoding="utf-8")
    try:
        with _Logfang(f"dcbot.{modul.__name__}") as fang:
            assert modul.setup() is True
            yield kern, fang.zeilen
    finally:
        vars(kern).clear()
        vars(kern).update(alt)
        wegraeumen()


def _datei(name):
    pfad = store.DATA_DIR / name
    return json.loads(pfad.read_text(encoding="utf-8")) if pfad.exists() else None


@contextlib.contextmanager
def _uhr_gestellt(wand, mono):
    """Eine eigene Uhr (laengst gestartet) hinter allem, was die Module fragen.
    wand/mono sind Listen mit einem Wert - der Test stellt sie."""
    u = uhr.Uhr(zeit=lambda: wand[0], mono=lambda: mono[0])
    u._start = float("-inf")
    with mock.patch.multiple(uhr, jetzt=u.jetzt, stabil=u.stabil, tag_key=u.tag_key,
                             woche_key=u.woche_key, monat_key=u.monat_key,
                             vormonat_key=u.vormonat_key, glaubhaft=u.glaubhaft), \
            _Logfang("dcbot.uhr"):
        yield u


def _wann(tag, stunde, minute=0, monat=3, jahr=2026):
    return datetime(jahr, monat, tag, stunde, minute, tzinfo=uhr.TIMEZONE).timestamp()


def _stellen(wand, mono, ts):
    """Die Zeit vergeht bis 'ts' - ohne Sprung (monotonic laeuft mit)."""
    mono[0] += ts - wand[0]
    wand[0] = ts


def _springt(wand, mono, sekunden):
    """Die Wanduhr springt - monotonic laeuft nur eine Sekunde weiter."""
    wand[0] += sekunden
    mono[0] += 1


class _Kanal:
    def __init__(self, cid=600, *, kaputt=0, umfragen=True, ohne_bild=False):
        self.id = cid
        self.gesendet = []
        self.kaputt = kaputt            # so oft scheitert send noch
        self.umfragen = umfragen
        self.ohne_bild = ohne_bild
        self.nachrichten = {}
        self.beim_senden = None
        self.fehler = None              # so scheitert send EINMAL (kein HTTPException)
        self.verlauf = []               # was history() liefert, aelteste zuerst
        self.verlauf_fehler = None

    def permissions_for(self, _wer):
        return SimpleNamespace(send_messages=True, send_polls=self.umfragen)

    def history(self, limit=100):
        kanal = self

        async def durchgehen():
            if kanal.verlauf_fehler is not None:
                raise kanal.verlauf_fehler
            for m in list(reversed(kanal.verlauf))[:limit]:
                yield m
        return durchgehen()

    async def send(self, content=None, **kw):
        if self.beim_senden is not None:
            self.beim_senden(content, kw)
        if self.fehler is not None:
            fehler, self.fehler = self.fehler, None
            raise fehler
        if self.kaputt > 0:
            self.kaputt -= 1
            raise _http_fehler("Discord sagt nein")
        if self.ohne_bild and "file" in kw:
            raise _http_fehler("Missing Permissions")
        self.gesendet.append((content, kw))
        return SimpleNamespace(id=5000 + len(self.gesendet))

    async def fetch_message(self, mid):
        if mid not in self.nachrichten:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "weg")
        wert = self.nachrichten[mid]
        if isinstance(wert, Exception):
            raise wert
        return wert


class _Server:
    """Ein Server mit Rollen und Mitgliedern, die man holen muss (fetch_member)."""

    def __init__(self, gid=77, *, rollen_recht=True, kanal=None):
        self.id = gid
        self.name = "Testserver"
        self.roles = []
        self.me = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(
            manage_roles=rollen_recht))
        self.kanal = kanal or _Kanal()
        self.mitglieder = {}
        self.protokoll = []
        self.holen_kaputt = set()

    def get_channel(self, cid):
        return self.kanal if cid == self.kanal.id else None

    def get_member(self, _uid):
        return None                     # ohne Mitglieder-Intent: nie im Cache

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    async def create_role(self, **kw):
        rolle = SimpleNamespace(id=555, name=kw["name"])
        self.roles.append(rolle)
        self.protokoll.append(("rolle_neu", kw["name"]))
        return rolle

    def mitglied(self, uid, name=None, *rollen):
        server = self
        person = SimpleNamespace(id=uid, roles=list(rollen), bot=False,
                                 display_name=name or f"Person{uid}", mention=f"<@{uid}>")

        async def geben(rolle, reason=None):
            if "geben" in server.holen_kaputt:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "nein")
            person.roles.append(rolle)
            server.protokoll.append(("geben", uid))

        async def nehmen(rolle, reason=None):
            if "nehmen" in server.holen_kaputt:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "nein")
            person.roles.remove(rolle)
            server.protokoll.append(("nehmen", uid))
        person.add_roles, person.remove_roles = geben, nehmen
        self.mitglieder[uid] = person
        return person

    async def fetch_member(self, uid):
        if "holen" in self.holen_kaputt:
            raise _http_fehler("Discord wackelt")
        if uid not in self.mitglieder:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "weg")
        return self.mitglieder[uid]


class _Raum(_Kanal):
    """Ein Kanal mit Leserechten je Rolle (fuer die Hinterzimmer-Pruefung)."""

    def __init__(self, cid, leser, *, nsfw=False, typ=None):
        super().__init__(cid)
        self.leser = set(leser)
        self.nsfw = nsfw
        self.type = typ or discord.ChannelType.text

    def is_nsfw(self):
        return self.nsfw

    def permissions_for(self, wer):
        return SimpleNamespace(send_messages=True, send_polls=True,
                               read_messages=getattr(wer, "id", 0) in self.leser)


@contextlib.contextmanager
def _aufraeum(*kanaele):
    """Diese Kanaele sind Aufraeum-Kanaele (guildcfg autodelete_channels) - wie
    ab Werk auf dem Hauptserver der Ansagen-Kanal."""
    echt = guildcfg.get

    def get(gid, key):
        if key == "autodelete_channels":
            return list(kanaele)
        return echt(gid, key)
    with mock.patch.object(guildcfg, "get", get):
        yield


def _client(*server):
    async def fetch_user(uid):
        for s in server:
            if uid in s.mitglieder:
                return s.mitglieder[uid]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "weg")
    return SimpleNamespace(guilds=list(server), _event_channel=lambda g: g.kanal,
                           fetch_user=fetch_user, get_channel=lambda _c: None)


def _nachricht(text, server, uid=5, name="Tester", *, mentions=(), rechte=None):
    """Eine Chat-Nachricht an Flo; was Flo antwortet, steht in msg.gesendet."""
    gesendet = []

    async def antworten(inhalt=None, **kw):
        gesendet.append((inhalt, kw))
        return SimpleNamespace(id=1)
    autor = SimpleNamespace(id=uid, bot=False, display_name=name, mention=f"<@{uid}>",
                            guild_permissions=rechte or SimpleNamespace(
                                manage_messages=False, administrator=False))
    msg = SimpleNamespace(content=text, guild=server, mentions=list(mentions),
                          reply=antworten, author=autor, reference=None, id=4711,
                          channel=SimpleNamespace(id=5, send=antworten))
    msg.gesendet = gesendet
    return msg


def admin_id():
    import admin
    return admin.OWNER_ID


# =============================================================================
# Geburtstage
# =============================================================================
@contextlib.contextmanager
def _gb_umgebung(daten=None, *, coins=None, rollen_recht=True):
    """geburtstage frisch, economy mit Attrappe, ein Server im Client, KI aus.
    Liefert (kern, server, client, log)."""
    zurueck = _with_economy(coins or {})
    server = _Server(rollen_recht=rollen_recht)
    client = _client(server)
    try:
        with _frisch(geburtstage, "geburtstage.json", daten) as (kern, zeilen), \
                mock.patch.object(ai, "is_enabled", lambda: False), \
                mock.patch.object(geburtstage.Geburtstage, "_kuerzlich_notlage",
                                  staticmethod(lambda _uid: False)):
            yield kern, server, client, zeilen
    finally:
        zurueck()


def _gb_daten(*leute, gid=77, **extra):
    """leute: (uid, tag, monat, seit[, name])"""
    g = {"leute": {str(e[0]): {"t": e[1], "m": e[2], "seit": e[3],
                               "name": e[4] if len(e) > 4 else f"Person{e[0]}"}
                   for e in leute},
         "geaendert": {str(e[0]): "" for e in leute}}
    g.update(extra)
    return {"guilds": {str(gid): g}}


def _gb_ticken(client):
    asyncio.run(geburtstage.tick(client))


def _gb(text, server, uid=5, **kw):
    return asyncio.run(geburtstage.handle(_nachricht(text, server, uid, **kw)))


def test_geburtstag_formate_und_grammatik():
    """'12.03.', '12.3', '12. März', '29.02.', ein Jahr dahinter (wird ignoriert),
    'am'/'ist am' davor. Was es nicht gibt, bekommt eine Abfuhr; was kein
    Befehl ist, geht an die KI (None)."""
    b = geburtstage.Geburtstage.befehl
    for text, erwartet in (
            ("geburtstag 12.03.", ("setzen", 12, 3, False)),
            ("geburtstag 12.3", ("setzen", 12, 3, False)),
            ("Geburtstag 12.3.", ("setzen", 12, 3, False)),
            ("geburtstag 12. 03.", ("setzen", 12, 3, False)),
            ("geburtstag 12. März", ("setzen", 12, 3, False)),
            ("geburtstag 12 maerz", ("setzen", 12, 3, False)),
            ("geburtstag 12.mär", ("setzen", 12, 3, False)),
            ("geburtstag 1. Mai!", ("setzen", 1, 5, False)),
            ("bday 3. sept", ("setzen", 3, 9, False)),
            ("birthday 24. dez.", ("setzen", 24, 12, False)),
            ("geburtstag 29.02.", ("setzen", 29, 2, False)),
            ("geburtstag 12.03.1990", ("setzen", 12, 3, True)),
            ("geburtstag 12.03.90", ("setzen", 12, 3, True)),
            ("geburtstag 12. März 1990", ("setzen", 12, 3, True)),
            ("geburtstag am 12.03.", ("setzen", 12, 3, False)),
            ("geburtstag ist am 7.7.", ("setzen", 7, 7, False)),
            ("geburtstag", ("zeigen",)),
            ("geburtstag?", ("zeigen",)),
            ("geburtstag wann", ("zeigen",)),
            ("geburtstag löschen", ("loeschen",)),
            ("geburtstag weg", ("loeschen",)),
            ("geburtstage", ("liste",)),
            ("geburtstage hier", ("liste",)),
            ("geburtstag liste", ("liste",)),
            ("geburtstag 31.02.", ("ungueltig", "31.02.")),
            ("geburtstag 32.1.", ("ungueltig", "32.1.")),
            ("geburtstag 12.13.", ("ungueltig", "12.13.")),
            ("geburtstag 31. april", ("ungueltig", "31. april"))):
        assert b(text) == erwartet, (text, b(text))
    for satz in ("geburtstag ist doof", "geburtstag von meiner mutter ist morgen",
                 "geburtstage sind toll", "geburtstage 12.03.", "geburtstag 12",
                 "geburtstag märz", "geburtstag morgen", "geburtstag 12,03",
                 "geburtstag 12.03. war toll", "geburtstagsparty heute?",
                 "birthday party", "bday war geil", "geburtstag ²³.03.",
                 "geburtstag ١٢.٠٣.", "geburtstag 12.03.19999", "", "hallo flo",
                 # Ein Vorwurf ist kein Auftrag - und eine Frage loescht nichts.
                 "Geburtstag vergessen?", "geburtstag vergessen", "geburtstag vergiss",
                 "geburtstag löschen?", "geburtstag weg?"):
        assert b(satz) is None, satz
    import cmdnorm
    # Ein Vertipper mit Fliesstext dahinter wird KEIN Eintrag (geburtstag tut was).
    for satz in ("geburtstags feier heute", "geburstag ist morgen"):
        assert cmdnorm.normalize(satz) is None, satz


def test_geburtstag_eintragen_zeigen_loeschen():
    wand, mono = [_wann(10, 18, monat=1)], [1000.0]
    with _gb_umgebung() as (kern, server, _c, _l), _uhr_gestellt(wand, mono):
        assert _gb("flo geburtstag", server).startswith("Hab ich nicht.")
        assert _gb("flo geburtstag löschen", server) == geburtstage._NIX_ZU_LOESCHEN
        antwort = _gb("flo geburtstag 12.03.1990", server, name="Anna")
        assert antwort.startswith("Das Jahr will ich gar nicht wissen. Eingetragen: **12. März**.")
        assert "Applaus" in antwort, "61 Tage vorher: das Geschenk ist drin"
        g = kern._guild(77)
        assert g["leute"]["5"] == {"t": 12, "m": 3, "name": "Anna", "seit": "2026-01-10"}
        assert _datei("geburtstage.json")["guilds"]["77"]["leute"]["5"]["t"] == 12, \
            "sofort auf der Platte"
        assert "noch 61 Tage" in _gb("flo geburtstag", server)
        assert _gb("flo geburtstag 12.3", server) == geburtstage._SCHON.format(datum="12. März")
        assert _gb("Flo, geburtstag löschen!", server) == geburtstage._GELOESCHT
        assert "5" not in g["leute"] and "5" in g["geaendert"]
        assert _gb("flo geburtstag", server).startswith("Hab ich nicht.")
        assert _gb("flo geburtstag 30.02.", server) in [
            u.format(datum="30.02.") for u in geburtstage._UNGUELTIG]
        # Heute bzw. morgen
        _stellen(wand, mono, _wann(12, 8))
        assert "zu knapp" in _gb("flo geburtstag 12.03.", server, uid=6)
        assert "– heute." in _gb("flo geburtstag", server, uid=6)
        _gb("flo geburtstag 13.3.", server, uid=7)
        assert "– morgen." in _gb("flo geburtstag", server, uid=7)
        assert "zu knapp" in _gb("flo geburtstag 20.3.", server, uid=8), "unter 14 Tagen"
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "economy"):
            antwort = _gb("flo geburtstag 21.3.", server, uid=9)
        assert antwort.startswith("Eingetragen") and "Coins" not in antwort
        assert "Geschenk" not in antwort, "ohne Wirtschaft kein Wort ueber Geld"
        # Nicht auf dem Server: nichts. Und ohne Modul: nichts.
        dm = _nachricht("flo geburtstag 12.03.", server)
        dm.guild = None
        assert asyncio.run(geburtstage.handle(dm)) is None
        kern._enabled = False
        assert _gb("flo geburtstag 12.03.", server) is None


def test_geburtstag_nie_fuer_andere():
    """Nur fuer sich selbst - eine Erwaehnung dahinter ist eine Abfuhr, und es
    wird nichts gespeichert (auch nicht unter dem eigenen Namen)."""
    wand, mono = [_wann(10, 18, monat=1)], [1000.0]
    andere = SimpleNamespace(id=222, bot=False, display_name="Opfer")
    with _gb_umgebung() as (kern, server, _c, _l), _uhr_gestellt(wand, mono):
        for text in ("flo geburtstag <@222> 12.03.", "flo geburtstag <@!222>",
                     "flo geburtstag löschen <@222>"):
            antwort = _gb(text, server, mentions=[andere])
            assert "Opfer" in antwort and any(
                antwort == v.format(wer="Opfer") for v in geburtstage._FUER_ANDERE), antwort
        assert kern._guild(77)["leute"] == {}
        # Flo selbst anpingen ist Ansprache, kein "fuer andere".
        flo = SimpleNamespace(id=1, bot=True, display_name="Flo")
        assert "Eingetragen" in _gb("<@1> geburtstag 12.03.", server, mentions=[flo])


def test_geburtstag_aenderung_hoechstens_alle_30_tage():
    """Der erste Eintrag ist frei (ein Tippfehler darf gleich korrigiert
    werden); jede Aenderung danach sperrt AENDERN_ABSTAND Tage. Loeschen und
    neu eintragen ist auch eine Aenderung - die Sperre bleibt stehen."""
    wand, mono = [_wann(10, 18, monat=1)], [1000.0]
    with _gb_umgebung() as (kern, server, _c, _l), _uhr_gestellt(wand, mono):
        g = kern._guild(77)
        assert "Eingetragen" in _gb("flo geburtstag 21.03.", server)
        assert "Geändert auf **12. März**" in _gb("flo geburtstag 12.03.", server)
        assert g["geaendert"]["5"] == "2026-01-10"
        assert _gb("flo geburtstag 13.03.", server) == geburtstage._GESPERRT.format(
            ab="09.02.2026")
        assert g["leute"]["5"]["t"] == 12
        # Loeschen und neu: dieselbe Sperre - auch fuer dasselbe Datum.
        _gb("flo geburtstag löschen", server)
        assert "frühestens am 09.02.2026" in _gb("flo geburtstag 13.03.", server)
        assert "frühestens am 09.02.2026" in _gb("flo geburtstag 12.03.", server)
        assert "5" not in g["leute"]
        _stellen(wand, mono, _wann(8, 23, monat=2))
        assert "frühestens" in _gb("flo geburtstag 13.03.", server), "einen Tag zu frueh"
        _stellen(wand, mono, _wann(9, 0, 5, monat=2))
        assert "Geändert auf **13. März**" in _gb("flo geburtstag 13.03.", server)
        assert g["leute"]["5"]["seit"] == "2026-02-09", "der Vorlauf faengt neu an"
        assert g["geaendert"]["5"] == "2026-02-09"
        # Die Sperre steht auf der Platte und ueberlebt einen Neustart.
        assert geburtstage.setup() is True
        assert "frühestens am 11.03.2026" in _gb("flo geburtstag 14.03.", server)
        # Ein kaputter Wert weit in der Zukunft sperrt nicht fuer immer.
        kern._guild(77)["geaendert"]["5"] = "2031-01-01"
        assert "Geändert" in _gb("flo geburtstag 14.03.", server)


def test_geburtstage_liste_die_naechsten_zehn():
    wand, mono = [_wann(10, 18)], [1000.0]
    leute = [(100 + i, 1 + i, 4, "2026-01-01", f"Leute{i}") for i in range(12)]
    leute += [(90, 10, 3, "2026-01-01", "Heute**Kind"), (91, 11, 3, "2026-01-01", "@everyone"),
              (92, 9, 3, "2026-01-01", "Gestern")]
    with _gb_umgebung(_gb_daten(*leute)) as (_k, server, _c, _l), _uhr_gestellt(wand, mono):
        text = _gb("flo geburtstage", server)
        zeilen = text.split("\n")[1:]
        assert len(zeilen) == geburtstage.LISTE_MAX, zeilen
        assert zeilen[0] == "• **10. März** – Heute\\*\\*Kind (heute 🎂)", zeilen[0]
        assert zeilen[1] == "• **11. März** – @\u200beveryone (morgen)", zeilen[1]
        assert zeilen[2] == "• **1. April** – Leute0 (in 22 Tagen)"
        assert "Leute8" not in text and "Gestern" not in text, "erst naechstes Jahr wieder"
    with _gb_umgebung() as (_k, server, _c, _l), _uhr_gestellt(wand, mono):
        assert _gb("flo geburtstage", server) == geburtstage._LISTE_LEER


def test_geburtstag_gratuliert_einmal_mit_coins_auch_nach_uhrspruengen():
    """Am Tag ab 9 Uhr, nur bei stabiler Uhr: ein Ping nur fuer das
    Geburtstagskind, die Coins einmal. Springt die Uhr zurueck (vor 9 Uhr, in
    die Tage davor) und wieder vor, kommt nichts doppelt."""
    import economy
    wand, mono = [_wann(12, 8, 59)], [1000.0]
    daten = _gb_daten((1000, 12, 3, "2026-02-01", "Anna"))
    with _gb_umgebung(daten) as (kern, server, client, _l), _uhr_gestellt(wand, mono):
        server.mitglied(1000, "Anna")
        _gb_ticken(client)
        assert server.kanal.gesendet == [], "vor 9 Uhr nicht"
        _stellen(wand, mono, _wann(12, 9, 0))
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1
        text, kw = server.kanal.gesendet[0]
        assert text.startswith("🎂 <@1000> ") and "Nachträglich" not in text
        assert "2.500 Flo Coins" in text and "Anna" in text
        assert kw["allowed_mentions"].users == [discord.Object(1000)]
        assert not kw["allowed_mentions"].everyone and not kw["allowed_mentions"].roles
        assert economy.instance._profile(1000)["coins"] == 2500
        g = kern._guild(77)
        assert g["gefeiert"] == {"1000": "2026"} and g["offen"] == {}
        assert g["geprueft"] == "2026-03-11"
        for wie, sekunden in ((_stellen, None), (_springt, -7200), (_stellen, 400),
                              (_springt, -86400 * 2), (_stellen, 400),
                              (_springt, 86400 * 2), (_stellen, 400)):
            if wie is _stellen:
                _stellen(wand, mono, wand[0] + (sekunden or 60))
            else:
                wie(wand, mono, sekunden)
            _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1, server.kanal.gesendet
        assert economy.instance._profile(1000)["coins"] == 2500
        # Ein kaputtes 'geprueft' weit in der Zukunft haelt nichts an - und
        # 'gefeiert' haelt trotzdem dicht.
        g["geprueft"] = "2099-01-01"
        _stellen(wand, mono, _wann(12, 9, 30))
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1 and g["geprueft"] == "2026-03-11"


def test_geburtstag_coins_nur_mit_vierzehn_tagen_vorlauf():
    """Coins gelten auf allen Servern - auf Zuruf gibt es sie nicht: wer sein
    Datum weniger als VORLAUF_TAGE vor dem Tag eintraegt, bekommt den
    Glueckwunsch mit einer Bemerkung statt Coins."""
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    daten = _gb_daten((1000, 12, 3, "2026-02-27"), (1001, 12, 3, "2026-02-26"),
                      (1002, 12, 3, "2026-03-12"))
    with _gb_umgebung(daten) as (kern, server, client, _l), _uhr_gestellt(wand, mono):
        for uid in (1000, 1001, 1002):
            server.mitglied(uid)
        _gb_ticken(client)
        texte = {t.split()[1]: t for t, _kw in server.kanal.gesendet}
        assert set(texte) == {"<@1000>", "<@1001>", "<@1002>"}
        for knapp in ("<@1000>", "<@1002>"):
            assert any(z in texte[knapp] for z in geburtstage._ZU_KNAPP), texte[knapp]
            assert "Flo Coins" not in texte[knapp]
        assert "2.500 Flo Coins" in texte["<@1001>"]
        assert economy.instance._profile(1000)["coins"] == 0
        assert economy.instance._profile(1002)["coins"] == 0
        assert economy.instance._profile(1001)["coins"] == 2500
        assert kern._guild(77)["gefeiert"] == {"1000": "2026", "1001": "2026", "1002": "2026"}


def test_geburtstag_einmal_pro_jahr_auch_mit_neuem_datum():
    """Gefeiert (und bezahlt) wird hoechstens einmal je Jahr und Server - ein
    neues Datum spaeter im Jahr, Loeschen und neu Eintragen aendern daran nichts."""
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    daten = _gb_daten((5, 12, 3, "2026-01-01", "Anna"))
    with _gb_umgebung(daten) as (kern, server, client, _l), _uhr_gestellt(wand, mono):
        server.mitglied(5, "Anna")
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1 and economy.instance._profile(5)["coins"] == 2500
        _stellen(wand, mono, _wann(20, 18, monat=4))
        antwort = _gb("flo geburtstag 15.05.", server, name="Anna")
        assert "Geändert" in antwort and "dieses Jahr schon" in antwort
        _gb("flo geburtstag löschen", server)
        _stellen(wand, mono, _wann(21, 18, monat=5))
        assert "Geändert" in _gb("flo geburtstag 22.6.", server), "nach 30 Tagen wieder"
        for tag, monat in ((22, 6), (23, 6)):
            _stellen(wand, mono, _wann(tag, 10, monat=monat))
            _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1 and economy.instance._profile(5)["coins"] == 2500
        assert kern._guild(77)["gefeiert"]["5"] == "2026"
        # Naechstes Jahr wieder - mit Coins (der Vorlauf ist laengst erfuellt).
        _stellen(wand, mono, _wann(22, 10, monat=6, jahr=2027))
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 2
        assert economy.instance._profile(5)["coins"] == 5000
        assert kern._guild(77)["gefeiert"]["5"] == "2027"


def test_geburtstag_holt_hoechstens_zwei_tage_nach():
    """Offline gewesen? Jeder Tag ab 'geprueft' bis heute, aber hoechstens zwei
    zurueck - mit "Nachtraeglich". Ohne 'geprueft' (noch nie durchgekommen)
    dasselbe Fenster."""
    wand, mono = [_wann(12, 10)], [1000.0]
    leute = [(1, 12, 3, "2026-01-01"), (2, 11, 3, "2026-01-01"),
             (3, 10, 3, "2026-01-01"), (4, 9, 3, "2026-01-01")]
    with _gb_umgebung(_gb_daten(*leute, geprueft="2026-03-06")) as (kern, server, client, _l), \
            _uhr_gestellt(wand, mono):
        for uid in range(1, 5):
            server.mitglied(uid)
        _gb_ticken(client)
        texte = {t.split("<@")[1].split(">")[0]: t for t, _kw in server.kanal.gesendet}
        assert set(texte) == {"1", "2", "3"}, texte
        assert texte["1"].startswith("🎂 <@1> ")
        for uid in ("2", "3"):
            assert texte[uid].startswith("🎂 Nachträglich, weil ich offline war: <@")
        assert kern._guild(77)["geprueft"] == "2026-03-11"
    with _gb_umgebung(_gb_daten(*leute)) as (kern, server, client, _l), \
            _uhr_gestellt(wand, mono):
        for uid in range(1, 5):
            server.mitglied(uid)
        _gb_ticken(client)
        assert sorted(t.split("<@")[1].split(">")[0] for t, _kw in server.kanal.gesendet) \
            == ["1", "2", "3"]
        # Ein Tag spaeter ist nichts mehr nachzuholen.
        _stellen(wand, mono, _wann(13, 10))
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 3 and kern._guild(77)["geprueft"] == "2026-03-12"


def test_geburtstag_29_februar():
    """Der 29.02. wird in Nicht-Schaltjahren am 28.02. gefeiert - in
    Schaltjahren am 29.02. (und nicht schon am 28.)."""
    wand, mono = [_wann(28, 10, monat=2, jahr=2027)], [1000.0]
    daten = _gb_daten((5, 29, 2, "2026-01-01"))
    with _gb_umgebung(daten) as (kern, server, client, _l), _uhr_gestellt(wand, mono):
        server.mitglied(5)
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1 and kern._guild(77)["gefeiert"]["5"] == "2027"
        assert geburtstage.naechster(29, 2, datetime(2027, 3, 1).date()).isoformat() \
            == "2028-02-29"
        _stellen(wand, mono, _wann(28, 10, monat=2, jahr=2028))
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1, "2028 ist ein Schaltjahr"
        _stellen(wand, mono, _wann(29, 9, 1, monat=2, jahr=2028))
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 2 and kern._guild(77)["gefeiert"]["5"] == "2028"
        _stellen(wand, mono, _wann(1, 10, monat=2, jahr=2029))
        assert "**29. Februar** – noch 27 Tage" in _gb("flo geburtstag", server)


def test_geburtstag_wartet_auf_uhr_sendepause_und_schalter():
    """Gleich nach dem Start (Uhr nicht stabil), in der Sendepause und mit
    abgeschaltetem Schalter: nichts - verschoben, nicht verloren. Danach kommt
    der Glueckwunsch (am Tag danach als "nachtraeglich")."""
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    daten = _gb_daten((5, 12, 3, "2026-01-01"))
    with _gb_umgebung(daten) as (kern, server, client, _l), _uhr_gestellt(wand, mono):
        server.mitglied(5)
        with mock.patch.object(uhr, "stabil", lambda: False):
            _gb_ticken(client)
        with mock.patch.object(geburtstage, "_sendepause", lambda: True):
            _gb_ticken(client)
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "geburtstage"):
            _gb_ticken(client)
        assert server.kanal.gesendet == [] and kern._guild(77)["gefeiert"] == {}
        _stellen(wand, mono, _wann(13, 9, 30))
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1
        assert server.kanal.gesendet[0][0].startswith("🎂 Nachträglich")
        assert economy.instance._profile(5)["coins"] == 2500


def test_geburtstag_ohne_economy_oder_mit_null_coins_nur_glueckwunsch():
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    daten = _gb_daten((5, 12, 3, "2026-03-11"), (6, 12, 3, "2026-01-01"))
    with _gb_umgebung(daten) as (kern, server, client, _l), _uhr_gestellt(wand, mono), \
            mock.patch.object(features, "is_on_in", lambda _g, k: k != "economy"):
        server.mitglied(5)
        server.mitglied(6)
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 2
        for text, _kw in server.kanal.gesendet:
            assert "Coins" not in text, "ohne Geld auch kein 'zu knapp'"
        assert economy.instance._profile(6)["coins"] == 0


def test_geburtstag_coin_grenzen_gegen_selbstgedruckte_coins():
    """Coins gelten auf allen Servern: ein Admin darf sich auf seinem
    Privatserver kein Geld drucken. guildcfg nimmt hoechstens 10.000 (Geburtstag)
    bzw. 25.000 (DJ des Monats) - und geburtstage deckelt auch einen Wert, der an
    guildcfg vorbei in der Datei steht."""
    import economy
    cfg, zurueck = _cfg_frisch()
    try:
        for key, zu_viel, genug in (("geburtstag_coins", "10001", "10000"),
                                    ("geburtstag_coins", "1000000", "0"),
                                    ("dj_belohnung", "25001", "25000"),
                                    ("dj_belohnung", "1000000", "5000")):
            ok, _w, fehler = asyncio.run(cfg.instance.setzen(77, key, zu_viel))
            assert not ok and "Höchstens" in fehler, (key, fehler)
            ok, wert, _f = asyncio.run(cfg.instance.setzen(77, key, genug))
            assert ok and wert == int(genug)
        # Von Hand in die Datei geschrieben: gilt nicht, der Standard greift.
        cfg.instance._store.data["guilds"]["78"] = {"geburtstag_coins": 1_000_000,
                                                    "dj_belohnung": 1_000_000}
        with _Logfang("dcbot.guildcfg"):
            assert cfg.get(78, "geburtstag_coins") == 2500
            assert cfg.get(78, "dj_belohnung") == 5000
    finally:
        zurueck()
    wand, mono = [_wann(12, 10)], [1000.0]
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01"))) as (_k, server, client, _l), \
            _uhr_gestellt(wand, mono), \
            mock.patch.object(guildcfg, "get",
                              lambda _g, k: 50_000 if k == "geburtstag_coins" else 0):
        server.mitglied(5)
        _gb_ticken(client)
        assert economy.instance._profile(5)["coins"] == geburtstage.COINS_MAX == 10_000


def test_geburtstag_bezahlt_erst_nach_dem_festhalten_und_nie_halb():
    """Lock-before-pay: 'gefeiert' steht auf der Platte, BEVOR die Coins
    fliessen. Bricht der Takt mittendrin ab (ALLTAG_FRIST), wird trotzdem zu
    Ende bezahlt - und der naechste Takt zahlt nicht noch einmal, holt aber den
    Glueckwunsch nach."""
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01"))) as (kern, server, client, _l), \
            _uhr_gestellt(wand, mono):
        server.mitglied(5)
        pfad = store.DATA_DIR / "geburtstage.json"
        gesehen, frei = [], []
        echt = kern._store.save

        async def langsam():
            gesehen.append(((json.loads(pfad.read_text(encoding="utf-8"))["guilds"]["77"]
                             .get("gefeiert") if pfad.exists() else None),
                            economy.instance._profile(5)["coins"]))
            ok = await echt()
            await frei[0].wait()
            return ok

        async def lauf():
            frei.append(asyncio.Event())
            with mock.patch.object(kern._store, "save", langsam):
                takt = asyncio.ensure_future(geburtstage.tick(client))
                for _ in range(20):
                    await asyncio.sleep(0)
                takt.cancel()
                frei[0].set()
                with contextlib.suppress(asyncio.CancelledError):
                    await takt
                for _ in range(500):
                    if kern._guild(77)["offen"].get("5", {}).get("bezahlt"):
                        break
                    await asyncio.sleep(0.01)
        asyncio.run(lauf())
        assert gesehen[0] == (None, 0), gesehen
        assert _datei("geburtstage.json")["guilds"]["77"]["gefeiert"] == {"5": "2026"}
        assert economy.instance._profile(5)["coins"] == 2500
        assert server.kanal.gesendet == []
        _gb_ticken(client)
        assert economy.instance._profile(5)["coins"] == 2500
        assert len(server.kanal.gesendet) == 1 and "2.500 Flo Coins" in server.kanal.gesendet[0][0]
        assert kern._guild(77)["offen"] == {}


def test_geburtstag_abgelehnter_glueckwunsch_kommt_nach():
    """Lehnt Discord den Post ab, bleibt er offen und kommt nach NOCHMAL_NACH -
    ohne noch einmal zu zahlen. Nach VERSUCHEN oder ausserhalb des Nachhol-
    Fensters gibt Flo auf. Wer nicht mehr auf dem Server ist, bekommt nichts."""
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    uhr_mono = [500.0]
    daten = _gb_daten((5, 12, 3, "2026-01-01"), (6, 12, 3, "2026-01-01"))
    with _gb_umgebung(daten) as (kern, server, client, zeilen), _uhr_gestellt(wand, mono), \
            mock.patch.object(geburtstage, "_mono", lambda: uhr_mono[0]):
        server.mitglied(5)                       # 6 hat den Server verlassen
        server.kanal.kaputt = 1
        _gb_ticken(client)
        g = kern._guild(77)
        assert server.kanal.gesendet == [] and g["offen"]["5"]["versuche"] == 1
        assert "6" not in g["offen"] and g["gefeiert"]["6"] == "2026"
        assert economy.instance._profile(6)["coins"] == 0
        # ... und steht nicht mehr in der Liste; 'gefeiert' und die Sperre bleiben.
        assert "6" not in g["leute"] and g["geaendert"]["6"] == ""
        assert "Person6" not in _gb("flo geburtstage", server)
        assert "Person5" in _gb("flo geburtstage", server)
        _gb_ticken(client)
        assert server.kanal.gesendet == [], "Pause zwischen zwei Versuchen"
        uhr_mono[0] += geburtstage.NOCHMAL_NACH + 1
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1 and g["offen"] == {}
        assert economy.instance._profile(5)["coins"] == 2500
        # Kein Kanal / Discord lehnt immer ab: nach VERSUCHE ist Schluss.
        g["offen"]["5"] = {"tag": "2026-03-12", "betrag": 0, "bezahlt": True, "grund": "",
                           "versuche": geburtstage.VERSUCHE}
        _gb_ticken(client)
        assert g["offen"] == {} and len(server.kanal.gesendet) == 1
        g["offen"]["5"] = {"tag": "2026-03-01", "betrag": 0, "bezahlt": True, "grund": "",
                           "versuche": 0}
        _gb_ticken(client)
        assert g["offen"] == {}, "ausserhalb des Nachhol-Fensters"
        # Discord wackelt beim Mitglied: weder gefeiert noch bezahlt - spaeter.
        g["leute"]["7"] = {"t": 12, "m": 3, "seit": "2026-01-01", "name": "x"}
        server.mitglied(7)
        server.holen_kaputt.add("holen")
        _gb_ticken(client)
        assert "7" not in g["gefeiert"]
        server.holen_kaputt.clear()
        _gb_ticken(client)
        assert g["gefeiert"]["7"] == "2026" and len(server.kanal.gesendet) == 2


def test_geburtstag_rolle_an_und_ab_auch_ueber_neustart():
    """Mit 'geburtstag_rolle' und "Rollen verwalten": die Rolle fuer den Tag,
    am naechsten Tag wieder weg (per fetch_member, auch vor 9 Uhr und nach einem
    Neustart). Ohne Recht oder Schalter: keine Rolle."""
    wand, mono = [_wann(12, 10)], [1000.0]
    schalter = {"geburtstag_rolle": True}
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01"))) as (kern, server, client, zeilen), \
            _uhr_gestellt(wand, mono), \
            mock.patch.object(guildcfg, "an", lambda _g, k: schalter.get(k, False)):
        person = server.mitglied(5)
        _gb_ticken(client)
        assert server.protokoll == [("rolle_neu", geburtstage.ROLLE), ("geben", 5)]
        assert [r.name for r in person.roles] == [geburtstage.ROLLE]
        g = kern._guild(77)
        assert g["rolle_bis"] == {"5": "2026-03-13"} and g["rolle_id"] == 555
        asyncio.run(kern._store.flush_now())
        # Neustart: alles nur noch von der Platte.
        assert geburtstage.setup() is True
        _stellen(wand, mono, _wann(13, 0, 5))
        _gb_ticken(client)
        assert person.roles == [] and server.protokoll[-1] == ("nehmen", 5)
        assert kern._guild(77)["rolle_bis"] == {}
    # Ohne "Rollen verwalten": nichts angelegt, nichts vergeben, ein Hinweis im Log.
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01")), rollen_recht=False) as \
            (kern, server, client, zeilen), _uhr_gestellt([_wann(12, 10)], [1000.0]), \
            mock.patch.object(guildcfg, "an", lambda _g, k: True):
        server.mitglied(5)
        _gb_ticken(client)
        assert server.protokoll == [] and len(server.kanal.gesendet) == 1
        assert any("Rollen verwalten" in z for z in zeilen)
    # Schalter aus (Standard ausserhalb des Hauptservers): keine Rolle.
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01"))) as (kern, server, client, _z), \
            _uhr_gestellt([_wann(12, 10)], [1000.0]):
        server.mitglied(5)
        _gb_ticken(client)
        assert server.protokoll == [] and kern._guild(77)["rolle_bis"] == {}


def test_geburtstag_rolle_abnehmen_klemmt():
    """Nimmt Discord die Rolle nicht (Recht, Rangfolge), bleibt der Eintrag
    stehen und es wird nach ROLLE_NOCHMAL_NACH nochmal versucht; nach
    ROLLE_AUFGEBEN_TAGE gibt Flo auf. Wer weg ist, ist erledigt."""
    wand, mono = [_wann(13, 1)], [1000.0]
    uhr_mono = [500.0]
    rolle = SimpleNamespace(id=555, name=geburtstage.ROLLE)
    daten = _gb_daten(rolle_id=555, rolle_bis={"5": "2026-03-13", "6": "2026-03-12"})
    with _gb_umgebung(daten) as (kern, server, client, _z), _uhr_gestellt(wand, mono), \
            mock.patch.object(geburtstage, "_mono", lambda: uhr_mono[0]):
        server.roles.append(rolle)
        person = server.mitglied(5, None, rolle)
        server.holen_kaputt.add("nehmen")
        _gb_ticken(client)
        g = kern._guild(77)
        assert g["rolle_bis"] == {"5": "2026-03-13"}, "6 ist weg, 5 klemmt"
        server.holen_kaputt.clear()
        _gb_ticken(client)
        assert person.roles == [rolle], "Pause"
        uhr_mono[0] += geburtstage.ROLLE_NOCHMAL_NACH + 1
        _gb_ticken(client)
        assert person.roles == [] and g["rolle_bis"] == {}
        # Klemmt es eine Woche lang, gibt Flo auf.
        g["rolle_bis"]["5"] = "2026-03-01"
        person.roles.append(rolle)
        server.holen_kaputt.add("nehmen")
        uhr_mono[0] += geburtstage.ROLLE_NOCHMAL_NACH + 1
        _gb_ticken(client)
        assert g["rolle_bis"] == {}


def test_geburtstag_ki_zeile_hintergrund_mit_grenze_und_ersatz():
    """Die Zeile kommt von der KI - als Hintergrund-Aufruf, mit der Grenze im
    System-Prompt, im Kontext des Servers. Verweigerung, leere Antwort, 'ki'
    aus: der Ersatz-Pool. Die Coins stehen nie in der KI-Zeile, sondern dahinter."""
    wand, mono = [_wann(12, 10)], [1000.0]
    aufrufe, antworten = [], []

    async def generate(prompt, *, system=None, temperature=0.8, max_tokens=300,
                       hintergrund=False):
        aufrufe.append({"prompt": prompt, "system": system, "hintergrund": hintergrund,
                        "gid": ai.aktuelle_guild()})
        return antworten.pop(0) if antworten else None

    daten = _gb_daten((5, 12, 3, "2026-01-01"), (6, 12, 3, "2026-01-01"))
    with _gb_umgebung(daten) as (kern, server, client, _z), _uhr_gestellt(wand, mono), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        server.mitglied(5, "Anna")
        server.mitglied(6, "Bob")
        antworten.extend(["Anna wird alt, und keiner merkt's. Glückwunsch trotzdem.",
                          "I'm sorry, but I can't help with that."])
        _gb_ticken(client)
        assert len(aufrufe) == 2
        for a in aufrufe:
            assert a["hintergrund"] is True and a["gid"] == 77
            assert ai.FloAI._GUARDRAIL in a["system"] and "GENAU ZWEI" in a["system"]
        assert "Anna" in aufrufe[0]["prompt"] and "Bob" in aufrufe[1]["prompt"]
        anna, bob = (t for t, _kw in server.kanal.gesendet)
        assert anna.startswith("🎂 <@5> Anna wird alt, und keiner merkt's. Glückwunsch trotzdem. ")
        assert any(anna.endswith(c.format(n="2.500")) for c in geburtstage._COINS)
        assert "sorry" not in bob and any(
            e.format(name="Bob") in bob for e in geburtstage._ERSATZ), bob
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01"))) as (kern, server, client, _z), \
            _uhr_gestellt(wand, mono), mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate), \
            mock.patch.object(features, "is_on_in", lambda _g, k: k != "ki"):
        aufrufe.clear()
        server.mitglied(5, "Anna")
        _gb_ticken(client)
        assert aufrufe == [] and len(server.kanal.gesendet) == 1


def test_geburtstag_reagiert_nie_auf_notsignale():
    """Ein Name mit Notsignal geht nicht an die KI (und steht nicht im Post),
    eine KI-Zeile mit Notsignal fliegt raus - und wer gerade ein Notsignal
    geschrieben hat (nachhaken), bekommt einen Glueckwunsch ohne Spott und ohne
    KI."""
    wand, mono = [_wann(12, 10)], [1000.0]
    aufrufe = []

    async def generate(prompt, **kw):
        aufrufe.append(prompt)
        return "Ich will sterben, so alt bist du schon."

    assert ai.FloAI._NOTLAGE_RE.search("will nicht mehr leben")
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01"))) as (kern, server, client, _z), \
            _uhr_gestellt(wand, mono), mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        server.mitglied(5, "will nicht mehr leben")
        _gb_ticken(client)
        assert len(aufrufe) == 1 and "leben" not in aufrufe[0] and "'du'" in aufrufe[0]
        text = server.kanal.gesendet[0][0]
        assert not ai.FloAI._NOTLAGE_RE.search(text), text
        assert any(e.format(name=geburtstage.OHNE_NAME) in text for e in geburtstage._ERSATZ)
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-02-27"), (6, 12, 3, "2026-01-01"))) as \
            (kern, server, client, _z), _uhr_gestellt(wand, mono), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate), \
            mock.patch.object(geburtstage.Geburtstage, "_kuerzlich_notlage",
                              staticmethod(lambda uid: uid in (5, 6))):
        aufrufe.clear()
        server.mitglied(5, "Anna")
        server.mitglied(6, "Bob")
        _gb_ticken(client)
        assert aufrufe == []
        anna, bob = (t for t, _kw in server.kanal.gesendet)
        assert any(s.format(name="Anna") in anna for s in geburtstage._SANFT)
        assert not any(z in anna for z in geburtstage._ZU_KNAPP), "kein Spott"
        assert bob.endswith("Dazu 2.500 Flo Coins.") and "Würde" not in bob
    # Die echte Abfrage bei nachhaken (nur im Speicher).
    import nachhaken
    with mock.patch.object(nachhaken, "is_enabled", lambda: True), \
            mock.patch.object(nachhaken.instance, "_gesperrt", lambda uid: uid == 5):
        assert geburtstage.Geburtstage._kuerzlich_notlage(5)
        assert not geburtstage.Geburtstage._kuerzlich_notlage(6)
    with mock.patch.object(nachhaken, "is_enabled", lambda: False):
        assert not geburtstage.Geburtstage._kuerzlich_notlage(5)


def test_geburtstag_ueber_bot_kette():
    """'flo geburtstag 12.03.' kommt durch die echte Kette in bot.py; ein Satz
    ueber Geburtstage geht weiter (an die KI bzw. niemanden)."""
    import bot
    from test_fundament import _menschen_nachricht, _nur_geladen
    wand, mono = [_wann(10, 18, monat=1)], [1000.0]
    with _gb_umgebung() as (kern, _s, _c, _z), _uhr_gestellt(wand, mono), \
            _nur_geladen(bot, "GEBURTSTAGE_ENABLED"):
        msg = _menschen_nachricht("flo geburtstag 12.03.")
        asyncio.run(bot.client.on_message(msg))
        texte = [c for c, _e, _v in msg.channel.gesendet]
        assert texte and "Eingetragen: **12. März**" in texte[0], texte
        assert kern.eintrag(77, msg.author.id) == (12, 3)
        msg = _menschen_nachricht("flo geburtstag von meiner oma ist morgen", mid=702)
        asyncio.run(bot.client.on_message(msg))
        assert not msg.channel.gesendet
        with mock.patch.object(bot.features, "fuer", lambda _g: (lambda k: k != "geburtstage")):
            msg = _menschen_nachricht("flo geburtstage", mid=703)
            asyncio.run(bot.client.on_message(msg))
            assert not msg.channel.gesendet


def test_notsignal_sperre_auch_ohne_ki():
    """Wer gerade ein Notsignal geschrieben hat, wird von Zitat der Woche,
    Geburtstagen und Wrapped nicht vorgefuehrt. Das wusste bisher nur
    nachhaken - und das laedt nur mit KI. Ohne KI griffen Zitate und
    Geburtstage dann zum Spott-Pool ('Wieder ein Jahr aelter und immer noch nix
    erreicht', mit Ping). Jetzt merkt es sich bot.py in basis, egal welche
    Module laufen - nur die ID und wann, nie den Text, nur im Speicher."""
    import basis
    import bot
    import nachhaken
    from test_fundament import _menschen_nachricht, _nur_geladen
    try:
        with _nur_geladen(bot, "GEBURTSTAGE_ENABLED"), \
                mock.patch.object(nachhaken, "is_enabled", lambda: False):
            msg = _menschen_nachricht("hallo zusammen", mid=711)
            asyncio.run(bot.client.on_message(msg))
            uid = msg.author.id
            assert not basis.kuerzlich_notlage(uid)
            asyncio.run(bot.client.on_message(
                _menschen_nachricht("ich will nicht mehr leben", mid=712)))
            assert basis.kuerzlich_notlage(uid), "ohne KI weiss es jetzt trotzdem jemand"
            assert geburtstage.Geburtstage._kuerzlich_notlage(uid)
            assert zitate._kuerzlich_notlage(uid) and not basis.kuerzlich_notlage(uid + 1)
            assert list(basis._notlage_seit) == [uid], "nur die ID, kein Text"
            # Nach 72 Stunden ist es vorbei.
            basis._notlage_seit[uid] -= basis.NOTLAGE_SPERRE + 1
            assert not basis.kuerzlich_notlage(uid)
    finally:
        basis._notlage_seit.clear()


def test_geburtstag_zahlt_nicht_ohne_lock_auf_der_platte():
    """Lock-before-pay heisst: der Lock steht WIRKLICH auf der Platte. Ist
    geburtstage.json nicht schreibbar (ein Docker-Lauf hat sie als root
    angelegt), economy.json aber schon, zahlte sonst jeder Neustart im
    Nachhol-Fenster noch einmal. Dann gibt es lieber keine Coins - gratuliert
    wird trotzdem, und das Kassenbuch bleibt frei."""
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01", "Anna"))) as \
            (kern, server, client, zeilen), _uhr_gestellt(wand, mono):
        server.mitglied(5, "Anna")

        async def nicht_schreibbar():
            return False
        kern._store.save = nicht_schreibbar
        _gb_ticken(client)
        assert economy.instance._profile(5)["coins"] == 0
        assert kern._store.data["bezahlt"] == {}
        (text, _kw), = server.kanal.gesendet
        assert "<@5>" in text and "Flo Coins" not in text, text
        assert any("nicht schreibbar" in z for z in zeilen), zeilen


def test_geburtstag_coins_einmal_im_jahr_ueber_alle_server():
    """Coins gelten auf allen Servern - also gibt es sie auch nur einmal im Jahr,
    egal auf wie vielen Servern jemand gefeiert wird (sonst druckte sich jeder
    mit ein paar Privatservern Geld). Gratuliert wird trotzdem ueberall."""
    import economy
    wand, mono = [_wann(12, 10)], [1000.0]
    daten = _gb_daten((5, 12, 3, "2026-01-01", "Anna"))
    daten["guilds"]["78"] = _gb_daten((5, 12, 3, "2026-01-01", "Anna"), gid=78)["guilds"]["78"]
    with _gb_umgebung(daten) as (kern, server, client, _z), _uhr_gestellt(wand, mono):
        zweiter, dritter = _Server(78, kanal=_Kanal(610)), _Server(79, kanal=_Kanal(620))
        client.guilds.extend([zweiter, dritter])
        for s in (server, zweiter, dritter):
            s.mitglied(5, "Anna")
        _gb_ticken(client)
        eins, zwei = server.kanal.gesendet, zweiter.kanal.gesendet
        assert len(eins) == 1 and len(zwei) == 1, "gratuliert wird auf beiden"
        assert "2.500 Flo Coins" in eins[0][0] and "Flo Coins" not in zwei[0][0]
        assert zwei[0][0].endswith(geburtstage._ANDERSWO), zwei[0][0]
        assert economy.instance._profile(5)["coins"] == 2500
        assert kern._store.data["bezahlt"] == {"5": "2026"}
        assert kern._guild(78)["gefeiert"] == {"5": "2026"}
        assert _datei("geburtstage.json")["bezahlt"] == {"5": "2026"}
        # Ein anderes Datum auf dem dritten Server hilft auch nicht.
        antwort = _gb("flo geburtstag 20.6.", dritter, uid=5, name="Anna")
        assert "Eingetragen" in antwort and "anderen Server kassiert" in antwort, antwort
        _stellen(wand, mono, _wann(20, 10, monat=6))
        _gb_ticken(client)
        assert len(dritter.kanal.gesendet) == 1
        assert geburtstage._ANDERSWO in dritter.kanal.gesendet[0][0]
        assert economy.instance._profile(5)["coins"] == 2500
        # Naechstes Jahr wieder genau einmal.
        _stellen(wand, mono, _wann(12, 10, jahr=2027))
        _gb_ticken(client)
        assert economy.instance._profile(5)["coins"] == 5000
        assert kern._store.data["bezahlt"] == {"5": "2027"}
        assert sum("Flo Coins" in t for s in (server, zweiter)
                   for t, _kw in s.kanal.gesendet) == 2
    # Ohne Coins auf dem einen Server (zu knapp) bleibt das Kassenbuch frei.
    daten = _gb_daten((6, 12, 3, "2026-03-01"))
    daten["guilds"]["78"] = _gb_daten((6, 12, 3, "2026-01-01"), gid=78)["guilds"]["78"]
    with _gb_umgebung(daten) as (kern, server, client, _z), \
            _uhr_gestellt([_wann(12, 10)], [1000.0]):
        zweiter = _Server(78, kanal=_Kanal(610))
        client.guilds.append(zweiter)
        for s in (server, zweiter):
            s.mitglied(6)
        _gb_ticken(client)
        assert any(z in server.kanal.gesendet[0][0] for z in geburtstage._ZU_KNAPP)
        assert "2.500 Flo Coins" in zweiter.kanal.gesendet[0][0]
        assert economy.instance._profile(6)["coins"] == 2500


def test_geburtstag_nie_in_aufraeum_kanaele():
    """Ist der Ansagen-Kanal ein Aufraeum-Kanal (ab Werk auf dem Hauptserver),
    waere der Glueckwunsch samt Ping nach einer Minute weg - dann der
    System-Kanal; gibt es keinen, wartet der Glueckwunsch (und steht im Log).
    Auch ein eingestellter geburtstag_channel, der aufraeumt, zaehlt nicht."""
    wand, mono = [_wann(12, 10)], [1000.0]
    uhr_mono = [500.0]
    with _gb_umgebung(_gb_daten((5, 12, 3, "2026-01-01"))) as (kern, server, client, zeilen), \
            _uhr_gestellt(wand, mono), _aufraeum(600), \
            mock.patch.object(geburtstage, "_mono", lambda: uhr_mono[0]):
        server.mitglied(5)
        _gb_ticken(client)
        g = kern._guild(77)
        assert server.kanal.gesendet == [] and g["offen"]["5"]["versuche"] == 1
        assert g["gefeiert"] == {"5": "2026"}, "bezahlt ist bezahlt - nur der Post wartet"
        assert any("Aufraeum-Kanal" in z for z in zeilen)
        server.system_channel = _Kanal(601)
        uhr_mono[0] += geburtstage.NOCHMAL_NACH + 1
        _gb_ticken(client)
        assert len(server.system_channel.gesendet) == 1 and server.kanal.gesendet == []
        assert server.system_channel.gesendet[0][0].startswith("🎂 <@5> ")
    echt = guildcfg.get
    with mock.patch.object(guildcfg, "get",
                           lambda gid, key: 600 if key == "geburtstag_channel"
                           else echt(gid, key)):
        server = _Server()
        server.system_channel = _Kanal(601)
        assert geburtstage.Geburtstage._kanal(None, server) is server.kanal
        with _aufraeum(600):
            assert geburtstage.Geburtstage._kanal(None, server) is server.system_channel
            assert geburtstage.Geburtstage._kanal(_client(server), server) \
                is server.system_channel


def test_geburtstag_erster_eintrag_ist_kein_nachtraeglich():
    """Der erste Eintrag auf einem Server: bis gestern ist nichts nachzuholen -
    wer gestern Geburtstag hatte, bekommt kein "nachtraeglich, weil ich offline
    war", Flo war ja nicht offline. Wer heute hat, wird heute gefeiert."""
    wand, mono = [_wann(12, 10)], [1000.0]
    with _gb_umgebung() as (kern, server, client, _z), _uhr_gestellt(wand, mono):
        server.mitglied(5)
        server.mitglied(6)
        _gb("flo geburtstag 11.03.", server, uid=5)
        assert kern._guild(77)["geprueft"] == "2026-03-11"
        _gb_ticken(client)
        assert server.kanal.gesendet == [], "gestern - und Flo war nicht offline"
        _gb("flo geburtstag 12.03.", server, uid=6)
        _gb_ticken(client)
        assert len(server.kanal.gesendet) == 1
        assert server.kanal.gesendet[0][0].startswith("🎂 <@6> ")
    # Gleich nach dem Start (Uhr noch nicht verlaesslich): kein Stempel.
    with _gb_umgebung() as (kern, server, client, _z), _uhr_gestellt(wand, mono), \
            mock.patch.object(uhr, "stabil", lambda: False):
        _gb("flo geburtstag 11.03.", server, uid=5)
        assert kern._guild(77)["geprueft"] == ""
    # Ein Server mit Eintraegen, auf dem der Takt nie durchkam: das volle Fenster.
    with _gb_umgebung(_gb_daten((7, 11, 3, "2026-01-01"))) as (kern, server, client, _z), \
            _uhr_gestellt(wand, mono):
        server.mitglied(7)
        _gb("flo geburtstag 1.8.", server, uid=5)
        assert kern._guild(77)["geprueft"] == ""
        _gb_ticken(client)
        assert server.kanal.gesendet[0][0].startswith("🎂 Nachträglich")


# =============================================================================
# Zitat der Woche
# =============================================================================
#: Mittwoch der ISO-Woche 2026-W43, Sonntag derselben Woche ist der 25.10.
_MITTWOCH = (21, 12)


def _zwann(tag, stunde, minute=0, monat=10, jahr=2026):
    return _wann(tag, stunde, minute, monat=monat, jahr=jahr)


@contextlib.contextmanager
def _zt_umgebung(daten=None, *, umfragen=True):
    """zitate frisch, ein Server im Client (laufzeit.client), KI aus, Karte und
    Profilbild gestubbt. Liefert (kern, server, client, karten)."""
    server = _Server(kanal=_Kanal(umfragen=umfragen))
    client = _client(server)
    karten = []

    def karte(avatar, text, name):
        karten.append((avatar, text, name))
        return io.BytesIO(b"png")

    async def avatar(_person):
        return b"avatar"

    import media
    import render
    with _frisch(zitate, "zitate.json", daten) as (kern, _zeilen), \
            mock.patch.object(ai, "is_enabled", lambda: False), \
            mock.patch.object(laufzeit, "client", client), \
            mock.patch.object(render, "quote_card", karte), \
            mock.patch.object(media.instance, "_fetch_avatar", avatar):
        yield kern, server, client, karten


def _zitat_msg(mid, text, uid, name=None, *, bot=False, server=None, protokoll=None):
    """Eine Nachricht, die jemand als Zitat speichern will."""
    async def reagieren(emoji):
        msg.reaktionen.append(("reaktion", emoji))
    msg = SimpleNamespace(id=mid, content=text, guild=server, channel=SimpleNamespace(id=5),
                          author=SimpleNamespace(id=uid, bot=bot,
                                                 display_name=name or f"Person{uid}"),
                          add_reaction=reagieren)
    msg.reaktionen = protokoll if protokoll is not None else []
    return msg


def _kandidat(mid, uid, text=None, name=None, von=1, ts=None):
    return {"mid": mid, "cid": 5, "uid": uid, "name": name or f"Person{uid}",
            "text": text or f"Zitat Nummer {mid}", "von": von, "von_name": "Sammler",
            "ts": float(ts if ts is not None else mid)}


def _zt_daten(wochen, gid=77, **extra):
    g = {"wochen": wochen}
    g.update(extra)
    return {"guilds": {str(gid): g}}


def _zt_ticken(client):
    asyncio.run(zitate.tick(client))


def _zt(text, server, uid=5, **kw):
    return asyncio.run(zitate.handle(_nachricht(text, server, uid, **kw)))


def _speichern_per_klick(saver, msg, gid=77):
    klick = _Klick(uid=saver, gid=gid)
    msg.reaktionen = klick.protokoll
    asyncio.run(zitate.rechtsklick(klick, msg))
    return klick


def _ergebnis_msg(server, mid, *, sieger=None, text="", stimmen=0, gesamt=0, frage=None):
    felder = [SimpleNamespace(name="poll_question_text", value=frage or zitate.FRAGE),
              SimpleNamespace(name="total_votes", value=str(gesamt))]
    if sieger is not None:
        felder += [SimpleNamespace(name="victor_answer_id", value=str(sieger)),
                   SimpleNamespace(name="victor_answer_text", value=text),
                   SimpleNamespace(name="victor_answer_votes", value=str(stimmen))]
    return SimpleNamespace(id=9000 + mid, guild=server, channel=server.kanal,
                           reference=SimpleNamespace(message_id=mid),
                           embeds=[SimpleNamespace(fields=felder)],
                           type=discord.MessageType.poll_result)


def _umfrage_laeuft(kern, server, client, wand, mono, kandidaten):
    """Eine laufende Umfrage mit 'kandidaten' (Liste von _kandidat) - ueber den
    echten Sonntags-Takt. Liefert die Umfrage-Nachricht (mid)."""
    kern._guild(77)["wochen"]["2026-W43"] = list(kandidaten)
    _stellen(wand, mono, _zwann(25, 12, 1))
    _zt_ticken(client)
    assert len(server.kanal.gesendet) == 1 and "poll" in server.kanal.gesendet[-1][1]
    return kern._guild(77)["umfrage"]["mid"]


def test_zitat_grammatik_und_was_media_bleibt():
    """Nur die eigenen Formen - 'flo zitat <text>' und die Antwort mit 'flo
    zitat' gehoeren weiter dem Quote-Meme von media."""
    b = zitate.Zitate.befehl
    for text, erwartet in (
            ("zitate", ("liste",)), ("Zitate?", ("liste",)), ("zitate liste", ("liste",)),
            ("zitat der woche", ("sieger",)), ("Zitat der Woche?", ("sieger",)),
            ("zitate der woche", ("sieger",)), ("zitat speichern", ("speichern",)),
            ("zitat merken!", ("speichern",)), ("zitat weg 3", ("weg", 3)),
            ("zitat löschen #2", ("weg", 2)), ("zitate weg 1", ("weg", 1)),
            ("zitat weg", ("weg", None)), ("zitat umfrage jetzt", ("umfrage",))):
        assert b(text) == erwartet, (text, b(text))
    # Jede Form aus der README-Tabelle ("Was sich fuer das Quote-Meme geaendert
    # hat") - mit zitat UND zitate. Kommt eine dazu, gehoert sie dorthin.
    for wort in ("zitat", "zitate"):
        for rest in ("der woche", "dieser woche", "letzte woche", "der letzten woche",
                     "von letzter woche"):
            assert b(f"{wort} {rest}") == ("sieger",), (wort, rest)
        for rest in ("speichern", "speicher", "merken", "merk"):
            assert b(f"{wort} {rest}") == ("speichern",), (wort, rest)
        for rest in ("weg", "raus", "löschen", "loeschen", "entfernen", "delete"):
            assert b(f"{wort} {rest}") == ("weg", None), (wort, rest)
            assert b(f"{wort} {rest} 3") == ("weg", 3), (wort, rest)
        for rest in ("umfrage jetzt", "abstimmung jetzt", "umfrage starten"):
            assert b(f"{wort} {rest}") == ("umfrage",), (wort, rest)
    for rest in ("", "liste", "zeigen", "anzeigen", "kandidaten"):
        assert b(f"zitate {rest}") == ("liste",), rest
        if rest:
            assert b(f"zitat {rest}") is None, rest
    for satz in ("zitat", "zitat Pizza ist Leben", "zitat der woche ist lustig",
                 "zitate von goethe", "zitate sind doof", "zitat weg mit dem scheiß",
                 "zitat weg ²", "zitat weg ٣", "zitat weg 1 2", "zitat speichern bitte",
                 "zitat umfrage", "zitatesammlung", "zitieren", "", "hallo flo"):
        assert b(satz) is None, satz
    import cmdnorm
    import media
    assert cmdnorm.normalize("zitaten von goethe") is None
    # Ein Vertipper vor einem Befehl, der etwas tut, nur mit Argument dahinter.
    assert "zitate" in cmdnorm.ZUSTAND
    assert cmdnorm.normalize("zitae") == "zitate"
    for satz in ("zitae weg mit dem mist", "zitae weg 3", "ziate speichern"):
        assert cmdnorm.normalize(satz) is None, satz
    for text in ("zitat Pizza ist Leben", "zitat", "zitat der woche ist lustig"):
        assert media.Media._QUOTE_RE.match(text), text
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    with _zt_umgebung() as (kern, server, _c, _k), _uhr_gestellt(wand, mono):
        for satz in ("flo zitat Pizza ist Leben", "flo zitat", "flo zitate von goethe"):
            assert _zt(satz, server) is None, satz
        assert _zt("flo zitate", server) == zitate._KEINE_KANDIDATEN
        kern._enabled = False
        assert _zt("flo zitate", server) is None


def test_zitat_speichern_per_rechtsklick_und_jede_abfuhr():
    """Antwort sofort (ephemeral), die 📌-Reaktion danach. Abgelehnt: Bots, die
    eigene Nachricht, leer, zu kurz/lang, doppelt, Hetze, private Daten, eine
    volle Runde, zu viele von einer Person - und ein Notsignal, neutral."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    with _zt_umgebung() as (kern, server, _c, _k), _uhr_gestellt(wand, mono):
        msg = _zitat_msg(900, "Pizza ist   ein Gemüse,\nwenn man dran glaubt", 40, "Anna",
                         server=server)
        klick = _speichern_per_klick(31, msg)
        assert klick.protokoll == [("send", zitate._GESPEICHERT, {"ephemeral": True}),
                                   ("reaktion", "📌")], klick.protokoll
        eintrag = kern._guild(77)["wochen"]["2026-W43"][0]
        assert eintrag["mid"] == 900 and eintrag["uid"] == 40 and eintrag["von"] == 31
        assert eintrag["text"] == "Pizza ist ein Gemüse, wenn man dran glaubt"
        assert eintrag["name"] == "Anna" and eintrag["von_name"] == "U31"

        def abfuhr(grund, msg, saver=31):
            klick = _speichern_per_klick(saver, msg)
            assert klick.protokoll == [("send", zitate._ABGELEHNT[grund], {"ephemeral": True})], \
                (grund, klick.protokoll)

        abfuhr("doppelt", msg, saver=32)
        abfuhr("bot", _zitat_msg(901, "Ich bin ein Bot", 1, bot=True, server=server))
        abfuhr("eigen", _zitat_msg(902, "Ich bin genial", 31, server=server))
        abfuhr("leer", _zitat_msg(903, "   ", 40, server=server))
        abfuhr("kurz", _zitat_msg(904, "ok", 40, server=server))
        abfuhr("lang", _zitat_msg(905, "x" * 301, 40, server=server))
        abfuhr("hetze", _zitat_msg(906, "Ausländer raus", 40, server=server))
        abfuhr("heikel", _zitat_msg(907, "schreib mir: anna@example.com", 40, server=server))
        abfuhr("heikel", _zitat_msg(908, "guckt mal https://example.com", 40, server=server))
        # Ein Notsignal: neutral abgelehnt - auch die eigene Nachricht, auch zu lang.
        for mid, text, autor in ((909, "ich will nicht mehr leben", 40),
                                 (910, "ich will nicht mehr leben " * 20, 31)):
            abfuhr("notlage", _zitat_msg(mid, text, autor, server=server))
        abfuhr("notlage", _zitat_msg(911, "Harmloser Satz", 40, "will nicht mehr leben",
                                     server=server))
        neutral = zitate._ABGELEHNT["notlage"]
        assert not any(w in neutral.lower() for w in ("lol", "heul", "jammer", "opfer"))
        assert ai.FloAI._NOTLAGE_RE.search("ich will nicht mehr leben")
        assert [k["mid"] for k in kern._guild(77)["wochen"]["2026-W43"]] == [900]
        # Hoechstens 10 je Person, die speichert, und 50 offene je Server.
        for i in range(9):
            assert _speichern_per_klick(31, _zitat_msg(1000 + i, f"Satz {i}", 40,
                                                       server=server)).protokoll[0][1] \
                == zitate._GESPEICHERT
        abfuhr("zu_viele", _zitat_msg(1100, "noch einer", 40, server=server))
        for i in range(40):
            _speichern_per_klick(200 + i, _zitat_msg(1200 + i, f"Satz {i}", 40, server=server))
        abfuhr("voll", _zitat_msg(1300, "einer zu viel", 40, server=server), saver=299)
        # Fremder Server: nein.
        fremd = _zitat_msg(1301, "von woanders", 40, server=SimpleNamespace(id=78))
        abfuhr("fremd", fremd, saver=298)


def test_zitat_speichern_als_antwort():
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    with _zt_umgebung() as (kern, server, _c, _k), _uhr_gestellt(wand, mono):
        assert _zt("flo zitat speichern", server) == zitate._NICHT_ALS_ANTWORT.format(
            name=ai.bot_name())
        quelle = _zitat_msg(900, "Montag ist auch nur ein Gefühl", 40, server=server)
        msg = _nachricht("flo zitat speichern", server, uid=31)
        msg.reference = SimpleNamespace(message_id=900, resolved=None)

        async def holen(mid):
            if mid == 900:
                return quelle
            raise _http_fehler("weg")
        msg.channel.fetch_message = holen
        assert asyncio.run(zitate.handle(msg)) == zitate._GESPEICHERT
        assert quelle.reaktionen == [("reaktion", "📌")]
        assert kern._guild(77)["wochen"]["2026-W43"][0]["von"] == 31
        msg.reference = SimpleNamespace(message_id=901, resolved=None)
        assert asyncio.run(zitate.handle(msg)) == zitate._NICHT_GEFUNDEN
        # Die eigene Nachricht als Antwort: Eigenlob.
        msg = _nachricht("flo zitat merken", server, uid=40)
        msg.reference = SimpleNamespace(message_id=900, resolved=None)
        msg.channel.fetch_message = holen
        assert asyncio.run(zitate.handle(msg)) == zitate._ABGELEHNT["eigen"]


def test_zitate_liste_und_weg():
    """'flo zitate' zeigt die offenen Kandidaten nummeriert; 'flo zitat weg 2'
    darf, wer gespeichert hat, wer zitiert wurde, und die Mods."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    daten = _zt_daten({"2026-W43": [_kandidat(1, 40, "Erster **Satz**", "Anna", von=31),
                                    _kandidat(2, 41, "Zweiter @everyone", "Bob", von=32),
                                    _kandidat(3, 42, "Dritter", "Cem", von=33)]})
    mod = SimpleNamespace(manage_messages=True, administrator=False)
    with _zt_umgebung(daten) as (kern, server, _c, _k), _uhr_gestellt(wand, mono):
        liste = _zt("flo zitate", server)
        assert "(3)" in liste and "**1** „Erster \\*\\*Satz\\*\\*“ – Anna" in liste
        assert "@everyone" not in liste and "**3** „Dritter“ – Cem" in liste
        assert _zt("flo zitat weg 2", server, uid=99) == zitate._DARFST_NICHT
        assert _zt("flo zitat weg 9", server) == zitate._GIBTS_NICHT.format(
            nr=9, name=ai.bot_name())
        assert _zt("flo zitat weg", server) == zitate._WELCHE_NUMMER.format(name=ai.bot_name())
        assert _zt("flo zitat weg 2", server, uid=41) in zitate._WEG_TEXT, "der Zitierte"
        assert _zt("flo zitat weg 1", server, uid=31) in zitate._WEG_TEXT, "wer gespeichert hat"
        assert _zt("flo zitat weg 1", server, uid=98, rechte=mod) in zitate._WEG_TEXT
        assert _zt("flo zitate", server) == zitate._KEINE_KANDIDATEN
        assert kern._guild(77)["wochen"] == {"2026-W43": []}


def test_zitat_umfrage_sonntags_genau_einmal():
    """Sonntag ab 12 Uhr, bei stabiler Uhr, einmal je Woche: bis zu zehn
    Kandidaten, hoechstens zwei derselben Person (feste Zufallsauswahl),
    nummerierte Antworten mit hoechstens 55 Zeichen, die vollen Zitate im Text.
    'gestartet' steht auf der Platte, bevor die Umfrage rausgeht - nach
    Uhrspruengen kommt keine zweite. Was danach gespeichert wird, gehoert zur
    naechsten Runde."""
    kandidaten = [_kandidat(i, 40, f"Anna sagt Satz {i}", "Anna") for i in range(1, 5)]
    kandidaten += [_kandidat(10 + i, 50 + i, f"Satz von Person {i}", f"Leute{i}")
                   for i in range(8)]
    kandidaten.append(_kandidat(30, 60, "Ein sehr langes Zitat " + "bla " * 60, "Ein "
                                "unfassbar langer Anzeigename"))
    wand, mono = [_zwann(25, 11, 59)], [1000.0]
    with _zt_umgebung(_zt_daten({"2026-W43": kandidaten}, gestartet="2026-W42")) as \
            (kern, server, client, _k), _uhr_gestellt(wand, mono):
        auf_platte = []
        server.kanal.beim_senden = lambda _c, _kw: auf_platte.append(
            _datei("zitate.json")["guilds"]["77"]["gestartet"])
        _zt_ticken(client)
        assert server.kanal.gesendet == [], "vor 12 Uhr nicht"
        _stellen(wand, mono, _zwann(25, 12, 0))
        _zt_ticken(client)
        assert auf_platte == ["2026-W43"]
        assert len(server.kanal.gesendet) == 1
        text, kw = server.kanal.gesendet[0]
        umfrage = kw["poll"]
        assert umfrage.question == zitate.FRAGE and umfrage.duration.total_seconds() == 8 * 3600
        antworten = [a.text for a in umfrage.answers]
        assert len(antworten) == 10 and all(len(a) <= 55 for a in antworten), antworten
        assert [a.split(" · ")[0] for a in antworten] == [str(n) for n in range(1, 11)]
        assert sum(a.endswith("– Anna") for a in antworten) == 2, antworten
        assert kw["allowed_mentions"].users is False and not kw["allowed_mentions"].everyone
        assert len(text) <= 2000 and text.startswith("🏆 **Zitat der Woche**")
        u = kern._guild(77)["umfrage"]
        for nr, k in enumerate(u["kandidaten"], 1):
            assert antworten[nr - 1].startswith(f"{nr} · „")
            if len(k["text"]) < 100:
                assert f"**{nr}** „{k['text']}“" in text, k["text"]
        # Dieselbe Runde, derselbe Seed - dieselbe Auswahl.
        assert zitate.auswahl(kandidaten, "77:2026-W43") == u["kandidaten"]
        assert u["mid"] == 5001 and u["woche"] == "2026-W43" and u["sieger"] is None
        # Weiter ticken; die Uhr springt zurueck (vor 12, in den Samstag) und vor.
        for wie, sekunden in ((_stellen, 60), (_springt, -7200), (_stellen, 400),
                              (_springt, -86400), (_stellen, 400), (_springt, 86400),
                              (_stellen, 400)):
            if wie is _stellen:
                _stellen(wand, mono, wand[0] + sekunden)
            else:
                wie(wand, mono, sekunden)
            _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1
        # Nach dem Start gespeichert: naechste Runde.
        _stellen(wand, mono, _zwann(25, 13))
        _speichern_per_klick(31, _zitat_msg(500, "Neu am Sonntag", 41, server=server))
        assert kern._guild(77)["wochen"]["2026-W44"][0]["mid"] == 500
        assert [k["mid"] for k in kern.offene(kern._guild(77))] == [500]
        # Kaputter Stand weit in der Zukunft haelt nichts fuer immer an.
        kern._guild(77)["gestartet"] = "2099-W01"
        assert kern.sammel_woche(kern._guild(77)) == "2026-W43"


def test_zitat_umfrage_montags_nachgeholt_und_sonst_in_die_naechste_runde():
    """War Flo am Sonntag weg, kommt die Abstimmung bis Montag 12 Uhr nach.
    Danach nicht mehr - die Kandidaten wandern in die naechste Runde."""
    daten = _zt_daten({"2026-W43": [_kandidat(1, 40), _kandidat(2, 41)]},
                      gestartet="2026-W42")
    with _zt_umgebung(daten) as (kern, server, client, _k), \
            _uhr_gestellt([_zwann(26, 11, 30)], [1000.0]):
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1 and kern._guild(77)["gestartet"] == "2026-W43"
    wand, mono = [_zwann(26, 12, 0)], [1000.0]
    with _zt_umgebung(daten) as (kern, server, client, _k), _uhr_gestellt(wand, mono):
        _zt_ticken(client)
        assert server.kanal.gesendet == []
        kern._guild(77)["wochen"]["2026-W44"] = [_kandidat(3, 42, ts=99)]
        _stellen(wand, mono, _zwann(1, 12, 30, monat=11))
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1
        assert [k["mid"] for k in kern._guild(77)["umfrage"]["kandidaten"]] == [1, 2, 3]
        assert kern._guild(77)["gestartet"] == "2026-W44"


def test_zitat_ein_kandidat_konkurrenzlos_keiner_eine_zeile():
    """Ein Kandidat gewinnt ohne Umfrage. Keiner: eine derbe Zeile - aber nur,
    wenn hier in den letzten vier Wochen ueberhaupt etwas gespeichert wurde."""
    wand, mono = [_zwann(25, 12, 5)], [1000.0]
    daten = _zt_daten({"2026-W43": [_kandidat(1, 40, "Ich bin der Einzige", "Anna")]},
                      gestartet="2026-W42")
    with _zt_umgebung(daten) as (kern, server, client, karten), _uhr_gestellt(wand, mono):
        server.mitglied(40, "Anna")              # das Profilbild kommt per fetch_user
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1
        text, kw = server.kanal.gesendet[0]
        assert "poll" not in kw and "konkurrenzlos" in text and "<@40>" in text
        assert "„Ich bin der Einzige“" in text and kw["file"].filename == "zitat_der_woche.png"
        assert karten == [(b"avatar", "Ich bin der Einzige", "Anna")]
        assert kw["allowed_mentions"].users == [discord.Object(40)]
        assert kern._guild(77)["sieger"][0]["uid"] == 40
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1
    # Nichts gespeichert, nie etwas gehabt: Stille.
    with _zt_umgebung(_zt_daten({}, gestartet="2026-W42")) as (kern, server, client, _k), \
            _uhr_gestellt(wand, mono):
        _zt_ticken(client)
        assert server.kanal.gesendet == [] and kern._guild(77)["gestartet"] == "2026-W43"
    # Vor drei Wochen gab es Zitate: eine Zeile, ohne Ping.
    with _zt_umgebung(_zt_daten({"2026-W40": [_kandidat(1, 40)]}, gestartet="2026-W42")) as \
            (kern, server, client, _k), _uhr_gestellt(wand, mono):
        _zt_ticken(client)
        assert [t for t, _kw in server.kanal.gesendet] in ([[z] for z in zitate._LEER_ZEILE])
        assert server.kanal.gesendet[0][1]["allowed_mentions"].users is False
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1


def test_zitat_ohne_umfrage_recht_entscheidet_flo():
    wand, mono = [_zwann(25, 12, 5)], [1000.0]
    daten = _zt_daten({"2026-W43": [_kandidat(i, 40 + i) for i in range(1, 4)]})
    with _zt_umgebung(daten, umfragen=False) as (kern, server, client, _k), \
            _uhr_gestellt(wand, mono):
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1
        text, kw = server.kanal.gesendet[0]
        assert "poll" not in kw and "Umfragen erstellen" in text
        u = kern._guild(77)["umfrage"]
        assert u["art"] == "ohne_umfrage" and u["verkuendet"]
        assert f"<@{u['kandidaten'][u['sieger']]['uid']}>" in text


def test_zitat_ergebnis_ueber_die_embedfelder():
    """Das poll_result kommt ueber bot.py hierher: der Sieger steht in
    victor_answer_id/-text. Karte mit Profilbild, Kommentar, ein Ping. Doppelt
    zugestellt oder danach getickt: kein zweiter Post."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    kandidaten = [_kandidat(1, 40, "Erster", "Anna"), _kandidat(2, 41, "Zweiter Satz", "Bob"),
                  _kandidat(3, 42, "Dritter", "Cem")]
    with _zt_umgebung(_zt_daten({}, gestartet="2026-W42")) as (kern, server, client, karten), \
            _uhr_gestellt(wand, mono):
        server.mitglied(41, "Bob")
        mid = _umfrage_laeuft(kern, server, client, wand, mono, kandidaten)
        ergebnis = _ergebnis_msg(server, mid, sieger=2, text="2 · „Zweiter Satz“ – Bob",
                                 stimmen=3, gesamt=5)
        assert zitate.ist_eigene_umfrage(ergebnis)
        assert not zitate.ist_eigene_umfrage(_ergebnis_msg(server, 1234, frage="Pizza?"))
        assert zitate.ist_eigene_umfrage(_ergebnis_msg(server, 1234)), "an der Frage erkannt"
        assert asyncio.run(zitate.ergebnis(_ergebnis_msg(server, 1234))) is False
        assert asyncio.run(zitate.ergebnis(ergebnis)) is True
        assert len(server.kanal.gesendet) == 2
        text, kw = server.kanal.gesendet[1]
        assert text.startswith("🏆 **Zitat der Woche** – <@41> gewinnt mit 3 von 5 Stimmen.")
        assert "\n> „Zweiter Satz“\n" in text
        assert any(text.endswith(e.format(name="Bob")) for e in zitate._ERSATZ), text
        assert kw["file"].filename == "zitat_der_woche.png"
        assert karten == [(b"avatar", "Zweiter Satz", "Bob")]
        assert kw["allowed_mentions"].users == [discord.Object(41)]
        assert not kw["allowed_mentions"].everyone and not kw["allowed_mentions"].roles
        assert asyncio.run(zitate.ergebnis(ergebnis)) is False
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 2
        antwort = _zt("flo zitat der woche", server)
        assert "KW 43" in antwort and "„Zweiter Satz“" in antwort and "**Bob**" in antwort
        assert "(3 von 5 Stimmen)" in antwort
        # Die naechste Runde: die alte Umfrage gehoert weiter zu zitate.
        kern._guild(77)["wochen"]["2026-W44"] = [_kandidat(7, 40), _kandidat(8, 41)]
        _stellen(wand, mono, _zwann(1, 12, 5, monat=11))
        _zt_ticken(client)
        assert kern._guild(77)["alte"] == [mid]
        assert zitate.ist_eigene_umfrage(_ergebnis_msg(server, mid, frage="anders"))
        assert asyncio.run(zitate.ergebnis(ergebnis)) is False


def test_zitat_gleichstand_ueber_fetch():
    """Bei Gleichstand nennt Discord keinen Sieger - Flo holt die Umfrage und
    entscheidet unter den Besten per Zufall. Geht das Holen schief, haelt Flo
    nichts fest; der Takt holt es spaeter nach."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    kandidaten = [_kandidat(1, 40, name="Anna"), _kandidat(2, 41, name="Bob"),
                  _kandidat(3, 42, name="Cem")]
    with _zt_umgebung(_zt_daten({})) as (kern, server, client, _k), _uhr_gestellt(wand, mono):
        mid = _umfrage_laeuft(kern, server, client, wand, mono, kandidaten)
        ergebnis = _ergebnis_msg(server, mid, gesamt=4)
        server.kanal.nachrichten[mid] = _http_fehler("wackelt")
        assert asyncio.run(zitate.ergebnis(ergebnis)) is False
        assert kern._guild(77)["umfrage"]["sieger"] is None and len(server.kanal.gesendet) == 1
        umfrage = SimpleNamespace(is_finalised=lambda: True, answers=[
            SimpleNamespace(id=1, text="1 · „Zitat Nummer 1“ – Anna", vote_count=2),
            SimpleNamespace(id=2, text="2 · „Zitat Nummer 2“ – Bob", vote_count=2),
            SimpleNamespace(id=3, text="3 · „Zitat Nummer 3“ – Cem", vote_count=0)])
        server.kanal.nachrichten[mid] = SimpleNamespace(poll=umfrage)
        assert asyncio.run(zitate.ergebnis(ergebnis)) is True
        text = server.kanal.gesendet[1][0]
        assert text.startswith("🏆 **Zitat der Woche** – Gleichstand, ich entscheide: <@4")
        u = kern._guild(77)["umfrage"]
        assert u["sieger"] in (0, 1) and u["stimmen"] == 2 and u["gesamt"] == 4


def test_zitat_offline_ergebnis_holt_der_takt():
    """Kam das Ergebnis nie an (Flo war offline), holt der Takt die Umfrage
    30 Minuten nach dem Ende selbst - und wertet sie aus, sobald Discord sie
    abgeschlossen hat. Ist sie weg, gibt Flo auf; null Stimmen: eine Zeile."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    jetzt = [0.0]
    uhr_mono = [500.0]
    kandidaten = [_kandidat(1, 40, name="Anna"), _kandidat(2, 41, name="Bob"),
                  _kandidat(3, 42, name="Cem")]
    with _zt_umgebung(_zt_daten({})) as (kern, server, client, _k), _uhr_gestellt(wand, mono), \
            mock.patch.object(zitate, "_zeit", lambda: jetzt[0]), \
            mock.patch.object(zitate, "_mono", lambda: uhr_mono[0]):
        jetzt[0] = 1_000_000.0
        mid = _umfrage_laeuft(kern, server, client, wand, mono, kandidaten)
        u = kern._guild(77)["umfrage"]
        assert u["ende_ts"] == 1_000_000.0 + 8 * 3600
        geholt = []
        umfrage = SimpleNamespace(fertig=False, answers=[
            SimpleNamespace(id=1, text="1 · „x“ – Anna", vote_count=0),
            SimpleNamespace(id=2, text="2 · „x“ – Bob", vote_count=1),
            SimpleNamespace(id=3, text="3 · „x“ – Cem", vote_count=3)])
        umfrage.is_finalised = lambda: umfrage.fertig

        async def holen(m):
            geholt.append(m)
            return SimpleNamespace(poll=umfrage)
        server.kanal.fetch_message = holen
        jetzt[0] = u["ende_ts"] + 29 * 60
        _zt_ticken(client)
        assert geholt == [] and len(server.kanal.gesendet) == 1
        jetzt[0] = u["ende_ts"] + 31 * 60
        _zt_ticken(client)
        assert geholt == [mid] and u["sieger"] is None, "noch nicht abgeschlossen"
        _zt_ticken(client)
        assert geholt == [mid], "Pause"
        umfrage.fertig = True
        uhr_mono[0] += zitate.NOCHMAL_NACH + 1
        _zt_ticken(client)
        assert u["sieger"] == 2 and u["stimmen"] == 3 and u["gesamt"] == 4 and u["verkuendet"]
        assert server.kanal.gesendet[1][0].startswith(
            "🏆 **Zitat der Woche** – <@42> gewinnt mit 3 von 4 Stimmen.")
    # Weg (geloescht) -> aufgegeben, nichts gepostet. Null Stimmen -> eine Zeile.
    for stimmen, erwartet in ((None, 1), (0, 2)):
        wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
        with _zt_umgebung(_zt_daten({})) as (kern, server, client, _k), \
                _uhr_gestellt(wand, mono), \
                mock.patch.object(zitate, "_zeit", lambda: jetzt[0]):
            jetzt[0] = 1_000_000.0
            mid = _umfrage_laeuft(kern, server, client, wand, mono, kandidaten)
            if stimmen is not None:
                server.kanal.nachrichten[mid] = SimpleNamespace(poll=SimpleNamespace(
                    is_finalised=lambda: True, answers=[
                        SimpleNamespace(id=i, text=f"{i} · „x“", vote_count=stimmen)
                        for i in (1, 2, 3)]))
            jetzt[0] += 9 * 3600
            _zt_ticken(client)
            u = kern._guild(77)["umfrage"]
            assert u["verkuendet"] and len(server.kanal.gesendet) == erwartet
            if stimmen == 0:
                assert server.kanal.gesendet[1][0] == zitate._KEINE_STIMMEN


def test_zitat_sendepause_haelt_den_sieger_fest():
    """In der Sendepause wird der Sieger festgehalten, aber nicht gepostet -
    der Takt holt es danach nach, genau einmal. Ist Zitate auf dem Server aus,
    verfaellt der Post."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    kandidaten = [_kandidat(1, 40, name="Anna"), _kandidat(2, 41, name="Bob")]
    with _zt_umgebung(_zt_daten({})) as (kern, server, client, _k), _uhr_gestellt(wand, mono):
        mid = _umfrage_laeuft(kern, server, client, wand, mono, kandidaten)
        ergebnis = _ergebnis_msg(server, mid, sieger=1, text="1 · „x“ – Anna", stimmen=2,
                                 gesamt=3)
        with mock.patch.object(zitate, "_sendepause", lambda: True):
            assert asyncio.run(zitate.ergebnis(ergebnis)) is False
            _zt_ticken(client)
        u = kern._guild(77)["umfrage"]
        assert u["sieger"] == 0 and not u["verkuendet"] and len(server.kanal.gesendet) == 1
        _zt_ticken(client)
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 2 and "<@40> gewinnt" in server.kanal.gesendet[1][0]
    with _zt_umgebung(_zt_daten({})) as (kern, server, client, _k), _uhr_gestellt(wand, mono):
        mid = _umfrage_laeuft(kern, server, client, wand, mono, kandidaten)
        ergebnis = _ergebnis_msg(server, mid, sieger=1, text="1 · „x“ – Anna", stimmen=2,
                                 gesamt=3)
        with mock.patch.object(features, "is_on_in", lambda _g, k: k != "zitate"):
            assert asyncio.run(zitate.ergebnis(ergebnis)) is False
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1 and kern._guild(77)["umfrage"]["verkuendet"]


def test_zitat_abgelehnte_posts_kommen_nach():
    """Lehnt Discord die Umfrage ab, ist die Runde nicht verbraucht: nach
    NOCHMAL_NACH nochmal, mit denselben Kandidaten. Ein abgelehnter Sieger-Post
    kommt nach; ohne 'Dateien anhaengen' eben ohne Karte."""
    wand, mono = [_zwann(25, 12, 5)], [1000.0]
    uhr_mono = [500.0]
    daten = _zt_daten({"2026-W43": [_kandidat(1, 40), _kandidat(2, 41)]},
                      gestartet="2026-W42")
    with _zt_umgebung(daten) as (kern, server, client, _k), _uhr_gestellt(wand, mono), \
            mock.patch.object(zitate, "_mono", lambda: uhr_mono[0]):
        server.kanal.kaputt = 1
        _zt_ticken(client)
        g = kern._guild(77)
        assert server.kanal.gesendet == [] and g["gestartet"] == "2026-W42"
        _zt_ticken(client)
        assert server.kanal.gesendet == [], "Pause"
        uhr_mono[0] += zitate.NOCHMAL_NACH + 1
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1 and g["gestartet"] == "2026-W43"
        mid = g["umfrage"]["mid"]
        server.kanal.kaputt = 2            # mit Karte und ohne
        ergebnis = _ergebnis_msg(server, mid, sieger=2, text="2 · „x“", stimmen=1, gesamt=1)
        assert asyncio.run(zitate.ergebnis(ergebnis)) is False
        assert not g["umfrage"]["verkuendet"]
        server.kanal.ohne_bild = True
        uhr_mono[0] += zitate.NOCHMAL_NACH + 1
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 2 and "file" not in server.kanal.gesendet[1][1]
        assert g["umfrage"]["verkuendet"] and g["umfrage"]["versuche"] == 2


def test_zitat_kommentar_hintergrund_mit_grenze_und_notsignal():
    """Der Kommentar kommt von der KI - als Hintergrund-Aufruf, mit der Grenze,
    im Kontext des Servers. Verweigerung, Notsignal in der Antwort, 'ki' aus:
    der Ersatz. Ein Name mit Notsignal geht nicht an die KI."""
    wand, mono = [_zwann(25, 12, 5)], [1000.0]
    aufrufe, antworten = [], []

    async def generate(prompt, *, system=None, temperature=0.8, max_tokens=300,
                       hintergrund=False):
        aufrufe.append({"prompt": prompt, "system": system, "hintergrund": hintergrund,
                        "gid": ai.aktuelle_guild()})
        return antworten.pop(0) if antworten else None

    def runde(name, antwort, **patches):
        daten = _zt_daten({"2026-W43": [_kandidat(1, 40, "Döner ist Gemüse", name)]})
        with _zt_umgebung(daten) as (kern, server, client, _k), \
                _uhr_gestellt(list(wand), list(mono)), \
                mock.patch.object(ai, "is_enabled", lambda: True), \
                mock.patch.object(ai, "generate", generate), contextlib.ExitStack() as st:
            for ziel, wert in patches.items():
                st.enter_context(mock.patch.object(features, ziel, wert))
            aufrufe.clear()
            antworten[:] = [antwort]
            _zt_ticken(client)
            return list(aufrufe), server.kanal.gesendet[0][0]

    a, text = runde("Anna", "Döner als Gemüse, Anna? Deine Ärztin weint leise.")
    assert len(a) == 1 and a[0]["hintergrund"] is True and a[0]["gid"] == 77
    assert ai.FloAI._GUARDRAIL in a[0]["system"] and "GENAU ZWEI" in a[0]["system"]
    assert "Döner ist Gemüse" in a[0]["prompt"] and "Anna" in a[0]["prompt"]
    assert text.endswith("Döner als Gemüse, Anna? Deine Ärztin weint leise.")
    for antwort in ("I'm sorry, but I can't help with that.",
                    "Ich will sterben, so schlecht ist der Satz.", None):
        a, text = runde("Anna", antwort)
        assert len(a) == 1 and any(text.endswith(e.format(name="Anna")) for e in zitate._ERSATZ)
    # Ein Name mit Notsignal: so ein Kandidat kommt gar nicht in die Abstimmung
    # (aeltere Daten) - und falls doch einer verkuendet wird, sieht die KI nur 'du'.
    daten = _zt_daten({"2026-W43": [_kandidat(1, 40, "Döner ist Gemüse",
                                              "will nicht mehr leben")]})
    with _zt_umgebung(daten) as (kern, server, client, _k), \
            _uhr_gestellt(list(wand), list(mono)), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        aufrufe.clear()
        _zt_ticken(client)
        assert server.kanal.gesendet == [] and aufrufe == []
        antworten[:] = ["Na du."]
        k = _kandidat(1, 40, "Döner ist Gemüse", "will nicht mehr leben")
        assert asyncio.run(kern._kommentar(77, k, hintergrund=True)) == "Na du."
        assert "leben" not in aufrufe[0]["prompt"] and "Gesagt von: du" in aufrufe[0]["prompt"]
        k["text"] = "ich will nicht mehr leben"
        aufrufe.clear()
        kommentar = asyncio.run(kern._kommentar(77, k, hintergrund=True))
        assert kommentar in [e.format(name="du") for e in zitate._ERSATZ], kommentar
        assert aufrufe == [], "ein Zitat mit Notsignal geht nie an die KI"
    a, text = runde("Anna", "egal", is_on_in=lambda _g, k: k != "ki")
    assert a == [] and any(text.endswith(e.format(name="Anna")) for e in zitate._ERSATZ)


def test_zitat_umfrage_jetzt_nur_fuer_den_chef():
    """Der Testausloeser: nur der Besitzer, und die Runde zaehlt - sonntags
    kommt fuer diese Woche keine zweite."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    daten = _zt_daten({"2026-W43": [_kandidat(1, 40), _kandidat(2, 41)]},
                      gestartet="2026-W42")
    with _zt_umgebung(daten) as (kern, server, client, _k), _uhr_gestellt(wand, mono):
        assert _zt("flo zitat umfrage jetzt", server, uid=5) == zitate._NUR_CHEF
        assert server.kanal.gesendet == []
        assert _zt("flo zitat umfrage jetzt", server, uid=admin_id()).startswith("Läuft.")
        assert len(server.kanal.gesendet) == 1 and kern._guild(77)["gestartet"] == "2026-W43"
        _stellen(wand, mono, _zwann(25, 12, 5))
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1
        assert _zt("flo zitat umfrage jetzt", server, uid=admin_id()).startswith("Keine Kandidaten")


def test_zitat_ueber_bot_kette_vor_media():
    """'flo zitat der woche' gehoert zitate (sonst machte media ein Meme
    daraus), 'flo zitat <text>' weiter media."""
    import bot
    from test_fundament import _menschen_nachricht, _nur_geladen
    memes = []

    async def meme(_self, _message, text):
        memes.append(text)
        return bot.media.HANDLED

    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    with _zt_umgebung() as (_kern, _s, _c, _k), _uhr_gestellt(wand, mono), \
            _nur_geladen(bot, "ZITATE_ENABLED", "MEDIA_ENABLED"), \
            mock.patch.object(bot.media.Media, "_cmd_quote", meme):
        bot.media.instance._enabled = True
        msg = _menschen_nachricht("flo zitat der woche")
        asyncio.run(bot.client.on_message(msg))
        assert [c for c, _e, _v in msg.channel.gesendet] == [zitate._KEIN_SIEGER]
        msg = _menschen_nachricht("flo zitat Pizza ist Leben", mid=702)
        asyncio.run(bot.client.on_message(msg))
        assert memes == ["Pizza ist Leben"] and not msg.channel.gesendet
        msg = _menschen_nachricht("flo zitate", mid=703)
        asyncio.run(bot.client.on_message(msg))
        assert [c for c, _e, _v in msg.channel.gesendet] == [zitate._KEINE_KANDIDATEN]
        assert memes == ["Pizza ist Leben"]


def test_zitat_antworten_und_text_passen_in_discords_grenzen():
    lang = {"text": "Wort " * 80, "name": "Ein Name, der einfach nicht aufhören will"}
    for nr in (1, 10):
        antwort = zitate.antwort_text(nr, lang)
        assert len(antwort) <= 55 and antwort.startswith(f"{nr} · „") and "…" in antwort
    assert zitate.antwort_text(3, {"text": "Kurz", "name": "Bob"}) == "3 · „Kurz“ – Bob"
    kandidaten = [{"text": "**fett** @everyone " + "x" * 280, "name": f"N{i}"} for i in range(10)]
    text = zitate.Zitate.umfrage_text(kandidaten)
    assert len(text) <= 2000 and text.count("\n**") == 10
    assert "@everyone" not in text and "\\*\\*fett\\*\\*" in text
    assert zitate.Zitate.umfrage_text(kandidaten[:2]).count("x" * 280) == 2, "passt: voll"
    # Die Abstimmung startet sonntags ab 12, montags wird bis 12 nachgeholt.
    zeit = lambda t, s: datetime.fromtimestamp(_zwann(t, s), uhr.TIMEZONE)  # noqa: E731
    assert zitate.faellige_woche(zeit(25, 11)) is None
    assert zitate.faellige_woche(zeit(25, 12)) == "2026-W43"
    assert zitate.faellige_woche(zeit(26, 11)) == "2026-W43"
    assert zitate.faellige_woche(zeit(26, 12)) is None
    assert zitate.faellige_woche(zeit(24, 20)) is None


def test_zitat_umfrage_nie_in_aufraeum_kanaele():
    """Ist der Ansagen-Kanal ein Aufraeum-Kanal (ab Werk auf dem Hauptserver),
    waere die Umfrage nach einer Minute weg und Discord schickte nie ein
    Ergebnis - dann der System-Kanal. Gibt es keinen, keine Umfrage (die Runde
    bleibt), und der Testausloeser sagt, warum."""
    wand, mono = [_zwann(25, 12, 5)], [1000.0]
    daten = _zt_daten({"2026-W43": [_kandidat(1, 40), _kandidat(2, 41)]},
                      gestartet="2026-W42")
    with _zt_umgebung(daten) as (kern, server, client, _k), _uhr_gestellt(wand, mono), \
            _aufraeum(600):
        server.system_channel = _Kanal(601)
        _zt_ticken(client)
        assert server.kanal.gesendet == [] and len(server.system_channel.gesendet) == 1
        assert "poll" in server.system_channel.gesendet[0][1]
        assert kern._guild(77)["umfrage"]["cid"] == 601
    with _zt_umgebung(daten) as (kern, server, client, _k), \
            _uhr_gestellt([_zwann(*_MITTWOCH)], [1000.0]), _aufraeum(600):
        antwort = _zt("flo zitat umfrage jetzt", server, uid=admin_id())
        assert "Aufräum-Kanal" in antwort and server.kanal.gesendet == [], antwort
        assert kern._guild(77)["gestartet"] == "2026-W42"
    # Ein eingestellter zitat_channel, der aufraeumt, zaehlt auch nicht.
    echt = guildcfg.get
    server = _Server()
    server.system_channel = _Kanal(601)
    with mock.patch.object(guildcfg, "get",
                           lambda gid, key: 600 if key == "zitat_channel" else echt(gid, key)):
        assert zitate.instance._kanal(None, server) is server.kanal
        with _aufraeum(600):
            assert zitate.instance._kanal(_client(server), server) is server.system_channel
            server.system_channel = None
            assert zitate.instance._kanal(_client(server), server) is None


def test_zitat_umfrage_ohne_antwort_beim_senden():
    """Bleibt das Senden der Umfrage ohne Antwort (Netz weg, Zeitueberschreitung,
    Takt abgebrochen), weiss keiner, ob sie drueben ist. Dann keine zweite
    Umfrage, sondern: nach UNKLAR_NACH im Kanal nachsehen. Gefunden -> sie
    laeuft normal weiter; kommt vorher ihr Ergebnis, wird es angenommen; sicher
    nicht da -> Runde frei (neuer Versuch); nicht nachsehbar -> nach
    AUFGEBEN_NACH frei, die Kandidaten wandern weiter."""
    jetzt, uhr_mono = [1_000_000.0], [500.0]
    kandidaten = [_kandidat(1, 40, name="Anna"), _kandidat(2, 41, name="Bob")]

    @contextlib.contextmanager
    def umgebung():
        wand, mono = [_zwann(25, 12, 5)], [1000.0]
        jetzt[0] = 1_000_000.0
        with _zt_umgebung(_zt_daten({"2026-W43": list(kandidaten)}, gestartet="2026-W42")) \
                as (kern, server, client, _k), _uhr_gestellt(wand, mono), \
                mock.patch.object(zitate, "_zeit", lambda: jetzt[0]), \
                mock.patch.object(zitate, "_mono", lambda: uhr_mono[0]):
            yield kern, server, client

    def drueben(server, mid=777):
        def angekommen(_c, kw):
            server.kanal.verlauf.append(SimpleNamespace(
                id=mid, author=SimpleNamespace(id=1), poll=kw["poll"],
                created_at=datetime.fromtimestamp(jetzt[0], uhr.TIMEZONE)))
        return angekommen

    # a) Drueben angekommen, nur die Antwort ging verloren: der Takt findet sie.
    with umgebung() as (kern, server, client):
        server.kanal.verlauf.append(SimpleNamespace(   # eine alte Umfrage von Flo
            id=111, author=SimpleNamespace(id=1), poll=SimpleNamespace(question=zitate.FRAGE),
            created_at=datetime.fromtimestamp(jetzt[0] - 7 * 86400, uhr.TIMEZONE)))
        server.kanal.beim_senden = drueben(server)
        server.kanal.fehler = OSError("Verbindung weg")
        _zt_ticken(client)
        server.kanal.beim_senden = None
        g = kern._guild(77)
        u = g["umfrage"]
        assert g["gestartet"] == "2026-W43" and u["mid"] == 0 and u["start_ts"] == jetzt[0]
        assert _datei("zitate.json")["guilds"]["77"]["umfrage"]["mid"] == 0
        jetzt[0] += 60
        _zt_ticken(client)
        assert u["mid"] == 0 and server.kanal.gesendet == [], "keine zweite, noch nicht nachgesehen"
        jetzt[0] += zitate.UNKLAR_NACH
        _zt_ticken(client)
        assert u["mid"] == 777 and u["ende_ts"] == 1_000_000.0 + 8 * 3600
        assert server.kanal.gesendet == []
        ergebnis = _ergebnis_msg(server, 777, sieger=2, text="2 · „x“ – Bob", stimmen=2,
                                 gesamt=3)
        assert asyncio.run(zitate.ergebnis(ergebnis)) is True
        assert "<@41> gewinnt mit 2 von 3 Stimmen" in server.kanal.gesendet[-1][0]
    # b) Das Ergebnis kommt, bevor der Takt nachsieht: angenommen - aber nicht
    #    aus einem anderen Kanal und keine der alten.
    with umgebung() as (kern, server, client):
        server.kanal.fehler = asyncio.TimeoutError()
        _zt_ticken(client)
        g = kern._guild(77)
        g["alte"] = [555]
        anderswo = _ergebnis_msg(server, 888, sieger=1, text="1 · „x“ – Anna", gesamt=1)
        anderswo.channel = SimpleNamespace(id=601)
        assert asyncio.run(zitate.ergebnis(anderswo)) is False
        assert asyncio.run(zitate.ergebnis(_ergebnis_msg(server, 555, sieger=1, gesamt=1))) \
            is False
        assert g["umfrage"]["mid"] == 0 and server.kanal.gesendet == []
        ergebnis = _ergebnis_msg(server, 888, sieger=1, text="1 · „x“ – Anna", stimmen=1,
                                 gesamt=1)
        assert asyncio.run(zitate.ergebnis(ergebnis)) is True
        assert g["umfrage"]["mid"] == 888 and "<@40> gewinnt" in server.kanal.gesendet[-1][0]
    # c) Abgebrochen (ALLTAG_FRIST) und nicht drueben: Runde frei, neuer Versuch.
    #    Flos Umfrage von letzter Woche im Verlauf zaehlt nicht.
    with umgebung() as (kern, server, client):
        server.kanal.verlauf.append(SimpleNamespace(
            id=111, author=SimpleNamespace(id=1), poll=SimpleNamespace(question=zitate.FRAGE),
            created_at=datetime.fromtimestamp(jetzt[0] - 7 * 86400, uhr.TIMEZONE)))
        server.kanal.fehler = asyncio.CancelledError()
        with contextlib.suppress(asyncio.CancelledError):
            _zt_ticken(client)
        assert _datei("zitate.json")["guilds"]["77"]["gestartet"] == "2026-W43"
        assert kern._sendet == set()
        jetzt[0] += zitate.UNKLAR_NACH + 1
        _zt_ticken(client)
        g = kern._guild(77)
        assert g["gestartet"] == "2026-W42" and g["umfrage"] == {}
        assert server.kanal.gesendet == [], "Pause vor dem neuen Versuch"
        uhr_mono[0] += zitate.NOCHMAL_NACH + 1
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1 and g["gestartet"] == "2026-W43"
        assert [k["mid"] for k in g["umfrage"]["kandidaten"]] == [1, 2]
    # d) Nicht nachsehbar (Verlauf lesen fehlt): spaeter nochmal, nach
    #    AUFGEBEN_NACH frei - die Kandidaten kommen in die naechste Runde.
    with umgebung() as (kern, server, client):
        server.kanal.fehler = OSError("weg")
        server.kanal.verlauf_fehler = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "kein Verlauf")
        _zt_ticken(client)
        jetzt[0] += zitate.UNKLAR_NACH + 1
        _zt_ticken(client)
        g = kern._guild(77)
        assert g["gestartet"] == "2026-W43" and g["umfrage"]["mid"] == 0
        uhr_mono[0] += zitate.NOCHMAL_NACH + 1
        jetzt[0] += zitate.AUFGEBEN_NACH
        _zt_ticken(client)
        assert g["gestartet"] == "2026-W42" and g["umfrage"] == {}
        assert [k["mid"] for k in kern.offene(g)] == [1, 2]
        assert server.kanal.gesendet == []


def test_zitat_sieger_mit_notlage_ohne_spott_und_ohne_ping():
    """Wer gerade ein Notsignal geschrieben hat (nachhaken, 72 Stunden), kommt
    nicht zur Wahl ("wer hat den groessten Mist gelabert?"). Gewinnt jemand,
    der waehrend der Abstimmung eins schreibt: keine KI, kein Spott, kein Ping."""
    import nachhaken
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    gesperrt, aufrufe = {42}, []

    async def generate(prompt, **_kw):
        aufrufe.append(prompt)
        return "Bob, das war der duemmste Satz der Woche."

    kandidaten = [_kandidat(1, 40, name="Anna"), _kandidat(2, 41, name="Bob"),
                  _kandidat(3, 42, name="Cem")]
    with _zt_umgebung(_zt_daten({}, gestartet="2026-W42")) as (kern, server, client, karten), \
            _uhr_gestellt(wand, mono), \
            mock.patch.object(nachhaken, "is_enabled", lambda: True), \
            mock.patch.object(nachhaken.instance, "_gesperrt", lambda uid: uid in gesperrt), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        server.mitglied(41, "Bob")
        mid = _umfrage_laeuft(kern, server, client, wand, mono, kandidaten)
        u = kern._guild(77)["umfrage"]
        assert [k["uid"] for k in u["kandidaten"]] == [40, 41], "Cem nicht zur Wahl"
        assert "Cem" not in server.kanal.gesendet[0][0]
        gesperrt.add(41)
        ergebnis = _ergebnis_msg(server, mid, sieger=2, text="2 · „x“ – Bob", stimmen=2,
                                 gesamt=3)
        assert asyncio.run(zitate.ergebnis(ergebnis)) is True
        text, kw = server.kanal.gesendet[-1]
        assert text.endswith("\n" + zitate._SANFT) and aufrufe == []
        assert not any(e.format(name="Bob") in text for e in zitate._ERSATZ)
        assert kw["allowed_mentions"].users is False, "kein Ping"
        assert karten == [(b"avatar", "Zitat Nummer 2", "Bob")]
    # Ohne Sperre: wie immer (KI-Kommentar, Ping).
    with _zt_umgebung(_zt_daten({"2026-W43": [_kandidat(1, 40, name="Anna")]})) as \
            (kern, server, client, _k), _uhr_gestellt([_zwann(25, 12, 5)], [1000.0]), \
            mock.patch.object(nachhaken, "is_enabled", lambda: True), \
            mock.patch.object(nachhaken.instance, "_gesperrt", lambda uid: False), \
            mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        _zt_ticken(client)
        text, kw = server.kanal.gesendet[0]
        assert text.endswith("duemmste Satz der Woche.") and kw["allowed_mentions"].users


def test_zitat_aus_dem_hinterzimmer_bleibt_drin():
    """Was in einem Kanal gesagt wurde, den nicht jeder lesen kann, der den
    Zitat-Kanal liest (Mod-Kanal, privater Thread) - oder in einem NSFW-Kanal
    -, wird nicht gespeichert, nicht abgestimmt und in 'flo zitate' nicht im
    Wortlaut gezeigt. Gemessen je Rolle: es geht auch auf Servern, auf denen
    @everyone nichts sieht. Bot-Rollen zaehlen nicht."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    jeder, mitglied, mod, botrolle = (SimpleNamespace(id=i, managed=(i == 901))
                                      for i in (77, 902, 900, 901))
    with _zt_umgebung(_zt_daten({}, gestartet="2026-W42")) as (kern, server, client, _k), \
            _uhr_gestellt(wand, mono):
        server.roles = [jeder, mitglied, mod, botrolle]
        server.default_role = jeder
        server.kanal = _Raum(600, {77, 902, 900, 901})        # der Zitat-Kanal
        offen = _Raum(601, {77, 902, 900})                     # ohne die Bot-Rolle
        modraum = _Raum(602, {900})
        nsfw = _Raum(603, {77, 902, 900}, nsfw=True)
        faden = _Raum(604, {77, 902, 900}, typ=discord.ChannelType.private_thread)
        unbekannt = SimpleNamespace(id=605, permissions_for=lambda _w: discord.Permissions.none())
        raeume = {r.id: r for r in (server.kanal, offen, modraum, nsfw, faden)}
        server.get_channel = raeume.get

        def msg(mid, kanal, uid=40):
            m = _zitat_msg(mid, f"Satz {mid} aus dem Kanal", uid, server=server)
            m.channel = kanal
            return m

        for mid, kanal in ((1, modraum), (2, nsfw), (3, faden), (4, unbekannt)):
            klick = _speichern_per_klick(31, msg(mid, kanal))
            assert klick.protokoll == [("send", zitate._ABGELEHNT["privat"],
                                        {"ephemeral": True})], (mid, klick.protokoll)
        neutral = zitate._ABGELEHNT["privat"]
        assert "Hinterzimmer" in neutral
        assert _speichern_per_klick(31, msg(5, offen)).protokoll[0][1] == zitate._GESPEICHERT
        # Verifizierte Server: @everyone sieht nichts, die Mitglieds-Rolle alles.
        for raum in raeume.values():
            raum.leser.discard(77)
        assert _speichern_per_klick(31, msg(6, offen, 41)).protokoll[0][1] == zitate._GESPEICHERT
        assert _speichern_per_klick(31, msg(7, modraum)).protokoll[0][1] == neutral
        # Gespeichert, als der Zitat-Kanal noch Mod-Kanal war: 'flo zitate'
        # zeigt es nur dort im Wortlaut, und abgestimmt wird es im
        # oeffentlichen Zitat-Kanal nicht.
        heimlich = _kandidat(8, 42, "Nur unter uns Mods gesagt", "Cem")
        heimlich["cid"] = 602
        kern._guild(77)["wochen"]["2026-W43"].append(heimlich)

        def liste(kanal):
            m = _nachricht("flo zitate", server)
            m.channel = kanal
            return asyncio.run(zitate.handle(m))
        draussen = liste(offen)
        assert "Nur unter uns" not in draussen and "(+1 aus Kanälen" in draussen, draussen
        assert "**1**" not in draussen and "**2** „Satz 5" in draussen, "Nummern bleiben"
        drinnen = liste(modraum)
        assert "**1** „Nur unter uns Mods gesagt“ – Cem" in drinnen and "(+" not in drinnen
        _stellen(wand, mono, _zwann(25, 12, 5))
        _zt_ticken(client)
        text, kw = server.kanal.gesendet[0]
        assert "Nur unter uns" not in text and len(kw["poll"].answers) == 2
        assert [k["mid"] for k in kern._guild(77)["umfrage"]["kandidaten"]] == [5, 6]


def test_zitat_umfrage_jetzt_ohne_kandidaten_verbraucht_keine_runde():
    """Der Testausloeser ohne Kandidaten aendert nichts - weder mitten in der
    Woche noch nach der Sonntags-Umfrage: der naechste Sonntag kommt."""
    wand, mono = [_zwann(*_MITTWOCH)], [1000.0]
    with _zt_umgebung(_zt_daten({}, gestartet="2026-W42")) as (kern, server, client, _k), \
            _uhr_gestellt(wand, mono):
        g = kern._guild(77)
        assert _zt("flo zitat umfrage jetzt", server, uid=admin_id()).startswith(
            "Keine Kandidaten")
        assert g["gestartet"] == "2026-W42"
        for mid, uid in ((900, 40), (901, 41)):
            _speichern_per_klick(31, _zitat_msg(mid, f"Mittwochs-Satz {mid}", uid, server=server))
        assert [k["mid"] for k in g["wochen"]["2026-W43"]] == [900, 901]
        _stellen(wand, mono, _zwann(25, 12, 5))
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 1 and g["gestartet"] == "2026-W43"
        assert _zt("flo zitat umfrage jetzt", server, uid=admin_id()).startswith(
            "Keine Kandidaten")
        assert g["gestartet"] == "2026-W43", "nach der Umfrage auch nicht"
        for mid, uid in ((902, 40), (903, 41)):
            _speichern_per_klick(31, _zitat_msg(mid, f"Sonntags-Satz {mid}", uid, server=server))
        _stellen(wand, mono, _zwann(1, 12, 5, monat=11))
        _zt_ticken(client)
        assert len(server.kanal.gesendet) == 2 and g["gestartet"] == "2026-W44"
        assert [k["mid"] for k in g["umfrage"]["kandidaten"]] == [902, 903]


if __name__ == "__main__":
    run(globals())
