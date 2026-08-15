"""Coin-Handelsbuch: dokumentiert JEDE Flo-Coin-Bewegung auf dem Server.

Jede Buchung laeuft zentral ueber economy.add_coins() (bzw. die drei direkten
Stellen Level-Up/Daily/Shop in economy) und wird hier verbucht - mit Betrag,
Quelle (casino, spiele, daily, shop, pay, ...), Tag und Kontostand danach.

Gefuehrt werden pro Nutzer:
- Gesamtsummen (eingenommen / ausgegeben / Anzahl Buchungen)
- Summen je Quelle
- Tages-Buckets der letzten 60 Tage (fuers Netto-Chart)
- die letzten 50 Einzelbuchungen (Anzeige: die juengsten davon)

Befehl: handel [@wer]  ->  Statistik-Karte als Bild (render.handel_card).
"""

import asyncio
import copy
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import ai
from basis import FeatureBasis
import economy
import numfmt
import render
from store import JsonStore

log = logging.getLogger("dcbot.handel")

# So lange werden Buchungen gesammelt, bevor geschrieben wird (Sekunden).
SAVE_DEBOUNCE = float(os.getenv("HANDEL_SAVE_DEBOUNCE", "3") or "3")


def _zahl(wert):
    """Zahl aus dem Buch. Murks (None, Text) zaehlt als 0.

    Steht bewusst auf Modulebene: der Text-Fallback in _karte() braucht sie
    genauso wie die Auskunft an den Profil-Lookup."""
    try:
        return int(wert)
    except (TypeError, ValueError):
        return 0


