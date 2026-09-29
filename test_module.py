"""Kleinere Module: Admin, Terraria, Kalorien, Bilder.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur module      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _fake_msg)



def test_admin_extract():
    # Mention + Betrag
    uid, amount = admin._extract("<@1040135855710404659> 250")
    assert uid == 1040135855710404659 and amount == 250
    # Rohe ID + Betrag (DM-Fall)
    uid, amount = admin._extract("123456789012345678 100")
    assert uid == 123456789012345678 and amount == 100
    # Negativer Betrag
    uid, amount = admin._extract("123456789012345678 -50")
    assert uid == 123456789012345678 and amount == -50
    # Nichts Brauchbares
    assert admin._extract("hallo welt") == (None, None)
    # Betrag ohne Ziel
    uid, amount = admin._extract("500")
    assert uid is None and amount == 500




def test_admin_owner_gate():
    admin.setup()
    # Fremde bekommen von admin.handle grundsaetzlich None (kein Befehl, keine Antwort).
    fremd = asyncio.run(admin.handle(_fake_msg(999, "gib 123456789012345678 100")))
    assert fremd is None
    # Besitzer: unbekanntes Wort -> None (KI/andere Handler sind dran).
    frei = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "wie geht's dir?")))
    assert frei is None
    # Besitzer: Admin-Befehl wird erkannt (economy ist im Test aus -> Hinweis-Text).
    antwort = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID,
                                                 "gib 123456789012345678 100")))
    assert isinstance(antwort, str) and "Economy" in antwort
    # Besitzer: 'gib' als normales Chat-Wort (kein Ziel, kein Betrag) wird NICHT
    # gekapert - die KI soll antworten duerfen.
    chat = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID,
                                              "gib mir mal einen Tipp")))
    assert chat is None
    # Adminhilfe kommt als Embed.
    hilfe = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "adminhilfe")))
    assert hilfe is not None and not isinstance(hilfe, str)




# --- Stocks (Aktienkurse) ------------------------------------------------------
def test_terraria_logic():
    import terraria
    t = terraria.instance
    # Terraria-Fragen werden erkannt, Alltag nicht.
    assert terraria.erkennt_frage("wie besiege ich plantera")
    assert terraria.erkennt_frage("was ist terraria eigentlich")
    assert terraria.erkennt_frage("wie craftet man das zenith")
    assert not terraria.erkennt_frage("wie wird das wetter morgen")
    assert not terraria.erkennt_frage("was gibts heute zu essen")
    assert not terraria.erkennt_frage("mein boss hat frei gegeben")   # kein Fehlalarm
    # _kuerzen haelt das Limit ein.
    lang = "Ein Satz. " * 400
    k = t._kuerzen(lang, 120)
    assert len(k) <= 130
    # _beste_seite versteht beide Such-Formate.
    assert t._beste_seite({"query": {"search": [{"title": "Plantera"}]}}) == "Plantera"
    assert t._beste_seite(["copper", ["Copper Ore", "Copper Bar"], [], []]) == "Copper Ore"
    assert t._beste_seite(None) is None
    assert t._beste_seite({"query": {"search": []}}) is None




def test_terraria_random_und_kategorie():
    """Pagination, Kategorie-Map/Random-Pool und das handle-Routing: 'random' ->
    Zufalls-Seite, ein Kategorie-Wort -> Kategorie, mehrere Woerter -> Frage."""
    import discord
    import terraria
    t = terraria.instance
    # Pagination haelt das Limit ein.
    pages = t._paginate("Absatz.\n\n" * 300, 400)
    assert len(pages) > 1 and all(len(p) <= 420 for p in pages)
    # Kategorie-Map + Zufalls-Pool.
    assert terraria._KATEGORIEN["bosse"] == "Bosses"
    assert terraria._KATEGORIEN["waffen"] == "Weapons"
    assert terraria._random_titel() in terraria._RANDOM_POOL

    calls = {"random": 0, "cat": None}

    async def fake_random():
        calls["random"] += 1
        return discord.Embed(title="Zufall"), None

    async def fake_cat(kat, anzeige):
        calls["cat"] = kat
        return discord.Embed(title=kat), None

    async def fake_send(message, emb, view=None):
        return terraria.HANDLED

    async def fake_beantworte(message, frage):
        calls.setdefault("frage", frage)
        return None

    orig = (t._build_random, t._build_category, t._send, t.beantworte, t._enabled)
    t._build_random, t._build_category, t._send = fake_random, fake_cat, fake_send
    t.beantworte = fake_beantworte
    t._enabled = True

    def msg(content):
        return SimpleNamespace(content=content, guild=SimpleNamespace(id=1),
                               author=SimpleNamespace(display_name="x"))
    try:
        # 'terraria random' -> Zufalls-Seite.
        assert asyncio.run(terraria.handle(msg("terraria random"))) is terraria.HANDLED
        assert calls["random"] == 1
        # Ein Kategorie-Wort -> Kategorie.
        assert asyncio.run(terraria.handle(msg("terraria bosse"))) is terraria.HANDLED
        assert calls["cat"] == "Bosses"
        # Mehrere Woerter mit Kategorie-Wort -> normale Frage (nicht Kategorie).
        calls["cat"] = None
        r = asyncio.run(terraria.handle(msg("terraria waffen gegen plantera")))
        assert calls["cat"] is None and isinstance(r, discord.Embed)  # keine_seite_embed
        assert calls.get("frage") == "waffen gegen plantera"
        # Kein Terraria-Prefix -> None.
        assert asyncio.run(terraria.handle(msg("spiel despacito"))) is None
    finally:
        (t._build_random, t._build_category, t._send, t.beantworte, t._enabled) = orig




# --- Sendepause (nur Owner) ------------------------------------------------------
def test_admin_sendepause_toggle():
    """'sendepause' schaltet um, 'an'/'aus' erzwingen den Zustand; nur der Owner
    erreicht den Befehl ueberhaupt (admin.handle gibt Fremden None)."""
    admin.setup()
    # Ohne Store (Test): Persistenz-Aufruf darf nicht crashen -> Fake-Store.
    class FakeStore:
        def __init__(self):
            self.data = {"sendepause": False}

        async def save(self):
            self.data["sendepause_saved"] = self.data["sendepause"]

    alt = admin.instance._store
    admin.instance._store = FakeStore()
    admin.instance._locked = False
    try:
        assert admin.is_locked() is False
        # Fremder kann die Sendepause NICHT setzen (kein Owner -> None, kein Effekt).
        assert asyncio.run(admin.handle(_fake_msg(999, "sendepause"))) is None
        assert admin.is_locked() is False
        # Owner schaltet an (Toggle) -> Embed, Flag + Persistenz gesetzt.
        antwort = asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause")))
        assert antwort is not None and not isinstance(antwort, str)
        assert admin.is_locked() is True
        assert admin.instance._store.data["sendepause_saved"] is True
        # Toggle zurueck.
        asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause")))
        assert admin.is_locked() is False
        # Explizit 'an' und idempotentes 'aus'.
        asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause an")))
        assert admin.is_locked() is True
        asyncio.run(admin.handle(_fake_msg(admin.OWNER_ID, "sendepause aus")))
        assert admin.is_locked() is False
    finally:
        admin.instance._store = alt
        admin.instance._locked = False




def test_admin_ansage_parsing():
    # Rohe Channel-ID
    cid, text = admin._parse_announce("1453881901738889351 Servus Leute!")
    assert cid == 1453881901738889351 and text == "Servus Leute!"
    # Channel-Erwaehnung <#id> (so kam es in der DM an)
    cid, text = admin._parse_announce("<#1453881901738889351> Servus Leute!")
    assert cid == 1453881901738889351 and text == "Servus Leute!"
    # Mehrzeiliger Text bleibt komplett erhalten
    cid, text = admin._parse_announce("1453881901738889351 Zeile 1\nZeile 2")
    assert cid is not None and text == "Zeile 1\nZeile 2"
    # Ohne Text / ohne ID -> Hinweis-Fall
    assert admin._parse_announce("1453881901738889351") == (None, "")
    assert admin._parse_announce("hallo welt") == (None, "")




def test_admin_dm_parsing():
    # Mention + Text
    uid, text = admin._parse_dm("<@1040135855710404659> hey na, alles fit?")
    assert uid == 1040135855710404659 and text == "hey na, alles fit?"
    # Rohe ID + Text (DM-Fall)
    uid, text = admin._parse_dm("123456789012345678 komm mal Voice")
    assert uid == 123456789012345678 and text == "komm mal Voice"
    # Text VOR der ID geht auch
    uid, text = admin._parse_dm("sag mal 123456789012345678")
    assert uid == 123456789012345678 and text == "sag mal"
    # Ohne Ziel / ohne Text -> Hinweis-Fall
    assert admin._parse_dm("nur text ohne ziel") == (None, "")
    uid, text = admin._parse_dm("<@123456789012345678>")
    assert uid == 123456789012345678 and text == ""




# --- Voice-Gags ------------------------------------------------------------------
def test_voicegags_connect_fehler_bekommt_eine_klare_antwort():
    """channel.connect() kann haengen (asyncio.TimeoutError) - das liess
    _play_path durch. Beim Sound-Befehl stand dann "Da ist gerade etwas
    schiefgelaufen." im Chat, beim Knopf und beim Join-Sound gar nichts.
    Jetzt kommt fuer jeden Verbindungsfehler derselbe Satz wie bei der Musik."""
    import discord
    import music
    import voicegags

    gid = 4715
    assert not music.is_voice_busy(gid)
    guild = SimpleNamespace(id=gid, voice_client=None)
    for fehler in (asyncio.TimeoutError(), RuntimeError("davey library needed"),
                   discord.ClientException("Rechte")):
        async def connect(_f=fehler, **_kw):
            raise _f

        kanal = SimpleNamespace(id=1, name="Voice", connect=connect)
        ok, antwort = asyncio.run(voicegags.instance._play_path(guild, kanal, "x.mp3"))
        assert ok is False and antwort == music.VOICE_KAPUTT, (fehler, antwort)




def test_voicegags_soundboard_ist_nach_der_musik_wieder_frei():
    """Nach dem letzten Song blieb das Soundboard fuer immer gesperrt.

    voicegags fragt music.is_voice_busy - und das war True, solange Flo in
    einem Kanal sein SOLLTE, also auch lange nach dem letzten Song. Der Knopf
    sagte dann bis zum Neustart "Gerade läuft was im Voice"."""
    import music
    import voicegags

    mi = music.instance
    gid = 4716
    alt = mi._players.get(gid)
    player = music.GuildPlayer(loop=None, guild_id=gid)
    player.active_channel_id = 42                    # Flo sitzt noch drin ...
    player.voice = SimpleNamespace(is_connected=lambda: True,
                                   is_playing=lambda: False,
                                   is_paused=lambda: False)
    mi._players[gid] = player
    guild = SimpleNamespace(id=gid, voice_client=player.voice)
    try:
        # ... aber es laeuft nichts mehr: das Soundboard ist frei.
        assert voicegags.instance._voice_beschaeftigt(guild) is False
        # Laeuft ein Song, weicht es weiter aus.
        player.current = music.Track(title="A", stream_url="http://a")
        assert voicegags.instance._voice_beschaeftigt(guild) is True
    finally:
        if alt is None:
            mi._players.pop(gid, None)
        else:
            mi._players[gid] = alt




def test_admin_befehle_treffen_das_erwaehnte_ziel():
    """'Flo gib @wer 500' hat NIE funktioniert - nur der Umweg ueber die rohe ID.

    admin.handle bekommt den Text ueber ai.strip_lead, und das entfernt ALLE
    Erwaehnungen. _extract und _parse_dm suchten die Erwaehnung danach im
    Resttext - dort stand keine mehr. Der Besitzer bekam 'So: Flo gib @wer 100'
    zurueck, obwohl er genau das getippt hatte. Jetzt kommt das Ziel aus den
    getippten Erwaehnungen der Nachricht (ohne Bots, also ohne Flo selbst)."""
    from testhilfe import _embed_text, _with_economy
    alt_an = admin.instance._enabled
    admin.instance._enabled = True
    ZIEL, DRITTE = 222222222222222222, 333333333333333333
    post = []

    async def senden(text):
        post.append(text)

    bob = SimpleNamespace(id=ZIEL, bot=False, display_name="Bob", send=senden)
    alice = SimpleNamespace(id=DRITTE, bot=False, display_name="Alice")
    flo = SimpleNamespace(id=999999999999999999, bot=True, display_name="Flo")

    def chef(text, *mentions):
        return SimpleNamespace(
            author=SimpleNamespace(id=admin.OWNER_ID, bot=False, display_name="Chef"),
            content=text, mentions=list(mentions), guild=None)

    restore = _with_economy({ZIEL: 1000})
    try:
        # Flo per @ angesprochen UND Bob per @ als Ziel - so kommt es aus Discord.
        antwort = asyncio.run(admin.handle(chef(f"<@{flo.id}> gib <@{ZIEL}> 500", flo, bob)))
        assert economy.get_coins(ZIEL) == 1500, _embed_text(antwort)
        asyncio.run(admin.handle(chef(f"Flo nimm <@{ZIEL}> 200", bob)))
        assert economy.get_coins(ZIEL) == 1300
        asyncio.run(admin.handle(chef(f"Flo setcoins <@!{ZIEL}> 42", bob)))
        assert economy.get_coins(ZIEL) == 42
        # Die rohe ID geht weiter (in der DM gibt es keine Erwaehnungen).
        asyncio.run(admin.handle(chef(f"gib {ZIEL} 8")))
        assert economy.get_coins(ZIEL) == 50

        # DM an den Erwaehnten - weitere Erwaehnungen bleiben im Text stehen.
        antwort = asyncio.run(admin.handle(chef(
            f"Flo dm <@{ZIEL}> sag <@{DRITTE}> hallo", bob, alice)))
        assert post == [f"sag <@{DRITTE}> hallo"], (post, antwort)

        # Halbe Befehle mit fremden Woertern sind Chat - die KI ist dran.
        for satz in ("Flo gib mir 5 Tipps für Python",
                     "Flo nimm mir 2 Minuten Zeit",
                     "Flo flüster mir die Lösung",
                     "Flo dm mir das morgen nochmal"):
            assert asyncio.run(admin.handle(chef(satz))) is None, satz
        # Der blanke Befehl bekommt weiter den Hinweis.
        assert "So:" in str(asyncio.run(admin.handle(chef("Flo gib 100"))))
        assert "So:" in str(asyncio.run(admin.handle(chef("Flo dm"))))
    finally:
        restore()
        admin.instance._enabled = alt_an




def test_terraria_erkennt_kein_alltagsdeutsch():
    """'hell' und 'boss' sind deutsche Alltagswoerter, 'golem' ist kein
    eindeutiger Terraria-Begriff. Bei einem Treffer schaltet bot.py den ganzen
    KI-Fallback ab und antwortet stattdessen mit einem Wiki-Embed."""
    import terraria

    for harmlos in ("ist es draußen schon hell, boss?",
                    "was steht heute bei golem?",
                    "der golem im museum sah krass aus",
                    "wo ist der boss? es ist noch hell draußen",
                    "wie wird das wetter morgen in regensburg?"):
        assert not terraria.erkennt_frage(harmlos), harmlos

    for echt in ("wie besiege ich den Wall of Flesh?",
                 "wo finde ich Hellstone?",
                 "wie komme ich in den Hardmode?",
                 "wo spawnt der Moon Lord?",
                 "welches Erz brauche ich für Chlorophyte?"):
        assert terraria.erkennt_frage(echt), echt

    # Und der Blaetterer schiebt keine leere Seite mehr ein (Absatz genau am Limit).
    p = terraria.instance._paginate
    for n in (1, 1798, 1799, 1800, 1801, 3600):
        seiten = p("x" * n)
        assert all(s.strip() for s in seiten), (n, [len(s) for s in seiten])




def test_terraria_kapert_keinen_slang():
    """Owner-Bug: 'flo du npc, du goblin' ergab zwei schwache Treffer und
    damit ein Wiki-Embed statt Flos Konter. Jetzt ist 'npc' raus, und zwei
    schwache Begriffe reichen nur MIT Fragesignal. Eindeutige Begriffe und
    das Wort 'terraria' tragen weiter allein."""
    import terraria
    assert "npc" not in terraria._TERRA_KEYWORDS
    for slang in ("flo du npc, du goblin", "du goblin, du slime",
                  "halt die klappe du slime goblin", "du bist so ein npc",
                  "flo du mana-loser goblin"):
        assert not terraria.erkennt_frage(slang), slang
    for echt in ("wo finde ich slime und goblin?",
                 "wie farm ich goblin und slime",
                 "welche wings droppt der goblin im snow biome",
                 "flo terraria du goblin"):
        assert terraria.erkennt_frage(echt), echt


def test_terraria_auto_antwort_schweigt_nicht_bei_sendefehler():
    """_send meldete HANDLED, auch wenn Discord die Antwort abgelehnt hat -
    bot.py hielt die Frage fuer beantwortet, und der Nutzer bekam NICHTS.
    Die Auto-Antwort (beantworte) gibt dann None, damit die KI antwortet."""
    import discord
    import terraria
    t = terraria.instance

    async def titel(_frage):
        return "Plantera"

    async def seite(_titel, voll=False):
        return {"titel": "Plantera", "extract": "Ein Boss.", "url": ""}

    async def antwort(_frage, _seite):
        return discord.Embed(title="Plantera"), None

    async def kaputt(*_a, **_k):
        raise discord.HTTPException(SimpleNamespace(status=403, reason="x"), "nein")

    async def geht(*_a, **_k):
        return SimpleNamespace(id=1)

    orig = (t._suche_titel, t._seite_laden, t._build_answer)
    t._suche_titel, t._seite_laden, t._build_answer = titel, seite, antwort
    try:
        msg = SimpleNamespace(content="wo spawnt plantera", reply=kaputt,
                              channel=SimpleNamespace(send=kaputt),
                              author=SimpleNamespace(mention="<@1>"))
        assert asyncio.run(terraria.beantworte(msg, "wo spawnt plantera")) is None
        msg = SimpleNamespace(content="wo spawnt plantera", reply=geht,
                              channel=SimpleNamespace(send=geht),
                              author=SimpleNamespace(mention="<@1>"))
        assert asyncio.run(terraria.beantworte(msg, "wo spawnt plantera")) is terraria.HANDLED
    finally:
        t._suche_titel, t._seite_laden, t._build_answer = orig


def test_bildauftrag_braucht_wirklich_einen_auftrag():
    """Ein Bild kostet echtes Geld bei der KI. Nachgemessen loesten SECHS
    voellig normale deutsche Woerter einen Auftrag aus:

        'Flo zeichnen wir mal was?'  -> 'zeichne wir mal was'  -> Bild
        'Flo malen wir mal was?'     -> 'male wir mal was'     -> Bild
        'Flo generierte Bilder ...'  -> traf media._GEN_RE direkt

    Zwei verschiedene Ursachen: cmdnorm korrigierte die Beugungen auf den
    Befehl, und media._GEN_RE nahm mit 'generier\\w*' auch Vergangenheit und
    Substantive ('generierte', 'generierung')."""
    import cmdnorm
    import media
    muster = media.Media._GEN_RE

    def loest_aus(satz):
        return bool(muster.match(cmdnorm.normalize(satz) or satz))

    for wort in ("zeichnen", "zeichnet", "zeichnete", "malen", "malte",
                 "generieren", "generierte", "generierung"):
        satz = f"{wort} wir mal was"
        assert not loest_aus(satz), f"{satz!r} loest einen bezahlten Bildauftrag aus"

    # Die echten Befehle muessen selbstverstaendlich weiter funktionieren.
    for satz in ("zeichne einen drachen", "male einen hund", "generiere ein bild",
                 "generier was", "img katze", "bild von einem auto"):
        assert loest_aus(satz), f"{satz!r} wird nicht mehr erkannt"




def test_food_verschluckt_im_kalorienkanal_nichts():
    """Owner-Bug: im Kalorien-Channel gab 'Flo kalorien' IMMER HANDLED zurueck,
    bevor ueberhaupt nach einem Bild gesucht wurde. 'Flo kalorien von 100 g
    Reis?' und 'Flo kalorien' als Antwort auf ein Essensfoto verschwanden
    spurlos. HANDLED gibt es dort nur noch, wenn die Nachricht selbst ein Bild
    hat (das analysiert der passive Hook); ein Bild in der beantworteten
    Nachricht wird analysiert, und eine Frage ohne Bild bekommt die KI."""
    import food
    import guildcfg
    f = food.instance
    analysiert = []

    async def respond(message, att):
        analysiert.append(att.url)

    orig = (f._enabled, f._respond, guildcfg.get)
    f._enabled, f._respond = True, respond
    guildcfg.get = lambda gid, key: 555 if key == "kalorien_channel" else None
    foto = SimpleNamespace(content_type="image/png", filename="essen.png",
                           url="http://x/essen.png", size=10)

    def msg(text, kanal=555, anhang=(), antwort_auf=None):
        ref = None
        if antwort_auf is not None:
            ref = SimpleNamespace(resolved=antwort_auf, message_id=1)
        return SimpleNamespace(content=text, guild=SimpleNamespace(id=7),
                               channel=SimpleNamespace(id=kanal),
                               attachments=list(anhang), reference=ref)
    try:
        # Frage ohne Bild: KI, in UND ausserhalb des Kalorien-Channels.
        for kanal in (555, 999):
            for satz in ("Flo kalorien von 100 g Reis?", "Flo kcal in einer Banane",
                         "Flo nährwerte von Haferflocken?"):
                assert asyncio.run(food.handle(msg(satz, kanal))) is None, (satz, kanal)
        # Antwort auf ein Essensfoto im Kalorien-Channel: wird analysiert.
        essen = SimpleNamespace(attachments=[foto])
        assert asyncio.run(food.handle(
            msg("Flo kalorien", antwort_auf=essen))) is food.HANDLED
        assert analysiert == ["http://x/essen.png"]
        # Bild an der Nachricht selbst im Kanal: der passive Hook macht das.
        analysiert.clear()
        assert asyncio.run(food.handle(msg("Flo kalorien", anhang=[foto]))) is food.HANDLED
        assert analysiert == [], "doppelt analysiert"
        # ... ausserhalb des Kanals analysiert der Befehl selbst.
        assert asyncio.run(food.handle(
            msg("Flo kalorien", kanal=999, anhang=[foto]))) is food.HANDLED
        assert analysiert == ["http://x/essen.png"]
        # Nur 'Flo kalorien' ohne jedes Bild: der Hinweis, keine Stille.
        assert "Foto" in str(asyncio.run(food.handle(msg("Flo kalorien"))))
    finally:
        f._enabled, f._respond, guildcfg.get = orig


def test_food_liest_deutsche_tausenderpunkte():
    """'ca. 1.200 kcal' wurde zu 1,2 kcal - der Punkt ist im Deutschen der
    Tausender-Trenner, nicht das Dezimalzeichen."""
    import food
    num = food.instance._num
    assert num("ca. 1.200 kcal") == 1200.0
    assert num("1.234.567 kcal") == 1234567.0
    assert num("1,5") == 1.5           # Komma bleibt Dezimalzeichen
    assert num("2.5 g") == 2.5         # Punkt mit 1 Ziffer = Dezimalzeichen
    assert num("8/10") == 8.0
    assert num("abc") == 0.0 and num(None) == 0.0


if __name__ == "__main__":
    run(globals())
