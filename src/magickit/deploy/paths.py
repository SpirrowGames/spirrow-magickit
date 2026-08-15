"""Absolute paths, because systemd will not take anything else.

Every path this package hands to ``systemd-run`` has to be absolute.
``ReadWritePaths=data/deploy/runs/abc`` is not merely ignored -- measured
on this host, the transient unit refuses to start at all:

    Failed to start transient service unit: Invalid ReadWritePaths

which surfaces as "the agent wrote no report" on *every* deploy, with
nothing in the result naming the real cause. So the rule is enforced
here, at the point where paths are produced, rather than trusted at each
of the places they are consumed.
"""

from __future__ import annotations

import os
from pathlib import Path


def magickit_root() -> Path:
    """The repo root, as an absolute path."""
    override = os.environ.get("MAGICKIT_ROOT")
    if override:
        return Path(override).resolve()
    # src/magickit/deploy/paths.py -> repo root
    return Path(__file__).resolve().parents[3]


#: ``PrivateTmp=true`` gives the unit its own empty /tmp, so any path
#: under the real one simply is not there once the namespace is set up.
#: A ``WorkingDirectory`` under /tmp makes the unit die during setup with
#: exit 226 (EXIT_NAMESPACE) before a single line runs -- measured -- and
#: the deploy then reports "the agent wrote no report", blaming the agent
#: for a path it was never given.
_TMP_ROOTS = (Path("/tmp"), Path("/var/tmp"))


def require_absolute(path: Path, *, what: str) -> Path:
    """Return ``path``, or fail loudly before systemd does.

    A relative path here is a programming error whose symptom appears
    three layers away, in a failed deploy that blames the agent.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        raise ValueError(
            f"{what} must be an absolute path, got {resolved!r}. systemd refuses "
            "a transient unit with a relative ReadWritePaths, and the deploy would "
            "fail as 'the agent wrote no report'."
        )
    return resolved


def hidden_by_private_tmp(path: Path) -> bool:
    """Would ``PrivateTmp`` make this path invisible inside the unit?

    A warning rather than a refusal. Every deployable target lives under
    ``/home/sgadmin/services/spirrow``, so this cannot fire in
    production; it fires in test and development trees, where refusing
    would mean the code could not be exercised at all. The failure it
    warns about is still identified at the other end -- ``run_agent``
    reads exit 226 and says "path problem, not an agent problem" instead
    of blaming the agent for writing no report.
    """
    resolved = Path(path)
    return any(resolved == root or root in resolved.parents for root in _TMP_ROOTS)
