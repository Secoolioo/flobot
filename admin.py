"""Admin-Befehle fuer den Bot-Besitzer (OWNER_ID) - im Server UND privat (DM).

Nur der Besitzer bekommt Antworten; fuer alle anderen gibt jeder Befehl hier
None zurueck (= naechster Handler ist dran, als gaebe es das Modul nicht).

Befehle (nach 'Flo' - in der DM geht's auch ohne 'Flo' davor):
- gib <@wer|ID> <anzahl>        Coins schenken
- nimm <@wer|ID> <anzahl>       Coins abziehen
- setcoins <@wer|ID> <anzahl>   Kontostand exakt setzen
- gibxp <@wer|ID> <anzahl>      XP geben (Level-Ups inklusive)
- profil <@wer|ID>              Konto-Uebersicht (Level, XP, Coins, Aktivitaet)
- ansage <channel-id> <text>    Text als Flo in einen Channel senden
- shopneu                       Tages-Shop sofort neu wuerfeln
- admin / adminhilfe            diese Liste

Ziel-Angabe: @-Mention ODER rohe User-ID (in der DM gibt es keine Mentions,
da ist die ID der Weg). Betraege duerfen bei 'gib' auch negativ sein.
"""
from __future__ import annotations

import logging
import os
import re

import discord

import ai
import economy

log = logging.getLogger("dcbot.admin")

OWNER_ID = int(os.getenv("OWNER_ID", "1040135855710404659") or "0")

_enabled: bool = False
_bot_name: str = "Flo"

_MENTION_RE = re.compile(r"<@!?(\d{15,20})>")
_ID_RE = re.compile(r"\b(\d{15,20})\b")
_AMOUNT_RE = re.compile(r"-?\d{1,12}")


def setup() -> bool:
    """Aktiviert die Admin-Befehle (braucht nur eine OWNER_ID)."""
    global _enabled, _bot_name
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if not OWNER_ID:
        log.info("Admin-Befehle aus (keine OWNER_ID).")
        return False
    _enabled = True
    log.info("Admin-Befehle aktiv (Besitzer %d).", OWNER_ID)
    return True


def is_enabled() -> bool:
    return _enabled


# --- Parsen ----------------------------------------------------------------
def _extract(rest: str) -> tuple[int | None, int | None]:
    """Zieht (ziel_user_id, betrag) aus dem Resttext: erst @-Mention, sonst
    rohe 15-20-stellige ID; der Betrag ist die erste verbleibende Zahl."""
    text = rest or ""
    uid: int | None = None
    m = _MENTION_RE.search(text)
    if m:
        uid = int(m.group(1))
        text = text.replace(m.group(0), " ", 1)
    else:
        m2 = _ID_RE.search(text)
        if m2:
            uid = int(m2.group(1))
            text = text[:m2.start()] + " " + text[m2.end():]
    m3 = _AMOUNT_RE.search(text)
    amount = int(m3.group(0)) if m3 else None
    return uid, amount


async def _user_of(message: discord.Message, uid: int):
    """Bestmoegliches User-Objekt zur ID (Mention > Guild-Cache > API)."""
    for m in message.mentions:
        if m.id == uid:
            return m
    if message.guild is not None:
        member = message.guild.get_member(uid)
        if member is not None:
            return member
    try:
        import bot
        return bot.client.get_user(uid) or await bot.client.fetch_user(uid)
    except Exception:  # noqa: BLE001 - unbekannte ID o. Ae.
        return None


async def _name_of(message: discord.Message, uid: int) -> str:
    user = await _user_of(message, uid)
    return user.display_name if user is not None else f"User {uid}"


def _emb(text: str, *, color: discord.Color | None = None) -> discord.Embed:
    emb = discord.Embed(description=text, color=color or discord.Color.gold())
    emb.set_author(name="🛠️ Flo Admin")
    return emb


# --- Befehle ----------------------------------------------------------------
async def handle(message: discord.Message) -> "str | discord.Embed | None":
    """Owner-only. None = kein Admin-Befehl -> naechster Handler ist dran."""
    if not _enabled or message.author.id != OWNER_ID:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.split(None, 1)
    first = parts[0].lower().strip(".,;:!?")
    rest = parts[1] if len(parts) > 1 else ""

    if first in ("gib", "give", "schenk", "schenke"):
        return await _give(message, rest, sign=+1)
    if first in ("nimm", "take", "entzieh", "entziehe"):
        return await _give(message, rest, sign=-1)
    if first in ("setcoins", "coinsset"):
        return await _set_coins(message, rest)
    if first in ("gibxp", "givexp", "xpgeben"):
        return await _give_xp(message, rest)
    if first in ("profil", "profile"):
        return await _profile(message, rest)
    if first in ("ansage", "announce"):
        return await _announce(message, rest)
    if first in ("dm", "flüster", "fluester"):
        return await _dm(message, rest)
    if first in ("shopneu", "shoprefresh"):
        return await _shop_refresh()
    if first in ("admin", "adminhilfe", "adminhelp"):
        return _admin_help()
    return None


