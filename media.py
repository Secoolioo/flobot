"""Bild-Features fuer Flo.

- ``Flo male <prompt>``  -> generiert ein Bild (Pollinations.ai, kostenlos, kein Key).
- ``Flo quote <spruch>`` -> Quote-Meme: Profilbild mit Verlauf ins Schwarze + Zitat
  (auch als Antwort auf eine Nachricht: zitiert dann deren Text + Autor).

Das eigentliche Bild-Lesen (Vision) laeuft ueber ai.see_image() und wird von
bot.py beim KI-Fallback aufgerufen, wenn eine Nachricht ein Bild enthaelt.
"""

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


class Media:
    """Bild-Features (Bild generieren + Quote-Meme) als Objekt gekapselt."""

    # bot.py erkennt das: das Modul hat selbst geantwortet (Bild gesendet).
    HANDLED = object()

    # Pollinations: kostenlose Bildgenerierung ohne API-Key.
    _POLLI = ("https://image.pollinations.ai/prompt/{p}"
              "?width=1024&height=1024&nologo=true&model=flux")

    # Befehle. "male/zeichne/generiere/bild/img <prompt>" bzw. "quote/zitat/meme <text>".
    # ('spruch' bewusst NICHT hier - das ist der Spruch-des-Tages-Befehl aus fun.py,
    # der sonst von media verschluckt wuerde.)
    _GEN_RE = re.compile(r"^(?:male|zeichne|generier\w*|bild|img)\s+(.+)", re.I | re.S)
    _QUOTE_RE = re.compile(r"^(?:quote|zitat|meme)\b\s*(.*)", re.I | re.S)

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"

    def setup(self):
        """Aktiviert das Media-Feature (braucht nur Pillow + Internet)."""
        self._bot_name = ai.bot_name()
        self._enabled = True
        log.info("Media-Feature aktiv (Bild generieren + Quote-Meme).")
        return self._enabled

    def is_enabled(self):
        return self._enabled

    def _clean_lead(self, text):
        return ai.strip_lead(text)

    async def handle(self, message):
        """Erkennt einen Bild-Befehl. Rueckgabe: HANDLED (selbst gesendet),
        Text/Embed (Hinweis) oder None (kein Bild-Befehl -> naechstes Modul/KI)."""
        if not self._enabled or message.guild is None:
            return None
        cleaned = self._clean_lead(message.content or "")
        if not cleaned:
            return None
        m = self._GEN_RE.match(cleaned)
        if m:
            return await self._cmd_generate(message, m.group(1).strip())
        m = self._QUOTE_RE.match(cleaned)
        if m:
            return await self._cmd_quote(message, m.group(1).strip())
        return None

    # --- Bild generieren -----------------------------------------------------
    async def generate_image(self, prompt):
        """Holt ein generiertes Bild von Pollinations (kostenlos) und gibt saubere
        PNG-Bytes zurueck. None bei Fehler/Timeout."""
        url = self._POLLI.format(p=urllib.parse.quote(prompt[:400]))
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

    async def _cmd_generate(self, message, prompt):
        if not prompt:
            return f"Was soll ich malen? z. B. `{self._bot_name} male einen Drachen aus Neon`."
        data = await self.generate_image(prompt)
        if not data:
            return "Der Bild-Dienst zickt gerade - probier's gleich nochmal."
        emb = discord.Embed(description=f"🎨  **{prompt[:230]}**", color=discord.Color.purple())
        emb.set_image(url="attachment://flo_bild.png")
        emb.set_footer(text=f"für {message.author.display_name} · generiert von {self._bot_name}")
        try:
            await message.reply(embed=emb,
                                file=discord.File(io.BytesIO(data), "flo_bild.png"),
                                mention_author=False)
        except discord.HTTPException:
            log.exception("Bild senden fehlgeschlagen")
            return "Konnte das Bild gerade nicht senden."
        return self.HANDLED

    # --- Quote-Meme ----------------------------------------------------------
    async def _fetch_avatar(self, user):
        try:
            return await asyncio.wait_for(user.display_avatar.with_size(256).read(), timeout=8)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko
            return None

    async def _cmd_quote(self, message, text):
        target = message.author
        # Mention-Reste und umschliessende Anfuehrungszeichen aufraeumen
        # (die Karte setzt selbst typografische Anfuehrungszeichen).
        quote = re.sub(r"<@!?\d+>", " ", text).strip().strip('"„“”\'').strip()
        # `flo quote @wer <text>` -> das Zitat der ERWAEHNTEN Person in den Mund legen.
        erwaehnt = next((m for m in message.mentions if not m.bot), None)
        if erwaehnt is not None:
            target = erwaehnt
        # Als Antwort auf eine Nachricht ohne eigenen Text -> deren Inhalt + Autor zitieren.
        ref = message.reference.resolved if message.reference is not None else None
        if isinstance(ref, discord.Message) and not quote:
            quote = (ref.content or "").strip()
            if erwaehnt is None:
                target = ref.author
        if not quote:
            return (f"Was soll das Zitat sein? z. B. `{self._bot_name} quote Pizza ist Leben`, "
                    f"`{self._bot_name} quote @wer Ich liebe Montage` - oder antworte mit "
                    f"`{self._bot_name} quote` auf eine Nachricht.")
        avatar = await self._fetch_avatar(target)
        name = getattr(target, "display_name", None) or "Unbekannt"
        try:
            # Im Thread rendern: quote_card jagt den ganzen Text durch die
            # Glyphen-Pruefung und zeichnet auf grossem Canvas - nicht im Loop.
            buf = await asyncio.to_thread(render.quote_card, avatar, quote, name)
        except Exception:  # noqa: BLE001
            log.exception("Quote-Render fehlgeschlagen")
            return "Das Zitat-Bild ist gerade abgestürzt."
        try:
            await message.reply(file=discord.File(buf, "flo_quote.png"), mention_author=False)
        except discord.HTTPException:
            log.exception("Quote senden fehlgeschlagen")
            return "Konnte das Zitat-Bild nicht senden."
        return self.HANDLED


instance = Media()

# Modul-Aliase, damit bot.py & Co. weiter media.<name> nutzen koennen.
HANDLED = Media.HANDLED
setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
generate_image = instance.generate_image
