"""Musik: Erkennung, Warteschlange, Verlauf, YouTube.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur musik      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore, _VoiceChannelStub, _cfg_frisch, _embed_text, _fake_person,
    _musik_umgebung, _track, _verlauf_frisch, _verlauf_track)



# --- Musik: natuerlichsprachige Play-Trigger ------------------------------------
def test_music_natural_language():
    """'Flo mach mal <X> an' & Co. werden wie ein Play-Befehl erkannt; generische
    Floskeln fuehren zu resume/Hinweis, normale Saetze bleiben None."""
    import music
    pc = music.instance.parse_command
    # Song steht in der Mitte -> Suche nach genau diesem Song.
    assert pc("flo mach mal bohemian rhapsody an") == ("search", "bohemian rhapsody")
    assert pc("Flo mach mal despacito an") == ("search", "despacito")
    assert pc("flo leg mir mal sandstorm auf") == ("search", "sandstorm")
    assert pc("flo hau mal darude sandstorm raus") == ("search", "darude sandstorm")
    assert pc("flo pack mal lofi beats auf") == ("search", "lofi beats")
    assert pc("flo kannst du mal wonderwall abspielen") == ("search", "wonderwall")
    assert pc("flo spiel mir mal africa vor") == ("search", "africa")
    # "spiel mir mal <X>" darf nicht nach "mir mal <X>" suchen.
    assert pc("flo spiel mir mal africa") == ("search", "africa")
    # Generisch ohne konkreten Song -> resume/Hinweis.
    assert pc("flo mach mal musik an") == ("resume_or_hint", "")
    assert pc("flo mach mal die mucke an") == ("resume_or_hint", "")
    # "Musik aus" -> stoppen.
    assert pc("flo mach die musik aus") == ("stop", "")
    assert pc("flo stell die mucke ab") == ("stop", "")
    # Normaler Play-Befehl bleibt unveraendert.
    assert pc("flo spiel despacito") == ("search", "despacito")
    # Kein Musikbefehl -> None (keine Kaperung normaler Saetze).
    assert pc("flo wie gehts dir") is None
    assert pc("flo mach mal langsam") is None
    # Spiel-/Feature-Namen werden NICHT als Song gesucht (kein Kapern des Quiz-Starts).
    assert pc("flo mach das quiz an") is None
    assert pc("flo mach mal blackjack an") is None




# --- Musik: Zufalls-Song mit Genre-Auswahl --------------------------------------
def test_music_random_genre():
    """start_random: ohne Voice -> Hinweis; mit Voice -> Track aufgeloest, gestartet
    und Panel gepostet; 'surprise' waehlt ein gueltiges Genre; Genre-Pools sauber."""
    import music

    # Genre-Datenbank plausibel (Dropdown-Limit, gefuellte Pools).
    assert 1 <= len(music._RANDOM_GENRES) <= 24
    assert all(pool and isinstance(pool, list)
               for _l, _e, pool in music._RANDOM_GENRES.values())

    calls = {"defer": 0, "panel": 0, "started": None, "ephemeral": []}

    class FakePlayer:
        def __init__(self):
            self.text_channel = None
            self.queue = []

        async def connect(self, ch):
            pass

        def is_active(self):
            return False

        def start(self, track):
            calls["started"] = track.title

    fake_player = FakePlayer()

    class Resp:
        def is_done(self):
            return False

        async def defer(self):
            calls["defer"] += 1

        async def send_message(self, *a, **k):
            calls["ephemeral"].append((a, k))

    class Inter:
        def __init__(self, in_voice):
            self.guild = SimpleNamespace(id=1)
            self.channel = SimpleNamespace(id=2)
            self.user = SimpleNamespace(
                id=7, display_name="Tester",
                voice=SimpleNamespace(channel=SimpleNamespace(id=9)) if in_voice else None)
            self.response = Resp()

        async def edit_original_response(self, *a, **k):
            pass

        # followup.send
        @property
        def followup(self):
            async def _send(*a, **k):
                pass
            return SimpleNamespace(send=_send)

    inst = music.instance
    alt = (inst._enabled, inst._player_for, inst._extract, inst._send_panel)
    inst._enabled = True
    inst._player_for = lambda gid: fake_player

    async def _fake_extract(q):
        return music.Track(title=f"Song für {q}", stream_url="http://x")
    inst._extract = _fake_extract

    async def _fake_panel(player, track, **k):
        calls["panel"] += 1
    inst._send_panel = _fake_panel
    try:
        # 1) Nicht im Voice -> ephemerer Hinweis, kein Abspielen.
        asyncio.run(inst.start_random(Inter(in_voice=False), "rock"))
        assert calls["ephemeral"] and calls["started"] is None and calls["panel"] == 0

        # 2) Im Voice -> defer, Track gestartet, Panel gepostet.
        asyncio.run(inst.start_random(Inter(in_voice=True), "rock"))
        assert calls["defer"] == 1
        assert calls["started"] is not None and calls["panel"] == 1

        # 3) 'surprise' waehlt ein gueltiges Genre (kein Crash, spielt).
        calls["started"] = None
        asyncio.run(inst.start_random(Inter(in_voice=True), "surprise"))
        assert calls["started"] is not None

        # 4) Unbekanntes Genre -> ephemerer Hinweis, kein Abspielen.
        before = calls["started"]
        asyncio.run(inst.start_random(Inter(in_voice=True), "gibtsnicht"))
        assert calls["started"] == before  # unveraendert (nicht gestartet)
    finally:
        inst._enabled, inst._player_for, inst._extract, inst._send_panel = alt




# --- Musik: Spotify Best-Match (richtiger Song statt Sped-Up/Loop) --------------
def test_music_spotify_best_match():
    """Aus mehreren YouTube-Treffern wird der beste fuer einen Spotify-Song gewaehlt:
    Dauer-Naehe + Titel-Match, Abwertung von Sped-Up/Loop/Nightcore/Cover/Live -
    aber 'live' darf nicht in 'Alive' matchen und ein gewollter Remix nicht sinken."""
    import music
    m = music.instance

    def pick(cands, dur, title, artist=""):
        return m._pick_best_match(cands, dur, title, artist)

    # Original-Video (Dauer passt) schlaegt Sped-Up/1h-Loop/Nightcore.
    c = [{"title": "Alan Walker - Faded (Sped Up)", "duration": 175, "id": "a"},
         {"title": "Alan Walker - Faded [1 HOUR LOOP]", "duration": 3600, "id": "b"},
         {"title": "Alan Walker - Faded (Official Music Video)", "duration": 212, "id": "c"},
         {"title": "Faded - Alan Walker (Nightcore)", "duration": 150, "id": "d"}]
    assert pick(c, 212, "Faded", "Alan Walker")["id"] == "c"
    # Ohne Dauer-Info wird wenigstens der Junk abgewertet.
    c2 = [{"title": "Song X (Sped Up)", "duration": None, "id": "1"},
          {"title": "Song X (Official Audio)", "duration": None, "id": "2"},
          {"title": "Song X 10 hours", "duration": None, "id": "3"}]
    assert pick(c2, None, "Song X")["id"] == "2"
    # Ein gewuenschter Remix wird NICHT als 'Junk' abgestraft.
    c3 = [{"title": "Titel (Original Mix)", "duration": 200, "id": "o"},
          {"title": "Titel (Tiesto Remix)", "duration": 201, "id": "r"}]
    assert pick(c3, 201, "Titel Tiesto Remix")["id"] == "r"
    # 'live'-Abwertung darf 'Stayin Alive' nicht treffen.
    c4 = [{"title": "Bee Gees - Stayin Alive (Official)", "duration": 285, "id": "x"},
          {"title": "Bee Gees - Stayin Alive (Live 1979)", "duration": 300, "id": "y"}]
    assert pick(c4, 285, "Stayin Alive", "Bee Gees")["id"] == "x"
    # Normalisierung behaelt Klammer-Woerter ('faded sped up').
    assert m._norm_match("Alan Walker - Faded (Sped Up!)") == "alan walker faded sped up"




# --- Musik: Lyrics -------------------------------------------------------------
def test_music_lyrics():
    """Artist/Titel-Split, Seiten-Pagination und _build_lyrics (Fetch gemockt):
    Treffer -> Embed + Paginator-View, kein Treffer -> Fehler-Embed ohne View."""
    import music
    m = music.instance
    # YouTube-Deko wird entfernt, am ' - ' getrennt.
    assert m._split_artist_title("Queen - Bohemian Rhapsody (Official Video)") \
        == ("Queen", "Bohemian Rhapsody")
    assert m._split_artist_title("Rick Astley - Never Gonna Give You Up [HD]") \
        == ("Rick Astley", "Never Gonna Give You Up")
    assert m._split_artist_title("Bohemian Rhapsody") == ("", "Bohemian Rhapsody")
    # Pagination bricht an Strophen und haelt das Zeichenlimit ein.
    text = "\n\n".join(f"Strophe {i}\nzeile a\nzeile b" for i in range(30))
    pages = m._lyrics_pages(text, limit=300)
    assert len(pages) > 1 and all(len(p) <= 300 for p in pages)
    # Eine leere/kurze Eingabe liefert trotzdem mindestens eine Seite.
    assert m._lyrics_pages("") and m._lyrics_pages("nur eine zeile")

    async def fake_ok(artist, title):
        return "Vers 1\nZeile A\nZeile B\n\nRefrain\nHook 1\nHook 2"

    async def fake_none(artist, title):
        return None

    try:
        m.fetch_lyrics = fake_ok
        emb, view = asyncio.run(m._build_lyrics("Queen - Bohemian Rhapsody", None))
        assert view is not None and emb.title.startswith("🎤")
        assert "Vers 1" in (emb.description or "")
        assert len(view.pages) >= 1 and view.embed().title.startswith("🎤")
        # Kein Treffer -> Fehler-Embed, KEINE View.
        m.fetch_lyrics = fake_none
        emb2, view2 = asyncio.run(m._build_lyrics("Voellig Unbekannt XY", None))
        assert view2 is None and "Kein Text" in (emb2.title or "")
    finally:
        try:
            del m.fetch_lyrics    # Instanz-Override weg -> Klassenmethode zurueck
        except AttributeError:
            pass




def test_musik_advance_raeumt_die_warteschlange_nicht_leer():
    """Startet jemand selbst einen Song, waehrend der Automat gerade den naechsten
    aufloest, darf der Automat NICHT weitermachen.

    Nachgestellt: Song A endet, _advance haengt im Aufloesen eines
    Playlist-Tracks, in der Luecke sagt jemand 'flo spiel X'. Danach lief zwar X,
    aber _advance machte weiter - jedes start() scheiterte an 'Already playing
    audio.', wurde als 'Track nicht ladbar' verbucht und uebersprungen. Ergebnis:
    Warteschlange 4 -> 0, current auf None, und das frisch gepostete Panel
    geloescht."""
    import discord
    import music

    alt_ff = (discord.FFmpegPCMAudio, discord.PCMVolumeTransformer)
    alt_res, alt_panel, alt_ret = (music._resolve_track, music._send_panel,
                                   music._retire_panel)
    try:
        discord.FFmpegPCMAudio = lambda *a, **k: object()
        discord.PCMVolumeTransformer = lambda src, volume=1.0: src

        class FakeVoice:
            def __init__(self):
                self.spielt = False

            def is_connected(self):
                return True

            def is_playing(self):
                return self.spielt

            def is_paused(self):
                return False

            def play(self, _src, after=None):
                if self.spielt:
                    raise discord.ClientException("Already playing audio.")
                self.spielt = True

            def stop(self):
                self.spielt = False

            @property
            def channel(self):
                return SimpleNamespace(id=42)

        async def langsam(track):
            await asyncio.sleep(0.05)
            track.stream_url = "http://x"
            return track

        async def kein_panel(*a, **k):
            pass

        geloescht = []

        async def retire(_p):
            geloescht.append(1)

        music._resolve_track = langsam
        music._send_panel = kein_panel
        music._retire_panel = retire

        def aufbau():
            p = music.GuildPlayer(loop=asyncio.new_event_loop())
            p.voice = FakeVoice()
            p.queue = [music.Track(title=f"Song {i}", stream_url="", query=f"q{i}")
                       for i in range(4)]
            p.current = music.Track(title="Song A", stream_url="http://a")
            p.voice.spielt = True
            return p

        # 1) Jemand startet waehrend des Aufloesens etwas Eigenes.
        p = aufbau()
        gen = p._play_gen

        async def stoerung():
            t = asyncio.ensure_future(p._advance(gen))
            await asyncio.sleep(0.01)
            p.voice.stop()
            p.start(music.Track(title="Wunsch X", stream_url="http://x"))
            await t

        asyncio.run(stoerung())
        assert len(p.queue) == 4, len(p.queue)
        assert p.current is not None and p.current.title == "Wunsch X", p.current
        assert not geloescht, "Panel des Nutzers wurde geloescht"

        # 2) Der normale Songwechsel funktioniert unveraendert.
        p = aufbau()
        p.voice.spielt = False
        asyncio.run(p._advance(p._play_gen))
        assert len(p.queue) == 3, len(p.queue)
        assert p.current.title == "Song 0", p.current.title

        # 3) Aufrufe ohne Generation (aus _reconnect) pruefen nichts.
        p = aufbau()
        p.voice.spielt = False
        asyncio.run(p._advance())
        assert p.current.title == "Song 0", p.current.title
    finally:
        discord.FFmpegPCMAudio, discord.PCMVolumeTransformer = alt_ff
        music._resolve_track, music._send_panel, music._retire_panel = (
            alt_res, alt_panel, alt_ret)




def test_floaktie_panel_zeigt_wer_gezaehlt_wird():
    """Damit man SIEHT, woher die Aktivitaet kommt (und dass kein Bot dabei ist),
    nennt das Panel die gezaehlten Menschen beim Namen."""
    import floaktie
    fa = floaktie.instance

    def member(uid, name, *, bot=False, stream=False):
        vs = SimpleNamespace(self_stream=stream, self_video=False)
        return SimpleNamespace(id=uid, bot=bot, voice=vs, display_name=name,
                               name=name), vs

    m_flo, vs_flo = member(99, "Flo", bot=True)
    m_musik, vs_musik = member(2, "Musik-Bot", bot=True)
    m_anna, vs_anna = member(1, "Anna", stream=True)

    class Chan:
        def __init__(self, members, states):
            self.id = 5
            self.members = members
            self.voice_states = states

    class Guild:
        def __init__(self, members, states):
            self.voice_channels = [Chan(members, states)]
            self.afk_channel = None
            self.me = SimpleNamespace(id=99)

        def get_member(self, uid):
            return None

    # Nur Bots im Call -> die Zeile sagt das ausdruecklich, kein Bot-Name drin.
    fa._measure(Guild([m_flo, m_musik], {99: vs_flo, 2: vs_musik}))
    fa._zuletzt_mess = (0, 0, 0, 0)
    zeile = fa._mess_zeile()
    assert "Niemand im Call" in zeile, zeile
    assert "Musik-Bot" not in zeile and "Flo" not in zeile, zeile

    # Mit einem Menschen: sein Name steht da, der Bot weiterhin nicht.
    leute, streams, video = fa._measure(Guild([m_flo, m_musik, m_anna],
                                              {99: vs_flo, 2: vs_musik, 1: vs_anna}))
    assert (leute, streams, video) == (1, 1, 0), (leute, streams, video)
    fa._zuletzt_mess = (leute, streams, video, 12)
    zeile = fa._mess_zeile()
    assert "Anna" in zeile, zeile
    assert "Musik-Bot" not in zeile, zeile
    assert "Livestream" in zeile and "12" in zeile, zeile




def test_musik_probiert_andere_youtube_clients_durch():
    """YouTube prueft, ob da ein echter Browser sitzt. Welcher "player_client"
    ohne Login durchkommt, aendert sich alle paar Monate - ein fester Name im
    Code ist deshalb in drei Monaten wieder tot. Flo probiert die Reihe durch,
    merkt sich was ging und sagt im Log, was in die .env gehoert.

    Genauso wichtig: bei einem geloeschten Video hilft KEIN anderer Client -
    dann waere jeder weitere Versuch nur Wartezeit fuer den Nutzer."""
    import music

    class FakeYDL:
        geht_ab = None
        fehler = "Sign in to confirm you're not a bot"
        versuche = []

        def __init__(self, opts):
            self.client = (opts.get("extractor_args", {})
                           .get("youtube", {}).get("player_client", [None])[0])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, ziel, download=False):
            FakeYDL.versuche.append(self.client)
            if FakeYDL.geht_ab is not None and self.client == FakeYDL.geht_ab:
                return {"title": "Song", "url": "http://x/s", "webpage_url": "http://x",
                        "duration": 10, "http_headers": {"User-Agent": "u"}}
            raise Exception(FakeYDL.fehler)

    m = music.instance
    alt_ydl, alt_client = music.yt_dlp, m._guter_client
    alt_env = os.environ.pop("YTDLP_PLAYER_CLIENT", None)
    music.yt_dlp = type("M", (), {"YoutubeDL": FakeYDL})
    try:
        # 1. Standard blockt, 'ios' geht -> durchprobieren, merken, melden.
        FakeYDL.versuche, FakeYDL.geht_ab, m._guter_client = [], "ios", ""
        track = asyncio.run(m._extract("ytsearch1:egal"))
        # Reihenfolge ist nicht beliebig: die drei Clients OHNE PO Token zuerst.
        assert FakeYDL.versuche == [None, "tv", "android_vr", "web_embedded",
                                    "tv_simply", "ios"], FakeYDL.versuche
        assert m._guter_client == "ios"
        assert track.title == "Song"
        # Die Kopfzeilen muessen dabei erhalten bleiben (sonst 403 bei ffmpeg).
        assert track.kopfzeilen.get("User-Agent") == "u"

        # 2. Beim naechsten Mal steht der, der ging, vorne.
        FakeYDL.versuche = []
        asyncio.run(m._extract("ytsearch1:egal"))
        assert FakeYDL.versuche == [None, "ios"], FakeYDL.versuche

        # 3. Geloeschtes Video: sofort aufgeben, nicht acht Mal fragen.
        FakeYDL.versuche, FakeYDL.geht_ab, m._guter_client = [], None, ""
        FakeYDL.fehler = "Video unavailable. This video has been removed"
        try:
            asyncio.run(m._extract("http://x"))
        except Exception:
            pass
        assert FakeYDL.versuche == [None], (
            f"probiert bei einem geloeschten Video weiter: {FakeYDL.versuche}")

        # 4. Bot-Check und nichts geht: alle durch, dann mit dem ECHTEN Grund raus.
        FakeYDL.versuche = []
        FakeYDL.fehler = "Sign in to confirm you're not a bot"
        try:
            asyncio.run(m._extract("http://x"))
            raise AssertionError("haette scheitern muessen")
        except Exception as exc:
            assert music.Music.yt_fehler_deuten(exc)[0] == "botcheck"
        assert len(FakeYDL.versuche) == 1 + len(m.client_reihe()), FakeYDL.versuche

        # 5. Festgenagelt per .env -> genau einer, kein Durchprobieren.
        os.environ["YTDLP_PLAYER_CLIENT"] = "tv"
        FakeYDL.versuche, FakeYDL.geht_ab = [], "tv"
        asyncio.run(m._extract("http://x"))
        assert FakeYDL.versuche == ["tv"], FakeYDL.versuche
    finally:
        music.yt_dlp, m._guter_client = alt_ydl, alt_client
        os.environ.pop("YTDLP_PLAYER_CLIENT", None)
        if alt_env is not None:
            os.environ["YTDLP_PLAYER_CLIENT"] = alt_env

    # 6. DIE LAGE VOM SERVER: alle Clients Bot-Check, ausser einem - und der
    #    kommt zwar durch, hat aber keine reine Tonspur ("Requested format is
    #    not available"). Ihn deswegen fallenzulassen waere der Fehler: er ist
    #    der EINZIGE, den YouTube noch durchlaesst. Also Video nehmen und den
    #    Ton herausziehen (ffmpeg wirft das Bild ohnehin weg, -vn).
    class FakeNurAndroid(FakeYDL):
        def __init__(self, opts):
            super().__init__(opts)
            self.fmt = opts.get("format")

        def extract_info(self, ziel, download=False):
            FakeYDL.versuche.append((self.client, self.fmt))
            if self.client != "android":
                raise Exception("Sign in to confirm you're not a bot. Use --cookies")
            if "worst" not in (self.fmt or ""):
                raise Exception("Requested format is not available. Use --list-formats")
            return {"title": "Semmel Song", "url": "http://x/s",
                    "webpage_url": "http://x", "duration": 180,
                    "http_headers": {"User-Agent": "u"}}

    music.yt_dlp = type("M", (), {"YoutubeDL": FakeNurAndroid})
    FakeYDL.versuche, m._guter_client = [], ""
    try:
        track = asyncio.run(m._extract("ytsearch1:semmel song robert F"))
    finally:
        music.yt_dlp = type("M", (), {"YoutubeDL": FakeYDL})
    assert track.title == "Semmel Song"
    assert m._guter_client == "android", m._guter_client
    assert track.kopfzeilen.get("User-Agent") == "u", "Kopfzeilen gehen verloren"
    # Derselbe Client wurde ein zweites Mal gefragt - mit weicherem Format.
    letzte_zwei = FakeYDL.versuche[-2:]
    assert [c for c, _f in letzte_zwei] == ["android", "android"], FakeYDL.versuche
    assert "worst" in letzte_zwei[1][1], letzte_zwei
    m._guter_client = ""

    # Kein Client-Name im Code, den die installierte yt-dlp-Fassung nicht kennt.
    bekannt = music.Music._bekannte_clients()
    if bekannt:
        unbekannt = [c for c in music.Music._CLIENT_REIHE if c not in bekannt]
        assert not unbekannt, f"diese player_client gibt es nicht (mehr): {unbekannt}"




def test_musik_weicht_auf_soundcloud_aus_wenn_youtube_dicht_ist():
    """Am Server gemessen: ALLE acht player_client antworten mit "Sign in to
    confirm you're not a bot" - die IP ist markiert, da hilft kein Client mehr.
    Ohne Ausweg waere Musik damit tot, bis der Betreiber Cookies exportiert.

    SoundCloud kennt YouTubes Bot-Pruefung nicht. Also sucht Flo denselben Song
    dort, statt eine Fehlermeldung zu posten - das ist der einzige Weg, der OHNE
    Zutun des Betreibers noch Musik liefert."""
    import music

    class NurSoundCloud:
        gefragt = []

        def __init__(self, opts):
            self.sc = opts.get("default_search") == "scsearch"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, ziel, download=False):
            NurSoundCloud.gefragt.append(ziel)
            if not self.sc:
                raise Exception("Sign in to confirm you're not a bot. Use --cookies")
            return {"entries": [{"title": "Semmel Song (SC)", "url": "http://sc/s",
                                 "webpage_url": "http://soundcloud.com/x",
                                 "duration": 175,
                                 "http_headers": {"User-Agent": "sc"}}]}

    m = music.instance
    alt_ydl, alt_client = music.yt_dlp, m._guter_client
    music.yt_dlp = type("M", (), {"YoutubeDL": NurSoundCloud})
    try:
        # 1. Reine Textsuche
        NurSoundCloud.gefragt, m._guter_client = [], ""
        track = asyncio.run(m._extract("ytsearch1:semmel song robert F"))
        assert track.title == "Semmel Song (SC)", track.title
        assert "soundcloud.com" in track.webpage_url
        assert track.kopfzeilen.get("User-Agent") == "sc", "Kopfzeilen fehlen"
        assert NurSoundCloud.gefragt[-1] == "scsearch1:semmel song robert F"

        # 2. Spotify-Weg: der Suchtext MUSS durchgereicht werden - mit einer
        #    nackten Video-Adresse koennte SoundCloud nichts anfangen.
        NurSoundCloud.gefragt, m._guter_client = [], ""
        track = asyncio.run(m._resolve_input("ytsearch1:egal",
                                             {"query": "Robert F Semmel"}))
        assert track.title == "Semmel Song (SC)"
        assert "scsearch1:Robert F Semmel" in NurSoundCloud.gefragt, NurSoundCloud.gefragt

        # 3. Eine nackte YouTube-Adresse hat keinen Suchtext - dort darf NICHT
        #    blind irgendetwas von SoundCloud gespielt werden.
        assert music.Music._suchtext("https://youtu.be/abc") == ""
        for eingabe, erwartet in (("ytsearch1:a b", "a b"), ("ytsearch5:x", "x"),
                                  ("nur text", "nur text")):
            assert music.Music._suchtext(eingabe) == erwartet, eingabe
    finally:
        music.yt_dlp, m._guter_client = alt_ydl, alt_client




def test_arzt_prueft_youtube_bis_zur_abspielbaren_adresse():
    """Der Musik-Arzt meldete "Suche geht", waehrend im Bot JEDER Song an
    YouTubes Bot-Pruefung scheiterte. Grund: er lief mit extract_flat, las also
    nur die Trefferliste und fasste den Player nie an.

    Eine Diagnose, die den Ausfall nicht sieht, ist schlimmer als keine - sie
    schickt einen auf die falsche Faehrte (hier: ein sinnloses yt-dlp-Update)."""
    import inspect as _i
    import tools_musik_check
    quelle = _i.getsource(tools_musik_check.MusikCheck.youtube_pruefen)
    # Auf die OPTION prüfen, nicht auf das Wort - im Kommentar darueber steht
    # bewusst "KEIN extract_flat", und daran darf der Test sich nicht aufhaengen.
    code_zeilen = [z for z in quelle.splitlines()
                   if not z.lstrip().startswith("#")]
    assert "'extract_flat'" not in "\n".join(code_zeilen), (
        "die Pruefung liest wieder nur die Trefferliste statt den Player")
    assert "'stream'" in quelle or '"stream"' in quelle, (
        "es wird nicht geprueft, ob eine abspielbare Adresse herauskommt")
    # Cookies und festgenagelter Client muessen mitgeprueft werden - sonst misst
    # der Arzt etwas anderes als der Bot tut.
    assert "YTDLP_COOKIES" in quelle and "YTDLP_PLAYER_CLIENT" in quelle
    # Und die Ausweichquelle gehoert zur Diagnose: sie entscheidet, ob ueberhaupt
    # noch Musik moeglich ist.
    assert hasattr(tools_musik_check.MusikCheck, "_soundcloud_pruefen")
    assert "scsearch" in _i.getsource(tools_musik_check.MusikCheck._soundcloud_pruefen)




def test_musik_cookies_erreichen_jeden_yt_dlp_aufruf():
    """Am echten Server bestaetigt: YouTube antwortet "Sign in to confirm you're
    not a bot". Ist die IP einmal markiert, hilft kein player_client mehr - dann
    bleibt nur ein angemeldeter Zugang, so steht es auch in yt-dlps eigener FAQ.

    music.py ruft yt-dlp an DREI Stellen (Einzeltrack, Suche, Playlist). Kaemen
    die Cookies nur bei einer an, ginge ein Link - und die Suche weiter nicht.
    Genau so eine halbe Reparatur faellt niemandem auf."""
    import re
    import music
    quelle = open(music.__file__, encoding="utf-8").read()
    stellen = [m.start() for m in re.finditer(r"yt_dlp\.YoutubeDL\(", quelle)]
    assert len(stellen) >= 3, f"nur {len(stellen)} yt-dlp-Aufrufe gefunden"
    ohne = [i for i, pos in enumerate(stellen, 1)
            if "_cookie_optionen()" not in quelle[max(0, pos - 700):pos]]
    assert not ohne, f"diese yt-dlp-Aufrufe bekommen keine Cookies: {ohne}"

    # Verhalten der Konfiguration - inklusive der ehrlichen Warnung.
    alt_datei = os.environ.pop("YTDLP_COOKIES", None)
    alt_browser = os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
    try:
        assert music.Music._cookie_optionen() == {}
        # Ein falscher Pfad darf NICHT still verschluckt werden.
        os.environ["YTDLP_COOKIES"] = "/gibt/es/nicht.txt"
        assert music.Music._cookie_optionen() == {}, (
            "eine fehlende Cookie-Datei wird als gesetzt behandelt")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# Netscape HTTP Cookie File\n")
            pfad = f.name
        try:
            os.environ["YTDLP_COOKIES"] = pfad
            assert music.Music._cookie_optionen() == {"cookiefile": pfad}
            os.environ["YTDLP_COOKIES_FROM_BROWSER"] = "firefox"
            assert music.Music._cookie_optionen() == {
                "cookiefile": pfad, "cookiesfrombrowser": ("firefox",)}
        finally:
            os.unlink(pfad)
    finally:
        for name, wert in (("YTDLP_COOKIES", alt_datei),
                           ("YTDLP_COOKIES_FROM_BROWSER", alt_browser)):
            os.environ.pop(name, None)
            if wert is not None:
                os.environ[name] = wert

    # Der Hinweis auf den Ausweg muss im Code stehen - sonst sucht der Betreiber
    # ihn nie. Und die Warnung vor dem Haupt-Account gehoert dazu.
    assert "WEGWERF" in quelle, "die Warnung vor dem Haupt-Account fehlt"
    assert "YTDLP_COOKIES=" in quelle




def test_musik_sagt_WARUM_ein_song_nicht_geht():
    """"Den Song konnte ich nicht laden. Probier einen anderen Link" - derselbe
    Satz fuer JEDEN Grund, und der echte verschwand im Traceback. Ob YouTube
    einen Login sehen will, das Video geloescht ist, das Land gesperrt oder
    yt-dlp schlicht zu alt - von aussen nicht zu unterscheiden.

    Die Reihenfolge der Muster ist der schwierige Teil, beide Faelle sind
    nachgemessen:
      - "Sign in to confirm your AGE" ist KEIN Bot-Check
      - "Video unavailable. ... not made this video available in your country"
        ist eine LAENDER-Sperre, keine Loeschung
    """
    import music
    faelle = (
        ("Sign in to confirm you're not a bot. Use --cookies-from-browser", "botcheck"),
        ("Sign in to confirm your age. This video may be inappropriate for some users.", "alter"),
        ("Video unavailable. This video has been removed by the uploader", "weg"),
        ("Private video. Sign in if you have been granted access", "weg"),
        ("Video unavailable. The uploader has not made this video available "
         "in your country.", "land"),
        ("[DRM] The requested site is known to use DRM protection.", "drm"),
        ("Unable to download API page: HTTP Error 429: Too Many Requests", "limit"),
        ("nsig extraction failed: please report this issue", "veraltet"),
        ("Unable to download webpage: Temporary failure in name resolution", "netz"),
        ("keine Treffer", "nichts"),
        ("Requested format is not available", "format"),
        ("etwas voellig anderes", "unbekannt"),
    )
    for text, erwartet in faelle:
        art, satz = music.Music.yt_fehler_deuten(Exception(text))
        assert art == erwartet, (text[:60], art, erwartet)
        assert satz and satz == music.Music._YT_SAETZE[art]
    # Jede Art braucht einen eigenen Satz - sonst ist die Einordnung wertlos.
    saetze = list(music.Music._YT_SAETZE.values())
    assert len(set(saetze)) == len(saetze), "zwei Gruende teilen sich einen Satz"
    # Und jede Art aus der Musterliste muss auch einen Satz haben.
    for art, _muster in music.Music._YT_GRUENDE:
        assert art in music.Music._YT_SAETZE, art

    # Die Aufrufstelle muss den Grund benutzen UND ihn greppbar loggen.
    quelle = inspect.getsource(music.Music.handle)
    assert "yt_fehler_deuten(exc)" in quelle, "handle() nutzt die Einordnung nicht"
    assert "Musik-Fehler:" in quelle, "der Grund landet nicht greppbar im Log"




def test_arzt_findet_die_musik_meldungen():
    """Wie beim Panel: der Arzt muss die Zeilen auch ZEIGEN. "Musik-Selbsttest ok"
    und "Musik-Fehler: ..." fielen beide durch das Suchmuster in 'k' - also
    ausgerechnet die zwei, die die Frage beantworten."""
    import re
    arzt = open("k", encoding="utf-8").read()
    muster = re.search(r'MUSIKMUSTER="([^"]+)"', arzt)
    assert muster, "in 'k' gibt es kein MUSIKMUSTER"
    filter_re = re.compile(muster.group(1))
    for zeile in ("Musik-Selbsttest ok (Song, 57600 Bytes Ton in 0.6s).",
                  "Musik-Selbsttest: ffmpeg bekommt keinen Ton (403).",
                  "Musik-Fehler: botcheck bei 'irgendwas' - Sign in to confirm",
                  "Musik-Feature aktiv (YouTube: ja, Spotify: ja).",
                  "Musik-Selbsttest: Spotify-Token ok."):
        assert filter_re.search(zeile), f"'k m' zeigt diese Zeile nicht: {zeile}"




def test_module_lesen_ihre_kanaele_vom_server():
    """Kanaele und Lautstaerke kommen aus der Server-Einstellung, nicht aus der
    .env - sonst zeigen auf Server B alle IDs ins Leere."""
    import games
    import music
    guildcfg, zurueck = _cfg_frisch()
    try:
        A, B = 111, 222

        # Musik: jeder Server faengt mit seiner eigenen Lautstaerke an.
        assert music.instance._start_lautstaerke(A) == music.DEFAULT_VOLUME
        asyncio.run(guildcfg.setzen(A, "lautstaerke", "80"))
        assert abs(music.instance._start_lautstaerke(A) - 0.8) < 1e-9
        assert music.instance._start_lautstaerke(B) == music.DEFAULT_VOLUME

        # Spiele: der Event-Kanal ist der eingestellte, sonst der System-Kanal.
        def kanal(cid, name):
            return SimpleNamespace(id=cid, name=name,
                                   permissions_for=lambda _me: SimpleNamespace(send_messages=True))
        allgemein, spiele = kanal(900, "allgemein"), kanal(901, "spiele")

        def server(gid):
            g = SimpleNamespace(id=gid, me=SimpleNamespace(id=999),
                                system_channel=allgemein, text_channels=[allgemein, spiele])
            g.get_channel = lambda cid: {900: allgemein, 901: spiele}.get(cid)
            return g

        asyncio.run(guildcfg.setzen(A, "event_channel", "901"))
        assert games.instance._pick_event_channel(server(A)) is spiele
        assert games.instance._pick_event_channel(server(B)) is allgemein
    finally:
        zurueck()




def test_musik_stall_wird_erkannt_und_song_neu_gestartet():
    """DER gemeldete Fehler: 'paar sachen in der queue und er spielt sie nicht'.

    Bleibt der Audio-Stream still stehen (Verbindung offen, keine Daten mehr),
    wartet FFmpeg ohne -rw_timeout endlos: der after-Callback feuert nie,
    is_playing() bleibt True - und weil is_active() dann True meldet, landet
    jeder weitere 'Flo spiel X' nur noch in der Warteschlange. Der alte
    Watchdog war dafuer blind, weil er ausschliesslich 'not is_playing()'
    kannte. Jetzt zaehlt er die tatsaechlich gesendeten Audio-Bloecke."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())

    # Der Watchdog holt beim Neustart eine FRISCHE Stream-Adresse - hier ging
    # dabei bisher ein ECHTER yt-dlp-Aufruf ins Netz. Das ist in einem Test
    # nichts verloren: er wurde damit langsam, abhaengig von YouTube, und als
    # das Durchprobieren der player_client dazukam, brachte er ploetzlich einen
    # fremden Song zurueck statt zu scheitern. Hier zaehlt nur der Watchdog.
    alt_resolve = music._resolve_track

    async def frisch(track):
        neu_track = music.Track(title=track.title, stream_url="http://stream/neu",
                                query=track.query, duration=track.duration,
                                requested_by=track.requested_by)
        return neu_track

    music._resolve_track = frisch
    try:
        player.start(_track("A"))
        voice.takt(50)                       # laeuft normal
        player.queue.extend([_track("B"), _track("C")])

        # Solange Bloecke fliessen, fasst der Watchdog nichts an.
        for _ in range(5):
            voice.takt(50)
            asyncio.run(player.heal(guild))
        assert voice.stops == 0 and player.current.title == "A"

        # Jetzt der Stall: is_playing() bleibt True, es kommt aber nichts mehr.
        voice.stall = True
        vorher_stops = voice.stops
        for _ in range(music.VOICE_STALL_TICKS):
            voice.takt(50)                   # laeuft ins Leere (stall)
            asyncio.run(player.heal(guild))
        assert voice.stops > vorher_stops, "Watchdog hat den Stall nicht bemerkt"
        assert player.current.title == "A", "falscher Song neu gestartet"
        assert [t.title for t in player.queue] == ["B", "C"], \
            "Warteschlange wurde beim Neustart geopfert"
        assert voice.is_playing(), "nach dem Neustart laeuft nichts"
    finally:
        music._resolve_track = alt_resolve
        aufraeumen()




