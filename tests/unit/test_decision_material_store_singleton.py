"""Regression tests for the ``_get_material_store`` process-singleton cache.

Scope: **only** the caching contract of
``magickit.web.decisions._get_material_store``. Everything else about the
store / endpoint / renderer is covered by the peer tests in this
directory; the point here is to pin the two properties the S5''
per-request I/O fix (T-material-store-per-request-sync-io) needs.

- **Signature is nullary.** Einstein's naysayer disposition on the
  same thread ruled out adding a ``db_path`` argument: a ``Depends``-
  injected ``_get_material_store(db_path)`` would be interpreted as a
  query parameter and every call would 422. This test fails loud if a
  later edit adds a required argument.
- **The function is ``lru_cache``-wrapped.** Without the cache,
  ``get_settings()`` (which stats ``config/magickit_config.yaml`` and
  reads it when present) and ``DecisionMaterialStore.__init__`` (which
  does a synchronous ``mkdir``) run on every HTTP request against the
  endpoint — the antipattern the fix was made to remove.

We grab the real function reference at *module import time*, before any
autouse fixture in ``conftest.py`` has a chance to swap the module
attribute out with a per-test lambda. Autouse fixtures run per test, not
per module, so this capture is race-free.
"""

from __future__ import annotations

import inspect
import os
import tempfile

from magickit.config import Settings
from magickit.web import decisions as decisions_module

# Capture the real, ``lru_cache``-wrapped function BEFORE any autouse
# fixture in ``tests/unit/conftest.py`` runs. Autouse fixtures fire per
# test, not per module import, so at this scope the module attribute is
# still the production callable.
_REAL_GET_MATERIAL_STORE = decisions_module._get_material_store


def test_get_material_store_has_a_nullary_signature() -> None:
    """Signature must remain ``() -> DecisionMaterialStore``.

    Rationale (Einstein naysayer disposition on
    T-material-store-per-request-sync-io): the FastAPI endpoints call
    this function directly. If it grew a required argument, FastAPI's
    ``Depends`` resolution would try to inject it as a query parameter
    and every call would 422. The test guards that boundary.
    """
    unwrapped = inspect.unwrap(_REAL_GET_MATERIAL_STORE)
    sig = inspect.signature(unwrapped)
    required = [
        p
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    ]
    assert required == [], (
        "_get_material_store must be nullary: FastAPI Depends would "
        "otherwise pull required args from the query string and 422 "
        "every request. Rework the cache key instead of adding an "
        "argument (T-material-store-per-request-sync-io / msg-255 §4, "
        "naysayer disposition)."
    )


def test_get_material_store_is_lru_cached() -> None:
    """The prod function is wrapped by ``functools.lru_cache``.

    Failing this test signals the decorator was removed, which would
    silently re-introduce per-request ``get_settings()`` + sync
    ``mkdir`` on the event loop (T-material-store-per-request-sync-io
    / msg-255 §3).
    """
    assert hasattr(_REAL_GET_MATERIAL_STORE, "cache_clear"), (
        "_get_material_store lost its lru_cache wrapper — per-request "
        "get_settings() + mkdir will return. See "
        "T-material-store-per-request-sync-io / msg-255 §4."
    )
    assert hasattr(_REAL_GET_MATERIAL_STORE, "cache_info"), (
        "_get_material_store lost its lru_cache wrapper (cache_info "
        "missing) — same root cause as above."
    )


def test_get_material_store_returns_same_instance_on_repeat_calls(
    monkeypatch,
) -> None:
    """Two calls to the real function must return the same instance.

    We invoke the captured original directly (not the module attribute,
    which the autouse fixture has replaced with a lambda). ``get_settings``
    is redirected to a tmp path so calling the real function does not
    ``mkdir`` inside the repo's ``data/`` directory during the test run,
    and ``cache_clear`` is called on both sides so this test does not
    depend on — or leak into — the cache slot for any other test.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(
        decisions_module, "get_settings", lambda: Settings(db_path=db_path)
    )

    _REAL_GET_MATERIAL_STORE.cache_clear()
    try:
        first = _REAL_GET_MATERIAL_STORE()
        second = _REAL_GET_MATERIAL_STORE()
    finally:
        _REAL_GET_MATERIAL_STORE.cache_clear()
        if os.path.exists(db_path):
            try:
                os.unlink(db_path)
            except OSError:
                pass

    assert first is second, (
        "_get_material_store must return the same instance across calls; "
        "otherwise get_settings() + DecisionMaterialStore.__init__ "
        "(sync mkdir on the event loop) run once per HTTP request."
    )
