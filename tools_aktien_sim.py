"""Langzeit-Simulation der $FLO-Aktie - das Werkzeug zum Nachjustieren.

Nicht Teil des Bots. Aufruf:  python3 tools_aktien_sim.py

Die Aktie soll STRENG sein: man kann schnell viel gewinnen UND schnell viel
verlieren. Beides laesst sich nicht am Einzeltakt ablesen, sondern nur ueber
viele Tage mit echtem Handel. Deshalb simuliert dieses Skript 60 Tage Serverbetrieb
mit verschiedenen Spielertypen und prueft am Ende harte Kriterien.

Gemessen wird gegen diese Fragen:
  1. Laeuft der Kurs langfristig weg (Hyperinflation) oder stirbt er am Boden?
  2. Kann ein aufmerksamer Spieler an einem guten Abend richtig Geld machen?
  3. Verliert jemand, der zum falschen Zeitpunkt kauft und haelt, richtig Geld?
  4. Ist Kaufen-und-liegen-lassen ein sicherer Gewinn? (Das darf es NICHT sein.)
  5. Schwankt es ueberhaupt genug, um spannend zu sein?
"""

import os
import random
import statistics

os.environ.setdefault("TZ", "Europe/Berlin")

import floaktie


MIN = 60            # Takte pro Stunde (die Konstanten sind pro Minute gedacht)


