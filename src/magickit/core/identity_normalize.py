"""ADR-2026-05-29-11 identity-key normalisation, ported from spirrow-mindwire.

Source: ``spirrow-mindwire`` ``src/spirrow_mindwire/identity/normalize.py`` at
commit ``10aabb9eeb36f223f5495cf3dc8468285c22232d`` (blob ``57c43cb7``).
Ported, not imported (T-magickit-stop-disposition-intake, Bohr DESIGN v6 §1):
mindwire is a client of Magickit's API, so Magickit importing mindwire would
make the dependency circular, and the rule is two functions long.

A second spelling of a rule is a second place for it to be wrong. The guard
against that is the shared conformance vectors
(``tests/fixtures/adr11_normalize_vectors.json``) that both implementations
must pass; see ``tests/unit/test_identity_normalize_port.py``.

The rule, unchanged from the source: strip, collapse every run of whitespace /
underscore / hyphen to a single ``-``, casefold. ``find_collisions`` REPORTS
groups of distinct raw spellings that share a key; it never picks a winner.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

__all__ = ["find_collisions", "normalize_identity_key"]

_SEP_RE = re.compile(r"[\s_-]+")


def normalize_identity_key(name: str) -> str:
    """Return the ADR-11 canonical key for ``name``. Empty input returns ``""``."""
    if not name:
        return ""
    return _SEP_RE.sub("-", name.strip()).casefold()


def find_collisions(names: Iterable[str]) -> dict[str, list[str]]:
    """Group raw strings by key; return only groups with 2+ distinct raws.

    Repeated identical raws are folded (the same name is the same identity).
    Raws within a group keep first-seen order.
    """
    seen: dict[str, list[str]] = defaultdict(list)
    for raw in names:
        key = normalize_identity_key(raw)
        if raw in seen[key]:
            continue
        seen[key].append(raw)
    return {key: raws for key, raws in seen.items() if len(raws) > 1}
