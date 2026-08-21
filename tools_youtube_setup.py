"""Bringt YouTube wieder zum Laufen - und sagt genau, woran es gerade haengt.

YouTube verlangt inzwischen fuer fast jeden Zugang ein sogenanntes "PO Token".
yt-dlp kann so eines NICHT selbst erzeugen. Bleibt die IP des Servers einmal
markiert ("Sign in to confirm you're not a bot"), gibt es genau drei Wege
zurueck, und dieses Werkzeug probiert sie der Reihe nach durch:

  1. Ein Client, der ohne PO Token auskommt (tv, android_vr, web_embedded).
     Kostet nichts, braucht kein Konto - wird zuerst probiert.
  2. Cookies eines angemeldeten Kontos.        ->  k y browser firefox
                                                   k y datei /pfad/cookies.txt
  3. Ein PO-Token-Anbieter oder ein Proxy.     ->  k y  sagt, wie.

Aufruf:
    bash k y                      pruefen: was geht, was nicht, was tun
    bash k y browser firefox      Cookies aus einem Browser auf DIESEM Rechner
    bash k y datei cookies.txt    eine exportierte cookies.txt einrichten

Kein Teil des Bots. Spielt nichts ab, laedt nichts herunter.
"""

import os
import shutil
import sys

from arzt import Arzt

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dann eben ohne .env
    load_dotenv = None

HIER = os.path.dirname(os.path.abspath(__file__))
ENV_DATEI = os.path.join(HIER, ".env")

# Ein Video, das es seit Jahren gibt und das nirgends gesperrt ist.
PROBE = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

# Diese drei Clients brauchen laut yt-dlp KEIN PO Token - nur sie haben auf
# einem nackten Server ueberhaupt eine Chance. Der Rest steht dahinter, weil er
# ohne Token oder Anmeldung frueher oder spaeter abgewiesen wird.
OHNE_POT = ("tv", "android_vr", "web_embedded")
MIT_POT = ("tv_simply", "ios", "mweb", "web_safari", "android")

# Browser, aus denen yt-dlp Cookies lesen kann.
BROWSER = ("firefox", "chromium", "chrome", "brave", "edge", "opera", "vivaldi",
           "safari", "whale")


class Stumm:
    """Verschluckt yt-dlps eigene Ausgaben.

    quiet/no_warnings reichen NICHT: Fehler schreibt yt-dlp trotzdem direkt
    nach stderr. Auf einem Handy-Display ist der Bericht damit unlesbar - und
    genau da wird er gelesen. Wir sagen den Grund selbst, in einer Zeile."""

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


