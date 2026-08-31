"""Arbeit, Schichten, Karriere und das Wort des Tages.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur arbeit      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _als_coro, _arbeit_frisch, _embed_text, _fake_person,
    _karriere_durchspielen)



def test_arbeit_spasswordle_haelt_den_vertrag_der_basisklasse():
    """Alle Schichten versprechen bauen(chef, autor, **_extra). SpassWordle hat
    das **_extra weggelassen - damit wirft derselbe Aufruf, den jede andere
    Schicht klaglos schluckt, bei dieser einen einen TypeError. Der Aufrufer
    (arbeit.py:1214 reicht **extra durch) kann das nicht sehen."""
    import arbeit
    import inspect
    basis = inspect.signature(arbeit.Schicht.bauen).parameters
    assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in basis.values())
    for name, obj in vars(arbeit).items():
        if not (isinstance(obj, type) and issubclass(obj, arbeit.Schicht)
                and obj is not arbeit.Schicht):
            continue
        if "bauen" not in vars(obj):
            continue                          # erbt die Basis-Signatur, passt
        params = inspect.signature(obj.bauen).parameters
        assert any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()), (
            f"{name}.bauen nimmt kein **_extra - arbeit.py:1214 reicht aber "
            f"beliebige Schluesselwoerter durch")




# --- Mittlere und niedrige Fehler aus dem Volltest --------------------------
def test_cmdnorm_kapert_keine_alltagswoerter():
    """Die Tippfehler-Korrektur schrieb harmlose Woerter auf GEFAEHRLICHE
    Befehle um: 'banane' -> ban, 'klick'/'kicks' -> kick, 'waren' -> warn,
    'klaus' (ein Name!) -> klau. Das erste Wort einer an Flo gerichteten
    Nachricht landet danach direkt bei der Moderation."""
    import cmdnorm
    gefaehrlich = {"ban", "bann", "banne", "verbann", "sperr", "kick", "rauswerf",
                   "warn", "verwarn", "timeout", "mute", "stumm", "knebel",
                   "lösch", "loesch", "purge", "nuke", "nimm", "setcoins",
                   "steal", "klau", "klauen", "raub"}
    harmlos = """banane bananen klick klicks kicks waren ware klaus klaue klauen
        banner warte warten wanne kanne kicker sperre sperrt bahn band bande baum
        raum mann liste lieb leben ende engel essen zeit zeig mach macht mag lang
        kette wette beste feste reste tanne kante kanten""".split()
    schlecht = []
    for wort in harmlos:
        norm = cmdnorm.normalize(wort)
        if norm and norm != wort and norm.split()[0] in gefaehrlich:
            schlecht.append(f"{wort} -> {norm}")
    assert not schlecht, f"harmlose Woerter werden zu Befehlen: {schlecht}"

    # Echte Tippfehler muessen weiterhin korrigiert werden.
    for falsch, richtig in (("warnn", "warn"), ("timout", "timeout"),
                            ("purg", "purge"), ("lösche", "lösch")):
        assert cmdnorm.normalize(falsch) == richtig, (falsch, cmdnorm.normalize(falsch))

    # --- Die arbeit-Befehle: Vertipper ja, Alltagsdeutsch nein --------------
    # Alle drei Faelle sind GEMESSEN, nicht geraten. Ohne die Stopwords wuerde
    # aus "Flo lohnt sich das?" der Lohnzettel und aus "schlicht" eine Schicht.
    for wort in ("lohnt", "lohnte", "schlicht", "schlichte"):
        assert cmdnorm.normalize(wort) is None, (wort, cmdnorm.normalize(wort))
    # 'world' war schon vor arbeit kaputt: 'word' (Wort-Zaehler) ist eine
    # Loeschung entfernt, also wurde "Flo world of warcraft" zur Wort-Statistik.
    assert cmdnorm.normalize("world of warcraft") is None
    for falsch, richtig in (("wokr", "work"), ("arbiet", "arbeit"),
                            ("schihct", "schicht"), ("lohnzettl", "lohnzettel"),
                            ("wrodle", "wordle"), ("tageswrot", "tageswort"),
                            ("gehatl", "gehalt"), ("malohcen", "malochen")):
        raus = cmdnorm.normalize(falsch)
        assert raus == richtig, (falsch, raus)

    # Und jedes Befehlswort, das arbeit.py selbst versteht, MUSS in KNOWN
    # stehen - sonst greift die Korrektur fuer genau dieses Wort nicht.
    import arbeit
    for wort in (arbeit._CMDS + arbeit._LOHN_CMDS + arbeit._WORDLE_CMDS
                 + arbeit._TAGES_CMDS):
        assert wort in cmdnorm.KNOWN, f"{wort!r} fehlt in cmdnorm.KNOWN"




def test_schicht_rechnet_bei_doppelklick_nur_einmal_ab():
    """SchichtView.beenden() setzte self.fertig, hat es aber nie GEPRUEFT - und
    interaction_check prueft nur, WER klickt, nicht wie oft. Zwei schnelle
    Klicks (oder zwei Modal-Eingaben) liefen also beide durch und
    abrechnen() schrieb den Lohn zweimal gut.

    Der Riegel gehoert an genau eine Stelle: dreizehn Aufrufstellen koennen ihn
    nicht jede fuer sich mitbringen."""
    import arbeit
    v = arbeit.SchichtView(arbeit.instance, 4242, arbeit.SCHICHTEN["salat"])
    v.message = None
    gerufen = []

    alt_abrechnen = arbeit.instance.abrechnen
    alt_embed = arbeit.instance.ergebnis_embed
    arbeit.instance.abrechnen = lambda *a, **k: (gerufen.append(1), (1000, ""))[1]
    arbeit.instance.ergebnis_embed = lambda *a, **k: SimpleNamespace(
        set_image=lambda **kw: None)

    async def nichts(**_k):
        pass

    inter = SimpleNamespace(
        response=SimpleNamespace(edit_message=nichts, defer=nichts),
        user=SimpleNamespace(id=4242, display_name="x"),
        edit_original_response=nichts)

    async def dreimal():
        await v.beenden(inter, "t", "x", 1.0)
        await v.beenden(inter, "t", "x", 1.0)     # Doppelklick
        await v.beenden(inter, "t", "x", 1.0)

    try:
        asyncio.run(dreimal())
    finally:
        arbeit.instance.abrechnen = alt_abrechnen
        arbeit.instance.ergebnis_embed = alt_embed

    assert len(gerufen) == 1, f"Lohn wurde {len(gerufen)}x gutgeschrieben"
    assert v.fertig is True




def test_arbeit_woerter_sind_sauber():
    """Jedes Wort muss GENAU so lang sein, wie sein Fach sagt, und darf nur
    A-Z enthalten.

    Das ist kein Pedanterie-Test: ein 8-Buchstaben-Wort im 7er-Fach macht das
    Eingabefeld unbedienbar (min_length/max_length kommen aus der Fachlaenge),
    und ein Umlaut ist bei einem Buchstabenspiel nicht eindeutig eintippbar.
    Beim Schreiben der Listen war GENAU das der Fehler: die 7er- und 8er-Liste
    enthielt fast durchgehend Woerter, die einen Buchstaben zu lang waren."""
    import re
    import arbeit
    for laenge, worte in arbeit.WOERTER.items():
        assert worte, f"Fach {laenge} ist leer"
        for w in worte:
            assert len(w) == laenge, f"{w!r} liegt im {laenge}er-Fach, ist aber {len(w)}"
            assert re.fullmatch(r"[A-Z]+", w), f"{w!r} ist nicht rein A-Z"
        assert len(set(worte)) == len(worte), f"Doppelte im {laenge}er-Fach"
    # Jede ziehbare Laenge muss auch wirklich ein Fach haben - sonst faellt das
    # Wort des Tages an dem Tag auf die 5er-Liste zurueck, ohne dass es jemand
    # merkt (ausser am zu kleinen Topf).
    for laenge in arbeit.TAGES_LAENGEN:
        assert laenge in arbeit.WOERTER, f"Ziehung verlangt {laenge}, gibt es nicht"
    assert len(arbeit.TAGES_GEWICHT_WOCHE) == len(arbeit.TAGES_LAENGEN)
    assert len(arbeit.TAGES_GEWICHT_WOCHENENDE) == len(arbeit.TAGES_LAENGEN)




def test_arbeit_wordle_faerbung():
    """Die zweistufige Faerbung - der Klassiker unter den Wordle-Fehlern.

    Erst alle exakten Treffer wegnehmen, DANN die restlichen Buchstaben
    verteilen. Ohne das faerbt ein Versuch mit zwei gleichen Buchstaben beide
    gelb, obwohl in der Loesung nur einer steckt."""
    import arbeit
    W = arbeit.Wordle

    # Volltreffer.
    assert W("ABEND").muster("ABEND") == "🟩🟩🟩🟩🟩"
    # Nichts davon drin.
    assert W("ABEND").muster("PILZE") == "⬛⬛⬛⬛🟨"      # das E aus ABEND
    # Doppelter Buchstabe im VERSUCH, einfacher in der Loesung.
    # NOTEN hat genau ein T. In OTTER steht das zweite T an der richtigen
    # Stelle (gruen) - fuer das erste T bleibt danach NICHTS uebrig, es muss
    # grau werden. Ohne die zweistufige Zaehlung waere es gelb.
    m = W("NOTEN").muster("OTTER")
    assert m == "🟨⬛🟩🟩⬛", m
    # Noch deutlicher: ABEND hat ein E, der Versuch besteht nur aus E.
    # Genau eins davon darf gruen werden, alle anderen grau.
    m2 = W("ABEND").muster("EEEEE")
    assert m2 == "⬛⬛🟩⬛⬛", m2
    # Umgekehrt: doppelt in der Loesung, einfach im Versuch.
    assert W("ESSEN").muster("SEITE") == "🟨🟨⬛⬛🟨"
    # Richtiger Buchstabe an falscher Stelle.
    assert W("KATZE").muster("ZEBRA") == "🟨🟨⬛⬛🟨"




def test_arbeit_wordle_ablauf():
    """Versuche zaehlen, Zustand stimmt, Murks wird abgewiesen."""
    import arbeit
    spiel = arbeit.Wordle("ABEND")
    assert spiel.offen == 6 and not spiel.geloest and not spiel.aus

    assert spiel.raten("XY") == "laenge", "zu kurz durchgelassen"
    assert spiel.raten("AB3ND") == "laenge", "Ziffer durchgelassen"
    assert spiel.offen == 6, "ungueltiger Versuch wurde gezaehlt"

    assert spiel.raten("blume") == "weiter"      # klein geschrieben geht auch
    assert spiel.versuche == ["BLUME"] and spiel.offen == 5

    for _ in range(4):
        spiel.raten("BLUME")
    assert spiel.offen == 1 and not spiel.aus
    assert spiel.raten("BLUME") == "aus"
    assert spiel.aus and not spiel.geloest
    assert spiel.raten("ABEND") == "fertig", "nach dem Aus ging es weiter"

    # Und der gute Ausgang.
    spiel = arbeit.Wordle("ABEND")
    assert spiel.raten("ABEND") == "geloest"
    assert spiel.geloest and spiel.lohnfaktor() == 2.0




def test_arbeit_wort_des_tages_ist_berechnet():
    """Dasselbe Wort fuer denselben Server am selben Tag - auch nach einem
    Neustart mitten im Raten. Und NICHT dasselbe wie auf dem Nachbarserver,
    sonst holt man sich die Loesung von drueben."""
    import arbeit
    a = arbeit.Wordle.des_tages(111, tag="2026-08-16")
    b = arbeit.Wordle.des_tages(111, tag="2026-08-16")
    assert a.loesung == b.loesung, "Wort nicht reproduzierbar"

    # Anderer Server, anderer Tag: ueber viele Faelle darf das nicht konstant sein.
    andere = {arbeit.Wordle.des_tages(gid, tag="2026-08-16").loesung
              for gid in range(1, 40)}
    assert len(andere) > 5, "alle Server bekommen fast dasselbe Wort"
    tage = {arbeit.Wordle.des_tages(111, tag=f"2026-08-{t:02d}").loesung
            for t in range(1, 29)}
    assert len(tage) > 10, "das Wort wechselt kaum von Tag zu Tag"

    # Die LAENGE wird gewuerfelt, nicht am Wochentag festgemacht - vorher war
    # Mo-Do immer 5 Buchstaben, also vier Tage die Woche dieselbe Aufgabe und
    # derselbe Topf.
    laengen = [len(arbeit.Wordle.des_tages(1, tag=f"2026-0{m}-{t:02d}").loesung)
               for m in (7, 8, 9) for t in range(1, 29)]
    assert set(laengen) == set(arbeit.TAGES_LAENGEN), sorted(set(laengen))
    haeufigste = max(set(laengen), key=laengen.count)
    assert laengen.count(haeufigste) < len(laengen) * 0.6, \
        "eine Laenge dominiert alles"

    # Aber reproduzierbar: derselbe Tag, dieselbe Laenge.
    assert (arbeit.Wordle.des_tages(1, tag="2026-08-16").loesung
            == arbeit.Wordle.des_tages(1, tag="2026-08-16").loesung)

    # Am Wochenende sind lange Woerter wahrscheinlicher - im Schnitt, nicht
    # garantiert. Ueber ein ganzes Jahr muss sich das zeigen.
    import datetime
    woche, ende = [], []
    for n in range(365):
        tag = (datetime.date(2026, 1, 1) + datetime.timedelta(days=n)).isoformat()
        laenge = len(arbeit.Wordle.des_tages(1, tag=tag).loesung)
        (ende if datetime.date.fromisoformat(tag).weekday() >= 5 else woche
         ).append(laenge)
    assert sum(ende) / len(ende) > sum(woche) / len(woche), \
        "Wochenende ist im Schnitt nicht laenger"

    # Und die Ziehung selbst: jeder Wurf landet in einem echten Fach.
    for wurf in range(0, 5000, 7):
        assert arbeit.laenge_des_tages("2026-08-17", wurf) in arbeit.TAGES_LAENGEN




def test_arbeit_tageswordle_ist_ein_wettrennen():
    """Wer zuerst loest, nimmt alles - danach ist die Runde durch.

    So gewuenscht: ein Rennen mit einem Sieger hat Spannung, ein Raetsel, das
    jeder in Ruhe nachholen kann, nicht."""
    arbeit, restore = _arbeit_frisch({1: 0, 2: 0})
    try:
        r = arbeit.instance.raetsel(555)
        r.starten()
        wort = r.wort
        assert r.laeuft and not r.entschieden

        # Nutzer 1 raet daneben, danach richtig.
        assert r.raten(1, "X" * len(wort))[0] == "weiter"
        status, spiel = r.raten(1, wort)
        assert status == "geloest" and r.entschieden and r.gewinner == 1
        assert len(spiel.versuche) == 2

        # Nutzer 2 darf NACH der Entscheidung weiterraten - fuer die eigene
        # Bilanz. Der Topf ist weg, aber der Tag ist nicht gelaufen: vorher war
        # fuer alle ausser dem Sieger sofort Schluss.
        status2, spiel2 = r.raten(2, wort)
        assert status2 == "geloest", status2
        assert spiel2.geloest and r.gespielt(2)
        # Der Sieger bleibt der Erste - daran aendert das nichts.
        assert r.gewinner == 1, "der Sieger wurde ueberschrieben"
        assert r.daten["versuche"] == 2, r.daten["versuche"]
    finally:
        restore()




def test_arbeit_tageswordle_preis():
    """Der Topf haengt an der WORTLAENGE, der Faktor an den Versuchen.

    Vom Nutzer vorgegeben: 5 Buchstaben sollen bei ~50.000 liegen, laengere
    Woerter mehr - und der Anhieb das Doppelte."""
    arbeit, restore = _arbeit_frisch()
    try:
        r = arbeit.instance.raetsel(1)
        # 5 Buchstaben, dritter Versuch: 50.000 x 1.5
        r.daten.update({"datum": arbeit._heute(), "wort": "ABEND",
                        "spieler": {}, "gewinner": 0})
        assert r.topf == 50_000
        assert r.preis(arbeit.Wordle("ABEND", versuche=["A", "B", "ABEND"])) == 75_000
        # Anhieb -> doppelt.
        assert r.preis(arbeit.Wordle("ABEND", versuche=["ABEND"])) == 100_000
        # 8 Buchstaben auf Anhieb: der Sonntags-Jackpot.
        r.daten["wort"] = "ARBEITER"
        assert r.topf == 80_000
        assert r.preis(arbeit.Wordle("ARBEITER", versuche=["ARBEITER"])) == 160_000
        # Letzter Versuch: nur der Grundtopf, nie weniger.
        sechs = arbeit.Wordle("ARBEITER", versuche=["X"] * 5 + ["ARBEITER"])
        assert r.preis(sechs) == 80_000
    finally:
        restore()




def test_arbeit_wordle_faellt_nur_wenn_was_los_ist():
    """Nicht die Uhr entscheidet, sondern der Voice-Kanal.

    So gewuenscht: ein Raetsel um 4 Uhr morgens in einen leeren Server zu
    werfen, waere verschenkt. Bots zaehlen dabei NICHT mit - Flo sitzt oft
    selbst im Call und wuerde sich sonst mitzaehlen."""
    arbeit, restore = _arbeit_frisch()
    try:
        def guild(n_menschen, n_bots=0):
            leute = [SimpleNamespace(bot=False) for _ in range(n_menschen)]
            leute += [SimpleNamespace(bot=True) for _ in range(n_bots)]
            return SimpleNamespace(id=42, voice_channels=[SimpleNamespace(members=leute)])

        assert arbeit.instance.leute_im_voice(guild(3, 2)) == 3, "Bots mitgezaehlt"

        # Zu wenig los: nicht mal ein Termin wird gezogen.
        assert not arbeit.instance.faellig(guild(2)), "bei zwei Leuten schon geplant"
        assert arbeit.instance.raetsel(42).geplant_fuer is None

        # Genug Leute: JETZT wird geplant - aber noch nichts gepostet.
        assert not arbeit.instance.faellig(guild(3)), "sofort gefeuert statt geplant"
        termin = arbeit.instance.raetsel(42).geplant_fuer
        assert termin is not None and termin > int(time.time()), termin

        # Der Termin bleibt STEHEN. Wuerde er bei jedem Tick neu gezogen,
        # ruecke er ewig weiter weg und das Wort kaeme nie.
        assert not arbeit.instance.faellig(guild(5))
        assert arbeit.instance.raetsel(42).geplant_fuer == termin, "Termin verschoben"

        # Termin da und immer noch was los -> jetzt faellt es.
        arbeit.instance.raetsel(42).daten["plan_zeit"] = 0
        assert arbeit.instance.faellig(guild(3))
        # Ist der Call inzwischen leer, wartet Flo trotzdem.
        assert not arbeit.instance.faellig(guild(1)), "in den leeren Call gepostet"

        # Ist das Wort raus, kommt heute keins mehr - egal wie voll es wird.
        arbeit.instance.raetsel(42).starten()
        assert not arbeit.instance.faellig(guild(9)), "zweites Wort am selben Tag"
    finally:
        restore()




def test_arbeit_tagesdeckel_und_serie():
    """Der Tagesdeckel begrenzt WIRKLICH, und die Serie zahlt sich aus.

    Ohne Deckel waere die Schicht die beste Geldquelle im Spiel - sie ist ja
    risikofrei, anders als Casino und Aktie."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        schicht = arbeit.SCHICHTEN["wordle"]

        # Erste volle Schicht: Grundlohn + 5 % Serie (Stufe 0 gibt nichts dazu).
        betrag, info = wp.abrechnen(7, schicht, 1.0)
        assert info["serie"] == 1 and betrag == round(schicht.lohn * 1.05), betrag
        assert info["stufe"].titel == "Praktikant", info["stufe"]

        # Zweite: Serie 2 -> mehr.
        betrag2, info2 = wp.abrechnen(7, schicht, 1.0)
        assert info2["serie"] == 2 and betrag2 > betrag

        # Reinfall setzt die Serie zurueck und zahlt nichts - die STUFE bleibt.
        betrag3, info3 = wp.abrechnen(7, schicht, 0.0)
        assert betrag3 == 0 and info3["serie"] == 0
        assert info3["geschafft"] == 2, "Reinfall hat die Stufe angetastet"

        # Deckel: das Konto auf kurz vor Schluss setzen.
        prof = wp._nutzer(7)
        prof["tag"], prof["heute"] = arbeit._heute(), arbeit.TAGES_DECKEL - 1000
        betrag4, info4 = wp.abrechnen(7, schicht, 1.0)
        assert betrag4 == 1000, betrag4
        assert "Tagesdeckel" in info4["hinweis"], info4["hinweis"]
        # Und danach ist Schluss - aufs Konto kommt wirklich nichts mehr.
        stand = economy.instance._profile(7)["coins"]
        betrag5, info5 = wp.abrechnen(7, schicht, 1.0)
        assert betrag5 == 0 and "voll" in info5["hinweis"].lower(), info5
        assert economy.instance._profile(7)["coins"] == stand, "trotz Deckel gebucht"
        assert wp._nutzer(7)["heute"] == arbeit.TAGES_DECKEL
    finally:
        restore()




