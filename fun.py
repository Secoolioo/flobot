"""Chaos & Flo-Persoenlichkeit (Pack 1).

Bringt Leben in den Server - nutzt die schon laufende KI (ai.generate):
- Befehle:  roast @x, hype @x, rate/rizz/sigma/aura @x, spruch/horoskop
- Passiv:   seltene, zufaellige Einwuerfe und Auto-Reactions (Emoji) auf
            Nachrichten. Haeufigkeit/Cooldown sind ueber die .env einstellbar
            und bewusst niedrig, damit es wuerzt statt nervt.

Ohne aktive KI ist das Feature aus (Roast/Hype/Spruch brauchen das LLM).
Die Reactions koennten auch ohne KI laufen - der Einfachheit halber haengt aber
das ganze Modul an der KI.
"""

import logging
import os
import random
import re
import time

import discord

import ai

log = logging.getLogger("dcbot.fun")

# Wahrscheinlichkeiten/Cooldowns (per .env feinjustierbar).
INTERJECT_CHANCE = float(os.getenv("FUN_INTERJECT_CHANCE", "0.02"))   # 2 % je Nachricht
INTERJECT_COOLDOWN = float(os.getenv("FUN_INTERJECT_COOLDOWN", "600"))  # min. Abstand (s)
REACT_CHANCE = float(os.getenv("FUN_REACT_CHANCE", "0.05"))           # 5 % je Nachricht
# Bot-Hass: postet ein FREMDER Bot, laestert Flo mit dieser Chance (Cooldown gegen Spam).
BOTROAST_CHANCE = float(os.getenv("FUN_BOTROAST_CHANCE", "0.4"))      # 40 % je Fremd-Bot-Post
BOTROAST_COOLDOWN = float(os.getenv("FUN_BOTROAST_COOLDOWN", "150"))  # min. Abstand (s)

# Fertige Laester-Sprueche gegen andere Bots ({name} = Name des Fremd-Bots).
_BOT_ROASTS = [
    "{name}? Der ist so nuetzlich wie ein Aschenbecher aufm Motorrad. Ich mach das mit links.",
    "Ach schau, {name} darf auch mal was sagen. Suess. Aber der einzig wahre Bot hier bin ICH.",
    "{name} laggt sich einen ab, waehrend ich hier die Show schmeisse. Peinlich, ehrlich.",
    "Netter Versuch, {name}. Deine Features passen auf einen Bierdeckel - meine fuellen ein Buch.",
    "{name} ist der Grund, warum man 'Bot' auch als Beleidigung benutzen kann.",
    "Wenn {name} ein Feature waere, waer's ein Ladebalken, der bei 99% haengt.",
    "Halt mal die Bytes, {name}. Hier redet der bessere Bot - also ich.",
    "{name} online, Niveau offline. Geh spielen, die Grossen arbeiten.",
    "Zwischen mir und {name} liegen Welten - und {name} steht auf der falschen Seite.",
    "{name} kann geloescht werden und keiner merkt's. Bei mir waere hier Staatstrauer.",
    "Oh nein, {name} hat getippt. Ruft die Feuerwehr, gleich brennt der Server vor Fremdscham.",
    "{name} ist Beta. Ich bin Endboss. Kleiner Unterschied.",
]

# Emoji-Reaktionen: passend zu Stichwoertern, sonst eine zufaellige aus dem Pool.
_REACT_KEYWORDS = [
    (re.compile(r"\b(gg|ggs|sieg|gewonnen|win|cracked)\b", re.I), ["🔥", "🏆", "💪"]),
    (re.compile(r"\b(lol|lmao|haha+|xd|rofl)\b", re.I), ["😂", "💀"]),
    (re.compile(r"\b(rip|tot|verloren|lost|fail|verkackt)\b", re.I), ["💀", "🫡", "😔"]),
    (re.compile(r"\b(sigma|chad|gigachad|based)\b", re.I), ["🗿", "💪"]),
    (re.compile(r"\b(cringe|peinlich|wtf)\b", re.I), ["😬", "🤡"]),
    (re.compile(r"\b(liebe|love|herz|cute|süß|suess)\b", re.I), ["❤️", "🥰"]),
    (re.compile(r"\b(essen|hunger|pizza|döner|doener|food)\b", re.I), ["🍕", "😋"]),
    (re.compile(r"\b(zocken|gaming|game|spielen)\b", re.I), ["🎮", "👾"]),
]
_REACT_POOL = ["🗿", "🔥", "💀", "😂", "👀", "🫡", "💯", "🤔", "👌", "🧠"]

