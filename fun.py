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
from basis import FeatureBasis, echte_erwaehnungen

log = logging.getLogger("dcbot.fun")

# Wahrscheinlichkeiten/Cooldowns (per .env feinjustierbar).
INTERJECT_CHANCE = float(os.getenv("FUN_INTERJECT_CHANCE", "0.02"))   # 2 % je Nachricht
INTERJECT_COOLDOWN = float(os.getenv("FUN_INTERJECT_COOLDOWN", "600"))  # min. Abstand (s)
REACT_CHANCE = float(os.getenv("FUN_REACT_CHANCE", "0.05"))           # 5 % je Nachricht
# Bot-Hass: postet ein FREMDER Bot, laestert Flo mit dieser Chance (Cooldown gegen Spam).
BOTROAST_CHANCE = float(os.getenv("FUN_BOTROAST_CHANCE", "0.4"))      # 40 % je Fremd-Bot-Post
BOTROAST_COOLDOWN = float(os.getenv("FUN_BOTROAST_COOLDOWN", "150"))  # min. Abstand (s)
# DM-Konter: wer im Chat nur Beleidigungen/Muell raushaut, kriegt GANZ SELTEN von
# Flo privat (DM) eine freche Retoure. Bewusst niedrige Chance + doppelter Cooldown
# (pro Person UND serverweit), damit es nur selten passiert - alles per .env.
DMROAST_CHANCE = float(os.getenv("FUN_DMROAST_CHANCE", "0.08"))              # 8 % je erkannter Beleidigung
DMROAST_USER_COOLDOWN = float(os.getenv("FUN_DMROAST_USER_COOLDOWN", "21600"))    # 6 h pro Person
DMROAST_GLOBAL_COOLDOWN = float(os.getenv("FUN_DMROAST_GLOBAL_COOLDOWN", "1800"))  # 30 min serverweit
# Gegenrede: schreibt jemand menschenfeindlichen Muell in den Chat, haelt Flo
# OEFFENTLICH dagegen. Anders als der DM-Konter wird hier NICHT gewuerfelt - sie
# soll wirken, nicht ueberraschen. Gebremst wird nur gegen Flut.
GEGENREDE_USER_COOLDOWN = float(os.getenv("FUN_GEGENREDE_USER_COOLDOWN", "120"))    # 2 min pro Person
GEGENREDE_GLOBAL_COOLDOWN = float(os.getenv("FUN_GEGENREDE_GLOBAL_COOLDOWN", "30"))  # 30 s serverweit

# Erkennt Beleidigungen / "random Scheiss" (deutsch, grob).
#
# Die Liste stand frueher in EINEM Regex, und darin standen ganz normale
# deutsche Woerter: Maul, Opfer, Lappen, Spinner, Arsch, Penner, Noob. Damit
# galten 15 von 20 harmlosen Alltagssaetzen als Beleidigung - "Der Hund hatte
# den Ball im Maul", "Bei dem Erdbeben gab es viele Opfer", "Wisch das mit dem
# Lappen weg". Flo hat wildfremden, hoeflichen Leuten daraufhin eine
# beleidigende DM geschickt.
#
# Deshalb ZWEI Listen: eindeutige Beleidigungen zaehlen ueberall, mehrdeutige
# Alltagswoerter nur mit direkter Anrede ("du Opfer", "halt dein Maul").
_INSULT_RE = re.compile(
    r"\b("
    r"schei(ss|ß)e?|fuck|fick(en|st|t)?|gefickt|motherfucker|"
    r"wichser|wixer|wixxer|arschloch|fotze|hurensohn|huso|hurentochter|"
    r"hurensöhne|hure|schlampe|missgeburt|spast(i)?|spacko|spacken|"
    r"vollidiot|vollpfosten|trottel|idiot|drecksau|dreckskerl|drecksack|"
    r"mistkerl|bastard|bitch|verpiss"
    r")\b",
    re.IGNORECASE,
)
# Mehrdeutig: nur als Beleidigung zaehlen, wenn wirklich jemand gemeint ist.
_MEHRDEUTIG = (r"maul|fresse|opfer|lappen|spinner|versager|noob|penner|arsch|"
               r"kacke|kack|depp|nutte")
