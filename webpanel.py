"""Lokales Web-Panel (Standard: Port 9123) zum Verwalten von Flo.

Laeuft IM Bot-Prozess (gleicher asyncio-Loop wie discord.py), operiert also direkt
auf den Live-Daten (economy, floaktie, lotto, merchant, admin, discord-Client) -
Aenderungen sind sofort wirksam und werden ganz normal gespeichert.

Features (JSON-API + schicke Single-Page-Oberflaeche webpanel.html):
- Uebersicht/Statistiken: Nutzer, Coins, Server, Aktie, Lotto, Level-Verteilung
- Nutzer verwalten: suchen, Profil ansehen, Flo Coins geben/nehmen/setzen,
  XP anpassen, Titel geben/nehmen
- Server verwalten: Guilds ansehen, Sendepause schalten, Ansage posten,
  Features schalten, per Knopf aktualisieren (git pull) und neu starten

SICHERHEIT - bitte lesen:
Das Panel verlangt standardmaessig einen LOGIN (WEBPANEL_AUTH=1): es darf nur
der Besitzer bedienen - dort werden Coins vergeben und der Bot neu gestartet.
Ist kein WEBPANEL_PASS gesetzt, wuerfelt Flo beim Start eines und schreibt es
EINMAL ins Log; ein festes Standardpasswort im Quelltext waere kein Schutz. Wer
die Adresse erreicht, kann damit alles: Coins vergeben, Ansagen im Namen des Bots
posten, den Kurs setzen, den Bot aktualisieren und neu starten.

Seit der BotSicht kommt dazu: den kompletten Chat MITLESEN (Verlauf und live),
im Namen des Bots in jeden Kanal schreiben, reagieren und Nachrichten loeschen.
Das ist Absicht - genau dafuer ist die Ansicht da -, hebt aber den Einsatz: der
Port gehoert NUR ins eigene Netz bzw. hinter die Firewall, niemals offen ins
Internet. Wer das nicht sicherstellen kann, setzt WEBPANEL_AUTH=1.

    WEBPANEL_AUTH=0      Login abschalten (nur fuer rein lokale Aufbauten)
    WEBPANEL_USER/PASS   Zugangsdaten fuer den Login
    WEBPANEL_HOST=127.0.0.1   nur lokal erreichbar
    WEBPANEL_ENABLED=0   Panel ganz aus

Host/Port: WEBPANEL_HOST / WEBPANEL_PORT (Standard 0.0.0.0:9123).
"""

import asyncio
import logging
import os
import re
import secrets
import time
import unicodedata
from collections import deque
from pathlib import Path

import discord

import numfmt

import economy
from basis import FeatureBasis

try:
    from aiohttp import web
except Exception:  # noqa: BLE001 - ohne aiohttp laeuft das Panel eben nicht
    web = None

log = logging.getLogger("dcbot.webpanel")

# So viele Panel-Aktionen bleiben im Protokoll stehen.
PANEL_LOG_MAX = int(os.getenv("PANEL_LOG_MAX", "200") or "200")

# BotSicht: so viele zuletzt gesehene Nachrichten haelt der Live-Strom vor.
# Das ist ein RAM-Puffer, keine Datei - was Flo sieht, ist fluechtig, und ein
# Mitschnitt des ganzen Servers auf der Platte will hier niemand.
BOTSICHT_PUFFER = int(os.getenv("BOTSICHT_PUFFER", "400") or "400")
# So viele Nachrichten holt die Verlaufs-Ansicht hoechstens auf einmal.
BOTSICHT_VERLAUF_MAX = 100
# So viele DM-Bekanntschaften merkt sich Flo (aelteste fliegen raus).
BOTSICHT_DM_MAX = int(os.getenv("BOTSICHT_DM_MAX", "500") or "500")
# So weit blaettert die Suche nach alten DMs hoechstens in die Owner-DM zurueck.
BOTSICHT_DM_SUCHE_MAX = 5000

_HTML_PATH = Path(__file__).resolve().parent / "webpanel.html"


