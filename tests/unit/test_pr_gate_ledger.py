"""Unit tests for the PR-gate ledger close carve-out (T-pr-gate-ledger-debt).

Tier-C msg-1001 §2 adopted P-A② + P-C with an explicit acceptance condition:

    "(b)/(c) が機構的に閉じられないことが受入条件（閉じられてしまうなら
     carve-out が広すぎる）"

So the centre of gravity here is not "does the happy path work" but "does the
predicate refuse the four threads a human must still rule on". Those four are
real, and so are their artifacts — every SHA, review state and review id in
:data:`_REAL_PRS` was read from the GitHub API on 2026-08-14 and matches the
full-census table in msg-978 §3. Pinning against the recorded reality (rather
than invented fixtures) is what makes this a test of the acceptance condition
instead of a test of my own understanding of it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from magickit.config import Settings
from magickit.mcp import pr_gate_ledger
from magickit.mcp.pr_gate_ledger import (
    LedgerVerdict,
    evaluate_ledger_verdict,
    is_pr_gate_ledger_thread,
    parse_pr_ref,
)
from magickit.mcp.tools import chatroom as chatroom_tools

GATE_TAG = "gate:naysayer"

# --------------------------------------------------------------------------- #
# The census, as recorded. (a) = closable, (b)/(c) = must NOT be closable.
# --------------------------------------------------------------------------- #


def _pr(head: str, *, merged: bool = True, state: str = "closed") -> dict[str, Any]:
    return {"merged": merged, "state": state, "head": {"sha": head}}


def _rv(state: str, commit_id: str, review_id: int) -> dict[str, Any]:
    return {"state": state, "commit_id": commit_id, "id": review_id}


_REAL_PRS: dict[str, tuple[dict[str, Any], list[dict[str, Any]], bool, str]] = {
    # --- (a): APPROVED at the exact merged head -> the loop may file these ---
    "SpirrowGames/spirrow-mindwire#143": (
        _pr("7828c7e725861d49edfc9af62b3ecaf19dceabc6"),
        [_rv("APPROVED", "7828c7e725861d49edfc9af62b3ecaf19dceabc6", 4923581091)],
        True,
        "single approve, on the head that merged",
    ),
    "SpirrowGames/spirrow-mindwire#142": (
        _pr("450dc2052cde96890fb8897cd03c647668e0fd66"),
        [
            _rv("CHANGES_REQUESTED", "02344219d5f9582f254d63c8211aef13e869a306", 4922558939),
            _rv("CHANGES_REQUESTED", "481edbe2e60f9774259431638d7063b40cd81fae", 4922690012),
            _rv("APPROVED", "450dc2052cde96890fb8897cd03c647668e0fd66", 4922840433),
        ],
        True,
        "earlier REQUEST_CHANGES rounds are stale and must not veto the final approve",
    ),
    # --- (b): blocked, each for a different reason -------------------------- #
    "SpirrowGames/spirrow-mindwire#135": (
        # The verdict/artifact split (msg-978 §4-1): the review body at this
        # head ends "VERDICT: APPROVE", but the submitted artifact is
        # CHANGES_REQUESTED because the diff was truncated. Reading the prose
        # would file this away; reading the artifact does not.
        _pr("1cd55bbf77a1caaa6b5118b4b44165af340f20b4"),
        [
            _rv("COMMENTED", "249d18ab1ccc4f9596f19f4b5521780eaa883e0c", 4890696380),
            _rv("CHANGES_REQUESTED", "7c686f9b2c546387cc6a5e0b410a33452bef529e", 4891308006),
            _rv("CHANGES_REQUESTED", "89950dd72d43d959481baf2aa02324345c031de9", 4892842390),
            _rv("CHANGES_REQUESTED", "1cd55bbf77a1caaa6b5118b4b44165af340f20b4", 4892885581),
        ],
        False,
        "exact-head artifact is CHANGES_REQUESTED even though its body says APPROVE",
    ),
    "SpirrowGames/Spirrow-VoxelWorld#179": (
        _pr("fd528e589dce9eef62d6e71ce8b408b4ef3594f9"),
        [
            _rv("CHANGES_REQUESTED", "4d6707f3e78d07efd8cb4fe1b6406d65a6bf3d1d", 4839089744),
            _rv("CHANGES_REQUESTED", "fd528e589dce9eef62d6e71ce8b408b4ef3594f9", 4840631778),
        ],
        False,
        "objection stands at the exact head (dead enum still shipped)",
    ),
    "SpirrowGames/Spirrow-VoxelWorld#184": (
        # msg-978 §4-2: the only review sits on 39627d8; eca68cf shipped unreviewed.
        _pr("eca68cf23a49fd9dd49ff67acc490b8ade8d4550"),
        [_rv("CHANGES_REQUESTED", "39627d8ae157fd67c0010c793d144d5510e7f534", 4901146600)],
        False,
        "merged head was never reviewed at all",
    ),
    # --- (c): the one Tier-C deliberately left unruled ---------------------- #
    "SpirrowGames/spirrow-mindwire#114": (
        _pr("6b48df89cc5fccfd89d493ea03b98442510cfa21"),
        [
            _rv("CHANGES_REQUESTED", "fb2d8d86e6f0d0aa9193ba29c2f42fa700ebeef6", 4559524147),
            _rv("CHANGES_REQUESTED", "bda9c6bfbe8b703caac4cdc101a8c3e3ca67e0db", 4561015932),
        ],
        False,
        "no review of any kind on the merged head (msg-1001 §4 left this to a human)",
    ),
}


@pytest.mark.parametrize("slug", sorted(_REAL_PRS))
def test_census_verdicts_match_the_adjudicated_ledger(slug: str) -> None:
    """Every audited PR gets the verdict msg-978/msg-1001 assigned it.

    This is the acceptance condition in one test: the four surviving threads
    ((b)×3 + (c)) must all come back ``closable=False``, and the sampled (a)
    threads must come back True.
    """
    pr, reviews, expected, why = _REAL_PRS[slug]
    verdict = evaluate_ledger_verdict(pr, reviews, pr_slug=slug)
    assert verdict.closable is expected, f"{slug}: {why} — got {verdict.reason}"


def test_the_four_surviving_threads_are_all_refused() -> None:
    """Stated as a whole rather than per-PR, because "widened too far" is a
    property of the *set*: if any future edit makes one of these closable, the
    carve-out has stopped separating bookkeeping from judgement.
    """
    survivors = [
        "SpirrowGames/spirrow-mindwire#135",
        "SpirrowGames/Spirrow-VoxelWorld#179",
        "SpirrowGames/Spirrow-VoxelWorld#184",
        "SpirrowGames/spirrow-mindwire#114",
    ]
    for slug in survivors:
        pr, reviews, _, _ = _REAL_PRS[slug]
        assert evaluate_ledger_verdict(pr, reviews, pr_slug=slug).closable is False


def test_refusal_reason_names_the_missing_artifact() -> None:
    """A refusal must say what is absent, not merely that it was refused."""
    pr, reviews, _, _ = _REAL_PRS["SpirrowGames/Spirrow-VoxelWorld#184"]
    verdict = evaluate_ledger_verdict(pr, reviews, pr_slug="SpirrowGames/Spirrow-VoxelWorld#184")
    assert "eca68cf23a49fd9dd49ff67acc490b8ade8d4550" in verdict.reason
    assert "No APPROVED review exists" in verdict.reason


def test_approve_on_a_non_merged_head_is_named_as_such() -> None:
    """The #114/#184 shape ("approved, but not here") gets its own wording."""
    verdict = evaluate_ledger_verdict(
        _pr("head-that-merged"),
        [_rv("APPROVED", "an-earlier-commit", 1)],
        pr_slug="o/r#1",
    )
    assert verdict.closable is False
    assert "an-earlier-commit" in verdict.reason
    assert "judged a different diff" in verdict.reason


