"""Passthrough dispatcher for the github-mcp container.

Claude connectors freeze the MCP tool list at connection time and ignore
tools/list_changed, so dynamically revealing toolsets is impossible. To keep
the context cost low while retaining full GitHub capability, the 35 github-mcp
tools are collapsed into a single dispatcher tool plus an on-demand schema
lookup, instead of being exposed individually.

- ``github(operation, arguments)``: Claude picks the operation name and builds
  the arguments; the call is forwarded to the github-mcp container.
- ``github_operations(name_filter)``: returns the exact upstream input schemas
  on demand (a normal tool result, so it works under the connector's fixed
  tool list, unlike tools/list_changed).

Identity: review-submit operations are forwarded with the *reviewer* PAT
(``GITHUB_MCP_PAT_REVIEWER``); all other operations use the *implementer* PAT
(``GITHUB_MCP_PAT_IMPLEMENTER``). Both fall back to the legacy single
``GITHUB_MCP_PAT`` when a role-specific token is unset, so existing single-PAT
deployments keep working unchanged.

Merge policy: ``merge_pull_request`` into a protected base branch (default
``main``, see ``GITHUB_PROTECTED_BASE_BRANCHES``) is refused here before it
reaches GitHub, since this plan has no branch protection and per-tool perms
cannot gate by operation. Merges into other branches pass.

Upstream transport: a minimal, **stateless per-call** httpx JSON-RPC client.
We deliberately avoid FastMCP's StreamableHttp client here: its stateful
session + SSE GET stream (which github-mcp answers with 405, triggering an
endless reconnect loop) was the suspected cause of intermittent 400s in the
long-lived service. github-mcp's HTTP endpoint accepts a plain initialize +
request flow, which is all we need.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

_PROTOCOL_VERSION = "2025-06-18"

# --- Identity routing (implementer vs reviewer) ------------------------------
# Operations that submit a formal PR review run under the *reviewer* identity
# (spirrowgames-ops; Pull requests RW, Contents read-only). Everything else —
# commits, branch/file writes, PR creation, merges — runs under the
# *implementer* identity (takahito-spirrowgames; Contents RW). Keeping the
# review event off the account that opened the PR avoids the self-review
# "422 Unprocessable Entity" seen on PR #67, while the implementer keeps
# authoring commits and PRs.
#
# NOTE: both PATs live in this one process's environment, so this is an
# *operation-level* split, not process/file isolation — a compromise of this
# process exposes both tokens. True isolation would need two dispatcher
# instances (separate units + EnvironmentFiles).
_REVIEWER_OPS = frozenset(
    {
        "pull_request_review_write",
        "add_comment_to_pending_review",
    }
)

_IMPLEMENTER_PAT_ENV = "GITHUB_MCP_PAT_IMPLEMENTER"
_REVIEWER_PAT_ENV = "GITHUB_MCP_PAT_REVIEWER"
_LEGACY_PAT_ENV = "GITHUB_MCP_PAT"  # single-PAT fallback and enable gate

# --- Merge policy ("merge to main = human GO") -------------------------------
# This repo's GitHub plan has no branch protection, and Claude-Code per-tool
# permissions cannot gate by `operation` (the 35 tools are one `github` tool).
# So the dispatcher itself refuses merges into protected branches: it looks up
# the PR's base via pull_request_read before forwarding merge_pull_request, and
# blocks if the base is protected. Merges into other branches (e.g. develop)
# pass. Comma-separated, overridable via GITHUB_PROTECTED_BASE_BRANCHES.
_PROTECTED_BASE_ENV = "GITHUB_PROTECTED_BASE_BRANCHES"
_DEFAULT_PROTECTED_BASE = "main"

# Compact operation catalog. Toolsets are fixed at repos,issues,pull_requests;
# names are the upstream github-mcp tool names (no prefix). Keep in sync if the
# container's --toolsets change.
_CATALOG = """\
Available `operation` values (github-mcp; toolsets repos/issues/pull_requests).
Arguments follow github-mcp's own schema — call github_operations(name_filter)
for the exact input schema of any operation.

repos/files: search_repositories, search_code, create_repository,
  fork_repository, get_file_contents, create_or_update_file, delete_file,
  push_files, create_branch, list_branches, get_commit, list_commits
issues: issue_read, issue_write, add_issue_comment, list_issues,
  list_issue_types, search_issues, sub_issue_write
pull_requests: pull_request_read, create_pull_request, update_pull_request,
  update_pull_request_branch, list_pull_requests, merge_pull_request,
  pull_request_review_write, add_comment_to_pending_review,
  add_reply_to_pull_request_comment, search_pull_requests
releases/tags: get_latest_release, get_release_by_tag, list_releases,
  get_tag, list_tags, get_label

NOTE: merge_pull_request into a protected branch (default: main) is refused by
local policy — a human merges those. Merges into other branches (e.g. develop)
are allowed."""


class _UpstreamError(RuntimeError):
    """github-mcp returned a non-2xx or a JSON-RPC error."""


def _parse_mcp_response(resp: httpx.Response) -> Any:
    """Extract the JSON-RPC ``result`` from a JSON or SSE response body."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        payload = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
        if payload is None:
            raise _UpstreamError("empty SSE stream from github-mcp")
    else:
        payload = resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise _UpstreamError(f"JSON-RPC error: {payload['error']}")
    return payload.get("result") if isinstance(payload, dict) else payload


