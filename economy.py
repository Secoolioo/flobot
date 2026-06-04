"""Level- & SigmaCoins-System fuer Flo (Pack 3).

Was es kann:
- XP fuers Schreiben (mit Cooldown gegen Spam) und fuer Zeit im Sprachkanal.
- Automatische Level-Up-Ansage im Chat, dazu SigmaCoins als Belohnung.
- Befehle: level/rank, top/bestenliste, coins/konto, daily, pay, shop, kaufen.
- Speichert alles in data/economy.json (siehe store.py, ohne Extra-Abhaengigkeit).

Dieses Modul ist die EINZIGE Quelle fuer den Coin-Kontostand. Andere Module
(z. B. games.py) vergeben Coins ueber economy.add_coins(), damit es nur einen
Topf gibt.
"""
from __future__ import annotations

import io
import logging
import os
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import ai
import leaderboard_img
from store import JsonStore

log = logging.getLogger("dcbot.economy")

# --- Konfiguration -------------------------------------------------------
_enabled: bool = False
_bot_name: str = "Flo"
_tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

# XP-Vergabe
XP_PER_MSG = (15, 25)        # zufaellig in diesem Bereich pro Nachricht
COINS_PER_MSG = (1, 3)       # nebenbei ein paar Coins
MSG_COOLDOWN = 45            # Sekunden Sperre pro Nutzer (Anti-Spam)
XP_PER_VOICE_TICK = 10       # XP pro Voice-Runde (Bot-Loop ruft tick_voice auf)
VOICE_TICK_SECONDS = 60      # nur Info fuer die Ansage; Takt steuert bot.py

# Muenzeinheit
COIN = "SigmaCoins"

# Wohin Level-Up-Ansagen gehen. Standard: der Commands-Channel. 0 = im selben
# Kanal ansagen, in dem die Nachricht kam. Per .env (LEVELUP_CHANNEL_ID) aenderbar.
LEVELUP_CHANNEL_ID = int(os.getenv("LEVELUP_CHANNEL_ID", "1512045750362837013") or "0")
# Titel der Level-Up-Embeds. bot.py nimmt solche Nachrichten vom Auto-Loeschen
# aus, damit Erfolge im Commands-Channel sichtbar bleiben.
LEVELUP_EMBED_TITLE = "🎉 Level Up!"

# Shop: Titel sind reine Kosmetik (erscheinen auf der Level-Karte).
SHOP: dict[str, dict] = {
    "sigma":    {"preis": 1000, "titel": "🗿 Sigma",      "info": "Der Klassiker."},
    "gigachad": {"preis": 2500, "titel": "💪 Gigachad",   "info": "Maximale Aura."},
    "rizzler":  {"preis": 1500, "titel": "😏 Rizzler",    "info": "Unwiderstehlich."},
    "goblin":   {"preis":  500, "titel": "👺 Goblin",     "info": "Chaos-Energie."},
    "npc":      {"preis":  100, "titel": "🤖 NPC",        "info": "Lebt im Hintergrund."},
    "king":     {"preis": 5000, "titel": "👑 Server-King", "info": "Ganz oben."},
}

# Cooldowns nur im Speicher (gehen bei Neustart verloren - egal, sind kurz).
_last_msg_xp: dict[str, float] = {}

_store: JsonStore | None = None


def setup() -> bool:
    """Aktiviert das Feature. Laeuft immer (keine externen Voraussetzungen)."""
    global _enabled, _bot_name, _store
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("ECONOMY_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Level/Coins-Feature aus (ECONOMY_ENABLED=0).")
        return False
    _store = JsonStore("economy.json", default={"users": {}})
    _enabled = True
    log.info("Level/Coins-Feature aktiv (%d Profile geladen).", len(_users()))
    return True


def is_enabled() -> bool:
    return _enabled


# --- Datenzugriff --------------------------------------------------------
def _users() -> dict:
    assert _store is not None
    return _store.data.setdefault("users", {})


