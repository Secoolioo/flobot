"""Die Werkzeuge selbst: Inventar, Abdruck, Testlauf, Speicher.

Teil der Flo-Testsuite. Gemeinsame Attrappen und Helfer liegen in
testhilfe.py; von dort kommt auch der umgebogene Datenordner.

    python lauf.py --nur werkzeug      nur diese Tests
"""

from testhilfe import *        # noqa: F401,F403 - Attrappen und Module
from testhilfe import (  # noqa: F401 - die privaten Helfer
    _FakeStore)



def test_attrappe_kann_alles_was_der_store_kann():
    """Die Test-Attrappe darf nicht hinter dem echten Store zurueckbleiben.

    Als store.JsonStore save_soon bekam, kippten auf einen Schlag elf Tests mit
    AttributeError um - die Attrappe kannte die Methode nicht. Der Fehler lag
    nicht im Bot, sondern in der Attrappe, und er kostet jedes Mal Zeit, bis man
    das gemerkt hat. Dieser Test sagt es sofort und beim Namen.
    """
    import store

    echt = {name for name in dir(store.JsonStore)
            if not name.startswith("_") and callable(getattr(store.JsonStore, name))}
    attrappe = {name for name in dir(_FakeStore) if not name.startswith("_")}
    fehlt = sorted(echt - attrappe)
    assert not fehlt, (
        f"_FakeStore fehlen Methoden, die store.JsonStore hat: {fehlt}. "
        f"Attrappe nachziehen, nicht den Store beschneiden.")

    # Und keine Testdatei baut sich ihre eigene, kleinere Attrappe. Vier Tests
    # hatten eine lokale 'class FakeStore' nur mit save(): sobald economy
    # gesammelt speichert (save_soon), platzte das Speichern - im Log stand
    # "Speichern nach dem Raub fehlgeschlagen", und der Test war trotzdem gruen.
    import ast
    import pathlib
    zu_klein = []
    for pfad in sorted(pathlib.Path(".").glob("test*.py")):
        for knoten in ast.walk(ast.parse(pfad.read_text(encoding="utf-8"))):
            if not (isinstance(knoten, ast.ClassDef) and "store" in knoten.name.lower()):
                continue
            kann = {k.name for k in knoten.body
                    if isinstance(k, (ast.FunctionDef, ast.AsyncFunctionDef))}
            if not {"save_soon", "flush_now"} <= kann:
                zu_klein.append(f"{pfad.name}:{knoten.lineno} class {knoten.name}")
    assert not zu_klein, (
        "Diese Store-Attrappen koennen weniger als der echte Store - "
        "testhilfe._FakeStore nehmen:\n  " + "\n  ".join(zu_klein))




def test_numfmt():
    """Deutsche Tausenderpunkte ab 1000; kleine/negative/Murks-Werte robust."""
    import numfmt
    assert numfmt.fmt(1000000) == "1.000.000"
    assert numfmt.fmt(2500) == "2.500"
    assert numfmt.fmt(-5000) == "-5.000"
    assert numfmt.fmt(999) == "999"
    assert numfmt.fmt(0) == "0"
    assert numfmt.fmt(1234567) == "1.234.567"




def test_speichern_meldet_fehlschlag():
    """save() verschluckte Schreibfehler (Platte voll) dauerhaft still."""
    import pathlib
    import tempfile
    import unittest.mock as mock
    import store
    alt = store.DATA_DIR
    store.DATA_DIR = pathlib.Path(tempfile.mkdtemp())
    try:
        s = store.JsonStore("s.json", default={"a": 1})
        assert asyncio.run(s.save()) is True
        with mock.patch("os.replace", side_effect=OSError(28, "No space left")):
            assert asyncio.run(s.save()) is False
        # Und es bleibt keine .tmp-Leiche liegen (sonst sammeln die sich genau
        # dann an, wenn die Platte ohnehin voll ist).
        assert not list(store.DATA_DIR.glob("s.json*.tmp"))
    finally:
        store.DATA_DIR = alt




def test_speichern_laesst_die_datei_nie_verschwinden():
    """Die Sicherung darf die Hauptdatei nicht kurz WEGnehmen.

    Frueher lief das per Rename: in dem Fenster gab es economy.json schlicht
    nicht. Wer da las (ein zweiter Store, das Panel, ein Reparatur-Skript),
    bekam ENOENT oder den veralteten .bak-Stand - und schlug das folgende
    Rename fehl, war die Hauptdatei dauerhaft weg."""
    import pathlib
    import tempfile
    import unittest.mock as mock
    import store
    alt = store.DATA_DIR
    store.DATA_DIR = pathlib.Path(tempfile.mkdtemp())
    try:
        s = store.JsonStore("w.json", default={"n": 0})
        s.data["n"] = 1
        assert asyncio.run(s.save()) is True
        s.data["n"] = 2
        gesehen = []

        # Mitten im Schreiben nachsehen, ob die Hauptdatei noch da ist.
        echtes_replace = os.replace

        def spion(a, b):
            gesehen.append((s.path.exists(), s.path.read_text(encoding="utf-8")
                            if s.path.exists() else ""))
            return echtes_replace(a, b)

        with mock.patch("os.replace", side_effect=spion):
            assert asyncio.run(s.save()) is True
        assert gesehen and gesehen[0][0] is True, gesehen
        assert '"n":1' in gesehen[0][1], gesehen[0][1]
        # Danach steht der neue Stand in der Datei und der alte in der Sicherung.
        assert '"n":2' in s.path.read_text(encoding="utf-8")
        assert '"n":1' in s._bak.read_text(encoding="utf-8")
    finally:
        store.DATA_DIR = alt




