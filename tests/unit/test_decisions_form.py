"""Unit tests for the S5 増分 2 form composition path (chatroom_writes).

Scope, said in advance:

* These tests pin the opt-in composition inside
  ``chatroom_writes.post_message`` -- the G-1 regression (existing POSTs
  unchanged), the compose-before-gates rule (spec §3.4), the D-30 NEXT:
  append, I-12 (send with freeform only), and D-31 (preserve inputs on
  gate errors).
* They do **not** re-cover the D-26' 4-branch GET (that belongs in
  ``test_decisions_routes.py``).
* Nothing here asserts external arrival (A-13). See spec §8.

**msg-102 の欠陥回帰**: 旧トリガ `decision_freeform is not None` は空
`_freeform` (人が textarea を触らない場合) を判別できず、live で人が最初
に I-12 を使った 1 通で sentinel が leak した。トリガは
``_decision_form=1`` に移した (msg-103 §2)。**「空 `_freeform` で opt-in
が起動すること」を pin する回帰テストを必ず含める** (spec §12)。全ての
判断ページ由来テストは `_decision_form=1` を送る (テンプレートが常に送る)。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web import chatroom_writes as ui_writes
from magickit.web import decisions as decision_page


PROJECT = "spirrow-magickit"
THREAD = "T-decide"


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


async def _post(path: str, data: dict[str, str]) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.post(path, data=data)


def _passing_gate(role: str | None = None):
    return AsyncMock(return_value=chatroom_tools._RoleDecision(error=None, role=role))


def _blocking_gate(error: dict[str, Any]):
    return AsyncMock(return_value=chatroom_tools._RoleDecision(error=error, role=None))


# --- pure helpers (composition rules) ------------------------------------
# These test the spec §3.3 / §3.5 truth tables in isolation, without the
# HTTP surface. If they fail, the higher-level tests below are also wrong.


def test_compose_body_both_present_joins_with_blank_line():
    assert decision_page._compose_decision_body("A: yes", "because reason") == (
        "A: yes\n\nbecause reason"
    )


def test_compose_body_content_empty_returns_freeform_only():
    assert decision_page._compose_decision_body("", "just text") == "just text"


def test_compose_body_freeform_empty_returns_content_only():
    """When only a choice button is pressed, the body is exactly the label."""
    assert decision_page._compose_decision_body("A: yes", "") == "A: yes"


def test_compose_body_both_empty_returns_empty_string():
    """Don't invent a new rejection here; let the downstream gates decide."""
    assert decision_page._compose_decision_body("", "") == ""


def test_maybe_append_next_appends_when_no_next_line_present():
    body = "some text"
    out = decision_page._maybe_append_next(body, "Bohr")
    assert out == "some text\n\nNEXT: Bohr"


def test_maybe_append_next_does_not_append_when_next_line_already_present():
    """The human wrote NEXT: X themselves; parser is last-wins ∴ don't
    silently overwrite (D-30, msg-084 §2)."""
    body = "reasoning\n\nNEXT: Einstein"
    out = decision_page._maybe_append_next(body, "Bohr")
    assert out == body  # 1 文字も変わらない (I-6)


def test_maybe_append_next_matches_indented_next_line_too():
    """`^\\s*NEXT:` -- indented NEXT still counts as a directive."""
    body = "reasoning\n\n   NEXT: Einstein"
    assert decision_page._maybe_append_next(body, "Bohr") == body


def test_maybe_append_next_does_not_match_next_in_middle_of_line():
    """`NEXT:` only counts when the line starts with it (after whitespace)."""
    body = "here is my NEXT: idea"  # not a standalone directive
    out = decision_page._maybe_append_next(body, "Bohr")
    assert out == "here is my NEXT: idea\n\nNEXT: Bohr"


def test_maybe_append_next_empty_next_participant_leaves_body_unchanged():
    body = "no target"
    assert decision_page._maybe_append_next(body, "") == body


