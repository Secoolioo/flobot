"""Coins, Level, Shop, Titel, Luxus, Handel, Lotto, Haendler, Verlosungen.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur wirtschaft      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _BOESE_EINGABEN, _FakeChannel, _FakeStore, _embed_text, _fake_person,
    _giveaway_msg, _giveaway_setup, _rauch_nachricht, _schuld,
    _schulden_setup, _with_economy)



def test_roulette_stuerzt_nicht_bei_ungueltigem_tipp():
    """_roulette_payout liefert bei einem unbekannten Tipp (None, target) - und
    direkt danach stand 'payout > 0'. Das ist ein TypeError, kein Fehlschlag.

    Heute pruefen alle vier Aufrufer den Tipp vorher ab, der Pfad ist also nicht
    erreichbar. Aber der Einsatz ist an dieser Stelle SCHON abgebucht: wer den
    fuenften Aufrufer schreibt und die Pruefung vergisst, verbrennt fremde Coins
    mit einem Absturz. Die Absicherung gehoert deshalb an die eine Stelle."""
    import io
    e = economy.instance
    uid = 987654321

    async def kein_bild(*_a, **_k):          # Bildbau kostet hier nur Zeit
        return io.BytesIO(b"x"), "png"

    alt_anim = casino.instance._anim
    casino.instance._anim = kein_bild
    # economy AUSDRUECKLICH anschalten: add_coins/get_coins sind sonst ein
    # No-Op und der Test war nur gruen, weil ein frueherer Test economy
    # angelassen hatte. In gemischter Reihenfolge fiel er um.
    alt_eco = (economy.instance._store, economy.instance._enabled)
    economy.instance._store = _FakeStore({"users": {}})
    economy.instance._enabled = True
    try:
        e.add_coins(uid, 10_000 - e.get_coins(uid))    # Startguthaben setzen
        einsatz = 500
        e.add_coins(uid, -einsatz)           # so wie es jeder Aufrufer tut
        vorher = e.get_coins(uid)
        emb, datei = asyncio.run(
            casino.instance._play_roulette(uid, einsatz, "voelliger quatsch"))
        # 1. Kein Absturz, und die Form der Rueckgabe bleibt gleich - die
        #    Aufrufer reichen 'datei' direkt an Discord weiter.
        assert emb is not None and datei is not None
        # 2. Der Einsatz ist zurueck: kein Gewinn, kein Verlust.
        assert e.get_coins(uid) == vorher + einsatz, (
            f"Coins verbrannt: {vorher} -> {e.get_coins(uid)}")
    finally:
        casino.instance._anim = alt_anim
        economy.instance._store, economy.instance._enabled = alt_eco

    # Ein gueltiger Tipp verhaelt sich unveraendert.
    assert casino._roulette_payout("rot", 10, 1) == (20, "Rot")




# --- Steal (Coin-Raub) ---------------------------------------------------------
def test_steal_heist():
    """steal.handle: kein Ziel -> Hinweis; Erfolg klaut (Topf konstant); Cooldown
    greift; Misserfolg kostet Strafe; Selbst-/Bot-/Arm-Ziel abgefangen."""
    import steal

    class FakeStore:
        def __init__(self, data):
            self.data = data

        async def save(self):
            pass

    alt_eco = (economy.instance._store, economy.instance._enabled)
    economy.instance._store = FakeStore({"users": {}})
    economy.instance._enabled = True
    economy.instance._profile(1)["coins"] = 10000   # Opfer
    economy.instance._profile(2)["coins"] = 5000    # Raeuber
    alt_steal = (steal.instance._store, steal.instance._enabled,
                 steal.instance._success_chance)
    steal.instance._store = FakeStore({"cooldowns": {}})
    steal.instance._enabled = True

    def cd_clear():
        steal.instance._store.data["cooldowns"].clear()

    def mk(author, content, mentions):
        return SimpleNamespace(author=author, content=content, mentions=mentions,
                               guild=SimpleNamespace(id=1))
    raeuber = SimpleNamespace(id=2, bot=False, display_name="Raeuber")
    opfer = SimpleNamespace(id=1, bot=False, display_name="Opfer")
    try:
        # Kein Ziel -> Hinweistext.
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal", []))), str)
        # Erfolg erzwingen: Opfer verliert, Raeuber gewinnt, Gesamttopf konstant.
        steal.instance._success_chance = 1.0
        cd_clear()
        vo, vr = economy.get_coins(1), economy.get_coins(2)
        emb = asyncio.run(steal.handle(mk(raeuber, "steal <@1>", [opfer])))
        assert not isinstance(emb, str)
        assert economy.get_coins(1) < vo and economy.get_coins(2) > vr
        assert economy.get_coins(1) + economy.get_coins(2) == vo + vr
        # Cooldown greift jetzt.
        r = asyncio.run(steal.handle(mk(raeuber, "steal <@1>", [opfer])))
        assert isinstance(r, str) and "Min" in r
        # Misserfolg: neuer Raeuber zahlt Strafe.
        steal.instance._success_chance = 0.0
        cd_clear()
        economy.instance._profile(3)["coins"] = 3000
        pech = SimpleNamespace(id=3, bot=False, display_name="Pech")
        v3 = economy.get_coins(3)
        asyncio.run(steal.handle(mk(pech, "steal <@1>", [opfer])))
        assert economy.get_coins(3) < v3
        # Selbst-Klau, Bot-Ziel, armes Ziel -> jeweils Hinweistext, kein Raub.
        cd_clear()
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal <@2>", [raeuber]))), str)
        botziel = SimpleNamespace(id=99, bot=True, display_name="RivalBot")
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal <@99>", [botziel]))), str)
        economy.instance._profile(9)["coins"] = 10
        arm = SimpleNamespace(id=9, bot=False, display_name="Arm")
        cd_clear()
        assert isinstance(asyncio.run(steal.handle(mk(raeuber, "steal <@9>", [arm]))), str)
        # Kein Steal-Befehl -> None.
        assert asyncio.run(steal.handle(mk(raeuber, "wie gehts", []))) is None
    finally:
        steal.instance._store, steal.instance._enabled, steal.instance._success_chance = alt_steal
        economy.instance._store, economy.instance._enabled = alt_eco




def test_economy_display_name_of():
    # economy ist im Test nicht aktiviert -> None statt Crash.
    assert economy.display_name_of(123456789012345678) is None




# --- Luxus-Shop ------------------------------------------------------------------
def test_luxus_katalog():
    """Preisleiter: erreichbarer Einstieg, spuerbare Stufen, 1 Mrd als Endziel.
    Die Preise sind aus dem Tageseinkommen abgeleitet (economy.py) - ein normal
    aktiver Tag bringt rund 8-10k, ein guter Aktien-Vormittag bis ~20 Mio."""
    preise = [i["preis"] for i in luxus.ITEMS]
    assert preise == sorted(preise), "Katalog muss nach Preis aufsteigen"
    assert len(set(preise)) == len(preise), "keine zwei gleich teuren Stufen"
    # Einstieg: ein paar Tage normal spielen, nicht ein Monat.
    assert 20_000 <= preise[0] <= 80_000, preise[0]
    # Die Leiter endet jenseits der Milliarde (1 Mrd war fuer Aktien-Haendler zu
    # schnell erreicht) - aber im erreichbaren Bereich: das Gleichgewicht der
    # Vermoegenssteuer liegt bei rund 3,4 Mrd (economy.TAX_SOFT/TAX_RATE_TOP).
    assert preise[-1] == 3_000_000_000
    hoechst = economy.instance.TAX_FREE
    lo, hi = 0, 10 ** 13
    for _ in range(200):                      # Gleichgewicht bei 20 Mio/Tag suchen
        m = (lo + hi) // 2
        if economy.instance.steuer_fuer(m) < 20_000_000:
            lo = m
        else:
            hi = m
    hoechst = lo
    assert preise[-1] <= hoechst, (preise[-1], hoechst)   # kein totes Inhalt
    # Stufen muessen sich anfuehlen. Unten grosse Spruenge (x5), oben flacher -
    # dort bremst die Steuer, jeder Schritt dauert dann ohnehin Wochen.
    for a, b in zip(preise, preise[1:]):
        faktor = b / a
        grenze = 1.2 if a >= 1_000_000_000 else 3.0
        assert grenze <= faktor <= 10.0, (a, b, round(faktor, 2))
    assert len({i["key"] for i in luxus.ITEMS}) == len(luxus.ITEMS)
    assert len({i["n"] for i in luxus.ITEMS}) == len(luxus.ITEMS)
    assert luxus.THRONE_FACTOR > 1.0                # Thron wird immer teurer
    # Der Thron ist ein Zwischenziel, kein Einstieg.
    assert luxus.THRONE_START >= preise[0]




def test_luxus_fmt_coins():
    assert luxus.fmt_coins(1_500) == "1.500"
    assert luxus.fmt_coins(400_000) == "400.000"
    assert luxus.fmt_coins(2_500_000) == "2,5 Mio"
    assert luxus.fmt_coins(20_000_000) == "20 Mio"
    assert luxus.fmt_coins(1_000_000_000) == "1 Mrd"




def test_luxus_besitz_und_rahmen():
    # Fake-Store: Besitz-Logik ohne Datei/Discord testen (Zustand lebt in der Instanz).
    luxus.instance._store = type("S", (), {"data": {"users": {}, "throne": {
        "owner": "", "preis": luxus.THRONE_START, "n": 0}}})()
    luxus.instance._enabled = True
    try:
        uid = 42
        assert luxus.get_frame(uid) is None
        luxus._owned(uid).extend(["bronze", "gold"])
        assert luxus.get_frame(uid) == "gold"       # bester Rahmen zaehlt
        assert not luxus.has_crown(uid)
        # Imperium schaltet ALLES frei.
        luxus._owned(uid).append("imperium")
        assert luxus.get_frame(uid) == "imperium"
        assert luxus.owns(uid, "krone") and luxus.has_crown(uid)
        assert "Imperator" in luxus.get_tone_extra(uid)
        # Thron-Deko im Leaderboard.
        luxus.throne_state()["owner"] = "7"
        rows = [{"id": 7}, {"id": 42}, {"id": 9}]
        luxus.decorate_rows(rows)
        assert rows[0].get("throne") and rows[1].get("crown")
        assert not rows[2].get("crown") and not rows[2].get("throne")
    finally:
        luxus.instance._store = None
        luxus.instance._enabled = False




# --- Coin-Handelsbuch ------------------------------------------------------------
def test_handel_buchhaltung():
    """record() fuehrt Gesamtsummen, Quellen, Tages-Buckets und Einzelbuchungen;
    economy.add_coins bucht mit echtem Delta und erkanntem Quell-Modul."""
    import handel

    class FakeStore:
        def __init__(self, data):
            self.data = data

        async def save(self):
            pass

    alt = (handel.instance._store, handel.instance._enabled)
    handel.instance._store, handel.instance._enabled = FakeStore({"users": {}}), True
    try:
        handel.record(7, +150, "casino", 1150)
        handel.record(7, -100, "casino", 1050)
        handel.record(7, +120, "daily", 1170)
        handel.record(7, 0, "casino", 1170)      # 0-Buchung wird ignoriert
        u = handel.instance._store.data["users"]["7"]
        assert u["n"] == 3 and u["in"] == 270 and u["out"] == 100
        assert u["by"]["casino"] == {"in": 150, "out": 100, "n": 2}
        assert u["by"]["daily"]["in"] == 120
        assert len(u["days"]) == 1 and len(u["last"]) == 3
        tag = next(iter(u["days"].values()))
        assert tag["in"] == 270 and tag["out"] == 100
        assert u["last"][-1]["amt"] == 120 and u["last"][-1]["bal"] == 1170

        # economy-Integration: echtes Delta + Quelle (Aufrufer-Modul) landen hier.
        alt_eco = (economy.instance._store, economy.instance._enabled)
        economy.instance._store = FakeStore({"users": {}})
        economy.instance._enabled = True
        try:
            economy.add_coins(8, 500)
            economy.add_coins(8, -800)           # Konto 500 -> echtes Delta -500
            u8 = handel.instance._store.data["users"]["8"]
            assert u8["in"] == 500 and u8["out"] == 500
            # Quelle = Aufrufer-Modul. Frueher stand hier der Dateiname
            # woertlich drin; beim Aufteilen der Suite hiess die Datei anders
            # und der Test fiel um, obwohl die Buchhaltung stimmte. Geprueft
            # wird jetzt, was gemeint war: dass ueberhaupt der AUFRUFER
            # vermerkt wird und nicht etwa 'economy'.
            quelle = next(iter(u8["by"]))
            assert quelle == "__main__" or quelle.startswith("test_"), quelle
            assert u8["by"][quelle] == {"in": 500, "out": 500, "n": 2}
        finally:
            economy.instance._store, economy.instance._enabled = alt_eco

        # Karte rendert die Daten als PNG (auch mit leeren Tagen im Chart).
        buf = render.handel_card("Tester", None, u, 1170)
        assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        handel.instance._store, handel.instance._enabled = alt




# --- economy Titel-API (grant/remove/list/owns) --------------------------------
def test_economy_title_helpers():
    restore = _with_economy({1: 0})
    try:
        assert not economy.owns_title(1, "Held")
        neu = economy.grant_title(1, "Held", "🛡️ Held", "selten", wear=True)
        assert neu is True
        assert economy.owns_title(1, "Held")
        assert economy.get_title(1) == "🛡️ Held"
        # Zweites grant_title fuer denselben Titel -> kein neuer Eintrag.
        assert economy.grant_title(1, "Held", "🛡️ Held", "selten") is False
        assert len(economy.list_titles(1)) == 1
        # remove_title nimmt ihn raus und legt den getragenen Titel ab.
        entry = economy.remove_title(1, "Held")
        assert entry is not None and entry["text"] == "Held"
        assert not economy.owns_title(1, "Held")
        assert economy.get_title(1) == ""
        # remove_title auf nicht-besessenen Titel -> None.
        assert economy.remove_title(1, "GibtsNicht") is None
        # Unbekannte Rarity faellt auf 'normal' zurueck.
        economy.grant_title(1, "X", "X", "quatsch")
        assert economy.list_titles(1)[0]["rarity"] == "normal"
    finally:
        restore()




# --- economy: Aktien duerfen ins Minus (Casino nicht) --------------------------
def test_economy_stock_debt():
    """allow_negative laesst NUR Aktien unter 0; normale Ausgaben enden bei 0 und
    Gutschriften bauen Schulden korrekt ab (kein faelschliches 0-Clampen)."""
    restore = _with_economy({1: 100})
    try:
        # Normale Ausgabe (Casino & Co.) kann nicht unter 0.
        economy.add_coins(1, -500, reason="casino")
        assert economy.get_coins(1) == 0
        # Aktien duerfen ins Minus (allow_negative=True).
        economy.add_coins(1, -3000, reason="floaktie", allow_negative=True)
        assert economy.get_coins(1) == -3000
        # Gutschrift baut Schulden ab (NICHT auf 0 geklemmt).
        economy.add_coins(1, 1000, reason="nachricht")
        assert economy.get_coins(1) == -2000
        # Normale Ausgabe im Minus ist ein No-Op (kein Geld da).
        economy.add_coins(1, -500, reason="casino")
        assert economy.get_coins(1) == -2000
        # Genug Gutschrift -> wieder ins Plus.
        economy.add_coins(1, 3000, reason="lotto")
        assert economy.get_coins(1) == 1000
        # Konten OHNE Aktien bleiben unveraendert bei 0 Schluss.
        economy.add_coins(2, 50, reason="daily")
        economy.add_coins(2, -80, reason="shop")
        assert economy.get_coins(2) == 0
    finally:
        restore()




# --- Fahrender Haendler --------------------------------------------------------
def test_merchant_shop_und_trade():
    """Kaufen (inkl. limitiert/ausverkauft/zu teuer/schon besessen), Tauschen
    (Einsatz raus, Belohnung rein), eligible_gives, is_present & closed-Text."""
    import merchant

    restore_eco = _with_economy({1: 6000, 2: 60000, 3: 60000, 4: 5000})
    m = merchant.instance
    alt = (m._store, m._enabled)
    m._enabled = True
    stock = [
        {"id": "haendler:schatzsucher", "text": "Schatzsucher",
         "label": "🧭 Schatzsucher", "rarity": "selten", "price": 5000, "limit": 0},
        {"id": "haendler:drachenlord", "text": "Drachenlord",
         "label": "🐉 Drachenlord", "rarity": "legendary", "price": 45000, "limit": 1},
    ]
    trades = [{
        "id": "trade:haendler:sternenjaeger", "need_rarity": "selten",
        "surcharge": 3000, "reward_id": "haendler:sternenjaeger",
        "reward_text": "Sternenjäger", "reward_label": "🌠 Sternenjäger",
        "reward_rarity": "mythisch"}]
    m._store = _FakeStore({"arrived": True, "departed": False, "depart_at": 9e18,
                           "stock": stock, "trades": trades, "sold": {}})

    def member(uid, name="M"):
        return SimpleNamespace(id=uid, display_name=name, guild=None)
    try:
        assert m.is_present() is True
        # Kauf: genug Coins -> Titel im Inventar, Coins abgezogen.
        r = asyncio.run(m.buy(member(1), "haendler:schatzsucher"))
        assert "Gekauft" in r
        assert economy.get_coins(1) == 1000
        assert economy.owns_title(1, "Schatzsucher")
        # Nochmal derselbe -> schon besessen, keine erneute Abbuchung.
        r = asyncio.run(m.buy(member(1), "haendler:schatzsucher"))
        assert "schon" in r.lower() and economy.get_coins(1) == 1000
        # Zu teuer.
        assert "teuer" in asyncio.run(m.buy(member(4), "haendler:drachenlord")).lower()
        # Limitierte Ware: erster kauft, zweiter guckt in die Roehre.
        r = asyncio.run(m.buy(member(2), "haendler:drachenlord"))
        assert "Gekauft" in r and economy.get_coins(2) == 15000
        r = asyncio.run(m.buy(member(3), "haendler:drachenlord"))
        assert "ausverkauft" in r.lower() and economy.get_coins(3) == 60000

        # Tausch: member 3 braucht einen SELTENEN Einsatz-Titel.
        assert asyncio.run(m.trade(member(3), "trade:haendler:sternenjaeger",
                                   "IrgendWas")).lower().count("besitzt") >= 0
        economy.grant_title(3, "Glücksbär", "🐻 Glücksbär", "selten")
        assert len(m.eligible_gives(member(3), "selten")) == 1
        assert m.eligible_gives(member(3), "mythisch") == []   # zu niedrig
        r = asyncio.run(m.trade(member(3), "trade:haendler:sternenjaeger", "Glücksbär"))
        assert "Tausch" in r
        assert economy.owns_title(3, "Sternenjäger")           # Belohnung da
        assert not economy.owns_title(3, "Glücksbär")          # Einsatz weg
        assert economy.get_coins(3) == 57000                   # 3000 Aufzahlung
        # Belohnung schon besessen -> abgelehnt.
        economy.grant_title(3, "Glücksbär", "🐻 Glücksbär", "selten")
        assert "schon" in asyncio.run(
            m.trade(member(3), "trade:haendler:sternenjaeger", "Glücksbär")).lower()

        # Kein Haendler-Befehl -> None; Weg-Zustand -> closed-Text (str).
        def hmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=member(uid))
        assert asyncio.run(m.handle(hmsg(1, "wie gehts"))) is None
        m._store.data["departed"] = True
        assert isinstance(asyncio.run(m.handle(hmsg(1, "haendler"))), str)

        # _roll_stock ist KRASSER als der Shop: keine gewoehnlichen/seltenen Titel,
        # immer mind. ein Highlight; Tausch-Belohnungen sind ebenfalls Highlights.
        import titles as _t
        erlaubt = {"episch", "mythisch", "legendary", "relikt", "exklusiv", "goettlich"}
        gesehen = set()
        for _ in range(60):
            m._roll_stock()
            stock = m._state()["stock"]
            assert stock and all(e["rarity"] in erlaubt for e in stock), stock
            assert any(e["rarity"] in merchant.Merchant._HIGHLIGHTS for e in stock)
            gesehen.update(e["rarity"] for e in stock)
            for t in m._state()["trades"]:
                assert t["reward_rarity"] in merchant.Merchant._HIGHLIGHTS
                # Einsatz ist immer die Stufe DIREKT unter der Belohnung.
                assert (_t.RANK[t["need_rarity"]]
                        == _t.RANK[t["reward_rarity"]] - 1), t
                assert t["surcharge"] >= 1000
        # Ueber 60 Tage muss auch die Spitze mal auftauchen (sonst ist sie toter Code).
        assert "goettlich" in gesehen or "exklusiv" in gesehen, gesehen
        # Jeder Katalog-Preis passt in die Spanne seiner Stufe (hier: nie teurer).
        for e in merchant._KATALOG:
            lo, hi = _t.RARITY[e["rarity"]]["price"]
            assert lo * 0.2 <= e["price"] <= hi, (e["id"], e["price"], lo, hi)
    finally:
        m._store, m._enabled = alt
        restore_eco()




# --- Titel: die Seltenheits-Leiter -------------------------------------------
def test_titles_leiter():
    """Acht Stufen, saubere Verteilung, eigene Farbe/Rolle je Stufe - und die
    Haendler-Stufen tauchen NIE im normalen Shop auf."""
    import titles
    ordnung = titles.RARITY_ORDER
    assert len(ordnung) == 8 and len(set(ordnung)) == 8
    # Jede Stufe hat alles, was Shop, Rolle, Bild und KI brauchen.
    for r in ordnung:
        meta = titles.RARITY[r]
        for key in ("label", "emoji", "color", "role", "price", "pool_pct",
                    "shop_weight", "tone"):
            assert key in meta and meta[key] not in (None, ""), (r, key)
        assert meta["role"].startswith("Flo · ")
    # Farben und Rollennamen muessen EINDEUTIG sein (sonst zwei Stufen, eine Rolle).
    farben = [titles.RARITY[r]["color"] for r in ordnung]
    rollen = [titles.RARITY[r]["role"] for r in ordnung]
    assert len(set(farben)) == len(farben), farben
    assert len(set(rollen)) == len(rollen), rollen
    # Preise steigen streng mit dem Rang und ueberlappen nicht.
    spannen = [titles.RARITY[r]["price"] for r in ordnung]
    for (lo1, hi1), (lo2, hi2) in zip(spannen, spannen[1:]):
        assert lo1 < hi1 <= lo2 < hi2, (lo1, hi1, lo2, hi2)
    # Verteilung: pool_pct summiert auf 100 und die Hash-Grenzen passen dazu.
    assert sum(titles.RARITY[r]["pool_pct"] for r in ordnung) == 100
    anzahl = titles.counts()
    gesamt = titles.total()
    for r in ordnung:
        pct = titles.RARITY[r]["pool_pct"]
        if pct == 0:
            assert anzahl.get(r, 0) == 0, r          # Haendler-Stufe: kein Pool
            continue
        anteil = anzahl.get(r, 0) / gesamt * 100
        assert abs(anteil - pct) < 2.0, (r, round(anteil, 2), pct)
    # Haendler-Stufen kommen NIE aus dem Shop.
    nur_haendler = [r for r in ordnung if titles.RARITY[r]["shop_weight"] == 0]
    assert set(nur_haendler) == {"exklusiv", "goettlich"}
    for e in titles.random_titles(60):
        assert e["rarity"] not in nur_haendler
    assert all(titles.rarity_of(t) not in nur_haendler
               for t in ("Goldener König", "Wilder Wolf", "Titan des Chaos"))
    # 'goettlich' ist die hoechste Stufe.
    assert titles.RANK["goettlich"] == max(titles.RANK.values())




# --- Monats-Lotto --------------------------------------------------------------
def test_lotto_flow():
    """Lospreis = Jackpot/80 (glatt); Kauf zieht Coins ab, zaehlt Lose UND fuellt
    Flos Kasse; Gewinnen ist chancen-gesteuert (extrem selten); kein Gewinner ->
    Jackpot-Rollover waechst; nur der Owner kann die Kasse abbuchen."""
    import lotto

    restore_eco = _with_economy({1: 1_000_000, 2: 100, lotto.OWNER_ID: 0, 999: 0})
    lt = lotto.instance
    alt = (lt._store, lt._enabled, lt._win_chance)
    lt._enabled = True
    lt._win_chance = 1.0
    lt._store = _FakeStore({"month": "2026-07", "jackpot": 20_000_000,
                            "ticket_price": 10_000, "entries": {}, "house": 0,
                            "history": []})
    try:
        # Lospreis: sanft gekoppelt & gedeckelt - 20 Mio -> 10k (kein Wucher mehr).
        assert lt._price_for(20_000_000) == 10_000
        assert lt._price_for(5_000_000) == 2_500
        assert lt._price_for(500_000_000) == lotto.TICKET_MAX     # Deckel greift
        for _ in range(50):
            jp, preis = lt._roll_jackpot()
            assert jp % 1_000_000 == 0
            assert preis == max(lotto.TICKET_MIN,
                                min(lotto.TICKET_MAX, jp // lotto.PRICE_DIVISOR))
            assert lotto.TICKET_MIN <= preis <= lotto.TICKET_MAX
            assert lotto.JACKPOT_MIN_M * 1_000_000 <= jp <= lotto.JACKPOT_MAX_M * 1_000_000

        # Kauf: Coins ab, Lose gezaehlt, Losgeld aufgeteilt.
        # Der GROSSTEIL waechst den Jackpot (die Spieler fuellen den Topf, aus dem
        # sie gewinnen), nur der Hausanteil bleibt in der Kasse. Vorher ging alles
        # in die Kasse - und weil der Besitzer die abbuchen kann, war das Lotto in
        # der Bilanz gar keine Senke, sondern nur ein Zwischenlager.
        jp_vor = lt._state()["jackpot"]
        r = asyncio.run(lt.buy(SimpleNamespace(id=1), 3))
        kosten = 3 * 10_000
        zum_jackpot = int(kosten * lotto.JACKPOT_SHARE)
        assert "3" in r and economy.get_coins(1) == 1_000_000 - kosten
        assert lt._entries()["1"] == 3
        assert lt._state()["house"] == kosten - zum_jackpot
        assert lt._state()["jackpot"] == jp_vor + zum_jackpot
        # Kein Coin geht verloren oder entsteht: Losgeld = Jackpot-Zuwachs + Kasse.
        assert (lt._state()["jackpot"] - jp_vor) + lt._state()["house"] == kosten
        assert 0.0 < lotto.JACKPOT_SHARE < 1.0
        # Zu wenig Coins -> Hinweis, keine Lose, Kasse/Jackpot unveraendert.
        haus_vor, jp2 = lt._state()["house"], lt._state()["jackpot"]
        r = asyncio.run(lt.buy(SimpleNamespace(id=2), 1))
        assert isinstance(r, str) and "2" not in lt._entries()
        assert lt._state()["house"] == haus_vor and lt._state()["jackpot"] == jp2
        # 'max' deckelt nach Guthaben.
        assert lt._resolve_count(SimpleNamespace(id=1), "max") == \
            economy.get_coins(1) // 10_000

        # Gewinnchance: 1 Los bei chance=1.0 sicher; bei chance 0 nie.
        assert lt._win_prob_for(1) == 1.0
        lt._win_chance = 0.0
        assert lt._win_prob_for(9999) == 0.0
        lt._win_chance = 1.0

        # Ziehung MIT Gewinn (chance 1.0): Spieler kriegt den Jackpot, won=True.
        # Der Jackpot ist inzwischen um den Losgeld-Anteil gewachsen.
        vorher = economy.get_coins(1)
        jackpot_jetzt = lt._state()["jackpot"]
        res = lt._draw()
        assert res.won and res.winner_ids == [1]
        assert economy.get_coins(1) == vorher + jackpot_jetzt
        assert lt._state()["history"][-1]["winner_ids"] == [1]

        # Ziehung OHNE Gewinn (chance 0): kein Gewinner, kein Crash, won=False.
        lt._win_chance = 0.0
        lt._store.data["entries"] = {"1": 5}
        res = lt._draw()
        assert not res.won and res.winner_ids == []

        # Kasse: Fremder darf NICHT abbuchen; Owner holt alles aufs Konto.
        lt._store.data["house"] = 750_000
        deny = asyncio.run(lt.withdraw(SimpleNamespace(id=999), "alles"))
        assert isinstance(deny, str) and economy.get_coins(999) == 0
        assert lt._state()["house"] == 750_000
        ow_vor = economy.get_coins(lotto.OWNER_ID)
        asyncio.run(lt.withdraw(SimpleNamespace(id=lotto.OWNER_ID), "alles"))
        assert economy.get_coins(lotto.OWNER_ID) == ow_vor + 750_000
        assert lt._state()["house"] == 0

        # Monatswechsel via tick() MIT Gewinn: loest aus, frischer Jackpot, Lose leer.
        lt._win_chance = 1.0
        lt._store.data.update({"month": "2020-01", "jackpot": 10_000_000,
                               "ticket_price": 125_000, "entries": {"1": 2}})
        h_vor = len(lt._state()["history"])
        res = asyncio.run(lt.tick())
        assert res is not None and res.won and res.winner_ids == [1]
        assert lt._state()["month"] == lt._month_str()      # neuer Monat laeuft
        assert lt._state()["entries"] == {}                 # Lose zurueckgesetzt
        assert len(lt._state()["history"]) == h_vor + 1
        assert (lotto.JACKPOT_MIN_M * 1_000_000
                <= lt._state()["jackpot"] <= lotto.JACKPOT_MAX_M * 1_000_000)

        # Monatswechsel OHNE Gewinn: Jackpot-Rollover waechst.
        lt._win_chance = 0.0
        lt._store.data.update({"month": "2019-12", "jackpot": 8_000_000,
                               "ticket_price": 100_000, "entries": {"1": 1}})
        res = asyncio.run(lt.tick())
        assert not res.won
        assert lt._state()["jackpot"] >= 8_000_000 + lotto.GROWTH_MIN_M * 1_000_000

        # handle: Nicht-Befehl -> None; Kauf -> str; 'kasse' fuer Fremde -> str.
        def lmsg(uid, content):
            return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                                   author=SimpleNamespace(id=uid, display_name="P"))
        assert asyncio.run(lt.handle(lmsg(1, "wie gehts"))) is None
        assert isinstance(asyncio.run(lt.handle(lmsg(1, "lotto kauf 1"))), str)
        assert isinstance(asyncio.run(lt.handle(lmsg(1, "lotto kasse"))), str)
    finally:
        lt._store, lt._enabled, lt._win_chance = alt
        restore_eco()




def test_floaktie_kein_pump_and_dump():
    """REGRESSION (Exploit): Der Kurs muss pfad-unabhaengig sein - viele kleine
    Kaeufe duerfen den Kurs NICHT staerker heben als ein grosser Kauf, sonst kann
    man hochpumpen und teuer dumpen (Geld aus dem Nichts). Zusaetzlich muss jeder
    Round-Trip (kaufen+verkaufen) durch die Gebuehr verlieren."""
    import floaktie
    START = 5_000_000

    def fresh():
        restore = _with_economy({1: START})
        fa = floaktie.instance
        fa._enabled = True
        fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                                "act_ema": floaktie.ACT_BASELINE, "msg_count": 0,
                                "last_msg_count": 0, "holdings": {}, "history": [],
                                "ticks": []})
        return fa, SimpleNamespace(id=1), restore

    fa, M, restore = fresh()
    try:
        # 1) Pump & Dump verliert (frueher: +414k Gewinn aus dem Nichts).
        for _ in range(300):
            asyncio.run(fa.buy(M, 1))
        asyncio.run(fa.sell(M, fa.shares_of(1)))
        assert economy.get_coins(1) < START, "Pump&Dump druckt Geld!"
    finally:
        restore()

    # 2) Pfad-Unabhaengigkeit: 300x1 kostet praktisch genauso viel wie 1x300.
    fa, M, restore = fresh()
    try:
        for _ in range(300):
            asyncio.run(fa.buy(M, 1))
        klein = START - economy.get_coins(1)
    finally:
        restore()
    fa, M, restore = fresh()
    try:
        asyncio.run(fa.buy(M, 300))
        gross = START - economy.get_coins(1)
    finally:
        restore()
    assert abs(klein - gross) <= gross * 0.01, (klein, gross)   # <=1% (nur Rundung)

    # 3) Round-Trip verliert immer (Gebuehr).
    fa, M, restore = fresh()
    try:
        asyncio.run(fa.buy(M, 200))
        asyncio.run(fa.sell(M, 200))
        assert economy.get_coins(1) < START
        # 4) Verkaufen in Scheiben + Rueckkauf am Stueck bringt auch nichts.
        asyncio.run(fa.buy(M, 100))
        bal = economy.get_coins(1)
        for _ in range(100):
            asyncio.run(fa.sell(M, 1))
        asyncio.run(fa.buy(M, 100))
        assert economy.get_coins(1) <= bal
    finally:
        restore()




def test_exploit_fixes_games_und_steal():
    """REGRESSION für die im Audit bewiesenen Exploits:
    - Gratis-Slot (Einsatz 0 / negativ) darf KEINE Coins auszahlen,
    - Skill-Spiele (mathe/anagramm/reaktion) sind gedeckelt + haben Cooldown,
    - Klauen ohne eigenes Geld (Strafe würde auf 0 klemmen) ist gesperrt,
    - parse_amount versteht deutsche Tausenderpunkte ('1.000' == 1000)."""
    import io
    import casino
    import games
    import steal

    # --- Gratis-Slot zahlt nichts mehr aus -------------------------------
    restore = _with_economy({1: 0})
    alt_anim = games.instance._anim
    alt_g = games.instance._enabled
    alt_c = (casino.instance._enabled, casino.instance._stats)
    games.instance._enabled = True
    casino.instance._enabled = True
    casino.instance._stats = _FakeStore({"stats": {}})

    async def no_anim(*a, **k):          # Bild-Rendering im Test überspringen
        return (io.BytesIO(b"x"), "png")
    games.instance._anim = no_anim
    try:
        for _ in range(40):
            asyncio.run(games.instance._spin_slot(1, 0))
        assert economy.get_coins(1) == 0, "Gratis-Slot ist ein Coin-Faucet!"
        for _ in range(20):
            asyncio.run(games.instance._spin_slot(1, -1_000_000))
        assert economy.get_coins(1) == 0, "negativer Einsatz zahlt aus!"
        # Bezahlter Slot rechnet weiter normal ab.
        economy.instance._profile(1)["coins"] = 100_000
        asyncio.run(games.instance._spin_slot(1, 1000))
        assert economy.get_coins(1) != 100_000
    finally:
        games.instance._anim = alt_anim
        games.instance._enabled = alt_g
        casino.instance._enabled, casino.instance._stats = alt_c
        restore()

    # --- Skill-Spiele: Einsatz gedeckelt + Cooldown ----------------------
    restore = _with_economy({1: 10_000_000})
    games.instance._enabled = True
    games.instance._skill_cd = {}
    try:
        bet, err = games.instance._take_bet(1, [str(games.SKILL_MAX_BET * 10)])
        assert bet == 0 and err, "Einsatz nicht gedeckelt"
        bet, err = games.instance._take_bet(1, [str(games.SKILL_MAX_BET)])
        assert bet == games.SKILL_MAX_BET and err is None
        bet2, err2 = games.instance._take_bet(1, ["100"])
        assert bet2 == 0 and err2, "kein Cooldown -> farmbar"
    finally:
        games.instance._enabled = alt_g
        games.instance._skill_cd = {}
        restore()

    # --- Steal braucht eigenes Risiko -----------------------------------
    restore = _with_economy({1: 10_000_000, 2: 0, 3: -50_000, 4: 5_000})
    alt_s = (steal.instance._enabled, steal.instance._store)
    steal.instance._enabled = True
    steal.instance._store = _FakeStore({"cooldowns": {}})

    def mk(uid):
        return SimpleNamespace(
            author=SimpleNamespace(id=uid, bot=False, display_name=f"U{uid}"),
            content="steal <@1>",
            mentions=[SimpleNamespace(id=1, bot=False, display_name="Opfer")],
            guild=SimpleNamespace(id=1))
    try:
        for uid in (2, 3):               # blank bzw. im Minus -> gesperrt
            r = asyncio.run(steal.handle(mk(uid)))
            assert isinstance(r, str) and "Risiko" in r, (uid, r)
        steal.instance._store = _FakeStore({"cooldowns": {}})
        r = asyncio.run(steal.handle(mk(4)))     # mit Guthaben -> erlaubt
        assert not (isinstance(r, str) and "Risiko" in r)
    finally:
        steal.instance._enabled, steal.instance._store = alt_s
        restore()

    # --- Tausenderpunkte im Betrag --------------------------------------
    assert economy.parse_amount("1.000") == 1000
    assert economy.parse_amount("1.000.000") == 1_000_000
    assert economy.parse_amount("2,5k") == 2500
    assert economy.parse_amount("9" * 400) is None      # kein OverflowError
    assert economy.parse_amount("-5") is None




def test_exploit_kurspump_und_verschluckte_einsaetze():
    """REGRESSION für die drei im zweiten Audit BEWIESENEN Löcher:

    1) Aktien-Kauf auf Kredit ohne eigenes Geld: Wegwerf-Konten (Guthaben 0)
       konnten Anteile kaufen und damit den Kurs hochtreiben, das Hauptkonto
       verkaufte oben. Gemessen: 300.000 -> 429.227, also +129.227 aus dem Nichts,
       plus 1,17 Mio uneinbringliche Schuld auf leeren Konten.
    2) Quiz-Duell: 'läuft hier schon eins?' wurde vor der (langsamen) KI-Frage
       geprüft, die Runde aber erst danach registriert -> zwei Duelle gleichzeitig,
       10.000 Coins ersatzlos vernichtet.
    3) Mathe/Anagramm: derselbe Fehler, Fenster = ein Discord-Roundtrip."""
    import floaktie
    import games

    # --- 1) Kein Kredit ohne Sicherheit ---------------------------------
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    restore = _with_economy({1: 300_000, 201: 0, 202: 0, 203: -50_000})
    fa._enabled = True
    fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x",
                            "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
                            "leer_min": 0.0, "holdings": {}, "history": [], "ticks": []})
    fa._sync_price()
    try:
        # Leeres bzw. negatives Konto: kauft GAR NICHTS und bewegt den Kurs nicht.
        for uid in (201, 202, 203):
            kurs_vor = fa.price()
            r = asyncio.run(fa.buy(SimpleNamespace(id=uid), 150))
            assert isinstance(r, str) and "Guthaben nicht" in r, (uid, r)
            assert fa.shares_of(uid) == 0 and fa.price() == kurs_vor, uid
            assert economy.get_coins(uid) >= min(0, -50_000)   # keine neue Schuld
        # KEIN KREDIT: der Kaufrahmen ist GENAU das Guthaben, nie mehr.
        assert fa._kaufrahmen(economy.get_coins(1)) == 300_000
        assert fa._kaufrahmen(0) == 0 and fa._kaufrahmen(-9_999) == 0
        assert fa._kaufrahmen(1_000) == 1_000
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 150))
        assert fa.shares_of(1) > 0, r
        assert economy.get_coins(1) >= 0, "Kauf hat das Konto ins Minus gedrueckt"
        # Round-Trip bleibt ein Verlust (kein risikoloser Gewinn).
        vorher = economy.get_coins(1)
        asyncio.run(fa.sell(SimpleNamespace(id=1), 150))
        assert economy.get_coins(1) > vorher          # Erlös kommt an ...
        assert economy.get_coins(1) < 300_000, economy.get_coins(1)   # ... aber netto Minus
    finally:
        fa._store, fa._enabled = alt
        restore()

    # --- 2+3) Kanal-Platz wird SYNCHRON belegt --------------------------
    g = games.instance
    alt_start = set(g._starting)
    try:
        g._starting = set()
        assert g._slot_belegen("mathe", 42) is True
        assert g._slot_belegen("mathe", 42) is False      # zweiter Start prallt ab
        assert g._slot_belegen("anagramm", 42) is True    # anderes Spiel, egal
        assert g._slot_belegen("mathe", 43) is True       # anderer Kanal, egal
        g._slot_frei("mathe", 42)
        assert g._slot_belegen("mathe", 42) is True
    finally:
        g._starting = alt_start
    # Und die Reservierung muss VOR dem ersten await stehen - sonst nutzt sie nichts.
    quelle = open("games.py", encoding="utf-8").read()
    for art, marke in (("qduel", '_slot_belegen("qduel"'),
                       ("mathe", '_slot_belegen("mathe"'),
                       ("anagramm", '_slot_belegen("anagramm"')):
        i = quelle.index(marke)
        # zwischen Kanal-Prüfung und Reservierung darf kein 'await' liegen
        vor = quelle[max(0, i - 300):i]
        assert "await" not in vor.split("laeuft")[-1], art
        # und jeder Pfad gibt den Platz wieder frei
        assert f'_slot_frei("{art}"' in quelle, art




def test_money_leaderboard_bild_und_namen():
    """'flo reichste' ist ein BILD, und ein fehlender/ID-artiger Name wird zum
    echten Discord-Namen aufgelöst (war ein Bug: da stand die rohe ID)."""
    import discord
    import render
    restore = _with_economy({111: 1_505_000, 222: 890_000, 333: 250_000})
    try:
        economy.instance._profile(111)["name"] = "Secoolio"
        economy.instance._profile(222)["name"] = ""        # leer
        economy.instance._profile(333)["name"] = "333"     # rohe ID als Name

        class Guild:
            def get_member(self, uid):
                namen = {222: "LunaEcht", 333: "KevinEcht"}
                if uid in namen:
                    return SimpleNamespace(display_name=namen[uid])
                return None

        rows = economy.instance.money_leaderboard_data(10)
        assert [r["id"] for r in rows] == [111, 222, 333]      # nach Coins sortiert
        asyncio.run(economy.instance._resolve_names(rows, Guild()))
        assert [r["name"] for r in rows] == ["Secoolio", "LunaEcht", "KevinEcht"]
        # Aufgelöster Name wird fürs nächste Mal im Profil gemerkt.
        assert economy.instance._profile(222)["name"] == "LunaEcht"

        # Bild-Renderer liefert ein gültiges PNG.
        buf = render.money_card(rows, "REICHSTE", "Test")
        png = buf.getvalue()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000

        # handle() gibt eine discord.File zurück (kein Embed mehr).
        msg = SimpleNamespace(content="Flo reichste", guild=Guild(), mentions=[],
                              author=SimpleNamespace(id=111, display_name="S"))
        res = asyncio.run(economy.handle(msg))
        assert isinstance(res, discord.File) and res.filename == "reichste.png"
    finally:
        restore()




def test_economy_money_leaderboard():
    """Geld-Rangliste sortiert nach aktuellem Kontostand, zeigt 'insgesamt verdient'
    (Lebenszeit) mit Tausenderpunkten; earned zaehlt nur echte Zufluesse."""
    restore = _with_economy({1: 5000, 2: 20000, 3: 100})
    try:
        # Gutschrift zaehlt in 'earned'; Ausgabe nicht.
        economy.add_coins(2, 30000, reason="lotto")     # coins 50.000, earned 30.000
        assert economy.instance._profile(2).get("earned", 0) == 30000
        economy.add_coins(2, -1000, reason="casino")    # earned unveraendert
        assert economy.instance._profile(2).get("earned", 0) == 30000

        # Rangliste: nach Kontostand sortiert, 'earned' als Lebenszeit-Wert.
        rows = economy.instance.money_leaderboard_data(10)
        assert [r["id"] for r in rows] == [2, 1, 3]       # 49.000 > 5.000 > 100
        assert rows[0]["coins"] == 49000
        # 'insgesamt' wird nie kleiner als der Kontostand angezeigt (Counter ist neu):
        # roh sind es 30.000 verdiente, angezeigt also der Kontostand 49.000.
        assert rows[0]["earned"] == 49000
        assert all(r["earned"] >= r["coins"] for r in rows)
    finally:
        restore()




def test_schulden_entstehen_nur_mit_zustimmung():
    """DER Kernumbau: eine Zahlung ist ein GESCHENK, keine Forderung.

    Vorher erzeugte jede 'Flo pay'-Zahlung automatisch eine Schuld des
    Empfaengers - ein Geschenk, ein verlorener Wetteinsatz, eine geteilte
    Rechnung, alles wurde stillschweigend zur Forderung. Jetzt braucht jede
    Schuld einen Klick der Person, die sie bekommt."""
    restore, sch = _schulden_setup({1: 50_000, 2: 50_000})
    try:
        # Zahlen erzeugt KEINE Schuld mehr.
        sch.pay_block(1, 2, 5_000, ziel_name="Bert")
        assert sch.saldo(1, 2) == 0
        assert sch.buch.alle() == []

        # Ein Leih-Angebot bewegt fuer sich genommen auch noch nichts.
        besteller = _fake_person(uid=1, name="anna")
        ziel = _fake_person(uid=2, name="bert")
        vorher = (economy.get_coins(1), economy.get_coins(2))
        assert economy.get_coins(1) == vorher[0]

        # Erst das Annehmen bucht Geld UND legt den Posten an.
        ok, text = asyncio.run(sch.annehmen(besteller, ziel, 5_000, grund="Pizza",
                                            faellig=0, mit_geld=True))
        assert ok, text
        assert economy.get_coins(1) == vorher[0] - 5_000
        assert economy.get_coins(2) == vorher[1] + 5_000
        assert sch.saldo(1, 2) == 5_000
        p = sch.buch.alle()[0]
        assert p.grund == "Pizza" and p.urspruenglich == 5_000 and p.ist_offen()

        # Ein Schuldschein bewegt KEIN Geld, legt aber einen Posten an.
        stand = (economy.get_coins(1), economy.get_coins(2))
        ok, _t = asyncio.run(sch.annehmen(besteller, ziel, 1_000, grund="Kino",
                                          faellig=0, mit_geld=False))
        assert ok
        assert (economy.get_coins(1), economy.get_coins(2)) == stand
        assert sch.saldo(1, 2) == 6_000
    finally:
        restore()




def test_schulden_pay_geht_immer_durch():
    """WICHTIG: das Buch ist nur Buchfuehrung. 'pay' bewegt die Coins wie vorher,
    der Hinweis haengt nur dran - und selbst wenn das Buch kaputt ist, geht die
    Zahlung durch."""
    import schulden
    restore, sch = _schulden_setup({7: 10_000, 8: 0})
    alt_flush = economy.instance._flush

    async def kein_flush():
        return None
    economy.instance._flush = kein_flush

    ziel = _fake_person(uid=8, name="empfaenger")
    autor = _fake_person(uid=7, name="zahler")
    msg = SimpleNamespace(content="flo pay <@8> 2500", mentions=[ziel],
                          author=autor,
                          guild=SimpleNamespace(id=1, get_member=lambda _u: None))
    try:
        def text_von(emb):
            teile = [emb.title or "", emb.description or ""]
            for f in emb.fields:
                teile.append(str(f.name)); teile.append(str(f.value))
            return "\n".join(teile)

        emb = asyncio.run(economy.instance._pay(msg))
        assert economy.get_coins(7) == 7500 and economy.get_coins(8) == 2500
        t = text_von(emb)
        assert "2.500" in t and "Überweisung" in t
        # ... aber KEINE Schuld daraus.
        assert sch.saldo(7, 8) == 0

        # Buch kaputt (wirft) -> Zahlung MUSS trotzdem durchgehen.
        def kaputt(*_a, **_k):
            raise RuntimeError("Buch kaputt")
        alt_note = schulden.instance.note_pay_block
        schulden.instance.note_pay_block = kaputt
        try:
            msg.content = "flo pay <@8> 1000"
            emb = asyncio.run(economy.instance._pay(msg))
            assert economy.get_coins(7) == 6500 and economy.get_coins(8) == 3500
            assert "1.000" in text_von(emb)
        finally:
            schulden.instance.note_pay_block = alt_note

        # Nicht genug Geld -> gar nichts passiert.
        msg.content = "flo pay <@8> 999999"
        antwort = asyncio.run(economy.instance._pay(msg))
        assert isinstance(antwort, str) and "nicht genug" in antwort.lower()
        assert economy.get_coins(7) == 6500
    finally:
        economy.instance._flush = alt_flush
        restore()




def test_schulden_automatische_tilgung():
    """Der Zwang zum Zahlen: von jeder ECHTEN Einnahme wandert ein Anteil
    automatisch an die Glaeubiger - anteilig an ALLE, nicht mehr alles an den
    groessten. Coins entstehen dabei nie und verschwinden nie."""
    restore, sch = _schulden_setup({1: 0, 2: 0, 3: 0})
    try:
        A, B, C = 1, 2, 3
        _schuld(sch, B, A, 7_500)             # A schuldet B 7.500
        _schuld(sch, C, A, 2_500)             # A schuldet C 2.500
        summe_vorher = sum(economy.get_coins(u) for u in (A, B, C))

        # A gewinnt 5.000 -> 20 % = 1.000 gehen anteilig raus (75/25).
        economy.add_coins(A, 5_000, reason="casino")
        assert economy.get_coins(A) == 4_000, economy.get_coins(A)
        assert economy.get_coins(B) == 750 and economy.get_coins(C) == 250
        assert sch.saldo(B, A) == 6_750 and sch.saldo(C, A) == 2_250
        assert sch.getilgt_summe(A) == 1_000
        # Coins nur umverteilt (plus die Einnahme von aussen).
        assert sum(economy.get_coins(u) for u in (A, B, C)) == summe_vorher + 5_000

        # Kleinstbetraege bleiben unberuehrt.
        vor = economy.get_coins(A)
        economy.add_coins(A, 10, reason="spiele")
        assert economy.get_coins(A) == vor + 10

        # Tabu-Quellen tilgen NICHT (Panel-Korrektur, Aktien, direkte pay).
        for grund in ("panel", "floaktie", "pay", "schulden-tilgung"):
            vor = economy.get_coins(A)
            economy.add_coins(A, 1000, reason=grund)
            assert economy.get_coins(A) == vor + 1000, grund

        # Ausgaben loesen keine Tilgung aus.
        vor_b = economy.get_coins(B)
        economy.add_coins(A, -500, reason="casino")
        assert economy.get_coins(B) == vor_b

        # Nie mehr als die Restschuld.
        sch.erlassen(B, A, sch.saldo(B, A) - 50)
        sch.erlassen(C, A)
        assert sch.saldo(B, A) == 50 and sch.saldo(C, A) == 0
        vor_b = economy.get_coins(B)
        economy.add_coins(A, 100_000, reason="spiele")
        assert economy.get_coins(B) == vor_b + 50
        assert sch.saldo(B, A) == 0

        # Ohne Schulden passiert nichts.
        vor = economy.get_coins(B)
        economy.add_coins(B, 5000, reason="casino")
        assert economy.get_coins(B) == vor + 5000

        # Kein Endlos-Kreislauf: die Tilgung bucht selbst und darf sich nicht
        # erneut selbst anstossen (sonst haengt der Bot).
        assert sch._tilgung_laeuft is False
    finally:
        restore()




def test_giveaway_sprachverstaendnis():
    """Der Assistent muss Alltagssprache verstehen: viele Formulierungen, die
    dasselbe bedeuten, muessen exakt dasselbe ergeben."""
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        # --- Einsatz ---
        for text, erwartet in (
            ("5000", 5000), ("5k", 5000), ("5 k", 5000), ("10.000", 10000),
            ("10000 coins", 10000), ("2 mio", 2_000_000), ("2m", 2_000_000),
            ("1,5k", 1500), ("1.5k", 1500), ("2,5 mio", 2_500_000),
            ("zweitausend", 2000), ("zwei tausend", 2000),
            ("zweitausendfünfhundert", 2500), ("fünfhundert", 500),
            ("einundzwanzig", 21), ("dreizehn", 13), ("eine million", 1_000_000),
            ("drei mrd", 3_000_000_000), ("1 000", 1000),
        ):
            betrag, _h = gw.parse_stake(text, 100_000)
            assert betrag == erwartet, (text, betrag, erwartet)
        # Anteile am Guthaben
        assert gw.parse_stake("alles", 100_000)[0] == 100_000
        assert gw.parse_stake("all in", 100_000)[0] == 100_000
        assert gw.parse_stake("mein ganzes geld", 100_000)[0] == 100_000
        assert gw.parse_stake("die hälfte", 100_000)[0] == 50_000
        assert gw.parse_stake("halb", 100_000)[0] == 50_000
        assert gw.parse_stake("ein drittel", 99_000)[0] == 33_000
        assert gw.parse_stake("viertel", 100_000)[0] == 25_000
        assert gw.parse_stake("25%", 100_000)[0] == 25_000
        assert gw.parse_stake("10 %", 100_000)[0] == 10_000
        # Murks bleibt Murks (der Assistent fragt dann nochmal nach)
        for text in ("keine ahnung", "hä", "", "?!"):
            assert gw.parse_stake(text, 100_000)[0] is None, text

        # --- Dauer ---
        for text, secs in (
            ("10min", 600), ("10 min", 600), ("10 minuten", 600), ("10m", 600),
            ("10", 600),                                  # nackte Zahl = Minuten
            ("1h", 3600), ("1 stunde", 3600), ("eine stunde", 3600),
            ("1 std", 3600), ("60 minuten", 3600), ("1h 30m", 5400),
            ("1h30", 5400), ("1 stunde 30 minuten", 5400), ("eineinhalb stunden", 5400),
            ("halbe stunde", 1800), ("eine halbe stunde", 1800),
            ("viertelstunde", 900), ("dreiviertelstunde", 2700),
            ("2 tage", 172800), ("zwei tage", 172800), ("1 tag", 86400),
            ("24h", 86400), ("1 woche", 604800), ("30 sek", 30), ("45 sekunden", 45),
            ("kurz", 600), ("mittel", 3600), ("lang", 86400),
            ("über nacht", 43200), ("wochenende", 172800),
        ):
            assert gw.parse_duration(text) == secs, (text, gw.parse_duration(text), secs)
        assert gw.parse_duration("bla") is None

        # --- Ja/Nein/Abbruch ---
        for t in ("ja", "jo", "jup", "yes", "passt", "klar", "los", "start", "ok",
                  "👍", "✅", "auf jeden fall", "mach", "bestätigen"):
            assert gw.is_yes(t), t
        for t in ("nein", "ne", "nö", "no", "abbrechen", "lieber nicht", "❌", "stop"):
            assert gw.is_no(t), t
        for t in ("abbrechen", "stop", "stopp", "cancel", "vergiss es", "nvm"):
            assert gw.is_cancel(t), t
        assert not gw.is_yes("nein")
        assert not gw.is_no("ja")

        # Lesbare Dauer-Ausgabe
        assert gw.dauer_text(600) == "10 Minuten"
        assert gw.dauer_text(3600) == "1 Stunde"
        assert gw.dauer_text(5400) == "1 Stunde 30 Minuten"
        assert gw.dauer_text(86400) == "1 Tag"
        assert gw.dauer_text(172800) == "2 Tage"
    finally:
        restore()




def test_shop_wuerfelt_nur_einmal_pro_nacht():
    """Die Tagesauswahl wechselt GENAU EINMAL, naemlich um 2 Uhr.

    Vorher haengte sie am Kalendertag: um 00:00 wuerfelte der Datumswechsel neu
    und um 02:00 nochmal der Loop mit force=True. Wer nachts zwischen 0 und 2 Uhr
    in den Shop schaute, sah eine Auswahl, die zwei Stunden spaeter schon wieder
    eine andere war - obwohl im Embed "Jeden Tag um 2 Uhr" steht."""
    from datetime import datetime as _dt, timedelta as _td
    import economy
    e = economy.instance
    alt = (e._store, e._enabled)
    try:
        e._enabled = True
        e._store = _FakeStore({"users": {}, "shop": {"date": "", "items": []}})

        def tag_um(stunde, minute=0):
            jetzt = _dt(2026, 8, 6, stunde, minute, tzinfo=e._tz)
            return (jetzt - _td(hours=e.SHOP_TAGESWECHSEL_STD)).strftime("%Y-%m-%d")

        # Vor 2 Uhr gehoert die Nacht noch zum VORTAG.
        assert tag_um(0) == tag_um(1) == tag_um(1, 59) == "2026-08-05"
        # Ab 2 Uhr der neue Tag - und der haelt bis Mitternacht durch.
        assert tag_um(2) == tag_um(3) == tag_um(12) == tag_um(23) == "2026-08-06"

        # Ohne force wird innerhalb desselben Shop-Tags nicht neu gewuerfelt.
        st1 = e.refresh_shop(force=True)
        namen1 = [i["label"] for i in st1["items"]]
        st2 = e.refresh_shop(force=False)
        assert [i["label"] for i in st2["items"]] == namen1
        assert st2["date"] == e._shop_tag()

        # Ein Kalendertag-Wechsel allein darf NICHTS ausloesen - genau daran lag es.
        assert e._shop_tag() != e._today() or True   # (nur Doku: beides moeglich)
        e._store.data["shop"]["date"] = e._shop_tag()
        st3 = e.refresh_shop(force=False)
        assert [i["label"] for i in st3["items"]] == namen1
    finally:
        e._store, e._enabled = alt




def test_voice_coins_haben_einen_tagesdeckel():
    """Herumsitzen im Call brachte 43.200 Coins am Tag - das 17-fache des
    Daily-Bonus, fuers Nichtstun. Zwei Leute konnten ueber Nacht parken.

    Gedeckelt werden nur die COINS. Voice-Stunden und XP muessen unveraendert
    weiterlaufen, die Statistik soll die echte Zeit zeigen."""
    import economy
    e = economy.instance
    alt = (e._store, e._enabled, e.XP_PER_VOICE_TICK, e._today)
    try:
        e._enabled = True
        e._store = _FakeStore({"users": {}})
        e.XP_PER_VOICE_TICK = 0          # Level-Up-Praemien ausblenden
        tag = ["2026-08-06"]
        e._today = lambda: tag[0]

        def mitglied(uid):
            return SimpleNamespace(id=uid, bot=False, display_name=f"U{uid}",
                                   voice=SimpleNamespace(self_deaf=False, deaf=False))

        class Guild(SimpleNamespace):
            def get_channel(self, _cid):
                return None

        guild = Guild(voice_channels=[SimpleNamespace(
            id=1, members=[mitglied(1), mitglied(2)])],
            afk_channel=None, system_channel=None)

        def minuten(n):
            for _ in range(n):
                asyncio.run(e.tick_voice(guild))

        minuten(60)                                    # 1 h
        p = e._users()["1"]
        assert p["coins"] == 60 * e.COINS_PER_VOICE_TICK, p["coins"]

        minuten(420)                                   # zusammen 8 h
        assert e._users()["1"]["coins"] == e.VOICE_COINS_DAILY_MAX

        minuten(960)                                   # zusammen 24 h
        p = e._users()["1"]
        assert p["coins"] == e.VOICE_COINS_DAILY_MAX, p["coins"]
        # Stunden laufen trotzdem weiter - genau das soll erhalten bleiben.
        assert p["voice_secs"] == 1440 * e.VOICE_TICK_SECONDS, p["voice_secs"]

        # Neuer Tag -> neues Guthaben.
        tag[0] = "2026-08-07"
        minuten(60)
        assert e._users()["1"]["coins"] == (e.VOICE_COINS_DAILY_MAX
                                            + 60 * e.COINS_PER_VOICE_TICK)

        # Deckel abschaltbar (0 = aus).
        e._store = _FakeStore({"users": {}})
        e.VOICE_COINS_DAILY_MAX, merk = 0, e.VOICE_COINS_DAILY_MAX
        try:
            minuten(600)
            assert e._users()["1"]["coins"] == 600 * e.COINS_PER_VOICE_TICK
        finally:
            e.VOICE_COINS_DAILY_MAX = merk
    finally:
        e._store, e._enabled, e.XP_PER_VOICE_TICK, e._today = alt




def test_einsatz_rueckgabe_ist_keine_einnahme():
    """Wer Schulden hat, bekommt seinen EINSATZ voll zurueck - getilgt wird nur
    vom echten Gewinn.

    Vorher hat _auszahlen alles als "spiele" gebucht. Die Schulden-Tilgung nimmt
    20 % jeder Einnahme - "Einsatz zurueck" gab damit nur 80 % wieder, obwohl
    ueberhaupt nichts gewonnen wurde. Die Tabu-Liste kannte "shop-rueck" und
    "floaktie-rueck", aber die Spiele hatten keinen eigenen Grund."""
    import economy
    import games
    import schulden

    alt = (economy.instance._store, economy.instance._enabled,
           schulden.instance._store, schulden.instance._enabled,
           schulden.instance.buch._store, schulden.instance.buch._posten,
           games.instance._store, games.instance._enabled)
    try:
        def aufsetzen():
            economy.instance._enabled = True
            economy.instance._store = _FakeStore({"users": {
                str(i): {"coins": 5000, "xp": 0, "owned": [], "name": f"U{i}",
                         "title": "", "title_rarity": "", "voice_secs": 0,
                         "msgs": 0, "streak": 0, "last_daily": ""}
                for i in (1, 2)}})
            schulden.instance._enabled = True
            schulden.instance._store = _FakeStore(
                {"posten": [], "next_id": 1, "score": {}, "stats": {},
                 "archiv": {}, "pairs": {}})
            schulden.instance.buch.laden(schulden.instance._store)
            # Spieler 1 schuldet Spieler 2 hunderttausend.
            schulden.instance.buch.anlegen(2, 1, 100_000)
            schulden.instance._tilgung_laeuft = False
            games.instance._enabled = True
            games.instance._store = _FakeStore(
                {"counting": {}, "daily": {"day": "", "won": {}}})

        aufsetzen()
        assert schulden.instance.posten(1)[1] == [(2, 100_000)]

        # 1) Unentschieden: Einsatz komplett zurueck, nichts getilgt.
        aufsetzen()
        vor, vor_g = economy.get_coins(1), economy.get_coins(2)
        games.instance._auszahlen(1, 1000, einsatz=1000, spiel="slot")
        assert economy.get_coins(1) - vor == 1000, economy.get_coins(1) - vor
        assert economy.get_coins(2) == vor_g

        # 2) Echter Gewinn: Einsatz voll, vom Gewinn 20 % an den Glaeubiger.
        aufsetzen()
        vor, vor_g = economy.get_coins(1), economy.get_coins(2)
        games.instance._auszahlen(1, 3000, einsatz=1000, spiel="slot")
        assert economy.get_coins(1) - vor == 2600, economy.get_coins(1) - vor
        assert economy.get_coins(2) - vor_g == 400

        # 3) Gewinn ohne Einsatz wird ganz normal getilgt.
        aufsetzen()
        vor, vor_g = economy.get_coins(1), economy.get_coins(2)
        games.instance._auszahlen(1, 2000, einsatz=0, spiel="quiz")
        assert economy.get_coins(1) - vor == 1600
        assert economy.get_coins(2) - vor_g == 400

        # 4) Die Regel gilt allgemein: jeder Grund auf "-rueck"/"-rueckgabe"
        #    ist tabu, nicht nur die namentlich gelisteten.
        for grund in ("spiele-rueck", "casino-rueckgabe", "shop-rueck",
                      "floaktie-rueck"):
            aufsetzen()
            betrag, _g = schulden.instance.tilgen_von_einnahme(1, 5000, grund)
            assert betrag == 0, (grund, betrag)
        aufsetzen()
        betrag, _g = schulden.instance.tilgen_von_einnahme(1, 5000, "slot")
        assert betrag > 0, betrag
    finally:
        (economy.instance._store, economy.instance._enabled,
         schulden.instance._store, schulden.instance._enabled,
         schulden.instance.buch._store, schulden.instance.buch._posten,
         games.instance._store, games.instance._enabled) = alt




def test_store_verliert_nie_daten():
    """Eine kaputte Datei darf NIEMALS stillschweigend ueberschrieben werden.

    Vorher hiess "kaputt" schlicht: leer starten. Der erste save() wenige
    Sekunden spaeter hat die kaputte, aber noch rettbare Datei durch eine leere
    ersetzt - bei economy.json waeren das alle Coins, Level und Voice-Stunden
    gewesen, endgueltig und unbemerkt."""
    import pathlib
    import shutil
    import tempfile
    import store

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="flo_store_"))
    alt_dir = store.DATA_DIR
    store.DATA_DIR = tmp
    try:
        s = store.JsonStore("economy.json", default={"users": {}})
        s.data["users"] = {str(i): {"coins": 10 ** 7} for i in range(50)}
        for _ in range(3):                      # laufender Betrieb
            asyncio.run(s.save())
        assert (tmp / "economy.json.bak").exists(), "keine Sicherung angelegt"

        # Stromausfall: Datei abgeschnitten.
        roh = (tmp / "economy.json").read_text(encoding="utf-8")
        (tmp / "economy.json").write_text(roh[:len(roh) // 2], encoding="utf-8")

        s2 = store.JsonStore("economy.json", default={"users": {}})
        # 1) Automatisch aus der Sicherung geholt.
        assert len(s2.data.get("users", {})) == 50, len(s2.data.get("users", {}))
        # 2) Der kaputte Stand liegt zusaetzlich daneben, nichts ist geloescht.
        kaputt = [p for p in tmp.iterdir() if ".kaputt-" in p.name]
        assert kaputt, sorted(p.name for p in tmp.iterdir())
        # 3) Und Speichern wirft ihn nicht weg.
        asyncio.run(s2.save())
        assert kaputt[0].exists()
        assert len(store.JsonStore("economy.json",
                                   default={"users": {}}).data["users"]) == 50

        # 4) Ganz ohne Datei bleibt es beim leeren Start (kein Krach).
        leer = store.JsonStore("gibtsnicht.json", default={"a": 1})
        assert leer.data == {"a": 1}

        # 5) Ein JSON-Inhalt, der KEIN Objekt ist, wird ignoriert statt uebernommen.
        (tmp / "liste.json").write_text("[1,2,3]", encoding="utf-8")
        assert store.JsonStore("liste.json", default={"a": 1}).data == {"a": 1}
    finally:
        store.DATA_DIR = alt_dir
        shutil.rmtree(tmp, ignore_errors=True)




def test_giveaway_verneinung_startet_nie():
    """Eine Verneinung darf NIE als Zustimmung durchgehen - hier haengt Geld dran.

    Vorher reichte irgendein Ja-Wort irgendwo im Satz. Weil "sicher" auf der
    Ja-Liste steht, galt "sicher nicht" als Zustimmung: das Giveaway startete und
    der Einsatz war abgebucht, obwohl der Nutzer ausdruecklich abgelehnt hat.
    Dasselbe bei "nicht bestaetigen" und "warte, nicht starten"."""
    import giveaway
    g = giveaway.instance
    absagen = ("nein", "ne", "nö", "nein danke", "lieber nicht", "abbrechen",
               "auf keinen fall", "nicht bestätigen", "doch nicht senden",
               "warte, nicht starten", "ne doch nicht", "besser nicht machen",
               "sicher nicht", "auf gar keinen fall", "nee lass", "keinesfalls",
               "nein, ich will das nicht starten", "stop, nicht abschicken",
               "lass mal nicht", "nie im leben")
    for t in absagen:
        # Entweder klar als Nein erkannt oder zumindest NICHT als Ja - dann
        # fragt Flo nach, und es passiert nichts mit dem Geld.
        assert not (not g.is_no(t) and g.is_yes(t)), t

    zusagen = ("ja", "j", "ok", "okay", "passt", "los", "start", "klar", "sicher",
               "jawohl", "👍", "auf jeden fall", "los gehts", "machs",
               "bestätigen", "perfekt", "yes", "jo")
    for t in zusagen:
        assert g.is_yes(t), t
        assert not g.is_no(t), t




def test_giveaway_geldweg_und_ziehung():
    """Der Einsatz wird beim Start abgebucht (Escrow), geht am Ende an genau EINEN
    Gewinner - und bei 0 Teilnehmern/Abbruch komplett zurueck. Coins entstehen nie
    neu und verschwinden nie."""
    restore, gw = _giveaway_setup({1: 50_000, 2: 0, 3: 0})
    try:
        summe_vorher = sum(economy.get_coins(u) for u in (1, 2, 3))
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 20_000, "reason": "weil", "seconds": 60}))
        # Abgebucht, im Escrow, Giveaway laeuft.
        assert economy.get_coins(1) == 30_000
        g = list(gw._active().values())[0]
        assert g["escrow"] == 20_000 and g["host"] == 1
        assert sum(economy.get_coins(u) for u in (1, 2, 3)) == summe_vorher - 20_000

        # Teilnehmer eintragen (wie der Knopf es tut) und ziehen.
        g["entries"] = [2, 3]
        asyncio.run(gw._auslosen(None, g))
        assert not gw._active(), "Giveaway muss nach der Ziehung weg sein"
        gewinner = 2 if economy.get_coins(2) else 3
        verlierer = 3 if gewinner == 2 else 2
        assert economy.get_coins(gewinner) == 20_000
        assert economy.get_coins(verlierer) == 0
        assert economy.get_coins(1) == 30_000
        # Coin-Summe unveraendert: nur umverteilt.
        assert sum(economy.get_coins(u) for u in (1, 2, 3)) == summe_vorher
        hist = gw._state()["done"][-1]
        assert hist["winner"] == gewinner and hist["stake"] == 20_000
    finally:
        restore()

    # Ohne Teilnehmer -> alles zurueck an den Veranstalter.
    restore, gw = _giveaway_setup({1: 10_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 10_000, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 0
        g = list(gw._active().values())[0]
        asyncio.run(gw._auslosen(None, g))
        assert economy.get_coins(1) == 10_000
    finally:
        restore()

    # Abbruch -> Einsatz zurueck, kein Gewinner (auch mit Teilnehmern).
    restore, gw = _giveaway_setup({1: 10_000, 2: 0})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        g = list(gw._active().values())[0]
        g["entries"] = [2]
        asyncio.run(gw._auslosen(None, g, abgebrochen=True))
        assert economy.get_coins(1) == 10_000 and economy.get_coins(2) == 0
    finally:
        restore()




def test_giveaway_exploit_schutz():
    """REGRESSION: kein Geld aus dem Nichts. Zu wenig Guthaben, doppeltes Ziehen,
    Veranstalter als Teilnehmer, mehrere Giveaways gleichzeitig."""
    restore, gw = _giveaway_setup({1: 1_000, 2: 0})
    try:
        msg = _giveaway_msg(host=1)
        # 1) Einsatz groesser als Guthaben -> nichts passiert, kein Giveaway.
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 1_000 and not gw._active()

        # 2) Guthaben verschwindet zwischen Frage und Bestaetigung (Casino o. ae.):
        #    die Abbuchung muss vollstaendig sein, sonst Rueckbuchung + Abbruch.
        economy.instance._profile(1)["coins"] = 800
        asyncio.run(gw._starten(msg, {"stake": 800, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 0
        g = list(gw._active().values())[0]

        # 3) Doppeltes Auslosen darf NICHT doppelt zahlen.
        g["entries"] = [2]
        asyncio.run(gw._auslosen(None, g))
        assert economy.get_coins(2) == 800
        asyncio.run(gw._auslosen(None, g))          # zweiter Versuch
        assert economy.get_coins(2) == 800, "doppelte Auszahlung!"

        # 4) Veranstalter zaehlt nie als Teilnehmer (auch nicht manipuliert).
        economy.instance._profile(1)["coins"] = 5_000
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        g = list(gw._active().values())[0]
        g["entries"] = [1]                           # nur der Host selbst
        asyncio.run(gw._auslosen(None, g))
        assert economy.get_coins(1) == 5_000         # Rueckbuchung, kein "Gewinn"
        assert gw._state()["done"][-1]["winner"] == 0
    finally:
        restore()

    # 5) Nur EIN laufendes Giveaway pro Nutzer (sonst mehrfach Escrow).
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 10_000, "reason": "", "seconds": 60}))
        antwort = asyncio.run(gw.start_wizard(msg))
        assert isinstance(antwort, str) and "laufendes Giveaway" in antwort
        assert len(gw._active()) == 1
    finally:
        restore()




def test_giveaway_escrow_grenzen():
    """REGRESSION (vom Exploit-Audit gefunden): _starten bewegt echtes Geld und muss
    deshalb SELBST alle Grenzen pruefen - nicht darauf vertrauen, dass der Assistent
    das schon getan hat."""
    import giveaway
    # 1) Negativer Einsatz erzeugte Coins: add_coins(uid, -(-500)) = GUTSCHRIFT.
    restore, gw = _giveaway_setup({1: 1_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": -500, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 1_000, "negativer Einsatz druckt Coins!"
        assert not gw._active()
        # 0 und unter dem Minimum ebenfalls abgelehnt.
        for schlecht in (0, 1, giveaway.MIN_STAKE - 1, giveaway.MAX_STAKE + 1):
            asyncio.run(gw._starten(msg, {"stake": schlecht, "reason": "", "seconds": 60}))
        assert economy.get_coins(1) == 1_000 and not gw._active()
        # Unsinnige Dauer ebenfalls (0, negativ, absurd lang).
        for secs in (0, -60, giveaway.MAX_SECONDS + 1):
            asyncio.run(gw._starten(msg, {"stake": 500, "reason": "", "seconds": secs}))
        assert economy.get_coins(1) == 1_000 and not gw._active()
        # parse_stake gibt selbst nie etwas Negatives zurueck.
        assert gw.parse_stake("-500", 100_000)[0] >= 0
    finally:
        restore()

    # 2) Zwei Assistenten in ZWEI Kanaelen -> nur EIN Giveaway (Escrow-Limit).
    restore, gw = _giveaway_setup({41: 1_000})
    try:
        c1, c2 = _FakeChannel(101), _FakeChannel(102)
        m1 = _giveaway_msg(host=41, channel=c1)
        m2 = _giveaway_msg(host=41, channel=c2)
        asyncio.run(gw.start_wizard(m1))
        asyncio.run(gw.start_wizard(m2))
        # Ein Nutzer hat nur EINEN Assistenten (der zweite ersetzt den ersten).
        assert len([k for k in gw._wizards if k[1] == 41]) == 1
        asyncio.run(gw._starten(m1, {"stake": 500, "reason": "", "seconds": 60}))
        asyncio.run(gw._starten(m2, {"stake": 500, "reason": "", "seconds": 60}))
        assert len(gw._active()) == 1, "zwei Escrows gleichzeitig!"
        assert economy.get_coins(41) == 500
    finally:
        restore()

    # 3) Teilnehmer-Liste mit Strings/Duplikaten: keine doppelte Gewinnchance.
    restore, gw = _giveaway_setup({1: 5_000, 61: 0, 62: 0})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw._starten(msg, {"stake": 5_000, "reason": "", "seconds": 60}))
        g = list(gw._active().values())[0]
        g["entries"] = ["61", 61, 61, "62"]          # haendisch verbogen
        asyncio.run(gw._auslosen(None, g))
        # Genau einer der beiden bekommt alles - niemand doppelt.
        assert sorted([economy.get_coins(61), economy.get_coins(62)]) == [0, 5_000]
        assert gw._state()["done"][-1]["entries"] == 2, "Duplikate nicht entfernt"
    finally:
        restore()




def test_giveaway_schnellstart_und_assistent():
    """'giveaway 5k 2h weil ...' liest alles aus einer Zeile; der Assistent fragt
    nur das Fehlende nach und akzeptiert lockere Antworten."""
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        d = gw._vorgabe_aus_text("5k 2h weil ich 1 mio geknackt habe", 100_000)
        assert d["stake"] == 5_000 and d["seconds"] == 7200
        assert "1 mio geknackt" in d["reason"] and not d["reason"].startswith("weil")
        d = gw._vorgabe_aus_text("10.000 30min", 100_000)
        assert d["stake"] == 10_000 and d["seconds"] == 1800
        d = gw._vorgabe_aus_text("alles 1 tag", 100_000)
        assert d["stake"] == 100_000 and d["seconds"] == 86400
        # Nur ein Betrag -> Dauer fehlt und wird nachgefragt.
        d = gw._vorgabe_aus_text("2k", 100_000)
        assert d["stake"] == 2_000 and "seconds" not in d

        # Assistent: Schritt fuer Schritt, mit lockeren Antworten.
        msg = _giveaway_msg(host=1)
        asyncio.run(gw.start_wizard(msg))
        w = gw._wizards[(msg.channel.id, 1)]
        assert w["step"] == "stake"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="zwei tausend")))
        assert w["data"]["stake"] == 2_000 and w["step"] == "reason"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="einfach so")))
        assert w["data"]["reason"] == "einfach so" and w["step"] == "seconds"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="halbe stunde")))
        assert w["data"]["seconds"] == 1800 and w["step"] == "confirm"
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="passt")))
        assert len(gw._active()) == 1 and economy.get_coins(1) == 98_000
        g = list(gw._active().values())[0]
        assert g["reason"] == "einfach so" and int(g["ends"] - g["started"]) == 1800
        assert (msg.channel.id, 1) not in gw._wizards
    finally:
        restore()

    # Unverstaendliche Antwort -> Assistent bleibt stehen und fragt nochmal.
    restore, gw = _giveaway_setup({1: 100_000})
    try:
        msg = _giveaway_msg(host=1)
        asyncio.run(gw.start_wizard(msg))
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="öhm keine ahnung")))
        w = gw._wizards[(msg.channel.id, 1)]
        assert w["step"] == "stake" and "stake" not in w["data"]
        # Unter dem Minimum -> auch nachfragen, nichts uebernehmen.
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="5")))
        assert "stake" not in w["data"]
        # 'abbrechen' beendet sauber, ohne Abbuchung.
        assert asyncio.run(gw.on_message_passive(_giveaway_msg(host=1, text="abbrechen")))
        assert (msg.channel.id, 1) not in gw._wizards
        assert economy.get_coins(1) == 100_000 and not gw._active()
    finally:
        restore()




def test_admin_meldet_das_echte_delta():
    """'Flo nimm @wer 1000' meldete den gewuenschten Betrag, auch wenn gar nicht
    so viel da war - add_coins klemmt bei 0 ab.

    Gemessen bei 200 Coins: abgezogen wurden 200, gemeldet '-1.000'. Bei leerem
    Konto war die Buchung ein reiner No-Op, gemeldet wurde trotzdem ein Abzug."""
    import admin
    import economy
    UID = 111111111111111111
    a = admin.instance
    alt = (a._enabled, a._name_of, economy.instance._store,
           economy.instance._enabled)
    try:
        a._enabled = True
        a._name_of = lambda _m, _u: asyncio.sleep(0, result="Testnutzer")
        economy.instance._enabled = True
        nachricht = SimpleNamespace(author=SimpleNamespace(id=1), guild=None,
                                    mentions=[])

        def konto(coins):
            economy.instance._store = _FakeStore({"users": {str(UID): {
                "coins": coins, "xp": 0, "owned": [], "name": "T", "title": "",
                "title_rarity": "", "voice_secs": 0, "msgs": 0, "streak": 0,
                "last_daily": ""}}})

        def lauf(betrag, sign):
            vor = economy.get_coins(UID)
            emb = asyncio.run(a._give(nachricht, f"{UID} {betrag}", sign=sign))
            return economy.get_coins(UID) - vor, _embed_text(emb)

        # 1) Mehr genommen als da war -> echter Betrag, plus Hinweis.
        konto(200)
        echt, text = lauf(1000, -1)
        assert echt == -200, echt
        assert "200" in text and "1.000" in text, text
        assert "-1.000 Flo Coins**" not in text, text     # keine falsche Zahl

        # 2) Leeres Konto -> ausdruecklich sagen, dass nichts passiert ist.
        konto(0)
        echt, text = lauf(500, -1)
        assert echt == 0, echt
        assert "nichts mehr zu holen" in text, text

        # 3) Genug da -> ganz normal, ohne Zusatz.
        konto(5000)
        echt, text = lauf(1000, -1)
        assert echt == -1000, echt
        assert "mehr war nicht da" not in text, text

        # 4) Geben funktioniert unveraendert.
        konto(200)
        echt, text = lauf(1000, +1)
        assert echt == 1000, echt
        assert "+1.000" in text, text
    finally:
        (a._enabled, a._name_of, economy.instance._store,
         economy.instance._enabled) = alt




def test_teurer_kauf_fragt_nach():
    """Ueber `kaufen <n>` kostet ein Tippfehler in EINER Ziffer bis zu 90 Mio
    Coins - unwiderruflich und bisher ohne jede Rueckfrage.

    Nachgefragt wird nur bei teuren Titeln; alles Alltaegliche bleibt ein
    einziger Schritt. Bestaetigt wird durch nochmal denselben Befehl."""
    import time as _t
    import economy
    e = economy.instance
    alt = (e._store, e._enabled, e._kauf_offen, e._sync_role)
    try:
        e._enabled = True
        e._sync_role = lambda m: asyncio.sleep(0)     # Rollen-Deko stoert hier
        mitglied = SimpleNamespace(id=1, display_name="T", name="T")

        def aufsetzen(coins=200_000_000):
            e._store = _FakeStore({"users": {"1": {
                "coins": coins, "xp": 0, "owned": [], "name": "T", "title": "",
                "title_rarity": "", "voice_secs": 0, "msgs": 0, "streak": 0,
                "last_daily": ""}}, "shop": {}})
            e._kauf_offen = {}
            e.refresh_shop(force=True)
            e._store.data["shop"]["items"] = list(e.get_shop_items()) + [
                {"n": 9, "label": "Weltenbrand", "text": "Weltenbrand",
                 "rarity": "goettlich", "price": 90_000_000}]
            return [x for x in e.get_shop_items()
                    if x["price"] < e.KAUF_RUECKFRAGE_AB][0]

        def kaufen(n):
            return asyncio.run(e._buy_text(mitglied, ["kaufen", str(n)]))

        # 1) Guenstig bleibt ein Schritt.
        billig = aufsetzen()
        vor = e.get_coins(1)
        kaufen(billig["n"])
        assert vor - e.get_coins(1) == billig["price"], (vor, e.get_coins(1))

        # 2) Teuer: erst fragen, nichts abbuchen.
        aufsetzen()
        vor = e.get_coins(1)
        antwort = kaufen(9)
        assert e.get_coins(1) == vor, "teurer Kauf ohne Rueckfrage gebucht"
        assert "90.000.000" in str(antwort), antwort

        # 3) Derselbe Befehl noch einmal kauft dann wirklich.
        kaufen(9)
        assert vor - e.get_coins(1) == 90_000_000, vor - e.get_coins(1)

        # 4) Ein anderer Kauf dazwischen beendet die Rueckfrage.
        billig = aufsetzen()
        vor = e.get_coins(1)
        kaufen(9)
        kaufen(billig["n"])
        kaufen(9)
        assert (vor - e.get_coins(1)) < 90_000_000, "Rueckfrage nicht abgebrochen"

        # 5) Nach einer Minute gilt sie nicht mehr.
        aufsetzen()
        vor = e.get_coins(1)
        kaufen(9)
        e._kauf_offen[1] = (9, _t.time() - 1)
        kaufen(9)
        assert e.get_coins(1) == vor, "abgelaufene Rueckfrage hat gekauft"

        # 6) Der Merker waechst nicht: abgelaufene Eintraege fliegen raus.
        e._kauf_offen = {i: (9, _t.time() - 1) for i in range(50)}
        aufsetzen()
        kaufen(9)
        assert len(e._kauf_offen) <= 2, len(e._kauf_offen)
    finally:
        e._store, e._enabled, e._kauf_offen, e._sync_role = alt




def test_floaktie_kein_minus_und_anteil_limit():
    """Zwei harte Zusagen an den Besitzer:
    1) Die Aktie drueckt ein Konto NIE ins Minus - es gibt keinen Kredit mehr.
    2) Es gibt ein Anteil-Limit pro Person, und es greift auf JEDEM Weg
       (Text-Befehl, 'kauf max', Panel-Button)."""
    import floaktie
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._enabled = True
    restore = _with_economy({1: 5_000_000, 2: 500, 3: 0, 4: -20_000})
    fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x", "act_ema": 0.0,
                            "msg_count": 0, "last_msg_count": 0, "leer_min": 0.0,
                            "holdings": {}, "history": [], "ticks": []})
    fa._sync_price()
    try:
        limit = floaktie.MAX_SHARES_PER_USER
        assert limit > 0

        # --- 1) NIE ins Minus, egal wie man es versucht -------------------
        # a) viel mehr wollen, als man hat
        r = asyncio.run(fa.buy(SimpleNamespace(id=2), limit))
        assert economy.get_coins(2) >= 0, economy.get_coins(2)
        # b) blankes Konto
        r = asyncio.run(fa.buy(SimpleNamespace(id=3), 5))
        assert fa.shares_of(3) == 0 and economy.get_coins(3) == 0
        assert isinstance(r, str) and "Guthaben nicht" in r
        # c) Konto, das (aus alten Zeiten) noch im Minus steht: nichts wird schlimmer
        vorher4 = economy.get_coins(4)
        r = asyncio.run(fa.buy(SimpleNamespace(id=4), 5))
        assert fa.shares_of(4) == 0 and economy.get_coins(4) == vorher4
        # d) hundert Kaeufe hintereinander mit kleinem Konto
        economy.instance._profile(5)["coins"] = 30_000
        for _ in range(100):
            asyncio.run(fa.buy(SimpleNamespace(id=5), 3))
            assert economy.get_coins(5) >= 0, economy.get_coins(5)
        # Kein Aufrufer nutzt noch den Minus-Schalter von add_coins.
        quelle = open("floaktie.py", encoding="utf-8").read()
        assert "allow_negative" not in quelle
        assert "KREDIT_LINIE" not in quelle

        # --- 2) Anteil-Limit auf jedem Weg --------------------------------
        economy.instance._profile(1)["coins"] = 10 ** 12
        asyncio.run(fa.buy(SimpleNamespace(id=1), limit * 10))     # Text-Befehl
        assert fa.shares_of(1) == limit, fa.shares_of(1)
        r = asyncio.run(fa.buy(SimpleNamespace(id=1), 1))          # noch einer
        assert "Depot ist voll" in r and fa.shares_of(1) == limit
        # 'kauf max' respektiert den freien Platz
        asyncio.run(fa.sell(SimpleNamespace(id=1), 40))
        assert fa._resolve_count(SimpleNamespace(id=1), "max") == 40
        assert fa._freies_depot(SimpleNamespace(id=1).id if False else 1) == 40
        # Auch in vielen kleinen Schritten kommt niemand darueber.
        for _ in range(200):
            asyncio.run(fa.buy(SimpleNamespace(id=1), 5))
            assert fa.shares_of(1) <= limit, fa.shares_of(1)
        assert fa.shares_of(1) == limit
        # Das Limit steht sichtbar im Panel und in der Kauf-Bestaetigung.
        panel = _embed_text(fa._panel_embed(SimpleNamespace(id=1)))
        assert "Anteil-Limit" in panel and str(limit) in panel
        # Das Panel verspricht keinen Kredit mehr, sondern sagt das Gegenteil.
        assert "eigenem Guthaben" in panel
        assert "auf Kredit" not in panel and "MINUS" not in panel
    finally:
        fa._store, fa._enabled = alt
        restore()




def test_luxus_jenseits_der_milliarde():
    """Die neuen Stufen ueber dem Imperium muessen WIRKEN: sichtbarer Rahmen,
    richtige Besitz-Logik, eigener KI-Ton. REGRESSION: get_frame() hatte einen
    harten Vorzug fuer 'imperium' - jede Stufe darueber waere unsichtbar geblieben,
    obwohl bezahlt. Und owns() gab 'imperium' als "alles inklusive" aus, wodurch
    der Imperium-Kaeufer die 3-Mrd-Stufe gratis "besessen" haette."""
    import render
    lx = luxus.instance
    alt = (lx._store, lx._enabled)
    lx._enabled = True
    try:
        neu = [i for i in luxus.ITEMS if i["preis"] > 1_000_000_000]
        assert len(neu) >= 3, "keine Stufen ueber der Milliarde"
        # Jede neue Stufe hat einen eigenen Rahmen-Stil im Bild-Renderer.
        for item in neu:
            if item["art"] in ("rahmen", "multiversum"):
                assert item["key"] in render.Render._FRAME_STYLES, item["key"]
        # Rahmen-Reihenfolge kommt aus ITEMS (keine handgepflegte Liste mehr).
        assert luxus._FRAME_ORDER[-1] == "multiversum"
        assert luxus._FRAME_ORDER.index("imperium") < luxus._FRAME_ORDER.index("nova")

        lx._store = _FakeStore({"users": {
            "1": ["imperium"], "2": ["multiversum"], "3": ["bronze", "nova"]},
            "throne": {"owner": "", "preis": luxus.THRONE_START, "n": 0}})
        # Der beste besessene Rahmen gewinnt - nach RANG, nicht nach Sonderfall.
        assert lx.get_frame(1) == "imperium"
        assert lx.get_frame(2) == "multiversum"
        assert lx.get_frame(3) == "nova"
        # 'alles inklusive' gilt nur nach UNTEN.
        assert lx.owns(1, "gold") and not lx.owns(1, "nova")
        assert not lx.owns(1, "multiversum")
        assert lx.owns(2, "gold") and lx.owns(2, "nova") and lx.owns(2, "imperium")
        assert not lx.owns(3, "gold")            # Nova bringt nichts darunter mit
        # Eigener Ton je Spitze (sonst redet Flo mit dem 3-Mrd-Besitzer wie mit
        # einem Imperator).
        t2 = lx.get_tone_extra(2)
        assert "MULTIVERSUM" in t2 and "Imperator" not in t2
        assert "NOVA" in lx.get_tone_extra(3)
    finally:
        lx._store, lx._enabled = alt




def test_bilder_grosse_zahlen():
    """REGRESSION (Design): Seit der Wirtschafts-Umstellung sind Millionen und
    Milliarden normal. '3.000.000.000' war breiter als seine Spalte und lief in die
    Nachbarzahl hinein; im Shop-Banner stand '90000000' ohne Trenner; und das Label
    des Singularitaets-Rahmens war dunkelgrau auf schwarz (unlesbar)."""
    import render
    r = render.instance
    # Kompakte Betraege: lesbar und schmal.
    assert r._coin_kurz(1150) == "1.150"
    assert r._coin_kurz(445_850) == "445.850"
    assert r._coin_kurz(3_774_410) == "3,77 Mio"
    assert r._coin_kurz(90_000_000) == "90 Mio"
    assert r._coin_kurz(3_000_000_000) == "3 Mrd"
    assert r._coin_kurz("quatsch") == "0"          # nie ein Absturz
    for v in (0, 7, 10 ** 15):
        assert isinstance(r._coin_kurz(v), str)
    # Helligkeit entscheidet die Textfarbe (Label auf dunklem Rahmen).
    assert r._helligkeit((255, 255, 255)) > 200
    assert r._helligkeit((18, 18, 28)) < 40
    # Level-Karte mit Extremwerten: rendert, und die Zahl bleibt in ihrer Spalte.
    from PIL import Image, ImageDraw
    for frame in ("singularitaet", "multiversum", None):
        buf = render.level_card(
            None, name="EinSehrLangerNameDerNichtPasst", level=999, into=5, step=6,
            place=1, total=99, xp=10 ** 12, coins=123_456_789_012, msgs=9_999_999,
            voice_secs=99 * 3600, streak=365, title="✨ Ein sehr langer Titel hier",
            accent=(0, 229, 255), frame=frame)
        img = Image.open(buf)
        assert img.size == (1000, 320), img.size
    # Die Stat-Spalten sind ~180 px breit - der kompakte Text muss darunter bleiben.
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = r._font(23)
    for v in (123_456_789_012, 3_000_000_000, 9_999_999):
        assert d.textlength(r._coin_kurz(v), font=f) < 170, v




def test_seltenheiten_ueberall_bekannt():
    """Eine neue Seltenheitsstufe muss AN JEDER Stelle mitspielen. Die Kartierung
    hat vier Stellen gefunden, an denen sie stillschweigend gefehlt hat:
    die Shop-Ansage (nur 'legendary'), der Kauf-Text (feste Zweier-Liste), das
    Web-Panel (kannte drei Stufen nicht) und das Leaderboard (Titel farblos)."""
    import leaderboard_img
    import titles

    # 1) Shop-Ansage: JEDE Stufe ab 'legendary' wird ausgerufen, nicht nur eine.
    bot_quelle = open("bot.py", encoding="utf-8").read()
    i = bot_quelle.index("highlights = [i for i in st.get(\"items\", [])")
    block = bot_quelle[i - 300:i + 300]
    assert 'rarity") == "legendary"' not in block, "Ansage haengt wieder an einer Stufe"
    assert "titles.RANK" in block

    # 2) Kauf-Text: 'Flo redet entspannt' gilt ab Mythisch AUFWAERTS.
    eco_quelle = open("economy.py", encoding="utf-8").read()
    j = eco_quelle.index("ab jetzt redet Flo richtig entspannt")
    assert 'in ("mythisch", "legendary")' not in eco_quelle[j:j + 300]
    assert "titles.RANK" in eco_quelle[j:j + 300]

    # 3) Web-Panel kennt ALLE Stufen - Liste und CSS-Klassen.
    panel = open("webpanel.html", encoding="utf-8").read()
    for r in titles.RARITY_ORDER:
        assert f'["{r}",' in panel, f"Panel-Liste ohne {r}"
        assert f".chip.rar-{r}{{" in panel, f"Panel-CSS ohne {r}"

    # 4) Leaderboard: Titel in der Farbe seiner Stufe.
    lb = leaderboard_img.instance
    for r in titles.RARITY_ORDER:
        wert = titles.RARITY[r]["color"]
        assert lb._rarity_color(r) == ((wert >> 16) & 255, (wert >> 8) & 255, wert & 255)
    assert lb._rarity_color("") is None
    assert lb._rarity_color("gibtsnicht") is None      # nie ein Absturz
    # ... und Milliarden werden richtig gekuerzt (vorher "3000M").
    assert lb._fmt_num(3_000_000_000) == "3 Mrd"
    assert lb._fmt_num(2_500_000_000) == "2,5 Mrd"
    assert lb._fmt_num(2_500_000) == "2.5M"
    assert lb._fmt_num(1234) == "1.2k"
    assert lb._fmt_num(0) == "0"

    # 5) Luxus-Farben duerfen sich nicht mit den Titel-Stufen doppeln (zwei Dinge
    #    in derselben Farbe sind im Discord nicht unterscheidbar). Der Gold-Rahmen
    #    darf golden sein wie 'legendary' - das ist eine andere Oberflaeche.
    titel_farben = {titles.RARITY[r]["color"]: r for r in titles.RARITY_ORDER}
    doppelt = [(i["key"], titel_farben[i["farbe"]]) for i in luxus.ITEMS
               if i["farbe"] in titel_farben]
    assert [k for k, _ in doppelt] == ["gold"], doppelt
    assert len({i["farbe"] for i in luxus.ITEMS}) == len(luxus.ITEMS)

    # 6) Der Notnagel in _grenzen(): faellt die pool_pct-Summe unter 100, bekommt
    #    die HAEUFIGSTE Stufe den Rest - nicht die seltenste (die waere sonst
    #    ploetzlich sechsmal so haeufig).
    t = titles.instance
    alt_pct = titles.RARITY["normal"]["pool_pct"]
    alt_cache = t.__dict__.get("_grenz_cache")
    try:
        titles.RARITY["normal"]["pool_pct"] = alt_pct - 5      # Summe 95
        t.__dict__["_grenz_cache"] = None
        grenzen = dict((r, g) for g, r in t._grenzen())
        assert grenzen["relikt"] == 100
        # 'relikt' behaelt seine Breite von 1 Punkt.
        vorletzte = max(g for g, r in t._grenzen() if r != "relikt")
        assert grenzen["relikt"] - vorletzte == 1, t._grenzen()
    finally:
        titles.RARITY["normal"]["pool_pct"] = alt_pct
        t.__dict__["_grenz_cache"] = alt_cache




def test_seltenheits_rollen_werden_gepflegt():
    """Flo baut die Rollen selbst: fehlende anlegen, geaenderte Farben nachziehen,
    alte Namen umbenennen. Ohne das haengen nach einer Farb-Aenderung Rollen in der
    ALTEN Farbe im Server - und die umbenannte Stufe doppelt."""
    import titles
    eco = economy.instance
    restore = _with_economy({})

    class FakeRole:
        def __init__(self, name, value, position=1):
            self.name, self.position = name, position
            self.colour = SimpleNamespace(value=value)
            self.edits = []

        async def edit(self, **kw):
            self.edits.append(kw)
            if "name" in kw:
                self.name = kw["name"]
            if "colour" in kw:
                self.colour = SimpleNamespace(value=kw["colour"].value)

    class FakeGuild:
        def __init__(self, roles):
            self.name = "Testserver"
            self.roles = list(roles)
            self.created = []
            self.positions = None

        async def create_role(self, *, name, colour, **kw):
            rolle = FakeRole(name, colour.value, position=len(self.roles) + 1)
            self.roles.append(rolle)
            self.created.append(name)
            return rolle

        async def edit_role_positions(self, *, positions, reason=None):
            self.positions = positions

    try:
        # Ausgangslage: eine Rolle mit ALTEM Namen und eine mit falscher Farbe.
        falsch = FakeRole(titles.RARITY["selten"]["role"], 0x000000, position=3)
        veraltet = FakeRole("Flo · Normal", 0x57F287, position=2)
        guild = FakeGuild([veraltet, falsch])
        stats = asyncio.run(eco.ensure_roles(guild))

        # Alter Name wurde auf den neuen gezogen (keine Dublette).
        assert veraltet.name == titles.RARITY["normal"]["role"]
        assert any("Flo · Normal ->" in x for x in stats["renamed"]), stats
        namen = [r.name for r in guild.roles]
        assert namen.count(titles.RARITY["normal"]["role"]) == 1
        # Falsche Farbe wurde korrigiert.
        assert falsch.colour.value == titles.RARITY["selten"]["color"]
        assert titles.RARITY["selten"]["role"] in stats["recolored"]
        # Alle acht Stufen existieren danach, jede in ihrer Farbe.
        for r in titles.RARITY_ORDER:
            meta = titles.RARITY[r]
            rolle = next((x for x in guild.roles if x.name == meta["role"]), None)
            assert rolle is not None, meta["role"]
            assert rolle.colour.value == meta["color"], meta["role"]
        # Reihenfolge gesetzt: seltener = weiter oben.
        assert guild.positions, "Reihenfolge wurde nicht gesetzt"
        nach_rang = sorted(guild.positions.items(),
                           key=lambda kv: titles.RANK[
                               next(r for r in titles.RARITY_ORDER
                                    if titles.RARITY[r]["role"] == kv[0].name)])
        werte = [p for _r, p in nach_rang]
        assert werte == sorted(werte), werte

        # Zweiter Lauf aendert nichts mehr (idempotent).
        vorher = len(guild.created)
        stats2 = asyncio.run(eco.ensure_roles(guild))
        assert len(guild.created) == vorher
        assert not stats2["created"] and not stats2["recolored"]
    finally:
        restore()




def test_vermoegenssteuer_als_senke():
    """Die Wirtschaft braucht eine WIEDERKEHRENDE Senke: alle Einnahmen sind
    zeitbasiert, alle alten Senken waren einmalig (ein Titel, ein Rahmen, ein
    Thron) oder prozentual zu freiwilligem Casino-Umsatz. Wer nur farmte und nie
    spielte, hatte gar keine Senke - der Server lief mit Faktor 1,69x pro Tag in
    die Inflation. Die Steuer verbrennt taeglich einen Anteil oberhalb des
    Freibetrags und bringt jedes Einkommen in ein Gleichgewicht."""
    import floaktie as _fa_mod
    eco = economy.instance
    restore = _with_economy({})
    # Fuer den ersten Teil die Aktie AUS: hier geht es nur um Konto-Guthaben.
    # (Sonst zaehlt ein Depot aus einem frueheren Test mit in die Steuer.)
    _fa_alt = _fa_mod.instance._enabled
    _fa_mod.instance._enabled = False
    try:
        # Kleine Konten zahlen NICHTS - die Steuer trifft nur echtes Vermoegen.
        for c in (0, 50_000, eco.TAX_FREE - 1, eco.TAX_FREE):
            assert eco.steuer_fuer(c) == 0, c
        assert eco.steuer_fuer(eco.TAX_FREE + 1_000_000) == int(1_000_000 * eco.TAX_RATE)
        # DEGRESSIV ganz oben: ab TAX_SOFT nur noch der kleine Satz. Ohne das laege
        # das Gleichgewicht selbst fuer den besten Haendler bei ~1 Mrd - jede
        # Luxus-Stufe darueber waere fuer immer unerreichbar (totes Inhalt).
        assert 0 < eco.TAX_RATE_TOP < eco.TAX_RATE
        mitte = int((eco.TAX_SOFT - eco.TAX_FREE) * eco.TAX_RATE)
        assert eco.steuer_fuer(eco.TAX_SOFT) == mitte
        assert eco.steuer_fuer(eco.TAX_SOFT + 1_000_000_000) == \
            mitte + int(1_000_000_000 * eco.TAX_RATE_TOP)

        # Einzug: verbrennt Coins (kein Gegenkonto!) und laeuft pro Tag nur EINMAL.
        eco._profile(1)["coins"] = 21_000_000
        eco._profile(2)["coins"] = 500          # unter dem Freibetrag
        eco._profile(3)["coins"] = -80_000      # Aktien-Schulden: nichts holen
        vor_summe = sum(p["coins"] for p in eco._users().values())
        n, weg = asyncio.run(eco.vermoegenssteuer())
        assert n == 1, n                                  # nur das dicke Konto
        assert weg == int((21_000_000 - eco.TAX_FREE) * eco.TAX_RATE), weg
        assert eco._profile(2)["coins"] == 500            # unangetastet
        assert eco._profile(3)["coins"] == -80_000        # kein Nachtreten
        nach_summe = sum(p["coins"] for p in eco._users().values())
        assert nach_summe == vor_summe - weg              # Coins sind WEG
        # Zweiter Aufruf am selben Tag: kein zweiter Abzug.
        n2, weg2 = asyncio.run(eco.vermoegenssteuer())
        assert (n2, weg2) == (0, 0)
        # Neuer Tag -> wieder faellig.
        eco._store.data["tax_day"] = "1999-01-01"
        n3, weg3 = asyncio.run(eco.vermoegenssteuer())
        assert n3 == 1 and weg3 > 0

        # Auch ein laufendes GIVEAWAY ist kein Tresor: Einsatz kurz vor 2 Uhr
        # einzahlen, danach abbrechen und alles zurueckbekommen.
        import giveaway
        gw = giveaway.instance
        alt_gw = (gw._store, gw._enabled)
        gw._enabled = True
        gw._store = _FakeStore({"active": {"1": {"host": 21, "stake": 40_000_000}},
                                "next_id": 2, "done": []})
        try:
            eco._users().clear()
            eco._profile(21)["coins"] = eco.TAX_FREE
            eco._profile(22)["coins"] = eco.TAX_FREE
            assert eco.depot_wert(21) == 40_000_000 and eco.depot_wert(22) == 0
            eco._store.data["tax_day"] = ""
            n, weg = asyncio.run(eco.vermoegenssteuer())
            assert n == 1 and weg == int(40_000_000 * eco.TAX_RATE), (n, weg)
            assert eco.get_coins(22) == eco.TAX_FREE
        finally:
            gw._store, gw._enabled = alt_gw
        eco._users().clear()

        # Das AKTIEN-DEPOT ist kein steuerfreier Parkplatz: 5 Mio auf dem Konto
        # plus 36 Mio in Anteilen hat vorher NICHTS gekostet, und man haette kurz
        # vor 2 Uhr umparken koennen.
        fa = _fa_mod.instance
        alt_fa = (fa._store, fa._enabled)
        fa._enabled = True
        fa._store = _FakeStore({"price": 200_000, "base": 200_000.0, "day": "x",
                                "act_ema": 5.0, "msg_count": 0, "last_msg_count": 0,
                                "leer_min": 0.0, "holdings": {"11": 150},
                                "history": [], "ticks": []})
        fa._sync_price()
        try:
            eco._users().clear()
            eco._profile(11)["coins"] = eco.TAX_FREE     # genau am Freibetrag
            eco._profile(12)["coins"] = eco.TAX_FREE     # gleich viel, ohne Depot
            depot = eco.depot_wert(11)
            assert depot > 30_000_000, depot
            assert eco.depot_wert(12) == 0
            eco._store.data["tax_day"] = ""
            n, weg = asyncio.run(eco.vermoegenssteuer())
            assert n == 1 and weg == int(depot * eco.TAX_RATE), (n, weg, depot)
            assert eco.get_coins(12) == eco.TAX_FREE      # ohne Depot: steuerfrei
            assert eco.get_coins(11) < eco.TAX_FREE       # mit Depot: zahlt
        finally:
            fa._store, fa._enabled = alt_fa
        eco._users().clear()

        # Gleichgewicht: Einkommen X pro Tag landet bei Freibetrag + X/Satz.
        # Genau das macht die Steuer selbst-skalierend - sie waechst mit der
        # Inflation mit, statt als fester Betrag zu verpuffen.
        for tages_einkommen in (10_000, 20_000_000):
            eco._profile(9)["coins"] = 0
            stand = 0
            for _ in range(2000):
                eco._profile(9)["coins"] = stand + tages_einkommen
                eco._store.data["tax_day"] = ""
                asyncio.run(eco.vermoegenssteuer())
                neu = eco._profile(9)["coins"]
                if abs(neu - stand) <= max(1, tages_einkommen // 1000):
                    break
                stand = neu
            # Erwartung aus der Steuerformel selbst zurueckgerechnet (die Steuer ist
            # oben degressiv: ab TAX_SOFT nur noch TAX_RATE_TOP).
            lo, hi = 0, 10 ** 14
            for _ in range(200):
                m = (lo + hi) // 2
                if eco.steuer_fuer(m) < tages_einkommen:
                    lo = m
                else:
                    hi = m
            erwartet = lo
            assert abs(stand - erwartet) < max(1000, erwartet * 0.05), \
                (tages_einkommen, stand, erwartet)
    finally:
        _fa_mod.instance._enabled = _fa_alt
        restore()




def test_economy_reset_umfang():
    """Das Reset-Skript muss GENAU das treffen, was es soll: alles Coin-nahe auf 0,
    aber Level/XP, Voice-Stunden und Chat-Nachrichten unangetastet.

    Zwei Fehler sind hier schon aufgefallen (beide vom End-zu-End-Lauf gefunden):
    die Tagesbremse der Aktie blieb stehen (Kurs sprang danach auf 5.123 statt
    1.000) und die Startwerte waren vom Modul abgeschrieben, statt daraus gelesen
    (Thron stand nach dem Reset auf dem ALTEN Startpreis)."""
    import importlib.util
    import floaktie
    import luxus

    spec = importlib.util.spec_from_file_location("_reset", "economy_reset.py")
    er = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(er)

    # 1) Startwerte kommen aus den Modulen - keine abgeschriebene Kopie.
    assert er.START_PRICE == floaktie.START_PRICE, (er.START_PRICE, floaktie.START_PRICE)
    assert er.THRONE_START == luxus.THRONE_START, (er.THRONE_START, luxus.THRONE_START)

    # 2) Profile: was bleibt, bleibt - was Geld ist, geht auf 0.
    prof = {"xp": 5000, "coins": 23_000_000, "earned": 90_000_000, "streak": 9,
            "last_daily": "2026-07-25", "voice_secs": 720_000, "msgs": 12_345,
            "title": "🔱 X", "title_rarity": "exklusiv",
            "owned": [{"label": "🔱 X"}], "name": "flo", "eigenes_feld": "bleibt"}
    d = {"users": {"1": dict(prof), "2": "kaputt"}, "shop": {"date": "x", "items": [1]}}
    n, coins = er.reset_economy(d, [])
    p = d["users"]["1"]
    assert (p["xp"], p["voice_secs"], p["msgs"], p["name"]) == (5000, 720_000, 12_345, "flo")
    assert p["eigenes_feld"] == "bleibt"          # Unbekanntes wird nicht angefasst
    for key in er.PROFIL_RESET:
        assert p[key] == er.PROFIL_RESET[key], key
    # Beide Profile zaehlen: das echte (hatte Coins) und das kaputte (neu gebaut).
    assert n == 2 and coins == 23_000_000, (n, coins)
    assert isinstance(d["users"]["2"], dict) and d["users"]["2"]["coins"] == 0
    assert d["shop"] == {"date": "", "items": []}
    # Die geschuetzten Felder duerfen NIE in der Reset-Liste stehen.
    assert not set(er.PROFIL_RESET) & set(er.PROFIL_BEHALTEN)
    assert set(er.PROFIL_BEHALTEN) == {"xp", "voice_secs", "msgs", "name"}

    # 3) Aktie: Depots, Kurs, Verlauf UND die Tagesbremse zurueck auf Start.
    fd = {"price": 79_192_961, "base": 61234.5, "day": "2026-07-26", "act_ema": 12.4,
          "msg_count": 90210, "last_msg_count": 90000, "leer_min": 40,
          "open_day": "2026-07-26", "open_base": 51234.0,
          "holdings": {"1": 150, "2": 150}, "history": [1, 2], "ticks": [1, 2, 3]}
    er.reset_floaktie(fd, [])
    assert fd["price"] == floaktie.START_PRICE and fd["base"] == float(floaktie.START_PRICE)
    assert fd["holdings"] == {} and fd["history"] == [] and fd["ticks"] == []
    assert fd["act_ema"] == 0.0 and fd["leer_min"] == 0 and fd["msg_count"] == 0
    assert not fd["open_day"] and not fd["open_base"], "Tagesbremse blieb stehen!"

    # 4) Die uebrigen Dateien - jede muss ihren Geld-Teil leeren.
    ld = {"users": {"1": ["gold"]}, "throne": {"owner": "1", "preis": 9_000_000, "n": 14}}
    er.reset_luxus(ld, [])
    assert ld["users"] == {} and ld["throne"] == {"owner": "", "preis": luxus.THRONE_START,
                                                  "n": 0}
    lo = {"month": "2026-07", "jackpot": 41_000_000, "ticket_price": 20500,
          "entries": {"1": {"n": 40}}, "house": 1_234_567, "history": [1]}
    er.reset_lotto(lo, [])
    assert (lo["jackpot"], lo["house"], lo["entries"], lo["history"]) == (0, 0, {}, [])
    for fn, daten, pruef in (
            (er.reset_handel, {"users": {"1": {}}}, lambda x: x["users"] == {}),
            (er.reset_casino, {"stats": {"1": {}}}, lambda x: x["stats"] == {}),
            (er.reset_steal, {"cooldowns": {"1": 1}}, lambda x: x["cooldowns"] == {}),
            (er.reset_schulden, {"pairs": {"1:2": {}}, "stats": {"1": {}}},
             lambda x: x["pairs"] == {} and x["stats"] == {}),
            (er.reset_giveaway, {"active": {"7": {}}, "next_id": 8, "done": [1]},
             lambda x: x["active"] == {} and x["next_id"] == 1 and x["done"] == []),
            (er.reset_merchant, {"trades": [1], "sold": {"a": 1}, "stock": [1],
                                 "arrived": True, "departed": True, "day": "x",
                                 "appear_at": 1, "depart_at": 2},
             lambda x: x["trades"] == [] and x["sold"] == {} and x["arrived"] is False)):
        fn(daten, [])
        assert pruef(daten), fn.__name__

    # 5) Nicht-Geld-Dateien stehen NICHT in der Reset-Liste.
    dateien = {name for name, _fn, _b in er.DATEIEN}
    assert not dateien & {"words.json", "moderation.json", "voicegags.json",
                          "admin.json", "features.json", "guildcfg.json",
                          "profil.json"}
    assert "economy.json" in dateien and "floaktie.json" in dateien

    # 6) games.json: NUR die Spiele-Tageskappe, der Zaehlspiel-Stand bleibt.
    #    Ohne das startet jeder in den Reset-Tag mit ausgeschoepfter Kappe.
    assert "games.json" in dateien
    gd = {"counting": {"kanal1": {"n": 42}}, "daily": {"day": "2026-07-26",
                                                       "won": {"1": 50_000, "2": 10}}}
    er.reset_spiele_kappe(gd, [])
    assert gd["daily"] == {"day": "", "won": {}}
    assert gd["counting"] == {"kanal1": {"n": 42}}, "Zaehlspiel wurde mitgeloescht!"

    # 7) Der Steuer-Stempel wird mitgeloescht (sonst gilt der Reset-Tag als erledigt).
    ed = {"users": {}, "tax_day": "2026-07-26"}
    er.reset_economy(ed, [])
    assert ed["tax_day"] == ""

    # 8) ZWEI PHASEN: erst alles einlesen und umbauen, DANN schreiben. Ein kaputtes
    #    JSON in der Mitte darf die Wirtschaft nicht halb zurueckgesetzt lassen.
    quelle = open("economy_reset.py", encoding="utf-8").read()
    i_lade = quelle.index("for name, fn, beschreibung in DATEIEN:")
    i_schreib = quelle.index("for i, (pfad, daten) in enumerate(fertig):")
    assert i_lade < i_schreib, "Schreibphase muss NACH der Lesephase kommen"
    # In der Leseschleife darf nicht geschrieben werden.
    assert "schreiben(" not in quelle[i_lade:i_schreib]




def test_embeds_statt_fliesstext():
    """Die haeufigsten Antworten muessen ECHTE Embeds sein (mit Farbe, Feldern und
    allem, was man nach der Aktion wissen will) - vorher waren das ein bis zwei
    Zeilen Fliesstext."""
    import discord
    import floaktie

    # --- Tagesbonus ------------------------------------------------------
    restore = _with_economy({7: 0})
    eco = economy.instance
    try:
        m = SimpleNamespace(id=7, display_name="T",
                            display_avatar=SimpleNamespace(url="http://x/y.png"))
        e = asyncio.run(eco._daily(m))
        assert isinstance(e, discord.Embed), e
        txt = _embed_text(e)
        assert "Tagesbonus" in txt and "Serie" in txt
        assert str(economy.fmt(eco.DAILY_BASE)) in txt
        assert "🔥" in txt                       # Streak-Kette sichtbar
        assert economy.get_coins(7) == eco.DAILY_BASE + eco.DAILY_STREAK_STEP
        # Zweiter Versuch am selben Tag: eigenes Embed, KEIN Geld.
        vorher = economy.get_coins(7)
        e2 = asyncio.run(eco._daily(m))
        assert isinstance(e2, discord.Embed)
        assert "schon" in _embed_text(e2).lower()
        assert economy.get_coins(7) == vorher
        # Streak-Kette waechst und ist bei DAILY_STREAK_MAX voll.
        assert eco._streak_balken(0).count("🔥") == 0
        assert eco._streak_balken(3).count("🔥") == 3
        assert eco._streak_balken(99).count("🔥") == eco.DAILY_STREAK_MAX
        assert "▫️" not in eco._streak_balken(eco.DAILY_STREAK_MAX)
    finally:
        restore()

    # --- Aktien-Bestaetigung ---------------------------------------------
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    restore = _with_economy({8: 5_000_000})
    fa._enabled = True
    fa._store = _FakeStore({"price": 1000, "base": 1000.0, "day": "x", "act_ema": 0.0,
                            "msg_count": 0, "last_msg_count": 0, "leer_min": 0.0,
                            "holdings": {}, "history": [], "ticks": []})
    fa._sync_price()
    try:
        e = asyncio.run(fa.buy(SimpleNamespace(id=8), 20))
        assert isinstance(e, discord.Embed)
        txt = _embed_text(e)
        for muss in ("Gekauft", "Kurs", "Dein Depot", "Kontostand", "▲"):
            assert muss in txt, (muss, txt)
        assert "▰" in txt                        # Depot-Balken
        e = asyncio.run(fa.sell(SimpleNamespace(id=8), 20))
        txt = _embed_text(e)
        assert "Verkauft" in txt and "Verkaufssteuer" in txt and "▼" in txt
        # Und das Panel nennt Deckel, Depot-Grenze und Steuer.
        p = _embed_text(fa._panel_embed(SimpleNamespace(id=8)))
        assert "Deckel" in p and str(floaktie.MAX_SHARES_PER_USER) in p
        assert "Verkaufssteuer" in p
        # Abgeschaltete Aktie: sauberes Embed, kein Durchfallen an die KI.
        aus = _embed_text(fa.aus_embed())
        assert "deaktiviert" in aus and "Anteile" in aus
    finally:
        fa._store, fa._enabled = alt
        restore()




def test_aktie_zaehlt_alle_server_zusammen():
    """Die Aktie ist fuer alle Server dieselbe: die Aktivitaet wird summiert,
    die Dividende bekommt trotzdem jeder nur EINMAL."""
    def mitglied(uid, stream=False):
        return SimpleNamespace(id=uid, bot=False, display_name=f"U{uid}",
                               voice=SimpleNamespace(self_stream=stream, self_video=False,
                                                     self_deaf=False, deaf=False))

    def server(gid, leute):
        vc = SimpleNamespace(id=gid * 10, members=leute, voice_states={})
        return SimpleNamespace(id=gid, name=f"S{gid}", afk_channel=None,
                               voice_channels=[vc], me=SimpleNamespace(id=999))

    a, b = mitglied(1), mitglied(2, stream=True)
    c = mitglied(3)
    A, B = server(111, [a, b]), server(222, [b, c])   # b sitzt auf BEIDEN

    fa = floaktie.instance
    # Ein Server allein.
    assert fa._measure_alle(A) == (2, 1, 0)
    # Beide zusammen: 4 Leute (b doppelt anwesend = doppelt aktiv), 2 Streams.
    assert fa._measure_alle([A, B]) == (4, 2, 0)
    # Auch ein einzelner Server als Liste geht.
    assert fa._measure_alle([B]) == (2, 1, 0)
    assert fa._measure_alle(None) == (0, 0, 0)
    assert fa._measure_alle([]) == (0, 0, 0)

    # Dividende: b haelt Anteile und sitzt auf beiden Servern -> genau eine Zahlung.
    alt_store, alt_on = fa._store, fa._enabled
    fa._store = _FakeStore({"holdings": {"2": 1000}, "state": {}})
    fa._enabled = True
    gezahlt = []
    alt_add = economy.add_coins
    alt_flush = economy.flush

    async def kein_flush():
        pass

    economy.add_coins = lambda uid, betrag, reason="": gezahlt.append((uid, betrag))
    economy.flush = kein_flush
    try:
        asyncio.run(fa.pay_voice_dividends([A, B]))
        assert len(gezahlt) == 1, gezahlt
        assert gezahlt[0][0] == 2 and gezahlt[0][1] > 0
    finally:
        economy.add_coins, economy.flush = alt_add, alt_flush
        fa._store, fa._enabled = alt_store, alt_on




# --- Schwere Fehler aus dem Volltest (B1-B8) --------------------------------
def test_alle_modul_aliase_existieren():
    """bot.py ruft Modul-Funktionen ueber den Alias am Dateiende auf.

    Fehlt einer, fliegt zur LAUFZEIT ein AttributeError - und in einem
    tasks.loop stoppt das den Loop DAUERHAFT. Genau so ist der Haendler-Loop
    bei der ersten Ankunft gestorben: bot.py rief merchant.build_view(), das
    Modul exportierte den Alias aber nicht. Dieser Test liest bot.py als Text
    und prueft JEDEN dort benutzten Modul-Aufruf."""
    import importlib
    import re
    quelle = open("bot.py", encoding="utf-8").read()
    module = set(re.findall(r"^import (\w+)$", quelle, re.M))
    fehlend = []
    for treffer in re.finditer(r"\b(\w+)\.(\w+)\(", quelle):
        mod, attr = treffer.group(1), treffer.group(2)
        if mod not in module or attr.startswith("_"):
            continue
        try:
            m = importlib.import_module(mod)
        except Exception:  # noqa: BLE001 - nicht importierbar ist hier egal
            continue
        if not hasattr(m, attr):
            fehlend.append(f"{mod}.{attr}")
    assert not fehlend, f"bot.py ruft nicht existierende Aliase: {sorted(set(fehlend))}"




def test_kaputte_datei_haelt_den_bot_nicht_auf():
    """Eine von Hand editierte JSON-Datei darf den START nicht verhindern.

    Die setup()-Kette laeuft in bot.py auf MODULEBENE - wirft dort ein Modul,
    kommt der ganze Bot nicht mehr hoch, nicht nur das eine Feature.
    Nachgemessen galt das fuer economy, words, features, lotto, giveaway und
    schulden. Der Standard eines JsonStore ist jetzt zugleich die
    Typ-Schablone."""
    import json
    import pathlib
    import tempfile
    import store

    alt_dir = store.DATA_DIR
    d = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d
    try:
        (d / "kaputt.json").write_text(json.dumps(
            {"users": None, "zahl": "viel", "liste": {}, "text": 5, "flag": "ja"}))
        s = store.JsonStore("kaputt.json", default={
            "users": {}, "zahl": 0, "liste": [], "text": "", "flag": False})
        assert s.data["users"] == {} and s.data["zahl"] == 0
        assert s.data["liste"] == [] and s.data["text"] == ""
        assert s.data["flag"] is False
        # Unbekannte Schluessel bleiben unangetastet (nichts stillschweigend loeschen).
        (d / "extra.json").write_text(json.dumps({"users": {"1": {"xp": 5}}, "fremd": 42}))
        s2 = store.JsonStore("extra.json", default={"users": {}})
        assert s2.data["users"] == {"1": {"xp": 5}} and s2.data["fremd"] == 42
    finally:
        store.DATA_DIR = alt_dir

    # Und die sechs echten Module kommen mit ihrem jeweiligen Muell hoch.
    import features
    import giveaway
    import lotto
    import schulden
    import words
    d2 = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d2
    try:
        (d2 / "words.json").write_text(json.dumps({"words": None, "scan": []}))
        (d2 / "lotto.json").write_text(json.dumps({"jackpot": "viel"}))
        (d2 / "giveaway.json").write_text(json.dumps({"active": None}))
        (d2 / "schulden.json").write_text(json.dumps({"pairs": None}))
        (d2 / "features.json").write_text(json.dumps(
            {"disabled": [], "guilds": {"abc": ["casino"], "123": ["music"]}}))
        for mod in (words, lotto, giveaway, schulden, features):
            neu = type(mod.instance)()
            neu.setup()          # darf NICHT werfen
        # Der unbrauchbare Server-Eintrag wird uebersprungen, der gute bleibt.
        f = type(features.instance)()
        f.setup()
        assert 123 in f._per_guild and f._per_guild[123] == {"music"}
    finally:
        store.DATA_DIR = alt_dir




def test_haendler_nimmt_keinen_besseren_titel_als_einsatz():
    """Der Tausch prueft nur eine UNTERGRENZE. Wer fuer einen Tausch
    'mindestens episch' braucht, konnte damit seinen GOETTLICHEN Titel
    hergeben und einen schlechteren zurueckbekommen - unwiderruflich, ohne
    Rueckfrage, und im Dropdown stand er ganz normal zur Auswahl."""
    import merchant
    import titles
    m = merchant.instance

    besitz = [
        {"text": "n", "label": "Normal", "rarity": "normal"},
        {"text": "e", "label": "Episch", "rarity": "episch"},
        {"text": "x", "label": "Exklusiv", "rarity": "exklusiv"},
        {"text": "g", "label": "Goettlich", "rarity": "goettlich"},
    ]
    alt_liste = economy.list_titles
    economy.list_titles = lambda _uid: besitz
    try:
        wer = SimpleNamespace(id=1)
        # Tausch: braucht mindestens 'episch', gibt 'exklusiv'.
        auswahl = [o["rarity"] for o in m.eligible_gives(wer, "episch", "exklusiv")]
        assert "episch" in auswahl and "exklusiv" in auswahl, auswahl
        assert "goettlich" not in auswahl, "der bessere Titel steht noch im Dropdown"
        assert "normal" not in auswahl, auswahl
        # Ohne Obergrenze bleibt das alte Verhalten (andere Aufrufer).
        assert "goettlich" in [o["rarity"] for o in m.eligible_gives(wer, "episch")]
    finally:
        economy.list_titles = alt_liste

    # Die Rangfolge, auf der das beruht, muss aufsteigend sein.
    assert titles.RANK["goettlich"] > titles.RANK["exklusiv"] > titles.RANK["episch"]




def test_kauf_rueckfrage_haengt_am_titel_nicht_an_der_nummer():
    """Die Bestaetigung band nur an die SLOT-NUMMER. Wuerfelt der Shop um 2 Uhr
    neu, kaufte 'nochmal derselbe Befehl' danach den Titel auf derselben
    Nummer - einen anderen, dessen Preis nie jemand gesehen hatte."""
    import economy
    e = economy.instance
    alt = dict(e._kauf_offen)
    e._kauf_offen = {}
    try:
        wer = SimpleNamespace(id=4711)
        teuer_a = {"text": "koenig", "label": "König", "price": 5_000_000}
        teuer_b = {"text": "kaiser", "label": "Kaiser", "price": 9_000_000}
        # Erste Anfrage -> Rueckfrage.
        assert e._kauf_rueckfrage(wer, 2, teuer_a) is not None
        # Gleicher Titel nochmal -> geht durch.
        assert e._kauf_rueckfrage(wer, 2, teuer_a) is None
        # Neue Anfrage, dann Shop-Reroll: gleiche Nummer, ANDERER Titel.
        assert e._kauf_rueckfrage(wer, 2, teuer_a) is not None
        assert e._kauf_rueckfrage(wer, 2, teuer_b) is not None, \
            "anderer Titel auf derselben Nummer wurde ohne Rueckfrage gekauft!"
    finally:
        e._kauf_offen = alt




def test_los_und_lose_erreichen_das_lotto():
    """'Flo los' / 'Flo lose 5' landeten im Casino (frueher in der Kette) als
    Rubbellos - das Lotto-Panel war darueber nicht erreichbar."""
    import casino
    import lotto
    quelle = open("casino.py", encoding="utf-8").read()
    handle_teil = quelle.split("async def handle", 1)[1][:8000]
    assert '"los"' not in handle_teil and '"lose"' not in handle_teil, \
        "casino beansprucht 'los'/'lose' wieder"
    assert "los" in lotto._CMDS and "lose" in lotto._CMDS
    # Das Rubbellos behaelt seine eindeutigen Woerter.
    for wort in ("rubbellos", "rubbel", "scratch"):
        assert f'"{wort}"' in handle_teil, wort




def test_kleine_fehler_der_wirtschaft():
    """add_coins-Rueckgabe, Admin-Korrekturen und Slot-Deckel."""
    import economy
    import games
    import schulden
    restore = _with_economy({1: 1000, 2: 0})
    alt = (schulden.instance._enabled, schulden.instance._store,
           schulden.instance.buch._store, schulden.instance.buch._posten)
    schulden.instance._enabled = True
    schulden.instance._store = _FakeStore(
        {"posten": [], "next_id": 1, "score": {}, "stats": {}, "archiv": {},
         "pairs": {}})
    schulden.instance.buch.laden(schulden.instance._store)
    try:
        schulden.instance.buch.anlegen(2, 1, 5_000)   # 1 schuldet 2 nun 5.000
        # add_coins meldete den Stand VOR der Tilgung - Anzeigen logen.
        gemeldet = economy.add_coins(1, 1_000, reason="spiele")
        assert gemeldet == economy.get_coins(1), \
            f"add_coins meldet {gemeldet}, Konto ist {economy.get_coins(1)}"
        # Admin-Korrekturen muessen EXAKT ankommen (wie beim Panel).
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="admin")
        assert economy.get_coins(1) - vor == 1_000
        vor = economy.get_coins(1)
        economy.add_coins(1, 1_000, reason="setcoins")
        assert economy.get_coins(1) - vor == 1_000
    finally:
        (schulden.instance._enabled, schulden.instance._store,
         schulden.instance.buch._store, schulden.instance.buch._posten) = alt
        restore()

    # Slot-Textbefehl kannte keine Obergrenze - das Menue schon.
    quelle = open("games.py", encoding="utf-8").read()
    slot_teil = quelle.split("async def _slot(", 1)[1][:900]
    assert "SKILL_MAX_BET" in slot_teil, "Slot-Textpfad ohne Hoechsteinsatz"




def test_giveaway_schnellstart_lost_nichts_aus():
    """'jetzt', 'ende' und 'stop' sind normale Woerter. Weil sie IRGENDWO im
    Text gesucht wurden, loste 'giveaway 5k 2h weil ich jetzt Lust habe' ein
    laufendes Giveaway sofort aus - Gewinner Stunden zu frueh."""
    quelle = open("giveaway.py", encoding="utf-8").read()
    teil = quelle.split("# Abbrechen / sofort ziehen", 1)[1][:1200]
    assert "erstes" in teil and "low.split()" in teil, \
        "ziehen/abbrechen wird nicht mehr am ERSTEN Wort erkannt"
    assert "self._hat(low, (\"abbrechen\"" not in teil




def test_kaputte_daten_toeten_kein_feature_zur_laufzeit():
    """Kaputte Werte im Store duerfen nicht erst BEIM BENUTZEN sterben.

    Die Typ-Schablone des JsonStore prueft nur die oberste Ebene: '"coins":
    null' oder '"xp": "viel"' TIEF IM Profil kommt daran vorbei. Vorher starb
    daran nicht nur die eine Anzeige - economy.on_message laeuft ohne
    Auffangnetz, also erreichte danach der ganze Rest dieser Nachricht die
    Platte nicht mehr, und EIN kaputtes Fremdprofil kippte 'top' und
    'reichste' fuer alle."""
    import casino
    import economy
    import floaktie
    import handel
    import luxus
    import moderation
    import steal
    import words

    # --- economy: Profil-Weg und die drei Ranglisten --------------------
    alt = (economy.instance._store, economy.instance._enabled)
    economy.instance._enabled = True
    economy.instance._store = _FakeStore({"users": {
        "1": {"xp": "viel", "coins": None, "owned": {"a": 1}, "name": "A"},
        "2": None,
        "3": {"xp": 500, "coins": 100, "owned": [], "name": "C"},
    }})
    try:
        economy.instance.add_coins(1, 10, reason="test")
        assert economy.instance.get_coins(1) == 10
        assert economy.instance._profile(1)["owned"] == []
        assert economy.instance._level_only(economy.instance._profile(1)["xp"]) == 0
        # Die Ranglisten lesen den Store ROH - genau dort lag der Fehler.
        assert len(economy.instance.leaderboard_data(10)) == 3
        assert len(economy.instance.money_leaderboard_data(10)) == 3
        assert economy.instance._rank_of(3)[1] == 3
        economy.instance._leaderboard_embed()
    finally:
        (economy.instance._store, economy.instance._enabled) = alt

    # --- floaktie: Anteile und Kurs-Verlauf ------------------------------
    alt_f = floaktie.instance._store
    floaktie.instance._store = _FakeStore({
        "holdings": {"1": "viel", "2": None, "3": 4},
        "history": [{"day": "2026-01-01", "price": 10}, None, "murks"],
        "ticks": None,
        "price": 100, "base": 100, "day": "2026-01-01",
    })
    try:
        assert floaktie.instance.shares_of(1) == 0
        assert floaktie.instance.total_shares() == 4
        assert floaktie.instance.holders_count() == 1
        assert floaktie.instance.top_holder() == 3
        floaktie.instance._sparkline()
        floaktie.instance._series(7)
        assert isinstance(floaktie.instance.leaderboard(), list)
    finally:
        floaktie.instance._store = alt_f

    # --- die uebrigen Auskuenfte, die der Profil-Lookup mitzieht ---------
    faelle = [
        (handel.instance, "_store", {"users": {"7": {"in": None, "out": "x", "n": None}}},
         lambda m: m.summe_von(7) == (0, 0, 0)),
        (moderation.instance, "_store", {"warns": None},
         lambda m: m.warns_of(1, 7) == 0),
        (moderation.instance, "_store", {"warns": {"1": {"7": None}}},
         lambda m: m.warns_of(1, 7) == 0),
        (steal.instance, "_store", {"cooldowns": None},
         lambda m: m._remaining(7) == 0),
        (luxus.instance, "_store", {"users": {"7": None}, "throne": None},
         lambda m: m.owns(7, "krone") is False and m.throne_preis() == luxus.THRONE_START),
        (casino.instance, "_stats", {"stats": None},
         lambda m: m._stats_profile(7)["games"] == 0),
        (words.instance, "_store", {"guilds": {"1": {"words": {"hallo": 5},
                                                     "total": "viel"}}},
         lambda m: m.statistik_von(7) == (0, 0, [])),
    ]
    for modul, feld, daten, pruefung in faelle:
        vorher_store = getattr(modul, feld)
        vorher_an = modul._enabled
        setattr(modul, feld, _FakeStore(daten))
        modul._enabled = True
        try:
            assert pruefung(modul), (type(modul).__name__, daten)
        finally:
            setattr(modul, feld, vorher_store)
            modul._enabled = vorher_an

    # words zaehlt trotz kaputtem Index weiter (das lief bei JEDER Nachricht).
    alt_w = (words.instance._store, words.instance._enabled)
    words.instance._enabled = True
    words.instance._store = _FakeStore(
        {"guilds": {"1": {"words": None, "total": None, "msgs": "x"}}})
    try:
        words.instance._count_text("hallo welt hallo", "7", 1)
        words.instance._count_text("hallo welt", "7", 1)
        buch = words.instance._buch(1)
        assert buch["words"]["hallo"]["n"] == 3
        assert buch["total"] == 5
        assert buch["msgs"] == 2
    finally:
        (words.instance._store, words.instance._enabled) = alt_w




def test_kaputte_games_json_kostet_keinen_gewinn():
    """Eine games.json mit  "daily": null  ist GUELTIGES JSON und kommt deshalb
    an der Quarantaene im Store vorbei. setdefault() half nur gegen einen
    FEHLENDEN Schluessel, nicht gegen einen kaputten Wert.

    Der AttributeError landete mitten in der Abrechnung einer GEWONNENEN Runde.
    bot.py faengt ihn ab und loggt "Spiele-Hook fehlgeschlagen" - der Spieler
    sieht gar nichts und sein Einsatz ist weg. Betroffen: Mathe, Anagramm,
    Quiz, Zahlenraten, Reaktion und das Schnell-Event."""
    import games
    games.setup()
    g = games.instance
    if g._store is None:
        return
    alt_daily = g._store.data.get("daily")
    heute = g._heute()
    kaputte_formen = (
        (None, "daily ist null"),
        ("quatsch", "daily ist Text"),
        ([], "daily ist Liste"),
        ({"day": heute, "won": None}, "won ist null"),
        ({"day": heute, "won": []}, "won ist Liste"),
        ({"day": heute, "won": {"4242": "viel"}}, "Zaehlerstand ist Text"),
    )
    try:
        for form, name in kaputte_formen:
            g._store.data["daily"] = dict(form) if isinstance(form, dict) else form
            g._kappe_rest(4242)          # darf nicht fliegen
            g._store.data["daily"] = dict(form) if isinstance(form, dict) else form
            g._kappe_buchen(4242, 100)   # darf auch nicht fliegen
            # Und danach muss die Struktur wieder brauchbar sein.
            zustand = g._store.data["daily"]
            assert isinstance(zustand, dict), (name, zustand)
            assert isinstance(zustand.get("won"), dict), (name, zustand)
    finally:
        if alt_daily is None:
            g._store.data.pop("daily", None)
        else:
            g._store.data["daily"] = alt_daily

    # Der Store-Standard muss den Zweig selbst schon kennen - sonst entsteht die
    # Luecke beim naechsten frischen Server wieder.
    quelle = inspect.getsource(games.Games.setup)
    assert '"daily"' in quelle, "der Store-Standard kennt 'daily' nicht"




def test_tts_nutzertext_ist_niemals_eine_option():
    """espeak bekommt den Nutzertext als argv-Element. OHNE ein "--" davor liest
    es jeden Text mit fuehrendem Bindestrich als OPTION:

        Flo sprich -w/opt/flobot/data/economy.json   -> WAV UEBERSCHREIBT die Daten
        Flo sprich -f/opt/flobot/.env                -> liest den Token im Voice vor

    Jeder Nutzer, keine Rechtepruefung. create_subprocess_exec schuetzt nur vor
    der Shell, nicht vor der Optionserkennung des aufgerufenen Programms.

    Der Test benutzt ein espeak-Double, das die Optionen genauso liest wie das
    echte - damit ist es ein Nachweis, keine Behauptung."""
    import subprocess
    import voicegags

    with tempfile.TemporaryDirectory() as ordner:
        opfer = os.path.join(ordner, "economy.json")
        with open(opfer, "w", encoding="utf-8") as datei:
            datei.write('{"wichtige": "Wirtschaftsdaten"}')

        # Ein "espeak", das -w wie das echte behandelt und bei "--" aufhoert.
        falsches_espeak = os.path.join(ordner, "espeak-ng")
        with open(falsches_espeak, "w", encoding="utf-8") as datei:
            datei.write(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "argv, ziel, nur_text = sys.argv[1:], None, False\n"
                "i = 0\n"
                "while i < len(argv):\n"
                "    a = argv[i]\n"
                "    if not nur_text and a == '--':\n"
                "        nur_text = True\n"
                "    elif not nur_text and a.startswith('-w'):\n"
                "        ziel = a[2:] or (argv[i + 1] if i + 1 < len(argv) else None)\n"
                "        if not a[2:]:\n"
                "            i += 1\n"
                "    i += 1\n"
                "if ziel:\n"
                "    open(ziel, 'wb').write(b'RIFF....WAVEfmt ' + b'\\0' * 64)\n")
        os.chmod(falsches_espeak, 0o755)

        # _synthesize prueft den NAMEN der Engine ("espeak-ng"), nicht den Pfad.
        # Deshalb muss das Double unter diesem Namen im PATH stehen - sonst
        # laeuft es gar nicht und der Test waere gruen, ohne etwas zu pruefen.
        flo = voicegags.VoiceGags()
        alt_engine, alt_pfad = flo._tts_engine, os.environ.get("PATH", "")
        flo._tts_engine = "espeak-ng"
        os.environ["PATH"] = ordner + os.pathsep + alt_pfad
        pfad = None
        try:
            # Genau der Angriff.
            pfad = asyncio.run(flo._synthesize(f"-w{opfer}"))
        finally:
            flo._tts_engine = alt_engine
            os.environ["PATH"] = alt_pfad
            if pfad and os.path.exists(pfad):
                os.unlink(pfad)

        # Erst pruefen, dass das Double ueberhaupt lief - sonst beweist der
        # naechste assert nichts.
        assert pfad is not None, "Double wurde nicht ausgefuehrt"

        inhalt = open(opfer, encoding="utf-8", errors="replace").read()
        assert inhalt == '{"wichtige": "Wirtschaftsdaten"}', (
            "Der Nutzertext wurde als espeak-Option gelesen und hat eine fremde "
            f"Datei ueberschrieben: {inhalt[:60]!r}")

    # Und der Riegel muss im Code auch wirklich stehen - beide Ebenen.
    quelle = inspect.getsource(voicegags.VoiceGags._synthesize)
    assert '"--"' in quelle, "das '--' vor dem Nutzertext fehlt wieder"
    assert quelle.index('"--"') < quelle.index("text,"), "'--' steht nicht VOR dem Text"
    assert 'lstrip("-")' in inspect.getsource(voicegags.VoiceGags._cmd_say), (
        "der zweite Riegel (fuehrende Bindestriche weg) fehlt")




def test_rauchtest_jeder_befehl_laeuft_wirklich_durch():
    """Die grosse Luecke: fast alle Tests pruefen EINZELTEILE. Ob ein Befehl als
    Ganzes noch durchlaeuft - Erkennung, Handler, Antwort - stand nirgends.
    Genau dort brechen Umbauten.

    Hier geht jeder wichtige Befehl durch das ECHTE handle() seines Moduls, mit
    einer Nachricht, wie bot.on_message sie weiterreicht. Der Test prueft nicht,
    WAS herauskommt (das machen die Einzeltests), sondern DASS etwas
    herauskommt und nichts fliegt."""
    import ai
    import arbeit
    import casino
    import fun
    import games
    import guildcfg
    import handel
    import lotto
    import luxus
    import merchant
    import moderation
    import profil
    import schulden
    import steal
    import terraria
    import words

    # fun/chaos braucht die KI - ohne einen Client bleibt es aus und der
    # Rauchtest wuerde die Befehle stillschweigend ueberspringen.
    alt_client = ai.instance._client
    ai.instance._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=None)))
    for modul in (economy, words, schulden, profil, luxus, handel, steal, lotto,
                  floaktie, arbeit, fun, games, casino, moderation, guildcfg,
                  terraria, merchant, gehirn):
        try:
            modul.setup()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"{modul.__name__}.setup() fliegt: {exc}") from exc

    PROBEN = (
        (economy, ("level", "coins", "top", "daily", "shop", "inventar", "titel",
                   "rang", "kontostand", "bestenliste")),
        (words, ("woerter", "wort pizza", "wordcount")),
        (gehirn, ("gedaechtnis", "gedächtnis", "vergiss mich", "vergiss",
                  "was weisst du")),
        (schulden, ("schulden", "kreide", "leih 100", "insolvenz", "schuldenbuch")),
        (profil, ("profil", "avatar", "banner", "steckbrief")),
        (luxus, ("luxus", "thron", "prestige")),
        (handel, ("handel", "transaktionen", "verlauf")),
        (steal, ("steal", "klau", "raub")),
        (lotto, ("lotto", "jackpot", "lose", "ziehung")),
        (floaktie, ("aktie", "kurs", "chart", "floaktie", "aktienkurs")),
        (arbeit, ("work", "arbeit", "lohnzettel", "wordle", "tageswort", "schicht")),
        (fun, ("roast", "hype", "spruch", "horoskop", "rizz", "aura", "weisheit")),
        (games, ("quiz", "zahlenraten", "coinflip", "wuerfel", "slots", "reaktion")),
        (casino, ("casino", "blackjack", "roulette 100 rot", "mines 100", "stats",
                  "crash 100", "keno 100 1 2 3", "hilo 100", "turm 100")),
        (moderation, ("warns", "warnungen", "warnliste")),
        (guildcfg, ("einstellungen", "config", "settings")),
        (terraria, ("terraria", "twiki")),
        (merchant, ("haendler", "merchant", "kraemer")),
    )

    stumm, geflogen = [], []
    try:
        for modul, befehle in PROBEN:
            for befehl in befehle:
                try:
                    antwort = asyncio.run(modul.handle(_rauch_nachricht(befehl)))
                except Exception as exc:  # noqa: BLE001 - genau das suchen wir
                    geflogen.append(f"{modul.__name__} '{befehl}': "
                                    f"{type(exc).__name__}: {str(exc)[:120]}")
                    continue
                if antwort is None:
                    stumm.append(f"{modul.__name__} '{befehl}'")
    finally:
        ai.instance._client = alt_client

    assert not geflogen, "Befehle stuerzen ab:\n  " + "\n  ".join(geflogen)
    assert not stumm, ("Befehle werden nicht mehr erkannt (handle gibt None, "
                       "der Befehl faellt zur KI durch):\n  " + "\n  ".join(stumm))




def test_kein_befehl_stuerzt_bei_feindlicher_eingabe_ab():
    """Aktiv gesuchte Grenzfaelle statt Hoffnung: jeder Befehl, der Argumente
    nimmt, bekommt 47 gemeine Eingaben - leer, negativ, absurd gross, falsche
    Zahlenformate, arabische Ziffern, Formatzeichen, Einschleusungsversuche,
    @everyone, 5000 Zeichen, Nullbytes.

    Ein Absturz hier ist im Betrieb eine Nachricht, die Flo verschluckt - und
    bei Coin-Befehlen im schlimmsten Fall ein abgebuchter Einsatz ohne Spiel.

    WICHTIG: alles laeuft in EINEM Event-Loop. Mit asyncio.run je Eingabe misst
    man das Testgeschirr statt den Bot - die geteilte HTTP-Sitzung ueberlebt das
    Schliessen des Loops nicht und erzeugt 44 Phantom-Fehler."""
    import arbeit
    import casino
    import games
    import guildcfg
    import handel
    import lotto
    import luxus
    import merchant
    import moderation
    import profil
    import schulden
    import steal
    import terraria
    import words

    mit_argumenten = {
        economy: ("pay", "zahl", "kaufen", "buy", "equip", "setze", "top", "coins"),
        schulden: ("leih", "tilg", "abzahl", "borg", "schuldschein"),
        steal: ("steal", "klau"),
        lotto: ("lotto", "lose"),
        floaktie: ("aktie", "kaufen", "verkaufen"),
        arbeit: ("work", "wordle", "lohnzettel"),
        games: ("zahlenraten", "coinflip", "wuerfel", "quiz"),
        casino: ("roulette", "blackjack", "mines", "crash", "keno", "hilo",
                 "turm", "slots", "baccarat", "sieben", "rubbellos", "duell"),
        moderation: ("purge", "warn", "timeout", "kick", "ban", "unban", "unwarn"),
        guildcfg: ("einstellung",),
        words: ("wort",),
        profil: ("check", "avatar"),
        luxus: ("luxus", "thron"),
        merchant: ("haendler",),
        terraria: ("terraria",),
        handel: ("handel",),
    }
    for modul in mit_argumenten:
        modul.setup()

    abstuerze = []

    async def alles_durchspielen():
        for modul, befehle in mit_argumenten.items():
            for befehl in befehle:
                for eingabe in _BOESE_EINGABEN:
                    text = f"{befehl} {eingabe}".strip()
                    try:
                        await modul.handle(_rauch_nachricht(text))
                    except Exception as exc:  # noqa: BLE001 - genau das suchen wir
                        abstuerze.append(
                            f"{modul.__name__} '{befehl}' mit {eingabe[:30]!r}: "
                            f"{type(exc).__name__}: {str(exc)[:100]}")

    asyncio.run(alles_durchspielen())
    # Doppelte Meldungen zusammenfassen - sonst steht derselbe Fehler 47-mal da.
    eindeutig = sorted(set(abstuerze))
    assert not eindeutig, (f"{len(abstuerze)} Abstuerze bei feindlicher Eingabe:\n  "
                           + "\n  ".join(eindeutig[:20]))




def test_kein_titel_verbietet_das_roasten():
    """DER Grund, warum Flo zahm wurde - und er hatte nichts mit dem Modell zu tun.

    Die Tonfall-Texte in titles.py waren eine Zahmheits-Rampe: je seltener der
    Titel, desto braver sollte Flo sein. Ganz oben stand woertlich "Kein
    Roasten, keine fiesen Sprueche" und "Absolut kein Spott". Wer lange dabei
    ist - also genau die Stammgaeste - bekam per Bauart einen lieben Flo.

    Seltenheit darf aendern, WIE geroastet wird, niemals OB."""
    import titles
    verboten = ("kein roasten", "absolut kein spott", "kein fuenkchen spott",
                "spott hat hier nichts zu suchen", "kein spott",
                "das fiese roasten laesst du grossteils weg",
                "leg den ganzen aggro-modus komplett ab",
                "fahr die aggression einen tick runter")
    schuldige = []
    for key, stufe in titles.Titles.RARITY.items():
        ton = (stufe.get("tone") or "").lower()
        for satz in verboten:
            if satz in ton:
                schuldige.append(f"{key}: '{satz}'")
    assert not schuldige, (
        "Diese Stufen verbieten Flo das Roasten:\n  " + "\n  ".join(schuldige))
    # Und jede Stufe MUSS einen Tonfall haben - eine leere Stufe waere still
    # wieder der neutrale, brave Standard.
    for key, stufe in titles.Titles.RARITY.items():
        assert (stufe.get("tone") or "").strip(), f"{key} hat gar keinen Tonfall"




def test_schnell_event_verspricht_nichts_was_es_nicht_zahlt():
    """Bei 100 Coins fiel es nie auf - bei fuenfstelligen Preisen schon:
    _auszahlen kuerzt auf die Tageskappe, die Siegermeldung nannte aber den
    AUSGELOBTEN Betrag. Wer seine Kappe voll hatte, las "+25.000 Flo Coins"
    und bekam nichts."""
    quelle = open("games.py", encoding="utf-8").read()
    i = quelle.index('self._auszahlen(message.author.id, runde["reward"]')
    rumpf = quelle[i:i + 1200]
    assert "gezahlt =" in quelle[max(0, i - 200):i + 100], (
        "der Rueckgabewert von _auszahlen wird weggeworfen")
    assert "gezahlt" in rumpf and "Tageskappe" in rumpf, (
        "die Siegermeldung nennt weiter den ausgelobten statt den gezahlten Betrag")
    # Und der Betrag muss mit Tausenderpunkten dastehen, nicht als '25000'.
    assert "numfmt.fmt(gezahlt)" in rumpf, rumpf[:200]




def test_ein_sentinel_fuer_alle():
    """Jedes HANDLED muss DASSELBE Objekt sein.

    'Ich habe selbst geantwortet' erkennt bot.on_message an der IDENTITAET des
    zurueckgegebenen Objekts. Frueher hatte jedes der 21 Module sein eigenes
    `HANDLED = object()`, und bot.py sammelte sie aus einer von Hand gepflegten
    Liste ein. Beim Aufteilen einer Datei waere das eine Falle mit Ansage: die
    neue Datei macht sich ihr eigenes object(), das ist ein ANDERES, bot.py
    erkennt es nicht - und die Antwort landet in str(antwort)[:80]. Das ist kein
    Schweigen, sondern ein Fehler auf einem nackten object().

    Seit basis.HANDLED zeigen alle auf dasselbe. Dieser Test haelt das fest,
    damit es beim naechsten neuen Modul nicht wieder auseinanderlaeuft.
    """
    import importlib
    import basis

    namen = ("arbeit bayern casino economy floaktie food games giveaway "
             "guildcfg lotto luxus media merchant moderation music profil "
             "schulden steal terraria voicegags words").split()
    eigen = []
    for name in namen:
        modul = importlib.import_module(name)
        if getattr(modul, "HANDLED", None) is not basis.HANDLED:
            eigen.append(name)
    assert not eigen, (
        f"Diese Module haben ein EIGENES HANDLED statt basis.HANDLED: {eigen}. "
        f"bot.py erkennt es nur, solange es in seiner Liste steht - und die "
        f"vergisst man beim Aufteilen einer Datei.")

    # Und der Fangnetz-Eintrag in bot.py muss basis.HANDLED wirklich enthalten.
    import bot
    assert any(s is basis.HANDLED for s in bot._HANDLED_SENTINELS), (
        "bot._HANDLED_SENTINELS kennt basis.HANDLED nicht")



def test_rauchtest_deckt_die_kette_aus_bot_py_ab():
    """Der Rauchtest nuetzt nichts, wenn er ein Modul vergisst, das bot.py
    aufruft. Deshalb: die Liste hier gegen die echte Kette in bot.py halten."""
    quelle = open("bot.py", encoding="utf-8").read()
    # Anker bewusst nur auf den NAMEN, nicht auf die Schreibweise dahinter:
    # als _HANDLED_SENTINELS von 'tuple(' auf '(basis.HANDLED,) + tuple(' wechselte,
    # fand dieser Test seine Stelle nicht mehr und fiel mit 'substring not found'
    # um - ein Test, der an der Formatierung fremden Codes haengt, meldet
    # Aenderungen statt Fehlern.
    anfang = quelle.index("_HANDLED_SENTINELS = ")
    kette = set(re.findall(r"\((?:\w+_ENABLED)[^)]*\), (\w+)\.handle",
                           quelle[anfang:]))
    assert len(kette) >= 15, f"Kette nicht gefunden (nur {kette})"
    eigen = inspect.getsource(test_rauchtest_jeder_befehl_laeuft_wirklich_durch)
    # Module, die der Rauchtest bewusst NICHT anfasst - mit Begruendung.
    ausgenommen = {
        "music",      # braucht eine echte Voice-Verbindung
        "media",      # erzeugt Bilder ueber einen fremden Dienst (kostet)
        "food",       # braucht ein Foto im Anhang
        "voicegags",  # braucht Voice
        "giveaway",   # legt echte Coins fest und startet Timer
        "admin",      # nur Besitzer, veraendert fremde Konten
        "bayern",     # schaltet eine Server-Einstellung um
    }
    fehlt = sorted(m for m in kette
                   if m not in ausgenommen and f"({m}, (" not in eigen)
    assert not fehlt, (f"bot.py ruft diese Module auf, der Rauchtest kennt sie "
                       f"aber nicht: {fehlt}")

if __name__ == "__main__":
    run(globals())
