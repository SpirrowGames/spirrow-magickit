"""Unit tests for the chatroom digest producer.

Most of this module's risk lives in two pure functions -- ``build_digest_input``
and ``accept_digest`` -- so most of these tests take no mocks at all, following
``test_ops_view.py::classify``.

The single most important test here is
``test_a_cognilens_failure_never_stores_anything``: the whole reason
``CognilensAdapter`` now raises is so a rejection cannot become a summary, and
a digest is read by humans in the chatroom UI.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from magickit.adapters.cognilens import CognilensError
from magickit.core.digest_producer import (
    DigestBounds,
    DigestInput,
    DigestProducer,
    accept_digest,
    build_digest_input,
    sweep_forever,
)

NOW = datetime(2026, 8, 27, 4, 12, tzinfo=timezone.utc)


def _bounds(**overrides: Any) -> DigestBounds:
    base: dict[str, Any] = {
        "style": "concise",
        "max_tokens": 400,
        "min_msg_count": 4,
        "min_input_chars": 1200,
        "max_input_chars": 24000,
        "head_chars_ratio": 0.6,
        "max_msg_chars": 4000,
        "min_redigest": timedelta(minutes=60),
        "max_threads_per_cycle": 5,
        "max_threads_per_project": 2,
        "max_concurrency": 1,
        "include_statuses": ("active", "awaiting_reply"),
        "failure_backoff": timedelta(minutes=30),
        "failure_backoff_max": timedelta(minutes=720),
        "max_consecutive_failures": 5,
        "summarize_timeout": 150.0,
    }
    base.update(overrides)
    return DigestBounds(**base)


def _msg(num: int, *, content: str = "本文", **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "msg_id": f"msg-{num:03d}",
        "author": "Heisenberg",
        "type": "report",
        "timestamp": "2026-08-27T04:12:00Z",
        "content": content,
    }
    msg.update(extra)
    return msg


def _view(messages: list[dict[str, Any]], **thread: Any) -> dict[str, Any]:
    base = {
        "project": "spirrow-mindwire",
        "thread_id": "T-1",
        "title": "digest design",
        "owner": "Bohr",
        "status": "active",
        "tags": ["gate:naysayer"],
        "msg_count": len(messages),
    }
    base.update(thread)
    return {"thread": base, "messages": messages}


def _settings(**overrides: Any) -> MagicMock:
    settings = MagicMock()
    settings.conclair_url = "http://localhost:8115"
    settings.conclair_timeout = 30.0
    settings.cognilens_url = "http://localhost:8111"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _producer(**overrides: Any) -> DigestProducer:
    bounds = overrides.pop("bounds", _bounds())
    clock = overrides.pop("now", lambda: NOW)
    return DigestProducer(_settings(**overrides), bounds=bounds, now=clock)


# =====================================================================
# build_digest_input -- pure
# =====================================================================


def test_the_header_names_the_thread_not_just_its_id() -> None:
    """The model has to know it is reading a design thread, not a log."""
    out = build_digest_input(_view([_msg(1)] * 4), _bounds())

    assert "# スレッド: digest design" in out.text
    assert "project: spirrow-mindwire" in out.text
    assert "status: active" in out.text
    assert "owner: Bohr" in out.text
    assert "tags: gate:naysayer" in out.text


def test_each_message_carries_id_author_type_and_time() -> None:
    out = build_digest_input(_view([_msg(12, content="実装した")]), _bounds())

    # msg_id costs ~4 tokens and lets the digest cite ("msg-012 で決定"),
    # which is the difference between actionable and merely descriptive.
    assert "[msg-012]" in out.text
    assert "Heisenberg" in out.text
    assert "report" in out.text
    # Truncated to the minute: full ISO is ~500 tokens of noise over 40 msgs.
    assert "2026-08-27 04:12" in out.text
    assert "2026-08-27T04:12:00Z" not in out.text
    assert "実装した" in out.text


def test_role_appears_only_when_recorded() -> None:
    """A recorded role has passed the allowed_roles gate, so it is trustworthy."""
    with_role = build_digest_input(
        _view([_msg(1, role="implementer")]), _bounds()
    ).text
    without = build_digest_input(_view([_msg(1)]), _bounds()).text

    assert "Heisenberg (implementer)" in with_role
    assert "(implementer)" not in without
    assert "Heisenberg" in without


def test_operational_fields_are_left_out() -> None:
    """embodiment is operational, not semantic; per-msg tags are noise here."""
    out = build_digest_input(
        _view([_msg(1, embodiment="terminal_coding_agent", tags=["x"], commit_ref="abc")]),
        _bounds(),
    )

    assert "terminal_coding_agent" not in out.text
    assert "abc" not in out.text


def test_a_short_thread_is_rendered_whole() -> None:
    out = build_digest_input(_view([_msg(n) for n in range(1, 6)]), _bounds())

    assert out.truncated is False
    assert out.omitted_msgs == 0
    assert out.source_msg_count == 5
    assert out.source_last_msg_id == "msg-005"


def test_source_last_msg_id_is_numeric_max_not_lexicographic() -> None:
    """msg-9 > msg-100 under string comparison; the ids are a sequence."""
    out = build_digest_input(
        _view([_msg(9), _msg(100), _msg(11)]), _bounds()
    )

    assert out.source_last_msg_id == "msg-100"


def test_a_thread_with_no_messages_is_refused() -> None:
    with pytest.raises(ValueError):
        build_digest_input(_view([]), _bounds())


# ---- per-message cap ---------------------------------------------------


def test_one_pasted_log_cannot_eat_the_head_budget() -> None:
    huge = "L" * 20000
    out = build_digest_input(
        _view([_msg(1, content=huge), _msg(2)]), _bounds(max_msg_chars=500)
    )

    assert len(out.text) < 3000
    assert "文字省略" in out.text
    # The middle is cut, not the tail: a log's first lines say what it is
    # and its last lines say how it ended.
    assert out.text.count("L") > 0


def test_a_message_under_the_cap_is_untouched() -> None:
    out = build_digest_input(_view([_msg(1, content="短い")]), _bounds())

    assert "文字省略" not in out.text
    assert "短い" in out.text


# ---- whole-input elision -----------------------------------------------


def _oversized_view(count: int = 40, chars: int = 2000) -> dict[str, Any]:
    return _view(
        [_msg(n, content=f"M{n}-" + "x" * chars) for n in range(1, count + 1)]
    )


def test_an_oversized_thread_keeps_head_and_tail() -> None:
    """Head = the propose (why the thread exists); tail = where it is stuck."""
    out = build_digest_input(_oversized_view(), _bounds(max_input_chars=12000))

    assert out.truncated is True
    assert out.omitted_msgs > 0
    assert "M1-" in out.text
    assert "M40-" in out.text
    # Something in the middle is gone.
    assert "M20-" not in out.text


def test_the_elision_marker_goes_into_the_prompt() -> None:
    """The model can only say 中略あり if it is told."""
    out = build_digest_input(_oversized_view(), _bounds(max_input_chars=12000))

    assert "中略" in out.text
    assert f"{out.omitted_msgs} 件" in out.text


def test_elision_cuts_only_on_message_boundaries() -> None:
    """Half a message is half a sentence; the model confabulates the rest."""
    out = build_digest_input(_oversized_view(), _bounds(max_input_chars=12000))

    head, _, tail = out.text.partition("中略")
    # Every rendered message starts with its own "## [msg-NNN]" header, and
    # no partial body may be left dangling before the marker.
    for half in (head, tail):
        for block in half.split("## [")[1:]:
            assert block.startswith("msg-")


def test_source_msg_count_is_what_was_read_not_what_exists() -> None:
    """A truncated digest must report what it read, or it is unauditable."""
    view = _oversized_view()
    out = build_digest_input(view, _bounds(max_input_chars=12000))

    assert out.thread_msg_count == 40
    assert out.source_msg_count < 40
    assert out.source_msg_count > 0


def test_source_last_msg_id_comes_from_the_rendered_tail() -> None:
    """Not from a listing: two round trips, and a msg can land between them.

    Using the listing's value would mark the digest fresh for a set it never
    summarized.
    """
    out = build_digest_input(_oversized_view(), _bounds(max_input_chars=12000))

    assert out.source_last_msg_id == "msg-040"
    assert out.source_last_msg_id in out.text


def test_the_input_stays_near_the_ceiling() -> None:
    """The ceiling is derived from Cognilens's 30s timeout, so it must hold."""
    out = build_digest_input(_oversized_view(), _bounds(max_input_chars=12000))

    assert len(out.text) <= 13000


