"""Casino-Feature fuer Flo (Pack 5): spielen mit Flo Coins – jetzt mit Buttons,
Formularen (Modals) und echter Grafik.

Spiele (nach 'Flo'):
- casino                      Uebersicht mit Buttons je Spiel (oeffnet ein Formular)
- blackjack <einsatz>         17-und-4 gegen den Dealer, gesteuert per Buttons
                              (Karte / Stand / Double) – Karten als Bild
- crash <einsatz> <ziel>      Rakete steigt – grafische Kurve, cash vor dem Absturz
- keno <einsatz> <1-8 zahlen> tippe Zahlen 1-40, 10 werden gezogen
- roulette <einsatz> <auf>    rot/schwarz, gerade/ungerade, 1-18/19-36 oder Zahl 0-36

Alles laeuft ueber EINEN Coin-Topf: economy.py. Tippen funktioniert weiter als
Fallback (gut bei Neustarts) – die Buttons sind nur der bequeme Weg. Offene
Blackjack-Runden leben im Speicher und verfallen nach BJ_TIMEOUT.
"""
from __future__ import annotations

import logging
import os
import random
import time

import discord

import ai
import economy
import render

log = logging.getLogger("dcbot.casino")

# Sentinel: Casino hat selbst geantwortet (Embed/Bild/Buttons) -> bot.py schweigt.
HANDLED = object()

_enabled: bool = False
_bot_name: str = "Flo"

MIN_BET = 1
MAX_BET = int(os.getenv("CASINO_MAX_BET", "100000") or "100000")
BJ_TIMEOUT = 180        # Sekunden, bis eine offene Blackjack-Runde verfaellt

# Aktive Blackjack-Runden je (channel_id, user_id) -> BlackjackView. Nur im Speicher.
_bj_views: "dict[tuple[int, int], BlackjackView]" = {}

# Farben
_C_PLAY = discord.Color.blurple()
_C_WIN = discord.Color.green()
_C_LOSE = discord.Color.red()
_C_PUSH = discord.Color.greyple()
_C_BJ = discord.Color.gold()


def setup() -> bool:
    """Aktiviert das Casino. Voraussetzung: economy (Flo Coins) ist aktiv."""
    global _enabled, _bot_name
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("CASINO_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Casino-Feature aus (CASINO_ENABLED=0).")
        return False
    if not economy.is_enabled():
        log.info("Casino-Feature aus: economy (Flo Coins) ist nicht aktiv.")
        return False
    _enabled = True
    log.info("Casino-Feature aktiv (Einsatz %d–%d %s, mit Buttons & Grafik).",
             MIN_BET, MAX_BET, economy.COIN)
    return True


def is_enabled() -> bool:
    return _enabled


# --- Einsatz- & Embed-Helfer ---------------------------------------------
def _resolve_bet(token: str, uid: int) -> int | None:
    """Wandelt ein Einsatz-Token in eine Zahl. 'alles'/'max' = ganzer Kontostand."""
    token = (token or "").strip().lower()
    if token in ("all", "alles", "max", "allin", "all-in"):
        return min(economy.get_coins(uid), MAX_BET)
    if token.isdigit():
        return int(token)
    return None


def _check_bet(uid: int, bet: int | None) -> tuple[int, str | None]:
    """Prueft einen Einsatz. Rueckgabe: (gepruefter Einsatz, Fehlertext oder None)."""
    if bet is None:
        return 0, "Wie viel setzt du? z. B. `50` oder `alles`."
    if bet < MIN_BET:
        return 0, f"Mindesteinsatz ist {MIN_BET} {economy.COIN}."
    if bet > MAX_BET:
        return 0, f"Maximaleinsatz ist {MAX_BET} {economy.COIN}."
    bal = economy.get_coins(uid)
    if bet > bal:
        return 0, f"Dafuer reicht's nicht – du hast {bal} {economy.COIN}."
    return bet, None


def _take(uid: int, raw_bet: int | None) -> tuple[int, str | None]:
    """Prueft den Einsatz und zieht ihn sofort ein. (bet, fehler)."""
    bet, err = _check_bet(uid, raw_bet)
    if err:
        return 0, err
    economy.add_coins(uid, -bet)
    return bet, None


def _bal_footer(uid: int) -> str:
    return f"Kontostand: {economy.get_coins(uid)} {economy.COIN}"


