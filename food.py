"""Kalorien-Analyse fuer Flo ("Kalorien-Channel").

Postet jemand im konfigurierten Channel ein Essensfoto, analysiert Flo es
automatisch (Vision-Modell, kostenlos ueber Groq): geschaetzte Kalorien,
Eiweiss/Kohlenhydrate/Fett/Zucker und ein Natuerlichkeits-Score - je
natuerlicher/unverarbeiteter, desto besser; je mehr Industrie, desto
schlechter. Ergebnis kommt als gerenderte Ernaehrungs-Karte (render.py).

Funktioniert auch per Befehl ueberall: "Flo kalorien" mit angehaengtem Bild
(oder als Antwort auf ein Bild).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re

import aiohttp
import discord

import ai
import render

log = logging.getLogger("dcbot.food")

HANDLED = object()

_enabled: bool = False
_bot_name: str = "Flo"

# In DIESEM Channel wird jedes gepostete Essensbild automatisch analysiert.
CHANNEL_ID = int(os.getenv("KALORIEN_CHANNEL_ID", "1522294725116428329") or "0")

# Expliziter Befehl (funktioniert in jedem Channel, Bild angehaengt/als Reply).
_CMD_RE = re.compile(r"^(?:kalorien|kcal|n(?:ae|ä)hrwerte?|makros?)\b", re.I)

# Nie mehr als 2 Analysen gleichzeitig (schont das kostenlose Vision-Limit).
_sem = asyncio.Semaphore(2)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

_PROMPT = (
    "Du bist ein praeziser Ernaehrungsberater. Analysiere das ESSEN auf dem Foto "
    "und antworte NUR mit einem JSON-Objekt (kein Text drumherum, keine "
    "Markdown-Zaeune) mit GENAU diesen Feldern:\n"
    '{"is_food": true/false, "gericht": "kurzer deutscher Name", '
    '"kcal": geschaetzte Gesamt-Kalorien der gezeigten Portion (Zahl), '
    '"kcal_min": untere Schaetzung, "kcal_max": obere Schaetzung, '
    '"protein_g": Gramm Eiweiss, "carbs_g": Gramm Kohlenhydrate, '
    '"fett_g": Gramm Fett, "zucker_g": Gramm Zucker, '
    '"natur_score": 0-10 (10 = komplett natuerlich/unverarbeitet wie Obst, '
    "Gemuese, frisches Fleisch; 0 = hochindustriell wie Chips, Softdrinks, "
    'Fertiggerichte - je weniger Industrie, desto hoeher), '
    '"verarbeitung": "kurzes Label, z. B. unverarbeitet / frisch gekocht / '
    'verarbeitet / hochindustriell", '
    '"fazit": "1 kurzer Satz: gut oder schlecht fuer den Koerper und warum", '
    '"flo_spruch": "1 kurzer, frecher, lustiger Kommentar zum Essen auf Deutsch"}\n'
    "Ist auf dem Foto KEIN Essen, setze is_food auf false und schreibe in "
    "flo_spruch einen kurzen frechen Spruch, was stattdessen zu sehen ist. "
    "Schaetze realistisch fuer die GEZEIGTE Portionsgroesse."
)


def setup() -> bool:
    """Aktiviert die Kalorien-Analyse (braucht die KI/Vision)."""
    global _enabled, _bot_name
    _bot_name = ai.bot_name()
    if not ai.is_enabled():
        log.info("Kalorien-Feature aus (KI nicht aktiv).")
        return False
    _enabled = True
    log.info("Kalorien-Feature aktiv (Auto-Channel: %s).", CHANNEL_ID or "-")
    return _enabled


def is_enabled() -> bool:
    return _enabled


def _image_of(message: discord.Message) -> "discord.Attachment | None":
    for att in message.attachments:
        ct = (att.content_type or "").lower()
        if ct.startswith("image/") or att.filename.lower().endswith(_IMAGE_EXTS):
            return att
    return None


def _parse_json(text: str) -> "dict | None":
    """Zieht das erste JSON-Objekt aus der Antwort (auch wenn Zaeune/Text
    drumherum stehen) und parst es tolerant."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return _sanitize(data) if isinstance(data, dict) else None


_NUM_FIELDS = ("kcal", "kcal_min", "kcal_max", "protein_g", "carbs_g",
               "fett_g", "zucker_g", "natur_score")


def _num(val) -> float:
    """Macht aus LLM-Werten robuste Zahlen: 1200, "1200", "ca. 1200 kcal",
    "8/10" -> erste Zahl; alles andere -> 0."""
    if isinstance(val, (int, float)):
        return float(val)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(val or ""))
    return float(m.group(0).replace(",", ".")) if m else 0.0


def _sanitize(data: dict) -> dict:
    """Zahlenfelder hart normalisieren, damit der Renderer nie an Strings
    wie '8/10' oder 'ca. 500' scheitert."""
    for k in _NUM_FIELDS:
        data[k] = _num(data.get(k))
    data["natur_score"] = max(0.0, min(10.0, data["natur_score"]))
    return data


async def _analyze(att: discord.Attachment) -> "dict | None":
    raw = await ai.see_image_raw(_PROMPT, att.url, temperature=0.3, max_tokens=500)
    return _parse_json(raw or "")


