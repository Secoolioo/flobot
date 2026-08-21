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
import subprocess
import sys

from arzt import Arzt

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dann eben ohne .env
    load_dotenv = None

# Ein bekannter oeffentlicher Track, an dem sich der Abruf pruefen laesst.
PROBE_TRACK = "4cOdK2wGLETKBW3PvgPWqT"
TIMEOUT = 15


class MusikCheck(Arzt):
    """Prueft die Musik-Strecke Schritt fuer Schritt."""

    NAME = "Flo - Musik-Diagnose"

    def __init__(self):
        super().__init__()
        self.sp_id = ""
        self.sp_secret = ""

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

    def _anfrage(self, url, daten=None, kopf=None):
        """Kompatibilitaets-Huelle um Arzt.anfrage: gibt nur (status, body)."""
        status, body, _kopf = self.anfrage(url, form=daten, kopf=kopf)
        return status, body

    _meldung = staticmethod(Arzt.meldung)

    # --- Schritt 2: Spotify -------------------------------------------------
    def spotify_pruefen(self):
        self.titel("2. Spotify")
        self.sp_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        self.sp_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
        self.info(f"Client-ID    : {self.maskiere(self.sp_id)}")
        self.info(f"Client-Secret: {self.maskiere(self.sp_secret)}")

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
        # KEIN extract_flat. Damit las die Pruefung nur die Trefferliste und
        # fasste den Player nie an - sie meldete "Suche geht", waehrend im Bot
        # jeder Song an YouTubes Bot-Pruefung scheiterte. Eine Diagnose, die den
        # Ausfall nicht sieht, ist schlimmer als keine: sie schickt einen auf die
        # falsche Faehrte (hier: ein sinnloses yt-dlp-Update).
        # Geprueft wird deshalb wie im Betrieb - bis zur abspielbaren Adresse.
        code = ("import json,yt_dlp\n"
                "o={'quiet':True,'no_warnings':True,'skip_download':True,"
                "'noplaylist':True,'format':'bestaudio/best'}\n"
                "f=__import__('os').getenv('YTDLP_COOKIES','').strip()\n"
                "if f: o['cookiefile']=f\n"
                "c=__import__('os').getenv('YTDLP_PLAYER_CLIENT','').strip()\n"
                "if c: o['extractor_args']={'youtube':{'player_client':[c]}}\n"
                "with yt_dlp.YoutubeDL(o) as y:\n"
                "    i=y.extract_info('ytsearch1:rick astley never gonna give you up',"
                "download=False)\n"
                "e=[x for x in (i or {}).get('entries') or [] if x]\n"
                "d=e[0] if e else {}\n"
                "print(json.dumps({'titel': d.get('title') or '',"
                " 'stream': bool(d.get('url'))}))\n")
        try:
            fertig = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                    text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fehler("YouTube-Suche haengt (ueber 60s).")
            self.merke("YouTube antwortet nicht",
                       "venv/bin/pip install -U yt-dlp, dann nochmal.")
            return
        titel, stream = "", False
        if fertig.returncode == 0:
            try:
                d = json.loads(fertig.stdout.strip().splitlines()[-1])
                titel, stream = d.get("titel", ""), bool(d.get("stream"))
            except (ValueError, IndexError):
                titel, stream = "", False
        if titel and not stream:
            # Treffer gefunden, aber keine abspielbare Adresse - genau der Fall,
            # den die alte Pruefung als "geht" gemeldet hat.
            self.fehler(f"Treffer da ({titel[:50]}), aber KEINE abspielbare "
                        "Adresse - genau das merkt der Bot beim Abspielen.")
            self.merke("YouTube liefert keinen Stream",
                       "Meist YouTubes Bot-Pruefung. Flo weicht dann selbst auf "
                       "SoundCloud aus; dauerhaft hilft nur "
                       "YTDLP_COOKIES=/opt/flobot/cookies.txt (Wegwerf-Account!).")
            self._soundcloud_pruefen()
            return
        if titel:
            self.ok(f"Suche geht: {titel[:70]}")
            return
        grund = (fertig.stderr or fertig.stdout or "").strip().splitlines()
        self.fehler(f"YouTube-Suche schlaegt fehl: {grund[-1][:200] if grund else '?'}")
        self.merke("YouTube geht nicht",
                   "Fast immer veraltetes yt-dlp:  venv/bin/pip install -U yt-dlp   "
                   "danach: systemctl restart flobot")

    def _soundcloud_pruefen(self):
        """Geht wenigstens die Ausweichquelle? Ohne sie ist Musik ganz tot."""
        code = ("import json,yt_dlp\n"
                "o={'quiet':True,'no_warnings':True,'skip_download':True,"
                "'noplaylist':True,'format':'bestaudio/best',"
                "'default_search':'scsearch'}\n"
                "with yt_dlp.YoutubeDL(o) as y:\n"
                "    i=y.extract_info('scsearch1:rick astley never gonna give you up',"
                "download=False)\n"
                "e=[x for x in (i or {}).get('entries') or [] if x]\n"
                "d=e[0] if e else {}\n"
                "print(json.dumps({'titel': d.get('title') or '',"
                " 'stream': bool(d.get('url'))}))\n")
        try:
            fertig = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                    text=True, timeout=60)
            d = json.loads(fertig.stdout.strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            d = {}
        if d.get("stream"):
            self.ok(f"SoundCloud geht ({str(d.get('titel'))[:45]}) - Flo weicht "
                    "dorthin aus, Musik laeuft also weiter.")
        else:
            self.fehler("Auch SoundCloud liefert nichts - dann geht gar keine Musik.")
            self.merke("Keine Musikquelle erreichbar",
                       "Netz/Firewall pruefen und dann YTDLP_COOKIES setzen.")

    # --- Abschluss ----------------------------------------------------------
    def bericht(self):
        return super().bericht(schluss=(
            "\n  Geht es trotzdem nicht, zeigt das den Grund:\n    bash k m"))

    def lauf(self):
        self._schreib(f"\033[1m{self.NAME}\033[0m", self.NAME)
        self.werkzeuge_pruefen()
        self.spotify_pruefen()
        self.youtube_pruefen()
        return self.bericht()


instance = MusikCheck()
lauf = instance.lauf

if __name__ == "__main__":
    sys.exit(lauf())
