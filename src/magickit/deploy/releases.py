"""Two release directories and a symlink, for targets that use them.

The problem this solves is not rollback speed. On this host the working
tree *is* production -- systemd serves ``services/spirrow/*`` directly --
so a ``git merge`` in place makes templates and static files new while
the running Python is still old, and the service serves that mixture
until it is restarted. Measured on cognilens: swapping the symlink
instead leaves the running process on the directory it already opened,
so it keeps serving one consistent version until the restart, and the
new process starts on the new one.

The layout, as built on the host::

    services/spirrow/
      spirrow-cognilens -> releases/spirrow-cognilens/current
      releases/spirrow-cognilens/
          a/  b/      full checkouts, each with its own venv
          shared/     what must not be swapped
          current -> a

``services/spirrow/<name>`` stays the path every systemd unit names, so
adopting this needs **no unit file changes at all** -- which matters,
because rewriting ``WorkingDirectory`` / ``ExecStart`` /
``EnvironmentFile`` across every unit was the largest single risk in
doing this.

Two slots, not N. You only ever go back one step; a release two deploys
old is already invalid against the database schema, and more slots buy
bookkeeping rather than safety.

What this does **not** do is make a migration reversible. Both slots
talk to the same database. Switching back restores code and nothing
else, which is the same limitation a load balancer would have, and the
reason R-2 does not get any easier here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

SLOTS = ("a", "b")
SHARED_DIR = "shared"
CURRENT_LINK = "current"


class ReleaseLayoutError(Exception):
    """The release directory is not in a state a deploy can reason about."""


@dataclass(frozen=True)
class Slots:
    """Which directory is live and which one the next deploy prepares."""

    root: Path
    current: str
    standby: str

    @property
    def current_path(self) -> Path:
        return self.root / self.current

    @property
    def standby_path(self) -> Path:
        return self.root / self.standby

    @property
    def shared_path(self) -> Path:
        return self.root / SHARED_DIR


def resolve(root: Path) -> Slots:
    """Read the layout, or say exactly what is wrong with it.

    Refusing loudly here is the point: every later step -- the pin, the
    agent, the switch -- assumes it knows which directory is live, and a
    wrong answer means preparing the *running* one in place, which is
    the failure this whole layout exists to prevent.
    """
    root = Path(root)
    if not root.is_dir():
        raise ReleaseLayoutError(f"{root} is not a directory")

    link = root / CURRENT_LINK
    if not link.is_symlink():
        raise ReleaseLayoutError(f"{link} is not a symlink; the layout is not set up")

    current = os.readlink(link).strip("/")
    if current not in SLOTS:
        raise ReleaseLayoutError(
            f"{link} points at {current!r}, which is not one of {SLOTS}. Refusing to "
            "guess which directory is live."
        )

    for slot in SLOTS:
        if not (root / slot).is_dir():
            raise ReleaseLayoutError(f"{root / slot} is missing; both slots must exist")
    if not (root / SHARED_DIR).is_dir():
        raise ReleaseLayoutError(f"{root / SHARED_DIR} is missing")

    standby = next(slot for slot in SLOTS if slot != current)
    return Slots(root=root, current=current, standby=standby)


def switch(root: Path, slot: str) -> None:
    """Point ``current`` at ``slot``, atomically.

    ``ln -sfn`` on an existing symlink is not atomic -- it unlinks and
    recreates, and a start in that window finds no path at all. Creating
    a temporary link and ``rename``-ing it over the old one is a single
    syscall, so the service either sees the old release or the new one
    and never sees nothing.
    """
    root = Path(root)
    if slot not in SLOTS:
        raise ReleaseLayoutError(f"{slot!r} is not one of {SLOTS}")
    if not (root / slot).is_dir():
        raise ReleaseLayoutError(f"{root / slot} does not exist")

    tmp = root / f"{CURRENT_LINK}.swap"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(slot)
    os.replace(tmp, root / CURRENT_LINK)
    logger.info("Switched release", root=str(root), slot=slot)


def describe(root: Path) -> str:
    try:
        slots = resolve(root)
    except ReleaseLayoutError as exc:
        return f"release layout unreadable: {exc}"
    return f"current={slots.current} standby={slots.standby}"
