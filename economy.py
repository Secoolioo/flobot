"""Level- & Flo-Coins-System fuer Flo (Pack 3).

Was es kann:
- XP fuers Schreiben (mit Cooldown gegen Spam) und fuer Zeit im Sprachkanal.
- Automatische Level-Up-Ansage im Chat, dazu Flo Coins als Belohnung.
- Befehle: level/rank, top/bestenliste, coins/konto, daily, pay, shop, kaufen.
- Speichert alles in data/economy.json (siehe store.py, ohne Extra-Abhaengigkeit).

Dieses Modul ist die EINZIGE Quelle fuer den Coin-Kontostand. Andere Module
(z. B. games.py) vergeben Coins ueber economy.add_coins(), damit es nur einen
Topf gibt.
"""

import asyncio
import io
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import ai
import leaderboard_img
import render
import titles
from store import JsonStore

log = logging.getLogger("dcbot.economy")

# Sentinel: economy hat selbst geantwortet (interaktive Shop-/Inventar-View)
# -> bot.py schweigt (wie bei games/casino).
HANDLED = object()


class Economy:
    """Kapselt das komplette Level-/Coin-System: Konfiguration, Datenzugriff,
    XP-Vergabe, Shop, Inventar und die Befehls-Verarbeitung."""

    # --- Konfiguration -------------------------------------------------------
    _tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

    # XP-Vergabe
    XP_PER_MSG = (15, 25)        # zufaellig in diesem Bereich pro Nachricht
    COINS_PER_MSG = (1, 3)       # nebenbei ein paar Coins
    MSG_COOLDOWN = 45            # Sekunden Sperre pro Nutzer (Anti-Spam)
    XP_PER_VOICE_TICK = 10       # XP pro Voice-Runde (Bot-Loop ruft tick_voice auf)
    VOICE_TICK_SECONDS = 60      # nur Info fuer die Ansage; Takt steuert bot.py

    # Muenzeinheit
    COIN = "Flo Coins"

    # Wohin Level-Up-Ansagen gehen. Standard: der Commands-Channel. 0 = im selben
    # Kanal ansagen, in dem die Nachricht kam. Per .env (LEVELUP_CHANNEL_ID) aenderbar.
    LEVELUP_CHANNEL_ID = int(os.getenv("LEVELUP_CHANNEL_ID", "1512045750362837013") or "0")
    # Titel der Level-Up-Embeds. bot.py nimmt solche Nachrichten vom Auto-Loeschen
    # aus, damit Erfolge im Commands-Channel sichtbar bleiben.
    LEVELUP_EMBED_TITLE = "🎉 Level Up!"

    # --- Shop v1.2 -----------------------------------------------------------
    # Der Shop zeigt taeglich eine zufaellige, seltenheits-gewichtete Auswahl aus
    # zehntausenden Titeln (siehe titles.py). Um 2 Uhr morgens wird neu gewuerfelt
    # (bot.py ruft refresh_shop). Gekaufte Titel geben dem Nutzer eine farbige Rolle
    # in der Seltenheits-Farbe (gruen/blau/lila/gold).
    SHOP_SIZE = int(os.getenv("SHOP_SIZE", "8") or "8")

    # Alte, fest verdrahtete Titel von vor v1.2 - nur noch fuer die Migration alter
    # Profile (wer sie schon besitzt, behaelt sie). Werden nicht mehr verkauft.
    LEGACY_SHOP = {
        "sigma":    {"preis": 1000, "titel": "🗿 Sigma",      "info": "Der Klassiker."},
        "gigachad": {"preis": 2500, "titel": "💪 Gigachad",   "info": "Maximale Aura."},
        "rizzler":  {"preis": 1500, "titel": "😏 Rizzler",    "info": "Unwiderstehlich."},
        "goblin":   {"preis":  500, "titel": "👺 Goblin",     "info": "Chaos-Energie."},
        "npc":      {"preis":  100, "titel": "🤖 NPC",        "info": "Lebt im Hintergrund."},
        "king":     {"preis": 5000, "titel": "👑 Server-King", "info": "Ganz oben."},
    }
    # Welche Seltenheit bekommen alte Titel bei der Migration?
    LEGACY_RARITY = {
        "sigma": "mythisch", "gigachad": "legendary", "rizzler": "selten",
        "goblin": "normal", "npc": "normal", "king": "legendary",
    }

    # Betrags-Parser: '1000', '1k', '2,5k', '1m'/'1mio', '1mrd' -> Coins.
    _AMOUNT_TOKEN_RE = re.compile(r"^(\d+(?:[.,]\d+)?)(k|m|mio|mrd|b)?$", re.IGNORECASE)
    _AMOUNT_MULT = {"": 1, "k": 1_000, "m": 1_000_000, "mio": 1_000_000,
                    "mrd": 1_000_000_000, "b": 1_000_000_000}

    # Unhinged Level-Up-Texter. Die KI roastet den Aufsteiger derb & lustig.
    _LEVELUP_SYSTEM = (
        "Du bist Flo, ein komplett unhinged, asozialer Gremlin-Bot ohne jede Manieren. "
        "Ein User in einem Discord ist gerade im Level aufgestiegen. Feiere das, indem "
        "du ihn auf absurd-derbe, vulgaere und brutal lustige Art zur Sau machst - wie "
        "der schlimmste beste Freund, den man haben kann. NSFW, Schimpfwoerter und "
        "fiese Sexwitze sind ausdruecklich erlaubt und erwuenscht.\n"
        "Regeln:\n"
        "- GENAU EIN kurzer Satz, hoechstens ~15 Woerter. Kurz und schmerzhaft, aber "
        "lustig - es soll ein bisschen weh tun, nicht in einen Roman ausarten.\n"
        "- Deutsch. Keine Einleitung wie 'Hier:' und keine Anfuehrungszeichen drumherum.\n"
        "- Erwaehne das neue Level irgendwie.\n"
        "- Jedes Mal anders, lieber pointiert-absurd als generisch.\n"
        "- KEIN echter Hass gegen Herkunft, Hautfarbe, Religion, Geschlecht, Behinderung "
        "oder sexuelle Orientierung - das ist nicht lustig. Alles andere ist Freiwild.\n"
        "- Hoechstens 1 Emoji."
    )
    # Fallback, falls die KI aus ist oder zickt - trotzdem mit etwas Pfeffer.
    _LEVELUP_FALLBACK = [
        "GG! Level {lvl} – und im echten Leben immer noch hartes Level 1, ne?",
        "Level {lvl} erreicht. Geh mal raus, die Sonne fragt nach dir, du Höhlentroll.",
        "Sheesh, Level {lvl}! Deine Tastatur leidet mehr als dein Sozialleben.",
        "Aufstieg auf Level {lvl}! Gönn dir zur Feier mal 'ne Dusche, du Sumpfkreatur.",
        "Level {lvl}, Sigma-Move. Schade nur, dass dich offline keiner kennt.",
        "Boom, Level {lvl}. Touch grass, bevor es Wurzeln in deinem Stuhl schlägt.",
    ]

    # Avatar-Cache: Profilbilder aendern sich selten -> kurz zwischenspeichern,
    # damit ein wiederholtes 'top' nicht jedes Mal alles neu laedt. Schluessel ist
    # die User-ID, damit der Cache VOR jeder User-Aufloesung greifen kann.
    _AVATAR_TTL = 1800.0       # 30 Minuten
    _AVATAR_FAIL_TTL = 600.0   # 10 Minuten

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        # Cooldowns nur im Speicher (gehen bei Neustart verloren - egal, sind kurz).
        self._last_msg_xp = {}
        self._store = None
        # Avatar-Cache (positiv) und Negativ-Cache: IDs, deren Aufloesung/Download
        # gerade erst fehlschlug (geloeschter Account, CDN-Huster) - nicht bei
        # jedem 'top' neu versuchen.
        self._AVATAR_CACHE = {}
        self._AVATAR_FAIL = {}

    def setup(self):
        """Aktiviert das Feature. Laeuft immer (keine externen Voraussetzungen)."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("ECONOMY_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Level/Coins-Feature aus (ECONOMY_ENABLED=0).")
            return False
        self._store = JsonStore("economy.json", default={"users": {}})
        self._enabled = True
        log.info("Level/Coins-Feature aktiv (%d Profile geladen).", len(self._users()))
        return True

    def is_enabled(self):
        return self._enabled

    # --- Datenzugriff --------------------------------------------------------
    def _users(self):
        assert self._store is not None
        return self._store.data.setdefault("users", {})

    def _strip_emoji(self, label):
        """'👑 Goldener König' -> 'Goldener König' (fuehrendes Emoji/Symbol weg)."""
        return re.sub(r"^\W+\s*", "", label or "").strip()

    def _owned_entry_from_legacy(self, key):
        it = self.LEGACY_SHOP.get(key)
        if it:
            return {"text": self._strip_emoji(it["titel"]), "label": it["titel"],
                    "rarity": self.LEGACY_RARITY.get(key, "selten")}
        # Unbekannter String -> als generierter Titel-Text behandeln.
        return {"text": key, "label": titles.label_of(key), "rarity": titles.rarity_of(key)}

    def _migrate_profile(self, prof):
        """Bringt ein Profil auf das v1.2-Schema (owned = Liste von Dicts mit
        text/label/rarity, plus title_rarity). Idempotent."""
        prof.setdefault("owned", [])
        prof.setdefault("msgs", 0)
        prof.setdefault("title", "")
        prof.setdefault("title_rarity", "")
        owned = prof["owned"]
        # Altprofil (vor Inventar): wer einen Titel TRAEGT, bekommt ihn ins Inventar.
        if prof.get("title") and not owned:
            for k, it in self.LEGACY_SHOP.items():
                if it["titel"] == prof["title"]:
                    owned.append(k)
                    break
        # owned von Liste[str] (alte Keys) -> Liste[dict] migrieren.
        if owned and isinstance(owned[0], str):
            prof["owned"] = [self._owned_entry_from_legacy(k) for k in owned]
            owned = prof["owned"]
        # Getragene Seltenheit nachziehen, falls Titel da, aber Rarity leer.
        if prof.get("title") and not prof.get("title_rarity"):
            for o in owned:
                if o.get("label") == prof["title"]:
                    prof["title_rarity"] = o.get("rarity", "")
                    break

    def _profile(self, user_id):
        """Holt (oder erstellt) das Profil eines Nutzers."""
        users = self._users()
        key = str(user_id)
        prof = users.get(key)
        if prof is None:
            prof = {"xp": 0, "coins": 0, "last_daily": "", "streak": 0,
                    "voice_secs": 0, "msgs": 0, "title": "", "title_rarity": "",
                    "owned": [], "name": ""}
            users[key] = prof
        self._migrate_profile(prof)
        return prof

    def _owned_list(self, prof):
        """Inventar als Liste von Dicts (text/label/rarity)."""
        return prof.setdefault("owned", [])

    # --- Auto-Loesch-Schutz (fuer die Shop-/Inventar-Views) ------------------
    def _protect(self, msg):
        if msg is None:
            return
        try:
            import bot
            bot.protect_message(msg)
        except Exception:  # noqa: BLE001
            pass

    def _release(self, msg):
        if msg is None:
            return
        try:
            import bot
            bot.release_message(msg)
        except Exception:  # noqa: BLE001
            pass

    async def _flush(self):
        if self._store is not None:
            await self._store.save()

    async def flush(self):
        """Oeffentliches Speichern - andere Module (z. B. games) rufen das nach
        einer Coin-Aenderung auf, damit der Gewinn die Platte erreicht."""
        await self._flush()

    # --- Level-Mathematik ----------------------------------------------------
    def _level_for_xp(self, xp):
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

    def _level_only(self, xp):
        return self._level_for_xp(xp)[0]

    # --- XP vergeben ---------------------------------------------------------
    async def add_xp(self, member, amount):
        """Gibt einem Mitglied XP. Rueckgabe: neues Level, falls ein Level-Up
        passiert ist, sonst None."""
        if not self._enabled or amount <= 0:
            return None
        prof = self._profile(member.id)
        prof["name"] = getattr(member, "display_name", "") or prof.get("name", "")
        before = self._level_only(prof["xp"])
        prof["xp"] += amount
        after = self._level_only(prof["xp"])
        if after > before:
            reward = after * 25
            prof["coins"] += reward
            self._record_trade(member.id, reward, "levelup", prof["coins"])
            return after
        return None

    # Huebsche Quellen-Labels fuers Handelsbuch (Modulname -> Anzeige).
    _TRADE_SOURCES = {"games": "spiele", "fun": "chaos", "voicegags": "voice"}

    def add_coins(self, user_id, amount, reason = ""):
        """Aendert den Kontostand (auch negativ) und gibt den neuen Stand zurueck.
        Geht nie unter 0. Jede Bewegung landet im Handelsbuch (handel.py):
        'reason' benennt die Quelle; ohne reason wird das aufrufende Modul
        ermittelt (casino, spiele, luxus, admin, ...)."""
        if not self._enabled:
            return 0
        if not reason:
            try:
                mod = sys._getframe(1).f_globals.get("__name__", "?")
                reason = self._TRADE_SOURCES.get(mod, mod)
            except Exception:  # noqa: BLE001 - Quelle ist nur Doku
                reason = "?"
        prof = self._profile(user_id)
        alt = prof["coins"]
        prof["coins"] = max(0, alt + amount)
        # Echtes Delta buchen (bei leerem Konto kann weniger abgehen als gewollt).
        self._record_trade(user_id, prof["coins"] - alt, reason, prof["coins"])
        return prof["coins"]

    def _record_trade(self, uid, delta, source, balance):
        """Meldet eine Coin-Bewegung ans Handelsbuch. Lazy-Import (kein
        Import-Zyklus) und niemals fatal - Buchhaltung sprengt kein Spiel."""
        if not delta:
            return
        try:
            import handel
            handel.record(uid, delta, source, balance)
        except Exception:  # noqa: BLE001
            pass

    def get_coins(self, user_id):
        return self._profile(user_id)["coins"] if self._enabled else 0

    def parse_amount(self, token):
        """'1k' -> 1000, '2,5k' -> 2500, '1m' -> 1000000. None wenn keine Zahl."""
        m = self._AMOUNT_TOKEN_RE.match((token or "").strip().lower())
        if not m:
            return None
        wert = float(m.group(1).replace(",", "."))
        betrag = int(round(wert * self._AMOUNT_MULT[(m.group(2) or "").lower()]))
        return betrag if betrag > 0 else None

    def display_name_of(self, user_id):
        """Zuletzt bekannter Anzeigename aus dem Profil-Cache (wird bei jeder
        Nachricht aktualisiert), sonst None. Praktisch als Fallback, wenn
        guild.get_member() ohne Members-Intent nichts liefert."""
        if not self._enabled:
            return None
        prof = self._users().get(str(user_id))
        return (prof or {}).get("name") or None

    def get_title(self, user_id):
        """Aktuell getragener Titel (Label inkl. Emoji), z. B. '🤖 NPC', sonst ''.
        bot.py reicht das an die KI weiter, damit Flo den Nutzer damit anspricht."""
        if not self._enabled:
            return ""
        return self._profile(user_id).get("title", "") or ""

    def get_user_rarity(self, user_id):
        """Hoechste Seltenheit, die der Nutzer BESITZT (fuer die Rollen-Farbe)."""
        if not self._enabled:
            return None
        best = None
        best_rank = -1
        for o in self._owned_list(self._profile(user_id)):
            rank = titles.RANK.get(o.get("rarity", "normal"), 0)
            if rank > best_rank:
                best_rank, best = rank, o.get("rarity", "normal")
        return best

    def get_tone(self, user_id):
        """Tonfall-Hinweis fuer die KI: je seltener der GETRAGENE Titel, desto
        entspannter spricht Flo. Leerer String = normaler Ton."""
        if not self._enabled:
            return ""
        rar = self._profile(user_id).get("title_rarity") or ""
        return titles.RARITY.get(rar, {}).get("tone", "") if rar else ""

    # --- Taeglicher Shop -----------------------------------------------------
    def _shop_state(self):
        assert self._store is not None
        return self._store.data.setdefault("shop", {"date": "", "items": []})

    def refresh_shop(self, force = False):
        """Wuerfelt die Tagesauswahl neu, falls noetig (neuer Tag, leer oder force).
        Speichert NICHT selbst – Aufrufer ruft danach flush()."""
        st = self._shop_state()
        if not force and st.get("date") == self._today() and st.get("items"):
            return st
        items = titles.random_titles(self.SHOP_SIZE)
        for i, e in enumerate(items, 1):
            e["n"] = i
        st["date"] = self._today()
        st["items"] = items
        log.info("Flo Shop neu gewuerfelt (%d Titel, %s).", len(items), st["date"])
        return st

    async def refresh_shop_async(self, force = False):
        st = self.refresh_shop(force=force)
        await self._flush()
        return st

    def get_shop_items(self):
        return self._shop_state().get("items", [])

    # --- Seltenheits-Rollen --------------------------------------------------
    async def _find_or_create_role(self, guild, rarity):
        """Sucht die Rarity-Rolle oder legt sie in der passenden Farbe an.
        Gibt None zurueck, wenn das nicht klappt (fehlende Rechte o. Ae.)."""
        meta = titles.RARITY[rarity]
        name = meta["role"]
        role = discord.utils.get(guild.roles, name=name)
        if role is not None:
            return role
        try:
            return await guild.create_role(
                name=name, colour=discord.Colour(meta["color"]),
                mentionable=False, reason="Flo Titel-Seltenheit (v1.2)")
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Konnte Rolle '%s' nicht anlegen (Rechte?).", name)
            return None

    async def ensure_roles(self, guild):
        """Legt beim Start ALLE vier Rarity-Rollen (in ihren Farben) im Server an,
        falls sie noch fehlen. Idempotent: vorhandene Rollen bleiben unangetastet.
        Fehlertolerant – fehlende Rechte sprengen nie den Start. Gibt eine Statistik
        {'created'|'existed'|'failed': [Rollennamen]} zurueck."""
        stats = {"created": [], "existed": [], "failed": []}
        if not self._enabled or guild is None:
            return stats
        for rarity in titles.RARITY_ORDER:
            name = titles.RARITY[rarity]["role"]
            if discord.utils.get(guild.roles, name=name) is not None:
                stats["existed"].append(name)
                continue
            role = await self._find_or_create_role(guild, rarity)
            (stats["created"] if role is not None else stats["failed"]).append(name)
        if stats["created"]:
            log.info("Rarity-Rollen angelegt in '%s': %s",
                     guild.name, ", ".join(stats["created"]))
        if stats["failed"]:
            log.warning("Rarity-Rollen NICHT anlegbar in '%s' (fehlt 'Rollen verwalten' "
                        "oder Bot-Rolle zu weit unten?): %s",
                        guild.name, ", ".join(stats["failed"]))
        return stats

    async def _sync_role(self, member):
        """Gibt dem Mitglied genau EINE Flo-Rarity-Rolle: die seiner hoechsten
        besessenen Seltenheit (die anderen werden entfernt). Alles fehlertolerant –
        ein Rechteproblem darf den Kauf nie sprengen."""
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        best = self.get_user_rarity(member.id)
        want = titles.RARITY[best]["role"] if best else None
        flo_roles = {titles.RARITY[r]["role"] for r in titles.RARITY_ORDER}
        have = list(getattr(member, "roles", []))
        to_remove = [r for r in have if r.name in flo_roles and r.name != want]
        try:
            if to_remove:
                await member.remove_roles(*to_remove, reason="Flo Titel-Update")
        except (discord.Forbidden, discord.HTTPException):
            pass
        if want and not any(r.name == want for r in have):
            role = await self._find_or_create_role(guild, best)
            if role is not None:
                try:
                    await member.add_roles(role, reason="Flo Titel gekauft")
                except (discord.Forbidden, discord.HTTPException):
                    pass

    async def _do_buy(self, member, item):
        """Kauft (oder legt an) den Titel 'item' fuer 'member', vergibt die Rolle und
        gibt eine Antwort als Text zurueck."""
        prof = self._profile(member.id)
        owned = self._owned_list(prof)
        meta = titles.RARITY[item["rarity"]]
        already = next((o for o in owned if o.get("text") == item["text"]), None)
        if already:
            if prof.get("title") == item["label"]:
                return f"Den Titel **{item['label']}** trägst du schon. 😎"
            prof["title"] = item["label"]
            prof["title_rarity"] = item["rarity"]
            await self._sync_role(member)
            await self._flush()
            return f"Den hast du schon – ich hab dir **{item['label']}** angelegt. 😎"
        if prof["coins"] < item["price"]:
            fehlt = item["price"] - prof["coins"]
            return f"Zu teuer – dir fehlen noch {fehlt} {self.COIN}."
        prof["coins"] -= item["price"]
        self._record_trade(member.id, -item["price"], "shop", prof["coins"])
        owned.append({"text": item["text"], "label": item["label"],
                      "rarity": item["rarity"]})
        prof["title"] = item["label"]
        prof["title_rarity"] = item["rarity"]
        await self._sync_role(member)
        await self._flush()
        chill = ("ab jetzt redet Flo richtig entspannt mit dir 😌"
                 if item["rarity"] in ("mythisch", "legendary")
                 else "Flo spricht dich ab jetzt damit an")
        return (f"🎉 Gekauft! Du trägst jetzt **{item['label']}** "
                f"({meta['emoji']} {meta['label']}) und hast die Rolle "
                f"**{meta['role']}** bekommen – {chill}.")

    # --- Passiver Hook: XP pro Nachricht -------------------------------------
    async def on_message(self, message):
        """Vergibt XP/Coins fuer eine Nachricht (mit Cooldown) und sagt Level-Ups an.
        Wird in bot.py fuer JEDE Nicht-Bot-Nachricht aufgerufen."""
        if not self._enabled or message.guild is None or message.author.bot:
            return
        # Nachrichtenzaehler fuers Leaderboard: zaehlt JEDE Nachricht (ohne Cooldown).
        # Wird im Speicher hochgezaehlt und beim naechsten _flush() mitgespeichert.
        prof = self._profile(message.author.id)
        prof["msgs"] = prof.get("msgs", 0) + 1
        prof["name"] = getattr(message.author, "display_name", "") or prof.get("name", "")

        key = str(message.author.id)
        now = time.monotonic()
        if now - self._last_msg_xp.get(key, 0.0) < self.MSG_COOLDOWN:
            return
        self._last_msg_xp[key] = now

        self.add_coins(message.author.id, random.randint(*self.COINS_PER_MSG),
                       reason="nachricht")
        new_level = await self.add_xp(message.author, random.randint(*self.XP_PER_MSG))
        if new_level is not None:
            await self._announce_levelup(message.guild, message.author, new_level, message.channel)
        await self._flush()

    def _levelup_target(self, guild, fallback):
        """Liefert den Ziel-Channel fuer Level-Up-Ansagen (Commands-Channel, sonst
        der Kanal, in dem die Aktion passierte)."""
        if guild is not None and self.LEVELUP_CHANNEL_ID:
            ch = guild.get_channel(self.LEVELUP_CHANNEL_ID)
            if ch is not None:
                perms = ch.permissions_for(guild.me)
                if perms.view_channel and perms.send_messages:
                    return ch
        return fallback

    async def _levelup_text(self, member, level):
        """Holt einen frischen, unhinged Roast von der KI; faellt sonst auf eine
        derbe Standardzeile zurueck. Bricht NIE die Ansage ab."""
        if ai.is_enabled():
            name = getattr(member, "display_name", "") or "der Typ"
            try:
                out = await ai.generate(
                    f"{name} ist gerade auf Level {level} aufgestiegen. Ein kurzer, fieser Einzeiler.",
                    system=self._LEVELUP_SYSTEM,
                    temperature=1.15,   # hoch = mehr Chaos/Abwechslung
                    max_tokens=60,      # harte Bremse -> bleibt ein kurzer Satz
                )
            except Exception:  # noqa: BLE001 - KI-Fehler darf die Ansage nicht killen
                out = None
            if out:
                return out.strip().strip('"').strip()
        return random.choice(self._LEVELUP_FALLBACK).format(lvl=level)

    async def _announce_levelup(self, guild, member, level, fallback=None):
        channel = self._levelup_target(guild, fallback)
        if channel is None:
            return
        reward = level * 25
        roast = await self._levelup_text(member, level)
        emb = discord.Embed(
            title=self.LEVELUP_EMBED_TITLE,
            description=f"{member.mention} ist jetzt **Level {level}**!\n\n{roast}",
            color=discord.Color.gold(),
        )
        emb.add_field(name="Belohnung", value=f"💰 +{reward} {self.COIN}", inline=True)
        try:
            emb.set_thumbnail(url=member.display_avatar.url)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko
            pass
        try:
            await channel.send(content=member.mention, embed=emb)
        except discord.HTTPException:
            pass

    # --- Voice-XP (bot.py ruft das periodisch pro Guild auf) -----------------
    async def tick_voice(self, guild):
        """Gibt allen aktiven Mitgliedern in Sprachkanaelen XP. AFK/stumm/Bots
        bekommen nichts. bot.py ruft das im Takt VOICE_TICK_SECONDS auf."""
        if not self._enabled:
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
                prof = self._profile(m.id)
                prof["voice_secs"] = prof.get("voice_secs", 0) + self.VOICE_TICK_SECONDS
                new_level = await self.add_xp(m, self.XP_PER_VOICE_TICK)
                changed = True
                if new_level is not None:
                    await self._announce_levelup(guild, m, new_level, guild.system_channel)
        if changed:
            await self._flush()

    # --- Befehls-Erkennung ---------------------------------------------------
    def _clean_lead(self, text):
        """Entfernt @-Mentions und den fuehrenden Botnamen/Alias ('Florian, level' ->
        'level'). Zentral in ai.strip_lead, damit alle Module gleich reagieren."""
        return ai.strip_lead(text)

    def _bar(self, into, step, width = 12):
        filled = 0 if step <= 0 else max(0, min(width, round(into / step * width)))
        return "█" * filled + "░" * (width - filled)

    def _rank_of(self, user_id):
        """Platz (1-basiert) nach XP und Gesamtzahl der Profile."""
        ranking = sorted(self._users().items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
        total = len(ranking)
        for i, (key, _prof) in enumerate(ranking, start=1):
            if key == str(user_id):
                return i, total
        return total, total

    def _today(self):
        return datetime.now(self._tz).strftime("%Y-%m-%d")

    async def handle(self, message):
        """Erkennt Level-/Coin-Befehle. Rueckgabe: Antworttext, Embed, Bild oder None."""
        if not self._enabled or message.guild is None:
            return None
        cmd = self._clean_lead(message.content or "")
        if not cmd:
            return None
        low = cmd.lower()
        parts = low.split()
        first = parts[0] if parts else ""

        # Zielnutzer (erste Mention), sonst der Autor selbst.
        target = message.mentions[0] if message.mentions else message.author

        if first in ("level", "lvl", "rank", "rang"):
            return await self._card_image(target)
        if first in ("coins", "konto", "kontostand", "münzen", "muenzen", "balance", "bal"):
            c = self.get_coins(target.id)
            wer = "Du hast" if target.id == message.author.id else f"{target.display_name} hat"
            return f"💰 {wer} **{c} {self.COIN}**."
        if first in ("top", "bestenliste", "rangliste", "leaderboard", "lb"):
            return await self._leaderboard(message.guild)
        if first in ("daily", "täglich", "taeglich", "tagesbonus"):
            return await self._daily(message.author)
        if first in ("pay", "zahl", "zahle", "überweis", "ueberweis", "überweise"):
            return await self._pay(message)
        if first in ("shop", "laden", "store"):
            return await self._shop(message)
        if first in ("kaufen", "buy", "kauf"):
            return await self._buy_text(message.author, parts)
        if first in ("inventar", "inventory", "inv", "titel", "titles", "title"):
            # 'titel ab/<name>' aendert den getragenen Titel; sonst Inventar zeigen.
            if first in ("titel", "title", "titles") and len(parts) > 1:
                return await self._equip(message.author, low)
            return await self._inventory(message)
        if first in ("equip", "anlegen", "trage", "tragen", "anziehen", "setze"):
            return await self._equip(message.author, low)
        return None

    def _rarity_accent(self, prof):
        """RGB-Akzentfarbe der Titel-Seltenheit (fuer die Level-Karte)."""
        rar = prof.get("title_rarity") or ""
        hexcol = titles.RARITY.get(rar, {}).get("color")
        if not hexcol:
            return None
        return ((hexcol >> 16) & 255, (hexcol >> 8) & 255, hexcol & 255)

    async def _card_image(self, member):
        """Level-/Rank-Karte als gerendertes Bild; faellt bei Problemen aufs
        bewaehrte Embed zurueck (niemals ein Crash)."""
        prof = self._profile(member.id)
        level, into, step = self._level_for_xp(prof["xp"])
        place, total = self._rank_of(member.id)
        avatar = None
        try:
            avatar = await asyncio.wait_for(
                member.display_avatar.with_size(256).read(), timeout=6)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko
            pass
        # Luxus-Rahmen (Flo Luxus Shop) - lazy, damit kein Import-Zyklus entsteht.
        frame = None
        try:
            import luxus
            frame = luxus.get_frame(member.id)
        except Exception:  # noqa: BLE001 - Rahmen ist nur Deko
            pass
        try:
            buf = render.level_card(
                avatar,
                name=getattr(member, "display_name", "") or "Spieler",
                level=level, into=into, step=step, place=place, total=total,
                xp=prof["xp"], coins=prof["coins"], msgs=prof.get("msgs", 0),
                voice_secs=prof.get("voice_secs", 0), streak=prof.get("streak", 0),
                title=self._clean_title_text(prof.get("title") or ""),
                accent=self._rarity_accent(prof), frame=frame,
            )
            return discord.File(buf, filename="flo_level.png")
        except Exception:  # noqa: BLE001
            log.exception("Level-Karte fehlgeschlagen - nutze Embed")
            return self._card(member)

    def _clean_title_text(self, title):
        """Titel ohne fuehrendes Emoji (das kann die Karte nicht zeichnen)."""
        return re.sub(r"^\W+", "", title or "").strip()

    def _card(self, member):
        prof = self._profile(member.id)
        level, into, step = self._level_for_xp(prof["xp"])
        place, total = self._rank_of(member.id)
        title = prof.get("title") or "—"
        pct = 100 if step <= 0 else round(into / step * 100)
        emb = discord.Embed(
            title=f"📈 Level {level}",
            description=f"`{self._bar(into, step)}`  **{pct}%**\n{into} / {step} XP bis Level {level + 1}",
            color=discord.Color.blurple(),
        )
        try:
            emb.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            emb.set_thumbnail(url=member.display_avatar.url)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko, Fallback darf nie crashen
            emb.set_author(name=str(getattr(member, "display_name", "Spieler")))
        emb.add_field(name="Gesamt-XP", value=f"✨ {prof['xp']}", inline=True)
        emb.add_field(name=self.COIN, value=f"💰 {prof['coins']}", inline=True)
        emb.add_field(name="Platz", value=f"🏅 #{place} / {total}", inline=True)
        emb.add_field(name="Titel", value=title, inline=True)
        emb.add_field(name="Streak", value=f"🔥 {prof.get('streak', 0)} Tag(e)", inline=True)
        emb.add_field(name="Nachrichten", value=f"💬 {prof.get('msgs', 0)}", inline=True)
        emb.set_footer(text=f"{self._bot_name} top   ·   {self._bot_name} daily   ·   {self._bot_name} shop")
        return emb

    def leaderboard_data(self, limit = 10):
        """Aufbereitete Bestenliste fuers Leaderboard-Bild (sortiert nach XP).
        'id' = Discord-User-ID (fuers Laden des Profilbilds)."""
        ranking = sorted(self._users().items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
        out = []
        for key, prof in ranking[:limit]:
            try:
                uid = int(key)
            except (TypeError, ValueError):
                uid = 0
            out.append({
                "id": uid,
                "name": prof.get("name") or "Unbekannt",
                "level": self._level_only(prof.get("xp", 0)),
                "xp": prof.get("xp", 0),
                "coins": prof.get("coins", 0),
                "voice_secs": prof.get("voice_secs", 0),
                "msgs": prof.get("msgs", 0),
                "title": prof.get("title") or "",
            })
        return out

    async def _resolve_avatar_user(self, guild, uid):
        """Member-/User-Objekt fuer die Avatar-URL. WICHTIG: Ohne das privilegierte
        Members-Intent ist guild.get_member() unzuverlaessig - im Cache stehen nur
        Mitglieder, die gerade geschrieben haben oder im Voice sind. Deshalb die
        Kette Member-Cache -> globaler User-Cache -> API-Fetch. So bekommt das
        Leaderboard die Bilder IMMER, nicht nur fuer zufaellig aktive Leute."""
        member = guild.get_member(uid) if guild is not None else None
        if member is not None:
            return member
        try:
            import bot
            user = bot.client.get_user(uid)
            if user is None:
                user = await bot.client.fetch_user(uid)
            return user
        except Exception:  # noqa: BLE001 - unbekannte/geloeschte ID, API-Huster
            return None

    async def _attach_avatars(self, rows, guild):
        """Laedt die Discord-Profilbilder der Top-Spieler (parallel, mit Cache &
        Timeout) und haengt die Bytes als row['avatar'] an. Faellt etwas aus, bleibt
        es leer - das Bild zeigt dann einen Initial-Platzhalter (nie ein Crash)."""

        async def one(row):
            uid = int(row.get("id") or 0)
            if not uid:
                return
            now = time.monotonic()
            hit = self._AVATAR_CACHE.get(uid)
            if hit and now - hit[1] < self._AVATAR_TTL:
                row["avatar"] = hit[0]
                return
            if self._AVATAR_FAIL.get(uid, 0.0) > now:
                return  # gerade erst fehlgeschlagen -> kurz Ruhe geben
            user = await self._resolve_avatar_user(guild, uid)
            if user is None:
                self._AVATAR_FAIL[uid] = now + self._AVATAR_FAIL_TTL
                return
            try:
                data = await asyncio.wait_for(
                    user.display_avatar.with_size(64).read(), timeout=5)
            except Exception:  # noqa: BLE001 - Profilbild ist nur Deko
                self._AVATAR_FAIL[uid] = now + self._AVATAR_FAIL_TTL
                return
            self._AVATAR_CACHE[uid] = (data, now)
            row["avatar"] = data

        await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)

    async def _leaderboard(self, guild=None):
        """Bestenliste als Grafana-artiges PNG (wenn Pillow da ist), sonst als Embed.
        Laedt die Profilbilder der Top-Spieler mit ins Bild."""
        rows = self.leaderboard_data(10)
        if rows and leaderboard_img.is_available():
            try:
                try:
                    import luxus
                    luxus.decorate_rows(rows)   # Thron-/Kronen-Deko (nur Optik)
                except Exception:  # noqa: BLE001
                    pass
                await self._attach_avatars(rows, guild)
                stand = datetime.now(self._tz).strftime("Stand: %d.%m.%Y %H:%M")
                png = leaderboard_img.render_png(rows, subtitle=stand)
                if png:
                    return discord.File(io.BytesIO(png), filename="leaderboard.png")
            except Exception:  # noqa: BLE001 - Bild ist nice-to-have, niemals fatal
                log.exception("Leaderboard-Bild fehlgeschlagen - nutze Embed")
        return self._leaderboard_embed()

    def _leaderboard_embed(self):
        ranking = sorted(self._users().items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
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
                f"{marker} **{name}** — Lvl {self._level_only(prof['xp'])} ({prof['xp']} XP){suffix}"
            )
        emb.description = "\n".join(lines)
        emb.set_footer(text=f"Schreiben & Voice bringt XP   ·   {self._bot_name} level für deine Karte")
        return emb

    def _inventory_embed(self, member, prof, owned):
        current = prof.get("title") or "—"
        color = discord.Color.blurple()
        if owned:
            best = max(owned, key=lambda o: titles.RANK.get(o.get("rarity", "normal"), 0))
            color = discord.Color(titles.RARITY[best.get("rarity", "normal")]["color"])
        emb = discord.Embed(
            title=f"🎒 Inventar von {member.display_name}",
            description=f"Getragener Titel: **{current}**",
            color=color,
        )
        try:
            emb.set_thumbnail(url=member.display_avatar.url)
        except Exception:  # noqa: BLE001
            pass
        if not owned:
            emb.add_field(name="Noch leer",
                          value=f"Kauf dir einen Titel im `{self._bot_name} shop`!", inline=False)
            return emb
        by = {r: [] for r in titles.RARITY_ORDER}
        for o in owned:
            by.setdefault(o.get("rarity", "normal"), []).append(o)
        for r in reversed(titles.RARITY_ORDER):   # legendaer zuerst
            bucket = by.get(r) or []
            if not bucket:
                continue
            meta = titles.RARITY[r]
            lines = []
            for o in bucket[:12]:
                worn = " ✅" if o.get("label") == prof.get("title") else ""
                lines.append(f"{o.get('label', o.get('text', '?'))}{worn}")
            if len(bucket) > 12:
                lines.append(f"…und {len(bucket) - 12} weitere")
            emb.add_field(name=f"{meta['emoji']} {meta['label']} ({len(bucket)})",
                          value="\n".join(lines), inline=False)
        emb.set_footer(text=f"Titel wechseln: Dropdown unten  ·  Ablegen: {self._bot_name} titel ab")
        return emb

    async def _inventory(self, message):
        member = message.author
        prof = self._profile(member.id)
        owned = self._owned_list(prof)
        emb = self._inventory_embed(member, prof, owned)
        if not owned:
            return emb
        view = _InventoryView(member.id, owned)
        try:
            msg = await message.reply(embed=emb, view=view, mention_author=False)
        except discord.HTTPException:
            log.exception("Inventar konnte nicht gesendet werden")
            return HANDLED
        view.message = msg
        self._protect(msg)
        return HANDLED

    async def _daily(self, member):
        prof = self._profile(member.id)
        today = self._today()
        if prof.get("last_daily") == today:
            return "🕒 Deinen Tagesbonus hast du heute schon abgeholt. Komm morgen wieder!"
        # Streak: gestern abgeholt -> +1, sonst zurueck auf 1.
        yesterday = (datetime.now(self._tz).toordinal() - 1)
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
        self._record_trade(member.id, total, "daily", prof["coins"])
        await self._flush()
        return (f"🎁 Tagesbonus: **+{total} {self.COIN}**! "
                f"(Streak: {prof['streak']} Tag(e), Bonus +{bonus})")

    async def _pay(self, message):
        if not message.mentions:
            return f"So geht's: `{self._bot_name} pay @jemand 100`"
        # Empfaenger = erste @-Erwaehnung IN DER REIHENFOLGE DES TEXTES. message.mentions
        # ist unsortiert und enthaelt bei einer Antwort-mit-Ping auch den Autor der
        # beantworteten Nachricht - deshalb die ID aus dem geschriebenen Text ziehen.
        by_id = {u.id: u for u in message.mentions}
        ziel = None
        for token in re.findall(r"<@!?(\d+)>", message.content or ""):
            u = by_id.get(int(token))
            if u is not None:
                ziel = u
                break
        ziel = ziel or message.mentions[0]
        if ziel.id == message.author.id:
            return "Dir selbst Geld geben? Netter Versuch. 😄"
        if ziel.bot:
            return "Bots brauchen kein Geld. 🤖"
        # Betrag: auch '1k', '2,5k', '1m' usw. (erster passender Token).
        rest = re.sub(r"<@!?\d+>", " ", self._clean_lead(message.content or ""))
        betrag = next((self.parse_amount(t) for t in rest.split()
                       if self.parse_amount(t) is not None), None)
        if betrag is None:
            return f"Wie viel denn? `{self._bot_name} pay @{ziel.display_name} 100` (auch `1k`)"
        if self.get_coins(message.author.id) < betrag:
            return f"Du hast nicht genug. Kontostand: {self.get_coins(message.author.id)} {self.COIN}."
        self.add_coins(message.author.id, -betrag, reason="pay")
        self.add_coins(ziel.id, betrag, reason="pay")
        await self._flush()
        return f"✅ {message.author.display_name} → {ziel.display_name}: **{betrag} {self.COIN}**."

    async def _ensure_shop(self):
        """Sorgt dafuer, dass der heutige Shop existiert (sonst neu wuerfeln+speichern)."""
        st = self._shop_state()
        if st.get("date") != self._today() or not st.get("items"):
            return await self.refresh_shop_async(force=False)
        return st

    def _shop_embed(self, items, *, with_fields = False):
        """Shop-Embed. Normalfall: schlank – die Titel zeigt das Banner-BILD. Nur als
        Notfall (Bild liess sich nicht rendern) werden die Titel als Textfelder
        nachgereicht (with_fields=True)."""
        rar_best = max(items, key=lambda e: titles.RANK.get(e["rarity"], 0))["rarity"]
        emb = discord.Embed(
            title="🛒 Flo Shop — Titel des Tages",
            description=("Jeden Tag um **2 Uhr** frische Titel. Je seltener, desto edler "
                         "die Farbe – und desto **entspannter quatscht Flo** mit dir.\n"
                         "**Kaufen:** unten im Dropdown auswählen. 👇"),
            color=discord.Color(titles.RARITY[rar_best]["color"]),
        )
        if with_fields:
            for e in items:
                meta = titles.RARITY[e["rarity"]]
                emb.add_field(
                    name=f"{e['n']}. {e['label']}",
                    value=f"{meta['emoji']} **{meta['label']}** · 💰 {e['price']} {self.COIN}",
                    inline=True,
                )
            while len(emb.fields) % 3 != 0:
                emb.add_field(name="​", value="​", inline=True)
        emb.set_footer(text=f"{len(items)} Titel heute · beim Kauf gibt's die farbige Rarity-Rolle")
        return emb

    def _shop_banner_file(self, items, date):
        """Optionales Shop-Banner (render.shop_banner). Faellt sauber aus, wenn der
        Renderer (noch) nicht da ist."""
        fn = getattr(render, "shop_banner", None)
        if not callable(fn):
            return None
        try:
            buf = fn(items, date=date)
        except Exception:  # noqa: BLE001 - Bild ist nice-to-have, nie fatal
            log.exception("Shop-Banner fehlgeschlagen - nutze Embed ohne Bild")
            return None
        if buf is None:
            return None
        return discord.File(buf, filename="shop.png")

    async def _shop(self, message):
        st = await self._ensure_shop()
        items = st.get("items", [])
        if not items:
            return discord.Embed(
                title="🛒 Flo Shop",
                description="Der Shop ist gerade leer – schau gleich nochmal rein.",
                color=discord.Color.blurple())
        file = self._shop_banner_file(items, st.get("date", ""))
        emb = self._shop_embed(items, with_fields=(file is None))
        if file is not None:
            emb.set_image(url="attachment://shop.png")
        view = _ShopView(items)
        try:
            kwargs = {"embed": emb, "view": view, "mention_author": False}
            if file is not None:
                kwargs["file"] = file
            msg = await message.reply(**kwargs)
        except discord.HTTPException:
            log.exception("Shop konnte nicht gesendet werden")
            return HANDLED
        view.message = msg
        self._protect(msg)
        return HANDLED

    async def _buy_text(self, member, parts):
        st = await self._ensure_shop()
        items = st.get("items", [])
        arg = parts[1] if len(parts) > 1 else ""
        if not arg.isdigit():
            return (f"Welche Nummer? z. B. `{self._bot_name} kaufen 1` – oder öffne den "
                    f"`{self._bot_name} shop` und nimm das Dropdown.")
        n = int(arg)
        e = next((x for x in items if x.get("n") == n), None)
        if not e:
            return f"Nummer {n} gibt's heute nicht. Schau in den `{self._bot_name} shop`."
        return await self._do_buy(member, e)

    async def _equip(self, member, low):
        parts = low.split()
        name = " ".join(parts[1:]).strip()
        prof = self._profile(member.id)
        if name in ("ab", "aus", "weg", "kein", "keinen", "none", "off"):
            prof["title"] = ""
            prof["title_rarity"] = ""
            await self._sync_role(member)
            await self._flush()
            return "Titel abgelegt – du trägst jetzt keinen. 🫥"
        if not name:
            return (f"Welchen Titel? Öffne `{self._bot_name} inventar` und wähl ihn im "
                    f"Dropdown (oder `{self._bot_name} titel ab` zum Ablegen).")
        owned = self._owned_list(prof)
        o = (next((x for x in owned if x.get("text", "").lower() == name), None)
             or next((x for x in owned if name in x.get("text", "").lower()), None))
        if not o:
            return (f"Den Titel **{name}** besitzt du nicht. Öffne `{self._bot_name} inventar` "
                    f"und wähl im Dropdown.")
        if prof.get("title") == o.get("label"):
            return f"Du trägst **{o.get('label')}** bereits. 😎"
        prof["title"] = o.get("label")
        prof["title_rarity"] = o.get("rarity", "")
        await self._sync_role(member)
        await self._flush()
        return f"✅ Titel gewechselt: Du trägst jetzt **{o.get('label')}**."


# --- Interaktive Shop-/Inventar-Views ------------------------------------
class _ShopBuySelect(discord.ui.Select):
    def __init__(self, items):
        opts = []
        for e in items:
            meta = titles.RARITY[e["rarity"]]
            opts.append(discord.SelectOption(
                label=f"{e['n']}. {e['text']}"[:100],
                value=str(e["n"]),
                description=f"{meta['label']} · {e['price']} {Economy.COIN}"[:100],
            ))
        super().__init__(placeholder="Titel kaufen…", min_values=1, max_values=1,
                         options=opts, row=0)

    async def callback(self, interaction):
        await self.view._buy(interaction, int(self.values[0]))


class _ShopView(discord.ui.View):
    """Geteilter Tages-Shop: jeder kann fuer sich selbst kaufen."""

    def __init__(self, items):
        super().__init__(timeout=180)
        self.message = None
        self.items = {e["n"]: e for e in items}
        self.add_item(_ShopBuySelect(items))

    async def _buy(self, interaction, n):
        e = self.items.get(n)
        if not e:
            await interaction.response.send_message(
                "Diesen Titel gibt's nicht mehr.", ephemeral=True)
            return
        try:
            text = await instance._do_buy(interaction.user, e)
        except Exception:  # noqa: BLE001
            log.exception("Kauf fehlgeschlagen")
            text = "Da ist beim Kauf etwas schiefgelaufen. Versuch's gleich nochmal."
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="Luxus", emoji="🏆", style=discord.ButtonStyle.primary, row=1)
    async def _luxus(self, interaction, _b):
        """Bruecke in den Luxus-Shop (Prestige-Katalog + Thron)."""
        try:
            import luxus
            if not luxus.is_enabled():
                raise RuntimeError("luxus aus")
            view = luxus._LuxusView(interaction.user.id)
            await interaction.response.send_message(
                embed=luxus._luxus_embed(interaction.user.id), view=view)
            view.message = await interaction.original_response()
            instance._protect(view.message)
        except Exception:  # noqa: BLE001
            log.exception("Luxus-Bruecke fehlgeschlagen")
            try:
                await interaction.response.send_message(
                    f"`{instance._bot_name} luxus` öffnet den Luxus-Shop.", ephemeral=True)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            instance._release(self.message)


class _EquipSelect(discord.ui.Select):
    def __init__(self, owned):
        opts = []
        seen = set()
        for o in owned:
            t = o.get("text", "")
            if not t or t in seen:
                continue
            seen.add(t)
            meta = titles.RARITY.get(o.get("rarity", "normal"), titles.RARITY["normal"])
            opts.append(discord.SelectOption(
                label=t[:100], value=t[:100], description=meta["label"]))
            if len(opts) >= 25:
                break
        super().__init__(placeholder="Titel anlegen…", min_values=1, max_values=1,
                         options=opts, row=0)

    async def callback(self, interaction):
        await self.view._equip(interaction, self.values[0])


class _InventoryView(discord.ui.View):
    def __init__(self, uid, owned):
        super().__init__(timeout=180)
        self.uid = uid
        self.message = None
        self._owned = {o.get("text"): o for o in owned}
        self.add_item(_EquipSelect(owned))

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message(
                "Das ist nicht dein Inventar. 🙂", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction):
        prof = instance._profile(self.uid)
        emb = instance._inventory_embed(interaction.user, prof, instance._owned_list(prof))
        await interaction.response.edit_message(embed=emb, view=self)

    async def _equip(self, interaction, text):
        prof = instance._profile(self.uid)
        o = self._owned.get(text)
        if not o:
            await interaction.response.send_message(
                "Den Titel hast du nicht.", ephemeral=True)
            return
        prof["title"] = o.get("label")
        prof["title_rarity"] = o.get("rarity", "")
        await instance._sync_role(interaction.user)
        await instance._flush()
        await self._refresh(interaction)

    @discord.ui.button(label="Titel ablegen", emoji="🫥",
                       style=discord.ButtonStyle.secondary, row=1)
    async def _unequip(self, interaction,
                       button):
        prof = instance._profile(self.uid)
        prof["title"] = ""
        prof["title_rarity"] = ""
        await instance._sync_role(interaction.user)
        await instance._flush()
        await self._refresh(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            instance._release(self.message)


# --- Singleton + Modul-API -------------------------------------------------
# Eine Instanz pro Prozess; die bisherigen Modul-Aufrufe (economy.setup(),
# economy.add_coins() usw.) funktionieren ueber die Aliase unveraendert weiter.
instance = Economy()

COIN = Economy.COIN
XP_PER_MSG = Economy.XP_PER_MSG
COINS_PER_MSG = Economy.COINS_PER_MSG
MSG_COOLDOWN = Economy.MSG_COOLDOWN
XP_PER_VOICE_TICK = Economy.XP_PER_VOICE_TICK
VOICE_TICK_SECONDS = Economy.VOICE_TICK_SECONDS
LEVELUP_CHANNEL_ID = Economy.LEVELUP_CHANNEL_ID
LEVELUP_EMBED_TITLE = Economy.LEVELUP_EMBED_TITLE
SHOP_SIZE = Economy.SHOP_SIZE
LEGACY_SHOP = Economy.LEGACY_SHOP
LEGACY_RARITY = Economy.LEGACY_RARITY

setup = instance.setup
is_enabled = instance.is_enabled
flush = instance.flush
add_xp = instance.add_xp
add_coins = instance.add_coins
get_coins = instance.get_coins
parse_amount = instance.parse_amount
display_name_of = instance.display_name_of
get_title = instance.get_title
get_user_rarity = instance.get_user_rarity
get_tone = instance.get_tone
refresh_shop = instance.refresh_shop
refresh_shop_async = instance.refresh_shop_async
get_shop_items = instance.get_shop_items
ensure_roles = instance.ensure_roles
on_message = instance.on_message
tick_voice = instance.tick_voice
handle = instance.handle
leaderboard_data = instance.leaderboard_data
_attach_avatars = instance._attach_avatars
_resolve_avatar_user = instance._resolve_avatar_user