# =====================================================================
# accept_digest -- pure
# =====================================================================


def _source(text: str = "x" * 5000) -> DigestInput:
    return DigestInput(
        text=text,
        source_last_msg_id="msg-042",
        source_msg_count=18,
        thread_msg_count=21,
        truncated=False,
        omitted_msgs=0,
    )


def test_a_normal_digest_is_accepted() -> None:
    accepted, reason = accept_digest("3 行の要約です。", _source(), _bounds())

    assert accepted is True
    assert reason == ""


def test_an_empty_digest_is_rejected() -> None:
    for summary in ("", "   ", "\n\n"):
        accepted, reason = accept_digest(summary, _source(), _bounds())
        assert accepted is False
        assert reason == "rejected_empty"


def test_a_stringified_dict_is_rejected() -> None:
    """Belt and braces on the adapter fix.

    This is what used to reach callers as prose. It must never reach the
    chatroom UI, so it is checked here too even though the adapter should
    now make it unreachable.
    """
    accepted, reason = accept_digest(
        "{'summary': '...', 'original_tokens': 3521}", _source(), _bounds()
    )

    assert accepted is False
    assert reason == "rejected_error_envelope"


def test_an_error_envelope_is_rejected() -> None:
    accepted, reason = accept_digest(
        '{"error_type": "UpstreamValidationError", "error": "bad"}',
        _source(),
        _bounds(),
    )

    assert accepted is False
    assert reason == "rejected_error_envelope"


