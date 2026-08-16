"""R-4: the target set is magickit's, and magickit is not in it.

The agent decides *how* to deploy, which is the point -- but that only
stays safe while the set of things it can be pointed at is finite and
reviewed. An unbounded target parameter would turn one MCP call into
"run a Claude Code agent as sgadmin against any path", which is a remote
shell with extra steps.

The self-deploy case is separate from the rest of the allowlist and
tested separately, because it fails for a different reason. Every other
name is refused because nobody has vouched for it yet; `spirrow-magickit`
is refused because the deploy would run *inside the process it is
restarting* -- the runner is launched from the MCP server, so a restart
mid-deploy kills the thing writing the audit record and leaves a request
stuck in `running` with nobody to finish it. That needs a detached
mechanism, not an allowlist entry, so it must not be fixable by editing
a set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from magickit.deploy import registry

# ── the self-deploy refusal ──────────────────────────────────────


def test_magickit_cannot_deploy_itself():
    with pytest.raises(registry.SelfDeployRefusedError) as exc:
        registry.resolve_target("spirrow-magickit")

    # The message has to say *why*, because the next person to hit it
    # will be holding a merged magickit PR and wondering what to do.
    assert "restart" in str(exc.value).lower()


def test_self_deploy_refusal_is_not_merely_absence_from_the_allowlist():
    """Adding magickit to the target table must not be enough to enable it.

    If the refusal were "it isn't in the dict", a well-meaning edit would
    switch it on. It is a distinct branch, checked first.
    """
    assert registry.SELF_TARGET == "spirrow-magickit"
    assert registry.SELF_TARGET not in registry.target_names()

    # Even if the table gained an entry, the guard still fires.
    smuggled = dict(registry._TARGETS)
    smuggled[registry.SELF_TARGET] = registry.DeployTarget(
        name=registry.SELF_TARGET,
        repo_path=Path("/home/sgadmin/services/spirrow/spirrow-magickit"),
        services=("spirrow-magickit.service",),
        health_url=None,
    )
    with pytest.raises(registry.SelfDeployRefusedError):
        registry.resolve_target(registry.SELF_TARGET, targets=smuggled)


def test_self_deploy_refusal_is_a_kind_of_target_not_allowed():
    """Callers that only catch the general error still fail closed."""
    assert issubclass(registry.SelfDeployRefusedError, registry.TargetNotAllowedError)


def test_the_refusal_does_not_claim_a_technical_impossibility():
    """It was first justified as "the restart would kill the runner". That
    was measured to be false -- the runner is a user transient unit and
    outlives the system service that launched it -- so the message says
    the real reason: a failed self-deploy takes down the tools that
    report what happened."""
    with pytest.raises(registry.SelfDeployRefusedError) as exc:
        registry.resolve_target("spirrow-magickit")

    message = str(exc.value)
    assert "deploy_status" in message
    assert "kill" not in message


# ── the rest of the allowlist ────────────────────────────────────


def test_unknown_target_is_refused():
    with pytest.raises(registry.TargetNotAllowedError):
        registry.resolve_target("spirrow-voxelworld")


def test_path_shaped_targets_are_refused():
    """The parameter names a target, never a location."""
    for attempt in (
        "/home/sgadmin/services/spirrow/spirrow-conclair",
        "../spirrow-conclair",
        "spirrow-conclair/../spirrow-magickit",
    ):
        with pytest.raises(registry.TargetNotAllowedError):
            registry.resolve_target(attempt)


def test_conclair_is_deployable_and_carries_what_the_runner_needs():
    target = registry.resolve_target("spirrow-conclair")

    assert target.repo_path == Path("/home/sgadmin/services/spirrow/spirrow-conclair")
    # Verified against the installed unit, not copied from a doc:
    # /etc/systemd/system/spirrow-conclair.service.
    assert target.services == ("spirrow-conclair.service",)
    assert target.health_url == "http://127.0.0.1:8115/health"
    # R-2: the backup has to exist before a migration can be allowed.
    assert target.backup_script == Path("scripts/backup.sh")


def test_the_other_spirrow_services_on_this_host_are_deployable():
    """Every GitHub-managed spirrow product running here, except magickit.

    Verified against the host rather than assumed: each of these has a
    `SpirrowGames/...` origin and a running unit. `rag-server` and
    `ue-investigator` also run here and are deliberately absent -- both
    were checked and are not git working trees at all, so there is no
    `origin/main` to deploy.
    """
    assert set(registry.target_names()) == {
        "spirrow-conclair",
        "spirrow-lexora",
        "spirrow-cognilens",
        "spirrow-prismind",
    }


def test_the_targets_that_hold_state_declare_a_backup_for_it():
    """lexora keeps costs.db, prismind keeps OAuth credentials and caches.

    A deploy does not touch either -- both are gitignored -- but until
    these scripts existed there was no copy of them anywhere, which is a
    gap rather than a design. Declaring the script here is what makes the
    runner take a snapshot before it pins anything.
    """
    for name in ("spirrow-lexora", "spirrow-prismind"):
        assert registry.resolve_target(name).backup_script == Path("scripts/backup.sh")


def test_cognilens_declares_none_because_it_holds_nothing():
    """The one target where "stateless" is actually true. Declaring a
    script that is not there would make it undeployable: the runner
    refuses when a declared backup is missing."""
    assert registry.resolve_target("spirrow-cognilens").backup_script is None


def test_a_declared_backup_script_exists_on_this_host():
    """The refusal for a missing script is deliberate, so a typo in this
    table would take a target offline for deploys. Skipped where the repo
    is not checked out, so CI does not depend on the host."""
    for name in registry.target_names():
        target = registry.resolve_target(name)
        if target.backup_script is None or not target.repo_path.exists():
            continue
        assert (target.repo_path / target.backup_script).exists(), name


def test_the_mcp_transport_targets_point_at_the_endpoint_that_answers():
    """Probed: /health, /healthz, /api/health and / are all 404 on these
    two; the SSE mount is what responds."""
    assert registry.resolve_target("spirrow-cognilens").health_url.endswith(":8111/sse")
    assert registry.resolve_target("spirrow-prismind").health_url.endswith(":8112/sse")
    # lexora does have a plain one, from its own uvicorn bind.
    assert registry.resolve_target("spirrow-lexora").health_url.endswith(":8110/health")


def test_every_target_declares_a_health_check_or_says_it_has_none():
    """R-7 depends on this: "did it come back up" must be answerable."""
    for name in registry.target_names():
        target = registry.resolve_target(name)
        assert target.services, f"{name} declares no service to restart"
        assert target.health_url is None or target.health_url.startswith("http")


def test_targets_are_absolute_paths_under_the_services_root():
    for name in registry.target_names():
        target = registry.resolve_target(name)
        assert target.repo_path.is_absolute()
        assert target.repo_path.parent == Path("/home/sgadmin/services/spirrow")