def test_arbeit_safe_hinweis_zaehlt_zweistufig():
    """Beim Zahlenschloss dieselbe Falle wie beim Wordle: '111' gegen '123'
    darf NICHT dreimal 'richtige Ziffer am falschen Platz' ergeben."""
    import arbeit
    view = arbeit.SafeView.__new__(arbeit.SafeView)
    view.code = "123"
    assert view.hinweis("123") == (3, 0)
    assert view.hinweis("111") == (1, 0), view.hinweis("111")
    assert view.hinweis("321") == (1, 2)
    assert view.hinweis("456") == (0, 0)
    assert view.hinweis("213") == (1, 2)




def test_arbeit_schichten_sind_vollstaendig():
    """Jede Schicht muss sich selbst beschreiben und ein Gesicht bauen koennen.

    Der Katalog haengt an den Klassen - wer eine sechste Schicht dazulegt, soll
    nichts weiter anfassen muessen. Dieser Test faellt um, wenn doch."""
    import arbeit
    autor = SimpleNamespace(id=1, display_name="Tester")
    for key, schicht in arbeit.SCHICHTEN.items():
        assert schicht.key == key, f"{schicht} kennt seinen eigenen Schluessel nicht"
        assert schicht.titel and schicht.was, key
        assert schicht.lohn > 0, key
        embed, view, datei = asyncio.run(schicht.bauen(arbeit.instance, autor))
        assert embed.description, key
        assert view.uid == 1 and view.schicht is schicht, key
        # Die Frist muss zur Aufgabe passen: sechs Rateversuche brauchen mehr
        # Zeit als fuenf Klicks.
        assert view.timeout == schicht.frist and schicht.frist >= 300, key
    # Wordle bringt sein Brett als Bild mit - genau dafuer gibt es das
    # Drei-Tupel. Faellt render aus, ist datei None und es geht trotzdem.
    _e, _v, datei = asyncio.run(
        arbeit.SCHICHTEN["wordle"].bauen(arbeit.instance, autor))
    assert datei is not None, "Wordle-Schicht ohne Brett-Bild"
    assert arbeit.SCHICHTEN["wordle"].frist > arbeit.SCHICHTEN["sortieren"].frist




