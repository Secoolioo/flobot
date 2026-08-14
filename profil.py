"""Discord-Profil nachschlagen: 'Flo check @wer'.

Zeigt alles, was ueber ein Discord-Profil herauszufinden ist - allen voran das
PROFILBILD in voller Aufloesung (4096 px) und UNBESCHNITTEN. Der runde Zuschnitt
ist reine Anzeige im Client; die CDN-Datei ist immer das volle Quadrat. Als
Embed-BILD (set_image) zeigt Discord es rechteckig, als Thumbnail oder
Autor-Icon rund - deshalb steht das Bild hier gross unten und nicht als
Miniatur oben.

Befehle:
    Flo check @wer      ganzes Profil (auch: profil, whois, userinfo, wer ist)
    Flo avatar @wer     nur das Bild, gross (auch: pb, pfp, profilbild)
    Flo banner @wer     nur das Banner

Ziel finden geht ueber Erwaehnung, rohe ID, Antwort auf eine Nachricht - oder
gar nichts, dann bist du selbst gemeint.

WAS GEHT UND WAS NICHT (nachgemessen, nicht geraten):

* Eine ERWAEHNUNG liefert ein VOLLSTAENDIGES Member-Objekt, ohne einen einzigen
  API-Aufruf: Discord schickt bei jeder Nachricht das 'member'-Feld zu jeder
  Erwaehnung mit, discord.py baut daraus ein echtes Member (message.py,
  _handle_mentions -> Member._try_upgrade). Beitrittsdatum, Rollen, Nickname,
  Boost-Beginn und Server-Avatar sind damit sofort da - obwohl der Bot das
  privilegierte Members-Intent NICHT hat.
* Das globale BANNER gibt es ausschliesslich ueber Client.fetch_user() (so
  steht es in der Bibliothek: "only available via Client.fetch_user"). Das ist
  ein echter REST-Aufruf pro Person, den discord.py NICHT zwischenspeichert -
  deshalb der eigene Cache hier unten.
* ONLINE-STATUS UND AKTIVITAET STEHEN BEWUSST NICHT DRIN. Ohne das
  presences-Intent meldet member.status IMMER 'offline' und activities ist
  IMMER leer. Das waere kein Fehler, den man sieht, sondern eine stille
  Falschaussage - Flo wuerde jeden als offline ausgeben. Lieber gar nichts.
* Einen NAMENSVERLAUF gibt die Discord-API nicht her - fuer niemanden, mit
  keinem Intent. Flo fuehrt deshalb seinen eigenen: bei jeder Nachricht wird
  verglichen, ob sich Handle, Anzeigename oder Server-Nickname geaendert haben,
  und nur DANN ein Eintrag geschrieben. Der Verlauf beginnt also mit dem Tag,
  an dem diese Funktion aktiv wurde - was das Panel auch dazuschreibt.
  (Ueber Events ginge es gar nicht: on_user_update/on_member_update kommen
  ausschliesslich aus GUILD_MEMBER_UPDATE und brauchen das Members-Intent.)
"""

import logging
import os
import re
import time

import discord

import ai
import numfmt
from store import JsonStore

log = logging.getLogger("dcbot.profil")

HANDLED = object()

# So lange gilt ein per fetch_user geholtes Profil (Banner/Akzentfarbe) als
# frisch. Der Aufruf kostet jedes Mal einen echten Request, und ein Banner
# aendert sich nicht im Minutentakt.
FETCH_TTL = float(os.getenv("PROFIL_FETCH_TTL", "1800") or "1800")
# Nach einem Fehlschlag (geloeschtes Konto, API-Huster) kurz Ruhe geben.
FETCH_FAIL_TTL = 300.0
# So viele Konten behalten wir hoechstens im Kurzzeit-Speicher.
FETCH_MAX = 200
# Abstand zwischen zwei Nachschlagen JE PERSON. Ein Aufruf loest bis zu zwei
# REST-Aufrufe aus - ohne Bremse kann man den Bot damit ins Rate-Limit treiben.
COOLDOWN = float(os.getenv("PROFIL_COOLDOWN", "4") or "4")

# So viele Namen je Art werden aufgehoben.
VERLAUF_MAX = int(os.getenv("PROFIL_VERLAUF_MAX", "12") or "12")

_ID_RE = re.compile(r"\b(\d{15,20})\b")

