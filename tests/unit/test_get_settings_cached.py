"""Regression tests for the ``get_settings`` process-lifetime cache.

Scope: **only** the caching contract of ``magickit.config.get_settings``.
Everything else about ``Settings`` and its YAML loader is covered by peer
tests; the point here is to pin the two properties the
T-get-settings-uncached-on-live-request-paths fix (msg-491) needs.

- **Signature is nullary.** Consumers call it directly on the async
  request path (``board.py``'s HTMX poll fragment, ``chatroom_digest``,
  ``chatroom_proxy``, ``deploys``, ``ops``). Adding a required argument
  would also change how FastAPI would resolve any of those handlers.
- **The function is ``lru_cache``-wrapped.** Without the cache, each
  request handler on the poll path (every 20s per open dashboard, per
  ``board.html``'s ``hx-trigger``) stats ``config/magickit_config.yaml``
  and, when the file is present, reads and parses ~11KB of YAML on the
  event loop — the frequency Einstein's naysayer disposition (same
  thread) correctly judged operationally invisible, but the ownership
  residual (msg-491 §4) is what this cache retires.

Mirrors ``test_decision_material_store_singleton.py`` structurally: the
real function reference is captured at *module import time*, before any
autouse fixture in ``conftest.py`` runs; the caller of the assertions is
this file, and the fixtures cannot swap ``magickit.config.get_settings``
away by the time this ``import`` line evaluates.
"""

from __future__ import annotations

import inspect

from magickit import config as config_module

# Capture the real, ``lru_cache``-wrapped function at import time. Autouse
# fixtures fire per test, not per module import, so this reference is safe
# even if a future fixture starts swapping the module attribute.
_REAL_GET_SETTINGS = config_module.get_settings


def test_get_settings_has_a_nullary_signature() -> None:
    """Signature must remain ``() -> Settings``.

    Every consumer imports and calls it directly (``from magickit.config
    import get_settings``; ``settings = get_settings()``). A required
    argument would either break those call sites at import time or, if
    added as a keyword-only, would silently fail to be passed at the
    poll-fragment call. The test guards that boundary the same way
    ``test_get_material_store_has_a_nullary_signature`` does for the
    material-store singleton.
    """
    unwrapped = inspect.unwrap(_REAL_GET_SETTINGS)
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
        "magickit.config.get_settings must be nullary: consumers on the "
        "async request path call it directly with no arguments. Rework the "
        "cache key (or take the setting as a function-local variable) "
        "rather than adding an argument "
        "(T-get-settings-uncached-on-live-request-paths / msg-491)."
    )


def test_get_settings_is_lru_cached() -> None:
    """The prod function is wrapped by ``functools.lru_cache``.

    Failing this test signals the decorator was removed, which would
    silently re-introduce per-request ``stat`` + YAML read + parse on the
    event loop for every consumer that calls ``get_settings()``
    (T-get-settings-uncached-on-live-request-paths / msg-491 §1).
    """
    assert hasattr(_REAL_GET_SETTINGS, "cache_clear"), (
        "magickit.config.get_settings lost its lru_cache wrapper — "
        "per-request YAML stat + read + parse will return on every async "
        "handler that touches the settings. See "
        "T-get-settings-uncached-on-live-request-paths / msg-491."
    )
    assert hasattr(_REAL_GET_SETTINGS, "cache_info"), (
        "magickit.config.get_settings lost its lru_cache wrapper "
        "(cache_info missing) — same root cause as above."
    )


def test_get_settings_returns_same_instance_on_repeat_calls() -> None:
    """Two calls to the real function must return the same instance.

    Uses the captured original reference. ``cache_clear`` is invoked on
    both sides so this test does not depend on — or leak into — the cache
    slot for any other test. The function reads
    ``config/magickit_config.yaml`` relative to the process CWD (the repo
    root under ``.mindwire-gate``'s ``uv run``), which is the same path
    the deploy shell ``start.sh`` resolves against.
    """
    _REAL_GET_SETTINGS.cache_clear()
    try:
        first = _REAL_GET_SETTINGS()
        second = _REAL_GET_SETTINGS()
    finally:
        _REAL_GET_SETTINGS.cache_clear()

    assert first is second, (
        "magickit.config.get_settings must return the same instance "
        "across calls; otherwise per-request YAML stat + read + parse "
        "return on the async request path "
        "(T-get-settings-uncached-on-live-request-paths / msg-491)."
    )
