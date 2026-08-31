#!/usr/bin/env python3
"""Panelprobe: das Web-Panel wirklich im Browser aufmachen und anklicken.

    python werkzeug/panelprobe.py            alle Proben
    python werkzeug/panelprobe.py --zeigen   dazu Bildschirmfotos ablegen

WOZU
====
Von den 337 Tests prueft kein einziger die Oberflaeche des Panels. Sie besteht
aus rund 1.800 Zeilen Javascript, die alles Sichtbare per innerHTML
zusammenbauen - und dafuer gab es bisher genau eine Absicherung: hinsehen.

Beim Umbau ist das zu wenig. Ein Tippfehler in einem Selektor, ein Feld, das
anders heisst als vorher, ein Knopf ohne Ereignis: nichts davon faellt in
einem Python-Test auf, nichts davon sieht man am Rueckgabewert einer Route.
Das Inventar erkennt, ob es den ENDPUNKT noch gibt - nicht, ob der KNOPF ihn
noch trifft.

Also: der echte aiohttp-Server, die echte webpanel.html, ein echter Chromium.
Angeklickt wird, was ein Mensch anklickt.

WAS HIER NICHT PASSIERT
=======================
Kein Netz nach draussen, kein echter Discord. Der Bot wird durch eine Attrappe
ersetzt, die zwei Server und ein paar Kanaele hat. Es geht nicht darum, ob die
Daten stimmen - das pruefen die Python-Tests -, sondern darum, ob die
Oberflaeche sie ANZEIGT und ob die Knoepfe die richtigen Endpunkte treffen.

Rueckgabecodes: 0 alles gut · 1 eine Probe ist gefallen · 3 Werkzeug kaputt
"""

import argparse
import json
import os
import pathlib
import sys
import tempfile
import threading

WURZEL = pathlib.Path(__file__).resolve().parent.parent
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="flobot-panelprobe-")
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
if str(WURZEL) not in sys.path:
    sys.path.insert(0, str(WURZEL))

#: Wo der mitgelieferte Chromium liegt. Playwright sucht sonst nach einer
#: Version, die es hier nicht gibt.
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


class BotAttrappe:
    """Gerade so viel Discord, dass das Panel etwas anzuzeigen hat."""

    def __init__(self):
        from types import SimpleNamespace

        def kanal(cid, name):
            return SimpleNamespace(id=cid, name=name, type=None,
                                   category=None, position=0)

        self.kanaele = [kanal(111, "allgemein"), kanal(222, "kommandos"),
                        kanal(333, "musik")]
        rechte = SimpleNamespace(administrator=True, manage_guild=True,
                                 send_messages=True, view_channel=True)
        self.guild = SimpleNamespace(
            id=77, name="Probeserver", member_count=42, owner_id=1,
            text_channels=self.kanaele, voice_channels=[], channels=self.kanaele,
            members=[], roles=[], icon=None,
            me=SimpleNamespace(id=1, guild_permissions=rechte),
            get_member=lambda _i: None, system_channel=self.kanaele[0])
        self.guilds = [self.guild]
        self.user = SimpleNamespace(id=1, name="Flo", display_avatar=None)
        self.latency = 0.05

    def get_guild(self, gid):
        return self.guild if int(gid) == 77 else None

    def is_closed(self):
        return False


