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

# --- Konfiguration (wird in setup() aus der .env gelesen) ----------------
_client: "AsyncOpenAI | None" = None
_model: str = DEFAULT_MODEL
_default_city: str = "Regensburg"
_bot_name: str = "Flo"

MAX_STEPS = 5          # max. Tool-Runden pro Frage (Schutz vor Endlosschleifen)
MAX_TOKENS = 800       # Antwortlaenge (Discord erlaubt max. 2000 Zeichen)
TEMPERATURE = 0.6      # etwas Lockerheit, aber nicht zu wild

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
    global _client, _model, _default_city, _bot_name

    _model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    _default_city = os.getenv("DEFAULT_WEATHER_CITY", "Regensburg").strip() or "Regensburg"
    _bot_name = os.getenv("BOT_NAME", "Flo").strip() or "Flo"

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


def _system_prompt(author: str = "", title: str = "") -> str:
    base = (
        f"Du bist {_bot_name}, ein freundlicher, lockerer KI-Assistent in einem "
        "deutschen Discord-Server. Du antwortest immer auf Deutsch, kurz und "
        "natuerlich, so wie man in einem Chat schreibt. Lange Vortraege vermeidest "
        "du. Benutze KEINE Emojis - die Leute im Server moegen das ueberhaupt nicht. "
        "Schreib einfach normalen Text ohne Emojis und ohne Emoticons. "
        "Bei Wetterfragen nutzt du immer das Werkzeug 'get_weather'. Nennt jemand "
        f"keinen Ort, nimm '{_default_city}' als Standard. Erfinde niemals "
        "Wetterdaten - wenn das Werkzeug einen Fehler liefert, sag das ehrlich."
    )
    clean = _clean_title(title)
    if clean:
        wer = author or "Der Nutzer"
        base += (
            f" {wer} hat sich im Server den Titel '{clean}' verdient. Sprich {wer} "
            f"in deiner Antwort mindestens einmal mit diesem Titel an (als Anrede, "
            f"z. B. 'Klar, {clean}.' oder bau ihn locker ein). Mach es natuerlich, "
            "nicht in jedem Satz, und niemals mit Emoji."
        )
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


async def ask_flo(user_message: str, *, author: str = "", title: str = "") -> str:
    """Schickt die Nutzerfrage ans LLM und fuehrt bei Bedarf Werkzeuge aus.

    Hat der Nutzer im Shop einen Titel gekauft (title), wird Flo angewiesen, ihn
    mit diesem Titel anzusprechen."""
    if _client is None:
        return "Mein KI-Modus ist gerade nicht eingerichtet."

    text = user_message.strip()
    if author:
        text = f"{author} schreibt: {text}"

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(author, title)},
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