def test_arbeit_cooldown_und_befehle():
    """'Flo work' startet eine Schicht, die zweite prallt am Cooldown ab -
    und 'Flo wordle' sagt ehrlich, dass noch kein Wort draussen ist."""
    arbeit, restore = _arbeit_frisch({5: 0})
    gesendet = []
    try:
        autor = _fake_person(5, name="tester", global_name="Tester")
        kanal = SimpleNamespace(
            id=9, send=lambda **kw: _als_coro(gesendet.append(kw) or SimpleNamespace(id=1)))
        msg = SimpleNamespace(content="Flo work", author=autor, channel=kanal,
                              guild=SimpleNamespace(id=42))

        antwort = asyncio.run(arbeit.handle(msg))
        assert antwort is arbeit.HANDLED, antwort
        assert gesendet and "embed" in gesendet[0]

        # Zweiter Versuch sofort danach: Cooldown-Hinweis statt neuer Schicht,
        # und der Hinweis sagt auch, WIE LANGE noch.
        antwort2 = asyncio.run(arbeit.handle(msg))
        text2 = _embed_text(antwort2)
        assert "warten" in text2.lower() and "Minuten" in text2, text2
        assert len(gesendet) == 1, "trotz Cooldown eine zweite Schicht gestartet"

        # Die Liste nennt jede BESTELLBARE Schicht mit ihrem Schluessel.
        # Die seltene steht auch drin, aber ohne Schluessel - man kann sie ja
        # nicht anfordern, und ein Schluessel waere eine Einladung dazu.
        msg.content = "Flo work liste"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        for key, sch in arbeit.SCHICHTEN.items():
            if sch.selten:
                assert "SELTEN" in text and "bestellbar" in text.lower(), text
            else:
                assert key in text, key

        # 'Flo wordle' ist jetzt das SPASS-Wordle - das Wort des Tages hat
        # eigene Woerter. Sonst wuesste man nicht, ob man um 15.000 oder um
        # 80.000 spielt.
        msg.content = "Flo tageswort"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "Voice" in text, text
    finally:
        restore()




