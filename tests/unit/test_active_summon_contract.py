"""The ``active_summon`` contract, held to mindwire's canonical fixture.

``tests/data/summon/active_summon_contract.json`` is a byte-identical copy of
``SpirrowGames/spirrow-mindwire`` ``tests/data/summon/active_summon_contract.json``
(last changed in 153b509, measured on origin/main 660dbac). The sha256 and the
size are pinned here so that when the canonical file changes, this copy
cannot keep passing silently: re-copy it, then update the two constants.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from magickit.core.active_summon import parse_active_summon, parse_since

FIXTURE = Path(__file__).resolve().parents[1] / "data" / "summon" / "active_summon_contract.json"
CANONICAL_SHA256 = "e522e200d941d18254b9f5defa7e6d5f14479d95158157cf84577735445bf18a"
CANONICAL_BYTES = 4730

_CASES = json.loads(FIXTURE.read_bytes())["cases"]


def test_the_copy_is_byte_identical_to_the_canonical_fixture() -> None:
    raw = FIXTURE.read_bytes()
    assert len(raw) == CANONICAL_BYTES
    assert b"\r\n" not in raw  # .gitattributes keeps it LF on a Windows checkout
    assert hashlib.sha256(raw).hexdigest() == CANONICAL_SHA256


def test_the_fixture_still_names_this_repo_as_a_copy() -> None:
    copies = json.loads(FIXTURE.read_bytes())["_copies"]
    assert any("magickit" in c and "loop_control_get" in c for c in copies)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_every_contract_case(case: dict) -> None:
    expect = case["expect"]
    got = parse_active_summon(case["payload"])

    assert got.kind == expect["kind"]
    if expect["kind"] == "valid":
        assert got.thread_id == expect["thread_id"]
        assert got.by == expect["by"]
        # The fixture states `since` already normalised to UTC; compare as
        # instants, not as strings (Z vs +00:00 is formatting).
        assert got.since == parse_since(expect["since"])
        assert got.since is not None and got.since.utcoffset().total_seconds() == 0
    else:
        assert got.thread_id is None and got.since is None and got.by is None


def test_conclairs_own_serialisation_is_valid() -> None:
    """Conclair writes ``+00:00`` with 6 fractional digits (design v1.2 §1)."""
    got = parse_active_summon(
        {"active_summon": {"thread_id": "T-x", "since": "2026-10-29T01:02:03.000000+00:00", "by": "h"}}
    )
    assert got.kind == "valid"
