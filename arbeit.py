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
from basis import FeatureBasis
import economy
import numfmt
from store import JsonStore

log = logging.getLogger("dcbot.arbeit")

# Sentinel: wir haben selbst geantwortet -> bot.py schweigt.
HANDLED = object()

# --- Befehlswoerter ---------------------------------------------------------
_CMDS = ("work", "arbeit", "arbeiten", "job", "schicht", "malochen")
_LOHN_CMDS = ("lohnzettel", "lohn", "gehalt", "arbeitszeugnis")
_WORDLE_CMDS = ("wordle", "wordl", "tageswort", "wortdestages")

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

# --- Wort des Tages ---------------------------------------------------------
TAGES_PRO_BUCHSTABE = int(os.getenv("WORDLE_PRO_BUCHSTABE", "10000") or "10000")
# Wortlaenge nach Wochentag (Montag = 0). Zum Wochenende laenger und damit
# lukrativer - Sonntag ist Zahltag.
TAGES_LAENGE = {0: 5, 1: 5, 2: 5, 3: 5, 4: 6, 5: 7, 6: 8}
# Je weniger Versuche, desto mehr. Schluessel = gebrauchte Versuche.
VERSUCH_FAKTOR = {1: 2.00, 2: 1.75, 3: 1.50, 4: 1.30, 5: 1.15, 6: 1.00}
MAX_VERSUCHE = 6
# So viele Leute muessen im Voice sitzen, damit sich das Raetsel lohnt.
STANDARD_MIN_VOICE = 3

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

    # --- Anzeige ---
    def muster(self, versuch):
        """Die gruen/gelb/grau-Zeile zu einem Versuch.

        Die ZWEISTUFIGE Zaehlweise ist der Kern und wird gern falsch gemacht:
        erst alle exakten Treffer wegnehmen, DANN die restlichen Buchstaben
        verteilen. Sonst faerbt 'OTTER' gegen 'NOTEN' beide T gelb, obwohl in
        der Loesung nur ein T steckt."""
        versuch = str(versuch).upper()
        muster = ["⬛"] * self.laenge
        rest = {}
        for i, richtig in enumerate(self.loesung):
            if i < len(versuch) and versuch[i] == richtig:
                muster[i] = "🟩"
            else:
                rest[richtig] = rest.get(richtig, 0) + 1
        for i in range(self.laenge):
            if muster[i] == "🟩" or i >= len(versuch):
                continue
            if rest.get(versuch[i], 0) > 0:
                muster[i] = "🟨"
                rest[versuch[i]] -= 1
        return "".join(muster)

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
        if laenge is None:
            try:
                laenge = TAGES_LAENGE[datetime.strptime(tag, "%Y-%m-%d").weekday()]
            except (TypeError, ValueError):
                laenge = 5
        liste = WOERTER.get(laenge) or WOERTER[5]
        roh = hashlib.sha256(f"{tag}:{int(gid or 0)}".encode()).hexdigest()
        return cls(liste[int(roh, 16) % len(liste)], versuche=versuche)


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

        status: 'aus_runde' (Runde schon entschieden) | 'fertig' (diese Person
        ist durch) | 'laenge' | 'weiter' | 'geloest' | 'aus'."""
        if not self.laeuft:
            return "kein_wort", None
        if self.entschieden:
            return "aus_runde", self.spiel_von(uid)
        spiel = self.spiel_von(uid)
        status = spiel.raten(wort)
        if status in ("laenge", "fertig"):
            return status, spiel
        self.daten.setdefault("spieler", {})[str(int(uid))] = list(spiel.versuche)
        if status == "geloest":
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

    def bauen(self, chef, autor):
        """Gibt (embed, view) zurueck. Muss jede Unterklasse liefern."""
        raise NotImplementedError

    def kopf(self, text):
        e = discord.Embed(title=self.titel, description=text,
                          color=discord.Color.blurple())
        e.set_footer(text=random.choice(_SCHICHT_START))
        return e


class WordleSchicht(Schicht):
    key, titel = "wordle", "🟩 Wordle-Schicht"
    was = "Fünf Buchstaben, sechs Versuche."
    lohn = 9000

    def bauen(self, chef, autor):
        spiel = Wordle.zufall(5)
        view = WordleView(chef, autor.id, self, spiel)
        return self.kopf(f"Fünf Buchstaben, **{MAX_VERSUCHE} Versuche**.\n\n"
                         f"{spiel.tafel()}\n\n"
                         f"Nur **{autor.display_name}** darf raten."), view


class SalatSchicht(Schicht):
    key, titel = "salat", "🔤 Buchstabensalat"
    was = "Wort entwirren, drei Versuche."
    lohn = 7000
    VERSUCHE = 3

    def bauen(self, chef, autor):
        wort = random.choice(WOERTER[6] + WOERTER[7])
        buchstaben = list(wort)
        # Wirklich mischen: sonst steht das Wort im Klartext da.
        for _ in range(12):
            random.shuffle(buchstaben)
            if "".join(buchstaben) != wort:
                break
        view = SalatView(chef, autor.id, self, wort)
        return self.kopf(f"Entwirr das:\n\n# `{' '.join(buchstaben)}`\n\n"
                         f"**{self.VERSUCHE} Versuche.** {len(wort)} Buchstaben."), view


class RechenSchicht(Schicht):
    key, titel = "rechnen", "🧮 Rechenschicht"
    was = "Fünf Aufgaben, jede falsche kostet den Rest."
    lohn = 6500
    RUNDEN = 5

    def bauen(self, chef, autor):
        view = RechenView(chef, autor.id, self)
        return self.kopf("Fünf Aufgaben. Jede richtige zählt, "
                         "eine falsche beendet die Schicht.\n\n"
                         + view.frage_text()), view


class SafeSchicht(Schicht):
    key, titel = "safe", "🔐 Safe knacken"
    was = "Zahlenschloss, Hinweise nach jedem Versuch."
    lohn = 7500
    VERSUCHE = 5

    def bauen(self, chef, autor):
        code = "".join(random.choice("0123456789") for _ in range(3))
        view = SafeView(chef, autor.id, self, code)
        return self.kopf("Ein **dreistelliger** Code. Nach jedem Versuch sagt "
                         "dir das Schloss, wie nah du dran warst.\n\n"
                         f"**{self.VERSUCHE} Versuche.**"), view


class SortierSchicht(Schicht):
    key, titel = "sortieren", "📋 Schichtplan ordnen"
    was = "In die richtige Reihenfolge klicken."
    lohn = 6000

    def bauen(self, chef, autor):
        zahlen = random.sample(range(10, 100), 5)
        view = SortierView(chef, autor.id, self, zahlen)
        return self.kopf("Klick die Zahlen **von klein nach groß**.\n"
                         "Ein Fehlklick und die Schicht ist rum."), view


SCHICHTEN = {s.key: s for s in (WordleSchicht(), SalatSchicht(), RechenSchicht(),
                                SafeSchicht(), SortierSchicht())}


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
        return topf.setdefault(str(int(uid)), {
            "cooldown": 0, "serie": 0, "schichten": 0, "geschafft": 0,
            "verdient": 0, "tag": "", "heute": 0})

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

    def _serie_faktor(self, prof):
        return 1.0 + min(SERIE_MAX, int(prof.get("serie", 0)) * SERIE_SCHRITT)

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
            return self._lohnzettel(message.author)
        if erst in _WORDLE_CMDS and len(teile) == 1:
            return await self._tages_zeigen(message)
        if erst not in _CMDS:
            return None

        zweit = teile[1].lower().strip(".,!?") if len(teile) > 1 else ""
        if zweit in ("liste", "list", "was", "hilfe", "help"):
            return self._schichtliste()
        return await self._schicht_starten(message, SCHICHTEN.get(zweit))

    def _schichtliste(self):
        e = discord.Embed(
            title="🧰 Die Schichten",
            description=(f"`{self._bot_name} work` gibt dir eine zufällige – "
                         f"`{self._bot_name} work <name>` genau die hier:"),
            color=discord.Color.blurple())
        for key, s in SCHICHTEN.items():
            e.add_field(name=f"{s.titel} · `{key}`",
                        value=f"{s.was}\nGrundlohn **{_muenzen(s.lohn)}** {economy.COIN}",
                        inline=False)
        e.set_footer(text=f"Alle {COOLDOWN // 60} Minuten eine Schicht · "
                          f"höchstens {_muenzen(TAGES_DECKEL)} am Tag")
        return e

    def _lohnzettel(self, autor):
        prof = self._nutzer(autor.id)
        heute = self._tageskonto(prof)
        quote = 0
        if prof.get("schichten"):
            quote = round(100 * int(prof.get("geschafft", 0)) / int(prof["schichten"]))
        e = discord.Embed(title=f"🧾 Lohnzettel · {autor.display_name}",
                          color=discord.Color.green())
        e.add_field(name="Schichten",
                    value=f"{prof.get('schichten', 0)} angetreten\n"
                          f"{prof.get('geschafft', 0)} geschafft ({quote} %)")
        e.add_field(name="Serie",
                    value=f"**{prof.get('serie', 0)}** in Folge\n"
                          f"Zuschlag **+{round((self._serie_faktor(prof) - 1) * 100)} %**")
        e.add_field(name="Verdient",
                    value=f"insgesamt **{_muenzen(prof.get('verdient', 0))}** "
                          f"{economy.COIN}\nheute **{_muenzen(heute)}** von "
                          f"{_muenzen(TAGES_DECKEL)}")
        warte = int(prof.get("cooldown", 0)) - int(time.time())
        e.set_footer(text=(f"Nächste Schicht in {warte // 60 + 1} Min."
                           if warte > 0 else "Du kannst sofort ran."))
        return e

    async def _schicht_starten(self, message, schicht=None):
        prof = self._nutzer(message.author.id)
        warte = int(prof.get("cooldown", 0)) - int(time.time())
        if warte > 0:
            return self._kurz(f"⏳ Pause. Nächste Schicht in "
                              f"**{warte // 60 + 1} Minuten**.",
                              discord.Color.orange())
        if self._tageskonto(prof) >= TAGES_DECKEL:
            return self._kurz(
                f"🛑 Feierabend. Du hast heute deine **{_muenzen(TAGES_DECKEL)}** "
                f"{economy.COIN} voll. Morgen wieder.", discord.Color.orange())

        schicht = schicht or random.choice(list(SCHICHTEN.values()))
        prof["cooldown"] = int(time.time()) + COOLDOWN
        prof["schichten"] = int(prof.get("schichten", 0)) + 1
        self._speichern()

        embed, view = schicht.bauen(self, message.author)
        try:
            view.message = await message.channel.send(embed=embed, view=view)
        except discord.HTTPException:
            log.exception("Schicht konnte nicht gestartet werden")
            return "Die Schicht ließ sich nicht aufmachen."
        return HANDLED

    # --- Abrechnung einer Schicht ----------------------------------------
    def abrechnen(self, uid, schicht, anteil):
        """Zahlt eine beendete Schicht aus. 'anteil' ist die Leistung (0..1,2).

        Gibt (betrag, serie, hinweis) zurueck; die View baut daraus das Ergebnis."""
        prof = self._nutzer(uid)
        if anteil > 0:
            prof["serie"] = int(prof.get("serie", 0)) + 1
            prof["geschafft"] = int(prof.get("geschafft", 0)) + 1
        else:
            prof["serie"] = 0
        gewollt = int(round(schicht.lohn * anteil * self._serie_faktor(prof)))
        echt, frei = self._auszahlen(uid, gewollt, f"arbeit:{schicht.key}")
        return echt, int(prof.get("serie", 0)), self._deckel_hinweis(gewollt, echt, frei)

    def ergebnis_embed(self, autor, titel, text, betrag, serie, hinweis, gut):
        e = discord.Embed(
            title=titel,
            description=text + "\n\n" + random.choice(_LOB if gut else _TADEL),
            color=discord.Color.green() if gut else discord.Color.red())
        if betrag > 0:
            e.add_field(name="Lohn", value=f"**+{_muenzen(betrag)}** {economy.COIN}")
        if serie:
            zuschlag = round(min(SERIE_MAX, serie * SERIE_SCHRITT) * 100)
            e.add_field(name="Serie", value=f"**{serie}** in Folge (+{zuschlag} %)")
        if hinweis:
            e.add_field(name="Hinweis", value=hinweis, inline=False)
        e.set_footer(text=f"{autor.display_name} · nächste Schicht in "
                          f"{COOLDOWN // 60} Minuten")
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

    def faellig(self, guild):
        """Ist auf diesem Server heute noch kein Wort gefallen - und lohnt es sich?

        Nicht die Uhr entscheidet, sondern ob wirklich jemand da ist. Ein Raetsel
        um 4 Uhr morgens in einen leeren Server zu werfen, waere verschenkt: bis
        abends jemand hinschaut, hat es keiner mitbekommen."""
        if self.raetsel(getattr(guild, "id", 0)).datum == _heute():
            return False
        return self.leute_im_voice(guild) >= self.min_voice(getattr(guild, "id", 0))

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
                raus.append((guild, self.tages_embed(guild.id), TagesView(guild.id)))
            except Exception:  # noqa: BLE001
                log.exception("Wort des Tages fuer %s fehlgeschlagen", guild)
        return raus

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
        try:
            await message.channel.send(embed=self.tages_embed(gid),
                                       view=TagesView(gid),
                                       reference=message, mention_author=False)
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
        if raetsel.entschieden:
            await interaction.response.send_message(
                f"Zu spät – <@{raetsel.gewinner}> war schneller. "
                f"Das Wort war **{raetsel.wort}**.", ephemeral=True)
            return
        spiel = raetsel.spiel_von(interaction.user.id)
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
        if status == "aus_runde":
            await interaction.response.send_message(
                f"Zu spät – <@{raetsel.gewinner}> war schneller.", ephemeral=True)
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
        if status == "weiter":
            e = discord.Embed(
                title="🟩 Wort des Tages",
                description=f"{spiel.tafel()}\n\nNoch **{spiel.offen}** Versuche.",
                color=discord.Color.blurple())
            e.set_footer(text="Nur du siehst das hier.")
            await interaction.response.send_message(embed=e, ephemeral=True)
            return
        if status == "aus":
            e = discord.Embed(
                title="🟥 Alle Versuche weg",
                description=f"{spiel.tafel(verdeckt=True)}\n\n"
                            f"Sechs Versuche, nichts. Das Wort verrate ich nicht – "
                            f"es rennen ja noch andere.",
                color=discord.Color.red())
            await interaction.response.send_message(embed=e, ephemeral=True)
            return

        # Gewonnen: Topf kassieren, Runde schliessen.
        gewollt = raetsel.preis(spiel)
        echt, frei = self._auszahlen(interaction.user.id, gewollt, "arbeit:tageswordle")
        hinweis = self._deckel_hinweis(gewollt, echt, frei)
        self._speichern()

        e = discord.Embed(
            title="🏆 Wort des Tages geknackt",
            description=f"{spiel.tafel()}\n\n**{spiel.loesung}** – in "
                        f"{len(spiel.versuche)} Versuch"
                        f"{'en' if len(spiel.versuche) != 1 else ''}.",
            color=discord.Color.gold())
        e.add_field(name="Topf", value=f"**+{_muenzen(echt)}** {economy.COIN}")
        if hinweis:
            e.add_field(name="Hinweis", value=hinweis, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)
        await self._tages_ausrufen(interaction, raetsel, spiel, echt)

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
            await nachricht.edit(embed=self.tages_embed(raetsel.gid), view=None)
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

    def __init__(self, chef, uid, schicht, timeout=180):
        super().__init__(timeout=timeout)
        self.chef = chef
        self.uid = int(uid)
        self.schicht = schicht
        self.message = None
        self.fertig = False

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
        self.chef.abrechnen(self.uid, self.schicht, 0.0)
        try:
            await self.message.edit(
                embed=discord.Embed(title="⌛ Schicht verpennt",
                                    description="Zu lange nichts gemacht. Kein Lohn.",
                                    color=discord.Color.dark_grey()),
                view=None)
        except discord.HTTPException:
            pass

    async def beenden(self, interaction, titel, text, anteil):
        """Schicht abschliessen, auszahlen, Ergebnis zeigen."""
        self.fertig = True
        self.stop()
        betrag, serie, hinweis = self.chef.abrechnen(self.uid, self.schicht, anteil)
        emb = self.chef.ergebnis_embed(interaction.user, titel, text,
                                       betrag, serie, hinweis, anteil > 0)
        try:
            await interaction.response.edit_message(embed=emb, view=None)
        except discord.InteractionResponded:
            await interaction.edit_original_response(embed=emb, view=None)


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
            await self.beenden(interaction, "🟩 Geknackt",
                               f"**{self.spiel.loesung}** – in {n} Versuch"
                               f"{'en' if n != 1 else ''}.\n\n{self.spiel.tafel()}",
                               anteil)
            return
        if status == "aus":
            await self.beenden(interaction, "🟥 Vorbei",
                               f"Das Wort war **{self.spiel.loesung}**.\n\n"
                               f"{self.spiel.tafel()}", 0.0)
            return
        e = discord.Embed(
            title=self.schicht.titel,
            description=f"Noch **{self.spiel.offen}** Versuche.\n\n"
                        f"{self.spiel.tafel()}",
            color=discord.Color.blurple())
        await interaction.response.edit_message(embed=e, view=self)


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


# ===========================================================================
# Wort des Tages: ein Knopf fuer alle, jeder raet fuer sich
# ===========================================================================
class TagesModal(discord.ui.Modal):
    def __init__(self, gid, laenge):
        super().__init__(title="Wort des Tages", timeout=120)
        self.gid = int(gid)
        self.feld = discord.ui.TextInput(
            label=f"{laenge} Buchstaben", min_length=laenge, max_length=laenge,
            placeholder="Nur Buchstaben, keine Umlaute")
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
