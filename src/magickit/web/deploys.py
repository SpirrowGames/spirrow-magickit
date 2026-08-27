"""The deploy history page, and the third approval door.

R-8 says a failed deploy must be investigable without reaching the host.
The MCP tools already satisfy that; this page satisfies the other half
of it -- being able to *look* at what has been deployed without knowing
which tool to call, which is what someone does when something is wrong
and they are not sure what yet.

**This file used to say there must never be an approve button here, and
the reason it gave was right:** the app is the tailnet front door, it is
unauthenticated (ADR-2026-06-04-18 D-5), and approval restarts services
and runs migrations. A form added on those terms would have handed
restart-and-migrate to anyone who could reach the port -- which, measured
2026-08-27, included every device on the LAN, because the bind was
``0.0.0.0`` rather than the loopback the comment assumed.

What changed is not the appetite for risk but the availability of an
actor. ``tailscale serve`` knows which tailnet user it is proxying for
and says so in a header it will not let the client set; the app now binds
loopback, so that header cannot arrive any other way. Approval here is
therefore gated on a *named user from an allowlist*, which is the same
standard the OAuth door applies, rather than on "reached the page".
:mod:`magickit.web.identity` carries the measurements and the exact
conditions under which the header is worth anything.

The invariant that mattered is intact. The development loop runs on a
tagged device, a tagged device has no user login, and an entry in
``deploy.approver_logins`` is a user login -- so the loop still cannot
approve its own deploy, for the same reason it cannot call a tool that is
not registered. The allowlist is empty by default, so a deployment that
has not thought about this gets exactly the read-only page it had before.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from magickit.config import get_settings
from magickit.deploy import approval, records, registry
from magickit.utils.logging import get_logger
from magickit.web import identity
from magickit.web.deps import templates

logger = get_logger(__name__)

router = APIRouter()

#: Which CSS class a status gets. Anything unmapped falls back to
#: "unknown" rather than to a colour that implies things are fine.
_STATUS_CLASS = {
    records.STATUS_SUCCEEDED: "ok",
    records.STATUS_FAILED: "bad",
    records.STATUS_INTERRUPTED: "bad",
    records.STATUS_RUNNING: "busy",
    records.STATUS_APPROVED: "busy",
    records.STATUS_PENDING: "waiting",
}

#: Audit lines carry different fields per event; this picks the one worth
#: showing in a narrow column without dumping the whole record.
_DETAIL_KEYS = (
    "reason",
    "via",
    "override_reason",
    "error",
    "service_state",
    "rollback_to_sha",
)


def _row(request: records.DeployRequest) -> dict[str, Any]:
    result = request.result or {}
    return {
        # Needed by the approve form; harmless in the read-only render.
        "request_id": request.request_id,
        "is_pending": request.status == records.STATUS_PENDING,
        "reason": request.reason,
        "created_at": request.created_at,
        "target": request.target,
        "status": request.status,
        "status_class": _STATUS_CLASS.get(request.status, "unknown"),
        "requested_by": request.requested_by,
        "approved_by": request.approved_by,
        "approved_via": request.approved_via,
        "is_rollback": request.is_rollback,
        "override_ref": request.override_ref,
        "deployed_sha": (result.get("deployed_sha") or "")[:12] or None,
        "service_state": result.get("service_state"),
        # Shown only when there is one; a successful deploy gets no row.
        "error": result.get("error"),
    }


def _event(raw: dict[str, Any]) -> dict[str, Any]:
    detail = next((str(raw[key]) for key in _DETAIL_KEYS if raw.get(key)), "")
    return {
        "at": raw.get("at", ""),
        "event": raw.get("event", ""),
        "target": raw.get("target"),
        "actor": raw.get("actor"),
        "approved_by": raw.get("approved_by"),
        "detail": detail[:200],
    }


def _page_context(request: Request, *, flash: str | None = None,
                  flash_ok: bool = False) -> dict[str, Any]:
    """Everything both the GET and the post-approval redraw need.

    One builder, so the page a reader lands on and the page they get back
    after pressing the button cannot disagree about who may approve.
    """
    store = records.get_store()
    allowed = get_settings().deploy_approver_logins

    targets = []
    for name in registry.target_names():
        target = registry.resolve_target(name)
        targets.append(
            {
                "name": target.name,
                "services": list(target.services),
                "health_url": target.health_url,
                "has_backup": target.backup_script is not None,
            }
        )

    login = identity.tailnet_login(request)
    return {
        "active_page": "deploys",
        "targets": targets,
        "requests": [_row(r) for r in store.list_requests(limit=30)],
        "events": [_event(e) for e in store.read_audit(limit=40)][::-1],
        "can_approve": identity.is_approver(request, allowed),
        # Shown so a reader who cannot approve is told *why* rather than
        # left with a page that silently lacks a control: "no identity"
        # and "identity not on the list" are different problems with
        # different fixes.
        "viewer_login": login,
        "approvers_configured": bool(allowed),
        "flash": flash,
        "flash_ok": flash_ok,
    }


@router.get("/dashboard/deploys", response_class=HTMLResponse)
async def deploys_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "deploys.html", _page_context(request)
    )


@router.post("/dashboard/deploys/{request_id}/approve", response_class=HTMLResponse)
async def approve_deploy(
    request: Request,
    request_id: str,
    note: str = Form(""),
) -> HTMLResponse:
    """Approve one pending request, as the tailnet user who pressed it.

    Deliberately narrow. There is no ref override and no migration
    unblock on this form: both are the R-1/R-2 escape hatches, both need
    a written reason to be worth anything, and a button that carries
    them would make the dangerous path the convenient one. Those stay on
    the MCP and CLI doors, where the caller has to spell them out.

    ``approved_by`` is the tailnet identity, not a text box. The other
    two doors take a name because the actor is vouched for some other
    way (OAuth, or a shell on the host); here the vouching *is* the
    identity, so letting the form supply a different name would put a
    value in the audit log that nothing checked.
    """
    settings = get_settings()

    if identity.cross_site(request):
        # The identity header is real and still not enough: it says whose
        # browser this is, not whose intent it carries.
        logger.warning(
            "Deploy approval refused: cross-site POST",
            request_id=request_id,
            login=identity.tailnet_login(request),
        )
        return templates.TemplateResponse(
            request,
            "deploys.html",
            _page_context(request, flash="別サイトからの操作として拒否しました。"),
            status_code=403,
        )

    if not identity.is_approver(request, settings.deploy_approver_logins):
        login = identity.tailnet_login(request)
        logger.warning(
            "Deploy approval refused: not an approver",
            request_id=request_id,
            login=login,
        )
        return templates.TemplateResponse(
            request,
            "deploys.html",
            _page_context(
                request,
                flash=(
                    "この経路では承認できません。"
                    + (
                        f"tailnet identity = {login} は承認者リストにありません。"
                        if login
                        else "tailnet identity が付いていません "
                        "(:8443 の tailscale serve 経由で開いてください)。"
                    )
                ),
            ),
            status_code=403,
        )

    result = approval.approve_request(
        store=records.get_store(),
        request_id=request_id,
        approved_by=identity.tailnet_login(request) or "unknown",
        via=approval.VIA_DASHBOARD,
        note=note.strip(),
    )

    if not result.get("ok"):
        # approve_request's refusals are the substance of R-1/R-2 and are
        # written for a human; show the text rather than a status code.
        return templates.TemplateResponse(
            request,
            "deploys.html",
            _page_context(request, flash=str(result.get("message") or "承認できませんでした。")),
            status_code=409,
        )

    logger.info(
        "Deploy approved from dashboard",
        request_id=request_id,
        target=result.get("target"),
        approved_by=identity.tailnet_login(request),
    )
    return templates.TemplateResponse(
        request,
        "deploys.html",
        _page_context(
            request,
            flash=f"{result.get('target')} の deploy を開始しました。",
            flash_ok=True,
        ),
    )


__all__ = ["router"]
