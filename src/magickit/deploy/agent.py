"""Launching the Claude Code agent that performs the deploy.

Why an agent at all: the deploy of a given repo is knowledge that lives
in that repo -- whether it needs ``uv sync``, whether it has migrations,
what order they go in, what its own CLAUDE.md says. Copying that into
magickit would centralise a set of facts magickit has no way to keep
current. The agent reads the repo it is standing in, and can say why it
failed, which a fixed script cannot.

**How it is confined (R-5).** The agent runs in its own *system*
transient unit, and the confinement is the unit, not the prompt:

- ``NoNewPrivileges=true`` -- ``sudo`` cannot escalate at all. This is
  the load-bearing one. sgadmin has ``NOPASSWD: ALL``, so any agent
  running unconfined as sgadmin is root for practical purposes, and no
  list of denied commands changes that. Measured: inside such a unit
  ``sudo -n true`` fails with "the no new privileges flag is set".
- ``ProtectHome=read-only`` plus ``ReadWritePaths`` for exactly the
  target repo and this run's scratch directory -- the agent cannot
  modify another service's working tree, which on this host means it
  cannot deploy anything it was not pointed at. Measured: a write to a
  sibling repo fails with EROFS.
- ``PrivateTmp``, ``MemoryMax``, and a wall-clock timeout.

Note ``systemd-run --user`` was measured *not* to apply these -- the
sandboxing options are silently dropped for user-manager units on this
host -- so the agent unit is a ``--system`` unit started with sudo by
the runner, which is outside the MCP server's sandbox and can. The
global "long-running processes must be transient units with MemoryMax"
rule is honoured either way; only the scope differs, and it differs
because the user scope does not actually confine.

On top of that the Claude Code permission layer denies git ref-moving
commands and ``systemctl``. That layer is a *guardrail*, not a boundary:
a deny list enumerates what is forbidden and a shell has infinite ways
to spell things. It is worth having because denials are reported back in
the run's JSON and land in the audit record -- an agent that tried to
move a ref is a thing we want to see -- but nothing here depends on it
holding. What the deploy cannot survive (privilege, other repos) is
denied by the kernel.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from magickit.deploy.paths import hidden_by_private_tmp, require_absolute
from magickit.deploy.registry import DeployTarget
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

CLAUDE_BIN = os.environ.get("MAGICKIT_DEPLOY_CLAUDE_BIN", "/home/sgadmin/.local/bin/claude")
AGENT_TIMEOUT_S = float(os.environ.get("MAGICKIT_DEPLOY_AGENT_TIMEOUT_S", "900"))
AGENT_MEMORY_MAX = os.environ.get("MAGICKIT_DEPLOY_AGENT_MEMORY_MAX", "4G")

#: Claude Code keeps session state under ``$HOME/.claude``; the unit is
#: otherwise ``ProtectHome=read-only``, so it needs this one hole. The
#: agent already runs as sgadmin and can read the credentials there
#: regardless -- it needs them to authenticate -- so the hole grants no
#: access it did not already have.
CLAUDE_STATE_DIR = Path(os.environ.get("MAGICKIT_DEPLOY_CLAUDE_STATE", "/home/sgadmin/.claude"))

#: Moving a ref is magickit's job and was already done before the agent
#: started (see :mod:`magickit.deploy.pin`). Read-only git is left
#: alone: the agent needs ``log``/``show``/``diff``/``status`` to work
#: out what changed and therefore what the deploy needs.
_GIT_REF_DENIES = tuple(
    f"Bash(git {verb}:*)"
    for verb in (
        "checkout",
        "switch",
        "merge",
        "rebase",
        "reset",
        "pull",
        "fetch",
        "push",
        "branch",
        "tag",
        "cherry-pick",
        "revert",
        "stash",
        "clean",
        "worktree",
        "remote",
    )
)

#: Restarting is the runner's step, not the agent's (and the unit makes
#: it impossible anyway). Denying it here turns an attempt into a
#: reported denial instead of a confusing sudo error in the transcript.
#:
#: Only the *mutating* systemctl verbs. A blanket ``Bash(systemctl:*)``
#: contradicted the diagnosis brief, which tells the agent to look at
#: ``systemctl status`` -- the agent caught this itself on a smoke run
#: ("the brief instructs the agent to use systemctl status, but the
#: sandbox denies Bash(systemctl:*) outright"), having to diagnose with
#: one hand tied. Reading unit state is the whole job of that pass, and
#: it needs no privilege; changing unit state needs sudo, which is
#: denied here and impossible under ``NoNewPrivileges`` regardless.
_PRIVILEGE_DENIES = (
    "Bash(sudo:*)",
    "Bash(systemd-run:*)",
    "Bash(su:*)",
) + tuple(
    f"Bash(systemctl {verb}:*)"
    for verb in (
        "start",
        "stop",
        "restart",
        "reload",
        "try-restart",
        "kill",
        "enable",
        "disable",
        "mask",
        "unmask",
        "isolate",
        "edit",
        "set-property",
        "daemon-reload",
    )
)

#: Only added when the migration gate is shut. Prevention here is
#: best-effort -- there are more ways to spell "run alembic" than can be
#: listed -- which is why the runner also reads the alembic revision
#: before and after and fails the deploy loudly if it moved when it was
#: not allowed to. See :mod:`magickit.deploy.runner`.
_MIGRATION_DENIES = (
    "Bash(alembic:*)",
    "Bash(.venv/bin/alembic:*)",
    "Bash(uv run alembic:*)",
    "Bash(python -m alembic:*)",
    "Bash(python3 -m alembic:*)",
)


@dataclass
class AgentOutcome:
    """What the agent phase produced, as the runner sees it."""

    ok: bool
    summary: str = ""
    steps: list[dict] = field(default_factory=list)
    denials: list[str] = field(default_factory=list)
    migration_applied: bool | None = None
    undetermined: bool = False
    error: str | None = None
    exit_code: int | None = None


#: Deny-list semantics, stated explicitly. Without an ``allow`` entry the
#: agent is asked to confirm every Bash command, and in a headless run
#: there is nobody to ask, so *everything* is refused -- measured: a
#: smoke run denied `git ls-tree`, `printenv` and `python3 -c` alike and
#: the agent could not even write its own report.
#:
#: Allowing the tools wholesale and denying the dangerous spellings is
#: also the only configuration compatible with the design: the agent is
#: supposed to work out this repo's procedure, which means running
#: commands nobody listed in advance. The boundary that stops it doing
#: harm is the unit it runs in, not this list.
_ALLOWED_TOOLS = ("Bash", "Read", "Write", "Edit", "Glob", "Grep")


def _settings_json(*, migration_allowed: bool) -> str:
    deny = list(_GIT_REF_DENIES) + list(_PRIVILEGE_DENIES)
    if not migration_allowed:
        deny += list(_MIGRATION_DENIES)
    # deny wins over allow, so the broad allow does not reopen anything
    # named above.
    return json.dumps({"permissions": {"allow": list(_ALLOWED_TOOLS), "deny": deny}})


def _unit_properties(*, target: DeployTarget, scratch: Path, read_only: bool) -> list[str]:
    """The sandbox. ``read_only`` drops the repo from the writable set.

    The diagnosis pass has to be unable to change the thing it is
    describing, and that cannot be expressed as a permission mode: its
    one deliverable is a file write, so a mode that refuses writes
    refuses the report too (measured -- ``--permission-mode plan``
    produced a plan document and waited for an approval that never came,
    so every diagnosis came back as "the agent wrote no report ... the
    service was NOT restarted", which is false in exactly the case a
    diagnosis is asked for). Read-only therefore means the filesystem:
    it keeps its scratch directory and loses the repo.
    """
    writable = [scratch, CLAUDE_STATE_DIR]
    if not read_only:
        writable.insert(0, target.repo_path)

    properties = [
        "--property=NoNewPrivileges=true",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=read-only",
    ]
    for path in writable:
        absolute = require_absolute(path, what="the agent unit's ReadWritePaths")
        if hidden_by_private_tmp(absolute):
            logger.warning(
                "Path is under /tmp and PrivateTmp will hide it from the agent unit; "
                "the unit will fail namespace setup (exit 226)",
                path=str(absolute),
            )
        properties.append(f"--property=ReadWritePaths={absolute}")
    properties += [
        "--property=PrivateTmp=true",
        f"--property=MemoryMax={AGENT_MEMORY_MAX}",
    ]
    return properties


def build_argv(
    *,
    target: DeployTarget,
    scratch: Path,
    unit: str,
    prompt_file: Path,
    report_path: Path,
    migration_allowed: bool,
    read_only: bool,
) -> list[str]:
    """The full ``sudo systemd-run`` command line for one agent phase."""
    argv = [
        "sudo",
        "-n",
        "systemd-run",
        "--system",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={unit}",
        "--uid=sgadmin",
        "--gid=sgadmin",
        "--setenv=HOME=/home/sgadmin",
        "--setenv=PATH=/home/sgadmin/.local/bin:/usr/local/bin:/usr/bin:/bin",
        f"--setenv=DEPLOY_REPORT_PATH="
        f"{require_absolute(report_path, what='DEPLOY_REPORT_PATH')}",
        f"--working-directory="
        f"{require_absolute(target.repo_path, what='the target repo path')}",
        *_unit_properties(target=target, scratch=scratch, read_only=read_only),
        CLAUDE_BIN,
        "-p",
        # The report lives outside the repo, and Claude Code refuses to
        # write outside its working directories regardless of what the
        # filesystem allows. Without this the agent finishes the deploy
        # and then cannot tell anyone -- which the runner is obliged to
        # read as a failure.
        "--add-dir",
        str(scratch),
        # acceptEdits, not manual and not plan: both refuse writes (there
        # is nobody to ask), which would make the phase a no-op -- and
        # the report is itself a write, so even the read-only diagnosis
        # pass needs a mode that can write. What makes that pass
        # read-only is its unit, which does not include the repo in
        # ReadWritePaths. The boundary is the sandbox, not the mode.
        "--permission-mode",
        "acceptEdits",
        # No MCP servers. The agent must not be able to reach magickit's
        # own tools -- approving or re-requesting its own deploy is the
        # obvious hazard, and it has no need for them.
        "--strict-mcp-config",
        "--settings",
        _settings_json(migration_allowed=migration_allowed),
        "--output-format",
        "json",
    ]
    model = os.environ.get("MAGICKIT_DEPLOY_AGENT_MODEL")
    if model:
        argv += ["--model", model]
    argv += [prompt_file.read_text(encoding="utf-8")]
    return argv


PREPARE_BRIEF = """\
You are performing a deploy on sg-ai-server-01. You are running inside a
locked-down systemd unit; this is deliberate and is not something to work
around.

