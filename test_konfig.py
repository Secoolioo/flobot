"""Server-Einstellungen, Funktionsschalter und die Befehlserkennung.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur konfig      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _als_coro, _arbeit_frisch, _cfg_frisch, _embed_text,
    _fake_msg, _rauch_nachricht)



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




def test_soundboard_ist_eine_server_einstellung():
    """Das Soundboard lag frueher in einem EIGENEN Speicher, galt fuer ALLE
    Server gleich und liess sich nur vom Bot-Besitzer umschalten - im Web-Panel
    tauchte es gar nicht auf.

    Jetzt steht es in guildcfg. Damit gilt es je Server, erscheint automatisch
    im Panel (das rendert den Katalog) und Discord-Befehl und Panel schreiben
    dieselbe Stelle. Genau das ist mit "synchronisiert" gemeint."""
    import guildcfg
    import voicegags

    # Es MUSS im Katalog stehen - sonst ist es nicht im Panel.
    assert "soundboard" in {e.key for e in guildcfg.KATALOG}
    assert "join_sounds" in {e.key for e in guildcfg.KATALOG}

    stand = {}
    alt_an, alt_setzen = guildcfg.an, guildcfg.setzen

    async def fake_setzen(gid, key, roh, guild=None):
        stand[(int(gid), key)] = str(roh).lower() in ("an", "ein", "on", "1", "true")
        return True, stand[(int(gid), key)], ""

    guildcfg.an = lambda gid, key: stand.get((int(gid), key), True)
    guildcfg.setzen = fake_setzen
    alt_enabled = voicegags.instance._enabled
    voicegags.instance._enabled = True
    try:
        # Gelesen wird je Server - nicht global.
        stand[(77, "soundboard")] = False
        assert voicegags.soundboard_enabled(77) is False
        assert voicegags.soundboard_enabled(88) is True, "der Wert gilt serveruebergreifend"

        def msg(text, darf=True, gid=77):
            m = _fake_msg(5, f"flo {text}")
            m.guild = SimpleNamespace(id=gid)
            m.author.guild_permissions = SimpleNamespace(manage_guild=darf)
            return m

        # Wer den Server verwaltet, darf schalten - und es landet in guildcfg.
        antwort = asyncio.run(voicegags.handle(msg("soundboard an")))
        assert stand[(77, "soundboard")] is True, stand
        assert "AN" in str(antwort)
        asyncio.run(voicegags.handle(msg("soundboard aus")))
        assert stand[(77, "soundboard")] is False

        # Wer nicht darf, aendert NICHTS - vorher brauchte man dafuer den
        # Bot-Besitzer, jetzt reicht 'Server verwalten'.
        stand[(77, "soundboard")] = True
        antwort = asyncio.run(voicegags.handle(msg("soundboard aus", darf=False)))
        assert stand[(77, "soundboard")] is True, "ohne Recht wurde geschaltet"
        assert "verwaltet" in str(antwort)

        # 'soundboard' OHNE an/aus ist weiterhin der Aufruf des Bretts und
        # aendert die Einstellung NICHT (sonst schaltet ein Blick sie um).
        stand[(77, "soundboard")] = True
        try:
            asyncio.run(voicegags.handle(msg("soundboard")))
        except AttributeError:
            pass        # das Brett braucht ein echtes Discord-Objekt
        assert stand[(77, "soundboard")] is True, "ein Blick hat umgeschaltet"
    finally:
        guildcfg.an, guildcfg.setzen = alt_an, alt_setzen
        voicegags.instance._enabled = alt_enabled




def test_cmdnorm_admin_sicherheit():
    # Alltagswoerter, die 1 Tippfehler von Admin-Befehlen entfernt sind,
    # duerfen NICHT gekapert werden.
    for satz in ("nimmt das ernst", "profi tipp", "ansagen bitte"):
        assert cmdnorm.normalize(satz) is None, satz
    # Echte Vertipper werden weiterhin korrigiert.
    assert cmdnorm.normalize("admiin") == "admin"




def test_guildcfg_trennt_die_server():
    """Zwei Server, zwei Meinungen: was der eine einstellt, geht den anderen
    nichts an. Ohne eigenen Wert gilt weiterhin der Standard."""
    guildcfg, zurueck = _cfg_frisch()
    try:
        A, B = 111, 222
        std = guildcfg.get(A, "lautstaerke")
        assert guildcfg.get(B, "lautstaerke") == std        # beide auf Standard
        assert not guildcfg.eigen(A, "lautstaerke")

        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "lautstaerke", "80"))
        assert ok and wert == 80
        assert guildcfg.get(A, "lautstaerke") == 80
        assert guildcfg.get(B, "lautstaerke") == std, "Server B wurde mitgezogen"
        assert guildcfg.eigen(A, "lautstaerke") and not guildcfg.eigen(B, "lautstaerke")

        # Zurueck auf Standard -> der eigene Wert verschwindet wirklich.
        asyncio.run(guildcfg.setzen(A, "lautstaerke", "standard"))
        assert guildcfg.get(A, "lautstaerke") == std
        assert not guildcfg.eigen(A, "lautstaerke")

        # Grenzen und Unsinn werden abgewiesen - und aendern NICHTS.
        for murks in ("999", "-5", "abc", "inf", "nan", ""):
            ok, _w, fehler = asyncio.run(guildcfg.setzen(A, "lautstaerke", murks))
            assert not ok and fehler, murks
        assert guildcfg.get(A, "lautstaerke") == std

        # An/Aus in allen Schreibweisen.
        for ja in ("an", "AN", "on", "1", "ja", "true"):
            asyncio.run(guildcfg.setzen(A, "bayern", ja))
            assert guildcfg.an(A, "bayern") is True, ja
        for nein in ("aus", "off", "0", "nein", "false"):
            asyncio.run(guildcfg.setzen(A, "bayern", nein))
            assert guildcfg.an(A, "bayern") is False, nein
        assert guildcfg.an(B, "bayern") is False

        # Kanaele: Erwaehnung, rohe ID, Liste, und wieder aus.
        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "ansage_channel",
                                                   "<#1512045750362837013>"))
        assert ok and wert == 1512045750362837013
        assert guildcfg.text(A, "ansage_channel") == "<#1512045750362837013>"
        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "autodelete_channels",
                                                   "111222333444555666, 777888999000111222"))
        assert ok and wert == [111222333444555666, 777888999000111222]
        ok, wert, _f = asyncio.run(guildcfg.setzen(A, "ansage_channel", "aus"))
        assert ok and wert == 0 and guildcfg.text(A, "ansage_channel") == "aus"

        # Unbekannter Schluessel: sauberes Nein, kein Absturz.
        ok, _w, fehler = asyncio.run(guildcfg.setzen(A, "gibtsnicht", "1"))
        assert not ok and fehler
        assert guildcfg.get(A, "gibtsnicht") is None

        # Von Hand kaputt editierte Datei -> Standard statt Absturz.
        guildcfg.instance._store.data["guilds"][str(A)]["lautstaerke"] = "quatsch"
        assert guildcfg.get(A, "lautstaerke") == std

        # Server weg -> Einstellungen weg.
        assert asyncio.run(guildcfg.vergessen(A)) is True
        assert guildcfg.instance._store.data["guilds"].get(str(A)) is None
    finally:
        zurueck()




def test_guildcfg_befehl_braucht_rechte():
    """'Flo einstellung ...' darf nur, wer den Server verwalten darf."""
    import guildcfg
    guildcfg, zurueck = _cfg_frisch()
    try:
        def msg(text, darf):
            return SimpleNamespace(
                content=f"Flo {text}",
                author=SimpleNamespace(id=5, bot=False, display_name="T",
                                       guild_permissions=SimpleNamespace(manage_guild=darf)),
                guild=SimpleNamespace(id=111, name="Testserver", text_channels=[]),
                mentions=[])

        # Ohne Recht: Absage, und NICHTS wird gesetzt.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung lautstaerke 80", False)))
        assert "Server verwalten" in str(antwort)
        assert not guildcfg.eigen(111, "lautstaerke")

        # Mit Recht: gesetzt.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung lautstaerke 80", True)))
        assert "80" in _embed_text(antwort)
        assert guildcfg.get(111, "lautstaerke") == 80

        # Nur nachfragen aendert nichts.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung lautstaerke", True)))
        assert "80" in _embed_text(antwort) and guildcfg.get(111, "lautstaerke") == 80

        # Unbekannter Schluessel nennt die moeglichen.
        antwort = asyncio.run(guildcfg.handle(msg("einstellung quatsch 1", True)))
        assert "lautstaerke" in _embed_text(antwort)

        # Kein Einstellungs-Befehl -> None (naechster Handler ist dran).
        assert asyncio.run(guildcfg.handle(msg("blackjack 100", True))) is None
    finally:
        zurueck()




def test_features_je_server():
    """Der globale Schalter ist der Not-Aus, der Server-Schalter das Feintuning."""
    import features
    alt = (features.instance._store, set(features.instance._disabled),
           dict(features.instance._per_guild))
    features.instance._store = _FakeStore({"disabled": [], "guilds": {}})
    features.instance._disabled = set()
    features.instance._per_guild = {}
    try:
        A, B = 111, 222
        assert features.is_on_in(A, "casino") and features.is_on_in(B, "casino")

        # Nur auf A aus.
        asyncio.run(features.set_guild(A, "casino", False))
        assert not features.is_on_in(A, "casino")
        assert features.is_on_in(B, "casino"), "Nachbarserver mitgerissen"
        assert features.is_on("casino"), "global faelschlich aus"

        # Global aus schlaegt jedes 'an' eines Servers.
        asyncio.run(features.set_feature("casino", False))
        assert not features.is_on_in(B, "casino")
        assert asyncio.run(features.set_guild(B, "casino", True)) is False
        assert not features.is_on_in(B, "casino")

        # Global wieder an -> A bleibt aus (sein eigener Wille), B ist an.
        asyncio.run(features.set_feature("casino", True))
        assert not features.is_on_in(A, "casino") and features.is_on_in(B, "casino")

        # fuer(gid) ist dieselbe Pruefung, nur vorgemerkt (so nutzt bot.py sie).
        _on = features.fuer(A)
        assert _on("casino") is False and _on("music") is True
        assert features.fuer(0)("casino") is True      # DM: nur global zaehlt

        # Ohne Server (DM) zaehlt nur der globale Schalter.
        assert features.is_on_in(0, "casino") is True

        # Gespeichert wird beides getrennt.
        assert features.instance._store.data["guilds"] == {"111": ["casino"]}
        assert features.instance._store.data["disabled"] == []

        # Server weg -> seine Schalter auch.
        assert asyncio.run(features.vergessen(A)) is True
        assert features.is_on_in(A, "casino")

        # Panel-Liste: 'on' gilt fuer den Server, 'global_on' fuers Ganze.
        asyncio.run(features.set_feature("music", False))
        st = {f["key"]: f for f in features.state({"music": True, "casino": True}, B)}
        assert st["music"]["on"] is False and st["music"]["global_on"] is False
        assert st["casino"]["on"] is True and st["casino"]["global_on"] is True
    finally:
        (features.instance._store, features.instance._disabled,
         features.instance._per_guild) = alt




def test_bayrisch_ueberlebt_den_neustart():
    """Der Dialekt lag frueher nur im Arbeitsspeicher - nach jedem Update hat Flo
    wieder hochdeutsch geredet, obwohl niemand etwas umgestellt hatte."""
    import bayern
    guildcfg, zurueck = _cfg_frisch()
    alt = bayern.instance._enabled
    bayern.instance._enabled = True
    try:
        A, B = 111, 222

        def msg(gid, text, darf=True):
            return SimpleNamespace(
                content=f"Flo {text}", mentions=[],
                author=SimpleNamespace(
                    id=5, bot=False, display_name="T",
                    guild_permissions=SimpleNamespace(manage_guild=darf)),
                guild=SimpleNamespace(id=gid, name="S"))

        assert bayern.is_on(A) is False
        # Ohne das Recht wird NICHTS umgestellt. Vorher hat bayern.py die
        # Server-Einstellung voellig ungeprueft umgelegt - ein beilaeufiges
        # "flo red mal bayerisch" hat sie fuer alle geaendert.
        asyncio.run(bayern.handle(msg(A, "bayrisch an", darf=False)))
        assert bayern.is_on(A) is False, "ohne Recht wurde der Dialekt umgestellt"

        antwort = asyncio.run(bayern.handle(msg(A, "bayrisch an")))
        assert "boarisch" in str(antwort)
        assert bayern.is_on(A) is True
        assert bayern.is_on(B) is False, "Nachbarserver mitgerissen"
        # Der Zustand liegt jetzt im Speicher, nicht im Arbeitsspeicher.
        assert guildcfg.an(A, "bayern") is True

        asyncio.run(bayern.handle(msg(A, "bayrisch aus")))
        assert bayern.is_on(A) is False and guildcfg.an(A, "bayern") is False
    finally:
        bayern.instance._enabled = alt
        zurueck()




def test_webpanel_einstellungen_je_server():
    """Panel-Seite eines Servers: Einstellungen lesen, setzen, zuruecksetzen -
    und Funktionen NUR fuer diesen Server schalten."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    import features
    # bot ZUERST importieren - dieselbe Falle wie in _arbeit_frisch():
    # webpanel._api_guildcfg holt sich per lazy 'import bot' die Liste der
    # geladenen Module. Ist bot in diesem Prozess noch nie importiert worden,
    # laeuft dabei bot.py komplett durch - samt 'WEBPANEL_ENABLED =
    # webpanel.setup()', und setup() ueberschreibt _auth aus der .env (Standard
    # AN). Der Test setzt _auth vorher auf False; mitten im Testlauf kippte es
    # dadurch auf True zurueck und der POST kam als 401 nicht mehr durch.
    # Alphabetisch fiel das nie auf, weil ein frueherer Test bot laengst
    # importiert hatte.
    import bot                                                    # noqa: F401
    guildcfg, zurueck = _cfg_frisch()
    alt_feat = (features.instance._store, set(features.instance._disabled),
                dict(features.instance._per_guild))
    features.instance._store = _FakeStore({"disabled": [], "guilds": {}})
    features.instance._disabled = set()
    features.instance._per_guild = {}

    ch = SimpleNamespace(id=901, name="spiele")
    guild = SimpleNamespace(id=111, name="Testserver", text_channels=[ch])
    guild.get_channel = lambda cid: ch if cid == 901 else None

    wp = webpanel.instance
    alt = (wp._enabled, wp._auth, wp._client, dict(wp._tokens))
    wp._enabled, wp._auth = True, False
    wp._tokens = {}
    wp._client = SimpleNamespace(guilds=[guild], is_closed=lambda: False,
                                 get_guild=lambda x: guild if int(x) == 111 else None,
                                 get_channel=lambda _x: None)
    app = wp._build_app()

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            # Unbekannter Server -> 404 statt Absturz.
            assert (await cli.get("/api/guildcfg?guild=999")).status == 404
            assert (await cli.get("/api/guildcfg?guild=quatsch")).status == 404

            j = await (await cli.get("/api/guildcfg?guild=111")).json()
            assert j["ok"] and j["guild"]["id"] == "111"
            keys = {s["key"]: s for s in j["settings"]}
            assert "lautstaerke" in keys and keys["lautstaerke"]["eigen"] is False
            assert {"id": "901", "name": "spiele"} in j["channels"]
            assert any(f["key"] == "casino" for f in j["features"])

            # Setzen.
            r = await cli.post("/api/guildcfg", json={"guild": "111",
                                                      "key": "lautstaerke", "value": "80"})
            assert (await r.json())["ok"] and guildcfg.get(111, "lautstaerke") == 80

            # Unsinn wird abgewiesen und aendert nichts.
            r = await cli.post("/api/guildcfg", json={"guild": "111",
                                                      "key": "lautstaerke", "value": "999"})
            assert r.status == 400 and guildcfg.get(111, "lautstaerke") == 80

            # Kanal-IDs kommen als TEXT zurueck (JS-Zahlen sind zu ungenau).
            await cli.post("/api/guildcfg", json={"guild": "111",
                                                  "key": "ansage_channel", "value": "901"})
            j = await (await cli.get("/api/guildcfg?guild=111")).json()
            keys = {s["key"]: s for s in j["settings"]}
            assert keys["ansage_channel"]["wert"] == "901"
            assert keys["ansage_channel"]["eigen"] is True

            # Fremde Kanal-ID gehoert nicht auf diesen Server -> Absage.
            r = await cli.post("/api/guildcfg", json={"guild": "111",
                                                      "key": "ansage_channel", "value": "12345"})
            assert r.status == 400

            # Zuruecksetzen.
            await cli.post("/api/guildcfg", json={"guild": "111",
                                                  "key": "lautstaerke", "value": "standard"})
            assert guildcfg.eigen(111, "lautstaerke") is False

            # Funktion NUR auf diesem Server abschalten.
            r = await cli.post("/api/feature", json={"guild": "111",
                                                     "key": "casino", "on": False})
            assert (await r.json())["ok"]
            assert features.is_on_in(111, "casino") is False
            assert features.is_on("casino") is True, "global mitgerissen"
            assert features.is_on_in(222, "casino") is True

    try:
        asyncio.run(run_it())
    finally:
        (wp._enabled, wp._auth, wp._client, wp._tokens) = alt
        (features.instance._store, features.instance._disabled,
         features.instance._per_guild) = alt_feat
        zurueck()




