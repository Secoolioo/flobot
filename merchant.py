"""Der fahrende Haendler: Flos Wander-Kraemer (Minecraft-Style).

Einmal am Tag - zu einer ZUFAELLIGEN Uhrzeit - taucht der fahrende Haendler im
Server auf, bleibt ein paar Stunden und zieht dann weiter. Waehrend er da ist,
verkauft er EXKLUSIVE Raenge/Titel (die es im normalen Tages-Shop NICHT gibt) zu
fairen Sonderpreisen - und er TAUSCHT: du gibst einen Titel her (Mindest-
Seltenheit) plus eine kleine Aufzahlung und bekommst dafuer einen krasseren.

Befehl (nach 'Flo'):
- haendler / merchant / kraemer   Zeigt den Stand des Haendlers (nur waehrend er
                                  da ist), sonst wann er das naechste Mal kommt.

Kaufen & Tauschen laeuft ueber die Buttons/Dropdowns am Haendler-Panel.

Alle Coins/Titel laufen ueber economy (ein Topf, ein Inventar). Dieses Modul
haelt nur den Haendler-Zustand in data/merchant.json:
- wann er heute kommt / bis wann er bleibt
- welche 3 Titel er heute im Angebot hat + seine Tausch-Deals
- wer welchen (limitierten) Titel schon gekauft hat

Design: Die Titel des Haendlers sind HANDVERLESEN (eigener Pool, IDs mit Praefix
'haendler:'), damit sie sich nie mit dem deterministischen Tages-Shop ueber-
schneiden. Die Seltenheit wird beim Kauf explizit ins Inventar geschrieben -
economy vergibt daraufhin die passende Farb-Rolle.
"""

import logging
import os
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import economy
import titles
from basis import FeatureBasis
from store import JsonStore

log = logging.getLogger("dcbot.merchant")

# Sentinel: der Haendler hat selbst geantwortet (Panel gesendet) -> bot.py schweigt.
HANDLED = object()

# Befehlswoerter, auf die der Haendler hoert.
_CMDS = ("haendler", "händler", "merchant", "kraemer", "krämer",
         "wanderhaendler", "wanderhändler", "trader", "kramer")

# --- Zeitfenster (per .env justierbar) --------------------------------------
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))
# Frühester / spätester Zeitpunkt (Ortszeit-Stunde), zu dem er auftauchen kann.
APPEAR_EARLIEST_HOUR = 9
APPEAR_LATEST_HOUR = 22
# Wie lange er bleibt, bevor er weiterzieht (Stunden). Standard: 1 STUNDE - danach
# ist er weg und man muss auf morgen warten.
PRESENT_HOURS = float(os.getenv("MERCHANT_PRESENT_HOURS", "1") or "1")