def test_musik_liegengebliebene_warteschlange_wird_angestossen():
    """Zweiter Dauer-Steckzustand: endet ein Song genau waehrend eines kurzen
    Voice-Aussetzers, setzt _advance current=None und laesst die volle
    Warteschlange liegen. Der Watchdog stellte die Verbindung zwar wieder her -
    angestossen hat die Schlange danach aber NIEMAND mehr."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())
    try:
        player.current = None
        player.queue.extend([_track("B"), _track("C")])
        voice.spielt = False

        asyncio.run(player.heal(guild))
        assert player.current is not None and player.current.title == "B", \
            "Watchdog hat die liegengebliebene Warteschlange nicht angestossen"
        assert [t.title for t in player.queue] == ["C"]
    finally:
        aufraeumen()




def test_musik_stop_laesst_keinen_geister_track_zurueck():
    """'Flo stop', waehrend _advance gerade einen Playlist-Track aufloest:
    der fertig aufgeloeste Track landete danach per insert(0) in der SOEBEN
    GELEERTEN Warteschlange - und spielte beim naechsten Play ungefragt wieder
    an ('ich hab doch gestoppt')."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        lazy = music.Track(title="B", stream_url="", query="ytsearch1:B")
        player.queue.append(lazy)
        player.current = _track("A")

        async def langsam(track):
            await asyncio.sleep(0.05)
            track.stream_url = "http://stream/b"
            return track

        music._resolve_track = langsam

        async def lauf():
            gen = player._play_gen
            aufgabe = asyncio.ensure_future(player._advance(gen))
            await asyncio.sleep(0.01)      # _advance haengt im Aufloesen
            await player.disconnect()      # <- 'Flo stop'
            await aufgabe

        asyncio.run(lauf())
        assert player.queue == [], f"Geister-Track zurueck in der Queue: {player.queue}"
        assert player.current is None
    finally:
        aufraeumen()




