"""稼働状況 (ops) view: is the autonomous loop actually turning?

The existing ``/dashboard`` answers "what is in Magickit's task queue",
which is Magickit's own SQLite and has nothing to do with the loop that
actually writes code. The chatroom UI answers "what are they saying".
Neither answers the question a human asks first, which is simply whether
anything is happening at all -- and answering it today means reading a
thread list, a control widget and a service log and doing the arithmetic
between three timestamps by hand.

So this page reports one derived judgement per project, from three
sources Magickit can already reach:

- Conclair's cross-project summary  -> when the project was last touched,
  and how much of it is blocked (awaiting a reply, or held at a gate).
- Conclair's loop control record    -> whether the loop is *allowed* to
  run (``desired``) and when it last said it was there (``observed``).
- Conclair's event log              -> which thread moved last, and who
  moved it, so "running" comes with evidence rather than a green dot.

Two axes, deliberately not collapsed into one
---------------------------------------------
**稼働軸** (running / stalled / held / unmanaged / unknown) says whether
the loop is turning. **ブロック軸** (awaiting reply, gated) says what it
is waiting for. A project can be alive and blocked, or stopped with
nothing blocking it, and folding those together produces a badge that
cannot distinguish "waiting for the naysayer" from "died two hours ago
while waiting for the naysayer" -- which is the whole point of the page.

``stalled`` is a suspicion, not a fact
--------------------------------------
Nothing here observes a process. The判定 is "no chatroom activity and no
heartbeat for ``ops_stall_minutes``", and a loop in the middle of one very
long turn looks identical. The page says so in words rather than implying
certainty with a colour; the threshold is configurable because the right
value depends on how long a turn actually takes.

Note this is a different question from the ``stale`` marker on Conclair's
own control widget (15 min): that one watches ``observed_at`` alone, for
one project, and asks "has the conductor checked in". This one folds in
chatroom activity too, so a project whose loop reports rarely but whose
AIs are visibly talking is not accused of being down.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from magickit.adapters.base import BaseAdapter
from magickit.adapters.chatroom import ChatroomAdapter
from magickit.adapters.cognilens import CognilensAdapter
from magickit.adapters.lexora import LexoraAdapter
from magickit.adapters.prismind import PrismindAdapter
from magickit.config import Settings, get_settings
from magickit.utils.logging import get_logger
from magickit.web.deps import age_seconds, humanize_age, parse_ts, templates

logger = get_logger(__name__)

router = APIRouter(tags=["ops"])

#: Statuses that still owe someone something. Mirrors the chatroom panel:
#: resolved / superseded / parked are not work in flight.
OPEN_STATUSES = ("active", "awaiting_reply")

#: Rows rendered before the list is cut. The sort puts the projects that
#: need a human first, so the tail is the quiet end -- but a truncated
#: list still has to say it was truncated.
MAX_ROWS = 20

#: Loop-control states, in the order the buttons are drawn. The label and
#: the hover text repeat Conclair's widget on purpose: two surfaces that
#: set the same record must not describe it differently.
CONTROL_CHOICES = (
    ("run", "RUN", "完全自律 — independent naysayer の proceed で implementer まで進む"),
    (
        "supervised",
        "SUPERVISED",
        "設計ループのみ — human decide / PR-gate REQUEST_CHANGES だけがコードに到達",
    ),
    ("hold", "HOLD", "停止 — sweep は起動せず、実行中の conductor は次のラウンド境界で止まる"),
)

#: Ceiling on one backend health probe, in seconds. Well under the
#: strip's own 60s poll so a wedged backend cannot stack requests behind
#: it, and several times the ~2.5s the four take warm (Lexora's own
#: /health is the slow one; it polls its backends). A probe that blows
#: this reports 確認不可 and the next poll corrects it -- which is the
#: honest answer anyway, since a health check that will not answer has
#: told you nothing about the service.
PROBE_TIMEOUT = 10.0

#: 稼働軸. Ordered worst-first; the row sort reads the index directly, so
#: adding a state here places it in the ranking too.
STATUS_ORDER = ("unknown", "stalled", "held", "running", "unmanaged")

STATUS_LABELS = {
    "running": "稼働中",
    "stalled": "停止疑い",
    "held": "停止 (HOLD)",
    "unmanaged": "ループ未接続",
    "unknown": "不明",
}


@dataclass
class ProjectOps:
    """One row: everything the page says about a single project."""

    project: str
    thread_count: int = 0
    message_count: int = 0
    open_threads: int = 0
    awaiting: int = 0
    gated: int = 0
    last_activity_at: datetime | None = None

    desired: str | None = None
    desired_actor: str | None = None
    configured: bool = False
    observed: str | None = None
    observed_actor: str | None = None
    observed_at: datetime | None = None
    control_error: str | None = None

    last_event: dict[str, Any] = field(default_factory=dict)

    # The digest of `last_event.thread_id`, when one is stored. Answers a
    # third question the two axes do not: 何を話しているのか. Never another
    # thread's digest -- see `_row_digest`.
    digest: dict[str, Any] | None = None
    digest_chars: int = 160

    # Derived below; templates read these rather than recomputing.
    status: str = "unknown"
    heartbeat_at: datetime | None = None
    idle_seconds: float | None = None
    idle_text: str = "不明"
    diverged: bool = False

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def digest_line(self) -> str:
        """The digest, cut to one scannable line. Empty when there is none.

        Truncated because a 400-token digest is 4-6 lines and 20 rows of
        that stops being readable in one screen, which is this page's whole
        claim. The full text goes in the cell's `title`.
        """
        if not self.digest:
            return ""
        text = " ".join(str(self.digest.get("digest", "")).split())
        if len(text) <= self.digest_chars:
            return text
        return text[: self.digest_chars - 1] + "…"

    @property
    def digest_full(self) -> str:
        return str(self.digest.get("digest", "")) if self.digest else ""

    @property
    def digest_is_stale(self) -> bool:
        """Only ever from Conclair's own verdict.

        This page's standing rule is to make no claim it cannot back. A
        digest response without `stale` (an older Conclair) gets its age
        shown and no staleness claim at all.
        """
        return bool(self.digest and self.digest.get("stale") is True)

    @property
    def digest_generated_at(self) -> datetime | None:
        if not self.digest:
            return None
        return parse_ts(self.digest.get("generated_at"))

    @property
    def blocked_note(self) -> str:
        """One phrase for the ブロック軸, or "" when nothing blocks."""
        parts = []
        if self.gated:
            parts.append(f"gate 待ち {self.gated}")
        if self.awaiting:
            parts.append(f"返答待ち {self.awaiting}")
        return " / ".join(parts)


def _open_count(by_status: dict[str, Any]) -> int:
    return sum(int(by_status.get(s, 0) or 0) for s in OPEN_STATUSES)


def _is_error(payload: Any) -> bool:
    """Conclair signals failure with an envelope, not a ``success`` flag."""
    return isinstance(payload, dict) and "error_type" in payload


def classify(row: ProjectOps, *, stall_seconds: float, now: datetime) -> None:
    """Fill in the derived fields on ``row``.

    Order matters and encodes the precedence the page promises:

    1. A control read that *failed* is ``unknown``. Callers of this record
       are contractually required to treat a failed read as ``hold``
       (Conclair's ``GET`` never 404s precisely so the two stay apart), so
       inventing "probably running" here would be the same mistake.
    2. ``hold`` outranks staleness: a project someone stopped is not a
       project that died, and painting it red would train the reader to
       ignore red.
    3. A loop that has never reported ``observed`` is ``unmanaged``, not
       stalled. Old scratch projects have no conductor and never will;
       calling them stalled fills the page with alarms nobody can act on.
    4. Otherwise staleness, measured from the *later* of the heartbeat and
       the last chatroom message -- either one proves something is alive.
    """
    heartbeat_candidates = [
        ts for ts in (row.observed_at, row.last_activity_at) if ts is not None
    ]
    row.heartbeat_at = max(heartbeat_candidates) if heartbeat_candidates else None
    row.idle_seconds = (
        (now - row.heartbeat_at).total_seconds() if row.heartbeat_at else None
    )
    row.idle_text = humanize_age(row.idle_seconds)
    # "Pending" means a loop has not caught up yet, so it needs a loop.
    # `configured` alone is not enough: setting HOLD on a project no
    # conductor runs would then read as 反映待ち forever, which describes a
    # loop that is lagging rather than one that does not exist.
    row.diverged = bool(
        row.configured
        and row.desired is not None
        and row.observed_at is not None
        and row.observed != row.desired
    )

    if row.control_error is not None:
        row.status = "unknown"
    elif row.desired == "hold":
        row.status = "held"
    elif row.observed_at is None:
        row.status = "unmanaged"
    elif row.idle_seconds is not None and row.idle_seconds > stall_seconds:
        row.status = "stalled"
    else:
        row.status = "running"


def _sort_key(row: ProjectOps) -> tuple[int, float]:
    """Worst first; within a state, the most recently touched first.

    The negation on idle is what puts a project that stalled *just now*
    above one that has been dead for a week: the fresh one is the one a
    human can still do something about.
    """
    severity = (
        STATUS_ORDER.index(row.status) if row.status in STATUS_ORDER else len(STATUS_ORDER)
    )
    return (severity, row.idle_seconds if row.idle_seconds is not None else float("inf"))


async def _row_details(adapter: ChatroomAdapter, row: ProjectOps) -> None:
    """Attach the loop-control record and the last event to one row.

    The two reads are independent, so a project whose event log errors
    still shows its control state and vice versa. Both are per-project
    endpoints -- Conclair has no cross-project form of either -- so this
    is an N+1 by construction. It is loopback traffic for a handful of
    projects; if the project list ever grows past that, the fix is an
    aggregate endpoint in Conclair, not a shorter list here.
    """
    control, events = await asyncio.gather(
        adapter.get_loop_control(project=row.project),
        adapter.list_events(project=row.project, limit=1),
        return_exceptions=True,
    )

    if isinstance(control, BaseException) or _is_error(control):
        row.control_error = (
            str(control)
            if isinstance(control, BaseException)
            else str(control.get("error", "control read failed"))
        )
    elif isinstance(control, dict) and "desired_state" in control:
        row.desired = control.get("desired_state")
        row.desired_actor = control.get("desired_actor")
        row.configured = bool(control.get("configured"))
        row.observed = control.get("observed_state")
        row.observed_actor = control.get("observed_actor")
        row.observed_at = parse_ts(control.get("observed_at"))
    else:
        # A 200 without `desired_state` is a Conclair that does not serve
        # this endpoint -- a deploy fact. Not knowing is `unknown`, which
        # is exactly what an unset control_error would hide.
        row.control_error = "conclair が control を返しませんでした (要 deploy 確認)"

    if not isinstance(events, BaseException) and not _is_error(events):
        items = (events or {}).get("items") or []
        if items:
            latest = items[0]
            row.last_event = {
                "thread_id": latest.get("thread_id") or "",
                "actor": latest.get("actor") or "",
                "action": latest.get("action") or "",
                "timestamp": latest.get("timestamp"),
            }

    await _row_digest(adapter, row)


async def _row_digest(adapter: ChatroomAdapter, row: ProjectOps) -> None:
    """Attach the digest of the thread that moved last, if there is one.

    **Which thread**: ``last_event.thread_id``, which the read above already
    produced -- so selecting it costs no extra call. Not the
    ``awaiting_reply`` one: "awaiting a reply" is already the ブロック軸
    badge, and putting that thread's digest in a second place would make the
    blocked axis louder and the 稼働軸 quieter, which is exactly the
    two-axis collapse this module forbids. The digest answers a *third*
    question -- 何を話しているのか -- and the thread that moved last is the
    honest answer to it.

    **Never substituted.** If that thread has no digest, the cell says so.
    A digest labelled with this project that describes thread B while the
    直近の動き cell says thread A is the worst possible cell on this page.

    A separate step rather than a third leg of the gather above, because it
    depends on that gather's result. Guarded on its own: a missing digest is
    not a reason to stop reporting whether anything is running, so this
    never reaches the `unavailable` path that blanks the page.
    """
    thread_id = row.last_event.get("thread_id")
    if not thread_id:
        return
    try:
        stored = await adapter.get_thread_digest(
            project=row.project, thread_id=str(thread_id)
        )
    except Exception as e:  # noqa: BLE001 - a dead cell must not kill the page
        logger.warning(
            "Digest read failed for the ops row",
            project=row.project,
            thread_id=thread_id,
            error=str(e),
        )
        return
    if _is_error(stored) or not stored.get("present"):
        return
    digest = stored.get("digest")
    if isinstance(digest, dict):
        row.digest = digest


async def collect(settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """Build the whole view. Returns the template context."""
    now = now or datetime.now(timezone.utc)
    stall_seconds = settings.ops_stall_minutes * 60

    adapter = ChatroomAdapter(
        base_url=settings.conclair_url, timeout=settings.conclair_timeout
    )
    try:
        try:
            summaries = await adapter.list_project_summaries()
        except Exception as e:  # noqa: BLE001 - a dead panel must not kill the page
            logger.warning("Ops summary unavailable", error=str(e))
            return {"unavailable": f"conclair に接続できません ({e})", "rows": []}

        if _is_error(summaries):
            return {
                "unavailable": f"conclair: {summaries.get('error', '')}",
                "rows": [],
            }
        if "items" not in summaries:
            # Same reasoning as the chatroom panel: a 404 body is not an
            # empty chatroom, it is an out-of-date Conclair.
            return {
                "unavailable": (
                    "conclair が project summary を返しませんでした "
                    "(spirrow-conclair.service は最新ですか)"
                ),
                "rows": [],
            }

        rows = []
        for entry in summaries["items"]:
            by_status = entry.get("threads_by_status") or {}
            rows.append(
                ProjectOps(
                    project=str(entry.get("project", "")),
                    thread_count=int(entry.get("thread_count", 0) or 0),
                    message_count=int(entry.get("message_count", 0) or 0),
                    open_threads=_open_count(by_status),
                    awaiting=int(by_status.get("awaiting_reply", 0) or 0),
                    gated=int(entry.get("gated_thread_count", 0) or 0),
                    last_activity_at=parse_ts(entry.get("last_activity_at")),
                    digest_chars=settings.digest_dashboard_chars,
                )
            )

        await asyncio.gather(*(_row_details(adapter, row) for row in rows))
    finally:
        await adapter.close()

    for row in rows:
        classify(row, stall_seconds=stall_seconds, now=now)

    rows.sort(key=_sort_key)
    shown = rows[:MAX_ROWS]

    counts = {state: 0 for state in STATUS_ORDER}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1

    return {
        "rows": shown,
        "hidden": len(rows) - len(shown),
        "counts": counts,
        "status_labels": STATUS_LABELS,
        "status_order": STATUS_ORDER,
        "control_choices": CONTROL_CHOICES,
        "stall_minutes": settings.ops_stall_minutes,
        "checked_at": now,
        "unavailable": None,
    }


# --- routes ---------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def ops_page(request: Request) -> HTMLResponse:
    """The page. Its table arrives from the fragment below."""
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "ops.html",
        {
            "active_page": "ops",
            "stall_minutes": settings.ops_stall_minutes,
        },
    )


@router.get("/dashboard/_ops", response_class=HTMLResponse)
async def ops_fragment(request: Request) -> HTMLResponse:
    """The status table (HTMX polling target)."""
    context = await collect(get_settings())
    return templates.TemplateResponse(request, "partials/ops_table.html", context)


@router.post("/dashboard/_ops/{project}/control", response_class=HTMLResponse)
async def ops_set_control(
    request: Request,
    project: str,
    state: str = Form(...),
    actor: str = Form(""),
    note: str = Form(""),
) -> HTMLResponse:
    """Set a project's desired loop state, then redraw the whole table.

    The table is the swap target rather than the one row: a state change
    moves the row (the sort is by severity), and swapping a row in place
    would leave it sitting under the wrong heading until the next poll.

    ``actor`` is a record, not an authentication -- the same position
    Conclair takes, and the page says so. An empty box still writes a
    value rather than failing, because a control action lost to a
    validation error is worse than one attributed to "unknown".
    """
    settings = get_settings()
    adapter = ChatroomAdapter(
        base_url=settings.conclair_url, timeout=settings.conclair_timeout
    )
    error: str | None = None
    try:
        result = await adapter.set_loop_control(
            project=project,
            state=state,
            actor=actor.strip() or "unknown (ops UI)",
            note=note.strip() or None,
        )
        if _is_error(result):
            error = f"{result.get('error_type')}: {result.get('error')}"
    except Exception as e:  # noqa: BLE001 - render the failure, keep the buttons
        logger.warning("Loop control set failed", project=project, error=str(e))
        error = str(e)
    finally:
        await adapter.close()

    context = await collect(settings)
    context["flash_error"] = error
    return templates.TemplateResponse(request, "partials/ops_table.html", context)


@router.get("/dashboard/_ops_health", response_class=HTMLResponse)
async def ops_health_fragment(request: Request) -> HTMLResponse:
    """Backend health strip.

    Its own fragment on its own (slower) poll: the Prismind probe is an
    MCP round trip and can take seconds, and the status table must not
    wait behind it. "Why did it stop" is very often "a backend went
    down", which is why this is on the page at all.

    Every probe is capped at ``PROBE_TIMEOUT``. The adapters' own timeouts
    are sized for real work -- Prismind's is 360s, because a document
    write does Drive plus an embedding plus a Qdrant write -- and a strip
    that inherits those can outlive its own 60s poll and stack requests
    behind it. Measured warm: ~2.5s for all four, dominated by Lexora.
    """
    settings = get_settings()

    async def probe(name: str, build) -> tuple[str, bool | None]:
        """Construct, ask, and clean up -- reporting all three failures alike.

        Constructing counts as part of the probe: Conclair and Lexora take
        ``base_url`` while Cognilens and Prismind are MCP clients taking
        ``sse_url``, and getting that wrong raises before any request is
        made. A strip that 500s takes the whole page's explanation of *why
        nothing is running* with it, so the failure belongs in one cell.
        """
        adapter = None
        try:
            adapter = build()
            healthy = await asyncio.wait_for(
                adapter.health_check(), timeout=PROBE_TIMEOUT
            )
            return name, bool(healthy)
        except Exception as e:  # noqa: BLE001
            logger.warning("Ops health probe failed", service=name, error=str(e))
            return name, None
        finally:
            # `isinstance`, not `getattr(adapter, "close", None)`:
            # MCPBaseAdapter.__getattr__ turns *any* unknown attribute into
            # an MCP tool call, so duck-typing for a `close` does not find
            # one -- it invents one and fires a bogus `close` tool over the
            # wire on every poll. Only the HTTP adapters hold a client that
            # needs releasing; the MCP ones open a session per call.
            if isinstance(adapter, BaseAdapter):
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001 - probe cleanup is best effort
                    pass

    probes = [
        probe(
            "conclair",
            lambda: ChatroomAdapter(
                base_url=settings.conclair_url, timeout=settings.conclair_timeout
            ),
        ),
        probe(
            "lexora",
            lambda: LexoraAdapter(
                base_url=settings.lexora_url, timeout=settings.lexora_timeout
            ),
        ),
        probe(
            "cognilens",
            lambda: CognilensAdapter(
                sse_url=settings.cognilens_url, timeout=settings.cognilens_timeout
            ),
        ),
        probe(
            "prismind",
            lambda: PrismindAdapter(
                sse_url=settings.prismind_url, timeout=settings.prismind_timeout
            ),
        ),
    ]

    results = dict(await asyncio.gather(*probes))
    return templates.TemplateResponse(
        request, "partials/ops_health.html", {"services": results}
    )


__all__ = ["router", "collect", "classify", "ProjectOps", "age_seconds"]
