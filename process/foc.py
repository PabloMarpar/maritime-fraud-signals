"""Flag-of-convenience (FOC) classification, by flag-state country name.

Source: the ITF Fair Practices Committee's current FOC registry list, confirmed live 2026-09-23 at
https://www.itfseafarers.org/en/issues/flags-of-convenience/current-registries-listed-focs (48
registries; the page carries no last-updated date or machine-readable feed, so the list below is
transcribed by hand and stamped with the date it was read, not the date the ITF last changed it).

This module classifies by :data:`process.mid.MID_COUNTRY`'s country-name strings, the same
cosmetic-but-useful lookup P4-1's naive baseline already depends on for ``flag_country``. Two of
the ITF's 48 names have no MID-level counterpart at all (:data:`UNMAPPED_ITF_REGISTRIES`) --
France's and Germany's international second registers are legally distinct from their national
flag but broadcast under the same MID, so this project cannot see them. Three more are simple
name-variant mismatches, resolved by hand in :data:`FOC_NAME_TO_MID_COUNTRY` (``St Kitts and
Nevis`` -> ``Saint Kitts and Nevis``, ``St Vincent`` -> ``Saint Vincent and the Grenadines``,
``Tanzania (Zanzibar)`` -> ``Tanzania``).

Consequently :func:`is_foc` systematically undercounts: a vessel reflagged to a second register,
or to an ITF-listed flag whose MID entry is missing or wrong, reads as not-FOC. That is an
accepted property of a naive baseline, not a bug to fix here.
"""

from __future__ import annotations

ITF_FOC_REGISTRIES: frozenset[str] = frozenset(
    {
        "Antigua and Barbuda",
        "Bahamas",
        "Barbados",
        "Belize",
        "Bermuda",
        "Bolivia",
        "Cameroon",
        "Cayman Islands",
        "Comoros",
        "Cook Islands",
        "Curacao",
        "Cyprus",
        "Dominica",
        "Equatorial Guinea",
        "Eswatini",
        "Faroe Islands",
        "French International Ship Registry (FIS)",
        "Gabon",
        "Gambia",
        "German International Ship Registry (GIS)",
        "Georgia",
        "Gibraltar",
        "Guinea-Bissau",
        "Honduras",
        "Jamaica",
        "Lebanon",
        "Liberia",
        "Malta",
        "Madeira",
        "Marshall Islands",
        "Mauritius",
        "Moldova",
        "Mongolia",
        "Myanmar",
        "North Korea",
        "Niue",
        "Palau",
        "Panama",
        "San Marino",
        "Sao Tome and Principe",
        "Sierra Leone",
        "St Kitts and Nevis",
        "St Vincent",
        "Sri Lanka",
        "Tanzania (Zanzibar)",
        "Togo",
        "Tuvalu",
        "Vanuatu",
    }
)

# ITF registries with no MID-level counterpart -- second/international registers, invisible at the
# MID granularity this project resolves flags to. name -> one-line reason.
UNMAPPED_ITF_REGISTRIES: dict[str, str] = {
    "French International Ship Registry (FIS)": "second register under France's MID, not its own MID",
    "German International Ship Registry (GIS)": "second register under Germany's MID, not its own MID",
}

# ITF name -> the exact process.mid.MID_COUNTRY value it corresponds to. Identity for the 43 names
# that already match a MID_COUNTRY value verbatim; explicit for the 3 renamed variants.
_RENAMED_TO_MID_COUNTRY: dict[str, str] = {
    "St Kitts and Nevis": "Saint Kitts and Nevis",
    "St Vincent": "Saint Vincent and the Grenadines",
    "Tanzania (Zanzibar)": "Tanzania",
}
FOC_NAME_TO_MID_COUNTRY: dict[str, str] = {
    name: _RENAMED_TO_MID_COUNTRY.get(name, name)
    for name in ITF_FOC_REGISTRIES
    if name not in UNMAPPED_ITF_REGISTRIES
}

# The set of MID_COUNTRY values that count as flag-of-convenience for is_foc's lookup.
_FOC_MID_COUNTRY_VALUES: frozenset[str] = frozenset(FOC_NAME_TO_MID_COUNTRY.values())


def is_foc(flag_country: str | None) -> bool:
    """True if flag_country names an ITF-listed flag-of-convenience registry.

    Total predicate: None (unresolved MID) and any name not on the ITF list both return False.
    """
    if flag_country is None:
        return False
    return flag_country in _FOC_MID_COUNTRY_VALUES


def foc_status(flag_country: str | None) -> str:
    """"foc" / "not_foc" / "unknown_flag", for reporting rather than filtering."""
    if flag_country is None:
        return "unknown_flag"
    return "foc" if flag_country in _FOC_MID_COUNTRY_VALUES else "not_foc"