# Nur ANREDE-Formen (du/dein/ihr/euer), nicht dir/dich: "Ich hab dir den Lappen
# gegeben" ist kein Angriff, "du Lappen" schon. Hoechstens zwei Woerter dazwischen,
# damit "du hast gestern den Ball ins Maul bekommen" nicht mitzaehlt.
_INSULT_ANREDE_RE = re.compile(
    r"\b(?:du|dein(?:e|em|en|er)?|ihr|euer|eure|halt\s+(?:die|dein\w*))\s+"
    r"(?:\w+\s+){0,2}"
    rf"(?:{_MEHRDEUTIG})\b",
    re.IGNORECASE,
)
# Fertige DM-Konter, falls die KI aus ist oder abblockt ({name} = Ziel).
_DM_ROASTS = [
    "Ey {name}, dein Wortschatz hat angerufen - er will sein Niveau zurück.",
    "Sag mal {name}, muss man dumm sein oder ist das bei dir ein Hobby?",
    "{name}, du laberst Müll, als würdest du dafür bezahlt. Spoiler: tust du nicht.",
    "Nettes Geschreibsel, {name}. Hält dich wenigstens einer für witzig? Nein? Dachte ich mir.",
    "{name}, wenn Blödheit Coins wären, wärst du reicher als das ganze Casino.",
    "Hör zu {name}: erst denken, dann tippen. In deinem Fall: einfach mal nicht tippen.",
    "{name}, du bist im Chat wie Fußpilz - keiner will dich, aber du bist trotzdem da.",
    "Beeindruckend, {name}. So viel Unsinn und trotzdem keine einzige Pointe.",
]

# --- Gegenrede: menschenfeindlichen Muell erkennen ------------------------
#
# Hier gilt dieselbe teuer bezahlte Lehre wie oben bei _INSULT_RE, nur schaerfer:
# der DM-Konter geht privat raus, die Gegenrede steht OEFFENTLICH im Kanal. Eine
# oeffentliche Zurechtweisung, die danebenliegt, ist schlimmer als gar keine.
# Deshalb im Zweifel IMMER nicht ausloesen.
#
# Und eine zweite Grenze: Flo hat eine politische Meinung und darf sie derb
# vertreten (siehe ai._POLITIK). Eine Meinung zu Zuwanderung ist etwas anderes
# als Hetze gegen Menschen - deshalb steht hier KEIN politisches Vokabular,
# sondern nur Entmenschlichung und Gewaltaufrufe.

# 1) Eindeutig: Woerter, die niemand harmlos benutzt. Zaehlen ueberall.
_HETZE_RE = re.compile(
    r"\b("
    r"judensau|judenschwein|judenpack|judenpresse|"
    r"untermensch(en)?|rassenschande|volksschädling|volksschaedling|"
    r"sieg\s*heil|heil\s+hitler|"
    r"kanake|kanaken|kanacke|kanacken|kanakenpack|"
    r"neger|negerin|negern|nigger|schlitzauge|schlitzaugen|"
    r"schwuchtel|schwuchteln|"
    r"ausländerpack|auslaenderpack|"
    r"holocaust(-|\s*)?(lüge|luege|märchen|maerchen|schwindel)|"
    r"auschwitz(-|\s*)?(lüge|luege|märchen|maerchen)"
    r")\b",
    re.IGNORECASE,
)
# 2) Mehrdeutig: eine Menschengruppe zu NENNEN ist voellig harmlos. Erst das
#    Urteil darueber macht es zur Hetze - "X sind Ungeziefer", "X gehoeren
#    vergast", "X muessen raus". Deshalb Gruppe + Urteilswort + Abwertung, und
#    zwar dicht beieinander.
_GRUPPE = (r"jud(?:en|e)|jüdinnen|juedinnen|moslems?|muslim(?:e|en|innen)?|"
           r"türken|tuerken|araber|afrikaner|schwarzen|"
           r"ausländer\w*|auslaender\w*|flüchtlinge|fluechtlinge|asylanten|"
           r"migranten|zigeuner\w*|homos|schwule\w*|lesben|behinderte\w*")
