"""Minecraft-Leaderboard fuer Flo: zeigt auf 'flo mcleaderboard' die besten
Statistiken eines Minecraft-Servers (wer am meisten abgebaut hat, welche Bloecke,
plus Haupt-Stats wie ingame) als richtig schicke Minecraft-Style-Grafik.

Die Roh-Statistiken kommen aus den Vanilla-Stat-Dateien des Servers
(<welt>/stats/<uuid>.json). Da Bot und MC-Server i. d. R. auf VERSCHIEDENEN
Maschinen laufen, gibt es zwei Quellen (per .env umschaltbar):

  * HTTP  (MC_STATS_URL): die mitgelieferte 'Flo MC Bridge'-.jar stellt die Stats
    token-geschuetzt unter z. B. http://<mc-host>:4918/leaderboard bereit.
  * Datei (MC_STATS_DIR): laeuft der Server auf derselben Maschine, liest Flo die
    stats-Dateien direkt (praktisch zum Testen).

Bewusst entkoppelt: ohne Konfiguration ist das Feature einfach aus, der restliche
Bot laeuft normal weiter. Die GESAMTE Aggregation passiert hier in Python - die
.jar/Bridge liefert nur die Roh-Stats (eine einzige Aggregations-Implementierung).
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os

import discord

import ai
import render

log = logging.getLogger("dcbot.minecraft")

# Sentinel: minecraft hat selbst geantwortet (Bild/Embed) -> bot.py schweigt.
HANDLED = object()

# --- Konfiguration (in setup() aus der .env gelesen) ---------------------
_enabled = False
_bot_name = "Flo"
_url = ""
_token = ""
_dir = ""
_usercache = ""
_server_name = "Minecraft"
_version = ""
_limit = 5
_timeout = 8.0
_avatars = True
# Flatternde/instabile Netze: den Stats-Abruf mehrfach probieren, bevor wir
# "nicht erreichbar" melden. So killt ein kurzer Verbindungs-Aussetzer nicht
# gleich den ganzen Befehl.
_HTTP_RETRIES = 4
_HTTP_RETRY_DELAY = 1.2

# Bots/Farm-Spieler aus dem Leaderboard raushalten.
#  - feste Namen (genaue Treffer, klein geschrieben) - per MC_EXCLUDE erweiterbar
#  - Heuristik: Name endet auf eine Bot-Endung (farm/bot/afk) oder enthaelt sie
#    (per MC_EXCLUDE_PATTERNS erweiterbar; per MC_EXCLUDE_BOTS=0 abschaltbar).
_BOT_EXACT_DEFAULT = {"ominousfarm", "hoglinfarm", "froglightfarm",
                      "creeperfarm", "scvb", "goid"}
_BOT_SUFFIX_DEFAULT = ("farm", "bot", "afk")     # Name endet so -> Bot
_BOT_SUBSTR_DEFAULT = ("afkbot", "_bot", "_farm")  # Name enthaelt so -> Bot
_exclude_exact: "set[str]" = set(_BOT_EXACT_DEFAULT)
_exclude_suffix: "tuple[str, ...]" = _BOT_SUFFIX_DEFAULT
_exclude_substr: "tuple[str, ...]" = _BOT_SUBSTR_DEFAULT
_exclude_heuristic = True


def setup() -> bool:
    """Aktiviert das Feature, wenn es nicht abgeschaltet ist UND eine Quelle
    (HTTP-URL oder lokaler stats-Ordner) konfiguriert wurde."""
    global _enabled, _bot_name, _url, _token, _dir, _usercache
    global _server_name, _version, _limit, _timeout, _avatars
    global _exclude_exact, _exclude_substr, _exclude_heuristic

    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("MINECRAFT_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Minecraft-Feature aus (MINECRAFT_ENABLED=0).")
        return False

    _url = os.getenv("MC_STATS_URL", "").strip()
    _token = os.getenv("MC_STATS_TOKEN", "").strip()
    _dir = os.getenv("MC_STATS_DIR", "").strip()
    _usercache = os.getenv("MC_USERCACHE", "").strip()
    _server_name = os.getenv("MC_SERVER_NAME", "Minecraft").strip() or "Minecraft"
    _version = os.getenv("MC_VERSION", "").strip()
    try:
        _limit = max(3, min(10, int(os.getenv("MC_LB_LIMIT", "5"))))
    except ValueError:
        _limit = 5
    try:
        _timeout = float(os.getenv("MC_HTTP_TIMEOUT", "8"))
    except ValueError:
        _timeout = 8.0
    _avatars = os.getenv("MC_AVATARS", "1").strip().lower() not in ("0", "false", "no", "off")
    # Ausschluss-Liste (Bots/Farmen): Defaults + per .env erweiterbar.
    extra = {n.lower() for n in os.getenv("MC_EXCLUDE", "").replace(",", " ").split()}
    _exclude_exact = set(_BOT_EXACT_DEFAULT) | extra
    pats = [p.lower() for p in os.getenv("MC_EXCLUDE_PATTERNS", "").replace(",", " ").split()]
    _exclude_substr = tuple(_BOT_SUBSTR_DEFAULT) + tuple(pats)
    _exclude_heuristic = os.getenv("MC_EXCLUDE_BOTS", "1").strip().lower() not in (
        "0", "false", "no", "off")

    if not _url and not _dir:
        log.info("Minecraft-Feature aus: weder MC_STATS_URL noch MC_STATS_DIR gesetzt.")
        return False
    # Frueh warnen statt still leer liefern (hilft beim Einrichten):
    if _dir and not os.path.isdir(_dir):
        log.warning("MC_STATS_DIR existiert (noch) nicht: %s", _dir)
    if _url and not _token:
        log.warning("MC_STATS_URL ohne MC_STATS_TOKEN gesetzt - die Bridge wird die "
                    "Anfrage mit 401 ablehnen.")

    _enabled = True
    quelle = f"HTTP {_url}" if _url else f"Ordner {_dir}"
    log.info("Minecraft-Feature aktiv (Quelle: %s, Top %d).", quelle, _limit)
    return True


def is_enabled() -> bool:
    return _enabled


# --- Block-Namen / Icon-Arten -------------------------------------------
# Deutsche Klartext-Namen fuer die haeufigsten Bloecke; Rest wird aus der ID
# huebsch gemacht ('minecraft:deepslate_diamond_ore' -> 'Deepslate Diamond Ore').
_BLOCK_DE = {
    "stone": "Stein", "cobblestone": "Bruchstein", "deepslate": "Tiefenschiefer",
    "cobbled_deepslate": "Geschl. Tiefenschiefer", "dirt": "Erde", "grass_block": "Gras",
    "sand": "Sand", "red_sand": "Roter Sand", "gravel": "Kies", "netherrack": "Netherrack",
    "andesite": "Andesit", "diorite": "Diorit", "granite": "Granit", "tuff": "Tuff",
    "calcite": "Calcit", "obsidian": "Obsidian", "clay": "Ton", "ice": "Eis",
    "coal_ore": "Kohleerz", "deepslate_coal_ore": "Kohleerz (Tief)",
    "iron_ore": "Eisenerz", "deepslate_iron_ore": "Eisenerz (Tief)",
    "copper_ore": "Kupfererz", "deepslate_copper_ore": "Kupfererz (Tief)",
    "gold_ore": "Golderz", "deepslate_gold_ore": "Golderz (Tief)",
    "nether_gold_ore": "Nether-Golderz",
    "redstone_ore": "Redstone-Erz", "deepslate_redstone_ore": "Redstone-Erz (Tief)",
    "lapis_ore": "Lapis-Erz", "deepslate_lapis_ore": "Lapis-Erz (Tief)",
    "diamond_ore": "Diamanterz", "deepslate_diamond_ore": "Diamanterz (Tief)",
    "emerald_ore": "Smaragderz", "deepslate_emerald_ore": "Smaragderz (Tief)",
    "ancient_debris": "Antiker Schutt", "nether_quartz_ore": "Nether-Quarz",
    "oak_log": "Eichenholz", "birch_log": "Birkenholz", "spruce_log": "Fichtenholz",
    "jungle_log": "Tropenholz", "acacia_log": "Akazienholz", "dark_oak_log": "Schwarzeiche",
    "mangrove_log": "Mangrovenholz", "cherry_log": "Kirschholz",
}

# Icon-Art (Palette in render.py) je Block. Fein granular fuer Erze, sonst grob.
_KIND_MAP = {
    "stone": "stone", "cobblestone": "cobblestone", "deepslate": "deepslate",
    "cobbled_deepslate": "deepslate", "dirt": "dirt", "grass_block": "grass_block",
    "sand": "sand", "red_sand": "sand", "gravel": "gravel", "netherrack": "netherrack",
    "andesite": "stone", "diorite": "stone", "granite": "stone", "tuff": "deepslate",
    "calcite": "stone", "obsidian": "obsidian", "clay": "dirt", "ice": "ice",
    "coal_ore": "coal_ore", "deepslate_coal_ore": "coal_ore",
    "iron_ore": "iron_ore", "deepslate_iron_ore": "iron_ore",
    "copper_ore": "copper_ore", "deepslate_copper_ore": "copper_ore",
    "gold_ore": "gold_ore", "deepslate_gold_ore": "gold_ore", "nether_gold_ore": "gold_ore",
    "redstone_ore": "redstone_ore", "deepslate_redstone_ore": "redstone_ore",
    "lapis_ore": "lapis_ore", "deepslate_lapis_ore": "lapis_ore",
    "diamond_ore": "diamond_ore", "deepslate_diamond_ore": "diamond_ore",
    "emerald_ore": "emerald_ore", "deepslate_emerald_ore": "emerald_ore",
    "ancient_debris": "ancient_debris", "nether_quartz_ore": "quartz",
    "oak_log": "log", "birch_log": "log", "spruce_log": "log", "jungle_log": "log",
    "acacia_log": "log", "dark_oak_log": "log", "mangrove_log": "log", "cherry_log": "log",
}


def _short_id(block_id: str) -> str:
    """'minecraft:diamond_ore' -> 'diamond_ore'."""
    return block_id.split(":", 1)[-1] if block_id else block_id


def _pretty_block(block_id: str) -> str:
    sid = _short_id(block_id)
    if sid in _BLOCK_DE:
        return _BLOCK_DE[sid]
    return sid.replace("_", " ").title()


def _block_kind(block_id: str) -> str:
    return _KIND_MAP.get(_short_id(block_id), "generic")


# --- Item-Namen / -Icons (Crafting / Benutzt) ----------------------------
_ITEM_DE = {
    "oak_planks": "Eichenbretter", "stick": "Stock", "torch": "Fackel",
    "crafting_table": "Werkbank", "furnace": "Ofen", "chest": "Truhe",
    "iron_ingot": "Eisenbarren", "gold_ingot": "Goldbarren",
    "copper_ingot": "Kupferbarren", "netherite_ingot": "Netheritbarren",
    "diamond": "Diamant", "emerald": "Smaragd", "bread": "Brot",
    "iron_pickaxe": "Eisen-Spitzhacke", "diamond_pickaxe": "Diamant-Spitzhacke",
    "netherite_pickaxe": "Netherit-Spitzhacke", "iron_sword": "Eisenschwert",
    "diamond_sword": "Diamantschwert", "bucket": "Eimer", "water_bucket": "Wassereimer",
    "arrow": "Pfeil", "bow": "Bogen", "wheat": "Weizen", "coal": "Kohle",
    "redstone": "Redstone", "shield": "Schild", "ladder": "Leiter",
}


def _pretty_item(item_id: str) -> str:
    sid = _short_id(item_id)
    if sid in _ITEM_DE:
        return _ITEM_DE[sid]
    if sid in _BLOCK_DE:
        return _BLOCK_DE[sid]
    return sid.replace("_", " ").title()


def _item_kind(item_id: str) -> str:
    """Icon-Art fuer ein Item: bekannter Block -> Block-Icon, sonst Item-Familie."""
    sid = _short_id(item_id)
    bk = _KIND_MAP.get(sid)
    if bk:
        return bk
    if sid.endswith("_planks") or sid.endswith("_wood") or sid.endswith("_log"):
        return "item:plank"
    if sid.endswith("_ingot") or sid in ("iron_nugget", "gold_nugget"):
        return "item:ingot"
    if sid in ("diamond", "emerald", "amethyst_shard", "quartz", "lapis_lazuli"):
        return "item:gem"
    if sid.endswith(("_pickaxe", "_axe", "_shovel", "_hoe")):
        return "item:tool"
    if sid.endswith("_sword"):
        return "item:sword"
    if sid in ("bread", "apple", "golden_apple", "carrot", "potato", "beef",
               "porkchop", "cooked_beef", "melon_slice", "cookie", "cooked_porkchop"):
        return "item:food"
    if sid == "stick":
        return "item:stick"
    if sid == "torch" or sid.endswith("_torch"):
        return "item:torch"
    return "item:generic"


# --- Mob-Namen / -Icons (getoetete Mobs) ---------------------------------
_MOB_DE = {
    "zombie": "Zombie", "skeleton": "Skelett", "creeper": "Creeper", "spider": "Spinne",
    "cave_spider": "Hoehlenspinne", "enderman": "Enderman", "witch": "Hexe",
    "slime": "Schleim", "blaze": "Lohe", "ghast": "Ghast", "piglin": "Piglin",
    "zombified_piglin": "Zombie-Piglin", "hoglin": "Hoglin",
    "wither_skeleton": "Wither-Skelett", "drowned": "Ertrunkener",
    "husk": "Wuestenzombie", "stray": "Eiswanderer", "phantom": "Phantom",
    "pillager": "Pluenderer", "vindicator": "Diener", "silverfish": "Silberfischchen",
    "magma_cube": "Magmawuerfel", "guardian": "Waechter", "cow": "Kuh",
    "pig": "Schwein", "chicken": "Huhn", "sheep": "Schaf", "villager": "Dorfbewohner",
}


def _pretty_mob(mob_id: str) -> str:
    return _MOB_DE.get(_short_id(mob_id), _short_id(mob_id).replace("_", " ").title())


_MOB_FACES = {"zombie", "skeleton", "creeper", "spider", "cave_spider", "enderman",
              "witch", "slime", "blaze", "piglin", "zombified_piglin"}


def _mob_kind(mob_id: str) -> str:
    sid = _short_id(mob_id)
    if sid in _MOB_FACES:
        return f"mob:{sid}"
    if "spider" in sid:
        return "mob:spider"
    if "skeleton" in sid:
        return "mob:skeleton"
    if "zombie" in sid or sid in ("drowned", "husk"):
        return "mob:zombie"
    if "piglin" in sid:
        return "mob:piglin"
    return "mob:generic"


# --- Roh-Stats holen -----------------------------------------------------
async def _fetch_players() -> list[dict]:
    """Liefert eine Liste {name, uuid, stats} - aus HTTP-Bridge oder lokalem
    Ordner. Wirft bei echten Verbindungsfehlern (wird oben abgefangen)."""
    if _url:
        return await _fetch_http()
    # Datei-IO (glob/open/json) nicht im Event-Loop blockieren - bei vielen
    # Stat-Dateien wuerde das sonst alle Discord-Events verzoegern.
    return await asyncio.get_event_loop().run_in_executor(None, _read_dir)


async def _fetch_http() -> list[dict]:
    import aiohttp  # lazy: Bot soll auch ohne aiohttp grundsaetzlich starten
    headers = {"X-Auth-Token": _token} if _token else {}
    params = {"token": _token} if _token else {}
    # Eigenes Connect-Timeout, damit ein haengender Verbindungsaufbau nicht das
    # ganze Zeitbudget frisst (wichtig fuer die Retries bei flatterndem Netz).
    timeout = aiohttp.ClientTimeout(total=_timeout, sock_connect=min(4.0, _timeout))
    last_exc: "Exception | None" = None
    for attempt in range(1, _HTTP_RETRIES + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(_url, headers=headers, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            if isinstance(data, dict):
                global _server_name, _version
                _server_name = data.get("server") or _server_name
                _version = data.get("mc_version") or _version
                return data.get("players") or []
            return data or []
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, OSError) as exc:
            # Nur Verbindungs-Wackler erneut versuchen - HTTP-Status-Fehler (z. B.
            # 401 falscher Token) fliegen sofort raus (raise_for_status -> oben).
            last_exc = exc
            if attempt < _HTTP_RETRIES:
                log.info("MC-Stats Versuch %d/%d fehlgeschlagen (%s) - neuer Versuch "
                         "in %.1fs", attempt, _HTTP_RETRIES, type(exc).__name__,
                         _HTTP_RETRY_DELAY)
                await asyncio.sleep(_HTTP_RETRY_DELAY)
    raise last_exc


def _read_dir() -> list[dict]:
    """Liest <stats-Ordner>/*.json + usercache.json direkt vom Dateisystem."""
    names = _load_usercache()
    players: list[dict] = []
    for path in glob.glob(os.path.join(_dir, "*.json")):
        uuid = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            continue
        stats = blob.get("stats") if isinstance(blob, dict) else None
        if not stats:
            continue
        players.append({"name": names.get(uuid.replace("-", "").lower()),
                        "uuid": uuid, "stats": stats})
    return players


def _load_usercache() -> dict[str, str]:
    """uuid(ohne Striche) -> Name. Sucht usercache.json (explizit oder neben dem
    stats-Ordner). Fehlt sie, bleibt die Map leer (Fallback: Kurz-UUID)."""
    candidates = []
    if _usercache:
        candidates.append(_usercache)
    if _dir:
        candidates.append(os.path.join(_dir, os.pardir, os.pardir, "usercache.json"))
        candidates.append(os.path.join(_dir, os.pardir, "usercache.json"))
    out: dict[str, str] = {}
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                arr = json.load(fh)
        except (OSError, ValueError):
            continue
        for e in arr if isinstance(arr, list) else []:
            uid = str(e.get("uuid", "")).replace("-", "").lower()
            if uid and e.get("name"):
                out[uid] = e["name"]
        if out:
            break
    return out


# --- Aggregation ---------------------------------------------------------
def _display_name(p: dict) -> str:
    name = (p.get("name") or "").strip()
    if name:
        return name[:18]  # MC-Namen sind <=16; kappt nur kaputte usercache-Eintraege
    uid = str(p.get("uuid", "")).replace("-", "")
    return f"Spieler {uid[:8]}" if uid else "Unbekannt"


def _is_bot(name: str) -> bool:
    """True, wenn der Spielername ausgeschlossen werden soll: fester Listen-
    Treffer ODER (Heuristik) endet auf farm/bot/afk bzw. enthaelt _farm/_bot/afkbot.
    So fliegen genannte Bots raus UND kuenftige '<x>farm'/'afk...'-Bots automatisch."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in _exclude_exact:
        return True
    if not _exclude_heuristic:
        return False
    if n.endswith(_exclude_suffix):
        return True
    return any(s in n for s in _exclude_substr)


def _fmt(n) -> str:
    """12345 -> '12.345' (deutsche Tausenderpunkte)."""
    return f"{int(n):,}".replace(",", ".")


# --- Kategorien (Tab-Buttons, wie das Ingame-Statistik-Menue) -------------
# Jede Kategorie ranked SPIELER (zum Vergleichen). Tupel:
#   (key, Button-Emoji, Label, stat_id, kind_fn, name_fn, Wert-Wort)
# 'playtime' ist ein Spezialfall (Wert = Stunden, Spezialitaet = Top-Block).
_CATS = [
    ("mined",    "⛏️", "Abbau",    "minecraft:mined",   _block_kind, _pretty_block, "Bloecke"),
    ("playtime", "⏱️", "Aktiv",    None,                _block_kind, _pretty_block, "Stunden"),
    ("crafted",  "🔨", "Crafting", "minecraft:crafted", _item_kind,  _pretty_item,  "gecraftet"),
    ("used",     "🗡️", "Benutzt",  "minecraft:used",    _item_kind,  _pretty_item,  "benutzt"),
    ("killed",   "👹", "Mobs",     "minecraft:killed",  _mob_kind,   _pretty_mob,   "Kills"),
]
_CAT_LABEL = {key: label for key, _e, label, *_rest in _CATS}
_DEFAULT_CAT = "mined"


def _build_category(players, key, stat_id, kind_fn, name_fn, value_label, limit):
    """Rangliste der SPIELER fuer eine Kategorie: je Spieler Gesamtzahl + seine
    Spezialitaet (haeufigstes Item/Block/Mob in dieser Kategorie)."""
    rows = []
    for p in players:
        stats = p.get("stats") or {}
        if key == "playtime":
            ticks = int((stats.get("minecraft:custom") or {}).get("minecraft:play_time", 0))
            count = ticks / 20 / 3600
            spec_src = stats.get("minecraft:mined") or {}
        else:
            spec_src = stats.get(stat_id) or {}
            count = sum(int(v) for v in spec_src.values() if int(v) > 0)
        if count <= 0:
            continue
        top = None
        if spec_src:
            tid, tc = max(spec_src.items(), key=lambda kv: int(kv[1]))
            if int(tc) > 0:
                top = {"name": name_fn(tid), "kind": kind_fn(tid), "count": int(tc)}
        rows.append({
            "name": _display_name(p), "uuid": str(p.get("uuid", "")), "count": count,
            "value": f"{count:.0f}" if key == "playtime" else _fmt(int(count)),
            "label": value_label, "top": top,
        })
    rows.sort(key=lambda r: (-r["count"], r["name"].lower()))
    rows = rows[:limit]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def _aggregate(players: list[dict], limit: int) -> dict:
    """Baut die komplette Statistik: je Kategorie eine SPIELER-Rangliste (Top-N)
    plus die Server-Summen fuer das Stats-Band."""
    # Bots/Farmen komplett raushalten (aus Rangliste, Summen und Spielerzahl).
    before = len(players)
    players = [p for p in players if not _is_bot(_display_name(p))]
    if before != len(players):
        log.info("MC-Stats: %d Bot(s)/Farm(en) ausgeschlossen.", before - len(players))
    cats = {}
    for key, _emoji, _label, stat_id, kind_fn, name_fn, word in _CATS:
        cats[key] = {"label": word,
                     "rows": _build_category(players, key, stat_id, kind_fn,
                                             name_fn, word, limit)}
    default_cat = next((k for k, *_ in _CATS if cats[k]["rows"]), _DEFAULT_CAT)

    tot_mined = tot_deaths = tot_kills = tot_cm = tot_play = 0
    for p in players:
        stats = p.get("stats") or {}
        custom = stats.get("minecraft:custom") or {}
        mined = stats.get("minecraft:mined") or {}
        tot_mined += sum(int(v) for v in mined.values())
        tot_play += int(custom.get("minecraft:play_time", 0))
        tot_deaths += int(custom.get("minecraft:deaths", 0))
        tot_kills += int(custom.get("minecraft:mob_kills", 0))
        tot_cm += sum(int(v) for k, v in custom.items() if k.endswith("_one_cm"))

    return {
        "server": _server_name,
        "version": _version,
        "player_count": len(players),
        "default_cat": default_cat,
        "cats": cats,
        "totals": {
            "mined": tot_mined,
            "play_h": tot_play / 20 / 3600,
            "deaths": tot_deaths,
            "kills": tot_kills,
            "km": tot_cm / 100000,
        },
    }


def _has_any_rows(data: dict) -> bool:
    return any((data.get("cats") or {}).get(k, {}).get("rows") for k, *_ in _CATS)


# --- Spieler-Koepfe (echte Minecraft-Skins via Minotar, gecacht) ---------
_HEAD_CACHE: "dict[str, bytes | None]" = {}
_HEAD_URL = "https://minotar.net/helm/{uuid}/{size}.png"
_HEAD_SIZE = 96


async def _fetch_head(session, uuid: str):
    key = uuid.replace("-", "").lower()
    if key in _HEAD_CACHE:
        return _HEAD_CACHE[key]
    blob = None
    try:
        async with session.get(_HEAD_URL.format(uuid=key, size=_HEAD_SIZE)) as resp:
            if resp.status == 200:
                blob = await resp.read()
    except Exception:  # noqa: BLE001 - Avatar ist nice-to-have, nie fatal
        blob = None
    _HEAD_CACHE[key] = blob
    return blob


async def _attach_heads(data: dict) -> None:
    """Holt fuer alle Spieler den echten Skin-Kopf (gecacht) und haengt die PNG-
    Bytes an die Reihen (render zeichnet sie ohne Netz)."""
    uuids, seen = [], set()
    for cat in (data.get("cats") or {}).values():
        for r in cat.get("rows") or []:
            u = r.get("uuid")
            if u and u not in seen:
                seen.add(u)
                uuids.append(u)
    if not uuids:
        return
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=_timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await asyncio.gather(*(_fetch_head(session, u) for u in uuids),
                                 return_exceptions=True)
    except Exception:  # noqa: BLE001
        pass
    for cat in (data.get("cats") or {}).values():
        for r in cat.get("rows") or []:
            r["head"] = _HEAD_CACHE.get(str(r.get("uuid", "")).replace("-", "").lower())


# --- Darstellung ---------------------------------------------------------
def _render_file(data: dict, category: str):
    """Optionales MC-Style-Statistikbild (render.mc_stats). Faellt sauber aus."""
    fn = getattr(render, "mc_stats", None)
    if not callable(fn):
        return None
    try:
        buf = fn(data, category)
    except Exception:  # noqa: BLE001 - Bild ist nice-to-have, nie fatal
        log.exception("MC-Statistik-Bild fehlgeschlagen - nutze Text-Embed")
        return None
    if buf is None:
        return None
    return discord.File(buf, filename="mcstats.png")


def _embed(data: dict, category: str, *, with_image: bool, with_fields: bool) -> discord.Embed:
    t = data["totals"]
    ver = f" · {data['version']}" if data.get("version") else ""
    emb = discord.Embed(
        title=f"⛏️ Minecraft Statistik — {_CAT_LABEL.get(category, '')}",
        description=(f"**{data['server']}**{ver} · {data['player_count']} Spieler\n"
                     f"Vergleicht euch – Kategorie unten mit den **Buttons** wechseln. 👇"),
        color=0x5E9B33,  # Gras-Gruen
    )
    if with_image:
        emb.set_image(url="attachment://mcstats.png")
    if with_fields:
        cat = (data.get("cats") or {}).get(category, {})
        word = cat.get("label", "")
        for r in cat.get("rows") or []:
            rk = r.get("rank") or 0
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rk, f"#{rk}")
            top = r.get("top")
            extra = f" · {top['name']} ×{_fmt(top['count'])}" if top else ""
            emb.add_field(name=f"{medal} {r.get('name', '?')}",
                          value=f"**{r.get('value', '0')}** {word}{extra}", inline=False)
    emb.set_footer(
        text=(f"Gesamt: {_fmt(t['mined'])} Bloecke · {t['play_h']:.0f} h gespielt · "
              f"{_fmt(t['kills'])} Mobs · {_fmt(t['deaths'])} Tode · {t['km']:.1f} km"))
    return emb


def _protect(msg) -> None:
    if msg is None:
        return
    try:
        import bot
        bot.protect_message(msg)
    except Exception:  # noqa: BLE001
        pass


def _release(msg) -> None:
    if msg is None:
        return
    try:
        import bot
        bot.release_message(msg)
    except Exception:  # noqa: BLE001
        pass


# --- Interaktive Statistik-Ansicht (Tab-Buttons) -------------------------
class MCStatsView(discord.ui.View):
    """Wie das Ingame-Statistik-Menue: Buttons wechseln die Kategorie; jede
    Kategorie ist eine Spieler-Rangliste mit echtem Skin-Kopf."""

    def __init__(self, data: dict, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.data = data
        self.message = None
        self.category = data.get("default_cat", _DEFAULT_CAT)
        for key, emoji, label, *_rest in _CATS:
            rows = (data.get("cats") or {}).get(key, {}).get("rows") or []
            btn = discord.ui.Button(
                label=label, emoji=emoji, disabled=not rows, custom_id=f"mc:{key}",
                style=discord.ButtonStyle.primary if key == self.category
                else discord.ButtonStyle.secondary)
            btn.callback = self._cb(key)
            self.add_item(btn)

    def _cb(self, key: str):
        async def callback(interaction: discord.Interaction):
            await self._switch(interaction, key)
        return callback

    async def _switch(self, interaction: discord.Interaction, key: str) -> None:
        for child in self.children:
            cid = getattr(child, "custom_id", "") or ""
            if cid.startswith("mc:"):
                child.style = (discord.ButtonStyle.primary
                               if cid == f"mc:{key}" else discord.ButtonStyle.secondary)
        file = _render_file(self.data, key)
        emb = _embed(self.data, key, with_image=file is not None, with_fields=file is None)
        try:
            await interaction.response.edit_message(
                embed=emb, attachments=[file] if file is not None else [], view=self)
            self.category = key            # erst nach erfolgreichem Edit
        except discord.HTTPException:
            log.exception("MC-Statistik-Wechsel fehlgeschlagen")

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass
        finally:
            _release(self.message)        # IMMER freigeben


# --- Befehl --------------------------------------------------------------
_LB_WORDS = {"mcleaderboard", "mclb", "mcstats", "mctop", "mcboard"}
_MC_WORDS = {"mc", "minecraft"}
_SUB_WORDS = {"leaderboard", "lb", "stats", "stat", "top", "board", "bestenliste"}


def _is_lb_command(parts: list[str]) -> bool:
    if not parts:
        return False
    first = parts[0]
    if first in _LB_WORDS:
        return True
    if first in _MC_WORDS:
        return len(parts) > 1 and parts[1] in _SUB_WORDS
    return False


async def handle(message: discord.Message) -> object:
    """Reagiert auf 'flo mcleaderboard' / 'flo mc stats' / 'flo minecraft top'."""
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.lower().split()
    if not _is_lb_command(parts):
        return None

    try:
        players = await _fetch_players()
    except Exception as exc:  # noqa: BLE001 - Verbindungsfehler nie als Crash
        log.warning("MC-Stats nicht erreichbar (%s): %s", type(exc).__name__, exc)
        await _reply_error(message)
        return HANDLED

    data = _aggregate(players, _limit)
    if not players or not _has_any_rows(data):
        emb = discord.Embed(
            title="⛏️ Minecraft Statistik",
            description="Es gibt noch keine Statistiken – spielt erst mal ein bisschen! 🙂",
            color=0x5E9B33)
        await _safe_reply(message, embed=emb)
        return HANDLED

    if _avatars:
        try:
            await _attach_heads(data)
        except Exception as exc:  # noqa: BLE001 - Avatare optional
            log.warning("MC-Avatare nicht ladbar (%s)", type(exc).__name__)

    cat = data.get("default_cat", _DEFAULT_CAT)
    file = _render_file(data, cat)
    emb = _embed(data, cat, with_image=file is not None, with_fields=file is None)
    view = MCStatsView(data)
    kwargs = {"embed": emb, "view": view, "mention_author": False}
    if file is not None:
        kwargs["file"] = file
    msg = await _safe_reply(message, **kwargs)
    if msg is not None:
        view.message = msg
        _protect(msg)
    return HANDLED


async def _reply_error(message: discord.Message) -> None:
    emb = discord.Embed(
        title="⛏️ Minecraft Statistik",
        description=("Komm gerade nicht an den Minecraft-Server ran. Laeuft die "
                     "**Flo MC Bridge** und stimmt `MC_STATS_URL`/`MC_STATS_TOKEN`?"),
        color=0xE74C3C)
    await _safe_reply(message, embed=emb)


async def _safe_reply(message: discord.Message, **kwargs):
    try:
        return await message.reply(**kwargs)
    except discord.HTTPException:
        log.exception("MC-Statistik konnte nicht gesendet werden")
        return None
