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

import logging
import math
import os
import random
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord

import economy
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

# --- Balance (per .env justierbar) -------------------------------------------
START_PRICE = int(os.getenv("FLOAKTIE_START_PRICE", "1000") or "1000")   # Coins/Anteil
MIN_PRICE = int(os.getenv("FLOAKTIE_MIN_PRICE", "50") or "50")
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
# Kredit-Kauf) absurd zu verzerren. Mehr geht ueber mehrere Orders - dank der
# pfad-unabhaengigen Kurve kostet das genau dasselbe.
MAX_SHARES_PER_TRADE = int(os.getenv("FLOAKTIE_MAX_TRADE", "750") or "750")

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
FAIR_BASE = float(os.getenv("FLOAKTIE_FAIR_BASE", "300") or "300")     # Kurs eines toten Servers
# Wert je Aktivitaetspunkt. Merksatz: Zielkurs ~ 1.000 x Aktivitaetspunkte.
# (Vorher 120 - damit "lohnte" sich bei 12 Punkten nur ein Kurs von 1.740, und
# jeder Kurs darueber wurde auf den Mindest-Anstieg heruntergebremst: der Kurs
# stand bei hoher Aktivitaet praktisch still. Genau das war der Fehler.)
AKT_WERT = float(os.getenv("FLOAKTIE_AKT_WERT", "1000") or "1000")
# Plus je Aktivitaetspunkt und Minute. 0,0012 heisst: 1 Zuhoerer +0,12 %/min,
# 20 Punkte +2,4 %/min, 40 Punkte +4,8 %/min. Das Tempo bestimmt nur, WIE SCHNELL
# der Kurs an sein Niveau kommt - wie HOCH er laeuft, legt AKT_WERT/CEIL_FACTOR
# fest. Deshalb darf es ruhig steil sein.
# 0,006 heisst: 1 Zuhoerer +0,6 %/min, 3 Leute +1,8 %/min, 17 Punkte am Deckel.
# Bei 3 Leuten im Call kommt ueber einen Vormittag rund das 25- bis 55-fache
# heraus - genau die Groessenordnung, in der aus 600k Anteilen 23 Mio wurden.
TICK_GAIN = float(os.getenv("FLOAKTIE_TICK_GAIN", "0.006") or "0.006")
TICK_CAP = float(os.getenv("FLOAKTIE_TICK_CAP", "0.10") or "0.10")     # max +10 % in einem Takt
# Verfall bei LEEREM Server - gestaffelt: je laenger nichts los ist, desto
# schneller faellt der Kurs (wie eine Aktie, die keiner mehr will). Eine kurze
# Pause tut fast nichts, stundenlanger Leerlauf laesst den Kurs deutlich sinken.
#   Verfall/min = IDLE_RATE * min(1, Leerlauf-Minuten / IDLE_RAMP_MIN)
# IDLE_RATE 0,0025 = bis zu -0,25 %/min = -15 %/h bei vollem Leerlauf.
IDLE_RATE = float(os.getenv("FLOAKTIE_IDLE_RATE", "0.0025") or "0.0025")
# Nach so vielen zusammenhaengenden Leerlauf-Minuten ist der Verfall voll da.
IDLE_RAMP_MIN = float(os.getenv("FLOAKTIE_IDLE_RAMP", "25") or "25")
# Alt (nur noch als Startwert der Rampe, damit auch die erste Minute schon sinkt).
IDLE_DECAY = float(os.getenv("FLOAKTIE_IDLE_DECAY", "0.0003") or "0.0003")
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
# BEWUSST genauso gross wie IDLE_DECAY: sonst summiert sich der Mindest-Anstieg
# bei einem Server, auf dem fast immer jemand online ist, auf +23 %/Tag - und der
# Kurs laeuft langfristig weg (in der Simulation nach 30 Tagen 50 Millionen).
MIN_UP = float(os.getenv("FLOAKTIE_MIN_UP", "0.00008") or "0.00008")
# Notbremse GANZ weit oben, damit der Kurs nicht in absurde Gleitkomma-Regionen
# laeuft - im Alltag greift sie nicht. Der Deckel waechst mit der Aktivitaet:
# 3 Punkte tragen bis 3,3 Mio, 20 Punkte bis 20 Mio, 39 Punkte bis 39 Mio.
# (Vorher stand er bei 2,0 - da war bei 3 Leuten schon ab Kurs 6.600 Schluss und
# der Kurs stand komplett still, obwohl Leute im Call waren.)
CEIL_FACTOR = float(os.getenv("FLOAKTIE_CEIL", "1000") or "1000")
# Glaettung asymmetrisch: mehr Aktivitaet wird fast sofort uebernommen (man soll es
# sehen), weniger nur langsam (eine kurze Pause soll den Kurs nicht abwuergen).
ACT_ALPHA_UP = float(os.getenv("FLOAKTIE_ACT_ALPHA_UP", "0.85") or "0.85")
ACT_ALPHA_DOWN = float(os.getenv("FLOAKTIE_ACT_ALPHA_DOWN", "0.15") or "0.15")
MSG_WEIGHT = float(os.getenv("FLOAKTIE_MSG_WEIGHT", "0.5") or "0.5")       # eine Nachricht = ein halber Zuhoerer
MSG_MAX_ACT = float(os.getenv("FLOAKTIE_MSG_MAX_ACT", "12") or "12")       # Chat allein kann nicht beliebig pumpen
STREAM_BONUS = float(os.getenv("FLOAKTIE_STREAM_BONUS", "2.5") or "2.5")   # ein Live-Streamer zaehlt so viel EXTRA
VIDEO_BONUS = float(os.getenv("FLOAKTIE_VIDEO_BONUS", "1.5") or "1.5")     # eine Kamera zaehlt so viel extra
TICK_NOISE = float(os.getenv("FLOAKTIE_TICK_NOISE", "0.0002") or "0.0002") # nur noch ein Hauch Boersen-Rauschen
# Sofort-Impulse fuer Ereignisse (unabhaengig vom Niveau, pro Minute gedeckelt).
PULSE_STREAM = float(os.getenv("FLOAKTIE_PULSE_STREAM", "0.004") or "0.004")   # Livestream geht an: +0,4 %
PULSE_JOIN = float(os.getenv("FLOAKTIE_PULSE_JOIN", "0.0015") or "0.0015")     # jemand kommt in den Call: +0,15 %
PULSE_MAX_PER_MIN = float(os.getenv("FLOAKTIE_PULSE_MAX", "0.02") or "0.02")   # zusammen max +2 %/Minute
# Alter Name, nur noch fuer den Startwert der Glaettung (nicht mehr im Drift).
ACT_BASELINE = 0.0