async def _give(message: discord.Message, rest: str, *, sign: int
                ) -> "str | discord.Embed | None":
    uid, amount = _extract(rest)
    if uid is None and amount is None:
        # 'gib mir mal einen Tipp' u. Ae.: kein Admin-Befehl, sondern Chat ->
        # weiterreichen (None), damit die KI antworten kann.
        return None
    if not economy.is_enabled():
        return "Economy (Flo Coins) ist gerade aus."
    verb = "gib" if sign > 0 else "nimm"
    if uid is None or amount is None:
        return f"So: `{_bot_name} {verb} @wer 100` (oder mit User-ID)."
    delta = sign * abs(amount) if sign < 0 else sign * amount
    neu = economy.add_coins(uid, delta)
    await economy.flush()
    name = await _name_of(message, uid)
    if delta >= 0:
        return _emb(f"💰 **{name}** bekommt **+{delta} {economy.COIN}** "
                    f"→ neuer Stand: **{neu}**.", color=discord.Color.green())
    return _emb(f"🪙 **{name}** verliert **{delta} {economy.COIN}** "
                f"→ neuer Stand: **{neu}**.", color=discord.Color.orange())


async def _set_coins(message: discord.Message, rest: str) -> "str | discord.Embed":
    if not economy.is_enabled():
        return "Economy (Flo Coins) ist gerade aus."
    uid, amount = _extract(rest)
    if uid is None or amount is None or amount < 0:
        return f"So: `{_bot_name} setcoins @wer 500` (Betrag ≥ 0)."
    delta = amount - economy.get_coins(uid)
    neu = economy.add_coins(uid, delta)
    await economy.flush()
    name = await _name_of(message, uid)
    return _emb(f"🎯 Kontostand von **{name}** auf **{neu} {economy.COIN}** gesetzt.")


async def _give_xp(message: discord.Message, rest: str) -> "str | discord.Embed | None":
    uid, amount = _extract(rest)
    if uid is None and amount is None:
        return None   # Chat, kein Befehl - weiterreichen an die KI
    if not economy.is_enabled():
        return "Economy (Flo Coins) ist gerade aus."
    if uid is None or amount is None or amount <= 0:
        return f"So: `{_bot_name} gibxp @wer 250`."
    user = await _user_of(message, uid)
    if user is None:
        return f"Ich finde keinen User mit der ID {uid}."
    level = await economy.add_xp(user, amount)
    await economy.flush()
    extra = f" → **Level {level}**! 🎉" if level else ""
    return _emb(f"⭐ **{user.display_name}** bekommt **+{amount} XP**{extra}")


async def _profile(message: discord.Message, rest: str) -> "str | discord.Embed":
    if not economy.is_enabled():
        return "Economy (Flo Coins) ist gerade aus."
    uid, _ = _extract(rest)
    if uid is None:
        uid = message.author.id   # ohne Ziel: eigenes Profil
    # leaderboard_data liefert ALLE Profile (oeffentliche API) - Row suchen.
    rows = economy.leaderboard_data(limit=10 ** 9)
    row = next((r for r in rows if r.get("id") == uid), None)
    name = await _name_of(message, uid)
    if row is None:
        return _emb(f"**{name}** hat noch kein Profil (nie geschrieben).")
    h, rem = divmod(int(row.get("voice_secs", 0)), 3600)
    emb = _emb(f"👤 **{name}**", color=discord.Color.blurple())
    emb.add_field(name="Level", value=f"{row.get('level', 0)} ({row.get('xp', 0)} XP)",
                  inline=True)
    emb.add_field(name="Coins", value=f"{row.get('coins', 0)} {economy.COIN}",
                  inline=True)
    emb.add_field(name="Aktivität",
                  value=f"{row.get('msgs', 0)} Nachrichten · {h}h {rem // 60}m Voice",
                  inline=False)
    if row.get("title"):
        emb.add_field(name="Titel", value=row["title"], inline=False)
    return emb


def _parse_announce(rest: str) -> tuple[int | None, str]:
    """Zerlegt 'ansage ...' in (channel_id, text). Akzeptiert die rohe ID
    UND eine Channel-Erwaehnung wie <#1234...>."""
    m = re.match(r"\s*(?:<#)?(\d{15,20})>?\s+(.+)", rest or "", re.DOTALL)
    if not m:
        return None, ""
    return int(m.group(1)), m.group(2).strip()


