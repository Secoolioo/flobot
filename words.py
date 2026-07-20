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
import economy
import render
from store import JsonStore

log = logging.getLogger("dcbot.words")

# Sentinel: words hat selbst geantwortet (Bild) -> bot.py schweigt.
HANDLED = object()

# Debounce fuers Speichern: words.json waechst mit dem Server - nicht bei jeder
# Nachricht schreiben, sondern gesammelt.
FLUSH_SECONDS = float(os.getenv("WORDS_FLUSH_SECONDS", "60"))
# Einmaliger History-Backfill beim ersten Start (per .env abschaltbar).
BACKFILL = os.getenv("WORDS_BACKFILL", "1").strip().lower() not in ("0", "false", "no", "off")
# Nach so vielen eingelesenen Nachrichten: Checkpoint speichern + kurz Luft holen.
_BACKFILL_BATCH = 2000

_ALIASES = ("wörter", "woerter", "wort", "worte", "wortzähler", "wortzaehler",
            "words", "word", "wordcount")

# Woerter: nur Buchstaben (inkl. Umlaute/ß), 2-32 Zeichen, kleingeschrieben.
_WORD_RE = re.compile(r"[a-zäöüß]{2,32}")
_URL_RE = re.compile(r"https?://\S+")
# Custom-Emojis <a:name:id> sowie Mentions/Channel/Rollen <@123> <#123> <@&123>
_MARKUP_RE = re.compile(r"<a?:\w+:\d+>|<[@#][&!]?\d+>")