def _profile(user_id: int) -> dict:
    """Holt (oder erstellt) das Profil eines Nutzers."""
    users = _users()
    key = str(user_id)
    prof = users.get(key)
    if prof is None:
        prof = {"xp": 0, "coins": 0, "last_daily": "", "streak": 0,
                "voice_secs": 0, "msgs": 0, "title": "", "owned": [], "name": ""}
        users[key] = prof
    prof.setdefault("owned", [])  # Altprofile nachruesten
    prof.setdefault("msgs", 0)    # Nachrichtenzaehler (fuer das Leaderboard-Bild)
    # Migration: wer schon einen Titel TRAEGT (Altprofil vor dem Inventar),
    # bekommt diesen Titel rueckwirkend ins Inventar.
    if prof.get("title") and not prof["owned"]:
        for k, it in SHOP.items():
            if it["titel"] == prof["title"]:
                prof["owned"].append(k)
                break
    return prof


async def _flush() -> None:
    if _store is not None:
        await _store.save()


async def flush() -> None:
    """Oeffentliches Speichern - andere Module (z. B. games) rufen das nach
    einer Coin-Aenderung auf, damit der Gewinn die Platte erreicht."""
    await _flush()


# --- Level-Mathematik ----------------------------------------------------
def _level_for_xp(xp: int) -> tuple[int, int, int]:
    """Rechnet Gesamt-XP in (Level, XP-im-Level, XP-bis-naechstes-Level) um.

    Stufe L -> L+1 kostet 100 + L*55 XP (wird mit jedem Level teurer).
    """
    level = 0
    needed = 0
    while True:
        step = 100 + level * 55
        if xp < needed + step:
            return level, xp - needed, step
        needed += step
        level += 1
        if level > 1000:  # Sicherheitsnetz
            return level, 0, step


def _level_only(xp: int) -> int:
    return _level_for_xp(xp)[0]


# --- XP vergeben ---------------------------------------------------------
async def add_xp(member: discord.abc.User, amount: int) -> int | None:
    """Gibt einem Mitglied XP. Rueckgabe: neues Level, falls ein Level-Up
    passiert ist, sonst None."""
    if not _enabled or amount <= 0:
        return None
    prof = _profile(member.id)
    prof["name"] = getattr(member, "display_name", "") or prof.get("name", "")
    before = _level_only(prof["xp"])
    prof["xp"] += amount
    after = _level_only(prof["xp"])
    if after > before:
        reward = after * 25
        prof["coins"] += reward
        return after
    return None


def add_coins(user_id: int, amount: int) -> int:
    """Aendert den Kontostand (auch negativ) und gibt den neuen Stand zurueck.
    Geht nie unter 0."""
    if not _enabled:
        return 0
    prof = _profile(user_id)
    prof["coins"] = max(0, prof["coins"] + amount)
    return prof["coins"]


def get_coins(user_id: int) -> int:
    return _profile(user_id)["coins"] if _enabled else 0


def get_title(user_id: int) -> str:
    """Aktuell getragener Titel (Label inkl. Emoji), z. B. '🤖 NPC', sonst ''.
    bot.py reicht das an die KI weiter, damit Flo den Nutzer damit anspricht."""
    if not _enabled:
        return ""
    return _profile(user_id).get("title", "") or ""


