"""Approving a deploy: the checks, and the two doors that reach them.

Approval was originally reachable only through the OAuth-gated MCP
instance, which is right for the actor it was built against -- the loop
on sg-tomtebo-01, which has no shell on this host and therefore cannot
do anything except what an MCP tool offers it.

It is not the whole picture for an actor that is *already* on the host.
A shell here runs as sgadmin, which has ``NOPASSWD: ALL`` and can write
``data/deploy/requests/*.json`` directly; there is nothing to stop it
approving its own request, or skipping the mechanism entirely and
running ``git merge && systemctl restart`` by hand. Pretending otherwise
would be the kind of security theatre that gets believed.

So there is a second door, and it is a command rather than an endpoint::

    python -m magickit.deploy.approval <request_id> --by <name>

It grants nothing that was not already available. What it buys is that
the thing which was going to happen anyway happens *through the
mechanism*: the same checks run, the same audit line is written, and the
record says which door it came through. An untracked bypass becomes a
recorded action, which is the only difference actually available here.

The loop cannot use it, for the same reason it cannot use ssh: this is a
process on the host, not a tool on the MCP surface. That is the boundary
that was real to begin with, and it is unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from magickit.deploy import launcher, records, registry
from magickit.deploy.records import STATUS_APPROVED, STATUS_PENDING
from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: Which door an approval came through. Kept in the record because "who
#: approved" and "how were they vouched for" are different questions,
#: and only the second one is answerable by the system.
VIA_MCP = "mcp-authenticated"
VIA_HOST = "host-cli"
#: The dashboard button, vouched for by the tailnet identity that
#: `tailscale serve` attaches (see `magickit.web.identity`). A third door,
#: not a widened one: it answers to a *named user*, which is the same
#: standard as VIA_MCP and the reason a tagged device -- the development
#: loop -- still cannot approve its own deploy.
VIA_DASHBOARD = "tailnet-identity"


def _error(error_type: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error_type": error_type, "message": message, **extra}


def approve_request(
    *,
    store: records.DeployStore,
    request_id: str,
    approved_by: str,
    via: str,
    note: str = "",
    override_ref: str = "",
    override_reason: str = "",
    override_allows_migration: bool = False,
) -> dict[str, Any]:
    """Run the approval checks, record the decision, start the runner.

    One implementation for both doors on purpose. The checks are the
    substance of R-1 and R-2 at approval time -- a ref override needs a
    reason, migrations need separate consent, a rollback may not carry
    either -- and a second copy of them would eventually disagree with
    the first.
    """
    try:
        request = store.load(request_id)
    except KeyError:
        return _error("not_found", f"no deploy request {request_id!r}")

    if request.status != STATUS_PENDING:
        return _error(
            "not_pending",
            f"request {request_id} is {request.status}, not {STATUS_PENDING}. "
            "A request is approved once; file a new one to deploy again.",
            status=request.status,
        )

    if not approved_by.strip():
        return _error("approved_by_required", "say who is approving; it goes in the audit log")

    if request.is_rollback and (override_ref or override_allows_migration):
        return _error(
            "override_on_rollback",
            "a rollback already has its commit, taken from the record of the deploy "
            "it undoes; overriding the ref or unblocking migrations on top of that is "
            "not a rollback. File a normal deploy request instead.",
        )

    # Asking explicitly for the default ref is not an override. Left as
    # one, the run was pinned like a normal deploy (on the branch, not
    # detached) while `is_default_ref` stayed false and shut the
    # migration gate -- two halves of the system disagreeing about what
    # kind of deploy it was.
    override_ref = "" if override_ref.strip() == registry.DEPLOY_REF else override_ref.strip()

    if override_ref and not override_reason.strip():
        return _error(
            "override_reason_required",
            "a ref override has to come with a reason -- it is what makes the "
            "override auditable rather than merely possible",
        )
    if override_allows_migration and not override_ref:
        return _error(
            "override_migration_without_override",
            "override_allows_migration only means something alongside override_ref; "
            "the default ref already allows migrations",
        )

    request.status = STATUS_APPROVED
    request.approved_by = approved_by
    request.approved_at = records.utcnow()
    request.approved_via = via
    request.approval_note = note or None
    request.override_ref = override_ref or None
    request.override_reason = override_reason or None
    request.override_allows_migration = bool(override_allows_migration)
    # Written *before* the launch, because after it the runner owns this
    # file. Saving a copy taken before the launch would race the runner's
    # own write and drop whichever side lost.
    request.runner_unit = launcher.unit_name(request.request_id)
    store.save(request)
    store.audit(
        "approved",
        request_id=request.request_id,
        target=request.target,
        actor=approved_by,
        via=via,
        note=note or None,
        ref=request.ref,
        override_ref=request.override_ref,
        override_reason=request.override_reason,
        override_allows_migration=request.override_allows_migration,
        rollback_of=request.rollback_of,
        rollback_to_sha=request.rollback_to_sha,
    )

    ok, detail = launcher.launch(request.request_id)
    if not ok:
        request = store.load(request.request_id)
        request.status = records.STATUS_FAILED
        request.finished_at = records.utcnow()
        request.result = {"ok": False, "error": detail, "service_state": "unknown"}
        store.save(request)
        store.audit(
            "launch_failed",
            request_id=request.request_id,
            target=request.target,
            error=detail,
        )
        return _error(
            "launch_failed",
            f"approved, but the deploy runner did not start: {detail}. "
            "Nothing was deployed.",
            request_id=request.request_id,
        )

    return {
        "ok": True,
        "request_id": request.request_id,
        "target": request.target,
        "status": records.STATUS_RUNNING,
        "ref": request.ref,
        "approved_via": via,
        "unit": detail,
        "note": "the deploy is running; poll deploy_status for the result",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m magickit.deploy.approval",
        description=(
            "Approve a pending deploy request from this host. The same checks and "
            "the same audit trail as the authenticated MCP tool; the record says it "
            "came from here."
        ),
    )
    parser.add_argument("request_id", help="from deploy_request / deploy_history")
    parser.add_argument(
        "--by",
        required=True,
        help="who is approving. A record, not a credential -- nothing authenticates it.",
    )
    parser.add_argument("--note", default="", help="kept with the approval")
    parser.add_argument(
        "--override-ref",
        default="",
        help="deploy something other than origin/main. Requires --override-reason.",
    )
    parser.add_argument("--override-reason", default="", help="why the override is justified")
    parser.add_argument(
        "--override-allows-migration",
        action="store_true",
        help=(
            "let an overridden ref apply migrations too. Code from an unmerged branch "
            "is undone by putting main back; a migration from one is not."
        ),
    )
    args = parser.parse_args(argv)

    result = approve_request(
        store=records.get_store(),
        request_id=args.request_id,
        approved_by=args.by,
        via=VIA_HOST,
        note=args.note,
        override_ref=args.override_ref,
        override_reason=args.override_reason,
        override_allows_migration=args.override_allows_migration,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