class Handel(FeatureBasis):
    """Kapselt das Handelsbuch: Buchungs-Erfassung, Speicher und den Befehl."""

    _tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

    # So viele Tages-Buckets bzw. Einzelbuchungen bleiben je Nutzer erhalten.
    DAYS_KEPT = 60
    LAST_KEPT = 50

    # Befehlswoerter, auf die das Handelsbuch hoert.
    _CMDS = ("handel", "handelsbuch", "transaktionen", "transaktion",
             "verlauf", "trades")

    def __init__(self):
        self._enabled = False
        self._store = None
        # Referenzen auf laufende Speicher-Tasks halten - sonst kann der GC einen
        # noch nicht fertigen Task einsammeln (asyncio-Doku).
        self._save_tasks = set()
        self._save_task = None     # laufender Sammel-Speicherer
        self._dirty = False

    def setup(self):
        """Aktiviert das Handelsbuch. Braucht economy (dort liegt der Coin-Topf)."""
        if os.getenv("HANDEL_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Handelsbuch aus (HANDEL_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Handelsbuch aus (economy ist aus - keine Coins, kein Handel).")
            return False
        self._store = JsonStore("handel.json", default={"users": {}})
        self._enabled = True
        log.info("Handelsbuch aktiv (%d Nutzer mit Historie).",
                 len(self._store.data.get("users", {})))
        return True

    def is_enabled(self):
        return self._enabled

    # --- Buchung ----------------------------------------------------------
    def _user(self, uid):
        assert self._store is not None
        return self._store.data.setdefault("users", {}).setdefault(
            str(uid), {"in": 0, "out": 0, "n": 0, "by": {}, "days": {}, "last": []})

    def record(self, uid, amount, source, balance):
        """Verbucht EINE Coin-Bewegung (amount: echtes Delta, +/-). economy ruft
        das fuer jede Bewegung auf; Fehler bleiben lokal (nie fatal fuers Spiel)."""
        if not self._enabled or self._store is None or not amount:
            return
        try:
            now = datetime.now(self._tz)
            u = self._user(uid)
            u["n"] += 1
            if amount > 0:
                u["in"] += amount
            else:
                u["out"] += -amount
            src = (source or "?")[:24]
            b = u["by"].setdefault(src, {"in": 0, "out": 0, "n": 0})
            b["n"] += 1
            if amount > 0:
                b["in"] += amount
            else:
                b["out"] += -amount
            day = now.strftime("%Y-%m-%d")
            d = u["days"].setdefault(day, {"in": 0, "out": 0})
            if amount > 0:
                d["in"] += amount
            else:
                d["out"] += -amount
            # Alte Tage/Buchungen kappen, damit die Datei nicht endlos waechst.
            if len(u["days"]) > self.DAYS_KEPT:
                for k in sorted(u["days"])[:-self.DAYS_KEPT]:
                    del u["days"][k]
            u["last"].append({"t": now.strftime("%d.%m. %H:%M"),
                              "src": src, "amt": int(amount), "bal": int(balance)})
            del u["last"][:-self.LAST_KEPT]
            self._save_soon()
        except Exception:  # noqa: BLE001 - Buchhaltung darf nie ein Spiel sprengen
            log.exception("Handelsbuch-Buchung fehlgeschlagen")

    def summe_von(self, uid):
        """(eingenommen, ausgegeben, Buchungen) fuer fremde Anzeigen (Profil-
        Lookup). Unbekannte ID -> (0, 0, 0), nie None."""
        if not self._enabled or self._store is None:
            return 0, 0, 0
        try:
            u = (self._store.data.get("users") or {}).get(str(int(uid))) or {}
        except (TypeError, ValueError):
            return 0, 0, 0
        # Das int() lag frueher AUSSERHALB des try - ein None-Feld im Buch riss
        # damit den Profil-Lookup mit, der diese Funktion aufruft.
        return _zahl(u.get("in")), _zahl(u.get("out")), _zahl(u.get("n"))

    def _save_soon(self):
        """Speichern SAMMELN statt bei jeder Buchung neu zu schreiben.

        Vorher startete jede einzelne Coin-Bewegung einen eigenen save()-Task, und
        store.save serialisiert das ganze Buch SYNCHRON im Event-Loop. Bei einer
        Casino-Runde mit vielen Buchungen waren das dutzende volle
        Serialisierungen pro Sekunde - der Bot ruckelt dann fuer alle.
        Jetzt laeuft hoechstens EIN Speicher-Task; kommt waehrenddessen etwas
        Neues dazu, wird danach genau einmal nachgespeichert."""
        self._dirty = True
        if self._save_task is not None and not self._save_task.done():
            return
        try:
            self._save_task = asyncio.get_running_loop().create_task(self._save_loop())
        except RuntimeError:
            self._save_task = None      # kein Loop (Tests) - beim naechsten Mal
            return
        self._save_tasks.add(self._save_task)
        self._save_task.add_done_callback(self._save_tasks.discard)

    async def _save_loop(self):
        """Schreibt, solange zwischendurch neue Aenderungen aufgelaufen sind."""
        try:
            while self._dirty:
                self._dirty = False
                await asyncio.sleep(SAVE_DEBOUNCE)
                await self._store.save()
        except Exception:  # noqa: BLE001 - Speichern darf nie ein Spiel sprengen
            log.exception("Verzoegertes Speichern fehlgeschlagen")

    # --- Befehl -----------------------------------------------------------
    async def _fetch_avatar(self, user):
        try:
            return await asyncio.wait_for(user.display_avatar.with_size(128).read(), 6)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko
            return None

    async def handle(self, message):
        """Erkennt 'handel [@wer]' und liefert die Handels-Karte als Bild."""
        if not self._enabled or message.guild is None:
            return None
        cmd = ai.strip_lead(message.content or "")
        parts = cmd.lower().split()
        if not parts or parts[0] not in self._CMDS:
            return None
        target = next((m for m in message.mentions if not m.bot), None) or message.author
        u = (self._store.data.get("users") or {}).get(str(target.id)) \
            if self._store is not None else None
        # Nicht nur leer, sondern auch KEIN dict: die Typ-Schablone des Store
        # prueft nur, dass 'users' ein dict ist, nicht was darin steht. Ein
        # Eintrag als Liste/Zahl warf hier sonst AttributeError, und der Befehl
        # antwortete dauerhaft nur mit der allgemeinen Fehlermeldung.
        if not isinstance(u, dict) or not u.get("n"):
            return (f"📒 **{target.display_name}** hat noch keine Coin-Bewegung im "
                    f"Handelsbuch. Schreib was oder zock eine Runde im "
                    f"`{self._bot_name} casino`!")
        avatar = await self._fetch_avatar(target)
        balance = economy.get_coins(target.id)
        # Snapshot: record() mutiert das Live-Dict weiter, waehrend der Render-Thread
        # darueber iteriert. Eine tiefe Kopie schuetzt vor 'dict changed size'-Races.
        snapshot = copy.deepcopy(u)
        try:
            buf = await asyncio.to_thread(render.handel_card,
                                          target.display_name, avatar, snapshot, balance)
            return discord.File(buf, filename=f"handel_{target.id}.png")
        except Exception:  # noqa: BLE001 - Karte ist nice-to-have, Text geht immer
            log.exception("Handels-Karte fehlgeschlagen - Text-Fallback")
            # Der Fallback rechnete frueher EXAKT so ungeschuetzt wie die Karte,
            # die er auffangen soll: bei "in": null starb er in seiner eigenen
            # ersten Zeile an derselben Ursache. Der Nutzer bekam dann weder Bild
            # noch Text, sondern nur die Notbremse aus bot.py.
            ein, aus, anz = _zahl(u.get("in")), _zahl(u.get("out")), _zahl(u.get("n"))
            netto = ein - aus
            return (f"📒 **{target.display_name}** – {anz} Transaktionen, "
                    f"eingenommen +{numfmt.fmt(ein)}, ausgegeben -{numfmt.fmt(aus)}, "
                    f"Netto {'+' if netto >= 0 else ''}{numfmt.fmt(netto)} {economy.COIN}.")


# --- Singleton + Modul-API -------------------------------------------------
# Eine Instanz pro Prozess; economy bucht ueber handel.record(), bot.py nutzt
# setup()/handle() wie bei jedem anderen Feature-Modul.
instance = Handel()

setup = instance.setup
is_enabled = instance.is_enabled
record = instance.record
summe_von = instance.summe_von
handle = instance.handle