def _any_pat_configured() -> bool:
    """True if any GitHub PAT (role-specific or legacy single) is set."""
    return any(
        os.environ.get(name)
        for name in (_LEGACY_PAT_ENV, _IMPLEMENTER_PAT_ENV, _REVIEWER_PAT_ENV)
    )


def _resolve_pat(role_env: str) -> str:
    """Return the PAT for a role, falling back to the legacy single PAT.

    Args:
        role_env: ``GITHUB_MCP_PAT_IMPLEMENTER`` or ``GITHUB_MCP_PAT_REVIEWER``.

    Raises:
        _UpstreamError: neither the role PAT nor the legacy fallback is set.
    """
    pat = os.environ.get(role_env) or os.environ.get(_LEGACY_PAT_ENV)
    if not pat:
        raise _UpstreamError(f"{role_env} (and {_LEGACY_PAT_ENV} fallback) unset")
    return pat


def _pat_for_operation(operation: str) -> str:
    """Pick the reviewer PAT for review-submit ops, else the implementer PAT."""
    role_env = (
        _REVIEWER_PAT_ENV if operation in _REVIEWER_OPS else _IMPLEMENTER_PAT_ENV
    )
    return _resolve_pat(role_env)


async def _mcp_call(method: str, params: dict[str, Any], pat: str) -> Any:
    """One stateless MCP exchange: initialize, then the requested method.

    Args:
        method: JSON-RPC method (e.g. ``tools/call``, ``tools/list``).
        params: JSON-RPC params for ``method``.
        pat: GitHub PAT forwarded as the upstream Bearer credential; choose it
            with :func:`_pat_for_operation` so the identity matches the op.

    Raises:
        _UpstreamError: github-mcp returned an HTTP or JSON-RPC error. The
            message includes the upstream status and body snippet so the
            failure is self-diagnosing in logs and tool results.
    """
    url = os.environ.get("GITHUB_MCP_URL", "http://127.0.0.1:8116/mcp")
    headers = {
        "Authorization": f"Bearer {pat}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": _PROTOCOL_VERSION,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        init = await client.post(
            url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "magickit-dispatch", "version": "1"},
                },
            },
        )
        if init.status_code >= 400:
            raise _UpstreamError(
                f"initialize HTTP {init.status_code}: {init.text[:300]}"
            )
        sid = init.headers.get("mcp-session-id")
        if sid:
            headers["Mcp-Session-Id"] = sid

        await client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

        resp = await client.post(
            url,
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if resp.status_code >= 400:
            raise _UpstreamError(
                f"{method} HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return _parse_mcp_response(resp)


def _err(e: Exception) -> dict:
    """Uniform error envelope for both tools."""
    return {
        "error": f"{type(e).__name__}: {str(e)[:400]}",
        "hint": (
            "If this is an HTTP/JSON-RPC error the github-mcp upstream is the "
            "problem (status/body included above), not the operation name. "
            "Otherwise check the operation name or call "
            "github_operations(name_filter) for the exact input schema."
        ),
    }


def _protected_base_branches() -> frozenset[str]:
    """Branches a merge may not target via this dispatcher (human-GO only).

    Read from ``GITHUB_PROTECTED_BASE_BRANCHES`` (comma-separated) each call so
    it tracks env changes and stays testable; defaults to ``main``.
    """
    raw = os.environ.get(_PROTECTED_BASE_ENV, _DEFAULT_PROTECTED_BASE)
    return frozenset(b.strip() for b in raw.split(",") if b.strip())


def _policy_block(reason: str, base_ref: str | None) -> dict:
    """Envelope for an op refused by local policy (distinct from an upstream error)."""
    return {"error": reason, "blocked_by": "policy", "base_ref": base_ref}


async def _pr_base_ref(arguments: dict[str, Any], pat: str) -> str | None:
    """Return a PR's base branch via upstream ``pull_request_read(get)``.

    ``merge_pull_request`` only carries ``owner/repo/pullNumber``; the base
    branch lives on the PR, so it must be looked up. Returns None when it
    cannot be determined (missing args, upstream or parse failure) so the
    caller can fail closed.
    """
    owner = arguments.get("owner")
    repo = arguments.get("repo")
    number = arguments.get("pullNumber")
    if not (owner and repo and number is not None):
        return None
    try:
        result = await _mcp_call(
            "tools/call",
            {
                "name": "pull_request_read",
                "arguments": {
                    "method": "get",
                    "owner": owner,
                    "repo": repo,
                    "pullNumber": number,
                },
            },
            pat,
        )
    except Exception as e:  # noqa: BLE001 - unknown base => fail closed in caller
        logger.warning("pr base lookup failed", repo=repo, num=number, err=str(e))
        return None
    content = result.get("content") if isinstance(result, dict) else None
    if not content:
        return None
    try:
        pr = json.loads(content[0].get("text", ""))
        ref = (pr.get("base") or {}).get("ref")
        return ref if isinstance(ref, str) else None
    except (ValueError, TypeError, AttributeError, IndexError, KeyError):
        return None


def _merge_block_reason(base_ref: str | None) -> str | None:
    """Reason to refuse a merge into ``base_ref``, or None to allow it."""
    protected = sorted(_protected_base_branches())
    if base_ref is None:
        return (
            "merge_pull_request blocked: could not determine the PR's base "
            f"branch, and merges into protected branches {protected} require a "
            "human (fail-closed). Verify the PR or merge manually after approval."
        )
    if base_ref in _protected_base_branches():
        return (
            f"merge_pull_request into '{base_ref}' is blocked by policy "
            f"(protected: {protected}). A human must merge to '{base_ref}'; "
            "merges into other branches (e.g. develop) are allowed."
        )
    return None


async def _execute_operation(operation: str, arguments: dict[str, Any]) -> Any:
    """Pick the identity, enforce merge policy, then forward to github-mcp.

    Returns the upstream result, or a :func:`_policy_block` envelope when a
    merge into a protected branch is refused (the merge is not forwarded).
    """
    pat = _pat_for_operation(operation)
    if operation == "merge_pull_request":
        base_ref = await _pr_base_ref(arguments, pat)
        reason = _merge_block_reason(base_ref)
        if reason is not None:
            logger.warning(
                "merge blocked by policy",
                repo=arguments.get("repo"),
                num=arguments.get("pullNumber"),
                base=base_ref,
            )
            return _policy_block(reason, base_ref)
    return await _mcp_call(
        "tools/call", {"name": operation, "arguments": arguments}, pat
    )


async def upstream_health() -> dict:
    """Liveness probe for the github-mcp container, for service_health.

    Returns a dict in the same shape the health tool uses for other
    services. Reports ``disabled`` (not an error) when no GitHub PAT is
    configured, so the no-auth tailnet instance does not look unhealthy.
    """
    import time

    url = os.environ.get("GITHUB_MCP_URL", "http://127.0.0.1:8116/mcp")
    if not _any_pat_configured():
        return {
            "status": "disabled",
            "url": url,
            "reason": "no GITHUB_MCP_PAT/_IMPLEMENTER/_REVIEWER configured",
        }

    start = time.monotonic()
    try:
        result = await _mcp_call(
            "tools/list", {}, _resolve_pat(_IMPLEMENTER_PAT_ENV)
        )
        elapsed = (time.monotonic() - start) * 1000
        return {
            "status": "healthy",
            "response_time_ms": round(elapsed, 2),
            "url": url,
            "operation_count": len(result.get("tools", [])),
        }
    except Exception as e:  # noqa: BLE001 - reported as health status
        elapsed = (time.monotonic() - start) * 1000
        return {
            "status": "error",
            "response_time_ms": round(elapsed, 2),
            "url": url,
            "error": str(e)[:300],
        }


def install_github_dispatch(mcp) -> None:
    """Register the github dispatcher and schema-lookup tools.

    No-op unless a GitHub PAT is set, so the no-auth tailnet instance and
    the test suite are unaffected.

    Args:
        mcp: The Magickit FastMCP server.
    """
    if not _any_pat_configured():
        logger.info("github dispatcher disabled (no GITHUB_MCP_PAT* configured)")
        return

    @mcp.tool(
        name="github",
        description=(
            "Call a GitHub operation through the github-mcp server. Set "
            "`operation` to one of the names below and `arguments` to that "
            "operation's parameters.\n\n" + _CATALOG
        ),
    )
    async def github(operation: str, arguments: dict | None = None) -> Any:
        try:
            result = await _execute_operation(operation, arguments or {})
        except Exception as e:  # noqa: BLE001 - surfaced as guidance to caller
            logger.warning("github dispatch failed", op=operation, err=str(e))
            return _err(e)
        if isinstance(result, dict) and result.get("blocked_by") == "policy":
            return result  # local policy refusal (e.g. merge into main)
        content = result.get("content") if isinstance(result, dict) else None
        if content:
            return [c.get("text", c) for c in content]
        return result

    @mcp.tool(
        name="github_operations",
        description=(
            "List github-mcp operations with their exact JSON input schemas. "
            "Optional `name_filter` is a case-insensitive substring "
            "(e.g. 'pull_request', 'issue', 'file')."
        ),
    )
    async def github_operations(name_filter: str | None = None) -> Any:
        try:
            result = await _mcp_call(
                "tools/list", {}, _resolve_pat(_IMPLEMENTER_PAT_ENV)
            )
        except Exception as e:  # noqa: BLE001 - same envelope as github()
            logger.warning("github_operations failed", err=str(e))
            return _err(e)
        nf = (name_filter or "").lower()
        return [
            {
                "operation": t.get("name"),
                "description": (t.get("description") or "")[:200],
                "input_schema": t.get("inputSchema"),
            }
            for t in result.get("tools", [])
            if nf in (t.get("name") or "").lower()
        ]

    logger.info("github dispatcher installed (stateless httpx client)")
