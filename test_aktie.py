"""Die FloCorp-Aktie: Kurs, Depot, Chart.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur aktie      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _embed_text, _with_economy)



def test_floaktie_market():
    """Kauf hebt/Verkauf senkt den Kurs (arb-frei), Markt-Tick reagiert auf Voice-
    Aktivitaet, Dividende ist proportional (Groesster doppelt), Leaderboard sortiert."""
    import discord
    import floaktie
    restore_eco = _with_economy({1: 1_000_000, 2: 500_000})
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    alt_noise = floaktie.TICK_NOISE
    fa._enabled = True
    fa._store = _FakeStore({"price": 1000, "day": fa._today(), "act_ema": floaktie.ACT_BASELINE,
                            "msg_count": 0, "last_msg_count": 0,
                            "holdings": {}, "history": [{"day": "d", "price": 1000}]})
    try:
        p0 = fa.price()
        # Kauf: Kurs steigt, (Impact-)Kosten ab, Depot waechst.
        c0 = economy.get_coins(1)
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 50))
        # Bestaetigung ist ein Embed (vorher zwei Zeilen Fliesstext).
        assert _embed_text(r).count("Gekauft") == 1, _embed_text(r)
        assert fa.price() > p0 and fa.shares_of(1) == 50
        assert c0 - economy.get_coins(1) >= 50 * p0        # Impact -> mind. 50*Startkurs

        # Verkauf: Kurs faellt, Coins zurueck, Depot schrumpft.
        p1 = fa.price()
        r = asyncio.run(fa.sell(SimpleNamespace(id=1), 20))
        assert "Verkauft" in _embed_text(r)
        assert fa.price() < p1 and fa.shares_of(1) == 30

        # Kein Gratis-Arbitrage: sofortiger Round-Trip macht Verlust.
        economy.instance._profile(3)["coins"] = 1_000_000
        start = economy.get_coins(3)
        asyncio.run(fa.buy(SimpleNamespace(id=3), 100))
        asyncio.run(fa.sell(SimpleNamespace(id=3), 100))
        assert economy.get_coins(3) < start and fa.shares_of(3) == 0

        # KEIN KREDIT MEHR: ein Konto mit 10 Coins kauft nichts und bleibt bei 10.
        economy.instance._profile(4)["coins"] = 10
        r = asyncio.run(fa.buy(SimpleNamespace(id=4), 100))
        assert isinstance(r, str) and fa.shares_of(4) == 0, r
        assert economy.get_coins(4) == 10 and "Guthaben nicht" in r
        # Wer mehr will, als das Guthaben hergibt, bekommt so viel wie moeglich -
        # und landet NIE im Minus.
        economy.instance._profile(5)["coins"] = 60_000
        r = asyncio.run(fa.buy(SimpleNamespace(id=5), 100))
        assert fa.shares_of(5) > 0, r
        assert 0 <= economy.get_coins(5) < 60_000, economy.get_coins(5)
        assert "MINUS" not in _embed_text(r)

        # Aktivitaets-Takt: viel los -> Kurs STEIGT, wenig -> faellt (Rauschen aus).
        floaktie.TICK_NOISE = 0.0
        fa._store.data.update({"price": 1000, "act_ema": floaktie.ACT_BASELINE})
        a, n, drift, act = fa._activity_tick(15, 0)          # 15 im Call
        assert n > a and drift > 0
        # Fallen kann er nur ueber dem Wert eines toten Servers (FAIR_BASE) -
        # deshalb den BASISkurs klar darueber setzen, nicht nur den Anzeigewert.
        fa._store.data.update({"base": floaktie.FAIR_BASE * 10,
                               "act_ema": floaktie.ACT_BASELINE, "leer_min": 0.0})
        fa._sync_price()          # Anzeigekurs auf die Kurve ziehen (sonst stale)
        a, n, drift, act = fa._activity_tick(0, 0)           # niemand da
        assert drift < 0 and n <= a                          # Tendenz nach unten
        # Immer BASIS setzen und synchronisieren - sonst ist der gespeicherte
        # Anzeigekurs veraltet und 'vorher/nachher' vergleicht Aepfel mit Birnen.
        def stelle(kurs=1000):
            fa._store.data.update({"base": float(kurs),
                                   "act_ema": floaktie.ACT_BASELINE, "leer_min": 0.0})
            fa._sync_price()

        # NACHRICHTEN treiben den Kurs - aber nur BEI BESETZTEM CALL. Ohne
        # jemanden im Sprachkanal sind es 0 Punkte, egal wie viel getippt wird
        # (sonst setzt ein einziger Schreiber den Leerlauf-Verfall komplett aus).
        stelle()
        _a, _n, drift_leer, act_leer = fa._activity_tick(0, 200)   # reger Chat, leerer Call
        assert act_leer == 0.0 and drift_leer < 0, (act_leer, drift_leer)
        stelle()
        a, n, drift, act = fa._activity_tick(2, 200)         # zwei im Call + reger Chat
        assert n > a and drift > 0 and act > floaktie.ACT_BASELINE
        # ... und der Chat macht dabei einen messbaren Unterschied.
        stelle()
        _, _, _, act_ohne_chat = fa._activity_tick(2, 0)
        assert act > act_ohne_chat, (act, act_ohne_chat)
        # Live-Streamer zaehlen EXTRA: gleiche Personen, aber Streams -> mehr Aktivitaet.
        stelle()
        _, _, _, act_plain = fa._activity_tick(12, 0)
        stelle()
        _, _, _, act_stream = fa._activity_tick(12, 0, streams=6, video=3)
        assert act_stream > act_plain                        # Streamer/Kameras zaehlen mit
        # Ueber eine Stunde (60 Min-Takte) aktiver Call -> Kurs klar hoch.
        stelle()
        for _ in range(60):
            fa._activity_tick(12, 30, streams=6)
        assert fa.price() > 1030                             # klar gestiegen (Boersenwert mit)
        # note_message + sample_and_tick: Nachrichten werden GEZAEHLT, heben den
        # Kurs bei LEEREM Call aber nicht mehr (siehe activity_of).
        # ACHTUNG: 'base' setzen, nicht 'price' - 'price' ist nur der gespeicherte
        # Stand, gerechnet wird immer aus der Kurve. Frueher stand hier
        # {"price": 1000}; der Test war dadurch gruen, weil der Vergleich einen
        # veralteten Wert gegen einen frisch gerechneten hielt, nicht weil der
        # Chat irgendetwas bewegt haette.
        # Deutlich UEBER den Grundwert stellen, sonst faellt der Kurs zu Recht
        # gar nicht (siehe boden_base) und der Test misst nichts.
        stelle()
        fa._store.data["base"] = fa.boden_base() * 4.0
        fa._sync_price()
        vorher = fa.price()
        for _ in range(200):
            fa.note_message()
        assert fa._store.data["msg_count"] == 200
        guild0 = SimpleNamespace(voice_channels=[], afk_channel=None)
        asyncio.run(fa.sample_and_tick(guild0))
        assert fa.price() < vorher, (vorher, fa.price())

        # Dividende proportional; groesster Aktionaer doppelt; Leaderboard sortiert.
        fa._store.data["holdings"] = {"1": 100, "2": 300}
        assert fa.top_holder() == 2
        assert fa.dividend_for(1) == 100 // floaktie.DIVIDEND_DIVISOR
        assert fa.dividend_for(2) == (300 // floaktie.DIVIDEND_DIVISOR) * 2
        assert fa.leaderboard()[:2] == [(2, 300), (1, 100)]

        # Voice-Dividende landet bei aktiven Aktionaeren.
        m1 = SimpleNamespace(id=1, bot=False, voice=SimpleNamespace(self_deaf=False, deaf=False))
        m2 = SimpleNamespace(id=2, bot=False, voice=SimpleNamespace(self_deaf=False, deaf=False))
        guild = SimpleNamespace(voice_channels=[SimpleNamespace(id=10, members=[m1, m2])],
                                afk_channel=None)
        b1, b2 = economy.get_coins(1), economy.get_coins(2)
        asyncio.run(fa.pay_voice_dividends(guild))
        assert economy.get_coins(1) == b1 + fa.dividend_for(1)
        assert economy.get_coins(2) == b2 + fa.dividend_for(2)

        # handle-Routing: Nicht-Befehl -> None; kauf/top/depot -> Embed.
        # 'aktie' ist identisch zu 'floaktie' (nur EINE Aktie).
        def fmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="T"))
        assert asyncio.run(fa.handle(fmsg(1, "wie gehts"))) is None
        # Der Kurs ist durch die Takte oben weit gelaufen -> Konto auffuellen,
        # sonst scheitert der Kauf an der Kreditlinie (das ist hier nicht der Test).
        economy.instance._profile(1)["coins"] = 10_000_000_000
        assert "Gekauft" in _embed_text(asyncio.run(fa.handle(fmsg(1, "floaktie kauf 1"))))
        # 'aktie' == 'floaktie'
        economy.instance._profile(1)["coins"] = 10_000_000_000
        assert "Gekauft" in _embed_text(asyncio.run(fa.handle(fmsg(1, "aktie kauf 1"))))
        assert not isinstance(asyncio.run(fa.handle(fmsg(1, "aktie top"))), (str, type(None)))
        assert not isinstance(asyncio.run(fa.handle(fmsg(1, "floaktie depot"))), (str, type(None)))

        # Kurs-Chart: Serie hat >=2 Punkte, _chart_file rendert ein PNG.
        assert len(fa._series(7)) >= 2
        f = asyncio.run(fa._chart_file(7, "7 Tage"))   # rendert im Thread
        assert isinstance(f, discord.File)

        # aktienkurs / aktie chart -> Chart senden (HANDLED) via Fake-reply.
        class FakeMsg:
            def __init__(s, uid, content):
                s.author = SimpleNamespace(id=uid, display_name="T")
                s.content = content
                s.guild = SimpleNamespace(id=1)

            async def reply(s, **kw):
                return SimpleNamespace(id=1, channel=SimpleNamespace(id=1))
        assert asyncio.run(fa.handle(FakeMsg(1, "aktienkurs"))) is floaktie.HANDLED
        assert asyncio.run(fa.handle(FakeMsg(1, "aktie chart"))) is floaktie.HANDLED
        assert asyncio.run(fa.handle(FakeMsg(1, "aktie"))) is floaktie.HANDLED  # Panel

        # Live-Update: das zuletzt gepostete 'flo aktie'-Panel UND der zuletzt
        # gepostete 'flo aktienkurs'-Chart werden bei jeder Kursaenderung
        # (Trade & Aktivitaets-Takt) automatisch nachgezogen.
        edits, chart_edits = [], []

        class FakePanel:
            id = 1
            channel = SimpleNamespace(id=1)

            async def edit(self, **kw):
                edits.append(kw)

        class FakeChart:
            id = 2
            channel = SimpleNamespace(id=1)

            async def edit(self, **kw):
                chart_edits.append(kw)
        fa._panel_msg = FakePanel()
        fa._panel_uid = 1
        fa._chart_msg = FakeChart()
        fa._chart_days = 1
        asyncio.run(fa._refresh_last_panel())
        assert len(edits) == 1 and "embed" in edits[0]            # Panel aktualisiert
        asyncio.run(fa._refresh_last_chart())
        assert len(chart_edits) == 1 and "attachments" in chart_edits[0]   # Chart-Bild neu
        economy.instance._profile(1)["coins"] = 1_000_000
        asyncio.run(fa.buy(SimpleNamespace(id=1), 1))
        assert len(edits) >= 2 and len(chart_edits) >= 2         # Trade zieht beide nach
        # Aktivitaets-Takt mit Kursaenderung zieht Panel UND Chart nach.
        # Basiskurs bewusst tief setzen, damit der Takt garantiert etwas bewegt
        # (steht er schon am Deckel, ist die Drift 0 - und dann gibt es zu Recht
        # kein Update).
        fa._store.data["base"] = float(floaktie.FAIR_BASE)
        fa._store.data["act_ema"] = 0.0
        fa._sync_price()
        n0, c0 = len(edits), len(chart_edits)
        # Genug Nachrichten fuer einen sichtbaren Kurssprung (der Chat-Anteil ist
        # bei MSG_MAX_ACT gedeckelt, der Sprung muss den gerundeten Kurs bewegen).
        fa._store.data["base"] = float(floaktie.FAIR_BASE) * 50
        fa._sync_price()
        fa._store.data["msg_count"] = 999
        fa._store.data["last_msg_count"] = 0
        asyncio.run(fa.sample_and_tick(SimpleNamespace(voice_channels=[], afk_channel=None)))
        assert len(edits) > n0 and len(chart_edits) > c0
    finally:
        fa._store, fa._enabled = alt[0], alt[1]
        fa._panel_msg = None
        fa._chart_msg = None
        floaktie.TICK_NOISE = alt_noise
        restore_eco()




def test_aktie_sagt_wenn_dieser_server_gar_nicht_zaehlt():
    """Gemeldet mit Screenshot: acht Leute im Call, mehrere mit Kamera und
    Livestream - und das Panel sagt "Niemand im Call", Aktivitaet 0.0.

    Es war nicht die Zaehlung. Es gibt genau EINE Aktie, und ein neu
    dazugekommener Server bewegt sie bewusst erst, wenn man es dort einschaltet
    (guildcfg 'aktie_zaehlt'). Gemessen wurde also ein ANDERER Server - und dort
    sass tatsaechlich niemand. Fuer den Fragenden sah das wie ein kaputter Bot
    aus, denn er sah ja seinen vollen Call.

    Das Panel muss den Unterschied benennen: "niemand da" und "du zaehlst hier
    nicht mit" sind zwei voellig verschiedene Aussagen."""
    import floaktie
    import guildcfg
    a = floaktie.instance
    alt_store, alt_on = a._store, a._enabled
    a._store = _FakeStore({"base": 10.0, "act_ema": 0.0, "grund_akt": 0.0,
                           "shares": {}, "ticks": [], "hist": []})
    a._enabled = True
    a._zuletzt_mess = (0, 0, 0, 0)          # wie im Screenshot: nichts gemessen
    a._zuletzt_gezaehlt = []
    fremd = 4242424242
    alt_an = guildcfg.an
    try:
        # Der Schalter wird hier direkt gesteuert - geprueft wird das PANEL,
        # nicht guildcfg (das hat eigene Tests und einen eigenen Speicher).
        zaehlt = {"wert": False}
        alt_an = guildcfg.an
        guildcfg.an = lambda gid, key: (zaehlt["wert"] if key == "aktie_zaehlt"
                                        else alt_an(gid, key))

        # Ein Server, der NICHT zaehlt, bekommt die Wahrheit zu sehen.
        zeile = a._mess_zeile(fremd)
        assert "zählt nicht für die Aktie" in zeile, zeile
        assert "aktie_zaehlt an" in zeile, "der Weg zur Loesung fehlt"
        assert "Niemand im Call" not in zeile, (
            "sagt weiterhin 'Niemand im Call', obwohl der Server nur nicht zaehlt")

        # Ein Server, der zaehlt, bekommt die Messung - und da stimmt
        # 'Niemand im Call' ja auch.
        zaehlt["wert"] = True
        try:
            assert "Niemand im Call" in a._mess_zeile(fremd)
            # Und mit Leuten drin steht dort, wer den Kurs traegt.
            a._zuletzt_mess = (3, 1, 2, 5)
            a._zuletzt_gezaehlt = ["Anna", "Bert", "Cem"]
            voll = a._mess_zeile(fremd)
            assert "3 im Call" in voll and "Anna" in voll, voll
            assert "Livestream" in voll and "Kamera" in voll, voll
        finally:
            zaehlt["wert"] = False

        # Ohne Server-Bezug (DM) wird nichts behauptet.
        a._zuletzt_mess, a._zuletzt_gezaehlt = (0, 0, 0, 0), []
        assert "Niemand im Call" in a._mess_zeile(None)
    finally:
        guildcfg.an = alt_an
        a._store, a._enabled = alt_store, alt_on




