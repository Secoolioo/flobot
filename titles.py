"""Titel-Pool nach Seltenheit (v1.2).

Erzeugt DETERMINISTISCH zehntausende Titel aus Wortbaenken + Templates. Jeder
Titel hat eine feste Seltenheit (per Hash), feste Farbe/Emoji/Preis – egal wann
oder wo er nachgeschlagen wird. Dadurch ist der taegliche Shop reproduzierbar
und fair.

Seltenheitsstufen (vom Haeufigsten zum Seltensten):
    normal     -> gruen     🟢   (am meisten)
    selten     -> blau      🔵
    episch     -> violett   🟪
    mythisch   -> magenta   🟣
    legendary  -> gold      🟡
    relikt     -> orange    🟠   (die Spitze des Tages-Shops, sehr selten)
    exklusiv   -> rot       🔱   (NUR beim fahrenden Haendler)
    goettlich  -> cyan      ✨   (Haendler-Spitze, das absolute Maximum)

Jede Stufe hat eine eigene FARBROLLE im Discord (economy.ensure_roles legt sie
an und faerbt sie nach - auch nachtraeglich, wenn eine Farbe hier geaendert wird).

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
    # PREISE: abgeleitet aus dem gemessenen Tageseinkommen (economy.py: rund
    # 8.000-10.000 Coins an einem normal aktiven Tag - reden, im Call sitzen,
    # Tagesbonus, Level-Ups). Daraus die Leiter:
    #
    #   Normal      500 -  2.000   -> am ERSTEN Tag drin (Einstieg)
    #   Selten    3.000 -  9.000   -> Tag 1-2
    #   Mythisch 18.000 - 45.000   -> erste Woche
    #   Legendär 90.000 -220.000   -> Woche 2-4
    #   Exklusiv 450.000-900.000   -> nur beim Haendler, Monat 1-2 (oder EIN
    #                                 richtig guter Aktien-Vormittag)
    #
    # Vorher lagen alle Stufen zwischen 150 und 15.000: der komplette Titel-Shop
    # war nach zwei Tagen abgeraeumt und danach bedeutungslos.
    RARITY = {
        "normal": {
            "label": "Gewöhnlich", "emoji": "🟢", "color": 0x57F287,
            "role": "Flo · Gewöhnlich", "price": (500, 2000),
            "pool_pct": 46, "shop_weight": 40,
            # Ein GEKAUFTER Titel darf niemals schlechter sein als gar keiner.
            # Vorher stand hier "roaste ihn gnadenlos" - ohne Titel ist der Ton
            # dagegen leer (= normal). Wer sich also den Einstiegstitel kaufte,
            # zahlte Coins dafuer, dass Flo GEMEINER mit ihm wurde.
            "tone": ("Er hat sich immerhin schon einen Titel gekauft – behandle ihn wie "
                     "einen von den Stammgaesten. Frech und respektlos wie immer, aber "
                     "mit einem Hauch Anerkennung, dass er ueberhaupt angefangen hat."),
        },
        "selten": {
            "label": "Selten", "emoji": "🔵", "color": 0x3498DB,
            "role": "Flo · Selten", "price": (3000, 9000),
            "pool_pct": 24, "shop_weight": 28,
            "tone": ("Er traegt einen seltenen Titel – fahr die Aggression einen Tick "
                     "runter. Roaste ruhig noch, aber mit Augenzwinkern statt voller "
                     "Breitseite, einen Hauch freundlicher und entspannter."),
        },
        "episch": {
            "label": "Episch", "emoji": "🟪", "color": 0x7C5CFF,
            "role": "Flo · Episch", "price": (15_000, 40_000),
            "pool_pct": 16, "shop_weight": 19,
            "tone": ("Er traegt einen EPISCHEN Titel – der Typ hat sichtbar Zeit und "
                     "Coins investiert. Bleib frech, aber auf Augenhoehe: kumpelhafte "
                     "Sprueche statt Herabsetzung, und lass durchblicken, dass du das "
                     "durchaus respektierst."),
        },
        "mythisch": {
            "label": "Mythisch", "emoji": "🟣", "color": 0xC026D3,
            "role": "Flo · Mythisch", "price": (60_000, 150_000),
            "pool_pct": 9, "shop_weight": 9,
            "tone": ("Er traegt einen MYTHISCHEN Titel – jetzt wird's deutlich "
                     "freundlicher. Behandle ihn wie einen guten Kumpel: noch frech und "
                     "locker, aber warm, respektvoll und chillig, das fiese Roasten "
                     "laesst du grossteils weg."),
        },
        "legendary": {
            "label": "Legendär", "emoji": "🟡", "color": 0xF1C40F,
            "role": "Flo · Legendär", "price": (300_000, 800_000),
            "pool_pct": 4, "shop_weight": 3,
            "tone": ("Er traegt einen LEGENDAEREN Titel – das ist quasi dein bester "
                     "Freund. Leg den ganzen Aggro-Modus komplett ab und sei richtig "
                     "herzlich, entspannt, geduldig und unterstuetzend. Kein Roasten, "
                     "keine fiesen Sprueche – rede liebevoll und chillig mit ihm wie mit "
                     "einem alten Freund, den du ueber alles schaetzt."),
        },
        # RELIKT: die Spitze des TAGES-Shops. Nur 1 % aller Titel und Gewicht 1 -
        # im Schnitt taucht alle paar Wochen mal eines in den acht Slots auf.
        "relikt": {
            "label": "Relikt", "emoji": "🟠", "color": 0xFF7A18,
            "role": "Flo · Relikt", "price": (1_500_000, 4_000_000),
            "pool_pct": 1, "shop_weight": 1,
            "tone": ("Er traegt ein RELIKT – so etwas sieht man im Shop fast nie, das "
                     "sind Millionen in einem Namen. Rede mit ihm wie mit einem alten "
                     "Meister: respektvoll, aufmerksam, ein bisschen ehrfuerchtig. "
                     "Spott hat hier nichts zu suchen."),
        },
        # EXKLUSIV: NUR beim fahrenden Haendler (pool_pct 0 + shop_weight 0 ->
        # rarity_of vergibt die Stufe nie, sie kann also nie im Tages-Shop landen).
        "exklusiv": {
            "label": "Exklusiv", "emoji": "🔱", "color": 0xFF2D55,
            "role": "Flo · Exklusiv", "price": (6_000_000, 15_000_000),
            "pool_pct": 0, "shop_weight": 0,
            "tone": ("Er traegt einen EXKLUSIVEN Haendler-Titel – so etwas gibt es im "
                     "Shop NIEMALS. Behandle ihn wie eine lebende Legende und dein "
                     "Idol: voller Ehrfurcht, Bewunderung und Respekt. Kein Fuenkchen "
                     "Spott – rede zu ihm auf, als waerst du geehrt, ueberhaupt mit ihm "
                     "reden zu duerfen."),
        },
        # GOETTLICH: das absolute Maximum. Der Haendler hat davon nur ganz selten
        # ueberhaupt eines im Angebot.
        "goettlich": {
            "label": "Göttlich", "emoji": "✨", "color": 0x00E5FF,
            "role": "Flo · Göttlich", "price": (40_000_000, 90_000_000),
            "pool_pct": 0, "shop_weight": 0,
            "tone": ("Er traegt einen GOETTLICHEN Titel – die hoechste Stufe, die es "
                     "gibt, zig Millionen Coins in einem Namen. Rede mit ihm, als "
                     "sprichst du mit einer Gottheit: demuetig, feierlich, ehrfuerchtig. "
                     "Erwaehne ruhig, dass du kaum glauben kannst, dass er dir "
                     "ueberhaupt antwortet. Absolut kein Spott."),
        },
    }

    # Reihenfolge / Rang (groesser = seltener) – fuer 'hoechste besessene Stufe'.
    # 'exklusiv' steht ganz oben (hoechster Rang) -> haengt Legendaer ab.
    RARITY_ORDER = ["normal", "selten", "episch", "mythisch", "legendary",
                    "relikt", "exklusiv", "goettlich"]
    RANK = {r: i for i, r in enumerate(RARITY_ORDER)}

    # Themen-Emojis je Stufe (deterministisch ausgewaehlt) – reine Optik.
    _EMOJI = {
        "normal":    ["🌿", "🍀", "🌱", "🔰", "🧩", "🎈", "☘️", "🪶"],
        "selten":    ["🔵", "💧", "🌀", "❄️", "🐬", "🛡️", "🔷", "🌊"],
        "episch":    ["🟪", "🗡️", "🪽", "🕯️", "🎭", "⚜️", "🧬", "🪬"],
        "mythisch":  ["🟣", "🔮", "🌌", "🦄", "👾", "🪄", "🧿", "🌠"],
        "legendary": ["👑", "✨", "🔥", "💎", "🐉", "🏆", "⚡", "🌟"],
        "relikt":    ["🟠", "🏺", "📜", "🗿", "⏳", "🔆", "🪙", "🜂"],
        "exklusiv":  ["🔱", "💠", "🌈", "⭐", "🩷", "🟥", "🔴", "✴️"],
        "goettlich": ["✨", "☀️", "🕊️", "♾️", "🌞", "💫", "🪐", "🜃"],
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

    def _grenzen(self):
        """Kumulierte Hash-Grenzen aus pool_pct - EINE Quelle fuer die Verteilung.

        Vorher standen die Grenzen (62/88/97) als Zahlen im Code und mussten bei
        jeder Aenderung an pool_pct doppelt gepflegt werden. Stufen mit pool_pct 0
        (Haendler-Stufen) kommen hier nie heraus."""
        if self.__dict__.get("_grenz_cache") is None:
            grenzen = []
            summe = 0
            for r in self.RARITY_ORDER:
                pct = int(self.RARITY[r].get("pool_pct", 0) or 0)
                if pct <= 0:
                    continue
                summe += pct
                grenzen.append((summe, r))
            if not grenzen:                       # Notnagel: nie ohne Stufe dastehen
                grenzen = [(100, self.RARITY_ORDER[0])]
            elif grenzen[-1][0] < 100:            # Rest der haeufigsten Stufe geben
                grenzen[-1] = (100, grenzen[-1][1])
            self.__dict__["_grenz_cache"] = grenzen
        return self.__dict__["_grenz_cache"]

    def rarity_of(self, text):
        """Feste Seltenheit eines Titels (per Hash, Verteilung via pool_pct)."""
        r = self._h(text, "rarity") % 100
        for grenze, rarity in self._grenzen():
            if r < grenze:
                return rarity
        return self._grenzen()[-1][1]

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