# Behauptung ("X SIND ...") und Forderung ("X GEHOEREN ...") werden bewusst
# getrennt: "raus/weg" ist nur als FORDERUNG Hetze. "Die Fluechtlinge sind weg"
# ist eine Feststellung, "Fluechtlinge muessen raus" eine Parole.
_URTEIL_HART = r"sind|ist|war(?:en)?|bleiben"
_URTEIL_FORDERUNG = r"gehör\w+|gehoer\w+|soll\w*|müssen|muessen|muss|sollte\w*"
_ABWERTUNG = (r"ungeziefer|ratten|parasiten|abschaum|dreckspack|"
              r"minderwertig\w*|untermenschen?|"
              r"vergast|vergasen|ausgerottet|ausrotten|verrecken|krepieren|"
              r"abgeknallt|abknallen|aufgehängt|aufgehaengt|vergasung")
_FORDERUNG = r"raus|weg|entsorgt|entsorgen|abgeschafft"
_HETZE_URTEIL_RE = re.compile(
    rf"\b(?:{_GRUPPE})\b(?:\s+\w+){{0,2}}\s+"
    rf"(?:(?:{_URTEIL_HART}|{_URTEIL_FORDERUNG})\b(?:\s+\w+){{0,2}}\s+(?:{_ABWERTUNG})"
    rf"|(?:{_URTEIL_FORDERUNG})\b(?:\s+\w+){{0,2}}\s+(?:{_FORDERUNG}))\b",
    re.IGNORECASE,
)
# 3) Die Parole ohne Verb ("Ausländer raus") - der Klassiker, den Muster 2 nicht
#    faengt, weil kein Urteilswort dazwischensteht.
_HETZE_PAROLE_RE = re.compile(
    rf"\b(?:{_GRUPPE})\s+(?:raus|weg)\b", re.IGNORECASE)

# 3b) Leugnung. "Holocaust" allein ist ein voellig normales Thema (Referat,
#     Doku, Unterricht) - erst das Bestreiten macht es zur Hetze. Muster 1
#     faengt nur das zusammengeschriebene Wort ("Holocaust-Luege"), hier kommt
#     die auseinandergezogene Form dazu.
_HETZE_LEUGNUNG_RE = re.compile(
    r"\b(?:holocaust|auschwitz|schoah|shoah)\b(?:\s+\w+){0,3}\s+"
    r"(?:lüge|luege|märchen|maerchen|schwindel|erfunden|erlogen|"
    r"nie\s+gegeben|nie\s+passiert)"
    r"|(?:erfunden|erlogen|frei\s+erfunden)\b(?:\s+\w+){0,3}\s+"
    r"\b(?:holocaust|auschwitz)\b",
    re.IGNORECASE,
)

# 4) Die Meta-Ausnahme, und die ist der Kern des Ganzen: wer UEBER so etwas
#    redet - es meldet, es benennt, es zitiert, dagegen ist -, wird nicht
#    angegangen. Ohne sie wuerde Flo ausgerechnet die anmachen, die sich
#    beschweren.
_META_RE = re.compile(
    r"(?:ist|war|klingt|find(?:e|est|et)|wär(?:e)?|waer(?:e)?)\s+"
    r"(?:(?:voll|echt|ziemlich|sehr|einfach|ja|doch|schon)\s+)*"
    r"(?:antisemitisch|rassistisch|menschenverachtend|hetze|volksverhetzung|"
    r"widerlich|ekelhaft|daneben|nazi\w*|braun|strafbar|verboten)"
    r"|sagt man (?:so )?nicht|sowas sagt man|macht man nicht|geht (?:ja )?gar nicht|"
    r"(?:hat|hab|habe|hast|haben)\s+(?:das |sowas |er |sie |die |gerade |eben |vorhin )*"
    r"(?:ge)?(?:sagt|schrieben|schrieb|postet|gepostet)|"
    r"gegen (?:rassismus|antisemitismus|hetze|nazis|rechte)|"
    r"kein(?:e|en)? (?:rassismus|antisemitismus|hetze|platz)|"
    r"melde|meldet|gemeldet|report|anzeige|volksverhetzung|"
    r"(?:im )?(?:geschichts|geschichte|unterricht|referat|dokumentation|doku)\b",
    re.IGNORECASE,
)
# Zitiert jemand (Anfuehrungszeichen, Discord-Zitat '>', Codeblock), reden wir
# UEBER den Satz, nicht in ihm. Bewusst grob: lieber einmal zu oft schweigen.
_ZITAT_RE = re.compile(r'(?m)^\s*>|["\u201c\u201d\u201e\u00ab\u00bb]|`')

