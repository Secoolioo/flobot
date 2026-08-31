"""Zentrale Befehls-Normalisierung.

Korrigiert das ERSTE Wort einer an Flo gerichteten Nachricht auf einen bekannten
Befehl. Vier Toepfe, in genau dieser Reihenfolge:

1. KNOWN    - alle Woerter, die IRGENDEIN Modul woertlich als Befehl versteht.
              Treffer heisst: alles gut, NICHTS umschreiben.
2. ALIAS    - fremdsprachige Synonyme ('wipe' -> 'purge', 'shift' -> 'schicht').
3. DIALECT  - boarisch/oesterreichisch ('spui' -> 'spiel', 'hackln' -> 'arbeit').
4. Tippfehler - genau EINE Einfuegung/Loeschung/Nachbar-Vertauschung zu einem
              KNOWN-Wort, und nur wenn eindeutig ('skpi' -> 'skip').
              STOPWORDS bremst hier Alltagswoerter aus.

Der Unterschied zwischen 1 und 2/3 ist wichtig: ein Synonym in KNOWN waere tot.
KNOWN bedeutet 'schon gueltig, nicht anfassen' - das Wort ginge unveraendert an
ein Modul, das es nicht kennt, und faellt zur KI durch. Uebersetzt wird nur in
ALIAS/DIALECT.

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
        "reichste", "reich", "geld", "vermögen", "vermoegen", "reichtum",
        "geldtop", "moneytop", "coinlb",
        "überweis", "ueberweis", "überweise", "shop", "laden", "store", "kaufen",
        "buy", "kauf", "inventar", "inventory", "titel", "titles", "title",
        "equip", "anlegen", "trage", "tragen", "anziehen", "setze",
        # moderation
        "lösch", "loesch", "delete", "clear", "purge", "aufräum", "cleanup", "nuke",
        "warn", "verwarn", "warns", "verwarnungen", "warnungen", "warnliste",
        "unwarn", "entwarn", "verzeih", "timeout", "mute", "muten", "stumm",
        "knebel", "auszeit", "untimeout", "enttimeout", "unmute", "unmuten",
        "entmute", "entstumm", "entknebel", "kick", "rauswerf", "rausschmeis",
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
        "chart",
        # floaktie: eigene Aktie ($FLO) - 'aktie'/'kurs'/'aktienkurs' gehoeren jetzt hierher
        "aktie", "aktien", "kurs", "aktienkurs", "kursverlauf", "flokurs",
        # terraria (Wiki)
        "terraria", "terra", "twiki", "terrariawiki",
        # profil-lookup - MUSS hier stehen: sonst korrigiert die Aehnlichkeits-
        # suche 'banner' auf 'banne' (Moderation), und 'Flo banner @wer' hat die
        # Person GEBANNT statt ihr Banner zu zeigen.
        "check", "profil", "profile", "userinfo", "whois", "lookup",
        "nachschlagen", "steckbrief", "avatar", "pfp", "profilbild",
        "banner", "profilbanner",
        # schuldbuch - MUSS hier stehen (sonst korrigiert die Aehnlichkeits-
        # suche 'leih' auf 'leise' und 'tilg' auf 'titel')
        "schulden", "schuld", "kreide", "kreidetafel", "zettel", "schuldenbuch",
        "leih", "leihe", "leihen", "verleih", "verleihe", "kredit", "borg",
        "borge", "borgen", "schuldschein", "schein", "iou", "anschreiben",
        "tilg", "tilge", "tilgen", "abzahl", "abzahlen",
        "insolvenz", "privatinsolvenz", "bankrott", "pleite",
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
        # arbeit (Schichten, Lohnzettel, Wordle) - das Modul fehlte hier ganz,
        # damit griff die Tippfehler-Korrektur fuer keinen seiner Befehle.
        "work", "arbeit", "arbeiten", "job", "schicht", "malochen",
        "lohnzettel", "lohn", "gehalt", "arbeitszeugnis",
        # Spass-Wordle: die Schreibweisen, die Leute WIRKLICH tippen. Mehr als
        # einen Vertipper faengt _fuzzy nicht, deshalb stehen die haeufigen
        # Varianten direkt hier.
        "wordle", "wordl", "wordel", "worlde", "wörtle", "woertle", "wortle",
        "wörtel",
        # Wort des Tages - eigene Woerter, damit klar ist, um welchen Topf es
        # geht. 'daily' bleibt bewusst der Tagesbonus aus economy.
        "tageswort", "tageswordle", "wortdestages", "tagesraetsel",
        "tagesrätsel", "worddaily", "dailyword",
        # --- Nachtrag: Befehle, die die Module WOERTLICH kennen, hier aber
        # fehlten. Solange sie fehlen, hielt _fuzzy sie fuer Vertipper und hat
        # sie verbogen ('sendpause' -> 'sendepause', 'time-out' -> 'timeout',
        # 'naehrwert' -> 'naehrwerte'). Jeder Eintrag ist nachgemessen: die
        # Liste bringt 40 falsche Kaperungen weg und keine neue dazu.
        # music: Lautstaerke-Kurz- und Tippfehlerformen aus _VOLUME_WORDS
        "lautst", "lautstk", "lautstrke", "lautstaerk", "lautstärk",
        "lautsärke", "lautstärje", "lautsterke", "lautstaeke", "lautsärcke",
        "lautr", "ls", "lst", "lstk", "lstrk", "lstrke",
        "verlasse", "wiederholen", "wiederholst",
        # guildcfg (das Modul stand hier ueberhaupt nicht)
        "einstellungen", "einstellung", "config", "konfig", "konfiguration",
        "servereinstellungen", "settings",
        # giveaway (stand hier ebenfalls nicht)
        "giveaway", "giveaways", "gewinnspiel", "gewinnspiele", "verlosung",
        "verlosungen", "verlosen", "raffle", "gw",
        # admin
        "give", "schenk", "schenke", "take", "entzieh", "entziehe",
        "coinsset", "givexp", "xpgeben", "announce", "dm", "flüster",
        "fluester", "shoprefresh", "sendpause", "adminhelp",
        # moderation
        "del", "aufraeum", "time-out", "rausschmeiss", "rausschmeiß", "unbann",
        # casino
        "bj", "17und4", "siebzehnundvier", "hit", "pass", "dd", "roul",
        "mine", "rad", "höhertiefer", "hoehertiefer", "don",
        # games
        "rps", "sss", "ssp", "w6", "quizduel", "quizzz",
        # profil / economy
        "user", "pb", "av", "bal", "lb", "inv",
        # schulden
        "debt", "debts", "zurueckzahlen", "zurückzahlen",
        # arbeit / fun / voicegags / media / food / floaktie
        "werk", "gas", "sb", "tts", "say", "generiert", "generierst", "img",
        "naehrwert", "nährwert", "$flo",
        # bayern: Dialekt-Schalter und die Gruesse, die das Modul erkennt
        "bayrisch", "bayerisch", "bairisch", "baierisch", "boarisch",
        "boirisch", "dialekt", "servus", "servas", "sers", "seas", "habidere",
        "griasdi", "griaßdi", "griasgod", "griaseich", "zefix", "pfiadi",
        "pfiati", "griaß", "griass", "pfiat", "pfiad",
    }

    # Fremdsprachige Synonyme -> anerkanntes Befehlswort. Bewusst ein eigener
    # Topf und NICHT in KNOWN: KNOWN heisst "ist schon ein gueltiger Befehl,
    # nichts umschreiben" - ein Synonym dort wuerde also unveraendert an ein
    # Modul gehen, das es nicht kennt, und zur KI durchfallen (nachgeprueft
    # mit 'wipe'). Hier wird es dagegen auf den echten Befehl umgeschrieben.
    ALIAS = {
        "addcoins": "gib", "addmoney": "gib", "addxp": "gibxp",
        "broadcast": "ansage", "broke": "pleite", "cfg": "config",
        "grind": "arbeit", "options": "einstellungen", "removemoney": "nimm",
        "serversettings": "einstellungen", "shift": "schicht",
        "shopreset": "shopneu", "silence": "mute", "tempmute": "timeout",
        "warnings": "warns", "whisper": "dm", "wipe": "purge",
    }

    # Bayrisch/oesterreichischer Dialekt -> anerkanntes Befehlswort.
    DIALECT = {
        "spui": "spiel", "spuih": "spiel", "spü": "spiel", "spöi": "spiel",
        "spöl": "spiel", "spuis": "spiel", "spün": "spiel",
        "hoit": "stop", "hoid": "stop", "aus": "stop",   # 'aus' siehe NUR_ALLEIN
        "weida": "weiter", "weita": "weiter", "weda": "weiter",
        "schleich": "leave", "schleichdi": "leave", "gemma": "leave",
        "lauda": "lauter", "leisa": "leiser",
        # 'geld' steht schon in KNOWN (ein gueltiger Befehl) - normalize() steigt
        # dort vorher aus, der Eintrag war also toter Code.
        "moos": "coins", "kohle": "coins", "koin": "coins",
        "kaffa": "kaufen", "kafn": "kaufen",
        "iberspring": "skip", "übaspring": "skip", "iwaspring": "skip",
        "wiafl": "würfel", "wiaschd": "würfel",
        "haudi": "leave",
        "vasteck": "roast", "obara": "hype",
        "wiavui": "coins",
        # --- Nachtrag Boarisch. Exakter Vergleich, deshalb ziehen diese
        # Schluessel keine Nachbarwoerter an (gegengeprueft: 'kommt' bleibt
        # 'kommt', 'normal' bleibt 'normal').
        "abgebrannt": "pleite", "aufdrahn": "lauter", "aufhean": "stop",
        "aufraama": "aufräum", "auframa": "aufräum", "auszoin": "auszahlen",
        "batzn": "coins", "borgn": "borg", "eistellunga": "einstellungen",
        "fladern": "klau", "gnua": "genug", "goid": "coins",
        "greisler": "haendler", "greissler": "haendler", "gschaeft": "shop",
        "gschäft": "shop", "gwinnspui": "gewinnspiel", "göid": "coins",
        "hackln": "arbeit", "hackn": "arbeit", "hoits": "stop",
        "ibaweis": "ueberweis", "iwaspringa": "skip", "kartn": "karte",
        "kaufn": "kauf", "kimm": "komm", "kini": "thron",
        "kontostond": "kontostand", "kreidn": "kreide", "kumm": "komm",
        "leihn": "leih", "lodn": "laden", "nausschmeissn": "kick",
        "nochmoi": "nochmal", "nomal": "nochmal", "nomoi": "nochmal",
        "pausn": "pause", "raubn": "raub", "reichstn": "reichste",
        "schaugn": "check", "schmaeh": "spruch", "schmäh": "spruch",
        "schotter": "coins", "schuftn": "arbeit", "schuid": "schuld",
        "schuidn": "schulden", "siebm": "sieben", "spuits": "spiel",
        "stibitzn": "klau", "vabann": "verbann", "vaschwind": "leave",
        "vawarn": "verwarn", "vazeih": "verzeih", "weisheid": "weisheit",
        "wiafln": "würfel", "wörtl": "wordle", "zammrama": "aufräum",
        "zaster": "coins", "zeichna": "zeichne", "ziag": "zieh",
        "ziagn": "ziehen", "ziagung": "ziehung", "zoggn": "casino",
        "zoihn": "zahl", "zoin": "zahl", "übaspringa": "skip",
    }

    # Uebersetzungen, die NUR gelten, wenn das Wort ALLEIN steht.
    #
    # 'Flo aus!' heisst wirklich "stop" - aber 'aus' ist eben auch eine der
    # haeufigsten deutschen Praepositionen. Nachgemessen wurde daraus:
    #   'Flo aus welchem Grund machst du das?'  ->  'stop welchem Grund ...'
    # Die Musik ging aus, und die Frage erreichte die KI nie. Deshalb greift
    # diese Uebersetzung nur ohne Rest - dann ist die Absicht eindeutig.
    NUR_ALLEIN = {"aus"}

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
        # 1 Tippfehler von einem GEFAEHRLICHEN Moderations-/Raub-Befehl. Ohne
        # diese Liste wurde aus 'banane' ein Bann, aus 'klick'/'kicks' ein Kick,
        # aus 'waren' eine Verwarnung und aus 'klaus' (ein Name!) ein Coin-Raub.
        # Neue Eintraege hier gehoeren in test_cmdnorm_kapert_keine_alltagswoerter.
        "banane", "bananen", "klick", "klicks", "kicks", "waren", "ware",
        "klaus", "klaue", "klauen", "banner", "bann", "warte", "warten",
        "wanne", "kanne", "kicker", "sperre", "sperrt",
        # 1 Tippfehler von 'nimm'/'profil'/'ansage' (Admin-Befehle):
        "nimmt", "profi", "ansagen",
        # 1 Tippfehler von 'turm' - 'Sturm' ist Alltagssprache:
        "sturm",
        # 1 Tippfehler von 'handel' - normale Verben/Woerter nicht kapern:
        "handeln", "wandel",
        # 1 Buchstabe von 'komm'/'spiele' entfernt - gaengige Verben nicht kapern
        # ('kommt ihr?' darf nicht zum Voice-Join werden, 'spielen wir?' nicht zu 'spiele'):
        "kommt", "spielen",
        # 1 Buchstabe von 'setze'/'trage' entfernt - Alltags-Imperative bleiben
        # Alltagssprache ('Flo setz dich mal' war vorher 'Titel dich besitzt du nicht').
        "setz", "trag",
        # 1 Tippfehler von den arbeit-Befehlen. Nachgemessen, nicht geraten:
        # ohne die ersten drei wuerde aus 'Flo lohnt sich das?' der Lohnzettel,
        # aus 'schlicht' eine Schicht.
        "lohnt", "lohnte", "schlicht",
        # 'world' war schon VORHER kaputt und hat mit arbeit nichts zu tun:
        # 'word' steht seit immer in KNOWN (Wort-Zaehler), und 'world' ist davon
        # eine Loeschung entfernt - 'Flo world of warcraft' wurde damit zu
        # 'Flo word of warcraft' und zeigte die Wort-Statistik.
        "world", "worlds", "worldofwarcraft",
        # --- Nachgemessen am ganzen Repo-Wortschatz (13.882 Woerter): das
        # waren echte Fehlgriffe, keine geratenen. Sortiert nach dem Schaden,
        # den der Fehlgriff angerichtet hat.
        # Moderation/Raub/Mute: 'Flo heisst du Flo?' hat einen Coin-Raub
        # gestartet, 'pure' war ein Massenloeschen, 'nebel' ein Knebel,
        # 'anne' (ein Vorname!) ein Bann.
        "pure", "heisst", "nebel", "anne",
        # Gluecksspiel: 'cash' hat Crash gestartet, 'mies'/'meine'/'ines' Mines.
        "cash", "mies", "meine", "ines",
        # Coins: 'leiche'/'bogen' waren Kredite, 'leite' eine Privatinsolvenz,
        # 'schwein' ein Schuldschein, 'takte'/'stake'/'tanke' Coin-Abzug,
        # 'schenken'/'schenkt' Coin-Geschenke.
        "leiche", "bogen", "leite", "schwein", "takte", "stake", "tanke",
        "schenken", "schenkt",
        # Bilder kosten echtes Geld bei der KI - die duerfen keinen Auftrag
        # ausloesen. Nachgemessen loesten SECHS alltaegliche Woerter einen aus:
        # "Flo zeichnen wir mal was?" wurde zu "zeichne wir mal was" und Flo
        # malte das. Dasselbe mit malen/malte/zeichnet/generieren.
        "build", "zeichen", "malle",
        "zeichnen", "zeichnet", "zeichnete", "zeichneten",
        "malen", "malte", "malten", "malst",
        "generieren", "generierte", "generierten", "generierung",
        # 'komma' hat Flo in den Voice geholt.
        "komma",
        # Vornamen und sehr haeufige Chatwoerter auf harmlosen Zielen - falsch
        # bleibt falsch: 'frank'/'krank'/'trank' -> Rang, 'laura' -> Aura,
        # 'chat'/'hart' -> Aktienchart, 'swords' -> Wortzaehler,
        # 'dicke' -> Wuerfel, 'close' -> Lottolose, 'grass' -> Grussformel,
        # 'pfad' -> Abschiedsgruss, 'spieler'/'zweiter' -> Musik.
        "frank", "krank", "trank", "laura", "chat", "hart", "swords", "dicke",
        "close", "grass", "pfad", "spieler", "zweiter",
        # Blackjack 'pass': 'Flo passt das?' und 'Spass' sind haeufiger.
        "passt", "spass", "spaß",
        # Nur MIT Satzmuster ein Befehl ('mach den song an', 'red bayerisch').
        # Die duerfen darum nicht in KNOWN stehen - dort wuerde ihr Schutz hier
        # wegfallen (STOPWORDS -= KNOWN) und jede Beugung waere Freiwild.
        "antwort", "antworte", "dreh", "hau", "kannst", "leg", "mach", "pack",
        "red", "rede", "redn", "schalt", "schreib", "schreibe", "stell", "tu",
        # Englische und deutsche Alltagswoerter, die einen Buchstaben von einem
        # Befehl entfernt liegen (aus der Modulpruefung, jedes nachgemessen).
        "bavaria", "broadcasts", "burns", "burnt", "claims", "clam", "clan",
        "cleans", "continued", "crow", "debit", "debits", "debut", "debuts",
        "debüt", "debüts", "drawn", "draws", "even", "gain", "generated",
        "glatze", "glazed", "guests", "images", "insults", "invests", "item",
        "lean", "mage", "marco", "mats", "option", "paints", "peak",
        "purchases", "quit", "quite", "seen", "sehen", "share", "shuffles",
        "speaks", "spinn", "spion", "spuin", "steak", "stock", "surprised",
        "surprises", "sven", "swipe", "ticket", "unser", "wealthy", "whispern",
        "wiped", "wippe",
        # --- Nachgemessen mit werkzeug/inventar.py --cmdnorm (31.08.2026) ---
        # Das Inventar kennt seit heute die echte Befehlsliste (536 Woerter).
        # Damit liess sich zum ersten Mal MESSEN statt raten, was die
        # Tippfehler-Toleranz verbiegt: jedes Wort aus dem Repo-Wortschatz
        # durch normalize(), und die Treffer nach Mechanismus getrennt.
        #
        # Uebrig blieben 63 Faelle, in denen ein ANDERES Wort auf einem Befehl
        # landet (Beugungen wie 'bannen' -> 'banne' sind genau richtig und
        # bleiben). Davon stehen hier die, die ein Mensch wirklich tippt.
        # Die schlimmsten waren:
        #   'status' -> stats      'Flo status?' ist die natuerlichste Frage
        #                          der Welt und rief die Casino-Statistik auf
        #   'tage'   -> trage      'Tage' ist Alltagsdeutsch
        #   'reply'  -> replay     'lese'/'leser' -> leise/leiser
        #   'erlass' -> verlass    im Bot mit Schuldenbuch besonders fies:
        #                          statt Schulden zu erlassen ging Flo aus dem
        #                          Voice-Channel
        "abzaehlen", "abzählen", "aktive", "aktiven", "arten", "blieb",
        "blieben", "eich", "erlass", "haelt", "hält", "handle", "handler",
        "landen", "leib", "lese", "leser", "mate", "mins", "raet", "rät",
        "rasten", "rauten", "reichte", "relay", "reply", "saetze", "sätze",
        "setzte", "spielte", "starts", "states", "status", "strand", "tage",
        "tagen", "traege", "träge", "verrate", "worst",
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
        # Erst die exakten Uebersetzungen (Fremdsprache, dann Dialekt), danach
        # die Tippfehler-Toleranz. Beide Toepfe werden EXAKT verglichen und sind
        # bewusst keine Fuzzy-Ziele: gemessen zog das sonst Vornamen mit rein
        # ('emma' -> leave, 'lisa' -> leiser) und 'normal' wurde zu 'nochmal'.
        target = self.ALIAS.get(core) or self.DIALECT.get(core)
        if target is not None and core in self.NUR_ALLEIN and rest.strip():
            return None                          # nur allein ein Befehl
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
ALIAS = CmdNorm.ALIAS
DIALECT = CmdNorm.DIALECT
NUR_ALLEIN = CmdNorm.NUR_ALLEIN
STOPWORDS = CmdNorm.STOPWORDS
_one_typo = instance._one_typo
_fuzzy = instance._fuzzy
normalize = instance.normalize