def test_beide_dateien_kaputt_wird_nichts_weggeworfen():
    """Hauptdatei UND Sicherung kaputt: BEIDE muessen beiseite.

    Vorher wurde nur die Hauptdatei quarantaeniert; die kaputte .bak blieb
    liegen und wurde vom zweiten save() unwiederbringlich ueberschrieben -
    genau das, was diese Klasse versprochen hat zu verhindern."""
    import pathlib
    import tempfile
    import store
    alt = store.DATA_DIR
    d = pathlib.Path(tempfile.mkdtemp())
    store.DATA_DIR = d
    try:
        (d / "k.json").write_text("{kaputt", encoding="utf-8")
        (d / "k.json.bak").write_text("auch kaputt", encoding="utf-8")
        s = store.JsonStore("k.json", default={"a": 1})
        assert s.data == {"a": 1}
        beiseite = sorted(p.name for p in d.glob("*.kaputt-*"))
        assert len(beiseite) == 2, beiseite
        # Zweimal speichern: das hat frueher die kaputte Sicherung gefressen.
        assert asyncio.run(s.save()) is True
        assert asyncio.run(s.save()) is True
        assert sorted(p.name for p in d.glob("*.kaputt-*")) == beiseite
    finally:
        store.DATA_DIR = alt




# --- Das Inventar: ist noch alles da? ---------------------------------------
def test_inventar_findet_ueberhaupt_etwas():
    """Die Wache am Werkzeug selbst.

    Das Inventar (werkzeug/inventar.py) soll beim Umbau sagen, was verloren
    gegangen ist. Es kann dabei auf eine besonders unangenehme Art versagen:
    es laeuft in einer kaputten Umgebung, findet fast nichts, und ab da ist
    jeder Vergleich trivial gruen - das Sicherheitsnetz haette ein Loch in
    genau der Groesse des Problems.

    Dieser Test laeuft ohne Probe (nur Quelltext, also schnell) und prueft, ob
    die gefundenen Mengen ueber den Untergrenzen liegen.
    """
    from werkzeug import inventar

    stand = inventar.Inventar(laut=False).aufnehmen(mit_probe=False)
    zu_wenig = inventar.untergrenze_pruefen(stand)
    assert not zu_wenig, (
        "Das Inventar findet zu wenig - das heisst fast immer, dass die "
        "Umgebung kaputt ist, nicht der Bot:\n  " + "\n  ".join(zu_wenig))
    # Und die Handler-Schleife in bot.py muss auffindbar bleiben: ohne sie
    # weiss das Inventar nicht mehr, wer eine Wort-Kollision gewinnt.
    quelle = inventar.Quelltext.hol(inventar.WURZEL / "bot.py")
    module = inventar.Reihenfolge(quelle).module()
    assert len(module) >= 20, f"nur {len(module)} Module in der Handler-Schleife"




def test_inventar_hat_nichts_verloren():
    """Der eigentliche Zweck: nach jedem Umbauschritt muss noch alles da sein.

    Laeuft als Unterprozess, weil die Probe alle Module hochfaehrt und
    einschaltet - das soll den uebrigen Tests nicht in die Quere kommen.

    Rueckgabecodes des Werkzeugs: 0 alles da, 1 nur angekuendigte Verluste
    (steht begruendet in inventar/erwartet.json), 2 echter Verlust,
    3 das Werkzeug selbst ist kaputt.
    """
    import subprocess

    wurzel = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(wurzel, "inventar", "stand.json")):
        return          # noch kein Grundstand aufgenommen - nichts zu pruefen
    lauf = subprocess.run(
        [sys.executable, os.path.join("werkzeug", "inventar.py"), "--vergleiche"],
        cwd=wurzel, capture_output=True, text=True, timeout=900)
    assert lauf.returncode in (0, 1), (
        f"Inventar meldet Code {lauf.returncode}:\n"
        + (lauf.stdout or "")[-3000:] + (lauf.stderr or "")[-1500:])




def test_lauf_kennt_jede_testdatei():
    """Der Waechter, den lauf.py in seinem Kopf versprochen hat.

    lauf.py fuehrt die Testdateien in einer Liste (TESTDATEIEN). Wenn beim
    Aufteilen der Suite eine neue Datei entsteht und niemand traegt sie ein,
    laufen ihre Tests einfach nicht - und der Lauf meldet trotzdem 'alles
    gruen'. Der Kommentar in lauf.py behauptete, ein Test halte die Liste gegen
    den Ordner. Den gab es nicht. Jetzt schon, und er prueft beide Richtungen.
    """
    import lauf

    wurzel = os.path.dirname(os.path.abspath(__file__))
    im_ordner = {p[:-3] for p in os.listdir(wurzel)
                 if p.startswith("test_") and p.endswith(".py")}
    in_liste = set(lauf.TESTDATEIEN)
    fehlt = sorted(im_ordner - in_liste)
    zuviel = sorted(in_liste - im_ordner)
    assert not fehlt, (
        f"Diese Testdateien stehen NICHT in lauf.TESTDATEIEN und laufen "
        f"deshalb nie mit: {fehlt}")
    assert not zuviel, (
        f"Diese Namen stehen in lauf.TESTDATEIEN, aber es gibt keine Datei "
        f"dazu: {zuviel}")