def test_aktie_steigt_auch_auf_einem_lebendigen_server():
    """Gemeldet: "10 Leute im Call und sie steigt nur 0,05 %".

    Ursache war ein Vergleich zwischen einer skalierten und einer unskalierten
    Zahl: deckel_fuer() rechnete max(ziel * CEIL_FACTOR, boden). Der Boden
    waechst aber mit GRUND_FAKTOR (4), der Deckel nur mit CEIL_FACTOR (2) - ab
    einem 3-Tage-Schnitt von etwa der halben aktuellen Aktivitaet UEBERHOLT der
    Boden den Deckel. Dann ist deckel == boden, der Kurs steht genau darauf, und
    drift_fuer gibt 0 zurueck.

    Je lebendiger der Server WAR, desto weniger konnte die Aktie steigen."""
    import floaktie
    a = floaktie.instance
    alt_store, alt_on = a._store, a._enabled
    a._store = _FakeStore({"base": 10.0, "act_ema": 0.0, "grund_akt": 0.0,
                           "shares": {}, "ticks": [], "hist": []})
    a._enabled = True
    st = a._state()
    try:
        # Der Kurs steht auf dem Boden (so sieht es nach einer ruhigen Nacht
        # aus), dann kommen zehn Leute in den Call.
        for grund in (0.0, 2.0, 5.0, 6.0, 8.0, 10.0, 20.0):
            st["grund_akt"], st["act_ema"] = grund, 10.0
            st["base"] = a.boden_base()
            deckel, boden = a.deckel_fuer(10.0), a.boden_base()
            assert deckel > boden, (
                f"3-Tage-Schnitt {grund}: Boden {boden:.0f} hat den Deckel "
                f"{deckel:.0f} eingeholt - der Kurs kann nicht mehr steigen")
            drift = a.drift_fuer(10.0)
            assert drift > 0.005, (
                f"3-Tage-Schnitt {grund}: nur {drift * 100:.3f} %/min bei zehn "
                f"Leuten im Call")

        # Gegenprobe: ohne Aktivitaet darf sie NICHT steigen.
        st["grund_akt"], st["act_ema"] = 10.0, 0.0
        st["base"] = a.boden_base()
        assert a.drift_fuer(0.0) <= 0.0, "steigt, obwohl niemand da ist"

        # Und ein einzelner Zuhoerer bewegt sie deutlich weniger als zehn.
        st["act_ema"] = 1.0
        einer = a.drift_fuer(1.0)
        st["act_ema"] = 10.0
        zehn = a.drift_fuer(10.0)
        assert 0 < einer < zehn / 5, (einer, zehn)
    finally:
        a._store, a._enabled = alt_store, alt_on




