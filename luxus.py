"""Flo Luxus: die Coin-Senke fuer Reiche - Prestige-Katalog + DER THRON.

Befehle (nach 'Flo'):
- luxus               Katalog mit Kauf-Menue (15.000 bis 1 MILLIARDE Coins)
- luxus kaufen <n>    Item Nr. n direkt kaufen (Text-Fallback)
- thron               Der Thron: Unikat! Nur EINER sitzt drauf - wer den
                      aktuellen Preis zahlt, stuerzt den Besitzer. Jede
                      Eroberung macht den Thron 75% teurer.

Effekte (sichtbar, nicht nur Zahlen):
- Rahmen (bronze..galaxie): die Level-Karte ('flo level') bekommt einen
  edlen Rahmen - es zaehlt der beste besessene.
- Koenigskrone: Krone neben dem Namen im Leaderboard ('flo top').
- FLO-IMPERIUM (1 Mrd): alle Rahmen, Krone, eigene Imperator-Rolle im
  Server und die KI spricht dich als Imperator an.
- Thron-Besitzer: goldene Krone + Zeile im Leaderboard, die KI behandelt
  dich wie Adel - bis dich jemand stuerzt.

Coins laufen ueber economy (ein Topf); Besitz liegt in data/luxus.json.
"""
from __future__ import annotations

import logging
import os

import discord

import ai
import economy
from store import JsonStore

log = logging.getLogger("dcbot.luxus")

# Sentinel: luxus hat selbst geantwortet -> bot.py schweigt.
HANDLED = object()

_enabled: bool = False
_bot_name: str = "Flo"
_store: JsonStore | None = None

THRONE_START = 50_000
THRONE_FACTOR = 1.75          # jede Eroberung: Preis x1.75
IMPERATOR_ROLE = "🏰 Imperator"

# Katalog: fest, bewusst KEIN Zufall - das sind Lebensziele. 'rang' ordnet
# die Rahmen (der beste besessene wird angezeigt).
ITEMS: list[dict] = [
    {"n": 1, "key": "bronze", "name": "Bronze-Rahmen", "emoji": "🥉",
     "preis": 15_000, "art": "rahmen", "rang": 1, "farbe": 0xCD7F32,
     "desc": "Deine Level-Karte in edlem Bronze."},
    {"n": 2, "key": "silber", "name": "Silber-Rahmen", "emoji": "🥈",
     "preis": 75_000, "art": "rahmen", "rang": 2, "farbe": 0xC0C7CE,
     "desc": "Silber-Look fuer die Level-Karte."},
    {"n": 3, "key": "gold", "name": "Gold-Rahmen", "emoji": "🥇",
     "preis": 400_000, "art": "rahmen", "rang": 3, "farbe": 0xF1C40F,
     "desc": "Gold + Funkeln auf der Level-Karte."},
    {"n": 4, "key": "diamant", "name": "Diamant-Rahmen", "emoji": "💎",
     "preis": 2_500_000, "art": "rahmen", "rang": 4, "farbe": 0x78DCFF,
     "desc": "Eisblauer Diamant-Doppelrahmen."},
    {"n": 5, "key": "krone", "name": "Königskrone", "emoji": "👑",
     "preis": 20_000_000, "art": "krone", "rang": 0, "farbe": 0xF1C40F,
     "desc": "Krone neben deinem Namen im Leaderboard."},
    {"n": 6, "key": "galaxie", "name": "Galaxie-Rahmen", "emoji": "🌌",
     "preis": 150_000_000, "art": "rahmen", "rang": 5, "farbe": 0x9B59B6,
     "desc": "Galaxie-Rand mit Sternen - kaum jemand wird das je sehen."},
    {"n": 7, "key": "imperium", "name": "FLO-IMPERIUM", "emoji": "🏰",
     "preis": 1_000_000_000, "art": "imperium", "rang": 6, "farbe": 0xE74C3C,
     "desc": "ALLES. Imperator-Rolle, alle Rahmen, Krone - und die KI "
             "nennt dich fuer immer Imperator."},
]
_BY_KEY = {i["key"]: i for i in ITEMS}
_FRAME_ORDER = ["bronze", "silber", "gold", "diamant", "galaxie", "imperium"]


def fmt_coins(n: int) -> str:
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


