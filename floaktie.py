"""Die FloCorp-Aktie ($FLO): Flos eigene Aktie zum Handeln.

Wie an einer echten Boerse - nur auf Discord gemuenzt:

- PREIS in Flo Coins pro Anteil. Er schwankt realistisch:
  * KAEUFE treiben den Kurs hoch, VERKAEUFE druecken ihn (Markt-Impact, je
    groesser die Order, desto staerker - gedeckelt). Wer kauft, zahlt den
    angehobenen Kurs; wer verkauft, bekommt den gedrueckten - ein sofortiger
    Hin-und-Her-Trade macht also IMMER Verlust (kein Gratis-Arbitrage).
  * VOICE-AKTIVITAET ueber mehrere Tage: sind viele Leute im Call, steigt der
    Kurs Tag fuer Tag; ist wenig los, faellt er. Gemessen ueber einen gleitenden
    Mehr-Tages-Schnitt (EMA), plus etwas Zufalls-Rauschen wie an echten Boersen.

- HANDEL: 'floaktie kauf 10' / 'floaktie verkauf alles' (oder per Buttons).
- LEADERBOARD: 'floaktie top' - wer die meisten Anteile haelt.
- VORTEIL: Aktionaere kassieren im Voice eine DIVIDENDE (mehr Anteile = mehr
  Coins pro Voice-Runde). Der groesste Aktionaer bekommt die doppelte Dividende.

Alle Coins laufen ueber economy (ein Topf). Dieses Modul haelt Kurs, Depots und
Kurs-Historie in data/floaktie.json.
"""

import asyncio
import logging
import math
import os
import random
import re
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import economy
from basis import FeatureBasis
from store import JsonStore

log = logging.getLogger("dcbot.floaktie")

# Sentinel: die Aktie hat selbst geantwortet (Panel gesendet) -> bot.py schweigt.
HANDLED = object()

# Marke.
NAME = "FloCorp"
TICKER = "$FLO"

# Befehlswoerter, die das Handels-Panel oeffnen (Kauf/Verkauf der EIGENEN Aktie).
_CMDS = ("floaktie", "floaktien", "aktie", "aktien", "flostock", "floshare",
         "flonyse", "$flo", "floboerse")
# Befehlswoerter, die direkt den Kurs-Chart (mit Zeitraum-Buttons) zeigen.
_CHART_CMDS = ("aktienkurs", "kurs", "kursverlauf", "chart", "flokurs")
# Zeitraeume fuer den Chart: (Label, Tage).
_RANGES = (("1 Tag", 1), ("7 Tage", 7), ("30 Tage", 30), ("Gesamt", 100000))
HISTORY_TICKS_MAX = 20000   # ~14 Tage Minuten-Takte (vorher 50 h -> 7/30/Gesamt sahen gleich aus)

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
# Nur der Besitzer darf die Aktie zuruecksetzen.
OWNER_ID = int(os.getenv("OWNER_ID", "1040135855710404659") or "0")

# --- Balance (per .env justierbar) -------------------------------------------
# Startkurs = FAIR_BASE: eine frisch zurueckgesetzte Aktie steht damit GENAU auf
# dem Wert eines toten Servers und kann sofort in beide Richtungen - vorher lag
# der Start unter dem Boden und der Kurs konnte anfangs gar nicht fallen.
START_PRICE = int(os.getenv("FLOAKTIE_START_PRICE", "10") or "10")   # Coins/Anteil
MIN_PRICE = int(os.getenv("FLOAKTIE_MIN_PRICE", "5") or "5")
# Absolute Untergrenze fuer den BASISkurs (nicht fuer den angezeigten Kurs!).
# Nur da, damit die Kurve nie auf 0 kollabiert - siehe _base().
BASE_FLOOR = 0.01
# Liquiditaet: so viele Anteile verdoppeln den Kurs. Kleiner = volatiler.
LIQUIDITY = int(os.getenv("FLOAKTIE_LIQUIDITY", "750") or "750")
# Gebuehr je Order (jede Richtung). Sorgt dafuer, dass ein sofortiger
# Hin-und-Her-Trade IMMER verliert (kein risikoloser Gewinn).
TRADE_FEE = float(os.getenv("FLOAKTIE_TRADE_FEE", "0.02") or "0.02")
# Groesste EINZEL-Order. Bewusst in der Groessenordnung der Liquiditaet: eine
# einzige Order kann den Kurs so maximal etwa verdoppeln, statt ihn (mit
# viele Anteile auf einmal) absurd zu verzerren. Mehr geht ueber mehrere Orders - dank der
# pfad-unabhaengigen Kurve kostet das genau dasselbe.
MAX_SHARES_PER_TRADE = int(os.getenv("FLOAKTIE_MAX_TRADE", "150") or "150")

# --- Aktivitaets-Modell: der Kurs reagiert JEDE MINUTE auf die Server-Aktivitaet -
# Bei JEDEM Sample-Takt (bot.py, alle FLOAKTIE_SAMPLE_SECONDS - Standard 60 s) wird
# der Kurs bewegt. Es zaehlen MEHRERE Kriterien:
#
#   Aktivitaet = Leute-im-Call
#              + STREAM_BONUS * Live-Streamer   (Go Live / Screenshare zaehlt extra)
#              + VIDEO_BONUS  * Kameras an
#              + MSG_WEIGHT   * Nachrichten seit dem letzten Takt (gedeckelt)
#
#   Aktivitaet > 0  ->  Kurs STEIGT IMMER. Tempo = Aktivitaet * TICK_GAIN
#   Aktivitaet = 0  ->  Kurs sinkt (je laenger leer, desto schneller), nie unter FAIR_BASE
#
# Die Regel ist genau so, wie sie gemeint ist: sitzt EIN Mensch im Call, steigt
# der Kurs - langsam. Sitzen zehn drin und streamen alle, steigt er rasant. Ist
# gar nichts los, sinkt er langsam. Es gibt KEINEN Fall, in dem der Kurs faellt,
# waehrend Leute im Call sind.
#
# Damit das trotzdem nicht ins Unendliche laeuft, wirkt die Aktivitaet zusaetzlich
# als "was ist der Server gerade wert" (Zielkurs): steht der Kurs schon weit
# darueber, steigt er WEITER - nur immer gemaechlicher (exponentielle Daempfung).
# Der Kurs laeuft also gegen ungefaehr das Vier- bis Fuenffache dessen, was die
# aktuelle Aktivitaet hergibt, statt zu explodieren. Faellt die Aktivitaet, sinkt
# dieser Deckel mit - der Kurs steigt dann nur noch minimal weiter.
#
#   Zielkurs = FAIR_BASE + AKT_WERT * Aktivitaet
#   Drift    = min(TICK_CAP, Aktivitaet * TICK_GAIN) * exp(-Ueberhang / LEVEL_SOFT)
#
# Vorher wurde von JEDER der 1440 Tagesminuten ein "Normalwert" von 3,0
# Aktivitaetspunkten ABGEZOGEN. Der Leerlauf hat damit jede Spitze gefressen -
# nachgemessen: 5 Stunden mit 8 Aktivitaetspunkten endeten bei -19,2 % am Tag,
# 2 Stunden mit 6 Leuten bei -36,0 %. Dazu war das Rauschen (+/-0,12 %/min) bei 4
# Leuten im Call 12x GROESSER als das Signal (+0,010 %/min): man KONNTE den
# Anstieg gar nicht sehen. Beides ist hier behoben.
# Kurs eines TOTEN Servers - und damit der Boden, unter den der Kurs im Leerlauf
# nicht mehr faellt. Der Wert muss NIEDRIG sein: er lag bei 5.000, waehrend der
# Kurs nach einem Reset bei 1.000 startet und im Betrieb oft darunter stand. Damit
# war der Kurs in einer Einbahnstrasse - im Leerlauf passierte GAR NICHTS, bis er
# sich erst versechsfacht hatte. Genau das kam im Betrieb an: "die Aktie sinkt nie".
FAIR_BASE = float(os.getenv("FLOAKTIE_FAIR_BASE", "10") or "10")
# --- Grundwert: was der Server LANGFRISTIG wert ist ------------------------
# Ein FESTER Boden funktioniert bei diesem Verfall nicht. Nachgerechnet ueber
# 60 simulierte Tage: mit -11 % je halber Stunde ist nach einer Nacht ohne Call
# alles weg, der Kurs stand jeden Morgen wieder auf dem Mindestwert 10 und jeden
# Abend bei ~130.000. Spanne 13.296-fach, bester 6-Stunden-Handel +1.129.685 %.
# Das ist kein Markt, das ist ein Saegezahn.
#
# Deshalb ist der Boden BEWEGLICH: die Aktie ist so viel wert, wie der Server im
# Schnitt lebendig ist. 'grund_akt' ist ein langsamer Mittelwert der Aktivitaet
# ueber GRUND_TAGE Tage (Leerlaufstunden zaehlen mit). Der Kurs faellt im
# Leerlauf also nicht mehr ins Nichts, sondern auf den Wert, den der Server
# regelmaessig hergibt - wer jeden Abend im Call sitzt, haelt den Boden oben.
# Stirbt der Server wirklich, sinkt auch der Grundwert und mit ihm der Boden.
GRUND_TAGE = float(os.getenv("FLOAKTIE_GRUND_TAGE", "3") or "3")
# Wie stark der Grundwert den Boden traegt. 'grund_akt' ist ein 24-Stunden-Schnitt
# und damit klein (ein Server mit 4 aktiven Stunden landet bei ~0,7 Punkten). Ohne
# diesen Faktor laege der Boden bei ~680, waehrend ein voller Abend auf ~128.000
# geht - eine Spanne von 182-fach, viel zu wild zum Handeln. Der Faktor hebt den
# Boden auf das Niveau eines DURCHSCHNITTLICHEN Abends.
GRUND_FAKTOR = float(os.getenv("FLOAKTIE_GRUND_FAKTOR", "4") or "4")
# Wert je Aktivitaetspunkt. Merksatz: Zielkurs ~ 1.000 x Aktivitaetspunkte.
# (Vorher 120 - damit "lohnte" sich bei 12 Punkten nur ein Kurs von 1.740, und
# jeder Kurs darueber wurde auf den Mindest-Anstieg heruntergebremst: der Kurs
# stand bei hoher Aktivitaet praktisch still. Genau das war der Fehler.)
AKT_WERT = float(os.getenv("FLOAKTIE_AKT_WERT", "1000") or "1000")
# Plus je Aktivitaetspunkt und Minute. 0,0012 heisst: 1 Zuhoerer +0,12 %/min,
# 20 Punkte +2,4 %/min, 40 Punkte +4,8 %/min. Das Tempo bestimmt nur, WIE SCHNELL
# der Kurs an sein Niveau kommt - wie HOCH er laeuft, legt AKT_WERT/CEIL_FACTOR
# fest. Deshalb darf es ruhig steil sein.
# 0,0028 heisst: 1 Zuhoerer +0,28 %/min, 3 Leute +0,84 %/min, ab 10 Punkten am
# Anschlag. Bewusst GEDROSSELT: mit 0,006 und AKT_WERT 3000 kam ein Abend mit
# 10 Leuten und 5 Streams auf 93 MILLIONEN Erloes fuer ein volles Depot - das war
# selbst dem Besitzer "zu krass". Jetzt sind es rund 9,6 Mio an so einem Abend
# und 1,3 Mio an einem normalen (3 Leute, ein Stream).
TICK_GAIN = float(os.getenv("FLOAKTIE_TICK_GAIN", "0.0028") or "0.0028")
TICK_CAP = float(os.getenv("FLOAKTIE_TICK_CAP", "0.0275") or "0.0275") # max +2,75 %/min
# Verfall bei LEEREM Call - SOFORT und GLEICHMAESSIG.
#
# Vorgabe des Besitzers: "sinken soll es direkt sobald keiner mehr im Call ist,
# aber rasant, gute 10 % pro 30 min." Genau das steht hier - und zwar in der
# Einheit, in der es gedacht ist, damit man beim Nachjustieren nicht erst
# Minutenraten umrechnen muss. Die Rampe von frueher ist ABSICHTLICH weg: sie
# hiess, dass in der ersten Viertelstunde spuerbar weniger passierte als danach,
# und genau das fuehlte sich an wie "es sinkt ja gar nicht".
IDLE_PER_30MIN = float(os.getenv("FLOAKTIE_IDLE_30MIN", "0.11") or "0.11")
# Daraus die Rate pro Minute, sauber ueber den Zinseszins: (1-r)^30 = 1-Vorgabe.
# 0,11 je halbe Stunde -> 0,388 %/min -> 20,8 % je Stunde, 61 % ueber 4 Stunden.
IDLE_RATE = 1.0 - (1.0 - min(0.99, max(0.0, IDLE_PER_30MIN))) ** (1.0 / 30.0)
# Wie stark es ueber dem Zielkurs gemaechlicher wird. Die Daempfung ist
# LOGARITHMISCH und damit sehr langatmig: beim Doppelten des Zielkurses noch 78 %
# Tempo, beim 15-fachen 48 %, beim 150-fachen 33 %. So steigt der Kurs auch dann
# noch spuerbar, wenn er laengst weit ueber dem "fairen" Wert steht - er wird nur
# gemaechlicher. (Vorher exponentiell mit 0,35: schon beim Doppelten waren es
# 6 % Tempo, und ab dem Dreifachen stand der Kurs praktisch still, obwohl Leute
# im Call sassen.)
LEVEL_SOFT = float(os.getenv("FLOAKTIE_LEVEL_SOFT", "2.5") or "2.5")
# Mindest-Anstieg, solange ueberhaupt jemand da ist: EIN Zuhoerer bei hohem Kurs
# soll den Kurs immer noch heben (+0,48 %/Stunde), nicht nur rechnerisch.
# BEWUSST winzig gegen IDLE_RATE: sonst summiert sich der Mindest-Anstieg auf
# einem Server, auf dem fast immer jemand im Call haengt, auf +23 %/Tag - und der
# Kurs laeuft langfristig weg (in der Simulation nach 30 Tagen 50 Millionen).
MIN_UP = float(os.getenv("FLOAKTIE_MIN_UP", "0.00008") or "0.00008")
# Wie weit der Kurs ueber den Zielkurs der aktuellen Aktivitaet hinauslaufen darf.
# Zusammen mit GRUND_FAKTOR legt das die SPANNE fest, in der gehandelt wird -
# also wie streng die Aktie ist. Ausgesucht ueber eine Parametersuche mit je 30
# simulierten Tagen (16 Kombinationen, siehe tools_aktien_sim.py):
#   CEIL 6 (vorher): Boden 681, Spitze 128.456 -> Spanne 182-fach. Der Kurs
#     stand jeden Morgen am Boden und jeden Abend am Anschlag - kein Handel,
#     nur ein Saegezahn.
#   CEIL 2 + GRUND 4 (jetzt): Boden ~3.000, Spitze ~38.000 -> Spanne 12,6-fach.
#     Ein gut getimter 6-Stunden-Handel bringt +826 %, ein schlecht getimter
#     kostet -77 %, ueber Nacht halten -67 %. Blind 7 Tage halten: +4 % - es
#     gibt also nichts geschenkt, nur fuers Dabeisein.
CEIL_FACTOR = float(os.getenv("FLOAKTIE_CEIL", "2") or "2")
# Glaettung asymmetrisch: mehr Aktivitaet wird fast sofort uebernommen (man soll es
# sehen), weniger nur langsam (eine kurze Pause soll den Kurs nicht abwuergen).
ACT_ALPHA_UP = float(os.getenv("FLOAKTIE_ACT_ALPHA_UP", "0.85") or "0.85")
ACT_ALPHA_DOWN = float(os.getenv("FLOAKTIE_ACT_ALPHA_DOWN", "0.15") or "0.15")
MSG_WEIGHT = float(os.getenv("FLOAKTIE_MSG_WEIGHT", "0.5") or "0.5")       # eine Nachricht = ein halber Zuhoerer
MSG_MAX_ACT = float(os.getenv("FLOAKTIE_MSG_MAX_ACT", "12") or "12")       # Chat allein kann nicht beliebig pumpen
STREAM_BONUS = float(os.getenv("FLOAKTIE_STREAM_BONUS", "2.5") or "2.5")   # ein Live-Streamer zaehlt so viel EXTRA
VIDEO_BONUS = float(os.getenv("FLOAKTIE_VIDEO_BONUS", "1.5") or "1.5")     # eine Kamera zaehlt so viel extra
TICK_NOISE = float(os.getenv("FLOAKTIE_TICK_NOISE", "0.0002") or "0.0002") # Rauschen am Deckel
# --- Atmen: der Kurs steht NIE still -----------------------------------------
# An zwei Stellen war der Trend rechnerisch genau 0 - am Deckel und am Grundwert.
# Am Grundwert hiess das echten Stillstand: gemessen 200 von 200 Takten exakt
# 0,000 %/min und ueber 200 Takte ein einziger Kurswert. Eine Aktie, die stundenlang
# auf derselben Zahl steht, sieht kaputt aus.
#
# Statt 0 atmet der Kurs jetzt um sein Niveau: eine kleine Bewegung mit Vorzeichen,
# nie exakt 0, im Mittel 0 - dazu ein sanfter Zug zurueck zum Niveau. Ohne diesen
# Zug waere das Atmen ein freier Zufallslauf, der irgendwann vom Deckel oder unter
# den Boden wegwandert; damit waere die ganze Kalibrierung hinueber.
ATEM_MAX = float(os.getenv("FLOAKTIE_ATEM_MAX", "0.06") or "0.06")     # hoechstens 6 %/min
ATEM_RUECK = float(os.getenv("FLOAKTIE_ATEM_RUECK", "0.15") or "0.15") # Zug zum Niveau je Minute
# --- Lebendiger Kursverlauf --------------------------------------------------
# VOL_SPREAD: so stark schwankt die Minutenbewegung um ihren Trend. 0,8 heisst,
# eine Minute bringt zwischen 20 % und 180 % des rechnerischen Anstiegs. Ohne das
# war der Chart eine gerade Linie - eine Aktie sieht anders aus.
VOL_SPREAD = float(os.getenv("FLOAKTIE_VOL_SPREAD", "0.8") or "0.8")
# Momentum: ein Teil der letzten Bewegung wirkt nach. Damit halten Trends an und
# Wendepunkte sind sichtbar, statt dass jeder Takt bei null anfaengt.
MOM_WEIGHT = float(os.getenv("FLOAKTIE_MOM_WEIGHT", "0.25") or "0.25")
MOM_DECAY = float(os.getenv("FLOAKTIE_MOM_DECAY", "0.7") or "0.7")
# --- Flo als Analyst ---------------------------------------------------------
# Die KI schaut sich regelmaessig den Markt an und verstaerkt oder daempft die
# Bewegung um bis zu KI_MAX. Bewusst gedeckelt: die KI darf Wuerze geben, aber
# niemals die Balance aushebeln. Faellt sie aus, ist der Faktor 0 und alles laeuft
# wie vorher.
KI_MAX = float(os.getenv("FLOAKTIE_KI_MAX", "0.4") or "0.4")          # +/- 40 %
KI_ALLE_SEK = float(os.getenv("FLOAKTIE_KI_INTERVAL", "600") or "600")  # alle 10 min
KI_MAX_ALTER = float(os.getenv("FLOAKTIE_KI_TTL", "2400") or "2400")   # nach 40 min ungueltig
# Sofort-Impulse fuer Ereignisse (unabhaengig vom Niveau, pro Minute gedeckelt).
PULSE_STREAM = float(os.getenv("FLOAKTIE_PULSE_STREAM", "0.004") or "0.004")   # Livestream geht an: +0,4 %
PULSE_JOIN = float(os.getenv("FLOAKTIE_PULSE_JOIN", "0.0015") or "0.0015")     # jemand kommt in den Call: +0,15 %
PULSE_MAX_PER_MIN = float(os.getenv("FLOAKTIE_PULSE_MAX", "0.02") or "0.02")   # zusammen max +2 %/Minute
# Alter Name, nur noch fuer den Startwert der Glaettung (nicht mehr im Drift).
ACT_BASELINE = 0.0

