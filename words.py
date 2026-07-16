"""Wort-Zaehler fuer Flo: zaehlt passiv JEDES Wort auf dem Server.

Befehle (nach 'Flo'):
- woerter <wort>   Wie oft wurde <wort> hier schon gesagt? (+ Rang & Top-Sager)
- woerter          Top-15 der meistgesagten Woerter als Bild

Passiv: note_message() wird von bot.py fuer jede Nicht-Bot-Nachricht gerufen
(reine dict-Operationen, kein await noetig). Beim ersten Start liest ein
Backfill einmalig die komplette Channel-History ein (mit Checkpoints,
neustart-sicher), damit "schon gesagt" wirklich die ganze Server-Geschichte
meint - nicht erst ab heute.

Persistenz: data/words.json ueber JsonStore. Gespeichert wird DEBOUNCED
(alle FLUSH_SECONDS, nur wenn sich etwas geaendert hat) - so schreibt der Bot
nicht bei jeder Chat-Nachricht auf die Platte.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import re

import discord

import ai
import render
from store import JsonStore

log = logging.getLogger("dcbot.words")

# Sentinel: words hat selbst geantwortet (Bild) -> bot.py schweigt.
HANDLED = object()

_enabled: bool = False
_bot_name: str = "Flo"
_store: JsonStore | None = None

# Debounce fuers Speichern: words.json waechst mit dem Server - nicht bei jeder
# Nachricht schreiben, sondern gesammelt.
FLUSH_SECONDS = float(os.getenv("WORDS_FLUSH_SECONDS", "60"))
# Einmaliger History-Backfill beim ersten Start (per .env abschaltbar).
BACKFILL = os.getenv("WORDS_BACKFILL", "1").strip().lower() not in ("0", "false", "no", "off")
# Nach so vielen eingelesenen Nachrichten: Checkpoint speichern + kurz Luft holen.
_BACKFILL_BATCH = 2000

_dirty: bool = False
_flush_task: asyncio.Task | None = None
_backfill_running: bool = False
# Waehrend json.dumps im Thread laeuft, darf NIEMAND das words-dict anfassen
# (sonst 'dictionary changed size during iteration'). Neue Nachrichten landen
# solange im _backlog und werden direkt nach dem Speichern nachgezaehlt.
_saving: bool = False
_backlog: list[tuple[str, str]] = []

_ALIASES = ("wörter", "woerter", "wort", "worte", "wortzähler", "wortzaehler",
            "words", "word", "wordcount")

# Woerter: nur Buchstaben (inkl. Umlaute/ß), 2-32 Zeichen, kleingeschrieben.
_WORD_RE = re.compile(r"[a-zäöüß]{2,32}")
_URL_RE = re.compile(r"https?://\S+")
# Custom-Emojis <a:name:id> sowie Mentions/Channel/Rollen <@123> <#123> <@&123>
_MARKUP_RE = re.compile(r"<a?:\w+:\d+>|<[@#][&!]?\d+>")


def setup() -> bool:
    """Aktiviert den Wort-Zaehler und laedt data/words.json."""
    global _enabled, _bot_name, _store
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("WORDS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Wort-Zaehler aus (WORDS_ENABLED=0).")
        return False
    _store = JsonStore("words.json", default={
        "words": {}, "total": 0, "msgs": 0,
        "scan": {"before": 0, "done": False, "channels": {}},
    })
    # Backfill-Obergrenze SOFORT einfrieren (vor der allerersten Nachricht):
    # alles vor diesem Snowflake liest der Backfill, alles danach zaehlt der
    # Live-Hook - so gibt es kein Doppelzaehl-Fenster beim ersten Start.
    scan = _store.data.setdefault("scan", {"before": 0, "done": False, "channels": {}})
    if BACKFILL and not scan.get("done") and not scan.get("before"):
        scan["before"] = discord.utils.time_snowflake(discord.utils.utcnow())
    _enabled = True
    log.info("Wort-Zaehler aktiv (%d Woerter erfasst, Backfill: %s).",
             len(_store.data.get("words", {})), "an" if BACKFILL else "aus")
    return True


def is_enabled() -> bool:
    return _enabled


# --- Zaehlen ---------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    """Zerlegt eine Nachricht in zaehlbare Woerter: URLs/Mentions/Custom-Emojis
    raus, alles klein, nur Buchstaben-Woerter mit 2-32 Zeichen."""
    text = _URL_RE.sub(" ", text or "")
    text = _MARKUP_RE.sub(" ", text)
    return _WORD_RE.findall(text.lower())


def _count_text(text: str, uid: str) -> int:
    """Zaehlt alle Woerter einer Nachricht. Gibt die Anzahl gezaehlter Woerter
    zurueck. Reine dict-Arbeit - bewusst synchron (Mikrosekunden)."""
    assert _store is not None
    tokens = _tokenize(text)
    if not tokens:
        return 0
    words = _store.data.setdefault("words", {})
    for tok in tokens:
        entry = words.get(tok)
        if entry is None:
            entry = words[tok] = {"n": 0, "u": {}}
        entry["n"] += 1
        entry["u"][uid] = entry["u"].get(uid, 0) + 1
    _store.data["total"] = _store.data.get("total", 0) + len(tokens)
    _store.data["msgs"] = _store.data.get("msgs", 0) + 1
    return len(tokens)


def _count_guarded(text: str, uid: str) -> None:
    """Zaehlt sofort - oder puffert, falls gerade gespeichert wird."""
    if _saving:
        _backlog.append((text, uid))
    else:
        _count_text(text, uid)


def note_message(message: discord.Message) -> None:
    """Passiver Hook: bot.py ruft das fuer jede Nicht-Bot-Guild-Nachricht auf.
    Synchron und billig - das Speichern passiert gesammelt im Hintergrund."""
    if not _enabled:
        return
    _count_guarded(message.content or "", str(message.author.id))
    _mark_dirty()


def _mark_dirty() -> None:
    """Merkt sich 'es gibt Ungespeichertes' und sorgt fuer einen (einzigen)
    Hintergrund-Task, der debounced auf die Platte schreibt."""
    global _dirty, _flush_task
    _dirty = True
    if _flush_task is None or _flush_task.done():
        try:
            _flush_task = asyncio.create_task(_flush_later())
        except RuntimeError:
            pass  # kein laufender Event-Loop (Tests) - naechster Aufruf probiert's neu


async def _save_store() -> None:
    """Speichert words.json, OHNE den Event-Loop zu blockieren: json.dumps
    laeuft (anders als beim Standard-JsonStore) im Thread - das lohnt sich,
    weil der Wort-Index mit dem Server waechst. Waehrenddessen setzt _saving
    neue Zaehlungen auf den _backlog; sie werden danach nachgeholt."""
    global _saving
    assert _store is not None
    async with _store._lock:
        _saving = True
        try:
            payload = await asyncio.to_thread(
                json.dumps, _store.data, ensure_ascii=False, separators=(",", ":"))
            await asyncio.to_thread(_store._write_text, payload)
        finally:
            _saving = False
            _replay_backlog()


def _replay_backlog() -> None:
    """Zaehlt Nachrichten nach, die waehrend eines Speicher-/Sortier-Laufs
    aufgelaufen sind."""
    if _backlog:
        pending, _backlog[:] = list(_backlog), []
        for text, uid in pending:
            _count_text(text, uid)


async def _flush_later() -> None:
    global _dirty
    try:
        while _dirty:
            _dirty = False
            await asyncio.sleep(FLUSH_SECONDS)
            if _backfill_running:
                # Waehrend des Backfills speichert NUR der Backfill selbst
                # (seine Checkpoints sichern auch die Live-Zaehlungen mit) -
                # sonst koennte sein Iterieren mitten in unser dumps fallen.
                _dirty = True
                continue
            await _save_store()
    except Exception:
        log.exception("Wort-Zaehler: Speichern fehlgeschlagen")


async def flush_now() -> None:
    """Ungespeicherte Zaehlungen sofort sichern (bot.py ruft das z. B. vor
    einem Neustart, damit keine Minute Zaehlung verloren geht)."""
    await _flush_now()


async def _flush_now() -> None:
    """Sofort speichern (vor Abfragen), damit die Zahlen frisch sind."""
    global _dirty
    if _store is None or not _dirty or _backfill_running:
        return
    _dirty = False
    try:
        await _save_store()
    except Exception:
        log.exception("Wort-Zaehler: Sofort-Speichern fehlgeschlagen")


# --- Einmaliger History-Backfill --------------------------------------------
def is_scanning() -> bool:
    """True, solange der einmalige History-Einleser noch nicht durch ist."""
    if not _enabled or _store is None or not BACKFILL:
        return False
    return not _store.data.get("scan", {}).get("done", False)


async def backfill(guild: discord.Guild) -> None:
    """Liest einmalig die komplette Channel-History ein (nur beim ersten Start;
    neustart-sicher per Checkpoint je Channel). Laeuft gemuetlich im Hintergrund
    und schont die Discord-API (Pause je _BACKFILL_BATCH Nachrichten).

    Doppel-Zaehl-Schutz: beim ersten Start wird ein Zeitstempel-Snowflake
    ('before') eingefroren - der Backfill liest nur Nachrichten DAVOR, das
    Live-Zaehlen uebernimmt alles danach."""
    global _backfill_running
    if not _enabled or _store is None or not BACKFILL or _backfill_running:
        return
    scan = _store.data.setdefault("scan", {"before": 0, "done": False, "channels": {}})
    if scan.get("done"):
        return
    _backfill_running = True
    try:
        if not scan.get("before"):   # Sicherheitsnetz - setup() stempelt normal schon
            scan["before"] = discord.utils.time_snowflake(discord.utils.utcnow())
            await _save_store()
        before_obj = discord.Object(id=int(scan["before"]))
        log.info("Wort-Zaehler: History-Backfill startet (%d Channels) ...",
                 len(guild.text_channels))
        total = 0
        all_ok = True
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            key = str(channel.id)
            state = scan["channels"].get(key)
            if state == "done":
                continue
            after_obj = discord.Object(id=int(state)) if state else None
            try:
                batch = 0
                async for msg in channel.history(limit=None, after=after_obj,
                                                 before=before_obj, oldest_first=True):
                    if not msg.author.bot:
                        _count_guarded(msg.content or "", str(msg.author.id))
                    scan["channels"][key] = msg.id   # Checkpoint (aufsteigend)
                    total += 1
                    batch += 1
                    if batch % _BACKFILL_BATCH == 0:
                        await _save_store()
                        await asyncio.sleep(1.0)     # API & CPU schonen
                scan["channels"][key] = "done"
                await _save_store()
                log.info("Wort-Zaehler: #%s eingelesen.", channel.name)
            except discord.Forbidden:
                scan["channels"][key] = "done"       # kein Zugriff -> ueberspringen
                await _save_store()
            except discord.HTTPException as exc:
                all_ok = False                        # naechster Start macht weiter
                log.warning("Wort-Zaehler: Backfill in #%s unterbrochen: %s",
                            channel.name, exc)
                await _save_store()
        if all_ok:
            scan["done"] = True
            await _save_store()
            log.info("Wort-Zaehler: Backfill fertig - %d Nachrichten gelesen, "
                     "%d Woerter im Index.", total, len(_store.data.get("words", {})))
    except Exception:
        log.exception("Wort-Zaehler: Backfill-Fehler (naechster Start macht weiter)")
    finally:
        _backfill_running = False


# --- Befehle ----------------------------------------------------------------
async def _send(message: discord.Message, *, embed=None, file=None):
    kwargs = {"mention_author": False}
    if embed is not None:
        kwargs["embed"] = embed
    if file is not None:
        kwargs["file"] = file
    try:
        return await message.reply(**kwargs)
    except discord.HTTPException:
        log.exception("Wort-Zaehler: Antwort konnte nicht gesendet werden")
        return None


def _scan_hint() -> str:
    return ("\n⏳ *Ich lese gerade noch alte Nachrichten ein – die Zahlen "
            "wachsen eventuell noch.*" if is_scanning() else "")


async def handle(message: discord.Message) -> "str | object | None":
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.split()
    first = parts[0].lower().strip(".,;:!?")
    if first not in _ALIASES:
        return None
    await _flush_now()
    args = parts[1:]
    if not args:
        return await _top_command(message)
    return await _word_query(message, " ".join(args))


async def _word_query(message: discord.Message, raw: str) -> "str | object":
    assert _store is not None
    tokens = _tokenize(raw)
    if not tokens:
        return (f"Gib mir ein echtes Wort – z. B. `{_bot_name} wörter pizza`. "
                "(Zahlen/Links zähle ich nicht.)")
    wort = tokens[0]
    words = _store.data.get("words", {})
    entry = words.get(wort)
    count = int(entry["n"]) if entry else 0

    if not entry:
        emb = discord.Embed(
            title=f"📊 „{wort}“",
            description=f"wurde auf diesem Server noch **nie** gesagt. "
                        f"Du könntest der/die Erste sein. 👀{_scan_hint()}",
            color=discord.Color.greyple())
        await _send(message, embed=emb)
        return HANDLED

    rank = 1 + sum(1 for e in words.values() if int(e["n"]) > count)
    if count >= 1000:
        color = discord.Color.gold()
    elif count >= 100:
        color = discord.Color.green()
    else:
        color = discord.Color.blurple()
    emb = discord.Embed(
        title=f"📊 „{wort}“",
        description=f"wurde auf diesem Server **{count}×** gesagt.{_scan_hint()}",
        color=color)
    emb.add_field(name="Rang", value=f"#{rank} von {len(words)} Wörtern", inline=True)

    # Top-Sager (bis zu 3, mit Medaillen)
    users = sorted((entry.get("u") or {}).items(), key=lambda kv: kv[1], reverse=True)
    if users:
        medaillen = ("🥇", "🥈", "🥉")
        zeilen = []
        for i, (uid, n) in enumerate(users[:3]):
            member = message.guild.get_member(int(uid))
            name = member.display_name if member else "Unbekannt"
            zeilen.append(f"{medaillen[i]} **{name}** ({n}×)")
        emb.add_field(name="Top-Sager", value="\n".join(zeilen), inline=True)
    emb.set_footer(text=f"{_bot_name} zählt seit dem ersten Server-Tag mit. 🧮")
    await _send(message, embed=emb)
    return HANDLED


def _build_top(words_dict: dict, total: int) -> tuple[list, "object"]:
    """Sortiert + rendert die Top-Liste (laeuft im Thread - der Wortschatz
    kann gross sein). Der Aufrufer friert waehrenddessen das dict ein."""
    rows = sorted(words_dict.items(), key=lambda kv: int(kv[1]["n"]), reverse=True)[:15]
    buf = render.words_card([(w, int(e["n"])) for w, e in rows],
                            total_words=len(words_dict), total_count=total)
    return rows, buf


async def _top_command(message: discord.Message) -> object:
    global _saving
    assert _store is not None
    words_dict = _store.data.get("words", {})
    if not words_dict:
        emb = discord.Embed(
            title="📊 Flo Wörter",
            description=f"Noch nichts gezählt.{_scan_hint()}",
            color=discord.Color.greyple())
        await _send(message, embed=emb)
        return HANDLED
    # Einfrieren wie beim Speichern: sortiert/rendert im Thread, neue
    # Nachrichten laufen solange in den Backlog. Der Store-Lock stellt sicher,
    # dass wir nicht parallel zu einem laufenden _save_store am dict arbeiten
    # (dessen finally wuerde sonst _saving zu frueh zuruecksetzen).
    async with _store._lock:
        _saving = True
        try:
            rows, buf = await asyncio.to_thread(
                _build_top, words_dict, int(_store.data.get("total", 0)))
        except Exception:
            log.exception("Woerter-Karte fehlgeschlagen - Text-Fallback")
            rows, buf = [], None
        finally:
            _saving = False
            _replay_backlog()
    if buf is not None:
        emb = discord.Embed(
            title="📊 Die meistgesagten Wörter",
            description=f"Frag ein bestimmtes Wort ab: `{_bot_name} wörter <wort>`"
                        f"{_scan_hint()}",
            color=discord.Color.blurple())
        emb.set_image(url="attachment://flo_woerter.png")
        await _send(message, embed=emb,
                    file=discord.File(buf, filename="flo_woerter.png"))
        return HANDLED
    top = heapq.nlargest(10, words_dict.items(), key=lambda kv: int(kv[1]["n"]))
    zeilen = [f"**{i + 1}.** {w} – {int(e['n'])}×" for i, (w, e) in enumerate(top)]
    emb = discord.Embed(title="📊 Die meistgesagten Wörter",
                        description="\n".join(zeilen) + _scan_hint(),
                        color=discord.Color.blurple())
    await _send(message, embed=emb)
    return HANDLED