def setup() -> bool:
    """Aktiviert den Luxus-Shop. Braucht economy (den Coin-Topf)."""
    global _enabled, _bot_name, _store
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("LUXUS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Luxus-Feature aus (LUXUS_ENABLED=0).")
        return False
    if not economy.is_enabled():
        log.info("Luxus-Feature aus: economy ist nicht aktiv.")
        return False
    _store = JsonStore("luxus.json", default={
        "users": {},                                     # uid -> [item_keys]
        "throne": {"owner": "", "preis": THRONE_START, "n": 0},
    })
    _enabled = True
    log.info("Luxus-Feature aktiv (%d Items, Thron ab %s Coins).",
             len(ITEMS), fmt_coins(THRONE_START))
    return True


def is_enabled() -> bool:
    return _enabled


# --- Besitz-API (auch fuer economy/render/leaderboard) ----------------------
def _owned(uid: int) -> list[str]:
    if _store is None:
        return []
    return _store.data.setdefault("users", {}).setdefault(str(uid), [])


def owns(uid: int, key: str) -> bool:
    if key in _owned(uid):
        return True
    return key != "imperium" and "imperium" in _owned(uid)   # Imperium = alles


def get_frame(uid: int) -> "str | None":
    """Bester besessener Rahmen fuer die Level-Karte (oder None)."""
    if not _enabled:
        return None
    besitz = _owned(uid)
    if "imperium" in besitz:
        return "imperium"
    best = None
    for key in _FRAME_ORDER:
        if key in besitz:
            best = key
    return best


def has_crown(uid: int) -> bool:
    return _enabled and (owns(uid, "krone"))


def throne_state() -> dict:
    assert _store is not None
    return _store.data.setdefault(
        "throne", {"owner": "", "preis": THRONE_START, "n": 0})


def throne_owner() -> "int | None":
    if not _enabled or _store is None:
        return None
    raw = throne_state().get("owner") or ""
    return int(raw) if raw.isdigit() else None


def decorate_rows(rows: list[dict]) -> None:
    """Markiert Leaderboard-Zeilen: 'throne' (goldene Krone) / 'crown'."""
    if not _enabled:
        return
    king = throne_owner()
    for row in rows:
        uid = int(row.get("id") or 0)
        if uid and uid == king:
            row["throne"] = True
        if uid and has_crown(uid):
            row["crown"] = True


def get_tone_extra(uid: int) -> str:
    """Zusatz fuer den KI-Tonfall (bot.py haengt das an economy.get_tone an)."""
    if not _enabled:
        return ""
    teile: list[str] = []
    if owns(uid, "imperium"):
        teile.append("WICHTIG: Diese Person besitzt das FLO-IMPERIUM (fuer "
                     "1 MILLIARDE Coins gekauft). Sprich sie ehrfuerchtig als "
                     "'Imperator' an - sie steht ueber allen.")
    elif throne_owner() == uid:
        teile.append("Diese Person sitzt gerade auf DEM THRON des Servers - "
                     "behandle sie wie Adel (bis sie jemand stuerzt).")
    return " ".join(teile)


# --- Kauf-Logik --------------------------------------------------------------
async def _flush_all() -> None:
    assert _store is not None
    await _store.save()
    await economy.flush()


async def _buy(member: discord.abc.User, item: dict) -> str:
    """Kauft ein Katalog-Item. Gibt die Antwort als Text zurueck."""
    uid = member.id
    if owns(uid, item["key"]):
        return f"Du besitzt **{item['name']}** schon. 😌"
    preis = int(item["preis"])
    if economy.get_coins(uid) < preis:
        fehlt = preis - economy.get_coins(uid)
        return (f"**{item['name']}** kostet **{fmt_coins(preis)}** {economy.COIN} - "
                f"dir fehlen noch **{fmt_coins(fehlt)}**. Bleib dran! 💪")
    economy.add_coins(uid, -preis)
    _owned(uid).append(item["key"])
    await _flush_all()
    if item["key"] == "imperium":
        await _grant_imperator_role(member)
        return (f"🏰 **{member.display_name} HAT DAS FLO-IMPERIUM GEKAUFT!** "
                f"1 MILLIARDE {economy.COIN} - der Server hat jetzt einen "
                f"**Imperator**. Alle verneigen sich. 👑")
    return (f"{item['emoji']} **{item['name']}** gehört jetzt dir "
            f"(-{fmt_coins(preis)} {economy.COIN}). "
            f"Kontostand: {fmt_coins(economy.get_coins(uid))}.")