def test_arbeit_kanal_wird_vernuenftig_gesucht():
    """Reihenfolge: eingestellter Kanal -> ein Kanal, der passend heisst ->
    Ansagen-Kanal. Eine feste ID im Code gibt es bewusst nicht."""
    arbeit, restore = _arbeit_frisch()
    try:
        gigachat = SimpleNamespace(id=200, name="gigachat")
        sonst = SimpleNamespace(id=201, name="smalltalk")
        eingestellt = SimpleNamespace(id=300, name="wordle-only")
        kanaele = {200: gigachat, 201: sonst, 300: eingestellt}
        guild = SimpleNamespace(id=42, text_channels=[sonst, gigachat],
                                get_channel=kanaele.get, system_channel=None)

        werte = {}
        alt = arbeit.instance._cfg
        arbeit.instance._cfg = lambda gid, key, standard: werte.get(key, standard)
        try:
            # Nichts eingestellt -> der Kanal, der 'gigachat' heisst.
            assert arbeit.instance.kanal_fuer(guild) is gigachat
            # Eingestellt gewinnt.
            werte["wordle_channel"] = 300
            assert arbeit.instance.kanal_fuer(guild) is eingestellt
            # Eingestellte, aber unbekannte ID: nicht ins Leere posten,
            # sondern auf den Namens-Fund zurueckfallen.
            werte["wordle_channel"] = 999
            assert arbeit.instance.kanal_fuer(guild) is gigachat
        finally:
            arbeit.instance._cfg = alt
    finally:
        restore()




def test_arbeit_tick_merkt_sich_die_ansage():
    """tick() liefert (guild, embed, view) - und die Ansage MUSS gemerkt werden.

    Das Raten laeuft ueber ein Eingabefeld, und bei einer Modal-Antwort ist
    interaction.message IMMER None. Ohne die gemerkten IDs koennte die oeffentliche
    Ansage nach dem Sieg nicht auf 'entschieden' umgestellt werden - sie stuende
    bis zum naechsten Tag da und lockte Leute in ein Rennen, das laengst
    gelaufen ist."""
    arbeit, restore = _arbeit_frisch()
    try:
        leute = [SimpleNamespace(bot=False) for _ in range(4)]
        guild = SimpleNamespace(id=77, voice_channels=[SimpleNamespace(members=leute)])

        # Erster Tick: es ist genug los, ALSO wird ein Termin gezogen - aber
        # noch nichts gepostet. Das Wort soll ueberraschen, nicht an der
        # dritten Person im Call haengen.
        assert asyncio.run(arbeit.tick([guild])) == []
        r0 = arbeit.instance.raetsel(77)
        assert r0.geplant_fuer is not None, "kein Termin gezogen"
        assert r0.geplant_fuer >= int(time.time()) + arbeit.VERZUG_MIN
        # Termin vorziehen (im Betrieb wartet Flo 5-45 Minuten).
        r0.daten["plan_zeit"] = 0
        faellig = asyncio.run(arbeit.tick([guild]))
        assert len(faellig) == 1, faellig
        g, embed, view, datei = faellig[0]
        assert g is guild
        # Der Aushang bringt ein leeres Brett mit: man sieht auf einen Blick,
        # wie lang das Wort ist und wie viele Versuche man hat.
        assert datei is not None, "Aushang ohne Brett-Bild"
        assert "Wettrennen" in (embed.description or ""), embed.description
        assert view.children, "kein Rate-Knopf an der Ansage"
        assert view.children[0].custom_id == "flo:wordle:77", view.children[0].custom_id

        # Ein zweiter Tick am selben Tag darf NICHTS mehr liefern.
        assert asyncio.run(arbeit.tick([guild])) == []

        # Ansage merken - genau das macht bot.py nach dem Senden.
        r = arbeit.instance.raetsel(77)
        r.ansage_merken(SimpleNamespace(id=4242, channel=SimpleNamespace(id=99)))
        assert r.daten["ansage"] == 4242 and r.daten["kanal"] == 99

        # Nach dem Sieg zeigt das Embed die Aufloesung statt des Rennens.
        r.raten(5, r.wort)
        fertig = arbeit.instance.tages_embed(77)
        assert "entschieden" in (fertig.title or "").lower(), fertig.title
        assert r.wort in (fertig.description or ""), fertig.description
    finally:
        restore()