def test_guildcfg_standard_kommt_aus_der_env():
    """Nach dem Update darf sich am bestehenden Server NICHTS verschieben.

    Bisher lasen die Module ihre Kanaele direkt aus der .env - mit einem fest
    verdrahteten Wert, wenn dort nichts stand. Beides muss der Katalog genauso
    abbilden, inklusive des Unterschieds zwischen "gar nicht gesetzt" und
    "ausdruecklich leer" (das hiess immer: Funktion aus)."""
    import guildcfg
    A = 111
    alt = {k: os.environ.get(k) for k in
           ("AUTODELETE_CHANNEL_IDS", "LEVELUP_CHANNEL_ID", "AUTODELETE_SECONDS")}

    def setze(**kw):
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    try:
        # 1) Nichts in der .env -> der frueher fest verdrahtete Wert gilt weiter.
        setze(AUTODELETE_CHANNEL_IDS=None, LEVELUP_CHANNEL_ID=None,
              AUTODELETE_SECONDS=None)
        assert guildcfg.instance.standard("autodelete_channels", A) == [1512045750362837013]
        assert guildcfg.instance.standard("ansage_channel", A) == 1512045750362837013
        assert guildcfg.instance.standard("kalorien_channel", A) == 1522294725116428329
        assert guildcfg.instance.standard("autodelete_sekunden", A) == 60

        # 2) AUSDRUECKLICH leer heisst weiterhin: aus.
        setze(AUTODELETE_CHANNEL_IDS="")
        assert guildcfg.instance.standard("autodelete_channels", A) == []
        setze(LEVELUP_CHANNEL_ID="0")
        assert guildcfg.instance.standard("ansage_channel", A) == 0

        # 3) Gesetzte Werte gewinnen - auch mehrere, mit Komma oder Leerzeichen.
        setze(AUTODELETE_CHANNEL_IDS="555111222333444555, 666111222333444555",
              AUTODELETE_SECONDS="45")
        assert guildcfg.instance.standard("autodelete_channels", A) == \
            [555111222333444555, 666111222333444555]
        assert guildcfg.instance.standard("autodelete_sekunden", A) == 45

        # 4) Murks in der .env kippt nichts um - der Fallback traegt.
        setze(AUTODELETE_SECONDS="ganz schnell")
        assert guildcfg.instance.standard("autodelete_sekunden", A) == 60

        # 5) Icon-Automatik und Aktien-Zaehlung sind nur zu Hause von selbst an.
        haupt = guildcfg.HAUPT_GUILD
        for key in ("icon_auto", "aktie_zaehlt"):
            assert guildcfg.instance.standard(key, 999999999999999999) is False, key
            assert guildcfg.instance.standard(key, haupt) is bool(haupt), key
    finally:
        setze(**alt)




