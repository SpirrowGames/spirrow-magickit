"""Close carve-out (4) ``settled_disposition`` (mindwire T-done-disposition-auto-close).

Design v2.4 msg-8185 (naysayer APPROVE msg-8186), slice 1b-ii msg-8218 §3.
Conclair's half (row-lock re-check of the same head) is spirrow-conclair#34.

The weight is on refusals. The carve-out lets a non-owner close a thread that
cannot be reopened, so every condition is pinned by a case that breaks only
that condition, and the wrapper tests check that a refusal never reaches
Conclair and never falls back to the owner check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp import settled_disposition as sd
from magickit.mcp.settled_disposition import (
    SETTLED_DISPOSITION_ROLE,
    assess_settled_disposition,
    parse_timestamp,
)
from magickit.mcp.tools import chatroom as chatroom_tools

GATE_TAG = "gate:naysayer"
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)
OLD = "2026-11-07T11:00:00.000000Z"  # 25h before NOW
CLOSER = "SettledCloser"
OWNER = "Fermi"
PROJECT = "spirrow-mindwire"
THREAD = "T-settled"


def _msg(msg_id: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "msg_id": msg_id,
        "author": "Bohr",
        "type": "report",
        "content": "STOP: done",
        "timestamp": OLD,
        "role": SETTLED_DISPOSITION_ROLE,
        "next_participant": None,
        "disposition": {"kind": "done"},
        "tags": [],
    }
    base.update(kw)
    return base


def _thread(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "thread_id": THREAD,
        "title": "a design thread",
        "owner": OWNER,
        "status": "awaiting_reply",
        "tags": ["design"],
        "last_msg_id": "msg-3",
    }
    base.update(kw)
    return base


def _messages(head: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [
        _msg("msg-1", type="propose", disposition=None, role="proposer"),
        _msg("msg-2", author="Heisenberg", role="implementer", disposition=None),
        head if head is not None else _msg("msg-3"),
    ]


def _assess(
    *,
    thread: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    head_msg_id: str = "msg-3",
    is_ledger_thread: bool = False,
    now: datetime = NOW,
) -> sd.SettledVerdict:
    return assess_settled_disposition(
        thread=thread if thread is not None else _thread(),
        messages=messages if messages is not None else _messages(),
        head_msg_id=head_msg_id,
        is_ledger_thread=is_ledger_thread,
        now=now,
        grace=GRACE,
    )


# --------------------------------------------------------------------------- #
# the pure predicate
# --------------------------------------------------------------------------- #


def test_a_proposer_done_older_than_the_grace_is_closable() -> None:
    verdict = _assess()
    assert verdict.closable is True
    assert verdict.head_msg_id == "msg-3"
    assert verdict.owner == OWNER


def test_the_role_matches_conclairs() -> None:
    """Conclair checks role == "proposer" (spirrow-conclair#34). A drift would
    only ever turn into a 422, but the two are meant to be the same value."""
    assert SETTLED_DISPOSITION_ROLE == "proposer"


def test_exactly_the_grace_is_enough() -> None:
    head = _msg("msg-3", timestamp=(NOW - GRACE).isoformat())
    assert _assess(messages=_messages(head)).closable is True


def test_one_second_short_of_the_grace_is_refused() -> None:
    head = _msg("msg-3", timestamp=(NOW - GRACE + timedelta(seconds=1)).isoformat())
    verdict = _assess(messages=_messages(head))
    assert (verdict.closable, verdict.reason) == (False, sd.REASON_GRACE_PENDING)


def test_a_head_from_the_future_is_refused_not_trusted() -> None:
    head = _msg("msg-3", timestamp=(NOW + timedelta(hours=1)).isoformat())
    assert _assess(messages=_messages(head)).reason == sd.REASON_GRACE_PENDING


@pytest.mark.parametrize(
    "raw", [None, "", "yesterday", "2026-11-07T11:00:00", 1730000000, "2026-13-40T00:00:00Z"]
)
def test_an_unreadable_or_naive_timestamp_is_refused(raw: Any) -> None:
    """A naive timestamp's age is unknowable; guessing UTC would be the one
    wrong guess here that silently passes."""
    head = _msg("msg-3", timestamp=raw)
    assert _assess(messages=_messages(head)).reason == sd.REASON_TIMESTAMP_UNREADABLE


def test_parse_timestamp_reads_conclairs_format() -> None:
    parsed = parse_timestamp("2026-11-08T10:05:02.359825Z")
    assert parsed == datetime(2026, 11, 8, 10, 5, 2, 359825, tzinfo=UTC)
    assert parse_timestamp("2026-11-08T19:05:02+09:00") == datetime(
        2026, 11, 8, 10, 5, 2, tzinfo=UTC
    )


def test_a_reply_after_the_done_moves_the_head() -> None:
    """Einstein msg-8186 (ii): the grace is judged on the message the caller
    named, and that message must still be the head."""
    messages = [*_messages(), _msg("msg-4", author="Einstein", role="naysayer", disposition=None)]
    verdict = _assess(thread=_thread(last_msg_id="msg-4"), messages=messages)
    assert (verdict.closable, verdict.reason) == (False, sd.REASON_HEAD_MOVED)


def test_naming_an_older_message_is_head_moved() -> None:
    assert _assess(head_msg_id="msg-2").reason == sd.REASON_HEAD_MOVED


@pytest.mark.parametrize("rollup", [None, "msg-2", "msg-9", 3])
def test_a_read_whose_rollup_disagrees_is_refused(rollup: Any) -> None:
    """Which message is the head must be knowable from the read itself."""
    verdict = _assess(thread=_thread(last_msg_id=rollup))
    assert verdict.reason == sd.REASON_HEAD_UNKNOWN


@pytest.mark.parametrize("messages", [[], None, ["not a dict"]])
def test_a_read_without_messages_is_refused(messages: Any) -> None:
    verdict = assess_settled_disposition(
        thread=_thread(),
        messages=messages,
        head_msg_id="msg-3",
        is_ledger_thread=False,
        now=NOW,
        grace=GRACE,
    )
    assert verdict.reason == sd.REASON_HEAD_UNKNOWN


@pytest.mark.parametrize("head_msg_id", ["", "   "])
def test_an_empty_claim_is_refused(head_msg_id: str) -> None:
    assert _assess(head_msg_id=head_msg_id).reason == sd.REASON_HEAD_UNKNOWN


@pytest.mark.parametrize("status", ["resolved", "superseded", "parked", None, "open?"])
def test_only_an_open_thread_qualifies(status: Any) -> None:
    assert _assess(thread=_thread(status=status)).reason == sd.REASON_NOT_OPEN


@pytest.mark.parametrize("status", ["active", "awaiting_reply"])
def test_both_open_statuses_qualify(status: str) -> None:
    assert _assess(thread=_thread(status=status)).closable is True


def test_a_pr_gate_ledger_thread_belongs_to_carve_out_3() -> None:
    assert _assess(is_ledger_thread=True).reason == sd.REASON_PR_GATE_LEDGER


@pytest.mark.parametrize(
    "disposition",
    [
        None,
        "done",
        {},
        {"kind": "Done"},
        {"kind": "blocked_on", "trigger": {"arm": "pr", "ref": "a/b#1"}, "wake": "Bohr"},
        # D-8 (3) owner_unresolved rewrite: stored as blocked_on, never done.
        {
            "kind": "blocked_on",
            "trigger": {"arm": "human", "ref": THREAD},
            "wake": "human",
            "fallback": {"reason": "owner_unresolved", "owner": "x", "declared": "done"},
        },
    ],
)
def test_only_a_stored_done_settles(disposition: Any) -> None:
    """Structured field only. The body below says STOP: done in every case and
    is never read."""
    head = _msg("msg-3", disposition=disposition, content="STOP: done")
    assert _assess(messages=_messages(head)).reason == sd.REASON_NOT_DONE


def test_done_with_a_successor_is_a_contradiction() -> None:
    head = _msg("msg-3", next_participant="Heisenberg")
    assert _assess(messages=_messages(head)).reason == sd.REASON_NAMES_SUCCESSOR


@pytest.mark.parametrize("role", ["naysayer", "implementer", "human", "integrator", None, ""])
def test_only_a_proposer_done_settles(role: Any) -> None:
    """msg-8185 Obj-5-1: naysayer has no close power under ADR-10; implementer
    must not close the design it implemented; human done is the human's own
    force-close; relay posts carry no role."""
    head = _msg("msg-3", role=role)
    assert _assess(messages=_messages(head)).reason == sd.REASON_ROLE_NOT_ELIGIBLE


@pytest.mark.parametrize("owner", [None, "", "  "])
def test_an_ownerless_thread_is_refused(owner: Any) -> None:
    assert _assess(thread=_thread(owner=owner)).reason == sd.REASON_OWNER_MISSING


def test_the_sanction_and_note_carry_the_head() -> None:
    assert sd.settled_close_sanction("msg-3") == {
        "kind": "settled_disposition",
        "head_msg_id": "msg-3",
    }
    note = sd.format_settled_close_note(
        author=CLOSER, owner=OWNER, head_msg_id="msg-3", grace=GRACE
    )
    assert "[settled-disposition-close]" in note
    assert "head=msg-3" in note and f"owner={OWNER}" in note and f"author={CLOSER}" in note
    assert "msg-3" in sd.settled_close_reason("msg-3", GRACE)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #


def test_the_carve_out_ships_closed() -> None:
    settings = Settings()
    assert settings.settled_close_closers == []
    assert settings.settled_close_grace_seconds == 24 * 60 * 60


def test_yaml_names_closers_and_grace(tmp_path: Any) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "settled_close:\n  closers: [SettledCloser]\n  grace_seconds: 3600\n",
        encoding="utf-8",
    )
    settings = Settings.from_yaml(path)
    assert settings.settled_close_closers == ["SettledCloser"]
    assert settings.settled_close_grace_seconds == 3600


def test_yaml_without_the_block_keeps_it_closed(tmp_path: Any) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("mcp:\n  port: 8114\n", encoding="utf-8")
    assert Settings.from_yaml(path).settled_close_closers == []


# --------------------------------------------------------------------------- #
# wrapper: what chatroom_close_thread does with it
# --------------------------------------------------------------------------- #


def _settings(*, closers: list[str] | None = None, gate_enabled: bool = True) -> Settings:
    return Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        naysayer_gate_enabled=gate_enabled,
        naysayer_gate_tag=GATE_TAG,
        naysayer_identities=["Einstein"],
        settled_close_closers=[CLOSER] if closers is None else closers,
        settled_close_grace_seconds=int(GRACE.total_seconds()),
    )


def _capture_tools(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*_args: Any, **_kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    chatroom_tools.register_tools(mock_mcp, settings)
    return registered


def _adapter(
    *, thread: dict[str, Any] | None = None, messages: list[dict[str, Any]] | None = None
) -> MagicMock:
    adapter = MagicMock()
    adapter.get_thread = AsyncMock(
        return_value={
            "thread": thread if thread is not None else _thread(),
            "messages": messages if messages is not None else _messages(),
            "mode": "full",
        }
    )
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    adapter.close = AsyncMock()
    return adapter


_CLOSEABLE = {"success": True, "found": True, "message": "ok"}


def _identity(roles: list[str]) -> dict[str, Any]:
    return {**_CLOSEABLE, "identity": {"allowed_roles": roles}}


def _prismind(registry: dict[str, Any] | None = None) -> MagicMock:
    """Per-name answers. A name absent from the registry is "not registered"."""
    table: dict[str, Any] = {
        CLOSER: _identity(["integrator"]),
        OWNER: _identity(["proposer"]),
        "Heisenberg": _identity(["implementer"]),
    }
    if registry:
        table.update(registry)

    async def get_identity(*, identity_name: str) -> Any:
        answer = table.get(identity_name)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return {"success": True, "found": False, "identity": None, "message": "none"}
        return answer

    prismind = MagicMock()
    prismind.get_identity = AsyncMock(side_effect=get_identity)
    return prismind


async def _close(
    *,
    settings: Settings | None = None,
    adapter: MagicMock | None = None,
    prismind: MagicMock | None = None,
    author: str = CLOSER,
    head: str = "msg-3",
    now: datetime = NOW,
    fetch: AsyncMock | None = None,
    **extra: Any,
) -> tuple[dict[str, Any], MagicMock, AsyncMock]:
    tools = _capture_tools(settings or _settings())
    adapter = adapter or _adapter()
    fetch = fetch or AsyncMock()
    kwargs: dict[str, Any] = {
        "project": PROJECT,
        "thread_id": THREAD,
        "summary_content": "settled",
        "author": author,
        "settled_head_msg_id": head,
    }
    if author != "human":
        kwargs["embodiment"] = "terminal_coding_agent"
    kwargs.update(extra)
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind or _prismind()),
        patch.object(chatroom_tools, "fetch_ledger_verdict", fetch),
        patch.object(chatroom_tools, "_now", return_value=now),
    ):
        result = await tools["chatroom_close_thread"](**kwargs)
    return result, adapter, fetch


def _reason(result: dict[str, Any]) -> str:
    assert result.get("error_type") == "SettledDispositionNotClosableError", result
    return result["details"]["reason"]


@pytest.mark.asyncio
async def test_a_settled_thread_is_closed_with_the_sanction_and_a_note() -> None:
    result, adapter, _ = await _close()

    assert "error_type" not in result
    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["owner_override"] is True
    assert kwargs["close_sanction"] == {"kind": "settled_disposition", "head_msg_id": "msg-3"}
    assert kwargs["owner_override_reason"] == sd.settled_close_reason("msg-3", GRACE)
    assert "[settled-disposition-close]" in kwargs["summary_content"]
    assert "head=msg-3" in kwargs["summary_content"]
    assert kwargs["author"] == CLOSER


@pytest.mark.asyncio
async def test_without_the_argument_nothing_changes() -> None:
    """The carve-out is opt-in: a plain non-owner close is still answered by
    Conclair's owner check, with no override and no owner lookup."""
    prismind = _prismind()
    result, adapter, _ = await _close(head="", prismind=prismind)

    assert "error_type" not in result  # the mock Conclair accepts anything
    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["owner_override"] is False
    assert kwargs["close_sanction"] is None
    looked_up = [c.kwargs["identity_name"] for c in prismind.get_identity.await_args_list]
    assert OWNER not in looked_up


@pytest.mark.asyncio
async def test_with_no_closer_configured_everyone_is_refused_before_any_read() -> None:
    """The shipped default. Unregistered authors pass the closeable_roles stage
    for legacy reasons, so "nobody registered" alone would not have closed it."""
    adapter = _adapter()
    result, adapter, _ = await _close(settings=_settings(closers=[]), adapter=adapter)

    assert _reason(result) == sd.REASON_CLOSER_NOT_CONFIGURED
    adapter.get_thread.assert_not_awaited()
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_configured_but_unregistered_closer_is_refused() -> None:
    prismind = _prismind({CLOSER: None})
    result, adapter, _ = await _close(prismind=prismind)
    assert _reason(result) == sd.REASON_CLOSER_UNREGISTERED
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_closer_lookup_outage_refuses() -> None:
    """The closeable_roles stage reads the same record first, so an outage
    there already refuses with its own envelope; either way nothing closes."""
    prismind = _prismind({CLOSER: RuntimeError("prismind down")})
    result, adapter, _ = await _close(prismind=prismind)
    assert "error_type" in result
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_human_asking_for_it_is_refused() -> None:
    result, adapter, _ = await _close(author="human", owner_override_reason="x")
    assert _reason(result) == sd.REASON_HUMAN_CALLER
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_reply_after_the_judged_head_refuses_the_close() -> None:
    messages = [*_messages(), _msg("msg-4", author="Einstein", role="naysayer", disposition=None)]
    adapter = _adapter(thread=_thread(last_msg_id="msg-4"), messages=messages)
    result, adapter, _ = await _close(adapter=adapter)

    assert _reason(result) == sd.REASON_HEAD_MOVED
    assert result["details"]["head_msg_id"] == "msg-3"
    assert result["details"]["last_msg_id"] == "msg-4"
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_inside_the_grace_is_refused() -> None:
    result, adapter, _ = await _close(now=NOW - timedelta(hours=2))
    assert _reason(result) == sd.REASON_GRACE_PENDING
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_configured_grace_is_used() -> None:
    settings = _settings()
    settings.settled_close_grace_seconds = 26 * 60 * 60  # longer than the 25h age
    result, _, _ = await _close(settings=settings)
    assert _reason(result) == sd.REASON_GRACE_PENDING


@pytest.mark.parametrize(
    ("registry", "owner", "reason"),
    [
        ({}, "human", sd.REASON_OWNER_HUMAN),
        ({}, "Human", sd.REASON_OWNER_HUMAN),
        ({"Takahito": _identity(["human"])}, "Takahito", sd.REASON_OWNER_HUMAN),
        ({}, "orchestrator", sd.REASON_OWNER_UNRESOLVED),
        ({OWNER: RuntimeError("prismind down")}, OWNER, sd.REASON_OWNER_LOOKUP_UNAVAILABLE),
        ({OWNER: {"success": True}}, OWNER, sd.REASON_OWNER_LOOKUP_UNAVAILABLE),
    ],
)
@pytest.mark.asyncio
async def test_only_a_registered_non_human_owner_qualifies(
    registry: dict[str, Any], owner: str, reason: str
) -> None:
    """msg-8172 §3: a human-owned thread is never closed by the machine; an owner
    the registry cannot place is not provably non-human."""
    adapter = _adapter(thread=_thread(owner=owner))
    result, adapter, _ = await _close(adapter=adapter, prismind=_prismind(registry))
    assert _reason(result) == reason
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_owner_itself_is_told_to_close_normally() -> None:
    adapter = _adapter(thread=_thread(owner=CLOSER))
    result, adapter, _ = await _close(adapter=adapter)
    assert _reason(result) == sd.REASON_OWNER_IS_AUTHOR
    adapter.close_thread.assert_not_awaited()


@pytest.mark.parametrize("gate_enabled", [True, False])
@pytest.mark.asyncio
async def test_an_unreadable_thread_refuses_whatever_the_gate_setting(gate_enabled: bool) -> None:
    """The legacy path proceeds on an unreadable thread when the gate is off;
    a request for this carve-out must not."""
    adapter = _adapter()
    adapter.get_thread = AsyncMock(return_value={"error_type": "ChatroomUnavailable"})
    result, adapter, _ = await _close(
        settings=_settings(gate_enabled=gate_enabled), adapter=adapter
    )
    assert _reason(result) == sd.REASON_THREAD_UNREADABLE
    assert result["details"]["upstream_error_type"] == "ChatroomUnavailable"
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_pr_gate_ledger_thread_is_refused_without_a_github_lookup() -> None:
    adapter = _adapter(
        thread=_thread(
            owner="orchestrator",
            tags=["pr-review", "naysayer"],
            title="PR review — SpirrowGames/spirrow-mindwire#143",
        )
    )
    result, adapter, fetch = await _close(adapter=adapter)
    assert _reason(result) == sd.REASON_PR_GATE_LEDGER
    fetch.assert_not_awaited()
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_naysayer_gate_runs_first() -> None:
    """Owner bypass is never a gate bypass (ADR-2026-06-04-19 D-5)."""
    adapter = _adapter(thread=_thread(tags=["design", GATE_TAG]))
    result, adapter, _ = await _close(adapter=adapter)
    assert result["error_type"] == "NaysayerReviewRequiredError"
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_proposer_done_after_the_approve_makes_the_review_stale() -> None:
    """Recorded consequence, not an accident: the done is itself a substantive
    proposer post, so on a gated thread it always post-dates the review and the
    gate refuses. Gated threads are never closed by this carve-out; they stay
    with the gate and the human."""
    approve = _msg(
        "msg-2",
        author="Einstein",
        role="naysayer",
        disposition=None,
        content="VERDICT: APPROVE",
    )
    messages = [_msg("msg-1", type="propose", disposition=None), approve, _msg("msg-3")]
    adapter = _adapter(thread=_thread(tags=["design", GATE_TAG]), messages=messages)
    result, adapter, _ = await _close(adapter=adapter)
    assert result["error_type"] == "NaysayerReviewStaleError"
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_gated_thread_with_the_gate_disabled_is_refused() -> None:
    """With the gate off nothing checked an APPROVE; the carve-out must not be
    the reason a gated thread closes unreviewed."""
    adapter = _adapter(thread=_thread(tags=["design", GATE_TAG]))
    result, adapter, _ = await _close(settings=_settings(gate_enabled=False), adapter=adapter)
    assert _reason(result) == sd.REASON_GATE_DISABLED
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ineligible_head_is_reported_by_code() -> None:
    head = _msg("msg-3", role="naysayer")
    result, adapter, _ = await _close(adapter=_adapter(messages=_messages(head)))
    assert _reason(result) == sd.REASON_ROLE_NOT_ELIGIBLE
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_closeable_roles_still_gates_the_closer() -> None:
    """Layered on the existing stages: a closer that may not close at all is
    refused before the carve-out is consulted."""
    prismind = _prismind({CLOSER: _identity(["naysayer"])})
    adapter = _adapter()
    result, adapter, _ = await _close(prismind=prismind, adapter=adapter)
    assert result["error_type"] == "RoleNotAllowedToClose"
    adapter.get_thread.assert_not_awaited()
