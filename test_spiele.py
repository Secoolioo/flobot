"""Mini-Spiele, Wortzaehler und Chaos.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur spiele      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _fake_person)



# --- Wort-Zaehler ------------------------------------------------------------------
def test_words_tokenizer():
    toks = words._tokenize(
        "Hallo WELT! https://beispiel.de/pfad <@123> <#456> <a:emo:789> "
        "Pizza-Party äöüß 42 a zu")
    assert toks == ["hallo", "welt", "pizza", "party", "äöüß", "zu"]
    assert words._tokenize("") == []
    assert words._tokenize("1234 !!! ...") == []




def test_words_zaehlen():
    """Gezaehlt wird JE SERVER - frueher lief alles in einen gemeinsamen Topf,
    und auf dem zweiten Server stand die Statistik des ersten mit drin."""
    # Fake-Store: reine dict-Logik testen, ohne Datei (Zustand lebt in der Instanz).
    # Das Modul wird hier AUSDRUECKLICH angeschaltet: vorher hing der Test daran,
    # dass irgendein alphabetisch frueherer Test words.setup() gerufen hatte. In
    # gemischter Reihenfolge war er deshalb rot.
    alt = (words.instance._store, words.instance._enabled)
    words.instance._store = type("S", (), {"data": {"guilds": {}}})()
    words.instance._enabled = True
    try:
        n = words._count_text("pizza pizza salat", "111", 1)
        assert n == 3
        n = words._count_text("PIZZA!", "222", 1)
        assert n == 1
        buch = words.instance._buch(1)
        assert buch["words"]["pizza"]["n"] == 3
        assert buch["words"]["pizza"]["u"] == {"111": 2, "222": 1}
        assert buch["words"]["salat"]["n"] == 1
        assert buch["total"] == 4 and buch["msgs"] == 2

        # Ein zweiter Server faengt bei null an und faerbt den ersten nicht ein.
        words._count_text("pizza", "111", 2)
        assert words.instance._buch(2)["words"]["pizza"]["n"] == 1
        assert words.instance._buch(1)["words"]["pizza"]["n"] == 3
        # Der Profil-Lookup fragt ausdruecklich nach EINEM Server; ohne Angabe
        # (z. B. aus einer DM) kommt die Summe ueber alle.
        assert words.instance.statistik_von("111", gid=1)[0] == 3
        assert words.instance.statistik_von("111", gid=2)[0] == 1
        assert words.instance.statistik_von("111")[0] == 4
    finally:
        words.instance._store, words.instance._enabled = alt




def test_words_migriert_den_alten_topf():
    """Der alte, gemeinsame Wortschatz wird dem HAUPT-Server zugeordnet -
    etwas anderes waere geraten, denn wem die Woerter gehoerten, steht
    nirgends. Geloescht wird nichts."""
    import os
    alt_env = os.environ.get("GUILD_ID")
    os.environ["GUILD_ID"] = "77"
    words.instance._store = _FakeStore({
        "words": {"pizza": {"n": 5, "u": {"1": 5}}}, "total": 5, "msgs": 2,
        "guilds": {},
    })
    try:
        words.instance._migrieren()
        daten = words.instance._store.data
        assert "words" not in daten and "total" not in daten
        assert daten["guilds"]["77"]["words"]["pizza"]["n"] == 5
        assert daten["guilds"]["77"]["total"] == 5
        # Zweimal migrieren darf nichts doppeln.
        words.instance._migrieren()
        assert daten["guilds"]["77"]["total"] == 5
    finally:
        words.instance._store = None
        if alt_env is None:
            os.environ.pop("GUILD_ID", None)
        else:
            os.environ["GUILD_ID"] = alt_env




def test_fun_dm_roast():
    """Beleidigung im Chat -> Flo schickt (im Test sicher) eine DM-Retoure; harmlose
    Nachrichten NICHT; Cooldown pro Person greift; Bots werden nicht angeschrieben."""
    import fun
    f = fun.instance
    alt = (f._enabled, f._last_dmroast, dict(f._dm_cooldowns))
    alt_mod = (fun.DMROAST_CHANCE, fun.DMROAST_GLOBAL_COOLDOWN, fun.DMROAST_USER_COOLDOWN)
    f._enabled = True
    f._last_dmroast = 0.0
    f._dm_cooldowns = {}
    fun.DMROAST_CHANCE = 1.0            # zum Testen sicher feuern
    fun.DMROAST_GLOBAL_COOLDOWN = 0.0   # serverweiten Cooldown ausschalten
    fun.DMROAST_USER_COOLDOWN = 0.0

    sent = []

    class FakeAuthor:
        def __init__(self, uid, name="Poebler", is_bot=False):
            self.id = uid
            self.bot = is_bot
            self.display_name = name

        async def send(self, text):
            sent.append((self.id, text))

    def mk(author, content):
        return SimpleNamespace(author=author, content=content,
                               guild=SimpleNamespace(id=1))
    try:
        # Erkennung: Beleidigung ja, harmlos nein.
        assert f.looks_offensive("du bist ein arschloch")
        assert f.looks_offensive("halt dein maul")
        assert not f.looks_offensive("schönen tag noch, alles gut")

        # Beleidigung -> DM-Konter (nicht-leerer String an den Autor).
        a = FakeAuthor(1)
        asyncio.run(f.maybe_dm_roast(mk(a, "du hurensohn spinnst wohl")))
        assert len(sent) == 1 and sent[0][0] == 1 and isinstance(sent[0][1], str) and sent[0][1]

        # Harmlose Nachricht -> keine DM.
        asyncio.run(f.maybe_dm_roast(mk(FakeAuthor(9), "wann fangen wir an zu zocken")))
        assert len(sent) == 1

        # Cooldown pro Person: gleicher Poebler nochmal -> nichts (trotz Chance 1.0).
        fun.DMROAST_USER_COOLDOWN = 99999.0
        f._dm_cooldowns = {}
        b = FakeAuthor(2)
        asyncio.run(f.maybe_dm_roast(mk(b, "fick dich du opfer")))
        n1 = len(sent)
        asyncio.run(f.maybe_dm_roast(mk(b, "arschloch nochmal")))
        assert len(sent) == n1        # vom Personen-Cooldown geblockt

        # Bots werden nicht privat angeschrieben.
        f._dm_cooldowns = {}
        asyncio.run(f.maybe_dm_roast(mk(FakeAuthor(3, is_bot=True), "du wichser")))
        assert all(uid != 3 for uid, _ in sent)
    finally:
        f._enabled, f._last_dmroast, f._dm_cooldowns = alt
        fun.DMROAST_CHANCE, fun.DMROAST_GLOBAL_COOLDOWN, fun.DMROAST_USER_COOLDOWN = alt_mod




def test_bilder_text_bleibt_im_rahmen():
    """Drei Bild-Fehler, die jeder Nutzer sieht - alle nachgemessen.

    1. Unterstriche verschwanden aus JEDEM Namen. Die Glyph-Pruefung malte das
       Zeichen auf eine 48x48-Flaeche; der Unterstrich einer 40-px-Schrift landet
       bei y=51..55 und fiel damit ganz heraus - er galt als "kein Glyph".
       "Maximilian_Schneider99" wurde zu "MaximilianSchneider99".
    2. Ein zu langes ERSTES Wort (eine URL) wurde nie hart getrennt: 1.146 px
       breit in einem 500-px-Feld. Dieselbe URL als zweites Wort ging korrekt.
    3. Zitate ab rund 400 Zeichen kippten auf EINE Zeile von 12.338 px."""
    from PIL import Image, ImageDraw
    import render
    r = render.instance

    # 1) Unterstriche und Unterlaengen bleiben erhalten.
    for name in ("Maximilian_Schneider99", "a_b_c", "__test__", "gyp_qj",
                 "Ben-Ove", "user.name", "Käpt'n Blaubär"):
        assert r._clean_text(name) == name, (name, r._clean_text(name))
    # ... Emoji fliegen weiterhin raus (die Schrift hat sie nicht).
    assert r._clean_text("🎮Gamer🎮") == "Gamer"

    # 2) Umbruch: nichts laeuft aus dem Feld, egal an welcher Stelle.
    d = ImageDraw.Draw(Image.new("RGB", (1000, 200)))
    url = "https://example.com/ein/wirklich/sehr/langer/pfad/der/nicht/passt/12345"
    for maxw in (500, 120, 40):
        f = r._font(28)
        for text in (url, "Schau: " + url, "A" * 200, "kurz", "", "   "):
            zeilen = r._wrap(d, text, f, maxw)
            assert zeilen, text
            for z in zeilen:
                # Ein einzelnes Zeichen darf breiter sein als das Feld - mehr nicht.
                assert d.textlength(z, font=f) <= maxw or len(z) <= 1, (text, maxw, z)

    # 3) Das Zitat-Bild bleibt bei jeder Laenge brauchbar.
    for n in (10, 200, 400, 1000, 4000):
        buf = r.quote_card(None, "Wort " * max(1, n // 5), "Anna")
        assert buf is not None and len(buf.getvalue()) > 3000, n

    # 4) Der Glyph-Test wird gemerkt (sonst malt jede Karte dieselben Zeichen neu).
    r._glyph_q.clear()
    r._clean_text("Testtext")
    assert r._glyph_q, "Glyph-Pruefung wird nicht zwischengespeichert"




def test_backfill_laeuft_fuer_jeden_server():
    """bot.py startet den Verlauf-Nachbau JE SERVER gleichzeitig
    (bot.py:2042-2044). _backfill_running war aber ein prozessweites Bool: der
    erste Server gewann, alle anderen stiegen sofort wieder aus. Und weil auch
    scan['done'] global war, bekam Server 2 seinen Verlauf NIE - der Kommentar
    im Code behauptete sogar, es liefe sauber je Guild."""
    import words
    w = words.instance
    alt_zustand = (w._enabled, w._store, w._backfill_running, w._save_store,
                   w._buch, words.BACKFILL)
    w._enabled = True
    words.BACKFILL = True
    w._store = SimpleNamespace(
        data={"scan": {"before": 1, "done": False, "channels": {}}, "guilds": {}},
        save=lambda: None)

    async def save():
        pass

    w._save_store = save
    w._buch = lambda _gid: {"words": {}}
    w._backfill_running = set()
    gelaufen = []

    def guild(gid, kanal_id):
        perms = SimpleNamespace(view_channel=True, read_message_history=True)
        kanal = SimpleNamespace(id=kanal_id, name=f"c{kanal_id}",
                                permissions_for=lambda _m: perms)

        async def history(**_k):
            gelaufen.append(gid)
            return
            yield                                  # macht daraus einen Generator

        kanal.history = history
        return SimpleNamespace(id=gid, me=SimpleNamespace(), text_channels=[kanal])

    async def beide():
        await asyncio.gather(w.backfill(guild(1, 11)), w.backfill(guild(2, 22)))

    try:
        asyncio.run(beide())
        assert sorted(set(gelaufen)) == [1, 2], (
            f"nur Server {sorted(set(gelaufen))} hat seinen Verlauf bekommen")
        assert w._store.data["scan"]["fertig"] == {"1": True, "2": True}
        # Beim naechsten Start ist nichts mehr zu tun.
        gelaufen.clear()
        asyncio.run(beide())
        assert not gelaufen, "Backfill laeuft unnoetig erneut"
    finally:
        (w._enabled, w._store, w._backfill_running, w._save_store,
         w._buch, words.BACKFILL) = alt_zustand




def test_wort_index_hat_einen_deckel():
    """Ohne Deckel konnte eine Person die Datei beliebig aufblasen.

    Jedes Speichern serialisiert den ganzen Index im Event-Loop (json.dumps
    gibt die GIL auch im Thread nicht frei) - 100.000 Woerter kosteten
    gemessen rund 200 ms Stillstand fuer ALLE."""
    import words

    alt = (words.instance._store, words.instance._enabled, words.MAX_WORDS)
    words.instance._enabled = True
    words.MAX_WORDS = 50
    words.instance._store = _FakeStore({"guilds": {}})
    try:
        # Ein haeufiges Wort und viel Einmal-Spam.
        for _ in range(5):
            words.instance._count_text("wichtig", "7", 1)
        for i in range(300):
            words.instance._count_text(f"spamwort{i:04d}", "7", 1)
        index = words.instance._buch(1)["words"]
        assert len(index) <= words.MAX_WORDS, len(index)
        # Gekuerzt wird nach Haeufigkeit: das oft gesagte Wort bleibt.
        assert "wichtig" in index
    finally:
        (words.instance._store, words.instance._enabled, words.MAX_WORDS) = alt




def test_befehle_kapern_kein_alltagsdeutsch():
    """Ganz normale deutsche Saetze duerfen keinen Befehl ausloesen.

    Gemessen waren das 75 % Fehlalarm bei der Beleidigungs-Erkennung (Flo hat
    hoeflichen Leuten daraufhin eine beleidigende DM geschickt), 74 % beim
    Bildgenerator und 63 % beim Soundboard."""
    import fun
    import media
    import voicegags

    # --- Beleidigungen: Alltagswoerter zaehlen nur mit direkter Anrede -----
    harmlos = [
        "Der Hund hatte den Ball im Maul.",
        "Bei dem Erdbeben gab es leider viele Opfer.",
        "Wisch das mal mit dem Lappen weg.",
        "Ich hab mir einen Fidget Spinner gekauft.",
        "Er hat sich als Noob geoutet, haha, alles gut.",
        "Der Penner-Bus faehrt gleich, ich muss los.",
        "Ich hab dir den Lappen doch schon gegeben.",
        "Mein Arsch tut weh vom langen Sitzen.",
        "Das war echt Kacke gestern, so ein Pech.",
    ]
    for satz in harmlos:
        assert not fun.instance.looks_offensive(satz), satz
    beleidigend = [
        "du opfer",
        "halt dein maul",
        "du bist ein lappen ey",
        "arschloch",
        "was ein hurensohn",
        "verpiss dich",
    ]
    for satz in beleidigend:
        assert fun.instance.looks_offensive(satz), satz

    # --- 'rate' ist auch der Imperativ von RATEN --------------------------
    class _Msg:
        def __init__(self, text, mentions=()):
            self.content = text
            self.mentions = list(mentions)
            self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=42))
            self.author = _fake_person(uid=7, name="wer")

    alt_fun = fun.instance._enabled
    fun.instance._enabled = True
    try:
        for satz in ("rate mal wer gewonnen hat",
                     "rate mal wieviel Uhr es ist",
                     "bewerte doch mal unseren neuen Kanal ehrlich"):
            assert asyncio.run(fun.instance.handle(_Msg(satz))) is None, satz
    finally:
        fun.instance._enabled = alt_fun

    # --- Bildgenerator: Redewendungen sind kein Motiv ---------------------
    for satz in ("bild dir nichts ein", "bild dir nur nichts drauf ein",
                 "zeichne dich nicht durch Faulheit aus", "male mir nichts vor"):
        assert media.Media._GEN_RE.match(satz) is None, satz
    for satz in ("bild ein Drache aus Neon", "male einen Drachen aus Neon",
                 "zeichne eine Katze im Weltall", "generiere ein Bild von einem Auto",
                 "img cyberpunk city at night"):
        assert media.Media._GEN_RE.match(satz) is not None, satz

    # --- Soundboard/TTS: 'sounds gut' ist Zustimmung, kein Befehl ---------
    def _sag(text):
        m = SimpleNamespace(content=text, guild=SimpleNamespace(id=1),
                            author=_fake_person(uid=7), mentions=[])
        return asyncio.run(voicegags.instance.handle(m))

    alt_v = voicegags.instance._enabled
    voicegags.instance._enabled = True
    try:
        for satz in ("sounds gut, lass uns das so machen", "sounds good!",
                     "sprich nicht so laut", "say what?",
                     "vorlesen macht mein Kind gerne", "sprich mal mit ihm"):
            assert _sag(satz) is None, satz
    finally:
        voicegags.instance._enabled = alt_v




def test_verweigerte_roasts_bleiben_nicht_still():
    """Wenn das Modell kneift, sah der Server nur einen von fuenf Fertig-Spruechen.

    Im Log stand NICHTS. Genau daran war nicht zu erkennen, dass Flo nicht
    langweilig geworden ist, sondern dass das Modell verweigert - und ohne diese
    Zeile sucht der Betreiber den Fehler ewig an der falschen Stelle."""
    import logging
    import fun
    strom = io.StringIO()
    haken = logging.StreamHandler(strom)
    log = logging.getLogger("dcbot.fun")
    log.addHandler(haken)
    alt = log.level
    log.setLevel(logging.WARNING)
    try:
        assert fun.instance._looks_like_refusal(
            "Sorry, aber ich kann nicht beleidigend werden.") is True
        assert fun.instance._looks_like_refusal("Du bist ein Vollpfosten.") is False
    finally:
        log.removeHandler(haken)
        log.setLevel(alt)
    text = strom.getvalue()
    assert "verweigert" in text, f"die Verweigerung blieb still: {text!r}"
    # 'bash k' filtert nach KIMUSTER - die Zeile muss da durchkommen.
    hier = os.path.dirname(os.path.abspath(__file__))
    muster = open(os.path.join(hier, "k"), encoding="utf-8").read()
    assert "KI-Fehler" in muster and "KI-Fehler" in text, (
        "'bash k' zeigt verweigerte Roasts nicht an")




def test_schnell_event_lohnt_sich_wirklich():
    """Der Preis fuers Schnell-Event war 100 Coins.

    Danebengehalten: der Tagesbonus bringt 2.500, das Wort des Tages ab 50.000.
    Fuer 100 Coins hat sich niemand mehr vom Stuhl bewegt. Jetzt wird der Preis
    je Runde gewuerfelt - eine Spanne ist spannender als eine feste Zahl."""
    import games
    assert games.EVENT_REWARD_MIN >= 1000, (
        "der Preis ist wieder auf Trinkgeld-Niveau")
    assert games.EVENT_REWARD_MAX > games.EVENT_REWARD_MIN, "keine Spanne"

    werte = {games.instance._event_preis() for _ in range(500)}
    assert min(werte) >= games.EVENT_REWARD_MIN
    assert max(werte) <= games.EVENT_REWARD_MAX
    assert len(werte) > 50, "der Preis wird gar nicht gewuerfelt"

    # Ein Vertipper in der .env (MIN groesser als MAX) darf das Event nicht
    # sprengen - random.randint wirft bei verdrehten Grenzen sonst.
    alt = (games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX)
    try:
        games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX = 9_000, 1_000
        assert games.instance._event_preis() == 1_000
        games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX = 0, 0
        assert games.instance._event_preis() >= 1
    finally:
        games.EVENT_REWARD_MIN, games.EVENT_REWARD_MAX = alt

    # Die Tageskappe bleibt der Deckel - der Preis darf sie nicht aushebeln.
    assert games.GAMES_DAILY_MAX > 0
    assert games.EVENT_REWARD_MAX <= games.GAMES_DAILY_MAX, (
        "ein einzelnes Event fuellt die komplette Tageskappe")


if __name__ == "__main__":
    run(globals())
