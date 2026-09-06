"""Unit-test-scoped fixtures.

The tree-wide fixtures live in ``tests/conftest.py`` (``temp_db_path``,
``state_manager``, ``task_queue``, ``event_loop``). This file adds the
one thing that is unit-specific: **isolating the S5'' decision material
store per test**, so a test that only cares about the D-26' branches
does not leak a SQLite file into the repo's ``data/`` dir, and tests
that DO care get a fresh empty store.

The mechanism is a small autouse patch on ``decisions._get_material_store``
returning a ``DecisionMaterialStore`` bound to a ``tmp_path`` file. Tests
that want to seed materials call ``material_store()`` from this fixture
to obtain the same store instance the handler will see.
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable
from unittest.mock import AsyncMock

import pytest

from magickit.core.decision_materials import DecisionMaterialStore
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web import decisions as decisions_module


@pytest.fixture(autouse=True)
def stub_decision_identity_lookup(monkeypatch):
    """Default the *decision page's* identity lookup to "registered".

    Scoped to ``decisions_module._resolve_identity`` (the local wrapper
    added by I-19 / msg-146 §3), so the fixture does **not** interfere
    with the role / next_participant / pr-gate tests that drive
    ``chatroom_tools._lookup_identity`` through a mocked
    ``_prismind_adapter``. Those tests share the identity registry the
    real POST-time gate uses; the decision page wrapper is a separate
    binding for exactly this test-isolation reason.

    Why the default is "registered" rather than the real Prismind call:
    the real call is a connection-refused per candidate in the unit
    env, which is (a) ~4s per test at the render-budget cap and (b) a
    behaviour swap unrelated tests were not written for (the select
    empties, ``parked_author`` is dropped, etc). Tests that WANT to
    observe UNKNOWN / UNREGISTERED branches override with
    ``patch.object(chatroom_tools, "_lookup_identity", side_effect=...)``
    (that is the surface the decision-page wrapper calls into).

    The stubbed verdict is "registered with no allowed_roles" — the
    same shape ``_identity_response`` uses in
    ``test_next_participant_gate``: ``found=True`` satisfies the select
    filter, ``allowed_roles=()`` is inert because the filter does not
    read it.
    """
    async def _stub(name: str):
        return chatroom_tools._IdentityLookup(
            unavailable_reason=None, found=True, allowed_roles=()
        )

    monkeypatch.setattr(decisions_module, "_resolve_identity", _stub)
    yield _stub


@pytest.fixture(autouse=True)
def isolated_material_store(monkeypatch) -> Callable[[], DecisionMaterialStore]:
    """Redirect ``decisions._get_material_store`` to a per-test temp file.

    Return value: a callable that yields the isolated store, so tests
    that want to seed materials write via the same DB the handler will
    read. Autouse so tests that don't care get isolation for free.

    The temp file is cleaned up when the test ends (regardless of
    success/failure) via a finalizer. We use a plain ``NamedTemporaryFile``
    rather than pytest's ``tmp_path`` so the fixture stays usable in
    non-async tests that don't take ``tmp_path`` directly.

    **cache_clear safety net** (T-material-store-per-request-sync-io): the
    production ``_get_material_store`` is now ``functools.lru_cache``-
    wrapped so `get_settings()` / `mkdir` run once per process. Every test
    replaces the function whole via ``monkeypatch.setattr`` below ∴ the
    cache on the original is inert during patched calls, but we still
    clear it around each test as defence in depth (any test that reaches
    the real function must not observe a store cached from an earlier
    test whose settings differed).
    """
    # Defence in depth: reset the singleton cache before patching. Guard
    # with ``hasattr`` so this fixture keeps working if the production
    # cache decorator is ever removed / replaced.
    if hasattr(decisions_module._get_material_store, "cache_clear"):
        decisions_module._get_material_store.cache_clear()

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = DecisionMaterialStore(db_path=db_path)

    monkeypatch.setattr(decisions_module, "_get_material_store", lambda: store)

    def factory() -> DecisionMaterialStore:
        return store

    yield factory

    # Cleanup: aiosqlite closes the connection per call ∴ no lingering
    # handle. Deleting the file is safe.
    if os.path.exists(db_path):
        try:
            os.unlink(db_path)
        except OSError:
            # On Windows a stray handle can prevent unlink. Test isolation
            # still holds (each test got its own file) ∴ leave it for the
            # OS temp reaper rather than failing the run.
            pass

    # Clear again after teardown: monkeypatch restores the original
    # function object, and any state it accreted during this test's
    # non-patched code paths (rare, but possible for tests that spawn
    # subprocesses or bypass the module attribute) must not leak.
    if hasattr(decisions_module._get_material_store, "cache_clear"):
        decisions_module._get_material_store.cache_clear()
