"""PR 一覧 (``/dashboard/prs``): what is sitting open across the org.

Two tables, each row linking to the PR on GitHub:

- **マージ待ち** — gate 済: the independent naysayer's APPROVE stands at
  the current head and the PR is neither conflicting nor a draft. The
  next action is a human clicking merge.
- **停滞** — no update for ``prs_stale_hours``. Drafts included: a draft
  nobody touched is still a thing somebody forgot.

How this differs from the board's マージ lane
---------------------------------------------

The board asks "what is waiting for me right now" and reads a fixed
allowlist of repos, because every card there costs a chatroom ledger
lookup. This page asks "what is sitting open", which is a question about
the whole org — a PR in a repo nobody put on a list is exactly the one
that gets forgotten (measured 2026-09-29: two 17-day-old PRs in
``UeRestartCommand*`` that no Magickit surface showed).

So the live set comes from **one search** (``org:<org> is:pr is:open``),
not a per-repo list. The search payload already carries created/updated
times and the draft flag, which is everything the 停滞 table needs.

The gate 済 judgement is **not reimplemented**: it calls
:func:`magickit.core.pr_watch.collect_pr_snapshots` on the repos the
search found, the same function the board uses. Two implementations of
"approved at head" would drift, and a PR would show 済 here and 未 there.
It runs on its own :class:`~magickit.core.pr_watch.PrWatchState`: that
function prunes its caches down to the repos it was just given, so
sharing the board's state would have each caller evict the other's
entries every cycle.

Cost is bounded by caching the whole result for ``prs_refresh_seconds``.
The page polls more often than that; a poll inside the window re-renders
the cached result, so ages keep moving without spending API calls.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from magickit.config import Settings, get_settings
from magickit.core import pr_watch
from magickit.utils.logging import get_logger
from magickit.web.deps import parse_ts, templates

logger = get_logger(__name__)

router = APIRouter()

_NO_STORE = "no-store"

#: Search pages read per refresh. 100 per page is GitHub's maximum; three
#: pages is far above today's live set (8 PRs outside the excluded repo).
#: Hitting the ceiling is reported, not truncated silently.
_MAX_SEARCH_PAGES = 3
_SEARCH_PER_PAGE = 100

_REPO_API_PREFIX = "https://api.github.com/repos/"

#: The page's own watcher state (see module docstring for why not the
#: board's).
_WATCH_STATE = pr_watch.PrWatchState()

#: ``(monotonic fetched_at, overview)`` of the last successful read.
_CACHE: tuple[float, Overview] | None = None
_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class PrRow:
    """One open PR as the search returned it."""

    owner: str
    repo: str
    number: int
    title: str
    html_url: str
    author: str
    created_at: str
    updated_at: str
    draft: bool

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.owner, self.repo, self.number)


@dataclass
class Overview:
    """Everything one GitHub read produced. Rendered as-is."""

    checked_at: datetime
    rows: list[PrRow]
    approved: set[tuple[str, str, int]]
    notices: list[str]


def excluded_repo_names(org: str, entries: list[str]) -> list[str]:
    """Normalise ``prs_exclude_repos`` to bare repo names inside ``org``.

    ``owner/repo`` for another owner cannot be excluded from an ``org:``
    search (it was never in it), so it is dropped with a warning rather
    than turned into a qualifier that silently does nothing.
    """
    names: list[str] = []
    for raw in entries:
        text = str(raw or "").strip()
        if not text:
            continue
        owner, sep, repo = text.rpartition("/")
        if sep and owner.lower() != org.lower():
            logger.warning("prs: exclude entry outside org ignored", entry=raw, org=org)
            continue
        if repo and repo not in names:
            names.append(repo)
    return names


def build_query(org: str, excluded: list[str]) -> str:
    """The one search that defines the page's live set."""
    parts = [f"org:{org}", "is:pr", "is:open"]
    parts.extend(f"-repo:{org}/{name}" for name in excluded)
    return " ".join(parts)