def test_floaktie_aktivitaet_treibt_den_kurs():
    """DIE Regel, in Zahlen: jeder im Call, jeder Livestream und jede Chat-Nachricht
    heben den Kurs - je mehr, desto schneller. Ist gar nichts los, sinkt er langsam.
    REGRESSION: vorher wurde von jeder Minute ein 'Normalwert' von 3,0 abgezogen,
    wodurch ein Tag mit 5 aktiven Stunden bei -19 % landete, und das Rauschen war
    bei 4 Leuten im Call 12x groesser als das Signal."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0

    def frisch(preis=1000):
        fa._store = _FakeStore({"price": preis, "base": float(preis), "day": "x",
                               "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                               "holdings": {}, "history": [], "ticks": []})
        fa._sync_price()

    try:
        # 1) Mehr Leute -> hoeherer Zielkurs UND schnellerer Anstieg.
        frisch()
        tempo = []
        for leute in (0, 1, 5, 12, 20):
            frisch()
            akt = fa.activity_of(leute, 0, 0, 0)
            tempo.append((leute, fa.ziel_base(akt), fa.drift_fuer(akt)))
        ziele = [z for _l, z, _d in tempo]
        assert ziele == sorted(ziele) and len(set(ziele)) == len(ziele)
        # Ab dem Punkt, wo das Ziel ueber dem Kurs liegt, steigt es mit den Leuten.
        steigend = [d for _l, z, d in tempo if z > 1000]
        assert steigend == sorted(steigend), steigend

        # 2) Livestreams zaehlen EXTRA: 10 Leute die alle streamen schlagen
        #    10 Leute ohne Stream deutlich.
        ohne = fa.activity_of(10, 0, 0, 0)
        mit = fa.activity_of(10, 10, 0, 0)
        assert mit > ohne * 2, (ohne, mit)
        assert fa.ziel_base(mit) > fa.ziel_base(ohne)

        # 3) Reiner Chat treibt den Kurs NICHT MEHR (bewusst geaendert).
        #    Vorher hob eine einzige Nachricht die Aktivitaet ueber 0 und setzte
        #    den Leerlauf-Verfall komplett aus - auf einem Server, auf dem
        #    tagsueber immer mal wer tippt, ist der Kurs deshalb praktisch nie
        #    gefallen. Jetzt gilt: kein Call, keine Punkte.
        frisch()
        _a, _n, drift, akt = fa._activity_tick(0, 40)
        assert akt == 0.0, akt
        assert drift < 0, drift
        # ... im Call zaehlt derselbe Chat aber voll mit.
        assert fa.activity_of(2, 0, 0, 40) > fa.activity_of(2, 0, 0, 0)

        # 4) 10 im Call, ALLE streamen: in der ERSTEN Minute sichtbar (>= 1 %),
        #    nach einer Stunde vielfach.
        frisch()
        a, n, drift, _akt = fa._activity_tick(10, 20, streams=10)
        assert drift >= 0.01, drift
        assert n > a
        for _ in range(59):
            fa._activity_tick(10, 20, streams=10)
        assert fa.price() > 2500, fa.price()          # nach 1 h fast verdreifacht

        # 4b) REGRESSION (EMA-Rest): steht nach viel Aktivitaet noch ein winziger
        #     EMA-Rest im Zustand, DARF der Kurs im Leerlauf trotzdem nicht
        #     "steigen". Vorher gab drift_fuer(0.03) den Mindest-Anstieg (+0,48%/h)
        #     zurueck, obwohl niemand mehr da war - genau der gemeldete Bug.
        fa._store = _FakeStore({"price": 33_586, "base": 33_586.0, "day": "x",
                               "act_ema": 0.03, "msg_count": 0, "last_msg_count": 0,
                               "holdings": {}, "history": [], "ticks": [],
                               "leer_min": 0.0})
        fa._sync_price()
        _a, _n, d, akt = fa._activity_tick(0, 0)            # niemand da
        assert d < 0, (d, akt)                              # muss FALLEN
        assert fa._state()["act_ema"] == 0.0               # EMA-Rest ist weg

        # 5) Leerer Call: faellt SOFORT und GLEICHMAESSIG. Ausdrueckliche
        #    Vorgabe: "sinken soll es direkt sobald keiner mehr im Call ist,
        #    aber rasant, gute 10 % pro 30 min." Kein Anlauf mehr, keine
        #    Staffelung - die halbe Stunde ist das Mass.
        frisch(200_000)
        for _ in range(30):
            fa._activity_tick(0, 0)
        nach_30 = 1 - fa.price() / 200_000
        assert 0.10 <= nach_30 <= 0.14, nach_30
        for _ in range(30):
            fa._activity_tick(0, 0)
        nach_1h = 1 - fa.price() / 200_000
        assert 0.19 <= nach_1h <= 0.26, nach_1h
        for _ in range(60):
            fa._activity_tick(0, 0)
        nach_2h = 1 - fa.price() / 200_000
        assert 0.34 <= nach_2h <= 0.45, nach_2h
        # Gleichmaessig heisst: die zweite halbe Stunde kostet anteilig genauso
        # viel wie die erste (frueher war die erste bewusst schwaecher).
        frisch(200_000)
        for _ in range(30):
            fa._activity_tick(0, 0)
        h1 = fa.price()
        for _ in range(30):
            fa._activity_tick(0, 0)
        h2 = fa.price()
        assert abs((1 - h1 / 200_000) - (1 - h2 / h1)) < 0.02, (h1, h2)
        # Beim SINKEN gibt es kein Rauschen - jede Leerlauf-Minute geht wirklich
        # runter (vorher stieg der Kurs in ~30 % der Minuten durch Zufall).
        frisch(200_000)
        for _ in range(300):
            _a, n, d, _akt = fa._activity_tick(0, 0)
            assert d <= 0
        # ... aber nie unter den Wert eines toten Servers.
        for _ in range(30 * 1440):
            fa._activity_tick(0, 0)
        assert fa.price() >= floaktie.MIN_PRICE, fa.price()

        # 6) Kein Aufsummieren: Dauer-Vollbetrieb laeuft in ein NIVEAU (Deckel:
        #    CEIL_FACTOR x Zielkurs), statt zu explodieren (vorher: 10**16 nach
        #    30 Tagen). Der Deckel steigt nur, wenn die Aktivitaet steigt.
        frisch()
        # 14 Tage einschwingen lassen: der Grundwert ist ein 3-Tage-Mittel und
        # zieht den Deckel in den ersten Tagen noch mit hoch. Frueher wurde ab
        # Tag 4 gemessen - da war er noch mitten in der Bewegung.
        for _ in range(14 * 1440):
            fa._activity_tick(10, 40, streams=10)
        vier_tage = fa.price()
        for _ in range(16 * 1440):
            fa._activity_tick(10, 40, streams=10)
        # Der Deckel ist akt_deckel_base() - EINE Zahl fuer Drift, Panel und
        # Impuls. Bei DAUER-Vollbetrieb waechst der Grundwert bis auf die volle
        # Aktivitaet und liegt dann ueber CEIL_FACTOR x Zielkurs; frueher hing
        # der Kurs zwischen zwei widerspruechlichen Decken (Bremse 94.020,
        # Grundwert 188.001). Im echten Betrieb (Call nur abends) greift das nie,
        # dort bleibt der Grundwert weit unter der Bremse.
        deckel = fa.akt_deckel_base()
        # Toleranz = ein voller Takt: die Pruefung im Drift laeuft VOR dem Schritt,
        # der letzte Schritt darf also einmal um bis zu TICK_CAP ueberschiessen.
        assert fa.price() <= deckel * (1 + floaktie.TICK_CAP) * 1.01, (fa.price(), deckel)
        # ... und der Deckel selbst bleibt gedeckelt: keine Explosion.
        #
        # Die Grenze ist 10x die Zielbasis, nicht 5x. Grund: deckel_fuer() legt
        # CEIL_FACTOR (2) jetzt auf BEIDE Grundwerte statt nur auf einen - vorher
        # wurde ziel*2 gegen boden*1 verglichen, und dadurch holte der Boden
        # (waechst mit GRUND_FAKTOR 4) den Deckel ein. Stand er drauf, war der
        # Drift exakt 0: "10 Leute im Call und sie steigt nur 0,05 %".
        #
        # In DIESEM Dauervollbetrieb rund um die Uhr wird grund_akt zur vollen
        # Aktivitaet, also boden = ziel_base(4 x 47) und Deckel = 2 x boden = 8x.
        # Im echten Betrieb (Call nur abends) ist grund_akt etwa ein Viertel der
        # Spitze - genau dafuer ist GRUND_FAKTOR 4 gedacht -, dort liegt der
        # Deckel wieder bei 2x. Nachgemessen laeuft der Kurs auch hier NICHT weg,
        # sondern in eine Ebene: Tag 30 -> Tag 40 sind +0,33 %. Genau das prueft
        # die Zeile darunter, und sie ist die eigentliche Absicherung.
        assert deckel <= fa.ziel_base(fa.activity_of(10, 10, 0, 40)) * 10, deckel
        assert fa.price() < vier_tage * 1.05, (vier_tage, fa.price())

        # 7) Sofort-Impuls: Livestream geht an -> Kurs zieht augenblicklich an,
        #    aber pro Minute gedeckelt (kein Pump durch Rein-/Rausspringen).
        #    Der Impuls braucht Luft nach oben - bei Aktivitaet 0 ist der Deckel
        #    winzig und er tut (richtigerweise) gar nichts.
        frisch()
        fa._store.data["act_ema"] = 8.0
        vorher = fa.price()
        assert fa._puls(floaktie.PULSE_STREAM, "test") > vorher
        for _ in range(30):
            fa._puls(floaktie.PULSE_STREAM, "test")
        assert fa.price() <= vorher * (1 + floaktie.PULSE_MAX_PER_MIN) + 1, fa.price()

        # 7b) DER VORMITTAG: auch mit nur 3 Leuten im Call muss sich ein Depot
        #     ueber ein paar Stunden VERVIELFACHEN - vom normalen Niveau aus, also
        #     dort, wo der Kurs nach einer ruhigen Nacht steht.
        #     Die Schwelle folgt dem Deckel: CEIL_FACTOR x Zielkurs. Frueher stand
        #     der bei 6 (also 18-fach), was zusammen mit dem festen Boden eine
        #     182-fache Tagesspanne ergab - kein Markt, nur ein Saegezahn. Mit
        #     CEIL_FACTOR 2 sind es rund 6-fach in sechs Stunden mit DREI Leuten.
        for startkurs in (1_000, int(floaktie.FAIR_BASE)):
            frisch(startkurs)
            for _ in range(6 * 60):
                fa._activity_tick(3, 0)
            faktor = fa.price() / startkurs
            assert faktor > 5, (startkurs, fa.price(), faktor)
        # ... aber NICHT unbegrenzt: steht der Kurs schon am Deckel dieser
        # Aktivitaet, hoert es auf. Genau das verhindert die Hyperinflation
        # (vorher: 750 Anteile fuer 1,1 Mio -> Verkauf fuer 43 MILLIARDEN).
        akt3 = fa.activity_of(3, 0, 0, 0)
        frisch(int(fa.ziel_base(akt3) * floaktie.CEIL_FACTOR))
        vorm_deckel = fa.price()
        for _ in range(6 * 60):
            fa._activity_tick(3, 0)
        assert fa.price() <= vorm_deckel * 1.06, (vorm_deckel, fa.price())
        # Und mehr Leute muessen auf demselben Niveau schneller sein - gemessen
        # UNTERHALB des Deckels, sonst vergleicht man zwei Nullen.
        frisch(2_000)
        d3 = fa.drift_fuer(fa.activity_of(3, 0, 0, 0))
        d10 = fa.drift_fuer(fa.activity_of(10, 0, 0, 0))
        d10s = fa.drift_fuer(fa.activity_of(10, 10, 0, 0))
        assert d3 < d10 <= d10s, (d3, d10, d10s)
        assert d10s >= floaktie.TICK_CAP * 0.99, d10s      # voller Call: Anschlag

        # Umgekehrt: steht der Kurs WEIT ueber dem, was die Aktivitaet hergibt,
        # geht gar nichts mehr nach oben. Genau das haelt die Spanne im Zaum -
        # bei Kurs 50.000 rechtfertigen weder 3 noch 10 Leute einen Anstieg,
        # ein voller Call mit 10 Streams aber schon.
        frisch(50_000)
        assert fa.drift_fuer(fa.activity_of(3, 0, 0, 0)) == 0.0
        assert fa.drift_fuer(fa.activity_of(10, 0, 0, 0)) == 0.0
        assert fa.drift_fuer(fa.activity_of(10, 10, 0, 0)) > 0.0

        # 8) Signal deutlich ueber dem Rauschen - man MUSS es sehen koennen.
        #    (Beim Niveau-Modell zaehlt der Abstand zum Ziel: steht der Kurs auf
        #    Totlast-Niveau, treiben schon 4 Leute im Call ihn klar nach oben.)
        frisch(int(floaktie.FAIR_BASE))
        sig = fa.drift_fuer(fa.activity_of(4, 0, 0, 0))
        assert sig > floaktie.TICK_NOISE * 10, (sig, floaktie.TICK_NOISE)
        # Solange Leute da sind, geht es NIE runter - und es bleibt auch bei einem
        # hohen Kurs in Bewegung, nur gemaechlicher (logarithmische Daempfung).
        akt4 = fa.activity_of(4, 0, 0, 0)
        frisch(int(floaktie.FAIR_BASE))
        schnell = fa.drift_fuer(akt4)
        # Knapp UNTER dem Deckel dieser Aktivitaet - dort muss es noch aufwaerts
        # gehen, nur gemaechlicher.
        frisch(int(fa.ziel_base(akt4) * floaktie.CEIL_FACTOR * 0.9))
        langsam = fa.drift_fuer(akt4)
        assert schnell > langsam > 0, (schnell, langsam)
        # Erst die Notbremse ganz oben stoppt ihn (Deckel = Zielkurs x CEIL_FACTOR).
        frisch(int(fa.ziel_base(akt4) * floaktie.CEIL_FACTOR) + 10_000)
        assert fa.drift_fuer(akt4) == 0
        # Nur ohne jede Aktivitaet faellt er.
        assert fa.drift_fuer(0) < 0

        # 9) Messung ohne Member-Cache: voice_states als Notfall-Quelle. Wer sich
        #    NICHT aufloesen laesst, zaehlt NICHT - lieber eine Minute zu wenig
        #    Aktivitaet als ein Bot, der den Kurs dauerhaft oben haelt.
        #    (Details in test_floaktie_bots_zaehlen_nie.)
        class _VS:
            def __init__(self, stream=False, video=False):
                self.self_stream, self.self_video = stream, video

        class _VC:
            id = 5
            voice_states = {11: _VS(True), 12: _VS(), 13: _VS(video=True)}
            members = []                      # Cache leer!

        leute = {11: SimpleNamespace(id=11, bot=False),
                 12: SimpleNamespace(id=12, bot=False),
                 13: SimpleNamespace(id=13, bot=False)}
        guild = SimpleNamespace(voice_channels=[_VC()], afk_channel=None,
                               me=SimpleNamespace(id=99),
                               get_member=lambda uid: leute.get(uid))
        assert fa._measure(guild) == (3, 1, 1)
        # Gar kein Cache -> gar keine Aktivitaet (statt drei erfundenen Menschen).
        guild.get_member = lambda _uid: None
        assert fa._measure(guild) == (0, 0, 0)
        # Flo selbst und andere Bots zaehlen nicht mit.
        _VC.voice_states = {99: _VS(), 11: _VS()}
        guild.get_member = lambda uid: leute.get(uid)
        assert fa._measure(guild)[0] == 1
    finally:
        fa._store, fa._enabled, floaktie.TICK_NOISE = alt




def test_floaktie_chart_zeitraeume_und_bild():
    """REGRESSION (Chart): '7 Tage', '30 Tage' und 'Gesamt' zeigten dieselbe Reihe,
    sobald die Minuten-Ticks nur ein paar Tage zurueckreichten - die Tages-Historie
    wurde nur benutzt, wenn es GAR keine Ticks gab. Und das Bild bekam bei grossen
    Spannen eine negative Kurs-Achse."""
    import floaktie
    import render
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    jetzt = time.time()
    # 3 Tage Minuten-Ticks (erst flach, dann steil) + 24 Tage Schlusskurse.
    ticks, p = [], 1000.0
    for i in range(3 * 1440):
        p *= 1.0002 if i < 2 * 1440 else 1.003
        ticks.append({"t": jetzt - (3 * 1440 - i) * 60, "price": int(p)})
    # Die Schlusskurse muessen RELATIV ZU HEUTE liegen - die Reihe waehlt sie
    # ueber das Datum aus. Feste Kalendertage (frueher "2026-06-xx") fallen je
    # nach Testtag komplett aus dem Fenster.
    from datetime import datetime as _dt, timedelta as _td
    _heute = _dt.now(floaktie.TIMEZONE)
    fa._store = _FakeStore({
        "price": int(p), "base": float(p), "day": "x", "act_ema": 20.0,
        "msg_count": 0, "last_msg_count": 0, "holdings": {},
        "history": [{"day": (_heute - _td(days=24 - d)).strftime("%Y-%m-%d"),
                     "price": 800 + d * 30} for d in range(1, 25)],
        "ticks": ticks})
    try:
        fa._sync_price()
        reihen = {}
        for tage in (1, 7, 30):
            pts, chg = fa.series(tage)
            reihen[tage] = (pts, chg)
            assert len(pts) >= 2
            assert pts[-1] == fa.price()          # Linie endet auf dem echten Kurs
        # Die Zeitraeume muessen sich UNTERSCHEIDEN.
        assert reihen[1][0] != reihen[7][0], "1 Tag und 7 Tage identisch"
        assert reihen[7][0] != reihen[30][0], "7 Tage und 30 Tage identisch"
        # Je laenger der Zeitraum, desto weiter zurueck der erste Punkt.
        assert reihen[30][0][0] <= reihen[7][0][0]

        # Bild: keine negative Kurs-Achse, log. Skala bei grosser Spanne.
        buf = render.floaktie_chart(reihen[1][0], "$FLO", "1 Tag", reihen[1][1])
        assert buf is not None and len(buf.getvalue()) > 5000
        for pts in ([5000] * 40, [1000, 78000], [50, 1_500_000], [0, 0, 1000]):
            assert render.floaktie_chart(pts, "$FLO", "x", 1.0) is not None
    finally:
        fa._store, fa._enabled = alt




def test_floaktie_chart_serie():
    """REGRESSION (Dashboard-Chart war falsch): Die Kurs-Reihe muss aus den FEINEN
    Intraday-Ticks kommen (nicht nur aus den Tages-Schlusskursen) und IMMER auf dem
    aktuellen Kurs enden - sonst passt die Linie nicht zur angezeigten Kurs-Zahl."""
    import time as _t
    import floaktie
    restore = _with_economy({1: 1000})
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    now = _t.time()
    # 2 Tage Minuten-Ticks (steigend) + nur 2 Tages-Schlusskurse
    ticks = [{"t": int(now - (2 * 24 * 60 - i) * 60), "price": 1000 + i}
             for i in range(2 * 24 * 60)]
    fa._store = _FakeStore({"price": 1000 + len(ticks) - 1, "base": 1000.0,
                            "holdings": {}, "ticks": ticks,
                            "history": [{"day": "d1", "price": 1000},
                                        {"day": "d2", "price": 1500}],
                            "day": "x", "act_ema": floaktie.ACT_BASELINE,
                            "msg_count": 0, "last_msg_count": 0})
    try:
        punkte, chg = fa.series(1)
        # Kommt aus den Ticks (viele Punkte), nicht aus den 2 History-Einträgen.
        assert len(punkte) > 10, len(punkte)
        # Endet EXAKT auf dem aktuellen Kurs.
        assert punkte[-1] == fa.price(), (punkte[-1], fa.price())
        # Änderung passt zur gezeigten Reihe.
        erwartet = round((punkte[-1] - punkte[0]) / punkte[0] * 100, 2)
        assert abs(chg - erwartet) < 0.01, (chg, erwartet)
        # Auf eine handliche Punktzahl verdichtet (nicht 2880 Punkte ins SVG).
        assert len(punkte) <= 120
        # Größerer Zeitraum -> größere Spanne, immer noch am aktuellen Kurs.
        p7, chg7 = fa.series(7)
        assert p7[-1] == fa.price() and p7[0] <= punkte[0]
        # Ohne jede Historie bleibt es robust (2 Punkte, kein Crash).
        fa._store.data["ticks"] = []
        fa._store.data["history"] = []
        p0, c0 = fa.series(1)
        assert len(p0) >= 2 and p0[-1] == fa.price() and c0 == 0.0
    finally:
        fa._store, fa._enabled = alt
        restore()




def test_floaktie_grenzen_gegen_hyperinflation():
    """Die Aktie darf ein Depot ueber einen Vormittag vervielfachen (genau das war
    der Wunsch), aber sie darf die Wirtschaft nicht zerreissen.

    Gemessener Ausgangsbug: 750 Anteile fuer 1.147.501 gekauft, 5 h aktiver Call,
    Kurs 79 Mio -> Verkauf fuer 43.655.972.117 Coins. Faktor 38.043 aus dem Nichts,
    weil die Kurve keine Reserve hat. Vier Bremsen halten das jetzt:
    Kurs-Deckel (absolut + je Aktivitaet), Depot-Deckel, Verkaufssteuer, Tagesbremse."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, floaktie.TICK_NOISE, fa._today)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    restore_eco = _with_economy({1: 5_000_000, 2: 5_000_000})

    def frisch(preis=None, tag="2026-07-26"):
        preis = floaktie.FAIR_BASE if preis is None else preis
        fa._store = _FakeStore({"price": int(preis), "base": float(preis), "day": tag,
                                "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {}, "history": [], "ticks": []})
        fa._today = lambda: tag
        fa._tages_anker()
        fa._sync_price()

    try:
        # 1) DEPOT-DECKEL: niemand haelt mehr als MAX_SHARES_PER_USER Anteile -
        #    das begrenzt, wie viel Geld eine Person aus einer Blase schoepfen kann.
        frisch()
        deckel = floaktie.MAX_SHARES_PER_USER
        asyncio.run(fa.buy(SimpleNamespace(id=1), deckel + 500))
        assert fa.shares_of(1) == deckel, fa.shares_of(1)
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 10))
        assert "Depot ist voll" in r and fa.shares_of(1) == deckel
        # 'kauf max' respektiert den freien Platz ebenfalls.
        asyncio.run(fa.sell(SimpleNamespace(id=1), 20))
        economy.instance._profile(1)["coins"] = 10 ** 12
        assert fa._resolve_count(SimpleNamespace(id=1), "max") == 20

        # 2) KURS-DECKEL je Aktivitaet: Dauer-Aktivitaet laeuft in ein Niveau,
        #    nicht in die Unendlichkeit - und der Deckel steigt nur mit mehr Leuten.
        frisch()
        for _ in range(12 * 60):
            fa._activity_tick(3, 0)
        akt3 = fa.activity_of(3, 0, 0, 0)
        d3 = fa.ziel_base(akt3) * floaktie.CEIL_FACTOR
        assert fa.price() <= d3 * 1.06, (fa.price(), d3)
        assert fa.ziel_base(fa.activity_of(10, 5, 0, 0)) > fa.ziel_base(akt3)

        # 3) ABSOLUTE Notbremse: auch mit absurdem Basiskurs bleibt der ANGEZEIGTE
        #    Kurs unter MAX_PRICE (vorher lief er in Gleitkomma-Regionen).
        frisch()
        fa._store.data["base"] = 10.0 ** 18
        assert fa.price() <= floaktie.MAX_PRICE, fa.price()
        assert fa._base() <= floaktie.MAX_PRICE

        # 4) TAGESBREMSE (Circuit Breaker): innerhalb eines Tages maximal x DAY_UP.
        frisch(1000)
        anker = fa._state()["open_base"]
        fa._store.data["base"] = anker * floaktie.DAY_UP * 1000
        assert fa._base() <= anker * floaktie.DAY_UP * 1.0001, (fa._base(), anker)
        # ... und nicht tiefer als DAY_DOWN.
        fa._store.data["base"] = anker * floaktie.DAY_DOWN / 1000
        assert fa._base() >= anker * floaktie.DAY_DOWN * 0.9999
        # Neuer Tag -> neuer Anker (die Bremse ist tages-, nicht ewigkeitsbezogen).
        fa._today = lambda: "2026-07-27"
        fa._tages_anker()
        assert fa._state()["open_day"] == "2026-07-27"

        # 5) VERKAUFSSTEUER steigt mit der Blase - der wichtigste Geld-Abfluss.
        #    Vom Bodensatz (10) aus braucht der Kurs eine Weile, bis er ueberhaupt
        #    UEBER seinem Zielkurs steht - erst dann ist es eine Blase.
        frisch()
        normal = fa._sell_fee()
        for _ in range(14 * 60):
            fa._activity_tick(3, 0)
        assert fa._base() > fa.ziel_base(fa._state()["act_ema"]), "keine Blase entstanden"
        blase = fa._sell_fee()
        assert normal < blase <= floaktie.SELL_TAX_MAX, (normal, blase)
        assert abs(normal - floaktie.TRADE_FEE) < 0.01

        # 6) DIE ENTSCHEIDENDE ZAHL: ein guter Vormittag darf sich lohnen
        #    (Faktor > 5), aber nicht Milliarden aus dem Nichts machen.
        frisch()
        kosten, _ = fa._buy_cost(deckel)
        fa._holdings()["1"] = deckel
        fa._sync_price()
        for _ in range(5 * 60):
            fa._activity_tick(3, 1, 0, 4)      # 3 Leute, einer streamt, etwas Chat
        erloes, _ = fa._sell_proceeds(deckel)
        faktor = erloes / max(1, kosten)
        # Der FAKTOR ist absichtlich gross: der Kurs startet am Bodensatz, wer
        # frueh kauft, verdient kraeftig. Begrenzt wird nicht der Faktor, sondern
        # der ABSOLUTE Erloes - nur der bestimmt, wie viele Coins entstehen.
        assert faktor > 5, (kosten, erloes, faktor)
        assert erloes < 100_000_000, erloes     # nie Milliarden aus einer Runde
        # Gegenprobe: mehr als Depot-Deckel x Kurs-Deckel geht NIE.
        assert erloes <= floaktie.MAX_SHARES_PER_USER * floaktie.MAX_PRICE

        # 7) NOTAUS: ist die Aktie per Panel aus, geht gar nichts mehr.
        import features
        alt_dis = set(features.instance._disabled)
        try:
            features.instance._disabled.add("floaktie")
            assert fa.is_off()
            frisch()
            r = asyncio.run(fa.buy(SimpleNamespace(id=2), 5))
            assert "deaktiviert" in r and fa.shares_of(2) == 0
            fa._holdings()["2"] = 10
            r = asyncio.run(fa.sell(SimpleNamespace(id=2), 10))
            assert "deaktiviert" in r and fa.shares_of(2) == 10
            # Kein Kurs-Tick und keine Aktivitaets-Zaehlung mehr.
            vorher = fa.price()
            guild = SimpleNamespace(voice_channels=[], afk_channel=None)
            asyncio.run(fa.sample_and_tick(guild))
            fa.note_message()
            assert fa.price() == vorher and fa._state()["msg_count"] == 0
        finally:
            features.instance._disabled = alt_dis
    finally:
        restore_eco()
        fa._store, fa._enabled, floaktie.TICK_NOISE, fa._today = alt




