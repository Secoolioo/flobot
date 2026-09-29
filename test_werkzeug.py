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
    economy.json, der Posten aber noch offen in schulden.json: doppelt bezahlt."""
    import bot
    import handel
    import schulden
    gesichert = []

    async def merke(name):
        gesichert.append(name)

    alt = (schulden.instance._store, getattr(schulden.instance, "_save", None),
           handel.instance._store, bot.client.close)
    schulden.instance._store = _FakeStore({})
    schulden.instance._save = lambda: merke("schulden")
    handel.instance._store = SimpleNamespace(save=lambda: merke("handel"))
    geschlossen = []

    async def zu():
        geschlossen.append(True)

    bot.client.close = zu
    try:
        asyncio.run(bot.client._sauber_beenden("SIGTERM"))
        asyncio.run(bot.client._sauber_beenden("SIGTERM"))   # zweites Signal: nichts doppelt
    finally:
        (schulden.instance._store, schulden.instance._save,
         handel.instance._store, bot.client.close) = alt
        bot.client._beendet_schon = False
    assert "schulden" in gesichert and "handel" in gesichert, gesichert
    assert geschlossen == [True], geschlossen
    # Und der Signal-Weg ist wirklich angeschlossen.
    quelle = inspect.getsource(bot.FloBot.setup_hook)
    assert "SIGTERM" in quelle and "add_signal_handler" in quelle




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