# --------------------------------------------------------------------------- #
# fail-closed on everything unproven
# --------------------------------------------------------------------------- #


def test_unmerged_pr_is_not_closable() -> None:
    verdict = evaluate_ledger_verdict(
        _pr("abc", merged=False, state="open"),
        [_rv("APPROVED", "abc", 1)],
        pr_slug="o/r#1",
    )
    assert verdict.closable is False
    assert "not merged" in verdict.reason


@pytest.mark.parametrize(
    "pr,reviews",
    [
        (None, [_rv("APPROVED", "abc", 1)]),
        ("not-a-dict", [_rv("APPROVED", "abc", 1)]),
        (_pr("abc"), None),
        (_pr("abc"), "not-a-list"),
        ({"merged": True, "state": "closed"}, [_rv("APPROVED", "abc", 1)]),  # no head sha
        ({"merged": True, "state": "closed", "head": {}}, [_rv("APPROVED", "abc", 1)]),
    ],
)
def test_unreadable_payloads_are_never_closable(pr: Any, reviews: Any) -> None:
    """Absence of proof is not proof — every malformed shape refuses."""
    assert evaluate_ledger_verdict(pr, reviews, pr_slug="o/r#1").closable is False


def test_non_dict_rows_in_the_review_list_are_skipped_not_trusted() -> None:
    verdict = evaluate_ledger_verdict(
        _pr("abc"), ["garbage", None, _rv("APPROVED", "abc", 7)], pr_slug="o/r#1"
    )
    assert verdict.closable is True
    assert verdict.approving_review_id == 7