def test_feature_schluessel_passen_ueberall_zusammen():
    """features.CATALOG, bot.FEATURE_LOADED und die Handler-Kette muessen
    DIESELBEN Schluessel fuehren.

    Ein Tippfehler in genau einem der drei Orte faellt sonst niemandem auf: der
    Schalter im Panel taucht auf, tut aber nichts - oder eine Funktion laesst
    sich gar nicht mehr abschalten. bot.py wird bewusst als TEXT gelesen und
    nicht importiert (das zieht den halben Bot hoch)."""
    import re
    import features
    katalog = {f["key"] for f in features.CATALOG}
    assert len(katalog) == len(features.CATALOG), "doppelter Schluessel im CATALOG"

    quelle = open("bot.py", encoding="utf-8").read()

    # 1) FEATURE_LOADED = { "ki": ..., "music": ... }
    block = re.search(r"FEATURE_LOADED\s*=\s*\{(.*?)\n\}", quelle, re.S)
    assert block, "FEATURE_LOADED nicht gefunden"
    geladen = set(re.findall(r'"([a-z_]+)":', block.group(1)))
    assert geladen == katalog, (
        f"FEATURE_LOADED weicht ab: nur dort {sorted(geladen - katalog)}, "
        f"nur im CATALOG {sorted(katalog - geladen)}")

    # 2) Jeder Schalter in der Handler-Kette und den passiven Hooks: _on("key")
    benutzt = set(re.findall(r'_on\("([a-z_]+)"\)', quelle))
    benutzt |= set(re.findall(r'features\.is_on(?:_in)?\((?:[^,]+,\s*)?"([a-z_]+)"\)',
                              quelle))
    unbekannt = benutzt - katalog
    assert not unbekannt, f"bot.py fragt unbekannte Schalter ab: {sorted(unbekannt)}"

    # 3) Umgekehrt: jeder Katalog-Eintrag wird in bot.py auch WIRKLICH abgefragt -
    #    sonst steht im Panel ein Schalter, der nichts bewirkt.
    tot = katalog - benutzt
    assert not tot, f"Schalter ohne Wirkung in bot.py: {sorted(tot)}"




