"""Arbeit: Coins verdienen, ohne zu zocken.

Casino und Aktie sind Glueck bzw. Timing. Hier gibt es den dritten Weg: echte
Schichten mit echten Aufgaben, bei denen KOENNEN zaehlt.

    Flo work                 Schicht antreten (zufaellige Aufgabe)
    Flo work wordle          gezielt eine bestimmte Schicht
    Flo work liste           welche Schichten es gibt
    Flo lohnzettel           eigene Bilanz: Schichten, Serie, Verdienst
    Flo wordle               zum Wort des Tages (wenn eins laeuft)

Fuenf Schichten, alle mit Knoepfen bzw. Eingabefeld:

    wordle      Fuenf Buchstaben, sechs Versuche - die Koenigsdisziplin
    salat       Buchstabensalat entwirren
    rechnen     Rechenschicht: fuenf Aufgaben, jede falsche kostet den Rest
    safe        Zahlenschloss knacken (Hinweise nach jedem Versuch)
    sortieren   Schichtplan ordnen - ein Fehlklick und die Schicht ist rum

WORT DES TAGES - das grosse Ding:
Einmal am Tag legt Flo ein Wort in den dafuer eingestellten Kanal. Das ist ein
WETTRENNEN: wer es als Erster knackt, nimmt den ganzen Topf, danach ist die
Runde durch und das Wort wird aufgeloest. Der Topf haengt an der Wortlaenge,
und die waechst zum Wochenende hin:

    Mo-Do  5 Buchstaben    50.000 Grundtopf
    Fr     6 Buchstaben    60.000
    Sa     7 Buchstaben    70.000
    So     8 Buchstaben    80.000   (Zahltag)

Mal Versuchs-Faktor: beim ersten Versuch das Doppelte, beim sechsten nur den
Grundtopf. Acht Buchstaben auf Anhieb sind also 160.000.

WANN es faellt, entscheidet der Server selbst - nicht die Uhr: Flo wartet, bis
mindestens `wordle_min_voice` Leute (Standard 3) in einem Sprachkanal sitzen.
Ist an einem Tag nie was los, faellt das Wort an dem Tag eben aus. Ein Raetsel
um 4 Uhr morgens in einen leeren Server zu werfen, waere verschenkt.

Die Wordle-SCHICHT aus 'Flo work' zahlt bewusst nur einen Bruchteil davon - das
Wort des Tages soll der Hoehepunkt bleiben, nicht die Nebensache.

Alle Coins laufen ueber economy (ein Topf, ein Handelsbuch). Dieses Modul haelt
nur Cooldowns, Serien und den Stand des Tages in data/arbeit.json.
"""

import asyncio
import hashlib
import logging
import os
import random
import time
from datetime import date, datetime

import discord

import ai
from basis import FeatureBasis, erstes_ziel
import economy
import numfmt
from store import JsonStore

log = logging.getLogger("dcbot.arbeit")

# Sentinel: wir haben selbst geantwortet -> bot.py schweigt.
HANDLED = object()

# --- Befehlswoerter ---------------------------------------------------------
_CMDS = ("work", "arbeit", "arbeiten", "job", "schicht", "malochen")
_LOHN_CMDS = ("lohnzettel", "lohn", "gehalt", "arbeitszeugnis")
_TOP_CMDS = ("top", "rangliste", "bestenliste", "leaderboard", "werk")
# Spass-Wordle: die Tippfehler, die Leute WIRKLICH machen, gleich mit drin.
# cmdnorm faengt zwar Vertipper ab, aber nur bei Woertern, die es kennt - und
# nur einen Fehler. 'wordl' und 'wordel' sind so haeufig, dass sie hier direkt
# stehen.
# 'wordly' stand hier mal mit drin und ist wieder raus: es ist kein deutscher
# Vertipper, aber das englische 'worldly' ist davon eine Loeschung entfernt -
# damit waere aus "Flo worldly wisdom" ein Wordle geworden.
_WORDLE_CMDS = ("wordle", "wordl", "wordel", "worlde", "wörtle",
                "woertle", "wortle", "wörtel")
# Das Wort des Tages hat EIGENE Woerter - sonst waere nicht klar, ob man um
# 50.000 spielt oder um Spass. 'daily' ist bewusst NICHT dabei: das ist seit
# immer der Tagesbonus aus economy.
_TAGES_CMDS = ("tageswort", "tageswordle", "wortdestages", "tagesraetsel",
               "tagesrätsel", "worddaily", "dailyword")

# --- Balance ----------------------------------------------------------------
# Cooldown zwischen zwei Schichten. 15 Minuten: lang genug, dass niemand den
# ganzen Abend nur klickt, kurz genug, dass es sich nach Arbeit anfuehlt und
# nicht nach Warten.
COOLDOWN = int(os.getenv("ARBEIT_COOLDOWN", "900") or "900")
# Deckel pro Tag und Nase. Ohne den waere die Schicht die beste Geldquelle im
# Spiel - sie ist ja risikofrei, anders als Casino und Aktie.
TAGES_DECKEL = int(os.getenv("ARBEIT_TAGESDECKEL", "250000") or "250000")
# Serie: jede geschaffte Schicht in Folge legt 5 % drauf, gedeckelt bei +50 %.
# Ein Reinfall setzt zurueck - deshalb lohnt es sich, die schwere Schicht auch
# wirklich zu Ende zu bringen, statt sie wegzuklicken.
SERIE_SCHRITT = 0.05
SERIE_MAX = 0.50

# --- Spass-Wordle -----------------------------------------------------------
# Jederzeit spielbar, ohne auf den Voice-Kanal oder die Schicht-Pause zu warten.
# Damit das die Wirtschaft nicht aushebelt, gilt ein HARTER Deckel je Runde und
# ein eigener, kurzer Cooldown: es soll Spass machen, nicht die Haupteinnahme
# sein. Das Wort des Tages (50.000-160.000) und die seltene Wordle-Schicht
# bleiben klar darueber.
SPASS_MAX = int(os.getenv("WORDLE_SPASS_MAX", "15000") or "15000")
SPASS_JE_BUCHSTABE = int(os.getenv("WORDLE_SPASS_JE_BUCHSTABE", "2000") or "2000")
SPASS_COOLDOWN = int(os.getenv("WORDLE_SPASS_COOLDOWN", "120") or "120")

# --- Wort des Tages ---------------------------------------------------------
TAGES_PRO_BUCHSTABE = int(os.getenv("WORDLE_PRO_BUCHSTABE", "10000") or "10000")
# Wortlaenge: WUERFELT je Tag und Server, nicht am Wochentag festgemacht.
# Vorher war Mo-Do immer 5 Buchstaben - vier Tage die Woche dieselbe Aufgabe
# und derselbe Topf, das nutzt sich ab. Jetzt weiss man morgens nicht, was
# kommt: ein kurzes Wort mit kleinem Topf oder acht Buchstaben fuer 80.000.
# Am Wochenende verschiebt sich das Gewicht nach oben - lange Woerter werden
# dann wahrscheinlicher, ohne dass es garantiert waere.
TAGES_LAENGEN = (5, 6, 7, 8)
TAGES_GEWICHT_WOCHE = (34, 30, 21, 15)      # Montag bis Freitag
TAGES_GEWICHT_WOCHENENDE = (16, 26, 30, 28)  # Samstag und Sonntag
# Je weniger Versuche, desto mehr. Schluessel = gebrauchte Versuche.
VERSUCH_FAKTOR = {1: 2.00, 2: 1.75, 3: 1.50, 4: 1.30, 5: 1.15, 6: 1.00}
MAX_VERSUCHE = 6
# So viele Leute muessen im Voice sitzen, damit sich das Raetsel lohnt.
STANDARD_MIN_VOICE = 3
# Sind genug Leute da, faellt das Wort NICHT sofort, sondern irgendwann in den
# naechsten Minuten. Zwei Gruende: es soll ueberraschen statt vorhersagbar an
# der dritten Person zu haengen ("gleich kommt das Wordle, ich mach den Call
# voll"), und wer zufaellig gerade auf den Kanal schaut, hat keinen Vorsprung.
VERZUG_MIN = int(os.getenv("WORDLE_VERZUG_MIN", "300") or "300")      # 5 Min
VERZUG_MAX = int(os.getenv("WORDLE_VERZUG_MAX", "2700") or "2700")    # 45 Min

# Wie der Kanal gefunden wird, wenn nichts eingestellt ist: ein Kanal, der so
# heisst. Das trifft den Normalfall, ohne eine ID in den Code zu schreiben -
# eingestellt wird es trotzdem lieber (guildcfg 'wordle_channel').
KANAL_NAMEN = ("gigachat", "giga-chat", "wordle", "allgemein", "general")

# --- Woerter ----------------------------------------------------------------
# Bewusst OHNE Umlaute und ohne ss/sz: bei einem Buchstabenspiel muss jeder
# Buchstabe eindeutig eintippbar sein - 'AE' fuer 'Ä' waere geraten.
# test_arbeit_woerter prueft Laenge und Zeichenvorrat JEDES Eintrags.
WOERTER = {
    5: ("ABEND ADLER AKTIE ALARM AMPEL ANKER APFEL ARENA ATLAS AUGEN BADEN "
        "BEBEN BEERE BERGE BESEN BIENE BIRNE BLATT BLICK BLOCK BLUME BODEN "
        "BOGEN BOHNE BOMBE BRAND BRAUT BRIEF BROTE BRUCH BUCHE BUCHT DACHS "
        "DAMEN DAMPF DATEN DAUER DECKE DEGEN DIEBE DINGE DOSEN DRAHT DRUCK "
        "DUNST EBENE ECKEN EIMER EISEN ENTEN ERBSE ERNTE ESSEN EULEN FABEL "
        "FADEN FAHNE FALKE FALLE FARBE FASAN FEDER FEIER FERNE FEUER FILME "
        "FIRMA FISCH FLUCH FOLGE FORUM FRAGE FRIST FUNKE GABEL GASSE GEIGE "
        "GEIST GEMSE GENIE GESTE GLANZ GLEIS GNADE GURKE HAFEN HAKEN HALLE "
        "HASEN HAUPT HEBEL HECKE HEIDE HERDE HEUTE HOBEL HONIG HORDE HOSEN "
        "HUNDE INSEL JACKE JAHRE JUNGE JUWEL KABEL KAMEL KAMIN KAMPF KANAL "
        "KANTE KARTE KASSE KATZE KEGEL KELCH KERZE KETTE KIOSK KISTE KLAGE "
        "KLANG KLEID KLIMA KOHLE KOMET KOPIE KRAFT KRANZ KRAUT KREIS KREUZ "
        "KRIEG KRONE KUGEL KUNDE KUNST KURVE LADEN LAGER LAMPE LANZE LASER "
        "LAUBE LEBEN LEDER LEHRE LEUTE LICHT LIEBE LINDE LINIE LINSE LISTE "
        "LOCKE LOGIK MAGEN MAGIE MAKEL MANGO MARKE MARKT MASSE MAUER MEILE "
        "MENGE MESSE METER MIETE MILCH MINEN MITTE MODUS MONAT MOTOR MOTTE "
        "MUSIK NABEL NACHT NADEL NAGEL NAMEN NASEN NEBEL NEFFE NETZE NIERE "
        "NOTEN NUDEL OASEN OCHSE OLIVE ONKEL OPFER ORDEN ORGEL OSTEN PAKET "
        "PALME PAPST PARTY PAUSE PEDAL PERLE PFAHL PFEIL PFERD PFLUG PILOT "
        "PILZE PISTE PLANE PLATZ POKAL PROBE PUDEL PULLI PUNKT PUPPE QUARZ "
        "QUOTE RADIO RASEN RATEN RAUCH RAUPE REGAL REGEL REGEN REIFE REIHE "
        "REISE RENTE RIESE RINGE RIPPE ROBBE ROLLE ROMAN ROSEN RUDER RUINE "
        "RUNDE SALAT SALBE SAMEN SAUCE SCHAF SCHAL SCHUH SEELE SEGEL SEIFE "
        "SEITE SEKTE SILBE SINNE SIRUP SOCKE SOHLE SONNE SORTE SPALT SPIEL "
        "SPORT SPULE STADT STAHL STAMM STAUB STEIN STERN STIFT STIRN STOCK "
        "STOFF STOLZ STROM STUBE STUFE STUHL STURM SUCHE SUPPE TAFEL TAGEN "
        "TANNE TANTE TASSE TASTE TAUBE TEICH TEILE TEMPO TIEFE TIERE TINTE "
        "TISCH TITEL TRAUM TREUE TRICK TRUHE TULPE TURBO UHREN VASEN VATER "
        "VIDEO VOGEL WAAGE WACHE WAGEN WALZE WANNE WAREN WEIDE WEINE WEISE "
        "WELLE WERFT WERKE WERTE WESPE WESTE WETTE WIESE WILLE WINDE WITWE "
        "WOCHE WOLKE WOLLE WORTE WUNDE WURST ZANGE ZEBRA ZEHEN ZEILE ZELTE "
        "ZIEGE ZIELE ZITAT ZUNGE ZWERG").split(),
    6: (
        "ABLAGE ARBEIT BAGGER BALKEN BILDER BIRNEN BLENDE BLITZE BRUDER "
        "DECKEL DIENST DONNER DRACHE DRUCKE EIMERN ERNTEN FEHLER FELDER "
        "FELSEN FINGER FLAGGE FLIEGE FLOSSE FREUND FROSCH FRUCHT GARTEN "
        "GEIGEN GELDER GERADE GIPFEL GLOCKE GRABEN GRENZE GRUPPE HAMMER "
        "HANDEL HEMDEN HERBST HERREN HIMMEL HIRSCH INSEKT KELLER KIESEL "
        "KINDER KIRCHE KLASSE KLINGE KNOTEN KOCHEN KOFFER KUCHEN LEITER "
        "LERCHE LIEDER MANTEL MENSCH MUTTER NERVEN NESTER NORDEN NUMMER "
        "NUTZEN OBERST PORTAL POSTEN PULVER QUELLE RAHMEN RAKETE REVIER "
        "RINDER RISIKO RITTER SCHIFF SCHLAF SCHNEE SENDER SESSEL SIEBEN "
        "SOMMER SPRUNG STRAND STUNDE TASCHE TELLER TERMIN TEUFEL TICKET "
        "UNFALL URLAUB URTEIL VENTIL VERBOT WASSER WEIHER WELTEN WINKEL "
        "WINTER WISSEN WURZEL ZAUBER ZEIGER ZEITEN ZIMMER ZIRKEL ZUCKER"
        ).split(),
    7: (
        "ANGEBOT AUSGABE BAHNHOF BALKONE BEAMTER BRIEFEN DIENSTE EINKAUF "
        "ELEFANT FABRIKS FAHRRAD FENSTER FISCHER FLASCHE FORELLE FREITAG "
        "GEBIRGE GEDANKE GITARRE GLOCKEN HAFTUNG HANDELN HAUSTUR HEIMWEG "
        "JOURNAL KAMERAS KAPITEL KARTONS KELLNER KIRSCHE KLASSEN KLAVIER "
        "KNOCHEN KOMPASS KONTAKT KONZERT LIEFERN MASCHEN MEDIZIN MEISTER "
        "MINUTEN MOMENTE MONTAGE MOTOREN MUSTERN NACHBAR PAKETEN PFLANZE "
        "POLIZEI PORTALE PROBLEM PRODUKT PROJEKT QUELLEN RAKETEN REZEPTE "
        "RICHTER ROBOTER SAMSTAG SCHIENE SCHLOSS SCHNITT SCHRANK SCHRIFT "
        "SEKUNDE SIGNALE SONNTAG SPIEGEL SPRACHE STATION STIEFEL STRASSE "
        "SYSTEME TABLETT TASCHEN TELEFON TEPPICH THEATER TOCHTER TROMMEL "
        "URLAUBE VERKEHR VERTRAG VIERTEL VITRINE VORHANG WAGGONS WANDERN "
        "WOHNUNG ZEITUNG ZENTRUM ZWIEBEL"
        ).split(),
    8: (
        "ANGEBOTE ARBEITER AUSGABEN BAHNHOFE BEISPIEL DIENSTAG DOKTOREN "
        "FABRIKEN FEIERTAG FENSTERN FLASCHEN FLUGZEUG FORELLEN FREITAGE "
        "FUSSBALL GARDINEN GEBURTEN GEDANKEN GEFAHREN GEMEINDE GESCHENK "
        "GESCHIRR GEWITTER HANDTUCH HANDWERK HAUSTIER JOURNALE KAPITELN "
        "KAROTTEN KINDHEIT KIRSCHEN KLAVIERE KOMPASSE KONTAKTE KONZERTE "
        "LANDWIRT LEHRLING MASCHINE MEISTERN MITTWOCH MONTAGEN NACHBARN "
        "PFLANZEN PLANETEN POLIZIST PRODUKTE PROJEKTE REGISTER ROBOTERN "
        "SAMSTAGE SCHATTEN SCHIENEN SCHNITTE SCHRANKE SCHULTER SEKUNDEN "
        "SPRACHEN STRASSEN TABLETTS TELEFONE TEPPICHE TERRASSE TOCHTERN "
        "TROMMELN VERTRAGE VIERTELN VORHANGE WANDERER WERKZEUG ZWIEBELN"
        ).split(),
}

