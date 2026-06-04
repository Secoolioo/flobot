"""Casino-Feature fuer Flo (Pack 5): spielen mit Flo Coins.

Spiele (nach 'Flo'):
- casino                      Uebersicht aller Spiele
- blackjack <einsatz>         17-und-4 gegen den Dealer
    -> danach: karte (ziehen) / stand (halten) / double (verdoppeln)
- crash <einsatz> <ziel>      Rakete steigt - cash vor dem Absturz aus (z. B. 2.0)
- keno <einsatz> <1-8 zahlen> tippe Zahlen 1-40, 10 werden gezogen
- roulette <einsatz> <auf>    rot/schwarz, gerade/ungerade, 1-18/19-36 oder Zahl 0-36

Alles laeuft ueber EINEN Coin-Topf: economy.py. Dieses Modul aendert Kontostaende
nur ueber economy.add_coins() und liest sie ueber economy.get_coins(). Ohne ein
aktives economy-Feature bleibt das Casino aus.

Bewusst rein textbasiert (keine Buttons): jeder Zug ist ein kurzer Befehl. So ist
es robust, auch wenn der Bot kurz neu startet (offene Blackjack-Runden leben nur
im Speicher und verfallen nach BJ_TIMEOUT).
"""
from __future__ import annotations

import logging
import os
import random
import time

import discord

import ai
import economy

log = logging.getLogger("dcbot.casino")

_enabled: bool = False
_bot_name: str = "Flo"

MIN_BET = 1
MAX_BET = int(os.getenv("CASINO_MAX_BET", "100000") or "100000")
BJ_TIMEOUT = 180        # Sekunden, bis eine offene Blackjack-Runde verfaellt

# Offene Blackjack-Runden, je (channel_id, user_id). Nur im Speicher.
_bj: dict[tuple[int, int], dict] = {}


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
    log.info("Casino-Feature aktiv (Einsatz %d–%d %s).", MIN_BET, MAX_BET, economy.COIN)
    return True


def is_enabled() -> bool:
    return _enabled


# --- Einsatz-Helfer ------------------------------------------------------
def _resolve_bet(token: str, uid: int) -> int | None:
    """Wandelt ein Einsatz-Token in eine Zahl. 'alles'/'max' = ganzer Kontostand."""
    token = (token or "").lower()
    if token in ("all", "alles", "max", "allin", "all-in"):
        return min(economy.get_coins(uid), MAX_BET)
    if token.isdigit():
        return int(token)
    return None


def _check_bet(uid: int, bet: int | None) -> tuple[int, str | None]:
    """Prueft einen Einsatz. Rueckgabe: (gepruefter Einsatz, Fehlertext oder None)."""
    if bet is None:
        return 0, f"Wie viel setzt du? z. B. `50` oder `alles`."
    if bet < MIN_BET:
        return 0, f"Mindesteinsatz ist {MIN_BET} {economy.COIN}."
    if bet > MAX_BET:
        return 0, f"Maximaleinsatz ist {MAX_BET} {economy.COIN}."
    bal = economy.get_coins(uid)
    if bet > bal:
        return 0, f"Dafuer reicht's nicht – du hast {bal} {economy.COIN}."
    return bet, None


def _bal_footer(uid: int) -> str:
    return f"Kontostand: {economy.get_coins(uid)} {economy.COIN}"


def _outcome(bet: int, payout: int) -> tuple[discord.Color, str, str]:
    """Farbe + Ergebnis-Feld aus Einsatz und Auszahlung (Auszahlung inkl. Einsatz)."""
    net = payout - bet
    if net > 0:
        return discord.Color.green(), "Gewinn", f"+{net} {economy.COIN}"
    if net == 0:
        return discord.Color.greyple(), "Ergebnis", f"±0 – Einsatz zurueck"
    return discord.Color.red(), "Verlust", f"-{bet} {economy.COIN}"


# --- Spielkarten (Blackjack) ---------------------------------------------
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


def _fmt_hand(hand: list[tuple[str, str]]) -> str:
    return "  ".join(f"`{r}{s}`" for r, s in hand)


