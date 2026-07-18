"""Dead-by-Daylight-Daten einsammeln -> dbd_data.json.

Quellen:
- dbd.tricky.lol Community-API: alle Perks + Charaktere (DE und EN)
- Otzdarvas Build-Empfehlungen: mrtipson.github.io/otz-builds (statische Seite,
  wird automatisch aus Otzdarvas oeffentlichem Google-Sheet gebaut)

Nutzung:
    python dbd_fetch.py          # schreibt dbd_data.json neu
Das dbd-Modul nutzt dieselben Funktionen fuer 'flo dbd update' (Owner).
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
from pathlib import Path

API = "https://dbd.tricky.lol/api"
OTZ_URL = "https://mrtipson.github.io/otz-builds/"
DATA_FILE = Path(__file__).resolve().parent / "dbd_data.json"

_UA = {"User-Agent": "FloBot/1.0 (Discord-Bot, DbD-Feature)"}


# --- Text-Aufbereitung -----------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_TUNABLE_RE = re.compile(r"\{Tunable\.[^.}]+\.([A-Za-z0-9_%]+)\}")
_KEYWORD_RE = re.compile(r"\{Keyword\.([A-Za-z0-9_]+)\}")


def clean_desc(desc: str, tunables: dict | None) -> str:
    """HTML raus, {Tunable...}-Platzhalter durch die echten Zahlen ersetzen
    (bei 3 Stufen: 'x/y/z'), {Keyword...} durch den Namen."""
    tunables = {k.lower(): v for k, v in (tunables or {}).items()}

    def tun(m: re.Match) -> str:
        werte = tunables.get(m.group(1).lower())
        if not werte:
            return "?"
        werte = [f"{v:g}" if isinstance(v, (int, float)) else str(v) for v in werte]
        return werte[0] if len(set(werte)) == 1 else "/".join(werte)

    s = _TUNABLE_RE.sub(tun, desc or "")
    s = _KEYWORD_RE.sub(lambda m: m.group(1), s)
    s = s.replace("<li>", " • ").replace("</li>", "")
    s = s.replace("<br><br>", "\n").replace("<br>", "\n")
    s = _TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    return re.sub(r"[ \t]+", " ", s).strip()


# --- API-Fetches -----------------------------------------------------------
async def _get_json(session, url: str):
    async with session.get(url, headers=_UA) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _get_text(session, url: str) -> str:
    async with session.get(url, headers=_UA) as resp:
        resp.raise_for_status()
        return await resp.text()


async def fetch_perks(session) -> dict:
    """Perks beider Rollen, DE + EN. Schluessel = API-Key (z. B. 'Adrenaline')."""
    de = await _get_json(session, f"{API}/perks?locale=de")
    en = await _get_json(session, f"{API}/perks")
    out = {}
    for key, p_en in en.items():
        p_de = de.get(key, {})
        out[key] = {
            "name_en": p_en.get("name", key),
            "name_de": p_de.get("name") or p_en.get("name", key),
            "role": p_en.get("role", ""),
            "character": p_en.get("character"),
            "beschreibung": clean_desc(p_de.get("description") or
                                       p_en.get("description", ""),
                                       p_de.get("tunables") or p_en.get("tunables")),
        }
    return out


async def fetch_chars(session) -> dict:
    """Charaktere (Killer + Survivor), DE-Bio + EN-Name. Schluessel = Charakter-Nr."""
    de = await _get_json(session, f"{API}/characters?locale=de")
    en = await _get_json(session, f"{API}/characters")
    out = {}
    for cid, c_en in en.items():
        c_de = de.get(cid, {})
        out[str(c_en.get("charindex", cid))] = {
            "name_en": c_en.get("name", ""),
            "name_de": c_de.get("name") or c_en.get("name", ""),
            "role": c_en.get("role", ""),
            "schwierigkeit": c_en.get("difficulty", ""),
            "item": c_en.get("item") or "",     # Power-ID -> verknuepft Addons
            "bio": clean_desc(c_de.get("bio") or c_en.get("bio", ""), None)[:600],
        }
    return out


async def fetch_addons(session) -> dict:
    """Alle Addons (Killer-Power + Survivor-Items), DE + EN."""
    de = await _get_json(session, f"{API}/addons?locale=de")
    en = await _get_json(session, f"{API}/addons")
    out = {}
    for key, a_en in en.items():
        a_de = de.get(key, {})
        out[key] = {
            "name_en": a_en.get("name", key),
            "name_de": a_de.get("name") or a_en.get("name", key),
            "role": a_en.get("role", ""),
            "rarity": a_en.get("rarity", ""),
            "parents": a_en.get("parents") or [],
            "item_type": a_en.get("item_type") or "",
            "beschreibung": clean_desc(a_de.get("description") or
                                       a_en.get("description", ""), None)[:400],
        }
    return out


# --- Otzdarva-Builds parsen -------------------------------------------------
def parse_otz(html: str) -> dict:
    """Zieht alle Builds aus der otz-builds-Seite.
    Rueckgabe: {charakter_name: [{"name": ..., "perks": [...], "alt": {perk: [...]}}]}"""
    out: dict[str, list] = {}
    for cm in re.finditer(
            r'<div class="character" id="([^"]+)"(.*?)(?=<div class="character" id=|$)',
            html, re.S):
        char_name, block = cm.group(1), cm.group(2)
        builds = []
        for bm in re.finditer(
                r'<div class="build">\s*<div class="buildName">(.*?)</div>(.*?)(?=<div class="build">|</div>\s*</div>\s*</div>|$)',
                block, re.S):
            bname = html_mod.unescape(bm.group(1)).strip()
            perks = []
            alts: dict[str, list] = {}
            for pm in re.finditer(r'<img class="perk" title="([^"]+)"[^>]*?(?:data-altPerks="([^"]*)")?\s*loading=',
                                  bm.group(2)):
                pname = html_mod.unescape(pm.group(1)).strip()
                perks.append(pname)
            if bname and perks:
                builds.append({"name": bname, "perks": perks[:4], "alt": alts})
        if builds:
            out[char_name] = builds
    return out


# Killer-Guides von otzdarva.com/dbd/killer-guides (jeweils der Top-Link;
# von der Live-Seite extrahiert - Seite aendert sich selten, daher gepflegt).
GUIDES = {
    "The Trapper": "https://youtu.be/Si249J2ngh0",
    "The Hillbilly": "https://youtu.be/buJQNLmP1AA",
    "The Nurse": "https://youtu.be/XPQb8B7pMIM",
    "The Huntress": "https://youtu.be/IQRDijaX5Io",
    "The Shape": "https://steamcommunity.com/sharedfiles/filedetails/?id=3712449059",
    "The Hag": "https://youtu.be/AX5AGQiNKg0",
    "The Doctor": "https://www.youtube.com/watch?v=v9nuFCWBD_o",
    "The Cannibal": "https://youtu.be/qgjvKM9QlHM",
    "The Nightmare": "https://docs.google.com/document/d/1kY9wbd0MSZ6c5y7mXsT_Xw5BbLRJXbWCE12n0YbOigA",
    "The Pig": "https://youtu.be/-ZLb3wbc-EQ",
    "The Clown": "https://docs.google.com/document/d/1umXUJy-If1bzdN-Mw3LIr12YYzVFkRv10R-6akE1v5o",
    "The Spirit": "https://bit.ly/4iAiDHE",
    "The Legion": "https://youtu.be/TapiizxKvyk",
    "The Plague": "https://youtu.be/Bmx6Cb_4Gmc",
    "The Ghost Face": "https://youtu.be/LIrc4Tn5KHU",
    "The Demogorgon": "https://www.youtube.com/watch?v=8RfEmtu9hIU",
    "The Oni": "https://youtu.be/5FTHQnFWeSE",
    "The Deathslinger": "https://youtu.be/WX_3Yowf53c",
    "The Executioner": "https://www.youtube.com/watch?v=4i-OW9lT9yE",
    "The Blight": "https://youtu.be/9sp_0PCdSOo",
    "The Twins": "https://youtu.be/ZE3CuWn5LeU",
    "The Nemesis": "https://youtu.be/-GxLEtbGPns",
    "The Cenobite": "https://www.youtube.com/watch?v=dv4gC6PIRXM",
    "The Artist": "https://youtu.be/u24RF10J-ao",
    "The Onryō": "https://docs.google.com/document/d/1odRx2ge-ix5yZkb8yZuMeb3Ax3Vgm4StPYL_HtR0zRs",
    "The Dredge": "https://docs.google.com/document/d/1oVp9Rz-7Vi_252ufpAaTPFy-5YcvCkSi9l5rMZNJ82k",
    "The Mastermind": "https://www.youtube.com/watch?v=YH4OpnhGtIE",
    "The Knight": "https://www.youtube.com/watch?v=kS9LNIQLFeo",
    "The Singularity": "https://youtu.be/zJkVPZMTj5w",
    "The Xenomorph": "https://docs.google.com/document/d/1-lG_Ow-OAE6kavsj-HEhh489_e-rIkrB4A43oHJL2wI",
    "The Unknown": "https://youtu.be/Mr9crO-Y9-c",
    "The Lich": "https://youtu.be/SPSt0t5MoQE",
    "The Dark Lord": "https://www.youtube.com/watch?v=nVBuYlwbpFc",
    "The Animatronic": "https://docs.google.com/document/d/1XcUusQV2QIsQowEAWRTZIaF-AUQxhKYNeRIZd4tT0EY",
    "The Krasue": "https://docs.google.com/document/d/1Covn0kPf4XJ-kRTJs4Jyd1SAXF0sM1iLdvc8vEsAovc",
    "_allgemein": "https://youtu.be/xHFHiLcCmTk",
}


async def fetch_all() -> dict:
    """Alles einsammeln. Wirft bei Netzwerkfehlern (Aufrufer faengt ab)."""
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        perks, chars, addons, otz_html = await asyncio.gather(
            fetch_perks(session), fetch_chars(session), fetch_addons(session),
            _get_text(session, OTZ_URL))
    return {
        "perks": perks,
        "chars": chars,
        "addons": addons,
        "otz": parse_otz(otz_html),
        "guides": GUIDES,
    }


def save(data: dict) -> None:
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DATA_FILE)


if __name__ == "__main__":
    daten = asyncio.run(fetch_all())
    save(daten)
    print(f"OK: {len(daten['perks'])} Perks, {len(daten['chars'])} Charaktere, "
          f"{len(daten['otz'])} Otz-Eintraege -> {DATA_FILE.name}")