# --- G-1 regression: _freeform-less POSTs stay bit-identical -------------


@pytest.mark.asyncio
async def test_g1_existing_post_without_decision_form_reaches_conclair_unchanged():
    """G-1 (spec §3.2 / msg-103 §2): POSTs without `_decision_form` do not
    enter the new path.

    Concretely: the adapter receives the raw ``content`` field, no
    composition happened, and the response is the existing HTMX flash
    (not a 303). Trigger has moved off ``_freeform`` — existing forms
    that never send `_decision_form` are bit-identical.
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m1", "type": "question"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": "question", "author": "human", "content": "hi from human"},
        )

    # Legacy flash success (200 + fragment).
    assert r.status_code == 200
    assert "posted m1" in r.text
    adapter.post_message.assert_called_once()
    # Bit-identical: `content` passed through untouched, no composition.
    assert adapter.post_message.call_args.kwargs["content"] == "hi from human"


@pytest.mark.asyncio
async def test_g1_form_without_decision_form_field_does_not_422_on_load():
    """Einstein §2 / msg-103 §2: default None so absence doesn't 422.

    A missing `_decision_form` reaches the handler as ``None`` (not a 422),
    so G-1's early-return branch actually runs. Same rule for `_freeform`
    (still ``Optional[str] = None`` for the same reason).
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m2", "type": "question"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        # No `_decision_form` field at all in the POST body.
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {"type": "question", "author": "human", "content": "no decision form here"},
        )

    assert r.status_code == 200  # not 422
    adapter.post_message.assert_called_once()


@pytest.mark.asyncio
async def test_g1_freeform_present_without_decision_form_stays_on_legacy_path():
    """★ msg-103 §7 の中核 assert (トリガが完全に移ったこと):
    ``_freeform`` を送っていても `_decision_form` が無ければ opt-in を通らず、
    既存挙動そのまま (合成もしない・NEXT: も足さない・303 redirect でもない)。

    もし旧トリガ `decision_freeform is not None` が生きていれば、この
    ケースは opt-in を通ってしまう (それが msg-102 の欠陥と真逆の型)。
    ここは新トリガ (`_decision_form == "1"`) が正しく効いているかの pin。
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "mg1a", "type": "question"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "question",
                "author": "human",
                "content": "existing UI text",
                # _freeform present but _decision_form absent → still legacy.
                "_freeform": "this must not be spliced into content",
                "next_participant": "Bohr",
            },
        )

    assert r.status_code == 200  # not 303: opt-in did not fire
    adapter.post_message.assert_called_once()
    # Bit-identical downstream: no composition, no NEXT: append.
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["content"] == "existing UI text"


@pytest.mark.asyncio
async def test_decision_form_empty_string_does_not_fire_opt_in():
    """★ msg-103 §4-2: `_decision_form=""` (empty hidden) must be treated
    the same as absence (F-1: empty Form field is indistinguishable from
    missing to the handler). ``== "1"`` value-equality is the defence:
    both "absent" and "empty" collapse to a single "not decision-post"
    outcome.

    msg-103 §4-1 line 3 probe: `_decision_form=""` empirically arrives
    as `None` on current FastAPI (0.128 / pydantic 2.12); this test pins
    the resulting handler behavior — legacy path, not opt-in.
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "mg1b", "type": "question"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "question",
                "author": "human",
                "content": "safe body",
                "_decision_form": "",  # empty hidden — must behave as absent
                "_freeform": "should not be spliced",
            },
        )

    # Not 303: opt-in did not fire.
    assert r.status_code == 200
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["content"] == "safe body"  # no composition, no NEXT: append


# --- I-12: freeform-only send (content="") -------------------------------


