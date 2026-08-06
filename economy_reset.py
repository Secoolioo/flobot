#!/usr/bin/env python3
"""Wirtschafts-Reset fuer Flo - die eigentliche JSON-Arbeit.

Wird von economy_reset.sh aufgerufen (das Backup + Rueckfrage macht das Shell-
Skript). Direkt starten kann man es auch, dann OHNE Backup:

    FLO_DATA_DIR=data FLO_DRY_RUN=1 python3 economy_reset.py

Regel: alles, was mit Flo Coins zu tun hat, geht auf 0. Ausdruecklich ERHALTEN
bleiben pro Profil:

    xp          -> Level
    voice_secs  -> Voice-Stunden
    msgs        -> Chat-Nachrichten
    name        -> Anzeigename (nur Anzeige, kein Guthaben)

Unbekannte Zusatzfelder bleiben ebenfalls stehen - zurueckgesetzt wird nur, was
hier ausdruecklich aufgelistet ist. Dateien, die kein Geld enthalten (words,
moderation, voicegags, admin, features), werden nicht angefasst; aus games.json
wird NUR die Spiele-Tageskappe geleert, der Zaehlspiel-Stand bleibt.
"""

import json
import os
import re
import sys
from pathlib import Path

# --- Umgebung ---------------------------------------------------------------
DATA_DIR = Path(os.getenv("FLO_DATA_DIR")
                or (Path(__file__).resolve().parent / "data"))
DRY_RUN = (os.getenv("FLO_DRY_RUN", "0").strip().lower()
           in ("1", "true", "yes", "on"))

# Startwerte muessen zu den Modulen passen. Sie werden direkt aus dem Quelltext
# gelesen statt hier abgeschrieben: importieren geht nicht (die Module ziehen
# discord mit), und eine Kopie laeuft irgendwann auseinander - genau das ist
# passiert (Thron stand nach dem Reset auf dem ALTEN Startpreis).
_HIER = Path(__file__).resolve().parent


def _konstante(datei, name, default):
    """Liest 'NAME = 12_345' aus einer Python-Datei (ohne sie zu importieren)."""
    try:
        quelle = (_HIER / datei).read_text(encoding="utf-8")
    except OSError:
        return default
    m = re.search(rf"^{re.escape(name)}\s*=\s*([0-9_]+)", quelle, re.MULTILINE)
    if m:
        try:
            return int(m.group(1).replace("_", ""))
        except ValueError:
            pass
    # Variante mit os.getenv-Standard: NAME = int(os.getenv("X", "1000") or "1000")
    m = re.search(rf"^{re.escape(name)}\s*=.*?getenv\([^,]+,\s*\"([0-9_]+)\"",
                  quelle, re.MULTILINE)
    if m:
        try:
            return int(m.group(1).replace("_", ""))
        except ValueError:
            pass
    return default


START_PRICE = int(os.getenv("FLOAKTIE_START_PRICE", "")
                  or _konstante("floaktie.py", "START_PRICE", 1000))
THRONE_START = int(os.getenv("LUXUS_THRONE_START", "")
                   or _konstante("luxus.py", "THRONE_START", 100_000))

# --- Ausgabe ----------------------------------------------------------------
_FARBE = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _FARBE else text


def rot(t):
    return _c("31", t)


def gruen(t):
    return _c("32", t)


def gelb(t):
    return _c("33", t)


def grau(t):
    return _c("90", t)


def fett(t):
    return _c("1", t)