# --- Befehls-Einstieg ----------------------------------------------------
async def handle(message: discord.Message) -> "str | discord.Embed | None":
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.split()
    first = parts[0].lower()
    args = parts[1:]

    if first in ("casino", "spielbank", "kasino", "glücksspiel", "gluecksspiel", "gambling"):
        return _menu(message.author.id)
    if first in ("blackjack", "bj", "17und4", "siebzehnundvier"):
        return await _bj_start(message, args)
    if first in ("hit", "karte", "ziehen", "zieh"):
        return await _bj_action(message, "hit")
    if first in ("stand", "stehen", "bleiben", "bleib", "pass", "genug", "fertig"):
        return await _bj_action(message, "stand")
    if first in ("double", "doppeln", "verdoppeln", "dd"):
        return await _bj_action(message, "double")
    if first in ("crash", "absturz", "rakete", "rocket"):
        return await _crash(message, args)
    if first == "keno":
        return await _keno(message, args)
    if first in ("roulette", "roul", "kessel"):
        return await _roulette(message, args)
    return None


def _menu(uid: int) -> discord.Embed:
    c = economy.COIN
    n = _bot_name
    emb = discord.Embed(
        title="🎰 Flo Casino",
        description=(f"Setze deine **{c}** und versuch dein Glück.\n"
                     f"Einsatz ist eine Zahl oder `alles`."),
        color=discord.Color.gold(),
    )
    emb.add_field(name="🂡 Blackjack",
                  value=f"`{n} blackjack 50`\ndann `karte` · `stand` · `double`", inline=True)
    emb.add_field(name="🚀 Crash",
                  value=f"`{n} crash 50 2.0`\nsteig vor dem Absturz aus", inline=True)
    emb.add_field(name="🎱 Keno",
                  value=f"`{n} keno 50 3 7 12`\ntippe 1–8 Zahlen (1–40)", inline=True)
    emb.add_field(name="🎡 Roulette",
                  value=f"`{n} roulette 50 rot`\nFarbe · gerade · Zahl 0–36", inline=True)
    emb.add_field(name="🎰 Slots",
                  value=f"`{n} slot 20`\ndrei Gleiche gewinnen", inline=True)
    emb.add_field(name="🪙 Coinflip",
                  value=f"`{n} coinflip 50 kopf`\nKopf oder Zahl", inline=True)
    emb.set_footer(text=_bal_footer(uid))
    return emb


# --- Blackjack -----------------------------------------------------------
def _bj_prompt(game: dict, uid: int) -> str:
    hint = f"`{_bot_name} karte` (ziehen) · `{_bot_name} stand` (halten)"
    if len(game["player"]) == 2 and economy.get_coins(uid) >= game["bet"]:
        hint += f" · `{_bot_name} double` (verdoppeln)"
    return hint


def _bj_embed(game: dict, uid: int, *, reveal: bool, title: str,
              color: discord.Color, note: str) -> discord.Embed:
    player = game["player"]
    dealer = game["dealer"]
    pv = _hand_value(player)
    emb = discord.Embed(title=title, color=color)
    emb.set_author(name="🎰 Flo Casino")
    if reveal:
        emb.add_field(name=f"🤖 Dealer ({_hand_value(dealer)})",
                      value=_fmt_hand(dealer), inline=False)
    else:
        emb.add_field(name="🤖 Dealer",
                      value=f"{_fmt_hand(dealer[:1])}  `🂠`", inline=False)
    emb.add_field(name=f"🧑 Du ({pv})", value=_fmt_hand(player), inline=False)
    if note:
        emb.add_field(name="​", value=note, inline=False)
    emb.set_footer(text=f"{_bal_footer(uid)}  ·  Einsatz: {game['bet']} {economy.COIN}")
    return emb


