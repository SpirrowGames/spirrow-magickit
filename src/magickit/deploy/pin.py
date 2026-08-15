"""Resolve a ref and put the working tree on it, before the agent runs.

R-1's second half: the agent is never handed the job of deciding what
code to deploy. By the time it starts, the tree is already on the
commit, and every git command that could move it is denied to it. So
"which code went live" is answered by magickit, in one place, and the
agent's only relationship to the ref is that it can read it.

On this host the working tree *is* production -- systemd serves
``services/spirrow/*`` directly -- so this module is not staging
anything. Moving the tree is already half the deploy, which is why a
dirty tree is refused rather than stashed: local modifications in a
production checkout are either someone's unfinished work or a previous
deploy that died, and neither is safe to roll over.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from magickit.deploy.registry import DEPLOY_REF
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: git is fast here; a fetch that hangs longer than this is a network
#: problem and should surface as one rather than stall the deploy.
GIT_TIMEOUT_S = 120.0


class PinRefusedError(Exception):
    """The tree could not be put on the requested commit, safely."""


@dataclass(frozen=True)
class PinResult:
    ref: str
    sha: str
    previous_sha: str
    detached: bool

    @property
    def changed(self) -> bool:
        return self.sha != self.previous_sha


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run one git command in ``repo`` and return its stdout, stripped.

    A hung fetch or a missing git binary comes back as
    :class:`PinRefusedError` like any other refusal. Letting
    ``TimeoutExpired`` escape would take it past the runner's handler and
    kill the runner mid-deploy, leaving the request ``running`` with no
    process behind it -- the one state this design claims cannot exist.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise PinRefusedError(
            f"git {' '.join(args)} in {repo} did not finish within {GIT_TIMEOUT_S:.0f}s"
        ) from exc
    except OSError as exc:
        raise PinRefusedError(f"could not run git {' '.join(args)} in {repo}: {exc}") from exc
    if check and proc.returncode != 0:
        raise PinRefusedError(
            f"git {' '.join(args)} failed in {repo} (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()[:400]}"
        )
    return proc.stdout.strip()


def head_sha(repo: Path) -> str:
    """The commit the tree is on right now.

    The runner reads this back *after* the deploy to fill
    ``deployed_sha``. Asking git rather than believing the agent is the
    only reason the "deployed sha == merged sha" check means anything.
    """
    return git(repo, "rev-parse", "HEAD")


def is_clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain") == ""


def _local_branch_tracking(repo: Path, remote_ref: str) -> str | None:
    """The local branch whose upstream is ``remote_ref``, if any."""
    listing = git(repo, "for-each-ref", "--format=%(refname:short) %(upstream:short)", "refs/heads")
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == remote_ref:
            return parts[0]
    return None


def pin(repo: Path, ref: str, *, default_ref: str = DEPLOY_REF, fetch: bool = True) -> PinResult:
    """Fetch, resolve ``ref``, and leave the tree on that commit.

    The default ref (``origin/main``) is applied as a fast-forward of the
    local branch that tracks it, so production stays on a branch and the
    tree looks afterwards exactly like a human running ``git merge
    --ff-only origin/main`` -- which is what the next person to log in
    will expect. Anything else is checked out detached, because an
    override is an exceptional state and should be visible as one to
    whoever looks next.

    Raises:
        PinRefusedError: dirty tree, unknown ref, non-fast-forward, or a
            post-condition that did not hold. Every failure here is
            before anything has been restarted.
    """
    if not (repo / ".git").exists():
        raise PinRefusedError(f"{repo} is not a git working tree")

    previous_sha = head_sha(repo)

    if not is_clean(repo):
        dirty = git(repo, "status", "--porcelain")
        raise PinRefusedError(
            f"{repo} has local modifications; refusing to move a dirty production "
            f"tree:\n{dirty[:600]}"
        )

    if fetch:
        git(repo, "fetch", "origin", "--prune")

    try:
        sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except PinRefusedError as exc:
        raise PinRefusedError(f"cannot resolve {ref!r} in {repo}: {exc}") from exc

    detached = False
    # Only the *default* ref lands on a branch. An override that happens
    # to live on origin (origin/feat/x) is still an override, and putting
    # production on a branch tracking it would leave the exceptional
    # state looking exactly like the normal one.
    if ref == default_ref and "/" in ref:
        branch = _local_branch_tracking(repo, ref) or ref.split("/", 1)[1]
        current = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if current != branch:
            git(repo, "checkout", branch)
        # --ff-only: if this is not a fast-forward, the local branch has
        # commits that are not on the remote, and quietly resolving that
        # during a deploy is exactly the wrong moment to be clever.
        git(repo, "merge", "--ff-only", sha)
    else:
        git(repo, "checkout", "--detach", sha)
        detached = True

    landed = head_sha(repo)
    if landed != sha:
        raise PinRefusedError(
            f"post-condition failed: asked for {sha}, tree is on {landed}. "
            "The deploy was stopped before anything was restarted."
        )
    if not is_clean(repo):
        raise PinRefusedError(
            "post-condition failed: the tree is dirty after pinning. "
            "The deploy was stopped before anything was restarted."
        )

    logger.info(
        "Pinned working tree",
        repo=str(repo),
        ref=ref,
        sha=sha,
        previous_sha=previous_sha,
        detached=detached,
    )
    return PinResult(ref=ref, sha=sha, previous_sha=previous_sha, detached=detached)


def matches_remote_main(repo: Path, sha: str, *, default_ref: str) -> bool:
    """Is ``sha`` exactly what ``default_ref`` points at right now?

    R-2's gate. Asked again at migration time rather than inferred from
    "we pinned the default ref earlier": between the pin and the
    migration, the only thing that could have changed is the remote, and
    a migration applied from a revision that is not on ``main`` is the
    failure this is here to prevent -- it forks the revision graph, and
    the next legitimate deploy inherits the fork.
    """
    try:
        remote_sha = git(repo, "rev-parse", "--verify", f"{default_ref}^{{commit}}")
    except PinRefusedError:
        return False
    return remote_sha == sha
