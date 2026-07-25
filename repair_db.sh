#!/usr/bin/env bash
# ============================================================================
#  Flo Datenbank-Reparatur - findet und korrigiert Exploit-Schaeden
# ----------------------------------------------------------------------------
#  Sucht auffaellige Konten (absurd viele Coins / Anteile) und setzt sie auf
#  einen normalen Stand zurueck. Casino-Statistik der Betroffenen wird geleert.
#
#  SICHERHEIT (bewusst so gebaut):
#   * OHNE --apply wird NICHTS geaendert - es gibt nur einen Bericht.
#   * Mit --apply wird VORHER ein vollstaendiges Backup angelegt.
#   * Laeuft der Bot noch, bricht das Skript ab: der laufende Bot haelt die
#     Daten im Speicher und wuerde die Datei sofort wieder ueberschreiben.
#
#  Benutzung:
#     ./repair_db.sh                      # nur ansehen (aendert nichts)
#     sudo systemctl stop flobot
#     ./repair_db.sh --apply              # reparieren (mit Backup)
#     sudo systemctl start flobot
#
#  Optionen:
#     --apply                 Aenderungen wirklich schreiben (sonst nur Bericht)
#     --coins-max N           ab so vielen Coins gilt ein Konto als auffaellig
#                             (Standard 70000000 = 70 Mio)
#     --shares-max N          ab so vielen $FLO-Anteilen auffaellig (Standard 1000)
#     --reset-coins N         Coins-DECKEL fuer auffaellige Konten (Standard
#                             1000000). Es wird NIE erhoeht - wer weniger hat,
#                             behaelt seinen Stand.
#     --reset-shares N        auf so viele Anteile zuruecksetzen (Standard 0)
#     --include-owner         auch das Owner-Konto pruefen (standardmaessig
#                             ausgenommen: dessen Admin-Gutschriften zaehlen als
#                             'verdient' und sind kein Exploit)
#     --skip ID[,ID,...]      diese Konten komplett ueberspringen
#     --price N               Aktienkurs danach auf N setzen
#     --keep-price            Kurs unveraendert lassen (statt ihn ehrlich neu
#                             aus dem Basiskurs zu bestimmen)
#     --keep-stats            Casino-Statistik der Betroffenen NICHT leeren
#     --data VERZEICHNIS      Datenverzeichnis (Standard: ./data)
#     --force                 auch bei laufendem Bot ausfuehren (NICHT empfohlen)
#     -h | --help             diese Hilfe
# ============================================================================
set -euo pipefail

APPLY=0; FORCE=0; KEEP_PRICE=0; KEEP_STATS=0; INCLUDE_OWNER=0
COINS_MAX=70000000
SHARES_MAX=1000
RESET_COINS=1000000
RESET_SHARES=0
SET_PRICE=""
SKIP_IDS=""
DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/data"
# Owner-ID: aus der .env lesen, sonst der eingebaute Standard (wie in bot.py).
# ('|| true': fehlt die .env oder die Zeile, darf 'set -e' nicht zuschlagen.)
OWNER_ID="$( { grep -E '^OWNER_ID=' "$(dirname "${BASH_SOURCE[0]}")/.env" 2>/dev/null \
               | tail -1 | cut -d= -f2 | tr -d ' "'"'"'\r'; } || true )"
OWNER_ID="${OWNER_ID:-1040135855710404659}"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    --keep-price) KEEP_PRICE=1 ;;
    --keep-stats) KEEP_STATS=1 ;;
    --include-owner) INCLUDE_OWNER=1 ;;
    --skip) SKIP_IDS="${2:?}"; shift ;;
    --coins-max) COINS_MAX="${2:?}"; shift ;;
    --shares-max) SHARES_MAX="${2:?}"; shift ;;
    --reset-coins) RESET_COINS="${2:?}"; shift ;;
    --reset-shares) RESET_SHARES="${2:?}"; shift ;;
    --price) SET_PRICE="${2:?}"; shift ;;
    --data) DATA_DIR="${2:?}"; shift ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unbekannte Option: $1 (--help fuer Hilfe)" >&2; exit 2 ;;
  esac
  shift