def test_floaktie_bots_zaehlen_nie():
    """Der Bot (und jeder andere) darf die Aktie NICHT beeinflussen. Gemeldet aus
    dem Betrieb: der Kurs sank nie. Ursache: gezaehlt wurde ueber voice_states, und
    ob ein Eintrag ein Bot ist, stand nur im Member-Cache - lieferte der nichts,
    zaehlte der Musik-Bot als MENSCH und hielt die Aktivitaet dauerhaft ueber 0."""
    import floaktie
    fa = floaktie.instance

    def member(uid, *, bot=False, stream=False, video=False):
        vs = SimpleNamespace(self_stream=stream, self_video=video)
        return SimpleNamespace(id=uid, bot=bot, voice=vs), vs

    m_mensch, vs_mensch = member(1, stream=True)
    m_bot, vs_bot = member(2, bot=True)
    m_flo, vs_flo = member(99)

    class Chan:
        def __init__(self, members, states):
            self.id = 5
            self.members = members
            self.voice_states = states

    class Guild:
        def __init__(self, chan, cache):
            self.voice_channels = [chan]
            self.afk_channel = None
            self.me = SimpleNamespace(id=99)
            self._cache = cache

        def get_member(self, uid):
            return self._cache.get(uid)

    # 1) Normalfall: Member-Objekte da -> Bot und Flo zaehlen nicht.
    chan = Chan([m_mensch, m_bot, m_flo], {1: vs_mensch, 2: vs_bot, 99: vs_flo})
    g = Guild(chan, {1: m_mensch, 2: m_bot, 99: m_flo})
    assert fa._measure(g) == (1, 1, 0), fa._measure(g)

    # 2) NUR Bots im Call -> Aktivitaet 0 (vorher: der Bot hielt sie ueber 0).
    chan = Chan([m_bot, m_flo], {2: vs_bot, 99: vs_flo})
    g = Guild(chan, {2: m_bot, 99: m_flo})
    assert fa._measure(g) == (0, 0, 0), fa._measure(g)
    assert fa.activity_of(*fa._measure(g)) == 0.0

    # 3) Member-Cache LEER, nur voice_states: unbekannte Eintraege zaehlen NICHT.
    #    Genau hier lag der Fehler - unbekannt hiess frueher "Mensch".
    chan = Chan([], {1: vs_mensch, 2: vs_bot, 99: vs_flo})
    g = Guild(chan, {})                      # get_member liefert nichts
    assert fa._measure(g) == (0, 0, 0), fa._measure(g)
    # ... sobald der Cache den Menschen kennt, zaehlt er wieder.
    g = Guild(chan, {1: m_mensch})
    assert fa._measure(g) == (1, 1, 0), fa._measure(g)

    # 4) AFK-Kanal zaehlt nie.
    chan = Chan([m_mensch], {1: vs_mensch})
    g = Guild(chan, {1: m_mensch})
    g.afk_channel = SimpleNamespace(id=5)
    assert fa._measure(g) == (0, 0, 0)




