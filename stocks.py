"""Aktien-Feature fuer Flo.

``Flo aktie <Symbol/Name>`` -> aktueller Kurs von Yahoo Finance (kostenlos, ohne
API-Key) plus eine freche Kauf/Halten/Verkauf-Empfehlung im Flo-Stil. Die
Empfehlung ist ausdruecklich Spass und KEINE echte Anlageberatung.

Datenquelle: Yahoo Finance (Suche + Chart-Endpoint). Wichtig ist ein
User-Agent-Header, sonst blockt Yahoo mit 429/403.
"""

import asyncio
import logging
import re

import aiohttp
import discord

import ai

log = logging.getLogger("dcbot.stocks")

# bot.py erkennt daran: das Modul hat selbst geantwortet.
HANDLED = object()


class Stocks:
    """Aktienkurs-Abfrage + freche Empfehlung, als Objekt gekapselt."""

    # Erste Worte, die den Befehl ausloesen.
    _COMMANDS = {
        "aktie", "aktien", "stock", "stocks", "kurs",
        "ticker", "boerse", "börse", "share",
    }

    # Yahoo blockt ohne Browser-Header. Deshalb bei JEDEM Abruf mitschicken.
    _HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FloBot/1.0)"}

    _SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
    _CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"

    def setup(self):
        """Aktiviert das Aktien-Feature (braucht nur Internet; KI ist optional)."""
        self._bot_name = ai.bot_name()
        self._enabled = True
        log.info("Aktien-Feature aktiv (Kurse via Yahoo Finance).")
        return self._enabled

    def is_enabled(self):
        return self._enabled

    # --- kleine Helfer -------------------------------------------------------
    def _safe_float(self, val):
        """Macht aus beliebigen Eingaben robust einen float - oder None."""
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def _de_num(self, val, decimals=2):
        """Formatiert eine Zahl deutsch (1.234,56). None -> Gedankenstrich."""
        v = self._safe_float(val)
        if v is None:
            return "—"
        s = f"{v:,.{decimals}f}"
        # US-Format (1,234.56) in deutsches Format (1.234,56) drehen.
        return s.replace(",", "X").replace(".", ",").replace("X", ".")

    def _format_change(self, price, prev):
        """Veraenderung Kurs ggue. Vortag: (absolut, prozent, ist_plus).

        Faengt None/Muell robust ab: dann (None, None, True). ist_plus ist True,
        wenn die Veraenderung >= 0 ist."""
        p = self._safe_float(price)
        pv = self._safe_float(prev)
        if p is None or pv is None:
            return (None, None, True)
        diff = p - pv
        pct = (diff / pv * 100.0) if pv else None
        return (diff, pct, diff >= 0)

    def _looks_like_ticker(self, text):
        """Schaetzt, ob der Text schon ein Ticker ist (AAPL, ^GDAXI, BTC-USD,
        SAP.DE) statt eines Namens (Apple)."""
        t = (text or "").strip()
        if not t or " " in t:
            return False
        if not re.match(r"^\^?[A-Za-z0-9]{1,7}(?:[.\-][A-Za-z0-9]{1,7})?$", t):
            return False
        # Sonderzeichen deuten klar auf einen Ticker hin.
        if t.startswith("^") or "-" in t or "." in t:
            return True
        # Ansonsten nur, wenn komplett gross geschrieben (AAPL, SAP) - 'apple'
        # ist eher ein Name und geht ueber die Suche.
        return t.isupper()

    # --- HTTP ----------------------------------------------------------------
    async def _get_json(self, url, params):
        """GET auf Yahoo mit geteilter Session, Header + Timeout. None bei Fehler."""
        timeout = aiohttp.ClientTimeout(total=12)
        try:
            async with ai.http_session().get(
                url, params=params, headers=self._HEADERS, timeout=timeout,
            ) as r:
                if r.status != 200:
                    log.warning("Yahoo-Antwort %s fuer %s", r.status, url)
                    return None
                # Yahoo schickt teils falschen Content-Type -> Pruefung aus.
                return await r.json(content_type=None)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError,
                ValueError, KeyError, IndexError) as exc:
            log.warning("Yahoo-Abruf fehlgeschlagen (%s): %s", url, exc)
            return None

    async def _search_symbol(self, query):
        """Name -> Ticker ueber die Yahoo-Suche. Nimmt den ersten Treffer."""
        data = await self._get_json(
            self._SEARCH_URL,
            {"q": query, "quotesCount": 6, "newsCount": 0},
        )
        if not data:
            return None
        for quote in data.get("quotes") or []:
            sym = quote.get("symbol")
            if sym:
                return sym
        return None

    async def _fetch_quote(self, symbol):
        """Holt Kurs + Meta + Schlusskurse der letzten ~30 Tage. None bei Fehler."""
        if not symbol:
            return None
        data = await self._get_json(
            self._CHART_URL.format(symbol=symbol),
            {"range": "1mo", "interval": "1d"},
        )
        if not data:
            return None
        try:
            chart = data.get("chart") or {}
            if chart.get("error"):
                return None
            results = chart.get("result") or []
            if not results:
                return None
            res = results[0]
            meta = res.get("meta") or {}
            closes = []
            quote_list = (res.get("indicators") or {}).get("quote") or []
            if quote_list:
                closes = [c for c in (quote_list[0].get("close") or []) if c is not None]
            return {"meta": meta, "closes": closes}
        except (KeyError, IndexError, TypeError):
            return None

    # --- Empfehlung ----------------------------------------------------------
    async def _ai_tip(self, name, sym, price, currency, day_pct, closes, meta):
        """Freche KI-Empfehlung mit den echten Zahlen. None, wenn die KI ausfaellt."""
        vals = [c for c in closes if c is not None]
        month_pct = None
        if len(vals) >= 2:
            _, month_pct, _ = self._format_change(vals[-1], vals[0])
        daten = (
            f"Aktie: {name} ({sym})\n"
            f"Aktueller Kurs: {self._de_num(price)} {currency}\n"
            f"Veraenderung heute: {self._de_num(day_pct)} %\n"
            f"Veraenderung 30 Tage: {self._de_num(month_pct)} %\n"
            f"52-Wochen-Hoch: {self._de_num(meta.get('fiftyTwoWeekHigh'))} {currency}\n"
            f"52-Wochen-Tief: {self._de_num(meta.get('fiftyTwoWeekLow'))} {currency}"
        )
        system = (
            f"Du bist {self._bot_name}, ein freches deutsches Chat-Grossmaul. Antworte "
            "auf Deutsch, locker und schnoddrig, OHNE Emojis. Du bekommst echte "
            "Boersenzahlen und gibst eine unterhaltsame Kurz-Einschaetzung ab - reiner "
            "Spass, du bist KEIN Finanzberater."
        )
        prompt = (
            f"Hier die echten Zahlen:\n{daten}\n\n"
            "Gib in 1-2 frechen Saetzen eine klare Tendenz mit GENAU einem der Worte "
            "KAUFEN, HALTEN oder VERKAUFEN (gross) und einer knappen Begruendung anhand "
            "der Zahlen. Kein Roman, kein Disclaimer (den haenge ich selbst an)."
        )
        txt = await ai.generate(prompt, system=system, temperature=0.9, max_tokens=180)
        if not txt:
            return None
        return f"{txt.strip()}\n\n*Reiner Spass, KEINE echte Finanz- oder Anlageberatung.*"

    def _rule_based_tip(self, price, day_pct, meta):
        """Einfache regelbasierte Tendenz, falls die KI aus ist - anhand Abstand
        zum 52-Wochen-Hoch/-Tief und der Tagesveraenderung."""
        p = self._safe_float(price)
        hi52 = self._safe_float(meta.get("fiftyTwoWeekHigh"))
        lo52 = self._safe_float(meta.get("fiftyTwoWeekLow"))
        dp = self._safe_float(day_pct) or 0.0

        tendenz, grund = "HALTEN", "Laeuft ziemlich seitwaerts, kein Grund zur Aufregung."
        if p is not None and hi52 is not None and lo52 is not None and hi52 > lo52:
            pos = (p - lo52) / (hi52 - lo52)
            if pos >= 0.85:
                tendenz = "VERKAUFEN"
                grund = "Das Ding kratzt am 52-Wochen-Hoch, ganz schoen heissgelaufen."
            elif pos <= 0.2:
                tendenz = "KAUFEN"
                grund = ("Fast am Jahrestief - entweder Schnaeppchen oder fallendes "
                         "Messer, dein Risiko.")
            else:
                tendenz = "HALTEN"
                grund = "Solide in der Mitte des Jahresrange, nix Wildes."
        if dp <= -4:
            grund += f" Heute satte {self._de_num(dp)} % im Minus, aua."
        elif dp >= 4:
            grund += f" Heute {self._de_num(dp)} % im Plus, laeuft ja."
        return f"**{tendenz}** - {grund}\n\n*Reiner Spass, KEINE echte Finanz- oder Anlageberatung.*"

    # --- Embed ---------------------------------------------------------------
    def _month_trend(self, closes, currency):
        """Kleiner 1-Monats-Trend aus den Schlusskursen. None bei zu wenig Daten."""
        vals = [c for c in closes if c is not None]
        if len(vals) < 2:
            return None
        _, pct, plus = self._format_change(vals[-1], vals[0])
        lo, hi = min(vals), max(vals)
        pfeil = "▲" if plus else "▼"
        vz = "+" if plus else "−"
        pct_txt = self._de_num(abs(pct)) if pct is not None else "—"
        return (f"{pfeil} {vz}{pct_txt} % (30T)  ·  Hoch {self._de_num(hi)} / "
                f"Tief {self._de_num(lo)} {currency}".strip())

    async def _build_embed(self, symbol, quote):
        """Baut das Kurs-Embed. None, wenn gar kein Preis ermittelbar ist."""
        meta = quote.get("meta") or {}
        closes = quote.get("closes") or []
        currency = meta.get("currency") or ""
        sym = meta.get("symbol") or symbol
        name = meta.get("longName") or meta.get("shortName") or sym

        price = meta.get("regularMarketPrice")
        if price is None and closes:
            price = closes[-1]
        if self._safe_float(price) is None:
            return None
        prev = meta.get("chartPreviousClose")
        if prev is None:
            prev = meta.get("previousClose")

        diff, pct, ist_plus = self._format_change(price, prev)
        if diff is None:
            color = discord.Color.greyple()
            change_txt = "—"
        else:
            color = discord.Color.green() if ist_plus else discord.Color.red()
            pfeil = "▲" if ist_plus else "▼"
            vz = "+" if ist_plus else "−"
            pct_txt = self._de_num(abs(pct)) if pct is not None else "—"
            change_txt = (f"{pfeil} {vz}{self._de_num(abs(diff))} {currency} "
                          f"({vz}{pct_txt} %)").strip()

        title = f"{name} ({sym})"
        emb = discord.Embed(
            title=title[:256],
            description=f"**{self._de_num(price)} {currency}**".strip(),
            color=color,
        )
        emb.add_field(name="Veränderung heute", value=change_txt, inline=False)

        day_low = meta.get("regularMarketDayLow")
        day_high = meta.get("regularMarketDayHigh")
        if self._safe_float(day_low) is not None or self._safe_float(day_high) is not None:
            emb.add_field(
                name="Tageshoch / -tief",
                value=f"{self._de_num(day_high)} / {self._de_num(day_low)} {currency}".strip(),
            )
        lo52 = meta.get("fiftyTwoWeekLow")
        hi52 = meta.get("fiftyTwoWeekHigh")
        if self._safe_float(lo52) is not None or self._safe_float(hi52) is not None:
            emb.add_field(
                name="52-Wochen-Hoch / -tief",
                value=f"{self._de_num(hi52)} / {self._de_num(lo52)} {currency}".strip(),
            )
        trend = self._month_trend(closes, currency)
        if trend:
            emb.add_field(name="Monats-Trend", value=trend, inline=False)

        # Empfehlung: erst KI, sonst Regel-Fallback.
        tip = None
        if ai.is_enabled():
            tip = await self._ai_tip(name, sym, price, currency, pct, closes, meta)
        if not tip:
            tip = self._rule_based_tip(price, pct, meta)
        emb.add_field(name="🧠 Flos Tipp", value=tip[:1024], inline=False)

        emb.set_footer(text="Kurs via Yahoo Finance · keine Anlageberatung")
        return emb

    # --- Einstieg ------------------------------------------------------------
    async def handle(self, message):
        """Erkennt den Aktien-Befehl. Rueckgabe: Embed/Text (bot.py sendet) oder
        None (kein Aktien-Befehl -> naechstes Modul/KI)."""
        if not self._enabled or message.guild is None:
            return None
        cmd = ai.strip_lead(message.content or "")
        if not cmd:
            return None
        parts = cmd.split(None, 1)
        first = parts[0].lower().strip(",.:!?")
        if first not in self._COMMANDS:
            return None
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            return (f"So: `{self._bot_name} aktie Apple` oder "
                    f"`{self._bot_name} aktie AAPL`")

        # Symbol aufloesen: sieht es wie ein Ticker aus, direkt probieren;
        # sonst (oder wenn der Ticker-Versuch danebengeht) ueber die Suche.
        looks = self._looks_like_ticker(query)
        symbol = query.upper() if looks else await self._search_symbol(query)
        quote = await self._fetch_quote(symbol) if symbol else None
        if quote is None and looks:
            symbol = await self._search_symbol(query)
            quote = await self._fetch_quote(symbol) if symbol else None
        if quote is None:
            return f"Zu '{query}' finde ich keinen Kurs."

        embed = await self._build_embed(symbol, quote)
        if embed is None:
            return f"Zu '{query}' finde ich keinen Kurs."
        return embed


instance = Stocks()

# Modul-Aliase: die Modul-Schnittstelle wie bei food.py/media.py.
setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
# Interne Helfer, die getestet werden sollen:
_format_change = instance._format_change
_looks_like_ticker = instance._looks_like_ticker
_de_num = instance._de_num
_rule_based_tip = instance._rule_based_tip
_month_trend = instance._month_trend
