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
from datetime import datetime
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

# Befehlswoerter.
_CMDS = ("floaktie", "floaktien", "flostock", "floshare", "flonyse", "$flo", "floboerse")

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

# --- Balance (per .env justierbar) -------------------------------------------
START_PRICE = int(os.getenv("FLOAKTIE_START_PRICE", "1000") or "1000")   # Coins/Anteil
MIN_PRICE = int(os.getenv("FLOAKTIE_MIN_PRICE", "50") or "50")
# Liquiditaet: so viele Anteile bewegen den Kurs "voll". Kleiner = volatiler.
LIQUIDITY = int(os.getenv("FLOAKTIE_LIQUIDITY", "750") or "750")
# Maximaler Kurs-Impact EINER Order (Anteil). 0.15 = +/-15 %.
IMPACT_CAP = float(os.getenv("FLOAKTIE_IMPACT_CAP", "0.15") or "0.15")
MAX_SHARES_PER_TRADE = int(os.getenv("FLOAKTIE_MAX_TRADE", "100000") or "100000")

# Voice-Trend (Tages-Ziehung).
VOICE_BASELINE = float(os.getenv("FLOAKTIE_VOICE_BASELINE", "2.0") or "2.0")  # erwarteter Schnitt
VOICE_SENS = float(os.getenv("FLOAKTIE_VOICE_SENS", "0.02") or "0.02")        # Drift je Person/Tag
DAILY_NOISE = float(os.getenv("FLOAKTIE_DAILY_NOISE", "0.05") or "0.05")      # +/-5 % Rauschen/Tag
EMA_ALPHA = float(os.getenv("FLOAKTIE_EMA_ALPHA", "0.4") or "0.4")            # Mehr-Tages-Glaettung

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
            "price": START_PRICE, "day": "", "day_sum": 0.0, "day_count": 0,
            "voice_ema": VOICE_BASELINE, "holdings": {}, "history": []})
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
        guthaben = economy.get_coins(member.id)
        if guthaben < cost:
            machbar = self._max_affordable(guthaben)
            if machbar <= 0:
                return (f"Ein Anteil {TICKER} kostet gerade **{self._fmt(self.price())}** "
                        f"{economy.COIN} - so viel hast du nicht.")
            return (f"Für **{count}** Anteile ({self._fmt(cost)} {economy.COIN}) reicht's "
                    f"nicht. Du könntest dir **{machbar}** leisten.")
        economy.add_coins(member.id, -cost, reason="floaktie")
        self._holdings()[str(member.id)] = self.shares_of(member.id) + count
        self._state()["price"] = neu   # Kauf hebt den Kurs
        await self._save_all()
        return (f"📈 Gekauft! **{count}** Anteile {TICKER} für **{self._fmt(cost)}** "
                f"{economy.COIN}.\nNeuer Kurs: **{self._fmt(neu)}** {economy.COIN} "
                f"· dein Depot: **{self.shares_of(member.id)}** Anteile.")

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
        await self._save_all()
        return (f"📉 Verkauft! **{count}** Anteile {TICKER} für **{self._fmt(proceeds)}** "
                f"{economy.COIN}.\nNeuer Kurs: **{self._fmt(neu)}** {economy.COIN} "
                f"· dein Depot: **{rest}** Anteile.")

    async def _save_all(self):
        try:
            await self._save()
            await economy.flush()
        except Exception:  # noqa: BLE001
            log.exception("Speichern nach FloCorp-Trade fehlgeschlagen")

    # --- Voice-Trend (mehrtaegig) ----------------------------------------
    def sample_voice(self, count):
        """Merkt sich einen Voice-Zaehlerstand fuer den heutigen Tagesschnitt."""
        st = self._state()
        st["day_sum"] = float(st.get("day_sum", 0.0)) + max(0, int(count))
        st["day_count"] = int(st.get("day_count", 0)) + 1

    def _count_voice(self, guild):
        """Wie viele (Nicht-Bot-)Leute stecken gerade in Sprachkanaelen (ohne AFK)?"""
        total = 0
        for vc in getattr(guild, "voice_channels", []):
            if guild.afk_channel and vc.id == guild.afk_channel.id:
                continue
            total += sum(1 for m in vc.members if not getattr(m, "bot", False))
        return total

    def market_tick(self):
        """Tages-Ziehung des Kurses: gleitender Voice-Schnitt (EMA) treibt die Drift
        (viele im Call -> hoch, wenig -> runter) + etwas Zufalls-Rauschen. Schreibt
        den neuen Tagesschluss in die Historie. Rueckgabe: (alt, neu, drift)."""
        st = self._state()
        cnt = int(st.get("day_count", 0))
        heute_schnitt = (float(st.get("day_sum", 0.0)) / cnt) if cnt else float(st.get("voice_ema", VOICE_BASELINE))
        ema = EMA_ALPHA * heute_schnitt + (1 - EMA_ALPHA) * float(st.get("voice_ema", VOICE_BASELINE))
        st["voice_ema"] = ema
        drift = (ema - VOICE_BASELINE) * VOICE_SENS + random.uniform(-DAILY_NOISE, DAILY_NOISE)
        alt = self.price()
        neu = max(MIN_PRICE, int(round(alt * (1 + drift))))
        st["price"] = neu
        st.setdefault("history", []).append({"day": self._today(), "price": neu})
        st["history"] = st["history"][-HISTORY_MAX:]
        st["day_sum"] = 0.0
        st["day_count"] = 0
        st["day"] = self._today()
        log.info("FloCorp Tageskurs: %s -> %s (Voice-EMA %.2f, Drift %+.2f%%).",
                 self._fmt(alt), self._fmt(neu), ema, drift * 100)
        return alt, neu, drift

    async def tick(self):
        """Von bot.py periodisch aufgerufen. Zieht bei Tageswechsel den Kurs neu."""
        if not self._enabled:
            return None
        st = self._state()
        if not st.get("day"):
            st["day"] = self._today()
            await self._save()
            return None
        if st["day"] == self._today():
            return None
        result = self.market_tick()
        await self._save()
        return result

    async def sample_and_tick(self, guild):
        """Bequemer Loop-Einstieg: zaehlt Voice, sammelt und zieht ggf. den Tageskurs."""
        if not self._enabled or guild is None:
            return
        try:
            self.sample_voice(self._count_voice(guild))
            await self.tick()
            await self._save()
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
                   "Anteile = mehr Coins pro Runde), der größte Aktionär die doppelte."),
            inline=False)
        emb.set_footer(text=f"{self._bot_name} floaktie kauf 10 · verkauf alles · top · depot")
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
        if not parts or parts[0].lower().strip(".,;:!?") not in _CMDS:
            return None
        if not economy.is_enabled():
            return "💤 Gerade gibt's keine Coins - das Economy-System schläft."
        sub = parts[1].lower() if len(parts) >= 2 else ""
        arg = parts[2].lower() if len(parts) >= 3 else ""
        if sub in ("kauf", "kaufen", "buy", "long"):
            return await self.buy(message.author, self._resolve_count(message.author, arg or "1"))
        if sub in ("verkauf", "verkaufen", "sell", "verkaufe", "short", "dump"):
            return await self.sell(message.author, self._resolve_count(message.author, arg or "1", selling=True))
        if sub in ("top", "leaderboard", "rangliste", "aktionäre", "aktionaere"):
            return self._top_embed()
        if sub in ("depot", "portfolio", "anteile", "meins"):
            return self._depot_embed(message.author)
        # sonst: Panel mit Buttons.
        view = FloAktieView()
        try:
            view.message = await message.reply(
                embed=self._panel_embed(message.author), view=view, mention_author=False)
            self._protect(view.message)
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
    def __init__(self, label, emoji, style, action, count):
        super().__init__(label=label, emoji=emoji, style=style)
        self.action = action     # "buy" | "sell"
        self.count = count       # int oder "alles"

    async def callback(self, interaction):
        await self.view._trade(interaction, self.action, self.count)