class Sim:
    """Ein Simulationslauf: setzt die Aktie auf einen frischen Stand und
    spielt einen Tagesablauf nach Stundenplan ab."""

    def __init__(self, seed=1, tage=60):
        self.seed = seed
        self.tage = tage
        self.fa = floaktie.instance
        self.verlauf = []          # (tag, stunde, kurs, aktivitaet)

    class _Store:
        def __init__(self, data):
            self.data = data

        async def save(self):
            pass

    def _frisch(self):
        fa = self.fa
        fa._enabled = True
        fa._store = self._Store({
            "price": floaktie.START_PRICE, "base": float(floaktie.START_PRICE),
            "day": "", "act_ema": 0.0, "mom": 0.0, "leer_min": 0.0,
            "msg_count": 0, "last_msg_count": 0, "holdings": {},
            "history": [], "ticks": [], "open_day": "", "open_base": 0,
        })
        fa._sync_price()

    def _tagesplan(self, tag, rng):
        """Ein realistischer Tag als Liste von (Stunden, Leute, Streams, Msgs/min).

        Wochentags ist abends was los, am Wochenende mehr und laenger, und es
        gibt tote Tage - genau die sollen im Kurs auch wehtun."""
        wochenende = (tag % 7) in (5, 6)
        tot = rng.random() < 0.12          # jeder achte Tag: niemand da
        if tot:
            return [(24, 0, 0, 0)]
        if wochenende:
            leute = rng.randint(5, 11)
            streams = rng.randint(1, 4)
            dauer = rng.randint(5, 8)
        else:
            leute = rng.randint(2, 7)
            streams = rng.randint(0, 2)
            dauer = rng.randint(2, 5)
        leer_vor = rng.randint(12, 17)
        leer_nach = max(0, 24 - leer_vor - dauer)
        return [(leer_vor, 0, 0, 0),
                (dauer, leute, streams, rng.randint(1, 6)),
                (leer_nach, 0, 0, 0)]

    def lauf(self):
        """Spielt alle Tage ab und liefert den Kursverlauf je Stunde."""
        rng = random.Random(self.seed)
        self._frisch()
        fa = self.fa
        self.verlauf = []
        for tag in range(self.tage):
            fa._today = lambda t=tag: f"2026-01-{(t % 28) + 1:02d}"
            fa._tages_anker()
            stunde = 0
            for dauer, leute, streams, msgs in self._tagesplan(tag, rng):
                for _ in range(int(dauer)):
                    for _ in range(MIN):
                        fa._activity_tick(leute, msgs, streams, 0)
                    akt = float(fa._state().get("act_ema", 0.0) or 0.0)
                    self.verlauf.append((tag, stunde, fa.price(), akt))
                    stunde += 1
        return self.verlauf

    # --- Auswertung -------------------------------------------------------
    def kurse(self):
        return [k for _t, _s, k, _a in self.verlauf]

    def bester_handel(self, haltedauer_h):
        """Bestmoegliche Rendite, wenn man genau 'haltedauer_h' Stunden haelt.
        Das ist die Obergrenze fuer 'schnell viel gewinnen'.

        AUSDRUECKLICH ohne die Anlaufphase: dort startet der Kurs bei 10 und ein
        Kauf am ersten Tag sah nach +39.710 % aus - das misst den Anlauf, nicht
        den Handel."""
        k = self.kurse_eingeschwungen()
        beste = 0.0
        for i in range(len(k) - haltedauer_h):
            if k[i] <= 0:
                continue
            beste = max(beste, k[i + haltedauer_h] / k[i] - 1.0)
        return beste

    def schlimmster_handel(self, haltedauer_h):
        """Schlimmstmoegliches Ergebnis bei derselben Haltedauer - die Obergrenze
        fuer 'schnell viel verlieren'."""
        k = self.kurse_eingeschwungen()
        schlimm = 0.0
        for i in range(len(k) - haltedauer_h):
            if k[i] <= 0:
                continue
            schlimm = min(schlimm, k[i + haltedauer_h] / k[i] - 1.0)
        return schlimm

    def kurse_eingeschwungen(self, ab_tag=14):
        """Nur der eingeschwungene Teil: die ersten Tage startet der Kurs bei 10
        und der Grundwert baut sich erst auf. Wer das mitmisst, misst den
        Anlauf, nicht das Verhalten."""
        return [k for t, _s, k, _a in self.verlauf if t >= ab_tag]

    def blind_halten(self, dauer_h=7 * 24):
        """Rendite fuer jemanden, der irgendwann kauft und eine FESTE Zeit haelt.

        Gegen einen einzelnen Endpunkt zu messen war unbrauchbar: endete der Lauf
        zufaellig an einem vollen Abend, sah blindes Halten grossartig aus."""
        k = self.kurse_eingeschwungen()
        return [k[i + dauer_h] / k[i] - 1.0
                for i in range(len(k) - dauer_h) if k[i] > 0]

    def trefferquote(self, dauer_h=6):
        """Anteil der Zeitpunkte, an denen ein Kauf nach dauer_h im Plus waere.
        Ein fairer Markt liegt hier grob bei der Haelfte, nicht bei 0 oder 1."""
        k = self.kurse_eingeschwungen()
        werte = [k[i + dauer_h] > k[i] for i in range(len(k) - dauer_h) if k[i] > 0]
        return sum(werte) / len(werte) if werte else 0.0

    def nacht_verlust(self):
        """Was kostet es, ueber Nacht zu halten (Kurs-Hoch des Tages -> naechster
        Morgen)? Das ist die typische 'zu spaet verkauft'-Strafe."""
        je_tag = {}
        for tag, stunde, kurs, _a in self.verlauf:
            je_tag.setdefault(tag, []).append((stunde, kurs))
        verluste = []
        tage = sorted(je_tag)
        for a, b in zip(tage, tage[1:]):
            hoch = max(k for _s, k in je_tag[a])
            morgen = min(k for s, k in je_tag[b] if s <= 10) if je_tag[b] else hoch
            if hoch > 0:
                verluste.append(morgen / hoch - 1.0)
        return verluste


