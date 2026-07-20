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

import hashlib
import random


class Titles:
    """Objektorientierte Kapselung des Titel-Pools (Verhalten identisch zur
    frueheren Modul-Fassung; Modul-Aliase siehe Dateiende)."""

    # --- Seltenheits-Metadaten ----------------------------------------------
    # pool_pct: Anteil ALLER Titel in dieser Stufe (mehr normal als legendary).
    # shop_weight: Gewicht bei der taeglichen Shop-Auswahl (gleiche Tendenz).
    # tone: wie Flo mit Traegern dieser Stufe spricht (ai.py liest das).
    RARITY = {
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

    def __init__(self):
        # --- Titel-Pool (lazy gebaut, dann gecacht) ------------------------------
        self._POOL = None   # rarity -> [titel, ...]
        self._ALL = None               # alle Titel (flach)

    # --- Hash-Helfer (deterministische Eigenschaften je Titel) ---------------
    def _h(self, text, salt):
        digest = hashlib.sha256(f"{salt}|{text}".encode("utf-8")).hexdigest()
        return int(digest[:12], 16)

    def rarity_of(self, text):
        """Feste Seltenheit eines Titels (per Hash, Verteilung via pool_pct)."""
        r = self._h(text, "rarity") % 100
        if r < 62:
            return "normal"      # 62 %
        if r < 88:
            return "selten"      # 26 %
        if r < 97:
            return "mythisch"    # 9 %
        return "legendary"       # 3 %

    def price_of(self, text):
        """Fester Preis (deterministisch in der Preisspanne der Stufe, auf 10 gerundet)."""
        lo, hi = self.RARITY[self.rarity_of(text)]["price"]
        steps = (hi - lo) // 10
        return lo + (self._h(text, "price") % (steps + 1)) * 10

    def emoji_of(self, text):
        """Themen-Emoji des Titels (deterministisch)."""
        bank = self._EMOJI[self.rarity_of(text)]
        return bank[self._h(text, "emoji") % len(bank)]

    def label_of(self, text):
        """Anzeigename inkl. Emoji, z. B. '👑 Goldener König'."""
        return f"{self.emoji_of(text)} {text}"

    def color_of(self, text):
        return self.RARITY[self.rarity_of(text)]["color"]

    def entry(self, text):
        """Vollstaendiger Datensatz zu einem Titel."""
        rar = self.rarity_of(text)
        meta = self.RARITY[rar]
        return {
            "text": text,
            "label": self.label_of(text),
            "emoji": self.emoji_of(text),
            "rarity": rar,
            "rarity_label": meta["label"],
            "price": self.price_of(text),
            "color": meta["color"],
            "role": meta["role"],
        }

    def _generate(self):
        """Erzeugt ALLE Titel aus den Templates (deterministisch, ohne Duplikate)."""
        out = []
        seen = set()
        for adj in self._ADJ:                  # Template 1: 'Adj Noun'
            for noun in self._NOUN:
                t = f"{adj} {noun}"
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        for noun in self._NOUN:                # Template 2: 'Noun des X'
            for gen in self._GEN:
                t = f"{noun} des {gen}"
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        for noun in self._NOUN:                # Template 3: 'Noun von Ort'
            for place in self._PLACE:
                t = f"{noun} von {place}"
                if t not in seen:
                    seen.add(t)
                    out.append(t)
        return out

    def _build(self):
        if self._POOL is not None:
            return
        self._ALL = self._generate()
        pool = {r: [] for r in self.RARITY_ORDER}
        for t in self._ALL:
            pool[self.rarity_of(t)].append(t)
        self._POOL = pool

    def pool(self):
        self._build()
        assert self._POOL is not None
        return self._POOL

    def total(self):
        self._build()
        assert self._ALL is not None
        return len(self._ALL)

    def counts(self):
        """Anzahl Titel je Seltenheit (fuer Diagnose/Tests)."""
        return {r: len(v) for r, v in self.pool().items()}

    # --- Tagesauswahl fuer den Shop -----------------------------------------
    def random_titles(self, n, *, rng = None,
                       exclude = None):
        """Waehlt n verschiedene Titel fuer den Shop – seltenheits-gewichtet
        (mehr normale, selten mal ein legendaerer). Gibt entry()-Dicts zurueck."""
        rng = rng or random
        exclude = set(exclude or ())
        p = self.pool()
        rarities = self.RARITY_ORDER
        weights = [self.RARITY[r]["shop_weight"] for r in rarities]
        picked = []
        picked_set = set()
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
        return [self.entry(t) for t in picked]


# --- Modul-Instanz + Aliase (economy.py & Co. nutzen weiter die alten Namen) --
instance = Titles()

# Konstanten
RARITY = Titles.RARITY
RARITY_ORDER = Titles.RARITY_ORDER
RANK = Titles.RANK
_EMOJI = Titles._EMOJI
_ADJ = Titles._ADJ
_NOUN = Titles._NOUN
_GEN = Titles._GEN
_PLACE = Titles._PLACE

# Funktionen
_h = instance._h
rarity_of = instance.rarity_of
price_of = instance.price_of
emoji_of = instance.emoji_of
label_of = instance.label_of
color_of = instance.color_of
entry = instance.entry
pool = instance.pool
total = instance.total
counts = instance.counts
random_titles = instance.random_titles
