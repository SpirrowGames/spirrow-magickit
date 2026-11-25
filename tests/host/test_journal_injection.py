"""PR-gate item (msg-1772 Obj-2-2): forged journal fields do not come back.

Runs only on the services host, opt-in: ``MAGICKIT_HOST_TESTS=1``. It needs
``sudo`` because the injector has to run as a *system* unit -- the probe
selects with ``-u`` exactly as in production, and a ``--user`` unit would
need a different selector, i.e. would test a different argv.

What it does: start a throw-away system unit (as the service user) that
sends forged entries to journald over the native protocol
(``journal_injector.py``), then run the probe's own journal path --
:func:`magickit.hosts.probes.summarise_journal` with the production argv
and parser -- against that unit, and check:

- the forged values are dropped and counted, the valid ones returned;
- no body and no free-text field appears anywhere in the output;
- an entry that writes ``UNIT=spirrow-lexora.service`` from a non-PID-1
  process is not reported as lexora's (M3).
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MAGICKIT_HOST_TESTS") != "1", reason="host test; set MAGICKIT_HOST_TESTS=1"
)

from magickit.hosts import probes  # noqa: E402

HERE = Path(__file__).resolve().parent


async def test_forged_journal_fields_are_not_returned():
    secret = "inject-" + secrets.token_hex(16)
    valid_id = uuid.uuid4().hex
    unit = f"hostprobe-inject-{secrets.token_hex(4)}.service"
    started = int(time.time()) - 1
    proc = subprocess.run(
        [
            "sudo",
            "-n",
            "systemd-run",
            "--system",
            "--wait",
            "--collect",
            f"--unit={unit}",
            f"--uid={os.getuid()}",
            f"--gid={os.getgid()}",
            "-p",
            "MemoryMax=64M",
            "/usr/bin/python3",
            "-I",
            str(HERE / "journal_injector.py"),
            secret,
            valid_id,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    time.sleep(1)
    until = int(time.time()) + 1

    omitted: Counter[str] = Counter()
    summary, stats = await probes.summarise_journal(
        probes.HostIO(),
        ["-u", unit],
        unit=unit,
        since_s=started,
        until_s=until,
        limit=200,
        omitted=omitted,
    )
    out = json.dumps(
        {"entries": summary.entries, "omitted": omitted, "by_priority": summary.by_priority},
        default=str,
    )
    print("RESULT", out)
    print("STATS", stats)

    assert secret not in out
    assert "token=" not in out
    # 11 forged entries + systemd's own start/stop lines about the unit
    assert summary.scanned >= 11
    ours = [e for e in summary.entries if e.get("pid") != 1]
    assert len(ours) == 11, ours
    # the well-formed entry came back whole
    assert {"priority": 3, "message_id": valid_id, "exit_status": 3}.items() <= next(
        e for e in ours if e.get("message_id") == valid_id
    ).items()
    # each forged value was dropped and counted
    assert omitted["journal.message_id"] >= 2
    assert omitted["journal.priority"] == 2
    assert omitted["journal.exit_status"] == 3
    assert omitted.get("journal.foreign_row", 0) == 0

    # M3: posing as lexora from a non-PID-1 process does not land in lexora's view
    lexora_omitted: Counter[str] = Counter()
    lexora, _ = await probes.summarise_journal(
        probes.HostIO(),
        ["-u", "spirrow-lexora.service"],
        unit="spirrow-lexora.service",
        since_s=started,
        until_s=until,
        limit=200,
        omitted=lexora_omitted,
    )
    injector_pids = {e["pid"] for e in ours}
    assert not [e for e in lexora.entries if e.get("pid") in injector_pids]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-s"]))