def _outcome(bet: int, payout: int) -> tuple[discord.Color, str, str]:
    """Farbe + Ergebnis-Feld aus Einsatz und Auszahlung (Auszahlung inkl. Einsatz)."""
    net = payout - bet
    if net > 0:
        return _C_WIN, "Gewinn", f"+{net} {economy.COIN}"
    if net == 0:
        return _C_PUSH, "Ergebnis", "±0 – Einsatz zurueck"
    return _C_LOSE, "Verlust", f"-{bet} {economy.COIN}"


def _err(text: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {text}", color=_C_LOSE)


def _info(text: str) -> discord.Embed:
    return discord.Embed(description=text, color=_C_BJ)


async def _send(message: discord.Message, *, embed=None, file=None, view=None):
    """Sendet eine Casino-Antwort als Reply. Gibt die Nachricht zurueck (oder None)."""
    kwargs = {"mention_author": False}
    if embed is not None:
        kwargs["embed"] = embed
    if file is not None:
        kwargs["file"] = file
    if view is not None:
        kwargs["view"] = view
    try:
        return await message.reply(**kwargs)
    except discord.HTTPException:
        log.exception("Casino-Antwort konnte nicht gesendet werden")
        return None


# --- Spielkarten ---------------------------------------------------------
_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
_SUITS = ["♠", "♥", "♦", "♣"]


def _new_deck() -> list[tuple[str, str]]:
    deck = [(r, s) for s in _SUITS for r in _RANKS]
    random.shuffle(deck)
    return deck


def _card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)


def _hand_value(hand: list[tuple[str, str]]) -> int:
    total = sum(_card_value(r) for r, _ in hand)
    aces = sum(1 for r, _ in hand if r == "A")
    while total > 21 and aces:
        total -= 10   # Ass von 11 auf 1 abwerten
        aces -= 1
    return total


# --- Befehls-Einstieg ----------------------------------------------------
async def handle(message: discord.Message) -> "object | None":
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.split()
    first = parts[0].lower()
    args = parts[1:]

    if first in ("casino", "spielbank", "kasino", "glücksspiel", "gluecksspiel", "gambling"):
        view = CasinoHubView(message.author.id)
        msg = await _send(message, embed=_menu(message.author.id), view=view)
        if msg:
            view.message = msg
        return HANDLED
    if first in ("blackjack", "bj", "17und4", "siebzehnundvier"):
        return await _bj_command(message, args)
    if first in ("hit", "karte", "ziehen", "zieh"):
        return await _bj_text_action(message, "hit")
    if first in ("stand", "stehen", "bleiben", "bleib", "pass", "genug", "fertig"):
        return await _bj_text_action(message, "stand")
    if first in ("double", "doppeln", "verdoppeln", "dd"):
        return await _bj_text_action(message, "double")
    if first in ("crash", "absturz", "rakete", "rocket"):
        return await _crash_command(message, args)
    if first == "keno":
        return await _keno_command(message, args)
    if first in ("roulette", "roul", "kessel"):
        return await _roulette_command(message, args)
    return None


def _menu(uid: int) -> discord.Embed:
    c = economy.COIN
    emb = discord.Embed(
        title="🎰 Flo Casino",
        description=(f"Setze deine **{c}** und versuch dein Glück.\n"
                     "Tippe unten einfach auf ein **Spiel** – der Rest geht per Button & Formular. 👇"),
        color=_C_BJ,
    )
    emb.add_field(name="🂡 Blackjack", value="17 & 4 gegen den Dealer", inline=True)
    emb.add_field(name="🚀 Crash", value="steig vor dem Absturz aus", inline=True)
    emb.add_field(name="🎱 Keno", value="tippe 1–8 Zahlen (1–40)", inline=True)
    emb.add_field(name="🎡 Roulette", value="Farbe · gerade · Zahl 0–36", inline=True)
    emb.set_footer(text=_bal_footer(uid))
    return emb


