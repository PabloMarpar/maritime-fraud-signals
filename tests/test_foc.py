"""Unit tests for process.foc. Pure set/dict lookups, no fixtures, no data/."""

from __future__ import annotations

from process.foc import (
    FOC_NAME_TO_MID_COUNTRY,
    ITF_FOC_REGISTRIES,
    UNMAPPED_ITF_REGISTRIES,
    foc_status,
    is_foc,
)
from process.mid import MID_COUNTRY


def test_every_itf_registry_is_either_mapped_or_explicitly_unmapped():
    # If someone adds an ITF registry without deciding how to handle it, this must fail loudly.
    assert ITF_FOC_REGISTRIES == set(FOC_NAME_TO_MID_COUNTRY) | set(UNMAPPED_ITF_REGISTRIES)


def test_every_mapped_mid_country_value_actually_exists():
    # If process.mid.MID_COUNTRY ever renames a country, this must fail loudly rather than
    # silently dropping a flag out of FOC coverage.
    mid_values = set(MID_COUNTRY.values())
    for itf_name, mid_value in FOC_NAME_TO_MID_COUNTRY.items():
        assert mid_value in mid_values, f"{itf_name!r} maps to {mid_value!r}, not in MID_COUNTRY"


def test_no_overlap_between_mapped_and_unmapped():
    assert set(FOC_NAME_TO_MID_COUNTRY).isdisjoint(UNMAPPED_ITF_REGISTRIES)


def test_is_foc_true_for_known_foc_flag():
    assert is_foc("Panama") is True
    assert is_foc("Liberia") is True
    assert is_foc("Marshall Islands") is True


def test_is_foc_false_for_non_foc_flag():
    assert is_foc("Denmark") is False
    assert is_foc("Netherlands") is False


def test_is_foc_false_for_none():
    assert is_foc(None) is False


def test_is_foc_handles_renamed_itf_variants():
    # These are the 3 name-variant mismatches between the ITF's own spelling and MID_COUNTRY's.
    assert is_foc("Saint Kitts and Nevis") is True
    assert is_foc("Saint Vincent and the Grenadines") is True
    assert is_foc("Tanzania") is True


def test_foc_status_three_way():
    assert foc_status("Panama") == "foc"
    assert foc_status("Denmark") == "not_foc"
    assert foc_status(None) == "unknown_flag"


def test_unmapped_registries_are_not_reachable_via_is_foc():
    # FIS/GIS are second registers with no MID of their own -- no flag_country value can ever
    # equal these literal ITF names, so is_foc must not accidentally treat them as valid input.
    for name in UNMAPPED_ITF_REGISTRIES:
        assert is_foc(name) is False