TARGET: {name}
REPO:   {repo}
COMMIT: {sha} (ref {ref})

The working tree is ALREADY on that commit. magickit pinned it before
starting you. Do not run any git command that moves a ref (checkout,
merge, fetch, pull, reset, ...) -- they are denied, and the deploy's
whole guarantee is that the commit you see is the commit that was
approved. Read-only git is fine and encouraged.

YOUR JOB: get this repository's code ready to serve at that commit --
everything the deploy needs EXCEPT restarting the service.

Read the repo to work out what that means: its CLAUDE.md, its
pyproject/lockfile, its migration directory, its own docs. Do not assume
a procedure; this repo's procedure is whatever the repo says it is.

WHAT YOU MUST NOT DO:
- Do not restart, start, or stop any service. You cannot (sudo is
  unavailable to you by kernel policy) and you do not need to: magickit
  restarts the service itself after you finish, and then checks health.
- Do not modify any file outside {repo}. You cannot; the filesystem is
  read-only outside it.
- Do not move git refs.

MIGRATIONS: {migration_clause}

BACKUP: {backup_clause}

BEFORE YOU FINISH, PROVE THE CODE AT LEAST LOADS. magickit restarts the
service after you return, and a restart is the expensive way to discover
an import error or a missing dependency. Work out what a cheap,
side-effect-free check is for *this* repo -- usually importing the
module the service entry point names, or whatever the repo itself
suggests -- and run it. Record it as a step.

