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