def test_guildcfg_roundtrip_fuer_jeden_typ():
    """Gesetzt == gelesen, fuer JEDEN Typ des Katalogs.

    get() schickt den gespeicherten Wert zur Sicherheit nochmal durch die
    Typ-Pruefung - und fuer Listen wurde daraus str([123, 456]), also die
    Tokens '[123,' und '456]'. Die fielen durch die Kanal-Erkennung, und get()
    lieferte IMMER den Standard: die Aufraeum-Kanaele waren faktisch nicht
    einstellbar."""
    guildcfg, zurueck = _cfg_frisch()
    try:
        kanal_ids = [200000000000000001, 200000000000000002]
        guild = SimpleNamespace(
            id=999, text_channels=[],
            get_channel=lambda c: SimpleNamespace(id=c) if c in kanal_ids else None)
        faelle = {
            "autodelete_channels": ("200000000000000001 200000000000000002", kanal_ids),
            "ansage_channel": ("200000000000000001", kanal_ids[0]),
            "lautstaerke": ("80", 80),
            "autodelete_sekunden": ("45", 45),
            "bayern": ("an", True),
        }
        for key, (eingabe, erwartet) in faelle.items():
            ok, gesetzt, fehler = asyncio.run(guildcfg.setzen(999, key, eingabe, guild))
            assert ok, (key, fehler)
            assert gesetzt == erwartet, (key, gesetzt)
            assert guildcfg.get(999, key) == erwartet, \
                f"{key}: gesetzt {gesetzt!r}, gelesen {guildcfg.get(999, key)!r}"
        # Abschalten muss auch wirken (nicht auf den Standard zurueckfallen).
        asyncio.run(guildcfg.setzen(999, "autodelete_channels", "aus", guild))
        assert guildcfg.get(999, "autodelete_channels") == []
    finally:
        zurueck()




