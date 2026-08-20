"""Sagt in Klartext, WARUM die KI nicht antwortet.

Der Bot faengt LLM-Fehler ab und schreibt nur "Mein KI-Dienst antwortet gerade
nicht" in den Chat - der echte Grund landet als Traceback im Journal. Auf dem
Handy ist das unlesbar. Dieses Skript fragt den Anbieter direkt und uebersetzt
die Antwort in einen Satz plus den naechsten Schritt.

    /opt/flobot/venv/bin/python /opt/flobot/tools_ki_check.py

Kein Teil des Bots. Aendert nichts, schreibt nichts, braucht keine neuen Pakete.
Der Schluessel wird NIE vollstaendig ausgegeben - auch nicht im Fehlerfall.
"""

import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dann eben ohne .env
    load_dotenv = None

# Muss zu ai.py passen. Bewusst hier kopiert, damit das Skript auch dann laeuft,
# wenn ai.py selbst kaputt ist (Importfehler).
STANDARD_BASE = "https://api.groq.com/openai/v1"
STANDARD_MODELL = "openai/gpt-oss-120b"
STANDARD_VISION = "qwen/qwen3.6-27b"

TIMEOUT = 20


class KiCheck:
    """Prueft die KI-Strecke Schritt fuer Schritt: Konfig, Netz, Modell, Aufruf."""

    def __init__(self):
        self.base = ""
        self.modell = ""
        self.vision = ""
        self.key = ""
        self.probleme = []      # (Ueberschrift, Was tun)
        self.modelle = None     # Liste vom Anbieter, None = nicht abrufbar
        self.cf_code = ""       # Cloudflare-Fehlercode, falls einer kam

    # --- Ausgabe ------------------------------------------------------------
    def titel(self, text):
        print(f"\n\033[1m{text}\033[0m")

    def ok(self, text):
        print(f"  \033[32mOK\033[0m    {text}")

    def warn(self, text):
        print(f"  \033[33m!\033[0m     {text}")

    def fehler(self, text):
        print(f"  \033[31mFEHLER\033[0m {text}")

    def merke(self, ueberschrift, was_tun):
        """Sammelt Befunde fuer den Abschluss - jeden nur einmal. Ohne das steht
        derselbe Rat doppelt da, wenn Modell-Liste UND Chat-Aufruf scheitern."""
        if (ueberschrift, was_tun) not in self.probleme:
            self.probleme.append((ueberschrift, was_tun))

    @staticmethod
    def maskiere(key):
        """Zeigt genug zum Wiedererkennen, aber nie den Schluessel selbst."""
        if not key:
            return "(leer)"
        if len(key) <= 12:
            return f"{key[:2]}...{key[-2:]} ({len(key)} Zeichen)"
        return f"{key[:4]}...{key[-4:]} ({len(key)} Zeichen)"

    # --- Schritt 1: Konfiguration ------------------------------------------
    def konfig_lesen(self):
        self.titel("1. Konfiguration (.env)")
        pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if load_dotenv is None:
            self.warn("Paket python-dotenv fehlt - lese nur die Umgebung.")
        elif os.path.exists(pfad):
            load_dotenv(pfad)
            self.ok(f".env gelesen: {pfad}")
        else:
            self.warn(f"keine .env gefunden unter {pfad}")

        self.base = (os.getenv("LLM_BASE_URL", "").strip() or STANDARD_BASE).rstrip("/")
        self.modell = os.getenv("LLM_MODEL", "").strip() or STANDARD_MODELL
        self.vision = os.getenv("LLM_VISION_MODEL", "").strip() or STANDARD_VISION
        self.key = os.getenv("LLM_API_KEY", "").strip()

        print(f"        Anbieter : {self.base}")
        print(f"        Modell   : {self.modell}")
        print(f"        Vision   : {self.vision}")
        print(f"        Schluessel: {self.maskiere(self.key)}")

        proxy = next((os.environ[v] for v in ("HTTPS_PROXY", "https_proxy",
                                              "HTTP_PROXY", "http_proxy")
                      if os.environ.get(v)), "")
        if proxy:
            self.warn(f"Ein Proxy ist gesetzt: {proxy} - alle Anfragen laufen darueber.")

        lokal = any(h in self.base for h in ("localhost", "127.0.0.1", ":11434"))
        if not self.key and not lokal:
            self.fehler("LLM_API_KEY ist leer - das KI-Feature ist damit komplett AUS.")
            self.merke("Kein Schluessel gesetzt",
                       "LLM_API_KEY=... in die .env schreiben, dann: systemctl restart flobot")
            return False
        if self.key and self.key.strip('"\' ') != self.key:
            self.warn("Der Schluessel hat Anfuehrungszeichen oder Leerzeichen drumherum.")
            self.merke("Schluessel mit Anfuehrungszeichen",
                       "In der .env OHNE Quotes schreiben: LLM_API_KEY=gsk_...")
        return True

    # --- Schritt 2: kommt ueberhaupt was raus? -----------------------------
    def netz_pruefen(self):
        """Trennt sauber: 'kommt nicht raus' vs. 'Anbieter lehnt ab'."""
        self.titel("2. Netz zum Anbieter")
        try:
            teil = self.base.split("://", 1)[1]
        except IndexError:
            self.fehler(f"LLM_BASE_URL sieht nicht wie eine URL aus: {self.base}")
            self.merke("Basis-URL kaputt", "LLM_BASE_URL in der .env pruefen.")
            return False
        # Ein Port in der URL schlaegt das Schema. Ohne das haette jeder lokale
        # Anbieter (Ollama :11434, LM Studio :1234) faelschlich "kommt nicht raus"
        # gemeldet, weil nur nach http/https entschieden wurde.
        # Direkt umwandeln statt vorher zu pruefen: es gibt Ziffern, die eine
        # Pruefung durchlassen und int() trotzdem nicht mag.
        hostteil = teil.split("/", 1)[0]
        host, _, portteil = hostteil.rpartition(":")
        try:
            port = int(portteil)
            if not host:
                raise ValueError
        except ValueError:
            host, port = hostteil, (443 if self.base.startswith("https") else 80)

        try:
            adressen = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            self.ok(f"DNS: {host} -> {adressen[0][4][0]}")
        except socket.gaierror as exc:
            self.fehler(f"DNS geht nicht: {host} ({exc})")
            self.merke("DNS kaputt",
                       "Auf dem Server pruefen:  ping -c1 1.1.1.1   und   cat etc/resolv.conf")
            return False

        try:
            with socket.create_connection((host, port), timeout=8) as roh:
                if port == 443:
                    ctx = ssl.create_default_context()
                    with ctx.wrap_socket(roh, server_hostname=host) as tls:
                        self.ok(f"TLS steht ({tls.version()})")
                else:
                    self.ok("TCP steht (unverschluesselt)")
        except ssl.SSLCertVerificationError as exc:
            self.fehler(f"TLS-Zertifikat wird abgelehnt: {exc}")
            self.merke("TLS schlaegt fehl",
                       "Meist die Systemzeit oder alte CA-Zertifikate. Pruefen: date  "
                       "und  apt install --reinstall ca-certificates")
            return False
        except (socket.timeout, OSError) as exc:
            self.fehler(f"Keine Verbindung zu {host}:{port} ({exc})")
            self.merke("Kommt nicht raus",
                       "Ausgehende Firewall oder kein Internet. Pruefen:  curl -sS -m5 -o "
                       "nul -w '%{http_code}' https://api.groq.com")
            return False
        return True

    # --- HTTP-Helfer --------------------------------------------------------
    def _anfrage(self, pfad, daten=None, ua=None, zeit=None):
        """Gibt (status, body_text, kopfzeilen) zurueck. Wirft nicht - Fehler
        sind hier Daten. 'ua' setzt eine abweichende Client-Signatur; genau die
        prueft Cloudflare bei Fehler 1010."""
        url = f"{self.base}{pfad}"
        kopf = {"Authorization": f"Bearer {self.key or 'ollama'}",
                "Content-Type": "application/json"}
        if ua:
            kopf["User-Agent"] = ua
        rumpf = json.dumps(daten).encode() if daten is not None else None
        req = urllib.request.Request(url, data=rumpf, headers=kopf,
                                     method="POST" if daten is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=zeit or TIMEOUT) as antwort:
                return (antwort.status, antwort.read().decode("utf-8", "replace"),
                        dict(antwort.headers))
        except urllib.error.HTTPError as exc:
            return (exc.code, exc.read().decode("utf-8", "replace"),
                    dict(exc.headers or {}))
        except urllib.error.URLError as exc:
            return 0, str(exc.reason), {}
        except (socket.timeout, TimeoutError):
            return 0, f"Zeitueberschreitung nach {zeit or TIMEOUT}s", {}

    @staticmethod
    def _meldung(body):
        """Zieht die Klartext-Meldung aus einer JSON-Fehlerantwort."""
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            return (body or "").strip()[:300]
        f = d.get("error", d)
        if isinstance(f, dict):
            return str(f.get("message") or f.get("detail") or f)[:300]
        return str(f)[:300]

    # --- Schritt 3: gibt es das Modell ueberhaupt noch? --------------------
    def modelle_pruefen(self):
        self.titel("3. Modell-Liste des Anbieters")
        status, body, kopf = self._anfrage("/models")
        if status == 200:
            try:
                self.modelle = sorted(m["id"] for m in json.loads(body).get("data", []))
            except (ValueError, KeyError, TypeError):
                self.warn("Antwort war kein erwartetes JSON.")
                return
            self.ok(f"{len(self.modelle)} Modelle verfuegbar")
            for name, wert in (("Chat", self.modell), ("Vision", self.vision)):
                if wert in self.modelle:
                    self.ok(f"{name}-Modell '{wert}' gibt es.")
                else:
                    self.fehler(f"{name}-Modell '{wert}' steht NICHT in der Liste "
                                "- vermutlich ausgemustert.")
                    self.merke(f"{name}-Modell existiert nicht mehr",
                               f"In der .env ein aktuelles Modell setzen "
                               f"(LLM_{'MODEL' if name == 'Chat' else 'VISION_MODEL'}=...), "
                               "dann: systemctl restart flobot")
            self._vorschlagen()
            return True
        self._status_deuten(status, body, "Modell-Liste", kopf)
        # Status 0 = gar keine Antwort. Dann ist auch der Chat-Aufruf sinnlos.
        return status != 0

    def _vorschlagen(self):
        """Zeigt aus der echten Liste des Anbieters, was passen wuerde."""
        if not self.modelle:
            return
        fehlt_chat = self.modell not in self.modelle
        fehlt_vision = self.vision not in self.modelle
        if not (fehlt_chat or fehlt_vision):
            return
        print("\n        Verfuegbar beim Anbieter (Auswahl):")
        for m in self.modelle:
            klein = m.lower()
            if any(x in klein for x in ("whisper", "tts", "guard", "embed")):
                continue
            marke = "  (kann Bilder)" if any(
                x in klein for x in ("vision", "scout", "maverick", "vl", "multimodal")) else ""
            print(f"          {m}{marke}")

    # --- Schritt 4: der echte Aufruf ---------------------------------------
    def aufruf_pruefen(self):
        self.titel("4. Echter Chat-Aufruf (so wie der Bot ihn macht)")
        start = time.monotonic()
        status, body, kopf = self._anfrage("/chat/completions", {
            "model": self.modell,
            "messages": [{"role": "user", "content": "Sag nur: ok"}],
            "max_tokens": 5,
            "temperature": 0,
        })
        dauer = time.monotonic() - start
        if status == 200:
            try:
                antwort = json.loads(body)["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                antwort = "(Antwort ohne Text)"
            self.ok(f"Antwort nach {dauer:.1f}s: {antwort.strip()!r}")
            self.ok("Die KI-Strecke funktioniert. Wenn Flo trotzdem meckert, "
                    "liegt es nicht am Anbieter - siehe Log unten.")
            return True
        self._status_deuten(status, body, "Chat-Aufruf", kopf)
        return False

    @staticmethod
    def _cloudflare_code(body):
        """Cloudflare antwortet VOR dem Anbieter. Seine Codes bedeuten etwas
        voellig anderes als ein Fehler der API - 1010 heisst z. B. 'wegen der
        Client-Signatur gesperrt' und hat mit dem Schluessel nichts zu tun."""
        treffer = re.search(r"error code:\s*(\d{4})", body or "")
        return treffer.group(1) if treffer else ""

    def _status_deuten(self, status, body, was, kopf=None):
        """Uebersetzt HTTP-Status + Anbietertext in einen deutschen Satz."""
        meldung = self._meldung(body)
        kopf = kopf or {}
        strahl = kopf.get("cf-ray") or kopf.get("Cf-Ray") or kopf.get("CF-RAY") or ""
        cf = self._cloudflare_code(body)
        if cf:
            self.cf_code = cf
            erklaerung = {
                "1010": "Cloudflare sperrt die Client-Signatur (Browser/Programm).",
                "1006": "Cloudflare hat diese IP gesperrt.",
                "1007": "Cloudflare hat diese IP gesperrt.",
                "1008": "Cloudflare hat diese IP gesperrt.",
                "1015": "Cloudflare drosselt: zu viele Anfragen von dieser IP.",
                "1020": "Eine Firewall-Regel des Anbieters lehnt die Anfrage ab.",
            }.get(cf, "Cloudflare lehnt die Anfrage ab.")
            self.fehler(f"{was}: {erklaerung} (Cloudflare-Fehler {cf}, HTTP {status})")
            if strahl:
                print(f"        Cloudflare Ray-ID: {strahl}")
            self.merke(f"Cloudflare blockt vor Groq (Fehler {cf})",
                       "Der Schluessel ist NICHT schuld - die Anfrage kommt nie bei "
                       "Groq an. Siehe Signatur-Test unten.")
            return
        if status == 0:
            self.fehler(f"{was}: keine Antwort ({meldung})")
            self.merke("Anbieter nicht erreichbar",
                       "Netz/Firewall pruefen - siehe Schritt 2.")
        elif status == 401:
            self.fehler(f"{was}: Schluessel abgelehnt (401). Anbieter sagt: {meldung}")
            self.merke("Schluessel ungueltig oder abgelaufen",
                       "Neuen Key beim Anbieter holen, LLM_API_KEY in der .env "
                       "ersetzen, dann: systemctl restart flobot")
        elif status == 403:
            self.fehler(f"{was}: Zugriff verweigert (403). Anbieter sagt: {meldung}")
            self.merke("Konto oder Modell nicht freigegeben",
                       "Beim Anbieter pruefen, ob das Konto aktiv und das Modell "
                       "freigeschaltet ist.")
        elif status == 404:
            self.fehler(f"{was}: nicht gefunden (404). Anbieter sagt: {meldung}")
            self.merke("Modell gibt es nicht (mehr)",
                       "LLM_MODEL in der .env auf ein Modell aus der Liste oben "
                       "setzen, dann: systemctl restart flobot")
        elif status == 429:
            self.fehler(f"{was}: Ratenlimit (429). Anbieter sagt: {meldung}")
            self.merke("Kontingent aufgebraucht",
                       "Warten (Limit laeuft zurueck), Tarif erhoehen oder ein "
                       "kleineres Modell nehmen.")
        elif status >= 500:
            self.fehler(f"{was}: Anbieter-Stoerung ({status}). Sagt: {meldung}")
            self.merke("Stoerung beim Anbieter",
                       "Nichts zu tun ausser warten - Statusseite des Anbieters pruefen.")
        else:
            self.fehler(f"{was}: HTTP {status}. Anbieter sagt: {meldung}")
            self.merke(f"Unerwarteter Status {status}", "Meldung oben lesen.")

    # --- Schritt 5: Signatur oder IP? --------------------------------------
    # Cloudflare-Fehler 1010 heisst "wegen der Client-Signatur gesperrt". Ob
    # damit das PROGRAMM oder die IP gemeint ist, kann man nicht raten - aber
    # messen: dieselbe Anfrage mit verschiedenen Signaturen. Kommt eine durch,
    # ist es die Signatur (im Code zu beheben). Kommt keine durch, ist es die IP.
    SIGNATUREN = (
        ("Python (Standard)", "Python-urllib/3.11"),
        ("openai-Paket", "OpenAI/Python 1.40.0"),
        ("curl", "curl/8.5.0"),
        ("Browser", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    )

    def signatur_pruefen(self):
        """Nur bei einer Cloudflare-Sperre sinnvoll - sonst reine Zeitverschwendung."""
        if not self.cf_code:
            return
        self.titel("5. Woran liegt die Sperre? (Signatur-Test)")
        durch = []
        for name, ua in self.SIGNATUREN:
            status, body, _ = self._anfrage("/models", ua=ua, zeit=12)
            cf = self._cloudflare_code(body)
            if status == 200:
                self.ok(f"{name:18s} -> kommt DURCH (HTTP 200)")
                durch.append((name, ua))
            elif status in (401, 403) and not cf:
                # Von Groq selbst beantwortet - Cloudflare war also zufrieden.
                self.ok(f"{name:18s} -> kommt durch bis Groq (HTTP {status})")
                durch.append((name, ua))
            else:
                zusatz = f", Cloudflare {cf}" if cf else ""
                self.warn(f"{name:18s} -> blockiert (HTTP {status}{zusatz})")

        if durch:
            name, ua = durch[0]
            self.ok(f"Es liegt an der SIGNATUR - mit '{name}' geht es.")
            self.merke("Cloudflare sperrt die Client-Signatur",
                       f"Im Bot eine andere Signatur setzen. Sofort testbar per .env: "
                       f"LLM_USER_AGENT={ua}   dann: systemctl restart flobot")
        else:
            self.fehler("Keine Signatur kommt durch - dann ist die IP gesperrt.")
            self.merke("Die IP dieses Servers ist bei Cloudflare gesperrt",
                       "Kein Code-Fix moeglich. Moeglichkeiten: Router neu verbinden "
                       "(neue IP), anderer Weg raus (VPN/Proxy), oder bei Groq mit der "
                       "Ray-ID melden.")
        self._ip_zeigen()

    def _ip_zeigen(self):
        """Welche IP sieht die Aussenwelt? Die steht in der Cloudflare-Sperre."""
        for dienst in ("https://api.ipify.org", "https://ipinfo.io/ip",
                       "https://icanhazip.com"):
            try:
                req = urllib.request.Request(dienst, headers={"User-Agent": "curl/8.5.0"})
                with urllib.request.urlopen(req, timeout=8) as a:
                    ip = a.read().decode("utf-8", "replace").strip()
                if ip:
                    print(f"        Oeffentliche IP dieses Servers: {ip}")
                    print("        (die ist gesperrt, nicht der Schluessel)")
                    return
            except Exception:  # noqa: BLE001 - reine Zusatzinfo, darf scheitern
                continue

    # --- Abschluss ----------------------------------------------------------
    def bericht(self):
        self.titel("Ergebnis")
        if not self.probleme:
            print("  Keine Probleme gefunden - die KI-Strecke ist in Ordnung.")
            print("\n  Meckert Flo trotzdem, zeigt das hier den echten Grund:")
            print("    journalctl -u flobot --since -1h --no-pager | grep -A18 'LLM-Aufruf'")
            return 0
        for i, (ueberschrift, was_tun) in enumerate(self.probleme, 1):
            print(f"  {i}. \033[1m{ueberschrift}\033[0m")
            print(f"     -> {was_tun}")
        return 1

    def lauf(self):
        print("\033[1mFlo - KI-Diagnose\033[0m")
        if not self.konfig_lesen():
            return self.bericht()
        if not self.netz_pruefen():
            return self.bericht()
        erreichbar = self.modelle_pruefen()
        if erreichbar is not False:
            self.aufruf_pruefen()
        self.signatur_pruefen()
        return self.bericht()


instance = KiCheck()
lauf = instance.lauf

if __name__ == "__main__":
    sys.exit(lauf())