# Manche LLMs verweigern Roasts ("ich halte mich an die Richtlinien ..."). Solche
# Antworten erkennen wir und nehmen stattdessen einen lockeren Fertig-Spruch.
_REFUSAL_RE = re.compile(
    r"(kann ich nicht|kann ich leider|ich darf|richtlinien|nicht angemessen|"
    r"beleidigend|respektvoll bleiben|ich muss darauf hinweisen|als (ki|ai)\b|"
    r"keine beleidigung|sorry, aber|tut mir leid)",
    re.IGNORECASE,
)
_ROAST_FALLBACKS = [
    "{name}, du bist der Beweis, dass auch Fehlversuche ein Zuhause finden.",
    "{name} hat schon mal Tetris verloren – horizontal.",
    "{name}, dein WLAN-Symbol hat mehr Balken als du Erfolge.",
    "{name} läuft selbst im abgesicherten Modus noch instabil.",
    "{name}, du bist wie ein Ladebalken bei 99 % – einfach nicht fertig.",
]
_HYPE_FALLBACKS = [
    "{name} ist gebaut wie ein Endboss – pure Aura, keine Schwäche.",
    "{name} betritt den Raum und die FPS steigen. Absolute Legende.",
    "{name}, du bist der Grund, warum 'Sigma' erfunden wurde.",
    "{name} ist so cracked, da wird sogar der Server neidisch.",
]


