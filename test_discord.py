"""Die neuen Discord-Funktionen: Umfragen, Rechtsklick-Befehle, Einladungslink.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur discord      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import _FakeStore, _RauchKanal, _rauch_nachricht, _with_economy  # noqa: F401

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


def test_botsicht_liest_den_text_neuer_nachrichten():
    """Components-V2-Nachrichten (Hilfe, Musik-Panel) haben kein content - die
    BotSicht im Web-Panel zeigte sie als leere Blasen."""
    from discord.components import _component_factory
    import basis
    import bot
    import webpanel
    view, _ = asyncio.run(bot.client._hilfe_nachricht("casino"))
    msg = _rauch_nachricht("")
    msg.components = [_component_factory(d) for d in view.to_components()]
    msg.embeds, msg.reactions, msg.pinned = [], [], False
    text = basis.v2_text(msg)
    assert "## Casino" in text and "blackjack" in text
    assert "## Casino" in webpanel.instance._sicht_msg(msg)["text"]
    assert basis.v2_text(_rauch_nachricht("hallo")) == ""


# --- Soundboard -----------------------------------------------------------------
def _snd(sid, name, emoji="📢"):
    return SimpleNamespace(id=sid, name=name, emoji=discord.PartialEmoji(name=emoji),
                           available=True)


def test_soundboard_hat_platz_fuer_alles_und_ueberlebt_neustarts():
    """Vorher: hoechstens 25 Knoepfe (schon voll), nur eigene Dateien, nach zehn
    Minuten tot. Jetzt: 15 Knoepfe, Menues fuer weitere Dateien, die Sounds des
    Servers und die von Discord - alles DynamicItems ohne Timeout."""
    import voicegags
    dateien = [f"datei{i}" for i in range(20)]
    server = [_snd(100 + i, f"server{i}") for i in range(3)]
    standard = [_snd(i, f"discord{i}") for i in range(1, 31)]
    view = voicegags.SoundboardView(dateien, server, standard)
    assert view.timeout is None
    teile = list(view.walk_children())
    knoepfe = [t for t in teile if isinstance(t, voicegags.SoundKnopf)]
    menues = {t.art: t for t in teile if isinstance(t, voicegags.SoundAuswahl)}
    assert [k.sound_name for k in knoepfe] == dateien[:15]
    assert [o.value for o in menues["datei"].item.options] == dateien[15:]
    assert [o.value for o in menues["server"].item.options] == ["100", "101", "102"]
    assert len(menues["discord"].item.options) == 25
    assert len(teile) <= 40
    # Ohne Discord-Sounds (kein Recht): nur die Dateien.
    nur = voicegags.SoundboardView(["a"])
    assert not [t for t in nur.walk_children() if isinstance(t, voicegags.SoundAuswahl)]
    knopf = asyncio.run(voicegags.SoundKnopf.from_custom_id(
        None, discord.ui.Button(custom_id="flo:sb:d:pups", emoji="💥"), {"name": "pups"}))
    assert knopf.sound_name == "pups"


def test_alle_dauerhaften_knoepfe_ueberschneiden_sich_nie():
    """Jede custom_id passt auf GENAU eine Vorlage - sonst feuern zwei Klassen,
    und die zweite stirbt an der schon beantworteten Interaktion. Und jedes
    DynamicItem in einem Panel steht in DYNAMISCHE_KNOEPFE (sonst waere genau
    dieser Knopf nach einem Neustart tot)."""
    import bot
    import music
    import voicegags
    alle = [bot.HilfeAuswahl]
    for modul in (bot.lotto, bot.floaktie, bot.merchant, music, voicegags):
        alle.extend(getattr(modul, "DYNAMISCHE_KNOEPFE", ()))
    assert len(set(alle)) == len(alle)
    player = music.GuildPlayer(loop=None)
    player.current = music.Track(title="A", stream_url="x", duration=10)
    views = {
        music: [music.MusikPanel(player), music._klassisches_panel(player)],
        voicegags: [voicegags.SoundboardView([f"s{i}" for i in range(20)],
                                             [_snd(5, "srv")], [_snd(1, "std")])],
    }
    hilfe, _ = asyncio.run(bot.client._hilfe_nachricht(None))
    views[bot] = [hilfe]
    for modul, liste in views.items():
        eigene = getattr(modul, "DYNAMISCHE_KNOEPFE", (bot.HilfeAuswahl,))
        for view in liste:
            for teil in view.walk_children():
                if not isinstance(teil, discord.ui.DynamicItem):
                    continue
                assert type(teil) in eigene, (modul.__name__, type(teil).__name__)
                cid = teil.custom_id
                assert len(cid) <= 100, cid
                passend = [k.__name__ for k in alle
                           if k.__discord_ui_compiled_template__.fullmatch(cid)]
                assert passend == [type(teil).__name__], (cid, passend)


class _SoundKanal:
    def __init__(self, cid=42, rechte=True):
        self.id, self.name = cid, f"voice{cid}"
        self.gesendet, self.verbunden = [], []
        self._rechte = rechte

    def permissions_for(self, _wer):
        return SimpleNamespace(use_soundboard=self._rechte, speak=True)

    async def send_sound(self, sound):
        self.gesendet.append(sound.name)

    async def connect(self, **kw):
        self.verbunden.append(kw)
        return _SoundVoice(self)


class _SoundVoice:
    def __init__(self, kanal):
        self.channel, self.getrennt = kanal, False

    def is_connected(self):
        return not self.getrennt

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    async def disconnect(self, force=False):
        self.getrennt = True

    async def move_to(self, kanal):
        self.channel = kanal


def _sound_guild(voice=None, taub=False):
    zustand = []

    async def change_voice_state(**kw):
        zustand.append(kw)

    ich = SimpleNamespace(id=1, guild_permissions=SimpleNamespace(use_soundboard=True, speak=True),
                          voice=SimpleNamespace(self_deaf=taub, self_mute=False) if voice else None)
    return SimpleNamespace(id=77, me=ich, voice_client=voice,
                           change_voice_state=change_voice_state, zustand=zustand)


def test_discord_sound_kommt_ueber_die_musik_und_flo_ist_dafuer_nicht_taub():
    """Soundboard-Sounds laufen UEBER die Musik (Discord mischt selbst). Dafuer
    darf Flo nicht taub im Kanal sitzen - die Musik verbindet sich aber taub.
    Also: nicht verbunden -> ohne Taubheit rein; taub verbunden -> umschalten."""
    import voicegags
    from unittest import mock
    vg = voicegags.VoiceGags()
    sound = _snd(3, "airhorn")
    with mock.patch.object(vg, "_spawn", lambda coro: coro.close()):
        # 1. Flo ist nirgends: verbindet OHNE self_deaf, spielt den Sound.
        kanal = _SoundKanal()
        guild = _sound_guild()
        ok, _ = asyncio.run(vg._discord_sound_spielen(guild, kanal, sound))
        assert ok and kanal.verbunden == [{"self_deaf": False}] and kanal.gesendet == ["airhorn"]

        # 2. Flo sitzt (taub) mit Musik im selben Kanal: nicht neu verbinden,
        #    nur die Taubheit aus - dann der Sound ueber die Musik.
        kanal = _SoundKanal()
        guild = _sound_guild(voice=_SoundVoice(kanal), taub=True)
        with mock.patch.object(vg, "_voice_beschaeftigt", lambda _g: True):
            ok, _ = asyncio.run(vg._discord_sound_spielen(guild, kanal, sound))
        assert ok and not kanal.verbunden and kanal.gesendet == ["airhorn"]
        assert guild.zustand and guild.zustand[0]["self_deaf"] is False

        # 3. Musik laeuft in einem ANDEREN Kanal: nicht rueberziehen.
        dort = _SoundKanal(cid=50)
        guild = _sound_guild(voice=_SoundVoice(dort))
        hier = _SoundKanal(cid=42)
        with mock.patch.object(vg, "_voice_beschaeftigt", lambda _g: True):
            ok, text = asyncio.run(vg._discord_sound_spielen(guild, hier, sound))
        assert not ok and "voice50" in text and not hier.gesendet

        # 4. Kein Recht: klare Ansage, kein Versuch.
        kanal = _SoundKanal(rechte=False)
        ok, text = asyncio.run(vg._discord_sound_spielen(_sound_guild(), kanal, sound))
        assert not ok and "Soundboard verwenden" in text and not kanal.verbunden


def test_sound_befehl_findet_auch_discord_sounds():
    """'flo sound airhorn' ohne eigene Datei: dann eben Discords eigener."""
    import voicegags
    from unittest import mock
    vg = voicegags.VoiceGags()
    gespielt = []

    async def discord_sounds(_guild):
        return [_snd(9, "Kuhglocke")], [_snd(1, "airhorn")]

    async def spielen(_g, _k, snd):
        gespielt.append(snd.name)
        return True, ""

    msg = _rauch_nachricht("sound airhorn")
    msg.author.voice = SimpleNamespace(channel=_SoundKanal())
    with mock.patch.object(vg, "_discord_sounds", discord_sounds), \
            mock.patch.object(vg, "_discord_sound_spielen", spielen), \
            mock.patch.object(vg, "_find_sound", lambda _n: None):
        assert asyncio.run(vg._cmd_sound(msg, "airhorn")) == "🔊 **airhorn**"
        assert asyncio.run(vg._cmd_sound(msg, "kuhglocke")) == "🔊 **Kuhglocke**"
        assert "kenne ich nicht" in asyncio.run(vg._cmd_sound(msg, "gibtsnicht"))
    assert gespielt == ["airhorn", "Kuhglocke"]


# --- Tempo: erst reagieren, dann speichern ------------------------------------------
def test_zaehlspiel_reagiert_ohne_auf_die_platte_zu_warten():
    """Jede Zahl im Zaehlkanal wartete auf einen kompletten Schreibvorgang des
    Stores, bevor das ✅ kam. Jetzt: Haken sofort, gespeichert wird gesammelt."""
    import games
    import guildcfg
    from unittest import mock
    g = games.instance
    alt = g._store
    g._store = _FakeStore({"counting": {}})
    reaktionen = []

    async def reagieren(e):
        reaktionen.append((e, g._store.gespeichert))

    msg = _rauch_nachricht("1")
    msg.add_reaction = reagieren
    msg.channel.id = 5150
    try:
        with mock.patch.object(guildcfg, "get", lambda _g, k: 5150 if k == "zaehl_channel" else 0), \
                mock.patch.object(games.economy, "is_enabled", lambda: False):
            assert asyncio.run(g._check_counting(msg))
            msg.content, msg.author = "5", SimpleNamespace(id=9, display_name="B")
            asyncio.run(g._check_counting(msg))
    finally:
        daten, g._store = g._store, alt
    assert reaktionen == [("✅", 0), ("❌", 0)], reaktionen
    assert daten.gespeichert == 0 and daten.angemeldet == 2


def test_aktie_takt_und_impulse_speichern_gesammelt():
    """Livestream-/Call-Impulse schrieben jedes Mal den ganzen Aktien-Store
    sofort. Kaeufe bleiben beim sofortigen Speichern (Geld + Anteile)."""
    import floaktie
    from unittest import mock
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._store = _FakeStore({"price": 1000, "holdings": {}, "history": [], "ticks": []})
    fa._enabled = True

    async def nix(*_a, **_k):
        return None
    try:
        with mock.patch.object(fa, "is_off", lambda *a: False), \
                mock.patch.object(fa, "_puls", lambda *a: True), \
                mock.patch.object(fa, "_refresh_live", nix):
            asyncio.run(fa.note_stream_start())
            asyncio.run(fa.note_voice_join())
        assert fa._store.gespeichert == 0 and fa._store.angemeldet == 2
    finally:
        fa._store, fa._enabled = alt


def test_level_karte_merkt_sich_das_profilbild():
    """Jede 'flo level'-Abfrage lud das Profilbild neu von Discord (bis 6 s).
    Die Adresse enthaelt den Bild-Hash - ein neues Bild ist eine neue Adresse."""
    import economy
    geladen = []

    def person(url):
        async def lesen():
            geladen.append(url)
            return b"PNG" + url.encode()
        asset = SimpleNamespace(url=url, read=lesen)
        return SimpleNamespace(display_avatar=SimpleNamespace(with_size=lambda _n: asset))

    eco = economy.Economy()
    assert asyncio.run(eco._karten_avatar(person("http://cdn/a.png"))) == b"PNGhttp://cdn/a.png"
    asyncio.run(eco._karten_avatar(person("http://cdn/a.png")))
    asyncio.run(eco._karten_avatar(person("http://cdn/b.png")))   # neues Bild
    assert geladen == ["http://cdn/a.png", "http://cdn/b.png"]


def test_bestenliste_loest_namen_parallel_auf():
    import economy
    import time as zeit
    eco = economy.Economy()

    async def langsam(uid, guild):
        await asyncio.sleep(0.05)
        return f"Name{uid}"

    eco.resolve_display_name = langsam
    zeilen = [{"id": i, "name": ""} for i in range(1, 9)] + [{"id": 99, "name": "Anna"}]
    start = zeit.monotonic()
    asyncio.run(eco._resolve_names(zeilen, None))
    dauer = zeit.monotonic() - start
    assert [z["name"] for z in zeilen][:2] == ["Name1", "Name2"] and zeilen[-1]["name"] == "Anna"
    assert dauer < 0.3, f"nacheinander statt parallel ({dauer:.2f} s)"


def test_bild_kodieren_blockiert_den_bot_nicht():
    """Das PNG-Kodieren eines generierten Bildes lief auf dem Event-Loop."""
    import inspect
    import io as _io
    import media
    from PIL import Image
    quelle = inspect.getsource(media.Media.generate_image)
    assert "to_thread(self._als_png" in quelle
    roh = _io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(roh, format="JPEG")
    assert media.Media._als_png(roh.getvalue()).startswith(b"\x89PNG")
    assert "basis.antworte" in inspect.getsource(media.Media._cmd_generate)


# --- Aus der Pruefung der Wirtschaft -----------------------------------------------
def test_casino_formular_bestaetigt_vor_dem_spielen():
    """'Einsatz aendern' spielte und renderte erst und antwortete dann. Dauerte
    das GIF > 3 s: 'Etwas ist schiefgelaufen', die Runde war aber verbucht,
    und wer nochmal abschickte, zahlte doppelt."""
    import casino
    from unittest import mock
    ablauf = []

    class Antwort:
        async def defer(self, **kw):
            ablauf.append(("defer", kw.get("thinking")))

    async def followup_send(**kw):
        ablauf.append(("ergebnis", kw.get("wait")))
        return SimpleNamespace(id=5, channel=SimpleNamespace(id=1))

    async def crash(uid, bet, ziel):
        ablauf.append(("spielen", bet))
        return SimpleNamespace(copy=lambda: None), None

    ia = SimpleNamespace(response=Antwort(), followup=SimpleNamespace(send=followup_send),
                         guild_id=1, channel_id=1)
    formular = casino._BetModal("crash", 7, title="x")
    formular.bet._value, formular.extra._value = "100", "2.0"
    restore = _with_economy({7: 10_000})
    try:
        with mock.patch.object(casino, "_play_crash", crash), \
                mock.patch.object(casino, "_protect", lambda _m: None):
            asyncio.run(formular.on_submit(ia))
    finally:
        restore()
    assert [a for a, _ in ablauf] == ["defer", "spielen", "ergebnis"], ablauf

    # Scheitert schon das Bestaetigen: keine Runde, Geld zurueck.
    class Kaputt:
        async def defer(self, **_kw):
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "weg")

    ia.response = Kaputt()
    ablauf.clear()
    restore = _with_economy({7: 10_000})
    try:
        with mock.patch.object(casino, "_play_crash", crash):
            asyncio.run(formular.on_submit(ia))
        assert economy.get_coins(7) == 10_000 and not ablauf
    finally:
        restore()


def test_titel_ablegen_und_aktie_ohne_menge():
    import economy as eco_mod
    import floaktie
    restore = _with_economy({7: 1_000})
    try:
        antwort = asyncio.run(eco_mod.handle(_rauch_nachricht("trage ab", uid=7)))
        assert antwort is not None, "'trage ab' legt den Titel ab"
        assert asyncio.run(eco_mod.handle(_rauch_nachricht("setze dich hin", uid=7))) is None
    finally:
        restore()
    fa = floaktie.instance
    alt = (fa._store, fa._enabled)
    fa._store = _FakeStore({"price": 1000, "holdings": {"7": 5}, "history": [],
                            "ticks": [], "day": fa._today()})
    fa._enabled = True
    restore = _with_economy({7: 1_000})
    try:
        for satz in ("verkaufe meine aktien", "verkauf die aktien"):
            antwort = asyncio.run(fa.handle(SimpleNamespace(
                content=satz, guild=SimpleNamespace(id=1),
                author=SimpleNamespace(id=7, display_name="T"))))
            assert fa.shares_of(7) == 5, (satz, antwort)
            assert "verkauf 5" in str(antwort), antwort
    finally:
        fa._store, fa._enabled = alt
        restore()


def test_rollen_sync_laeuft_je_person_nacheinander():
    """Zwei schnelle Titelkaeufe: der zweite Sync rechnete mit der alten
    Rollenliste - am Ende hatte man zwei Titel-Rollen."""
    import economy as eco_mod
    eco = eco_mod.Economy()
    eco._hintergrund = set()
    laeuft, ueberlappt, geholt = [], [], []

    async def sync(ziel):
        if laeuft:
            ueberlappt.append(1)
        laeuft.append(1)
        await asyncio.sleep(0.01)
        laeuft.pop()

    async def fetch_member(uid):
        geholt.append(uid)
        return SimpleNamespace(id=uid)

    eco._sync_role = sync
    person = SimpleNamespace(id=7, guild=SimpleNamespace(fetch_member=fetch_member))

    async def lauf():
        a = eco.rolle_spaeter(person)
        b = eco.rolle_spaeter(person)
        await asyncio.gather(a, b)
    asyncio.run(lauf())
    assert not ueberlappt, "zwei Syncs gleichzeitig"
    assert geholt == [7], "der zweite Sync muss den frischen Stand holen"


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