def test_lauf_wache_sieht_sendepause_und_funktions_schalter():
    """Die Wache in lauf.py kannte von admin nur _enabled. test_webpanel_api
    schaltete die Sendepause an und liess sie stehen - mit 'lauf.py --misch
    576651' fiel danach ein Umfrage-Test, weil bot.py jede Nachricht verwarf.
    Die Funktions-Schalter (features) werden an Ort und Stelle veraendert, das
    sieht ein 'is'-Vergleich nie. Und ("features", "_store") stand zwar in der
    Liste, gab es aber gar nicht - eine Wache, die nichts findet, schweigt."""
    import importlib
    import features
    import lauf
    for modulname, pfad in lauf.WACHE_TIEF + lauf.WACHE_INHALT:
        importlib.import_module(modulname)
        _wert, da = lauf.Lauf._hole(modulname, pfad)
        assert da, f"Wache {modulname}.{pfad} findet nichts - sie schweigt fuer immer"

    f = features.instance
    alt_lock = admin.instance._locked
    alt_inhalt = (set(f._disabled), {g: set(k) for g, k in f._per_guild.items()})
    schluessel = next(k["key"] for k in features.CATALOG if k["key"] not in f._disabled)
    vorher = lauf.Lauf._zustand()
    try:
        admin.instance._locked = not alt_lock
        f._disabled.add(schluessel)                          # an Ort und Stelle
        f._per_guild.setdefault(4711, set()).add(schluessel)
        verstellt = lauf.Lauf._zustand_zurueck(vorher)
        for name in ("admin.instance._locked", "features.instance._disabled",
                     "features.instance._per_guild"):
            assert name in verstellt, (name, verstellt)
        assert admin.instance._locked is alt_lock
        assert schluessel not in f._disabled and 4711 not in f._per_guild
        # Nichts verstellt -> nichts gemeldet (auch nicht durch die Kopien).
        assert lauf.Lauf._zustand_zurueck(lauf.Lauf._zustand()) == []
    finally:
        admin.instance._locked = alt_lock
        f._disabled, f._per_guild = alt_inhalt




def test_werkzeuge_lassen_keine_tasks_haengen():
    """Inventar und Abdruck schlossen ihren Event-Loop mit noch laufenden
    Hintergrund-Tasks der Handler (gesammeltes Speichern, Quiz-/Rate-Zeitlimits).
    Jeder davon kam am Ende als "Task was destroyed but it is pending!" ins Log
    - in jedem Werkzeuglauf, also auch in jedem Testlauf."""
    from werkzeug import abdruck, inventar
    aufgeraeumt = []

    async def zeitlimit():
        try:
            await asyncio.sleep(90)            # wie games._guess_timeout
        finally:
            aufgeraeumt.append(True)

    async def anlegen():
        return asyncio.get_running_loop().create_task(zeitlimit())

    loop = asyncio.new_event_loop()
    task = loop.run_until_complete(anlegen())
    assert not task.done()
    inventar.loop_schliessen(loop)
    assert loop.is_closed() and task.cancelled() and aufgeraeumt == [True]
    # Ein Loop ohne offene Tasks geht genauso zu.
    leer = asyncio.new_event_loop()
    inventar.loop_schliessen(leer)
    assert leer.is_closed()
    # Und BEIDE Werkzeuge schliessen ihren Loop auf diesem Weg.
    for quelle in (inspect.getsource(inventar.Probe.laufen),
                   inspect.getsource(abdruck.Abdrucknahme.aufnehmen)):
        assert "loop_schliessen(" in quelle and ".close()" not in quelle

    # Abdruck schloss seinen Loop erst NACH der Netzsperre - das Abbrechen der
    # Tasks lief also mit offenem Netz. Inventar macht es innerhalb; jetzt beide.
    import socket
    echt = inventar.loop_schliessen
    gesperrt = []

    def schliessen(lp):
        gesperrt.append(socket.getaddrinfo is inventar.KeinNetz._nein)
        echt(lp)

    nahme = abdruck.Abdrucknahme(laut=False)
    nahme.befehle = lambda: {}
    nahme.ruesten = lambda: {}
    nahme.durchlauf = lambda lp, worte: {}
    inventar.loop_schliessen = schliessen
    try:
        nahme.aufnehmen(runden=1)
    finally:
        inventar.loop_schliessen = echt
    assert gesperrt == [True], gesperrt
    assert socket.getaddrinfo is not inventar.KeinNetz._nein