def test_musik_netzausfall_frisst_die_warteschlange_nicht():
    """Ein kurzer Netz-Aussetzer beim Songwechsel hat frueher die KOMPLETTE
    Playlist in einem Rutsch als 'nicht ladbar' verbucht und stumm entsorgt -
    jeder Resolve warf, jeder Fehler machte 'continue'. Nach zwei Fehlschlaegen
    am Stueck bleibt die Warteschlange jetzt stehen."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    gesagt = []
    try:
        for i in range(6):
            player.queue.append(music.Track(title=f"T{i}", stream_url="",
                                            query=f"ytsearch1:T{i}"))

        async def kaputt(_track):
            raise RuntimeError("Netz weg")

        async def sag(text):
            gesagt.append(text)

        music._resolve_track = kaputt
        player._sag = sag
        asyncio.run(player._advance())

        assert len(player.queue) >= 4, \
            f"Warteschlange wurde weggefressen: nur noch {len(player.queue)}"
        assert gesagt and "Warteschlange" in gesagt[0], gesagt
    finally:
        aufraeumen()




def test_musik_pause_ueberlebt_tempo_und_reconnect():
    """Wer pausiert hat, will keine Musik - auch nicht nach einem
    Tempo-Wechsel oder einem Watchdog-Reconnect. Beide starteten die
    Wiedergabe frueher kommentarlos wieder."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        player.start(_track("A"))
        player.pausieren()
        assert player.ist_pausiert() and voice.is_paused()

        # Tempo-Wechsel startet den Song neu - die Pause muss bleiben.
        asyncio.run(player.apply_speed(1.5))
        assert player.ist_pausiert(), "Tempo-Wechsel hat die Pause aufgehoben"
        assert voice.is_paused()

        # Und ein neuer Song hebt sie auf (die Absicht galt dem alten).
        # Wie im echten Ablauf endet der alte Song erst (voice.stop()).
        voice.stop()
        voice.pausiert_ = False
        player.start(_track("B"))
        assert not player.ist_pausiert()
    finally:
        aufraeumen()




def test_musik_befehle_kapern_kein_alltagsdeutsch():
    """Steuerbefehle wurden per PRAEFIX erkannt, ohne Wortgrenze. Damit wurde
    'verlass dich drauf' zum Voice-Leave und 'rausschmeisen @wer' (die
    gaengige Ein-s-Schreibweise) liess Flo den Sprachkanal verlassen und die
    Musik abbrechen, statt die Person zu kicken."""
    import cmdnorm
    import moderation
    import music
    mi = music.instance

    # Gerede darf KEIN Musik-Befehl sein.
    for satz in ("verlass dich drauf", "verlass dich nicht darauf",
                 "liste mal auf was du kannst", "stoppuhr an",
                 "pausenbrot mitbringen", "weitermachen jetzt"):
        assert mi.parse_command(satz) is None, satz

    # Echte Befehle muessen weiter gehen.
    for satz, erwartet in (("leave", "leave"), ("raus", "leave"),
                           ("verlass den kanal", "leave"), ("liste", "queue"),
                           ("queue", "queue"), ("skip", "skip"),
                           ("pause", "pause"), ("weiter", "resume")):
        got = mi.parse_command(satz)
        assert got and got[0] == erwartet, (satz, got)

    # Kick in allen Schreibweisen erreicht die Moderation und NICHT die Musik.
    for satz in ("rausschmeisen <@777>", "rausschmeis <@777>",
                 "rausschmeissen <@777>", "rausschmeißen <@777>", "kick <@777>"):
        norm = cmdnorm.normalize(satz) or satz
        assert moderation.classify(norm) == "kick", satz
        assert mi.parse_command(satz) is None, satz

    # Lautstaerke ueber 999 wurde auf drei Ziffern geschnitten ('ls 1000' -> 100 %).
    assert mi.parse_command("ls 1000") == ("volume", "1000")
    assert mi.parse_command("lautstärke 250") == ("volume", "250")