async def _grant_imperator_role(member) -> None:
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


async def _seize_throne(member: discord.abc.User) -> tuple[str, bool]:
    """Erobert den Thron. Rueckgabe: (text, erfolgreich)."""
    uid = member.id
    st = throne_state()
    if st.get("owner") == str(uid):
        return "Du sitzt schon auf dem Thron. Genieß die Aussicht. 👑", False
    preis = int(st.get("preis", THRONE_START))
    if economy.get_coins(uid) < preis:
        fehlt = preis - economy.get_coins(uid)
        return (f"Der Thron kostet gerade **{fmt_coins(preis)}** {economy.COIN} - "
                f"dir fehlen **{fmt_coins(fehlt)}**."), False
    alter = st.get("owner") or ""
    economy.add_coins(uid, -preis)
    st["owner"] = str(uid)
    st["preis"] = int(preis * THRONE_FACTOR)
    st["n"] = int(st.get("n", 0)) + 1
    await _flush_all()
    gestuerzt = f" <@{alter}> ist **gestürzt**!" if alter else ""
    return (f"⚔️ **{member.display_name}** erobert **DEN THRON** für "
            f"**{fmt_coins(preis)}** {economy.COIN}!{gestuerzt}\n"
            f"Nächste Eroberung kostet **{fmt_coins(st['preis'])}**. 👑"), True


# --- Befehle -----------------------------------------------------------------
async def handle(message: discord.Message) -> "str | object | None":
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.split()
    first = parts[0].lower().strip(".,;:!?")
    args = parts[1:]

    if first in ("luxus", "luxury", "prestige"):
        if len(args) >= 2 and args[0].lower() in ("kaufen", "kauf", "buy") and args[1].isdigit():
            item = next((i for i in ITEMS if i["n"] == int(args[1])), None)
            if item is None:
                return f"Es gibt nur Item 1-{len(ITEMS)}. `{_bot_name} luxus` zeigt alle."
            return await _buy(message.author, item)
        return await _luxus_overview(message)
    if first in ("thron", "throne"):
        return await _throne_overview(message)
    return None


def _luxus_embed(uid: int) -> discord.Embed:
    emb = discord.Embed(
        title="🏆 Flo Luxus",
        description=("Hier verbrennst du Coins für **Status**. "
                     "Rahmen zieren deine Level-Karte, die Krone glänzt im "
                     "Leaderboard - und ganz oben wartet das **IMPERIUM**.\n"
                     f"Kaufen: unten auswählen oder `{_bot_name} luxus kaufen <nr>`."),
        color=discord.Color.gold())
    for item in ITEMS:
        besitzt = owns(uid, item["key"])
        status = " ✅" if besitzt else ""
        emb.add_field(
            name=f"{item['n']}. {item['emoji']} {item['name']}{status}",
            value=f"**{fmt_coins(item['preis'])}** {economy.COIN}\n{item['desc']}",
            inline=True)
    st = throne_state()
    king = throne_owner()
    thron_wert = (f"Besitzer: <@{king}> · nächste Eroberung "
                  if king else "**UNBESETZT** · Eroberung ")
    emb.add_field(name="⚔️ DER THRON (Unikat)",
                  value=(f"{thron_wert}**{fmt_coins(int(st.get('preis', THRONE_START)))}** "
                         f"{economy.COIN} · `{_bot_name} thron`"),
                  inline=False)
    emb.set_footer(text=f"Kontostand: {fmt_coins(economy.get_coins(uid))} {economy.COIN}")
    return emb


