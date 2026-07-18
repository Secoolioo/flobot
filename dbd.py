"""Dead-by-Daylight-Wissen fuer Flo: Builds, Perks, Killer - alles.

Befehle (nach 'Flo'):
- build <killer|survivor>   Otzdarvas empfohlene Builds (z. B. 'build nurse',
                            'build survivor', 'build team', 'build profi')
- killer <name>             Killer-Steckbrief: Bio, eigene Perks, Builds, Guide
- perk <name>               Perk-Beschreibung (deutsch, echte Zahlen)
- dbd <frage>               freie Frage - die KI antwortet mit den ECHTEN
                            Spieldaten als Grundlage (kein Halluzinieren)
- dbd update                (nur Besitzer) Daten live neu einlesen

Datenbasis: dbd_data.json - alle Perks/Charaktere von der dbd.tricky.lol-API
(auf Deutsch) + Otzdarvas Build-Empfehlungen (otz-builds, wird automatisch
aus seinem oeffentlichen Sheet gebaut). 'flo dbd update' zieht alles frisch.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path

import discord

import ai
import dbd_fetch

log = logging.getLogger("dcbot.dbd")

OWNER_ID = int(os.getenv("OWNER_ID", "1040135855710404659") or "0")

_enabled: bool = False
_bot_name: str = "Flo"
_data: dict = {}

# Indizes (in _reindex() gebaut)
_killers: list[dict] = []          # {key, name_en, name_de, ...}
_alias_to_killer: dict[str, dict] = {}
_perk_index: dict[str, str] = {}   # norm(name de/en) -> perk-key
_meta_killer: list[str] = []       # meistgenutzte Perks in Otz-Killer-Builds
_meta_surv: list[str] = []

# Community-Spitznamen -> englischer Killer-Name.
_NICKNAMES = {
    "billy": "The Hillbilly", "bubba": "The Cannibal",
    "leatherface": "The Cannibal", "myers": "The Shape",
    "michael": "The Shape", "michael myers": "The Shape",
    "chucky": "The Good Guy", "springtrap": "The Animatronic",
    "fnaf": "The Animatronic", "wesker": "The Mastermind",
    "sadako": "The Onryō", "pinhead": "The Cenobite",
    "freddy": "The Nightmare", "pyramid head": "The Executioner",
    "ph": "The Executioner", "dracula": "The Dark Lord",
    "kaneki": "The Ghoul", "xeno": "The Xenomorph",
    "alien": "The Xenomorph", "sm": "The Skull Merchant",
    "hux": "The Singularity", "amanda": "The Pig",
    "slinger": "The Deathslinger", "demo": "The Demogorgon",
    "nemi": "The Nemesis", "vecna": "The Lich", "anna": "The Huntress",
    "doc": "The Doctor", "wraith": "The Wraith", "ghostface": "The Ghost Face",
}

# Survivor-Build-Sektionen bei Otz + deutsche Trigger dafuer.
_SURV_SECTIONS = {
    "Solo Survivors": ("solo", "survivor", "surv", "überlebende", "ueberlebende"),
    "Builds for Teams": ("team", "teams", "swf"),
    "Advanced Builds": ("profi", "advanced", "fortgeschritten"),
}

_SCHWIERIGKEIT = {"easy": "leicht", "intermediate": "mittel", "hard": "schwer",
                  "very hard": "sehr schwer"}


def _norm(s: str) -> str:
    """klein, Akzente weg (Onryō -> onryo), nur Buchstaben/Zahlen/Leerzeichen."""
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def setup() -> bool:
    """Laedt dbd_data.json und baut die Such-Indizes."""
    global _enabled, _bot_name, _data
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    if os.getenv("DBD_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        log.info("DbD-Feature aus (DBD_ENABLED=0).")
        return False
    try:
        _data = json.loads(dbd_fetch.DATA_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        log.exception("dbd_data.json fehlt/kaputt - DbD-Feature aus")
        return False
    _reindex()
    _enabled = True
    log.info("DbD-Feature aktiv (%d Perks, %d Killer, %d Otz-Eintraege).",
             len(_data.get("perks", {})), len(_killers), len(_data.get("otz", {})))
    return True


def is_enabled() -> bool:
    return _enabled


def _reindex() -> None:
    """Baut alle Suchstrukturen aus _data neu."""
    global _killers, _alias_to_killer, _perk_index, _meta_killer, _meta_surv
    chars = _data.get("chars", {})
    otz = _data.get("otz", {})
    guides = _data.get("guides", {})
    otz_by_norm = {_norm(k): v for k, v in otz.items()}
    guide_by_norm = {_norm(k): v for k, v in guides.items()}

    _killers = []
    _alias_to_killer = {}
    for key, c in chars.items():
        if c.get("role") != "killer":
            continue
        k = dict(c)
        k["key"] = key
        k["builds"] = otz_by_norm.get(_norm(c["name_en"]), [])
        k["guide"] = guide_by_norm.get(_norm(c["name_en"]), "")
        _killers.append(k)
        for alias in {_norm(c["name_en"]),
                      _norm(re.sub(r"^The ", "", c["name_en"])),
                      _norm(c["name_de"]),
                      _norm(re.sub(r"^(Der|Die|Das) ", "", c["name_de"]))}:
            if alias:
                _alias_to_killer[alias] = k
    for nick, en_name in _NICKNAMES.items():
        ziel = next((k for k in _killers if k["name_en"] == en_name), None)
        if ziel is not None:
            _alias_to_killer[_norm(nick)] = ziel

    _perk_index = {}
    for key, p in _data.get("perks", {}).items():
        _perk_index[_norm(p["name_en"])] = key
        _perk_index[_norm(p["name_de"])] = key

    # Meta = die haeufigsten Perks in Otzdarvas aktuellen Builds.
    kc: Counter = Counter()
    sc: Counter = Counter()
    for name, builds in otz.items():
        ziel = sc if name in _SURV_SECTIONS else kc
        for b in builds:
            ziel.update(b.get("perks", []))
    _meta_killer = [p for p, _n in kc.most_common(14)]
    _meta_surv = [p for p, _n in sc.most_common(14)]


# --- Suche -----------------------------------------------------------------
def _find_killer(text: str) -> "dict | None":
    t = _norm(text)
    if not t:
        return None
    if t in _alias_to_killer:
        return _alias_to_killer[t]
    # Wort-Treffer: 'nurse build bitte' -> nurse
    woerter = set(t.split())
    for alias, k in _alias_to_killer.items():
        if " " not in alias and alias in woerter:
            return k
    for alias, k in _alias_to_killer.items():
        if " " in alias and alias in t:
            return k
    return None


def _find_perk(text: str) -> "str | None":
    t = _norm(text)
    if not t:
        return None
    if t in _perk_index:
        return _perk_index[t]
    # Teil-Treffer: eindeutig beginnend oder enthaltend
    treffer = [k for n, k in _perk_index.items() if n.startswith(t)]
    if len(set(treffer)) == 1:
        return treffer[0]
    treffer = [k for n, k in _perk_index.items() if t in n]
    return treffer[0] if len(set(treffer)) == 1 else None


def _perk_de(name_en: str) -> str:
    """Englischer Otz-Perkname -> deutscher Name (falls bekannt)."""
    key = _perk_index.get(_norm(name_en))
    if key:
        return _data["perks"][key]["name_de"]
    return name_en


def _surv_section(text: str) -> "str | None":
    t = _norm(text)
    for sektion, trigger in _SURV_SECTIONS.items():
        if any(w in t.split() or w == t for w in trigger):
            return sektion
    return None


# --- Embeds ----------------------------------------------------------------
_C_DBD = discord.Color.from_str("#8b0000")


def _builds_embed_killer(k: dict) -> discord.Embed:
    emb = discord.Embed(
        title=f"🔪 {k['name_de']} – Builds",
        description=f"Empfohlen von **Otzdarva** (live aus seinem Sheet).",
        color=_C_DBD)
    for b in k["builds"][:4]:
        perks = "\n".join(f"• {_perk_de(p)}" for p in b["perks"])
        emb.add_field(name=f"🧩 {b['name']}", value=perks or "—", inline=True)
    if not k["builds"]:
        emb.description = ("Für diesen Killer hat Otzdarva noch keine Builds "
                           "veröffentlicht (zu neu).")
    if k.get("guide"):
        emb.add_field(name="🎬 Guide", value=k["guide"], inline=False)
    emb.set_footer(text=f"{_bot_name} killer {k['name_en'].removeprefix('The ').lower()}"
                        f" · {_bot_name} dbd <frage>")
    return emb


def _builds_embed_surv(sektion: str) -> discord.Embed:
    titel = {"Solo Survivors": "🏃 Survivor-Builds (Solo)",
             "Builds for Teams": "👥 Survivor-Builds (Team/SWF)",
             "Advanced Builds": "🎓 Survivor-Builds (Fortgeschritten)"}[sektion]
    emb = discord.Embed(title=titel,
                        description="Empfohlen von **Otzdarva**.", color=_C_DBD)
    for b in _data["otz"].get(sektion, [])[:4]:
        perks = "\n".join(f"• {_perk_de(p)}" for p in b["perks"])
        emb.add_field(name=f"🧩 {b['name']}", value=perks or "—", inline=True)
    emb.set_footer(text=f"auch: {_bot_name} build team · {_bot_name} build profi")
    return emb


def _killer_embed(k: dict) -> discord.Embed:
    emb = discord.Embed(
        title=f"🔪 {k['name_de']}  ({k['name_en']})",
        description=k.get("bio") or "—", color=_C_DBD)
    schwer = _SCHWIERIGKEIT.get(k.get("schwierigkeit", ""), k.get("schwierigkeit", "?"))
    emb.add_field(name="Schwierigkeit", value=schwer, inline=True)
    eigene = [p["name_de"] for p in _data["perks"].values()
              if p.get("role") == "killer" and str(p.get("character")) == k["key"]]
    if eigene:
        emb.add_field(name="Eigene Perks", value="\n".join(f"• {n}" for n in eigene[:3]),
                      inline=True)
    if k["builds"]:
        b = k["builds"][0]
        emb.add_field(name=f"Otz-Build: {b['name']}",
                      value=" · ".join(_perk_de(p) for p in b["perks"]), inline=False)
        emb.set_footer(text=f"Alle Builds: {_bot_name} build "
                            f"{k['name_en'].removeprefix('The ').lower()}")
    if k.get("guide"):
        emb.add_field(name="🎬 Guide", value=k["guide"], inline=False)
    return emb


def _perk_embed(key: str) -> discord.Embed:
    p = _data["perks"][key]
    rolle = "Killer" if p.get("role") == "killer" else "Survivor"
    emb = discord.Embed(
        title=f"🧩 {p['name_de']}",
        description=p.get("beschreibung") or "—", color=_C_DBD)
    if p["name_en"] != p["name_de"]:
        emb.add_field(name="Englisch", value=p["name_en"], inline=True)
    emb.add_field(name="Rolle", value=rolle, inline=True)
    besitzer = _data["chars"].get(str(p.get("character")))
    if besitzer:
        emb.add_field(name="Lehrbar von", value=besitzer["name_de"], inline=True)
    return emb


def _help_embed() -> discord.Embed:
    n = _bot_name
    emb = discord.Embed(
        title="🔪 Dead by Daylight",
        description=(f"`{n} build nurse` – Otz-Builds für einen Killer\n"
                     f"`{n} build survivor` / `team` / `profi` – Survivor-Builds\n"
                     f"`{n} killer wesker` – Steckbrief + Guide\n"
                     f"`{n} perk adrenalin` – was macht der Perk?\n"
                     f"`{n} dbd <frage>` – frag einfach irgendwas 🧠"),
        color=_C_DBD)
    emb.set_footer(text="Daten: Otzdarva-Builds + Community-API · "
                        f"{len(_data.get('perks', {}))} Perks, {len(_killers)} Killer")
    return emb


# --- Freie Fragen (KI mit echten Daten) ------------------------------------
_PROGRESSION = (
    "Fortschritt/Grind-Basics: Blutpunkte (BP) farmt man am besten ueber "
    "aktives Spielen (Jagden/Rettungen/Gens); als Killer bringen volle "
    "Kategorien am meisten. Prestige 1 schaltet Tier-1-Versionen der eigenen "
    "Perks fuer ALLE Charaktere frei, Prestige 2 Tier 2, Prestige 3 Tier 3 - "
    "wichtige Lehrmeister-Charaktere zuerst auf P3 bringen lohnt sich. "
    "Anfaenger-Killer: Wraith, Trapper, Legion. Starke Einsteiger-Perks gibt "
    "es oft im Schrein der Geheimnisse."
)


def _kontext_fuer(frage: str) -> str:
    """RAG-Kontext: erkannte Killer/Perks + Otz-Meta zur Frage zusammenstellen."""
    teile: list[str] = []
    k = _find_killer(frage)
    if k is not None:
        info = [f"KILLER: {k['name_de']} ({k['name_en']}), "
                f"Schwierigkeit {k.get('schwierigkeit', '?')}. {k.get('bio', '')}"]
        for b in k["builds"][:3]:
            info.append(f"Otz-Build '{b['name']}': " + ", ".join(b["perks"]))
        if k.get("guide"):
            info.append(f"Guide: {k['guide']}")
        teile.append("\n".join(info))
    # bis zu 4 erwaehnte Perks aufloesen (2-3-Wort-Fenster ueber der Frage)
    woerter = _norm(frage).split()
    gefunden: list[str] = []
    for laenge in (3, 2, 1):
        for i in range(len(woerter) - laenge + 1):
            kand = " ".join(woerter[i:i + laenge])
            key = _perk_index.get(kand)
            if key and key not in gefunden:
                gefunden.append(key)
    for key in gefunden[:4]:
        p = _data["perks"][key]
        teile.append(f"PERK {p['name_de']} ({p['name_en']}, {p['role']}): "
                     f"{p['beschreibung'][:400]}")
    teile.append("Aktuelle Killer-Meta laut Otzdarvas Builds: "
                 + ", ".join(_meta_killer))
    teile.append("Aktuelle Survivor-Meta laut Otzdarvas Builds: "
                 + ", ".join(_meta_surv))
    teile.append(_PROGRESSION)
    return "\n\n".join(teile)


async def _frage(message: discord.Message, frage: str) -> str:
    if not ai.is_enabled():
        return (f"Die KI ist gerade aus - nutz `{_bot_name} build <killer>` "
                f"oder `{_bot_name} perk <name>`.")
    system = (
        "Du bist Flo, ein Dead-by-Daylight-Experte in einem deutschen Discord. "
        "Antworte kurz und konkret auf Deutsch (max. ~10 Saetze), gerne mit "
        "einer klaren Empfehlung. Nutze VORRANGIG die folgenden aktuellen "
        "Spieldaten (Otzdarva-Builds, Perk-Beschreibungen); erfinde keine "
        "Perks oder Werte. Wenn du etwas nicht sicher weisst, sag das.\n\n"
        "=== AKTUELLE DATEN ===\n" + _kontext_fuer(frage))
    antwort = await ai.generate(frage, system=system, temperature=0.5,
                                max_tokens=600)
    return antwort or "Da fällt mir gerade nichts Gescheites ein - frag nochmal."


async def _update(message: discord.Message) -> str:
    """(Owner) Daten live neu einlesen: API + Otz-Builds."""
    global _data
    if message.author.id != OWNER_ID:
        return "Das Update darf nur mein Besitzer anstoßen. 😉"
    try:
        neu = await dbd_fetch.fetch_all()
    except Exception:  # noqa: BLE001
        log.exception("DbD-Update fehlgeschlagen")
        return "Update fehlgeschlagen (Quelle nicht erreichbar) - alte Daten bleiben."
    # Guides behalten, falls die Tabelle mal leer sein sollte
    if not neu.get("guides"):
        neu["guides"] = _data.get("guides", {})
    _data = neu
    dbd_fetch.save(neu)
    _reindex()
    return (f"✅ DbD-Daten aktualisiert: {len(neu['perks'])} Perks, "
            f"{sum(1 for c in neu['chars'].values() if c['role'] == 'killer')} Killer, "
            f"{len(neu['otz'])} Otz-Eintraege.")


# --- Befehls-Einstieg ------------------------------------------------------
async def handle(message: discord.Message) -> "str | discord.Embed | None":
    if not _enabled or message.guild is None:
        return None
    cmd = ai.strip_lead(message.content or "")
    if not cmd:
        return None
    parts = cmd.split(maxsplit=1)
    first = parts[0].lower().strip(".,;:!?")
    rest = parts[1].strip() if len(parts) > 1 else ""

    if first == "dbd":
        if not rest:
            return _help_embed()
        if rest.lower() == "update":
            return await _update(message)
        return await _frage(message, rest)

    if first in ("build", "builds"):
        if not rest:
            return _help_embed()
        sektion = _surv_section(rest)
        if sektion:
            return _builds_embed_surv(sektion)
        k = _find_killer(rest)
        if k is not None:
            return _builds_embed_killer(k)
        # kein Treffer -> vielleicht meint er es allgemein: KI fragen
        return await _frage(message, f"Welchen Build empfiehlst du für: {rest}?")

    if first in ("perk", "perks"):
        if not rest:
            return f"Welcher Perk? z. B. `{_bot_name} perk adrenalin`"
        key = _find_perk(rest)
        if key:
            return _perk_embed(key)
        return await _frage(message, f"Was macht der DbD-Perk '{rest}'?")

    if first == "killer":
        if not rest:
            namen = ", ".join(k["name_de"].removeprefix("Der ").removeprefix("Die ")
                              .removeprefix("Das ") for k in _killers)
            emb = discord.Embed(title="🔪 Alle Killer", description=namen[:4000],
                                color=_C_DBD)
            emb.set_footer(text=f"{_bot_name} killer <name> für Details")
            return emb
        k = _find_killer(rest)
        if k is not None:
            return _killer_embed(k)
        return f"Den Killer `{rest}` kenne ich nicht - `{_bot_name} killer` zeigt alle."

    if first in ("survivor", "surv") and (not rest or _surv_section(rest or "solo")):
        return _builds_embed_surv(_surv_section(rest or "solo") or "Solo Survivors")

    return None
