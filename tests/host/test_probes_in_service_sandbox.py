"""Every probe works inside the MCP service's sandbox (host test, opt-in).

The no-auth instance runs under NoNewPrivileges / ProtectSystem=strict /
ProtectHome=read-only / PrivateTmp. journalctl, systemctl, git, nvidia-smi,
``/proc`` and the registered env file all have to be reachable from there,
and a probe that only works from an interactive shell would be a probe
that does not work. Needs ``sudo`` for a ``--system`` transient unit:
``--user`` units silently ignore sandbox properties (docs/deploy-runner.md).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MAGICKIT_HOST_TESTS") != "1", reason="host test; set MAGICKIT_HOST_TESTS=1"
)

ROOT = Path(__file__).resolve().parents[2]


def test_all_probes_succeed_in_the_service_sandbox():
    unit = f"hostprobe-sandbox-{secrets.token_hex(4)}.service"
    venv_python = ROOT / ".venv" / "bin" / "python"
    proc = subprocess.run(
        [
            "sudo",
            "-n",
            "systemd-run",
            "--system",
            "--wait",
            "--collect",
            "--pipe",
            f"--unit={unit}",
            f"--uid={os.getuid()}",
            f"--gid={os.getgid()}",
            f"--working-directory={ROOT}",
            "-p",
            "NoNewPrivileges=true",
            "-p",
            "ProtectSystem=strict",
            "-p",
            "ProtectHome=read-only",
            "-p",
            "PrivateTmp=true",
            "-p",
            "MemoryMax=512M",
            "-E",
            f"PYTHONPATH={ROOT / 'src'}",
            "-E",
            "MAGICKIT_AUTH_DISABLED=1",
            str(venv_python),
            str(ROOT / "tests" / "host" / "sandbox_smoke.py"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [json.loads(line[6:]) for line in proc.stdout.splitlines() if line.startswith("SMOKE ")]
    for line in lines:
        print(line)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert len(lines) == 19
    failed = [line for line in lines if not line["ok"]]
    assert not failed, failed
