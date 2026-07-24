"""Zentrale Befehls-Normalisierung.

Korrigiert das ERSTE Wort einer an Flo gerichteten Nachricht auf einen bekannten
Befehl, wenn:
- es ein bayrisch/oesterreichisches Dialekt-Wort ist (z. B. 'spui' -> 'spiel'), ODER
- es sich nur um EINEN Tippfehler unterscheidet (Levenshtein/Transposition = 1,
  z. B. 'skpi' -> 'skip', 'lautstrke' -> ... nutzt eh eigene Toleranz).

So reagieren ALLE Feature-Module tolerant, ohne dass jedes einzeln angepasst
werden muss. bot.py setzt danach message.content kurz auf die korrigierte Form
und stellt sie nach dem Befehls-Durchlauf wieder her (die KI bekommt immer den
Originaltext, falls kein Befehl passte)."""


class CmdNorm:
    """Zentrale Befehls-Normalisierung als Klasse (rein struktureller Umbau)."""

    # Alle Befehls-Trigger-Woerter, die IRGENDEIN Modul am Satzanfang versteht.
    # (Aus dem Inventar aller Module - dorthin wird korrigiert.)
    KNOWN = {
        # music
        "skip", "ueberspring", "überspring", "naechst", "nächst", "next", "pause",
        "pausier", "resume", "weiter", "fortsetz", "weiterspiel", "stop", "stopp",
        "halt", "aufhoer", "aufhör", "leave", "verlass", "raus", "disconnect",
        "queue", "warteschlange", "liste", "join", "connect", "verbinde", "komm",
        "spiel", "spiele", "play", "lautstärke", "lautstaerke", "lautstarke",
        "volume", "vol", "lauter", "louder", "leiser", "quieter", "leise",
        "nochmal", "nochmals", "repeat", "replay", "wiederhol", "wiederhole",
        "random", "zufall", "zufallssong", "überrasch", "ueberrasch",
        "lyrics", "lyric", "songtext", "liedtext",
        # economy
        "level", "lvl", "rank", "rang", "coins", "konto", "kontostand", "münzen",
        "muenzen", "balance", "top", "bestenliste", "rangliste", "leaderboard",
        "daily", "täglich", "taeglich", "tagesbonus", "pay", "zahl", "zahle",
        "überweis", "ueberweis", "überweise", "shop", "laden", "store", "kaufen",
        "buy", "kauf", "inventar", "inventory", "titel", "titles", "title",
        "equip", "anlegen", "trage", "tragen", "anziehen", "setze",
        # moderation
        "lösch", "loesch", "delete", "clear", "purge", "aufräum", "cleanup", "nuke",
        "warn", "verwarn", "warns", "verwarnungen", "warnungen", "warnliste",
        "unwarn", "entwarn", "verzeih", "timeout", "mute", "muten", "stumm",
        "knebel", "auszeit", "untimeout", "enttimeout", "unmute", "unmuten",
        "entmute", "entstumm", "entknebel", "kick", "rauswerf", "rausschmei",
        "ban", "bann", "banne", "verbann", "sperr", "unban", "entbann", "entsperr",
        # games
        "quiz", "trivia", "zahlenraten", "raten", "errate", "schnickschnack",
        "coinflip", "münzwurf", "muenzwurf", "flip", "münze", "muenze", "slot",
        "slots", "spielautomat", "automat", "würfel", "wuerfel", "würfeln",
        "wuerfeln", "dice", "roll",
        # casino
        "casino", "spielbank", "kasino", "glücksspiel", "gluecksspiel", "gambling",
        "blackjack", "karte", "ziehen", "zieh", "stand", "stehen", "bleiben",
        "bleib", "genug", "fertig", "double", "doppeln", "verdoppeln", "crash",
        "absturz", "rakete", "rocket", "keno", "roulette", "kessel",
        "mines", "minen", "minesweeper", "bomben", "cashout", "auszahlen",
        "glücksrad", "gluecksrad", "wheel", "rubbellos", "rubbel",
        "scratch", "duell", "duel", "stats", "statistik", "statistiken", "bilanz",
        # words (Wort-Zaehler)
        "wörter", "woerter", "wort", "worte", "wortzähler", "wortzaehler",
        "words", "word", "wordcount",
        # admin (nur Besitzer - schadet als Korrekturziel niemandem)
        "gib", "nimm", "setcoins", "gibxp", "profil", "ansage", "shopneu",
        "adminhilfe", "admin", "sendepause", "funkstille", "lockdown",
        # luxus
        "luxus", "luxury", "prestige", "thron", "throne",
        # handel (Coin-Handelsbuch)
        "handel", "handelsbuch", "transaktion", "transaktionen", "verlauf",
        "trades",
        # steal (Coin-Raub)
        "steal", "klau", "klauen", "raub", "rauben", "heist",
        # merchant (fahrender Haendler)
        "haendler", "händler", "merchant", "kraemer", "krämer",
        "wanderhaendler", "wanderhändler", "trader", "kramer",
        # lotto (Monats-Lotto)
        "lotto", "lottery", "jackpot", "lose", "los", "ziehung",
        # floaktie (FloCorp-Aktie $FLO)
        "floaktie", "floaktien", "flostock", "floshare", "flonyse", "floboerse",
        # stocks (Aktienkurse)
        "aktie", "aktien", "stock", "stocks", "kurs", "ticker", "börse",
        "boerse", "share",
        # terraria (Wiki)
        "terraria", "terra", "twiki", "terrariawiki",
        # neue casino-spiele
        "hilo", "tower", "turm", "sieben", "baccarat", "bakkarat", "punto",
        "doppelt",
        # coin-spiele
        "mathe", "rechnen", "kopfrechnen", "anagramm", "wortsalat", "reaktion",
        "reaktionstest", "reflex", "quizduell",
        # fun
        "roast", "disst", "diss", "rösten", "roesten", "hype", "hyped", "props",
        "rate", "bewerte", "rizz", "sigma", "aura", "chad", "rizzler", "spruch",
        "horoskop", "weisheit", "wisdom", "fortune", "keks",
        # voicegags
        "sounds", "soundboard", "soundliste", "sound", "soundeffekt", "sprich",
        "vorlesen",
        # media
        "male", "zeichne", "generier", "generiere", "bild", "quote", "zitat", "meme",
        # food
        "kalorien", "kcal", "naehrwerte", "nährwerte", "makros", "makro",
    }

    # Bayrisch/oesterreichischer Dialekt -> anerkanntes Befehlswort.
    DIALECT = {
        "spui": "spiel", "spuih": "spiel", "spü": "spiel", "spöi": "spiel",
        "spöl": "spiel", "spuis": "spiel", "spün": "spiel",
        "hoit": "stop", "hoid": "stop", "aus": "stop",
        "weida": "weiter", "weita": "weiter", "weda": "weiter",
        "schleich": "leave", "schleichdi": "leave", "gemma": "leave",
        "lauda": "lauter", "leisa": "leiser",
        "geld": "coins", "moos": "coins", "kohle": "coins", "koin": "coins",
        "kaffa": "kaufen", "kafn": "kaufen",
        "iberspring": "skip", "übaspring": "skip", "iwaspring": "skip",
        "wiafl": "würfel", "wiaschd": "würfel",
        "haudi": "leave",
        "vasteck": "roast", "obara": "hype",
        "wiavui": "coins",
    }

    # Haeufige normale Woerter, die NICHT als vertippter Befehl gelten sollen
    # (Distanz 1 zu einem Befehl, aber im Chat gaengig).
    STOPWORDS = {
        # Gaengige Chat-Woerter (nur relevant, wenn sie per Einfuegen/Loeschen/
        # Vertauschung auf einen Befehl fallen wuerden - Ersetzungen sind eh gesperrt).
        "hallo", "danke", "bitte", "gerne", "kannst", "machst", "willst", "musst",
        "hast", "habt", "bist", "sagst", "gehst", "siehst", "meinst", "denkst",
        # 'Befehl + 1 Buchstabe = echtes deutsches Wort' (per Loeschung gefaehrlich):
        "halts", "warnt", "warnst", "ratet", "ratest", "kickt", "kickst", "rollt",
        "rollst", "pausen", "pausier", "stopp", "stops", "spielt", "spielst",
        "leiser", "leise", "banne", "banns", "kalte", "kalter", "bilde", "bilder",
        # 1 Buchstabe von 'worte'/'wort' entfernt - normale Woerter in Ruhe lassen:
        "orte", "ort", "worten", "wert", "werte",
        # 1 Tippfehler von 'minen'/'bomben' - Alltagswoerter nicht kapern:
        "meinen", "mienen", "bombe",
        # 1 Tippfehler von 'nimm'/'profil'/'ansage' (Admin-Befehle):
        "nimmt", "profi", "ansagen",
        # 1 Tippfehler von 'turm' - 'Sturm' ist Alltagssprache:
        "sturm",
        # 1 Tippfehler von 'handel' - normale Verben/Woerter nicht kapern:
        "handeln", "wandel",
        # 1 Buchstabe von 'komm'/'spiele' entfernt - gaengige Verben nicht kapern
        # ('kommt ihr?' darf nicht zum Voice-Join werden, 'spielen wir?' nicht zu 'spiele'):
        "kommt", "spielen",
    }
    # Echte Befehle nie als Stopword blocken:
    STOPWORDS -= KNOWN

    def _one_typo(self, a, b):
        """True, wenn b aus a durch GENAU EINEN typischen Tippfehler entsteht:
        eine Einfuegung, eine Loeschung ODER eine Nachbar-Vertauschung.

        BEWUSST OHNE Ersetzung (ein Buchstabe gegen einen anderen): die produziert
        viel zu oft ein anderes ECHTES Wort (hast->halt, plan->play, nice->dice,
        bald->bild ...) und wuerde normalen Chat als Befehl kapern. Echte Vertipper
        sind fast immer Vertauschungen/verdoppelte/fehlende Buchstaben - die bleiben."""
        if a == b:
            return True
        la, lb = len(a), len(b)
        if la == lb:
            # Gleich lang -> nur eine Nachbar-Vertauschung erlauben (keine Ersetzung).
            diff = [i for i in range(la) if a[i] != b[i]]
            return (len(diff) == 2 and diff[1] == diff[0] + 1
                    and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
        if abs(la - lb) != 1:
            return False
        # Laenge unterscheidet sich um 1 -> genau eine Einfuegung/Loeschung?
        longer, shorter = (a, b) if la > lb else (b, a)
        i = j = 0
        skipped = False
        while i < len(longer) and j < len(shorter):
            if longer[i] == shorter[j]:
                i += 1
                j += 1
            elif skipped:
                return False
            else:
                skipped = True
                i += 1
        return True

    def _fuzzy(self, word):
        """Naechstgelegenes bekanntes Befehlswort bei genau einem Vertipper
        (Einfuegen/Loeschen/Nachbar-Vertauschung) - nur wenn EINDEUTIG und das Wort
        lang genug ist (kurze Woerter sind zu mehrdeutig)."""
        if len(word) < 4:
            return None
        # Ziel muss selbst >= 4 Buchstaben haben: kurze Befehle (ban, top, pay, vol ...)
        # nur EXAKT erkennen, sonst faellt jedes 4-Buchstaben-Wort per Loeschung drauf
        # (band->ban, tops->top ...).
        hits = {w for w in self.KNOWN if len(w) >= 4 and self._one_typo(word, w)}
        return next(iter(hits)) if len(hits) == 1 else None

    def normalize(self, cleaned):
        """Nimmt den (schon vom Botnamen befreiten) Text. Gibt die korrigierte Form
        zurueck, falls das erste Wort per Dialekt/Tippfehler ersetzt wurde - sonst
        None (dann bleibt alles wie es ist)."""
        if not cleaned:
            return None
        parts = cleaned.split(None, 1)
        first = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        core = first.lower().strip(".,;:!?-")
        if not core or core in self.KNOWN:
            return None                          # schon ein gueltiger Befehl
        target = self.DIALECT.get(core)
        if target is None:
            if core in self.STOPWORDS:
                return None                      # normales Wort in Ruhe lassen
            target = self._fuzzy(core)
        if not target or target == core:
            return None
        return f"{target} {rest}".strip()


# Modul-Instanz + Aliase, damit die bisherigen Modulnamen weiter funktionieren.
instance = CmdNorm()
KNOWN = CmdNorm.KNOWN
DIALECT = CmdNorm.DIALECT
STOPWORDS = CmdNorm.STOPWORDS
_one_typo = instance._one_typo
_fuzzy = instance._fuzzy
normalize = instance.normalize
