"""Unit tests for process.mid. Pure arithmetic/dict lookups, no fixtures, no data/."""

from __future__ import annotations

from process.mid import MID_COUNTRY, country_of, mid_of


def test_mid_of_extracts_first_three_digits():
    assert mid_of(219000001) == 219
    assert mid_of(219633000) == 219


def test_mid_of_denmark_examples():
    assert mid_of(219029848) == 219
    assert mid_of(220123456) == 220


def test_mid_of_boundary_mmsi():
    assert mid_of(200000000) == 200
    assert mid_of(799999999) == 799


def test_country_of_known_mid():
    assert country_of(219000001) == "Denmark"
    assert country_of(259036100) == "Norway"


def test_country_of_unknown_mid_returns_none():
    # 999 is outside any allocated MID range in MID_COUNTRY.
    assert 999 not in MID_COUNTRY
    assert country_of(999000000) is None


def test_two_mmsi_same_mid_are_same_flag():
    a, b = 219029848, 219633000
    assert mid_of(a) == mid_of(b)


def test_two_mmsi_different_mid_are_different_flag():
    # Real cross-MID pair observed in the P2-5 data profiling (Denmark vs Norway).
    a, b = 219029848, 259036100
    assert mid_of(a) != mid_of(b)