async def _bj_start(message: discord.Message, args: list[str]) -> "str | discord.Embed":
    uid = message.author.id
    key = (message.channel.id, uid)
    game = _bj.get(key)
    if game and (time.monotonic() - game["ts"] < BJ_TIMEOUT):
        return (f"Du hast schon eine Blackjack-Runde offen – "
                f"`{_bot_name} karte` oder `{_bot_name} stand`.")

    bet = _resolve_bet(args[0], uid) if args else None
    bet, err = _check_bet(uid, bet)
    if err:
        return err

    economy.add_coins(uid, -bet)   # Einsatz sofort einziehen
    deck = _new_deck()
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    game = {"bet": bet, "deck": deck, "player": player, "dealer": dealer,
            "ts": time.monotonic(), "doubled": False}

    pv, dv = _hand_value(player), _hand_value(dealer)
    if pv == 21 or dv == 21:        # Natural -> sofort entscheiden
        if pv == 21 and dv == 21:
            economy.add_coins(uid, bet)
            await economy.flush()
            return _bj_embed(game, uid, reveal=True, title="🂡 Blackjack – Push",
                             color=discord.Color.greyple(),
                             note=f"Beide haben 21 – Einsatz ({bet} {economy.COIN}) zurueck.")
        if pv == 21:
            payout = bet + (bet * 3) // 2     # 3:2
            economy.add_coins(uid, payout)
            await economy.flush()
            return _bj_embed(game, uid, reveal=True, title="🂡 BLACKJACK! 🎉",
                             color=discord.Color.green(),
                             note=f"Natürlicher Blackjack! +{payout - bet} {economy.COIN} (3:2).")
        await economy.flush()
        return _bj_embed(game, uid, reveal=True, title="🂡 Dealer-Blackjack 😬",
                         color=discord.Color.red(),
                         note=f"Der Dealer hat Blackjack. -{bet} {economy.COIN}.")

    _bj[key] = game
    await economy.flush()
    return _bj_embed(game, uid, reveal=False, title="🂡 Blackjack",
                     color=discord.Color.blurple(), note=_bj_prompt(game, uid))


async def _bj_action(message: discord.Message, kind: str) -> "str | discord.Embed | None":
    uid = message.author.id
    key = (message.channel.id, uid)
    game = _bj.get(key)
    if not game:
        return None     # keine offene Runde -> nicht kapern, andere duerfen ran
    if time.monotonic() - game["ts"] >= BJ_TIMEOUT:
        _bj.pop(key, None)
        return None
    game["ts"] = time.monotonic()

    if kind == "double":
        if len(game["player"]) != 2:
            return "Verdoppeln geht nur als allererste Aktion."
        extra = game["bet"]
        if economy.get_coins(uid) < extra:
            return f"Zum Verdoppeln brauchst du nochmal {extra} {economy.COIN}."
        economy.add_coins(uid, -extra)
        game["bet"] += extra
        game["doubled"] = True
        game["player"].append(game["deck"].pop())   # genau eine Karte
        return await _bj_finish(message, key, game)

    if kind == "hit":
        game["player"].append(game["deck"].pop())
        if _hand_value(game["player"]) > 21:
            _bj.pop(key, None)
            await economy.flush()
            return _bj_embed(game, uid, reveal=True, title="🂡 Bust! 💥",
                             color=discord.Color.red(),
                             note=f"Über 21 – verloren. -{game['bet']} {economy.COIN}.")
        return _bj_embed(game, uid, reveal=False, title="🂡 Blackjack",
                         color=discord.Color.blurple(), note=_bj_prompt(game, uid))

    return await _bj_finish(message, key, game)   # stand


async def _bj_finish(message: discord.Message, key: tuple[int, int],
                     game: dict) -> "str | discord.Embed":
    uid = message.author.id
    player, dealer, deck = game["player"], game["dealer"], game["deck"]
    bet = game["bet"]
    pv = _hand_value(player)

    if pv > 21:     # nur nach Double moeglich
        _bj.pop(key, None)
        await economy.flush()
        return _bj_embed(game, uid, reveal=True, title="🂡 Bust! 💥",
                         color=discord.Color.red(),
                         note=f"Über 21 – verloren. -{bet} {economy.COIN}.")

    while _hand_value(dealer) < 17:     # Dealer zieht bis 17
        dealer.append(deck.pop())
    dv = _hand_value(dealer)
    _bj.pop(key, None)

    if dv > 21 or pv > dv:
        payout = bet * 2
        economy.add_coins(uid, payout)
        await economy.flush()
        grund = "Dealer überkauft sich!" if dv > 21 else f"Deine {pv} schlägt {dv}."
        return _bj_embed(game, uid, reveal=True, title="🂡 Gewonnen! 🎉",
                         color=discord.Color.green(),
                         note=f"{grund} +{payout - bet} {economy.COIN}.")
    if pv < dv:
        await economy.flush()
        return _bj_embed(game, uid, reveal=True, title="🂡 Verloren 😬",
                         color=discord.Color.red(),
                         note=f"Dealer {dv} schlägt deine {pv}. -{bet} {economy.COIN}.")
    economy.add_coins(uid, bet)
    await economy.flush()
    return _bj_embed(game, uid, reveal=True, title="🂡 Push",
                     color=discord.Color.greyple(),
                     note=f"Beide {pv} – Einsatz ({bet} {economy.COIN}) zurueck.")


