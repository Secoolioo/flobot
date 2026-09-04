"""Flo Luxus: die Coin-Senke fuer Reiche - Prestige-Katalog + DER THRON.

Befehle (nach 'Flo'):
- luxus               Katalog mit Kauf-Menue (50.000 bis 3 MILLIARDEN Coins)
- luxus kaufen <n>    Item Nr. n direkt kaufen (Text-Fallback)
- thron               Der Thron: Unikat! Nur EINER sitzt drauf - wer den
                      aktuellen Preis zahlt, stuerzt den Besitzer. Jede
                      Eroberung macht den Thron 75% teurer.

Effekte (sichtbar, nicht nur Zahlen):
- Rahmen (bronze..galaxie): die Level-Karte ('flo level') bekommt einen
  edlen Rahmen - es zaehlt der beste besessene.
- Koenigskrone: Krone neben dem Namen im Leaderboard ('flo top').
- FLO-IMPERIUM (1 Mrd): alle Rahmen darunter, Krone, eigene Imperator-Rolle
  im Server und die KI spricht dich als Imperator an.
- Jenseits davon: NOVA-AURA (1,6 Mrd), SINGULARITAET (2,2 Mrd) und das
  FLO-MULTIVERSUM (3 Mrd) - das Ende der Leiter.
- Thron-Besitzer: goldene Krone + Zeile im Leaderboard, die KI behandelt
  dich wie Adel - bis dich jemand stuerzt.

Coins laufen ueber economy (ein Topf); Besitz liegt in data/luxus.json.
"""

import logging
import os

import discord

import numfmt

import ai
import basis
from basis import FeatureBasis
import economy
from store import JsonStore

log = logging.getLogger("dcbot.luxus")

# Sentinel: luxus hat selbst geantwortet -> bot.py schweigt.
HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

THRONE_START = 100_000
THRONE_FACTOR = 1.75          # jede Eroberung: Preis x1.75
IMPERATOR_ROLE = "🏰 Imperator"

