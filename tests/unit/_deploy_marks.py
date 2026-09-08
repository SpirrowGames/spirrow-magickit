"""Skip markers for deploy tests, keyed on the mechanism they require.

The deploy stack targets Linux + systemd + a POSIX filesystem. Running
the unit suite anywhere else (a Windows developer host, a macOS CI
worker) leaves a subset of these tests wanting a kernel primitive the
host has no equivalent for. Marking on "the mechanism I need" rather
than "the OS I dislike" is the point:

- ``sys.platform == "win32"`` is what happens to be true today. Whoever
  ports this to a different POSIX-shaped host tomorrow (macOS, WSL
  without systemd) meets a different subset of failures, and a marker
  written as "not Windows" hides that from them.
- The reason string names the primitive. When a test skips, the log
  says *why* it skipped, not just *where*.

Bohr's `T-gate-uncollectable-on-the-loop-host` msg-568 §4.3 asked for
this shape; Einstein's naysayer review of the same specification asked
that ``systemctl`` be separated from "POSIX in general" because a POSIX
host without systemd (macOS) is a real second case. Both are captured
here.
"""

from __future__ import annotations

import shutil
import sys

import pytest

# Deploy hard-codes POSIX-style absolute paths under
# ``/home/sgadmin/services`` and ``/home/sgadmin/.claude``, and
# ``paths.require_absolute`` uses ``Path.is_absolute``. On Windows a
# path that begins with ``/`` is *not* absolute (it lacks a drive
# letter), so those tests fail before the code they meant to exercise
# runs. Skip on Windows; the tests are correct on any POSIX host.
requires_posix_paths = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "these tests exercise POSIX-style absolute paths "
        "(/home/sgadmin/...) which Windows' pathlib does not treat as "
        "absolute"
    ),
)

# ``fcntl.flock`` is a POSIX system call; Windows has no equivalent
# primitive at all, and a shim that noops it silently drops the mutual
# exclusion the test is asserting on. The runtime `import fcntl` in
# ``records.target_lock`` still succeeds on Linux; only these tests are
# skipped elsewhere.
requires_real_flock = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "cross-process fcntl.flock is a POSIX mechanism; Windows has "
        "no equivalent and any shim would silently drop the exclusion"
    ),
)

# ``releases.switch`` uses ``os.replace`` on an existing symlink to
# repoint ``current`` atomically. On Windows, replacing a symlink with
# another symlink raises ``PermissionError`` even with Developer Mode
# enabled, because the operation goes through a different syscall.
requires_symlink_atomic_replace = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "atomic symlink replacement via os.replace is a POSIX filesystem "
        "operation; Windows raises PermissionError on the equivalent path"
    ),
)

# ``systemctl`` gates the tests that actually invoke systemd. On macOS
# (POSIX-shaped, no systemd) these still need to skip -- Einstein's
# advisory in msg-... on this thread called this out separately from
# POSIX-in-general.
requires_systemctl = pytest.mark.skipif(
    shutil.which("systemctl") is None,
    reason="systemctl is not on PATH; the deploy runner has nothing to talk to",
)
