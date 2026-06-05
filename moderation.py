"""Moderation fuer Flo (Pack 6): das ganze Moderations-Werkzeug per Chat-Befehl.

Alles laeuft natuerlich-sprachig nach dem Botnamen, z. B.:

  Aufraeumen   Flo lösch 20            · Flo lösch alle · Flo clear 50 · Flo nuke
  Verwarnen    Flo warn @x Spam        · Flo warns @x   · Flo unwarn @x [alle]
  Timeout      Flo timeout @x 10m Spam · Flo untimeout @x   (alias: mute/stumm)
  Kick         Flo kick @x Grund       (alias: rauswerfen/rausschmeißen)
  Bann         Flo ban @x Grund        · Flo unban <ID>     (alias: sperren)

Design-Prinzipien:
- Jede Aktion prueft ZWEI Dinge: hat der *Aufrufer* das noetige Recht, und hat
  *Flo selbst* es auf dem Server? Dazu eine Rollen-Hierarchie-Pruefung (niemand
  ueber dem Aufrufer/Bot, kein Owner, nicht Flo selbst).
- Erfolge kommen als sauberes Embed zurueck (bot.py schickt es) und landen
  zusaetzlich im optionalen Mod-Log-Channel (MOD_LOG_CHANNEL_ID) als Protokoll.
- Verwarnungen werden in data/moderation.json gespeichert; bei Erreichen von
  WARN_LIMIT setzt es automatisch einen Timeout (WARN_TIMEOUT_SECONDS).
- Purge schuetzt angepinnte Nachrichten und loescht auch >14 Tage alte (einzeln).

Rueckgabe von handle():
- None      -> kein Moderations-Befehl (naechster Handler/KI ist dran).
- str       -> Hinweis/Fehlertext, den bot.py normal als Antwort schickt.
- Embed     -> Erfolg; bot.py schickt das Embed (zusaetzlich Mod-Log).
- HANDLED   -> schon selbst geantwortet (Purge-Bestaetigung), bot.py sendet nichts.
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta

import discord

import ai
from store import JsonStore

log = logging.getLogger("dcbot.mod")

_enabled: bool = False
_bot_name: str = "Flo"
_store: JsonStore | None = None

# handle() gibt das zurueck, wenn es den Befehl SELBST erledigt und bereits
# geantwortet hat -> bot.py soll dann nichts mehr senden.
HANDLED = object()

# --- Einstellungen (per .env) -------------------------------------------
# Sicherheitslimit fuer eine einzelne Zahl-Angabe ("loesch 5000" wird gedeckelt).
MAX_PURGE = int(os.getenv("PURGE_MAX", "1000") or "1000")
# Wie lange die Bestaetigung stehen bleibt, bevor sie sich selbst loescht.
CONFIRM_TTL = 6.0
# Optionaler Protokoll-Channel fuer alle Mod-Aktionen (0 = aus).
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0") or "0")
# Ab so vielen Verwarnungen setzt es automatisch einen Timeout ...
WARN_LIMIT = int(os.getenv("WARN_LIMIT", "3") or "3")
# ... von dieser Laenge (Sekunden).
WARN_TIMEOUT_SECONDS = int(os.getenv("WARN_TIMEOUT_SECONDS", "3600") or "3600")
# Timeout-Standardlaenge, wenn der Befehl keine Dauer nennt.
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("MOD_DEFAULT_TIMEOUT", "600") or "600")
# Discord-Hartlimit fuer Timeouts: 28 Tage.
DISCORD_TIMEOUT_MAX = 28 * 24 * 3600

# --- Befehls-Erkennung (auf dem um den Botnamen bereinigten Text) --------
# Loesch-Befehl am Satzanfang. NAME bewusst _CMD_RE (Self-Test referenziert ihn).
_CMD_RE = re.compile(
    r"^(?:l(?:ö|oe)sch\w*|delete|del|clear|purge|aufr(?:ä|ae)um\w*|cleanup|nuke)\b",
    re.IGNORECASE,
)
# "alle/alles/all/komplett/ganz/everything" -> ganzen Channel leeren / alle Warns.
_ALL_RE = re.compile(r"\b(?:alles?|all|everything|komplett|ganz)\b", re.IGNORECASE)

# Verwarnungen (Reihenfolge in handle(): unwarn -> warns -> warn).
_WARNS_RE = re.compile(
    r"^(?:warns|verwarnungen|warnungen|warnliste)\b", re.IGNORECASE)
_UNWARN_RE = re.compile(
    r"^(?:un-?warn\w*|entwarn\w*|verzeih\w*)\b", re.IGNORECASE)
_WARN_RE = re.compile(r"^(?:ver)?warn\w*\b", re.IGNORECASE)

# Timeout (Reihenfolge: untimeout -> timeout).
_UNTIMEOUT_RE = re.compile(
    r"^(?:un-?timeout|enttimeout|un-?mute|unmuten|entmute\w*|entstumm\w*|"
    r"entknebel\w*|timeout\s+(?:weg|aus|raus|entfern\w*))\b", re.IGNORECASE)
_TIMEOUT_RE = re.compile(
    r"^(?:timeout|time-out|mute|muten|stumm\w*|knebel\w*|auszeit)\b", re.IGNORECASE)

# Kick: bewusst KEIN nacktes "raus" (sonst Kollision mit Musik 'geh raus').
_KICK_RE = re.compile(
    r"^(?:kick\w*|rauswerf\w*|rausschmei(?:ß|ss)\w*)\b", re.IGNORECASE)

# Bann (Reihenfolge: unban -> ban).
_UNBAN_RE = re.compile(
    r"^(?:un-?bann?\w*|entbann\w*|entsperr\w*)\b", re.IGNORECASE)
_BAN_RE = re.compile(
    r"^(?:bann?(?:e|en|t|st|ne|nen)?|verbann\w*|sperr\w*)\b", re.IGNORECASE)

# --- Hilfs-Muster --------------------------------------------------------
_MENTION_RE = re.compile(r"<@[!&]?\d+>")
_ID_RE = re.compile(r"\b(\d{15,20})\b")
_UNIT_SECONDS = {
    "sekunden": 1, "sekunde": 1, "sek": 1, "s": 1,
    "minuten": 60, "minute": 60, "min": 60, "m": 60,
    "stunden": 3600, "stunde": 3600, "std": 3600, "h": 3600,
    "tagen": 86400, "tage": 86400, "tag": 86400, "d": 86400,
    "wochen": 604800, "woche": 604800, "w": 604800,
}
_DURATION_RE = re.compile(
    r"(\d+)\s*(sekunden|sekunde|sek|minuten|minute|min|stunden|stunde|std|"
    r"tagen|tage|tag|wochen|woche|s|m|h|d|w)\b", re.IGNORECASE)

# Routing-Tabelle: erste passende Regel gewinnt. Reihenfolge ist Absicht -
# Purge zuerst, dann je Gruppe UN-/Listen-Varianten VOR der Basis.
_ROUTES = (
    ("purge", _CMD_RE),
    ("unwarn", _UNWARN_RE),
    ("warns", _WARNS_RE),
    ("warn", _WARN_RE),
    ("untimeout", _UNTIMEOUT_RE),
    ("timeout", _TIMEOUT_RE),
    ("kick", _KICK_RE),
    ("unban", _UNBAN_RE),
    ("ban", _BAN_RE),
)


def classify(cmd: str) -> "str | None":
    """Welche Moderations-Aktion steckt im (schon um den Botnamen bereinigten)
    Text? Gibt das Label zurueck oder None. Pur testbar - handle() nutzt es."""
    for label, rx in _ROUTES:
        if rx.match(cmd):
            return label
    return None


def setup() -> bool:
    """Aktiviert das Moderation-Feature. Keine externen Voraussetzungen - die
    noetigen Rechte werden erst beim jeweiligen Befehl geprueft."""
    global _enabled, _bot_name, _store
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("MOD_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Moderation-Feature aus (MOD_ENABLED=0).")
        return False
    _store = JsonStore("moderation.json", default={"warns": {}})
    _enabled = True
    log.info(
        "Moderation aktiv (Purge bis %d · warn/timeout/kick/ban · "
        "Warn-Limit %d -> %s Timeout · Mod-Log: %s).",
        MAX_PURGE, WARN_LIMIT, _fmt_duration(WARN_TIMEOUT_SECONDS),
        "an" if MOD_LOG_CHANNEL_ID else "aus",
    )
    return True


def is_enabled() -> bool:
    return _enabled


# --- Routing -------------------------------------------------------------
async def handle(message: discord.Message) -> "object | str | discord.Embed | None":
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None

    label = classify(cmd)
    if label is None:
        return None
    if label == "purge":
        return await _do_purge(message, cmd)  # braucht den ganzen Befehl (Zahl/'alle')
    rx = dict(_ROUTES)[label]
    rest = rx.sub("", cmd, count=1).strip()
    return await _HANDLERS[label](message, rest)


# --- gemeinsame Helfer ---------------------------------------------------
def _need(label: str) -> str:
    return f"Dafür brauchst du das Recht **{label}**. 🔒"


def _bot_need(label: str) -> str:
    return f"Mir fehlt das Recht **{label}** auf dem Server – das muss mir ein Admin geben. 🔒"


def _actor_can(message: discord.Message, attr: str) -> bool:
    p = getattr(message.author, "guild_permissions", None)
    return bool(p and (getattr(p, attr, False) or p.administrator))


def _bot_can(guild: discord.Guild, attr: str) -> bool:
    p = guild.me.guild_permissions
    return bool(getattr(p, attr, False) or p.administrator)


def _resolve_target(message: discord.Message, rest: str):
    """Findet das Ziel: erst eine echte @-Erwaehnung (ausser Flo selbst), sonst
    eine rohe 15-20-stellige ID. Gibt (member_or_user|None, id|None, rest_ohne_ziel).
    Bei reiner ID kann member None sein (z. B. fuer Ban/Unban von Nicht-Mitgliedern)."""
    me_id = message.guild.me.id
    mentioned = [u for u in message.mentions if u.id != me_id]
    if mentioned:
        target = mentioned[0]
        rest = _MENTION_RE.sub("", rest).strip()
        return target, target.id, rest
    m = _ID_RE.search(rest)
    if m:
        uid = int(m.group(1))
        rest2 = (rest[:m.start()] + rest[m.end():]).strip()
        return message.guild.get_member(uid), uid, rest2
    return None, None, rest


def _clean_reason(rest: str) -> str:
    r = _MENTION_RE.sub("", rest or "").strip()
    r = re.sub(r"^(?:wegen|weil|f(?:ü|ue)r|for|grund|reason)\b[:\s]*", "", r,
               flags=re.IGNORECASE).strip()
    # Auf 500 deckeln: Discords Audit-Log-Grund erlaubt max. 512 Zeichen, und das
    # Embed-Feld max. 1024 - so kann ein Mega-Grund den Kick/Ban nicht crashen.
    return r.strip(" :–-")[:500]


def _parse_duration(text: str):
    """Liest die erste Zeitangabe (z. B. '10m', '2 stunden') und gibt
    (sekunden, rest_ohne_dauer). Ohne Treffer: (None, text)."""
    m = _DURATION_RE.search(text)
    if not m:
        return None, text
    secs = int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
    rest = (text[:m.start()] + text[m.end():]).strip()
    return secs, rest


def _fmt_duration(secs: int) -> str:
    secs = int(secs)
    parts: list[str] = []
    for one, many, size in (("Tag", "Tage", 86400), ("Stunde", "Stunden", 3600),
                            ("Minute", "Minuten", 60), ("Sekunde", "Sekunden", 1)):
        if secs >= size:
            n, secs = divmod(secs, size)
            parts.append(f"{n} {one if n == 1 else many}")
        if len(parts) >= 2:
            break
    return " ".join(parts) if parts else "0 Sekunden"


def _hierarchy_problem(message: discord.Message, member, full: bool = True) -> "str | None":
    """Gibt einen Klartext-Grund zurueck, WARUM die Aktion nicht erlaubt ist,
    sonst None. 'full' schaltet die Rollen-Rang-Pruefung dazu (fuer harte
    Aktionen); fuer Verwarnungen reicht der leichte Check (self/bot/owner)."""
    guild = message.guild
    if member.id == guild.me.id:
        return "Mich selbst moderiere ich nicht. 😎"
    if member.id == getattr(message.author, "id", 0):
        return "Dich selbst? Lieber nicht. 🙂"
    if guild.owner_id == member.id:
        return "Den Server-Owner kann ich nicht anfassen. 👑"
    if not full or not isinstance(member, discord.Member):
        return None
    author = message.author
    if (getattr(author, "id", 0) != guild.owner_id and isinstance(author, discord.Member)
            and member.top_role >= author.top_role):
        return "Diese Person hat eine gleich hohe oder höhere Rolle als du. ⛔"
    if member.top_role >= guild.me.top_role:
        return "Diese Person steht in der Rollen-Rangordnung über mir – da komme ich nicht ran. ⛔"
    return None


def _bot_hierarchy_ok(guild: discord.Guild, member: discord.Member) -> bool:
    """Reicht Flos Rolle, um gegen 'member' vorzugehen? (Fuer den Auto-Timeout.)"""
    if member.id in (guild.me.id, guild.owner_id):
        return False
    return guild.me.top_role > member.top_role


def _action_embed(emoji: str, titel: str, color: discord.Color, target, by,
                  reason: str, extra: "list[tuple[str, str]] | None" = None) -> discord.Embed:
    if isinstance(target, (discord.Member, discord.User)):
        who = f"{target.mention}\n`{target}` · ID `{target.id}`"
        avatar = target.display_avatar.url
    else:  # rohe ID (z. B. Ban von jemandem, der nicht im Server ist)
        who = f"<@{target}>\nID `{target}`"
        avatar = None
    emb = discord.Embed(title=f"{emoji} {titel}", color=color,
                        timestamp=discord.utils.utcnow())
    emb.add_field(name="Mitglied", value=who, inline=True)
    emb.add_field(name="Moderator", value=getattr(by, "mention", str(by)), inline=True)
    for name, value in (extra or []):
        emb.add_field(name=name, value=value, inline=True)
    emb.add_field(name="Grund", value=reason or "—", inline=False)
    if avatar:
        try:
            emb.set_thumbnail(url=avatar)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko
            pass
    emb.set_footer(text=f"{_bot_name} · Moderation")
    return emb


async def _modlog(message: discord.Message, embed: discord.Embed) -> None:
    """Schreibt das Aktions-Embed zusaetzlich ins Mod-Log (falls eingerichtet und
    nicht ohnehin derselbe Channel, in dem der Befehl kam)."""
    if not MOD_LOG_CHANNEL_ID:
        return
    ch = message.guild.get_channel(MOD_LOG_CHANNEL_ID)
    if ch is None or ch.id == message.channel.id or not hasattr(ch, "send"):
        return
    if not ch.permissions_for(message.guild.me).send_messages:
        return
    try:
        await ch.send(embed=embed)
    except discord.HTTPException:
        pass


async def _apply_timeout(guild: discord.Guild, member: discord.Member,
                         secs: int, reason: str) -> bool:
    secs = max(1, min(DISCORD_TIMEOUT_MAX, int(secs)))
    try:
        await member.timeout(timedelta(seconds=secs), reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Timeout fehlgeschlagen: %s", exc)
        return False


# --- Verwarnungen --------------------------------------------------------
def _warns_for(guild_id: int) -> dict:
    return _store.data.setdefault("warns", {}).setdefault(str(guild_id), {})


async def _do_warn(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "moderate_members"):
        return _need("Mitglieder moderieren")
    member, uid, rest = _resolve_target(message, rest)
    if member is None:
        return (f"Wen soll ich verwarnen? z. B. `{_bot_name} warn @name Spam`."
                if uid is None else "Diese Person ist nicht (mehr) auf dem Server.")
    prob = _hierarchy_problem(message, member, full=False)
    if prob:
        return prob
    reason = _clean_reason(rest) or "kein Grund angegeben"

    gw = _warns_for(guild.id)
    lst = gw.setdefault(str(member.id), [])
    lst.append({"by": message.author.id, "reason": reason, "ts": time.time()})
    count = len(lst)

    auto_note = None
    if (count >= WARN_LIMIT and WARN_TIMEOUT_SECONDS > 0
            and _bot_can(guild, "moderate_members")
            and isinstance(member, discord.Member) and _bot_hierarchy_ok(guild, member)):
        if await _apply_timeout(guild, member, WARN_TIMEOUT_SECONDS,
                                f"Auto-Timeout nach {count} Verwarnungen"):
            gw[str(member.id)] = []  # Zaehler nach der Strafe zuruecksetzen
            auto_note = (f"⏳ Limit erreicht → **{_fmt_duration(WARN_TIMEOUT_SECONDS)}** "
                         f"Timeout. Zähler zurückgesetzt.")
    await _store.save()

    emb = _action_embed("⚠️", "Verwarnung", discord.Color.gold(), member,
                        message.author, reason, [("Stand", f"**{count}/{WARN_LIMIT}**")])
    if auto_note:
        emb.add_field(name="Folge", value=auto_note, inline=False)
    await _modlog(message, emb)
    return emb


async def _do_warns(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "moderate_members"):
        return _need("Mitglieder moderieren")
    member, uid, rest = _resolve_target(message, rest)
    if member is None and uid is None:
        return f"Von wem die Verwarnungen? z. B. `{_bot_name} warns @name`."
    key = str(uid if uid is not None else member.id)
    who = member.mention if member is not None else f"<@{uid}>"
    lst = _store.data.get("warns", {}).get(str(guild.id), {}).get(key, [])
    if not lst:
        return f"✅ {who} hat aktuell **keine** Verwarnungen."
    emb = discord.Embed(title="⚠️ Verwarnungen", color=discord.Color.gold(),
                        timestamp=discord.utils.utcnow())
    emb.description = f"{who} hat **{len(lst)}/{WARN_LIMIT}** Verwarnungen:"
    for i, w in enumerate(lst[-10:], 1):
        ts = datetime.fromtimestamp(w.get("ts", 0)).strftime("%d.%m.%Y")
        emb.add_field(name=f"#{i} · {ts}",
                      value=f"{w.get('reason', '—')}\n— von <@{w.get('by')}>",
                      inline=False)
    emb.set_footer(text=f"{_bot_name} · Moderation")
    return emb


async def _do_unwarn(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "moderate_members"):
        return _need("Mitglieder moderieren")
    member, uid, rest = _resolve_target(message, rest)
    if member is None and uid is None:
        return f"Wem eine Verwarnung erlassen? z. B. `{_bot_name} unwarn @name` (oder `... alle`)."
    key = str(uid if uid is not None else member.id)
    who = member.mention if member is not None else f"<@{uid}>"
    gw = _store.data.get("warns", {}).get(str(guild.id), {})
    lst = gw.get(key, [])
    if not lst:
        return f"{who} hat gar keine Verwarnungen. 🤷"
    if _ALL_RE.search(rest):
        gw[key] = []
        was = "Alle Verwarnungen entfernt"
    else:
        lst.pop()
        was = "Letzte Verwarnung entfernt"
    await _store.save()
    emb = _action_embed("✅", "Verwarnung erlassen", discord.Color.green(),
                        member if member is not None else uid, message.author, was,
                        [("Rest", f"**{len(gw.get(key, []))}/{WARN_LIMIT}**")])
    await _modlog(message, emb)
    return emb


# --- Timeout -------------------------------------------------------------
async def _do_timeout(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "moderate_members"):
        return _need("Mitglieder moderieren")
    if not _bot_can(guild, "moderate_members"):
        return _bot_need("Mitglieder moderieren (Timeout)")
    secs, rest = _parse_duration(rest)
    member, uid, rest = _resolve_target(message, rest)
    if member is None:
        return (f"Wen für wie lange? z. B. `{_bot_name} timeout @name 10m Spam`."
                if uid is None else "Diese Person ist nicht (mehr) auf dem Server.")
    prob = _hierarchy_problem(message, member)
    if prob:
        return prob
    secs = max(1, min(DISCORD_TIMEOUT_MAX, secs if secs is not None else DEFAULT_TIMEOUT_SECONDS))
    reason = _clean_reason(rest) or "kein Grund angegeben"
    if not await _apply_timeout(guild, member, secs, f"{reason} · von {message.author}"):
        return "Der Timeout hat nicht geklappt (Rechte oder Rollen-Hierarchie?)."
    emb = _action_embed("🔇", "Timeout", discord.Color.orange(), member,
                        message.author, reason, [("Dauer", _fmt_duration(secs))])
    await _modlog(message, emb)
    return emb


async def _do_untimeout(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "moderate_members"):
        return _need("Mitglieder moderieren")
    if not _bot_can(guild, "moderate_members"):
        return _bot_need("Mitglieder moderieren (Timeout)")
    member, uid, rest = _resolve_target(message, rest)
    if member is None:
        return f"Wem den Timeout abnehmen? z. B. `{_bot_name} untimeout @name`."
    try:
        await member.timeout(None, reason=f"Timeout aufgehoben von {message.author}")
    except (discord.Forbidden, discord.HTTPException):
        return "Konnte den Timeout nicht aufheben (Rechte/Hierarchie?)."
    emb = _action_embed("🔊", "Timeout aufgehoben", discord.Color.green(), member,
                        message.author, _clean_reason(rest) or "—")
    await _modlog(message, emb)
    return emb


# --- Kick ----------------------------------------------------------------
async def _do_kick(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "kick_members"):
        return _need("Mitglieder kicken")
    if not _bot_can(guild, "kick_members"):
        return _bot_need("Mitglieder kicken")
    member, uid, rest = _resolve_target(message, rest)
    if member is None:
        return (f"Wen rauswerfen? z. B. `{_bot_name} kick @name Grund`."
                if uid is None else "Diese Person ist nicht (mehr) auf dem Server.")
    prob = _hierarchy_problem(message, member)
    if prob:
        return prob
    reason = _clean_reason(rest) or "kein Grund angegeben"
    try:
        await member.kick(reason=f"{reason} · von {message.author}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Kick fehlgeschlagen: %s", exc)
        return "Der Kick hat nicht geklappt (Rechte/Hierarchie?)."
    emb = _action_embed("👢", "Gekickt", discord.Color.red(), member,
                        message.author, reason)
    await _modlog(message, emb)
    return emb


# --- Bann ----------------------------------------------------------------
async def _do_ban(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "ban_members"):
        return _need("Mitglieder bannen")
    if not _bot_can(guild, "ban_members"):
        return _bot_need("Mitglieder bannen")
    member, uid, rest = _resolve_target(message, rest)
    if member is None and uid is None:
        return f"Wen bannen? z. B. `{_bot_name} ban @name Grund` (oder per ID)."
    if isinstance(member, discord.Member):
        prob = _hierarchy_problem(message, member)
        if prob:
            return prob
    reason = _clean_reason(rest) or "kein Grund angegeben"
    target_obj = member if isinstance(member, (discord.Member, discord.User)) else discord.Object(id=uid)
    try:
        await guild.ban(target_obj, reason=f"{reason} · von {message.author}",
                        delete_message_seconds=0)
    except discord.NotFound:
        return "Diese Nutzer-ID gibt es nicht."
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Ban fehlgeschlagen: %s", exc)
        return "Der Bann hat nicht geklappt (Rechte/Hierarchie?)."
    emb = _action_embed("🔨", "Gebannt", discord.Color.dark_red(),
                        member if member is not None else uid, message.author, reason)
    await _modlog(message, emb)
    return emb


async def _do_unban(message: discord.Message, rest: str):
    guild = message.guild
    if not _actor_can(message, "ban_members"):
        return _need("Mitglieder bannen")
    if not _bot_can(guild, "ban_members"):
        return _bot_need("Mitglieder bannen")
    _, uid, rest = _resolve_target(message, rest)
    if uid is None:
        return f"Welche ID entbannen? z. B. `{_bot_name} unban 123456789012345678`."
    try:
        await guild.unban(discord.Object(id=uid), reason=f"Entbannt von {message.author}")
    except discord.NotFound:
        return "Diese ID ist gar nicht gebannt."
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Unban fehlgeschlagen: %s", exc)
        return "Das Entbannen hat nicht geklappt."
    emb = _action_embed("♻️", "Entbannt", discord.Color.green(), uid,
                        message.author, _clean_reason(rest) or "—")
    await _modlog(message, emb)
    return emb


# --- Aufraeumen / Purge --------------------------------------------------
def _keep(message: discord.Message) -> bool:
    """True = diese Nachricht NICHT loeschen (angepinnte schuetzen wir)."""
    return bool(message.pinned)


async def _do_purge(message: discord.Message, cmd: str):
    """Erkennt einen Loesch-Befehl und fuehrt ihn aus (gibt nie None zurueck)."""
    rest = _CMD_RE.sub("", cmd, count=1).strip()

    if not _actor_can(message, "manage_messages"):
        return "Dafür brauchst du das Recht **Nachrichten verwalten**. 🔒"

    channel = message.channel
    if not hasattr(channel, "purge"):
        return "Hier kann ich nichts löschen."
    me_perms = channel.permissions_for(message.guild.me)
    if not (me_perms.manage_messages and me_perms.read_message_history):
        return ("Mir fehlt hier das Recht **Nachrichten verwalten** "
                "(und Verlauf lesen) – dann kann ich nichts löschen.")

    want_all = bool(_ALL_RE.search(rest)) or cmd.lower().startswith("nuke")
    num_match = re.search(r"\d+", rest)

    try:
        if want_all:
            deleted = await channel.purge(limit=None, check=lambda m: not _keep(m))
            count = len(deleted)
        elif num_match:
            n = max(1, min(MAX_PURGE, int(num_match.group())))
            # +1, damit die Befehls-Nachricht selbst nicht als eine der n zaehlt.
            deleted = await channel.purge(limit=n + 1, check=lambda m: not _keep(m))
            count = max(0, len(deleted) - (0 if _keep(message) else 1))
        else:
            return (f"Wie viele? z. B. `{_bot_name} lösch 20` oder "
                    f"`{_bot_name} lösch alle`.")
    except discord.Forbidden:
        await _confirm(channel, "Mir fehlt das Recht zum Löschen. 🔒")
        return HANDLED
    except discord.HTTPException as exc:
        log.warning("Purge fehlgeschlagen: %s", exc)
        await _confirm(channel, "Das Löschen hat nicht ganz geklappt.")
        return HANDLED

    wort = "Nachricht" if count == 1 else "Nachrichten"
    await _confirm(channel, f"🧹 **{count}** {wort} gelöscht.")
    log.info(
        "Purge von %s in #%s: %d geloescht (%s).",
        message.author.display_name, getattr(channel, "name", channel.id), count,
        "alle" if want_all else f"max {num_match.group() if num_match else '?'}",
    )
    return HANDLED


async def _confirm(channel: discord.abc.Messageable, text: str) -> None:
    """Kurze Bestaetigung, die sich nach CONFIRM_TTL Sekunden selbst loescht."""
    try:
        await channel.send(text, delete_after=CONFIRM_TTL)
    except discord.HTTPException:
        pass


# Label -> Handler (Purge laeuft gesondert, da es den ganzen Befehl braucht).
_HANDLERS = {
    "unwarn": _do_unwarn,
    "warns": _do_warns,
    "warn": _do_warn,
    "untimeout": _do_untimeout,
    "timeout": _do_timeout,
    "kick": _do_kick,
    "unban": _do_unban,
    "ban": _do_ban,
}