# --- Passiver Hook: XP pro Nachricht -------------------------------------
async def on_message(message: discord.Message) -> None:
    """Vergibt XP/Coins fuer eine Nachricht (mit Cooldown) und sagt Level-Ups an.
    Wird in bot.py fuer JEDE Nicht-Bot-Nachricht aufgerufen."""
    if not _enabled or message.guild is None or message.author.bot:
        return
    # Nachrichtenzaehler fuers Leaderboard: zaehlt JEDE Nachricht (ohne Cooldown).
    # Wird im Speicher hochgezaehlt und beim naechsten _flush() mitgespeichert.
    prof = _profile(message.author.id)
    prof["msgs"] = prof.get("msgs", 0) + 1
    prof["name"] = getattr(message.author, "display_name", "") or prof.get("name", "")

    key = str(message.author.id)
    now = time.monotonic()
    if now - _last_msg_xp.get(key, 0.0) < MSG_COOLDOWN:
        return
    _last_msg_xp[key] = now

    add_coins(message.author.id, random.randint(*COINS_PER_MSG))
    new_level = await add_xp(message.author, random.randint(*XP_PER_MSG))
    if new_level is not None:
        await _announce_levelup(message.guild, message.author, new_level, message.channel)
    await _flush()


def _levelup_target(guild, fallback):
    """Liefert den Ziel-Channel fuer Level-Up-Ansagen (Commands-Channel, sonst
    der Kanal, in dem die Aktion passierte)."""
    if guild is not None and LEVELUP_CHANNEL_ID:
        ch = guild.get_channel(LEVELUP_CHANNEL_ID)
        if ch is not None:
            perms = ch.permissions_for(guild.me)
            if perms.view_channel and perms.send_messages:
                return ch
    return fallback


async def _announce_levelup(guild, member, level: int, fallback=None) -> None:
    channel = _levelup_target(guild, fallback)
    if channel is None:
        return
    reward = level * 25
    spruch = random.choice([
        "GG!", "Lets gooo!", "Sheesh!", "Aufstieg!", "Weiter so!", "Sigma move.",
    ])
    emb = discord.Embed(
        title=LEVELUP_EMBED_TITLE,
        description=f"{member.mention} ist jetzt **Level {level}**! {spruch}",
        color=discord.Color.gold(),
    )
    emb.add_field(name="Belohnung", value=f"💰 +{reward} {COIN}", inline=True)
    try:
        emb.set_thumbnail(url=member.display_avatar.url)
    except Exception:  # noqa: BLE001 - Avatar ist nur Deko
        pass
    try:
        await channel.send(content=member.mention, embed=emb)
    except discord.HTTPException:
        pass


# --- Voice-XP (bot.py ruft das periodisch pro Guild auf) -----------------
async def tick_voice(guild: discord.Guild) -> None:
    """Gibt allen aktiven Mitgliedern in Sprachkanaelen XP. AFK/stumm/Bots
    bekommen nichts. bot.py ruft das im Takt VOICE_TICK_SECONDS auf."""
    if not _enabled:
        return
    changed = False
    for vc in guild.voice_channels:
        if guild.afk_channel and vc.id == guild.afk_channel.id:
            continue
        members = [m for m in vc.members if not m.bot]
        if len(members) < 2:
            continue  # allein im Kanal bringt nichts (Anti-Farm)
        for m in members:
            vs = m.voice
            if vs is None or vs.self_deaf or vs.deaf:
                continue  # wer taub ist, hoert eh nichts -> kein XP
            prof = _profile(m.id)
            prof["voice_secs"] = prof.get("voice_secs", 0) + VOICE_TICK_SECONDS
            new_level = await add_xp(m, XP_PER_VOICE_TICK)
            changed = True
            if new_level is not None:
                await _announce_levelup(guild, m, new_level, guild.system_channel)
    if changed:
        await _flush()


# --- Befehls-Erkennung ---------------------------------------------------
def _clean_lead(text: str) -> str:
    """Entfernt @-Mentions und den fuehrenden Botnamen/Alias ('Florian, level' ->
    'level'). Zentral in ai.strip_lead, damit alle Module gleich reagieren."""
    return ai.strip_lead(text)


def _bar(into: int, step: int, width: int = 12) -> str:
    filled = 0 if step <= 0 else max(0, min(width, round(into / step * width)))
    return "█" * filled + "░" * (width - filled)


