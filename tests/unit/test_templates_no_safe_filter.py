"""Every value a template outputs must go through Jinja autoescape.

**spec** ``spec/slices/S5-decision-materials.md`` §4-2 / §5.

Rationale (spec §4-2): the judgement page emits values a composer
generated ― question, gain/loss, recommendation_reason. Those strings
are indistinguishable from user input (they are, transitively). If any
template ever bypasses autoescape with ``|safe``, a ``<script>`` in
a composer field lands on the page as a real script.

Tier-C msg-118 §3 also nailed shut the other side: no ``html.unescape()``
at render either (a "double-escape" workaround would hide the real
injection point). So the whole shape is: **raw text in, autoescape at
render, nothing else in between**. The invariant this file pins is the
"nothing else" — statically.

The check is intentionally simple: search the template text for
``|safe`` (with optional whitespace around the pipe). Full Jinja
parsing would be overkill here — a false positive is a code comment
that literally says ``|safe`` (rare and easy to word around), and a
false negative would require someone constructing a filter name via
Jinja concatenation (which is not a thing).

**Do not add exceptions.** If a template genuinely needs pre-rendered
HTML (e.g. a Markdown render), the safe path is to render on the server
side into a controlled fragment and pass that as raw HTML *outside* of
Jinja (e.g. include a file, or emit the fragment via a route). But
before doing that, ask: is the "genuine need" actually just YAGNI?
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from magickit.main import TEMPLATES_DIR

# `|safe` with optional whitespace around the pipe. Deliberately lenient
# so ``{{ x |safe }}`` and ``{{ x|safe }}`` both trip.
SAFE_FILTER = re.compile(r"\|\s*safe\b")


def _templates() -> list[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def test_templates_are_found() -> None:
    """Guard the guard: a moved template dir must not silently pass."""
    assert _templates(), f"no templates under {TEMPLATES_DIR}"


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_safe_filter_used(template: Path) -> None:
    text = template.read_text(encoding="utf-8")
    hits = [
        (n, line.rstrip())
        for n, line in enumerate(text.splitlines(), start=1)
        if SAFE_FILTER.search(line)
    ]
    assert not hits, (
        f"{template.relative_to(TEMPLATES_DIR)} uses `|safe`, which bypasses "
        "autoescape:\n  "
        + "\n  ".join(f"line {n}: {ln}" for n, ln in hits)
        + "\nSee spec/slices/S5-decision-materials.md §4-2."
    )


def test_the_check_catches_a_safe_filter() -> None:
    """The regex, against the exact string the rule exists to reject."""
    for line in (
        "<p>{{ material.question|safe }}</p>",
        "<p>{{ material.question | safe }}</p>",
        "<p>{{ material.question |safe }}</p>",
    ):
        assert SAFE_FILTER.search(line), f"regex missed: {line!r}"


def test_the_check_ignores_the_word_safe_elsewhere() -> None:
    """A hit requires the pipe. ``value="safe"`` and ``class="safe"``
    are unrelated and must not fire."""
    for line in (
        '<a class="safe">safe</a>',
        '<input value="safe">',
        "{# `safe` is not the filter here #}",
    ):
        assert not SAFE_FILTER.search(line), f"regex false-positive: {line!r}"
