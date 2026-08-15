"""Starting the runner from the MCP server process.

The MCP server cannot deploy anything itself, and that is not a policy
-- it is measured. Its unit sets ``NoNewPrivileges=true`` (so ``sudo``
fails outright), ``ProtectHome=read-only`` and a single
``ReadWritePaths`` for ``data/``, so it cannot restart a service or
write to any repository. What it *can* do is ask the user's systemd
manager to start a transient unit, which runs outside that sandbox.

So this module is the whole of the MCP server's power over production:
one call, taking one already-approved request id, with no other
parameters. There is nothing to inject -- no path, no command, no ref --
because everything the runner needs it reads back from the request file
under ``data/deploy/requests/``, which is the file the approval was
written to.

Detached rather than awaited: a deploy is minutes (conclair's unit alone
allows 120s for ``alembic upgrade head`` before systemd gives up), and an
MCP call that blocks that long has usually lost its client already. The
caller gets a request id and polls ``deploy_status``.
"""

from __future__ import annotations

import os
import subprocess
import sys

from magickit.deploy.paths import magickit_root
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

RUNNER_MEMORY_MAX = os.environ.get("MAGICKIT_DEPLOY_RUNNER_MEMORY_MAX", "1G")

#: systemd-run --user needs to find the user manager. A system unit does
#: not get these in its environment, so they are supplied explicitly
#: rather than inherited.
_USER_BUS_ENV = {
    "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
    "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
        "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
    ),
}


def unit_name(request_id: str) -> str:
    return f"magickit-deploy-{request_id}"


def launch(request_id: str) -> tuple[bool, str]:
    """Start the runner for ``request_id``. Returns ``(ok, unit or error)``."""
    if not request_id.isalnum():
        return False, f"invalid request id {request_id!r}"

    unit = unit_name(request_id)
    root = magickit_root()
    env = {**os.environ, **_USER_BUS_ENV}

    argv = [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit={unit}",
        f"--property=MemoryMax={RUNNER_MEMORY_MAX}",
        f"--working-directory={root}",
        f"--setenv=PYTHONPATH={root / 'src'}",
    ]
    # systemd-run --user gives the unit the *user manager's* environment,
    # not this process's, so anything the runner or the agent reads has
    # to be forwarded by name. A variable missing from this list is not
    # merely defaulted -- it is silently ignored wherever it was set,
    # which is worse than not supporting it.
    for passthrough in (
        "MAGICKIT_ROOT",
        "MAGICKIT_DEPLOY_STATE_DIR",
        "MAGICKIT_DEPLOY_AGENT_MODEL",
        "MAGICKIT_DEPLOY_AGENT_TIMEOUT_S",
        "MAGICKIT_DEPLOY_AGENT_MEMORY_MAX",
        "MAGICKIT_DEPLOY_CLAUDE_BIN",
        "MAGICKIT_DEPLOY_CLAUDE_STATE",
    ):
        if passthrough in os.environ:
            argv.append(f"--setenv={passthrough}={os.environ[passthrough]}")
    argv += [sys.executable, "-m", "magickit.deploy.runner", request_id]

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("Could not launch deploy runner", request_id=request_id, error=str(exc))
        return False, f"could not start the deploy runner: {exc}"

    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()[:600]
        logger.error("Deploy runner launch rejected", request_id=request_id, detail=detail)
        return False, f"systemd refused to start the deploy runner: {detail}"

    logger.info("Deploy runner launched", request_id=request_id, unit=unit)
    return True, unit
