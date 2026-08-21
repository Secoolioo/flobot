#!/usr/bin/env bash
# Flo-Arzt. Kurzbefehl fuers Handy an der noVNC-Konsole:
#
#     bash k          alles pruefen (Konfig, Netz, Modell, echter Aufruf + Log)
#     bash k n        vorher git pull
#     bash k l        nur die letzten KI-Fehler aus dem Journal
#     bash k m        Musik/Spotify pruefen (yt-dlp, ffmpeg, Token, Track)
#     bash k p        Panel-Zugang: das gewuerfelte Passwort aus dem Log
#     bash k a        ALLES pruefen und einen Bericht in diagnose.txt schreiben
#     bash k r        Dienst neu starten
#     bash k setup    einmalig: danach reicht ueberall  k
#
# Absichtlich ein Buchstabe und ohne Schraegstrich im Aufruf - auf einer
# US-belegten Konsole ist "/" schwer zu tippen und Copy-Paste gibt es nicht.
set -u
cd "$(dirname "$0")" || exit 1

PY="venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

trenner() { printf '\033[2m%s\033[0m\n' "----------------------------------------"; }

# Die Log-Zeilen, an denen man einen KI-Fehler ODER eine Selbstheilung erkennt.
# "KI:" MUSS drinbleiben: das sind die Zeilen, die sagen was Flo selbst repariert
# hat und was dauerhaft in die .env gehoert. Ein Test in test_games_logic.py
# haelt diese Liste gegen alle log-Meldungen in ai.py, damit hier nichts
# durchfaellt, wenn dort eine neue Zeile dazukommt.
KIMUSTER="KI-Fehler|KI:|KI-Selbsttest|KI-Feature|LLM-Aufruf|Vision-Aufruf|LLM generate"

# Das Panel schreibt sein gewuerfeltes Passwort EINMAL beim Start ins Log.
# Ohne eigenen Weg dorthin waere es praktisch unauffindbar - "k l" sucht
# nur KI-Zeilen, und im vollen Journal geht es zwischen den Zugriffs-
# zeilen des Panels unter.
PANELMUSTER="Web-Panel|WEBPANEL"

# $1 = Ueberschrift, $2 = Suchmuster
logs() {
    trenner
    printf '\033[1m%s\033[0m\n' "${1:-Letzte KI-Fehler im Journal}"
    muster="${2:-$KIMUSTER}"
    if ! command -v journalctl >/dev/null 2>&1; then
        echo "  (journalctl gibt es hier nicht)"
        return
    fi
    # Nur die Ursachenzeilen, nicht der ganze Traceback - das passt sonst auf
    # kein Handy-Display.
    #
    # "KI:" MUSS mit rein: genau diese Zeilen sagen, was Flo selbst repariert hat
    # und was dauerhaft in die .env gehoert ("wechsle selbst auf ...", "Signatur
    # ... funktioniert"). Ohne sie verschweigt der Arzt die Heilung.
    treffer="$(journalctl -u flobot --since -6h --no-pager 2>/dev/null \
        | grep -E "$muster" \
        | tail -8)"
    if [ -n "$treffer" ]; then
        # journalctl setzt FUENF Felder davor: "Aug 19 14:25:20 Ubuntu python3[8422]:"
        # - mit vier blieb "python3[8422]:" stehen und frass die Zeilenbreite.
        echo "$treffer" | sed 's/^[^ ]* [^ ]* [^ ]* [^ ]* [^ ]* //' | cut -c1-200
    else
        echo "  Nichts dazu in den letzten 6 Stunden."
    fi
}

# Musik: die Zeilen, an denen man einen Abspiel-Fehler wirklich erkennt.
MUSIKMUSTER="Spotify|Musik-Feature|yt-dlp|403 Forbidden|DRM|brach nach|bricht immer wieder|Track konnte nicht|Voice-Connect|Stream-Adresse"

case "${1:-}" in
    n|neu)
        printf '\033[1mHole neuen Stand...\033[0m\n'
        git pull --ff-only || exit 1
        exec bash k
        ;;
    l|log)
        logs
        ;;
    a|alles)
        "$PY" tools_check.py
        logs "Letzte Meldungen im Journal" "$KIMUSTER|$MUSIKMUSTER"
        ;;
    p|panel|passwort)
        logs "Web-Panel: Zugang und Zustand" "$PANELMUSTER"
        trenner
        echo "Passwort dauerhaft machen:  WEBPANEL_PASS=...  in die .env"
        ;;
    m|musik)
        "$PY" tools_musik_check.py
        logs "Letzte Musik-Meldungen im Journal" "$MUSIKMUSTER"
        ;;
    r|neustart)
        systemctl restart flobot && echo "flobot neu gestartet." && sleep 3 && logs
        ;;
    setup)
        # Winziger Starter in den Pfad, damit ueberall nur noch  k  reicht.
        ziel="/usr/local/bin/k"
        hier="$(pwd)"
        if printf '#!/bin/sh\nexec bash "%s/k" "$@"\n' "$hier" > "$ziel" 2>/dev/null; then
            chmod +x "$ziel"
            echo "Fertig. Ab jetzt reicht ueberall:   k"
            echo "(und  k n  /  k l  /  k r  wie gehabt)"
        else
            echo "Konnte $ziel nicht schreiben - als root nochmal versuchen."
            exit 1
        fi
        ;;
    *)
        "$PY" tools_ki_check.py
        logs
        trenner
        echo "k a = ALLES | k n = neu holen | k l = Log | k m = Musik | k r = Neustart"
        ;;
esac
