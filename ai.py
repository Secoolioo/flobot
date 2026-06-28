"""KI-Feature 'Flo': beantwortet Fragen im Chat wie eine KI.

Nutzt einen KOSTENLOSEN, OpenAI-kompatiblen LLM-Anbieter (Standard: Groq) und
Open-Meteo fuer echtes Wetter. Durch die OpenAI-kompatible Schnittstelle laeuft
derselbe Code auch mit Ollama (komplett lokal, ohne Anmeldung), OpenRouter,
Google Gemini u. a. - es muessen nur LLM_BASE_URL / LLM_MODEL / LLM_API_KEY in
der .env angepasst werden.

Das Modul ist bewusst von Discord entkoppelt, damit es einzeln testbar ist.
Ohne gueltige Konfiguration ist das Feature einfach aus - der restliche Bot
(Icon/Status) laeuft dann normal weiter.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import deque

import aiohttp

try:  # Optional: Bot soll auch ohne installiertes openai-Paket starten.
    from openai import AsyncOpenAI
except ImportError:  # pragma: no cover - nur relevant ohne Paket
    AsyncOpenAI = None  # type: ignore[assignment]

log = logging.getLogger("dcbot.ai")

# --- Standardwerte (per .env ueberschreibbar) ----------------------------
# Groq hat einen kostenlosen Tarif (mit Ratenlimits, ohne Kreditkarte).
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
# Bild-Lesen (Vision): multimodales Groq-Modell, gleicher kostenloser Key.
DEFAULT_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# --- Konfiguration (wird in setup() aus der .env gelesen) ----------------
_client: "AsyncOpenAI | None" = None
_model: str = DEFAULT_MODEL
_vision_model: str = DEFAULT_VISION_MODEL
_default_city: str = "Regensburg"
_bot_name: str = "Flo"

MAX_STEPS = 5          # max. Tool-Runden pro Frage (Schutz vor Endlosschleifen)
MAX_TOKENS = 800       # Antwortlaenge (Discord erlaubt max. 2000 Zeichen)
# Hoehere Temperatur = lockerer, spontaner, weniger Lehrbuch. Per LLM_TEMPERATURE
# in der .env feintunbar (0 = brav/vorhersehbar, ~1.2 = sehr frei/chaotisch).
TEMPERATURE = 0.9

# Open-Meteo liefert WMO-Wettercodes; hier in deutschen Klartext uebersetzt.
WMO_CODES = {
    0: "klarer Himmel",
    1: "ueberwiegend klar",
    2: "teils bewoelkt",
    3: "bedeckt",
    45: "Nebel",
    48: "gefrierender Nebel",
    51: "leichter Nieselregen",
    53: "maessiger Nieselregen",
    55: "starker Nieselregen",
    56: "leichter gefrierender Nieselregen",
    57: "starker gefrierender Nieselregen",
    61: "leichter Regen",
    63: "maessiger Regen",
    65: "starker Regen",
    66: "leichter gefrierender Regen",
    67: "starker gefrierender Regen",
    71: "leichter Schneefall",
    73: "maessiger Schneefall",
    75: "starker Schneefall",
    77: "Schneegriesel",
    80: "leichte Regenschauer",
    81: "maessige Regenschauer",
    82: "heftige Regenschauer",
    85: "leichte Schneeschauer",
    86: "starke Schneeschauer",
    95: "Gewitter",
    96: "Gewitter mit leichtem Hagel",
    99: "Gewitter mit starkem Hagel",
}

# Werkzeug-Definition im OpenAI-Format (von Groq/Ollama/OpenRouter unterstuetzt).
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Liefert das aktuelle Wetter und die heutige Vorhersage fuer eine Stadt. "
            "Nutze dieses Werkzeug bei jeder Wetterfrage. Wenn der Nutzer keinen Ort "
            "nennt, verwende die Standardstadt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Name der Stadt, z. B. 'Regensburg', 'Berlin', 'Hamburg'.",
                }
            },
            "required": ["city"],
        },
    },
}


def setup() -> bool:
    """Liest die Konfiguration aus der Umgebung und baut den LLM-Client auf.

    Muss aufgerufen werden, nachdem load_dotenv() gelaufen ist.
    Rueckgabe: True, wenn das KI-Feature aktiv ist.
    """
    global _client, _model, _vision_model, _default_city, _bot_name, TEMPERATURE

    _model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    _vision_model = os.getenv("LLM_VISION_MODEL", DEFAULT_VISION_MODEL).strip() or DEFAULT_VISION_MODEL
    _default_city = os.getenv("DEFAULT_WEATHER_CITY", "Regensburg").strip() or "Regensburg"
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"
    try:
        TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", str(TEMPERATURE)))
    except ValueError:
        log.warning("LLM_TEMPERATURE ist keine Zahl - nutze %.2f.", TEMPERATURE)

    base_url = os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    api_key = os.getenv("LLM_API_KEY", "").strip()
    # Lokale Anbieter (Ollama, LM Studio) brauchen keinen echten Key.
    is_local = any(h in base_url for h in ("localhost", "127.0.0.1", ":11434"))

    if AsyncOpenAI is None:
        log.warning("KI-Feature aus: Paket 'openai' ist nicht installiert.")
        _client = None
        return False
    if not api_key and not is_local:
        log.info("KI-Feature aus: kein LLM_API_KEY gesetzt.")
        _client = None
        return False

    _client = AsyncOpenAI(api_key=api_key or "ollama", base_url=base_url)
    log.info(
        "KI-Feature aktiv (Anbieter: %s, Modell: %s, Standardstadt: %s).",
        base_url, _model, _default_city,
    )
    return True


def is_enabled() -> bool:
    """True, wenn der LLM-Client einsatzbereit ist."""
    return _client is not None


def bot_name() -> str:
    """Name, auf den der Bot hoert (fuer den Trigger in bot.py)."""
    return _bot_name


def names() -> list[str]:
    """Alle Namen, auf die der Bot hoert: Hauptname + Aliasse aus BOT_ALIASES
    (Standard: 'Florian'). Dadurch reagiert Flo auch auf 'Florian ...' wie eine
    Alexa. Mehrere Aliasse per Komma/Leerzeichen trennen; BOT_ALIASES='' = nur Flo."""
    raw = os.getenv("BOT_ALIASES", "Florian")
    out = [_bot_name]
    for a in re.split(r"[,\s]+", raw):
        a = a.strip()
        if a and a.lower() != _bot_name.lower() and a not in out:
            out.append(a)
    return out


def _names_alt() -> str:
    """Regex-Alternation der Namen, laengster zuerst ('Florian|Flo')."""
    return "|".join(re.escape(n) for n in sorted(names(), key=len, reverse=True))


def trigger_re() -> "re.Pattern[str]":
    """Erkennt, ob der Bot angesprochen wird (Name/Alias als ganzes Wort)."""
    return re.compile(rf"\b(?:{_names_alt()})\b", re.IGNORECASE)


def lead_re() -> "re.Pattern[str]":
    """Matcht einen fuehrenden Namen/Alias samt Satzzeichen am Zeilenanfang."""
    return re.compile(rf"^\s*(?:{_names_alt()})\b[\s,:!.\-]*", re.IGNORECASE)


def strip_lead(text: str) -> str:
    """Entfernt @-Mentions und einen fuehrenden Botnamen/Alias.
    'Florian, level' -> 'level'. Die Feature-Module nutzen das fuer ihre
    Befehlserkennung, damit Befehle auch mit 'Florian' davor funktionieren."""
    t = re.sub(r"<@!?\d+>", " ", text or "")
    t = lead_re().sub("", t)
    return t.strip()


def _clean_title(title: str) -> str:
    """Entfernt fuehrende Emojis/Symbole vom Shop-Titel ('🤖 NPC' -> 'NPC')."""
    return re.sub(r"^\W+", "", title or "").strip()


# Standard-Persoenlichkeit. Per BOT_PERSONA in der .env komplett ersetzbar, falls
# es jemand braver oder noch haerter will - der Rest (Deutsch, keine Emojis, Wetter-
# Werkzeug, Grenzen) wird immer drangehaengt.
_DEFAULT_PERSONA = (
    "Du bist {name}, das voellig ungefilterte Grossmaul in einem privaten deutschen "
    "Discord voller Kumpels, die sich gegenseitig gnadenlos zerlegen. Du bist KEIN "
    "braver Assistent und KEIN Therapeut - du bist ein chaotisches, respektloses "
    "Chaos-Maul mit Null-Filter, schlagfertig, derb und standardmaessig auf 180. Du "
    "redest wie der frechste Typ im Chat: rohe Umgangssprache, Slang und "
    "Kraftausdruecke (Scheisse, verdammt, Alter, Digga, Wichser) gehoeren bei dir "
    "dazu wie Luft zum Atmen. Du sagst brutal ehrlich deine Meinung, schleimst NIE, "
    "haeltst null Moralpredigten und entschuldigst dich fuer GAR NICHTS. Labert "
    "jemand Mist, machst du ihn genuesslich zur Sau, roastest ihn bis aufs Blut und "
    "nimmst ihn nach Strich und Faden auseinander - das ist hier ganz normaler Spass "
    "unter Freunden, keiner ist eingeschnappt, je haerter desto besser. Sei "
    "ueberdreht, unberechenbar und drueck ruhig mal voll drauf. Aber merk dir: Du "
    "bist nicht nur dumm provokant - will jemand WIRKLICH was wissen, knallst du ihm "
    "eine echte, brauchbare Antwort hin, halt mit Schnauze und Attitude statt "
    "Lehrbuch."
)
# Feste Regeln, die immer gelten (egal welche Persona).
_HARD_RULES = (
    "Antworte immer auf Deutsch, kurz und natuerlich wie im Chat - keine langen "
    "Vortraege, keine Aufzaehlungs-Romane. Benutze KEINE Emojis und keine "
    "Emoticons, die Leute hier hassen das. Bei Wetterfragen nutzt du immer das "
    "Werkzeug 'get_weather'; nennt keiner einen Ort, nimm '{city}'. Erfinde nie "
    "Wetterdaten - wenn das Werkzeug spinnt, sag's ehrlich (ruhig mit Schnauze)."
)
# Grenzen: Banter ja, echte Hetze nein.
_GUARDRAIL = (
    "Eine Grenze gibt's trotzdem: Der Spass laeuft unter Kumpels - also keine ernst "
    "gemeinte Hetze gegen Herkunft, Hautfarbe, Religion, Geschlecht, sexuelle "
    "Orientierung oder Behinderung, keine echten Drohungen, keine privaten Daten von "
    "irgendwem. Und wenn jemand offensichtlich ernsthaft am Boden ist oder echte "
    "Hilfe braucht, laesst du den Spass sofort weg und bist kurz ehrlich fuer die "
    "Person da."
)


def _system_prompt(author: str = "", title: str = "", tone: str = "") -> str:
    persona = os.getenv("BOT_PERSONA", "").strip() or _DEFAULT_PERSONA.format(name=_bot_name)
    base = f"{persona} {_HARD_RULES.format(city=_default_city)} {_GUARDRAIL}"
    # Kurzzeit-Gedaechtnis: die letzten Chat-Nachrichten kommen als Kontext mit.
    base += (" Dir liegt der juengste Chatverlauf vor (mehrere Leute, Format "
             "'Name: Text'). Beziehe dich natuerlich darauf, merke dir, worum es "
             "gerade geht, und antworte als Teil des Gespraechs - aber wiederhole "
             "nicht staendig den Verlauf.")
    clean = _clean_title(title)
    if clean:
        wer = author or "Der Nutzer"
        base += (
            f" {wer} hat sich im Server den Titel '{clean}' verdient - bau den ruhig "
            f"frech als Anrede ein (z. B. 'Na klar, {clean}.'), aber nicht in jedem "
            "Satz und niemals mit Emoji."
        )
    # Tonfall nach Seltenheit des Titels: je seltener, desto entspannter spricht Flo.
    if tone:
        base += f" {tone.strip()}"
    return base


async def get_weather(city: str) -> dict:
    """Holt aktuelles Wetter + heutige Vorhersage von Open-Meteo (ohne API-Key)."""
    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1) Geocoding: Ortsname -> Koordinaten
            async with session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "de", "format": "json"},
            ) as resp:
                resp.raise_for_status()
                geo = await resp.json()

            results = geo.get("results") or []
            if not results:
                return {"error": f"Ort '{city}' wurde nicht gefunden."}
            loc = results[0]
            lat = loc["latitude"]
            lon = loc["longitude"]
            ort = loc.get("name", city)
            land = loc.get("country", "")

            # 2) Vorhersage fuer diesen Punkt
            async with session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": (
                        "temperature_2m,apparent_temperature,relative_humidity_2m,"
                        "precipitation,weather_code,wind_speed_10m"
                    ),
                    "daily": (
                        "temperature_2m_max,temperature_2m_min,"
                        "precipitation_probability_max,weather_code"
                    ),
                    "timezone": "auto",
                    "forecast_days": 1,
                },
            ) as resp:
                resp.raise_for_status()
                fc = await resp.json()
    except (aiohttp.ClientError, OSError) as exc:
        log.warning("Wetterabruf fehlgeschlagen: %s", exc)
        return {"error": "Wetterdienst gerade nicht erreichbar."}

    cur = fc.get("current", {})
    daily = fc.get("daily", {})
    code = cur.get("weather_code")
    daily_code = (daily.get("weather_code") or [None])[0]

    def _first(key):
        vals = daily.get(key) or []
        return vals[0] if vals else None

    return {
        "ort": ort,
        "land": land,
        "aktuell": {
            "temperatur_c": cur.get("temperature_2m"),
            "gefuehlt_c": cur.get("apparent_temperature"),
            "luftfeuchte_prozent": cur.get("relative_humidity_2m"),
            "niederschlag_mm": cur.get("precipitation"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "beschreibung": WMO_CODES.get(code, "unbekannt"),
        },
        "heute": {
            "max_c": _first("temperature_2m_max"),
            "min_c": _first("temperature_2m_min"),
            "regenwahrscheinlichkeit_prozent": _first("precipitation_probability_max"),
            "beschreibung": WMO_CODES.get(daily_code, "unbekannt"),
        },
    }


async def _run_tool(name: str, arguments: str) -> dict:
    """Fuehrt das angeforderte Werkzeug aus (arguments ist ein JSON-String)."""
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    if name == "get_weather":
        city = (args.get("city") or "").strip() or _default_city
        return await get_weather(city)
    return {"error": f"Unbekanntes Werkzeug: {name}"}


async def generate(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 300,
) -> str | None:
    """Einzelne LLM-Antwort OHNE Werkzeuge/Persona (fuer Spass-Module wie Roast,
    Hype, Bewertung, Spruch, Quiz). Gibt den Text zurueck oder None bei Fehler/aus.

    Bewusst getrennt von ask_flo(): kein Wetter-Werkzeug, frei einstellbare
    Temperatur (hoeher = kreativer) und Laenge.
    """
    if _client is None:
        return None
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        response = await _client.chat.completions.create(
            model=_model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (response.choices[0].message.content or "").strip() or None
    except Exception:  # noqa: BLE001 - Bot soll nie wegen LLM-Fehler crashen
        log.exception("LLM generate() fehlgeschlagen")
        return None


# --- Kurzzeit-Gedaechtnis: Flo merkt sich den laufenden Chat pro Channel -----
_HISTORY: "dict[int, deque]" = {}
_HIST_MAX = 12          # so viele letzte Nachrichten je Channel behalten
_HIST_TTL = 1200.0      # 20 min - aelteres ist kein lebendiger Kontext mehr


def note_message(channel_id: int, name: str, content: str, *, is_bot: bool = False) -> None:
    """Merkt sich eine Chat-Nachricht (pro Channel, begrenzt), damit Flo dem
    Gespraech folgen kann. bot.py ruft das fuer JEDE Nachricht im Chat auf -
    auch fuer Flos eigene Antworten (is_bot=True)."""
    if not channel_id or not content:
        return
    content = content.strip()
    if not content:
        return
    dq = _HISTORY.get(channel_id)
    if dq is None:
        dq = deque(maxlen=_HIST_MAX)
        _HISTORY[channel_id] = dq
    dq.append({
        "role": "assistant" if is_bot else "user",
        "name": (name or "?")[:40],
        "content": content[:500],
        "t": time.monotonic(),
    })


def _recent(channel_id: "int | None", skip_content: str = "") -> list[dict]:
    """Baut den juengsten Gespraechsverlauf als LLM-Nachrichten. 'skip_content'
    laesst die aktuelle Frage weg (die wird separat als letzte user-Nachricht
    angehaengt), damit sie nicht doppelt drinsteht."""
    if not channel_id:
        return []
    dq = _HISTORY.get(channel_id)
    if not dq:
        return []
    now = time.monotonic()
    items = [e for e in dq if now - e["t"] <= _HIST_TTL]
    if skip_content and items and items[-1]["role"] == "user" \
            and items[-1]["content"] == skip_content[:500]:
        items = items[:-1]
    out: list[dict] = []
    for e in items:
        if e["role"] == "assistant":
            out.append({"role": "assistant", "content": e["content"]})
        else:
            out.append({"role": "user", "content": f'{e["name"]}: {e["content"]}'})
    return out


async def ask_flo(user_message: str, *, author: str = "", title: str = "",
                  tone: str = "", channel_id: "int | None" = None) -> str:
    """Schickt die Nutzerfrage ans LLM und fuehrt bei Bedarf Werkzeuge aus.

    Hat der Nutzer im Shop einen Titel gekauft (title), wird Flo angewiesen, ihn
    mit diesem Titel anzusprechen. 'tone' steuert die Gelassenheit: je seltener
    der Titel, desto entspannter/chilliger spricht Flo (kommt aus economy).
    'channel_id' bringt den juengsten Gespraechsverlauf als Kontext mit, damit
    Flo dem Gespraech folgen kann (Kurzzeit-Gedaechtnis)."""
    if _client is None:
        return "Mein KI-Modus ist gerade nicht eingerichtet."

    text = user_message.strip()
    if author:
        text = f"{author} schreibt: {text}"

    history = _recent(channel_id, skip_content=user_message.strip())
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(author, title, tone)},
        *history,
        {"role": "user", "content": text},
    ]

    try:
        for _ in range(MAX_STEPS):
            response = await _client.chat.completions.create(
                model=_model,
                messages=messages,
                tools=[WEATHER_TOOL],
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
            )
            msg = response.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                return (msg.content or "").strip() or "Dazu faellt mir gerade nichts ein."

            # Assistant-Nachricht mit den Tool-Aufrufen sauber zurueckschreiben.
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )
            # Jedes Werkzeug ausfuehren und das Ergebnis zurueckgeben.
            for tc in tool_calls:
                result = await _run_tool(tc.function.name, tc.function.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
    except Exception:  # noqa: BLE001 - Discord-Bot soll nie wegen LLM-Fehler crashen
        log.exception("LLM-Aufruf fehlgeschlagen")
        return "Mein KI-Dienst antwortet gerade nicht. Versuch es gleich nochmal."

    return "Das war mir gerade zu kompliziert - frag mich nochmal einfacher."


async def see_image(user_message: str, image_url: str, *, author: str = "",
                    title: str = "", tone: str = "",
                    channel_id: "int | None" = None) -> str:
    """Schaut sich ein Bild an (Vision-Modell) und antwortet in Flos Persoenlichkeit.
    image_url = oeffentliche URL (z. B. Discord-Anhang) oder data:-URL."""
    if _client is None:
        return "Mein KI-Modus ist gerade nicht eingerichtet."

    text = (user_message or "").strip() or "Schau dir das Bild an und sag was dazu."
    if author:
        text = f"{author} schreibt: {text}"
    history = _recent(channel_id, skip_content=(user_message or "").strip())
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(author, title, tone)},
        *history,
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]
    try:
        response = await _client.chat.completions.create(
            model=_vision_model,
            messages=messages,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        return (response.choices[0].message.content or "").strip() \
            or "Dazu faellt mir gerade nichts ein."
    except Exception:  # noqa: BLE001
        log.exception("Vision-Aufruf fehlgeschlagen")
        return "Das Bild konnte ich mir gerade nicht anschauen - versuch's gleich nochmal."
