"""R-5: what the agent is given, and what it is not.

The confinement is the systemd unit, not the prompt and not the deny
list, so that is what these tests assert on: the argv the runner builds.
If ``NoNewPrivileges`` or the ``ReadWritePaths`` pair ever fall out of
that command line, the agent silently becomes an unconfined process
running as a user with passwordless sudo -- which is the failure mode
with no visible symptom until it matters.

The Claude Code deny rules are checked too, at a lower stake. They are a
guardrail: an enumeration of what is forbidden, in a shell that has
unbounded ways to spell things. They earn their place because denials
come back in the run's JSON and land in the audit record, so an agent
that reached for a ref-moving command is something we get to see.
Nothing here depends on them holding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magickit.deploy import agent as agent_mod
from magickit.deploy.registry import DeployTarget


@pytest.fixture
def target(tmp_path) -> DeployTarget:
    return DeployTarget(
        name="spirrow-conclair",
        repo_path=tmp_path / "repo",
        services=("spirrow-conclair.service",),
        health_url="http://127.0.0.1:8115/health",
        backup_script=Path("scripts/backup.sh"),
    )


def _argv(target, tmp_path, **overrides):
    prompt = tmp_path / "p.txt"
    prompt.write_text("brief")
    kwargs = dict(
        target=target,
        scratch=tmp_path / "scratch",
        unit="magickit-deploy-agent-abc",
        prompt_file=prompt,
        report_path=tmp_path / "report.json",
        migration_allowed=True,
        read_only=False,
    )
    kwargs.update(overrides)
    return agent_mod.build_argv(**kwargs)


def _settings(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])


# ── the boundary that is real ────────────────────────────────────


def test_the_agent_unit_cannot_escalate_privilege(target, tmp_path):
    argv = _argv(target, tmp_path)
    assert "--property=NoNewPrivileges=true" in argv


def test_the_agent_can_write_only_the_repo_and_its_own_scratch(target, tmp_path):
    argv = _argv(target, tmp_path)

    assert "--property=ProtectHome=read-only" in argv
    assert "--property=ProtectSystem=strict" in argv

    prefix = "--property=ReadWritePaths="
    writable = {a[len(prefix) :] for a in argv if a.startswith(prefix)}
    assert str(target.repo_path) in writable
    assert str(tmp_path / "scratch") in writable
    # Claude Code keeps session state under $HOME/.claude and cannot run
    # without it; nothing else in home is writable.
    assert str(agent_mod.CLAUDE_STATE_DIR) in writable
    assert len(writable) == 3


def test_the_agent_is_bounded_in_memory_and_confined_to_the_repo(target, tmp_path):
    argv = _argv(target, tmp_path)
    assert f"--property=MemoryMax={agent_mod.AGENT_MEMORY_MAX}" in argv
    assert "--property=PrivateTmp=true" in argv
    assert f"--working-directory={target.repo_path}" in argv


def test_it_is_a_system_unit_because_user_units_do_not_get_sandboxed(target, tmp_path):
    """Measured on this host: --user silently drops these properties."""
    argv = _argv(target, tmp_path)
    assert argv[:3] == ["sudo", "-n", "systemd-run"]
    assert "--system" in argv
    assert "--user" not in argv


def test_the_agent_gets_no_mcp_servers(target, tmp_path):
    """It must not be able to reach magickit's own tools and approve itself."""
    argv = _argv(target, tmp_path)
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_the_agent_can_write_its_report_outside_the_repo(target, tmp_path):
    """Claude Code refuses writes outside its working directories even
    when the filesystem allows them. Measured: without this the agent
    completes the deploy and then cannot tell anyone, which the runner
    is obliged to read as a failure."""
    argv = _argv(target, tmp_path)
    assert argv[argv.index("--add-dir") + 1] == str(tmp_path / "scratch")


# ── the guardrail layer ──────────────────────────────────────────


def test_the_tools_are_allowed_wholesale_so_the_deny_list_is_the_rule(target, tmp_path):
    """Deny-list semantics, and they have to be asked for explicitly.

    With no `allow`, a headless run has nobody to confirm each Bash call
    and refuses all of them -- measured: a smoke run denied `git
    ls-tree`, `printenv` and `python3 -c` alike. It also has to be this
    way for the design to work: an agent that is supposed to work out
    an unfamiliar repo's procedure will run commands nobody listed in
    advance.
    """
    allow = _settings(_argv(target, tmp_path))["permissions"]["allow"]
    assert "Bash" in allow
    assert {"Read", "Write", "Edit", "Glob", "Grep"} <= set(allow)


