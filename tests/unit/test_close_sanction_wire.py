"""D-2: Magickit sends *which* bypass sanctioned a non-owner close, not just a bit.

``T-integrity-flags-sanctioned-force-close`` msg-219 §3 / msg-221 §2. Conclair
(shipped in its PR #17) records a ``close_sanction`` object on the audit event
so ``GET /integrity`` can tell a human Tier-C force-close and a PR-gate ledger
carve-out apart from a genuinely corrupt row. This is the caller side: Magickit
already holds the structured grounds at the moment it decides, and used to
project them onto one boolean and throw the rest away.

Why the shape assertions below are so exact
-------------------------------------------

Conclair validates ``close_sanction`` and answers 422 when it does not fit, and
a 422 on this path stops the loop -- every carve-out close fails. Magickit
cannot import Conclair to check, so the wire contract is pinned here as literal
shape. It was **measured**, not assumed: run against the real
``spirrow_conclair.schemas.message.CloseSanction`` at conclair ``7bde45f`` (the
head Takahito merged as ``b84640f``) under pydantic 2.13.3 --

===============================================  ========
payload                                          result
===============================================  ========
``pr_gate_ledger`` + ``approving_review_id`` int  REJECT (W-4a: wire is ``str``)
``pr_gate_ledger`` + all three as str             ACCEPT
``pr_gate_ledger`` + a missing/empty field        REJECT (W-4b)
``pr_gate_ledger`` + ``reason``                   REJECT (W-4c)
``human_override`` + ``reason``                   ACCEPT
``human_override`` without ``reason``             REJECT (W-4d)
``unspecified`` alone                             ACCEPT
===============================================  ========

W-4a/b/c are Bohr's (msg-327 §4). W-4d is the same class found here: the gated
force-close branch accepts an empty reason -- a fresh APPROVE has already
justified the close -- so a human_override with nothing in it is reachable from
a path the suite already pins (``test_owner_side_c_...``). It must claim
nothing rather than assert an empty reason.

Short evidence is never sent as a partial claim. It degrades to "no claim",
which Conclair records as ``kind="unspecified"`` and counts as
``unclassified_override`` -- visible, and not a refused close.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp.pr_gate_ledger import LedgerVerdict, ledger_close_sanction
from magickit.mcp.tools import chatroom as chatroom_tools

GATE_TAG = "gate:naysayer"
_TITLE = "PR review (develop→main) — SpirrowGames/spirrow-mindwire#143"

_CLOSABLE = LedgerVerdict(
    True,
    "merged and approved at head",
    pr_slug="SpirrowGames/spirrow-mindwire#143",
    merged_head="7828c7e",
    approving_review_id=4923581091,
)


# --------------------------------------------------------------------------- #
# The projection itself, as a pure function.
# --------------------------------------------------------------------------- #


def test_w4a_the_review_id_crosses_the_wire_as_a_string() -> None:
    """int is what GitHub returns and what the verdict holds; str is the wire."""
    sanction = ledger_close_sanction(_CLOSABLE)
    assert sanction == {
        "kind": "pr_gate_ledger",
        "pr": "SpirrowGames/spirrow-mindwire#143",
        "merged_head": "7828c7e",
        "approving_review_id": "4923581091",
    }
    # Not merely str()-able at the far end: pydantic v2 refuses int for a str
    # field, so the conversion has to happen here or every close 422s.
    assert isinstance(sanction["approving_review_id"], str)


def test_w4c_a_ledger_sanction_never_carries_a_reason() -> None:
    """The wire refuses `reason` on this kind, and the carve-out has one to hand."""
    assert "reason" not in (ledger_close_sanction(_CLOSABLE) or {})


@pytest.mark.parametrize(
    "field",
    ["pr_slug", "merged_head", "approving_review_id"],
)
def test_w4b_a_closable_verdict_missing_any_evidence_claims_nothing(field: str) -> None:
    """`closable=True` does not imply all three are present -- so check, don't assume.

    ``evaluate_ledger_verdict`` keeps ``approving_review_id`` only when GitHub
    reported an int, so the closable branch can reach the wire one field short.
    A partial ``pr_gate_ledger`` is a 422; None is a recorded ``unspecified``.
    """
    evidence: dict[str, Any] = {
        "pr_slug": _CLOSABLE.pr_slug,
        "merged_head": _CLOSABLE.merged_head,
        "approving_review_id": _CLOSABLE.approving_review_id,
    }
    evidence[field] = None
    verdict = LedgerVerdict(True, _CLOSABLE.reason, **evidence)
    assert ledger_close_sanction(verdict) is None


def test_a_refused_verdict_yields_no_sanction() -> None:
    assert ledger_close_sanction(LedgerVerdict(False, "not merged")) is None


# --------------------------------------------------------------------------- #
# End to end, through both families of close path.
# --------------------------------------------------------------------------- #


def _capture_tools(settings: Settings) -> dict[str, Any]:
    registered: dict[str, Any] = {}

    def fake_tool(*args: Any, **kwargs: Any):
        def decorator(fn):
            registered[fn.__name__] = fn
            return fn
        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = fake_tool
    chatroom_tools.register_tools(mock_mcp, settings)
    return registered


@pytest.fixture
def settings() -> Settings:
    return Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        naysayer_gate_enabled=True,
        naysayer_gate_tag=GATE_TAG,
        naysayer_identities=["Einstein"],
    )


def _adapter(
    *,
    owner: str,
    tags: list[str],
    title: str = _TITLE,
    messages: list[dict[str, Any]] | None = None,
) -> MagicMock:
    adapter = MagicMock()
    adapter.get_thread = AsyncMock(
        return_value={
            "thread": {
                "thread_id": "T-pr-review-143",
                "owner": owner,
                "tags": tags,
                "title": title,
            },
            "messages": messages or [],
            "mode": "full",
        }
    )
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
    adapter.post_message = AsyncMock(
        return_value={"msg": {}, "thread_status_changed_to": "resolved"}
    )
    adapter.close = AsyncMock()
    return adapter


def _prismind() -> MagicMock:
    adapter = MagicMock()
    adapter.get_identity = AsyncMock(
        return_value={
            "success": True,
            "found": True,
            "identity": {"allowed_roles": ["proposer", "implementer", "integrator"]},
            "message": "ok",
        }
    )
    return adapter


def _patched(adapter: MagicMock, verdict: LedgerVerdict | None = None):
    patches = [
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()),
    ]
    if verdict is not None:
        patches.append(
            patch.object(
                chatroom_tools, "fetch_ledger_verdict", AsyncMock(return_value=verdict)
            )
        )
    return patches


@pytest.mark.asyncio
async def test_a_carve_out_close_forwards_the_ledger_sanction(settings: Settings) -> None:
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review", "naysayer"])
    with patch.object(chatroom_tools, "_adapter", return_value=adapter), \
         patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()), \
         patch.object(
             chatroom_tools, "fetch_ledger_verdict", AsyncMock(return_value=_CLOSABLE)
         ):
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-143",
            summary_content="filed",
            author="Heisenberg",
            embodiment="terminal_coding_agent",
            role="implementer",
        )

    assert "error_type" not in result
    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["close_sanction"] == {
        "kind": "pr_gate_ledger",
        "pr": "SpirrowGames/spirrow-mindwire#143",
        "merged_head": "7828c7e",
        "approving_review_id": "4923581091",
    }
    # The prose still rides the sibling field. It is not evidence, and putting
    # it inside the sanction would be a 422.
    assert kwargs["owner_override_reason"] == "merged and approved at head"
    assert kwargs["owner_override"] is True


@pytest.mark.asyncio
async def test_a_human_force_close_forwards_its_reason_as_the_sanction(
    settings: Settings,
) -> None:
    tools = _capture_tools(settings)
    adapter = _adapter(owner="Bohr", tags=["design"])
    with patch.object(chatroom_tools, "_adapter", return_value=adapter), \
         patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()):
        await tools["chatroom_close_thread"](
            project="p",
            thread_id="T-pr-review-143",
            summary_content="force",
            author="human",
            owner_override_reason="stale thread; closing",
        )

    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["close_sanction"] == {
        "kind": "human_override",
        "reason": "stale thread; closing",
    }
    # human_override must not carry ledger evidence -- the wire refuses that too.
    assert set(kwargs["close_sanction"]) == {"kind", "reason"}


@pytest.mark.asyncio
async def test_w4d_a_gated_force_close_without_a_reason_claims_nothing(
    settings: Settings,
) -> None:
    """A fresh APPROVE lets a human force-close with no reason of their own.

    That path reaches the wire with an empty string. ``human_override`` requires
    a non-empty ``reason`` (and Conclair strips before validating), so claiming
    one here would 422 a close that succeeds today. It degrades to no claim.
    """
    tools = _capture_tools(settings)
    adapter = _adapter(
        owner="Bohr",
        tags=[GATE_TAG],
        messages=[
            {"msg_id": "msg-001", "author": "Bohr", "type": "propose",
             "content": "", "tags": []},
            {"msg_id": "msg-002", "author": "Einstein", "type": "report",
             "content": "", "tags": ["verdict:approve"]},
        ],
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter), \
         patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()):
        result = await tools["chatroom_close_thread"](
            project="p",
            thread_id="T-pr-review-143",
            summary_content="force",
            author="human",
        )

    assert "error_type" not in result
    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["owner_override"] is True  # the close still happens
    assert kwargs["close_sanction"] is None  # but nothing is asserted about why


@pytest.mark.asyncio
async def test_a_short_verdict_still_closes_and_degrades_to_no_claim(
    settings: Settings,
) -> None:
    """W-4b end to end: the loop keeps running, the shortfall is only countable."""
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review", "naysayer"])
    short = LedgerVerdict(
        True,
        "merged and approved at head",
        pr_slug="SpirrowGames/spirrow-mindwire#143",
        merged_head="7828c7e",
        approving_review_id=None,
    )
    with patch.object(chatroom_tools, "_adapter", return_value=adapter), \
         patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()), \
         patch.object(
             chatroom_tools, "fetch_ledger_verdict", AsyncMock(return_value=short)
         ):
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-143",
            summary_content="filed",
            author="Heisenberg",
            embodiment="terminal_coding_agent",
            role="implementer",
        )

    assert "error_type" not in result
    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["owner_override"] is True
    assert kwargs["close_sanction"] is None


@pytest.mark.asyncio
async def test_the_decide_route_forwards_the_sanction_too(settings: Settings) -> None:
    """Both close routes carry it, or the audit learns to trust only one of them.

    ``closes_thread`` on ``post_message`` is the second entrance; Conclair's
    recorder sits at the choke point they share, so a caller that feeds only one
    of them leaves the other landing as ``unclassified_override`` forever.
    """
    tools = _capture_tools(settings)
    adapter = _adapter(owner="Bohr", tags=["design"])
    with patch.object(chatroom_tools, "_adapter", return_value=adapter), \
         patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()):
        await tools["chatroom_post_message"](
            project="p",
            thread_id="T-pr-review-143",
            msg_type="decide",
            author="human",
            content="force",
            closes_thread="T-pr-review-143",
            owner_override_reason="stale thread; closing",
        )

    kwargs = adapter.post_message.await_args.kwargs
    assert kwargs["close_sanction"] == {
        "kind": "human_override",
        "reason": "stale thread; closing",
    }


@pytest.mark.asyncio
async def test_an_ordinary_message_sends_no_sanction(settings: Settings) -> None:
    """Nothing was bypassed, so there is nothing to attribute."""
    tools = _capture_tools(settings)
    adapter = _adapter(owner="Bohr", tags=["design"])
    with patch.object(chatroom_tools, "_adapter", return_value=adapter), \
         patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()):
        await tools["chatroom_post_message"](
            project="p",
            thread_id="T-pr-review-143",
            msg_type="report",
            author="Heisenberg",
            content="progress",
            role="implementer",
        )

    assert adapter.post_message.await_args.kwargs["close_sanction"] is None
