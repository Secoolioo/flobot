"""Reine Zeit-/Jahreszeit-Logik: ordnet einem Zeitpunkt das richtige Icon zu.

Bewusst ohne Discord-Abhaengigkeit, damit es isoliert getestet werden kann.

Tagesablauf:  Nacht -> Morgen -> Tag -> Abend -> Nacht
Tagsueber wird zusaetzlich nach Jahreszeit unterschieden.
"""
from __future__ import annotations

from datetime import datetime, time

# --- Tageszeit-Grenzen (lokale Zeit) -------------------------------------
# Aendere hier die Uhrzeiten, falls dir andere Uebergaenge lieber sind.
MORGEN_START = time(6, 0)    # ab 06:00 = Morgen
TAG_START = time(10, 0)      # ab 10:00 = Tag
ABEND_START = time(18, 0)    # ab 18:00 = Abend
NACHT_START = time(22, 0)    # ab 22:00 = Nacht (bis 06:00)

# --- Bilddateien ---------------------------------------------------------
# Jahreszeit-unabhaengig:
NACHT_IMAGE = "NachtBild.png"
MORGEN_IMAGE = "MorgenBild.png"
ABEND_IMAGE = "AbendsBild.png"

# Tagsueber, je nach Jahreszeit:
SEASON_IMAGES = {
    "winter": "Winter.png",
    "fruehling": "Frühling.png",
    "sommer": "Sommer.png",
    "herbst": "Herbst.png",
}


class ScheduleLogic:
    """Objektorientierte Kapselung der Zeit-/Jahreszeit-Logik."""

    def get_period(self, t: time) -> str:
        """Liefert 'nacht' | 'morgen' | 'tag' | 'abend' fuer eine Uhrzeit."""
        if MORGEN_START <= t < TAG_START:
            return "morgen"
        if TAG_START <= t < ABEND_START:
            return "tag"
        if ABEND_START <= t < NACHT_START:
            return "abend"
        return "nacht"  # 22:00-06:00, laeuft ueber Mitternacht

    def get_season(self, month: int) -> str:
        """Meteorologische Jahreszeit fuer einen Monat (Nordhalbkugel)."""
        if month in (12, 1, 2):
            return "winter"
        if month in (3, 4, 5):
            return "fruehling"
        if month in (6, 7, 8):
            return "sommer"
        return "herbst"  # 9, 10, 11

    def get_image_filename(self, now: datetime) -> str:
        """Bestimmt den Dateinamen des passenden Icons fuer 'now'."""
        period = self.get_period(now.time())
        if period == "tag":
            return SEASON_IMAGES[self.get_season(now.month)]
        return {
            "nacht": NACHT_IMAGE,
            "morgen": MORGEN_IMAGE,
            "abend": ABEND_IMAGE,
        }[period]

    def all_image_filenames(self) -> list[str]:
        """Alle Bilddateien, die der Bot ueber das Jahr braucht (fuer Checks)."""
        return [NACHT_IMAGE, MORGEN_IMAGE, ABEND_IMAGE, *SEASON_IMAGES.values()]


# --- Modul-Schnittstelle (rueckwaertskompatibel) -------------------------
# bot.py und test_logic.py nutzen weiterhin die alten Modulnamen.
instance = ScheduleLogic()
get_period = instance.get_period
get_season = instance.get_season
get_image_filename = instance.get_image_filename
all_image_filenames = instance.all_image_filenames