# Die groesste Groesse, die Discord ausliefert. Muss eine Zweierpotenz zwischen
# 16 und 4096 sein - alles andere wirft ValueError.
MAX_PX = 4096

_CHECK_CMDS = ("check", "profil", "profile", "userinfo", "whois", "lookup",
               "nachschlagen", "steckbrief", "user")
_AVATAR_CMDS = ("avatar", "pb", "pfp", "profilbild", "bild", "av")
_BANNER_CMDS = ("banner", "profilbanner")

# public_flags -> (Emoji, deutscher Name). Reihenfolge = Anzeige-Reihenfolge.
_BADGES = (
    ("staff", "🛡️", "Discord-Mitarbeiter"),
    ("partner", "🤝", "Partner-Server-Inhaber"),
    ("discord_certified_moderator", "🎓", "Zertifizierter Moderator"),
    ("hypesquad", "🎉", "HypeSquad Events"),
    ("hypesquad_bravery", "🏠", "HypeSquad Bravery"),
    ("hypesquad_brilliance", "🏠", "HypeSquad Brilliance"),
    ("hypesquad_balance", "🏠", "HypeSquad Balance"),
    ("bug_hunter", "🐛", "Bug-Jäger"),
    ("bug_hunter_level_2", "🐛", "Bug-Jäger Gold"),
    ("early_supporter", "💜", "Früher Unterstützer"),
    ("active_developer", "🛠️", "Aktiver Entwickler"),
    ("early_verified_bot_developer", "⚙️", "Früher Bot-Entwickler"),
    ("verified_bot", "✅", "Verifizierter Bot"),
    ("team_user", "👥", "Team-Konto"),
    ("system", "⚙️", "System-Konto"),
    ("spammer", "🚩", "Von Discord als Spam markiert"),
)


