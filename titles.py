"""Titel-Pool nach Seltenheit (v1.2).

Erzeugt DETERMINISTISCH zehntausende Titel aus Wortbaenken + Templates. Jeder
Titel hat eine feste Seltenheit (per Hash), feste Farbe/Emoji/Preis – egal wann
oder wo er nachgeschlagen wird. Dadurch ist der taegliche Shop reproduzierbar
und fair.

Seltenheitsstufen (vom Haeufigsten zum Seltensten):
    normal     -> gruen   🟢   (am meisten)
    selten     -> blau    🔵
    mythisch   -> lila    🟣
    legendary  -> gold    🟡   (am seltensten)

Umso seltener ein Titel, desto teurer ist er – und desto entspannter spricht
Flo mit dem Traeger (siehe 'tone'). Andere Module nutzen:
    rarity_of(text), price_of(text), entry(text), random_titles(n)
und die Metadaten in RARITY.
"""
from __future__ import annotations

import hashlib
import random

# --- Seltenheits-Metadaten ----------------------------------------------
# pool_pct: Anteil ALLER Titel in dieser Stufe (mehr normal als legendary).
# shop_weight: Gewicht bei der taeglichen Shop-Auswahl (gleiche Tendenz).
# tone: wie Flo mit Traegern dieser Stufe spricht (ai.py liest das).
RARITY: dict[str, dict] = {
    "normal": {
        "label": "Normal", "emoji": "🟢", "color": 0x57F287,
        "role": "Flo · Normal", "price": (150, 600),
        "pool_pct": 62, "shop_weight": 55,
        "tone": ("Der Typ hat nur einen normalen Titel – also null Sonderbehandlung. "
                 "Gib ihm die volle ungefilterte Packung, roaste ihn gnadenlos und "
                 "frech wie jeden anderen Pleitegeier auch."),
    },
    "selten": {
        "label": "Selten", "emoji": "🔵", "color": 0x3498DB,
        "role": "Flo · Selten", "price": (700, 1800),
        "pool_pct": 26, "shop_weight": 30,
        "tone": ("Er traegt einen seltenen Titel – fahr die Aggression einen Tick "
                 "runter. Roaste ruhig noch, aber mit Augenzwinkern statt voller "
                 "Breitseite, einen Hauch freundlicher und entspannter."),
    },
    "mythisch": {
        "label": "Mythisch", "emoji": "🟣", "color": 0x9B59B6,
        "role": "Flo · Mythisch", "price": (2200, 5000),
        "pool_pct": 9, "shop_weight": 12,
        "tone": ("Er traegt einen MYTHISCHEN Titel – jetzt wird's deutlich "
                 "freundlicher. Behandle ihn wie einen guten Kumpel: noch frech und "
                 "locker, aber warm, respektvoll und chillig, das fiese Roasten "
                 "laesst du grossteils weg."),
    },
    "legendary": {
        "label": "Legendär", "emoji": "🟡", "color": 0xF1C40F,
        "role": "Flo · Legendär", "price": (6000, 15000),
        "pool_pct": 3, "shop_weight": 3,
        "tone": ("Er traegt einen LEGENDAEREN Titel – das ist quasi dein bester "
                 "Freund. Leg den ganzen Aggro-Modus komplett ab und sei richtig "
                 "herzlich, entspannt, geduldig und unterstuetzend. Kein Roasten, "
                 "keine fiesen Sprueche – rede liebevoll und chillig mit ihm wie mit "
                 "einem alten Freund, den du ueber alles schaetzt."),
    },
}

# Reihenfolge / Rang (groesser = seltener) – fuer 'hoechste besessene Stufe'.
RARITY_ORDER = ["normal", "selten", "mythisch", "legendary"]
RANK = {r: i for i, r in enumerate(RARITY_ORDER)}