# Katalog: fest, bewusst KEIN Zufall - das sind Lebensziele. 'rang' ordnet
# die Rahmen (der beste besessene wird angezeigt).
#
# FARBEN muessen sich von den Titel-Stufen (titles.py RARITY) unterscheiden:
# zwei verschiedene Dinge in derselben Farbe sind im Discord nicht auseinander-
# zuhalten. Nova war exakt die Relikt-Farbe, Multiversum exakt die Goettlich-Farbe.
#
# PREISE: aus dem gemessenen Tageseinkommen abgeleitet (economy.py: ~8-10k an
# einem normal aktiven Tag; ein guter Aktien-Vormittag bis ~20 Mio). Jede Stufe
# kostet ungefaehr das Fuenffache der vorherigen - so bleibt bis zum Imperium
# etwas zu tun, ohne dass die erste Stufe unerreichbar wird:
#
#   Bronze     50.000   ~1 Woche normal spielen
#   Silber    250.000   ~1 Monat  (oder 1 guter Aktien-Tag)
#   Gold      1,2 Mio   mehrere gute Aktien-Tage
#   Diamant     6 Mio
#   Krone      25 Mio
#   Galaxie   120 Mio
#   Imperium    1 Mrd   war bis heute das Endziel
#   Nova      1,6 Mrd   ab hier: Monate konsequenter Aktienhandel
#   Singul.   2,2 Mrd
#   Multivers.  3 Mrd   das Ende der Leiter (Steuer-Gleichgewicht ~3,4 Mrd)
ITEMS = [
    {"n": 1, "key": "bronze", "name": "Bronze-Rahmen", "emoji": "🥉",
     "preis": 50_000, "art": "rahmen", "rang": 1, "farbe": 0xCD7F32,
     "desc": "Deine Level-Karte in edlem Bronze."},
    {"n": 2, "key": "silber", "name": "Silber-Rahmen", "emoji": "🥈",
     "preis": 250_000, "art": "rahmen", "rang": 2, "farbe": 0xC0C7CE,
     "desc": "Silber-Look fuer die Level-Karte."},
    {"n": 3, "key": "gold", "name": "Gold-Rahmen", "emoji": "🥇",
     "preis": 1_200_000, "art": "rahmen", "rang": 3, "farbe": 0xF1C40F,
     "desc": "Gold + Funkeln auf der Level-Karte."},
    {"n": 4, "key": "diamant", "name": "Diamant-Rahmen", "emoji": "💎",
     "preis": 6_000_000, "art": "rahmen", "rang": 4, "farbe": 0x78DCFF,
     "desc": "Eisblauer Diamant-Doppelrahmen."},
    {"n": 5, "key": "krone", "name": "Königskrone", "emoji": "👑",
     "preis": 25_000_000, "art": "krone", "rang": 0, "farbe": 0xE0A21A,
     "desc": "Krone neben deinem Namen im Leaderboard."},
    {"n": 6, "key": "galaxie", "name": "Galaxie-Rahmen", "emoji": "🌌",
     "preis": 120_000_000, "art": "rahmen", "rang": 5, "farbe": 0x9B59B6,
     "desc": "Galaxie-Rand mit Sternen - kaum jemand wird das je sehen."},
    {"n": 7, "key": "imperium", "name": "FLO-IMPERIUM", "emoji": "🏰",
     "preis": 1_000_000_000, "art": "imperium", "rang": 6, "farbe": 0xE74C3C,
     "desc": "Imperator-Rolle, alle Rahmen darunter, Krone - und die KI "
             "nennt dich fuer immer Imperator."},
    # --- Jenseits der Milliarde -------------------------------------------
    # Wer die Aktie beherrscht, hatte die alte Spitze (1 Mrd) zu schnell. Diese
    # drei Stufen sind der neue Horizont: erreichbar, aber nur mit Monaten
    # konsequentem Handel (die Vermoegenssteuer laesst das Gleichgewicht bei
    # rund 3,4 Mrd liegen - siehe economy.TAX_SOFT).
    {"n": 8, "key": "nova", "name": "NOVA-AURA", "emoji": "☄️",
     "preis": 1_600_000_000, "art": "rahmen", "rang": 7, "farbe": 0xFFE9A3,
     "desc": "Weissglühender Rand mit Nova-Schweif. Ab hier reden Leute "
             "ueber dich, wenn du nicht im Call bist."},
    {"n": 9, "key": "singularitaet", "name": "SINGULARITÄT", "emoji": "🕳️",
     "preis": 2_200_000_000, "art": "rahmen", "rang": 8, "farbe": 0x1B1B2F,
     "desc": "Deine Karte hat kein Licht mehr, das entkommt. Schwarzer "
             "Rand mit Akkretions-Ring."},
    {"n": 10, "key": "multiversum", "name": "FLO-MULTIVERSUM", "emoji": "🌠",
     "preis": 3_000_000_000, "art": "multiversum", "rang": 9, "farbe": 0xB4FF39,
     "desc": "Das Ende der Leiter. ALLES darunter inklusive, dazu ein "
             "prismatischer Rahmen, den es genau einmal geben kann."},
]
_BY_KEY = {i["key"]: i for i in ITEMS}
# Rahmen nach Rang (der beste besessene wird gezeigt). Aus ITEMS abgeleitet,
# damit eine neue Stufe nicht doppelt gepflegt werden muss - vorher stand hier
# eine handgeschriebene Liste, in der neue Rahmen einfach gefehlt haetten.
_FRAME_ORDER = [i["key"] for i in sorted(ITEMS, key=lambda x: x["rang"])
                if i["art"] in ("rahmen", "imperium", "multiversum")]


