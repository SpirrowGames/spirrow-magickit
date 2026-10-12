"""Executor-pool MCP tools (ADR-2026-10-09-23 unit 4, Magickit side).

INV-0: with ``executor_pool_enabled`` false (the default) this module
registers nothing. Not registered-and-refusing -- absent -- so the MCP
tool list is identical to the one before this unit existed, and no
GitHub call can be made from here.

Today the module carries D10's tag path only (``executor_tag_create``).
The switch report (D12) and the reservation tools (D13) land here behind
the same setting.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from magickit.config import Settings
from magickit.deploy import executor_tags
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

def _make_api(pat_env: str) -> executor_tags.HttpxTagApi:
    """Token from ``settings.executor_tag_pat_env``; that identity must be the
    bypass actor of config/github/executor-tags-create.ruleset.json."""
    from magickit.mcp.github_dispatch import _resolve_pat  # noqa: PLC0415

    return executor_tags.HttpxTagApi(_resolve_pat(pat_env))


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    if not settings.executor_pool_enabled:
        return

    repo = settings.executor_tag_repo
    pat_env = settings.executor_tag_pat_env

    @mcp.tool()
    async def executor_tag_create(
        version: str, sha: str, message: str, requested_by: str
    ) -> dict[str, Any]:
        """Create the annotated tag `executor/v<version>` on `sha`.

        USE THIS WHEN: releasing a new executor version, or rolling back
        (D10: a rollback is a *new* version tagged on an older commit).
        This is the only path that creates `executor/*` tags; the GitHub
        ruleset refuses deletion, update and creation by anyone else.

        Args:
            version: exactly `X.Y.Z` (no `v`, no leading zeros, no suffix).
            sha: full 40-character commit id, reachable from `main`.
            message: the tag message (what this version is).
            requested_by: who asked; recorded in the tag message.

        Returns:
            {"ok": true, "repo", "tag", "ref", "commit", "tag_object",
             "requested_by"}
            On refusal: {"ok": false, "error_type", "message"} with
            `error_type` in {"invalid_version", "invalid_sha",
            "invalid_argument", "tag_exists", "sha_not_on_main",
            "github_error", "not_configured"}. An existing tag is never
            re-pointed.
        """
        try:
            api = _make_api(pat_env)
        except Exception as exc:  # the PAT is unset
            return {"ok": False, "error_type": "not_configured", "message": str(exc)}
        try:
            async with api:  # one connection pool for the whole run
                return await executor_tags.create_executor_tag(
                    api,
                    repo=repo,
                    version=version,
                    sha=sha,
                    message=message,
                    requested_by=requested_by,
                )
        except executor_tags.ExecutorTagError as exc:
            return {"ok": False, "error_type": exc.error_type, "message": exc.message}

    logger.info("Executor tools registered", repo=repo)
