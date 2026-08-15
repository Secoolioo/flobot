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
Das Panel laeuft standardmaessig OHNE LOGIN (WEBPANEL_AUTH=0, so gewuenscht). Wer
die Adresse erreicht, kann damit alles: Coins vergeben, Ansagen im Namen des Bots
posten, den Kurs setzen, den Bot aktualisieren und neu starten. Betreibe es
deshalb NUR im eigenen Netz bzw. hinter der Firewall und haenge den Port nicht
offen ins Internet.

    WEBPANEL_AUTH=1      Login wieder verlangen (WEBPANEL_USER/WEBPANEL_PASS)
    WEBPANEL_HOST=127.0.0.1   nur lokal erreichbar
    WEBPANEL_ENABLED=0   Panel ganz aus

Host/Port: WEBPANEL_HOST / WEBPANEL_PORT (Standard 0.0.0.0:9123).
"""

import asyncio
import logging
import os
import secrets
import time
import unicodedata
from pathlib import Path

import discord

import numfmt

import economy

try:
    from aiohttp import web
except Exception:  # noqa: BLE001 - ohne aiohttp laeuft das Panel eben nicht
    web = None

log = logging.getLogger("dcbot.webpanel")

_HTML_PATH = Path(__file__).resolve().parent / "webpanel.html"


class WebPanel:
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
        self._bot_name = "Flo"
        self._av_cache = {}    # uid -> (avatar_url|None, ablauf) fuer /api/avatar
        self._fails = {}       # ip -> (Fehlversuche, gesperrt_bis) gegen Raten
        self._auth = False     # Login verlangen? (WEBPANEL_AUTH, Standard aus)

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("WEBPANEL_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Web-Panel aus (WEBPANEL_ENABLED=0).")
            return False
        if web is None:
            log.warning("Web-Panel aus: aiohttp nicht verfuegbar.")
            return False
        self._host = os.getenv("WEBPANEL_HOST", "0.0.0.0").strip() or "0.0.0.0"
        try:
            self._port = int(os.getenv("WEBPANEL_PORT", "9123") or "9123")
        except (TypeError, ValueError):
            self._port = 9123
        if not 1 <= self._port <= 65535:
            log.warning("WEBPANEL_PORT=%s ist kein gueltiger Port - nehme 9123.", self._port)
            self._port = 9123
        self._user = os.getenv("WEBPANEL_USER", "Secoolio") or "Secoolio"
        self._pass = os.getenv("WEBPANEL_PASS", "Secoolio") or "Secoolio"
        # Login standardmaessig AUS (so gewuenscht). Mit WEBPANEL_AUTH=1 wieder an.
        self._auth = os.getenv("WEBPANEL_AUTH", "0").strip().lower() in (
            "1", "true", "yes", "on")
        self._enabled = True
        log.info("Web-Panel bereit (startet in on_ready auf %s:%d).", self._host, self._port)
        if not self._auth:
            # Bewusst nur INFO und einzeilig: der Betrieb im eigenen Netz ohne
            # Login ist so gewollt. Eine WARNUNG bei jedem Neustart waere blosses
            # Genoergel - der Hinweis, wie man ihn zurueckholt, reicht.
            log.info("Web-Panel ohne Login (WEBPANEL_AUTH=0, so eingestellt) - "
                     "zurueckholen mit WEBPANEL_AUTH=1 in der .env.")
        return True

    def is_enabled(self):
        return self._enabled

    def _build_app(self):
        """Baut die aiohttp-App mit allen Routen (auch von Tests genutzt)."""
        app = web.Application()
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
            web.get("/api/avatar/{uid}", self._api_avatar),
            web.get("/api/features", self._api_features),
            web.post("/api/feature", self._api_feature),
            web.get("/api/guildcfg", self._api_guildcfg),
            web.post("/api/guildcfg", self._api_guildcfg_set),
            web.post("/api/update", self._api_update),
        ])
        return app

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

    def _guard(self, request):
        if not self._valid(request):
            raise web.HTTPUnauthorized(text='{"ok":false,"error":"unauthorized"}',
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

    def _user_row(self, uid, prof):
        # Kaputte/halbe Profile (z. B. coins=null nach einem Absturz) duerfen die
        # Liste nicht komplett abschiessen - jedes Feld wird defensiv gelesen.
        if not isinstance(prof, dict):
            prof = {}
        xp = max(0, self._as_int(prof.get("xp"), 0))
        level = 0
        if economy.is_enabled():
            level = self._safe(lambda: economy.instance._level_only(xp), 0) or 0
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

    def _all_rows(self):
        """Alle Zeilen: economy-Profile PLUS reine Aktien-Halter.

        Wer nur Anteile besitzt (z. B. per Panel gesetzt, ohne je Coins gehabt zu
        haben), tauchte vorher in keiner Liste auf - zaehlte aber in den
        Boersenwert. Genau so verschwinden Anteile aus der Statistik."""
        rows = [self._user_row(uid, p) for uid, p in list(self._users_dict().items())]
        gesehen = {r["id"] for r in rows}
        for uid, n in self._holdings_items():
            if n > 0 and uid not in gesehen:
                gesehen.add(uid)
                rows.append(self._user_row(uid, {}))
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
            return {}
        return data if isinstance(data, dict) else {}

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
        rows = self._all_rows()
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
        rows = self._all_rows()
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
