"""Tests for model.sealed_split: the frozen examined list, the hash rule and the scopes."""

from __future__ import annotations

import zlib

import numpy as np
import pytest

from model.sealed_split import (
    DEV,
    EXAMINED,
    SEALED,
    group_of,
    groups,
    hash_is_sealed,
    load_examined,
    scope_mask,
)


def test_examined_list_is_the_frozen_260_imos():
    ex = load_examined()
    assert len(ex) == 260
    assert all(i.isdigit() and len(i) == 7 for i in ex)


def test_hash_rule_is_the_preregistered_one():
    for imo in ("9123456", "9999999", "1000000"):
        assert hash_is_sealed(imo) == (zlib.crc32(f"p4-10|{imo}".encode()) % 2 == 1)


def test_examined_imos_are_never_sealed():
    sealed_by_hash = next(str(i) for i in range(9000000, 9000100) if hash_is_sealed(str(i)))
    assert group_of(sealed_by_hash, frozenset({sealed_by_hash})) == EXAMINED
    assert group_of(sealed_by_hash, frozenset()) == SEALED


def test_split_is_roughly_half_and_same_in_every_month():
    imos = np.array([str(i) for i in range(9000000, 9004000)])
    g = groups(imos, frozenset())
    assert 0.45 < (g == SEALED).mean() < 0.55
    assert set(g) == {DEV, SEALED}
    assert (groups(imos[::-1], frozenset())[::-1] == g).all()


def test_scopes_partition_the_rows():
    imos = np.array([str(i) for i in range(9000000, 9000200)])
    ex = frozenset(imos[:10])
    dev = scope_mask(imos, "dev", ex)
    dev_clean = scope_mask(imos, "dev_clean", ex)
    sealed = scope_mask(imos, "sealed", ex)
    assert not (dev & sealed).any()
    assert (dev | sealed).all()
    assert (dev_clean <= dev).all()
    assert dev[:10].all() and not dev_clean[:10].any()


def test_unknown_scope_is_an_error():
    with pytest.raises(ValueError):
        scope_mask(np.array(["9123456"]), "everything", frozenset())
