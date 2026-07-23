"""Das Monats-Lotto: einmal im Monat wird ausgelost - Gewinnen ist EXTREM selten.

Jeder Monat hat einen Jackpot im Millionen-Bereich. Der Lospreis ist SANFT an den
Jackpot gekoppelt und gedeckelt, damit Lose bezahlbar bleiben (kein Wucher):

    Lospreis = Jackpot / 2000, gedeckelt auf 1.000 - 50.000
    (Beispiel: 5 Mio -> 2.500, 20 Mio -> 10.000 pro Los)

Anders als ein Verlosungs-Lotto gewinnt hier NICHT garantiert jemand: JEDES Los
hat nur eine winzige, unabhaengige Gewinnchance (Standard 0,02 % pro Los, per
.env justierbar). Die Chance ist bewusst so niedrig, dass ein Los -EV bleibt -
Flos Kasse behaelt den Hausvorteil. Meistens gewinnt NIEMAND - dann rollt der
Jackpot in den naechsten Monat weiter und WAECHST. So wird ein Gewinn extrem
selten, der Jackpot dafuer mit der Zeit riesig.

Die Los-Einnahmen verpuffen NICHT: sie landen auf Flos Konto ("die Kasse") -
nur der Besitzer kann sie sich per 'lotto abbuchen' aufs eigene Konto holen.
Der Jackpot selbst wird beim (seltenen) Gewinn frisch gutgeschrieben.

Befehl (nach 'Flo'):
- lotto                  Jackpot, Lospreis, deine Lose & Gewinnchance (mit Kauf-Buttons)
- lotto kauf [anzahl]    Lose kaufen (Standard 1; 'max' = so viele wie möglich)
- lotto kasse            (nur Besitzer) Stand der Lotto-Kasse
- lotto abbuchen [betr]  (nur Besitzer) Kasse aufs eigene Konto holen ('alles' = komplett)

Dieses Modul haelt den Lotto-Zustand in data/lotto.json.
"""

import calendar
import logging
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import economy
from store import JsonStore

log = logging.getLogger("dcbot.lotto")

# Sentinel: das Lotto hat selbst geantwortet (Panel gesendet) -> bot.py schweigt.
HANDLED = object()

# Befehlswoerter.
_CMDS = ("lotto", "lottery", "jackpot", "lose", "los", "ziehung")

TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
# Besitzer (nur er darf die Kasse abbuchen). Wie in bot.py/admin.py.
OWNER_ID = int(os.getenv("OWNER_ID", "1040135855710404659") or "0")

# --- Balance (per .env justierbar) -------------------------------------------
# Jackpot-Spanne in MILLIONEN (Schrittweite 1 Mio -> Lospreis bleibt glatt).
JACKPOT_MIN_M = int(os.getenv("LOTTO_JACKPOT_MIN_M", "5") or "5")
JACKPOT_MAX_M = int(os.getenv("LOTTO_JACKPOT_MAX_M", "50") or "50")
# Waechst der Jackpot bei einem Monat OHNE Gewinner (Rollover), in MILLIONEN.
GROWTH_MIN_M = int(os.getenv("LOTTO_GROWTH_MIN_M", "1") or "1")
GROWTH_MAX_M = int(os.getenv("LOTTO_GROWTH_MAX_M", "5") or "5")
# Lospreis waechst SANFT mit dem Jackpot: Preis = Jackpot / PRICE_DIVISOR,
# gedeckelt auf [TICKET_MIN, TICKET_MAX] - damit Lose nie Wucher werden.
# Beispiel: 5 Mio -> 2.500, 20 Mio -> 10.000, ab ~100 Mio bei 50.000 gedeckelt.
PRICE_DIVISOR = int(os.getenv("LOTTO_PRICE_DIVISOR", "2000") or "2000")
TICKET_MIN = int(os.getenv("LOTTO_TICKET_MIN", "1000") or "1000")
TICKET_MAX = int(os.getenv("LOTTO_TICKET_MAX", "50000") or "50000")
# Wie viele Lose man auf einen Schlag maximal kaufen kann.
MAX_TICKETS_PER_BUY = int(os.getenv("LOTTO_MAX_TICKETS_PER_BUY", "1000") or "1000")
# EXTREM selten: Gewinnchance PRO LOS (unabhaengig). 0.0002 = 0,02 % je Los.
# Bewusst so niedrig, dass ein Los -EV bleibt (Flos Kasse behaelt den Vorteil):
# 200 Lose/Monat -> ~3,9 % Chance, dass ueeberhaupt jemand gewinnt.
WIN_CHANCE = float(os.getenv("LOTTO_WIN_CHANCE", "0.0002") or "0.0002")

