"""Ein Befehl, der ALLES prueft - und einen Bericht zum Kopieren schreibt.

    bash k alles

Fuehrt die KI- und die Musik-Diagnose aus und nimmt die Systemseite dazu:
Discord-Token, Datenordner, Plattenplatz, Stand gegenueber dem Repo, die
systemd-Datei und die Pakete. Am Ende liegt alles ohne Farbcodes in einer
Datei - eine Nachricht, statt fuenf Screenshots.

Kein Teil des Bots. Aendert nichts. Geheimnisse werden NIE vollstaendig
ausgegeben, auch nicht im Bericht.
"""

import os
import shutil
import subprocess
import sys

from arzt import Arzt

HIER = os.path.dirname(os.path.abspath(__file__))
BERICHT = os.path.join(HIER, "diagnose.txt")

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


class GesamtCheck(Arzt):
    """Bindet die einzelnen Aerzte zusammen und prueft den Server selbst."""

    NAME = "Flo - Gesamtdiagnose"

    # Pakete, ohne die einzelne Features still ausfallen. Die Meldung sagt, was
    # genau wegbricht - "Paket fehlt" allein hilft niemandem.
    PAKETE = (
        ("discord", "discord.py", "ALLES - ohne das startet der Bot nicht"),
        ("dotenv", "python-dotenv", "die .env wird nicht gelesen"),
        ("aiohttp", "aiohttp", "Web-Panel und alle Netz-Abrufe"),
        ("openai", "openai", "die KI"),
        ("yt_dlp", "yt-dlp", "Musik"),
        ("nacl", "PyNaCl", "Voice (Musik, Soundboard)"),
        ("PIL", "Pillow", "alle Bilder (Level-Karten, Wordle, Bestenliste)"),
    )

    def __init__(self):
        super().__init__()
        self.fehlgeschlagen = []

    # --- System -------------------------------------------------------------
    def umgebung(self):
        self.titel("1. Umgebung")
        self.info(f"Python  : {sys.version.split()[0]}")
        self.info(f"Ordner  : {HIER}")
        pfad = os.path.join(HIER, ".env")
        if load_dotenv is not None and os.path.exists(pfad):
            load_dotenv(pfad)
            self.ok(".env gelesen")
        else:
            self.fehler("keine .env gefunden - ohne sie startet nichts.")
            self.merke("Keine .env", f"Datei {pfad} anlegen (siehe README).")

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if token:
            self.ok(f"DISCORD_TOKEN gesetzt ({self.maskiere(token)})")
        else:
            self.fehler("DISCORD_TOKEN fehlt - der Bot kann sich nicht anmelden.")
            self.merke("Kein Discord-Token", "DISCORD_TOKEN in die .env schreiben.")

        for modul, paket, wofuer in self.PAKETE:
            try:
                __import__(modul)
            except ImportError:
                self.fehler(f"{paket} fehlt -> {wofuer}")
                self.merke(f"Paket {paket} fehlt", f"venv/bin/pip install {paket}")
        if shutil.which("ffmpeg"):
            self.ok("ffmpeg gefunden")
        else:
            self.fehler("ffmpeg fehlt -> keine Musik, kein Soundboard")
            self.merke("ffmpeg fehlt", "apt install ffmpeg")

    def daten(self):
        self.titel("2. Daten und Platte")
        ordner = os.getenv("DATA_DIR", "").strip() or os.path.join(HIER, "data")
        if not os.path.isdir(ordner):
            self.warn(f"Datenordner gibt es (noch) nicht: {ordner}")
        else:
            gesamt = 0
            kaputt = []
            for name in sorted(os.listdir(ordner)):
                voll = os.path.join(ordner, name)
                if not os.path.isfile(voll):
                    continue
                gesamt += os.path.getsize(voll)
                if name.endswith(".kaputt") or ".kaputt-" in name:
                    kaputt.append(name)
            self.ok(f"Datenordner: {ordner} ({gesamt / 1024:.0f} kB)")
            if not os.access(ordner, os.W_OK):
                self.fehler("Datenordner ist NICHT beschreibbar - nichts wird gespeichert.")
                self.merke("Datenordner schreibgeschuetzt", f"chown/chmod fuer {ordner}")
            if kaputt:
                self.warn(f"{len(kaputt)} unter Quarantaene gestellte Datei(en): "
                          f"{', '.join(kaputt[:4])}")
                self.merke("Kaputte Datendateien",
                           "Das sind Sicherungen defekter JSON-Dateien. Ansehen und "
                           "loeschen, wenn der Bot laeuft.")
        try:
            platte = shutil.disk_usage(HIER)
            frei_mb = platte.free / 1024 / 1024
            if frei_mb < 200:
                self.fehler(f"Nur noch {frei_mb:.0f} MB frei - Speichern schlaegt bald fehl.")
                self.merke("Platte fast voll",
                           "Platz schaffen:  journalctl --vacuum-size=100M")
            else:
                self.ok(f"Platte: {frei_mb / 1024:.1f} GB frei")
        except OSError as exc:
            self.warn(f"Plattenplatz nicht ermittelbar ({exc})")

    def stand(self):
        self.titel("3. Stand gegenueber dem Repo")

        def git(*args):
            try:
                fertig = subprocess.run(["git", *args], cwd=HIER, capture_output=True,
                                        text=True, timeout=30)
                return fertig.stdout.strip() if fertig.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                return ""

        kopf = git("rev-parse", "--short", "HEAD")
        if not kopf:
            self.warn("kein git-Repo (oder git fehlt) - Stand nicht pruefbar.")
            return
        self.info(f"HEAD    : {kopf}  {git('log', '-1', '--format=%s')[:60]}")
        git("fetch", "--quiet")
        zurueck = git("rev-list", "--count", "HEAD..@{u}")
        if zurueck and zurueck != "0":
            self.warn(f"{zurueck} Commit(s) hinterher.")
            self.merke("Neuer Stand verfuegbar", "bash k n   (oder Update im Panel)")
        else:
            self.ok("aktuell")
        schmutz = git("status", "--porcelain")
        if schmutz:
            self.warn(f"{len(schmutz.splitlines())} lokal geaenderte Datei(en) - ein "
                      "Update per git pull kann daran scheitern.")
            self.merke("Lokale Aenderungen im Repo",
                       "Ansehen mit:  git status   und  git diff")

        # systemd-Datei: git pull allein bringt sie NICHT an ihren Platz.
        hier_datei = os.path.join(HIER, "flobot.service")
        dort = "/etc/systemd/system/flobot.service"
        if os.path.exists(hier_datei) and os.path.exists(dort):
            try:
                a = open(hier_datei, encoding="utf-8").read()
                b = open(dort, encoding="utf-8").read()
                if a.strip() != b.strip():
                    self.warn("flobot.service im Repo weicht von der installierten ab.")
                    self.merke("systemd-Datei veraltet",
                               f"cp {hier_datei} {dort} && systemctl daemon-reload")
                else:
                    self.ok("flobot.service ist installiert und aktuell")
            except OSError:
                pass

    def dienst(self):
        self.titel("4. Dienst")
        if not shutil.which("systemctl"):
            self.info("kein systemd hier - uebersprungen")
            return
        try:
            fertig = subprocess.run(
                ["systemctl", "show", "flobot", "-p", "LoadState",
                 "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts",
                 "-p", "ExecMainStartTimestamp"],
                capture_output=True, text=True, timeout=20)
            if fertig.returncode != 0:
                self.info(f"systemctl antwortet nicht "
                          f"({fertig.stderr.strip().splitlines()[0][:80] if fertig.stderr.strip() else '?'})")
                return
            werte = dict(z.split("=", 1) for z in fertig.stdout.strip().splitlines()
                         if "=" in z)
        except (OSError, subprocess.SubprocessError, ValueError):
            self.warn("systemctl nicht abfragbar")
            return
        if werte.get("LoadState", "") in ("not-found", "masked", ""):
            # Kein Dienst eingerichtet - das ist auf einem Entwicklungsrechner
            # normal und kein Fehler. Nur auf dem Server waere es einer.
            self.info("kein flobot-Dienst eingerichtet (auf einem Server waere das "
                      "ein Fehler)")
            return
        zustand = werte.get("ActiveState", "?")
        if zustand == "active":
            self.ok(f"flobot laeuft (seit {werte.get('ExecMainStartTimestamp', '?')})")
        else:
            self.fehler(f"flobot ist {zustand} ({werte.get('SubState', '?')})")
            self.merke("Dienst laeuft nicht",
                       "systemctl status flobot --no-pager   und  bash k l")
        neustarts = werte.get("NRestarts", "0")
        try:
            if int(neustarts) > 5:
                self.warn(f"{neustarts} Neustarts - der Bot faengt sich immer wieder.")
                self.merke("Haeufige Neustarts",
                           "Ursache im Log:  journalctl -u flobot -p err -n 50 --no-pager")
        except ValueError:
            pass

    # --- Die einzelnen Aerzte -----------------------------------------------
    def unterarzt(self, name, bauen):
        """Fuehrt einen Einzel-Arzt aus und uebernimmt seine Befunde. Ein Arzt,
        der selbst abstuerzt, darf die Gesamtdiagnose nicht mitreissen."""
        self.titel(name)
        try:
            arzt = bauen()
            arzt.lauf()
        except Exception as exc:  # noqa: BLE001 - Diagnose darf nie abbrechen
            self.fehler(f"{name} abgebrochen: {type(exc).__name__}: {exc}")
            self.merke(f"{name} laeuft nicht", "Das ist selbst ein Fehler - melden.")
            return
        self.zeilen.extend(arzt.zeilen)
        for befund in arzt.probleme:
            self.merke(*befund)

    def lauf(self):
        self._schreib(f"\033[1m{self.NAME}\033[0m", self.NAME)
        self.umgebung()
        self.daten()
        self.stand()
        self.dienst()

        import tools_ki_check
        import tools_musik_check
        self.unterarzt("5. KI", tools_ki_check.KiCheck)
        self.unterarzt("6. Musik", tools_musik_check.MusikCheck)

        code = self.bericht(schluss="\n  Alles in Ordnung.")
        pfad = self.bericht_schreiben(BERICHT)
        if pfad:
            self._schreib(f"\n\033[2mBericht liegt in {pfad}\033[0m",
                          f"\nBericht liegt in {pfad}")
        return code


instance = GesamtCheck()
lauf = instance.lauf

if __name__ == "__main__":
    sys.exit(lauf())