# Themen-Emojis je Stufe (deterministisch ausgewaehlt) – reine Optik.
_EMOJI = {
    "normal":    ["🌿", "🍀", "🌱", "🔰", "🧩", "🎈", "☘️", "🪶"],
    "selten":    ["🔵", "💧", "🌀", "❄️", "🐬", "🛡️", "🔷", "🌊"],
    "mythisch":  ["🟣", "🔮", "🌌", "🦄", "👾", "🪄", "🧿", "🌠"],
    "legendary": ["👑", "✨", "🔥", "💎", "🐉", "🏆", "⚡", "🌟"],
}

# --- Wortbaenke (fuer die Titel-Generierung) ----------------------------
_ADJ = [
    "Eisiger", "Glühender", "Dunkler", "Strahlender", "Wilder", "Stiller",
    "Uralter", "Heiliger", "Verfluchter", "Goldener", "Silberner", "Eiserner",
    "Wütender", "Sanfter", "Listiger", "Mächtiger", "Flinker", "Schattiger",
    "Leuchtender", "Frostiger", "Stürmischer", "Donnernder", "Lautloser",
    "Ewiger", "Verlorener", "Kühner", "Edler", "Roher", "Zorniger", "Weiser",
    "Blutiger", "Nebliger", "Funkelnder", "Rasender", "Träumender",
    "Wandernder", "Brennender", "Gefallener", "Erhabener", "Verborgener",
    "Tobender", "Schweigender", "Glänzender", "Klirrender", "Wachsamer",
    "Heulender", "Grollender", "Reißender", "Schimmernder", "Knisternder",
    "Lodernder", "Stählerner", "Kristallener", "Rubinroter", "Saphirblauer",
    "Smaragdgrüner", "Obsidianschwarzer", "Nebelgrauer", "Mondheller",
    "Sternenklarer", "Endloser", "Namenloser", "Furchtloser", "Gnadenloser",
    "Zeitloser", "Schlafloser", "Ruheloser", "Grenzenloser", "Herzloser",
    "Eisenharter", "Zahmer", "Scheuer", "Frecher", "Kecker", "Dreister",
    "Mutiger", "Tapferer", "Schlauer", "Cleverer", "Treuer", "Falscher",
    "Reiner", "Zarter", "Harter", "Bitterer", "Süßer", "Scharfer", "Milder",
    "Heißer", "Kalter", "Finsterer", "Lichter", "Schneller", "Zäher",
    "Wuchtiger", "Geschmeidiger", "Unsterblicher", "Vergessener", "Geheimer",
    "Königlicher", "Kaiserlicher", "Teuflischer", "Engelhafter", "Wölfischer",
    "Bärenstarker", "Adlerscharfer", "Fuchsschlauer", "Schlangengleicher",
    "Sturmgeborener",
    "Gebufffter", "Ungepatchter", "Lagfreier", "Overpowerter", "Nervloser",
    "Koffeinierter", "Ungechillter", "Durchgedrehter", "Legendärer",
    "Verpixelter", "Gecarrryter", "Tiltloser", "Cracked-out", "Sagenhafter",
    "Mythischer", "Kosmischer", "Galaktischer", "Interdimensionaler",
    "Allwissender", "Unbesiegter",
]

