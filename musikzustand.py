"""Die Warteschlange, die einen Neustart ueberlebt (musikzustand.json).

Nach einem Update (oder Absturz) kommt Flo von selbst zurueck in den Kanal und
spielt weiter, wo er war: derselbe Song an derselben Stelle, die Schlange
dahinter, Pause, Radio. Aber nur, wenn das wirklich jemand will - der Stand ist
hoechstens MUSIC_WIEDERHERSTELLEN_MIN (20) Minuten alt und im Kanal sitzt noch
ein Mensch. Sonst wird er verworfen (je eine Logzeile, warum).

Wo was aufgerufen wird:
    merken(player)          music.py, SYNCHRON: nach jedem erfolgreichen
                            start(), bei Pause/Weiter, beim Einreihen, am Ende
                            jedes Songwechsels, beim Verschieben in der
                            Warteschlange, beim Radio-Schalter und wenn der
                            Watchdog sie parkt
    vergessen(gid)          music.py, GuildPlayer.disconnect() ('flo stop',
                            Rauswurf, Leerlauf)
    ausstehend(gid)         music.py, 'flo stop' / 'flo hau ab': kommt hier
                            gleich eine Warteschlange zurueck? Dann meint der
                            Befehl SIE - auch wenn Flo noch in keinem Kanal
                            sitzt (sonst hiess es "Ich bin gerade in keinem
                            Sprachkanal.", und Flo kaeme trotzdem)
    takt()                  bot.py, im Voice-Watchdog-Takt (alle 15 s)
    sichern()               bot.py, beim Herunterfahren: EINMAL, im ersten
                            Durchgang VOR dem Abmelden (_alles_sichern mit
                            vor_abmelden=True), vor store.alle_sichern
    wiederherstellen(client) bot.py, einmal je Prozess aus on_ready. Die
                            Sendepause prueft bot.py dort NICHT - das tut
                            wiederherstellen selbst: in der Sendepause geht Flo
                            in keinen Kanal und postet nichts, die Schlange
                            kommt GEPARKT zurueck ('flo weiter' setzt fort)

merken() und vergessen() laufen mitten in der Wiedergabe - sie duerfen nie
werfen und nie warten. Beim Import passiert nichts.

Der Vertrag beim Herunterfahren (der Musik-Teil muss ihn halten):
* merken() kommt in SCHUEBEN: _play_many ruft es je eingereihtem Song, bei
  einer grossen Playlist bis zu musik_max_queue (500) Mal hintereinander. Es
  darf deshalb nur "geaendert" vermerken; gebaut und gespeichert wird in
  takt() bzw. sichern(). Jedes Mal die ganze Schlange neu bauen waere bei
  500 Songs 125.000 Track-Kopien auf dem Event-Loop.
* NACH sichern() sind merken() und vergessen() No-ops. Beim Abmelden trennt
  discord.py jede Voice-Verbindung (voice.disconnect(force=True)), der
  after-Callback schaltet weiter, und music.py meldet einen Player ohne Song
  (current=None) - das darf den gesicherten Stand nicht mehr aendern, sonst
  fehlt nach jedem Neustart genau der Song, der lief.
* sichern() bleibt trotzdem idempotent: ein zweiter Aufruf schreibt denselben
  Stand noch einmal, sonst nichts.
test_fundament.test_warteschlange_ueberlebt_das_abmelden haelt das fest.

Was NICHT gespeichert wird: die Stream-Adresse (YouTube unterschreibt sie nur
fuer Minuten - sie wird beim Zurueckholen frisch aufgeloest), Loop und Tempo
(gehoeren zu genau einem laufenden Song, siehe music.GuildPlayer.loop_rest).

Die Uhr: 'ts' ist "so stand es, als Flo noch lief" - takt() frischt es jede
Minute auf, sichern() beim Beenden. Das Alter misst also die Auszeit, nicht
wie lange ein Song schon laeuft. Auf dem Server springt die Wanduhr ~15 s nach
dem Booten zwei Stunden zurueck (uhr.py): ein Stand aus den Sekunden davor
liegt danach in der Zukunft (deshalb ZUKUNFT_TOLERANZ), und ein Flo, der VOR
dem Sprung zurueckholt, haelt einen frischen Stand fuer zwei Stunden alt.
Solange die Uhr nicht stabil ist, schaut er deshalb UHR_NACHHOLEN lang nach,
ob sie noch zurueckspringt, bevor er einen "zu alten" Stand verwirft.

Daten: data/musikzustand.json
    {"guilds": {"<gid>": {"v": 1, "ts": 1761052800.0, "voice": 111, "text": 222,
      "current": {...Song...}, "pos": 83.4, "pausiert": false, "radio": true,
      "radio_gesehen": ["dQw4w9WgXcQ"], "radio_zahl": 3, "geparkt": false,
      "queue": [{...Song...}]}}}
    Song: {"t": Titel, "q": yt-dlp-Eingabe, "u": Seite, "d": Dauer, "w": Wuenscher,
           "wid": Wuenscher-ID, "th": Bild, "h": Spotify-Hinweis, "r": Radio,
           "ab": Stelle (geparkt), "wh": lief schon, "lv": Live}
"""