class Luxus(FeatureBasis):
    """Objektorientierte Huelle: veraenderlicher Zustand lebt auf der Instanz."""

    def __init__(self):
        # Veraenderlicher Zustand (frueher Modul-Globals).
        self._enabled = False
        self._store = None

    def fmt_coins(self, n):
        """1500 -> '1.500', 2_500_000 -> '2,5 Mio', 1_000_000_000 -> '1 Mrd'."""
        if n >= 1_000_000_000:
            v = n / 1_000_000_000
            s = f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            return f"{s} Mrd"
        if n >= 1_000_000:
            v = n / 1_000_000
            s = f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            return f"{s} Mio"
        return f"{n:,}".replace(",", ".")

    def setup(self):
        """Aktiviert den Luxus-Shop. Braucht economy (den Coin-Topf)."""
        if os.getenv("LUXUS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Luxus-Feature aus (LUXUS_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Luxus-Feature aus: economy ist nicht aktiv.")
            return False
        self._store = JsonStore("luxus.json", default={
            "users": {},                                     # uid -> [item_keys]
            "throne": {"owner": "", "preis": THRONE_START, "n": 0},
        })
        self._enabled = True
        log.info("Luxus-Feature aktiv (%d Items, Thron ab %s Coins).",
                 len(ITEMS), self.fmt_coins(THRONE_START))
        return True

    def is_enabled(self):
        return self._enabled

    # --- Besitz-API (auch fuer economy/render/leaderboard) ----------------------
    def _owned(self, uid):
        if self._store is None:
            return []
        users = self._store.data.setdefault("users", {})
        if not isinstance(users, dict):
            users = self._store.data["users"] = {}
        besitz = users.get(str(uid))
        # setdefault rettet nur einen FEHLENDEN Schluessel - ein vorhandenes
        # null/String kommt daran vorbei und nimmt owns(), has_crown(),
        # get_frame() und damit die ganze Level-Karte mit.
        if not isinstance(besitz, list):
            besitz = users[str(uid)] = []
        return besitz

    # Stufen, die "alles darunter" mitbringen - und zwar nur das, was WIRKLICH
    # darunter liegt. Vorher galt 'imperium' als alles inklusive; mit den neuen
    # Stufen darueber (Nova, Singularitaet, Multiversum) hiess das: wer das
    # Imperium kauft, "besitzt" auch die 3-Mrd-Stufe. Jetzt zaehlt der Rang.
    _ALLES_INKLUSIVE = ("imperium", "multiversum")

    def owns(self, uid, key):
        besitz = self._owned(uid)
        if key in besitz:
            return True
        rang = int(_BY_KEY.get(key, {}).get("rang", 0))
        for paket in self._ALLES_INKLUSIVE:
            if paket in besitz and paket != key:
                if rang < int(_BY_KEY.get(paket, {}).get("rang", 0)):
                    return True
        return False

    def get_frame(self, uid):
        """Bester besessener Rahmen fuer die Level-Karte (oder None)."""
        if not self._enabled:
            return None
        besitz = self._owned(uid)
        # Hoechster RANG gewinnt. Vorher stand hier ein harter Vorzug fuer
        # 'imperium' - jede Stufe darueber (Nova, Singularitaet, Multiversum)
        # waere damit unsichtbar geblieben, obwohl bezahlt.
        best = None
        best_rang = -1
        for key in _FRAME_ORDER:
            if key not in besitz:
                continue
            rang = int(_BY_KEY.get(key, {}).get("rang", 0))
            if rang >= best_rang:
                best, best_rang = key, rang
        return best

    def has_crown(self, uid):
        return self._enabled and (self.owns(uid, "krone"))

    def throne_state(self):
        assert self._store is not None
        st = self._store.data.setdefault(
            "throne", {"owner": "", "preis": THRONE_START, "n": 0})
        # Ein vorhandenes "throne": null kommt an setdefault vorbei. Ohne diese
        # Zeile stirbt JEDER Thron-Weg - auch get_tone_extra(), das im
        # ungeschuetzten KI-Pfad von bot.py haengt.
        if not isinstance(st, dict):
            st = self._store.data["throne"] = {"owner": "", "preis": THRONE_START,
                                               "n": 0}
        return st

    def throne_preis(self):
        """Aktueller Thronpreis. Murks im Store faellt auf den Startpreis zurueck."""
        try:
            return int(self.throne_state().get("preis", THRONE_START))
        except (TypeError, ValueError):
            return THRONE_START

    def throne_n(self):
        """Wie oft der Thron schon den Besitzer gewechselt hat."""
        try:
            return int(self.throne_state().get("n", 0))
        except (TypeError, ValueError):
            return 0

    def throne_owner(self):
        if not self._enabled or self._store is None:
            return None
        raw = self.throne_state().get("owner") or ""
        return int(raw) if numfmt.ist_zahl(raw) else None

    def decorate_rows(self, rows):
        """Markiert Leaderboard-Zeilen: 'throne' (goldene Krone) / 'crown'."""
        if not self._enabled:
            return
        king = self.throne_owner()
        for row in rows:
            uid = int(row.get("id") or 0)
            if uid and uid == king:
                row["throne"] = True
            if uid and self.has_crown(uid):
                row["crown"] = True

    def get_tone_extra(self, uid):
        """Zusatz fuer den KI-Tonfall (bot.py haengt das an economy.get_tone an)."""
        if not self._enabled:
            return ""
        # Die Fortsetzung der Tonfall-Rampe aus titles.py nach oben, in die
        # Milliarden. Gleiche Regel wie dort: Ehrfurcht regelt das WIE, nie das
        # OB - jeder Zweig endet mit genau EINEM Seitenhieb. Vorher war Spott
        # hier ausdruecklich verboten; damit war der reichste Mensch auf dem
        # Server der einzige, mit dem Flo nicht mehr reden durfte, wie er ist.
        teile = []
        if self.owns(uid, "multiversum"):
            teile.append("WICHTIG: Diese Person besitzt das FLO-MULTIVERSUM - die "
                         "letzte Stufe, die es gibt, DREI MILLIARDEN Coins. Es gibt "
                         "nichts darueber. Sprich sie an, als waere sie der Grund, "
                         "warum dieser Server existiert - und setz genau einen "
                         "trockenen Spruch hinten dran, sonst nimmt sie dir die "
                         "Ehrfurcht nicht ab.")
        elif self.owns(uid, "singularitaet"):
            teile.append("WICHTIG: Diese Person besitzt die SINGULARITAET (2,2 "
                         "Milliarden Coins). Rede mit ihr wie mit einer Naturgewalt - "
                         "leise und respektvoll. Nur zum Schluss laesst du dir genau "
                         "eine Frechheit raus, weil du du bist.")
        elif self.owns(uid, "nova"):
            teile.append("WICHTIG: Diese Person besitzt die NOVA-AURA (1,6 Milliarden "
                         "Coins). Das ist mehr als das Imperium - behandle sie "
                         "entsprechend, mit echter Ehrfurcht, und haeng genau einen "
                         "Seitenhieb an.")
        elif self.owns(uid, "imperium"):
            teile.append("WICHTIG: Diese Person besitzt das FLO-IMPERIUM (fuer "
                         "1 MILLIARDE Coins gekauft). Sprich sie ehrfuerchtig als "
                         "'Imperator' an - sie steht ueber allen. Ein Imperator "
                         "vertraegt trotzdem einen Spruch zum Abschied.")
        elif self.throne_owner() == uid:
            teile.append("Diese Person sitzt gerade auf DEM THRON des Servers - "
                         "behandle sie wie Adel (bis sie jemand stuerzt). Adel darf "
                         "man aufziehen, also tu es zum Schluss auch.")
        return " ".join(teile)

    # --- Kauf-Logik --------------------------------------------------------------
    async def _flush_all(self):
        assert self._store is not None
        await self._store.save()
        await economy.flush()

    async def _buy(self, member, item):
        """Kauft ein Katalog-Item. Gibt die Antwort als Text zurueck."""
        uid = member.id
        if self.owns(uid, item["key"]):
            return f"Du besitzt **{item['name']}** schon. 😌"
        preis = int(item["preis"])
        if economy.get_coins(uid) < preis:
            fehlt = preis - economy.get_coins(uid)
            return (f"**{item['name']}** kostet **{self.fmt_coins(preis)}** {economy.COIN} - "
                    f"dir fehlen noch **{self.fmt_coins(fehlt)}**. Bleib dran! 💪")
        economy.add_coins(uid, -preis)
        self._owned(uid).append(item["key"])
        await self._flush_all()
        if item["key"] == "imperium":
            await self._grant_imperator_role(member)
            return (f"🏰 **{member.display_name} HAT DAS FLO-IMPERIUM GEKAUFT!** "
                    f"1 MILLIARDE {economy.COIN} - der Server hat jetzt einen "
                    f"**Imperator**. Alle verneigen sich. 👑")
        if item["key"] == "multiversum":
            await self._grant_imperator_role(member)
            return (f"🌠 **{member.display_name} HAT DAS FLO-MULTIVERSUM GEKAUFT.** "
                    f"3 MILLIARDEN {economy.COIN}. Es gibt nichts darüber – "
                    f"die Leiter endet hier, und oben steht ein Name.")
        if item["key"] in ("nova", "singularitaet"):
            return (f"{item['emoji']} **{member.display_name}** hat "
                    f"**{item['name']}** gekauft – {self.fmt_coins(preis)} "
                    f"{economy.COIN}. Jenseits des Imperiums. "
                    f"Kontostand: {self.fmt_coins(economy.get_coins(uid))}.")
        return (f"{item['emoji']} **{item['name']}** gehört jetzt dir "
                f"(-{self.fmt_coins(preis)} {economy.COIN}). "
                f"Kontostand: {self.fmt_coins(economy.get_coins(uid))}.")

    async def _grant_imperator_role(self, member):
        """Imperator-Rolle anlegen + zuweisen (fehlertolerant, nur Deko)."""
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        try:
            role = discord.utils.get(guild.roles, name=IMPERATOR_ROLE)
            if role is None:
                role = await guild.create_role(
                    name=IMPERATOR_ROLE, colour=discord.Colour(0xE74C3C),
                    hoist=True, reason="Flo-Imperium gekauft (1 Mrd Coins)")
            await member.add_roles(role, reason="Flo-Imperium")
        except Exception:  # noqa: BLE001 - Rolle ist Bonus, Kauf zaehlt trotzdem
            log.exception("Imperator-Rolle konnte nicht vergeben werden")

    async def _seize_throne(self, member):
        """Erobert den Thron. Rueckgabe: (text, erfolgreich)."""
        uid = member.id
        st = self.throne_state()
        if st.get("owner") == str(uid):
            return "Du sitzt schon auf dem Thron. Genieß die Aussicht. 👑", False
        preis = self.throne_preis()
        if economy.get_coins(uid) < preis:
            fehlt = preis - economy.get_coins(uid)
            return (f"Der Thron kostet gerade **{self.fmt_coins(preis)}** {economy.COIN} - "
                    f"dir fehlen **{self.fmt_coins(fehlt)}**."), False
        alter = st.get("owner") or ""
        economy.add_coins(uid, -preis)
        st["owner"] = str(uid)
        st["preis"] = int(preis * THRONE_FACTOR)
        st["n"] = self.throne_n() + 1
        await self._flush_all()
        gestuerzt = f" <@{alter}> ist **gestürzt**!" if alter else ""
        return (f"⚔️ **{member.display_name}** erobert **DEN THRON** für "
                f"**{self.fmt_coins(preis)}** {economy.COIN}!{gestuerzt}\n"
                f"Nächste Eroberung kostet **{self.fmt_coins(st['preis'])}**. 👑"), True

    # --- Befehle -----------------------------------------------------------------
    async def handle(self, message):
        if not self._enabled or message.guild is None:
            return None
        cmd = ai.strip_lead(message.content or "")
        if not cmd:
            return None
        parts = cmd.split()
        first = parts[0].lower().strip(".,;:!?")
        args = parts[1:]

        if first in ("luxus", "luxury", "prestige"):
            if len(args) >= 2 and args[0].lower() in ("kaufen", "kauf", "buy") and numfmt.ist_zahl(args[1]):
                item = next((i for i in ITEMS if i["n"] == int(args[1])), None)
                if item is None:
                    return f"Es gibt nur Item 1-{len(ITEMS)}. `{self._bot_name} luxus` zeigt alle."
                return await self._buy(message.author, item)
            return await self._luxus_overview(message)
        if first in ("thron", "throne"):
            return await self._throne_overview(message)
        return None

    # Die Leiter in drei Abschnitte geteilt - zehn gleich aussehende Felder waren
    # eine Wand aus Text, in der man weder den Fortschritt noch die naechste
    # Stufe erkannt hat.
    _STAFFELN = (("Einstieg", "🪙", ("bronze", "silber")),
                 ("Oberklasse", "💠", ("gold", "diamant", "krone")),
                 ("Endgame", "🌌", ("galaxie", "imperium")),
                 ("Jenseits der Milliarde", "☄️",
                  ("nova", "singularitaet", "multiversum")))

    def _fortschritt(self, uid):
        """(besessen, gesamt, Balken) ueber die ganze Leiter."""
        gesamt = len(ITEMS)
        habe = sum(1 for i in ITEMS if self.owns(uid, i["key"]))
        voll = int(round(12.0 * habe / max(1, gesamt)))
        return habe, gesamt, "▰" * voll + "▱" * (12 - voll)

    def _luxus_embed(self, uid):
        guthaben = economy.get_coins(uid)
        habe, gesamt, balken = self._fortschritt(uid)
        # Naechstes Ziel = die guenstigste Stufe, die noch fehlt.
        offen = [i for i in ITEMS if not self.owns(uid, i["key"])]
        naechste = min(offen, key=lambda i: i["preis"]) if offen else None
        top = max((i for i in ITEMS if self.owns(uid, i["key"])),
                  key=lambda i: i["rang"], default=None)
        farbe = discord.Color(top["farbe"]) if top else discord.Color.gold()

        emb = discord.Embed(
            title="🏆 Flo Luxus",
            description=(f"Coins in **Status** verwandeln. Rahmen zieren deine "
                         f"Level-Karte, die Krone glänzt im Leaderboard – und ganz "
                         f"oben endet die Leiter im **MULTIVERSUM**.\n"
                         f"{balken}  **{habe}/{gesamt}** Stufen"),
            color=farbe)

        for name, emoji, keys in self._STAFFELN:
            zeilen = []
            for key in keys:
                item = _BY_KEY.get(key)
                if item is None:
                    continue
                if self.owns(uid, key):
                    marke = "✅"
                    preis = "_gehört dir_"
                elif guthaben >= item["preis"]:
                    marke = "🟢"
                    preis = f"**{self.fmt_coins(item['preis'])}** – _leistbar_"
                else:
                    marke = "🔒"
                    fehlt = item["preis"] - guthaben
                    preis = (f"**{self.fmt_coins(item['preis'])}** "
                             f"_(noch {self.fmt_coins(fehlt)})_")
                zeilen.append(f"{marke} `{item['n']:>2}` {item['emoji']} "
                              f"**{item['name']}** · {preis}")
            if zeilen:
                emb.add_field(name=f"{emoji} {name}", value="\n".join(zeilen),
                              inline=False)

        if naechste is not None:
            fehlt = max(0, naechste["preis"] - guthaben)
            emb.add_field(
                name="🎯 Nächstes Ziel",
                value=(f"{naechste['emoji']} **{naechste['name']}** · "
                       f"{self.fmt_coins(naechste['preis'])} {economy.COIN}\n"
                       + ("_Du kannst es dir jetzt leisten._" if fehlt <= 0
                          else f"_Es fehlen noch {self.fmt_coins(fehlt)}._")
                       + f"\n{naechste['desc']}"),
                inline=False)
        else:
            emb.add_field(name="🏁 Komplett",
                          value="_Du besitzt jede Stufe. Es gibt nichts mehr zu kaufen._",
                          inline=False)

        st = self.throne_state()
        king = self.throne_owner()
        thron_wert = (f"Besitzer: <@{king}> · Stürzen kostet "
                      if king else "**UNBESETZT** · Eroberung kostet ")
        emb.add_field(name="⚔️ DER THRON (Unikat)",
                      value=(f"{thron_wert}**{self.fmt_coins(self.throne_preis())}** "
                             f"{economy.COIN} · `{self._bot_name} thron`"),
                      inline=False)
        emb.set_footer(text=(f"Kontostand: {self.fmt_coins(guthaben)} {economy.COIN} · "
                            f"unten auswählen oder {self._bot_name} luxus kaufen <nr>"))
        return emb

    async def _luxus_overview(self, message):
        view = _LuxusView(message.author.id)
        try:
            msg = await message.reply(embed=self._luxus_embed(message.author.id),
                                      view=view, mention_author=False)
            view.message = msg
            self._protect(msg)
        except discord.HTTPException:
            log.exception("Luxus-Katalog konnte nicht gesendet werden")
        return HANDLED

    def _throne_embed(self):
        st = self.throne_state()
        king = self.throne_owner()
        if king:
            desc = (f"Aktueller Herrscher: <@{king}> 👑\n"
                    f"Stürzen kostet **{self.fmt_coins(self.throne_preis())}** {economy.COIN}.")
        else:
            desc = (f"Der Thron ist **UNBESETZT**! Erster Preis: "
                    f"**{self.fmt_coins(self.throne_preis())}** {economy.COIN}.")
        emb = discord.Embed(
            title="⚔️ DER THRON",
            description=(f"{desc}\n\nEs gibt nur **einen**. Jede Eroberung macht ihn "
                         f"**75% teurer** - und der alte Besitzer geht leer aus. 😈\n"
                         f"Der Herrscher glänzt im Leaderboard und wird von "
                         f"{self._bot_name} wie Adel behandelt."),
            color=discord.Color.dark_gold())
        emb.set_footer(text=f"Bisher {int(st.get('n', 0))} Eroberung(en)")
        return emb

    async def _throne_overview(self, message):
        view = _ThroneView()
        try:
            msg = await message.reply(embed=self._throne_embed(), view=view,
                                      mention_author=False)
            view.message = msg
            self._protect(msg)
        except discord.HTTPException:
            log.exception("Thron konnte nicht gesendet werden")
        return HANDLED

    # --- Auto-Loesch-Schutz (wie in den anderen Modulen) -------------------------
    # Loesch-Schutz: liegt in basis.FeatureBasis (schuetzen/freigeben).
    def _protect(self, msg):
        self.schuetzen(msg)

    def _release(self, msg):
        self.freigeben(msg)


