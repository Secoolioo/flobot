"""Flo Steal: der Coin-Raub (Heist) im Economy-System.

Befehl (nach 'Flo'):
- steal @wer   Versuch, jemandem Flo Coins zu klauen. Klappt in ~45% der
               Faelle - dann wandert ein Teil vom Konto des Opfers zu dir.
               Geht's schief, wirst du erwischt und zahlst Schmerzensgeld
               an dein Wunschopfer. Danach: 1 Stunde Cooldown.

Aliase fuers Kommando: steal, klau, klauen, raub, rauben, heist.

Alle Coins laufen ueber economy (ein Topf, ein Handelsbuch). Dieses Modul
haelt nur die Cooldowns je Raeuber in data/steal.json.

Design-Entscheidung: Bei einem Misserfolg geht die Strafe NICHT ins Nichts,
sondern als 'Schmerzensgeld' an das anvisierte Opfer - so lohnt es sich auch,
Ziel eines Raubversuchs zu sein, und der Coin-Topf bleibt konstant.
"""

import logging
import os
import random
import time

import discord

import ai
import economy
from store import JsonStore

log = logging.getLogger("dcbot.steal")

# Sentinel: steal hat selbst geantwortet -> bot.py schweigt. Wir nutzen zwar die
# Empfehlung (einfach das Embed zurueckgeben, bot.py sendet), halten das Sentinel
# aber wie die anderen Module bereit - falls wir doch mal selbst reply() rufen.
HANDLED = object()

# Befehlswoerter, auf die der Raub hoert.
_CMDS = ("steal", "klau", "klauen", "raub", "rauben", "heist")

# --- Balance-Stellschrauben (per .env feinjustierbar) ------------------------
DEFAULT_COOLDOWN = 3600          # Sekunden zwischen zwei Raubzuegen je Nutzer
DEFAULT_SUCCESS_CHANCE = 0.45    # ~45% Chance, dass der Coup gelingt
MIN_TARGET = 100                 # unter so vielen Coins ist "eh nix zu holen"
LOOT_MIN_PCT = 0.10              # Beute: mind. 10% ...
LOOT_MAX_PCT = 0.30             # ... bis max. 30% des Opfer-Kontostands
LOOT_CAP = 5000                  # aber nie mehr als so viel auf einen Schlag
PENALTY_MIN_PCT = 0.05           # Strafe: 5% ...
PENALTY_MAX_PCT = 0.15           # ... bis 15% des eigenen Kontostands ...
PENALTY_FLAT_MIN = 50            # ... mindestens aber flat 50 ...
PENALTY_FLAT_MAX = 200           # ... bis flat 200 Coins.

# --- Freche deutsche Flavor-Texte (random.choice) ----------------------------
_SUCCESS_LINES = [
    "🥷 **{raeuber}** schleicht sich an **{opfer}** heran und macht die Taschen leer!",
    "💰 Coup geglückt! **{raeuber}** zieht **{opfer}** gnadenlos ab.",
    "😈 Ein sauberer Griff - **{raeuber}** erleichtert **{opfer}** um ein hübsches Sümmchen.",
    "🏃 **{raeuber}** rennt lachend davon, die Coins von **{opfer}** im Sack.",
    "🎭 Meisterdieb-Modus: **{raeuber}** hat **{opfer}** ausgenommen wie eine Weihnachtsgans.",
    "🪝 Zack, weg! **{raeuber}** angelt sich die Coins von **{opfer}**.",
]
_FAIL_LINES = [
    "🚨 Erwischt! **{raeuber}** stolpert über den eigenen Fuß - **{opfer}** kassiert Schmerzensgeld.",
    "👮 **{raeuber}** wird auf frischer Tat geschnappt und muss **{opfer}** entschädigen.",
    "🤡 Peinlich: **{raeuber}** verwechselt die Tasche und zahlt drauf. Freut sich: **{opfer}**.",
    "💥 Alarm! Der Coup von **{raeuber}** fliegt auf - **{opfer}** lacht sich ins Fäustchen.",
    "🪤 In die Falle getappt! **{raeuber}** blecht Reue-Coins an **{opfer}**.",
    "🙈 **{raeuber}** löst versehentlich die Alarmanlage aus und muss **{opfer}** entschädigen.",
]
_SELF_LINES = [
    "🪞 Dich selbst beklauen? Deine linke Tasche klaut der rechten - lohnt nicht.",
    "🤨 Du willst DIR SELBST die Coins klauen? Das nennt man Sparen, Kollege.",
    "😅 Selbstbestehlung abgelehnt - du hast dich sofort selbst erwischt.",
]
_BOT_LINES = [
    "🤖 Bots haben keine Taschen - und ich passe auf meine Coins auf. Nix da.",
    "🚫 Einen Bot ausrauben? Süß. Ich habe die Kohle im Tresor, du kommst nicht ran.",
    "😏 Netter Versuch, aber an meinen Coins vergreift sich hier keiner.",
]
_POOR_LINES = [
    "🕸️ Bei **{opfer}** ist eh nix zu holen - blank wie eine geputzte Fensterscheibe.",
    "💸 **{opfer}** hat die Taschen leer. Da lohnt sich nicht mal das Anschleichen.",
    "🪹 **{opfer}** ist pleite - such dir ein fetteres Ziel.",
]


