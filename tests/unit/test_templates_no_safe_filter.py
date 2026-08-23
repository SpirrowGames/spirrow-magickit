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

# Jinja comment blocks -- ``{# ... #}`` -- may span multiple lines. Strip
# them before searching so a template can *explain* why ``|safe`` is
# banned without tripping its own guard. Jinja itself skips comment
# content at parse time; this regex just brings the linter into agreement
# with that behavior (otherwise we'd have to write "|" + "safe" splits in
# any documentation, which is worse than the rule it protects).
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)


def _templates() -> list[Path]:
    return sorted(TEMPLATES_DIR.rglob("*.html"))


def _strip_jinja_comments(text: str) -> str:
    """Return ``text`` with all ``{# ... #}`` blocks replaced by blank
    lines (line numbers preserved so error reports still point at the
    original line the hit was on)."""
    def _blank_out(match: re.Match[str]) -> str:
        # Preserve newlines so reported line numbers still refer to the
        # correct source line -- otherwise a multi-line comment above the
        # offending line would shift every downstream line number.
        return "\n" * match.group(0).count("\n")
    return JINJA_COMMENT.sub(_blank_out, text)


def test_templates_are_found() -> None:
    """Guard the guard: a moved template dir must not silently pass."""
    assert _templates(), f"no templates under {TEMPLATES_DIR}"


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_safe_filter_used(template: Path) -> None:
    text = _strip_jinja_comments(template.read_text(encoding="utf-8"))
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
    ):
        assert not SAFE_FILTER.search(line), f"regex false-positive: {line!r}"


def test_the_check_ignores_safe_filter_inside_jinja_comments() -> None:
    """★ A template must be free to document ``|safe`` in its own
    comments (that is where the "why not" belongs). Jinja ignores
    ``{# ... #}`` content at parse time; the linter must agree.

    Regression fix for the initial CI-red on this PR: the very template
    that codifies the rule contained the string ``|safe`` inside a
    ``{# ... #}`` comment and the naive line-by-line search tripped."""
    text = (
        "<p>{{ x }}</p>\n"
        "{# don't use |safe on user text -- see spec §4-2 #}\n"
        "<p>{{ y }}</p>\n"
    )
    stripped = _strip_jinja_comments(text)
    hits = [ln for ln in stripped.splitlines() if SAFE_FILTER.search(ln)]
    assert hits == [], f"comment stripping failed: {stripped!r}"

    # And an actual filter in the SAME text still trips (guard the guard).
    with_real_hit = text + "<p>{{ z|safe }}</p>\n"
    stripped2 = _strip_jinja_comments(with_real_hit)
    hits2 = [ln for ln in stripped2.splitlines() if SAFE_FILTER.search(ln)]
    assert len(hits2) == 1


def test_multiline_jinja_comment_stripping_preserves_line_numbers() -> None:
    """A multi-line ``{# ... #}`` block is blanked without shifting the
    line numbers of code below it, so a real hit's ``line N`` message
    still points at the correct source line."""
    text = (
        "<p>{{ a }}</p>\n"          # line 1
        "{# multi\n"                 # line 2
        "line comment\n"             # line 3
        "with |safe inside #}\n"     # line 4
        "<p>{{ b|safe }}</p>\n"      # line 5 -- the real hit
    )
    stripped = _strip_jinja_comments(text)
    lines = stripped.splitlines()
    assert len(lines) == 5
    # Only line 5 (the real hit) matches; the comment lines are blank.
    hit_lines = [n for n, ln in enumerate(lines, 1) if SAFE_FILTER.search(ln)]
    assert hit_lines == [5]
