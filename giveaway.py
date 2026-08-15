"""Giveaways: ein Nutzer verlost seine eigenen Flo Coins - Flo fragt alles ab.

Ablauf: 'Flo giveaway' -> Flo stellt EINE Frage nach der anderen (Einsatz, Grund,
Dauer), zeigt eine Zusammenfassung zum Bestaetigen und postet dann das Giveaway
mit Mitmach-Knopf. Nach Ablauf zieht Flo einen Gewinner und schreibt ihm die Coins
gut.

Geld-Weg (wichtig, damit nichts erschlichen werden kann):
- Der Einsatz wird beim START SOFORT vom Veranstalter abgebucht und liegt bis zur
  Ziehung auf dem Giveaway ("Escrow"). Er kann ihn also nicht mehr ausgeben.
- Die Abbuchung wird GEPRUEFT (economy deckelt bei 0): kam nicht der ganze Betrag
  zusammen, wird der Teilbetrag sofort zurueckgebucht und das Giveaway platzt.
- Gewinner bekommt exakt den hinterlegten Betrag. Ohne Teilnehmer (oder bei
  Abbruch) geht alles zurueck an den Veranstalter. Coins entstehen nie neu.

Antworten dürfen so klingen, wie man redet: '5k', '10.000', 'zwei tausend',
'alles', 'die hälfte' fuer den Einsatz; '10min', 'eine halbe stunde', '1h30',
'2 tage', 'kurz' fuer die Dauer; 'ja/jo/passt/los' bzw. 'nein/abbrechen' zum
Bestaetigen. Alles, was dasselbe bedeutet, wirkt auch gleich.

Befehle (nach 'Flo'):
- giveaway                      Assistent starten (fragt alles nacheinander)
- giveaway 5k 2h [grund]        Schnellstart, fehlende Angaben fragt Flo nach
- giveaways                     laufende Giveaways
- giveaway abbrechen [id]       eigenes Giveaway abbrechen (Einsatz zurueck)
- giveaway ziehen [id]          eigenes Giveaway sofort auslosen

Zustand: data/giveaway.json
"""

import logging
import os
import random
import re
import time

import discord

import ai
from basis import FeatureBasis
import economy
import numfmt
from store import JsonStore

log = logging.getLogger("dcbot.giveaway")

# Sentinel: das Modul hat selbst geantwortet -> bot.py schweigt.
HANDLED = object()

fmt = numfmt.fmt

OWNER_ID = int(os.getenv("OWNER_ID", "1040135855710404659") or "0")

# Befehlswoerter (auch Tippfehler-nah und englisch).
_CMDS = ("giveaway", "giveaways", "gewinnspiel", "gewinnspiele", "verlosung",
         "verlosungen", "verlosen", "raffle", "gw")

# --- Grenzen (per .env justierbar) -------------------------------------------
MIN_STAKE = int(os.getenv("GIVEAWAY_MIN_STAKE", "100") or "100")
MAX_STAKE = int(os.getenv("GIVEAWAY_MAX_STAKE", "1000000000") or "1000000000")
MIN_SECONDS = int(os.getenv("GIVEAWAY_MIN_SECONDS", "60") or "60")
MAX_SECONDS = int(os.getenv("GIVEAWAY_MAX_SECONDS", str(7 * 86400)) or str(7 * 86400))
# So lange wartet Flo auf eine Antwort im Assistenten, dann bricht er ab.
WIZARD_TIMEOUT = int(os.getenv("GIVEAWAY_WIZARD_TIMEOUT", "180") or "180")
# Gleichzeitig laufende Giveaways pro Nutzer (Escrow-Schutz).
MAX_ACTIVE_PER_USER = int(os.getenv("GIVEAWAY_MAX_PER_USER", "1") or "1")
# Panel-Aktualisierung beim Beitreten hoechstens so oft (Discord-Rate-Limit).
EDIT_THROTTLE = 3.0
GOLD = 0x2ECC71

# --- Sprachverstaendnis ------------------------------------------------------
# Zahlwoerter: werden auch in zusammengeschriebenen Woertern erkannt
# ('zweitausendfünfhundert').
_ZAHLWORT = {
    "null": 0, "ein": 1, "eine": 1, "einen": 1, "einem": 1, "eins": 1, "one": 1,
    "zwei": 2, "zwo": 2, "two": 2, "drei": 3, "three": 3, "vier": 4, "four": 4,
    "fünf": 5, "fuenf": 5, "five": 5, "sechs": 6, "six": 6, "sieben": 7, "seven": 7,
    "acht": 8, "eight": 8, "neun": 9, "nine": 9, "zehn": 10, "ten": 10,
    "elf": 11, "eleven": 11, "zwölf": 12, "zwoelf": 12, "twelve": 12,
    "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15, "fuenfzehn": 15, "sechzehn": 16,
    "siebzehn": 17, "achtzehn": 18, "neunzehn": 19, "zwanzig": 20, "twenty": 20,
    "dreißig": 30, "dreissig": 30, "thirty": 30, "vierzig": 40, "forty": 40,
    "fünfzig": 50, "fuenfzig": 50, "fifty": 50, "sechzig": 60, "sixty": 60,
    "siebzig": 70, "achtzig": 80, "neunzig": 90,
}
_MULTIWORT = {
    "hundert": 100, "hundred": 100, "tausend": 1000, "thousand": 1000, "k": 1000,
    "million": 10 ** 6, "millionen": 10 ** 6, "mio": 10 ** 6, "mios": 10 ** 6,
    "milliarde": 10 ** 9, "milliarden": 10 ** 9, "mrd": 10 ** 9,
    "billion": 10 ** 9, "billions": 10 ** 9,
}
# Schluessel fuers Zerlegen zusammengeschriebener Zahlwoerter ('zweitausend').
# Laengste zuerst, damit 'dreizehn' nicht als 'drei' endet - und OHNE die
# Ein-Buchstaben-Kuerzel (k/m/b): sonst wird aus dem 'm' in "öhm" eine Million.
_WORTZAHL_KEYS = sorted((w for w in set(list(_ZAHLWORT) + list(_MULTIWORT)) if len(w) >= 3),
                        key=len, reverse=True)
# Fuellsilben, die INNERHALB eines Zahlworts stehen duerfen ('einundzwanzig').
_WORTZAHL_FUELL = ("und",)