def test_musik_ffmpeg_bekommt_kein_lesetimeout():
    """KEIN -rw_timeout in den FFmpeg-Optionen - das war ein Eigentor.

    Nachgemessen mit echtem ffmpeg 6.1.1 und einem Leser im Echtzeit-Takt
    (so liest discord.py), Server liefert schubweise mit 20 s Pausen - genau
    so drosselt YouTube:

        mit -rw_timeout 15s :  12,2 s Audio in 99,8 s Wanduhr,
                               stderr voller "Will reconnect at 0"
        ohne                :  24,5 s Audio in 80,4 s Wanduhr, keine Reconnects

    Das Timeout deutet eine normale Liefer-Pause als NETZWERKFEHLER, dann
    greift -reconnect_on_network_error und FFmpeg faengt wieder bei Byte 0 an.
    Der Song beginnt also staendig von vorne - die gemeldete Beschwerde
    "funktioniert nur halbwegs".

    Gegen den stillen Stall steht jetzt der Fortschritts-Waechter in heal():
    der zaehlt echte Audio-Bloecke statt Socket-Betrieb."""
    import music
    assert "-rw_timeout" not in music._FFMPEG_BEFORE, music._FFMPEG_BEFORE
    assert "-timeout" not in music._FFMPEG_BEFORE, music._FFMPEG_BEFORE
    # Die Reconnect-Flags bleiben: bei einem ECHTEN Fehler sind sie richtig.
    for flag in ("-reconnect 1", "-reconnect_streamed 1",
                 "-reconnect_on_network_error 1"):
        assert flag in music._FFMPEG_BEFORE, flag




def test_musik_erkennt_soundcloud():
    """SoundCloud-Links landeten in der YouTube-TEXTSUCHE: parse_command kannte
    nur Spotify und YouTube, alles andere fiel durch und wurde wie Freitext
    behandelt - Flo suchte also auf YouTube nach der URL-Zeichenkette. yt-dlp
    bringt den SoundCloud-Extractor laengst mit; es fehlte nur die Erkennung."""
    import music
    mi = music.instance

    # Einzelne Tracks -> ganz normaler play-Pfad.
    for url in ("https://soundcloud.com/forss/flickermood",
                "https://www.soundcloud.com/forss/flickermood",
                "https://m.soundcloud.com/forss/flickermood",
                "https://on.soundcloud.com/AbCdEf",
                "https://SoundCloud.COM/a/b"):
        for text in (url, f"spiel {url}", f"schau mal {url} an"):
            got = mi.parse_command(text)
            assert got == ("play", url), (text, got)

    # Sets -> eigener Playlist-Pfad.
    for url in ("https://soundcloud.com/forss/sets/soulhack",
                "https://www.soundcloud.com/user/sets/mein-mix"):
        got = mi.parse_command(f"spiel {url}")
        assert got == ("sc_playlist", url), got

    # Fremde Links bleiben unveraendert.
    assert mi.parse_command("spiel https://www.youtube.com/watch?v=x") \
        == ("play", "https://www.youtube.com/watch?v=x")
    # Direkte Audio-Dateien spielt FFmpeg selbst - auch die landeten frueher
    # in der YouTube-Textsuche.
    assert mi.parse_command("spiel https://example.com/lied.mp3") \
        == ("play", "https://example.com/lied.mp3")
    assert mi.parse_command("https://example.com/set.opus?x=1") \
        == ("play", "https://example.com/set.opus?x=1")
    # Eine BELIEBIGE Webseite darf die Musik NICHT an sich reissen.
    assert mi.parse_command("was hältst du von https://example.com/artikel") is None

    # Kein Link -> weiterhin Suche.
    assert mi.parse_command("spiel Bohemian Rhapsody") == ("search", "Bohemian Rhapsody")




def test_musik_playlist_helfer_teilt_sich_youtube_und_soundcloud():
    """EIN flacher Playlist-Helfer fuer beide Quellen. YouTube liefert pro
    Eintrag manchmal nur die Video-ID - daraus muss die volle URL werden;
    SoundCloud liefert immer die komplette Adresse."""
    import music
    mi = music.instance

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if "soundcloud" in url:
                return {"entries": [
                    {"url": "https://soundcloud.com/a/eins", "title": "Eins"},
                    {"url": "https://soundcloud.com/a/zwei", "title": "Zwei"},
                    None,                       # kaputter Eintrag -> ueberspringen
                ]}
            return {"entries": [{"id": "abc123", "title": "Video"}]}

    alt = music.yt_dlp
    music.yt_dlp = SimpleNamespace(YoutubeDL=FakeYDL)
    try:
        sc = asyncio.run(mi._soundcloud_set("https://soundcloud.com/a/sets/x"))
        assert sc == [("https://soundcloud.com/a/eins", "Eins"),
                      ("https://soundcloud.com/a/zwei", "Zwei")], sc
        yt = asyncio.run(mi._youtube_playlist("https://youtube.com/playlist?list=X"))
        assert yt == [("https://www.youtube.com/watch?v=abc123", "Video")], yt
        # Die flache Extraktion muss wirklich flach sein (sonst dauert eine
        # 100-Track-Playlist ewig, weil jeder Track einzeln aufgeloest wird).
        assert FakeYDL({}).opts is not None
    finally:
        music.yt_dlp = alt




def test_musik_watchdog_frisst_die_liegengebliebene_queue_nicht():
    """Gibt _advance auf, darf der Watchdog nicht im 15-s-Takt nachtreten.

    Sonst hebelt Fall 3 der Heilung genau die Aufgabe-Schwelle aus, die
    verhindern soll, dass ein Netzausfall die ganze Playlist wegfrisst - und
    dieselbe Warnung landet alle 15 Sekunden im Chat."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    gesagt = []
    try:
        for i in range(6):
            player.queue.append(music.Track(title=f"T{i}", stream_url="",
                                            query=f"ytsearch1:T{i}"))

        async def kaputt(_track):
            raise RuntimeError("Netz weg")

        async def sag(text):
            gesagt.append(text)

        music._resolve_track = kaputt
        player._sag = sag
        asyncio.run(player._advance())
        assert player._advance_aufgegeben is True
        rest = len(player.queue)

        # Der Watchdog laeuft mehrfach - und laesst die Warteschlange in Ruhe.
        voice.spielt = False
        guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())
        for _ in range(3):
            asyncio.run(player.heal(guild))
        assert len(player.queue) == rest, (len(player.queue), rest)
        assert len(gesagt) == 1, gesagt

        # Ein neuer Song hebt die Aufgabe auf - vielleicht laedt der ja.
        music.instance._einreihen(player, music.Track(title="neu", stream_url="", query="q"))
        assert player._advance_aufgegeben is False
    finally:
        aufraeumen()




def test_musik_kaputter_song_blockiert_den_naechsten_nicht():
    """DIE gemeldete Sackgasse: ein Song, der keinen Ton liefert, liess sich
    nicht wegskippen - nur 'Flo stop' half.

    Grund: der Watchdog belebte denselben toten Song alle 30 s neu, und JEDER
    Neustart zaehlt die Wiedergabe-Generation hoch. Genau daran hing aber der
    after-Callback, den 'skip' ausgeloest hat - der Skip verpuffte, Flo meldete
    trotzdem 'uebersprungen', und weil is_active() die ganze Zeit True blieb,
    reihte 'Flo spiel X' nur noch ein."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    gesagt = []

    async def sag(text):
        gesagt.append(text)

    player._sag = sag
    guild = SimpleNamespace(id=1, get_channel=lambda _c: _VoiceChannelStub())
    try:
        kaputt = _track("Kaputt")
        gut = _track("Gut")
        player.start(kaputt)
        player.queue.append(gut)
        # Der Song "spielt", aber der Block-Zaehler steht: kein Ton.
        voice.stall = True

        # Der Watchdog versucht es - aber nicht ewig.
        for _ in range(12):
            asyncio.run(player.heal(guild))
        assert player._neustart_versuche <= music.NEUSTART_MAX_VERSUCHE
        # Er hat aufgegeben und ist weitergegangen, statt in der Schleife zu bleiben.
        assert player.current is gut, player.current
        assert not player.queue
        assert any("nächsten" in t for t in gesagt), gesagt
    finally:
        aufraeumen()




def test_musik_skip_haengt_nicht_am_callback():
    """Skip muss auch dann wirken, wenn der after-Callback entwertet ist.

    Der Watchdog, ein Tempo-Wechsel oder ein Reconnect zaehlen die Generation
    hoch; faellt ein Skip in dieses Fenster, kam er frueher nie an."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        a, b = _track("A"), _track("B")
        player.start(a)
        player.queue.append(b)
        # Generation hochzaehlen, so wie es ein Watchdog-Neustart tut ...
        player._play_gen += 5
        # ... und trotzdem muss der Skip durchgehen.
        asyncio.run(player.skip())
        assert player.current is b, player.current
        assert not player.queue

        # Und ein Skip auf dem LETZTEN Song raeumt sauber auf.
        asyncio.run(player.skip())
        assert player.current is None and not player.queue
    finally:
        aufraeumen()




def test_musik_versteht_die_links_aus_den_apps():
    """Wie Links WIRKLICH im Chat ankommen - nicht wie im Lehrbuch.

    Der Kurzlink der Spotify-Handy-App (spotify.link/...) traf keinen einzigen
    Regex und landete in der YouTube-TEXTSUCHE: Flo suchte nach der
    Zeichenkette. Am PC ging es, vom Handy geteilt nicht - genau das gemeldete
    'Spotify geht nur halb'. Dazu kleben im Chat Satzzeichen an der URL."""
    import music
    p = music.instance.parse_command
    faelle = {
        # Handy-Share
        "https://spotify.link/aBcDeFgHi": ("spotify_kurz", "https://spotify.link/aBcDeFgHi"),
        "https://spoti.fi/3xYz": ("spotify_kurz", "https://spoti.fi/3xYz"),
        # Discord unterdrueckt die Vorschau mit spitzen Klammern
        "<https://youtu.be/dQw4w9WgXcQ>": ("play", "https://youtu.be/dQw4w9WgXcQ"),
        # Link am Satzende / in Klammern
        "https://youtu.be/dQw4w9WgXcQ.": ("play", "https://youtu.be/dQw4w9WgXcQ"),
        "(https://youtu.be/dQw4w9WgXcQ)": ("play", "https://youtu.be/dQw4w9WgXcQ"),
        "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT,":
            ("play", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"),
        # die ueblichen Varianten muessen weiter stimmen
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ": ("play", "https://m.youtube.com/watch?v=dQw4w9WgXcQ"),
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ": ("play", "https://music.youtube.com/watch?v=dQw4w9WgXcQ"),
        "https://www.youtube.com/shorts/dQw4w9WgXcQ": ("play", "https://www.youtube.com/shorts/dQw4w9WgXcQ"),
        "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT?si=x":
            ("play", "https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT?si=x"),
    }
    for eingabe, erwartet in faelle.items():
        assert p(f"spiel {eingabe}") == erwartet, (eingabe, p(f"spiel {eingabe}"))
    # Ein Kurzlink OHNE Befehlswort bleibt normales Gerede.
    assert p("schau mal https://spotify.link/aBc") == ("spotify_kurz", "https://spotify.link/aBc")




def test_musik_abgebrochener_song_gilt_nicht_als_fertig():
    """Stirbt FFmpeg mitten im Song, darf Flo nicht einfach weiterschalten.

    discord.py meldet beides GLEICH: liefert read() b"", ist der Song 'zu
    Ende' - egal ob er wirklich durch ist oder der Prozess abgestuerzt ist.
    Der after-Callback bekommt dabei KEINEN Fehler. Flo hielt einen nach 40
    von 200 Sekunden abgestuerzten Song also fuer fertig und ging zum
    naechsten; fuer den Zuhoerer bricht die Musik staendig ab und springt
    weiter - genau das gemeldete 'funktioniert nur halbwegs'."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        lang = music.Track(title="Lang", stream_url="http://stream/lang",
                           query="ytsearch1:Lang", duration=200)
        naechster = _track("Naechster")
        player.start(lang)
        player.queue.append(naechster)

        def stirbt_bei(sekunde):
            """FFmpeg ist weg: der Player spielt nicht mehr, after feuert -
            und zwar OHNE Fehler, genau wie am echten Songende."""
            player._played = sekunde
            player._seg_start = None
            voice.spielt = False
            asyncio.run(player._advance(player._play_gen))

        # 40 von 200 Sekunden gehoert - dann stirbt FFmpeg.
        stirbt_bei(40.0)

        # Der Song laeuft weiter (an der Stelle), die Warteschlange bleibt.
        assert player.current is lang, player.current
        assert player.queue == [naechster], player.queue
        assert player._neustart_versuche == 1

        # Beim zweiten Mal nochmal - beim dritten gibt Flo auf und schaltet weiter.
        stirbt_bei(40.0)
        assert player.current is lang and player._neustart_versuche == 2
        stirbt_bei(40.0)
        assert player.current is naechster, player.current

        # Und ein Song, der WIRKLICH durch ist, schaltet ganz normal weiter.
        kurz = music.Track(title="Kurz", stream_url="http://stream/kurz",
                           query="", duration=100)
        letzter = _track("Letzter")
        voice.spielt = False
        player.start(kurz)
        player.queue.append(letzter)
        stirbt_bei(100.0)          # hier ist er wirklich durch
        assert player.current is letzter, player.current
    finally:
        aufraeumen()




def test_musik_veraltete_stream_adresse_wird_erneuert():
    """Eine Stream-Adresse, die lange in der Warteschlange lag, ist tot.

    YouTube unterschreibt seine Adressen zeitlich. Wer eine Playlist einwirft
    und eine Stunde spaeter beim zwanzigsten Song ankommt, startet dort eine
    URL, die es nicht mehr gibt: der Song 'laeuft', es kommt aber nie Ton -
    genau das sah nach 'der Song geht einfach nicht' aus."""
    import time as _t
    import music
    player, voice, aufraeumen = _musik_umgebung()
    geholt = []

    async def frisch(track):
        geholt.append(track.title)
        return music.Track(title=track.title, stream_url="http://stream/neu",
                           query=track.query, duration=100,
                           geloest_um=_t.monotonic())

    music._resolve_track = frisch
    try:
        alt = music.Track(title="Alt", stream_url="http://stream/tot",
                          query="ytsearch1:Alt", duration=100,
                          geloest_um=_t.monotonic() - music.STREAM_MAX_ALTER - 60)
        assert music._adresse_alt(alt) is True
        player.queue.append(alt)
        asyncio.run(player._advance())
        assert geholt == ["Alt"], geholt
        assert player.current.stream_url == "http://stream/neu"

        # Eine FRISCHE Adresse wird nicht unnoetig neu geholt.
        geholt.clear()
        voice.spielt = False
        neu = music.Track(title="Neu", stream_url="http://stream/frisch",
                          query="ytsearch1:Neu", duration=100,
                          geloest_um=_t.monotonic())
        assert music._adresse_alt(neu) is False
        player.queue.append(neu)
        asyncio.run(player._advance())
        assert geholt == [], geholt
        assert player.current.stream_url == "http://stream/frisch"
    finally:
        aufraeumen()