import asyncio
import logging
import os
import time
from dataclasses import replace

import discord

import ai
from basis import FeatureBasis
import features
import laufzeit
import uhr
from store import JsonStore

log = logging.getLogger("dcbot.musikzustand")

#: Version des Eintrags - ein kuenftiger Umbau erkennt alte Staende daran.
VERSION = 1


def _env_zahl(name, vorgabe):
    try:
        wert = float(os.getenv(name, "") or vorgabe)
    except ValueError:
        log.warning("%s ist keine Zahl - nehme %s.", name, vorgabe)
        return float(vorgabe)
    return wert if wert > 0 else float(vorgabe)


#: Nur so frische Staende kommen zurueck (Sekunden; .env in MINUTEN). Wer
#: laenger weg war, dem reisst Flo nicht ungefragt den Kanal auf.
MAX_ALTER = 60.0 * _env_zahl("MUSIC_WIEDERHERSTELLEN_MIN", 20)
#: So weit darf ein Stand in der Zukunft liegen: geschrieben, als die Uhr
#: beim Booten noch zwei Stunden vorging (uhr.py).
ZUKUNFT_TOLERANZ = 2.5 * 3600
#: So lange nach on_ready wartet das Zurueckholen - Discord fuellt erst seine
#: Sprachkanal-Listen (wer sitzt wo?).
START_VERZOEGERUNG = 5.0
#: Steht das Gateway nach der Verzoegerung noch nicht, so lange darauf warten.
GATEWAY_WARTEN = 60.0
#: Ein "zu alter" Stand bei noch nicht stabiler Uhr: so lange schaut Flo nach,
#: ob die Uhr zurueckspringt, und zwar in diesem Abstand.
UHR_NACHHOLEN = 120.0
NACHHOL_TAKT = 5.0
#: So oft frischt takt() Stellen und Zeitstempel auf (Absturz, kill -9).
AUFFRISCHEN = 60.0
#: Mehr Songs nimmt kein Server mit (guildcfg 'musik_max_queue' geht bis 500).
MAX_SONGS = 500
#: Ein Song, der bis hierhin (Sekunden vor Schluss) lief, war durch.
FAST_DURCH = 3.0

_WIEDER_DA = "🔁 Bin wieder da. Hab eure Warteschlange mitgebracht – {n}, ihr Undankbaren."
_WIEDER_DA_PAUSE = ("🔁 Bin wieder da. Eure Warteschlange auch – {n}, auf Pause, wie ihr "
                    "sie liegen gelassen habt. `{name} weiter`, wenn ihr euch entschieden habt.")
_GEPARKT = ("🔁 Wollte zurück in euren Voice, Discord lässt mich nicht. Die Warteschlange "
            "({n}) hab ich geparkt – `{name} weiter`, und es geht da weiter.")
_HINTEN_AN = ("🔁 Bin wieder da – und ihr habt natürlich schon ohne mich angefangen. Eure "
              "alte Warteschlange ({n}) hab ich hinten drangehängt. Bitte, gern geschehen.")


def _jetzt():
    """Die Wanduhr (Tests stellen sie)."""
    return time.time()


def _mono():
    return time.monotonic()


async def _schlafen(sekunden):
    """Warten (Tests stellen es ab - asyncio.sleep selbst bleibt unberuehrt)."""
    await asyncio.sleep(sekunden)


def _songs(n):
    return "1 Song" if n == 1 else f"{n} Songs"


def _zahl(wert, vorgabe=0.0):
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return vorgabe
    return zahl if zahl == zahl and abs(zahl) != float("inf") else vorgabe


def _ganz(wert):
    try:
        return int(wert or 0)
    except (TypeError, ValueError):
        return 0