class Words:
    def __init__(self) -> None:
        self._enabled: bool = False
        self._bot_name: str = "Flo"
        self._store: JsonStore | None = None

        self._dirty: bool = False
        self._flush_task: asyncio.Task | None = None
        self._backfill_running: bool = False
        # Waehrend json.dumps im Thread laeuft, darf NIEMAND das words-dict anfassen
        # (sonst 'dictionary changed size during iteration'). Neue Nachrichten landen
        # solange im _backlog und werden direkt nach dem Speichern nachgezaehlt.
        self._saving: bool = False
        self._backlog: list[tuple[str, str]] = []

    def setup(self) -> bool:
        """Aktiviert den Wort-Zaehler und laedt data/words.json."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("WORDS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Wort-Zaehler aus (WORDS_ENABLED=0).")
            return False
        self._store = JsonStore("words.json", default={
            "words": {}, "total": 0, "msgs": 0,
            "scan": {"before": 0, "done": False, "channels": {}},
        })
        # Backfill-Obergrenze SOFORT einfrieren (vor der allerersten Nachricht):
        # alles vor diesem Snowflake liest der Backfill, alles danach zaehlt der
        # Live-Hook - so gibt es kein Doppelzaehl-Fenster beim ersten Start.
        scan = self._store.data.setdefault("scan", {"before": 0, "done": False, "channels": {}})
        if BACKFILL and not scan.get("done") and not scan.get("before"):
            scan["before"] = discord.utils.time_snowflake(discord.utils.utcnow())
        self._enabled = True
        log.info("Wort-Zaehler aktiv (%d Woerter erfasst, Backfill: %s).",
                 len(self._store.data.get("words", {})), "an" if BACKFILL else "aus")
        return True

    def is_enabled(self) -> bool:
        return self._enabled

    # --- Zaehlen ---------------------------------------------------------------
    def _tokenize(self, text: str) -> list[str]:
        """Zerlegt eine Nachricht in zaehlbare Woerter: URLs/Mentions/Custom-Emojis
        raus, alles klein, nur Buchstaben-Woerter mit 2-32 Zeichen."""
        text = _URL_RE.sub(" ", text or "")
        text = _MARKUP_RE.sub(" ", text)
        return _WORD_RE.findall(text.lower())

    def _count_text(self, text: str, uid: str) -> int:
        """Zaehlt alle Woerter einer Nachricht. Gibt die Anzahl gezaehlter Woerter
        zurueck. Reine dict-Arbeit - bewusst synchron (Mikrosekunden)."""
        assert self._store is not None
        tokens = self._tokenize(text)
        if not tokens:
            return 0
        words = self._store.data.setdefault("words", {})
        for tok in tokens:
            entry = words.get(tok)
            if entry is None:
                entry = words[tok] = {"n": 0, "u": {}}
            entry["n"] += 1
            entry["u"][uid] = entry["u"].get(uid, 0) + 1
        self._store.data["total"] = self._store.data.get("total", 0) + len(tokens)
        self._store.data["msgs"] = self._store.data.get("msgs", 0) + 1
        return len(tokens)

    def _count_guarded(self, text: str, uid: str) -> None:
        """Zaehlt sofort - oder puffert, falls gerade gespeichert wird."""
        if self._saving:
            self._backlog.append((text, uid))
        else:
            self._count_text(text, uid)

    def note_message(self, message: discord.Message) -> None:
        """Passiver Hook: bot.py ruft das fuer jede Nicht-Bot-Guild-Nachricht auf.
        Synchron und billig - das Speichern passiert gesammelt im Hintergrund."""
        if not self._enabled:
            return
        self._count_guarded(message.content or "", str(message.author.id))
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        """Merkt sich 'es gibt Ungespeichertes' und sorgt fuer einen (einzigen)
        Hintergrund-Task, der debounced auf die Platte schreibt."""
        self._dirty = True
        if self._flush_task is None or self._flush_task.done():
            try:
                self._flush_task = asyncio.create_task(self._flush_later())
            except RuntimeError:
                pass  # kein laufender Event-Loop (Tests) - naechster Aufruf probiert's neu

    async def _save_store(self) -> None:
        """Speichert words.json, OHNE den Event-Loop zu blockieren: json.dumps
        laeuft (anders als beim Standard-JsonStore) im Thread - das lohnt sich,
        weil der Wort-Index mit dem Server waechst. Waehrenddessen setzt _saving
        neue Zaehlungen auf den _backlog; sie werden danach nachgeholt."""
        assert self._store is not None
        async with self._store._lock:
            self._saving = True
            try:
                payload = await asyncio.to_thread(
                    json.dumps, self._store.data, ensure_ascii=False, separators=(",", ":"))
                await asyncio.to_thread(self._store._write_text, payload)
            finally:
                self._saving = False
                self._replay_backlog()

    def _replay_backlog(self) -> None:
        """Zaehlt Nachrichten nach, die waehrend eines Speicher-/Sortier-Laufs
        aufgelaufen sind."""
        if self._backlog:
            pending, self._backlog[:] = list(self._backlog), []
            for text, uid in pending:
                self._count_text(text, uid)

    async def _flush_later(self) -> None:
        try:
            while self._dirty:
                self._dirty = False
                await asyncio.sleep(FLUSH_SECONDS)
                if self._backfill_running:
                    # Waehrend des Backfills speichert NUR der Backfill selbst
                    # (seine Checkpoints sichern auch die Live-Zaehlungen mit) -
                    # sonst koennte sein Iterieren mitten in unser dumps fallen.
                    self._dirty = True
                    continue
                await self._save_store()
        except Exception:
            log.exception("Wort-Zaehler: Speichern fehlgeschlagen")

    async def flush_now(self) -> None:
        """Ungespeicherte Zaehlungen sofort sichern (bot.py ruft das z. B. vor
        einem Neustart, damit keine Minute Zaehlung verloren geht)."""
        await self._flush_now()

    async def _flush_now(self) -> None:
        """Sofort speichern (vor Abfragen), damit die Zahlen frisch sind."""
        if self._store is None or not self._dirty or self._backfill_running:
            return
        self._dirty = False
        try:
            await self._save_store()
        except Exception:
            log.exception("Wort-Zaehler: Sofort-Speichern fehlgeschlagen")

    # --- Einmaliger History-Backfill --------------------------------------------
    def is_scanning(self) -> bool:
        """True, solange der einmalige History-Einleser noch nicht durch ist."""
        if not self._enabled or self._store is None or not BACKFILL:
            return False
        return not self._store.data.get("scan", {}).get("done", False)

    async def backfill(self, guild: discord.Guild) -> None:
        """Liest einmalig die komplette Channel-History ein (nur beim ersten Start;
        neustart-sicher per Checkpoint je Channel). Laeuft gemuetlich im Hintergrund
        und schont die Discord-API (Pause je _BACKFILL_BATCH Nachrichten).

        Doppel-Zaehl-Schutz: beim ersten Start wird ein Zeitstempel-Snowflake
        ('before') eingefroren - der Backfill liest nur Nachrichten DAVOR, das
        Live-Zaehlen uebernimmt alles danach."""
        if not self._enabled or self._store is None or not BACKFILL or self._backfill_running:
            return
        scan = self._store.data.setdefault("scan", {"before": 0, "done": False, "channels": {}})
        if scan.get("done"):
            return
        self._backfill_running = True
        try:
            if not scan.get("before"):   # Sicherheitsnetz - setup() stempelt normal schon
                scan["before"] = discord.utils.time_snowflake(discord.utils.utcnow())
                await self._save_store()
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
                            self._count_guarded(msg.content or "", str(msg.author.id))
                        scan["channels"][key] = msg.id   # Checkpoint (aufsteigend)
                        total += 1
                        batch += 1
                        if batch % _BACKFILL_BATCH == 0:
                            await self._save_store()
                            await asyncio.sleep(1.0)     # API & CPU schonen
                    scan["channels"][key] = "done"
                    await self._save_store()
                    log.info("Wort-Zaehler: #%s eingelesen.", channel.name)
                except discord.Forbidden:
                    scan["channels"][key] = "done"       # kein Zugriff -> ueberspringen
                    await self._save_store()
                except discord.HTTPException as exc:
                    all_ok = False                        # naechster Start macht weiter
                    log.warning("Wort-Zaehler: Backfill in #%s unterbrochen: %s",
                                channel.name, exc)
                    await self._save_store()
            if all_ok:
                scan["done"] = True
                await self._save_store()
                log.info("Wort-Zaehler: Backfill fertig - %d Nachrichten gelesen, "
                         "%d Woerter im Index.", total, len(self._store.data.get("words", {})))
        except Exception:
            log.exception("Wort-Zaehler: Backfill-Fehler (naechster Start macht weiter)")
        finally:
            self._backfill_running = False

    # --- Befehle ----------------------------------------------------------------
    async def _send(self, message: discord.Message, *, embed=None, file=None):
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

    def _scan_hint(self) -> str:
        return ("\n⏳ *Ich lese gerade noch alte Nachrichten ein – die Zahlen "
                "wachsen eventuell noch.*" if self.is_scanning() else "")

    async def handle(self, message: discord.Message) -> "str | object | None":
        if not self._enabled or message.guild is None:
            return None
        cmd = ai.strip_lead(message.content or "")
        if not cmd:
            return None
        parts = cmd.split()
        first = parts[0].lower().strip(".,;:!?")
        if first not in _ALIASES:
            return None
        await self._flush_now()
        args = parts[1:]
        if not args:
            return await self._top_command(message)
        return await self._word_query(message, " ".join(args))

    async def _word_query(self, message: discord.Message, raw: str) -> "str | object":
        assert self._store is not None
        tokens = self._tokenize(raw)
        if not tokens:
            return (f"Gib mir ein echtes Wort – z. B. `{self._bot_name} wörter pizza`. "
                    "(Zahlen/Links zähle ich nicht.)")
        wort = tokens[0]
        words = self._store.data.get("words", {})
        entry = words.get(wort)
        count = int(entry["n"]) if entry else 0

        if not entry:
            emb = discord.Embed(
                title=f"📊 „{wort}“",
                description=f"wurde auf diesem Server noch **nie** gesagt. "
                            f"Du könntest der/die Erste sein. 👀{self._scan_hint()}",
                color=discord.Color.greyple())
            await self._send(message, embed=emb)
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
            description=f"wurde auf diesem Server **{count}×** gesagt.{self._scan_hint()}",
            color=color)
        emb.add_field(name="Rang", value=f"#{rank} von {len(words)} Wörtern", inline=True)

        # Top-Sager (bis zu 3, mit Medaillen)
        users = sorted((entry.get("u") or {}).items(), key=lambda kv: kv[1], reverse=True)
        if users:
            medaillen = ("🥇", "🥈", "🥉")
            zeilen = []
            for i, (uid, n) in enumerate(users[:3]):
                # get_member ist ohne Members-Intent unzuverlaessig -> Fallback
                # auf den Namens-Cache von economy (wird bei jeder Nachricht gepflegt).
                member = message.guild.get_member(int(uid))
                name = (member.display_name if member
                        else economy.display_name_of(int(uid)) or "Unbekannt")
                zeilen.append(f"{medaillen[i]} **{name}** ({n}×)")
            emb.add_field(name="Top-Sager", value="\n".join(zeilen), inline=True)
        emb.set_footer(text=f"{self._bot_name} zählt seit dem ersten Server-Tag mit. 🧮")
        await self._send(message, embed=emb)
        return HANDLED

    def _build_top(self, words_dict: dict, total: int) -> tuple[list, "object"]:
        """Sortiert + rendert die Top-Liste (laeuft im Thread - der Wortschatz
        kann gross sein). Der Aufrufer friert waehrenddessen das dict ein."""
        rows = sorted(words_dict.items(), key=lambda kv: int(kv[1]["n"]), reverse=True)[:15]
        buf = render.words_card([(w, int(e["n"])) for w, e in rows],
                                total_words=len(words_dict), total_count=total)
        return rows, buf

    async def _top_command(self, message: discord.Message) -> object:
        assert self._store is not None
        words_dict = self._store.data.get("words", {})
        if not words_dict:
            emb = discord.Embed(
                title="📊 Flo Wörter",
                description=f"Noch nichts gezählt.{self._scan_hint()}",
                color=discord.Color.greyple())
            await self._send(message, embed=emb)
            return HANDLED
        # Einfrieren wie beim Speichern: sortiert/rendert im Thread, neue
        # Nachrichten laufen solange in den Backlog. Der Store-Lock stellt sicher,
        # dass wir nicht parallel zu einem laufenden _save_store am dict arbeiten
        # (dessen finally wuerde sonst _saving zu frueh zuruecksetzen).
        async with self._store._lock:
            self._saving = True
            try:
                rows, buf = await asyncio.to_thread(
                    self._build_top, words_dict, int(self._store.data.get("total", 0)))
            except Exception:
                log.exception("Woerter-Karte fehlgeschlagen - Text-Fallback")
                rows, buf = [], None
            finally:
                self._saving = False
                self._replay_backlog()
        if buf is not None:
            emb = discord.Embed(
                title="📊 Die meistgesagten Wörter",
                description=f"Frag ein bestimmtes Wort ab: `{self._bot_name} wörter <wort>`"
                            f"{self._scan_hint()}",
                color=discord.Color.blurple())
            emb.set_image(url="attachment://flo_woerter.png")
            await self._send(message, embed=emb,
                             file=discord.File(buf, filename="flo_woerter.png"))
            return HANDLED
        top = heapq.nlargest(10, words_dict.items(), key=lambda kv: int(kv[1]["n"]))
        zeilen = [f"**{i + 1}.** {w} – {int(e['n'])}×" for i, (w, e) in enumerate(top)]
        emb = discord.Embed(title="📊 Die meistgesagten Wörter",
                            description="\n".join(zeilen) + self._scan_hint(),
                            color=discord.Color.blurple())
        await self._send(message, embed=emb)
        return HANDLED


instance = Words()

# Modul-Aliase: bot.py/Tests rufen weiterhin die gewohnten Modul-Funktionen.
# _store bekommt bewusst KEINEN Alias (wird zur Laufzeit neu zugewiesen;
# Zugriff laeuft ueber words.instance._store).
setup = instance.setup
is_enabled = instance.is_enabled
_tokenize = instance._tokenize
_count_text = instance._count_text
_count_guarded = instance._count_guarded
note_message = instance.note_message
_mark_dirty = instance._mark_dirty
_save_store = instance._save_store
_replay_backlog = instance._replay_backlog
_flush_later = instance._flush_later
flush_now = instance.flush_now
_flush_now = instance._flush_now
is_scanning = instance.is_scanning
backfill = instance.backfill
_send = instance._send
_scan_hint = instance._scan_hint
handle = instance.handle
_word_query = instance._word_query
_build_top = instance._build_top
_top_command = instance._top_command