def test_bilder_blockieren_den_bot_nicht():
    """Kein Bild darf auf dem Event-Loop gezeichnet werden.

    PIL rechnet synchron: die Profilkarte braucht ~28 ms, der Kurs-Chart ~52 ms.
    Solange das auf dem Loop laeuft, steht der GANZE Bot - Musik, Aktien-Takt,
    jede andere Antwort. Drei Aufrufe hatten die Auslagerung nicht (Profilkarte,
    Bestenliste, Kurs-Chart), waehrend alle uebrigen sie laengst hatten."""
    import ast
    import pathlib

    verdaechtig = []
    for p in sorted(pathlib.Path(".").glob("*.py")):
        if p.name.startswith("test_") or p.name in ("render.py", "leaderboard_img.py"):
            continue
        quelle = p.read_text(encoding="utf-8")
        baum = ast.parse(quelle)
        # Alle Funktionen merken, die als Argument in einen Thread gereicht
        # werden (words._build_top zeichnet z. B. selbst, wird aber ausgelagert).
        im_thread = set()
        for n in ast.walk(baum):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                continue
            if n.func.attr not in ("to_thread", "run_in_executor"):
                continue
            for arg in n.args:
                if isinstance(arg, ast.Attribute):
                    im_thread.add(arg.attr)
                elif isinstance(arg, ast.Name):
                    im_thread.add(arg.id)

        def ausgelagert(knoten):
            """Steht der Aufruf selbst in einem to_thread/run_in_executor?"""
            for eltern in ast.walk(baum):
                if not (isinstance(eltern, ast.Call)
                        and isinstance(eltern.func, ast.Attribute)
                        and eltern.func.attr in ("to_thread", "run_in_executor")):
                    continue
                for kind in ast.walk(eltern):
                    if kind is knoten:
                        return True
            return False

        for fn in ast.walk(baum):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name in im_thread:
                continue                      # wird als Ganzes ausgelagert
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
                    continue
                if not (isinstance(n.func.value, ast.Name)
                        and n.func.value.id in ("render", "leaderboard_img")):
                    continue
                if n.func.attr in ("is_available", "is_enabled", "setup"):
                    continue                  # keine Zeichenarbeit
                if ausgelagert(n):
                    continue
                verdaechtig.append(f"{p.name}:{n.lineno} {n.func.value.id}.{n.func.attr}")

        # ZWEITER Weg, den die Suche oben NICHT sieht: die Zeichenfunktion wird
        # per getattr geholt und dann unter einem eigenen Namen gerufen -
        #     fn = getattr(render, "shop_banner", None)
        #     buf = fn(items, date=date)
        # Genau so lief das Shop-Banner (gemessen 236-278 ms) jahrelang auf dem
        # Event-Loop, waehrend dieser Test gruen war.
        per_getattr = {}
        for n in ast.walk(baum):
            if not (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)):
                continue
            ruf = n.value
            if not (isinstance(ruf.func, ast.Name) and ruf.func.id == "getattr"):
                continue
            if not (ruf.args and isinstance(ruf.args[0], ast.Name)
                    and ruf.args[0].id in ("render", "leaderboard_img")):
                continue
            for ziel in n.targets:
                if isinstance(ziel, ast.Name):
                    quelle_name = (ruf.args[1].value
                                   if len(ruf.args) > 1 and isinstance(ruf.args[1], ast.Constant)
                                   else "?")
                    per_getattr[ziel.id] = quelle_name
        if per_getattr:
            for fn in ast.walk(baum):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name in im_thread:
                    continue
                for n in ast.walk(fn):
                    if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)):
                        continue
                    if n.func.id not in per_getattr or ausgelagert(n):
                        continue
                    verdaechtig.append(
                        f"{p.name}:{n.lineno} {n.func.id}() = "
                        f"render.{per_getattr[n.func.id]} (per getattr geholt)")

    assert not verdaechtig, ("Bild wird auf dem Event-Loop gezeichnet: "
                             + ", ".join(verdaechtig))




