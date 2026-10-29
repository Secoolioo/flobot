"""Voice-Clips: 'flo clip' schneidet die letzten 30 s bis 5 min aus dem Voice.

Wie es sich anfuehlt
====================
    flo clip                Dialog: 30 s · 1 min · 5 min, dazu "👥 Alle" oder EINE
                            Person - Stand ist der Moment der Frage, nicht des Klicks
    flo clip 1m @Bob        direkt: Laenge und nur Bob ('30s', '2 min', '90',
                            'halbe minute', 'fuenf minuten' ...; eine nackte Zahl
                            bis 5 sind Minuten, darueber Sekunden)
    flo clips [monat|alle]  die Clips mit den meisten 🔥 (Woche, Monat, alles, was
                            der Index kennt: 5 Wochen)
    flo clip nein · ja      ich will nicht mitgeschnitten werden · wieder rein
    flo clip an · aus       Flo kommt lauschen · Mikro aus, Puffer weg
    flo clip status         Zahlen fuer Mods (nie Namen)

Raus kommt nur eine MP3 unter 10 MB (Budget: min(9,5 MiB, Upload-Grenze des
Servers minus 512 KiB)) im Clip-Kanal (guildcfg 'clip_channel', Standard
guildcfg.CLIP_CHANNEL, sonst die Ansagen, sonst der System-Kanal - nie ein
Aufraeum-Kanal). Ist der Voice privat, bleibt der Clip in dessen Voice-Chat.
Dazu ein Transkript (Whisper bei Groq, "KI, ohne Gewaehr"), ein passiv-
aggressiver Satz von Flo, 🔥 zum Abstimmen und 🗑️ fuer alle, die drin sind.

Wann Flo mithoert
=================
Nur in einer ECHTEN Musik-Sitzung (spielen, Pause, Radio, 'flo komm', die
wiederhergestellte Warteschlange, 'flo clip an') - nie bei einem kurzen
Soundboard-/TTS-Besuch. Dazu muessen gelten: Funktion 'clips' an, Einstellung
'clip_aufnahme' an (ab Werk nur auf dem Hauptserver), keine Sendepause, ffmpeg
mit MP3 da, Flo nicht vom Server taub gestellt, kein 'Mikro aus' fuer diesen
Kanal - und die ANSAGE steht im Voice-Chat des Kanals. Ohne Ansage keine
Aufnahme. Erst danach hebt Flo die Taubheit auf: das Kopfhoerer-Symbol ist die
ehrliche Anzeige. Hoert er auf, stellt er sich wieder taub. Die Reihenfolge
der Pruefungen steht in _soll().

Kein Bock? 🙅 Ohne mich (Knopf an der Ansage) oder 'flo clip nein': sofort
raus, auf jedem Server - auch wo Clips aus sind (datenschutz()), auch in der
Sendepause (der Knopf). Pakete dieser Person werden schon vor DAVE verworfen,
was im Puffer lag, ist weg, und vor dem Hochladen wird nochmal geprueft.
🔇 Fuer alle aus (oder 'flo clip aus') leert den Puffer und gilt, bis die
Runde in diesem Kanal endet (hoechstens 12 Stunden, auch ueber einen Neustart).

Was gespeichert wird (data/clips.json) - nie Ton, nie ein Transkript:
    {"opt_out": {"<uid>": ts},                 wer raus ist (global)
     "ansagen": {"<voice_cid>": {"ts", "mid"}}, wann die Ansage wo steht
     "aus":     {"<voice_cid>": ts},           Mikro aus fuer diese Runde
     "whisper": {...},                         clipstext.Kontingent.als_dict()
     "guilds":  {"<gid>": {"clips": {"<id8>": {mid, cid, vc, ts, von, dauer,
                                              sprecher: [uid], stimmen: [uid]}}}}}
Aufgeraeumt: Clips nach 35 Tagen, Ansagen nach 7 Tagen, 'aus' nach 12 Stunden
oder wenn die Runde in dem Kanal vorbei ist.

Der Ton selbst liegt nur im Arbeitsspeicher (Ringpuffer, 315 s, gedeckelt auf
CLIP_PUFFER_MB je Server und 160 MiB insgesamt), die MP3 nur als Bytes; die
Temp-Dateien des Schnitts loescht clipschnitt im finally.

Wo was aufgerufen wird:
    setup()                     bot.py, nur wenn die Musik laeuft (CLIPS_ENABLED)
    nur_datenschutz()           bot.py sonst: nur der Ausstieg (Speicher offen)
    handle(message)             Handler-Kette, direkt nach voicegags
    datenschutz(message)        gleich dahinter, IMMER: 'clip nein/ja' geht auch,
                                wo Clips aus sind
    voice_state(m, vorher, nachher)  bot.on_voice_state_update (nur Flo zaehlt)
    tick(client)                bot.voice_heal_loop, alle 15 s
    herunterfahren()            bot._alles_sichern(vor_abmelden=True)
    datenschutz_dm(message)     bot.on_message, Privatnachricht: 'clip nein/ja'
                                geht auch dort (nicht an den Besitzer)
    ansage_behalten(msg)        bot._keep_bot_msg - die AKTUELLE Ansage bleibt
                                auch in einem Aufraeum-Kanal stehen
    ist_clip(msg)               ebenso ein Clip im Voice-Chat eines privaten
                                Voice (der Clip-Kanal ist nie ein Aufraeum-Kanal)
    geloescht(gid, cid, ids)    bot.on_raw_message_delete (+ bulk): die Ansage
                                weg -> sofort eine neue (sonst Schluss); ein
                                von Hand geloeschter Clip fliegt aus dem Index
    empfangs_klasse()           music.verbindungs_optionen (sys.modules, beim Aufruf)
    sitzung_geaendert(gid)      music.GuildPlayer.connect/disconnect (ebenso)
    haelt(gid, kanal_id)        music.GuildPlayer._leerlauf: haelt 'flo clip an'
                                Flo hier noch? (ebenso)
    laeuft_auf(gid)             music.GuildPlayer._fremder_besuch: koennte clips
                                hier mithoeren? (ebenso)

music und voicegags fragen clips ueber sys.modules - beim Aufruf, nie beim
Import, und setup() traegt sich nirgends ein: solange clips aus ist, verbinden
Musik, Soundboard und TTS exakt wie vorher.

Beim Import passiert nichts.
"""

import asyncio
import contextlib
import io
import logging
import os
import re
import secrets
import sys
import time
from collections import deque
from dataclasses import dataclass

import discord

import ai
import basis
from basis import FeatureBasis
import clipschnitt
import clipstext
import features
import guildcfg
import laufzeit
import music
import numfmt
import uhr
import voiceempfang
from store import JsonStore

log = logging.getLogger("dcbot.clips")

HANDLED = basis.HANDLED   # ein Sentinel fuer alle, siehe basis.py

# Befehlswoerter. Beide stehen auch in cmdnorm.KNOWN (ein Test haelt das fest).
# befehl() vergleicht an der Befehlsstelle mit genau diesem Tupel als Literal:
# werkzeug/inventar.py liest die Woerter eines Moduls ohne Probe von dort.
_CMDS = ("clip", "clips")

# --- Grenzen ------------------------------------------------------------------
#: Laengster Clip.
PUFFER_SEK = 300
#: So lange bleibt ein Paket im Puffer (etwas mehr als der laengste Clip).
HALTEN_SEK = 315
#: Kuerzester Clip - kuerzer wird aufgerundet.
MIN_SEK = 10
#: Hat Flo noch nicht so lange zugehoert, gibt es keinen Clip ...
MIN_REST_SEK = 3
#: ... und darunter ist es keine Stimme, sondern ein Raeuspern.
STIMME_MIN_SEK = 1.0
#: So lange wartet der Dialog auf einen Klick.
DIALOG_SEK = 60
#: Spaetestens dann raeumt der Takt einen Dialog weg, dessen Timeout nie kam.
DIALOG_ABLAUF = 75
#: So lange nach der Frage wird noch abgearbeitet, was unterwegs war - das
#: Fenster endet trotzdem im Moment der Frage.
SETTLE_SEK = 0.7
#: Steht die Ansage in einem Kanal juenger als das (und gibt es sie noch), kommt
#: keine neue - ein Neuaufbau spammt nicht.
ANSAGE_PAUSE = 45 * 60
#: Ging die Ansage nicht raus, so lange keinen neuen Versuch in DIESEM Kanal.
ANSAGE_FEHLT_PAUSE = 600
#: So oft sieht der Takt nach, ob die Ansage noch steht (geloescht -> neu, und
#: geht das nicht: aufhoeren). Meldet Discord die Loeschung, sofort.
ANSAGE_NACHSEHEN = 300
#: So lange ueberlebt der Puffer einen Client-Wechsel der Musik (Neuaufbau).
GNADE_SEK = 120
#: Je Person so lange zwischen zwei Clips ...
ABKUEHLEN_SEK = 45
#: Nach 'Mikro aus' so lange kein 'flo clip an' im selben Kanal - jedes aus
#: postet einen Hinweis in den Voice-Chat, jedes an eine neue Ansage.
AN_PAUSE = 60
#: ... und je Server hoechstens so viele in der Stunde.
STUNDE_MAX = 12
#: So viele Schnitte duerfen warten oder laufen (es rechnet immer nur einer).
WARTESCHLANGE_MAX = 3
#: So lange darf ein Schnitt dauern - der Auftrag selbst bricht etwas frueher ab.
RENDER_FRIST = 150
#: So lange darf das Transkript dauern.
WHISPER_FRIST = 35.0
#: Laeuft keine Musik, meldet Flo sich so oft kurz (aufwecken), damit Discord
#: ihm weiter Ton schickt.
WECKEN_ALLE = 120
#: Die Taubheit hoechstens so oft aufheben (ein Neuaufbau setzt sie neu).
TAUB_ALLE = 30
#: So lange wartet _hoeren() hoechstens auf eine fertige Verbindung und eine
#: freie Voice-Sperre der Musik.
HOEREN_WARTEN = 10.0
#: Laeuft ein Handshake (kein Ereignis meldet sein Ende), wird nach SPAETER_SEK,
#: dem Doppelten usw. nochmal abgeglichen - hoechstens SPAETER_MAX Mal am Stueck.
SPAETER_SEK = 1.0
SPAETER_MAX = 4
#: Upload-Budget (darunter liegt Discords Grenze von 10 MiB).
BUDGET = int(9.5 * 1024 * 1024)
BUDGET_RESERVE = 512 * 1024


def _env_mb(name, vorgabe, hoechstens):
    """Megabyte aus der .env -> Bytes, zwischen 1 MB und 'hoechstens'. Ein
    Tippfehler dort darf den Start nicht kippen - dieser Wert wird beim Import
    gelesen, und ein ValueError dort haette den ganzen Bot mitgenommen. Dann
    gilt eben die Vorgabe."""
    roh = (os.getenv(name, "") or "").strip().replace(",", ".")
    try:
        wert = float(roh) if roh else float(vorgabe)
    except ValueError:
        wert = float(vorgabe)
    if not wert > 0:                      # auch NaN
        wert = float(vorgabe)
    return int(max(1.0, min(wert, float(hoechstens))) * 1024 * 1024)


#: Puffer-Deckel je Server und insgesamt. Beim Ueberlaufen fliegt das aelteste
#: Paket zuerst.
GESAMT_BYTES = 160 * 1024 * 1024
PUFFER_BYTES = _env_mb("CLIP_PUFFER_MB", 48, 160)
#: Was ein Paket ausser seinen Opus-Bytes im Speicher kostet (Tupel, Zahlen).
PAKET_EXTRA = 220
#: Hoechstens so viele Sprecher je Server - der am laengsten stille fliegt.
SPRECHER_MAX = 25
#: Flos eigene Spur: so viele 20-ms-Rahmen (gut 5 Minuten).
EIGEN_MAX = 320 * 50
#: Im Dialog: 'Alle' plus hoechstens so viele Personen.
AUSWAHL_MAX = 24
#: Dialoge je Server und je Person.
DIALOGE_JE_SERVER = 3
#: Alle so viele Sekunden eine Zeile je laufender Sitzung ins Log ...
BERICHT_ALLE = 300
#: ... und hoechstens so oft eine Warnung je Server.
WARNEN_ALLE = 600
#: So lange ohne ein einziges Paket, obwohl Leute reden koennten -> Warnung.
STILL_SEK = 180
#: Aufbewahren im Index (Tage), Ansagen (Tage), 'Mikro aus' (Sekunden).
CLIPS_TAGE = 35
ANSAGEN_TAGE = 7
AUS_SEK = 12 * 3600
#: Gruende, bei denen auch laufende Dialoge und Schnitte verworfen werden.
_SPERR_GRUENDE = frozenset(("aus", "feature", "server", "sendepause", "ansage", "ansage_weg",
                            "ende"))
#: Was der Empfaenger als 'roh' mitzaehlt, aber kein Ton ist: Berichte (RTCP),
#: fremde Nutzlast (Video) und Muell. Fuer die Warnungen im Takt zaehlt nur der Rest.
_KEIN_TON = ("rtcp", "fremd", "kaputt")
#: So viele Ton-Pakete in einem Takt, ohne dass eins im Puffer landet -> Warnung.
WARNEN_AB_PAKETEN = 50
#: Gruende, aus denen 'flo clip an' Flo NICHT im Kanal haelt: so bald wird
#: daraus nichts (anders als 'start' - da kommt die Ansage gleich).
_AN_SINNLOS = frozenset(("ansage", "ansage_weg", "server_taub", "client"))

#: custom_ids der dauerhaften Knoepfe.
OHNE_ID = "flo:clip:ohne"
FEUER_PRAEFIX = "flo:clip:feuer:"

_STANDARD = {"opt_out": {}, "ansagen": {}, "aus": {}, "whisper": {}, "guilds": {}}

# --- Grammatik ----------------------------------------------------------------
_RAND = " \t.,!?;:\"'„“”`"
_RAUS = frozenset(("nein", "ohne mich", "raus", "opt-out", "optout", "opt out",
                   "nicht mit mir", "mich nicht", "nicht mich", "ich will nicht",
                   "will ich nicht", "will nicht", "ich nicht", "nee", "nö", "noe"))
_REIN = frozenset(("ja", "mit mir", "rein", "opt-in", "optin", "opt in", "wieder rein"))
#: Hoeflichkeit um die festen Formen herum: 'flo clip nein danke', 'flo clip
#: bitte ohne mich', 'flo clip aus bitte'. Wer raus will, soll nicht an einem
#: 'bitte' scheitern und stattdessen bei der KI landen.
_HOEFLICH = frozenset(("bitte", "danke", "dankeschön", "dankeschoen", "mal", "doch", "pls",
                       "plz", "bidde", "lieber"))
#: 'flo cilp nein': ein Vertipper von 'clip' (vertauscht oder doppelt - wie
#: cmdnorm sie kennt). cmdnorm korrigiert ihn hier nicht ('clip' steht in
#: seinem ZUSTAND, und 'nein' ist kein Argument). Er zaehlt NUR fuer den
#: Ausstieg: wer raus will, soll nicht an einem Buchstabendreher scheitern -
#: zurueck ('ja') geht es bewusst nur richtig geschrieben.
_CLIP_VERTIPPT_RE = re.compile(r"cilp|lcip|clpi|cclip|cllip|cliip|clipp")
_AUS = frozenset(("aus", "stop", "stopp", "beenden", "mikro aus"))
_AN = frozenset(("an", "ein", "start", "starten", "komm", "mikro an"))
_STATUS = frozenset(("status", "info", "diagnose"))
_HILFE = frozenset(("hilfe", "help"))
_BRETT = {"": "woche", "woche": "woche", "der woche": "woche", "monat": "monat",
          "des monats": "monat", "alle": "alle", "top": "top", "jahr": "alle",
          "des jahres": "alle"}
_FUELL = frozenset(("das", "mal", "bitte", "pls", "kurz", "jetzt", "eben", "gerade",
                    "den", "die", "der", "dem", "moment", "was", "davon", "von",
                    "letzte", "letzten", "letztes"))
_SEKUNDEN = frozenset(("s", "sek", "sekunde", "sekunden", "sec", "secs"))
_MINUTEN = frozenset(("m", "min", "mins", "minute", "minuten"))
_NUR_MINUTE = frozenset(("minute", "min"))     # 'flo clip minute' = 60 s
_ICH = frozenset(("mich", "ich"))
_ALLE = frozenset(("alle", "alles"))
_ZAHL_EINHEIT = re.compile(r"([0-9]{1,3})(?:[,.]([0-9]))?(s|sek|sekunde|sekunden|sec|secs|m"
                           r"|min|mins|minute|minuten)?")
#: '1:30' - Minuten und Sekunden.
_MIN_SEK = re.compile(r"([0-9]{1,2}):([0-5][0-9])")
#: 'eine halbe minute', 'ne halbe minute': der Artikel vor 'halbe' ist Fuellung.
_ARTIKEL = frozenset(("eine", "ein", "ne", "'ne"))
_ZAHLWOERTER = {"halbe": 30, "eine": 60, "ein": 60, "einer": 60, "zwei": 120,
                "drei": 180, "vier": 240, "fünf": 300, "fuenf": 300}

# --- Texte (Flos Ton: passiv-aggressiv) -----------------------------------------
ANSAGE = (
    "🎙️ **Ab jetzt hör ich hier mit.**\n"
    "Ich behalte die letzten **5 Minuten** dieses Calls im Kopf – nur im Arbeitsspeicher, "
    "laufend überschrieben. Gespeichert wird nur, was jemand mit `{name} clip` "
    "rausschneidet: das landet als MP3 in {ziel}{transkript}.\n"
    "Kein Bock drauf? **🙅 Ohne mich** drücken oder `{name} clip nein` schreiben – dann "
    "bist du raus, sofort und auf jedem Server (zurück mit `{name} clip ja`). Ganz aus für "
    "diese Runde: **🔇 Für alle aus** oder `{name} clip aus`.\n"
    "-# Gilt auch für alle, die später dazukommen. Clippen dürfen nur Leute aus dem Call "
    "und Mods. Und ja, auch das, was ihr über mich sagt.")