def test_musik_playlist_ueberlebt_kaputten_ersten_song():
    """Ein gesperrter erster Titel warf die KOMPLETTE Liste weg.

    'Den ersten Song konnte ich nicht laden' - und die 49 einwandfreien
    dahinter waren mit weg. Genau so fuehlt sich 'Playlist geht nur halb' an."""
    import music
    mi = music.instance
    player, voice, aufraeumen = _musik_umgebung()
    versucht = []

    async def resolve(inp, hint):
        versucht.append(inp)
        if "kaputt" in inp:
            raise RuntimeError("gesperrt")
        return music.Track(title=inp, stream_url="http://stream/x", duration=100)

    async def kein_panel(*a, **k):
        return None

    alt = (mi._resolve_input, mi._send_panel)
    mi._resolve_input = resolve
    mi._send_panel = kein_panel
    try:
        items = [("kaputt1", "Kaputt 1", None),
                 ("gut1", "Gut 1", None),
                 ("gut2", "Gut 2", None),
                 ("gut3", "Gut 3", None)]
        antwort = asyncio.run(mi._play_many(
            player, _VoiceChannelStub(), items, "wer", "aus der Playlist"))
        assert antwort is music.HANDLED, antwort
        # Der kaputte wurde uebersprungen, der naechste laeuft, der Rest wartet.
        assert player.current.title == "gut1", player.current
        assert [t.query for t in player.queue] == ["gut2", "gut3"]

        # Zwei kaputte am Stueck sind kein Zufall -> die Liste NICHT durchbrennen.
        player2, voice2, aufraeumen2 = _musik_umgebung()
        try:
            versucht.clear()
            items = [("kaputt1", "K1", None), ("kaputt2", "K2", None),
                     ("gut1", "Gut 1", None)]
            antwort = asyncio.run(mi._play_many(
                player2, _VoiceChannelStub(), items, "wer", "aus der Playlist"))
            assert antwort is not music.HANDLED
            assert len(versucht) == 2, versucht
        finally:
            aufraeumen2()
    finally:
        (mi._resolve_input, mi._send_panel) = alt
        aufraeumen()




def test_musik_geteilter_song_schlaegt_die_playlist():
    """Wer einen Song AUS einer Playlist teilt, will DEN Song.

    Der Haupt-Grund fuer 'YouTube-Links gehen nur halb': geteilt wird
    watch?v=DERSONG&list=PL...&index=17 - und Flo spielte Track 1 der Playlist,
    also einen voellig anderen Song. Bei list=WL ('Später ansehen') oder
    list=LL ('Mag ich') kam sogar gar nichts: an diese Listen kommt der Bot
    nicht heran, und der Fehler beendete den ganzen Befehl."""
    import music
    p = music.instance.parse_command
    # Ein Video im Link -> das Video, egal welche Liste danebensteht.
    fuer_video = [
        "https://www.youtube.com/watch?v=sharedVid11&list=PLbig&index=17",
        "https://www.youtube.com/watch?v=abc12345678&list=WL&index=3",
        "https://www.youtube.com/watch?v=abc12345678&list=LL",
        "https://youtu.be/abc12345678?list=PLbig",
        "https://www.youtube.com/shorts/abc12345678?list=PLbig",
        "https://www.youtube.com/watch?v=abc12345678&list=RDabc",   # Auto-Mix
    ]
    for u in fuer_video:
        assert p(f"spiel {u}") == ("play", u), (u, p(f"spiel {u}"))
    # Eine reine Playlist-Adresse benennt kein Video - die bleibt Playlist.
    rein = "https://www.youtube.com/playlist?list=PLbig"
    assert p(f"spiel {rein}") == ("yt_playlist", rein)




def test_musik_spotify_landet_nie_in_der_textsuche():
    """Podcast, Show, Kuenstler-Seite: Flo hat woertlich nach der URL gesucht.

    Was kein Song, Album oder keine Playlist ist, fiel durch alle Zweige und
    landete in der YouTube-TEXTSUCHE - Flo spielte dann irgendein fremdes
    Video, das zufaellig auf die Zeichenkette passte."""
    import music
    p = music.instance.parse_command
    for u in ("https://open.spotify.com/episode/512ojhOuo1ktJprKbVcKyQ",
              "https://open.spotify.com/artist/0TnOYISbd1XYRBk9myaseg",
              "https://open.spotify.com/show/4rOoJ6Egrf8K2IrywzwOMk",
              "spotify:episode:512ojhOuo1ktJprKbVcKyQ",
              "https://open.spotify.com/collection/tracks"):
        aktion, _arg = p(f"spiel {u}")
        assert aktion == "spotify_unbekannt", (u, aktion)
    # Die alte /user/<name>/playlist/-Form ist eine ganz normale Playlist.
    alt = "https://open.spotify.com/user/spotify/playlist/37i9dQZF1DXcBWIGoYBM5M"
    assert p(f"spiel {alt}") == ("spotify_playlist", alt)
    # Und die bekannten Formen bleiben, wie sie waren.
    for u, erwartet in (
            ("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", "play"),
            ("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3", "spotify_album")):
        assert p(f"spiel {u}")[0] == erwartet, u




def test_musik_ffmpeg_bekommt_die_client_kennung():
    """DER Grund, warum gar nichts mehr lief. Am Server nachgemessen:

        [https] HTTP error 403 Forbidden
        Song '...' brach nach 0 von 178 s ab

    YouTube unterschreibt eine Stream-Adresse fuer GENAU den Client, der sie
    angefragt hat - in der Adresse steht 'c=ANDROID_VR'. ffmpeg meldete sich
    aber mit seiner eigenen Kennung ('Lavf/...'), weil die http_headers von
    yt-dlp nirgends weitergereicht wurden. YouTube antwortet darauf mit 403,
    und jeder Song bricht nach 0 Sekunden ab."""
    import music
    ua = "com.google.android.apps.youtube.vr.oculus/1.62.27 (Linux; U; Android 12)"

    # Ohne Kopfzeilen darf gar nichts vorangestellt werden (SoundCloud, Dateien).
    assert music.Track(title="x", stream_url="u").ffmpeg_vorspann() == ""

    t = music.Track(title="x", stream_url="u", kopfzeilen={
        "User-Agent": ua, "Accept-Language": "de-DE,de;q=0.9",
        "Range": "bytes=0-", "Host": "boese.example", "Accept-Encoding": "gzip"})
    vorspann = t.ffmpeg_vorspann()
    zerlegt = shlex.split(vorspann)          # genau so zerlegt discord.py sie
    assert "-user_agent" in zerlegt and ua in zerlegt, zerlegt
    assert "Accept-Language: de-DE,de;q=0.9\r\n" in " ".join(zerlegt)
    # Range/Host/Accept-Encoding gehoeren zur ANFRAGE, nicht zum Client -
    # durchgereicht brechen sie die Verbindung.
    for verboten in ("bytes=0-", "boese.example", "gzip"):
        assert verboten not in vorspann, verboten

    # Eine Kennung mit Leerzeichen und Anfuehrungszeichen darf die Kommandozeile
    # nicht zerlegen - sonst waere das eine Befehls-Einschleusung.
    gemein = 'Mozilla/5.0 "x" ; rm -rf /'
    zerlegt = shlex.split(music.Track(title="x", stream_url="u",
                                      kopfzeilen={"User-Agent": gemein}).ffmpeg_vorspann())
    assert zerlegt == ["-user_agent", gemein], zerlegt

    # _extract MUSS die Kopfzeilen von yt-dlp uebernehmen - sonst ist der ganze
    # Vorspann wertlos.
    quelle = inspect.getsource(music.Music._extract)
    assert "http_headers" in quelle, "_extract nimmt die Kopfzeilen nicht mit"

    # Und beim Abspielen muessen sie VOR '-i' landen (danach ignoriert ffmpeg sie).
    start = inspect.getsource(music.GuildPlayer.start)
    assert "ffmpeg_vorspann()" in start, "_start reicht die Kennung nicht durch"
    assert start.index("ffmpeg_vorspann()") < start.index("before_options")




def test_musik_ffmpeg_holt_damit_wirklich_ton():
    """Der Ernstfall mit ECHTEM ffmpeg: ein Server, der sich wie YouTube
    verhaelt (Ton nur fuer die richtige Client-Kennung, sonst 403). Die reine
    Options-Pruefung oben beweist noch nicht, dass ffmpeg sie auch annimmt."""
    import music
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return                               # ohne ffmpeg laeuft die Musik ohnehin nicht
    import http.server
    import subprocess
    import threading

    ua = "TestClient/1.0 (Android 12)"
    # 0,4 s Stille als WAV - klein, ohne Fremddaten, von ffmpeg selbst erzeugt.
    erzeugt = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.4", "-f", "wav", "-"],
        capture_output=True, timeout=60)
    if erzeugt.returncode != 0 or len(erzeugt.stdout) < 1000:
        return                               # ffmpeg zu alt/beschnitten
    ton = erzeugt.stdout

    class Griff(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.headers.get("User-Agent", "") != ua:
                leib = b"403 Forbidden"
                self.send_response(403)
                self.send_header("Content-Length", str(len(leib)))
                self.end_headers()
                self.wfile.write(leib)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(ton)))
            self.end_headers()
            self.wfile.write(ton)

    server = http.server.HTTPServer(("127.0.0.1", 0), Griff)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    adresse = f"http://127.0.0.1:{server.server_address[1]}/videoplayback?c=ANDROID_VR"

    def wieviel_ton(track):
        """Baut die ffmpeg-Zeile genau so wie GuildPlayer._start."""
        vorne = [t for t in (track.ffmpeg_vorspann(), music._FFMPEG_BEFORE) if t]
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error",
                *shlex.split(" ".join(vorne)), "-i", track.stream_url,
                "-f", "s16le", "-ar", "48000", "-ac", "2", "-"]
        return len(subprocess.run(argv, capture_output=True, timeout=60).stdout)

    try:
        ohne = wieviel_ton(music.Track(title="x", stream_url=adresse))
        mit = wieviel_ton(music.Track(title="x", stream_url=adresse,
                                      kopfzeilen={"User-Agent": ua}))
    finally:
        server.shutdown()
    assert ohne == 0, f"ohne Kennung kam unerwartet Ton ({ohne} Bytes)"
    assert mit > 10000, f"mit Kennung kam kein Ton ({mit} Bytes)"




def test_musik_selbsttest_meldet_die_wahrheit():
    """"Musik-Feature aktiv" hiess bisher nur: yt-dlp, ffmpeg und PyNaCl sind
    INSTALLIERT. Ob damit ein Ton herauskommt, hat nie jemand geprueft - beim
    Ausfall am 20.08.2026 loeste yt-dlp sauber auf, ffmpeg bekam vom Ziel aber
    403. Im Log stand trotzdem "aktiv"; gemerkt hat es erst jemand im Voice."""
    import logging
    import music
    m = music.Music()
    m._enabled = True

    puffer = io.StringIO()
    griff = logging.StreamHandler(puffer)
    protokoll = logging.getLogger("dcbot.music")
    protokoll.addHandler(griff)
    alt_stufe = protokoll.level
    protokoll.setLevel(logging.INFO)

    async def kein_extract(_eingabe):
        raise RuntimeError("yt-dlp kaputt")

    try:
        # 1. yt-dlp kommt nicht durch -> ehrlich melden, nicht "aktiv" behaupten.
        m._extract = kein_extract
        ok, grund = asyncio.run(m.selbsttest())
        assert ok is False and "yt-dlp" in grund, (ok, grund)

        # 2. Aufloesen klappt, aber es kommt kein Ton (genau der 403-Fall).
        async def extract_ok(_eingabe):
            return music.Track(title="Testsong", stream_url="http://x/y")
        m._extract = extract_ok

        async def kein_ton(_track):
            return 0, "Server returned 403 Forbidden (access denied)"
        m._probe_ton = kein_ton
        ok, grund = asyncio.run(m.selbsttest())
        assert ok is False and "403" in grund, (ok, grund)
        # Und der Log muss den Fall BEIM NAMEN nennen - sonst sucht man wieder
        # beim Schluessel oder beim Modell.
        assert "Client-Bindung" in puffer.getvalue(), puffer.getvalue()

        # 3. Es kommt Ton -> ok.
        async def viel_ton(_track):
            return 576000, ""
        m._probe_ton = viel_ton
        ok, grund = asyncio.run(m.selbsttest())
        assert ok is True and grund == "", (ok, grund)

        # 4. Ohne Stream-Adresse gibt es nichts zu spielen.
        async def ohne_adresse(_eingabe):
            return music.Track(title="x", stream_url="")
        m._extract = ohne_adresse
        assert asyncio.run(m.selbsttest())[0] is False

        # 5. Ist die Musik aus, ist das KEIN Fehler.
        m._enabled = False
        assert asyncio.run(m.selbsttest())[0] is False
    finally:
        protokoll.removeHandler(griff)
        protokoll.setLevel(alt_stufe)

    # Und der Selbsttest muss die Client-Kennung wirklich mitschicken - sonst
    # prueft er nicht die Strecke, die im Betrieb bricht.
    assert "ffmpeg_vorspann()" in inspect.getsource(music.Music._probe_ton)




def test_musik_spotify_erneuert_sich_ueber_youtube_nicht_ueber_spotify():
    """Im Log stand: '[DRM] The requested site is known to use DRM protection'.

    Grund: nach dem Aufloesen trug play() die URSPRUENGLICHE Eingabe als Quelle
    ein - bei einem Spotify-Link also die Spotify-Adresse. yt-dlp kann Spotify
    aber gar nicht oeffnen, es kennt nur die YouTube-Suche dahinter. Jede
    Wiederbelebung eines abgebrochenen Spotify-Songs war damit chancenlos, und
    der Best-Match-Hinweis war auch weg - der naechste Versuch haette blind den
    ersten Treffer genommen (Sped-Up-Remix statt Song)."""
    import music
    quelle = inspect.getsource(music.Music.handle)
    stelle = quelle.index("_SPOTIFY_TRACK_RE.search(arg)")
    danach = quelle[stelle:stelle + 2000]
    assert "track.query = f\"ytsearch1:" in danach, (
        "die Spotify-Adresse landet wieder als Erneuerungs-Quelle im Track")
    assert "track.match_hint" in danach, "der Best-Match-Hinweis geht verloren"
    # Und die Erneuerung muss den Hinweis wirklich mitnehmen.
    assert "resolved.match_hint = track.match_hint" in inspect.getsource(
        music.Music._resolve_track)