# --- Blackjack -----------------------------------------------------------
class BlackjackView(discord.ui.View):
    """Eine laufende Blackjack-Runde mit Buttons. State lebt im View."""

    def __init__(self, channel_id: int, uid: int, bet: int) -> None:
        super().__init__(timeout=BJ_TIMEOUT)
        self.channel_id = channel_id
        self.uid = uid
        self.bet = bet
        self.deck = _new_deck()
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        self.doubled = False
        self.message: discord.Message | None = None
        self._n = 0
        self._sync_buttons()

    # -- Hilfen --
    def _sync_buttons(self) -> None:
        can_double = (len(self.player) == 2 and economy.get_coins(self.uid) >= self.bet)
        self._double.disabled = not can_double

    def _disable_all(self) -> None:
        for ch in self.children:
            if isinstance(ch, discord.ui.Button):
                ch.disabled = True

    def _unregister(self) -> None:
        if _bj_views.get((self.channel_id, self.uid)) is self:
            _bj_views.pop((self.channel_id, self.uid), None)

    def _prompt(self) -> str:
        return "Drück **Karte**, **Stand** oder **Double**. 👇"

    def _payload(self, *, reveal: bool, title: str, color: discord.Color,
                 note: str, state: str = "") -> tuple[discord.Embed, discord.File]:
        self._n += 1
        fname = f"bj_{self.uid}_{self._n}.png"
        buf = render.blackjack_table(
            self.dealer, self.player, hide_hole=not reveal,
            dealer_value=_hand_value(self.dealer), player_value=_hand_value(self.player),
            player_state=state)
        file = discord.File(buf, filename=fname)
        emb = discord.Embed(title=title, description=note or None, color=color)
        emb.set_author(name="🎰 Flo Casino")
        emb.set_image(url=f"attachment://{fname}")
        emb.set_footer(text=f"{_bal_footer(self.uid)}  ·  Einsatz: {self.bet} {economy.COIN}")
        return emb, file

    async def natural_payload(self) -> tuple[discord.Embed, discord.File]:
        """Sofort-Entscheidung bei Natural (21 auf der Hand). Zahlt aus + flush."""
        pv, dv = _hand_value(self.player), _hand_value(self.dealer)
        if pv == 21 and dv == 21:
            economy.add_coins(self.uid, self.bet)
            await economy.flush()
            return self._payload(reveal=True, title="🂡 Push", color=_C_PUSH,
                                 note=f"Beide haben 21 – Einsatz ({self.bet} {economy.COIN}) zurück.",
                                 state="push")
        if pv == 21:
            payout = self.bet + (self.bet * 3) // 2     # 3:2
            economy.add_coins(self.uid, payout)
            await economy.flush()
            return self._payload(reveal=True, title="🂡 BLACKJACK! 🎉", color=_C_BJ,
                                 note=f"Natürlicher Blackjack! +{payout - self.bet} {economy.COIN} (3:2).",
                                 state="blackjack")
        await economy.flush()
        return self._payload(reveal=True, title="🂡 Dealer-Blackjack 😬", color=_C_LOSE,
                             note=f"Der Dealer hat Blackjack. -{self.bet} {economy.COIN}.",
                             state="lose")

    async def _settle(self) -> tuple[str, str, str, discord.Color]:
        """Dealer spielt aus, Ergebnis bestimmen, auszahlen + flush.
        Rueckgabe: (state, note, title, color)."""
        pv = _hand_value(self.player)
        if pv > 21:     # nur nach Double moeglich
            await economy.flush()
            return ("bust", f"Über 21 – verloren. -{self.bet} {economy.COIN}.",
                    "🂡 Bust! 💥", _C_LOSE)
        while _hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        dv = _hand_value(self.dealer)
        if dv > 21 or pv > dv:
            economy.add_coins(self.uid, self.bet * 2)
            await economy.flush()
            grund = "Dealer überkauft sich!" if dv > 21 else f"Deine {pv} schlägt {dv}."
            return ("win", f"{grund} +{self.bet} {economy.COIN}.", "🂡 Gewonnen! 🎉", _C_WIN)
        if pv < dv:
            await economy.flush()
            return ("lose", f"Dealer {dv} schlägt deine {pv}. -{self.bet} {economy.COIN}.",
                    "🂡 Verloren 😬", _C_LOSE)
        economy.add_coins(self.uid, self.bet)
        await economy.flush()
        return ("push", f"Beide {pv} – Einsatz ({self.bet} {economy.COIN}) zurück.",
                "🂡 Push", _C_PUSH)

    async def _mutate(self, action: str) -> tuple:
        """Fuehrt eine Aktion aus. Rueckgabe:
        (finished, state, title, color, note, reveal)."""
        if action == "double":
            if len(self.player) != 2:
                return (False, "", "🂡 Blackjack", _C_PLAY,
                        "Verdoppeln geht nur als allererste Aktion.", False)
            if economy.get_coins(self.uid) < self.bet:
                return (False, "", "🂡 Blackjack", _C_PLAY,
                        f"Zum Verdoppeln fehlen dir {self.bet} {economy.COIN}.", False)
            economy.add_coins(self.uid, -self.bet)
            self.bet += self.bet
            self.doubled = True
            self.player.append(self.deck.pop())
            state, note, title, color = await self._settle()
            return (True, state, title, color, note, True)
        if action == "hit":
            self.player.append(self.deck.pop())
            if _hand_value(self.player) > 21:
                await economy.flush()
                return (True, "bust", "🂡 Bust! 💥", _C_LOSE,
                        f"Über 21 – verloren. -{self.bet} {economy.COIN}.", True)
            self._sync_buttons()
            return (False, "", "🂡 Blackjack", _C_PLAY, self._prompt(), False)
        # stand
        state, note, title, color = await self._settle()
        return (True, state, title, color, note, True)

    async def _step(self, action: str) -> tuple[discord.Embed, discord.File, discord.ui.View, bool]:
        finished, state, title, color, note, reveal = await self._mutate(action)
        emb, file = self._payload(reveal=reveal, title=title, color=color, note=note, state=state)
        if finished:
            self._unregister()
            self._disable_all()
            again = _AgainView(self.uid, "blackjack",
                               {"bet": self._original_bet()}, channel_id=self.channel_id)
            return emb, file, again, True
        return emb, file, self, False

    def _original_bet(self) -> int:
        # Nach einem Double ist self.bet verdoppelt – fuer 'Nochmal' den Grundeinsatz.
        return self.bet // 2 if self.doubled else self.bet

    # -- Buttons --
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.uid:
            return True
        await interaction.response.send_message(
            "Das ist nicht deine Blackjack-Runde. 🃏", ephemeral=True)
        return False

    async def _do(self, interaction: discord.Interaction, action: str) -> None:
        if self.is_finished():
            await interaction.response.defer()
            return
        emb, file, view, ended = await self._step(action)
        await interaction.response.edit_message(embed=emb, view=view, attachments=[file])
        if ended:
            view.message = interaction.message
            self.stop()

    @discord.ui.button(label="Karte", emoji="🃏", style=discord.ButtonStyle.primary)
    async def _karte(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._do(interaction, "hit")

    @discord.ui.button(label="Stand", emoji="✋", style=discord.ButtonStyle.secondary)
    async def _stand(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._do(interaction, "stand")

    @discord.ui.button(label="Double", emoji="💰", style=discord.ButtonStyle.success)
    async def _double(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._do(interaction, "double")

    async def on_timeout(self) -> None:
        self._unregister()
        self._disable_all()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _bj_deal(channel_id: int, uid: int, bet: int
                   ) -> tuple[discord.Embed, discord.File, discord.ui.View, bool]:
    """Teilt eine neue Runde aus (Einsatz ist bereits eingezogen).
    Rueckgabe: (embed, file, view, ended)."""
    view = BlackjackView(channel_id, uid, bet)
    pv, dv = _hand_value(view.player), _hand_value(view.dealer)
    if pv == 21 or dv == 21:
        emb, file = await view.natural_payload()
        again = _AgainView(uid, "blackjack", {"bet": bet}, channel_id=channel_id)
        return emb, file, again, True
    await economy.flush()
    view._sync_buttons()
    emb, file = view._payload(reveal=False, title="🂡 Blackjack",
                              color=_C_PLAY, note=view._prompt())
    return emb, file, view, False


async def _bj_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    ch = message.channel.id
    existing = _bj_views.get((ch, uid))
    if existing and not existing.is_finished():
        await _send(message, embed=_info(
            "Du hast schon eine Blackjack-Runde offen – nutz die **Buttons** drunter. 👇"))
        return HANDLED
    bet, err = _take(uid, _resolve_bet(args[0], uid) if args else None)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    emb, file, view, ended = await _bj_deal(ch, uid, bet)
    msg = await _send(message, embed=emb, file=file, view=view)
    if msg:
        view.message = msg
        if not ended:
            _bj_views[(ch, uid)] = view
    return HANDLED


async def _bj_text_action(message: discord.Message, action: str) -> object | None:
    uid = message.author.id
    ch = message.channel.id
    view = _bj_views.get((ch, uid))
    if not view or view.is_finished():
        return None     # keine offene Runde -> nicht kapern, andere duerfen ran
    emb, file, nview, ended = await view._step(action)
    if view.message is not None:
        try:
            await view.message.edit(embed=emb, view=nview, attachments=[file])
            if ended:
                nview.message = view.message
                view.stop()
        except discord.HTTPException:
            log.exception("Blackjack-Text-Aktion fehlgeschlagen")
    return HANDLED


# --- Crash ---------------------------------------------------------------
def _parse_mult(token: str) -> float | None:
    token = (token or "").lower().rstrip("x").replace(",", ".").strip()
    try:
        v = float(token)
    except ValueError:
        return None
    return v if v > 0 else None


def _crash_point() -> float:
    """Zufaelliger Absturzpunkt. ~2% Sofort-Crash, sonst exponentiell verteilt
    mit kleinem Hausvorteil (Faktor 0.97)."""
    if random.random() < 0.02:
        return 1.00
    cp = 0.97 / (1.0 - random.random())
    return max(1.00, min(round(cp, 2), 1000.0))


async def _play_crash(uid: int, bet: int, target: float
                      ) -> tuple[discord.Embed, discord.File]:
    """Spielt eine Crash-Runde (Einsatz bereits eingezogen)."""
    cp = _crash_point()
    payout = int(bet * target) if cp >= target else 0
    cashed = payout > 0
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()
    color, fname_field, fval = _outcome(bet, payout)
    fn = f"crash_{uid}_{random.randint(1000, 9999)}.png"
    file = discord.File(render.crash_chart(cp, target, cashed), filename=fn)
    if cashed:
        desc = (f"🚀 Die Rakete fliegt bis **{cp:.2f}×** – du bist bei "
                f"**{target:.2f}×** ausgestiegen! 🎉")
    else:
        desc = f"💥 Bei **{cp:.2f}×** zerschellt – dein Ziel war **{target:.2f}×**. 😬"
    emb = discord.Embed(title="🚀 Crash", description=desc, color=color)
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name=fname_field, value=fval, inline=True)
    emb.set_image(url=f"attachment://{fn}")
    emb.set_footer(text=_bal_footer(uid))
    return emb, file


def _crash_target(args: list[str], bet: int) -> tuple[float | None, str | None]:
    target = _parse_mult(args[1]) if len(args) > 1 else None
    if target is None:
        return None, f"Bei welchem Faktor steigst du aus? z. B. `{_bot_name} crash {bet} 2.0`"
    if target < 1.01:
        return None, "Das Ziel muss über 1.0 liegen (z. B. 1.5, 2.0, 5.0)."
    return min(target, 100.0), None


async def _crash_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    raw = _resolve_bet(args[0], uid) if args else None
    bet0, err = _check_bet(uid, raw)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    target, terr = _crash_target(args, bet0)
    if terr:
        await _send(message, embed=_info(terr))
        return HANDLED
    economy.add_coins(uid, -bet0)
    emb, file = await _play_crash(uid, bet0, target)
    again = _AgainView(uid, "crash", {"bet": bet0, "target": target})
    msg = await _send(message, embed=emb, file=file, view=again)
    if msg:
        again.message = msg
    return HANDLED


# --- Keno ----------------------------------------------------------------
_KENO_TABLE: dict[tuple[int, int], int] = {
    (1, 1): 3,
    (2, 1): 1, (2, 2): 9,
    (3, 2): 2, (3, 3): 16,
    (4, 2): 1, (4, 3): 5, (4, 4): 40,
    (5, 3): 2, (5, 4): 15, (5, 5): 100,
    (6, 3): 1, (6, 4): 5, (6, 5): 40, (6, 6): 200,
    (7, 4): 3, (7, 5): 20, (7, 6): 100, (7, 7): 500,
    (8, 4): 2, (8, 5): 10, (8, 6): 50, (8, 7): 200, (8, 8): 1000,
}


def _parse_picks(s: str) -> list[int]:
    picks: list[int] = []
    for t in (s or "").replace(",", " ").split():
        if t.isdigit():
            n = int(t)
            if 1 <= n <= 40 and n not in picks:
                picks.append(n)
    return picks[:8]


async def _play_keno(uid: int, bet: int, picks: list[int]
                     ) -> tuple[discord.Embed, None]:
    draw = random.sample(range(1, 41), 10)
    hits = sorted(set(picks) & set(draw))
    mult = _KENO_TABLE.get((len(picks), len(hits)), 0)
    payout = int(bet * mult)
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()
    color, fname, fval = _outcome(bet, payout)
    drawn = " ".join(f"**__{n}__**" if n in hits else f"{n}" for n in sorted(draw))
    pk = " ".join(f"**{n}**" if n in hits else f"~~{n}~~" for n in sorted(picks))
    emb = discord.Embed(title="🎱 Keno", description=f"Gezogen: {drawn}", color=color)
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name="Deine Zahlen", value=pk, inline=False)
    emb.add_field(name="Treffer", value=f"{len(hits)}/{len(picks)}  →  ×{mult}", inline=True)
    emb.add_field(name=fname, value=fval, inline=True)
    emb.set_footer(text=_bal_footer(uid))
    return emb, None


async def _keno_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    if not args:
        await _send(message, embed=_info(
            f"So geht's: `{_bot_name} keno <einsatz> <1-8 Zahlen 1-40>` "
            f"– z. B. `{_bot_name} keno 50 3 7 12 21`"))
        return HANDLED
    raw = _resolve_bet(args[0], uid)
    bet0, err = _check_bet(uid, raw)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    picks = _parse_picks(" ".join(args[1:]))
    if not picks:
        await _send(message, embed=_info(
            f"Tippe 1–8 Zahlen von 1 bis 40. z. B. `{_bot_name} keno {bet0} 3 7 12 21`"))
        return HANDLED
    economy.add_coins(uid, -bet0)
    emb, _ = await _play_keno(uid, bet0, picks)
    again = _AgainView(uid, "keno", {"bet": bet0, "picks": picks})
    msg = await _send(message, embed=emb, view=again)
    if msg:
        again.message = msg
    return HANDLED


# --- Roulette ------------------------------------------------------------
_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}