# --- Karriere ---------------------------------------------------------------
# Das Rueckgrat der ganzen Sache. Vorher gab es nur die Serie: ein Reinfall und
# alles war weg, ueber Wochen baute man NICHTS auf. Die Stufe zaehlt dagegen
# GESCHAFFTE Schichten und geht nie zurueck - wer dranbleibt, verdient dauerhaft
# mehr, und es gibt einen Grund, morgen wiederzukommen.
class Stufe:
    """Eine Karrierestufe: ab wie vielen geschafften Schichten, wie viel mehr."""

    def __init__(self, ab, titel, bonus, symbol):
        self.ab = int(ab)
        self.titel = titel
        self.bonus = float(bonus)     # +Anteil auf den Lohn
        self.symbol = symbol

    def __repr__(self):
        return f"<Stufe {self.titel} ab {self.ab}>"


# Absichtlich flach am Anfang (die erste Stufe ist nach zehn Schichten drin) und
# steil am Ende - der Werksleiter soll etwas bedeuten.
STUFEN = (
    Stufe(0, "Praktikant", 0.00, "🧻"),
    Stufe(10, "Aushilfe", 0.08, "🧹"),
    Stufe(30, "Facharbeiter", 0.16, "🔧"),
    Stufe(75, "Vorarbeiter", 0.25, "📋"),
    Stufe(150, "Schichtleiter", 0.34, "🎖️"),
    Stufe(300, "Meister", 0.42, "🏅"),
    Stufe(600, "Werksleiter", 0.50, "👑"),
)


def stufe_fuer(geschafft):
    """Die aktuelle Stufe zu einer Zahl geschaffter Schichten."""
    aktuell = STUFEN[0]
    for st in STUFEN:
        if int(geschafft or 0) >= st.ab:
            aktuell = st
    return aktuell


def naechste_stufe(geschafft):
    """Die naechste Stufe - oder None, wenn oben angekommen."""
    for st in STUFEN:
        if int(geschafft or 0) < st.ab:
            return st
    return None


# --- Goldene Schicht --------------------------------------------------------
# Jede Schicht kann golden sein: doppelter Lohn. Reiner Zufall, klar markiert -
# das ist der kleine Kick, der auch die dreissigste Rechenschicht interessant
# macht, ohne dass man dafuer etwas koennen muesste.
GOLD_CHANCE = float(os.getenv("ARBEIT_GOLD_CHANCE", "0.08") or "0.08")
GOLD_FAKTOR = 2.0

# --- Flavor -----------------------------------------------------------------
_SCHICHT_START = ["Anwesenheit notiert. Los.", "Stempelkarte durch. Ich schau zu.",
                  "Na dann zeig mal, was du kannst.", "Schicht läuft. Trödel nicht rum.",
                  "Arbeitszeit. Ausnahmsweise."]
_LOB = ["Sauber. Hätte ich dir nicht zugetraut.", "Geht doch. Manchmal.",
        "Ordentlich. Nicht dran gewöhnen.", "Abgehakt. Kasse stimmt.",
        "Fertig. War ja auch nicht die Weltformel."]
_TADEL = ["Das war nichts. Schicht vorbei.", "Rausgeschmissene Zeit – meine.",
          "Schön daneben. Kein Lohn.", "Feierabend. Unbezahlt.",
          "Das nennt man Fehlschicht."]


def _heute():
    """Datum als Text - eine Stelle, damit Tests sie umbiegen koennen."""
    return date.today().isoformat()


def _muenzen(n):
    return numfmt.fmt(int(n))


def laenge_des_tages(tag, wurf):
    """Wie lang ist das Wort heute? Gewichtet gezogen, aber reproduzierbar.

    'wurf' ist eine beliebig grosse Zahl (kommt aus dem Tages-Hash) - damit
    faellt an demselben Tag auf demselben Server immer dieselbe Laenge, auch
    nach einem Neustart. Am Wochenende sind lange Woerter wahrscheinlicher,
    aber nie sicher: das Wuerfeln ist ja der Punkt."""
    try:
        wochenende = datetime.strptime(tag, "%Y-%m-%d").weekday() >= 5
    except (TypeError, ValueError):
        wochenende = False
    gewichte = TAGES_GEWICHT_WOCHENENDE if wochenende else TAGES_GEWICHT_WOCHE
    ziel = int(wurf) % sum(gewichte)
    summe = 0
    for laenge, gewicht in zip(TAGES_LAENGEN, gewichte):
        summe += gewicht
        if ziel < summe:
            return laenge
    return TAGES_LAENGEN[-1]


def _schuetzen(message):
    """Meldet eine laufende Arbeit beim Auto-Loesch-Schutz an.

    OHNE das verschwindet eine Schicht in einem Aufraeum-Kanal MITTEN IM SPIEL -
    der Cooldown laeuft, der Lohn ist weg, und die Knoepfe zeigen ins Leere.
    Lazy-Import von bot, um Zirkel zu vermeiden (wie casino/games es machen)."""
    if message is None:
        return
    try:
        import bot
        bot.protect_message(message)
    except Exception:  # noqa: BLE001 - ohne bot (Tests) passiert eben nichts
        pass


def _freigeben(message):
    """Arbeit vorbei -> nach kurzer Gnadenfrist darf aufgeraeumt werden."""
    if message is None:
        return
    try:
        import bot
        bot.release_message(message)
    except Exception:  # noqa: BLE001
        pass


async def _wordle_bild(spiel, titel, untertitel="", *, verdeckt=False, loesung=""):
    """Das Rate-Brett als Bild-Anhang. (discord.File, Dateiname) - oder (None, "").

    Das Brett war vorher eine Emoji-Zeile mit Buchstaben dahinter. Lesbar war das
    nur mit Muehe: welcher Buchstabe zu welchem Kaestchen gehoert, musste man
    abzaehlen, und welche Buchstaben schon raus sind, musste man sich merken.
    Jetzt ist es ein echtes Brett mit Tastatur darunter.

    Gezeichnet wird im THREAD: PIL rechnet in C und gibt den Event-Loop nicht
    ab - waehrend Flo ein Brett malt, stuende sonst der ganze Bot."""
    try:
        import render
        buf = await asyncio.to_thread(
            render.wordle_board,
            spiel.zeilen(), spiel.laenge, max_versuche=spiel.max_versuche,
            titel=titel, untertitel=untertitel, tastatur=spiel.tastatur(),
            verdeckt=verdeckt, loesung=loesung)
        name = f"wordle{int(time.time() * 1000) % 10**9}.png"
        return discord.File(buf, filename=name), name
    except Exception:  # noqa: BLE001 - ein Bild ist nie spielentscheidend
        log.debug("Wordle-Brett liess sich nicht zeichnen", exc_info=True)
        return None, ""


# ===========================================================================
# Spiellogik - ohne jedes Discord
# ===========================================================================
class Wordle:
    """Ein Wordle-Spiel: Loesung, Versuche, Faerbung, Tafel.

    Bewusst ohne Discord-Bezug: dieselbe Klasse traegt die Wordle-Schicht aus
    'Flo work' UND das Wort des Tages. Testbar ohne Bot, ohne Netz."""

    def __init__(self, loesung, max_versuche=MAX_VERSUCHE, versuche=None):
        self.loesung = str(loesung).upper()
        self.max_versuche = int(max_versuche)
        self.versuche = [str(v).upper() for v in (versuche or [])]

    # --- Zustand ---
    @property
    def geloest(self):
        return bool(self.versuche) and self.versuche[-1] == self.loesung

    @property
    def aus(self):
        return not self.geloest and len(self.versuche) >= self.max_versuche

    @property
    def offen(self):
        return max(0, self.max_versuche - len(self.versuche))

    @property
    def laenge(self):
        return len(self.loesung)

    def passt(self, wort):
        """Ist das ueberhaupt ein zulaessiger Versuch?"""
        wort = str(wort or "").strip().upper()
        return len(wort) == self.laenge and wort.isalpha() and wort.isascii()

    def raten(self, wort):
        """Einen Versuch verbuchen. Gibt 'laenge' | 'weiter' | 'geloest' | 'aus'."""
        if self.geloest or self.aus:
            return "fertig"
        if not self.passt(wort):
            return "laenge"
        self.versuche.append(str(wort).strip().upper())
        if self.geloest:
            return "geloest"
        return "aus" if self.aus else "weiter"

    # --- Faerbung ---
    # Eine einzige Quelle fuer die Farben: farben() rechnet, muster() malt
    # Emojis daraus und das Bild in render.py nimmt dieselben Buchstaben.
    # Frueher haette jede Anzeige ihre eigene Rechnung gebraucht - und die
    # zweistufige Zaehlung unten wird garantiert an einer davon falsch.
    _EMOJI = {"g": "🟩", "y": "🟨", "b": "⬛"}

    def farben(self, versuch):
        """'g' (gruen), 'y' (gelb), 'b' (grau) je Stelle.

        Die ZWEISTUFIGE Zaehlweise ist der Kern und wird gern falsch gemacht:
        erst alle exakten Treffer wegnehmen, DANN die restlichen Buchstaben
        verteilen. Sonst faerbt 'OTTER' gegen 'NOTEN' beide T gelb, obwohl in
        der Loesung nur ein T steckt."""
        versuch = str(versuch).upper()
        farben = ["b"] * self.laenge
        rest = {}
        for i, richtig in enumerate(self.loesung):
            if i < len(versuch) and versuch[i] == richtig:
                farben[i] = "g"
            else:
                rest[richtig] = rest.get(richtig, 0) + 1
        for i in range(self.laenge):
            if farben[i] == "g" or i >= len(versuch):
                continue
            if rest.get(versuch[i], 0) > 0:
                farben[i] = "y"
                rest[versuch[i]] -= 1
        return "".join(farben)

    def muster(self, versuch):
        """Die gruen/gelb/grau-Zeile zu einem Versuch als Emojis."""
        return "".join(self._EMOJI[f] for f in self.farben(versuch))

    def zeilen(self):
        """(Wort, Farben) je Versuch - das, was das Bild zeichnet."""
        return [(v, self.farben(v)) for v in self.versuche]

    def tastatur(self):
        """Welcher Buchstabe ist durch? {Buchstabe: 'g'|'y'|'b'}.

        Die eigentliche Denkhilfe beim Wordle: ohne sie muss man sich merken,
        welche Buchstaben schon raus sind. Der beste Stand gewinnt - ein
        Buchstabe, der einmal gruen war, wird nie wieder grau."""
        rang = {"b": 0, "y": 1, "g": 2}
        stand = {}
        for wort, farben in self.zeilen():
            for buchstabe, farbe in zip(wort, farben):
                if rang[farbe] > rang.get(stand.get(buchstabe, "b"), -1) \
                        or buchstabe not in stand:
                    stand[buchstabe] = farbe
        return stand

    def tafel(self, verdeckt=False):
        """Das Rate-Bild. 'verdeckt' zeigt nur die Farben, nicht die Buchstaben -
        dafuer, wenn andere mitlesen duerfen sollen, ohne etwas zu erfahren."""
        zeilen = []
        for v in self.versuche:
            zeile = self.muster(v)
            if not verdeckt:
                zeile += f"  `{' '.join(v)}`"
            zeilen.append(zeile)
        zeilen += ["⬜" * self.laenge] * self.offen
        return "\n".join(zeilen)

    def lohnfaktor(self):
        """Wie gut war das? 1 Versuch = voll, danach weniger."""
        return VERSUCH_FAKTOR.get(len(self.versuche), 1.0)

    @classmethod
    def zufall(cls, laenge=5, **kw):
        return cls(random.choice(WOERTER[laenge]), **kw)

    @classmethod
    def des_tages(cls, gid, tag=None, laenge=None, versuche=None):
        """Das Wort eines Servers an einem Tag - BERECHNET, nicht gewuerfelt.

        Derselbe Server bekommt an demselben Tag immer dasselbe Wort, auch nach
        einem Neustart mitten im Rateverlauf. Und zwei Server bekommen
        verschiedene Woerter, damit man sich die Loesung nicht von nebenan holt."""
        tag = tag or _heute()
        # EIN Hash, zwei Entscheidungen: die hinteren Stellen ziehen die Laenge,
        # die vorderen das Wort. So bleibt beides reproduzierbar, ohne zwei
        # Hashes rechnen zu muessen - und die zwei Wuerfe beeinflussen sich
        # nicht (verschiedene Enden derselben Zufallszahl).
        roh = hashlib.sha256(f"{tag}:{int(gid or 0)}".encode()).hexdigest()
        if laenge is None:
            laenge = laenge_des_tages(tag, int(roh[-8:], 16))
        liste = WOERTER.get(laenge) or WOERTER[5]
        return cls(liste[int(roh[:32], 16) % len(liste)], versuche=versuche)


