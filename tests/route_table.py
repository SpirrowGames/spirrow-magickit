"""Enumerate an app's routes without depending on how FastAPI stores them.

`app.routes` used to be a flat list: `include_router` copied each route in,
so a test could scan it for a (method, path) and read the endpoint off the
match. FastAPI 0.141 / Starlette 1.x stopped flattening -- an included
router now sits in `app.routes` as a single `_IncludedRouter` holding its
own routes, and dispatch descends into it.

Nothing about the app changed: the requests still route to the same
handlers. Only the shape a test sees changed, and tests that read
`app.routes` directly went from passing to reporting routes as missing.
That is worth a helper rather than a fix in each test, because the
failure mode is a false negative -- "no POST route registered for X" reads
like the route was dropped, not like the test cannot see it.

`pyproject.toml` floors its dependencies without capping them, so CI
resolves newer versions than the pinned service venv: both shapes are live
at once and both have to work here.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Iterator


def _leaf_routes(routes: Iterable[object]) -> Iterator[object]:
    """Yield real routes, descending through included routers.

    Only `_IncludedRouter` is unwrapped, via its `original_router`. A Mount
    (the /static files app) is left alone -- it is an endpoint in its own
    right, not a container of them.
    """
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _leaf_routes(included.routes)
        else:
            yield route


def route_table(app) -> dict[tuple[str, str], list[str]]:
    """Map (method, path) -> the module.function of every handler on it.

    A list, not a single value: a path with two handlers is the bug
    `test_no_path_is_registered_twice` exists to catch, so the shape has to
    be able to represent it.
    """
    table: dict[tuple[str, str], list[str]] = defaultdict(list)

    for route in _leaf_routes(app.routes):
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None)
        if not path or endpoint is None:
            continue
        name = (
            f"{getattr(endpoint, '__module__', '?')}."
            f"{getattr(endpoint, '__name__', '?')}"
        )
        for method in getattr(route, "methods", None) or ():
            table[(method, path)].append(name)

    return dict(table)


def sole_handler(app, method: str, path: str) -> str:
    """The one handler on (method, path). Fails if there are none or several.

    "Several" was the original bug -- two handlers answering 200 while only
    the first is reachable -- so this refuses to pick a winner instead of
    quietly returning one. Which of them Starlette would have chosen is not
    a question worth encoding: the answer depends on registration order and
    on the version's matching rules, and the invariant the tests actually
    want is that the question never comes up.
    """
    handlers = route_table(app).get((method, path), [])

    assert handlers, f"no route registered for {method} {path}"
    assert len(handlers) == 1, (
        f"{method} {path} has more than one handler, so only one of them is "
        f"reachable: {handlers}"
    )
    return handlers[0]
