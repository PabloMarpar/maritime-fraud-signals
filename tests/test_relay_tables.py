"""The Worker relay (relay/) carries its own MID -> flag table as JSON. It must match what
process.mid + report.countries give, so the live page shows the same flags whichever relay runs.
Regenerate with ``python -m report.countries``."""

from __future__ import annotations

import json

from report.countries import RELAY_MID_TABLE, mid_iso2_table


def test_relay_mid_table_matches_python():
    assert json.loads(RELAY_MID_TABLE.read_text(encoding="utf-8")) == mid_iso2_table()