# --- Der exklusive Titel-Katalog des Haendlers ------------------------------
# BEWUSST krasser als der normale Flo-Shop: KEIN 'normal'/'selten'-Kram, nur
# mythisch, legendaer und die brandneue Stufe EXKLUSIV (🔱) - die es im Shop
# NIEMALS gibt. price = fairer Sonderpreis: JEDER Titel hier ist billiger als
# dieselbe Stufe im Tages-Shop (titles.py RARITY) - dafuer ist er nur eine
# Stunde am Tag zu haben. Preise wie ueberall aus dem Tageseinkommen abgeleitet. Jeder Titel hat eine feste Seltenheit
# (economy schreibt sie beim Kauf ins Inventar -> passende Farb-Rolle).
_KATALOG = [
    # --- GOETTLICH (✨) - das absolute Maximum, gibt es NUR hier und selten ---
    {"id": "haendler:allvater", "text": "Allvater",
     "label": "☀️ Allvater", "rarity": "goettlich", "price": 78_000_000},
    {"id": "haendler:schoepfer", "text": "Der Schöpfer",
     "label": "♾️ Der Schöpfer", "rarity": "goettlich", "price": 64_000_000},
    {"id": "haendler:lichtbringer", "text": "Lichtbringer",
     "label": "💫 Lichtbringer", "rarity": "goettlich", "price": 52_000_000},
    # --- EXKLUSIV (🔱) - hoeher als Legendaer, nur beim Haendler ---
    {"id": "haendler:weltenherrscher", "text": "Weltenherrscher",
     "label": "🔱 Weltenherrscher", "rarity": "exklusiv", "price": 12_500_000},
    {"id": "haendler:gottkaiser", "text": "Gottkaiser",
     "label": "👑 Gottkaiser", "rarity": "exklusiv", "price": 11_000_000},
    {"id": "haendler:der_eine", "text": "Der Auserwählte",
     "label": "✴️ Der Auserwählte", "rarity": "exklusiv", "price": 7_200_000},
    {"id": "haendler:sternengott", "text": "Sternengott",
     "label": "🌌 Sternengott", "rarity": "exklusiv", "price": 8_400_000},
    {"id": "haendler:ewiger", "text": "Der Ewige",
     "label": "♾️ Der Ewige", "rarity": "exklusiv", "price": 13_800_000},
    {"id": "haendler:drachenkoenig", "text": "Drachenkönig",
     "label": "🐲 Drachenkönig", "rarity": "exklusiv", "price": 9_600_000},
    # --- Relikt (🟠) - im Shop extrem selten, hier verlaesslich ---
    {"id": "haendler:aschebewahrer", "text": "Aschebewahrer",
     "label": "🏺 Aschebewahrer", "rarity": "relikt", "price": 2_100_000},
    {"id": "haendler:zeitwaechter", "text": "Zeitwächter",
     "label": "⏳ Zeitwächter", "rarity": "relikt", "price": 2_600_000},
    {"id": "haendler:steintafel", "text": "Träger der Steintafel",
     "label": "📜 Träger der Steintafel", "rarity": "relikt", "price": 1_800_000},
    # --- Legendär (🟡 gold) - die Kronjuwelen ---
    {"id": "haendler:drachenlord", "text": "Drachenlord",
     "label": "🐉 Drachenlord", "rarity": "legendary", "price": 520_000},
    {"id": "haendler:schattenkaiser", "text": "Schattenkaiser",
     "label": "🌑 Schattenkaiser", "rarity": "legendary", "price": 460_000},
    {"id": "haendler:weltenbrenner", "text": "Weltenbrenner",
     "label": "🔥 Weltenbrenner", "rarity": "legendary", "price": 580_000},
    {"id": "haendler:goetterbote", "text": "Götterbote",
     "label": "🪽 Götterbote", "rarity": "legendary", "price": 410_000},
    {"id": "haendler:unsterblich", "text": "Der Unsterbliche",
     "label": "💀 Der Unsterbliche", "rarity": "legendary", "price": 640_000},
    {"id": "haendler:sturmbaendiger", "text": "Sturmbändiger",
     "label": "⚡ Sturmbändiger", "rarity": "legendary", "price": 350_000},
    # --- Episch (🟪) - der guenstigere Einstieg beim Haendler ---
    {"id": "haendler:klingentaenzer", "text": "Klingentänzer",
     "label": "🗡️ Klingentänzer", "rarity": "episch", "price": 26_000},
    {"id": "haendler:maskenspieler", "text": "Maskenspieler",
     "label": "🎭 Maskenspieler", "rarity": "episch", "price": 19_000},
    {"id": "haendler:runenmeister", "text": "Runenmeister",
     "label": "⚜️ Runenmeister", "rarity": "episch", "price": 31_000},
    # --- Mythisch (🟣) - solide Mittelklasse ---
    {"id": "haendler:sternenjaeger", "text": "Sternenjäger",
     "label": "🌠 Sternenjäger", "rarity": "mythisch", "price": 92_000},
    {"id": "haendler:nebelwandler", "text": "Nebelwandler",
     "label": "🌫️ Nebelwandler", "rarity": "mythisch", "price": 68_000},
    {"id": "haendler:frostfuerst", "text": "Frostfürst",
     "label": "❄️ Frostfürst", "rarity": "mythisch", "price": 55_000},
    {"id": "haendler:phoenix", "text": "Phönix",
     "label": "🦅 Phönix", "rarity": "mythisch", "price": 118_000},
]
_KATALOG_BY_ID = {e["id"]: e for e in _KATALOG}

# --- Freche Begrüßungs-/Abschieds-Sprüche (random.choice) --------------------
_ARRIVE_LINES = [
    "Ein Karren rumpelt heran… **der fahrende Händler** hat seinen Stand aufgeschlagen! 🛒",
    "🔔 Glöckchen-Klingeln! **Der fahrende Händler** ist da - und nur **{dauer}**!",
    "Aus dem Nebel tritt **der fahrende Händler** mit prall gefüllter Truhe. 🧳",
    "**Der fahrende Händler** ist eingetroffen! Ware, die's im Shop NIE gibt - **{dauer}**. ⏳",
    "Hört, hört! **Der fahrende Händler** packt Schätze aus, die kein Shop führt. 💼",
]
_LEAVE_LINES = [
    "Der Händler packt zusammen und zieht weiter. Bis zum nächsten Mal! 🐫",
    "🌙 Der Stand ist leer - **der fahrende Händler** ist weitergezogen.",
    "Weg ist er. Wer nicht zugeschlagen hat, muss auf morgen warten. 👋",
    "Der Karren rollt davon - **der fahrende Händler** verschwindet im Nebel. 🌫️",
]
_HAGGLE = [
    "Nur diese eine Stunde! Solche Ware siehst du so schnell nicht wieder.",
    "Handverlesen und KRASSER als alles im Shop - im Laden kriegst du das nie.",
    "Kauf jetzt oder ärgere dich morgen. In einer Stunde bin ich weg.",
    "Titel, die kein normaler Shop führt - bis hoch zu EXKLUSIV. 🔱",
]