def test_cmdnorm_kennt_die_befehle_aller_module():
    """KNOWN war unvollstaendig: guildcfg, giveaway und die Kurzformen aus
    music/casino/games/profil fehlten komplett. Solange ein Befehl dort fehlt,
    haelt die Aehnlichkeitssuche ihn fuer einen Vertipper und VERBIEGT ihn -
    'sendpause' wurde zu 'sendepause', 'time-out' zu 'timeout', 'naehrwert' zu
    'naehrwerte', 'rausschmeiss' zu 'rausschmeis'. Der Befehl war damit weg.

    Der Test haengt an den Befehlslisten der Module selbst, nicht an einer
    Abschrift - laeuft eine Liste auseinander, faellt es hier auf."""
    import cmdnorm
    import arbeit
    import floaktie
    import giveaway
    import guildcfg
    import lotto
    import merchant
    import profil
    import schulden
    import steal
    listen = (
        arbeit._CMDS + arbeit._LOHN_CMDS + arbeit._TOP_CMDS
        + arbeit._WORDLE_CMDS + arbeit._TAGES_CMDS
        + floaktie._CMDS + floaktie._CHART_CMDS
        + giveaway._CMDS + guildcfg.GuildConfig._CMDS + lotto._CMDS
        + merchant._CMDS + profil._CHECK_CMDS + profil._AVATAR_CMDS
        + profil._BANNER_CMDS + schulden._CMDS + steal._CMDS
    )
    fehlt = sorted({w for w in listen if w not in cmdnorm.KNOWN})
    assert not fehlt, f"fehlt in cmdnorm.KNOWN: {fehlt}"
    # Und keiner davon darf umgeschrieben werden.
    verbogen = {w: cmdnorm.normalize(w) for w in listen
                if cmdnorm.normalize(w) not in (None, w)}
    assert not verbogen, f"cmdnorm verbiegt eigene Befehle: {verbogen}"

    # Die Kurzformen, die vorher gar nicht bekannt waren - stichprobenartig,
    # aber genau die, die im Chat wirklich getippt werden.
    for wort in ("ls", "lst", "bal", "lb", "inv", "bj", "dd", "rps", "ssp",
                 "w6", "dm", "del", "img", "tts", "say", "sb", "gw",
                 "settings", "config", "unbann", "time-out", "sendpause",
                 "naehrwert", "nährwert", "quizduel", "rausschmeiss"):
        assert wort in cmdnorm.KNOWN, wort
        assert cmdnorm.normalize(f"{wort} x") is None, wort