class Steal:
    """Objektorientierte Huelle: der veraenderliche Zustand lebt auf der Instanz."""

    def __init__(self):
        # Veraenderlicher Zustand (frueher Modul-Globals).
        self._enabled = False
        self._bot_name = "Flo"
        self._store = None
        self._cooldown = DEFAULT_COOLDOWN
        self._success_chance = DEFAULT_SUCCESS_CHANCE

    # --- Kleine Helfer ----------------------------------------------------
    def _env_int(self, key, fallback):
        """Liest einen Integer aus der .env, faellt bei Murks auf fallback zurueck."""
        try:
            return int(str(os.getenv(key, "")).strip())
        except (TypeError, ValueError):
            return fallback

    def _env_float(self, key, fallback):
        """Liest einen Float aus der .env, faellt bei Murks auf fallback zurueck."""
        try:
            return float(str(os.getenv(key, "")).strip().replace(",", "."))
        except (TypeError, ValueError):
            return fallback

    def fmt_coins(self, n):
        """1500 -> '1.500' (deutsche Tausenderpunkte)."""
        return f"{int(n):,}".replace(",", ".")

    # --- Lebenszyklus -----------------------------------------------------
    def setup(self):
        """Aktiviert den Raub. Braucht economy (dort liegt der Coin-Topf)."""
        self._bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
        if os.getenv("STEAL_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
            log.info("Steal-Feature aus (STEAL_ENABLED=0).")
            return False
        if not economy.is_enabled():
            log.info("Steal-Feature aus: economy ist nicht aktiv.")
            return False
        self._cooldown = self._env_int("STEAL_COOLDOWN", DEFAULT_COOLDOWN)
        self._success_chance = self._env_float("STEAL_SUCCESS_CHANCE", DEFAULT_SUCCESS_CHANCE)
        self._store = JsonStore("steal.json", default={"cooldowns": {}})
        self._enabled = True
        log.info("Steal-Feature aktiv (Cooldown %ds, Erfolgschance %.0f%%).",
                 self._cooldown, self._success_chance * 100)
        return True

    def is_enabled(self):
        return self._enabled

    # --- Cooldown-Verwaltung ----------------------------------------------
    def _cooldowns(self):
        """Liefert das Cooldown-Dict (uid-str -> letzter Raub-Timestamp)."""
        if self._store is None:
            return {}
        return self._store.data.setdefault("cooldowns", {})

    def _remaining(self, uid):
        """Restliche Cooldown-Sekunden fuer uid (0 = frei)."""
        last = self._cooldowns().get(str(uid), 0)
        try:
            last = float(last)
        except (TypeError, ValueError):
            last = 0
        rest = self._cooldown - (time.time() - last)
        return rest if rest > 0 else 0

    # --- Befehl -----------------------------------------------------------
    async def handle(self, message):
        """Erkennt 'steal @wer' (+ Aliase) und wickelt den Raub ab.

        Rueckgabe:
        - None            -> kein Steal-Befehl, naechster Handler / die KI ist dran
        - str             -> Hinweistext (Fehlbedienung, Cooldown, ...)
        - discord.Embed   -> das Ergebnis des Raubs (bot.py sendet es)
        """
        if not self._enabled or message.guild is None:
            return None

        cmd = ai.strip_lead(message.content or "")
        parts = cmd.split()
        if not parts or parts[0].lower().strip(".,;:!?") not in _CMDS:
            return None  # kein Raub gemeint - andere Handler / KI uebernehmen

        # economy koennte zur Laufzeit deaktiviert worden sein.
        if not economy.is_enabled():
            return "💤 Gerade gibt's keine Coins zu holen - das Economy-System schläft."

        autor = message.author

        # --- Ziel bestimmen: erste @-Mention, die nicht der Autor / kein Bot ist.
        mentions = list(getattr(message, "mentions", []) or [])
        ziele = [m for m in mentions if m.id != autor.id and not getattr(m, "bot", False)]
        if not ziele:
            # Selbst-Klau / Bot-Ziel bekommen einen eigenen frechen Text.
            if any(m.id == autor.id for m in mentions):
                return random.choice(_SELF_LINES)
            if any(getattr(m, "bot", False) for m in mentions):
                return random.choice(_BOT_LINES)
            return (f"🥷 So geht's: `{self._bot_name} steal @wer` - "
                    f"nenn mir ein Opfer mit @.")
        ziel = ziele[0]

        # --- Cooldown pruefen (verbraucht wird er erst bei echtem Raubversuch).
        rest = self._remaining(autor.id)
        if rest > 0:
            mins = max(1, int((rest + 59) // 60))
            return (f"⏳ Deine Finger sind noch heiß - warte noch **{mins} Min**, "
                    f"bevor du wieder zuschlägst.")

        # --- Ziel-Kontostand + Schutz fuer die Armen (kein Cooldown verbraucht).
        try:
            ziel_coins = economy.get_coins(ziel.id)
        except Exception:  # noqa: BLE001 - defensiv, economy soll nie den Raub sprengen
            log.exception("Konnte Kontostand des Ziels nicht lesen")
            ziel_coins = 0
        if ziel_coins < MIN_TARGET:
            return random.choice(_POOR_LINES).format(opfer=ziel.display_name)

        # --- Auslosung: Erfolg oder Erwischt? --------------------------------
        erfolg = random.random() < self._success_chance
        if erfolg:
            embed = await self._do_success(autor, ziel, ziel_coins)
        else:
            embed = await self._do_fail(autor, ziel)

        # --- Cooldown setzen + alles persistieren ----------------------------
        self._cooldowns()[str(autor.id)] = time.time()
        try:
            if self._store is not None:
                await self._store.save()
            await economy.flush()
        except Exception:  # noqa: BLE001 - Speichern darf den Raub nicht platzen lassen
            log.exception("Speichern nach dem Raub fehlgeschlagen")

        return embed

    # --- Ausgang: geglueckt ----------------------------------------------
    async def _do_success(self, autor, ziel, ziel_coins):
        """Beute berechnen, umbuchen und einen gruenen Erfolgs-Embed bauen."""
        anteil = random.uniform(LOOT_MIN_PCT, LOOT_MAX_PCT)
        beute = int(ziel_coins * anteil)
        beute = min(beute, LOOT_CAP, ziel_coins)  # nie mehr als Deckel/Kontostand
        if beute < 1:
            beute = 1
        try:
            economy.add_coins(ziel.id, -beute, reason="steal")
            economy.add_coins(autor.id, beute, reason="steal")
        except Exception:  # noqa: BLE001 - Umbuchung defensiv absichern
            log.exception("Coin-Umbuchung (Erfolg) fehlgeschlagen")
        text = random.choice(_SUCCESS_LINES).format(
            raeuber=autor.display_name, opfer=ziel.display_name)
        emb = discord.Embed(
            title="💰 Raub geglückt!",
            description=text,
            color=discord.Color.green())
        emb.add_field(name="Beute",
                      value=f"**+{self.fmt_coins(beute)}** {economy.COIN}", inline=True)
        emb.add_field(name="Opfer verliert",
                      value=f"**-{self.fmt_coins(beute)}** {economy.COIN}", inline=True)
        emb.set_footer(text=f"Nächster Coup in {self._cooldown // 60} Min möglich.")
        return emb

    # --- Ausgang: erwischt -----------------------------------------------
    async def _do_fail(self, autor, ziel):
        """Strafe berechnen, als Schmerzensgeld ans Opfer buchen, roter Embed."""
        try:
            eigen = economy.get_coins(autor.id)
        except Exception:  # noqa: BLE001
            log.exception("Konnte eigenen Kontostand nicht lesen")
            eigen = 0
        # Strafe = groesserer Wert aus (5-15% Konto) und (flat 50-200), aber nie
        # mehr, als der Raeuber ueberhaupt besitzt.
        prozent = int(eigen * random.uniform(PENALTY_MIN_PCT, PENALTY_MAX_PCT))
        flat = random.randint(PENALTY_FLAT_MIN, PENALTY_FLAT_MAX)
        strafe = min(eigen, max(prozent, flat))
        if strafe < 0:
            strafe = 0
        if strafe > 0:
            try:
                # Schmerzensgeld: geht vom Raeuber ans anvisierte Opfer.
                economy.add_coins(autor.id, -strafe, reason="steal")
                economy.add_coins(ziel.id, strafe, reason="steal")
            except Exception:  # noqa: BLE001
                log.exception("Coin-Umbuchung (Misserfolg) fehlgeschlagen")
        text = random.choice(_FAIL_LINES).format(
            raeuber=autor.display_name, opfer=ziel.display_name)
        emb = discord.Embed(
            title="🚨 Erwischt!",
            description=text,
            color=discord.Color.red())
        if strafe > 0:
            emb.add_field(name="Schmerzensgeld",
                          value=f"**-{self.fmt_coins(strafe)}** {economy.COIN} an "
                                f"{ziel.display_name}", inline=False)
        else:
            emb.add_field(name="Glück im Unglück",
                          value="Du bist blank - nicht mal Schmerzensgeld zu holen.",
                          inline=False)
        emb.set_footer(text=f"Nächster Versuch in {self._cooldown // 60} Min.")
        return emb


# --- Singleton + Modul-API ---------------------------------------------------
# Eine Instanz pro Prozess. bot.py nutzt setup()/is_enabled()/handle() wie bei
# jedem anderen Feature-Modul; HANDLED bleibt fuer den Fall, dass wir selbst senden.
instance = Steal()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
fmt_coins = instance.fmt_coins