def test_abdruck_flo_antwortet_noch_genauso():
    """Die Bedingung des Betreibers, nachpruefbar gemacht.

    Das Inventar sagt, WELCHE Befehle es gibt. Das reicht nicht: ein Modul kann
    nach dem Verschieben weiterhin auf 'flo level' reagieren und trotzdem eine
    andere Ueberschrift, andere Felder oder keine Knoepfe mehr schicken. Inventar
    gruen, Testlauf gruen - und im Discord sieht es anders aus.

    werkzeug/abdruck.py nimmt darum die FORM jeder Antwort auf (Typ, Titel,
    Feldnamen, Knopfbeschriftungen, Textgeruest) und vergleicht sie. Was nicht
    reproduzierbar ist (Wuerfel, Uhrzeit), hat das Werkzeug selbst gemessen und
    aussortiert - 472 von 480 Befehlen sind stabil.
    """
    import subprocess

    wurzel = os.path.dirname(os.path.abspath(__file__))
    if not os.path.exists(os.path.join(wurzel, "inventar", "abdruck.json")):
        return          # noch kein Abdruck aufgenommen
    lauf = subprocess.run(
        [sys.executable, os.path.join("werkzeug", "abdruck.py"), "--vergleiche",
         "--leise"],
        cwd=wurzel, capture_output=True, text=True, timeout=900)
    assert lauf.returncode == 0, (
        "Flo antwortet woanders anders als vorher:\n"
        + (lauf.stdout or "")[-4000:] + (lauf.stderr or "")[-1500:])



# --- Betrieb: Docker neben systemd ------------------------------------------------
def test_docker_bringt_keine_geheimnisse_ins_image():
    """.env (Discord-Token, KI-Schluessel), data/ (alle Konten) und die
    YouTube-Cookies (eine Google-Anmeldung) duerfen NIE in einer Image-Schicht
    landen - wer das Image bekommt, haette sie sonst alle."""
    wurzel = os.path.dirname(os.path.abspath(__file__))
    zeilen = {z.strip() for z in open(os.path.join(wurzel, ".dockerignore"),
                                       encoding="utf-8") if z.strip() and not z.startswith("#")}
    for muss in (".env", "data/", "cookies.txt", "youtube.txt", "youtube_cookies.txt",
                 "venv/", ".git/"):
        assert muss in zeilen, f".dockerignore laesst {muss} ins Image"

    datei = open(os.path.join(wurzel, "docker", "Dockerfile"), encoding="utf-8").read()
    # Gleiche Pfade wie systemd - sonst stimmen Pfade aus der .env nicht.
    assert "WORKDIR /opt/flobot" in datei
    # 'Flo restart' startet per os.execv neu, und execv sucht NICHT im PATH.
    assert 'CMD ["/usr/local/bin/python", "bot.py"]' in datei
    for paket in ("ffmpeg", "libopus0", "tzdata", "fonts-dejavu-core", "espeak-ng"):
        assert paket in datei, f"{paket} fehlt im Image"
    assert "FLO_LAUFZEIT=docker" in datei

    compose = open(os.path.join(wurzel, "docker", "compose.yaml"), encoding="utf-8").read()
    # Derselbe Datenordner wie der systemd-Dienst - nur dann greift die Sperre.
    assert "source: ../data" in compose and "target: /opt/flobot/data" in compose
    assert "source: ../.env" in compose
    assert "init: true" in compose            # SIGTERM kommt bei Flo an
    assert "network_mode: host" in compose    # keine Firewall-Umgehung durch ports:
    echte_zeilen = [z for z in compose.splitlines() if not z.strip().startswith("#")]
    assert not any(z.strip().startswith("ports:") for z in echte_zeilen)




def test_nur_ein_flo_gleichzeitig():
    """systemd UND Docker mit demselben Token waeren zwei Flos: doppelte
    Antworten, doppelte XP, Lotto zweimal gezogen - und die zwei Prozesse
    ueberschreiben sich gegenseitig die Daten. Die Sperre im Datenordner
    verhindert das, und zwar prozessuebergreifend."""
    import subprocess
    import sys
    import tempfile
    ordner = tempfile.mkdtemp(prefix="flobot-sperre-")
    wurzel = os.path.dirname(os.path.abspath(__file__))
    umgebung = dict(os.environ, DATA_DIR=ordner, FLO_LAUFZEIT="probe")
    code = ("import store, sys, time; ok, wer = store.einzelbetrieb_sichern(); "
            "print(ok, wer, flush=True); time.sleep(float(sys.argv[1]))")
    erster = subprocess.Popen([sys.executable, "-c", code, "4"], cwd=wurzel,
                              env=umgebung, stdout=subprocess.PIPE, text=True)
    try:
        assert erster.stdout.readline().startswith("True"), "der erste bekam die Sperre nicht"
        zweiter = subprocess.run([sys.executable, "-c", code, "0"], cwd=wurzel,
                                 env=umgebung, capture_output=True, text=True, timeout=30)
        assert zweiter.stdout.startswith("False"), zweiter.stdout
        assert "probe" in zweiter.stdout, "der zweite erfaehrt nicht, WER schon laeuft"
    finally:
        erster.wait(timeout=30)
    # Ist der erste weg, ist die Sperre frei.
    dritter = subprocess.run([sys.executable, "-c", code, "0"], cwd=wurzel,
                             env=umgebung, capture_output=True, text=True, timeout=30)
    assert dritter.stdout.startswith("True"), dritter.stdout