async def _download(att: discord.Attachment) -> "bytes | None":
    """Laedt das Foto fuer die Karte (Groessen-Deckel, geteilte Session)."""
    if att.size and att.size > 12_000_000:
        return None
    try:
        async with ai.http_session().get(
            att.url, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.read()
    except Exception:  # noqa: BLE001
        return None


async def _deliver(message: discord.Message, *, content: "str | None" = None,
                   embed: "discord.Embed | None" = None,
                   buf: "io.BytesIO | None" = None) -> bool:
    """Stellt die Antwort robust zu: erst als Reply, und falls das scheitert
    (Ursprungsnachricht geloescht, Reply-Rechte) direkt in den Channel."""
    senders = (lambda **kw: message.reply(mention_author=False, **kw),
               message.channel.send)
    for send in senders:
        try:
            kw: dict = {}
            if content:
                kw["content"] = content
            if embed is not None:
                kw["embed"] = embed
            if buf is not None:
                buf.seek(0)   # Datei je Versuch frisch aufsetzen
                kw["file"] = discord.File(buf, "flo_kalorien.png")
            await send(**kw)
            return True
        except discord.HTTPException:
            continue
    log.warning("Kalorien-Antwort nicht sendbar (Rechte im Channel? Datei zu gross?)")
    return False


async def _respond(message: discord.Message, att: discord.Attachment) -> None:
    """Analysiert das Bild und antwortet mit der Ernaehrungs-Karte."""
    async with _sem:
        try:
            async with message.channel.typing():
                # Hartes Zeitlimit: ein haengender Vision-Call darf den
                # Semaphor nicht dauerhaft blockieren.
                data, photo = await asyncio.wait_for(
                    asyncio.gather(_analyze(att), _download(att)), timeout=75)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            log.exception("Kalorien-Analyse fehlgeschlagen")
            data, photo = None, None
    if data is None:
        await _deliver(message, content="Das konnte ich gerade nicht analysieren - "
                                        "probier's gleich nochmal.")
        return
    if not data.get("is_food", False):
        spruch = str(data.get("flo_spruch") or "Das ist kein Essen, Digga.")[:300]
        await _deliver(message, content=f"🍽️ {spruch}")
        return
    sent = False
    try:
        buf = render.nutrition_card(photo, data)
        sent = await _deliver(message, buf=buf)
    except Exception:  # noqa: BLE001 - Render-Fehler -> Embed-Fallback
        log.exception("Kalorien-Karte fehlgeschlagen - nutze Embed")
    if not sent:
        # Karte kam nicht durch (Render ODER Senden) -> wenigstens das Embed.
        await _deliver(message, embed=_fallback_embed(data, att.url))


def _fallback_embed(data: dict, image_url: str) -> discord.Embed:
    # _num statt float()/int(): auch hier duerfen LLM-Ausreisser nie crashen.
    score = max(0.0, min(10.0, _num(data.get("natur_score"))))
    color = 0x2ECC71 if score >= 7 else (0xF1C40F if score >= 4 else 0xE74C3C)
    emb = discord.Embed(
        title=f"🍎 {str(data.get('gericht') or 'Essen')[:80]}",
        description=str(data.get("fazit") or "")[:300],
        color=color,
    )
    emb.add_field(name="Kalorien", value=f"≈ **{int(_num(data.get('kcal')))} kcal**")
    emb.add_field(name="Eiweiß", value=f"{_num(data.get('protein_g')):g} g")
    emb.add_field(name="Natürlichkeit", value=f"{score:g}/10")
    emb.set_thumbnail(url=image_url)
    spruch = str(data.get("flo_spruch") or "")[:200]
    if spruch:
        emb.set_footer(text=spruch)
    return emb


async def on_message_passive(message: discord.Message) -> None:
    """bot.py ruft das fuer jede Nachricht auf: Bild im Kalorien-Channel ->
    automatisch analysieren (ohne dass man Flo ansprechen muss)."""
    if not _enabled or CHANNEL_ID == 0 or message.channel.id != CHANNEL_ID:
        return
    att = _image_of(message)
    if att is not None:
        await _respond(message, att)


async def handle(message: discord.Message):
    """Expliziter Befehl: 'Flo kalorien' mit Bild (angehaengt oder als Reply)."""
    if not _enabled or message.guild is None:
        return None
    if not _CMD_RE.match(ai.strip_lead(message.content or "")):
        return None
    # Im Kalorien-Channel analysiert schon der passive Hook jedes Bild -
    # hier nicht doppelt antworten.
    if CHANNEL_ID and message.channel.id == CHANNEL_ID:
        return HANDLED
    att = _image_of(message)
    if att is None and message.reference is not None:
        ref = message.reference.resolved
        if isinstance(ref, discord.Message):
            att = _image_of(ref)
    if att is None:
        return (f"Haeng ein Foto von deinem Essen an (oder antworte auf eins) - "
                f"dann sag ich dir Kalorien & Naehrwerte. z. B. `{_bot_name} kalorien` + Bild.")
    await _respond(message, att)
    return HANDLED