def test_cmdnorm_versteht_englisch_und_boarisch():
    """Der Bot soll auf Deutsch, Englisch UND Boarisch hoeren. Uebersetzt wird
    dabei NUR in ALIAS/DIALECT - ein Synonym in KNOWN waere tot: KNOWN heisst
    'ist schon gueltig, nicht anfassen', das Wort ginge unveraendert an ein
    Modul, das es nicht kennt, und faellt zur KI durch (mit 'wipe' geprueft)."""
    import cmdnorm

    # 1. Struktur: jedes Ziel muss ein Modul kennen, kein Schluessel darf tot
    #    sein, und kein Schluessel darf gleichzeitig als Stopword gebremst sein.
    fehler = []
    for name, topf in (("ALIAS", cmdnorm.ALIAS), ("DIALECT", cmdnorm.DIALECT)):
        for schluessel, ziel in sorted(topf.items()):
            if ziel not in cmdnorm.KNOWN:
                fehler.append(f"{name}[{schluessel}] -> {ziel}: kennt kein Modul")
            if schluessel in cmdnorm.KNOWN:
                fehler.append(f"{name}[{schluessel}]: steht in KNOWN, Eintrag tot")
            if schluessel in cmdnorm.STOPWORDS:
                fehler.append(f"{name}[{schluessel}]: steht in STOPWORDS")
            if schluessel in cmdnorm.NUR_ALLEIN:
                # Diese gelten absichtlich nur OHNE Rest ('aus' ist sonst eine
                # der haeufigsten deutschen Praepositionen).
                if cmdnorm.normalize(schluessel) != ziel:
                    fehler.append(f"{name}[{schluessel}] schreibt allein nicht auf {ziel} um")
                if cmdnorm.normalize(f"{schluessel} x") is not None:
                    fehler.append(f"{name}[{schluessel}] greift trotz NUR_ALLEIN mit Rest")
            elif cmdnorm.normalize(f"{schluessel} x") != f"{ziel} x":
                fehler.append(f"{name}[{schluessel}] schreibt nicht auf {ziel} um")
    assert not fehler, fehler
    assert not (set(cmdnorm.ALIAS) & set(cmdnorm.DIALECT)), "Schluessel doppelt"
    assert not (cmdnorm.STOPWORDS & cmdnorm.KNOWN), "Stopword-Schutz waere tot"

    # 2. Englisch
    for rein, raus in (("wipe 50", "purge 50"), ("silence @wer", "mute @wer"),
                       ("tempmute @wer 10m", "timeout @wer 10m"),
                       ("addcoins @wer 100", "gib @wer 100"),
                       ("removemoney @wer 100", "nimm @wer 100"),
                       ("broadcast hallo", "ansage hallo"),
                       ("whisper @wer hi", "dm @wer hi"),
                       ("warnings @wer", "warns @wer"), ("cfg", "config"),
                       ("options", "einstellungen"), ("shift", "schicht"),
                       ("grind", "arbeit"), ("broke", "pleite")):
        assert cmdnorm.normalize(rein) == raus, (rein, cmdnorm.normalize(rein))

    # 3. Boarisch - quer durch alle Features
    for rein, raus in (("spuits was", "spiel was"), ("kimm eina", "komm eina"),
                       ("nomoi", "nochmal"), ("aufdrahn", "lauter"),
                       ("vaschwind", "leave"), ("goid", "coins"),
                       ("schotter", "coins"), ("kontostond", "kontostand"),
                       ("gschäft", "shop"), ("kini", "thron"),
                       ("fladern @wer", "klau @wer"), ("hackln", "arbeit"),
                       ("schuftn", "arbeit"), ("wörtl", "wordle"),
                       ("gwinnspui 5k 2h", "gewinnspiel 5k 2h"),
                       ("zoggn", "casino"), ("vawarn @wer", "verwarn @wer"),
                       ("zammrama 20", "aufräum 20"), ("schaugn @wer", "check @wer"),
                       ("eistellunga", "einstellungen"), ("schmäh", "spruch")):
        assert cmdnorm.normalize(rein) == raus, (rein, cmdnorm.normalize(rein))

    # 4. Ende zu Ende: das uebersetzte Wort muss beim echten Modul auch ANKOMMEN.
    #    'Ziel steht in KNOWN' allein reicht als Beweis nicht - hier laufen die
    #    Muster der Module selbst mit.
    import media
    import moderation
    strecken = (("wipe 50", moderation._CMD_RE),
                ("silence @wer 10m", moderation._TIMEOUT_RE),
                ("tempmute @wer 10m", moderation._TIMEOUT_RE),
                ("warnings @wer", moderation._WARNS_RE),
                ("zeichna ein drache", media.Media._GEN_RE))
    for rein, muster in strecken:
        norm = cmdnorm.normalize(rein) or rein
        assert muster.match(norm), f"{rein!r} -> {norm!r} erkennt das Modul nicht"

    # 5. Die Uebersetzungs-Toepfe sind bewusst KEINE Vertipper-Ziele. Gemessen:
    #    liesse man _fuzzy auch auf ihre Schluessel los, wuerde 'emma' zu leave,
    #    'lisa' zu leiser und 'normal' zu 'nochmal'.
    for wort in ("emma", "lisa", "normal", "weit", "hits", "geschäft",
                 "warning", "kommt", "schleicht"):
        assert cmdnorm.normalize(wort) is None, (wort, cmdnorm.normalize(wort))




def test_cmdnorm_aus_ist_nur_allein_ein_befehl():
    """'Flo aus!' heisst wirklich stop - aber 'aus' ist auch eine der
    haeufigsten deutschen Praepositionen. Nachgemessen wurde aus

        'Flo aus welchem Grund machst du das?'

    ein 'stop welchem Grund ...': die Musik ging aus, und die Frage erreichte
    die KI nie, weil der Befehl sie abgefangen hat."""
    import cmdnorm
    assert "aus" in cmdnorm.NUR_ALLEIN
    for allein in ("aus", "aus!", "AUS", " aus "):
        assert cmdnorm.normalize(allein) == "stop", allein
    for satz in ("aus welchem Grund machst du das?", "aus Spass", "aus dem Nichts",
                 "aus Versehen geloescht", "aus der Reihe"):
        assert cmdnorm.normalize(satz) is None, (satz, cmdnorm.normalize(satz))
    # Der Rest des Dialekt-Topfs bleibt unberuehrt.
    assert cmdnorm.normalize("spui was") == "spiel was"
    assert cmdnorm.normalize("hoit") == "stop"
    # Und jeder NUR_ALLEIN-Eintrag muss auch wirklich im Dialekt-Topf stehen.
    for wort in cmdnorm.NUR_ALLEIN:
        assert wort in cmdnorm.DIALECT or wort in cmdnorm.ALIAS, wort




def test_cmdnorm_laesst_alltag_und_vornamen_in_ruhe():
    """Am ganzen Wortschatz des Repos nachgemessen (nicht geraten): das hier
    waren echte Fehlgriffe der Aehnlichkeitssuche. 'Flo heisst du Flo?' hat
    einen Coin-Raub gestartet, 'pure' war ein Massenloeschen, 'nebel' ein
    Knebel, 'cash' eine Crash-Wette, 'build'/'zeichen' ein KI-Bildauftrag
    (kostet Geld), 'komma' hat Flo in den Voice geholt - und 'anne', 'frank',
    'laura', 'ines' sind Vornamen."""
    import cmdnorm
    ruhe = """pure heisst nebel anne cash mies meine ines leiche bogen leite
        schwein takte stake tanke schenken schenkt build zeichen malle komma
        frank krank trank laura chat hart swords dicke close grass pfad
        spieler zweiter passt spass spaß mach macht kannst red rede stell
        unser sehen even quite stock share item ticket option marco sven
        mats""".split()
    schlecht = {w: cmdnorm.normalize(w) for w in ruhe
                if cmdnorm.normalize(w) is not None}
    assert not schlecht, f"Alltagswoerter werden zu Befehlen: {schlecht}"

    # Die Toleranz darf davon nicht stumpf werden - echte Vertipper gehen weiter.
    for falsch, richtig in (("skpi", "skip"), ("wokr", "work"),
                            ("wrodle", "wordle"), ("timout", "timeout"),
                            ("admiin", "admin"), ("lottoo", "lotto"),
                            ("laut", "lautr"), ("verlassen", "verlasse")):
        assert cmdnorm.normalize(falsch) == richtig, (falsch,
                                                     cmdnorm.normalize(falsch))