class Tagesraetsel:
    """Die Runde eines Tages auf EINEM Server - ein Wettrennen.

    Wer als Erster loest, nimmt den ganzen Topf; danach ist die Runde durch und
    das Wort wird aufgeloest. Das ist so gewollt: ein Rennen mit einem Sieger
    hat Spannung, ein Raetsel, das jeder in Ruhe nachholen kann, nicht.

    Der Zustand liegt als schlichtes dict in data/arbeit.json - diese Klasse ist
    die Sicht darauf, damit der Rest des Moduls nicht in fremden dicts wuehlt."""

    def __init__(self, gid, daten):
        self.gid = int(gid or 0)
        self.daten = daten          # lebende Referenz in den Store

    # --- Stammdaten ---
    @property
    def datum(self):
        return self.daten.get("datum") or ""

    @property
    def wort(self):
        return (self.daten.get("wort") or "").upper()

    @property
    def laeuft(self):
        return self.datum == _heute() and bool(self.wort)

    @property
    def gewinner(self):
        return int(self.daten.get("gewinner") or 0)

    @property
    def entschieden(self):
        return bool(self.gewinner)

    @property
    def topf(self):
        """Der Grundtopf - die Wortlaenge macht den Preis."""
        return len(self.wort) * TAGES_PRO_BUCHSTABE

    def spiel_von(self, uid):
        """Das Wordle EINER Person in dieser Runde."""
        versuche = (self.daten.get("spieler") or {}).get(str(int(uid))) or []
        return Wordle(self.wort, versuche=list(versuche))

    def gespielt(self, uid):
        return bool((self.daten.get("spieler") or {}).get(str(int(uid))))

    # --- Ablauf ---
    def starten(self):
        """Neue Runde anlegen (ueberschreibt die alte)."""
        spiel = Wordle.des_tages(self.gid)
        self.daten.clear()
        self.daten.update({"datum": _heute(), "wort": spiel.loesung,
                           "spieler": {}, "gewinner": 0, "versuche": 0,
                           "kanal": 0, "ansage": 0})
        return spiel

    # --- Der Zufalls-Termin ---
    @property
    def geplant_fuer(self):
        """Ab wann darf das Wort fallen? None = fuer heute noch kein Termin.

        Bewusst None und nicht 0: '0' ist ein gueltiger Zeitpunkt (und in Tests
        genau der, den man setzt, um sofort zu feuern). Mit 0 als 'kein Termin'
        haette Flo den Plan bei jedem Tick neu gezogen und das Wort waere nie
        gefallen."""
        if self.daten.get("plan_tag") != _heute():
            return None
        zeit = self.daten.get("plan_zeit")
        return int(zeit) if zeit is not None else None

    def termin_setzen(self, jetzt=None, wuerfel=None):
        """Merkt sich einen Zeitpunkt in den naechsten Minuten.

        Wird genau EINMAL pro Tag gesetzt: waere es bei jedem Tick neu
        gewuerfelt, ruecke der Termin staendig weiter weg und das Wort kaeme
        nie. Der Termin ueberlebt einen Neustart, weil er im Store steht."""
        jetzt = int(jetzt if jetzt is not None else time.time())
        spanne = max(0, VERZUG_MAX - VERZUG_MIN)
        wurf = wuerfel if wuerfel is not None else random.random()
        self.daten["plan_tag"] = _heute()
        self.daten["plan_zeit"] = jetzt + VERZUG_MIN + int(wurf * spanne)
        return self.daten["plan_zeit"]

    def ansage_merken(self, message):
        """Wo die oeffentliche Ansage steht.

        Noetig, weil das Raten ueber ein Eingabefeld laeuft: bei einer
        Modal-Antwort ist interaction.message IMMER None. Ohne diese zwei IDs
        koennte die Ansage nach dem Sieg nicht auf 'entschieden' umgestellt
        werden - sie stuende bis morgen da und lockte Leute in ein Rennen, das
        laengst gelaufen ist."""
        self.daten["kanal"] = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
        self.daten["ansage"] = int(getattr(message, "id", 0) or 0)

    def raten(self, uid, wort):
        """Ein Versuch. Gibt (status, spiel) zurueck.

        status: 'kein_wort' | 'fertig' (diese Person ist durch) | 'laenge' |
        'weiter' | 'geloest' | 'aus'.

        WICHTIG: der Sieger wird nur gesetzt, wenn noch keiner drinsteht. Wer
        nach der Entscheidung noch loest, darf das - er nimmt dem Ersten aber
        nichts weg. Ob Geld fliesst, entscheidet der Aufrufer daran, ob DIESE
        Person der Gewinner ist."""
        if not self.laeuft:
            return "kein_wort", None
        spiel = self.spiel_von(uid)
        status = spiel.raten(wort)
        if status in ("laenge", "fertig"):
            return status, spiel
        self.daten.setdefault("spieler", {})[str(int(uid))] = list(spiel.versuche)
        if status == "geloest" and not self.entschieden:
            self.daten["gewinner"] = int(uid)
            self.daten["versuche"] = len(spiel.versuche)
        return status, spiel

    def preis(self, spiel):
        """Was der Sieg wert ist: Topf mal Versuchs-Faktor."""
        return int(round(self.topf * spiel.lohnfaktor()))


# ===========================================================================
# Schichten - eine Klasse je Aufgabe
# ===========================================================================
class Schicht:
    """Basis fuer alles, was 'Flo work' anbieten kann.

    Eine Unterklasse beschreibt sich selbst (key/titel/lohn/was) und baut ihr
    Discord-Gesicht in `bauen`. Eine sechste Schicht ist damit EINE neue Klasse
    plus ein Eintrag in SCHICHTEN - kein Anfassen von handle(), Katalog oder
    Abrechnung."""

    key = "?"
    titel = "Schicht"
    was = ""
    lohn = 5000
    # Wie lange man an einer Schicht sitzen darf. Wordle braucht deutlich mehr
    # als ein Klickspiel - drei Minuten fuer sechs Rateversuche waren schlicht
    # zu knapp, da war die Schicht weg, bevor man nachgedacht hatte.
    frist = 300
    # Wie oft die Schicht gezogen wird (relativ zu den anderen).
    gewicht = 22
    # SELTEN heisst: kommt kaum vor UND laesst sich nicht bestellen. Beides
    # gehoert zusammen - eine seltene Schicht, die man per 'Flo work wordle'
    # jederzeit anfordern kann, ist nicht selten, sondern nur schlecht
    # sortiert. Dafuer zahlt sie deutlich besser.
    selten = False
    # Eigener Hoechstlohn je Runde (None = nur der Tagesdeckel gilt).
    deckel = None
    # Zaehlt die Runde fuer Karriere und Serie? Beim Spass-Wordle NICHT: die
    # Stufe soll fuer ARBEIT stehen, nicht fuer Zeitvertreib - sonst waere der
    # Werksleiter der, der am meisten geraten hat.
    zaehlt = True
    # Welcher Cooldown gilt. Das Spass-Wordle hat einen eigenen, damit es die
    # Schicht nicht blockiert (und umgekehrt).
    cooldown_key = "cooldown"
    cooldown_sek = None          # None = COOLDOWN

    async def bauen(self, chef, autor, **_extra):
        """Gibt (embed, view, datei) zurueck; 'datei' darf None sein.

        Bewusst async fuer ALLE Schichten, obwohl nur Wordle zeichnet: eine
        einheitliche Schnittstelle ist mehr wert als das eine gesparte await,
        und die naechste Schicht mit Bild braucht dann nichts umzustellen."""
        raise NotImplementedError

    def kopf(self, text):
        e = discord.Embed(title=self.titel, description=text,
                          color=discord.Color.blurple())
        e.set_footer(text=random.choice(_SCHICHT_START))
        return e


class WordleSchicht(Schicht):
    key, titel = "wordle", "🟩 Wordle-Schicht"
    was = "Fünf Buchstaben, sechs Versuche. Kommt selten – und zahlt dann fett."
    # Selten und deutlich besser bezahlt: rund jede sechzehnte Schicht. Wer sie
    # zieht, hat einen guten Tag - genau das soll sie sein, ein Highlight und
    # keine Routine.
    lohn = 22000
    gewicht = 8
    selten = True
    frist = 900          # 15 Minuten - Wordle will gedacht werden

    async def bauen(self, chef, autor, **_extra):
        spiel = Wordle.zufall(5)
        view = WordleView(chef, autor.id, self, spiel)
        embed = self.kopf(f"Fünf Buchstaben, **{MAX_VERSUCHE} Versuche**. "
                          f"Nur **{autor.display_name}** darf raten.")
        datei, name = await _wordle_bild(spiel, "WORDLE-SCHICHT",
                                   f"{MAX_VERSUCHE} Versuche · Grundlohn "
                                   f"{_muenzen(self.lohn)}")
        if datei is not None:
            embed.set_image(url=f"attachment://{name}")
        return embed, view, datei


class SalatSchicht(Schicht):
    key, titel = "salat", "🔤 Buchstabensalat"
    was = "Wort entwirren, drei Versuche."
    lohn = 7000
    frist = 420
    VERSUCHE = 3

    async def bauen(self, chef, autor, **_extra):
        wort = random.choice(WOERTER[6] + WOERTER[7])
        buchstaben = list(wort)
        # Wirklich mischen: sonst steht das Wort im Klartext da.
        for _ in range(12):
            random.shuffle(buchstaben)
            if "".join(buchstaben) != wort:
                break
        view = SalatView(chef, autor.id, self, wort)
        return self.kopf(f"Entwirr das:\n\n# `{' '.join(buchstaben)}`\n\n"
                         f"**{self.VERSUCHE} Versuche.** {len(wort)} Buchstaben."), view, None


class RechenSchicht(Schicht):
    key, titel = "rechnen", "🧮 Rechenschicht"
    was = "Fünf Aufgaben, jede falsche kostet den Rest."
    lohn = 6500
    RUNDEN = 5

    async def bauen(self, chef, autor, **_extra):
        view = RechenView(chef, autor.id, self)
        return self.kopf("Fünf Aufgaben. Jede richtige zählt, "
                         "eine falsche beendet die Schicht.\n\n"
                         + view.frage_text()), view, None


class SafeSchicht(Schicht):
    key, titel = "safe", "🔐 Safe knacken"
    was = "Zahlenschloss, Hinweise nach jedem Versuch."
    lohn = 7500
    frist = 420
    VERSUCHE = 5

    async def bauen(self, chef, autor, **_extra):
        code = "".join(random.choice("0123456789") for _ in range(3))
        view = SafeView(chef, autor.id, self, code)
        return self.kopf("Ein **dreistelliger** Code. Nach jedem Versuch sagt "
                         "dir das Schloss, wie nah du dran warst.\n\n"
                         f"**{self.VERSUCHE} Versuche.**"), view, None