# --- Harte Grenzen (gegen Hyperinflation) ------------------------------------
# Die Kurs-Kurve ist NICHT reservegedeckt: steigt der Basiskurs, ist jeder alte
# Anteil beim Verkauf mehr wert, als beim Kauf hineingeflossen ist - die Differenz
# entsteht aus dem Nichts. Nachgemessen (alte Werte): 750 Anteile fuer 1,1 Mio
# gekauft, 5 h aktiver Call, Kurs 79 Mio -> Verkauf 43,6 MILLIARDEN, also Faktor
# 38.043 aus Luft. Drei Schrauben halten das jetzt in Grenzen, OHNE dem Spiel den
# Spass zu nehmen (ein guter Vormittag darf weiter ein Vielfaches bringen):
#
#   1. Kurs-Deckel: absolut (MAX_PRICE) UND relativ zur Aktivitaet (CEIL_FACTOR 8)
#   2. Depot-Deckel: niemand haelt mehr als MAX_SHARES_PER_USER Anteile
#   3. Verkaufs-Steuer: je groesser die Blase, desto mehr wird beim Verkauf
#      verbrannt (bis SELL_TAX_MAX) - genau dann, wenn am meisten Geld entsteht
#
# Dazu die Tagesbremse (Circuit Breaker): innerhalb EINES Kalendertages darf der
# Basiskurs maximal um Faktor DAY_UP steigen und auf DAY_DOWN fallen.
MAX_PRICE = int(os.getenv("FLOAKTIE_MAX_PRICE", "50000000") or "50000000")
MAX_SHARES_PER_USER = int(os.getenv("FLOAKTIE_MAX_USER_SHARES", "150") or "150")
# Tagesbremse nach OBEN: nur noch eine Gleitkomma-Notbremse, KEINE Spielgrenze.
# Die echte Grenze ist der Aktivitaets-Deckel (CEIL_FACTOR x Zielkurs) - und der
# ist absolut, nicht relativ zum aktuellen Kurs. Ueber Tage aufschaukeln kann sich
# deshalb nichts, auch ohne Tagesbremse. Sie stand auf x50: weil der Kurs nachts
# bis auf den Bodensatz faellt, startete damit JEDER Tag bei 10 und kam nie ueber
# 500 - der Kurs sass in einem Band von 10 bis 500 fest.
DAY_UP = float(os.getenv("FLOAKTIE_DAY_UP", "100000") or "100000")
DAY_DOWN = float(os.getenv("FLOAKTIE_DAY_DOWN", "0.01") or "0.01")  # max -99 % pro Tag
# Verkaufs-Steuer: TRADE_FEE + SELL_TAX_K * ln(1+Ueberhang), gedeckelt.
# Am Deckel (Ueberhang 7) sind das rund 33 % - dort entstehen die meisten Coins.
SELL_TAX_K = float(os.getenv("FLOAKTIE_SELL_TAX_K", "0.15") or "0.15")
SELL_TAX_MAX = float(os.getenv("FLOAKTIE_SELL_TAX_MAX", "0.35") or "0.35")
# KEIN KREDIT. Man kann Anteile nur von eigenem Guthaben kaufen - das Konto geht
# durch die Aktie NIE ins Minus. Frueher ging das (erst unbegrenzt, dann bis zu
# einer Kreditlinie mit Sicherheit), und beides hat sich als schlechte Idee
# erwiesen: ein leeres Konto konnte den Kurs pumpen, Schulden wurden nie
# eingezogen, und ein Konto im Minus ist gegen normale Ausgaben ohnehin immun -
# es blockiert also nur den Spieler, ohne irgendetwas zu bewirken.

# Dividende: Coins pro Voice-Runde je 'DIVIDEND_DIVISOR' Anteile (gedeckelt).
DIVIDEND_DIVISOR = int(os.getenv("FLOAKTIE_DIVIDEND_DIVISOR", "10") or "10")
DIVIDEND_MAX = int(os.getenv("FLOAKTIE_DIVIDEND_MAX", "2000") or "2000")

HISTORY_MAX = 60

_SPARK = "▁▂▃▄▅▆▇█"


