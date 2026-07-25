"""Flos Kreide-Tafel: merkt sich, wer wem was gegeben hat.

Das ist AUSDRUECKLICH nur eine Anzeige. Es blockiert nichts, es zieht nichts ab,
es verzinst nichts - eine Zahlung geht IMMER durch. Flo schreibt nur mit, wie der
Stand zwischen zwei Leuten ist, und erinnert bei der naechsten Zahlung daran.

Gerechnet wird ein NETTO-SALDO pro Personen-Paar. Dadurch verrechnet sich eine
Rueckzahlung von selbst:

    A zahlt B 1.000   ->  B steht mit 1.000 bei A in der Kreide
    B zahlt A   400   ->  B steht noch mit   600 bei A in der Kreide
    B zahlt A   600   ->  ausgeglichen
    B zahlt A   200   ->  jetzt steht A mit 200 bei B in der Kreide

Erfasst werden nur FREIWILLIGE Ueberweisungen zwischen Menschen ('Flo pay'), kein
Casino, keine Spiele, keine Admin-Buchungen - sonst waere die Tafel wertlos.

Befehle (nach 'Flo'):
- schulden                    deine Tafel: was du bekommst, was du offen hast
- schulden @wer               Stand mit dieser Person + letzte Bewegungen
- schulden top                die groessten offenen Posten auf dem Server
- schulden erlassen @wer [x]  als Gläubiger einen Posten streichen (ganz oder teils)

Zustand: data/schulden.json
"""

import logging
import os
import time

import discord

import ai
import economy
import numfmt
from store import JsonStore

log = logging.getLogger("dcbot.schulden")

# Sentinel: das Modul hat selbst geantwortet -> bot.py schweigt.
HANDLED = object()

fmt = numfmt.fmt

# Bewusst OHNE "offen"/"soll": das sind zu allgemeine Woerter und wuerden normale
# Saetze ("Flo offen gesagt ...") als Befehl abfangen.
_CMDS = ("schulden", "schuld", "kreide", "kreidetafel", "zettel", "schuldenbuch",
         "debt", "debts")

# So viele Bewegungen bleiben je Paar erhalten (fuer die Detail-Ansicht).
LOG_KEPT = 12
# Ab dieser Summe faellt der Hinweis etwas deutlicher aus.
DEUTLICH_AB = int(os.getenv("SCHULDEN_DEUTLICH_AB", "100000") or "100000")
FARBE = 0xC9A227