async def _search_page(query: str, page: int) -> Any:
    """One ``search_pull_requests`` round trip; the decoded payload or None."""
    from magickit.mcp.github_dispatch import _mcp_call, _resolve_pat  # noqa: PLC0415

    pat = _resolve_pat("GITHUB_MCP_PAT_IMPLEMENTER")
    result = await _mcp_call(
        "tools/call",
        {
            "name": "search_pull_requests",
            "arguments": {
                "query": query,
                "perPage": _SEARCH_PER_PAGE,
                "page": page,
                "sort": "updated",
                "order": "asc",
            },
        },
        pat,
    )
    return pr_watch._first_json_payload(result)


def _row_from_item(item: dict[str, Any]) -> PrRow | None:
    repo_url = str(item.get("repository_url") or "")
    if not repo_url.startswith(_REPO_API_PREFIX):
        return None
    owner, _, repo = repo_url[len(_REPO_API_PREFIX):].partition("/")
    number = item.get("number")
    if not owner or not repo or not isinstance(number, int):
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    return PrRow(
        owner=owner,
        repo=repo,
        number=number,
        title=str(item.get("title") or ""),
        html_url=str(item.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}"),
        author=str(user.get("login") or ""),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
        draft=item.get("draft") is True,
    )


async def search_open_prs(
    query: str, *, search_page: Any = None
) -> tuple[list[PrRow] | None, list[str]]:
    """Every open PR matching ``query``, or ``None`` when GitHub could not be read.

    ``None`` and ``[]`` are different answers and the page says different
    things for them: "no open PRs" versus "could not ask". A payload that is
    not a search result (an error envelope such as ``{"message": ...}``)
    counts as could-not-ask, for the same reason as
    :func:`magickit.core.pr_watch._fetch_open_prs`.
    """
    search_page = search_page or _search_page
    rows: list[PrRow] = []
    notices: list[str] = []
    total = 0
    for page in range(1, _MAX_SEARCH_PAGES + 1):
        try:
            payload = await search_page(query, page)
        except Exception as exc:  # noqa: BLE001 - 読めなかったと言う
            logger.warning("prs: search failed", query=query, page=page, err=str(exc))
            return None, [f"GitHub の PR 検索に失敗しました ({exc})"]
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            detail = payload.get("message") if isinstance(payload, dict) else None
            logger.warning("prs: search unrecognized payload", query=query, page=page)
            return None, [
                "GitHub の PR 検索が結果を返しませんでした"
                + (f" ({detail})" if detail else "")
            ]
        total = int(payload.get("total_count") or 0)
        rows.extend(r for r in (_row_from_item(i) for i in items if isinstance(i, dict)) if r)
        if len(items) < _SEARCH_PER_PAGE or len(rows) >= total:
            break
    if payload.get("incomplete_results") is True:
        notices.append("GitHub の検索がタイムアウトし、結果が欠けている可能性があります")
    if total > len(rows):
        notices.append(
            f"open な PR {total} 件のうち {len(rows)} 件だけを表示しています"
            f"（検索の上限 {_MAX_SEARCH_PAGES * _SEARCH_PER_PAGE} 件）"
        )
    return rows, notices


class _SearchFailedError(Exception):
    """The search itself failed: there is no live set to show."""

    def __init__(self, notices: list[str]) -> None:
        super().__init__("; ".join(notices))
        self.notices = notices