def bewerte(tage=60, laeufe=8):
    """Fuehrt mehrere Laeufe aus und prueft die Kriterien fuer 'streng'."""
    zeilen = []
    hochs, tiefs, spannen = [], [], []
    beste6, schlimm6, blind_med, naechte, quoten = [], [], [], [], []
    for seed in range(1, laeufe + 1):
        sim = Sim(seed=seed, tage=tage)
        sim.lauf()
        k = sim.kurse_eingeschwungen()
        hochs.append(max(k))
        tiefs.append(min(k))
        spannen.append(max(k) / max(1, min(k)))
        beste6.append(sim.bester_handel(6))
        schlimm6.append(sim.schlimmster_handel(6))
        blind_med.append(statistics.median(sim.blind_halten()))
        naechte.append(statistics.median(sim.nacht_verlust()))
        quoten.append(sim.trefferquote(6))
        zeilen.append((seed, min(k), max(k), spannen[-1]))

    print(f"  {'Lauf':>4} {'Tief':>12} {'Hoch':>12} {'Spanne':>10}")
    for s, lo, hi, sp in zeilen:
        print(f"  {s:>4} {lo:>12,} {hi:>12,} {sp:>9.1f}x".replace(",", "."))

    med = statistics.median
    erg = {
        "Tief (Median)": med(tiefs),
        "Hoch (Median)": med(hochs),
        "Hoch (schlimmster Lauf)": max(hochs),
        "Spanne Hoch/Tief": med(spannen),
        "bester 6h-Handel": med(beste6),
        "schlimmster 6h-Handel": med(schlimm6),
        "blind 7 Tage halten (Median)": med(blind_med),
        "ueber Nacht halten (Median)": med(naechte),
        "Trefferquote 6h-Kauf": med(quoten),
    }
    print()
    for k, v in erg.items():
        if "Handel" in k or "halten" in k:
            print(f"  {k:32s} {v * 100:+9.1f} %")
        elif "Spanne" in k:
            print(f"  {k:32s} {v:9.1f} x")
        elif "Trefferquote" in k:
            print(f"  {k:32s} {v * 100:9.0f} %")
        else:
            print(f"  {k:32s} {v:>9,.0f}".replace(",", "."))

    # --- Kriterien fuer "streng, aber ein Markt" -------------------------
    pruefungen = [
        ("kein Weglaufen (Hoch < 1 Mio)", max(hochs) < 1_000_000),
        ("nicht tot (Tief > 100)", med(tiefs) > 100),
        ("schnell viel GEWINNEN (bester 6h-Handel >= +100 %)", med(beste6) >= 1.00),
        ("schnell viel VERLIEREN (schlimmster 6h-Handel <= -40 %)", med(schlimm6) <= -0.40),
        ("blind halten bringt nichts geschenkt (|Median| <= 40 %)",
         abs(med(blind_med)) <= 0.40),
        ("ueber Nacht halten tut weh (Median <= -20 %)", med(naechte) <= -0.20),
        ("spielbare Spanne (5x .. 60x)", 5.0 <= med(spannen) <= 60.0),
        ("Timing entscheidet (Trefferquote 20-60 %)",
         0.20 <= med(quoten) <= 0.60),
    ]
    print()
    alle_ok = True
    for name, ok in pruefungen:
        print(f"  {'BESTANDEN' if ok else 'DURCHGEFALLEN'}  {name}")
        alle_ok = alle_ok and ok
    return alle_ok, erg


if __name__ == "__main__":
    floaktie.TICK_NOISE = 0.0
    print(f"Konstanten: IDLE_PER_30MIN={floaktie.IDLE_PER_30MIN} "
          f"TICK_GAIN={floaktie.TICK_GAIN} TICK_CAP={floaktie.TICK_CAP} "
          f"AKT_WERT={floaktie.AKT_WERT} CEIL_FACTOR={floaktie.CEIL_FACTOR}")
    print(f"\n60 Tage Serverbetrieb, 8 Laeufe:\n")
    ok, _ = bewerte()
    print("\n" + ("ALLE KRITERIEN ERFUELLT" if ok else "NACHJUSTIEREN NOETIG"))
