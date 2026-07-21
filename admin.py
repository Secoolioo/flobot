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

import logging
import os
import re

import discord

import ai
import economy
from store import JsonStore

log = logging.getLogger("dcbot.admin")

OWNER_ID = int(os.getenv("OWNER_ID", "1040135855710404659") or "0")


class Admin:
    """Kapselt die Admin-Befehle samt veraenderlichem Zustand als Instanz."""

    _MENTION_RE = re.compile(r"<@!?(\d{15,20})>")
    _ID_RE = re.compile(r"\b(\d{15,20})\b")
    _AMOUNT_RE = re.compile(r"-?\d{1,12}")

    def __init__(self):
        # Veraenderlicher Modulzustand (frueher per 'global' neu zugewiesen).
        self._enabled = False
        self._bot_name = "Flo"
        # Sendepause ('Funkstille'): ist sie an, ignoriert der Bot JEDEN ausser dem
        # Besitzer komplett. Persistiert in data/admin.json (ueberlebt Neustarts).
        self._store = None
        self._locked = False

    def setup(self):
        """Aktiviert die Admin-Befehle (braucht nur eine OWNER_ID)."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if not OWNER_ID:
            log.info("Admin-Befehle aus (keine OWNER_ID).")
            return False
        self._store = JsonStore("admin.json", default={"sendepause": False})
        self._locked = bool(self._store.data.get("sendepause", False))
        self._enabled = True
        log.info("Admin-Befehle aktiv (Besitzer %d%s).",
                 OWNER_ID, ", SENDEPAUSE aktiv" if self._locked else "")
        return True

    def is_enabled(self):
        return self._enabled

    def is_locked(self):
        """True, wenn die Sendepause aktiv ist. bot.py fragt das bei JEDER Nachricht
        ab, um alle ausser dem Besitzer zu ignorieren - daher bewusst nur ein
        In-Memory-Flag (kein Datei-Zugriff auf dem heissen Pfad)."""
        return self._locked

    # --- Parsen ----------------------------------------------------------------
    def _extract(self, rest):
        """Zieht (ziel_user_id, betrag) aus dem Resttext: erst @-Mention, sonst
        rohe 15-20-stellige ID; der Betrag ist die erste verbleibende Zahl."""
        text = rest or ""
        uid = None
        m = self._MENTION_RE.search(text)
        if m:
            uid = int(m.group(1))
            text = text.replace(m.group(0), " ", 1)
        else:
            m2 = self._ID_RE.search(text)
            if m2:
                uid = int(m2.group(1))
                text = text[:m2.start()] + " " + text[m2.end():]
        m3 = self._AMOUNT_RE.search(text)
        amount = int(m3.group(0)) if m3 else None
        return uid, amount

    async def _user_of(self, message, uid):
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

    async def _name_of(self, message, uid):
        user = await self._user_of(message, uid)
        return user.display_name if user is not None else f"User {uid}"

    def _emb(self, text, *, color = None):
        emb = discord.Embed(description=text, color=color or discord.Color.gold())
        emb.set_author(name="🛠️ Flo Admin")
        return emb

    # --- Befehle ----------------------------------------------------------------
    async def handle(self, message):
        """Owner-only. None = kein Admin-Befehl -> naechster Handler ist dran."""
        if not self._enabled or message.author.id != OWNER_ID:
            return None
        cmd = ai.strip_lead(message.content or "")
        if not cmd:
            return None
        parts = cmd.split(None, 1)
        first = parts[0].lower().strip(".,;:!?")
        rest = parts[1] if len(parts) > 1 else ""

        if first in ("gib", "give", "schenk", "schenke"):
            return await self._give(message, rest, sign=+1)
        if first in ("nimm", "take", "entzieh", "entziehe"):
            return await self._give(message, rest, sign=-1)
        if first in ("setcoins", "coinsset"):
            return await self._set_coins(message, rest)
        if first in ("gibxp", "givexp", "xpgeben"):
            return await self._give_xp(message, rest)
        if first in ("profil", "profile"):
            return await self._profile(message, rest)
        if first in ("ansage", "announce"):
            return await self._announce(message, rest)
        if first in ("dm", "flüster", "fluester"):
            return await self._dm(message, rest)
        if first in ("soundboard", "sounds", "soundliste"):
            # NUR die an/aus-Form abfangen - 'flo soundboard' ohne Argument faellt
            # durch an voicegags (der Owner will das Board ja auch benutzen).
            schalter = rest.strip().lower()
            if schalter in ("an", "ein", "on", "aus", "off"):
                return await self._toggle_soundboard(schalter in ("an", "ein", "on"))
            return None
        if first in ("shopneu", "shoprefresh"):
            return await self._shop_refresh()
        if first in ("sendepause", "sendpause", "funkstille", "lockdown"):
            return await self._toggle_lock(rest.strip().lower())
        if first in ("admin", "adminhilfe", "adminhelp"):
            return self._admin_help()
        return None

    async def _toggle_lock(self, arg):
        """Sendepause an-/abschalten (persistiert). Ohne Argument: umschalten.
        Waehrend der Sendepause reagiert Flo auf NIEMANDEN ausser dem Besitzer."""
        if arg in ("aus", "off", "ende", "beenden", "stop", "weg"):
            neu = False
        elif arg in ("an", "ein", "on", "start"):
            neu = True
        else:
            neu = not self._locked
        self._locked = neu
        if self._store is not None:
            self._store.data["sendepause"] = neu
            try:
                await self._store.save()
            except Exception:  # noqa: BLE001 - Persistenz ist nice-to-have
                log.exception("Sendepause-Zustand konnte nicht gespeichert werden")
        if neu:
            log.info("SENDEPAUSE aktiviert (nur noch Besitzer nutzt Befehle).")
            return self._emb(
                "🔇 **Sendepause AN.**\nAb jetzt reagiere ich auf **keine Befehle und "
                "keine KI-Fragen** mehr – außer von dir. XP, Level, Coins und der "
                "Wortzähler laufen für alle ganz normal weiter.\n"
                f"Aufheben mit `{self._bot_name} sendepause aus`.",
                color=discord.Color.red())
        log.info("SENDEPAUSE aufgehoben (wieder fuer alle da).")
        return self._emb(
            "🔊 **Sendepause AUS.**\nIch bin wieder für alle da. 🎉",
            color=discord.Color.green())

    async def _give(self, message, rest, *, sign
                    ):
        uid, amount = self._extract(rest)
        if uid is None and amount is None:
            # 'gib mir mal einen Tipp' u. Ae.: kein Admin-Befehl, sondern Chat ->
            # weiterreichen (None), damit die KI antworten kann.
            return None
        if not economy.is_enabled():
            return "Economy (Flo Coins) ist gerade aus."
        verb = "gib" if sign > 0 else "nimm"
        if uid is None or amount is None:
            return f"So: `{self._bot_name} {verb} @wer 100` (oder mit User-ID)."
        delta = sign * abs(amount) if sign < 0 else sign * amount
        neu = economy.add_coins(uid, delta)
        await economy.flush()
        name = await self._name_of(message, uid)
        if delta >= 0:
            return self._emb(f"💰 **{name}** bekommt **+{delta} {economy.COIN}** "
                             f"→ neuer Stand: **{neu}**.", color=discord.Color.green())
        return self._emb(f"🪙 **{name}** verliert **{delta} {economy.COIN}** "
                         f"→ neuer Stand: **{neu}**.", color=discord.Color.orange())

    async def _set_coins(self, message, rest):
        if not economy.is_enabled():
            return "Economy (Flo Coins) ist gerade aus."
        uid, amount = self._extract(rest)
        if uid is None or amount is None or amount < 0:
            return f"So: `{self._bot_name} setcoins @wer 500` (Betrag ≥ 0)."
        delta = amount - economy.get_coins(uid)
        neu = economy.add_coins(uid, delta)
        await economy.flush()
        name = await self._name_of(message, uid)
        return self._emb(f"🎯 Kontostand von **{name}** auf **{neu} {economy.COIN}** gesetzt.")

    async def _give_xp(self, message, rest):
        uid, amount = self._extract(rest)
        if uid is None and amount is None:
            return None   # Chat, kein Befehl - weiterreichen an die KI
        if not economy.is_enabled():
            return "Economy (Flo Coins) ist gerade aus."
        if uid is None or amount is None or amount <= 0:
            return f"So: `{self._bot_name} gibxp @wer 250`."
        user = await self._user_of(message, uid)
        if user is None:
            return f"Ich finde keinen User mit der ID {uid}."
        level = await economy.add_xp(user, amount)
        await economy.flush()
        extra = f" → **Level {level}**! 🎉" if level else ""
        return self._emb(f"⭐ **{user.display_name}** bekommt **+{amount} XP**{extra}")

    async def _profile(self, message, rest):
        if not economy.is_enabled():
            return "Economy (Flo Coins) ist gerade aus."
        uid, _ = self._extract(rest)
        if uid is None:
            uid = message.author.id   # ohne Ziel: eigenes Profil
        # leaderboard_data liefert ALLE Profile (oeffentliche API) - Row suchen.
        rows = economy.leaderboard_data(limit=10 ** 9)
        row = next((r for r in rows if r.get("id") == uid), None)
        name = await self._name_of(message, uid)
        if row is None:
            return self._emb(f"**{name}** hat noch kein Profil (nie geschrieben).")
        h, rem = divmod(int(row.get("voice_secs", 0)), 3600)
        emb = self._emb(f"👤 **{name}**", color=discord.Color.blurple())
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

    def _parse_announce(self, rest):
        """Zerlegt 'ansage ...' in (channel_id, text). Akzeptiert die rohe ID
        UND eine Channel-Erwaehnung wie <#1234...>."""
        m = re.match(r"\s*(?:<#)?(\d{15,20})>?\s+(.+)", rest or "", re.DOTALL)
        if not m:
            return None, ""
        return int(m.group(1)), m.group(2).strip()

    async def _announce(self, message, rest):
        cid, text = self._parse_announce(rest)
        if cid is None or not text:
            return (f"So: `{self._bot_name} ansage <channel-id> <text>` "
                    f"(auch `{self._bot_name} ansage #channel <text>`) - "
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

    def _parse_dm(self, rest):
        """'@wer hallo du' / '1234... hallo du' -> (user_id, text)."""
        m = self._MENTION_RE.search(rest or "") or self._ID_RE.search(rest or "")
        if not m:
            return None, ""
        text = (rest[:m.start()] + rest[m.end():]).strip()
        return int(m.group(1)), text

    async def _dm(self, message, rest):
        """Flo schreibt jemandem privat - als waere er's selbst. Antworten der
        Person leitet bot.py automatisch an den Besitzer zurueck (DM-Relay)."""
        uid, text = self._parse_dm(rest)
        if uid is None or not text:
            return (f"So: `{self._bot_name} dm @wer <text>` (oder mit User-ID) - "
                    "ich stelle es privat zu; Antworten landen wieder bei dir.")
        user = await self._user_of(message, uid)
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
        emb = self._emb(f"📨 An **{user.display_name}** zugestellt:\n> {kurz}",
                        color=discord.Color.green())
        emb.set_footer(text="Antworten leite ich automatisch an dich weiter.")
        return emb

    async def _toggle_soundboard(self, an):
        """Soundboard serverweit an-/abschalten (persistiert ueber Neustarts)."""
        try:
            import voicegags
            if not voicegags.is_enabled():
                return "Voice-Gags sind gerade komplett aus (ffmpeg/PyNaCl fehlt?)."
            await voicegags.set_soundboard(an)
        except Exception:  # noqa: BLE001
            log.exception("Soundboard-Toggle fehlgeschlagen")
            return "Da ist beim Umschalten etwas schiefgelaufen."
        if an:
            return self._emb("🔊 Soundboard ist wieder **AN** - lasst es krachen!",
                             color=discord.Color.green())
        return self._emb("🔇 Soundboard ist jetzt **AUS** - niemand kann Sounds abspielen "
                         f"(wieder an: `{self._bot_name} soundboard an`).",
                         color=discord.Color.orange())

    async def _shop_refresh(self):
        if not economy.is_enabled():
            return "Economy (Flo Coins) ist gerade aus."
        st = await economy.refresh_shop_async(force=True)
        return (f"✅ Shop neu gewürfelt: {len(st.get('items', []))} Titel "
                f"für {st.get('date', '?')}.")

    def _admin_help(self):
        n = self._bot_name
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
                             f"`{n} soundboard an/aus` · `{n} restart`\n"
                             f"`{n} sendepause` – Funkstille: nur DU wirst noch bedient "
                             f"(aus: `{n} sendepause aus`)"), inline=False)
        emb.add_field(name="DM-Relay",
                      value=(f"`{n} dm @wer <text>` – {n} schreibt privat; "
                             "Antworten landen automatisch bei dir."), inline=False)
        return emb


# Eine Instanz fuers ganze Modul + Aliase, damit bot.py/Tests die bisherigen
# Modulnamen weiter nutzen koennen. Bewusst KEINE Aliase fuer _enabled/_bot_name:
# die werden zur Laufzeit neu zugewiesen und leben nur als Instanzattribute.
instance = Admin()

setup = instance.setup
is_enabled = instance.is_enabled
is_locked = instance.is_locked
_toggle_lock = instance._toggle_lock
_extract = instance._extract
_user_of = instance._user_of
_name_of = instance._name_of
_emb = instance._emb
handle = instance.handle
_give = instance._give
_set_coins = instance._set_coins
_give_xp = instance._give_xp
_profile = instance._profile
_parse_announce = instance._parse_announce
_announce = instance._announce
_parse_dm = instance._parse_dm
_dm = instance._dm
_toggle_soundboard = instance._toggle_soundboard
_shop_refresh = instance._shop_refresh
_admin_help = instance._admin_help