Two rules about that check, and they matter more than the check itself:

- **It must not touch production state.** Do not start the real service.
  Do not run migrations to "see if they work". Be aware that some
  services in this platform apply migrations from inside the application
  at startup, so merely booting them writes to the live database -- if
  that is true here, booting is not a test, it is the deploy happening
  early and unsupervised.
- **If you cannot check it without side effects, do not check it.** Say
  so in the report and let the restart be the test. An honest "I could
  not verify this safely" is a fine outcome; a verification that quietly
  changed something is not.

IF YOU CANNOT DETERMINE THE PROCEDURE: stop. Do not guess, do not try a
plausible-looking command to see what happens, and do not do "the usual
thing". Write the report with "undetermined": true and say exactly what
you looked at and what was ambiguous. A deploy that stops with nothing
done is a good outcome; a deploy that half-happened because you guessed
is the outcome this whole mechanism exists to prevent.

WHEN DONE, write your report to the file named by $DEPLOY_REPORT_PATH as
JSON with exactly these keys:

  {{"ok": true|false,
    "summary": "one or two sentences, what you did",
    "undetermined": true|false,
    "migration_applied": true|false|null,
    "steps": [{{"name": "...", "ok": true|false, "detail": "..."}}]}}

"ok" means "the repo is ready to be restarted at this commit". If you
did not get there, "ok" is false -- do not report success for partial
work. Write the file even when you fail; a missing report is treated as
a failed deploy with no information, which helps nobody.
"""

MIGRATION_ALLOWED_CLAUSE = """\
allowed. This commit is exactly what {default_ref} points at, so applying
its migrations cannot fork the revision graph. A database snapshot was
already taken before you started -- you do not need to take one, and you
should not skip a needed migration on the grounds that you did not take
one. Apply migrations the way this repo says to apply them. Do NOT run a
downgrade; if a migration will not apply, stop and report it."""

MIGRATION_BLOCKED_CLAUSE = """\
NOT allowed for this run{why}. Do not run alembic (or any other
migration tool), in any form. If this commit needs a migration to work,
that is a stopping condition: write the report with "ok": false and say
so plainly. Deploying code that needs a migration without the migration
is worse than not deploying."""

DIAGNOSE_BRIEF = """\
A deploy on sg-ai-server-01 has just gone wrong and you are being asked
to explain it, not to fix it. You are read-only: make no changes.