def _text(wert, laenge):
    return str(wert or "")[:laenge]


def _hinweis(hint):
    """Der Spotify-Hinweis (Best-Match), JSON-sicher - oder None."""
    if not isinstance(hint, dict):
        return None
    sauber = {str(k)[:20]: (v if isinstance(v, (int, float, bool)) or v is None
                            else str(v)[:200])
              for k, v in list(hint.items())[:8]}
    return sauber or None


def _song_dict(track):
    """Ein Track als Eintrag in musikzustand.json - ohne Stream-Adresse."""
    dauer = getattr(track, "duration", None)
    eintrag = {
        "t": _text(getattr(track, "title", ""), 150) or "Unbekannter Titel",
        "q": _text(getattr(track, "query", ""), 400),
        "u": _text(getattr(track, "webpage_url", ""), 400),
        "d": int(dauer) if isinstance(dauer, (int, float)) and dauer > 0 else None,
        "w": _text(getattr(track, "requested_by", ""), 64),
        "wid": int(getattr(track, "requested_by_id", 0) or 0),
        "th": _text(getattr(track, "thumbnail", ""), 400),
        "h": _hinweis(getattr(track, "match_hint", None)),
        "r": bool(getattr(track, "radio", False)),
    }
    ab = _zahl(getattr(track, "weiter_ab", 0.0))
    if ab >= 1.0:
        eintrag["ab"] = round(ab, 1)
    if getattr(track, "wiederhergestellt", False):
        eintrag["wh"] = True
    if getattr(track, "live", False):
        eintrag["lv"] = True
    return eintrag


def _song_aus(eintrag, music):
    """Ein Eintrag als lazy Track (wird erst beim Abspielen aufgeloest) - None,
    wenn nichts drinsteht, womit man ihn wiederfinden koennte."""
    if not isinstance(eintrag, dict):
        return None
    adresse = str(eintrag.get("u") or "")
    eingabe = str(eintrag.get("q") or "") or adresse
    if not eingabe:
        return None
    dauer = eintrag.get("d")
    hint = eintrag.get("h") if isinstance(eintrag.get("h"), dict) else None
    try:
        wid = int(eintrag.get("wid") or 0)
    except (TypeError, ValueError):
        wid = 0
    return music.Track(
        title=str(eintrag.get("t") or "Unbekannter Titel")[:150], stream_url="",
        webpage_url=adresse, duration=int(dauer) if isinstance(dauer, (int, float)) and dauer > 0 else None,
        requested_by=str(eintrag.get("w") or "")[:64], query=eingabe,
        thumbnail=str(eintrag.get("th") or ""), match_hint=hint,
        weiter_ab=max(0.0, _zahl(eintrag.get("ab"))), requested_by_id=wid,
        radio=bool(eintrag.get("r")), wiederhergestellt=bool(eintrag.get("wh")),
        live=bool(eintrag.get("lv")))


def _hat_stand(player):
    """Gibt es hier etwas, das einen Neustart ueberleben soll?"""
    return (getattr(player, "current", None) is not None
            or bool(getattr(player, "queue", None)))


def eintrag_bauen(player, jetzt=None):
    """Der Schnappschuss EINES Players - None, wenn es nichts zu merken gibt."""
    if not _hat_stand(player):
        return None
    current = getattr(player, "current", None)
    try:
        pos = float(player.position()) if current is not None else 0.0
    except Exception:  # noqa: BLE001 - Attrappen ohne Uhr: dann von vorn
        pos = 0.0
    text = getattr(player, "text_channel", None)
    gesehen = [str(v) for v in (getattr(player, "_radio_gesehen", None) or ())][-200:]
    return {
        "v": VERSION,
        "ts": _jetzt() if jetzt is None else jetzt,
        "voice": int(getattr(player, "active_channel_id", 0) or 0),
        "text": int(getattr(text, "id", 0) or 0),
        "current": _song_dict(current) if current is not None else None,
        "pos": round(max(0.0, pos), 1),
        "pausiert": bool(getattr(player, "pausiert", False)),
        "radio": bool(getattr(player, "radio", False)),
        "radio_gesehen": gesehen,
        "radio_zahl": int(getattr(player, "_radio_zahl", 0) or 0),
        "geparkt": bool(getattr(player, "_geparkt", False)),
        "queue": [_song_dict(t) for t in list(getattr(player, "queue", None) or ())[:MAX_SONGS]],
    }


