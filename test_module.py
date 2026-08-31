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
