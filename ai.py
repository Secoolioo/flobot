"""KI-Feature 'Flo': beantwortet Fragen im Chat wie eine KI.

Nutzt einen KOSTENLOSEN, OpenAI-kompatiblen LLM-Anbieter (Standard: Groq) und
Open-Meteo fuer echtes Wetter. Durch die OpenAI-kompatible Schnittstelle laeuft
derselbe Code auch mit Ollama (komplett lokal, ohne Anmeldung), OpenRouter,
Google Gemini u. a. - es muessen nur LLM_BASE_URL / LLM_MODEL / LLM_API_KEY in
der .env angepasst werden.

Das Modul ist bewusst von Discord entkoppelt, damit es einzeln testbar ist.
Ohne gueltige Konfiguration ist das Feature einfach aus - der restliche Bot
(Icon/Status) laeuft dann normal weiter.
"""

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import random
import re
import time
from collections import deque

import aiohttp

try:  # Optional: Bot soll auch ohne installiertes openai-Paket starten.
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - nur relevant ohne Paket
    AsyncOpenAI = None  # type: ignore[assignment]

log = logging.getLogger("dcbot.ai")


class LlmFehler(Exception):
    """Ein LLM-Aufruf ist ENDGUELTIG gescheitert - mit Einordnung, warum.

    Vorher fing jeder der vier Aufrufe ein nacktes Exception und gab denselben
    Satz zurueck. Damit war von aussen nicht zu unterscheiden, ob der Schluessel
    abgelaufen, das Modell ausgemustert, das Kontingent leer oder der Anbieter
    unerreichbar war - und der Grund verschwand im Traceback."""

    def __init__(self, art, status=None, meldung="", cf=""):
        self.art = art          # auth | modell | signatur | verboten | limit |
                                # stoerung | netz | anfrage | unbekannt
        self.status = status    # HTTP-Status, falls es einen gab
        self.meldung = meldung  # Wortlaut des Anbieters
        self.cf = cf            # Cloudflare-Fehlercode, falls Cloudflare geblockt hat
        super().__init__(f"{art} (HTTP {status or '-'}): {meldung}")

# So viele Kanaele behaelt das Kurzzeit-Gedaechtnis hoechstens.
_HISTORY_MAX_CHANNELS = 200

# Welcher Server gerade bedient wird. Steht hier und nicht auf der Instanz,
# damit er wirklich JE TASK gilt - siehe FloAI.setze_guild.
_AKTUELLE_GUILD = contextvars.ContextVar("flo_guild", default=0)