def test_arbeit_laufende_schicht_wird_nicht_weggeraeumt():
    """Eine laufende Schicht MUSS sich beim Auto-Loesch-Schutz anmelden.

    Ohne das verschwindet sie in einem Aufraeum-Kanal mitten im Spiel: der
    Cooldown laeuft weiter, der Lohn ist weg, und die Knoepfe zeigen ins Leere.
    Genau das war die Beschwerde. Freigegeben wird erst, wenn die Schicht
    wirklich vorbei ist - dann darf sie nach der Gnadenfrist weg."""
    # Angesetzt wird an laufzeit.py und nicht mehr an bot.py: dort holen sich
    # die Module den Loesch-Schutz seit dem Umbau. Geprueft wird dieselbe Sache
    # wie vorher - nur an der Stelle, an der sie jetzt wirklich passiert.
    import laufzeit
    arbeit, restore = _arbeit_frisch({5: 0})
    geschuetzt, freigegeben = [], []
    alt = (laufzeit.protect_message, laufzeit.release_message)
    laufzeit.protect_message = lambda m: geschuetzt.append(getattr(m, "id", None))
    laufzeit.release_message = lambda m, **kw: freigegeben.append(getattr(m, "id", None))
    try:
        autor = _fake_person(5, name="tester", global_name="Tester")
        kanal = SimpleNamespace(id=9, send=lambda **kw: _als_coro(SimpleNamespace(id=777)))
        msg = SimpleNamespace(content="Flo work sortieren", author=autor,
                              channel=kanal, guild=SimpleNamespace(id=42))
        assert asyncio.run(arbeit.handle(msg)) is arbeit.HANDLED
        assert geschuetzt == [777], geschuetzt
        assert freigegeben == [], "schon vor dem Ende freigegeben"

        # Zeit rum: abrechnen, freigeben - und den STAND stehen lassen.
        view = arbeit.SortierView(arbeit.instance, 5,
                                  arbeit.SCHICHTEN["sortieren"], [3, 1, 2])
        bearbeitet = {}

        async def edit(**kw):
            bearbeitet.update(kw)
        view.message = SimpleNamespace(id=777, edit=edit)
        asyncio.run(view.on_timeout())
        assert freigegeben == [777], freigegeben
        # Nur die Knoepfe sind weg; das Embed wird NICHT ueberschrieben -
        # sonst sieht man nach einer Pause nicht mal mehr, woran man sass.
        assert bearbeitet.get("view") is None and "embed" not in bearbeitet, bearbeitet
        assert "content" in bearbeitet and "Zeit" in bearbeitet["content"]
    finally:
        laufzeit.protect_message, laufzeit.release_message = alt
        restore()




def test_arbeit_wordle_brett_wird_gezeichnet():
    """Das Brett kommt als Bild - mit Tastatur, damit man sieht, welche
    Buchstaben schon raus sind. Und die Farben kommen aus EINER Quelle:
    farben() rechnet, muster() malt Emojis, das Bild nimmt dieselben Zeichen."""
    import arbeit
    import render
    spiel = arbeit.Wordle("ARBEITER", versuche=["MEISTERN", "ARBEITER"])

    # Eine Quelle fuer die Faerbung.
    assert spiel.farben("ARBEITER") == "gggggggg"
    assert spiel.muster("ARBEITER") == "🟩" * 8
    assert [f for _w, f in spiel.zeilen()] == [spiel.farben("MEISTERN"), "gggggggg"]

    # Tastatur: der beste Stand gewinnt. In MEISTERN ist das E gelb, in
    # ARBEITER gruen - gruen muss bleiben.
    tast = spiel.tastatur()
    assert tast["E"] == "g" and tast["A"] == "g", tast
    assert tast["M"] == "b" and tast["S"] == "b", tast
    assert "Q" not in tast, "nie geratener Buchstabe steht in der Tastatur"

    # Und das Bild entsteht wirklich.
    buf = render.wordle_board(spiel.zeilen(), spiel.laenge, titel="TEST",
                              tastatur=tast)
    kopf = buf.read(8)
    assert kopf.startswith(b"\x89PNG"), kopf
    # Auch mit acht Buchstaben und ohne Tastatur darf nichts umfallen.
    assert render.wordle_board([], 8, titel="LEER").read(4) == b"\x89PNG"




def test_arbeit_wordle_ist_selten_und_nicht_bestellbar():
    """Wordle soll ein Highlight sein, keine Routine.

    Beides gehoert zusammen und war der eigentliche Denkfehler davor: eine
    seltene Schicht, die man sich per 'Flo work wordle' jederzeit holen kann,
    ist nicht selten - sie ist nur schlecht sortiert. Also: kaum gezogen UND
    nicht bestellbar, dafuer deutlich besser bezahlt."""
    arbeit, restore = _arbeit_frisch({5: 0})
    try:
        # Etwa jede sechzehnte Schicht - nicht jede fuenfte.
        p = arbeit.seltene_chance()
        assert 0.03 < p < 0.10, p
        # Und sie zahlt sich aus: klar mehr als jede normale Schicht.
        normal = max(s.lohn for s in arbeit.SCHICHTEN.values() if not s.selten)
        selten = max(s.lohn for s in arbeit.SCHICHTEN.values() if s.selten)
        assert selten >= normal * 2.5, (normal, selten)

        # Die Ziehung haelt sich grob an die Gewichte.
        random.seed(4)
        gezogen = [arbeit.schicht_ziehen().key for _ in range(4000)]
        anteil = gezogen.count("wordle") / len(gezogen)
        assert abs(anteil - p) < 0.02, (anteil, p)
        # Jede Schicht kommt ueberhaupt vor - sonst ist eine tot.
        assert set(gezogen) == set(arbeit.SCHICHTEN), set(arbeit.SCHICHTEN) - set(gezogen)

        # Bestellen geht nicht - und die Absage erklaert, warum.
        autor = _fake_person(5, name="tester", global_name="Tester")
        msg = SimpleNamespace(content="Flo work wordle", author=autor,
                              channel=SimpleNamespace(id=9), guild=SimpleNamespace(id=42))
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "nicht aussuchen" in text, text
        # Wichtig: der Cooldown darf dabei NICHT anspringen - man hat ja
        # nicht gearbeitet, nur gefragt.
        assert arbeit.instance._nutzer(5)["cooldown"] == 0, "Absage kostet Cooldown"
    finally:
        random.seed()
        restore()