class _LuxusSelect(discord.ui.Select):
    def __init__(self, uid: int) -> None:
        options = []
        for item in ITEMS:
            besitzt = owns(uid, item["key"])
            options.append(discord.SelectOption(
                label=f"{item['name']} – {fmt_coins(item['preis'])}",
                value=item["key"], emoji=item["emoji"],
                description=("bereits gekauft" if besitzt else item["desc"][:100])))
        super().__init__(placeholder="🛍️ Was gönnst du dir?", min_values=1,
                         max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        item = _BY_KEY[self.values[0]]
        text = await _buy(interaction.user, item)
        await interaction.response.send_message(text, ephemeral=True)
        # Uebersicht aktualisieren (Besitz-Haken, Kontostand).
        view: "_LuxusView" = self.view  # type: ignore[assignment]
        if view.message is not None:
            try:
                await view.message.edit(embed=_luxus_embed(view.uid))
            except discord.HTTPException:
                pass


class _LuxusView(discord.ui.View):
    """Luxus-Katalog: Dropdown kauft direkt (Antwort ephemeral)."""

    def __init__(self, uid: int) -> None:
        super().__init__(timeout=180)
        self.uid = uid
        self.message: discord.Message | None = None
        self.add_item(_LuxusSelect(uid))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.uid:
            return True
        await interaction.response.send_message(
            f"Öffne deinen eigenen Katalog mit `{_bot_name} luxus`. 🛍️", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        for ch in self.children:
            ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            _release(self.message)


async def _luxus_overview(message: discord.Message) -> object:
    view = _LuxusView(message.author.id)
    try:
        msg = await message.reply(embed=_luxus_embed(message.author.id),
                                  view=view, mention_author=False)
        view.message = msg
        _protect(msg)
    except discord.HTTPException:
        log.exception("Luxus-Katalog konnte nicht gesendet werden")
    return HANDLED


class _ThroneConfirm(discord.ui.View):
    """Ephemere Sicherheitsabfrage - ein Fehlklick waere teuer."""

    def __init__(self, uid: int, panel: "_ThroneView") -> None:
        super().__init__(timeout=30)
        self.uid = uid
        self.panel = panel

    @discord.ui.button(label="Ja, Thron erobern!", emoji="⚔️",
                       style=discord.ButtonStyle.danger)
    async def _yes(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        text, ok = await _seize_throne(interaction.user)
        for ch in self.children:
            ch.disabled = True
        await interaction.response.edit_message(content=text, view=self)
        self.stop()
        if ok and self.panel.message is not None:
            try:  # Panel aktualisieren + Eroberung oeffentlich ausrufen
                await self.panel.message.edit(embed=_throne_embed())
                await self.panel.message.channel.send(text)
            except discord.HTTPException:
                pass


class _ThroneView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=180)
        self.message: discord.Message | None = None

    @discord.ui.button(label="Erobern", emoji="⚔️", style=discord.ButtonStyle.danger)
    async def _seize(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        st = throne_state()
        preis = int(st.get("preis", THRONE_START))
        await interaction.response.send_message(
            f"Den Thron für **{fmt_coins(preis)}** {economy.COIN} erobern?",
            view=_ThroneConfirm(interaction.user.id, self), ephemeral=True)

    async def on_timeout(self) -> None:
        for ch in self.children:
            ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            _release(self.message)


def _throne_embed() -> discord.Embed:
    st = throne_state()
    king = throne_owner()
    if king:
        desc = (f"Aktueller Herrscher: <@{king}> 👑\n"
                f"Stürzen kostet **{fmt_coins(int(st['preis']))}** {economy.COIN}.")
    else:
        desc = (f"Der Thron ist **UNBESETZT**! Erster Preis: "
                f"**{fmt_coins(int(st.get('preis', THRONE_START)))}** {economy.COIN}.")
    emb = discord.Embed(
        title="⚔️ DER THRON",
        description=(f"{desc}\n\nEs gibt nur **einen**. Jede Eroberung macht ihn "
                     f"**75% teurer** - und der alte Besitzer geht leer aus. 😈\n"
                     f"Der Herrscher glänzt im Leaderboard und wird von "
                     f"{_bot_name} wie Adel behandelt."),
        color=discord.Color.dark_gold())
    emb.set_footer(text=f"Bisher {int(st.get('n', 0))} Eroberung(en)")
    return emb


async def _throne_overview(message: discord.Message) -> object:
    view = _ThroneView()
    try:
        msg = await message.reply(embed=_throne_embed(), view=view,
                                  mention_author=False)
        view.message = msg
        _protect(msg)
    except discord.HTTPException:
        log.exception("Thron konnte nicht gesendet werden")
    return HANDLED


# --- Auto-Loesch-Schutz (wie in den anderen Modulen) -------------------------
def _protect(msg) -> None:
    if msg is None:
        return
    try:
        import bot
        bot.protect_message(msg)
    except Exception:
        pass


def _release(msg) -> None:
    if msg is None:
        return
    try:
        import bot
        bot.release_message(msg)
    except Exception:
        pass
