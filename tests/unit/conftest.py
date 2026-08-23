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

import pytest

from magickit.core.decision_materials import DecisionMaterialStore
from magickit.web import decisions as decisions_module


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
    """
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
