"""Jinja2 environment for Magickit's own HTML views.

``main.py`` builds a ``Jinja2Templates`` inside ``create_app`` and keeps it
in a module global. A router in this package cannot read that without
importing ``main``, which imports the routers -- so the environment is
built here instead and both sides point at the same directory.

The filters are the ones a status page needs and a template should not
reimplement: "how long ago was this" and "what does this timestamp say
exactly". They accept the shapes an HTTP payload actually carries -- ISO
strings, naive datetimes, ``None`` -- because the alternative is a page
that 500s on a field the backend happened to leave out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def parse_ts(value: Any) -> datetime | None:
    """Coerce a wire timestamp to an aware UTC datetime, or ``None``.

    Conclair serialises with a trailing ``Z``, which ``fromisoformat``
    rejects before Python 3.11's relaxation and which is easy to lose
    track of once a value has been round-tripped through a dict. A naive
    datetime is read as UTC: every producer here writes UTC, and guessing
    local time would make an "8 hours ago" out of a fresh heartbeat.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    """Seconds since ``value``. ``None`` when it cannot be read."""
    parsed = parse_ts(value)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return (reference - parsed).total_seconds()


def humanize_age(seconds: float | None) -> str:
    """Render an age the way the page talks about it.

    Clock time is not what a reader wants here -- "止まっているか" is a
    question about elapsed time, and making the reader subtract two
    timestamps is exactly the work this page exists to remove.
    """
    if seconds is None:
        return "不明"
    if seconds < 0:
        # A clock skew between this host and the writer. Saying "-3分前"
        # would read as a bug in the data rather than in the clocks.
        return "たった今"
    if seconds < 60:
        return "たった今"

    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}分前"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}時間{minutes}分前" if minutes else f"{hours}時間前"

    days, hours = divmod(hours, 24)
    return f"{days}日{hours}時間前" if hours else f"{days}日前"


def _ago_filter(value: Any) -> str:
    return humanize_age(age_seconds(value))


def _iso_filter(value: Any) -> str:
    parsed = parse_ts(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%SZ") if parsed else "—"


templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["ago"] = _ago_filter
templates.env.filters["iso"] = _iso_filter
