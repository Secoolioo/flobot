"""Sagt in Klartext, warum Musik oder Spotify nicht geht.

Wie tools_ki_check.py, nur fuer die Musik-Strecke: Voraussetzungen (yt-dlp,
ffmpeg, PyNaCl), Spotify-Zugangsdaten, ein echter Token-Abruf und ein echter
Track-Abruf. Statt eines Tracebacks im Journal steht danach ein deutscher Satz
und der naechste Schritt da.

    /opt/flobot/venv/bin/python /opt/flobot/tools_musik_check.py
    bash k m

Kein Teil des Bots. Aendert nichts. Zugangsdaten werden NIE vollstaendig
ausgegeben, auch nicht im Fehlerfall.
"""

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dann eben ohne .env
    load_dotenv = None

# Ein bekannter oeffentlicher Track, an dem sich der Abruf pruefen laesst.
PROBE_TRACK = "4cOdK2wGLETKBW3PvgPWqT"
TIMEOUT = 15


class MusikCheck:
    """Prueft die Musik-Strecke Schritt fuer Schritt."""

    def __init__(self):
        self.sp_id = ""
        self.sp_secret = ""
        self.probleme = []

    # --- Ausgabe (gleiche Optik wie die KI-Diagnose) ------------------------
    def titel(self, text):
        print(f"\n\033[1m{text}\033[0m")

    def ok(self, text):
        print(f"  \033[32mOK\033[0m    {text}")

    def warn(self, text):
        print(f"  \033[33m!\033[0m     {text}")

    def fehler(self, text):
        print(f"  \033[31mFEHLER\033[0m {text}")

    def merke(self, ueberschrift, was_tun):
        if (ueberschrift, was_tun) not in self.probleme:
            self.probleme.append((ueberschrift, was_tun))

    @staticmethod
    def maskiere(wert):
        if not wert:
            return "(leer)"
        if len(wert) <= 12:
            return f"{wert[:2]}...{wert[-2:]} ({len(wert)} Zeichen)"
        return f"{wert[:4]}...{wert[-4:]} ({len(wert)} Zeichen)"

    # --- Schritt 1: Voraussetzungen ----------------------------------------
    def werkzeuge_pruefen(self):
        self.titel("1. Voraussetzungen")
        pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if load_dotenv is not None and os.path.exists(pfad):
            load_dotenv(pfad)
            self.ok(f".env gelesen: {pfad}")
        else:
            self.warn("keine .env gelesen - nutze nur die Umgebung.")

        for paket, wofuer in (("yt_dlp", "YouTube abspielen"),
                              ("nacl", "Voice (PyNaCl)")):
            try:
                modul = __import__(paket)
            except ImportError:
                self.fehler(f"Paket '{paket}' fehlt - {wofuer} geht nicht.")
                self.merke(f"Paket {paket} fehlt",
                           f"venv/bin/pip install {'yt-dlp' if paket == 'yt_dlp' else 'PyNaCl'}")
                continue
            version = getattr(modul, "version", None)
            version = getattr(version, "__version__", None) or getattr(
                modul, "__version__", "?")
            self.ok(f"{paket} da (Version {version})")
            if paket == "yt_dlp":
                self._ytdlp_alter(str(version))

        if shutil.which("ffmpeg"):
            self.ok("ffmpeg gefunden")
        else:
            self.fehler("ffmpeg fehlt - ohne das spielt gar nichts.")
            self.merke("ffmpeg fehlt", "apt install ffmpeg")

    def _ytdlp_alter(self, version):
        """yt-dlp veraltet schnell: YouTube aendert staendig etwas, und eine alte
        Fassung faellt dann bei JEDEM Video um - auch bei Spotify-Links, weil die
        am Ende ueber YouTube laufen."""
        # yt-dlp versioniert nach Datum: 2026.08.19
        treffer = re.match(r"(\d{4})\.(\d{1,2})", version or "")
        if not treffer:
            return
        jahr, monat = int(treffer.group(1)), int(treffer.group(2))
        import time as _t
        jetzt = _t.gmtime()
        monate = (jetzt.tm_year - jahr) * 12 + (jetzt.tm_mon - monat)
        if monate >= 3:
            self.warn(f"yt-dlp ist ~{monate} Monate alt - YouTube bricht damit gern.")
            self.merke("yt-dlp ist veraltet",
                       "venv/bin/pip install -U yt-dlp   danach: systemctl restart flobot")

    # --- HTTP-Helfer --------------------------------------------------------
    def _anfrage(self, url, daten=None, kopf=None):
        """(status, body). Wirft nicht - Fehler sind Daten."""
        rumpf = None
        if daten is not None:
            rumpf = "&".join(f"{k}={v}" for k, v in daten.items()).encode()
        req = urllib.request.Request(url, data=rumpf, headers=kopf or {},
                                     method="POST" if daten is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as antwort:
                return antwort.status, antwort.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            return 0, str(exc.reason)
        except (socket.timeout, TimeoutError):
            return 0, f"Zeitueberschreitung nach {TIMEOUT}s"

    @staticmethod
    def _meldung(body):
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            return (body or "").strip()[:300]
        for schluessel in ("error_description", "error", "message"):
            wert = d.get(schluessel)
            if isinstance(wert, dict):
                wert = wert.get("message") or wert.get("status")
            if wert:
                return str(wert)[:300]
        return str(d)[:300]

    # --- Schritt 2: Spotify -------------------------------------------------
    def spotify_pruefen(self):
        self.titel("2. Spotify")
        self.sp_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self.sp_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        print(f"        Client-ID    : {self.maskiere(self.sp_id)}")
        print(f"        Client-Secret: {self.maskiere(self.sp_secret)}")

        if not (self.sp_id and self.sp_secret):
            self.fehler("Zugangsdaten fehlen - Spotify-Links koennen gar nicht "
                        "aufgeloest werden.")
            self.merke("Spotify-Zugangsdaten fehlen",
                       "Unter developer.spotify.com eine App anlegen und "
                       "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in die .env "
                       "schreiben, dann: systemctl restart flobot")
            return
        for name, wert in (("SPOTIFY_CLIENT_ID", self.sp_id),
                           ("SPOTIFY_CLIENT_SECRET", self.sp_secret)):
            if wert.strip("\"' ") != wert:
                self.warn(f"{name} hat Anfuehrungszeichen/Leerzeichen drumherum.")
                self.merke("Zugangsdaten mit Anfuehrungszeichen",
                           "In der .env OHNE Quotes schreiben.")

        # Token holen - genau der Aufruf, den music.py macht.
        auth = base64.b64encode(f"{self.sp_id}:{self.sp_secret}".encode()).decode()
        status, body = self._anfrage(
            "https://accounts.spotify.com/api/token",
            daten={"grant_type": "client_credentials"},
            kopf={"Authorization": f"Basic {auth}",
                  "Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            self._token_deuten(status, body)
            return
        try:
            token = json.loads(body).get("access_token", "")
        except ValueError:
            token = ""
        if not token:
            self.fehler("Token-Antwort ohne Token.")
            self.merke("Spotify antwortet unerwartet", "Meldung oben lesen.")
            return
        self.ok("Token bekommen - die Zugangsdaten stimmen.")

        # Und jetzt ein echter Track-Abruf.
        status, body = self._anfrage(
            f"https://api.spotify.com/v1/tracks/{PROBE_TRACK}",
            kopf={"Authorization": f"Bearer {token}"})
        if status == 200:
            try:
                d = json.loads(body)
                titel = d.get("name", "?")
                wer = ", ".join(a.get("name", "") for a in d.get("artists", []))
                self.ok(f"Track-Abruf geht: {wer} - {titel}")
                self.ok("Die Spotify-Strecke funktioniert.")
            except ValueError:
                self.warn("Track-Antwort war kein erwartetes JSON.")
            return
        self._track_deuten(status, body)

    def _token_deuten(self, status, body):
        meldung = self._meldung(body)
        if status in (400, 401):
            self.fehler(f"Zugangsdaten abgelehnt (HTTP {status}). Spotify sagt: {meldung}")
            self.merke("Spotify-Zugangsdaten stimmen nicht (mehr)",
                       "Unter developer.spotify.com im Dashboard pruefen: existiert "
                       "die App noch, und ist das Secret noch dasselbe? Neues Secret "
                       "erzeugen, in die .env, dann: systemctl restart flobot")
        elif status == 429:
            self.fehler(f"Ratenlimit (429). Spotify sagt: {meldung}")
            self.merke("Zu viele Anfragen", "Kurz warten, dann nochmal.")
        elif status == 0:
            self.fehler(f"accounts.spotify.com nicht erreichbar: {meldung}")
            self.merke("Spotify nicht erreichbar",
                       "Netz/Firewall pruefen:  curl -sS -m5 -o /dev/null "
                       "-w '%{http_code}' https://accounts.spotify.com")
        else:
            self.fehler(f"Token-Abruf: HTTP {status}. Spotify sagt: {meldung}")
            self.merke(f"Unerwarteter Status {status}", "Meldung oben lesen.")

    def _track_deuten(self, status, body):
        meldung = self._meldung(body)
        if status == 401:
            self.fehler(f"Token wird beim Track-Abruf abgelehnt (401): {meldung}")
            self.merke("Token gilt nicht fuer die API",
                       "App im Spotify-Dashboard pruefen (existiert sie noch?).")
        elif status == 403:
            self.fehler(f"Zugriff verweigert (403): {meldung}")
            self.merke("App darf die API nicht nutzen",
                       "Im Spotify-Dashboard pruefen, ob die App noch aktiv und "
                       "nicht im eingeschraenkten Modus ist.")
        elif status == 404:
            self.fehler(f"Testtrack nicht gefunden (404): {meldung}")
            self.merke("Track-Abruf geht nicht",
                       "Kann am Markt liegen - mit einem eigenen Link gegenpruefen.")
        elif status == 429:
            self.fehler(f"Ratenlimit (429): {meldung}")
            self.merke("Zu viele Anfragen", "Kurz warten.")
        else:
            self.fehler(f"Track-Abruf: HTTP {status}. Spotify sagt: {meldung}")
            self.merke(f"Unerwarteter Status {status}", "Meldung oben lesen.")

    # --- Schritt 3: YouTube (dort landet ein Spotify-Link am Ende) ----------
    def youtube_pruefen(self):
        self.titel("3. YouTube (dort landen Spotify-Songs am Ende)")
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            self.warn("uebersprungen - yt-dlp fehlt.")
            return
        code = ("import json,yt_dlp\n"
                "o={'quiet':True,'no_warnings':True,'skip_download':True,"
                "'extract_flat':'in_playlist','noplaylist':True}\n"
                "with yt_dlp.YoutubeDL(o) as y:\n"
                "    i=y.extract_info('ytsearch1:rick astley never gonna give you up',"
                "download=False)\n"
                "e=[x for x in (i or {}).get('entries') or [] if x]\n"
                "print(json.dumps({'titel': e[0].get('title') if e else ''}))\n")
        try:
            fertig = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                    text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fehler("YouTube-Suche haengt (ueber 60s).")
            self.merke("YouTube antwortet nicht",
                       "venv/bin/pip install -U yt-dlp, dann nochmal.")
            return
        titel = ""
        if fertig.returncode == 0:
            try:
                titel = json.loads(fertig.stdout.strip().splitlines()[-1]).get("titel", "")
            except (ValueError, IndexError):
                titel = ""
        if titel:
            self.ok(f"Suche geht: {titel[:70]}")
            return
        grund = (fertig.stderr or fertig.stdout or "").strip().splitlines()
        self.fehler(f"YouTube-Suche schlaegt fehl: {grund[-1][:200] if grund else '?'}")
        self.merke("YouTube geht nicht",
                   "Fast immer veraltetes yt-dlp:  venv/bin/pip install -U yt-dlp   "
                   "danach: systemctl restart flobot")

    # --- Abschluss ----------------------------------------------------------
    def bericht(self):
        self.titel("Ergebnis")
        if not self.probleme:
            print("  Keine Probleme gefunden - Musik und Spotify sind in Ordnung.")
            print("\n  Geht es trotzdem nicht, zeigt das den Grund:")
            print("    bash k m")
            return 0
        for i, (ueberschrift, was_tun) in enumerate(self.probleme, 1):
            print(f"  {i}. \033[1m{ueberschrift}\033[0m")
            print(f"     -> {was_tun}")
        return 1

    def lauf(self):
        print("\033[1mFlo - Musik-Diagnose\033[0m")
        self.werkzeuge_pruefen()
        self.spotify_pruefen()
        self.youtube_pruefen()
        return self.bericht()


instance = MusikCheck()
lauf = instance.lauf

if __name__ == "__main__":
    sys.exit(lauf())