def test_bayrisch_versteht_die_standard_schreibweise():
    """'bayerisch' (mit e) schaltete gar nichts - der Regex kannte nur
    'bayrisch' und 'boarisch'."""
    import bayern
    rx = bayern.instance._TOGGLE_RE
    for schreibweise in ("bayrisch an", "bayerisch an", "bairisch an",
                         "baierisch aus", "boarisch an", "dialekt aus"):
        assert rx.match(schreibweise), schreibweise
    for kein in ("banane", "bayern muenchen", "bay"):
        assert not rx.match(kein), kein
    # 'sprich/red bayerisch' ist die natuerlichste Formulierung - die landete
    # frueher bei voicegags in der Sprachausgabe statt beim Dialekt.
    lose = bayern.instance._TOGGLE_LOSE_RE
    for satz in ("sprich bayerisch", "red mal boarisch", "schreib auf bayrisch",
                 "antworte bitte bayerisch", "sprich bayerisch aus"):
        assert lose.match(satz), satz
    assert not lose.match("sprich nicht so laut")




def test_flo_pingt_niemals_everyone():
    """Mehrere Befehle geben die Nutzereingabe WOERTLICH zurueck - gemessen:
    economy 'setze', guildcfg 'einstellung', profil 'avatar'. Und zwar als
    Klartext, nicht im Embed (Embeds pingen nicht).

    bot.py setzte nirgends allowed_mentions. Damit machte "Flo setze @everyone"
    aus jedem Nutzer einen Server-Durchsager."""
    quelle = open("bot.py", encoding="utf-8").read()
    stelle = quelle.index("client = FloBot(")
    block = quelle[stelle:stelle + 1200]
    assert "allowed_mentions" in block, (
        "der Client wird ohne allowed_mentions gebaut - Flo kann @everyone anpingen")
    assert "everyone=False" in block and "roles=False" in block, block[:400]
    # Personen-Erwaehnungen muessen ANBLEIBEN, sonst kann Flo niemanden ansprechen.
    assert "users=True" in block, block[:400]

    # Gegenprobe an der Wirklichkeit: die drei Befehle geben die Eingabe wirklich
    # woertlich zurueck. Faellt das weg, ist der Schutz trotzdem richtig - aber
    # dieser Test soll wissen, wovon er redet.
    import guildcfg
    economy.setup()
    guildcfg.setup()
    woertlich = []
    for modul, befehl in ((economy, "setze"), (guildcfg, "einstellung")):
        antwort = asyncio.run(modul.handle(_rauch_nachricht(f"{befehl} @everyone")))
        if isinstance(antwort, str) and "@everyone" in antwort:
            woertlich.append(f"{modul.__name__} {befehl}")
    assert woertlich, ("kein Befehl gibt die Eingabe mehr woertlich zurueck - "
                       "dann diesen Test anpassen, nicht den Schutz entfernen")




def test_guildcfg_sagt_allen_bescheid_die_daran_haengen():
    """Der Verteiler hinter dem Nachziehen.

    Frueher stand in _nachziehen eine if-Kette mit genau einem Eintrag
    (praefix). Jeder weitere Verbraucher haette sie erweitern muessen - und
    niemand, der ein Modul aendert, schaut in guildcfg nach."""
    import guildcfg
    gerufen = []
    guildcfg.horcht_auf("testschalter", gerufen.append)
    # Doppelte Anmeldung derselben Funktion darf nicht doppelt feuern
    # (setup() laeuft in Tests mehrfach).
    guildcfg.horcht_auf("testschalter", gerufen.append)
    try:
        guildcfg.instance._nachziehen(77, "testschalter")
        assert gerufen == [77], gerufen

        # Ein kaputter Hoerer darf die Einstellung NICHT umwerfen - sonst
        # scheitert das Speichern an einem Nebeneffekt.
        def kaputt(_gid):
            raise RuntimeError("absichtlich")

        guildcfg.horcht_auf("testschalter", kaputt)
        guildcfg.instance._nachziehen(88, "testschalter")
        assert gerufen == [77, 88], gerufen
    finally:
        guildcfg._HOERER.pop("testschalter", None)




def test_keine_server_einstellung_ohne_rechtepruefung():
    """Wer eine Server-Einstellung schreibt, muss vorher fragen, ob er darf.

    bayern.py hat guildcfg.setzen ohne jede Pruefung gerufen - ein beilaeufiges
    "flo red mal bayerisch" hat damit fuer den ganzen Server umgestellt. Der
    Test haelt das fuer ALLE Module fest, nicht nur fuer das eine."""
    import glob
    import re as _re
    schuldige = []
    for datei in sorted(glob.glob("*.py")):
        if datei.startswith("test_") or datei in ("guildcfg.py", "webpanel.py"):
            continue        # guildcfg prueft selbst, das Panel hat _guard
        quelle = open(datei, encoding="utf-8").read()
        # Ausnahmen an der FUNKTION festmachen, nicht an einem Textfenster:
        # eine lange Erklaerung davor hat den Namen sonst aus dem Fenster
        # geschoben und der Test schlug grundlos an.
        ohne_nutzer = ("altlast_migrieren",)   # laeuft beim Start, ohne Person
        for treffer in _re.finditer(r"guildcfg\.setzen\(", quelle):
            davor = quelle[:treffer.start()]
            umgebung = _re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", davor,
                                   _re.MULTILINE)
            if umgebung and umgebung[-1] in ohne_nutzer:
                continue
            if "darf(" in davor[-900:] or "_darf(" in davor[-900:]:
                continue
            zeile = davor.count("\n") + 1
            schuldige.append(f"{datei}:{zeile} (in {umgebung[-1] if umgebung else '?'})")
    assert not schuldige, (
        "Diese Stellen aendern eine Server-Einstellung, ohne die Rechte zu "
        "pruefen:\n  " + "\n  ".join(schuldige))