async def _announce(message: discord.Message, rest: str) -> str:
    cid, text = _parse_announce(rest)
    if cid is None or not text:
        return (f"So: `{_bot_name} ansage <channel-id> <text>` "
                f"(auch `{_bot_name} ansage #channel <text>`) - "
                "ich schicke den Text als Flo in den Channel.")
    channel = None
    try:
        import bot
        channel = bot.client.get_channel(cid)
        if channel is None:
            # Nicht im Cache (z. B. Thread oder frisch angelegt) -> per API holen.
            channel = await bot.client.fetch_channel(cid)
    except discord.NotFound:
        return f"Es gibt keinen Channel mit der ID `{cid}`."
    except discord.Forbidden:
        return "Auf diesen Channel habe ich keinen Zugriff."
    except Exception:  # noqa: BLE001 - z. B. Client (noch) nicht bereit
        log.exception("Ansage: Channel-Aufloesung fehlgeschlagen")
        channel = None
    if channel is None or not hasattr(channel, "send"):
        return "Diesen Channel finde ich nicht (oder er ist kein Text-Channel)."
    try:
        await channel.send(text)
    except discord.Forbidden:
        return f"Mir fehlt das Schreibrecht in **#{getattr(channel, 'name', cid)}**."
    except discord.HTTPException as exc:
        return f"Senden fehlgeschlagen: {exc}"
    return f"✅ Gesendet in **#{getattr(channel, 'name', '?')}**."


def _parse_dm(rest: str) -> tuple[int | None, str]:
    """'@wer hallo du' / '1234... hallo du' -> (user_id, text)."""
    m = _MENTION_RE.search(rest or "") or _ID_RE.search(rest or "")
    if not m:
        return None, ""
    text = (rest[:m.start()] + rest[m.end():]).strip()
    return int(m.group(1)), text


async def _dm(message: discord.Message, rest: str) -> "str | discord.Embed":
    """Flo schreibt jemandem privat - als waere er's selbst. Antworten der
    Person leitet bot.py automatisch an den Besitzer zurueck (DM-Relay)."""
    uid, text = _parse_dm(rest)
    if uid is None or not text:
        return (f"So: `{_bot_name} dm @wer <text>` (oder mit User-ID) - "
                "ich stelle es privat zu; Antworten landen wieder bei dir.")
    user = await _user_of(message, uid)
    if user is None:
        return f"Ich finde keinen User mit der ID {uid}."
    if getattr(user, "bot", False):
        return "Bots lesen keine DMs. 🤖"
    try:
        await user.send(text)
    except discord.Forbidden:
        return (f"**{user.display_name}** hat DMs zu (oder blockiert mich) - "
                "Zustellung nicht möglich. 📪")
    except discord.HTTPException as exc:
        return f"Zustellung fehlgeschlagen: {exc}"
    kurz = text if len(text) <= 150 else text[:150] + "…"
    emb = _emb(f"📨 An **{user.display_name}** zugestellt:\n> {kurz}",
               color=discord.Color.green())
    emb.set_footer(text="Antworten leite ich automatisch an dich weiter.")
    return emb


async def _shop_refresh() -> str:
    if not economy.is_enabled():
        return "Economy (Flo Coins) ist gerade aus."
    st = await economy.refresh_shop_async(force=True)
    return (f"✅ Shop neu gewürfelt: {len(st.get('items', []))} Titel "
            f"für {st.get('date', '?')}.")


def _admin_help() -> discord.Embed:
    n = _bot_name
    emb = discord.Embed(
        title="🛠️ Admin-Befehle (nur für dich)",
        description="Funktionieren im Server und hier privat - "
                    "Ziel per @-Mention oder User-ID.",
        color=discord.Color.gold())
    emb.add_field(name="Coins",
                  value=(f"`{n} gib @wer 100` · `{n} nimm @wer 100`\n"
                         f"`{n} setcoins @wer 500`"), inline=False)
    emb.add_field(name="XP & Profil",
                  value=f"`{n} gibxp @wer 250` · `{n} profil @wer`", inline=False)
    emb.add_field(name="Server",
                  value=(f"`{n} ansage <channel-id> <text>` · `{n} shopneu`\n"
                         f"`{n} restart`"), inline=False)
    emb.add_field(name="DM-Relay",
                  value=(f"`{n} dm @wer <text>` – {n} schreibt privat; "
                         "Antworten landen automatisch bei dir."), inline=False)
    return emb