class SortierSchicht(Schicht):
    key, titel = "sortieren", "📋 Schichtplan ordnen"
    was = "In die richtige Reihenfolge klicken."
    lohn = 6000

    async def bauen(self, chef, autor, **_extra):
        zahlen = random.sample(range(10, 100), 5)
        view = SortierView(chef, autor.id, self, zahlen)
        return self.kopf("Klick die Zahlen **von klein nach groß**.\n"
                         "Ein Fehlklick und die Schicht ist rum."), view, None


class PaareSchicht(Schicht):
    key, titel = "paare", "🧰 Werkzeug sortieren"
    was = "Vier Paare finden, so wenige Griffe wie möglich."
    lohn = 6800
    frist = 420
    PAARE = 4

    async def bauen(self, chef, autor, **_extra):
        werkzeug = random.sample(["🔧", "🔩", "⚙️", "🔌", "🧪", "📐", "🪛", "🔨"],
                                 self.PAARE)
        karten = werkzeug * 2
        random.shuffle(karten)
        view = PaareView(chef, autor.id, self, karten)
        return self.kopf(f"Acht Kisten, **{self.PAARE} Paare**. Immer zwei "
                         f"aufmachen.\nJe weniger Griffe, desto mehr Lohn – "
                         f"acht wären perfekt."), view, None


class KontrolleSchicht(Schicht):
    key, titel = "kontrolle", "🔍 Qualitätskontrolle"
    was = "Den Ausschuss finden – dreimal."
    lohn = 6200
    frist = 300
    RUNDEN = 3
    # Vier aus einer Kiste, eins gehoert nicht dazu. Bewusst Woerter statt
    # Zahlen: Rechnen gibt es schon als eigene Schicht.
    KISTEN = {
        "Werkzeug": ("Hammer", "Zange", "Schraube", "Bohrer", "Feile", "Meissel"),
        "Obst": ("Apfel", "Birne", "Kirsche", "Pflaume", "Banane", "Traube"),
        "Fahrzeug": ("Lastwagen", "Bagger", "Traktor", "Motorrad", "Bus", "Zug"),
        "Tier": ("Dachs", "Otter", "Reiher", "Marder", "Hirsch", "Fuchs"),
        "Moebel": ("Sessel", "Schrank", "Tisch", "Regal", "Kommode", "Hocker"),
        "Wetter": ("Nebel", "Hagel", "Sturm", "Regen", "Schnee", "Frost"),
        "Musik": ("Geige", "Trommel", "Fluegel", "Posaune", "Harfe", "Gitarre"),
        "Gebaeude": ("Turm", "Scheune", "Halle", "Villa", "Huette", "Bahnhof"),
    }

    async def bauen(self, chef, autor, **_extra):
        view = KontrolleView(chef, autor.id, self)
        return self.kopf("Vier Stück gehören zusammen, **eins nicht**. "
                         "Klick den Ausschuss.\n\n" + view.frage_text()), view, None


class SpassWordle(Schicht):
    """Wordle zum Spass - jederzeit, aber mit hartem Deckel.

    BEWUSST NICHT in SCHICHTEN: es soll nie per 'Flo work' gezogen werden und
    auch nicht bestellbar sein wie eine Schicht. Es erbt trotzdem alles, was
    eine Schicht koennen muss - Auto-Loesch-Schutz, Frist, Brett, Abrechnung -
    statt das alles ein zweites Mal zu schreiben."""

    key, titel = "spasswordle", "🟩 Wordle"
    was = "Wordle zum Spaß, jederzeit."
    # Lohn = Laenge x SPASS_JE_BUCHSTABE x Leistung, aber NIE mehr als SPASS_MAX.
    # Der Deckel bindet wirklich: acht Buchstaben auf Anhieb waeren sonst 24.000.
    lohn = 0                     # wird je Runde aus der Wortlaenge gerechnet
    deckel = SPASS_MAX
    zaehlt = False               # Zeitvertreib ist keine Karriere
    cooldown_key = "cooldown_spass"
    cooldown_sek = SPASS_COOLDOWN
    frist = 900

    def fuer_laenge(self, laenge):
        """Eine eigene Ausfuehrung mit dem Lohn fuer genau diese Wortlaenge.

        Kopie statt Zustand am Singleton: sonst haette die Runde von A den Lohn
        von B, wenn zwei gleichzeitig spielen."""
        eigen = SpassWordle()
        eigen.lohn = int(laenge) * SPASS_JE_BUCHSTABE
        eigen.titel = f"🟩 Wordle · {laenge} Buchstaben"
        return eigen

    # **_extra wie in der Basisklasse: ohne das wirft ein Aufruf mit einem
    # weiteren Schluesselwort TypeError, waehrend alle anderen Schichten ihn
    # schlucken - ein Unterschied, den der Aufrufer nicht sehen kann.
    async def bauen(self, chef, autor, laenge=5, **_extra):
        laenge = int(laenge)
        eigen = self.fuer_laenge(laenge)
        spiel = Wordle.zufall(laenge)
        view = WordleView(chef, autor.id, eigen, spiel)
        hoechst = min(SPASS_MAX, round(eigen.lohn * 1.5))
        embed = eigen.kopf(
            f"**{laenge} Buchstaben**, {MAX_VERSUCHE} Versuche. "
            f"Nur **{autor.display_name}** darf raten.\n"
            f"Bis zu **{_muenzen(hoechst)}** {economy.COIN} – je weniger "
            f"Versuche, desto mehr.")
        datei, name = await _wordle_bild(
            spiel, "WORDLE", f"{laenge} Buchstaben · bis {_muenzen(hoechst)}")
        if datei is not None:
            embed.set_image(url=f"attachment://{name}")
        return embed, view, datei


SPASS = SpassWordle()


# Die Ziehung: jede Schicht bringt ihr eigenes Gewicht mit. Wordle steht mit 8
# gegen rund 130 der anderen - also etwa jede sechzehnte Schicht.
SCHICHTEN = {s.key: s for s in (WordleSchicht(), SalatSchicht(), RechenSchicht(),
                                SafeSchicht(), SortierSchicht(), PaareSchicht(),
                                KontrolleSchicht())}


def schicht_ziehen():
    """Eine Schicht nach Gewicht ziehen."""
    kandidaten = list(SCHICHTEN.values())
    return random.choices(kandidaten,
                          weights=[s.gewicht for s in kandidaten], k=1)[0]


def seltene_chance():
    """Wie wahrscheinlich ist eine seltene Schicht? (fuer die Anzeige)"""
    gesamt = sum(s.gewicht for s in SCHICHTEN.values())
    selten = sum(s.gewicht for s in SCHICHTEN.values() if s.selten)
    return selten / gesamt if gesamt else 0.0