_EVEN_MONEY = {
    "rot": (lambda s: s in _RED, "Rot"),
    "red": (lambda s: s in _RED, "Rot"),
    "schwarz": (lambda s: s not in _RED and s != 0, "Schwarz"),
    "black": (lambda s: s not in _RED and s != 0, "Schwarz"),
    "gerade": (lambda s: s != 0 and s % 2 == 0, "Gerade"),
    "even": (lambda s: s != 0 and s % 2 == 0, "Gerade"),
    "ungerade": (lambda s: s % 2 == 1, "Ungerade"),
    "odd": (lambda s: s % 2 == 1, "Ungerade"),
    "1-18": (lambda s: 1 <= s <= 18, "1–18"),
    "low": (lambda s: 1 <= s <= 18, "1–18"),
    "klein": (lambda s: 1 <= s <= 18, "1–18"),
    "19-36": (lambda s: 19 <= s <= 36, "19–36"),
    "high": (lambda s: 19 <= s <= 36, "19–36"),
    "gross": (lambda s: 19 <= s <= 36, "19–36"),
    "groß": (lambda s: 19 <= s <= 36, "19–36"),
}


def _roulette_payout(target: str, bet: int, spin: int) -> tuple[int | None, str]:
    """Auszahlung (inkl. Einsatz) fuer einen Roulette-Tipp. (None, ...) = ungueltig."""
    if target in _EVEN_MONEY:
        check, label = _EVEN_MONEY[target]
        return (bet * 2 if check(spin) else 0), label
    num = target.replace("zahl", "").strip()
    if num.isdigit():
        n = int(num)
        if 0 <= n <= 36:
            return (bet * 36 if spin == n else 0), f"Zahl {n}"
    return None, target