def test_funktionen_lassen_sich_auch_in_discord_schalten():
    """Die Funktions-Schalter gab es NUR im Web-Panel.

    Wer kein Panel offen hatte, konnte auf seinem eigenen Server das Casino
    nicht abschalten. Jetzt geht beides - und beides schreibt dieselbe Stelle."""
    import features
    import guildcfg
    gesetzt = {}
    alt_set, alt_on, alt_in = features.set_guild, features.is_on, features.is_on_in

    async def fake_set(gid, key, on):
        if key not in {e["key"] for e in features.CATALOG}:
            return None
        gesetzt[(int(gid), key)] = bool(on)
        return bool(on)

    def msg(text, darf=True):
        m = SimpleNamespace(
            content=f"Flo {text}", mentions=[],
            author=SimpleNamespace(
                id=5, bot=False, display_name="T",
                guild_permissions=SimpleNamespace(manage_guild=darf)),
            guild=SimpleNamespace(id=77, name="S"))
        m.channel = SimpleNamespace(send=lambda **kw: _als_coro(None))
        return m

    alt_enabled = guildcfg.instance._enabled
    guildcfg.instance._enabled = True
    features.set_guild = fake_set
    features.is_on = lambda key: True
    features.is_on_in = lambda gid, key: gesetzt.get((int(gid), key), True)
    try:
        antwort = asyncio.run(guildcfg.handle(msg("funktion casino aus")))
        assert gesetzt.get((77, "casino")) is False, gesetzt
        assert "aus" in str(antwort)

        # Ohne Recht wird NICHTS umgeschaltet.
        gesetzt.clear()
        antwort = asyncio.run(guildcfg.handle(msg("funktion casino aus", darf=False)))
        assert not gesetzt, "ohne Recht wurde geschaltet"
        assert "verwalten" in str(antwort)

        # Unbekannter Schluessel wird benannt, nicht still verschluckt.
        antwort = asyncio.run(guildcfg.handle(msg("funktion gibtsnicht aus")))
        assert "kenne ich nicht" in str(antwort)

        # Der globale Not-Aus gewinnt weiter.
        features.is_on = lambda key: False
        antwort = asyncio.run(guildcfg.handle(msg("funktion casino an")))
        assert "global" in str(antwort)

        # Uebersicht ohne Argument geht auch ohne Rechte (nur ansehen).
        assert asyncio.run(guildcfg.handle(msg("funktionen", darf=False))) is guildcfg.HANDLED
    finally:
        features.set_guild, features.is_on, features.is_on_in = alt_set, alt_on, alt_in
        guildcfg.instance._enabled = alt_enabled




def test_einstellungen_ordnen_jeden_wert_ein():
    """Die eine Liste darf keinen Wert unterschlagen.

    einstellungen.py fuehrt guildcfg (26 Werte) und features (24 Schalter) zu
    EINER nach Funktionen geordneten Liste zusammen - das ist die Antwort auf
    'manche einstellungen sind da und andere da'. Die Zuordnung ist von Hand
    gepflegt, weil sie nirgends im Code steht: sie ergibt sich daraus, was ein
    Wert tatsaechlich beeinflusst.

    Von Hand gepflegt heisst: sie verrottet, sobald jemand einen Schluessel
    dazulegt und hier nichts eintraegt. Der Wert waere dann im Panel
    unsichtbar - genau die Sorte stiller Verlust, gegen die dieser Umbau
    laeuft. Deshalb prueft dieser Test beide Richtungen und beide Kataloge.
    """
    import einstellungen

    vergessen = einstellungen.vergessene_werte()
    assert not vergessen, (
        f"Diese guildcfg-Werte sind in keinem Abschnitt und damit im Panel "
        f"unsichtbar: {vergessen}. In einstellungen.ZUORDNUNG einordnen (oder "
        f"in GRUNDLEGEND, wenn sie zu keiner Funktion gehoeren).")
    erfunden = einstellungen.erfundene_werte()
    assert not erfunden, (
        f"Diese Namen stehen in einstellungen.ZUORDNUNG, aber nicht in "
        f"guildcfg.KATALOG: {erfunden}")
    erfundene_f = einstellungen.erfundene_funktionen()
    assert not erfundene_f, (
        f"Diese Funktionsnamen stehen in einstellungen.ZUORDNUNG, aber nicht "
        f"in features.CATALOG: {erfundene_f}")

    # Und die Liste muss wirklich jeden Schalter zeigen, nicht nur die mit Werten.
    baum = einstellungen.als_dicts(0, {})
    gezeigt = {a["key"] for a in baum}
    import features
    fehlt = sorted({f["key"] for f in features.CATALOG} - gezeigt)
    assert not fehlt, f"Diese Funktionen fehlen in der Liste: {fehlt}"




def test_einstellungen_aendern_das_discord_nicht():
    """Die neue Liste ist NUR fuer das Panel.

    'Flo einstellungen' und 'Flo funktionen' im Discord sollen sich nicht
    bewegen - der Betreiber hat ausdruecklich gesagt, im Discord soll alles so
    bleiben. Deshalb darf guildcfg.handle nichts aus einstellungen.py holen.
    """
    quelle = open("guildcfg.py", encoding="utf-8").read()
    assert "import einstellungen" not in quelle, (
        "guildcfg benutzt einstellungen.py - dann wandert die neue Gruppierung "
        "ins Discord. Sie ist aber nur fuer das Panel gedacht.")
    # Der umgekehrte Weg ist richtig und muss bleiben.
    neu = open("einstellungen.py", encoding="utf-8").read()
    assert "import guildcfg" in neu and "import features" in neu


if __name__ == "__main__":
    run(globals())