class Profil:
    """Profil-Lookup: Discord-Daten, Flos eigene Daten, eigener Namensverlauf."""

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        self._store = None
        self._dirty = False
        # uid -> (User-Objekt|None, gueltig_bis) aus fetch_user (Banner!).
        self._voll = {}
        # uid -> Zeitpunkt, ab dem wieder nachgeschlagen werden darf.
        self._cooldown = {}

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("PROFIL_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Profil-Lookup aus (PROFIL_ENABLED=0).")
            return False
        self._store = JsonStore("profil.json", default={"users": {}, "seit": 0})
        if not self._store.data.get("seit"):
            # Ab wann Flo ueberhaupt mitschreibt - das gehoert unter jeden
            # Verlauf, sonst liest sich "1 Name bekannt" wie eine Aussage
            # ueber die Person statt ueber Flos Gedaechtnis.
            self._store.data["seit"] = int(time.time())
        self._enabled = True
        log.info("Profil-Lookup aktiv (%d Profile im Namensverlauf, seit %s).",
                 len(self._users()),
                 time.strftime("%d.%m.%Y", time.localtime(self._store.data["seit"])))
        return True

    def is_enabled(self):
        return self._enabled

    def _users(self):
        if self._store is None:
            return {}
        return self._store.data.setdefault("users", {})

    def seit(self):
        """Seit wann Flo Namen mitschreibt (Unix-Zeit, 0 = unbekannt)."""
        if self._store is None:
            return 0
        return int(self._store.data.get("seit", 0) or 0)

    # --- Namensverlauf ----------------------------------------------------
    def notiere(self, wer, guild_id=0):
        """Schreibt Namensaenderungen mit. bot.py ruft das fuer JEDE Nachricht auf.

        Bewusst billig: im Normalfall drei Zeichenketten-Vergleiche und sonst
        nichts. Geschrieben wird nur, wenn sich wirklich etwas geaendert hat -
        gespeichert dann gesammelt (siehe flush)."""
        if not self._enabled or self._store is None or wer is None:
            return False
        try:
            uid = int(getattr(wer, "id", 0) or 0)
        except (TypeError, ValueError):
            return False
        if not uid or getattr(wer, "bot", False):
            return False

        eintrag = self._users().get(str(uid))
        if eintrag is None:
            eintrag = self._users()[str(uid)] = {
                "handle": [], "anzeige": [], "nick": {},
                "erst": int(time.time()), "letzt": 0,
            }
        jetzt = int(time.time())
        eintrag["letzt"] = jetzt

        geaendert = False
        # 1) @handle (user.name) - der eindeutige Discord-Name.
        geaendert |= self._merke(eintrag.setdefault("handle", []),
                                 getattr(wer, "name", "") or "", jetzt)
        # 2) Anzeigename (global_name) - der frei waehlbare Name.
        geaendert |= self._merke(eintrag.setdefault("anzeige", []),
                                 getattr(wer, "global_name", None) or "", jetzt)
        # 3) Server-Nickname - gilt nur hier, also je Server getrennt.
        gid = int(guild_id or getattr(getattr(wer, "guild", None), "id", 0) or 0)
        if gid:
            nick = getattr(wer, "nick", None)
            if nick is not None or str(gid) in eintrag.get("nick", {}):
                geaendert |= self._merke(
                    eintrag.setdefault("nick", {}).setdefault(str(gid), []),
                    nick or "", jetzt)

        if geaendert:
            self._dirty = True
        return geaendert

    @staticmethod
    def _merke(liste, name, jetzt):
        """Haengt einen Namen an, WENN er neu ist. True = es gab eine Aenderung."""
        name = (name or "").strip()
        if liste and (liste[-1].get("n") or "") == name:
            return False
        if not liste and not name:
            return False        # von "nichts" auf "nichts" ist keine Aenderung
        liste.append({"n": name, "t": jetzt})
        del liste[:-VERLAUF_MAX]
        return True

    async def flush(self):
        """Gesammelt speichern. bot.py ruft das im ruhigen Takt auf - eine
        Namensaenderung ist selten, ein Schreibvorgang je Nachricht waere
        Verschwendung."""
        if not self._enabled or self._store is None or not self._dirty:
            return False
        self._dirty = False
        try:
            await self._store.save()
        except Exception:  # noqa: BLE001 - ein Namensverlauf ist nie fatal
            log.exception("Namensverlauf konnte nicht gespeichert werden")
            return False
        return True

    def verlauf(self, uid, guild_id=0):
        """Alle bekannten Namen: (Handles, Anzeigenamen, Nicknames) als Listen
        von (Name, Zeitpunkt) - juengster zuletzt."""
        eintrag = self._users().get(str(uid)) or {}

        def raus(liste):
            return [((e.get("n") or ""), int(e.get("t", 0) or 0))
                    for e in (liste or [])]

        nicks = (eintrag.get("nick") or {}).get(str(int(guild_id or 0)), [])
        return raus(eintrag.get("handle")), raus(eintrag.get("anzeige")), raus(nicks)

    def bekannt_seit(self, uid):
        """Wann Flo diese Person das erste Mal gesehen hat (0 = nie)."""
        return int((self._users().get(str(uid)) or {}).get("erst", 0) or 0)

    # --- Discord-Daten holen ----------------------------------------------
    async def _voll_user(self, uid):
        """User-Objekt MIT Banner und Akzentfarbe (fetch_user), gepuffert.

        Nur dieser Weg liefert das globale Banner - die Bibliothek sagt es
        ausdruecklich. discord.py puffert das Ergebnis nicht, also machen wir
        es: sonst kostet jedes 'Flo check' erneut einen Request."""
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return None
        jetzt = time.monotonic()
        treffer = self._voll.get(uid)
        if treffer is not None and treffer[1] > jetzt:
            return treffer[0]
        try:
            import bot
            user = await bot.client.fetch_user(uid)
        except Exception:  # noqa: BLE001 - geloescht, gesperrt, API-Huster
            log.debug("fetch_user(%s) fehlgeschlagen", uid, exc_info=True)
            self._merken(uid, None, jetzt + FETCH_FAIL_TTL)
            return None
        self._merken(uid, user, jetzt + FETCH_TTL)
        return user

    def _merken(self, uid, user, bis):
        """Legt ein geholtes Profil ab und haelt den Speicher klein."""
        self._voll[uid] = (user, bis)
        if len(self._voll) > FETCH_MAX:
            jetzt = time.monotonic()
            for key in [k for k, v in self._voll.items() if v[1] <= jetzt]:
                self._voll.pop(key, None)
            while len(self._voll) > FETCH_MAX:
                self._voll.pop(next(iter(self._voll)), None)

    async def _ziel(self, message, rest):
        """Wen soll ich nachschlagen? (Objekt, Hinweistext-falls-Problem).

        Reihenfolge: Erwaehnung -> rohe ID -> beantwortete Nachricht -> ich
        selbst. Eine Erwaehnung ist der beste Fall: sie bringt ein komplettes
        Member-Objekt mit, ganz ohne API-Aufruf."""
        eigene_id = getattr(getattr(message, "guild", None), "me", None)
        eigene_id = getattr(eigene_id, "id", 0)
        for m in (getattr(message, "mentions", None) or []):
            # '@Flo check @wer' - die Erwaehnung des Bots ist der Trigger,
            # nicht das Ziel. Steht nur Flo da, ist Flo gemeint.
            if getattr(m, "id", 0) != eigene_id:
                return m, ""
        if getattr(message, "mentions", None):
            return message.mentions[0], ""

        treffer = _ID_RE.search(rest or "")
        if treffer:
            return await self._per_id(message, int(treffer.group(1)))

        ref = getattr(message, "reference", None)
        aufgeloest = getattr(ref, "resolved", None) if ref is not None else None
        if isinstance(aufgeloest, discord.Message):
            return aufgeloest.author, ""

        if (rest or "").strip():
            gefunden = self._per_name(message.guild, rest.strip())
            if gefunden is not None:
                return gefunden, ""
            # Ohne das Members-Intent kennt Flo nur einen Teil der Leute.
            # Das ehrlich sagen, statt "gibt es nicht" zu behaupten.
            return None, (
                f"Ich finde hier niemanden mit „{rest.strip()[:60]}“. "
                f"Nimm eine **@Erwähnung** oder die **ID** – nach dem Namen "
                f"suchen kann ich nur bei Leuten, die ich gerade sehe.")

        return message.author, ""

    async def _per_id(self, message, uid):
        """Rohe ID: erst im Server nachsehen, sonst nachladen (2 Wege, 1 Call)."""
        guild = getattr(message, "guild", None)
        member = guild.get_member(uid) if guild is not None else None
        if member is not None:
            return member, ""
        if guild is not None:
            try:
                return await guild.fetch_member(uid), ""
            except Exception:  # noqa: BLE001 - nicht auf dem Server, kein Drama
                log.debug("fetch_member(%s) fehlgeschlagen", uid, exc_info=True)
        user = await self._voll_user(uid)
        if user is not None:
            return user, ""
        return None, f"Zu der ID `{uid}` finde ich kein Discord-Konto."

    @staticmethod
    def _per_name(guild, text):
        """Notnagel: im Member-Cache nach Name/Nickname suchen.

        Der Cache ist ohne das Members-Intent klein (im Wesentlichen Leute im
        Sprachkanal), also ist das ausdruecklich nur ein Bonus - der Weg ueber
        Erwaehnung oder ID ist der verlaessliche."""
        if guild is None:
            return None
        suche = text.lower().lstrip("@")
        for m in (getattr(guild, "members", None) or []):
            for kandidat in (getattr(m, "nick", None), getattr(m, "global_name", None),
                             getattr(m, "name", None)):
                if kandidat and kandidat.lower() == suche:
                    return m
        for m in (getattr(guild, "members", None) or []):
            for kandidat in (getattr(m, "nick", None), getattr(m, "global_name", None),
                             getattr(m, "name", None)):
                if kandidat and suche in kandidat.lower():
                    return m
        return None

    # --- Bild-Adressen ----------------------------------------------------
    @staticmethod
    def _gross(asset, px=MAX_PX):
        """Ein Bild in voller Aufloesung. None, wenn es das Bild nicht gibt.

        Bewusst OHNE Format-Zwang: with_format('png') wuerde ein animiertes
        Avatar zum Standbild machen. discord.py waehlt von selbst .gif fuer
        animierte und .webp fuer statische Bilder."""
        if asset is None:
            return None
        try:
            return asset.with_size(px).url
        except Exception:  # noqa: BLE001 - kaputtes Asset ist kein Grund zu sterben
            return getattr(asset, "url", None)

    @staticmethod
    def _als_png(asset, px=MAX_PX):
        """Dasselbe Bild als PNG - fuer alle, die es weiterverarbeiten wollen."""
        if asset is None:
            return None
        try:
            return asset.with_size(px).with_format("png").url
        except Exception:  # noqa: BLE001
            return None

    def _bilder(self, wer, voll):
        """Alle Bilder einer Person: global, Server-eigen, Banner."""
        global_avatar = getattr(wer, "avatar", None)
        server_avatar = getattr(wer, "guild_avatar", None)
        # Ohne eigenes Bild zeigt Discord das Standard-Muster - das ist auch
        # eins, und es soll nicht als "kein Bild" durchgehen.
        anzeige = getattr(wer, "display_avatar", None)
        banner = getattr(voll, "banner", None) or getattr(wer, "banner", None)
        deko = getattr(wer, "avatar_decoration", None)
        return {
            "anzeige": anzeige,
            "global": global_avatar,
            "server": server_avatar,
            "banner": banner,
            "deko": deko,
            "eigenes": global_avatar is not None or server_avatar is not None,
        }

    # --- Bausteine fuers Embed --------------------------------------------
    @staticmethod
    def _zeit(dt, *, relativ=True):
        """Discord-Zeitstempel: zeigt jedem seine eigene Zeitzone."""
        if dt is None:
            return "—"
        stamp = int(dt.timestamp())
        return f"<t:{stamp}:D>" + (f" (<t:{stamp}:R>)" if relativ else "")

    @staticmethod
    def _badges(wer):
        """Gesetzte Discord-Abzeichen als lesbare Liste."""
        flags = getattr(wer, "public_flags", None)
        if flags is None:
            return []
        raus = []
        for key, emoji, label in _BADGES:
            try:
                if getattr(flags, key, False):
                    raus.append(f"{emoji} {label}")
            except Exception:  # noqa: BLE001 - unbekanntes Flag in neuer Version
                continue
        return raus

    def _nitro_hinweis(self, bilder, wer):
        """Anzeichen fuer Nitro - bewusst als VERMUTUNG formuliert.

        Die API sagt nicht, wer Nitro hat. Animiertes Avatar, Banner, ein
        Avatar-Rahmen oder ein eigenes Server-Avatar gibt es aber nur damit."""
        gruende = []
        avatar = bilder.get("global")
        if avatar is not None and getattr(avatar, "is_animated", None) and avatar.is_animated():
            gruende.append("animiertes Profilbild")
        if bilder.get("banner") is not None:
            gruende.append("Profil-Banner")
        if bilder.get("deko") is not None:
            gruende.append("Avatar-Rahmen")
        if bilder.get("server") is not None:
            gruende.append("eigenes Bild für diesen Server")
        if getattr(wer, "premium_since", None) is not None:
            gruende.append("boostet den Server")
        return gruende

    def _namen_feld(self, wer):
        """Alle Namen, die diese Person gerade traegt."""
        zeilen = [f"**Anzeige:** {getattr(wer, 'display_name', '?')}",
                  f"**Handle:** `@{getattr(wer, 'name', '?')}`"]
        global_name = getattr(wer, "global_name", None)
        if global_name and global_name != getattr(wer, "name", ""):
            zeilen.append(f"**Global:** {global_name}")
        nick = getattr(wer, "nick", None)
        if nick:
            zeilen.append(f"**Auf diesem Server:** {nick}")
        disc = str(getattr(wer, "discriminator", "0") or "0")
        if disc not in ("0", "0000"):
            # Alte Konten, die die Namensumstellung nie mitgemacht haben.
            zeilen.append(f"**Alte Kennung:** `#{disc}`")
        return "\n".join(zeilen)

    def _verlauf_feld(self, wer, guild_id):
        """Flos eigener Namensverlauf - ehrlich beschriftet."""
        handles, anzeigen, nicks = self.verlauf(getattr(wer, "id", 0), guild_id)
        zeilen = []
        for label, liste in (("Handle", handles), ("Anzeige", anzeigen),
                             ("Nickname", nicks)):
            if len(liste) < 2:
                continue        # nur der aktuelle Name = kein Verlauf
            # Juengste Aenderung zuerst, den aktuellen Namen weglassen.
            teile = []
            for name, ts in reversed(liste[:-1]):
                teile.append(f"„{name or '—'}“ (bis <t:{ts}:d>)")
            zeilen.append(f"**{label}:** " + " ← ".join(teile[:4]))
        if not zeilen:
            return None
        return "\n".join(zeilen)

    def _flo_feld(self, wer, guild):
        """Was Flo selbst ueber die Person weiss - jedes Modul liefert seins."""
        uid = getattr(wer, "id", 0)
        zeilen = []
        try:
            import economy
            kurz = economy.kurzprofil(uid)
            if kurz and kurz.get("bekannt"):
                stunden = kurz["voice_secs"] // 3600
                platz = f" · Platz {kurz['platz']}/{kurz['gesamt']}" if kurz.get("platz") else ""
                zeilen.append(f"📈 **Level {kurz['level']}**{platz}")
                zeilen.append(f"💰 {numfmt.fmt(kurz['coins'])} Coins · "
                              f"✉️ {numfmt.fmt(kurz['msgs'])} Nachrichten · "
                              f"🎧 {numfmt.fmt(stunden)} h im Call")
                if kurz.get("titel"):
                    zeilen.append(f"🏷️ Titel: **{kurz['titel']}** "
                                  f"({kurz['titel_anzahl']} im Inventar)")
        except Exception:  # noqa: BLE001 - jedes Modul einzeln, keins darf sprengen
            log.debug("economy-Teil des Profils fehlgeschlagen", exc_info=True)
        try:
            import floaktie
            anteile = floaktie.shares_of(uid)
            if anteile:
                zeilen.append(f"📊 **{numfmt.fmt(anteile)} $FLO** "
                              f"(≈ {numfmt.fmt(anteile * floaktie.price())} Coins)")
        except Exception:  # noqa: BLE001
            log.debug("Aktien-Teil des Profils fehlgeschlagen", exc_info=True)
        try:
            import words
            gesagt, verschieden, top = words.statistik_von(uid)
            if gesagt:
                lieblinge = ", ".join(f"„{w}“ ({numfmt.fmt(n)}×)" for w, n in top)
                zeilen.append(f"🗣️ {numfmt.fmt(gesagt)} Wörter "
                              f"({numfmt.fmt(verschieden)} verschiedene)"
                              + (f" · {lieblinge}" if lieblinge else ""))
        except Exception:  # noqa: BLE001
            log.debug("Wort-Teil des Profils fehlgeschlagen", exc_info=True)
        try:
            import moderation
            warns = moderation.warns_of(getattr(guild, "id", 0), uid)
            if warns:
                zeilen.append(f"⚠️ **{warns}** offene Verwarnung(en)")
        except Exception:  # noqa: BLE001
            log.debug("Moderations-Teil des Profils fehlgeschlagen", exc_info=True)
        seit = self.bekannt_seit(uid)
        if seit:
            zeilen.append(f"👀 Flo kennt dich seit <t:{seit}:D>")
        return "\n".join(zeilen) if zeilen else None

    @staticmethod
    def _rollen_feld(wer):
        """Rollen, hoechste zuerst, ohne @everyone."""
        rollen = [r for r in (getattr(wer, "roles", None) or [])
                  if getattr(r, "name", "") != "@everyone"]
        if not rollen:
            return None
        rollen.reverse()        # discord.py sortiert aufsteigend
        text = " ".join(r.mention for r in rollen[:15])
        if len(rollen) > 15:
            text += f" … *(+{len(rollen) - 15})*"
        return f"**{len(rollen)}** · {text}"

    def _farbe(self, wer, voll):
        """Embed-Farbe: Akzentfarbe des Profils, sonst die hoechste Rollenfarbe."""
        akzent = getattr(voll, "accent_colour", None) or getattr(wer, "accent_colour", None)
        if akzent is not None:
            return akzent
        farbe = getattr(wer, "colour", None)
        if farbe is not None and getattr(farbe, "value", 0):
            return farbe
        return discord.Color.blurple()

    # --- Die Embeds -------------------------------------------------------
    def _profil_embed(self, wer, voll, bilder, guild):
        # Member oder nur User? Ein Member haengt immer an einem Server, ein
        # User nie - das unterscheidet die beiden zuverlaessiger als eine
        # Typpruefung (und laesst sich testen, ohne discord.Member zu bauen).
        ist_member = getattr(wer, "guild", None) is not None
        emb = discord.Embed(color=self._farbe(wer, voll))
        emb.set_author(name=getattr(wer, "display_name", "?"),
                       icon_url=self._gross(bilder["anzeige"], 128) or discord.utils.MISSING)
        emb.title = f"@{getattr(wer, 'name', '?')}"
        kopf = [f"{getattr(wer, 'mention', '')} · `{getattr(wer, 'id', 0)}`"]
        if getattr(wer, "bot", False):
            kopf.append("🤖 **Bot**")
        if getattr(wer, "system", False):
            kopf.append("⚙️ **System-Konto von Discord**")
        emb.description = "\n".join(kopf)

        emb.add_field(name="👤 Namen", value=self._namen_feld(wer), inline=False)

        emb.add_field(name="🎂 Konto erstellt",
                      value=self._zeit(getattr(wer, "created_at", None)), inline=True)
        if ist_member:
            emb.add_field(name="📥 Auf dem Server seit",
                          value=self._zeit(getattr(wer, "joined_at", None)), inline=True)
            boost = getattr(wer, "premium_since", None)
            if boost is not None:
                emb.add_field(name="🚀 Boostet seit", value=self._zeit(boost), inline=True)
        else:
            emb.add_field(name="📥 Auf diesem Server",
                          value="**nein** – nicht (mehr) hier", inline=True)

        badges = self._badges(wer)
        if badges:
            emb.add_field(name="🏅 Abzeichen", value="\n".join(badges), inline=False)

        if ist_member:
            rollen = self._rollen_feld(wer)
            if rollen:
                emb.add_field(name="🎭 Rollen", value=rollen, inline=False)
            if getattr(wer, "is_timed_out", None) and wer.is_timed_out():
                emb.add_field(name="🔇 Timeout",
                              value=f"noch bis {self._zeit(wer.timed_out_until)}",
                              inline=False)
            if getattr(wer, "pending", False):
                emb.add_field(name="⏳ Regeln", value="noch nicht angenommen", inline=True)

        tag = getattr(wer, "primary_guild", None)
        if tag is not None and getattr(tag, "tag", None) and getattr(tag, "identity_enabled", False):
            emb.add_field(name="🏷️ Server-Tag", value=f"`{tag.tag}`", inline=True)

        nitro = self._nitro_hinweis(bilder, wer)
        if nitro:
            emb.add_field(name="💎 Deutet auf Nitro hin",
                          value=", ".join(nitro), inline=False)

        verlauf = self._verlauf_feld(wer, getattr(guild, "id", 0))
        if verlauf:
            emb.add_field(name="📛 Frühere Namen (von Flo mitgeschrieben)",
                          value=verlauf, inline=False)

        flo = self._flo_feld(wer, guild)
        if flo:
            emb.add_field(name=f"📊 Bei {self._bot_name}", value=flo, inline=False)

        links = self._link_zeile(bilder)
        if links:
            emb.add_field(name="🖼️ Bilder in voller Auflösung", value=links, inline=False)

        # DAS BILD: als set_image, nicht als Thumbnail - nur so zeigt Discord
        # es rechteckig und gross statt als runden Kreis.
        gross = self._gross(bilder["anzeige"])
        if gross:
            emb.set_image(url=gross)
        emb.set_footer(text=f"{self._bot_name} · Bild oben in {MAX_PX} px, "
                            f"unbeschnitten · Online-Status zeigt Flo bewusst nicht")
        return emb

    def _link_zeile(self, bilder):
        """Direktlinks - damit man das Bild wirklich herunterladen kann."""
        teile = []
        for key, label in (("global", "Profilbild"), ("server", "Server-Bild"),
                           ("banner", "Banner")):
            asset = bilder.get(key)
            if asset is None:
                continue
            url = self._gross(asset)
            png = self._als_png(asset)
            eintrag = f"[{label}]({url})"
            if png and png != url:
                eintrag += f" · [PNG]({png})"
            teile.append(eintrag)
        if not teile and bilder.get("anzeige") is not None:
            teile.append(f"[Standard-Bild von Discord]({self._gross(bilder['anzeige'])})")
        return " • ".join(teile)

    def _bild_embed(self, wer, asset, titel, farbe):
        """Ein Bild gross und allein - kein Kreis, keine Miniatur."""
        emb = discord.Embed(title=titel, color=farbe)
        url = self._gross(asset)
        emb.set_image(url=url)
        emb.description = f"[Direktlink]({url})"
        png = self._als_png(asset)
        if png and png != url:
            emb.description += f" · [als PNG]({png})"
        if getattr(asset, "is_animated", None) and asset.is_animated():
            emb.description += " · animiert 🎞️"
        emb.set_footer(text=f"{getattr(wer, 'display_name', '?')} · {MAX_PX} px")
        return emb

    @staticmethod
    def _view(bilder):
        """Knoepfe zum Oeffnen - Discord erlaubt nur http(s) in Link-Knoepfen."""
        view = discord.ui.View(timeout=None)
        gebaut = 0
        for key, label, emoji in (("global", "Profilbild", "🖼️"),
                                  ("server", "Server-Bild", "🏠"),
                                  ("banner", "Banner", "🎏")):
            asset = bilder.get(key)
            if asset is None:
                continue
            try:
                url = asset.with_size(MAX_PX).url
            except Exception:  # noqa: BLE001
                continue
            view.add_item(discord.ui.Button(label=label, url=url,
                                            style=discord.ButtonStyle.link, emoji=emoji))
            gebaut += 1
        return view if gebaut else None

    # --- Befehl -----------------------------------------------------------
    def _darf_schon(self, uid):
        """Bremse je Person: ein Aufruf kann zwei REST-Aufrufe ausloesen."""
        jetzt = time.monotonic()
        frei_ab = self._cooldown.get(uid, 0.0)
        if frei_ab > jetzt:
            return False, frei_ab - jetzt
        self._cooldown[uid] = jetzt + COOLDOWN
        if len(self._cooldown) > 500:
            for key in [k for k, v in self._cooldown.items() if v <= jetzt]:
                self._cooldown.pop(key, None)
        return True, 0.0

    async def handle(self, message):
        if not self._enabled or getattr(message, "guild", None) is None:
            return None
        cleaned = ai.strip_lead(message.content or "")
        if not cleaned:
            return None
        teile = cleaned.split()
        erstes = teile[0].lower().strip(".,!?:") if teile else ""
        rest = " ".join(teile[1:])

        if erstes in _CHECK_CMDS:
            art = "profil"
        elif erstes in _AVATAR_CMDS:
            art = "avatar"
        elif erstes in _BANNER_CMDS:
            art = "banner"
        else:
            return None
        # 'user' und 'bild' sind auch normale Woerter - ohne Ziel dahinter ist
        # das eher Gerede als ein Befehl, dann soll die KI ran.
        if erstes in ("user", "bild") and not rest.strip():
            return None

        frei, warten = self._darf_schon(message.author.id)
        if not frei:
            return f"Immer mit der Ruhe – in **{warten:.0f} s** wieder. ⏳"

        wer, problem = await self._ziel(message, rest)
        if wer is None:
            return problem or "Wen soll ich nachschlagen?"

        # Auch das Nachschlagen selbst haelt den Namensverlauf frisch.
        self.notiere(wer, getattr(message.guild, "id", 0))

        voll = await self._voll_user(getattr(wer, "id", 0))
        bilder = self._bilder(wer, voll)
        farbe = self._farbe(wer, voll)

        if art == "avatar":
            embeds = [self._bild_embed(wer, bilder["anzeige"], "🖼️ Profilbild", farbe)]
            if bilder["server"] is not None and bilder["global"] is not None:
                embeds.append(self._bild_embed(wer, bilder["global"],
                                               "🌍 Globales Profilbild", farbe))
        elif art == "banner":
            if bilder["banner"] is None:
                return (f"**{getattr(wer, 'display_name', '?')}** hat kein "
                        f"Profil-Banner. 🎏")
            embeds = [self._bild_embed(wer, bilder["banner"], "🎏 Banner", farbe)]
        else:
            embeds = [self._profil_embed(wer, voll, bilder, message.guild)]
            if bilder["banner"] is not None:
                embeds.append(self._bild_embed(wer, bilder["banner"], "🎏 Banner", farbe))
            if bilder["server"] is not None and bilder["global"] is not None:
                embeds.append(self._bild_embed(wer, bilder["global"],
                                               "🌍 Globales Profilbild", farbe))

        try:
            await message.channel.send(embeds=embeds, view=self._view(bilder)
                                       or discord.utils.MISSING)
        except discord.HTTPException:
            log.exception("Profil-Antwort konnte nicht gesendet werden")
            return "Das Profil konnte ich gerade nicht schicken. 😕"
        return HANDLED


# --- Singleton + Modul-API ---------------------------------------------------
instance = Profil()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
notiere = instance.notiere
flush = instance.flush
verlauf = instance.verlauf
bekannt_seit = instance.bekannt_seit
seit = instance.seit