class WebPanel(FeatureBasis):
    """Objektorientierte Huelle fuers Web-Panel (aiohttp-Server im Bot-Loop)."""

    def __init__(self):
        self._enabled = False
        self._runner = None
        self._client = None
        # Laeuft gerade ein 'git pull' ueber den Update-Knopf? Schuetzt davor,
        # dass zwei Tabs gleichzeitig im selben Verzeichnis ziehen.
        self._update_laeuft = False
        self._tokens = {}      # token -> Ablauf-Timestamp
        self._host = "0.0.0.0"
        self._port = 9123
        self._user = "Secoolio"
        self._pass = "Secoolio"
        self._ttl = 12 * 3600
        self._html_cache = None
        self._av_cache = {}    # uid -> (avatar_url|None, ablauf) fuer /api/avatar
        self._fails = {}       # ip -> (Fehlversuche, gesperrt_bis) gegen Raten
        self._auth = False     # Login verlangen? (WEBPANEL_AUTH, Standard aus)
        # Protokoll der schreibenden Panel-Aktionen (Nachvollziehbarkeit, kein
        # Login-Thema - den gibt es hier bewusst nicht).
        self._log = []
        self._log_store = None
        self._log_tasks = set()
        # --- BotSicht: der Live-Strom dessen, was Flo mitbekommt ----------
        self._sicht = deque(maxlen=max(20, BOTSICHT_PUFFER))
        self._sicht_ws = {}         # offene WebSocket-Verbindung -> ihre Warteschlange
        self._sicht_tasks = set()   # laufende Sende-Tasks (sonst sammelt der GC sie ein)
        self._sicht_nr = 0          # laufende Nummer, damit der Browser Luecken merkt
        # Mit wem hat Flo je privat geschrieben? Discord verraet das nicht -
        # er muss es selbst mitschreiben (siehe _dm_merken).
        self._dm_store = None
        self._dm_partner = {}
        self._dm_suche = None       # Stand der Suche nach alten DMs

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if os.getenv("WEBPANEL_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Web-Panel aus (WEBPANEL_ENABLED=0).")
            return False
        if web is None:
            log.warning("Web-Panel aus: aiohttp nicht verfuegbar.")
            return False
        # Protokoll laden (ueberlebt Neustarts).
        try:
            from store import JsonStore
            self._log_store = JsonStore("panel_log.json", default={"eintraege": []})
            roh = self._log_store.data.get("eintraege")
            self._log = [e for e in (roh if isinstance(roh, list) else [])
                         if isinstance(e, dict)][-PANEL_LOG_MAX:]
        except Exception:  # noqa: BLE001 - ein Protokoll ist nie start-kritisch
            log.exception("Panel-Protokoll konnte nicht geladen werden")
            self._log_store, self._log = None, []
        # DM-Gedaechtnis laden (mit wem hat Flo je privat geschrieben).
        try:
            from store import JsonStore
            self._dm_store = JsonStore("botsicht_dms.json", default={"partner": {}})
            roh = self._dm_store.data.get("partner")
            self._dm_partner = {str(k): v for k, v in (roh or {}).items()
                                if isinstance(v, dict)}
        except Exception:  # noqa: BLE001
            log.exception("DM-Gedaechtnis konnte nicht geladen werden")
            self._dm_store, self._dm_partner = None, {}
        self._host = os.getenv("WEBPANEL_HOST", "0.0.0.0").strip() or "0.0.0.0"
        try:
            self._port = int(os.getenv("WEBPANEL_PORT", "9123") or "9123")
        except (TypeError, ValueError):
            self._port = 9123
        if not 1 <= self._port <= 65535:
            log.warning("WEBPANEL_PORT=%s ist kein gueltiger Port - nehme 9123.", self._port)
            self._port = 9123
        self._user = os.getenv("WEBPANEL_USER", "Secoolio") or "Secoolio"
        self._pass = os.getenv("WEBPANEL_PASS", "").strip()
        # Login standardmaessig AN: das Panel darf ausdruecklich nur der
        # Besitzer bedienen. Ausschalten geht bewusst weiterhin (WEBPANEL_AUTH=0),
        # etwa fuer einen rein lokalen Aufbau.
        self._auth = os.getenv("WEBPANEL_AUTH", "1").strip().lower() in (
            "1", "true", "yes", "on")
        self._enabled = True
        log.info("Web-Panel bereit (startet in on_ready auf %s:%d).", self._host, self._port)
        if not self._auth:
            log.warning("Web-Panel OHNE Login (WEBPANEL_AUTH=0) - jeder, der "
                        "%s:%d erreicht, kann Coins vergeben und den Bot "
                        "neu starten.", self._host, self._port)
        elif not self._pass:
            # Kein Passwort gesetzt. Ein FESTES Standardpasswort waere hier das
            # Schlimmste von beidem: es sieht nach Schutz aus und ist keiner,
            # weil es im Quelltext steht. Also eins wuerfeln und EINMAL ins Log
            # schreiben - der Besitzer traegt es in die .env ein, dann bleibt es.
            self._pass = secrets.token_urlsafe(18)
            log.warning("Web-Panel: kein WEBPANEL_PASS gesetzt. Zugang fuer "
                        "diesen Start - Benutzer '%s', Passwort: %s",
                        self._user, self._pass)
            log.warning("Dauerhaft machen:  WEBPANEL_PASS=%s  in die .env "
                        "(sonst gilt bei jedem Neustart ein neues).", self._pass)
        return True

    def is_enabled(self):
        return self._enabled

    def _build_app(self):
        """Baut die aiohttp-App mit allen Routen (auch von Tests genutzt)."""
        app = web.Application(middlewares=[self._protokoll_middleware])
        app.add_routes([
            web.get("/", self._index),
            web.get("/panel", self._index),
            web.get("/api/config", self._api_config),
            web.post("/api/login", self._api_login),
            web.get("/api/overview", self._api_overview),
            web.get("/api/users", self._api_users),
            web.get("/api/user/{uid}", self._api_user),
            web.post("/api/user/coins", self._api_coins),
            web.post("/api/user/xp", self._api_xp),
            web.post("/api/user/title", self._api_title),
            web.post("/api/user/shares", self._api_shares),
            web.post("/api/stock/price", self._api_stock_price),
            web.get("/api/stock/series", self._api_stock_series),
            web.get("/api/servers", self._api_servers),
            web.post("/api/server/sendepause", self._api_sendepause),
            web.post("/api/server/announce", self._api_announce),
            web.post("/api/wordle/start", self._api_wordle_start),
            web.get("/api/avatar/{uid}", self._api_avatar),
            web.get("/api/features", self._api_features),
            web.post("/api/feature", self._api_feature),
            web.get("/api/guildcfg", self._api_guildcfg),
            web.post("/api/guildcfg", self._api_guildcfg_set),
            web.post("/api/update", self._api_update),
            web.get("/api/log", self._api_log),
            web.get("/api/backup", self._api_backup),
            # BotSicht: Discord aus Flos Blickwinkel
            web.get("/api/sicht/guilds", self._api_sicht_guilds),
            web.get("/api/sicht/channels", self._api_sicht_channels),
            web.get("/api/sicht/messages", self._api_sicht_messages),
            web.get("/api/sicht/members", self._api_sicht_members),
            web.get("/api/sicht/feed", self._api_sicht_feed),
            web.get("/api/sicht/ws", self._api_sicht_ws),
            web.post("/api/sicht/send", self._api_sicht_send),
            web.post("/api/sicht/typing", self._api_sicht_typing),
            web.post("/api/sicht/react", self._api_sicht_react),
            web.post("/api/sicht/delete", self._api_sicht_delete),
            web.get("/api/sicht/dms", self._api_sicht_dms),
            web.post("/api/sicht/dm", self._api_sicht_dm_merken),
            web.post("/api/sicht/dmsuche", self._api_sicht_dm_suche_start),
            web.get("/api/sicht/dmsuche", self._api_sicht_dm_suche),
        ])
        return app

    # --- Protokoll: was hat das Panel eigentlich getan? -------------------
    # /api/login: da stuende das Passwort drin.
    # /api/sicht/typing: das feuert bei jedem Tastendruck - nach zwei Minuten
    # Tippen waere das ganze Protokoll damit vollgeschrieben und alles andere
    # herausgerollt.
    _NICHT_NOTIEREN = ("/api/login", "/api/sicht/typing")

    @web.middleware
    async def _protokoll_middleware(self, request, handler):
        """Schreibt JEDE schreibende Panel-Aktion mit.

        Bewusst als Middleware und nicht in den elf Handlern einzeln: so ist
        auch der zwoelfte Knopf protokolliert, den irgendwann jemand nachruest.
        Das hat NICHTS mit Login zu tun (den gibt es hier absichtlich nicht) -
        es geht um Nachvollziehbarkeit: wer im Nachhinein wissen will, warum
        ein Konto 5 Mio mehr hat, findet es hier."""
        antwort = await handler(request)
        try:
            if request.method == "POST" and request.path not in self._NICHT_NOTIEREN:
                self._notiere(request, getattr(antwort, "status", 0))
        except Exception:  # noqa: BLE001 - das Protokoll darf nie stoeren
            log.debug("Panel-Protokoll fehlgeschlagen", exc_info=True)
        return antwort

    def _notiere(self, request, status):
        eintrag = {
            "t": int(time.time()),
            "pfad": request.path,
            "status": int(status or 0),
            "von": request.remote or "?",
            # Der Rumpf steht schon geparst im Request (siehe _json_objekt) -
            # nochmal lesen ginge auch gar nicht, der Strom ist verbraucht.
            "daten": request.get("_panel_daten") or {},
        }
        self._log.append(eintrag)
        del self._log[:-PANEL_LOG_MAX]
        if self._log_store is not None:
            self._log_store.data["eintraege"] = list(self._log)
            self._spawn_save()

    def _spawn_save(self, topf=None):
        """Speichert nebenher - der Knopfdruck soll nicht auf die Platte warten."""
        topf = topf if topf is not None else self._log_store
        if topf is None:
            return
        try:
            aufgabe = asyncio.get_running_loop().create_task(topf.save())
        except RuntimeError:
            return          # kein Loop (Tests)
        self._log_tasks.add(aufgabe)
        aufgabe.add_done_callback(self._log_tasks.discard)

    async def _api_log(self, request):
        """Die letzten Panel-Aktionen, neueste zuerst."""
        self._guard(request)
        return web.json_response({"ok": True, "eintraege": list(reversed(self._log))})

    async def _api_backup(self, request):
        """Alle data/*.json als ZIP zum Herunterladen.

        Reine LESE-Operation: es wird nichts angefasst, nur gepackt. Wer den
        Bot umzieht oder vor einem Reset sichergehen will, braucht dafuer sonst
        einen Shell-Zugang."""
        self._guard(request)
        import io
        import zipfile
        import store as store_modul
        puffer = io.BytesIO()
        dateien = sorted(store_modul.DATA_DIR.glob("*.json"))
        with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for pfad in dateien:
                try:
                    zf.write(pfad, arcname=pfad.name)
                except OSError:
                    log.warning("Backup: %s liess sich nicht lesen", pfad.name)
        stempel = time.strftime("%Y%m%d-%H%M%S")
        return web.Response(
            body=puffer.getvalue(),
            headers={"Content-Disposition":
                     f'attachment; filename="flobot-backup-{stempel}.zip"'},
            content_type="application/zip")

    async def start(self, client):
        """Startet den aiohttp-Server im laufenden Loop. Idempotent."""
        if not self._enabled or web is None or self._runner is not None:
            return
        self._client = client
        app = self._build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
            log.info("🌐 Web-Panel laeuft auf http://%s:%d (%s)",
                     self._host, self._port,
                     f"Login: {self._user}" if self._auth else "ohne Login")
        except Exception as exc:  # noqa: BLE001 - Port belegt/Adresse kaputt/...
            # Vorher nur OSError: bei einem kaputten Port (z. B. WEBPANEL_PORT=999999)
            # flog ein OverflowError durch und _runner blieb gesetzt - danach war
            # jeder weitere start() ein stiller No-Op.
            log.error("Web-Panel konnte Port %s nicht binden: %s", self._port, exc)
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._runner = None

    async def stop(self):
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._runner = None

    # --- Auth -------------------------------------------------------------
    _TOKEN_MAX = 50         # so viele Sitzungen gleichzeitig reichen dicke
    _LOGIN_MAX_FAILS = 8    # danach kurz gesperrt (Bruteforce-Bremse)
    _LOGIN_BLOCK = 300

    def _new_token(self):
        now = time.time()
        # Abgelaufene Tokens wegwerfen und die Menge deckeln: der Bot laeuft
        # monatelang, vorher wuchs das Dict mit jedem Login weiter.
        for tok, exp in list(self._tokens.items()):
            if exp < now:
                self._tokens.pop(tok, None)
        if len(self._tokens) >= self._TOKEN_MAX:
            alt = sorted(self._tokens.items(), key=lambda kv: kv[1])
            for tok, _exp in alt[:len(self._tokens) - self._TOKEN_MAX + 1]:
                self._tokens.pop(tok, None)
        tok = secrets.token_urlsafe(32)
        self._tokens[tok] = now + self._ttl
        return tok

    def _creds_ok(self, user, pw):
        """Zugangsdaten zeitkonstant vergleichen - auch mit Umlauten/Emoji.

        secrets.compare_digest wirft bei Nicht-ASCII-Strings einen TypeError;
        vorher endete so ein Login-Versuch in einem 500er (und mit einem
        WEBPANEL_PASS mit Umlaut kam man ueberhaupt nicht mehr rein)."""
        try:
            ok_u = secrets.compare_digest(str(user).encode("utf-8"),
                                          str(self._user).encode("utf-8"))
            ok_p = secrets.compare_digest(str(pw).encode("utf-8"),
                                          str(self._pass).encode("utf-8"))
            return bool(ok_u and ok_p)
        except Exception:  # noqa: BLE001
            return False

    def _login_blocked(self, ip):
        n, until = self._fails.get(ip, (0, 0.0))
        if until > time.time():
            return int(until - time.time()) + 1
        if until and until <= time.time():
            self._fails.pop(ip, None)
        return 0

    def _note_login_fail(self, ip):
        # Liste klein halten: abgelaufene Sperren und alte Zaehler wegwerfen.
        if len(self._fails) > 200:
            jetzt = time.time()
            for k, (_n, bis) in list(self._fails.items()):
                if bis <= jetzt:
                    self._fails.pop(k, None)
        n, _until = self._fails.get(ip, (0, 0.0))
        n += 1
        until = time.time() + self._LOGIN_BLOCK if n >= self._LOGIN_MAX_FAILS else 0.0
        self._fails[ip] = (n, until)
        if until:
            log.warning("Web-Panel: %d Fehl-Logins von %s - %ds gesperrt.",
                        n, ip, self._LOGIN_BLOCK)
        else:
            log.info("Web-Panel: Fehl-Login von %s (%d).", ip, n)

    def _valid(self, request):
        # Ohne Login-Pflicht ist jede Anfrage in Ordnung (WEBPANEL_AUTH=0).
        if not self._auth:
            return True
        # Token aus 'Authorization: Bearer ...' ODER Cookie.
        tok = ""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            tok = auth[7:].strip()
        if not tok:
            tok = request.cookies.get("flo_token", "")
        exp = self._tokens.get(tok)
        if not exp:
            return False
        if exp < time.time():
            self._tokens.pop(tok, None)
            return False
        return True

    #: Anfragen, die etwas VERAENDERN. Nur die brauchen den Formular-Riegel.
    _AENDERND = ("POST", "PUT", "PATCH", "DELETE")

    def _guard(self, request):
        if not self._valid(request):
            raise web.HTTPUnauthorized(text='{"ok":false,"error":"unauthorized"}',
                                       content_type="application/json")
        # Riegel gegen Formulare von fremden Seiten. Das Login-Cookie ist zwar
        # SameSite=Lax und wird bei einem fremden POST gar nicht erst
        # mitgeschickt - aber mit WEBPANEL_AUTH=0 gibt es kein Cookie, das
        # schuetzen koennte, und dann reicht ein
        #     <form action="http://192.168.x.x:9123/api/user/coins"
        #           method="post" enctype="text/plain">
        # auf irgendeiner Seite, die der Besitzer im selben Netz oeffnet.
        # Ein Browser-Formular kann KEIN application/json senden - genau daran
        # ist es zu erkennen. Die Oberflaeche selbst schickt immer JSON.
        if request.method in self._AENDERND:
            typ = (request.headers.get("Content-Type", "") or "").split(";")[0].strip()
            if typ.lower() != "application/json":
                raise web.HTTPUnsupportedMediaType(
                    text='{"ok":false,"error":"application/json erwartet"}',
                    content_type="application/json")

    # --- Seiten -----------------------------------------------------------
    async def _index(self, request):
        if self._html_cache is None:
            try:
                self._html_cache = _HTML_PATH.read_text(encoding="utf-8")
            except OSError:
                self._html_cache = ("<h1>Flo Panel</h1><p>webpanel.html fehlt.</p>")
        return web.Response(text=self._html_cache, content_type="text/html")

    async def _api_config(self, request):
        """Was die Oberflaeche VOR dem Login wissen muss. Bewusst ohne Waechter -
        hier steht nichts Geheimes drin, nur ob ueberhaupt ein Login noetig ist."""
        return web.json_response({"ok": True, "auth": bool(self._auth),
                                  "bot_name": self._bot_name})

    async def _api_login(self, request):
        data = await self._json_objekt(request)
        ip = str(getattr(request, "remote", "") or "?")
        wart = self._login_blocked(ip)
        if wart:
            return web.json_response(
                {"ok": False, "error": f"Zu viele Versuche - warte {wart}s"}, status=429)
        user = str(data.get("user", "") if data.get("user") is not None else "")
        pw = str(data.get("pass", "") if data.get("pass") is not None else "")
        if not self._creds_ok(user, pw):
            self._note_login_fail(ip)
            return web.json_response({"ok": False, "error": "Falsche Zugangsdaten"}, status=401)
        self._fails.pop(ip, None)
        log.info("Web-Panel: Login von %s ok.", ip)
        tok = self._new_token()
        resp = web.json_response({"ok": True, "token": tok, "bot_name": self._bot_name})
        resp.set_cookie("flo_token", tok, max_age=self._ttl, httponly=True, samesite="Lax")
        return resp

    # --- Daten-Helfer -----------------------------------------------------
    def _users_dict(self):
        if not economy.is_enabled():
            return {}
        try:
            return economy.instance._users()
        except Exception:  # noqa: BLE001
            return {}

    def _name_of(self, uid):
        n = economy.display_name_of(uid) if economy.is_enabled() else None
        return n or f"User {uid}"

    def _user_row(self, uid, prof, anteile=None):
        # Kaputte/halbe Profile (z. B. coins=null nach einem Absturz) duerfen die
        # Liste nicht komplett abschiessen - jedes Feld wird defensiv gelesen.
        if not isinstance(prof, dict):
            prof = {}
        xp = max(0, self._as_int(prof.get("xp"), 0))
        level = 0
        if economy.is_enabled():
            level = self._safe(lambda: economy.instance._level_only(xp), 0) or 0
        # Die Anteile kommen als fertige Tabelle herein, wenn der Aufrufer eine
        # hat. Vorher fragte JEDE Zeile einzeln bei floaktie nach - bei 10.000
        # Nutzern 10.000 Nachschlagevorgaenge fuer eine Liste, die einmal zu
        # holen gewesen waere. Und im Thread darf man ohnehin nicht mehr in
        # fremden Speichern stoebern.
        if anteile is not None:
            shares = self._as_int(anteile.get(str(uid)), 0)
        else:
            shares = 0
            try:
                import floaktie
                if floaktie.is_enabled():
                    shares = self._as_int(floaktie.instance.shares_of(uid), 0)
            except Exception:  # noqa: BLE001
                pass
        name = prof.get("name")
        name = str(name).strip() if isinstance(name, (str, int, float)) else ""
        titel = prof.get("title")
        owned = prof.get("owned") or []
        return {
            "id": str(uid),
            "name": name or f"User {uid}",
            "coins": self._as_int(prof.get("coins"), 0),
            "xp": xp,
            "level": level,
            "title": str(titel) if isinstance(titel, (str, int, float)) else "",
            "titles": len(owned) if isinstance(owned, (list, tuple)) else 0,
            "shares": shares,
            "streak": max(0, self._as_int(prof.get("streak"), 0)),
            "msgs": max(0, self._as_int(prof.get("msgs"), 0)),
            "voice_secs": max(0, self._as_int(prof.get("voice_secs"), 0)),
        }

    def _holdings_items(self):
        """[(uid_str, anteile), ...] aus floaktie - leer, wenn die Aktie aus ist."""
        try:
            import floaktie
            if not floaktie.is_enabled():
                return []
            hold = floaktie.instance._holdings() or {}
            return [(str(u), self._as_int(n, 0)) for u, n in list(hold.items())]
        except Exception:  # noqa: BLE001
            return []

    def _all_rows(self, anteile=None):
        """Alle Zeilen: economy-Profile PLUS reine Aktien-Halter.

        Wer nur Anteile besitzt (z. B. per Panel gesetzt, ohne je Coins gehabt zu
        haben), tauchte vorher in keiner Liste auf - zaehlte aber in den
        Boersenwert. Genau so verschwinden Anteile aus der Statistik."""
        if anteile is None:
            anteile = dict(self._holdings_items())
        rows = [self._user_row(uid, p, anteile)
                for uid, p in list(self._users_dict().items())]
        gesehen = {r["id"] for r in rows}
        for uid, n in anteile.items():
            if n > 0 and uid not in gesehen:
                gesehen.add(uid)
                rows.append(self._user_row(uid, {}, anteile))
        return rows

    async def _zeilen(self):
        """Dasselbe wie _all_rows, aber ohne den Bot anzuhalten.

        Das Panel laeuft IM Bot-Prozess und in DESSEN Event-Loop. _all_rows baut
        fuer jeden Nutzer eine Zeile; gemessen sind das 19 ms bei 3.000 Nutzern
        und 82 ms bei 10.000 - und die Oberflaeche fragt die Uebersicht alle
        sechs Sekunden ab. So lange stand der ganze Bot: keine Antwort, keine
        Musik, nichts.

        Kein Zwischenspeicher, sondern ein Thread: wer im Panel Coins setzt,
        soll sie sofort sehen. Ein Cache mit Verfallszeit wuerde da das Falsche
        behaupten - und ein Cache mit Verwerfen waere eine Liste von Stellen,
        die man vergessen kann.

        Der Schnappschuss der beiden Speicher wird noch IM Loop gezogen (ein
        list(), Mikrosekunden). Erst das Bauen der Zeilen wandert in den Thread:
        ueber ein dict zu laufen, das der Bot gleichzeitig veraendert, waere
        genau der Fehler, den man in einem Thread nicht machen darf.
        """
        profile = list(self._users_dict().items())
        anteile = dict(self._holdings_items())
        return await asyncio.to_thread(self._zeilen_bauen, profile, anteile)

    def _zeilen_bauen(self, profile, anteile):
        """Der reine Rechenteil - laeuft im Thread, fasst keinen Speicher an."""
        rows = [self._user_row(uid, p, anteile) for uid, p in profile]
        gesehen = {r["id"] for r in rows}
        for uid, n in anteile.items():
            if n > 0 and uid not in gesehen:
                gesehen.add(uid)
                rows.append(self._user_row(uid, {}, anteile))
        return rows

    def _guild(self, uid=None):
        """Der Server, ueber den ein Name/Avatar aufgeloest wird.

        Mit uid zuerst der Server, auf dem die Person WIRKLICH ist - sonst
        stand jemand, der nur auf dem zweiten Server unterwegs ist, in der
        Liste ohne Server-Nickname. Ohne uid: der Hauptserver."""
        guilds = list(getattr(self._client, "guilds", None) or [])
        if uid:
            for g in guilds:
                try:
                    if g.get_member(int(uid)) is not None:
                        return g
                except Exception:  # noqa: BLE001
                    continue
        try:
            gid = int(os.getenv("GUILD_ID", "0") or "0")
            if self._client is not None and gid:
                haupt = self._client.get_guild(gid)
                if haupt is not None:
                    return haupt
        except Exception:  # noqa: BLE001
            pass
        return guilds[0] if guilds else None

    async def _remember_name(self, uid):
        """Holt den echten Discord-Namen zu einer ID und merkt ihn im Profil.

        Wird nach JEDER Panel-Aenderung aufgerufen: wer hier nur per ID bearbeitet
        wird, hatte sonst keinen Namen im Profil - und tauchte in 'Flo reichste'
        als 'Unbekannt' auf."""
        try:
            await economy.resolve_display_name(uid, self._guild(uid))
        except Exception:  # noqa: BLE001 - reine Kosmetik, nie fatal
            log.debug("Namens-Merken fuer %s fehlgeschlagen", uid, exc_info=True)

    # --- Eingabe-Pruefer (jede Panel-Eingabe laeuft hier durch) ------------
    _MAX_AMOUNT = 10 ** 15      # eine Billiarde: weit ueber allem Echten
    _MAX_SHARES = 10 ** 9       # Anteile: eine Milliarde ist mehr als genug
    _MAX_PRICE = 10 ** 12
    _MAX_XP = 10 ** 12

    @staticmethod
    def _as_int(raw, default=0):
        """int aus irgendwas - nie eine Exception (kaputte Profile, Query-Murks)."""
        try:
            if isinstance(raw, bool):
                return int(raw)
            return int(raw)
        except (TypeError, ValueError, OverflowError):
            return default

    def _parse_amount(self, raw, maximum=None):
        """Nimmt Zahl oder '1k'/'2m' und gibt einen int zurueck - oder None.

        WICHTIG: bei Murks ('abc', '1 000', leer, null) kommt None heraus und der
        Aufrufer antwortet mit 400. Vorher kam hier 0 heraus - ein Tippfehler bei
        'Setzen' hat damit still das ganze Guthaben auf 0 gesetzt, und
        Geben/Nehmen tat nichts, waehrend die Oberflaeche Erfolg meldete.
        Zu grosse Werte gelten ebenfalls als Murks (sonst rechnet die Aktien-Kurve
        sich mit 10**400 in einen OverflowError)."""
        limit = int(maximum if maximum is not None else self._MAX_AMOUNT)
        wert = None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            try:
                wert = int(raw)                 # NaN/inf fliegen hier raus
            except (ValueError, OverflowError):
                return None
        else:
            s = str(raw if raw is not None else "").strip()
            if not s:
                return None
            try:
                wert = int(s)
            except ValueError:
                wert = None
            if wert is None and economy.is_enabled():
                try:
                    v = economy.parse_amount(s)
                except Exception:  # noqa: BLE001
                    v = None
                if v:
                    wert = int(v)
        if wert is None or abs(wert) > limit:
            return None
        return wert

    @staticmethod
    def _flag(raw, default=True):
        """Echtes Boolean aus JSON. '{"on":"false"}' war vorher True (truthy)."""
        if raw is None:
            return bool(default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw != 0
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on", "an", "ja"):
            return True
        if s in ("0", "false", "no", "off", "aus", "nein", ""):
            return False
        return bool(default)

    @staticmethod
    def _uid(raw):
        """Discord-ID aus der Eingabe - None, wenn unbrauchbar.

        Verhindert Geister-Profile: '-1', '0', '0123' (wurde zu 123) oder Text
        legten vorher echte Konten in der Datenbank an."""
        s = str(raw if raw is not None else "").strip()
        if not numfmt.ist_zahl(s) or (len(s) > 1 and s[0] == "0"):
            return None
        v = int(s)
        if v <= 0 or v > 2 ** 63 - 1:
            return None
        return v

    @staticmethod
    async def _json_objekt(request):
        """Body als dict - oder {}.

        request.json() wirft NUR bei kaputtem JSON. Bei GUELTIGEM, aber
        nicht-objektem JSON ([1,2,3], null, 42, "x") liefert es die Liste bzw.
        None zurueck - und das anschliessende data.get(...) warf dann einen
        AttributeError, also HTTP 500 auf JEDEM der elf POST-Endpunkte."""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - kaputtes/leeres JSON
            data = {}
        if not isinstance(data, dict):
            data = {}
        # Fuers Protokoll merken: der Rumpf laesst sich nur EINMAL lesen, die
        # Middleware kaeme sonst an nichts mehr heran.
        request["_panel_daten"] = {k: v for k, v in data.items()
                                   if k not in ("pass", "passwort", "token")}
        return data

    @staticmethod
    def _text(raw, maximum=200):
        """Nur echte Textwerte annehmen (None -> 400) und auf Laenge kuerzen.
        Vorher landete ein versehentlich gesendetes dict als Python-repr im Chat."""
        if raw is None:
            return ""
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            return None
        return str(raw).strip()[:max(1, int(maximum))]

    @staticmethod
    def _fold(name):
        """Sortier-Schluessel wie im Duden: Ä wie A, ß wie ss, Emoji nach hinten."""
        s = str(name if name is not None else "").strip().lower().replace("ß", "ss")
        s = unicodedata.normalize("NFKD", s)
        s = "".join(c for c in s if not unicodedata.combining(c))
        return (0 if s[:1].isalpha() else 1, s)

    @staticmethod
    def _safe(fn, default=None):
        """Ruft fn() auf und schluckt Fehler - eine kaputte Kennzahl darf nie
        die ganze Uebersicht (oder die Nachbar-Kennzahlen) mitnehmen."""
        try:
            return fn()
        except Exception:  # noqa: BLE001
            log.debug("Panel-Kennzahl fehlgeschlagen", exc_info=True)
            return default

    # --- API: Uebersicht --------------------------------------------------
    async def _api_overview(self, request):
        self._guard(request)
        rows = await self._zeilen()
        coins_total = sum(r["coins"] for r in rows)
        top_coins = sorted(rows, key=lambda r: r["coins"], reverse=True)[:10]
        top_shares = sorted([r for r in rows if r["shares"] > 0],
                            key=lambda r: r["shares"], reverse=True)[:10]
        # Level-Verteilung in Baendern.
        bands = {"0-4": 0, "5-9": 0, "10-19": 0, "20-49": 0, "50+": 0}
        for r in rows:
            lv = r["level"]
            key = ("0-4" if lv < 5 else "5-9" if lv < 10 else "10-19" if lv < 20
                   else "20-49" if lv < 50 else "50+")
            bands[key] += 1
        # Discord-Zustand.
        guilds = getattr(self._client, "guilds", []) or []
        members = sum(getattr(g, "member_count", 0) or 0 for g in guilds)
        stats = {
            "users": len(rows),
            "coins_total": coins_total,
            "guilds": len(guilds),
            "members": members,
            "bot_online": bool(self._client and not getattr(self._client, "is_closed", lambda: True)()),
        }
        # Aktie. WICHTIG: fuer den Chart die FEINEN Intraday-Ticks nehmen (gleiche
        # Quelle wie der Discord-Chart). Vorher kamen hier nur die Tages-
        # Schlusskurse - ein Punkt pro Kalendertag, ohne den aktuellen Kurs. Die
        # Linie passte damit nicht zur angezeigten Kurs-Zahl.
        # Jede Kennzahl EINZELN abgesichert (_safe): faellt z. B. holders_count aus,
        # fehlten vorher gleich auch Kurs-Aenderung und Boersenwert - die Kacheln
        # blieben dann beim alten Wert stehen.
        floaktie_history = []
        try:
            import floaktie
            if floaktie.is_enabled():
                preis = self._safe(floaktie.instance.price, 0) or 0
                punkte, chg = self._safe(lambda: floaktie.series(1), ([], 0.0))
                stats["floaktie_price"] = preis
                stats["floaktie_holders"] = self._safe(floaktie.instance.holders_count, 0) or 0
                stats["floaktie_change"] = chg if chg is not None else 0.0
                stats["floaktie_marketcap"] = (self._safe(floaktie.instance.total_shares, 0) or 0) * preis
                floaktie_history = list(punkte or [])
                # Woher kommt die Aktivitaet gerade? Genau das war im Betrieb
                # nicht sichtbar - "sinkt nicht" hiess in Wahrheit "irgendwer
                # (oder ein Bot) wird noch gezaehlt". Jetzt steht es im Panel.
                akt = self._safe(lambda: float(
                    floaktie.instance._state().get("act_ema", 0.0) or 0.0), 0.0) or 0.0
                stats["floaktie_activity"] = round(akt, 1)
                stats["floaktie_trend"] = round(self._safe(
                    lambda: floaktie.instance._pro_stunde(
                        floaktie.instance.drift_fuer(akt)), 0.0) or 0.0, 1)
                stats["floaktie_who"] = self._safe(
                    lambda: floaktie.instance._mess_zeile(), "") or ""
        except Exception:  # noqa: BLE001
            pass
        # Lotto.
        try:
            import lotto
            if lotto.is_enabled():
                st = self._safe(lotto.instance._state, {}) or {}
                stats["lotto_jackpot"] = self._as_int(st.get("jackpot"), 0)
                stats["lotto_house"] = self._as_int(st.get("house"), 0)
        except Exception:  # noqa: BLE001
            pass
        # Haendler.
        try:
            import merchant
            if merchant.is_enabled():
                stats["merchant_present"] = bool(self._safe(merchant.instance.is_present, False))
        except Exception:  # noqa: BLE001
            pass
        # Sendepause.
        stats["sendepause"] = self._sendepause_state()
        return web.json_response({
            "ok": True, "bot_name": self._bot_name, "stats": stats,
            "top_coins": top_coins, "top_shares": top_shares,
            "level_dist": [{"band": k, "count": v} for k, v in bands.items()],
            "floaktie_history": floaktie_history,
        })

    def _sendepause_state(self):
        """Aktueller Sendepause-Zustand (fuer Uebersicht UND Server-Seite)."""
        try:
            import admin
            if not admin.is_enabled():
                return False
            return bool(self._safe(admin.is_locked, False))
        except Exception:  # noqa: BLE001
            return False

    # --- API: Nutzerliste -------------------------------------------------
    async def _api_users(self, request):
        self._guard(request)
        q = (request.query.get("q", "") or "").strip().lower()
        sort = request.query.get("sort", "coins")
        # page und size EINZELN lesen: vorher lagen beide in einem try - ein
        # kaputtes 'size' setzte auch die Seite still auf 1 zurueck.
        page = max(1, self._as_int(request.query.get("page", "1"), 1))
        size = min(100, max(5, self._as_int(request.query.get("size", "25"), 25)))
        rows = await self._zeilen()
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in r["id"]]
        keyf = {"coins": lambda r: r["coins"], "level": lambda r: r["xp"],
                "shares": lambda r: r["shares"], "msgs": lambda r: r["msgs"],
                "name": lambda r: self._fold(r["name"])}.get(sort, lambda r: r["coins"])
        rows.sort(key=keyf, reverse=(sort != "name"))
        total = len(rows)
        pages = max(1, (total + size - 1) // size)
        # Seite auf den gueltigen Bereich ziehen: sonst liefert eine zu hohe
        # Seitenzahl eine leere Liste, und die Oberflaeche zeigte eine
        # Sackgasse ohne Blaetter-Knoepfe.
        page = min(page, pages)
        start = (page - 1) * size
        return web.json_response({
            "ok": True, "total": total, "page": page, "pages": pages,
            "users": rows[start:start + size],
        })

    async def _api_user(self, request):
        self._guard(request)
        uid = request.match_info.get("uid", "")
        users = self._users_dict()
        prof = users.get(str(uid))
        if prof is None:
            # Reiner Aktien-Halter ohne economy-Profil: trotzdem oeffenbar, sonst
            # ist die Zeile in der Liste ein Klick ins Leere.
            if any(u == str(uid) and n > 0 for u, n in self._holdings_items()):
                prof = {}
            else:
                return web.json_response({"ok": False, "error": "unbekannt"}, status=404)
        if not isinstance(prof, dict):
            prof = {}
        row = self._user_row(uid, prof)
        owned = prof.get("owned") or []
        row["owned"] = [dict(o) for o in owned if isinstance(o, dict)]
        letzte = prof.get("last_daily", "")
        row["last_daily"] = str(letzte) if isinstance(letzte, (str, int, float)) else ""
        return web.json_response({"ok": True, "user": row})

    # --- API: Coins geben/nehmen/setzen -----------------------------------
    async def _api_coins(self, request):
        self._guard(request)
        data = await self._json_objekt(request)
        if not economy.is_enabled():
            return web.json_response({"ok": False, "error": "economy aus"}, status=400)
        uid_int = self._uid(data.get("id"))
        if uid_int is None:
            return web.json_response({"ok": False, "error": "ungueltige id"}, status=400)
        action = str(data.get("action", "give"))
        if action not in ("give", "take", "set"):
            return web.json_response({"ok": False, "error": "aktion?"}, status=400)
        amount = self._parse_amount(data.get("amount"))
        if amount is None:
            return web.json_response(
                {"ok": False, "error": "Betrag nicht lesbar (z. B. 1000, 5k, 2m)"}, status=400)
        if amount < 0:
            return web.json_response({"ok": False, "error": "Betrag muss positiv sein"}, status=400)
        if action == "give":
            economy.add_coins(uid_int, amount, reason="panel")
        elif action == "take":
            # Ehrlich bleiben: economy deckelt bei 0, ein Abzug vom leeren Konto
            # ist also ein No-Op. Vorher meldete das Panel trotzdem gruen Erfolg.
            vorher = economy.get_coins(uid_int)
            economy.add_coins(uid_int, -amount, reason="panel")
            if economy.get_coins(uid_int) == vorher and amount:
                return web.json_response(
                    {"ok": False, "error": f"Nichts abgezogen – Konto steht auf "
                                           f"{vorher} und kann nicht ins Minus."},
                    status=400)
        else:                                   # "set"
            cur = economy.get_coins(uid_int)
            economy.add_coins(uid_int, amount - cur, reason="panel")
        await self._remember_name(uid_int)
        await economy.flush()
        return web.json_response({"ok": True, "coins": economy.get_coins(uid_int)})

    async def _api_xp(self, request):
        self._guard(request)
        data = await self._json_objekt(request)
        if not economy.is_enabled():
            return web.json_response({"ok": False, "error": "economy aus"}, status=400)
        uid_int = self._uid(data.get("id"))
        if uid_int is None:
            return web.json_response({"ok": False, "error": "ungueltige id"}, status=400)
        action = str(data.get("action", "give"))
        if action not in ("give", "take", "set"):
            return web.json_response({"ok": False, "error": "aktion?"}, status=400)
        # XP gedeckelt: Level-Rechnung und Anzeige sollen mit echten Werten
        # arbeiten, nicht mit 10**30.
        amount = self._parse_amount(data.get("amount"), maximum=self._MAX_XP)
        if amount is None:
            return web.json_response(
                {"ok": False, "error": "XP-Wert nicht lesbar (z. B. 500, 1k)"}, status=400)
        if amount < 0:
            return web.json_response({"ok": False, "error": "Wert muss positiv sein"}, status=400)
        prof = economy.instance._profile(uid_int)
        alt = max(0, self._as_int(prof.get("xp"), 0))
        if action == "set":
            prof["xp"] = amount
        elif action == "take":
            prof["xp"] = max(0, alt - amount)
        else:
            prof["xp"] = min(self._MAX_XP, alt + amount)
        await self._remember_name(uid_int)
        await economy.flush()
        return web.json_response({"ok": True, "xp": prof["xp"],
                                  "level": economy.instance._level_only(prof["xp"])})

    async def _api_title(self, request):
        self._guard(request)
        data = await self._json_objekt(request)
        if not economy.is_enabled():
            return web.json_response({"ok": False, "error": "economy aus"}, status=400)
        uid_int = self._uid(data.get("id"))
        if uid_int is None:
            return web.json_response({"ok": False, "error": "ungueltige id"}, status=400)
        action = str(data.get("action", "grant"))
        if action not in ("grant", "remove"):
            return web.json_response({"ok": False, "error": "aktion?"}, status=400)
        text = self._text(data.get("text"), 64)
        if text is None:
            return web.json_response({"ok": False, "error": "titel?"}, status=400)
        if not text:
            return web.json_response({"ok": False, "error": "titel?"}, status=400)
        entfernt = None
        if action == "remove":
            # Rueckmelden, ob der Titel ueberhaupt vorhanden war - vorher kam
            # immer ein froehliches "entfernt", auch bei Tippfehlern.
            hatte = bool(self._safe(lambda: economy.owns_title(uid_int, text), False))
            economy.remove_title(uid_int, text)
            entfernt = hatte
        else:
            label = self._text(data.get("label"), 48)
            rarity = self._text(data.get("rarity"), 24)
            if label is None or rarity is None:
                return web.json_response({"ok": False, "error": "titel?"}, status=400)
            rarity = (rarity or "selten").lower()
            erlaubt = self._safe(lambda: __import__("titles").RARITY_ORDER, None)
            if erlaubt and rarity not in erlaubt:
                return web.json_response({"ok": False, "error": "seltenheit?"}, status=400)
            economy.grant_title(uid_int, text, label or text, rarity)
        # Rolle nachziehen, falls das Mitglied auffindbar ist (best effort).
        try:
            # Der Server, auf dem die Person wirklich ist - die Farb-Rolle
            # gibt es nur dort.
            guild = self._guild(uid_int)
            member = guild.get_member(uid_int) if guild else None
            if member is not None:
                await economy.sync_role(member)
        except Exception:  # noqa: BLE001
            pass
        await self._remember_name(uid_int)
        await economy.flush()
        antwort = {"ok": True, "titles": economy.list_titles(uid_int)}
        if entfernt is not None:
            antwort["removed"] = entfernt
        return web.json_response(antwort)

    # --- API: Aktien-Anteile korrigieren ----------------------------------
    async def _api_shares(self, request):
        """Anteile eines Nutzers geben/nehmen/setzen (z. B. Exploit-Anteile weg)."""
        self._guard(request)
        data = await self._json_objekt(request)
        try:
            import floaktie
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "aktie aus"}, status=400)
        if not floaktie.is_enabled():
            return web.json_response({"ok": False, "error": "aktie aus"}, status=400)
        uid_int = self._uid(data.get("id"))
        if uid_int is None:
            return web.json_response({"ok": False, "error": "ungueltige id"}, status=400)
        action = str(data.get("action", "set"))
        if action not in ("give", "take", "set"):
            return web.json_response({"ok": False, "error": "aktion?"}, status=400)
        # Anteile GEDECKELT einlesen: mit einer 400-stelligen Zahl rechnete die
        # Kurs-Kurve sich vorher in einen OverflowError - und zwar NACHDEM das
        # Depot schon geaendert war. Danach war die Aktie komplett blockiert.
        amount = self._parse_amount(data.get("amount"), maximum=self._MAX_SHARES)
        if amount is None:
            return web.json_response(
                {"ok": False,
                 "error": f"Anzahl nicht lesbar oder zu groß (max {self._MAX_SHARES:,})".replace(",", ".")},
                status=400)
        if amount < 0:
            return web.json_response({"ok": False, "error": "Anzahl muss positiv sein"}, status=400)
        # keep_price (Standard an): haelt den Kurs stabil, damit das Streichen
        # grosser Positionen den Markt nicht abstuerzen laesst.
        keep = self._flag(data.get("keep_price"), True)
        try:
            shares, kurs, total = await floaktie.admin_shares(
                uid_int, action, amount, keep_price=keep)
        except Exception:  # noqa: BLE001
            log.exception("Anteils-Korrektur via Panel fehlgeschlagen")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)
        await self._remember_name(uid_int)
        if economy.is_enabled():
            await economy.flush()
        return web.json_response({"ok": True, "shares": shares, "price": kurs,
                                  "total_shares": total})

    async def _api_stock_series(self, request):
        """Kursverlauf fuer den Chart: ?days=1|7|30|3650 (Gesamt).

        Nutzt exakt dieselbe Quelle wie der Discord-Chart (floaktie.series), damit
        Panel und Bot immer dasselbe zeigen - inklusive des AKTUELLEN Kurses als
        letztem Punkt."""
        self._guard(request)
        try:
            import floaktie
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "aktie aus"}, status=400)
        if not floaktie.is_enabled():
            return web.json_response({"ok": False, "error": "aktie aus"}, status=400)
        try:
            days = float(request.query.get("days", "1") or "1")
        except (TypeError, ValueError):
            days = 1.0
        if days != days or days in (float("inf"), float("-inf")):   # NaN/inf
            days = 1.0
        days = max(0.04, min(days, 3650.0))     # min ~1 Stunde, max 10 Jahre
        try:
            punkte, chg = floaktie.series(days)
        except Exception:  # noqa: BLE001
            log.exception("Kursverlauf konnte nicht gebaut werden")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)
        return web.json_response({"ok": True, "days": days, "points": punkte,
                                  "change": chg, "price": floaktie.instance.price(),
                                  "count": len(punkte)})

    async def _api_stock_price(self, request):
        """Setzt den Aktienkurs direkt (Korrektur nach einem Exploit)."""
        self._guard(request)
        data = await self._json_objekt(request)
        try:
            import floaktie
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "aktie aus"}, status=400)
        if not floaktie.is_enabled():
            return web.json_response({"ok": False, "error": "aktie aus"}, status=400)
        preis = self._parse_amount(data.get("price"), maximum=self._MAX_PRICE)
        if preis is None:
            return web.json_response(
                {"ok": False, "error": "Kurs nicht lesbar oder zu groß"}, status=400)
        if preis <= 0:
            return web.json_response({"ok": False, "error": "kurs?"}, status=400)
        try:
            neu = await floaktie.admin_set_price(preis)
        except Exception:  # noqa: BLE001
            log.exception("Kurs-Korrektur via Panel fehlgeschlagen")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)
        # 'requested' mitschicken: liegt der Wunsch unter dem Mindestkurs, steht
        # in 'price' der WIRKLICH gesetzte Kurs - das Panel meldet dann keinen
        # Erfolg, den es nicht gegeben hat.
        return web.json_response({"ok": True, "price": neu, "requested": preis})

    # --- API: Server ------------------------------------------------------
    async def _api_servers(self, request):
        self._guard(request)
        out = []
        for g in (getattr(self._client, "guilds", []) or []):
            icon = None
            try:
                icon = g.icon.url if g.icon else None
            except Exception:  # noqa: BLE001
                icon = None
            out.append({
                "id": str(g.id),
                "name": g.name,
                "members": getattr(g, "member_count", 0) or 0,
                "channels": len(getattr(g, "channels", []) or []),
                "icon": icon,
                "owner_id": str(getattr(g, "owner_id", "") or ""),
            })
        # sendepause MITSCHICKEN: die Server-Seite zeigte sonst den zuletzt aus
        # der Uebersicht gemerkten Zustand - war der veraltet, war der erste
        # Klick auf den Schalter nur ein Neu-Abgleich und tat gar nichts.
        # Gleiches gilt fuer den Aktien-Schalter unter 'Steuerung'.
        return web.json_response({"ok": True, "guilds": out,
                                  "sendepause": self._sendepause_state(),
                                  "aktie": self._feature_state("floaktie")})

    async def _api_update(self, request):
        """'git pull' im Bot-Verzeichnis und danach Neustart - der Knopf unter
        'Steuerung'. Spart den Weg ins Terminal.

        Bewusst eng gehalten: es laeuft NUR 'git pull --ff-only' (kein Merge, kein
        Rebase, keine beliebigen Befehle), im Verzeichnis des Bots, mit Zeitlimit.
        Neu gestartet wird nur, wenn der Pull sauber durchlief UND sich wirklich
        etwas geaendert hat - sonst waere der Knopf ein Neustart-Knopf mit
        Zusatzschritt."""
        self._guard(request)
        # Nur EIN git-pull gleichzeitig. Zwei offene Browser-Tabs (oder ein
        # zweiter Klick aus einem anderen Geraet) haetten sonst zwei Pulls im
        # selben Arbeitsverzeichnis gestartet - git legt dann index.lock an und
        # der zweite Lauf bricht mit einem Fehler ab, den niemand einordnen kann.
        if self._update_laeuft:
            return web.json_response(
                {"ok": False, "error": "Update läuft bereits",
                 "log": "Es läuft schon ein Update. Bitte warten."}, status=409)
        self._update_laeuft = True
        try:
            return await self._update_lauf(request)
        finally:
            self._update_laeuft = False

    async def _update_lauf(self, request):
        """Der eigentliche Update-Ablauf (siehe _api_update)."""
        data = await self._json_objekt(request)
        neustart = self._flag(data.get("restart"), True)
        verzeichnis = str(Path(__file__).resolve().parent)

        async def lauf(*args, timeout=120):
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=verzeichnis,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                aus, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                return 124, "Zeitüberschreitung"
            return proc.returncode, (aus or b"").decode("utf-8", "replace").strip()

        try:
            rc_vor, vorher = await lauf("git", "rev-parse", "HEAD")
            rc, ausgabe = await lauf("git", "pull", "--ff-only")
            rc_nach, nachher = await lauf("git", "rev-parse", "HEAD")
        except FileNotFoundError:
            return web.json_response({"ok": False, "error": "git nicht gefunden"},
                                     status=500)
        except Exception:  # noqa: BLE001
            log.exception("Update ueber das Panel fehlgeschlagen")
            return web.json_response({"ok": False, "error": "Update fehlgeschlagen"},
                                     status=500)

        geaendert = (rc_vor == 0 and rc_nach == 0 and vorher != nachher)
        if rc != 0:
            log.warning("Panel-Update: git pull fehlgeschlagen (%s)", ausgabe[:400])
            return web.json_response({"ok": False, "error": "git pull fehlgeschlagen",
                                      "log": ausgabe[-1500:]}, status=400)
        log.info("Panel-Update: git pull ok (%s)", "neue Commits" if geaendert
                 else "schon aktuell")
        antwort = {"ok": True, "changed": geaendert, "log": ausgabe[-1500:],
                   "commit": nachher[:8] if rc_nach == 0 else ""}
        if neustart and geaendert:
            antwort["restarting"] = True
            # Erst die Antwort rausgeben, DANN neu starten - sonst sieht das Panel
            # nur einen Verbindungsabbruch und weiss nicht, ob es geklappt hat.
            client = self._client
            if client is not None and hasattr(client, "restart_soon"):
                try:
                    client._spawn(client.restart_soon(2.0))
                except Exception:  # noqa: BLE001
                    log.exception("Neustart nach Panel-Update fehlgeschlagen")
                    antwort["restarting"] = False
                    antwort["hinweis"] = "Neustart fehlgeschlagen – bitte von Hand."
            else:
                antwort["restarting"] = False
                antwort["hinweis"] = "Neustart nicht möglich – bitte von Hand."
        return web.json_response(antwort)

    def _feature_state(self, key):
        """{loaded, on} eines Features - fuer die dicken Schalter unter 'Steuerung'."""
        geladen = bool(self._loaded_flags().get(key, False))
        an = False
        if geladen:
            try:
                import features
                an = bool(features.is_on(key))
            except Exception:  # noqa: BLE001
                an = False
        return {"loaded": geladen, "on": an}

    async def _api_sendepause(self, request):
        self._guard(request)
        data = await self._json_objekt(request)
        on = self._flag(data.get("on"), True)
        try:
            import admin
            if not admin.is_enabled():
                return web.json_response({"ok": False, "error": "admin aus"}, status=400)
            state = await admin.set_lock(on)
        except Exception:  # noqa: BLE001
            log.exception("Sendepause via Panel fehlgeschlagen")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)
        return web.json_response({"ok": True, "sendepause": state})

    async def _api_announce(self, request):
        self._guard(request)
        data = await self._json_objekt(request)
        # Discord-Limit ist 2000 Zeichen - laenger schickt man gar nicht erst los.
        # Und nur echte Texte: ein versehentlich gesendetes Objekt landete vorher
        # als Python-repr ("{'a': 1}") im Chat.
        text = self._text(data.get("text"), 1900)
        if text is None:
            return web.json_response({"ok": False, "error": "text?"}, status=400)
        if not text:
            return web.json_response({"ok": False, "error": "kein text"}, status=400)
        cid = data.get("channel_id")
        channel = None
        if cid not in (None, ""):
            # Kanal-ID angegeben, aber unbekannt? Dann NICHT still in den
            # System-Kanal posten - sonst landet die Ansage woanders als gewollt.
            kid = self._uid(cid)
            if kid is None:
                return web.json_response({"ok": False, "error": "Kanal-ID ungueltig"}, status=400)
            channel = self._safe(lambda: self._client.get_channel(kid), None)
            if channel is None:
                return web.json_response({"ok": False, "error": "Kanal nicht gefunden"}, status=400)
        else:
            # Ohne Kanal-ID: der Ansagen-Kanal des gewaehlten Servers (sonst des
            # Hauptservers), notfalls dessen System-Kanal. BEWUSST nicht an alle
            # Server gleichzeitig - eine Ansage aus Versehen an fremde Server zu
            # schicken laesst sich nicht zurueckholen.
            gid = self._as_int(data.get("guild"), 0) \
                or self._as_int(os.getenv("GUILD_ID", "0") or "0", 0)
            guild = self._guild_by_id(gid)
            if guild is not None:
                try:
                    import guildcfg
                    kid = guildcfg.get(guild.id, "ansage_channel")
                except Exception:  # noqa: BLE001
                    kid = 0
                channel = self._safe(lambda: guild.get_channel(kid), None) if kid else None
                if channel is None:
                    channel = getattr(guild, "system_channel", None)
        if channel is None:
            return web.json_response({"ok": False, "error": "kein channel"}, status=400)
        if not hasattr(channel, "send"):
            return web.json_response({"ok": False, "error": "Kanal kann keine Nachrichten"},
                                     status=400)
        try:
            # KEINE Massen-Pings aus dem Panel: ein versehentliches '@everyone'
            # im Ansage-Feld haette sonst den ganzen Server angepingt - das laesst
            # sich nicht zurueckholen.
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:  # noqa: BLE001
            log.exception("Ansage via Panel fehlgeschlagen")
            return web.json_response({"ok": False, "error": "senden fehlgeschlagen"}, status=500)
        return web.json_response({"ok": True})

    # --- API: Wort des Tages von Hand ausloesen ---------------------------
    async def _api_wordle_start(self, request):
        """Legt das Wort des Tages sofort aus, ohne auf Voice und Termin zu warten.

        Normalerweise wartet Flo, bis wirklich Leute da sind - ein Raetsel in
        einen leeren Server zu werfen waere verschenkt. Sitzt der Server aber
        voll und es kommt trotzdem nichts, muss der Betreiber es von Hand
        ausloesen koennen, ohne sich auf den Server einzuloggen."""
        self._guard(request)
        data = await self._json_objekt(request)
        gid = self._as_int(data.get("guild"), 0) \
            or self._as_int(os.getenv("GUILD_ID", "0") or "0", 0)
        if not gid:
            return web.json_response({"ok": False, "error": "kein Server"}, status=400)
        cid = 0
        if data.get("channel_id") not in (None, ""):
            cid = self._uid(data.get("channel_id")) or 0
            if not cid:
                return web.json_response({"ok": False, "error": "Kanal-ID ungueltig"},
                                         status=400)
        starten = getattr(self._client, "wordle_jetzt", None)
        if starten is None:
            return web.json_response({"ok": False, "error": "Bot nicht bereit"},
                                     status=503)
        try:
            satz = await starten(gid, cid)
        except ValueError as exc:
            # Ein bekannter Grund ("schon geloest", "Kanal fehlt") ist KEIN
            # Serverfehler - der Betreiber soll den Satz lesen, nicht eine 500.
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            log.exception("Wort des Tages via Panel fehlgeschlagen")
            return web.json_response({"ok": False, "error": "senden fehlgeschlagen"},
                                     status=500)
        return web.json_response({"ok": True, "text": satz})

    # --- API: Profilbilder ------------------------------------------------
    _AV_TTL = 3600      # eine Stunde: Discord-CDN-URLs sind stabil genug
    _AV_MAX = 500       # so viele Bilder merken wir uns hoechstens

    async def _api_avatar(self, request):
        """Leitet auf das Discord-Profilbild der ID weiter (302).

        Loest den Nutzer notfalls ueber die API auf - so bekommt die Nutzer-Liste
        auch Bilder von Leuten, die nicht im Cache stehen. Nicht auffindbar -> 404,
        das Panel zeigt dann das Initial-Kaestchen.
        Hinweis: <img>-Anfragen schicken keinen Authorization-Header, aber das
        Login-Cookie - und genau das prueft _guard/_valid mit."""
        self._guard(request)
        try:
            uid = int(request.match_info.get("uid", "0"))
        except (TypeError, ValueError):
            raise web.HTTPNotFound()
        if not uid:
            raise web.HTTPNotFound()
        now = time.time()
        hit = self._av_cache.get(uid)
        if hit and hit[1] > now:
            url = hit[0]
        else:
            url = None
            try:
                user = await economy.instance._resolve_avatar_user(self._guild(uid), uid)
                if user is not None:
                    asset = user.display_avatar
                    try:
                        asset = asset.with_size(64)
                    except Exception:  # noqa: BLE001 - Groesse ist nur Optik
                        pass
                    url = str(asset.url)
            except Exception:  # noqa: BLE001 - Bild ist nie kritisch
                log.debug("Avatar fuer %s nicht ermittelbar", uid, exc_info=True)
            # Cache deckeln: der Bot laeuft monatelang, vorher wuchs das Dict
            # mit jeder je angesehenen ID weiter.
            if len(self._av_cache) > self._AV_MAX:
                for alt_uid, (_u, exp) in list(self._av_cache.items()):
                    if exp <= now:
                        self._av_cache.pop(alt_uid, None)
                while len(self._av_cache) > self._AV_MAX:
                    self._av_cache.pop(next(iter(self._av_cache)), None)
            self._av_cache[uid] = (url, now + self._AV_TTL)
        if not url:
            raise web.HTTPNotFound()
        raise web.HTTPFound(url)

    # --- API: Funktionen (Laufzeit-Schalter) -----------------------------
    def _loaded_flags(self):
        """Start-Flags {key: geladen?} aus bot.py (welche Module aktiv sind)."""
        try:
            import bot
            return dict(getattr(bot, "FEATURE_LOADED", {}) or {})
        except Exception:  # noqa: BLE001
            return {}

    async def _api_features(self, request):
        self._guard(request)
        try:
            import features
            return web.json_response({"ok": True,
                                      "features": features.state(self._loaded_flags())})
        except Exception:  # noqa: BLE001
            log.exception("Feature-Liste fehlgeschlagen")
            return web.json_response({"ok": True, "features": []})

    async def _api_feature(self, request):
        self._guard(request)
        data = await self._json_objekt(request)
        key = str(data.get("key", "")).strip()
        on = self._flag(data.get("on"), True)
        try:
            import features
            # Nicht geladene Module kann man nicht per Schalter aktivieren.
            if on and not self._loaded_flags().get(key, False):
                return web.json_response({"ok": False,
                                          "error": "Modul ist nicht geladen (Neustart nötig)"}, status=400)
            # Mit 'guild' schaltet der Knopf NUR diesen Server, ohne ihn global.
            gid = self._as_int(data.get("guild"), 0)
            if gid:
                res = await features.set_guild(gid, key, on)
            else:
                res = await features.set_feature(key, on)
            if res is None:
                return web.json_response({"ok": False, "error": "unbekanntes Feature"}, status=400)
        except Exception:  # noqa: BLE001
            log.exception("Feature-Schalter fehlgeschlagen")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)
        return web.json_response({"ok": True, "key": key, "on": res})

    # =====================================================================
    # BotSicht - Discord aus Flos Blickwinkel
    # =====================================================================
    # Der Reiz dieser Ansicht ist nicht "Discord im Browser" (dafuer gibt es
    # Discord), sondern: was sieht der BOT eigentlich? Und da ist Flos Bild
    # LOECHRIG, und zwar mit Absicht:
    #
    #   * Kanaele, in denen ihm 'Nachrichten lesen' fehlt, existieren fuer ihn
    #     praktisch nicht - hier stehen sie trotzdem, aber gesperrt.
    #   * Das Members-Intent ist aus (bot.py:290 ff.), Flo kennt also nur die
    #     Leute, die er im Chat oder im Voice gesehen hat. Die Mitgliederliste
    #     ist deshalb kurz - das ist kein Fehler, das ist der Blickwinkel.
    #   * Ohne message_content-Intent waeren Texte fremder Nachrichten leer.
    #
    # Genau das wird hier ehrlich mitgeliefert statt kaschiert. Wer einen
    # Fehler sucht ("warum antwortet er da nicht?"), sieht die Antwort sofort,
    # statt sie im Journal zu suchen.

    _SICHT_TEXT_MAX = 4000        # ein Discord-Text ist nie laenger
    _SICHT_EMBED_MAX = 6          # mehr Embeds haengt Discord selbst nicht an

    @staticmethod
    def _sicht_farbe(wert):
        """Discord-Farbe als '#rrggbb' - oder None bei 'keine Farbe' (0)."""
        try:
            zahl = int(getattr(wert, "value", wert) or 0)
        except (TypeError, ValueError):
            return None
        return f"#{zahl:06x}" if zahl else None

    def _sicht_autor(self, autor):
        """Ein Nachrichten-Autor so, wie Flo ihn kennt."""
        uid = self._safe(lambda: int(autor.id), 0)
        eigen = bool(self._client is not None
                     and getattr(self._client, "user", None) is not None
                     and uid == self._safe(lambda: int(self._client.user.id), -1))
        return {
            "id": str(uid),
            "name": self._safe(lambda: str(autor.name), "?") or "?",
            "display": self._safe(lambda: str(autor.display_name), None)
                       or self._safe(lambda: str(autor.name), "?") or "?",
            "avatar": self._safe(lambda: autor.display_avatar.url, None),
            "bot": bool(self._safe(lambda: autor.bot, False)),
            # color gibt es nur auf Member (nicht auf User) - im DM ist das None.
            "farbe": self._safe(lambda: self._sicht_farbe(autor.color), None),
            "eigen": eigen,
        }

    def _sicht_embed(self, embed):
        """Embed auf das reduzieren, was die Oberflaeche zeichnet.

        to_dict() liefert alles, aber auch Felder, die niemand rendert - und bei
        einem Embed mit 25 langen Feldern waere die Antwort groesser als der
        ganze restliche Verlauf."""
        d = self._safe(lambda: embed.to_dict(), None) or {}
        felder = []
        for f in (d.get("fields") or [])[:25]:
            felder.append({"name": str(f.get("name", ""))[:256],
                           "wert": str(f.get("value", ""))[:1024],
                           "inline": bool(f.get("inline"))})
        bild = (d.get("image") or {}).get("url")
        thumb = (d.get("thumbnail") or {}).get("url")
        autor = d.get("author") or {}
        fuss = d.get("footer") or {}
        return {
            "titel": str(d.get("title") or "")[:256],
            "text": str(d.get("description") or "")[:2048],
            "url": d.get("url") or None,
            "farbe": self._sicht_farbe(d.get("color")),
            "autor": str(autor.get("name") or "")[:256],
            "autor_bild": autor.get("icon_url") or None,
            "fuss": str(fuss.get("text") or "")[:2048],
            "fuss_bild": fuss.get("icon_url") or None,
            "bild": bild, "thumb": thumb,
            "felder": felder,
        }

    def _sicht_msg(self, message):
        """Eine Discord-Nachricht als JSON fuer die Oberflaeche.

        Alles einzeln in _safe: eine Nachricht mit einem kaputten Anhang darf
        nicht den ganzen Verlauf zum 500er machen."""
        kanal = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        anhaenge = []
        for a in (self._safe(lambda: list(message.attachments), []) or [])[:10]:
            anhaenge.append({
                "name": self._safe(lambda: str(a.filename), "datei") or "datei",
                "url": self._safe(lambda: str(a.url), "") or "",
                "typ": self._safe(lambda: a.content_type, None),
                "groesse": self._safe(lambda: int(a.size), 0) or 0,
                "breite": self._safe(lambda: a.width, None),
                "hoehe": self._safe(lambda: a.height, None),
            })
        embeds = [self._sicht_embed(e)
                  for e in (self._safe(lambda: list(message.embeds), []) or []
                            )[:self._SICHT_EMBED_MAX]]
        reaktionen = []
        for r in (self._safe(lambda: list(message.reactions), []) or [])[:20]:
            emoji = self._safe(lambda: r.emoji, None)
            reaktionen.append({
                "emoji": self._safe(lambda: str(emoji), "?") or "?",
                # Eigene Server-Emojis haben ein Bild, Unicode-Emojis nicht.
                "bild": self._safe(lambda: emoji.url, None),
                "anzahl": self._safe(lambda: int(r.count), 0) or 0,
                "eigen": bool(self._safe(lambda: r.me, False)),
            })
        antwort = None
        bezug = self._safe(lambda: message.reference, None)
        if bezug is not None:
            geloest = self._safe(lambda: bezug.resolved, None)
            antwort = {
                "id": str(self._safe(lambda: bezug.message_id, "") or ""),
                # Steckt die Ursprungsnachricht nicht mehr im Cache, kennt Flo
                # sie nicht mehr - dann bleibt es bei der nackten ID.
                "autor": self._safe(lambda: str(geloest.author.display_name), None),
                "text": self._safe(lambda: str(geloest.content)[:200], None),
            }
        # Ein DM-Kanal hat keinen Namen - dort ist der Gegenueber der Name.
        # Schreibt Flo selbst, ist das 'recipient'; schreibt der andere, ist es
        # der Autor. Ohne diese Unterscheidung heisst jede DM nur "DM".
        dm_name = None
        if guild is None:
            dm_name = (self._safe(lambda: str(kanal.recipient.display_name), None)
                       or self._safe(lambda: str(message.author.display_name), None))
        return {
            "id": str(self._safe(lambda: message.id, "") or ""),
            "kanal": str(self._safe(lambda: kanal.id, "") or ""),
            "kanal_name": (self._safe(lambda: str(kanal.name), None)
                           or (f"DM · {dm_name}" if dm_name else "Direktnachricht")),
            "dm_mit": str(self._safe(lambda: kanal.recipient.id, "") or "") if guild is None else "",
            "guild": str(self._safe(lambda: guild.id, "") or ""),
            "guild_name": self._safe(lambda: str(guild.name), None) or "Direktnachricht",
            "t": int(self._safe(lambda: message.created_at.timestamp(), 0) or 0),
            "bearbeitet": int(self._safe(
                lambda: message.edited_at.timestamp(), 0) or 0) or None,
            "autor": self._sicht_autor(message.author),
            "text": (self._safe(lambda: str(message.content), "") or "")[:self._SICHT_TEXT_MAX],
            "anhaenge": anhaenge,
            "embeds": embeds,
            "reaktionen": reaktionen,
            "antwort_auf": antwort,
            "angeheftet": bool(self._safe(lambda: message.pinned, False)),
            "system": bool(self._safe(
                lambda: message.type is not discord.MessageType.default
                and message.type is not discord.MessageType.reply, False)),
        }

    # --- Live-Strom -------------------------------------------------------
    def sicht_notiere(self, message, art="neu"):
        """Von bot.py fuer JEDE gesehene Nachricht aufgerufen (auch fuer Flos
        eigene und die anderer Bots) - genau darum geht es ja.

        Muss billig und absolut lautlos sein: das haengt im heissen Pfad von
        on_message. Faellt hier etwas um, darf davon nichts nach oben
        durchschlagen, sonst kostet eine Panel-Spielerei die Nachricht."""
        if not self._enabled:
            return
        try:
            ereignis = {"art": art, "nr": self._sicht_nr + 1,
                        "msg": self._sicht_msg(message)}
            self._sicht_nr += 1
            self._sicht.append(ereignis)
            self._sicht_push(ereignis)
            # Ist das eine DM, den Gespraechspartner ins Verzeichnis eintragen -
            # Discord fuehrt keins, siehe Kommentar am DM-Abschnitt.
            if getattr(message, "guild", None) is None:
                self._dm_aus_nachricht(message)
        except Exception:  # noqa: BLE001
            log.debug("BotSicht: Nachricht liess sich nicht aufbereiten", exc_info=True)

    # So viele Ereignisse darf ein langsamer Browser hinterherhinken.
    _SICHT_STAU = 200

    def _sicht_push(self, ereignis):
        """Legt ein Ereignis in die Warteschlange JEDER offenen Verbindung.

        Bewusst eine Schlange je Verbindung und ein Sende-Task dahinter, statt
        pro Nachricht ein create_task(ws.send_json(...)): zwei kurz
        hintereinander erzeugte Tasks koennen sich beim Schreiben ueberholen,
        und dann steht die Antwort im Panel VOR der Frage. Nebenbei kann so
        kein Browser den Bot mit unbegrenzt vielen Tasks zumuellen.

        Diese Methode laeuft im heissen Pfad von on_message - sie wartet auf
        nichts und wirft nichts."""
        for ws, schlange in list(self._sicht_ws.items()):
            if getattr(ws, "closed", False):
                self._sicht_ws.pop(ws, None)
                continue
            try:
                schlange.put_nowait(ereignis)
            except asyncio.QueueFull:
                # Der Browser kommt nicht hinterher. Dann faellt das AELTESTE
                # raus - im Live-Strom ist das Neueste das Interessante.
                try:
                    schlange.get_nowait()
                    schlange.put_nowait(ereignis)
                except Exception:  # noqa: BLE001
                    pass

    async def _sicht_sender(self, ws, schlange):
        """Schreibt die Schlange einer Verbindung ab - in Reihenfolge."""
        try:
            while True:
                ereignis = await schlange.get()
                await ws.send_json(ereignis)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - Browser weg, Leitung tot, egal
            self._sicht_ws.pop(ws, None)

    async def _api_sicht_ws(self, request):
        """Live-Leitung: der Browser bekommt jede Nachricht, die Flo sieht.

        Der Browser schickt hier nichts - Senden laeuft ueber /api/sicht/send.
        Wir lesen die Leitung trotzdem, sonst merkt aiohttp nie, dass der Tab
        zu ist, und die Verbindung bleibt bis zum Neustart stehen."""
        self._guard(request)
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        schlange = asyncio.Queue(maxsize=self._SICHT_STAU)
        # Der Gruss geht durch dieselbe Schlange wie alles andere - zwei
        # Schreiber auf einem WebSocket vertragen sich nicht.
        schlange.put_nowait({"art": "hallo", "nr": self._sicht_nr})
        self._sicht_ws[ws] = schlange
        sender = asyncio.get_running_loop().create_task(self._sicht_sender(ws, schlange))
        self._sicht_tasks.add(sender)
        sender.add_done_callback(self._sicht_tasks.discard)
        try:
            async for _nachricht in ws:
                pass
        except Exception:  # noqa: BLE001
            log.debug("BotSicht-Leitung beendet", exc_info=True)
        finally:
            self._sicht_ws.pop(ws, None)
            sender.cancel()
        return ws

    async def _api_sicht_feed(self, request):
        """Der Live-Strom als normaler Abruf - Rueckfallweg, wenn die
        WebSocket-Leitung nicht zustande kommt (Reverse-Proxy o. ae.).
        'seit' ist die zuletzt gesehene laufende Nummer."""
        self._guard(request)
        seit = self._as_int(request.query.get("seit"), 0)
        neu = [e for e in list(self._sicht) if e.get("nr", 0) > seit]
        return web.json_response({"ok": True, "nr": self._sicht_nr,
                                  "ereignisse": neu[-BOTSICHT_VERLAUF_MAX:]})

    # =====================================================================
    # Direktnachrichten
    # =====================================================================
    # Harte Tatsache vorweg: Discord gibt einem Bot KEINE Moeglichkeit, seine
    # DM-Kanaele aufzulisten. client.private_channels bleibt bei Bots leer, im
    # READY stehen keine privaten Kanaele. Es gibt keinen Endpunkt "zeig mir
    # alle DMs dieses Bots".
    #
    # Was es sehr wohl gibt: kennt man die Nutzer-ID, liefert create_dm() +
    # history() den KOMPLETTEN Verlauf, auch von vor Jahren - Discord hebt ihn
    # auf, nur das Verzeichnis fehlt.
    #
    # Also fuehrt Flo das Verzeichnis selbst (data/botsicht_dms.json, gefuettert
    # aus sicht_notiere und den Sende-Stellen). Fuer alles, was VOR dieser
    # Aenderung passiert ist, gibt es drei Wege zurueck - siehe _dm_suche_lauf.

    def _dm_merken(self, user, ts=None, quelle="chat"):
        """Haelt fest, dass Flo mit dieser Person privat geschrieben hat."""
        uid = self._safe(lambda: int(user.id), 0) if not isinstance(user, int) else int(user)
        if not uid:
            return False
        ich = self._safe(lambda: int(self._client.user.id), 0)
        if uid == ich:
            return False                    # mit sich selbst schreibt niemand
        schluessel = str(uid)
        eintrag = self._dm_partner.get(schluessel) or {
            "zuerst": int(ts or time.time()), "anzahl": 0, "quelle": quelle}
        name = self._safe(lambda: str(user.display_name), None) \
            or self._safe(lambda: str(user.name), None)
        if name:
            eintrag["name"] = name
        avatar = self._safe(lambda: user.display_avatar.url, None)
        if avatar:
            eintrag["avatar"] = avatar
        eintrag["zuletzt"] = int(ts or time.time())
        eintrag["anzahl"] = int(eintrag.get("anzahl", 0)) + 1
        neu = schluessel not in self._dm_partner
        self._dm_partner[schluessel] = eintrag
        if len(self._dm_partner) > BOTSICHT_DM_MAX:
            # Aelteste Bekanntschaft faellt raus. Der Verlauf bei Discord
            # bleibt - man kann sie ueber die ID jederzeit zurueckholen.
            aeltest = sorted(self._dm_partner.items(),
                             key=lambda kv: kv[1].get("zuletzt", 0))
            for k, _v in aeltest[:len(self._dm_partner) - BOTSICHT_DM_MAX]:
                self._dm_partner.pop(k, None)
        if self._dm_store is not None:
            self._dm_store.data["partner"] = dict(self._dm_partner)
            self._spawn_save(self._dm_store)
        return neu

    def _dm_aus_nachricht(self, message):
        """Aus einer DM den Gespraechspartner heraussuchen und merken.

        Schreibt FLO die Nachricht, ist der Partner der Empfaenger des Kanals;
        schreibt jemand anders, ist er es selbst."""
        kanal = getattr(message, "channel", None)
        autor = getattr(message, "author", None)
        ich = self._safe(lambda: int(self._client.user.id), 0)
        wer = autor
        if self._safe(lambda: int(autor.id), 0) == ich:
            wer = self._safe(lambda: kanal.recipient, None)
        if wer is None:
            return
        ts = self._safe(lambda: message.created_at.timestamp(), None)
        self._dm_merken(wer, ts)

    async def _dm_kanal(self, uid):
        """DM-Kanal zu einer Nutzer-ID - notfalls neu geoeffnet.

        create_dm() legt den Kanal an bzw. holt ihn; der Verlauf ist danach
        vollstaendig da, auch wenn Flo die Person nie im Cache hatte."""
        if self._client is None:
            return None, None
        user = self._safe(lambda: self._client.get_user(uid), None)
        if user is None:
            user = await self._client.fetch_user(uid)
        kanal = getattr(user, "dm_channel", None)
        if kanal is None:
            kanal = await user.create_dm()
        return kanal, user

    async def _api_sicht_dms(self, request):
        """Alle DM-Bekanntschaften, die Flo kennt - neueste zuerst."""
        self._guard(request)
        out = []
        for uid, e in self._dm_partner.items():
            out.append({
                "id": uid,
                "name": e.get("name") or f"Unbekannt ({uid})",
                "avatar": e.get("avatar"),
                "zuletzt": int(e.get("zuletzt", 0) or 0),
                "zuerst": int(e.get("zuerst", 0) or 0),
                "anzahl": int(e.get("anzahl", 0) or 0),
                "quelle": e.get("quelle", "chat"),
            })
        out.sort(key=lambda e: -e["zuletzt"])
        lauf = getattr(self, "_dm_suche", None)
        return web.json_response({
            "ok": True, "partner": out,
            "suche": lauf if lauf else None,
            # Damit die Oberflaeche ehrlich erklaeren kann, warum die Liste
            # eventuell kurz ist.
            "hinweis": "Discord verraet einem Bot seine DM-Kanaele nicht - "
                       "Flo fuehrt die Liste selbst.",
        })

    async def _api_sicht_dm_merken(self, request):
        """Eine Nutzer-ID von Hand hinzufuegen.

        Der Notausgang: wer weiss, mit wem Flo mal geschrieben hat, holt das
        Gespraech ueber die ID zurueck - Discord hat es noch."""
        self._guard(request)
        data = await self._json_objekt(request)
        uid = self._uid(data.get("id"))
        if uid is None:
            return web.json_response({"ok": False, "error": "Das ist keine Nutzer-ID."},
                                     status=400)
        try:
            kanal, user = await self._dm_kanal(uid)
        except discord.NotFound:
            return web.json_response({"ok": False, "error": "Diesen Nutzer gibt es nicht."},
                                     status=404)
        except Exception:  # noqa: BLE001
            log.exception("BotSicht: DM-Kanal konnte nicht geoeffnet werden")
            return web.json_response({"ok": False, "error": "Kanal nicht zu oeffnen"},
                                     status=500)
        # Nachsehen, ob dort ueberhaupt je etwas stand - sonst legt man sich
        # eine Liste voller leerer Gespraeche an.
        leer = True
        try:
            async for _m in kanal.history(limit=1):
                leer = False
        except Exception:  # noqa: BLE001
            leer = False        # im Zweifel behalten
        self._dm_merken(user, quelle="hand")
        return web.json_response({"ok": True, "leer": leer,
                                  "name": self._safe(lambda: str(user.display_name), str(uid))})

    # Genau das Format, das _forward_dm_to_owner in bot.py schreibt:
    #   📥 **DM von Name** (`123456789`):
    _DM_RELAY_RE = re.compile(r"DM von\s+(.+?)\*\*\s*\(`?(\d{5,25})`?\)")
    # Und das, was der Besitzer selbst tippt: 'flo dm 123 text' / 'flo dm <@123> text'
    _DM_BEFEHL_RE = re.compile(r"\bdm\s+<?@?!?(\d{5,25})>?", re.IGNORECASE)

    async def _api_sicht_dm_suche_start(self, request):
        """Startet die Suche nach alten DMs (laeuft nebenher, Fortschritt via GET)."""
        self._guard(request)
        data = await self._json_objekt(request)
        lauf = getattr(self, "_dm_suche", None)
        if lauf and lauf.get("laeuft"):
            return web.json_response({"ok": False, "error": "Die Suche laeuft schon."},
                                     status=409)
        gruendlich = self._flag(data.get("gruendlich"), False)
        self._dm_suche = {"laeuft": True, "schritt": "Start", "geprueft": 0,
                          "gesamt": 0, "gefunden": 0, "neu": [], "fehler": ""}
        aufgabe = asyncio.get_running_loop().create_task(self._dm_suche_lauf(gruendlich))
        self._sicht_tasks.add(aufgabe)
        aufgabe.add_done_callback(self._sicht_tasks.discard)
        return web.json_response({"ok": True, "gruendlich": gruendlich})

    async def _api_sicht_dm_suche(self, request):
        """Fortschritt der laufenden Suche."""
        self._guard(request)
        return web.json_response({"ok": True,
                                  "suche": getattr(self, "_dm_suche", None)})

    async def _dm_suche_lauf(self, gruendlich):
        """Alte DM-Bekanntschaften zurueckholen. Drei Wege, absteigend billig:

        1. DIE WEITERLEITUNGEN. bot.py schickt jede Fremd-DM an den Besitzer
           weiter - MIT Absender-ID im Text. Flos eigene DM mit dem Besitzer
           ist damit ein Verzeichnis aller, die ihm je geschrieben haben.
        2. DIE BEFEHLE. 'flo dm <id> <text>' steht in dem Kanal, in dem es
           getippt wurde. Das findet die Leute, denen Flo geschrieben hat.
        3. DAS ABKLOPFEN. Fuer jede Nutzer-ID, die Flo ueberhaupt kennt
           (Server-Caches, Wirtschaftsprofile, Namensverlauf), einmal den
           DM-Kanal oeffnen und nachsehen, ob dort etwas steht. Das ist der
           einzige VOLLSTAENDIGE Weg, kostet aber zwei Anfragen je Person -
           deshalb nur auf ausdruecklichen Wunsch ('gruendlich')."""
        stand = self._dm_suche
        try:
            owner_id = self._as_int(os.getenv("OWNER_ID", "0") or "0", 0)

            # --- Weg 1: die Weiterleitungen in der Owner-DM -----------------
            stand["schritt"] = "Weiterleitungen in deiner DM durchsehen"
            if owner_id:
                try:
                    kanal, _u = await self._dm_kanal(owner_id)
                    gesehen = 0
                    async for m in kanal.history(limit=BOTSICHT_DM_SUCHE_MAX):
                        gesehen += 1
                        stand["geprueft"] = gesehen
                        for name, uid in self._DM_RELAY_RE.findall(m.content or ""):
                            if self._dm_merken(int(uid), quelle="relay"):
                                self._dm_partner[str(int(uid))].setdefault(
                                    "name", name.strip())
                                stand["neu"].append(name.strip() or uid)
                                stand["gefunden"] += 1
                except Exception:  # noqa: BLE001
                    log.info("DM-Suche: Owner-Verlauf nicht lesbar", exc_info=True)

            # --- Weg 2: 'flo dm <id>' in den Serverkanaelen -----------------
            stand["schritt"] = "Server nach 'dm'-Befehlen durchsehen"
            for g in (getattr(self._client, "guilds", []) or []):
                for c in (self._safe(lambda: list(g.text_channels), []) or []):
                    r = self._sicht_rechte(c, g)
                    if not r.get("verlauf"):
                        continue
                    try:
                        async for m in c.history(limit=400):
                            text = (m.content or "")
                            if " dm " not in f" {text.lower()} ":
                                continue
                            for uid in self._DM_BEFEHL_RE.findall(text):
                                if self._dm_merken(int(uid), quelle="befehl"):
                                    stand["neu"].append(str(uid))
                                    stand["gefunden"] += 1
                    except Exception:  # noqa: BLE001
                        continue

            # --- Weg 3: jede bekannte ID abklopfen --------------------------
            if gruendlich:
                kandidaten = self._dm_kandidaten()
                stand["gesamt"] = len(kandidaten)
                stand["geprueft"] = 0
                stand["schritt"] = f"{len(kandidaten)} bekannte Leute abklopfen"
                for i, uid in enumerate(kandidaten, 1):
                    stand["geprueft"] = i
                    if str(uid) in self._dm_partner:
                        continue
                    try:
                        kanal, user = await self._dm_kanal(uid)
                        async for _m in kanal.history(limit=1):
                            self._dm_merken(user, quelle="abgeklopft")
                            stand["neu"].append(
                                self._safe(lambda: str(user.display_name), str(uid)))
                            stand["gefunden"] += 1
                            break
                    except Exception:  # noqa: BLE001
                        continue
                    # Freundlich zum Rate-Limit bleiben: das sind zwei Anfragen
                    # je Person, und der Bot soll waehrenddessen weiterlaufen.
                    await asyncio.sleep(0.25)
            stand["schritt"] = "fertig"
        except Exception as exc:  # noqa: BLE001
            log.exception("DM-Suche fehlgeschlagen")
            stand["fehler"] = str(exc)[:200]
            stand["schritt"] = "abgebrochen"
        finally:
            stand["laeuft"] = False
            del stand["neu"][40:]        # die Liste ist nur zum Anzeigen da

    def _dm_kandidaten(self):
        """Jede Nutzer-ID, die Flo irgendwoher kennt - Grundlage fuers
        Abklopfen. Bots fliegen raus, die lesen keine DMs."""
        ids = set()
        ich = self._safe(lambda: int(self._client.user.id), 0)
        for u in (self._safe(lambda: list(self._client.users), []) or []):
            if not self._safe(lambda: u.bot, False):
                ids.add(self._safe(lambda: int(u.id), 0))
        for g in (getattr(self._client, "guilds", []) or []):
            for m in (self._safe(lambda: list(g.members), []) or []):
                if not self._safe(lambda: m.bot, False):
                    ids.add(self._safe(lambda: int(m.id), 0))
        # Wer je Coins bekommen hat, hat auch je geschrieben.
        for uid in (self._users_dict() or {}):
            ids.add(self._as_int(uid, 0))
        try:
            import profil
            for uid in (profil.instance._store.data.get("users", {}) or {}):
                ids.add(self._as_int(uid, 0))
        except Exception:  # noqa: BLE001
            pass
        ids.discard(0)
        ids.discard(ich)
        return sorted(ids)

    # --- Struktur: Server, Kanaele, Mitglieder ----------------------------
    async def _api_sicht_guilds(self, request):
        """Wer ist Flo, und auf welchen Servern ist er?"""
        self._guard(request)
        ich = getattr(self._client, "user", None)
        out = []
        for g in (getattr(self._client, "guilds", []) or []):
            out.append({
                "id": str(self._safe(lambda: g.id, 0) or 0),
                "name": self._safe(lambda: str(g.name), "?") or "?",
                "icon": self._safe(lambda: g.icon.url if g.icon else None, None),
                # 'mitglieder' ist die echte Zahl vom Server, 'bekannt' die, die
                # Flo tatsaechlich im Cache hat. Die Luecke ist die Aussage.
                "mitglieder": self._safe(lambda: int(g.member_count or 0), 0) or 0,
                "bekannt": len(self._safe(lambda: list(g.members), []) or []),
            })
        out.sort(key=lambda e: self._fold(e["name"]))
        return web.json_response({
            "ok": True,
            "ich": {
                "id": str(self._safe(lambda: ich.id, "") or ""),
                "name": self._safe(lambda: str(ich.name), None) or self._bot_name,
                "avatar": self._safe(lambda: ich.display_avatar.url, None),
                "latenz": round(self._safe(lambda: float(self._client.latency) * 1000, 0.0) or 0.0),
            },
            "guilds": out,
            # Ehrlichkeit ueber den Blickwinkel: was Flo strukturell NICHT sieht.
            "intents": self._sicht_intents(),
        })

    def _sicht_intents(self):
        """Welche Intents an sind - daraus erklaeren sich die Luecken in Flos Bild."""
        i = self._safe(lambda: self._client.intents, None)
        if i is None:
            return {}
        return {
            "mitglieder": bool(self._safe(lambda: i.members, False)),
            "inhalt": bool(self._safe(lambda: i.message_content, False)),
            "praesenz": bool(self._safe(lambda: i.presences, False)),
            "voice": bool(self._safe(lambda: i.voice_states, False)),
        }

    def _sicht_rechte(self, kanal, guild):
        """Was Flo in diesem Kanal DARF - der Kern der ganzen Ansicht."""
        ich = self._safe(lambda: guild.me, None)
        if ich is None or not hasattr(kanal, "permissions_for"):
            # Direktnachricht: keine Rechteverwaltung, Flo darf alles.
            return {"lesen": True, "verlauf": True, "senden": True,
                    "dateien": True, "embeds": True, "verwalten": False,
                    "reagieren": True}
        p = self._safe(lambda: kanal.permissions_for(ich), None)
        if p is None:
            return {"lesen": False, "verlauf": False, "senden": False,
                    "dateien": False, "embeds": False, "verwalten": False,
                    "reagieren": False}
        return {
            "lesen": bool(p.read_messages),
            "verlauf": bool(p.read_message_history),
            "senden": bool(p.send_messages),
            "dateien": bool(p.attach_files),
            "embeds": bool(p.embed_links),
            "verwalten": bool(p.manage_messages),
            "reagieren": bool(p.add_reactions),
        }

    async def _api_sicht_channels(self, request):
        """Der Kanalbaum eines Servers - mit Flos Rechten an jedem Kanal.

        Kanaele ohne Leserecht kommen MIT, nur gesperrt markiert. Sie
        wegzulassen waere bequemer und genau falsch: die Frage 'warum sagt Flo
        da nichts?' beantwortet sich nur, wenn man den Kanal sieht."""
        self._guard(request)
        gid = self._as_int(request.query.get("guild"), 0)
        guild = self._guild_by_id(gid)
        if guild is None:
            return web.json_response({"ok": False, "error": "Server nicht gefunden"},
                                     status=404)
        text_k, voice_k = [], []
        for c in (self._safe(lambda: list(guild.channels), []) or []):
            ist_text = isinstance(c, (discord.TextChannel, discord.Thread))
            ist_voice = isinstance(c, (discord.VoiceChannel, discord.StageChannel))
            if not (ist_text or ist_voice):
                continue
            kat = self._safe(lambda: c.category, None)
            eintrag = {
                "id": str(self._safe(lambda: c.id, 0) or 0),
                "name": self._safe(lambda: str(c.name), "?") or "?",
                "thema": (self._safe(lambda: str(c.topic or ""), "") or "")[:200],
                "nsfw": bool(self._safe(lambda: c.is_nsfw(), False)),
                "thread": isinstance(c, discord.Thread),
                "position": self._safe(lambda: int(c.position), 0) or 0,
                "kategorie": self._safe(lambda: str(kat.name), None),
                "kategorie_id": str(self._safe(lambda: kat.id, 0) or 0),
                "rechte": self._sicht_rechte(c, guild),
            }
            if ist_voice:
                # Wer sitzt gerade drin? Das sieht Flo wirklich (voice_states).
                eintrag["drin"] = [
                    {"id": str(m.id), "name": self._safe(lambda: str(m.display_name), "?"),
                     "avatar": self._safe(lambda: m.display_avatar.url, None)}
                    for m in (self._safe(lambda: list(c.members), []) or [])[:25]
                ]
                voice_k.append(eintrag)
            else:
                text_k.append(eintrag)
        text_k.sort(key=lambda e: (e["position"], self._fold(e["name"])))
        voice_k.sort(key=lambda e: (e["position"], self._fold(e["name"])))
        # In welchem Voice haengt Flo gerade selbst?
        vc = self._safe(lambda: guild.voice_client, None)
        return web.json_response({
            "ok": True,
            "guild": {"id": str(guild.id), "name": guild.name,
                      "icon": self._safe(lambda: guild.icon.url if guild.icon else None, None)},
            "text": text_k, "voice": voice_k,
            "mein_voice": str(self._safe(lambda: vc.channel.id, "") or ""),
        })

    async def _api_sicht_members(self, request):
        """Die Mitglieder, die Flo KENNT. Ohne Members-Intent sind das nur die,
        die er im Chat oder Voice gesehen hat - genau das steht auch dran."""
        self._guard(request)
        gid = self._as_int(request.query.get("guild"), 0)
        guild = self._guild_by_id(gid)
        if guild is None:
            return web.json_response({"ok": False, "error": "Server nicht gefunden"},
                                     status=404)
        out = []
        for m in (self._safe(lambda: list(guild.members), []) or [])[:500]:
            rolle = None
            try:
                # Die oberste Rolle mit Farbe - dieselbe, die Discord anzeigt.
                farbige = [r for r in reversed(m.roles) if r.color and r.color.value]
                rolle = farbige[0].name if farbige else None
            except Exception:  # noqa: BLE001
                rolle = None
            out.append({
                "id": str(self._safe(lambda: m.id, 0) or 0),
                "name": self._safe(lambda: str(m.display_name), "?") or "?",
                "avatar": self._safe(lambda: m.display_avatar.url, None),
                "bot": bool(self._safe(lambda: m.bot, False)),
                "farbe": self._safe(lambda: self._sicht_farbe(m.color), None),
                "rolle": rolle,
                "status": self._safe(lambda: str(m.status), "offline") or "offline",
                "voice": bool(self._safe(lambda: m.voice is not None, False)),
            })
        out.sort(key=lambda e: (e["bot"], self._fold(e["name"])))
        return web.json_response({
            "ok": True, "mitglieder": out,
            "gesamt": self._safe(lambda: int(guild.member_count or 0), 0) or 0,
            "intents": self._sicht_intents(),
        })

    # --- Verlauf und Eingreifen -------------------------------------------
    def _sicht_kanal(self, cid):
        """Kanal-Objekt zu einer ID - oder None."""
        kid = self._uid(cid)
        if kid is None or self._client is None:
            return None
        return self._safe(lambda: self._client.get_channel(kid), None)

    async def _api_sicht_messages(self, request):
        """Der Verlauf eines Kanals, so wie Flo ihn abrufen darf.

        Fehlt das Recht 'Nachrichtenverlauf lesen', kommt hier ehrlich eine
        Absage statt einer leeren Liste - sonst sieht es aus, als sei der
        Kanal still."""
        self._guard(request)
        # ?dm=<uid> statt ?channel=<cid>: den privaten Verlauf mit einer Person.
        # Der Kanal wird dabei notfalls neu geoeffnet - der Verlauf ist danach
        # vollstaendig da, auch wenn Flo diese DM seit dem Neustart nie sah.
        dm_id = self._uid(request.query.get("dm"))
        if dm_id is not None:
            try:
                kanal, user = await self._dm_kanal(dm_id)
            except discord.NotFound:
                return web.json_response({"ok": False, "error": "Nutzer gibt es nicht"},
                                         status=404)
            except Exception:  # noqa: BLE001
                log.exception("BotSicht: DM-Kanal nicht zu oeffnen")
                return web.json_response({"ok": False, "error": "DM nicht zu oeffnen"},
                                         status=500)
            self._dm_partner.setdefault(str(dm_id), {})
        else:
            kanal = self._sicht_kanal(request.query.get("channel"))
        if kanal is None:
            return web.json_response({"ok": False, "error": "Kanal nicht gefunden"},
                                     status=404)
        if not hasattr(kanal, "history"):
            return web.json_response({"ok": False, "error": "Kanal hat keinen Verlauf"},
                                     status=400)
        guild = getattr(kanal, "guild", None)
        rechte = self._sicht_rechte(kanal, guild) if guild is not None else \
            {"lesen": True, "verlauf": True, "senden": True, "dateien": True,
             "embeds": True, "verwalten": False, "reagieren": True}
        if not rechte.get("verlauf"):
            return web.json_response(
                {"ok": False, "gesperrt": True, "rechte": rechte,
                 "error": "Flo darf den Verlauf dieses Kanals nicht lesen."},
                status=403)
        anzahl = max(1, min(BOTSICHT_VERLAUF_MAX,
                            self._as_int(request.query.get("limit"), 50)))
        vor = self._uid(request.query.get("before"))
        try:
            kwargs = {"limit": anzahl}
            if vor is not None:
                kwargs["before"] = discord.Object(id=vor)
            roh = [m async for m in kanal.history(**kwargs)]
        except discord.Forbidden:
            return web.json_response(
                {"ok": False, "gesperrt": True, "rechte": rechte,
                 "error": "Discord verweigert den Verlauf (Rechte gerade geaendert?)."},
                status=403)
        except Exception:  # noqa: BLE001
            log.exception("BotSicht: Verlauf konnte nicht geladen werden")
            return web.json_response({"ok": False, "error": "Verlauf nicht ladbar"},
                                     status=500)
        # history() liefert neueste zuerst - die Oberflaeche liest von oben.
        msgs = [self._sicht_msg(m) for m in reversed(roh)]
        return web.json_response({
            "ok": True, "messages": msgs, "rechte": rechte,
            "kanal": {"id": str(kanal.id),
                      "name": self._safe(lambda: str(kanal.name), None) or "Direktnachricht",
                      "thema": (self._safe(lambda: str(kanal.topic or ""), "") or "")[:200]},
            # Weniger als angefragt -> wir sind am Anfang des Kanals.
            "mehr": len(roh) >= anzahl,
        })

    @staticmethod
    def _sicht_pings():
        """Aus dem Panel wird NIE @everyone/@here ausgeloest und keine Rolle
        angepingt: das laesst sich nicht zurueckholen, und ein Tippfehler im
        Eingabefeld soll nicht den halben Server aufwecken. Einzelne Leute und
        die Person, auf die man antwortet, duerfen sehr wohl gepingt werden -
        sonst kann man aus dieser Ansicht nicht sinnvoll mitreden."""
        return discord.AllowedMentions(everyone=False, roles=False,
                                       users=True, replied_user=True)

    async def _api_sicht_send(self, request):
        """Als Flo in einen Kanal schreiben - optional als Antwort."""
        self._guard(request)
        data = await self._json_objekt(request)
        dm_id = self._uid(data.get("dm"))
        if dm_id is not None:
            try:
                kanal, _user = await self._dm_kanal(dm_id)
            except Exception:  # noqa: BLE001
                return web.json_response({"ok": False, "error": "DM nicht zu oeffnen"},
                                         status=404)
        else:
            kanal = self._sicht_kanal(data.get("channel"))
        if kanal is None:
            return web.json_response({"ok": False, "error": "Kanal nicht gefunden"},
                                     status=404)
        text = self._text(data.get("text"), 1900)
        if text is None:
            return web.json_response({"ok": False, "error": "text?"}, status=400)
        if not text:
            return web.json_response({"ok": False, "error": "kein text"}, status=400)
        if not hasattr(kanal, "send"):
            return web.json_response({"ok": False, "error": "Kanal kann keine Nachrichten"},
                                     status=400)
        kwargs = {"allowed_mentions": self._sicht_pings()}
        antwort_auf = self._uid(data.get("reply_to"))
        if antwort_auf is not None:
            # Nicht erst holen - das spart einen API-Aufruf. Aber es MUSS eine
            # MessageReference sein: discord.py ruft to_message_reference_dict()
            # auf, und discord.Object hat die Methode nicht ("reference parameter
            # must be Message, MessageReference, or PartialMessage"). Mit Object
            # scheiterte JEDE Antwort aus der BotSicht - der frueher hier
            # stehende Kommentar behauptete das Gegenteil.
            # fail_if_not_exists=False liefert genau das Versprechen: ist die
            # Nachricht weg, wird daraus eine normale Nachricht.
            kwargs["reference"] = discord.MessageReference(
                message_id=antwort_auf,
                channel_id=getattr(kanal, "id", 0),
                guild_id=getattr(getattr(kanal, "guild", None), "id", None),
                fail_if_not_exists=False)
            kwargs["mention_author"] = True
        try:
            msg = await kanal.send(text, **kwargs)
        except discord.Forbidden:
            return web.json_response(
                {"ok": False, "error": ("Die Person hat DMs zu oder blockiert Flo."
                                        if dm_id is not None
                                        else "Flo darf hier nicht schreiben.")}, status=403)
        except Exception:  # noqa: BLE001
            log.exception("BotSicht: Senden fehlgeschlagen")
            return web.json_response({"ok": False, "error": "senden fehlgeschlagen"},
                                     status=500)
        # Sofort in den Strom legen, damit die eigene Nachricht ohne Verzoegerung
        # im Panel steht. Normalerweise kommt sie gleich NOCHMAL ueber das
        # Gateway (on_message feuert auch fuer Flos eigene Nachrichten) - die
        # Oberflaeche wirft Doppelte anhand der Nachrichten-ID weg. Ohne diesen
        # Aufruf haengt das Feld nach dem Abschicken kurz leer da, und auf einem
        # Bot ohne Nachrichten-Intent kaeme die Nachricht nie an.
        self.sicht_notiere(msg)
        return web.json_response({"ok": True, "msg": self._sicht_msg(msg)})

    async def _api_sicht_typing(self, request):
        """Das Tipp-Zeichen im Kanal ausloesen - wirkt fuer die Leute im Chat
        so, als wuerde Flo gerade schreiben. Genau das tut er ja auch."""
        self._guard(request)
        data = await self._json_objekt(request)
        kanal = self._sicht_kanal(data.get("channel"))
        if kanal is None or not hasattr(kanal, "typing"):
            return web.json_response({"ok": False, "error": "Kanal nicht gefunden"},
                                     status=404)
        try:
            await kanal.typing()
        except Exception:  # noqa: BLE001 - reine Kosmetik, nie ein Fehler
            log.debug("BotSicht: Tipp-Zeichen fehlgeschlagen", exc_info=True)
        return web.json_response({"ok": True})

    async def _api_sicht_react(self, request):
        """Als Flo auf eine Nachricht reagieren (oder die Reaktion zuruecknehmen)."""
        self._guard(request)
        data = await self._json_objekt(request)
        kanal = self._sicht_kanal(data.get("channel"))
        mid = self._uid(data.get("message"))
        emoji = self._text(data.get("emoji"), 64)
        if kanal is None or mid is None:
            return web.json_response({"ok": False, "error": "Kanal/Nachricht?"}, status=400)
        if not emoji:
            return web.json_response({"ok": False, "error": "emoji?"}, status=400)
        weg = self._flag(data.get("weg"), False)
        try:
            msg = await kanal.fetch_message(mid)
            if weg:
                await msg.remove_reaction(emoji, self._client.user)
            else:
                await msg.add_reaction(emoji)
        except discord.Forbidden:
            return web.json_response({"ok": False, "error": "Flo darf hier nicht reagieren."},
                                     status=403)
        except Exception:  # noqa: BLE001
            log.info("BotSicht: Reaktion fehlgeschlagen", exc_info=True)
            return web.json_response({"ok": False, "error": "Reaktion abgelehnt "
                                      "(unbekanntes Emoji?)"}, status=400)
        return web.json_response({"ok": True})

    async def _api_sicht_delete(self, request):
        """Eine Nachricht loeschen - so weit Flos Rechte reichen."""
        self._guard(request)
        data = await self._json_objekt(request)
        kanal = self._sicht_kanal(data.get("channel"))
        mid = self._uid(data.get("message"))
        if kanal is None or mid is None:
            return web.json_response({"ok": False, "error": "Kanal/Nachricht?"}, status=400)
        try:
            msg = await kanal.fetch_message(mid)
            await msg.delete()
        except discord.Forbidden:
            return web.json_response(
                {"ok": False, "error": "Flo fehlt 'Nachrichten verwalten'."}, status=403)
        except discord.NotFound:
            return web.json_response({"ok": False, "error": "Nachricht ist schon weg."},
                                     status=404)
        except Exception:  # noqa: BLE001
            log.exception("BotSicht: Loeschen fehlgeschlagen")
            return web.json_response({"ok": False, "error": "loeschen fehlgeschlagen"},
                                     status=500)
        self._sicht_nr += 1
        ereignis = {"art": "weg", "nr": self._sicht_nr,
                    "msg": {"id": str(mid), "kanal": str(kanal.id)}}
        self._sicht.append(ereignis)
        self._sicht_push(ereignis)
        return web.json_response({"ok": True})

    # --- Einstellungen je Server -----------------------------------------
    def _guild_by_id(self, gid):
        """Server-Objekt zu einer ID (None, wenn Flo dort nicht ist)."""
        if not gid or self._client is None:
            return None
        try:
            return self._client.get_guild(int(gid))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _cfg_wert(typ, wert):
        """Einen Einstellungs-Wert fuers Formular aufbereiten. Kanal-IDs gehen als
        TEXT raus: eine Discord-ID ist groesser als JavaScripts genaue Zahlen,
        als Zahl kaeme im Browser eine falsche ID zurueck."""
        if typ == "channel":
            return str(wert or "")
        if typ == "channels":
            return ",".join(str(c) for c in (wert or []))
        return wert

    async def _api_guildcfg(self, request):
        """Alles, was die Server-Seite fuer EINEN Server braucht: seine
        Einstellungen, die Kanalliste zum Auswaehlen und seine Feature-Schalter.
        Ein Aufruf statt drei - die Seite baut sich daraus komplett auf."""
        self._guard(request)
        gid = self._as_int(request.query.get("guild"), 0)
        guild = self._guild_by_id(gid)
        if guild is None:
            return web.json_response({"ok": False, "error": "Server nicht gefunden"},
                                     status=404)
        try:
            import guildcfg
            settings = []
            for e in guildcfg.alle(gid):
                # Werte fuer HTML aufbereiten: IDs als Text (JS-Zahlen sind zu
                # klein fuer Discord-Snowflakes), Listen als Komma-Text.
                settings.append({**e,
                                 "wert": self._cfg_wert(e["typ"], e["wert"]),
                                 "standard": self._cfg_wert(e["typ"], e["standard"]),
                                 "text": guildcfg.text(gid, e["key"])})
            kanaele = [{"id": str(c.id), "name": c.name}
                       for c in (getattr(guild, "text_channels", None) or [])]
        except Exception:  # noqa: BLE001
            log.exception("Server-Einstellungen konnten nicht gelesen werden")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)
        try:
            import features
            feats = features.state(self._loaded_flags(), gid)
        except Exception:  # noqa: BLE001
            feats = []
        return web.json_response({
            "ok": True,
            "guild": {"id": str(guild.id), "name": guild.name},
            "settings": settings,
            "channels": kanaele,
            "features": feats,
        })

    async def _api_guildcfg_set(self, request):
        """Eine Einstellung eines Servers setzen (oder mit 'standard' zuruecksetzen)."""
        self._guard(request)
        data = await self._json_objekt(request)
        gid = self._as_int(data.get("guild"), 0)
        key = str(data.get("key", "")).strip()
        wert = data.get("value")
        if isinstance(wert, bool):
            wert = "an" if wert else "aus"
        guild = self._guild_by_id(gid)
        if guild is None:
            return web.json_response({"ok": False, "error": "Server nicht gefunden"},
                                     status=404)
        try:
            import guildcfg
            ok, _wert, fehler = await guildcfg.setzen(gid, key, str(wert), guild)
            if not ok:
                return web.json_response({"ok": False, "error": fehler}, status=400)
            return web.json_response({"ok": True, "key": key,
                                      "text": guildcfg.text(gid, key)})
        except Exception:  # noqa: BLE001
            log.exception("Server-Einstellung konnte nicht gesetzt werden")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)


# --- Singleton + Modul-API ---------------------------------------------------
instance = WebPanel()

setup = instance.setup
is_enabled = instance.is_enabled
start = instance.start
stop = instance.stop
# BotSicht: bot.py meldet hier JEDE gesehene Nachricht (heisser Pfad, siehe dort).
sicht_notiere = instance.sicht_notiere
# Und hier die Stellen, an denen Flo von sich aus eine DM verschickt - Discord
# fuehrt kein Verzeichnis der DM-Kanaele, also fuehrt Flo es selbst.
sicht_dm_merken = instance._dm_merken
