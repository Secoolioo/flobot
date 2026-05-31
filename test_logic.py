"""Tests fuer schedule_logic - ohne Zusatzpakete lauffaehig:

    python test_logic.py
"""
from datetime import datetime

import schedule_logic as sl


def test_periods():
    assert sl.get_period(datetime(2026, 1, 1, 0, 0).time()) == "nacht"
    assert sl.get_period(datetime(2026, 1, 1, 5, 59).time()) == "nacht"
    assert sl.get_period(datetime(2026, 1, 1, 6, 0).time()) == "morgen"
    assert sl.get_period(datetime(2026, 1, 1, 9, 59).time()) == "morgen"
    assert sl.get_period(datetime(2026, 1, 1, 10, 0).time()) == "tag"
    assert sl.get_period(datetime(2026, 1, 1, 17, 59).time()) == "tag"
    assert sl.get_period(datetime(2026, 1, 1, 18, 0).time()) == "abend"
    assert sl.get_period(datetime(2026, 1, 1, 21, 59).time()) == "abend"
    assert sl.get_period(datetime(2026, 1, 1, 22, 0).time()) == "nacht"
    assert sl.get_period(datetime(2026, 1, 1, 23, 59).time()) == "nacht"


def test_seasons():
    assert sl.get_season(12) == "winter"
    assert sl.get_season(1) == "winter"
    assert sl.get_season(2) == "winter"
    assert sl.get_season(3) == "fruehling"
    assert sl.get_season(5) == "fruehling"
    assert sl.get_season(6) == "sommer"
    assert sl.get_season(8) == "sommer"
    assert sl.get_season(9) == "herbst"
    assert sl.get_season(11) == "herbst"


def test_image_defaults_jahreszeitunabhaengig():
    # Nacht/Morgen/Abend: Jahreszeit egal -> immer dasselbe Bild
    for month in range(1, 13):
        assert sl.get_image_filename(datetime(2026, month, 15, 3, 0)) == "NachtBild.png"
        assert sl.get_image_filename(datetime(2026, month, 15, 7, 0)) == "MorgenBild.png"
        assert sl.get_image_filename(datetime(2026, month, 15, 20, 0)) == "AbendsBild.png"


def test_image_tagsueber_jahreszeitabhaengig():
    # Tagsueber (12 Uhr) haengt es von der Jahreszeit ab
    assert sl.get_image_filename(datetime(2026, 1, 15, 12, 0)) == "Winter.png"
    assert sl.get_image_filename(datetime(2026, 4, 15, 12, 0)) == "Frühling.png"
    assert sl.get_image_filename(datetime(2026, 7, 15, 12, 0)) == "Sommer.png"
    assert sl.get_image_filename(datetime(2026, 10, 15, 12, 0)) == "Herbst.png"


def test_voller_tag_im_sommer():
    seq = [sl.get_image_filename(datetime(2026, 7, 1, h, 0)) for h in range(24)]
    expected = (
        ["NachtBild.png"] * 6      # 00-05
        + ["MorgenBild.png"] * 4   # 06-09
        + ["Sommer.png"] * 8       # 10-17
        + ["AbendsBild.png"] * 4   # 18-21
        + ["NachtBild.png"] * 2    # 22-23
    )
    assert seq == expected, seq


def test_voller_tag_im_winter():
    seq = [sl.get_image_filename(datetime(2026, 1, 1, h, 0)) for h in range(24)]
    # Nur das Tagbild unterscheidet sich von Sommer
    assert seq[10:18] == ["Winter.png"] * 8
    assert seq[0:6] == ["NachtBild.png"] * 6


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok   {t.__name__}")
    print(f"\n{len(tests)} Tests bestanden.")


if __name__ == "__main__":
    run()
