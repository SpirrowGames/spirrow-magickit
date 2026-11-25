"""Host-task MCP tools, step 1: look at the services host without a shell.

Design: spirrow-magickit ``T-host-task-request-with-human-approval``
(msg-1750 .. msg-1777). This module is its step 1 (msg-1757 D-1): the
registry and ``host_probe``. Config-change requests, approval and the apply
runner are later steps.

Both tools are registered on both MCP instances, the unauthenticated
tailnet one included (msg-1750 §2.3). That is acceptable because neither
can change anything: ``host_probe`` runs a probe from a fixed catalogue
against registry names, and returns values it rebuilt and validated (see
:mod:`magickit.hosts.probes`). What it costs the host is bounded by
:mod:`magickit.hosts.limits`, because on that instance nobody is
identified.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from magickit.config import Settings
from magickit.hosts import probes
from magickit.hosts.limits import RateLimiter


def register_tools(mcp: FastMCP, settings: Settings, *, limiter: RateLimiter | None = None) -> None:
    """Register ``host_task_targets`` and ``host_probe``.

    Args:
        mcp: the FastMCP server.
        settings: unused; kept for the ``register_tools`` convention.
        limiter: the rate limiter (tests pass their own). One per process.
    """
    del settings
    shared_limiter = limiter or RateLimiter()

    @mcp.tool()
    async def host_task_targets() -> dict[str, Any]:
        """List the hosts, their targets, and the probe catalogue.

        USE THIS WHEN: you want to investigate something on a service host
        (unit state, structured journal fields, env key names, deployed
        commit, health, listening ports, resources) and need the names to
        pass to `host_probe`.

        Hosts are logical names (`services` is `{{HOST_SERVICES}}` in
        platform:infra-registry), not hostnames. Nothing here is a path or
        an address. The set is fixed in magickit's source
        (`magickit.hosts.registry`); adding a target or a probe is a pull
        request. magickit itself is not a target (R-4).

        Returns:
            {"ok": true,
             "hosts": [{"name", "kind",
                        "targets": [{"name", "units", "ports", "has_health",
                                     "deployable", "files": [{"name", "format"}]}]}],
             "probes": [{"name", "summary", "needs_target",
                         "params": [{"name", "kind", "default"?, "min"?, "max"?, "doc"}]}]}
        """
        return probes.describe_targets()

    @mcp.tool()
    async def host_probe(
        host: str,
        probe: str,
        target: str = "",
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one read-only probe on a service host. No approval needed.

        USE THIS WHEN: you need to see the state of a service on the host
        instead of asking a human to log in and look. Call
        `host_task_targets` first for the names.

        This is not a shell. There is no free-text probe, no path argument,
        no grep: each probe is a fixed function over registry names. If
        what you need is not in the catalogue, say so in your thread --
        that is the signal to add a probe (a pull request), not something
        to work around.

        Log message bodies are never returned (ruling 2 of
        T-host-task-request-with-human-approval, msg-1770). `journal_summary`
        returns only validated structured fields: timestamp, priority, pid,
        MESSAGE_ID, exit status, and counts. Env files come back as key
        names only. Every returned value is rebuilt from a validated form;
        values that fail are dropped and counted in `omitted`.

        Args:
            host: logical host name (e.g. "services").
            probe: one of `unit_state`, `journal_summary`, `env_keys`,
                `deployed_sha`, `health`, `listening`, `resources`.
            target: target name; "" for host-wide probes (`resources`).
            params: per-probe parameters, e.g.
                `{"since_minutes": 60, "limit": 50}` for `journal_summary`,
                `{"file": "env"}` for `env_keys`. Integers only, within the
                bounds `host_task_targets` lists.

        Returns:
            {"ok": true, "host", "probe", "target", "params", "result",
             "omitted": {field: count}, "trimmed_to_fit": bool}
            or {"ok": false, "error_type", "message"[, "retry_after_s"]}.
            error_type is one of unknown_host / unknown_target /
            unknown_probe / unknown_file / unknown_param / invalid_param /
            target_required / target_not_applicable / out_of_scope /
            not_applicable / unsafe_path / rate_limited / probe_failed /
            output_too_large / internal_error.
        """
        return await probes.run_probe(host, probe, target, params, limiter=shared_limiter)
