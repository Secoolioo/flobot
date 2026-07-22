"""Terraria-Wiki-Feature fuer Flo.

Flo kennt das komplette Terraria-Wiki und beantwortet JEDE Terraria-Frage mit
echten Wiki-Daten + Bildern. Datenquelle ist das offizielle Terraria-Wiki ueber
die MediaWiki-API (https://terraria.wiki.gg/api.php) - komplett kostenlos, ohne
API-Key und ohne Browser.

Bedienung:
- ``Flo terraria <thema/frage>``   -> Wiki-Antwort mit Bild.
- Aliasse: ``terra``, ``twiki``, ``terrariawiki``.
- Ohne Thema kommt ein Hinweis samt zufaelligem Terraria-Fakt.

Zusaetzlich (fuer den KI-Fallback in bot.py, analog zum frueheren DBD-Feature):
- ``erkennt_frage(content)``  -> erkennt Terraria-Fragen OHNE explizites Prefix.
- ``beantworte(message, frage)`` -> beantwortet eine freie Frage mit Wiki-Kontext.

Ist die KI (ai) aktiv, wird der Wiki-Text als Kontext benutzt (RAG): Flo
beantwortet die konkrete Frage kurz & frech auf Deutsch und erfindet nichts -
der rohe Wiki-Auszug bleibt als Beleg-Feld erhalten. Ist die KI aus, zeigt Flo
einfach den gekuerzten Wiki-Auszug.
"""

import asyncio
import logging
import random
import re

import aiohttp
import discord

import ai

log = logging.getLogger("dcbot.terraria")

# --- Terraria-Wiki (MediaWiki-API) ----------------------------------------
BASIS = "https://terraria.wiki.gg/api.php"
# Die wiki.gg-Server verlangen einen aussagekraeftigen User-Agent, sonst 403.
_HEADERS = {"User-Agent": "FloBot/1.0 (Discord)"}
_TIMEOUT = aiohttp.ClientTimeout(total=12)

# Terraria-Gruen fuer die Embeds.
_FARBE = 0x8DB360