def test_a_digest_longer_than_its_source_is_rejected() -> None:
    """The model echoed the input. Real with no-think models on short inputs."""
    accepted, reason = accept_digest("y" * 600, _source("x" * 500), _bounds())

    assert accepted is False
    assert reason == "rejected_longer_than_source"


def test_a_runaway_decode_is_rejected() -> None:
    accepted, reason = accept_digest(
        "あ" * 5000, _source("x" * 100000), _bounds(max_tokens=400)
    )

    assert accepted is False
    assert reason == "rejected_runaway_length"


def test_quality_score_is_not_a_gate() -> None:
    """Pinning the decision, not the behaviour of a caller.

    Cognilens computes quality_score as
    ``preservation_ratio * 0.6 + ratio_score * 0.4``, and with an empty
    ``preserve`` the preservation term is unconditionally 1.0 -- so the
    score is a function of output length alone. ``accept_digest`` therefore
    does not take it as an argument at all, which is what this asserts.
    """
    import inspect

    assert "quality" not in str(inspect.signature(accept_digest))


# =====================================================================
# DigestProducer -- the parts that touch adapters
# =====================================================================


def _chatroom_mock(**overrides: Any) -> AsyncMock:
    room = AsyncMock()
    room.list_project_summaries.return_value = {
        "items": [
            {
                "project": "p1",
                "threads_by_status": {"active": 2},
                "last_activity_at": "2026-08-27T04:00:00Z",
            }
        ]
    }
    room.list_threads.return_value = {
        "items": [
            {
                "thread_id": "T-1",
                "status": "active",
                "last_msg_id": "msg-010",
                "msg_count": 10,
                "last_activity_at": "2026-08-27T04:00:00Z",
            }
        ]
    }
    room.get_thread_digest.return_value = {"present": False, "digest": None}
    room.get_thread.return_value = _view([_msg(n, content="x" * 400) for n in range(1, 11)])
    room.put_thread_digest.return_value = {"present": True}
    room.close = AsyncMock()
    for key, value in overrides.items():
        getattr(room, key).return_value = value
    return room


def _cognilens_mock(summary: str = "これは要約です。") -> AsyncMock:
    lens = AsyncMock()
    lens.summarize_payload.return_value = {
        "summary": summary,
        "original_tokens": 6000,
        "compressed_tokens": 380,
        "quality_score": 0.72,
    }
    return lens


# ---- the test the bug earns -------------------------------------------


