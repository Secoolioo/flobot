"""Die neuen Discord-Funktionen: Umfragen, Rechtsklick-Befehle, Einladungslink.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur discord      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import _RauchKanal, _rauch_nachricht  # noqa: F401

import datetime
from unittest import mock

import discord

import ai
import fun
import umfrage


# --- Umfragen ------------------------------------------------------------------
class _UmfrageKanal(_RauchKanal):
    """Kanal, der sich merkt, was gesendet wurde - samt poll= und reference=."""

    def __init__(self, send_polls=True):
        super().__init__(cid=4242)
        self._send_polls = send_polls

    async def send(self, content=None, **kw):
        self.gesendet.append((content, kw))
        return SimpleNamespace(id=len(self.gesendet), channel=self)

    def permissions_for(self, _wer):
        return SimpleNamespace(send_polls=self._send_polls)


def _umfrage_msg(text, kanal=None, uid=31337):
    kanal = kanal or _UmfrageKanal()
    msg = _rauch_nachricht(text, uid=uid, kanal=kanal)
    msg.to_reference = lambda **_k: SimpleNamespace(message_id=1)
    return msg, kanal


def _umfrage_frisch():
    inst = umfrage.Umfrage()
    inst.setup()
    return inst


def test_umfrage_greift_nur_bei_eindeutigen_befehlen():
    """'flo umfrage ist doof' ist eine Meinung ueber Umfragen, keine Umfrage.
    Die KI soll darauf antworten - nicht eine Umfrage 'ist doof?' posten."""
    inst = _umfrage_frisch()
    for harmlos in ("umfrage ist doof", "umfragen sind nervig", "poll war lustig",
                    "abstimmung von gestern", "was ist eine umfrage", "umfrage hat genervt",
                    "abstimmen ist pflicht"):
        assert inst.zerlegen(harmlos) is None, harmlos
    assert inst.zerlegen("umfrage") == (24, "", False)
    assert inst.zerlegen("umfrage 48h pizza oder döner") == (48, "pizza oder döner", False)
    assert inst.zerlegen("Umfrage: wer ist der größte Lappen?")[1] == "wer ist der größte Lappen?"
    assert inst.zerlegen("poll 2 tage urlaub?") == (48, "urlaub?", False)
    assert inst.zerlegen("voting 1w: bestes essen") == (168, "bestes essen", False)
    # Eine Zahl OHNE Einheit ist Teil der Frage, keine Laufzeit.
    assert inst.zerlegen("umfrage 5 dinge die nerven") == (24, "5 dinge die nerven", False)
    # Mehr als Discord kann: gedeckelt - und der Befehl merkt es sich fuer den Hinweis.
    assert inst.zerlegen("umfrage 900h x") == (768, "x", True)


def test_umfrage_nackt_zeigt_die_anleitung():
    inst = _umfrage_frisch()
    msg, kanal = _umfrage_msg("umfrage")
    antwort = asyncio.run(inst.handle(msg))
    assert isinstance(antwort, str) and "umfrage" in antwort and "|" in antwort
    assert not kanal.gesendet


def test_umfrage_von_hand_wird_echte_discord_umfrage():
    """Mit '|' baut Flo die Umfrage genau so, wie sie dasteht - ohne KI.
    Doppelte Antworten (auch anders geschrieben) fliegen raus, Discords
    Grenzen (55 Zeichen je Antwort) werden eingehalten."""
    inst = _umfrage_frisch()
    lang = "x" * 80
    msg, kanal = _umfrage_msg(f"umfrage 48h Pizza oder Döner? | Pizza | Döner | pizza | {lang}")
    assert asyncio.run(inst.handle(msg)) is umfrage.HANDLED
    (text, kw), = kanal.gesendet
    poll = kw["poll"]
    assert isinstance(poll, discord.Poll)
    assert poll.question == "Pizza oder Döner?"
    antworten = [a.text for a in poll.answers]
    assert antworten[:2] == ["Pizza", "Döner"] and len(antworten) == 3
    assert len(antworten[2]) <= umfrage.ANTWORT_MAX
    assert poll.duration == datetime.timedelta(hours=48)
    assert "2 Tage" in text
    assert "reference" in kw   # als Antwort auf den Befehl


def test_umfrage_von_hand_mit_einer_antwort_ist_keine():
    inst = _umfrage_frisch()
    msg, kanal = _umfrage_msg("umfrage Pizza? | Pizza")
    antwort = asyncio.run(inst.handle(msg))
    assert "Mindestens zwei" in antwort and not kanal.gesendet
    msg, kanal = _umfrage_msg("umfrage | a | b")
    assert "Frage" in asyncio.run(inst.handle(msg)) and not kanal.gesendet


def test_umfrage_ki_entwurf_bringt_den_guardrail_mit():
    """ai.generate laeuft ohne Persona und ohne Guardrail - die Umfrage muss die
    Grenze selbst mitbringen. Und die KI liefert gern Codebloecke, Nummern und
    zu viele Antworten: alles muss trotzdem eine gueltige Umfrage ergeben."""
    inst = _umfrage_frisch()
    gesehen = {}
    json_text = ('Hier:\n```json\n{"frage": "Welches Fast Food ist Müll?", "antworten": '
                 '["1. Burger", "2. Döner", "- Pizza", "Burger", '
                 + ", ".join(f'"A{i}"' for i in range(12)) + ']}\n```')

    async def generate(prompt, *, system=None, **_kw):
        gesehen["system"], gesehen["prompt"] = system, prompt
        return json_text

    msg, kanal = _umfrage_msg("umfrage bestes fast food")
    with mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        assert asyncio.run(inst.handle(msg)) is umfrage.HANDLED
    assert ai.FloAI._GUARDRAIL in gesehen["system"]
    assert "bestes fast food" in gesehen["prompt"]
    poll = kanal.gesendet[0][1]["poll"]
    antworten = [a.text for a in poll.answers]
    assert poll.question == "Welches Fast Food ist Müll?"
    assert antworten[:3] == ["Burger", "Döner", "Pizza"]
    assert len(antworten) == umfrage.ANTWORTEN_MAX


def test_umfrage_ohne_ki_bleibt_trotzdem_eine_umfrage():
    inst = _umfrage_frisch()

    async def kaputt(*_a, **_k):
        return "Ich kann dabei nicht helfen."

    msg, kanal = _umfrage_msg("umfrage wer ist der größte lappen")
    with mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", kaputt):
        assert asyncio.run(inst.handle(msg)) is umfrage.HANDLED
    poll = kanal.gesendet[0][1]["poll"]
    assert poll.question == "Wer ist der größte lappen?"
    assert len(poll.answers) >= 2


def test_umfrage_ueber_hetze_gibt_es_nicht():
    """Flo stellt die Umfrage in SEINEM Namen - ueber menschenfeindlichen Dreck
    laesst er nicht abstimmen, egal ob Thema, Frage oder Antwort."""
    inst = _umfrage_frisch()
    with mock.patch.object(fun.instance, "ist_hetze", lambda _t: True):
        for text in ("umfrage irgendwas", "umfrage Frage? | a | b"):
            msg, kanal = _umfrage_msg(text)
            assert asyncio.run(inst.handle(msg)) in umfrage._HETZE
            assert not kanal.gesendet


def test_umfrage_ohne_recht_sagt_es_derb():
    inst = _umfrage_frisch()
    msg, kanal = _umfrage_msg("umfrage Frage? | a | b", kanal=_UmfrageKanal(send_polls=False))
    assert asyncio.run(inst.handle(msg)) == umfrage._KEIN_RECHT
    assert not kanal.gesendet


def test_umfrage_abkuehlzeit_blockiert_nicht_nach_dem_hochfahren():
    """Die Monotonic-Falle: time.monotonic() zaehlt ab Rechnerstart. Mit 0.0
    als 'nie' waere die erste Umfrage nach einem Neustart des Servers eine
    Minute lang gesperrt."""
    inst = _umfrage_frisch()
    with mock.patch.object(umfrage.time, "monotonic", lambda: 10.0):
        msg, kanal = _umfrage_msg("umfrage Frage? | a | b")
        assert asyncio.run(inst.handle(msg)) is umfrage.HANDLED
        msg, _ = _umfrage_msg("umfrage Noch eine? | a | b", kanal=kanal)
        assert "Chill" in asyncio.run(inst.handle(msg))
    assert len(kanal.gesendet) == 1


def _ergebnis_msg(felder, kanal=None):
    kanal = kanal or _UmfrageKanal()
    embed = discord.Embed()
    for name, wert in felder.items():
        embed.add_field(name=name, value=str(wert))
    msg = SimpleNamespace(embeds=[embed], channel=kanal,
                          to_reference=lambda **_k: SimpleNamespace(message_id=5))
    return msg, kanal


def test_umfrage_ergebnis_kommentiert_sieg_patt_und_leere():
    inst = _umfrage_frisch()
    with mock.patch.object(ai, "is_enabled", lambda: False):
        msg, kanal = _ergebnis_msg({"poll_question_text": "Pizza?", "victor_answer_text": "Döner",
                                    "victor_answer_votes": 3, "total_votes": 5,
                                    "victor_answer_id": 2})
        assert asyncio.run(inst.ergebnis(msg))
        text, kw = kanal.gesendet[0]
        assert "Döner" in text and "3" in text and "5" in text
        # Discord postet das Ergebnis - Flo pingt dabei niemanden.
        assert kw["allowed_mentions"].users is False

        msg, kanal = _ergebnis_msg({"poll_question_text": "Pizza?", "total_votes": 4})
        asyncio.run(inst.ergebnis(msg))
        assert kanal.gesendet[0][0] in [z.format(frage="Pizza?", gesamt=4)
                                        for z in umfrage._PATT]

        msg, kanal = _ergebnis_msg({"poll_question_text": "Pizza?", "total_votes": 0})
        asyncio.run(inst.ergebnis(msg))
        assert kanal.gesendet[0][0] in [z.format(frage="Pizza?") for z in umfrage._NIEMAND]


def test_umfrage_ergebnis_ki_kommentar_mit_guardrail():
    inst = _umfrage_frisch()
    gesehen = {}

    async def generate(prompt, *, system=None, **_kw):
        gesehen["system"], gesehen["prompt"] = system, prompt
        return "Döner gewinnt, und ihr habt trotzdem keinen Geschmack, ihr Lappen."

    msg, kanal = _ergebnis_msg({"poll_question_text": "Pizza?", "victor_answer_text": "Döner",
                                "victor_answer_votes": 3, "total_votes": 5})
    with mock.patch.object(ai, "is_enabled", lambda: True), \
            mock.patch.object(ai, "generate", generate):
        asyncio.run(inst.ergebnis(msg))
    assert ai.FloAI._GUARDRAIL in gesehen["system"]
    assert "Döner" in gesehen["prompt"]
    assert kanal.gesendet[0][0].startswith("Döner gewinnt")


def test_bot_reicht_nur_flos_eigene_umfrage_ergebnisse_weiter():
    """Discord postet das Ergebnis im Namen dessen, der die Umfrage gestartet
    hat - fuer Flos Umfragen also als Flo, und damit als Bot. Der Bot-Check in
    on_message haette es verschluckt. Fremde Umfragen kommentiert Flo nicht."""
    import bot
    aufrufe = []

    async def ergebnis(msg):
        aufrufe.append(msg)

    alt_user = bot.client._connection.user
    bot.client._connection.user = SimpleNamespace(id=1)
    try:
        with mock.patch.object(bot.umfrage, "ergebnis", ergebnis), \
                mock.patch.object(bot, "UMFRAGE_ENABLED", True), \
                mock.patch.object(bot, "WEBPANEL_ENABLED", False):
            async def lauf():
                for autor_id in (1, 2):
                    msg = _rauch_nachricht("")
                    msg.author = SimpleNamespace(id=autor_id, bot=True, display_name="x")
                    msg.type = discord.MessageType.poll_result
                    await bot.client.on_message(msg)
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            asyncio.run(lauf())
    finally:
        bot.client._connection.user = alt_user
    assert [m.author.id for m in aufrufe] == [1]


def test_umfrage_ist_ueberall_angemeldet():
    import bot
    import cmdnorm
    import features
    assert any(f["key"] == "umfrage" for f in features.CATALOG)
    assert "umfrage" in bot.FEATURE_LOADED
    for wort in umfrage._CMDS:
        assert wort in cmdnorm.KNOWN, wort
        assert cmdnorm.normalize(f"{wort} test") is None
    hilfe = " ".join(b for _t, _f, zeilen in bot._HELP_DATA.values() for b, _ in zeilen)
    assert "flo umfrage" in hilfe


# --- Rechtsklick ---------------------------------------------------------------
class _Antwort:
    def __init__(self):
        self.verschoben = False
        self.privat = []

    async def defer(self, **_kw):
        self.verschoben = True

    async def send_message(self, text, ephemeral=False, **_kw):
        self.privat.append((text, ephemeral))


class _Nachfass:
    def __init__(self):
        self.gesendet = []

    async def send(self, text=None, **kw):
        self.gesendet.append((text, kw))


def _interaktion(uid=555, gid=77):
    return SimpleNamespace(user=SimpleNamespace(id=uid, display_name="Klicker"),
                           guild_id=gid, channel_id=4242, response=_Antwort(),
                           followup=_Nachfass())


def _rechtsklick_bot():
    import bot
    bot.client._rechtsklick_zuletzt.clear()
    return bot


def _alles_an(bot):
    """Schalter und Sendepause festnageln - andere Tests lassen sie gern
    anders stehen, und dann haengt das Ergebnis an der Reihenfolge."""
    return (mock.patch.object(bot.features, "is_on_in", lambda _g, _k: True),
            mock.patch.object(bot.admin, "is_locked", lambda: False))


def test_rechtsklick_befehle_sind_richtig_gebaut():
    """Zwei Eintraege im Apps-Menue: einer an Nachrichten, einer an Personen,
    beide nur auf Servern. Angemeldet wird im Hintergrund (_spawn)."""
    bot = _rechtsklick_bot()
    gestartet = []
    baum = bot.client.tree
    for befehl in list(baum.get_commands()):
        baum.remove_command(befehl.name, type=befehl.type)

    def spawn(coro):
        gestartet.append(coro)
        coro.close()

    with mock.patch.object(bot.client, "_spawn", spawn):
        bot.client._rechtsklick_anmelden()
        bot.client._rechtsklick_anmelden()   # zweimal (Reconnect): kein Absturz
    befehle = {b.name: b for b in baum.get_commands()}
    assert set(befehle) == {"Flo, sag was dazu", "Roasten"}
    assert befehle["Flo, sag was dazu"].type is discord.AppCommandType.message
    assert befehle["Roasten"].type is discord.AppCommandType.user
    for befehl in befehle.values():
        assert befehl.to_dict(baum)["contexts"] == [0]   # nur Server
    assert len(gestartet) == 2


def test_rechtsklick_gleicht_nur_bei_aenderung_ab():
    """Jeder Abgleich schreibt Discords Befehlsliste neu (mit Tageslimit), und
    Flo startet oft neu. Unveraendert -> kein Abgleich."""
    bot = _rechtsklick_bot()
    syncs = []

    async def sync(**_kw):
        syncs.append(1)
        return []

    (store.DATA_DIR / "befehle.json").unlink(missing_ok=True)
    with mock.patch.object(bot.client.tree, "sync", sync):
        asyncio.run(bot.client._befehle_abgleichen())
        asyncio.run(bot.client._befehle_abgleichen())
    assert syncs == [1]


def test_rechtsklick_sag_was_dazu_antwortet_oeffentlich():
    bot = _rechtsklick_bot()
    gefragt = {}

    async def ask_flo(frage, **kw):
        gefragt["frage"], gefragt["kw"] = frage, kw
        return "Was fuer ein Schwachsinn, Bob."

    nachricht = SimpleNamespace(
        id=9, content="Die Erde ist eine Scheibe", embeds=[], attachments=[],
        reference=None, author=SimpleNamespace(id=2, display_name="Bob"),
        jump_url="https://discord.com/channels/77/4242/9")
    ia = _interaktion()
    schalter, pause = _alles_an(bot)
    with mock.patch.object(bot, "AI_ENABLED", True), schalter, pause, \
            mock.patch.object(bot.ai, "ask_flo", ask_flo):
        asyncio.run(bot.client._rk_sag_was(ia, nachricht))
    assert ia.response.verschoben
    assert "Die Erde ist eine Scheibe" in gefragt["frage"] and "Bob" in gefragt["frage"]
    assert gefragt["kw"]["uid"] == 555 and gefragt["kw"]["gid"] == 77
    (text, _kw), = ia.followup.gesendet
    assert nachricht.jump_url in text and text.endswith("Was fuer ein Schwachsinn, Bob.")


def test_rechtsklick_respektiert_schalter_und_abkuehlzeit():
    """Dieselben Schranken wie im Chat - sonst waere Rechtsklick der Weg vorbei
    an Sendepause und abgeschalteter KI."""
    bot = _rechtsklick_bot()
    nachricht = SimpleNamespace(id=9, content="x", embeds=[], attachments=[],
                                reference=None, author=SimpleNamespace(id=2, display_name="Bob"),
                                jump_url="u")

    ia = _interaktion(gid=None)
    asyncio.run(bot.client._rk_sag_was(ia, nachricht))
    assert ia.response.privat and ia.response.privat[0][1] is True
    assert not ia.followup.gesendet

    with mock.patch.object(bot.features, "is_on_in", lambda _g, _k: False), \
            mock.patch.object(bot.admin, "is_locked", lambda: False), \
            mock.patch.object(bot, "AI_ENABLED", True):
        ia = _interaktion()
        asyncio.run(bot.client._rk_sag_was(ia, nachricht))
        assert "abgeschaltet" in ia.response.privat[0][0]

    async def ask_flo(*_a, **_k):
        return "ok"

    schalter, pause = _alles_an(bot)
    with mock.patch.object(bot, "AI_ENABLED", True), schalter, pause, \
            mock.patch.object(bot.ai, "ask_flo", ask_flo), \
            mock.patch.object(bot.time, "monotonic", lambda: 10.0):
        ia = _interaktion(uid=777)
        asyncio.run(bot.client._rk_sag_was(ia, nachricht))
        assert ia.followup.gesendet, "erster Klick nach dem Hochfahren gesperrt"
        ia = _interaktion(uid=777)
        asyncio.run(bot.client._rk_sag_was(ia, nachricht))
        assert "Chill" in ia.response.privat[0][0]


def test_rechtsklick_roasten():
    bot = _rechtsklick_bot()
    gerostet = []

    async def roast_text(name):
        gerostet.append(name)
        return f"{name}, du Lauch."

    person = SimpleNamespace(id=2, display_name="Bob", mention="<@2>")
    ia = _interaktion()
    schalter, pause = _alles_an(bot)
    with mock.patch.object(bot, "FUN_ENABLED", True), schalter, pause, \
            mock.patch.object(bot.fun, "roast_text", roast_text):
        asyncio.run(bot.client._rk_roasten(ia, person))
    (text, kw), = ia.followup.gesendet
    assert gerostet == ["Bob"] and text == "<@2> Bob, du Lauch."
    assert kw["allowed_mentions"].everyone is False


def test_fun_roast_befehl_und_rechtsklick_teilen_den_text():
    """Der Chat-Befehl 'flo roast @wer' und der Rechtsklick nutzen denselben
    Roast - sonst driftet einer von beiden ins Zahme ab."""
    async def generate(*_a, **_k):
        return "Du bist so nutzlos wie ein Stuhl ohne Beine."

    with mock.patch.object(ai, "generate", generate):
        assert asyncio.run(fun.roast_text("Bob")).startswith("Du bist so nutzlos")


# --- Hilfe-Menue ---------------------------------------------------------------
def _hilfe_teile(view):
    """Alle Bausteine einer V2-Nachricht, flach (Container, Text, Auswahl ...)."""
    return list(view.walk_children())


def test_hilfe_ist_ein_menue_statt_zwoelf_knoepfen():
    """Ein Auswahlmenue mit allen Kategorien - die neuen (Arbeit, Profil,
    Einstellungen) inklusive. Kein Timeout: das Menue ist ein DynamicItem und
    lebt, solange die Nachricht steht."""
    import bot
    view, datei = asyncio.run(bot.client._hilfe_nachricht(None))
    assert isinstance(view, discord.ui.LayoutView) and view.timeout is None
    auswahl = [t for t in _hilfe_teile(view) if isinstance(t, bot.HilfeAuswahl)]
    assert len(auswahl) == 1
    werte = [o.value for o in auswahl[0].item.options]
    assert werte[0] == "_uebersicht"
    assert werte[1:] == [k for k, _e, _l in bot.client._help_categories()]
    assert {"arbeit", "profil", "einstellungen"} <= set(werte)
    assert auswahl[0].item.custom_id == "flo:hilfe:auswahl"
    # Die Karte haengt als Datei dran und steht im Container.
    assert datei is not None and datei.filename == "help_uebersicht.png"
    galerie = [t for t in _hilfe_teile(view) if isinstance(t, discord.ui.MediaGallery)]
    assert galerie and galerie[0].items[0].media.url == "attachment://help_uebersicht.png"


def test_hilfe_kategorie_zeigt_befehle_zum_kopieren_mit_servernamen():
    """Auf der Karte steht immer 'flo' - kopieren soll man aber, was auf DIESEM
    Server klappt."""
    import bot
    with mock.patch.object(bot.ai, "bot_name", lambda *_a: "Bob"):
        view, _ = asyncio.run(bot.client._hilfe_nachricht("musik"))
    texte = " ".join(t.content for t in _hilfe_teile(view)
                     if isinstance(t, discord.ui.TextDisplay))
    assert "`bob spiel <song/link>`" in texte and "`bob history`" in texte
    # Die gewaehlte Kategorie ist im Menue vorausgewaehlt (Musik ist im Test
    # aus - kein ffmpeg -, also am Casino pruefen).
    view, _ = asyncio.run(bot.client._hilfe_nachricht("casino"))
    auswahl = next(t for t in _hilfe_teile(view) if isinstance(t, bot.HilfeAuswahl))
    assert [o.value for o in auswahl.item.options if o.default] == ["casino"]


def test_hilfe_passt_in_discords_grenzen():
    """V2-Nachrichten: hoechstens 4000 Zeichen Text, 40 Bausteine; Menue
    hoechstens 25 Eintraege mit Beschreibungen bis 100 Zeichen."""
    import bot
    for key in [None, *bot._HELP_DATA]:
        view, _ = asyncio.run(bot.client._hilfe_nachricht(key))
        teile = _hilfe_teile(view)
        text = sum(len(t.content) for t in teile if isinstance(t, discord.ui.TextDisplay))
        assert text <= 4000 and len(teile) <= 40, key
        auswahl = next(t for t in teile if isinstance(t, bot.HilfeAuswahl))
        assert len(auswahl.item.options) <= 25
        for o in auswahl.item.options:
            assert len(o.label) <= 100 and len(o.description or "") <= 100


class _HilfeAntwort:
    def __init__(self):
        self.gesendet, self.bearbeitet, self._fertig = [], [], False

    def is_done(self):
        return self._fertig

    async def send_message(self, **kw):
        self.gesendet.append(kw)
        self._fertig = True

    async def edit_message(self, **kw):
        self.bearbeitet.append(kw)
        self._fertig = True

    async def defer(self, **_kw):
        self._fertig = True


def _hilfe_klick(privat):
    return SimpleNamespace(guild_id=77, response=_HilfeAntwort(),
                           message=SimpleNamespace(flags=SimpleNamespace(ephemeral=privat)))


def test_hilfe_auswahl_zeigt_die_seite_nur_dem_klicker():
    """Vorher blaetterte ein Klick die OEFFENTLICHE Nachricht fuer alle um.
    Jetzt: aus der oeffentlichen Nachricht -> private Seite; in der privaten
    Seite -> dort weiterblaettern (kein Stapel privater Nachrichten)."""
    import bot
    asyncio.run(bot.client._hilfe_vorrendern())
    klick = _hilfe_klick(privat=False)
    asyncio.run(bot.client._hilfe_zeigen(klick, "casino"))
    (kw,), = [klick.response.gesendet]
    assert kw["ephemeral"] is True and isinstance(kw["view"], bot.HelpView)
    assert [f.filename for f in kw["files"]] == ["help_casino.png"]

    klick = _hilfe_klick(privat=True)
    asyncio.run(bot.client._hilfe_zeigen(klick, "_uebersicht"))
    assert not klick.response.gesendet and len(klick.response.bearbeitet) == 1


def test_hilfe_auswahl_ueberlebt_den_neustart():
    """Das Menue meldet sich in setup_hook als DynamicItem an; nach einem
    Neustart baut discord.py es aus der custom_id neu und ruft den Rueckruf."""
    import inspect
    import bot
    assert "add_dynamic_items(HilfeAuswahl)" in inspect.getsource(bot.FloBot.setup_hook)
    gezeigt = []

    async def zeigen(_ia, key):
        gezeigt.append(key)

    neu = asyncio.run(bot.HilfeAuswahl.from_custom_id(None, None, None))
    neu.item._values = ["arbeit"]
    neu.item._refresh_state = lambda *_a: None
    with mock.patch.object(bot.client, "_hilfe_zeigen", zeigen):
        asyncio.run(neu.callback(SimpleNamespace()))
    assert gezeigt == ["arbeit"]


def test_hilfe_im_chat_antwortet_mit_dem_menue():
    import bot
    kanal = _RauchKanal()
    msg = _rauch_nachricht("flo hilfe", kanal=kanal)
    gesendet = []

    async def antworte(message, content=None, **kw):
        gesendet.append((content, kw))

    with mock.patch.object(bot.basis, "antworte", antworte):
        asyncio.run(bot.client._hilfe_senden(msg, "voice"))
    (inhalt, kw), = gesendet
    assert inhalt is None and isinstance(kw["view"], bot.HelpView)
    assert kw["file"].filename == "help_voice.png"


# --- Einladungslink ------------------------------------------------------------
def test_einladelink_hat_alle_noetigen_rechte():
    """Der alte Link kannte weder Voice noch Reaktionen, Bilder, Rollen,
    Soundboard oder Umfragen - und keine Rechtsklick-Befehle."""
    import bot
    link = bot.invite_url()
    assert "applications.commands" in link and "scope=bot" in link
    rechte = bot.EINLADE_RECHTE
    for recht in ("send_messages", "embed_links", "attach_files", "add_reactions",
                  "read_message_history", "manage_messages", "manage_roles",
                  "manage_guild", "moderate_members", "connect", "speak",
                  "use_soundboard", "send_polls", "set_voice_channel_status"):
        assert getattr(rechte, recht), recht
    assert f"permissions={rechte.value}" in link
    assert not rechte.administrator


if __name__ == "__main__":
    run(globals())