# Fertige Gegenreden, falls die KI aus ist oder abblockt. Bauart wie gewuenscht:
# erst ein Hieb auf DEN SCHREIBER, dann eine klare Ansage. Nie ein Wort ueber
# die Gruppe, um die es ging ({name} = der Schreiber).
_GEGENREDE = [
    "{name}, das war der billigste Satz, den dieser Server je gesehen hat, und du hast ihn freiwillig getippt. So einen Dreck lass hier stecken.",
    "Krass {name}, du brauchst echt eine ganze Menschengruppe, damit du dich mal groß fühlst. Das läuft hier nicht, such dir ein anderes Hobby.",
    "{name}, dein Hirn hat gerade Feierabend gemacht und dein Daumen hat trotzdem weitergetippt. So was kommt hier nicht durch, Ende.",
    "Wow {name}, mutig, so einen Müll rauszuhauen, wo dich keiner drum gebeten hat. Hier ist dafür kein Platz, merk dir das.",
    "{name}, das ist nicht kantig, das ist einfach nur erbärmlich. Lass den Scheiß stecken oder lass es hier ganz.",
    "Sag mal {name}, hat dir das jemand ins Ohr geflüstert oder bist du selbst so drauf? Egal – so redet hier keiner, aus.",
    "{name}, du bist der Beweis, dass Reichweite und Verstand nichts miteinander zu tun haben. Solche Sprüche haben hier nichts verloren.",
    "Peinlich, {name}. Wirklich. Und nein, das ist kein Humor – das ist einfach nur Hetze und die bleibt hier draußen.",
]

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