class _LuxusSelect(discord.ui.Select):
    def __init__(self, uid):
        options = []
        for item in ITEMS:
            besitzt = instance.owns(uid, item["key"])
            options.append(discord.SelectOption(
                label=f"{item['name']} – {instance.fmt_coins(item['preis'])}",
                value=item["key"], emoji=item["emoji"],
                description=("bereits gekauft" if besitzt else item["desc"][:100])))
        super().__init__(placeholder="🛍️ Was gönnst du dir?", min_values=1,
                         max_values=1, options=options, row=0)

    async def callback(self, interaction):
        item = _BY_KEY[self.values[0]]
        text = await instance._buy(interaction.user, item)
        await interaction.response.send_message(text, ephemeral=True)
        # Uebersicht aktualisieren (Besitz-Haken, Kontostand).
        view = self.view  # type: ignore[assignment]
        if view.message is not None:
            try:
                await view.message.edit(embed=instance._luxus_embed(view.uid))
            except discord.HTTPException:
                pass


class _LuxusView(discord.ui.View):
    """Luxus-Katalog: Dropdown kauft direkt (Antwort ephemeral)."""

    def __init__(self, uid):
        super().__init__(timeout=180)
        self.uid = uid
        self.message = None
        self.add_item(_LuxusSelect(uid))

    async def interaction_check(self, interaction):
        if interaction.user.id == self.uid:
            return True
        await interaction.response.send_message(
            f"Öffne deinen eigenen Katalog mit `{instance._bot_name} luxus`. 🛍️", ephemeral=True)
        return False

    async def on_timeout(self):
        for ch in self.children:
            ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            instance._release(self.message)


