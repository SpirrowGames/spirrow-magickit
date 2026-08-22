"""Unit tests for the S5'' decision material SQLite store.

Scope: ``DecisionMaterialStore.put_material`` / ``.get_material`` in
isolation. These tests do **not** go through HTTP; the endpoint tests
live in ``test_decision_materials_endpoint.py``.

Rules pinned here (spec ``spec/slices/S5-decision-materials.md`` §2):

- ``INSERT OR REPLACE`` on ``UNIQUE(project, thread_id)`` is idempotent
  (Heisenberg F-A) — the second PUT for the same key must not raise,
  and must overwrite (**P-8**).
- ``get_material`` on a missing row returns ``None`` (the HTTP layer
  turns that into 404; the renderer turns it into J-absent).
- ``options`` / ``unknowns`` round-trip through JSON serialization
  (stored as TEXT).
"""

from __future__ import annotations

import os
import tempfile

import pytest

from magickit.core.decision_materials import DecisionMaterialStore


@pytest.fixture
def store():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield DecisionMaterialStore(db_path=db_path)
    if os.path.exists(db_path):
        try:
            os.unlink(db_path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_put_then_get_round_trips_all_fields(store):
    """Every field survives a PUT → GET round-trip.

    ``options`` / ``unknowns`` are stored as JSON in TEXT columns; this
    test pins that the schema does not silently drop or reshape them.
    """
    result = await store.put_material(
        project="p1",
        thread_id="T-x",
        head_msg_id="msg-42",
        signature="opaque-sig",
        question="which path?",
        options=[
            {"id": "A", "label": "keep going", "gain": "speed", "loss": "risk"},
            {"id": "B", "label": "pause", "gain": "safety", "loss": "delay"},
        ],
        recommendation="A",
        recommendation_reason="cheaper to unwind if wrong",
        unknowns=["what does mindwire's push encoding look like?"],
    )
    assert result == {"stored": True, "replaced": False}

    read = await store.get_material(project="p1", thread_id="T-x")
    assert read is not None
    assert read["project"] == "p1"
    assert read["thread_id"] == "T-x"
    assert read["head_msg_id"] == "msg-42"
    assert read["signature"] == "opaque-sig"
    assert read["question"] == "which path?"
    assert read["options"] == [
        {"id": "A", "label": "keep going", "gain": "speed", "loss": "risk"},
        {"id": "B", "label": "pause", "gain": "safety", "loss": "delay"},
    ]
    assert read["recommendation"] == "A"
    assert read["recommendation_reason"] == "cheaper to unwind if wrong"
    assert read["unknowns"] == ["what does mindwire's push encoding look like?"]
    # ``stored_at`` is an ISO timestamp; we just assert it exists and looks
    # ISO-ish. Format is diagnostic-only (spec §2.3), not used for freshness.
    assert isinstance(read["stored_at"], str)
    assert "T" in read["stored_at"] and read["stored_at"].endswith("Z")


@pytest.mark.asyncio
async def test_second_put_replaces_and_reports_replaced(store):
    """★ P-8 pin: ``INSERT OR REPLACE`` on ``UNIQUE(project, thread_id)``.

    Second PUT with the same key must return ``replaced=True``. The row
    then reflects the second call's values (not merged). Concurrency
    lives on the SQLite side, not in our Python — this test does not
    exercise concurrency, only the schema-level idempotency.
    """
    await store.put_material(
        project="p", thread_id="T",
        head_msg_id="msg-1", signature=None,
        question="q1", options=None, recommendation=None,
        recommendation_reason=None, unknowns=None,
    )
    r2 = await store.put_material(
        project="p", thread_id="T",
        head_msg_id="msg-2", signature="s2",
        question="q2", options=[{"id": "X", "label": "later"}],
        recommendation="X", recommendation_reason="new rationale",
        unknowns=["u"],
    )
    assert r2 == {"stored": True, "replaced": True}

    read = await store.get_material(project="p", thread_id="T")
    assert read is not None
    # second write's values, not merged with first
    assert read["head_msg_id"] == "msg-2"
    assert read["signature"] == "s2"
    assert read["question"] == "q2"
    assert read["options"] == [{"id": "X", "label": "later"}]


@pytest.mark.asyncio
async def test_get_missing_row_returns_none(store):
    """Absence is signalled with ``None`` — HTTP → 404, renderer → J-absent."""
    assert await store.get_material(project="nope", thread_id="nope") is None


@pytest.mark.asyncio
async def test_optional_fields_round_trip_as_none(store):
    """A minimal PUT (just head_msg_id) reads back with ``None`` optionals.

    JSON columns must not appear as empty lists when they were absent —
    ``None`` and ``[]`` mean different things (respectively "not
    provided" and "provided empty"). The renderer distinguishes these
    when deciding whether to draw the "unknowns" panel.
    """
    await store.put_material(
        project="p", thread_id="T2",
        head_msg_id="msg-9", signature=None,
        question=None, options=None, recommendation=None,
        recommendation_reason=None, unknowns=None,
    )
    read = await store.get_material(project="p", thread_id="T2")
    assert read is not None
    assert read["options"] is None
    assert read["unknowns"] is None
    assert read["signature"] is None
    assert read["question"] is None


@pytest.mark.asyncio
async def test_distinct_keys_isolated(store):
    """Same project, different thread ids → two rows, no leak."""
    await store.put_material(
        project="p", thread_id="A",
        head_msg_id="msg-A", signature=None,
        question="qA", options=None, recommendation=None,
        recommendation_reason=None, unknowns=None,
    )
    await store.put_material(
        project="p", thread_id="B",
        head_msg_id="msg-B", signature=None,
        question="qB", options=None, recommendation=None,
        recommendation_reason=None, unknowns=None,
    )
    a = await store.get_material(project="p", thread_id="A")
    b = await store.get_material(project="p", thread_id="B")
    assert a is not None and b is not None
    assert a["question"] == "qA"
    assert b["question"] == "qB"
