"""Ein Befehl, der ALLES prueft - und einen Bericht zum Kopieren schreibt.

    bash k alles

Fuehrt die KI- und die Musik-Diagnose aus und nimmt die Systemseite dazu:
Discord-Token, Datenordner, Plattenplatz, Stand gegenueber dem Repo, die
systemd-Datei, die Pakete und die Uhr. Am Ende liegt alles ohne Farbcodes in
einer Datei - eine Nachricht, statt fuenf Screenshots.

Kein Teil des Bots. Aendert nichts. Geheimnisse werden NIE vollstaendig
ausgegeben, auch nicht im Bericht.
"""

import os
import shutil
import subprocess
import sys
import time

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
        ("davey", "davey", "Voice ueberhaupt - Discord verlangt seit 03/2026 DAVE"),
        ("deno", "deno", "YouTube (JS-Aufgaben fuer yt-dlp)"),
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

    # --- Uhr ---------------------------------------------------------------
    #: Ab dieser Abweichung zwischen Hardware-Uhr und Systemzeit ist es kein
    #: Drift mehr, sondern eine Zeitzone (Sekunden).
    RTC_GRENZE = 1800

    @staticmethod
    def _timedatectl():
        """{'LocalRTC': 'no', 'NTPSynchronized': 'yes'} - oder {}, wenn es kein
        timedatectl gibt (Container, kein systemd) oder es nicht antwortet."""
        if not shutil.which("timedatectl"):
            return {}
        try:
            fertig = subprocess.run(
                ["timedatectl", "show", "-p", "LocalRTC", "-p", "NTPSynchronized"],
                capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return {}
        if fertig.returncode != 0:
            return {}
        return dict(z.split("=", 1) for z in (fertig.stdout or "").splitlines() if "=" in z)

    @staticmethod
    def _rtc_sekunden():
        """Die Hardware-Uhr, wie der Kernel sie liest (als UTC) - oder None."""
        try:
            with open("/sys/class/rtc/rtc0/since_epoch", encoding="ascii") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _systemctl(*args):
        """Was 'systemctl ...' ausgibt - oder '' (kein systemd, Container, keine
        Antwort). Der Rueckgabecode zaehlt hier nicht: 'is-enabled' meldet
        'disabled' mit Code 1."""
        if not shutil.which("systemctl"):
            return ""
        try:
            fertig = subprocess.run(["systemctl", *args], capture_output=True,
                                    text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return (fertig.stdout or "").strip()

    def uhr(self):
        """Geht die Uhr des Servers richtig - und zwar schon beim Booten?

        Gemessen auf dem Server: die Hardware-Uhr (RTC) der VM laeuft auf
        Ortszeit, das System haelt sie fuer UTC ('RTC in local TZ: no'). Beim
        Booten steht die Uhr damit zwei Stunden vor, ~15 s spaeter stellt NTP
        sie zurueck. Flo startet vorher - alles, was er in diesen Sekunden
        stempelt, liegt danach zwei Stunden in der Zukunft, und systemd zeigt
        'active since' zwei Stunden voraus. Rein lesend; fehlt etwas, wird
        die Pruefung leise uebersprungen."""
        try:
            werte = self._timedatectl() or {}
            rtc = self._rtc_sekunden()
        except Exception:  # noqa: BLE001 - Diagnose darf nie abbrechen
            return
        if not isinstance(werte, dict):
            werte = {}
        if isinstance(rtc, bool) or not isinstance(rtc, (int, float)):
            rtc = None
        lokal = str(werte.get("LocalRTC", "")).strip().lower()
        ntp = str(werte.get("NTPSynchronized", "")).strip().lower()
        rat = ("timedatectl set-local-rtc 1   (die Hardware-Uhr laeuft auf "
               "Ortszeit) und  systemctl enable systemd-time-wait-sync   (Flo "
               "startet erst mit gestellter Uhr)")
        if rtc is not None and lokal == "no":
            # Mit LocalRTC=yes ist die Abweichung gewollt (genau die Zeitzone).
            abweichung = rtc - time.time()
            if abs(abweichung) >= self.RTC_GRENZE:
                self.warn(f"Hardware-Uhr weicht {abweichung / 3600:+.1f} h von der "
                          "Systemzeit ab - nach jedem Booten springt die Uhr, und "
                          "Flo stempelt in den ersten Sekunden falsche Zeiten.")
                self.merke("Uhr springt nach dem Booten", rat)
            else:
                self.ok("Hardware-Uhr passt zur Systemzeit")
        elif rtc is not None and not lokal:
            # Ohne timedatectl (etwa im Container, in dem 'k alles' unter Docker
            # laeuft) ist offen, ob die Hardware-Uhr auf Ortszeit laufen SOLL -
            # nach 'set-local-rtc 1' waere die Abweichung genau richtig.
            self.info("Hardware-Uhr nicht pruefbar (kein timedatectl, z. B. im "
                      "Container)")
        if ntp == "no":
            self.warn("Systemuhr ist NICHT per NTP gestellt - Zeiten koennen "
                      "falsch sein.")
            self.merke("Uhr nicht synchronisiert",
                       "timedatectl set-ntp true   und  systemctl enable "
                       "systemd-time-wait-sync")
        elif ntp == "yes":
            self.ok("Systemuhr per NTP gestellt")
        try:
            self._dienst_wartet_auf_uhr()
        except Exception:  # noqa: BLE001 - Diagnose darf nie abbrechen
            pass

    def _dienst_wartet_auf_uhr(self):
        """Wartet der EINGERICHTETE Dienst wirklich auf die gestellte Uhr?

        Ein Update bringt die neue flobot.service nicht an ihren Platz, und
        ohne daemon-reload kennt systemd sie nicht - dann startet Flo weiter
        vor NTP. Gefragt wird deshalb systemd selbst, nicht die Datei. Und
        time-sync.target ist ohne systemd-time-wait-sync sofort erreicht."""
        werte = dict(z.split("=", 1) for z in self._systemctl(
            "show", "flobot", "-p", "LoadState", "-p", "After").splitlines()
            if "=" in z)
        if werte.get("LoadState") != "loaded":
            return   # kein Dienst hier (Entwicklungsrechner, Container)
        if "time-sync.target" in werte.get("After", "").split():
            self.ok("flobot startet erst nach time-sync.target")
        else:
            self.warn("Der eingerichtete flobot-Dienst wartet nicht auf die "
                      "gestellte Uhr (time-sync.target fehlt).")
            self.merke("Dienst startet vor der Uhr",
                       f"cp {os.path.join(HIER, 'flobot.service')} "
                       "/etc/systemd/system/ && systemctl daemon-reload")
        # Nur mit systemd-timesyncd: chrony & Co. bringen eigene Warte-Dienste.
        if (self._systemctl("is-enabled", "systemd-timesyncd") == "enabled"
                and self._systemctl("is-enabled", "systemd-time-wait-sync") == "disabled"):
            self.warn("systemd-time-wait-sync ist aus - time-sync.target wartet "
                      "dann nicht auf NTP.")
            self.merke("Start wartet nicht auf NTP",
                       "systemctl enable systemd-time-wait-sync")

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
        self.uhr()

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
