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
  zusaetzlich im optionalen Mod-Log-Channel (je Server, guildcfg) als Protokoll.
- Verwarnungen werden in data/moderation.json gespeichert; bei Erreichen von
  WARN_LIMIT setzt es automatisch einen Timeout (WARN_TIMEOUT_SECONDS).
- Purge schuetzt angepinnte Nachrichten und loescht auch >14 Tage alte (einzeln).

Rueckgabe von handle():
- None      -> kein Moderations-Befehl (naechster Handler/KI ist dran).
- str       -> Hinweis/Fehlertext, den bot.py normal als Antwort schickt.
- Embed     -> Erfolg; bot.py schickt das Embed (zusaetzlich Mod-Log).
- HANDLED   -> schon selbst geantwortet (Purge-Bestaetigung), bot.py sendet nichts.
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta

import discord

import ai
from basis import FeatureBasis, echte_erwaehnungen
import guildcfg
from store import JsonStore

log = logging.getLogger("dcbot.mod")

# handle() gibt das zurueck, wenn es den Befehl SELBST erledigt und bereits
# geantwortet hat -> bot.py soll dann nichts mehr senden.
HANDLED = object()

# --- Einstellungen ------------------------------------------------------
# Diese vier Werte stehen jetzt in guildcfg und gelten JE SERVER - sie sind
# damit auch im Web-Panel einstellbar, ohne Neustart. Die .env bleibt der
# Standard (guildcfg.Einstellung liest dieselben Variablen), damit sich ohne
# eigene Einstellung nichts aendert. Die Konstanten hier sind nur noch der
# Rueckfall, wenn guildcfg gar nicht da ist.
def _cfg_zahl(gid, key, standard):
    """Der Wert dieses Servers - bei JEDEM Gebrauch frisch gelesen.

    Frueher waren das Modul-Konstanten aus der .env: eine Aenderung brauchte
    einen Neustart und galt fuer alle Server gleich."""
    try:
        import guildcfg
        wert = guildcfg.get(gid, key)
        return standard if wert is None else int(wert)
    except Exception:  # noqa: BLE001 - ohne guildcfg gilt der alte Standard
        return standard


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
# Fuers LOESCHEN reicht das nicht: 'alle', 'ganz' und 'komplett' sind deutsche
# Fuellwoerter und standen in ganz normalen Saetzen ('loesch das ganz schnell').
# Hier muss das Alles-Wort am ANFANG stehen, damit es zaehlt.
# 'ganz' fehlt hier bewusst: allein steht es im Deutschen fast immer als
# Verstaerker ('ganz kurz', 'ganz schnell'), nicht als Menge. Die Bedeutung
# tragen 'alles' und 'komplett'.
# Alles, was Discord als <...> in den Text schreibt: Erwaehnungen, Kanaele,
# Rollen, Custom-Emojis, Zeitmarken. Steckt voller Ziffern.
_MARKE_RE = re.compile(r"<[^>]{1,64}>")

_ALL_START_RE = re.compile(r"^(?:alles?|all|everything|komplett)\b",
                           re.IGNORECASE)

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
# 'rausschmeisen' (ein s) ist die gaengige Tippweise - ohne sie fiel der
# Befehl durch bis zur Musik, die daraufhin den Sprachkanal verliess.
_KICK_RE = re.compile(
    r"^(?:kick\w*|rauswerf\w*|rausschmei(?:ß|ss|s)\w*)\b", re.IGNORECASE)

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