@pytest.mark.asyncio
async def test_github_failure_yields_refusal_not_an_exception() -> None:
    """An unreachable github-mcp must degrade to "not closable", not raise.

    A raise here would surface as a tool crash on a close that would have been
    refused anyway; the carve-out withholding itself simply leaves the caller
    on the human-only path that predates it.
    """

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("github-mcp unreachable")

    with (
        patch("magickit.mcp.github_dispatch._resolve_pat", return_value="pat"),
        patch("magickit.mcp.github_dispatch._mcp_call", new=_boom),
    ):
        verdict = await pr_gate_ledger.fetch_ledger_verdict(
            pr_gate_ledger.PrRef("SpirrowGames", "spirrow-mindwire", 143)
        )
    assert verdict.closable is False
    assert "RuntimeError" in verdict.reason


@pytest.mark.asyncio
async def test_missing_pat_yields_refusal() -> None:
    """A deployment with no GitHub credentials refuses rather than crashes."""
    with patch("magickit.mcp.github_dispatch._resolve_pat", side_effect=RuntimeError("PAT unset")):
        verdict = await pr_gate_ledger.fetch_ledger_verdict(
            pr_gate_ledger.PrRef("SpirrowGames", "spirrow-mindwire", 143)
        )
    assert verdict.closable is False


# --------------------------------------------------------------------------- #
# thread recognition + ref parsing
# --------------------------------------------------------------------------- #


def test_carve_out_requires_both_the_drivers_owner_and_its_tag() -> None:
    """Either half alone must not qualify a thread.

    ``owner`` alone would sweep in anything else the orchestrator opens; the
    tag alone would let a hand-opened thread claim the carve-out by typing one
    word into its tag list.
    """
    assert is_pr_gate_ledger_thread({"owner": "orchestrator", "tags": ["pr-review"]}) is True
    assert is_pr_gate_ledger_thread({"owner": "orchestrator", "tags": ["naysayer"]}) is False
    assert is_pr_gate_ledger_thread({"owner": "Bohr", "tags": ["pr-review"]}) is False
    assert is_pr_gate_ledger_thread({"owner": "human", "tags": ["pr-review"]}) is False
    assert is_pr_gate_ledger_thread({"owner": "orchestrator"}) is False
    assert is_pr_gate_ledger_thread({"owner": "orchestrator", "tags": "pr-review"}) is False


def test_real_thread_titles_parse() -> None:
    """The driver builds titles as ``PR review (develop→main) — <pr_ref>``
    (spirrow_mindwire.orchestrator.fire_pr_review), where ``pr_ref`` is either
    ``owner/repo#n`` or a PR URL. Both forms must survive the round trip.
    """
    ref = parse_pr_ref("PR review (develop→main) — SpirrowGames/spirrow-mindwire#135")
    assert ref is not None and ref.slug == "SpirrowGames/spirrow-mindwire#135"

    ref = parse_pr_ref("PR review (develop→main) — SpirrowGames/Spirrow-VoxelWorld#184")
    assert ref is not None and ref.number == 184

    ref = parse_pr_ref("PR review — https://github.com/SpirrowGames/spirrow-magickit/pull/9")
    assert ref is not None and ref.slug == "SpirrowGames/spirrow-magickit#9"

    assert parse_pr_ref("PR review (develop→main) — the one about caching") is None
    assert parse_pr_ref("") is None


