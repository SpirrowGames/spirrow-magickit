"""The set of things that may be deployed, and the one that may not.

R-4: the *procedure* is the agent's, the *target set* is magickit's.
Without this file the tool would be "run a Claude Code agent as sgadmin
against any path you name", which is not a deploy tool, it is a remote
shell with an approval step in front of it.

The set lives in Python rather than in ``config/magickit_config.yaml``
on purpose. It is the security boundary of this feature; changing it
should require a pull request, not an edit to a file the service
re-reads on restart.

Every field here was read off the running system, not off a document:

- ``services`` matches ``/etc/systemd/system/spirrow-conclair.service``
  (which is byte-identical to ``deploy/systemd/`` in the conclair repo).
- ``health_url`` is the loopback bind in that unit's ``ExecStart``
  (``--host 127.0.0.1 --port 8115``); conclair is not exposed directly,
  magickit is its front door.
- ``backup_script`` exists at that path and is what
  ``spirrow-conclair-backup.service`` runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: The only ref the request path can produce. R-1 is one constant and
#: one refusal, not a ref-to-permission table: a bad code-only deploy is
#: undone by putting `main` back, so the rule is cheap to hold and does
#: not deserve machinery. A human may override it at approval time, with
#: a reason, and that reason lands in the audit log.
DEPLOY_REF = "origin/main"

#: Excluded for a structural reason, not for lack of vouching --
#: see :class:`SelfDeployRefusedError`.
SELF_TARGET = "spirrow-magickit"

#: Where every deployable repo lives. Used to reject anything that
#: smells like a path rather than a name.
SERVICES_ROOT = Path("/home/sgadmin/services/spirrow")


class TargetNotAllowedError(Exception):
    """The named target is not in the allowlist."""


class SelfDeployRefusedError(TargetNotAllowedError):
    """magickit cannot deploy itself in v1.

    The runner is launched *from* the MCP server process. Restarting
    magickit mid-deploy would kill the process that holds the lock and
    writes the audit record, leaving the request stuck in ``running``
    with nobody left to finish it -- the one outcome R-7 forbids, since
    it is indistinguishable from a deploy still in flight.

    Doing this properly needs a mechanism that outlives the restart: a
    detached unit that takes the request id, performs the deploy, and
    reports afterwards, with the MCP server as a reader rather than the
    parent. That is a separate design, not an allowlist entry, which is
    why this is a distinct branch in :func:`resolve_target` and not an
    absence from the table.
    """


@dataclass(frozen=True)
class DeployTarget:
    """One deployable repository and the handles the runner needs.

    Attributes:
        name: the name callers use. Never a path.
        repo_path: absolute path to the working tree. On this host the
            working tree *is* production -- systemd serves it directly
            -- so pinning it is the deploy, and a half-pinned tree is a
            half-deployed service.
        services: systemd units the runner may restart for this target.
            The runner restarts these; the agent never does, and cannot
            (its unit forbids privilege escalation).
        health_url: what "is it back up" means for this target. ``None``
            says the target has no health endpoint, which the result
            then reports as ``health_ok=None`` rather than as success.
        backup_script: path *relative to the repo*, run before anything
            else touches the target. R-2: code comes back by putting
            `main` in again, state does not, so the snapshot is taken
            unconditionally rather than only when a migration is
            expected -- deciding whether one was coming would mean
            magickit predicting the agent's judgement.
        health_grace_s: how long to keep asking before calling it down.
            conclair's unit runs ``alembic upgrade head`` as
            ``ExecStartPre`` with ``TimeoutStartSec=120``, so a restart
            is not instant.
    """

    name: str
    repo_path: Path
    services: tuple[str, ...]
    health_url: str | None
    backup_script: Path | None = None
    health_grace_s: float = 120.0


_TARGETS: dict[str, DeployTarget] = {
    "spirrow-conclair": DeployTarget(
        name="spirrow-conclair",
        repo_path=SERVICES_ROOT / "spirrow-conclair",
        services=("spirrow-conclair.service",),
        health_url="http://127.0.0.1:8115/health",
        backup_script=Path("scripts/backup.sh"),
        health_grace_s=120.0,
    ),
}


def target_names() -> tuple[str, ...]:
    """The deployable names, sorted."""
    return tuple(sorted(_TARGETS))


def resolve_target(
    name: str,
    *,
    targets: dict[str, DeployTarget] | None = None,
) -> DeployTarget:
    """Look up a target by name, refusing anything not vouched for.

    Args:
        name: the target name. A path -- absolute, relative, or with
            ``..`` in it -- is refused rather than normalised, because a
            caller passing a path has misunderstood the parameter and
            the next thing they try should fail too.
        targets: the table to search. Only tests pass this; it exists so
            the self-deploy guard can be shown to hold even against a
            table that contains magickit.

    Raises:
        SelfDeployRefusedError: for magickit itself, checked before the table.
        TargetNotAllowedError: for everything else not in the table.
    """
    table = _TARGETS if targets is None else targets

    if name == SELF_TARGET:
        raise SelfDeployRefusedError(
            "spirrow-magickit cannot deploy itself: the runner is launched from "
            "the MCP server, so the restart would kill the process holding the "
            "lock and writing the result. This needs a detached mechanism, not "
            "an allowlist entry."
        )

    if "/" in name or name.startswith(".") or name != name.strip():
        raise TargetNotAllowedError(
            f"{name!r} looks like a path. This parameter names a target from "
            f"the allowlist: {', '.join(sorted(table))}."
        )

    try:
        return table[name]
    except KeyError:
        raise TargetNotAllowedError(
            f"{name!r} is not a deployable target. Allowed: "
            f"{', '.join(sorted(table))}. Adding one is a pull request against "
            "magickit.deploy.registry, deliberately."
        ) from None
