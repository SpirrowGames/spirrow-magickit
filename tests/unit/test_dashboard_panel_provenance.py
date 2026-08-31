"""Two guards that keep the dashboard from re-growing "which project?" ambiguity.

Background lives in T-dashboard-panels-do-not-name-the-project. Two things
came out of that thread and both must survive a refactor:

  1. `/dashboard/_stats` no longer emits `Workspaces` or `Projects` cards.
     Those counters read Magickit's own SQLite (`POST /v1/workspaces` /
     `POST /v1/projects`) and sat directly above the Chatroom panel, which
     lists the loop's real projects out of Conclair. Measured on
     sg-ai-server-01 on 2026-08-31: `Workspaces: 13 / Projects: 1` sitting
     above eight real projects. Two counters labelled "Projects" on the
     same screen was the confusion the whole thread turned on, and the
     Tier-C decision (msg-206 in that thread) was to drop them rather
     than label them: a footnote admits the metric is misleading but
     does not stop it from being misleading. The four task counters
     (Total / Completed / Running / Failed) stay because they mirror the
     Task Queue panel below and share its provenance.

  2. Each panel names its own provenance -- one line under the card
     heading saying where its rows come from. This is what stops the
     word "project" from meaning two different things on the same page.
     A future card added without one would silently re-create the class
     of bug the whole thread was about, so the four panels that have
     one today are pinned by name.

Neither test is a taxonomy check on all `<section class="card">` elements
-- the `data-scope` template scan was dropped as YAGNI. The Chatroom
panel and the four fragments have specific reasons to name their sources,
and those specific reasons are what the tests encode.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from magickit.main import create_app
from tests.route_table import sole_route

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "magickit" / "templates"


def _request(**state):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


class _StateManager:
    def __init__(self, **returns):
        self._returns = returns

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            return self._returns.get(name)

        return call


@pytest.mark.asyncio
async def test_stats_fragment_drops_the_two_conflicting_labels():
    """`Workspaces` and `Projects` are the two counters that read
    Magickit-internal tables and sat under a heading that meant something
    else in the panel just below. They must not come back through the
    _stats fragment -- if a future refactor restores them, this test
    fails and the T-dashboard-panels-do-not-name-the-project decision is
    surfaced instead of quietly reversed.
    """
    app = create_app()
    stats = {
        "total_workspaces": 13,
        "total_projects": 1,
        "total_tasks": 8,
        "tasks_by_status": {"completed": 0, "running": 0, "failed": 0},
    }
    request = _request(state_manager=_StateManager(get_dashboard_stats=stats))

    response = await sole_route(app, "GET", "/dashboard/_stats").endpoint(request)
    body = response.body.decode()

    assert ">Workspaces<" not in body, (
        "the Workspaces stat card is back -- it counts rows in Magickit's "
        "own `workspaces` table, which is a different population from the "
        "Chatroom summary below; see T-dashboard-panels-do-not-name-the-project"
    )
    assert ">Projects<" not in body, (
        "the Projects stat card is back -- it counts rows in Magickit's "
        "own `projects` table (POST /v1/projects), which is a different "
        "population from the Chatroom summary below; see "
        "T-dashboard-panels-do-not-name-the-project"
    )


@pytest.mark.asyncio
async def test_stats_fragment_keeps_the_four_task_counters():
    """The four task counters share provenance with the Task Queue panel
    below them and are honest under it. They stay -- the Tier-C decision
    dropped only the two counters whose labels collided with Chatroom.
    """
    app = create_app()
    stats = {
        "total_workspaces": 13,
        "total_projects": 1,
        "total_tasks": 8,
        "tasks_by_status": {"completed": 2, "running": 1, "failed": 5},
    }
    request = _request(state_manager=_StateManager(get_dashboard_stats=stats))

    response = await sole_route(app, "GET", "/dashboard/_stats").endpoint(request)
    body = response.body.decode()

    for label in ("Total Tasks", "Completed", "Running", "Failed"):
        assert f">{label}<" in body, f"the {label!r} stat card is missing"


def test_each_data_panel_declares_where_its_rows_come_from():
    """One line per panel, saying whose data it shows.

    The specific strings are the point: it is the naming, not merely the
    presence, that resolves the "which project?" ambiguity. A different
    caption on the Chatroom panel could restart the same confusion --
    e.g. if it stopped naming Conclair -- and the point of this test is
    to make that a red build rather than a slow re-discovery. Each
    substring is the shortest one that uniquely identifies its panel's
    provenance line.
    """
    source = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")

    expected = {
        "Chatroom": "Conclair から",
        "Recent Events": "上の Task Queue の状態遷移ログ",
        "Active Locks": "resource → project の写像は定義されていない",
        "Task Queue": "自律ループのタスクは Prismind 側",
    }

    missing = [name for name, marker in expected.items() if marker not in source]
    assert not missing, (
        "dashboard.html no longer names the provenance of these panels: "
        f"{missing}. Each panel that shows rows must say where they come "
        "from -- see T-dashboard-panels-do-not-name-the-project."
    )
