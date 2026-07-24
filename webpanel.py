"""Lokales Web-Panel (Standard: Port 9123) zum Verwalten von Flo.

Laeuft IM Bot-Prozess (gleicher asyncio-Loop wie discord.py), operiert also direkt
auf den Live-Daten (economy, floaktie, lotto, merchant, admin, discord-Client) -
Aenderungen sind sofort wirksam und werden ganz normal gespeichert.

Features (JSON-API + schicke Single-Page-Oberflaeche webpanel.html):
- Login (Standard Secoolio/Secoolio, per .env aenderbar)
- Uebersicht/Statistiken: Nutzer, Coins, Server, Aktie, Lotto, Level-Verteilung
- Nutzer verwalten: suchen, Profil ansehen, Flo Coins geben/nehmen/setzen,
  XP anpassen, Titel geben/nehmen
- Server verwalten: Guilds ansehen, Sendepause schalten, Ansage posten

Sicherheit: Das Panel ist NUR mit Login erreichbar. Trotzdem: Zugangsdaten in der
.env setzen (WEBPANEL_USER/WEBPANEL_PASS) und den Port nicht offen ins Internet
haengen - gedacht ist es fuers lokale Netz / hinter der Firewall.

Abschaltbar mit WEBPANEL_ENABLED=0. Host/Port: WEBPANEL_HOST / WEBPANEL_PORT.
"""

