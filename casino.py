"""Casino-Feature fuer Flo (Pack 5): spielen mit Flo Coins – mit Buttons,
Formularen (Modals) und echten GIF-Animationen.

Spiele (nach 'Flo'):
- casino                      Uebersicht mit Buttons je Spiel (oeffnet ein Formular)
- blackjack <einsatz>         17-und-4 gegen den Dealer, gesteuert per Buttons
                              (Karte / Stand / Double) – Karten als Bild
- crash <einsatz> <ziel>      Rakete steigt – animierte Kurve, cash vor dem Absturz
- keno <einsatz> <1-8 zahlen> tippe Zahlen 1-40, 10 werden animiert gezogen
- roulette <einsatz> <auf>    rot/schwarz, gerade/ungerade, 1-18/19-36 oder Zahl 0-36
                              – der Kessel dreht sich als GIF
- mines <einsatz> [minen]     5x4-Feld voller Buttons: Diamanten sammeln,
                              vor der Bombe aussteigen (Cashout)
- rad <einsatz>               Gluecksrad mit Multiplikatoren (animiert)
- rubbellos <einsatz>         3x3-Rubbellos: drei Gleiche in einer Reihe gewinnen
- duell @wer <einsatz>        Muenz-Duell gegen ein Mitglied - Gewinner nimmt alles
- stats [@wer]                persoenliche Casino-Bilanz als Bild

Alles laeuft ueber EINEN Coin-Topf: economy.py. Tippen funktioniert weiter als
Fallback (gut bei Neustarts) – die Buttons sind nur der bequeme Weg. Offene
Blackjack-/Mines-Runden leben im Speicher und verfallen nach Timeout.
Alle GIF-/PNG-Renderings der Spielergebnisse laufen via asyncio.to_thread,
damit der Event-Loop nie blockiert.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time

import discord

import ai
import economy
import render
from store import JsonStore

log = logging.getLogger("dcbot.casino")

# Sentinel: Casino hat selbst geantwortet (Embed/Bild/Buttons) -> bot.py schweigt.
HANDLED = object()


def _protect(msg) -> None:
    """Meldet eine laufende Spiel-Nachricht beim Auto-Loesch-Schutz an (damit sie
    im #commands-Channel nicht mitten im Spiel verschwindet). Lazy-Import von bot,
    um Zirkel-Importe zu vermeiden; faellt der Bot weg (Tests), passiert nichts."""
    if msg is None:
        return
    try:
        import bot
        bot.protect_message(msg)
    except Exception:
        pass


def _release(msg) -> None:
    """Gibt eine Spiel-Nachricht wieder frei (Runde vorbei / kein Reagieren mehr)
    -> der Bot raeumt sie nach kurzer Gnadenfrist weg."""
    if msg is None:
        return
    try:
        import bot
        bot.release_message(msg)
    except Exception:
        pass

_enabled: bool = False
_bot_name: str = "Flo"
_stats: JsonStore | None = None   # Casino-Bilanz je Spieler (data/casino.json)

MIN_BET = 1
MAX_BET = int(os.getenv("CASINO_MAX_BET", "100000") or "100000")
BJ_TIMEOUT = 180        # Sekunden, bis eine offene Blackjack-Runde verfaellt
MINES_TIMEOUT = 180     # Sekunden, bis eine offene Mines-Runde auto-cashoutet

# Aktive Blackjack-Runden je (channel_id, user_id) -> BlackjackView. Nur im Speicher.
_bj_views: "dict[tuple[int, int], BlackjackView]" = {}
# Aktive Mines-Runden je (channel_id, user_id) -> MinesView. Nur im Speicher.
_mines_views: "dict[tuple[int, int], MinesView]" = {}

# Farben
_C_PLAY = discord.Color.blurple()
_C_WIN = discord.Color.green()
_C_LOSE = discord.Color.red()
_C_PUSH = discord.Color.greyple()
_C_BJ = discord.Color.gold()


def setup() -> bool:
    """Aktiviert das Casino. Voraussetzung: economy (Flo Coins) ist aktiv."""
    global _enabled, _bot_name, _stats
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("CASINO_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Casino-Feature aus (CASINO_ENABLED=0).")
        return False
    if not economy.is_enabled():
        log.info("Casino-Feature aus: economy (Flo Coins) ist nicht aktiv.")
        return False
    _stats = JsonStore("casino.json", default={"stats": {}})
    _enabled = True
    log.info("Casino-Feature aktiv (Einsatz %d–%d %s, mit Buttons, GIFs & Bilanz).",
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
        return 0, f"Dafür reicht's nicht – du hast {bal} {economy.COIN}."
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
        return _C_PUSH, "Ergebnis", "±0 – Einsatz zurück"
    return _C_LOSE, "Verlust", f"-{bet} {economy.COIN}"


def _err(text: str) -> discord.Embed:
    return discord.Embed(description=f"⚠️ {text}", color=_C_LOSE)


def _info(text: str) -> discord.Embed:
    return discord.Embed(description=text, color=_C_BJ)


# --- Casino-Bilanz (Stats) ------------------------------------------------
def _stats_profile(uid: int) -> dict:
    assert _stats is not None
    prof = _stats.data.setdefault("stats", {}).setdefault(
        str(uid), {"games": 0, "wagered": 0, "payout": 0, "best_win": 0, "per": {}})
    return prof


async def record(uid: int, game: str, bet: int, payout: int) -> None:
    """Verbucht eine gespielte Runde in der Casino-Bilanz (auch games.py nutzt
    das fuer Slots/Coinflip). Speichert asynchron; Fehler bleiben lokal."""
    if not _enabled or _stats is None or bet <= 0:
        return
    try:
        prof = _stats_profile(uid)
        prof["games"] += 1
        prof["wagered"] += bet
        prof["payout"] += payout
        net = payout - bet
        if net > prof.get("best_win", 0):
            prof["best_win"] = net
        g = prof["per"].setdefault(game, {"n": 0, "net": 0})
        g["n"] += 1
        g["net"] += net
        await _stats.save()
    except Exception:
        log.exception("Casino-Bilanz konnte nicht gespeichert werden")


# --- Render-Helfer: Animation in Thread, Standbild als Fallback ------------
async def _anim(anim_fn, static_fn, *args) -> tuple:
    """Rendert das Spielergebnis als GIF in einem Thread (Event-Loop bleibt
    frei). Faellt die Animation aus, kommt das Standbild (PNG). Rueckgabe:
    (BytesIO, dateiendung)."""
    try:
        return await asyncio.to_thread(anim_fn, *args), "gif"
    except Exception:
        log.exception("Animation fehlgeschlagen - nutze Standbild")
        if static_fn is None:
            raise
        return await asyncio.to_thread(static_fn, *args), "png"


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
        msg = await message.reply(**kwargs)
    except discord.HTTPException:
        log.exception("Casino-Antwort konnte nicht gesendet werden")
        return None
    if view is not None:
        _protect(msg)   # aktives Spiel -> vorm Auto-Loeschen schuetzen
    return msg


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
        # Ohne Einsatz im Text -> bequemes Formular (Buttons) statt Tipp-Aufforderung.
        return await (_open_setup(message, "blackjack") if not args
                      else _bj_command(message, args))
    if first in ("hit", "karte", "ziehen", "zieh"):
        return await _bj_text_action(message, "hit")
    if first in ("stand", "stehen", "bleiben", "bleib", "pass", "genug", "fertig"):
        return await _bj_text_action(message, "stand")
    if first in ("double", "doppeln", "verdoppeln", "dd"):
        return await _bj_text_action(message, "double")
    if first in ("crash", "absturz", "rakete", "rocket"):
        return await (_open_setup(message, "crash") if not args
                      else _crash_command(message, args))
    if first == "keno":
        return await (_open_setup(message, "keno") if not args
                      else _keno_command(message, args))
    if first in ("roulette", "roul", "kessel"):
        return await (_open_setup(message, "roulette") if not args
                      else _roulette_command(message, args))
    if first in ("mines", "minen", "mine", "minesweeper", "bomben"):
        return await (_open_setup(message, "mines") if not args
                      else _mines_command(message, args))
    if first in ("cashout", "auszahlen"):
        # Text-Fallback fuer eine laufende Mines-Runde (falls Buttons haken).
        return await _mines_text_cashout(message)
    # 'fortune' bleibt bewusst beim Horoskop (fun.py) - hier nur Rad-Aliasse.
    if first in ("glücksrad", "gluecksrad", "rad", "wheel"):
        return await (_open_setup(message, "wheel") if not args
                      else _wheel_command(message, args))
    if first in ("rubbellos", "rubbel", "scratch", "los", "lose"):
        # 'los'/'lose' sind Alltagswoerter: nur als Rubbellos deuten, wenn sie
        # allein stehen oder ein Einsatz folgt ('flo los 50'). 'flo los gehts'
        # o. Ae. faellt durch an die naechsten Handler / die KI.
        if first in ("los", "lose") and args and _resolve_bet(args[0], message.author.id) is None:
            return None
        return await (_open_setup(message, "scratch") if not args
                      else _scratch_command(message, args))
    if first in ("duell", "duel"):
        return await _duel_command(message, args)
    if first in ("stats", "statistik", "statistiken", "bilanz"):
        return await _stats_command(message)
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
    emb.add_field(name="💣 Mines", value="Diamanten sammeln, Bombe meiden", inline=True)
    emb.add_field(name="🍀 Glücksrad", value="dreh um bis zu ×3", inline=True)
    emb.add_field(name="🎫 Rubbellos", value="3 Gleiche in einer Reihe", inline=True)
    emb.add_field(name="⚔️ Duell", value=f"`{_bot_name} duell @wer 100`", inline=True)
    emb.add_field(name="📊 Bilanz", value=f"`{_bot_name} stats`", inline=True)
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
        # Terminal-Guard gegen Doppel-Settlement: wird SYNCHRON gesetzt, bevor
        # eine Runde ausgezahlt wird. Ein zweiter Klick/Text-Befehl im selben
        # Moment (eigener Task!) darf _settle nie zweimal durchlaufen.
        self.settled = False
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
            await record(self.uid, "blackjack", self.bet, self.bet)
            return self._payload(reveal=True, title="🂡 Push", color=_C_PUSH,
                                 note=f"Beide haben 21 – Einsatz ({self.bet} {economy.COIN}) zurück.",
                                 state="push")
        if pv == 21:
            payout = self.bet + (self.bet * 3) // 2     # 3:2
            economy.add_coins(self.uid, payout)
            await economy.flush()
            await record(self.uid, "blackjack", self.bet, payout)
            return self._payload(reveal=True, title="🂡 BLACKJACK! 🎉", color=_C_BJ,
                                 note=f"Natürlicher Blackjack! +{payout - self.bet} {economy.COIN} (3:2).",
                                 state="blackjack")
        await economy.flush()
        await record(self.uid, "blackjack", self.bet, 0)
        return self._payload(reveal=True, title="🂡 Dealer-Blackjack 😬", color=_C_LOSE,
                             note=f"Der Dealer hat Blackjack. -{self.bet} {economy.COIN}.",
                             state="lose")

    async def _settle(self) -> tuple[str, str, str, discord.Color]:
        """Dealer spielt aus, Ergebnis bestimmen, auszahlen + flush.
        Rueckgabe: (state, note, title, color)."""
        pv = _hand_value(self.player)
        if pv > 21:     # nur nach Double moeglich
            await economy.flush()
            await record(self.uid, "blackjack", self.bet, 0)
            return ("bust", f"Über 21 – verloren. -{self.bet} {economy.COIN}.",
                    "🂡 Bust! 💥", _C_LOSE)
        while _hand_value(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
        dv = _hand_value(self.dealer)
        if dv > 21 or pv > dv:
            economy.add_coins(self.uid, self.bet * 2)
            await economy.flush()
            await record(self.uid, "blackjack", self.bet, self.bet * 2)
            grund = "Dealer überkauft sich!" if dv > 21 else f"Deine {pv} schlägt {dv}."
            return ("win", f"{grund} +{self.bet} {economy.COIN}.", "🂡 Gewonnen! 🎉", _C_WIN)
        if pv < dv:
            await economy.flush()
            await record(self.uid, "blackjack", self.bet, 0)
            return ("lose", f"Dealer {dv} schlägt deine {pv}. -{self.bet} {economy.COIN}.",
                    "🂡 Verloren 😬", _C_LOSE)
        economy.add_coins(self.uid, self.bet)
        await economy.flush()
        await record(self.uid, "blackjack", self.bet, self.bet)
        return ("push", f"Beide {pv} – Einsatz ({self.bet} {economy.COIN}) zurück.",
                "🂡 Push", _C_PUSH)

    async def _mutate(self, action: str) -> tuple:
        """Fuehrt eine Aktion aus. Rueckgabe:
        (finished, state, title, color, note, reveal).

        self.settled wird SYNCHRON vor dem ersten Auszahlungs-Await gesetzt -
        so kann ein parallel dispatchter zweiter Klick/Text-Befehl die Runde
        nie doppelt abrechnen (das waere freie Coin-Erzeugung)."""
        if action == "double":
            if len(self.player) != 2:
                return (False, "", "🂡 Blackjack", _C_PLAY,
                        "Verdoppeln geht nur als allererste Aktion.", False)
            if economy.get_coins(self.uid) < self.bet:
                return (False, "", "🂡 Blackjack", _C_PLAY,
                        f"Zum Verdoppeln fehlen dir {self.bet} {economy.COIN}.", False)
            self.settled = True
            economy.add_coins(self.uid, -self.bet)
            self.bet += self.bet
            self.doubled = True
            self.player.append(self.deck.pop())
            state, note, title, color = await self._settle()
            return (True, state, title, color, note, True)
        if action == "hit":
            self.player.append(self.deck.pop())
            if _hand_value(self.player) > 21:
                self.settled = True
                await economy.flush()
                await record(self.uid, "blackjack", self.bet, 0)
                return (True, "bust", "🂡 Bust! 💥", _C_LOSE,
                        f"Über 21 – verloren. -{self.bet} {economy.COIN}.", True)
            self._sync_buttons()
            return (False, "", "🂡 Blackjack", _C_PLAY, self._prompt(), False)
        # stand
        self.settled = True
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
        # settled-Check VOR allem anderen: die Runde ist evtl. schon abgerechnet,
        # waehrend ein zweiter Klick noch in der Warteschlange hing.
        if self.settled or self.is_finished():
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
        if not self.settled:
            # Runde verfallen (dokumentiert): Einsatz ist weg - das gehoert
            # auch in die Casino-Bilanz, sonst rechnet 'flo stats' zu schoen.
            await record(self.uid, "blackjack", self.bet, 0)
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            _release(self.message)   # keine Reaktion mehr -> Nachricht freigeben


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
    elif not ended:
        # Anzeige fehlgeschlagen -> Runde abbrechen und Einsatz zurueckgeben,
        # sonst waere er stumm weg (die View wurde nie sichtbar/registriert).
        view.stop()
        economy.add_coins(uid, bet)
        await economy.flush()
    return HANDLED


async def _bj_text_action(message: discord.Message, action: str) -> object | None:
    uid = message.author.id
    ch = message.channel.id
    view = _bj_views.get((ch, uid))
    if not view or view.is_finished() or view.settled:
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
    await record(uid, "crash", bet, payout)
    color, fname_field, fval = _outcome(bet, payout)
    buf, ext = await _anim(render.crash_chart_anim, render.crash_chart, cp, target, cashed)
    fn = f"crash_{uid}_{random.randint(1000, 9999)}.{ext}"
    file = discord.File(buf, filename=fn)
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
                     ) -> tuple[discord.Embed, discord.File]:
    draw = random.sample(range(1, 41), 10)
    hits = sorted(set(picks) & set(draw))
    mult = _KENO_TABLE.get((len(picks), len(hits)), 0)
    payout = int(bet * mult)
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()
    await record(uid, "keno", bet, payout)
    color, res_name, res_val = _outcome(bet, payout)
    buf, ext = await _anim(render.keno_grid_anim, render.keno_grid, picks, draw, hits)
    fn = f"keno_{uid}_{random.randint(1000, 9999)}.{ext}"
    file = discord.File(buf, filename=fn)
    emb = discord.Embed(
        title="🎱 Keno",
        description=f"**{len(hits)}** von **{len(picks)}** getroffen  →  Faktor **×{mult}**",
        color=color)
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name=res_name, value=res_val, inline=True)
    emb.set_image(url=f"attachment://{fn}")
    emb.set_footer(text=_bal_footer(uid))
    return emb, file


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
    emb, file = await _play_keno(uid, bet0, picks)
    again = _AgainView(uid, "keno", {"bet": bet0, "picks": picks})
    msg = await _send(message, embed=emb, file=file, view=again)
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
                         ) -> tuple[discord.Embed, discord.File]:
    spin = random.randint(0, 36)
    payout, label = _roulette_payout(target, bet, spin)
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()
    await record(uid, "roulette", bet, payout)
    color, res_name, res_val = _outcome(bet, payout)
    buf, ext = await _anim(render.roulette_wheel_anim, render.roulette_wheel,
                           spin, payout > 0)
    fn = f"roul_{uid}_{random.randint(1000, 9999)}.{ext}"
    file = discord.File(buf, filename=fn)
    spin_color = "🟢" if spin == 0 else ("🔴" if spin in _RED else "⚫")
    emb = discord.Embed(
        title="🎡 Roulette",
        description=f"Die Kugel fällt auf **{spin}** {spin_color}\nDein Tipp: **{label}**",
        color=color,
    )
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name=res_name, value=res_val, inline=True)
    emb.set_image(url=f"attachment://{fn}")
    emb.set_footer(text=_bal_footer(uid))
    return emb, file


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
    emb, file = await _play_roulette(uid, bet0, target)
    again = _AgainView(uid, "roulette", {"bet": bet0, "target": target})
    msg = await _send(message, embed=emb, file=file, view=again)
    if msg:
        again.message = msg
    return HANDLED


# --- Gluecksrad ------------------------------------------------------------
# 12 Segmente; Erwartungswert 0.95 (kleiner Hausvorteil wie bei den anderen
# Spielen). 0 = Niete, <1 = Teil vom Einsatz zurueck, >1 = Gewinn.
_WHEEL_SEGMENTS = [0, 0.5, 1.2, 0, 2.0, 0.2, 0, 1.5, 0.5, 3.0, 0, 2.5]


async def _play_wheel(uid: int, bet: int) -> tuple[discord.Embed, discord.File]:
    """Dreht das Gluecksrad (Einsatz bereits eingezogen)."""
    idx = random.randrange(len(_WHEEL_SEGMENTS))
    mult = _WHEEL_SEGMENTS[idx]
    payout = int(bet * mult)
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()
    await record(uid, "glücksrad", bet, payout)
    color, res_name, res_val = _outcome(bet, payout)
    if mult <= 0:
        desc = "Das Rad bleibt auf der **Niete** stehen. 😬"
    elif mult < 1:
        desc = f"Das Rad zeigt **×{mult:g}** – ein Trostpreis. 🙃"
    else:
        desc = f"Das Rad stoppt auf **×{mult:g}**! 🎉"
    emb = discord.Embed(title="🍀 Glücksrad", description=desc, color=color)
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name=res_name, value=res_val, inline=True)
    emb.set_footer(text=_bal_footer(uid))
    file = None
    try:
        buf, ext = await _anim(render.wheel_fortune_anim, None, _WHEEL_SEGMENTS, idx)
        fn = f"rad_{uid}_{random.randint(1000, 9999)}.{ext}"
        file = discord.File(buf, filename=fn)
        emb.set_image(url=f"attachment://{fn}")
    except Exception:
        # Ohne Bild weiterspielen - das Ergebnis steht im Text, Coins stimmen.
        log.exception("Gluecksrad-Animation fehlgeschlagen - Ergebnis als Text")
    return emb, file


async def _wheel_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    bet0, err = _take(uid, _resolve_bet(args[0], uid) if args else None)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    emb, file = await _play_wheel(uid, bet0)
    again = _AgainView(uid, "wheel", {"bet": bet0})
    msg = await _send(message, embed=emb, file=file, view=again)
    if msg:
        again.message = msg
    return HANDLED


# --- Rubbellos -------------------------------------------------------------
# 3 Gleiche in einer WAAGRECHTEN Reihe gewinnen. Multiplikator ersetzt den
# Einsatz (wie bei Keno). Erwartungswert ~0.92.
_SCRATCH_PAYOUT = {"seven": 40, "diamond": 22, "star": 15, "bar": 10,
                   "grape": 8, "lemon": 6, "cherry": 4}


def _scratch_roll() -> tuple[list[str], list[int], int]:
    """Wuerfelt ein 3x3-Los. Rueckgabe: (9 Symbole, Gewinn-Reihen, Multiplikator)."""
    keys = [random.choice(render.SLOT_KEYS) for _ in range(9)]
    rows = [r for r in range(3) if keys[3 * r] == keys[3 * r + 1] == keys[3 * r + 2]]
    mult = sum(_SCRATCH_PAYOUT[keys[3 * r]] for r in rows)
    return keys, rows, mult


async def _play_scratch(uid: int, bet: int) -> tuple[discord.Embed, discord.File]:
    """Rubbelt ein Los frei (Einsatz bereits eingezogen)."""
    keys, rows, mult = _scratch_roll()
    payout = bet * mult
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()
    await record(uid, "rubbellos", bet, payout)
    color, res_name, res_val = _outcome(bet, payout)
    if rows:
        desc = f"**{len(rows)}** Gewinn-Reihe(n)  →  Faktor **×{mult}** 🎉"
    else:
        desc = "Keine drei Gleichen in einer Reihe. 😬"
    emb = discord.Embed(title="🎫 Rubbellos", description=desc, color=color)
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name=res_name, value=res_val, inline=True)
    emb.set_footer(text=_bal_footer(uid))
    file = None
    try:
        buf, ext = await _anim(render.scratch_card_anim, None, keys, rows,
                               max(0, payout - bet))
        fn = f"los_{uid}_{random.randint(1000, 9999)}.{ext}"
        file = discord.File(buf, filename=fn)
        emb.set_image(url=f"attachment://{fn}")
    except Exception:
        log.exception("Rubbellos-Animation fehlgeschlagen - Ergebnis als Text")
    return emb, file


async def _scratch_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    bet0, err = _take(uid, _resolve_bet(args[0], uid) if args else None)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    emb, file = await _play_scratch(uid, bet0)
    again = _AgainView(uid, "scratch", {"bet": bet0})
    msg = await _send(message, embed=emb, file=file, view=again)
    if msg:
        again.message = msg
    return HANDLED


# --- Mines -----------------------------------------------------------------
# 20 Felder (5x4), darunter 1-8 Bomben. Jedes sichere Feld erhoeht den
# Multiplikator fair (Wahrscheinlichkeits-Kehrwert) mit 3% Hausvorteil.
_MINES_TILES = 20
_MINES_DEFAULT = 3
_MINES_MAX = 8


def _mines_mult(picked: int, mines: int) -> float:
    """Multiplikator nach ``picked`` sicheren Feldern bei ``mines`` Bomben."""
    if picked <= 0:
        return 1.0
    m = 1.0
    for i in range(picked):
        m *= (_MINES_TILES - i) / (_MINES_TILES - mines - i)
    return round(0.97 * m, 2)


class _MineTile(discord.ui.Button):
    """Ein Feld im Mines-Raster."""

    def __init__(self, idx: int) -> None:
        super().__init__(emoji="❓", style=discord.ButtonStyle.secondary, row=idx // 5)
        self.idx = idx
        self.safe_open = False

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._pick(interaction, self)


class _MinesCashout(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Cashout", emoji="💰",
                         style=discord.ButtonStyle.success, row=4, disabled=True)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._cashout(interaction)


class _MinesAgainBtn(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Nochmal", emoji="🔁",
                         style=discord.ButtonStyle.success, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        v: "MinesView" = self.view  # type: ignore[assignment]
        if v.is_finished():          # Doppelklick: nur der erste Klick startet neu
            await interaction.response.defer()
            return
        bet, err = _check_bet(v.uid, v.bet)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        v.stop()   # synchron VOR dem Abzug: schliesst das Doppelklick-Fenster
        economy.add_coins(v.uid, -bet)
        await economy.flush()
        nv = MinesView(v.channel_id, v.uid, bet, v.mines)
        nv.message = interaction.message
        _mines_views[(v.channel_id, v.uid)] = nv
        try:
            await interaction.response.edit_message(embed=nv._embed(), view=nv)
        except discord.HTTPException:
            log.exception("Mines-Nochmal: Anzeige fehlgeschlagen - Einsatz zurueck")
            nv.settled = True
            nv.stop()
            if _mines_views.get((v.channel_id, v.uid)) is nv:
                _mines_views.pop((v.channel_id, v.uid), None)
            economy.add_coins(v.uid, bet)
            await economy.flush()
            return
        _protect(interaction.message)


class _MinesBetBtn(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Einsatz ändern", emoji="✏️",
                         style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        v: "MinesView" = self.view  # type: ignore[assignment]
        await interaction.response.send_modal(
            _BetModal("mines", v.uid, title=_MODAL_TITLES["mines"],
                      params={"bet": v.bet, "mines": v.mines}))


class MinesView(discord.ui.View):
    """Eine laufende Mines-Runde: 20 Feld-Buttons + Cashout. Der Einsatz ist
    beim Erzeugen bereits eingezogen."""

    def __init__(self, channel_id: int, uid: int, bet: int, mines: int) -> None:
        super().__init__(timeout=MINES_TIMEOUT)
        self.channel_id = channel_id
        self.uid = uid
        self.bet = bet
        self.mines = max(1, min(int(mines), _MINES_MAX))
        self.mine_set = set(random.sample(range(_MINES_TILES), self.mines))
        self.picked = 0
        self.settled = False
        self.message: discord.Message | None = None
        self.tiles = [_MineTile(i) for i in range(_MINES_TILES)]
        for t in self.tiles:
            self.add_item(t)
        self.cash = _MinesCashout()
        self.add_item(self.cash)

    # -- Anzeige --
    def _embed(self, *, color: discord.Color | None = None,
               note: str | None = None) -> discord.Embed:
        cur = _mines_mult(self.picked, self.mines)
        nxt = _mines_mult(self.picked + 1, self.mines)
        emb = discord.Embed(
            title="💣 Mines",
            description=note or ("Deck die 💎 auf – aber erwisch keine 💣!\n"
                                 "Steig mit **Cashout** aus, solange du vorne liegst."),
            color=color or _C_PLAY)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=f"{self.bet} {economy.COIN}", inline=True)
        emb.add_field(name="Bomben", value=f"{self.mines} 💣", inline=True)
        emb.add_field(name="Aufgedeckt",
                      value=f"{self.picked}/{_MINES_TILES - self.mines} 💎", inline=True)
        if not self.settled:
            cash_txt = (f"**{int(self.bet * cur)}** {economy.COIN} (×{cur:.2f})"
                        if self.picked else "_erst ein Feld aufdecken_")
            emb.add_field(name="Cashout", value=cash_txt, inline=True)
            emb.add_field(name="Nächstes Feld", value=f"×{nxt:.2f}", inline=True)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    def _reveal_all(self, boom_idx: int | None = None) -> None:
        for t in self.tiles:
            t.disabled = True
            if t.idx in self.mine_set:
                t.emoji = "💥" if t.idx == boom_idx else "💣"
                t.style = (discord.ButtonStyle.danger if t.idx == boom_idx
                           else discord.ButtonStyle.secondary)
            elif t.safe_open:
                t.emoji = "💎"
                t.style = discord.ButtonStyle.success
            else:
                t.emoji = "▪️"
        self.cash.disabled = True

    def _finish_buttons(self) -> None:
        """Ergebnis steht -> Nochmal/Einsatz-aendern anbieten (Feld bleibt sichtbar)."""
        self.add_item(_MinesAgainBtn())
        self.add_item(_MinesBetBtn())

    def _unregister(self) -> None:
        if _mines_views.get((self.channel_id, self.uid)) is self:
            _mines_views.pop((self.channel_id, self.uid), None)

    # -- Spielzuege --
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.uid:
            return True
        await interaction.response.send_message(
            f"Das ist nicht dein Minenfeld – starte deins mit `{_bot_name} mines`. 💣",
            ephemeral=True)
        return False

    async def _pick(self, interaction: discord.Interaction, tile: _MineTile) -> None:
        if self.settled or tile.disabled:
            await interaction.response.defer()
            return
        if tile.idx in self.mine_set:
            self.settled = True
            self._unregister()
            self._reveal_all(boom_idx=tile.idx)
            self._finish_buttons()
            await economy.flush()
            await record(self.uid, "mines", self.bet, 0)
            emb = self._embed(color=_C_LOSE,
                              note=f"**BOOM!** 💥 Feld {tile.idx + 1} war eine Bombe. "
                                   f"-{self.bet} {economy.COIN}.")
            await interaction.response.edit_message(embed=emb, view=self)
            return
        tile.safe_open = True
        tile.disabled = True
        tile.emoji = "💎"
        tile.style = discord.ButtonStyle.success
        self.picked += 1
        self.cash.disabled = False
        if self.picked >= _MINES_TILES - self.mines:
            await self._cashout(interaction)   # alles sicher aufgedeckt -> auto
            return
        cur = _mines_mult(self.picked, self.mines)
        self.cash.label = f"Cashout {int(self.bet * cur)}"
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _settle_cashout(self) -> tuple[int, float]:
        """Zahlt den aktuellen Stand aus. Rueckgabe: (payout, multiplikator)."""
        mult = _mines_mult(self.picked, self.mines)
        payout = int(self.bet * mult)
        economy.add_coins(self.uid, payout)
        await economy.flush()
        await record(self.uid, "mines", self.bet, payout)
        return payout, mult

    async def _cashout(self, interaction: discord.Interaction) -> None:
        if self.settled or self.picked <= 0:
            await interaction.response.defer()
            return
        self.settled = True
        self._unregister()
        payout, mult = await self._settle_cashout()
        self._reveal_all()
        self._finish_buttons()
        net = payout - self.bet
        color = _C_WIN if net > 0 else (_C_PUSH if net == 0 else _C_LOSE)
        emb = self._embed(color=color,
                          note=f"**Cashout!** 💰 ×{mult:.2f} → "
                               f"**{'+' if net >= 0 else ''}{net} {economy.COIN}**.")
        await interaction.response.edit_message(embed=emb, view=self)

    async def on_timeout(self) -> None:
        # Keine Reaktion mehr: laufende Runde fair beenden - mit mind. einem
        # aufgedeckten Feld wird automatisch ausgezahlt, sonst Einsatz zurueck.
        if not self.settled:
            self.settled = True
            # Gehoert die Nachricht inzwischen einer NEUEN Runde (Nochmal)?
            # Dann nur still abrechnen - nicht deren Board zerschiessen.
            fremd = _mines_views.get((self.channel_id, self.uid)) not in (None, self)
            self._unregister()
            if self.picked > 0:
                payout, mult = await self._settle_cashout()
                note = (f"⏰ Zeit um – automatischer Cashout bei ×{mult:.2f} "
                        f"(**{payout} {economy.COIN}**).")
            else:
                economy.add_coins(self.uid, self.bet)
                await economy.flush()
                note = "⏰ Zeit um – Einsatz zurück."
            if fremd:
                return
            self._reveal_all()
            if self.message is not None:
                try:
                    await self.message.edit(embed=self._embed(color=_C_PUSH, note=note),
                                            view=self)
                except discord.HTTPException:
                    pass
        else:
            for ch in self.children:
                if isinstance(ch, discord.ui.Button):
                    ch.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass
        if self.message is not None:
            _release(self.message)


async def _mines_text_cashout(message: discord.Message) -> "object | None":
    """`flo cashout` als Text: zahlt die laufende Mines-Runde aus (Fallback,
    falls die Buttons haken). Ohne offene Runde: None -> andere Handler/KI."""
    view = _mines_views.get((message.channel.id, message.author.id))
    if not view or view.is_finished() or view.settled or view.picked <= 0:
        return None
    view.settled = True
    view._unregister()
    payout, mult = await view._settle_cashout()
    view._reveal_all()
    view._finish_buttons()
    net = payout - view.bet
    color = _C_WIN if net > 0 else (_C_PUSH if net == 0 else _C_LOSE)
    emb = view._embed(color=color,
                      note=f"**Cashout!** 💰 ×{mult:.2f} → "
                           f"**{'+' if net >= 0 else ''}{net} {economy.COIN}**.")
    if view.message is not None:
        try:
            await view.message.edit(embed=emb, view=view)
        except discord.HTTPException:
            log.exception("Mines-Text-Cashout: Nachricht konnte nicht editiert werden")
    return HANDLED


def _parse_mines_count(args: list[str]) -> int:
    for a in args[1:]:
        if a.isdigit():
            return max(1, min(int(a), _MINES_MAX))
    return _MINES_DEFAULT


async def _mines_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    ch = message.channel.id
    existing = _mines_views.get((ch, uid))
    if existing and not existing.is_finished() and not existing.settled:
        await _send(message, embed=_info(
            "Du hast hier schon ein Minenfeld offen – spiel es zu Ende. 💣"))
        return HANDLED
    bet, err = _take(uid, _resolve_bet(args[0], uid) if args else None)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    await economy.flush()   # Mines-Runden leben laenger - Abzug sofort sichern
    view = MinesView(ch, uid, bet, _parse_mines_count(args))
    msg = await _send(message, embed=view._embed(), view=view)
    if msg:
        view.message = msg
        _mines_views[(ch, uid)] = view
    else:
        # Anzeige fehlgeschlagen -> Runde abbrechen, Einsatz zurueck.
        view.settled = True
        view.stop()
        economy.add_coins(uid, bet)
        await economy.flush()
    return HANDLED


# --- Muenz-Duell (PvP) ------------------------------------------------------
class DuelView(discord.ui.View):
    """Herausforderung: nur der Herausgeforderte darf annehmen/ablehnen, der
    Herausforderer darf zurueckziehen. Coins fliessen erst bei Annahme."""

    def __init__(self, challenger: discord.Member, target: discord.Member,
                 bet: int) -> None:
        super().__init__(timeout=120)
        self.challenger = challenger
        self.target = target
        self.bet = bet
        self.done = False
        self.message: discord.Message | None = None

    def embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="⚔️ Münz-Duell",
            description=(f"{self.challenger.mention} fordert {self.target.mention} "
                         f"heraus – Einsatz **{self.bet} {economy.COIN}** pro Kopf.\n"
                         f"{self.challenger.display_name} = **KOPF** · "
                         f"{self.target.display_name} = **ZAHL**.\n"
                         f"{self.target.mention}, nimmst du an? (120s)"),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        return emb

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in (self.target.id, self.challenger.id):
            return True
        await interaction.response.send_message(
            "Das Duell geht nur die beiden an. 🍿", ephemeral=True)
        return False

    def _disable(self) -> None:
        for ch in self.children:
            if isinstance(ch, discord.ui.Button):
                ch.disabled = True

    @discord.ui.button(label="Annehmen", emoji="⚔️", style=discord.ButtonStyle.success)
    async def _accept(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if interaction.user.id != self.target.id:
            await interaction.response.send_message(
                "Annehmen kann nur der Herausgeforderte. 😉", ephemeral=True)
            return
        if self.done:
            await interaction.response.defer()
            return
        cid, tid = self.challenger.id, self.target.id
        if economy.get_coins(cid) < self.bet or economy.get_coins(tid) < self.bet:
            self.done = True
            self._disable()
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="⚔️ Münz-Duell geplatzt",
                    description="Einer von euch hat nicht mehr genug Coins. 😅",
                    color=_C_PUSH),
                view=self)
            self.stop()
            _release(self.message)
            return
        self.done = True
        economy.add_coins(cid, -self.bet)
        economy.add_coins(tid, -self.bet)
        face = random.choice(["kopf", "zahl"])
        winner = self.challenger if face == "kopf" else self.target
        loser = self.target if face == "kopf" else self.challenger
        pot = self.bet * 2
        economy.add_coins(winner.id, pot)
        await economy.flush()
        await record(winner.id, "duell", self.bet, pot)
        await record(loser.id, "duell", self.bet, 0)
        buf, ext = await _anim(render.coin_flip_anim, render.coin_flip, face)
        fn = f"duell_{winner.id}_{random.randint(1000, 9999)}.{ext}"
        file = discord.File(buf, filename=fn)
        emb = discord.Embed(
            title="⚔️ Münz-Duell",
            description=(f"Die Münze zeigt **{face.upper()}**!\n"
                         f"🏆 {winner.mention} gewinnt den Pott: "
                         f"**+{self.bet} {economy.COIN}** "
                         f"(von {loser.display_name})."),
            color=_C_WIN)
        emb.set_author(name="🎰 Flo Casino")
        emb.set_image(url=f"attachment://{fn}")
        self._disable()
        await interaction.response.edit_message(embed=emb, attachments=[file], view=self)
        self.stop()
        _release(self.message)

    @discord.ui.button(label="Ablehnen", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def _decline(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if self.done:
            await interaction.response.defer()
            return
        self.done = True
        wer = ("zieht zurück" if interaction.user.id == self.challenger.id
               else "lehnt ab")
        self._disable()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⚔️ Münz-Duell",
                description=f"**{interaction.user.display_name}** {wer}. Kein Duell heute. 🕊️",
                color=_C_PUSH),
            view=self)
        self.stop()
        _release(self.message)

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(
                    embed=discord.Embed(
                        title="⚔️ Münz-Duell",
                        description=f"{self.target.display_name} hat nicht reagiert – "
                                    "Duell verfallen.",
                        color=_C_PUSH),
                    view=self)
            except discord.HTTPException:
                pass
            _release(self.message)


async def _duel_command(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    target = next((m for m in message.mentions if not m.bot), None)
    if target is None:
        await _send(message, embed=_info(
            f"Wen forderst du heraus? `{_bot_name} duell @wer <einsatz>`"))
        return HANDLED
    if target.id == uid:
        await _send(message, embed=_info("Gegen dich selbst? Die Münze gewinnt immer. 😄"))
        return HANDLED
    bet = next((_resolve_bet(a, uid) for a in args
                if _resolve_bet(a, uid) is not None), None)
    bet, err = _check_bet(uid, bet)
    if err:
        await _send(message, embed=_err(err))
        return HANDLED
    if economy.get_coins(target.id) < bet:
        await _send(message, embed=_info(
            f"**{target.display_name}** hat keine {bet} {economy.COIN} – "
            "such dir ein reicheres Opfer. 😏"))
        return HANDLED
    view = DuelView(message.author, target, bet)
    msg = await _send(message, embed=view.embed(), view=view)
    if msg:
        view.message = msg
    return HANDLED


# --- Casino-Bilanz anzeigen -------------------------------------------------
async def _fetch_avatar(user) -> "bytes | None":
    try:
        return await asyncio.wait_for(user.display_avatar.with_size(128).read(), 6)
    except Exception:  # noqa: BLE001 - Avatar ist nur Deko
        return None


async def _stats_command(message: discord.Message) -> object:
    target = next((m for m in message.mentions if not m.bot), None) or message.author
    prof = ((_stats.data.get("stats") or {}).get(str(target.id))
            if _stats is not None else None)
    if not prof or not prof.get("games"):
        await _send(message, embed=_info(
            f"**{target.display_name}** hat noch keine Casino-Runde gespielt. "
            f"`{_bot_name} casino` wartet. 🎰"))
        return HANDLED
    avatar = await _fetch_avatar(target)
    try:
        buf = await asyncio.to_thread(render.casino_stats_card,
                                      target.display_name, avatar, prof)
    except Exception:
        log.exception("Stats-Karte fehlgeschlagen - Text-Fallback")
        net = prof.get("payout", 0) - prof.get("wagered", 0)
        emb = _info(f"**{target.display_name}** – {prof.get('games', 0)} Runden, "
                    f"Netto {'+' if net >= 0 else ''}{net} {economy.COIN}.")
        await _send(message, embed=emb)
        return HANDLED
    fn = f"stats_{target.id}.png"
    emb = discord.Embed(title=f"📊 Casino-Bilanz – {target.display_name}", color=_C_BJ)
    emb.set_author(name="🎰 Flo Casino")
    emb.set_image(url=f"attachment://{fn}")
    emb.set_footer(text=_bal_footer(target.id))
    await _send(message, embed=emb, file=discord.File(buf, filename=fn))
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
    if kind == "wheel":
        return await _play_wheel(uid, params["bet"])
    if kind == "scratch":
        return await _play_scratch(uid, params["bet"])
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
        elif kind == "mines":
            self.extra = discord.ui.TextInput(
                label=f"Bomben (1–{_MINES_MAX})", placeholder="z. B. 3",
                default=str(params.get("mines", _MINES_DEFAULT)), max_length=2)
        if self.extra is not None:
            self.add_item(self.extra)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        uid = self.uid
        bet, err = _check_bet(uid, _resolve_bet(self.bet.value, uid))
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return

        if self.kind == "blackjack":
            ch = interaction.channel_id
            existing = _bj_views.get((ch, uid))
            if existing and not existing.is_finished():
                await interaction.response.send_message(
                    "Du hast schon eine Blackjack-Runde offen – nutz deren Buttons. 👇",
                    ephemeral=True)
                return
            economy.add_coins(uid, -bet)
            emb, file, view, ended = await _bj_deal(ch, uid, bet)
            try:
                await interaction.response.send_message(embed=emb, file=file, view=view)
                msg = await interaction.original_response()
            except discord.HTTPException:
                # Anzeige fehlgeschlagen: laufende Runde abbrechen + Einsatz
                # zurueck (bei ended ist die Runde schon korrekt verbucht).
                log.exception("Blackjack-Formular: Anzeige fehlgeschlagen")
                if not ended:
                    view.stop()
                    economy.add_coins(uid, bet)
                    await economy.flush()
                return
            view.message = msg
            _protect(msg)
            if not ended:
                _bj_views[(ch, uid)] = view
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
            _protect(again.message)
            return

        if self.kind == "keno":
            picks = _parse_picks(self.extra.value)
            if not picks:
                await interaction.response.send_message(
                    "Tippe 1–8 Zahlen von 1 bis 40 (mit Leerzeichen).", ephemeral=True)
                return
            economy.add_coins(uid, -bet)
            emb, file = await _play_keno(uid, bet, picks)
            again = _AgainView(uid, "keno", {"bet": bet, "picks": picks})
            await interaction.response.send_message(embed=emb, file=file, view=again)
            again.message = await interaction.original_response()
            _protect(again.message)
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
            emb, file = await _play_roulette(uid, bet, target)
            again = _AgainView(uid, "roulette", {"bet": bet, "target": target})
            await interaction.response.send_message(embed=emb, file=file, view=again)
            again.message = await interaction.original_response()
            _protect(again.message)
            return

        if self.kind in ("wheel", "scratch"):
            economy.add_coins(uid, -bet)
            emb, file = await _replay(uid, self.kind, {"bet": bet})
            again = _AgainView(uid, self.kind, {"bet": bet})
            if file is not None:
                await interaction.response.send_message(embed=emb, file=file, view=again)
            else:
                await interaction.response.send_message(embed=emb, view=again)
            again.message = await interaction.original_response()
            _protect(again.message)
            return

        if self.kind == "mines":
            raw = (self.extra.value or "").strip()
            mines = max(1, min(int(raw), _MINES_MAX)) if raw.isdigit() else _MINES_DEFAULT
            ch = interaction.channel_id
            existing = _mines_views.get((ch, uid))
            if existing and not existing.is_finished() and not existing.settled:
                await interaction.response.send_message(
                    "Du hast hier schon ein Minenfeld offen. 💣", ephemeral=True)
                return
            economy.add_coins(uid, -bet)
            await economy.flush()
            view = MinesView(ch, uid, bet, mines)
            try:
                await interaction.response.send_message(embed=view._embed(), view=view)
                msg = await interaction.original_response()
            except discord.HTTPException:
                log.exception("Mines-Formular: Anzeige fehlgeschlagen - Einsatz zurueck")
                view.settled = True
                view.stop()
                economy.add_coins(uid, bet)
                await economy.flush()
                return
            view.message = msg
            _protect(msg)
            _mines_views[(ch, uid)] = view
            return


_MODAL_TITLES = {
    "blackjack": "Blackjack – Einsatz",
    "crash": "Crash – Einsatz & Ziel",
    "keno": "Keno – Einsatz & Zahlen",
    "roulette": "Roulette – Einsatz & Tipp",
    "mines": "Mines – Einsatz & Bomben",
    "wheel": "Glücksrad – Einsatz",
    "scratch": "Rubbellos – Einsatz",
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
        # Doppelklick-Fenster schliessen: der zweite (bereits dispatchte) Klick
        # sieht is_finished() und tut nichts - sonst wuerde er ERNEUT abbuchen.
        if self.is_finished():
            await interaction.response.defer()
            return
        bet, err = _check_bet(uid, self.params.get("bet"))
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        ch = self.channel_id or interaction.channel_id
        if self.kind == "blackjack":
            existing = _bj_views.get((ch, uid))
            if existing and not existing.is_finished():
                await interaction.response.send_message(
                    "Du hast schon eine Blackjack-Runde offen – nutz deren Buttons. 👇",
                    ephemeral=True)
                return
        # stop() VOR dem Abzug (synchron): ab hier ist diese View entwertet.
        self.stop()
        economy.add_coins(uid, -bet)
        params = {**self.params, "bet": bet}

        if self.kind == "blackjack":
            emb, file, view, ended = await _bj_deal(ch, uid, bet)
            view.message = interaction.message
            if not ended:
                # Vor dem Edit registrieren: so greifen die Text-Befehle
                # (flo karte/stand) selbst dann, wenn der Edit fehlschlaegt.
                _bj_views[(ch, uid)] = view
            try:
                await interaction.response.edit_message(embed=emb, view=view,
                                                        attachments=[file])
            except discord.HTTPException:
                log.exception("Blackjack-Nochmal: Anzeige fehlgeschlagen")
                return
            _protect(interaction.message)
            return

        emb, file = await _replay(uid, self.kind, params)
        again = _AgainView(uid, self.kind, params, channel_id=self.channel_id)
        attachments = [file] if file is not None else []
        try:
            await interaction.response.edit_message(embed=emb, view=again,
                                                    attachments=attachments)
        except discord.HTTPException:
            log.exception("Nochmal: Anzeige fehlgeschlagen (Runde ist verbucht)")
            _release(interaction.message)
            return
        again.message = interaction.message
        _protect(interaction.message)

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
            _release(self.message)   # keine Reaktion mehr -> Nachricht freigeben


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

    async def _open(self, interaction: discord.Interaction, kind: str) -> None:
        """Oeffnet den interaktiven Aufbau (Einsatz-Auswahl + Spiel-Buttons) als
        eigene Nachricht – kein Tippen noetig."""
        view = _SETUPS[kind](self.uid, channel_id=interaction.channel_id)
        await interaction.response.send_message(embed=view._embed(), view=view)
        try:
            view.message = await interaction.original_response()
            _protect(view.message)
        except discord.HTTPException:
            log.exception("Casino-Hub: Spielaufbau konnte nicht geoeffnet werden")

    @discord.ui.button(label="Blackjack", emoji="🂡", style=discord.ButtonStyle.primary, row=0)
    async def _bj(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "blackjack")

    @discord.ui.button(label="Crash", emoji="🚀", style=discord.ButtonStyle.primary, row=0)
    async def _crash(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "crash")

    @discord.ui.button(label="Keno", emoji="🎱", style=discord.ButtonStyle.secondary, row=0)
    async def _keno(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "keno")

    @discord.ui.button(label="Roulette", emoji="🎡", style=discord.ButtonStyle.secondary, row=0)
    async def _roulette(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "roulette")

    @discord.ui.button(label="Mines", emoji="💣", style=discord.ButtonStyle.primary, row=1)
    async def _mines(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "mines")

    @discord.ui.button(label="Glücksrad", emoji="🍀", style=discord.ButtonStyle.primary, row=1)
    async def _wheel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "wheel")

    @discord.ui.button(label="Rubbellos", emoji="🎫", style=discord.ButtonStyle.secondary, row=1)
    async def _scratch(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._open(interaction, "scratch")

    @discord.ui.button(label="Bilanz", emoji="📊", style=discord.ButtonStyle.secondary, row=1)
    async def _bilanz(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        """Zeigt die eigene Casino-Bilanz ephemer (nur fuer den Klicker)."""
        uid = interaction.user.id
        prof = ((_stats.data.get("stats") or {}).get(str(uid))
                if _stats is not None else None)
        if not prof or not prof.get("games"):
            await interaction.response.send_message(
                "Du hast noch keine Runde gespielt – such dir oben ein Spiel aus. 🎰",
                ephemeral=True)
            return
        # Sofort bestaetigen (3s-Frist!) - Avatar-Download darf bis zu 6s dauern.
        await interaction.response.defer(ephemeral=True, thinking=True)
        avatar = await _fetch_avatar(interaction.user)
        try:
            buf = await asyncio.to_thread(render.casino_stats_card,
                                          interaction.user.display_name, avatar, prof)
        except Exception:
            log.exception("Bilanz-Karte fehlgeschlagen")
            net = prof.get("payout", 0) - prof.get("wagered", 0)
            await interaction.followup.send(
                f"{prof.get('games', 0)} Runden, Netto "
                f"{'+' if net >= 0 else ''}{net} {economy.COIN}.", ephemeral=True)
            return
        await interaction.followup.send(
            file=discord.File(buf, filename=f"stats_{uid}.png"), ephemeral=True)

    async def on_timeout(self) -> None:
        for ch in self.children:
            if isinstance(ch, discord.ui.Button):
                ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            _release(self.message)   # keine Reaktion mehr -> Nachricht freigeben


# =========================================================================
#  Interaktiver Spielaufbau: Einsatz per Dropdown, Spielzug per Button.
#  So muss man im Chat NICHTS mehr tippen – `Flo keno` oeffnet direkt das
#  Menue. (Discord laesst Formulare/Modals nur nach einem Klick zu, darum
#  ist der Einstieg ein Button-Menue statt eines Pop-ups.)
# =========================================================================
_BET_CHOICES = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


class _BetSelect(discord.ui.Select):
    """Dropdown fuer den Einsatz (feste Stufen + 'Alles')."""

    def __init__(self) -> None:
        options = [discord.SelectOption(label=f"{v} {economy.COIN}", value=str(v), emoji="🪙")
                   for v in _BET_CHOICES]
        options.append(discord.SelectOption(
            label="Alles", value="all", emoji="💰", description="Dein ganzer Kontostand"))
        super().__init__(placeholder="💰 Einsatz wählen …", min_values=1, max_values=1,
                         options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._set_bet(interaction, self.values[0])


class _Setup(discord.ui.View):
    """Basis fuer den interaktiven Aufbau einer Runde. Haelt den gewaehlten
    Einsatz, baut das Erklaer-Embed und raeumt sich beim Timeout selbst weg."""

    kind = ""

    def __init__(self, uid: int, *, channel_id: int | None = None,
                 bet: int | None = None) -> None:
        super().__init__(timeout=120)
        self.uid = uid
        self.channel_id = channel_id
        self.bet = bet
        self.message: discord.Message | None = None
        self.add_item(_BetSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.uid:
            return True
        await interaction.response.send_message(
            f"Mach dir deine eigene Runde mit `{_bot_name} {self.kind}`. 😉", ephemeral=True)
        return False

    async def _set_bet(self, interaction: discord.Interaction, token: str) -> None:
        self.bet = _resolve_bet(token, self.uid)
        # Auswahl im Dropdown sichtbar halten (sonst 'vergisst' es die Optik
        # bei jedem Re-Render der View).
        for child in self.children:
            if isinstance(child, _BetSelect):
                for opt in child.options:
                    opt.default = (opt.value == token)
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _ensure_bet(self, interaction: discord.Interaction) -> "int | None":
        """Prueft den gewaehlten Einsatz. Bei Problem: kurzer ephemerer Hinweis."""
        bet, err = _check_bet(self.uid, self.bet)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return None
        return bet

    async def _claim_bet(self, interaction: discord.Interaction) -> "int | None":
        """Doppelklick-sicher spielen: prueft den Einsatz, entwertet das Menue
        (stop) und zieht ein - alles SYNCHRON am Stueck, damit ein zweiter,
        parallel dispatchter Klick nie ein zweites Mal abbuchen kann."""
        if self.is_finished():
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return None
        bet, err = _check_bet(self.uid, self.bet)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return None
        self.stop()
        economy.add_coins(self.uid, -bet)
        return bet

    async def _finish(self, interaction: discord.Interaction, emb: discord.Embed,
                      file: "discord.File | None", again: "_AgainView") -> None:
        """Ersetzt das Aufbau-Menue durch das Ergebnis (+ Nochmal-Buttons)."""
        attachments = [file] if file is not None else []
        await interaction.response.edit_message(embed=emb, attachments=attachments, view=again)
        again.message = interaction.message or self.message
        _protect(again.message)
        self.stop()

    def _bet_txt(self) -> str:
        return f"**{self.bet} {economy.COIN}**" if self.bet else "_noch nichts gewählt_"

    def _embed(self) -> discord.Embed:    # von den Unterklassen gefuellt
        raise NotImplementedError

    async def on_timeout(self) -> None:
        for ch in self.children:
            if isinstance(ch, (discord.ui.Button, discord.ui.Select)):
                ch.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            _release(self.message)


# --- Keno: Zahlen per Auswahlmenue, kein Tippen --------------------------
class _NumberSelect(discord.ui.Select):
    """Mehrfach-Auswahl fuer einen Zahlenblock (z. B. 1–20)."""

    def __init__(self, lo: int, hi: int, label: str, *, row: int) -> None:
        self.chosen: list[int] = []
        options = [discord.SelectOption(label=str(n), value=str(n)) for n in range(lo, hi + 1)]
        super().__init__(placeholder=f"🔢 {label}", min_values=0, max_values=8,
                         options=options, row=row)

    def set_chosen(self, nums: list[int]) -> None:
        self.chosen = list(nums)
        for opt in self.options:
            opt.default = int(opt.value) in self.chosen

    async def callback(self, interaction: discord.Interaction) -> None:
        self.set_chosen([int(v) for v in self.values])
        self.view._recalc()
        await interaction.response.edit_message(embed=self.view._embed())


class _KenoSetup(_Setup):
    kind = "keno"

    def __init__(self, uid: int, *, channel_id: int | None = None,
                 bet: int | None = None) -> None:
        super().__init__(uid, channel_id=channel_id, bet=bet)
        self.picks: list[int] = []
        self._lo = _NumberSelect(1, 20, "Zahlen 1–20", row=1)
        self._hi = _NumberSelect(21, 40, "Zahlen 21–40", row=2)
        self.add_item(self._lo)
        self.add_item(self._hi)

    def _recalc(self) -> None:
        self.picks = sorted(set(self._lo.chosen) | set(self._hi.chosen))[:8]

    def _embed(self) -> discord.Embed:
        nums = "  ".join(f"`{n}`" for n in self.picks) if self.picks else "_keine_"
        emb = discord.Embed(
            title="🎱 Keno",
            description=("Wähle **Einsatz** und **1–8 Zahlen** (1–40), dann **Spielen**.\n"
                         "Es werden 10 Zahlen gezogen – je mehr Treffer, desto mehr Gewinn."),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=self._bet_txt(), inline=True)
        emb.add_field(name=f"Deine Zahlen ({len(self.picks)}/8)", value=nums, inline=False)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    @discord.ui.button(label="Zufall", emoji="🎲", style=discord.ButtonStyle.secondary, row=3)
    async def _rng(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        pick = random.sample(range(1, 41), 5)
        self._lo.set_chosen([n for n in pick if n <= 20])
        self._hi.set_chosen([n for n in pick if n > 20])
        self._recalc()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Spielen", emoji="▶️", style=discord.ButtonStyle.success, row=3)
    async def _go(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        self._recalc()
        if not self.picks:
            await interaction.response.send_message(
                "Wähle erst 1–8 Zahlen (oder 🎲 Zufall).", ephemeral=True)
            return
        bet = await self._claim_bet(interaction)
        if bet is None:
            return
        emb, file = await _play_keno(self.uid, bet, self.picks)
        again = _AgainView(self.uid, "keno", {"bet": bet, "picks": self.picks},
                           channel_id=self.channel_id)
        await self._finish(interaction, emb, file, again)


# --- Roulette: ein Klick = sofort drehen ---------------------------------
class _NumberBetModal(discord.ui.Modal):
    """Kleines Formular fuer eine exakte Roulette-Zahl (0–36)."""

    def __init__(self, setup: "_RouletteSetup") -> None:
        super().__init__(title="Roulette – Zahl 0–36")
        self.setup = setup
        self.num = discord.ui.TextInput(label="Zahl", placeholder="0–36", max_length=2)
        self.add_item(self.num)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s = self.setup
        if s.is_finished():   # Menue wurde inzwischen anders benutzt/geschlossen
            await interaction.response.send_message(
                "Das Menü ist schon zu – starte einfach eine neue Runde. 🙂",
                ephemeral=True)
            return
        raw = (self.num.value or "").strip()
        if not raw.isdigit() or not (0 <= int(raw) <= 36):
            await interaction.response.send_message("Bitte eine Zahl von 0 bis 36.", ephemeral=True)
            return
        bet, err = _check_bet(s.uid, s.bet)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        s.stop()   # synchron VOR dem Abzug: entwertet Doppel-Wege ins Menue
        economy.add_coins(s.uid, -bet)
        emb, file = await _play_roulette(s.uid, bet, raw)
        again = _AgainView(s.uid, "roulette", {"bet": bet, "target": raw},
                           channel_id=s.channel_id)
        if s.message is not None:
            try:
                await s.message.edit(embed=emb, attachments=[file], view=again)
                again.message = s.message
                _protect(s.message)
            except discord.HTTPException:
                log.exception("Roulette-Zahl: Ergebnis konnte nicht angezeigt werden")
                _release(s.message)   # kein Schutz-Leak: Nachricht freigeben
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass


class _RouletteSetup(_Setup):
    kind = "roulette"

    def _embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="🎡 Roulette",
            description=("**Einsatz** wählen, dann **worauf** du tippst – die Kugel rollt sofort.\n"
                         "Außenwetten (Rot/Schwarz/…): ×2 · eine exakte Zahl: ×36."),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=self._bet_txt(), inline=True)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    async def _spin(self, interaction: discord.Interaction, target: str) -> None:
        bet = await self._claim_bet(interaction)
        if bet is None:
            return
        emb, file = await _play_roulette(self.uid, bet, target)
        again = _AgainView(self.uid, "roulette", {"bet": bet, "target": target},
                           channel_id=self.channel_id)
        await self._finish(interaction, emb, file, again)

    @discord.ui.button(label="Rot", emoji="🔴", style=discord.ButtonStyle.danger, row=1)
    async def _red(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._spin(i, "rot")

    @discord.ui.button(label="Schwarz", emoji="⚫", style=discord.ButtonStyle.secondary, row=1)
    async def _black(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._spin(i, "schwarz")

    @discord.ui.button(label="Gerade", style=discord.ButtonStyle.primary, row=1)
    async def _even(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._spin(i, "gerade")

    @discord.ui.button(label="Ungerade", style=discord.ButtonStyle.primary, row=1)
    async def _odd(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._spin(i, "ungerade")

    @discord.ui.button(label="Zahl", emoji="🔢", style=discord.ButtonStyle.success, row=1)
    async def _number(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await i.response.send_modal(_NumberBetModal(self))

    @discord.ui.button(label="1–18", style=discord.ButtonStyle.secondary, row=2)
    async def _low(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._spin(i, "1-18")

    @discord.ui.button(label="19–36", style=discord.ButtonStyle.secondary, row=2)
    async def _high(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._spin(i, "19-36")


# --- Crash: Ziel-Faktor per Button ---------------------------------------
_CRASH_TARGETS = (1.5, 2.0, 3.0, 5.0, 10.0)


class _CrashTargetModal(discord.ui.Modal):
    """Formular fuer einen eigenen Crash-Ziel-Faktor."""

    def __init__(self, setup: "_CrashSetup") -> None:
        super().__init__(title="Crash – eigenes Ziel")
        self.setup = setup
        self.target = discord.ui.TextInput(label="Ziel-Faktor", placeholder="z. B. 2.5", max_length=8)
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s = self.setup
        if s.is_finished():   # Menue wurde inzwischen anders benutzt/geschlossen
            await interaction.response.send_message(
                "Das Menü ist schon zu – starte einfach eine neue Runde. 🙂",
                ephemeral=True)
            return
        target = _parse_mult(self.target.value)
        if target is None or target < 1.01:
            await interaction.response.send_message(
                "Ziel muss eine Zahl über 1.0 sein (z. B. 2.5).", ephemeral=True)
            return
        target = min(target, 100.0)
        bet, err = _check_bet(s.uid, s.bet)
        if err:
            await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
            return
        s.stop()   # synchron VOR dem Abzug: entwertet Doppel-Wege ins Menue
        economy.add_coins(s.uid, -bet)
        emb, file = await _play_crash(s.uid, bet, target)
        again = _AgainView(s.uid, "crash", {"bet": bet, "target": target},
                           channel_id=s.channel_id)
        if s.message is not None:
            try:
                await s.message.edit(embed=emb, attachments=[file], view=again)
                again.message = s.message
                _protect(s.message)
            except discord.HTTPException:
                log.exception("Crash: Ergebnis konnte nicht angezeigt werden")
                _release(s.message)   # kein Schutz-Leak: Nachricht freigeben
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass


class _CrashSetup(_Setup):
    kind = "crash"

    def _embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="🚀 Crash",
            description=("**Einsatz** wählen, dann **Ziel-Faktor** – die Rakete startet sofort.\n"
                         "Erreicht sie dein Ziel, kassierst du Einsatz × Faktor. Sonst weg. 💥"),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=self._bet_txt(), inline=True)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    async def _launch(self, interaction: discord.Interaction, target: float) -> None:
        bet = await self._claim_bet(interaction)
        if bet is None:
            return
        emb, file = await _play_crash(self.uid, bet, target)
        again = _AgainView(self.uid, "crash", {"bet": bet, "target": target},
                           channel_id=self.channel_id)
        await self._finish(interaction, emb, file, again)

    @discord.ui.button(label="1.5×", style=discord.ButtonStyle.primary, row=1)
    async def _t15(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._launch(i, 1.5)

    @discord.ui.button(label="2×", style=discord.ButtonStyle.primary, row=1)
    async def _t2(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._launch(i, 2.0)

    @discord.ui.button(label="3×", style=discord.ButtonStyle.primary, row=1)
    async def _t3(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._launch(i, 3.0)

    @discord.ui.button(label="5×", style=discord.ButtonStyle.primary, row=1)
    async def _t5(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._launch(i, 5.0)

    @discord.ui.button(label="10×", style=discord.ButtonStyle.primary, row=1)
    async def _t10(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await self._launch(i, 10.0)

    @discord.ui.button(label="Eigenes Ziel", emoji="✏️", style=discord.ButtonStyle.secondary, row=2)
    async def _custom(self, i: discord.Interaction, _b: discord.ui.Button) -> None:
        await i.response.send_modal(_CrashTargetModal(self))


# --- Blackjack: Einsatz waehlen, dann Deal -------------------------------
class _BlackjackSetup(_Setup):
    kind = "blackjack"

    def _embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="🂡 Blackjack",
            description=("**Einsatz** wählen, dann **Deal**. Danach steuerst du mit "
                         "**Karte / Stand / Double** – so nah wie möglich an 21."),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=self._bet_txt(), inline=True)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    @discord.ui.button(label="Deal", emoji="🂡", style=discord.ButtonStyle.success, row=1)
    async def _deal(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if self.is_finished():        # Doppelklick: nur der erste Klick dealt
            await interaction.response.defer()
            return
        ch = self.channel_id or interaction.channel_id
        existing = _bj_views.get((ch, self.uid))
        if existing and not existing.is_finished():
            await interaction.response.send_message(
                "Du hast schon eine Blackjack-Runde offen – nutz die Buttons drunter. 👇",
                ephemeral=True)
            return
        bet = await self._ensure_bet(interaction)
        if bet is None:
            return
        self.stop()   # synchron VOR dem Abzug: schliesst das Doppelklick-Fenster
        economy.add_coins(self.uid, -bet)
        emb, file, view, ended = await _bj_deal(ch, self.uid, bet)
        view.message = interaction.message
        if not ended:
            _bj_views[(ch, self.uid)] = view   # vor dem Edit: Text-Fallback greift
        try:
            await interaction.response.edit_message(embed=emb, attachments=[file], view=view)
        except discord.HTTPException:
            log.exception("Blackjack-Deal: Anzeige fehlgeschlagen")
            return
        _protect(interaction.message)


# --- Gluecksrad: Einsatz waehlen, dann Drehen -----------------------------
class _WheelSetup(_Setup):
    kind = "wheel"

    def _embed(self) -> discord.Embed:
        segs = " · ".join("0" if m <= 0 else f"×{m:g}"
                          for m in sorted(set(_WHEEL_SEGMENTS)))
        emb = discord.Embed(
            title="🍀 Glücksrad",
            description=("**Einsatz** wählen, dann **Drehen** – das Rad entscheidet.\n"
                         f"Felder: {segs}"),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=self._bet_txt(), inline=True)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    @discord.ui.button(label="Drehen", emoji="🍀", style=discord.ButtonStyle.success, row=1)
    async def _spin(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        bet = await self._claim_bet(interaction)
        if bet is None:
            return
        emb, file = await _play_wheel(self.uid, bet)
        again = _AgainView(self.uid, "wheel", {"bet": bet}, channel_id=self.channel_id)
        await self._finish(interaction, emb, file, again)


# --- Rubbellos: Einsatz waehlen, dann Rubbeln ------------------------------
class _ScratchSetup(_Setup):
    kind = "scratch"

    def _embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="🎫 Rubbellos",
            description=("**Einsatz** wählen, dann **Rubbeln**.\n"
                         "Drei gleiche Symbole in einer **Reihe** gewinnen – "
                         "je edler das Symbol, desto fetter der Faktor (bis ×40)."),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=self._bet_txt(), inline=True)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    @discord.ui.button(label="Rubbeln", emoji="🎫", style=discord.ButtonStyle.success, row=1)
    async def _go(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        bet = await self._claim_bet(interaction)
        if bet is None:
            return
        emb, file = await _play_scratch(self.uid, bet)
        again = _AgainView(self.uid, "scratch", {"bet": bet}, channel_id=self.channel_id)
        await self._finish(interaction, emb, file, again)


# --- Mines: Einsatz + Bombenzahl, dann Start -------------------------------
class _MinesCountSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=f"{n} Bomben", value=str(n), emoji="💣",
                                 description=f"1. Feld zahlt ×{_mines_mult(1, n):.2f}",
                                 default=(n == _MINES_DEFAULT))
            for n in range(1, _MINES_MAX + 1)
        ]
        super().__init__(placeholder="💣 Bomben wählen …", min_values=1, max_values=1,
                         options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.mines = int(self.values[0])
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await interaction.response.edit_message(embed=self.view._embed(), view=self.view)


class _MinesSetup(_Setup):
    kind = "mines"

    def __init__(self, uid: int, *, channel_id: int | None = None,
                 bet: int | None = None) -> None:
        super().__init__(uid, channel_id=channel_id, bet=bet)
        self.mines = _MINES_DEFAULT
        self.add_item(_MinesCountSelect())

    def _embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="💣 Mines",
            description=("**Einsatz** und **Bombenzahl** wählen, dann **Start**.\n"
                         "Jedes sichere Feld erhöht den Multiplikator – "
                         "Cashout, bevor es knallt!"),
            color=_C_BJ)
        emb.set_author(name="🎰 Flo Casino")
        emb.add_field(name="Einsatz", value=self._bet_txt(), inline=True)
        emb.add_field(name="Bomben", value=f"{self.mines} 💣", inline=True)
        emb.add_field(name="1. Feld", value=f"×{_mines_mult(1, self.mines):.2f}",
                      inline=True)
        emb.set_footer(text=_bal_footer(self.uid))
        return emb

    @discord.ui.button(label="Start", emoji="💣", style=discord.ButtonStyle.success, row=2)
    async def _start(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        if self.is_finished():        # Doppelklick: nur der erste Klick startet
            await interaction.response.defer()
            return
        ch = self.channel_id or interaction.channel_id
        existing = _mines_views.get((ch, self.uid))
        if existing and not existing.is_finished() and not existing.settled:
            await interaction.response.send_message(
                "Du hast hier schon ein Minenfeld offen. 💣", ephemeral=True)
            return
        bet = await self._ensure_bet(interaction)
        if bet is None:
            return
        self.stop()   # synchron VOR dem Abzug: schliesst das Doppelklick-Fenster
        economy.add_coins(self.uid, -bet)
        await economy.flush()
        view = MinesView(ch, self.uid, bet, self.mines)
        view.message = interaction.message
        _mines_views[(ch, self.uid)] = view
        try:
            await interaction.response.edit_message(embed=view._embed(), view=view)
        except discord.HTTPException:
            log.exception("Mines-Start: Anzeige fehlgeschlagen - Einsatz zurueck")
            view.settled = True
            view.stop()
            _mines_views.pop((ch, self.uid), None)
            economy.add_coins(self.uid, bet)
            await economy.flush()
            return
        _protect(interaction.message)


_SETUPS = {
    "keno": _KenoSetup,
    "roulette": _RouletteSetup,
    "crash": _CrashSetup,
    "blackjack": _BlackjackSetup,
    "wheel": _WheelSetup,
    "scratch": _ScratchSetup,
    "mines": _MinesSetup,
}


async def _open_setup(message: discord.Message, kind: str) -> object:
    """Antwortet auf `Flo <spiel>` mit dem interaktiven Aufbau-Menue."""
    view = _SETUPS[kind](message.author.id, channel_id=message.channel.id)
    msg = await _send(message, embed=view._embed(), view=view)
    if msg:
        view.message = msg
    return HANDLED
