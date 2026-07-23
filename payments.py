"""Flo Coin-Shop: echtes Geld -> Flo Coins (Stripe Checkout).

ZWECK: Zeit sparen statt stundenlang zu grinden - man kann sich Flo Coins fuer
die Ingame-Spiele kaufen. Die Coins haben KEINEN Auszahlungswert (man kann sie
NICHT wieder in echtes Geld tauschen) - es ist reine Ingame-Waehrung wie Gems in
einem Handyspiel, kein Gluecksspiel mit Geldgewinn.

WIE ES TECHNISCH LAEUFT (bewusst ohne offene Schnittstelle):
- Zahlung laeuft ueber Stripe Checkout (gehostete Bezahlseite von Stripe).
  Dort gehen Apple Pay, Google Pay, Kreditkarte UND PayPal - Apple Pay sogar
  ohne eigene Domain, weil es auf checkout.stripe.com passiert.
- Der Bot fragt Stripe von SICH AUS regelmaessig ab, ob eine Bestellung bezahlt
  ist (Polling) - es ist KEIN Webhook, KEIN offener Port, KEINE Domain noetig,
  nur ausgehendes HTTPS (hat der Server ohnehin).
- Coins werden ERST nach echter Zahlungsbestaetigung von Stripe gutgeschrieben.
  Betraege/Coins sind serverseitig fest (nie vom Client), und jede Bestellung
  wird nur EINMAL gutgeschrieben (Schutz vor Doppel-Gutschrift).

AKTIVIEREN (sonst ist das Feature komplett aus, aendert nichts am Bot):
- STRIPE_SECRET_KEY = dein geheimer Stripe-Key (sk_live_... bzw. sk_test_...).
- optional PAYMENTS_CURRENCY (Standard: eur), PAYMENTS_SUCCESS_URL /
  PAYMENTS_CANCEL_URL (wohin Stripe nach der Zahlung zurueckleitet),
  PAYMENTS_POLL_SECONDS (Abfragetakt), PAYMENTS_ENABLED=0 zum Ausschalten.

RECHTLICHES (bitte SELBST pruefen, kein Rechtsrat): Wer echtes Geld einnimmt,
hat Pflichten (Impressum, Widerruf/AGB, Steuern/USt., Jugendschutz). Stripe/
PayPal haben eigene Regeln zu "Gambling" - auch ein reines Ingame-Casino ohne
Auszahlung kann Rueckfragen ausloesen. Discords Entwickler-/Monetarisierungs-
Richtlinien beachten. Deshalb ist das hier NUR das technische Geruest und
standardmaessig AUS.
"""

import asyncio
import logging
import os
import time

import aiohttp
import discord

import ai
import economy
from store import JsonStore

log = logging.getLogger("dcbot.payments")

STRIPE_API = "https://api.stripe.com/v1"

# Sentinel: payments hat selbst geantwortet (interaktives Menue) -> bot.py schweigt.
HANDLED = object()

# Coin-Pakete: SERVER-SEITIG festgelegt (coins + Preis in Cent). Reihenfolge =
# Anzeige-Reihenfolge. Preise/Coins nie vom Client uebernehmen!
_PACKAGES = {
    "starter": {"coins": 25_000,    "cents": 199,  "label": "Starter", "emoji": "🪙"},
    "bundle":  {"coins": 100_000,   "cents": 499,  "label": "Bundle",  "emoji": "💰"},
    "big":     {"coins": 500_000,   "cents": 1499, "label": "Big Stack", "emoji": "💎"},
    "mega":    {"coins": 2_000_000, "cents": 4999, "label": "Mega",    "emoji": "👑"},
}

# Wie lange eine unbezahlte Bestellung verfolgt wird, bevor der Bot aufgibt.
PENDING_TTL = float(os.getenv("PAYMENTS_PENDING_TTL", "5400"))   # 90 Minuten
# Erledigte/abgelaufene Bestellungen so lange aufheben (fuer Nachvollziehbarkeit).
KEEP_DONE = float(os.getenv("PAYMENTS_KEEP_DONE", "604800"))     # 7 Tage


def _fmt(n):
    """12345 -> '12.345' (deutsche Tausenderpunkte)."""
    return f"{int(n):,}".replace(",", ".")


def _euro(cents):
    """199 -> '1,99 €'."""
    return f"{cents / 100:.2f}".replace(".", ",") + " €"


