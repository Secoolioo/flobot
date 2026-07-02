"""Bild-Features fuer Flo.

- ``Flo male <prompt>``  -> generiert ein Bild (Pollinations.ai, kostenlos, kein Key).
- ``Flo quote <spruch>`` -> Quote-Meme: Profilbild mit Verlauf ins Schwarze + Zitat
  (auch als Antwort auf eine Nachricht: zitiert dann deren Text + Autor).

Das eigentliche Bild-Lesen (Vision) laeuft ueber ai.see_image() und wird von
bot.py beim KI-Fallback aufgerufen, wenn eine Nachricht ein Bild enthaelt.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import urllib.parse

import aiohttp
import discord

import ai
import render

log = logging.getLogger("dcbot.media")

# bot.py erkennt das: das Modul hat selbst geantwortet (Bild gesendet).
HANDLED = object()

_enabled: bool = False
_bot_name: str = "Flo"

# Pollinations: kostenlose Bildgenerierung ohne API-Key.
_POLLI = ("https://image.pollinations.ai/prompt/{p}"
          "?width=1024&height=1024&nologo=true&model=flux")

# Befehle. "male/zeichne/generiere/bild/img <prompt>" bzw. "quote/zitat/meme/spruch <text>".
_GEN_RE = re.compile(r"^(?:male|zeichne|generier\w*|bild|img)\s+(.+)", re.I | re.S)
_QUOTE_RE = re.compile(r"^(?:quote|zitat|meme|spruch)\b\s*(.*)", re.I | re.S)


def setup() -> bool:
    """Aktiviert das Media-Feature (braucht nur Pillow + Internet)."""
    global _enabled, _bot_name
    _bot_name = ai.bot_name()
    _enabled = True
    log.info("Media-Feature aktiv (Bild generieren + Quote-Meme).")
    return _enabled


def is_enabled() -> bool:
    return _enabled


def _clean_lead(text: str) -> str:
    return ai.strip_lead(text)


async def handle(message: discord.Message):
    """Erkennt einen Bild-Befehl. Rueckgabe: HANDLED (selbst gesendet),
    Text/Embed (Hinweis) oder None (kein Bild-Befehl -> naechstes Modul/KI)."""
    if not _enabled or message.guild is None:
        return None
    cleaned = _clean_lead(message.content or "")
    if not cleaned:
        return None
    m = _GEN_RE.match(cleaned)
    if m:
        return await _cmd_generate(message, m.group(1).strip())
    m = _QUOTE_RE.match(cleaned)
    if m:
        return await _cmd_quote(message, m.group(1).strip())
    return None


# --- Bild generieren -----------------------------------------------------
async def generate_image(prompt: str) -> "bytes | None":
    """Holt ein generiertes Bild von Pollinations (kostenlos) und gibt saubere
    PNG-Bytes zurueck. None bei Fehler/Timeout."""
    url = _POLLI.format(p=urllib.parse.quote(prompt[:400]))
    try:
        # geteilte Session aus ai (Keep-Alive) statt neuer pro Bild
        async with ai.http_session().get(
            url, timeout=aiohttp.ClientTimeout(total=75)
        ) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
    except Exception:  # noqa: BLE001 - Netzfehler darf den Bot nie crashen
        log.exception("Bildgenerierung fehlgeschlagen")
        return None
    if not raw:
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        log.exception("Generiertes Bild nicht lesbar")
        return None


async def _cmd_generate(message: discord.Message, prompt: str):
    if not prompt:
        return f"Was soll ich malen? z. B. `{_bot_name} male einen Drachen aus Neon`."
    data = await generate_image(prompt)
    if not data:
        return "Der Bild-Dienst zickt gerade - probier's gleich nochmal."
    emb = discord.Embed(description=f"🎨  **{prompt[:230]}**", color=discord.Color.purple())
    emb.set_image(url="attachment://flo_bild.png")
    emb.set_footer(text=f"für {message.author.display_name} · generiert von {_bot_name}")
    try:
        await message.reply(embed=emb,
                            file=discord.File(io.BytesIO(data), "flo_bild.png"),
                            mention_author=False)
    except discord.HTTPException:
        log.exception("Bild senden fehlgeschlagen")
        return "Konnte das Bild gerade nicht senden."
    return HANDLED


# --- Quote-Meme ----------------------------------------------------------
async def _fetch_avatar(user) -> "bytes | None":
    try:
        return await asyncio.wait_for(user.display_avatar.with_size(256).read(), timeout=8)
    except Exception:  # noqa: BLE001 - Avatar ist nur Deko
        return None


async def _cmd_quote(message: discord.Message, text: str):
    target = message.author
    quote = text
    # Als Antwort auf eine Nachricht ohne eigenen Text -> deren Inhalt + Autor zitieren.
    ref = message.reference.resolved if message.reference is not None else None
    if isinstance(ref, discord.Message) and not quote:
        quote = (ref.content or "").strip()
        target = ref.author
    if not quote:
        return (f"Was soll das Zitat sein? z. B. `{_bot_name} quote Pizza ist Leben` "
                f"- oder antworte mit `{_bot_name} quote` auf eine Nachricht.")
    avatar = await _fetch_avatar(target)
    name = getattr(target, "display_name", None) or "Unbekannt"
    try:
        buf = render.quote_card(avatar, quote, name)
    except Exception:  # noqa: BLE001
        log.exception("Quote-Render fehlgeschlagen")
        return "Das Zitat-Bild ist gerade abgestürzt."
    try:
        await message.reply(file=discord.File(buf, "flo_quote.png"), mention_author=False)
    except discord.HTTPException:
        log.exception("Quote senden fehlgeschlagen")
        return "Konnte das Zitat-Bild nicht senden."
    return HANDLED