class Probe:
    """Ein Browser, ein Server, eine Reihe von Behauptungen."""

    def __init__(self, laut=True, bilder=False):
        self.laut = laut
        self.bilder = bilder
        self.fehler = []
        self.geprueft = 0
        self.adresse = None
        self._runner = None
        self._loop = None
        self._thread = None

    def _sagen(self, text):
        if self.laut:
            print(text)

    # -- Server ---------------------------------------------------------------
    def server_starten(self):
        """Den echten Panel-Server in einem eigenen Thread hochfahren.

        Eigener Thread mit eigenem Event-Loop, damit der Browser (der synchron
        bedient wird) und der Server sich nicht gegenseitig blockieren.
        """
        import asyncio
        import guildcfg
        import features
        import economy
        import webpanel

        os.environ["WEBPANEL_AUTH"] = "0"      # ohne Login - so will es der Betreiber
        economy.setup()
        guildcfg.setup()
        features.setup()
        panel = webpanel.WebPanel()
        panel._client = BotAttrappe()
        # Ohne geladene Module waeren im Panel ALLE Schalter gesperrt, und die
        # Probe koennte keinen einzigen Klick pruefen. Die Flags kommen sonst
        # aus bot.py; hier setzen wir sie selbst.
        import bot
        bot.FEATURE_LOADED = {f["key"]: True for f in features.CATALOG}

        bereit = threading.Event()

        def lauf():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def start():
                from aiohttp import web
                app = panel._build_app()
                runner = web.AppRunner(app)
                await runner.setup()
                seite = web.TCPSite(runner, "127.0.0.1", 0)
                await seite.start()
                self._runner = runner
                port = seite._server.sockets[0].getsockname()[1]
                self.adresse = f"http://127.0.0.1:{port}"
                bereit.set()

            self._loop.run_until_complete(start())
            self._loop.run_forever()

        self._thread = threading.Thread(target=lauf, daemon=True)
        self._thread.start()
        if not bereit.wait(30):
            raise SystemExit("Panel-Server ist nicht hochgekommen")
        self._sagen(f"  Server laeuft auf {self.adresse}")
        return panel

    def server_stoppen(self):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # -- Behauptungen ---------------------------------------------------------
    def stimmt(self, bedingung, was):
        self.geprueft += 1
        if bedingung:
            self._sagen(f"  ok   {was}")
        else:
            self.fehler.append(was)
            self._sagen(f"  NEIN {was}")
        return bool(bedingung)

    # -- Der Durchgang --------------------------------------------------------
    def laufen(self):
        from playwright.sync_api import sync_playwright

        self.server_starten()
        bilderordner = WURZEL / "inventar" / "panelbilder"
        if self.bilder:
            bilderordner.mkdir(parents=True, exist_ok=True)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=CHROME)
                seite = browser.new_page(viewport={"width": 1400, "height": 1000})
                # Javascript-Fehler sind hier KEINE Nebensache: eine geworfene
                # Ausnahme laesst den halben Aufbau stehen, und die Seite sieht
                # trotzdem gefuellt aus.
                krachs = []
                seite.on("pageerror", lambda e: krachs.append(str(e)))
                # Fehlgeschlagene Anfragen MIT Adresse festhalten: 'irgendwas
                # war 404' ist keine Meldung, mit der man arbeiten kann.
                seite.on("response", lambda r: krachs.append(
                    f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

                self._uebersicht(seite)
                self._servers_und_einstellungen(seite)
                self._funktionen(seite)
                self._nutzer(seite)

                if self.bilder:
                    seite.screenshot(path=str(bilderordner / "zuletzt.png"),
                                     full_page=True)
                self.stimmt(not krachs,
                            f"keine Javascript-Fehler ({krachs[:3]})")
                browser.close()
        finally:
            self.server_stoppen()
        return not self.fehler

    def _oeffnen(self, seite, was=""):
        seite.goto(self.adresse, wait_until="networkidle")
        seite.wait_for_timeout(400)

    def _uebersicht(self, seite):
        self._sagen("\n  Uebersicht")
        self._oeffnen(seite)
        self.stimmt(seite.locator("nav a, .nav a, [data-view]").count() >= 5,
                    "die Seitenleiste hat ihre Eintraege")
        self.stimmt("Flo" in seite.content(), "der Botname steht auf der Seite")
        self.stimmt(seite.locator("#loginCard").count() == 0
                    or not seite.locator("#loginCard").first.is_visible(),
                    "ohne WEBPANEL_AUTH kommt keine Login-Maske")

    def _reiter(self, seite, name):
        """Auf einen Reiter klicken - so, wie ein Mensch es tut.

        Nicht die Javascript-Funktion direkt rufen: das ganze Panel steckt in
        einem IIFE, seine Funktionen sind bewusst nicht global. Das ist richtig
        so, und es zwingt diese Probe dazu, den echten Weg zu gehen: klicken.
        """
        seite.click(f'.nav-i[data-view="{name}"]')
        seite.wait_for_timeout(800)

    def _servers_und_einstellungen(self, seite):
        """Der Kern: die neue, zusammengefuehrte Einstellungsliste."""
        self._sagen("\n  Server-Einstellungen (die neue Liste)")
        self._reiter(seite, "servers")
        self.stimmt(seite.locator(".srv-card[data-gid]").count() >= 1,
                    "die Server-Uebersicht zeigt den Server")
        seite.locator(".srv-card[data-gid]").first.click()
        seite.wait_for_timeout(1200)
        text = seite.inner_text("#content")

        self.stimmt("Grundlegendes" in text,
                    "der Block 'Grundlegendes' steht oben")
        for ueberschrift in ("Musik", "Casino", "Moderation"):
            self.stimmt(ueberschrift in text,
                        f"die Funktion '{ueberschrift}' hat einen eigenen Block")
        self.stimmt("Lautstärke" in text,
                    "die Lautstaerke steht beim Block Musik")
        self.stimmt("Weitere Funktionen" in text,
                    "Funktionen ohne Einstellungen stehen gesammelt am Ende")

        # Der eigentliche Grund fuer den Umbau: 'Bayrisch-Modus' stand zweimal
        # mit demselben Wortlaut untereinander.
        self.stimmt(text.count("Bayrisch-Modus") == 1,
                    "'Bayrisch-Modus' steht nur noch EINMAL da")
        self.stimmt("Boarisch reden" in text,
                    "der Zustand darunter heisst jetzt anders als die Funktion")

        # Bedienelemente muessen wirklich da sein, nicht nur die Ueberschriften.
        self.stimmt(seite.locator(".toggle[data-fkey]").count() >= 20,
                    "jede Funktion hat ihren Schalter")
        self.stimmt(seite.locator(".cfg-save").count() >= 10,
                    "die Einstellwerte haben ihre OK-Knoepfe")
        self.stimmt(seite.locator("#wdlGo").count() == 1,
                    "'Wort des Tages jetzt starten' ist noch da")
        self.stimmt(seite.locator("#cfgBack").count() == 1,
                    "der Weg zurueck ist da")

        # Und ein Klick muss wirklich beim Server ankommen.
        gerufen = []
        seite.on("request", lambda r: gerufen.append(r.url)
                 if "/api/" in r.url else None)
        offen = seite.locator('.toggle[data-fkey]:not([data-lock="1"])')
        self.stimmt(offen.count() >= 1,
                    "mindestens ein Funktionsschalter ist bedienbar")
        offen.first.click()
        seite.wait_for_timeout(600)
        self.stimmt(any("/api/feature" in u for u in gerufen),
                    "ein Klick auf einen Funktionsschalter ruft /api/feature")

    def _funktionen(self, seite):
        self._sagen("\n  Die uebrigen Reiter")
        for name, erwartet in (("features", "aktiv"), ("users", ""),
                               ("coins", ""), ("overview", "")):
            self._reiter(seite, name)
            text = seite.inner_text("#content")
            self.stimmt(len(text.strip()) > 40,
                        f"der Reiter '{name}' baut sich auf")
            if erwartet:
                self.stimmt(erwartet in text,
                            f"der Reiter '{name}' zeigt '{erwartet}'")

    def _nutzer(self, seite):
        """Die BotSicht gesondert: sie oeffnet eine Live-Leitung."""
        self._sagen("\n  BotSicht")
        self._reiter(seite, "botsicht")
        seite.wait_for_timeout(1200)
        self.stimmt(len(seite.inner_text("#content").strip()) > 40,
                    "die BotSicht baut sich auf")
        # Wieder weg, damit die Leitung gekappt wird (bsStop).
        self._reiter(seite, "overview")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--zeigen", action="store_true",
                   help="Bildschirmfotos nach inventar/panelbilder/ legen")
    p.add_argument("--leise", action="store_true")
    a = p.parse_args(argv)
    try:
        probe = Probe(laut=not a.leise, bilder=a.zeigen)
        gut = probe.laufen()
    except ImportError as exc:
        print(f"Playwright fehlt ({exc}) - Probe uebersprungen.")
        return 0
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 3
    print(f"\n  {probe.geprueft - len(probe.fehler)}/{probe.geprueft} Proben gut")
    if probe.fehler:
        print("  Gefallen:\n    " + "\n    ".join(probe.fehler))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