def test_floaktie_chat_haengt_nicht_am_takt():
    """Derselbe Chat-Verkehr muss dieselbe Aktivitaet ergeben - egal wie schnell
    der Loop taktet.

    Nachrichten sind als einzige Groesse eine ZAEHLUNG ueber den Takt; Leute,
    Streams und Kameras sind Zustaende. Ohne Hochrechnen auf eine Minute zaehlte
    derselbe Verkehr bei 20-s-Takt nur einen Bruchteil: gemessen 8,0 statt 15,0
    Punkte. Der Bot taktet im Betrieb alle 20 s - der Chat zaehlte dort also
    deutlich weniger als in jeder Rechnung und jeder Simulation."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-06"
    try:
        def aktivitaet_bei(dt, msgs_pro_minute, leute=3, streams=0):
            fa._store = _FakeStore({"price": 0, "base": 1000.0, "day": "x",
                                    "act_ema": 0.0, "grund_akt": 0.0, "mom": 0.0,
                                    "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0, "holdings": {},
                                    "history": [], "ticks": [],
                                    "open_day": "2026-08-06", "open_base": 1000.0})
            fa._sync_price()
            rest, akts = 0.0, []
            for _ in range(int(30 * 60 / dt)):
                rest += msgs_pro_minute * dt / 60.0
                n = int(rest)
                rest -= n
                _a, _p, _d, akt = fa._activity_tick(leute, n, streams, 0, dt=dt)
                akts.append(akt)
            return sum(akts) / len(akts)

        # Glatt aufgehende Raten: sonst schwankt die Eingabe und die bewusst
        # asymmetrische Glaettung hebt den Mittelwert (das ist gewollt).
        for rate in (0, 3, 6, 30):
            a60 = aktivitaet_bei(60.0, rate)
            a20 = aktivitaet_bei(20.0, rate)
            assert abs(a60 - a20) < 0.01, (rate, a60, a20)

        # Und der Chat traegt ueberhaupt spuerbar bei (sonst prueft das nichts).
        assert aktivitaet_bei(20.0, 30) > aktivitaet_bei(20.0, 0) + 5
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt




def test_floaktie_steht_nie_still():
    """Der Kurs bewegt sich IMMER - er steigt oder faellt, aber er steht nie.

    Gemessen vorher: am Grundwert 200 von 200 Takten exakt 0,000 %/min und ueber
    200 Takte ein einziger Kurswert. Eine Aktie, die stundenlang auf derselben
    Zahl steht, sieht kaputt aus.

    Das Atmen muss dabei SYMMETRISCH bleiben (Erwartungswert 0) und am Niveau
    haengen - sonst waere es nach oben Inflation und nach unten ein Grab."""
    import random
    import statistics
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE,
           floaktie.VOL_SPREAD)
    fa._enabled = True
    fa._today = lambda: "2026-08-06"
    try:
        def frisch(base, ema=0.0, grund=0.0, S=0):
            fa._store = _FakeStore({"price": 0, "base": float(base), "day": "x",
                                    "act_ema": ema, "grund_akt": grund, "mom": 0.0,
                                    "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0,
                                    "holdings": ({"1": S} if S else {}),
                                    "history": [], "ticks": [],
                                    "open_day": "2026-08-06",
                                    "open_base": float(base)})
            fa._sync_price()

        # 1) AM GRUNDWERT (leerer Call) - das war der echte Stillstand.
        frisch(3000.0, grund=0.75)
        fa._store.data["base"] = fa.boden_base()
        fa._sync_price()
        kurse, drifts = [], []
        for _ in range(300):
            _a, n, d, _akt = fa._activity_tick(0, 0)
            kurse.append(n)
            drifts.append(d)
        assert all(d != 0.0 for d in drifts), "Takt mit exakt 0 am Grundwert"
        assert len(set(kurse)) > 20, (len(set(kurse)), min(kurse), max(kurse))

        # 2) AM DECKEL (Leute im Call).
        akt = fa.activity_of(6, 2, 0, 0)
        frisch(fa.ziel_base(akt) * floaktie.CEIL_FACTOR * 1.02, ema=akt, grund=0.75)
        kurse, drifts = [], []
        for _ in range(300):
            _a, n, d, _akt = fa._activity_tick(6, 0, streams=2)
            kurse.append(n)
            drifts.append(d)
        assert all(d != 0.0 for d in drifts), "Takt mit exakt 0 am Deckel"
        assert len(set(kurse)) > 20, len(set(kurse))

        # 3) NORMALER BETRIEB: auch dort nie exakt 0 - ein stark negatives
        #    Momentum konnte die Summe frueher auf genau 0 druecken.
        frisch(1000.0, grund=0.5)
        drifts = []
        for _ in range(300):
            _a, _n, d, _akt = fa._activity_tick(4, 5, streams=1)
            drifts.append(d)
        assert all(d != 0.0 for d in drifts), "Takt mit exakt 0 im Normalbetrieb"

        # 4) SYMMETRIE: das Atmen darf den Kurs nicht systematisch tragen.
        #    Boden festhalten und ueber viele Seeds den Endstand mitteln.
        enden = []
        for seed in range(30):
            random.seed(seed)
            frisch(3000.0, grund=0.75)
            fa.boden_base = lambda: 3000.0
            fa.drift_fuer = lambda _a: 0.0
            try:
                for _ in range(300):
                    fa._activity_tick(0, 0)
                enden.append(fa.price() / 3000.0 - 1.0)
            finally:
                del fa.boden_base, fa.drift_fuer
        mittel = statistics.mean(enden)
        streuung = statistics.pstdev(enden)
        # Der Mittelwert muss im Rauschen liegen (Standardfehler = sd/sqrt(n)).
        assert abs(mittel) < 3.0 * streuung / (len(enden) ** 0.5) + 0.01, \
            (mittel, streuung)

        # 5) Das Atmen bleibt klein - es ist kein Antrieb.
        frisch(3000.0, grund=0.75)
        for _ in range(200):
            assert abs(fa._atem()) <= floaktie.ATEM_MAX + 1e-9
            assert fa._atem() != 0.0

        # 6) Und es zieht ans Niveau zurueck: unter dem Boden nach oben.
        frisch(1000.0, grund=0.75)          # Boden liegt deutlich hoeher
        assert fa.boden_base() > 1500, fa.boden_base()
        zuege = [fa._atem(fa.boden_base()) for _ in range(200)]
        assert statistics.mean(zuege) > 0, statistics.mean(zuege)
        # ... und ueber dem Deckel nach unten.
        frisch(90_000.0, grund=0.1)
        zuege = [fa._atem(fa.akt_deckel_base()) for _ in range(200)]
        assert statistics.mean(zuege) < 0, statistics.mean(zuege)
    finally:
        (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE,
         floaktie.VOL_SPREAD) = alt




def test_floaktie_grundwert_traegt_den_boden():
    """Der Boden ist BEWEGLICH: die Aktie ist so viel wert, wie der Server im
    Schnitt lebendig ist.

    Vorher war der Boden fest (FAIR_BASE = 10). Bei -11 % je halber Stunde ist
    nach einer Nacht alles weg - in der 60-Tage-Simulation stand der Kurs jeden
    Morgen bei 10 und jeden Abend bei ~130.000, Spanne 182-fach. Kein Markt,
    nur ein Saegezahn."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-06"
    try:
        def frisch(kurs=1000):
            fa._store = _FakeStore({"price": int(kurs), "base": float(kurs),
                                    "day": "x", "act_ema": 0.0, "grund_akt": 0.0,
                                    "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0, "mom": 0.0, "holdings": {},
                                    "history": [], "ticks": [],
                                    "open_day": "2026-08-06", "open_base": float(kurs)})
            fa._sync_price()

        # 1) Frischer Server: Grundwert 0 -> Boden ist der Mindestwert.
        frisch()
        assert fa.grund_akt() == 0.0
        assert abs(fa.boden_base() - floaktie.FAIR_BASE) < 0.01, fa.boden_base()

        # 2) Ein paar Tage mit taeglichem Abend-Call heben den Boden deutlich.
        frisch(1000)
        for _tag in range(5):
            for _ in range(4 * 60):                     # 4 h Call
                fa._activity_tick(6, 3, streams=2)
            for _ in range(20 * 60):                    # 20 h Ruhe
                fa._activity_tick(0, 0)
        boden_lebendig = fa.boden_base()
        assert boden_lebendig > floaktie.FAIR_BASE * 50, boden_lebendig

        # 3) Genau dorthin faellt der Kurs im Leerlauf - und nicht tiefer.
        fa._store.data["base"] = boden_lebendig * 5.0
        fa._sync_price()
        for _ in range(48 * 60):                        # zwei ganze Tage leer
            fa._activity_tick(0, 0)
        assert fa._base() >= fa.boden_base() * 0.98, (fa._base(), fa.boden_base())
        # Am Grundwert ist Schluss. Geprueft wird das ueber _am_grundwert() mit
        # seiner Toleranz und NICHT ueber "drift_fuer(0) == 0.0": der Kurs atmet
        # dort und liegt mal ein Stueck ueber dem Boden, dann liefert drift_fuer
        # richtigerweise den Verfall. Mit der strengen Gleichheit war dieser Test
        # in 2 von 25 Laeufen grundlos rot.
        assert fa._am_grundwert(), (fa._base(), fa.boden_base())

        # 4) Bleibt es WIRKLICH dauerhaft still, schmilzt auch der Grundwert.
        for _ in range(20 * 24 * 60):                   # 20 Tage tot
            fa._activity_tick(0, 0)
        assert fa.boden_base() < boden_lebendig * 0.2, (fa.boden_base(), boden_lebendig)

        # 4b) Der angezeigte Deckel darf NIE unter dem Grundwert liegen - sonst
        #     stand im Panel "Kurs 6.511, Deckel 22", und der Sofort-Impuls war
        #     tot (er bricht ab, sobald der Kurs ueber dem Deckel steht - also
        #     ausgerechnet dann, wenn jemand einen stillen Server betritt).
        frisch(1000)
        for _tag in range(5):
            for _ in range(4 * 60):
                fa._activity_tick(6, 3, streams=2)
            for _ in range(20 * 60):
                fa._activity_tick(0, 0)
        assert fa.akt_deckel_base() >= fa.boden_base(), (fa.akt_deckel_base(),
                                                         fa.boden_base())
        assert fa._deckel_kurs() >= fa.price() * 0.99, (fa._deckel_kurs(), fa.price())
        vor_puls = fa.price()
        fa._store.data["pulse_min"] = 0
        fa._store.data["pulse_sum"] = 0.0
        fa._puls(floaktie.PULSE_JOIN, "jemand kommt")
        assert fa.price() >= vor_puls, (vor_puls, fa.price())

        # 5) Ein einzelner Abend darf den Boden nicht hochreissen (Traegheit) -
        #    sonst waere er nur eine langsamere Kopie des Kurses.
        frisch(1000)
        vor = fa.boden_base()
        for _ in range(4 * 60):
            fa._activity_tick(10, 5, streams=4)
        akt = fa.activity_of(10, 4, 0, 5)
        assert fa.boden_base() < fa.ziel_base(akt) * 0.25, (fa.boden_base(),
                                                            fa.ziel_base(akt))
        assert fa.boden_base() > vor
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt




def test_floaktie_ist_streng_aber_ein_markt():
    """Die Vorgabe: man kann schnell viel gewinnen UND schnell viel verlieren.

    Gemessen an einem Tagesablauf (Abend-Call, danach Ruhe) - genau der Fall,
    um den es geht. Die ausfuehrliche 60-Tage-Fassung steht in
    tools_aktien_sim.py; hier die Kernaussagen als Regressionsschutz."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-06"
    try:
        fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                "act_ema": 0.0, "grund_akt": 0.0, "mom": 0.0,
                                "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {}, "history": [],
                                "ticks": [], "open_day": "2026-08-06",
                                "open_base": 1000.0})
        fa._sync_price()
        # Eine Woche einschwingen lassen, damit der Grundwert steht.
        for _tag in range(7):
            for _ in range(4 * 60):
                fa._activity_tick(6, 3, streams=2)
            for _ in range(20 * 60):
                fa._activity_tick(0, 0)

        # SCHNELL VIEL GEWINNEN: wer zum Call-Start kauft, verdoppelt in Stunden.
        vor_abend = fa.price()
        for _ in range(4 * 60):
            fa._activity_tick(6, 3, streams=2)
        hoch = fa.price()
        assert hoch >= vor_abend * 2.0, (vor_abend, hoch)

        # SCHNELL VIEL VERLIEREN: wer bis zum naechsten Morgen haelt, blutet.
        for _ in range(8 * 60):
            fa._activity_tick(0, 0)
        morgen = fa.price()
        assert morgen <= hoch * 0.65, (hoch, morgen)

        # ABER NICHT INS NICHTS: der Grundwert faengt den Kurs auf.
        assert morgen > vor_abend * 0.4, (vor_abend, morgen)

        # Und die Tagesspanne bleibt handhabbar (kein Saegezahn ueber 100-fach).
        assert hoch / max(1, morgen) < 30, (hoch, morgen)
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt




def test_floaktie_chart_klebt_keine_tage_doppelt():
    """Die Kurs-Reihe darf Tage nicht doppelt zaehlen.

    Vorher wurden schlicht die n NEUESTEN Tages-Schlusskurse vor die Ticks
    geklebt - die liegen aber genau IM Tick-Fenster. Gemessen bei '7 Tage' mit
    Ticks der letzten 3 Tage: alle 4 vorangestellten Tage waren doppelt, der
    Kurs von vor 7 Tagen kam gar nicht vor, und die angezeigte Veraenderung war
    +67 % statt +290 %."""
    import time as _t
    from datetime import datetime as _dt, timedelta as _td
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    try:
        jetzt = _t.time()
        heute = _dt.now(floaktie.TIMEZONE)
        # 10 Tage Schlusskurse (100..1000), Ticks nur fuer die letzten 3 Tage.
        hist = [{"day": (heute - _td(days=9 - i)).strftime("%Y-%m-%d"),
                 "price": 100 * (i + 1)} for i in range(10)]
        ticks = [{"t": jetzt - 3 * 86400 + i * 3600, "price": 1100 + i}
                 for i in range(72)]
        fa._store = _FakeStore({"price": 0, "base": 1171.0, "holdings": {},
                                "history": hist, "ticks": ticks, "act_ema": 0.0,
                                "leer_min": 0.0, "msg_count": 0,
                                "last_msg_count": 0})
        fa._sync_price()

        # 1) Kein Tageskurs aus dem Tick-Fenster darf vorangestellt werden.
        ab_tag = _dt.fromtimestamp(min(t["t"] for t in ticks),
                                   floaktie.TIMEZONE).strftime("%Y-%m-%d")
        drin = {h["price"] for h in hist if h["day"] >= ab_tag}
        reihe = fa._series(7)
        kopf = reihe[:len(reihe) - len(ticks)]
        assert not (drin & set(kopf)), (sorted(drin & set(kopf)), kopf)

        # 2) Die 7-Tage-Reihe beginnt beim Kurs von vor 7 Tagen.
        vor7 = next(h["price"] for h in hist
                    if h["day"] == (heute - _td(days=7)).strftime("%Y-%m-%d"))
        assert reihe[0] == vor7, (reihe[0], vor7)
        _pts, chg = fa.series(7)
        assert abs(chg - (1171 - vor7) / vor7 * 100) < 1.0, chg

        # 3) Laengeres Fenster -> frueherer Startpunkt, nie spaeter.
        starts = [fa._series(d)[0] for d in (1, 7, 30)]
        assert starts[2] <= starts[1] <= starts[0], starts

        # 4) Decken die Ticks das ganze Fenster ab, kommt KEINE Historie dazu.
        #
        # Der erste Tick lag hier auf GENAU der Fenstergrenze (jetzt - 86400).
        # _series liest die Uhr aber selbst und spaeter - vergehen dazwischen
        # ein paar Millisekunden, rutscht der Tick aus dem Fenster, Historie
        # wird ergaenzt und der Test faellt um. Genau so ist er auf einer
        # ausgelasteten Maschine gerissen (erwartet >= 1100, bekommen 900),
        # nachdem er kurz zuvor noch gruen war. Eine Minute Luft macht ihn von
        # der Wanduhr unabhaengig, ohne die Aussage zu aendern.
        fa._store.data["ticks"] = [{"t": jetzt - 86400 + 60 + i * 600,
                                    "price": 1100 + i} for i in range(140)]
        assert fa._series(1)[0] >= 1100, fa._series(1)[0]

        # 5) Ganz ohne Ticks traegt die Historie die Reihe allein.
        fa._store.data["ticks"] = []
        ohne = fa._series(7)
        assert len(ohne) >= 2 and ohne[0] == vor7, ohne[:3]
    finally:
        fa._store, fa._enabled = alt




def test_floaktie_kurs_ist_nie_veraltet():
    """price() muss IMMER zur Kurve passen - auch wenn jemand den gespeicherten
    Stand von Hand verbiegt oder ein Schreibweg _sync_price() vergisst.

    Aufgefallen im Betriebs-Log: 'Kurs 1.000->6.193' bei NEGATIVEM Drift. Ursache
    war ein veraltetes st['price']; ueber _record_tick waere dieser falsche Wert
    zusaetzlich in Chart und Tages-Historie gewandert."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    try:
        fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {}, "history": [],
                                "ticks": []})
        fa._sync_price()

        def erwartet():
            return max(floaktie.MIN_PRICE, min(floaktie.MAX_PRICE, int(round(
                fa._base() * (1.0 + fa.total_shares() / floaktie.LIQUIDITY)))))

        # 1) Anteile dazu, ohne zu synchronisieren -> price() zieht trotzdem mit.
        fa._store.data["holdings"] = {"1": 500}
        assert fa.price() == erwartet(), (fa.price(), erwartet())
        # 2) Gespeicherter Stand von Hand verbogen -> price() glaubt ihm NICHT.
        fa._store.data["price"] = 7
        assert fa.price() == erwartet(), fa.price()
        assert fa.price() > 1000
        # 3) Basiskurs geaendert, ohne zu synchronisieren.
        fa._store.data["base"] = 250.0
        assert fa.price() == erwartet(), fa.price()
        # 4) Und der Chart bekommt nie einen veralteten Wert zu sehen.
        fa._store.data["ticks"] = []
        fa._record_tick()
        assert fa._store.data["ticks"][-1]["price"] == erwartet()
        # 5) Ein Takt meldet einen ECHTEN Vorher/Nachher-Vergleich: sinkt der
        #    Kurs, darf 'neu' nicht ueber 'alt' liegen.
        for _ in range(20):
            a, n, drift, _akt = fa._activity_tick(0, 0)
            assert (n <= a) == (drift <= 0), (a, n, drift)
    finally:
        fa._store, fa._enabled = alt




