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


if __name__ == "__main__":
    run(globals())
