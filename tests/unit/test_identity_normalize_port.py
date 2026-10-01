"""Conformance of the ADR-11 port against the shared vectors.

T-magickit-stop-disposition-intake (DESIGN v6 §1, v9 §1, v10 §1):

- the port must reproduce every ``[raw, expected_key]`` pair and every
  collision group in ``tests/fixtures/adr11_normalize_vectors.json``;
- the copy must not have been edited by hand: its sha256, taken over the
  text with line endings normalised to LF, must equal ``sha256_lf_text`` in
  the provenance file. LF-normalised so ``core.autocrlf`` on Windows cannot
  red the test. No network, no git: hermetic.

Not in scope (stated by the design, kept visible here): a change to the
canonical copy in spirrow-mindwire is NOT detected. This test only notices
a hand edit to this copy.

Refreshing the copy: take the canonical file with
``git show <commit>:tests/fixtures/adr11_normalize_vectors.json`` in
spirrow-mindwire, write it here, compute
``sha256(text.replace("\r\n", "\n").encode("utf-8"))`` and write that
and the commit into the provenance file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from magickit.core.identity_normalize import find_collisions, normalize_identity_key

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_VECTORS = _FIXTURES / "adr11_normalize_vectors.json"
_PROVENANCE = _FIXTURES / "adr11_normalize_vectors.provenance.json"


def _lf_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _vectors() -> dict:
    return json.loads(_VECTORS.read_text(encoding="utf-8"))


def test_vector_copy_matches_its_recorded_hash() -> None:
    provenance = json.loads(_PROVENANCE.read_text(encoding="utf-8"))
    assert _lf_text_sha256(_VECTORS) == provenance["sha256_lf_text"], (
        "adr11_normalize_vectors.json was edited after it was copied. Re-copy it "
        "from the canonical file and update the provenance (see module docstring)."
    )


def test_provenance_names_the_canonical_location() -> None:
    provenance = json.loads(_PROVENANCE.read_text(encoding="utf-8"))
    assert provenance["source_repo"] == "spirrow-mindwire"
    assert provenance["source_path"] == "tests/fixtures/adr11_normalize_vectors.json"
    assert "source_commit" in provenance


@pytest.mark.parametrize("raw,expected", _vectors()["normalize"])
def test_normalize_conforms(raw: str, expected: str) -> None:
    assert normalize_identity_key(raw) == expected


@pytest.mark.parametrize(
    "case", _vectors()["collisions"], ids=lambda c: repr(c["input"])
)
def test_find_collisions_conforms(case: dict) -> None:
    assert find_collisions(case["input"]) == case["expected"]