def test_floaktie_volatilitaet_ist_symmetrisch():
    """Die Volatilitaet macht den Kurs lebendig - sie darf ihn aber weder
    systematisch heben noch druecken. Der einzige Test, der sie EINSCHALTET
    (die Suite laeuft sonst bewusst ohne, siehe Kopf der Datei)."""
    import statistics
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, floaktie.TICK_NOISE, floaktie.VOL_SPREAD)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    floaktie.VOL_SPREAD = 0.8
    try:
        def frisch():
            fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                    "act_ema": 0.0, "msg_count": 0,
                                    "last_msg_count": 0, "holdings": {},
                                    "history": [], "ticks": []})
            fa._sync_price()

        drifts = []
        for _ in range(400):
            frisch()
            _a, _n, d, _akt = fa._activity_tick(10, 20, streams=10)
            drifts.append(d)

        # 1) Es schwankt ueberhaupt - sonst waere die Aktie tot.
        assert len(set(drifts)) > 300, len(set(drifts))
        # 2) Es geht in dieser Lage NIE nach unten (Aktivitaet ist hoch).
        assert min(drifts) > 0, min(drifts)
        # 3) Sie bleibt innerhalb ihres Spielraums um den Anschlag.
        spanne = floaktie.VOL_SPREAD
        assert max(drifts) <= floaktie.TICK_CAP * (1.0 + spanne) + 1e-9, max(drifts)
        # 4) Der Mittelwert liegt beim ungestoerten Trend, nicht daneben.
        floaktie.VOL_SPREAD = 0.0
        frisch()
        _a, _n, ruhig, _akt = fa._activity_tick(10, 20, streams=10)
        schnitt = statistics.mean(drifts)
        assert abs(schnitt - ruhig) < ruhig * 0.15, (schnitt, ruhig)
    finally:
        fa._store, fa._enabled, floaktie.TICK_NOISE, floaktie.VOL_SPREAD = alt




def test_floaktie_leerlauf_faellt_ab_der_ersten_minute():
    """Gemeldet: "es sinkt nur so 1 % pro Stunde". Verlangt: gute 20 % pro Stunde,
    ab der ERSTEN leeren Minute - und der Anstieg im Call bleibt unveraendert.

    Geprueft wird deshalb dreierlei:
      1. schon bei leer_min = 0 sind es >= 20 %/h,
      2. eine volle leere Stunde kostet deutlich mehr als 20 %,
      3. der Anstieg bei Aktivitaet ist NICHT mit gedrosselt worden."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-08-05"
    try:
        def frisch(kurs, S=147):
            b = kurs / (1 + S / floaktie.LIQUIDITY)
            fa._store = _FakeStore({"price": int(kurs), "base": b, "day": "x",
                                    "act_ema": 0.0, "msg_count": 0,
                                    "last_msg_count": 0, "leer_min": 0.0,
                                    "open_day": "2026-08-05", "open_base": b,
                                    "holdings": {"1": S}, "history": [], "ticks": []})
            fa._sync_price()

        # 1) Sofort-Rate: auch in der allerersten leeren Minute >= 20 %/h.
        frisch(5_000)
        pro_min = fa._leerlauf_verfall()
        pro_std = 1.0 - (1.0 - pro_min) ** 60
        assert pro_std >= 0.20, (pro_min, pro_std)

        # ... und mit laenger werdendem Leerlauf wird es NUR schneller.
        vorher = 0.0
        for leer in (0, 2, 4, 6, 30):
            fa._state()["leer_min"] = float(leer)
            jetzt = fa._leerlauf_verfall()
            assert jetzt >= vorher, (leer, jetzt, vorher)
            vorher = jetzt

        # 2) Eine volle leere Stunde: deutlich mehr als die geforderten 20 %.
        for start in (1_000, 5_000, 40_000):
            frisch(start)
            for _ in range(60):
                fa._activity_tick(0, 0)
            verlust = 1.0 - fa.price() / start
            assert verlust >= 0.20, (start, fa.price(), verlust)

        # 3) Der ANSTIEG im Call bleibt: 6 Leute + 2 Streams heben den Kurs.
        frisch(1_000)
        for _ in range(60):
            fa._activity_tick(6, 4, streams=2)
        assert fa.price() > 1_000, fa.price()
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt




def test_floaktie_reset_befehl():
    """'flo aktie reset' setzt den Kurs auf den Start zurueck - nur fuer den Chef."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today)
    fa._enabled = True
    fa._today = lambda: "2026-07-27"
    restore = _with_economy({floaktie.OWNER_ID: 1000, 7: 1000})
    fa._store = _FakeStore({"price": 500_000, "base": 400_000.0, "day": "x",
                            "act_ema": 9.9, "msg_count": 40, "last_msg_count": 10,
                            "leer_min": 3.0, "open_day": "2026-07-27",
                            "open_base": 12_345.0, "holdings": {"1": 150, "2": 30},
                            "history": [{"day": "x", "price": 1}], "ticks": [1, 2, 3]})
    fa._sync_price()
    try:
        def msg(uid, text):
            return SimpleNamespace(content=text, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="T"))

        # Fremde duerfen nicht - der Kurs bleibt, wo er war.
        vorher = fa.price()
        assert vorher > 100_000, vorher
        r = asyncio.run(fa.handle(msg(7, "aktie reset")))
        assert isinstance(r, str) and "nur der Chef" in r
        assert fa.price() == vorher

        # Der Chef schon - Depots bleiben erhalten.
        r = asyncio.run(fa.handle(msg(floaktie.OWNER_ID, "aktie reset")))
        assert "zurückgesetzt" in _embed_text(r)
        st = fa._store.data
        erwartet = max(floaktie.MIN_PRICE,
                       round(floaktie.START_PRICE * (1 + 180 / floaktie.LIQUIDITY)))
        assert fa.price() == erwartet, (fa.price(), erwartet)
        assert fa.price() < vorher / 100                   # wirklich ganz unten
        assert st["holdings"] == {"1": 150, "2": 30}       # Anteile bleiben
        assert st["history"] == [] and st["act_ema"] == 0.0 and st["leer_min"] == 0.0
        assert st["open_day"] == "2026-07-27"              # Tagesbremse neu verankert

        # 'reset alles' loescht zusaetzlich die Depots.
        r = asyncio.run(fa.handle(msg(floaktie.OWNER_ID, "aktie reset alles")))
        assert fa._store.data["holdings"] == {}
        assert "2 geleert" in _embed_text(r)
    finally:
        fa._store, fa._enabled, fa._today = alt
        restore()