done

if [ ! -d "$DATA_DIR" ]; then
  echo "❌ Datenverzeichnis nicht gefunden: $DATA_DIR" >&2
  echo "   Mit --data <pfad> das richtige angeben." >&2
  exit 1
fi

# --- Laeuft der Bot? Dann NICHT schreiben (er ueberschreibt sonst alles) -----
if [ "$APPLY" = "1" ] && [ "$FORCE" = "0" ]; then
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet flobot 2>/dev/null; then
    echo "❌ Der Bot laeuft noch (systemd: flobot)."
    echo "   Er haelt die Daten im Speicher und wuerde die Reparatur sofort"
    echo "   ueberschreiben. Bitte zuerst stoppen:"
    echo ""
    echo "     sudo systemctl stop flobot"
    echo "     $0 --apply"
    echo "     sudo systemctl start flobot"
    exit 1
  fi
fi

# --- Backup vor jeder Aenderung ---------------------------------------------
if [ "$APPLY" = "1" ]; then
  BACKUP="$DATA_DIR/backup_$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$BACKUP"
  cp -a "$DATA_DIR"/*.json "$BACKUP"/ 2>/dev/null || true
  echo "💾 Backup angelegt: $BACKUP"
  echo ""
fi

DATA_DIR="$DATA_DIR" APPLY="$APPLY" COINS_MAX="$COINS_MAX" SHARES_MAX="$SHARES_MAX" \
RESET_COINS="$RESET_COINS" RESET_SHARES="$RESET_SHARES" SET_PRICE="$SET_PRICE" \
KEEP_PRICE="$KEEP_PRICE" KEEP_STATS="$KEEP_STATS" OWNER_ID="$OWNER_ID" \
INCLUDE_OWNER="$INCLUDE_OWNER" SKIP_IDS="$SKIP_IDS" python3 - <<'PYTHON'
import json, os, sys
from pathlib import Path

D          = Path(os.environ["DATA_DIR"])
APPLY      = os.environ["APPLY"] == "1"
COINS_MAX  = int(os.environ["COINS_MAX"])
SHARES_MAX = int(os.environ["SHARES_MAX"])
RESET_COINS  = int(os.environ["RESET_COINS"])
RESET_SHARES = int(os.environ["RESET_SHARES"])
SET_PRICE  = os.environ["SET_PRICE"].strip()
KEEP_PRICE = os.environ["KEEP_PRICE"] == "1"
KEEP_STATS = os.environ["KEEP_STATS"] == "1"
OWNER_ID   = os.environ.get("OWNER_ID", "").strip()
INCLUDE_OWNER = os.environ.get("INCLUDE_OWNER") == "1"
SKIP = {s.strip() for s in os.environ.get("SKIP_IDS", "").split(",") if s.strip()}
if OWNER_ID and not INCLUDE_OWNER:
    SKIP.add(OWNER_ID)

MIN_PRICE = 50          # wie in floaktie.py
LIQUIDITY = 750         # wie in floaktie.py

def fmt(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)

def load(name):
    p = D / name
    if not p.exists():
        return None, p
    try:
        with p.open(encoding="utf-8") as fh:
            return json.load(fh), p
    except Exception as exc:
        print(f"⚠️  {name} nicht lesbar ({exc}) - wird uebersprungen.")
        return None, p

def save(data, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

eco,  eco_p  = load("economy.json")
flo,  flo_p  = load("floaktie.json")
cas,  cas_p  = load("casino.json")

if eco is None:
    print("❌ economy.json fehlt - nichts zu tun.")
    sys.exit(1)

users    = eco.setdefault("users", {})
holdings = (flo or {}).get("holdings", {}) or {}

print("═══════════════════════════════════════════════════════════════════")
print("  FLO DATENBANK-PRUEFUNG")
print("═══════════════════════════════════════════════════════════════════")
print(f"  Verzeichnis:      {D}")
print(f"  Nutzer insgesamt: {len(users)}")
print(f"  Auffaellig ab:    {fmt(COINS_MAX)} Coins  /  {fmt(SHARES_MAX)} Anteile")
print(f"  Modus:            {'REPARIEREN (--apply)' if APPLY else 'nur Bericht (nichts wird geaendert)'}")
print("")

# --- Auffaellige Konten finden ---------------------------------------------
# WICHTIG: Coins werden NIE erhoeht. Wer schon weniger hat als der Deckel (z. B.
# weil du manuell heruntergesetzt hast), behaelt seinen Stand - repariert wird nur,
# was wirklich zu hoch ist. Und es wird nur angefasst, was auffaellig IST:
# jemand mit Exploit-ANTEILEN behaelt seine Coins.
flagged = []
for uid, prof in users.items():
    if str(uid) in SKIP:
        continue
    coins  = int(prof.get("coins", 0) or 0)
    earned = int(prof.get("earned", 0) or 0)
    shares = int(holdings.get(str(uid), 0) or 0)
    grund, plan = [], {}
    if coins > COINS_MAX:
        grund.append(f"Coins {fmt(coins)}")
        plan["coins"] = min(coins, RESET_COINS)          # nur runter, nie hoch
    if earned > COINS_MAX:
        grund.append(f"verdient {fmt(earned)}")
        # Lebenszeit-Wert deckeln, aber nie unter den (bleibenden) Kontostand.
        plan["earned"] = max(plan.get("coins", coins), min(earned, RESET_COINS))
    if shares > SHARES_MAX:
        grund.append(f"Anteile {fmt(shares)}")
        plan["shares"] = min(shares, RESET_SHARES)
    if grund:
        flagged.append((uid, prof, coins, earned, shares, grund, plan))

flagged.sort(key=lambda t: t[2], reverse=True)

# --- Uebersicht: die groessten Konten (auch die unauffaelligen) -------------
top = sorted(users.items(), key=lambda kv: int(kv[1].get("coins", 0) or 0), reverse=True)[:10]
print("  Groesste Konten (Top 10):")
for uid, prof in top:
    coins  = int(prof.get("coins", 0) or 0)
    shares = int(holdings.get(str(uid), 0) or 0)
    mark   = "  ⚠️ AUFFAELLIG" if any(f[0] == uid for f in flagged) else ""
    name   = (prof.get("name") or "?")[:20]
    print(f"    {uid:<20} {name:<22} {fmt(coins):>18} Coins  "
          f"{fmt(shares):>10} Anteile{mark}")
print("")

if flo is not None:
    ges_anteile = sum(int(v or 0) for v in holdings.values())
    print(f"  Aktie:  Kurs {fmt(flo.get('price', 0))}  ·  "
          f"Anteile im Umlauf {fmt(ges_anteile)}  ·  "
          f"Basiskurs {fmt(round(float(flo.get('base') or 0)))}")
    print("")

if not flagged:
    print("  ✅ Keine auffaelligen Konten gefunden - alles im gruenen Bereich.")
    sys.exit(0)

print("───────────────────────────────────────────────────────────────────")
print(f"  {len(flagged)} auffaellige(s) Konto/Konten:")
print("───────────────────────────────────────────────────────────────────")
for uid, prof, coins, earned, shares, grund, plan in flagged:
    name = (prof.get("name") or "?")[:24]
    print(f"    ⚠️  {uid}  ({name})")
    print(f"        Grund:   {', '.join(grund)}")
    if "coins" in plan:
        print(f"        Coins:   {fmt(coins)}  ->  {fmt(plan['coins'])}")
    else:
        print(f"        Coins:   {fmt(coins)}  (unverändert)")
    if "earned" in plan:
        print(f"        Verdient:{fmt(earned)}  ->  {fmt(plan['earned'])}")
    if "shares" in plan:
        print(f"        Anteile: {fmt(shares)}  ->  {fmt(plan['shares'])}")
    if cas and not KEEP_STATS and str(uid) in (cas.get("stats") or {}):
        st = cas["stats"][str(uid)]
        print(f"        Casino:  {st.get('games', 0)} Runden, "
              f"gewonnen {fmt(st.get('won', 0))}  ->  Statistik wird geleert")
print("")
if SKIP:
    print(f"  (ausgenommen: {', '.join(sorted(SKIP))}"
          f"{' - Owner' if OWNER_ID in SKIP and not INCLUDE_OWNER else ''})")
    print("")

if not APPLY:
    print("═══════════════════════════════════════════════════════════════════")
    print("  Es wurde NICHTS geaendert (Bericht-Modus).")
    print("  Zum Reparieren:")
    print("      sudo systemctl stop flobot")
    print("      ./repair_db.sh --apply")
    print("      sudo systemctl start flobot")
    print("")
    print("  Feinjustierung z. B.:")
    print("      ./repair_db.sh --coins-max 50000000 --reset-coins 500000 --apply")
    print("═══════════════════════════════════════════════════════════════════")
    sys.exit(0)

# --- Reparieren -------------------------------------------------------------
for uid, prof, coins, earned, shares, _g, plan in flagged:
    if "coins" in plan:
        prof["coins"] = plan["coins"]
    if "earned" in plan:
        prof["earned"] = plan["earned"]
    if "shares" in plan:
        if plan["shares"] > 0:
            holdings[str(uid)] = plan["shares"]
        else:
            holdings.pop(str(uid), None)
    if cas and not KEEP_STATS:
        (cas.get("stats") or {}).pop(str(uid), None)
save(eco, eco_p)
print(f"✔ economy.json repariert ({len(flagged)} Konto/Konten).")

if flo is not None:
    flo["holdings"] = holdings
    ges = sum(int(v or 0) for v in holdings.values())
    if SET_PRICE:
        preis = max(MIN_PRICE, int(SET_PRICE))
        flo["base"] = preis / (1.0 + ges / LIQUIDITY)
        flo["price"] = preis
        print(f"✔ Aktienkurs auf {fmt(preis)} gesetzt.")
    elif KEEP_PRICE:
        preis = max(MIN_PRICE, int(flo.get("price") or MIN_PRICE))
        flo["base"] = preis / (1.0 + ges / LIQUIDITY)
        flo["price"] = preis
        print(f"✔ Aktienkurs unveraendert bei {fmt(preis)}.")
    else:
        # Ehrlich: Basiskurs behalten, der Kurs faellt auf das echte Niveau
        # (der hohe Kurs war ja eine FOLGE der Exploit-Anteile).
        base = float(flo.get("base") or 0) or float(flo.get("price") or MIN_PRICE)
        base = max(float(MIN_PRICE), base)
        flo["base"] = base
        flo["price"] = max(MIN_PRICE, int(round(base * (1.0 + ges / LIQUIDITY))))
        print(f"✔ Aktienkurs ehrlich neu bestimmt: {fmt(flo['price'])} "
              f"(Anteile im Umlauf: {fmt(ges)}).")
    save(flo, flo_p)
    print("✔ floaktie.json repariert.")

if cas is not None and not KEEP_STATS:
    save(cas, cas_p)
    print("✔ casino.json - Statistik der Betroffenen geleert.")

print("")
print("═══════════════════════════════════════════════════════════════════")
print("  ✅ Fertig. Jetzt den Bot wieder starten:")
print("       sudo systemctl start flobot")
print("  (Das Backup liegt im data-Verzeichnis unter backup_<datum>/)")
print("═══════════════════════════════════════════════════════════════════")
PYTHON
