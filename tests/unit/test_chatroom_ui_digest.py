"""Unit tests for the on-demand digest route.

Two shapes matter here. First, a refusal must be a *flash*, not a 404: a
button that renders and then 404s is the loop-control 405 trap CLAUDE.md
records, where the failure reads as a bug in the page rather than a setting.
Second, a refusal must not have spent GPU time getting there -- the floors
exist because a two-message thread's original is shorter and more accurate
than any summary of it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from magickit.config import Settings
from magickit.core.digest_producer import DigestOutcome, DigestProducer
from magickit.main import create_app

PROJECT = "spirrow-mindwire"
THREAD = "T-1"
PATH = f"/ui/projects/{PROJECT}/threads/{THREAD}/digest"


def _app_with_producer(producer: Any, **settings_overrides: Any) -> Any:
    """Build the app and install a stand-in producer.

    The real one is created in the lifespan, which the ASGI transport below
    does not run -- and would need a live Conclair anyway.
    """
    app = create_app()
    app.state.digest_producer = producer
    app.state.digest_sweeper = None
    return app


async def _post(app: Any, data: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(PATH, data=data or {})


def _producer_returning(outcome: DigestOutcome) -> MagicMock:
    producer = MagicMock(spec=DigestProducer)
    producer.digest_thread = AsyncMock(return_value=outcome)
    return producer


# --- success ------------------------------------------------------------


async def test_a_written_digest_reports_its_coverage(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(
            PROJECT, THREAD, "written", "ok", source_last_msg_id="msg-042"
        )
    )

    response = await _post(_app_with_producer(producer))

    assert response.status_code == 200
    assert "要約を生成しました" in response.text
    assert "msg-042" in response.text
    # Not `messagePosted`: a digest is not a post, and Conclair's page may
    # bind the two differently.
    assert response.headers["hx-trigger"] == "digestGenerated"


async def test_a_truncated_digest_says_so(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(
            PROJECT, THREAD, "written", "ok",
            source_last_msg_id="msg-042", truncated=True,
        )
    )

    response = await _post(_app_with_producer(producer))

    assert "中略あり" in response.text


async def test_force_is_always_set_on_this_path(monkeypatch: Any) -> None:
    """A human pressing the button is new information.

    It bypasses staleness and the failure backoff -- not the size floors.
    """
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(PROJECT, THREAD, "written", "ok", source_last_msg_id="msg-1")
    )

    await _post(_app_with_producer(producer))

    kwargs = producer.digest_thread.await_args.kwargs
    assert kwargs["force"] is True
    assert kwargs["producer_label"] == "magickit-digest-ondemand"
    assert kwargs["project"] == PROJECT
    assert kwargs["thread_id"] == THREAD


async def test_a_style_override_is_passed_through(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(PROJECT, THREAD, "written", "ok", source_last_msg_id="msg-1")
    )

    await _post(_app_with_producer(producer), {"style": "bullet"})

    assert producer.digest_thread.await_args.kwargs["style"] == "bullet"


async def test_an_empty_style_field_means_the_default(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(PROJECT, THREAD, "written", "ok", source_last_msg_id="msg-1")
    )

    await _post(_app_with_producer(producer), {"style": ""})

    assert producer.digest_thread.await_args.kwargs["style"] is None


# --- refusals -----------------------------------------------------------


async def test_a_short_thread_is_refused_with_an_explanation(
    monkeypatch: Any,
) -> None:
    """The refusal teaches the rule rather than just declining."""
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(
            PROJECT, THREAD, "skipped", "too_short",
            detail="2 件のスレッドは、要約より原文の方が短く正確です (下限 4 件)",
        )
    )

    response = await _post(_app_with_producer(producer))

    assert response.status_code == 200
    assert "要約しませんでした" in response.text
    assert "原文の方が" in response.text
    # An error-styled flash even though nothing broke: conclair.js
    # auto-dismisses .alert-success after 6 seconds, and a refusal the
    # reader misses is a refusal they will retry.
    assert "alert-error" in response.text


async def test_disabled_answers_with_a_flash_not_a_404(monkeypatch: Any) -> None:
    """A rendered button that 404s reads as a bug in the page, not a setting."""
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings",
        lambda: Settings(digest_on_demand_enabled=False),
    )
    producer = _producer_returning(
        DigestOutcome(PROJECT, THREAD, "written", "ok", source_last_msg_id="msg-1")
    )

    response = await _post(_app_with_producer(producer))

    assert response.status_code == 200
    assert "DigestDisabled" in response.text
    assert "on_demand_enabled" in response.text
    producer.digest_thread.assert_not_awaited()


async def test_a_missing_producer_is_reported_not_crashed(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    app = create_app()
    app.state.digest_producer = None

    response = await _post(app)

    assert response.status_code == 200
    assert "DigestUnavailable" in response.text


# --- failures -----------------------------------------------------------


async def test_a_failed_digest_names_its_reason(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(
            PROJECT, THREAD, "failed", "cognilens_error",
            detail="summarize returned no 'summary'",
        )
    )

    response = await _post(_app_with_producer(producer))

    assert response.status_code == 200
    assert "cognilens_error" in response.text
    assert "summarize returned no" in response.text


async def test_a_hanging_producer_is_bounded(monkeypatch: Any) -> None:
    """The person who pressed the button must not wait forever."""
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings",
        lambda: Settings(digest_on_demand_timeout_seconds=0.05),
    )

    async def _hang(**_kwargs: Any) -> DigestOutcome:
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled")

    producer = MagicMock(spec=DigestProducer)
    producer.digest_thread = _hang

    response = await _post(_app_with_producer(producer))

    assert response.status_code == 200
    assert "DigestTimeout" in response.text


async def test_an_unexpected_exception_does_not_500_the_page(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = MagicMock(spec=DigestProducer)
    producer.digest_thread = AsyncMock(side_effect=RuntimeError("boom"))

    response = await _post(_app_with_producer(producer))

    assert response.status_code == 200
    assert "RuntimeError" in response.text
    assert "boom" in response.text


# --- escaping -----------------------------------------------------------


async def test_the_flash_escapes_producer_output(monkeypatch: Any) -> None:
    """`detail` can carry model or upstream text; it must not reach raw."""
    monkeypatch.setattr(
        "magickit.web.chatroom_digest.get_settings", lambda: Settings()
    )
    producer = _producer_returning(
        DigestOutcome(
            PROJECT, THREAD, "failed", "conclair_error",
            detail="<script>alert(1)</script>",
        )
    )

    response = await _post(_app_with_producer(producer))

    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


# --- the sweeper is not started by importing the app --------------------


def test_building_the_app_does_not_start_a_sweeper() -> None:
    """`create_app` must not spawn it: the sweeper belongs to the lifespan.

    mcp_server.py runs as two systemd units, so a sweeper attached to app
    construction would end up with three copies on one GPU.
    """
    app = create_app()

    assert getattr(app.state, "digest_sweeper", None) is None


@pytest.mark.parametrize(
    "flag,expected",
    [(True, True), (False, False)],
)
def test_the_sweeper_flag_is_independent_of_the_button(
    flag: bool, expected: bool
) -> None:
    """Two flags, because they are two different risks."""
    settings = Settings(digest_sweeper_enabled=flag)

    assert settings.digest_sweeper_enabled is expected
    # The button's default does not follow the sweeper's.
    assert settings.digest_on_demand_enabled is True
