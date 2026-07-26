#!/usr/bin/env bash
# =============================================================================
#  Flo - Wirtschafts-Reset
# =============================================================================
#  Setzt ALLES zurueck, was mit Flo Coins zu tun hat. AUSDRUECKLICH ERHALTEN
#  bleiben: Level/XP, Voice-Stunden und Chat-Nachrichten (plus der Name).
#
#  Ablauf auf dem Server:
#      cd /opt/flobot
#      git pull
#      sudo systemctl stop flobot          # WICHTIG - siehe unten
#      ./economy_reset.sh --dry-run        # erst schauen
#      ./economy_reset.sh                  # dann wirklich
#      sudo systemctl start flobot
#
#  Warum der Bot gestoppt sein MUSS: er haelt alle data/*.json im Speicher und
#  schreibt sie bei der naechsten Aenderung komplett zurueck. Laeuft er waehrend
#  des Resets, ueberschreibt er die zurueckgesetzten Dateien mit den alten
#  Werten aus dem Speicher - der Reset waere wirkungslos.
#
#  Optionen:
#      --dry-run     zeigt nur, was passieren wuerde (aendert NICHTS)
#      --yes         ohne Rueckfrage (fuer Skripte; sonst wird gefragt)
#      --data DIR    anderer Datenordner (Standard: ./data)
#      --keep-backup N   nur die letzten N Backups behalten (Standard: alle)
#      -h | --help   diese Hilfe
# =============================================================================

set -euo pipefail

SKRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$SKRIPT_DIR/data"
DRY_RUN=0
ASSUME_YES=0
KEEP_BACKUP=0

# Farben nur im Terminal - in eine Logdatei sollen keine Steuerzeichen wandern.
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    _f() { printf '\033[%sm%s\033[0m\n' "$1" "${*:2}"; }
else
    _f() { printf '%s\n' "${*:2}"; }
fi
rot()   { _f 31 "$*"; }
gruen() { _f 32 "$*"; }
gelb()  { _f 33 "$*"; }
fett()  { _f 1  "$*"; }

hilfe() {
    # Der Kopfkommentar IST die Hilfe: vom ersten '# ====' bis zum dritten.
    awk '/^# ={10,}/ {n++} n>=1 {z=$0; sub(/^# ?/, "", z); print z} n>=3 {exit}' \
        "${BASH_SOURCE[0]}"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)     DRY_RUN=1; shift ;;
        --yes|-y)      ASSUME_YES=1; shift ;;
        --data)        DATA_DIR="${2:?--data braucht einen Pfad}"; shift 2 ;;
        --keep-backup) KEEP_BACKUP="${2:?--keep-backup braucht eine Zahl}"; shift 2 ;;
        -h|--help)     hilfe ;;
        *) rot "Unbekannte Option: $1"; echo "Hilfe: $0 --help"; exit 2 ;;
    esac
done

# --- Vorbedingungen ----------------------------------------------------------
command -v python3 >/dev/null 2>&1 || { rot "python3 nicht gefunden."; exit 1; }

if [[ ! -d "$DATA_DIR" ]]; then
    rot "Datenordner nicht gefunden: $DATA_DIR"
    echo "Liegt er woanders? Dann: $0 --data /pfad/zu/data"
    exit 1
fi

echo
fett "═══════════════════════════════════════════════════════════════"
fett "  Flo · Wirtschafts-Reset"
fett "═══════════════════════════════════════════════════════════════"
echo "  Datenordner : $DATA_DIR"
echo "  Modus       : $([[ $DRY_RUN -eq 1 ]] && echo 'PROBELAUF (aendert nichts)' || echo 'ECHT')"
echo

# --- Laeuft der Bot noch? ----------------------------------------------------
BOT_LAEUFT=0
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet flobot 2>/dev/null; then BOT_LAEUFT=1; fi
fi
if [[ $BOT_LAEUFT -eq 0 ]] && pgrep -f "python.*bot\.py" >/dev/null 2>&1; then
    BOT_LAEUFT=1
fi