async def _play_roulette(uid: int, bet: int, target: str
                         ) -> tuple[discord.Embed, None]:
    spin = random.randint(0, 36)
    payout, label = _roulette_payout(target, bet, spin)
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()
    color, fname, fval = _outcome(bet, payout)
    spin_color = "🟢" if spin == 0 else ("🔴" if spin in _RED else "⚫")
    emb = discord.Embed(
        title="🎡 Roulette",
        description=f"Die Kugel fällt auf **{spin}** {spin_color}\nDein Tipp: **{label}**",
        color=color,
    )
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name=fname, value=fval, inline=True)
    emb.set_footer(text=_bal_footer(uid))
    return emb, None


async def _roulette_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    if len(args) < 2:
        await _send(message, embed=_info(
            f"So geht's: `{_bot_name} roulette <einsatz> <auf>` – auf: rot/schwarz, "
            f"gerade/ungerade, 1-18/19-36 oder eine Zahl 0-36."))
        return HANDLED
    raw = _resolve_bet(args[0], uid)
    bet0, err = _check_bet(uid, raw)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    target = " ".join(args[1:]).lower().strip()
    test, _ = _roulette_payout(target, 1, 0)
    if test is None:
        await _send(message, embed=_info(
            f"Worauf? rot/schwarz, gerade/ungerade, 1-18/19-36 oder eine Zahl 0-36. "
            f"z. B. `{_bot_name} roulette {bet0} rot`"))
        return HANDLED
    economy.add_coins(uid, -bet0)
    emb, _ = await _play_roulette(uid, bet0, target)
    again = _AgainView(uid, "roulette", {"bet": bet0, "target": target})
    msg = await _send(message, embed=emb, view=again)
    if msg:
        again.message = msg
    return HANDLED