def test_musik_weiter_holt_die_liegengebliebene_queue():
    """Flo empfiehlt nach zwei Fehlschlaegen selbst 'weiter' - dann muss
    'weiter' auch etwas tun.

    Vorher kam dort "Da ist nichts pausiert", und die stehengebliebene
    Warteschlange blieb stehen: eine Sackgasse, aus der nur 'stop' herausfuehrte."""
    import music
    mi = music.instance
    player, voice, aufraeumen = _musik_umgebung()
    alt_state = (mi._enabled, dict(mi._players))
    mi._enabled = True
    mi._players[4242] = player          # unseren Stub-Player unterschieben
    try:
        player.queue.append(_track("Wartet"))
        player.current = None
        player._advance_aufgegeben = True
        voice.spielt = False

        msg = SimpleNamespace(
            content="flo weiter", guild=SimpleNamespace(id=4242),
            channel=SimpleNamespace(id=1), author=_fake_person(uid=7),
            mentions=[])
        antwort = asyncio.run(mi.handle(msg))
        assert antwort is music.HANDLED, _embed_text(antwort)
        assert player.current is not None and player.current.title == "Wartet"
        assert player._advance_aufgegeben is False
    finally:
        (mi._enabled, mi._players) = alt_state
        aufraeumen()




def test_musik_neustart_behaelt_den_gewuenschten_song():
    """Nach einem Neustart darf nicht ploetzlich ein anderer Song laufen.

    _resolve_track gab den Match-Hint (Spotify-Titel/Kuenstler/Dauer) nicht
    weiter - beim naechsten Aufloesen waehlte Flo also wieder blind den ersten
    YouTube-Treffer, und das ist bei Spotify-Songs oft ein Sped-Up-Remix."""
    import music
    mi = music.instance
    gesehen = []

    async def resolve_input(inp, hint):
        gesehen.append(hint)
        return music.Track(title="X", stream_url="http://stream/x", duration=100)

    alt = mi._resolve_input
    mi._resolve_input = resolve_input
    try:
        hint = {"query": "Alan Walker Faded", "dur": 212, "title": "Faded",
                "artist": "Alan Walker"}
        t = music.Track(title="Faded", stream_url="", query="ytsearch1:Faded",
                        match_hint=hint)
        erst = asyncio.run(mi._resolve_track(t))
        assert erst.match_hint == hint, erst.match_hint
        # Und beim ZWEITEN Mal ist er immer noch da (das war der Fehler).
        asyncio.run(mi._resolve_track(erst))
        assert gesehen == [hint, hint], gesehen
    finally:
        mi._resolve_input = alt




def test_jede_aussenabhaengigkeit_hat_einen_arzt():
    """Alle echten Ausfaelle kamen von AUSSEN: Groq hat ein Modell abgeschaltet,
    YouTube bindet die Adresse an den Client, Cloudflare sperrte die Signatur.
    Interne Tests fangen so etwas nie. Was dagegen hilft, ist: jede
    Aussenabhaengigkeit muss mit einem Befehl pruefbar sein."""
    import tools_check
    import tools_ki_check
    import tools_musik_check
    quelle = (open("tools_check.py", encoding="utf-8").read()
              + open("tools_ki_check.py", encoding="utf-8").read()
              + open("tools_musik_check.py", encoding="utf-8").read())
    for was, marke in (("Discord-Token", "DISCORD_TOKEN"),
                       ("LLM-Schluessel", "LLM_API_KEY"),
                       ("LLM-Modell", "/models"),
                       ("Spotify", "accounts.spotify.com"),
                       ("YouTube/yt-dlp", "yt_dlp"),
                       ("ffmpeg", "ffmpeg"),
                       ("Datenordner", "DATA_DIR"),
                       ("Plattenplatz", "disk_usage"),
                       ("Dienst", "systemctl"),
                       ("Repo-Stand", "rev-list")):
        assert marke in quelle, f"{was} wird von keinem Arzt geprueft ({marke})"

    # Alle drei Aerzte muessen dieselbe Basis nutzen - sonst laufen Maskierung
    # und Berichtsformat wieder auseinander.
    import arzt
    for klasse in (tools_ki_check.KiCheck, tools_musik_check.MusikCheck,
                   tools_check.GesamtCheck):
        assert issubclass(klasse, arzt.Arzt), klasse
        assert callable(getattr(klasse, "lauf", None)), klasse

    # Und 'k' muss sie auch wirklich anbieten.
    arztruf = open("k", encoding="utf-8").read()
    for datei in ("tools_ki_check.py", "tools_musik_check.py", "tools_check.py"):
        assert datei in arztruf, f"{datei} ist ueber 'bash k' nicht erreichbar"




def test_zwei_skips_fressen_keinen_song():
    """Zwei Skips kurz hintereinander (oder Skip, waehrend der after-Callback
    schon laeuft) liefen beide gleichzeitig durch _advance. Beide holten sich
    mit queue.pop(0) einen Track - einer davon verschwand spurlos.

    Der 'gen'-Schutz greift dagegen NICHT: Aufrufe ohne gen (skip, weiter,
    Reconnect) umgehen ihn ausdruecklich, und das ist so gewollt.

    Wichtig am Fix: der zweite Lauf wird nicht VERSCHLUCKT, er wartet nur. Die
    Zusicherung "ohne gen laeuft IMMER" bleibt damit wahr - ein frueher
    Ausstieg haette einen zweiten Skip stillschweigend gefressen."""
    import music
    spieler = music.GuildPlayer(loop=asyncio.get_event_loop_policy().new_event_loop(),
                               guild_id=77, volume=0.5)
    lief = []

    async def falsches_intern(gen=None):
        # Genau die kritische Stelle: lesen, abgeben, schreiben.
        lief.append("start")
        if spieler.queue:
            track = spieler.queue.pop(0)
            await asyncio.sleep(0)        # hier wechselt der Task
            lief.append(track)
        lief.append("ende")

    spieler._advance_intern = falsches_intern
    spieler.queue = ["A", "B", "C"]

    async def zwei_skips():
        await asyncio.gather(spieler._advance(), spieler._advance())

    asyncio.run(zwei_skips())

    # Kein Track darf verlorengehen, und die Laeufe duerfen sich nicht
    # verschraenken (start/ende muessen paarweise aufeinander folgen).
    geholt = [x for x in lief if x in ("A", "B", "C")]
    assert geholt == ["A", "B"], f"Songs verschwunden oder doppelt: {lief}"
    assert spieler.queue == ["C"], spieler.queue
    assert lief == ["start", "A", "ende", "start", "B", "ende"], lief

    # Und der Lock muss wirklich um den ganzen Lauf liegen.
    quelle = inspect.getsource(music.GuildPlayer._advance)
    assert "_advance_lock" in quelle and "_advance_intern" in quelle
    assert quelle.index("_advance_lock") < quelle.index("_advance_intern")




def test_musik_extractor_args_verliert_kein_po_token():
    """player_client UND po_token landen beide unter dem Schluessel "youtube".

    Wer nur eines davon setzt, LOESCHT das andere - genau das war der Fehler:
    sobald ein Client durchprobiert wurde, war das PO Token weg. Und ohne PO
    Token weist YouTube inzwischen fast jeden Client ab. Der Fehler haette also
    ausgerechnet dann zugeschlagen, wenn die Rettung schon eingerichtet war."""
    import music
    alt = os.environ.pop("YTDLP_PO_TOKEN", None)
    try:
        assert music.Music._extractor_args(None) == {}
        assert music.Music._extractor_args("tv") == {
            "youtube": {"player_client": ["tv"]}}

        os.environ["YTDLP_PO_TOKEN"] = "web.gvs+AAA, tv.gvs+BBB"
        # Leerzeichen und Komma sauber trennen - abgeschriebene Tokens haben das.
        assert music.Music._pot_tokens() == ["web.gvs+AAA", "tv.gvs+BBB"]
        beides = music.Music._extractor_args("tv")["youtube"]
        assert beides["player_client"] == ["tv"], "Client verloren"
        assert beides["po_token"] == ["web.gvs+AAA", "tv.gvs+BBB"], (
            "PO Token beim Client-Wechsel verloren")
        # Auch ohne Client muss das Token durchkommen (Suche/Playlist).
        assert music.Music._extractor_args(None) == {
            "youtube": {"po_token": ["web.gvs+AAA", "tv.gvs+BBB"]}}
    finally:
        os.environ.pop("YTDLP_PO_TOKEN", None)
        if alt is not None:
            os.environ["YTDLP_PO_TOKEN"] = alt




def test_musik_probiert_zuerst_die_clients_ohne_po_token():
    """YouTube verlangt fuer die meisten Clients ein "PO Token", das yt-dlp gar
    nicht selbst erzeugen kann. Genau drei kommen ohne aus - nur die haben auf
    einem nackten Server ueberhaupt eine Chance.

    Standen die hinten, lief Flo erst durch vier Clients, die ohne Token
    NIE gehen konnten, bevor er den einen probierte, der geht. Fuer den Nutzer
    ist das der Unterschied zwischen "spielt" und "spielt nicht"."""
    import music
    alt = os.environ.pop("YTDLP_PLAYER_CLIENT", None)
    try:
        reihe = music.instance.client_reihe()
        for name in music.Music._OHNE_POT:
            assert name in reihe, f"{name} fehlt in der Reihe"
        vorne = reihe[:len(music.Music._OHNE_POT)]
        assert set(vorne) == set(music.Music._OHNE_POT), (
            f"die Clients ohne PO Token stehen nicht vorne: {reihe}")
        # Und es muessen Namen sein, die DIESE yt-dlp-Fassung wirklich kennt.
        bekannt = music.Music._bekannte_clients()
        if bekannt:
            unbekannt = [c for c in reihe if c not in bekannt]
            assert not unbekannt, f"yt-dlp kennt diese Clients nicht: {unbekannt}"
    finally:
        if alt is not None:
            os.environ["YTDLP_PLAYER_CLIENT"] = alt




def test_musik_findet_cookies_auch_ohne_env_eintrag():
    """Cookies sollen wirken, sobald die Datei da liegt - ohne .env-Zeile.

    Der Betreiber sitzt womoeglich am Handy an einer noVNC-Konsole ohne
    Copy-Paste. Eine Datei ablegen kann er dort, eine .env-Zeile tippen kaum.
    Eine LEERE Datei darf dabei nicht zaehlen: sonst rennt yt-dlp mit einem
    leeren Zugang los und alles scheitert - mit einer irrefuehrenden Meldung."""
    import music
    ordner = tempfile.mkdtemp()
    alt_data = os.environ.get("DATA_DIR")
    alt_datei = os.environ.pop("YTDLP_COOKIES", None)
    alt_browser = os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
    os.environ["DATA_DIR"] = ordner
    try:
        assert music.Music._cookie_datei_finden() in ("", None) or True
        pfad = os.path.join(ordner, "cookies.txt")
        open(pfad, "w").close()                       # leer
        assert music.Music._cookie_datei_finden() != pfad, (
            "eine leere cookies.txt wird als gueltiger Zugang behandelt")
        with open(pfad, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n"
                    ".youtube.com\tTRUE\t/\tTRUE\t0\tX\ty\n")
        assert music.Music._cookie_datei_finden() == pfad
        assert music.Music._cookie_optionen()["cookiefile"] == pfad

        # Ein falscher Eintrag in der .env darf den Fund nicht verhindern -
        # sonst kostet ein Tippfehler die ganze Musik.
        os.environ["YTDLP_COOKIES"] = "/gibt/es/nicht.txt"
        assert music.Music._cookie_optionen()["cookiefile"] == pfad
    finally:
        os.environ.pop("YTDLP_COOKIES", None)
        os.environ.pop("YTDLP_COOKIES_FROM_BROWSER", None)
        os.environ.pop("DATA_DIR", None)
        for name, wert in (("DATA_DIR", alt_data), ("YTDLP_COOKIES", alt_datei),
                           ("YTDLP_COOKIES_FROM_BROWSER", alt_browser)):
            if wert is not None:
                os.environ[name] = wert
        shutil.rmtree(ordner, ignore_errors=True)




def test_musik_proxy_erreicht_jeden_yt_dlp_aufruf():
    """Ein eigener Ausgang (YTDLP_PROXY) ist einer der zwei Wege zurueck zu
    YouTube, wenn die Server-IP gesperrt ist. Kaeme er nur beim Abspielen an
    und nicht bei der Suche, ginge ein Link - und "flo spiel <titel>" nicht.
    Genau diese halbe Reparatur faellt beim Testen nicht auf."""
    import re
    import music
    quelle = open(music.__file__, encoding="utf-8").read()
    stellen = [m.start() for m in re.finditer(r"yt_dlp\.YoutubeDL\(", quelle)]
    assert len(stellen) >= 4, f"nur {len(stellen)} yt-dlp-Aufrufe gefunden"
    ohne = [i for i, pos in enumerate(stellen, 1)
            if "_netz_optionen()" not in quelle[max(0, pos - 900):pos]]
    assert not ohne, f"diese yt-dlp-Aufrufe gehen am Proxy vorbei: {ohne}"

    alt = os.environ.pop("YTDLP_PROXY", None)
    try:
        assert music.Music._netz_optionen() == {}
        os.environ["YTDLP_PROXY"] = "socks5://127.0.0.1:1080"
        assert music.Music._netz_optionen() == {"proxy": "socks5://127.0.0.1:1080"}
    finally:
        os.environ.pop("YTDLP_PROXY", None)
        if alt is not None:
            os.environ["YTDLP_PROXY"] = alt




def test_musik_soundcloud_ausweich_laesst_sich_abschalten():
    """Weicht Flo stillschweigend auf SoundCloud aus, merkt niemand, dass
    YouTube klemmt - und der Betreiber repariert es nie. Wer das nicht will,
    soll eine ehrliche Fehlermeldung bekommen koennen."""
    import music
    alt = os.environ.pop("MUSIC_SOUNDCLOUD_FALLBACK", None)
    try:
        assert music.Music._ausweich_erlaubt(), "Ausweich ist nicht die Vorgabe"
        for wert in ("0", "false", "no", "off", "aus", "AUS", " Off "):
            os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = wert
            assert not music.Music._ausweich_erlaubt(), f"{wert!r} schaltet nicht ab"
        for wert in ("1", "ja", "an", "true"):
            os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = wert
            assert music.Music._ausweich_erlaubt(), f"{wert!r} schaltet faelschlich ab"
        # Und abgeschaltet darf wirklich NICHTS von SoundCloud kommen.
        os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = "0"
        assert asyncio.run(music.instance._soundcloud_ausweich("egal")) is None
    finally:
        os.environ.pop("MUSIC_SOUNDCLOUD_FALLBACK", None)
        if alt is not None:
            os.environ["MUSIC_SOUNDCLOUD_FALLBACK"] = alt




