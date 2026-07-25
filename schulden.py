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

import asyncio
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

# --- Automatische Tilgung ("was zwingt mich zu zahlen?") ---------------------
# Von JEDER Einnahme wandert dieser Anteil automatisch an den groessten
# Glaeubiger, bis die Schuld weg ist. Coins entstehen dabei nie und verschwinden
# nie - sie wechseln nur den Besitzer, und die Tafel schreibt es mit.
TILGUNG_PCT = float(os.getenv("SCHULDEN_TILGUNG_PCT", "0.20") or "0.20")
# Kleinstbetraege in Ruhe lassen (sonst wird jede 5-Coin-Gutschrift angeknabbert).
TILGUNG_MIN_EINNAHME = int(os.getenv("SCHULDEN_TILGUNG_MIN", "50") or "50")
# Quellen, die NICHT angetastet werden:
# - schulden-tilgung: eigene Buchung (Rekursions-Schutz)
# - panel: Korrekturen aus dem Dashboard muessen exakt bleiben
# - floaktie: Aktien-Erloese, damit die Kurs-Rechnung sauber bleibt
# - giveaway/-rueck: Escrow gehoert dem Veranstalter
# - pay: direkte Ueberweisungen zwischen Menschen. Sonst passiert Unsinn: der
#   Glaeubiger gibt dem Schuldner 500, und 100 davon wandern im selben Moment
#   automatisch zu ihm zurueck. Solche Zahlungen stehen ohnehin auf der Tafel.
# Getilgt wird also aus ECHTEN Einnahmen: Daily, Casino, Spiele, Gewinne, Raub ...
TILGUNG_TABU = ("schulden-tilgung", "panel", "floaktie", "giveaway-rueck",
                "giveaway", "dividende", "pay")
# Mahnung: hoechstens so oft eine DM je Schuldner.
MAHN_ABSTAND = int(os.getenv("SCHULDEN_MAHN_ABSTAND", "86400") or "86400")
# Erst ab dieser Summe mahnt Flo ueberhaupt.
MAHN_AB = int(os.getenv("SCHULDEN_MAHN_AB", "1000") or "1000")

FARBE = 0xC9A227
FARBE_SCHULDEN = 0xE04B3C      # rot: da ist was offen
FARBE_SAUBER = 0x2ECC71        # gruen: blitzsauber


