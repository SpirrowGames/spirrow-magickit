"""Deploy execution: request, approve, run, report.

The gap this closes: a PR can be merged from anywhere, but `main` only
becomes *live* on sg-ai-server-01, where the systemd units and the
alembic history are. The development loop runs on sg-tomtebo-01 and has
no ssh to here, so "landed" and "running" were separated by a step
nobody could take remotely. magickit already sits on that boundary and
is already reachable from the loop, so the crossing goes here.

magickit owns four things and deliberately not a fifth:

- **which** targets may be deployed (:mod:`registry`)
- **what** is deployed -- ``origin/main``, resolved and pinned into the
  working tree *before* the agent starts (:mod:`pin`)
- **that a human approved it**, and on what terms (:mod:`records`, plus
  the split MCP surface in ``magickit.mcp.tools.deploy``)
- **what happened** -- a structured result and an append-only audit
  trail (:mod:`records`)

The fifth -- *how* a given repository is deployed -- belongs to the
Claude Code agent that :mod:`runner` launches. Keeping it here would
mean magickit carried a copy of every repo's deploy procedure, and that
copy would go stale the first time a repo changed and nobody thought to
update magickit. The agent reads the repo it is standing in.

The one place that split is bent is the *privileged* steps. Restarting a
unit and checking health are not repo-specific knowledge that can rot --
the unit name is already in the registry, because R-4 requires magickit
to hold the target set anyway -- so the runner does them itself. That
buys a real boundary rather than a stated one: the agent never needs
``sudo``, so its transient unit sets ``NoNewPrivileges=true`` and
``sudo`` becomes *impossible* for it instead of merely denied. See
``docs/deploy-runner.md`` for the measurements behind that claim.
"""

from __future__ import annotations

from magickit.deploy.registry import (
    DEPLOY_REF,
    DeployTarget,
    SelfDeployRefusedError,
    TargetNotAllowedError,
    resolve_target,
    target_names,
)

__all__ = [
    "DEPLOY_REF",
    "DeployTarget",
    "SelfDeployRefusedError",
    "TargetNotAllowedError",
    "resolve_target",
    "target_names",
]