def test_floaktie_faellt_auf_jedem_niveau():
    """REGRESSION (aus dem Betrieb: "die Aktie sinkt nie"): Der Boden FAIR_BASE lag
    bei 5.000, waehrend der Kurs nach einem Reset bei 1.000 startet und im Betrieb
    oft darunter stand - unterhalb des Bodens passiert im Leerlauf GAR NICHTS.
    Der Kurs war also in einer Einbahnstrasse, bis er sich versechsfacht hatte."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-07-27"
    try:
        def frisch(kurs, S=147):
            b = kurs / (1 + S / floaktie.LIQUIDITY)
            fa._store = _FakeStore({"price": int(kurs), "base": b, "day": "x",
                                    "act_ema": 0.0, "msg_count": 0,
                                    "last_msg_count": 0, "leer_min": 0.0,
                                    "open_day": "2026-07-27", "open_base": b,
                                    "holdings": {"1": S}, "history": [], "ticks": []})
            fa._sync_price()

        # Der Startkurs darf NICHT unter dem Boden liegen, sonst kann eine frisch
        # zurueckgesetzte Aktie anfangs gar nicht fallen.
        assert floaktie.START_PRICE >= floaktie.FAIR_BASE

        # Von JEDEM Niveau aus faellt der Kurs im Leerlauf spuerbar.
        for start in (1_200, 3_421, 6_000, 20_000, 52_453):
            frisch(start)
            for _ in range(4 * 60):
                fa._activity_tick(0, 0)
            assert fa.price() < start * 0.85, (start, fa.price())
            # ... und zwar in JEDER Minute, nie nach oben.
            for _ in range(60):
                _a, _n, drift, _akt = fa._activity_tick(0, 0)
                assert drift <= 0, drift

        # Innerhalb EINES Tages bremst der Circuit Breaker bei -90 %.
        frisch(20_000)
        for _ in range(1440):
            fa._activity_tick(0, 0)
        assert abs(fa.price() - 20_000 * floaktie.DAY_DOWN) < 20_000 * 0.02, fa.price()

        # Ueber mehrere Tage geht es weiter runter - aber nicht ins Bodenlose.
        for tag in range(28, 32):
            fa._today = lambda t=f"2026-07-{tag:02d}": t
            fa._tages_anker()
            for _ in range(1440):
                fa._activity_tick(0, 0)
        # Nach Tagen ohne jede Aktivitaet ist auch der GRUNDWERT abgeschmolzen -
        # ein wirklich toter Server faellt bis auf den Mindestwert durch.
        boden = fa.boden_base() * (1 + 147 / floaktie.LIQUIDITY)
        assert fa.price() >= floaktie.MIN_PRICE
        # Die Toleranz muss das ATMEN mitnehmen und wird deshalb aus den
        # Konstanten abgeleitet statt geraten. Am absoluten Boden ist der Kurs
        # einstellig - dort ist EINE ganze Zahl schon 8 %, und der Kurs pendelt
        # gemessen zwischen 10 und 13 (Boden 11,96). Mit festen 5 % war dieser
        # Test in 5 von 25 Laeufen grundlos rot.
        toleranz = boden * (0.05 + 3 * fa._atem_spanne()) + 1.0
        assert abs(fa.price() - boden) < toleranz, (fa.price(), boden, toleranz)
        assert abs(fa.boden_base() - floaktie.FAIR_BASE) < 1.0, fa.boden_base()
        # Am Boden sagt das Panel ausdruecklich, dass es nicht tiefer geht.
        restore = _with_economy({2: 0})
        try:
            panel = _embed_text(fa._panel_embed(SimpleNamespace(id=2)))
            assert "Grundwert erreicht" in panel, panel
        finally:
            restore()

        # Und nach oben geht es weiterhin: vom Boden aus vervielfacht sich der
        # Kurs mit ein paar Leuten im Call.
        frisch(int(floaktie.START_PRICE), S=0)
        vorher = fa.price()
        for _ in range(2 * 60):
            fa._activity_tick(3, 4, streams=1)
        assert fa.price() > vorher * 8, (vorher, fa.price())
    finally:
        fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE = alt




def test_floaktie_panel_zeigt_echten_deckel():
    """Das Panel muss den WIRKLICH bindenden Deckel zeigen, nicht nur den der
    Aktivitaet. Aus dem Betrieb gemeldet: Panel sagte 'Deckel 888.674', die
    Tagesbremse stoppte den Kurs aber schon bei 52.453 - wer das nicht weiss,
    wartet stundenlang auf einen Anstieg, der heute nicht mehr kommen kann."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE, floaktie.DAY_UP)
    fa._enabled = True
    floaktie.TICK_NOISE = 0.0
    fa._today = lambda: "2026-07-27"
    # Die Tagesbremse nach oben ist im Betrieb bewusst weit offen (der Aktivitaets-
    # Deckel ist die echte Grenze). Fuer diesen Test wird sie eng gestellt, damit
    # der Fall "Tagesbremse bindet frueher" ueberhaupt eintritt.
    floaktie.DAY_UP = 50.0
    restore = _with_economy({2: 1000})
    try:
        S = 147
        base = 3421 / (1 + S / floaktie.LIQUIDITY)
        anker = base / 3.261                     # Tag startete deutlich tiefer
        fa._store = _FakeStore({"price": 3421, "base": base, "day": "x",
                                "act_ema": 29.3, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "open_day": "2026-07-27",
                                "open_base": anker,
                                "holdings": {"1": 137, "2": 8, "3": 2},
                                "history": [], "ticks": []})
        fa._sync_price()

        # Der Aktivitaets-Deckel liegt hoch, die Tagesbremse VIEL tiefer.
        akt_deckel = fa.akt_deckel_base()
        _lo, tag_hi = fa._tages_band()
        assert tag_hi is not None and tag_hi < akt_deckel
        assert fa._deckel_base() == tag_hi          # der kleinere gewinnt
        assert fa._deckel_grund() == "tag"

        # Der Kurs bleibt wirklich dort stehen - auch nach Stunden Vollgas.
        for _ in range(12 * 60):
            fa._activity_tick(10, 25, streams=5, video=1)
        gedeckelt = fa.price()
        assert gedeckelt <= fa._deckel_kurs() * 1.001, (gedeckelt, fa._deckel_kurs())
        assert gedeckelt < akt_deckel               # der Aktivitaets-Deckel war Fiktion

        # Und das Panel sagt es: Tagesdeckel, samt Ausblick auf morgen.
        panel = _embed_text(fa._panel_embed(SimpleNamespace(id=2)))
        assert "Tagesdeckel erreicht" in panel, panel
        assert "Mitternacht" in panel

        # Solange noch Luft nach oben ist, markiert das Panel das Tempo-Limit -
        # sonst wundert man sich, warum mehr Leute nichts mehr beschleunigen.
        fa._store.data["base"] = fa._deckel_base() / 100.0
        fa._sync_price()
        luft = _embed_text(fa._panel_embed(SimpleNamespace(id=2)))
        assert "(Anschlag)" in luft, luft
        assert "Tagesdeckel erreicht" not in luft

        # Fuer den naechsten Teil wieder auf den gedeckelten Stand zurueck.
        fa._store.data["base"] = fa._deckel_base()
        fa._sync_price()
        gedeckelt = fa.price()

        # Neuer Tag -> neuer Anker, jetzt bindet die Aktivitaet.
        fa._today = lambda: "2026-07-28"
        fa._tages_anker()
        assert fa._deckel_grund() in ("aktivitaet", "tag")
        for _ in range(24 * 60):
            fa._activity_tick(10, 25, streams=5, video=1)
        assert fa.price() > gedeckelt               # es geht weiter
    finally:
        (fa._store, fa._enabled, fa._today, floaktie.TICK_NOISE,
         floaktie.DAY_UP) = alt
        restore()




def test_floaktie_impuls_respektiert_deckel():
    """REGRESSION (Gegenpruefung des Umbaus): Der Sofort-Impuls _puls() kannte den
    Aktivitaets-Deckel NICHT - er lief nur durch _clamp_base (Boden, MAX_PRICE,
    Tagesbremse). Mit Rein-/Raus-Springen aus dem Call liess sich der Kurs damit am
    Deckel vorbei bis an die Tagesbremse treiben: gemessen Deckel 112.000, erreicht
    wurden 250.000 - und weil die Tagesbremse pro KALENDERTAG verankert wird, ging
    das jede Nacht von vorn (ueber 7 Tage Faktor 1.593 statt 22)."""
    import time as _t
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._today)
    fa._enabled = True
    try:
        def frisch(tag="2026-07-26"):
            fa._store = _FakeStore({"price": 5000, "base": 5000.0, "day": "x",
                                    "act_ema": 3.0, "msg_count": 0, "last_msg_count": 0,
                                    "leer_min": 0.0, "holdings": {}, "history": [],
                                    "ticks": []})
            fa._today = lambda: tag
            fa._tages_anker()
            fa._sync_price()

        frisch()
        deckel = fa.akt_deckel_base()
        assert deckel == fa.ziel_base(3.0) * floaktie.CEIL_FACTOR
        # Ein einzelner Impuls hebt den Kurs - das soll er auch.
        vorher = fa.price()
        assert fa._puls(floaktie.PULSE_STREAM, "test") > vorher

        # Dauer-Spam ueber 7 Tage (inkl. Mitternacht) endet AM DECKEL, nicht drueber.
        frisch()
        t0 = _t.time()
        for tag in range(7):
            fa._today = lambda t=f"2026-08-{tag + 1:02d}": t
            fa._tages_anker()
            for minute in range(7 * 60):
                fa._store.data["pulse_min"] = t0 - 61 - minute * 60
                fa._store.data["pulse_sum"] = 0.0
                for _ in range(20):
                    fa._puls(floaktie.PULSE_JOIN, "spam")
        assert fa.price() <= deckel * 1.02, (fa.price(), deckel)
        assert fa._am_deckel()
        # Am Deckel gibt der Impuls ausdruecklich 0 zurueck (kein stiller No-Op).
        assert fa._puls(floaktie.PULSE_STREAM, "test") == 0

        # Steigt die Aktivitaet, steigt der Deckel - und der Impuls zieht wieder.
        fa._store.data["act_ema"] = 12.0
        assert fa.akt_deckel_base() > deckel
        assert fa._puls(floaktie.PULSE_STREAM, "test") > 0
    finally:
        fa._store, fa._enabled, fa._today = alt




def test_webpanel_zeigt_aktien_aktivitaet():
    """Das Terminal muss LIVE zeigen, ob gerade jemand gezaehlt wird - sonst ist
    "die Aktie sinkt nicht" nicht nachpruefbar. Aktivitaet, Tempo pro Stunde und
    die Namen (ohne Bots) gehoeren in die Uebersicht."""
    import asyncio as _asyncio
    import floaktie
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    fa = floaktie.instance
    alt = (fa._store, fa._enabled, fa._zuletzt_mess, fa._zuletzt_gezaehlt)
    fa._enabled = True
    fa._store = _FakeStore({"price": 500, "base": 500.0, "day": "x",
                            "act_ema": 12.5, "msg_count": 0, "last_msg_count": 0,
                            "leer_min": 0.0, "holdings": {"1": 5},
                            "history": [], "ticks": []})
    fa._sync_price()
    fa._zuletzt_mess = (3, 1, 0, 7)
    fa._zuletzt_gezaehlt = ["Anna", "Ben", "Cem"]

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = 0
        async with TestClient(TestServer(wp._build_app())) as c:
            return await (await c.get("/api/overview")).json()

    try:
        d = _asyncio.run(lauf())
        st = d["stats"]
        assert st["floaktie_activity"] == 12.5, st.get("floaktie_activity")
        # Bei Aktivitaet steigt der Kurs - das Tempo muss positiv sein.
        assert st["floaktie_trend"] > 0, st.get("floaktie_trend")
        wer = st["floaktie_who"]
        assert "Anna" in wer and "3 im Call" in wer, wer

        # Leerer Call: Tempo negativ, und zwar mindestens die geforderten 20 %/h.
        fa._state()["act_ema"] = 0.0
        fa._zuletzt_mess = (0, 0, 0, 0)
        fa._zuletzt_gezaehlt = []
        st = _asyncio.run(lauf())["stats"]
        assert st["floaktie_activity"] == 0.0, st.get("floaktie_activity")
        assert st["floaktie_trend"] <= -20.0, st.get("floaktie_trend")
        assert "Niemand im Call" in st["floaktie_who"], st.get("floaktie_who")
    finally:
        fa._store, fa._enabled, fa._zuletzt_mess, fa._zuletzt_gezaehlt = alt




def test_tests_fassen_die_echten_daten_nicht_an():
    """Sicherung gegen den gefaehrlichsten Testfehler ueberhaupt.

    Die README schickt zum Testen nach /opt/flobot - dorthin, wo die ECHTEN
    Daten liegen. Vorher schrieb ein Lauf dort wirklich hinein (gemessen:
    ein Testkonto in economy.json, "holdings": {"1": 5} plus neuer Kurs und
    History in floaktie.json). Seitdem zeigt DATA_DIR auf einen Wegwerf-Ordner;
    dieser Test haelt das fest."""
    import pathlib
    import store
    echt = pathlib.Path(__file__).resolve().parent / "data"
    genutzt = pathlib.Path(store.DATA_DIR).resolve()
    assert genutzt != echt, f"Tests schreiben in die echten Daten: {genutzt}"
    assert not str(genutzt).startswith(str(echt)), genutzt

    # Und die Module haengen wirklich an diesem Ordner, nicht am alten.
    for modul in (economy, floaktie):
        laden = getattr(modul.instance, "_store", None)
        if laden is not None and getattr(laden, "path", None) is not None:
            assert pathlib.Path(laden.path).resolve().parent == genutzt, modul.__name__

    # Nach dem kompletten Lauf steht im echten Ordner nichts Neues von uns.
    if echt.exists():
        for datei in ("economy.json", "floaktie.json", "profil.json", "guildcfg.json"):
            pfad = echt / datei
            assert not pfad.exists() or pfad.stat().st_size > 0, pfad




def test_aktie_laesst_sich_je_server_abschalten():
    """Das Panel schreibt den Funktions-Schalter JE SERVER (features.set_guild),
    die Aktie fragte aber nur den globalen ab. Ein Server, der die Aktie im
    Panel abgeschaltet hatte, handelte munter weiter."""
    import features
    import floaktie
    alt_on, alt_in = features.is_on, features.is_on_in
    try:
        features.is_on = lambda key: True
        features.is_on_in = lambda gid, key: gid != 77
        assert floaktie.instance.is_off(77) is True, (
            "der Server-Schalter aus dem Panel wird ignoriert")
        assert floaktie.instance.is_off(88) is False
        # Ohne Server (DM) zaehlt nur der globale Schalter.
        assert floaktie.instance.is_off(None) is False
        features.is_on = lambda key: False
        assert floaktie.instance.is_off(88) is True, "der globale Not-Aus greift nicht"
    finally:
        features.is_on, features.is_on_in = alt_on, alt_in


if __name__ == "__main__":
    run(globals())
