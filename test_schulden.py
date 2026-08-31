"""Schuldbuch: Leihen, Tilgen, Erlassen, Pranger.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur schulden      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _embed_text, _fake_msg, _fake_person, _schuld,
    _schulden_setup, _with_economy)



def test_schulden_zahlung_wird_angerechnet():
    """Wer seinem Glaeubiger etwas ueberweist, meint fast immer die Schuld.

    Der Rest ist ein Geschenk - und aus einem Geschenk wird nie eine
    Gegenforderung (genau das war der alte Konstruktionsfehler)."""
    restore, sch = _schulden_setup({1: 0, 2: 0})
    try:
        _schuld(sch, 1, 2, 3_000)           # 2 schuldet 1 dreitausend
        block = sch.pay_block(2, 1, 1_000, ziel_name="Anna")
        assert sch.saldo(1, 2) == 2_000
        assert "2.000" in block["stand"], block["stand"]

        # Mehr zahlen als offen ist: der Ueberschuss ist geschenkt, KEINE
        # Forderung in die Gegenrichtung.
        block = sch.pay_block(2, 1, 5_000, ziel_name="Anna")
        assert sch.saldo(1, 2) == 0 and sch.saldo(2, 1) == 0
        assert "beglichen" in block["stand"].lower()

        # Zahlung an jemanden, dem man nichts schuldet: reines Geschenk.
        block = sch.pay_block(2, 9, 500)
        assert sch.saldo(9, 2) == 0 and sch.saldo(2, 9) == 0
        assert "geschenk" in block["stand"].lower()
    finally:
        restore()




def test_schulden_posten_summen_und_erlassen():
    """Uebersicht (wer bekommt, wer schuldet), Summen, Top-Liste und Erlassen."""
    restore, sch = _schulden_setup()
    try:
        A, B, C, D = 1, 2, 3, 4
        _schuld(sch, A, B, 1000)      # B schuldet A 1.000
        _schuld(sch, A, C, 2500)      # C schuldet A 2.500
        _schuld(sch, D, A, 400)       # A schuldet D   400

        forderungen, schulden_ = sch.posten(A)
        assert forderungen == [(C, 2500), (B, 1000)]     # absteigend
        assert schulden_ == [(D, 400)]
        haben, soll, netto = sch.summen(A)
        assert (haben, soll, netto) == (3500, 400, 3100)

        # Top-Liste serverweit: (glaeubiger, schuldner, betrag)
        top = sch.top(10)
        assert top[0] == (A, C, 2500) and (A, B, 1000) in top and (D, A, 400) in top

        # Erlassen: nur der Glaeubiger, teilweise und ganz.
        weg, rest = sch.erlassen(A, C, 500)
        assert (weg, rest) == (500, 2000) and sch.saldo(A, C) == 2000
        weg, rest = sch.erlassen(A, C)                    # der Rest
        assert (weg, rest) == (2000, 0) and sch.saldo(A, C) == 0
        # Wer nichts zu bekommen hat, kann nichts erlassen.
        assert sch.erlassen(C, A, 100) == (0, 0)
        assert sch.erlassen(A, D, 100)[0] == 0            # A ist hier Schuldner
        assert sch.saldo(D, A) == 400                     # unveraendert
        # Mehr erlassen als offen ist -> nur das Offene.
        weg, rest = sch.erlassen(A, B, 999_999)
        assert (weg, rest) == (1000, 0)
        # Ein erledigter Posten bleibt als Historie stehen.
        assert len(sch.buch.alle()) == 3
        assert [p.status for p in sch.buch.alle()].count("erlassen") == 2
    finally:
        restore()




def test_schulden_pay_als_leihgabe_fragt_nach():
    """'pay @wer 5k als leihgabe' darf NICHT sofort buchen - daraus wird eine
    Schuld, und die entsteht nur mit Zustimmung."""
    import schulden
    restore, sch = _schulden_setup({7: 50_000, 8: 0})
    ziel = _fake_person(uid=8, name="empfaenger")
    msg = SimpleNamespace(content="flo pay <@8> 5k als leihgabe", mentions=[ziel],
                          author=_fake_person(uid=7, name="zahler"),
                          guild=SimpleNamespace(id=1, get_member=lambda _u: None))
    gestellt = {}

    async def fake_anfrage(message, ziel_, betrag, *, grund, faellig, mit_geld):
        gestellt.update(betrag=betrag, grund=grund, mit_geld=mit_geld)
        return schulden.HANDLED

    alt = schulden.leih_anfrage
    schulden.leih_anfrage = fake_anfrage
    try:
        antwort = asyncio.run(economy.instance._pay(msg))
        assert antwort is schulden.HANDLED
        assert gestellt == {"betrag": 5000, "grund": "", "mit_geld": True}, gestellt
        # Kein Coin hat sich bewegt.
        assert economy.get_coins(7) == 50_000 and economy.get_coins(8) == 0
    finally:
        schulden.leih_anfrage = alt
        restore()




def test_schulden_tilgung_bevorzugt_ueberfaellige():
    """Ueberfaellige Posten zuerst, danach der Rest - und innerhalb einer
    Person der aelteste Posten zuerst (FIFO)."""
    restore, sch = _schulden_setup({1: 0, 2: 0, 3: 0})
    try:
        jetzt = time.time()
        # B: ein ueberfaelliger Posten. C: ein dickerer, aber nicht faelliger.
        p_alt = _schuld(sch, 2, 1, 1_000, faellig=jetzt - 86400,
                        entstanden=jetzt - 20 * 86400)
        _schuld(sch, 3, 1, 9_000, entstanden=jetzt - 10 * 86400)
        economy.add_coins(1, 5_000, reason="casino")     # 1.000 Budget
        # Alles ging an den ueberfaelligen Posten, obwohl der kleiner ist.
        assert p_alt.offen == 0 and p_alt.status == "getilgt"
        assert sch.saldo(3, 1) == 9_000
        assert economy.get_coins(2) == 1_000 and economy.get_coins(3) == 0

        # FIFO innerhalb eines Glaeubigers: der aeltere Posten zuerst.
        sch.erlassen(3, 1, 9_000)                        # den dicken wegraeumen
        alt_1 = _schuld(sch, 3, 1, 500, entstanden=jetzt - 30 * 86400)
        neu_1 = _schuld(sch, 3, 1, 500, entstanden=jetzt - 1 * 86400)
        economy.add_coins(1, 1_000, reason="casino")     # 200 Budget
        assert (alt_1.offen, neu_1.offen) == (300, 500), (alt_1.offen, neu_1.offen)
    finally:
        restore()




def test_schulden_grenzen_und_kreditwuerdigkeit():
    """Grenzen schuetzen beide Seiten - und die Kreditwuerdigkeit bewegt sich
    nur durch eigenes Verhalten."""
    import schulden
    restore, sch = _schulden_setup({1: 10_000_000, 2: 1_000_000})
    try:
        # Zu klein lohnt die Buchfuehrung nicht.
        ok, fehler = sch._darf_anlegen(1, 2, 10)
        assert not ok and "lohnt" in fehler

        # Bei sich selbst geht gar nichts.
        assert sch._darf_anlegen(1, 1, 1_000)[0] is False

        # Der Start-Score ist 50 und die Ampel gelb.
        assert sch.score.score(2) == 50 and sch.score.ampel(2) == "🟡"

        # Hoechstens fuenf offene Posten je Paar.
        for _ in range(schulden.MAX_POSTEN_JE_PAAR):
            _schuld(sch, 1, 2, 1_000)
        ok, fehler = sch._darf_anlegen(1, 2, 1_000)
        assert not ok and "offene" in fehler.lower()
        for p in sch.buch.alle():
            p.status = "erlassen"
            p.offen = 0

        # Puenktlich getilgt hebt den Score, ueberfaellig senkt ihn.
        p = _schuld(sch, 1, 2, 1_000, faellig=time.time() + 86400)
        sch._abbuchen(p, 1_000)
        assert sch.score.score(2) == 55, sch.score.score(2)
        sch.score.notiere(2, schulden.Kreditwuerdigkeit.UEBERFAELLIG, "test")
        assert sch.score.score(2) == 45

        # Alte Eintraege zaehlen nur halb.
        sch.buch.score_daten()["2"] = [
            {"t": time.time() - 200 * 86400, "d": -20, "g": "alt"}]
        assert sch.score.score(2) == 40

        # Der Score deckelt KEINE Betraege mehr. Die einzige Betragsgrenze ist
        # der Kontostand des Verleihers - sein GANZES Geld darf er verleihen.
        assert not hasattr(sch.score, "leih_limit"), (
            "leih_limit lebt wieder in der Kreditwuerdigkeit - genau dort ist "
            "die alte Score-Regel schon einmal eingewandert")
        habe = economy.get_coins(1)
        assert habe == 10_000_000, habe
        # Genau alles geht - das ist der ganze Punkt der Aenderung.
        assert sch._darf_anlegen(1, 2, habe)[0] is True, "das eigene Geld ist tabu"
        # Ein Coin mehr nicht, und die Meldung nennt den Kontostand statt Score.
        ok, fehler = sch._darf_anlegen(1, 2, habe + 1)
        assert not ok, "man kann mehr verleihen als man hat"
        import numfmt
        assert "Konto" in fehler and numfmt.fmt(habe) in fehler, fehler
        assert "Kreditwürdigkeit" not in fehler, (
            f"die Absage redet weiter vom Score: {fehler}")

        # Unter 20 ist ganz Schluss.
        sch.buch.score_daten()["2"] = [{"t": time.time(), "d": -40, "g": "test"}]
        gesperrt, warum = sch.score.gesperrt(2)
        assert gesperrt and "niedrig" in warum
    finally:
        restore()




def test_leihgabe_wird_geprueft_BEVOR_sie_oeffentlich_steht():
    """'Flo pay @wer 5k als leihgabe' lief an allen Grenzen vorbei.

    Der Weg kommt aus economy und ruft _anfrage_stellen direkt auf - die
    Pruefungen sassen aber in _cmd_leih. Ergebnis: das Angebot wurde oeffentlich
    gepostet, der andere klickte, und ERST dann platzte es. Jetzt prueft
    _anfrage_stellen selbst, also fuer alle drei Wege gleich."""
    restore, sch = _schulden_setup({1: 1_000, 2: 1_000})
    try:
        gepostet = []

        async def reply(*a, **kw):
            gepostet.append(kw)
            return SimpleNamespace(id=1, guild=None)

        msg = _fake_msg(1, "pay <@2> 50k als leihgabe")
        msg.reply = reply
        ziel = SimpleNamespace(id=2, bot=False, display_name="Kumpel")

        # 50k, aber nur 1k auf dem Konto -> Absage, und NICHTS im Kanal.
        antwort = asyncio.run(sch._anfrage_stellen(
            msg, ziel, 50_000, grund="", faellig=0, mit_geld=True))
        assert not gepostet, "das Angebot stand oeffentlich, bevor es geprueft war"
        assert "Konto" in str(antwort), antwort

        # Was durchgeht, wird gepostet - und das Embed muss sagen, worauf man
        # klickt: die 20 % jeder Einnahme standen bisher nirgends dort.
        antwort = asyncio.run(sch._anfrage_stellen(
            msg, ziel, 1_000, grund="", faellig=0, mit_geld=True))
        assert len(gepostet) == 1, gepostet
        emb = gepostet[0]["embed"]
        text = " ".join([emb.title or "", emb.description or ""]
                        + [f"{f.name} {f.value}" for f in emb.fields])
        assert "%" in text and "Einnahme" in text, (
            f"das Angebot verschweigt die Tilgungsautomatik: {text}")
    finally:
        restore()




def test_schuldschein_braucht_kein_bargeld():
    """Beim Schuldschein fliesst KEIN Geld - dort darf der Kontostand nichts
    blockieren.

    'Kumpel schuldet mir noch 5.000 vom Kinoabend' ist eine Beurkundung von
    etwas, das laengst passiert ist. Ob mein Geld gerade im Casino oder in
    Aktien steckt, geht die Sache nichts an. Beim Leihen ist es umgekehrt:
    da fliesst echtes Geld, das ich haben muss."""
    restore, sch = _schulden_setup({1: 0, 2: 5_000})
    try:
        # Kontostand 0 - als Schuldschein trotzdem in Ordnung.
        assert sch._darf_anlegen(1, 2, 5_000, mit_geld=False)[0] is True, (
            "der Schuldschein wird am Bargeld des Ausstellers gemessen")
        # Mit echtem Geldfluss geht dasselbe nicht.
        ok, fehler = sch._darf_anlegen(1, 2, 5_000, mit_geld=True)
        assert not ok and "Konto" in fehler, fehler
        # Die anderen Grenzen gelten beim Schuldschein weiter.
        assert sch._darf_anlegen(1, 2, 10, mit_geld=False)[0] is False
        assert sch._darf_anlegen(1, 1, 5_000, mit_geld=False)[0] is False
    finally:
        restore()




def test_schulden_sperre_nennt_den_richtigen():
    """"Deine Kreditwuerdigkeit ist zu niedrig" bekam der FALSCHE zu lesen.

    Gesperrt ist immer der Schuldner - getippt hat 'Flo leih @kumpel 5k' aber
    der Verleiher. Der las, SEIN Score sei kaputt, und suchte den Fehler bei
    sich. Nach dem Umbau ist die Sperre die einzige verbliebene Wirkung des
    Scores; dann muss sie wenigstens den Richtigen benennen."""
    restore, sch = _schulden_setup({1: 100_000, 2: 100_000})
    try:
        sch.buch.score_daten()["2"] = [{"t": time.time(), "d": -40, "g": "test"}]
        gesperrt, warum = sch.score.gesperrt(2)
        assert gesperrt
        assert "deine" not in warum.lower(), (
            f"der Grund redet den Falschen an: {warum}")
        ok, fehler = sch._darf_anlegen(1, 2, 5_000)
        assert not ok
        assert "<@2>" in fehler, f"der Gesperrte wird nicht genannt: {fehler}"
        # Und der Weg zurueck muss dastehen - sonst ist es eine Sackgasse.
        assert "Tilgen" in fehler or "tilgen" in fehler, fehler
    finally:
        restore()




def test_leih_liest_den_betrag_und_nicht_die_frist():
    """'Flo leih @wer bis 3 wochen 10k' hat die **3** als Betrag gelesen.

    Die Antwort war dann "unter 50 lohnt die Buchfuehrung nicht" - eine
    Fehlermeldung, die mit der Eingabe nichts zu tun hat und niemanden auf den
    richtigen Weg bringt. _lies_grund schneidet die Frist laengst heraus,
    _lies_betrag tat es nicht."""
    restore, sch = _schulden_setup({1: 100_000, 2: 0})
    try:
        for text, erwartet in (
                ("<@2> bis 3 wochen 10k", 10_000),
                ("<@2> bis 2 monaten 5000", 5_000),
                ("<@2> bis in 7 tagen 2,5k", 2_500),
                ("<@2> bis freitag 1k", 1_000),
                ("<@2> 10k", 10_000),
                ("<@2> 10k bis 3 wochen", 10_000)):
            assert sch._lies_betrag(None, text) == erwartet, (
                f"{text!r} -> {sch._lies_betrag(None, text)}, erwartet {erwartet}")
        # Die Frist selbst muss weiter erkannt werden.
        assert sch._lies_frist("<@2> bis 3 wochen 10k") > time.time()
    finally:
        restore()




def test_erlassen_streicht_nicht_versehentlich_alles():
    """'schulden erlassen @x 500 fuers ballern' hat ALLES gestrichen.

    Geprueft wurde mit 'all' als TEILSTRING - und 'all' steckt in 'ballern',
    'halle', 'Fussball'. Aus 500 wurde die komplette Forderung, ohne Rueckfrage
    und ohne Weg zurueck. Das ist der teuerste Tippfehler im ganzen Modul."""
    restore, sch = _schulden_setup({1: 100_000, 2: 100_000})
    try:
        _schuld(sch, 1, 2, 20_000)

        async def erlassen(text):
            msg = _fake_msg(1, text)
            msg.mentions = [SimpleNamespace(id=2, bot=False, display_name="Kumpel")]
            return await sch._erlassen(msg)

        # 'ballern' enthaelt 'all' - es duerfen trotzdem nur 500 weg sein.
        asyncio.run(erlassen("schulden erlassen <@2> 500 fuers ballern"))
        assert sch.buch.saldo(1, 2) == 19_500, sch.buch.saldo(1, 2)
        # Steht eine Zahl da, gewinnt die Zahl - auch neben dem Wort 'ganz'.
        asyncio.run(erlassen("schulden erlassen <@2> 500 ganz sicher"))
        assert sch.buch.saldo(1, 2) == 19_000, sch.buch.saldo(1, 2)
        # Ohne Zahl und ohne 'alles' wird nachgefragt statt alles zu streichen.
        antwort = asyncio.run(erlassen("schulden erlassen <@2>"))
        assert sch.buch.saldo(1, 2) == 19_000, "hat kommentarlos alles gestrichen"
        assert "Wie viel" in str(antwort), antwort
        # Ausdrueckliches 'alles' raeumt weiter komplett ab.
        asyncio.run(erlassen("schulden erlassen <@2> alles"))
        assert sch.buch.saldo(1, 2) == 0, sch.buch.saldo(1, 2)
    finally:
        restore()




def test_schulden_darf_mehr_sein_als_man_besitzt():
    """Man darf ausdruecklich MEHR schulden, als man hat.

    Frueher war die Gesamtschuld auf das Dreifache des eigenen Vermoegens
    gedeckelt. Das hat niemand erraten ("verstehe das vermoegen zeug nicht"),
    und es hat den Fall verboten, um den es beim Leihen ueberhaupt geht: wer
    pleite ist, braucht das Geld - wer reich ist, nicht.

    Die einzige Grenze ist jetzt, was der VERLEIHER wirklich hat."""
    # Der Schuldner ist absichtlich arm - genau daran waere es vorher
    # gescheitert.
    restore, sch = _schulden_setup({1: 100_000_000, 2: 1_000})
    try:
        _schuld(sch, 1, 2, 2_900_000)
        # Das 2900-fache seines Geldes steht schon offen - trotzdem geht mehr.
        assert sch._darf_anlegen(1, 2, 200_000)[0] is True, (
            "der alte Vermoegens-Deckel lebt noch")
        # Auch das ganze Geld des Verleihers auf einmal.
        assert sch._darf_anlegen(1, 2, 100_000_000)[0] is True
        # Nur mehr als der Verleiher hat, geht nicht.
        ok, fehler = sch._darf_anlegen(1, 2, 100_000_001)
        assert not ok and "Konto" in fehler, fehler
    finally:
        restore()




def test_schulden_verfall_und_mahnstufen():
    """Ein Posten ohne jede Bewegung verfaellt - kein ewiges Druckmittel. Und
    gemahnt wird in Stufen, nicht jeden Tag gleich."""
    import schulden
    restore, sch = _schulden_setup({1: 0, 2: 0})
    try:
        jetzt = time.time()
        # Kurz vor dem Verfall -> Warnung an den Glaeubiger.
        p = _schuld(sch, 1, 2, 5_000, entstanden=jetzt - 55 * 86400)
        p.log = [{"t": jetzt - 55 * 86400, "art": "entstanden", "betrag": 5_000}]
        verfallen, warnen = sch.verfall_pruefen()
        assert not verfallen and len(warnen) == 1 and warnen[0][1] <= 7

        # Drueber -> verfallen, und der Score des Schuldners leidet.
        p.log = [{"t": jetzt - 61 * 86400, "art": "entstanden", "betrag": 5_000}]
        p.entstanden = jetzt - 61 * 86400
        verfallen, _w = sch.verfall_pruefen()
        assert len(verfallen) == 1 and p.status == "verfallen"
        assert sch.saldo(1, 2) == 0
        assert sch.score.score(2) == 35

        # Mahnstufen: 1 = faellig, 2 = eine Woche, 3 = zwei Wochen.
        for tage, erwartet in ((1, 1), (schulden.MAHN_STUFE_2 + 1, 2),
                               (schulden.MAHN_STUFE_3 + 1, 3)):
            q = _schuld(sch, 1, 2, 1_000, faellig=jetzt - tage * 86400)
            stufen = [s for _u, s, post in sch.faellige_stufen() if post is q]
            assert stufen == [erwartet], (tage, stufen)
            q.status = "erlassen"
            q.offen = 0
    finally:
        restore()




def test_schulden_insolvenz():
    """Privatinsolvenz: die Haelfte des Vermoegens anteilig raus, der Rest
    erlassen, Score unten und 14 Tage Sperre."""
    import schulden
    restore, sch = _schulden_setup({1: 10_000, 2: 0, 3: 0})
    try:
        _schuld(sch, 2, 1, 30_000)
        _schuld(sch, 3, 1, 10_000)
        text = asyncio.run(sch.insolvenz_durchfuehren(1))
        # 50 % von 10.000 = 5.000, anteilig 75/25.
        assert economy.get_coins(1) == 5_000, economy.get_coins(1)
        assert economy.get_coins(2) == 3_750 and economy.get_coins(3) == 1_250
        assert sch.summen(1)[1] == 0
        assert all(p.status in ("getilgt", "erlassen") for p in sch.buch.alle())
        assert sch.score.score(1) == schulden.Kreditwuerdigkeit.INSOLVENZ_SCORE
        # Gesperrt ist gesperrt - hier greift schon der Score (10 < 20), die
        # 14-Tage-Frist steht zusaetzlich im Buch.
        gesperrt, warum = sch.score.gesperrt(1)
        assert gesperrt and warum
        bis = sch.buch.stats(1)["insolvenz_bis"]
        assert bis > time.time() + 13 * 86400
        assert "erlassen" in text
    finally:
        restore()




def test_schulden_migration_alter_tafel():
    """Der alte Netto-Saldo je Paar wird zu je EINEM Posten - und nichts geht
    verloren: die alten Zahlen wandern ins Archiv."""
    restore, sch = _schulden_setup()
    try:
        store = _FakeStore({
            "pairs": {"1:2": {"net": 5_000, "vol": 9_000, "n": 4, "first": 1_000_000},
                      "3:4": {"net": -2_000, "vol": 2_000, "n": 1},
                      "5:6": {"net": 0, "vol": 500, "n": 2}},
            "posten": [], "next_id": 1, "score": {}, "stats": {}, "archiv": {},
        })
        sch.buch.laden(store)
        # net > 0 heisst "der ZWEITE schuldet dem ERSTEN".
        assert sch.buch.saldo(1, 2) == 5_000
        assert sch.buch.saldo(4, 3) == 2_000
        # Ein ausgeglichenes Paar erzeugt keinen Posten.
        assert len(sch.buch.alle()) == 2
        assert all(p.grund.startswith("Übernahme") for p in sch.buch.alle())
        # Nichts geloescht: die alte Tafel liegt im Archiv.
        assert store.data["archiv"]["pairs"]["1:2"]["vol"] == 9_000
        assert store.data["pairs"] == {}
        # Zweites Laden legt NICHT nochmal an.
        sch.buch.laden(store)
        assert len(sch.buch.alle()) == 0     # posten stand noch nicht im Store
    finally:
        restore()




def test_schulden_mahnung():
    """Mahnung per DM: nur ab einer Mindestsumme und hoechstens einmal je Abstand."""
    import schulden
    restore, sch = _schulden_setup()
    try:
        _schuld(sch, 1, 2, 50_000)        # 2 schuldet 1 -> 2 wird gemahnt
        _schuld(sch, 1, 3, 10)            # zu klein -> keine Mahnung
        gesendet = []

        class _User:
            def __init__(self, uid):
                self.id = uid

            async def send(self, **kw):
                gesendet.append((self.id, kw.get("embed")))

        client = SimpleNamespace(get_user=lambda uid: _User(uid), guilds=[])
        n = asyncio.run(sch.mahn_tick(client))
        assert n == 1 and gesendet[0][0] == 2
        emb = gesendet[0][1]
        text = (emb.title or "") + (emb.description or "") + "".join(
            str(f.name) + str(f.value) for f in emb.fields)
        assert "50.000" in text and "%" in text
        assert emb.color.value == schulden.FARBE_SCHULDEN
        # Direkt nochmal -> keine zweite DM (Abstand).
        assert asyncio.run(sch.mahn_tick(client)) == 0
        # Nach Ablauf des Abstands wieder.
        sch.buch.stats(2)["mahnung"] = time.time() - schulden.MAHN_ABSTAND - 5
        assert asyncio.run(sch.mahn_tick(client)) == 1
    finally:
        restore()




def test_kein_test_loest_ein_echtes_git_aus():
    """Ein Testlauf darf das Repo NICHT anfassen.

    Gefunden im Audit und am Protokoll belegt: ein Test schickte
    POST /api/update, und dahinter laeuft ein echtes 'git pull --ff-only' im
    Bot-Verzeichnis ("Panel-Update: git pull ok (schon aktuell)" stand im
    Testprotokoll). Auf dem Server haette ein Testlauf damit Code gezogen -
    waehrend die README ausdruecklich verspricht, dass er ungefaehrlich ist.

    Deshalb hier ein Riegel: wer /api/update im Test anspricht, muss vorher
    _update_lauf ersetzen."""
    import ast
    baum = ast.parse(open(__file__, encoding="utf-8").read())
    schuldige = []
    for knoten in ast.walk(baum):
        if not isinstance(knoten, ast.FunctionDef) or not knoten.name.startswith("test_"):
            continue
        quelle = ast.unparse(knoten)
        if "/api/update" not in quelle:
            continue
        if "_update_lauf" not in quelle:
            schuldige.append(knoten.name)
    assert not schuldige, (
        f"Diese Tests loesen ein echtes git pull aus: {schuldige}. "
        f"Vorher wp._update_lauf durch ein Double ersetzen.")




def test_einsatz_rueckgaben_werden_nicht_getilgt():
    """20 % jeder Einnahme gehen an den groessten Glaeubiger - eine ZURUECK-
    GEZAHLTE Wette ist aber keine Einnahme. Alle Rueckgaben in games/casino
    buchten ohne reason und wurden deshalb angeknabbert: der Bot meldete
    'Einsaetze zurueck', angekommen sind 80 %."""
    import re
    import economy
    import schulden
    restore = _with_economy({1: 10_000, 2: 0})
    alt = (schulden.instance._enabled, schulden.instance._store,
           schulden.instance.buch._store, schulden.instance.buch._posten)
    schulden.instance._enabled = True
    schulden.instance._store = _FakeStore(
        {"posten": [], "next_id": 1, "score": {}, "stats": {}, "archiv": {},
         "pairs": {}})
    schulden.instance.buch.laden(schulden.instance._store)
    try:
        schulden.instance.buch.anlegen(2, 1, 5_000)   # 1 schuldet 2 nun 5.000
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="spiele-rueck")
        assert economy.get_coins(1) - vor == 1_000, "Rueckgabe wurde getilgt"
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="spiele")
        assert economy.get_coins(1) - vor == 800, "echter Gewinn wurde NICHT getilgt"
    finally:
        (schulden.instance._enabled, schulden.instance._store,
         schulden.instance.buch._store, schulden.instance.buch._posten) = alt
        restore()

    # Und: keine Rueckgabe-Stelle darf den reason vergessen.
    for datei in ("games.py", "casino.py"):
        quelle = open(datei, encoding="utf-8").read()
        for zeile in quelle.splitlines():
            if "add_coins(" not in zeile or "reason=" in zeile:
                continue
            # Negative Betraege sind Abbuchungen - die loesen keine Tilgung aus.
            if re.search(r"add_coins\([^,]+,\s*-", zeile):
                continue
            # Rueckgaben erkennt man am Kommentar bzw. am Wort 'zurueck'.
            assert "zurueck" not in zeile.lower(), f"{datei}: {zeile.strip()}"




def test_kein_modul_zielt_wieder_auf_die_rohe_mention_liste():
    """Der Riegel zur Fehlerklasse oben: wer ein ZIEL bestimmt, muss
    basis.echte_erwaehnungen nehmen. message.mentions roh ist nur dort in
    Ordnung, wo es NICHT ums Zielen geht (z. B. 'ist Flo erwaehnt?').

    Geprueft wird der SYNTAXBAUM, nicht der Text - sonst schlagen Docstrings an,
    die message.mentions bloss erwaehnen (das ist mir hier prompt passiert)."""
    import ast
    import glob
    # Die Ausnahmen haengen am INHALT der Zeile, nicht an ihrer Nummer.
    # Vorher stand hier ("bot.py", 1637) - und jede Aenderung WEITER OBEN in
    # bot.py liess diesen Test scheitern, obwohl an der Sache nichts falsch
    # war. Ein Riegel, der bei fremden Aenderungen anschlaegt, wird
    # frueher oder spaeter weggeworfen statt gelesen.
    erlaubt = {
        # nur: ist Flo ueberhaupt angesprochen?
        ("bot.py", "if not angesprochen and self.user in message.mentions:"),
        # nur: ist ueberhaupt jemand genannt?
        ("economy.py", "if not message.mentions:"),
        # nur eine Nachschlagetabelle - das Ziel waehlt _pay danach aus dem Text
        ("economy.py", "by_id = {u.id: u for u in message.mentions}"),
        # Rueckfall, nachdem der Text nichts hergab
        ("economy.py", "ziel = ziel or message.mentions[0]"),
        # nur: ist ueberhaupt jemand genannt?
        ("fun.py", 'if (first in ("rate", "bewerte") and not message.mentions'),
        # Rueckfall hinter erstes_ziel(...)
        ("profil.py", "return erstes_ziel(message) or message.mentions[0], \"\""),
    }
    schuldige = []
    gesehen = set()
    for datei in sorted(glob.glob("*.py")):
        if datei.startswith("test_") or datei == "basis.py":
            continue
        quelle = open(datei, encoding="utf-8").read()
        for knoten in ast.walk(ast.parse(quelle)):
            if not (isinstance(knoten, ast.Attribute) and knoten.attr == "mentions"):
                continue
            basis_knoten = knoten.value
            if not (isinstance(basis_knoten, ast.Name) and basis_knoten.id == "message"):
                continue
            zeile = quelle.splitlines()[knoten.lineno - 1].strip()
            if (datei, zeile) in erlaubt:
                gesehen.add((datei, zeile))
                continue
            schuldige.append(f"{datei}:{knoten.lineno}  {zeile[:70]}")
    assert not schuldige, (
        "Diese Stellen zielen auf die rohe Mention-Liste - bei einer "
        "Antwort-mit-Ping treffen sie den Falschen:\n  " + "\n  ".join(schuldige))
    # Und die Ausnahmeliste darf nicht vergammeln: verschwindet eine Zeile,
    # muss sie hier raus - sonst deckt sie irgendwann versehentlich eine
    # voellig andere Stelle mit demselben Wortlaut.
    tot = sorted(erlaubt - gesehen)
    assert not tot, f"Ausnahmen zeigen ins Leere, bitte entfernen: {tot}"




def test_bot_erwaehnung_ist_kein_ziel():
    """'@Flo schulden' ist die eigene Tafel, nicht die Tafel MIT Flo.

    Wer Flo per @Mention anspricht, hat den Bot in message.mentions stehen.
    Ohne Filter zeigte '@Flo schulden' die Paar-Tafel mit Flo als Gegenueber,
    und '@Flo schulden erlassen @Kumpel' richtete sich gegen den Bot."""
    import schulden

    flo = _fake_person(uid=42, name="flo", bot=True)
    ich = _fake_person(uid=7, name="ich")
    kumpel = _fake_person(uid=8, name="kumpel")

    def _msg(text, mentions):
        return SimpleNamespace(
            content=text, mentions=list(mentions), author=ich,
            guild=SimpleNamespace(id=1, me=flo, get_member=lambda _u: None))

    restore, sch = _schulden_setup()
    try:
        # Nur Flo erwaehnt -> eigenes Buch.
        emb = asyncio.run(schulden.instance.handle(_msg("<@42> schulden", [flo])))
        assert "Dein Schuldbuch" in _embed_text(emb), _embed_text(emb)
        # Flo UND ein Mensch -> der Mensch ist gemeint.
        _schuld(sch, 8, 7, 5_000)
        emb = asyncio.run(schulden.instance.handle(
            _msg("<@42> schulden <@8>", [flo, kumpel])))
        assert "<@8>" in _embed_text(emb) and "<@42>" not in _embed_text(emb), _embed_text(emb)
        # Erlassen richtet sich nie gegen den Bot.
        antwort = asyncio.run(schulden.instance.handle(
            _msg("<@42> schulden erlassen", [flo])))
        assert "Wem willst du was erlassen" in _embed_text(antwort)
    finally:
        restore()




def test_doppelklick_verleiht_nicht_zweimal():
    """discord.py serialisiert Klick-Callbacks NICHT. Zwei Klicks auf
    »Annehmen« in ~200 ms liefen beide in _ja - und weil dort vor dem ersten
    await weder die Knoepfe deaktiviert noch die View gestoppt wurde, verlieh
    Flo ZWEIMAL: der Verleiher zahlte 10.000 statt der angebotenen 5.000, der
    Schuldner bekam zwei Posten.

    Das Fenster ist echt: das erste await steht erst in annehmen()."""
    import schulden
    schulden.setup()

    aufrufe = []

    class FalschesModul:
        async def annehmen(self, *_a, **_k):
            aufrufe.append(1)
            await asyncio.sleep(0)          # genau hier gibt der Task ab
            return True, "abgemacht"

    class FalscheAntwort:
        def __init__(self):
            self.deferred = 0

        async def edit_message(self, **_k):
            return None

        async def defer(self):
            self.deferred += 1

    class FalscheInteraktion:
        def __init__(self):
            self.response = FalscheAntwort()
            self.user = SimpleNamespace(id=2)

    ziel = SimpleNamespace(id=2, display_name="Bert")
    besteller = SimpleNamespace(id=1, display_name="Anna")
    view = schulden._AnfrageView(FalschesModul(), besteller, ziel, 5000,
                                 grund="Test", faellig=0.0, mit_geld=True)

    # discord.ui verpackt die Methode in ein Item - die echte Funktion liegt
    # darunter. So wird genau das aufgerufen, was auch ein Klick ausloest.
    ja = schulden._AnfrageView._ja
    ja = getattr(ja, "callback", ja)
    ja = getattr(ja, "callback", ja)

    async def zwei_klicks():
        a, b = FalscheInteraktion(), FalscheInteraktion()
        # Genau gleichzeitig - so wie ein Doppelklick ankommt.
        await asyncio.gather(ja(view, a, None), ja(view, b, None))
        return a, b

    a, b = asyncio.run(zwei_klicks())

    assert len(aufrufe) == 1, (
        f"annehmen() lief {len(aufrufe)}x - der Verleiher zahlt doppelt")
    assert a.response.deferred + b.response.deferred == 1, (
        "der zweite Klick wurde nicht sauber abgewiesen")
    # Und die Knoepfe muessen aus sein, bevor irgendetwas gebucht wird.
    quelle = inspect.getsource(schulden._AnfrageView._ja)
    assert quelle.index("self._laeuft = True") < quelle.index("await self._modul"), (
        "der Riegel steht nicht VOR dem ersten await - dann greift er nicht")




def test_casino_rueckgaben_werden_nicht_besteuert():
    """Wer Schulden hat, gibt von JEDER Einnahme 20 % an seine Glaeubiger ab.
    Eine Rueckgabe ist aber keine Einnahme: Baccarat-Push, Timeout auf Stufe 0,
    "Anzeige fehlgeschlagen - Einsatz zurueck". Ohne das Flag bekam der
    Schuldner von 1.000 zurueckgegebenen Coins nur 800 - obwohl er weder
    gewonnen noch verloren hatte.

    Das Flag rueckgabe=True gibt es seit langem und ist in casino.py:155
    ausdruecklich dafuer dokumentiert - es wurde an sieben Stellen nur nicht
    benutzt."""
    import schulden
    economy.setup()
    schulden.setup()
    casino.setup()
    schuldner, glaeubiger = 660011, 660022
    economy.add_coins(schuldner, 50_000)
    economy.add_coins(glaeubiger, 1_000)
    schulden.instance.buch.anlegen(glaeubiger, schuldner, 20_000, grund="Test")

    vorher = economy.get_coins(schuldner)
    casino._auszahlen(schuldner, 1_000, "test", rueckgabe=True)
    mit_flag = economy.get_coins(schuldner) - vorher
    vorher = economy.get_coins(schuldner)
    casino._auszahlen(schuldner, 1_000, "test")
    ohne_flag = economy.get_coins(schuldner) - vorher

    assert mit_flag == 1_000, f"Rueckgabe kam nicht voll an: {mit_flag}"
    assert ohne_flag < 1_000, (
        "Die Tilgung greift gar nicht mehr - dann ist dieser Test wertlos "
        "geworden und muss angepasst werden, nicht das Flag entfernt.")

    # --- Die Regel, damit es nicht wieder passiert -------------------------
    # In casino.py ist die Rueckgabe des Einsatzes IMMER daran zu erkennen, dass
    # das Ergebnis wieder nach 'bet' geht: "bet = _auszahlen(uid, bet)". Genau
    # diese Form muss das Flag tragen.
    quelle = open("casino.py", encoding="utf-8").read()
    fehlt = []
    for treffer in re.finditer(r"^\s*bet = _auszahlen\(([^\n]*)\)", quelle, re.M):
        if "rueckgabe" not in treffer.group(1):
            zeile = quelle[:treffer.start()].count("\n") + 1
            fehlt.append(f"casino.py:{zeile}  {treffer.group(0).strip()}")
    assert not fehlt, ("Einsatz-Rueckgabe ohne rueckgabe=True - der Schuldner "
                       "verliert davon 20 %:\n  " + "\n  ".join(fehlt))


if __name__ == "__main__":
    run(globals())
