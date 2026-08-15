"""The deploy history page. Read-only, deliberately.

R-8 says a failed deploy must be investigable without reaching the host.
The MCP tools already satisfy that; this page satisfies the other half
of it -- being able to *look* at what has been deployed without knowing
which tool to call, which is what someone does when something is wrong
and they are not sure what yet.

There is no approve button here and there must not be one. This app is
the tailnet front door and is unauthenticated (ADR-2026-06-04-18 D-5);
the whole reason approval lives on the OAuth-gated MCP instance is that
the unauthenticated surface must not be able to restart a service. A
form on this page would undo that in one commit.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from magickit.deploy import records, registry
from magickit.utils.logging import get_logger
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
    "override_reason",
    "error",
    "service_state",
    "rollback_to_sha",
)


def _row(request: records.DeployRequest) -> dict[str, Any]:
    result = request.result or {}
    return {
        "created_at": request.created_at,
        "target": request.target,
        "status": request.status,
        "status_class": _STATUS_CLASS.get(request.status, "unknown"),
        "requested_by": request.requested_by,
        "approved_by": request.approved_by,
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


@router.get("/dashboard/deploys", response_class=HTMLResponse)
async def deploys_page(request: Request) -> HTMLResponse:
    store = records.get_store()

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

    return templates.TemplateResponse(
        request,
        "deploys.html",
        {
            "active_page": "deploys",
            "targets": targets,
            "requests": [_row(r) for r in store.list_requests(limit=30)],
            "events": [_event(e) for e in store.read_audit(limit=40)][::-1],
        },
    )


__all__ = ["router"]
