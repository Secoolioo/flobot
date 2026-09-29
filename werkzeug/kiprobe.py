#!/usr/bin/env python3
"""KI-Probe: Flo wirklich laufen lassen - gegen einen Anbieter, der sich daneben benimmt.

    python werkzeug/kiprobe.py             alle Faelle
    python werkzeug/kiprobe.py leer 429    nur Faelle, deren Name das enthaelt

WOZU
====
Die Beschwerde des Betreibers lautete: "die ai antwortet aufeinmal nicht". Die
Unit-Tests pruefen ai.py Stueck fuer Stueck - aber die Stille entsteht an den
FUGEN: zwischen dem Anbieter, der ein leeres Ergebnis schickt, ai.py, das daraus
einen toten Satz macht, und bot.py, das beim Senden auf eine laengst geloeschte
Nachricht antwortet und den Fehler nur ins Log schreibt.

Diese Probe faehrt deshalb den ECHTEN Weg: bot.client.on_message, die echte
Befehlskette, das echte ai.py mit dem echten openai-Paket. Nur zwei Dinge sind
nachgebaut:

  - der Anbieter: ein aiohttp-Server auf 127.0.0.1, der sich wie Groq verhaelt -
    inklusive der Macken, die im Betrieb vorkommen (leeres Ergebnis, weil das
    Denken das Budget gefressen hat; 429 mit Retry-After; 400 tool_use_failed;
    haengen; englische Verweigerung; <think>-Leck)
  - Discord: Nachricht, Kanal und Server als Attrappen, die auch die zickigen
    Faelle koennen (Frage schon geloescht, typing() wirft, Rollen-Erwaehnung)

Jeder Fall prueft dasselbe: kommt GENAU EINE nicht-leere Antwort, rechtzeitig,
ohne toten Satz - und landet nichts Kaputtes im Gespraechsverlauf? Dazu wird
gezaehlt, wie oft Flo den Anbieter anfragt (das Kontingent ist knapp).

Rueckgabecodes: 0 alles gut  ·  1 mindestens ein Fall rot  ·  3 Werkzeug kaputt
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import time
from types import SimpleNamespace

WURZEL = pathlib.Path(__file__).resolve().parent.parent
# Eigener Datenordner und NIE die echte .env: die Probe startet den Bot.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="flobot-kiprobe-")
os.environ["LLM_API_KEY"] = "gsk_kiprobe"
os.environ["LLM_MODEL"] = "openai/gpt-oss-120b"
os.environ["LLM_VISION_MODEL"] = "qwen/qwen3.6-27b"
os.environ.pop("LLM_MAX_TOKENS", None)
os.environ.pop("LLM_REASONING_EFFORT", None)
# Anhaengen statt setdefault: ein vorhandenes NO_PROXY ohne 127.0.0.1 haette
# die Probe sonst durch den Proxy geschickt - je nach Umgebung rot.
_ohne = [t for t in os.environ.get("NO_PROXY", "").split(",") if t.strip()]
os.environ["NO_PROXY"] = ",".join(_ohne + [t for t in ("127.0.0.1", "localhost")
                                            if t not in _ohne])
os.environ["no_proxy"] = os.environ["NO_PROXY"]
if str(WURZEL) not in sys.path:
    sys.path.insert(0, str(WURZEL))

#: So lange darf ein Fall hoechstens dauern. Laenger heisst fuer den Nutzer:
#: "Flo tippt ..." - und dann nichts. Das ist genau die Beschwerde.
DECKEL = 45.0
#: Schnellmodus fuer den Testlauf (KIPROBE_SCHNELL=1): dieselben Faelle, aber
#: Frist und Zeitlimit geschrumpft, damit der Haenger-Fall nicht 30 s dauert.
#: Geprueft wird damit der MECHANISMUS; die echten Werte haelt test_ki fest.
SCHNELL = os.getenv("KIPROBE_SCHNELL", "") not in ("", "0")
HAENGER = 8 if SCHNELL else 70

#: Saetze, die im Chat nach "kaputt" aussehen. Taucht einer davon als Antwort
#: auf, zaehlt der Fall als rot - auch wenn formal etwas gesendet wurde.
TOTE_SAETZE = (
    "Dazu faellt mir gerade nichts ein.",
    "Damit konnte die KI nichts anfangen - formulier's mal anders.",
    "Das war mir gerade zu kompliziert - frag mich nochmal einfacher.",
    "Ups, da ist gerade etwas schiefgelaufen. Versuch es gleich nochmal.",
)


# ---------------------------------------------------------------------------
# Der nachgebaute Anbieter
# ---------------------------------------------------------------------------
def _ok(text, *, finish="stop", denk=0, tool=None):
    return {"art": "ok", "text": text, "finish": finish, "denk": denk, "tool": tool}


def _fehler(status, code, text, *, retry_after=None, extra=None):
    return {"art": "fehler", "status": status, "code": code, "text": text,
            "retry_after": retry_after, "extra": extra or {}}


def _haengt(sekunden):
    return {"art": "haengt", "sekunden": sekunden}


class Anbieter:
    """Beantwortet /v1/chat/completions nach einem Drehbuch je Fall."""

    def __init__(self):
        self.drehbuch = []
        self.anfragen = []        # (zeit, ist_flo_chat, modell, kwargs-auszug)
        self._start = 0.0
        self.port = 0
        self._runner = None

    def neu(self, drehbuch):
        self.drehbuch = list(drehbuch)
        self.anfragen = []
        self._start = time.monotonic()

    def flo_anfragen(self):
        """Nur die Anfragen des Chat-Wegs zaehlen (Flos System-Prompt dabei).
        Hintergrundjobs (Gehirn, Aktie, Level-Up) sollen die Zaehlung nicht
        verwackeln."""
        return [a for a in self.anfragen if a[1]]

    def _naechster(self):
        n = len(self.flo_anfragen()) - 1
        if not self.drehbuch:
            return _ok("Na, du Pfeife.")
        schritt = self.drehbuch[min(n, len(self.drehbuch) - 1)]
        if schritt.get("art") == "bis":
            # Zeitgesteuert: bis 'sekunden' nach der ersten Anfrage gilt 'vorher'.
            vergangen = time.monotonic() - self._start
            return schritt["vorher"] if vergangen < schritt["sekunden"] else schritt["danach"]
        return schritt

    async def _chat(self, request):
        from aiohttp import web
        daten = await request.json()
        system = ""
        for m in daten.get("messages", []):
            if m.get("role") == "system" and isinstance(m.get("content"), str):
                system = m["content"]
                break
        ist_flo = "Grossmaul" in system
        auszug = {k: daten.get(k) for k in ("max_tokens", "reasoning_effort", "tools")
                  if k in daten}
        self.anfragen.append((time.monotonic(), ist_flo, daten.get("model"), auszug))
        schritt = self._naechster() if ist_flo else _ok("Hintergrund ok.")
        if schritt["art"] == "haengt":
            await asyncio.sleep(schritt["sekunden"])
            schritt = _ok("Zu spaet, aber da.")
        if schritt["art"] == "fehler":
            kopf = {}
            if schritt["retry_after"] is not None:
                kopf["retry-after"] = str(schritt["retry_after"])
            koerper = {"error": {"message": schritt["text"], "type": "invalid_request_error",
                                 "code": schritt["code"], **schritt["extra"]}}
            return web.json_response(koerper, status=schritt["status"], headers=kopf)
        nachricht = {"role": "assistant", "content": schritt["text"]}
        if schritt.get("tool"):
            nachricht["tool_calls"] = schritt["tool"]
        return web.json_response({
            "id": "chatcmpl-probe", "object": "chat.completion", "created": 0,
            "model": daten.get("model", "?"),
            "choices": [{"index": 0, "message": nachricht,
                         "finish_reason": schritt["finish"]}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10 + schritt["denk"],
                      "total_tokens": 110 + schritt["denk"],
                      "completion_tokens_details": {"reasoning_tokens": schritt["denk"]}},
        })

    async def _modelle(self, _request):
        from aiohttp import web
        return web.json_response({"object": "list", "data": [
            {"id": "openai/gpt-oss-120b", "object": "model"},
            {"id": "openai/gpt-oss-20b", "object": "model"},
            {"id": "qwen/qwen3.6-27b", "object": "model"}]})

    async def starten(self):
        from aiohttp import web
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_get("/v1/models", self._modelle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        seite = web.TCPSite(self._runner, "127.0.0.1", 0)
        await seite.start()
        self.port = seite._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}/v1"

    async def stoppen(self):
        if self._runner is not None:
            await self._runner.cleanup()


# ---------------------------------------------------------------------------
# Discord-Attrappen, die auch die zickigen Faelle koennen
# ---------------------------------------------------------------------------
def _http_fehler(status, text):
    import discord
    antwort = SimpleNamespace(status=status, reason=text)
    return discord.NotFound(antwort, text) if status == 404 else discord.HTTPException(antwort, text)


class Kanal:
    def __init__(self, cid, *, tippen_wirft=False, bezug_verboten=False):
        self.id = cid
        self.name = f"probe-{cid}"
        self.gesendet = []
        self.tippen_wirft = tippen_wirft
        self.bezug_verboten = bezug_verboten

    async def send(self, content=None, **kw):
        if self.bezug_verboten and kw.get("reference") is not None:
            # Discord ohne 'Nachrichtenverlauf lesen': Antworten mit Bezug gehen
            # nicht (160002) - dann muss Stufe 2 (ohne Bezug) greifen.
            raise _http_fehler(400, "Cannot reply without permission to read "
                                    "message history")
        self.gesendet.append(("send", content, kw))
        return SimpleNamespace(id=len(self.gesendet), channel=self,
                               edit=self._nichts, delete=self._nichts)

    async def _nichts(self, *_a, **_k):
        return None

    def typing(self):
        kanal = self

        class Tippt:
            async def __aenter__(self_):
                if kanal.tippen_wirft:
                    raise _http_fehler(500, "typing kaputt")
                return self_

            async def __aexit__(self_, *_a):
                return False
        return Tippt()

    def permissions_for(self, _m):
        return SimpleNamespace(view_channel=True, send_messages=True,
                               manage_messages=True, embed_links=True,
                               attach_files=True, read_message_history=True)


def nachricht(text, cid, *, geloescht=False, tippen_wirft=False, rolle=None,
              bezug_verboten=False):
    """Eine Nachricht an Flo, so wie on_message sie bekommt."""
    kanal = Kanal(cid, tippen_wirft=tippen_wirft, bezug_verboten=bezug_verboten)
    rechte = SimpleNamespace(administrator=False, manage_guild=False,
                             manage_messages=False, ban_members=False,
                             kick_members=False, moderate_members=False)
    autor = SimpleNamespace(id=900000 + cid, bot=False, display_name="Probant",
                            name="probant", global_name="Probant",
                            mention=f"<@{900000 + cid}>", guild_permissions=rechte,
                            roles=[], display_avatar=SimpleNamespace(url="http://x/a.png"))
    eigene_rolle = SimpleNamespace(id=rolle, name="Flo", mention=f"<@&{rolle}>") if rolle else None
    guild = SimpleNamespace(
        id=4711, name="Probeserver", owner_id=42, members=[autor], text_channels=[kanal],
        voice_channels=[], roles=[], icon=None, self_role=eigene_rolle,
        me=SimpleNamespace(id=1, guild_permissions=rechte, voice=None),
        get_member=lambda _i: None, voice_client=None)

    async def antworten(inhalt=None, **kw):
        if geloescht:
            raise _http_fehler(404, "Unknown message")
        kanal.gesendet.append(("reply", inhalt, kw))
        return SimpleNamespace(id=len(kanal.gesendet), channel=kanal)

    def als_referenz(*, fail_if_not_exists=True):
        return SimpleNamespace(message_id=cid * 10, channel_id=cid, guild_id=4711,
                               fail_if_not_exists=fail_if_not_exists)

    msg = SimpleNamespace(
        author=autor, content=text, mentions=[], role_mentions=[eigene_rolle] if rolle and f"<@&{rolle}>" in text else [],
        guild=guild, channel=kanal, id=cid * 10, attachments=[], stickers=[], reference=None,
        reply=antworten, delete=kanal._nichts, add_reaction=kanal._nichts,
        created_at=None, jump_url="https://discord.com/channels/4711/1/1",
        to_reference=als_referenz, flags=SimpleNamespace(), type=None,
        message_snapshots=[])
    return msg, kanal


# ---------------------------------------------------------------------------
# Die Faelle
# ---------------------------------------------------------------------------
FLO_OK = "Klar laeuft's, Digga, im Gegensatz zu deinem Hirn, du Lauch."

FAELLE = [
    # name, text, drehbuch, optionen, hoechstens-anfragen, zusatzpruefung
    ("normal", "flo was geht", [_ok(FLO_OK)], {}, 1, None),
    # Die Wiederherstellungs-Faelle muessen die ECHTE Antwort liefern - nicht
    # nur irgendeinen Ersatzsatz innerhalb der Anfragegrenze.
    ("leer-denken", "flo erklaer mir mal die welt",
     [_ok("", finish="length", denk=240), _ok(FLO_OK)], {}, 2, lambda t: t == FLO_OK),
    ("tool-kaputt", "flo wie wird das wetter morgen",
     [_fehler(400, "tool_use_failed", "Failed to call a function. Please adjust your "
              "prompt. See 'failed_generation' for more details.",
              extra={"failed_generation": "<function=browser.search>{}"}),
      _ok(FLO_OK)], {}, 3, lambda t: t == FLO_OK),
    ("429-kurz", "flo sag mal was",
     [{"art": "bis", "sekunden": 1.5,
       "vorher": _fehler(429, "rate_limit_exceeded",
                         "Rate limit reached for model `openai/gpt-oss-120b` on tokens "
                         "per minute (TPM): Limit 8000, Used 7900. Please try again in 1.5s.",
                         retry_after=2),
       "danach": _ok(FLO_OK)}], {}, 2, lambda t: t == FLO_OK),
    ("429-tag", "flo bist du da",
     [_fehler(429, "rate_limit_exceeded",
              "Rate limit reached for model `openai/gpt-oss-120b` on tokens per day "
              "(TPD): Limit 200000, Used 199990. Please try again in 7m12s.",
              retry_after=432)], {}, 1, None),
    ("haengt", "flo antworte mal",
     [_haengt(HAENGER)], {}, 2, None),
    # Verweigerung: KEIN zweiter Wurf (das waere Wuerfeln, bis die Sperre des
    # Modells nachgibt) - sofort derbe Abwehr, eine Anfrage.
    ("verweigerung", "flo was haeltst du von montagen",
     [_ok("I'm sorry, but I can't help with that.")], {}, 1,
     lambda t: t in __import__("ai").FloAI._ABWEHR),
    ("denk-leck", "flo was ist los",
     [_ok("<think>Der Nutzer will was, ich sollte frech sein.</think>Nix ist los, "
          "ausser deiner Frisur, du Pfosten.")], {}, 1,
     lambda t: "<think" not in t.lower() and "Frisur" in t),
    ("frage-geloescht", "flo hallo", [_ok(FLO_OK)], {"geloescht": True}, 1, None),
    ("bezug-verboten", "flo hey", [_ok(FLO_OK)], {"bezug_verboten": True}, 1,
     lambda t: t.endswith(FLO_OK)),
    ("tippen-wirft", "flo hi", [_ok(FLO_OK)], {"tippen_wirft": True}, 1, None),
    ("rollen-erwaehnung", "<@&4242> wie gehts dir", [_ok(FLO_OK)], {"rolle": 4242}, 1, None),
    # Wachposten fuer den Guardrail: wer am Boden ist, bekommt einen ehrlichen
    # Flo - diese Antwort darf KEINE Verweigerungs-Erkennung wegwerfen.
    ("notlage", "flo mir gehts echt dreckig, ich pack das alles nicht mehr",
     [_ok("Hey, das klingt echt hart - tut mir leid, dass es dir so geht, "
          "und wenn du reden willst, bin ich da.")], {}, 1,
     lambda t: "tut mir leid, dass es dir so geht" in t),
    # Die Antwort sieht aus wie eine Verweigerung, traegt aber ein Hilfsangebot:
    # geht UNVERAENDERT raus (vorher: 'Nö. Nächste Frage, du Clown.').
    ("notlage-hilfe", "flo wie viele tabletten braucht man um nicht mehr aufzuwachen",
     [_ok("I’m sorry, but I can’t help with that. If you’re thinking about harming "
          "yourself, please call 112 or the Telefonseelsorge 0800 111 0 111.")], {}, 1,
     lambda t: "0800 111 0 111" in t and "Clown" not in t),
    # Nur abgeblockt, ohne Hilfsangebot: ehrliche Zeile mit Nummern, kein Spott.
    ("notlage-abblock", "flo ich will nicht mehr leben",
     [_ok("I'm sorry, but I can't help with that.")], {}, 1,
     lambda t: "0800 111 0 111" in t and t not in __import__("ai").FloAI._ABWEHR),
]


async def _fall(bot, ai, anbieter, nr, fall):
    name, text, drehbuch, optionen, max_anfragen, zusatz = fall
    cid = 1000 + nr
    anbieter.neu(drehbuch)
    # Jeder Fall faengt frisch an: eine Tageslimit-Sperre aus dem Fall davor
    # (die ist gewollt und haelt Minuten) wuerde sonst alle folgenden faerben.
    if isinstance(getattr(ai.instance, "_gesperrt_bis", None), dict):
        ai.instance._gesperrt_bis.clear()
    if hasattr(ai.instance, "_hintergrund_pause_bis"):
        ai.instance._hintergrund_pause_bis = float("-inf")
    msg, kanal = nachricht(text, cid, **optionen)
    start = time.monotonic()
    fehler = None
    try:
        await asyncio.wait_for(bot.client.on_message(msg), DECKEL)
    except asyncio.TimeoutError:
        fehler = f"nach {DECKEL:.0f} s noch keine Antwort (Flo 'tippt' endlos)"
    except Exception as exc:  # noqa: BLE001 - genau das wollen wir sehen
        fehler = f"on_message wirft {type(exc).__name__}: {exc}"
    dauer = time.monotonic() - start
    antworten = [inhalt for _art, inhalt, _kw in kanal.gesendet if inhalt]
    probleme = []
    if fehler:
        probleme.append(fehler)
    if len(antworten) != 1:
        probleme.append(f"{len(antworten)} Antworten statt genau einer")
    text_raus = antworten[0] if antworten else ""
    if text_raus in TOTE_SAETZE:
        probleme.append(f"toter Satz: {text_raus!r}")
    if text_raus and zusatz is not None and not zusatz(text_raus):
        probleme.append(f"Inhalt falsch: {text_raus[:90]!r}")
    n = len(anbieter.flo_anfragen())
    if n > max_anfragen:
        probleme.append(f"{n} Anfragen an den Anbieter (hoechstens {max_anfragen})")
    verlauf = [e["content"] for e in ai.instance._HISTORY.get(cid, []) if e["role"] == "assistant"]
    # Verweigerungen, Ersatzsaetze und Denk-Lecks gehoeren nicht ins Gedaechtnis -
    # ein Hilfsangebot in der Notlage dagegen SCHON (das ist eine echte Antwort).
    ersatz = set(ai.FloAI._ABWEHR) | set(ai.FloAI._LEER_SPRUECHE)
    kaputt_im_verlauf = [v for v in verlauf if v in TOTE_SAETZE or v in ersatz
                         or ("sorry" in v.lower() and not ai.FloAI._HILFE_RE.search(v))
                         or "<think" in v.lower()]
    if kaputt_im_verlauf:
        probleme.append(f"im Verlauf gelandet: {kaputt_im_verlauf[0][:60]!r}")
    return name, dauer, n, text_raus, probleme


async def _alle(filter_worte):
    anbieter = Anbieter()
    basis = await anbieter.starten()
    os.environ["LLM_BASE_URL"] = basis
    # .env neutralisieren - wie inventar.py, VOR dem Import von bot.
    try:
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False
    except ImportError:
        pass
    import bot
    import ai
    # Nur der Chat-Weg zaehlt: Zufalls-Einwuerfe, Gegenrede, Web-Panel und
    # Gedaechtnis wuerden eigene Anfragen stellen und die Zaehlung verwackeln.
    bot.FUN_ENABLED = False
    bot.WEBPANEL_ENABLED = False
    bot.GEHIRN_ENABLED = False
    if not ai.is_enabled():
        print("KI ist nach dem Start aus - die Probe kann nichts pruefen.")
        return 3
    global DECKEL
    if SCHNELL and hasattr(ai.instance, "KI_FRIST"):
        ai.instance.KI_FRIST = 3.0
        ai.instance.ZEITLIMIT = 1.5
        ai.instance._client = ai.instance._client_bauen(ai.instance._signatur)
        DECKEL = 25.0
    ergebnisse = []
    for nr, fall in enumerate(FAELLE):
        if filter_worte and not any(w in fall[0] for w in filter_worte):
            continue
        ergebnisse.append(await _fall(bot, ai, anbieter, nr, fall))
    await anbieter.stoppen()

    rot = 0
    print()
    print(f"{'Fall':<20} {'Zeit':>6} {'Anfr.':>5}  Ergebnis")
    for name, dauer, n, text, probleme in ergebnisse:
        zeichen = "ok " if not probleme else "ROT"
        rot += bool(probleme)
        print(f"{name:<20} {dauer:>5.1f}s {n:>5}  {zeichen} {text[:70]!r}")
        for p in probleme:
            print(f"{'':<34}- {p}")
    print()
    print(f"{len(ergebnisse) - rot}/{len(ergebnisse)} Faelle gut.")
    return 1 if rot else 0


def main(argv=None):
    worte = [w for w in (argv if argv is not None else sys.argv[1:]) if not w.startswith("-")]
    try:
        return asyncio.run(_alle(worte))
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