# --------------------------------------------------------------------------- #
# wrapper layer: what the close path actually does with the verdict
# --------------------------------------------------------------------------- #


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


@pytest.fixture
def settings() -> Settings:
    return Settings(
        conclair_url="http://localhost:8115",
        conclair_timeout=5.0,
        naysayer_gate_enabled=True,
        naysayer_gate_tag=GATE_TAG,
        naysayer_identities=["Einstein"],
    )


def _adapter(*, owner: str, tags: list[str], title: str) -> MagicMock:
    adapter = MagicMock()
    adapter.get_thread = AsyncMock(
        return_value={
            "thread": {
                "thread_id": "T-pr-review-143",
                "owner": owner,
                "tags": tags,
                "title": title,
            },
            "messages": [],
            "mode": "full",
        }
    )
    adapter.close_thread = AsyncMock(return_value={"thread": {}, "decide_msg": {}})
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


_TITLE = "PR review (develop→main) — SpirrowGames/spirrow-mindwire#143"


def _patched(adapter: MagicMock, verdict: LedgerVerdict):
    return (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()),
        patch.object(chatroom_tools, "fetch_ledger_verdict", AsyncMock(return_value=verdict)),
    )


@pytest.mark.asyncio
async def test_provable_thread_is_filed_by_a_role_with_an_audit_note(settings: Settings) -> None:
    """The whole point: a non-human closes an orchestrator-owned thread."""
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review", "naysayer"], title=_TITLE)
    verdict = LedgerVerdict(
        True,
        "merged and approved at head",
        pr_slug="SpirrowGames/spirrow-mindwire#143",
        merged_head="7828c7e",
        approving_review_id=4923581091,
    )
    p1, p2, p3 = _patched(adapter, verdict)
    with p1, p2, p3:
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-143",
            summary_content="filed: shipped with an approve at the merged head",
            author="Heisenberg",
            embodiment="terminal_coding_agent",
            role="implementer",
        )

    assert "error_type" not in result
    kwargs = adapter.close_thread.await_args.kwargs
    assert kwargs["owner_override"] is True
    assert kwargs["owner_override_reason"] == "merged and approved at head"
    # The bypass must be legible in the body, not only in the audit event.
    assert "[pr-gate-ledger-close]" in kwargs["summary_content"]
    assert "author=Heisenberg" in kwargs["summary_content"]
    assert "approving_review_id=4923581091" in kwargs["summary_content"]


@pytest.mark.asyncio
async def test_unprovable_thread_is_refused_and_never_reaches_conclair(
    settings: Settings,
) -> None:
    """A refusal must stop here — not be forwarded and rejected downstream."""
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review"], title=_TITLE)
    verdict = LedgerVerdict(
        False,
        "merged at eca68cf with no APPROVED review submitted against that commit.",
        pr_slug="SpirrowGames/Spirrow-VoxelWorld#184",
        merged_head="eca68cf",
    )
    p1, p2, p3 = _patched(adapter, verdict)
    with p1, p2, p3:
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-184",
            summary_content="filing this away",
            author="Heisenberg",
            embodiment="terminal_coding_agent",
            role="implementer",
        )

    assert result["error_type"] == "PrGateLedgerNotClosableError"
    assert "no APPROVED review" in result["error"]
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ordinary_thread_is_untouched_by_the_carve_out(settings: Settings) -> None:
    """A normal (non-driver) thread must keep exactly today's behaviour:
    no override, no ledger lookup, ownership decided by Conclair.
    """
    tools = _capture_tools(settings)
    adapter = _adapter(owner="Bohr", tags=["process"], title="some design thread")
    fetch = AsyncMock()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()),
        patch.object(chatroom_tools, "fetch_ledger_verdict", fetch),
    ):
        await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-design",
            summary_content="done",
            author="Heisenberg",
            embodiment="terminal_coding_agent",
            role="implementer",
        )

    fetch.assert_not_awaited()
    assert adapter.close_thread.await_args.kwargs["owner_override"] is False