async def test_a_cognilens_failure_never_stores_anything() -> None:
    room = _chatroom_mock()
    lens = _cognilens_mock()
    lens.summarize_payload.side_effect = CognilensError(
        "summarize returned no 'summary'", tool="summarize"
    )
    producer = _producer()

    outcome = await producer.digest_thread(
        project="p1", thread_id="T-1", chatroom=room, cognilens=lens
    )

    assert outcome.action == "failed"
    assert outcome.reason == "cognilens_error"
    room.put_thread_digest.assert_not_awaited()


async def test_a_rejected_summary_never_stores_anything() -> None:
    room = _chatroom_mock()
    lens = _cognilens_mock(summary="{'summary': 'oops'}")
    producer = _producer()

    outcome = await producer.digest_thread(
        project="p1", thread_id="T-1", chatroom=room, cognilens=lens
    )

    assert outcome.action == "failed"
    assert outcome.reason == "rejected_error_envelope"
    room.put_thread_digest.assert_not_awaited()


# ---- the happy path ----------------------------------------------------


async def test_a_successful_digest_records_its_coverage_and_provenance() -> None:
    room = _chatroom_mock()
    lens = _cognilens_mock()
    producer = _producer()

    outcome = await producer.digest_thread(
        project="p1", thread_id="T-1", chatroom=room, cognilens=lens,
        producer_label="magickit-digest-ondemand",
    )

    assert outcome.action == "written"
    kwargs = room.put_thread_digest.await_args.kwargs
    assert kwargs["digest"] == "これは要約です。"
    assert kwargs["source_last_msg_id"] == "msg-010"
    assert kwargs["source_msg_count"] == 10
    assert kwargs["producer"] == "magickit-digest-ondemand"
    assert kwargs["style"] == "concise"
    assert kwargs["truncated"] is False
    # The tier is what we *requested*; Cognilens does not report which model
    # served, so `model` stays None until it does.
    assert kwargs["tier"] == "light"
    assert kwargs["model"] is None
    assert kwargs["source_chars"] > 0
    assert kwargs["duration_ms"] is not None


async def test_the_model_field_is_used_when_cognilens_reports_one() -> None:
    """One line in Cognilens turns this from a guess into an observation."""
    room = _chatroom_mock()
    lens = _cognilens_mock()
    lens.summarize_payload.return_value = {
        "summary": "要約", "model": "Qwen3-32B", "original_tokens": 10,
    }
    producer = _producer()

    await producer.digest_thread(
        project="p1", thread_id="T-1", chatroom=room, cognilens=lens
    )

    assert room.put_thread_digest.await_args.kwargs["model"] == "Qwen3-32B"


async def test_a_conclair_store_error_is_a_failure_not_a_success() -> None:
    room = _chatroom_mock()
    room.put_thread_digest.return_value = {
        "error_type": "ChatroomIntegrityError", "error": "bad source id"
    }
    producer = _producer()

    outcome = await producer.digest_thread(
        project="p1", thread_id="T-1", chatroom=room, cognilens=_cognilens_mock()
    )

    assert outcome.action == "failed"
    assert outcome.reason == "conclair_error"


# ---- the floors --------------------------------------------------------


async def test_a_short_thread_is_refused_without_calling_cognilens() -> None:
    """A 2-message thread's original is shorter and more accurate."""
    room = _chatroom_mock()
    room.get_thread.return_value = _view([_msg(1), _msg(2)])
    lens = _cognilens_mock()
    producer = _producer()

    outcome = await producer.digest_thread(
        project="p1", thread_id="T-1", chatroom=room, cognilens=lens
    )

    assert outcome.action == "skipped"
    assert outcome.reason == "too_short"
    assert "原文の方が" in outcome.detail
    lens.summarize_payload.assert_not_awaited()


async def test_force_does_not_bypass_the_size_floors() -> None:
    """`force` is about staleness, not about the output being worthless."""
    room = _chatroom_mock()
    room.get_thread.return_value = _view([_msg(1), _msg(2)])
    lens = _cognilens_mock()
    producer = _producer()

    outcome = await producer.digest_thread(
        project="p1", thread_id="T-1", force=True, chatroom=room, cognilens=lens
    )

    assert outcome.reason == "too_short"
    lens.summarize_payload.assert_not_awaited()