class _InfoButton(discord.ui.Button):
    def __init__(self, label, emoji, kind):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=1)
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
        self.add_item(_TradeButton("Kauf 1", "📈", discord.ButtonStyle.success, "buy", 1))
        self.add_item(_TradeButton("Kauf 10", "📈", discord.ButtonStyle.success, "buy", 10))
        self.add_item(_TradeButton("Verkauf 1", "📉", discord.ButtonStyle.danger, "sell", 1))
        self.add_item(_TradeButton("Verkauf alles", "💸", discord.ButtonStyle.danger, "sell", "alles"))
        self.add_item(_InfoButton("Depot", "💼", "depot"))
        self.add_item(_InfoButton("Top", "🏆", "top"))

    async def _trade(self, interaction, action, count):
        try:
            if action == "buy":
                n = instance._resolve_count(interaction.user, str(count))
                text = await instance.buy(interaction.user, n)
            else:
                n = instance._resolve_count(interaction.user, str(count), selling=True)
                text = await instance.sell(interaction.user, n)
        except Exception:  # noqa: BLE001
            log.exception("FloCorp-Trade (Button) fehlgeschlagen")
            text = "Beim Handeln ist etwas schiefgelaufen - versuch's gleich nochmal."
        await interaction.response.send_message(text, ephemeral=True)
        if self.message is not None:
            try:
                await self.message.edit(embed=instance._panel_embed(interaction.user), view=self)
            except discord.HTTPException:
                pass


# --- Singleton + Modul-API ---------------------------------------------------
instance = FloAktie()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
tick = instance.tick
sample_and_tick = instance.sample_and_tick
pay_voice_dividends = instance.pay_voice_dividends
price = instance.price