@pytest.mark.asyncio
async def test_the_owner_itself_does_not_trigger_a_lookup(settings: Settings) -> None:
    """``orchestrator`` closing its own thread needs no bypass, so the carve-out
    must not interpose (and must not be able to refuse it)."""
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review"], title=_TITLE)
    fetch = AsyncMock()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()),
        patch.object(chatroom_tools, "fetch_ledger_verdict", fetch),
    ):
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-143",
            summary_content="done",
            author="orchestrator",
            embodiment="terminal_coding_agent",
        )

    assert "error_type" not in result
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_human_force_close_still_works_on_a_refused_thread(settings: Settings) -> None:
    """The escape hatch the design depends on.

    (b)/(c) staying mechanically un-closable is only tolerable because a human
    can still rule on them. If the carve-out's refusal also caught the human,
    the four surviving threads would be un-closable by *anyone*.
    """
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review"], title=_TITLE)
    fetch = AsyncMock(return_value=LedgerVerdict(False, "nope"))
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()),
        patch.object(chatroom_tools, "fetch_ledger_verdict", fetch),
    ):
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-114",
            summary_content="accepting the procedural gap knowingly",
            author="human",
            owner_override_reason="Tier-C: technical debt is nil, ratifying the process hole",
        )

    assert "error_type" not in result
    fetch.assert_not_awaited()
    assert adapter.close_thread.await_args.kwargs["owner_override"] is True


@pytest.mark.asyncio
async def test_naysayer_gate_runs_before_the_carve_out(settings: Settings) -> None:
    """Ownership bypass must never double as a gate bypass.

    A gate-tagged PR-review thread with no approving naysayer review has to be
    stopped by the naysayer gate — reached *before* the ledger lookup, so a
    provable merge cannot buy a way past an unreviewed gate.
    """
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review", GATE_TAG], title=_TITLE)
    fetch = AsyncMock(return_value=LedgerVerdict(True, "provable"))
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()),
        patch.object(chatroom_tools, "fetch_ledger_verdict", fetch),
    ):
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-143",
            summary_content="filing",
            author="Heisenberg",
            embodiment="terminal_coding_agent",
            role="implementer",
        )

    assert result["error_type"] == "NaysayerReviewRequiredError"
    fetch.assert_not_awaited()
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_unparseable_title_is_refused_without_a_lookup(settings: Settings) -> None:
    """No PR reference means nothing to verify against — refuse, do not guess."""
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review"], title="PR review — the big one")
    fetch = AsyncMock()
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=_prismind()),
        patch.object(chatroom_tools, "fetch_ledger_verdict", fetch),
    ):
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-999",
            summary_content="filing",
            author="Heisenberg",
            embodiment="terminal_coding_agent",
            role="implementer",
        )

    assert result["error_type"] == "PrGateLedgerNotClosableError"
    assert "no parseable" in result["error"]
    fetch.assert_not_awaited()
    adapter.close_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_closeable_roles_still_gates_the_carve_out(settings: Settings) -> None:
    """The carve-out is layered on the existing stages, not a way around them.

    An identity that may not close at all is refused before any of this runs —
    so "provable state" widens *who* among close-capable identities may file a
    PR-review thread, not the set of close-capable identities.
    """
    tools = _capture_tools(settings)
    adapter = _adapter(owner="orchestrator", tags=["pr-review"], title=_TITLE)
    prismind = MagicMock()
    prismind.get_identity = AsyncMock(
        return_value={
            "success": True,
            "found": True,
            "identity": {"allowed_roles": ["naysayer"]},
            "message": "ok",
        }
    )
    fetch = AsyncMock(return_value=LedgerVerdict(True, "provable"))
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_prismind_adapter", return_value=prismind),
        patch.object(chatroom_tools, "fetch_ledger_verdict", fetch),
    ):
        result = await tools["chatroom_close_thread"](
            project="spirrow-mindwire",
            thread_id="T-pr-review-143",
            summary_content="filing",
            author="Einstein",
            embodiment="terminal_coding_agent",
        )

    assert result["error_type"] == "RoleNotAllowedToClose"
    fetch.assert_not_awaited()