# --- Wiederholen / Formulare (Buttons & Modals) --------------------------
async def _replay(uid: int, kind: str, params: dict
                  ) -> tuple[discord.Embed, discord.File | None]:
    if kind == "crash":
        return await _play_crash(uid, params["bet"], params["target"])
    if kind == "keno":
        return await _play_keno(uid, params["bet"], params["picks"])
    if kind == "roulette":
        return await _play_roulette(uid, params["bet"], params["target"])
    raise ValueError(kind)


class _BetModal(discord.ui.Modal):
    """Formular fuer Einsatz (+ Spiel-Extra). Startet das jeweilige Spiel."""

    def __init__(self, kind: str, uid: int, *, title: str, params: dict | None = None) -> None:
        super().__init__(title=title)
        self.kind = kind
        self.uid = uid
        params = params or {}
        self.bet = discord.ui.TextInput(
            label="Einsatz", placeholder="z. B. 50 oder alles",
            default=str(params["bet"]) if params.get("bet") else None, max_length=12)
        self.add_item(self.bet)
        self.extra: discord.ui.TextInput | None = None
        if kind == "crash":
            self.extra = discord.ui.TextInput(
                label="Ziel-Faktor", placeholder="z. B. 2.0",
                default=str(params.get("target", "2.0")), max_length=8)
        elif kind == "keno":
            self.extra = discord.ui.TextInput(
                label="Zahlen (1–40, mit Leerzeichen)", placeholder="z. B. 3 7 12 21",
                default=" ".join(map(str, params.get("picks", []))) or None, max_length=60)
        elif kind == "roulette":
            self.extra = discord.ui.TextInput(
                label="Tipp", placeholder="rot / schwarz / gerade / 17",
                default=str(params.get("target", "rot")), max_length=20)
        if self.extra is not None:
            self.add_item(self.extra)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uid = self.uid
        bet, err = _check_bet(uid, _resolve_bet(self.bet.value, uid))
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        if self.kind == "blackjack":
            economy.add_coins(uid, -bet)
            emb, file, view, ended = await _bj_deal(interaction.channel_id, uid, bet)
            await interaction.response.send_message(embed=emb, file=file, view=view)
            msg = await interaction.original_response()
            view.message = msg
            if not ended:
                _bj_views[(interaction.channel_id, uid)] = view
            return

        if self.kind == "crash":
            target = _parse_mult(self.extra.value)
            if target is None or target < 1.01:
                await interaction.response.send_message(
                    "Ziel muss eine Zahl über 1.0 sein (z. B. 2.0).", ephemeral=True)
                return
            target = min(target, 100.0)
            economy.add_coins(uid, -bet)
            emb, file = await _play_crash(uid, bet, target)
            again = _AgainView(uid, "crash", {"bet": bet, "target": target})
            await interaction.response.send_message(embed=emb, file=file, view=again)
            again.message = await interaction.original_response()
            return

        if self.kind == "keno":
            picks = _parse_picks(self.extra.value)
            if not picks:
                await interaction.response.send_message(
                    "Tippe 1–8 Zahlen von 1 bis 40 (mit Leerzeichen).", ephemeral=True)
                return
            economy.add_coins(uid, -bet)
            emb, _ = await _play_keno(uid, bet, picks)
            again = _AgainView(uid, "keno", {"bet": bet, "picks": picks})
            await interaction.response.send_message(embed=emb, view=again)
            again.message = await interaction.original_response()
            return

        if self.kind == "roulette":
            target = self.extra.value.strip().lower()
            test, _ = _roulette_payout(target, 1, 0)
            if test is None:
                await interaction.response.send_message(
                    "Worauf? rot/schwarz, gerade/ungerade, 1-18/19-36 oder Zahl 0–36.",
                    ephemeral=True)
                return
            economy.add_coins(uid, -bet)
            emb, _ = await _play_roulette(uid, bet, target)
            again = _AgainView(uid, "roulette", {"bet": bet, "target": target})
            await interaction.response.send_message(embed=emb, view=again)
            again.message = await interaction.original_response()
            return


