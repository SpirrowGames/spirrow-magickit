"""Run every probe once, the way the MCP service would (host check helper).

``test_probes_in_service_sandbox.py`` starts this as a throw-away system
unit with the same user and sandbox properties as
``spirrow-magickit-mcp-local.service`` (NoNewPrivileges, ProtectSystem=strict,
ProtectHome=read-only, PrivateTmp, MemoryMax=512M), so that what is
measured is what the no-auth instance can actually do. Prints one JSON line
per probe: its name, ``ok``, ``error_type`` and the response size -- not the
result itself.
"""

import asyncio
import json

from magickit.hosts import probes
from magickit.hosts.limits import RateLimiter, Window

CALLS = (
    [
        ("unit_state", t, None)
        for t in (
            "spirrow-conclair",
            "spirrow-lexora",
            "spirrow-cognilens",
            "spirrow-prismind",
            "vllm-32b",
        )
    ]
    + [
        ("journal_summary", t, {"since_minutes": 4320, "limit": 200})
        for t in (
            "spirrow-conclair",
            "spirrow-lexora",
            "spirrow-cognilens",
            "spirrow-prismind",
            "vllm-32b",
        )
    ]
    + [
        ("env_keys", "spirrow-conclair", {"file": "env"}),
        ("deployed_sha", "spirrow-conclair", None),
        ("deployed_sha", "spirrow-lexora", None),
        ("deployed_sha", "spirrow-cognilens", None),
        ("deployed_sha", "spirrow-prismind", None),
        ("health", "spirrow-lexora", None),
        ("health", "spirrow-prismind", None),
        ("listening", "spirrow-conclair", None),
        ("resources", "", None),
    ]
)


async def main() -> None:
    lim = RateLimiter(global_windows=(Window(1000, 60),), probe_windows={})
    for name, target, params in CALLS:
        r = await probes.run_probe("services", name, target, params, limiter=lim)
        summary = {
            "probe": name,
            "target": target,
            "ok": r.get("ok"),
            "error_type": r.get("error_type"),
            "bytes": len(json.dumps(r)),
            "omitted": r.get("omitted"),
        }
        if name == "journal_summary" and r.get("ok"):
            u = r["result"]["units"][0]
            summary.update(scanned=u["scanned"], partial=u["partial"], trimmed=r["trimmed_to_fit"])
        print("SMOKE " + json.dumps(summary), flush=True)


asyncio.run(main())