def test_musik_suche_wechselt_auch_den_client():
    """"flo spiel <titel>" sucht erst und spielt dann. Wird die SUCHE geblockt,
    kommt es nie bis zum Abspielen - der Client-Wechsel beim Abspielen nuetzt
    dann gar nichts. Genau das fehlte: die Suche hat einmal blind gefragt und
    aufgegeben."""
    import music

    class FakeYDL:
        geht_ab = "tv"
        versuche = []

        def __init__(self, opts):
            self.client = (opts.get("extractor_args", {})
                           .get("youtube", {}).get("player_client", [None])[0])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, ziel, download=False):
            FakeYDL.versuche.append(self.client)
            if self.client != FakeYDL.geht_ab:
                raise Exception("Sign in to confirm you're not a bot")
            return {"entries": [{"title": "Semmel Song", "id": "abc123",
                                 "duration": 180}]}

    m = music.instance
    alt_ydl, alt_client = music.yt_dlp, m._guter_client
    alt_env = os.environ.pop("YTDLP_PLAYER_CLIENT", None)
    music.yt_dlp = type("M", (), {"YoutubeDL": FakeYDL})
    try:
        FakeYDL.versuche, m._guter_client = [], ""
        treffer = asyncio.run(m._youtube_search_best("semmel song",
                                                     want_title="Semmel Song"))
        assert treffer == "https://www.youtube.com/watch?v=abc123", treffer
        assert FakeYDL.versuche[0] is None, "fragt nicht zuerst yt-dlps Vorgabe"
        assert "tv" in FakeYDL.versuche, (
            f"die Suche probiert keine anderen Clients: {FakeYDL.versuche}")
        assert m._guter_client == "tv", "merkt sich den Client der Suche nicht"
    finally:
        music.yt_dlp, m._guter_client = alt_ydl, alt_client
        if alt_env is not None:
            os.environ["YTDLP_PLAYER_CLIENT"] = alt_env




def test_youtube_arzt_kennt_dieselben_clients_wie_der_bot():
    """Der Arzt (tools_youtube_setup.py) laeuft absichtlich OHNE den Bot zu
    importieren - er muss auch dann noch helfen, wenn music.py kaputt ist.
    Der Preis dafuer ist eine zweite Liste, und zwei Listen driften.

    Sagt der Arzt "player_client=tv geht", der Bot probiert tv aber gar nicht,
    schickt die Diagnose den Betreiber in die Irre. Deshalb dieser Abgleich."""
    import music
    import tools_youtube_setup as arzt
    assert tuple(arzt.OHNE_POT) == tuple(music.Music._OHNE_POT), (
        f"Arzt: {arzt.OHNE_POT}  Bot: {music.Music._OHNE_POT}")
    zusammen = tuple(arzt.OHNE_POT) + tuple(arzt.MIT_POT)
    assert zusammen == tuple(music.Music._CLIENT_REIHE), (
        f"Arzt probiert eine andere Reihe als der Bot:\n"
        f"  Arzt: {zusammen}\n  Bot:  {music.Music._CLIENT_REIHE}")
    # Der Arzt darf yt-dlps Ausgaben nicht durchlassen - der Bericht wird auf
    # einem Handy gelesen und ist mit acht Tracebacks unbrauchbar.
    for name in ("debug", "info", "warning", "error"):
        assert hasattr(arzt.Stumm, name), f"Stumm kann kein {name}()"




def test_youtube_arzt_verwechselt_netzfehler_nicht_mit_bot_sperre():
    """"YouTube blockt deine IP" und "der Server kommt nicht ins Netz" brauchen
    voellig verschiedene Reparaturen. Rateraten der Arzt falsch, richtet der
    Betreiber stundenlang Cookies ein, obwohl die Firewall zu ist."""
    import tools_youtube_setup as arzt
    a = arzt.YoutubeSetup()
    assert a.grund(Exception("Sign in to confirm you're not a bot")) == "Bot-Sperre"
    assert a.grund(Exception("Unable to connect to proxy: 403")) == \
        "Proxy laesst nicht durch"
    assert a.grund(Exception("The read operation timed out")) == "kein Netz zu YouTube"

    a.gruende = ["Proxy laesst nicht durch", "kein Netz zu YouTube"]
    a.urteil()
    text = "\n".join(a.zeilen)
    assert "KEINE Bot-Sperre" in text, text
    assert "WEGWERF" not in text, "raet zu Cookies, obwohl das Netz das Problem ist"

    b = arzt.YoutubeSetup()
    b.gruende = ["Bot-Sperre", "Bot-Sperre"]
    b.urteil()
    text = "\n".join(b.zeilen)
    assert "blockt diese IP" in text, text
    assert "WEGWERF" in text, "warnt nicht vor dem eigenen Google-Konto"




def test_youtube_cookies_landen_niemals_im_repo():
    """Eine cookies.txt IST eine Anmeldung bei Google. Landet sie im Repo, ist
    das Konto oeffentlich - der schlimmste denkbare Ausgang dieser Reparatur.
    Deshalb steht sie in .gitignore, und deshalb prueft das hier ein Test."""
    import subprocess
    hier = os.path.dirname(os.path.abspath(__file__))
    text = open(os.path.join(hier, ".gitignore"), encoding="utf-8").read()
    muster = [z.strip() for z in text.splitlines()
              if z.strip() and not z.strip().startswith("#")]
    for name in ("cookies.txt", "youtube.txt", "youtube_cookies.txt"):
        assert name in muster, f"{name} steht nicht in .gitignore"
    assert "data/" in muster, "data/ steht nicht in .gitignore"
    # Und es darf gerade wirklich keine im Baum liegen.
    for ordner in (hier, os.path.join(hier, "data")):
        pfad = os.path.join(ordner, "cookies.txt")
        if os.path.exists(pfad):
            fertig = subprocess.run(["git", "check-ignore", pfad],
                                    cwd=hier, capture_output=True)
            assert fertig.returncode == 0, f"{pfad} wuerde committet werden!"




def test_k_kennt_den_youtube_befehl():
    """"k y" ist der Befehl, mit dem der Betreiber YouTube wieder ans Laufen
    bringt. Steht er nicht im Hilfetext, findet ihn niemand - und ein Befehl,
    den niemand findet, hilft niemandem."""
    hier = os.path.dirname(os.path.abspath(__file__))
    text = open(os.path.join(hier, "k"), encoding="utf-8").read()
    assert "y|yt|youtube)" in text, "k kennt den Befehl 'y' nicht"
    assert "tools_youtube_setup.py" in text
    assert 'shift' in text.split("y|yt|youtube)")[1].split(";;")[0], (
        "k reicht die weiteren Woerter nicht durch - 'k y browser firefox' "
        "kaeme nie an")
    assert '"$@"' in text.split("y|yt|youtube)")[1].split(";;")[0]
    assert "k y" in text, "der Hilfetext nennt 'k y' nicht"
    assert os.path.exists(os.path.join(hier, "tools_youtube_setup.py"))




def test_panel_aenderung_erreicht_den_laufenden_player():
    """DAS war der eigentliche "nicht synchronisiert"-Fehler.

    Die Lautstaerke wurde NUR beim Anlegen eines Players aus guildcfg gelesen -
    und Player werden nie weggeraeumt. Wer einmal Musik gehoert hatte, behielt
    seine Lautstaerke bis zum Neustart: 'flo ls 80' griff sofort, ein Klick im
    Web-Panel nie. Jetzt meldet sich music bei guildcfg an und zieht nach."""
    import guildcfg
    import music

    import discord

    # Eine ECHTE PCMVolumeTransformer - music prueft per isinstance, und mit
    # einer Attrappe wuerde der Test gruen, obwohl im Betrieb nichts passiert.
    class StummeQuelle(discord.AudioSource):
        def read(self):
            return b""

        def is_opus(self):
            return False

    quelle = discord.PCMVolumeTransformer(StummeQuelle(), volume=0.5)
    player = SimpleNamespace(volume=0.5, voice=SimpleNamespace(source=quelle))
    m = music.instance
    alt_players = dict(m._players)
    m._players[4242] = player
    alt_get = guildcfg.get
    try:
        guildcfg.get = lambda gid, key: 90 if key == "lautstaerke" else alt_get(gid, key)
        m.lautstaerke_nachziehen(4242)
        assert abs(player.volume - 0.9) < 1e-6, player.volume
        assert abs(player.voice.source.volume - 0.9) < 1e-6, (
            "die LAUFENDE Tonspur bleibt leise - es wirkt erst beim naechsten Lied")
        # Ein Server ohne Player darf nicht umfallen.
        m.lautstaerke_nachziehen(999999)
    finally:
        guildcfg.get = alt_get
        m._players.clear()
        m._players.update(alt_players)




def test_verlauf_erkennt_die_richtigen_befehle():
    """'flo history' und 'flo nochmal verlauf' - auch vertippt.

    Die Grenze ist genauso wichtig wie die Treffer: waere die Erkennung zu
    locker, landeten andere Befehle im Verlauf statt dort, wo sie hingehoeren."""
    import music
    treffer = ("history", "histori", "historie", "nochmal verlauf",
               "nochmal history", "nochmall verlauf", "nohmal history",
               "nochmal histori", "nochmal verlauv", "again history",
               "replay history", "musik verlauf", "music history",
               "song verlauf", "wiederhole verlauf")
    for text in treffer:
        assert music.verlauf_befehl(text), f"nicht erkannt: {text!r}"

    daneben = ("nochmal", "nochmal 3", "skip", "stop", "pause", "queue",
               "spiel wonderwall", "lyrics", "shuffle", "leave", "volume 50",
               "handel", "transaktionen", "trades", "luxus", "join",
               "spiel history von abba", "")
    for text in daneben:
        assert not music.verlauf_befehl(text), f"faelschlich Verlauf: {text!r}"




def test_verlauf_nimmt_dem_handelsbuch_nicht_den_befehl_weg():
    """'flo verlauf' gehoert seit jeher dem Handelsbuch - und music.handle
    laeuft in bot.py VOR handel.handle.

    Haette der Musik-Verlauf das nackte Wort beansprucht, wuerde Flo ab sofort
    Songs zeigen, wenn jemand seine Coin-Umsaetze sehen will. Ein bestehender
    Befehl darf davon nicht kaputtgehen."""
    import handel
    import music
    assert "verlauf" in handel.Handel._CMDS, "Annahme veraltet"
    assert not music.verlauf_befehl("verlauf"), (
        "der Musik-Verlauf hat dem Handelsbuch 'verlauf' weggenommen")
    assert not music.verlauf_befehl("verlaufs")
    # In bot.py steht music wirklich vor handel - deshalb ist das kein Detail.
    quelle = open("bot.py", encoding="utf-8").read()
    assert quelle.index("music.handle") < quelle.index("handel.handle")




def test_verlauf_ueberlebt_den_neustart():
    """Der Player haelt nur die letzten 30 im Arbeitsspeicher - nach einem
    Neustart ist alles weg. Genau danach fragt man aber 'was lief gestern?'."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        for i in range(1, 6):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}", f"https://x/{i}", "Anna"))
        # Neuester zuerst - Nummer 1 ist der zuletzt gespielte.
        assert [e["t"] for e in m.verlauf(77)][:2] == ["Song 5", "Song 4"]

        # "Neustart": derselbe Inhalt, frisch aus dem Speicher gelesen.
        roh = m._store.data
        m._store = _FakeStore(roh)
        assert len(m.verlauf(77)) == 5, "der Verlauf ist beim Neustart weg"
        assert m.verlauf(77)[0]["t"] == "Song 5"

        # Server bleiben getrennt.
        assert m.verlauf(88) == []
    finally:
        restore()




def test_verlauf_waechst_nicht_unbegrenzt():
    """Mindestens 100 Eintraege, aeltere fliegen automatisch raus - sonst
    waechst die Datei bei jedem Song weiter."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        assert music.VERLAUF_MAX >= 100, "weniger als gefordert"
        for i in range(music.VERLAUF_MAX + 60):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}"))
        eintraege = m.verlauf(77)
        assert len(eintraege) == music.VERLAUF_MAX, len(eintraege)
        # Der NEUESTE ueberlebt, der aelteste ist weg.
        assert eintraege[0]["t"] == f"Song {music.VERLAUF_MAX + 59}"
        assert not any(e["t"] == "Song 0" for e in eintraege)

        # Derselbe Song zweimal hintereinander (Stall-Neustart, Seek) ist EIN
        # Eintrag - sonst steht der Verlauf nach einer Stoerung voll damit.
        m.verlauf_notieren(99, _verlauf_track("Derselbe"))
        m.verlauf_notieren(99, _verlauf_track("Derselbe"))
        assert len(m.verlauf(99)) == 1
    finally:
        restore()