if [[ $BOT_LAEUFT -eq 1 ]]; then
    rot "  ⚠ Der Bot laeuft noch!"
    echo "     Er haelt die Daten im Speicher und wuerde den Reset ueberschreiben."
    echo "     Bitte zuerst:  sudo systemctl stop flobot"
    if [[ $DRY_RUN -eq 0 ]]; then
        exit 1
    fi
    gelb "     (Probelauf laeuft trotzdem weiter - es wird nichts geaendert.)"
    echo
fi

# --- Backup ------------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$SKRIPT_DIR/backups/economy-reset-$STAMP"

if [[ $DRY_RUN -eq 1 ]]; then
    gelb "  Backup: wuerde nach backups/economy-reset-$STAMP/ geschrieben"
else
    mkdir -p "$BACKUP_DIR"
    ANZ=0
    shopt -s nullglob
    for f in "$DATA_DIR"/*.json; do
        cp -p "$f" "$BACKUP_DIR/"
        ANZ=$((ANZ + 1))
    done
    shopt -u nullglob
    if [[ $ANZ -eq 0 ]]; then
        rot "  Keine *.json in $DATA_DIR gefunden - nichts zu tun."
        rmdir "$BACKUP_DIR" 2>/dev/null || true
        exit 1
    fi
    # Zusaetzlich ein Tar, das man in einem Stueck wegkopieren kann.
    tar -czf "$BACKUP_DIR.tar.gz" -C "$(dirname "$BACKUP_DIR")" "$(basename "$BACKUP_DIR")"
    gruen "  ✓ Backup: $ANZ Dateien"
    echo "     Ordner : $BACKUP_DIR"
    echo "     Archiv : $BACKUP_DIR.tar.gz"
    echo "     Zurueckspielen:  cp $BACKUP_DIR/*.json $DATA_DIR/"
fi
echo

# --- Sicherheitsabfrage ------------------------------------------------------
if [[ $DRY_RUN -eq 0 && $ASSUME_YES -eq 0 ]]; then
    fett "  Zurueckgesetzt wird: Coins, Aktien-Depots, Inventar/Titel, Streaks,"
    fett "  Handelsbuch, Luxus-Besitz, Thron, Lotto-Lose, Kreide-Tafel,"
    fett "  Casino-Statistiken, Raub-Cooldowns, laufende Giveaways."
    gruen "  ERHALTEN bleiben: Level/XP, Voice-Stunden, Chat-Nachrichten, Namen."
    echo
    read -r -p "  Wirklich ALLE Economy-Daten resetten? [yes/no] " ANTWORT
    if [[ "$ANTWORT" != "yes" ]]; then
        gelb "  Abgebrochen - es wurde nichts geaendert."
        exit 0
    fi
    echo
fi

# --- Der eigentliche Reset (Python macht die JSON-Arbeit) --------------------
export FLO_DATA_DIR="$DATA_DIR"
export FLO_DRY_RUN="$DRY_RUN"

python3 "$SKRIPT_DIR/economy_reset.py"
RC=$?

echo
if [[ $RC -ne 0 ]]; then
    rot "  Reset fehlgeschlagen (Code $RC)."
    [[ $DRY_RUN -eq 0 ]] && echo "     Backup liegt in: $BACKUP_DIR"
    exit $RC
fi

# --- Alte Backups aufraeumen (optional) --------------------------------------
if [[ $DRY_RUN -eq 0 && "$KEEP_BACKUP" =~ ^[0-9]+$ && "$KEEP_BACKUP" -gt 0 ]]; then
    mapfile -t ALTE < <(ls -1dt "$SKRIPT_DIR"/backups/economy-reset-* 2>/dev/null | tail -n +$((KEEP_BACKUP + 1)) || true)
    for a in "${ALTE[@]:-}"; do
        [[ -n "$a" ]] && rm -rf "$a" && echo "  (altes Backup entfernt: $(basename "$a"))"
    done
fi

if [[ $DRY_RUN -eq 1 ]]; then
    gelb "  Probelauf beendet - es wurde NICHTS geaendert."
    echo "     Echt ausfuehren:  $0"
else
    gruen "  ✓ Reset abgeschlossen."
    echo "     Jetzt:  sudo systemctl start flobot"
fi
echo