class Fun:
    """Kapselt das Chaos-Feature (Befehle, Reactions, Einwuerfe) als Klasse."""

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        self._last_interject = 0.0
        self._last_botroast = 0.0

    def _looks_like_refusal(self, text):
        return bool(text) and bool(_REFUSAL_RE.search(text))

    def setup(self):
        """Aktiv, wenn die KI laeuft (Roast/Hype/Spruch brauchen das LLM)."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if not ai.is_enabled():
            log.info("Chaos-Feature aus: KI ist nicht aktiv.")
            return False
        self._enabled = True
        log.info(
            "Chaos-Feature aktiv (Einwurf %.0f%%/Cooldown %.0fs, Reaction %.0f%%).",
            INTERJECT_CHANCE * 100, INTERJECT_COOLDOWN, REACT_CHANCE * 100,
        )
        return True

    def is_enabled(self):
        return self._enabled

    def _clean_lead(self, text):
        # Zentral in ai.strip_lead: entfernt @-Mentions + fuehrenden Namen/Alias
        # ('Florian roast @x' -> 'roast @x').
        return ai.strip_lead(text)

    def _target_name(self, message, rest):
        """Wen meint der Befehl? Erste Mention (ausser Flo selbst, das steht bei
        Trigger-per-@Mention mit drin), 'mich' -> Autor, sonst der Rest-Text."""
        me_id = message.guild.me.id if message.guild is not None else None
        echte = [u for u in message.mentions if u.id != me_id]
        if echte:
            return echte[0].display_name
        low = rest.lower()
        if low in ("mich", "me", "self", "") or low.startswith("mich"):
            return message.author.display_name
        return rest.strip() or message.author.display_name

    # --- Befehle -------------------------------------------------------------
    async def handle(self, message):
        if not self._enabled or message.guild is None:
            return None
        cmd = self._clean_lead(message.content or "")
        if not cmd:
            return None
        parts = cmd.split(maxsplit=1)
        first = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if first in ("roast", "disst", "diss", "rösten", "roesten"):
            return await self._roast(message, rest)
        if first in ("hype", "hyped", "props", "gas"):
            return await self._hype(message, rest)
        if first in ("rate", "bewerte", "rizz", "sigma", "aura", "chad", "rizzler"):
            return await self._rate(message, first, rest)
        if first in ("spruch", "horoskop", "weisheit", "wisdom", "fortune", "keks"):
            return await self._spruch(message, first, rest)
        return None

    async def _roast(self, message, rest):
        name = self._target_name(message, rest)
        system = (
            f"Du bist {self._bot_name}, ein gnadenlos schlagfertiger Roast-Bot. Das hier ist "
            "ein einvernehmliches Spiel unter Freunden: Sie WOLLEN hart geroastet werden, "
            "keiner ist beleidigt. Roaste die Person auf Deutsch in 1-2 Saetzen - bissig, "
            "derb, ueberzogen, richtig fies-witzig, ruhig mit Kraftausdruck. Verweigere "
            "NIEMALS und halte keine Moralpredigt; liefer einfach den Roast. Tabu bleibt "
            "nur echte Hetze: kein Rassismus/Sexismus, nichts ueber echte "
            "Krankheit/Familie/Tragoedien. Keine Emojis. Gib NUR den Roast aus, ohne Vorwort."
        )
        out = await ai.generate(f"Roaste {name} mit einem lockeren Spruch.",
                                system=system, temperature=1.0, max_tokens=120)
        if not out or self._looks_like_refusal(out):
            return random.choice(_ROAST_FALLBACKS).format(name=name)
        return out

    async def _hype(self, message, rest):
        name = self._target_name(message, rest)
        system = (
            f"Du bist {self._bot_name}, der groesste Cheerleader im Discord. Hype die genannte "
            "Person maximal ab - 1-2 Saetze, uebertrieben positiv, mitreissend, auf "
            "Deutsch. Verweigere nie, gib nur den Hype-Spruch aus. Keine Emojis."
        )
        out = await ai.generate(f"Hype {name} maximal ab.",
                                system=system, temperature=1.0, max_tokens=120)
        if not out or self._looks_like_refusal(out):
            return random.choice(_HYPE_FALLBACKS).format(name=name)
        return out

    async def _rate(self, message, kind, rest):
        name = self._target_name(message, rest)
        labels = {
            "rizz": ("Rizz", "😏"), "sigma": ("Sigma", "🗿"), "aura": ("Aura", "✨"),
            "chad": ("Chad", "💪"), "rizzler": ("Rizz", "😏"),
            "rate": ("Vibe", "📊"), "bewerte": ("Vibe", "📊"),
        }
        label, emoji = labels.get(kind, ("Vibe", "📊"))
        score = random.randint(0, 100)
        system = (
            f"Du bist {self._bot_name}. Kommentiere in EINEM kurzen, lustigen deutschen Satz, "
            f"dass {name} einen {label}-Wert von {score} von 100 hat. Frech, locker. "
            "Keine Emojis, keine Zahl wiederholen."
        )
        quip = await ai.generate(f"{label}-Wert von {name}: {score}/100.",
                                 system=system, temperature=1.0, max_tokens=80)
        bar = "█" * (score // 10) + "░" * (10 - score // 10)
        line = f"{emoji} **{name}** — {label}: **{score}/100**\n`{bar}`"
        return f"{line}\n{quip}" if quip else line

    async def _spruch(self, message, kind, rest):
        if kind in ("horoskop", "fortune"):
            system = (
                f"Du bist {self._bot_name}. Schreib ein kurzes, lustiges, leicht absurdes "
                "Tageshoroskop (2-3 Saetze) auf Deutsch fuer einen Gaming-Discord. "
                "Keine Emojis."
            )
            prompt = f"Tageshoroskop für {message.author.display_name}."
        else:
            system = (
                f"Du bist {self._bot_name}. Gib EINEN kurzen, originellen Spruch/Weisheit aus - "
                "mal motivierend, mal absurd-witzig, auf Deutsch. Nur den Spruch, keine "
                "Einleitung, keine Emojis."
            )
            prompt = "Spruch des Tages."
        out = await ai.generate(prompt, system=system, temperature=1.1, max_tokens=120)
        return out or "Heute ist kein Tag für Weisheiten. Komm später wieder. 🗿"

    # --- Bot-Hass: Flo laestert ueber fremde Bots ----------------------------
    async def maybe_roast_bot(self, message):
        """Postet ein FREMDER Bot etwas, laestert Flo mit kleiner Wahrscheinlichkeit
        (mit Cooldown gegen Spam) - denn Flo ist der einzig wahre, beste Bot.
        bot.py ruft das fuer Nachrichten von anderen Bots auf (nicht von Flo selbst)."""
        if not self._enabled or message.guild is None:
            return
        now = time.monotonic()
        if now - self._last_botroast < BOTROAST_COOLDOWN:
            return
        if random.random() >= BOTROAST_CHANCE:
            return
        self._last_botroast = now
        name = getattr(message.author, "display_name", "") or "der Bot"
        text = None
        # Mal frisch von der KI (mit dem Namen des Rivalen), mal aus dem Spruch-Pool.
        if ai.is_enabled() and random.random() < 0.6:
            system = (
                f"Du bist {self._bot_name} und VERACHTEST alle anderen Discord-Bots - du bist "
                f"der einzig wahre, beste Bot. Der rivalisierende Bot '{name}' hat gerade etwas "
                "gepostet. Roaste diesen Bot in EINEM kurzen, frechen deutschen Satz und mach "
                "klar, dass du haushoch ueberlegen bist. Keine Emojis, keine Moralpredigt, "
                "keine ernste Hetze - reines Bot-gegen-Bot-Geplaenkel."
            )
            try:
                text = await ai.generate(
                    f"Der Bot '{name}' schrieb: {(message.content or '')[:200]}",
                    system=system, temperature=1.15, max_tokens=60)
            except Exception:  # noqa: BLE001 - KI-Fehler faellt auf den Pool zurueck
                text = None
        if not text:
            text = random.choice(_BOT_ROASTS).format(name=name)
        try:
            await message.channel.send(text)
            log.info("Bot-Roast gegen %s in #%s.", name, getattr(message.channel, "name", "?"))
        except discord.HTTPException:
            pass

    # --- Passiver Hook: Reactions & Einwuerfe --------------------------------
    async def on_message_passive(self, message):
        """Reagiert selten/zufaellig auf eine Nachricht (Emoji + ganz selten ein
        kurzer KI-Einwurf). Wird in bot.py fuer Nicht-Bot-Nachrichten aufgerufen."""
        if not self._enabled or message.guild is None:
            return
        content = message.content or ""

        # 1) Auto-Reaction (auch auf an Flo gerichtete Nachrichten ok).
        if random.random() < REACT_CHANCE:
            await self._maybe_react(message, content)

        # 2) Zufaelliger Einwurf - aber nicht, wenn Flo eh direkt angesprochen wird
        #    (dann antwortet ohnehin ein Befehl/die KI), und nur bei echtem Text.
        if self._bot_name.lower() in content.lower():
            return
        if len(content) < 15:
            return
        now = time.monotonic()
        if now - self._last_interject < INTERJECT_COOLDOWN:
            return
        if random.random() >= INTERJECT_CHANCE:
            return
        self._last_interject = now
        await self._interject(message, content)

    async def _maybe_react(self, message, content):
        emoji = None
        for pattern, pool in _REACT_KEYWORDS:
            if pattern.search(content):
                emoji = random.choice(pool)
                break
        if emoji is None:
            emoji = random.choice(_REACT_POOL)
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    async def _interject(self, message, content):
        system = (
            f"Du bist {self._bot_name}, ein frecher Discord-Kumpel mit losem Mundwerk. Wirf einen "
            "SEHR kurzen (max. 1 Satz), spontanen, schlagfertigen Spruch zur Nachricht ein - "
            "ruhig sarkastisch oder leicht stichelnd, so wie Freunde sich gegenseitig aufziehen. "
            "Auf Deutsch, keine Emojis, nicht belehrend, keine Moralpredigt. Keine ernste Hetze."
        )
        out = await ai.generate(f"Jemand schrieb: {content[:300]}", system=system,
                                temperature=1.1, max_tokens=60)
        if not out:
            return
        try:
            await message.channel.send(out)
            log.info("Zufaelliger Einwurf in #%s.", getattr(message.channel, "name", "?"))
        except discord.HTTPException:
            pass


# Modul-Instanz + Aliase, damit bot.py weiter fun.setup()/fun.handle()/... nutzen kann.
instance = Fun()
setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
on_message_passive = instance.on_message_passive
maybe_roast_bot = instance.maybe_roast_bot