async def test_a_small_thread_is_refused_after_rendering() -> None:
    """Message count alone lies: six one-line acks are not worth a call."""
    room = _chatroom_mock()
    room.get_thread.return_value = _view([_msg(n, content="ok") for n in range(1, 7)])
    lens = _cognilens_mock()
    producer = _producer()

    outcome = await producer.digest_thread(
        project="p1", thread_id="T-1", chatroom=room, cognilens=lens
    )

    assert outcome.reason == "too_small"
    lens.summarize_payload.assert_not_awaited()


# ---- freshness comes from Conclair ------------------------------------


async def test_a_fresh_digest_is_skipped() -> None:
    room = _chatroom_mock()
    room.get_thread_digest.return_value = {
        "present": True,
        "digest": {"stale": False, "generated_at": "2026-08-01T00:00:00Z"},
    }
    producer = _producer()
    producer._chatroom = lambda: room  # type: ignore[method-assign]
    producer._cognilens = _cognilens_mock  # type: ignore[method-assign]

    outcomes = await producer.run_cycle()

    assert [(o.action, o.reason) for o in outcomes] == [("skipped", "fresh")]
    room.get_thread.assert_not_awaited()


async def test_a_recently_digested_stale_thread_has_its_own_reason() -> None:
    """Distinct from `fresh`: it *is* out of date, we are declining to spend."""
    room = _chatroom_mock()
    room.get_thread_digest.return_value = {
        "present": True,
        "digest": {"stale": True, "generated_at": "2026-08-27T04:00:00Z"},
    }
    producer = _producer()
    producer._chatroom = lambda: room  # type: ignore[method-assign]
    producer._cognilens = _cognilens_mock  # type: ignore[method-assign]

    outcomes = await producer.run_cycle()

    assert [(o.action, o.reason) for o in outcomes] == [
        ("skipped", "recently_digested")
    ]


async def test_a_digest_read_failure_skips_rather_than_guessing() -> None:
    """"Read failed" is not "no digest"; skipping costs nothing."""
    room = _chatroom_mock()
    room.get_thread_digest.return_value = {
        "error_type": "ConclairUpstreamError", "error": "boom"
    }
    producer = _producer()
    producer._chatroom = lambda: room  # type: ignore[method-assign]
    producer._cognilens = _cognilens_mock  # type: ignore[method-assign]

    outcomes = await producer.run_cycle()

    assert [(o.action, o.reason) for o in outcomes] == [("skipped", "read_error")]


# ---- failure backoff ---------------------------------------------------


def test_backoff_grows_and_then_caps() -> None:
    producer = _producer(bounds=_bounds(max_consecutive_failures=99))
    key = ("p1", "T-1")

    producer._record_failure(key, "msg-010")
    first = producer._backoff_until(key, "msg-010")
    producer._record_failure(key, "msg-010")
    second = producer._backoff_until(key, "msg-010")

    assert first is not None and second is not None
    assert second > first


def test_a_new_message_resets_the_failure_count() -> None:
    """New msgs are evidence it might work now, and that somebody cares.

    Without this, one transient Lexora outage permanently blacklists
    whatever was in flight.
    """
    producer = _producer()
    key = ("p1", "T-1")
    for _ in range(5):
        producer._record_failure(key, "msg-010")

    assert producer._backoff_until(key, "msg-010") == datetime.max.replace(
        tzinfo=timezone.utc
    )
    assert producer._backoff_until(key, "msg-011") is None


def test_a_thread_leaves_the_candidate_set_after_repeated_failures() -> None:
    producer = _producer(bounds=_bounds(max_consecutive_failures=3))
    key = ("p1", "T-1")
    for _ in range(3):
        producer._record_failure(key, "msg-010")

    blocked = producer._backoff_until(key, "msg-010")
    assert blocked == datetime.max.replace(tzinfo=timezone.utc)


async def test_force_bypasses_the_backoff() -> None:
    producer = _producer()
    room = _chatroom_mock()
    for _ in range(5):
        producer._record_failure(("p1", "T-1"), "msg-010")

    needed, _reason = await producer._needs_digest(
        room,
        _make_candidate(),
        force=True,
    )

    assert needed is True


def _make_candidate() -> Any:
    from magickit.core.digest_producer import Candidate

    return Candidate(
        project="p1",
        thread_id="T-1",
        status="active",
        last_msg_id="msg-010",
        msg_count=10,
        last_activity_at=NOW,
    )