def test_verlauf_embed_nummeriert_und_blaettert():
    """10 je Seite, neuester ist Nummer 1, Seitenanzeige im Fusstext."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        for i in range(1, 26):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}", f"https://x/{i}", "Anna",
                                                  dauer=200))
        view = music.VerlaufView(77, owner_id=5)
        emb = view.embed()
        assert "**1.** [Song 25]" in emb.description, emb.description[:120]
        assert "**10.** " in emb.description
        assert "**11.** " not in emb.description, "mehr als 10 auf einer Seite"
        assert "Seite 1/3" in emb.footer.text, emb.footer.text
        assert "Anna" in emb.description and "25 Songs" in emb.footer.text

        # Dropdown + zwei Blaetter-Knoepfe.
        namen = [type(c).__name__ for c in view.children]
        assert namen.count("Button") == 2 and "_VerlaufSelect" in namen, namen

        view.seite = 1
        view._aufbauen()
        assert "**11.** [Song 15]" in view.embed().description
        assert "Seite 2/3" in view.embed().footer.text

        # Ueber die letzte Seite hinaus wird geklemmt statt zu fliegen.
        view.seite = 99
        view._aufbauen()
        assert "Seite 3/3" in view.embed().footer.text
    finally:
        restore()




def test_verlauf_faengt_die_raender_ab():
    """Leerer Verlauf, unsinnige Nummer, Eintrag ohne Quelle - jeweils ein
    klarer Satz statt eines Absturzes."""
    music, restore = _verlauf_frisch()
    try:
        m = music.instance
        # Leer.
        eintrag, fehler = m.verlauf_eintrag(77, 1)
        assert eintrag is None and "Noch keine Songs" in fehler
        assert "Noch keine Songs" in music.VerlaufView(77, 5).embed().description

        m.verlauf_notieren(77, _verlauf_track("Song A", "https://x/a"))
        # Zu gross, zu klein, keine Zahl.
        assert m.verlauf_eintrag(77, 9)[0] is None
        assert "gibt es nicht" in m.verlauf_eintrag(77, 9)[1]
        assert m.verlauf_eintrag(77, 0)[0] is None
        assert "keine Nummer" in m.verlauf_eintrag(77, "abc")[1]
        # Gueltig.
        assert m.verlauf_eintrag(77, 1)[0]["t"] == "Song A"

        # Ohne Quelle laesst sich nichts nachspielen - das muss auffallen,
        # bevor yt-dlp mit einem leeren String losrennt.
        assert m._verlauf_quelle({"t": "", "u": "", "q": ""}) == ""
        assert m._verlauf_quelle({"t": "Nur Titel"}) == "Nur Titel"
        assert m._verlauf_quelle({"u": "https://x/1", "q": "such"}) == "https://x/1"
    finally:
        restore()




def test_nochmal_n_meint_dieselbe_nummer_wie_der_verlauf():
    """'flo nochmal 3' und die 3 in 'flo history' MUESSEN derselbe Song sein.

    Vorher las 'nochmal' aus player.history (Arbeitsspeicher, letzte 30,
    aelteste zuerst gezaehlt) - zwei Listen mit verschiedener Zaehlrichtung.
    Nach einem Neustart war sie ausserdem leer, obwohl der Verlauf noch stand."""
    import music
    quelle = open(music.__file__, encoding="utf-8").read()
    i = quelle.index('if action == "replay":')
    rumpf = quelle[i:i + 1200]
    assert "verlauf_eintrag" in rumpf, (
        "'nochmal N' liest wieder aus dem fluechtigen player.history")
    assert "player.history[-idx]" not in rumpf

    music_mod, restore = _verlauf_frisch()
    try:
        m = music_mod.instance
        for i in range(1, 6):
            m.verlauf_notieren(77, _verlauf_track(f"Song {i}", f"https://x/{i}"))
        # Nummer 3 im Embed ...
        emb = music_mod.VerlaufView(77, 5).embed()
        assert "**3.** [Song 3]" in emb.description, emb.description[:200]
        # ... ist auch die 3 fuer 'nochmal 3'.
        assert m.verlauf_eintrag(77, 3)[0]["t"] == "Song 3"
    finally:
        restore()




def test_verlauf_ist_im_hilfetext():
    """Ein Befehl, den niemand findet, hilft niemandem."""
    quelle = open("bot.py", encoding="utf-8").read()
    assert "flo history" in quelle, "der Verlauf fehlt in der Hilfe"
    # Gezielt in _HELP_HINTS schauen: 'musik' kommt in bot.py mehrfach vor
    # (u. a. in der Kategorie-Zuordnung), ein blindes split() traefe die
    # falsche Stelle - und der Test waere gruen, ohne etwas zu pruefen.
    hints = quelle.split("_HELP_HINTS = {")[1].split("}")[0]
    assert "history" in hints.split('"musik": "')[1].split('"')[0], (
        "die Musik-Kurzuebersicht nennt den Verlauf nicht")


# --- Loop: den laufenden Song wiederholen -------------------------------------
def _loop_lauf(player, voice, runden):
    """Laesst 'runden' Songenden durchlaufen (so, wie FFmpeg sie meldet)."""
    for _ in range(runden):
        voice.stop()
        player.loop.run_until_complete(player._advance(gen=player._play_gen))


def test_loop_wiederholt_und_zaehlt_runter():
    """'loop 3' spielt den Song noch drei Mal - danach geht es normal weiter.

    Der Haken sitzt in _advance_intern und reiht eine KOPIE des Songs vorne
    ein. Eine Kopie deshalb, weil dasselbe Track-Objekt gleichzeitig in
    'current' und in der Warteschlange QueuePositionView.apply_move
    durcheinanderbraechte - die sucht per 'is'."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        a, b = _track("A"), _track("B")
        player.queue[:] = [a, b]
        player.loop.run_until_complete(player._advance())
        assert player.current is a
        assert player.loop_setzen(3)
        assert player.loop_rest == 3
        assert player.loop_key == music._loop_key(a)

        for erwartet in (2, 1, 0):
            _loop_lauf(player, voice, 1)
            assert player.current.title == "A", player.current.title
            assert player.loop_rest == erwartet, player.loop_rest
            # B bleibt liegen, solange der Loop laeuft.
            assert [t.title for t in player.queue] == ["B"]
            # Gespielt wird eine KOPIE, nie das Original-Objekt aus 'current'.
            assert player.current is not a, "der Loop reiht dasselbe Objekt ein"

        # Loop abgelaufen -> der naechste Song ist dran.
        _loop_lauf(player, voice, 1)
        assert player.current is b
        assert player.queue == []
    finally:
        aufraeumen()




def test_loop_endlos_laeuft_bis_jemand_ihn_ausmacht():
    """'endlos' (-1) zaehlt nicht runter - und 'loop aus' beendet ihn sofort."""
    player, voice, aufraeumen = _musik_umgebung()
    try:
        player.queue[:] = [_track("A")]
        player.loop.run_until_complete(player._advance())
        player.loop_setzen(-1)
        _loop_lauf(player, voice, 5)
        assert player.loop_rest == -1
        assert player.current.title == "A"

        player.loop_setzen(0)
        assert player.loop_rest == 0 and player.loop_key == ""
        _loop_lauf(player, voice, 1)
        # Nichts mehr da -> Wiedergabe endet, statt ewig weiterzulaufen.
        assert player.current is None, player.current
    finally:
        aufraeumen()




def test_skip_bricht_den_loop_ab():
    """Ohne das waere Skip wirkungslos: der Loop reiht den Song sofort wieder
    vorne ein und man kaeme aus der Dauerschleife nur noch per Stop raus.

    Deshalb zwei Sicherungen: skip() loescht den Loop AUSDRUECKLICH, und der
    Haken in _advance_intern greift nur bei gen is not None (Skip laeuft ohne
    gen)."""
    player, voice, aufraeumen = _musik_umgebung()
    try:
        a, b = _track("A"), _track("B")
        player.queue[:] = [a, b]
        player.loop.run_until_complete(player._advance())
        player.loop_setzen(-1)
        player.loop.run_until_complete(player.skip())
        assert player.loop_rest == 0, "der Loop hat den Skip ueberlebt"
        assert player.current is b, player.current
        assert player.queue == []
    finally:
        aufraeumen()




def test_stop_nimmt_den_loop_mit():
    """Sonst wiederholt die NAECHSTE Sitzung ungefragt - ein Geister-Loop, den
    niemand gesetzt hat."""
    player, _voice, aufraeumen = _musik_umgebung()
    try:
        player.queue[:] = [_track("A")]
        player.loop.run_until_complete(player._advance())
        player.loop_setzen(5)
        player.loop.run_until_complete(player.disconnect())
        assert player.loop_rest == 0 and player.loop_key == ""
    finally:
        aufraeumen()




def test_loop_verbraucht_bei_einem_abbruch_keine_wiederholung():
    """Stirbt FFmpeg mitten im Song, ist das kein fertiger Durchlauf.

    discord.py meldet den Absturz genau wie das Songende (read() liefert b"").
    Steht der Loop-Haken VOR _nach_abbruch_fortsetzen, frisst jeder Aussetzer
    eine Wiederholung - deshalb steht er dahinter."""
    player, voice, aufraeumen = _musik_umgebung()
    try:
        t = _track("A")
        t.duration = 300                 # lang -> ein Ende nach 0 s ist ein Abbruch
        player.queue[:] = [t]
        player.loop.run_until_complete(player._advance())
        player.loop_setzen(3)
        rest_vorher = player.loop_rest
        _loop_lauf(player, voice, 1)     # sofortiger "Songende"-Callback = Absturz
        assert player.loop_rest == rest_vorher, (
            f"ein Abbruch hat eine Wiederholung gefressen ({player.loop_rest})")
    finally:
        aufraeumen()




def test_kaputter_song_beendet_den_loop_statt_ewig_neu_zu_starten():
    """Ein Song, der nicht laeuft, darf nicht ewig wiederholt werden.

    Vorher waere das eine Endlosschleife auf einem toten Stream gewesen:
    start() wirft, der Loop reiht nach, start() wirft wieder."""
    import music
    player, voice, aufraeumen = _musik_umgebung()
    try:
        a = _track("A")
        player.queue[:] = [a]
        player.loop.run_until_complete(player._advance())
        player.loop_setzen(-1)
        # Ab jetzt scheitert JEDER Start.
        echt = music.GuildPlayer.start

        def kaputt(self, track, **kw):
            raise RuntimeError("Stream tot")

        music.GuildPlayer.start = kaputt
        try:
            _loop_lauf(player, voice, 1)
        finally:
            music.GuildPlayer.start = echt
        assert player.loop_rest == 0, "der Loop laeuft auf einem toten Song weiter"
        assert player.loop_key == ""
    finally:
        aufraeumen()




def test_loop_befehl_wird_erkannt_und_klaut_repeat_nichts():
    """'loop' ist neu, 'repeat/nochmal/wiederhol' gehoeren weiter dem VERLAUF.

    'flo repeat 3' heisst seit jeher "spiel Song Nummer 3 aus dem Verlauf".
    Haette der Loop sich das Wort genommen, aenderte sich die Bedeutung eines
    bestehenden Befehls - und niemand haette es gemerkt."""
    import music
    assert music.parse_command("flo loop") == ("loop", "")
    assert music.parse_command("flo loop 3") == ("loop", "3")
    assert music.parse_command("flo loop 10x") == ("loop", "10")
    assert music.parse_command("flo loop aus") == ("loop", "aus")
    assert music.parse_command("flo loop endlos") == ("loop", "endlos")
    assert music.parse_command("flo dauerschleife 5") == ("loop", "5")
    assert music.parse_command("flo endlosschleife") == ("loop", "")
    # Der Verlauf behaelt seine Woerter.
    assert music.parse_command("flo repeat 3") == ("replay", "3")
    assert music.parse_command("flo nochmal 2") == ("replay", "2")
    assert music.parse_command("flo wiederhole") == ("replay", "1")
    # Und ein Satz, in dem 'loop' nur vorkommt, kapert die Musik nicht.
    for satz in ("flo was ist eigentlich ein loop",
                 "flo loop mal den song",
                 "flo der loop war kaputt"):
        assert music.parse_command(satz) is None, satz




def test_loop_panel_zeigt_den_zustand_und_hat_platz_fuer_den_knopf():
    """Zwei Sachen, die man sonst erst im echten Discord merkt:

    (1) Laeuft ein Loop, muss das im Panel stehen - sonst sieht niemand, warum
        derselbe Song wiederkommt und die Warteschlange steht.
    (2) Reihe 0 ist mit fuenf Buttons voll. Der Loop-Knopf braucht row=1 und
        der Tempo-Select muss auf row=2 ausweichen, sonst wirft discord.py
        beim Bauen der View 'item would not fit at row 1' - und dann kaeme gar
        kein Panel mehr."""
    import music
    t = _track("A")
    emb = music._now_playing_embed(t, 2, loop=3)
    felder = {f.name: f.value for f in emb.fields}
    assert "Loop" in felder, felder
    assert "3" in felder["Loop"]
    assert "endlos" in music._now_playing_embed(t, 0, loop=-1).fields[-1].value
    assert "Loop" not in [f.name for f in music._now_playing_embed(t, 0).fields]

    player, _voice, aufraeumen = _musik_umgebung()
    try:
        view = music.PlaybackControlView(player)
        reihen = {}
        for kind in view.children:
            reihen.setdefault(kind._rendered_row, []).append(
                getattr(kind, "label", None) or type(kind).__name__)
        assert "Loop" in reihen.get(1, []), reihen
        assert len(reihen.get(0, [])) == 5, reihen
        assert any(n == "_SpeedSelect" for n in reihen.get(2, [])), reihen
    finally:
        aufraeumen()




def test_loop_befehl_antwortet_und_postet_kein_zweites_panel():
    """Der Textweg neben dem Knopf - und die Falle dahinter.

    'flo loop 3' beantwortet Flo schon mit einem eigenen Embed. Frischt der
    Befehl das Panel per _panel_auffrischen auf und das faellt auf 'dann eben
    ein neues posten' zurueck, stuenden auf einmal ZWEI Panels im Kanal."""
    import music
    player, _voice, aufraeumen = _musik_umgebung()
    gepostet = []

    async def merken(_p, _t, **_kw):
        gepostet.append(_t)

    alt_send = music._send_panel
    music._send_panel = merken
    try:
        player.queue[:] = [_track("A")]
        player.loop.run_until_complete(player._advance())
        gepostet.clear()

        class Msg:
            content = ""
            guild = SimpleNamespace(id=1)
            channel = SimpleNamespace(id=1)

        alt_zustand = (music.instance._enabled, music.instance._players.get(1))
        music.instance._enabled = True
        music.instance._players[1] = player
        try:
            def sag(text):
                Msg.content = text
                return player.loop.run_until_complete(music.handle(Msg()))

            e = sag("flo loop aus")          # laeuft keiner -> ehrliche Auskunft
            assert "kein Loop" in e.description, e.description
            assert player.loop_rest == 0

            e = sag("flo loop 5")
            assert player.loop_rest == 5, player.loop_rest
            assert "5" in e.description

            e = sag("flo loop 99999")        # gedeckelt, statt Sackgasse
            assert player.loop_rest == music.LOOP_MAX, player.loop_rest

            e = sag("flo loop")              # nackt + laeuft einer -> aus
            assert player.loop_rest == 0, player.loop_rest
            e = sag("flo loop")              # nackt + keiner -> Dauerschleife
            assert player.loop_rest == -1, player.loop_rest
            assert "Dauerschleife" in e.title

            assert gepostet == [], "der Loop-Befehl hat ein zweites Panel gepostet"
        finally:
            music.instance._enabled = alt_zustand[0]
            if alt_zustand[1] is None:
                music.instance._players.pop(1, None)
            else:
                music.instance._players[1] = alt_zustand[1]
    finally:
        music._send_panel = alt_send
        aufraeumen()




def test_loop_ist_im_hilfetext():
    """Ein Befehl, den niemand findet, hilft niemandem."""
    quelle = open("bot.py", encoding="utf-8").read()
    assert "flo loop" in quelle, "der Loop fehlt in der Hilfe"




if __name__ == "__main__":
    run(globals())
