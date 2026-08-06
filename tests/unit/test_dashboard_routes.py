"""Route-resolution tests for the dashboard's HTMX fragments.

A path registered twice is not an error in Starlette -- it matches the
first registration and the second becomes dead code. That is how the
dashboard came to render a raw JSON dump where its stat cards belong:
routes_v2's JSON `/dashboard/stats` is included before main.py's HTML
handler of the same path, so the panel got JSON, with a 200, for as long
as nobody looked at the page on purpose.

These tests check *which handler a path resolves to*, which is what the
bug was actually about, and they need no database -- running the app's
lifespan here would open the real SQLite file (the YAML config overrides
MAGICKIT_DB_PATH), and the running service holds a lock on it.
"""

from __future__ import annotations

from pathlib import Path

from magickit.main import create_app
from tests.route_table import route_table, sole_handler

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "magickit" / "templates"


def test_no_path_is_registered_twice():
    """Two handlers on one (method, path) means one of them is unreachable.

    The general form of the bug. It costs nothing to check and it fails
    loudly, which is the opposite of how this behaved: both handlers
    answer 200, so the only symptom was a page rendering the wrong thing.
    """
    duplicates = {
        key: handlers
        for key, handlers in route_table(create_app()).items()
        if len(handlers) > 1
    }

    assert not duplicates, (
        "these paths have more than one handler; only the first is reachable: "
        f"{duplicates}"
    )


def test_stats_fragment_resolves_to_the_html_handler():
    assert sole_handler(create_app(), "GET", "/dashboard/_stats") == (
        "magickit.main.dashboard_stats_html"
    )


def test_json_stats_api_keeps_its_path():
    """The typed, authenticated API keeps `/dashboard/stats` -- it is the
    contract, and it has a test of its own. The fragment moved, not it."""
    assert sole_handler(create_app(), "GET", "/dashboard/stats") == (
        "magickit.api.routes_v2.get_dashboard_stats"
    )


def test_dashboard_template_points_at_a_route_that_exists():
    """A rename that misses the template leaves the panel loading forever,
    and a 404 in a polled HTMX target is silent on the page."""
    source = (TEMPLATES / "dashboard.html").read_text()

    assert 'hx-get="/dashboard/_stats"' in source

    assert ("GET", "/dashboard/_stats") in route_table(create_app())