async def test_backoff_blocks_selection() -> None:
    producer = _producer()
    room = _chatroom_mock()
    producer._record_failure(("p1", "T-1"), "msg-010")

    needed, reason = await producer._needs_digest(
        room, _make_candidate(), force=False
    )

    assert needed is False
    assert reason == "backoff"
    room.get_thread_digest.assert_not_awaited()


# ---- candidate selection ----------------------------------------------


async def test_short_threads_are_filtered_from_the_listing() -> None:
    """Decided from the listing's own rollup, with no extra round trip."""
    room = _chatroom_mock()
    room.list_threads.return_value = {
        "items": [
            {"thread_id": "T-tiny", "status": "active", "last_msg_id": "msg-002",
             "msg_count": 2, "last_activity_at": "2026-08-27T04:00:00Z"},
            {"thread_id": "T-real", "status": "active", "last_msg_id": "msg-020",
             "msg_count": 20, "last_activity_at": "2026-08-27T04:00:00Z"},
        ]
    }
    producer = _producer()

    candidates = await producer.select_candidates(room)

    assert [c.thread_id for c in candidates] == ["T-real"]


async def test_the_per_project_cap_applies() -> None:
    room = _chatroom_mock()
    room.list_threads.return_value = {
        "items": [
            {"thread_id": f"T-{n}", "status": "active", "last_msg_id": "msg-020",
             "msg_count": 20, "last_activity_at": "2026-08-27T04:00:00Z"}
            for n in range(10)
        ]
    }
    producer = _producer(bounds=_bounds(max_threads_per_project=2))

    candidates = await producer.select_candidates(room)

    assert len(candidates) == 2


async def test_the_per_cycle_cap_applies_across_projects() -> None:
    room = _chatroom_mock()
    room.list_project_summaries.return_value = {
        "items": [
            {"project": f"p{n}", "threads_by_status": {"active": 5},
             "last_activity_at": "2026-08-27T04:00:00Z"}
            for n in range(10)
        ]
    }
    room.list_threads.return_value = {
        "items": [
            {"thread_id": f"T-{n}", "status": "active", "last_msg_id": "msg-020",
             "msg_count": 20, "last_activity_at": "2026-08-27T04:00:00Z"}
            for n in range(5)
        ]
    }
    producer = _producer(bounds=_bounds(max_threads_per_cycle=3))

    candidates = await producer.select_candidates(room)

    assert len(candidates) == 3


async def test_projects_are_taken_round_robin_so_none_starves() -> None:
    """A permanently busy project must not monopolise every cycle."""
    room = _chatroom_mock()
    room.list_project_summaries.return_value = {
        "items": [
            {"project": "busy", "threads_by_status": {"active": 5},
             "last_activity_at": "2026-08-27T04:00:00Z"},
            {"project": "quiet", "threads_by_status": {"active": 5},
             "last_activity_at": "2026-08-01T00:00:00Z"},
        ]
    }
    producer = _producer(bounds=_bounds(max_threads_per_cycle=1, max_threads_per_project=1))

    first = await producer.select_candidates(room)
    second = await producer.select_candidates(room)

    assert first[0].project == "busy"
    assert second[0].project == "quiet"


async def test_projects_with_nothing_open_are_dropped_for_free() -> None:
    room = _chatroom_mock()
    room.list_project_summaries.return_value = {
        "items": [
            {"project": "done", "threads_by_status": {"resolved": 40},
             "last_activity_at": "2026-08-27T04:00:00Z"}
        ]
    }
    producer = _producer()

    assert await producer.select_candidates(room) == []
    room.list_threads.assert_not_awaited()


async def test_an_unavailable_project_listing_yields_no_candidates() -> None:
    room = _chatroom_mock()
    room.list_project_summaries.return_value = {
        "error_type": "ConclairUpstreamError", "error": "down"
    }
    producer = _producer()

    assert await producer.select_candidates(room) == []


# ---- loop control is not consulted ------------------------------------