class Merchant(FeatureBasis):
    """Objektorientierte Huelle: der Haendler-Zustand lebt auf der Instanz."""

    def __init__(self):
        self._enabled = False
        # Zuletzt geschuetztes Panel je Slot - damit das neue das alte
        # beim Auto-Loesch-Schutz wieder abmeldet.
        self._geschuetzt = {}
        self._store = None
        self._present_secs = int(PRESENT_HOURS * 3600)

    def _dauer_text(self):
        """Verweildauer als Text ('1 Stunde', '90 Minuten') - immer aus dem
        WIRKLICHEN Wert, nicht hart in die Sprueche geschrieben."""
        secs = max(60, int(self._present_secs))
        if secs % 3600 == 0:
            n = secs // 3600
            return f"{n} Stunde" if n == 1 else f"{n} Stunden"
        n = max(1, secs // 60)
        return f"{n} Minute" if n == 1 else f"{n} Minuten"

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        """Aktiviert den Haendler. Braucht economy (Coins + Titel-Inventar)."""
        if os.getenv("MERCHANT_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Haendler-Feature aus (MERCHANT_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Haendler-Feature aus: economy ist nicht aktiv.")
            return False
        # EINE Quelle fuer die Verweildauer: setup() nahm frueher 3 Stunden als
        # Standard, die Konstante daneben 1 Stunde - je nachdem, wen man fragte,
        # war der Haendler unterschiedlich lange da.
        self._present_secs = max(60, int(PRESENT_HOURS * 3600))
        self._store = JsonStore("merchant.json", default={
            "day": "", "appear_at": 0, "depart_at": 0,
            "arrived": False, "departed": False,
            "stock": [], "trades": [], "sold": {}})
        self._enabled = True
        log.info("Haendler-Feature aktiv (bleibt %d Min pro Besuch).",
                 self._present_secs // 60)
        return True

    def is_enabled(self):
        return self._enabled

    # --- Kleine Helfer ----------------------------------------------------
    def _fmt(self, n):
        """1500 -> '1.500' (deutsche Tausenderpunkte)."""
        return f"{int(n):,}".replace(",", ".")

    def _state(self):
        assert self._store is not None
        return self._store.data

    async def _save(self):
        if self._store is not None:
            await self._store.save()

    def _now(self):
        return time.time()

    def _today_str(self):
        return datetime.now(TIMEZONE).strftime("%Y-%m-%d")

    def _midnight_epoch(self):
        """Epoch-Zeit von 'heute 00:00' in der Server-Zeitzone."""
        now = datetime.now(TIMEZONE)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()

    def is_present(self):
        """True, wenn der Haendler GERADE am Stand ist (angekommen, nicht weg)."""
        if not self._enabled:
            return False
        st = self._state()
        return bool(st.get("arrived") and not st.get("departed"))

    def appear_time_str(self):
        """Uhrzeit, zu der er heute (an)kommt bzw. kam, als 'HH:MM'."""
        st = self._state()
        ts = st.get("appear_at", 0)
        if not ts:
            return "?"
        return datetime.fromtimestamp(ts, TIMEZONE).strftime("%H:%M")

    # --- Tages-Rhythmus ---------------------------------------------------
    def _ensure_day(self):
        """Bei einem neuen Kalendertag den Besuch neu planen (zufaellige Uhrzeit).
        Rueckgabe: True, wenn geplant/zurueckgesetzt wurde (Aufrufer speichert)."""
        st = self._state()
        today = self._today_str()
        if st.get("day") == today:
            return False
        earliest = int(self._midnight_epoch()) + APPEAR_EARLIEST_HOUR * 3600
        latest = int(self._midnight_epoch()) + APPEAR_LATEST_HOUR * 3600
        appear_at = random.randint(earliest, max(earliest, latest))
        st.update({"day": today, "appear_at": appear_at, "depart_at": 0,
                   "arrived": False, "departed": False,
                   "stock": [], "trades": [], "sold": {}})
        log.info("Haendler fuer %s eingeplant: %s Uhr.",
                 today, datetime.fromtimestamp(appear_at, TIMEZONE).strftime("%H:%M"))
        return True

    # Was als "Highlight" gilt (davon ist IMMER mindestens eines im Angebot) und
    # wie stark die Stufen gezogen werden. Goettlich ist bewusst ganz selten -
    # der Haendler hat es nur an ungefaehr jedem zwanzigsten Tag ueberhaupt dabei.
    _HIGHLIGHTS = ("legendary", "relikt", "exklusiv", "goettlich")
    _STOCK_GEWICHT = {"episch": 5, "mythisch": 5, "legendary": 4,
                      "relikt": 3, "exklusiv": 2, "goettlich": 1}

    def _roll_stock(self):
        """Wuerfelt 3 Verkaufs-Titel (episch bis GOETTLICH - krasser als der Shop)
        und 1-2 Tausch-Deals. Es ist IMMER mindestens ein Highlight dabei
        (legendaer/Relikt/exklusiv/goettlich). Schreibt beides in den Zustand."""
        st = self._state()
        # Verkauf: gewichtet Richtung 'krass'. Episch/Mythisch als bezahlbarer
        # Einstieg, dazu fast immer ein Brocken und mit Glueck das Top-Zeug.
        gewicht = self._STOCK_GEWICHT
        pool = list(_KATALOG)
        gewichte = [gewicht.get(e["rarity"], 1) for e in pool]
        stock = []
        gewaehlt_ids = set()
        versuche = 0
        while len(stock) < 3 and versuche < 80:
            versuche += 1
            e = random.choices(pool, weights=gewichte, k=1)[0]
            if e["id"] in gewaehlt_ids:
                continue
            gewaehlt_ids.add(e["id"])
            eintrag = dict(e)
            # Manche Angebote sind limitiert (Minecraft-Flair: "nur X Stück!").
            eintrag["limit"] = random.choice([0, 0, 1, 2, 3])  # 0 = unbegrenzt
            stock.append(eintrag)
        # Garantie: mindestens EIN Highlight (legendaer/exklusiv) im Angebot.
        if not any(e["rarity"] in self._HIGHLIGHTS for e in stock):
            highlights = [e for e in _KATALOG
                          if e["rarity"] in self._HIGHLIGHTS
                          and e["id"] not in gewaehlt_ids]
            if highlights and stock:
                e = dict(random.choice(highlights))
                e["limit"] = random.choice([0, 1, 2])
                gewaehlt_ids.discard(stock[-1]["id"])
                gewaehlt_ids.add(e["id"])
                stock[-1] = e

        # Tausch-Deals: gib einen Titel her (Mindest-Seltenheit) + Aufzahlung ->
        # bekomm einen krasseren. Belohnung != bereits im Verkauf (mehr Abwechslung).
        trades = []
        rewards = [e for e in _KATALOG
                   if e["rarity"] in self._HIGHLIGHTS
                   and e["id"] not in gewaehlt_ids]
        random.shuffle(rewards)
        anzahl_trades = random.choice([1, 2])
        for reward in rewards[:anzahl_trades]:
            # Einsatz ist immer die Stufe DIREKT darunter, die Aufzahlung ein
            # Bruchteil des Preisunterschieds - eine Stelle statt einer if-Kette,
            # damit neue Stufen automatisch mitspielen.
            rang = titles.RANK.get(reward["rarity"], 1)
            need = next((r for r in titles.RARITY_ORDER
                         if titles.RANK.get(r, 0) == max(0, rang - 1)),
                        "mythisch")
            basis = int(reward["price"] * random.choice([0.18, 0.22, 0.26, 0.30]))
            aufzahlung = max(1000, int(round(basis, -3)))
            trades.append({
                "id": "trade:" + reward["id"],
                "need_rarity": need,
                "surcharge": aufzahlung,
                "reward_id": reward["id"],
                "reward_text": reward["text"],
                "reward_label": reward["label"],
                "reward_rarity": reward["rarity"],
            })

        st["stock"] = stock
        st["trades"] = trades
        st["sold"] = {}
        log.info("Haendler-Angebot gewuerfelt: %d Titel, %d Tausch-Deals.",
                 len(stock), len(trades))

    async def tick(self):
        """Wird von bot.py periodisch aufgerufen. Plant den Tag, laesst den
        Haendler zur geplanten Zeit ankommen bzw. spaeter weiterziehen.

        Rueckgabe:
        - None                 -> nichts zu tun
        - TickResult('arrive') -> Haendler kam gerade an (embed + view zum Posten)
        - TickResult('leave')  -> Haendler ist gerade weitergezogen (embed)
        """
        if not self._enabled:
            return None
        changed = self._ensure_day()
        st = self._state()
        now = self._now()
        result = None
        if not st.get("arrived") and now >= st.get("appear_at", 0):
            # Ankunft: Angebot wuerfeln, Abreisezeit setzen.
            self._roll_stock()
            st["arrived"] = True
            st["departed"] = False
            st["depart_at"] = now + self._present_secs
            changed = True
            result = TickResult("arrive", self._panel_embed(), self.build_view())
        elif st.get("arrived") and not st.get("departed") and now >= st.get("depart_at", 0):
            st["departed"] = True
            changed = True
            result = TickResult("leave", self._leave_embed(), None)
        if changed:
            await self._save()
        return result

    # --- Panels / Embeds --------------------------------------------------
    def build_view(self):
        """Baut die interaktive Haendler-Ansicht (Kaufen-/Tausch-Dropdown)."""
        return HaendlerView()

    def _panel_embed(self):
        """Der Haendler-Stand als Embed: Begrueßung + Angebot + Tausch-Deals."""
        st = self._state()
        emb = discord.Embed(
            title="🛒 Der fahrende Händler",
            description=(random.choice(_ARRIVE_LINES).format(dauer=self._dauer_text())
                         + f"\n*{random.choice(_HAGGLE)}*"),
            color=discord.Color.dark_gold())
        # Verkaufs-Angebot.
        zeilen = []
        for e in st.get("stock", []):
            meta = titles.RARITY.get(e["rarity"], titles.RARITY["normal"])
            rest = self._rest_text(e)
            zeilen.append(
                f"{meta['emoji']} **{e['label']}** · {meta['label']}\n"
                f"┗ {self._fmt(e['price'])} {economy.COIN}{rest}")
        if zeilen:
            emb.add_field(name="🧾 Im Angebot", value="\n".join(zeilen), inline=False)
        # Tausch-Deals.
        tzeilen = []
        for t in st.get("trades", []):
            need_meta = titles.RARITY.get(t["need_rarity"], titles.RARITY["normal"])
            rew_meta = titles.RARITY.get(t["reward_rarity"], titles.RARITY["normal"])
            tzeilen.append(
                f"Gib **{need_meta['emoji']} {need_meta['label']}**-Titel + "
                f"{self._fmt(t['surcharge'])} {economy.COIN}\n"
                f"┗ bekomm **{t['reward_label']}** ({rew_meta['label']})")
        if tzeilen:
            emb.add_field(name="🔄 Tausch-Deals", value="\n\n".join(tzeilen), inline=False)
        weg = max(0, int(st.get("depart_at", 0) - self._now()))
        emb.set_footer(text=f"⏳ Zieht in ~{max(1, weg // 60)} Min weiter · "
                            f"kaufen & tauschen unten per Menü")
        return emb

    def _rest_text(self, e):
        """' · nur noch X von Y!' fuer limitierte Angebote, sonst ''."""
        limit = e.get("limit", 0)
        if not limit:
            return ""
        verkauft = len(self._state().get("sold", {}).get(e["id"], []))
        rest = max(0, limit - verkauft)
        return f"  ·  🔥 nur noch **{rest}** von {limit}!" if rest else "  ·  ❌ ausverkauft"

    def _leave_embed(self):
        emb = discord.Embed(
            title="🐫 Der Händler ist weg",
            description=random.choice(_LEAVE_LINES),
            color=discord.Color.dark_grey())
        emb.set_footer(text="Morgen kommt er zu einer anderen Zeit wieder.")
        return emb

    def _closed_text(self):
        """Antwort, wenn jemand den Haendler ruft, er aber nicht da ist."""
        st = self._state()
        if st.get("arrived") and st.get("departed"):
            return ("🐫 Der fahrende Händler ist heute schon wieder weitergezogen. "
                    "Morgen kommt er zu einer anderen Zeit wieder!")
        # Kommt heute noch.
        return (f"🛒 Der fahrende Händler ist gerade unterwegs und noch nicht am "
                f"Stand. Halt heute die Augen offen - er taucht **irgendwann** auf. 👀")

    # --- Befehl -----------------------------------------------------------
    async def handle(self, message):
        """Erkennt 'haendler'/'merchant'/... und zeigt den Stand (nur wenn er da
        ist). Sendet das Panel selbst und gibt HANDLED zurueck; sonst None/str."""
        if not self._enabled or message.guild is None:
            return None
        cmd = None
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
        if not self.is_present():
            return self._closed_text()
        view = HaendlerView()
        try:
            view.message = await message.reply(
                embed=self._panel_embed(), view=view, mention_author=False)
            self._protect(view.message)
        except (discord.HTTPException, TypeError):
            log.exception("Haendler-Panel konnte nicht gesendet werden")
            return "Der Händler kramt gerade in seiner Truhe - versuch's gleich nochmal."
        return HANDLED

    # --- Kauf -------------------------------------------------------------
    async def buy(self, member, offer_id):
        """Kauft einen Verkaufs-Titel fuer 'member'. Rueckgabe: Antworttext."""
        if not self.is_present():
            return "🐫 Zu spät - der Händler ist schon weitergezogen."
        st = self._state()
        e = next((o for o in st.get("stock", []) if o["id"] == offer_id), None)
        if e is None:
            return "Diesen Titel führt der Händler nicht (mehr)."
        if economy.owns_title(member.id, e["text"]):
            return f"Den Titel **{e['label']}** hast du schon. 😎"
        # Limitierte Ware pruefen.
        limit = e.get("limit", 0)
        sold = st.setdefault("sold", {}).setdefault(e["id"], [])
        if limit and len(sold) >= limit:
            return f"❌ **{e['label']}** ist leider ausverkauft. Zu langsam!"
        preis = int(e["price"])
        if economy.get_coins(member.id) < preis:
            fehlt = preis - economy.get_coins(member.id)
            return f"Zu teuer - dir fehlen noch {self._fmt(fehlt)} {economy.COIN}."
        # Kauf abwickeln.
        economy.add_coins(member.id, -preis, reason="haendler")
        economy.grant_title(member.id, e["text"], e["label"], e["rarity"], wear=True)
        if str(member.id) not in sold:
            sold.append(str(member.id))
        try:
            await economy.sync_role(member)
        except Exception:  # noqa: BLE001 - Rolle ist Deko, Kauf darf nie platzen
            log.exception("Rollen-Sync nach Haendler-Kauf fehlgeschlagen")
        await self._save_all()
        meta = titles.RARITY.get(e["rarity"], titles.RARITY["normal"])
        return (f"🎉 Gekauft! Du trägst jetzt **{e['label']}** "
                f"({meta['emoji']} {meta['label']}) und hast die Rolle "
                f"**{meta['role']}** bekommen. Ein Schnäppchen! 🤝")

    def eligible_gives(self, member, min_rarity, max_rarity=None):
        """Titel im Inventar, die als Tausch-Einsatz taugen.

        Rang >= min_rarity - und NIE besser als die Belohnung. Vorher gab es nur
        die Untergrenze: wer fuer einen Tausch "mindestens episch" brauchte,
        konnte seinen GOETTLICHEN Titel hergeben und einen schlechteren
        zurueckbekommen. Unwiderruflich, ohne Rueckfrage, und im Dropdown stand
        er ganz normal zur Auswahl."""
        min_rank = titles.RANK.get(min_rarity, 0)
        max_rank = titles.RANK.get(max_rarity, 99) if max_rarity else 99
        out = []
        for o in economy.list_titles(member.id):
            rang = titles.RANK.get(o.get("rarity", "normal"), 0)
            if min_rank <= rang <= max_rank:
                out.append(o)
        return out

    async def trade(self, member, trade_id, give_text):
        """Wickelt einen Tausch ab: gibt 'give_text' her (+ Aufzahlung) und
        bekommt den Belohnungs-Titel. Rueckgabe: Antworttext."""
        if not self.is_present():
            return "🐫 Zu spät - der Händler ist schon weitergezogen."
        st = self._state()
        t = next((x for x in st.get("trades", []) if x["id"] == trade_id), None)
        if t is None:
            return "Diesen Tausch bietet der Händler nicht (mehr) an."
        if economy.owns_title(member.id, t["reward_text"]):
            return f"Den Titel **{t['reward_label']}** hast du schon. 😎"
        # Einsatz pruefen: besitzt er den Titel und hat der die noetige Seltenheit?
        besitz = next((o for o in economy.list_titles(member.id)
                       if o.get("text") == give_text), None)
        if besitz is None:
            return "Diesen Titel besitzt du gar nicht."
        min_rank = titles.RANK.get(t["need_rarity"], 0)
        eigen_rang = titles.RANK.get(besitz.get("rarity", "normal"), 0)
        if eigen_rang < min_rank:
            need_meta = titles.RARITY.get(t["need_rarity"], titles.RARITY["normal"])
            return (f"Für diesen Tausch brauchst du mindestens einen "
                    f"**{need_meta['label']}**-Titel als Einsatz.")
        # Auch serverseitig: niemand gibt etwas Besseres her, als er bekommt.
        # Die Auswahl filtert das schon, aber der Tausch darf sich nicht darauf
        # verlassen - der Titel ist danach weg und kommt nicht zurueck.
        lohn_rang = titles.RANK.get(t.get("reward_rarity", "normal"), 0)
        if eigen_rang > lohn_rang:
            rew_meta = titles.RARITY.get(t.get("reward_rarity", "normal"),
                                         titles.RARITY["normal"])
            return (f"Nicht dein Ernst – der Einsatz ist **besser** als die "
                    f"Belohnung (**{rew_meta['label']}**). Such dir einen "
                    f"schwächeren Titel aus.")
        surcharge = int(t["surcharge"])
        if economy.get_coins(member.id) < surcharge:
            fehlt = surcharge - economy.get_coins(member.id)
            return (f"Für die Aufzahlung fehlen dir noch "
                    f"{self._fmt(fehlt)} {economy.COIN}.")
        # Tausch abwickeln: Einsatz-Titel raus, Aufzahlung abbuchen, Belohnung rein.
        # Belohnung wird mit reward_text als 'text' vergeben - identisch zu einem
        # gekauften Exemplar (und konsistent mit der owns_title-Pruefung oben).
        economy.remove_title(member.id, give_text)
        if surcharge:
            economy.add_coins(member.id, -surcharge, reason="haendler-tausch")
        economy.grant_title(member.id, t["reward_text"], t["reward_label"],
                            t["reward_rarity"], wear=True)
        try:
            await economy.sync_role(member)
        except Exception:  # noqa: BLE001
            log.exception("Rollen-Sync nach Haendler-Tausch fehlgeschlagen")
        await self._save_all()
        rew_meta = titles.RARITY.get(t["reward_rarity"], titles.RARITY["normal"])
        return (f"🤝 Tausch perfekt! **{besitz.get('label', give_text)}** ist weg - "
                f"dafür trägst du jetzt **{t['reward_label']}** "
                f"({rew_meta['emoji']} {rew_meta['label']}). Guter Deal! ✨")

    async def _save_all(self):
        """Haendler-Zustand + economy zusammen persistieren."""
        try:
            await self._save()
            await economy.flush()
        except Exception:  # noqa: BLE001
            log.exception("Speichern nach Haendler-Aktion fehlgeschlagen")

    # --- Auto-Loesch-Schutz fuers Panel -----------------------------------
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



# --- Interaktive Views -------------------------------------------------------
class _BuySelect(discord.ui.Select):
    """Dropdown mit den Verkaufs-Titeln des Haendlers."""

    def __init__(self, stock):
        opts = []
        for e in stock:
            meta = titles.RARITY.get(e["rarity"], titles.RARITY["normal"])
            opts.append(discord.SelectOption(
                label=e["label"][:100],
                value=e["id"],
                description=f"{meta['label']} · {instance._fmt(e['price'])} {economy.COIN}"[:100],
                emoji=meta["emoji"]))
        if not opts:
            opts = [discord.SelectOption(label="– nichts im Angebot –", value="_none")]
        super().__init__(placeholder="🛒 Titel kaufen…", min_values=1, max_values=1,
                         options=opts, row=0)

    async def callback(self, interaction):
        if self.values[0] == "_none":
            await interaction.response.send_message("Gerade nichts zu kaufen.", ephemeral=True)
            return
        try:
            text = await instance.buy(interaction.user, self.values[0])
        except Exception:  # noqa: BLE001
            log.exception("Haendler-Kauf fehlgeschlagen")
            text = "Beim Kauf ist etwas schiefgelaufen - versuch's gleich nochmal."
        await interaction.response.send_message(text, ephemeral=True)
        await self.view._refresh(interaction)


class _TradeSelect(discord.ui.Select):
    """Dropdown mit den Tausch-Deals des Haendlers."""

    def __init__(self, trades):
        opts = []
        for t in trades:
            need_meta = titles.RARITY.get(t["need_rarity"], titles.RARITY["normal"])
            opts.append(discord.SelectOption(
                label=t["reward_label"][:100],
                value=t["id"],
                description=(f"gib {need_meta['label']}-Titel + "
                             f"{instance._fmt(t['surcharge'])} {economy.COIN}")[:100],
                emoji="🔄"))
        if not opts:
            opts = [discord.SelectOption(label="– keine Tausch-Deals –", value="_none")]
        super().__init__(placeholder="🔄 Titel eintauschen…", min_values=1, max_values=1,
                         options=opts, row=1)

    async def callback(self, interaction):
        if self.values[0] == "_none":
            await interaction.response.send_message("Gerade keine Tausch-Deals.", ephemeral=True)
            return
        st = instance._state()
        t = next((x for x in st.get("trades", []) if x["id"] == self.values[0]), None)
        if t is None:
            await interaction.response.send_message(
                "Diesen Tausch gibt's nicht mehr.", ephemeral=True)
            return
        if economy.owns_title(interaction.user.id, t["reward_text"]):
            await interaction.response.send_message(
                f"Den Titel **{t['reward_label']}** hast du schon. 😎", ephemeral=True)
            return
        eligible = instance.eligible_gives(interaction.user, t["need_rarity"],
                                           t.get("reward_rarity"))
        if not eligible:
            need_meta = titles.RARITY.get(t["need_rarity"], titles.RARITY["normal"])
            await interaction.response.send_message(
                f"Dir fehlt ein **{need_meta['label']}**-Titel (oder besser) als "
                f"Tausch-Einsatz. Kauf dir erst einen im Shop. 🙂", ephemeral=True)
            return
        # Zweiter Schritt: welchen eigenen Titel gibst du her?
        view = _GiveView(t, eligible)
        await interaction.response.send_message(
            f"Welchen Titel gibst du für **{t['reward_label']}** her?",
            view=view, ephemeral=True)


class _GiveSelect(discord.ui.Select):
    """Dropdown der eigenen Titel, die als Tausch-Einsatz taugen."""

    def __init__(self, trade, eligible):
        self.trade = trade
        opts = []
        seen = set()
        for o in eligible:
            t = o.get("text", "")
            if not t or t in seen:
                continue
            seen.add(t)
            meta = titles.RARITY.get(o.get("rarity", "normal"), titles.RARITY["normal"])
            opts.append(discord.SelectOption(
                label=o.get("label", t)[:100], value=t[:100],
                description=meta["label"], emoji=meta["emoji"]))
            if len(opts) >= 25:
                break
        super().__init__(placeholder="Deinen Einsatz-Titel wählen…",
                         min_values=1, max_values=1, options=opts, row=0)

    async def callback(self, interaction):
        try:
            text = await instance.trade(interaction.user, self.trade["id"], self.values[0])
        except Exception:  # noqa: BLE001
            log.exception("Haendler-Tausch fehlgeschlagen")
            text = "Beim Tausch ist etwas schiefgelaufen - versuch's gleich nochmal."
        # Ephemere Auswahl entschaerfen (Buttons/Selects raus) und Ergebnis zeigen.
        for child in self.view.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(content=text, view=self.view)
        except discord.HTTPException:
            await interaction.response.send_message(text, ephemeral=True)


class _GiveView(discord.ui.View):
    """Ephemere Zwischen-Ansicht: eigenen Titel für den Tausch wählen."""

    def __init__(self, trade, eligible):
        super().__init__(timeout=120)
        self.add_item(_GiveSelect(trade, eligible))


class HaendlerView(discord.ui.View):
    """Das Haendler-Panel: Kaufen + Tauschen. Jeder kauft/tauscht fuer sich."""

    def __init__(self):
        super().__init__(timeout=None)
        self.message = None
        st = instance._state()
        self.add_item(_BuySelect(st.get("stock", [])))
        self.add_item(_TradeSelect(st.get("trades", [])))

    async def _refresh(self, interaction):
        """Nach einem Kauf die Restmengen im Panel aktualisieren (best effort)."""
        if self.message is None:
            return
        try:
            await self.message.edit(embed=instance._panel_embed(), view=self)
        except discord.HTTPException:
            pass


class TickResult:
    """Ergebnis von merchant.tick(): 'arrive' (embed+view posten) oder 'leave'."""

    def __init__(self, kind, embed, view):
        self.kind = kind
        self.embed = embed
        self.view = view


# --- Singleton + Modul-API ---------------------------------------------------
instance = Merchant()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
tick = instance.tick
buy = instance.buy
trade = instance.trade
is_present = instance.is_present
appear_time_str = instance.appear_time_str
# bot.py baut fuer JEDEN Server ein eigenes Kauf-Panel - ohne diesen Alias
# flog dort ein AttributeError, und der Haendler-Loop war fuer immer tot.
build_view = instance.build_view
# Den Auto-Loesch-Schutz fuer ein Panel setzt bot.py ueber diesen Weg, damit das
# vorige Panel desselben Kanals dabei wieder abgemeldet wird.
protect_panel = instance._protect
