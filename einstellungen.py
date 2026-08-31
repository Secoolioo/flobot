"""Eine Liste fuer alles, was man an Flo einstellen kann.

WOZU
====
Der Betreiber hat es so gesagt: "manche einstellungen sind da und andere da".
Er hat recht, und der Grund ist historisch gewachsen - Flo hat zwei
Konfigurationssysteme, die beide ihre Berechtigung haben:

    features.py    24 Schalter: GIBT es diese Funktion auf diesem Server?
                   (global aus / hier aus / gar nicht geladen)
    guildcfg.py    26 Werte:    WIE verhaelt sie sich? Kanal, Lautstaerke,
                   Warn-Limit, Einsatzdeckel ...

Das sind wirklich zwei verschiedene Fragen, und sie zu einem Speicher
zusammenzuruehren waere ein Rueckschritt: 'Casino aus' und 'Casino-Deckel
10.000' meinen nichts Gemeinsames, und ein gemeinsamer Speicher haette 50
Schluessel, von denen die Haelfte nur manchmal gilt.

Was falsch war, ist nicht die Trennung - es ist die DARSTELLUNG. Wer den
Einsatzdeckel sucht, sucht ihn beim Casino und nicht unter 'Spiel'; wer das
Warn-Limit sucht, sucht es bei der Moderation. Im Panel standen sie in zwei
voellig getrennten Listen, und bei 'Bayrisch-Modus' sogar zweimal mit
demselben Wortlaut untereinander.

Dieses Modul aendert deshalb keinen Speicher. Es JOINT die beiden vorhandenen
zu EINER Liste, geordnet nach dem, wonach ein Mensch sucht: nach der Funktion.

    🎵 Musik            [an]
         Lautstaerke                50
         Max. Warteschlange         50
    🎰 Casino           [an]
         Hoechster Einsatz      10.000
    🛡 Moderation        [an]
         Mod-Log-Kanal          #modlog
         Warnungen bis Timeout       3
         ...

WAS SICH NICHT AENDERT
======================
Im Discord bleibt alles, wie es war. 'Flo einstellungen' und 'Flo funktionen'
liefern weiter genau dieselben Embeds - dieses Modul wird dort nicht benutzt.
Es ist die Grundlage fuer die Oberflaeche im Web-Panel, wo der Betreiber die
Unordnung bemaengelt hat.
"""

import logging

import features
import guildcfg

log = logging.getLogger("dcbot.einstellungen")


#: Welcher Funktionsschalter zu welchen Einstellwerten gehoert.
#:
#: Von Hand gepflegt und bewusst nicht geraten: die Zuordnung steht nirgends im
#: Code, sie ergibt sich aus dem, was ein Wert tatsaechlich beeinflusst. Ein
#: Test haelt sie gegen die beiden Kataloge, damit hier nichts verwaist, wenn
#: jemand einen Schluessel hinzufuegt oder umbenennt.
ZUORDNUNG = {
    "music": ("lautstaerke", "musik_max_queue"),
    "casino": ("casino_max_einsatz",),
    "mod": ("modlog_channel", "warn_limit", "warn_timeout", "timeout_standard",
            "purge_max"),
    "economy": ("levelup_channel", "levelup_ansagen", "daily_erinnerung"),
    "games": ("zaehl_channel", "event_channel"),
    "food": ("kalorien_channel",),
    "arbeit": ("wordle_channel", "wordle_min_voice"),
    "schulden": ("schulden_pranger",),
    "floaktie": ("aktie_zaehlt",),
    "voice": ("soundboard", "join_sounds"),
    "bayern": ("bayern",),
}

#: Die Einstellwerte, zu denen es keinen Funktionsschalter gibt - sie gelten
#: fuer Flo als Ganzes. Stehen in der Oberflaeche oben, vor den Funktionen.
GRUNDLEGEND = ("praefix", "ansage_channel", "autodelete_channels",
               "autodelete_sekunden", "icon_auto")

#: Ueberschrift und Erklaerung fuer den grundlegenden Block.
GRUNDTITEL = "Grundlegendes"
GRUNDTEXT = "Gilt fuer Flo auf diesem Server, unabhaengig von einzelnen Funktionen."

#: Wo Schalter und Wert denselben Namen tragen, braucht die Zeile im Abschnitt
#: einen eigenen Wortlaut - sonst steht 'Bayrisch-Modus' unter der Ueberschrift
#: 'Bayrisch-Modus', und niemand sieht, dass das Erste die Funktion und das
#: Zweite ihr Zustand ist.
#:
#: Bewusst NUR hier und nicht in guildcfg.KATALOG geaendert: der Wortlaut dort
#: steht so im Discord ('Flo einstellungen'), und der soll sich nicht bewegen.
ZEILENTITEL = {
    "bayern": "Boarisch reden",
}