class FloAktie(FeatureBasis):
    """Objektorientierte Huelle: Kurs, Depots & Historie leben auf der Instanz."""

    def __init__(self):
        self._enabled = False
        self._store = None
        # Das ZULETZT gepostete 'flo aktie'-Panel (Message) + fuer wen es gepostet
        # wurde. Es wird live nachgezogen, sobald sich Kurs/Boersenwert aendern.
        self._panel_msg = None
        # Zuletzt geschuetztes Panel je Slot - damit das neue das alte
        # beim Auto-Loesch-Schutz wieder abmeldet.
        self._geschuetzt = {}
        self._panel_uid = None
        # Der ZULETZT gepostete Kurs-Chart ('flo aktienkurs') + sein Zeitraum -
        # das Bild wird ebenfalls live nachgezogen.
        self._chart_msg = None
        self._chart_days = 1
        # Handels-Schloesser je Nutzer (gegen Doppelklick-Rennen).
        self._locks = {}
        # Eigene Bot-ID. guild.me ist nicht immer im Cache - ohne diesen Merker
        # koennte Flo sich selbst als Zuhoerer zaehlen und den Kurs oben halten.
        self._self_id = 0
        # Namen der zuletzt gezaehlten Menschen (nur Anzeige/Diagnose).
        self._zuletzt_gezaehlt = []
        # Die Rohwerte des letzten Takts (Leute, Streams, Kameras, Nachrichten) -
        # damit das Panel zeigen kann, WORAUS die Aktivitaet besteht.
        self._zuletzt_mess = (0, 0, 0, 0)

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if os.getenv("FLOAKTIE_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("FloCorp-Aktie aus (FLOAKTIE_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("FloCorp-Aktie aus: economy ist nicht aktiv.")
            return False
        self._store = JsonStore("floaktie.json", default={
            "price": START_PRICE, "base": float(START_PRICE), "day": "",
            "act_ema": ACT_BASELINE, "msg_count": 0, "last_msg_count": 0,
            "holdings": {}, "history": [], "ticks": []})
        st = self._state()
        if not st.get("price"):
            st["price"] = START_PRICE
        # Basiskurs aus altem Stand ableiten (Migration) und Kurs synchronisieren.
        self._base()
        self._tages_anker()
        self._sync_price()
        if not st.get("history"):
            st["history"] = [{"day": self._today(), "price": int(st["price"])}]
        self._enabled = True
        log.info("FloCorp-Aktie (%s) aktiv: Kurs %s Coins, %d Aktionaere.",
                 TICKER, self._fmt(st["price"]), len(st.get("holdings", {})))
        return True

    def is_enabled(self):
        return self._enabled

    # --- Notaus ------------------------------------------------------------
    # Der Schalter im Web-Panel ("Steuerung -> FloCorp-Aktie") legt die Boerse
    # KOMPLETT still: kein Kauf, kein Verkauf, kein Kurs-Tick, keine Dividende,
    # keine Aktivitaets-Zaehlung. Jede oeffentliche Tuer fragt hier nach - sonst
    # laufen alte Panel-Buttons und die Loops munter weiter.
    def is_off(self):
        """True, wenn die Aktie per Panel-Schalter abgeschaltet ist."""
        try:
            import features
            return not features.is_on("floaktie")
        except Exception:  # noqa: BLE001 - ohne features gilt: an
            return False

    def aus_text(self):
        return (f"🔒 **Die Aktie ist derzeit deaktiviert.**\n"
                f"{self._bot_name or 'Flo'} hat die Börse geschlossen – kein Kauf, "
                f"kein Verkauf, kein Kurs. Deine Anteile bleiben dir erhalten, "
                f"sie liegen nur eingefroren im Depot.")

    def aus_embed(self):
        """Sauberes Embed statt Fließtext - das sieht der Nutzer, wenn die Aktie aus ist."""
        e = discord.Embed(
            title="🔒 Börse geschlossen",
            description=("Die **FloCorp-Aktie** ist derzeit deaktiviert.\n"
                         "Kein Handel, kein Kursverlauf, keine Voice-Dividende."),
            colour=discord.Colour.from_rgb(90, 96, 106))
        e.add_field(name="Deine Anteile", value="bleiben erhalten (eingefroren)", inline=True)
        e.add_field(name="Wann geht's weiter?", value="wenn der Chef sie wieder anschaltet",
                    inline=True)
        e.set_footer(text=f"{TICKER} · vom Server-Chef abgeschaltet")
        return e

    async def handle_aus(self, message):
        """Ersatz-Handler, solange die Aktie AUS ist: antwortet auf die Aktien-
        Befehle sauber statt sie an die KI durchfallen zu lassen."""
        if not self._enabled or message.guild is None:
            return None
        try:
            import ai
            cmd = ai.strip_lead(message.content or "")
        except Exception:  # noqa: BLE001
            cmd = message.content or ""
        parts = cmd.split()
        first = parts[0].lower().strip(".,;:!?") if parts else ""
        if first not in _CMDS and first not in _CHART_CMDS:
            return None
        return self.aus_embed()

    # --- Kleine Helfer ----------------------------------------------------
    def _fmt(self, n):
        return f"{int(n):,}".replace(",", ".")

    def _state(self):
        assert self._store is not None
        return self._store.data

    async def _save(self):
        if self._store is not None:
            await self._store.save()

    def _today(self):
        return datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    def price(self):
        """Der aktuelle Anzeigekurs - IMMER frisch aus der Kurve gerechnet.

        Frueher stand hier das gespeicherte Feld st['price']. Das stimmt aber nur
        so lange, wie JEDER Schreibweg danach _sync_price() aufruft. Ein einziger
        vergessener Aufruf (oder ein von Hand gesetzter Stand) liefert sonst einen
        veralteten Kurs - und der landet ueber _record_tick auch noch im Chart und
        in der Tages-Historie, wo er nicht mehr auffaellt. Rechnen kostet eine
        Multiplikation; das ist die Verlaesslichkeit allemal wert. st['price']
        bleibt als gespeicherter Stand erhalten (Migration alter Staende)."""
        return max(MIN_PRICE, min(MAX_PRICE,
                                  int(round(self._price_at(self.total_shares())))))

    def _record_tick(self, now=None):
        """Schreibt einen Kurs-Zeitpunkt (fuer den Chart) - bei jedem Trade, Sample
        und Tages-Tick. Zeitstempel als Epoch, Liste gedeckelt."""
        st = self._state()
        ticks = st.setdefault("ticks", [])
        if not isinstance(ticks, list):
            ticks = st["ticks"] = []
        t = int(now if now is not None else time.time())
        ticks.append({"t": t, "price": self.price()})
        if len(ticks) > HISTORY_TICKS_MAX:
            del ticks[:len(ticks) - HISTORY_TICKS_MAX]

    def _holdings(self):
        hold = self._state().setdefault("holdings", {})
        # setdefault rettet nur einen FEHLENDEN Schluessel - ein vorhandenes
        # "holdings": null kommt daran vorbei.
        if not isinstance(hold, dict):
            hold = self._state()["holdings"] = {}
        return hold

    @staticmethod
    def _n(wert):
        """Anteilszahl aus dem Store. Murks zaehlt als 0, statt den Kurs zu
        sprengen: price() haengt an total_shares, und daran haengen wiederum
        jeder Handel, das Panel, das Profil und die Vermoegensrechnung."""
        try:
            return int(wert)
        except (TypeError, ValueError):
            return 0

    def _verlauf(self, feld):
        """'history' bzw. 'ticks' als Liste echter Dicts.

        Ein einziger Eintrag, der kein dict ist (oder das Feld selbst als null),
        riss sonst Sparkline, Chart, Kurs-Prozente und das Panel mit."""
        roh = self._state().get(feld)
        if not isinstance(roh, list):
            return []
        return [e for e in roh if isinstance(e, dict)]

    def shares_of(self, uid):
        return self._n(self._holdings().get(str(uid), 0))

    def total_shares(self):
        return sum(self._n(v) for v in self._holdings().values())

    def holders_count(self):
        return sum(1 for v in self._holdings().values() if self._n(v) > 0)

    def top_holder(self):
        """UID (int) des groessten Aktionaers oder None."""
        hold = self._holdings()
        best, best_n = None, 0
        for uid, n in hold.items():
            anteile = self._n(n)
            if anteile > best_n:
                best, best_n = self._n(uid), anteile
        return best

    def value_of(self, uid):
        return self.shares_of(uid) * self.price()

    # --- Markt-Mathematik (pfad-unabhaengige Kurve) -----------------------
    # WICHTIG (Exploit-Fix): Der Kurs ist eine FUNKTION der ausgegebenen Anteile:
    #
    #     Kurs(S) = Basiskurs * (1 + S / LIQUIDITY)
    #
    # Kaufen/Verkaufen bezahlt das INTEGRAL unter dieser Kurve. Dadurch kostet
    # "750x 1 Anteil" exakt dasselbe wie "1x 750 Anteile" (pfad-unabhaengig) -
    # frueher konnte man mit vielen Klein-Kaeufen den Kurs multiplikativ hoch-
    # pumpen und dann mit gedeckeltem Impact teuer dumpen (Geld aus dem Nichts).
    # Zusaetzlich liegt auf jeder Order eine Gebuehr, damit ein sofortiger
    # Hin-und-Her-Trade IMMER verliert. Die Aktivitaet (Call/Chat/Streams)
    # bewegt den BASISKURS - der Kurs reagiert also weiter wie gewohnt.
    def _base(self):
        """Basiskurs (Kurs bei 0 ausgegebenen Anteilen). Migriert alte Staende.

        Untergrenze ist absichtlich winzig (nicht MIN_PRICE): der ANGEZEIGTE Kurs
        wird in _sync_price auf MIN_PRICE gedeckelt. Mit MIN_PRICE als Basis-
        Untergrenze liess sich ein niedriger Kurs bei vielen ausgegebenen Anteilen
        gar nicht setzen - 'Kurs 100' wurde bei 1 Mio Anteilen wieder zu einer
        riesigen Zahl hochgerechnet."""
        st = self._state()
        base = st.get("base")
        if not base:
            # Alter Stand: aus dem gespeicherten Kurs + Anteilen zurueckrechnen.
            alt_kurs = float(st.get("price", START_PRICE) or START_PRICE)
            base = alt_kurs / (1.0 + self.total_shares() / LIQUIDITY)
            st["base"] = base
        try:
            base = float(base)
        except (TypeError, ValueError):
            base = float(START_PRICE)
        if base != base or base in (float("inf"), float("-inf")):   # NaN/inf
            base = float(START_PRICE)
        return self._clamp_base(base)

    def _clamp_base(self, base):
        """Zwingt den Basiskurs in die erlaubte Spanne - EINE Stelle fuer alle
        Deckel, weil jeder Lesezugriff durch _base() laeuft:

        - unten: _base_floor() (angezeigter Kurs nie unter MIN_PRICE)
        - oben : _base_ceiling() (angezeigter Kurs nie ueber MAX_PRICE)
        - dazu die Tagesbremse (Circuit Breaker), falls fuer heute verankert
        """
        lo, hi = self._base_floor(), self._base_ceiling()
        tag_lo, tag_hi = self._tages_band()
        if tag_hi is not None:
            hi = min(hi, tag_hi)
        if tag_lo is not None:
            lo = max(lo, tag_lo)
        if lo > hi:            # Boden schlaegt Deckel (viele Anteile im Umlauf)
            lo = hi
        return max(lo, min(float(base), hi))

    def _base_ceiling(self):
        """Obergrenze des Basiskurses: so hoch, dass der ANGEZEIGTE Kurs gerade
        noch MAX_PRICE ergibt. Reine Notbremse gegen Gleitkomma-Regionen."""
        return max(BASE_FLOOR, float(MAX_PRICE) / (1.0 + self.total_shares() / LIQUIDITY))

    def _tages_band(self):
        """(untere, obere) Grenze des Basiskurses fuer HEUTE - der Circuit Breaker.

        Verankert wird der Basiskurs beim ersten Takt des Tages (siehe
        _tages_anker). Liest NUR den gespeicherten Wert - nie _base(), sonst
        Endlos-Rekursion."""
        st = self._store.data if self._store is not None else {}
        if st.get("open_day") != self._today():
            return None, None
        try:
            anker = float(st.get("open_base") or 0.0)
        except (TypeError, ValueError):
            return None, None
        if anker <= 0 or anker != anker:
            return None, None
        return anker * max(0.0, min(1.0, DAY_DOWN)), anker * max(1.0, DAY_UP)

    def _tages_anker(self):
        """Merkt sich zu Tagesbeginn den Basiskurs (Grundlage der Tagesbremse)."""
        st = self._state()
        heute = self._today()
        if st.get("open_day") == heute and st.get("open_base"):
            return
        # Erst den alten Anker loeschen: sonst rechnet _base() unten noch mit dem
        # Band von GESTERN und der neue Anker klebt am alten Tagesdeckel.
        st["open_base"] = 0
        st["open_day"] = heute
        st["open_base"] = float(self._base())

    def _base_floor(self):
        """Untergrenze des Basiskurses: genau so tief, dass der ANGEZEIGTE Kurs
        gerade noch MIN_PRICE ergibt.

        Ohne diese Kopplung lief der Basiskurs unter der MIN_PRICE-Klemme weiter
        nach unten: der Kurs zeigte stur 50, waehrend der echte Wert bis zum
        Faktor 5000 darunter lag - und danach brauchte es TAGE Dauer-Aktivitaet,
        bis die angezeigte Zahl sich ueberhaupt bewegte."""
        return max(BASE_FLOOR, float(MIN_PRICE) / (1.0 + self.total_shares() / LIQUIDITY))

    def _price_at(self, shares_out):
        """Kurs bei 'shares_out' ausgegebenen Anteilen (exakt, ungerundet)."""
        return self._base() * (1.0 + max(0, shares_out) / LIQUIDITY)

    def _integral(self, s_from, s_to):
        """Flaeche unter der Kurve zwischen zwei Anteils-Staenden (= Geldbetrag).
        Telescopiert exakt -> viele kleine Orders kosten wie eine grosse."""
        b = self._base()
        lo, hi = float(min(s_from, s_to)), float(max(s_from, s_to))
        return b * ((hi - lo) + (hi * hi - lo * lo) / (2.0 * LIQUIDITY))

    def _sync_price(self):
        """Haelt den angezeigten Kurs (st['price']) mit der Kurve synchron."""
        st = self._state()
        st["price"] = max(MIN_PRICE, min(MAX_PRICE,
                          int(round(self._price_at(self.total_shares())))))
        return st["price"]

    def _buy_cost(self, shares):
        """Kosten fuer 'shares' Anteile (Integral + Gebuehr) und der Kurs danach."""
        s = self.total_shares()
        brutto = self._integral(s, s + max(0, shares))
        cost = int(brutto * (1.0 + TRADE_FEE)) + 1 if brutto > 0 else 0
        neu = max(MIN_PRICE, int(round(self._price_at(s + max(0, shares)))))
        return cost, neu

    def _sell_fee(self):
        """Verkaufs-Gebuehr, GESTAFFELT nach Blase.

        Steht der Kurs nur bei seinem 'fairen' Wert, ist es die normale Gebuehr.
        Je weiter er darueber steht, desto mehr wird beim Verkauf verbrannt (bis
        SELL_TAX_MAX). Das ist der wichtigste Geld-Abfluss der Wirtschaft: genau
        beim Verkauf in eine Blase entstehen die meisten Coins aus dem Nichts."""
        st = self._state()
        # Der Grundwert gehoert MIT in die Bezugsgroesse - genau wie in
        # deckel_fuer(). Ohne ihn misst die Steuer den "Blasen-Ueberhang"
        # nachts gegen ziel_base(0), also gegen den nackten Mindestwert: am
        # voellig legitimen Boden wurden dadurch 35 % statt 2 % faellig, nur
        # weil gerade niemand im Call war. Derselbe Kurs, 33 Prozentpunkte
        # Unterschied - je nach Uhrzeit.
        ziel = max(self.ziel_base(float(st.get("act_ema", 0.0) or 0.0)),
                   self.boden_base())
        ueberhang = max(0.0, self._base() / max(1.0, ziel) - 1.0)
        return min(SELL_TAX_MAX, TRADE_FEE + SELL_TAX_K * math.log1p(ueberhang))

    def _sell_proceeds(self, shares):
        """Erloes fuer 'shares' Anteile (Integral - Steuer) und der Kurs danach."""
        s = self.total_shares()
        shares = max(0, min(int(shares), s))
        brutto = self._integral(s - shares, s)
        proceeds = max(0, int(brutto * (1.0 - self._sell_fee())))
        neu = max(MIN_PRICE, int(round(self._price_at(s - shares))))
        return proceeds, neu

    def _freies_depot(self, uid):
        """Wie viele Anteile dieser Nutzer noch dazukaufen DARF (Depot-Deckel)."""
        return max(0, MAX_SHARES_PER_USER - self.shares_of(uid))

    def _kaufrahmen(self, stand):
        """Wie viele Coins dieser Kontostand fuer einen Kauf hergibt: GENAU das
        Guthaben. Kein Kredit, kein Hebel - das Konto geht durch die Aktie nie
        ins Minus."""
        return max(0, int(stand))

    def _max_affordable(self, coins, deckel=None):
        """Wie viele Anteile man sich mit 'coins' leisten kann (inkl. Gebuehr).
        Binaersuche auf der Kurve - nie mehr, als das Guthaben wirklich deckt.
        'deckel' begrenzt zusaetzlich (freier Platz im Depot)."""
        coins = int(coins)
        if coins <= 0:
            return 0
        grenze = MAX_SHARES_PER_TRADE if deckel is None else max(0, min(MAX_SHARES_PER_TRADE, int(deckel)))
        if grenze <= 0:
            return 0
        lo, hi = 0, grenze
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self._buy_cost(mid)[0] <= coins:
                lo = mid
            else:
                hi = mid - 1
        return lo

    # --- Handel -----------------------------------------------------------
    def _resolve_count(self, member, token, *, selling=False):
        token = (token or "").strip().lower()
        if selling:
            if token in ("alles", "all", "max", "maximum"):
                return self.shares_of(member.id)
        else:
            if token in ("alles", "all", "max", "maximum"):
                return max(0, self._max_affordable(economy.get_coins(member.id),
                                                   self._freies_depot(member.id)))
        try:
            n = int(token)
        except (TypeError, ValueError):
            return 1
        return max(1, min(n, MAX_SHARES_PER_TRADE))

    def _lock(self, uid):
        """Ein Schloss je Nutzer. Zwei schnelle Klicks auf 'Kauf MAX' liefen sonst
        gleichzeitig durch dieselbe Kurs-Rechnung: beide sahen denselben (billigen)
        Kurs, beide bekamen die Anteile - Geld aus dem Nichts."""
        uid = int(uid)
        lock = self._locks.get(uid)
        if lock is None:
            lock = asyncio.Lock()
            # Der Bot laeuft wochenlang: alte, freie Schloesser wegraeumen.
            if len(self._locks) > 500:
                for k in [k for k, v in list(self._locks.items()) if not v.locked()][:250]:
                    self._locks.pop(k, None)
            self._locks[uid] = lock
        return lock

    async def buy(self, member, count):
        async with self._lock(member.id):
            return await self._buy(member, count)

    async def sell(self, member, count):
        async with self._lock(member.id):
            return await self._sell(member, count)

    async def _buy(self, member, count):
        if self.is_off():
            return self.aus_text()
        count = int(count)
        if count < 1:
            return "Kauf mindestens **1** Anteil. 📈"
        if count > MAX_SHARES_PER_TRADE:
            count = MAX_SHARES_PER_TRADE
        # Depot-Deckel: begrenzt, wie viel Geld ein einzelner Halter beim Verkauf
        # in eine Blase ueberhaupt schoepfen kann.
        frei = self._freies_depot(member.id)
        if frei <= 0:
            return (f"🚫 Dein Depot ist voll: **{MAX_SHARES_PER_USER}** Anteile "
                    f"{TICKER} sind das Maximum pro Person.\n"
                    f"Verkauf erst welche, dann geht wieder was.")
        if count > frei:
            count = frei
        # Kosten IMMER vor der Depot-Aenderung berechnen (Kurve haengt an den
        # ausgegebenen Anteilen).
        alt_kurs = self.price()
        cost, _ = self._buy_cost(count)
        # NUR von eigenem Guthaben - kein Kredit, kein Minus. Passt der Wunsch
        # nicht, wird auf das heruntergerechnet, was das Konto wirklich hergibt.
        stand_vor = economy.get_coins(member.id)
        rahmen = self._kaufrahmen(stand_vor)
        if cost > rahmen:
            moeglich = self._max_affordable(rahmen, frei)
            if moeglich < 1:
                ein_stueck = self._buy_cost(1)[0]
                return (f"💸 **Dafür reicht dein Guthaben nicht.**\n"
                        f"Du hast **{self._fmt(max(0, stand_vor))}** {economy.COIN}, "
                        f"ein einzelner Anteil kostet gerade "
                        f"**{self._fmt(ein_stueck)}**.\n"
                        f"_Auf Pump gibt es hier nichts – erst verdienen._")
            count = moeglich
            cost, _ = self._buy_cost(count)
        # Gegenpruefen, dass wirklich abgebucht wurde: add_coins klemmt bei 0, und
        # ohne diese Kontrolle gaebe es die Anteile geschenkt.
        economy.add_coins(member.id, -cost, reason="floaktie")
        if stand_vor - economy.get_coins(member.id) != cost:
            economy.add_coins(member.id, stand_vor - economy.get_coins(member.id),
                              reason="floaktie-rueck")
            return ("💸 Die Abbuchung ist nicht durchgegangen – dein Guthaben hat sich "
                    "gerade geändert. Versuch's nochmal.")
        self._holdings()[str(member.id)] = self.shares_of(member.id) + count
        neu = self._sync_price()       # Kauf hebt den Kurs (Kurve neu auswerten)
        self._record_tick()
        await self._save_all()
        await self._refresh_live()
        stand = economy.get_coins(member.id)
        return self._trade_embed("kauf", member, count, cost, alt_kurs, neu, stand)

    async def _sell(self, member, count):
        if self.is_off():
            return self.aus_text()
        count = int(count)
        habe = self.shares_of(member.id)
        if habe <= 0:
            return f"Du besitzt keine {TICKER}-Anteile zum Verkaufen."
        if count < 1:
            return "Verkauf mindestens **1** Anteil. 📉"
        count = min(count, habe)
        alt_kurs = self.price()
        steuer = self._sell_fee()
        proceeds, _ = self._sell_proceeds(count)
        economy.add_coins(member.id, proceeds, reason="floaktie")
        rest = habe - count
        if rest > 0:
            self._holdings()[str(member.id)] = rest
        else:
            self._holdings().pop(str(member.id), None)
        neu = self._sync_price()       # Verkauf drueckt den Kurs (Kurve neu)
        self._record_tick()
        await self._save_all()
        await self._refresh_live()
        return self._trade_embed("verkauf", member, count, proceeds, alt_kurs, neu,
                                 economy.get_coins(member.id), steuer=steuer)

    def _trade_embed(self, art, member, count, betrag, alt_kurs, neu_kurs, stand,
                     steuer=None):
        """Kauf-/Verkaufs-Bestaetigung als Embed (vorher zwei Zeilen Fliesstext).
        Zeigt alles, was man nach einem Trade wissen will: Stueckzahl, Summe,
        Kursbewegung, Depot-Auslastung, neuer Kontostand."""
        kauf = art == "kauf"
        pfeil = "📈" if kauf else "📉"
        farbe = (discord.Colour.from_rgb(87, 242, 135) if kauf
                 else discord.Colour.from_rgb(235, 69, 158))
        meine = self.shares_of(member.id)
        e = discord.Embed(
            title=f"{pfeil} {'Gekauft' if kauf else 'Verkauft'} · {TICKER}",
            description=(f"**{count}** {'Anteil' if count == 1 else 'Anteile'}\n"
                         f"## {'−' if kauf else '+'}{self._fmt(betrag)} {economy.COIN}"),
            colour=farbe)
        richtung = "▲" if neu_kurs > alt_kurs else ("▼" if neu_kurs < alt_kurs else "▬")
        e.add_field(name="Kurs",
                    value=(f"{self._fmt(alt_kurs)} {richtung} **{self._fmt(neu_kurs)}**"),
                    inline=True)
        e.add_field(name="Dein Depot",
                    value=f"**{meine}**/{MAX_SHARES_PER_USER}\n{self._depot_balken(meine)}",
                    inline=True)
        e.add_field(name="Kontostand", value=f"**{self._fmt(stand)}**", inline=True)
        if steuer:
            e.add_field(name="Verkaufssteuer",
                        value=(f"{steuer * 100:.0f} % einbehalten\n"
                               f"_steigt, je höher die Blase_"),
                        inline=False)

        if kauf and meine >= MAX_SHARES_PER_USER:
            e.set_footer(text=f"Depot voll ({MAX_SHARES_PER_USER} Anteile) · "
                              f"{self._bot_name} aktie verkauf alles")
        else:
            e.set_footer(text=f"{self._bot_name} aktie · aktienkurs · depot · top")
        return e

    async def reset_kurs(self, *, depots_leeren=False):
        """Setzt die Aktie auf den Startkurs zurueck (Verlauf weg, Tagesbremse neu).

        depots_leeren=True loescht zusaetzlich alle Anteile. Ohne das behalten die
        Aktionaere ihre Anteile - die sind danach eben billig.
        Rueckgabe: (alter Kurs, neuer Kurs, geloeschte Depots)."""
        st = self._state()
        alt_kurs = self.price()
        depots = len(self._holdings()) if depots_leeren else 0
        if depots_leeren:
            st["holdings"] = {}
        st["base"] = float(START_PRICE)
        st["act_ema"] = 0.0
        st["leer_min"] = 0.0
        st["pulse_min"] = 0
        st["pulse_sum"] = 0.0
        st["history"] = []
        st["ticks"] = []
        st["open_day"] = ""
        st["open_base"] = 0
        self._tages_anker()
        neu = self._sync_price()
        self._record_tick()
        await self._save_all()
        await self._refresh_live()
        log.warning("FloCorp zurueckgesetzt: Kurs %s -> %s (Depots geleert: %s)",
                    self._fmt(alt_kurs), self._fmt(neu), depots_leeren)
        return alt_kurs, neu, depots

    async def _save_all(self):
        try:
            await self._save()
            await economy.flush()
        except Exception:  # noqa: BLE001
            log.exception("Speichern nach FloCorp-Trade fehlgeschlagen")

    # --- Admin-API (Web-Panel): Anteile & Kurs korrigieren ----------------
    # Obergrenzen fuer die Admin-Eingriffe: absurde Werte (10**30 Anteile) haben
    # die Kurs-Kurve vorher in Gleitkomma-Muell bzw. einen OverflowError gerissen -
    # danach bekamen unbeteiligte Halter beim Verkauf 0 Coins.
    ADMIN_MAX_SHARES = 10 ** 9
    ADMIN_MAX_PRICE = 10 ** 12

    async def admin_set_price(self, preis):
        """Setzt den ANGEZEIGTEN Kurs (verankert den Basiskurs passend neu)."""
        preis = max(MIN_PRICE, min(int(preis), self.ADMIN_MAX_PRICE))
        st = self._state()
        st["base"] = self._clamp_base(preis / (1.0 + self.total_shares() / LIQUIDITY))
        neu = self._sync_price()
        self._record_tick()
        await self._save()
        await self._refresh_live()
        return neu

    async def admin_shares(self, uid, action, amount, keep_price=True):
        """Korrigiert die Anteile eines Nutzers (Panel: give/take/set) - z. B. um
        per Exploit erschlichene Anteile zu entfernen.

        keep_price=True haelt den ANGEZEIGTEN Kurs stabil: da der Kurs an den
        ausgegebenen Anteilen haengt, wuerde das Streichen von 100.000 Anteilen den
        Kurs sonst abstuerzen lassen. Der Basiskurs wird deshalb neu verankert.
        Rueckgabe: (neue Anteile des Nutzers, Kurs, Anteile insgesamt)."""
        uid = str(int(uid))
        # ZUERST deckeln, DANN anfassen: vorher wurde das Depot geaendert und die
        # Kurs-Rechnung flog danach mit OverflowError raus - Depot verbogen, nichts
        # gespeichert, jeder weitere Kauf/Verkauf kaputt.
        amount = max(0, min(int(amount), self.ADMIN_MAX_SHARES))
        hold = self._holdings()
        vorher_kurs = self.price()
        habe = int(hold.get(uid, 0))
        if action == "give":
            neu_n = habe + amount
        elif action == "take":
            neu_n = max(0, habe - amount)
        else:                              # "set"
            neu_n = amount
        neu_n = max(0, min(neu_n, self.ADMIN_MAX_SHARES))
        if neu_n > 0:
            hold[uid] = neu_n
        else:
            hold.pop(uid, None)
        if keep_price:
            # Kurs festhalten -> Basiskurs an die neue Anteilsmenge anpassen.
            # DURCH _clamp_base schicken: bei sehr grossen Anteilsmengen ergibt
            # die Rechnung einen winzigen Basiskurs (bei 1 Mrd Anteilen 0,0009),
            # den jeder LESEZUGRIFF anschliessend auf den Boden hochklemmt.
            # Der "festgehaltene" Kurs sprang dadurch um das 11.111-fache nach
            # oben, und ein unbeteiligter Halter konnte seine 150 Anteile fuer
            # Milliarden aus dem Nichts verkaufen. Wenn sich der Kurs nicht
            # halten laesst, ist es ehrlicher, das zu sagen als zu tun als ob.
            st = self._state()
            gewuenscht = vorher_kurs / (1.0 + self.total_shares() / LIQUIDITY)
            st["base"] = self._clamp_base(gewuenscht)
            if abs(st["base"] - gewuenscht) > 1e-9:
                log.warning("Panel: Kurs liess sich bei %d Anteilen NICHT halten "
                            "(Basiskurs %.6f -> %.6f geklemmt).",
                            self.total_shares(), gewuenscht, st["base"])
        kurs = self._sync_price()
        self._record_tick()
        await self._save()
        await self._refresh_live()
        log.info("Panel: Anteile von %s auf %d gesetzt (%s %d) - Kurs %s.",
                 uid, neu_n, action, amount, kurs)
        return neu_n, kurs, self.total_shares()

    # --- Aktivitaets-Modell (Kurs folgt der Server-Aktivitaet) -----------
    def note_message(self):
        """Zaehlt eine ECHTE Chat-Nachricht als Aktivitaet (treibt den Kurs hoch).
        bot.py ruft das fuer jede Nachricht der Haupt-Guild auf, die KEIN Befehl an
        Flo ist - nur ein billiger Zaehler, gespeichert wird beim naechsten Takt.""" 
        if not self._enabled or self.is_off():
            return
        st = self._state()
        st["msg_count"] = int(st.get("msg_count", 0)) + 1

    def _measure(self, guild):
        """Misst die Voice-Aktivitaet: (Leute, Live-Streamer, Kameras).

        BOTS ZAEHLEN NIE - weder Flo selbst noch fremde. Genau da lag ein Fehler:
        gezaehlt wurde ueber channel.voice_states, und ob ein Eintrag ein Bot ist,
        stand nur im Member-Cache. Lieferte guild.get_member(uid) nichts (nach
        einem Neustart, oder weil der Cache den Bot nicht kennt), wurde er als
        MENSCH mitgezaehlt - der Musik-Bot allein hielt so die Aktivitaet dauerhaft
        ueber 0, und der Kurs konnte nie fallen.

        Deshalb jetzt: ZUERST channel.members (echte Member-Objekte, '.bot' ist dort
        verlaesslich). Nur wenn diese Liste leer ist, obwohl voice_states etwas
        zeigen (Cache wirklich nicht da), wird ueber voice_states gezaehlt - und
        dabei jeder nicht aufloesbare Eintrag SICHERHEITSHALBER als Bot behandelt
        statt als Mensch."""
        people = streams = video = 0
        if guild is None:
            return 0, 0, 0
        afk_id = getattr(getattr(guild, "afk_channel", None), "id", None)
        eigene_id = getattr(getattr(guild, "me", None), "id", 0) or self._self_id
        if eigene_id and not self._self_id:
            self._self_id = eigene_id
        gezaehlt = []
        for vc in (getattr(guild, "voice_channels", None) or []):
            if afk_id is not None and getattr(vc, "id", None) == afk_id:
                continue
            members = [m for m in (getattr(vc, "members", None) or [])
                       if getattr(m, "id", None) != eigene_id]
            if members:
                for m in members:
                    if getattr(m, "bot", False):
                        continue                      # Bots zaehlen nicht
                    people += 1
                    gezaehlt.append(getattr(m, "display_name", None)
                                    or getattr(m, "name", None) or str(getattr(m, "id", "?")))
                    vs = getattr(m, "voice", None)
                    if vs is not None:
                        if getattr(vs, "self_stream", False):
                            streams += 1
                        if getattr(vs, "self_video", False):
                            video += 1
                continue
            # Notfall: keine Member-Objekte da, aber voice_states schon.
            for uid, vs in list((getattr(vc, "voice_states", None) or {}).items()):
                if uid == eigene_id:
                    continue
                m = None
                try:
                    m = guild.get_member(uid)
                except Exception:  # noqa: BLE001
                    m = None
                if m is None or getattr(m, "bot", False):
                    # Unbekannt -> NICHT mitzaehlen. Lieber eine Minute zu wenig
                    # Aktivitaet als ein Bot, der den Kurs dauerhaft oben haelt.
                    continue
                people += 1
                gezaehlt.append(getattr(m, "display_name", None)
                                or getattr(m, "name", None) or str(uid))
                if getattr(vs, "self_stream", False):
                    streams += 1
                if getattr(vs, "self_video", False):
                    video += 1
        # Wer genau gezaehlt wurde, merken - das Panel zeigt es an, damit man
        # SIEHT, woher die Aktivitaet kommt (und dass eben KEIN Bot dabei ist).
        self._zuletzt_gezaehlt = gezaehlt
        return people, streams, video

    @staticmethod
    def _als_liste(guilds):
        """Ein Server oder mehrere - beides ist erlaubt.

        Es gibt genau EINE $FLO-Aktie fuer alle Server. Welche Server ihren Kurs
        bewegen duerfen, entscheidet bot.py (guildcfg 'aktie_zaehlt') und reicht
        die Liste hier herein."""
        if guilds is None:
            return []
        if isinstance(guilds, (list, tuple, set, frozenset)):
            return [g for g in guilds if g is not None]
        return [guilds]

    def _measure_alle(self, guilds):
        """Summiert die Voice-Aktivitaet ueber alle mitzaehlenden Server."""
        liste = self._als_liste(guilds)
        if len(liste) == 1:
            return self._measure(liste[0])
        people = streams = video = 0
        namen = []
        for g in liste:
            p, s, v = self._measure(g)
            people += p
            streams += s
            video += v
            namen.extend(self._zuletzt_gezaehlt)
        # _measure hat die Namen je Server ueberschrieben - hier die Summe.
        self._zuletzt_gezaehlt = namen
        return people, streams, video

    def activity_of(self, people, streams=0, video=0, msgs=0):
        """Aktivitaetspunkte: jeder im Call zaehlt 1, jeder Livestream EXTRA,
        jede Kamera extra, Nachrichten anteilig (gedeckelt gegen Spam-Pumpen).

        WICHTIG - der Call ist die Bedingung: ist NIEMAND im Sprachkanal, sind es
        0 Punkte, egal wie viel geschrieben wird. Vorher konnte eine einzige
        Nachricht die Aktivitaet ueber 0 heben und damit den Verfall komplett
        aussetzen; auf einem Server, auf dem tagsueber immer mal jemand etwas
        tippt, ist der Kurs deshalb praktisch nie gefallen - genau das Verhalten,
        das als "die Aktie sinkt nie" aufgefallen ist. Der Chat zaehlt weiter
        voll mit, aber als VERSTAERKER eines laufenden Calls, nicht als Ersatz."""
        leute = float(max(0, people))
        if leute <= 0:
            return 0.0
        chat = min(MSG_MAX_ACT, MSG_WEIGHT * float(max(0, msgs)))
        return (leute
                + STREAM_BONUS * float(max(0, streams))
                + VIDEO_BONUS * float(max(0, video))
                + chat)

    def ziel_base(self, activity):
        """Basiskurs, den DIESE Aktivitaet hergibt - dorthin strebt der Kurs.
        Jeder im Call zaehlt 1, jeder Livestream extra, Chat zaehlt mit (aber
        nur bei besetztem Call - siehe activity_of)."""
        return max(BASE_FLOOR, FAIR_BASE + AKT_WERT * max(0.0, float(activity)))

    def grund_akt(self):
        """Die durchschnittliche Aktivitaet der letzten GRUND_TAGE Tage."""
        return max(0.0, float(self._state().get("grund_akt", 0.0) or 0.0))

    def boden_base(self):
        """Der Boden, auf den der Kurs im Leerlauf faellt - der Grundwert.

        NICHT der feste Mindestwert: die Aktie ist so viel wert, wie der Server
        im Schnitt hergibt. Ein Server mit taeglichem Abend-Call haelt damit
        einen ordentlichen Boden, ein toter Server verfaellt bis auf FAIR_BASE."""
        return self.ziel_base(GRUND_FAKTOR * self.grund_akt())

    def _grund_tick(self, roh_aktiv, skala):
        """Zieht den Grundwert langsam Richtung der tatsaechlichen Aktivitaet.

        Bewusst TRAEGE (GRUND_TAGE Tage): ein einzelner voller Abend soll den
        Boden nur ein Stueck heben, und eine einzelne stille Nacht ihn nur ein
        Stueck senken. Sonst waere der Boden bloss eine zweite, langsamere Kopie
        des Kurses und wuerde nichts mehr stuetzen."""
        st = self._state()
        alt = max(0.0, float(st.get("grund_akt", 0.0) or 0.0))
        tau = max(1.0, GRUND_TAGE * 1440.0)          # in Minuten
        alpha = min(1.0, skala / tau)
        st["grund_akt"] = alt + (max(0.0, float(roh_aktiv)) - alt) * alpha

    def ziel_kurs(self, activity=None):
        """Derselbe Zielwert als ANGEZEIGTER Kurs (inkl. ausgegebener Anteile)."""
        if activity is None:
            activity = float(self._state().get("act_ema", 0.0) or 0.0)
        ziel = self.ziel_base(activity) * (1.0 + self.total_shares() / LIQUIDITY)
        return max(MIN_PRICE, int(round(ziel)))

    def drift_fuer(self, activity):
        """Kurs-Drift pro Minute.

        Ist irgendjemand da (Aktivitaet > 0), STEIGT der Kurs - langsam bei einem
        Zuhoerer, rasant bei einem vollen Call mit Streams. Nur wenn gar nichts
        los ist, sinkt er langsam. Steht er schon weit ueber dem, was die
        Aktivitaet hergibt, steigt er weiter - aber immer gemaechlicher."""
        base = self._base()
        if base <= 0:
            return 0.0
        ziel = self.ziel_base(activity)
        if activity <= 0:
            # Nichts los -> runter, aber nur bis zum GRUNDWERT: so viel ist die
            # Aktie auf diesem Server auch ohne laufenden Call wert. Vorher ging
            # es bis auf den festen Mindestwert - der Kurs stand deshalb jeden
            # Morgen wieder bei 10, egal wie lebendig der Server sonst ist.
            if base <= self.boden_base():
                return 0.0
            return -self._leerlauf_verfall()
        # EINE Decke fuer alle: dieselbe, die akt_deckel_base() nennt und die das
        # Panel anzeigt. Vorher bremste der Drift schon bei CEIL_FACTOR x Zielkurs,
        # waehrend akt_deckel_base() zusaetzlich den Grundwert beruecksichtigt.
        # Bei DAUERHAFT hoher Aktivitaet waechst der Grundwert bis auf die volle
        # Aktivitaet und liegt dann UEBER dieser Bremse (gemessen: Bremse 94.020,
        # Grundwert 188.001). Der Kurs stand dann zwischen zwei widerspruechlichen
        # Decken - er konnte nicht steigen, wurde vom Grundwert aber nach oben
        # gezogen. Jetzt gibt es nur noch eine Zahl.
        if base >= self.deckel_fuer(activity):
            return 0.0          # am Deckel: seitwaerts, aber kein Minus
        tempo = min(TICK_CAP, activity * TICK_GAIN)
        ueberhang = max(0.0, base / ziel - 1.0)
        daempfung = 1.0 / (1.0 + math.log1p(ueberhang) / max(0.05, LEVEL_SOFT))
        # Nie ganz auf Null bremsen: solange jemand da ist, geht es nach oben.
        return max(MIN_UP, tempo * daempfung)

    def akt_deckel_base(self):
        """Hoechster Basiskurs, den die AKTUELLE Aktivitaet hergibt.

        Das ist die eigentliche Bremse der Aktie (CEIL_FACTOR x Zielkurs). Sie
        muss auf JEDEM Schreibweg gelten - drift_fuer hat sie beachtet, der
        Sofort-Impuls _puls NICHT: mit Rein-/Raus-Springen aus dem Call liess sich
        der Kurs an ihr vorbei bis an die Tagesbremse treiben (gemessen: Deckel
        112.000, erreicht wurden 250.000, ueber Mitternacht Faktor 1.593).
        Deshalb steht sie jetzt hier an einer Stelle.

        Nie UNTER dem Grundwert: dort steht der Kurs im Leerlauf voellig zu Recht.
        Ohne dieses max() zeigte das Panel bei Kurs 6.511 einen 'Deckel 22', und
        der Sofort-Impuls war tot - er bricht ab, sobald der Kurs ueber dem
        Deckel steht, also ausgerechnet dann, wenn jemand einen stillen Server
        wieder betritt."""
        akt = float(self._state().get("act_ema", 0.0) or 0.0)
        return self.deckel_fuer(akt)

    def deckel_fuer(self, activity):
        """Hoechster Basiskurs, den DIESE Aktivitaet hergibt.

        Eine Formel fuer alle: drift_fuer bremst genau hier, akt_deckel_base()
        nennt denselben Wert fuer die aktuelle Aktivitaet, das Panel zeigt ihn an
        und _puls haelt sich daran. Vorher rechnete drift_fuer selbst mit
        CEIL_FACTOR x Zielkurs, waehrend akt_deckel_base() zusaetzlich den
        Grundwert beruecksichtigte - zwei Zahlen, die im Dauerbetrieb um den
        Faktor 2 auseinanderliefen."""
        # CEIL_FACTOR gehoert auf BEIDE Grundwerte, nicht nur auf einen.
        #
        # Vorher stand hier max(ziel*2, boden) - eine skalierte Zahl gegen eine
        # unskalierte. Der Boden waechst aber mit GRUND_FAKTOR (4) und der
        # Deckel nur mit CEIL_FACTOR (2): ab 4*grund >= 2*akt, also einem
        # 3-Tage-Schnitt von etwa der halben aktuellen Aktivitaet, UEBERHOLT der
        # Boden den Deckel. Dann ist deckel == boden, der Kurs steht genau
        # darauf - und drift_fuer gibt 0 zurueck.
        #
        # Nachgemessen mit 10 Leuten im Call:
        #   3-Tage-Schnitt 5  -> Deckel 20.020, Boden 20.010, Drift 2,15 %/min
        #   3-Tage-Schnitt 6  -> Deckel 24.010, Boden 24.010, Drift 0,00 %/min
        # Je lebendiger der Server WAR, desto weniger konnte die Aktie steigen -
        # genau verkehrt herum.
        return max(self.ziel_base(activity), self.boden_base()) * max(1.0, CEIL_FACTOR)

    # --- Flo als Analyst: die KI bewertet den Markt --------------------------
    def _ki_faktor(self):
        """Wie stark die KI die aktuelle Bewegung verstaerkt/daempft (-KI_MAX..+KI_MAX).

        Reine Lesefunktion - die Einschaetzung wird im Hintergrund erneuert
        (ki_tick). Ist sie zu alt oder gar nicht da, ist sie 0 und der Kurs
        verhaelt sich wie ohne KI."""
        st = self._store.data if self._store is not None else {}
        try:
            alter = time.time() - float(st.get("ki_zeit", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
        if alter > KI_MAX_ALTER:
            return 0.0
        try:
            faktor = float(st.get("ki_faktor", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if faktor != faktor:                       # NaN
            return 0.0
        return max(-KI_MAX, min(KI_MAX, faktor))

    def ki_text(self):
        """Der letzte Analysten-Kommentar (oder '')."""
        st = self._store.data if self._store is not None else {}
        try:
            if time.time() - float(st.get("ki_zeit", 0) or 0) > KI_MAX_ALTER:
                return ""
        except (TypeError, ValueError):
            return ""
        return str(st.get("ki_text", "") or "")[:180]

    def _ki_lage(self, people, streams, video, msgs):
        """Kurzer Marktbericht als Text - das bekommt die KI zu sehen."""
        st = self._state()
        akt = float(st.get("act_ema", 0.0) or 0.0)
        kurs = self.price()
        ziel = self.ziel_kurs(akt)
        deckel = self._deckel_kurs()
        stand = "unter" if kurs < ziel else "ueber"
        return (f"Kurs {kurs} Coins. Zielkurs {ziel} (Kurs steht {stand} seinem Wert). "
                f"Deckel {deckel}. Aenderung 1 Tag {self._change_pct(1):+.1f}%, "
                f"7 Tage {self._change_pct(7):+.1f}%. "
                f"Gerade im Call: {people} Leute, {streams} Livestreams, {video} Kameras, "
                f"{msgs} Chat-Nachrichten im letzten Takt. "
                f"Aktivitaet {akt:.1f} Punkte. "
                f"Ausgegebene Anteile: {self.total_shares()}, "
                f"Aktionaere: {self.holders_count()}.")

    _KI_SYSTEM = (
        "Du bist der Chef-Analyst der Discord-Boerse 'FloCorp'. Du bekommst einen "
        "kurzen Marktbericht und entscheidest, ob die aktuelle Kursbewegung "
        "VERSTAERKT oder GEDAEMPFT wird.\n"
        "Antworte in GENAU EINER Zeile in diesem Format:\n"
        "ZAHL|Kommentar\n"
        "ZAHL ist eine ganze Zahl von -40 bis +40 (Prozent, um die die Bewegung "
        "angepasst wird). Positiv = Rally, negativ = Bremse/Korrektur.\n"
        "Kommentar ist EIN kurzer, frecher Boersen-Satz auf Deutsch, hoechstens "
        "90 Zeichen, ohne Anfuehrungszeichen.\n"
        "Regeln fuer die Zahl:\n"
        "- viele Leute im Call, viele Streams, reger Chat -> eher positiv\n"
        "- Kurs steht schon weit UEBER seinem Zielkurs -> eher negativ (Korrektur)\n"
        "- Kurs steht weit UNTER seinem Wert und es ist was los -> deutlich positiv\n"
        "- leerer Server -> negativ\n"
        "- sei nicht immer gleich: mal 0, mal deutlich, das macht den Markt lebendig.\n"
        "Beispiel: -15|Zu heiss gelaufen, die Anleger nehmen Gewinne mit."
    )

    async def ki_tick(self, guild):
        """Holt eine frische Einschaetzung von der KI. Ruft bot.py seltener auf als
        den Kurs-Takt (KI_ALLE_SEK) - eine Boerse braucht keinen Analysten pro
        Minute, und jeder Aufruf kostet.

        Komplett fehlertolerant: geht etwas schief, bleibt die alte Einschaetzung
        stehen und laeuft nach KI_MAX_ALTER von selbst aus."""
        if not self._enabled or self.is_off() or self._store is None:
            return None
        st = self._state()
        jetzt = time.time()
        try:
            letzte = float(st.get("ki_zeit", 0) or 0)
        except (TypeError, ValueError):
            letzte = 0.0
        if jetzt - letzte < KI_ALLE_SEK:
            return None
        try:
            import ai
            if not ai.is_enabled():
                return None
        except Exception:  # noqa: BLE001
            return None
        people, streams, video = self._measure_alle(guild)
        msgs = max(0, int(st.get("msg_count", 0)) - int(st.get("last_msg_count", 0)))
        try:
            antwort = await ai.generate(self._ki_lage(people, streams, video, msgs),
                                        system=self._KI_SYSTEM, temperature=0.9,
                                        max_tokens=80)
        except Exception:  # noqa: BLE001 - die Boerse laeuft auch ohne Analyst
            log.exception("KI-Einschaetzung fehlgeschlagen")
            return None
        faktor, text = self._ki_parse(antwort)
        if faktor is None:
            return None
        st["ki_faktor"] = faktor
        st["ki_text"] = text
        st["ki_zeit"] = jetzt
        log.info("FloCorp Analyst: %+.0f %% - %s", faktor * 100, text or "(ohne Kommentar)")
        await self._save()
        return faktor, text

    @staticmethod
    def _ki_parse(antwort):
        """'-15|Zu heiss gelaufen' -> (-0.15, 'Zu heiss gelaufen').

        Sehr nachsichtig: die KI haelt sich nicht immer ans Format. Findet sich
        keine brauchbare Zahl, wird gar nichts uebernommen (None)."""
        zeilen = str(antwort or "").strip().splitlines()
        if not zeilen:
            return None, ""          # leer oder nur Leerzeichen (war ein IndexError)
        roh = zeilen[0].strip()
        text = ""
        zahl_teil = roh
        if "|" in roh:
            zahl_teil, text = roh.split("|", 1)
        m = re.search(r"[-+]?\d{1,3}", zahl_teil)
        if m is None:
            return None, ""
        try:
            prozent = int(m.group(0))
        except ValueError:
            return None, ""
        prozent = max(-100, min(100, prozent))
        text = re.sub(r"\s+", " ", text).strip().strip('"').strip("'")[:120]
        return max(-KI_MAX, min(KI_MAX, prozent / 100.0)), text

    def _atem_spanne(self):
        """Wie stark der Kurs atmet - gross genug, dass man es auch SIEHT.

        Der angezeigte Kurs ist eine ganze Zahl. Ein Rauschen von 0,1 % bewegt
        bei Kurs 30.000 rund 30 Punkte (gut sichtbar), bei Kurs 10 aber nur
        0,01 - das rundet sich weg, und es stuende wieder still. Deshalb waechst
        die Amplitude bei kleinen Kursen mit, gedeckelt bei ATEM_MAX: Atmen soll
        sichtbar sein, aber nie zum Antrieb werden."""
        kurs = max(1, self.price())
        noetig = 1.0 / float(kurs)           # so viel braucht die gerundete Zahl
        return max(TICK_NOISE * 5, min(ATEM_MAX, noetig))

    def _atem(self, ziel_base=None):
        """Bewegung pro Minute, wenn der Trend 0 waere. NIE exakt 0.

        Vorzeichen und Betrag werden getrennt gezogen - so ist ausgeschlossen,
        dass zufaellig genau 0 herauskommt, und der Erwartungswert bleibt 0.
        Mit 'ziel_base' kommt der Zug zurueck zum Niveau dazu: liegt der Kurs
        darunter, zieht es nach oben und umgekehrt. Damit atmet er UM das
        Niveau, statt von ihm wegzulaufen."""
        spanne = self._atem_spanne()
        zeichen = 1.0 if random.random() < 0.5 else -1.0
        pro_min = zeichen * random.uniform(spanne * 0.25, spanne)
        if ziel_base is None:
            return pro_min
        base = self._base()
        if base > 0 and ziel_base and ziel_base > 0:
            zug = (float(ziel_base) / base - 1.0) * ATEM_RUECK
            pro_min += max(-ATEM_MAX, min(ATEM_MAX, zug))
        return pro_min

    def _leerlauf_verfall(self):
        """Verfall pro Minute bei leerem Call - ab der ERSTEN Minute in voller
        Hoehe, ohne Anlauf und ohne Staffelung. Konstant heisst auch: man kann
        es ausrechnen ('nach einer halben Stunde bin ich 11 % los') statt raten."""
        return IDLE_RATE

    def _activity_tick(self, people, msgs_since, streams=0, video=0, dt=60.0):
        """EIN Aktivitaets-Takt. Rueckgabe: (alt, neu, drift, aktivitaet).

        'dt' ist die Laenge des Takts in Sekunden. Alle Konstanten sind PRO MINUTE
        gedacht - laeuft der Loop schneller (fuer ein lebendigeres Bild), wird hier
        entsprechend heruntergerechnet. Das Tagesverhalten bleibt damit gleich,
        egal ob alle 60 oder alle 20 Sekunden getaktet wird.

        Die Richtung (steigt/faellt) entscheidet die ECHTE, aktuelle Aktivitaet -
        NICHT das geglaettete EMA. Das war ein Bug: das EMA klingt nur langsam ab
        und erreichte fast nie exakt 0; solange es minimal ueber 0 stand, kam der
        Mindest-Anstieg heraus und der Kurs "stieg", obwohl niemand da war."""
        st = self._state()
        skala = max(0.05, min(4.0, float(dt) / 60.0))
        # Nachrichten sind als einzige Groesse eine ZAEHLUNG ueber den Takt - Leute,
        # Streams und Kameras sind Zustaende. Ohne Hochrechnen auf eine Minute
        # haengt die Aktivitaet an der Taktlaenge: gemessen ergab derselbe
        # Chat-Verkehr (30 Nachrichten/Minute, 3 Leute) bei 60-s-Takt 15,0 Punkte,
        # bei 20-s-Takt aber nur 8,0. Der Bot taktet alle 20 s - der Chat zaehlte
        # im Betrieb also deutlich weniger als in jeder Rechnung und Simulation.
        msgs_rate = float(max(0, msgs_since)) / skala
        roh_aktiv = self.activity_of(people, streams, video, msgs_rate)
        # Der Grundwert laeuft IMMER mit - auch im Leerlauf, sonst wuerde er nur
        # von aktiven Minuten hochgezogen und nie wieder sinken.
        self._grund_tick(roh_aktiv, skala)
        alt_ema = float(st.get("act_ema", 0.0) or 0.0)
        # Momentum wird als Rate PRO MINUTE gefuehrt - dadurch ist das Ergebnis
        # unabhaengig davon, ob der Loop alle 20 oder alle 60 Sekunden taktet.
        mom = float(st.get("mom", 0.0) or 0.0)
        if mom != mom:                                   # NaN
            mom = 0.0
        zerfall = MOM_DECAY ** skala

        if roh_aktiv > 0:
            alpha = ACT_ALPHA_UP if roh_aktiv >= alt_ema else ACT_ALPHA_DOWN
            ema = alpha * roh_aktiv + (1 - alpha) * alt_ema
            st["act_ema"] = ema
            st["leer_min"] = 0.0
            # Alles zuerst PRO MINUTE rechnen, ganz am Ende auf den Takt skalieren.
            basis = self.drift_fuer(ema) * (1.0 + self._ki_faktor())
            # Das Momentum steuert im Mittel MOM_WEIGHT x basis bei. Ohne diese
            # Korrektur waere die Aktie allein durch das lebendigere Verhalten
            # wieder deutlich schneller - die Balance soll aber bleiben.
            basis /= (1.0 + MOM_WEIGHT)
            if basis <= 0.0:
                # Am Deckel: nicht totenstill stehen, sondern um den Deckel
                # atmen - mit Zug zurueck, damit es nicht wegwandert.
                pro_min = self._atem(self._deckel_base())
            else:
                # LEBENDIG: die Bewegung schwankt kraeftig um ihren Trend, dazu
                # das nachwirkende Momentum. Vorher war der Chart eine gerade
                # Linie. Nach unten drehen kann es trotzdem nicht - solange jemand
                # da ist, faellt der Kurs nicht (Zusage an den Besitzer).
                # Untergrenze MIN_UP statt 0: ein stark negatives Momentum konnte
                # die Summe auf exakt 0 druecken - Stillstand, obwohl Leute im
                # Call sitzen.
                schwung = random.uniform(1.0 - VOL_SPREAD, 1.0 + VOL_SPREAD)
                pro_min = max(MIN_UP, basis * schwung + MOM_WEIGHT * mom)
        else:
            ema = 0.0
            st["act_ema"] = 0.0
            st["leer_min"] = float(st.get("leer_min", 0.0) or 0.0) + skala
            trend = self.drift_fuer(0.0)
            if trend < 0.0:
                # Beim SINKEN kein Aufhellen durch Zufall - der Verfall soll klar
                # sichtbar sein. Ein laufender Absturz zieht ueber das Momentum nach.
                pro_min = trend * (1.0 + self._ki_faktor())
                pro_min /= (1.0 + MOM_WEIGHT)
                pro_min = min(0.0, pro_min + MOM_WEIGHT * min(0.0, mom))
            else:
                # Am Grundwert angekommen: tiefer geht es nicht, aber stehen soll
                # er auch nicht. Hier war der einzige echte Stillstand im ganzen
                # Modell - gemessen 200 von 200 Takten exakt 0,000 %/min und ein
                # einziger Kurswert ueber 200 Takte.
                pro_min = self._atem(self.boden_base())

        drift = pro_min * skala
        # Die TATSAECHLICH angewandte Rate pro Minute merken. Am Deckel und am
        # Grundwert ist der rechnerische Trend 0, der Kurs bewegt sich dort aber
        # trotzdem (er atmet) - ohne diesen Merker koennte das Panel dort keine
        # echte Zahl zeigen.
        st["letzte_rate"] = pro_min
        st["mom"] = zerfall * mom + (1.0 - zerfall) * pro_min
        activity = roh_aktiv
        alt = self.price()
        st["base"] = self._clamp_base(self._base() * (1 + drift))
        neu = self._sync_price()
        return alt, neu, drift, activity

    def _puls(self, staerke, grund):
        """Sofort-Impuls (Livestream geht an, jemand kommt in den Call): hebt den
        Kurs AUGENBLICKLICH ein Stueck, ohne auf den Minuten-Takt zu warten.
        Pro Minute gedeckelt, damit Rein-/Rausspringen kein Pump-Werkzeug wird."""
        if not self._enabled or staerke <= 0:
            return 0
        st = self._state()
        jetzt = time.time()
        fenster = float(st.get("pulse_min", 0.0) or 0.0)
        summe = float(st.get("pulse_sum", 0.0) or 0.0)
        if jetzt - fenster >= 60:
            st["pulse_min"], summe = jetzt, 0.0
        frei = max(0.0, PULSE_MAX_PER_MIN - summe)
        if frei <= 0:
            return 0
        staerke = min(staerke, frei)
        if staerke <= 0:
            return 0
        # Auch der Sofort-Impuls endet am Aktivitaets-Deckel: sonst ist er ein
        # Pump-Werkzeug (rein/raus aus dem Call) und haengt die ganze Balance aus.
        deckel = self.akt_deckel_base()
        jetzt_base = self._base()
        if jetzt_base >= deckel:
            return 0
        st["pulse_sum"] = summe + staerke
        st["base"] = self._clamp_base(min(deckel, jetzt_base * (1 + staerke)))
        neu = self._sync_price()
        log.info("FloCorp Impuls (%s): +%.2f%% -> Kurs %s.", grund, staerke * 100,
                 self._fmt(neu))
        return neu

    async def note_stream_start(self, member=None):
        """Jemand geht LIVE -> Sofort-Impuls nach oben (und Panel/Chart nachziehen)."""
        if not self._enabled or self.is_off():
            return
        if self._puls(PULSE_STREAM, "Livestream an"):
            await self._save()
            await self._refresh_live()

    async def note_voice_join(self, member=None):
        """Jemand kommt in den Call -> kleiner Sofort-Impuls nach oben."""
        if not self._enabled or self.is_off():
            return
        if self._puls(PULSE_JOIN, "Call-Beitritt"):
            await self._save()
            await self._refresh_live()

    async def sample_and_tick(self, guild, dt=60.0):
        """Loop-Einstieg (bot.py, alle FLOAKTIE_SAMPLE_SECONDS - Standard 60 s): misst
        die aktuelle Aktivitaet (Call-Leute + Streamer + Kameras + Nachrichten seit
        dem letzten Takt) und bewegt den Kurs SOFORT - viel los -> steigt, wenig ->
        faellt.

        'guild' darf ein Server oder eine Liste von Servern sein: die Aktie ist
        fuer alle Server dieselbe, ihre Aktivitaet wird zusammengezaehlt."""
        guilds = self._als_liste(guild)
        if not self._enabled or not guilds or self.is_off():
            return
        try:
            st = self._state()
            people, streams, video = self._measure_alle(guilds)
            total_msgs = int(st.get("msg_count", 0))
            msgs_since = max(0, total_msgs - int(st.get("last_msg_count", total_msgs)))
            st["last_msg_count"] = total_msgs
            self._zuletzt_mess = (people, streams, video, msgs_since)
            self._tages_anker()      # Circuit Breaker fuer heute verankern
            alt, neu, drift, act = self._activity_tick(people, msgs_since, streams,
                                                       video, dt=dt)
            self._record_tick()
            # Einmal pro Tag den Schlusskurs fuer den Langzeit-Chart festhalten.
            today = self._today()
            if st.get("day") != today:
                st["day"] = today
                if not isinstance(st.get("history"), list):
                    st["history"] = []
                st["history"].append({"day": today, "price": self.price()})
                st["history"] = st["history"][-HISTORY_MAX:]
            await self._save()
            # Aendert sich der Kurs (und damit der Boersenwert), Panel UND Chart
            # (das jeweils zuletzt gepostete) live nachziehen.
            if neu != alt:
                await self._refresh_live()
            log.info("FloCorp Takt: Aktiv %.1f (Call %d, Stream %d, Cam %d, Msgs %d) "
                     "-> Kurs %s->%s (%+.3f%%/min, Ziel %s).",
                     act, people, streams, video, msgs_since,
                     self._fmt(alt), self._fmt(neu), drift * 100,
                     self.ziel_kurs())
        except Exception:  # noqa: BLE001
            log.exception("FloCorp Sample/Tick fehlgeschlagen")

    # --- Dividende (Vorteil fuers Halten) --------------------------------
    def dividend_for(self, uid):
        """Coins pro Voice-Runde fuer diesen Aktionaer (0, wenn keine Anteile)."""
        shares = self.shares_of(uid)
        if shares <= 0:
            return 0
        bonus = shares // DIVIDEND_DIVISOR
        if int(uid) == (self.top_holder() or -1):
            bonus *= 2   # Groesster Aktionaer: doppelte Dividende
        return int(min(DIVIDEND_MAX, bonus))

    async def pay_voice_dividends(self, guild):
        """Zahlt jedem Aktionaer, der GERADE aktiv im Voice ist, seine Dividende.
        Gleiche Regeln wie die Voice-XP (kein AFK, nicht taub, >=2 im Kanal).
        bot.py ruft das im Voice-Takt auf - mit einem Server oder mit allen.

        Wer auf ZWEI Servern gleichzeitig im Call sitzt, bekommt trotzdem nur
        einmal: es gibt einen Coin-Topf, also auch nur eine Dividende je Takt."""
        guilds = self._als_liste(guild)
        if not self._enabled or not guilds or self.is_off():
            return
        if not self._holdings():
            return
        changed = False
        bezahlt = set()
        for g in guilds:
            afk_id = getattr(getattr(g, "afk_channel", None), "id", None)
            for vc in (getattr(g, "voice_channels", None) or []):
                if afk_id is not None and getattr(vc, "id", None) == afk_id:
                    continue
                members = [m for m in (getattr(vc, "members", None) or [])
                           if not getattr(m, "bot", False)]
                if len(members) < 2:
                    continue
                for m in members:
                    if m.id in bezahlt:
                        continue
                    vs = getattr(m, "voice", None)
                    if vs is None or getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False):
                        continue
                    bonus = self.dividend_for(m.id)
                    if bonus > 0:
                        bezahlt.add(m.id)
                        economy.add_coins(m.id, bonus, reason="dividende")
                        changed = True
        if changed:
            try:
                await economy.flush()
            except Exception:  # noqa: BLE001
                log.exception("Dividenden-Flush fehlgeschlagen")

    # --- Leaderboard ------------------------------------------------------
    def leaderboard(self, limit=10):
        """[(uid_int, shares), ...] absteigend, nur echte Halter."""
        hold = [(self._n(u), self._n(n)) for u, n in self._holdings().items()
                if self._n(n) > 0]
        hold.sort(key=lambda x: x[1], reverse=True)
        return hold[:limit]

    # --- Anzeige ----------------------------------------------------------
    def _sparkline(self):
        # Nur echte Dicts: ein einzelner Murks-Eintrag in der Historie (null, Zahl,
        # Text) nahm sonst den ganzen Kurs-Chart und das Panel mit.
        hist = [self._n(h.get("price", 0)) for h in self._verlauf("history")][-16:]
        hist = hist + [self.price()]
        if len(hist) < 2:
            return ""
        lo, hi = min(hist), max(hist)
        if hi <= lo:
            return _SPARK[0] * len(hist)
        span = hi - lo
        return "".join(_SPARK[min(len(_SPARK) - 1, int((p - lo) / span * (len(_SPARK) - 1)))]
                       for p in hist)

    def _change_pct(self, back):
        """Kursaenderung (%) ueber die letzten 'back' Tage.

        Nimmt exakt dieselbe Reihe wie Chart und Web-Panel (rollendes Fenster ueber
        die Intraday-Ticks). Vorher wurde gegen den NEUESTEN History-Eintrag
        verglichen - also gegen den Kurs vom heutigen Tageswechsel, was bei '7 Tage'
        sowieso danebenlag und im Panel eine andere Zahl ergab als im Discord."""
        try:
            pts = self._series(max(1, int(back)))
        except Exception:  # noqa: BLE001
            return 0.0
        if len(pts) < 2 or not pts[0]:
            return 0.0
        return (pts[-1] - pts[0]) / pts[0] * 100

    # --- Kurs-Chart (Bild + Zeitraum-Buttons) -----------------------------
    def _series(self, days):
        """Kurs-Reihe (alt->neu) fuer den gewuenschten Zeitraum.

        Nimmt die feinen Intraday-Ticks fuer den Teil, den sie abdecken, und
        ERGAENZT davor die Tages-Schlusskurse aus der Historie. Vorher wurden die
        Tageskurse nur benutzt, wenn es GAR keine Ticks gab - dadurch zeigten
        '7 Tage', '30 Tage' und 'Gesamt' alle dasselbe Fenster, sobald die Ticks
        nur ein paar Tage zurueckreichten. Immer mind. 2 Punkte."""
        now = time.time()
        cutoff = now - max(0.0, float(days)) * 86400
        ticks = [t for t in self._verlauf("ticks") if self._n(t.get("t", 0)) >= cutoff]
        pts = [self._n(t.get("price", 0)) for t in ticks]
        # Die Tages-Schlusskurse ergaenzen NUR den Teil des Fensters, den die
        # Ticks nicht abdecken - ausgewaehlt ueber das DATUM, nicht ueber eine
        # Anzahl. Vorher wurden schlicht die n neuesten Tageskurse davorgeklebt;
        # die liegen aber genau IM Tick-Fenster. Gemessen bei '7 Tage' mit Ticks
        # der letzten 3 Tage: alle 4 vorangestellten Tage waren doppelt, der
        # Kurs von vor 7 Tagen kam gar nicht vor, und die angezeigte Veraenderung
        # war +67 % statt +290 %.
        aeltester = min((self._n(t.get("t", now)) or now for t in ticks), default=now)
        ab_tag = datetime.fromtimestamp(aeltester, TIMEZONE).strftime("%Y-%m-%d")
        von_tag = datetime.fromtimestamp(cutoff, TIMEZONE).strftime("%Y-%m-%d")
        hist_roh = self._verlauf("history")
        davor = [self._n(h.get("price", 0)) for h in hist_roh
                 if von_tag <= str(h.get("day", "")) < ab_tag]
        if not davor and hist_roh and not any(h.get("day") for h in hist_roh):
            # Uralter Stand ohne Datum: lieber die alte Naeherung als gar nichts.
            davor = [self._n(h.get("price", 0)) for h in hist_roh][-max(2, int(float(days))):]
        pts = davor + pts
        pts = [p for p in pts if p] or [self.price()]
        # Der letzte Punkt ist IMMER der aktuelle Kurs - sonst endet die Linie auf
        # einem alten Stand und passt nicht zur angezeigten Kurs-Zahl.
        if pts[-1] != self.price():
            pts.append(self.price())
        if len(pts) == 1:
            pts = [pts[0], pts[0]]
        return pts

    def series(self, days = 1):
        """Oeffentliche Kurs-Reihe fuer Charts (Web-Panel & Discord nutzen dieselbe
        Quelle). Rueckgabe: (punkte, veraenderung_prozent)."""
        pts = self._series(max(0, float(days)) or 1)
        # Fuer die Anzeige auf eine handliche Punktzahl verdichten (gleichmaessig
        # ausduennen, erster und letzter Punkt bleiben erhalten).
        MAX_PUNKTE = 120
        if len(pts) > MAX_PUNKTE:
            schritt = len(pts) / float(MAX_PUNKTE)
            dünn = [pts[int(i * schritt)] for i in range(MAX_PUNKTE)]
            dünn[-1] = pts[-1]
            pts = dünn
        chg = ((pts[-1] - pts[0]) / pts[0] * 100.0) if pts and pts[0] else 0.0
        return pts, round(chg, 2)

    async def _chart_file(self, days, label):
        """Rendert den Kursverlauf als PNG (discord.File) fuer den Zeitraum.

        Das Zeichnen laeuft im Thread: der Chart braucht ~52 ms, und in dieser
        Zeit stand der Event-Loop komplett still - inklusive Musik-Wiedergabe und
        Kurs-Takt. Jedes andere Bild im Projekt wird laengst so gebaut."""
        import render
        series = self._series(days)
        chg = ((series[-1] - series[0]) / series[0] * 100) if series[0] else 0.0
        buf = await asyncio.to_thread(render.floaktie_chart, series, TICKER,
                                      f"{NAME} · {label}", chg)
        return discord.File(buf, filename="floaktie_kurs.png")

    def _range_label(self, days):
        for lbl, dv in _RANGES:
            if dv == days:
                return lbl
        return "Verlauf"

    async def _refresh_live(self):
        """Zieht das zuletzt gepostete Panel UND den zuletzt geposteten Kurs-Chart
        nach - wird nach jeder Kursaenderung aufgerufen (Aktivitaets-Takt & Trades)."""
        await self._refresh_last_panel()
        await self._refresh_last_chart()

    async def _refresh_last_panel(self):
        """Zieht das ZULETZT gepostete 'flo aktie'-Panel nach (Kurs/Boersenwert live)."""
        msg = self._panel_msg
        if msg is None:
            return
        member = SimpleNamespace(id=self._panel_uid) if self._panel_uid else None
        try:
            await msg.edit(embed=self._panel_embed(member))
        except discord.NotFound:
            self._panel_msg = None      # Panel geloescht -> vergessen
        except discord.HTTPException:
            pass
        except Exception:  # noqa: BLE001 - ein Refresh-Fehler darf nichts sprengen
            log.exception("Aktien-Panel-Refresh fehlgeschlagen")

    async def _refresh_last_chart(self):
        """Rendert den zuletzt geposteten Kurs-Chart neu (gleicher Zeitraum) und
        tauscht das Bild aus - so bleibt auch 'flo aktienkurs' live."""
        msg = self._chart_msg
        if msg is None:
            return
        try:
            file = await self._chart_file(self._chart_days,
                                          self._range_label(self._chart_days))
            await msg.edit(attachments=[file])
        except discord.NotFound:
            self._chart_msg = None      # Chart geloescht -> vergessen
        except discord.HTTPException:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Aktien-Chart-Refresh fehlgeschlagen")

    async def open_chart(self, message, days=1):
        """Sendet den Kurs-Chart (Bild) mit Zeitraum-Buttons. Gibt HANDLED zurueck.
        Dieser Chart wird gemerkt -> sein Bild wird ab jetzt live nachgezogen,
        sobald sich der Kurs aendert."""
        if self.is_off():
            return self.aus_embed()
        view = KursView(days)
        try:
            file = await self._chart_file(days, self._range_label(days))
            view.message = await message.reply(
                file=file, view=view, mention_author=False)
            self._protect(view.message, slot="chart")
            self._chart_msg = view.message
            self._chart_days = days
        except Exception:  # noqa: BLE001
            log.exception("Kurs-Chart konnte nicht gesendet werden")
            return "Der Kurs-Chart klemmt gerade - versuch's gleich nochmal."
        return HANDLED

    def _panel_embed(self, member=None):
        st = self._state()
        preis = self.price()
        d1 = self._change_pct(1)
        d7 = self._change_pct(7)
        pfeil = "🟢▲" if d1 >= 0 else "🔴▼"
        emb = discord.Embed(
            title=f"📈 {NAME} ({TICKER})",
            description=(f"**Kurs:** {self._fmt(preis)} {economy.COIN} / Anteil  {pfeil}\n"
                         f"`{self._sparkline()}`\n"
                         f"**24 h:** {d1:+.1f}%  ·  **7 Tage:** {d7:+.1f}%"),
            color=discord.Color.green() if d1 >= 0 else discord.Color.red())
        emb.add_field(name="Börsenwert",
                      value=f"{self._fmt(self.total_shares() * preis)} {economy.COIN}", inline=True)
        emb.add_field(name="Aktionäre", value=str(self.holders_count()), inline=True)
        # Nachvollziehbar machen, WARUM der Kurs sich bewegt.
        akt = float(st.get("act_ema", 0.0) or 0.0)
        trend = self.drift_fuer(akt)
        pro_min = trend * 100
        # Lesbarer machen: kleine Werte pro STUNDE, grosse pro Minute.
        if trend == 0.0:
            # Kein TREND heisst nicht Stillstand: am Deckel und am Grundwert
            # atmet der Kurs um sein Niveau. Hier steht deshalb die Rate des
            # letzten Takts - eine echte Zahl, die zu dem passt, was der Kurs
            # gerade macht.
            letzte = float(st.get("letzte_rate", 0.0) or 0.0) * 100
            if abs(letzte) >= 0.005:
                tempo = f"**{letzte:+.2f} %/min** _(atmet ums Niveau)_"
            else:
                # Noch kein Takt gelaufen (frischer Stand): dann die Spannweite
                # nennen statt "0,00" - stillstehen tut der Kurs ja gerade nicht.
                tempo = (f"**±{self._atem_spanne() * 100:.2f} %/min** "
                         f"_(atmet ums Niveau)_")
        elif abs(pro_min) >= 0.1:
            tempo = f"**{pro_min:+.2f} %/min**"
            # Am Anschlag: sonst wundert man sich, warum mehr Leute nichts mehr
            # bringen. Mehr Aktivitaet hebt dann den DECKEL, nicht das Tempo.
            if pro_min >= TICK_CAP * 100 - 0.001:
                tempo += " _(Anschlag)_"
        else:
            tempo = f"**{self._pro_stunde(trend):+.2f} %/h**"
        am_deckel = self._am_deckel()
        if akt <= 0:
            leer = int(float(st.get("leer_min", 0.0) or 0.0))
            boden = int(round(self.boden_base()
                              * (1.0 + self.total_shares() / LIQUIDITY)))
            if self._am_grundwert():
                # Am Boden angekommen: tiefer geht es nicht, sonst waere die Aktie
                # irgendwann wertlos. Das ausdruecklich SAGEN - sonst sieht es aus,
                # als wuerde der Kurs im Leerlauf einfach nicht sinken.
                hinweis = (f"**Grundwert erreicht** ({self._fmt(max(MIN_PRICE, boden))}) – "
                           f"so viel ist die Aktie wert, weil auf diesem Server "
                           f"regelmäßig etwas los ist. Tiefer geht es nur, wenn "
                           f"es dauerhaft still bleibt. Der Kurs **atmet** hier "
                           f"weiter – er steht nie still.")
            elif leer >= 60:
                hinweis = (f"Seit {leer // 60} h leer – der Kurs fällt mit "
                           f"**{IDLE_PER_30MIN * 100:.0f} % pro halber Stunde** "
                           f"({self._pro_stunde(self.drift_fuer(0)):+.0f} %/h) bis auf "
                           f"**{self._fmt(max(MIN_PRICE, boden))}**.")
            else:
                hinweis = (f"Niemand im Call – der Kurs fällt ab sofort um "
                           f"**{IDLE_PER_30MIN * 100:.0f} % pro halber Stunde**, "
                           f"gleichmäßig, bis wieder jemand da ist.")
        elif am_deckel:
            grund = self._deckel_grund()
            if grund == "tag":
                hinweis = (f"**Tagesdeckel erreicht** ({self._fmt(self._deckel_kurs())}). "
                           f"Pro Tag darf der Kurs höchstens das {DAY_UP:g}-fache seines "
                           f"Morgenstands machen – ab Mitternacht geht es weiter, "
                           f"die Aktivität würde bis "
                           f"{self._fmt(int(self.akt_deckel_base() * (1 + self.total_shares() / LIQUIDITY)))} "
                           f"tragen.")
            elif grund == "max":
                hinweis = (f"**Höchstkurs erreicht** ({self._fmt(MAX_PRICE)} pro Anteil). "
                           f"Höher geht es grundsätzlich nicht.")
            else:
                hinweis = (f"**Deckel erreicht** ({self._fmt(self._deckel_kurs())}) – so viel "
                           f"gibt diese Aktivität her. Mehr Leute im Call oder mehr "
                           f"Livestreams heben den Deckel sofort an. Der Kurs "
                           f"**atmet** hier weiter – er steht nie still.")
        else:
            hinweis = "Je mehr im Call und je mehr Livestreams, desto schneller."
        emb.add_field(
            name="Server-Aktivität",
            value=(f"{akt:.1f} Punkte · {tempo} · "
                   f"Zielkurs **{self._fmt(self.ziel_kurs(akt))}** · "
                   f"Deckel **{self._fmt(self._deckel_kurs())}**\n"
                   f"{self._mess_zeile(getattr(getattr(member, 'guild', None), 'id', None))}\n"
                   f"_Jeder im Call zählt 1, jeder Livestream +{STREAM_BONUS:g}, "
                   f"jede Kamera +{VIDEO_BONUS:g}, Chat zählt mit – aber nur, "
                   f"solange jemand im Call ist. {hinweis}_"),
            inline=False)
        kommentar = self.ki_text()
        if kommentar:
            faktor = self._ki_faktor()
            pfeil_ki = "📈" if faktor > 0.02 else ("📉" if faktor < -0.02 else "➖")
            emb.add_field(
                name=f"🧠 Flos Markt-Analyse {pfeil_ki} {faktor * 100:+.0f} %",
                value=f"_{kommentar}_",
                inline=False)
        top = self.top_holder()
        if top:
            emb.add_field(name="👑 Größter Aktionär",
                          value=f"<@{top}> ({self._fmt(self.shares_of(top))} Anteile)", inline=False)
        if member is not None:
            meine = self.shares_of(member.id)
            frei = self._freies_depot(member.id)
            balken = self._depot_balken(meine)
            emb.add_field(
                name="Dein Depot",
                value=(f"{balken} **{meine}**/{MAX_SHARES_PER_USER} Anteile\n"
                       f"Wert **{self._fmt(meine * preis)}** {economy.COIN} · "
                       f"Dividende **{self._fmt(self.dividend_for(member.id))}** "
                       f"{economy.COIN}/Voice-Runde\n"
                       + (f"_Noch {frei} Anteile Platz._" if frei > 0
                          else "_Depot voll – erst verkaufen._")),
                inline=False)
        emb.add_field(
            name="Regeln, kurz",
            value=(f"📈 Kaufen treibt den Kurs, Verkaufen drückt ihn. Viel Aktivität "
                   f"= hoher Kurs, tote Hose = fallender Kurs.\n"
                   f"⚓ **Grundwert {self._fmt(int(round(self.boden_base() * (1 + self.total_shares() / LIQUIDITY))))}** "
                   f"– so weit fällt der Kurs im Leerlauf zurück, tiefer nicht. "
                   f"Er wächst mit, wenn hier regelmäßig was los ist.\n"
                   f"💰 **Dividende** im Voice (größter Aktionär: doppelt).\n"
                   f"🧾 **Verkaufssteuer {self._sell_fee() * 100:.0f} %** – sie steigt, "
                   f"je weiter der Kurs über seinem Wert steht.\n"
                   f"🎒 **Anteil-Limit: {MAX_SHARES_PER_USER} pro Person** – mehr geht nicht.\n"
                   f"💸 Gekauft wird nur von **eigenem Guthaben** – kein Kredit, "
                   f"kein Minus."),
            inline=False)
        emb.set_footer(text=f"{self._bot_name} aktie kauf max · verkauf alles · aktienkurs · top")
        return emb

    @staticmethod
    def _pro_stunde(pro_min):
        """Rechnet eine Minuten-Rate ehrlich auf eine Stunde hoch - also mit
        Zinseszins, nicht mal 60. Bei -1,5 %/min waren das frueher '-90 %/h';
        tatsaechlich bleiben nach einer Stunde 40 % uebrig, es sind -60 %/h."""
        return (((1.0 + pro_min) ** 60) - 1.0) * 100.0

    @staticmethod
    def zaehlt_hier(gid):
        """Bewegt DIESER Server den Kurs? (guildcfg 'aktie_zaehlt')

        Auf einem neuen Server ist das bewusst AUS - es gibt genau eine Aktie,
        und ein frisch dazugekommener Server soll sie nicht ungefragt mitbewegen.
        Nur: das Panel sagte trotzdem "Niemand im Call", waehrend acht Leute
        dasassen. Es misst naemlich den Server, der zaehlt - und das ist ein
        anderer. Fuer den Fragenden sah das schlicht wie ein kaputter Bot aus."""
        if not gid:
            return True                    # DM/unbekannt: nichts behaupten
        try:
            import guildcfg
            return bool(guildcfg.an(gid, "aktie_zaehlt"))
        except Exception:  # noqa: BLE001 - ohne guildcfg gilt der Standard
            return True

    def _mess_zeile(self, gid=None):
        if gid is not None and not self.zaehlt_hier(gid):
            return ("⚠️ **Dieser Server zählt nicht für die Aktie.** Was hier im "
                    "Call passiert, bewegt den Kurs nicht – gemessen wird der "
                    "Heimatserver.\n"
                    "Einschalten: `Flo einstellung aktie_zaehlt an`")
        return self._mess_zeile_gemessen()

    def _mess_zeile_gemessen(self):
        """Zeigt SCHWARZ AUF WEISS, wer den Kurs gerade traegt.

        Ohne diese Zeile ist nicht nachvollziehbar, warum die Aktivitaet nicht
        auf 0 geht, wenn 'gefuehlt' niemand mehr im Call ist - genau das war der
        Verdacht beim Bot im Call. Bots stehen hier NIE drin, weil sie gar nicht
        erst mitgezaehlt werden."""
        people, streams, video, msgs = self._zuletzt_mess
        if people <= 0:
            return ("👥 **Niemand im Call** – Bots zählen nicht mit."
                    + (f" (nur Chat: {msgs} Nachrichten)" if msgs > 0 else ""))
        namen = list(self._zuletzt_gezaehlt)[:6]
        rest = max(0, people - len(namen))
        liste = ", ".join(namen) + (f" +{rest} weitere" if rest else "")
        extra = []
        if streams > 0:
            extra.append(f"🔴 {streams} Livestream" + ("s" if streams != 1 else ""))
        if video > 0:
            extra.append(f"📷 {video} Kamera" + ("s" if video != 1 else ""))
        if msgs > 0:
            extra.append(f"💬 {msgs} Nachrichten")
        schwanz = (" · " + " · ".join(extra)) if extra else ""
        return f"👥 **{people} im Call:** {liste}{schwanz}"

    def _deckel_base(self):
        """Der WIRKLICH bindende Basiskurs-Deckel: der kleinste von dreien.

        Das Panel zeigte bisher nur den Aktivitaets-Deckel - an einem Tag, der
        weit unten angefangen hat, stoppt aber die TAGESBREMSE viel frueher.
        Beispiel aus dem Betrieb: Aktivitaets-Deckel 888.674, tatsaechlich war bei
        52.453 Schluss. Wer das nicht weiss, wartet stundenlang auf einen Anstieg,
        der heute gar nicht mehr kommen kann."""
        deckel = self.akt_deckel_base()
        _lo, tag_hi = self._tages_band()
        if tag_hi is not None:
            deckel = min(deckel, tag_hi)
        return min(deckel, self._base_ceiling())

    def _deckel_kurs(self):
        """Der ANGEZEIGTE Kurs, an dem heute Schluss ist."""
        deckel = self._deckel_base() * (1.0 + self.total_shares() / LIQUIDITY)
        return max(MIN_PRICE, min(MAX_PRICE, int(round(deckel))))

    def _deckel_grund(self):
        """Warum ist bei diesem Kurs Schluss? ('aktivitaet' | 'tag' | 'max')"""
        akt = self.akt_deckel_base()
        _lo, tag_hi = self._tages_band()
        absolut = self._base_ceiling()
        kleinste = self._deckel_base()
        if tag_hi is not None and kleinste >= tag_hi - 0.5:
            return "tag"
        if kleinste >= absolut - 0.5:
            return "max"
        return "aktivitaet" if kleinste <= akt + 0.5 else "aktivitaet"

    def _am_grundwert(self):
        """Steht der Kurs (praktisch) auf dem Grundwert?

        Die Toleranz richtet sich nach der ATEM-AMPLITUDE: der Kurs atmet um sein
        Niveau und liegt danach ein Stueck darueber, bis der Verfall ihn wieder
        eingeholt hat. Bei kleinen Kursen atmet er kraeftiger (sonst rundet sich
        die Bewegung weg), also darf auch die Toleranz dort groesser sein. Mit
        einer festen Grenze sprang die Panel-Meldung zwischen 'Grundwert
        erreicht' und 'faellt gerade' hin und her, obwohl sich nichts aendert."""
        return self._base() <= self.boden_base() * (1.0 + max(0.02, self._atem_spanne()))

    def _am_deckel(self):
        """Steht der Kurs (praktisch) am Deckel? Dann steigt er nur noch minimal."""
        akt = float(self._state().get("act_ema", 0.0) or 0.0)
        if akt <= 0:
            return False
        return self._base() >= self._deckel_base() * 0.995

    def _depot_balken(self, anteile):
        """Kleiner Fortschrittsbalken fuer die Depot-Auslastung."""
        voll = max(0, min(10, int(round(10.0 * anteile / max(1, MAX_SHARES_PER_USER)))))
        return "▰" * voll + "▱" * (10 - voll)

    def _depot_embed(self, member):
        preis = self.price()
        meine = self.shares_of(member.id)
        emb = discord.Embed(
            title=f"💼 Dein {TICKER}-Depot",
            color=discord.Color.blurple())
        emb.add_field(name="Anteile", value=str(meine), inline=True)
        emb.add_field(name="Kurs", value=f"{self._fmt(preis)} {economy.COIN}", inline=True)
        emb.add_field(name="Depotwert", value=f"{self._fmt(meine * preis)} {economy.COIN}", inline=True)
        emb.add_field(name="Dividende / Voice-Runde",
                      value=f"{self._fmt(self.dividend_for(member.id))} {economy.COIN}"
                            + ("  (👑 doppelt!)" if int(member.id) == (self.top_holder() or -1) and meine > 0 else ""),
                      inline=False)
        rang = None
        for i, (uid, _n) in enumerate(self.leaderboard(999), 1):
            if uid == member.id:
                rang = i
                break
        if rang:
            emb.set_footer(text=f"Du bist auf Platz {rang} der Aktionäre.")
        return emb

    def _top_embed(self):
        board = self.leaderboard(10)
        preis = self.price()
        emb = discord.Embed(
            title=f"🏆 Größte {TICKER}-Aktionäre",
            color=discord.Color.gold())
        if not board:
            emb.description = "Noch hält niemand Anteile. Sei der Erste! 📈"
            return emb
        medal = ["🥇", "🥈", "🥉"]
        zeilen = []
        for i, (uid, n) in enumerate(board):
            pre = medal[i] if i < 3 else f"**{i + 1}.**"
            zeilen.append(f"{pre} <@{uid}> — **{self._fmt(n)}** Anteile "
                          f"({self._fmt(n * preis)} {economy.COIN})")
        emb.description = "\n".join(zeilen)
        emb.set_footer(text="Der Größte bekommt doppelte Voice-Dividende. 👑")
        return emb

    # --- Befehl -----------------------------------------------------------
    async def handle(self, message):
        if not self._enabled or message.guild is None:
            return None
        try:
            import ai
            cmd = ai.strip_lead(message.content or "")
        except Exception:  # noqa: BLE001
            cmd = message.content or ""
        parts = cmd.split()
        first = parts[0].lower().strip(".,;:!?") if parts else ""
        if first not in _CMDS and first not in _CHART_CMDS:
            return None
        if self.is_off():
            return self.aus_embed()
        if not economy.is_enabled():
            return "💤 Gerade gibt's keine Coins - das Economy-System schläft."
        # 'aktienkurs'/'kurs'/'chart' (oder 'aktie chart') -> Kurs-Chart mit Buttons.
        if first in _CHART_CMDS:
            return await self.open_chart(message, 1)
        sub = parts[1].lower() if len(parts) >= 2 else ""
        arg = parts[2].lower() if len(parts) >= 3 else ""
        if sub in ("chart", "kurs", "kursverlauf", "verlauf", "graph"):
            return await self.open_chart(message, 1)
        if sub in ("kauf", "kaufen", "buy", "long"):
            return await self.buy(message.author, self._resolve_count(message.author, arg or "1"))
        if sub in ("verkauf", "verkaufen", "sell", "verkaufe", "short", "dump"):
            return await self.sell(message.author, self._resolve_count(message.author, arg or "1", selling=True))
        if sub in ("reset", "zuruecksetzen", "zurücksetzen", "neustart"):
            if int(getattr(message.author, "id", 0)) != OWNER_ID:
                return "🔒 Die Aktie zurücksetzen darf nur der Chef."
            alles = arg in ("alles", "all", "hart", "komplett", "depots")
            alt_k, neu_k, depots = await self.reset_kurs(depots_leeren=alles)
            e = discord.Embed(
                title="♻️ FloCorp zurückgesetzt",
                description=(f"Kurs **{self._fmt(alt_k)}** → **{self._fmt(neu_k)}** "
                             f"{economy.COIN}\nVerlauf gelöscht, Tagesbremse neu "
                             f"verankert."),
                colour=discord.Colour.from_rgb(87, 242, 135))
            e.add_field(name="Depots",
                        value=(f"{depots} geleert" if alles
                               else "bleiben erhalten (jetzt eben billig)"),
                        inline=True)
            e.set_footer(text=f"{self._bot_name} aktie reset alles · löscht auch die Depots")
            return e
        if sub in ("top", "leaderboard", "rangliste", "aktionäre", "aktionaere"):
            return self._top_embed()
        if sub in ("depot", "portfolio", "anteile", "meins"):
            return self._depot_embed(message.author)
        # sonst: Panel mit Buttons. Dieses Panel merken -> es wird ab jetzt LIVE
        # aktualisiert, sobald sich der Boersenwert aendert (jede Minute + bei Trades).
        view = FloAktieView()
        try:
            view.message = await message.reply(
                embed=self._panel_embed(message.author), view=view, mention_author=False)
            self._protect(view.message)
            self._panel_msg = view.message
            self._panel_uid = message.author.id
        except (discord.HTTPException, TypeError):
            log.exception("FloCorp-Panel konnte nicht gesendet werden")
            return "Die Börse klemmt gerade - versuch's gleich nochmal."
        return HANDLED

    # --- Auto-Loesch-Schutz -----------------------------------------------
    def _protect(self, msg, *, slot="panel"):
        """Neues Panel schuetzen und das VORIGE freigeben.

        Der Auto-Loesch-Schutz in bot.py kannte bisher nur den Weg hinein: jedes
        neue Panel trug seine ID ein, niemand trug je eine aus. Das Set wuchs
        damit die ganze Laufzeit, und die alten Panels blieben in den
        Aufraeum-Kanaelen fuer immer stehen. Es kann ohnehin nur EINS je Slot
        aktuell sein - also gibt das neue das alte frei."""
        if msg is None:
            return
        try:
            import bot
            alt = self._geschuetzt.get(slot)
            if alt is not None and alt != msg:
                bot.release_message(alt)
            self._geschuetzt[slot] = msg
            bot.protect_message(msg)
        except Exception:  # noqa: BLE001
            pass


# --- Interaktive View --------------------------------------------------------
class _TradeButton(discord.ui.Button):
    def __init__(self, label, emoji, style, action, count, row=0):
        super().__init__(label=label, emoji=emoji, style=style, row=row)
        self.action = action     # "buy" | "sell"
        self.count = count       # int oder "max"/"alles"

    async def callback(self, interaction):
        await self.view._trade(interaction, self.action, self.count)


class _InfoButton(discord.ui.Button):
    def __init__(self, label, emoji, kind):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=2)
        self.kind = kind         # "depot" | "top"

    async def callback(self, interaction):
        if self.kind == "top":
            emb = instance._top_embed()
        else:
            emb = instance._depot_embed(interaction.user)
        await interaction.response.send_message(embed=emb, ephemeral=True)


class FloAktieView(discord.ui.View):
    """Handels-Panel: kaufen/verkaufen + Depot/Top. Jeder handelt für sich."""

    def __init__(self):
        super().__init__(timeout=None)
        self.message = None
        # Reihe 0: Kaufen (inkl. MAX - so viele, wie das Guthaben hergibt).
        self.add_item(_TradeButton("Kauf 1", "📈", discord.ButtonStyle.success, "buy", 1, row=0))
        self.add_item(_TradeButton("Kauf 10", "📈", discord.ButtonStyle.success, "buy", 10, row=0))
        self.add_item(_TradeButton("Kauf MAX", "🤑", discord.ButtonStyle.success, "buy", "max", row=0))
        # Reihe 1: Verkaufen (inkl. alles).
        self.add_item(_TradeButton("Verkauf 1", "📉", discord.ButtonStyle.danger, "sell", 1, row=1))
        self.add_item(_TradeButton("Verkauf alles", "💸", discord.ButtonStyle.danger, "sell", "alles", row=1))
        # Reihe 2: Infos.
        self.add_item(_InfoButton("Depot", "💼", "depot"))
        self.add_item(_InfoButton("Top", "🏆", "top"))

    async def _trade(self, interaction, action, count):
        # ZUERST bestaetigen, DANN handeln. Discord gibt einer Interaktion nur
        # 3 Sekunden; buy()/sell() speichern aber, ziehen das Panel nach und
        # rendern den Chart neu. Wurde das langsamer, starb die Interaktion mit
        # "Interaktion fehlgeschlagen" - obwohl der Kauf laengst gebucht war.
        # Wer das sah, hat verstaendlicherweise nochmal geklickt und ein zweites
        # Mal gekauft. Mit defer() ist die Antwort sofort reserviert.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            log.exception("FloCorp-Trade (Button): defer fehlgeschlagen")
            return

        async def melden(inhalt):
            try:
                if isinstance(inhalt, discord.Embed):
                    await interaction.followup.send(embed=inhalt, ephemeral=True)
                else:
                    await interaction.followup.send(str(inhalt), ephemeral=True)
            except discord.HTTPException:
                log.exception("FloCorp-Trade (Button): Antwort fehlgeschlagen")

        antwort = None
        try:
            if action == "buy":
                n = instance._resolve_count(interaction.user, str(count))
                if n < 1:
                    await melden("Dein Guthaben reicht gerade für keinen ganzen "
                                 "Anteil. 😬 Auf Pump gibt es hier nichts – erst "
                                 "verdienen oder Anteile verkaufen.")
                    return
                antwort = await instance.buy(interaction.user, n)
            else:
                n = instance._resolve_count(interaction.user, str(count), selling=True)
                antwort = await instance.sell(interaction.user, n)
        except Exception:  # noqa: BLE001
            log.exception("FloCorp-Trade (Button) fehlgeschlagen")
            antwort = "Beim Handeln ist etwas schiefgelaufen - versuch's gleich nochmal."
        # buy()/sell() antworten mit einem Embed (Bestaetigung) oder mit Text
        # (Fehlerfall) - beides muss hier durchgehen.
        await melden(antwort)
        # Das Panel-Embed selbst wird von buy()/sell() ueber _refresh_last_panel()
        # aktualisiert (das zuletzt gepostete Panel bleibt so immer live).


class _KursButton(discord.ui.Button):
    def __init__(self, label, days, active):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary)
        self.days = days

    async def callback(self, interaction):
        await self.view.show(interaction, self.days)


class KursView(discord.ui.View):
    """Kurs-Chart mit Zeitraum-Buttons (1 Tag / 7 Tage / 30 Tage / Gesamt)."""

    def __init__(self, days=1):
        super().__init__(timeout=300)
        self.message = None
        self.days = days
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        for lbl, dv in _RANGES:
            self.add_item(_KursButton(lbl, dv, dv == self.days))

    async def show(self, interaction, days):
        self.days = days
        self._rebuild()
        # Dieser Chart ist jetzt der 'aktuelle' - Live-Refresh nutzt seinen Zeitraum.
        if self.message is not None:
            instance._chart_msg = self.message
        instance._chart_days = days
        try:
            file = await instance._chart_file(days, instance._range_label(days))
            await interaction.response.edit_message(attachments=[file], view=self)
        except Exception:  # noqa: BLE001
            log.exception("Kurs-Chart-Update fehlgeschlagen")
            try:
                await interaction.response.send_message(
                    "Der Chart klemmt gerade - versuch's gleich nochmal.", ephemeral=True)
            except discord.HTTPException:
                pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# --- Singleton + Modul-API ---------------------------------------------------
instance = FloAktie()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
handle_aus = instance.handle_aus
is_off = instance.is_off
note_message = instance.note_message
note_stream_start = instance.note_stream_start
note_voice_join = instance.note_voice_join
activity_of = instance.activity_of
drift_fuer = instance.drift_fuer
ziel_base = instance.ziel_base
ziel_kurs = instance.ziel_kurs
akt_deckel_base = instance.akt_deckel_base
deckel_fuer = instance.deckel_fuer
sample_and_tick = instance.sample_and_tick
ki_tick = instance.ki_tick
ki_text = instance.ki_text
pay_voice_dividends = instance.pay_voice_dividends
price = instance.price
reset_kurs = instance.reset_kurs
series = instance.series
admin_shares = instance.admin_shares
admin_set_price = instance.admin_set_price
shares_of = instance.shares_of
total_shares = instance.total_shares