# --- Crash ---------------------------------------------------------------
def _parse_mult(token: str) -> float | None:
    token = (token or "").lower().rstrip("x").replace(",", ".")
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


async def _crash(message: discord.Message, args: list[str]) -> "str | discord.Embed":
    uid = message.author.id
    bet = _resolve_bet(args[0], uid) if args else None
    bet, err = _check_bet(uid, bet)
    if err:
        return err
    target = _parse_mult(args[1]) if len(args) > 1 else None
    if target is None:
        return f"Bei welchem Faktor steigst du aus? z. B. `{_bot_name} crash {bet} 2.0`"
    if target < 1.01:
        return "Das Ziel muss über 1.0 liegen (z. B. 1.5, 2.0, 5.0)."
    target = min(target, 100.0)

    economy.add_coins(uid, -bet)
    cp = _crash_point()
    payout = int(bet * target) if cp >= target else 0
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()

    color, fname, fval = _outcome(bet, payout)
    if payout:
        desc = (f"🚀 Die Rakete fliegt bis **{cp:.2f}×** – du bist bei "
                f"**{target:.2f}×** ausgestiegen! 🎉")
    else:
        desc = (f"💥 Bei **{cp:.2f}×** zerschellt – dein Ziel war **{target:.2f}×**. 😬")
    emb = discord.Embed(title="🚀 Crash", description=desc, color=color)
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name=fname, value=fval, inline=True)
    emb.set_footer(text=_bal_footer(uid))
    return emb


# --- Keno ----------------------------------------------------------------
# Auszahlungs-Faktor je (getippte Zahlen, Treffer) - bezogen auf den Einsatz.
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


async def _keno(message: discord.Message, args: list[str]) -> "str | discord.Embed":
    uid = message.author.id
    if not args:
        return (f"So geht's: `{_bot_name} keno <einsatz> <1-8 Zahlen 1-40>` "
                f"– z. B. `{_bot_name} keno 50 3 7 12 21`")
    bet = _resolve_bet(args[0], uid)
    bet, err = _check_bet(uid, bet)
    if err:
        return err

    picks: list[int] = []
    for t in args[1:]:
        if t.isdigit():
            n = int(t)
            if 1 <= n <= 40 and n not in picks:
                picks.append(n)
    if not picks:
        return f"Tippe 1–8 Zahlen von 1 bis 40. z. B. `{_bot_name} keno {bet} 3 7 12 21`"
    picks = picks[:8]

    economy.add_coins(uid, -bet)
    draw = random.sample(range(1, 41), 10)
    hits = sorted(set(picks) & set(draw))
    mult = _KENO_TABLE.get((len(picks), len(hits)), 0)
    payout = int(bet * mult)
    if payout:
        economy.add_coins(uid, payout)
    await economy.flush()

    color, fname, fval = _outcome(bet, payout)
    drawn_str = " ".join(f"**__{n}__**" if n in hits else f"{n}" for n in sorted(draw))
    picks_str = " ".join(f"**{n}**" if n in hits else f"~~{n}~~" for n in sorted(picks))
    emb = discord.Embed(title="🎱 Keno", description=f"Gezogen: {drawn_str}", color=color)
    emb.set_author(name="🎰 Flo Casino")
    emb.add_field(name="Deine Zahlen", value=picks_str, inline=False)
    emb.add_field(name="Treffer", value=f"{len(hits)}/{len(picks)}  →  ×{mult}", inline=True)
    emb.add_field(name=fname, value=fval, inline=True)
    emb.set_footer(text=_bal_footer(uid))
    return emb


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


async def _roulette(message: discord.Message, args: list[str]) -> "str | discord.Embed":
    uid = message.author.id
    if len(args) < 2:
        return (f"So geht's: `{_bot_name} roulette <einsatz> <auf>` – auf: rot/schwarz, "
                f"gerade/ungerade, 1-18/19-36 oder eine Zahl 0-36.")
    bet = _resolve_bet(args[0], uid)
    bet, err = _check_bet(uid, bet)
    if err:
        return err
    target = " ".join(args[1:]).lower().strip()

    spin = random.randint(0, 36)
    payout, label = _roulette_payout(target, bet, spin)
    if payout is None:
        return (f"Worauf? rot/schwarz, gerade/ungerade, 1-18/19-36 oder eine Zahl 0-36. "
                f"z. B. `{_bot_name} roulette {bet} rot`")

    economy.add_coins(uid, -bet)
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
    return emb
