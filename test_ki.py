"""KI-Antworten, Persona, Gedaechtnis und die Diagnose.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur ki      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _FalscherAnbieter, _KiAnbieter, _KiAntwort, _KiFehler,
    _als_coro, _cfg_frisch, _embed_text, _gehirn_frisch, _gehirn_msg,
    _ki_arzt_lauf, _ki_frisch, _with_economy)



# --- Bot-Hass ------------------------------------------------------------------
def test_bot_beef():
    import ai
    import fun
    # Persona traegt den Bot-Hass.
    assert "verachtest" in ai.instance._system_prompt().lower()
    # Roast-Sprueche formatieren sauber mit dem Namen des Rivalen.
    assert "NervBot" in fun._BOT_ROASTS[0].format(name="NervBot")
    assert hasattr(fun, "maybe_roast_bot")




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
        # Im Leerlauf dreht es ins Minus - dafuer muss der Kurs aber UEBER dem
        # Grundwert stehen. Steht er darunter, faellt er zu Recht nicht (und das
        # Momentum bleibt bei 0), siehe boden_base().
        fa._store.data["base"] = fa.boden_base() * 3.0
        fa._sync_price()
        for _ in range(90):
            fa._activity_tick(0, 0, dt=20.0)
        assert fa._store.data["mom"] < 0, fa._store.data["mom"]

        # 3) TAKTUNABHAENGIG: 60 s und 20 s kommen am Ende auf dasselbe Niveau.
        #    WICHTIG: derselbe Chat-VERKEHR, nicht dieselbe Zahl je Takt. Leute
        #    und Streams sind Zustaende, Nachrichten sind eine Zaehlung ueber den
        #    Takt - bei 20 s kommen entsprechend weniger je Takt an. Vorher stand
        #    hier stur "4", also bei 20 s der dreifache Verkehr; das hat die
        #    doppelte Division im Code zufaellig ausgeglichen und den Test aus dem
        #    falschen Grund bestehen lassen.
        werte = {}
        for dt in (60.0, 20.0):
            ergebnisse = []
            for lauf in range(12):
                random.seed(700 + lauf)
                frisch()
                rest = 0.0
                for _ in range(int(3 * 3600 / dt)):
                    rest += 3.0 * dt / 60.0          # 3 Nachrichten je MINUTE
                    # 3/min geht bei 60 s UND 20 s glatt auf. Bei 4/min ergaebe
                    # sich im 20-s-Takt das Muster 1,1,2 - und die bewusst
                    # asymmetrische Glaettung (schnell rauf, langsam runter)
                    # hebt bei schwankender Eingabe den Mittelwert an.
                    n = int(rest)
                    rest -= n
                    fa._activity_tick(3, n, streams=1, dt=dt)
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




def test_ki_leere_antwort_bleibt_nicht_spurlos():
    """Am echten Server aufgefallen: die Diagnose meldete

        4. Echter Chat-Aufruf
          OK    Antwort nach 0.4s: ''

    Also OK - bei einer LEEREN Antwort. gpt-oss & Co. denken erst und schreiben
    dann; mit einem knappen Token-Budget geht alles ins Denken. Im Betrieb sagt
    Flo dann "Dazu faellt mir gerade nichts ein" und im Log stand NICHTS - das
    sieht wie Unlust aus und ist in Wahrheit eine zu enge Grenze."""
    import ai
    import logging

    puffer = io.StringIO()
    griff = logging.StreamHandler(puffer)
    protokoll = logging.getLogger("dcbot.ai")
    protokoll.addHandler(griff)
    alt_stufe = protokoll.level
    protokoll.setLevel(logging.INFO)
    try:
        flo, _ = _ki_frisch([_KiAntwort("")])          # Modell sagt nichts
        antwort = asyncio.run(flo.ask_flo("hi"))
    finally:
        protokoll.removeHandler(griff)
        protokoll.setLevel(alt_stufe)

    assert antwort == "Dazu faellt mir gerade nichts ein.", antwort
    text = puffer.getvalue()
    assert "leere Antwort" in text, f"die leere Antwort bleibt spurlos: {text!r}"
    # Muss mit 'k l' auffindbar sein - also dieselbe Marke wie alle KI-Fehler.
    assert "KI-Fehler:" in text

    # Und der Arzt darf eine leere Antwort nicht mehr als OK durchwinken.
    import tools_ki_check
    quelle = inspect.getsource(tools_ki_check.KiCheck.aufruf_pruefen)
    assert "OHNE Text" in quelle, "der Arzt meldet eine leere Antwort wieder als OK"
    assert '"max_tokens": 5,' not in inspect.getsource(tools_ki_check), (
        "die Probe gibt dem Modell wieder zu wenig Platz")




def test_ki_denk_aufwand_nur_wenn_gesetzt():
    """reasoning_effort kennen nur Denk-Modelle. Immer mitzuschicken wuerde jede
    Anfrage an ein normales Modell mit HTTP 400 abwuergen - der Schalter darf
    also nur in die Anfrage, wenn jemand ihn ausdruecklich gesetzt hat."""
    import ai
    alt_env = os.environ.get("LLM_REASONING_EFFORT")
    try:
        for wert, erwartet in (("", None), ("low", "low"), ("HIGH", "high"),
                               ("quatsch", None)):
            if wert:
                os.environ["LLM_REASONING_EFFORT"] = wert
            else:
                os.environ.pop("LLM_REASONING_EFFORT", None)
            flo, anbieter = _ki_frisch([_KiAntwort("ok")])
            flo._denk_aufwand = ""
            flo._denk_aufwand = (wert.lower() if wert.lower() in
                                 ("low", "medium", "high") else "")
            asyncio.run(flo.ask_flo("hi"))
            gesendet = anbieter.letzte_kwargs.get("reasoning_effort")
            assert gesendet == erwartet, (wert, gesendet, erwartet)
    finally:
        if alt_env is None:
            os.environ.pop("LLM_REASONING_EFFORT", None)
        else:
            os.environ["LLM_REASONING_EFFORT"] = alt_env




def test_arzt_meldet_sich_wie_der_bot():
    """Am echten Server passiert: Cloudflare sperrte die nackte Python-Kennung,
    der Bot lief laengst - und die Diagnose meldete trotzdem Alarm und empfahl
    einen .env-Eintrag, den niemand braucht.

        Python (Standard)  -> blockiert     <- der ARZT
        openai-Paket       -> kommt DURCH   <- der BOT

    Ein Arzt, der sich anders meldet als der Patient, misst etwas anderes.
    Also schickt er jetzt dieselbe Kennung wie Flo - abgelesen beim echten
    Client, nicht geraten (sie haengt an der Paketversion)."""
    import tools_ki_check
    a = tools_ki_check.KiCheck()
    a.base = "https://api.example.invalid/v1"

    quelle = inspect.getsource(tools_ki_check.KiCheck._anfrage)
    assert "self.bot_signatur()" in quelle, (
        "der Arzt benutzt wieder nicht die Kennung des Bots")

    # LLM_USER_AGENT hat Vorrang - das ist der Hebel, den die Diagnose empfiehlt.
    alt_env = os.environ.get("LLM_USER_AGENT")
    try:
        os.environ["LLM_USER_AGENT"] = "Testkennung/1.0"
        a._bot_ua = None
        assert a.bot_signatur() == "Testkennung/1.0"
        # Ohne Vorgabe wird sie beim echten openai-Client abgelesen.
        os.environ.pop("LLM_USER_AGENT")
        a._bot_ua = None
        gelesen = a.bot_signatur()
        try:
            import openai  # noqa: F401
        except ImportError:
            return                              # ohne das Paket nicht pruefbar
        assert gelesen, "keine Kennung ermittelt"
        assert "urllib" not in gelesen.lower(), (
            f"der Arzt meldet sich als urllib ({gelesen}) - genau das war der Fehler")
    finally:
        if alt_env is None:
            os.environ.pop("LLM_USER_AGENT", None)
        else:
            os.environ["LLM_USER_AGENT"] = alt_env
        a._bot_ua = None

    # Und: kommt Flos Kennung durch, darf NICHTS empfohlen werden.
    pruef = inspect.getsource(tools_ki_check.KiCheck.signatur_pruefen)
    assert "NICHT betroffen" in pruef, (
        "der Arzt unterscheidet nicht mehr zwischen 'Bot gesperrt' und "
        "'nur die nackte Python-Kennung gesperrt'")
    assert pruef.index("NICHT betroffen") < pruef.index("LLM_USER_AGENT="), (
        "die Empfehlung kommt, bevor geprueft wird, ob der Bot ueberhaupt "
        "betroffen ist")




def test_praefix_gilt_je_server():
    """Jeder Server darf Flo anders nennen - ohne Neustart.

    Frueher cachte sich JEDES der 21 Module beim Start seinen eigenen
    self._bot_name, und bot.py baute den Trigger-Regex einmal beim Import. Ein
    eigener Praefix je Server war damit nicht moeglich. Jetzt ist ai.py die
    einzige Autoritaet, und der Name wird zur Laufzeit nachgeschlagen."""
    import ai
    import economy
    import music
    guildcfg, zurueck = _cfg_frisch()
    try:
        guild = SimpleNamespace(id=4711, text_channels=[], get_channel=lambda _c: None)
        ok, wert, fehler = asyncio.run(guildcfg.setzen(4711, "praefix", "Bob", guild))
        assert ok, fehler
        assert guildcfg.get(4711, "praefix") == "Bob"

        # Der Name kommt je Server heraus ...
        assert ai.bot_name(4711) == "Bob"
        assert ai.bot_name(9999) == ai.instance._bot_name      # fremder Server
        # ... und die Regexe ziehen mit.
        assert ai.trigger_re(4711).search("bob mach mal was")
        assert not ai.trigger_re(9999).search("bob mach mal was")
        assert ai.strip_lead("Bob, level", 4711) == "level"
        assert ai.strip_lead("Bob, level", 9999) == "Bob, level"

        # Ohne ausdrueckliche gid gilt der Server, der gerade bedient wird -
        # daran haengen alle Module, ohne dass sie es selbst wissen muessen.
        token = ai.setze_guild(4711)
        try:
            assert ai.bot_name() == "Bob"
            assert economy.instance._bot_name == "Bob"
            assert music.instance._bot_name == "Bob"
            assert ai.strip_lead("bob level") == "level"
        finally:
            ai.guild_zuruecksetzen(token)
        assert economy.instance._bot_name != "Bob"

        # Aendern wirkt sofort - der Regex-Cache wird ueber den Hook geleert.
        asyncio.run(guildcfg.setzen(4711, "praefix", "Klaus", guild))
        assert ai.bot_name(4711) == "Klaus"
        assert ai.trigger_re(4711).search("klaus?") and not ai.trigger_re(4711).search("bob")
        # Zuruecksetzen ebenso.
        asyncio.run(guildcfg.loeschen(4711, "praefix"))
        assert ai.bot_name(4711) == ai.instance._bot_name

        # Unsinn kommt gar nicht erst rein: die Ansprache wird zum Regex.
        for murks in ("a", "x" * 40, "Flo Bot", "Fl(o", "flo|.*"):
            ok, _w, fehler = asyncio.run(guildcfg.setzen(4711, "praefix", murks, guild))
            assert not ok and fehler, murks
    finally:
        ai.praefix_geaendert()
        zurueck()




def test_kein_modul_haelt_den_botnamen_selbst():
    """Kein Modul darf sich den Namen wieder in eine eigene Variable legen.

    Genau das war die Bremse: 21 Kopien von os.getenv("BOT_NAME"), die beim
    Start eingefroren wurden. Wer ein neues Modul baut, erbt von FeatureBasis -
    dann stimmt der Name je Server von allein."""
    import pathlib
    import basis

    treffer = []
    for pfad in sorted(pathlib.Path(".").glob("*.py")):
        # ai haelt den .env-Standard, basis erklaert die Regel. Testdateien und
        # testhilfe sind ausgenommen, weil sie die Regel BESCHREIBEN - frueher
        # stand hier nur 'test_games_logic.py', und nach dem Aufteilen der
        # Suite meldete der Test die Datei, in der er selbst steht.
        if (pfad.name in ("ai.py", "basis.py", "testhilfe.py")
                or pfad.name.startswith("test_")):
            continue
        for nr, zeile in enumerate(pfad.read_text(encoding="utf-8").splitlines(), 1):
            if "self._bot_name = " in zeile and not zeile.strip().startswith("#"):
                treffer.append(f"{pfad.name}:{nr}")
    assert not treffer, f"eigene Kopie des Botnamens: {treffer}"

    # Und die Basis liefert wirklich den Namen des gerade bedienten Servers.
    class Beispiel(basis.FeatureBasis):
        pass

    import ai
    token = ai.setze_guild(0)
    try:
        assert Beispiel()._bot_name == ai.instance._bot_name
    finally:
        ai.guild_zuruecksetzen(token)




def test_ki_fehler_werden_unterschieden():
    """Vorher gab JEDE Ursache denselben Satz: abgelaufener Schluessel,
    ausgemustertes Modell, leeres Kontingent, Cloudflare-Sperre. Damit war ohne
    Serverzugang nicht zu sehen, woran es liegt - und im Log stand nur ein
    Traceback."""
    import ai
    faelle = {
        401: "auth",
        404: "modell",
        429: "limit",
        503: "stoerung",
        400: "anfrage",
    }
    saetze = {}
    for status, art in faelle.items():
        flo, _ = _ki_frisch([_KiFehler(status, "kaputt")] * 8)
        antwort = asyncio.run(flo.ask_flo("hi"))
        assert antwort == ai.FloAI.MELDUNGEN[art], (status, antwort)
        saetze[art] = antwort
    # Cloudflare-Sperre ist etwas anderes als ein verbotener Zugriff.
    flo, _ = _ki_frisch([_KiFehler(403, "error code: 1010")] * 8)
    flo._signatur_wechseln = lambda ua: False        # keine Rettung erlauben
    saetze["signatur"] = asyncio.run(flo.ask_flo("hi"))
    assert saetze["signatur"] == ai.FloAI.MELDUNGEN["signatur"]
    flo, _ = _ki_frisch([_KiFehler(403, "nope")] * 8)
    saetze["verboten"] = asyncio.run(flo.ask_flo("hi"))
    assert saetze["verboten"] == ai.FloAI.MELDUNGEN["verboten"]
    # Jede Ursache muss WIRKLICH einen eigenen Satz haben.
    assert len(set(saetze.values())) == len(saetze), saetze




def test_ki_wiederholt_nur_was_davon_besser_wird():
    """Ein einzelner 429 oder eine 503-Delle hat die Antwort bisher sofort
    getoetet. Wiederholt werden darf aber nur, was sich bessern kann - ein
    abgelehnter Schluessel wird beim zweiten Mal nicht gueltiger, das waere
    reines Haemmern gegen den Anbieter."""
    # Voruebergehend: erst 429, dann 503, dann klappt es.
    flo, anbieter = _ki_frisch([_KiFehler(429, "rate limit"),
                                _KiFehler(503, "unavailable"),
                                _KiAntwort("Na klar.")])
    assert asyncio.run(flo.ask_flo("hi")) == "Na klar."
    assert len(anbieter.aufrufe) == 3, anbieter.aufrufe

    # Dauerhaft: genau EIN Versuch, kein Nachtreten.
    for status, text in ((401, "bad key"), (400, "kaputt"), (403, "nope")):
        flo, anbieter = _ki_frisch([_KiFehler(status, text)] * 8)
        asyncio.run(flo.ask_flo("hi"))
        assert len(anbieter.aufrufe) == 1, (status, anbieter.aufrufe)

    # Und es gibt eine Obergrenze - kein Dauerfeuer.
    flo, anbieter = _ki_frisch([_KiFehler(429, "rate limit")] * 50)
    asyncio.run(flo.ask_flo("hi"))
    assert len(anbieter.aufrufe) == flo.WIEDERHOLUNGEN + 1, anbieter.aufrufe




def test_ki_heilt_ein_ausgemustertes_modell_selbst():
    """Anbieter mustern Modelle aus - genau das erklaert 'geht seit gestern gar
    nicht mehr, ohne dass jemand am Code war'. Statt dauerhaft stumm zu sein
    holt Flo die aktuelle Liste und nimmt selbst den besten Ersatz."""
    flo, anbieter = _ki_frisch(
        [_KiFehler(404, "The model `altes-modell-70b` has been decommissioned"),
         _KiAntwort("Bin wieder da.")],
        modelle=["whisper-large-v3", "neu-klein-8b", "neu-gross-70b",
                 "llama-guard-4-12b"])
    assert asyncio.run(flo.ask_flo("hi")) == "Bin wieder da."
    assert anbieter.modelle_gefragt == 1
    # Groesstes taugliches Modell, und nichts, was gar nicht chatten kann.
    assert flo._model == "neu-gross-70b", flo._model
    assert anbieter.aufrufe == ["altes-modell-70b", "neu-gross-70b"]

    # Vision nimmt nur ein Modell, das wirklich Bilder kann.
    flo, _ = _ki_frisch([_KiFehler(404, "model_not_found"), _KiAntwort("Bild gesehen.")],
                        modelle=["neu-gross-70b", "scout-17b", "whisper-large-v3"])
    assert asyncio.run(flo.see_image("was ist das", "http://x/y.png")) == "Bild gesehen."
    assert flo._vision_model == "scout-17b", flo._vision_model

    # Und wenn es keinen Ersatz gibt, wird ehrlich gemeldet statt endlos gesucht.
    import ai
    flo, anbieter = _ki_frisch([_KiFehler(404, "model_not_found")] * 8, modelle=[])
    assert asyncio.run(flo.ask_flo("hi")) == ai.FloAI.MELDUNGEN["modell"]
    assert len(anbieter.aufrufe) == 1, anbieter.aufrufe




def test_ki_wechselt_die_signatur_wenn_cloudflare_sperrt():
    """Gemessen auf dem echten Server: Groq sitzt hinter Cloudflare, und
    Cloudflare hat mit HTTP 403 'error code: 1010' geblockt - die Anfrage kam nie
    bei Groq an. Ein anderer Schluessel oder ein anderes Modell haetten daran
    nichts geaendert; nur eine andere Client-Signatur hilft."""
    import ai
    gebaut = []

    flo, _ = _ki_frisch([])
    def bauen(ua):
        gebaut.append(ua)
        # Die erste Ersatz-Signatur kommt durch, die urspruengliche nicht.
        if ua == ai.FloAI.SIGNATUREN[0]:
            return _KiAnbieter([_KiAntwort("Wieder da.")])
        return _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8)
    flo._client_bauen = bauen
    flo._client = _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8)

    assert asyncio.run(flo.ask_flo("hi")) == "Wieder da."
    assert gebaut and gebaut[0] == ai.FloAI.SIGNATUREN[0], gebaut
    assert flo._signatur == ai.FloAI.SIGNATUREN[0]
    # Nach dem Erfolg ist der Hinweis abgearbeitet und wird nicht endlos wiederholt.
    assert flo._signatur_offen == ""

    # Kommt KEINE Signatur durch, ist die IP gesperrt - dann ehrlich melden und
    # nicht ewig weiterprobieren.
    flo, _ = _ki_frisch([])
    versuche = []
    flo._client_bauen = lambda ua: (versuche.append(ua),
                                    _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8))[1]
    flo._client = _KiAnbieter([_KiFehler(403, "error code: 1010")] * 8)
    assert asyncio.run(flo.ask_flo("hi")) == ai.FloAI.MELDUNGEN["signatur"]
    assert len(versuche) == len(ai.FloAI.SIGNATUREN), versuche




def test_ki_stoerung_landet_nicht_im_gedaechtnis():
    """bot.py schreibt JEDE Antwort ins Kurzzeit-Gedaechtnis (bot.py:1529 und
    :1820) - auch eine Stoerungsmeldung. Die ging danach als Gespraechsverlauf
    wieder ans Modell, das sie brav nachgeplappert hat."""
    import ai
    flo, _ = _ki_frisch([])
    for satz in ai.FloAI.MELDUNGEN.values():
        flo.note_message(4711, "Flo", satz, is_bot=True)
    flo.note_message(4711, "Flo", "Klar, mach ich.", is_bot=True)
    gemerkt = [e["content"] for e in flo._HISTORY.get(4711, [])]
    assert gemerkt == ["Klar, mach ich."], gemerkt




def test_ki_selbsttest_sagt_die_wahrheit():
    """Der Log meldete 'KI-Feature aktiv', sobald ein Schluessel in der .env
    stand - ob er noch gilt, hat nie jemand geprueft. Eine Zusicherung, die
    niemand nachgesehen hat, ist schlimmer als keine."""
    flo, _ = _ki_frisch([_KiAntwort("ok")])
    assert asyncio.run(flo.selbsttest()) is True
    flo, _ = _ki_frisch([_KiFehler(401, "bad key")] * 8)
    assert asyncio.run(flo.selbsttest()) is False
    # Ohne Client faellt er sauber durch, statt zu krachen.
    flo, _ = _ki_frisch([])
    flo._client = None
    assert asyncio.run(flo.selbsttest()) is False




def test_ki_hat_nur_noch_einen_weg_nach_draussen():
    """Vier eigene try/except-Bloecke bedeuteten vier Politiken, die
    auseinanderlaufen. Es darf genau EINE Stelle geben, die den Anbieter
    anspricht - sonst vergisst der naechste Aufruf das Wiederholen wieder."""
    import ai
    quelle = open(ai.__file__, encoding="utf-8").read()
    assert quelle.count("chat.completions.create") == 1, (
        "es gibt wieder mehr als einen Weg zum Anbieter")
    # Und JEDER oeffentliche Aufrufer muss LlmFehler behandeln - sonst platzt
    # die Ausnahme bis in bot.py durch und der Nutzer sieht gar nichts.
    import inspect
    for name in ("ask_flo", "see_image", "see_image_raw", "generate"):
        koerper = inspect.getsource(getattr(ai.FloAI, name))
        assert "except LlmFehler" in koerper, f"{name} behandelt LlmFehler nicht"
        assert "except Exception" in koerper, f"{name} hat kein Sicherheitsnetz mehr"




def test_ki_gegen_einen_echten_http_anbieter():
    """Der Ernstfall-Test: echtes openai-Paket, echter HTTP-Server, echte
    Ausnahmen. ai._einordnen() liest exc.status_code bzw. exc.response.status_code -
    ob das mit den tatsaechlichen Ausnahmen des Pakets zusammenpasst, beweisen
    nachgebaute Fehler NICHT. Genau hier bricht es sonst still.

    Laeuft komplett auf 127.0.0.1 mit einem freien Port, kein echtes Netz."""
    import ai
    try:
        import openai  # noqa: F401
    except ImportError:                       # Bot laeuft auch ohne das Paket
        return
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    fall = {"wert": "ok", "modelle": []}
    cf_seite = "<html>error code: 1010</html>"

    def antwort(text):
        return _json.dumps({"id": "1", "object": "chat.completion", "created": 1,
                            "model": "m", "choices": [{"index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": text}}]})

    class Griff(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _raus(self, code, text, typ="application/json"):
            roh = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(roh)))
            self.end_headers()
            self.wfile.write(roh)

        def do_GET(self):
            if "/models" in self.path:
                return self._raus(200, _json.dumps({"object": "list", "data": [
                    {"id": m, "object": "model"} for m in fall["modelle"]]}))
            return self._raus(404, _json.dumps({"error": {"message": "nope"}}))

        def do_POST(self):
            art = fall["wert"]
            ua = self.headers.get("User-Agent", "")
            if art == "ok":
                return self._raus(200, antwort("Antwort da."))
            if art == "cf_signatur":
                if "curl" in ua:
                    return self._raus(200, antwort("Mit curl gehts."))
                return self._raus(403, cf_seite, "text/html")
            if art == "cf_ip":
                return self._raus(403, cf_seite, "text/html")
            if art == "modell_weg":
                laenge = int(self.headers.get("Content-Length", 0))
                try:
                    gewuenscht = _json.loads(self.rfile.read(laenge)).get("model", "")
                except ValueError:
                    gewuenscht = ""
                if gewuenscht and gewuenscht in fall["modelle"]:
                    return self._raus(200, antwort("Neues Modell laeuft."))
                return self._raus(404, _json.dumps({"error": {
                    "message": "The model `x` has been decommissioned"}}))
            return self._raus({"401": 401, "429": 429, "503": 503, "400": 400}[art],
                              _json.dumps({"error": {"message": "kaputt"}}))

    server = HTTPServer(("127.0.0.1", 0), Griff)   # Port 0 = freien nehmen
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    alt_umgebung = {k: os.environ.get(k) for k in
                    ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_USER_AGENT")}

    def frisch():
        os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
        os.environ["LLM_API_KEY"] = "gsk_test"
        os.environ["LLM_MODEL"] = "altes-modell-70b"
        os.environ.pop("LLM_USER_AGENT", None)
        flo = ai.FloAI()
        assert flo.setup() is True
        flo.WARTEN = (0.0, 0.0, 0.0)           # Tests sollen nicht schlafen
        return flo

    try:
        M = ai.FloAI.MELDUNGEN
        assert asyncio.run(frisch().ask_flo("hi")) == "Antwort da."

        for art, schluessel in (("401", "auth"), ("429", "limit"),
                                ("503", "stoerung"), ("400", "anfrage")):
            fall["wert"] = art
            assert asyncio.run(frisch().ask_flo("hi")) == M[schluessel], art

        # Cloudflare sperrt die IP: keine Signatur kommt durch -> ehrlich melden.
        fall["wert"] = "cf_ip"
        assert asyncio.run(frisch().ask_flo("hi")) == M["signatur"]

        # Cloudflare sperrt nur die Signatur -> Flo holt sich selbst raus.
        fall["wert"] = "cf_signatur"
        assert asyncio.run(frisch().ask_flo("hi")) == "Mit curl gehts."

        # Modell ausgemustert -> Flo nimmt selbst das groesste taugliche.
        fall["wert"] = "modell_weg"
        fall["modelle"] = ["klein-8b", "gross-70b", "whisper-large-v3"]
        flo = frisch()
        assert asyncio.run(flo.ask_flo("hi")) == "Neues Modell laeuft."
        assert flo._model == "gross-70b", flo._model

        fall["modelle"] = []
        assert asyncio.run(frisch().ask_flo("hi")) == M["modell"]

        # Der Selbsttest muss beides koennen: bestaetigen und widersprechen.
        fall["wert"] = "ok"
        assert asyncio.run(frisch().selbsttest()) is True
        fall["wert"] = "401"
        assert asyncio.run(frisch().selbsttest()) is False

        # Haengt der Anbieter, wartet Discord nicht ewig.
        fall["wert"] = "ok"
        flo = frisch()
        flo.ZEITLIMIT = 0.001
        flo._client = flo._client_bauen("")
        start = time.time()
        asyncio.run(flo.ask_flo("hi"))
        assert time.time() - start < 10, "Zeitlimit greift nicht"
    finally:
        server.shutdown()
        for k, v in alt_umgebung.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v




def test_ki_der_arzt_verschluckt_keine_meldung():
    """Nachgemessen und es stimmte NICHT: der Suchfilter in 'k' fand zwar
    "KI-Fehler", aber genau die Zeilen nicht, die sagen was Flo selbst repariert
    hat und was in die .env gehoert ("KI: wechsle selbst auf ...", "KI: Signatur
    ... funktioniert", "KI-Selbsttest fehlgeschlagen"). Der Arzt verschwieg also
    die Heilung.

    Der Test liest BEIDE Dateien als Quelltext, damit eine neue Log-Zeile in
    ai.py nicht still aus dem Filter fallen kann."""
    import ai
    hier = os.path.dirname(os.path.abspath(ai.__file__))
    arzt = open(os.path.join(hier, "k"), encoding="utf-8").read()

    # Das Muster steht in 'k' als KIMUSTER=... (der grep nimmt es ueber eine
    # Variable auf, deshalb hier die Zuweisung lesen und nicht den grep).
    muster = re.search(r'KIMUSTER="([^"]+)"', arzt)
    assert muster, "in 'k' gibt es kein KIMUSTER mehr"
    filter_re = re.compile(muster.group(1))

    quelle = open(ai.__file__, encoding="utf-8").read()
    meldungen = re.findall(r'log\.[a-z]+\(\s*"(KI[^"]*)', quelle)
    assert len(meldungen) >= 10, f"nur {len(meldungen)} KI-Meldungen gefunden"
    durchgefallen = [m for m in meldungen if not filter_re.search(m)]
    assert not durchgefallen, f"'k' zeigt diese Zeilen nicht an: {durchgefallen}"

    # Und der Vorspann von journalctl muss WIRKLICH weg sein: das sind fuenf
    # Felder ("Aug 19 14:25:20 Ubuntu python3[8422]:"), nicht vier.
    beispiel = "Aug 19 14:25:20 Ubuntu python3[8422]: 2026-08-19 [ERROR] KI: Modell weg"
    schnitt = re.search(r"sed 's/(\^[^']+)/", arzt)
    assert schnitt, "in 'k' steht kein sed mehr"
    gekuerzt = re.sub(schnitt.group(1).replace("^", "^"), "", beispiel)
    assert not gekuerzt.startswith("python"), (
        f"der Unit-Vorspann bleibt stehen: {gekuerzt!r}")




def test_ki_findet_bildmodelle_auch_anderer_familien():
    """Die Marker-Liste kannte nur Metas Namen. Nachgemessen fehlten pixtral und
    llava - die zwei verbreitetsten Bild-Modellfamilien ueberhaupt. Mustert der
    Anbieter Scout aus, blieb das Bild-Lesen damit tot, obwohl ein tauglicher
    Ersatz in der Liste stand."""
    import ai
    flo = ai.FloAI()
    for liste, erwartet in (
            (["llama-4-scout-17b", "gpt-oss-120b"], "llama-4-scout-17b"),
            (["pixtral-12b", "gpt-oss-120b"], "pixtral-12b"),
            (["llava-1.6-34b", "gpt-oss-120b"], "llava-1.6-34b"),
            (["qwen2-vl-72b", "gpt-oss-120b"], "qwen2-vl-72b")):
        assert flo._modell_waehlen(liste, True) == erwartet, liste
    # Die ECHTE Groq-Liste (nachgeschlagen, nicht erfunden): gpt-oss kann keine
    # Bilder, qwen3.6 ist multimodal - heisst aber weder "vision" noch "-vl".
    # Genau daran waere die Heilung sonst vorbeigelaufen.
    groq = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b",
            "whisper-large-v3", "meta-llama/llama-guard-4-12b"]
    assert flo._modell_waehlen(groq, False) == "openai/gpt-oss-120b"
    assert flo._modell_waehlen(groq, True) == "qwen/qwen3.6-27b"

    # Ein reines Textmodell darf NICHT als Bild-Ersatz durchgehen - es wuerde
    # das Bild-Format ablehnen. Lieber ehrlich nichts finden.
    assert flo._modell_waehlen(["gpt-oss-120b", "whisper-large-v3"], True) == ""
    # Und niemals etwas, das gar nicht chatten kann.
    assert flo._modell_waehlen(["whisper-large-v3", "guard-12b"], False) == ""




def test_ki_unbekannter_fehler_nennt_die_ausnahmeklasse():
    """Bei einem Fehler ohne HTTP-Status ("unbekannt") ist die Ausnahmeklasse die
    einzige Spur. Die stand nur in einem log.debug - und bot.py:70 loggt ab INFO,
    also erreichte sie das Journal ausgerechnet dort nie, wo sie gebraucht wird."""
    import ai
    import logging
    puffer = io.StringIO()
    griff = logging.StreamHandler(puffer)
    protokoll = logging.getLogger("dcbot.ai")
    protokoll.addHandler(griff)
    alt_stufe = protokoll.level
    protokoll.setLevel(logging.INFO)          # so wie bot.py es einstellt
    try:
        class VoelligUnbekannt(Exception):
            pass
        flo, _ = _ki_frisch([VoelligUnbekannt("kaputt")] * 8)
        asyncio.run(flo.ask_flo("hi"))
    finally:
        protokoll.removeHandler(griff)
        protokoll.setLevel(alt_stufe)
    text = puffer.getvalue()
    assert "KI-Fehler: unbekannt" in text, text
    assert "VoelligUnbekannt" in text, f"Ausnahmeklasse fehlt im Log: {text!r}"




def test_ki_vorgaben_von_bot_und_arzt_sind_gleich():
    """tools_ki_check.py haelt die Vorgaben bewusst als eigene Kopie - es muss
    auch laufen, wenn ai.py gar nicht importierbar ist. Genau deshalb koennen
    beide auseinanderlaufen, und dann diagnostiziert der Arzt etwas anderes als
    der Bot tut."""
    import ai
    import tools_ki_check
    assert tools_ki_check.STANDARD_BASE == ai.FloAI.DEFAULT_BASE_URL
    assert tools_ki_check.STANDARD_MODELL == ai.FloAI.DEFAULT_MODEL
    assert tools_ki_check.STANDARD_VISION == ai.FloAI.DEFAULT_VISION_MODEL
    # Und die Vorgaben duerfen nicht auf ausgemusterte Modelle zeigen. Groq hat
    # die beiden am 17.06.2026 abgeschaltet - das war die Ursache des Ausfalls.
    for tot in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"):
        assert tot != ai.FloAI.DEFAULT_MODEL, f"{tot} ist bei Groq abgeschaltet"
    # Das Chat-Modell MUSS Werkzeuge koennen - ask_flo reicht 'tools' mit.
    assert "tools=[self.WEATHER_TOOL]" in inspect.getsource(ai.FloAI.ask_flo)




def test_arzt_verraet_niemals_ein_geheimnis():
    """Der Arzt wird auf einem Handy fotografiert und in Chats geschickt. Ein
    Schluessel darf da NIE vollstaendig auftauchen - auch nicht im Fehlerfall
    und auch nicht in der Berichtsdatei."""
    import arzt
    a = arzt.Arzt()
    assert a.maskiere("") == "(leer)"
    for geheim in ("gsk_streng_geheim_1234567890", "kurz", "abcdefghijkl"):
        maskiert = a.maskiere(geheim)
        assert geheim not in maskiert, maskiert
        assert str(len(geheim)) in maskiert          # Laenge hilft beim Vergleichen

    schluessel = "gsk_streng_geheim_1234567890"
    with _FalscherAnbieter("401") as anbieter:
        text, ki_arzt = _ki_arzt_lauf(anbieter, schluessel)
    assert schluessel not in text, "Schluessel steht im Klartext auf dem Bildschirm"
    assert schluessel not in "\n".join(ki_arzt.zeilen), "Schluessel steht im Bericht"
    assert "gsk_...7890" in text, text[:400]




def test_ki_arzt_ordnet_jeden_anbieterfehler_richtig_ein():
    """Fuer jede Ursache ein anderer Rat - sonst tauscht man wieder den Schluessel,
    obwohl das Modell abgeschaltet wurde."""
    erwartet = {
        "401": "Schluessel",
        "404": "Modell",
        "429": "Kontingent",
        "500": "Stoerung",
    }
    for fall, stichwort in erwartet.items():
        with _FalscherAnbieter(fall) as anbieter:
            text, arzt = _ki_arzt_lauf(anbieter)
        raete = " ".join(u for u, _ in arzt.probleme)
        assert stichwort.lower() in raete.lower(), (fall, raete)

    # Und wenn alles geht, gibt es KEINEN Befund.
    with _FalscherAnbieter("ok") as anbieter:
        text, arzt = _ki_arzt_lauf(anbieter)
    assert not arzt.probleme, arzt.probleme
    assert "funktioniert" in text




def test_ki_arzt_unterscheidet_signatur_von_ip_sperre():
    """Cloudflare-Fehler 1010 hat ZWEI voellig verschiedene Ursachen, und die
    Behandlung ist entgegengesetzt: bei der Signatur hilft eine Zeile in der
    .env, bei einer IP-Sperre hilft gar kein Code. Raten waere hier teuer."""
    with _FalscherAnbieter("cf_signatur") as anbieter:
        text, arzt = _ki_arzt_lauf(anbieter)
    raete = " ".join(f"{u} {w}" for u, w in arzt.probleme)
    assert "LLM_USER_AGENT" in raete, raete
    assert "IP" not in " ".join(u for u, _ in arzt.probleme)

    with _FalscherAnbieter("cf_ip") as anbieter:
        text, arzt = _ki_arzt_lauf(anbieter)
    raete = " ".join(f"{u} {w}" for u, w in arzt.probleme)
    assert "IP" in raete and "gesperrt" in raete, raete
    # Und der Cloudflare-Code muss als solcher benannt sein, nicht als API-Fehler.
    assert "Cloudflare" in text and "1010" in text




def test_flo_laesst_sich_nichts_gefallen_und_hat_haltung():
    """Flo soll kontern statt zu schlucken - und eine politische Haltung haben.

    Beides steht in eigenen Bloecken, damit man es findet und abschalten kann,
    statt es im Persona-Absatz zu vergraben."""
    import ai
    alt = os.environ.pop("BOT_POLITIK", None)
    alt_persona = os.environ.pop("BOT_PERSONA", None)
    try:
        p = ai.instance._system_prompt(author="Tester", title="")
        # Konter: er entschuldigt sich nicht und gibt zurueck.
        assert "NICHTS gefallen" in p, "Flo schluckt Beleidigungen wieder"
        assert "doppelt zurueck" in p
        # Haltung ist da und ist als solche erkennbar.
        assert "RECHTS" in p, "die politische Haltung fehlt"
        # ... und laesst sich je Server/Betreiber abschalten.
        os.environ["BOT_POLITIK"] = "aus"
        assert "RECHTS" not in ai.instance._system_prompt(), (
            "BOT_POLITIK=aus schaltet die Haltung nicht ab")
        for wert in ("0", "off", "false", "nein"):
            os.environ["BOT_POLITIK"] = wert
            assert not ai.instance._politik_an(), wert
    finally:
        os.environ.pop("BOT_POLITIK", None)
        for name, wert in (("BOT_POLITIK", alt), ("BOT_PERSONA", alt_persona)):
            if wert is not None:
                os.environ[name] = wert




def test_die_grenze_steht_vorne_und_bleibt_vollstaendig():
    """Die eine Grenze ist nicht verhandelbar - auch nicht beim Entzahmen.

    Zwei Dinge zugleich: (1) Alle sechs Merkmale, echte Drohungen, private Daten
    und die Notlagen-Regel MUESSEN im Prompt stehen. Wer Flo haerter macht, darf
    sie nicht nebenbei mit wegraeumen. (2) Sie steht direkt hinter der Persona
    und nicht ganz am Ende - ganz hinten wirkte sie wie das letzte Wort und
    faerbte alles davor zahm ein."""
    import ai
    p = ai.instance._system_prompt(author="Tester", title="")
    for merkmal in ("Herkunft", "Hautfarbe", "Religion", "Geschlecht",
                    "sexuelle Orientierung", "Behinderung"):
        assert merkmal in p, f"die Grenze nennt {merkmal} nicht mehr"
    for regel in ("echten Drohungen", "privaten Daten", "Nazi-Verherrlichung"):
        assert regel in p, f"die Grenze nennt '{regel}' nicht mehr"
    # Wer wirklich am Boden ist, wird nicht weitergeroastet.
    assert "am Boden" in p and "Spass sofort weg" in p, (
        "die Notlagen-Regel ist verschwunden - genau die darf nie fallen")

    # Position: Grenze frueh, Vollgas-Erinnerung als letztes Wort.
    assert p.index("Herkunft") < len(p) * 0.5, (
        "die Grenze ist wieder ans Ende gerutscht und faerbt alles zahm")
    assert p.rstrip().endswith("Nur die eine Grenze von oben bleibt."), (
        f"das Schlusswort steht nicht am Ende: ...{p[-80:]!r}")




def test_flo_fasst_sich_kurz():
    """Die Beschwerde des Betreibers, nachpruefbar gemacht.

    Woertlich: "er schreibt wenn man was sagt zu ihm immer einen ewig langen
    text was halt einfach tot arsch ist". Ursache waren drei Sachen gleichzeitig,
    und ein weicher Halbsatz ("kurz und natuerlich wie im Chat") hat gegen die
    anderen zwei verloren. Deshalb sind hier alle drei festgenagelt."""
    import ai
    import bot

    p = ai.instance._system_prompt(author="Tester", title="")
    # 1. Die Regel ist eine Ansage, kein Gefuehl.
    assert "GENAU EINEM Satz" in p, "die harte Laengenregel fehlt"
    # 2. Sie steht ZWEIMAL: vorne bei den festen Regeln und als letztes Wort.
    #    Einmal in der Mitte war genau der Fehler - dort geht sie unter.
    assert p.index("GENAU EINEM Satz") < len(p) * 0.5
    assert "ERSTENS KURZ" in p, "die Laengenregel fehlt am Prompt-Ende"
    # 3. Der Erklaerbaer-Schalter ist raus. "knallst du ihm eine echte,
    #    brauchbare Antwort hin" war fuer ein Modell die Einladung zum Vortrag.
    assert "brauchbare Antwort hin" not in p, (
        "die Persona laedt wieder zum Vortrag ein")
    # 4. Der Deckel ist so gesetzt, dass eine KI-Antwort STRUKTURELL nicht mehr
    #    auf zwei Discord-Nachrichten passt (~4 Zeichen je Token im Deutschen).
    grenze = bot._split_message.__defaults__[0]
    assert ai.FloAI.MAX_TOKENS * 4 < grenze, (
        f"{ai.FloAI.MAX_TOKENS} Token koennen wieder zwei Nachrichten fuellen "
        f"(Grenze {grenze})")
    assert ai.FloAI.MAX_TOKENS_BILD <= ai.FloAI.MAX_TOKENS
    # 5. Aber nicht so knapp, dass ein Denk-Modell nur noch Leeres liefert -
    #    genau das war test_ki_leere_antwort_bleibt_nicht_spurlos.
    assert ai.FloAI.MAX_TOKENS >= 200, "zu knapp fuer ein Modell, das vordenkt"


def test_flo_setzt_den_letzten_hieb():
    """'am ende immer so wichser oder so' - der Schlusshieb ist eine Regel.

    Und er steht an der Stelle, die am schwersten wiegt: ganz hinten. Vorne
    wuerde er unter allem anderen verschwinden."""
    import ai
    p = ai.instance._system_prompt(author="Tester", title="")
    assert "LETZTES WORT" in p and "Seitenhieb" in p
    assert p.index("LETZTES WORT") > len(p) * 0.8, (
        "der Schlusshieb steht zu weit vorne und geht unter")


def test_beispiele_zeigen_das_mass():
    """IDEEN [39]: Der Ton kommt aus BEISPIELEN, nicht aus der Anweisung
    'sei passiv-aggressiv'. Zwei Bedingungen, damit das nicht nach hinten
    losgeht: die Beispiele muessen selbst kurz sein - sonst lehren sie das
    Gegenteil der Laengenregel -, und sie muessen VOR dem Tonfall stehen,
    damit die Rangstufe sie ueberstimmen kann."""
    import ai
    import titles

    zeilen = [z for z in ai.FloAI._BEISPIELE.split("\n") if z.startswith("Er: ")]
    assert len(zeilen) >= 5, "zu wenige Muster, das traegt den Ton nicht"
    for z in zeilen:
        assert len(z) <= 160, f"Beispiel ist selbst schon eine Wand: {z[:60]}"
        assert z.count(". ") <= 2, f"Beispiel hat mehr als einen Satz: {z[:60]}"
    p = ai.instance._system_prompt(author="T", title="X",
                                   tone=titles.RARITY["goettlich"]["tone"])
    assert p.index("Er: 'hi flo'") < p.index("GOETTLICHEN"), (
        "die Beispiele stehen hinter dem Tonfall und ueberschreiben die Rampe")


def test_ohne_rang_ist_flo_am_uebelsten():
    """Ein leerer Tonfall heisst 'kein Rang', nicht 'neutral'.

    Ohne diesen Zweig faengt die Rampe erst bei 'normal' an - und dann ist jeder
    gekaufte Einstiegstitel entweder eine Verschaerfung oder eine
    Verweichlichung. Genau an diesem Nullsummenspiel hat sich Commit 3ed75fae
    abgearbeitet."""
    import ai
    import titles

    ohne = ai.instance._system_prompt(author="Tester", title="", tone="")
    assert "keinen Titel, keinen Rang" in ohne
    assert "volle Breitseite" in ohne
    # Mit Rang steht der Text NICHT mehr drin - sonst gaelten beide gleichzeitig.
    mit = ai.instance._system_prompt(author="Tester", title="NPC",
                                     tone=titles.RARITY["goettlich"]["tone"])
    assert "keinen Titel, keinen Rang" not in mit


def test_gehirn_merkt_sich_nur_was_es_darf():
    """Flo hoert mit - aber nicht alles, und niemals heimlich in DMs.

    Ein Bot, der jede Zeile mitschneidet, waere ein Ueberwachungswerkzeug statt
    eines Spass-Features. Deshalb: keine DMs, keine Bots, keine Einzeiler (die
    sagen ueber einen Menschen nichts aus) und keine Befehle an Flo - Bedienung
    ist kein Gespraech."""
    gehirn, restore = _gehirn_frisch()
    try:
        g = gehirn.instance
        gehirn.note_message(_gehirn_msg(1, "ich zocke jeden Abend Terraria"))
        assert len(g._guild(77)["puffer"]) == 1

        # DM (kein guild) - darf NIE in den Puffer.
        dm = _gehirn_msg(1, "das ist eine lange private Nachricht")
        dm.guild = None
        gehirn.note_message(dm)
        # Bot-Nachrichten interessieren niemanden.
        gehirn.note_message(_gehirn_msg(9, "ich bin ein langer Bot-Text", bot=True))
        # Zu kurz, um etwas ueber jemanden zu sagen.
        gehirn.note_message(_gehirn_msg(1, "lol"))
        assert len(g._guild(77)["puffer"]) == 1, g._guild(77)["puffer"]

        # Der Puffer hat einen Deckel - sonst waechst die Datei unbegrenzt.
        for i in range(gehirn.PUFFER_MAX + 50):
            gehirn.note_message(_gehirn_msg(1, f"nachricht nummer {i} mit text"))
        assert len(g._guild(77)["puffer"]) == gehirn.PUFFER_MAX
    finally:
        restore()




def test_gehirn_speichert_keine_privaten_daten():
    """Der KI ist das verboten - aber eine Anweisung ist keine Zusicherung.

    Einmal in der Datei steht es da. Deshalb filtert Flo hinterher noch einmal
    hart nach, egal was das Modell vorschlaegt."""
    import gehirn
    heikel = [
        "erreichbar unter anna@example.com",
        "Handynummer ist +49 170 1234567",
        "IBAN DE89370400440532013000",
        "wohnt in der Hauptstrasse 5",
        "wohnt in 93047 Regensburg",
        "postet staendig https://example.com/x",
        "hat das Passwort geaendert",
    ]
    for text in heikel:
        assert gehirn.Gehirn._heikel(text), f"wird gespeichert: {text!r}"
    harmlos = ["spielt Terraria", "hasst Montage", "kann nicht zielen",
               "trinkt nur Club Mate", "verliert jede Wette gegen Ben"]
    for text in harmlos:
        assert not gehirn.Gehirn._heikel(text), f"faelschlich verworfen: {text!r}"




def test_gehirn_waechst_nicht_unbegrenzt_und_vergisst_das_richtige():
    """Ein Gedaechtnis ohne Deckel macht die Datei unlesbar und den Prompt teuer.

    Was oft bestaetigt wurde, ueberlebt; was einmal nebenbei fiel, fliegt zuerst
    raus. Sonst verdraengt zufaelliges Geplapper die echten Eigenheiten."""
    import gehirn
    liste = []
    # Derselbe Fakt zweimal ist kein zweiter Eintrag, sondern eine Bestaetigung.
    assert gehirn.Gehirn._merge(liste, "spielt Terraria", 5) is True
    assert gehirn.Gehirn._merge(liste, "Spielt  Terraria!", 5) is False, (
        "derselbe Fakt landet doppelt im Kopf")
    assert len(liste) == 1 and liste[0]["oft"] == 2

    # Ueber dem Deckel fliegt das Selten-Bestaetigte zuerst.
    liste = [{"t": "oft gehoert", "wann": time.time() - 10, "oft": 9}]
    for i in range(10):
        gehirn.Gehirn._merge(liste, f"nebensache {i}", 5)
    assert len(liste) == 5, len(liste)
    assert any(e["t"] == "oft gehoert" for e in liste), (
        "der oft bestaetigte Fakt wurde verdraengt")




def test_gehirn_bleibt_auf_seinem_server_und_ist_loeschbar():
    """Zwei Zusagen, die beide zaehlen:

    (1) Was im einen Server gesagt wurde, weiss Flo im anderen NICHT. Sonst
        traegt er Interna von Server zu Server.
    (2) Jeder kann loeschen, was Flo ueber ihn weiss - samt dem, was noch
        unausgewertet im Puffer liegt. Sonst waere es beim naechsten Lauf
        einfach wieder da."""
    gehirn, restore = _gehirn_frisch()
    try:
        g = gehirn.instance
        gehirn.Gehirn._merge(g._person(77, 1)["fakten"], "spielt Terraria", 25)
        gehirn.Gehirn._merge(g._person(88, 1)["fakten"], "ist auf 88 unterwegs", 25)

        k77 = gehirn.kontext_fuer(77, 1)
        assert "Terraria" in k77
        assert "88" not in k77, "Wissen leckt zwischen Servern"
        assert "Terraria" not in gehirn.kontext_fuer(88, 1)
        # Ohne Wissen kein Block - sonst haengt an jeder Antwort leerer Text.
        assert gehirn.kontext_fuer(77, 999) == ""
        # Und der Block bleibt kurz: er kostet bei JEDER Antwort Token.
        for i in range(40):
            gehirn.Gehirn._merge(g._person(77, 1)["fakten"], f"fakt {i} " + "x" * 60, 25)
        assert len(gehirn.kontext_fuer(77, 1)) <= 700

        # Loeschen raeumt Fakten UND Puffer.
        gehirn.note_message(_gehirn_msg(1, "noch eine lange Nachricht von mir"))
        assert g._guild(77)["puffer"]
        weg = gehirn.vergiss(77, 1)
        assert weg > 0
        assert gehirn.kontext_fuer(77, 1) == ""
        assert not [m for m in g._guild(77)["puffer"] if m["wer"] == 1], (
            "im Puffer steht die Person noch - beim naechsten Lauf waere sie zurueck")
        # Server 88 bleibt unberuehrt.
        assert "88" in gehirn.kontext_fuer(88, 1)
    finally:
        restore()




def test_gehirn_haengt_nicht_an_einem_kaputten_ki_aufruf_fest():
    """Scheitert die Auswertung, darf der Puffer nicht ewig neu geschickt werden.

    Sonst laeuft ein dauerhaft kaputter KI-Aufruf alle 10 Minuten in dieselben
    120 Nachrichten - und der Puffer nimmt nie wieder etwas Neues auf."""
    gehirn, restore = _gehirn_frisch()
    import ai
    alt_gen = ai.generate
    try:
        g = gehirn.instance
        for i in range(gehirn.PUFFER_ZIEL + 5):
            gehirn.note_message(_gehirn_msg(1, f"eine nachricht nummer {i}"))
        voll = len(g._guild(77)["puffer"])
        assert voll >= gehirn.PUFFER_ZIEL

        ai.generate = lambda *a, **k: _als_coro(None)     # KI liefert nichts
        neu = asyncio.run(gehirn.tick([SimpleNamespace(id=77)]))
        assert neu == 0
        assert g._guild(77)["puffer"] == [], (
            "der Puffer wird beim naechsten Lauf noch einmal verschickt")

        # Und der gute Fall: aus dem Chat werden Fakten.
        for i in range(gehirn.PUFFER_ZIEL + 1):
            gehirn.note_message(_gehirn_msg(1, f"ich zocke gerne runde {i}"))
        ai.generate = lambda *a, **k: _als_coro(
            "Anna: spielt jeden Abend Terraria\n"
            "SERVER: hier wird viel gezockt\n"
            "Anna: erreichbar unter anna@example.com\n"
            "Unbekannt: irgendwas")
        neu = asyncio.run(gehirn.tick([SimpleNamespace(id=77)]))
        assert neu == 2, neu
        kontext = gehirn.kontext_fuer(77, 1)
        assert "Terraria" in kontext and "gezockt" in kontext
        assert "example.com" not in kontext, "private Daten im Gedaechtnis"
        # Ein Name, den niemand im Chat hatte, wird nicht erfunden.
        assert "irgendwas" not in kontext
    finally:
        ai.generate = alt_gen
        restore()


if __name__ == "__main__":
    run(globals())