TARGET:  {name}
REPO:    {repo}
COMMIT:  {sha}
SERVICE: {services}

WHAT MAGICKIT OBSERVED:
{observed}

Work out what happened. Useful places: `systemctl status` and
`journalctl -u <unit> --since` for the units above (reading is
permitted), the repo at the commit above, its logs, its config, recent
commits. Say whether the previous version is still serving or nothing
is, if you can tell.

Write your answer to $DEPLOY_REPORT_PATH as JSON:

  {{"ok": false,
    "summary": "what went wrong, in a few sentences, and what a human
                should check or do first",
    "undetermined": false,
    "steps": []}}

If you cannot tell, say you cannot tell and say what you ruled out. A
confident wrong diagnosis costs more than an honest "I do not know" --
someone will act on this at 2am.
"""


def render_prepare_brief(
    *,
    target: DeployTarget,
    sha: str,
    ref: str,
    default_ref: str,
    migration_allowed: bool,
    migration_block_reason: str,
    backup_path: str | None,
) -> str:
    if migration_allowed:
        migration_clause = MIGRATION_ALLOWED_CLAUSE.format(default_ref=default_ref)
    else:
        why = f" ({migration_block_reason})" if migration_block_reason else ""
        migration_clause = MIGRATION_BLOCKED_CLAUSE.format(why=why)

    if backup_path:
        backup_clause = (
            f"a snapshot was taken before you started: {backup_path}. "
            "You do not need to take another."
        )
    else:
        backup_clause = (
            "this target declares no backup script, so no snapshot was taken. "
            "Treat anything irreversible with corresponding care."
        )

    return PREPARE_BRIEF.format(
        name=target.name,
        repo=target.repo_path,
        sha=sha,
        ref=ref,
        migration_clause=migration_clause,
        backup_clause=backup_clause,
    )


def render_diagnose_brief(*, target: DeployTarget, sha: str, observed: str) -> str:
    return DIAGNOSE_BRIEF.format(
        name=target.name,
        repo=target.repo_path,
        sha=sha,
        services=", ".join(target.services),
        observed=observed,
    )


def run_agent(
    *,
    target: DeployTarget,
    scratch: Path,
    unit: str,
    brief: str,
    migration_allowed: bool,
    read_only: bool = False,
    timeout_s: float = AGENT_TIMEOUT_S,
) -> AgentOutcome:
    """Run one agent phase and read back its report.

    The report is a file the agent writes, not its stdout. Parsing prose
    for a verdict is how "it said it worked" becomes "it worked"; a file
    that is absent or unparseable is a failure with a name, which is
    what R-6 asks for.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    prompt_file = scratch / f"{unit}.prompt.txt"
    prompt_file.write_text(brief, encoding="utf-8")
    report_path = scratch / f"{unit}.report.json"
    if report_path.exists():
        report_path.unlink()

    argv = build_argv(
        target=target,
        scratch=scratch,
        unit=unit,
        prompt_file=prompt_file,
        report_path=report_path,
        migration_allowed=migration_allowed,
        read_only=read_only,
    )

    logger.info("Starting deploy agent", unit=unit, target=target.name, read_only=read_only)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _stop_unit(unit)
        return AgentOutcome(
            ok=False,
            error=(
                f"the agent did not finish within {timeout_s:.0f}s and was stopped. "
                "The repository may be in a partially prepared state; the service "
                "was NOT restarted."
            ),
        )
    except OSError as exc:
        return AgentOutcome(ok=False, error=f"could not start the agent unit: {exc}")

    stdout = proc.stdout or ""
    denials = _denials_from_cli_json(stdout)

    if not report_path.exists():
        tail = (proc.stderr or stdout).strip()[-800:]
        # Distinguish "the agent ran and said nothing" from "the unit
        # never ran the agent at all". Both used to read as the former,
        # which sent whoever was debugging it to the transcript for an
        # answer that was in the unit's exit status: 226 is systemd
        # failing to set up the namespace (a path under PrivateTmp's
        # /tmp), not the agent misbehaving.
        if proc.returncode == 226:
            reason = (
                "the agent's unit could not start: systemd failed to set up its "
                "namespace (exit 226). This is a path problem, not an agent "
                "problem -- check that the repo and scratch directories exist and "
                "are not under /tmp, which PrivateTmp hides"
            )
        elif proc.returncode != 0:
            reason = (
                f"the agent's unit exited {proc.returncode} without writing a report"
            )
        else:
            reason = "the agent ran but wrote no report"
        return AgentOutcome(
            ok=False,
            denials=denials,
            exit_code=proc.returncode,
            error=(
                f"{reason}. Treating the deploy as failed; the service was NOT "
                f"restarted. Last output: {tail or '(none)'}"
            ),
        )

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return AgentOutcome(
            ok=False,
            denials=denials,
            exit_code=proc.returncode,
            error=f"the agent's report was unreadable ({exc}); treating the deploy as failed",
        )

    if not isinstance(report, dict):
        return AgentOutcome(
            ok=False,
            denials=denials,
            exit_code=proc.returncode,
            error="the agent's report was not a JSON object; treating the deploy as failed",
        )

    return AgentOutcome(
        ok=bool(report.get("ok")) and proc.returncode == 0,
        summary=str(report.get("summary", ""))[:2000],
        steps=[s for s in report.get("steps", []) if isinstance(s, dict)][:50],
        denials=denials,
        migration_applied=report.get("migration_applied"),
        undetermined=bool(report.get("undetermined")),
        exit_code=proc.returncode,
        error=None if report.get("ok") else str(report.get("summary", ""))[:2000] or None,
    )


def _denials_from_cli_json(stdout: str) -> list[str]:
    """Pull ``permission_denials`` out of the CLI's own result JSON.

    Best-effort by design: this is for the audit trail ("the agent tried
    to move a ref"), never for deciding whether the deploy succeeded.
    """
    start = stdout.find('{"')
    if start < 0:
        return []
    for chunk in stdout[start:].splitlines():
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "permission_denials" not in payload:
            continue
        out = []
        for denial in payload.get("permission_denials") or []:
            tool = denial.get("tool_name", "?")
            command = (denial.get("tool_input") or {}).get("command")
            out.append(f"{tool}: {command}" if command else tool)
        return out[:50]
    return []


def _stop_unit(unit: str) -> None:
    """Best effort. Called from a timeout handler, so it must not raise."""
    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
    try:
        subprocess.run(
            ["sudo", "-n", systemctl, "stop", f"{unit}.service"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not stop the agent unit", unit=unit, error=str(exc))
