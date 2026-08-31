"""Profil-Lookup, Namensverlauf, Avatare.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur profil      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeChannel, _FakeStore, _embed_text, _fake_person, _profil_frisch,
    _with_economy)



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




def test_profil_bild_gross_und_ohne_kreis():
    """Der Kern des Befehls: das Bild kommt in 4096 px und als set_image.

    set_thumbnail und das Autor-Icon zeigt Discord RUND - nur set_image bleibt
    rechteckig. Ein Wechsel auf thumbnail waere optisch subtil und genau der
    Fehler, den dieser Test verhindert."""
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person()
        bilder = profil.instance._bilder(wer, None)
        emb = profil.instance._profil_embed(wer, None, bilder,
                                            SimpleNamespace(id=999, name="S"))
        assert emb.image.url and "size=4096" in emb.image.url, emb.image.url
        assert not emb.thumbnail.url, \
            "Profilbild darf NICHT als (runder) Thumbnail gesetzt sein"
        # Direktlinks stehen auch als Text drin - zum Herunterladen.
        text = _embed_text(emb)
        assert "size=4096" in text
        # Und der Fussnoten-Hinweis nennt die Aufloesung.
        assert "4096" in (emb.footer.text or "")
    finally:
        zurueck()




def test_profil_zeigt_keinen_online_status():
    """Ohne presences-Intent meldet discord.py JEDEN als offline.

    Das waere kein sichtbarer Fehler, sondern eine stille Falschaussage -
    deshalb darf im Profil ueberhaupt kein Status/keine Aktivitaet stehen."""
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person()
        # Der Bot wuerde hier 'offline' sehen, obwohl die Person online ist.
        wer.status = "offline"
        wer.activities = ()
        emb = profil.instance._profil_embed(wer, None,
                                            profil.instance._bilder(wer, None),
                                            SimpleNamespace(id=999, name="S"))
        # Nur Titel/Beschreibung/Felder pruefen - die FUSSZEILE sagt bewusst,
        # dass der Status fehlt, und darf das Wort deshalb enthalten.
        inhalt = " ".join([emb.title or "", emb.description or ""]
                          + [f"{f.name} {f.value}" for f in emb.fields]).lower()
        for verboten in ("offline", "online", "abwesend", "beschäftigt", "spielt gerade"):
            assert verboten not in inhalt, f"'{verboten}' steht im Profil - das waere gelogen"
        # ... und die Fusszeile erklaert, warum da nichts steht.
        assert "status" in (emb.footer.text or "").lower()
    finally:
        zurueck()




def test_profil_namensverlauf_merkt_nur_aenderungen():
    """Discord fuehrt keinen Namensverlauf - Flo schreibt selbst mit.

    Geschrieben werden darf NUR bei echten Aenderungen: der Hook laeuft bei
    jeder Nachricht."""
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person(name="secoolio", global_name="Secoolio", nick=None)

        # Erste Sichtung legt an.
        assert profil.notiere(wer, 999) is True
        # Dieselbe Person nochmal: KEINE Aenderung, kein Schreiben.
        assert profil.notiere(wer, 999) is False
        assert profil.notiere(wer, 999) is False

        handles, anzeigen, nicks = profil.verlauf(wer.id, 999)
        assert [h[0] for h in handles] == ["secoolio"]
        assert [a[0] for a in anzeigen] == ["Secoolio"]

        # Handle geaendert -> ein Eintrag mehr, der alte bleibt stehen.
        wer.name = "flotus"
        assert profil.notiere(wer, 999) is True
        handles, _a, _n = profil.verlauf(wer.id, 999)
        assert [h[0] for h in handles] == ["secoolio", "flotus"]

        # Server-Nickname gilt NUR fuer diesen Server.
        wer.nick = "Chef"
        assert profil.notiere(wer, 999) is True
        _h, _a, nicks999 = profil.verlauf(wer.id, 999)
        _h, _a, nicks111 = profil.verlauf(wer.id, 111)
        assert [n[0] for n in nicks999][-1] == "Chef"
        assert nicks111 == [], "Nickname des einen Servers taucht beim anderen auf"

        # Bots bekommen keinen Verlauf.
        assert profil.notiere(_fake_person(uid=42, bot=True), 999) is False

        # Der Verlauf ist gedeckelt, sonst waechst die Datei ewig.
        for i in range(profil.VERLAUF_MAX + 10):
            wer.name = f"name{i}"
            profil.notiere(wer, 999)
        handles, _a, _n = profil.verlauf(wer.id, 999)
        assert len(handles) == profil.VERLAUF_MAX, len(handles)

        # Gespeichert wird gesammelt: flush schreibt nur, wenn etwas anliegt.
        assert asyncio.run(profil.flush()) is True
        assert asyncio.run(profil.flush()) is False
    finally:
        zurueck()




def test_profil_erkennt_seine_befehle():
    """check/profil/avatar/banner ja - normales Gerede nein."""
    profil, zurueck = _profil_frisch()
    try:
        kanal = _FakeChannel()

        async def antwort(text, mentions=None):
            msg = SimpleNamespace(
                content=f"Flo {text}", mentions=mentions or [],
                author=_fake_person(uid=5, name="ich"), reference=None,
                guild=SimpleNamespace(id=999, name="S", members=[],
                                      me=SimpleNamespace(id=1)),
                channel=kanal)
            return await profil.handle(msg)

        # Kein Profil-Befehl -> None, damit der naechste Handler drankommt.
        for kein in ("blackjack 100", "spiel was", "wie geht's", "checke mal ab"):
            profil.instance._cooldown.clear()
            assert asyncio.run(antwort(kein)) is None, kein

        # MEHRDEUTIGE Woerter ohne Ziel sind Gerede, kein Befehl: "check mal ob
        # das laeuft", "pb ist kaputt". Da muss die Kette weiterlaufen, sonst
        # beantwortet Flo jeden solchen Satz mit einem Hinweistext und die KI
        # kommt nie dran.
        for gerede in ("check", "user", "pb", "av",
                       "check mal ob das laeuft", "check das bitte kurz"):
            profil.instance._cooldown.clear()
            assert asyncio.run(antwort(gerede)) is None, gerede

        # "bild" gehoert dem Bildgenerator in media.py - profil darf es NICHT
        # anfassen, sonst ist "Flo bild ein Drache aus Neon" tot.
        assert "bild" not in profil._AVATAR_CMDS
        profil.instance._cooldown.clear()
        assert asyncio.run(antwort("bild ein Drache aus Neon")) is None

        # EINDEUTIGE Befehle ohne Ziel zeigen das eigene Profil.
        for cmd in ("profil", "whois", "steckbrief", "avatar", "userinfo"):
            profil.instance._cooldown.clear()
            del kanal.sent[:]
            assert asyncio.run(antwort(cmd)) is profil.HANDLED, cmd
            assert kanal.sent and kanal.sent[-1]["embeds"], cmd

        # Und "check" MIT Ziel ist selbstverstaendlich ein Befehl.
        ziel_person = _fake_person(uid=7, name="ziel")
        profil.instance._cooldown.clear()
        del kanal.sent[:]
        assert asyncio.run(antwort("check", [ziel_person])) is profil.HANDLED
        assert kanal.sent and kanal.sent[-1]["embeds"]

        # Ohne Banner sagt Flo das, statt ein leeres Embed zu schicken.
        profil.instance._cooldown.clear()
        res = asyncio.run(antwort("banner"))
        assert isinstance(res, str) and "Banner" in res
    finally:
        zurueck()




def test_profil_findet_das_richtige_ziel():
    """Erwaehnung schlaegt alles - und die Erwaehnung des BOTS ist der Ausloeser,
    nicht das Ziel ('@Flo check @wer')."""
    profil, zurueck = _profil_frisch()
    try:
        ich = _fake_person(uid=5, name="ich")
        ziel = _fake_person(uid=7, name="ziel")
        flo = _fake_person(uid=1, name="flo", bot=True)
        guild = SimpleNamespace(id=999, name="S", members=[ich, ziel],
                                me=SimpleNamespace(id=1))

        def msg(text, mentions):
            return SimpleNamespace(content=f"Flo {text}", mentions=mentions,
                                   author=ich, guild=guild, reference=None,
                                   channel=_FakeChannel())

        # Ohne alles: ich selbst.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", []), ""))
        assert wer is ich
        # Mit Erwaehnung: die Person.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", [ziel]), ""))
        assert wer is ziel
        # Bot-Erwaehnung davor wird uebersprungen.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", [flo, ziel]), ""))
        assert wer is ziel
        # Nur der Bot erwaehnt -> dann ist der Bot gemeint.
        wer, _p = asyncio.run(profil.instance._ziel(msg("check", [flo]), ""))
        assert wer is flo
        # Name im Text: im Cache suchen (Notnagel ohne Members-Intent).
        wer, _p = asyncio.run(profil.instance._ziel(msg("check ziel", []), "ziel"))
        assert wer is ziel
        # Unbekannter Name: ehrliche Absage statt "gibt es nicht".
        wer, problem = asyncio.run(profil.instance._ziel(msg("check xyz", []), "xyz"))
        assert wer is None and "Erwähnung" in problem
    finally:
        zurueck()




def test_profil_bremst_und_zeigt_flo_daten():
    """Ein Aufruf kann zwei REST-Aufrufe ausloesen - deshalb ein Cooldown.
    Und was Flo selbst weiss, gehoert mit ins Profil."""
    profil, zurueck = _profil_frisch()
    restore_eco = _with_economy({77: 4242})
    try:
        # Cooldown: der zweite Aufruf in Folge wird abgewiesen.
        frei, _w = profil.instance._darf_schon(5)
        assert frei is True
        frei, warten = profil.instance._darf_schon(5)
        assert frei is False and warten > 0
        # Eine ANDERE Person ist davon nicht betroffen.
        assert profil.instance._darf_schon(6)[0] is True

        # Flo-Daten: Level/Coins der Wirtschaft tauchen im Profil auf.
        economy.instance._profile(77)["msgs"] = 1234
        wer = _fake_person(uid=77)
        emb = profil.instance._profil_embed(wer, None,
                                            profil.instance._bilder(wer, None),
                                            SimpleNamespace(id=999, name="S"))
        text = _embed_text(emb)
        assert "4.242" in text or "4242" in text, text
        assert "1.234" in text or "1234" in text, text
    finally:
        restore_eco()
        zurueck()




def test_namensverlauf_wird_wirklich_gespeichert():
    """Der Verlauf muss in einem Loop wegschreiben, der IMMER laeuft.

    Zuerst hing flush() im Voice-Takt - und der startet nur mit eingeschalteter
    Wirtschaft (bot.py: 'if ECONOMY_ENABLED and not self.voice_xp_loop...').
    Ohne economy waere der Namensverlauf also nie auf der Platte gelandet und
    bei jedem Neustart weg gewesen. bot.py wird als TEXT geprueft, damit der
    Test den halben Bot nicht hochziehen muss."""
    import re
    quelle = open("bot.py", encoding="utf-8").read()

    # In welcher Loop-Methode steht der flush-Aufruf?
    methoden = re.findall(r"\n    async def (\w+)\(self\):(.*?)(?=\n    (?:async )?def |\n    @)",
                          quelle, re.S)
    drin = [name for name, rumpf in methoden if "profil.flush()" in rumpf]
    assert len(drin) == 1, f"profil.flush() sollte in genau einem Loop stehen, ist in {drin}"
    loop = drin[0]

    # Und wird dieser Loop bedingungslos gestartet?
    start = re.search(r"\n(\s*)if ([^\n]*?)not self\.%s\.is_running\(\):" % loop, quelle)
    assert start, f"{loop} wird nirgends gestartet"
    bedingung = start.group(2).strip()
    assert bedingung == "", (
        f"{loop} startet nur unter der Bedingung '{bedingung}' - dann wuerde der "
        f"Namensverlauf unter Umstaenden nie gespeichert")




def test_profil_loest_kein_bot_neuladen_aus():
    """profil.py darf bot.py NIEMALS erstmalig importieren.

    Ein 'import bot' fuehrt das Modul aus, wenn es noch nicht geladen ist - und
    damit saemtliche setup()-Aufrufe erneut. Gemessen: dabei bekommt JEDES Modul
    einen frischen Speicher, mitten im Betrieb bzw. mitten im Testlauf. Genau
    daran sind Namensverlauf und Flo-Daten aus dem fertigen Embed verschwunden.
    Laeuft der Bot, steht er ohnehin in sys.modules."""
    import sys
    profil, zurueck = _profil_frisch()
    hatte_bot = "bot" in sys.modules
    try:
        ziel = _fake_person(uid=777, name="alt", global_name="Alt", nick="Nick")
        profil.notiere(ziel, 999)
        ziel.name = "neu"
        profil.notiere(ziel, 999)

        kanal = _FakeChannel()
        msg = SimpleNamespace(
            content="Flo check", mentions=[ziel], author=_fake_person(uid=5),
            reference=None, channel=kanal,
            guild=SimpleNamespace(id=999, name="S", members=[], me=SimpleNamespace(id=1)))
        assert asyncio.run(profil.handle(msg)) is profil.HANDLED

        if not hatte_bot:
            assert "bot" not in sys.modules, \
                "profil.py hat bot.py importiert und damit alle Module neu aufgesetzt"

        # Und die Felder, die durch genau diesen Nebeneffekt verschwunden waren,
        # stehen wirklich im fertigen Embed - nicht nur in der Einzelfunktion.
        namen = [f.name for f in kanal.sent[-1]["embeds"][0].fields]
        assert any("Frühere Namen" in n for n in namen), namen
    finally:
        zurueck()




def test_profil_befehle_kollidieren_nicht():
    """Die drei Kollisionen, die der neue Lookup ausgeloest hatte.

    1) 'banner' war cmdnorm unbekannt. Die Aehnlichkeitssuche korrigierte es auf
       'banne' - und da Moderation auf Platz 2 der Handler-Kette steht, hat
       'Flo banner @wer' die Person GEBANNT statt ihr Banner zu zeigen.
    2) 'bild' gehoert dem Bildgenerator in media.py.
    3) 'profil' fing frueher admin.py fuer den Besitzer ab."""
    import cmdnorm
    import media
    import moderation
    import profil

    # 1) KEIN Profil-Befehl darf auf einen fremden Befehl korrigiert werden.
    for wort in profil._CHECK_CMDS + profil._AVATAR_CMDS + profil._BANNER_CMDS:
        korrigiert = cmdnorm.normalize(wort)
        assert korrigiert in (None, wort), (
            f"cmdnorm macht aus '{wort}' -> '{korrigiert}'")
    # ... und die Moderation darf 'banner' nicht mehr als Bann lesen.
    for text in ("banner", "banner <@777>", "profilbanner"):
        norm = cmdnorm.normalize(text) or text
        assert moderation.classify(norm) is None, (text, norm)

    # 2) 'bild' bleibt beim Bildgenerator.
    assert "bild" not in profil._AVATAR_CMDS + profil._CHECK_CMDS
    assert media.Media._GEN_RE.match("bild ein Drache aus Neon")

    # 3) admin.py beansprucht 'profil' nicht mehr.
    quelle = open("admin.py", encoding="utf-8").read()
    assert '"profil"' not in quelle, "admin faengt 'profil' wieder vor profil.py ab"




def test_profil_namensverlauf_zeigt_das_richtige_datum():
    """'bis' muss das ENDE eines Namens sein.

    _merke schreibt in 't' den Moment, in dem ein Name ANFING. Vorher stand
    genau dieses 't' hinter dem Wort 'bis' - jede Zeile war damit um einen
    kompletten Eintrag zu frueh, und beim aeltesten Namen stand sogar der Tag,
    an dem Flo die Person ueberhaupt zum ersten Mal gesehen hat."""
    import unittest.mock as mock
    profil, zurueck = _profil_frisch()
    try:
        wer = _fake_person(uid=1, name="start", global_name="G", nick=None)
        zeiten = {"alt": 1_600_000_000, "mittel": 1_750_000_000, "neu": 1_755_000_000}
        for name, t in (("alt", zeiten["alt"]), ("mittel", zeiten["mittel"]),
                        ("neu", zeiten["neu"])):
            wer.name = name
            with mock.patch("time.time", lambda t=t: t):
                profil.notiere(wer, 999)

        text = profil.instance._verlauf_feld(wer, 999)
        # 'alt' galt bis zu dem Moment, in dem 'mittel' anfing - NICHT bis zu
        # seinem eigenen Anfang.
        assert f"„alt“ (bis <t:{zeiten['mittel']}:d>)" in text, text
        assert f"„mittel“ (bis <t:{zeiten['neu']}:d>)" in text, text
        # Der eigene Anfangszeitpunkt darf NIRGENDS als 'bis' auftauchen.
        assert f"„alt“ (bis <t:{zeiten['alt']}:d>)" not in text
        # Der aktuelle Name ist kein "frueherer".
        assert "„neu“" not in text
        # Und es steht dabei, ab wann Flo ueberhaupt mitschreibt.
        assert "seit" in text
    finally:
        zurueck()




def test_profil_haelt_muell_und_grenzfaelle_aus():
    """Kaputte Datei, Emoji-IDs, fehlende Bilder, 0 als Deckel."""
    import profil
    profil_m, zurueck = _profil_frisch()
    try:
        # 1) Handgeschriebener Muell in profil.json darf nichts umbringen.
        profil.instance._store = _FakeStore({"users": {"5": {"handle": None,
                                                             "nick": "kaputt"}},
                                             "seit": "gestern"})
        assert profil.verlauf(5, 999) == ([], [], [])
        wer = _fake_person(uid=5, name="a", global_name="A", nick="N")
        profil.notiere(wer, 999)          # darf nicht fliegen
        assert isinstance(profil.instance._store.data["users"]["5"]["handle"], list)

        # 2) Deckel: 0 hiess frueher "unbegrenzt" ([:-0] ist eine leere Scheibe).
        assert profil.VERLAUF_MAX >= 1
        assert profil.USER_MAX >= 50

        # 3) Emoji-, Rollen- und Kanal-IDs sind KEINE Konten - sie duerfen
        #    keine REST-Aufrufe ausloesen.
        gerufen = []

        class Guild:
            id = 999
            name = "S"
            members = []
            me = SimpleNamespace(id=1)

            def get_channel(self, _c):
                return None

            def get_member(self, _u):
                gerufen.append("get_member")
                return None

        msg = SimpleNamespace(content="Flo check", mentions=[], reference=None,
                              author=_fake_person(uid=5), guild=Guild(),
                              channel=_FakeChannel())
        for markup in ("<:katze:123456789012345678>",
                       "<#123456789012345678>", "<@&123456789012345678>"):
            gerufen.clear()
            wer2, _problem = asyncio.run(profil.instance._ziel(msg, markup))
            assert gerufen == [], f"{markup} wurde als Konto behandelt"

        # 4) Ohne Bild kein '[Direktlink](None)'.
        assert profil.instance._bild_embed(wer, None, "x", None) is None
    finally:
        zurueck()


if __name__ == "__main__":
    run(globals())