_MODAL_TITLES = {
    "blackjack": "Blackjack – Einsatz",
    "crash": "Crash – Einsatz & Ziel",
    "keno": "Keno – Einsatz & Zahlen",
    "roulette": "Roulette – Einsatz & Tipp",
}


class _AgainView(discord.ui.View):
    """'Nochmal'-Buttons unter einem Ergebnis. Nur der Spieler darf klicken."""

    def __init__(self, uid: int, kind: str, params: dict, *, channel_id: int | None = None) -> None:
        super().__init__(timeout=120)
        self.uid = uid
        self.kind = kind
        self.params = params
        self.channel_id = channel_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.uid:
            return True
        await interaction.response.send_message(
            "Spiel doch deine eigene Runde. 😉", ephemeral=True)
        return False

    @discord.ui.button(label="Nochmal", emoji="🔁", style=discord.ButtonStyle.success)
    async def _again(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        uid = self.uid
        bet, err = _check_bet(uid, self.params.get("bet"))
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        economy.add_coins(uid, -bet)
        params = {**self.params, "bet": bet}

        if self.kind == "blackjack":
            ch = self.channel_id or interaction.channel_id
            emb, file, view, ended = await _bj_deal(ch, uid, bet)
            await interaction.response.edit_message(embed=emb, view=view, attachments=[file])
            view.message = interaction.message
            if not ended:
                _bj_views[(ch, uid)] = view
            return

        emb, file = await _replay(uid, self.kind, params)
        again = _AgainView(uid, self.kind, params, channel_id=self.channel_id)
        attachments = [file] if file is not None else []
        await interaction.response.edit_message(embed=emb, view=again, attachments=attachments)
        again.message = interaction.message

    @discord.ui.button(label="Einsatz ändern", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def _change(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            _BetModal(self.kind, self.uid, title=_MODAL_TITLES.get(self.kind, "Einsatz"),
                      params=self.params))

    async def on_timeout(self) -> None:
        for ch in self.children:
            if isinstance(ch, discord.ui.Button):
                ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class CasinoHubView(discord.ui.View):
    """Casino-Uebersicht: ein Button je Spiel, der ein Formular oeffnet."""

    def __init__(self, uid: int) -> None:
        super().__init__(timeout=180)
        self.uid = uid
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.uid:
            return True
        await interaction.response.send_message(
            f"Öffne dir dein eigenes Casino mit `{_bot_name} casino`. 🎰", ephemeral=True)
        return False

    async def _open(self, interaction: discord.Interaction, kind: str, params=None) -> None:
        await interaction.response.send_modal(
            _BetModal(kind, self.uid, title=_MODAL_TITLES[kind], params=params))

    @discord.ui.button(label="Blackjack", emoji="🂡", style=discord.ButtonStyle.primary)
    async def _bj(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "blackjack")

    @discord.ui.button(label="Crash", emoji="🚀", style=discord.ButtonStyle.primary)
    async def _crash(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "crash", {"target": "2.0"})

    @discord.ui.button(label="Keno", emoji="🎱", style=discord.ButtonStyle.secondary)
    async def _keno(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "keno")

    @discord.ui.button(label="Roulette", emoji="🎡", style=discord.ButtonStyle.secondary)
    async def _roulette(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "roulette", {"target": "rot"})

    async def on_timeout(self) -> None:
        for ch in self.children:
            if isinstance(ch, discord.ui.Button):
                ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