class Schulden:
    """Kapselt die Kreide-Tafel: Saldo je Paar, Hinweis-Texte und die Befehle."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._bot_name = "Flo"

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("SCHULDEN_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Kreide-Tafel aus (SCHULDEN_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Kreide-Tafel aus (economy ist aus - ohne Coins nichts zu merken).")
            return False
        self._store = JsonStore("schulden.json", default={"pairs": {}})
        self._enabled = True
        log.info("Kreide-Tafel aktiv (%d Paare notiert).", len(self._pairs()))
        return True

    def is_enabled(self):
        return self._enabled

    def _pairs(self):
        if self._store is None:
            return {}
        return self._store.data.setdefault("pairs", {})

    async def _save(self):
        if self._store is not None:
            await self._store.save()

    # --- Saldo-Verwaltung -------------------------------------------------
    @staticmethod
    def _key(a, b):
        """Kanonischer Schluessel: immer die kleinere ID zuerst. 'net' bedeutet
        dann IMMER: so viel schuldet der ZWEITE dem ERSTEN."""
        a, b = int(a), int(b)
        return (f"{a}:{b}", False) if a < b else (f"{b}:{a}", True)

    def _eintrag(self, a, b, *, anlegen=False):
        key, gedreht = self._key(a, b)
        p = self._pairs().get(key)
        if p is None and anlegen:
            p = {"net": 0, "vol": 0, "n": 0, "first": time.time(), "last": time.time(),
                 "log": []}
            self._pairs()[key] = p
        return p, gedreht

    def saldo(self, wer, gegen):
        """Stand aus Sicht von 'wer': positiv = 'gegen' schuldet 'wer' etwas,
        negativ = 'wer' schuldet 'gegen' etwas, 0 = ausgeglichen."""
        if not self._enabled:
            return 0
        p, gedreht = self._eintrag(wer, gegen)
        if p is None:
            return 0
        net = int(p.get("net", 0))
        # net = "der ZWEITE schuldet dem ERSTEN". Ist 'wer' der zweite (gedreht),
        # dreht sich das Vorzeichen.
        return -net if gedreht else net

    def paar_info(self, wer, gegen):
        """(saldo, anzahl_zahlungen, volumen, letzte_bewegungen) aus Sicht von 'wer'."""
        p, _gedreht = self._eintrag(wer, gegen)
        if p is None:
            return 0, 0, 0, []
        return (self.saldo(wer, gegen), int(p.get("n", 0)), int(p.get("vol", 0)),
                list(p.get("log", []))[-LOG_KEPT:])

    def record_pay(self, von, an, betrag):
        """Schreibt eine Zahlung mit. Rueckgabe: (saldo_vorher, saldo_nachher) aus
        Sicht des ZAHLERS (positiv = der Empfaenger steht bei ihm in der Kreide).

        Aendert NIE Kontostaende - das ist reine Buchfuehrung."""
        if not self._enabled:
            return 0, 0
        try:
            von, an, betrag = int(von), int(an), int(betrag)
        except (TypeError, ValueError):
            return 0, 0
        if von == an or betrag <= 0:
            return 0, 0
        vorher = self.saldo(von, an)
        p, gedreht = self._eintrag(von, an, anlegen=True)
        # Zahlen erhoeht die Forderung des Zahlers gegen den Empfaenger.
        p["net"] = int(p.get("net", 0)) + (-betrag if gedreht else betrag)
        p["vol"] = int(p.get("vol", 0)) + betrag
        p["n"] = int(p.get("n", 0)) + 1
        p["last"] = time.time()
        p.setdefault("log", []).append({"t": time.time(), "von": von, "betrag": betrag})
        p["log"] = p["log"][-LOG_KEPT:]
        return vorher, vorher + betrag

    def erlassen(self, glaeubiger, schuldner, betrag=None):
        """Streicht (einen Teil) dessen, was 'schuldner' bei 'glaeubiger' offen hat.

        Rueckgabe: (erlassen, rest) - oder (0, saldo) wenn da nichts offen ist.
        Erlassen darf nur, wer etwas zu bekommen hat."""
        if not self._enabled:
            return 0, 0
        offen = self.saldo(glaeubiger, schuldner)
        if offen <= 0:
            return 0, offen
        weg = offen if betrag is None else max(0, min(int(betrag), offen))
        if weg <= 0:
            return 0, offen
        p, gedreht = self._eintrag(glaeubiger, schuldner, anlegen=True)
        p["net"] = int(p.get("net", 0)) + (weg if gedreht else -weg)
        p["last"] = time.time()
        p.setdefault("log", []).append({"t": time.time(), "von": int(glaeubiger),
                                        "betrag": weg, "erlass": True})
        p["log"] = p["log"][-LOG_KEPT:]
        return weg, offen - weg

    # --- Auswertung -------------------------------------------------------
    def posten(self, uid):
        """(forderungen, schulden) fuer einen Nutzer, je absteigend sortiert.
        Beide Listen: [(andere_id, betrag), ...] mit betrag > 0."""
        uid = int(uid)
        forderungen, schulden = [], []
        if not self._enabled:
            return forderungen, schulden
        for key, p in list(self._pairs().items()):
            try:
                a, b = (int(x) for x in key.split(":"))
            except (TypeError, ValueError):
                continue
            if uid not in (a, b):
                continue
            net = int(p.get("net", 0))
            if not net:
                continue
            anderer = b if uid == a else a
            wert = net if uid == a else -net
            if wert > 0:
                forderungen.append((anderer, wert))
            else:
                schulden.append((anderer, -wert))
        forderungen.sort(key=lambda x: x[1], reverse=True)
        schulden.sort(key=lambda x: x[1], reverse=True)
        return forderungen, schulden

    def summen(self, uid):
        """(bekommt_insgesamt, schuldet_insgesamt, netto) fuer einen Nutzer."""
        forderungen, schulden = self.posten(uid)
        haben = sum(b for _u, b in forderungen)
        soll = sum(b for _u, b in schulden)
        return haben, soll, haben - soll

    def top(self, limit=10):
        """Groesste offene Posten serverweit: [(glaeubiger, schuldner, betrag), ...]"""
        raus = []
        for key, p in list(self._pairs().items()):
            net = int(p.get("net", 0))
            if not net:
                continue
            try:
                a, b = (int(x) for x in key.split(":"))
            except (TypeError, ValueError):
                continue
            if net > 0:
                raus.append((a, b, net))        # b schuldet a
            else:
                raus.append((b, a, -net))
        raus.sort(key=lambda x: x[2], reverse=True)
        return raus[:max(1, int(limit))]

    # --- Namen ------------------------------------------------------------
    def _name(self, uid, guild=None):
        """Anzeigename OHNE Ping (fuer Nachrichten-Text)."""
        n = ""
        try:
            n = economy.display_name_of(uid) or ""
        except Exception:  # noqa: BLE001
            n = ""
        if not n and guild is not None:
            m = guild.get_member(int(uid)) if hasattr(guild, "get_member") else None
            n = getattr(m, "display_name", "") or ""
        return n or f"ID {uid}"

    # --- Hinweis-Text fuer 'pay' ------------------------------------------
    def pay_hinweis(self, von, an, betrag, *, ziel_name=None, guild=None):
        """Der Zusatz, den 'Flo pay' unter die Bestaetigung haengt.

        Erzaehlt in EINEM Satz, was die Zahlung mit dem Stand gemacht hat
        (neue Forderung / Rueckzahlung / ausgeglichen / gedreht) und erinnert in
        einer zweiten Zeile an eigene offene Posten. Nie eine Blockade, nie ein
        Vorwurf - nur der Stand auf der Tafel."""
        if not self._enabled:
            return ""
        vorher, nachher = self.record_pay(von, an, betrag)
        name = ziel_name or self._name(an, guild)
        zeilen = []
        if vorher >= 0:
            if nachher > 0:
                zeilen.append(f"📒 **{name}** steht damit mit **{fmt(nachher)}** "
                              f"{economy.COIN} bei dir in der Kreide.")
        elif nachher < 0:
            zeilen.append(f"📒 Angerechnet: du hast bei **{name}** noch "
                          f"**{fmt(-nachher)}** {economy.COIN} offen "
                          f"(vorher {fmt(-vorher)}).")
        elif nachher == 0:
            zeilen.append(f"📒 Damit ist deine Rechnung mit **{name}** "
                          f"**ausgeglichen**. ✨")
        else:
            zeilen.append(f"📒 Schulden bei **{name}** beglichen – jetzt steht "
                          f"**{name}** mit **{fmt(nachher)}** {economy.COIN} bei dir "
                          f"in der Kreide.")
        # Zweite Zeile: eigene offene Posten bei ANDEREN. Die Person, an die eben
        # gezahlt wurde, steht schon in Zeile 1 - die hier nochmal zu nennen waere
        # nur Wiederholung.
        _f, meine = self.posten(von)
        andere = [(u, b) for u, b in meine if int(u) != int(an)]
        rest = sum(b for _u, b in andere)
        if rest > 0:
            top = ", ".join(f"{self._name(u, guild)} ({fmt(b)})" for u, b in andere[:2])
            mehr = f" +{len(andere) - 2} weitere" if len(andere) > 2 else ""
            wie = "stehen noch" if rest < DEUTLICH_AB else "stehen immer noch"
            zeilen.append(f"   Bei dir selbst {wie} **{fmt(rest)}** {economy.COIN} "
                          f"offen – bei {top}{mehr}.")
        return ("\n" + "\n".join(zeilen)) if zeilen else ""

    async def note_pay(self, von, an, betrag, *, ziel_name=None, guild=None):
        """Einstieg fuer economy._pay: mitschreiben, speichern, Hinweis liefern.

        Wirft NIE - eine kaputte Notiz darf eine Zahlung nicht kaputtmachen."""
        try:
            text = self.pay_hinweis(von, an, betrag, ziel_name=ziel_name, guild=guild)
        except Exception:  # noqa: BLE001
            log.exception("Kreide-Notiz fehlgeschlagen")
            return ""
        try:
            await self._save()
        except Exception:  # noqa: BLE001
            log.exception("Kreide-Tafel konnte nicht gespeichert werden")
        return text

    # --- Befehle ----------------------------------------------------------
    async def handle(self, message):
        """'schulden ...'. Rueckgabe: None | Text | Embed."""
        if not self._enabled or message.guild is None:
            return None
        roh = ai.strip_lead(message.content or "")
        teile = roh.split()
        if not teile or teile[0].lower().strip(".,!?") not in _CMDS:
            return None
        rest = " ".join(teile[1:]).strip()
        low = rest.lower()
        if low.startswith(("hilfe", "help", "?", "was ")):
            return self._hilfe_embed()
        if low.startswith(("top", "liste", "ranking", "meiste", "größte", "groesste")):
            return self._top_embed(message.guild)
        if low.startswith(("erlass", "streich", "vergeb", "schenk", "verzicht")):
            return await self._erlassen(message)
        if message.mentions:
            ziel = message.mentions[0]
            if ziel.id == message.author.id:
                return "Bei dir selbst hast du nichts offen. 😄"
            return self._paar_embed(message.author, ziel)
        return self._eigene_embed(message.author, message.guild)

    def _eigene_embed(self, autor, guild=None):
        forderungen, schulden = self.posten(autor.id)
        haben, soll, netto = self.summen(autor.id)
        e = discord.Embed(
            title="📒 Deine Kreide-Tafel",
            description=("Nur eine Notiz – Zahlungen gehen immer durch.\n"
                         if (forderungen or schulden) else
                         "Blitzsauber: du hast mit niemandem etwas offen. ✨"),
            color=FARBE)
        if forderungen:
            e.add_field(
                name=f"⬅️ Du bekommst noch · {fmt(haben)} {economy.COIN}",
                value="\n".join(f"<@{u}> — **{fmt(b)}**" for u, b in forderungen[:10]),
                inline=False)
        if schulden:
            e.add_field(
                name=f"➡️ Du hast noch offen · {fmt(soll)} {economy.COIN}",
                value="\n".join(f"<@{u}> — **{fmt(b)}**" for u, b in schulden[:10]),
                inline=False)
        if forderungen or schulden:
            zeichen = "＋" if netto >= 0 else "−"
            e.add_field(name="Unterm Strich",
                        value=f"**{zeichen}{fmt(abs(netto))}** {economy.COIN}",
                        inline=True)
            e.set_footer(text=f"{self._bot_name} schulden @wer · "
                              f"{self._bot_name} schulden erlassen @wer")
        try:
            e.set_thumbnail(url=autor.display_avatar.url)
        except Exception:  # noqa: BLE001 - Bild ist Deko
            pass
        return e

    def _paar_embed(self, autor, ziel):
        saldo, n, vol, log_ = self.paar_info(autor.id, ziel.id)
        e = discord.Embed(title=f"📒 Kreide-Tafel · {ziel.display_name}", color=FARBE)
        if saldo > 0:
            e.description = (f"<@{ziel.id}> steht mit **{fmt(saldo)}** {economy.COIN} "
                             f"bei dir in der Kreide.")
        elif saldo < 0:
            e.description = (f"Du hast bei <@{ziel.id}> noch **{fmt(-saldo)}** "
                             f"{economy.COIN} offen.")
        elif n:
            e.description = f"Mit <@{ziel.id}> ist alles **ausgeglichen**. ✨"
        else:
            e.description = f"Zwischen dir und <@{ziel.id}> lief noch nie etwas."
            return e
        e.add_field(name="Zahlungen", value=str(n), inline=True)
        e.add_field(name="Bewegt insgesamt", value=f"{fmt(vol)} {economy.COIN}", inline=True)
        if log_:
            zeilen = []
            for eintrag in reversed(log_[-5:]):
                ich = int(eintrag.get("von", 0)) == autor.id
                stamp = int(eintrag.get("t", 0))
                betrag_txt = f"**{fmt(eintrag.get('betrag', 0))}** {economy.COIN}"
                if eintrag.get("erlass"):
                    wer = "Du hast" if ich else f"{ziel.display_name} hat"
                    zeilen.append(f"<t:{stamp}:R> — {wer} {betrag_txt} erlassen")
                else:
                    wer = ("Du hast" if ich else f"{ziel.display_name} hat")
                    wohin = ziel.display_name if ich else "dich"
                    zeilen.append(f"<t:{stamp}:R> — {wer} {betrag_txt} an "
                                  f"{wohin} gezahlt")
            e.add_field(name="Letzte Bewegungen", value="\n".join(zeilen), inline=False)
        try:
            e.set_thumbnail(url=ziel.display_avatar.url)
        except Exception:  # noqa: BLE001
            pass
        return e

    def _top_embed(self, guild=None):
        posten = self.top(10)
        e = discord.Embed(title="📒 Die größten offenen Posten", color=FARBE)
        if not posten:
            e.description = "Auf dem ganzen Server ist nichts offen. Vorbildlich. ✨"
            return e
        zeilen = []
        for i, (glaeubiger, schuldner, betrag) in enumerate(posten, 1):
            marke = "🥇🥈🥉"[i - 1] if i <= 3 else f"`{i}.`"
            zeilen.append(f"{marke} <@{schuldner}> → <@{glaeubiger}> · "
                          f"**{fmt(betrag)}** {economy.COIN}")
        e.description = "\n".join(zeilen)
        e.set_footer(text="Wer zuerst steht, hat am meisten offen. Rein informativ.")
        return e

    async def _erlassen(self, message):
        if not message.mentions:
            return (f"Wem willst du was erlassen? "
                    f"`{self._bot_name} schulden erlassen @jemand [betrag]`")
        ziel = message.mentions[0]
        if ziel.id == message.author.id:
            return "Dir selbst etwas erlassen ist... kreativ. 😄"
        offen = self.saldo(message.author.id, ziel.id)
        if offen < 0:
            # Umgekehrter Fall: der Autor ist selbst der Schuldner.
            return (f"Erlassen kann nur, wem etwas zusteht – du hast bei "
                    f"**{ziel.display_name}** selbst noch **{fmt(-offen)}** "
                    f"{economy.COIN} offen. Das kann nur "
                    f"**{ziel.display_name}** streichen.")
        if offen == 0:
            return f"**{ziel.display_name}** hat bei dir nichts offen."
        rest_text = " ".join(message.content.split()[1:])
        betrag = None
        if not any(w in rest_text.lower() for w in ("alles", "komplett", "ganz", "all")):
            for token in rest_text.split():
                wert = economy.parse_amount(token)
                if wert:
                    betrag = wert
                    break
        weg, rest = self.erlassen(message.author.id, ziel.id, betrag)
        if weg <= 0:
            return f"**{ziel.display_name}** hat bei dir nichts offen."
        await self._save()
        if rest > 0:
            return (f"📒 Erlassen: **{fmt(weg)}** {economy.COIN} für "
                    f"**{ziel.display_name}** – offen bleiben **{fmt(rest)}**.")
        return (f"📒 Großzügig: **{fmt(weg)}** {economy.COIN} erlassen. "
                f"**{ziel.display_name}** ist bei dir wieder blitzsauber. ✨")

    def _hilfe_embed(self):
        return discord.Embed(
            title="📒 Kreide-Tafel",
            description=(
                "Flo schreibt mit, wer wem etwas überwiesen hat – **nur als Notiz**. "
                "Eine Zahlung wird nie blockiert, es gibt keine Zinsen und keine "
                "Nachteile.\n\n"
                f"`{self._bot_name} schulden` – deine Tafel\n"
                f"`{self._bot_name} schulden @wer` – Stand mit einer Person\n"
                f"`{self._bot_name} schulden top` – größte offene Posten\n"
                f"`{self._bot_name} schulden erlassen @wer [betrag]` – als Gläubiger "
                "streichen (`alles` geht auch)\n\n"
                "Rückzahlungen verrechnen sich automatisch: zahlst du zurück, "
                "schrumpft der Posten – zahlst du mehr zurück als offen war, dreht "
                "er sich."),
            color=FARBE)


# --- Singleton + Modul-API ---------------------------------------------------
instance = Schulden()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
record_pay = instance.record_pay
pay_hinweis = instance.pay_hinweis
note_pay = instance.note_pay
saldo = instance.saldo
posten = instance.posten
summen = instance.summen
paar_info = instance.paar_info
erlassen = instance.erlassen
top = instance.top