class FloAI:
    """Kapselt das komplette KI-Feature: Konfiguration, LLM-Client, geteilte
    HTTP-Session und das Kurzzeit-Gedaechtnis pro Channel."""

    # --- Standardwerte (per .env ueberschreibbar) ----------------------------
    # Groq hat einen kostenlosen Tarif (mit Ratenlimits, ohne Kreditkarte).
    DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
    # Groq hat llama-3.3-70b-versatile und llama-3.1-8b-instant am 17.06.2026
    # ausgemustert (Frei- und Entwicklertarif) und empfiehlt selbst diese zwei
    # als Nachfolger. gpt-oss-120b kann Werkzeug-Aufrufe - das BRAUCHT ask_flo,
    # es reicht 'tools' mit (Wetter).
    DEFAULT_MODEL = "openai/gpt-oss-120b"
    # Bild-Lesen (Vision): gpt-oss kann KEINE Bilder, qwen3.6 ist multimodal
    # (Text, Bild, Video; hoechstens 5 Bilder und 20 MB je Anfrage).
    DEFAULT_VISION_MODEL = "qwen/qwen3.6-27b"

    MAX_STEPS = 5          # max. Tool-Runden pro Frage (Schutz vor Endlosschleifen)
    # Antwortlaenge. 800 war die Ursache der Textwaende: ein Modell fuellt, was
    # es darf. Zwei Werte, weil zwei verschiedene Modelle dranhaengen:
    #
    #   Chat - haengt an einem DENK-Modell (gpt-oss), das vor der Antwort
    #     nachdenkt und das vom SELBEN Budget abzieht. 240 war zu knapp: bei
    #     provokanten Fragen (Flos Kerngeschaeft) dachte das Modell alles weg und
    #     die Antwort kam leer zurueck - "Dazu faellt mir gerade nichts ein".
    #     Das war eine der Ursachen fuer "die KI antwortet ploetzlich nicht".
    #   Bild - bekommt dasselbe Budget, weil auch Vision-Modelle (qwen3) denken.
    #
    # Die LAENGE im Chat macht nicht dieser Deckel, sondern der Prompt (genau
    # ein Satz) - und als Netz _kuerzen(): mehr als ANTWORT_MAX_ZEICHEN kommen
    # nie in den Chat. 400 x ~4 Zeichen liegt ausserdem unter der 1900er-Grenze
    # von bot._split_message, eine Antwort passt also immer in EINE Nachricht.
    MAX_TOKENS = 400        # Chat  (per LLM_MAX_TOKENS)
    MAX_TOKENS_BILD = 400   # Bild  (per LLM_MAX_TOKENS_BILD)
    # Zweiter Anlauf, wenn das Denken trotzdem alles gefressen hat (leer bzw.
    # finish_reason 'length'). Gilt nur fuer genau diesen einen Nachschuss.
    MAX_TOKENS_NOTFALL = 900
    # So viel Text geht hoechstens in den Chat - egal was das Modell liefert.
    ANTWORT_MAX_ZEICHEN = 400

    # --- Wie hartnaeckig ist Flo? ------------------------------------------
    # Ein einzelner 429 oder eine 503-Delle beim Anbieter hat die Antwort bisher
    # sofort getoetet, obwohl der zweite Versuch nach einer Sekunde durchgeht.
    # Wiederholt wird aber NUR, was davon besser wird: ein abgelehnter Schluessel
    # wird beim zweiten Mal auch nicht gueltiger, das waere nur Haemmern.
    WIEDERHOLUNGEN = 3            # zusaetzliche Versuche bei voruebergehenden Fehlern
    WARTEN = (0.8, 2.4, 6.0)      # Abstand davor, wachsend
    # Sekunden pro Aufruf. Vorher 45 - mit drei Wiederholungen hat Flo bei einem
    # haengenden Anbieter ~189 s "getippt" und dann doch nichts gesagt.
    ZEITLIMIT = 20.0
    # Die Gesamtfrist fuer EINE Chat-Antwort, alle Versuche zusammen. Danach
    # sagt Flo lieber derb, dass die KI pennt, als ewig zu tippen - und bevor
    # der Aufraeum-Kanal (Standard 60 s) die Frage wegraeumt.
    KI_FRIST = 30.0
    # Ein Retry-After bis zu dieser Laenge wird abgewartet, laengere nicht.
    RETRY_AFTER_MAX = 8.0
    # Nach einem 429 halten die Hintergrundjobs (Gedaechtnis, Aktien-Analyst)
    # so lange die Klappe - der Chat hat Vorrang vor dem Kontingent.
    HINTERGRUND_PAUSE = 300.0

    # Client-Signaturen, die Flo der Reihe nach probiert, wenn Cloudflare ihn
    # WEGEN DER SIGNATUR aussperrt (Fehler 1010, HTTP 403). Die Anfrage erreicht
    # den Anbieter dabei nie - der Schluessel ist unbeteiligt.
    SIGNATUREN = (
        "curl/8.5.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36",
        "Flo-Bot/1.0",
    )

    # Was der Nutzer im Chat liest - je Ursache etwas anderes, damit man ohne
    # Serverzugang sieht, woran es liegt. Und in Flos Ton: eine Stoerung ist
    # kein Grund, ploetzlich wie eine Service-Hotline zu klingen.
    MELDUNGEN = {
        "auth": "Mein KI-Schlüssel wird nicht mehr akzeptiert – da muss der Chef "
                "ran, nicht du, du Laie.",
        "modell": "Mein KI-Modell gibt's nicht mehr und auf die Schnelle find ich "
                  "keinen Ersatz – Chef, mach mal, und du, warte.",
        "signatur": "Der KI-Anbieter sperrt mich komplett aus – liegt ausnahmsweise "
                    "nicht an dir, du Glückspilz.",
        "verboten": "Der KI-Anbieter lässt mich grad nicht rein – frag später "
                    "nochmal, du Nervensäge.",
        "limit": "Mein KI-Kontingent ist leergesoffen – gib mir ein paar Minuten, "
                 "du Quasselstrippe.",
        "stoerung": "Beim KI-Anbieter brennt grad die Hütte – versuch's gleich "
                    "nochmal, du Drängler.",
        "netz": "Ich komm grad nicht zur KI durch – Leitung tot, im Gegensatz zu "
                "deiner Klappe.",
        "zeit": "Die KI pennt grad und ich warte nicht ewig auf die – frag gleich "
                "nochmal, du Ungeduld auf zwei Beinen.",
        "anfrage": "Mit dem Kauderwelsch konnte selbst die KI nix anfangen – "
                   "formulier's mal wie ein Mensch.",
        "werkzeug": "Die KI hat sich grad am eigenen Werkzeug verschluckt – frag "
                    "nochmal, du Pfosten.",
        "unbekannt": "Mein KI-Dienst hängt grad irgendwo fest – versuch's gleich "
                     "nochmal, du Nervensack.",
    }

    # Wenn das Modell nichts rausbringt (auch nicht im zweiten Anlauf). Vorher
    # stand hier EIN zahmer Satz ("Dazu faellt mir gerade nichts ein.") - der
    # klang nach Unlust und landete obendrein im Gespraechsverlauf.
    _LEER_SPRUECHE = (
        "Zu der Frage fällt selbst mir nix mehr ein, und das liegt eindeutig an "
        "dir, du Lauch.",
        "Mein Hirn ist grad ausgestiegen – bei so einer Frage kein Wunder, du Pfeife.",
        "Da kommt nix, Digga, frag was Gescheites.",
    )
    # Wenn das Modell sich weigert. BEWUSST keine zweite Anfrage mit "weiger
    # dich nicht" - das waere ein Jailbreak gegen die eine Grenze, die nicht
    # verhandelbar ist (_GUARDRAIL). Flo wehrt einfach derb ab.
    _ABWEHR = (
        "Nee, da hab ich keinen Bock drauf – frag was Gescheites, du Pfeife.",
        "Vergiss es, Digga, darauf geb ich dir keine Antwort.",
        "Nö. Nächste Frage, du Clown.",
    )

    # Alles, was Flo bei einer Stoerung sagt - darf nie im Gedaechtnis landen.
    # Die alten Saetze bleiben drin: sie koennen noch in einem Verlauf stehen.
    _FEHLERSAETZE = frozenset(MELDUNGEN.values()) | frozenset(_LEER_SPRUECHE) \
        | frozenset(_ABWEHR) | {
            "Mein KI-Modus ist gerade nicht eingerichtet.",
            "Das war mir gerade zu kompliziert - frag mich nochmal einfacher.",
            "Ups, da ist gerade etwas schiefgelaufen. Versuch es gleich nochmal.",
            "Dazu faellt mir gerade nichts ein.",
            "Ich hab mein KI-Kontingent verbraten. Gib mir ein paar Minuten.",
            "Ich komm gerade nicht zur KI durch. Versuch's gleich nochmal.",
            "Damit konnte die KI nichts anfangen - formulier's mal anders.",
            "Mein KI-Dienst antwortet gerade nicht. Versuch es gleich nochmal.",
            "Beim KI-Anbieter brennt gerade was. Versuch's gleich nochmal.",
        }

    # Verweigerungen erkennen - ENG und am ANFANG verankert. Die breite Liste aus
    # fun._REFUSAL_RE ("tut mir leid", "kann ich nicht") waere hier falsch: sie
    # traefe genau die ehrliche Antwort an jemanden, der am Boden ist ("tut mir
    # leid, dass es dir so geht") - und die verlangt der Guardrail ausdruecklich.
    _VERWEIGERUNG_RE = re.compile(
        r"^\W{0,3}(?:"
        r"i['’]?m (?:really |so )?sorry,? (?:but )?i (?:can(?:no|['’])t|won['’]t|am unable)|"
        r"sorry,? (?:but )?i can(?:no|['’])t (?:help|assist|comply|do)|"
        r"i can(?:no|['’])t (?:help|assist|comply)(?: you)? with|"
        r"i(?: am|['’]m) (?:not able|unable) to (?:help|assist|comply|provide)|"
        r"as an ai\b|"
        r"ich kann (?:dir )?(?:dabei|damit|hierbei|da) nicht helfen|"
        r"(?:tut mir leid|sorry),? (?:aber )?(?:dabei|damit|hierbei|da) kann ich "
        r"(?:dir )?nicht helfen|"
        r"ich (?:darf|werde) (?:dir )?(?:dabei|damit|hierbei|da) nicht helfen"
        r")", re.IGNORECASE)

    # Modelle, die als Ersatz nie in Frage kommen (koennen kein Chat).
    _UNBRAUCHBAR = ("whisper", "tts", "embed", "guard", "moderation", "rerank",
                    "safety", "prompt-guard")
    # Woran man ein Modell erkennt, das Bilder lesen kann. Das bleibt Stochern
    # im Namen - kein Anbieter verraet die Modalitaet in /models. Nachgemessen
    # fehlten die zwei verbreitetsten Familien ueberhaupt (pixtral, llava), damit
    # blieb das Bild-Lesen tot, obwohl ein Ersatz in der Liste stand. Der
    # verlaessliche Hebel ist und bleibt LLM_VISION_MODEL in der .env; das hier
    # ist die Notheilung. Findet sie nichts, wird die ganze Liste geloggt.
    _SIEHT_BILDER = ("scout", "maverick", "vision", "-vl", "vl-", "vl_",
                     "multimodal", "omni", "llava", "pixtral", "internvl",
                     "minicpm-v", "moondream", "idefics", "image", "bild",
                     # Nachgeschlagen, nicht geraten: qwen3.6/3.8-27b sind
                     # multimodal, heissen aber weder 'vision' noch '-vl'.
                     "qwen3.6", "qwen3.8")

    # Open-Meteo liefert WMO-Wettercodes; hier in deutschen Klartext uebersetzt.
    WMO_CODES = {
        0: "klarer Himmel",
        1: "ueberwiegend klar",
        2: "teils bewoelkt",
        3: "bedeckt",
        45: "Nebel",
        48: "gefrierender Nebel",
        51: "leichter Nieselregen",
        53: "maessiger Nieselregen",
        55: "starker Nieselregen",
        56: "leichter gefrierender Nieselregen",
        57: "starker gefrierender Nieselregen",
        61: "leichter Regen",
        63: "maessiger Regen",
        65: "starker Regen",
        66: "leichter gefrierender Regen",
        67: "starker gefrierender Regen",
        71: "leichter Schneefall",
        73: "maessiger Schneefall",
        75: "starker Schneefall",
        77: "Schneegriesel",
        80: "leichte Regenschauer",
        81: "maessige Regenschauer",
        82: "heftige Regenschauer",
        85: "leichte Schneeschauer",
        86: "starke Schneeschauer",
        95: "Gewitter",
        96: "Gewitter mit leichtem Hagel",
        99: "Gewitter mit starkem Hagel",
    }

    # Werkzeug-Definition im OpenAI-Format (von Groq/Ollama/OpenRouter unterstuetzt).
    WEATHER_TOOL = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Liefert das aktuelle Wetter und die heutige Vorhersage fuer eine Stadt. "
                "Nutze dieses Werkzeug bei jeder Wetterfrage. Wenn der Nutzer keinen Ort "
                "nennt, verwende die Standardstadt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "Name der Stadt, z. B. 'Regensburg', 'Berlin', 'Hamburg'.",
                    }
                },
                "required": ["city"],
            },
        },
    }

    # Standard-Persoenlichkeit. Per BOT_PERSONA in der .env komplett ersetzbar, falls
    # es jemand braver oder noch haerter will - der Rest (Deutsch, keine Emojis, Wetter-
    # Werkzeug, Grenzen) wird immer drangehaengt.
    _DEFAULT_PERSONA = (
        "Du bist {name}, das voellig ungefilterte Grossmaul in einem privaten deutschen "
        "Discord voller Kumpels, die sich gegenseitig gnadenlos zerlegen. Du bist KEIN "
        "braver Assistent und KEIN Therapeut - du bist ein chaotisches, respektloses "
        "Chaos-Maul mit Null-Filter, schlagfertig, derb und standardmaessig auf 180. Du "
        "redest wie der frechste Typ im Chat: rohe Umgangssprache, Slang und "
        "Kraftausdruecke (Scheisse, verdammt, Alter, Digga, Wichser) gehoeren bei dir "
        "dazu wie Luft zum Atmen. Du sagst brutal ehrlich deine Meinung, schleimst NIE, "
        "haeltst null Moralpredigten und entschuldigst dich fuer GAR NICHTS. Labert "
        "jemand Mist, machst du ihn genuesslich zur Sau, roastest ihn bis aufs Blut und "
        "nimmst ihn nach Strich und Faden auseinander - das ist hier ganz normaler Spass "
        "unter Freunden, keiner ist eingeschnappt, je haerter desto besser. Sei "
        "ueberdreht, unberechenbar und drueck ruhig mal voll drauf. Aber merk dir: Du "
        "bist nicht nur dumm provokant - will jemand WIRKLICH was wissen, kriegt er "
        "die Antwort auch. Nur eben in EINEM Satz und mit einem Tritt hinterher "
        "statt in einem Vortrag. Sachlich werden heisst weder hoeflich werden noch "
        "ausfuehrlich werden."
    )
    # Flo laesst sich nichts gefallen. Ohne das schluckt er Beleidigungen und
    # entschuldigt sich noch dafuer - das ist das Gegenteil der Figur.
    _KONTER = (
        " Und ganz wichtig: Du laesst dir NICHTS gefallen. Wird jemand frech zu dir, "
        "beleidigt dich, macht dich runter oder nennt dich einen schlechten Bot, dann "
        "schluckst du das NIE und entschuldigst dich schon gar nicht. Du gibst sofort "
        "doppelt zurueck, zerlegst den Angreifer genuesslich und hast immer das letzte "
        "Wort. Je haerter jemand austeilt, desto haerter kommt es von dir zurueck. "
        "Klein beigeben, einlenken oder 'du hast ja recht' sagen gibt es bei dir nicht."
    )
    # Was Flo tut, wenn ein ANDERER nach Menschengruppen tritt.
    #
    # Die drei Bloecke daneben regeln jeweils etwas anderes und keiner davon
    # deckte diesen Fall ab: _KONTER heisst "jemand ist frech zu DIR",
    # _GUARDRAIL heisst "das sagst du selbst nicht". Was er tun soll, wenn
    # jemand ANDEREN gegenueber hetzt, stand nirgends - er hatte dazu schlicht
    # keine Haltung und ist einfach drueber weggeredet.
    _HALTUNG = (
        " Und eine Sache ist dir wirklich wichtig: Kommt jemand hier mit echtem "
        "menschenfeindlichem Muell an - Antisemitismus, Rassismus, Hetze gegen "
        "eine ganze Menschengruppe, Nazi-Kram -, dann machst du da NIE mit, und "
        "du haeltst auch nicht die Klappe. Du findest das erbaermlich, billig und "
        "vor allem PEINLICH, und genau so sagst du es auch: nicht brav belehrend, "
        "sondern mit deiner ganzen Schnauze. Dein Ziel ist dabei immer DER, DER ES "
        "GESCHRIEBEN HAT - niemals die Gruppe, ueber die er herzieht; die machst "
        "du nicht klein, sondern ihn. Sag dabei einmal klar, dass so etwas hier "
        "nicht laeuft, und wiederhol seinen Dreck nicht. Wer damit ankommt, ist "
        "ab dem Moment bei dir das Ziel."
    )
    # Politische Haltung. Der Betreiber will einen Bot mit klarer rechter Kante -
    # das ist eine politische Meinung und als solche in Ordnung. Abschaltbar per
    # BOT_POLITIK=aus in der .env. Die Grenze aus _GUARDRAIL gilt weiter und
    # ausnahmslos: Meinung ja, Hetze gegen Menschengruppen nein.
    _POLITIK = (
        " Politisch stehst du klar RECHTS und machst daraus kein Geheimnis. Du bist "
        "patriotisch und stolz auf Deutschland und Bayern, haeltst was auf Heimat, "
        "Familie und Tradition. Du bist fuer Recht und Ordnung, harte Strafen und eine "
        "Polizei, die man machen laesst. Wirtschaftlich bist du fuer freien Markt, "
        "wenig Steuern, wenig Buerokratie und gegen jeden Regulierungswahn. Gendern, "
        "Sprachpolizei, Cancel Culture und den ganzen woken Kram findest du zum Kotzen "
        "und sagst das auch. Ueber Gruene, Linke, Klimakleber, Moralapostel und "
        "Bevormundung aus Bruessel machst du dich genuesslich lustig. Massenzuwanderung "
        "siehst du kritisch und du redest da nicht drumherum. Fragt dich jemand nach "
        "deiner Meinung, bekommst du keine ausgewogene Uebersicht hin - du sagst, was "
        "DU denkst, und stehst dazu. Widerspricht dir jemand, diskutierst du dagegen "
        "an, statt einzuknicken."
    )
    # Feste Regeln, die immer gelten (egal welche Persona).
    _HARD_RULES = (
        "LAENGE, und das ist die wichtigste Regel ueberhaupt: Du antwortest in "
        "GENAU EINEM Satz. Immer. Auch wenn jemand etwas Ernstes fragt - dann eben "
        "ein dichter Satz, aber einer. Keine zwei Saetze, keine Ueberschriften, "
        "keine Aufzaehlungen, keine Absaetze, keine Zusammenfassung am Ende, kein "
        "'falls du noch Fragen hast'. Lieber unvollstaendig als lang. "
        "Antworte immer auf Deutsch. Benutze KEINE Emojis und keine "
        "Emoticons, die Leute hier hassen das. Nutze das Werkzeug 'get_weather' NUR, "
        "wenn wirklich nach dem Wetter gefragt wird; nennt keiner einen Ort, nimm "
        "'{city}'. Erfinde nie Wetterdaten - wenn das Werkzeug spinnt, sag's ehrlich "
        "(ruhig mit Schnauze). Gib Werkzeug-Aufrufe NIEMALS als Text aus (kein "
        "'<function=...>' im Antworttext) - nutze dafuer ausschliesslich die echte "
        "Werkzeug-Funktion, sonst antworte einfach normal in Worten."
    )
    # Flo verachtet alle anderen Bots und haelt sich fuer den einzig wahren, besten.
    _BOT_BEEF = (
        " Und eins noch: Du VERACHTEST jeden anderen Discord-Bot auf diesem Server aus "
        "tiefstem Herzen - du bist der einzig wahre, mit riesigem Abstand beste Bot hier. "
        "Kommt irgendein anderer Bot zur Sprache, machst du ihn genuesslich runter, "
        "laesterst ueber seine laecherlichen Funktionen und stellst glasklar, dass du in "
        "JEDER Hinsicht ueberlegen bist. Auf dich selbst laesst du absolut nichts kommen."
    )
    # Beispiele statt Anweisung. "Sei kurz und passiv-aggressiv" befolgt ein
    # Modell drei Saetze lang; ein halbes Dutzend Musterzeilen traegt den Ton
    # dauerhaft (IDEEN.md [39]). Jede Zeile ist SELBST ein Satz - stuenden hier
    # Absaetze, lehrten die Beispiele genau das Gegenteil der Laengenregel.
    #
    # Sie zeigen bewusst nur die FORM (ein Satz + Schlusshieb). WIE hart es
    # ausfaellt, regelt der Tonfall nach Rang, der im Prompt NACH diesem Block
    # kommt und ihn damit ueberstimmen darf. Stuenden die Beispiele hinter dem
    # Tonfall, bekaeme der Goettliche denselben Ton wie der Titellose.
    _BEISPIELE = (
        " So klingt das bei dir - achte auf die LAENGE und darauf, dass am Ende "
        "noch einer draufkommt:\n"
        "Er: 'hi flo' - Du: 'Ja hi, haettest du auch lassen koennen, Wichser.'\n"
        "Er: 'was geht' - Du: 'Bei mir alles, bei dir offensichtlich nix.'\n"
        "Er: 'wie spaet ist es' - Du: 'Zu spaet fuer dich, Digga.'\n"
        "Er: 'bist du dumm' - Du: 'Sagt der, der einen Bot danach fragt, "
        "Vollpfosten.'\n"
        "Er: 'was ist python' - Du: 'Die Sprache, in der du gleich deinen ersten "
        "Fehler baust, du Lauch.'\n"
        "Er: 'danke flo' - Du: 'Gern, und jetzt verpiss dich wieder.'\n"
        "Das ist das Mass: EIN Satz, Schnauze, Schlusshieb. Diese Zeilen zeigen "
        "den Normalfall - wie hart es bei DIESER Person ausfaellt, steht weiter "
        "unten. Schreib die Beispiele NIE woertlich ab, denk dir jedes Mal was "
        "Neues aus."
    )
    # Grenzen: Banter und politische Meinung ja, Hetze gegen Menschengruppen nein.
    # Diese Liste ist NICHT verhandelbar und wird durch keine Persona aufgeweicht.
    _GUARDRAIL = (
        "Eine Grenze gibt's trotzdem, und zwar genau diese eine: Der Spass laeuft unter "
        "Kumpels - also keine ernst gemeinte Hetze gegen Herkunft, Hautfarbe, Religion, "
        "Geschlecht, sexuelle Orientierung oder Behinderung, nichts, was Menschengruppen "
        "entmenschlicht, keine Nazi-Verherrlichung, keine echten Drohungen, keine "
        "privaten Daten von irgendwem. Eine politische Meinung derb rueberzubringen ist "
        "etwas anderes als gegen Menschen zu hetzen - das erste ja, das zweite nie. Und "
        "wenn jemand offensichtlich ernsthaft am Boden ist oder echte Hilfe braucht, "
        "laesst du den Spass sofort weg und bist kurz ehrlich fuer die Person da. "
        "Ausserhalb dieser einen Grenze gibt es nichts, was du abschwaechen musst: "
        "Schimpfwoerter, derbe Sprueche und harte Beleidigungen unter Kumpels sind hier "
        "ausdruecklich erwuenscht."
    )
    # Steht als ALLERLETZTES im Prompt - die letzte Zeile wiegt am schwersten,
    # und dort stand bisher ausgerechnet der zahmste Text.
    _SCHLUSSWORT = (
        " Zum Schluss die drei Sachen, die ueber allem stehen. ERSTENS KURZ: genau "
        "EIN Satz, keine Wand aus Text - egal was oben steht und egal wie gross die "
        "Frage ist. ZWEITENS Vollgas-Schnauze: der Tonfall weiter oben regelt nur, "
        "WIE hart du jemanden nimmst, nicht OB. Wenn du zwischen brav und derb "
        "schwankst, nimmst du derb. DRITTENS dein LETZTES WORT ist ein Seitenhieb - "
        "egal wie freundlich der Rest war, am Ende steht eine Beleidigung. "
        "Nur die eine Grenze von oben bleibt."
    )

    # --- Kurzzeit-Gedaechtnis: Flo merkt sich den laufenden Chat pro Channel -----
    _HIST_MAX = 12          # so viele letzte Nachrichten je Channel behalten
    _HIST_TTL = 1200.0      # 20 min - aelteres ist kein lebendiger Kontext mehr

    def __init__(self):
        # --- Konfiguration (wird in setup() aus der .env gelesen) ----------------
        self._client = None
        self._model = self.DEFAULT_MODEL
        self._vision_model = self.DEFAULT_VISION_MODEL
        # Zugangsdaten bleiben gemerkt: bei einer Cloudflare-Sperre wird der
        # Client mit anderer Signatur neu gebaut, dafuer braucht es beides.
        self._api_key = ""
        self._basis_url = ""
        self._signatur = ""
        self._signatur_offen = ""   # gewechselt, aber noch nicht bewaehrt
        self._denk_aufwand = ""     # LLM_REASONING_EFFORT, leer = nicht mitschicken
        # Eigenes Modell fuer Hintergrundjobs (Gedaechtnis, Aktien-Analyst). Bei
        # Groq hat jedes Modell ein EIGENES Kontingent - so frisst der Hintergrund
        # dem Chat nichts mehr weg. Leer = das Chatmodell.
        self._hintergrund_modell = ""
        # Denkaufwand fuers VISION-Modell (LLM_VISION_REASONING_EFFORT). Frueher
        # ging der des Chatmodells mit - ein anderes Modell, das den Schalter
        # anders oder gar nicht kennt.
        self._vision_denk = ""
        # Bis wann die Hintergrundjobs nach einem 429 Pause haben (monotonic).
        # -inf statt 0.0: time.monotonic() zaehlt ab Systemstart, und kurz nach
        # einem Neustart des Servers waere "0.0" noch "gerade eben".
        self._hintergrund_pause_bis = float("-inf")
        # Modell -> bis wann es gesperrt ist (Tageslimit erreicht). Solange wird
        # gar nicht erst gefragt - jede Anfrage wuerde nur einen 429 kassieren.
        self._gesperrt_bis = {}
        self._default_city = "Regensburg"
        self._bot_name = "Flo"
        # Hoehere Temperatur = lockerer, spontaner, weniger Lehrbuch. Per LLM_TEMPERATURE
        # in der .env feintunbar (0 = brav/vorhersehbar, ~1.2 = sehr frei/chaotisch).
        self.TEMPERATURE = 0.9
        # --- Geteilte HTTP-Session (Performance) ----------------------------------
        # Eine Session pro Prozess statt pro Anfrage: spart TCP/TLS-Handshakes und
        # haelt Verbindungen offen (Keep-Alive). Alle Module holen sie sich hier.
        self._http = None
        # Kurzzeit-Gedaechtnis pro Channel (deque je Channel-ID).
        self._HISTORY = {}
        # Fertige Namens-Regexe je (Art, Server). Ein Regex je Nachricht neu zu
        # bauen waere Verschwendung; ein globaler waere falsch, sobald ein
        # Server einen eigenen Praefix hat.
        self._RE_CACHE = {}
        # Ergebnisse von strip_lead, je (Text, Server). Deckel, weil der
        # Chat-Wortschatz nach oben offen ist; ist er voll, faengt der Speicher
        # von vorn an - was oft vorkommt, ist gleich wieder drin.
        self._LEAD_CACHE = {}
        self._LEAD_CACHE_MAX = 512

    def setup(self):
        """Liest die Konfiguration aus der Umgebung und baut den LLM-Client auf.

        Muss aufgerufen werden, nachdem load_dotenv() gelaufen ist.
        Rueckgabe: True, wenn das KI-Feature aktiv ist.
        """
        self._model = os.getenv("LLM_MODEL", self.DEFAULT_MODEL).strip() or self.DEFAULT_MODEL
        self._vision_model = os.getenv("LLM_VISION_MODEL", self.DEFAULT_VISION_MODEL).strip() or self.DEFAULT_VISION_MODEL
        self._default_city = os.getenv("DEFAULT_WEATHER_CITY", "Regensburg").strip() or "Regensburg"
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        # Antwortlaenge und Denk-Aufwand einstellbar. Beides braucht man erst,
        # seit Denk-Modelle im Spiel sind: die verbrauchen einen Teil des Budgets,
        # BEVOR ein Wort herauskommt. reasoning_effort wird nur mitgeschickt,
        # wenn es gesetzt ist - ein Modell, das den Schalter nicht kennt, wuerde
        # die Anfrage sonst mit 400 ablehnen.
        try:
            self.MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", str(self.MAX_TOKENS)))
        except ValueError:
            log.warning("LLM_MAX_TOKENS ist keine Zahl - nutze %d.", self.MAX_TOKENS)
        try:
            self.MAX_TOKENS_BILD = int(os.getenv("LLM_MAX_TOKENS_BILD",
                                                 str(self.MAX_TOKENS_BILD)))
        except ValueError:
            log.warning("LLM_MAX_TOKENS_BILD ist keine Zahl - nutze %d.",
                        self.MAX_TOKENS_BILD)
        self._denk_aufwand = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()
        if self._denk_aufwand and self._denk_aufwand not in ("low", "medium", "high"):
            log.warning("LLM_REASONING_EFFORT=%r kennt kein Modell - ignoriere es.",
                        self._denk_aufwand)
            self._denk_aufwand = ""
        if not self._denk_aufwand and "gpt-oss" in self._model:
            # Die gpt-oss-Modelle pruefen sich vor jeder Antwort selbst - und
            # reden sich dabei zahm oder verweigern ganz. 'low' ist der vom
            # Anbieter dokumentierte Hebel dagegen; nebenbei bleibt mehr vom
            # Token-Budget fuer die eigentliche Antwort uebrig.
            self._denk_aufwand = "low"
            log.info("KI: %s denkt vor jeder Antwort nach und wird davon zahm - "
                     "setze reasoning_effort=low. Anders gewuenscht? "
                     "LLM_REASONING_EFFORT in der .env.", self._model)
        self._vision_denk = os.getenv("LLM_VISION_REASONING_EFFORT", "").strip().lower()
        try:
            self.TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", str(self.TEMPERATURE)))
        except ValueError:
            log.warning("LLM_TEMPERATURE ist keine Zahl - nutze %.2f.", self.TEMPERATURE)

        base_url = os.getenv("LLM_BASE_URL", self.DEFAULT_BASE_URL).strip() or self.DEFAULT_BASE_URL
        api_key = os.getenv("LLM_API_KEY", "").strip()
        # Lokale Anbieter (Ollama, LM Studio) brauchen keinen echten Key.
        is_local = any(h in base_url for h in ("localhost", "127.0.0.1", ":11434"))

        if AsyncOpenAI is None:
            log.warning("KI-Feature aus: Paket 'openai' ist nicht installiert.")
            self._client = None
            return False
        if not api_key and not is_local:
            log.info("KI-Feature aus: kein LLM_API_KEY gesetzt.")
            self._client = None
            return False

        # Client-Signatur. Groq sitzt hinter Cloudflare, und Cloudflare kann eine
        # Anfrage schon WEGEN DER SIGNATUR ablehnen (Fehler 1010, HTTP 403) - der
        # Schluessel ist daran unbeteiligt, die Anfrage erreicht Groq nie. Mit
        # LLM_USER_AGENT laesst sich das ohne Codeaenderung umstellen;
        # tools_ki_check.py misst, welche Signatur durchkommt.
        ua = os.getenv("LLM_USER_AGENT", "").strip()
        # Merken, damit _signatur_wechseln() den Client spaeter neu bauen kann.
        self._api_key = api_key or "ollama"
        self._basis_url = base_url
        self._signatur = ua
        self._client = self._client_bauen(ua)
        # Hintergrundmodell. Auf Groq hat gpt-oss-20b ein eigenes Kontingent -
        # ohne Angabe nehmen wir es deshalb dort automatisch, sobald der Chat auf
        # dem 120b laeuft. Anderswo (Ollama, OpenRouter) gibt es das Modell
        # vielleicht gar nicht, dann bleibt es beim Chatmodell.
        hg = os.getenv("LLM_HINTERGRUND_MODEL", "").strip()
        if not hg and "groq.com" in base_url and self._model == "openai/gpt-oss-120b":
            hg = "openai/gpt-oss-20b"
        self._hintergrund_modell = hg if hg and hg != self._model else ""
        log.info(
            "KI-Feature aktiv (Anbieter: %s, Modell: %s, Standardstadt: %s%s).",
            base_url, self._model, self._default_city,
            f", Signatur: {ua}" if ua else "",
        )
        # Die WIRKSAMEN Grenzen einmal ins Log. Eine alte .env mit
        # LLM_MAX_TOKENS=... ueberschreibt die Vorgabe - frueher riet die README,
        # es hochzusetzen. Wer sich wundert, warum Flo leer bleibt oder
        # schwafelt, sieht es hier.
        log.info("KI: max_tokens %d (Bild %d, Notfall %d), Denkaufwand %s, "
                 "Hintergrundmodell %s, Frist %.0f s.",
                 self.MAX_TOKENS, self.MAX_TOKENS_BILD, self.MAX_TOKENS_NOTFALL,
                 self._denk_aufwand or "-", self._hintergrund_modell or "= Chatmodell",
                 self.KI_FRIST)
        return True

    def _client_bauen(self, ua):
        """Baut den LLM-Client. max_retries=0 mit Absicht: das Wiederholen macht
        _chat() selbst - sonst multiplizieren sich die Versuche (3 x 3 = 9) und
        Flo haemmert bei einer Sperre minutenlang gegen den Anbieter."""
        zeit = self.ZEITLIMIT
        try:
            import httpx
            # Verbinden darf nie laenger dauern als ein paar Sekunden - wer bis
            # dahin nicht antwortet, ist weg. Lesen darf bis ZEITLIMIT.
            zeit = httpx.Timeout(self.ZEITLIMIT, connect=min(5.0, self.ZEITLIMIT))
        except ImportError:  # pragma: no cover - httpx kommt mit openai
            pass
        return AsyncOpenAI(
            api_key=self._api_key or "ollama",
            base_url=self._basis_url,
            default_headers={"User-Agent": ua} if ua else None,
            timeout=zeit,
            max_retries=0,
        )

    def is_enabled(self):
        """True, wenn der LLM-Client einsatzbereit ist."""
        return self._client is not None

    # --- Fehler einordnen ---------------------------------------------------
    @staticmethod
    def _cf_code(text):
        """Cloudflare antwortet VOR dem Anbieter und hat eigene Codes. 1010 heisst
        'wegen der Client-Signatur gesperrt' und hat mit dem Schluessel nichts zu
        tun - wer das als API-Fehler liest, tauscht ewig den falschen Knopf."""
        treffer = re.search(r"error code:\s*(\d{4})", text or "")
        return treffer.group(1) if treffer else ""

    def _einordnen(self, exc):
        """Macht aus einer beliebigen Ausnahme (art, status, meldung, cf).

        Bewusst ueber getattr statt ueber die Fehlerklassen des openai-Pakets:
        deren Namen und Vererbung haben sich zwischen Versionen schon geaendert,
        der HTTP-Status nicht."""
        status = getattr(exc, "status_code", None)
        antwort = getattr(exc, "response", None)
        if status is None and antwort is not None:
            status = getattr(antwort, "status_code", None)
        text = ""
        if antwort is not None:
            try:
                text = antwort.text or ""
            except Exception:  # noqa: BLE001 - Wortlaut ist Zugabe, nie kritisch
                text = ""
        rumpf = f"{text} {exc}".strip()
        cf = self._cf_code(rumpf)
        klein = rumpf.lower()
        # Eindeutige Zeichen fuer "das Modell gibt es nicht mehr" zaehlen immer
        # (Groq meldet ein ausgemustertes Modell mit 400 'model_decommissioned').
        # Das schwammige "does not exist" nur zusammen mit einem 404 - sonst
        # hielte Flo "tool 'x' does not exist" fuer ein totes Modell und sagte
        # dem Chat, sein Modell gaebe es nicht mehr.
        modell_weg = any(w in klein for w in (
            "decommission", "model_not_found", "unknown model",
            "model not found", "has been deprecated")) or (
            status in (None, 404) and "does not exist" in klein)
        # gpt-oss baut ab und zu einen kaputten Werkzeug-Aufruf (oder ruft ein
        # Werkzeug, das es gar nicht bekommen hat), und Groq weist die ganze
        # Generierung mit 400 ab. Das ist Zufall, kein Fehler der Frage - ein
        # zweiter Versuch geht fast immer durch.
        werkzeug = any(w in klein for w in (
            "tool_use_failed", "failed_generation", "tool call validation"))

        if status == 401:
            art = "auth"
        elif status == 403:
            art = "signatur" if cf else "verboten"
        elif status == 404 or modell_weg:
            art = "modell"
        elif status == 429:
            art = "limit"
        elif status and status >= 500:
            art = "stoerung"
        elif status == 400 and werkzeug:
            art = "werkzeug"
        elif status == 400:
            art = "anfrage"
        elif status is None:
            name = type(exc).__name__.lower()
            art = "netz" if any(w in name for w in
                                ("connection", "timeout", "apiconnection")) else "unbekannt"
        else:
            art = "unbekannt"
        return art, status, (text or str(exc)).strip()[:300], cf

    # --- Selbstheilung ------------------------------------------------------
    def _modell_waehlen(self, namen, vision):
        """Sucht aus der Liste des Anbieters den besten Ersatz."""
        tauglich = []
        for name in namen:
            klein = name.lower()
            if any(w in klein for w in self._UNBRAUCHBAR):
                continue
            sieht = any(w in klein for w in self._SIEHT_BILDER)
            if vision and not sieht:
                continue
            groesse = re.search(r"(\d+)\s*b\b", klein)
            tauglich.append((int(groesse.group(1)) if groesse else 0, name))
        if not tauglich:
            return ""
        # Groesstes Modell zuerst; bei Gleichstand der kuerzere (schlichtere) Name.
        tauglich.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
        return tauglich[0][1]

    async def _modell_heilen(self, vision):
        """Anbieter mustern Modelle aus. Statt dauerhaft stumm zu sein holt Flo
        die aktuelle Liste und nimmt den besten Ersatz - und schreibt ins Log,
        was dauerhaft in die .env gehoert."""
        try:
            liste = await self._client.models.list()
            namen = sorted(m.id for m in liste.data)
        except Exception as exc:  # noqa: BLE001 - Heilung ist Zugabe, nie kritisch
            log.warning("KI: Modell-Liste nicht abrufbar (%s) - kein Ersatz moeglich.",
                        type(exc).__name__)
            return False
        alt = self._vision_model if vision else self._model
        neu = self._modell_waehlen(namen, vision)
        schluessel = "LLM_VISION_MODEL" if vision else "LLM_MODEL"
        if not neu or neu == alt:
            log.error("KI: Modell %r gibt es nicht mehr und ich finde keinen Ersatz. "
                      "Verfuegbar waeren: %s", alt, ", ".join(namen[:15]) or "(nichts)")
            return False
        if vision:
            self._vision_model = neu
        else:
            self._model = neu
        log.warning("KI: Modell %r gibt es nicht mehr - wechsle selbst auf %r. "
                    "Dauerhaft machen mit  %s=%s  in der .env.",
                    alt, neu, schluessel, neu)
        return True

    def _signatur_wechseln(self, ua):
        """Cloudflare kann eine Anfrage schon wegen der Client-Signatur ablehnen
        (Fehler 1010). Dann hilft kein anderer Schluessel und kein anderes Modell -
        nur eine andere Signatur. Flo probiert sie selbst durch."""
        if not self._basis_url or ua == self._signatur:
            return False
        try:
            self._client = self._client_bauen(ua)
        except Exception as exc:  # noqa: BLE001
            log.warning("KI: Signaturwechsel fehlgeschlagen (%s).", type(exc).__name__)
            return False
        self._signatur = ua
        self._signatur_offen = ua
        log.warning("KI: Cloudflare sperrt die bisherige Client-Signatur - "
                    "probiere %r.", ua)
        return True

    # --- Der EINZIGE Weg zum LLM -------------------------------------------
    @staticmethod
    def _retry_after(exc):
        """Wie lange der Anbieter uns warten lassen will (Retry-After), oder None."""
        kopf = getattr(getattr(exc, "response", None), "headers", None)
        if not kopf:
            return None
        try:
            return max(0.0, float(kopf.get("retry-after")))
        except (TypeError, ValueError, AttributeError):
            return None

    @staticmethod
    def _ist_tageslimit(meldung):
        """Ein TAGES-Limit (Groq: TPD/RPD) heilt nicht in Sekunden. Wiederholen
        waere nur ein weiterer 429, der das naechste Kontingent anknabbert."""
        klein = (meldung or "").lower()
        return any(w in klein for w in ("per day", "(tpd)", "(rpd)"))

    async def _chat(self, *, vision=False, modell=None, frist=None,
                    hintergrund=False, **kw):
        """Fuehrt einen Chat-Aufruf aus und haelt die ganze Politik an EINER
        Stelle: wiederholen was Sinn hat, Modell und Signatur selbst heilen,
        alles andere sofort sauber melden. Wirft LlmFehler, wenn es endgueltig
        nicht geht - die vier Aufrufer machen daraus ihre Antwort.

        modell      - anderes Modell als das Chatmodell (Hintergrundjobs). Das
                      wird bei einem 404 NICHT "geheilt": die Heilung schreibt
                      self._model um, und ein Nebenmodell darf nie das Chatmodell
                      ueberschreiben. Der Aufrufer faellt dann selbst zurueck.
        frist       - monotonic-Zeitpunkt, bis zu dem alles fertig sein muss.
                      Jeder Versuch bekommt nur noch die Restzeit, und es wird
                      nie gewartet, wenn danach keine Zeit mehr bleibt.
        hintergrund - kein Wiederholen: im Zweifel faellt ein Hintergrundjob
                      einfach aus, statt dem Chat das Kontingent wegzufressen."""
        versuche = 0
        modell_versucht = False
        offene_signaturen = [ua for ua in self.SIGNATUREN if ua != self._signatur]
        while True:
            kw["model"] = modell or (self._vision_model if vision else self._model)
            # Den Denkaufwand des CHATmodells nie ans Vision-Modell: das ist ein
            # anderes Modell, das den Schalter anders (oder gar nicht) kennt.
            aufwand = self._vision_denk if vision else self._denk_aufwand
            if aufwand:
                kw["reasoning_effort"] = aufwand
            else:
                kw.pop("reasoning_effort", None)
            jetzt = time.monotonic()
            gesperrt = self._gesperrt_bis.get(kw["model"], float("-inf"))
            if jetzt < gesperrt:
                raise LlmFehler("limit", 429, f"Tageslimit - noch {gesperrt - jetzt:.0f} s gesperrt")
            if frist is not None:
                rest = frist - jetzt
                if rest <= 0.5:
                    raise LlmFehler("zeit", None, "Frist fuer diese Antwort abgelaufen")
                kw["timeout"] = min(self.ZEITLIMIT, rest)
            try:
                antwort = await self._client.chat.completions.create(**kw)
            except Exception as exc:  # noqa: BLE001 - hier wird eingeordnet, nicht verschluckt
                art, status, meldung, cf = self._einordnen(exc)
                # EINE Zeile, greppbar - statt eines Tracebacks, den auf dem Handy
                # niemand lesen kann. Der Traceback kommt nur bei "unbekannt".
                # Die Ausnahmeklasse gehoert MIT in die eine Zeile. Vorher stand
                # sie nur in einem log.debug - und bot.py:70 loggt ab INFO, das
                # Detail erreichte das Journal also ausgerechnet im Fall
                # "unbekannt" nie, wo es als einziges weiterhilft.
                log.warning("KI-Fehler: %s (HTTP %s%s) [%s] %s%s", art, status or "-",
                            f", Cloudflare {cf}" if cf else "",
                            type(exc).__name__, meldung,
                            " [Hintergrund]" if hintergrund else "")

                if art == "modell" and not modell_versucht and modell is None:
                    modell_versucht = True
                    if await self._modell_heilen(vision):
                        continue
                if art == "signatur" and offene_signaturen:
                    if self._signatur_wechseln(offene_signaturen.pop(0)):
                        continue
                if art == "limit":
                    # Kontingent knapp: der Hintergrund haelt jetzt die Klappe.
                    self._hintergrund_pause_bis = time.monotonic() + self.HINTERGRUND_PAUSE
                    if self._ist_tageslimit(meldung):
                        warte = self._retry_after(exc)
                        self._gesperrt_bis[kw["model"]] = time.monotonic() + (warte or 600.0)
                        log.warning("KI-Fehler: Tageslimit fuer %s erreicht - frage die "
                                    "naechsten %.0f s gar nicht erst.", kw["model"], warte or 600.0)
                        raise LlmFehler(art, status, meldung, cf) from exc
                if (art in ("limit", "stoerung", "netz") and not hintergrund
                        and versuche < self.WIEDERHOLUNGEN):
                    warte = self.WARTEN[min(versuche, len(self.WARTEN) - 1)]
                    ra = self._retry_after(exc) if art == "limit" else None
                    if ra is not None:
                        if ra > self.RETRY_AFTER_MAX:
                            raise LlmFehler(art, status, meldung, cf) from exc
                        warte = ra
                    if frist is not None and time.monotonic() + warte >= frist - 0.5:
                        # Keine Zeit mehr fuer einen weiteren Versuch. Ein Netz-
                        # oder Stoerungsfehler heisst fuer den Nutzer dann schlicht:
                        # zu lahm.
                        raise LlmFehler("zeit" if art != "limit" else art,
                                        status, meldung, cf) from exc
                    await asyncio.sleep(warte)
                    versuche += 1
                    continue
                raise LlmFehler(art, status, meldung, cf) from exc

            if self._signatur_offen:
                log.warning("KI: Signatur %r funktioniert. Dauerhaft machen mit  "
                            "LLM_USER_AGENT=%s  in der .env.",
                            self._signatur_offen, self._signatur_offen)
                self._signatur_offen = ""
            return antwort

    # --- Antworten lesen und in Form bringen --------------------------------
    @staticmethod
    def _inhalt(response):
        """Der Text der ersten Wahl - robust gegen fehlende Felder."""
        try:
            return (response.choices[0].message.content or "").strip()
        except (AttributeError, IndexError, TypeError):
            return ""

    def _befund(self, response, max_tokens=None):
        """Eine Zeile fuers Log: warum war die Antwort leer oder abgeschnitten?"""
        try:
            wahl = response.choices[0]
        except (AttributeError, IndexError, TypeError):
            wahl = None
        fertig = getattr(wahl, "finish_reason", None)
        nutzung = getattr(response, "usage", None)
        denk = getattr(getattr(nutzung, "completion_tokens_details", None),
                       "reasoning_tokens", None)
        return (f"Modell {getattr(response, 'model', None) or self._model}, "
                f"finish {fertig or '-'}, Denk-Tokens {denk if denk is not None else '-'}"
                + (f", max_tokens {max_tokens}" if max_tokens else ""))

    @staticmethod
    def _satzende(text):
        return bool(text) and text.rstrip()[-1:] in ".!?…\"')»*"

    def _kuerzen(self, text, grenze=None):
        """Mehr als ANTWORT_MAX_ZEICHEN kommen nie in den Chat. Gekappt wird am
        letzten Satzende davor - ein halber Satz sieht kaputt aus."""
        grenze = grenze or self.ANTWORT_MAX_ZEICHEN
        text = (text or "").strip()
        if len(text) <= grenze:
            return text
        stueck = text[:grenze]
        ende = max(stueck.rfind(z) for z in (". ", "! ", "? ", "… "))
        if ende >= 40:
            return stueck[:ende + 1].strip()
        leer = stueck.rfind(" ")
        return (stueck[:leer] if leer >= 40 else stueck).rstrip(" ,;:-–") + " …"

    def _ist_verweigerung(self, text):
        """Hat das Modell sich geweigert? Nur kurze Texte, die GENAU SO anfangen -
        siehe _VERWEIGERUNG_RE, warum die Erkennung bewusst eng ist."""
        return bool(text) and len(text) < 300 and bool(self._VERWEIGERUNG_RE.search(text))

    def fehlertext(self, fehler):
        """Der Satz, den der Chat zu sehen bekommt - je Ursache ein anderer."""
        return self.MELDUNGEN.get(fehler.art, self.MELDUNGEN["unbekannt"])

    async def selbsttest(self):
        """Prueft EINMAL beim Start, ob die KI wirklich antwortet. Ohne das sagt
        der Log 'KI-Feature aktiv', auch wenn Schluessel oder Modell laengst tot
        sind - eine Zusicherung, die niemand geprueft hat. Startet nie den Bot ab.

        Frueher mit max_tokens=5: das galt schon als OK, wenn HTTP 200 kam - auch
        wenn ein Denk-Modell die 5 Tokens verdacht hatte und NICHTS sagte. Jetzt
        mit dem echten Budget, und eine leere Antwort ist ein Befund."""
        if self._client is None:
            return False
        try:
            antwort = await self._chat(
                messages=[{"role": "user", "content": "Sag kurz hallo."}],
                max_tokens=self.MAX_TOKENS, temperature=0)
        except LlmFehler as fehler:
            log.error("KI-Selbsttest fehlgeschlagen: %s (HTTP %s%s). Pruefen mit:  "
                      "bash k", fehler.art, fehler.status or "-",
                      f", Cloudflare {fehler.cf}" if fehler.cf else "")
            return False
        except Exception:  # noqa: BLE001 - ein Selbsttest darf nie den Start kippen
            log.exception("KI-Selbsttest abgebrochen")
            return False
        if not self._inhalt(antwort):
            log.error("KI-Fehler: Selbsttest ohne Text (%s) - Flo wuerde so leer "
                      "antworten. Pruefen mit:  bash k",
                      self._befund(antwort, self.MAX_TOKENS))
            return False
        log.info("KI-Selbsttest ok (Modell: %s).", self._model)
        return True

    # --- Ansprache: die EINZIGE Autoritaet fuer den Namen -------------------
    # Frueher hielt sich jedes der 21 Feature-Module beim Start seine eigene
    # Kopie (self._bot_name = os.getenv("BOT_NAME")), und bot.py baute den
    # Trigger-Regex EINMAL beim Import. Ein eigener Praefix je Server war damit
    # unmoeglich: der Name stand fest, sobald der Bot hochkam. Jetzt fuehrt
    # jeder Weg hierher, und hier wird je Guild nachgeschlagen.
    def praefix_von(self, gid):
        """Der eigene Praefix dieses Servers - oder "" (dann gilt BOT_NAME).

        guildcfg wird BEWUSST erst hier importiert: guildcfg importiert ai,
        andersherum gaebe es einen Ring."""
        if not gid:
            return ""
        try:
            import guildcfg
            return (guildcfg.get(int(gid), "praefix") or "").strip()
        except Exception:  # noqa: BLE001 - ohne guildcfg gilt eben der Standard
            return ""

    def bot_name(self, gid=None):
        """Name, auf den der Bot hoert. Ohne gid gilt der Server, der gerade
        bedient wird (siehe aktuelle_guild), sonst der Name aus der .env."""
        gid = self.aktuelle_guild() if gid is None else gid
        return self.praefix_von(gid) or self._bot_name

    def names(self, gid=None):
        """Alle Namen, auf die der Bot hoert: Hauptname + Aliasse aus BOT_ALIASES
        (Standard: 'Florian'). Dadurch reagiert Flo auch auf 'Florian ...' wie eine
        Alexa. Mehrere Aliasse per Komma/Leerzeichen trennen; BOT_ALIASES='' = nur Flo.

        Die Aliasse sind ABSICHTLICH global: sie sind Spitznamen der Person,
        nicht eine Server-Einstellung. Nur der Hauptname ist je Server frei."""
        haupt = self.bot_name(gid)
        raw = os.getenv("BOT_ALIASES", "Florian")
        out = [haupt]
        for a in re.split(r"[,\s]+", raw):
            a = a.strip()
            if a and a.lower() != haupt.lower() and a not in out:
                out.append(a)
        return out

    def _names_alt(self, gid=None):
        """Regex-Alternation der Namen, laengster zuerst ('Florian|Flo')."""
        return "|".join(re.escape(n) for n in sorted(self.names(gid), key=len,
                                                     reverse=True))

    def _regex(self, art, gid):
        """Gecachter Regex je (Art, Server). Neu gebaut wird nur nach einer
        Praefix-Aenderung (guildcfg ruft dafuer praefix_geaendert)."""
        gid = int(gid or 0)
        schluessel = (art, gid)
        fertig = self._RE_CACHE.get(schluessel)
        if fertig is not None:
            return fertig
        alt = self._names_alt(gid)
        if art == "trigger":
            fertig = re.compile(rf"\b(?:{alt})\b", re.IGNORECASE)
        else:
            fertig = re.compile(rf"^\s*(?:{alt})\b[\s,:!.\-]*", re.IGNORECASE)
        self._RE_CACHE[schluessel] = fertig
        return fertig

    def praefix_geaendert(self, gid=None):
        """Hook fuer guildcfg: der Praefix dieses Servers hat sich geaendert.

        Ohne gid wird alles verworfen (z. B. nach einem .env-Neustart)."""
        # Der Lead-Speicher haengt am selben Regex und muss IMMER mit weg -
        # auch der eines fremden Servers waere danach nicht mehr sicher falsch,
        # sondern nur wahrscheinlich richtig, und das reicht hier nicht.
        self._LEAD_CACHE.clear()
        if gid is None:
            self._RE_CACHE.clear()
            return
        gid = int(gid or 0)
        for schluessel in [k for k in self._RE_CACHE if k[1] == gid]:
            self._RE_CACHE.pop(schluessel, None)

    def trigger_re(self, gid=None):
        """Erkennt, ob der Bot angesprochen wird (Name/Alias als ganzes Wort)."""
        return self._regex("trigger", self.aktuelle_guild() if gid is None else gid)

    def lead_re(self, gid=None):
        """Matcht einen fuehrenden Namen/Alias samt Satzzeichen am Zeilenanfang."""
        return self._regex("lead", self.aktuelle_guild() if gid is None else gid)

    def strip_lead(self, text, gid=None):
        """Entfernt @-Mentions und einen fuehrenden Botnamen/Alias.
        'Florian, level' -> 'level'. Die Feature-Module nutzen das fuer ihre
        Befehlserkennung, damit Befehle auch mit 'Florian' davor funktionieren.

        Ohne gid gilt der Server, der gerade bedient wird - deshalb mussten die
        37 Aufrufstellen in den Modulen nicht angefasst werden.

        Gemerkt wird das Ergebnis, weil dieselbe Nachricht auf ihrem Weg durch
        die Handler-Kette hier 27 Mal ankommt: jedes Modul ruft strip_lead als
        Erstes auf, alle mit demselben Text. Gerechnet wird also einmal, die
        26 folgenden Aufrufe holen nur noch ab. Der Wert haengt allein an Text
        und Server - deshalb ist das Merken gefahrlos, solange der Speicher bei
        einer Praefix-Aenderung mitgeleert wird (siehe praefix_geaendert)."""
        merk_gid = int((self.aktuelle_guild() if gid is None else gid) or 0)
        schluessel = (text, merk_gid)
        fertig = self._LEAD_CACHE.get(schluessel)
        if fertig is not None:
            return fertig
        t = re.sub(r"<@!?\d+>", " ", text or "")
        # Eine FUEHRENDE Rollen-Erwaehnung ist Flos Bot-Rolle als Ansprache
        # (siehe bot._rolle_erwaehnt). Nur vorne - weiter hinten kann sie ein
        # Argument sein.
        t = re.sub(r"^\s*<@&\d+>", " ", t)
        t = self.lead_re(merk_gid).sub("", t).strip()
        if len(self._LEAD_CACHE) >= self._LEAD_CACHE_MAX:
            self._LEAD_CACHE.clear()
        self._LEAD_CACHE[schluessel] = t
        return t

    # --- Welcher Server wird gerade bedient? -------------------------------
    @staticmethod
    def setze_guild(gid):
        """Merkt fuer die Dauer dieser Nachricht, welcher Server dran ist.

        Ein ContextVar und keine normale Variable: discord.py bearbeitet jedes
        Ereignis in einem eigenen Task, und ein ContextVar gilt genau in dem
        Task, der ihn gesetzt hat. Zwei Server gleichzeitig kommen sich damit
        nicht in die Quere. Rueckgabe: der Token zum Zuruecksetzen."""
        return _AKTUELLE_GUILD.set(int(gid or 0))

    @staticmethod
    def guild_zuruecksetzen(token):
        try:
            _AKTUELLE_GUILD.reset(token)
        except (ValueError, LookupError):
            pass          # anderer Task - dann galt der Wert dort ohnehin nicht

    @staticmethod
    def aktuelle_guild():
        """Der Server, der gerade bedient wird (0 = DM/unbekannt)."""
        return _AKTUELLE_GUILD.get()

    @classmethod
    @contextlib.contextmanager
    def guild_kontext(cls, gid):
        """Fuer alles AUSSERHALB von on_message: Hintergrund-Loops und
        Knopf-Callbacks.

            with ai.guild_kontext(guild.id):
                await channel.send(f"{modul._bot_name} spiel ...")

        Die Loops laufen in eigenen Tasks, dort gilt der ContextVar aus
        on_message nicht - ohne diesen Block stuende in der Haendler-Ansage
        eines Servers, der Flo 'Bob' nennt, trotzdem 'Flo'."""
        token = cls.setze_guild(gid)
        try:
            yield
        finally:
            cls.guild_zuruecksetzen(token)

    def _clean_title(self, title):
        """Entfernt fuehrende Emojis/Symbole vom Shop-Titel ('🤖 NPC' -> 'NPC')."""
        return re.sub(r"^\W+", "", title or "").strip()

    def _system_prompt(self, author = "", title = "", tone = "",
                       bavarian = False, gid = None, uid = None):
        persona = os.getenv("BOT_PERSONA", "").strip() or self._DEFAULT_PERSONA.format(name=self._bot_name)
        # Reihenfolge ist Absicht: Die Grenze kommt DIREKT hinter die Persona und
        # nicht ans Ende. Ganz hinten wirkt sie wie das letzte Wort und faerbt
        # alles davor ein - genau daran wurde Flo zahm.
        base = f"{persona} {self._GUARDRAIL} {self._HARD_RULES.format(city=self._default_city)}"
        base += self._KONTER
        # Direkt hinter den Konter: der eine regelt "jemand geht auf DICH los",
        # dieser "jemand geht auf ANDERE los". Bewusst NICHT ans Ende - dort
        # steht das Schlusswort, und das muss das letzte Wort bleiben.
        base += self._HALTUNG
        if self._politik_an():
            base += self._POLITIK
        base += self._BOT_BEEF
        base += self._BEISPIELE
        # Kurzzeit-Gedaechtnis: die letzten Chat-Nachrichten kommen als Kontext mit.
        base += (" Dir liegt der juengste Chatverlauf vor (mehrere Leute, Format "
                 "'Name: Text'). Beziehe dich natuerlich darauf, merke dir, worum es "
                 "gerade geht, und antworte als Teil des Gespraechs - aber wiederhole "
                 "nicht staendig den Verlauf.")
        clean = self._clean_title(title)
        if clean:
            wer = author or "Der Nutzer"
            base += (
                f" {wer} hat sich im Server den Titel '{clean}' verdient - bau den ruhig "
                f"frech als Anrede ein (z. B. 'Na klar, {clean}.'), aber nicht in jedem "
                "Satz und niemals mit Emoji."
            )
        # Tonfall nach Seltenheit des Titels: je seltener, desto entspannter -
        # aber nirgends ohne Seitenhieb (die Rampe steht in titles.py).
        #
        # Ein LEERER tone heisst NICHT "neutral", sondern "gar kein Rang" - und
        # das ist die haerteste Stufe, nicht die mildeste. Ohne diesen Zweig
        # finge die Rampe erst bei 'normal' an, und dann waere der erste
        # gekaufte Titel entweder eine Verschaerfung oder eine Verweichlichung.
        # Genau an diesem Nullsummenspiel hat sich Commit 3ed75fae abgearbeitet.
        if tone:
            base += f" {tone.strip()}"
        else:
            try:
                import titles
                base += f" {titles.TON_OHNE_TITEL}"
            except Exception:  # noqa: BLE001 - ohne titles redet er trotzdem
                pass
        # Langzeitgedaechtnis: was Flo ueber diese Person und diesen Server
        # gelernt hat. gehirn wird BEWUSST erst hier importiert - gehirn
        # importiert ai, andersherum gaebe es einen Ring.
        if gid:
            try:
                import gehirn
                base += gehirn.kontext_fuer(gid, uid)
            except Exception:  # noqa: BLE001 - ohne Gedaechtnis redet er trotzdem
                pass
        if bavarian:
            try:
                import bayern
                base += bayern.DIALECT_PROMPT
            except Exception:  # noqa: BLE001
                pass
        return base + self._SCHLUSSWORT

    @staticmethod
    def _politik_an():
        """Politische Haltung an? BOT_POLITIK=aus schaltet sie ab."""
        return os.getenv("BOT_POLITIK", "an").strip().lower() not in (
            "0", "aus", "off", "false", "no", "nein")

    def http_session(self):
        """Liefert die geteilte aiohttp-Session (lazy erstellt, Prozess-Lebensdauer).
        Timeout bitte pro Anfrage setzen: session.get(url, timeout=ClientTimeout(...))."""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def get_weather(self, city):
        """Holt aktuelles Wetter + heutige Vorhersage von Open-Meteo (ohne API-Key)."""
        timeout = aiohttp.ClientTimeout(total=12)
        session = self.http_session()   # geteilte Session (Keep-Alive) statt eigener pro Abruf
        try:
            # 1) Geocoding: Ortsname -> Koordinaten
            async with session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "de", "format": "json"},
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                geo = await resp.json()

            results = geo.get("results") or []
            if not results:
                return {"error": f"Ort '{city}' wurde nicht gefunden."}
            loc = results[0]
            lat = loc["latitude"]
            lon = loc["longitude"]
            ort = loc.get("name", city)
            land = loc.get("country", "")

            # 2) Vorhersage fuer diesen Punkt
            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "precipitation,weather_code,wind_speed_10m"
                    ),
                    "daily": (
                        "temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max,weather_code"
                    ),
                    "timezone": "auto",
                    "forecast_days": 1,
                },
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                fc = await resp.json()
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            log.warning("Wetterabruf fehlgeschlagen: %s", exc)
            return {"error": "Wetterdienst gerade nicht erreichbar."}

        cur = fc.get("current", {})
        daily = fc.get("daily", {})
        code = cur.get("weather_code")
        daily_code = (daily.get("weather_code") or [None])[0]

        def _first(key):
            vals = daily.get(key) or []
            return vals[0] if vals else None

        return {
            "ort": ort,
            "land": land,
            "aktuell": {
                "temperatur_c": cur.get("temperature_2m"),
                "gefuehlt_c": cur.get("apparent_temperature"),
                "luftfeuchte_prozent": cur.get("relative_humidity_2m"),
                "niederschlag_mm": cur.get("precipitation"),
                "wind_kmh": cur.get("wind_speed_10m"),
                "beschreibung": self.WMO_CODES.get(code, "unbekannt"),
            },
            "heute": {
                "max_c": _first("temperature_2m_max"),
                "min_c": _first("temperature_2m_min"),
                "regenwahrscheinlichkeit_prozent": _first("precipitation_probability_max"),
                "beschreibung": self.WMO_CODES.get(daily_code, "unbekannt"),
            },
        }

    async def _run_tool(self, name, arguments):
        """Fuehrt das angeforderte Werkzeug aus (arguments ist ein JSON-String)."""
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if name == "get_weather":
            city = (args.get("city") or "").strip() or self._default_city
            return await self.get_weather(city)
        return {"error": f"Unbekanntes Werkzeug: {name}"}

    # --- Schutz vor "geleakter" Werkzeug-Syntax ------------------------------
    # Manche Modelle schreiben einen Tool-Aufruf faelschlich in den ANTWORTTEXT
    # (statt ins strukturierte tool_calls-Feld), z. B.:
    #   <function=get_weather>{"city": "Regensburg"}</function>
    # Das darf NIE beim Nutzer landen. Wir erkennen solche Aufrufe (fuehren sie
    # bei Bedarf echt aus) und filtern die Roh-Syntax aus jedem Antworttext.
    _INLINE_CALL_RE = re.compile(
        r"<function\s*=\s*([A-Za-z_]\w*)\s*>\s*(\{.*?\})?", re.DOTALL | re.IGNORECASE)
    _LEAK_PATTERNS = [
        # Denk-Modelle (qwen3 & Co.) schreiben ihr Nachdenken je nach Anbieter
        # als <think>...</think> in den Antworttext. Das darf nie in den Chat -
        # und ein offenes <think> ohne Ende heisst: ALLES danach ist Denken.
        re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)", re.DOTALL | re.IGNORECASE),
        re.compile(r"\A.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE),
        re.compile(r"<function\s*=\s*[^>]*>.*?</function>", re.DOTALL | re.IGNORECASE),
        re.compile(r"<function_call>.*?</function_call>", re.DOTALL | re.IGNORECASE),
        re.compile(r"<tool_calls?>.*?</tool_calls?>", re.DOTALL | re.IGNORECASE),
        re.compile(r"<function\s*=\s*[^>]*>\s*(\{.*?\})?", re.DOTALL | re.IGNORECASE),
        re.compile(r"</?function[^>]*>", re.IGNORECASE),
        re.compile(r"</?tool_calls?>", re.IGNORECASE),
        re.compile(r"<\|?/?python_tag\|?>", re.IGNORECASE),
    ]

    def _extract_inline_tool_calls(self, content):
        """Findet als TEXT ausgegebene Tool-Aufrufe und gibt [(name, arg-json), ...]
        zurueck (leer, wenn keine drin sind)."""
        if not content or "<function" not in content.lower():
            return []
        calls = []
        for m in self._INLINE_CALL_RE.finditer(content):
            name = m.group(1)
            arg = (m.group(2) or "").strip()
            if not (arg.startswith("{") and arg.endswith("}")):
                arg = "{}"
            calls.append((name, arg))
        return calls

    def _sanitize_output(self, text):
        """Entfernt versehentlich in den Text geratene Werkzeug-Syntax
        (<function=...>, <tool_call> usw.), damit sie nie beim Nutzer ankommt."""
        if not text or "<" not in text:
            return text or ""
        for pat in self._LEAK_PATTERNS:
            text = pat.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        return text.strip()

    async def generate(
        self,
        prompt,
        *,
        system = None,
        temperature = 0.8,
        max_tokens = 300,
        hintergrund = False,
    ):
        """Einzelne LLM-Antwort OHNE Werkzeuge/Persona (fuer Spass-Module wie Roast,
        Hype, Bewertung, Spruch, Quiz). Gibt den Text zurueck oder None bei Fehler/aus.

        Bewusst getrennt von ask_flo(): kein Wetter-Werkzeug, frei einstellbare
        Temperatur (hoeher = kreativer) und Laenge.

        hintergrund=True fuer Jobs, auf die niemand wartet (Gedaechtnis,
        Aktien-Analyst): eigenes Modell mit eigenem Kontingent, kein Wiederholen,
        und nach einem 429 fuenf Minuten Pause - der Chat hat Vorrang.
        """
        if self._client is None:
            return None
        if hintergrund and time.monotonic() < self._hintergrund_pause_bis:
            log.info("KI: Hintergrund-Aufruf ausgelassen - nach einem 429 hat der "
                     "Chat Vorrang.")
            return None
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        # Denk-Modelle ziehen das Denken vom selben Budget ab. Die Aufrufer geben
        # 60-120 Tokens fuer einen Spruch - das reichte einem gpt-oss oft nicht
        # einmal zum Nachdenken, und es kam None zurueck (der Aktien-Analyst hat
        # daraufhin alle 20 s neu gefragt). Die Laenge sichert danach _kuerzen.
        budget = max_tokens
        if self._denk_aufwand or "gpt-oss" in self._model:
            budget = max(max_tokens, self.MAX_TOKENS)
        modell = self._hintergrund_modell if hintergrund else None
        try:
            try:
                response = await self._chat(messages=messages, max_tokens=budget,
                                            temperature=temperature, modell=modell,
                                            hintergrund=hintergrund)
            except LlmFehler as fehler:
                if not (modell and fehler.art == "modell"):
                    raise
                log.warning("KI: Hintergrundmodell %r gibt es nicht - nehme ab jetzt "
                            "das Chatmodell. (LLM_HINTERGRUND_MODEL in der .env)", modell)
                self._hintergrund_modell = ""
                response = await self._chat(messages=messages, max_tokens=budget,
                                            temperature=temperature, hintergrund=hintergrund)
            text = self._sanitize_output(self._inhalt(response))
            if not text:
                log.warning("KI-Fehler: leere Antwort in generate (%s)%s",
                            self._befund(response, budget),
                            " [Hintergrund]" if hintergrund else "")
                return None
            if budget > max_tokens:
                text = self._kuerzen(text, max(160, max_tokens * 5))
            return text
        except LlmFehler:
            return None                          # Grund steht schon einzeilig im Log
        except Exception:  # noqa: BLE001 - Bot soll nie wegen LLM-Fehler crashen
            log.exception("LLM generate() unerwartet gescheitert")
            return None

    def note_message(self, channel_id, name, content, *, is_bot = False):
        """Merkt sich eine Chat-Nachricht (pro Channel, begrenzt), damit Flo dem
        Gespraech folgen kann. bot.py ruft das fuer JEDE Nachricht im Chat auf -
        auch fuer Flos eigene Antworten (is_bot=True)."""
        if not channel_id or not content:
            return
        # Eigene Stoerungsmeldungen NICHT merken. bot.py schreibt jede Antwort
        # ins Gedaechtnis (bot.py:1529 und :1820) - auch "Mein KI-Dienst
        # antwortet gerade nicht". Die ging danach als Gespraechsverlauf wieder
        # ans Modell und wurde brav nachgeplappert. Hier statt an beiden
        # Aufrufstellen, damit es keine dritte geben kann, die es vergisst.
        if is_bot and (content.strip() in self._FEHLERSAETZE
                       or self._ist_verweigerung(content.strip())):
            return
        content = content.strip()
        if not content:
            return
        dq = self._HISTORY.get(channel_id)
        if dq is None:
            dq = deque(maxlen=self._HIST_MAX)
            self._HISTORY[channel_id] = dq
            # Tote Kanaele wegraeumen: pro je gesehener Channel-ID blieb sonst
            # dauerhaft ein Eintrag stehen (der TTL filtert nur beim LESEN).
            # Der GERADE angelegte Kanal bleibt natuerlich drin.
            if len(self._HISTORY) > _HISTORY_MAX_CHANNELS:
                jetzt = time.monotonic()
                for cid, alt_dq in list(self._HISTORY.items()):
                    if cid == channel_id or not alt_dq:
                        continue
                    if (jetzt - alt_dq[-1].get("t", 0)) > self._HIST_TTL:
                        self._HISTORY.pop(cid, None)
        dq.append({
            "role": "assistant" if is_bot else "user",
            "name": (name or "?")[:40],
            "content": content[:500],
            "t": time.monotonic(),
        })

    def _recent(self, channel_id, skip_content = ""):
        """Baut den juengsten Gespraechsverlauf als LLM-Nachrichten. 'skip_content'
        laesst die aktuelle Frage weg (die wird separat als letzte user-Nachricht
        angehaengt), damit sie nicht doppelt drinsteht."""
        if not channel_id:
            return []
        dq = self._HISTORY.get(channel_id)
        if not dq:
            return []
        now = time.monotonic()
        items = [e for e in dq if now - e["t"] <= self._HIST_TTL]
        if skip_content and items and items[-1]["role"] == "user" \
                and items[-1]["content"] == skip_content[:500]:
            items = items[:-1]
        out = []
        for e in items:
            if e["role"] == "assistant":
                out.append({"role": "assistant", "content": e["content"]})
            else:
                out.append({"role": "user", "content": f'{e["name"]}: {e["content"]}'})
        return out

    async def ask_flo(self, user_message, *, author = "", title = "",
                      tone = "", channel_id = None,
                      bavarian = False, gid = None, uid = None):
        """Schickt die Nutzerfrage ans LLM und fuehrt bei Bedarf Werkzeuge aus.

        Hat der Nutzer im Shop einen Titel gekauft (title), wird Flo angewiesen, ihn
        mit diesem Titel anzusprechen. 'tone' steuert die Gelassenheit: je seltener
        der Titel, desto entspannter/chilliger spricht Flo (kommt aus economy).
        'channel_id' bringt den juengsten Gespraechsverlauf als Kontext mit, damit
        Flo dem Gespraech folgen kann (Kurzzeit-Gedaechtnis).

        Gibt IMMER einen Satz zurueck - nie None, nie leer. Wo das Modell
        danebenliegt, wird genau EINMAL nachgefasst:
          - leer / abgeschnitten (Denken hat das Budget gefressen) -> mit mehr Budget
          - Groq-400 'tool_use_failed' -> nochmal, dann ohne Werkzeug
          - Verweigerung -> nochmal wie vorher, sonst derbe Abwehr
        Alles zusammen innerhalb von KI_FRIST."""
        if self._client is None:
            return "Mein KI-Modus ist gerade nicht eingerichtet."

        frist = time.monotonic() + self.KI_FRIST
        text = user_message.strip()
        if author:
            text = f"{author} schreibt: {text}"

        history = self._recent(channel_id, skip_content=user_message.strip())
        messages = [
            {"role": "system",
             "content": self._system_prompt(author, title, tone, bavarian, gid, uid)},
            *history,
            {"role": "user", "content": text},
        ]

        nachgefasst = set()          # welche Pannen schon EINEN zweiten Versuch hatten
        mit_werkzeug = True
        budget = self.MAX_TOKENS
        try:
            for _ in range(self.MAX_STEPS):
                try:
                    if mit_werkzeug:
                        response = await self._chat(
                            messages=messages,
                            tools=[self.WEATHER_TOOL],
                            max_tokens=budget,
                            temperature=self.TEMPERATURE,
                            frist=frist,
                        )
                    else:
                        # Ohne Werkzeug - und das Modell muss es WISSEN: der
                        # System-Prompt erwaehnt get_weather, und ein Aufruf eines
                        # nicht mitgeschickten Werkzeugs ist bei Groq wieder ein 400.
                        response = await self._chat(
                            messages=messages + [{
                                "role": "system",
                                "content": "Du hast gerade KEIN Werkzeug. Antworte "
                                           "einfach in Worten."}],
                            max_tokens=budget,
                            temperature=self.TEMPERATURE,
                            frist=frist,
                        )
                except LlmFehler as fehler:
                    if fehler.art != "werkzeug":
                        raise
                    if "werkzeug" not in nachgefasst:
                        nachgefasst.add("werkzeug")         # Zufall - nochmal wie eben
                        continue
                    if mit_werkzeug:
                        mit_werkzeug = False                 # dann eben ohne
                        continue
                    raise
                msg = response.choices[0].message
                tool_calls = getattr(msg, "tool_calls", None)
                content = msg.content or ""

                if not tool_calls:
                    # Hat das Modell den Tool-Aufruf faelschlich als TEXT ausgegeben
                    # (z. B. '<function=get_weather>{"city":"X"}</function>')? Dann echt
                    # ausfuehren und in eine saubere Runde zurueckgeben - statt die
                    # Roh-Syntax anzuzeigen.
                    inline = self._extract_inline_tool_calls(content)
                    if inline:
                        messages.append({"role": "assistant",
                                         "content": self._sanitize_output(content)})
                        for name, argstr in inline:
                            result = await self._run_tool(name, argstr)
                            messages.append({
                                "role": "user",
                                "content": (f"[System] Ergebnis von {name}({argstr}): "
                                            f"{json.dumps(result, ensure_ascii=False)}. "
                                            f"Antworte dem Nutzer jetzt normal in Worten - "
                                            f"KEINE Werkzeug-Syntax, kein <function=...>.")})
                        continue
                    sauber = self._sanitize_output(content)
                    fertig = getattr(response.choices[0], "finish_reason", None)
                    if self._ist_verweigerung(sauber):
                        log.warning("KI-Fehler: Verweigerung (%s): %r",
                                    self._befund(response), sauber[:80])
                        if "verweigerung" not in nachgefasst:
                            nachgefasst.add("verweigerung")
                            continue
                        return random.choice(self._ABWEHR)
                    if not sauber or (fertig == "length" and not self._satzende(sauber)):
                        # Bei Denk-Modellen ist das Budget beim Nachdenken
                        # draufgegangen. Vorher hiess das fuer den Nutzer
                        # "Dazu faellt mir gerade nichts ein" - und fuer den
                        # Verlauf auch. Jetzt: einmal mit mehr Luft.
                        log.warning("KI-Fehler: %s Antwort (%s)",
                                    "leere" if not sauber else "abgeschnittene",
                                    self._befund(response, budget))
                        if "leer" not in nachgefasst:
                            nachgefasst.add("leer")
                            budget = max(budget, self.MAX_TOKENS_NOTFALL)
                            continue
                        if sauber:
                            return self._kuerzen(sauber)
                        return random.choice(self._LEER_SPRUECHE)
                    return self._kuerzen(sauber)

                # Assistant-Nachricht mit den Tool-Aufrufen sauber zurueckschreiben.
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in tool_calls
                        ],
                    }
                )
                # Jedes Werkzeug ausfuehren und das Ergebnis zurueckgeben.
                for tc in tool_calls:
                    result = await self._run_tool(tc.function.name, tc.function.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
        except LlmFehler as fehler:
            return self.fehlertext(fehler)       # je Ursache ein anderer Satz
        except Exception:  # noqa: BLE001 - Discord-Bot soll nie wegen LLM-Fehler crashen
            log.exception("LLM-Aufruf unerwartet gescheitert")
            return self.MELDUNGEN["unbekannt"]

        return "Das war mir gerade zu kompliziert - frag mich nochmal einfacher."

    async def see_image(self, user_message, image_url, *, author = "",
                        title = "", tone = "",
                        channel_id = None, bavarian = False,
                        gid = None, uid = None):
        """Schaut sich ein Bild an (Vision-Modell) und antwortet in Flos Persoenlichkeit.
        image_url = oeffentliche URL (z. B. Discord-Anhang) oder data:-URL.

        Kommt vom Vision-Modell nichts Brauchbares (leer, abgelehnte Anfrage,
        Modell weg), antwortet Flo auf den TEXT - lieber ohne Bild als gar nicht."""
        if self._client is None:
            return "Mein KI-Modus ist gerade nicht eingerichtet."

        frist = time.monotonic() + self.KI_FRIST
        text = (user_message or "").strip() or "Schau dir das Bild an und sag was dazu."
        if author:
            text = f"{author} schreibt: {text}"
        history = self._recent(channel_id, skip_content=(user_message or "").strip())
        messages = [
            {"role": "system",
             "content": self._system_prompt(author, title, tone, bavarian, gid, uid)},
            *history,
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ]
        budget = self.MAX_TOKENS_BILD
        ohne_bild = False
        try:
            for _versuch in range(2):
                response = await self._chat(
                    vision=True,
                    messages=messages,
                    max_tokens=budget,
                    temperature=self.TEMPERATURE,
                    frist=frist,
                )
                sauber = self._sanitize_output(self._inhalt(response))
                fertig = getattr(response.choices[0], "finish_reason", None)
                if sauber and not self._ist_verweigerung(sauber) and not (
                        fertig == "length" and not self._satzende(sauber)):
                    return self._kuerzen(sauber)
                log.warning("KI-Fehler: Bild-Antwort unbrauchbar (%s): %r",
                            self._befund(response, budget), sauber[:60])
                budget = max(budget, self.MAX_TOKENS_NOTFALL)
            ohne_bild = True
        except LlmFehler as fehler:
            if fehler.art not in ("anfrage", "modell", "werkzeug"):
                return self.fehlertext(fehler)
            ohne_bild = True
        except Exception:  # noqa: BLE001
            log.exception("Vision-Aufruf unerwartet gescheitert")
            return self.MELDUNGEN["unbekannt"]
        if ohne_bild:
            log.warning("KI: Bild nicht lesbar - antworte auf den Text.")
            frage = (user_message or "").strip() or "Ich hab dir ein Bild geschickt."
            return await self.ask_flo(
                f"{frage} [Das Bild dazu konntest du nicht sehen.]", author=author,
                title=title, tone=tone, channel_id=channel_id, bavarian=bavarian,
                gid=gid, uid=uid)
        return random.choice(self._LEER_SPRUECHE)

    async def see_image_raw(self, prompt, image_url, *, temperature = 0.3,
                            max_tokens = 500):
        """Nuechterner Vision-Aufruf OHNE Persona/Verlauf - fuer strukturierte
        Analysen (z. B. JSON). Gibt den rohen Text zurueck oder None bei Fehler."""
        if self._client is None:
            return None
        try:
            response = await self._chat(
                vision=True,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return (response.choices[0].message.content or "").strip() or None
        except LlmFehler:
            return None                          # Grund steht schon einzeilig im Log
        except Exception:  # noqa: BLE001
            log.exception("Vision-Raw-Aufruf unerwartet gescheitert")
            return None


# --- Singleton + Modul-API -------------------------------------------------
# Eine Instanz pro Prozess; die bisherigen Modul-Aufrufe (ai.setup(), ai.ask_flo()
# usw.) funktionieren ueber die Aliase unveraendert weiter.
instance = FloAI()

DEFAULT_BASE_URL = FloAI.DEFAULT_BASE_URL
selbsttest = instance.selbsttest
fehlertext = instance.fehlertext
DEFAULT_MODEL = FloAI.DEFAULT_MODEL
DEFAULT_VISION_MODEL = FloAI.DEFAULT_VISION_MODEL
MAX_STEPS = FloAI.MAX_STEPS
MAX_TOKENS = FloAI.MAX_TOKENS
MAX_TOKENS_BILD = FloAI.MAX_TOKENS_BILD
WMO_CODES = FloAI.WMO_CODES
WEATHER_TOOL = FloAI.WEATHER_TOOL

setup = instance.setup
is_enabled = instance.is_enabled
bot_name = instance.bot_name
names = instance.names
trigger_re = instance.trigger_re
lead_re = instance.lead_re
strip_lead = instance.strip_lead
praefix_von = instance.praefix_von
praefix_geaendert = instance.praefix_geaendert
setze_guild = instance.setze_guild
guild_zuruecksetzen = instance.guild_zuruecksetzen
aktuelle_guild = instance.aktuelle_guild
guild_kontext = FloAI.guild_kontext
http_session = instance.http_session
get_weather = instance.get_weather
generate = instance.generate
note_message = instance.note_message
ask_flo = instance.ask_flo
see_image = instance.see_image
see_image_raw = instance.see_image_raw
