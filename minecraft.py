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


def setup() -> bool:
    """Aktiviert das Feature, wenn es nicht abgeschaltet ist UND eine Quelle
    (HTTP-URL oder lokaler stats-Ordner) konfiguriert wurde."""
    global _enabled, _bot_name, _url, _token, _dir, _usercache
    global _server_name, _version, _limit, _timeout

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
    timeout = aiohttp.ClientTimeout(total=_timeout)
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


def _aggregate(players: list[dict], limit: int) -> dict:
    """Baut aus den Roh-Stats die fertige Leaderboard-Struktur fuer Embed+Render."""
    miners: list[dict] = []
    tot_mined = tot_deaths = tot_kills = tot_cm = 0
    tot_play_ticks = 0
    block_global: dict[str, int] = {}      # block_id -> Gesamtmenge
    block_top_by: dict[str, tuple[int, str]] = {}  # block_id -> (max, spielername)

    for p in players:
        stats = p.get("stats") or {}
        mined = stats.get("minecraft:mined") or {}
        custom = stats.get("minecraft:custom") or {}
        total = sum(int(v) for v in mined.values())
        name = _display_name(p)

        # Top-Bloecke dieses Spielers
        blocks = sorted(mined.items(), key=lambda kv: -int(kv[1]))
        top_blocks = [{"name": _pretty_block(bid), "kind": _block_kind(bid),
                       "count": int(cnt)} for bid, cnt in blocks[:4]]

        play_ticks = int(custom.get("minecraft:play_time", 0))
        deaths = int(custom.get("minecraft:deaths", 0))
        kills = int(custom.get("minecraft:mob_kills", 0))
        cm = sum(int(v) for k, v in custom.items() if k.endswith("_one_cm"))

        miners.append({
            "name": name, "total_mined": total, "blocks": top_blocks,
            "play_h": play_ticks / 20 / 3600, "deaths": deaths, "kills": kills,
        })
        tot_mined += total
        tot_play_ticks += play_ticks
        tot_deaths += deaths
        tot_kills += kills
        tot_cm += cm
        for bid, cnt in mined.items():
            c = int(cnt)
            block_global[bid] = block_global.get(bid, 0) + c
            if c > block_top_by.get(bid, (0, ""))[0]:
                block_top_by[bid] = (c, name)

    miners.sort(key=lambda m: (-m["total_mined"], m["name"].lower()))
    for i, m in enumerate(miners[:limit], 1):
        m["rank"] = i

    spotlight = None
    if block_global:
        bid, gcount = max(block_global.items(), key=lambda kv: kv[1])
        if gcount > 0:
            spotlight = {"name": _pretty_block(bid), "kind": _block_kind(bid),
                         "count": gcount, "by": block_top_by.get(bid, (0, "?"))[1]}

    return {
        "server": _server_name,
        "version": _version,
        "player_count": len(players),
        "miners": miners[:limit],
        "spotlight": spotlight,
        "totals": {
            "mined": tot_mined,
            "play_h": tot_play_ticks / 20 / 3600,
            "deaths": tot_deaths,
            "kills": tot_kills,
            "km": tot_cm / 100000,
        },
    }


# --- Darstellung ---------------------------------------------------------
def _fmt(n: int) -> str:
    """12345 -> '12.345' (deutsche Tausenderpunkte)."""
    return f"{int(n):,}".replace(",", ".")


def _render_file(data: dict):
    """Optionales MC-Style-Banner (render.mc_leaderboard). Faellt sauber aus."""
    fn = getattr(render, "mc_leaderboard", None)
    if not callable(fn):
        return None
    try:
        buf = fn(data)
    except Exception:  # noqa: BLE001 - Bild ist nice-to-have, nie fatal
        log.exception("MC-Leaderboard-Bild fehlgeschlagen - nutze Text-Embed")
        return None
    if buf is None:
        return None
    return discord.File(buf, filename="mcleaderboard.png")


def _embed(data: dict, *, with_image: bool, with_fields: bool) -> discord.Embed:
    t = data["totals"]
    ver = f" · {data['version']}" if data.get("version") else ""
    emb = discord.Embed(
        title="⛏️ Minecraft Leaderboard",
        description=(f"**{data['server']}**{ver} · {data['player_count']} Spieler\n"
                     f"Wer hat am meisten abgebaut? 👇"),
        color=0x5E9B33,  # Gras-Gruen
    )
    if with_image:
        emb.set_image(url="attachment://mcleaderboard.png")
    if with_fields:
        for m in data["miners"]:
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(m["rank"], f"#{m['rank']}")
            top = m["blocks"][0] if m["blocks"] else None
            extra = f" · meist {top['name']} ({_fmt(top['count'])})" if top else ""
            emb.add_field(
                name=f"{medal} {m['name']}",
                value=f"⛏️ **{_fmt(m['total_mined'])}** Bloecke{extra}",
                inline=False,
            )
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

    if not players:
        emb = discord.Embed(
            title="⛏️ Minecraft Leaderboard",
            description="Es gibt noch keine Statistiken – spielt erst mal ein bisschen! 🙂",
            color=0x5E9B33)
        await _safe_reply(message, embed=emb)
        return HANDLED

    data = _aggregate(players, _limit)
    file = _render_file(data)
    emb = _embed(data, with_image=file is not None, with_fields=file is None)
    kwargs = {"embed": emb, "mention_author": False}
    if file is not None:
        kwargs["file"] = file
    msg = await _safe_reply(message, **kwargs)
    _protect(msg)
    return HANDLED


async def _reply_error(message: discord.Message) -> None:
    emb = discord.Embed(
        title="⛏️ Minecraft Leaderboard",
        description=("Komm gerade nicht an den Minecraft-Server ran. Laeuft die "
                     "**Flo MC Bridge** und stimmt `MC_STATS_URL`/`MC_STATS_TOKEN`?"),
        color=0xE74C3C)
    await _safe_reply(message, embed=emb)


async def _safe_reply(message: discord.Message, **kwargs):
    try:
        return await message.reply(**kwargs)
    except discord.HTTPException:
        log.exception("MC-Leaderboard konnte nicht gesendet werden")
        return None