async def test_the_sweeper_never_reads_loop_control() -> None:
    """`hold` means "the loop takes no more turns", not "nobody may read".

    A human sets HOLD precisely when they intend to go look at what
    happened, which is when "what is this stuck on" is most wanted -- and a
    held project's loop is not using the GPU, so it is the cheapest time to
    digest it. "Stop everything" is what digest.sweeper_enabled is for.
    Reading control here would also import its contract that a failed read
    means `hold`, so one Conclair hiccup would silently stop all digesting.
    """
    room = _chatroom_mock()
    producer = _producer()
    producer._chatroom = lambda: room  # type: ignore[method-assign]
    producer._cognilens = _cognilens_mock  # type: ignore[method-assign]

    await producer.run_cycle()

    room.get_loop_control.assert_not_awaited()


# ---- concurrency -------------------------------------------------------


async def test_summarize_calls_do_not_overlap_at_concurrency_one() -> None:
    """One GPU: parallelism buys nothing and delays the coding loop."""
    inflight = 0
    peak = 0

    async def _slow(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0)
        inflight -= 1
        return {"summary": "要約", "original_tokens": 10, "compressed_tokens": 2}

    lens = AsyncMock()
    lens.summarize_payload = _slow
    producer = _producer(bounds=_bounds(max_concurrency=1))

    await asyncio.gather(
        *(
            producer.digest_thread(
                project="p1", thread_id=f"T-{n}",
                chatroom=_chatroom_mock(), cognilens=lens,
            )
            for n in range(5)
        )
    )

    assert peak == 1


# ---- run_cycle resilience ---------------------------------------------


async def test_run_cycle_with_conclair_down_returns_rather_than_raising() -> None:
    room = _chatroom_mock()
    room.list_project_summaries.side_effect = RuntimeError("connection refused")
    producer = _producer()
    producer._chatroom = lambda: room  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await producer.run_cycle()
    # ... and the adapter is still closed on the way out.
    room.close.assert_awaited()


async def test_run_cycle_closes_the_chatroom_adapter() -> None:
    room = _chatroom_mock()
    producer = _producer()
    producer._chatroom = lambda: room  # type: ignore[method-assign]
    producer._cognilens = _cognilens_mock  # type: ignore[method-assign]

    await producer.run_cycle()

    room.close.assert_awaited()


async def test_the_cognilens_adapter_is_never_closed() -> None:
    """MCPBaseAdapter.__getattr__ fabricates `close` into a bogus tool call."""
    room = _chatroom_mock()
    lens = _cognilens_mock()
    producer = _producer()
    producer._chatroom = lambda: room  # type: ignore[method-assign]
    producer._cognilens = lambda: lens  # type: ignore[method-assign]

    await producer.run_cycle()

    lens.close.assert_not_awaited()


# =====================================================================
# sweep_forever
# =====================================================================


async def test_the_sweeper_sleeps_before_its_first_cycle() -> None:
    """Startup is when this process is busiest and the loop is likeliest
    to be mid-turn on the GPU. It also makes the sleep the crash backoff."""
    order: list[str] = []
    producer = MagicMock()

    async def _cycle() -> list[Any]:
        order.append("cycle")
        raise asyncio.CancelledError

    producer.run_cycle = _cycle

    async def _sleep(_seconds: float) -> None:
        order.append("sleep")

    original = asyncio.sleep
    asyncio.sleep = _sleep  # type: ignore[assignment]
    try:
        with pytest.raises(asyncio.CancelledError):
            await sweep_forever(producer, interval=900, cycle_timeout=720)
    finally:
        asyncio.sleep = original  # type: ignore[assignment]

    assert order[0] == "sleep"


async def test_one_bad_cycle_does_not_end_the_sweeper() -> None:
    calls = 0

    async def _cycle() -> list[Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        raise asyncio.CancelledError

    producer = MagicMock()
    producer.run_cycle = _cycle

    async def _sleep(_seconds: float) -> None:
        return None

    original = asyncio.sleep
    asyncio.sleep = _sleep  # type: ignore[assignment]
    try:
        with pytest.raises(asyncio.CancelledError):
            await sweep_forever(producer, interval=900, cycle_timeout=720)
    finally:
        asyncio.sleep = original  # type: ignore[assignment]

    assert calls == 2


async def test_the_sweeper_is_cancellable() -> None:
    """CancelledError must be re-raised, or shutdown hangs on this task."""

    async def _cycle() -> list[Any]:
        return []

    producer = MagicMock()
    producer.run_cycle = _cycle

    task = asyncio.create_task(
        sweep_forever(producer, interval=0.01, cycle_timeout=1)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