class Abschnitt:
    """Ein Block der Liste: eine Funktion mit ihrem Schalter und ihren Werten.

    Bei den grundlegenden Sachen gibt es keinen Schalter - dann ist `schalter`
    None und der Abschnitt zeigt nur seine Werte.
    """

    def __init__(self, key, titel, emoji, erklaerung, schalter, werte):
        self.key = key
        self.titel = titel
        self.emoji = emoji
        self.erklaerung = erklaerung
        self.schalter = schalter        # None oder {"zustand": ..., "key": ...}
        self.werte = werte              # Liste von Wert-dicts

    def als_dict(self):
        return {"key": self.key, "titel": self.titel, "emoji": self.emoji,
                "erklaerung": self.erklaerung, "schalter": self.schalter,
                "werte": self.werte}


class Baum:
    """Baut die eine Liste aus den beiden Katalogen.

    Kein eigener Speicher, kein Zwischenspeichern: gelesen wird bei jedem Aufruf
    frisch aus guildcfg und features. Beides ist billig (features ist O(1),
    guildcfg liest ein dict), und ein Zwischenspeicher waere hier genau die Art
    Fehlerquelle, die man nicht braucht - wer im Panel etwas umstellt, will es
    sofort sehen.
    """

    def __init__(self):
        self._nach_key = {e.key: e for e in guildcfg.KATALOG}

    # -- Bausteine -----------------------------------------------------------
    def _wert(self, key, gid):
        eintrag = self._nach_key.get(key)
        if eintrag is None:
            return None
        return {
            "key": eintrag.key,
            "label": ZEILENTITEL.get(key, eintrag.label),
            "typ": eintrag.typ,
            "hinweis": eintrag.hinweis,
            "wert": guildcfg.get(gid, key),
            "standard": guildcfg.instance.standard(key, gid),
            "minimum": eintrag.minimum,
            "maximum": eintrag.maximum,
            "nur_haupt": eintrag.nur_haupt,
            "eigen": guildcfg.eigen(gid, key),
        }

    @staticmethod
    def _geladen():
        """Welche Module beim Start hochgekommen sind ({key: bool} aus bot.py).

        Lazy und mit Rueckfall auf ein leeres dict: dieses Modul soll sich auch
        importieren lassen, wenn gar kein Bot laeuft (Werkzeuge, Tests)."""
        try:
            import bot
            return dict(getattr(bot, "FEATURE_LOADED", {}) or {})
        except Exception:  # noqa: BLE001
            return {}

    # -- Die Liste -----------------------------------------------------------
    def bauen(self, gid, geladen=None):
        """-> Liste von Abschnitt, in Anzeige-Reihenfolge."""
        zustand = {f["key"]: f for f in
                   features.state(self._geladen() if geladen is None else geladen,
                                  gid)}
        raus = [Abschnitt("_grund", GRUNDTITEL, "⚙️", GRUNDTEXT, None,
                          [w for w in (self._wert(k, gid) for k in GRUNDLEGEND)
                           if w])]
        for eintrag in features.CATALOG:
            key = eintrag["key"]
            werte = [w for w in (self._wert(k, gid)
                                 for k in ZUORDNUNG.get(key, ())) if w]
            f = zustand.get(key, {})
            raus.append(Abschnitt(
                key, eintrag["label"], eintrag.get("emoji", ""),
                eintrag.get("desc", ""),
                {"key": key, "an": bool(f.get("on")),
                 "geladen": bool(f.get("loaded")),
                 "global_an": bool(f.get("global_on"))},
                werte))
        return raus

    def als_dicts(self, gid, geladen=None):
        return [a.als_dict() for a in self.bauen(gid, geladen)]

    # -- Vollstaendigkeit ----------------------------------------------------
    def vergessene_werte(self):
        """Einstellwerte, die in keinem Abschnitt vorkommen.

        Der eigentliche Zweck dieser Methode ist der Test: wenn jemand einen
        neuen Wert in guildcfg.KATALOG legt und ihn hier nicht einordnet, waere
        er im Panel unsichtbar - genau die Sorte stiller Verlust, die dieser
        ganze Umbau vermeiden soll.
        """
        zugeordnet = set(GRUNDLEGEND)
        for keys in ZUORDNUNG.values():
            zugeordnet |= set(keys)
        return sorted(set(self._nach_key) - zugeordnet)

    def erfundene_werte(self):
        """Namen in der Zuordnung, die es in guildcfg gar nicht gibt."""
        genannt = set(GRUNDLEGEND)
        for keys in ZUORDNUNG.values():
            genannt |= set(keys)
        return sorted(genannt - set(self._nach_key))

    def erfundene_funktionen(self):
        """Funktionsnamen in der Zuordnung, die es in features nicht gibt."""
        echte = {f["key"] for f in features.CATALOG}
        return sorted(set(ZUORDNUNG) - echte)


# --- Singleton + Modul-API ---------------------------------------------------
instance = Baum()

bauen = instance.bauen
als_dicts = instance.als_dicts
vergessene_werte = instance.vergessene_werte
erfundene_werte = instance.erfundene_werte
erfundene_funktionen = instance.erfundene_funktionen
