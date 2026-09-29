# Flo als Container. systemd (flobot.service) bleibt daneben voll gueltig -
# beide nutzen denselben Datenordner, und eine Sperre darin verhindert, dass je
# zwei Flos gleichzeitig laufen.
#
#   docker compose -f docker/compose.yaml up -d --build     starten / aktualisieren
#   k d   /   k s                                            umziehen Docker <-> systemd
#
# Debian slim, NICHT Alpine: discord.py findet libopus per ctypes, und das ist
# auf musl unzuverlaessig; davey liefert fertige Wheels fuer glibc.
FROM python:3.12-slim-bookworm

# ffmpeg      Musik und Soundboard (bringt libopus0 mit, trotzdem ausdruecklich)
# libopus0    discord.py kodiert den Ton selbst ueber libopus
# espeak-ng   'Flo sprich ...' (TTS)
# fonts-*     Level-Karten, Bestenliste, Casino-Bilder (render.py sucht DejaVu)
# tzdata      ZoneInfo('Europe/Berlin') - ohne startet Flo gar nicht
# wngerman    Wortliste fuer Wordle/Wort des Tages (sonst eingebaute Liste)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ffmpeg libopus0 espeak-ng fonts-dejavu-core fonts-liberation \
        tzdata ca-certificates wngerman \
 && rm -rf /var/lib/apt/lists/*

# DIESELBEN Pfade wie unter systemd (/opt/flobot): Pfade aus der .env
# (z. B. YTDLP_COOKIES=/opt/flobot/data/cookies.txt) stimmen dann in beiden.
WORKDIR /opt/flobot

# Erst nur die Pakete - so baut ein Code-Update das Image in Sekunden neu.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# FLO_LAUFZEIT=docker: der Panel-Update-Knopf nennt dann den richtigen Befehl,
# fatale Login-Fehler warten vor dem Beenden (Dockers Neustart ist sonst zu
# schnell fuer Discord). TZ wie auf dem Server - Tag und Tageszeit-Icon wechseln
# sonst in UTC.
ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Berlin \
    FLO_LAUFZEIT=docker

# Gesund = in den letzten 2 Minuten wirklich mit Discord verbunden (bot.py
# schreibt den Herzschlag nur dann, siehe _herzschlag).
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import os,sys,time; p='/tmp/flo-herzschlag'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 120 else 1)"

# Absoluter Interpreter: 'Flo restart' startet per os.execv neu, und execv
# sucht NICHT im PATH - mit nacktem "python" wuerde jeder Neustart scheitern.
CMD ["/usr/local/bin/python", "bot.py"]