class Fun(FeatureBasis):
    """Kapselt das Chaos-Feature (Befehle, Reactions, Einwuerfe) als Klasse."""

    def __init__(self):
        self._enabled = False
        self._last_interject = 0.0
        self._last_botroast = 0.0
        self._last_dmroast = 0.0       # serverweiter Cooldown fuer den DM-Konter
        self._dm_cooldowns = {}        # uid -> letzter DM-Konter (pro Person)
        self._last_gegenrede = 0.0     # serverweiter Cooldown fuer die Gegenrede
        self._gegenrede_cooldowns = {}  # uid -> letzte Gegenrede (pro Person)

    def _looks_like_refusal(self, text, was="Roast"):
        """Hat das Modell gekniffen statt zu roasten?

        Wird das still abgefangen, sieht der Server nur einen von fuenf
        Fertig-Spruechen und haelt Flo fuer langweilig geworden - genau so ist
        es passiert. Deshalb steht jede Verweigerung ab jetzt im Log und damit
        in 'bash k'."""
        if not text or not _REFUSAL_RE.search(text):
            return False
        log.warning("KI-Fehler: %s verweigert (Modell zu zahm) - nehme einen "
                    "Fertig-Spruch. Antwort war: %r", was,
                    text.replace("\n", " ")[:120])
        return True

    def setup(self):
        """Aktiv, wenn die KI laeuft (Roast/Hype/Spruch brauchen das LLM)."""
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
        echte = [u for u in echte_erwaehnungen(message) if u.id != me_id]
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
            # 'rate' ist im Deutschen auch der Imperativ von RATEN: "Flo rate mal
            # wer gewonnen hat" wurde sonst zur Vibe-Note fuer den Satzrest als
            # angeblichen Personennamen. Ohne Erwaehnung zaehlt es nur, wenn gar
            # kein Ziel dahinter steht (oder 'mich'). Die eindeutigen
            # Slang-Trigger (rizz, sigma, aura ...) bleiben, wie sie sind.
            if (first in ("rate", "bewerte") and not message.mentions
                    and rest.lower().strip(" .,!?") not in ("", "mich", "me", "self")):
                return None
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

    # --- DM-Konter: Flo beleidigt Poebler privat zurueck ---------------------
    def looks_offensive(self, content):
        """Grobe Erkennung: enthaelt die Nachricht Beleidigungen / 'random Scheiss'?

        Eindeutige Woerter zaehlen ueberall, mehrdeutige nur mit direkter
        Anrede - sonst wird aus "der Ball im Maul des Hundes" eine
        beleidigende DM an jemanden, der nur ueber seinen Hund geredet hat."""
        if not content:
            return False
        return bool(_INSULT_RE.search(content)) or bool(_INSULT_ANREDE_RE.search(content))

    async def maybe_dm_roast(self, message):
        """Wer im Chat nur Beleidigungen/Muell raushaut, bekommt GANZ SELTEN von Flo
        privat (DM) einen frechen Konter zurueck. Passiver Hook (bot.py, nebenher).
        Dreifach gebremst: kleine Chance + Cooldown pro Person + serverweiter
        Cooldown - damit es wirklich nur ab und zu passiert."""
        if not self._enabled or message.guild is None:
            return
        author = message.author
        if getattr(author, "bot", False):
            return
        content = message.content or ""
        if not self.looks_offensive(content):
            return
        now = time.monotonic()
        if now - self._last_dmroast < DMROAST_GLOBAL_COOLDOWN:
            return
        if now - self._dm_cooldowns.get(author.id, 0.0) < DMROAST_USER_COOLDOWN:
            return
        if random.random() >= DMROAST_CHANCE:
            return
        # Ab hier feuern wir -> beide Cooldowns setzen (auch wenn die DM scheitert,
        # damit kein wiederholter Versuch am selben Poebler haengen bleibt).
        self._last_dmroast = now
        self._dm_cooldowns[author.id] = now
        name = getattr(author, "display_name", "") or "du"
        text = await self._dm_roast_text(name, content)
        try:
            await author.send(text)
            log.info("DM-Konter an %s (Beleidigung im Chat).", name)
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs zu / nicht erreichbar -> einfach schlucken

    async def _dm_roast_text(self, name, content):
        """Baut den DM-Konter: frisch von der KI, sonst ein Fertig-Spruch."""
        if ai.is_enabled():
            system = (
                f"Du bist {self._bot_name}, ein frecher Discord-Bot mit losem Mundwerk. "
                f"{name} hat gerade im Server-Chat nur Beleidigungen oder Muell rausgehauen. "
                "Schreib ihm PRIVAT genau EINEN kurzen, frechen, schlagfertigen deutschen "
                "Konter, der ihn zurueck aufzieht - so wie Kumpels sich derbe anmachen. "
                "KEINE Slurs, kein echtes Hate-Speech, keine Moralpredigt, keine Emojis - "
                "nur trockener, frecher Spott."
            )
            try:
                out = await ai.generate(f"{name} schrieb: {content[:200]}",
                                        system=system, temperature=1.15, max_tokens=60)
            except Exception:  # noqa: BLE001 - KI-Fehler faellt auf den Pool zurueck
                out = None
            if out and not self._looks_like_refusal(out):
                return out.strip()
        return random.choice(_DM_ROASTS).format(name=name)

    # --- Gegenrede: Flo haelt oeffentlich dagegen ----------------------------
    def ist_hetze(self, content):
        """Steht in dieser Nachricht menschenfeindlicher Muell?

        Drei Stufen, und die dritte ist die wichtigste:
        1. eindeutige Begriffe, die niemand harmlos benutzt,
        2. Gruppe + Urteil + Entmenschlichung (eine Gruppe zu NENNEN reicht nie),
        3. die Meta-Ausnahme: wer UEBER so etwas redet, es zitiert, es meldet
           oder dagegen ist, wird nicht angegangen.

        Ohne (3) haette Flo ausgerechnet die angemacht, die sich beschweren -
        und eine oeffentliche Zurechtweisung, die danebenliegt, ist schlimmer
        als gar keine. Im Zweifel also: False."""
        if not content:
            return False
        treffer = (_HETZE_RE.search(content) or _HETZE_URTEIL_RE.search(content)
                   or _HETZE_PAROLE_RE.search(content)
                   or _HETZE_LEUGNUNG_RE.search(content))
        if not treffer:
            return False
        # Erst jetzt die teureren Ausnahmen - sie laufen nur im Verdachtsfall.
        if _ZITAT_RE.search(content) or _META_RE.search(content):
            return False
        return True

    async def maybe_gegenrede(self, message):
        """Schreibt jemand menschenfeindlichen Muell, geht Flo OEFFENTLICH auf
        den los, der es geschrieben hat - mit Spruch und einer klaren Ansage.

        Anders als der DM-Konter wird hier nicht gewuerfelt: sie soll wirken.
        Gebremst wird nur gegen Flut (Cooldown pro Person + serverweit).
        Rueckgabe True, wenn geantwortet wurde - dann ist die Nachricht fuer den
        Rest des passiven Hooks erledigt."""
        if not self._enabled or message.guild is None:
            return False
        author = message.author
        if getattr(author, "bot", False):
            return False
        if not self.ist_hetze(message.content or ""):
            return False
        now = time.monotonic()
        if now - self._last_gegenrede < GEGENREDE_GLOBAL_COOLDOWN:
            return False
        if now - self._gegenrede_cooldowns.get(author.id, 0.0) < GEGENREDE_USER_COOLDOWN:
            return False
        # Cooldowns VOR dem Senden setzen: scheitert das Senden, soll Flo nicht
        # bei jeder weiteren Nachricht desselben Poeblers neu ansetzen.
        self._last_gegenrede = now
        self._gegenrede_cooldowns[author.id] = now
        name = getattr(author, "display_name", "") or "du"
        text = await self._gegenrede_text(name)
        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException:
            # Nachricht schon geloescht -> die Ansage trotzdem in den Kanal.
            try:
                await message.channel.send(text)
            except discord.HTTPException:
                return False
        log.info("Gegenrede an %s in #%s.", name,
                 getattr(message.channel, "name", "?"))
        return True

    async def _gegenrede_text(self, name):
        """Baut die Gegenrede: frisch von der KI, sonst ein Fertig-Spruch.

        ai.generate laeuft hier OHNE Persona und OHNE Guardrail (system= ersetzt
        beides) - der Prompt muss seine Grenzen also selbst mitbringen. Und
        ausdruecklich: das Wort nicht wiederholen. Flo antwortet auf den
        Menschen, nicht auf den Dreck."""
        if ai.is_enabled():
            system = (
                f"Du bist {self._bot_name}, ein frecher deutscher Discord-Bot mit losem "
                f"Mundwerk. {name} hat gerade menschenfeindlichen Muell in den Chat "
                "geschrieben - Hetze gegen eine ganze Menschengruppe. Du bist DAGEGEN "
                "und machst das klar. Antworte in GENAU ZWEI kurzen deutschen Saetzen: "
                "erst ein derber, spoettischer Spruch, der DEN SCHREIBER klein macht, "
                "dann eine klare Ansage, dass so etwas hier nicht laeuft. Zieh NIEMALS "
                "ueber die Gruppe her, ueber die er hergezogen ist - dein Ziel ist "
                "ausschliesslich er. Wiederhole seine Woerter nicht und zitiere ihn "
                "nicht. Keine Emojis, keine Moralpredigt, kein Vorwort, keine "
                "Aufzaehlung - nur die zwei Saetze."
            )
            try:
                out = await ai.generate(
                    f"{name} hat Hetze in den Chat geschrieben. Mach ihn fertig "
                    f"und sag, dass das hier nicht laeuft.",
                    system=system, temperature=1.1, max_tokens=90)
            except Exception:  # noqa: BLE001 - KI-Fehler faellt auf den Pool zurueck
                out = None
            if out and not self._looks_like_refusal(out, was="Gegenrede"):
                return out.strip()
        return random.choice(_GEGENREDE).format(name=name)

    # --- Passiver Hook: Reactions & Einwuerfe --------------------------------
    async def on_message_passive(self, message):
        """Reagiert selten/zufaellig auf eine Nachricht (Emoji + ganz selten ein
        kurzer KI-Einwurf). Wird in bot.py fuer Nicht-Bot-Nachrichten aufgerufen."""
        if not self._enabled or message.guild is None:
            return
        content = message.content or ""

        # 0a) Menschenfeindlicher Muell -> Flo haelt OEFFENTLICH dagegen. Die
        #     einzige Stelle hier, an der er zuverlaessig antwortet statt zu
        #     wuerfeln. Feuert sie, ist die Nachricht erledigt: ein zusaetzliches
        #     Zufalls-Emoji oder ein Kalauer daneben verwaessert die Ansage nur.
        if await self.maybe_gegenrede(message):
            return

        # 0) Ganz selten: wer nur Beleidigungen/Muell in den Chat rotzt, kriegt von
        #    Flo privat (DM) einen frechen Konter zurueck (Chance + Cooldowns intern).
        await self.maybe_dm_roast(message)

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
maybe_dm_roast = instance.maybe_dm_roast
looks_offensive = instance.looks_offensive
ist_hetze = instance.ist_hetze
maybe_gegenrede = instance.maybe_gegenrede