import logging
import os
import secrets
import time
from pathlib import Path

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
        self._tokens = {}      # token -> Ablauf-Timestamp
        self._host = "0.0.0.0"
        self._port = 9123
        self._user = "Secoolio"
        self._pass = "Secoolio"
        self._ttl = 12 * 3600
        self._html_cache = None
        self._bot_name = "Flo"

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
        except ValueError:
            self._port = 9123
        self._user = os.getenv("WEBPANEL_USER", "Secoolio") or "Secoolio"
        self._pass = os.getenv("WEBPANEL_PASS", "Secoolio") or "Secoolio"
        self._enabled = True
        log.info("Web-Panel bereit (startet in on_ready auf %s:%d).", self._host, self._port)
        return True

    def is_enabled(self):
        return self._enabled

    def _build_app(self):
        """Baut die aiohttp-App mit allen Routen (auch von Tests genutzt)."""
        app = web.Application()
        app.add_routes([
            web.get("/", self._index),
            web.get("/panel", self._index),
            web.post("/api/login", self._api_login),
            web.get("/api/overview", self._api_overview),
            web.get("/api/users", self._api_users),
            web.get("/api/user/{uid}", self._api_user),
            web.post("/api/user/coins", self._api_coins),
            web.post("/api/user/xp", self._api_xp),
            web.post("/api/user/title", self._api_title),
            web.get("/api/servers", self._api_servers),
            web.post("/api/server/sendepause", self._api_sendepause),
            web.post("/api/server/announce", self._api_announce),
            web.get("/api/features", self._api_features),
            web.post("/api/feature", self._api_feature),
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
            log.info("🌐 Web-Panel laeuft auf http://%s:%d (Login: %s)",
                     self._host, self._port, self._user)
        except OSError as exc:
            log.error("Web-Panel konnte Port %d nicht binden: %s", self._port, exc)
            self._runner = None

    async def stop(self):
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._runner = None

    # --- Auth -------------------------------------------------------------
    def _new_token(self):
        tok = secrets.token_urlsafe(32)
        self._tokens[tok] = time.time() + self._ttl
        return tok

    def _valid(self, request):
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

    async def _api_login(self, request):
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        user = str(data.get("user", ""))
        pw = str(data.get("pass", ""))
        ok = (secrets.compare_digest(user, self._user)
              and secrets.compare_digest(pw, self._pass))
        if not ok:
            return web.json_response({"ok": False, "error": "Falsche Zugangsdaten"}, status=401)
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
        xp = int(prof.get("xp", 0))
        level = economy.instance._level_only(xp) if economy.is_enabled() else 0
        shares = 0
        try:
            import floaktie
            if floaktie.is_enabled():
                shares = floaktie.instance.shares_of(uid)
        except Exception:  # noqa: BLE001
            pass
        return {
            "id": str(uid),
            "name": prof.get("name") or f"User {uid}",
            "coins": int(prof.get("coins", 0)),
            "xp": xp,
            "level": level,
            "title": prof.get("title", ""),
            "titles": len(prof.get("owned", []) or []),
            "shares": shares,
            "streak": int(prof.get("streak", 0)),
            "msgs": int(prof.get("msgs", 0)),
            "voice_secs": int(prof.get("voice_secs", 0)),
        }

    def _parse_amount(self, raw):
        """Nimmt Zahl oder '1k'/'2m' und gibt einen int zurueck (0 bei Murks)."""
        if isinstance(raw, (int, float)):
            return int(raw)
        s = str(raw or "").strip()
        try:
            return int(s)
        except ValueError:
            pass
        if economy.is_enabled():
            v = economy.parse_amount(s)
            if v:
                return int(v)
        return 0

    # --- API: Uebersicht --------------------------------------------------
    async def _api_overview(self, request):
        self._guard(request)
        users = self._users_dict()
        rows = [self._user_row(uid, p) for uid, p in users.items()]
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
        # Aktie.
        floaktie_history = []
        try:
            import floaktie
            if floaktie.is_enabled():
                stats["floaktie_price"] = floaktie.instance.price()
                stats["floaktie_holders"] = floaktie.instance.holders_count()
                stats["floaktie_change"] = round(floaktie.instance._change_pct(1), 2)
                floaktie_history = [h.get("price", 0)
                                    for h in floaktie.instance._state().get("history", [])][-30:]
        except Exception:  # noqa: BLE001
            pass
        # Lotto.
        try:
            import lotto
            if lotto.is_enabled():
                st = lotto.instance._state()
                stats["lotto_jackpot"] = int(st.get("jackpot", 0))
                stats["lotto_house"] = int(st.get("house", 0))
        except Exception:  # noqa: BLE001
            pass
        # Haendler.
        try:
            import merchant
            if merchant.is_enabled():
                stats["merchant_present"] = merchant.instance.is_present()
        except Exception:  # noqa: BLE001
            pass
        # Sendepause.
        try:
            import admin
            stats["sendepause"] = admin.is_locked() if admin.is_enabled() else False
        except Exception:  # noqa: BLE001
            stats["sendepause"] = False
        return web.json_response({
            "ok": True, "bot_name": self._bot_name, "stats": stats,
            "top_coins": top_coins, "top_shares": top_shares,
            "level_dist": [{"band": k, "count": v} for k, v in bands.items()],
            "floaktie_history": floaktie_history,
        })

    # --- API: Nutzerliste -------------------------------------------------
    async def _api_users(self, request):
        self._guard(request)
        q = (request.query.get("q", "") or "").strip().lower()
        sort = request.query.get("sort", "coins")
        try:
            page = max(1, int(request.query.get("page", "1")))
            size = min(100, max(5, int(request.query.get("size", "25"))))
        except ValueError:
            page, size = 1, 25
        rows = [self._user_row(uid, p) for uid, p in self._users_dict().items()]
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in r["id"]]
        keyf = {"coins": lambda r: r["coins"], "level": lambda r: r["xp"],
                "shares": lambda r: r["shares"], "msgs": lambda r: r["msgs"],
                "name": lambda r: r["name"].lower()}.get(sort, lambda r: r["coins"])
        rows.sort(key=keyf, reverse=(sort != "name"))
        total = len(rows)
        start = (page - 1) * size
        return web.json_response({
            "ok": True, "total": total, "page": page,
            "pages": max(1, (total + size - 1) // size),
            "users": rows[start:start + size],
        })

    async def _api_user(self, request):
        self._guard(request)
        uid = request.match_info.get("uid", "")
        users = self._users_dict()
        prof = users.get(str(uid))
        if prof is None:
            return web.json_response({"ok": False, "error": "unbekannt"}, status=404)
        row = self._user_row(uid, prof)
        row["owned"] = [dict(o) for o in prof.get("owned", []) or []]
        row["last_daily"] = prof.get("last_daily", "")
        return web.json_response({"ok": True, "user": row})

    # --- API: Coins geben/nehmen/setzen -----------------------------------
    async def _api_coins(self, request):
        self._guard(request)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        if not economy.is_enabled():
            return web.json_response({"ok": False, "error": "economy aus"}, status=400)
        uid = str(data.get("id", "")).strip()
        action = str(data.get("action", "give"))
        amount = self._parse_amount(data.get("amount", 0))
        if not uid or amount < 0:
            return web.json_response({"ok": False, "error": "ungueltig"}, status=400)
        try:
            uid_int = int(uid)
        except ValueError:
            return web.json_response({"ok": False, "error": "ungueltige id"}, status=400)
        if action == "give":
            economy.add_coins(uid_int, amount, reason="panel")
        elif action == "take":
            economy.add_coins(uid_int, -amount, reason="panel")
        elif action == "set":
            cur = economy.get_coins(uid_int)
            economy.add_coins(uid_int, amount - cur, reason="panel")
        else:
            return web.json_response({"ok": False, "error": "aktion?"}, status=400)
        await economy.flush()
        return web.json_response({"ok": True, "coins": economy.get_coins(uid_int)})

    async def _api_xp(self, request):
        self._guard(request)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        if not economy.is_enabled():
            return web.json_response({"ok": False, "error": "economy aus"}, status=400)
        try:
            uid_int = int(str(data.get("id", "")).strip())
        except ValueError:
            return web.json_response({"ok": False, "error": "id?"}, status=400)
        action = str(data.get("action", "give"))
        amount = self._parse_amount(data.get("amount", 0))
        prof = economy.instance._profile(uid_int)
        if action == "set":
            prof["xp"] = max(0, amount)
        else:
            prof["xp"] = max(0, int(prof.get("xp", 0)) + amount)
        await economy.flush()
        return web.json_response({"ok": True, "xp": prof["xp"],
                                  "level": economy.instance._level_only(prof["xp"])})

    async def _api_title(self, request):
        self._guard(request)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        if not economy.is_enabled():
            return web.json_response({"ok": False, "error": "economy aus"}, status=400)
        try:
            uid_int = int(str(data.get("id", "")).strip())
        except ValueError:
            return web.json_response({"ok": False, "error": "id?"}, status=400)
        action = str(data.get("action", "grant"))
        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "error": "titel?"}, status=400)
        if action == "remove":
            economy.remove_title(uid_int, text)
        else:
            label = str(data.get("label", "")).strip() or text
            rarity = str(data.get("rarity", "selten")).strip() or "selten"
            economy.grant_title(uid_int, text, label, rarity)
        # Rolle nachziehen, falls das Mitglied auffindbar ist (best effort).
        try:
            guild = self._client.get_guild(int(os.getenv("GUILD_ID", "0") or "0")) if self._client else None
            member = guild.get_member(uid_int) if guild else None
            if member is not None:
                await economy.sync_role(member)
        except Exception:  # noqa: BLE001
            pass
        await economy.flush()
        return web.json_response({"ok": True, "titles": economy.list_titles(uid_int)})

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
        return web.json_response({"ok": True, "guilds": out})

    async def _api_sendepause(self, request):
        self._guard(request)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        on = bool(data.get("on", True))
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
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        text = str(data.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "error": "kein text"}, status=400)
        cid = data.get("channel_id")
        try:
            channel = None
            if cid:
                channel = self._client.get_channel(int(cid))
            if channel is None:
                gid = int(os.getenv("GUILD_ID", "0") or "0")
                guild = self._client.get_guild(gid) if self._client else None
                channel = guild.system_channel if guild else None
            if channel is None:
                return web.json_response({"ok": False, "error": "kein channel"}, status=400)
            await channel.send(text)
        except Exception:  # noqa: BLE001
            log.exception("Ansage via Panel fehlgeschlagen")
            return web.json_response({"ok": False, "error": "senden fehlgeschlagen"}, status=500)
        return web.json_response({"ok": True})

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
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        key = str(data.get("key", "")).strip()
        on = bool(data.get("on", True))
        try:
            import features
            # Nicht geladene Module kann man nicht per Schalter aktivieren.
            if on and not self._loaded_flags().get(key, False):
                return web.json_response({"ok": False,
                                          "error": "Modul ist nicht geladen (Neustart nötig)"}, status=400)
            res = await features.set_feature(key, on)
            if res is None:
                return web.json_response({"ok": False, "error": "unbekanntes Feature"}, status=400)
        except Exception:  # noqa: BLE001
            log.exception("Feature-Schalter fehlgeschlagen")
            return web.json_response({"ok": False, "error": "fehler"}, status=500)
        return web.json_response({"ok": True, "key": key, "on": res})


# --- Singleton + Modul-API ---------------------------------------------------
instance = WebPanel()

setup = instance.setup
is_enabled = instance.is_enabled
start = instance.start
stop = instance.stop
