"""Das Monats-Lotto: einmal im Monat wird ausgelost.

Jeden Monat gibt es einen ZUFAELLIGEN Jackpot im Millionen-Bereich. Je groesser
der Jackpot, desto teurer ist ein Los - fest gekoppelt:

    Lospreis = Jackpot / 80      (Beispiel: 20 Mio Jackpot -> 250k pro Los)

Wer mehr Lose kauft, hat proportional groessere Gewinnchance (gewichtete
Ziehung). Am Monatsende wird EIN Gewinner gezogen und bekommt den kompletten
Jackpot gutgeschrieben. Danach startet ein neuer Monat mit neuem Zufalls-Jackpot.

Befehl (nach 'Flo'):
- lotto                  Jackpot, Lospreis, deine Lose & Gewinnchance (mit Kauf-Buttons)
- lotto kauf [anzahl]    Lose kaufen (Standard 1; 'max' = so viele wie möglich)

Alle Coins laufen ueber economy (ein Topf). Der Jackpot wird beim Gewinn frisch
gutgeschrieben; die Loseinnahmen sind der Einsatz (Coin-Senke). Dieses Modul
haelt nur den Lotto-Zustand in data/lotto.json.
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

# --- Balance (per .env justierbar) -------------------------------------------
# Jackpot-Spanne in MILLIONEN (Schrittweite 1 Mio -> Lospreis bleibt glatt).
JACKPOT_MIN_M = int(os.getenv("LOTTO_JACKPOT_MIN_M", "5") or "5")
JACKPOT_MAX_M = int(os.getenv("LOTTO_JACKPOT_MAX_M", "50") or "50")
# Teiler fuer den Lospreis: Lospreis = Jackpot / PRICE_DIVISOR (20M/80 = 250k).
PRICE_DIVISOR = int(os.getenv("LOTTO_PRICE_DIVISOR", "80") or "80")
# Wie viele Lose man auf einen Schlag maximal kaufen kann.
MAX_TICKETS_PER_BUY = int(os.getenv("LOTTO_MAX_TICKETS_PER_BUY", "1000") or "1000")

_FLAVOR = [
    "Wer wagt, gewinnt. Wer nicht spielt, kann nur zusehen. 🍀",
    "Ein Los ist ein Traum auf Papier. 🎟️",
    "Millionen warten - hol sie dir!",
    "Das Glück ist mit den Mutigen (und denen mit vielen Losen).",
]


class Lotto:
    """Objektorientierte Huelle: der Lotto-Zustand lebt auf der Instanz."""

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        self._store = None

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
        self._store = JsonStore("lotto.json", default={
            "month": "", "jackpot": 0, "ticket_price": 0,
            "entries": {}, "history": []})
        # Beim Start sicherstellen, dass ein Monat laeuft (ohne Ziehung).
        if not self._state().get("month"):
            self._start_month()
        self._enabled = True
        st = self._state()
        log.info("Lotto-Feature aktiv (%s: Jackpot %s, Los %s).",
                 st.get("month"), self._fmt(st.get("jackpot", 0)),
                 self._fmt(st.get("ticket_price", 0)))
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

    def _roll_jackpot(self):
        """Zufaelliger Jackpot in Millionen -> (jackpot, lospreis). Lospreis ist
        Jackpot/PRICE_DIVISOR und bleibt glatt (Jackpot ist ein Vielfaches von 1 Mio)."""
        lo = min(JACKPOT_MIN_M, JACKPOT_MAX_M)
        hi = max(JACKPOT_MIN_M, JACKPOT_MAX_M)
        millionen = random.randint(lo, hi)
        jackpot = millionen * 1_000_000
        preis = max(1, jackpot // PRICE_DIVISOR)
        return jackpot, preis

    def _start_month(self):
        """Startet einen frischen Monat mit neuem Zufalls-Jackpot (keine Lose)."""
        st = self._state()
        jackpot, preis = self._roll_jackpot()
        st["month"] = self._month_str()
        st["jackpot"] = jackpot
        st["ticket_price"] = preis
        st["entries"] = {}
        log.info("Lotto-Monat %s gestartet: Jackpot %s, Los %s.",
                 st["month"], self._fmt(jackpot), self._fmt(preis))

    # --- Monats-Ziehung ---------------------------------------------------
    async def tick(self):
        """Von bot.py periodisch aufgerufen. Wechselt der Monat, wird der ALTE
        Monat ausgelost (Gewinner bekommt den Jackpot) und ein neuer gestartet.

        Rueckgabe:
        - None                -> nichts zu tun
        - LottoResult(...)     -> es wurde gezogen (Ansage-Embed + Gewinner-ID)
        """
        if not self._enabled:
            return None
        st = self._state()
        if not st.get("month"):
            self._start_month()
            await self._save()
            return None
        if st["month"] == self._month_str():
            return None  # noch derselbe Monat - nichts auszulosen
        # Monat ist rum -> ziehen.
        result = self._draw()
        self._start_month()
        await self._save()
        try:
            await economy.flush()
        except Exception:  # noqa: BLE001
            pass
        return result

    def _draw(self):
        """Zieht EINEN Gewinner (gewichtet nach Losanzahl), schreibt den Jackpot
        gut und protokolliert das Ergebnis in der Historie. Rueckgabe: LottoResult."""
        st = self._state()
        month = st.get("month", "?")
        jackpot = int(st.get("jackpot", 0))
        entries = self._entries()
        total = self._total_tickets()
        if not entries or total <= 0:
            # Keiner hat gespielt - kein Gewinner.
            hist = {"month": month, "jackpot": jackpot, "winner_id": 0,
                    "winner_name": "", "tickets": 0, "total_tickets": 0}
            st.setdefault("history", []).append(hist)
            st["history"] = st["history"][-24:]
            emb = discord.Embed(
                title="🎰 Monats-Lotto: keine Ziehung",
                description=(f"Im {self._month_label(month)} hat **niemand** ein Los "
                             f"gekauft - der Jackpot von **{self._fmt(jackpot)}** "
                             f"{economy.COIN} verfällt. Nächsten Monat gibt's einen neuen!"),
                color=discord.Color.dark_grey())
            return LottoResult(emb, 0, month)
        # Gewichtete Ziehung.
        uids = list(entries.keys())
        weights = [int(entries[u]) for u in uids]
        winner_uid = random.choices(uids, weights=weights, k=1)[0]
        win_tickets = int(entries[winner_uid])
        winner_name = economy.display_name_of(int(winner_uid)) or "ein Glückspilz"
        try:
            economy.add_coins(int(winner_uid), jackpot, reason="lotto")
        except Exception:  # noqa: BLE001
            log.exception("Jackpot-Gutschrift fehlgeschlagen")
        hist = {"month": month, "jackpot": jackpot, "winner_id": int(winner_uid),
                "winner_name": winner_name, "tickets": win_tickets,
                "total_tickets": total}
        st.setdefault("history", []).append(hist)
        st["history"] = st["history"][-24:]
        chance = win_tickets / total * 100 if total else 0
        emb = discord.Embed(
            title="🎉 Monats-Lotto: der Gewinner steht fest!",
            description=(f"Die Ziehung für den **{self._month_label(month)}** ist durch!\n\n"
                         f"🏆 <@{winner_uid}> gewinnt den Jackpot von "
                         f"**{self._fmt(jackpot)}** {economy.COIN}!"),
            color=discord.Color.gold())
        emb.add_field(name="Gewinner-Lose",
                      value=f"{win_tickets} von {total} ({chance:.1f}%)", inline=True)
        emb.add_field(name="Mitspieler", value=str(len(entries)), inline=True)
        emb.set_footer(text="Neuer Monat, neuer Jackpot - viel Glück!")
        log.info("Lotto %s gezogen: Gewinner %s (%d/%d Lose), Jackpot %d.",
                 month, winner_uid, win_tickets, total, jackpot)
        return LottoResult(emb, int(winner_uid), month)

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
        """Kauft 'count' Lose fuer 'member'. Rueckgabe: Antworttext."""
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
        entries = self._entries()
        entries[str(member.id)] = int(entries.get(str(member.id), 0)) + count
        try:
            await self._save()
            await economy.flush()
        except Exception:  # noqa: BLE001
            log.exception("Speichern nach Los-Kauf fehlgeschlagen")
        meine = entries[str(member.id)]
        total = self._total_tickets()
        chance = meine / total * 100 if total else 0
        return (f"🎟️ Du hast **{count}** Los(e) für **{self._fmt(kosten)}** {economy.COIN} "
                f"gekauft!\nDu hältst jetzt **{meine}** Lose - Gewinnchance aktuell "
                f"**{chance:.1f}%** ({meine}/{total}).")

    # --- Befehl -----------------------------------------------------------
    async def handle(self, message):
        """Erkennt 'lotto [kauf N]' und zeigt das Panel bzw. kauft Lose."""
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
        # 'lotto kauf [n]' / 'lotto los [n]' -> direkt kaufen.
        if len(parts) >= 2 and parts[1].lower() in ("kauf", "kaufen", "buy", "los", "lose"):
            token = parts[2].lower() if len(parts) >= 3 else "1"
            count = self._resolve_count(message.author, token)
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
                         f"**🎟️ Lospreis:** {self._fmt(preis)} {economy.COIN}\n\n"
                         f"*{random.choice(_FLAVOR)}*"),
            color=discord.Color.gold())
        if member is not None:
            meine = int(self._entries().get(str(member.id), 0))
            chance = meine / total * 100 if total else 0
            emb.add_field(name="Deine Lose",
                          value=f"{meine} ({chance:.1f}% Chance)", inline=True)
        emb.add_field(name="Lose gesamt", value=f"{total} · {spieler} Spieler", inline=True)
        emb.add_field(name="Ziehung in", value=f"{self._days_left()} Tag(en)", inline=True)
        # Letzter Gewinner.
        hist = st.get("history", [])
        if hist:
            last = hist[-1]
            if last.get("winner_id"):
                emb.add_field(
                    name="Letzter Gewinner",
                    value=f"<@{last['winner_id']}> – {self._fmt(last.get('jackpot', 0))} "
                          f"{economy.COIN} ({self._month_label(last.get('month', '?'))})",
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
    """Ergebnis von lotto.tick(): eine Monats-Ziehung (Ansage-Embed + Gewinner)."""

    def __init__(self, embed, winner_id, month):
        self.embed = embed
        self.winner_id = winner_id
        self.month = month


# --- Singleton + Modul-API ---------------------------------------------------
instance = Lotto()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
tick = instance.tick
buy = instance.buy