class Terraria:
    """Terraria-Wiki-Nachschlagewerk als Objekt gekapselt."""

    # bot.py erkennt das: das Modul hat selbst geantwortet.
    HANDLED = object()

    # Prefix-Befehle, auf die das Feature reagiert (erstes Wort nach 'Flo').
    _PREFIXE = {"terraria", "terra", "twiki", "terrariawiki"}

    # ~65 markante Terraria-Begriffe fuer die prefixlose Erkennung (erkennt_frage).
    # Alles klein - gecheckt wird wortweise (Einzelwoerter) bzw. als Teilstring
    # (Mehrwort-Begriffe wie 'wall of flesh').
    _TERRA_KEYWORDS = {
        # Bosse
        "eye of cthulhu", "brain of cthulhu", "eater of worlds", "wall of flesh",
        "moon lord", "queen bee", "duke fishron", "empress of light", "king slime",
        "queen slime", "lunatic cultist", "skeletron", "plantera", "cthulhu",
        "deerclops", "golem", "destroyer", "twins", "mourning wood",
        # Biome / Orte
        "corruption", "crimson", "hallow", "jungle", "dungeon", "underworld",
        "hell", "ocean", "snow", "desert", "meteorite", "biome",
        # Items / Mechaniken / Erze / Tools
        "hardmode", "expert mode", "master mode", "calamity", "npc", "pickaxe",
        "hamaxe", "molten", "hellstone", "wing", "grappling hook", "mana", "boss",
        "ore", "demonite", "crimtane", "chlorophyte", "luminite", "zenith",
        "terra blade", "terrablade", "prisma", "terraprisma", "meowmere",
        "adamantite", "orichalcum", "mythril", "titanium", "cobalt", "palladium",
        "hellevator", "hoik", "shimmer", "goblin", "slime", "wof", "eoc",
    }

    # Untermenge der WIRKLICH eindeutigen Terraria-Begriffe: ein einziger Treffer
    # reicht dann fuer True. Alle anderen (mehrdeutigen wie 'hell', 'boss', 'ore',
    # 'snow') sind schwach - da braucht es mindestens zwei, um Fehlalarme bei
    # Alltagssaetzen (Wetter/Essen/Mathe) zu vermeiden.
    _STARKE_KEYWORDS = {
        "eye of cthulhu", "brain of cthulhu", "eater of worlds", "wall of flesh",
        "moon lord", "queen bee", "duke fishron", "empress of light", "king slime",
        "queen slime", "lunatic cultist", "skeletron", "plantera", "cthulhu",
        "deerclops", "golem", "hardmode", "calamity", "hellstone", "demonite",
        "crimtane", "chlorophyte", "luminite", "zenith", "terra blade", "terrablade",
        "terraprisma", "meowmere", "adamantite", "orichalcum", "hellevator", "hoik",
    }

    # Zufalls-Fakten fuer den Hinweis, wenn jemand nur 'Flo terraria' schreibt.
    _ZUFALLS_TIPPS = (
        "Der Moon Lord ist der finale Boss von Terraria - und ja, das ist Cthulhus Bruder.",
        "Das Zenith ist das staerkste Schwert und wird aus fast allen anderen Schwertern geschmiedet.",
        "Mit einem Hellevator (Schacht bis zur Unterwelt) sparst du dir laestiges Graben.",
        "Der Wall of Flesh laesst die Welt in den Hardmode kippen - danach ist nichts mehr wie vorher.",
        "Die Empress of Light killt dich tagsueber mit einem Schlag - nachts ist sie 'nur' knallhart.",
        "Chlorophyte-Erz waechst im Untergrund-Dschungel von selbst nach.",
        "Die Terra Blade schiesst Projektile und ist ein Klassiker unter den Schwertern.",
        "Der Dungeon-Waechter kommt, wenn du VOR Skeletron zu tief in den Dungeon gehst - viel Spass.",
    )

    def __init__(self):
        self._enabled = False
        self._bot_name = "Flo"

    def setup(self):
        """Aktiviert das Terraria-Wiki-Feature (braucht nur Internet).

        Die KI ist optional: ohne sie zeigt Flo den rohen Wiki-Auszug, mit ihr
        beantwortet Flo die konkrete Frage auf Basis der Wiki-Fakten (RAG)."""
        self._bot_name = ai.bot_name()
        self._enabled = True
        log.info("Terraria-Wiki-Feature aktiv (KI-Kontext: %s).",
                 "ja" if ai.is_enabled() else "nein")
        return self._enabled

    def is_enabled(self):
        return self._enabled

    # --- reine Helfer (ohne Netz testbar) ------------------------------------
    def _kuerzen(self, text, limit=1500):
        """Kuerzt Text moeglichst an einer Satzgrenze auf 'limit' Zeichen.

        Bevorzugt wird ein sauberer Satz-Abschluss (.!?), sonst das letzte
        Leerzeichen; abgeschnittener Text bekommt ein ' …' angehaengt."""
        text = re.sub(r"\s+", " ", text or "").strip()
        if len(text) <= limit:
            return text
        ausschnitt = text[:limit]
        # Letzte Satzgrenze im erlaubten Bereich suchen.
        grenzen = list(re.finditer(r"[.!?](?:\s|$)", ausschnitt))
        if grenzen and grenzen[-1].end() >= limit * 0.5:
            return ausschnitt[:grenzen[-1].end()].strip()
        # Sonst am letzten Leerzeichen trennen und Kuerzung markieren.
        cut = ausschnitt.rfind(" ")
        if cut > 0:
            ausschnitt = ausschnitt[:cut]
        return ausschnitt.rstrip() + " …"

    def _beste_seite(self, search_json):
        """Zieht den Titel der besten Trefferseite aus einem MediaWiki-Such-JSON.

        Versteht BEIDE Formate:
        - list=search:  {'query': {'search': [{'title': ...}, ...]}}
        - opensearch:   [suchwort, [titel...], [beschr...], [url...]]
        Rueckgabe: Titel (str) oder None."""
        if not search_json:
            return None
        # opensearch: Liste, deren zweites Element die Titel-Liste ist.
        if isinstance(search_json, list):
            if len(search_json) >= 2 and isinstance(search_json[1], list) and search_json[1]:
                return search_json[1][0]
            return None
        if isinstance(search_json, dict):
            treffer = (search_json.get("query") or {}).get("search") or []
            for t in treffer:
                titel = (t or {}).get("title") if isinstance(t, dict) else None
                if titel:
                    return titel
        return None

    def erkennt_frage(self, content):
        """True, wenn der Text nach einer Terraria-Frage klingt - OHNE dass 'Flo
        terraria' davorstehen muss (fuer den KI-Fallback in bot.py).

        Steht 'terraria' selbst drin -> immer True. Sonst reicht EIN eindeutiger
        Terraria-Begriff (Boss/Erz/Item) oder ZWEI schwaechere/mehrdeutige. So
        klingeln Alltagssaetze (Wetter, Essen, Mathe) nicht faelschlich an."""
        if not content:
            return False
        text = content.lower()
        # Einzelwoerter als Menge (fuer wortgenaue Treffer, kein Teilstring-Fehler
        # wie 'hell' in 'hello' oder 'ore' in 'more').
        woerter = set(re.findall(r"[a-zäöüß]+", text))
        if "terraria" in woerter:
            return True
        treffer = 0
        stark = 0
        for kw in self._TERRA_KEYWORDS:
            if " " in kw:
                gefunden = kw in text          # Mehrwort-Begriff: Teilstring
            else:
                gefunden = kw in woerter        # Einzelwort: wortgenau
            if gefunden:
                treffer += 1
                if kw in self._STARKE_KEYWORDS:
                    stark += 1
        if stark >= 1:
            return True
        return treffer >= 2

    # --- Wiki-Abrufe (alle fehlertolerant -> None) ---------------------------
    async def _api_get(self, params):
        """Ein GET gegen die MediaWiki-API. Gibt geparstes JSON zurueck oder None
        (bei jedem Netz-/Parse-Fehler). Setzt format=json und den User-Agent."""
        p = dict(params)
        p.setdefault("format", "json")
        try:
            async with ai.http_session().get(
                BASIS, params=p, headers=_HEADERS, timeout=_TIMEOUT
            ) as r:
                return await r.json(content_type=None)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError, KeyError):
            log.warning("Terraria-Wiki-Abruf fehlgeschlagen (%s)",
                        p.get("srsearch") or p.get("search") or p.get("titles") or "?")
            return None

    async def _suche_titel(self, frage):
        """Sucht die passendste Wiki-Seite zu einer Frage und gibt ihren Titel
        zurueck (oder None). Erst Volltextsuche, dann opensearch als Fallback."""
        data = await self._api_get({
            "action": "query", "list": "search", "srsearch": frage, "srlimit": 5,
        })
        titel = self._beste_seite(data)
        if titel:
            return titel
        data = await self._api_get({
            "action": "opensearch", "search": frage, "limit": 5,
        })
        return self._beste_seite(data)

    async def _seite_laden(self, titel, voll=False):
        """Holt Intro (oder Volltext) + grosses Seitenbild + Seiten-URL zu einer
        Seite. Rueckgabe: dict(titel, extract, bild, url) oder None.

        voll=False -> nur das Intro (exintro) fuer die Anzeige.
        voll=True  -> der ganze Seitentext als KI-Kontext (RAG)."""
        params = {
            "action": "query",
            "prop": "extracts|pageimages|info",
            "titles": titel,
            "explaintext": 1,        # Klartext statt HTML
            "piprop": "original",    # Original-(Gross-)Bild
            "inprop": "url",         # fullurl der Seite
            "redirects": 1,          # Weiterleitungen aufloesen
        }
        if not voll:
            params["exintro"] = 1
        data = await self._api_get(params)
        if not isinstance(data, dict):
            return None
        pages = (data.get("query") or {}).get("pages")
        if not isinstance(pages, dict):
            return None
        for pid, page in pages.items():
            if not isinstance(page, dict):
                continue
            if str(pid) == "-1" or "missing" in page:
                continue
            return {
                "titel": page.get("title") or titel,
                "extract": page.get("extract") or "",
                "bild": (page.get("original") or {}).get("source") or "",
                "url": page.get("fullurl") or "",
            }
        return None

    async def _frage_ki(self, frage, titel, kontext):
        """Laesst die KI die konkrete Frage NUR anhand der Wiki-Fakten beantworten
        (RAG). Rueckgabe: Antworttext oder None (KI aus / Fehler / leer)."""
        if not ai.is_enabled() or not kontext:
            return None
        kontext = kontext[:4000]      # KI-Kontext bewusst kurz halten
        prompt = (
            f"Terraria-Wiki-Seite: {titel}\n"
            f"Wiki-Fakten:\n{kontext}\n\n"
            f"Frage des Nutzers: {frage}\n\n"
            "Beantworte die Frage NUR mit diesen Wiki-Fakten, kurz und auf Deutsch. "
            "Steht die Antwort nicht drin, sag das ehrlich."
        )
        try:
            antwort = await ai.generate(
                prompt,
                system=("Du bist Flo, beantworte NUR mit den gegebenen "
                        "Terraria-Wiki-Fakten, kurz & frech, auf Deutsch, "
                        "erfinde nichts."),
                temperature=0.5,
                max_tokens=400,
            )
        except Exception:  # noqa: BLE001 - KI-Fehler darf das Wiki nie killen
            log.exception("Terraria-KI-Antwort fehlgeschlagen")
            return None
        return (antwort or "").strip() or None

    # --- Embeds --------------------------------------------------------------
    async def _baue_embed(self, message, frage, seite):
        """Baut das Antwort-Embed aus einer geladenen Wiki-Seite: KI-Antwort (wenn
        aktiv) als Beschreibung, grosses Seitenbild fuers Aussehen, roher
        Wiki-Auszug als Beleg-Feld, Link zur Quelle."""
        roh = re.sub(r"\s+", " ", seite.get("extract") or "").strip()
        ki_text = await self._frage_ki(frage, seite.get("titel") or "", roh)

        beschreibung = ki_text or self._kuerzen(roh, 1500)
        if not beschreibung:
            beschreibung = ("Zu dieser Seite steht im Terraria-Wiki gerade kein "
                            "Text bereit - schau am besten direkt rein.")

        emb = discord.Embed(
            title=self._kuerzen(seite.get("titel") or "Terraria", 240),
            description=self._kuerzen(beschreibung, 2000),
            color=_FARBE,
            url=(seite.get("url") or None),
        )
        # Grosses Bild: perfekt fuer 'wie sieht ... aus?' und Objekt-Namen.
        if seite.get("bild"):
            emb.set_image(url=seite["bild"])
        # Bei KI-Antwort bleibt der rohe Wiki-Auszug als Beleg sichtbar.
        if ki_text and roh:
            emb.add_field(name="📖 Wiki-Auszug",
                          value=self._kuerzen(roh, 1000), inline=False)
        emb.set_footer(text="Quelle: Terraria Wiki (terraria.wiki.gg)")
        return emb

    def _hinweis_embed(self):
        """Hinweis + zufaelliger Terraria-Fakt, wenn kein Thema genannt wurde."""
        name = self._bot_name or ai.bot_name()
        tipp = random.choice(self._ZUFALLS_TIPPS)
        emb = discord.Embed(
            title="🌳 Terraria-Wiki",
            description=(
                "Frag mich alles zu Terraria - ich zieh's live aus dem Wiki. Zum Beispiel:\n"
                f"`{name} terraria Plantera`\n"
                f"`{name} terraria wie besiege ich den Wall of Flesh?`\n"
                f"`{name} terraria Zenith`\n\n"
                f"💡 **Wusstest du?** {tipp}"
            ),
            color=_FARBE,
        )
        emb.set_footer(text="Quelle: Terraria Wiki (terraria.wiki.gg)")
        return emb

    def _keine_seite_embed(self, frage):
        """Freundlicher Hinweis, wenn zur Frage keine Wiki-Seite gefunden wurde."""
        emb = discord.Embed(
            title="🌳 Terraria-Wiki",
            description=(
                f"Dazu finde ich im Terraria-Wiki leider nichts zu "
                f"„{self._kuerzen(frage, 200)}“. Frag's mal anders oder nenn "
                "ein konkretes Item, einen Boss oder ein Biom."
            ),
            color=_FARBE,
        )
        emb.set_footer(text="Quelle: Terraria Wiki (terraria.wiki.gg)")
        return emb

    # --- oeffentliche Schnittstelle ------------------------------------------
    async def beantworte(self, message, frage):
        """Beantwortet eine freie Terraria-Frage mit echten Wiki-Daten + Bild.

        Sucht die beste Wiki-Seite, laedt Text + Bild + Link und baut daraus ein
        Embed. Ist die KI aktiv, beantwortet sie die konkrete Frage auf Basis der
        Fakten (RAG). Rueckgabe: discord.Embed (bot.py sendet es)."""
        frage = (frage or "").strip()
        if not frage:
            return self._hinweis_embed()
        titel = await self._suche_titel(frage)
        if not titel:
            return self._keine_seite_embed(frage)
        # Fuer die KI (RAG) den laengeren Volltext laden, sonst reicht das Intro.
        seite = await self._seite_laden(titel, voll=bool(ai.is_enabled()))
        if not seite:
            seite = await self._seite_laden(titel, voll=False)
        if not seite:
            return self._keine_seite_embed(frage)
        return await self._baue_embed(message, frage, seite)

    async def handle(self, message):
        """Erkennt die Prefix-Befehle ('terraria'/'terra'/'twiki'/'terrariawiki').

        Rueckgabe: Embed (Antwort/Hinweis) oder None (kein Terraria-Befehl ->
        naechstes Modul / KI ist dran)."""
        if not self._enabled or message.guild is None:
            return None
        cmd = ai.strip_lead(message.content or "")
        if not cmd:
            return None
        teile = cmd.split(None, 1)
        erstes = teile[0].lower()
        if erstes not in self._PREFIXE:
            return None
        rest = teile[1].strip() if len(teile) > 1 else ""
        if not rest:
            return self._hinweis_embed()
        return await self.beantworte(message, rest)


instance = Terraria()

# Modul-Aliase: bot.py & die Tests nutzen die Modul-Schnittstelle terraria.<name>.
HANDLED = Terraria.HANDLED
setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
erkennt_frage = instance.erkennt_frage
beantworte = instance.beantworte
# Interne Helfer (fuer Tests ohne Netz).
_kuerzen = instance._kuerzen
_beste_seite = instance._beste_seite
_suche_titel = instance._suche_titel
_seite_laden = instance._seite_laden
_TERRA_KEYWORDS = Terraria._TERRA_KEYWORDS
_STARKE_KEYWORDS = Terraria._STARKE_KEYWORDS
