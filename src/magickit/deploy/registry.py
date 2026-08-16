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
    """magickit does not deploy itself. An operational choice, not a limit.

    This was first written down as a technical impossibility -- "the
    runner is launched from the MCP server, so restarting magickit would
    kill the process holding the lock". That reasoning was wrong, and
    saying so matters more than quietly keeping the same rule: the
    runner is a *user* transient unit under
    ``user@1000.service/app.slice``, not a child of the MCP server's
    system unit. Measured: stopping the launching system service leaves
    the runner running. A self-deploy would in fact complete and record
    its result.

    It stays refused for a different, smaller reason. When magickit
    deploys magickit and the deploy goes wrong, the tool that reports
    what went wrong is the thing that is down: ``deploy_status`` and
    ``deploy_history`` both answer from the MCP server being restarted.
    The record survives on disk, but reading it means reaching the host
    -- which is the exact dependency this whole feature exists to
    remove. Every other target keeps its reporting path intact when its
    deploy fails; magickit is the one that cannot.

    So: a deliberate carve-out, checked before the table so that adding
    an entry cannot switch it on by accident. Lifting it needs a
    reporting path that does not depend on the service being deployed --
    not an allowlist edit.
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
    #: Set when this target uses two release directories and a symlink
    #: (see :mod:`magickit.deploy.releases`). ``repo_path`` stays the
    #: path systemd names either way -- for a release target it is the
    #: stable symlink -- so nothing about the units changes. What
    #: changes is where the deploy *works*: the standby slot, while the
    #: live one keeps serving.
    #:
    #: ``None`` means the older in-place behaviour: pin the live tree
    #: and restart. Both are supported on purpose, so targets can be
    #: converted one at a time rather than in one move.
    releases_root: Path | None = None

    @property
    def uses_releases(self) -> bool:
        return self.releases_root is not None


_TARGETS: dict[str, DeployTarget] = {
    "spirrow-conclair": DeployTarget(
        name="spirrow-conclair",
        repo_path=SERVICES_ROOT / "spirrow-conclair",
        services=("spirrow-conclair.service",),
        health_url="http://127.0.0.1:8115/health",
        backup_script=Path("scripts/backup.sh"),
        health_grace_s=120.0,
        releases_root=SERVICES_ROOT / "releases" / "spirrow-conclair",
    ),
    # None of the three runs migrations, so a bad deploy of them is
    # undone by putting `main` back -- the case R-1 was sized for.
    #
    # They are not *stateless*, though, and an earlier version of this
    # comment said they were. lexora keeps `data/costs.db` (live, ~1MB
    # of cost records with nowhere to re-derive them from) and prismind
    # keeps `config.toml`, `credentials.json`, `token.json` and two
    # caches. A deploy does not touch any of it -- all gitignored, so
    # pinning leaves it alone -- but "nothing here would be lost" is a
    # different claim from "nothing here exists", and only the first was
    # ever true. Both now ship a backup script, so the second is covered
    # too.
    #
    # All three also ignore the `start.sh` that systemd executes, so the
    # deployed sha does not describe how the service actually starts.
    # See `pin.would_silently_overwrite` for the accident that follows
    # from trying to fix that carelessly.
    "spirrow-lexora": DeployTarget(
        name="spirrow-lexora",
        repo_path=SERVICES_ROOT / "spirrow-lexora",
        services=("spirrow-lexora.service",),
        # Plain HTTP health, from the unit's own uvicorn bind
        # (`--host 0.0.0.0 --port 8110`). Known to stall for 20-30s on
        # occasion, which is what the long grace and the per-attempt
        # timeout in the runner are sized for.
        health_url="http://127.0.0.1:8110/health",
        backup_script=Path("scripts/backup.sh"),
        health_grace_s=180.0,
        releases_root=SERVICES_ROOT / "releases" / "spirrow-lexora",
    ),
    "spirrow-cognilens": DeployTarget(
        name="spirrow-cognilens",
        repo_path=SERVICES_ROOT / "spirrow-cognilens",
        services=("spirrow-cognilens.service",),
        # No plain health endpoint: /health, /healthz, /api/health and /
        # are all 404 (probed). What answers is the MCP SSE mount, so
        # liveness is "the transport is up", which is what a restart
        # verification needs. The runner reads only the response headers
        # -- reading the body of an SSE endpoint never returns.
        health_url="http://127.0.0.1:8111/sse",
        health_grace_s=120.0,
        # The pilot for the two-slot layout, converted first because it
        # holds no state at all -- its only shared file is the
        # gitignored start.sh -- so a mistake could not lose anything.
        # The other three followed once it had run four deploys clean.
        releases_root=SERVICES_ROOT / "releases" / "spirrow-cognilens",
    ),
    "spirrow-prismind": DeployTarget(
        name="spirrow-prismind",
        repo_path=SERVICES_ROOT / "spirrow-prismind",
        services=("spirrow-prismind.service",),
        # Same shape as cognilens; this one is an `mcp-proxy` in front of
        # the server, so /mcp answers 400 to a bare GET and /sse is the
        # honest liveness signal.
        health_url="http://127.0.0.1:8112/sse",
        backup_script=Path("scripts/backup.sh"),
        health_grace_s=120.0,
        releases_root=SERVICES_ROOT / "releases" / "spirrow-prismind",
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
            "spirrow-magickit does not deploy itself. The deploy would complete -- "
            "the runner outlives the restart -- but the tools that report what "
            "happened (deploy_status, deploy_history) are served by the process "
            "being restarted, so a failed self-deploy is the one case where "
            "reading the result means reaching the host. Lifting this needs a "
            "reporting path independent of magickit, not an allowlist entry."
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