_NOUN = [
    "Wolf", "Drache", "König", "Krieger", "Jäger", "Geist", "Schatten",
    "Sturm", "Fürst", "Ritter", "Wächter", "Titan", "Dämon", "Engel",
    "Reiter", "Schmied", "Wanderer", "Herrscher", "Magier", "Berserker",
    "Pirat", "Wikinger", "Samurai", "Ninja", "Gladiator", "Barbar", "Druide",
    "Schamane", "Hexer", "Paladin", "Templer", "Söldner", "Räuber", "Bandit",
    "Schurke", "Held", "Henker", "Schnitter", "Bezwinger", "Eroberer",
    "Bewahrer", "Hüter", "Späher", "Kundschafter", "Bote", "Pilger", "Mönch",
    "Prophet", "Seher", "Orakel", "Alchemist", "Gelehrter", "Meister",
    "Lehrling", "Novize", "Champion", "Veteran", "Rekrut", "Hauptmann",
    "General", "Marschall", "Admiral", "Kommandant", "Anführer", "Häuptling",
    "Kaiser", "Baron", "Graf", "Herzog", "Prinz", "Thronfolger", "Recke",
    "Kämpe", "Streiter", "Verteidiger", "Angreifer", "Schwertmeister",
    "Bogenschütze", "Speerträger", "Axtkämpfer", "Schildträger",
    "Klingenmeister", "Sturmreiter", "Schattenläufer", "Nachtjäger",
    "Geisterseher", "Drachentöter", "Riesentöter", "Dämonenjäger",
    "Wolfsbruder", "Bärenfänger", "Falkner", "Falke", "Rabe", "Adler",
    "Löwe", "Tiger", "Panther", "Bär", "Fuchs", "Luchs", "Hai", "Phönix",
    "Greif", "Basilisk", "Golem", "Koloss", "Wyvern", "Lindwurm",
    "Höllenhund", "Schreckgespenst", "Nachtmahr", "Wirbelwind", "Donnerkeil",
    "Blitzschlag", "Feuersturm", "Frostriese", "Steinwächter", "Schwarmgeist",
    "Sigma", "Gigachad", "Hauptcharakter", "NPC-Flüsterer", "Lootgoblin",
    "Speedrunner", "Clutchgott", "Carry", "Smurf", "Grinder", "Sweat",
    "Bosskiller", "Endgegner", "Miniboss", "Tutorialboss", "Weltenfresser",
    "Kaffeetrinker", "Nachtschichtler", "Snackwächter", "Couchkommandant",
    "Pixelkrieger", "Tastenschreck", "Mausakrobat", "Serverfürst",
    "Voicechat-Tyrann", "Memelord", "Ratiokönig", "Cringeverwalter",
    "Lachflash", "Ehrenmann",
]

_GEN = [
    "Schicksals", "Chaos", "Nordens", "Südens", "Ostens", "Westens",
    "Abgrunds", "Sturms", "Feuers", "Eises", "Donners", "Mondes", "Todes",
    "Lichts", "Schattens", "Zwielichts", "Wahnsinns", "Krieges", "Friedens",
    "Zorns", "Traums", "Albtraums", "Himmels", "Untergangs", "Aufbruchs",
    "Verderbens", "Olymps", "Blutes", "Stahls", "Goldes", "Silbers",
    "Kristalls", "Nebels", "Frostes", "Sieges", "Ruhms", "Ruins", "Verfalls",
    "Erwachens", "Vergessens", "Anfangs", "Endes", "Jenseits", "Ursprungs",
    "Schwurs",
]

_PLACE = [
    "Valoria", "Nordheim", "Drakenfels", "Schattenmoor", "Eisenwall",
    "Sturmkap", "Frostheim", "Glutland", "Nebeltal", "Sonnenstein",
    "Mondfels", "Sternenfels", "Wolkenstein", "Donnerberg", "Aschenfeld",
    "Rabenhorst", "Wolfsstein", "Eichwald", "Dornenwald", "Silberquell",
    "Goldhafen", "Schwarzwasser", "Rotfurt", "Graustein", "Weißenfels",
    "Blauenstein", "Grünmark", "Wildmark", "Ödland", "Geisterhain",
    "Drachenhort", "Titanenfeste", "Himmelsrand", "Abgrundtor", "Nimmerland",
    "Lagland", "Spawnpoint", "Endzone", "Bugwiese", "Serverraum",
    "Voicetal", "Memehausen", "Clutchhausen", "Tiltberg", "Ragequit-Furt",
]


# --- Hash-Helfer (deterministische Eigenschaften je Titel) ---------------
def _h(text: str, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}|{text}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def rarity_of(text: str) -> str:
    """Feste Seltenheit eines Titels (per Hash, Verteilung via pool_pct)."""
    r = _h(text, "rarity") % 100
    if r < 62:
        return "normal"      # 62 %
    if r < 88:
        return "selten"      # 26 %
    if r < 97:
        return "mythisch"    # 9 %
    return "legendary"       # 3 %


