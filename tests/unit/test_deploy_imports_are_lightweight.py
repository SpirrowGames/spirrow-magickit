"""Pin: importing the deploy modules must not require POSIX.

The bug this prevents is real and recurring. ``magickit.deploy.records``
previously did ``import fcntl`` at module scope, and
``magickit.deploy.launcher`` previously built its ``_USER_BUS_ENV`` dict
by calling ``os.getuid()`` at module scope. Both fired on import, and
because ``tests/unit/conftest.py`` -> ``magickit.web`` ->
``magickit.web.board`` -> ``magickit.deploy.records`` is a hard chain,
this meant that on any non-POSIX host the whole gate died at collection
time with ``ModuleNotFoundError: No module named 'fcntl'`` -- collect
zero, exit non-zero, ``.mindwire-gate``'s own promise that the CI and
the local loop cannot judge a change differently silently untrue.

The fix is to make both dependencies lazy: ``fcntl`` is imported inside
``target_lock``, and ``_USER_BUS_ENV`` is a function called from
``launch()``. Neither runs at import time.

The pin has to detect a regression *on every host*, including the Linux
host CI runs on. A naive ``sys.modules.pop("fcntl")`` before the
re-import is not enough: Linux has ``fcntl`` on disk, the import finds
it again, and the check silently passes even after the bad module-scope
import comes back. Setting the sys.modules entry to ``None`` is the
documented (PEP 328 / PEP 451) way to force a subsequent ``import x``
to raise ``ModuleNotFoundError`` regardless of what is on disk -- and
that is what these tests use.

Einstein's naysayer review of msg-568 flagged this specifically as a
BLOCKING correctness requirement.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


def _reload_isolated(monkeypatch, module_name: str):
    """Drop ``module_name`` from ``sys.modules`` and re-execute it.

    ``monkeypatch`` restores the previous entry (the module object as it
    was before the test) on teardown, so any other test that already had
    a reference to symbols in ``module_name`` continues to see them.
    """
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    return importlib.import_module(module_name)


def test_import_records_does_not_pull_in_fcntl(monkeypatch):
    """The regression this pins: ``magickit.deploy.records`` used to do
    ``import fcntl`` at module scope. On any non-POSIX host that killed
    every test collection -- deploy and non-deploy alike, because the
    web layer imports records transitively.

    Setting ``sys.modules["fcntl"] = None`` is the PEP 451 way to mark
    fcntl unimportable regardless of whether ``fcntl.so`` sits on disk;
    a plain ``del`` would let Linux find it again and let the test pass
    even after the regression came back.
    """
    monkeypatch.setitem(sys.modules, "fcntl", None)

    module = _reload_isolated(monkeypatch, "magickit.deploy.records")

    # And the symbols the callers actually use are still there.
    assert module.DeployStore is not None
    assert module.DeployRequest is not None


def test_import_launcher_does_not_call_getuid(monkeypatch):
    """The regression this pins: ``launcher._USER_BUS_ENV`` used to be
    a dict literal with two ``os.getuid()`` calls in it, evaluated at
    module import. ``os.getuid`` is POSIX-only, and its absence on
    Windows killed the collection the same way ``fcntl`` did.

    ``raising=False`` because on non-POSIX hosts ``os.getuid`` may not
    exist to begin with; monkeypatch inserts it as raising anyway, and
    a module-scope call would trip that.
    """

    def _fail(*_a, **_kw):
        raise AssertionError(
            "os.getuid() was called at import time; _USER_BUS_ENV must stay lazy"
        )

    monkeypatch.setattr(os, "getuid", _fail, raising=False)

    module = _reload_isolated(monkeypatch, "magickit.deploy.launcher")

    # ``launch`` is what the MCP server calls; the lazy accessor exists.
    assert module.launch is not None
    assert callable(module._user_bus_env)


def test_import_launcher_does_not_pull_in_fcntl(monkeypatch):
    """Belt-and-braces: the launcher must not indirectly re-introduce
    a module-scope fcntl either. Same PEP 451 technique."""
    monkeypatch.setitem(sys.modules, "fcntl", None)

    module = _reload_isolated(monkeypatch, "magickit.deploy.launcher")

    assert module.launch is not None


def test_the_pin_actually_bites(monkeypatch):
    """Meta-check: setting ``sys.modules["fcntl"] = None`` does force a
    later ``import fcntl`` to raise ``ModuleNotFoundError``. If this
    test ever passes without raising, the pin above has become a
    silent no-op on this host (Python removed the behaviour, or a shim
    is intercepting the import) and needs a different technique.

    Written as a separate test because a broken pin should surface as
    ``the pin is broken``, not ``the pin claims all is well``.
    """
    monkeypatch.setitem(sys.modules, "fcntl", None)

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("fcntl")