# ===========================================================================
# Das Feature
# ===========================================================================
class Arbeit(FeatureBasis):
    """Schichten, Lohn und das Wort des Tages."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._client = None
        self._views_ready = False

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if not economy.is_enabled():
            log.info("Arbeit aus: ohne Economy gibt es nichts zu verdienen.")
            return False
        self._store = JsonStore("arbeit.json", default={"nutzer": {}, "tag": {}})
        self._enabled = True
        log.info("Arbeit aktiv (%d Schichten, Cooldown %d Min, Wort des Tages ab "
                 "%d Leuten im Voice).", len(SCHICHTEN), COOLDOWN // 60,
                 STANDARD_MIN_VOICE)
        return True

    def is_enabled(self):
        return self._enabled

    # --- Daten ------------------------------------------------------------
    def _nutzer(self, uid):
        topf = self._store.data.setdefault("nutzer", {})
        prof = topf.setdefault(str(int(uid)), {
            "cooldown": 0, "serie": 0, "schichten": 0, "geschafft": 0,
            "verdient": 0, "tag": "", "heute": 0})
        # Nachtraeglich dazugekommene Felder ergaenzen: ein Konto aus der Zeit
        # vor der Karriere darf nicht an einem fehlenden Schluessel scheitern.
        prof.setdefault("beste_serie", int(prof.get("serie", 0)))
        prof.setdefault("gold", 0)              # goldene Schichten erwischt
        prof.setdefault("wordle_siege", 0)      # Woerter des Tages geknackt
        prof.setdefault("wordle_gespielt", 0)
        prof.setdefault("wordle_verteilung", [0] * MAX_VERSUCHE)
        prof.setdefault("spass_gespielt", 0)    # Wordle zum Spass
        prof.setdefault("spass_siege", 0)
        prof.setdefault("cooldown_spass", 0)
        return prof

    def raetsel(self, gid):
        """Das Tagesraetsel eines Servers - immer dasselbe Objekt auf denselben
        Daten, damit Aenderungen wirklich im Store landen."""
        daten = self._store.data.setdefault("tag", {}).setdefault(str(int(gid or 0)), {})
        return Tagesraetsel(gid, daten)

    def _speichern(self):
        try:
            asyncio.get_running_loop().create_task(self._store.save())
        except RuntimeError:
            pass        # kein Loop (Tests) - der Stand steht trotzdem im RAM

    def _tageskonto(self, prof):
        """Was heute schon verdient wurde - der Deckel haengt daran."""
        if prof.get("tag") != _heute():
            prof["tag"], prof["heute"] = _heute(), 0
        return int(prof.get("heute", 0) or 0)

    def _auszahlen(self, uid, betrag, grund):
        """Zahlt aus, achtet auf den Tagesdeckel, gibt (angekommen, frei) zurueck."""
        prof = self._nutzer(uid)
        schon = self._tageskonto(prof)
        frei = max(0, TAGES_DECKEL - schon)
        echt = int(min(max(0, int(betrag)), frei))
        if echt > 0:
            economy.add_coins(uid, echt, reason=grund)
            prof["heute"] = schon + echt
            prof["verdient"] = int(prof.get("verdient", 0)) + echt
        self._speichern()
        return echt, frei

    def _deckel_hinweis(self, gewollt, echt, frei):
        if echt >= gewollt:
            return ""
        if frei > 0:
            return (f"Tagesdeckel – von {_muenzen(gewollt)} kamen noch "
                    f"{_muenzen(echt)} an.")
        return "Dein Tagesdeckel ist voll – heute gibt es nichts mehr."

    @staticmethod
    def _serie_bonus(prof):
        """Nur der ZUSCHLAG (0..0,5), nicht der ganze Faktor - so lassen sich
        Serie und Stufe sauber addieren."""
        return min(SERIE_MAX, int(prof.get("serie", 0)) * SERIE_SCHRITT)

    # --- Befehle ----------------------------------------------------------
    async def handle(self, message):
        if not self._enabled:
            return None
        rest = ai.strip_lead(message.content or "")
        if rest is None:
            return None
        teile = rest.split()
        if not teile:
            return None
        erst = teile[0].lower().strip(".,!?")

        if erst in _LOHN_CMDS:
            return await self._lohnzettel(message)
        if erst in _TOP_CMDS and len(teile) == 1:
            return await self._rangliste(message)
        if erst in _TAGES_CMDS:
            return await self._tages_zeigen(message)
        if erst in _WORDLE_CMDS:
            zweit_w = teile[1].lower().strip(".,!?") if len(teile) > 1 else ""
            if zweit_w in ("tag", "heute", "tageswort", "daily"):
                return await self._tages_zeigen(message)
            if zweit_w in ("statistik", "stats", "bilanz", "lohnzettel"):
                return await self._lohnzettel(message)
            return await self._spass_starten(message, zweit_w)
        if erst not in _CMDS:
            return None

        zweit = teile[1].lower().strip(".,!?") if len(teile) > 1 else ""
        if zweit in ("liste", "list", "was", "hilfe", "help"):
            return self._schichtliste()
        if zweit in _TOP_CMDS:
            return await self._rangliste(message)
        if zweit in _LOHN_CMDS or zweit in ("bilanz", "statistik", "stats"):
            return await self._lohnzettel(message)
        gewuenscht = SCHICHTEN.get(zweit)
        if gewuenscht is not None and gewuenscht.selten:
            # Seltene Schichten kann man sich NICHT bestellen - sonst waeren sie
            # nicht selten, sondern nur schlecht sortiert.
            return self._kurz(
                f"**{gewuenscht.titel}** kannst du dir nicht aussuchen – die "
                f"kommt von allein, in etwa **jeder {self._selten_alle()}. "
                f"Schicht**. Dafür zahlt sie auch das Dreifache.\n"
                f"`{self._bot_name} work` und Daumen drücken.",
                discord.Color.gold())
        return await self._schicht_starten(message, gewuenscht)

    @staticmethod
    def _selten_alle():
        """'jede N. Schicht' - fuer die Anzeige."""
        p = seltene_chance()
        return int(round(1 / p)) if p else 0

    def _schichtliste(self):
        e = discord.Embed(
            title="🧰 Die Schichten",
            description=(f"`{self._bot_name} work` gibt dir eine zufällige – "
                         f"`{self._bot_name} work <name>` genau die hier:"),
            color=discord.Color.blurple())
        gesamt = sum(x.gewicht for x in SCHICHTEN.values())
        for key, sch in SCHICHTEN.items():
            if sch.selten:
                continue
            anteil = round(100 * sch.gewicht / gesamt)
            e.add_field(name=f"{sch.titel} · `{key}`",
                        value=f"{sch.was}\nGrundlohn **{_muenzen(sch.lohn)}** "
                              f"{economy.COIN} · etwa {anteil} % der Schichten",
                        inline=False)
        for sch in SCHICHTEN.values():
            if not sch.selten:
                continue
            e.add_field(
                name=f"⭐ {sch.titel} · SELTEN",
                value=f"{sch.was}\nGrundlohn **{_muenzen(sch.lohn)}** "
                      f"{economy.COIN} · etwa jede **{self._selten_alle()}. "
                      f"Schicht**\n*Nicht bestellbar – die kommt von allein.*",
                inline=False)
        e.set_footer(text=f"Alle {COOLDOWN // 60} Minuten eine Schicht · "
                          f"höchstens {_muenzen(TAGES_DECKEL)} am Tag")
        return e

    @staticmethod
    async def _avatar(user):
        try:
            return await asyncio.wait_for(user.display_avatar.with_size(128).read(), 6)
        except Exception:  # noqa: BLE001 - Profilbild ist nur Deko
            return None

    async def _lohnzettel(self, message):
        """Die Arbeitsbilanz als Karte - mit Stufe, Fortschritt und Wordle-Bilanz.

        Faellt das Zeichnen aus, geht der alte Text-Weg raus: eine fehlende
        Schrift darf niemandem seine Zahlen vorenthalten."""
        ziel = erstes_ziel(message) or message.author
        prof = self._nutzer(ziel.id)
        heute = self._tageskonto(prof)
        geschafft = int(prof.get("geschafft", 0))
        stufe = stufe_fuer(geschafft)
        weiter = naechste_stufe(geschafft)
        verteilung = list(prof.get("wordle_verteilung") or [0] * MAX_VERSUCHE)
        wordle = ((int(prof.get("wordle_siege", 0)),
                   int(prof.get("wordle_gespielt", 0)), verteilung)
                  if prof.get("wordle_gespielt") else None)
        spass = ((int(prof.get("spass_siege", 0)),
                  int(prof.get("spass_gespielt", 0)))
                 if prof.get("spass_gespielt") else None)
        # Nur Spass gespielt, nie beim Tagesraetsel dabei? Dann trotzdem den
        # Wordle-Block zeigen - sonst fehlt die halbe Bilanz.
        if wordle is None and spass is not None:
            wordle = (0, 0, [0] * MAX_VERSUCHE)
        avatar = await self._avatar(ziel)
        try:
            import render
            buf = await asyncio.to_thread(
                render.lohnzettel, ziel.display_name, avatar,
                stufe=stufe.titel, symbol=stufe.symbol, bonus=stufe.bonus,
                geschafft=geschafft, angetreten=int(prof.get("schichten", 0)),
                serie=int(prof.get("serie", 0)),
                beste_serie=int(prof.get("beste_serie", 0)),
                verdient=int(prof.get("verdient", 0)), heute=heute,
                deckel=TAGES_DECKEL, gold=int(prof.get("gold", 0)),
                naechste=(weiter.titel if weiter else None),
                fehlt=(weiter.ab - geschafft if weiter else 0), wordle=wordle,
                spass=spass)
        except Exception:  # noqa: BLE001
            log.exception("Lohnzettel-Karte fehlgeschlagen - Text-Fallback")
            return self._lohnzettel_text(ziel, prof, heute, stufe, weiter)
        warte = int(prof.get("cooldown", 0)) - int(time.time())
        e = discord.Embed(
            title=f"🧾 Lohnzettel · {ziel.display_name}",
            description=(f"{stufe.symbol} **{stufe.titel}** · "
                         f"+{round(stufe.bonus * 100)} % auf jeden Lohn"),
            color=discord.Color.gold())
        e.set_image(url="attachment://lohnzettel.png")
        e.set_footer(text=(f"Nächste Schicht in {warte // 60 + 1} Min."
                           if warte > 0 else "Du kannst sofort ran."))
        try:
            await message.channel.send(
                embed=e, file=discord.File(buf, filename="lohnzettel.png"))
        except discord.HTTPException:
            return self._lohnzettel_text(ziel, prof, heute, stufe, weiter)
        return HANDLED

    def _lohnzettel_text(self, ziel, prof, heute, stufe, weiter):
        """Der Notweg ohne Bild - dieselben Zahlen, nur als Embed."""
        quote = 0
        if prof.get("schichten"):
            quote = round(100 * int(prof.get("geschafft", 0)) / int(prof["schichten"]))
        e = discord.Embed(title=f"🧾 Lohnzettel · {ziel.display_name}",
                          color=discord.Color.gold())
        e.add_field(name="Stufe",
                    value=f"{stufe.symbol} **{stufe.titel}**\n"
                          f"+{round(stufe.bonus * 100)} % Zuschlag")
        e.add_field(name="Schichten",
                    value=f"{prof.get('schichten', 0)} angetreten\n"
                          f"{prof.get('geschafft', 0)} geschafft ({quote} %)")
        e.add_field(name="Serie",
                    value=f"**{prof.get('serie', 0)}** in Folge\n"
                          f"Bestwert {prof.get('beste_serie', 0)}")
        e.add_field(name="Verdient",
                    value=f"insgesamt **{_muenzen(prof.get('verdient', 0))}** "
                          f"{economy.COIN}\nheute **{_muenzen(heute)}** von "
                          f"{_muenzen(TAGES_DECKEL)}", inline=False)
        if weiter is not None:
            e.set_footer(text=f"Noch {weiter.ab - int(prof.get('geschafft', 0))} "
                              f"Schichten bis {weiter.titel}")
        return e

    async def _rangliste(self, message):
        """Wer im Werk was reisst - nach Verdienst, mit Stufe."""
        leute = []
        for uid, prof in (self._store.data.get("nutzer") or {}).items():
            if not isinstance(prof, dict) or not int(prof.get("geschafft", 0)):
                continue
            leute.append((int(prof.get("verdient", 0)), int(uid), prof))
        if not leute:
            return self._kurz(
                f"Hier hat noch niemand gearbeitet. `{self._bot_name} work` – "
                f"sei der Erste.", discord.Color.orange())
        leute.sort(key=lambda e: -e[0])
        zeilen = []
        for platz, (verdient, uid, prof) in enumerate(leute[:10], 1):
            stufe = stufe_fuer(prof.get("geschafft", 0))
            name = str(uid)
            nutzer = self._safe_user(message, uid)
            if nutzer is not None:
                name = nutzer.display_name
            zeilen.append((platz, name, stufe.symbol, stufe.titel,
                           int(prof.get("geschafft", 0)), verdient))
        try:
            import render
            buf = await asyncio.to_thread(
                render.arbeit_rangliste, zeilen,
                untertitel=f"{len(leute)} Arbeiter · {len(SCHICHTEN)} Schichten")
            e = discord.Embed(title="🏭 Werk-Rangliste", color=discord.Color.gold())
            e.set_image(url="attachment://rangliste.png")
            await message.channel.send(
                embed=e, file=discord.File(buf, filename="rangliste.png"))
            return HANDLED
        except Exception:  # noqa: BLE001
            log.exception("Rangliste-Karte fehlgeschlagen - Text-Fallback")
        e = discord.Embed(title="🏭 Werk-Rangliste", color=discord.Color.gold())
        e.description = "\n".join(
            f"**{p}.** {n} · {sym} {st} · {_muenzen(v)} {economy.COIN} "
            f"({g} Schichten)" for p, n, sym, st, g, v in zeilen)
        return e

    @staticmethod
    def _safe_user(message, uid):
        """Nutzer zu einer ID - erst im Server, dann global. Nie ein Fehler."""
        guild = getattr(message, "guild", None)
        try:
            if guild is not None:
                m = guild.get_member(int(uid))
                if m is not None:
                    return m
        except Exception:  # noqa: BLE001
            pass
        try:
            return message._state._get_client().get_user(int(uid))
        except Exception:  # noqa: BLE001
            return None

    async def _schicht_starten(self, message, schicht=None, **extra):
        prof = self._nutzer(message.author.id)
        # Jede Art hat ihren eigenen Cooldown-Schluessel: ein Spass-Wordle darf
        # die Schicht nicht blockieren und umgekehrt.
        schluessel = (schicht.cooldown_key if schicht is not None else "cooldown")
        pause = COOLDOWN
        if schicht is not None and schicht.cooldown_sek is not None:
            pause = int(schicht.cooldown_sek)
        warte = int(prof.get(schluessel, 0)) - int(time.time())
        if warte > 0:
            wie_lang = (f"**{warte // 60 + 1} Minuten**" if warte >= 60
                        else f"**{warte} Sekunden**")
            return self._kurz(f"⏳ Kurz warten – noch {wie_lang}.",
                              discord.Color.orange())
        if self._tageskonto(prof) >= TAGES_DECKEL:
            return self._kurz(
                f"🛑 Feierabend. Du hast heute deine **{_muenzen(TAGES_DECKEL)}** "
                f"{economy.COIN} voll. Morgen wieder.", discord.Color.orange())

        schicht = schicht or schicht_ziehen()
        prof[schluessel] = int(time.time()) + pause
        if schicht.zaehlt:
            prof["schichten"] = int(prof.get("schichten", 0)) + 1
        self._speichern()

        embed, view, datei = await schicht.bauen(self, message.author, **extra)
        if view.gold:
            embed.color = discord.Color.gold()
            embed.title = "🥇 " + (embed.title or schicht.titel)
            embed.add_field(
                name="🥇 Goldene Schicht",
                value="Diese eine zählt **doppelt**. Streng dich an.",
                inline=False)
        try:
            kwargs = {"embed": embed, "view": view}
            if datei is not None:
                kwargs["file"] = datei
            view.message = await message.channel.send(**kwargs)
        except discord.HTTPException:
            log.exception("Schicht konnte nicht gestartet werden")
            # Der Cooldown steht schon - waere die Schicht jetzt einfach weg,
            # haette man 15 Minuten Pause fuer nichts. Also zuruecknehmen.
            prof[schluessel] = 0
            self._speichern()
            return "Das ließ sich nicht aufmachen – versuch's nochmal."
        # WICHTIG: vor dem Auto-Loeschen schuetzen. Ohne das verschwindet die
        # Schicht in einem Aufraeum-Kanal mitten im Spiel, und der Cooldown
        # laeuft trotzdem weiter.
        _schuetzen(view.message)
        return HANDLED

    async def _spass_starten(self, message, laenge_roh=""):
        """'Flo wordle' - jederzeit, mit eigenem kurzen Cooldown.

        Die Wortlaenge darf man waehlen ('Flo wordle 7'): laenger heisst mehr
        Lohn, bis der Deckel greift. Unsinnige Angaben werden freundlich
        abgewiesen statt still auf 5 zu fallen - sonst wundert man sich, warum
        'Flo wordle 12' ein Fuenf-Buchstaben-Wort gibt."""
        laenge = 5
        if laenge_roh:
            if not numfmt.ist_zahl(laenge_roh):
                return self._kurz(
                    f"So: `{self._bot_name} wordle` oder mit Länge, z. B. "
                    f"`{self._bot_name} wordle 7`.", discord.Color.orange())
            laenge = int(laenge_roh)
            if laenge not in WOERTER:
                moeglich = ", ".join(str(x) for x in sorted(WOERTER))
                return self._kurz(
                    f"Ich habe nur Wörter mit **{moeglich}** Buchstaben.",
                    discord.Color.orange())
        return await self._schicht_starten(message, SPASS, laenge=laenge)

    # --- Abrechnung einer Schicht ----------------------------------------
    def abrechnen(self, uid, schicht, anteil, *, gold=False):
        """Zahlt eine beendete Schicht aus. 'anteil' ist die Leistung (0..1,2).

        Der Lohn setzt sich ADDITIV zusammen: Grundlohn x Leistung x
        (1 + Serien-Zuschlag + Stufen-Zuschlag), am Ende ggf. verdoppelt.
        Additiv und nicht multiplikativ, weil sich sonst zwei Faktoren von je
        +50 % zu +125 % aufschaukeln - und die Schicht faengt an, das Wort des
        Tages zu ueberholen, das ja der Hoehepunkt bleiben soll.

        Gibt (betrag, info) zurueck; 'info' ist alles, was das Ergebnis-Embed
        anzeigen will (Serie, Stufe, Aufstieg, Gold, Deckel-Hinweis)."""
        prof = self._nutzer(uid)
        vorher = int(prof.get("geschafft", 0))
        if not schicht.zaehlt:
            # Zeitvertreib (Spass-Wordle): Bilanz mitschreiben, aber Karriere
            # und Serie NICHT anfassen. Sonst waere der Werksleiter der, der am
            # meisten geraten hat.
            prof["spass_gespielt"] = int(prof.get("spass_gespielt", 0)) + 1
            if anteil > 0:
                prof["spass_siege"] = int(prof.get("spass_siege", 0)) + 1
        elif anteil > 0:
            prof["serie"] = int(prof.get("serie", 0)) + 1
            prof["geschafft"] = vorher + 1
            prof["beste_serie"] = max(int(prof.get("beste_serie", 0)),
                                      int(prof["serie"]))
        else:
            prof["serie"] = 0
        nachher = int(prof.get("geschafft", 0))
        stufe = stufe_fuer(nachher)
        aufgestiegen = stufe.ab > stufe_fuer(vorher).ab

        faktor = 1.0 + self._serie_bonus(prof) + stufe.bonus
        gewollt = int(round(schicht.lohn * anteil * faktor))
        if schicht.deckel is not None:
            # Der eigene Deckel greift VOR dem Gold-Bonus: sonst waere die
            # Obergrenze in Wahrheit das Doppelte, und "nie mehr als 15.000"
            # waere gelogen.
            gewollt = min(gewollt, int(schicht.deckel))
        if gold and gewollt > 0:
            gewollt = int(round(gewollt * GOLD_FAKTOR))
            if schicht.deckel is not None:
                gewollt = min(gewollt, int(schicht.deckel))
            prof["gold"] = int(prof.get("gold", 0)) + 1
        echt, frei = self._auszahlen(uid, gewollt, f"arbeit:{schicht.key}")
        info = {
            "serie": int(prof.get("serie", 0)),
            "serie_bonus": self._serie_bonus(prof),
            "stufe": stufe,
            "aufgestiegen": aufgestiegen,
            "geschafft": nachher,
            "gold": bool(gold and echt > 0),
            "hinweis": self._deckel_hinweis(gewollt, echt, frei),
        }
        return echt, info

    def ergebnis_embed(self, autor, titel, text, betrag, info, gut):
        stufe = info["stufe"]
        e = discord.Embed(
            title=("🥇 " + titel if info.get("gold") else titel),
            description=text + "\n\n" + random.choice(_LOB if gut else _TADEL),
            color=discord.Color.gold() if info.get("gold")
            else (discord.Color.green() if gut else discord.Color.red()))
        if betrag > 0:
            wert = f"**+{_muenzen(betrag)}** {economy.COIN}"
            if info.get("gold"):
                wert += "\n🥇 **Goldene Schicht** – doppelter Lohn"
            e.add_field(name="Lohn", value=wert)
        if info.get("serie"):
            e.add_field(name="Serie",
                        value=f"**{info['serie']}** in Folge "
                              f"(+{round(info['serie_bonus'] * 100)} %)")
        # Die Stufe MIT Fortschritt. Vorher stand hier nur der Titel, und wie weit
        # es noch ist, stand klein in der Fusszeile - dadurch sah es aus, als
        # passiere ueber Stunden gar nichts. Der Balken beantwortet die Frage
        # "ab wann steigt das?" da, wo man ohnehin hinschaut.
        stufen_wert = f"{stufe.symbol} **{stufe.titel}** (+{round(stufe.bonus * 100)} %)"
        weiter = naechste_stufe(info.get("geschafft", 0))
        if weiter is not None:
            hab = int(info.get("geschafft", 0))
            von, bis = stufe.ab, weiter.ab
            anteil = 0.0 if bis <= von else (hab - von) / (bis - von)
            # Abschneiden statt runden: ein VOLLER Balken bei 29/30 waere eine
            # kleine Luege - voll wird er erst, wenn die Stufe wirklich da ist.
            voll = max(0, min(10, int(anteil * 10)))
            stufen_wert += (f"\n{'▰' * voll}{'▱' * (10 - voll)} "
                            f"{hab}/{bis} bis {weiter.symbol} {weiter.titel}")
        e.add_field(name="Stufe", value=stufen_wert, inline=False)
        if info.get("aufgestiegen"):
            e.add_field(
                name="🎉 Aufstieg",
                value=f"Du bist jetzt **{stufe.titel}** – ab sofort "
                      f"**+{round(stufe.bonus * 100)} %** auf jeden Lohn.",
                inline=False)
        if info.get("hinweis"):
            e.add_field(name="Hinweis", value=info["hinweis"], inline=False)
        # Der Fortschritt steht jetzt oben am Stufen-Feld - hier nur noch, wann
        # es weitergeht.
        e.set_footer(text=f"{autor.display_name} · nächste Schicht in "
                          f"{COOLDOWN // 60} Min.")
        return e

    # --- Wort des Tages: wann und wohin -----------------------------------
    @staticmethod
    def leute_im_voice(guild):
        """Wie viele MENSCHEN gerade in Sprachkanaelen sitzen. Bots zaehlen nicht -
        Flo selbst sitzt ja oft mit drin und wuerde sich sonst selbst mitzaehlen."""
        n = 0
        for kanal in (getattr(guild, "voice_channels", None) or []):
            for m in (getattr(kanal, "members", None) or []):
                if not getattr(m, "bot", False):
                    n += 1
        return n

    def _cfg(self, gid, key, standard):
        try:
            import guildcfg
            wert = guildcfg.get(gid, key)
            return wert if wert not in (None, "") else standard
        except Exception:  # noqa: BLE001
            return standard

    def min_voice(self, gid):
        try:
            wert = int(self._cfg(gid, "wordle_min_voice", STANDARD_MIN_VOICE))
        except (TypeError, ValueError):
            return STANDARD_MIN_VOICE
        return wert if wert >= 1 else STANDARD_MIN_VOICE

    def faellig(self, guild, jetzt=None):
        """Darf jetzt ein Wort fallen? Zwei Bedingungen, beide muessen stimmen.

        1. ES MUSS WAS LOS SEIN. Nicht die Uhr entscheidet, sondern ob wirklich
           jemand da ist - ein Raetsel um 4 Uhr morgens in einen leeren Server
           zu werfen waere verschenkt.
        2. DER GEWUERFELTE TERMIN MUSS DA SEIN. Sobald genug Leute im Call
           sitzen, wird ein Zeitpunkt in den naechsten 5-45 Minuten gezogen.
           Bis dahin passiert nichts. Sonst haenge das Wort sichtbar an der
           dritten Person, und man koennte es sich herbeiholen.

        Leert sich der Call vor dem Termin, wartet Flo eben - der Termin bleibt
        stehen und greift, sobald wieder genug da sind."""
        gid = getattr(guild, "id", 0)
        raetsel = self.raetsel(gid)
        if raetsel.datum == _heute():
            return False
        if self.leute_im_voice(guild) < self.min_voice(gid):
            return False
        jetzt = int(jetzt if jetzt is not None else time.time())
        termin = raetsel.geplant_fuer
        if termin is None:
            termin = raetsel.termin_setzen(jetzt)
            self._speichern()
            log.info("Wort des Tages auf %s eingeplant (in %d Min).",
                     getattr(guild, "name", gid), max(0, termin - jetzt) // 60)
            return False
        return jetzt >= termin

    def kanal_fuer(self, guild):
        """Wohin das Wort des Tages geht."""
        kid = self._cfg(guild.id, "wordle_channel", 0)
        if kid:
            kanal = guild.get_channel(int(kid))
            if kanal is not None:
                return kanal
        # Nichts eingestellt: ein Kanal, der passend heisst. Trifft den
        # Normalfall, ohne eine feste ID in den Code zu schreiben.
        namen = {c.name.lower(): c
                 for c in (getattr(guild, "text_channels", None) or [])}
        for name in KANAL_NAMEN:
            if name in namen:
                return namen[name]
        kid = self._cfg(guild.id, "ansage_channel", 0)
        if kid:
            kanal = guild.get_channel(int(kid))
            if kanal is not None:
                return kanal
        return getattr(guild, "system_channel", None)

    async def tick(self, guilds):
        """Einmal am Tag je Server: ist ein Wort faellig? bot.py ruft das im Takt.

        Gibt (guild, embed, view) zurueck - genau wie merchant/lotto, damit das
        Senden in bot.py bleibt und dieses Modul keinen Client braucht."""
        if not self._enabled:
            return []
        raus = []
        for guild in guilds or []:
            try:
                if not self.faellig(guild):
                    continue
                raetsel = self.raetsel(guild.id)
                raetsel.starten()
                self._speichern()
                log.info("Wort des Tages auf %s gestartet (%d Buchstaben, %d im Voice).",
                         getattr(guild, "name", guild.id), len(raetsel.wort),
                         self.leute_im_voice(guild))
                embed, datei = await self.tages_ansage(guild.id)
                raus.append((guild, embed, TagesView(guild.id), datei))
            except Exception:  # noqa: BLE001
                log.exception("Wort des Tages fuer %s fehlgeschlagen", guild)
        return raus

    async def tages_ansage(self, gid):
        """Der Aushang zum Start: Embed + leeres Brett als Bild.

        Das leere Brett ist kein Schmuck - man sieht auf einen Blick, wie lang
        das Wort ist und wie viele Versuche man hat, ohne es zu lesen."""
        raetsel = self.raetsel(gid)
        embed = self.tages_embed(gid)
        spiel = Wordle(raetsel.wort)
        wochentag = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag",
                     "Samstag", "Sonntag"][datetime.now().weekday()]
        datei, name = await _wordle_bild(
            spiel, "WORT DES TAGES",
            f"{wochentag} · {len(raetsel.wort)} Buchstaben · Topf "
            f"{_muenzen(raetsel.topf)}")
        if datei is not None:
            embed.set_image(url=f"attachment://{name}")
        return embed, datei

    def tages_embed(self, gid):
        """Die oeffentliche Ansage - vor und nach der Entscheidung."""
        raetsel = self.raetsel(gid)
        if raetsel.entschieden:
            e = discord.Embed(
                title="🟩 Wort des Tages – entschieden",
                description=(f"Das Wort war **{raetsel.wort}**.\n\n"
                             f"<@{raetsel.gewinner}> war als Erster dran – in "
                             f"**{raetsel.daten.get('versuche', '?')}** Versuchen."),
                color=discord.Color.gold())
            e.set_footer(text="Morgen gibt es ein neues – wenn wieder was los ist.")
            return e
        e = discord.Embed(
            title="🟩 Wort des Tages",
            description=(
                f"**{len(raetsel.wort)} Buchstaben**, {MAX_VERSUCHE} Versuche.\n\n"
                f"⚡ **Wettrennen:** Wer es als **Erster** knackt, nimmt **alles**. "
                f"Danach ist die Runde durch.\n\n"
                f"Topf: **{_muenzen(raetsel.topf)}** {economy.COIN} – "
                f"beim ersten Versuch das **Doppelte**."),
            color=discord.Color.green())
        e.set_footer(text="Jeder rät für sich – deine Versuche sieht nur du.")
        return e

    async def _tages_zeigen(self, message):
        """'Flo wordle' im Chat: der Weg zum Raetsel ohne den Knopf zu suchen."""
        gid = getattr(message.guild, "id", 0)
        raetsel = self.raetsel(gid)
        if not raetsel.laeuft:
            return self._kurz(
                f"Heute ist noch kein Wort gefallen. Ich lege es raus, sobald "
                f"mindestens **{self.min_voice(gid)} Leute** im Voice sind.",
                discord.Color.orange())
        embed, datei = await self.tages_ansage(gid)
        try:
            kwargs = {"embed": embed, "view": TagesView(gid),
                      "reference": message, "mention_author": False}
            if datei is not None:
                kwargs["file"] = datei
            await message.channel.send(**kwargs)
        except discord.HTTPException:
            return "Ging gerade nicht."
        return HANDLED

    # --- Wort des Tages: die Interaktionen --------------------------------
    async def tages_knopf(self, interaction, gid):
        """Klick auf 'Raten' - prueft, ob die Person ueberhaupt noch darf."""
        raetsel = self.raetsel(gid)
        if not raetsel.laeuft:
            await interaction.response.send_message(
                "Das Wort von neulich ist durch. Warte aufs nächste.", ephemeral=True)
            return
        spiel = raetsel.spiel_von(interaction.user.id)
        if raetsel.entschieden and not spiel.geloest:
            # Das Rennen ist gelaufen - Geld gibt es nicht mehr. Weiterraten
            # darf man trotzdem: fuer die eigene Bilanz. Genau DAS hat die
            # Runde vorher nicht zugelassen, und damit war fuer alle ausser dem
            # Sieger der Tag gelaufen, sobald jemand schneller war.
            if spiel.aus:
                await interaction.response.send_message(
                    f"Rennen gelaufen, deine Versuche auch. Das Wort war "
                    f"**{raetsel.wort}**.", ephemeral=True)
                return
            await interaction.response.send_modal(
                TagesModal(gid, spiel.laenge, nur_ehre=True))
            return
        if spiel.aus:
            await interaction.response.send_message(
                f"Deine {MAX_VERSUCHE} Versuche sind weg. Verraten wird trotzdem "
                f"nichts – vielleicht knackt es ja noch jemand.", ephemeral=True)
            return
        await interaction.response.send_modal(TagesModal(gid, spiel.laenge))

    async def tages_antwort(self, interaction, gid, roh):
        """Ein abgeschickter Versuch aus dem Eingabefeld."""
        raetsel = self.raetsel(gid)
        status, spiel = raetsel.raten(interaction.user.id, roh)
        self._speichern()

        if status == "kein_wort":
            await interaction.response.send_message("Gerade läuft kein Wort.",
                                                    ephemeral=True)
            return
        if status == "laenge":
            await interaction.response.send_message(
                f"{len(raetsel.wort)} Buchstaben, nur Buchstaben. Nochmal.",
                ephemeral=True)
            return
        if status == "fertig":
            await interaction.response.send_message("Für heute bist du durch.",
                                                    ephemeral=True)
            return
        # Teilnahme mitschreiben - genau einmal am Tag, beim ersten Versuch.
        prof = self._nutzer(interaction.user.id)
        if len(spiel.versuche) == 1:
            prof["wordle_gespielt"] = int(prof.get("wordle_gespielt", 0)) + 1
            self._speichern()

        if status == "weiter":
            e = discord.Embed(
                title="🟩 Wort des Tages",
                description=f"Noch **{spiel.offen}** Versuche. Das Rennen läuft.",
                color=discord.Color.blurple())
            e.set_footer(text="Nur du siehst das hier.")
            await self._ephemer_mit_brett(
                interaction, e, spiel, "DEIN STAND",
                f"Noch {spiel.offen} von {MAX_VERSUCHE} Versuchen")
            return
        if status == "aus":
            e = discord.Embed(
                title="🟥 Alle Versuche weg",
                description="Sechs Versuche, nichts. Das Wort verrate ich nicht – "
                            "es rennen ja noch andere.",
                color=discord.Color.red())
            # Verdeckt: die Farben darf er sehen, die Buchstaben nicht - sonst
            # koennte er die Loesung im Chat ausplaudern und das Rennen kippen.
            await self._ephemer_mit_brett(interaction, e, spiel, "AUS",
                                          "Alle Versuche verbraucht", verdeckt=True)
            return

        # Geloest. Die Bilanz zaehlt immer, das Geld nur fuer den Ersten.
        n = len(spiel.versuche)
        prof["wordle_siege"] = int(prof.get("wordle_siege", 0)) + 1
        verteilung = list(prof.get("wordle_verteilung") or [0] * MAX_VERSUCHE)
        while len(verteilung) < MAX_VERSUCHE:
            verteilung.append(0)
        if 1 <= n <= MAX_VERSUCHE:
            verteilung[n - 1] += 1
        prof["wordle_verteilung"] = verteilung

        sieger = raetsel.gewinner == interaction.user.id
        if not sieger:
            self._speichern()
            e = discord.Embed(
                title="🟩 Doch noch geknackt",
                description=f"**{spiel.loesung}** – in {n} Versuch"
                            f"{'en' if n != 1 else ''}.\n\n"
                            f"Den Topf hat <@{raetsel.gewinner}> mitgenommen, "
                            f"aber in deiner Bilanz steht es.",
                color=discord.Color.blurple())
            await self._ephemer_mit_brett(interaction, e, spiel, "GEKNACKT",
                                          f"{n} Versuch{'e' if n != 1 else ''} · "
                                          f"nur für die Ehre")
            return

        gewollt = raetsel.preis(spiel)
        echt, frei = self._auszahlen(interaction.user.id, gewollt, "arbeit:tageswordle")
        hinweis = self._deckel_hinweis(gewollt, echt, frei)
        self._speichern()
        e = discord.Embed(
            title="🏆 Wort des Tages geknackt",
            description=f"**{spiel.loesung}** – in {n} Versuch"
                        f"{'en' if n != 1 else ''}. Du warst der Erste.",
            color=discord.Color.gold())
        e.add_field(name="Topf", value=f"**+{_muenzen(echt)}** {economy.COIN}")
        if hinweis:
            e.add_field(name="Hinweis", value=hinweis, inline=False)
        await self._ephemer_mit_brett(interaction, e, spiel, "GEKNACKT",
                                      f"{n} Versuch{'e' if n != 1 else ''} · "
                                      f"+{_muenzen(echt)}")
        await self._tages_ausrufen(interaction, raetsel, spiel, echt)

    @staticmethod
    async def _ephemer_mit_brett(interaction, embed, spiel, titel, untertitel,
                                 *, verdeckt=False):
        """Antwortet nur dem Klickenden - mit dem Brett als Bild.

        Faellt das Zeichnen aus (fehlende Schrift o. ae.), geht die Antwort
        trotzdem raus: ein fehlendes Bild darf niemandem den Versuch fressen."""
        datei, name = await _wordle_bild(spiel, titel, untertitel, verdeckt=verdeckt)
        if datei is not None:
            embed.set_image(url=f"attachment://{name}")
            await interaction.response.send_message(embed=embed, file=datei,
                                                    ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _tages_ausrufen(self, interaction, raetsel, spiel, betrag):
        """Sieg oeffentlich machen und die Ansage auf 'entschieden' umstellen.

        Bewusst NACH der Auszahlung und mit eigenem try: der Gewinner hat sein
        Geld, auch wenn Discord den Jubel gerade nicht annimmt."""
        n = len(spiel.versuche)
        try:
            await interaction.channel.send(
                f"🏆 **{interaction.user.display_name}** knackt das Wort des Tages "
                f"in **{n}** Versuch{'en' if n != 1 else ''} – **{spiel.loesung}** – "
                f"und nimmt **{_muenzen(betrag)}** {economy.COIN}. "
                f"Rennen vorbei.")
        except Exception:  # noqa: BLE001
            log.debug("Wordle-Jubel nicht sendbar", exc_info=True)
        # Die Ansage auf 'entschieden' umstellen. interaction.message ist hier
        # IMMER None (die Antwort kam aus einem Eingabefeld), deshalb ueber die
        # beim Posten gemerkten IDs.
        kanal_id = int(raetsel.daten.get("kanal") or 0)
        msg_id = int(raetsel.daten.get("ansage") or 0)
        if not (kanal_id and msg_id):
            return
        try:
            kanal = interaction.client.get_channel(kanal_id)
            if kanal is None:
                return
            nachricht = await kanal.fetch_message(msg_id)
            embed = self.tages_embed(raetsel.gid)
            # Das Siegerbrett bleibt als Beweis stehen - das ist die
            # interessanteste Nachricht des Tages, nicht ein leeres Gitter.
            datei, name = await _wordle_bild(
                spiel, "GELOEST", f"{interaction.user.display_name} · "
                f"{len(spiel.versuche)} Versuch"
                f"{'e' if len(spiel.versuche) != 1 else ''}")
            kwargs = {"embed": embed, "view": None}
            if datei is not None:
                embed.set_image(url=f"attachment://{name}")
                kwargs["attachments"] = [datei]
            await nachricht.edit(**kwargs)
            # Rennen gelaufen: der Aushang darf jetzt wieder aufgeraeumt werden.
            _freigeben(nachricht)
        except Exception:  # noqa: BLE001
            log.debug("Wordle-Ansage nicht aktualisierbar", exc_info=True)

    # --- Views nach Neustart wieder anmelden ------------------------------
    def register_views(self, client):
        """Macht die Rate-Knoepfe laufender Runden wieder klickbar."""
        if client is not None:
            self._client = client
        if not self._enabled or client is None or self._views_ready:
            return 0
        n = 0
        for gid, daten in list((self._store.data.get("tag") or {}).items()):
            raetsel = Tagesraetsel(gid, daten)
            if not raetsel.laeuft or raetsel.entschieden:
                continue
            try:
                client.add_view(TagesView(int(gid)))
                n += 1
            except Exception:  # noqa: BLE001
                log.debug("Wordle-View %s nicht anmeldbar", gid, exc_info=True)
        self._views_ready = True
        if n:
            log.info("%d Wordle-Knopf/Knöpfe wieder angemeldet.", n)
        return n

    @staticmethod
    def _kurz(text, farbe=discord.Color.blurple()):
        return discord.Embed(description=text, color=farbe)


# ===========================================================================
# Discord-Gesichter der Schichten
# ===========================================================================
class SchichtView(discord.ui.View):
    """Gemeinsamer Unterbau: eine Schicht gehoert genau EINER Person, und wer
    sie liegen laesst, bekommt nichts - sonst blockiert eine vergessene Schicht
    den Cooldown, ohne je abgerechnet zu werden."""

    def __init__(self, chef, uid, schicht):
        super().__init__(timeout=schicht.frist)
        self.chef = chef
        self.uid = int(uid)
        self.schicht = schicht
        self.message = None
        self.fertig = False
        # Gold wird BEIM START gewuerfelt, nicht beim Abrechnen: so kann es
        # dranstehen, waehrend man arbeitet. Man soll wissen, dass die Schicht
        # doppelt zaehlt - das ist der halbe Spass daran.
        self.gold = random.random() < GOLD_CHANCE

    async def interaction_check(self, interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("Das ist nicht deine Schicht.",
                                                    ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.fertig or self.message is None:
            return
        self.fertig = True
        # Verfallen ist verfallen: kein Gold-Bonus auf nichts.
        self.chef.abrechnen(self.uid, self.schicht, 0.0)
        # Nur die Knoepfe abnehmen, den Stand STEHEN LASSEN. Frueher wurde hier
        # das ganze Embed durch "Schicht verpennt" ersetzt - wer nach einer
        # Pause zurueckkam, sah nicht mal mehr, woran er gearbeitet hatte.
        try:
            await self.message.edit(content="⌛ Zeit ist rum – die Schicht ist "
                                            "unbezahlt verfallen.", view=None)
        except discord.HTTPException:
            pass
        _freigeben(self.message)

    async def beenden(self, interaction, titel, text, anteil, datei=None):
        """Schicht abschliessen, auszahlen, Ergebnis zeigen."""
        if self.fertig:
            # Zweiter Klick (oder zweite Modal-Eingabe) - NICHT nochmal
            # abrechnen. interaction_check prueft nur, WER klickt, nicht wie
            # oft; und dreizehn Aufrufstellen koennen den Riegel nicht jede
            # fuer sich mitbringen. Ohne das lief abrechnen() zweimal und der
            # Lohn wurde doppelt gutgeschrieben.
            try:
                await interaction.response.defer()
            except Exception:  # noqa: BLE001 - schon beantwortet ist auch gut
                pass
            return
        self.fertig = True
        self.stop()
        betrag, info = self.chef.abrechnen(self.uid, self.schicht, anteil,
                                          gold=self.gold)
        emb = self.chef.ergebnis_embed(interaction.user, titel, text,
                                       betrag, info, anteil > 0)
        kwargs = {"embed": emb, "view": None}
        if datei is not None:
            emb.set_image(url=f"attachment://{datei.filename}")
            kwargs["attachments"] = [datei]
        try:
            await interaction.response.edit_message(**kwargs)
        except discord.InteractionResponded:
            await interaction.edit_original_response(**kwargs)
        # Die fertige Schicht darf jetzt weg - aber erst nach der Gnadenfrist,
        # damit man das Ergebnis noch lesen kann.
        _freigeben(self.message)


class RateModal(discord.ui.Modal):
    """Eingabefeld fuer alles, was ein Wort erwartet."""

    def __init__(self, ziel, laenge, titel="Dein Versuch"):
        super().__init__(title=titel, timeout=120)
        self.ziel = ziel
        self.feld = discord.ui.TextInput(
            label=f"{laenge} Buchstaben", min_length=laenge, max_length=laenge,
            placeholder="Nur Buchstaben, keine Umlaute")
        self.add_item(self.feld)

    async def on_submit(self, interaction):
        await self.ziel.versuch(interaction, str(self.feld.value))


class WordleView(SchichtView):
    def __init__(self, chef, uid, schicht, spiel):
        super().__init__(chef, uid, schicht)
        self.spiel = spiel

    @discord.ui.button(label="Raten", emoji="✏️", style=discord.ButtonStyle.primary)
    async def raten(self, interaction, button):
        await interaction.response.send_modal(RateModal(self, self.spiel.laenge))

    async def versuch(self, interaction, roh):
        status = self.spiel.raten(roh)
        if status == "laenge":
            await interaction.response.send_message(
                f"{self.spiel.laenge} Buchstaben, keine Zahlen. Nochmal.",
                ephemeral=True)
            return
        if status == "geloest":
            # Weniger Versuche = mehr Lohn. Der Faktor 2.0 (Anhieb) landet bei
            # 1.5, der Faktor 1.0 (letzter Versuch) bei 1.0 - so bleibt selbst
            # die Zitterpartie voll bezahlt, der Volltreffer aber besser.
            anteil = self.spiel.lohnfaktor() / 2.0 + 0.5
            n = len(self.spiel.versuche)
            datei, _n = await _wordle_bild(self.spiel, "GEKNACKT",
                                     f"{n} Versuch{'e' if n != 1 else ''} gebraucht")
            await self.beenden(interaction, "🟩 Geknackt",
                               f"**{self.spiel.loesung}** – in {n} Versuch"
                               f"{'en' if n != 1 else ''}.", anteil, datei)
            return
        if status == "aus":
            datei, _n = await _wordle_bild(self.spiel, "VORBEI", "Alle sechs Versuche weg",
                                     loesung=self.spiel.loesung)
            await self.beenden(interaction, "🟥 Vorbei",
                               f"Das Wort war **{self.spiel.loesung}**.", 0.0, datei)
            return
        e = discord.Embed(
            title=self.schicht.titel,
            description=f"Noch **{self.spiel.offen}** Versuche.",
            color=discord.Color.blurple())
        datei, name = await _wordle_bild(self.spiel, "WORDLE-SCHICHT",
                                   f"Noch {self.spiel.offen} Versuche")
        kwargs = {"embed": e, "view": self}
        if datei is not None:
            e.set_image(url=f"attachment://{name}")
            kwargs["attachments"] = [datei]
        await interaction.response.edit_message(**kwargs)


class SalatView(SchichtView):
    def __init__(self, chef, uid, schicht, wort):
        super().__init__(chef, uid, schicht)
        self.wort = wort.upper()
        self.offen = schicht.VERSUCHE

    @discord.ui.button(label="Lösen", emoji="🔤", style=discord.ButtonStyle.primary)
    async def loesen(self, interaction, button):
        await interaction.response.send_modal(
            RateModal(self, len(self.wort), "Wie heißt das Wort?"))

    async def versuch(self, interaction, roh):
        wort = (roh or "").strip().upper()
        if wort == self.wort:
            # Beim ersten Anlauf voller Lohn, danach weniger.
            anteil = {3: 1.0, 2: 0.75, 1: 0.5}.get(self.offen, 0.5)
            await self.beenden(interaction, "🔤 Entwirrt",
                               f"**{self.wort}** – richtig.", anteil)
            return
        self.offen -= 1
        if self.offen <= 0:
            await self.beenden(interaction, "🔤 Daneben",
                               f"Das Wort war **{self.wort}**.", 0.0)
            return
        await interaction.response.send_message(
            f"Nein. Noch **{self.offen}** Versuch{'e' if self.offen != 1 else ''}.",
            ephemeral=True)


class RechenView(SchichtView):
    def __init__(self, chef, uid, schicht):
        super().__init__(chef, uid, schicht)
        self.runde = 0
        self.richtig = 0
        self._neue_aufgabe()

    def _neue_aufgabe(self):
        art = random.choice("+-*")
        if art == "+":
            a, b = random.randint(12, 89), random.randint(12, 89)
            self.frage, self.loesung = f"{a} + {b}", a + b
        elif art == "-":
            a, b = random.randint(40, 120), random.randint(10, 39)
            self.frage, self.loesung = f"{a} − {b}", a - b
        else:
            a, b = random.randint(3, 14), random.randint(3, 14)
            self.frage, self.loesung = f"{a} × {b}", a * b
        # Drei plausible Falschantworten - nah dran, damit Raten nicht reicht.
        falsch = set()
        while len(falsch) < 3:
            weg = random.choice([-1, 1]) * random.randint(1, max(3, self.loesung // 8 + 2))
            if self.loesung + weg > 0 and weg != 0:
                falsch.add(self.loesung + weg)
        optionen = list(falsch) + [self.loesung]
        random.shuffle(optionen)
        self.clear_items()
        for wert in optionen:
            self.add_item(RechenKnopf(wert))

    def frage_text(self):
        return (f"**Aufgabe {self.runde + 1} von {self.schicht.RUNDEN}**\n"
                f"# {self.frage} = ?")


class RechenKnopf(discord.ui.Button):
    def __init__(self, wert):
        super().__init__(label=str(wert), style=discord.ButtonStyle.secondary)
        self.wert = wert

    async def callback(self, interaction):
        v = self.view
        if self.wert != v.loesung:
            # Was schon stand, wird halb bezahlt - Aufhoeren ist nie besser
            # als es zu versuchen.
            await v.beenden(
                interaction, "🧮 Verrechnet",
                f"{v.frage} = **{v.loesung}**, nicht {self.wert}.\n"
                f"Geschafft: **{v.richtig} von {v.schicht.RUNDEN}**.",
                v.richtig / v.schicht.RUNDEN * 0.5)
            return
        v.richtig += 1
        v.runde += 1
        if v.runde >= v.schicht.RUNDEN:
            await v.beenden(interaction, "🧮 Schicht sauber",
                            f"Alle **{v.schicht.RUNDEN}** richtig.", 1.0)
            return
        v._neue_aufgabe()
        e = discord.Embed(title=v.schicht.titel, description=v.frage_text(),
                          color=discord.Color.blurple())
        e.set_footer(text=f"{v.richtig} richtig")
        await interaction.response.edit_message(embed=e, view=v)


class SafeModal(discord.ui.Modal):
    def __init__(self, view):
        super().__init__(title="Code eingeben", timeout=120)
        self.view_ref = view
        self.feld = discord.ui.TextInput(label="Drei Ziffern", min_length=3,
                                         max_length=3, placeholder="z. B. 407")
        self.add_item(self.feld)

    async def on_submit(self, interaction):
        await self.view_ref.versuch(interaction, str(self.feld.value))


class SafeView(SchichtView):
    def __init__(self, chef, uid, schicht, code):
        super().__init__(chef, uid, schicht)
        self.code = code
        self.offen = schicht.VERSUCHE
        self.verlauf = []

    @discord.ui.button(label="Versuchen", emoji="🔐", style=discord.ButtonStyle.primary)
    async def knacken(self, interaction, button):
        await interaction.response.send_modal(SafeModal(self))

    def hinweis(self, versuch):
        """Wie bei Mastermind: richtige Ziffer am richtigen Platz, richtige
        Ziffer am falschen Platz. Ohne die zweistufige Zaehlung waere '111'
        gegen '123' dreimal 'dabei', obwohl nur eine Eins drinsteckt."""
        genau = sum(1 for a, b in zip(versuch, self.code) if a == b)
        rest_code = [b for a, b in zip(versuch, self.code) if a != b]
        dabei = 0
        for a, b in zip(versuch, self.code):
            if a != b and a in rest_code:
                rest_code.remove(a)
                dabei += 1
        return genau, dabei

    async def versuch(self, interaction, roh):
        versuch = (roh or "").strip()
        # numfmt.ist_zahl statt der eingebauten Ziffernpruefung: die sagt auch
        # bei hochgestellten Ziffern ("²²²") ja. Der Code waere damit nie zu
        # knacken, und der Spieler saehe nur, dass nichts passiert.
        if len(versuch) != 3 or not numfmt.ist_zahl(versuch):
            await interaction.response.send_message("Drei Ziffern. Nur Ziffern.",
                                                    ephemeral=True)
            return
        if versuch == self.code:
            gebraucht = self.schicht.VERSUCHE - self.offen + 1
            anteil = {1: 1.2, 2: 1.0, 3: 0.85, 4: 0.7, 5: 0.55}.get(gebraucht, 0.55)
            await self.beenden(interaction, "🔐 Offen",
                               f"Code war **{self.code}** – {gebraucht} Versuch"
                               f"{'e' if gebraucht != 1 else ''} gebraucht.", anteil)
            return
        genau, dabei = self.hinweis(versuch)
        self.offen -= 1
        self.verlauf.append(f"`{versuch}` → **{genau}** richtig platziert, "
                            f"**{dabei}** richtige Ziffer am falschen Platz")
        if self.offen <= 0:
            await self.beenden(interaction, "🔐 Zu",
                               f"Der Code war **{self.code}**.\n\n"
                               + "\n".join(self.verlauf), 0.0)
            return
        e = discord.Embed(title=self.schicht.titel,
                          description="\n".join(self.verlauf) +
                                      f"\n\nNoch **{self.offen}** Versuche.",
                          color=discord.Color.blurple())
        await interaction.response.edit_message(embed=e, view=self)


class SortierView(SchichtView):
    def __init__(self, chef, uid, schicht, zahlen):
        super().__init__(chef, uid, schicht)
        self.reihenfolge = sorted(zahlen)
        self.gedrueckt = 0
        for z in zahlen:
            self.add_item(SortierKnopf(z))


class SortierKnopf(discord.ui.Button):
    def __init__(self, zahl):
        super().__init__(label=str(zahl), style=discord.ButtonStyle.secondary)
        self.zahl = zahl

    async def callback(self, interaction):
        v = self.view
        if self.zahl != v.reihenfolge[v.gedrueckt]:
            await v.beenden(
                interaction, "📋 Durcheinander",
                f"**{self.zahl}** war nicht dran – **{v.reihenfolge[v.gedrueckt]}** "
                f"wäre richtig gewesen.\nGeschafft: **{v.gedrueckt} von "
                f"{len(v.reihenfolge)}**.",
                v.gedrueckt / len(v.reihenfolge) * 0.5)
            return
        v.gedrueckt += 1
        self.disabled = True
        self.style = discord.ButtonStyle.success
        if v.gedrueckt >= len(v.reihenfolge):
            await v.beenden(interaction, "📋 Plan steht",
                            "Alles in der richtigen Reihenfolge.", 1.0)
            return
        e = discord.Embed(title=v.schicht.titel,
                          description=f"**{v.gedrueckt} von {len(v.reihenfolge)}** – "
                                      f"weiter.",
                          color=discord.Color.blurple())
        await interaction.response.edit_message(embed=e, view=v)


class PaareView(SchichtView):
    """Memory mit acht Kisten. Zwei aufmachen, passt es, bleiben sie offen."""

    def __init__(self, chef, uid, schicht, karten):
        super().__init__(chef, uid, schicht)
        self.karten = karten
        self.offen = []          # gerade aufgedeckte Knopf-Nummern
        self.gefunden = 0
        self.griffe = 0
        self.sperre = False      # waehrend das falsche Paar noch sichtbar ist
        for i in range(len(karten)):
            self.add_item(PaarKnopf(i))

    def stand(self):
        perfekt = self.schicht.PAARE * 2
        return (f"**{self.gefunden} von {self.schicht.PAARE}** Paaren · "
                f"{self.griffe} Griffe (perfekt wären {perfekt})")


class PaarKnopf(discord.ui.Button):
    def __init__(self, nr):
        # Ein blosses Leerzeichen als Beschriftung waere riskant: Discord
        # schneidet Randweiss ab und lehnt leere Labels ab. Ein Fragezeichen
        # sagt ausserdem klar "hier liegt noch was drunter".
        super().__init__(label="?", style=discord.ButtonStyle.secondary,
                         row=nr // 4)
        self.nr = nr

    async def callback(self, interaction):
        v = self.view
        # Waehrend das falsche Paar noch offen liegt, wird nicht weitergeklickt -
        # sonst deckt ein schneller Klick drei Kisten auf und die Rechnung unten
        # geht nicht mehr auf.
        if v.sperre or self.disabled or self.nr in v.offen:
            await interaction.response.defer()
            return
        v.offen.append(self.nr)
        self.label = None
        self.emoji = v.karten[self.nr]
        self.style = discord.ButtonStyle.primary

        if len(v.offen) < 2:
            await interaction.response.edit_message(view=v)
            return

        v.griffe += 1
        a, b = v.offen
        treffer = v.karten[a] == v.karten[b]
        if treffer:
            v.gefunden += 1
            for nr in (a, b):
                knopf = v.children[nr]
                knopf.style = discord.ButtonStyle.success
                knopf.disabled = True
            v.offen = []
            if v.gefunden >= v.schicht.PAARE:
                # Perfekt sind PAARE Griffe (jeder sitzt), doppelt so viele
                # sind noch in Ordnung, darunter wird es duenn.
                perfekt = v.schicht.PAARE
                anteil = max(0.4, min(1.2, (perfekt * 2) / max(1, v.griffe) * 0.6))
                await v.beenden(interaction, "🧰 Kiste sortiert",
                                f"Alle {perfekt} Paare in **{v.griffe} Griffen**.",
                                anteil)
                return
            e = discord.Embed(title=v.schicht.titel, description=v.stand(),
                              color=discord.Color.blurple())
            await interaction.response.edit_message(embed=e, view=v)
            return

        # Daneben: kurz zeigen, dann wieder zumachen.
        v.sperre = True
        e = discord.Embed(title=v.schicht.titel,
                          description=v.stand() + "\n\nPasst nicht.",
                          color=discord.Color.blurple())
        await interaction.response.edit_message(embed=e, view=v)
        await asyncio.sleep(1.1)
        for nr in v.offen:
            knopf = v.children[nr]
            knopf.emoji = None
            knopf.label = "?"
            knopf.style = discord.ButtonStyle.secondary
        v.offen = []
        v.sperre = False
        try:
            await interaction.edit_original_response(view=v)
        except discord.HTTPException:
            pass


class KontrolleView(SchichtView):
    """Vier gehoeren zusammen, eins nicht. Dreimal."""

    def __init__(self, chef, uid, schicht):
        super().__init__(chef, uid, schicht)
        self.runde = 0
        self.richtig = 0
        self._neue_runde()

    def _neue_runde(self):
        kisten = list(self.schicht.KISTEN)
        heimat, fremd = random.sample(kisten, 2)
        stuecke = random.sample(self.schicht.KISTEN[heimat], 4)
        self.ausschuss = random.choice(self.schicht.KISTEN[fremd])
        self.heimat = heimat
        auswahl = stuecke + [self.ausschuss]
        random.shuffle(auswahl)
        self.clear_items()
        for wort in auswahl:
            self.add_item(KontrolleKnopf(wort))

    def frage_text(self):
        return f"**Runde {self.runde + 1} von {self.schicht.RUNDEN}**"


class KontrolleKnopf(discord.ui.Button):
    def __init__(self, wort):
        super().__init__(label=wort, style=discord.ButtonStyle.secondary)
        self.wort = wort

    async def callback(self, interaction):
        v = self.view
        if self.wort != v.ausschuss:
            await v.beenden(
                interaction, "🔍 Durchgerutscht",
                f"**{self.wort}** ist {v.heimat} – der Ausschuss war "
                f"**{v.ausschuss}**.\nGeschafft: **{v.richtig} von "
                f"{v.schicht.RUNDEN}**.",
                v.richtig / v.schicht.RUNDEN * 0.5)
            return
        v.richtig += 1
        v.runde += 1
        if v.runde >= v.schicht.RUNDEN:
            await v.beenden(interaction, "🔍 Alles geprüft",
                            f"Alle **{v.schicht.RUNDEN}** Runden sauber.", 1.0)
            return
        v._neue_runde()
        e = discord.Embed(title=v.schicht.titel,
                          description="Vier gehören zusammen, **eins nicht**.\n\n"
                                      + v.frage_text(),
                          color=discord.Color.blurple())
        e.set_footer(text=f"{v.richtig} richtig")
        await interaction.response.edit_message(embed=e, view=v)


# ===========================================================================
# Wort des Tages: ein Knopf fuer alle, jeder raet fuer sich
# ===========================================================================
class TagesModal(discord.ui.Modal):
    def __init__(self, gid, laenge, *, nur_ehre=False):
        super().__init__(title=("Wort des Tages – nur für die Ehre" if nur_ehre
                                else "Wort des Tages"), timeout=120)
        self.gid = int(gid)
        self.feld = discord.ui.TextInput(
            label=f"{laenge} Buchstaben", min_length=laenge, max_length=laenge,
            placeholder=("Der Topf ist weg – zählt für deine Bilanz"
                         if nur_ehre else "Nur Buchstaben, keine Umlaute"))
        self.add_item(self.feld)

    async def on_submit(self, interaction):
        await instance.tages_antwort(interaction, self.gid, str(self.feld.value))


class TagesKnopf(discord.ui.Button):
    def __init__(self, gid):
        super().__init__(label="Raten", emoji="🟩",
                         style=discord.ButtonStyle.success,
                         custom_id=f"flo:wordle:{int(gid)}")
        self.gid = int(gid)

    async def callback(self, interaction):
        await instance.tages_knopf(interaction, self.gid)


class TagesView(discord.ui.View):
    """Bleibt dauerhaft aktiv (timeout=None) - auch nach einem Neustart, dank
    fester custom_id und register_views()."""

    def __init__(self, gid):
        super().__init__(timeout=None)
        self.message = None
        self.add_item(TagesKnopf(gid))


# --- Singleton + Modul-API ---------------------------------------------------
instance = Arbeit()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
tick = instance.tick
register_views = instance.register_views
kanal_fuer = instance.kanal_fuer
raetsel = instance.raetsel
leute_im_voice = instance.leute_im_voice
