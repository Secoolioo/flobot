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
HISTORY_TICKS_MAX = 3000

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

# --- Balance (per .env justierbar) -------------------------------------------
START_PRICE = int(os.getenv("FLOAKTIE_START_PRICE", "1000") or "1000")   # Coins/Anteil
MIN_PRICE = int(os.getenv("FLOAKTIE_MIN_PRICE", "50") or "50")
# Liquiditaet: so viele Anteile bewegen den Kurs "voll". Kleiner = volatiler.
LIQUIDITY = int(os.getenv("FLOAKTIE_LIQUIDITY", "750") or "750")
# Maximaler Kurs-Impact EINER Order (Anteil). 0.15 = +/-15 %.
IMPACT_CAP = float(os.getenv("FLOAKTIE_IMPACT_CAP", "0.15") or "0.15")
MAX_SHARES_PER_TRADE = int(os.getenv("FLOAKTIE_MAX_TRADE", "100000") or "100000")

# --- Aktivitaets-Modell: der Kurs reagiert JEDE MINUTE auf die Server-Aktivitaet -
# Bei JEDEM Sample-Takt (bot.py, alle FLOAKTIE_SAMPLE_SECONDS - Standard 60 s) wird
# der Kurs bewegt. Es zaehlen MEHRERE Kriterien:
#
#   Aktivitaet = Leute-im-Call
#              + STREAM_BONUS * Live-Streamer   (Go Live / Screenshare zaehlt extra)
#              + VIDEO_BONUS  * Kameras an
#              + Nachrichten_seit_letztem_Takt / MSG_DIVISOR
#   Drift/Takt = clamp((Aktivitaet_EMA - BASELINE) * TICK_SENS, +/-TICK_CAP) + Rauschen
#
# Viel los -> Kurs (und damit Boersenwert = Anteile*Kurs) STEIGT jede Minute
# sichtbar, wenig los -> er faellt. Beispiel: 12 im Call, 6 davon streamen, reger
# Chat -> deutlich sichtbarer Anstieg pro Minute.
ACT_BASELINE = float(os.getenv("FLOAKTIE_ACT_BASELINE", "3.0") or "3.0")   # "normale" Aktivitaet
TICK_SENS = float(os.getenv("FLOAKTIE_TICK_SENS", "0.0001") or "0.0001")   # Drift je Aktivitaet ueber Baseline (pro Minute)
TICK_CAP = float(os.getenv("FLOAKTIE_TICK_CAP", "0.02") or "0.02")         # max +/-2 % je Takt
ACT_ALPHA = float(os.getenv("FLOAKTIE_ACT_ALPHA", "0.5") or "0.5")         # schnelle Glaettung (reagiert in 1-2 Min)
MSG_DIVISOR = float(os.getenv("FLOAKTIE_MSG_DIVISOR", "4") or "4")         # so viele Nachrichten = 1 "Person"
STREAM_BONUS = float(os.getenv("FLOAKTIE_STREAM_BONUS", "2.0") or "2.0")   # ein Live-Streamer zaehlt so viel extra
VIDEO_BONUS = float(os.getenv("FLOAKTIE_VIDEO_BONUS", "1.0") or "1.0")     # eine Kamera zaehlt so viel extra
TICK_NOISE = float(os.getenv("FLOAKTIE_TICK_NOISE", "0.0012") or "0.0012") # +/-0.12 % Boersen-Rauschen/Takt

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
            "price": START_PRICE, "day": "", "act_ema": ACT_BASELINE,
            "msg_count": 0, "last_msg_count": 0,
            "holdings": {}, "history": [], "ticks": []})
        st = self._state()
        if not st.get("price"):
            st["price"] = START_PRICE
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

    # --- Markt-Impact -----------------------------------------------------
    def _impact(self, shares):
        """Kurs-Impact einer Order dieser Groesse (0..IMPACT_CAP)."""
        if shares <= 0:
            return 0.0
        return min(IMPACT_CAP, shares / LIQUIDITY)

    def _buy_cost(self, shares):
        """Was 'shares' Anteile JETZT kosten (zum bereits angehobenen Kurs)."""
        f = self._impact(shares)
        neu = self.price() * (1 + f)
        return int(round(shares * neu)), int(round(neu))

    def _sell_proceeds(self, shares):
        """Was 'shares' Anteile JETZT einbringen (zum bereits gedrueckten Kurs)."""
        f = self._impact(shares)
        neu = max(MIN_PRICE, self.price() * (1 - f))
        return int(round(shares * neu)), int(round(neu))

    def _max_affordable(self, coins):
        """Wie viele Anteile man sich mit 'coins' sicher leisten kann (inkl. Impact)."""
        p = self.price()
        if p <= 0:
            return 0
        # Konservativ: rechnet mit dem hoechstmoeglichen Impact -> immer bezahlbar.
        return int(coins // (p * (1 + IMPACT_CAP)))

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
        cost, neu = self._buy_cost(count)
        # Aktien auf KREDIT: kein Guthaben-Check - man darf beliebig tief ins Minus
        # (allow_negative). Wie mit Hebel an einer echten Boerse: faellt der Kurs,
        # sitzt du auf den Schulden. Nur die Aktie holt dich da wieder raus.
        economy.add_coins(member.id, -cost, reason="floaktie", allow_negative=True)
        self._holdings()[str(member.id)] = self.shares_of(member.id) + count
        self._state()["price"] = neu   # Kauf hebt den Kurs
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
        proceeds, neu = self._sell_proceeds(count)
        economy.add_coins(member.id, proceeds, reason="floaktie")
        rest = habe - count
        if rest > 0:
            self._holdings()[str(member.id)] = rest
        else:
            self._holdings().pop(str(member.id), None)
        self._state()["price"] = neu   # Verkauf drueckt den Kurs
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

    # --- Aktivitaets-Modell (Kurs folgt der Server-Aktivitaet) -----------
    def note_message(self):
        """Zaehlt eine Server-Nachricht als Aktivitaet (treibt den Kurs mit hoch).
        bot.py ruft das fuer jede Guild-Nachricht auf - nur ein billiger Zaehler,
        gespeichert wird erst beim naechsten Sample-Takt."""
        if not self._enabled:
            return
        st = self._state()
        st["msg_count"] = int(st.get("msg_count", 0)) + 1

    def _measure(self, guild):
        """Misst die aktuelle Voice-Aktivitaet: (Leute, Live-Streamer, Kameras).
        Zaehlt alle Nicht-Bots in Sprachkanaelen (ohne AFK); wer streamt (Go Live)
        oder die Kamera anhat, zaehlt zusaetzlich als Extra-Kriterium."""
        people = streams = video = 0
        for vc in getattr(guild, "voice_channels", []):
            if guild.afk_channel and vc.id == guild.afk_channel.id:
                continue
            for m in vc.members:
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

    def _activity_tick(self, people, msgs_since, streams=0, video=0):
        """EIN Aktivitaets-Takt (pro Minute). Kriterien: Leute im Call + Live-Streamer
        (extra) + Kameras (extra) + Nachrichten. Viel Aktivitaet -> Kurs steigt, wenig
        -> faellt. Rueckgabe: (alt, neu, drift, aktivitaet)."""
        st = self._state()
        activity = (float(max(0, people))
                    + STREAM_BONUS * float(max(0, streams))
                    + VIDEO_BONUS * float(max(0, video))
                    + float(max(0, msgs_since)) / MSG_DIVISOR)
        ema = ACT_ALPHA * activity + (1 - ACT_ALPHA) * float(st.get("act_ema", ACT_BASELINE))
        st["act_ema"] = ema
        raw = (ema - ACT_BASELINE) * TICK_SENS
        raw = max(-TICK_CAP, min(TICK_CAP, raw))
        drift = raw + random.uniform(-TICK_NOISE, TICK_NOISE)
        alt = self.price()
        neu = max(MIN_PRICE, int(round(alt * (1 + drift))))
        st["price"] = neu
        return alt, neu, drift, activity

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
                     "-> Kurs %s->%s (%+.2f%%).", act, people, streams, video, msgs_since,
                     self._fmt(alt), self._fmt(neu), drift * 100)
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
        """Kursaenderung (%) gegenueber dem Schlusskurs vor 'back' Tagen (History)."""
        hist = self._state().get("history", [])
        if len(hist) < 1:
            return 0.0
        ref = hist[-back]["price"] if len(hist) >= back else hist[0]["price"]
        if not ref:
            return 0.0
        return (self.price() - ref) / ref * 100

    # --- Kurs-Chart (Bild + Zeitraum-Buttons) -----------------------------
    def _series(self, days):
        """Kurs-Reihe (alt->neu) fuer den gewuenschten Zeitraum. Nutzt die feinen
        Intraday-Ticks; sind fuer den Zeitraum zu wenige da, faellt es auf die
        Tages-Schlusskurse zurueck. Immer mind. 2 Punkte."""
        st = self._state()
        now = time.time()
        cutoff = now - days * 86400
        pts = [int(t.get("price", 0)) for t in st.get("ticks", [])
               if t.get("t", 0) >= cutoff]
        if len(pts) < 2:
            hist = [int(h.get("price", 0)) for h in st.get("history", [])]
            n = max(2, min(len(hist), int(days) + 1)) if hist else 0
            pts = (hist[-n:] if n else []) + [self.price()]
        pts = [p for p in pts if p] or [self.price()]
        if len(pts) == 1:
            pts = [pts[0], pts[0]]
        return pts

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
                         f"**Heute:** {d1:+.1f}%  ·  **7 Tage:** {d7:+.1f}%"),
            color=discord.Color.green() if d1 >= 0 else discord.Color.red())
        emb.add_field(name="Börsenwert",
                      value=f"{self._fmt(self.total_shares() * preis)} {economy.COIN}", inline=True)
        emb.add_field(name="Aktionäre", value=str(self.holders_count()), inline=True)
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
sample_and_tick = instance.sample_and_tick
pay_voice_dividends = instance.pay_voice_dividends
price = instance.price