class Schulden:
    """Kapselt die Kreide-Tafel: Saldo je Paar, Hinweis-Texte und die Befehle."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._bot_name = "Flo"
        # Riegel gegen Rekursion: die Tilgung bucht selbst Coins um, und das ruft
        # add_coins - was ohne Riegel wieder die Tilgung anstossen wuerde.
        self._tilgung_laeuft = False
        self._save_tasks = set()

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("SCHULDEN_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Kreide-Tafel aus (SCHULDEN_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Kreide-Tafel aus (economy ist aus - ohne Coins nichts zu merken).")
            return False
        self._store = JsonStore("schulden.json", default={"pairs": {}, "stats": {}})
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

    # --- Automatische Tilgung --------------------------------------------
    def _stats(self, uid):
        st = self._store.data.setdefault("stats", {}) if self._store else {}
        return st.setdefault(str(int(uid)), {"getilgt": 0, "erhalten": 0, "mahnung": 0})

    def getilgt_summe(self, uid):
        """Wie viel bei diesem Nutzer schon automatisch abgezogen wurde."""
        if not self._enabled:
            return 0
        return int(self._stats(uid).get("getilgt", 0))

    def tilgen_von_einnahme(self, uid, einnahme, reason=""):
        """Zieht von einer Einnahme automatisch einen Teil ab und gibt ihn dem
        groessten Glaeubiger. Rueckgabe: (betrag, glaeubiger_id) oder (0, 0).

        DAS ist der Zwang zum Zahlen: wer Schulden hat, bekommt von jeder Einnahme
        nur noch einen Teil - der Rest geht automatisch an den, dem er was schuldet.
        Es wird nichts erschaffen und nichts vernichtet; beide Konten aendern sich
        um genau denselben Betrag, und die Tafel verrechnet es."""
        if not self._enabled or self._tilgung_laeuft:
            return 0, 0
        if TILGUNG_PCT <= 0:
            return 0, 0
        if str(reason or "").lower() in TILGUNG_TABU:
            return 0, 0
        try:
            einnahme = int(einnahme)
        except (TypeError, ValueError):
            return 0, 0
        if einnahme < TILGUNG_MIN_EINNAHME:
            return 0, 0
        _forderungen, meine = self.posten(uid)
        if not meine:
            return 0, 0
        glaeubiger, offen = meine[0]              # groesster Posten zuerst
        # Nie mehr als die Schuld, nie mehr als der Nutzer wirklich hat.
        habe = max(0, economy.get_coins(uid))
        betrag = min(int(einnahme * TILGUNG_PCT), int(offen), habe)
        if betrag <= 0:
            return 0, 0
        self._tilgung_laeuft = True
        try:
            economy.add_coins(uid, -betrag, reason="schulden-tilgung")
            economy.add_coins(glaeubiger, betrag, reason="schulden-tilgung")
            self.record_pay(uid, glaeubiger, betrag)
            self._stats(uid)["getilgt"] = self.getilgt_summe(uid) + betrag
            self._stats(glaeubiger)["erhalten"] = int(
                self._stats(glaeubiger).get("erhalten", 0)) + betrag
        finally:
            self._tilgung_laeuft = False
        log.info("Tilgung: %s -> %s, %d Coins (Einnahme %d aus '%s'), offen %d.",
                 uid, glaeubiger, betrag, einnahme, reason, offen - betrag)
        self._save_soon()
        return betrag, glaeubiger

    def _save_soon(self):
        """Speichert nebenher (ohne laufenden Loop passiert nichts - Tests)."""
        if self._store is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._store.save())
        except RuntimeError:
            return
        self._save_tasks.add(task)
        task.add_done_callback(self._save_tasks.discard)

    # --- Mahnungen --------------------------------------------------------
    async def mahn_tick(self, client):
        """Von bot.py periodisch: erinnert Schuldner per DM an offene Posten.
        Hoechstens einmal je MAHN_ABSTAND und nur ab MAHN_AB Coins."""
        if not self._enabled or client is None:
            return 0
        jetzt = time.time()
        gemahnt = 0
        ids = set()
        for key in list(self._pairs()):
            for teil in str(key).split(":"):
                try:
                    ids.add(int(teil))
                except (TypeError, ValueError):
                    continue
        for uid in ids:
            _haben, soll, _netto = self.summen(uid)
            if soll < MAHN_AB:
                continue
            st = self._stats(uid)
            if jetzt - float(st.get("mahnung", 0) or 0) < MAHN_ABSTAND:
                continue
            st["mahnung"] = jetzt
            try:
                user = client.get_user(uid) or await client.fetch_user(uid)
                if user is None:
                    continue
                await user.send(embed=self._mahn_embed(uid, soll))
                gemahnt += 1
            except Exception:  # noqa: BLE001 - DMs koennen zu sein, nie fatal
                log.debug("Mahnung an %s nicht moeglich", uid, exc_info=True)
        if gemahnt:
            self._save_soon()
        return gemahnt

    def _mahn_embed(self, uid, soll):
        _f, meine = self.posten(uid)
        zeilen = "\n".join(f"• <@{u}> — **{fmt(b)}** {economy.COIN}" for u, b in meine[:6])
        e = discord.Embed(
            title="🧾 Da ist noch was offen",
            description=(f"Auf deiner Kreide-Tafel stehen **{fmt(soll)}** "
                         f"{economy.COIN}:\n{zeilen}"),
            color=FARBE_SCHULDEN)
        e.add_field(
            name="Wie das kleiner wird",
            value=(f"**{int(TILGUNG_PCT * 100)} %** von jeder Einnahme (Daily, "
                   f"Casino, Spiele, Gewinne) wandern automatisch dorthin, bis "
                   f"nichts mehr offen ist.\n"
                   f"Schneller gehts mit `{self._bot_name} pay @wer betrag`."),
            inline=False)
        if self.getilgt_summe(uid):
            e.set_footer(text=f"Bisher automatisch getilgt: "
                              f"{fmt(self.getilgt_summe(uid))} Coins")
        return e

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

    # --- Bausteine fuer die 'pay'-Bestaetigung ----------------------------
    def pay_block(self, von, an, betrag, *, ziel_name=None, guild=None):
        """Schreibt die Zahlung mit und liefert alles, was die Bestaetigung braucht.

        Rueckgabe: dict mit
          stand    - was die Zahlung mit dem Stand gemacht hat (ein Satz)
          warnung  - eigene offene Posten bei ANDEREN (oder "")
          soll     - Gesamtschuld des Zahlers danach
          saldo    - Stand mit dem Empfaenger danach (aus Sicht des Zahlers)
          farbe    - rot bei eigenen Schulden, gold bei Forderung, sonst gruen
        """
        leer = {"stand": "", "warnung": "", "soll": 0, "saldo": 0,
                "farbe": FARBE_SAUBER}
        if not self._enabled:
            return leer
        vorher, nachher = self.record_pay(von, an, betrag)
        name = ziel_name or self._name(an, guild)
        if vorher >= 0:
            stand = (f"**{name}** steht damit mit **{fmt(nachher)}** {economy.COIN} "
                     f"bei dir in der Kreide.")
        elif nachher < 0:
            stand = (f"Angerechnet – bei **{name}** hast du noch **{fmt(-nachher)}** "
                     f"{economy.COIN} offen _(vorher {fmt(-vorher)})_.")
        elif nachher == 0:
            stand = f"Deine Rechnung mit **{name}** ist **ausgeglichen**. ✨"
        else:
            stand = (f"Schulden bei **{name}** beglichen – jetzt steht **{name}** "
                     f"mit **{fmt(nachher)}** {economy.COIN} bei dir in der Kreide.")
        _f, meine = self.posten(von)
        andere = [(u, b) for u, b in meine if int(u) != int(an)]
        rest = sum(b for _u, b in andere)
        warnung = ""
        if rest > 0:
            liste = "\n".join(f"• {self._name(u, guild)} — **{fmt(b)}** {economy.COIN}"
                               for u, b in andere[:5])
            mehr = f"\n… und {len(andere) - 5} weitere" if len(andere) > 5 else ""
            warnung = (f"{liste}{mehr}\n_{int(TILGUNG_PCT * 100)} % deiner Einnahmen "
                       f"(Daily, Casino, Spiele, Gewinne) gehen automatisch dorthin – "
                       f"schneller gehts mit `{self._bot_name} pay`._")
        soll = sum(b for _u, b in meine)
        farbe = FARBE_SCHULDEN if soll > 0 else (FARBE if nachher > 0 else FARBE_SAUBER)
        return {"stand": stand, "warnung": warnung, "soll": soll,
                "saldo": nachher, "farbe": farbe, "offen_bei_anderen": rest}

    async def note_pay_block(self, von, an, betrag, *, ziel_name=None, guild=None):
        """pay_block + speichern. Wirft nie."""
        try:
            block = self.pay_block(von, an, betrag, ziel_name=ziel_name, guild=guild)
        except Exception:  # noqa: BLE001
            log.exception("Kreide-Notiz fehlgeschlagen")
            return {"stand": "", "warnung": "", "soll": 0, "saldo": 0,
                    "farbe": FARBE_SAUBER, "offen_bei_anderen": 0}
        try:
            await self._save()
        except Exception:  # noqa: BLE001
            log.exception("Kreide-Tafel konnte nicht gespeichert werden")
        return block

    # --- Hinweis-Text fuer 'pay' (Text-Variante) --------------------------
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
        if soll > 0:
            kopf = (f"🔴 Du hast **{fmt(soll)}** {economy.COIN} offen. "
                    f"**{int(TILGUNG_PCT * 100)} %** deiner Einnahmen (Daily, Casino, "
                    f"Spiele, Gewinne) gehen automatisch dorthin, bis es weg ist.")
            farbe = FARBE_SCHULDEN
        elif forderungen:
            kopf = "🟡 Du hast nichts offen – aber andere bei dir."
            farbe = FARBE
        else:
            kopf = "🟢 Blitzsauber: du hast mit niemandem etwas offen. ✨"
            farbe = FARBE_SAUBER
        e = discord.Embed(title="🧾 Deine Kreide-Tafel", description=kopf, color=farbe)
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
            getilgt = self.getilgt_summe(autor.id)
            if getilgt:
                e.add_field(name="Automatisch getilgt",
                            value=f"**{fmt(getilgt)}** {economy.COIN}", inline=True)
            e.set_footer(text=f"{self._bot_name} schulden @wer · "
                              f"{self._bot_name} schulden erlassen @wer")
        try:
            e.set_thumbnail(url=autor.display_avatar.url)
        except Exception:  # noqa: BLE001 - Bild ist Deko
            pass
        return e

    def _paar_embed(self, autor, ziel):
        saldo, n, vol, log_ = self.paar_info(autor.id, ziel.id)
        farbe = FARBE_SCHULDEN if saldo < 0 else (FARBE if saldo > 0 else FARBE_SAUBER)
        e = discord.Embed(title=f"🧾 Kreide-Tafel · {ziel.display_name}", color=farbe)
        if saldo > 0:
            e.description = (f"<@{ziel.id}> steht mit **{fmt(saldo)}** {economy.COIN} "
                             f"bei dir in der Kreide.")
        elif saldo < 0:
            e.description = (f"🔴 Du hast bei <@{ziel.id}> noch **{fmt(-saldo)}** "
                             f"{economy.COIN} offen.\n"
                             f"_{int(TILGUNG_PCT * 100)} % deiner Einnahmen gehen "
                             f"automatisch dorthin._")
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
        e = discord.Embed(title="🧾 Die größten offenen Posten", color=FARBE_SCHULDEN)
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
                "Flo schreibt mit, wer wem etwas überwiesen hat. Eine Zahlung wird "
                "**nie blockiert** und es gibt keine Zinsen – aber wer Schulden hat, "
                f"gibt von seinen Einnahmen **{int(TILGUNG_PCT * 100)} %** "
                "automatisch an seinen größten Gläubiger ab (Daily, Casino, Spiele, "
                "Gewinne), bis nichts mehr offen ist.\n\n"
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
pay_block = instance.pay_block
note_pay_block = instance.note_pay_block
saldo = instance.saldo
posten = instance.posten
summen = instance.summen
paar_info = instance.paar_info
erlassen = instance.erlassen
top = instance.top
tilgen_von_einnahme = instance.tilgen_von_einnahme
getilgt_summe = instance.getilgt_summe
mahn_tick = instance.mahn_tick