def _sendepause():
    try:
        import admin
        return bool(admin.is_locked())
    except Exception:  # noqa: BLE001 - ohne admin keine Sendepause
        return False


class Musikzustand(FeatureBasis):
    """Schnappschuss der Player je Server, und das Zurueckholen nach dem Start."""

    def __init__(self):
        self._enabled = False
        self._store = None
        # Geaenderte Player seit dem letzten Bauen: gid -> Player (merken).
        self._geaendert = {}
        # Staende aus der Datei, die noch auf wiederherstellen warten:
        # "gid" -> Eintrag. takt() und sichern() lassen sie in Ruhe - sonst
        # raeumte ein leerer Player ('flo queue' in den ersten Sekunden) den
        # Stand weg, bevor er zurueckkam.
        self._alt = {}
        self._herunterfahren = False
        self._lief = False                  # wiederherstellen: einmal je Prozess
        self._aufgefrischt = float("-inf")  # monotonic der letzten Auffrischung

    def setup(self):
        if os.getenv("MUSIKZUSTAND_ENABLED", "1").strip().lower() in (
                "0", "false", "no", "off", "aus"):
            log.info("Neustartfeste Warteschlange aus (MUSIKZUSTAND_ENABLED=0).")
            return False
        self._store = JsonStore("musikzustand.json", default={"guilds": {}})
        guilds = self._store.data.get("guilds")
        if not isinstance(guilds, dict):
            guilds = self._store.data["guilds"] = {}
        self._alt = {gid: e for gid, e in guilds.items() if isinstance(e, dict)}
        self._geaendert = {}
        self._herunterfahren = False
        self._lief = False
        self._aufgefrischt = float("-inf")
        self._enabled = True
        log.info("Neustartfeste Warteschlange aktiv (%d Stand/Staende von vorher).",
                 len(self._alt))
        return True

    def is_enabled(self):
        return self._enabled

    # --- Mitschreiben -------------------------------------------------------
    def _guilds(self):
        guilds = self._store.data.setdefault("guilds", {})
        if not isinstance(guilds, dict):
            guilds = self._store.data["guilds"] = {}
        return guilds

    def merken(self, player):
        """Stand dieses Players als geaendert vermerken. Synchron, billig -
        kommt in Schueben. Nach sichern() ein No-op."""
        if not self._enabled or self._herunterfahren or player is None:
            return
        try:
            gid = int(getattr(player, "guild_id", 0) or 0)
        except (TypeError, ValueError):
            return
        if gid:
            self._geaendert[gid] = player

    def vergessen(self, gid):
        """Der Server hat bewusst gestoppt - kein Stand mehr fuer ihn. Nach
        sichern() ein No-op (das Abmelden ist kein 'flo stop')."""
        if not self._enabled or self._herunterfahren:
            return
        try:
            gid = int(getattr(gid, "id", gid) or 0)
        except (TypeError, ValueError):
            return
        self._geaendert.pop(gid, None)
        self._alt.pop(str(gid), None)
        if self._guilds().pop(str(gid), None) is not None:
            self._store.save_soon()

    def ausstehend(self, gid):
        """Wartet fuer diesen Server noch ein Stand von vor dem Neustart aufs
        Zurueckholen - oder wird er gerade zurueckgeholt? Synchron, billig."""
        if not self._enabled or not self._alt:
            return False
        try:
            return str(int(getattr(gid, "id", gid) or 0)) in self._alt
        except (TypeError, ValueError):
            return False

    def _bauen(self, gid, player, jetzt):
        """Den Eintrag eines Servers neu bauen (oder entfernen). True = geaendert."""
        guilds = self._guilds()
        self._alt.pop(str(gid), None)
        try:
            eintrag = eintrag_bauen(player, jetzt)
        except Exception:  # noqa: BLE001 - ein kaputter Player kippt nicht alle
            log.exception("Musik-Zustand von Server %s nicht baubar", gid)
            return False
        if eintrag is None:
            return guilds.pop(str(gid), None) is not None
        guilds[str(gid)] = eintrag
        return True

    def _spieler(self):
        """Die Player dieses Prozesses (gid -> GuildPlayer)."""
        try:
            import music
            return dict(music.instance._players)
        except Exception:  # noqa: BLE001
            return {}

    def _alles_bauen(self, *, alle):
        """Geaenderte Player bauen - mit alle=True auch jeden anderen, der etwas
        hat oder einen Eintrag fuehrt (Stelle und Zeitstempel auffrischen).
        Staende, die noch aufs Zurueckholen warten, bleiben, wie sie sind."""
        jetzt = _jetzt()
        geaendert = False
        faellig = dict(self._geaendert)
        self._geaendert.clear()
        if alle:
            guilds = self._guilds()
            for gid, player in self._spieler().items():
                try:
                    gid = int(gid or 0)
                except (TypeError, ValueError):
                    continue
                if not gid or gid in faellig or str(gid) in self._alt:
                    continue
                if _hat_stand(player) or str(gid) in guilds:
                    faellig[gid] = player
        for gid, player in faellig.items():
            geaendert = self._bauen(gid, player, jetzt) or geaendert
        return geaendert

    async def takt(self):
        """Regelmaessig (alle 15 s): Geaendertes bauen, jede Minute auch Stellen
        und Zeitstempel aller laufenden Player - das deckt Absturz und kill -9."""
        if not self._enabled or self._herunterfahren:
            return
        alle = _mono() - self._aufgefrischt >= AUFFRISCHEN
        if not alle and not self._geaendert:
            return
        if alle:
            self._aufgefrischt = _mono()
        if self._alles_bauen(alle=alle):
            self._store.save_soon()

    async def sichern(self):
        """Beim Herunterfahren, vor dem Abmelden: Positionen festhalten, sofort
        schreiben und ab jetzt nichts mehr aendern. Idempotent."""
        if not self._enabled or self._store is None:
            return
        if not self._herunterfahren:
            self._alles_bauen(alle=True)
            self._herunterfahren = True
            log.info("Musik-Zustand gesichert (%d Server).", len(self._guilds()))
        await self._store.save()

    # --- Zurueckholen ---------------------------------------------------------
    async def wiederherstellen(self, client):
        """Nach dem Start: zurueck in die Kanaele und weiterspielen. Einmal je
        Prozess - ein Reconnect ist kein Neustart."""
        if not self._enabled or self._lief:
            return
        self._lief = True
        if not self._alt:
            return
        await _schlafen(START_VERZOEGERUNG)
        await self._auf_gateway_warten()
        offen = dict(self._alt)
        frist = _mono() + UHR_NACHHOLEN
        while offen:
            jetzt = _jetzt()
            for gid in list(offen):
                eintrag = offen[gid]
                alter = jetzt - _zahl(eintrag.get("ts"))
                if alter > MAX_ALTER and not uhr.stabil() and _mono() < frist:
                    continue        # vielleicht geht die Uhr noch vor (uhr.py)
                del offen[gid]
                if self._alt.get(gid) is not eintrag:
                    continue        # inzwischen gestoppt oder neu gespielt
                try:
                    # Im Kontext DIESES Servers: der Task kommt aus on_ready,
                    # ohne ihn hiessen Ansagen und Panel nach der .env statt
                    # nach dem Praefix des Servers.
                    with ai.guild_kontext(_ganz(gid)):
                        await self._zurueckholen(client, gid, eintrag)
                except Exception:  # noqa: BLE001 - ein Server darf die anderen nie aufhalten
                    log.exception("Musik-Zustand: Zurueckholen auf Server %s gescheitert", gid)
                    self._weg(gid, eintrag)
            if offen:
                await _schlafen(NACHHOL_TAKT)
        self._store.save_soon()

    async def _auf_gateway_warten(self):
        bis = _mono() + GATEWAY_WARTEN
        while not laufzeit.gateway_stabil(gnade=0) and _mono() < bis:
            await _schlafen(NACHHOL_TAKT)

    def _weg(self, gid, eintrag, grund=None):
        """Einen alten Stand verwerfen - nur, wenn er noch der alte ist."""
        if grund:
            log.info("Musik-Zustand Server %s verworfen: %s.", gid, grund)
        if self._alt.get(gid) is eintrag:
            del self._alt[gid]
        if self._guilds().get(gid) is eintrag:
            del self._guilds()[gid]
            self._store.save_soon()

    async def _zurueckholen(self, client, gid, eintrag):
        """EINEN Server zurueckholen - oder sagen, warum nicht (Logzeile)."""
        import music
        alter = _jetzt() - _zahl(eintrag.get("ts"))
        if not -ZUKUNFT_TOLERANZ <= alter <= MAX_ALTER:
            return self._weg(gid, eintrag, f"{alter / 60:.0f} Minuten alt")
        try:
            guild = client.get_guild(int(gid))
        except (TypeError, ValueError):
            guild = None
        if guild is None:
            return self._weg(gid, eintrag, "Server nicht mehr da")
        if not features.is_on_in(guild.id, "music"):
            return self._weg(gid, eintrag, "Musik dort aus")
        songs = [_song_aus(e, music) for e in (eintrag.get("queue") or ())[:MAX_SONGS]]
        songs = [s for s in songs if s is not None]
        current = _song_aus(eintrag.get("current"), music)
        pos = max(0.0, _zahl(eintrag.get("pos")))
        if current is not None:
            current.wiederhergestellt = True     # lief schon - zaehlt nicht doppelt
            if current.live:
                pos = 0.0                        # Live spult man nicht, es geht am Rand weiter
            elif current.duration and pos >= current.duration - FAST_DURCH:
                current = None                   # war durch - weiter mit dem naechsten
        if current is None and not songs:
            return self._weg(gid, eintrag, "nichts drin")
        player = music.instance._player_for(guild.id)
        if player.sitzung_offen() or player._geparkt:
            # Jemand war schneller ('flo spiel ...' gleich nach dem Start): der
            # alte Stand stellt sich hinten an - genauso, als kaeme das 'spiel'
            # erst, waehrend Flo aufloest (_einsteigen).
            vorn = [replace(current, weiter_ab=pos if pos >= 1.0 else 0.0)] if current else []
            try:
                await self._hinten_an(player, vorn + songs)
            finally:
                self._erledigt(gid, eintrag)
            return None
        text = self._textkanal(guild, eintrag.get("text"))
        n = len(songs) + (current is not None)
        if eintrag.get("geparkt") or _sendepause():
            grund = "geparkt" if eintrag.get("geparkt") else "Sendepause"
            self._drumherum(player, eintrag, text)
            self._parken(player, current, pos, songs)
            log.info("Musik-Zustand Server %s: %s zurueck, %s ('weiter' setzt fort).",
                     gid, _songs(n), grund)
            return self._erledigt(gid, eintrag)
        kanal = self._sprachkanal(guild, eintrag.get("voice"))
        if kanal is None:
            return self._weg(gid, eintrag, "Sprachkanal weg")
        if not self._menschen(kanal):
            return self._weg(gid, eintrag, f"keiner in '{kanal.name}'")
        try:
            await self._einsteigen(player, kanal, current, pos, songs, eintrag, text, n)
        finally:
            # Erst jetzt: solange Flo aufloest und verbindet, laesst takt()
            # den alten Stand stehen - stirbt er in diesen Sekunden, ist er
            # beim naechsten Start noch da.
            self._erledigt(gid, eintrag)

    @staticmethod
    def _drumherum(player, eintrag, text):
        """Was ausser den Songs zurueckkommt: Radio (samt dem, was es schon
        gespielt hat - sonst kaemen dieselben Songs gleich wieder) und der
        Textkanal fuer Panel und Ansagen."""
        import music
        player.radio = bool(eintrag.get("radio"))
        player._radio_gesehen = [v for v in (eintrag.get("radio_gesehen") or ())
                                 if isinstance(v, str)][-music.RADIO_GESEHEN_MAX:]
        player._radio_zahl = max(0, int(_zahl(eintrag.get("radio_zahl"))))
        player._radio_hinweis = player.radio     # angesagt hat es sich schon
        if text is not None:
            player.text_channel = text

    def _erledigt(self, gid, eintrag):
        """Der alte Stand ist abgearbeitet - ab jetzt schreibt der Player selbst."""
        if self._alt.get(gid) is eintrag:
            del self._alt[gid]

    @staticmethod
    def _textkanal(guild, cid):
        try:
            cid = int(cid or 0)
        except (TypeError, ValueError):
            return None
        if not cid:
            return None
        holen = getattr(guild, "get_channel_or_thread", None) or guild.get_channel
        kanal = holen(cid)
        return kanal if kanal is not None and hasattr(kanal, "send") else None

    @staticmethod
    def _sprachkanal(guild, cid):
        try:
            kanal = guild.get_channel(int(cid or 0))
        except (TypeError, ValueError):
            return None
        return kanal if isinstance(kanal, discord.VoiceChannel) else None

    @staticmethod
    def _menschen(kanal):
        """Sitzt dort jemand, der kein Bot ist? Im Zweifel NEIN - nach einem
        Neustart ungefragt in einen leeren Kanal zu platzen waere schlimmer,
        als einmal nicht zurueckzukommen."""
        try:
            return any(not getattr(m, "bot", False) for m in kanal.members)
        except Exception:  # noqa: BLE001 - unbekannt ist nicht besetzt
            return False

    def _parken(self, player, current, pos, songs):
        """Als GEPARKTE Schlange zurueck: kein Beitritt, kein Post - 'weiter'
        oder das naechste 'spiel' machen dort weiter (Music._schlange_fortsetzen)."""
        import music
        if current is not None:
            songs = [replace(current, weiter_ab=pos if pos >= 1.0 else 0.0)] + songs
        player.queue = list(songs)
        player._geparkt = bool(player.queue)
        music._zustand_merken(player)

    async def _hinten_an(self, player, tracks):
        """Auf dem Server laeuft schon wieder eine Sitzung (jemand war schneller
        als das Zurueckholen): der alte Stand stellt sich hinten an, soweit der
        Deckel des Servers (musik_max_queue) reicht. Flo wechselt dafuer nicht
        den Kanal, Radio und Pause bleiben, wie sie sind. Rueckgabe: wie viele
        Songs mitkamen."""
        import music
        platz = max(0, music.max_queue(player.guild_id) - len(player.queue))
        mit = list(tracks)[:platz]
        if not mit:
            log.info("Musik-Zustand: auf dem Server lief schon was, und die Schlange "
                     "ist voll - %s verworfen.", _songs(len(tracks)))
            return 0
        leer = player.current is None and not player.queue
        for track in mit:
            music.instance._einreihen(player, track)
        log.info("Musik-Zustand: auf dem Server lief schon was - %s hinten angestellt.",
                 _songs(len(mit)))
        voice = player.voice
        if (leer and player.current is None and voice is not None and voice.is_connected()
                and not player.ist_pausiert() and not player._advance_lock.locked()):
            # Flo sass schon im Kanal, aber es lief nichts ('flo komm'): dann
            # geht es mit dem alten Stand los, statt dass er nur wartet.
            player._advancing = False
            await player._advance()
        elif not _sendepause():
            await player._sag(_HINTEN_AN.format(n=_songs(len(mit))))
            try:
                await music.instance._panel_auffrischen(player)
            except Exception:  # noqa: BLE001 - das Panel ist Deko
                log.debug("Panel nach dem Hintenanstellen nicht aufgefrischt", exc_info=True)
        return len(mit)

    async def _abbrechen(self, player, sitzung, current, pos, songs):
        """Waehrend Flo verband oder sich meldete, ist etwas weggebrochen. Dann
        nichts starten und nichts einreihen.

        Ist die Sitzung weg ('flo stop', Rauswurf, Leerlauf), bleibt es dabei -
        sonst stuende eine Geisterschlange im gestoppten Player, und merken()
        schriebe sie fuer den naechsten Neustart fest. Kam die Verbindung erst
        NACH dem Stopp zustande, haengt Flo ohne Sitzung im Kanal: auch die muss
        weg. Ist nur die Verbindung weg (kein Stopp), wird geparkt wie bei
        einem gescheiterten Beitritt."""
        if player._session_gen == sitzung:
            log.warning("Musik-Zustand: Verbindung beim Zurueckholen weg - Schlange geparkt.")
            player.active_channel_id = None
            self._parken(player, current, pos, songs)
        elif player.voice is not None:
            log.info("Musik-Zustand: waehrend des Zurueckholens gestoppt - raus aus dem Kanal.")
            await player.disconnect()
        else:
            log.info("Musik-Zustand: waehrend des Zurueckholens gestoppt - nichts gestartet.")

    async def _einsteigen(self, player, kanal, current, pos, songs, eintrag, text, n):
        """Den Song frisch aufloesen, rein in den Kanal, ihn an seiner Stelle
        starten, die Schlange dahinter - und sagen, dass Flo wieder da ist.

        Nach JEDEM await wird nachgesehen, ob die Sitzung noch steht: 'flo stop'
        geht auch, solange Flo noch in keinem Kanal sitzt (music.py fragt dafuer
        ausstehend()), und ein Rauswurf kann jederzeit kommen."""
        import music
        sitzung = player._session_gen
        pausiert = bool(eintrag.get("pausiert"))
        name = self.name_fuer(player.guild_id)

        def vorbei():
            return player._session_gen != sitzung or player.voice is None

        # Der Watchdog haelt sich raus, solange das hier laeuft (heal fragt
        # _advancing) - sonst stiesse er die halb gefuellte Schlange an.
        player._advancing = True
        try:
            gestartet = None
            if current is not None:
                # ERST aufloesen, dann in den Kanal: geht gar nichts, setzt sich
                # Flo nicht fuer nichts in den Kanal.
                try:
                    gestartet = await music._resolve_track(current)
                except Exception:  # noqa: BLE001 - dann eben der naechste
                    log.warning("Musik-Zustand: '%s' nicht ladbar - mache mit dem "
                                "naechsten weiter.", current.title)
                if player._session_gen != sitzung:
                    log.info("Musik-Zustand: waehrend des Aufloesens gestoppt.")
                    return
            if gestartet is None and not songs:
                log.info("Musik-Zustand: in '%s' war nur '%s', und der laedt nicht - "
                         "bleibe draussen.", kanal.name, getattr(current, "title", "?"))
                return
            # Der alte Song kommt LAZY hinten an (die frisch aufgeloeste
            # Stream-Adresse liefe in der Schlange ab).
            vorn = ([replace(current, weiter_ab=pos if pos >= 1.0 else 0.0)]
                    if gestartet is not None else [])
            if player.sitzung_offen() or player._geparkt:
                # Jemand war schneller ('flo spiel ...' in den ersten Sekunden):
                # Flo bleibt, wo er ist, statt in den alten Kanal zu wechseln.
                await self._hinten_an(player, vorn + songs)
                return
            self._drumherum(player, eintrag, text)
            try:
                await player.connect(kanal)
            except music.VOICE_CONNECT_FEHLER as exc:
                if player._session_gen != sitzung:
                    return                  # gestoppt, waehrend Discord nicht wollte
                log.warning("Musik-Zustand: zurueck in '%s' ging nicht (%s: %s) - "
                            "Schlange geparkt.", kanal.name, type(exc).__name__, exc)
                await player._halboffen_wegraeumen(kanal.guild)
                player.active_channel_id = None
                self._parken(player, gestartet or current, pos, songs)
                await player._sag(_GEPARKT.format(n=_songs(n), name=name))
                return
            if vorbei():
                return await self._abbrechen(player, sitzung, gestartet or current, pos, songs)
            player.fremden_ton_stoppen()
            await player._sag((_WIEDER_DA_PAUSE if pausiert and gestartet is not None
                               else _WIEDER_DA).format(n=_songs(n), name=name))
            if gestartet is not None:
                try:
                    await player._warte_bis_still()
                except Exception:  # noqa: BLE001 - dann eben ohne Warten
                    log.debug("Musik-Zustand: Warten auf Stille gescheitert", exc_info=True)
            if vorbei():
                return await self._abbrechen(player, sitzung, gestartet or current, pos, songs)
            if player.current is not None or player._advance_lock.locked():
                # In den Sekunden seit dem Verbinden hat jemand selbst etwas
                # gestartet - dann nicht drueberbuegeln, sondern hinten an.
                await self._hinten_an(player, vorn + songs)
                return
            if gestartet is not None:
                try:
                    player.start(gestartet, seek=0.0 if gestartet.live else pos)
                except Exception:  # noqa: BLE001
                    log.exception("Musik-Zustand: '%s' liess sich nicht starten",
                                  gestartet.title)
                    gestartet = None
            for track in songs:
                music.instance._einreihen(player, track)
            if gestartet is None:
                player._advancing = False
                await player._advance()     # der naechste, wie nach einem Skip
                log.info("Musik-Zustand: zurueck in '%s', %s.", kanal.name, _songs(n))
                return
            if pausiert:
                player.pausieren()
            stelle = music._fmt_dur(pos) or "0:00"
            extra = (f"Wieder da – pausiert bei {stelle}" if pausiert
                     else f"Wieder da – weiter bei {stelle}")
            try:
                await music._send_panel(player, gestartet, extra=extra)
            except Exception:  # noqa: BLE001 - das Panel ist Deko
                log.exception("Musik-Zustand: Panel nach dem Zurueckholen fehlgeschlagen")
            log.info("Musik-Zustand: zurueck in '%s' - '%s' bei %s, %s.", kanal.name,
                     gestartet.title, stelle, _songs(n))
        finally:
            player._advancing = False
            music._zustand_merken(player)


instance = Musikzustand()

setup = instance.setup
is_enabled = instance.is_enabled
merken = instance.merken
vergessen = instance.vergessen
takt = instance.takt
sichern = instance.sichern
wiederherstellen = instance.wiederherstellen
ausstehend = instance.ausstehend