class Moderation(FeatureBasis):
    def __init__(self):
        self._enabled = False
        self._store = None
        # Offene Rueckfragen vor einem kompletten Channel-Wipe:
        # kanal_id -> (nutzer_id, ablauf). Die Nutzer-ID gehoert unbedingt dazu:
        # ohne sie war die Rueckfrage nur an den KANAL gebunden, und ein
        # anderer Mod, der danach 'loesch alle' schrieb, loeste mit seinem
        # ERSTEN Befehl den kompletten Wipe aus - ohne die Warnung je gesehen
        # zu haben. Nur im Speicher: eine Rueckfrage soll keinen Neustart
        # ueberleben.
        self._wipe_offen = {}

        # Label -> Handler (Purge laeuft gesondert, da es den ganzen Befehl braucht).
        self._HANDLERS = {
            "unwarn": self._do_unwarn,
            "warns": self._do_warns,
            "warn": self._do_warn,
            "untimeout": self._do_untimeout,
            "timeout": self._do_timeout,
            "kick": self._do_kick,
            "unban": self._do_unban,
            "ban": self._do_ban,
        }

    def classify(self, cmd):
        """Welche Moderations-Aktion steckt im (schon um den Botnamen bereinigten)
        Text? Gibt das Label zurueck oder None. Pur testbar - handle() nutzt es."""
        for label, rx in _ROUTES:
            if rx.match(cmd):
                return label
        return None

    def setup(self):
        """Aktiviert das Moderation-Feature. Keine externen Voraussetzungen - die
        noetigen Rechte werden erst beim jeweiligen Befehl geprueft."""
        if os.getenv("MOD_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Moderation-Feature aus (MOD_ENABLED=0).")
            return False
        self._store = JsonStore("moderation.json", default={"warns": {}})
        self._enabled = True
        log.info(
            "Moderation aktiv (Purge bis %d · warn/timeout/kick/ban · "
            "Warn-Limit %d -> %s Timeout · Mod-Log: %s).",
            MAX_PURGE, WARN_LIMIT, self._fmt_duration(WARN_TIMEOUT_SECONDS),
            "je Server",
        )
        return True

    def is_enabled(self):
        return self._enabled

    # --- Routing -------------------------------------------------------------
    async def handle(self, message):
        if not self._enabled or message.guild is None:
            return None
        cmd = ai.strip_lead(message.content or "")
        if not cmd:
            return None

        label = self.classify(cmd)
        if label is None:
            return None
        if label == "purge":
            return await self._do_purge(message, cmd)  # braucht den ganzen Befehl (Zahl/'alle')
        rx = dict(_ROUTES)[label]
        rest = rx.sub("", cmd, count=1).strip()
        return await self._HANDLERS[label](message, rest)

    # --- gemeinsame Helfer ---------------------------------------------------
    def _need(self, label):
        return f"Dafür brauchst du das Recht **{label}**. 🔒"

    def _bot_need(self, label):
        return f"Mir fehlt das Recht **{label}** auf dem Server – das muss mir ein Admin geben. 🔒"

    def _actor_can(self, message, attr):
        p = getattr(message.author, "guild_permissions", None)
        return bool(p and (getattr(p, attr, False) or p.administrator))

    def _bot_can(self, guild, attr):
        p = guild.me.guild_permissions
        return bool(getattr(p, attr, False) or p.administrator)

    def _resolve_target(self, message, rest):
        """Findet das Ziel: erst eine echte @-Erwaehnung (ausser Flo selbst), sonst
        eine rohe 15-20-stellige ID. Gibt (member_or_user|None, id|None, rest_ohne_ziel).
        Bei reiner ID kann member None sein (z. B. fuer Ban/Unban von Nicht-Mitgliedern)."""
        me_id = message.guild.me.id
        # Nur WIRKLICH getippte Erwaehnungen. Bei einer Antwort-mit-Ping steht
        # der Autor der beantworteten Nachricht in message.mentions, ohne dass
        # ihn jemand geschrieben hat - eine Antwort auf Bob mit 'Flo ban spam'
        # hat damit BOB gebannt.
        mentioned = [u for u in echte_erwaehnungen(message) if u.id != me_id]
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

    def _clean_reason(self, rest):
        r = _MENTION_RE.sub("", rest or "").strip()
        r = re.sub(r"^(?:wegen|weil|f(?:ü|ue)r|for|grund|reason)\b[:\s]*", "", r,
                   flags=re.IGNORECASE).strip()
        # Auf 500 deckeln: Discords Audit-Log-Grund erlaubt max. 512 Zeichen, und das
        # Embed-Feld max. 1024 - so kann ein Mega-Grund den Kick/Ban nicht crashen.
        return r.strip(" :–-")[:500]

    def _parse_duration(self, text):
        """Liest die erste Zeitangabe (z. B. '10m', '2 stunden') und gibt
        (sekunden, rest_ohne_dauer). Ohne Treffer: (None, text)."""
        m = _DURATION_RE.search(text)
        if not m:
            return None, text
        secs = int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
        rest = (text[:m.start()] + text[m.end():]).strip()
        return secs, rest

    def _fmt_duration(self, secs):
        secs = int(secs)
        parts = []
        for one, many, size in (("Tag", "Tage", 86400), ("Stunde", "Stunden", 3600),
                                ("Minute", "Minuten", 60), ("Sekunde", "Sekunden", 1)):
            if secs >= size:
                n, secs = divmod(secs, size)
                parts.append(f"{n} {one if n == 1 else many}")
            if len(parts) >= 2:
                break
        return " ".join(parts) if parts else "0 Sekunden"

    def _hierarchy_problem(self, message, member, full = True):
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

    def _bot_hierarchy_ok(self, guild, member):
        """Reicht Flos Rolle, um gegen 'member' vorzugehen? (Fuer den Auto-Timeout.)"""
        if member.id in (guild.me.id, guild.owner_id):
            return False
        return guild.me.top_role > member.top_role

    def _action_embed(self, emoji, titel, color, target, by,
                      reason, extra = None):
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
        emb.set_footer(text=f"{self._bot_name} · Moderation")
        return emb

    async def _modlog(self, message, embed):
        """Schreibt das Aktions-Embed zusaetzlich ins Mod-Log (falls eingerichtet und
        nicht ohnehin derselbe Channel, in dem der Befehl kam)."""
        cid = guildcfg.get(message.guild.id, "modlog_channel")
        if not cid:
            return
        ch = message.guild.get_channel(cid)
        if ch is None or ch.id == message.channel.id or not hasattr(ch, "send"):
            return
        if not ch.permissions_for(message.guild.me).send_messages:
            return
        try:
            await ch.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _apply_timeout(self, guild, member,
                             secs, reason):
        secs = max(1, min(DISCORD_TIMEOUT_MAX, int(secs)))
        try:
            await member.timeout(timedelta(seconds=secs), reason=reason)
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Timeout fehlgeschlagen: %s", exc)
            return False

    # --- Verwarnungen --------------------------------------------------------
    def _warns_for(self, guild_id):
        warns = self._store.data.setdefault("warns", {})
        if not isinstance(warns, dict):
            warns = self._store.data["warns"] = {}
        je_server = warns.get(str(guild_id))
        if not isinstance(je_server, dict):
            je_server = warns[str(guild_id)] = {}
        return je_server

    def warns_of(self, guild_id, uid):
        """Wie viele OFFENE Verwarnungen jemand auf diesem Server hat.

        Fuer fremde Anzeigen (Profil-Lookup). Legt nichts an und braucht keine
        Rechte - die Zahl steht ohnehin unter 'warns'."""
        if not self._enabled or self._store is None:
            return 0
        # Alle drei Tiefen koennen kaputt sein (warns/Server/Nutzer), und das
        # len() lag frueher AUSSERHALB des try - ein "warns": null nahm damit
        # den ganzen Profil-Lookup mit, der diese Zahl abfragt.
        try:
            gw = self._store.data.get("warns") or {}
            lst = (gw.get(str(int(guild_id))) or {}).get(str(int(uid))) or []
            return len(lst)
        except (TypeError, ValueError, AttributeError):
            return 0

    async def _do_warn(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "moderate_members"):
            return self._need("Mitglieder moderieren")
        member, uid, rest = self._resolve_target(message, rest)
        if member is None:
            return (f"Wen soll ich verwarnen? z. B. `{self._bot_name} warn @name Spam`."
                    if uid is None else "Diese Person ist nicht (mehr) auf dem Server.")
        prob = self._hierarchy_problem(message, member, full=False)
        if prob:
            return prob
        reason = self._clean_reason(rest) or "kein Grund angegeben"

        gw = self._warns_for(guild.id)
        lst = gw.setdefault(str(member.id), [])
        lst.append({"by": message.author.id, "reason": reason, "ts": time.time()})
        count = len(lst)

        auto_note = None
        # Der harte Auto-Timeout muss DIESELBE Rangordnung achten wie ein direkter
        # 'timeout'-Befehl. Sonst knebelt ein Junior-Mod per dreimal 'warn' jemanden
        # mit hoeherer Rolle, den er direkt nicht anfassen duerfte - die Verwarnung
        # selbst prueft bewusst nur full=False und bleibt weiterhin moeglich.
        darf_strafen = not self._hierarchy_problem(message, member, full=True)
        grenze = _cfg_zahl(guild.id, "warn_limit", WARN_LIMIT)
        dauer = _cfg_zahl(guild.id, "warn_timeout", WARN_TIMEOUT_SECONDS)
        if (count >= grenze and dauer > 0 and darf_strafen
                and self._bot_can(guild, "moderate_members")
                and isinstance(member, discord.Member) and self._bot_hierarchy_ok(guild, member)):
            if await self._apply_timeout(guild, member, dauer,
                                         f"Auto-Timeout nach {count} Verwarnungen"):
                gw[str(member.id)] = []  # Zaehler nach der Strafe zuruecksetzen
                auto_note = (f"⏳ Limit erreicht → **{self._fmt_duration(dauer)}** "
                             f"Timeout. Zähler zurückgesetzt.")
        await self._store.save()

        emb = self._action_embed("⚠️", "Verwarnung", discord.Color.gold(), member,
                                 message.author, reason, [("Stand", f"**{count}/{grenze}**")])
        if auto_note:
            emb.add_field(name="Folge", value=auto_note, inline=False)
        await self._modlog(message, emb)
        return emb

    async def _do_warns(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "moderate_members"):
            return self._need("Mitglieder moderieren")
        member, uid, rest = self._resolve_target(message, rest)
        if member is None and uid is None:
            return f"Von wem die Verwarnungen? z. B. `{self._bot_name} warns @name`."
        key = str(uid if uid is not None else member.id)
        who = member.mention if member is not None else f"<@{uid}>"
        lst = self._store.data.get("warns", {}).get(str(guild.id), {}).get(key, [])
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
        emb.set_footer(text=f"{self._bot_name} · Moderation")
        return emb

    async def _do_unwarn(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "moderate_members"):
            return self._need("Mitglieder moderieren")
        member, uid, rest = self._resolve_target(message, rest)
        if member is None and uid is None:
            return f"Wem eine Verwarnung erlassen? z. B. `{self._bot_name} unwarn @name` (oder `... alle`)."
        key = str(uid if uid is not None else member.id)
        who = member.mention if member is not None else f"<@{uid}>"
        gw = self._store.data.get("warns", {}).get(str(guild.id), {})
        lst = gw.get(key, [])
        if not lst:
            return f"{who} hat gar keine Verwarnungen. 🤷"
        if _ALL_RE.search(rest):
            gw[key] = []
            was = "Alle Verwarnungen entfernt"
        else:
            lst.pop()
            was = "Letzte Verwarnung entfernt"
        await self._store.save()
        emb = self._action_embed("✅", "Verwarnung erlassen", discord.Color.green(),
                                 member if member is not None else uid, message.author, was,
                                 [("Rest", f"**{len(gw.get(key, []))}/{WARN_LIMIT}**")])
        await self._modlog(message, emb)
        return emb

    # --- Timeout -------------------------------------------------------------
    async def _do_timeout(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "moderate_members"):
            return self._need("Mitglieder moderieren")
        if not self._bot_can(guild, "moderate_members"):
            return self._bot_need("Mitglieder moderieren (Timeout)")
        secs, rest = self._parse_duration(rest)
        member, uid, rest = self._resolve_target(message, rest)
        if member is None:
            return (f"Wen für wie lange? z. B. `{self._bot_name} timeout @name 10m Spam`."
                    if uid is None else "Diese Person ist nicht (mehr) auf dem Server.")
        prob = self._hierarchy_problem(message, member)
        if prob:
            return prob
        standard = _cfg_zahl(guild.id, "timeout_standard", DEFAULT_TIMEOUT_SECONDS)
        secs = max(1, min(DISCORD_TIMEOUT_MAX, secs if secs is not None else standard))
        reason = self._clean_reason(rest) or "kein Grund angegeben"
        if not await self._apply_timeout(guild, member, secs, f"{reason} · von {message.author}"):
            return "Der Timeout hat nicht geklappt (Rechte oder Rollen-Hierarchie?)."
        emb = self._action_embed("🔇", "Timeout", discord.Color.orange(), member,
                                 message.author, reason, [("Dauer", self._fmt_duration(secs))])
        await self._modlog(message, emb)
        return emb

    async def _do_untimeout(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "moderate_members"):
            return self._need("Mitglieder moderieren")
        if not self._bot_can(guild, "moderate_members"):
            return self._bot_need("Mitglieder moderieren (Timeout)")
        member, uid, rest = self._resolve_target(message, rest)
        if member is None:
            return f"Wem den Timeout abnehmen? z. B. `{self._bot_name} untimeout @name`."
        try:
            await member.timeout(None, reason=f"Timeout aufgehoben von {message.author}")
        except (discord.Forbidden, discord.HTTPException):
            return "Konnte den Timeout nicht aufheben (Rechte/Hierarchie?)."
        emb = self._action_embed("🔊", "Timeout aufgehoben", discord.Color.green(), member,
                                 message.author, self._clean_reason(rest) or "—")
        await self._modlog(message, emb)
        return emb

    # --- Kick ----------------------------------------------------------------
    async def _do_kick(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "kick_members"):
            return self._need("Mitglieder kicken")
        if not self._bot_can(guild, "kick_members"):
            return self._bot_need("Mitglieder kicken")
        member, uid, rest = self._resolve_target(message, rest)
        if member is None:
            return (f"Wen rauswerfen? z. B. `{self._bot_name} kick @name Grund`."
                    if uid is None else "Diese Person ist nicht (mehr) auf dem Server.")
        prob = self._hierarchy_problem(message, member)
        if prob:
            return prob
        reason = self._clean_reason(rest) or "kein Grund angegeben"
        try:
            await member.kick(reason=f"{reason} · von {message.author}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Kick fehlgeschlagen: %s", exc)
            return "Der Kick hat nicht geklappt (Rechte/Hierarchie?)."
        emb = self._action_embed("👢", "Gekickt", discord.Color.red(), member,
                                 message.author, reason)
        await self._modlog(message, emb)
        return emb

    # --- Bann ----------------------------------------------------------------
    async def _do_ban(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "ban_members"):
            return self._need("Mitglieder bannen")
        if not self._bot_can(guild, "ban_members"):
            return self._bot_need("Mitglieder bannen")
        member, uid, rest = self._resolve_target(message, rest)
        if member is None and uid is None:
            return f"Wen bannen? z. B. `{self._bot_name} ban @name Grund` (oder per ID)."
        # Bei einer ROHEN ID war bisher jede Pruefung ausgehebelt: der Bot laeuft
        # ohne members-Intent (bot.py: Intents.none()), guild.get_member() liefert
        # deshalb None, 'isinstance(member, Member)' ist False - und die
        # Rangordnung wurde uebersprungen. Gemessen konnte ein Junior-Mod damit
        # einen Senior-Mod, Flo selbst UND sich selbst bannen, einfach indem er
        # die ID statt der Erwaehnung tippte.
        if member is None and uid is not None:
            if uid == guild.me.id:
                return "Mich selbst banne ich nicht. 🙃"
            if uid == getattr(message.author, "id", 0):
                return "Dich selbst bannen? Lieber nicht. 🙃"
            if uid == getattr(guild, "owner_id", 0):
                return "Den Server-Besitzer banne ich nicht. ⛔"
            try:
                member = await guild.fetch_member(uid)   # Cache ist leer, also nachladen
            except discord.NotFound:
                member = None            # wirklich nicht auf dem Server -> ID-Bann ok
            except discord.HTTPException as exc:
                log.warning("Mitglied %s liess sich nicht laden: %s", uid, exc)
                return "Ich konnte die Person gerade nicht prüfen – versuch's nochmal."
        if isinstance(member, discord.Member):
            prob = self._hierarchy_problem(message, member)
            if prob:
                return prob
        reason = self._clean_reason(rest) or "kein Grund angegeben"
        target_obj = member if isinstance(member, (discord.Member, discord.User)) else discord.Object(id=uid)
        try:
            await guild.ban(target_obj, reason=f"{reason} · von {message.author}",
                            delete_message_seconds=0)
        except discord.NotFound:
            return "Diese Nutzer-ID gibt es nicht."
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Ban fehlgeschlagen: %s", exc)
            return "Der Bann hat nicht geklappt (Rechte/Hierarchie?)."
        emb = self._action_embed("🔨", "Gebannt", discord.Color.dark_red(),
                                 member if member is not None else uid, message.author, reason)
        await self._modlog(message, emb)
        return emb

    async def _do_unban(self, message, rest):
        guild = message.guild
        if not self._actor_can(message, "ban_members"):
            return self._need("Mitglieder bannen")
        if not self._bot_can(guild, "ban_members"):
            return self._bot_need("Mitglieder bannen")
        _, uid, rest = self._resolve_target(message, rest)
        if uid is None:
            return f"Welche ID entbannen? z. B. `{self._bot_name} unban 123456789012345678`."
        try:
            await guild.unban(discord.Object(id=uid), reason=f"Entbannt von {message.author}")
        except discord.NotFound:
            return "Diese ID ist gar nicht gebannt."
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Unban fehlgeschlagen: %s", exc)
            return "Das Entbannen hat nicht geklappt."
        emb = self._action_embed("♻️", "Entbannt", discord.Color.green(), uid,
                                 message.author, self._clean_reason(rest) or "—")
        await self._modlog(message, emb)
        return emb

    # --- Aufraeumen / Purge --------------------------------------------------
    def _keep(self, message):
        """True = diese Nachricht NICHT loeschen (angepinnte schuetzen wir)."""
        return bool(message.pinned)

    async def _do_purge(self, message, cmd):
        """Erkennt einen Loesch-Befehl und fuehrt ihn aus (gibt nie None zurueck)."""
        rest = _CMD_RE.sub("", cmd, count=1).strip()

        if not self._actor_can(message, "manage_messages"):
            return "Dafür brauchst du das Recht **Nachrichten verwalten**. 🔒"

        channel = message.channel
        if not hasattr(channel, "purge"):
            return "Hier kann ich nichts löschen."
        me_perms = channel.permissions_for(message.guild.me)
        if not (me_perms.manage_messages and me_perms.read_message_history):
            return ("Mir fehlt hier das Recht **Nachrichten verwalten** "
                    "(und Verlauf lesen) – dann kann ich nichts löschen.")

        # Discord-Marken RAUS, bevor nach einer Zahl gesucht wird. Eine
        # Erwaehnung ist im Text '<@1453881901738889351>' - die Ziffern darin
        # wurden als Anzahl gelesen. Gemessen: 'Flo loesch @spammer' loeschte
        # damit MAX_PURGE Nachrichten, ohne Rueckfrage. Ausgerechnet die
        # harmlos aussehende Form war die gefaehrlichste: 'loesch alle' fragt
        # nach, 'loesch @wer' nicht. Betrifft genauso <#kanal>, <@&rolle>,
        # Custom-Emojis <:name:123> und Zeitmarken <t:123:R>.
        rest_ohne_marken = _MARKE_RE.sub(" ", rest)
        num_match = re.search(r"\d+", rest_ohne_marken)
        # Eine EXPLIZIT genannte Zahl hat Vorrang: 'loesch 20 ganz schnell' loescht
        # 20, nicht den ganzen Channel.
        #
        # Das Alles-Wort muss GANZ VORNE stehen. Vorher genuegte es irgendwo im
        # Satz - und 'alle', 'ganz' und 'komplett' sind deutsche Fuellwoerter.
        # Gemessen loeschten deshalb ALLE diese Saetze den kompletten Channel:
        #   'loesch das ganz schnell'
        #   'loesch mal alle deine Nachrichten'   (gemeint waren Flos Nachrichten)
        #   'loesch ganz kurz bitte'
        # Unwiderruflich, ohne Rueckfrage, mit der Meldung "N Nachrichten geloescht".
        want_all = cmd.lower().startswith("nuke") or (
            bool(_ALL_START_RE.match(rest_ohne_marken.strip())) and num_match is None)

        if want_all:
            # Ein ganzer Channel ist nicht wiederherstellbar - einmal nachfragen.
            jetzt = time.time()
            for cid, eintrag in list(self._wipe_offen.items()):
                if not isinstance(eintrag, tuple) or eintrag[1] <= jetzt:
                    self._wipe_offen.pop(cid, None)
            offen = self._wipe_offen.get(channel.id)
            wer = getattr(message.author, "id", 0)
            # Bestaetigen darf NUR, wer selbst gefragt wurde.
            if not (offen and offen[0] == wer and offen[1] > jetzt):
                self._wipe_offen[channel.id] = (wer, jetzt + 60.0)
                return ("⚠️ Das löscht **den kompletten Verlauf** dieses Channels – "
                        "das lässt sich nicht rückgängig machen.\nSchick den Befehl "
                        "noch einmal, wenn du sicher bist (gilt eine Minute).")
            self._wipe_offen.pop(channel.id, None)

        try:
            if want_all:
                deleted = await channel.purge(limit=None, check=lambda m: not self._keep(m))
                count = len(deleted)
            elif num_match:
                deckel = _cfg_zahl(
                    getattr(getattr(message, "guild", None), "id", 0),
                    "purge_max", MAX_PURGE)
                n = max(1, min(deckel, int(num_match.group())))
                # +1, damit die Befehls-Nachricht selbst nicht als eine der n zaehlt.
                deleted = await channel.purge(limit=n + 1, check=lambda m: not self._keep(m))
                count = max(0, len(deleted) - (0 if self._keep(message) else 1))
            elif rest.strip() and rest_ohne_marken.strip() != rest.strip():
                # Da stand eine Erwaehnung und sonst keine Zahl.
                return (f"Ich lösche nach **Anzahl**, nicht nach Person: "
                        f"`{self._bot_name} lösch 20`. "
                        f"(Für eine einzelne Person: die Nachricht anpinnen "
                        f"oder von Hand löschen.)")
            else:
                return (f"Wie viele? z. B. `{self._bot_name} lösch 20` oder "
                        f"`{self._bot_name} lösch alle`.")
        except discord.Forbidden:
            await self._confirm(channel, "Mir fehlt das Recht zum Löschen. 🔒")
            return HANDLED
        except discord.HTTPException as exc:
            log.warning("Purge fehlgeschlagen: %s", exc)
            await self._confirm(channel, "Das Löschen hat nicht ganz geklappt.")
            return HANDLED

        wort = "Nachricht" if count == 1 else "Nachrichten"
        await self._confirm(channel, f"🧹 **{count}** {wort} gelöscht.")
        log.info(
            "Purge von %s in #%s: %d geloescht (%s).",
            message.author.display_name, getattr(channel, "name", channel.id), count,
            "alle" if want_all else f"max {num_match.group() if num_match else '?'}",
        )
        return HANDLED

    async def _confirm(self, channel, text):
        """Kurze Bestaetigung, die sich nach CONFIRM_TTL Sekunden selbst loescht."""
        try:
            await channel.send(text, delete_after=CONFIRM_TTL)
        except discord.HTTPException:
            pass


instance = Moderation()

# Modul-Aliase, damit die bisherige Modul-API weiter funktioniert.
classify = instance.classify
warns_of = instance.warns_of
setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