def test_env_kommt_vor_den_modulen_und_nie_im_testlauf():
    """Stand load_dotenv() hinter den Feature-Importen, lasen alle Module, die
    eine Einstellung schon beim Import holen, die .env nie (GUILD_ID -> Server-
    Icon und Aktie auf dem Hauptserver aus). Und ein Testlauf auf dem Server
    darf die echte .env nicht ziehen."""
    wurzel = os.path.dirname(os.path.abspath(__file__))
    quelle = open(os.path.join(wurzel, "bot.py"), encoding="utf-8").read()
    assert quelle.index("load_dotenv()") < quelle.index("\nimport admin"), (
        "die .env wird wieder erst nach den Modulen geladen")
    assert 'os.getenv("FLO_TESTLAUF", "") != "1"' in quelle
    assert os.environ.get("FLO_TESTLAUF") == "1"
    import bot
    # Beim Import (Tests, Werkzeuge) laufen die Startwachen NICHT - die sind nur
    # fuer den echten Dauerbetrieb.
    assert bot._DAUERBETRIEB is False




def test_beenden_sichert_alles_auch_schulden_und_handel():
    """SIGTERM (systemctl stop, docker stop) beendete Python bisher sofort -
    und 'flo restart' sicherte Schulden, Handel und Profil nicht. Wurde in
    deren 3-s-Sammelfenster neu gestartet, stand eine Tilgung schon in
    economy.json, der Posten aber noch offen in schulden.json: doppelt bezahlt.

    Gesichert wird seitdem in close() selbst - der Signal-Weg muss also durch
    das ECHTE close() laufen, abgeklemmt wird erst die Discord-Verbindung."""
    import ai
    import bot
    import discord
    import handel
    import schulden
    import webpanel
    from unittest import mock
    gesichert = []

    async def merke(name):
        gesichert.append(name)

    alt = (schulden.instance._store, getattr(schulden.instance, "_save", None),
           handel.instance._store)
    schulden.instance._store = _FakeStore({})
    schulden.instance._save = lambda: merke("schulden")
    handel.instance._store = SimpleNamespace(save=lambda: merke("handel"))
    geschlossen = []

    async def zu(_self):
        geschlossen.append(True)
        gesichert.append("discord")

    try:
        with mock.patch.object(discord.Client, "close", zu), \
                mock.patch.object(webpanel, "stop", lambda: merke("panel")), \
                mock.patch.object(ai, "aclose", lambda: merke("ki")):
            asyncio.run(bot.client._sauber_beenden("SIGTERM"))
            asyncio.run(bot.client._sauber_beenden("SIGTERM"))   # zweites Signal: nichts doppelt
    finally:
        (schulden.instance._store, schulden.instance._save,
         handel.instance._store) = alt
        bot.client._beendet_schon = False
        bot.client._schliessen = None
    assert "schulden" in gesichert and "handel" in gesichert, gesichert
    assert geschlossen == [True], geschlossen
    assert gesichert.count("panel") == 1 and gesichert.count("ki") == 1, gesichert
    # Und NACH dem Abmelden noch einmal alles - nicht nur economy. Eine Tilgung,
    # die beim Abmelden noch durchlief, stand sonst in economy.json, ihr Posten
    # in schulden.json hing aber im Sammel-Task, den asyncio.run abbricht.
    nachher = gesichert[gesichert.index("discord"):]
    assert "schulden" in nachher and "handel" in nachher, gesichert
    # Und der Signal-Weg ist wirklich angeschlossen.
    quelle = inspect.getsource(bot.FloBot.setup_hook)
    assert "SIGTERM" in quelle and "add_signal_handler" in quelle




