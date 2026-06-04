"""Moderation fuer Flo (Pack 6): Nachrichten loeschen (Purge) per Befehl.

Befehle (nach 'Flo'), nur fuer Mitglieder mit dem Recht 'Nachrichten verwalten':
- loesch / lösch / delete / clear / purge / aufraeumen <n>   -> die letzten n Nachrichten
- ... alle / all / alles / komplett                          -> der ganze Channel
- nuke                                                       -> Kurzform fuer "alles"

Angepinnte Nachrichten bleiben immer stehen (Schutz vor versehentlichem Verlust).
discord.py loescht dabei selbst Nachrichten, die aelter als 14 Tage sind (dann
einzeln statt im Bulk) - so verschwindet wirklich ALLES, nicht nur die letzten Tage.

Das Modul antwortet mit einer kurzen Bestaetigung, die sich selbst wieder loescht,
und meldet bot.py ueber das Sentinel HANDLED, dass schon geantwortet wurde.
"""
from __future__ import annotations

import logging
import os
import re

import discord

import ai

log = logging.getLogger("dcbot.mod")

_enabled: bool = False
_bot_name: str = "Flo"

# handle() gibt das zurueck, wenn es den Befehl SELBST erledigt und bereits
# geantwortet hat -> bot.py soll dann nichts mehr senden.
HANDLED = object()

# Sicherheitslimit fuer eine einzelne Zahl-Angabe ("loesch 5000" wird gedeckelt).
MAX_PURGE = int(os.getenv("PURGE_MAX", "1000") or "1000")
# Wie lange die Bestaetigung stehen bleibt, bevor sie sich selbst loescht.
CONFIRM_TTL = 6.0

# Loesch-Befehl am Satzanfang (nach Entfernen des Botnamens via ai.strip_lead).
_CMD_RE = re.compile(
    r"^(?:l(?:ö|oe)sch\w*|delete|del|clear|purge|aufr(?:ä|ae)um\w*|cleanup|nuke)\b",
    re.IGNORECASE,
)
# "alle/alles/all/komplett/ganz/everything" -> ganzen Channel leeren.
_ALL_RE = re.compile(r"\b(?:alles?|all|everything|komplett|ganz)\b", re.IGNORECASE)


def setup() -> bool:
    """Aktiviert das Moderation-Feature. Keine externen Voraussetzungen - das
    noetige Recht 'Nachrichten verwalten' wird erst beim Befehl geprueft."""
    global _enabled, _bot_name
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("MOD_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Moderation-Feature aus (MOD_ENABLED=0).")
        return False
    _enabled = True
    log.info("Moderation-Feature aktiv (Loeschen: bis %d oder 'alle').", MAX_PURGE)
    return True


def is_enabled() -> bool:
    return _enabled


def _keep(message: discord.Message) -> bool:
    """True = diese Nachricht NICHT loeschen (angepinnte schuetzen wir)."""
    return bool(message.pinned)


async def handle(message: discord.Message) -> "object | str | None":
    """Erkennt einen Loesch-Befehl und fuehrt ihn aus.

    Rueckgabe:
    - None     -> kein Loesch-Befehl (naechster Handler/KI ist dran).
    - str      -> Hinweis/Fehlertext, den bot.py normal als Antwort schickt.
    - HANDLED  -> schon erledigt + selbst geantwortet, bot.py sendet nichts mehr.
    """
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd or not _CMD_RE.match(cmd):
        return None

    # Ab hier ist es ein Loesch-Befehl -> wir uebernehmen (geben nie None zurueck,
    # damit nicht aus Versehen die KI das Wort "loeschen" beantwortet).
    rest = _CMD_RE.sub("", cmd, count=1).strip()

    author_perms = getattr(message.author, "guild_permissions", None)
    if author_perms is None or not author_perms.manage_messages:
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
            # Die Befehls-Nachricht war (sofern nicht angepinnt) mit dabei -> abziehen.
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
        message.author.display_name,
        getattr(channel, "name", channel.id),
        count,
        "alle" if want_all else f"max {num_match.group() if num_match else '?'}",
    )
    return HANDLED


async def _confirm(channel: discord.abc.Messageable, text: str) -> None:
    """Kurze Bestaetigung, die sich nach CONFIRM_TTL Sekunden selbst loescht."""
    try:
        await channel.send(text, delete_after=CONFIRM_TTL)
    except discord.HTTPException:
        pass