def fmt(n):
    """1234567 -> '1.234.567' (deutsche Tausenderpunkte)."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(n)


# --- Profil-Regeln ----------------------------------------------------------
# Was pro Nutzerprofil auf den Startwert zurueckgeht (alles Coin-nah).
PROFIL_RESET = {
    "coins": 0,            # Guthaben
    "earned": 0,           # Lebensverdienst (Statistik/Leaderboard)
    "streak": 0,           # Daily-Serie -> Bonus
    "last_daily": "",      # damit das Daily sofort wieder geht
    "owned": [],           # gekaufte Titel/Inventar
    "title": "",           # getragener Titel
    "title_rarity": "",
}
# Was ausdruecklich bleibt (nur zur Anzeige/Doku - wird nie geschrieben).
PROFIL_BEHALTEN = ("xp", "voice_secs", "msgs", "name")


def _leer(wert):
    """Ist der Wert schon 'auf 0'? (0 / '' / [] / {} / None zaehlen als leer.)"""
    return wert in (0, 0.0, "", None) or wert == [] or wert == {}


def _mehrzahl(n, eins, viele):
    """'1 Titel' / '3 Titel' - deutsche Ein-/Mehrzahl ohne 'Eintrage'-Unfaelle."""
    return f"{fmt(n)} {eins if abs(int(n)) == 1 else viele}"


# --- Datei-Helfer -----------------------------------------------------------
class Fehler(Exception):
    """Abbruchgrund, den wir dem Nutzer lesbar zeigen koennen."""


def laden(pfad):
    """Liest eine JSON-Datei. Rueckgabe None, wenn sie (noch) nicht existiert."""
    try:
        with pfad.open("r", encoding="utf-8") as fh:
            daten = json.load(fh)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise Fehler(f"{pfad.name} ist kein gueltiges JSON ({exc}) - "
                     f"Reset abgebrochen, nichts geaendert.") from exc
    except OSError as exc:
        raise Fehler(f"{pfad.name} nicht lesbar ({exc}).") from exc
    if not isinstance(daten, dict):
        raise Fehler(f"{pfad.name} enthaelt kein Objekt - Reset abgebrochen.")
    return daten


def schreiben(pfad, daten):
    """Schreibt atomar (tmp + fsync + replace) wie store.py und prueft danach nach."""
    payload = json.dumps(daten, ensure_ascii=False, indent=2)
    tmp = pfad.with_suffix(pfad.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, pfad)
        # Die Sicherung von store.py enthaelt jetzt noch den Stand VOR dem Reset.
        # Bliebe sie liegen, wuerde der Bot bei einem spaeteren Defekt genau die
        # alten Coins zurueckholen - der Reset waere rueckgaengig gemacht. Das
        # Backup des .sh-Skripts ist der richtige Ort dafuer, nicht diese Datei.
        bak = pfad.with_name(pfad.name + ".bak")
        if bak.exists():
            try:
                bak.unlink()
            except OSError:
                pass
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise Fehler(f"{pfad.name} konnte nicht geschrieben werden ({exc}).") from exc
    # Gegenprobe: lieber jetzt laut scheitern als spaeter im Bot.
    try:
        with pfad.open("r", encoding="utf-8") as fh:
            json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise Fehler(f"{pfad.name} ist nach dem Schreiben unlesbar ({exc}).") from exc


def setze(d, spec):
    """Setzt Schluessel aus spec, wenn sie abweichen. Gibt die Anzahl Aenderungen."""
    n = 0
    for key, neu in spec.items():
        if key in d and d[key] == neu:
            continue
        d[key] = json.loads(json.dumps(neu)) if isinstance(neu, (list, dict)) else neu
        n += 1
    return n


# --- Die einzelnen Dateien --------------------------------------------------
# Jede Funktion bekommt die geladenen Daten, aendert sie in-place und gibt
# (anzahl_aenderungen, [zeilen fuer den Bericht]) zurueck.

def reset_economy(d, bericht):
    """Profile: Coins/Inventar/Titel/Streak auf 0 - Level, Stunden, Nachrichten bleiben."""
    users = d.get("users")
    if not isinstance(users, dict):
        users = {}
        d["users"] = users

    n_user = 0
    coins_weg = 0
    titel_weg = 0
    inv_weg = 0
    for uid, prof in list(users.items()):
        if not isinstance(prof, dict):
            # Kaputtes Profil: auf ein sauberes Minimalprofil ziehen.
            users[uid] = {"xp": 0, "voice_secs": 0, "msgs": 0, "name": "",
                          **PROFIL_RESET}
            n_user += 1
            continue
        vorher_coins = int(prof.get("coins") or 0)
        vorher_owned = prof.get("owned") or []
        hatte_titel = bool(prof.get("title"))
        # Als "zurueckgesetzt" zaehlt nur, wer wirklich etwas hatte - ein Profil,
        # dem bloss ein Feld fehlte, soll die Zahl im Bericht nicht aufblasen.
        hatte_was = any(not _leer(prof.get(k)) for k in PROFIL_RESET)
        setze(prof, PROFIL_RESET)
        if hatte_was:
            n_user += 1
            coins_weg += max(0, vorher_coins)
            inv_weg += len(vorher_owned) if isinstance(vorher_owned, list) else 0
            titel_weg += 1 if hatte_titel else 0

    # Steuer-Stempel loeschen: sonst haelt der Reset-Tag die Vermoegenssteuer fuer
    # erledigt (harmlos, aber unsauber - nach dem Reset ist ohnehin niemand ueber
    # dem Freibetrag).
    d["tax_day"] = ""

    # Tages-Shop neu wuerfeln lassen (Preise/Angebot kommen aus dem Code).
    shop = d.get("shop")
    if not isinstance(shop, dict) or shop.get("date") or shop.get("items"):
        d["shop"] = {"date": "", "items": []}

    bericht.append(f"{_mehrzahl(n_user, 'Profil', 'Profile')} geleert "
                   f"({fmt(coins_weg)} Coins, "
                   f"{_mehrzahl(inv_weg, 'Inventar-Eintrag', 'Inventar-Einträge')}, "
                   f"{_mehrzahl(titel_weg, 'getragener Titel', 'getragene Titel')})")
    bericht.append(grau(f"behalten: {', '.join(PROFIL_BEHALTEN)} · "
                        f"{fmt(len(users))} Profile bleiben bestehen"))
    return n_user, coins_weg


def reset_floaktie(d, bericht):
    """Aktie komplett auf Start: keine Depots, kein Kursverlauf, kein Momentum."""
    aktionaere = len(d.get("holdings") or {})
    anteile = 0
    for v in (d.get("holdings") or {}).values():
        try:
            anteile += int(v if not isinstance(v, dict) else v.get("shares", 0))
        except (TypeError, ValueError):
            pass
    alt_kurs = d.get("price")
    setze(d, {
        "price": START_PRICE, "base": float(START_PRICE), "day": "",
        "act_ema": 0.0, "msg_count": 0, "last_msg_count": 0,
        "leer_min": 0, "pulse_min": 0, "pulse_sum": 0.0,
        # Tagesbremse (Circuit Breaker) MIT zuruecksetzen - sonst klemmt das Band
        # von gestern den frischen Startkurs sofort wieder nach oben.
        "open_day": "", "open_base": 0,
        "holdings": {}, "history": [], "ticks": [],
    })
    bericht.append(f"Kurs {fmt(alt_kurs or 0)} → {fmt(START_PRICE)} Coins, "
                   f"{_mehrzahl(aktionaere, 'Depot', 'Depots')} mit "
                   f"{_mehrzahl(anteile, 'Anteil', 'Anteilen')} gelöscht, "
                   f"Verlauf geleert")
    return 0, 0


def reset_handel(d, bericht):
    """Handelsbuch (jede Coin-Bewegung) leeren - sonst zeigt die Bilanz Altlasten."""
    n = len(d.get("users") or {})
    d["users"] = {}
    bericht.append(f"Bilanz/Historie von {_mehrzahl(n, 'Nutzer', 'Nutzern')} gelöscht")
    return 0, 0


def reset_luxus(d, bericht):
    """Luxus-Besitz weg, Thron wieder frei und beim Startpreis."""
    n = len(d.get("users") or {})
    besitz = sum(len(v) for v in (d.get("users") or {}).values()
                 if isinstance(v, list))
    thron = (d.get("throne") or {}).get("owner") or "-"
    d["users"] = {}
    d["throne"] = {"owner": "", "preis": THRONE_START, "n": 0}
    bericht.append(f"{_mehrzahl(besitz, 'Luxus-Objekt', 'Luxus-Objekte')} von "
                   f"{_mehrzahl(n, 'Nutzer', 'Nutzern')} entfernt, "
                   f"Thron frei (vorher: {thron}, jetzt ab {fmt(THRONE_START)} Coins)")
    return 0, 0


def reset_merchant(d, bericht):
    """Haendler: Lager, Kaufhistorie und Besuchsplan zuruecksetzen."""
    trades = len(d.get("trades") or [])
    setze(d, {"day": "", "appear_at": 0, "depart_at": 0,
              "arrived": False, "departed": False,
              "stock": [], "trades": [], "sold": {}})
    bericht.append(f"{_mehrzahl(trades, 'Händler-Geschäft', 'Händler-Geschäfte')} und das Lager gelöscht, "
                   f"nächster Besuch wird neu geplant")
    return 0, 0


def reset_lotto(d, bericht):
    """Lotto: Lose, Jackpot und Hauskasse auf 0 (Monat startet neu)."""
    lose = 0
    for v in (d.get("entries") or {}).values():
        try:
            lose += int(v if not isinstance(v, dict) else v.get("n", 0))
        except (TypeError, ValueError):
            pass
    jackpot = int(d.get("jackpot") or 0)
    haus = int(d.get("house") or 0)
    setze(d, {"month": "", "jackpot": 0, "ticket_price": 0,
              "entries": {}, "house": 0, "history": []})
    bericht.append(f"{_mehrzahl(lose, 'Los', 'Lose')} storniert, Jackpot {fmt(jackpot)} und "
                   f"Hauskasse {fmt(haus)} auf 0, Monat startet neu")
    return 0, 0


def reset_schulden(d, bericht):
    """Kreide-Tafel: alle offenen Schulden erlassen."""
    paare = len(d.get("pairs") or {})
    setze(d, {"pairs": {}, "stats": {}})
    bericht.append(f"{_mehrzahl(paare, 'Schuldverhältnis', 'Schuldverhältnisse')} erlassen")
    return 0, 0


def reset_casino(d, bericht):
    """Casino-Statistik (Einsatz/Gewinn pro Nutzer) leeren."""
    n = len(d.get("stats") or {})
    d["stats"] = {}
    bericht.append(f"Casino-Statistik von {_mehrzahl(n, 'Nutzer', 'Nutzern')} gelöscht")
    return 0, 0


def reset_steal(d, bericht):
    """Raub-Cooldowns loeschen - alle starten gleich."""
    n = len(d.get("cooldowns") or {})
    d["cooldowns"] = {}
    bericht.append(f"{_mehrzahl(n, 'Raub-Cooldown', 'Raub-Cooldowns')} aufgehoben")
    return 0, 0


def reset_spiele_kappe(d, bericht):
    """NUR die Spiele-Tageskappe leeren - der Zaehlspiel-Stand bleibt.

    Ohne das startet jeder in den Reset-Tag mit einer schon ausgeschoepften Kappe
    (bis zu 50.000 Coins, also mehrere gute Tage) und wundert sich, warum Spiele
    nichts mehr bringen."""
    kappe = d.get("daily") or {}
    betroffen = len(kappe.get("won") or {})
    d["daily"] = {"day": "", "won": {}}
    if betroffen:
        bericht.append(f"Spiele-Tageskappe von "
                       f"{_mehrzahl(betroffen, 'Nutzer', 'Nutzern')} freigegeben")
    else:
        bericht.append("Spiele-Tageskappe war leer")
    bericht.append(grau("Zählspiel-Stand bleibt (kein Geld)"))
    return 0, 0


def reset_giveaway(d, bericht):
    """Laufende Giveaways beenden (Einsatz liegt im Escrow und verfaellt mit dem Reset)."""
    aktiv = len(d.get("active") or {})
    setze(d, {"active": {}, "next_id": 1, "done": []})
    if aktiv:
        nachsatz = ("(Einsatz verfällt mit dem Reset)" if aktiv == 1
                    else "(Einsätze verfallen mit dem Reset)")
        bericht.append(gelb(f"{_mehrzahl(aktiv, 'laufendes Giveaway', 'laufende Giveaways')}"
                            f" abgebrochen {nachsatz}"))
    else:
        bericht.append("keine laufenden Giveaways")
    return 0, 0


# Reihenfolge = Reihenfolge im Bericht. (Datei, Funktion, Beschreibung)
DATEIEN = (
    ("economy.json",  reset_economy,  "Guthaben, Inventar, Titel, Streaks"),
    ("floaktie.json", reset_floaktie, "Aktie: Depots, Kurs, Verlauf"),
    ("handel.json",   reset_handel,   "Handelsbuch/Bilanz"),
    ("luxus.json",    reset_luxus,    "Luxus-Besitz + Thron"),
    ("merchant.json", reset_merchant, "Händler: Lager & Käufe"),
    ("lotto.json",    reset_lotto,    "Lotto: Lose, Jackpot, Hauskasse"),
    ("schulden.json", reset_schulden, "Kreide-Tafel (Schulden)"),
    ("casino.json",   reset_casino,   "Casino-Statistik"),
    ("steal.json",    reset_steal,    "Raub-Cooldowns"),
    ("giveaway.json", reset_giveaway, "laufende Giveaways"),
    ("games.json",    reset_spiele_kappe, "Spiele-Tageskappe"),
)

# Dateien, die absichtlich unberuehrt bleiben (fuer den Bericht).
UNBERUEHRT = ("words.json", "moderation.json", "voicegags.json",
              "admin.json", "features.json")


def main():
    if not DATA_DIR.is_dir():
        print(rot(f"  Datenordner nicht gefunden: {DATA_DIR}"))
        return 1

    print(fett("  Was zurückgesetzt wird"))
    print()

    nutzer_gesamt = 0
    coins_gesamt = 0
    geaendert = []
    fehlend = []

    # ZWEI PHASEN. Vorher wurde Datei fuer Datei gelesen, umgebaut UND SOFORT
    # geschrieben - ein kaputtes JSON in der Mitte liess die Wirtschaft halb
    # zurueckgesetzt zurueck (economy/floaktie/handel auf 0, lotto mit 47 Mio
    # Jackpot stehen), und dazu meldete das Skript auch noch "nichts geaendert".
    # Jetzt wird erst ALLES eingelesen und umgebaut; geschrieben wird nur, wenn
    # jede einzelne Datei fehlerfrei durchgelaufen ist.
    fertig = []          # (pfad, daten) - wartet auf die Schreibphase

    for name, fn, beschreibung in DATEIEN:
        pfad = DATA_DIR / name
        try:
            daten = laden(pfad)
        except Fehler as exc:
            print(rot(f"  ✗ {exc}"))
            print(gelb("     Es wurde NICHTS geschrieben."))
            return 1
        if daten is None:
            fehlend.append(name)
            continue

        bericht = []
        try:
            n_user, coins = fn(daten, bericht)
        except Fehler as exc:
            print(rot(f"  ✗ {exc}"))
            print(gelb("     Es wurde NICHTS geschrieben."))
            return 1
        except Exception as exc:  # noqa: BLE001 - nie halb resetten
            print(rot(f"  ✗ {name}: unerwarteter Fehler ({exc}) - abgebrochen."))
            print(gelb("     Es wurde NICHTS geschrieben."))
            return 1

        nutzer_gesamt += n_user
        coins_gesamt += coins

        print(f"  {fett(name.ljust(15))} {grau(beschreibung)}")
        for zeile in bericht:
            print(f"      {zeile}")
        fertig.append((pfad, daten))
        geaendert.append(name)
        print()

    # --- Schreibphase: alles oder nichts (soweit das mit Dateien geht) -------
    if not DRY_RUN:
        for i, (pfad, daten) in enumerate(fertig):
            try:
                schreiben(pfad, daten)
            except Fehler as exc:
                print(rot(f"  ✗ {exc}"))
                if i:
                    print(rot(f"     ACHTUNG: {i} Datei(en) waren schon geschrieben."))
                    print(gelb("     Backup zurueckspielen und erneut versuchen!"))
                return 1

    if fehlend:
        print(grau(f"  übersprungen (Datei existiert nicht): {', '.join(fehlend)}"))
    print(grau(f"  nicht angefasst: {', '.join(UNBERUEHRT)}"))
    print()

    # --- Abschluss ----------------------------------------------------------
    print(fett("  ═══════════════════════════════════════════════════════════"))
    kopf = "PROBELAUF" if DRY_RUN else "ERGEBNIS"
    print(fett(f"  {kopf}"))
    zeile = (f"  {_mehrzahl(nutzer_gesamt, 'Nutzer', 'Nutzer')} zurückgesetzt · "
             f"{fmt(coins_gesamt)} Flo Coins vernichtet · "
             f"{_mehrzahl(len(geaendert), 'Datei', 'Dateien')}")
    print(gelb(zeile) if DRY_RUN else gruen(zeile))
    print(gruen("  Behalten: Level/XP · Voice-Stunden · Chat-Nachrichten · Namen"))
    print(fett("  ═══════════════════════════════════════════════════════════"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print(gelb("  Abgebrochen."))
        sys.exit(130)