class Payments:
    """Kapselt den echten Coin-Kauf ueber Stripe (Polling-Modell)."""

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"
        self._secret_key = ""
        self._currency = "eur"
        self._success_url = "https://discord.com/channels/@me"
        self._cancel_url = "https://discord.com/channels/@me"
        self._store = None

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        """Aktiviert den Coin-Shop NUR, wenn ein Stripe-Key da ist und economy
        laeuft. Ohne Key bleibt alles aus (der restliche Bot merkt nichts davon)."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("PAYMENTS_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Coin-Shop aus (PAYMENTS_ENABLED=0).")
            return False
        self._secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
        if not self._secret_key:
            log.info("Coin-Shop aus (kein STRIPE_SECRET_KEY gesetzt).")
            return False
        if not economy.is_enabled():
            log.info("Coin-Shop aus (economy ist aus - kein Coin-Topf).")
            return False
        self._currency = (os.getenv("PAYMENTS_CURRENCY", "eur").strip().lower() or "eur")
        self._success_url = os.getenv("PAYMENTS_SUCCESS_URL", "").strip() or self._success_url
        self._cancel_url = os.getenv("PAYMENTS_CANCEL_URL", "").strip() or self._cancel_url
        self._store = JsonStore("payments.json", default={"orders": {}})
        self._enabled = True
        testmodus = self._secret_key.startswith("sk_test_")
        log.info("Coin-Shop aktiv (%d Pakete, Waehrung %s%s).",
                 len(_PACKAGES), self._currency.upper(),
                 ", TESTMODUS" if testmodus else "")
        return True

    def is_enabled(self):
        return self._enabled

    def packages(self):
        return _PACKAGES

    # --- Speicher ---------------------------------------------------------
    def _orders(self):
        if self._store is None:
            return {}
        return self._store.data.setdefault("orders", {})

    async def _save(self):
        if self._store is not None:
            try:
                await self._store.save()
            except Exception:  # noqa: BLE001 - Speichern darf nie den Bot sprengen
                log.exception("Payments-Store konnte nicht gespeichert werden")

    # --- Stripe-Aufrufe (nur ausgehend, authentifiziert) ------------------
    async def _stripe(self, method, path, fields=None):
        """Ein Stripe-REST-Aufruf (form-encoded). Gibt JSON oder None zurueck."""
        url = f"{STRIPE_API}/{path}"
        headers = {"Authorization": f"Bearer {self._secret_key}"}
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            session = ai.http_session()
            if method == "POST":
                ctx = session.post(url, data=fields, headers=headers, timeout=timeout)
            else:
                ctx = session.get(url, headers=headers, timeout=timeout)
            async with ctx as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    fehler = (data or {}).get("error", {}) if isinstance(data, dict) else {}
                    log.error("Stripe %s %s -> HTTP %s: %s", method, path, r.status,
                              fehler.get("message") or fehler.get("type") or "?")
                    return None
                return data
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("Stripe-Aufruf fehlgeschlagen (%s %s): %s", method, path, exc)
            return None

    # --- Checkout anlegen -------------------------------------------------
    async def create_checkout(self, uid, uname, pkg_key):
        """Legt eine Stripe-Checkout-Session fuer ein Paket an, merkt sie als
        offene Bestellung vor und gibt die Bezahl-URL zurueck (oder None)."""
        pkg = _PACKAGES.get(pkg_key)
        if not pkg or not self._enabled:
            return None
        name = f"{_fmt(pkg['coins'])} Flo Coins ({pkg['label']})"
        fields = [
            ("mode", "payment"),
            ("success_url", self._success_url),
            ("cancel_url", self._cancel_url),
            ("client_reference_id", str(uid)),
            ("metadata[discord_id]", str(uid)),
            ("metadata[coins]", str(pkg["coins"])),
            ("metadata[pkg]", pkg_key),
            ("line_items[0][quantity]", "1"),
            ("line_items[0][price_data][currency]", self._currency),
            ("line_items[0][price_data][unit_amount]", str(pkg["cents"])),
            ("line_items[0][price_data][product_data][name]", name),
            # Apple Pay / Google Pay reiten in Stripe Checkout automatisch auf 'card'.
            ("payment_method_types[0]", "card"),
            ("payment_method_types[1]", "paypal"),
        ]
        data = await self._stripe("POST", "checkout/sessions", fields)
        if not data or not data.get("url") or not data.get("id"):
            return None
        # Coins/Preis SERVERSEITIG in der Bestellung festhalten - beim Gutschreiben
        # zaehlt NUR dieser Wert, nie die (theoretisch manipulierbaren) Metadaten.
        self._orders()[data["id"]] = {
            "uid": int(uid), "name": str(uname), "coins": int(pkg["coins"]),
            "cents": int(pkg["cents"]), "pkg": pkg_key, "status": "pending",
            "created": time.time(),
        }
        await self._save()
        return data["url"]

    # --- Bezahlstatus abfragen (Polling) + gutschreiben -------------------
    async def poll_pending(self):
        """Fragt fuer jede offene Bestellung bei Stripe den Zahlungsstatus ab und
        schreibt bei 'bezahlt' die Coins gut. bot.py ruft das im Takt auf.
        No-op, wenn nichts offen ist (kein unnoetiger Stripe-Traffic)."""
        if not self._enabled or self._store is None:
            return
        orders = self._orders()
        offen = [sid for sid, o in orders.items() if o.get("status") == "pending"]
        geaendert = False
        for sid in offen:
            order = orders.get(sid) or {}
            if time.time() - order.get("created", 0) > PENDING_TTL:
                order["status"] = "expired"     # nach TTL nicht mehr verfolgen
                geaendert = True
                continue
            data = await self._stripe("GET", f"checkout/sessions/{sid}")
            if not data:
                continue
            if data.get("payment_status") == "paid":
                if await self._credit(sid, order):
                    geaendert = True
        if self._prune(orders):
            geaendert = True
        if geaendert:
            await self._save()

    async def _credit(self, sid, order):
        """Schreibt die Coins EINER bezahlten Bestellung gut - genau einmal."""
        if order.get("status") != "pending":
            return False               # schon erledigt -> keine Doppel-Gutschrift
        uid, coins = int(order["uid"]), int(order["coins"])
        try:
            neu = economy.add_coins(uid, coins, reason="kauf")
            await economy.flush()
        except Exception:  # noqa: BLE001
            log.exception("Coin-Gutschrift nach Kauf fehlgeschlagen (uid=%s)", uid)
            return False
        order["status"] = "credited"
        order["credited_at"] = time.time()
        log.info("KAUF gutgeschrieben: %s Coins an uid=%s (Session %s).", coins, uid, sid)
        await self._notify(uid, coins, neu)
        return True

    async def _notify(self, uid, coins, neu):
        """Schickt dem Kaeufer eine Bestaetigung per DM (rein informativ)."""
        try:
            import bot
            user = bot.client.get_user(uid) or await bot.client.fetch_user(uid)
            if user is None:
                return
            emb = discord.Embed(
                title="✅ Zahlung eingegangen – danke!",
                description=(f"Dir wurden **{_fmt(coins)} {economy.COIN}** gutgeschrieben.\n"
                             f"Neuer Kontostand: **{_fmt(neu)} {economy.COIN}**.\n\n"
                             "Viel Spaß beim Zocken! 🎰"),
                color=discord.Color.green())
            await user.send(embed=emb)
        except Exception:  # noqa: BLE001 - DM ist nur nett-to-have
            log.exception("Kauf-Bestaetigung (DM) fehlgeschlagen")

    def _prune(self, orders):
        """Loescht erledigte/abgelaufene Bestellungen nach KEEP_DONE. True = etwas
        entfernt."""
        weg = []
        for sid, o in orders.items():
            if o.get("status") in ("credited", "expired"):
                stamp = o.get("credited_at") or o.get("created", 0)
                if time.time() - stamp > KEEP_DONE:
                    weg.append(sid)
        for sid in weg:
            del orders[sid]
        return bool(weg)

    # --- Befehl -----------------------------------------------------------
    def _is_command(self, cmd):
        """True + Restwort, wenn cmd ein Aufladen-Befehl ist. Kollidiert NICHT mit
        economy 'kaufen' (Titel-Shop): wir hoeren auf 'aufladen'/'echtgeld'/... und
        auf 'coins kaufen/laden/aufladen'."""
        parts = cmd.lower().split()
        if not parts:
            return False
        first = parts[0].strip(".,;:!?")
        if first in ("aufladen", "topup", "echtgeld", "coinshop", "münzenkauf",
                     "muenzenkauf", "coinkauf"):
            return True
        if first in ("coins", "münzen", "muenzen", "coin") and len(parts) > 1 \
                and parts[1] in ("kaufen", "laden", "aufladen", "kauf"):
            return True
        return False

    async def handle(self, message):
        """Erkennt den Aufladen-Befehl und zeigt das Paket-Menue.
        Rueckgabe: HANDLED (Menue gesendet) oder None (kein Aufladen-Befehl)."""
        if not self._enabled or message.guild is None:
            return None
        cmd = ai.strip_lead(message.content or "")
        if not cmd or not self._is_command(cmd):
            return None
        emb = self._shop_embed()
        view = _KaufView(message.author.id)
        try:
            view.message = await message.reply(embed=emb, view=view, mention_author=False)
        except discord.HTTPException:
            log.exception("Coin-Shop-Menue konnte nicht gesendet werden")
        return HANDLED

    def _shop_embed(self):
        """Uebersicht der Pakete + klare Hinweise (kein Auszahlungswert etc.)."""
        emb = discord.Embed(
            title="🪙  Flo Coins aufladen",
            description=("Kein Bock auf stundenlanges Grinden? Lad dir Coins auf und "
                         "spar dir die Zeit. Zahlung per **Apple Pay, Google Pay, "
                         "Kreditkarte oder PayPal** über Stripe.\n"
                         "Wähl unten dein Paket 👇"),
            color=discord.Color.gold())
        for key, p in _PACKAGES.items():
            emb.add_field(
                name=f"{p['emoji']}  {p['label']}",
                value=f"**{_fmt(p['coins'])}** {economy.COIN}\n**{_euro(p['cents'])}**",
                inline=True)
        emb.set_footer(text="Coins sind reine Ingame-Währung ohne Auszahlungswert · "
                            "Zahlung sicher über Stripe")
        return emb

    async def start_checkout(self, interaction, pkg_key):
        """Aus dem Menue heraus: Bezahllink erzeugen und PRIVAT (ephemer) schicken."""
        pkg = _PACKAGES.get(pkg_key)
        if not pkg:
            await interaction.response.send_message("Dieses Paket gibt's nicht.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        url = await self.create_checkout(
            interaction.user.id, interaction.user.display_name, pkg_key)
        if not url:
            await interaction.followup.send(
                "Der Bezahllink ließ sich gerade nicht erstellen - versuch's gleich "
                "nochmal. 🙏", ephemeral=True)
            return
        emb = discord.Embed(
            title=f"{pkg['emoji']}  {pkg['label']} – {_fmt(pkg['coins'])} {economy.COIN}",
            description=(f"**{_euro(pkg['cents'])}** · zahlbar mit Apple Pay, Google Pay, "
                         "Kreditkarte oder PayPal.\n\n"
                         f"👉 **[Hier sicher bezahlen]({url})**\n\n"
                         "Sobald Stripe die Zahlung bestätigt (meist Sekunden), schreibe "
                         "ich dir die Coins automatisch gut und schick dir eine DM."),
            color=discord.Color.gold())
        emb.set_footer(text="Link nur für dich · Coins ohne Auszahlungswert")
        await interaction.followup.send(embed=emb, ephemeral=True)


# --- interaktives Menue ----------------------------------------------------
class _KaufSelect(discord.ui.Select):
    """Dropdown mit den Coin-Paketen."""

    def __init__(self):
        options = []
        for key, p in _PACKAGES.items():
            options.append(discord.SelectOption(
                label=f"{p['label']} – {_fmt(p['coins'])} Coins",
                value=key, emoji=p["emoji"],
                description=f"{_euro(p['cents'])}"))
        super().__init__(placeholder="Paket wählen … 💳", min_values=1, max_values=1,
                         options=options)

    async def callback(self, interaction):
        await instance.start_checkout(interaction, self.values[0])


class _KaufView(discord.ui.View):
    """Menue-View: nur der Aufrufer darf sein eigenes Paket waehlen."""

    def __init__(self, owner_id, *, timeout=180.0):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message = None
        self.add_item(_KaufSelect())

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Mach dir mit `flo aufladen` dein eigenes Menü auf. 🙂", ephemeral=True)
        return False

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# --- Singleton + Modul-API -------------------------------------------------
instance = Payments()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
poll_pending = instance.poll_pending
create_checkout = instance.create_checkout
packages = instance.packages
