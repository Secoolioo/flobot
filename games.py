"""Mini-Games & Events (Pack 2).

Befehle (nach 'Flo'):  quiz, zahlenraten, ssp <schere|stein|papier>,
                       coinflip [einsatz] [kopf|zahl], slot [einsatz],
                       wuerfel [NdM]
Passiv:  Counting-Channel (optional via COUNTING_CHANNEL_ID), Antworten auf
         laufende Quiz-/Zahlenraten-Runden, und zufaellige 'Schnell-tippen'-Events
         (bot.py ruft dafuer maybe_event() periodisch auf).

Gewinne werden als Flo Coins ueber economy.add_coins() ausgezahlt (ein Topf).
Quiz nutzt die KI (ai.generate); faellt sie aus, greift ein kleiner fester
Fragenkatalog. Alles andere laeuft auch ganz ohne KI.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import random
import re
import time

import discord

import ai
import casino
import economy
import render
from store import JsonStore

log = logging.getLogger("dcbot.games")

# Sentinel: games hat selbst geantwortet (Bild/Embed) -> bot.py schweigt.
HANDLED = object()

_enabled: bool = False
_bot_name: str = "Flo"
_store: JsonStore | None = None

# Optionaler Counting-Channel (Zahlen hochzaehlen). Leer = aus.
COUNTING_CHANNEL_ID = int(os.getenv("COUNTING_CHANNEL_ID", "0") or "0")

# Zufalls-Events: bot.py ruft maybe_event() im Takt; das ist die Chance pro Aufruf.
EVENT_CHANCE = float(os.getenv("GAMES_EVENT_CHANCE", "0.15"))
# Default: der Commands-Channel (wird eh automatisch aufgeraeumt, da gehoeren die
# kurzlebigen Tipp-Events hin). Per ENV ueberschreibbar.
EVENT_CHANNEL_ID = int(os.getenv("GAMES_EVENT_CHANNEL_ID", "1512045750362837013") or "0")

QUIZ_REWARD = 50
QUIZ_TIMEOUT = 30           # Sekunden bis zur Aufloesung
GUESS_TIMEOUT = 90
EVENT_REWARD = 100
EVENT_TIMEOUT = 30

# Laufende Runden je Channel (nur im Speicher).
_quiz: dict[int, dict] = {}
_guess: dict[int, dict] = {}
_event: dict[int, dict] = {}
_round_token: dict[int, int] = {}

_bg: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)


# --- Auto-Loesch-Schutz + Bild-/Text-Versand ----------------------------
def _protect(msg) -> None:
    """Meldet eine laufende Spiel-Nachricht (Quiz/Zahlenraten) beim Auto-Loesch-
    Schutz an, damit sie im #commands-Channel nicht mitten in der Runde
    verschwindet. Lazy-Import von bot wegen Zirkel-Import."""
    if msg is None:
        return
    try:
        import bot
        bot.protect_message(msg)
    except Exception:
        pass


def _release(msg) -> None:
    """Gibt eine geschuetzte Spiel-Nachricht wieder frei (Runde vorbei / keine
    Reaktion mehr) -> der Bot raeumt sie nach kurzer Gnadenfrist weg."""
    if msg is None:
        return
    try:
        import bot
        bot.release_message(msg)
    except Exception:
        pass


async def _say(message: discord.Message, text: str):
    """Schickt eine Text-Antwort als Reply und gibt die Nachricht zurueck."""
    try:
        return await message.reply(text, mention_author=False)
    except discord.HTTPException:
        log.exception("Spiel-Nachricht konnte nicht gesendet werden")
        return None


async def _send_image(message: discord.Message, emb: discord.Embed,
                      buf, fname: str) -> object:
    """Schickt ein Spiel-Bild als Reply (Embed mit Anhang). Gibt HANDLED zurueck,
    damit bot.py nicht zusaetzlich antwortet."""
    emb.set_image(url=f"attachment://{fname}")
    try:
        await message.reply(embed=emb, file=discord.File(buf, filename=fname),
                            mention_author=False)
    except discord.HTTPException:
        log.exception("Spiel-Bild konnte nicht gesendet werden")
    return HANDLED


def setup() -> bool:
    global _enabled, _bot_name, _store, _event_words
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("GAMES_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("Spiele-Feature aus (GAMES_ENABLED=0).")
        return False
    _store = JsonStore("games.json", default={"counting": {}})
    _event_words = _load_event_words()
    _enabled = True
    log.info(
        "Spiele-Feature aktiv (Counting: %s, Events: %.0f%%, %d Event-Woerter).",
        "an" if COUNTING_CHANNEL_ID else "aus", EVENT_CHANCE * 100, len(_event_words),
    )
    return True


def is_enabled() -> bool:
    return _enabled


def _clean_lead(text: str) -> str:
    # Zentral in ai.strip_lead: entfernt @-Mentions + fuehrenden Namen/Alias
    # ('Florian quiz' -> 'quiz').
    return ai.strip_lead(text)


def _new_token(channel_id: int) -> int:
    tok = _round_token.get(channel_id, 0) + 1
    _round_token[channel_id] = tok
    return tok


# --- Befehle -------------------------------------------------------------
async def handle(message: discord.Message) -> "str | object | None":
    if not _enabled or message.guild is None:
        return None
    cmd = _clean_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.split()
    first = parts[0].lower()
    args = parts[1:]

    if first in ("quiz", "trivia", "quizzz"):
        return await _start_quiz(message)
    if first in ("zahlenraten", "raten", "errate"):
        return await _start_guess(message)
    if first in ("ssp", "schnickschnack", "rps", "sss"):
        return await _ssp(message, args)
    if first in ("coinflip", "münzwurf", "muenzwurf", "flip", "münze", "muenze"):
        return await (_open_game(message, "coinflip") if not args
                      else _coinflip(message, args))
    if first in ("slot", "slots", "spielautomat", "automat"):
        return await (_open_game(message, "slot") if not args else _slot(message, args))
    if first in ("würfel", "wuerfel", "würfeln", "wuerfeln", "dice", "roll", "w6"):
        return await _dice(message, args)
    return None


# --- Schere-Stein-Papier -------------------------------------------------
_SSP = {
    "schere": "✂️", "stein": "🪨", "papier": "📄",
    "scissors": "✂️", "rock": "🪨", "paper": "📄",
    "✂️": "✂️", "🪨": "🪨", "📄": "📄", "✂": "✂️",
}
_SSP_NORM = {"scissors": "schere", "rock": "stein", "paper": "papier",
             "✂️": "schere", "✂": "schere", "🪨": "stein", "📄": "papier"}
_SSP_BEATS = {"schere": "papier", "stein": "schere", "papier": "stein"}


async def _ssp(message: discord.Message, args: list[str]) -> str:
    if not args:
        return f"Womit? `{_bot_name} ssp schere` (oder stein/papier)."
    raw = args[0].lower()
    user = _SSP_NORM.get(raw, raw)
    if user not in _SSP_BEATS:
        return "Nimm schere, stein oder papier."
    bot = random.choice(list(_SSP_BEATS))
    ub, bb = _SSP[user], _SSP[bot]
    if user == bot:
        return f"{ub} vs {bb} — **Unentschieden!**"
    if _SSP_BEATS[user] == bot:
        if economy.is_enabled():
            economy.add_coins(message.author.id, 10)
            await economy.flush()
        return f"{ub} vs {bb} — **Du gewinnst!** 🎉 (+10 Flo Coins)"
    return f"{ub} vs {bb} — **Ich gewinne!** 😎"


# --- Render-Helfer: Animation in Thread, Standbild als Fallback ------------
async def _anim(anim_fn, static_fn, *args, **kwargs) -> tuple:
    """Rendert ein Spielergebnis als GIF in einem Thread (Event-Loop bleibt
    frei); faellt die Animation aus, kommt das Standbild. (BytesIO, endung)."""
    try:
        return await asyncio.to_thread(anim_fn, *args, **kwargs), "gif"
    except Exception:
        log.exception("Animation fehlgeschlagen - nutze Standbild")
        return await asyncio.to_thread(static_fn, *args, **kwargs), "png"


# --- Coinflip ------------------------------------------------------------
async def _flip_result(uid: int, bet: int, tip: "str | None"):
    """Wirft die Muenze, verrechnet (bei Einsatz + Tipp) Coins und baut Embed +
    animiertes Bild. tip: 'kopf'/'zahl' oder None (freier Wurf). Gibt
    (embed, buffer, name) zurueck – Text-Befehl UND Button-Menue nutzen das."""
    ergebnis = random.choice(["kopf", "zahl"])
    note, color = "", discord.Color.blurple()
    spielt_um_coins = bet > 0 and economy.is_enabled() and tip in ("kopf", "zahl")
    if spielt_um_coins:
        if tip == ergebnis:
            economy.add_coins(uid, bet)
            await economy.flush()
            await casino.record(uid, "coinflip", bet, bet * 2)
            note, color = f"Gewonnen! **+{bet}** Flo Coins 🎉", discord.Color.green()
        else:
            economy.add_coins(uid, -bet)
            await economy.flush()
            await casino.record(uid, "coinflip", bet, 0)
            note, color = f"Verloren! **-{bet}** Flo Coins 😬", discord.Color.red()
    emb = discord.Embed(
        title="🪙 Münzwurf",
        description=f"Die Münze zeigt: **{ergebnis.upper()}**!" + (f"\n{note}" if note else ""),
        color=color)
    if spielt_um_coins:
        emb.set_footer(text=f"Konto: {economy.get_coins(uid)} Flo Coins")
    buf, ext = await _anim(render.coin_flip_anim, render.coin_flip, ergebnis)
    fn = f"coin_{uid}_{random.randint(1000, 9999)}.{ext}"
    return emb, buf, fn


async def _coinflip(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    bet = _extract_int(args) or 0
    seite = next((a.lower() for a in args
                  if a.lower() in ("kopf", "zahl", "heads", "tails")), None)
    if bet and economy.is_enabled():
        if not seite:
            return f"Auf was setzt du? `{_bot_name} coinflip {bet} kopf` (oder zahl)."
        if economy.get_coins(uid) < bet:
            return f"Du hast nicht genug. Konto: {economy.get_coins(uid)} Flo Coins."
    tip = ("kopf" if seite in ("kopf", "heads") else "zahl") if seite else None
    emb, buf, fn = await _flip_result(uid, bet, tip)
    return await _send_image(message, emb, buf, fn)


# --- Slot-Machine --------------------------------------------------------
# Symbol-Schluessel kommen aus render.SLOT_KEYS (werden dort gezeichnet).
_SLOT_PAYOUT = {  # drei Gleiche -> Faktor auf den Einsatz (fallend wie SLOT_KEYS)
    "seven": 25, "diamond": 15, "star": 10, "bar": 7,
    "grape": 5, "lemon": 4, "cherry": 3,
}


async def _spin_slot(uid: int, bet: int):
    """Dreht die Walzen, verrechnet (bei Einsatz) Coins und baut Embed + Bild.
    Gibt (embed, buffer, name) zurueck – wird vom Text-Befehl UND vom
    Button-Menue genutzt. Das Bild ist noch OHNE set_image (macht der Aufrufer)."""
    use_coins = bet > 0 and economy.is_enabled()
    keys = [random.choice(render.SLOT_KEYS) for _ in range(3)]
    jackpot = keys[0] == keys[1] == keys[2]
    zwei = (not jackpot) and (keys[0] == keys[1] or keys[1] == keys[2] or keys[0] == keys[2])
    basis = bet if bet > 0 else 10
    if jackpot:
        win = basis * _SLOT_PAYOUT[keys[0]]
    elif zwei:
        win = basis * 2
    else:
        win = 0

    # Coins verbuchen: mit Einsatz wird netto verrechnet; ohne Einsatz gibt es den
    # Gewinn (falls economy an) geschenkt - wie bisher.
    if use_coins:
        economy.add_coins(uid, win - bet)
        await economy.flush()
        await casino.record(uid, "slots", bet, win)
    elif win and economy.is_enabled():
        economy.add_coins(uid, win)
        await economy.flush()

    if jackpot:
        desc, color = "🎉 **JACKPOT!** Drei Gleiche!", discord.Color.gold()
    elif zwei:
        desc, color = "Zwei Gleiche — kleiner Gewinn!", discord.Color.green()
    else:
        desc, color = "Leider nichts. Versuch's nochmal!", discord.Color.greyple()
    emb = discord.Embed(title="🎰 Slot-Machine", description=desc, color=color)
    if use_coins:
        net = win - bet
        emb.set_footer(text=f"{'+' if net >= 0 else ''}{net} Flo Coins  ·  "
                            f"Konto: {economy.get_coins(uid)}")
    elif win and economy.is_enabled():
        emb.set_footer(text=f"+{win} Flo Coins  ·  Konto: {economy.get_coins(uid)}")
    buf, ext = await _anim(render.slot_machine_anim, render.slot_machine,
                           keys, win=win, jackpot=jackpot)
    fn = f"slot_{uid}_{random.randint(1000, 9999)}.{ext}"
    return emb, buf, fn


async def _slot(message: discord.Message, args: list[str]) -> object:
    uid = message.author.id
    bet = _extract_int(args) or 0
    if bet > 0 and economy.is_enabled() and economy.get_coins(uid) < bet:
        return f"Du hast nicht genug. Konto: {economy.get_coins(uid)} Flo Coins."
    emb, buf, fn = await _spin_slot(uid, bet)
    return await _send_image(message, emb, buf, fn)


# --- Wuerfel -------------------------------------------------------------
async def _dice(message: discord.Message, args: list[str]) -> object:
    count, sides = 1, 6
    if args:
        m = re.fullmatch(r"(\d*)d(\d+)", args[0].lower())
        if m:
            count = int(m.group(1) or "1")
            sides = int(m.group(2))
        elif args[0].isdigit():
            sides = int(args[0])
    count = max(1, min(count, 20))
    sides = max(2, min(sides, 1000))
    rolls = [random.randint(1, sides) for _ in range(count)]
    if count == 1:
        desc = f"Du würfelst eine **{rolls[0]}**  (W{sides})."
    else:
        desc = f"{count}×W{sides}:  {' + '.join(map(str, rolls))}  =  **{sum(rolls)}**"
    emb = discord.Embed(title="🎲 Würfel", description=desc, color=discord.Color.blurple())
    fn = f"dice_{message.author.id}_{random.randint(1000, 9999)}.png"
    buf = await asyncio.to_thread(render.dice_roll, rolls, sides)
    return await _send_image(message, emb, buf, fn)


def _extract_int(args: list[str]) -> int | None:
    for a in args:
        if a.isdigit():
            return int(a)
    return None


# --- Interaktive Spiel-Menues (Buttons/Dropdown) -------------------------
# Statt 'flo slot 100' tippen zu muessen: 'flo slot' oeffnet ein Menue mit
# Einsatz-Dropdown + Spiel-Buttons. Ein und dieselbe View bleibt stehen, man
# kann immer wieder klicken (Nachricht wird in-place aktualisiert).
_BET_CHOICES = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


class _GameBetSelect(discord.ui.Select):
    """Dropdown zum Einsatz waehlen (inkl. 'ohne Einsatz')."""

    def __init__(self) -> None:
        options = [discord.SelectOption(label="Ohne Einsatz (nur Spaß)", value="0",
                                        emoji="🎈", default=True)]
        options += [discord.SelectOption(label=f"{b} Flo Coins", value=str(b))
                    for b in _BET_CHOICES]
        super().__init__(placeholder="Einsatz wählen…", min_values=1, max_values=1,
                         options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view._set_bet(interaction, self.values[0])


class _GameView(discord.ui.View):
    """Basis fuer Slot/Coinflip-Menues: Einsatz-Dropdown, Besitzer-Check,
    In-place-Update nach jedem Spiel, Freigabe beim Timeout."""

    def __init__(self, uid: int, *, channel_id: int | None = None) -> None:
        super().__init__(timeout=180)
        self.uid = uid
        self.channel_id = channel_id
        self.bet = 0
        self.message = None
        self._bet_select = _GameBetSelect()
        self.add_item(self._bet_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uid:
            await interaction.response.send_message(
                "Das ist nicht dein Spiel — schreib selbst z. B. `Flo slot` 🙂",
                ephemeral=True)
            return False
        return True

    async def _set_bet(self, interaction: discord.Interaction, raw: str) -> None:
        self.bet = int(raw)
        for opt in self._bet_select.options:
            opt.default = (opt.value == raw)
        await interaction.response.edit_message(embed=self._embed(), view=self)

    def _has_funds(self) -> bool:
        return not (self.bet > 0 and economy.is_enabled()
                    and economy.get_coins(self.uid) < self.bet)

    def _bet_txt(self) -> str:
        return f"**{self.bet}** Flo Coins" if self.bet > 0 else "ohne Einsatz (nur Spaß)"

    async def _show(self, interaction: discord.Interaction, emb: discord.Embed,
                    buf, fn: str) -> None:
        """Aktualisiert die Menue-Nachricht mit Ergebnis-Embed + neuem Bild und
        laesst die View stehen (man kann gleich nochmal klicken)."""
        emb.set_image(url=f"attachment://{fn}")
        await interaction.response.edit_message(
            embed=emb, attachments=[discord.File(buf, filename=fn)], view=self)
        self.message = interaction.message or self.message
        _protect(self.message)

    async def _warn_funds(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"Du hast nicht genug. Konto: {economy.get_coins(self.uid)} Flo Coins.",
            ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            _release(self.message)


class _SlotView(_GameView):
    def _embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="🎰 Slot-Machine",
            description=("Wähle deinen Einsatz und drück **Drehen**!\n"
                         f"Aktueller Einsatz: {self._bet_txt()}"),
            color=discord.Color.gold())
        if economy.is_enabled():
            emb.set_footer(text=f"Konto: {economy.get_coins(self.uid)} Flo Coins")
        return emb

    @discord.ui.button(label="Drehen", emoji="🎰",
                       style=discord.ButtonStyle.success, row=1)
    async def _spin(self, interaction: discord.Interaction,
                    button: discord.ui.Button) -> None:
        if not self._has_funds():
            await self._warn_funds(interaction)
            return
        emb, buf, fn = await _spin_slot(self.uid, self.bet)
        await self._show(interaction, emb, buf, fn)


class _CoinView(_GameView):
    def _embed(self) -> discord.Embed:
        emb = discord.Embed(
            title="🪙 Münzwurf",
            description=("Wähle deinen Einsatz, dann tippe **Kopf** oder **Zahl**.\n"
                         f"Aktueller Einsatz: {self._bet_txt()}"),
            color=discord.Color.blurple())
        if self.bet > 0 and economy.is_enabled():
            emb.set_footer(text="Bei Einsatz ist dein Klick (Kopf/Zahl) die Wette.")
        elif economy.is_enabled():
            emb.set_footer(text=f"Konto: {economy.get_coins(self.uid)} Flo Coins")
        return emb

    async def _toss(self, interaction: discord.Interaction, tip: str) -> None:
        if not self._has_funds():
            await self._warn_funds(interaction)
            return
        emb, buf, fn = await _flip_result(self.uid, self.bet, tip)
        await self._show(interaction, emb, buf, fn)

    @discord.ui.button(label="Kopf", emoji="🙂",
                       style=discord.ButtonStyle.primary, row=1)
    async def _kopf(self, interaction: discord.Interaction,
                    button: discord.ui.Button) -> None:
        await self._toss(interaction, "kopf")

    @discord.ui.button(label="Zahl", emoji="🔢",
                       style=discord.ButtonStyle.primary, row=1)
    async def _zahl(self, interaction: discord.Interaction,
                    button: discord.ui.Button) -> None:
        await self._toss(interaction, "zahl")


_GAME_VIEWS = {"slot": _SlotView, "coinflip": _CoinView}


async def _open_game(message: discord.Message, kind: str) -> object:
    """Oeffnet das interaktive Menue (Einsatz-Dropdown + Buttons) fuer 'kind'."""
    view = _GAME_VIEWS[kind](message.author.id, channel_id=message.channel.id)
    try:
        msg = await message.reply(embed=view._embed(), view=view, mention_author=False)
    except discord.HTTPException:
        log.exception("Spiel-Menue konnte nicht gesendet werden")
        return HANDLED
    view.message = msg
    _protect(msg)
    return HANDLED


# --- Quiz ----------------------------------------------------------------
_QUIZ_BANK = [
    ("Welcher Planet ist der größte in unserem Sonnensystem?", "Jupiter"),
    ("Wie viele Beine hat eine Spinne?", "8"),
    ("In welchem Land steht der Eiffelturm?", "Frankreich"),
    ("Welches Element hat das chemische Symbol 'O'?", "Sauerstoff"),
    ("Wie heißt die Hauptstadt von Japan?", "Tokio"),
    ("Welche Farbe entsteht, wenn man Blau und Gelb mischt?", "Grün"),
    ("Wie viele Kontinente gibt es?", "7"),
    ("Welches Tier wird als 'König der Tiere' bezeichnet?", "Löwe"),
    ("In welchem Spiel sammelt man Vault-Hunter und Schätze auf Pandora?", "Borderlands"),
    ("Wie heißt der grüne Klempner aus Nintendo-Spielen?", "Luigi"),
]


async def _start_quiz(message: discord.Message) -> object:
    cid = message.channel.id
    if cid in _quiz and _quiz[cid]["expires"] > time.monotonic():
        return "Hier läuft schon ein Quiz - erst antworten! 🤓"

    frage = antwort = ""
    if ai.is_enabled():
        kat = random.choice(["Allgemeinwissen", "Gaming", "Musik", "Geschichte",
                             "Wissenschaft", "Geografie", "Internet/Memes", "Film & TV"])
        system = (
            "Erstelle EINE Quizfrage mit kurzer, eindeutiger Antwort auf Deutsch. "
            "Antworte NUR als JSON: {\"frage\": \"...\", \"antwort\": \"...\"}. "
            "Die Antwort soll ein einzelnes Wort oder ein kurzer Begriff sein."
        )
        raw = await ai.generate(f"Kategorie: {kat}.", system=system,
                                temperature=0.9, max_tokens=150)
        frage, antwort = _parse_quiz_json(raw)
    if not (frage and antwort):
        frage, antwort = random.choice(_QUIZ_BANK)

    tok = _new_token(cid)
    emb = discord.Embed(title="🧠 Quiz", description=f"**{frage}**",
                        color=discord.Color.blurple())
    emb.set_footer(text=f"{QUIZ_TIMEOUT}s Zeit · Antwort einfach in den Chat · "
                        f"+{QUIZ_REWARD} Flo Coins")
    try:
        msg = await message.reply(embed=emb, mention_author=False)
    except discord.HTTPException:
        log.exception("Quiz-Frage konnte nicht gesendet werden")
        msg = None
    _quiz[cid] = {"answer": antwort, "frage": frage,
                  "expires": time.monotonic() + QUIZ_TIMEOUT, "token": tok, "msg": msg}
    _protect(msg)   # laeuft -> nicht vom Auto-Loeschen wegraeumen lassen
    _spawn(_quiz_timeout(message.channel, tok))
    return HANDLED


def _parse_quiz_json(raw: str | None) -> tuple[str, str]:
    if not raw:
        return "", ""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return "", ""
    try:
        data = json.loads(m.group(0))
        return str(data.get("frage", "")).strip(), str(data.get("antwort", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        return "", ""


def _norm(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\wäöüß ]", "", text)
    text = re.sub(r"\b(der|die|das|ein|eine|the|a|an)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def _quiz_timeout(channel, token: int) -> None:
    await asyncio.sleep(QUIZ_TIMEOUT)
    cid = channel.id
    runde = _quiz.get(cid)
    if not runde or runde.get("token") != token:
        return
    _quiz.pop(cid, None)
    _release(runde.get("msg"))   # keine Antwort gekommen -> Frage freigeben
    try:
        await channel.send(f"⏰ Zeit um! Die Antwort war: **{runde['answer']}**")
    except discord.HTTPException:
        pass


async def _check_quiz(message: discord.Message) -> bool:
    cid = message.channel.id
    runde = _quiz.get(cid)
    if not runde:
        return False
    guess = _norm(message.content or "")
    answer = _norm(runde["answer"])
    if not guess or not answer:
        return False
    hit = guess == answer or (len(answer) >= 3 and answer in guess)
    if not hit:
        return False
    _quiz.pop(cid, None)
    _new_token(cid)  # evtl. laufenden Timeout entwerten
    _release(runde.get("msg"))   # richtig beantwortet -> Frage freigeben
    reward = ""
    if economy.is_enabled():
        economy.add_coins(message.author.id, QUIZ_REWARD)
        await economy.add_xp(message.author, 30)
        await economy.flush()
        reward = f" (+{QUIZ_REWARD} Flo Coins)"
    try:
        await message.reply(
            f"✅ Richtig, **{message.author.display_name}**! "
            f"Antwort: {runde['answer']}{reward}", mention_author=False)
    except discord.HTTPException:
        pass
    return True


# --- Zahlenraten ---------------------------------------------------------
async def _start_guess(message: discord.Message) -> object:
    cid = message.channel.id
    if cid in _guess and _guess[cid]["expires"] > time.monotonic():
        return "Hier läuft schon eine Raterunde - rate weiter! 🔢"
    number = random.randint(1, 100)
    tok = _new_token(cid)
    emb = discord.Embed(
        title="🔢 Zahlenraten",
        description="Ich denke an eine Zahl zwischen **1 und 100** – "
                    "schreib deine Tipps einfach in den Chat!",
        color=discord.Color.blurple())
    emb.set_footer(text=f"{GUESS_TIMEOUT}s Zeit · je weniger Versuche, desto mehr Coins")
    try:
        msg = await message.reply(embed=emb, mention_author=False)
    except discord.HTTPException:
        log.exception("Zahlenraten konnte nicht gestartet werden")
        msg = None
    _guess[cid] = {"number": number, "tries": 0,
                   "expires": time.monotonic() + GUESS_TIMEOUT, "token": tok, "msg": msg}
    _protect(msg)   # laeuft (bis zu 90s) -> nicht wegraeumen lassen
    _spawn(_guess_timeout(message.channel, tok))
    return HANDLED


async def _guess_timeout(channel, token: int) -> None:
    """Beendet eine Raterunde nach GUESS_TIMEOUT, falls niemand getroffen hat:
    sagt die Zahl an und gibt die geschuetzte Start-Nachricht wieder frei."""
    await asyncio.sleep(GUESS_TIMEOUT)
    cid = getattr(channel, "id", None)
    runde = _guess.get(cid)
    if not runde or runde.get("token") != token:
        return  # schon erraten oder neue Runde
    _guess.pop(cid, None)
    _release(runde.get("msg"))
    try:
        await channel.send(f"⏰ Zeit um! Die Zahl war **{runde['number']}**.")
    except discord.HTTPException:
        pass


async def _check_guess(message: discord.Message) -> bool:
    cid = message.channel.id
    runde = _guess.get(cid)
    if not runde:
        return False
    text = (message.content or "").strip()
    if not re.fullmatch(r"\d{1,3}", text):
        return False
    if runde["expires"] < time.monotonic():
        _guess.pop(cid, None)
        _release(runde.get("msg"))
        return False
    tip = int(text)
    runde["tries"] += 1
    ziel = runde["number"]
    if tip == ziel:
        _guess.pop(cid, None)
        _new_token(cid)              # Watchdog entwerten
        _release(runde.get("msg"))   # erraten -> Start-Nachricht freigeben
        tries = runde["tries"]
        reward = max(10, 120 - tries * 10)
        extra = ""
        if economy.is_enabled():
            economy.add_coins(message.author.id, reward)
            await economy.flush()
            extra = f" (+{reward} Flo Coins)"
        try:
            await message.reply(
                f"🎯 **{message.author.display_name}** hat's mit der {ziel} - "
                f"nach {tries} Versuch(en)!{extra}", mention_author=False)
        except discord.HTTPException:
            pass
        return True
    hint = "höher ⬆️" if tip < ziel else "tiefer ⬇️"
    try:
        await message.add_reaction("⬆️" if tip < ziel else "⬇️")
    except discord.HTTPException:
        pass
    return False  # weiter raten lassen (Nachricht nicht "verbraucht")


# --- Counting-Channel ----------------------------------------------------
async def _check_counting(message: discord.Message) -> bool:
    if not COUNTING_CHANNEL_ID or message.channel.id != COUNTING_CHANNEL_ID:
        return False
    assert _store is not None
    state = _store.data.setdefault("counting", {}).setdefault(
        str(message.channel.id), {"count": 0, "last": ""})
    text = (message.content or "").strip()
    m = re.match(r"^(\d{1,6})", text)
    if not m:
        return False  # keine Zahl -> ignorieren
    num = int(m.group(1))
    expected = state["count"] + 1
    if num == expected and state.get("last") != str(message.author.id):
        state["count"] = expected
        state["last"] = str(message.author.id)
        await _store.save()
        try:
            await message.add_reaction("✅" if expected % 50 else "🎉")
        except discord.HTTPException:
            pass
        if economy.is_enabled():
            await economy.add_xp(message.author, 5)
            await economy.flush()   # add_xp speichert selbst nicht
        return True
    # Falsch oder zweimal hintereinander -> Reset. Grund VOR dem Reset ermitteln
    # (danach ist state['last'] geleert und die Meldung waere immer 'erwartet war N').
    grund = ("du warst zweimal hintereinander dran"
             if (num == expected and state.get("last") == str(message.author.id))
             else f"erwartet war {expected}")
    state["count"] = 0
    state["last"] = ""
    await _store.save()
    try:
        await message.add_reaction("❌")
        await message.channel.send(
            f"💥 **{message.author.display_name}** hat die Kette zerstört "
            f"({grund})! Zurück zur **1**.")
    except discord.HTTPException:
        pass
    return True


# --- Zufalls-Event: 'Erster der X tippt, gewinnt' ------------------------
# Echte deutsche Woerter statt Meme-Kuerzeln. Beim Start versuchen wir, eine
# System-Wortliste zu laden (Ubuntu/Debian: `apt install wngerman` legt
# /usr/share/dict/ngerman an -> ~300k echte deutsche Woerter). Fehlt sie,
# greift die eingebaute Liste unten - so funktioniert es immer.
_EVENT_WORD_MIN = 4   # nicht zu kurz (sonst zu leicht zufaellig getippt)
_EVENT_WORD_MAX = 10  # nicht zu lang (sonst nervig schnell zu tippen)
_EVENT_DICT_PATHS = (
    "/usr/share/dict/ngerman",
    "/usr/share/dict/ogerman",
    "/usr/share/dict/german",
    "/usr/share/dict/deutsch",
)
_EVENT_FALLBACK_WORDS = [
    # Obst & Essen
    "Apfel", "Banane", "Kirsche", "Erdbeere", "Zitrone", "Pflaume", "Birne",
    "Traube", "Melone", "Orange", "Brot", "Käse", "Butter", "Honig", "Kuchen",
    "Nudel", "Suppe", "Salat", "Pizza", "Wurst", "Joghurt", "Kaffee", "Wasser",
    "Milch", "Schokolade", "Bonbon", "Keks", "Waffel", "Brezel", "Knödel",
    "Gemüse", "Karotte", "Gurke", "Tomate", "Zwiebel", "Paprika", "Pilz",
    # Tiere
    "Hund", "Katze", "Maus", "Pferd", "Esel", "Tiger", "Löwe", "Affe", "Hase",
    "Fuchs", "Wolf", "Adler", "Eule", "Robbe", "Delfin", "Otter", "Igel",
    "Biber", "Dachs", "Schwein", "Schaf", "Ziege", "Huhn", "Ente", "Biene",
    "Wespe", "Käfer", "Spinne", "Raupe", "Libelle", "Elefant", "Giraffe",
    "Zebra", "Kamel", "Pinguin", "Papagei", "Schnecke", "Frosch", "Schlange",
    # Natur
    "Wolke", "Regen", "Sonne", "Mond", "Stern", "Himmel", "Donner", "Blitz",
    "Nebel", "Schnee", "Sturm", "Wind", "Berg", "Fluss", "Meer", "Strand",
    "Insel", "Wald", "Wiese", "Höhle", "Wüste", "Blume", "Rose", "Tulpe",
    "Tanne", "Eiche", "Birke", "Welle", "Regenbogen", "Vulkan", "Quelle",
    # Stadt & Gebaeude
    "Garten", "Fenster", "Brücke", "Bahnhof", "Hafen", "Turm", "Kirche",
    "Schloss", "Burg", "Markt", "Brunnen", "Mauer", "Treppe", "Keller",
    "Leuchtturm", "Tunnel", "Fabrik", "Mühle", "Scheune",
    # Fahrzeuge
    "Auto", "Fahrrad", "Schiff", "Flugzeug", "Rakete", "Traktor", "Roller",
    "Kutsche", "Schlitten", "Ballon", "Segelboot",
    # Fantasie
    "Drache", "Ritter", "Zauberer", "Hexe", "Riese", "Zwerg", "Kobold",
    "Geist", "Vampir", "Pirat", "König", "Königin", "Prinz", "Held", "Schatz",
    "Krone", "Schwert", "Schild", "Zauber", "Wunder", "Einhorn", "Phönix",
    # Musik & Schule
    "Gitarre", "Klavier", "Trommel", "Flöte", "Geige", "Trompete", "Harfe",
    "Melodie", "Buch", "Stift", "Papier", "Schere", "Pinsel", "Kreide",
    "Tafel", "Schlüssel", "Lampe", "Spiegel", "Schrank", "Kerze", "Laterne",
    "Brille", "Koffer",
    # Zeit & Gefuehl
    "Sommer", "Winter", "Frühling", "Herbst", "Morgen", "Abend", "Stunde",
    "Minute", "Freude", "Glück", "Frieden", "Hoffnung", "Traum", "Geheimnis",
    "Abenteuer", "Rätsel",
    # Verben
    "laufen", "springen", "lachen", "singen", "tanzen", "malen", "fliegen",
    "schwimmen", "klettern", "rennen", "zaubern", "träumen", "staunen",
    # Eigenschaften
    "schnell", "riesig", "winzig", "mutig", "lustig", "golden", "bunt",
    "glücklich", "neugierig", "freundlich",
]
_event_words: list[str] = []  # in setup() befuellt (System-Liste oder Fallback)


def _fold(text: str) -> str:
    """Vereinheitlicht fuer den Vergleich: alles klein, Umlaute -> ae/oe/ue/ss,
    nur noch Buchstaben. So gewinnt 'Loewe' auch, wenn jemand 'löwe!' tippt
    (und umgekehrt) - Gross-/Kleinschreibung und Satzzeichen sind egal."""
    text = text.lower()
    text = (text.replace("ä", "ae").replace("ö", "oe")
                .replace("ü", "ue").replace("ß", "ss"))
    return re.sub(r"[^a-z]", "", text)


def _load_event_words() -> list[str]:
    """Echte deutsche Woerter laden: erst eine System-Wortliste, sonst die
    eingebaute Fallback-Liste. Gefiltert auf reine Buchstaben-Woerter mit
    sinnvoller Laenge."""
    for path in _EVENT_DICT_PATHS:
        woerter: list[str] = []
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    w = line.strip()
                    if (_EVENT_WORD_MIN <= len(w) <= _EVENT_WORD_MAX
                            and re.fullmatch(r"[A-Za-zÄÖÜäöüß]+", w)):
                        woerter.append(w)
        except OSError:
            continue
        if len(woerter) >= 50:
            uniq = sorted(set(woerter))
            log.info("Event-Woerter: %d echte deutsche Woerter aus %s.",
                     len(uniq), path)
            return uniq
    log.info("Event-Woerter: keine System-Wortliste gefunden, nutze eingebaute "
             "Liste (%d Woerter). Tipp: `apt install wngerman` fuer viel mehr.",
             len(_EVENT_FALLBACK_WORDS))
    return list(_EVENT_FALLBACK_WORDS)


async def maybe_event(guild: discord.Guild) -> None:
    """bot.py ruft das periodisch. Mit kleiner Chance startet ein Schnell-Event
    im passenden Channel."""
    if not _enabled or random.random() >= EVENT_CHANCE:
        return
    channel = _pick_event_channel(guild)
    if channel is None:
        return
    if channel.id in _event and _event[channel.id]["expires"] > time.monotonic():
        return
    wort = random.choice(_event_words or _EVENT_FALLBACK_WORDS)
    tok = _new_token(channel.id)
    _event[channel.id] = {"word": _fold(wort), "display": wort, "reward": EVENT_REWARD,
                          "expires": time.monotonic() + EVENT_TIMEOUT, "token": tok}
    try:
        await channel.send(
            f"⚡ **SCHNELL!** Wer als Erster `{wort}` in den Chat schreibt, "
            f"schnappt sich **{EVENT_REWARD} Flo Coins**! (du hast {EVENT_TIMEOUT}s)")
    except discord.HTTPException:
        _event.pop(channel.id, None)
        return
    # Watchdog: meldet 'Zeit vorbei', falls bis zum Ablauf niemand getroffen hat.
    _spawn(_event_timeout(channel, tok))


async def _event_timeout(channel: discord.abc.Messageable, token: int) -> None:
    """Wartet die Event-Dauer ab. Ist die Runde dann noch offen (niemand hat das
    Wort getippt) und gehoert sie noch zu diesem Aufruf (gleicher Token), wird sie
    geschlossen und 'Zeit vorbei' angesagt. Ein zwischenzeitlicher Gewinner hat das
    Event laengst aus _event entfernt -> dann passiert hier nichts."""
    await asyncio.sleep(EVENT_TIMEOUT)
    cid = getattr(channel, "id", None)
    runde = _event.get(cid)
    if not runde or runde.get("token") != token:
        return  # schon gewonnen oder durch eine neue Runde ersetzt
    _event.pop(cid, None)
    wort = runde.get("display", runde["word"])
    try:
        await channel.send(
            f"⏰ **Zeit vorbei!** Niemand hat **{wort}** rechtzeitig getippt. "
            f"Die {runde['reward']} Flo Coins bleiben im Topf.")
    except discord.HTTPException:
        pass


def _pick_event_channel(guild: discord.Guild):
    if EVENT_CHANNEL_ID:
        ch = guild.get_channel(EVENT_CHANNEL_ID)
        if ch is not None:
            return ch
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        return guild.system_channel
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            return ch
    return None


async def _check_event(message: discord.Message) -> bool:
    cid = message.channel.id
    runde = _event.get(cid)
    if not runde:
        return False
    if runde["expires"] < time.monotonic():
        _event.pop(cid, None)
        return False
    # Treffer, wenn die Nachricht genau das gesuchte Wort ist - Gross/Klein,
    # Satzzeichen und Umlaut-Schreibweise (ae/oe/ue/ss) sind egal.
    content = message.content or ""
    folded = _fold(content)
    if folded != runde["word"]:
        # Kein Treffer. War es ein knapper Fehlversuch? (Ein einzelnes Wort ab 4
        # Buchstaben, das dem gesuchten sehr aehnlich ist - also vertippt.) Dann
        # kurz 'falsch geschrieben' melden. Normale Chat-Saetze (mehrere Woerter)
        # und kurze Allerweltswoerter ('und', 'ist', ...) ignorieren wir bewusst.
        if (len(folded) >= 4 and len(content.split()) == 1
                and difflib.SequenceMatcher(None, folded, runde["word"]).ratio() >= 0.6):
            try:
                await message.channel.send(
                    f"❌ {message.author.mention} – fast! **{content.strip()}** ist "
                    f"falsch geschrieben. Tipp das Wort nochmal *genau* richtig! ⏳",
                    delete_after=8)
            except discord.HTTPException:
                pass
            return True  # Fehlversuch 'verbraucht' -> nicht an die KI weiterreichen
        return False
    _event.pop(cid, None)  # erster Treffer gewinnt -> Runde sofort schliessen
    belohnung = ""
    if economy.is_enabled():
        economy.add_coins(message.author.id, runde["reward"])
        await economy.add_xp(message.author, 20)
        await economy.flush()
        belohnung = f" und schnappt sich **+{runde['reward']} Flo Coins** 💰"
    wort = runde.get("display", runde["word"])
    text = (f"🏁 {message.author.mention} war am schnellsten mit **{wort}**"
            f"{belohnung}!")
    try:
        # Direkt im Chat ansagen + Gewinner anpingen. Bewusst channel.send
        # (kein Reply), damit es auch klappt, wenn die Tipp-Nachricht im
        # Auto-Loesch-Channel schon wieder weg ist.
        await message.channel.send(text)
    except discord.HTTPException:
        try:
            await message.reply(text, mention_author=True)
        except discord.HTTPException:
            pass
    return True


# --- Passiver Hook (bot.py ruft das fuer JEDE Nicht-Bot-Nachricht) -------
async def on_message_passive(message: discord.Message) -> bool:
    """Prueft laufende Spiele/Events fuer diese Nachricht.
    Rueckgabe True = Nachricht wurde 'verbraucht' (bot.py stoppt die Verarbeitung)."""
    if not _enabled or message.guild is None:
        return False
    if await _check_counting(message):
        return True
    if await _check_event(message):
        return True
    if await _check_quiz(message):
        return True
    if await _check_guess(message):
        return True
    return False