# Zeit-Einheiten in Sekunden, viele Schreibweisen.
_EINHEIT = {
    "s": 1, "sec": 1, "secs": 1, "sek": 1, "sekunde": 1, "sekunden": 1, "second": 1,
    "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minuten": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "std": 3600, "stunde": 3600, "stunden": 3600,
    "hour": 3600, "hours": 3600,
    "t": 86400, "d": 86400, "tag": 86400, "tage": 86400, "tagen": 86400, "day": 86400,
    "days": 86400,
    "w": 604800, "woche": 604800, "wochen": 604800, "week": 604800, "weeks": 604800,
}
# Feste Wendungen fuer die Dauer.
# ACHTUNG: laengere/spezifischere Wendungen MUESSEN oben stehen -
# 'dreiviertelstunde' enthaelt 'viertelstunde'.
_DAUER_PHRASEN = (
    (("dreiviertelstunde", "drei viertel stunde", "dreiviertel stunde"), 2700),
    (("viertelstunde", "viertel stunde", "quarter"), 900),
    (("halbestunde", "halbe stunde", "halbstunde", "half an hour", "half hour"), 1800),
    (("eineinhalb stunden", "anderthalb stunden", "eineinhalbstunden", "1,5h", "1.5h"), 5400),
    (("halber tag", "halben tag", "half day"), 43200),
    (("über nacht", "ueber nacht", "overnight", "bis morgen", "morgen"), 43200),
    (("ganz kurz", "sehr kurz", "blitz", "schnell"), 300),
    (("kurz",), 600),
    (("mittel", "normal", "standard", "mittellang"), 3600),
    (("ganz lang", "sehr lang", "lange", "lang"), 86400),
    (("wochenende", "weekend"), 172800),
)

_JA = ("ja", "j", "jo", "joa", "jup", "jep", "jap", "yes", "y", "yep", "yeah", "yo",
       "passt", "passt so", "klar", "sicher", "safe", "los", "los gehts", "go",
       "mach", "machs", "mach es", "starten", "start", "senden", "abschicken",
       "bestätigen", "bestätige", "bestaetigen", "ok", "okay", "oki", "okey", "k",
       "gerne", "jawohl", "definitiv", "absolut", "auf jeden", "auf jeden fall",
       "genau", "richtig", "korrekt", "stimmt", "perfekt", "top", "bombe", "👍", "✅",
       "🆗", "💯", "dito", "einverstanden", "einverständnis", "bestätigt")
_NEIN = ("nein", "n", "ne", "nee", "nö", "noe", "nope", "no", "nah", "niemals",
         "abbrechen", "abbruch", "abbrich", "stop", "stopp", "cancel", "canceln",
         "vergiss es", "vergiss", "lass es", "lassen wir", "lieber nicht",
         "doch nicht", "nicht senden", "quatsch", "falsch", "❌", "✖️", "🚫",
         "👎", "exit", "quit", "raus", "egal")
# Verneinungen. Steht eines dieser Woerter im Satz, ist es KEINE Zustimmung -
# egal welches Ja-Wort sonst noch drinsteht ("sicher nicht", "nicht starten").
_NEGATION = ("nicht", "nich", "net", "kein", "keine", "keinen", "keinem", "keiner",
             "nie", "niemals", "nix", "nichts", "ohne")
_ABBRUCH = ("abbrechen", "abbruch", "abbrich", "stop", "stopp", "cancel", "canceln",
            "exit", "quit", "vergiss es", "vergiss das", "lass es", "lassen wir",
            "nevermind", "nvm", "raus", "ende", "beenden", "schluss", "❌")
_UEBERSPRINGEN = ("skip", "überspringen", "ueberspringen", "weiter", "keine", "kein",
                  "kein grund", "keinen", "keinen grund", "nichts", "nix", "-", "--",
                  "/", ".", "egal", "ohne", "ohne grund", "leer", "none", "no reason")
_ALLES = ("alles", "all", "all in", "allin", "everything", "komplett", "komplettes",
          "mein ganzes geld", "ganzes geld", "ganze geld", "alle coins", "alle meine coins",
          "max", "maximum", "maximal", "so viel wie möglich", "so viel wie moeglich",
          "vollen einsatz", "volles konto", "ganzes konto", "mein konto", "mein vermögen",
          "mein vermoegen", "alle kohle", "alle kohle die ich hab")
_HAELFTE = ("hälfte", "haelfte", "die hälfte", "die haelfte", "halbes", "halb",
            "half", "50%", "fifty fifty", "die halben", "halbe")
_DRITTEL = ("drittel", "ein drittel", "1/3", "33%")
_VIERTEL = ("viertel", "ein viertel", "1/4", "25%")