@pytest.mark.asyncio
async def test_i12_freeform_only_content_is_composed_from_textarea():
    """I-12: the "自由記述だけで送る" button sends the sentinel value + freeform.

    spec §3.1a empirical finding: current FastAPI rejects `content=` (empty
    value) as a required-field missing → the button uses sentinel
    `(自由記述のみ)` which the handler normalizes to empty before compose.
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m3", "type": "decide"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",  # trigger (msg-103 §2)
                # I-12: sentinel that the template's I-12 button sends.
                "content": "(自由記述のみ)",
                "_freeform": "自由記述だけの決定",
                "next_participant": "Bohr",
            },
        )

    # decision-post success → 303 to the /ui thread page (spec §3.7).
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/projects/{PROJECT}/threads/{THREAD}"

    # Body reached Conclair with freeform + D-30 append. Sentinel was
    # stripped (not leaked into the composed body).
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["content"] == "自由記述だけの決定\n\nNEXT: Bohr"
    assert "自由記述のみ" not in kwargs["content"]  # sentinel not leaked


# --- Composition + D-30 with a choice button --------------------------


@pytest.mark.asyncio
async def test_choice_button_and_freeform_are_composed_with_blank_line():
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m4", "type": "decide"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                "content": "A: そのまま進める",
                "_freeform": "理由: すでに検証済み",
                "next_participant": "Bohr",
            },
        )

    assert r.status_code == 303
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["content"] == (
        "A: そのまま進める\n\n理由: すでに検証済み\n\nNEXT: Bohr"
    )


@pytest.mark.asyncio
async def test_d30_leaves_body_unchanged_when_human_wrote_next_line():
    """D-30: if the composed body already has a single-line NEXT:, don't
    append. Parser is last-wins; a silent second NEXT: would overwrite
    the human's directive (spec §3.5)."""
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m5", "type": "decide"}}
    )
    adapter.close = AsyncMock()

    freeform = "検討の結果\n\nNEXT: Einstein"

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                # I-12 sentinel (spec §3.1a) so `content=` isn't dropped as 422.
                "content": "(自由記述のみ)",
                "_freeform": freeform,
                "next_participant": "Bohr",  # ignored because NEXT: is in body
            },
        )

    assert r.status_code == 303
    kwargs = adapter.post_message.call_args.kwargs
    # 人の文章が 1 文字も変わっていない (前方一致より厳しい厳密比較)
    assert kwargs["content"] == freeform


# --- spec §3.4: compose BEFORE gates so close policies see composed body -


@pytest.mark.asyncio
async def test_closing_decide_carries_freeform_into_close_policies():
    """The core trap from msg-097 §4.2: `_enforce_close_policies` reads the
    raw ``content`` argument. If composition happens after this call, the
    freeform is silently lost on the *most important* message the page
    ever produces. This test pins that composition happens FIRST."""
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "m6", "type": "decide"}, "thread_status_changed_to": "resolved"}
    )
    adapter.close = AsyncMock()

    # Record what `_enforce_close_policies` saw for `body_content`.
    captured: dict[str, Any] = {}

    async def fake_enforce(
        adapter_arg, *, project, thread_id, author, body_content, **kw
    ):
        captured["body_content"] = body_content
        return {"action": "allow", "content": body_content}

    with (
        patch.object(chatroom_tools, "_check_close_permitted", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_enforce_close_policies", side_effect=fake_enforce),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                "content": "A: yes",
                "closes_thread": THREAD,
                "_freeform": "long human reasoning that must NOT be dropped",
                "next_participant": "Bohr",
            },
        )

    assert r.status_code == 303
    # Composition (`content` + `_freeform` + D-30 NEXT:) landed in the value
    # `_enforce_close_policies` actually saw. If this were composed *after*
    # the policy, the assertion would be "A: yes" and the freeform lost.
    assert captured["body_content"] == (
        "A: yes\n\nlong human reasoning that must NOT be dropped"
        "\n\nNEXT: Bohr"
    )
    # Same body reaches Conclair (nothing dropped downstream either).
    assert adapter.post_message.call_args.kwargs["content"] == captured["body_content"]


# --- D-31: preserve inputs on next_participant errors --------------------