def test_close_faehrt_alles_herunter_und_nur_einmal():
    """Vorher schloss close() nur die Discord-Verbindung: die geteilte
    aiohttp-Session, der Pool des LLM-Clients und das Web-Panel blieben offen
    ('Unclosed client session' bei JEDEM SIGTERM), und was nach dem letzten
    Sichern noch gebucht wurde, hing in save_soon und starb mit dem Loop.

    Jetzt: sichern -> Panel -> Discord -> nachsichern -> KI, genau einmal -
    auch wenn Signal und das Ende von run() gleichzeitig close() rufen, auch
    wenn ein Schritt scheitert, das Panel haengt oder ein Wartender
    abgebrochen wird."""
    import ai
    import bot
    import discord
    import webpanel
    from unittest import mock
    schritte = []

    def schritt(name, fehler=False, dauer=0.0):
        async def ausfuehren(*_a):
            schritte.append(name)
            if dauer:
                await asyncio.sleep(dauer)
            if fehler:
                raise RuntimeError(f"{name} kaputt")
        return ausfuehren

    def herunterfahren():
        async def alles():
            await asyncio.gather(bot.client.close(), bot.client.close())
            # Das Ende von client.run(): discord.py ruft __aexit__ - das muss
            # auf das GANZE Herunterfahren warten, nicht nur auf Discord.
            await bot.client.__aexit__(None, None, None)
        asyncio.run(alles())

    alt_modus = bot.MODE
    try:
        with mock.patch.object(bot.client, "_alles_sichern", schritt("sichern")), \
                mock.patch.object(webpanel, "stop", schritt("panel", fehler=True)), \
                mock.patch.object(discord.Client, "close", schritt("discord")), \
                mock.patch.object(ai, "aclose", schritt("ki")):
            herunterfahren()
            # Nachgesichert wird ALLES (Schulden, Handel, ...), nicht nur economy.
            assert schritte == ["sichern", "panel", "discord", "sichern", "ki"], schritte

            # --check/--once laufen womoeglich NEBEN dem Dienst - dort wird
            # nichts geschrieben, geschlossen aber schon.
            schritte.clear()
            bot.client._schliessen = None
            bot.MODE = "check"
            herunterfahren()
            assert schritte == ["panel", "discord", "ki"], schritte
        bot.MODE = alt_modus

        # Scheitert schon das erste Sichern, wird trotzdem abgemeldet und
        # geschlossen - sonst wartete der Prozess auf SIGKILL.
        schritte.clear()
        bot.client._schliessen = None
        with mock.patch.object(bot.client, "_alles_sichern",
                               schritt("sichern", fehler=True)), \
                mock.patch.object(webpanel, "stop", schritt("panel")), \
                mock.patch.object(discord.Client, "close", schritt("discord")), \
                mock.patch.object(ai, "aclose", schritt("ki")):
            herunterfahren()
        assert schritte == ["sichern", "panel", "discord", "sichern", "ki"], schritte

        # Ein haengendes Web-Panel (offene BotSicht-Leitung: gemessen 46 s)
        # haelt das Abmelden nicht auf - nach PANEL_FRIST geht es weiter.
        schritte.clear()
        bot.client._schliessen = None
        start = time.monotonic()
        with mock.patch.object(bot.client, "_alles_sichern", schritt("sichern")), \
                mock.patch.object(webpanel, "stop", schritt("panel", dauer=60)), \
                mock.patch.object(discord.Client, "close", schritt("discord")), \
                mock.patch.object(ai, "aclose", schritt("ki")), \
                mock.patch.object(bot.FloBot, "PANEL_FRIST", 0.05):
            herunterfahren()
        assert time.monotonic() - start < 5, "haengendes Panel blockiert das Beenden"
        assert schritte == ["sichern", "panel", "discord", "sichern", "ki"], schritte

        # Bricht jemand SEIN Warten ab (etwa ein abgebrochener Neustart-Task),
        # laeuft das Herunterfahren trotzdem bis zum Ende durch.
        schritte.clear()
        bot.client._schliessen = None

        async def abgebrochen():
            wartender = asyncio.ensure_future(bot.client.close())
            await asyncio.sleep(0.01)          # steckt jetzt im ersten Sichern
            wartender.cancel()
            await asyncio.gather(wartender, return_exceptions=True)
            await bot.client.close()           # der Rest wartet auf dasselbe Ende
        with mock.patch.object(bot.client, "_alles_sichern", schritt("sichern", dauer=0.1)), \
                mock.patch.object(webpanel, "stop", schritt("panel")), \
                mock.patch.object(discord.Client, "close", schritt("discord")), \
                mock.patch.object(ai, "aclose", schritt("ki")):
            asyncio.run(abgebrochen())
        assert schritte == ["sichern", "panel", "discord", "sichern", "ki"], schritte
    finally:
        bot.MODE = alt_modus
        bot.client._schliessen = None
    # Und beide Wege nach draussen gehen durch close(), statt selbst zu sichern.
    for weg in (bot.FloBot._sauber_beenden, bot.FloBot._restart_bot):
        quelle = inspect.getsource(weg)
        assert "self.close()" in quelle and "_alles_sichern()" not in quelle, weg




def test_ki_und_netz_session_werden_beim_testende_geschlossen():
    """Jeder Testlauf endete mit 'Unclosed client session': die geteilte Session
    aus ai.py legt der erste Test an, der etwas aus dem Netz holt, und keiner
    schloss sie. lauf.py raeumt jetzt am Ende auf wie FloBot.close() im
    Betrieb - auch eine Session, deren Loop laengst zu ist."""
    import ai
    import lauf

    async def anlegen():
        return ai.instance.http_session()

    alt = ai.instance._http, ai.instance._geschlossen
    ai.instance._http = None
    try:
        sitzung = asyncio.run(anlegen())          # ihr Loop ist danach zu
        assert not sitzung.closed
        lauf.Lauf._aufraeumen()
        assert sitzung.closed and ai.instance._http is None
        lauf.Lauf._aufraeumen()                   # zweimal: kein Fehler
    finally:
        ai.instance._http, ai.instance._geschlossen = alt




