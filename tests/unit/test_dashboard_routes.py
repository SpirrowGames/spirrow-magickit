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

import re
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
    source = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")

    assert 'hx-get="/dashboard/_stats"' in source

    assert ("GET", "/dashboard/_stats") in route_table(create_app())


def _htmx_targets():
    """(template, method, path) for every hx-get / hx-post in the templates.

    `rglob`, not `glob`: the partials under templates/partials are the
    HTMX swap targets themselves and carry hx-* attributes of their own
    (the ops table's control buttons post from inside one). Scanning only
    the top level checked the pages and skipped exactly the files whose
    whole reason for existing is to be fetched.

    Query strings are dropped: they carry Jinja expressions, and the route
    table is keyed on path. Paths interpolating a Jinja expression are
    skipped -- `/dashboard/_ops/{{ row.project }}/control` is not a
    literal, and the route table is keyed on the declared path.
    """
    for template in sorted(TEMPLATES.rglob("*.html")):
        source = template.read_text(encoding="utf-8")
        for verb, url in re.findall(r'hx-(get|post)="([^"]+)"', source):
            path = url.split("?", 1)[0]
            if "{{" in path or "{%" in path:
                continue
            yield template.name, verb.upper(), path


def test_every_htmx_target_is_a_real_route():
    """A fragment slot pointed at a path nobody serves polls 404s forever.

    `/dashboard/task-stats` did exactly that: tasks.html asked for it every
    10 seconds, got a 404 every time, and the four stat pills sat at their
    placeholder dashes. Nothing about the page said so.

    This check had an exemption for the New Project form, which posted to
    /dashboard/projects and 405'd because no POST handler had ever existed.
    The form has since been removed rather than given one -- projects are
    created through `init_project`, which allocates the project_uid and
    registers with Prismind, and nothing on this page could produce a
    project the rest of the platform knows about. With the form gone the
    exemption is gone too, and this check is now unconditional.
    """
    table = route_table(create_app())

    missing = [
        f"{name}: {method} {path}"
        for name, method, path in _htmx_targets()
        if (method, path) not in table
    ]

    assert not missing, f"HTMX targets with no route: {missing}"


def test_every_scripted_htmx_target_has_a_route_under_it():
    """The scan above only sees hx-* attributes, and not every target is one.

    tasks.html builds one in JavaScript -- `htmx.ajax('GET',
    '/dashboard/_tasks/' + id, ...)` for the Details button -- and it
    pointed at `/dashboard/tasks/{id}`, which has never existed. The button
    opened the modal and left it on "Loading..." forever, because a 404 into
    an HTMX swap puts nothing on the page and says nothing.

    Only the literal prefix is checkable; the rest is concatenation. That is
    enough to catch a prefix that leads nowhere, which is the failure this
    had.
    """
    paths = {path for _, path in route_table(create_app())}

    dangling = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        source = template.read_text(encoding="utf-8")
        for method, prefix in re.findall(
            r"""htmx\.ajax\(\s*['"](\w+)['"]\s*,\s*['"]([^'"]+)['"]""", source
        ):
            if not any(p.startswith(prefix) for p in paths):
                dangling.append(f"{template.name}: {method} {prefix}...")

    assert not dangling, f"scripted HTMX targets with no route under them: {dangling}"


def test_no_htmx_target_is_a_whole_page():
    """The general form of the self-nesting bug.

    A fragment slot must not point at a route that renders a full page:
    HTMX swaps the response into the slot, so the page lands inside itself
    -- and since the copy brings its own `hx-trigger="load"` slot, it does
    it again. projects.html reached 250 nested copies and a 137,870px
    document; tasks.html reached 121 across its four slots.

    Both looked fine from the server: every request answered 200. Page
    handlers are the ones named `*_page`, which is the convention in
    main.py and the only thing distinguishing them from fragments once
    they are both just routes.
    """
    table = route_table(create_app())

    page_routes = {
        key
        for key, handlers in table.items()
        if any(fn.rsplit(".", 1)[1].endswith("_page") for fn in handlers)
    }
    assert page_routes, "no *_page handlers found -- has the convention changed?"

    nesting = [
        f"{name}: {method} {path}"
        for name, method, path in _htmx_targets()
        if (method, path) in page_routes
    ]

    assert not nesting, (
        "these HTMX targets return a whole page, which will be swapped into "
        f"a fragment slot inside that same page: {nesting}"
    )