@pytest.mark.asyncio
async def test_d31_unknown_next_participant_preserves_inputs_and_returns_page():
    """`NextParticipantUnknownError` → 400 + re-rendered judgement page with
    `_freeform` / choice / select values populated. Distinct copy per
    error type (spec §3.6)."""
    envelope = {
        "error_type": "NextParticipantUnknownError",
        "error": "next_participant 'typoName' is not a registered identity.",
        "details": {"next_participant": "typoName"},
    }

    # get_thread will be called for the re-render's best-effort context load.
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value={
        "thread": {"title": "T"},
        "messages": [{"author": "Bohr", "content": "please decide",
                       "next_participant": "human"}],
        "mode": "full",
    })
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=envelope),
        ),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                "content": "A: keep going",
                "_freeform": "very long free text the human just typed",
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    # Inputs preserved (this is D-31's whole point).
    assert "very long free text the human just typed" in r.text
    assert "typoName" in r.text
    # 2 種のエラーを区別: Unknown 側の文面が出ている。
    assert "登録されていません" in r.text


@pytest.mark.asyncio
async def test_d31_unavailable_next_participant_uses_distinct_copy():
    """spec §3.6: 「未登録」と「確認不能」を区別する文面。混ぜない。"""
    envelope = {
        "error_type": "NextParticipantValidationUnavailableError",
        "error": "identity lookup failed (Prismind unreachable)",
        "details": {"next_participant": "Bohr", "reason": "unreachable"},
    }
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(side_effect=RuntimeError("no conclair either"))
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=envelope),
        ),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                "content": "A: keep going",
                "_freeform": "text",
                "next_participant": "Bohr",
            },
        )

    assert r.status_code == 400
    # Unavailable 側の文面が出ている (「登録されていません」ではなく)。
    assert "確認ができませんでした" in r.text
    assert "登録されていません" not in r.text
    # Inputs still preserved even when Conclair also failed to help rebuild ctx.
    assert "text" in r.text


# --- Helper unit tests (kept close to their callers) ---------------------


def test_is_parked_to_human_prefers_structured_field():
    """First rule: structured `next_participant` beats body regex."""
    assert decision_page._is_parked_to_human({
        "next_participant": "human",
        "content": "no NEXT: line here",
    })


def test_is_parked_to_human_structured_field_not_human_is_not_parked():
    """Structured field that points *elsewhere* means the fallback body
    regex should NOT be consulted (Bohr §2)."""
    assert not decision_page._is_parked_to_human({
        "next_participant": "Bohr",
        "content": "NEXT: human",  # fallback would say yes, but structured wins
    })


def test_is_parked_to_human_falls_back_to_body_when_field_missing():
    assert decision_page._is_parked_to_human({
        "content": "some words\n\nNEXT: human",
    })


def test_is_parked_to_human_ignores_next_that_is_not_standalone_line():
    assert not decision_page._is_parked_to_human({
        "content": "the NEXT: idea is human",
    })


def test_is_not_found_matches_common_variants():
    assert decision_page._is_not_found({"error_type": "ThreadNotFound"})
    assert decision_page._is_not_found({"error_type": "not_found"})
    assert decision_page._is_not_found({"error_type": "NotFound"})
    assert decision_page._is_not_found({"error_type": "thread-not-found"})


def test_is_not_found_rejects_other_error_types():
    """Everything else lands on 503 (spec §1 / msg-093 §2 一般則の実体)."""
    assert not decision_page._is_not_found({"error_type": "ChatroomIntegrityError"})
    assert not decision_page._is_not_found({"error_type": "ConclairUpstreamError"})
    assert not decision_page._is_not_found({})


# --- ★ msg-102 の欠陥回帰: 「人が最初に使った 1 回」を pin する ------------
# msg-103 §7: 「テストが `_freeform` 非空しか送っていなかった」ことが CI 緑を
# 通してしまった原因。空 `_freeform` を送るケースを全種類に足す (msg-103 §7)。


