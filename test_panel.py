"""Web-Panel und BotSicht.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur panel      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _botsicht_umgebung, _with_economy)



def test_webpanel_update_nur_einmal_gleichzeitig():
    """Zwei gleichzeitige Update-Klicks duerfen nicht zwei git-pulls starten.

    Zwei offene Tabs (oder ein zweites Geraet) haetten sonst zwei Pulls im selben
    Arbeitsverzeichnis losgeschickt; git legt dann index.lock an und der zweite
    Lauf bricht mit einer Meldung ab, die niemand einordnen kann."""
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = 0
        gestartet = []

        async def langsam(request):
            """Tut so, als liefe der Pull - lange genug fuer den zweiten Klick."""
            gestartet.append(1)
            await asyncio.sleep(0.25)
            return webpanel.web.json_response({"ok": True, "changed": False,
                                               "log": "test"})

        wp._update_lauf = langsam
        async with TestClient(TestServer(wp._build_app())) as c:
            a, b = await asyncio.gather(
                c.post("/api/update", json={"restart": False}),
                c.post("/api/update", json={"restart": False}),
            )
            codes = sorted([a.status, b.status])
            texte = [await a.json(), await b.json()]
            return codes, len(gestartet), texte

    codes, laeufe, texte = asyncio.run(lauf())
    assert codes == [200, 409], codes
    assert laeufe == 1, (laeufe, texte)          # nur EIN git pull
    abgelehnt = [t for t in texte if not t.get("ok")]
    assert abgelehnt and "läuft bereits" in abgelehnt[0].get("error", ""), texte

    # Und danach geht es wieder.
    codes2, laeufe2, _ = asyncio.run(lauf())
    assert laeufe2 == 1, laeufe2




def test_webpanel_api():
    """Web-Panel-Backend: Login-Gate, Overview/Users, Coins geben/nehmen/setzen,
    XP, Titel geben, Server-Liste, Sendepause - alles hinter Token-Auth."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001 - ohne aiohttp-Testutils ueberspringen
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return

    restore_eco = _with_economy({1: 1000, 2: 5000})
    economy.instance._profile(1)["name"] = "Alice"
    economy.instance._profile(2)["name"] = "Bob"
    import admin
    alt_admin = (admin.instance._enabled, admin.instance._store)
    admin.instance._enabled = True
    admin.instance._store = _FakeStore({"sendepause": False})

    wp = webpanel.instance
    alt = (wp._enabled, wp._user, wp._pass, dict(wp._tokens), wp._client)
    wp._enabled = True
    wp._user, wp._pass = "Secoolio", "Secoolio"
    wp._tokens = {}
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: None, get_channel=lambda _x: None)
    app = wp._build_app()

    # Dieser Test prueft den Login-Weg - dafuer muss die Login-Pflicht an sein
    # (im Betrieb ist sie standardmaessig aus, siehe test_webpanel_ohne_login).
    wp._auth = True

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            # Auth-Gate: ohne Token/Cookie 401 (vor dem Login pruefen).
            assert (await cli.get("/api/overview")).status == 401
            # Login: falsch -> 401, richtig -> Token.
            assert (await cli.post("/api/login", json={"user": "x", "pass": "y"})).status == 401
            r = await cli.post("/api/login", json={"user": "Secoolio", "pass": "Secoolio"})
            j = await r.json()
            assert j["ok"] and j["token"]
            H = {"Authorization": f"Bearer {j['token']}"}

            # Overview.
            j = await (await cli.get("/api/overview", headers=H)).json()
            assert j["ok"] and j["stats"]["users"] >= 2 and j["stats"]["coins_total"] >= 6000
            assert any(u["id"] == "2" for u in j["top_coins"])

            # Nutzerliste + Detail.
            j = await (await cli.get("/api/users?sort=coins", headers=H)).json()
            assert j["ok"] and j["total"] >= 2
            j = await (await cli.get("/api/user/1", headers=H)).json()
            assert j["ok"] and j["user"]["id"] == "1" and "owned" in j["user"]

            # Coins geben / nehmen / setzen.
            j = await (await cli.post("/api/user/coins",
                       json={"id": "1", "action": "give", "amount": 500}, headers=H)).json()
            assert j["ok"] and j["coins"] == 1500
            j = await (await cli.post("/api/user/coins",
                       json={"id": "1", "action": "take", "amount": 200}, headers=H)).json()
            assert j["coins"] == 1300
            j = await (await cli.post("/api/user/coins",
                       json={"id": "1", "action": "set", "amount": "9k"}, headers=H)).json()
            assert j["coins"] == 9000        # '9k' geparst

            # XP geben (Level steigt).
            j = await (await cli.post("/api/user/xp",
                       json={"id": "1", "action": "give", "amount": 1000}, headers=H)).json()
            assert j["ok"] and j["level"] >= 1

            # Titel geben.
            j = await (await cli.post("/api/user/title",
                       json={"id": "1", "action": "grant", "text": "Testi",
                             "label": "🧪 Testi", "rarity": "selten"}, headers=H)).json()
            assert j["ok"] and economy.owns_title(1, "Testi")

            # Server-Liste (leer, aber ok).
            j = await (await cli.get("/api/servers", headers=H)).json()
            assert j["ok"] and j["guilds"] == []

            # Sendepause schalten.
            j = await (await cli.post("/api/server/sendepause",
                       json={"on": True}, headers=H)).json()
            assert j["ok"] and j["sendepause"] is True and admin.is_locked() is True

            # --- Profilbilder für die Nutzer-Liste (/api/avatar/<id>) -------
            alt_res = economy.instance._resolve_avatar_user

            class _Asset:
                url = "https://cdn.discordapp.com/avatars/1/abc.png?size=64"

                def with_size(self, _n):
                    return self

            async def fake_resolve(_guild, uid):
                return SimpleNamespace(display_avatar=_Asset()) if uid == 1 else None
            economy.instance._resolve_avatar_user = fake_resolve
            wp._av_cache = {}
            try:
                # Auflösbar -> Weiterleitung (302) auf die Discord-CDN-URL.
                r = await cli.get("/api/avatar/1", headers=H, allow_redirects=False)
                assert r.status == 302, r.status
                assert "cdn.discordapp.com" in r.headers.get("Location", "")
                # Nicht auflösbar -> 404 (Panel zeigt dann die Initialen).
                assert (await cli.get("/api/avatar/999", headers=H,
                                      allow_redirects=False)).status == 404
                # Unsinnige ID -> 404, kein Crash.
                assert (await cli.get("/api/avatar/abc", headers=H,
                                      allow_redirects=False)).status == 404
            finally:
                economy.instance._resolve_avatar_user = alt_res
                wp._av_cache = {}

            # --- Aktien-Anteile per Panel korrigieren (Exploit-Aufräumen) ---
            import floaktie
            alt_fa = (floaktie.instance._store, floaktie.instance._enabled)
            floaktie.instance._enabled = True
            floaktie.instance._store = _FakeStore(
                {"price": 300000, "day": "x", "act_ema": floaktie.ACT_BASELINE,
                 "msg_count": 0, "last_msg_count": 0,
                 "holdings": {"1": 120000, "2": 5000}, "history": [], "ticks": []})
            floaktie.instance._base()
            floaktie.instance._sync_price()
            try:
                kurs_vor = floaktie.instance.price()
                # 120.000 Exploit-Anteile streichen - Kurs muss STABIL bleiben.
                j = await (await cli.post("/api/user/shares",
                           json={"id": "1", "action": "set", "amount": 0,
                                 "keep_price": True}, headers=H)).json()
                assert j["ok"] and j["shares"] == 0
                assert j["total_shares"] == 5000
                assert abs(j["price"] - kurs_vor) <= max(1, kurs_vor // 1000)
                # geben / nehmen
                j = await (await cli.post("/api/user/shares",
                           json={"id": "1", "action": "give", "amount": "100"}, headers=H)).json()
                assert j["shares"] == 100
                j = await (await cli.post("/api/user/shares",
                           json={"id": "1", "action": "take", "amount": "40"}, headers=H)).json()
                assert j["shares"] == 60
                # Kurs direkt setzen
                j = await (await cli.post("/api/stock/price",
                           json={"price": "1000"}, headers=H)).json()
                assert j["ok"] and j["price"] == 1000 and floaktie.instance.price() == 1000
                # Unsinn wird abgelehnt
                assert (await cli.post("/api/user/shares",
                        json={"id": "1", "action": "quatsch", "amount": "5"},
                        headers=H)).status == 400
                assert (await cli.post("/api/stock/price",
                        json={"price": "0"}, headers=H)).status == 400
            finally:
                floaktie.instance._store, floaktie.instance._enabled = alt_fa

    asyncio.run(run_it())
    wp._enabled, wp._user, wp._pass, wp._tokens, wp._client = alt
    admin.instance._enabled, admin.instance._store = alt_admin
    restore_eco()




def test_webpanel_eingaben_und_robustheit():
    """REGRESSION (Panel-Backend): unlesbare Eingaben duerfen NIE still etwas
    aendern, absurde Zahlen muessen abgelehnt werden (bevor Daten angefasst
    werden), kaputte Profile duerfen keine Liste abschiessen, und ein Login mit
    Umlaut-Passwort darf nicht in einen 500er laufen."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    import floaktie

    restore_eco = _with_economy({1: 5000, 2: 100})
    economy.instance._profile(1)["name"] = "Zacharias"
    economy.instance._profile(2)["name"] = "Äpfelchen"
    # Kaputtes Profil (wie nach einem Absturz): coins = None.
    economy.instance._users()["3"] = {"name": "Kaputt", "coins": None, "xp": "nix"}

    wp = webpanel.instance
    alt = (wp._enabled, wp._user, wp._pass, dict(wp._tokens), wp._client, dict(wp._fails))
    alt_fa = (floaktie.instance._store, floaktie.instance._enabled)
    gesendet = []

    class _Chan:
        async def send(self, text, **kw):
            gesendet.append((text, kw))

    system_chan = _Chan()
    guild = SimpleNamespace(id=7, system_channel=system_chan)
    wp._enabled = True
    wp._user, wp._pass = "Secoolio", "Pässwörtchen"      # Nicht-ASCII!
    wp._tokens, wp._fails = {}, {}
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: guild,
                                 get_channel=lambda _x: None)
    floaktie.instance._enabled = True
    floaktie.instance._store = _FakeStore(
        {"price": 1000, "base": 1000.0, "day": "x", "act_ema": floaktie.ACT_BASELINE,
         "msg_count": 0, "last_msg_count": 0,
         "holdings": {"1": 10, "4242": 9999}, "history": [], "ticks": []})
    floaktie.instance._sync_price()
    app = wp._build_app()
    os.environ["GUILD_ID"] = "7"

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            # Login mit Umlaut-Passwort: falsch -> 401 (kein 500), richtig -> Token.
            r = await cli.post("/api/login", json={"user": "Secoolio", "pass": "falsch"})
            assert r.status == 401, r.status
            r = await cli.post("/api/login", json={"user": "Secoolio", "pass": "Pässwörtchen"})
            assert r.status == 200, r.status
            H = {"Authorization": f"Bearer {(await r.json())['token']}"}

            # 1) UNLESBARER Betrag -> 400, Kontostand unveraendert.
            #    Vorher wurde daraus 0: 'set' hat still das ganze Guthaben geloescht.
            for murks in ("abc", "1 000", "", None, {"a": 1}, True, float("nan")):
                r = await cli.post("/api/user/coins",
                                   json={"id": "1", "action": "set", "amount": murks},
                                   headers=H)
                assert r.status == 400, (murks, r.status)
            assert economy.get_coins(1) == 5000

            # 2) Absurd grosser Betrag -> 400 (kein OverflowError, kein Wert-Muell).
            assert (await cli.post("/api/user/coins",
                    json={"id": "1", "action": "give", "amount": "9" * 40},
                    headers=H)).status == 400
            assert economy.get_coins(1) == 5000

            # 3) Ungueltige IDs -> 400 und KEIN Geister-Profil in der Datenbank.
            for bad in ("-1", "0", "0123", "abc", "", None, "1.5"):
                assert (await cli.post("/api/user/coins",
                        json={"id": bad, "action": "give", "amount": 10},
                        headers=H)).status == 400, bad
            assert "0" not in economy.instance._users()
            assert "123" not in economy.instance._users()

            # 4) Riesige Anteils-Zahl -> 400, und die Aktie bleibt handelbar.
            #    Vorher wurde das Depot VOR der Kursrechnung geaendert, die dann
            #    mit OverflowError starb: danach war jeder Kauf/Verkauf kaputt.
            assert (await cli.post("/api/user/shares",
                    json={"id": "1", "action": "set", "amount": "9" * 400},
                    headers=H)).status == 400
            assert (await cli.post("/api/user/shares",
                    json={"id": "1", "action": "set", "amount": 1e30},
                    headers=H)).status == 400
            assert floaktie.instance.shares_of(1) == 10
            j = await (await cli.post("/api/user/shares",
                       json={"id": "1", "action": "give", "amount": 5}, headers=H)).json()
            assert j["ok"] and j["shares"] == 15 and j["price"] > 0
            # Verkauf bringt weiter echte Coins (kein Gleitkomma-Kollaps).
            erloes, _ = floaktie.instance._sell_proceeds(5)
            assert erloes > 0

            # 5) keep_price:"false" ist FALSE (vorher machte der String True).
            floaktie.instance._holdings()["1"] = 4000
            floaktie.instance._sync_price()
            kurs_vor = floaktie.instance.price()
            j = await (await cli.post("/api/user/shares",
                       json={"id": "1", "action": "set", "amount": 0,
                             "keep_price": "false"}, headers=H)).json()
            assert j["ok"] and j["price"] < kurs_vor, (j["price"], kurs_vor)

            # 6) Kurs-Setzen: zu gross -> 400; unter Mindestkurs -> ehrliche Antwort.
            assert (await cli.post("/api/stock/price",
                    json={"price": "9" * 40}, headers=H)).status == 400
            j = await (await cli.post("/api/stock/price",
                       json={"price": 1}, headers=H)).json()
            assert j["price"] == floaktie.MIN_PRICE and j["requested"] == 1
            # Und ein niedriger Kurs laesst sich auch bei vielen Anteilen setzen.
            floaktie.instance._holdings()["1"] = 500_000
            j = await (await cli.post("/api/stock/price",
                       json={"price": 100}, headers=H)).json()
            assert j["price"] == 100, j["price"]
            floaktie.instance._holdings()["1"] = 10
            await floaktie.admin_set_price(1000)

            # 7) Kaputtes Profil + reiner Aktien-Halter: Listen laufen durch,
            #    der Halter ohne economy-Profil ist SICHTBAR (zaehlt ja im Wert).
            j = await (await cli.get("/api/users?size=100", headers=H)).json()
            assert j["ok"]
            ids = [u["id"] for u in j["users"]]
            assert "3" in ids and "4242" in ids, ids
            assert next(u for u in j["users"] if u["id"] == "3")["coins"] == 0
            assert next(u for u in j["users"] if u["id"] == "4242")["shares"] == 9999
            j = await (await cli.get("/api/overview", headers=H)).json()
            assert j["ok"] and any(u["id"] == "4242" for u in j["top_shares"])
            assert (await cli.get("/api/user/4242", headers=H)).status == 200
            assert (await cli.get("/api/user/3", headers=H)).status == 200

            # 8) Zu hohe Seitenzahl wird auf den gueltigen Bereich gezogen
            #    (vorher: leere Liste ohne Blaetter-Knoepfe = Sackgasse).
            j = await (await cli.get("/api/users?page=99&size=5", headers=H)).json()
            assert j["ok"] and j["page"] == j["pages"] and j["users"]
            # Kaputtes 'size' darf die Seite nicht mitreissen.
            j = await (await cli.get("/api/users?page=2&size=abc", headers=H)).json()
            assert j["page"] == 2 or j["pages"] == 1

            # 9) Namens-Sortierung deutsch: Ä sortiert wie A, also VOR Z.
            j = await (await cli.get("/api/users?sort=name&size=100", headers=H)).json()
            namen = [u["name"] for u in j["users"]]
            assert namen.index("Äpfelchen") < namen.index("Zacharias"), namen

            # 10) XP: Murks -> 400, unbekannte Aktion -> 400, 'take' rechnet runter.
            assert (await cli.post("/api/user/xp",
                    json={"id": "1", "action": "set", "amount": "abc"},
                    headers=H)).status == 400
            assert (await cli.post("/api/user/xp",
                    json={"id": "1", "action": "quatsch", "amount": 5},
                    headers=H)).status == 400
            await cli.post("/api/user/xp", json={"id": "1", "action": "set", "amount": 500},
                           headers=H)
            j = await (await cli.post("/api/user/xp",
                       json={"id": "1", "action": "take", "amount": 200}, headers=H)).json()
            assert j["xp"] == 300

            # 11) Titel: 'remove' meldet ehrlich, ob der Titel da war.
            j = await (await cli.post("/api/user/title",
                       json={"id": "1", "action": "remove", "text": "GibtsNicht"},
                       headers=H)).json()
            assert j["ok"] and j["removed"] is False
            economy.grant_title(1, "Echt", "Echt", "selten")
            j = await (await cli.post("/api/user/title",
                       json={"id": "1", "action": "remove", "text": "Echt"},
                       headers=H)).json()
            assert j["removed"] is True
            # Unsinnige Seltenheit -> 400.
            assert (await cli.post("/api/user/title",
                    json={"id": "1", "action": "grant", "text": "X", "rarity": "quatsch"},
                    headers=H)).status == 400

            # 12) Ansage: unbekannte Kanal-ID landet NICHT still im System-Kanal.
            assert (await cli.post("/api/server/announce",
                    json={"text": "hallo", "channel_id": "123456789012345"},
                    headers=H)).status == 400
            assert gesendet == []
            # Objekt als Text -> 400 (vorher landete ein Python-repr im Chat).
            assert (await cli.post("/api/server/announce",
                    json={"text": {"a": 1}}, headers=H)).status == 400
            # Ohne Kanal-ID: System-Kanal, gekuerzt auf Discord-Laenge.
            j = await (await cli.post("/api/server/announce",
                       json={"text": "x" * 5000}, headers=H)).json()
            assert j["ok"] and len(gesendet) == 1 and len(gesendet[0][0]) <= 1900
            # KEINE Massen-Pings aus dem Panel.
            erlaubt = gesendet[0][1].get("allowed_mentions")
            assert erlaubt is not None and erlaubt.everyone is False

            # 13) Sendepause-Zustand kommt jetzt mit der Server-Liste (kein Cache).
            j = await (await cli.get("/api/servers", headers=H)).json()
            assert "sendepause" in j

            # 14) Login-Bremse: nach genug Fehlversuchen 429 statt endlos raten.
            wp._fails = {}
            for _ in range(webpanel.WebPanel._LOGIN_MAX_FAILS):
                await cli.post("/api/login", json={"user": "x", "pass": "y"})
            assert (await cli.post("/api/login",
                    json={"user": "x", "pass": "y"})).status == 429
            wp._fails = {}

    try:
        asyncio.run(run_it())
    finally:
        os.environ.pop("GUILD_ID", None)
        floaktie.instance._store, floaktie.instance._enabled = alt_fa
        (wp._enabled, wp._user, wp._pass, wp._tokens, wp._client, wp._fails) = alt
        restore_eco()




def test_musik_panel_view_wird_abgemeldet():
    """Jedes Now-Playing-Panel laeuft mit timeout=None und wurde deshalb von
    discord.py NIE aus dem ViewStore genommen - auch das Loeschen der Nachricht
    raeumt dort nichts weg. Gemessen: 200 Panels = 200 Eintraege, die auch nach
    dem Loeschen aller Referenzen blieben. Mit jedem gespielten Song einer mehr."""
    import discord
    import music

    class FakeState:
        def __init__(self):
            self._view_store = discord.ui.view.ViewStore(self)

        def store_view(self, view, message_id=None, interaction_id=None):
            self._view_store.add_view(view, message_id=message_id)

    st = FakeState()

    class P:
        def __init__(self):
            self.volume = 1.0
            self.speed = 1.0
            self.voice = None
            self.current = None
            self.queue = []
            self.panel_message = None
            self.panel_view = None

        def is_active(self):
            return False

    p = P()

    async def lauf():
        for i in range(50):
            v = music.PlaybackControlView(p)
            st.store_view(v, message_id=1000 + i)
            p.panel_view = v
            p.panel_message = None
            await music.instance._retire_panel(p)

    asyncio.run(lauf())
    assert len(st._view_store._views) == 0, len(st._view_store._views)
    assert p.panel_view is None




def test_arzt_findet_das_panel_passwort():
    """Das Panel wuerfelt ohne WEBPANEL_PASS ein Passwort und schreibt es EINMAL
    beim Start ins Log. Ohne einen Weg dorthin ist es praktisch unauffindbar:
    'k l' sucht nur KI-Zeilen, und im vollen Journal geht die eine Zeile
    zwischen den Zugriffszeilen des Panels unter. Genau das ist passiert."""
    import re
    import webpanel
    arzt = open("k", encoding="utf-8").read()
    muster = re.search(r'PANELMUSTER="([^"]+)"', arzt)
    assert muster, "in 'k' gibt es kein PANELMUSTER"
    filter_re = re.compile(muster.group(1))
    assert "p|panel|passwort)" in arzt, "es gibt keinen Unterbefehl fuer das Panel"

    # Jede Panel-Meldung, die beim Start faellt, muss damit auffindbar sein.
    quelle = inspect.getsource(webpanel.WebPanel.setup)
    meldungen = re.findall(r'log\.[a-z]+\(\s*"([^"]*)', quelle)
    wichtig = [m for m in meldungen if "Web-Panel" in m or "WEBPANEL" in m]
    assert wichtig, "setup() meldet gar nichts ueber das Panel"
    fehlt = [m for m in wichtig if not filter_re.search(m)]
    assert not fehlt, f"'k p' zeigt diese Zeilen nicht: {fehlt}"




def test_webpanel_nur_fuer_den_besitzer():
    """Im Panel werden Coins vergeben, Titel verteilt und der Bot neu gestartet -
    das darf nur der Besitzer. Deshalb ist der Login jetzt der STANDARD.

    Wichtiger als der Schalter ist aber das Passwort: ein festes
    Standardpasswort im Quelltext waere das Schlimmste von beidem - es sieht
    nach Schutz aus und ist keiner. Ohne WEBPANEL_PASS wuerfelt Flo deshalb
    eins und schreibt es EINMAL ins Log."""
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    async def lauf(auth):
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._user, wp._pass, wp._auth = "u", "p", auth
        # /api/update fuehrt ein ECHTES 'git pull --ff-only' im Bot-Verzeichnis
        # aus. Ohne dieses Double zieht ein Testlauf auf dem Server also Code -
        # und die README verspricht ausdruecklich, dass ein Testlauf dort
        # ungefaehrlich ist. Nachgewiesen an der Zeile
        # "Panel-Update: git pull ok (schon aktuell)" im Testprotokoll.
        async def kein_git(_request):
            return webpanel.web.json_response(
                {"ok": True, "changed": False, "log": "(Test-Double)"})
        wp._update_lauf = kein_git
        async with TestClient(TestServer(wp._build_app())) as c:
            cfg = await (await c.get("/api/config")).json()
            codes = {}
            for pfad, meth in (("/api/overview", "get"), ("/api/features", "get"),
                               ("/api/user/coins", "post"), ("/api/update", "post")):
                r = await getattr(c, meth)(pfad, json={})
                codes[pfad] = r.status
            return cfg, codes

    # Mit Login (Standard) ist alles dicht.
    cfg, codes = asyncio.run(lauf(True))
    assert cfg["ok"] is True and cfg["auth"] is True
    assert all(v == 401 for v in codes.values()), codes

    # Abschalten geht weiterhin - fuer einen rein lokalen Aufbau.
    cfg, codes = asyncio.run(lauf(False))
    assert cfg["auth"] is False
    assert all(v != 401 for v in codes.values()), codes
    assert codes["/api/overview"] == 200

    quelle = open("webpanel.py", encoding="utf-8").read()
    # 1. Der Standard ist AN.
    assert 'os.getenv("WEBPANEL_AUTH", "1")' in quelle, "der Login ist nicht mehr Standard"
    # 2. KEIN festes Passwort im Quelltext. Genau das war vorher der Fall
    #    (WEBPANEL_PASS", "Secoolio") - ein Login mit bekanntem Passwort ist
    #    kein Login.
    assert 'os.getenv("WEBPANEL_PASS", "")' in quelle, (
        "es steht wieder ein festes Standardpasswort im Quelltext")
    assert "secrets.token_urlsafe" in quelle, (
        "ohne gesetztes Passwort muss eins gewuerfelt werden")
    # 3. Wer den Login ABschaltet, soll es laut im Log sehen.
    assert "OHNE Login" in quelle
    # 4. /api/config ist selbst nie geschuetzt - sonst koennte die Oberflaeche
    #    gar nicht herausfinden, ob sie einen Anmeldebildschirm zeigen muss.
    i = quelle.index("async def _api_config")
    assert "_guard" not in quelle[i:i + 400]

    # Die Oberflaeche fragt /api/config, bevor sie den Anmeldebildschirm zeigt.
    html = open("webpanel.html", encoding="utf-8").read()
    assert "/api/config" in html and "S.authNoetig" in html




def test_webpanel_nimmt_keine_fremden_formulare_an():
    """Ohne Login (WEBPANEL_AUTH=0) gibt es kein Cookie, das schuetzen koennte.
    Dann reicht ein

        <form action="http://192.168.x.x:9123/api/user/coins"
              method="post" enctype="text/plain">

    auf irgendeiner Seite, die der Besitzer im selben Netz oeffnet - der Browser
    schickt den POST mit. Ein Browser-Formular kann aber KEIN
    application/json senden, und genau daran ist es zu erkennen."""
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = False                      # der ungeschuetzte Fall
        async with TestClient(TestServer(wp._build_app())) as c:
            # So sendet ein Formular von einer fremden Seite.
            formular = await c.post("/api/user/coins", data="id=1&amount=999",
                                    headers={"Content-Type": "text/plain"})
            # So sendet die eigene Oberflaeche.
            echt = await c.post("/api/user/coins", json={})
            return formular.status, echt.status

    formular, echt = asyncio.run(lauf())
    assert formular == 415, f"fremdes Formular kam durch (HTTP {formular})"
    assert echt != 415, f"die eigene Oberflaeche wird blockiert (HTTP {echt})"

    # Und die Oberflaeche MUSS den Content-Type auch ohne Body setzen - sonst
    # waere der naechste POST ohne Body ein stiller 415.
    html = open("webpanel.html", encoding="utf-8").read()
    assert "if(aendernd) opts.headers[\"Content-Type\"]=\"application/json\";" in html




def test_webpanel_token_deckel():
    """Die Token-Tabelle darf nicht unbegrenzt wachsen (Prozess laeuft monatelang)
    und abgelaufene Tokens muessen verschwinden."""
    import webpanel
    wp = webpanel.WebPanel()
    wp._ttl = 60
    wp._tokens = {"alt": time.time() - 5}          # abgelaufen
    for _ in range(wp._TOKEN_MAX + 20):
        wp._new_token()
    assert "alt" not in wp._tokens
    assert len(wp._tokens) <= wp._TOKEN_MAX




def test_webpanel_haelt_jeden_json_body_aus():
    """Gueltiges, aber nicht-objektes JSON ([1,2,3], null, 42, "x") liess
    JEDEN der elf POST-Endpunkte mit HTTP 500 platzen: request.json() wirft
    dabei nicht, das folgende data.get() schon."""
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    restore = _with_economy({1: 100})
    wp = webpanel.instance
    alt = (wp._enabled, wp._auth, wp._client, dict(wp._tokens))
    wp._enabled, wp._auth = True, False
    wp._tokens = {}
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: None, get_channel=lambda _x: None)
    app = wp._build_app()

    pfade = ["/api/user/coins", "/api/user/xp", "/api/user/title", "/api/user/shares",
             "/api/stock/price", "/api/server/sendepause", "/api/server/announce",
             "/api/feature", "/api/guildcfg", "/api/login"]

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            for pfad in pfade:
                for body in ("[1,2,3]", "null", "42", '"x"', "true"):
                    r = await cli.post(pfad, data=body,
                                       headers={"Content-Type": "application/json"})
                    assert r.status != 500, f"{pfad} mit {body} -> HTTP 500"
                # Und kaputtes JSON darf auch nicht 500 werden.
                r = await cli.post(pfad, data="{kaputt",
                                   headers={"Content-Type": "application/json"})
                assert r.status != 500, f"{pfad} mit kaputtem JSON -> HTTP 500"

    try:
        asyncio.run(run_it())
    finally:
        (wp._enabled, wp._auth, wp._client, wp._tokens) = alt
        restore()




def test_giveaway_panel_behaelt_seinen_loeschschutz():
    """_protect gibt beim Schuetzen das VORIGE Panel desselben Slots frei - so
    ist es gedacht ("es kann nur EINS je Slot aktuell sein"). Aber ALLE
    Sendestellen liefen ueber denselben Standard-Slot: jede Assistenten-Frage
    gab damit den Schutz des LAUFENDEN Giveaway-Panels frei.

    In einem Aufraeum-Kanal verschwand danach genau die Nachricht mit dem
    Mitmach-Knopf - waehrend die Coins weiter hinterlegt blieben."""
    import giveaway
    gw = giveaway.instance
    geschuetzt = []
    alt_protect = gw._protect
    gw._protect = lambda msg, **kw: geschuetzt.append(kw.get("slot", "panel"))
    try:
        zaehler = {"n": 0}

        async def send(**_k):
            zaehler["n"] += 1
            return SimpleNamespace(id=zaehler["n"])

        kanal = SimpleNamespace(id=77, send=send)

        async def lauf():
            await gw._send(kanal, embed=None, slot="gw:5")   # das Panel
            await gw._send(kanal, embed=None)                # Assistenten-Frage
            await gw._send(kanal, embed=None)                # noch eine

        asyncio.run(lauf())
    finally:
        gw._protect = alt_protect

    assert geschuetzt[0] == "gw:5", geschuetzt
    assert geschuetzt[1] == geschuetzt[2] == "wizard:77", geschuetzt
    assert geschuetzt[0] not in geschuetzt[1:], (
        "Panel und Assistenten-Fragen teilen sich wieder einen Slot - die "
        "naechste Frage raeumt das laufende Giveaway weg")




def test_panel_protokolliert_und_sichert():
    """Jede SCHREIBENDE Panel-Aktion landet im Protokoll, und das Backup
    liefert wirklich ein ZIP.

    Das Protokoll ist kein Login-Thema (den gibt es hier bewusst nicht),
    sondern Nachvollziehbarkeit: wer spaeter wissen will, warum ein Konto
    5 Mio mehr hat, findet es hier. Es haengt an einer Middleware, damit auch
    der naechste neue Knopf mit protokolliert wird."""
    import pathlib
    import tempfile
    import store
    import webpanel
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    restore = _with_economy({1: 100})
    wp = webpanel.instance
    alt = (wp._enabled, wp._auth, wp._client, dict(wp._tokens), list(wp._log),
           wp._log_store, store.DATA_DIR)
    d = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d
    (d / "economy.json").write_text('{"users":{}}', encoding="utf-8")
    (d / "games.json").write_text('{"counting":{}}', encoding="utf-8")
    wp._enabled, wp._auth = True, False
    wp._tokens, wp._log, wp._log_store = {}, [], None
    wp._client = SimpleNamespace(guilds=[], is_closed=lambda: False,
                                 get_guild=lambda _x: None, get_channel=lambda _x: None)
    app = wp._build_app()

    async def run_it():
        async with TestClient(TestServer(app)) as cli:
            await cli.post("/api/user/coins", json={"uid": 1, "delta": 5000})
            await cli.get("/api/features")           # Lesen wird NICHT notiert
            r = await cli.get("/api/log")
            daten = await r.json()
            eintraege = daten["eintraege"]
            assert len(eintraege) == 1, eintraege
            assert eintraege[0]["pfad"] == "/api/user/coins"
            assert eintraege[0]["daten"]["delta"] == 5000
            # Backup: echtes ZIP mit den Dateien drin.
            r = await cli.get("/api/backup")
            assert r.status == 200
            roh = await r.read()
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(roh)) as zf:
                assert set(zf.namelist()) >= {"economy.json", "games.json"}
            assert "flobot-backup-" in r.headers.get("Content-Disposition", "")

    try:
        asyncio.run(run_it())
    finally:
        (wp._enabled, wp._auth, wp._client, wp._tokens, wp._log,
         wp._log_store, store.DATA_DIR) = alt
        restore()




def test_botsicht_zeigt_was_flo_sieht():
    """BotSicht liefert Server, Kanalbaum, Verlauf und Mitglieder - und zwar
    aus Flos Blickwinkel:

    * Ein Kanal ohne Leserecht wird MITGELIEFERT und als gesperrt markiert
      (weglassen waere bequemer und genau falsch - die Frage 'warum sagt Flo
      da nichts?' beantwortet sich nur, wenn man den Kanal sieht).
    * Der Verlauf kommt aeltest-zuerst, nicht so, wie Discord ihn liefert.
    * Flos eigene Nachricht ist als solche markiert.
    * Die Luecke zwischen 'Mitglieder laut Server' und 'Flo kennt' bleibt
      sichtbar, statt kaschiert zu werden."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    wp, _gesendet, _offen, _live = _botsicht_umgebung()
    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            j = await (await cli.get("/api/sicht/guilds")).json()
            assert j["ok"] and j["ich"]["name"] == "Flo", j
            g = j["guilds"][0]
            assert g["mitglieder"] == 120 and g["bekannt"] == 2, g
            assert j["intents"]["mitglieder"] is False, j["intents"]

            j = await (await cli.get("/api/sicht/channels?guild=10")).json()
            assert [c["name"] for c in j["text"]] == ["allgemein", "geheim"], j["text"]
            assert j["text"][0]["rechte"]["verlauf"] is True
            assert j["text"][1]["rechte"]["verlauf"] is False, "gesperrter Kanal fehlt"
            assert j["voice"][0]["drin"][0]["name"] == "Alice", j["voice"]

            j = await (await cli.get("/api/sicht/messages?channel=100")).json()
            assert [m["text"] for m in j["messages"]] == \
                ["erste `code`", "zweite <@1> hi"], j["messages"]
            assert j["messages"][0]["autor"]["eigen"] is True, j["messages"][0]["autor"]

            # Gesperrter Kanal: 403 mit Begruendung, NICHT eine leere Liste.
            r = await cli.get("/api/sicht/messages?channel=101")
            assert r.status == 403, r.status
            j = await r.json()
            assert j["gesperrt"] and "Verlauf" in j["error"], j

            j = await (await cli.get("/api/sicht/members?guild=10")).json()
            assert j["gesamt"] == 120 and len(j["mitglieder"]) == 2, j

            assert (await cli.get("/api/sicht/messages?channel=999")).status == 404
            assert (await cli.get("/api/sicht/channels?guild=77")).status == 404
    asyncio.run(lauf())




def test_botsicht_schreibt_aber_pingt_nie_alle():
    """Aus dem Panel als Flo schreiben - mit fest zugenagelten Erwaehnungen.

    @everyone aus dem Eingabefeld laesst sich nicht zurueckholen; ein
    Tippfehler soll nicht den halben Server aufwecken. Einzelne Leute duerfen
    sehr wohl gepingt werden, sonst kann man hier nicht mitreden."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    wp, gesendet, _offen, _live = _botsicht_umgebung()
    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            j = await (await cli.post("/api/sicht/send",
                       json={"channel": "100", "text": "@everyone Achtung"})).json()
            assert j["ok"], j
            text, kw = gesendet[0]
            assert text == "@everyone Achtung"
            am = kw["allowed_mentions"]
            assert am.everyone is False and am.roles is False, "Massen-Ping moeglich!"
            assert am.users is True and am.replied_user is True

            # Antwort setzt eine Referenz. Sie MUSS eine MessageReference sein:
            # discord.py ruft to_message_reference_dict() auf, und die hat nur
            # diese Klasse. Frueher stand hier ein discord.Object - der Test
            # fragte '.id' ab und war damit gruen, waehrend in Wirklichkeit
            # JEDE Antwort mit einem TypeError scheiterte. Die Zusicherung hat
            # den Fehler also nicht gefunden, sondern festgeschrieben.
            await cli.post("/api/sicht/send",
                           json={"channel": "100", "text": "dazu", "reply_to": "9001"})
            import discord as _d
            verweis = gesendet[1][1]["reference"]
            assert isinstance(verweis, _d.MessageReference), type(verweis)
            assert verweis.message_id == 9001, gesendet[1][1]
            assert verweis.to_message_reference_dict()["message_id"] == 9001

            # Leerer Text und unbekannter Kanal werden abgewiesen.
            assert (await cli.post("/api/sicht/send",
                    json={"channel": "100", "text": "   "})).status == 400
            assert (await cli.post("/api/sicht/send",
                    json={"channel": "999", "text": "x"})).status == 404
    asyncio.run(lauf())




def test_botsicht_live_strom_und_protokoll():
    """Der Live-Strom sammelt, was Flo sieht, und 'seit' liefert nur Neues.

    Ausserdem: das Panel-Protokoll haelt das Senden fest (jemand hat im Namen
    des Bots geschrieben - das gehoert nachvollziehbar), aber NICHT das
    Tipp-Zeichen. Das feuert bei jedem Tastendruck und wuerde das Protokoll
    in zwei Minuten vollschreiben."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    wp, _gesendet, _offen, live = _botsicht_umgebung()
    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            wp.sicht_notiere(live)
            j = await (await cli.get("/api/sicht/feed")).json()
            assert len(j["ereignisse"]) == 1 and j["nr"] == 1, j
            assert j["ereignisse"][0]["msg"]["text"] == "live", j

            # 'seit' filtert das schon Gesehene weg.
            j2 = await (await cli.get(f"/api/sicht/feed?seit={j['nr']}")).json()
            assert j2["ereignisse"] == [], j2

            await cli.post("/api/sicht/send", json={"channel": "100", "text": "hi"})
            await cli.post("/api/sicht/typing", json={"channel": "100"})
            j3 = await (await cli.get(f"/api/sicht/feed?seit={j['nr']}")).json()
            assert len(j3["ereignisse"]) == 1, j3
            assert j3["ereignisse"][0]["msg"]["text"] == "hi", j3
    asyncio.run(lauf())

    pfade = [e["pfad"] for e in wp._log]
    assert "/api/sicht/send" in pfade, pfade
    assert "/api/sicht/typing" not in pfade, "Tipp-Zeichen flutet das Protokoll"




def test_botsicht_ueberlebt_kaputte_nachrichten():
    """Eine Nachricht mit kaputtem Anhang darf nicht den ganzen Verlauf
    mitreissen - und sicht_notiere laeuft im heissen Pfad von on_message,
    darf also unter keinen Umstaenden nach oben durchschlagen."""
    import unittest.mock as mock
    import discord
    wp, _g, _o, _l = _botsicht_umgebung()

    kaputt = mock.MagicMock(spec=discord.Message)
    kaputt.id = 1
    # Jeder Zugriff auf .author fliegt - schlimmer geht es kaum.
    type(kaputt).author = property(lambda _s: (_ for _ in ()).throw(RuntimeError("weg")))
    wp.sicht_notiere(kaputt)          # darf NICHT werfen
    assert wp._sicht_nr == 0, "kaputte Nachricht wurde trotzdem gezaehlt"

    # Auch ohne laufenden Loop (Tests, Shell) bleibt es lautlos.
    wp.sicht_notiere(_l)
    assert wp._sicht_nr == 1 and len(wp._sicht) == 1

    # Ist das Panel aus, wird gar nichts aufbereitet.
    wp._enabled = False
    wp.sicht_notiere(_l)
    assert wp._sicht_nr == 1, "abgeschaltetes Panel sammelt trotzdem"




def test_botsicht_live_strom_haelt_die_reihenfolge():
    """Jede offene Leitung hat eine eigene Warteschlange und EINEN Schreiber.

    Der erste Entwurf hat pro Nachricht ein create_task(ws.send_json(...))
    abgesetzt. Zwei kurz hintereinander erzeugte Tasks koennen sich beim
    Schreiben aber ueberholen - dann steht im Panel die Antwort vor der Frage.
    Ausserdem konnte ein langsamer Browser den Bot mit beliebig vielen Tasks
    zumuellen; jetzt ist bei _SICHT_STAU Schluss und das AELTESTE faellt raus."""
    import webpanel
    wp, _g, _o, _l = _botsicht_umgebung()

    # SimpleNamespace geht hier NICHT: es definiert __eq__ und ist damit
    # unhashbar - als Schluessel im Verbindungs-Dict also unbrauchbar.
    class Leitung:
        def __init__(self):
            self.closed = False

    async def lauf():
        ws = Leitung()
        schlange = asyncio.Queue(maxsize=5)
        wp._sicht_ws = {ws: schlange}
        for i in range(5):
            wp._sicht_push({"nr": i})
        assert schlange.qsize() == 5
        # Voll: die naechsten drei verdraengen die aeltesten drei.
        for i in range(5, 8):
            wp._sicht_push({"nr": i})
        raus = [schlange.get_nowait()["nr"] for _ in range(5)]
        assert raus == [3, 4, 5, 6, 7], raus

        # Geschlossene Leitungen fliegen beim naechsten Schub raus.
        ws.closed = True
        wp._sicht_push({"nr": 99})
        assert wp._sicht_ws == {}, wp._sicht_ws
    asyncio.run(lauf())

    # Und ohne jede Leitung tut _sicht_push schlicht nichts (heisser Pfad).
    wp._sicht_ws = {}
    wp._sicht_push({"nr": 1})




def test_botsicht_dm_gedaechtnis():
    """Discord verraet einem Bot seine DM-Kanaele nicht - Flo fuehrt die Liste
    selbst. Hier: merken, doppelt zaehlen, Deckel, und wer bei einer DM
    eigentlich der Partner ist (bei Flos eigener Nachricht der Empfaenger,
    sonst der Absender)."""
    import unittest.mock as mock
    import discord
    wp, _g, _o, _l = _botsicht_umgebung()
    wp._dm_store = _FakeStore({"partner": {}})
    wp._dm_partner = {}

    def person(uid, name):
        u = mock.MagicMock(spec=discord.User)
        u.id, u.name, u.display_name, u.bot = uid, name, name, False
        u.display_avatar = SimpleNamespace(url=f"https://cdn.test/{uid}.png")
        return u

    marvin = person(222333444555666777, "Marvin")
    assert wp._dm_merken(marvin, ts=1000) is True, "erster Kontakt ist neu"
    assert wp._dm_merken(marvin, ts=2000) is False, "zweiter Kontakt ist nicht neu"
    e = wp._dm_partner["222333444555666777"]
    assert e["anzahl"] == 2 and e["zuerst"] == 1000 and e["zuletzt"] == 2000, e
    assert e["name"] == "Marvin"

    # Flo selbst ist nie sein eigener DM-Partner.
    ich = person(99, "Flo")
    assert wp._dm_merken(ich) is False and "99" not in wp._dm_partner

    # Eine DM VON jemandem: Partner ist der Absender.
    kanal = mock.MagicMock(spec=discord.DMChannel)
    kanal.id, kanal.recipient = 700, marvin
    rein = mock.MagicMock(spec=discord.Message)
    rein.author, rein.channel, rein.guild = marvin, kanal, None
    rein.created_at = SimpleNamespace(timestamp=lambda: 3000)
    wp._dm_aus_nachricht(rein)
    assert wp._dm_partner["222333444555666777"]["zuletzt"] == 3000

    # Eine DM VON FLO: Partner ist der Empfaenger des Kanals, nicht Flo.
    raus = mock.MagicMock(spec=discord.Message)
    raus.author, raus.channel, raus.guild = ich, kanal, None
    raus.created_at = SimpleNamespace(timestamp=lambda: 4000)
    wp._dm_aus_nachricht(raus)
    assert wp._dm_partner["222333444555666777"]["zuletzt"] == 4000
    assert "99" not in wp._dm_partner, "Flo hat sich selbst eingetragen"

    # Deckel: die aelteste Bekanntschaft faellt raus, nicht die neueste.
    import webpanel
    alt = webpanel.BOTSICHT_DM_MAX
    try:
        webpanel.BOTSICHT_DM_MAX = 3
        for i, uid in enumerate([10**17 + n for n in range(4)]):
            wp._dm_merken(person(uid, f"P{i}"), ts=5000 + i)
        assert len(wp._dm_partner) == 3, len(wp._dm_partner)
        assert str(10**17) not in wp._dm_partner, "aeltester blieb stehen"
        assert str(10**17 + 3) in wp._dm_partner, "neuester fehlt"
    finally:
        webpanel.BOTSICHT_DM_MAX = alt




def test_botsicht_dm_suche_kennt_die_echten_formate():
    """Die Wiederherstellung alter DMs haengt an zwei Mustern - und die muessen
    WOERTLICH zu dem passen, was der Bot schreibt bzw. der Besitzer tippt.
    Deshalb wird das Vergleichsmaterial hier aus bot.py gebaut, nicht von Hand
    abgetippt: aendert dort jemand den Text, faellt es hier auf und nicht erst,
    wenn die Suche nichts mehr findet."""
    import webpanel
    quelle = open("bot.py", encoding="utf-8").read()
    # Der Weiterleitungs-Text steht so in _forward_dm_to_owner:
    assert 'f"📥 **DM von {message.author.display_name}** "' in quelle, \
        "Format der DM-Weiterleitung hat sich geaendert - Regex nachziehen!"
    assert 'f"(`{message.author.id}`):' in quelle, \
        "Format der DM-Weiterleitung hat sich geaendert - Regex nachziehen!"

    name, uid = "Marvin", 222333444555666777
    echt = f"📥 **DM von {name}** (`{uid}`):\nhallo, bist du da?"
    treffer = webpanel.WebPanel._DM_RELAY_RE.findall(echt)
    assert treffer == [(name, str(uid))], treffer

    # Auch mit Leerzeichen/Emoji im Anzeigenamen.
    echt2 = f"📥 **DM von Lena ✨ (sie)** (`{uid}`):\ntext"
    assert webpanel.WebPanel._DM_RELAY_RE.findall(echt2) == [("Lena ✨ (sie)", str(uid))]

    # Und die 'dm'-Befehle des Besitzers, beide Schreibweisen.
    b = webpanel.WebPanel._DM_BEFEHL_RE
    assert b.findall(f"flo dm {uid} sag mal") == [str(uid)]
    assert b.findall(f"Flo dm <@{uid}> sag mal") == [str(uid)]
    assert b.findall(f"flo dm <@!{uid}> sag mal") == [str(uid)]
    # Kein Fehlalarm bei normalen Saetzen.
    assert b.findall("ich hab dm 42 gesagt") == [], "zu kurze Zahl wurde genommen"




def test_botsicht_dm_verlauf_und_liste():
    """Der DM-Verlauf wird ueber die Nutzer-ID geholt - create_dm() macht den
    Kanal notfalls auf, und Discord liefert dann den KOMPLETTEN Verlauf, auch
    von vor dem letzten Neustart. Genau das ist der Weg zurueck."""
    try:
        from aiohttp.test_utils import TestClient, TestServer
    except Exception:  # noqa: BLE001
        print("   (aiohttp test utils fehlen - uebersprungen)")
        return
    import unittest.mock as mock
    import discord
    wp, _g, _o, _l = _botsicht_umgebung()
    wp._dm_store = _FakeStore({"partner": {}})
    wp._dm_partner = {}

    marvin = mock.MagicMock(spec=discord.User)
    marvin.id, marvin.name, marvin.display_name, marvin.bot = \
        222333444555666777, "Marvin", "Marvin", False
    marvin.display_avatar = SimpleNamespace(url="https://cdn.test/m.png")

    dmk = mock.MagicMock(spec=discord.DMChannel)
    dmk.id, dmk.recipient = 700, marvin

    def dmnachricht(mid, text, autor, t):
        m = mock.MagicMock(spec=discord.Message)
        m.id, m.content, m.author, m.channel, m.guild = mid, text, autor, dmk, None
        m.created_at = SimpleNamespace(timestamp=lambda t=t: t)
        m.edited_at, m.pinned = None, False
        m.attachments, m.embeds, m.reactions, m.reference = [], [], [], None
        m.type = discord.MessageType.default
        return m

    flo = wp._client.user
    alt = [dmnachricht(2, "und? kommst du?", marvin, 1600000100.0),
           dmnachricht(1, "Nein.", flo, 1600000000.0)]

    async def hist(**kw):
        for m in alt[:kw.get("limit", 50)]:
            yield m
    dmk.history = lambda **kw: hist(**kw)
    gesendet = []

    async def send(text, **kw):
        gesendet.append((text, kw))
        return dmnachricht(3, text, flo, 1600000200.0)
    dmk.send = send
    marvin.dm_channel = dmk
    wp._client.get_user = lambda uid: marvin if uid == marvin.id else None

    app = wp._build_app()

    async def lauf():
        async with TestClient(TestServer(app)) as cli:
            # Von Hand hinzufuegen - der Notausgang, wenn man die ID kennt.
            j = await (await cli.post("/api/sicht/dm",
                       json={"id": str(marvin.id)})).json()
            assert j["ok"] and j["leer"] is False and j["name"] == "Marvin", j

            j = await (await cli.get("/api/sicht/dms")).json()
            assert len(j["partner"]) == 1, j
            assert j["partner"][0]["quelle"] == "hand", j["partner"][0]

            # Verlauf ueber die ID - aeltest zuerst, wie ueberall.
            j = await (await cli.get(f"/api/sicht/messages?dm={marvin.id}")).json()
            assert [m["text"] for m in j["messages"]] == ["Nein.", "und? kommst du?"], j
            # Der Kanalname nennt die Person, nicht nur "Direktnachricht".
            assert j["messages"][0]["kanal_name"] == "DM · Marvin", j["messages"][0]
            assert j["messages"][0]["dm_mit"] == str(marvin.id)

            # Privat antworten.
            j = await (await cli.post("/api/sicht/send",
                       json={"dm": str(marvin.id), "text": "doch"})).json()
            assert j["ok"] and gesendet[0][0] == "doch", j
            assert gesendet[0][1]["allowed_mentions"].everyone is False

            # Unsinnige ID -> 400, nicht 500.
            assert (await cli.post("/api/sicht/dm", json={"id": "abc"})).status == 400
    asyncio.run(lauf())




def test_botsicht_haengt_im_bot_ganz_oben():
    """Der Aufruf in bot.py muss VOR dem Bot-Check stehen.

    Sonst zeigt die Ansicht eine gefilterte Wahrheit: Flos eigene Antworten
    und die anderer Bots fehlten, obwohl er sie sehr wohl sieht."""
    quelle = open("bot.py", encoding="utf-8").read()
    start = quelle.index("async def on_message(self, message)")
    rumpf = quelle[start:start + 4000]
    hook = rumpf.index("webpanel.sicht_notiere(message)")
    botcheck = rumpf.index("if message.author.bot:")
    assert hook < botcheck, "sicht_notiere steht hinter dem Bot-Check"




def test_botsicht_antwort_kommt_wirklich_raus():
    """Antworten aus der BotSicht scheiterten AUSNAHMSLOS. Der Code reichte ein
    discord.Object als 'reference' weiter, discord.py ruft darauf aber
    to_message_reference_dict() auf - die Methode hat Object nicht:

        TypeError: reference parameter must be Message, MessageReference,
                   or PartialMessage

    Das landete im breiten except, der Nutzer sah "senden fehlgeschlagen" und
    suchte den Fehler bei sich. Der Kommentar im Code behauptete ausdruecklich
    das Gegenteil - deshalb hier ein Test, der discord.py selbst fragt."""
    import discord
    # 1. Die Tatsache, auf der alles beruht.
    assert not hasattr(discord.Object(id=1), "to_message_reference_dict")
    verweis = discord.MessageReference(message_id=1, channel_id=2, guild_id=3,
                                       fail_if_not_exists=False)
    daten = verweis.to_message_reference_dict()
    assert daten["message_id"] == 1 and daten["fail_if_not_exists"] is False

    # 2. Der Code baut wirklich eine MessageReference, nicht ein Object.
    quelle = open("webpanel.py", encoding="utf-8").read()
    stelle = quelle.index('kwargs["reference"]')
    block = quelle[stelle:stelle + 320]
    assert "discord.MessageReference(" in block, block[:200]
    assert "discord.Object" not in block, block[:200]
    # Und das Versprechen "faellt auf eine normale Nachricht zurueck" muss auch
    # eingeloest sein - sonst scheitert die Antwort weiterhin, nur spaeter.
    assert "fail_if_not_exists=False" in block, block[:300]




def test_wordle_knopf_im_panel_loest_wirklich_aus():
    """Der Knopf im Web-Panel muss den Bot erreichen - und ein bekannter Grund
    ("schon geloest", "Kanal fehlt") muss als lesbarer Satz ankommen, nicht als
    nichtssagender Serverfehler."""
    import asyncio as _asyncio
    import webpanel
    from aiohttp.test_utils import TestClient, TestServer

    gerufen = []

    class FakeBot:
        async def wordle_jetzt(self, gid, cid=0):
            gerufen.append((gid, cid))
            if gid == 999:
                raise ValueError("Das Wort von heute ist schon geloest.")
            return "Wort des Tages in #gigachat gestartet."

    async def lauf():
        wp = webpanel.WebPanel()
        wp._enabled = True
        wp._auth = 0
        wp._client = FakeBot()
        async with TestClient(TestServer(wp._build_app())) as c:
            gut = await c.post("/api/wordle/start",
                               json={"guild": 1453867645660303527,
                                     "channel_id": "1453881901738889351"})
            bekannt = await c.post("/api/wordle/start", json={"guild": 999})
            krumm = await c.post("/api/wordle/start",
                                 json={"guild": 1, "channel_id": "keine-zahl"})
            ohne = await c.post("/api/wordle/start", json={})
            return ([gut.status, await gut.json()],
                    [bekannt.status, await bekannt.json()],
                    [krumm.status, await krumm.json()],
                    ohne.status)

    alt_gid = os.environ.pop("GUILD_ID", None)
    try:
        gut, bekannt, krumm, ohne = _asyncio.run(lauf())
    finally:
        if alt_gid is not None:
            os.environ["GUILD_ID"] = alt_gid

    assert gut[0] == 200, gut
    assert gut[1]["ok"] is True and "gigachat" in gut[1]["text"], gut
    assert gerufen[0] == (1453867645660303527, 1453881901738889351), gerufen

    # Ein bekannter Grund ist KEIN Serverfehler - sonst steht im Panel nur
    # "senden fehlgeschlagen" und niemand weiss, warum.
    assert bekannt[0] == 400, bekannt
    assert "geloest" in bekannt[1]["error"].lower(), bekannt

    # Eine krumme Kanal-ID darf nicht still im Standard-Kanal landen. Der
    # Bot darf dafuer GAR NICHT erst gerufen werden - sonst haenge der Aushang
    # im falschen Kanal, und zurueckholen laesst sich das nicht.
    assert krumm[0] == 400, krumm
    assert [g for g, _c in gerufen] == [1453867645660303527, 999], (
        f"die krumme Kanal-ID hat den Bot erreicht: {gerufen}")

    # Ohne Server und ohne GUILD_ID: ablehnen statt raten.
    assert ohne == 400, ohne




def test_wordle_knopf_steht_wirklich_im_panel():
    """Ein Endpunkt ohne Knopf hilft niemandem: der Betreiber sitzt am Handy im
    Panel und soll das Wort dort ausloesen koennen, nicht per curl."""
    hier = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(hier, "webpanel.html"), encoding="utf-8").read()
    assert "/api/wordle/start" in html, "das Panel ruft den Endpunkt nirgends auf"
    assert "wdlGo" in html, "es gibt keinen Knopf"
    assert html.count('id="wdlGo"') == 1, "der Knopf steht doppelt im Panel"
    # Der Knopf muss den GEWAEHLTEN Server mitschicken - sonst landet das Wort
    # auf dem Hauptserver, egal welchen man im Panel geoeffnet hat.
    block = html.split("/api/wordle/start")[0][-400:]
    assert "guild:g.id" in block.replace(" ", ""), block[-200:]

    py = open(os.path.join(hier, "webpanel.py"), encoding="utf-8").read()
    assert '"/api/wordle/start"' in py, "die Route fehlt"
    assert "_api_wordle_start" in py
    # Schreibender Zugriff MUSS durch dieselbe Pruefung wie alles andere.
    rumpf = py.split("async def _api_wordle_start")[1].split("\n    async def")[0]
    assert "self._guard(request)" in rumpf, "der Endpunkt umgeht den Schutz"




def test_soundboard_hat_keinen_eigenen_speicher_mehr():
    """Zwei Wahrheiten fuer denselben Schalter sind genau der Grund, warum das
    Panel und Discord auseinanderlaufen konnten. voicegags darf keinen eigenen
    Konfigurations-Speicher mehr anlegen."""
    quelle = open("voicegags.py", encoding="utf-8").read()
    assert "JsonStore(" not in quelle, (
        "voicegags legt wieder einen eigenen Speicher an - der Schalter gehoert "
        "in guildcfg, sonst ist er im Panel nicht zu sehen")
    assert "set_soundboard" not in quelle
    # Und die Werte muessen bei JEDEM Gebrauch gelesen werden, nicht in setup().
    assert "self._join_sounds" not in quelle, (
        "die Join-Sounds werden wieder beim Start gemerkt - eine Aenderung im "
        "Panel wirkt dann erst nach einem Neustart")




def test_panel_laesst_sich_wirklich_bedienen():
    """Das Panel im echten Browser aufmachen und anklicken.

    Von allen Tests hier prueft sonst keiner die Oberflaeche des Panels - das
    sind rund 1.800 Zeilen Javascript, die alles Sichtbare per innerHTML
    zusammenbauen, und die einzige Absicherung war bisher: hinsehen. Ein
    Tippfehler in einem Selektor faellt in keinem Python-Test auf, und das
    Inventar sieht nur, ob es den ENDPUNKT noch gibt - nicht, ob der KNOPF ihn
    noch trifft.

    werkzeug/panelprobe.py faehrt den echten aiohttp-Server hoch, laedt die
    echte webpanel.html in Chromium und klickt sich durch. Laeuft als
    Unterprozess, weil ein Browser und ein Server im Testprozess nichts zu
    suchen haben.

    Fehlt Playwright oder Chromium, meldet die Probe das und gibt 0 zurueck -
    dann laeuft die Suite weiter, nur ohne dieses Netz.
    """
    import subprocess

    wurzel = os.path.dirname(os.path.abspath(__file__))
    probe = os.path.join(wurzel, "werkzeug", "panelprobe.py")
    if not os.path.exists(probe):
        return
    lauf = subprocess.run([sys.executable, probe, "--leise"], cwd=wurzel,
                          capture_output=True, text=True, timeout=900,
                          env=dict(os.environ, DATA_DIR=tempfile.mkdtemp(
                              prefix="flobot-panelprobe-test-")))
    assert lauf.returncode == 0, (
        "Das Panel laesst sich nicht mehr bedienen:\n"
        + (lauf.stdout or "")[-3000:] + (lauf.stderr or "")[-1500:])


if __name__ == "__main__":
    run(globals())
