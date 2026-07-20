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
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import ai
import economy
import render
from store import JsonStore

log = logging.getLogger("dcbot.handel")


class Handel:
    """Kapselt das Handelsbuch: Buchungs-Erfassung, Speicher und den Befehl."""

    _tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Berlin"))

    # So viele Tages-Buckets bzw. Einzelbuchungen bleiben je Nutzer erhalten.
    DAYS_KEPT = 60
    LAST_KEPT = 50

    # Befehlswoerter, auf die das Handelsbuch hoert.
    _CMDS = ("handel", "handelsbuch", "transaktionen", "transaktion",
             "verlauf", "trades")

    def __init__(self) -> None:
        self._enabled: bool = False
        self._bot_name: str = "Flo"
        self._store: JsonStore | None = None

    def setup(self) -> bool:
        """Aktiviert das Handelsbuch. Braucht economy (dort liegt der Coin-Topf)."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
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

    def is_enabled(self) -> bool:
        return self._enabled

    # --- Buchung ----------------------------------------------------------
    def _user(self, uid: int) -> dict:
        assert self._store is not None
        return self._store.data.setdefault("users", {}).setdefault(
            str(uid), {"in": 0, "out": 0, "n": 0, "by": {}, "days": {}, "last": []})

    def record(self, uid: int, amount: int, source: str, balance: int) -> None:
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

    def _save_soon(self) -> None:
        """Speichert asynchron (JsonStore serialisiert selbst per Lock). Ohne
        laufenden Event-Loop (Tests) passiert nichts - der naechste Lauf im Bot
        schreibt den Stand mit."""
        try:
            asyncio.get_running_loop().create_task(self._store.save())
        except RuntimeError:
            pass

    # --- Befehl -----------------------------------------------------------
    async def _fetch_avatar(self, user) -> "bytes | None":
        try:
            return await asyncio.wait_for(user.display_avatar.with_size(128).read(), 6)
        except Exception:  # noqa: BLE001 - Avatar ist nur Deko
            return None

    async def handle(self, message: discord.Message) -> "str | discord.File | None":
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
        if not u or not u.get("n"):
            return (f"📒 **{target.display_name}** hat noch keine Coin-Bewegung im "
                    f"Handelsbuch. Schreib was oder zock eine Runde im "
                    f"`{self._bot_name} casino`!")
        avatar = await self._fetch_avatar(target)
        balance = economy.get_coins(target.id)
        try:
            buf = await asyncio.to_thread(render.handel_card,
                                          target.display_name, avatar, u, balance)
            return discord.File(buf, filename=f"handel_{target.id}.png")
        except Exception:  # noqa: BLE001 - Karte ist nice-to-have, Text geht immer
            log.exception("Handels-Karte fehlgeschlagen - Text-Fallback")
            netto = u.get("in", 0) - u.get("out", 0)
            return (f"📒 **{target.display_name}** – {u.get('n', 0)} Transaktionen, "
                    f"eingenommen +{u.get('in', 0)}, ausgegeben -{u.get('out', 0)}, "
                    f"Netto {'+' if netto >= 0 else ''}{netto} {economy.COIN}.")


# --- Singleton + Modul-API -------------------------------------------------
# Eine Instanz pro Prozess; economy bucht ueber handel.record(), bot.py nutzt
# setup()/handle() wie bei jedem anderen Feature-Modul.
instance = Handel()

setup = instance.setup
is_enabled = instance.is_enabled
record = instance.record
handle = instance.handle