_FLAVOR = [
    "Wer wagt, gewinnt - aber fast nie. Genau das macht den Jackpot fett. 🍀",
    "Ein Los ist ein Traum auf Papier. 🎟️",
    "Millionen warten - der Jackpot rollt, bis ihn jemand knackt.",
    "Extrem selten. Extrem fett. Trau dich.",
]


class Lotto:
    """Objektorientierte Huelle: der Lotto-Zustand lebt auf der Instanz."""

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        self._store = None
        self._win_chance = WIN_CHANCE

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        """Aktiviert das Lotto. Braucht economy (dort liegt der Coin-Topf)."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("LOTTO_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Lotto-Feature aus (LOTTO_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Lotto-Feature aus: economy ist nicht aktiv.")
            return False
        self._win_chance = self._env_float("LOTTO_WIN_CHANCE", WIN_CHANCE)
        self._win_chance = min(1.0, max(0.0, self._win_chance))
        self._store = JsonStore("lotto.json", default={
            "month": "", "jackpot": 0, "ticket_price": 0,
            "entries": {}, "house": 0, "history": []})
        # Beim Start sicherstellen, dass ein Jackpot + Monat laufen (ohne Ziehung).
        st = self._state()
        if not st.get("jackpot"):
            self._new_jackpot()
        if not st.get("month"):
            self._start_month()
        self._enabled = True
        log.info("Lotto-Feature aktiv (%s: Jackpot %s, Los %s, Gewinnchance %.3f%%/Los).",
                 st.get("month"), self._fmt(st.get("jackpot", 0)),
                 self._fmt(st.get("ticket_price", 0)), self._win_chance * 100)
        return True

    def is_enabled(self):
        return self._enabled

    # --- Kleine Helfer ----------------------------------------------------
    def _env_float(self, key, fallback):
        try:
            return float(str(os.getenv(key, "")).strip().replace(",", "."))
        except (TypeError, ValueError):
            return fallback

    def _fmt(self, n):
        return f"{int(n):,}".replace(",", ".")

    def _state(self):
        assert self._store is not None
        return self._store.data

    async def _save(self):
        if self._store is not None:
            await self._store.save()

    def _month_str(self):
        return datetime.now(TIMEZONE).strftime("%Y-%m")

    def _days_left(self):
        """Volle Tage bis zum Monatsende (0 am letzten Tag)."""
        now = datetime.now(TIMEZONE)
        last_day = calendar.monthrange(now.year, now.month)[1]
        return max(0, last_day - now.day)

    def _entries(self):
        return self._state().setdefault("entries", {})

    def _total_tickets(self):
        return sum(int(v) for v in self._entries().values())

    def _win_prob_for(self, tickets):
        """Wahrscheinlichkeit, dass mindestens EINES von 'tickets' Losen gewinnt:
        1 - (1 - p)^tickets."""
        tickets = int(tickets)
        if tickets <= 0 or self._win_chance <= 0:
            return 0.0
        return 1.0 - (1.0 - self._win_chance) ** tickets

    # --- Jackpot-Verwaltung ----------------------------------------------
    def _price_for(self, jackpot):
        """Lospreis fuer einen Jackpot: sanft gekoppelt (Jackpot/PRICE_DIVISOR),
        aber gedeckelt auf [TICKET_MIN, TICKET_MAX] - nie Wucher, nie geschenkt."""
        preis = int(jackpot) // PRICE_DIVISOR
        return max(TICKET_MIN, min(TICKET_MAX, max(1, preis)))

    def _roll_jackpot(self):
        """Frischer Zufalls-Jackpot in Millionen -> (jackpot, lospreis)."""
        lo = min(JACKPOT_MIN_M, JACKPOT_MAX_M)
        hi = max(JACKPOT_MIN_M, JACKPOT_MAX_M)
        millionen = random.randint(lo, hi)
        jackpot = millionen * 1_000_000
        return jackpot, self._price_for(jackpot)

    def _new_jackpot(self):
        """Setzt einen frischen Zufalls-Jackpot (nach einem Gewinn oder beim Start)."""
        st = self._state()
        jackpot, preis = self._roll_jackpot()
        st["jackpot"] = jackpot
        st["ticket_price"] = preis
        return jackpot

    def _grow_jackpot(self):
        """Rollover: hat niemand gewonnen, waechst der Jackpot (und mit ihm der
        Lospreis, gedeckelt) um einen zufaelligen Millionen-Betrag."""
        st = self._state()
        lo = min(GROWTH_MIN_M, GROWTH_MAX_M)
        hi = max(GROWTH_MIN_M, GROWTH_MAX_M)
        st["jackpot"] = int(st.get("jackpot", 0)) + random.randint(lo, hi) * 1_000_000
        st["ticket_price"] = self._price_for(st["jackpot"])
        return st["jackpot"]

    def _start_month(self):
        """Startet einen frischen Monat: setzt den Monat und leert die Lose. Der
        Jackpot bleibt (er wird separat neu gewuerfelt bzw. waechst weiter)."""
        st = self._state()
        st["month"] = self._month_str()
        st["entries"] = {}

    # --- Monats-Ziehung ---------------------------------------------------
    async def tick(self):
        """Von bot.py periodisch aufgerufen. Wechselt der Monat, wird der ALTE
        Monat ausgelost: JEDES Los gewinnt nur mit winziger Chance. Gewinnt jemand,
        gibt's einen frischen Jackpot - sonst rollt der (gewachsene) weiter.

        Rueckgabe:
        - None            -> nichts zu tun
        - LottoResult(...) -> es wurde ausgelost (Ansage-Embed + Gewinner-IDs)
        """
        if not self._enabled:
            return None
        st = self._state()
        if not st.get("jackpot"):
            self._new_jackpot()
        if not st.get("month"):
            self._start_month()
            await self._save()
            return None
        if st["month"] == self._month_str():
            return None  # noch derselbe Monat - nichts auszulosen
        # Monat ist rum -> ziehen.
        result = self._draw()
        if result.won:
            self._new_jackpot()     # frischer Jackpot nach einem Gewinn
        else:
            self._grow_jackpot()    # keiner geknackt -> Rollover, waechst
        self._start_month()
        await self._save()
        try:
            await economy.flush()
        except Exception:  # noqa: BLE001
            pass
        return result

    def _draw(self):
        """Zieht den ablaufenden Monat aus. Jeder Spieler gewinnt nur mit winziger
        Chance (pro Los unabhaengig); gibt es Gewinner, teilen sie sich den Jackpot.
        Schreibt Gutschrift + Historie. Rueckgabe: LottoResult (won=True/False)."""
        st = self._state()
        month = st.get("month", "?")
        jackpot = int(st.get("jackpot", 0))
        entries = self._entries()
        total = self._total_tickets()

        # Jeder Spieler gewinnt, wenn mind. eines seiner Lose zieht (extrem selten).
        winners = []
        for uid, cnt in entries.items():
            if random.random() < self._win_prob_for(cnt):
                winners.append(uid)

        if not winners:
            # Kein Gewinner -> Jackpot rollt weiter (Aufrufer laesst ihn wachsen).
            hist = {"month": month, "jackpot": jackpot, "winner_ids": [],
                    "winner_names": [], "per_winner": 0, "total_tickets": total,
                    "players": len(entries)}
            st.setdefault("history", []).append(hist)
            st["history"] = st["history"][-24:]
            weiter = jackpot  # der aktuelle Jackpot; das Wachstum kommt danach dazu
            emb = discord.Embed(
                title="🎰 Monats-Lotto: kein Gewinner!",
                description=(f"Im {self._month_label(month)} hat **niemand** den Jackpot "
                             f"geknackt. 😤\nDer Jackpot von **{self._fmt(weiter)}** "
                             f"{economy.COIN} **rollt weiter** und wächst - nächsten "
                             f"Monat ist er noch fetter!"),
                color=discord.Color.dark_purple())
            if total:
                emb.set_footer(text=f"{total} Lose von {len(entries)} Spieler(n) - "
                                    f"alle daneben. So selten ist der Jackpot.")
            log.info("Lotto %s: kein Gewinner (%d Lose, %d Spieler) - Rollover.",
                     month, total, len(entries))
            return LottoResult(emb, [], month, won=False)

        # Gewinner teilen sich den Jackpot.
        anteil = jackpot // len(winners)
        namen = []
        for uid in winners:
            name = economy.display_name_of(int(uid)) or "Glückspilz"
            namen.append(name)
            try:
                economy.add_coins(int(uid), anteil, reason="lotto")
            except Exception:  # noqa: BLE001
                log.exception("Jackpot-Gutschrift fehlgeschlagen")
        hist = {"month": month, "jackpot": jackpot,
                "winner_ids": [int(u) for u in winners], "winner_names": namen,
                "per_winner": anteil, "total_tickets": total, "players": len(entries)}
        st.setdefault("history", []).append(hist)
        st["history"] = st["history"][-24:]
        if len(winners) == 1:
            beschreibung = (f"🏆 <@{winners[0]}> hat das Unmögliche geschafft und knackt "
                            f"den Jackpot von **{self._fmt(jackpot)}** {economy.COIN}!")
        else:
            liste = ", ".join(f"<@{u}>" for u in winners)
            beschreibung = (f"🏆 Wahnsinn - **{len(winners)}** Gewinner! {liste} teilen "
                            f"sich **{self._fmt(jackpot)}** {economy.COIN} "
                            f"(je **{self._fmt(anteil)}**)!")
        emb = discord.Embed(
            title="🎉 JACKPOT GEKNACKT!",
            description=(f"Die Ziehung für den **{self._month_label(month)}** ist durch!\n\n"
                         f"{beschreibung}"),
            color=discord.Color.gold())
        emb.add_field(name="Lose im Topf", value=str(total), inline=True)
        emb.add_field(name="Mitspieler", value=str(len(entries)), inline=True)
        emb.set_footer(text="Neuer Monat, frischer Jackpot - viel Glück!")
        log.info("Lotto %s GEKNACKT: %s (%d Lose gesamt), Jackpot %d, je %d.",
                 month, ",".join(str(u) for u in winners), total, jackpot, anteil)
        return LottoResult(emb, [int(u) for u in winners], month, won=True)

    def _month_label(self, month):
        """'2026-07' -> 'Juli 2026' (deutsch)."""
        namen = ["", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
                 "August", "September", "Oktober", "November", "Dezember"]
        try:
            jahr, monat = month.split("-")
            return f"{namen[int(monat)]} {jahr}"
        except Exception:  # noqa: BLE001
            return month

    # --- Los-Kauf ---------------------------------------------------------
    def _resolve_count(self, member, token):
        """Wandelt 'max'/'alles'/Zahl in eine Losanzahl um (nach Guthaben gedeckelt)."""
        preis = int(self._state().get("ticket_price", 0)) or 1
        guthaben = economy.get_coins(member.id)
        max_leistbar = guthaben // preis
        if token in ("max", "alles", "all", "maximum"):
            return int(min(max_leistbar, MAX_TICKETS_PER_BUY))
        try:
            n = int(token)
        except (TypeError, ValueError):
            return 1
        return max(1, min(n, MAX_TICKETS_PER_BUY))

    async def buy(self, member, count):
        """Kauft 'count' Lose fuer 'member'. Der Einsatz wandert in Flos Kasse
        (nicht ins Nichts). Rueckgabe: Antworttext."""
        st = self._state()
        preis = int(st.get("ticket_price", 0))
        if preis <= 0:
            return "Das Lotto ist gerade nicht bereit - versuch's gleich nochmal."
        count = int(count)
        if count < 1:
            return "Kauf mindestens **1** Los. 🎟️"
        kosten = preis * count
        guthaben = economy.get_coins(member.id)
        if guthaben < kosten:
            leistbar = guthaben // preis
            if leistbar <= 0:
                return (f"Ein Los kostet **{self._fmt(preis)}** {economy.COIN} - "
                        f"so viel hast du gerade nicht.")
            return (f"Für **{count}** Lose ({self._fmt(kosten)} {economy.COIN}) reicht's "
                    f"nicht. Du könntest dir **{leistbar}** leisten.")
        economy.add_coins(member.id, -kosten, reason="lotto")
        # Einsatz landet in Flos Kasse (Besitzer kann sie abbuchen).
        st["house"] = int(st.get("house", 0)) + kosten
        entries = self._entries()
        entries[str(member.id)] = int(entries.get(str(member.id), 0)) + count
        try:
            await self._save()
            await economy.flush()
        except Exception:  # noqa: BLE001
            log.exception("Speichern nach Los-Kauf fehlgeschlagen")
        meine = entries[str(member.id)]
        chance = self._win_prob_for(meine) * 100
        return (f"🎟️ Du hast **{count}** Los(e) für **{self._fmt(kosten)}** {economy.COIN} "
                f"gekauft!\nDu hältst jetzt **{meine}** Lose - Gewinnchance **{chance:.2f}%** "
                f"(Jackpot wird nur extrem selten geknackt!).")

    # --- Kasse (nur Besitzer) --------------------------------------------
    async def withdraw(self, member, token):
        """Bucht (einen Teil) der Lotto-Kasse aufs Konto des Besitzers. Nur OWNER."""
        if member.id != OWNER_ID:
            return "🔒 Die Lotto-Kasse gehört dem Chef - da kommst du nicht ran. 😏"
        st = self._state()
        kasse = int(st.get("house", 0))
        if kasse <= 0:
            return "💸 Die Lotto-Kasse ist gerade leer - noch nichts zu holen."
        token = (token or "alles").strip().lower()
        if token in ("alles", "all", "max", "komplett", "gesamt"):
            betrag = kasse
        else:
            betrag = economy.parse_amount(token)
            if betrag is None:
                try:
                    betrag = int(token)
                except (TypeError, ValueError):
                    betrag = kasse
        betrag = max(0, min(int(betrag), kasse))
        if betrag <= 0:
            return f"In der Kasse sind **{self._fmt(kasse)}** {economy.COIN}. Nichts abgebucht."
        economy.add_coins(OWNER_ID, betrag, reason="lotto-kasse")
        st["house"] = kasse - betrag
        try:
            await self._save()
            await economy.flush()
        except Exception:  # noqa: BLE001
            log.exception("Speichern nach Kassen-Abbuchung fehlgeschlagen")
        return (f"🏦 **{self._fmt(betrag)}** {economy.COIN} aus der Lotto-Kasse auf dein "
                f"Konto gebucht. Restkasse: **{self._fmt(st['house'])}** {economy.COIN}.")

    def _kasse_text(self, member):
        """Kassenstand (nur Besitzer bekommt Zahlen zu sehen)."""
        if member.id != OWNER_ID:
            return "🔒 Der Kassenstand geht nur den Chef was an. 😉"
        kasse = int(self._state().get("house", 0))
        return (f"🏦 **Lotto-Kasse:** {self._fmt(kasse)} {economy.COIN}\n"
                f"Abbuchen mit `{self._bot_name} lotto abbuchen alles` "
                f"(oder z. B. `... abbuchen 500k`).")

    # --- Befehl -----------------------------------------------------------
    async def handle(self, message):
        """Erkennt 'lotto [kauf N | kasse | abbuchen X]' und zeigt Panel bzw. handelt."""
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
        # Kasse ansehen / abbuchen (nur Besitzer bekommt echte Zahlen).
        if sub in ("kasse", "tresor", "konto", "house"):
            return self._kasse_text(message.author)
        if sub in ("abbuchen", "auszahlen", "withdraw", "cashout", "entnehmen", "abheben"):
            return await self.withdraw(message.author, arg or "alles")
        # Lose kaufen.
        if sub in ("kauf", "kaufen", "buy", "los", "lose"):
            count = self._resolve_count(message.author, arg or "1")
            return await self.buy(message.author, count)
        # sonst: Panel mit Kauf-Buttons.
        view = LottoView()
        try:
            view.message = await message.reply(
                embed=self._panel_embed(message.author), view=view, mention_author=False)
            self._protect(view.message)
        except (discord.HTTPException, TypeError):
            log.exception("Lotto-Panel konnte nicht gesendet werden")
            return "Das Lotto klemmt gerade - versuch's gleich nochmal."
        return HANDLED

    # --- Panel ------------------------------------------------------------
    def _panel_embed(self, member=None):
        st = self._state()
        jackpot = int(st.get("jackpot", 0))
        preis = int(st.get("ticket_price", 0))
        total = self._total_tickets()
        spieler = len(self._entries())
        emb = discord.Embed(
            title="🎰 Monats-Lotto",
            description=(f"**💰 Jackpot:** {self._fmt(jackpot)} {economy.COIN}\n"
                         f"**🎟️ Lospreis:** {self._fmt(preis)} {economy.COIN}\n"
                         f"**🍀 Chance pro Los:** {self._win_chance * 100:.2f}% "
                         f"(extrem selten!)\n\n*{random.choice(_FLAVOR)}*"),
            color=discord.Color.gold())
        if member is not None:
            meine = int(self._entries().get(str(member.id), 0))
            chance = self._win_prob_for(meine) * 100
            emb.add_field(name="Deine Lose",
                          value=f"{meine} ({chance:.2f}% Chance)", inline=True)
        emb.add_field(name="Lose gesamt", value=f"{total} · {spieler} Spieler", inline=True)
        emb.add_field(name="Ziehung in", value=f"{self._days_left()} Tag(en)", inline=True)
        emb.add_field(
            name="So läuft's",
            value=("Gewinnt niemand, **rollt der Jackpot weiter** und wächst. "
                   "Genau darum wird er selten, aber riesig. 🚀"),
            inline=False)
        # Letzter Ausgang (Gewinner oder Rollover).
        hist = st.get("history", [])
        if hist:
            last = hist[-1]
            if last.get("winner_ids"):
                wer = ", ".join(f"<@{u}>" for u in last["winner_ids"])
                emb.add_field(
                    name="Letzter Jackpot",
                    value=f"{wer} – {self._fmt(last.get('jackpot', 0))} {economy.COIN} "
                          f"({self._month_label(last.get('month', '?'))})",
                    inline=False)
        emb.set_footer(text=f"Kauf per Button oder '{self._bot_name} lotto kauf 5'")
        return emb

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
class _BuyButton(discord.ui.Button):
    def __init__(self, count, label, emoji):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.primary)
        self.count = count

    async def callback(self, interaction):
        await self.view._buy(interaction, self.count)


class _MaxButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Max", emoji="🤑", style=discord.ButtonStyle.success)

    async def callback(self, interaction):
        n = instance._resolve_count(interaction.user, "max")
        if n < 1:
            await interaction.response.send_message(
                "Für ein Los reicht dein Guthaben gerade nicht. 😬", ephemeral=True)
            return
        await self.view._buy(interaction, n)


class LottoView(discord.ui.View):
    """Lotto-Panel: Lose kaufen. Jeder kauft für sich."""

    def __init__(self):
        super().__init__(timeout=None)
        self.message = None
        self.add_item(_BuyButton(1, "1 Los", "🎟️"))
        self.add_item(_BuyButton(5, "5 Lose", "🎟️"))
        self.add_item(_BuyButton(10, "10 Lose", "🎟️"))
        self.add_item(_MaxButton())

    async def _buy(self, interaction, count):
        try:
            text = await instance.buy(interaction.user, count)
        except Exception:  # noqa: BLE001
            log.exception("Los-Kauf fehlgeschlagen")
            text = "Beim Kauf ist etwas schiefgelaufen - versuch's gleich nochmal."
        await interaction.response.send_message(text, ephemeral=True)
        if self.message is not None:
            try:
                await self.message.edit(embed=instance._panel_embed(interaction.user), view=self)
            except discord.HTTPException:
                pass


class LottoResult:
    """Ergebnis von lotto.tick(): eine Monats-Ziehung (Ansage + Gewinner-Liste)."""

    def __init__(self, embed, winner_ids, month, won):
        self.embed = embed
        self.winner_ids = list(winner_ids)
        self.month = month
        self.won = won


# --- Singleton + Modul-API ---------------------------------------------------
instance = Lotto()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
tick = instance.tick
buy = instance.buy
withdraw = instance.withdraw