def test_ref_moving_git_commands_are_denied(target, tmp_path):
    deny = _settings(_argv(target, tmp_path))["permissions"]["deny"]

    for verb in ("checkout", "merge", "reset", "fetch", "pull", "push", "rebase"):
        assert f"Bash(git {verb}:*)" in deny


def test_read_only_git_is_left_alone(target, tmp_path):
    """The agent has to be able to see what it is deploying."""
    deny = _settings(_argv(target, tmp_path))["permissions"]["deny"]

    for verb in ("log", "show", "diff", "status", "rev-parse"):
        assert f"Bash(git {verb}:*)" not in deny


def test_privileged_commands_are_denied_so_attempts_are_visible(target, tmp_path):
    deny = _settings(_argv(target, tmp_path))["permissions"]["deny"]

    assert "Bash(sudo:*)" in deny
    assert "Bash(systemctl:*)" in deny


def test_migrations_are_denied_when_the_gate_is_shut(target, tmp_path):
    open_gate = _settings(_argv(target, tmp_path, migration_allowed=True))["permissions"]["deny"]
    shut_gate = _settings(_argv(target, tmp_path, migration_allowed=False))["permissions"]["deny"]

    assert not [rule for rule in open_gate if "alembic" in rule]
    assert "Bash(alembic:*)" in shut_gate
    assert "Bash(.venv/bin/alembic:*)" in shut_gate


def test_the_diagnosis_pass_runs_in_a_mode_that_cannot_change_anything(target, tmp_path):
    argv = _argv(target, tmp_path, read_only=True)
    assert argv[argv.index("--permission-mode") + 1] == "plan"

    argv = _argv(target, tmp_path, read_only=False)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


# ── the brief ────────────────────────────────────────────────────


def test_the_brief_tells_the_agent_the_tree_is_already_pinned(target):
    brief = agent_mod.render_prepare_brief(
        target=target,
        sha="a" * 40,
        ref="origin/main",
        default_ref="origin/main",
        migration_allowed=True,
        migration_block_reason="",
        backup_path="/backups/x.dump.gz",
    )

    assert "a" * 40 in brief
    assert "ALREADY on that commit" in brief
    # Q-4: stopping is the required behaviour, and it is stated as such.
    assert "IF YOU CANNOT DETERMINE THE PROCEDURE" in brief
    assert "do not guess" in brief.lower()
    # R-2: it must not think it has to take its own snapshot.
    assert "/backups/x.dump.gz" in brief


def test_a_shut_migration_gate_is_explained_in_the_brief(target):
    brief = agent_mod.render_prepare_brief(
        target=target,
        sha="a" * 40,
        ref="fix/x",
        default_ref="origin/main",
        migration_allowed=False,
        migration_block_reason="the tree is not what origin/main points at",
        backup_path=None,
    )

    assert "NOT allowed" in brief
    assert "origin/main points at" in brief
    assert "stopping condition" in brief


def test_a_missing_report_is_a_failed_deploy_not_a_silent_one(target, tmp_path, monkeypatch):
    """R-6/R-7: no report means no information, and no information is failure."""

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(agent_mod.subprocess, "run", lambda *a, **k: _Proc())

    outcome = agent_mod.run_agent(
        target=target,
        scratch=tmp_path / "scratch",
        unit="magickit-deploy-agent-abc",
        brief="hi",
        migration_allowed=True,
    )

    assert outcome.ok is False
    assert "wrote no report" in outcome.error
    assert "NOT restarted" in outcome.error


def test_a_report_claiming_success_with_a_nonzero_exit_is_not_believed(
    target, tmp_path, monkeypatch
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    report = scratch / "magickit-deploy-agent-abc.report.json"

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = ""

    def fake_run(*args, **kwargs):
        report.write_text(json.dumps({"ok": True, "summary": "all good"}))
        return _Proc()

    monkeypatch.setattr(agent_mod.subprocess, "run", fake_run)

    outcome = agent_mod.run_agent(
        target=target,
        scratch=scratch,
        unit="magickit-deploy-agent-abc",
        brief="hi",
        migration_allowed=True,
    )

    assert outcome.ok is False


def test_denials_are_extracted_for_the_audit_record():
    stdout = json.dumps(
        {
            "permission_denials": [
                {"tool_name": "Bash", "tool_input": {"command": "git checkout main"}},
                {"tool_name": "Write", "tool_input": {}},
            ]
        }
    )

    denials = agent_mod._denials_from_cli_json(stdout)

    assert denials == ["Bash: git checkout main", "Write"]


def test_no_denials_when_the_cli_said_nothing_parseable():
    assert agent_mod._denials_from_cli_json("not json at all") == []