def _rank_of(user_id: int) -> tuple[int, int]:
    """Platz (1-basiert) nach XP und Gesamtzahl der Profile."""
    ranking = sorted(_users().items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
    total = len(ranking)
    for i, (key, _prof) in enumerate(ranking, start=1):
        if key == str(user_id):
            return i, total
    return total, total


def _today() -> str:
    return datetime.now(_tz).strftime("%Y-%m-%d")


async def handle(message: discord.Message) -> "str | discord.Embed | discord.File | None":
    """Erkennt Level-/Coin-Befehle. Rueckgabe: Antworttext, Embed, Bild oder None."""
    if not _enabled or message.guild is None:
        return None
    cmd = _clean_lead(message.content or "")
    if not cmd:
        return None
    low = cmd.lower()
    parts = low.split()
    first = parts[0] if parts else ""

    # Zielnutzer (erste Mention), sonst der Autor selbst.
    target = message.mentions[0] if message.mentions else message.author

    if first in ("level", "lvl", "rank", "rang"):
        return _card(target)
    if first in ("coins", "konto", "kontostand", "münzen", "muenzen", "balance", "bal"):
        c = get_coins(target.id)
        wer = "Du hast" if target.id == message.author.id else f"{target.display_name} hat"
        return f"💰 {wer} **{c} {COIN}**."
    if first in ("top", "bestenliste", "rangliste", "leaderboard", "lb"):
        return _leaderboard()
    if first in ("daily", "täglich", "taeglich", "tagesbonus"):
        return await _daily(message.author)
    if first in ("pay", "zahl", "zahle", "überweis", "ueberweis", "überweise"):
        return await _pay(message)
    if first in ("shop", "laden", "store"):
        return _shop()
    if first in ("kaufen", "buy", "kauf"):
        return await _buy(message.author, low)
    if first in ("inventar", "inventory", "inv", "titel", "titles", "title"):
        # 'titel <name>' legt einen besessenen Titel an; sonst Inventar zeigen.
        if first in ("titel", "title", "titles") and len(parts) > 1:
            return await _equip(message.author, low)
        return _inventory(message.author)
    if first in ("equip", "anlegen", "trage", "tragen", "anziehen", "setze"):
        return await _equip(message.author, low)
    return None


def _card(member: discord.abc.User) -> discord.Embed:
    prof = _profile(member.id)
    level, into, step = _level_for_xp(prof["xp"])
    place, total = _rank_of(member.id)
    title = prof.get("title") or "—"
    emb = discord.Embed(title=f"📈 {member.display_name}", color=discord.Color.blurple())
    try:
        emb.set_thumbnail(url=member.display_avatar.url)
    except Exception:  # noqa: BLE001
        pass
    emb.add_field(name=f"Level {level}",
                  value=f"`{_bar(into, step)}`  {into}/{step} XP", inline=False)
    emb.add_field(name="Gesamt-XP", value=f"{prof['xp']}", inline=True)
    emb.add_field(name=COIN, value=f"💰 {prof['coins']}", inline=True)
    emb.add_field(name="Platz", value=f"#{place}/{total}", inline=True)
    emb.add_field(name="Titel", value=title, inline=True)
    emb.add_field(name="Streak", value=f"🔥 {prof.get('streak', 0)} Tag(e)", inline=True)
    return emb


def leaderboard_data(limit: int = 10) -> list[dict]:
    """Aufbereitete Bestenliste fuer das Leaderboard-Bild (sortiert nach XP)."""
    ranking = sorted(_users().values(), key=lambda p: p.get("xp", 0), reverse=True)
    out: list[dict] = []
    for prof in ranking[:limit]:
        out.append({
            "name": prof.get("name") or "Unbekannt",
            "level": _level_only(prof.get("xp", 0)),
            "xp": prof.get("xp", 0),
            "coins": prof.get("coins", 0),
            "voice_secs": prof.get("voice_secs", 0),
            "msgs": prof.get("msgs", 0),
            "title": prof.get("title") or "",
        })
    return out


def _leaderboard() -> "discord.Embed | discord.File":
    """Bestenliste als Grafana-artiges PNG (wenn Pillow da ist), sonst als Embed."""
    rows = leaderboard_data(10)
    if rows and leaderboard_img.is_available():
        try:
            stand = datetime.now(_tz).strftime("Stand: %d.%m.%Y %H:%M")
            png = leaderboard_img.render_png(rows, subtitle=stand)
            if png:
                return discord.File(io.BytesIO(png), filename="leaderboard.png")
        except Exception:  # noqa: BLE001 - Bild ist nice-to-have, niemals fatal
            log.exception("Leaderboard-Bild fehlgeschlagen - nutze Embed")
    return _leaderboard_embed()


def _leaderboard_embed() -> discord.Embed:
    ranking = sorted(_users().items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
    emb = discord.Embed(title="🏆 Bestenliste (XP)", color=discord.Color.gold())
    if not ranking:
        emb.description = "Noch keine Daten - schreibt was, dann sammelt ihr XP! 😄"
        return emb
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (_key, prof) in enumerate(ranking[:10]):
        marker = medals[i] if i < 3 else f"`{i + 1}.`"
        name = prof.get("name") or "Unbekannt"
        title = prof.get("title")
        suffix = f"  ·  {title}" if title else ""
        lines.append(
            f"{marker} **{name}** — Lvl {_level_only(prof['xp'])} ({prof['xp']} XP){suffix}"
        )
    emb.description = "\n".join(lines)
    return emb


def _inventory(member: discord.abc.User) -> discord.Embed:
    prof = _profile(member.id)
    owned = prof.get("owned", [])
    current = prof.get("title") or "—"
    emb = discord.Embed(
        title=f"🎒 Inventar von {member.display_name}",
        description=f"Getragener Titel: **{current}**",
        color=discord.Color.blurple(),
    )
    try:
        emb.set_thumbnail(url=member.display_avatar.url)
    except Exception:  # noqa: BLE001
        pass
    if owned:
        lines = []
        for key in owned:
            item = SHOP.get(key)
            label = item["titel"] if item else key
            worn = " ✅" if item and prof.get("title") == item["titel"] else ""
            lines.append(f"• `{key}` — {label}{worn}")
        emb.add_field(name="Deine Titel", value="\n".join(lines), inline=False)
        emb.set_footer(
            text=f"Anlegen: {_bot_name} titel <name>  ·  Ablegen: {_bot_name} titel ab"
        )
    else:
        emb.add_field(
            name="Noch leer",
            value=f"Kauf dir einen Titel im `{_bot_name} shop`!", inline=False,
        )
    return emb


async def _daily(member: discord.abc.User) -> str:
    prof = _profile(member.id)
    today = _today()
    if prof.get("last_daily") == today:
        return "🕒 Deinen Tagesbonus hast du heute schon abgeholt. Komm morgen wieder!"
    # Streak: gestern abgeholt -> +1, sonst zurueck auf 1.
    yesterday = (datetime.now(_tz).toordinal() - 1)
    last = prof.get("last_daily") or ""
    try:
        last_ord = datetime.strptime(last, "%Y-%m-%d").toordinal()
    except ValueError:
        last_ord = -999
    prof["streak"] = prof.get("streak", 0) + 1 if last_ord == yesterday else 1
    prof["last_daily"] = today
    bonus = min(prof["streak"], 7) * 20
    total = 100 + bonus
    prof["coins"] += total
    await _flush()
    return (f"🎁 Tagesbonus: **+{total} {COIN}**! "
            f"(Streak: {prof['streak']} Tag(e), Bonus +{bonus})")


async def _pay(message: discord.Message) -> str:
    if not message.mentions:
        return f"So geht's: `{_bot_name} pay @jemand 100`"
    ziel = message.mentions[0]
    if ziel.id == message.author.id:
        return "Dir selbst Geld geben? Netter Versuch. 😄"
    if ziel.bot:
        return "Bots brauchen kein Geld. 🤖"
    m = re.search(r"(\d+)", re.sub(r"<@!?\d+>", " ", message.content or ""))
    if not m:
        return f"Wie viel denn? `{_bot_name} pay @{ziel.display_name} 100`"
    betrag = int(m.group(1))
    if betrag <= 0:
        return "Der Betrag muss positiv sein."
    if get_coins(message.author.id) < betrag:
        return f"Du hast nicht genug. Kontostand: {get_coins(message.author.id)} {COIN}."
    add_coins(message.author.id, -betrag)
    add_coins(ziel.id, betrag)
    await _flush()
    return f"✅ {message.author.display_name} → {ziel.display_name}: **{betrag} {COIN}**."


def _shop() -> discord.Embed:
    emb = discord.Embed(
        title="🛒 Shop",
        description="Titel sind Kosmetik (Level-Karte + Bestenliste) – und Flo "
                    "spricht dich mit deinem Titel an!",
        color=discord.Color.blurple(),
    )
    for key, item in SHOP.items():
        emb.add_field(
            name=f"{item['titel']} — `{key}`",
            value=f"**{item['preis']} {COIN}** · {item['info']}",
            inline=False,
        )
    emb.set_footer(
        text=f"Kaufen: {_bot_name} kaufen <name>  ·  Wechseln: {_bot_name} titel <name>"
    )
    return emb


async def _buy(member: discord.abc.User, low: str) -> str:
    parts = low.split()
    name = parts[1] if len(parts) > 1 else ""
    item = SHOP.get(name)
    if not item:
        return f"Das gibt's nicht. Schau in den `{_bot_name} shop`."
    prof = _profile(member.id)
    owned = prof.setdefault("owned", [])
    if name in owned:
        # Schon gekauft -> direkt anlegen, statt nochmal zu kassieren.
        if prof.get("title") == item["titel"]:
            return f"Den Titel **{item['titel']}** trägst du schon. 😎"
        prof["title"] = item["titel"]
        await _flush()
        return f"Den hast du schon – ich hab dir **{item['titel']}** angelegt. 😎"
    if prof["coins"] < item["preis"]:
        fehlt = item["preis"] - prof["coins"]
        return f"Zu teuer - dir fehlen noch {fehlt} {COIN}."
    prof["coins"] -= item["preis"]
    owned.append(name)
    prof["title"] = item["titel"]
    await _flush()
    return (f"🎉 Gekauft! Du trägst jetzt den Titel **{item['titel']}**. "
            f"Ab jetzt spricht Flo dich damit an.")


async def _equip(member: discord.abc.User, low: str) -> str:
    parts = low.split()
    name = parts[1] if len(parts) > 1 else ""
    prof = _profile(member.id)
    if name in ("ab", "aus", "weg", "kein", "keinen", "none", "off"):
        prof["title"] = ""
        await _flush()
        return "Titel abgelegt – du trägst jetzt keinen. 🫥"
    if not name:
        return (f"Welchen Titel? `{_bot_name} titel <name>` "
                f"(deine Titel: `{_bot_name} inventar`).")
    item = SHOP.get(name)
    if not item:
        return f"Den Titel `{name}` gibt's nicht. Schau in den `{_bot_name} shop`."
    if name not in prof.get("owned", []):
        return (f"Den Titel **{item['titel']}** besitzt du nicht. "
                f"Kaufen: `{_bot_name} kaufen {name}`.")
    if prof.get("title") == item["titel"]:
        return f"Du trägst **{item['titel']}** bereits. 😎"
    prof["title"] = item["titel"]
    await _flush()
    return f"✅ Titel gewechselt: Du trägst jetzt **{item['titel']}**."