@pytest.mark.asyncio
async def test_msg102_regression_empty_freeform_with_sentinel_fires_opt_in():
    """★ msg-102 の実タップと同じ入力 — この 1 件が「人が最初に使った 1 回」
    そのものである。旧トリガ (`decision_freeform is not None`) だと F-1 で
    空 `_freeform` が `None` に潰れて opt-in が起動せず、live で本文に
    sentinel `(自由記述のみ)` が leak した。新トリガ (`_decision_form == "1"`)
    ではテンプレートが hidden で `_decision_form=1` を常に送るため
    opt-in が確実に起動し、sentinel は空へ正規化され、D-30 の NEXT: 追記が
    合成後の本文に載る (msg-103 §2 / §4-1 実測)。

    もしこのテストが失敗するなら、msg-102 の欠陥がそのまま再発している。
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "regression", "type": "decide"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        # Takahito が実タップしたのと同じ入力 (msg-102 §1):
        #   - "自由記述だけで送る" ボタンを押した (content=sentinel)
        #   - 自由記述欄には何も入力していない (_freeform="")
        #   - _decision_form は hidden なので常に付く (template §2)
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",  # ★ opt-in が起動する必要条件
                "content": "(自由記述のみ)",
                "_freeform": "",  # ★ msg-102 の欠陥入力: 空
                "next_participant": "Bohr",
            },
        )

    # decision-post success → 303 (opt-in が起動したことの証拠)。
    # 旧コードでは 200 + legacy flash になっていた (opt-in が起動せず)。
    assert r.status_code == 303
    kwargs = adapter.post_message.call_args.kwargs
    # 本文は "NEXT: Bohr" だけ (sentinel は空へ正規化、freeform は空、
    # `_maybe_append_next` が empty body に append)。
    assert kwargs["content"] == "NEXT: Bohr"
    # sentinel が leak していないこと (msg-102 の症状の否定)。
    assert "自由記述のみ" not in kwargs["content"]


@pytest.mark.asyncio
async def test_choice_button_with_empty_freeform_fires_opt_in_and_appends_next():
    """★ msg-103 §7: 選択肢ボタン + 空 `_freeform` → 本文が "A: …" +
    NEXT: 追記。人が「自由記述に何も書かず選択肢を押す」ケースの pin。

    旧トリガでは opt-in が起動せず、NEXT: 追記なしで content="A: …" が
    そのまま Conclair に届いていた。
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "choice-empty", "type": "decide"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                "content": "A: そのまま進める",
                "_freeform": "",  # ★ 空欄のまま選択肢だけを押した
                "next_participant": "Einstein",
            },
        )

    assert r.status_code == 303
    kwargs = adapter.post_message.call_args.kwargs
    # 選択肢テキスト + 空行 + NEXT: 追記。
    assert kwargs["content"] == "A: そのまま進める\n\nNEXT: Einstein"


@pytest.mark.asyncio
async def test_all_empty_composed_body_is_empty_and_no_next_appended():
    """spec §3.3 の corner: `content` sentinel + 空 `_freeform` + 空
    `next_participant` → 合成本文は "" (新たな拒否を作らない)。

    Einstein msg-104 §3 (P-7) が心配した経路: 空 `content` が下流に届く。
    ここでは downstream (AsyncMock adapter) が受け入れることを confirm。
    live Conclair が空本文で crash するかは外形で測るしかない (§P-7 実測項目、
    spec §11.7)。
    """
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "empty-body", "type": "decide"}}
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        r = await _post(
            f"/ui/projects/{PROJECT}/threads/{THREAD}/messages",
            {
                "type": "decide",
                "author": "human",
                "_decision_form": "1",
                "content": "(自由記述のみ)",
                "_freeform": "",
                "next_participant": "",  # 追記名も空
            },
        )

    assert r.status_code == 303
    kwargs = adapter.post_message.call_args.kwargs
    assert kwargs["content"] == ""  # spec §3.3: 空のまま下流に渡す
