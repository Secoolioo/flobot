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
import economy
from store import JsonStore

log = logging.getLogger("dcbot.games")

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
async def handle(message: discord.Message) -> str | None:
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
        return _start_guess(message)
    if first in ("ssp", "schnickschnack", "rps", "sss"):
        return await _ssp(message, args)
    if first in ("coinflip", "münzwurf", "muenzwurf", "flip", "münze", "muenze"):
        return await _coinflip(message, args)
    if first in ("slot", "slots", "spielautomat", "automat"):
        return await _slot(message, args)
    if first in ("würfel", "wuerfel", "würfeln", "wuerfeln", "dice", "roll", "w6"):
        return _dice(args)
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


# --- Coinflip ------------------------------------------------------------
async def _coinflip(message: discord.Message, args: list[str]) -> str:
    bet = _extract_int(args)
    seite = next((a.lower() for a in args
                  if a.lower() in ("kopf", "zahl", "heads", "tails")), None)
    ergebnis = random.choice(["kopf", "zahl"])
    sym = "👑" if ergebnis == "kopf" else "🔢"

    if bet and economy.is_enabled():
        if not seite:
            return f"Auf was setzt du? `{_bot_name} coinflip {bet} kopf` (oder zahl)."
        tip = "kopf" if seite in ("kopf", "heads") else "zahl"
        if economy.get_coins(message.author.id) < bet:
            return f"Du hast nicht genug. Konto: {economy.get_coins(message.author.id)} Flo Coins."
        if tip == ergebnis:
            economy.add_coins(message.author.id, bet)
            await economy.flush()
            return f"{sym} **{ergebnis.upper()}** — gewonnen! +{bet} Flo Coins. 🎉"
        economy.add_coins(message.author.id, -bet)
        await economy.flush()
        return f"{sym} **{ergebnis.upper()}** — verloren! -{bet} Flo Coins. 😬"
    return f"{sym} Die Münze zeigt: **{ergebnis.upper()}**!"


# --- Slot-Machine --------------------------------------------------------
_SLOT_REELS = ["🍒", "🍋", "🔔", "🍉", "⭐", "💎", "🗿"]
_SLOT_PAYOUT = {  # drei Gleiche -> Faktor auf den Einsatz
    "🗿": 25, "💎": 15, "⭐": 10, "🔔": 7, "🍉": 5, "🍋": 4, "🍒": 3,
}


async def _slot(message: discord.Message, args: list[str]) -> str:
    bet = _extract_int(args) or 0
    use_coins = bet > 0 and economy.is_enabled()
    if use_coins and economy.get_coins(message.author.id) < bet:
        return f"Du hast nicht genug. Konto: {economy.get_coins(message.author.id)} Flo Coins."

    reels = [random.choice(_SLOT_REELS) for _ in range(3)]
    line = " | ".join(reels)
    win = 0
    if reels[0] == reels[1] == reels[2]:
        win = (bet or 10) * _SLOT_PAYOUT[reels[0]]
        text = f"🎰 [ {line} ]\n**JACKPOT!** {reels[0]*3}"
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        win = (bet or 10) * 2
        text = f"🎰 [ {line} ]\nZwei Gleiche — kleiner Gewinn!"
    else:
        text = f"🎰 [ {line} ]\nNix. Versuch's nochmal!"

    if use_coins:
        net = win - bet
        economy.add_coins(message.author.id, net)
        await economy.flush()
        if net > 0:
            text += f"\n+{net} Flo Coins (Konto: {economy.get_coins(message.author.id)})"
        else:
            text += f"\n-{bet} Flo Coins (Konto: {economy.get_coins(message.author.id)})"
    elif win and economy.is_enabled():
        economy.add_coins(message.author.id, win)
        await economy.flush()
        text += f"\n+{win} Flo Coins"
    return text


# --- Wuerfel -------------------------------------------------------------
def _dice(args: list[str]) -> str:
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
        return f"🎲 Du würfelst eine **{rolls[0]}** (W{sides})."
    return f"🎲 {count}×W{sides}: {' + '.join(map(str, rolls))} = **{sum(rolls)}**"


def _extract_int(args: list[str]) -> int | None:
    for a in args:
        if a.isdigit():
            return int(a)
    return None


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


async def _start_quiz(message: discord.Message) -> str:
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
    _quiz[cid] = {"answer": antwort, "frage": frage,
                  "expires": time.monotonic() + QUIZ_TIMEOUT, "token": tok}
    _spawn(_quiz_timeout(message.channel, tok))
    return f"🧠 **Quiz!** (du hast {QUIZ_TIMEOUT}s)\n{frage}"


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
def _start_guess(message: discord.Message) -> str:
    cid = message.channel.id
    if cid in _guess and _guess[cid]["expires"] > time.monotonic():
        return "Hier läuft schon eine Raterunde - rate weiter! 🔢"
    number = random.randint(1, 100)
    _guess[cid] = {"number": number, "tries": 0,
                   "expires": time.monotonic() + GUESS_TIMEOUT}
    return ("🔢 Ich denke an eine Zahl zwischen **1 und 100**. "
            "Schreib deine Tipps einfach in den Chat!")


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
        return False
    tip = int(text)
    runde["tries"] += 1
    ziel = runde["number"]
    if tip == ziel:
        _guess.pop(cid, None)
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
        return True
    # Falsch oder zweimal hintereinander -> Reset.
    state["count"] = 0
    state["last"] = ""
    await _store.save()
    grund = ("du warst zweimal hintereinander dran"
             if state.get("last") == str(message.author.id) else f"erwartet war {expected}")
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
        # Kein Treffer. War es ein knapper Fehlversuch? (Ein einzelnes Wort, das
        # dem gesuchten sehr aehnlich ist - also vertippt.) Dann kurz 'falsch
        # geschrieben' melden. Normale Chat-Saetze (mehrere Woerter) ignorieren wir.
        if (folded and len(content.split()) == 1
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