def price_of(text: str) -> int:
    """Fester Preis (deterministisch in der Preisspanne der Stufe, auf 10 gerundet)."""
    lo, hi = RARITY[rarity_of(text)]["price"]
    steps = (hi - lo) // 10
    return lo + (_h(text, "price") % (steps + 1)) * 10


def emoji_of(text: str) -> str:
    """Themen-Emoji des Titels (deterministisch)."""
    bank = _EMOJI[rarity_of(text)]
    return bank[_h(text, "emoji") % len(bank)]


def label_of(text: str) -> str:
    """Anzeigename inkl. Emoji, z. B. '👑 Goldener König'."""
    return f"{emoji_of(text)} {text}"


def color_of(text: str) -> int:
    return RARITY[rarity_of(text)]["color"]


def entry(text: str) -> dict:
    """Vollstaendiger Datensatz zu einem Titel."""
    rar = rarity_of(text)
    meta = RARITY[rar]
    return {
        "text": text,
        "label": label_of(text),
        "emoji": emoji_of(text),
        "rarity": rar,
        "rarity_label": meta["label"],
        "price": price_of(text),
        "color": meta["color"],
        "role": meta["role"],
    }


# --- Titel-Pool (lazy gebaut, dann gecacht) ------------------------------
_POOL: dict[str, list[str]] | None = None   # rarity -> [titel, ...]
_ALL: list[str] | None = None               # alle Titel (flach)


def _generate() -> list[str]:
    """Erzeugt ALLE Titel aus den Templates (deterministisch, ohne Duplikate)."""
    out: list[str] = []
    seen: set[str] = set()
    for adj in _ADJ:                       # Template 1: 'Adj Noun'
        for noun in _NOUN:
            t = f"{adj} {noun}"
            if t not in seen:
                seen.add(t)
                out.append(t)
    for noun in _NOUN:                     # Template 2: 'Noun des X'
        for gen in _GEN:
            t = f"{noun} des {gen}"
            if t not in seen:
                seen.add(t)
                out.append(t)
    for noun in _NOUN:                     # Template 3: 'Noun von Ort'
        for place in _PLACE:
            t = f"{noun} von {place}"
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _build() -> None:
    global _POOL, _ALL
    if _POOL is not None:
        return
    _ALL = _generate()
    pool: dict[str, list[str]] = {r: [] for r in RARITY_ORDER}
    for t in _ALL:
        pool[rarity_of(t)].append(t)
    _POOL = pool


def pool() -> dict[str, list[str]]:
    _build()
    assert _POOL is not None
    return _POOL


def total() -> int:
    _build()
    assert _ALL is not None
    return len(_ALL)


def counts() -> dict[str, int]:
    """Anzahl Titel je Seltenheit (fuer Diagnose/Tests)."""
    return {r: len(v) for r, v in pool().items()}


# --- Tagesauswahl fuer den Shop -----------------------------------------
def random_titles(n: int, *, rng: random.Random | None = None,
                   exclude: set[str] | None = None) -> list[dict]:
    """Waehlt n verschiedene Titel fuer den Shop – seltenheits-gewichtet
    (mehr normale, selten mal ein legendaerer). Gibt entry()-Dicts zurueck."""
    rng = rng or random
    exclude = set(exclude or ())
    p = pool()
    rarities = RARITY_ORDER
    weights = [RARITY[r]["shop_weight"] for r in rarities]
    picked: list[str] = []
    picked_set: set[str] = set()
    guard = 0
    while len(picked) < n and guard < n * 60:
        guard += 1
        rar = rng.choices(rarities, weights=weights, k=1)[0]
        bucket = p[rar]
        if not bucket:
            continue
        cand = rng.choice(bucket)
        if cand in picked_set or cand in exclude:
            continue
        picked_set.add(cand)
        picked.append(cand)
    return [entry(t) for t in picked]