# Dividende: Coins pro Voice-Runde je 'DIVIDEND_DIVISOR' Anteile (gedeckelt).
DIVIDEND_DIVISOR = int(os.getenv("FLOAKTIE_DIVIDEND_DIVISOR", "10") or "10")
DIVIDEND_MAX = int(os.getenv("FLOAKTIE_DIVIDEND_MAX", "5000") or "5000")

HISTORY_MAX = 60

_SPARK = "▁▂▃▄▅▆▇█"


class FloAktie:
    """Objektorientierte Huelle: Kurs, Depots & Historie leben auf der Instanz."""

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        self._store = None
        # Das ZULETZT gepostete 'flo aktie'-Panel (Message) + fuer wen es gepostet
        # wurde. Es wird live nachgezogen, sobald sich Kurs/Boersenwert aendern.
        self._panel_msg = None
        self._panel_uid = None
        # Der ZULETZT gepostete Kurs-Chart ('flo aktienkurs') + sein Zeitraum -
        # das Bild wird ebenfalls live nachgezogen.
        self._chart_msg = None
        self._chart_days = 1

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
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
        self._sync_price()
        if not st.get("history"):
            st["history"] = [{"day": self._today(), "price": int(st["price"])}]
        self._enabled = True
        log.info("FloCorp-Aktie (%s) aktiv: Kurs %s Coins, %d Aktionaere.",
                 TICKER, self._fmt(st["price"]), len(st.get("holdings", {})))
        return True

    def is_enabled(self):
        return self._enabled

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
        return int(self._state().get("price", START_PRICE))

    def _record_tick(self, now=None):
        """Schreibt einen Kurs-Zeitpunkt (fuer den Chart) - bei jedem Trade, Sample
        und Tages-Tick. Zeitstempel als Epoch, Liste gedeckelt."""
        st = self._state()
        ticks = st.setdefault("ticks", [])
        t = int(now if now is not None else time.time())
        ticks.append({"t": t, "price": self.price()})
        if len(ticks) > HISTORY_TICKS_MAX:
            del ticks[:len(ticks) - HISTORY_TICKS_MAX]

    def _holdings(self):
        return self._state().setdefault("holdings", {})

    def shares_of(self, uid):
        return int(self._holdings().get(str(uid), 0))

    def total_shares(self):
        return sum(int(v) for v in self._holdings().values())

    def holders_count(self):
        return sum(1 for v in self._holdings().values() if int(v) > 0)

    def top_holder(self):
        """UID (int) des groessten Aktionaers oder None."""
        hold = self._holdings()
        best, best_n = None, 0
        for uid, n in hold.items():
            if int(n) > best_n:
                best, best_n = int(uid), int(n)
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
        return max(self._base_floor(), base)

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
        st["price"] = max(MIN_PRICE, int(round(self._price_at(self.total_shares()))))
        return st["price"]

    def _buy_cost(self, shares):
        """Kosten fuer 'shares' Anteile (Integral + Gebuehr) und der Kurs danach."""
        s = self.total_shares()
        brutto = self._integral(s, s + max(0, shares))
        cost = int(brutto * (1.0 + TRADE_FEE)) + 1 if brutto > 0 else 0
        neu = max(MIN_PRICE, int(round(self._price_at(s + max(0, shares)))))
        return cost, neu

    def _sell_proceeds(self, shares):
        """Erloes fuer 'shares' Anteile (Integral - Gebuehr) und der Kurs danach."""
        s = self.total_shares()
        shares = max(0, min(int(shares), s))
        brutto = self._integral(s - shares, s)
        proceeds = max(0, int(brutto * (1.0 - TRADE_FEE)))
        neu = max(MIN_PRICE, int(round(self._price_at(s - shares))))
        return proceeds, neu

    def _max_affordable(self, coins):
        """Wie viele Anteile man sich mit 'coins' leisten kann (inkl. Gebuehr).
        Binaersuche auf der Kurve - nie mehr, als das Guthaben wirklich deckt."""
        coins = int(coins)
        if coins <= 0:
            return 0
        lo, hi = 0, MAX_SHARES_PER_TRADE
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
                return max(0, self._max_affordable(economy.get_coins(member.id)))
        try:
            n = int(token)
        except (TypeError, ValueError):
            return 1
        return max(1, min(n, MAX_SHARES_PER_TRADE))

    async def buy(self, member, count):
        count = int(count)
        if count < 1:
            return "Kauf mindestens **1** Anteil. 📈"
        if count > MAX_SHARES_PER_TRADE:
            count = MAX_SHARES_PER_TRADE
        # Kosten IMMER vor der Depot-Aenderung berechnen (Kurve haengt an den
        # ausgegebenen Anteilen).
        cost, _ = self._buy_cost(count)
        # Aktien auf KREDIT: kein Guthaben-Check - man darf beliebig tief ins Minus
        # (allow_negative). Wie mit Hebel an einer echten Boerse: faellt der Kurs,
        # sitzt du auf den Schulden. Nur die Aktie holt dich da wieder raus.
        economy.add_coins(member.id, -cost, reason="floaktie", allow_negative=True)
        self._holdings()[str(member.id)] = self.shares_of(member.id) + count
        neu = self._sync_price()       # Kauf hebt den Kurs (Kurve neu auswerten)
        self._record_tick()
        await self._save_all()
        await self._refresh_live()
        stand = economy.get_coins(member.id)
        warn = ""
        if stand < 0:
            warn = (f"\n⚠️ Du bist jetzt mit **{self._fmt(stand)}** {economy.COIN} im "
                    f"**MINUS** – nur steigende Kurse (oder Verkauf) holen dich da raus!")
        return (f"📈 Gekauft! **{count}** Anteile {TICKER} für **{self._fmt(cost)}** "
                f"{economy.COIN}.\nNeuer Kurs: **{self._fmt(neu)}** {economy.COIN} "
                f"· dein Depot: **{self.shares_of(member.id)}** Anteile.{warn}")

    async def sell(self, member, count):
        count = int(count)
        habe = self.shares_of(member.id)
        if habe <= 0:
            return f"Du besitzt keine {TICKER}-Anteile zum Verkaufen."
        if count < 1:
            return "Verkauf mindestens **1** Anteil. 📉"
        count = min(count, habe)
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
        return (f"📉 Verkauft! **{count}** Anteile {TICKER} für **{self._fmt(proceeds)}** "
                f"{economy.COIN}.\nNeuer Kurs: **{self._fmt(neu)}** {economy.COIN} "
                f"· dein Depot: **{rest}** Anteile.")

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
        st["base"] = preis / (1.0 + self.total_shares() / LIQUIDITY)
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
            st = self._state()
            st["base"] = vorher_kurs / (1.0 + self.total_shares() / LIQUIDITY)
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
        if not self._enabled:
            return
        st = self._state()
        st["msg_count"] = int(st.get("msg_count", 0)) + 1

    def _measure(self, guild):
        """Misst die Voice-Aktivitaet: (Leute, Live-Streamer, Kameras).

        Liest bewusst channel.voice_states und nicht channel.members: die
        voice_states stehen unabhaengig vom Member-Cache zur Verfuegung
        (discord.py nennt sie ausdruecklich den Ersatz fuer .members, "when the
        member cache is unavailable"). Damit zaehlt der Takt auch dann richtig,
        wenn der Cache nach einem Neustart noch leer ist. Fehlt die Property
        (aeltere discord.py), faellt es auf .members zurueck."""
        people = streams = video = 0
        if guild is None:
            return 0, 0, 0
        afk_id = getattr(getattr(guild, "afk_channel", None), "id", None)
        eigene_id = getattr(getattr(guild, "me", None), "id", 0)
        for vc in (getattr(guild, "voice_channels", None) or []):
            if afk_id is not None and getattr(vc, "id", None) == afk_id:
                continue
            states = getattr(vc, "voice_states", None)
            if states:
                for uid, vs in list(states.items()):
                    if uid == eigene_id:
                        continue                      # Flo selbst zaehlt nicht
                    m = None
                    try:
                        m = guild.get_member(uid)
                    except Exception:  # noqa: BLE001
                        m = None
                    if m is not None and getattr(m, "bot", False):
                        continue                      # andere Bots auch nicht
                    people += 1
                    if getattr(vs, "self_stream", False):
                        streams += 1
                    if getattr(vs, "self_video", False):
                        video += 1
                continue
            for m in (getattr(vc, "members", None) or []):
                if getattr(m, "bot", False):
                    continue
                people += 1
                vs = getattr(m, "voice", None)
                if vs is not None:
                    if getattr(vs, "self_stream", False):
                        streams += 1
                    if getattr(vs, "self_video", False):
                        video += 1
        return people, streams, video

    def activity_of(self, people, streams=0, video=0, msgs=0):
        """Aktivitaetspunkte: jeder im Call zaehlt 1, jeder Livestream EXTRA,
        jede Kamera extra, Nachrichten anteilig (gedeckelt gegen Spam-Pumpen)."""
        chat = min(MSG_MAX_ACT, MSG_WEIGHT * float(max(0, msgs)))
        return (float(max(0, people))
                + STREAM_BONUS * float(max(0, streams))
                + VIDEO_BONUS * float(max(0, video))
                + chat)

    def ziel_base(self, activity):
        """Basiskurs, den DIESE Aktivitaet hergibt - dorthin strebt der Kurs.
        Jeder im Call zaehlt 1, jeder Livestream extra, Chat zaehlt mit."""
        return max(BASE_FLOOR, FAIR_BASE + AKT_WERT * max(0.0, float(activity)))

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
            # Nichts los -> runter Richtung Wert eines toten Servers, und zwar
            # je laenger leer, desto schneller (siehe _leerlauf_verfall).
            if base <= ziel:
                return 0.0
            return -self._leerlauf_verfall()
        if base >= ziel * max(1.0, CEIL_FACTOR):
            return 0.0          # weit ueber Wert: seitwaerts, aber kein Minus
        tempo = min(TICK_CAP, activity * TICK_GAIN)
        ueberhang = max(0.0, base / ziel - 1.0)
        daempfung = 1.0 / (1.0 + math.log1p(ueberhang) / max(0.05, LEVEL_SOFT))
        # Nie ganz auf Null bremsen: solange jemand da ist, geht es nach oben.
        return max(MIN_UP, tempo * daempfung)

    def _leerlauf_verfall(self):
        """Verfall pro Minute bei leerem Server - waechst mit der Leerlauf-Dauer.
        Erste Minute schon spuerbar (IDLE_DECAY), nach IDLE_RAMP_MIN voll (IDLE_RATE)."""
        leer = float(self._state().get("leer_min", 0.0) or 0.0)
        anteil = min(1.0, leer / max(1.0, IDLE_RAMP_MIN))
        return max(IDLE_DECAY, IDLE_RATE * anteil)

    def _activity_tick(self, people, msgs_since, streams=0, video=0):
        """EIN Aktivitaets-Takt (pro Minute). Rueckgabe: (alt, neu, drift, aktivitaet).

        Die Richtung (steigt/faellt) entscheidet die ECHTE, aktuelle Aktivitaet -
        NICHT das geglaettete EMA. Das war der Bug: das EMA klingt nur langsam ab
        und wird von jeder alten Nachricht nachbefeuert, erreichte also fast nie
        exakt 0. Solange es minimal ueber 0 stand, gab drift_fuer den
        Mindest-Anstieg (MIN_UP, +0,48 %/h) zurueck - der Kurs "stieg" also, obwohl
        seit Stunden niemand da war."""
        st = self._state()
        roh_aktiv = self.activity_of(people, streams, video, msgs_since)
        alt_ema = float(st.get("act_ema", 0.0) or 0.0)
        if roh_aktiv > 0:
            # Jemand ist da -> Kurs steigt. Tempo geglaettet, damit ein kurzer
            # Ausreisser nicht sofort voll durchschlaegt.
            alpha = ACT_ALPHA_UP if roh_aktiv >= alt_ema else ACT_ALPHA_DOWN
            ema = alpha * roh_aktiv + (1 - alpha) * alt_ema
            st["act_ema"] = ema
            st["leer_min"] = 0.0
            basis = self.drift_fuer(ema)
            # Solange jemand da ist, faellt der Kurs NIE - das Rauschen darf einen
            # Anstieg hoechstens abschwaechen, nicht umdrehen.
            drift = max(0.0, basis + random.uniform(-TICK_NOISE, TICK_NOISE))
        else:
            # WIRKLICH leer -> Kurs faellt gestaffelt. EMA HART auf 0 (kein
            # Nachhall mehr), damit auch das Panel "0.0 Punkte" mit fallendem Kurs
            # zeigt und nicht den Mindest-Anstieg.
            ema = 0.0
            st["act_ema"] = 0.0
            st["leer_min"] = float(st.get("leer_min", 0.0) or 0.0) + 1.0
            # Beim SINKEN kein Rauschen - der Verfall soll klar sichtbar sein.
            drift = self.drift_fuer(0.0)
        activity = roh_aktiv
        alt = self.price()
        # Die Aktivitaet bewegt den BASISKURS - der angezeigte Kurs ergibt sich
        # daraus plus den ausgegebenen Anteilen (Kurve bleibt konsistent).
        st["base"] = max(self._base_floor(), self._base() * (1 + drift))
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
        st["pulse_sum"] = summe + staerke
        st["base"] = max(self._base_floor(), self._base() * (1 + staerke))
        neu = self._sync_price()
        log.info("FloCorp Impuls (%s): +%.2f%% -> Kurs %s.", grund, staerke * 100,
                 self._fmt(neu))
        return neu

    async def note_stream_start(self, member=None):
        """Jemand geht LIVE -> Sofort-Impuls nach oben (und Panel/Chart nachziehen)."""
        if not self._enabled:
            return
        if self._puls(PULSE_STREAM, "Livestream an"):
            await self._save()
            await self._refresh_live()

    async def note_voice_join(self, member=None):
        """Jemand kommt in den Call -> kleiner Sofort-Impuls nach oben."""
        if not self._enabled:
            return
        if self._puls(PULSE_JOIN, "Call-Beitritt"):
            await self._save()
            await self._refresh_live()

    async def sample_and_tick(self, guild):
        """Loop-Einstieg (bot.py, alle FLOAKTIE_SAMPLE_SECONDS - Standard 60 s): misst
        die aktuelle Aktivitaet (Call-Leute + Streamer + Kameras + Nachrichten seit
        dem letzten Takt) und bewegt den Kurs SOFORT - viel los -> steigt, wenig ->
        faellt."""
        if not self._enabled or guild is None:
            return
        try:
            st = self._state()
            people, streams, video = self._measure(guild)
            total_msgs = int(st.get("msg_count", 0))
            msgs_since = max(0, total_msgs - int(st.get("last_msg_count", total_msgs)))
            st["last_msg_count"] = total_msgs
            alt, neu, drift, act = self._activity_tick(people, msgs_since, streams, video)
            self._record_tick()
            # Einmal pro Tag den Schlusskurs fuer den Langzeit-Chart festhalten.
            today = self._today()
            if st.get("day") != today:
                st["day"] = today
                st.setdefault("history", []).append({"day": today, "price": self.price()})
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
        bot.py ruft das im Voice-Takt auf."""
        if not self._enabled or guild is None:
            return
        if not self._holdings():
            return
        changed = False
        for vc in getattr(guild, "voice_channels", []):
            if guild.afk_channel and vc.id == guild.afk_channel.id:
                continue
            members = [m for m in vc.members if not getattr(m, "bot", False)]
            if len(members) < 2:
                continue
            for m in members:
                vs = getattr(m, "voice", None)
                if vs is None or getattr(vs, "self_deaf", False) or getattr(vs, "deaf", False):
                    continue
                bonus = self.dividend_for(m.id)
                if bonus > 0:
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
        hold = [(int(u), int(n)) for u, n in self._holdings().items() if int(n) > 0]
        hold.sort(key=lambda x: x[1], reverse=True)
        return hold[:limit]

    # --- Anzeige ----------------------------------------------------------
    def _sparkline(self):
        hist = [h.get("price", 0) for h in self._state().get("history", [])][-16:]
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
        st = self._state()
        now = time.time()
        cutoff = now - max(0.0, float(days)) * 86400
        ticks = [t for t in st.get("ticks", []) if t.get("t", 0) >= cutoff]
        pts = [int(t.get("price", 0)) for t in ticks]
        # Wie weit reichen die Ticks zurueck? Alles davor kommt aus der Historie.
        aeltester = min((t.get("t", now) for t in ticks), default=now)
        tage_offen = max(0.0, (aeltester - cutoff) / 86400.0)
        if tage_offen >= 1.0 or len(pts) < 2:
            hist = [int(h.get("price", 0)) for h in st.get("history", [])]
            n = int(tage_offen) + 1 if pts else max(2, int(float(days)) + 1)
            davor = hist[-n:] if hist and n > 0 else []
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

    def _chart_file(self, days, label):
        """Rendert den Kursverlauf als PNG (discord.File) fuer den Zeitraum."""
        import render
        series = self._series(days)
        chg = ((series[-1] - series[0]) / series[0] * 100) if series[0] else 0.0
        buf = render.floaktie_chart(series, TICKER, f"{NAME} · {label}", chg)
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
            file = self._chart_file(self._chart_days, self._range_label(self._chart_days))
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
        view = KursView(days)
        try:
            file = self._chart_file(days, self._range_label(days))
            view.message = await message.reply(
                file=file, view=view, mention_author=False)
            self._protect(view.message)
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
        pro_min = self.drift_fuer(akt) * 100
        # Lesbarer machen: kleine Werte pro STUNDE, grosse pro Minute.
        if abs(pro_min) >= 0.1:
            tempo = f"**{pro_min:+.2f} %/min**"
        else:
            tempo = f"**{pro_min * 60:+.2f} %/h**"
        if akt <= 0:
            leer = int(float(st.get("leer_min", 0.0) or 0.0))
            if leer >= 60:
                hinweis = (f"Seit {leer // 60} h leer – der Kurs fällt jetzt "
                           f"({self.drift_fuer(0) * 60 * 100:+.0f} %/h). "
                           f"Je länger nichts los ist, desto schneller.")
            else:
                hinweis = ("Niemand da – der Kurs sinkt und wird immer schneller, "
                           "je länger es leer bleibt.")
        elif pro_min <= 0:
            hinweis = ("Kurs liegt weit über seinem Wert – er hält sich, bis mehr "
                       "Aktivität nachkommt.")
        else:
            hinweis = "Je mehr im Call und je mehr Livestreams, desto schneller."
        emb.add_field(
            name="Server-Aktivität",
            value=(f"{akt:.1f} Punkte · {tempo} · "
                   f"Zielkurs **{self._fmt(self.ziel_kurs(akt))}**\n"
                   f"_Jeder im Call zählt 1, jeder Livestream +{STREAM_BONUS:g}, "
                   f"jede Kamera +{VIDEO_BONUS:g}, Chat zählt mit. {hinweis}_"),
            inline=False)
        top = self.top_holder()
        if top:
            emb.add_field(name="👑 Größter Aktionär",
                          value=f"<@{top}> ({self._fmt(self.shares_of(top))} Anteile)", inline=False)
        if member is not None:
            meine = self.shares_of(member.id)
            emb.add_field(
                name="Dein Depot",
                value=(f"{meine} Anteile · Wert **{self._fmt(meine * preis)}** {economy.COIN}\n"
                       f"Dividende: **{self._fmt(self.dividend_for(member.id))}** {economy.COIN}/Voice-Runde"),
                inline=False)
        emb.add_field(
            name="So funktioniert's",
            value=("Kaufen treibt den Kurs, Verkaufen drückt ihn. Sind viele im Voice, "
                   "steigt $FLO über Tage - sonst fällt er.\n"
                   "**Vorteil:** Aktionäre kassieren im Voice eine **Dividende** (mehr "
                   "Anteile = mehr Coins pro Runde), der größte Aktionär die doppelte.\n"
                   "**Risiko:** Du kannst auf **Kredit** kaufen und ins **Minus** gehen – "
                   "fällt der Kurs, sitzt du auf Schulden. Nur Aktien gehen ins Minus!"),
            inline=False)
        emb.set_footer(text=f"{self._bot_name} aktie kauf max · verkauf alles · aktienkurs · top")
        return emb

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
    def _protect(self, msg):
        if msg is None:
            return
        try:
            import bot
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
        try:
            if action == "buy":
                n = instance._resolve_count(interaction.user, str(count))
                if n < 1:
                    await interaction.response.send_message(
                        "Dein Guthaben reicht gerade für keinen ganzen Anteil. 😬 "
                        "(Auf Kredit geht's mit `aktie kauf <anzahl>` – Achtung, Minus!)",
                        ephemeral=True)
                    return
                text = await instance.buy(interaction.user, n)
            else:
                n = instance._resolve_count(interaction.user, str(count), selling=True)
                text = await instance.sell(interaction.user, n)
        except Exception:  # noqa: BLE001
            log.exception("FloCorp-Trade (Button) fehlgeschlagen")
            text = "Beim Handeln ist etwas schiefgelaufen - versuch's gleich nochmal."
        await interaction.response.send_message(text, ephemeral=True)
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
            file = instance._chart_file(days, instance._range_label(days))
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
note_message = instance.note_message
note_stream_start = instance.note_stream_start
note_voice_join = instance.note_voice_join
activity_of = instance.activity_of
drift_fuer = instance.drift_fuer
ziel_base = instance.ziel_base
ziel_kurs = instance.ziel_kurs
sample_and_tick = instance.sample_and_tick
pay_voice_dividends = instance.pay_voice_dividends
price = instance.price
series = instance.series
admin_shares = instance.admin_shares
admin_set_price = instance.admin_set_price
shares_of = instance.shares_of
total_shares = instance.total_shares