def test_selbsttests_laufen_nur_einmal_pro_prozess():
    """on_ready feuert bei jedem Reconnect (neues IDENTIFY) erneut - und jedes
    Mal liefen KI- und Musik-Selbsttest noch einmal: eine LLM-Anfrage vom
    knappen Kontingent und ein echter yt-dlp/ffmpeg-Abruf. Dazu als lose Tasks
    ohne Referenz, deren Fehler nie im Log landeten."""
    import ai
    import bot
    from unittest import mock
    gelaufen = []

    async def ki():
        gelaufen.append("ki")
        return True

    async def musik():
        gelaufen.append("musik")

    async def zweimal_bereit():
        bot.client._selbsttests_starten()
        bot.client._selbsttests_starten()         # Reconnect: on_ready noch einmal
        loop = asyncio.get_running_loop()
        aufgaben = [t for t in bot.client._bg_tasks if t.get_loop() is loop]
        await asyncio.gather(*aufgaben)
        return aufgaben

    alt = (bot.AI_ENABLED, bot.MUSIC_ENABLED, bot.SELBSTTEST_MUSIK,
           bot.client._selbsttest_gelaufen)
    bot.AI_ENABLED = bot.MUSIC_ENABLED = bot.SELBSTTEST_MUSIK = True
    bot.client._selbsttest_gelaufen = False
    try:
        with mock.patch.object(ai, "selbsttest", ki), \
                mock.patch.object(bot.client, "_musik_selbsttest", musik):
            aufgaben = asyncio.run(zweimal_bereit())
    finally:
        (bot.AI_ENABLED, bot.MUSIC_ENABLED, bot.SELBSTTEST_MUSIK,
         bot.client._selbsttest_gelaufen) = alt
    assert sorted(gelaufen) == ["ki", "musik"], gelaufen
    # Ueber _spawn gestartet: festgehalten und mit Fehlermeldung.
    assert len(aufgaben) == 2, aufgaben
    quelle = inspect.getsource(bot.FloBot.on_ready)
    assert "self._selbsttests_starten()" in quelle
    assert "loop.create_task(" not in quelle, "on_ready startet wieder lose Tasks"




def test_http_bibliotheken_fluten_das_log_nicht():
    """httpx schrieb JEDE Anfrage ab INFO ins Journal ('HTTP Request: POST
    https://api.groq.com/... 200 OK') - eine Zeile pro KI-Antwort und pro
    Hintergrundjob. Die echten Fehler gingen darin unter. Dasselbe beim
    Web-Panel: jeder Abruf eine Zeile, und die offene Uebersicht fragt alle
    6 s nach, die BotSicht alle 2,5 s."""
    import logging
    import bot                                                    # noqa: F401
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "openai",
                 "aiohttp.access"):
        stufe = logging.getLogger(name).getEffectiveLevel()
        assert stufe >= logging.WARNING, (name, logging.getLevelName(stufe))
    # Warnungen und Fehler kommen weiterhin durch.
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)




def test_arzt_meldet_die_falsch_gehende_uhr():
    """Auf dem Server springt die Uhr ~15 s nach dem Booten zwei Stunden zurueck:
    die Hardware-Uhr der VM laeuft auf Ortszeit, das System haelt sie fuer UTC.
    Flo startet vorher und stempelt in diesen Sekunden Zeiten aus der Zukunft.
    Der Arzt muss das sehen - und darf dabei nie abstuerzen."""
    import tools_check

    def befund(werte, rtc, systemd=None):
        arzt = tools_check.GesamtCheck()
        arzt._timedatectl = lambda: werte
        arzt._rtc_sekunden = lambda: rtc
        # systemctl: {(Befehl, Einheit): Ausgabe} - nie das echte des Rechners.
        arzt._systemctl = lambda *args: (systemd or {}).get(args[:2], "")
        puffer = io.StringIO()
        alt_aus = sys.stdout
        sys.stdout = puffer
        try:
            arzt.uhr()
        finally:
            sys.stdout = alt_aus
        rat = " ".join(was for _u, was in arzt.probleme)
        return re.sub(r"\x1b\[[0-9;]*m", "", puffer.getvalue()), rat

    jetzt = time.time()
    # Genau der Server: Hardware-Uhr zwei Stunden vor, 'RTC in local TZ: no'.
    text, rat = befund({"LocalRTC": "no", "NTPSynchronized": "yes"}, int(jetzt + 7200))
    assert "Hardware-Uhr" in text and "+2.0 h" in text, text
    assert "timedatectl set-local-rtc 1" in rat, rat
    assert "systemctl enable systemd-time-wait-sync" in rat, rat

    # Alles richtig: kein Befund.
    text, rat = befund({"LocalRTC": "no", "NTPSynchronized": "yes"}, int(jetzt))
    assert not rat and "OK" in text, (text, rat)
    # LocalRTC=yes: die Abweichung IST die Zeitzone - gewollt, kein Befund.
    _text, rat = befund({"LocalRTC": "yes", "NTPSynchronized": "yes"}, int(jetzt + 7200))
    assert not rat, rat
    # Uhr nicht per NTP gestellt.
    text, rat = befund({"LocalRTC": "no", "NTPSynchronized": "no"}, None)
    assert "NTP" in text and "systemd-time-wait-sync" in rat, (text, rat)

    # Kein timedatectl, die Hardware-Uhr aber sichtbar: so laeuft 'k alles'
    # unter Docker im Container. Nach 'set-local-rtc 1' auf dem Host geht die
    # RTC GEWOLLT auf Ortszeit - ohne LocalRTC ist das nicht zu unterscheiden,
    # also keine Warnung, die den schon befolgten Rat wiederholt.
    text, rat = befund({}, int(jetzt + 7200))
    assert not rat and "nicht pruefbar" in text, (text, rat)

    # Ein Update bringt die neue Dienstdatei nicht an ihren Platz: gefragt wird
    # systemd selbst, ob der EINGERICHTETE Dienst auf die Uhr wartet.
    gut = {"LocalRTC": "no", "NTPSynchronized": "yes"}
    alt_dienst = {("show", "flobot"): "LoadState=loaded\n"
                  "After=network-online.target basic.target"}
    text, rat = befund(gut, int(jetzt), alt_dienst)
    assert "time-sync.target" in text, text
    assert "daemon-reload" in rat and "flobot.service" in rat, rat
    neu_dienst = {("show", "flobot"): "LoadState=loaded\n"
                  "After=network-online.target time-sync.target basic.target",
                  ("is-enabled", "systemd-timesyncd"): "enabled",
                  ("is-enabled", "systemd-time-wait-sync"): "enabled"}
    _text, rat = befund(gut, int(jetzt), neu_dienst)
    assert not rat, rat
    # time-sync.target ist ohne systemd-time-wait-sync sofort erreicht.
    _text, rat = befund(gut, int(jetzt), {
        **neu_dienst, ("is-enabled", "systemd-time-wait-sync"): "disabled"})
    assert rat == "systemctl enable systemd-time-wait-sync", rat
    # chrony statt timesyncd: der hat seinen eigenen Warte-Dienst - kein Befund.
    _text, rat = befund(gut, int(jetzt), {
        **neu_dienst, ("is-enabled", "systemd-timesyncd"): "disabled",
        ("is-enabled", "systemd-time-wait-sync"): "disabled"})
    assert not rat, rat
    # Kein Dienst eingerichtet (Entwicklungsrechner): dazu schweigt die Uhr.
    _text, rat = befund(gut, int(jetzt), {("show", "flobot"): "LoadState=not-found"})
    assert not rat, rat

    # Nichts davon da (Container, kein systemd) oder Muell: leise, nie ein Absturz.
    for werte, rtc in (({}, None), (None, "quatsch"), ({"LocalRTC": 3}, True)):
        befund(werte, rtc)
    befund(gut, None, {("show", "flobot"): "Quatsch ohne Gleich"})
    arzt = tools_check.GesamtCheck()
    arzt._timedatectl = lambda: 1 / 0
    arzt.uhr()
    # Und die Gesamtdiagnose fragt die Uhr wirklich.
    assert "self.uhr()" in inspect.getsource(tools_check.GesamtCheck.lauf)