TEXTE = {
    # Zustand
    "aus_gemeldet": ("🔇 **Mitschnitt aus** – {wer} hat abgedreht. Puffer gelöscht. "
                     "Wieder an: `{name} clip an`."),
    "raus": ("Erledigt, du bist raus. Ich hör dich nicht mehr mit, und was ich von dir im "
             "Kopf hatte, ist weg. Zurück mit `{name} clip ja` – falls du's dir anders "
             "überlegst."),
    "schon_raus": "Du bist doch schon raus. Noch rauser geht nicht.",
    "rein": "Okay, du bist wieder drin. Ab jetzt kann man dich clippen. Selbst schuld.",
    "schon_rein": "Du warst nie raus. Entspann dich.",
    "fremd_raus": "Das entscheidet {wer} selbst, nicht du.",
    # Sperren
    "aus_hier": "🎬 Clips sind auf diesem Server aus.",
    "nicht_erlaubt_server": ("Auf diesem Server hör ich nicht mit. Ein Admin kann's "
                             "erlauben: `{name} einstellung clip_aufnahme an`."),
    "kein_voice_flo": ("Ich bin in keinem Voice. Geh rein und sag `{name} clip an`, dann "
                       "hör ich mit."),
    "grund_aus": ("Für diese Runde hat jemand das Mikro abgedreht. `{name} clip an`, wenn "
                  "ihr's wieder wollt."),
    "grund_ansage": "Ich darf im Voice-Chat nicht schreiben – und ohne Ansage nehm ich nix auf.",
    "grund_ansage_weg": ("Irgendwer löscht mir im Voice-Chat dauernd die Ansage weg – und ohne "
                         "Ansage nehm ich nix auf. Lasst sie stehen, dann hör ich auch wieder zu."),
    "grund_sendepause": "Sendepause. Da hör ich auch nicht zu.",
    "grund_keine_sitzung": ("Ich bin nur kurz für einen Sound drin, da hör ich nicht mit. "
                            "`{name} clip an`, wenn ihr clippen wollt."),
    "grund_server_taub": "Mich hat einer vom Server taub gestellt. Taub clippt sich's schlecht.",
    "grund_render": "Mir fehlt ffmpeg mit MP3 – keine Clips, keine Aufnahme.",
    "grund_start": "Ich hab gerade erst angefangen zuzuhören. Gib mir ein paar Sekunden.",
    "grund_client": ("Ich häng hier mit einer Verbindung drin, mit der ich nicht hören kann. "
                     "`{name} leave`, dann `{name} clip an`."),
    "grund_getrennt": "Meine Verbindung wackelt gerade. Gib mir ein paar Sekunden.",
    "grund_ende": "Ich fahr gerade runter. Da wird nix mehr geclippt.",
    # Abfuhren und Grenzen
    "zaungast": "Du warst nicht mal im Call. Clips gibt's nur für Leute, die dabei waren.",
    "zaungast_mikro": "Du sitzt nicht mal im Call. Finger weg vom Mikro, du Zaungast.",
    "selbst_raus": "Selbst nicht mitgeschnitten werden wollen, aber andere clippen? Nö.",
    "abkuehlen": "Chill. Noch {s} s bis zum nächsten Clip.",
    "zu_viele": "Genug geclippt für diese Stunde. Lebt auch mal im Moment.",
    "belegt": "Ich schneid gerade schon was. Eins nach dem anderen.",
    "leer": "Da war nix. Absolute Stille – oder ihr habt alle das Mikro aus.",
    "nur_musik": ("Da hat keiner was gesagt, nur meine Musik lief. Die gibt's auf YouTube, "
                  "nicht bei mir."),
    # EINE Abfuhr, ob die Person raus ist oder gerade am Boden: wer 'flo clip
    # @Bob' tippt, darf daraus nie lesen, dass Bob ein Notsignal geschrieben hat.
    "person_raus": "{wer} clip ich nicht. Such dir wen anders.",
    "person_stumm": "Von {wer} hab ich in der Zeit nix gehört.",
    "mehrere": "Eine Person oder alle. Nicht drei.",
    "zu_lang": "Mehr als 5 Minuten hab ich nicht im Kopf.",
    "bot_ziel": "Bots clip ich nicht.",
    "dialog_offen": "Du hast schon einen Clip-Dialog offen. Erst den da.",
    "dialoge_voll": "Hier sind gerade genug Clip-Dialoge offen. Einer nach dem anderen.",
    # Dialog
    "frage": ("🎬 **Was soll ich clippen?** Länge wählen – und ob alle drauf sollen oder nur "
              "eine Person.\n-# Stand: eben gerade. Ich warte {sek} Sekunden."),
    # Gefragt ausserhalb eines privaten Voice: keine Namen im Menue.
    "frage_privat": ("🎬 **Was soll ich clippen?** Länge wählen. Wer im Voice redet, steht "
                     "hier nicht dran – euer Voice ist privat.\n-# Stand: eben gerade. Ich "
                     "warte {sek} Sekunden."),
    "fremd_knopf": "Finger weg. Das ist nicht dein Clip.",
    "zu_langsam": "⏱️ Zu langsam. Der Moment ist vorbei.",
    "abgebrochen": "✖️ Dann halt nicht. War eh nicht lustig.",
    "schneide": "🎬 Schneide {dauer} … gib mir ein paar Sekunden.",
    "fertig": "🎬 Clip ist raus: {link}",
    "fertig_hier": "🎬 Euer Voice ist privat – der Clip bleibt hier unter euch: {link}",
    # ... und gefragt wurde ausserhalb des Voice-Chats.
    "fertig_privat": "🎬 Clip ist raus – er liegt im Voice-Chat eures Voice, nicht hier: {link}",
    "kuerzer": "Hab erst seit {seit} zugehört – mehr gibt's nicht.",
    # Fehlschlaege
    "render_kaputt": "Beim Zusammenschneiden ist mir ffmpeg abgeschmiert. Nochmal versuchen.",
    "upload_kaputt": "Discord wollte den Clip nicht haben. Nochmal versuchen – oder Admin fragen.",
    "kein_kanal": ("Ich find keinen Kanal, in den ich den Clip legen darf. Admin: "
                   "`{name} einstellung clip_channel #kanal`."),
    "keine_dateien": "Im Clip-Kanal darf ich keine Dateien hochladen. Admin, du bist dran.",
    "privat_zu": ("Euer Voice ist privat, und in eurem Voice-Chat darf ich nichts hochladen. "
                  "Also kein Clip."),
    "verworfen_aus": "Das Mikro wurde inzwischen ausgemacht – Clip verworfen.",
    "verworfen_raus": "Einer aus dem Clip ist inzwischen ausgestiegen – verworfen. Nochmal clippen.",
    "zurueckgehalten": ("Den Clip lass ich liegen. Manche Sachen gehören nicht in einen "
                        "Clip-Kanal."),
    "hetze": "Da ist menschenfeindlicher Müll drin. Den häng ich nicht aus.",
    # Der Post
    "post_kopf": "🎬 **Clip · {dauer}** aus 🔊 **{voice}** · geclippt von {von}",
    "post_dabei": "Mit dabei: {namen}",
    "post_nur": "Nur {wer}.",
    "post_fuss": "-# Nicht jeder ist in jedem Clip zu hören. Raus: `{name} clip nein`",
    # Flammen und Muelleimer
    "feuer_selbst": "Eigenlob stinkt. 🔥 gibt's nur von den anderen.",
    # Der Post steht noch (sonst gaebe es keinen Klick) - nur der Index hat
    # ihn nach CLIPS_TAGE vergessen.
    "feuer_weg": "Der Clip ist aus der Wertung raus – 🔥 zählt nur die ersten {wochen} Wochen.",
    "weg_ok": "Weg ist er.",
    "weg_nein": "Nicht dein Clip, nicht deine Entscheidung.",
    "weg_kaputt": "Löschen ging nicht – mir fehlt da wohl das Recht. Sag's einem Mod.",
    # An / aus
    "aus_ok": ("🔇 Mikro aus. Puffer gelöscht, für diese Runde hör ich nicht mehr mit. "
               "`{name} clip an`, wenn ihr's euch anders überlegt."),
    "aus_schon": "Ich hör doch gar nicht mit.",
    "aus_verworfen": ("Hier hör ich gerade nicht mit – aber was von der Runde noch offen war, "
                      "ist weg. Zufrieden?"),
    "aus_raus": "🔇 Mikro aus, Puffer weg – und weil hier eh nix läuft: ich bin raus.",
    "an_ok": ("🎙️ Bin da und hör mit. Die Ansage steht im Voice-Chat. `{name} clip` zieht "
              "euch einen Clip."),
    "an_schon": "Ich hör schon mit. `{name} clip`, wenn was Lustiges war.",
    "an_kein_voice": "Geh erst in einen Voice, dann komm ich lauschen.",
    "an_woanders": "Ich häng gerade in **{kanal}** – komm rüber.",
    "an_zu_schnell": ("Gerade erst ausgemacht, jetzt schon wieder an? Entscheidet euch. In {s} s "
                      "geht's wieder."),
    "an_ansage_fehlt": ("Im Voice-Chat darf ich nicht schreiben – und ohne Ansage nehm ich nix "
                        "auf. Gebt mir dort Schreibrechte."),
    "aus_tot": "Clips laufen hier gerade gar nicht – mitgeschnitten wird also eh nix.",
    # Status, Bestenliste und Hilfe
    "status_nur_mods": "Das ist für Mods. Du bist keiner.",
    # 'flo clips alle': so weit reicht der Index (CLIPS_TAGE) - kein ganzes Jahr.
    "brett_alle": "🔥 Die besten Clips der letzten {wochen} Wochen",
    "hilfe": ("🎬 **Clips** – solange ich mithöre, hab ich die letzten 5 Minuten aus dem "
              "Voice im Kopf.\n"
              "`{name} clip` – Länge und Person per Knopf · `{name} clip 1m @wer` – direkt\n"
              "`{name} clips` – die Besten der Woche (· `monat` · `alle`)\n"
              "`{name} clip nein` · `ja` – nicht mitgeschnitten werden · wieder rein\n"
              "`{name} clip an` · `aus` – ich komm lauschen · Mikro aus, Puffer weg\n"
              "-# Mehr erklär ich nicht. Ist ein Mikrofon, kein Hexenwerk."),
}

#: Grund aus _soll() -> Textschluessel.
_GRUND_TEXT = {
    "feature": "aus_hier", "server": "nicht_erlaubt_server", "sendepause": "grund_sendepause",
    "render": "grund_render", "nicht_im_voice": "kein_voice_flo", "client": "grund_client",
    "keine_sitzung": "grund_keine_sitzung", "aus": "grund_aus",
    "server_taub": "grund_server_taub", "ansage": "grund_ansage", "start": "grund_start",
    "getrennt": "grund_getrennt", "ende": "grund_ende", "vorab": "grund_start",
    "ansage_weg": "grund_ansage_weg",
}


# =============================================================================
# Kleinteile
# =============================================================================
def _sendepause():
    try:
        import admin
        return bool(admin.is_locked())
    except Exception:  # noqa: BLE001 - ohne admin keine Sendepause
        return False


def _besitzer(uid):
    try:
        import admin
        return int(uid or 0) == int(admin.OWNER_ID)
    except Exception:  # noqa: BLE001
        return False


def _ganz(wert):
    try:
        return int(wert or 0)
    except (TypeError, ValueError):
        return 0


def _wand():
    """Die Wanduhr (fuer clips.json - muss einen Neustart ueberleben)."""
    return time.time()


def _sauber(text, laenge=60):
    """Fuer den Chat: einzeilig, gekuerzt, ohne Markdown und ohne Ping."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) > laenge:
        text = text[:max(1, laenge - 1)].rstrip() + "…"
    return discord.utils.escape_markdown(text).replace("@", "@​")


def _name(person):
    return _sauber(getattr(person, "display_name", None) or getattr(person, "name", None)
                   or "Jemand", 40)


#: Im Clip-Post: wer geclippt hat (Kopfzeile) und wer zu hoeren ist.
_POST_VON_RE = re.compile(r"· geclippt von <@!?([0-9]+)>")
_POST_LEUTE_RE = re.compile(r"^(?:Mit dabei: |Nur )(.+)$", re.MULTILINE)
_ERWAEHNUNG_RE = re.compile(r"<@!?([0-9]+)>")


def _beteiligte_im_post(text):
    """Wer laut dem Clip-Post selbst dazugehoert: wer geclippt hat ('geclippt
    von' in der Kopfzeile) und wer zu hoeren ist ('Mit dabei: ...', 'Nur ...').
    Fuer 🗑️, wenn der Index den Clip nach CLIPS_TAGE vergessen hat - der Post
    steht ja noch, und schreiben kann ihn nur Flo selbst. Ein '@' im
    Kanalnamen ist dort schon entschaerft (_sauber), eine Erwaehnung daraus
    zaehlt nie."""
    text = str(text or "")
    uids = set()
    kopf = text.split("\n", 1)[0]
    treffer = _POST_VON_RE.findall(kopf)
    if treffer:
        uids.add(int(treffer[-1]))
    for zeile in _POST_LEUTE_RE.findall(text):
        uids.update(int(u) for u in _ERWAEHNUNG_RE.findall(zeile))
    return uids


def _vielleicht_clip(inhalt):
    """Schnell raus fuer jede andere Nachricht: steht 'clip' (oder ein
    Vertipper davon, siehe _CLIP_VERTIPPT_RE) ueberhaupt drin?"""
    klein = str(inhalt or "").lower()
    return "clip" in klein or _CLIP_VERTIPPT_RE.search(klein) is not None


def _verbunden(vc):
    try:
        return bool(vc is not None and vc.is_connected())
    except Exception:  # noqa: BLE001
        return False


def _spielt(vc):
    try:
        return bool(vc.is_playing() or vc.is_paused())
    except Exception:  # noqa: BLE001
        return False


def _player(gid):
    """Der Musik-Player dieses Servers - nur nachsehen, nie anlegen."""
    try:
        return music.instance._players.get(int(gid or 0))
    except Exception:  # noqa: BLE001
        return None


def _erst_auf_verdacht(gid):
    """Verbindet die Musik nur auf Verdacht? 'flo spiel' sucht noch und hat
    schon mal verbunden (Music._vorab_verbinden), das Radio oder eine
    Warteschlange laedt erst ihren ersten Song. Scheitert das, ist Flo gleich
    wieder weg - und im Voice-Chat stuende eine Ansage fuer eine Runde, die es
    nie gab. Also erst, wenn wirklich etwas laeuft (oder 'flo clip an' es so
    will). Fuer eine LAUFENDE Runde zaehlt das nie (siehe _soll)."""
    player = _player(gid)
    if player is None or getattr(player, "clip_halten", False):
        return False
    try:
        if player.current is not None or player.ist_pausiert():
            return False
        return bool(getattr(player, "_laufende_anfragen", 0) > 0
                    or getattr(player, "_advancing", False))
    except Exception:  # noqa: BLE001 - im Zweifel: eine echte Sitzung
        return False


def _musik_sperre_belegt(gid):
    """Baut die Musik gerade (neu) auf? Dann nichts an der Stimme drehen."""
    player = _player(gid)
    try:
        return bool(player is not None and player._voice_lock.locked())
    except Exception:  # noqa: BLE001
        return False


def _server_taub(guild):
    stimme = getattr(getattr(guild, "me", None), "voice", None)
    return bool(stimme is not None and getattr(stimme, "deaf", False))


def _ist_mod(wer):
    try:
        return music.Music._ist_mod(wer)
    except Exception:  # noqa: BLE001
        return False


def _stimm_sek(pakete, von, bis):
    """Wie viele Sekunden echte Stimme in diesem Fenster liegen (Stille-Rahmen
    zaehlen nicht)."""
    samples = 0
    for p in pakete:
        if von <= p[0] <= bis and not clipschnitt.ist_stille(p[4]):
            samples += clipschnitt.rahmen_samples(p[4]) or clipschnitt.RAHMEN_STANDARD
    return samples / clipschnitt.RATE


def _flo_ton(eigen, von, bis):
    """Hat Flos eigene Spur in diesem Fenster Ton?"""
    return any(von <= t <= bis and not clipschnitt.ist_stille(o) for t, o in eigen)


def _notlage_melden(gid, uid):
    """Ein Notsignal im Transkript zaehlt wie eins im Chat (bot.on_message):
    basis merkt es sich (72 Stunden, nur im Speicher), und nachhaken vergisst
    alles Offene dieser Person und fragt tagelang nichts nach - ein "na, wie
    lief die Pruefung?" waere danach immer falsch. nachhaken wird beim Aufruf
    ueber sys.modules gefragt: ohne nachhaken gibt es dort auch nichts Offenes."""
    basis.notlage_merken(uid)
    nachhaken = sys.modules.get("nachhaken")
    if nachhaken is not None:
        try:
            nachhaken.notlage(gid, uid)
        except Exception:  # noqa: BLE001 - die Sperre in basis steht trotzdem
            log.exception("Clips: Notsignal an nachhaken ging nicht")


def _ton_pakete(zaehler):
    """Pakete, die Ton sein koennten: alles Rohe ohne RTCP, Video und Muell."""
    return zaehler.get("roh", 0) - sum(zaehler.get(k, 0) for k in _KEIN_TON)


async def _kurz(interaction, text):
    """Eine Antwort nur fuer den, der geklickt hat - und nie ein Absturz."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("Clips: Knopf-Antwort ging nicht", exc_info=True)


def _task_fehler(task):
    if task.cancelled():
        return
    fehler = task.exception()
    if fehler is not None:
        log.error("Clips: Hintergrund-Aufgabe gescheitert: %s: %s", type(fehler).__name__,
                  fehler, exc_info=fehler)


# =============================================================================
# Der Puffer
# =============================================================================
class _Konto:
    """Was alle Puffer zusammen belegen (fuer den Gesamtdeckel)."""

    def __init__(self):
        self.bytes = 0


class Spur:
    """Die Pakete EINER Person im Puffer."""

    __slots__ = ("uid", "name", "bot", "pakete", "bytes", "zuletzt")

    def __init__(self, uid, name, bot):
        self.uid = uid
        self.name = name
        self.bot = bot
        self.pakete = deque()     # (t, strang, seq, ts, opus)
        self.bytes = 0
        self.zuletzt = 0.0


@dataclass
class Schnappschuss:
    """Was im Moment der Frage im Puffer lag - nur einfache Daten."""

    gid: int
    kanal_id: int
    bis: float
    stimmen: dict             # uid -> (name, bot, [(t, strang, seq, ts, opus)])
    flo: list                 # [(t, opus)]
    seit: float
    sperre_gen: int

    def vergessen(self, uid):
        self.stimmen.pop(uid, None)


class Ringpuffer:
    """Die letzten HALTEN_SEK eines Calls, je Person, nur im Arbeitsspeicher.

    einfuegen() ist die Senke des Empfaengers (Loop-Thread), eigen_anhaengen()
    die Senke fuer Flos eigene Rahmen (AudioPlayer-Thread - haengt nur an, eine
    deque ist dafuer sicher). Alles andere laeuft im Loop-Thread."""

    def __init__(self, gid, kanal_id, *, raus=None, kennen=None, konto=None,
                 uhr_=time.monotonic, max_bytes=None):
        self.gid = gid
        self.kanal_id = kanal_id
        self.raus = raus or (lambda _uid: False)
        self.kennen = kennen or (lambda _uid: ("Jemand", False))
        self.konto = konto if konto is not None else _Konto()
        self.max_bytes = PUFFER_BYTES if max_bytes is None else max_bytes
        self.spuren = {}
        self.eigen = deque(maxlen=EIGEN_MAX)    # (t, opus)
        self.bytes = 0
        self.seit = uhr_()

    # -- rein ----------------------------------------------------------------
    def einfuegen(self, uid, strang, seq, ts, opus, t):
        """Senke des Empfaengers. Wer raus ist, kommt nie hinein - auch nicht,
        wenn der Empfaenger es einmal nicht wusste."""
        if self.raus(uid):
            return
        spur = self.spuren.get(uid)
        if spur is None:
            if len(self.spuren) >= SPRECHER_MAX:
                self._stillster_weg()
            try:
                name, bot = self.kennen(uid)
            except Exception:  # noqa: BLE001 - ein Name ist kein Grund, Ton zu verlieren
                name, bot = "Jemand", False
            spur = self.spuren[uid] = Spur(uid, name, bot)
        n = len(opus) + PAKET_EXTRA
        spur.pakete.append((t, strang, seq, ts, opus))
        spur.bytes += n
        spur.zuletzt = t
        self.bytes += n
        self.konto.bytes += n
        while ((self.bytes > self.max_bytes or self.konto.bytes > GESAMT_BYTES)
               and self._aeltestes_weg()):
            pass

    def eigen_anhaengen(self, opus, t):
        """Senke fuer Flos eigene Rahmen (EmpfangsClient.eigen_senke). Nur
        anhaengen - laeuft im AudioPlayer-Thread."""
        self.eigen.append((t, opus))

    # -- raus ----------------------------------------------------------------
    def _abziehen(self, spur, n):
        spur.bytes -= n
        self.bytes -= n
        self.konto.bytes -= n

    def _aeltestes_weg(self):
        """Das aelteste Paket ueberhaupt fliegt. -> ob es eins gab."""
        beste = None
        for spur in self.spuren.values():
            if spur.pakete and (beste is None or spur.pakete[0][0] < beste.pakete[0][0]):
                beste = spur
        if beste is None:
            return False
        paket = beste.pakete.popleft()
        self._abziehen(beste, len(paket[4]) + PAKET_EXTRA)
        if not beste.pakete:
            self._spur_weg(beste.uid)
        return True

    def _stillster_weg(self):
        """Platz fuer einen neuen Sprecher: wer am laengsten nichts sagte, fliegt."""
        if self.spuren:
            self._spur_weg(min(self.spuren.values(), key=lambda s: s.zuletzt).uid)

    def _spur_weg(self, uid):
        spur = self.spuren.pop(uid, None)
        if spur is not None:
            self.bytes -= spur.bytes
            self.konto.bytes -= spur.bytes
            spur.pakete.clear()

    def trimmen(self, jetzt):
        """Alles, was aelter als HALTEN_SEK ist, fliegt."""
        grenze = jetzt - HALTEN_SEK
        for spur in list(self.spuren.values()):
            while spur.pakete and spur.pakete[0][0] < grenze:
                paket = spur.pakete.popleft()
                self._abziehen(spur, len(paket[4]) + PAKET_EXTRA)
            if not spur.pakete:
                self._spur_weg(spur.uid)
        while self.eigen and self.eigen[0][0] < grenze:
            self.eigen.popleft()

    def vergessen(self, uid):
        """Opt-out: alles von dieser Person sofort weg."""
        self._spur_weg(uid)

    def leeren(self):
        for uid in list(self.spuren):
            self._spur_weg(uid)
        self.eigen.clear()

    # -- raus fuer den Schnitt ---------------------------------------------------
    def schnappschuss(self, bis, sperre_gen=0):
        """Kopie dessen, was im Fenster [bis - PUFFER_SEK, bis] liegt (mit etwas
        Rand fuer den Jitter). Flos Spur wird als Ganzes kopiert - der
        Player-Thread haengt waehrenddessen an."""
        von = bis - PUFFER_SEK - 1.0
        stimmen = {}
        for uid, spur in self.spuren.items():
            if self.raus(uid):
                continue
            pakete = [p for p in spur.pakete if von <= p[0] <= bis + 0.3]
            if pakete:
                stimmen[uid] = (spur.name, spur.bot, pakete)
        flo = [e for e in self.eigen.copy() if von <= e[0] <= bis + 0.3]
        return Schnappschuss(gid=self.gid, kanal_id=self.kanal_id, bis=bis, stimmen=stimmen,
                             flo=flo, seit=self.seit, sperre_gen=sperre_gen)

    # -- Auskunft (nur Zahlen) -----------------------------------------------------
    def stimm_sek(self, uid, von, bis):
        spur = self.spuren.get(uid)
        return _stimm_sek(spur.pakete, von, bis) if spur is not None else 0.0

    def sprecher_mit_ton(self):
        return sum(1 for s in self.spuren.values() if any(
            not clipschnitt.ist_stille(p[4]) for p in s.pakete))

    def eigen_sek(self):
        return sum(1 for _t, o in list(self.eigen) if not clipschnitt.ist_stille(o)) * 0.02

    def megabyte(self):
        eigen = sum(len(o) + 64 for _t, o in list(self.eigen))
        return (self.bytes + eigen) / 1048576


# =============================================================================
# Sitzung je Server
# =============================================================================
class Sitzung:
    """Was clips je Server ueber das Mithoeren weiss. Lebt so lange wie der
    Prozess - der Puffer darin nur, solange zugehoert wird."""

    def __init__(self, gid):
        self.gid = gid
        self.aktiv = False
        self.kanal_id = None
        self.seit = 0.0
        self.puffer = None
        self.empfaenger = None
        self.grund = "start"
        #: (kanal_id, bis[, grund]): dort ging die Ansage nicht raus (oder wird
        #: dauernd geloescht: 'ansage_weg') - bis dahin kein Versuch.
        self.ansage_fehlt = None
        self.ansage_msg = None
        self.taub_von_uns = False
        self.taub_um = float("-inf")
        #: Fuer welchen Client (id) die Taubheit zuletzt aufgehoben wurde - ein
        #: NEUER (die Musik baut neu auf, und zwar taub) bekommt sofort Ohren.
        self.taub_fuer = None
        self.geweckt_fuer = None
        self.letzte_weckung = float("-inf")
        self.getrennt_seit = None
        #: Zaehlt hoch, wenn Mikro aus, Funktion aus usw. - offene Dialoge und
        #: laufende Schnitte mit einem alten Stand werden dann verworfen.
        self.sperre_gen = 0
        self.lock = None
        self.lock_loop = None
        #: Der letzte Kanal, in dem Flo in einer Musik-Sitzung sass (fuer das
        #: Ende von 'Mikro aus').
        self.kanal_zuletzt = None
        self.hoeren_task = None
        #: Fuer welchen Client hoeren_task laeuft (ein neuer bekommt einen neuen).
        self.hoeren_vc = None
        # Fuer Status und Log: Zaehlerstaende des Empfaengers.
        self.verlauf = deque(maxlen=12)      # (t, zaehler)
        self.bericht_um = 0.0
        self.zaehler_bericht = {}
        self.warn_um = float("-inf")
        self.ton_um = 0.0                     # wann zuletzt ein Ton-Paket ankam
        self.ton_zuletzt = 0                  # _ton_pakete() beim letzten Takt
        self.ansage_geprueft = 0.0            # wann der Takt zuletzt nachsah
        #: Wann eine verschwundene Ansage zuletzt neu gepostet wurde (Bremse).
        self.ansage_neu_um = float("-inf")


# =============================================================================
# Knoepfe, die einen Neustart ueberleben
# =============================================================================
# Die Ansage und jeder Clip-Post tragen DynamicItems mit fester custom_id. Die
# Views laufen mit timeout=None und werden NIE gestoppt: stop() nimmt die
# Vorlagen global aus discord.py, danach waeren die Knoepfe ALLER Ansagen tot.
class ClipOhneMich(discord.ui.DynamicItem[discord.ui.Button], template=r"flo:clip:ohne"):
    """🙅 Ohne mich - geht IMMER: auch in der Sendepause und wo Clips aus sind."""

    def __init__(self):
        super().__init__(discord.ui.Button(label="Ohne mich", emoji="🙅",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=OHNE_ID))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction):
        await instance.ohne_mich_knopf(interaction)