async def read_overview(
    settings: Settings,
    *,
    search_page: Any = None,
    collect_snapshots: Any = None,
    state: pr_watch.PrWatchState | None = None,
) -> Overview:
    """One full GitHub read.

    Raises :class:`_SearchFailedError` when the search itself failed. An
    ``Overview`` with no rows would render as "0 件", which is the one
    thing a failed read must not say.

    A failure of the gate judgement alone does not fail the read: the
    停滞 table needs only the search, so it is still shown, with a notice
    that the マージ待ち table is unknown rather than empty.
    """
    excluded = excluded_repo_names(settings.prs_org, settings.prs_exclude_repos)
    rows, notices = await search_open_prs(
        build_query(settings.prs_org, excluded), search_page=search_page
    )
    if rows is None:
        raise _SearchFailedError(notices)

    repos = sorted({(r.owner, r.repo) for r in rows if not r.draft})
    approved: set[tuple[str, str, int]] = set()
    if repos:
        collect_snapshots = collect_snapshots or pr_watch.collect_pr_snapshots
        try:
            snapshots, watch_notices, _failed = await collect_snapshots(
                repos, state or _WATCH_STATE
            )
        except Exception as exc:  # noqa: BLE001 - 停滞の表は出す
            logger.warning("prs: pr_watch failed", err=str(exc))
            notices.append(f"gate の判定を読めません。マージ待ちの表は空ではなく不明です ({exc})")
        else:
            notices.extend(watch_notices)
            approved = {pr_watch.pr_key(s) for s in snapshots if s.artifact_approved}
            capped = sum(1 for s in snapshots if s.rate_capped and not s.artifact_approved)
            if capped:
                notices.append(
                    f"{capped} 件は rate-cap で review を読み直せていません"
                    "（gate 済でもマージ待ちに載っていない可能性があります）"
                )

    return Overview(
        checked_at=datetime.now(timezone.utc),
        rows=rows,
        approved=approved,
        notices=notices,
    )


async def get_overview(settings: Settings) -> tuple[Overview | None, list[str]]:
    """The cached read, refreshed at most every ``prs_refresh_seconds``.

    A failed read is not cached: the next poll retries, which costs one
    search call a minute while GitHub is down — cheaper than showing a
    stale failure for five minutes after it recovers.
    """
    global _CACHE
    async with _LOCK:
        now = time.monotonic()
        if _CACHE is not None and now - _CACHE[0] < settings.prs_refresh_seconds:
            return _CACHE[1], []
        try:
            overview = await read_overview(settings)
        except _SearchFailedError as exc:
            return None, exc.notices
        _CACHE = (now, overview)
        return overview, []


def build_context(
    overview: Overview | None,
    failure_notices: list[str],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Split the read into the two tables. Pure — tests call it directly."""
    now = now or datetime.now(timezone.utc)
    excluded = excluded_repo_names(settings.prs_org, settings.prs_exclude_repos)
    base: dict[str, Any] = {
        "org": settings.prs_org,
        "excluded": excluded,
        "stale_hours": settings.prs_stale_hours,
        "stale_label": _hours_label(settings.prs_stale_hours),
    }
    if overview is None:
        return {**base, "unavailable": True, "notices": failure_notices,
                "merge_ready": [], "stale": [], "total": 0, "checked_at": None}

    cutoff = now - timedelta(hours=settings.prs_stale_hours)
    merge_ready = sorted(
        (r for r in overview.rows if r.key in overview.approved),
        key=lambda r: r.updated_at,
    )
    stale = sorted(
        (
            r for r in overview.rows
            if (ts := parse_ts(r.updated_at)) is not None and ts <= cutoff
        ),
        key=lambda r: r.updated_at,
    )
    return {
        **base,
        "unavailable": False,
        "notices": overview.notices,
        "merge_ready": merge_ready,
        "stale": stale,
        "total": len(overview.rows),
        "checked_at": overview.checked_at,
    }


def _hours_label(hours: float) -> str:
    if hours >= 24 and hours % 24 == 0:
        return f"{int(hours // 24)}日"
    return f"{hours:g}時間"


# --- routes ----------------------------------------------------------------


@router.get("/dashboard/prs", response_class=HTMLResponse)
async def prs_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "prs.html",
        {
            "active_page": "prs",
            "stale_label": _hours_label(settings.prs_stale_hours),
        },
        headers={"Cache-Control": _NO_STORE},
    )


@router.get("/dashboard/prs/_tables", response_class=HTMLResponse)
async def prs_fragment(request: Request) -> HTMLResponse:
    settings = get_settings()
    overview, failure_notices = await get_overview(settings)
    return templates.TemplateResponse(
        request,
        "partials/prs_tables.html",
        build_context(overview, failure_notices, settings),
        headers={"Cache-Control": _NO_STORE},
    )