class Giveaway(FeatureBasis):
    """Kapselt Assistent, Escrow, Mitmach-Knopf und Ziehung."""

    def __init__(self):
        self._enabled = False
        self._store = None
        # Zuletzt geschuetztes Panel je Slot - damit das neue das alte
        # beim Auto-Loesch-Schutz wieder abmeldet.
        self._geschuetzt = {}
        self._client = None
        # Laufende Assistenten: (channel_id, user_id) -> dict
        self._wizards = {}
        # Panel-Bearbeitung drosseln: giveaway_id -> letzter Edit
        self._last_edit = {}

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        if os.getenv("GIVEAWAY_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Giveaways aus (GIVEAWAY_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Giveaways aus (economy ist aus - ohne Coins nichts zu verlosen).")
            return False
        self._store = JsonStore("giveaway.json",
                                default={"active": {}, "next_id": 1, "done": []})
        self._enabled = True
        log.info("Giveaways aktiv (%d laufend).", len(self._state().get("active", {})))
        return True

    def is_enabled(self):
        return self._enabled

    def _state(self):
        assert self._store is not None
        return self._store.data

    async def _save(self):
        if self._store is not None:
            await self._store.save()

    def _active(self):
        return self._state().setdefault("active", {})

    def escrow_von(self, uid):
        """Wie viele Coins dieser Nutzer gerade in laufenden Giveaways geparkt hat.

        Die Vermoegenssteuer (economy.depot_wert) rechnet das mit: sonst waere ein
        Giveaway ein Tresor - Einsatz kurz vor der naechtlichen Steuer einzahlen,
        danach abbrechen und alles zurueckbekommen."""
        if not self._enabled:
            return 0
        try:
            uid = int(uid)
        except (TypeError, ValueError):
            return 0
        summe = 0
        for g in self._active().values():
            try:
                if int(g.get("host", 0)) == uid and not g.get("settled"):
                    summe += max(0, int(g.get("stake", 0) or 0))
            except (TypeError, ValueError):
                continue
        return summe

    # --- Zahlen/Zeit aus Alltagssprache ----------------------------------
    def _norm(self, text):
        """Kleinschreibung, Emoji-Rand weg, Mehrfach-Leerzeichen zusammen."""
        s = str(text or "").strip().lower()
        s = s.replace(" ", " ")
        s = re.sub(r"\s+", " ", s)
        return s.strip(" .!?")

    def _dezimal(self, roh):
        """'1,5' und '1.5' -> 1.5, aber '10.000' -> 10000 (deutscher Tausenderpunkt),
        '1.234,56' -> 1234.56, '1 000' -> 1000."""
        s = str(roh).strip().replace(" ", "").replace(" ", "")
        if not s:
            raise ValueError("leer")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        elif s.count(".") == 1:
            vor, nach = s.split(".")
            if len(nach) == 3:                 # 10.000 -> Tausenderpunkt
                s = vor + nach
        elif s.count(".") > 1:
            s = s.replace(".", "")
        return float(s)

    def _wort_tokens(self, wort):
        """Zerlegt EIN Wort komplett in Zahlwoerter - oder gibt None zurueck.

        Streng von vorne bis hinten: nur wenn das GANZE Wort aus Zahlwoertern (plus
        'und') besteht, ist es eine Zahl. Sonst wuerde das 'ein' in "keine" oder das
        'm' in "öhm" mitgezaehlt und aus "keine ahnung" eine Million."""
        tokens = []
        i = 0
        while i < len(wort):
            for key in _WORTZAHL_KEYS:
                if wort.startswith(key, i):
                    tokens.append(key)
                    i += len(key)
                    break
            else:
                for f in _WORTZAHL_FUELL:
                    if wort.startswith(f, i):
                        i += len(f)
                        break
                else:
                    return None            # Fremdsilbe -> das ist keine Zahl
        return tokens or None

    def _wortzahl(self, text):
        """'zweitausendfünfhundert' / 'ein und zwanzig' -> Zahl (0 = nichts erkannt)."""
        woerter = re.findall(r"[a-zäöüß]+", str(text or "").lower())
        tokens = []
        for wort in woerter:
            teil = self._wort_tokens(wort)
            if teil:
                tokens.extend(teil)
        if not tokens:
            return 0
        total, current = 0, 0
        for tok in tokens:
            if tok in _ZAHLWORT:
                current += _ZAHLWORT[tok]
            else:
                mult = _MULTIWORT[tok]
                if mult >= 1000:
                    total += max(1, current) * mult
                    current = 0
                else:
                    current = max(1, current) * mult
        return total + current

    def _zahl(self, text):
        """Zahl aus Text: Ziffern (mit . , Leerzeichen, k/m/mrd) ODER Zahlwoerter."""
        s = self._norm(text)
        if not s:
            return None
        # Ziffern + angehaengter/getrennter Multiplikator ('5k', '2,5 mio', '3 tausend')
        m = re.search(r"(\d+(?:[.,]\d+)*)\s*(k|m|mio|mios|mrd|b|tausend|hundert|"
                      r"million(?:en)?|milliarde(?:n)?|thousand|hundred|billion)\b", s)
        if m:
            try:
                roh = self._dezimal(m.group(1))
            except ValueError:
                return None
            einheit = m.group(2)
            faktor = {"m": 10 ** 6, "b": 10 ** 9}.get(einheit) or _MULTIWORT.get(einheit, 1)
            return int(roh * faktor)
        # Reine Zahl, auch mit deutschen Tausenderpunkten / Leerzeichen.
        # NUR wenn ueberhaupt Ziffern drinstehen: economy.parse_amount ist bei
        # reinem Text zu gutmuetig ('eine million' kam als 1 zurueck).
        hat_ziffern = bool(re.search(r"\d", s))
        roh = economy.parse_amount(s) if (hat_ziffern and economy.is_enabled()) else None
        if roh:
            return int(roh)
        m = re.search(r"-?\d[\d\s.]*", s) if hat_ziffern else None
        if m:
            ziffern = re.sub(r"[^\d-]", "", m.group(0))
            if ziffern not in ("", "-"):
                try:
                    return int(ziffern)
                except ValueError:
                    pass
        wort = self._wortzahl(s)
        return wort or None

    @staticmethod
    def _als_id(roh):
        """Discord-ID aus irgendetwas (String/int) - 0, wenn unbrauchbar."""
        try:
            return int(str(roh).strip())
        except (TypeError, ValueError):
            return 0

    def _hat(self, text, woerter):
        """Ganzes Wort/Wendung im Text? (Wendungen mit Leerzeichen erlaubt)"""
        s = self._norm(text)
        for w in woerter:
            if " " in w:
                if w in s:
                    return True
            elif re.search(r"(?:^|\W)" + re.escape(w) + r"(?:\W|$)", s):
                return True
        return False

    def is_cancel(self, text):
        """Abbruch NUR, wenn die Nachricht wirklich ein Abbruch ist.

        Vorher reichte das Wort irgendwo im Satz - eine Begruendung wie
        "weil ich Schluss mache", "zum Ende des Monats" oder "ich geh raus feiern"
        hat den Assistenten damit mitten im Schritt abgewuergt."""
        s = self._norm(text)
        if not s:
            return False
        if s in _ABBRUCH:
            return True
        # Kurze Ansage wie "abbrechen bitte" / "stop das" zaehlt auch noch.
        woerter = s.split()
        if len(woerter) <= 3 and woerter[0] in _ABBRUCH:
            return True
        return False

    def is_yes(self, text):
        """Zustimmung? Eine VERNEINUNG im Satz zaehlt nie als Ja.

        Vorher reichte irgendein Ja-Wort irgendwo im Satz - und weil "sicher"
        auf der Ja-Liste steht, galt "sicher nicht" als Zustimmung. Dasselbe bei
        "nicht bestaetigen" und "warte, nicht starten". Das Giveaway startete und
        der Einsatz war weg, obwohl der Nutzer ausdruecklich abgelehnt hat.
        Ein exaktes "ja"/"ok" geht weiterhin sofort durch."""
        s = self._norm(text)
        if s in _JA:
            return True
        if self._hat(text, _NEGATION):
            return False
        return self._hat(text, _JA)

    def is_no(self, text):
        s = self._norm(text)
        return s in _NEIN or self._hat(text, _NEIN)

    def parse_stake(self, text, balance):
        """Einsatz aus der Antwort. Rueckgabe: (betrag|None, hinweis)."""
        s = self._norm(text)
        if not s:
            return None, ""
        balance = max(0, int(balance))
        if self._hat(s, _ALLES):
            return balance, "dein ganzes Guthaben"
        if self._hat(s, _HAELFTE):
            return balance // 2, "die Hälfte deines Guthabens"
        if self._hat(s, _DRITTEL):
            return balance // 3, "ein Drittel deines Guthabens"
        if self._hat(s, _VIERTEL):
            return balance // 4, "ein Viertel deines Guthabens"
        m = re.search(r"(\d{1,3})\s*%", s)
        if m:
            p = max(0, min(100, int(m.group(1))))
            return balance * p // 100, f"{p}% deines Guthabens"
        wert = self._zahl(s)
        if wert is None:
            return None, ""
        # Nie negativ zurueckgeben: der Aufrufer soll sich darauf verlassen koennen,
        # auch wenn er selbst keine Untergrenze prueft.
        return max(0, int(wert)), ""

    def parse_duration(self, text):
        """Dauer in Sekunden aus der Antwort (None = nicht verstanden)."""
        s = self._norm(text)
        if not s:
            return None
        for phrasen, secs in _DAUER_PHRASEN:
            for p in phrasen:
                if p in s:
                    return secs
        gesamt = 0
        letzter = 0
        # '1h 30m', '2 stunden 15 minuten', '90min', '1h30' (die 30 sind Minuten!)
        for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([a-zäöüß]+)?", s):
            roh, einheit = m.group(1), (m.group(2) or "").strip()
            try:
                zahl = float(roh.replace(",", "."))
            except ValueError:
                continue
            faktor = _EINHEIT.get(einheit)
            if faktor is None and einheit:
                # 'stunden'/'minuten' mit Anhang: laengste passende Einheit nehmen
                for key in sorted(_EINHEIT, key=len, reverse=True):
                    if einheit.startswith(key):
                        faktor = _EINHEIT[key]
                        break
            if faktor is None:
                # Nackte Zahl: nach einer Einheit ist die NAECHST KLEINERE gemeint
                # ('1h30' = 1 Stunde 30 Minuten), am Anfang sind es Minuten.
                faktor = {604800: 86400, 86400: 3600, 3600: 60, 60: 1}.get(letzter, 60)
            gesamt += int(zahl * faktor)
            letzter = faktor
        if gesamt:
            return gesamt
        # Nur Worte: 'eine stunde', 'zwei tage', 'stunde'
        einheit_gefunden = None
        for key in sorted(_EINHEIT, key=len, reverse=True):
            if len(key) > 2 and re.search(r"(?:^|\W)" + re.escape(key) + r"(?:\W|$)", s):
                einheit_gefunden = _EINHEIT[key]
                break
        if einheit_gefunden:
            n = self._wortzahl(re.sub(r"[a-zäöüß]*(sekunde|minute|stunde|tag|woche)[a-zäöüß]*",
                                      " ", s)) or 1
            return int(n * einheit_gefunden)
        return None

    def dauer_text(self, secs):
        """Sekunden -> '2 Tage 3 Stunden' (deutsch, hoechstens zwei Einheiten)."""
        secs = max(0, int(secs))
        teile = []
        for einzahl, mehrzahl, gr in (("Tag", "Tage", 86400), ("Stunde", "Stunden", 3600),
                                      ("Minute", "Minuten", 60),
                                      ("Sekunde", "Sekunden", 1)):
            n, secs = divmod(secs, gr)
            if n:
                teile.append(f"{fmt(n)} {einzahl if n == 1 else mehrzahl}")
            if len(teile) == 2:
                break
        return " ".join(teile) or "0 Sekunden"

    # --- Assistent --------------------------------------------------------
    def _wizard_key(self, message):
        return (message.channel.id, message.author.id)

    def _frage_einsatz(self, guthaben):
        e = discord.Embed(
            title="🎉 Giveaway einrichten · Schritt 1 von 3",
            description=("**Wie viele Flo Coins willst du verlosen?**\n"
                         f"Dein Guthaben: **{fmt(guthaben)}** {economy.COIN}\n\n"
                         "Antworte einfach so, wie du redest:\n"
                         "`5000` · `5k` · `10.000` · `2 mio` · `zweitausendfünfhundert`\n"
                         "`alles` · `die hälfte` · `25%`\n\n"
                         f"Mindestens **{fmt(MIN_STAKE)}** {economy.COIN}. "
                         "`abbrechen` beendet den Assistenten."),
            color=GOLD)
        return e

    def _frage_grund(self, betrag):
        return discord.Embed(
            title="🎉 Giveaway einrichten · Schritt 2 von 3",
            description=(f"Einsatz: **{fmt(betrag)}** {economy.COIN} ✅\n\n"
                         "**Warum machst du das Giveaway?**\n"
                         "Schreib einen Satz – der steht später im Giveaway.\n"
                         "_z. B. »weil ich 1 Mio geknackt habe« oder »einfach so«._\n\n"
                         "`keine` / `-` lässt es weg · `abbrechen` beendet."),
            color=GOLD)

    def _frage_dauer(self, betrag):
        return discord.Embed(
            title="🎉 Giveaway einrichten · Schritt 3 von 3",
            description=("**Wie lange soll das Giveaway laufen?**\n"
                         "`10min` · `1h` · `1h 30m` · `eine halbe stunde` · `2 tage`\n"
                         "`kurz` (10 Min) · `mittel` (1 Std) · `lang` (1 Tag)\n\n"
                         f"Erlaubt: {self.dauer_text(MIN_SECONDS)} bis "
                         f"{self.dauer_text(MAX_SECONDS)}.\n"
                         "Eine nackte Zahl versteht Flo als **Minuten**."),
            color=GOLD)

    def _frage_bestaetigung(self, data, guthaben):
        rest = guthaben - data["stake"]
        e = discord.Embed(
            title="🎉 Passt das so?",
            description=("Flo bucht den Einsatz **sofort** ab und legt ihn beiseite, "
                         "bis gezogen wird.\n"
                         "Kommt kein einziger Teilnehmer zusammen, bekommst du alles zurück."),
            color=GOLD)
        e.add_field(name="Einsatz", value=f"**{fmt(data['stake'])}** {economy.COIN}", inline=True)
        e.add_field(name="Dauer", value=self.dauer_text(data["seconds"]), inline=True)
        e.add_field(name="Danach auf deinem Konto", value=f"{fmt(rest)} {economy.COIN}",
                    inline=True)
        e.add_field(name="Grund", value=(data.get("reason") or "_kein Grund angegeben_"),
                    inline=False)
        e.set_footer(text="Antworte 'ja' zum Starten oder 'nein' zum Abbrechen.")
        return e

    async def start_wizard(self, message, vorgabe=None):
        """Startet den Assistenten (oder ueberspringt schon bekannte Angaben)."""
        uid = message.author.id
        eigene = [g for g in self._active().values() if int(g.get("host", 0)) == uid]
        if len(eigene) >= MAX_ACTIVE_PER_USER:
            return (f"Du hast schon ein laufendes Giveaway (#{eigene[0]['id']}). "
                    f"Beende es mit `{self._bot_name} giveaway ziehen` oder "
                    f"`{self._bot_name} giveaway abbrechen`.")
        guthaben = economy.get_coins(uid)
        if guthaben < MIN_STAKE:
            return (f"Für ein Giveaway brauchst du mindestens **{fmt(MIN_STAKE)}** "
                    f"{economy.COIN} – du hast **{fmt(guthaben)}**.")
        data = dict(vorgabe or {})
        # Ein Nutzer hat immer nur EINEN Assistenten: startet er in einem anderen
        # Kanal neu, wird der alte verworfen (sonst laufen zwei Frage-Runden
        # parallel und man kann zwei Einsaetze hinterlegen).
        for key in [k for k in self._wizards if k[1] == uid]:
            self._wizards.pop(key, None)
        w = {"step": "stake", "data": data, "deadline": time.time() + WIZARD_TIMEOUT,
             "channel": message.channel.id, "host": uid}
        self._wizards[self._wizard_key(message)] = w
        return await self._naechste_frage(message.channel, w, message.author)

    async def _naechste_frage(self, channel, w, user):
        """Stellt die naechste offene Frage (ueberspringt bereits Bekanntes)."""
        data = w["data"]
        guthaben = economy.get_coins(w["host"])
        w["deadline"] = time.time() + WIZARD_TIMEOUT
        if "stake" not in data:
            w["step"] = "stake"
            await self._send(channel, self._frage_einsatz(guthaben))
            return HANDLED
        if "reason" not in data:
            w["step"] = "reason"
            await self._send(channel, self._frage_grund(data["stake"]))
            return HANDLED
        if "seconds" not in data:
            w["step"] = "seconds"
            await self._send(channel, self._frage_dauer(data["stake"]))
            return HANDLED
        w["step"] = "confirm"
        await self._send(channel, self._frage_bestaetigung(data, guthaben))
        return HANDLED

    async def _send(self, channel, embed=None, content=None, view=None):
        try:
            msg = await channel.send(content=content, embed=embed, view=view)
            self._protect(msg)
            return msg
        except discord.HTTPException:
            log.warning("Giveaway-Nachricht konnte nicht gesendet werden")
            return None

    async def on_message_passive(self, message):
        """Antwort auf eine Assistenten-Frage? Dann hier verarbeiten (True), sonst False.

        Laeuft VOR dem 'Flo'-Trigger, damit man einfach '5k' schreiben kann,
        ohne Flo jedes Mal anzusprechen."""
        if not self._enabled or message.guild is None:
            return False
        key = self._wizard_key(message)
        w = self._wizards.get(key)
        if w is None:
            return False
        text = (message.content or "").strip()
        if not text:
            return False
        # Der Nutzer ruft einen anderen Flo-Befehl auf -> Assistent nicht im Weg stehen.
        if re.match(r"(?i)^\s*(flo|hey flo|ok flo)\b", text) and not self.is_cancel(text):
            return False
        if self.is_cancel(text):
            self._wizards.pop(key, None)
            await self._send(message.channel, content="Alles klar, abgebrochen. 👍")
            return True
        try:
            return await self._wizard_step(message, w, text)
        except Exception:  # noqa: BLE001 - Assistent darf nie den Bot mitnehmen
            log.exception("Giveaway-Assistent fehlgeschlagen")
            self._wizards.pop(key, None)
            await self._send(message.channel,
                             content="Da ist beim Giveaway-Assistenten etwas schiefgelaufen.")
            return True

    async def _wizard_step(self, message, w, text):
        data = w["data"]
        guthaben = economy.get_coins(w["host"])
        if w["step"] == "stake":
            betrag, hinweis = self.parse_stake(text, guthaben)
            if betrag is None:
                await self._send(message.channel, content=(
                    "Das habe ich nicht als Betrag erkannt. Probier `5000`, `5k`, "
                    "`10.000`, `zwei tausend`, `alles` oder `die hälfte`."))
                w["deadline"] = time.time() + WIZARD_TIMEOUT
                return True
            if betrag < MIN_STAKE:
                await self._send(message.channel, content=(
                    f"Mindestens **{fmt(MIN_STAKE)}** {economy.COIN} müssen es sein. "
                    "Wie viel soll es sein?"))
                w["deadline"] = time.time() + WIZARD_TIMEOUT
                return True
            if betrag > MAX_STAKE:
                betrag = MAX_STAKE
                hinweis = f"auf das Maximum von {fmt(MAX_STAKE)} begrenzt"
            if betrag > guthaben:
                await self._send(message.channel, content=(
                    f"Du hast nur **{fmt(guthaben)}** {economy.COIN}. "
                    "Nenn einen kleineren Betrag (oder `alles`)."))
                w["deadline"] = time.time() + WIZARD_TIMEOUT
                return True
            data["stake"] = int(betrag)
            if hinweis:
                await self._send(message.channel,
                                 content=f"Verstanden: **{fmt(betrag)}** {economy.COIN} ({hinweis}).")
            await self._naechste_frage(message.channel, w, message.author)
            return True
        if w["step"] == "reason":
            if self._hat(text, _UEBERSPRINGEN) or len(text) <= 1:
                data["reason"] = ""
            else:
                data["reason"] = text[:400]
            await self._naechste_frage(message.channel, w, message.author)
            return True
        if w["step"] == "seconds":
            secs = self.parse_duration(text)
            if secs is None or secs <= 0:
                await self._send(message.channel, content=(
                    "Die Dauer habe ich nicht verstanden. Probier `10min`, `1h`, "
                    "`1h 30m`, `eine halbe stunde`, `2 tage` – oder `kurz`/`mittel`/`lang`."))
                w["deadline"] = time.time() + WIZARD_TIMEOUT
                return True
            gedeckelt = ""
            if secs < MIN_SECONDS:
                secs, gedeckelt = MIN_SECONDS, f"auf {self.dauer_text(MIN_SECONDS)} angehoben"
            elif secs > MAX_SECONDS:
                secs, gedeckelt = MAX_SECONDS, f"auf {self.dauer_text(MAX_SECONDS)} begrenzt"
            data["seconds"] = int(secs)
            if gedeckelt:
                await self._send(message.channel, content=f"Dauer {gedeckelt}.")
            await self._naechste_frage(message.channel, w, message.author)
            return True
        if w["step"] == "confirm":
            if self.is_no(text):
                self._wizards.pop(self._wizard_key(message), None)
                await self._send(message.channel, content="Okay, nichts passiert. 👍")
                return True
            if not self.is_yes(text):
                await self._send(message.channel, content=(
                    "Sag `ja` (oder `passt`, `los`, `start`) zum Starten – "
                    "`nein`/`abbrechen` lässt es."))
                w["deadline"] = time.time() + WIZARD_TIMEOUT
                return True
            self._wizards.pop(self._wizard_key(message), None)
            await self._starten(message, data)
            return True
        return False

    # --- Start (Escrow) ---------------------------------------------------
    async def _starten(self, message, data):
        """Bucht den Einsatz ab (geprueft!) und postet das Giveaway."""
        uid = message.author.id
        stake = int(data.get("stake", 0))
        seconds = int(data.get("seconds", 0))
        # Hier wird WIRKLICH Geld bewegt - deshalb pruefen wir die Grenzen noch
        # einmal selbst, egal was der Aufrufer schon geprueft haben will.
        # Ein negativer Einsatz wuerde sonst zur GUTSCHRIFT (-(-500) = +500) und
        # Coins aus dem Nichts erzeugen.
        if stake < MIN_STAKE or stake > MAX_STAKE:
            log.warning("Giveaway-Start mit unzulaessigem Einsatz abgelehnt (%s: %d).",
                        uid, stake)
            await self._send(message.channel, content=(
                f"Der Einsatz ist ungültig – erlaubt sind **{fmt(MIN_STAKE)}** bis "
                f"**{fmt(MAX_STAKE)}** {economy.COIN}."))
            return
        if seconds < MIN_SECONDS or seconds > MAX_SECONDS:
            log.warning("Giveaway-Start mit unzulaessiger Dauer abgelehnt (%s: %d).",
                        uid, seconds)
            await self._send(message.channel, content=(
                f"Die Dauer ist ungültig – erlaubt sind "
                f"{self.dauer_text(MIN_SECONDS)} bis {self.dauer_text(MAX_SECONDS)}."))
            return
        # Und auch das Limit nochmal: der Assistent prueft es beim START, aber wer
        # in ZWEI Kanaelen je einen Assistenten oeffnet, hatte beide Male 0 laufende
        # Giveaways - und konnte so zwei Einsaetze gleichzeitig hinterlegen.
        eigene = [g for g in self._active().values() if int(g.get("host", 0)) == uid]
        if len(eigene) >= MAX_ACTIVE_PER_USER:
            await self._send(message.channel, content=(
                f"Du hast schon ein laufendes Giveaway (#{eigene[0]['id']}) – "
                f"beende das erst (`{self._bot_name} giveaway ziehen` oder "
                f"`{self._bot_name} giveaway abbrechen`)."))
            return
        vorher = economy.get_coins(uid)
        if vorher < stake:
            await self._send(message.channel, content=(
                f"Dein Guthaben reicht nicht mehr (**{fmt(vorher)}** {economy.COIN}). "
                "Starte den Assistenten neu."))
            return
        # Abbuchen VOR allem anderen - und pruefen, ob wirklich alles kam.
        economy.add_coins(uid, -stake, reason="giveaway")
        nachher = economy.get_coins(uid)
        abgebucht = vorher - nachher
        if abgebucht != stake:
            if abgebucht:
                economy.add_coins(uid, abgebucht, reason="giveaway-rueck")
            log.warning("Giveaway-Abbuchung unvollstaendig (%s: %d von %d) - zurueckgebucht.",
                        uid, abgebucht, stake)
            await self._send(message.channel, content=(
                "Die Abbuchung hat nicht geklappt – es wurde nichts verändert."))
            return
        st = self._state()
        gid = int(st.get("next_id", 1))
        st["next_id"] = gid + 1
        jetzt = time.time()
        g = {"id": gid, "host": uid, "guild": message.guild.id,
             "channel": message.channel.id, "message": 0, "stake": stake,
             "escrow": stake, "reason": data.get("reason", ""),
             "started": jetzt, "ends": jetzt + seconds, "entries": []}
        self._active()[str(gid)] = g
        await self._save()
        try:
            await economy.flush()
        except Exception:  # noqa: BLE001
            pass
        view = JoinView(gid)
        msg = await self._send(message.channel, embed=self._panel(g, message.author),
                               content="🎉 **Neues Giveaway!**", view=view)
        if msg is not None:
            g["message"] = msg.id
            view.message = msg
            await self._save()
        log.info("Giveaway #%d gestartet von %s: %d Coins, %s.",
                 gid, uid, stake, self.dauer_text(seconds))

    # --- Anzeige ----------------------------------------------------------
    def _panel(self, g, host_user=None):
        ends = int(g.get("ends", 0))
        e = discord.Embed(
            title=f"🎉 GIVEAWAY #{g['id']} · {fmt(g['stake'])} {economy.COIN}",
            description=(f"**Preis:** {fmt(g['stake'])} Flo Coins\n"
                         f"**Endet:** <t:{ends}:R>  ·  <t:{ends}:t>\n\n"
                         "Klick auf **🎉 Mitmachen**, um dabei zu sein."),
            color=GOLD)
        if g.get("reason"):
            e.add_field(name="Warum?", value=str(g["reason"])[:900], inline=False)
        e.add_field(name="Teilnehmer", value=f"**{fmt(len(g.get('entries', [])))}**", inline=True)
        e.add_field(name="Veranstalter", value=f"<@{g['host']}>", inline=True)
        if host_user is not None:
            try:
                e.set_thumbnail(url=host_user.display_avatar.url)
            except Exception:  # noqa: BLE001 - Bild ist Deko
                pass
        e.set_footer(text="Ein Klick reicht · der Einsatz liegt schon bei Flo")
        return e

    def _ende_panel(self, g, gewinner_id, teilnehmer):
        betrag = fmt(g.get("escrow", g.get("stake", 0)))
        if gewinner_id:
            text = (f"**Gewinner:** <@{gewinner_id}>\n"
                    f"**Gewinn:** {betrag} {economy.COIN}\n"
                    f"**Teilnehmer:** {fmt(teilnehmer)}")
        else:
            text = (f"Niemand hat mitgemacht – die **{betrag}** {economy.COIN} gehen "
                    f"zurück an <@{g['host']}>.")
        e = discord.Embed(title=f"🎉 Giveaway #{g['id']} beendet", description=text,
                          color=GOLD if gewinner_id else 0x95A5A6)
        if g.get("reason"):
            e.add_field(name="Warum?", value=str(g["reason"])[:900], inline=False)
        e.add_field(name="Veranstalter", value=f"<@{g['host']}>", inline=True)
        return e

    # --- Mitmachen --------------------------------------------------------
    async def join(self, interaction, gid):
        """Knopf 'Mitmachen'. Antwortet immer NUR dem Klickenden (ephemeral)."""
        g = self._active().get(str(gid))
        if g is None:
            await self._ephemeral(interaction, "Dieses Giveaway läuft nicht mehr.")
            return
        if time.time() >= float(g.get("ends", 0)):
            await self._ephemeral(interaction, "Zu spät – das Giveaway ist schon vorbei.")
            return
        uid = interaction.user.id
        if uid == int(g.get("host", 0)):
            await self._ephemeral(interaction,
                                  "Du veranstaltest das Giveaway – mitmachen wäre unfair. 😉")
            return
        if getattr(interaction.user, "bot", False):
            await self._ephemeral(interaction, "Bots dürfen nicht mitspielen.")
            return
        entries = g.setdefault("entries", [])
        # Typunabhaengig vergleichen: stand die ID (z. B. aus einer haendisch
        # bearbeiteten Datei) als STRING drin, wurde derselbe Nutzer sonst ein
        # zweites Mal eingetragen - und hatte doppelte Gewinnchance.
        if any(self._als_id(u) == uid for u in entries):
            await self._ephemeral(
                interaction, f"Du bist schon dabei – aktuell **{fmt(len(entries))}** Teilnehmer. "
                             f"Gewinnchance: **{100.0 / max(1, len(entries)):.1f}%**")
            return
        entries.append(uid)
        await self._save()
        await self._ephemeral(
            interaction, f"🎉 Du bist dabei! **{fmt(len(entries))}** Teilnehmer · "
                         f"Gewinnchance **{100.0 / len(entries):.1f}%**")
        await self._panel_aktualisieren(interaction, g)

    async def _ephemeral(self, interaction, text):
        try:
            await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def _panel_aktualisieren(self, interaction, g):
        """Teilnehmerzahl im Panel nachziehen - gedrosselt (Rate-Limit)."""
        gid = int(g["id"])
        jetzt = time.time()
        letzte = self._last_edit.get(gid, 0.0)
        if jetzt - letzte < EDIT_THROTTLE:
            return
        self._last_edit[gid] = jetzt
        try:
            msg = interaction.message
            if msg is not None:
                await msg.edit(embed=self._panel(g))
        except discord.HTTPException:
            pass

    # --- Ziehung ----------------------------------------------------------
    async def tick(self, client):
        """Von bot.py periodisch: abgelaufene Giveaways auslosen, alte Assistenten
        vergessen. Rueckgabe: Anzahl beendeter Giveaways."""
        if not self._enabled:
            return 0
        if client is not None:
            self._client = client
        # Abgelaufene Assistenten aufraeumen.
        for key, w in list(self._wizards.items()):
            if w.get("deadline", 0) < time.time():
                self._wizards.pop(key, None)
                chan = client.get_channel(key[0]) if client else None
                if chan is not None:
                    await self._send(chan, content=(
                        f"<@{key[1]}> Der Giveaway-Assistent ist abgelaufen – "
                        f"sag einfach nochmal `{self._bot_name} giveaway`."))
        fertig = 0
        for gid, g in list(self._active().items()):
            try:
                if time.time() < float(g.get("ends", 0)):
                    continue
                await self._auslosen(client, g)
                fertig += 1
            except Exception:  # noqa: BLE001 - ein kaputtes Giveaway blockiert nicht alle
                log.exception("Giveaway #%s konnte nicht ausgelost werden", gid)
        return fertig

    async def _auslosen(self, client, g, *, abgebrochen=False):
        """Zieht den Gewinner (oder bucht bei 0 Teilnehmern/Abbruch zurueck)."""
        gid = str(g["id"])
        # Riegel VOR jeder Zahlung: 'giveaway ziehen' und der Tick-Loop koennen
        # sonst gleichzeitig auslosen und den Einsatz zweimal auszahlen. Dafuer
        # IMMER das Objekt aus dem Store nehmen und nie dem uebergebenen blind
        # vertrauen - sonst haelt der Riegel nicht, wenn zwei Kopien desselben
        # Giveaways im Umlauf sind.
        aktuell = self._active().get(gid)
        if aktuell is None or aktuell.get("settled"):
            log.info("Giveaway #%s war schon abgerechnet - nichts getan.", gid)
            return
        g = aktuell
        # Riegel im Speicher SOFORT setzen (schuetzt gegen doppeltes Auslosen),
        # aber erst NACH der Auszahlung auf die Platte schreiben: ein Absturz
        # genau dazwischen haette den Einsatz sonst verschluckt - als erledigt
        # markiert, aber nie ausgezahlt.
        g["settled"] = True
        self._active().pop(gid, None)
        host_id = self._als_id(g.get("host"))
        entries = []
        for u in g.get("entries", []):
            wer = self._als_id(u)
            if wer and wer != host_id and wer not in entries:
                entries.append(wer)
        betrag = int(g.get("escrow", g.get("stake", 0)))
        gewinner = None
        if abgebrochen or not entries:
            if betrag > 0:
                economy.add_coins(int(g["host"]), betrag, reason="giveaway-rueck")
        else:
            gewinner = random.choice(entries)
            if betrag > 0:
                economy.add_coins(gewinner, betrag, reason="giveaway-gewinn")
        st = self._state()
        st.setdefault("done", []).append(
            {"id": g["id"], "host": g["host"], "stake": betrag,
             "winner": gewinner or 0, "entries": len(entries),
             "ended": int(time.time()), "cancelled": bool(abgebrochen)})
        st["done"] = st["done"][-200:]                 # Historie bleibt handlich
        await self._save()
        try:
            await economy.flush()
        except Exception:  # noqa: BLE001
            pass
        self._last_edit.pop(int(g["id"]), None)
        await self._verkuenden(client, g, gewinner, len(entries), abgebrochen)
        log.info("Giveaway #%s beendet: %s (%d Teilnehmer, %d Coins).",
                 gid, f"Gewinner {gewinner}" if gewinner else
                 ("abgebrochen" if abgebrochen else "keine Teilnehmer"), len(entries), betrag)

    async def _verkuenden(self, client, g, gewinner, teilnehmer, abgebrochen):
        """Panel abschliessen und den Gewinner ausrufen (best effort)."""
        chan = None
        try:
            chan = client.get_channel(int(g.get("channel", 0))) if client else None
        except Exception:  # noqa: BLE001
            chan = None
        if chan is None:
            return
        # Altes Panel abschliessen (Knopf weg).
        try:
            if g.get("message"):
                alt = await chan.fetch_message(int(g["message"]))
                await alt.edit(embed=self._ende_panel(g, gewinner, teilnehmer), view=None)
        except discord.HTTPException:
            pass
        except Exception:  # noqa: BLE001
            pass
        betrag = int(g.get("escrow", g.get("stake", 0)))
        if abgebrochen:
            text = (f"🚫 Giveaway **#{g['id']}** abgebrochen – <@{g['host']}> bekommt die "
                    f"**{fmt(betrag)}** {economy.COIN} zurück.")
        elif gewinner:
            text = (f"🎉 <@{gewinner}> gewinnt **{fmt(betrag)}** {economy.COIN} aus dem "
                    f"Giveaway von <@{g['host']}>! ({fmt(teilnehmer)} Teilnehmer)")
        else:
            text = (f"😐 Giveaway **#{g['id']}** ohne Teilnehmer – <@{g['host']}> bekommt "
                    f"die **{fmt(betrag)}** {economy.COIN} zurück.")
        await self._send(chan, content=text)

    # --- Befehle ----------------------------------------------------------
    async def handle(self, message):
        """'giveaway ...' / 'giveaways'. Rueckgabe: None | Text | HANDLED."""
        if not self._enabled or message.guild is None:
            return None
        roh = ai.strip_lead(message.content or "")
        teile = roh.split()
        if not teile or teile[0].lower().strip(".,!?") not in _CMDS:
            return None
        rest = " ".join(teile[1:]).strip()
        low = rest.lower()
        # Uebersicht
        if not rest and teile[0].lower().rstrip(".,!?") in ("giveaways", "gewinnspiele",
                                                           "verlosungen"):
            return self._liste_embed()
        if low in ("liste", "list", "laufend", "laufende", "alle", "übersicht", "uebersicht"):
            return self._liste_embed()
        if low.startswith(("hilfe", "help", "?")):
            return self._hilfe_embed()
        # Abbrechen / sofort ziehen - nur als ERSTES Wort nach 'giveaway'.
        # Vorher wurde irgendwo im Text gesucht, und der Schnellstart
        # 'giveaway 5k 2h weil ich jetzt Lust habe' loste dadurch ein LAUFENDES
        # Giveaway sofort aus (Gewinner Stunden zu frueh) bzw. brach es ab -
        # 'jetzt', 'ende' und 'stop' sind ganz normale deutsche Woerter.
        erstes = (low.split() or [""])[0].strip(".,!?:")
        if erstes in ("abbrechen", "abbruch", "cancel", "stop", "stopp", "löschen",
                      "loeschen", "zurücknehmen", "zuruecknehmen"):
            return await self._abbrechen(message, low)
        if erstes in ("ziehen", "ziehe", "auslosen", "losen", "jetzt", "beenden",
                      "ende", "draw", "sofort"):
            return await self._sofort_ziehen(message, low)
        # Schnellstart: 'giveaway 5k 2h weil ich Lust habe'
        vorgabe = self._vorgabe_aus_text(rest, economy.get_coins(message.author.id))
        return await self.start_wizard(message, vorgabe)

    def _vorgabe_aus_text(self, rest, guthaben):
        """Liest Einsatz/Dauer/Grund aus einer Zeile wie '5k 2h weil ...' heraus.
        Was fehlt, fragt der Assistent nach."""
        if not rest:
            return {}
        data = {}
        text = rest
        # Dauer: erstes Zahl+Zeiteinheit-Paar herausnehmen.
        m = re.search(r"(?i)\b(\d+(?:[.,]\d+)?)\s*(sek(?:unden?)?|s|min(?:uten?)?|m|"
                      r"std|stunden?|h|tage?n?|t|d|wochen?|w)\b", text)
        if m:
            secs = self.parse_duration(m.group(0))
            if secs:
                data["seconds"] = max(MIN_SECONDS, min(int(secs), MAX_SECONDS))
                text = (text[:m.start()] + " " + text[m.end():]).strip()
        # Einsatz: erste Zahl (mit k/m/mrd) oder 'alles'.
        m = re.search(r"(?i)\b(\d+(?:[.,]\d+)?\s*(?:k|m|mio|mrd|b)?|alles|hälfte|haelfte)\b", text)
        if m:
            betrag, _h = self.parse_stake(m.group(0), guthaben)
            if betrag and betrag >= MIN_STAKE and betrag <= guthaben:
                data["stake"] = int(betrag)
                text = (text[:m.start()] + " " + text[m.end():]).strip()
        grund = re.sub(r"(?i)^(weil|für|fuer|wegen|grund:?)\s+", "", text).strip(" ,.-")
        if grund:
            data["reason"] = grund[:400]
        return data

    def _mein_giveaway(self, message, low):
        """Das gemeinte Giveaway: per '#id' oder das eigene laufende."""
        m = re.search(r"#?(\d{1,6})", low)
        if m:
            g = self._active().get(str(int(m.group(1))))
            if g is None:
                return None, f"Ein Giveaway mit der Nummer **{int(m.group(1))}** läuft nicht."
            if int(g["host"]) != message.author.id and message.author.id != OWNER_ID:
                return None, "Das ist nicht dein Giveaway."
            return g, None
        eigene = [g for g in self._active().values()
                  if int(g.get("host", 0)) == message.author.id]
        if not eigene:
            return None, "Du hast gerade kein laufendes Giveaway."
        return eigene[0], None

    async def _abbrechen(self, message, low):
        g, fehler = self._mein_giveaway(message, low)
        if fehler:
            return fehler
        await self._auslosen(self._client_von(message), g, abgebrochen=True)
        return HANDLED

    async def _sofort_ziehen(self, message, low):
        g, fehler = self._mein_giveaway(message, low)
        if fehler:
            return fehler
        if not [u for u in g.get("entries", []) if int(u) != int(g["host"])]:
            return ("Es hat noch niemand mitgemacht. Warte noch etwas – oder brich es mit "
                    f"`{self._bot_name} giveaway abbrechen` ab (Einsatz zurück).")
        await self._auslosen(self._client_von(message), g)
        return HANDLED

    def _client_von(self, message):
        """Client fuer Ansagen: der in on_ready gemerkte - notfalls aus der Nachricht."""
        if self._client is not None:
            return self._client
        for pfad in (lambda: message._state._get_client(),
                     lambda: message.guild._state._get_client()):
            try:
                c = pfad()
                if c is not None:
                    return c
            except Exception:  # noqa: BLE001
                continue
        return None

    def _liste_embed(self):
        aktiv = sorted(self._active().values(), key=lambda g: g.get("ends", 0))
        e = discord.Embed(title="🎉 Laufende Giveaways", color=GOLD)
        if not aktiv:
            e.description = (f"Gerade läuft keins. Starte eins mit "
                             f"`{self._bot_name} giveaway`.")
            return e
        for g in aktiv[:10]:
            e.add_field(
                name=f"#{g['id']} · {fmt(g['stake'])} {economy.COIN}",
                value=(f"von <@{g['host']}> · endet <t:{int(g['ends'])}:R> · "
                       f"**{fmt(len(g.get('entries', [])))}** Teilnehmer"
                       + (f"\n_{str(g['reason'])[:120]}_" if g.get("reason") else "")),
                inline=False)
        return e

    def _hilfe_embed(self):
        e = discord.Embed(
            title="🎉 Giveaways",
            description=(f"`{self._bot_name} giveaway` – Flo fragt dich Einsatz, Grund und "
                         "Dauer nacheinander ab.\n"
                         f"`{self._bot_name} giveaway 5k 2h weil ...` – Schnellstart.\n"
                         f"`{self._bot_name} giveaways` – laufende Giveaways.\n"
                         f"`{self._bot_name} giveaway ziehen` – eigenes sofort auslosen.\n"
                         f"`{self._bot_name} giveaway abbrechen` – Einsatz zurückholen.\n\n"
                         "Der Einsatz wird beim Start abgebucht und geht am Ende an den "
                         "Gewinner. Ohne Teilnehmer bekommst du alles zurück."),
            color=GOLD)
        return e

    # --- Views nach Neustart wieder anmelden ------------------------------
    def register_views(self, client):
        """Macht die Mitmach-Knoepfe nach einem Bot-Neustart wieder klickbar."""
        if client is not None:
            self._client = client
        if not self._enabled or client is None:
            return 0
        n = 0
        for g in list(self._active().values()):
            try:
                client.add_view(JoinView(int(g["id"])))
                n += 1
            except Exception:  # noqa: BLE001
                log.debug("Giveaway-View #%s nicht anmeldbar", g.get("id"), exc_info=True)
        if n:
            log.info("%d Giveaway-Knopf/Knöpfe wieder angemeldet.", n)
        return n

    def _protect(self, msg, *, slot="panel"):
        """Neues Panel schuetzen und das VORIGE freigeben.

        Der Auto-Loesch-Schutz in bot.py kannte bisher nur den Weg hinein: jedes
        neue Panel trug seine ID ein, niemand trug je eine aus. Das Set wuchs
        damit die ganze Laufzeit, und die alten Panels blieben in den
        Aufraeum-Kanaelen fuer immer stehen. Es kann ohnehin nur EINS je Slot
        aktuell sein - also gibt das neue das alte frei."""
        if msg is None:
            return
        try:
            import bot
            alt = self._geschuetzt.get(slot)
            if alt is not None and alt != msg:
                bot.release_message(alt)
            self._geschuetzt[slot] = msg
            bot.protect_message(msg)
        except Exception:  # noqa: BLE001
            pass


# --- Mitmach-Knopf (ueberlebt Neustarts dank custom_id) ----------------------
class _JoinButton(discord.ui.Button):
    def __init__(self, gid):
        super().__init__(label="Mitmachen", emoji="🎉",
                         style=discord.ButtonStyle.success,
                         custom_id=f"flo:gw:{int(gid)}")
        self.gid = int(gid)

    async def callback(self, interaction):
        await instance.join(interaction, self.gid)


class JoinView(discord.ui.View):
    """Bleibt dauerhaft aktiv (timeout=None) - auch nach einem Bot-Neustart."""

    def __init__(self, gid):
        super().__init__(timeout=None)
        self.message = None
        self.add_item(_JoinButton(gid))


# --- Singleton + Modul-API ---------------------------------------------------
instance = Giveaway()

setup = instance.setup
is_enabled = instance.is_enabled
escrow_von = instance.escrow_von
handle = instance.handle
on_message_passive = instance.on_message_passive
tick = instance.tick
register_views = instance.register_views
parse_stake = instance.parse_stake
parse_duration = instance.parse_duration
dauer_text = instance.dauer_text
is_yes = instance.is_yes
is_no = instance.is_no
is_cancel = instance.is_cancel
