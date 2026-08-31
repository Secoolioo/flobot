"""Moderation: Warnen, Timeout, Loeschen, Bannen.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur moderation      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore)



def test_loeschen_raeumt_nicht_versehentlich_alles():
    """'alle', 'ganz' und 'komplett' sind deutsche FUELLWOERTER - sie duerfen
    nicht mitten im Satz einen kompletten Channel-Wipe ausloesen.

    Gemessen loeschten vorher alle diese Saetze den ganzen Verlauf, sofort und
    ohne Rueckfrage:
      'loesch das ganz schnell'
      'loesch mal alle deine Nachrichten'   (gemeint waren Flos Nachrichten)
      'loesch ganz kurz bitte'
    Unwiderruflich, mit der Meldung 'N Nachrichten geloescht'."""
    import re
    import moderation

    S = moderation._ALL_START_RE
    def wipe(rest):
        return bool(S.match(rest)) and re.search(r"\d+", rest) is None

    # Alltagssaetze raeumen NICHT alles weg.
    for harmlos in ("das ganz schnell", "mal alle deine Nachrichten",
                    "ganz kurz bitte", "bitte ganz schnell 10", "20",
                    "die letzten 20", "hier mal komplett durchwischen bitte"):
        assert not wipe(harmlos), harmlos

    # Die ausdrueckliche Ansage schon.
    for klar in ("alle", "alles", "all", "alles hier", "alle nachrichten",
                 "komplett", "everything"):
        assert wipe(klar), klar

    # Und der Wipe fragt einmal nach, weil er nicht rueckholbar ist.
    m = moderation.instance
    alt = (m._enabled, m._store, m._wipe_offen)
    try:
        m._enabled = True
        m._store = _FakeStore({"warns": {}})
        m._wipe_offen = {}
        gerufen = []

        class Chan:
            id = 7
            name = "c"

            async def purge(self, limit=None, check=None, before=None):
                gerufen.append(limit)
                return []

            def permissions_for(self, _m):
                return SimpleNamespace(manage_messages=True, view_channel=True,
                                       read_message_history=True, send_messages=True)

            async def send(self, *a, **k):
                return SimpleNamespace(id=1)

        class Msg:
            def __init__(self, text):
                self.content = text
                self.pinned = False
                self.author = SimpleNamespace(
                    id=1, display_name="Mod", name="Mod", bot=False, roles=[],
                    guild_permissions=SimpleNamespace(manage_messages=True,
                                                      administrator=True))
                self.channel = Chan()
                self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99),
                                             owner_id=1, get_member=lambda u: None)
                self.mentions = []

            async def reply(self, *a, **k):
                return SimpleNamespace(id=2)

        # Erster Versuch: nur Rueckfrage, nichts geloescht.
        antwort = asyncio.run(m._do_purge(Msg("Flo lösch alles"), "lösch alles"))
        assert not gerufen, gerufen
        assert "kompletten Verlauf" in str(antwort), antwort
        # Zweiter Versuch: jetzt wirklich.
        asyncio.run(m._do_purge(Msg("Flo lösch alles"), "lösch alles"))
        assert gerufen == [None], gerufen

        # Eine Zahl geht weiterhin sofort durch - da ist nichts unwiderruflich.
        gerufen.clear()
        m._wipe_offen = {}
        asyncio.run(m._do_purge(Msg("Flo lösch 20"), "lösch 20"))
        assert gerufen == [21], gerufen
    finally:
        m._enabled, m._store, m._wipe_offen = alt




def test_ban_per_id_umgeht_die_rangordnung_nicht():
    """Ein Bann per ROHER ID muss dieselben Pruefungen durchlaufen wie per @-Name.

    Der Bot laeuft ohne members-Intent (bot.py: Intents.none()), guild.get_member()
    liefert deshalb None - und damit war 'isinstance(member, Member)' False und die
    Rangordnung uebersprungen. Gemessen konnte ein Junior-Mod so einen Senior-Mod,
    Flo selbst UND sich selbst bannen, nur indem er die ID statt der Erwaehnung
    tippte."""
    import discord
    import moderation

    JUNIOR, SENIOR, FLO = (111111111111111111, 222222222222222222,
                           999999999999999999)
    OWNER, EXTERN = 555555555555555555, 333333333333333333
    gebannt, geprueft = [], []

    class FakeMember(discord.Member):
        def __init__(self, uid):
            self._uid = uid
        id = property(lambda s: s._uid)

    class Guild:
        id = 1
        owner_id = OWNER

        def __init__(self):
            self.me = SimpleNamespace(id=FLO)

        def get_member(self, _uid):
            return None                      # ohne Intent ist der Cache leer

        async def fetch_member(self, uid):
            if uid == SENIOR:
                return FakeMember(SENIOR)
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), "weg")

        async def ban(self, obj, reason=None, delete_message_seconds=0):
            gebannt.append(getattr(obj, "id", obj))

    class Msg:
        def __init__(self, rest):
            self.content = "Flo ban " + rest
            self.pinned = False
            self.author = SimpleNamespace(
                id=JUNIOR, display_name="Junior", name="Junior", bot=False, roles=[],
                guild_permissions=SimpleNamespace(ban_members=True,
                                                  administrator=False))
            self.guild = Guild()
            self.mentions = []
            self.channel = SimpleNamespace(id=7, name="c")

        async def reply(self, *a, **k):
            return SimpleNamespace(id=2)

    m = moderation.instance
    alt = (m._enabled, m._store, m._bot_can, m._modlog, m._hierarchy_problem)
    try:
        m._enabled = True
        m._store = _FakeStore({"warns": {}})
        m._bot_can = lambda _g, _p: True
        m._modlog = lambda *a, **k: asyncio.sleep(0)

        def rang(_msg, member):
            geprueft.append(getattr(member, "id", None))
            return "Diese Person hat eine gleich hohe oder höhere Rolle als du. ⛔"
        m._hierarchy_problem = rang

        def bann(uid):
            gebannt.clear()
            geprueft.clear()
            return asyncio.run(m._do_ban(Msg(f"{uid} Spam"), f"{uid} Spam"))

        # 1) Mitglied im Server -> Rangordnung greift jetzt auch per ID.
        bann(SENIOR)
        assert geprueft == [SENIOR], geprueft
        assert not gebannt, gebannt

        # 2) Flo, man selbst und der Besitzer sind hart gesperrt.
        for uid in (FLO, JUNIOR, OWNER):
            antwort = bann(uid)
            assert not gebannt, (uid, gebannt)
            assert isinstance(antwort, str), (uid, antwort)

        # 3) Wer wirklich nicht auf dem Server ist, laesst sich weiter per ID
        #    bannen - genau dafuer gibt es den ID-Bann.
        bann(EXTERN)
        assert gebannt == [EXTERN], gebannt
    finally:
        (m._enabled, m._store, m._bot_can, m._modlog,
         m._hierarchy_problem) = alt




def test_purge_rueckfrage_gilt_nur_fuer_den_frager():
    """Die Rueckfrage vor dem Total-Wipe war nur an den KANAL gebunden.

    Mod A fragte an, Mod B schrieb danach im selben Kanal 'loesch alle' - und
    B's ERSTER Befehl loeschte den kompletten Verlauf, ohne dass B die Warnung
    je gesehen hatte."""
    import moderation
    mi = moderation.instance
    alt = dict(mi._wipe_offen)
    mi._wipe_offen = {}
    try:
        kanal = SimpleNamespace(id=777)

        def frage(uid):
            msg = SimpleNamespace(author=SimpleNamespace(id=uid), channel=kanal)
            jetzt = time.time()
            for cid, e in list(mi._wipe_offen.items()):
                if not isinstance(e, tuple) or e[1] <= jetzt:
                    mi._wipe_offen.pop(cid, None)
            offen = mi._wipe_offen.get(kanal.id)
            if not (offen and offen[0] == uid and offen[1] > jetzt):
                mi._wipe_offen[kanal.id] = (uid, jetzt + 60.0)
                return "warnung"
            mi._wipe_offen.pop(kanal.id, None)
            return "wipe"

        assert frage(1) == "warnung"          # A fragt
        assert frage(2) == "warnung", "B durfte ohne eigene Warnung loeschen!"
        assert frage(2) == "wipe"             # B bestaetigt seine EIGENE Warnung
        assert frage(1) == "warnung"          # A faengt wieder von vorn an
    finally:
        mi._wipe_offen = alt




def test_antwort_mit_ping_ist_kein_ziel():
    """In Discord haengt eine Antwort den Autor der beantworteten Nachricht an
    message.mentions - auch wenn niemand ein @ getippt hat (der Client pingt
    beim Antworten standardmaessig). Wer die Liste roh nimmt, trifft den
    Falschen:

        (Antwort auf Bobs Nachricht) 'Flo ban spam'  -> bannt BOB
        (Antwort auf Bobs Nachricht) 'Flo klau'      -> beklaut BOB

    economy._pay hatte das schon geloest (mit Kommentar), zwoelf andere Stellen
    nicht - darunter Moderation, Raub, Schuldbuch und die Admin-Coins."""
    import basis
    bob = SimpleNamespace(id=111, bot=False)
    alice = SimpleNamespace(id=222, bot=False)
    flo = SimpleNamespace(id=999, bot=True)

    # 1. Antwort-mit-Ping, kein getipptes @ -> KEIN Ziel.
    antwort = SimpleNamespace(mentions=[bob], content="Flo ban spam")
    assert basis.echte_erwaehnungen(antwort) == []
    assert basis.erstes_ziel(antwort) is None

    # 2. Wirklich getippt -> Ziel.
    getippt = SimpleNamespace(mentions=[bob], content="Flo ban <@111> spam")
    assert basis.erstes_ziel(getippt) is bob
    # Auch die Nickname-Form <@!id>.
    assert basis.erstes_ziel(
        SimpleNamespace(mentions=[bob], content="ban <@!111>")) is bob

    # 3. Reihenfolge folgt dem TEXT, nicht der (unsortierten) Liste.
    reihe = SimpleNamespace(mentions=[bob, alice],
                            content="pay <@222> 100 statt <@111>")
    assert [u.id for u in basis.echte_erwaehnungen(reihe)] == [222, 111]

    # 4. Bots und ausgeschlossene IDs fliegen raus.
    mitbot = SimpleNamespace(mentions=[flo, bob], content="<@999> ban <@111>")
    assert basis.erstes_ziel(mitbot) is bob
    assert basis.erstes_ziel(mitbot, ohne=(111,)) is None

    # 5. Dieselbe Person doppelt getippt zaehlt einmal.
    doppelt = SimpleNamespace(mentions=[bob], content="<@111> und <@111>")
    assert len(basis.echte_erwaehnungen(doppelt)) == 1

    # 6. Kaputte Nachricht kippt nicht um.
    assert basis.echte_erwaehnungen(SimpleNamespace()) == []
    assert basis.erstes_ziel(SimpleNamespace(mentions=None, content=None)) is None




def test_purge_zaehlt_keine_erwaehnung_als_anzahl():
    """'Flo lösch @spammer' hat MAX_PURGE Nachrichten geloescht - ohne Rueckfrage.

    Die Erwaehnung steht im Text als '<@1453881901738889351>', und die Suche
    nach der Anzahl (re.search(r"\d+", rest)) fand die ID. Ausgerechnet die
    harmlos aussehende Form war die gefaehrlichste: 'loesch alle' fragt einmal
    nach, 'loesch @wer' loeschte sofort. Unwiderruflich.

    Gilt genauso fuer Kanaele <#123>, Rollen <@&123>, Custom-Emojis
    <:name:123> und Zeitmarken <t:123:R> - alles Ziffern."""
    import moderation
    marke = moderation._MARKE_RE

    def anzahl(rest):
        """Genau die zwei Zeilen aus moderation.py, die die Anzahl bestimmen."""
        ohne = marke.sub(" ", rest)
        treffer = re.search(r"\d+", ohne)
        return int(treffer.group()) if treffer else None

    # Was frueher zur Massenloeschung fuehrte, ergibt jetzt KEINE Anzahl.
    for gefaehrlich in ("<@1453881901738889351>",
                        "<@!1453881901738889351>",
                        "<@&987654321012345678>",
                        "<:blob:123456789012345678>",
                        "<a:party:123456789012345678>",
                        "<t:1787228079:R>",
                        "<#1453881901738889351>"):
        assert anzahl(gefaehrlich) is None, (gefaehrlich, anzahl(gefaehrlich))

    # Eine echte Zahl wird weiterhin gelesen - auch NEBEN einer Erwaehnung.
    assert anzahl("20") == 20
    assert anzahl("lösch 20 bitte") == 20
    assert anzahl("<#1453881901738889351> 5") == 5
    assert anzahl("alle") is None

    # Und der Code nutzt wirklich den bereinigten Text - nicht den rohen.
    quelle = inspect.getsource(moderation.Moderation)
    i = quelle.index("rest_ohne_marken = ")
    danach = quelle[i:i + 400]
    assert 're.search(r"\\d+", rest_ohne_marken)' in danach, (
        "die Anzahl wird wieder aus dem rohen Text gelesen")




def test_grosses_bild_reisst_den_bot_nicht_ins_oom():
    """Der Groessendeckel in food.py:142 zaehlt BYTES - und das ist die falsche
    Groesse. Ein PNG mit einer einfarbigen Flaeche ist winzig gepackt und
    riesig entpackt.

    Nachgemessen mit 9450x9450 (89,3 Mio Pixel, 276 kB auf der Platte):
    5,9 Sekunden und +341 MB Spitzenspeicher in EINEM Aufruf. Auf einem
    1-2-GB-Server holt der OOM-Killer dafuer den ganzen Dienst - Musik,
    laufende Casino-Runden, Giveaways. Ausgeloest von jedem, der ein Foto in
    den Kalorien-Kanal postet.

    PILs eigene Bremse hilft nicht: sie WARNT ab 89.478.485 Pixeln und wirft
    erst beim Doppelten - der Fall liegt genau dazwischen."""
    try:
        from PIL import Image
    except ImportError:
        return
    import warnings
    import render

    def bild_bytes(breite, hoehe):
        roh = Image.new("RGB", (breite, hoehe), (7, 7, 7))
        puffer = io.BytesIO()
        roh.save(puffer, "PNG", optimize=True)
        return puffer.getvalue()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")       # DecompressionBombWarning
        # Knapp UNTER PILs eigener Fehlergrenze - genau die Luecke.
        bombe = bild_bytes(9450, 9450)
        assert len(bombe) < 12_000_000, (
            "Die Bombe waere schon am Byte-Deckel in food.py haengengeblieben - "
            "dann prueft dieser Test nicht mehr, was er soll.")
        start = time.time()
        assert render.instance._round_img(bombe, 900, 600, radius=24) is None, (
            "Ein Bild mit 89 Mio Pixeln wird immer noch verarbeitet")
        assert time.time() - start < 2.0, "Die Ablehnung dauert zu lange"

        # Und ein grosses, aber ECHTES Handyfoto muss weiter durchgehen -
        # sonst haetten wir das Feature kaputtgemacht statt es zu schuetzen.
        echt = render.instance._round_img(bild_bytes(4000, 3000), 900, 600, radius=24)
        assert echt is not None, "12-Megapixel-Foto wird faelschlich abgelehnt"
        assert echt.size == (900, 600), echt.size

    # Der Deckel muss ueber der Groesse echter Kameras liegen, aber weit unter
    # dem, was den Speicher sprengt.
    assert 20_000_000 <= render.Render.MAX_PIXEL <= 60_000_000, render.Render.MAX_PIXEL




def test_moderation_grenzen_gelten_je_server():
    """Verwarn-Limit, Timeout-Dauern und das Loesch-Limit standen nur in der
    .env: eine Aenderung galt fuer alle Server und brauchte einen Neustart.
    Jetzt kommen sie aus guildcfg - also auch aus dem Web-Panel."""
    import guildcfg
    import moderation
    for key in ("warn_limit", "warn_timeout", "timeout_standard", "purge_max"):
        assert key in {e.key for e in guildcfg.KATALOG}, f"{key} fehlt im Katalog"

    # WICHTIG: die Module rufen guildcfg.get (den Modul-Alias), nicht
    # instance.get - der Alias wird beim Import gebunden. Wer instance.get
    # ersetzt, ersetzt nichts.
    alt = guildcfg.get
    try:
        guildcfg.get = lambda gid, key: (7 if (gid == 77 and key == "warn_limit")
                                         else alt(gid, key))
        assert moderation._cfg_zahl(77, "warn_limit", 3) == 7
        assert moderation._cfg_zahl(88, "warn_limit", 3) == 3, "gilt serveruebergreifend"
        # Kaputter Wert -> der alte Standard, kein Absturz mitten in der Moderation.
        guildcfg.get = lambda gid, key: "keine zahl"
        assert moderation._cfg_zahl(77, "warn_limit", 3) == 3
    finally:
        guildcfg.get = alt


if __name__ == "__main__":
    run(globals())