def test_dienst_wartet_auf_die_gestellte_uhr():
    """Startet Flo vor der NTP-Synchronisation, liegen alle Stempel aus den
    ersten Sekunden zwei Stunden in der Zukunft - systemd zeigte 'active since'
    zwei Stunden voraus. Der Dienst wartet deshalb auf time-sync.target."""
    wurzel = os.path.dirname(os.path.abspath(__file__))
    dienst = open(os.path.join(wurzel, "flobot.service"), encoding="utf-8").read()
    einheit = dienst.split("[Service]")[0]
    zeilen = {z.split("=", 1)[0]: z.split("=", 1)[1].split()
              for z in einheit.splitlines() if "=" in z and not z.startswith("#")}
    for schluessel in ("After", "Wants"):
        assert "time-sync.target" in zeilen.get(schluessel, []), (schluessel, zeilen)
        assert "network-online.target" in zeilen.get(schluessel, []), (schluessel, zeilen)
    # Der Rest bleibt, wie er war.
    assert "Restart=always" in dienst and "RestartMaxDelaySec=300" in dienst
    readme = open(os.path.join(wurzel, "README.md"), encoding="utf-8").read()
    assert "timedatectl set-local-rtc 1" in readme
    assert "systemctl enable systemd-time-wait-sync" in readme




def test_herzschlag_nur_bei_echter_verbindung():
    """Der Docker-Healthcheck prueft das Alter dieser Datei. Ein Prozess, der
    laeuft, aber keine Verbindung zu Discord hat, darf nicht als gesund gelten."""
    import bot
    import tempfile
    alt = bot.HERZSCHLAG_DATEI
    bot.HERZSCHLAG_DATEI = __import__("pathlib").Path(tempfile.mkdtemp()) / "herz"
    try:
        tot = SimpleNamespace(latency=float("inf"), is_closed=lambda: False,
                              is_ready=lambda: True)
        bot._herzschlag(tot)
        assert not bot.HERZSCHLAG_DATEI.exists()
        lebt = SimpleNamespace(latency=0.05, is_closed=lambda: False, is_ready=lambda: True)
        bot._herzschlag(lebt)
        assert bot.HERZSCHLAG_DATEI.exists()
    finally:
        bot.HERZSCHLAG_DATEI = alt




def test_panel_update_in_docker_nennt_den_richtigen_weg():
    """Im Container steckt der Code im Image: ein git pull dort waere beim
    naechsten Neubau weg, und neue Pakete kaemen nie an."""
    import webpanel
    wp = webpanel.WebPanel()

    class Anfrage(dict):
        async def json(self):
            return {"restart": False}

    alt = os.environ.get("FLO_LAUFZEIT")
    os.environ["FLO_LAUFZEIT"] = "docker"
    try:
        antwort = asyncio.run(wp._update_lauf(Anfrage()))
    finally:
        if alt is None:
            os.environ.pop("FLO_LAUFZEIT", None)
        else:
            os.environ["FLO_LAUFZEIT"] = alt
    assert antwort.status == 400
    assert "k n" in antwort.text
    # Und ausserhalb von Docker holt der Knopf neue Pakete gleich mit.
    quelle = inspect.getsource(webpanel.WebPanel._update_lauf)
    assert "requirements.txt" in quelle and '"pip", "install"' in quelle


if __name__ == "__main__":
    run(globals())
