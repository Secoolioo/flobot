"""Casino: alle Spiele und der Einsatzdeckel.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur casino      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _with_economy)



# --- Blackjack -------------------------------------------------------------
def test_blackjack_handwert():
    assert casino._hand_value([("A", "♠"), ("K", "♥")]) == 21
    assert casino._hand_value([("A", "♠"), ("A", "♥")]) == 12          # 11 + 1
    assert casino._hand_value([("A", "♠"), ("9", "♥"), ("5", "♦")]) == 15
    assert casino._hand_value([("10", "♠"), ("9", "♥"), ("5", "♦")]) == 24  # Bust




# --- Einsatz-Parsing ---------------------------------------------------------
def test_resolve_bet():
    assert casino._resolve_bet("50", 0) == 50
    assert casino._resolve_bet("abc", 0) is None
    assert casino._resolve_bet("", 0) is None
    # 'alles' ohne aktives economy: Kontostand 0 -> 0 (kein Crash)
    assert casino._resolve_bet("alles", 0) == 0




# --- Mines -------------------------------------------------------------------
def test_mines_multiplikator():
    # Ohne Pick kein Bonus.
    assert casino._mines_mult(0, 3) == 1.0
    # Streng steigend mit jedem sicheren Feld.
    vorher = 1.0
    for picked in range(1, casino._MINES_TILES - 3 + 1):
        m = casino._mines_mult(picked, 3)
        assert m > vorher, (picked, m, vorher)
        vorher = m
    # Mehr Bomben -> hoeherer Multiplikator beim gleichen Pick.
    assert casino._mines_mult(1, 5) > casino._mines_mult(1, 1)
    # Hausvorteil: erwarteter Wert eines 1-Feld-Picks liegt unter 1.
    # P(sicher) * mult = ((T-m)/T) * 0.97 * T/(T-m) = 0.97
    t, m = casino._MINES_TILES, 3
    p_sicher = (t - m) / t
    assert abs(p_sicher * casino._mines_mult(1, m) - 0.97) < 0.02




# --- Gluecksrad ---------------------------------------------------------------
def test_wheel_hausvorteil():
    segs = casino._WHEEL_SEGMENTS
    assert len(segs) == 12
    ev = sum(segs) / len(segs)
    assert 0.90 <= ev <= 0.99, ev              # kleiner Hausvorteil, kein Abzock-Rad
    assert any(m == 0 for m in segs)           # Nieten existieren
    assert max(segs) >= 2.0                    # aber auch echte Gewinne




# --- Rubbellos -----------------------------------------------------------------
def test_scratch_roll_konsistent():
    rng = random.Random(42)
    random.seed(42)
    for _ in range(200):
        keys, rows, mult = casino._scratch_roll()
        assert len(keys) == 9
        assert all(k in render.SLOT_KEYS for k in keys)
        for r in range(3):
            gewinn = keys[3 * r] == keys[3 * r + 1] == keys[3 * r + 2]
            assert (r in rows) == gewinn
        assert mult == sum(casino._SCRATCH_PAYOUT[keys[3 * r]] for r in rows)
    _ = rng  # nur zur Klarheit: Test nutzt globales random mit festem Seed




def test_scratch_hausvorteil():
    # Exakter Erwartungswert: 3 unabhaengige Reihen, P(Reihe aus Symbol s) = 1/343.
    n = len(render.SLOT_KEYS)
    ev = 3 * sum(casino._SCRATCH_PAYOUT[s] for s in render.SLOT_KEYS) / (n ** 3)
    assert 0.85 <= ev <= 1.0, ev




# --- Crash ---------------------------------------------------------------------
def test_crash_point_grenzen():
    random.seed(7)
    for _ in range(2000):
        cp = casino._crash_point()
        assert 1.0 <= cp <= 1000.0, cp




# --- Roulette --------------------------------------------------------------------
def test_roulette_auszahlung():
    payout, label = casino._roulette_payout("rot", 10, 1)      # 1 ist rot
    assert payout == 20 and label == "Rot"
    payout, _ = casino._roulette_payout("rot", 10, 2)          # 2 ist schwarz
    assert payout == 0
    payout, _ = casino._roulette_payout("gerade", 10, 0)       # 0 verliert immer
    assert payout == 0
    payout, label = casino._roulette_payout("17", 10, 17)
    assert payout == 360 and label == "Zahl 17"
    payout, _ = casino._roulette_payout("quatsch", 10, 17)
    assert payout is None




# --- Keno-Tabelle -----------------------------------------------------------------
def test_keno_tabelle():
    """Jede Tippanzahl muss denselben Rueckfluss haben. Vorher lag der RTP je nach
    Anzahl zwischen 0,29 (7 Zahlen - reine Falle) und 0,90 (2 Zahlen)."""
    from math import comb
    assert (3, 1) not in casino._KENO_TABLE   # zu wenig Treffer -> nichts

    def p(k, hit):                            # Ziehung: 10 aus 40
        return comb(k, hit) * comb(40 - k, 10 - hit) / comb(40, 10)

    for k in range(1, 9):
        stufen = sorted(h for (kk, h) in casino._KENO_TABLE if kk == k)
        assert stufen, k
        # Faktoren muessen mit den Treffern STEIGEN (sonst ist die Tabelle kaputt).
        werte = [casino._KENO_TABLE[(k, h)] for h in stufen]
        assert werte == sorted(werte) and len(set(werte)) == len(werte), (k, werte)
        assert stufen[-1] == k                # Volltreffer zahlt immer
        rtp = sum(p(k, h) * casino._KENO_TABLE[(k, h)] for h in stufen)
        assert 0.90 <= rtp <= 0.97, (k, round(rtp, 4))   # Hausvorteil ~6 %
        assert max(werte) <= 2000, (k, max(werte))        # kein Tail-Risk-Monster




# --- Casino-Bilanz: Gewonnen/Verloren-Summen -----------------------------------
def test_casino_bilanz_gewonnen_verloren():
    """record() zaehlt Brutto-Gewinne und -Verluste getrennt; Alt-Profile ohne
    die neuen Felder werden aus dem Netto geseedet; die Karte rendert damit."""
    class FakeStore:
        def __init__(self):
            self.data = {"stats": {}}

        async def save(self):
            pass

    alt_stats, alt_enabled = casino.instance._stats, casino.instance._enabled
    casino.instance._stats, casino.instance._enabled = FakeStore(), True
    try:
        asyncio.run(casino.record(1, "slots", 100, 250))   # +150 gewonnen
        asyncio.run(casino.record(1, "slots", 100, 0))     # -100 verloren
        asyncio.run(casino.record(1, "crash", 200, 0))     # -200 verloren
        asyncio.run(casino.record(1, "sieben", 50, 50))    # +-0 -> zaehlt nirgends
        prof = casino.instance._stats_profile(1)
        assert prof["games"] == 4 and prof["wagered"] == 450 and prof["payout"] == 300
        assert prof["won"] == 150 and prof["lost"] == 300
        assert prof["won"] - prof["lost"] == prof["payout"] - prof["wagered"]
        assert prof["best_win"] == 150
        # Migration: Alt-Profil ohne won/lost -> aus dem Netto geseedet.
        casino.instance._stats.data["stats"]["2"] = {
            "games": 5, "wagered": 1000, "payout": 1400, "best_win": 300, "per": {}}
        alt = casino.instance._stats_profile(2)
        assert alt["won"] == 400 and alt["lost"] == 0
        casino.instance._stats.data["stats"]["3"] = {
            "games": 2, "wagered": 500, "payout": 100, "best_win": 0, "per": {}}
        alt = casino.instance._stats_profile(3)
        assert alt["won"] == 0 and alt["lost"] == 400
        # Stats-Karte rendert die neuen Kennzahlen als PNG.
        buf = render.casino_stats_card("Tester", None, casino.instance._stats_profile(1))
        assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        casino.instance._stats, casino.instance._enabled = alt_stats, alt_enabled




def test_audit_geld_und_rechte():
    """REGRESSION aus dem grossen Audit: die fuenf Funde, die Geld oder Rechte
    betreffen. Jeder hier ist vorher nachgemessen worden."""
    import admin
    import casino
    import games

    # 1) Owner-Befehl 'gib @wer 5k' buchte still 5 Coins statt 5.000 (und
    #    '1.000.000' sogar 1) - und meldete gruen Erfolg.
    restore = _with_economy({1: 0})
    try:
        a = admin.instance
        ID = "<@1040135855710404659>"
        for text, erwartet in ((f"{ID} 5k", 5000), (f"{ID} 5000", 5000),
                               (f"{ID} 2m", 2_000_000), (f"{ID} 1.000.000", 1_000_000),
                               (f"{ID} 2,5k", 2500), (f"{ID} 750", 750)):
            uid, betrag = a._extract(text)
            assert uid == 1040135855710404659, text
            assert betrag == erwartet, (text, betrag, erwartet)
        # Ohne Betrag bleibt None (der Aufrufer fragt dann nach).
        assert a._extract(f"{ID} hallo")[1] is None
    finally:
        restore()

    # 2) Casino: EINE Runde konnte astronomisch auszahlen (Mines x179.213 bei
    #    Hoechsteinsatz = 179 Billionen Coins). Jetzt gedeckelt.
    restore = _with_economy({1: 0, 2: 0})
    try:
        mult = casino.instance._mines_mult(10, 10)
        assert mult > 100_000, mult                      # Jackpot existiert weiter
        roh = int(casino.MAX_BET * mult)
        gezahlt = casino._auszahlen(1, roh, "mines")
        assert gezahlt == casino.MAX_WIN < roh
        assert economy.get_coins(1) == casino.MAX_WIN
        # Normales Spiel bleibt unberuehrt.
        assert casino._auszahlen(2, 50_000, "test") == 50_000
        # Unsinn wird sauber verschluckt, statt zu crashen.
        for murks in (0, -5, None, float("inf"), float("nan"), "abc"):
            assert casino._auszahlen(2, murks, "test") == 0
        assert economy.get_coins(2) == 50_000
        # RTP bleibt fair (der Deckel greift nur im Extremfall).
        for bomben in (1, 3, 5, 10):
            frei = casino._MINES_TILES - bomben
            m = casino.instance._mines_mult(frei, bomben)
            chance = 1.0
            for i in range(frei):
                chance *= (casino._MINES_TILES - bomben - i) / (casino._MINES_TILES - i)
            assert 0.95 < m * chance < 1.0, (bomben, m * chance)
    finally:
        restore()

    # 3) Spiele-Wasserhaehne: Muenzwurf hatte 100 % RTP und im Text-Pfad keinen
    #    Hoechsteinsatz; Quiz/Zahlenraten/SSP waren pro Nutzer unbegrenzt farmbar.
    assert 0 < games.COINFLIP_PAYOUT < 1.0
    g = games.instance
    g._skill_cd = {}
    assert g._skill_frei(42) is True
    g._skill_cd[42] = time.monotonic()
    assert g._skill_frei(42) is False and g._skill_rest(42) > 0
    quelle = open("games.py", encoding="utf-8").read()
    # Die drei Belohnungen haengen jetzt alle an der Anti-Farm-Sperre - und
    # laufen zusaetzlich durch die TAGESKAPPE (_auszahlen).
    for stelle in ('QUIZ_REWARD, 0, "quiz"', 'reward, 0, "zahlenraten"',
                   '10, 0, "ssp"'):
        i = quelle.index(stelle)
        assert "_skill_frei" in quelle[max(0, i - 400):i], stelle
        assert "_auszahlen" in quelle[max(0, i - 120):i], stelle
    # Jede Auszahlung mit positivem Erwartungswert MUSS durch _auszahlen laufen,
    # sonst ist sie wieder ein Wasserhahn ohne Tageskappe.
    for stelle in ('"reaktion")', '"mathe")', '"anagramm")', '"event")'):
        assert stelle in quelle, stelle
    g._store = _FakeStore({"counting": {}})
    assert g._kappe_rest(4711) == games.GAMES_DAILY_MAX
    g._kappe_buchen(4711, games.GAMES_DAILY_MAX - 100)
    assert g._kappe_rest(4711) == 100
    g._kappe_buchen(4711, 999_999)
    assert g._kappe_rest(4711) == 0
    g._store = None

    # 4) Moderation: der Auto-Timeout nach Verwarnungen muss dieselbe Rangordnung
    #    achten wie ein direkter Timeout (sonst knebelt ein Junior-Mod einen Senior).
    mod_quelle = open("moderation.py", encoding="utf-8").read()
    # Am Verhalten festmachen, nicht am genauen Namen der Grenze: die stand
    # frueher als Konstante WARN_LIMIT da und kommt jetzt je Server aus
    # guildcfg. Der Test soll die RANGORDNUNG schuetzen, nicht die Schreibweise.
    treffer = re.search(r"if \(count >= \w+", mod_quelle)
    assert treffer, "die Auto-Timeout-Bedingung ist nicht mehr auffindbar"
    i = treffer.start()
    assert "darf_strafen" in mod_quelle[i - 400:i + 200]
    assert "full=True" in mod_quelle[i - 400:i]

    # 5) Panel-Ansage darf nie @everyone pingen.
    panel_quelle = open("webpanel.py", encoding="utf-8").read()
    i = panel_quelle.index("await channel.send(")
    assert "AllowedMentions.none()" in panel_quelle[i:i + 200]




def test_sonderziffern_sprengen_nichts():
    """str.isdigit() ist die falsche Pruefung vor int() - und hat Geld gekostet.

    "²".isdigit() ist True, int("²") wirft ValueError. Im Betrieb hiess das:
    'flo mines 100 ²' hat den Einsatz eingezogen und ist danach am Parsen
    gestorben - das Geld war weg, ohne dass je eine Runde lief. Betroffen waren
    14 Stellen in 7 Modulen."""
    import pathlib
    import numfmt

    # 1) Der Helfer selbst.
    for gut in ("42", "0", "7"):
        assert numfmt.ist_zahl(gut), gut
    for schlecht in ("²", "³", "5²", "１２３", "௫", "¹", "", " 7", "-3", "4.2",
                     None, 7, True):
        assert not numfmt.ist_zahl(schlecht), schlecht

    # 2) Was durchkommt, ueberlebt auch int().
    for s in ("0", "1", "42", "999999"):
        int(s)                                   # darf nicht werfen

    # 3) KEIN Modul darf sich mehr auf isdigit() verlassen (ausser numfmt selbst,
    #    das erklaert dort ja gerade warum).
    schuldige = []
    for p in sorted(pathlib.Path(".").glob("*.py")):
        if p.name.startswith("test_") or p.name == "numfmt.py":
            continue
        for i, zeile in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if ".isdigit()" in zeile:
                schuldige.append(f"{p.name}:{i}")
    assert not schuldige, ("isdigit() vor int() ist unsicher, nutze "
                           "numfmt.ist_zahl: " + ", ".join(schuldige))

    # 4) Der Mines-Parser nimmt die Sonderziffern nicht mehr an. args[0] ist der
    #    EINSATZ (den zieht der Aufrufer vorher schon ein), ab args[1] steht die
    #    Bombenzahl - genau dort ist der Befehl frueher gestorben, NACHDEM das
    #    Geld weg war.
    import casino
    for gift in ("²", "³", "5²", "௫"):
        n = casino.instance._parse_mines_count(["100", gift])
        assert n == casino._MINES_DEFAULT, (gift, n)
    assert casino.instance._parse_mines_count(["100", "3"]) == 3




def test_casino_deckel_stimmt_mit_dem_konto_ueberein():
    """_auszahlen deckelt auf MAX_WIN, aber alle Aufrufer verwarfen den
    Rueckgabewert: Embed und Bilanz meldeten den UNGEDECKELTEN Gewinn, das
    Konto bekam den gedeckelten. Anzeige und Wirklichkeit widersprachen sich."""
    import casino
    import economy
    restore = _with_economy({5: 0})
    alt_max = casino.MAX_WIN
    casino.MAX_WIN = 1000
    try:
        gemeldet = casino._auszahlen(5, 999_999)
        assert gemeldet == 1000, gemeldet
        assert economy.get_coins(5) == 1000, economy.get_coins(5)
        # Und der Normalfall bleibt unveraendert.
        gemeldet = casino._auszahlen(5, 500)
        assert gemeldet == 500 and economy.get_coins(5) == 1500
    finally:
        casino.MAX_WIN = alt_max
        restore()

    # Kein Aufrufer darf den Rueckgabewert noch wegwerfen.
    import re
    quelle = open("casino.py", encoding="utf-8").read()
    lose = [z.strip() for z in quelle.splitlines()
            if re.match(r"\s*_auszahlen\(", z) and "self.bet" not in z]
    assert not lose, f"_auszahlen-Rueckgabe ignoriert: {lose}"




def test_pvp_sieger_zahlt_keine_tilgung_auf_den_eigenen_einsatz():
    """Quizduell und Schere-Stein-Papier buchten den GANZEN Pot direkt per
    add_coins. Damit galt auch der eigene Einsatz des Siegers als Einnahme -
    wer Schulden hat, gibt davon 20 % ab.

    An echten Konten gemessen (Einsatz 1.000, Pot 2.000, Schuldner):
        direkt gebucht  -> netto +600
        ueber _auszahlen-> netto +800   (nur der GEWINN wird getilgt)

    Dazu lief die Tageskappe (GAMES_DAILY_MAX) an beiden Wegen vorbei, und die
    Ansage nannte den Einsatz als Gewinn statt des wirklich gezahlten Betrags."""
    import games
    import schulden
    economy.setup()
    schulden.setup()
    games.setup()
    sieger, glaeubiger = 770101, 770202
    economy.add_coins(sieger, 20_000 - economy.get_coins(sieger))
    economy.add_coins(glaeubiger, 1_000)
    schulden.instance.buch.anlegen(glaeubiger, sieger, 50_000, grund="Test")

    einsatz = 1_000
    vorher = economy.get_coins(sieger)
    economy.add_coins(sieger, -einsatz)              # Einsatz wird gezogen
    gezahlt = games._auszahlen(sieger, einsatz * 2, einsatz, "sspduell")
    netto = economy.get_coins(sieger) - vorher

    assert gezahlt == einsatz * 2, gezahlt
    assert netto == 800, (
        f"netto {netto} statt 800 - der Sieger zahlt Tilgung auf sein eigenes Geld")

    # Und beide PvP-Wege muessen wirklich ueber _auszahlen laufen.
    quelle = open("games.py", encoding="utf-8").read()
    fuer_pvp = (inspect.getsource(games.Games._check_qduel)
                + inspect.getsource(games._SSPDuel))
    assert "economy.add_coins(sieger.id, pot)" not in fuer_pvp
    assert "economy.add_coins(message.author.id, pot)" not in fuer_pvp
    assert fuer_pvp.count("_auszahlen(") >= 2, (
        "ein PvP-Weg bucht wieder direkt - dann zahlt der Sieger Tilgung auf "
        "seinen eigenen Einsatz")




def test_server_einsatzdeckel_gilt_auch_fuer_knoepfe():
    """Der je Server eingestellte Maximaleinsatz haengt an ai.aktuelle_guild().
    Die wird im GANZEN Repo an genau einer Stelle gesetzt: bot.on_message. Ein
    Klick kommt aber als eigenes Ereignis herein, ohne diesen Kontext.

    Nachgemessen bei Server-Deckel 1.000:
        max_bet(gid) = 1.000          max_bet() ohne Kontext = 1.000.000.000
        50.000 mit Guild  -> abgelehnt
        50.000 ohne Guild -> ANGENOMMEN

    Der Deckel galt damit nur fuer getippte Befehle. Ueber Menue, Dropdown
    "Alles" und "Nochmal" lief die Runde mit dem vollen Konto."""
    import guildcfg
    economy.setup()
    guildcfg.setup()
    casino.setup()
    gid, uid = 770077, 909090
    economy.add_coins(uid, 100_000_000 - economy.get_coins(uid))
    ok, wert, _fehler = asyncio.run(
        guildcfg.instance.setzen(gid, "casino_max_einsatz", "1000"))
    assert ok and wert == 1000, (ok, wert)
    try:
        # Mit Guild greift der Deckel.
        bet, fehler = casino.instance._check_bet(uid, 50_000, gid)
        assert bet == 0 and fehler and "Maximaleinsatz" in fehler, (bet, fehler)
        # Ein erlaubter Einsatz geht weiterhin durch.
        bet, fehler = casino.instance._check_bet(uid, 500, gid)
        assert bet == 500 and fehler is None, (bet, fehler)
        # Und der globale Deckel bleibt die Notbremse: ein Server darf strenger
        # sein als die .env, nie lockerer.
        assert casino.instance.max_bet(gid) <= casino.MAX_BET
    finally:
        asyncio.run(guildcfg.instance.loeschen(gid, "casino_max_einsatz"))

    # --- Die Regel, damit keine neue Knopf-Stelle sie vergisst -------------
    # In casino.py sind die beiden Wege sauber zu unterscheiden:
    #   self._check_bet(...)  = getippter Befehl, laeuft IN on_message (Kontext da)
    #   _check_bet(...)       = Knopf/Formular, eigener Task (kein Kontext)
    # Die zweite Form MUSS die Guild mitgeben.
    quelle = open("casino.py", encoding="utf-8").read()
    fehlt = []
    for treffer in re.finditer(r"(?<![\w.])_check_bet\(", quelle):
        if quelle[:treffer.start()].rstrip().endswith("def"):
            continue                      # die Definition selbst, kein Aufruf
        # Bis zur SCHLIESSENDEN Klammer lesen, nicht bis zur naechsten: die
        # Argumente enthalten selbst Klammern (self.params.get("bet")).
        tiefe, i = 0, treffer.end() - 1
        while i < len(quelle):
            if quelle[i] == "(":
                tiefe += 1
            elif quelle[i] == ")":
                tiefe -= 1
                if tiefe == 0:
                    break
            i += 1
        argumente = quelle[treffer.end():i]
        if "guild_id" not in argumente:
            zeile = quelle[:treffer.start()].count("\n") + 1
            fehlt.append(f"casino.py:{zeile}  _check_bet({' '.join(argumente.split())})")
    assert not fehlt, ("Knopf-Weg ohne Guild - der Server-Deckel gilt dort "
                       "nicht:\n  " + "\n  ".join(fehlt))


if __name__ == "__main__":
    run(globals())