def test_arbeit_neue_schichten_funktionieren():
    """Werkzeug sortieren (Memory) und Qualitaetskontrolle - die zwei, die den
    Platz fuellen, den Wordle als seltene Schicht frei gemacht hat."""
    import arbeit
    autor = SimpleNamespace(id=1, display_name="Tester")

    # Memory: acht Kisten, vier Paare, jedes Werkzeug genau zweimal.
    _e, view, _d = asyncio.run(
        arbeit.SCHICHTEN["paare"].bauen(arbeit.instance, autor))
    assert len(view.karten) == 8, view.karten
    for stueck in set(view.karten):
        assert view.karten.count(stueck) == 2, (stueck, view.karten)
    assert len(view.children) == 8
    # Verdeckt starten: kein Knopf verraet schon sein Werkzeug.
    assert all(k.emoji is None for k in view.children), "Kisten liegen offen"

    # Qualitaetskontrolle: fuenf Stuecke, genau eins aus einer anderen Kiste.
    _e, view2, _d = asyncio.run(
        arbeit.SCHICHTEN["kontrolle"].bauen(arbeit.instance, autor))
    woerter = [k.label for k in view2.children]
    assert len(woerter) == 5 and len(set(woerter)) == 5, woerter
    assert view2.ausschuss in woerter
    daheim = set(arbeit.KontrolleSchicht.KISTEN[view2.heimat])
    fremde = [w for w in woerter if w not in daheim]
    assert fremde == [view2.ausschuss], (fremde, view2.ausschuss)

    # Keine Kiste teilt sich ein Wort mit einer anderen - sonst gaebe es
    # Runden mit zwei richtigen Antworten.
    alle = [w for worte in arbeit.KontrolleSchicht.KISTEN.values() for w in worte]
    assert len(alle) == len(set(alle)), "Wort kommt in zwei Kisten vor"




def test_arbeit_karriere_geht_nie_zurueck():
    """Die Stufe ist das Rueckgrat: sie zaehlt GESCHAFFTE Schichten und faellt
    nie. Vorher gab es nur die Serie - ein Reinfall und alles war weg, ueber
    Wochen baute man nichts auf."""
    import arbeit
    assert arbeit.stufe_fuer(0).titel == "Praktikant"
    assert arbeit.stufe_fuer(9).titel == "Praktikant"
    assert arbeit.stufe_fuer(10).titel == "Aushilfe"
    assert arbeit.stufe_fuer(10**6).titel == arbeit.STUFEN[-1].titel

    # Monoton: mehr Schichten sind nie eine schlechtere Stufe.
    letzter = -1.0
    for n in range(0, 900, 7):
        bonus = arbeit.stufe_fuer(n).bonus
        assert bonus >= letzter, (n, bonus, letzter)
        letzter = bonus

    # Die Schwellen muessen aufsteigen, sonst greift stufe_fuer daneben.
    schwellen = [st.ab for st in arbeit.STUFEN]
    assert schwellen == sorted(schwellen) and len(set(schwellen)) == len(schwellen)
    assert arbeit.STUFEN[0].ab == 0, "es gibt keine Stufe fuer Anfaenger"
    assert arbeit.naechste_stufe(0).titel == "Aushilfe"
    assert arbeit.naechste_stufe(10**6) is None, "oben muss Schluss sein"




def test_arbeit_lohn_ist_additiv_und_gedeckelt():
    """Serie und Stufe werden ADDIERT, nicht multipliziert.

    Multiplikativ schaukeln sich zwei Zuschlaege von je +50 % zu +125 % auf -
    und dann faengt die Schicht an, das Wort des Tages zu ueberholen, das ja
    der Hoehepunkt bleiben soll."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        schicht = arbeit.SCHICHTEN["safe"]
        prof = wp._nutzer(7)
        # Volle Serie und hoechste Stufe von Hand setzen.
        prof["serie"] = 999
        prof["geschafft"] = arbeit.STUFEN[-1].ab
        max_faktor = 1.0 + arbeit.SERIE_MAX + arbeit.STUFEN[-1].bonus
        assert max_faktor <= 2.05, max_faktor      # nicht aus dem Ruder

        betrag, info = wp.abrechnen(7, schicht, 1.0)
        erwartet = round(schicht.lohn * 1.0 * (1.0 + info["serie_bonus"]
                                               + info["stufe"].bonus))
        assert betrag == erwartet, (betrag, erwartet)
        # Selbst im Bestfall bleibt eine normale Schicht unter dem kleinsten
        # Tages-Wordle-Topf - sonst waere das Wort des Tages entwertet.
        kleinster_topf = min(arbeit.TAGES_LAENGEN) * arbeit.TAGES_PRO_BUCHSTABE
        beste_normal = max(s.lohn for s in arbeit.SCHICHTEN.values() if not s.selten)
        assert beste_normal * 1.2 * max_faktor < kleinster_topf
    finally:
        restore()




def test_arbeit_goldene_schicht_verdoppelt():
    """Gold wird BEIM START gewuerfelt, damit es dransteht, waehrend man
    arbeitet - und verdoppelt am Ende den Lohn. Auf null bleibt null: fuer eine
    verpatzte Schicht gibt es auch doppelt nichts."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        schicht = arbeit.SCHICHTEN["safe"]
        normal, _i = wp.abrechnen(7, schicht, 1.0)
        wp._nutzer(7)["serie"] = 0          # gleiche Ausgangslage
        gold, info = wp.abrechnen(7, schicht, 1.0, gold=True)
        assert gold == normal * 2, (normal, gold)
        assert info["gold"] is True and wp._nutzer(7)["gold"] == 1

        # Verpatzt: kein Lohn, also auch keine Gold-Zaehlung.
        _b, info2 = wp.abrechnen(7, schicht, 0.0, gold=True)
        assert info2["gold"] is False and wp._nutzer(7)["gold"] == 1

        # Die Chance muss eine Chance bleiben - nicht immer, nicht nie.
        assert 0.0 < arbeit.GOLD_CHANCE < 0.35, arbeit.GOLD_CHANCE
        random.seed(11)
        views = [arbeit.SchichtView(wp, 7, schicht) for _ in range(3000)]
        anteil = sum(1 for v in views if v.gold) / len(views)
        assert abs(anteil - arbeit.GOLD_CHANCE) < 0.03, anteil
    finally:
        random.seed()
        restore()




