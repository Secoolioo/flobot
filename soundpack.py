"""Eingebautes Soundpack: synthetisiert klassische Soundboard-Effekte als
echte WAV-Dateien (kein TTS!) - Airhorn, Vine-Boom, Badum-Tss, traurige
Trompete, Applaus, Buzzer, Fanfare, Pups, Pew-Pew, Gong, Ding.

Nur Standardbibliothek (math/wave/array/random). voicegags.setup() ruft
ensure_pack() einmal auf: fehlende Dateien werden erzeugt, vorhandene
(auch eigene mp3s mit gleichem Namen) NIE ueberschrieben. Abschaltbar per
SOUND_PACK=0.
"""
from __future__ import annotations

import math
import random
import wave
from array import array
from pathlib import Path

RATE = 32000            # Mono, 16 Bit - reicht fuer Discord-Voice locker


class Soundpack:
    def __init__(self) -> None:
        self.PACK = {
            "airhorn": self._airhorn, "boom": self._boom,
            "badumtss": self._badumtss, "trompete": self._trompete,
            "applaus": self._applaus, "buzzer": self._buzzer,
            "ding": self._ding, "tada": self._tada, "pups": self._pups,
            "pew": self._pew, "gong": self._gong,
        }

    # --- Synthese-Helfer ---------------------------------------------------
    def _silence(self, dauer: float) -> list[float]:
        return [0.0] * int(RATE * dauer)

    def _env(self, n: int, attack: float = 0.005, release: float = 0.05
             ) -> list[float]:
        """Lineare Attack/Release-Huellkurve ueber n Samples."""
        a, r = max(1, int(RATE * attack)), max(1, int(RATE * release))
        out = [1.0] * n
        for i in range(min(a, n)):
            out[i] = i / a
        for i in range(min(r, n)):
            out[n - 1 - i] = min(out[n - 1 - i], i / r)
        return out

    def _exp_decay(self, n: int, tau: float) -> list[float]:
        k = 1.0 / (RATE * tau)
        return [math.exp(-i * k) for i in range(n)]

    def _saw(self, phase: float) -> float:
        return 2.0 * (phase - math.floor(phase + 0.5))

    def _square(self, phase: float) -> float:
        return 1.0 if (phase % 1.0) < 0.5 else -1.0

    def _smooth(self, samples: list[float], breite: int = 3) -> list[float]:
        """Billiger Tiefpass: gleitender Mittelwert (macht Saegezahn 'blechig-weich')."""
        out = samples[:]
        acc = sum(samples[:breite])
        for i in range(breite, len(samples)):
            out[i] = acc / breite
            acc += samples[i] - samples[i - breite]
        return out

    def _ton(self, dauer: float, freq, *, wellen=("saw",), vibrato: float = 0.0,
             vib_hz: float = 6.0, attack: float = 0.005, release: float = 0.05
             ) -> list[float]:
        """Ein Ton. ``freq``: Zahl ODER Funktion t->Hz (Sweeps). ``wellen``:
        Mischung aus 'sine'/'saw'/'square'."""
        n = int(RATE * dauer)
        env = self._env(n, attack, release)
        out = []
        phase = 0.0
        for i in range(n):
            t = i / RATE
            f = freq(t) if callable(freq) else freq
            if vibrato:
                f *= 1.0 + vibrato * math.sin(2 * math.pi * vib_hz * t)
            phase += f / RATE
            s = 0.0
            for w in wellen:
                if w == "sine":
                    s += math.sin(2 * math.pi * phase)
                elif w == "saw":
                    s += self._saw(phase)
                else:
                    s += self._square(phase)
            out.append(s / len(wellen) * env[i])
        return out

    def _mix_at(self, ziel: list[float], teil: list[float], start_s: float,
                gain: float = 1.0) -> None:
        o = int(RATE * start_s)
        fehlt = o + len(teil) - len(ziel)
        if fehlt > 0:
            ziel.extend([0.0] * fehlt)
        for i, s in enumerate(teil):
            ziel[o + i] += s * gain

    def _write(self, path: Path, samples: list[float]) -> None:
        """Normalisiert auf -1.4 dB und schreibt 16-Bit-Mono-WAV."""
        peak = max(1e-6, max(abs(s) for s in samples))
        g = 0.85 / peak
        data = array("h", (int(max(-1.0, min(1.0, s * g)) * 32767) for s in samples))
        with wave.open(str(path), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(RATE)
            fh.writeframes(data.tobytes())

    # --- Die Sounds --------------------------------------------------------
    def _airhorn(self) -> list[float]:
        """MLG-Airhorn: dreckiger Saegezahn mit Vibrato, kurz-kurz-laaang."""
        def blast(dauer: float) -> list[float]:
            raw = self._ton(dauer, 466.0, wellen=("saw", "saw", "square"),
                            vibrato=0.035, vib_hz=7.0, release=0.04)
            det = self._ton(dauer, 466.0 * 1.012, wellen=("saw",), vibrato=0.03,
                            vib_hz=6.3, release=0.04)
            return [max(-0.9, min(0.9, (a + 0.6 * b) * 1.6))   # leichte Verzerrung
                    for a, b in zip(raw, det)]
        out: list[float] = []
        for dauer, pause in ((0.18, 0.07), (0.18, 0.07), (1.0, 0.0)):
            out.extend(blast(dauer))
            out.extend(self._silence(pause))
        return out

    def _boom(self) -> list[float]:
        """Vine-Boom: tiefer Sinus-Sweep mit Punch und langem Ausklang."""
        n = int(RATE * 1.5)
        dec = self._exp_decay(n, 0.4)
        out = []
        phase = 0.0
        for i in range(n):
            t = i / RATE
            f = 34.0 + 46.0 * math.exp(-t * 9.0)         # 80 Hz -> 34 Hz
            phase += f / RATE
            s = math.sin(2 * math.pi * phase) + 0.4 * math.sin(4 * math.pi * phase)
            out.append(max(-0.95, min(0.95, s * 1.5)) * dec[i])
        for i in range(int(RATE * 0.006)):               # Attack-Klick
            out[i] += random.uniform(-0.5, 0.5) * (1 - i / (RATE * 0.006))
        return out

    def _badumtss(self) -> list[float]:
        out: list[float] = []

        def tom(f0: float, f1: float, dauer: float) -> list[float]:
            return [s * d for s, d in zip(
                self._ton(dauer, lambda t, a=f0, b=f1: a + (b - a) * (t * 4),
                          wellen=("sine",)),
                self._exp_decay(int(RATE * dauer), 0.09))]

        self._mix_at(out, tom(190, 120, 0.22), 0.0)
        self._mix_at(out, tom(150, 95, 0.22), 0.24)
        # Becken: helles Rauschen (Differenz-Filter) mit langem Ausklang
        n = int(RATE * 1.3)
        noise = [random.uniform(-1, 1) for _ in range(n)]
        hell = [noise[i] - noise[i - 1] for i in range(1, n)]
        dec = self._exp_decay(n - 1, 0.35)
        self._mix_at(out, [h * d for h, d in zip(hell, dec)], 0.48, gain=0.8)
        return out

    def _trompete(self) -> list[float]:
        """Traurige Trompete: waaah waaah waaah waaaaaah (faellt am Ende ab)."""
        out: list[float] = []
        t0 = 0.0
        for f, dauer in ((233.0, 0.38), (220.0, 0.38), (208.0, 0.38)):
            ton = self._ton(dauer, f, wellen=("saw", "square"), vibrato=0.02,
                            vib_hz=5.5, attack=0.03, release=0.08)
            self._mix_at(out, self._smooth(ton, 4), t0)
            t0 += dauer + 0.06
        # letzter Ton: lang, tiefes Vibrato, sackt am Schluss ab
        dauer = 1.3
        ton = self._ton(dauer, lambda t: 196.0 - (26.0 * max(0.0, t - 0.7) / 0.6),
                        wellen=("saw", "square"), vibrato=0.05, vib_hz=5.0,
                        attack=0.03, release=0.25)
        self._mix_at(out, self._smooth(ton, 4), t0)
        return out

    def _applaus(self) -> list[float]:
        """~2 s Applaus: viele kleine Klatscher (helle Rauschimpulse)."""
        out = self._silence(2.2)
        t = 0.05
        while t < 2.0:
            dichte = min(1.0, t / 0.35) * (1.0 if t < 1.5 else (2.0 - t) * 2)
            if random.random() < max(0.15, dichte):
                n = int(RATE * random.uniform(0.006, 0.012))
                klatsch = [random.uniform(-1, 1) for _ in range(n)]
                klatsch = [klatsch[i] - 0.6 * klatsch[i - 1] for i in range(1, n)]
                dec = self._exp_decay(n - 1, 0.004)
                self._mix_at(out, [k * d for k, d in zip(klatsch, dec)], t,
                             gain=random.uniform(0.4, 1.0))
            t += random.uniform(0.008, 0.03)
        return out

    def _buzzer(self) -> list[float]:
        """Falsche-Antwort-Buzzer: fies, rau, unmissverstaendlich."""
        a = self._ton(0.85, 112.0, wellen=("square", "saw"), attack=0.004,
                      release=0.1)
        b = self._ton(0.85, 89.0, wellen=("square",), attack=0.004, release=0.1)
        return [(x + 0.7 * y) * (0.8 + 0.2 * math.sin(2 * math.pi * 16 * i / RATE))
                for i, (x, y) in enumerate(zip(a, b))]

    def _ding(self) -> list[float]:
        """Heller Glocken-Ding (Service-Bell)."""
        n = int(RATE * 1.3)
        out = [0.0] * n
        for f, amp, tau in ((880.0, 1.0, 0.45), (1774.0, 0.45, 0.22),
                            (2660.0, 0.22, 0.12)):
            dec = self._exp_decay(n, tau)
            phase = 0.0
            for i in range(n):
                phase += f / RATE
                out[i] += amp * math.sin(2 * math.pi * phase) * dec[i]
        for i in range(min(64, n)):
            out[i] *= i / 64
        return out

    def _tada(self) -> list[float]:
        """Fanfare: C-E-G hoch, dann Akkord mit Glitzer."""
        out: list[float] = []
        for i, f in enumerate((523.25, 659.26, 783.99)):
            ton = self._ton(0.14, f, wellen=("saw", "sine"), attack=0.01,
                            release=0.04)
            self._mix_at(out, self._smooth(ton, 3), i * 0.10, gain=0.8)
        akkord: list[float] = []
        for f in (523.25, 659.26, 783.99, 1046.5):
            ton = self._ton(1.3, f, wellen=("saw", "sine"), vibrato=0.012,
                            vib_hz=5.5, attack=0.02, release=0.5)
            if not akkord:
                akkord = [0.0] * len(ton)
            for j, s in enumerate(ton):
                akkord[j] += s * 0.4
        self._mix_at(out, self._smooth(akkord, 3), 0.30)
        return out

    def _pups(self) -> list[float]:
        """Der Klassiker. Wobbelnder Tiefton + Rauschen = Comedy-Gold."""
        n = int(RATE * 1.0)
        out = []
        phase = 0.0
        wobble = 0.0
        for i in range(n):
            t = i / RATE
            wobble += random.uniform(-1, 1) * 4.0
            wobble *= 0.985
            f = 78.0 + wobble + 18.0 * math.sin(2 * math.pi * 11 * t)
            phase += max(30.0, f) / RATE
            s = self._saw(phase) * 0.8 + random.uniform(-1, 1) * 0.18
            env = min(1.0, t / 0.02) * (1.0 if t < 0.75 else max(0.0, (1.0 - t) / 0.25))
            out.append(s * env)
        return self._smooth(out, 6)

    def _pew(self) -> list[float]:
        """Zwei schnelle Laser-Pews."""
        out: list[float] = []

        def pew() -> list[float]:
            return [s * d for s, d in zip(
                self._ton(0.22, lambda t: 1400.0 * math.exp(-t * 10.0) + 160.0,
                          wellen=("square", "sine"), attack=0.002),
                self._exp_decay(int(RATE * 0.22), 0.1))]

        self._mix_at(out, pew(), 0.0)
        self._mix_at(out, pew(), 0.3)
        return out

    def _gong(self) -> list[float]:
        """Tiefer Tempel-Gong mit unharmonischen Teiltoenen und Schwebung."""
        n = int(RATE * 2.8)
        out = [0.0] * n
        for f, amp, tau in ((196.0, 1.0, 1.2), (196.0 * 1.504, 0.6, 0.9),
                            (196.0 * 2.44, 0.35, 0.6), (196.0 * 3.58, 0.2, 0.35),
                            (197.5, 0.5, 1.1)):
            dec = self._exp_decay(n, tau)
            phase = 0.0
            for i in range(n):
                phase += f / RATE
                out[i] += amp * math.sin(2 * math.pi * phase) * dec[i]
        for i in range(int(RATE * 0.01)):
            out[i] += random.uniform(-0.4, 0.4) * (1 - i / (RATE * 0.01))
        return out

    def ensure_pack(self, sounds_dir: Path) -> int:
        """Erzeugt fehlende Pack-Sounds als WAV. Vorhandene Dateien (egal welche
        Endung) werden NIE angefasst. Rueckgabe: Anzahl neu erzeugter Dateien."""
        sounds_dir.mkdir(parents=True, exist_ok=True)
        vorhanden = {p.stem.lower() for p in sounds_dir.iterdir() if p.is_file()}
        neu = 0
        for name, bauen in self.PACK.items():
            if name in vorhanden:
                continue
            random.seed(name)            # reproduzierbar, unabhaengig vom Start
            self._write(sounds_dir / f"{name}.wav", bauen())
            neu += 1
        return neu


instance = Soundpack()

# --- Modul-Aliase (voicegags nutzt ensure_pack) ----------------------------
_silence = instance._silence
_env = instance._env
_exp_decay = instance._exp_decay
_saw = instance._saw
_square = instance._square
_smooth = instance._smooth
_ton = instance._ton
_mix_at = instance._mix_at
_write = instance._write
_airhorn = instance._airhorn
_boom = instance._boom
_badumtss = instance._badumtss
_trompete = instance._trompete
_applaus = instance._applaus
_buzzer = instance._buzzer
_ding = instance._ding
_tada = instance._tada
_pups = instance._pups
_pew = instance._pew
_gong = instance._gong
PACK = instance.PACK
ensure_pack = instance.ensure_pack
