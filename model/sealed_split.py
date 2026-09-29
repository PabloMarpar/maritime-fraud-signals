"""P4-10: the sealed IMO split, pre-registered in ``docs/DECISIONS.md`` (2026-09-29) before any new
label source or feature was scored.

**Why.** From P4-9 on, several attempts (new labels, new signals, new models) are compared on the
same walk-forward pool. Picking the best of many attempts on one test set selects on that test
set, and 71% of the pool's positive rows are vessels already examined while designing P4-3c's
column set. So decisions are made on one half of the vessels and the chosen models are scored on
the other half once, at the end.

**Three groups, by IMO** (a vessel is in the same group in every month):

- ``examined``: the IMOs frozen in ``model/examined_imos.txt`` -- ever-sanctioned on the
  2026-09-21 snapshot and seen in the June or November 2024 population (the website's shadow
  fleet). Never sealed.
- ``sealed``: every other IMO whose ``crc32("p4-10|<imo>")`` is odd. A hash, not a random draw,
  so the split is reproducible and independent of labels and features.
- ``dev``: the rest.

**Scopes** name the test rows a run may report: ``dev`` (examined + dev), ``dev_clean`` (dev
only) and ``sealed``. Training rows are never filtered: a training label is an as-of-cutoff public
designation, and learning from a sealed vessel's past does not reveal performance on it.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np

EXAMINED_PATH = Path(__file__).with_name("examined_imos.txt")
SPLIT_SALT = "p4-10"

EXAMINED = "examined"
DEV = "dev"
SEALED = "sealed"

SCOPES: dict[str, tuple[str, ...]] = {
    "dev": (EXAMINED, DEV),
    "dev_clean": (DEV,),
    "sealed": (SEALED,),
}


def load_examined(path: Path = EXAMINED_PATH) -> frozenset[str]:
    """The frozen examined IMOs (``#`` lines are comments)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(s.strip() for s in lines if s.strip() and not s.startswith("#"))


def hash_is_sealed(imo: str) -> bool:
    return zlib.crc32(f"{SPLIT_SALT}|{imo}".encode()) % 2 == 1


def group_of(imo: str, examined: frozenset[str]) -> str:
    if imo in examined:
        return EXAMINED
    return SEALED if hash_is_sealed(imo) else DEV


def groups(imos: np.ndarray, examined: frozenset[str] | None = None) -> np.ndarray:
    ex = load_examined() if examined is None else examined
    return np.array([group_of(str(i), ex) for i in imos], dtype=object)


def scope_mask(
    imos: np.ndarray, scope: str, examined: frozenset[str] | None = None
) -> np.ndarray:
    """True for the rows whose IMO belongs to `scope`."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {sorted(SCOPES)}")
    return np.isin(groups(imos, examined), SCOPES[scope])