def test_arbeit_wordle_bilanz_wird_mitgeschrieben():
    """Jede Teilnahme am Wort des Tages zaehlt genau einmal, jeder Erfolg
    landet in der Verteilung - auch der, fuer den es kein Geld mehr gab."""
    arbeit, restore = _arbeit_frisch({1: 0, 2: 0})
    try:
        wp = arbeit.instance
        r = wp.raetsel(900)
        r.starten()
        wort = r.wort

        gesendet = []

        class Antwort:
            def __init__(self):
                self.done = False

            async def send_message(self, **kw):
                gesendet.append(kw)

        class Ia:
            def __init__(self, uid):
                self.user = SimpleNamespace(id=uid, display_name=f"U{uid}")
                self.response = Antwort()
                self.channel = SimpleNamespace(
                    send=lambda **kw: _als_coro(None))
                self.client = SimpleNamespace(get_channel=lambda _c: None)

        # Nutzer 1: einmal daneben, dann richtig -> Sieger.
        asyncio.run(wp.tages_antwort(Ia(1), 900, "X" * len(wort)))
        asyncio.run(wp.tages_antwort(Ia(1), 900, wort))
        p1 = wp._nutzer(1)
        assert p1["wordle_gespielt"] == 1, "Teilnahme mehrfach gezaehlt"
        assert p1["wordle_siege"] == 1
        assert p1["wordle_verteilung"][1] == 1, p1["wordle_verteilung"]

        # Nutzer 2 loest NACH der Entscheidung: Bilanz ja, Geld nein.
        vorher = economy.instance._profile(2)["coins"]
        asyncio.run(wp.tages_antwort(Ia(2), 900, wort))
        p2 = wp._nutzer(2)
        assert p2["wordle_siege"] == 1 and p2["wordle_verteilung"][0] == 1
        assert economy.instance._profile(2)["coins"] == vorher, "trotzdem bezahlt"
        assert r.gewinner == 1, "Sieger ueberschrieben"
    finally:
        restore()




def test_arbeit_lohnzettel_und_rangliste_als_karte():
    """Beide Karten entstehen wirklich - und ohne Bild gibt es einen Textweg,
    damit eine fehlende Schrift niemandem seine Zahlen vorenthaelt."""
    import render
    arbeit, restore = _arbeit_frisch({5: 0, 6: 0})
    try:
        wp = arbeit.instance
        for _ in range(12):
            wp.abrechnen(5, arbeit.SCHICHTEN["safe"], 1.0)
        wp.abrechnen(6, arbeit.SCHICHTEN["salat"], 1.0)
        prof = wp._nutzer(5)
        assert arbeit.stufe_fuer(prof["geschafft"]).titel == "Aushilfe"

        buf = render.lohnzettel("Tester", None, stufe="Aushilfe", symbol="🧹",
                                bonus=0.08, geschafft=12, angetreten=12, serie=12,
                                beste_serie=12, verdient=90000, heute=90000,
                                deckel=arbeit.TAGES_DECKEL, gold=0,
                                naechste="Facharbeiter", fehlt=18,
                                wordle=(2, 5, [0, 1, 1, 0, 0, 0]))
        assert buf.read(4) == b"\x89PNG"
        rows = [(1, "Tester", "🧹", "Aushilfe", 12, 90000)]
        assert render.arbeit_rangliste(rows).read(4) == b"\x89PNG"

        # Der Textweg trägt dieselben Zahlen.
        text = _embed_text(wp._lohnzettel_text(
            SimpleNamespace(id=5, display_name="Tester"), prof, 90000,
            arbeit.stufe_fuer(12), arbeit.naechste_stufe(12)))
        assert "Aushilfe" in text and "12" in text, text

        # Die Rangliste sortiert nach Verdienst, nicht nach ID.
        gesendet = []
        msg = SimpleNamespace(
            guild=None, mentions=[],
            author=SimpleNamespace(id=5, display_name="Tester"),
            channel=SimpleNamespace(send=lambda **kw: _als_coro(
                gesendet.append(kw) or SimpleNamespace(id=1))))
        assert asyncio.run(wp._rangliste(msg)) is arbeit.HANDLED
        assert gesendet and "file" in gesendet[0]
    finally:
        restore()




def test_arbeit_karriere_steigt_wirklich_und_faellt_nie_zurueck():
    """Nachgespielt statt nachgelesen: 12 Schichten am Stueck, dann verlieren,
    dann ein Spass-Wordle. Die Frage "ab wann steigt die Stufe ueberhaupt?"
    muss der Test beantworten koennen, nicht der Quelltext."""
    arbeit, restore = _arbeit_frisch()
    A = arbeit.instance
    uid = 5150501
    salat = arbeit.SCHICHTEN["salat"]
    try:
        _karriere_durchspielen(arbeit, A, uid, salat)
    finally:
        restore()




def test_arbeit_zeigt_den_fortschritt_zur_naechsten_stufe():
    """Die Karriere FUNKTIONIERTE, aber wie weit es noch ist, stand nur klein in
    der Fusszeile - ueber Stunden sah es deshalb aus, als passiere nichts. Der
    Balken gehoert an das Stufen-Feld selbst.

    Und er darf nicht luegen: bei 29 von 30 ist er NICHT voll."""
    import arbeit
    import inspect
    quelle = inspect.getsource(arbeit.Arbeit.ergebnis_embed)
    assert "\u25b0" in quelle and "\u25b1" in quelle, "kein Fortschrittsbalken am Stufen-Feld"
    assert "int(anteil * 10)" in quelle, (
        "der Balken rundet wieder - dann steht er bei 29/30 auf voll")

    def balken(hab):
        st = arbeit.stufe_fuer(hab)
        w = arbeit.naechste_stufe(hab)
        if w is None:
            return None
        anteil = 0.0 if w.ab <= st.ab else (hab - st.ab) / (w.ab - st.ab)
        return max(0, min(10, int(anteil * 10)))

    assert balken(10) == 0, "frisch aufgestiegen -> leerer Balken"
    assert balken(29) == 9, "29 von 30 darf NICHT voll aussehen"
    assert balken(7) == 7
    assert balken(600) is None, "hoechste Stufe hat keinen naechsten Schritt"