class YoutubeSetup(Arzt):
    """Findet den Weg, auf dem YouTube auf DIESEM Rechner noch funktioniert."""

    NAME = "Flo - YouTube einrichten"

    def __init__(self):
        super().__init__()
        self.ydl = None
        self.gefunden = []      # Clients, die durchkamen
        self.gruende = []       # warum die anderen nicht

    # --- Vorbereitung -------------------------------------------------------
    def _env_lesen(self):
        if load_dotenv is not None and os.path.exists(ENV_DATEI):
            load_dotenv(ENV_DATEI)
            return True
        return False

    def _ytdlp(self):
        """yt-dlp laden - ohne das geht hier gar nichts."""
        if self.ydl is not None:
            return self.ydl
        try:
            import yt_dlp
        except ImportError:
            self.fehler("yt-dlp fehlt. Ohne das kann Flo keine Musik abspielen.")
            self.merke("yt-dlp fehlt",
                       "venv/bin/pip install -U yt-dlp   und dann  k r")
            return None
        self.ydl = yt_dlp
        return yt_dlp

    # --- Schritt 1: Was ist ueberhaupt eingerichtet? ------------------------
    def lage(self):
        self.titel("1. Was ist eingerichtet?")
        if self._env_lesen():
            self.ok(f".env gelesen: {ENV_DATEI}")
        else:
            self.warn("keine .env gefunden - nutze nur die Umgebung.")

        yt_dlp = self._ytdlp()
        if yt_dlp is None:
            return False
        fassung = getattr(getattr(yt_dlp, "version", None), "__version__", "?")
        self.ok(f"yt-dlp {fassung}")
        self.info("Ist die aelter als ein paar Wochen, ist das haeufig schon "
                  "der ganze Fehler:  venv/bin/pip install -U yt-dlp")

        datei = self.cookie_datei()
        if datei:
            self.ok(f"Cookie-Datei: {datei}")
        else:
            self.info("Cookie-Datei: keine (das ist erstmal in Ordnung)")

        browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()
        if browser:
            self.ok(f"Cookies aus Browser: {browser}")

        proxy = os.getenv("YTDLP_PROXY", "").strip()
        if proxy:
            self.ok(f"Eigener Ausgang (YTDLP_PROXY): {self.maskiere(proxy)}")

        if os.getenv("YTDLP_PO_TOKEN", "").strip():
            self.ok("PO Token von Hand hinterlegt (YTDLP_PO_TOKEN)")
        if self.pot_anbieter():
            self.ok("PO-Token-Anbieter laeuft mit (bgutil) - sehr gut.")

        fest = os.getenv("YTDLP_PLAYER_CLIENT", "").strip()
        if fest:
            self.warn(f"YTDLP_PLAYER_CLIENT={fest} nagelt den Zugang fest. "
                      "Flo probiert dann NICHTS anderes mehr.")
            self.info("Wenn YouTube klemmt: diese Zeile aus der .env nehmen.")
        return True

    @staticmethod
    def pot_anbieter():
        """Laeuft ein PO-Token-Anbieter-Plugin mit?"""
        try:
            import importlib.util
            return any(importlib.util.find_spec(n) is not None
                       for n in ("bgutil_ytdlp_pot_provider",
                                 "yt_dlp_plugins.extractor.getpot_bgutil"))
        except Exception:  # noqa: BLE001 - Diagnose darf nie etwas umwerfen
            return False

    @staticmethod
    def cookie_datei():
        """Dieselbe Suche wie im Bot: erst die .env, dann die ueblichen Orte."""
        datei = os.getenv("YTDLP_COOKIES", "").strip()
        if datei and os.path.isfile(datei):
            return datei
        for ordner in (HIER, os.path.join(HIER, "data"),
                       os.getenv("DATA_DIR", "").strip() or HIER):
            for name in ("cookies.txt", "youtube.txt", "youtube_cookies.txt"):
                pfad = os.path.join(ordner, name)
                try:
                    if os.path.isfile(pfad) and os.path.getsize(pfad) > 0:
                        return pfad
                except OSError:
                    continue
        return ""

    # --- Ein einzelner echter Versuch --------------------------------------
    def versuch(self, client=None, cookies=None, browser=None):
        """Holt die Probe wirklich ab. Gibt (True, Titel) oder (False, Grund)."""
        yt_dlp = self._ytdlp()
        if yt_dlp is None:
            return False, "yt-dlp fehlt"
        opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "noplaylist": True, "socket_timeout": 20, "logger": Stumm()}
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        if cookies:
            opts["cookiefile"] = cookies
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
        proxy = os.getenv("YTDLP_PROXY", "").strip()
        if proxy:
            opts["proxy"] = proxy
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(PROBE, download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp wirft viele Arten
            return False, self.grund(exc)
        if not info or not info.get("url"):
            # Durchgekommen, aber ohne Stream-Adresse - das waere im Betrieb
            # genauso kaputt, nur mit einer anderen Fehlermeldung.
            return False, "kein abspielbarer Ton dabei"
        return True, str(info.get("title") or "")[:45]

    @staticmethod
    def grund(exc):
        """Aus einem yt-dlp-Fehler einen kurzen deutschen Grund machen."""
        text = f"{exc}".lower()
        if "not a bot" in text or "sign in to confirm" in text:
            return "Bot-Sperre"
        if "po token" in text or "po_token" in text:
            return "PO Token noetig"
        if "proxy" in text and ("403" in text or "tunnel" in text):
            return "Proxy laesst nicht durch"
        if "age" in text and "confirm" in text:
            return "Altersfreigabe"
        if "private" in text:
            return "privates Video"
        if "requested format" in text:
            return "kein passendes Format"
        if "unable to connect" in text or "timed out" in text:
            return "kein Netz zu YouTube"
        return f"{exc}".replace("\n", " ")[:70]

    # --- Schritt 2: Der Durchlauf ------------------------------------------
    def durchprobieren(self):
        self.titel("2. Welcher Zugang geht gerade?")
        datei = self.cookie_datei()
        browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "").strip()

        for client in OHNE_POT:
            geht, was = self.versuch(client, cookies=datei, browser=browser)
            if geht:
                self.ok(f"player_client={client} geht  ({was})")
                self.gefunden.append(client)
            else:
                self.warn(f"player_client={client}: {was}")
                self.gruende.append(was)

        if self.gefunden:
            return
        # Erst wenn die drei ohne Token nicht reichen, lohnt der Rest.
        self.info("Die drei Clients ohne PO Token gehen nicht. Probiere den Rest...")
        for client in MIT_POT:
            geht, was = self.versuch(client, cookies=datei, browser=browser)
            if geht:
                self.ok(f"player_client={client} geht  ({was})")
                self.gefunden.append(client)
                return
            self.warn(f"player_client={client}: {was}")
            self.gruende.append(was)

    # --- Schritt 3: Klartext ------------------------------------------------
    def urteil(self):
        self.titel("3. Was bedeutet das?")
        if self.gefunden:
            self.ok("YouTube geht auf diesem Rechner. Flo probiert genau diese "
                    "Clients in genau dieser Reihenfolge durch.")
            self.info(f"Funktioniert hat: {', '.join(self.gefunden)}")
            self.info("Nichts eintragen noetig - Flo findet das selbst.")
            return
        # Nicht raten, WARUM nichts ging - die Versuche haben es gesagt.
        netz = ("Proxy laesst nicht durch", "kein Netz zu YouTube")
        if self.gruende and all(g in netz for g in self.gruende):
            self.fehler("Dieser Rechner kommt gar nicht erst zu YouTube - das "
                        "ist KEINE Bot-Sperre, sondern das Netz.")
            proxy = os.getenv("YTDLP_PROXY", "").strip()
            if proxy:
                self.info(f"YTDLP_PROXY ist gesetzt ({self.maskiere(proxy)}) und "
                          "laesst nicht durch. Zum Pruefen einmal ohne:")
                self.info("  YTDLP_PROXY aus der .env nehmen und  k y  nochmal.")
                self.merke("Der eingetragene Proxy blockt",
                           "YTDLP_PROXY in der .env pruefen oder entfernen.")
            else:
                self.info("Firewall, DNS oder die Internetverbindung pruefen.")
                self.merke("Keine Verbindung zu YouTube",
                           "Netz/Firewall des Servers pruefen, dann  k y  nochmal.")
            return
        self.fehler("KEIN Zugang geht. YouTube blockt diese IP komplett.")
        if not self.cookie_datei() and not os.getenv("YTDLP_COOKIES_FROM_BROWSER"):
            self.info("Der einfachste Weg ist ein angemeldetes Konto:")
            self.info("")
            self.info("  Browser auf DIESEM Rechner (z. B. in der noVNC-Oberflaeche):")
            self.info("     k y browser firefox")
            self.info("")
            self.info("  Oder cookies.txt woanders exportiert und hierher kopiert:")
            self.info("     k y datei /pfad/zu/cookies.txt")
            self.info("")
            self.warn("Nimm dafuer einen WEGWERF-Google-Account. YouTube sperrt "
                      "Konten, deren Cookies von einem Server aus benutzt werden.")
            self.merke("YouTube blockt diese IP",
                       "Cookies einrichten:  k y browser firefox   ODER   "
                       "k y datei /pfad/cookies.txt")
        else:
            self.warn("Cookies sind eingerichtet, reichen aber nicht mehr.")
            self.info("Cookies laufen ab. Neu exportieren - und diesmal:")
            self.info("  1. Privates Fenster oeffnen, bei YouTube anmelden")
            self.info("  2. In DEMSELBEN Tab youtube.com/robots.txt aufrufen")
            self.info("  3. Cookies exportieren")
            self.info("  4. Das private Fenster schliessen, OHNE dich abzumelden")
            self.info("     (sonst dreht YouTube die Cookies sofort wieder weiter)")
            self.merke("Cookies reichen nicht mehr",
                       "Neu exportieren - aus einem PRIVATEN Fenster, siehe oben.")
        if not self.pot_anbieter():
            self.info("")
            self.info("Dauerhaft ohne Konto geht es mit einem PO-Token-Anbieter:")
            self.info("  venv/bin/pip install -U bgutil-ytdlp-pot-provider")
            self.info("  docker run -d --name bgutil -p 4416:4416 "
                      "brainicism/bgutil-ytdlp-pot-provider")
            self.info("  danach:  k r")
        if not os.getenv("YTDLP_PROXY", "").strip():
            self.info("")
            self.info("Oder ein zweiter Weg ins Netz (VPN/Hotspot/kleiner Server):")
            self.info("  YTDLP_PROXY=socks5://127.0.0.1:1080   in die .env")
        self.info("")
        self.ok("Bis dahin spielt Flo denselben Song von SoundCloud - Musik "
                "laeuft also weiter, nur nicht von YouTube.")

    # --- .env schreiben -----------------------------------------------------
    def env_setzen(self, schluessel, wert):
        """Eine Zeile in der .env setzen oder ersetzen. Legt sie notfalls an."""
        try:
            if os.path.exists(ENV_DATEI):
                with open(ENV_DATEI, encoding="utf-8") as f:
                    zeilen = f.read().splitlines()
            else:
                zeilen = []
            neu = []
            gesetzt = False
            for zeile in zeilen:
                if zeile.strip().startswith(f"{schluessel}="):
                    if not gesetzt:
                        neu.append(f"{schluessel}={wert}")
                        gesetzt = True
                    continue     # doppelte Eintraege gleich mit aufraeumen
                neu.append(zeile)
            if not gesetzt:
                neu.append(f"{schluessel}={wert}")
            with open(ENV_DATEI, "w", encoding="utf-8") as f:
                f.write("\n".join(neu).rstrip("\n") + "\n")
            os.chmod(ENV_DATEI, 0o600)
        except OSError as exc:
            self.fehler(f".env nicht schreibbar: {exc}")
            self.info(f"Dann von Hand eintragen:  {schluessel}={wert}")
            return False
        self.ok(f"In die .env geschrieben:  {schluessel}={wert}")
        return True

    # --- Unterbefehl: Browser ----------------------------------------------
    def aus_browser(self, name=""):
        self.titel("Cookies aus einem Browser")
        self._env_lesen()
        if self._ytdlp() is None:
            return self.bericht()
        namen = [name] if name else list(BROWSER)
        if name and name not in BROWSER:
            self.fehler(f"'{name}' kennt yt-dlp nicht.")
            self.info(f"Moeglich: {', '.join(BROWSER)}")
            return self.bericht()
        for browser in namen:
            geht, was = self.versuch("tv", browser=browser)
            if geht:
                self.ok(f"{browser}: geht  ({was})")
                self.env_setzen("YTDLP_COOKIES_FROM_BROWSER", browser)
                self.info("Jetzt neu starten:   k r")
                return self.bericht()
            self.warn(f"{browser}: {was}")
        self.fehler("Aus keinem Browser kamen brauchbare Cookies.")
        self.info("Der Browser muss auf DIESEM Rechner laufen und bei YouTube "
                  "angemeldet sein. Sonst: Cookies exportieren und dann")
        self.info("  k y datei /pfad/zu/cookies.txt")
        self.merke("Browser-Cookies gehen nicht",
                   "Cookies exportieren und  k y datei <pfad>  benutzen.")
        return self.bericht()

    # --- Unterbefehl: Datei -------------------------------------------------
    def aus_datei(self, pfad=""):
        self.titel("Cookie-Datei einrichten")
        self._env_lesen()
        if self._ytdlp() is None:
            return self.bericht()
        if not pfad:
            self.fehler("Kein Pfad angegeben.")
            self.info("So geht es:  k y datei /pfad/zu/cookies.txt")
            return self.bericht()
        pfad = os.path.expanduser(pfad)
        if not os.path.isfile(pfad):
            self.fehler(f"Da liegt keine Datei: {pfad}")
            return self.bericht()
        if os.path.getsize(pfad) == 0:
            self.fehler("Die Datei ist leer.")
            return self.bericht()
        with open(pfad, encoding="utf-8", errors="replace") as f:
            kopf = f.read(4096)
        if kopf.lstrip().startswith(("{", "[")):
            self.fehler("Das ist JSON, kein Netscape-Format.")
            self.info("Die Erweiterung muss 'Netscape'/'cookies.txt' exportieren.")
            self.merke("Cookie-Datei im falschen Format",
                       "Im Netscape-Format exportieren, nicht als JSON.")
            return self.bericht()
        if "youtube.com" not in kopf and ".google.com" not in kopf:
            self.warn("In der Datei steht nichts von youtube.com - das ist "
                      "vermutlich der Export der falschen Seite.")

        geht, was = self.versuch("tv", cookies=pfad)
        if not geht:
            self.fehler(f"Mit diesen Cookies geht YouTube nicht: {was}")
            self.info("Beim Export das private Fenster NICHT abmelden und erst")
            self.info("danach schliessen - sonst sind die Cookies sofort tot.")
            self.merke("Cookies funktionieren nicht",
                       "Neu exportieren, aus einem privaten Fenster.")
            return self.bericht()
        self.ok(f"Die Cookies gehen  ({was})")

        ziel = os.path.join(HIER, "data", "cookies.txt")
        try:
            os.makedirs(os.path.dirname(ziel), exist_ok=True)
            if os.path.abspath(pfad) != os.path.abspath(ziel):
                shutil.copyfile(pfad, ziel)
            os.chmod(ziel, 0o600)
        except OSError as exc:
            self.warn(f"Konnte nicht nach {ziel} kopieren: {exc}")
            ziel = pfad
        else:
            self.ok(f"Abgelegt: {ziel}  (nur fuer dich lesbar)")
        self.env_setzen("YTDLP_COOKIES", ziel)
        self.info("Jetzt neu starten:   k r")
        return self.bericht()

    # --- Abschluss ----------------------------------------------------------
    def bericht(self):
        return super().bericht(schluss=(
            "\n  Danach pruefen:\n    k y      (geht YouTube wieder?)"
            "\n    k m      (geht die ganze Musik-Strecke?)"))

    def lauf(self, argumente=None):
        argumente = list(argumente or [])
        befehl = (argumente[0] if argumente else "").lower()
        rest = argumente[1] if len(argumente) > 1 else ""
        self._schreib(f"\033[1m{self.NAME}\033[0m", self.NAME)
        if befehl in ("browser", "b"):
            return self.aus_browser(rest.lower())
        if befehl in ("datei", "d", "cookies"):
            return self.aus_datei(rest)
        if befehl:
            self.fehler(f"Unbekannt: {befehl}")
            self.info("Moeglich:  k y  |  k y browser <name>  |  k y datei <pfad>")
            return self.bericht()
        if not self.lage():
            return self.bericht()
        self.durchprobieren()
        self.urteil()
        return self.bericht()


instance = YoutubeSetup()
lauf = instance.lauf

if __name__ == "__main__":
    sys.exit(lauf(sys.argv[1:]))
