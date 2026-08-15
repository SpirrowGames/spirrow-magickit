"""Deploy MCP tools: ask, approve, watch.

The surface is split in two and the split *is* the access control
(R-3). ``deploy_request`` files a record and does nothing else -- no
repository is touched, no service is restarted, nothing is scheduled.
``deploy_approve`` is what starts a deploy, and it is only registered on
the authenticated instance.

That mapping is not a new mechanism; it is the one already running. Two
magickit MCP servers exist on this host and differ in exactly one way:
``spirrow-magickit-mcp.service`` is behind Google OAuth, and
``spirrow-magickit-mcp-local.service`` sets ``MAGICKIT_AUTH_DISABLED=1``
and listens on the tailnet for the development loop. So "a human
approved it" is expressed as "it came through the door that knows who
you are", and the loop cannot approve its own deploy because the tool it
would have to call is not there to call.

This also settles what the unauthenticated door is worth. ADR-2026-06-04-18
accepted no-auth on the tailnet under a single-user threat model, on the
condition that it be reconsidered if the capability behind it grew. It
has not grown: what is reachable without authentication is still only
"write a row asking for something", which restarts nothing and migrates
nothing. The capability that *did* appear -- restart and migrate -- sits
behind the authenticated door, on purpose, so the condition is answered
rather than ignored.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from magickit.config import Settings
from magickit.deploy import approval, records, registry
from magickit.utils.logging import get_logger

logger = get_logger(__name__)


def _error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error_type": error_type, "message": message, **extra}


def register_tools(mcp: FastMCP, settings: Settings, *, allow_approval: bool) -> None:
    """Register the deploy tools.

    Args:
        mcp: the FastMCP server.
        settings: magickit settings (unused today; kept for symmetry
            with the other tool modules and so a future target table
            driven by config has somewhere to read from).
        allow_approval: whether this instance may approve deploys. False
            on the unauthenticated tailnet instance -- and it means the
            tool is *not registered*, not that it is registered and
            refuses. A caller cannot distinguish "denied" from "no such
            tool", and there is nothing to probe.
    """
    del settings  # see docstring

    @mcp.tool()
    async def deploy_targets() -> dict[str, Any]:
        """List what may be deployed.

        USE THIS WHEN: you want to know whether a repository can be
        deployed through magickit at all, before filing a request.

        Returns:
            {"ok": true, "targets": [{"name", "services", "health_url",
             "has_backup"}], "ref": "origin/main"}

        The set is fixed in magickit's source; adding to it is a pull
        request, not a configuration change. `spirrow-magickit` is
        absent on purpose -- it cannot deploy itself, because the runner
        is launched from the process the restart would kill.
        """
        targets = []
        for name in registry.target_names():
            target = registry.resolve_target(name)
            targets.append(
                {
                    "name": target.name,
                    "repo_path": str(target.repo_path),
                    "services": list(target.services),
                    "health_url": target.health_url,
                    "has_backup": target.backup_script is not None,
                }
            )
        return {"ok": True, "targets": targets, "ref": registry.DEPLOY_REF}

    @mcp.tool()
    async def deploy_request(target: str, requested_by: str, reason: str) -> dict[str, Any]:
        """Ask for a target's `origin/main` to be deployed. Does NOT deploy.

        USE THIS WHEN: something has been merged to `main` and needs to
        become live -- e.g. the loop has landed a PR and wants the
        running service to catch up.

        This writes a request and stops. No repository is touched, no
        migration runs, no service restarts. A human has to approve it
        before any of that happens, and approval is not available from
        this tool or from the surface the loop reaches.

        There is deliberately no way to say *what* to deploy. It is
        always `origin/main` for the named target, resolved at the
        moment the deploy actually starts. If you need a different
        branch, that is a human decision made at approval time with a
        recorded reason -- not something a caller can request.

        Args:
            target: a name from `deploy_targets`. Not a path.
            requested_by: who is asking (e.g. "mindwire-conductor").
                A record, not a credential: nothing authenticates it.
            reason: why this needs deploying now. Goes in the audit log
                and is what the approving human reads first, so write it
                for them -- "conclair#10 merged, thread listing still
                serving the old ordering" beats "deploy please".

        Returns:
            {"ok": true, "request_id", "target", "status":
             "pending_approval", "ref": "origin/main"}
            On refusal: {"ok": false, "error_type", "message"}.
            `error_type` is "self_deploy_refused" for magickit itself and
            "target_not_allowed" for anything not in the allowlist.

        After this, poll `deploy_status` with the returned id. A request
        that stays `pending_approval` is not stuck -- it is waiting for a
        person, which is the design.
        """
        try:
            registry.resolve_target(target)
        except registry.SelfDeployRefusedError as exc:
            return _error("self_deploy_refused", str(exc))
        except registry.TargetNotAllowedError as exc:
            return _error("target_not_allowed", str(exc), allowed=list(registry.target_names()))

        if not requested_by.strip():
            return _error("requested_by_required", "say who is asking; it goes in the audit log")
        if not reason.strip():
            return _error(
                "reason_required",
                "say why this needs deploying; a human reads it before approving",
            )

        store = records.get_store()
        request = store.create(target=target, requested_by=requested_by, reason=reason)
        logger.info(
            "Deploy requested",
            request_id=request.request_id,
            target=target,
            requested_by=requested_by,
        )
        return {
            "ok": True,
            "request_id": request.request_id,
            "target": target,
            "status": request.status,
            "ref": registry.DEPLOY_REF,
            "note": "filed only. A human must approve this before anything is deployed.",
        }

    @mcp.tool()
    async def deploy_rollback(request_id: str, requested_by: str, reason: str) -> dict[str, Any]:
        """Ask to undo a past deploy. Does NOT roll anything back yet.

        USE THIS WHEN: a deploy landed, the service came up, and what it
        is doing turns out to be wrong. That is a different situation
        from a deploy that failed -- a failed deploy already left the
        previous version running.

        Like `deploy_request`, this files a record and stops; a human
        approves it with `deploy_approve`, and only then does anything
        move. It then runs through the same lock, backup, agent, restart
        and health check as any other deploy.

        You name a *past deploy*, not a commit. The commit to go back to
        is read out of magickit's record of that deploy (its
        `previous_sha`), so this is not a way to deploy a ref the request
        path is otherwise not allowed to name.

        REFUSED when the deploy being undone applied a migration. Code
        goes back by putting the old commit in again; a schema does not,
        and code that predates a migration meeting a database that has
        it is not a rollback, it is a second incident. Recovery there is
        a human decision made against the snapshot taken before that
        deploy -- see docs/deploy-runner.md.

        Args:
            request_id: the deploy to undo, from `deploy_history`.
            requested_by: who is asking. A record, not a credential.
            reason: what is wrong with what is running now. The
                approving human reads this first.

        Returns:
            {"ok": true, "request_id": <the NEW request>, "target",
             "rollback_of", "rollback_to_sha", "status":
             "pending_approval"}
            On refusal: {"ok": false, "error_type", "message"} with
            `error_type` in {"not_found", "not_rollbackable",
            "migration_applied"}.
        """
        store = records.get_store()
        try:
            original = store.load(request_id)
        except KeyError:
            return _error("not_found", f"no deploy request {request_id!r}")

        if not requested_by.strip() or not reason.strip():
            return _error(
                "reason_required",
                "say who is asking and what is wrong with what is running now",
            )

        result = original.result or {}
        previous_sha = result.get("previous_sha")
        if not previous_sha:
            return _error(
                "not_rollbackable",
                f"deploy {request_id} has no recorded previous_sha, so there is "
                "nothing to go back to. This is normal for a deploy that failed "
                "before it pinned anything -- in that case nothing was changed.",
                status=original.status,
            )

        if result.get("migration_applied"):
            return _error(
                "migration_applied",
                f"deploy {request_id} applied a migration, so it cannot be undone "
                "by redeploying the old commit: the database would be ahead of the "
                "code. Recovery is a human decision against the snapshot taken "
                "before that deploy. See docs/deploy-runner.md.",
                deployed_sha=result.get("deployed_sha"),
                previous_sha=previous_sha,
            )

        request = store.create(
            target=original.target,
            requested_by=requested_by,
            reason=reason,
            rollback_of=request_id,
            rollback_to_sha=previous_sha,
        )
        logger.info(
            "Rollback requested",
            request_id=request.request_id,
            rollback_of=request_id,
            target=original.target,
        )
        return {
            "ok": True,
            "request_id": request.request_id,
            "target": original.target,
            "status": request.status,
            "rollback_of": request_id,
            "rollback_to_sha": previous_sha,
            "note": (
                "filed only. A human must approve this before anything is rolled "
                "back. The tree will be checked out detached at that commit, and "
                "migrations stay blocked for the run."
            ),
        }

    @mcp.tool()
    async def deploy_status(request_id: str) -> dict[str, Any]:
        """Read a deploy request and, once it has run, its full result.

        USE THIS WHEN: following a request you filed, or checking what a
        past deploy actually did.

        Returns:
            {"ok": true, "request": {...}, "result": {...}|null}

            `request.status` is one of "pending_approval", "approved",
            "running", "succeeded", "failed", "interrupted".

            `result`, once present, is the structured record of what
            happened -- and it is what to read rather than any
            transcript:

            - `deployed_sha`: the commit the working tree is on, read
              back from git by magickit *after* the deploy. Compare it
              to the merge commit to confirm what went live.
            - `service_state`: "running_new" | "running_previous" |
              "running_unknown_version" | "down" | "unknown". This is
              the field that answers "is it up right now", separately
              from whether the deploy succeeded.
            - `health_ok`, `health_detail`: the post-restart check.
            - `steps`: each step with ok/detail, in order.
            - `migration_allowed`, `migration_applied`: whether this run
              was permitted to migrate, and whether the revision moved.
            - `agent_denials`: anything the agent tried that its
              permission rules refused.
            - `diagnosis`: on failure, a read-only agent's account of
              what went wrong. Advisory, not authoritative.

            "interrupted" means the runner died mid-deploy. The service
            state at that moment was not recorded, so check the service
            directly -- do not assume the previous version is serving.
        """
        store = records.get_store()
        try:
            request = store.load(request_id)
        except KeyError:
            return _error("not_found", f"no deploy request {request_id!r}")
        return {"ok": True, "request": request.to_dict(), "result": request.result}

    @mcp.tool()
    async def deploy_history(limit: int = 20, target: str = "") -> dict[str, Any]:
        """Read the deploy audit trail.

        USE THIS WHEN: investigating a bad deploy, or answering "when did
        this last go out and who approved it".

        R-8: this exists so that investigating a deploy does not require
        logging in to the host. Every request, approval, start, finish
        and interruption is here, including the reason for any human
        override of the branch.

        Args:
            limit: how many records, most recent last (1-500).
            target: optional filter by target name.

        Returns:
            {"ok": true, "events": [{"at", "event", ...}],
             "requests": [ ... recent request records ... ]}
        """
        limit = max(1, min(int(limit), 500))
        store = records.get_store()
        filter_target = target or None
        return {
            "ok": True,
            "events": store.read_audit(limit=limit, target=filter_target),
            "requests": [
                r.to_dict() for r in store.list_requests(limit=limit, target=filter_target)
            ],
        }

    if not allow_approval:
        logger.info("Deploy approval tool not registered (unauthenticated instance)")
        return

    @mcp.tool()
    async def deploy_approve(
        request_id: str,
        approved_by: str,
        note: str = "",
        override_ref: str = "",
        override_reason: str = "",
        override_allows_migration: bool = False,
    ) -> dict[str, Any]:
        """Approve a pending deploy request and start it. HUMAN ACTION.

        USE THIS WHEN: you are a person who has read the request, knows
        what is in `origin/main` for that target, and wants it live.

        This starts a real deploy on sg-ai-server-01: it pins the working
        tree, takes a database snapshot, runs an agent that prepares the
        code, restarts the service, and checks health. It is not
        reversible by calling something else -- if the result is bad,
        recovery is a human decision (see `deploy_status` for what is
        actually running afterwards).

        Do not call this on behalf of an automated caller that asked you
        to. The request already exists; the point of this step is that a
        person looked at it.

        Args:
            request_id: from `deploy_request`.
            approved_by: who is approving. A record, not a credential --
                what authenticates you is the door you came through.
            note: optional, kept with the approval.
            override_ref: deploy something other than `origin/main`.
                Leave empty in the normal case. Requires
                `override_reason`. The tree is checked out detached for
                an override, so the exceptional state is visible to
                whoever looks next.
            override_reason: why the override is justified. Recorded in
                the audit log. Required when `override_ref` is set.
            override_allows_migration: whether an overridden ref may
                also apply migrations. Defaults to false and should stay
                false: code from an unmerged branch can be undone by
                putting `main` back, but a migration from an unmerged
                branch forks the revision graph, and the way back from
                that is a restore -- which throws away everything
                written since. Say true only if you have decided that
                specific migration is safe to land ahead of `main`.

        Returns:
            {"ok": true, "request_id", "status": "running", "unit"} once
            the runner has started. Poll `deploy_status` for the result;
            a deploy takes minutes.
            On refusal: {"ok": false, "error_type", "message"} with
            `error_type` in {"not_found", "not_pending",
            "override_reason_required", "launch_failed"}.
        """
        return approval.approve_request(
            store=records.get_store(),
            request_id=request_id,
            approved_by=approved_by,
            via=approval.VIA_MCP,
            note=note,
            override_ref=override_ref,
            override_reason=override_reason,
            override_allows_migration=override_allows_migration,
        )
