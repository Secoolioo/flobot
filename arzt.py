"""Gemeinsame Grundlage der Flo-Aerzte (KI, Musik, Gesamt).

Die Aerzte sind bewusst EIGENSTAENDIG: sie muessen auch dann noch laufen, wenn
der Bot selbst nicht mehr importierbar ist. Deshalb haengt hier nichts am Bot -
nur Standardbibliothek. Was sie teilen, steht hier statt dreimal daneben.

Ein Arzt beantwortet immer dieselben zwei Fragen:
  1. Was ist kaputt?           -> fehler()/warn()
  2. Was mache ich dagegen?    -> merke(ueberschrift, was_tun)
"""

import json
import os
import socket
import ssl
import urllib.error
import urllib.request

TIMEOUT = 20


class Arzt:
    """Basis: Ausgabe, Befundsammlung, HTTP - fuer alle Aerzte gleich."""

    #: Ueberschrift des Berichts, von den Unterklassen gesetzt.
    NAME = "Flo - Diagnose"

    def __init__(self):
        self.probleme = []     # [(Ueberschrift, Was tun)]
        self.zeilen = []       # alles Ausgegebene, fuer den Bericht in eine Datei

    # --- Ausgabe ------------------------------------------------------------
    def _schreib(self, text, roh=None):
        print(text)
        self.zeilen.append(roh if roh is not None else text)

    def titel(self, text):
        self._schreib(f"\n\033[1m{text}\033[0m", f"\n{text}")

    def ok(self, text):
        self._schreib(f"  \033[32mOK\033[0m    {text}", f"  OK     {text}")

    def warn(self, text):
        self._schreib(f"  \033[33m!\033[0m     {text}", f"  !      {text}")

    def fehler(self, text):
        self._schreib(f"  \033[31mFEHLER\033[0m {text}", f"  FEHLER {text}")

    def info(self, text):
        self._schreib(f"        {text}", f"        {text}")

    def merke(self, ueberschrift, was_tun):
        """Sammelt Befunde fuer den Abschluss - jeden nur EINMAL. Ohne das steht
        derselbe Rat doppelt da, wenn zwei Pruefungen an derselben Ursache
        scheitern."""
        if (ueberschrift, was_tun) not in self.probleme:
            self.probleme.append((ueberschrift, was_tun))

    # --- Geheimnisse --------------------------------------------------------
    @staticmethod
    def maskiere(wert):
        """Zeigt genug zum Wiedererkennen, aber nie das Geheimnis selbst.
        Gilt IMMER - auch im Fehlerfall, auch im Bericht, auch im Log."""
        if not wert:
            return "(leer)"
        if len(wert) <= 12:
            return f"{wert[:2]}...{wert[-2:]} ({len(wert)} Zeichen)"
        return f"{wert[:4]}...{wert[-4:]} ({len(wert)} Zeichen)"

    # --- Netz ---------------------------------------------------------------
    def erreichbar(self, url, *, name=""):
        """DNS + TCP + TLS getrennt pruefen. Nur so laesst sich 'kommt nicht
        raus' von 'die Gegenseite lehnt ab' unterscheiden - und das ist bei
        jedem Ausfall die erste Frage."""
        name = name or url
        try:
            rest = url.split("://", 1)[1]
        except IndexError:
            self.fehler(f"{name}: das ist keine URL ({url})")
            return False
        hostteil = rest.split("/", 1)[0]
        host, _, portteil = hostteil.rpartition(":")
        try:
            port = int(portteil)
            if not host:
                raise ValueError
        except ValueError:
            host, port = hostteil, (443 if url.startswith("https") else 80)

        try:
            adressen = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            self.ok(f"DNS: {host} -> {adressen[0][4][0]}")
        except socket.gaierror as exc:
            self.fehler(f"DNS geht nicht: {host} ({exc})")
            self.merke("DNS kaputt",
                       "Auf dem Server pruefen:  ping -c1 1.1.1.1")
            return False
        try:
            with socket.create_connection((host, port), timeout=8) as roh:
                if port == 443:
                    with ssl.create_default_context().wrap_socket(
                            roh, server_hostname=host) as tls:
                        self.ok(f"TLS steht ({tls.version()})")
                else:
                    self.ok("TCP steht (unverschluesselt)")
        except ssl.SSLCertVerificationError as exc:
            self.fehler(f"TLS-Zertifikat abgelehnt: {exc}")
            self.merke("TLS schlaegt fehl",
                       "Meist Systemzeit oder alte CA-Zertifikate. Pruefen: date  "
                       "und  apt install --reinstall ca-certificates")
            return False
        except (socket.timeout, OSError) as exc:
            self.fehler(f"Keine Verbindung zu {host}:{port} ({exc})")
            self.merke("Kommt nicht raus", "Ausgehende Firewall oder kein Internet.")
            return False
        return True

    def anfrage(self, url, *, daten=None, kopf=None, form=None, zeit=None):
        """(status, body, kopfzeilen). Wirft NIE - Fehler sind hier Daten, keine
        Ausnahmen: ein Arzt, der selbst abstuerzt, ist nutzlos."""
        kopf = dict(kopf or {})
        rumpf = None
        if form is not None:
            rumpf = "&".join(f"{k}={v}" for k, v in form.items()).encode()
            kopf.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif daten is not None:
            rumpf = json.dumps(daten).encode()
            kopf.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=rumpf, headers=kopf,
                                     method="POST" if rumpf is not None else "GET")
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
    def meldung(body):
        """Zieht die Klartext-Meldung aus einer JSON-Fehlerantwort."""
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            return (body or "").strip()[:300]
        for schluessel in ("error_description", "error", "message", "detail"):
            wert = d.get(schluessel) if isinstance(d, dict) else None
            if isinstance(wert, dict):
                wert = wert.get("message") or wert.get("status")
            if wert:
                return str(wert)[:300]
        return str(d)[:300]

    # --- Abschluss ----------------------------------------------------------
    def bericht(self, *, schluss=""):
        self.titel("Ergebnis")
        if not self.probleme:
            self._schreib("  Keine Probleme gefunden.", "  Keine Probleme gefunden.")
            if schluss:
                self._schreib(schluss, schluss)
            return 0
        for i, (ueberschrift, was_tun) in enumerate(self.probleme, 1):
            self._schreib(f"  {i}. \033[1m{ueberschrift}\033[0m", f"  {i}. {ueberschrift}")
            self._schreib(f"     -> {was_tun}", f"     -> {was_tun}")
        return 1

    def bericht_schreiben(self, pfad):
        """Legt den Bericht OHNE Farbcodes ab - zum Kopieren und Verschicken."""
        try:
            with open(pfad, "w", encoding="utf-8") as datei:
                datei.write("\n".join(self.zeilen) + "\n")
            return pfad
        except OSError as exc:
            self.warn(f"Bericht nicht schreibbar ({exc})")
            return ""
