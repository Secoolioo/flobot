#!/usr/bin/env bash
# Flo-Arzt. Kurzbefehl fuers Handy an der noVNC-Konsole:
#
#     bash k          alles pruefen (Konfig, Netz, Modell, echter Aufruf + Log)
#     bash k n        vorher git pull
#     bash k l        nur die letzten KI-Fehler aus dem Journal
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

logs() {
    trenner
    printf '\033[1mLetzte KI-Fehler im Journal\033[0m\n'
    if ! command -v journalctl >/dev/null 2>&1; then
        echo "  (journalctl gibt es hier nicht)"
        return
    fi
    # Nur die Ursachenzeilen, nicht der ganze Traceback - das passt sonst auf
    # kein Handy-Display. Die Zeile "KI-Fehler ..." kommt aus ai.py.
    treffer="$(journalctl -u flobot --since -6h --no-pager 2>/dev/null \
        | grep -E "KI-Fehler|LLM-Aufruf|Vision-Aufruf|LLM generate|KI-Feature" \
        | tail -8)"
    if [ -n "$treffer" ]; then
        echo "$treffer" | sed 's/^[^ ]* [^ ]* [^ ]* [^ ]* //' | cut -c1-160
    else
        echo "  Keine KI-Fehler in den letzten 6 Stunden."
    fi
}

case "${1:-}" in
    n|neu)
        printf '\033[1mHole neuen Stand...\033[0m\n'
        git pull --ff-only || exit 1
        exec bash k
        ;;
    l|log)
        logs
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
        echo "k n = neu holen | k l = Log | k r = Neustart"
        ;;
esac