class _ThroneConfirm(discord.ui.View):
    """Ephemere Sicherheitsabfrage - ein Fehlklick waere teuer."""

    def __init__(self, uid, panel):
        super().__init__(timeout=30)
        self.uid = uid
        self.panel = panel

    @discord.ui.button(label="Ja, Thron erobern!", emoji="⚔️",
                       style=discord.ButtonStyle.danger)
    async def _yes(self, interaction, _b):
        text, ok = await instance._seize_throne(interaction.user)
        for ch in self.children:
            ch.disabled = True
        await interaction.response.edit_message(content=text, view=self)
        self.stop()
        if ok and self.panel.message is not None:
            try:  # Panel aktualisieren + Eroberung oeffentlich ausrufen
                await self.panel.message.edit(embed=instance._throne_embed())
                await self.panel.message.channel.send(text)
            except discord.HTTPException:
                pass


class _ThroneView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.message = None

    @discord.ui.button(label="Erobern", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def _seize(self, interaction, _b):
        st = instance.throne_state()
        preis = instance.throne_preis()
        await interaction.response.send_message(
            f"Den Thron für **{instance.fmt_coins(preis)}** {economy.COIN} erobern?",
            view=_ThroneConfirm(interaction.user.id, self), ephemeral=True)

    async def on_timeout(self):
        for ch in self.children:
            ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            instance._release(self.message)


instance = Luxus()

# Modul-Aliase: Aufrufer (bot.py, economy.py) nutzen weiter luxus.<name>.
# _store und _enabled bleiben bewusst OHNE Alias (Zugriff: luxus.instance.<name>).
fmt_coins = instance.fmt_coins
setup = instance.setup
is_enabled = instance.is_enabled
_owned = instance._owned
owns = instance.owns
get_frame = instance.get_frame
has_crown = instance.has_crown
throne_state = instance.throne_state
decorate_rows = instance.decorate_rows
get_tone_extra = instance.get_tone_extra
handle = instance.handle
_luxus_embed = instance._luxus_embed