def test_arbeit_spasswordle_deckel_haelt_wirklich():
    """'Flo wordle' zum Spass - aber NIE mehr als der Deckel hergibt.

    Der Deckel greift VOR dem Gold-Bonus. Danach waere die Obergrenze in
    Wahrheit das Doppelte, und "nie mehr als 15.000" waere gelogen."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        prof = wp._nutzer(7)
        # Hoechste Stufe und volle Serie: der Deckel muss trotzdem halten.
        prof["serie"] = 999
        prof["geschafft"] = arbeit.STUFEN[-1].ab

        for laenge in sorted(arbeit.WOERTER):
            runde = arbeit.SPASS.fuer_laenge(laenge)
            for anteil in (1.5, 1.2, 1.0, 0.5):
                for gold in (False, True):
                    prof["heute"] = 0          # Tagesdeckel wegdenken
                    prof["tag"] = arbeit._heute()
                    betrag, _i = wp.abrechnen(7, runde, anteil, gold=gold)
                    assert betrag <= arbeit.SPASS_MAX, (laenge, anteil, gold, betrag)

        # Ohne Deckel waere es wirklich mehr - der Deckel ist also kein Deko.
        acht = arbeit.SPASS.fuer_laenge(8)
        assert acht.lohn * 1.5 > arbeit.SPASS_MAX, "Deckel greift nie"

        # Und es bleibt klar unter dem Wort des Tages.
        kleinster_topf = min(arbeit.TAGES_LAENGEN) * arbeit.TAGES_PRO_BUCHSTABE
        assert arbeit.SPASS_MAX < kleinster_topf, (arbeit.SPASS_MAX, kleinster_topf)
        assert arbeit.SPASS_MAX <= 15000, arbeit.SPASS_MAX
    finally:
        restore()




def test_arbeit_spasswordle_ist_keine_karriere():
    """Zeitvertreib zaehlt NICHT fuer Stufe und Serie.

    Sonst waere der Werksleiter der, der am meisten geraten hat - und die
    Karriere haette nichts mehr mit Arbeit zu tun."""
    arbeit, restore = _arbeit_frisch({7: 0})
    try:
        wp = arbeit.instance
        runde = arbeit.SPASS.fuer_laenge(5)
        for _ in range(40):
            wp.abrechnen(7, runde, 1.0)
        prof = wp._nutzer(7)
        assert prof["geschafft"] == 0, "Spass zaehlt fuer die Karriere"
        assert prof["serie"] == 0, "Spass baut eine Serie auf"
        assert prof["schichten"] == 0
        # Aber die eigene Bilanz wird gefuehrt.
        assert prof["spass_gespielt"] == 40 and prof["spass_siege"] == 40
        # Eine echte Schicht danach ist trotzdem Schicht 1.
        wp.abrechnen(7, arbeit.SCHICHTEN["safe"], 1.0)
        assert wp._nutzer(7)["geschafft"] == 1

        # Und die Runde wird NIE per 'Flo work' gezogen.
        assert "spasswordle" not in arbeit.SCHICHTEN
        random.seed(3)
        assert all(arbeit.schicht_ziehen().key != "spasswordle"
                   for _ in range(2000))
    finally:
        random.seed()
        restore()




def test_arbeit_spasswordle_eigener_cooldown_und_laenge():
    """Das Spass-Wordle hat einen EIGENEN, kurzen Cooldown - es darf die
    Schicht nicht blockieren und umgekehrt. Und die Laenge darf man waehlen."""
    arbeit, restore = _arbeit_frisch({5: 0})
    gesendet = []
    try:
        autor = _fake_person(5, name="tester", global_name="Tester")
        kanal = SimpleNamespace(id=9, send=lambda **kw: _als_coro(
            gesendet.append(kw) or SimpleNamespace(id=1)))
        msg = SimpleNamespace(content="Flo wordle 7", author=autor, channel=kanal,
                              guild=SimpleNamespace(id=42))
        assert asyncio.run(arbeit.handle(msg)) is arbeit.HANDLED
        assert len(gesendet) == 1 and "file" in gesendet[0], gesendet[0].keys()
        prof = arbeit.instance._nutzer(5)
        # Eigener Schluessel, kurzer Cooldown - und die SCHICHT ist frei.
        assert prof["cooldown_spass"] > int(time.time()), prof
        assert prof["cooldown"] == 0, "Spass hat die Schicht blockiert"
        assert prof["schichten"] == 0, "Spass wurde als Schicht gezaehlt"

        # Direkt nochmal: Cooldown-Hinweis, in SEKUNDEN (er ist kurz).
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "warten" in text.lower(), text
        assert len(gesendet) == 1

        # Eine echte Schicht geht trotzdem sofort.
        msg.content = "Flo work sortieren"
        assert asyncio.run(arbeit.handle(msg)) is arbeit.HANDLED
        assert len(gesendet) == 2

        # Unsinnige Laenge wird erklaert, nicht still auf 5 gedreht.
        arbeit.instance._nutzer(5)["cooldown_spass"] = 0
        msg.content = "Flo wordle 12"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "Buchstaben" in text and str(min(arbeit.WOERTER)) in text, text
        msg.content = "Flo wordle blau"
        text = _embed_text(asyncio.run(arbeit.handle(msg)))
        assert "wordle" in text.lower(), text
        assert len(gesendet) == 2, "trotz Murks eine Runde gestartet"
    finally:
        restore()




def test_arbeit_wordle_versteht_vertipper_und_fremdsprachen():
    """'Flo wordle' muss auch bei Vertippern und auf Englisch/Bayrisch greifen.

    Zwei Ebenen: die haeufigsten Schreibweisen stehen direkt im Modul, alles
    Weitere faengt cmdnorm ab (ein Tippfehler, kein Buchstaben-Tausch)."""
    import arbeit
    import cmdnorm
    # Direkt verstanden - ohne Umweg ueber die Korrektur.
    for wort in ("wordle", "wordl", "wordel", "worlde", "wörtle"):
        assert wort in arbeit._WORDLE_CMDS, wort
    # Wort des Tages hat eigene Woerter, und 'daily' ist NICHT dabei:
    # das ist seit immer der Tagesbonus aus economy.
    assert "daily" not in arbeit._TAGES_CMDS
    assert "daily" in cmdnorm.KNOWN
    assert not set(arbeit._WORDLE_CMDS) & set(arbeit._TAGES_CMDS)




def test_wordle_von_hand_nimmt_niemandem_seine_versuche():
    """Der Knopf im Panel muss auch dann sicher sein, wenn schon gespielt wird.

    Der haeufigste Grund fuer "es kam kein Wort des Tages" ist NICHT, dass
    keines gezogen wurde - sondern dass die Nachricht nicht ankam oder im
    falschen Kanal landete. Wuerde der Knopf dann die Runde neu starten, waeren
    alle Versuche aller Mitspieler weg. Er haengt deshalb nur den Aushang neu
    aus."""
    arbeit, restore = _arbeit_frisch()
    try:
        guild = SimpleNamespace(id=4242, name="Heimat", text_channels=[],
                                voice_channels=[], system_channel=None)
        # 1. Nichts gestartet, KEINER im Voice, kein Termin -> trotzdem los.
        #    Genau das ist der Zweck des Knopfes.
        assert not arbeit.instance.faellig(guild), "waere ohnehin schon faellig"
        embed, view, datei, frisch = asyncio.run(
            arbeit.instance.jetzt_starten(guild))
        assert frisch is True
        raetsel = arbeit.instance.raetsel(4242)
        assert raetsel.laeuft, "es laeuft danach gar keine Runde"
        wort = raetsel.wort
        assert wort, "kein Wort gezogen"
        assert embed is not None and view is not None

        # 2. Jemand hat schon geraten. Zweiter Druck darf das NICHT wegwerfen.
        raetsel.daten["spieler"]["7"] = ["HAUS"]
        raetsel.daten["versuche"] = 1
        embed2, view2, datei2, frisch2 = asyncio.run(
            arbeit.instance.jetzt_starten(guild))
        assert frisch2 is False, "startet die laufende Runde neu"
        assert arbeit.instance.raetsel(4242).wort == wort, "das Wort wurde getauscht"
        assert arbeit.instance.raetsel(4242).daten["spieler"].get("7") == ["HAUS"], (
            "die Versuche der Mitspieler wurden geloescht")
        assert embed2 is not None and view2 is not None

        # 3. Ist das Wort von heute schon geloest, ist der Knopf sinnlos - und
        #    muss das SAGEN statt still ein zweites Raetsel hinzuwerfen.
        arbeit.instance.raetsel(4242).daten["gewinner"] = 7
        try:
            asyncio.run(arbeit.instance.jetzt_starten(guild))
            raise AssertionError("haette abgelehnt werden muessen")
        except ValueError as exc:
            assert "geloest" in str(exc).lower(), exc

        # 4. Ganz ausgeschaltet -> ebenfalls ein klarer Satz, kein Absturz.
        arbeit.instance._enabled = False
        try:
            asyncio.run(arbeit.instance.jetzt_starten(guild))
            raise AssertionError("haette abgelehnt werden muessen")
        except ValueError as exc:
            assert "ausgeschaltet" in str(exc).lower(), exc
    finally:
        restore()


if __name__ == "__main__":
    run(globals())