class ClipAus(discord.ui.DynamicItem[discord.ui.Button],
              template=r"flo:clip:aus:(?P<vc>[0-9]{1,20})"):
    """🔇 Fuer alle aus - Mikro aus fuer diese Runde in DIESEM Voice."""

    def __init__(self, vc_id):
        self.vc_id = int(vc_id)
        super().__init__(discord.ui.Button(label="Für alle aus", emoji="🔇",
                                           style=discord.ButtonStyle.danger,
                                           custom_id=f"flo:clip:aus:{self.vc_id}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["vc"])

    async def callback(self, interaction):
        await instance.aus_knopf(interaction, self.vc_id)


class ClipFeuer(discord.ui.DynamicItem[discord.ui.Button],
                template=r"flo:clip:feuer:(?P<cid>[0-9a-z]{8})"):
    """🔥 n - eine Flamme fuer den Clip (nochmal klicken nimmt sie zurueck)."""

    def __init__(self, cid, n=0):
        self.cid = cid
        super().__init__(discord.ui.Button(label=str(max(0, int(n))), emoji="🔥",
                                           style=discord.ButtonStyle.secondary,
                                           custom_id=f"{FEUER_PRAEFIX}{cid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"], _ganz(getattr(item, "label", 0)))

    async def callback(self, interaction):
        await instance.feuer_knopf(interaction, self.cid)


class ClipWeg(discord.ui.DynamicItem[discord.ui.Button],
              template=r"flo:clip:weg:(?P<cid>[0-9a-z]{8})"):
    """🗑️ - wer geclippt hat, wer drin ist, und Mods. Geht immer (Datenschutz)."""

    def __init__(self, cid):
        self.cid = cid
        super().__init__(discord.ui.Button(emoji="🗑️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"flo:clip:weg:{cid}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["cid"])

    async def callback(self, interaction):
        await instance.weg_knopf(interaction, self.cid)


#: bot.setup_hook meldet sie an (bot.DYNAMISCHE_MODULE).
DYNAMISCHE_KNOEPFE = (ClipOhneMich, ClipAus, ClipFeuer, ClipWeg)


class AnsageView(discord.ui.View):
    """Die Knoepfe unter der Ansage. NIE stop() (siehe oben)."""

    def __init__(self, vc_id):
        super().__init__(timeout=None)
        self.add_item(ClipOhneMich())
        self.add_item(ClipAus(vc_id))


class ClipView(discord.ui.View):
    """🔥 und 🗑️ unter einem Clip. NIE stop()."""

    def __init__(self, cid, n=0):
        super().__init__(timeout=None)
        self.add_item(ClipFeuer(cid, n))
        self.add_item(ClipWeg(cid))


class ClipDialog(discord.ui.View):
    """'flo clip' ohne Laenge: Laenge per Knopf, Alle oder eine Person per Menue.

    Gilt nur fuer den, der gefragt hat. Der Schnappschuss stammt aus dem Moment
    der Frage - wer erst nach 40 Sekunden klickt, bekommt trotzdem das, was
    VOR der Frage lief. Ein Riegel (_fertig) faengt den Doppelklick, bevor auch
    nur ein await kommt. Keine DynamicItems, also darf stop() hier sein."""

    def __init__(self, guild, besitzer, snap, sprecher, *, vorauswahl=None, anfrage=None):
        super().__init__(timeout=DIALOG_SEK)
        self.guild = guild
        self.besitzer = besitzer
        self.snap = snap
        self.wahl = vorauswahl
        self.anfrage = anfrage
        self.message = None
        self.erstellt = time.monotonic()
        self._fertig = False
        optionen = [discord.SelectOption(label="Alle", value="alle", emoji="👥",
                                         default=vorauswahl is None)]
        for uid, name in list(sprecher)[:AUSWAHL_MAX]:
            optionen.append(discord.SelectOption(label=str(name)[:100] or "?", value=str(uid),
                                                 default=uid == vorauswahl))
        self.auswahl = discord.ui.Select(placeholder="Wer soll drauf?", options=optionen,
                                         row=0)
        self.auswahl.callback = self._gewaehlt
        self.add_item(self.auswahl)
        for sek, text in ((30, "30 s"), (60, "1 min"), (300, "5 min")):
            knopf = discord.ui.Button(label=text, style=discord.ButtonStyle.primary, row=1)
            knopf.callback = self._laenge(sek)
            self.add_item(knopf)
        weg = discord.ui.Button(emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
        weg.callback = self._abbrechen
        self.add_item(weg)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.besitzer.id:
            return True
        await _kurz(interaction, TEXTE["fremd_knopf"])
        return False

    def vergessen(self, uid):
        """Opt-out, waehrend der Dialog offen ist: raus aus dem Schnappschuss."""
        if self.snap is not None:
            self.snap.vergessen(uid)

    async def _gewaehlt(self, interaction):
        werte = list(self.auswahl.values or ())
        wert = werte[0] if werte else "alle"
        self.wahl = None if wert == "alle" else _ganz(wert) or None
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

    def _laenge(self, sek):
        async def klick(interaction):
            if self._fertig:
                try:
                    await interaction.response.defer()
                except discord.HTTPException:
                    pass
                return
            self._fertig = True             # VOR dem ersten await: kein zweiter Schnitt
            self.stop()
            try:
                await interaction.response.edit_message(
                    content=TEXTE["schneide"].format(dauer=clipstext.dauer_text(sek)), view=None)
            except discord.HTTPException:
                log.debug("Clips: Dialog liess sich nicht umschreiben", exc_info=True)
            await instance.dialog_schneiden(self, sek)
        return klick

    async def _abbrechen(self, interaction):
        if self._fertig:
            try:
                await interaction.response.defer()
            except discord.HTTPException:
                pass
            return
        self._fertig = True
        self.stop()
        self.snap = None
        instance.dialog_weg(self)
        try:
            await interaction.response.edit_message(content=TEXTE["abgebrochen"], view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        if self._fertig:
            return
        self._fertig = True
        self.snap = None
        instance.dialog_weg(self)
        if self.message is not None:
            try:
                await self.message.edit(content=TEXTE["zu_langsam"], view=None)
            except discord.HTTPException:
                pass


# =============================================================================
# Das Feature
# =============================================================================
class Clips(FeatureBasis):
    """Mithoeren, Ansage, Opt-out, Befehle, Dialog, Schnitt, Post, Flammen."""

    def __init__(self):
        self._enabled = False
        self._store = None
        self._kann_rendern = False
        self._sitzungen = {}          # gid -> Sitzung
        self._raus_ids = set()        # wer raus ist (schnell fuer jedes Paket)
        self._dialoge = {}            # gid -> {uid: ClipDialog}
        #: (gid, uid): ein Dialog ist auf dem Weg (zwischen Pruefung und Post).
        self._dialog_kommt = set()
        self._zuletzt = {}            # uid -> Zeit des letzten Clips (Abkuehlen)
        self._stunde = {}             # gid -> deque[Zeit]
        self._in_arbeit = 0
        #: gid -> {voice_kanal_id: wie viele Schnitte von dort unterwegs sind}
        #: (vom Schnappschuss bis zum Post) - fuer 'Mikro aus' nach der Runde.
        self._laufend = {}
        self._render_sperre = None
        self._render_loop = None
        self._abbruch = False
        self._tasks = set()
        self._spaeter_geplant = set()
        self._spaeter_n = {}          # gid -> wie oft am Stueck nachgesehen
        self._konto = _Konto()
        #: Die Uhr fuer Puffer und Fenster (monotonic). Tests stellen sie.
        self._uhr = time.monotonic
        self._aufgeraeumt_um = float("-inf")
        self._kontingent_stand = None

    # --- Lebenszyklus -----------------------------------------------------
    def _datenschutz_oeffnen(self):
        """Der Ausstieg gilt IMMER - auch wenn die Clips hier gar nicht
        hochkommen (aus, ohne Musik, discord.py passt nicht, libopus fehlt):
        wer 🙅 drueckt oder 'flo clip nein' schreibt, wird gemerkt und bleibt
        raus, wenn sie spaeter wieder laufen. Dafuer reicht der Speicher."""
        if self._store is None:
            self._store = JsonStore("clips.json", default=_STANDARD)
        self._raus_ids = {_ganz(u) for u in self._daten("opt_out")} - {0}

    def nur_datenschutz(self):
        """bot.py, wenn die Clips gar nicht laufen (ohne Musik): nur der
        Ausstieg. -> False (wie setup ohne Clips)."""
        self._enabled = False
        self._datenschutz_oeffnen()
        log.info("Clips aus (ohne Musik) - 'flo clip nein' und 🙅 gelten trotzdem.")
        return False

    def setup(self):
        self._enabled = False
        self._datenschutz_oeffnen()
        if os.getenv("CLIPS_ENABLED", "1").strip().lower() in (
                "0", "false", "no", "off", "aus"):
            log.info("Clips aus (CLIPS_ENABLED=0) - 'flo clip nein' und 🙅 gelten trotzdem.")
            return False
        grund = voiceempfang.kompatibel()
        if grund:
            log.warning("Clips: Empfang passt nicht zu diesem discord.py (%s) - Clips bleiben "
                        "aus.", grund)
            return False
        if not music._opus_da():
            log.warning("Clips: libopus fehlt - ohne die kein Empfang. Clips bleiben aus.")
            return False
        render = clipschnitt.ffmpeg_pruefen()
        self._kann_rendern = render == ""
        if not self._kann_rendern:
            log.warning("Clips: %s - die Befehle gehen, mitgehoert wird aber nicht.", render)
        self._sitzungen = {}
        self._dialoge = {}
        self._dialog_kommt = set()
        self._zuletzt = {}
        self._stunde = {}
        self._in_arbeit = 0
        self._laufend = {}
        self._abbruch = False
        self._konto = _Konto()
        self._aufgeraeumt_um = float("-inf")
        self._aufraeumen()
        guildcfg.horcht_auf("clip_aufnahme", _einstellung_neu)
        clipschnitt.alte_reste_weg()
        clipstext.transkriber.kontingent.aus_dict(self._store.data.get("whisper"))
        self._kontingent_stand = None
        self._enabled = True
        anbieter = clipstext.transkriber.anbieter()
        log.info("Clips aktiv (Empfang ok, MP3 %s, Transkript: %s). Mithören ab Werk nur auf "
                 "dem Hauptserver.", "ok" if self._kann_rendern else "fehlt",
                 anbieter or "aus")
        return True

    def is_enabled(self):
        return self._enabled

    async def herunterfahren(self):
        """Beim Abmelden: alle Puffer leer, laufende Schnitte abbrechen, Temp-
        Ordner weg, Kontingent und Index auf die Platte. Danach faengt keine
        Sitzung mehr an (_soll: 'ende') - bis zum Abmelden koennen noch
        Voice-Ereignisse und ein Takt kommen."""
        if not self._enabled or self._store is None:
            return
        self._abbruch = True
        for gid, s in list(self._sitzungen.items()):
            if s.aktiv or s.puffer is not None:
                self._leeren(self._guild(gid), s, "ende")
                log.info("Clips: Server %s – Puffer geleert (herunterfahren).", gid)
        for dialoge in self._dialoge.values():
            for dialog in list(dialoge.values()):
                dialog.snap = None
        self._dialoge = {}
        clipschnitt.alte_reste_weg()
        try:
            await clipstext.transkriber.schliessen()
        except Exception:  # noqa: BLE001
            log.debug("Clips: Whisper-Client nicht geschlossen", exc_info=True)
        self._store.data["whisper"] = clipstext.transkriber.kontingent.als_dict()
        await self._store.save()

    # --- Haken fuer music (ueber sys.modules) ------------------------------------
    def empfangs_klasse(self):
        """Die Client-Klasse fuer channel.connect - None, solange clips aus ist
        (dann verbindet alles exakt wie vorher)."""
        return voiceempfang.EmpfangsClient if self._enabled else None

    def sitzung_geaendert(self, gid):
        """Die Musik hat verbunden oder getrennt: gleich abgleichen."""
        if not self._enabled:
            return
        guild = self._guild(gid)
        if guild is not None:
            self._spawn(self.abgleichen(guild))

    def laeuft_auf(self, gid):
        """music._fremder_besuch fragt: koennte clips auf diesem Server
        mithoeren (clips laeuft, Funktion und Einwilligung des Servers an)?
        Nur dann adoptiert die Musik keinen fremden Sound-Besuch - ueberall
        sonst verhaelt sie sich exakt wie ohne clips."""
        if not self._enabled or self._store is None:
            return False
        try:
            gid = int(gid)
        except (TypeError, ValueError):
            return False
        return bool(features.is_on_in(gid, "clips") and guildcfg.an(gid, "clip_aufnahme"))

    def haelt(self, gid, kanal_id):
        """music._leerlauf fragt: soll 'flo clip an' Flo in diesem Kanal halten?
        Nur, solange clips dort mithoeren DARF - Funktion und Einwilligung an,
        keine Sendepause, ffmpeg da, kein 'Mikro aus' fuer den Kanal, Flo nicht
        vom Server taub gestellt, kein fremder Client, die Ansage nicht gerade
        gescheitert (die Pruefungen aus _soll, die nicht an der Verbindung
        haengen). Sonst gilt der normale Leerlauf, statt dass Flo ewig taub
        herumsitzt. Nach einem Abriss haelt es weiter: der Watchdog holt ihn
        zurueck, und clips hoert wieder mit."""
        if not self._enabled or self._store is None or self._abbruch:
            return False
        try:
            gid, kanal_id = int(gid), int(kanal_id)
        except (TypeError, ValueError):
            return False
        if (not features.is_on_in(gid, "clips") or not guildcfg.an(gid, "clip_aufnahme")
                or _sendepause() or not self._kann_rendern):
            return False
        if str(kanal_id) in self._daten("aus"):
            return False
        guild = self._guild(gid)
        if guild is not None:
            if _server_taub(guild):
                return False
            vc = getattr(guild, "voice_client", None)
            if _verbunden(vc) and not isinstance(vc, voiceempfang.EmpfangsClient):
                return False
        s = self._sitzungen.get(gid)
        fehlt = s.ansage_fehlt if s is not None else None
        return not (fehlt is not None and fehlt[0] == kanal_id and time.monotonic() < fehlt[1])

    # --- Kleinkram ------------------------------------------------------------
    def _spawn(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_task_fehler)
        return task

    @staticmethod
    def _guild(gid):
        client = laufzeit.client
        try:
            return client.get_guild(int(gid)) if client is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _sitzung(self, gid):
        gid = int(gid)
        s = self._sitzungen.get(gid)
        if s is None:
            s = self._sitzungen[gid] = Sitzung(gid)
        return s

    @staticmethod
    def _sperre(s):
        """Die asyncio-Sperre der Sitzung - je Event-Loop eine (Tests fahren
        mehrere Loops nacheinander)."""
        loop = asyncio.get_running_loop()
        if s.lock is None or s.lock_loop is not loop:
            s.lock = asyncio.Lock()
            s.lock_loop = loop
        return s.lock

    def _render_semaphore(self):
        loop = asyncio.get_running_loop()
        if self._render_sperre is None or self._render_loop is not loop:
            self._render_sperre = asyncio.Semaphore(1)
            self._render_loop = loop
        return self._render_sperre

    def _spaeter(self, guild):
        """Gleich nochmal abgleichen - ein Handshake laeuft gerade, und wann er
        fertig ist, meldet kein Ereignis. Mit wachsendem Abstand (SPAETER_SEK,
        das Doppelte, ...) und hoechstens SPAETER_MAX Mal am Stueck; danach
        uebernimmt der 15-s-Takt. Ein Abgleich, der hoert oder aufhoert, setzt
        den Zaehler zurueck."""
        gid = getattr(guild, "id", None)
        if gid is None or gid in self._spaeter_geplant:
            return
        n = self._spaeter_n.get(gid, 0)
        if n >= SPAETER_MAX:
            return
        self._spaeter_n[gid] = n + 1

        async def nochmal():
            try:
                await asyncio.sleep(SPAETER_SEK * (2 ** n))
            finally:
                self._spaeter_geplant.discard(gid)
            await self.abgleichen(guild)

        self._spaeter_geplant.add(gid)
        if self._spawn(nochmal()) is None:
            self._spaeter_geplant.discard(gid)

    # --- Speicher ---------------------------------------------------------------
    def _daten(self, schluessel):
        daten = self._store.data.get(schluessel)
        if not isinstance(daten, dict):
            daten = self._store.data[schluessel] = {}
        return daten

    def _clips_von(self, gid, anlegen=False):
        guilds = self._daten("guilds")
        g = guilds.get(str(int(gid or 0)))
        if not isinstance(g, dict):
            if not anlegen:
                return {}
            g = guilds[str(int(gid or 0))] = {}
        clips = g.get("clips")
        if not isinstance(clips, dict):
            if not anlegen:
                return {}
            clips = g["clips"] = {}
        return clips

    def _aufraeumen(self, jetzt=None):
        """Clips nach CLIPS_TAGE, Ansagen nach ANSAGEN_TAGE, 'aus' nach AUS_SEK."""
        jetzt = _wand() if jetzt is None else jetzt
        weg = 0
        for name, frist in (("ansagen", ANSAGEN_TAGE * 86400), ("aus", AUS_SEK)):
            daten = self._daten(name)
            for cid, wert in list(daten.items()):
                ts = wert.get("ts") if isinstance(wert, dict) else wert
                try:
                    ts = basis.glaubhafte_zeit(float(ts), jetzt)
                except (TypeError, ValueError):
                    ts = 0.0
                if jetzt - ts > frist:
                    del daten[cid]
                    weg += 1
        for g in self._daten("guilds").values():
            clips = g.get("clips") if isinstance(g, dict) else None
            if not isinstance(clips, dict):
                continue
            for cid, eintrag in list(clips.items()):
                ts = _ganz(eintrag.get("ts")) if isinstance(eintrag, dict) else 0
                if jetzt - ts > CLIPS_TAGE * 86400:
                    del clips[cid]
                    weg += 1
        if weg:
            self._store.save_soon()
        self._aufgeraeumt_um = time.monotonic()
        return weg

    def _kontingent_merken(self):
        stand = clipstext.transkriber.kontingent.als_dict()
        if stand != self._kontingent_stand:
            self._kontingent_stand = stand
            self._store.data["whisper"] = stand
            self._store.save_soon()

    # --- Opt-out ------------------------------------------------------------------
    def ist_raus(self, uid):
        """Will diese Person nicht mitgeschnitten werden? (Fuer JEDES Paket -
        deshalb eine Menge statt des Speichers.)"""
        try:
            return int(uid) in self._raus_ids
        except (TypeError, ValueError):
            return True

    def raus(self, uid):
        """Raus - sofort, auf jedem Server, mitsamt allem, was schon im Puffer
        lag, was gehalten wird und was in offenen Dialogen steckt."""
        uid = int(uid)
        self._daten("opt_out")[str(uid)] = round(_wand())
        self._raus_ids.add(uid)
        self._store.save_soon()
        for s in self._sitzungen.values():
            if s.puffer is not None:
                s.puffer.vergessen(uid)
            if s.empfaenger is not None:
                s.empfaenger.vergessen(uid)
        for dialoge in self._dialoge.values():
            for dialog in dialoge.values():
                dialog.vergessen(uid)
        log.info("Clips: jemand ist ausgestiegen (%d raus).", len(self._raus_ids))

    def rein(self, uid):
        uid = int(uid)
        self._daten("opt_out").pop(str(uid), None)
        self._raus_ids.discard(uid)
        self._store.save_soon()
        log.info("Clips: jemand ist wieder drin (%d raus).", len(self._raus_ids))

    # --- Ob und wo zugehoert wird ----------------------------------------------------
    def _soll(self, guild, s):
        """(soll, grund, vc) - in GENAU dieser Reihenfolge geprueft (davor nur
        das Herunterfahren: danach faengt nichts mehr an)."""
        gid = guild.id
        if self._abbruch:
            return False, "ende", None
        if not features.is_on_in(gid, "clips"):
            return False, "feature", None
        if not guildcfg.an(gid, "clip_aufnahme"):
            return False, "server", None
        if _sendepause():
            return False, "sendepause", None
        if not self._kann_rendern:
            return False, "render", None
        vc = getattr(guild, "voice_client", None)
        if vc is None or not _verbunden(vc):
            return False, "nicht_im_voice", vc
        if not isinstance(vc, voiceempfang.EmpfangsClient):
            return False, "client", vc
        if not music.gehoert_der_musik(gid, vc):
            return False, "keine_sitzung", vc
        cid = getattr(getattr(vc, "channel", None), "id", None)
        if cid is None:
            return False, "nicht_im_voice", vc
        if str(cid) in self._daten("aus"):
            return False, "aus", vc
        if _server_taub(guild):
            return False, "server_taub", vc
        fehlt = s.ansage_fehlt
        if fehlt is not None and fehlt[0] == cid and time.monotonic() < fehlt[1]:
            return False, (fehlt[2] if len(fehlt) > 2 else "ansage"), vc
        if not (s.aktiv and s.kanal_id == cid) and _erst_auf_verdacht(gid):
            return False, "vorab", vc
        return True, "", vc

    async def abgleichen(self, guild):
        """Soll hier gerade zugehoert werden - und wird es? Angleichen. Gerufen
        vom Musik-Haken, von op 4, von Flos Voice-Ereignissen, vom Takt, von
        Befehlen und Einstellungen. Je Server nie zweimal gleichzeitig."""
        if not self._enabled or self._store is None or guild is None:
            return
        s = self._sitzung(guild.id)
        async with self._sperre(s):
            await self._abgleichen(guild, s)

    async def _abgleichen(self, guild, s):
        soll, grund, vc = self._soll(guild, s)
        self._aus_ende(guild, s)
        if soll:
            kanal = vc.channel
            s.getrennt_seit = None
            if s.aktiv and s.kanal_id != kanal.id:
                await self._stoppen(guild, s, "kanalwechsel")
            if not s.aktiv:
                if not await self._ankuendigen(guild, kanal, s):
                    s.grund = "ansage"
                    s.ansage_fehlt = (kanal.id, time.monotonic() + ANSAGE_FEHLT_PAUSE)
                    log.info("Clips: Server %s – Ansage in Kanal %s ging nicht, ich höre "
                             "nicht mit.", guild.id, kanal.id)
                    return
                # Waehrend der Ansage kann sich alles geaendert haben (verschoben,
                # raus, Mikro aus). Dann erst der naechste Abgleich.
                soll, grund, vc2 = self._soll(guild, s)
                if not soll or vc2 is not vc or getattr(vc.channel, "id", None) != kanal.id:
                    s.grund = grund or "start"
                    self._spaeter(guild)
                    return
                self._starten(guild, vc, s)
            else:
                s.empfaenger.anhaengen(vc)
                vc.eigen_senke = s.puffer.eigen_anhaengen
            s.grund = ""
            self._spaeter_n.pop(guild.id, None)
            self._hoeren_planen(guild, vc, s)
            return
        if s.aktiv:
            player = _player(guild.id)
            if (grund in ("nicht_im_voice", "client", "keine_sitzung") and player is not None
                    and player.active_channel_id == s.kanal_id):
                # Die Musik baut gerade neu auf (oder kommt gleich wieder): der
                # Puffer bleibt eine Weile, gehoert wird solange nicht.
                jetzt = time.monotonic()
                if s.getrennt_seit is None:
                    s.getrennt_seit = jetzt
                    log.info("Clips: Server %s – Verbindung weg (%s), Puffer wartet %d s.",
                             guild.id, grund, GNADE_SEK)
                if s.empfaenger is not None:
                    s.empfaenger.abhaengen()
                if jetzt - s.getrennt_seit < GNADE_SEK:
                    s.grund = "getrennt"
                    if vc is not None:
                        self._spaeter(guild)
                    return
                grund = "getrennt"
            await self._stoppen(guild, s, grund)
        elif s.taub_von_uns:
            # Ein frueheres Taubstellen kam nicht durch (die Musik hielt ihre
            # Sperre zu lange): jetzt nochmal - sonst saesse Flo hoerend da,
            # ohne zuzuhoeren, und das Kopfhoerer-Symbol luege.
            await self._wieder_taub(guild, s)
        s.grund = grund
        if grund == "vorab":
            # Ob aus dem Verdacht eine Runde wird, meldet kein Ereignis (der
            # erste Song startet ohne Haken) - also gleich nochmal nachsehen.
            self._spaeter(guild)
            return
        self._spaeter_n.pop(guild.id, None)

    def _aus_ende(self, guild, s):
        """'Mikro aus' gilt, bis die Runde in diesem Kanal vorbei ist: Flo ist
        dort nicht mehr, und die Musik will auch nicht mehr hin."""
        vc = getattr(guild, "voice_client", None)
        hier = None
        if _verbunden(vc) and music.gehoert_der_musik(guild.id, vc):
            hier = getattr(getattr(vc, "channel", None), "id", None)
        alt = s.kanal_zuletzt
        if hier is not None:
            s.kanal_zuletzt = hier
        if alt is None or alt == hier:
            return
        player = _player(guild.id)
        if player is not None and player.active_channel_id == alt:
            return                        # Neuaufbau - die Runde laeuft noch
        if hier is None:
            s.kanal_zuletzt = None
        if self._daten("aus").pop(str(alt), None) is not None:
            self._store.save_soon()
            log.info("Clips: Server %s – Mikro-aus für Kanal %s endet mit der Runde.",
                     guild.id, alt)

    def _kenner(self, guild):
        """uid -> (Name, Bot?) - beim ERSTEN Paket gefragt: ohne Members-Intent
        kennt der Cache nur, wer gerade im Voice ist."""
        def kennen(uid):
            person = None
            try:
                person = guild.get_member(uid)
            except Exception:  # noqa: BLE001
                person = None
            if person is None:
                return "Jemand", False
            return (str(getattr(person, "display_name", None) or getattr(person, "name", None)
                        or "Jemand")[:64], bool(getattr(person, "bot", False)))
        return kennen

    def _starten(self, guild, vc, s):
        gid = guild.id
        kanal_id = vc.channel.id
        s.puffer = Ringpuffer(gid, kanal_id, raus=self.ist_raus, kennen=self._kenner(guild),
                              konto=self._konto, uhr_=self._uhr)
        e = voiceempfang.Empfaenger(gid, s.puffer.einfuegen, opt_out=self.ist_raus,
                                    uhr=self._uhr)
        e.bei_verbindung = self._bei_verbindung
        voiceempfang.anmelden(gid, e)
        if not e.anhaengen(vc):
            log.warning("Clips: Server %s – Lauscher liess sich nicht anhängen.", gid)
        vc.eigen_senke = s.puffer.eigen_anhaengen
        s.empfaenger = e
        s.aktiv = True
        s.kanal_id = kanal_id
        s.seit = self._uhr()
        s.getrennt_seit = None
        s.geweckt_fuer = None
        s.ansage_fehlt = None
        s.verlauf.clear()
        s.bericht_um = time.monotonic()
        s.zaehler_bericht = e.zaehler()
        s.ton_um = time.monotonic()
        s.ton_zuletzt = 0
        s.ansage_geprueft = time.monotonic()
        log.info("Clips: Server %s – höre in Kanal %s mit (Puffer %d s).", gid, kanal_id,
                 HALTEN_SEK)

    def _bei_verbindung(self, client):
        """op 4 (Neuaufbau) eines Clients: abgleichen."""
        guild = getattr(getattr(client, "channel", None), "guild", None)
        if guild is not None:
            self._spawn(self.abgleichen(guild))

    def _leeren(self, guild, s, grund=""):
        """Sofort und ohne await: Lauscher ab, Puffer leer, eigene Spur ab. Der
        Teil, auf den nichts warten darf (Mikro aus, Opt-out-Faelle, Abschied)."""
        e = s.empfaenger
        if e is not None:
            e.bei_verbindung = None
            e.abhaengen()
            voiceempfang.abmelden(s.gid, e)
        vc = getattr(guild, "voice_client", None) if guild is not None else None
        if s.puffer is not None and isinstance(vc, voiceempfang.EmpfangsClient):
            if vc.__dict__.get("eigen_senke") == s.puffer.eigen_anhaengen:
                vc.eigen_senke = None
        if s.puffer is not None:
            s.puffer.leeren()
        s.puffer = None
        s.empfaenger = None
        s.aktiv = False
        s.kanal_id = None
        s.getrennt_seit = None
        s.geweckt_fuer = None
        if s.ansage_msg is not None:
            self.freigeben(s.ansage_msg)
            s.ansage_msg = None
        task = s.hoeren_task
        s.hoeren_task = None
        s.hoeren_vc = None
        if task is not None and not task.done():
            task.cancel()
        if grund in _SPERR_GRUENDE:
            s.sperre_gen += 1
            player = _player(s.gid)
            if player is not None:
                player.clip_halten = False

    async def _stoppen(self, guild, s, grund):
        """Aufhoeren: leeren (sofort), dann wieder taub stellen."""
        war = s.aktiv
        self._leeren(guild, s, grund)
        await self._wieder_taub(guild, s)
        if war:
            log.info("Clips: Server %s – Puffer geleert (%s).", guild.id, grund)

    async def _wieder_taub(self, guild, s):
        """Hat Flo fuers Mithoeren die Taubheit aufgehoben, stellt er sie
        wieder her - nur verbunden und wenn die Musik gerade nicht selbst baut:
        auf ihre Voice-Sperre wird kurz gewartet (ein Neuaufbau verbindet
        ohnehin taub, ein Tempo-Wechsel nicht). Geht es jetzt nicht (Sperre
        zu lange belegt, Discord lehnt ab), bleibt es vorgemerkt: der naechste
        Abgleich (spaetestens der Takt) versucht es wieder. Laeuft immer unter
        der Sperre der Sitzung."""
        if not s.taub_von_uns:
            return
        # Unser eigenes Taubstellen zaehlt nicht fuer die TAUB_ALLE-Bremse: nach
        # einem Kanalwechsel soll Flo im neuen Kanal gleich wieder hoeren.
        s.taub_um = float("-inf")
        ende = time.monotonic() + HOEREN_WARTEN
        while _musik_sperre_belegt(guild.id) and time.monotonic() < ende:
            await asyncio.sleep(0.2)
        if s.aktiv:
            return                        # hoert inzwischen wieder - so bleibt es
        vc = getattr(guild, "voice_client", None)
        if vc is None:
            # Nicht (mehr) im Voice: nichts zu tun. Die Musik verbindet taub.
            s.taub_von_uns = False
            return
        if not _verbunden(vc):
            # Der Client ist noch da, baut aber gerade neu auf (discord.py
            # selbst, etwa nach 4015) - und zwar mit dem Zustand von JETZT, also
            # hoerend. Vorgemerkt lassen: der naechste Abgleich (spaetestens der
            # Takt) stellt Flo taub, sobald er wieder verbunden ist.
            return
        if _musik_sperre_belegt(guild.id):
            return                        # vorgemerkt - siehe oben
        stimme = getattr(getattr(guild, "me", None), "voice", None)
        s.taub_von_uns = False
        try:
            await guild.change_voice_state(channel=vc.channel, self_deaf=True,
                                           self_mute=bool(getattr(stimme, "self_mute", False)))
        except Exception:  # noqa: BLE001
            s.taub_von_uns = True
            log.debug("Clips: wieder taub stellen ging nicht", exc_info=True)

    def _hoeren_planen(self, guild, vc, s):
        """_hoeren() fuer diesen Client starten - laeuft noch einer fuer einen
        ANDEREN (alten) Client, weicht der: sonst wartete der neue bis zum
        naechsten Takt auf Ohren und Wecken."""
        task = s.hoeren_task
        if task is not None and not task.done():
            if s.hoeren_vc is vc:
                return
            task.cancel()
        s.hoeren_vc = vc
        s.hoeren_task = self._spawn(self._hoeren(guild, vc, s))

    async def _hoeren(self, guild, vc, s):
        """Erst wenn die Verbindung steht und die Musik nicht gerade baut: die
        Taubheit aufheben (hoechstens alle TAUB_ALLE s) und Discord einmal je
        Verbindung wecken."""
        ende = time.monotonic() + HOEREN_WARTEN
        while True:
            if not s.aktiv or getattr(guild, "voice_client", None) is not vc:
                return
            if _verbunden(vc) and not _musik_sperre_belegt(guild.id):
                break
            if time.monotonic() >= ende:
                return
            await asyncio.sleep(0.2)
        stimme = getattr(getattr(guild, "me", None), "voice", None)
        if stimme is not None and getattr(stimme, "self_deaf", False):
            # Die Bremse gilt nur fuer DENSELBEN Client - ein neuer kommt von
            # der Musik (Neuaufbau, und der verbindet taub) und hoert sofort.
            if s.taub_fuer != id(vc) or time.monotonic() - s.taub_um >= TAUB_ALLE:
                s.taub_um = time.monotonic()
                s.taub_fuer = id(vc)
                # VOR dem await: bricht der Task hier ab (Mikro aus mittendrin),
                # stellt _stoppen Flo trotzdem wieder taub.
                s.taub_von_uns = True
                try:
                    await guild.change_voice_state(
                        channel=vc.channel, self_deaf=False,
                        self_mute=bool(getattr(stimme, "self_mute", False)))
                except Exception:  # noqa: BLE001
                    log.debug("Clips: Taubheit aufheben ging nicht", exc_info=True)
        elif stimme is not None:
            s.taub_von_uns = True        # hoert ohnehin (z. B. nach move_to)
        ws = getattr(vc, "ws", None)
        if s.aktiv and s.geweckt_fuer != id(ws):
            try:
                geweckt = await vc.aufwecken()
            except Exception:  # noqa: BLE001
                geweckt = False
            if geweckt or _spielt(vc):
                s.geweckt_fuer = id(ws)
                s.letzte_weckung = time.monotonic()

    # --- Die Ansage -------------------------------------------------------------
    def _zielkanal_roh(self, guild):
        """Der Clip-Kanal nach basis.kanal_ohne_aufraeumen - schreiben UND
        Dateien hochladen, nie ein Aufraeum-Kanal."""
        try:
            return basis.kanal_ohne_aufraeumen(laufzeit.client, guild, "clip_channel",
                                               rechte=("attach_files",))
        except Exception:  # noqa: BLE001
            log.debug("Clips: Clip-Kanal nicht bestimmbar", exc_info=True)
            return None

    def _ziel_text(self, guild, voice):
        """Wohin die Clips dieses Voice gehen - wie _zielkanal() es entscheidet."""
        ziel = self._zielkanal_roh(guild)
        if voice is not None and basis.privat(voice, ziel, guild):
            return "diesen Voice-Chat (euer Voice ist privat)"
        if ziel is not None:
            return getattr(ziel, "mention", None) or "den Clip-Kanal"
        return "den Clip-Kanal"

    def _ansage_text(self, guild, voice):
        """Die Ansage. 'ueber Groq' nur, wenn Transkript UND Spruch dort landen:
        der Spruch geht an die KI von ai.py (LLM_BASE_URL), und die kann ein
        ganz anderer Anbieter sein."""
        anbieter = clipstext.transkriber.anbieter()
        spruch_url = str(getattr(ai.instance, "_basis_url", "") or "")
        try:
            spruch_an = bool(ai.is_enabled())
        except Exception:  # noqa: BLE001
            spruch_an = True
        if anbieter == "Groq" and (not spruch_an or "groq.com" in spruch_url):
            transkript = " (mit KI-Transkript über Groq)"
        elif anbieter:
            transkript = " (mit KI-Transkript über einen externen KI-Dienst)"
        else:
            transkript = ""
        return ANSAGE.format(name=self.name_fuer(guild.id), ziel=self._ziel_text(guild, voice),
                             transkript=transkript)

    async def _ankuendigen(self, guild, kanal, s, erzwingen=False):
        """Die Ansage im Voice-Chat. Steht dort eine, die juenger als
        ANSAGE_PAUSE ist und die es noch gibt, kommt keine neue. -> ob eine steht."""
        ansagen = self._daten("ansagen")
        eintrag = ansagen.get(str(kanal.id))
        if not erzwingen and isinstance(eintrag, dict):
            try:
                ts = basis.glaubhafte_zeit(float(eintrag.get("ts") or 0.0))
            except (TypeError, ValueError):
                ts = 0.0
            if _wand() - ts < ANSAGE_PAUSE and _ganz(eintrag.get("mid")):
                try:
                    msg = await kanal.fetch_message(_ganz(eintrag.get("mid")))
                except Exception:  # noqa: BLE001 - weg, kein Recht, Attrappe: neu ansagen
                    msg = None
                if msg is not None:
                    self.schuetzen(msg)
                    s.ansage_msg = msg
                    return True
        try:
            rechte = kanal.permissions_for(guild.me)
            if not (rechte.view_channel and rechte.send_messages):
                return False
        except Exception:  # noqa: BLE001
            return False
        try:
            msg = await kanal.send(self._ansage_text(guild, kanal), view=AnsageView(kanal.id),
                                   allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            log.info("Clips: Server %s – Ansage abgelehnt (%s).", guild.id,
                     getattr(exc, "status", "?"))
            return False
        ansagen[str(kanal.id)] = {"ts": round(_wand()), "mid": int(msg.id)}
        self._store.save_soon()
        self.schuetzen(msg)
        s.ansage_msg = msg
        log.info("Clips: Server %s – Ansage in Kanal %s gepostet.", guild.id, kanal.id)
        return True

    async def _ansage_pruefen(self, guild, s, weg=False):
        """Steht die Ansage noch? Ohne Ansage keine Aufnahme - auch mitten in
        der Runde nicht ('flo lösch 50' im Voice-Chat nimmt sie mit). Gefragt
        vom Takt (alle ANSAGE_NACHSEHEN) und sofort, wenn Discord die Loeschung
        meldet (weg=True, dann ohne Nachsehen). Ist sie weg: eine neue - und
        geht das nicht, hoert Flo auf. Laeuft unter der Sperre der Sitzung."""
        if not s.aktiv:
            return
        kanal = guild.get_channel(s.kanal_id) if hasattr(guild, "get_channel") else None
        alt = s.ansage_msg
        if not weg and alt is not None and kanal is not None:
            try:
                await kanal.fetch_message(alt.id)
                return
            except discord.NotFound:
                pass                         # weg - unten neu
            except Exception:  # noqa: BLE001 - Netz, Rechte: deshalb nicht aufhoeren
                log.debug("Clips: Ansage nicht nachsehbar", exc_info=True)
                return
        if alt is not None:
            self.freigeben(alt)
            s.ansage_msg = None
        self._daten("ansagen").pop(str(s.kanal_id), None)
        self._store.save_soon()
        # Hoechstens einmal je ANSAGE_FEHLT_PAUSE neu - loescht sie jemand (oder
        # ein Moderations-Bot) gleich wieder, hoert Flo eben auf, statt den
        # Voice-Chat im Ping-Pong mit Ansagen zu fuellen.
        jetzt = time.monotonic()
        gebremst = jetzt - s.ansage_neu_um < ANSAGE_FEHLT_PAUSE
        if (kanal is not None and not gebremst
                and await self._ankuendigen(guild, kanal, s, erzwingen=True)):
            s.ansage_neu_um = jetzt
            log.info("Clips: Server %s – Ansage in Kanal %s war weg, neu gepostet.", guild.id,
                     s.kanal_id)
            return
        if gebremst:
            log.info("Clips: Server %s – Ansage in Kanal %s schon wieder weg - ich höre erst "
                     "mal auf.", guild.id, s.kanal_id)
        else:
            log.info("Clips: Server %s – Ansage in Kanal %s war weg und ging nicht neu - ich "
                     "höre auf.", guild.id, s.kanal_id)
        cid = s.kanal_id
        await self._stoppen(guild, s, "ansage")
        # Der Grund reist in ansage_fehlt mit (_soll): 'dauernd geloescht' ist
        # etwas anderes als 'darf nicht schreiben'.
        grund = "ansage_weg" if gebremst else "ansage"
        s.ansage_fehlt = (cid, time.monotonic() + ANSAGE_FEHLT_PAUSE, grund)
        s.grund = grund

    def geloescht(self, gid, kanal_id, ids):
        """bot.on_raw_message_delete / on_raw_bulk_message_delete: diese
        Nachrichten sind weg. War die Ansage dabei, muss sofort eine neue her
        (_ansage_pruefen); ein Clip, den jemand von Hand geloescht hat, fliegt
        aus dem Index (und damit aus der Bestenliste)."""
        if not self._enabled or self._store is None or not ids:
            return
        try:
            gid, kanal_id = int(gid or 0), int(kanal_id or 0)
            ids = {int(i) for i in ids}
        except (TypeError, ValueError):
            return
        geaendert = False
        index = self._clips_von(gid)
        for id8, eintrag in list(index.items()):
            if isinstance(eintrag, dict) and _ganz(eintrag.get("mid")) in ids:
                del index[id8]
                geaendert = True
        ansage = self._daten("ansagen").get(str(kanal_id))
        if isinstance(ansage, dict) and _ganz(ansage.get("mid")) in ids:
            del self._daten("ansagen")[str(kanal_id)]
            geaendert = True
        if geaendert:
            self._store.save_soon()
        s = self._sitzungen.get(gid)
        if (s is not None and s.aktiv and s.ansage_msg is not None
                and _ganz(getattr(s.ansage_msg, "id", 0)) in ids):
            guild = self._guild(gid)
            if guild is not None:
                self._spawn(self._ansage_weg(guild, s))

    async def _ansage_weg(self, guild, s):
        async with self._sperre(s):
            await self._ansage_pruefen(guild, s, weg=True)

    # --- Ereignisse -----------------------------------------------------------------
    async def voice_state(self, member, before, after):
        """bot.on_voice_state_update - nur Flo selbst zaehlt: rein, raus,
        verschoben, (vom Server) taub."""
        if not self._enabled:
            return
        guild = getattr(member, "guild", None)
        ich = getattr(getattr(guild, "me", None), "id", None)
        if guild is None or ich is None or getattr(member, "id", None) != ich:
            return
        vorher = getattr(getattr(before, "channel", None), "id", None)
        nachher = getattr(getattr(after, "channel", None), "id", None)
        if (vorher != nachher
                or getattr(before, "deaf", False) != getattr(after, "deaf", False)
                or getattr(before, "self_deaf", False) != getattr(after, "self_deaf", False)):
            await self.abgleichen(guild)

    async def tick(self, client):
        """bot.voice_heal_loop, alle 15 s: abgleichen (Sendepause, Funktion aus,
        Gnade vorbei), Puffer stutzen, Empfaenger-Takt, wach halten, Bericht,
        Warnungen, alte Dialoge und alte Eintraege weg."""
        if not self._enabled or self._store is None:
            return
        for guild in list(getattr(client, "guilds", ()) or ()):
            if getattr(guild, "voice_client", None) is None and guild.id not in self._sitzungen:
                continue
            s = self._sitzung(guild.id)
            if s.lock is not None and s.lock_loop is asyncio.get_running_loop() \
                    and s.lock.locked():
                continue                   # ein Abgleich laeuft gerade
            try:
                await self.abgleichen(guild)
            except Exception:  # noqa: BLE001
                log.exception("Clips: Abgleich für Server %s gescheitert", guild.id)
            if s.aktiv:
                try:
                    await self._takt_sitzung(guild, s)
                except Exception:  # noqa: BLE001
                    log.exception("Clips: Takt für Server %s gescheitert", guild.id)
            if s.aktiv and time.monotonic() - s.ansage_geprueft >= ANSAGE_NACHSEHEN:
                s.ansage_geprueft = time.monotonic()
                try:
                    async with self._sperre(s):
                        await self._ansage_pruefen(guild, s)
                except Exception:  # noqa: BLE001
                    log.exception("Clips: Ansage-Prüfung für Server %s gescheitert", guild.id)
        jetzt = self._uhr()
        for s in self._sitzungen.values():
            if s.puffer is not None:
                s.puffer.trimmen(jetzt)
        self._dialoge_ablaufen()
        if time.monotonic() - self._aufgeraeumt_um > 3600:
            self._aufraeumen()
        self._kontingent_merken()

    async def _takt_sitzung(self, guild, s):
        e = s.empfaenger
        if e is not None:
            e.takt()
        jetzt = time.monotonic()
        vc = getattr(guild, "voice_client", None)
        if (s.empfaenger is not None and _verbunden(vc) and not _spielt(vc)
                and jetzt - s.letzte_weckung >= WECKEN_ALLE):
            try:
                if await vc.aufwecken():
                    s.letzte_weckung = jetzt
            except Exception:  # noqa: BLE001
                log.debug("Clips: wach halten ging nicht", exc_info=True)
        if e is None:
            return
        zaehler = e.zaehler()
        s.verlauf.append((jetzt, zaehler))
        ton = _ton_pakete(zaehler)
        if ton > s.ton_zuletzt:
            s.ton_zuletzt = ton
            s.ton_um = jetzt
        if jetzt - s.bericht_um >= BERICHT_ALLE:
            self._berichten(guild, s, zaehler)
            s.bericht_um = jetzt
            s.zaehler_bericht = zaehler
        self._warnen(guild, s, vc, zaehler, jetzt)

    def _berichten(self, guild, s, zaehler):
        alt = s.zaehler_bericht or {}

        def d(*namen):
            return sum(zaehler.get(n, 0) - alt.get(n, 0) for n in namen)

        p = s.puffer
        log.info("Clips: Server %s – %d Pakete (%d gespeichert, %d DAVE-retry, %d verworfen, "
                 "%d unbekannt), %d Sprecher, Puffer %.0f s/%.1f MB", guild.id, d("roh"),
                 d("gespeichert"), d("dave_nochmal"),
                 d("dave_verworfen", "transport_fehler", "unbekannt_verfallen"),
                 d("unbekannt"), p.sprecher_mit_ton() if p else 0,
                 min(HALTEN_SEK, self._uhr() - s.seit) if p else 0.0,
                 p.megabyte() if p else 0.0)

    def _warnen(self, guild, s, vc, zaehler, jetzt):
        """Hoechstens alle WARNEN_ALLE eine Warnung: Ton kommt an, aber nichts
        landet im Puffer (DAVE, Zuordnung ...) - oder minutenlang gar kein Ton,
        obwohl mindestens zwei Leute reden koennten. Wer raus ist, zaehlt dabei
        nicht: seine Pakete fliegen mit Absicht weg, und auf ihn wartet niemand."""
        if jetzt - s.warn_um < WARNEN_ALLE:
            return
        if len(s.verlauf) >= 2:
            _t, vorher = s.verlauf[-2]
            ton = ((_ton_pakete(zaehler) - zaehler.get("raus", 0))
                   - (_ton_pakete(vorher) - vorher.get("raus", 0)))
            drin = zaehler.get("gespeichert", 0) - vorher.get("gespeichert", 0)
            if ton >= WARNEN_AB_PAKETEN and drin == 0:
                s.warn_um = jetzt
                log.warning("Clips: Server %s – es kommen Pakete an (%d), aber nichts landet im "
                            "Puffer. 'flo clip status' zeigt, wo sie hängen.", guild.id, ton)
                return
        if jetzt - s.ton_um >= STILL_SEK:
            leute = 0
            for m in getattr(getattr(vc, "channel", None), "members", ()) or ():
                stimme = getattr(m, "voice", None)
                if getattr(m, "bot", False) or stimme is None or self.ist_raus(
                        getattr(m, "id", 0)):
                    continue
                if not (getattr(stimme, "self_mute", False) or getattr(stimme, "mute", False)):
                    leute += 1
            if leute >= 2:
                s.warn_um = jetzt
                log.warning("Clips: Server %s – seit %d s kein einziges Paket, obwohl %d Leute "
                            "im Call reden könnten (taub? nicht geweckt?).", guild.id,
                            int(jetzt - s.ton_um), leute)

    def _dialoge_ablaufen(self):
        """Dialoge, deren Timeout nie kam (DIALOG_ABLAUF): wie ein Timeout -
        Schnappschuss weg, Loesch-Schutz frei, 'Zu langsam' dran. Nebenbei
        vergisst das Abkuehlen, wer laenger als ABKUEHLEN_SEK nicht geclippt hat."""
        jetzt = time.monotonic()
        for gid, dialoge in list(self._dialoge.items()):
            for uid, dialog in list(dialoge.items()):
                if jetzt - dialog.erstellt <= DIALOG_ABLAUF:
                    continue
                dialoge.pop(uid, None)
                dialog.snap = None
                if dialog._fertig:
                    self.freigeben(dialog.message)
                    continue
                try:
                    dialog.stop()
                except Exception:  # noqa: BLE001 - die View hing an keinem Loop mehr
                    log.debug("Clips: alter Dialog liess sich nicht stoppen", exc_info=True)
                if self._spawn(dialog.on_timeout()) is None:
                    self.freigeben(dialog.message)
            if not dialoge:
                self._dialoge.pop(gid, None)
        for uid, wann in list(self._zuletzt.items()):
            if jetzt - wann > ABKUEHLEN_SEK:
                del self._zuletzt[uid]

    # --- Befehle ------------------------------------------------------------------
    def _ziele(self, message):
        """Die getippten Erwaehnungen - ohne Flo selbst (der ist die Ansprache)."""
        guild = getattr(message, "guild", None)
        ich = {_ganz(getattr(getattr(guild, "me", None), "id", 0)),
               _ganz(getattr(getattr(laufzeit.client, "user", None), "id", 0))} - {0}
        return [m for m in basis.echte_erwaehnungen(message)
                if _ganz(getattr(m, "id", 0)) not in ich]

    def befehl(self, message):
        """Die Grammatik -> ('brett', zeitraum) | ('raus', ziele) | ('rein', ziele)
        | ('aus',) | ('an',) | ('status',) | ('hilfe',) | ('clip', sek|None, ziel|None)
        | ('fehler', textschluessel) | None (kein Befehl: die KI ist dran)."""
        text = ai.strip_lead(getattr(message, "content", "") or "")
        teile = " ".join(text.lower().split()).split(None, 1)
        if not teile:
            return None
        wort = teile[0].strip(_RAND)
        vertippt = False
        if wort not in ("clip", "clips"):           # = _CMDS
            if not _CLIP_VERTIPPT_RE.fullmatch(wort):
                return None
            vertippt = True
        rest_roh = teile[1].strip() if len(teile) > 1 else ""
        if wort == "clip" and rest_roh and re.fullmatch(r"\?+", rest_roh):
            return ("hilfe",)
        rest = " ".join(t.strip(_RAND) for t in rest_roh.split() if t.strip(_RAND))
        # Die festen Formen vertragen Hoeflichkeit drumherum ('nein danke',
        # 'bitte ohne mich', 'aus bitte') - die Clip-Grammatik unten sieht
        # weiter den ganzen Rest.
        kern = " ".join(t for t in rest.split() if t not in _HOEFLICH)
        ziele = self._ziele(message)
        if vertippt:
            return ("raus", ziele) if kern in _RAUS else None
        if wort == "clips":
            if rest in _BRETT and not ziele:
                return ("brett", _BRETT[rest])
            return None
        if kern in _RAUS:
            return ("raus", ziele)
        if kern in _REIN:
            return ("rein", ziele)
        if not ziele:
            if kern in _AUS:
                return ("aus",)
            if kern in _AN:
                return ("an",)
            if kern in _STATUS:
                return ("status",)
            if kern in _HILFE:
                return ("hilfe",)
        autor_id = _ganz(getattr(getattr(message, "author", None), "id", 0))
        return self._clip_grammatik(rest, ziele, autor_id)

    @staticmethod
    def _clip_grammatik(rest, ziele, autor_id=0):
        """'[dauer] [nur] [@wer]' mit Fuellwoertern. Alles andere: kein Befehl.
        (Die Laufvariable heisst bewusst nicht 'wort': werkzeug/inventar.py
        hielte die Fuellwoerter sonst fuer Befehle.)"""
        stuecke = rest.split()
        dauer, selbst, alle = None, False, False
        i = 0
        while i < len(stuecke):
            teil = stuecke[i]
            weiter = stuecke[i + 1] if i + 1 < len(stuecke) else ""
            treffer = _ZAHL_EINHEIT.fullmatch(teil)
            uhrzeit = _MIN_SEK.fullmatch(teil)
            if (uhrzeit and numfmt.ist_zahl(uhrzeit.group(1))
                    and numfmt.ist_zahl(uhrzeit.group(2))):
                if dauer is not None:                # '1:30' = 90 s
                    return None
                dauer = int(uhrzeit.group(1)) * 60 + int(uhrzeit.group(2))
            elif treffer and numfmt.ist_zahl(treffer.group(1)):
                if dauer is not None:
                    return None
                zahl = int(treffer.group(1))
                if treffer.group(2) and numfmt.ist_zahl(treffer.group(2)):
                    zahl += int(treffer.group(2)) / 10   # '1,5 min'
                einheit = treffer.group(3) or ""
                if not einheit and (weiter in _SEKUNDEN or weiter in _MINUTEN):
                    einheit = weiter
                    i += 1
                if einheit in _MINUTEN:
                    dauer = zahl * 60
                elif einheit in _SEKUNDEN:
                    dauer = zahl
                else:
                    dauer = zahl * 60 if zahl <= 5 else zahl
                dauer = int(round(dauer))
            elif teil in _ARTIKEL and weiter == "halbe":
                pass                                 # 'eine halbe minute'
            elif teil in _ZAHLWOERTER and weiter in _MINUTEN:
                if dauer is not None:
                    return None
                dauer = _ZAHLWOERTER[teil]
                i += 1
            elif teil in _NUR_MINUTE:
                if dauer is not None:
                    return None
                dauer = 60
            elif teil in _ICH:
                selbst = True
            elif teil in _ALLE:
                alle = True
            elif teil == "nur" or teil in _FUELL:
                pass
            else:
                return None
            i += 1
        if len(ziele) > 1:
            return ("fehler", "mehrere")
        ziel = ziele[0] if ziele else None
        if alle and (ziel is not None or selbst):
            return ("fehler", "mehrere")
        # 'mich @Bob' sind zwei Leute - 'mich @ich-selbst' nur einer.
        if selbst and ziel is not None and _ganz(getattr(ziel, "id", 0)) != autor_id:
            return ("fehler", "mehrere")
        if ziel is not None and getattr(ziel, "bot", False):
            return ("fehler", "bot_ziel")
        if dauer is not None:
            if dauer > PUFFER_SEK:
                return ("fehler", "zu_lang")
            dauer = max(MIN_SEK, dauer)
        return ("clip", dauer, "ich" if selbst and ziel is None else ziel)

    def _text(self, schluessel, **werte):
        werte.setdefault("name", self._bot_name)
        return TEXTE[schluessel].format(**werte)

    def _grund_text(self, grund):
        return self._text(_GRUND_TEXT.get(grund, "grund_start"))

    async def handle(self, message):
        """'flo clip ...', 'flo clips ...' -> Antwort; sonst None."""
        if not self._enabled or self._store is None:
            return None
        inhalt = getattr(message, "content", "") or ""
        if not _vielleicht_clip(inhalt):
            return None             # jede Chatnachricht kommt hier vorbei - schnell raus
        guild = getattr(message, "guild", None)
        if guild is None or getattr(message, "author", None) is None:
            return None
        befehl = self.befehl(message)
        if befehl is None:
            return None
        art = befehl[0]
        if art == "brett":
            return self._brett(message, guild, befehl[1])
        if art == "raus":
            return await self._leise(message, self._raus_befehl(message, befehl[1]))
        if art == "rein":
            return await self._leise(message, self._rein_befehl(message, befehl[1]))
        if art == "hilfe":
            return self._text("hilfe")
        if art == "fehler":
            return self._text(befehl[1])
        if art == "status":
            return self._status(message, guild)
        if art == "an":
            return await self._an(message, guild)
        if art == "aus":
            return await self._aus_befehl(message, guild)
        return await self._clip(message, guild, befehl[1], befehl[2])

    def _brett(self, message, guild, zeitraum):
        """'flo clips': die Bestenliste. Es zaehlen nur Clips, die jeder sehen
        darf, der hier mitliest - ein Clip aus dem Voice-Chat eines privaten
        Voice (oder aus einem Kanal, den es nicht mehr gibt) taucht hier nicht
        auf, auch nicht als 'von wem, wann, wie viele 🔥'. 'alle' reicht so
        weit wie der Index: CLIPS_TAGE."""
        hier = getattr(message, "channel", None)
        eintraege = []
        for eintrag in self._clips_von(guild.id).values():
            if not isinstance(eintrag, dict):
                continue
            kanal = (guild.get_channel(_ganz(eintrag.get("cid")))
                     if hasattr(guild, "get_channel") else None)
            if kanal is None or basis.privat(kanal, hier, guild):
                continue
            eintraege.append(eintrag)
        if zeitraum in ("alle", "top"):
            emb = clipstext.rangliste_embed(eintraege, CLIPS_TAGE, guild.id)
            emb.title = TEXTE["brett_alle"].format(wochen=CLIPS_TAGE // 7)
            return emb
        return clipstext.rangliste_embed(eintraege, zeitraum, guild.id)

    async def datenschutz(self, message):
        """IMMER in der Kette: 'clip nein/ja' geht auch, wo Clips aus sind; jeder
        andere Clip-Befehl bekommt dort ein klares 'aus' statt der KI."""
        if self._store is None:
            return None
        inhalt = getattr(message, "content", "") or ""
        if not _vielleicht_clip(inhalt):
            return None
        guild = getattr(message, "guild", None)
        if guild is None or getattr(message, "author", None) is None:
            return None
        befehl = self.befehl(message)
        if befehl is None:
            return None
        if befehl[0] == "raus":
            return await self._leise(message, self._raus_befehl(message, befehl[1]))
        if befehl[0] == "rein":
            return await self._leise(message, self._rein_befehl(message, befehl[1]))
        if self._enabled and features.is_on_in(guild.id, "clips"):
            return None             # dann hat handle() das schon beantwortet
        return self._text("aus_hier")

    @staticmethod
    async def _leise(message, text):
        """Antworten, die verraten, wer raus ist oder rein will, schickt clips
        selbst: bot.py schriebe sonst 'Befehl von <Name>: Erledigt, du bist
        raus' ins Journal. Die eigenen Logzeilen nennen dabei nie wen (raus())."""
        await basis.antworte(message, text)
        return HANDLED

    async def datenschutz_dm(self, message):
        """bot.on_message, Privatnachricht: 'clip nein' / 'clip ja' (mit oder
        ohne 'flo') geht auch hier - der Ausstieg gilt auf jedem Server, dafuer
        braucht es keinen. Sonst landete die Nachricht beim Besitzer. -> ob
        beantwortet (dann ist bot.py fertig)."""
        if self._store is None or getattr(message, "guild", None) is not None:
            return False
        if getattr(message, "author", None) is None:
            return False
        if not _vielleicht_clip(getattr(message, "content", "") or ""):
            return False
        befehl = self.befehl(message)
        if befehl is None or befehl[0] not in ("raus", "rein"):
            return False
        if befehl[0] == "raus":
            await self._leise(message, self._raus_befehl(message, befehl[1]))
        else:
            await self._leise(message, self._rein_befehl(message, befehl[1]))
        return True

    def _raus_befehl(self, message, ziele):
        autor = message.author
        fremde = [z for z in ziele if _ganz(getattr(z, "id", 0)) != _ganz(autor.id)]
        if fremde:
            return self._text("fremd_raus", wer=_name(fremde[0]))
        if self.ist_raus(autor.id):
            return self._text("schon_raus")
        self.raus(autor.id)
        return self._text("raus")

    def _rein_befehl(self, message, ziele):
        autor = message.author
        fremde = [z for z in ziele if _ganz(getattr(z, "id", 0)) != _ganz(autor.id)]
        if fremde:
            return self._text("fremd_raus", wer=_name(fremde[0]))
        if not self.ist_raus(autor.id):
            return self._text("schon_rein")
        self.rein(autor.id)
        return self._text("rein")

    # --- an / aus -------------------------------------------------------------------
    async def _an(self, message, guild):
        """'flo clip an': in den Voice des Fragenden (ueber die Musik - die haelt
        die Sitzung) und mithoeren."""
        gid = guild.id
        kanal = getattr(getattr(message.author, "voice", None), "channel", None)
        if kanal is None:
            return self._text("an_kein_voice")
        if not self._kann_rendern:
            return self._text("grund_render")
        if not guildcfg.an(gid, "clip_aufnahme"):
            return self._text("nicht_erlaubt_server")
        if _sendepause():
            return self._text("grund_sendepause")
        vc = getattr(guild, "voice_client", None)
        if (_verbunden(vc) and getattr(vc.channel, "id", None) != kanal.id
                and music.gehoert_der_musik(gid, vc)):
            return self._text("an_woanders", kanal=_sauber(getattr(vc.channel, "name", "?")))
        s = self._sitzung(gid)
        if s.aktiv and s.kanal_id == kanal.id:
            # Hoert schon mit (die Musik laeuft) - 'clip an' heisst trotzdem:
            # nach dem letzten Song bleiben, solange Menschen da sind.
            player = _player(gid)
            if player is not None:
                player.clip_halten = True
            return self._text("an_schon")
        # Umschalt-Bremse: jedes 'aus' postet in den Voice-Chat, jedes 'an'
        # eine neue Ansage - aus/an im Sekundentakt waere Spam dort.
        aus_um = self._daten("aus").get(str(kanal.id))
        if aus_um is not None:
            try:
                seit = _wand() - basis.glaubhafte_zeit(float(aus_um))
            except (TypeError, ValueError):
                seit = AN_PAUSE
            if seit < AN_PAUSE:
                return self._text("an_zu_schnell", s=max(1, int(AN_PAUSE - seit + 0.999)))
        self._daten("aus").pop(str(kanal.id), None)
        self._daten("ansagen").pop(str(kanal.id), None)
        self._store.save_soon()
        s.ansage_fehlt = None
        player = music.instance._player_for(gid)
        player.text_channel = message.channel
        try:
            await player.connect(kanal)
        except music.VOICE_CONNECT_FEHLER as exc:
            log.warning("Clips: Server %s – 'clip an' kam nicht in den Voice (%s).", gid,
                        type(exc).__name__)
            return music.VOICE_KAPUTT
        player.clip_halten = True
        await self.abgleichen(guild)
        if s.aktiv:
            return self._text("an_ok")
        if s.grund in _AN_SINNLOS:
            # Mithoeren geht hier so bald nicht (keine Ansage moeglich, vom
            # Server taub, falscher Client): dann haelt 'clip an' Flo auch
            # nicht im Kanal - es gilt wieder der normale Leerlauf.
            player.clip_halten = False
        if s.grund == "ansage":
            return self._text("an_ansage_fehlt")
        return self._grund_text(s.grund)

    async def _aus_befehl(self, message, guild):
        s = self._sitzung(guild.id)
        autor = message.author
        im_kanal = getattr(getattr(getattr(autor, "voice", None), "channel", None), "id", None)
        if not s.aktiv or (im_kanal != s.kanal_id and not _ist_mod(autor)):
            # Flo hoert (hier) nicht mit - aber aus der Runde kann noch etwas
            # unterwegs sein: ein offener Dialog, ein laufender Schnitt.
            offen = self._offen_aus(guild.id)
            if not offen:
                return self._text("aus_schon" if not s.aktiv else "zaungast_mikro")
            if im_kanal not in offen and not _ist_mod(autor):
                return self._text("zaungast_mikro")
            self._offenes_verwerfen(guild)
            return self._text("aus_verworfen")
        cid = s.kanal_id                  # _leeren() nimmt ihn der Sitzung gleich weg
        geht = self._mikro_aus_sofort(guild, cid)
        await self._mikro_aus_rest(guild, cid, autor, geht)
        return self._text("aus_raus" if geht else "aus_ok")

    def _offen_aus(self, gid):
        """Aus welchen Voice-Kanaelen gerade noch etwas unterwegs ist: offene
        Dialoge mit Stand und Schnitte zwischen Schnappschuss und Post."""
        kanaele = {d.snap.kanal_id for d in (self._dialoge.get(gid) or {}).values()
                   if d.snap is not None}
        kanaele.update(k for k, n in (self._laufend.get(gid) or {}).items() if n > 0)
        return kanaele

    @contextlib.contextmanager
    def _unterwegs(self, gid, kanal_id):
        """Solange ein Schnitt aus diesem Kanal unterwegs ist (siehe _offen_aus)."""
        laufend = self._laufend.setdefault(gid, {})
        laufend[kanal_id] = laufend.get(kanal_id, 0) + 1
        try:
            yield
        finally:
            rest = laufend.get(kanal_id, 1) - 1
            if rest > 0:
                laufend[kanal_id] = rest
            else:
                laufend.pop(kanal_id, None)
            if not laufend and self._laufend.get(gid) is laufend:
                self._laufend.pop(gid, None)

    def _offenes_verwerfen(self, guild):
        """Mikro aus, obwohl Flo (hier) gerade nicht zuhoert: was aus der Runde
        noch unterwegs ist - offene Dialoge, laufende Schnitte -, wird
        verworfen. Ein 'aus' fuer den Kanal gibt es dafuer nicht: die Runde,
        fuer die es gaelte, ist schon vorbei."""
        s = self._sitzung(guild.id)
        s.sperre_gen += 1
        for dialog in list((self._dialoge.get(guild.id) or {}).values()):
            dialog.snap = None
        log.info("Clips: Server %s – Mikro aus nach der Runde, Offenes verworfen.", guild.id)

    def _mikro_aus_sofort(self, guild, cid):
        """Mikro aus fuer Kanal cid - der Teil ohne await: merken, Puffer leer,
        laufende Schnitte und Dialoge verwerfen. -> ob Flo danach geht (er kam
        nur zum Mithoeren, und es laeuft nichts)."""
        self._daten("aus")[str(cid)] = round(_wand())
        self._daten("ansagen").pop(str(cid), None)
        self._store.save_soon()
        player = _player(guild.id)
        geht = bool(player is not None and player.clip_halten and player.nichts_zu_tun())
        s = self._sitzung(guild.id)
        if s.aktiv and s.kanal_id == cid:
            self._leeren(guild, s, "aus")
            log.info("Clips: Server %s – Puffer geleert (aus).", guild.id)
        else:
            s.sperre_gen += 1
        for dialog in list((self._dialoge.get(guild.id) or {}).values()):
            dialog.snap = None
        return geht

    async def _mikro_aus_rest(self, guild, cid, wer, geht):
        """Der Rest: wieder taub, im Voice-Chat sagen, wer es war, und - kam Flo
        nur zum Mithoeren und laeuft nichts - gehen."""
        s = self._sitzung(guild.id)
        async with self._sperre(s):
            await self._wieder_taub(guild, s)
            await self._abgleichen(guild, s)
        kanal = guild.get_channel(cid) if hasattr(guild, "get_channel") else None
        if kanal is not None:
            try:
                await kanal.send(self._text_fuer(guild, "aus_gemeldet",
                                                 wer=f"<@{_ganz(wer.id)}>"),
                                 allowed_mentions=discord.AllowedMentions.none())
            except Exception:  # noqa: BLE001
                log.debug("Clips: 'Mikro aus' im Voice-Chat ging nicht", exc_info=True)
        player = _player(guild.id)
        if geht and player is not None and player.nichts_zu_tun():
            try:
                await player.disconnect()
            except Exception:  # noqa: BLE001
                log.debug("Clips: nach 'Mikro aus' gehen ging nicht", exc_info=True)

    # --- Status -------------------------------------------------------------------
    def _status(self, message, guild):
        autor = message.author
        if not (_besitzer(autor.id) or _ist_mod(autor)):
            return self._text("status_nur_mods")
        s = self._sitzung(guild.id)
        vc = getattr(guild, "voice_client", None)
        emb = discord.Embed(title="🎬 Clips – Status", color=clipstext.FARBE)
        if s.aktiv:
            sitzung = f"läuft seit {clipstext.dauer_text(self._uhr() - s.seit)}"
        else:
            soll, grund, _vc = self._soll(guild, s)
            # Der frische Grund zuerst - s.grund kann bis zu einem Takt alt sein.
            sitzung = f"aus – Grund: `{grund or s.grund or '—'}`"
        if s.getrennt_seit is not None:
            sitzung += " (Verbindung weg, Puffer wartet)"
        kanal = s.kanal_id or getattr(getattr(vc, "channel", None), "id", None)
        ansage = "—"
        eintrag = self._daten("ansagen").get(str(kanal)) if kanal else None
        if isinstance(eintrag, dict):
            ansage = f"steht (<t:{_ganz(eintrag.get('ts'))}:R>)"
        emb.add_field(name="Sitzung", value=f"{sitzung}\nKanal: {f'<#{kanal}>' if kanal else '—'}"
                      f"\nAnsage: {ansage}", inline=False)
        stand = vc.stand() if isinstance(vc, voiceempfang.EmpfangsClient) else {}
        stimme = getattr(getattr(guild, "me", None), "voice", None)
        taub = "ja" if getattr(stimme, "self_deaf", False) or getattr(stimme, "deaf", False) \
            else ("nein" if stimme is not None else "—")
        emb.add_field(name="Verbindung", value=(
            f"Client: `{type(vc).__name__ if vc is not None else '—'}` · Modus: "
            f"`{stand.get('modus') or '—'}`\n"
            f"DAVE: v{stand.get('dave_version', 0)} · bereit "
            f"{'ja' if stand.get('dave_bereit') else 'nein'} · Epoche "
            f"{stand.get('dave_epoche') if stand.get('dave_epoche') is not None else '—'}\n"
            f"taub: {taub} · geweckt: {'ja' if s.geweckt_fuer is not None else 'nein'} · "
            f"Aufbauten: {stand.get('verbindungen', 0)}"), inline=False)
        z = self._zaehler_60(s)
        emb.add_field(name="Pakete (60 s)", value=(
            f"roh {z.get('roh', 0)} · gespeichert {z.get('gespeichert', 0)} · RTCP "
            f"{z.get('rtcp', 0)} · Transport-Fehler {z.get('transport_fehler', 0)}\n"
            f"DAVE ok {z.get('dave_ok', 0)} · klar {z.get('dave_klar', 0)} · nochmal "
            f"{z.get('dave_nochmal', 0)} · verworfen {z.get('dave_verworfen', 0)}\n"
            f"unbekannt {z.get('unbekannt', 0)} · raus {z.get('raus', 0)}"), inline=False)
        p = s.puffer
        emb.add_field(name="Leute", value=(
            f"zugeordnet {stand.get('zugeordnet', 0)} · mit Ton "
            f"{p.sprecher_mit_ton() if p else 0} · ausgestiegen {len(self._raus_ids)}"),
            inline=False)
        emb.add_field(name="Puffer", value=(
            f"{min(HALTEN_SEK, self._uhr() - s.seit) if p else 0:.0f} s · "
            f"{p.megabyte() if p else 0:.1f} MB · Flo-Spur {p.eigen_sek() if p else 0:.0f} s"),
            inline=False)
        anfragen, sekunden = clipstext.transkriber.kontingent.heute()
        emb.add_field(name="Schnitt & Transkript", value=(
            f"Schnitt: {'beschäftigt' if self._in_arbeit else 'frei'} "
            f"({self._in_arbeit} in Arbeit) · ffmpeg {'ok' if self._kann_rendern else 'fehlt'}\n"
            f"Transkript: {clipstext.transkriber.anbieter() or 'aus'} · heute {anfragen} "
            f"Anfragen, {sekunden} s"), inline=False)
        return emb

    @staticmethod
    def _zaehler_60(s):
        """Die Zaehler der letzten 60 s (aus dem Verlauf des Takts)."""
        e = s.empfaenger
        if e is None:
            return {}
        jetzt = e.zaehler()
        grenze = time.monotonic() - 60
        alt = None
        for t, stand in s.verlauf:
            if t <= grenze:
                alt = stand
        if alt is None:
            return jetzt
        return {k: jetzt.get(k, 0) - alt.get(k, 0) for k in jetzt}

    # --- Clip anfordern -----------------------------------------------------------------
    def _darf_clippen(self, autor, s):
        """Leute aus dem Call (wer gerade drin sitzt oder im Puffer redet) und Mods."""
        if _ist_mod(autor):
            return True
        im = getattr(getattr(getattr(autor, "voice", None), "channel", None), "id", None)
        if im is not None and im == s.kanal_id:
            return True
        return s.puffer is not None and _ganz(getattr(autor, "id", 0)) in s.puffer.spuren

    def _buchen(self, gid, uid):
        """Abkuehlen und Stundendeckel anrechnen - gleich nach der Pruefung
        (_sperre_fuer), vor dem ersten await. -> die Marke fuer _erstatten."""
        jetzt = time.monotonic()
        self._zuletzt[uid] = jetzt
        self._stunde.setdefault(gid, deque()).append(jetzt)
        return jetzt

    def _sperre_fuer(self, gid, uid):
        """Abkuehlen und Stundendeckel -> Text oder None."""
        jetzt = time.monotonic()
        rest = ABKUEHLEN_SEK - (jetzt - self._zuletzt.get(uid, float("-inf")))
        if rest > 0:
            return self._text("abkuehlen", s=max(1, int(rest + 0.999)))
        stunde = self._stunde.get(gid)
        if stunde is not None:
            while stunde and jetzt - stunde[0] > 3600:
                stunde.popleft()
            if len(stunde) >= STUNDE_MAX:
                return self._text("zu_viele")
        return None

    async def _clip(self, message, guild, dauer, ziel):
        gid = guild.id
        autor = message.author
        if not self._kann_rendern:
            return self._text("grund_render")
        if not guildcfg.an(gid, "clip_aufnahme"):
            return self._text("nicht_erlaubt_server")
        if _sendepause():
            return self._text("grund_sendepause")
        if self.ist_raus(autor.id):
            return await self._leise(message, self._text("selbst_raus"))
        s = self._sitzung(gid)
        if not s.aktiv:
            if not _verbunden(getattr(guild, "voice_client", None)):
                return self._text("kein_voice_flo")
            # Der Grund von JETZT - s.grund kann bis zu einem Takt alt sein.
            soll, grund, _vc = self._soll(guild, s)
            return self._grund_text(grund if not soll else (s.grund or "start"))
        if s.getrennt_seit is not None or s.puffer is None:
            return self._grund_text("getrennt")
        if not self._darf_clippen(autor, s):
            return self._text("zaungast")
        if ziel == "ich":
            ziel = autor
        if ziel is not None:
            if self.ist_raus(ziel.id) or basis.kuerzlich_notlage(ziel.id):
                return await self._leise(message, self._text("person_raus", wer=_name(ziel)))
        sperre = self._sperre_fuer(gid, autor.id)
        if sperre:
            return sperre
        if self._in_arbeit >= WARTESCHLANGE_MAX:
            return self._text("belegt")
        if dauer is None:
            offen = self._dialoge.get(gid) or {}
            if autor.id in offen or (gid, autor.id) in self._dialog_kommt:
                return self._text("dialog_offen")
            kommen = sum(1 for g, _u in self._dialog_kommt if g == gid)
            if len(offen) + kommen >= DIALOGE_JE_SERVER:
                return self._text("dialoge_voll")
        bis = self._uhr()
        if bis - s.seit < MIN_REST_SEK:
            return self._text("grund_start")
        if dauer is not None:
            return await self._clip_direkt(message, guild, s, bis, dauer, ziel)
        return await self._dialog_oeffnen(message, guild, s, bis, ziel)

    def _schnappschuss(self, s, bis):
        """Was im Fenster bis 'bis' liegt - samt dem, was gerade noch unterwegs
        war (der Empfaenger arbeitet seinen Eingang vorher ab). None, wenn
        inzwischen nicht mehr zugehoert wird."""
        if not s.aktiv or s.puffer is None:
            return None
        if s.empfaenger is not None:
            s.empfaenger.abarbeiten()
        return s.puffer.schnappschuss(bis, s.sperre_gen)

    async def _clip_direkt(self, message, guild, s, bis, dauer, ziel):
        """'flo clip 30': gleich schneiden. Abkuehlen und Stunde werden SOFORT
        angerechnet - vor dem ersten await. Sonst kaemen zwei gleichzeitige
        'flo clip 30' beide an der Pruefung vorbei (zwei Schnitte, zwei Posts)."""
        gid = guild.id
        autor = message.author
        wann = self._buchen(gid, autor.id)
        # Das Fenster endet JETZT - nur was gerade noch unterwegs war, kommt dazu.
        await asyncio.sleep(SETTLE_SEK)
        snap = self._schnappschuss(s, bis)
        if snap is None:
            self._erstatten(gid, autor.id, wann)
            return self._grund_text(s.grund or "start")
        with self._unterwegs(gid, snap.kanal_id):
            antwort = await basis.antworte(message, self._text(
                "schneide", dauer=clipstext.dauer_text(dauer)))
            # Die Zwischenmeldung bekommt das Ergebnis - in einem Aufraeum-Kanal
            # darf sie bis dahin nicht weg sein.
            self.schuetzen(antwort)
            try:
                await self._job(guild, snap, dauer, getattr(ziel, "id", None), autor,
                                antwort=antwort, anfrage=message,
                                ziel_name=_name(ziel) if ziel is not None else None,
                                angerechnet=wann)
            finally:
                self.freigeben(antwort)
        return HANDLED

    async def _dialog_oeffnen(self, message, guild, s, bis, ziel):
        """'flo clip': Laenge und Person per Knopf. Der Platz (einer je Person,
        DIALOGE_JE_SERVER je Server) ist belegt, bevor das erste await kommt -
        zwei gleichzeitige 'flo clip' oeffnen keine zwei Dialoge."""
        gid = guild.id
        autor = message.author
        platz = (gid, autor.id)
        self._dialog_kommt.add(platz)
        try:
            await asyncio.sleep(SETTLE_SEK)
            snap = self._schnappschuss(s, bis)
            if snap is None:
                return self._grund_text(s.grund or "start")
            sprecher = self._sprecher_liste(snap, autor.id)
            frage = "frage"
            if self._privat_fuer(guild, snap.kanal_id, getattr(message, "channel", None)):
                # Gefragt in einem Kanal, den auch Leute lesen, die den (privaten)
                # Voice nicht sehen: das Menue zeigte ihnen, wer dort redet. Also
                # nur 'Alle' - und wen der Fragende selbst schon genannt hat.
                sprecher = [(uid, name) for uid, name in sprecher
                            if uid == getattr(ziel, "id", None)]
                frage = "frage_privat"
            dialog = ClipDialog(guild, autor, snap, sprecher,
                                vorauswahl=getattr(ziel, "id", None), anfrage=message)
            msg = await basis.antworte(message, self._text(frage, sek=DIALOG_SEK), view=dialog)
            if msg is None:
                dialog.stop()
                return HANDLED
            dialog.message = msg
            self.schuetzen(msg)
            self._dialoge.setdefault(gid, {})[autor.id] = dialog
            return HANDLED
        finally:
            self._dialog_kommt.discard(platz)

    @staticmethod
    def _privat_fuer(guild, voice_id, kanal):
        """Ist dieser Voice privat gegenueber 'kanal' - lesen dort Leute mit, die
        den Voice (und seinen Chat) gar nicht sehen? Im Voice-Chat selbst nie."""
        voice = guild.get_channel(voice_id) if hasattr(guild, "get_channel") else None
        if voice is None or kanal is None:
            return False
        if getattr(kanal, "id", None) == getattr(voice, "id", None):
            return False
        try:
            return bool(basis.privat(voice, kanal, guild))
        except Exception:  # noqa: BLE001 - im Zweifel fuer die Privatsphaere
            return True

    def _sprecher_liste(self, snap, von_uid):
        """Wer im Menue steht: Menschen mit Ton im Schnappschuss - ohne wen,
        der raus ist oder gerade am Boden (der steht dort gar nicht erst)."""
        liste = []
        for uid, (name, bot, pakete) in snap.stimmen.items():
            if bot or self.ist_raus(uid) or basis.kuerzlich_notlage(uid):
                continue
            sek = _stimm_sek(pakete, snap.bis - PUFFER_SEK, snap.bis)
            if sek > 0:
                liste.append((sek, uid, name))
        liste.sort(key=lambda e: (-e[0], e[1]))
        return [(uid, name) for _sek, uid, name in liste]

    def dialog_weg(self, dialog, freigeben=True):
        """Der Dialog ist zu (Klick, Abbruch, Timeout). 'freigeben': den
        Loesch-Schutz seiner Nachricht gleich aufheben - beim Schneiden erst,
        wenn das Ergebnis drinsteht (in einem Aufraeum-Kanal loescht die
        Freigabe die Nachricht nach der Gnadenfrist)."""
        gid = getattr(dialog.guild, "id", None)
        offen = self._dialoge.get(gid) or {}
        if offen.get(dialog.besitzer.id) is dialog:
            offen.pop(dialog.besitzer.id, None)
        if not offen:
            self._dialoge.pop(gid, None)
        if freigeben and dialog.message is not None:
            self.freigeben(dialog.message)

    async def dialog_schneiden(self, dialog, sek):
        """Ein Laengen-Knopf im Dialog: den Schnitt fahren - mit dem Stand von
        der Frage."""
        snap = dialog.snap
        dialog.snap = None
        self.dialog_weg(dialog, freigeben=False)
        try:
            if snap is None:
                await self._melden(dialog.message, dialog.anfrage, self._text_fuer(
                    dialog.guild, "verworfen_aus"))
                return
            autor = dialog.besitzer
            sperre = self._sperre_fuer(dialog.guild.id, autor.id)
            if sperre:
                await self._melden(dialog.message, dialog.anfrage, sperre)
                return
            wann = self._buchen(dialog.guild.id, autor.id)      # vor dem ersten await
            with self._unterwegs(dialog.guild.id, snap.kanal_id):
                await self._job(dialog.guild, snap, sek, dialog.wahl, autor,
                                antwort=dialog.message, anfrage=dialog.anfrage,
                                angerechnet=wann)
        finally:
            self.freigeben(dialog.message)

    def _text_fuer(self, guild, schluessel, **werte):
        """Ein Text fuer einen Server ausserhalb von on_message (Knopf, Takt)."""
        werte.setdefault("name", self.name_fuer(getattr(guild, "id", 0)))
        return TEXTE[schluessel].format(**werte)

    @staticmethod
    async def _melden(antwort, anfrage, text):
        """Das Ergebnis dem, der gefragt hat: die Zwischenmeldung umschreiben -
        geht das nicht, eine neue Antwort."""
        if antwort is not None:
            try:
                await antwort.edit(content=text, view=None)
                return
            except Exception:  # noqa: BLE001 - geloescht, Attrappe ohne edit
                log.debug("Clips: Zwischenmeldung liess sich nicht umschreiben", exc_info=True)
        if anfrage is not None:
            await basis.antworte(anfrage, text)

    # --- Der Schnitt -----------------------------------------------------------------
    @staticmethod
    def _budget(guild):
        try:
            grenze = int(getattr(guild, "filesize_limit", 0) or 0)
        except (TypeError, ValueError):
            grenze = 0
        if grenze <= 0:
            grenze = 10 * 1024 * 1024
        return int(min(BUDGET, grenze - BUDGET_RESERVE))

    def _auftrag_bauen(self, guild, snap, dauer, ziel_uid, von, abbruch, ziel_name=None):
        """-> (Auftrag, Info) oder (None, Text). Wer raus ist, fehlt; wer am
        Boden ist, fehlt still (ausser es ist der, der fragt)."""
        def wer(fallback):
            if ziel_uid in snap.stimmen:
                return _sauber(snap.stimmen[ziel_uid][0], 40)
            return ziel_name or fallback

        verfuegbar = snap.bis - snap.seit
        if verfuegbar < MIN_REST_SEK:
            return None, self._text_fuer(guild, "grund_start")
        kuerzer = None
        if verfuegbar < dauer:
            kuerzer = self._text_fuer(guild, "kuerzer",
                                      seit=clipstext.dauer_text(verfuegbar))
            dauer = max(MIN_REST_SEK, round(verfuegbar, 1))
        von_t = snap.bis - dauer
        if ziel_uid is not None and (basis.kuerzlich_notlage(ziel_uid)
                                     or self.ist_raus(ziel_uid)):
            return None, self._text_fuer(guild, "person_raus", wer=wer("Die Person"))
        stimmen, namen, menschen = [], {}, 0.0
        for uid, (name, bot, pakete) in snap.stimmen.items():
            if self.ist_raus(uid):
                continue
            if uid != von.id and basis.kuerzlich_notlage(uid):
                continue
            if ziel_uid is not None and uid != ziel_uid:
                continue
            fenster = [p for p in pakete if von_t - 1.0 <= p[0] <= snap.bis + 0.3]
            sek = _stimm_sek(fenster, von_t, snap.bis)
            if sek <= 0:
                continue
            stimmen.append(clipschnitt.Spurdaten(uid=uid, name=name, bot=bot, pakete=fenster))
            namen[uid] = name
            if not bot:
                menschen += sek
        flo = [] if ziel_uid is not None else [
            (t, o) for t, o in snap.flo if von_t - 1.0 <= t <= snap.bis + 0.3]
        if ziel_uid is not None and menschen < STIMME_MIN_SEK:
            return None, self._text_fuer(guild, "person_stumm", wer=wer("der Person"))
        if menschen < STIMME_MIN_SEK:
            leer = "nur_musik" if _flo_ton(flo, von_t, snap.bis) else "leer"
            return None, self._text_fuer(guild, leer)
        voice = guild.get_channel(snap.kanal_id) if hasattr(guild, "get_channel") else None
        titel = f"Clip aus {getattr(voice, 'name', 'dem Voice')} – {uhr.jetzt():%d.%m.%Y %H:%M}"
        whisper = bool(clipstext.transkriber.an()
                       and not clipstext.transkriber.kontingent.gesperrt())
        auftrag = clipschnitt.Auftrag(
            bis=snap.bis, dauer=float(dauer), stimmen=stimmen, flo=flo,
            budget=self._budget(guild), titel=titel, whisper=whisper,
            frist=float(RENDER_FRIST - 10), abbruch=abbruch)
        return auftrag, {"kuerzer": kuerzer, "namen": namen, "dauer": dauer, "voice": voice}

    async def _job(self, guild, snap, dauer, ziel_uid, von, *, antwort=None, anfrage=None,
                   ziel_name=None, angerechnet=None):
        """Vom Schnappschuss zum Post. Jede Stufe darf verwerfen - geschrieben
        wird nur, was nach dem Schnitt immer noch gilt. 'angerechnet': die
        Marke aus _buchen (der Aufrufer hat Abkuehlen und Stunde schon
        belegt); wird gar nicht erst geschnitten, gibt es sie zurueck."""
        gid = guild.id
        s = self._sitzung(gid)
        gen = snap.sperre_gen

        def verworfen():
            return self._abbruch or s.sperre_gen != gen

        async def ohne_schnitt(text):
            self._erstatten(gid, von.id, angerechnet)
            await self._melden(antwort, anfrage, text)

        if verworfen():
            await ohne_schnitt(self._text_fuer(guild, "verworfen_aus"))
            return
        if self.ist_raus(von.id):
            await ohne_schnitt(self._text_fuer(guild, "verworfen_raus"))
            return
        auftrag, info = self._auftrag_bauen(guild, snap, dauer, ziel_uid, von, verworfen,
                                            ziel_name)
        if auftrag is None:
            await ohne_schnitt(info)
            return
        if self._in_arbeit >= WARTESCHLANGE_MAX:
            await ohne_schnitt(self._text_fuer(guild, "belegt"))
            return
        jetzt = angerechnet if angerechnet is not None else self._buchen(gid, von.id)
        info["angerechnet"] = jetzt
        self._in_arbeit += 1
        try:
            async with self._render_semaphore():
                if verworfen():
                    await self._melden(antwort, anfrage, self._text_fuer(guild, "verworfen_aus"))
                    return
                try:
                    erg = await asyncio.wait_for(asyncio.to_thread(clipschnitt.rendern, auftrag),
                                                 RENDER_FRIST)
                except clipschnitt.RenderFehler as exc:
                    if verworfen():
                        await self._melden(antwort, anfrage,
                                           self._text_fuer(guild, "verworfen_aus"))
                    elif exc.grund == "leer":
                        await self._melden(antwort, anfrage, self._text_fuer(guild, "leer"))
                    else:
                        log.warning("Clips: Server %s – Schnitt gescheitert (%s).", gid,
                                    exc.grund)
                        self._erstatten(gid, von.id, jetzt)
                        await self._melden(antwort, anfrage,
                                           self._text_fuer(guild, "render_kaputt"))
                    return
                except asyncio.TimeoutError:
                    log.warning("Clips: Server %s – Schnitt nach %d s abgebrochen.", gid,
                                RENDER_FRIST)
                    self._erstatten(gid, von.id, jetzt)
                    await self._melden(antwort, anfrage, self._text_fuer(guild, "render_kaputt"))
                    return
        finally:
            self._in_arbeit -= 1
        await self._nach_dem_schnitt(guild, s, gen, erg, info, ziel_uid, von, antwort, anfrage)

    def _erstatten(self, gid, uid, wann):
        """Ein Clip, der TECHNISCH scheiterte (ffmpeg, Discord lehnt den Upload
        ab), zaehlt weder fuers Abkuehlen noch fuer die Stunde: der Text sagt
        'Nochmal versuchen', also muss das auch gleich gehen."""
        if wann is None:
            return
        if self._zuletzt.get(uid) == wann:
            del self._zuletzt[uid]
        stunde = self._stunde.get(gid)
        if stunde is not None and wann in stunde:
            stunde.remove(wann)

    def _verworfen_text(self, guild, s, gen, von, dabei):
        """Gilt der Clip noch? -> Text, wenn nicht: Mikro aus (oder
        Herunterfahren, Funktion bzw. Einwilligung des Servers aus) seit dem
        Schnappschuss, Sendepause, der Fragende bzw. jemand, der im Clip zu
        hoeren ist, ist inzwischen raus - oder jemand im Clip (ausser dem
        Fragenden) ist inzwischen am Boden. Gefragt nach dem Schnitt, nach dem
        Transkript, unmittelbar vor dem Hochladen und gleich danach: der
        KI-Spruch allein darf bis zu clipstext.KI_FRIST dauern, und genau dann
        druecken die Leute auf 🙅."""
        gid = guild.id
        if (self._abbruch or s.sperre_gen != gen or not features.is_on_in(gid, "clips")
                or not guildcfg.an(gid, "clip_aufnahme")):
            return self._text_fuer(guild, "verworfen_aus")
        if _sendepause():
            return self._text_fuer(guild, "grund_sendepause")
        if self.ist_raus(von.id) or any(self.ist_raus(uid) for uid in dabei):
            return self._text_fuer(guild, "verworfen_raus")
        if any(uid != von.id and basis.kuerzlich_notlage(uid) for uid in dabei):
            # Kam waehrend des Schnitts ein Notsignal, ist der Ton schon drin -
            # still draussen lassen geht nicht mehr, also bleibt der Clip liegen.
            return self._text_fuer(guild, "zurueckgehalten")
        return None

    async def _nach_dem_schnitt(self, guild, s, gen, erg, info, ziel_uid, von, antwort, anfrage):
        gid = guild.id
        dauer = info["dauer"]
        dabei = [uid for uid, sek in (erg.stimm_sek or {}).items() if sek and sek > 0]
        # Waehrend ffmpeg rechnete: Mikro aus? Jemand ausgestiegen? Dann geht der
        # Ton auch nicht mehr zu Whisper - nicht erst der Post faellt weg.
        verworfen = self._verworfen_text(guild, s, gen, von, dabei)
        if verworfen:
            await self._melden(antwort, anfrage, verworfen)
            return
        zeilen, befund = [], None
        if erg.whisper:
            segmente = await clipstext.transkriber.je_sprecher(erg.whisper, frist=WHISPER_FRIST)
            self._kontingent_merken()
            if segmente:
                zeilen = clipstext.zeilen_bauen(segmente, erg.whisper, info["namen"])
                befund = clipstext.pruefen(zeilen)
                if befund.notlage_uids:
                    for uid in befund.notlage_uids:
                        _notlage_melden(gid, uid)
                    log.info("Clips: Server %s – Clip zurückgehalten (Notsignal im Transkript).",
                             gid)
                    await self._melden(antwort, anfrage,
                                       self._text_fuer(guild, "zurueckgehalten"))
                    return
                if befund.hetze:
                    log.info("Clips: Server %s – Clip zurückgehalten (Hetze im Transkript).", gid)
                    await self._melden(antwort, anfrage, self._text_fuer(guild, "hetze"))
                    return
        # Nach dem Transkript (bis WHISPER_FRIST) nochmal - erst dann der Post.
        verworfen = self._verworfen_text(guild, s, gen, von, dabei)
        if verworfen:
            await self._melden(antwort, anfrage, verworfen)
            return
        kommentar = None
        if not (basis.kuerzlich_notlage(von.id)
                or any(basis.kuerzlich_notlage(uid) for uid in dabei)):
            if zeilen and features.is_on_in(gid, "ki"):
                kommentar = await clipstext.kommentar(gid, zeilen, dauer)
            kommentar = kommentar or clipstext.ersatz_kommentar()
        voice = info.get("voice")
        ziel, hier, fehler = self._zielkanal(guild, voice)
        if fehler:
            await self._melden(antwort, anfrage, fehler)
            return
        # Der Spruch kann bis zu clipstext.KI_FRIST gedauert haben - wer in der
        # Zeit 🙅 oder 🔇 gedrueckt hat, kommt nicht in den Post. Die letzte
        # Pruefung steht deshalb HIER, unmittelbar vor dem Hochladen.
        verworfen = self._verworfen_text(guild, s, gen, von, dabei)
        if verworfen:
            await self._melden(antwort, anfrage, verworfen)
            return
        if kommentar and basis.kuerzlich_notlage(von.id):
            kommentar = None                # inzwischen am Boden: kein Spott
        name = self.name_fuer(gid)
        teile = [TEXTE["post_kopf"].format(
            dauer=clipstext.dauer_text(dauer), voice=_sauber(getattr(voice, "name", "Voice"), 40),
            von=f"<@{_ganz(von.id)}>")]
        if ziel_uid is not None:
            teile.append(TEXTE["post_nur"].format(wer=f"<@{_ganz(ziel_uid)}>"))
        elif dabei:
            teile.append(TEXTE["post_dabei"].format(
                namen=", ".join(f"<@{_ganz(uid)}>" for uid in dabei)))
        if kommentar:
            teile.append("> " + kommentar)
        teile.append(TEXTE["post_fuss"].format(name=name))
        embed = None
        if zeilen:
            embed = discord.Embed(title="📝 Transkript (KI, ohne Gewähr)",
                                  description=clipstext.transkript_text(
                                      zeilen, zensiert=befund.heikel if befund else frozenset()),
                                  color=clipstext.FARBE)
        id8 = self._neue_id(gid)
        dateiname = f"flo-clip-{uhr.jetzt():%Y%m%d-%H%M}-{int(round(dauer))}s.mp3"
        datei = discord.File(io.BytesIO(erg.mp3), filename=dateiname)
        try:
            msg = await ziel.send("\n".join(teile), file=datei, embed=embed,
                                  view=ClipView(id8, 0),
                                  allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await self._melden(antwort, anfrage, self._text_fuer(guild, "keine_dateien"))
            return
        except discord.HTTPException as exc:
            log.warning("Clips: Server %s – Upload abgelehnt (%s).", gid,
                        getattr(exc, "status", "?"))
            self._erstatten(gid, von.id, info.get("angerechnet"))
            await self._melden(antwort, anfrage, self._text_fuer(guild, "upload_kaputt"))
            return
        # Das Hochladen (ein paar MB) dauert auch: kam waehrenddessen ein
        # Ausstieg oder 🔇, kommt der Post gleich wieder weg.
        nachher = self._verworfen_text(guild, s, gen, von, dabei)
        if nachher:
            try:
                await msg.delete()
            except Exception:  # noqa: BLE001 - dann bleibt er stehen; 🗑️ geht trotzdem
                log.warning("Clips: Server %s – verworfener Clip liess sich nicht löschen.",
                            gid)
            else:
                log.info("Clips: Server %s – Clip gleich nach dem Hochladen verworfen.", gid)
                await self._melden(antwort, anfrage, nachher)
                return
        sprecher = sorted(set(dabei) | ({_ganz(ziel_uid)} if ziel_uid else set()))
        self._clips_von(gid, anlegen=True)[id8] = {
            "mid": _ganz(msg.id), "cid": _ganz(getattr(ziel, "id", 0)),
            "vc": _ganz(getattr(voice, "id", 0)), "ts": round(_wand()), "von": _ganz(von.id),
            "dauer": int(round(dauer)), "sprecher": sprecher, "stimmen": []}
        self._store.save_soon()
        log.info("Clips: Server %s – Clip %s gepostet (%.0f s, %d Sprecher, %d kbps, %.2f MB%s).",
                 gid, id8, dauer, len(sprecher), erg.kbps, len(erg.mp3) / 1048576,
                 ", Transkript" if zeilen else "")
        link = getattr(msg, "jump_url", "")
        if hier:
            # Im Voice-Chat gefragt: "hier unter euch". Sonst steht die Antwort
            # in einem Kanal, den auch andere lesen - dort nur der Verweis.
            gefragt_in = getattr(getattr(anfrage, "channel", None), "id", None)
            schluessel = "fertig_hier" if gefragt_in == getattr(ziel, "id", None) \
                else "fertig_privat"
        else:
            schluessel = "fertig"
        text = self._text_fuer(guild, schluessel, link=link)
        if info.get("kuerzer"):
            text += "\n" + info["kuerzer"]
        await self._melden(antwort, anfrage, text)

    def _zielkanal(self, guild, voice):
        """-> (kanal, privat_hier, fehlertext). Privater Voice: der Clip bleibt
        in dessen Voice-Chat - oder es gibt keinen. Ohne Clip-Kanal zaehlt
        @everyone als Publikum."""
        ziel = self._zielkanal_roh(guild)
        if voice is not None and basis.privat(voice, ziel, guild):
            try:
                rechte = voice.permissions_for(guild.me)
                darf = bool(rechte.send_messages and rechte.attach_files)
            except Exception:  # noqa: BLE001
                darf = False
            if not darf:
                return None, False, self._text_fuer(guild, "privat_zu")
            return voice, True, None
        if ziel is None:
            cid = _ganz(guildcfg.get(guild.id, "clip_channel"))
            eingestellt = guild.get_channel(cid) if cid and hasattr(guild, "get_channel") else None
            if eingestellt is not None:
                try:
                    rechte = eingestellt.permissions_for(guild.me)
                    if rechte.send_messages and not rechte.attach_files:
                        return None, False, self._text_fuer(guild, "keine_dateien")
                except Exception:  # noqa: BLE001
                    pass
            return None, False, self._text_fuer(guild, "kein_kanal")
        return ziel, False, None

    def _neue_id(self, gid):
        vorhanden = self._clips_von(gid)
        while True:
            id8 = "".join(secrets.choice("0123456789abcdefghijklmnopqrstuvwxyz")
                          for _ in range(8))
            if id8 not in vorhanden:
                return id8

    # --- Knoepfe -------------------------------------------------------------------
    async def ohne_mich_knopf(self, interaction):
        """🙅 - geht immer, auch in der Sendepause und wo Clips aus sind (auch
        wenn sie im ganzen Prozess nicht laufen: der Speicher steht trotzdem)."""
        gid = _ganz(getattr(interaction, "guild_id", 0))
        if self._store is None:
            await _kurz(interaction, TEXTE["aus_tot"])
            return
        uid = interaction.user.id
        if self.ist_raus(uid):
            text = TEXTE["schon_raus"]
        else:
            self.raus(uid)
            text = TEXTE["raus"].format(name=self.name_fuer(gid))
        await _kurz(interaction, text)

    async def aus_knopf(self, interaction, vc_id):
        """🔇 - wer im Call sitzt oder Mod ist. Erst antworten (3 s), dann der Rest."""
        guild = getattr(interaction, "guild", None)
        if guild is None or self._store is None:
            await _kurz(interaction, TEXTE["aus_schon"])
            return
        wer = interaction.user
        im = getattr(getattr(getattr(wer, "voice", None), "channel", None), "id", None)
        if im != vc_id and not _ist_mod(wer):
            await _kurz(interaction, TEXTE["zaungast_mikro"])
            return
        s = self._sitzung(guild.id)
        if not (s.aktiv and s.kanal_id == vc_id):
            # Flo hoert hier nicht (mehr) mit - was aus dieser Runde noch
            # unterwegs ist (offener Dialog, laufender Schnitt), stirbt trotzdem.
            if vc_id in self._offen_aus(guild.id):
                self._offenes_verwerfen(guild)
                await _kurz(interaction, self._text_fuer(guild, "aus_verworfen"))
            else:
                await _kurz(interaction, TEXTE["aus_schon"])
            return
        geht = self._mikro_aus_sofort(guild, vc_id)
        await _kurz(interaction, self._text_fuer(guild, "aus_raus" if geht else "aus_ok"))
        await self._mikro_aus_rest(guild, vc_id, wer, geht)

    async def feuer_knopf(self, interaction, cid):
        gid = _ganz(getattr(interaction, "guild_id", 0))
        if not self._enabled or self._store is None or not features.is_on_in(gid, "clips"):
            await _kurz(interaction, TEXTE["aus_hier"])
            return
        if _sendepause():
            await _kurz(interaction, TEXTE["grund_sendepause"])
            return
        eintrag = self._clips_von(gid).get(cid)
        if not isinstance(eintrag, dict):
            await _kurz(interaction, TEXTE["feuer_weg"].format(wochen=CLIPS_TAGE // 7))
            return
        uid = interaction.user.id
        if uid == _ganz(eintrag.get("von")):
            await _kurz(interaction, TEXTE["feuer_selbst"])
            return
        stimmen = eintrag.get("stimmen")
        if not isinstance(stimmen, list):
            stimmen = eintrag["stimmen"] = []
        if uid in stimmen:
            stimmen.remove(uid)
        else:
            stimmen.append(uid)
        self._store.save_soon()
        try:
            await interaction.response.edit_message(view=ClipView(cid, len(stimmen)))
        except discord.HTTPException:
            log.debug("Clips: Flammen-Zaehler liess sich nicht umschreiben", exc_info=True)

    async def weg_knopf(self, interaction, cid):
        """🗑️ - wer geclippt hat, wer im Clip ist, und wer Nachrichten verwalten
        darf. Geht immer (Datenschutz), auch wo Clips aus sind."""
        gid = _ganz(getattr(interaction, "guild_id", 0))
        uid = interaction.user.id
        eintrag = self._clips_von(gid).get(cid) if self._store is not None else None
        if isinstance(eintrag, dict):
            beteiligt = {_ganz(eintrag.get("von"))} | {
                _ganz(u) for u in eintrag.get("sprecher") or ()}
        else:
            # Nach CLIPS_TAGE hat der Index ihn vergessen - wer drin ist, steht
            # aber noch im Post selbst. Loeschen duerfen sie fuer immer.
            beteiligt = _beteiligte_im_post(getattr(getattr(
                interaction, "message", None), "content", ""))
        darf = _ganz(uid) in beteiligt - {0}
        rechte = getattr(interaction, "permissions", None)
        mod = bool(getattr(rechte, "manage_messages", False)) or bool(getattr(
            getattr(interaction.user, "guild_permissions", None), "manage_messages", False))
        if not (darf or mod):
            await _kurz(interaction, TEXTE["weg_nein"])
            return
        await _kurz(interaction, TEXTE["weg_ok"])
        try:
            await interaction.message.delete()
        except Exception:  # noqa: BLE001 - kein Recht, schon weg
            await _kurz(interaction, TEXTE["weg_kaputt"])
            return
        if self._store is not None and self._clips_von(gid).pop(cid, None) is not None:
            self._store.save_soon()
        log.info("Clips: Server %s – Clip %s gelöscht.", gid, cid)

    # --- fuer bot._keep_bot_msg ---------------------------------------------------------
    @staticmethod
    def ist_ansage(msg):
        """Ist das eine Clip-Ansage (am 🙅-Knopf erkannt)?"""
        return any(getattr(teil, "custom_id", None) == OHNE_ID for teil in basis.bausteine(msg))

    def ansage_behalten(self, msg):
        """bot._keep_bot_msg: bleibt diese Ansage vom Auto-Loeschen verschont?
        Nur die AKTUELLE eines Kanals (clips.json) - und nur, solange Flo dort
        zuhoert oder sie juenger als ANSAGE_PAUSE ist (dann holt eine neue Runde
        sie zurueck, statt neu anzukuendigen). Jede andere - eine alte, eine von
        vor einem Neustart, auf den keine Runde mehr folgte - raeumt das
        Auto-Loeschen weg, statt dass sie fuer immer 'Ab jetzt hör ich hier mit'
        sagt. Waehrend der Runde schuetzt sie ohnehin schuetzen()."""
        if self._store is None or not self.ist_ansage(msg):
            return False
        cid = _ganz(getattr(getattr(msg, "channel", None), "id", 0))
        eintrag = self._daten("ansagen").get(str(cid))
        if not isinstance(eintrag, dict) or _ganz(eintrag.get("mid")) != _ganz(
                getattr(msg, "id", 0)):
            return False
        if any(s.aktiv and s.kanal_id == cid for s in self._sitzungen.values()):
            return True
        try:
            ts = basis.glaubhafte_zeit(float(eintrag.get("ts") or 0.0))
        except (TypeError, ValueError):
            return False
        return _wand() - ts < ANSAGE_PAUSE

    @staticmethod
    def ist_clip(msg):
        """Ist das ein Clip-Post (am 🔥-Knopf erkannt)? Der Clip-Kanal ist nie ein
        Aufraeum-Kanal - der Voice-Chat eines privaten Voice kann einer sein, und
        dort soll der Clip nicht nach einer Minute verschwinden."""
        return any(str(getattr(teil, "custom_id", None) or "").startswith(FEUER_PRAEFIX)
                   for teil in basis.bausteine(msg))


def _einstellung_neu(gid):
    """guildcfg meldet: 'clip_aufnahme' dieses Servers hat sich geaendert."""
    if instance._enabled:
        instance.sitzung_geaendert(gid)


instance = Clips()

setup = instance.setup
is_enabled = instance.is_enabled
handle = instance.handle
datenschutz = instance.datenschutz
tick = instance.tick
voice_state = instance.voice_state
herunterfahren = instance.herunterfahren
ist_ansage = instance.ist_ansage
ansage_behalten = instance.ansage_behalten
nur_datenschutz = instance.nur_datenschutz
datenschutz_dm = instance.datenschutz_dm
ist_clip = instance.ist_clip
geloescht = instance.geloescht
