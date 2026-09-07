"""D-31 の再描画は元の mode を保つか — R-9 / R-10 の pin。

Scope (thread T-decision-post-error-rerender-mode, msg-186 / msg-188):

このファイルは D-31 (POST 失敗時のエラー再描画) の**契約**を pin する。
本スレッドが埋めた 3 つの穴を、それぞれ回帰しないよう固定する:

- **R-3**: 旧実装は再描画で常に ``mode="judgement"`` を返した。∴
  「対象は在るが手番でない」スレッド (parked → 手番の遷移が POST と
  再描画の間で起きた場合) では、**手番でない者に本物の J-fresh フォームと
  材料**を描いていた。 msg-185 Q2 が「単なる見出しの瑕疵ではなく、システムが
  状態について嘘をつく明確な欠陥」と判定した経路。
- **R-5 / R-8**: 再描画時の mode は POST-time snapshot からの carry では
  なく、**描画時の world state から derive** されなければならない。
  server 側の snapshot fallback も禁止 — client-supplied field と同型の
  server-side carry で、msg-186 §2 が「このスレッドが修正しているまさに
  その誤ったページを、エラー時にもう一度描く」経路と特定した。
- **R-10**: 再導出の fetch が失敗したとき、応答は **どの mode も名乗らず**、
  **材料を出さず**、**送信された入力を read-only で echo** する。
  Einstein msg-188 ADVISORY で「再送ボタンを付けない」形に固定
  (判断材料がない場所には決定を送信する導線が存在してはならない)。

行列 (msg-186 §4 / A-1〜A-5):

+---------+---------------------------+---------------------------+
| origin  | 失敗理由 = head 移動        | 失敗理由 = それ以外          |
+=========+===========================+===========================+
| parked  | J-stale (A-1: mode派生)     | J-stale (A-1: mode派生)      |
| advanced| not_waiting/advanced (A-2)   | not_waiting/advanced (A-2)   |
| answered| not_waiting/answered (A-2)   | not_waiting/answered (A-2)   |
| fetch fail | unavailable + echo (A-4/A-5) | unavailable + echo (A-4/A-5) |
+---------+---------------------------+---------------------------+

いずれも「見出しが状態の関数であること (A-1)」「手番でない者に導線を
描かないこと (A-2)」「入力保存 (A-3)」「fetch 失敗時に mode を名乗らない
こと (A-4)」「fetch 失敗時に入力を echo すること (A-5)」を pin する。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools


PROJECT = "spirrow-magickit"
THREAD = "T-error-rerender"


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


def _unknown_target_envelope() -> dict[str, Any]:
    """A minimal ``NextParticipantUnknownError`` envelope.

    Chosen as the trigger for A-1〜A-5 because it (a) is not a head-move
    failure — head is unchanged when the pre-write gate rejects — and (b)
    reliably fires the D-31 re-render path regardless of what world state
    the re-fetch finds. R-2 (msg-184) is what makes this valid: the gate
    rejection is independent of head advancement, so all four rows of
    the R-4 matrix are exercised by the same failure type.
    """
    return {
        "error_type": "NextParticipantUnknownError",
        "error": "next_participant 'typoName' is not a registered identity.",
        "details": {"next_participant": "typoName"},
    }


def _parked_thread_payload() -> dict[str, Any]:
    """Thread state: parked to human — the POST-time origin for all tests."""
    return {
        "thread": {"title": "T-parked", "last_msg_id": "msg-9", "status": "active"},
        "messages": [{
            "author": "Bohr",
            "content": "please decide",
            "next_participant": "human",
            "msg_id": "msg-9",
        }],
        "mode": "full",
    }


def _not_waiting_advanced_payload() -> dict[str, Any]:
    """Thread state at re-render: turn moved to Einstein between submit and re-render.

    The last msg's ``next_participant`` is Einstein, not human, so
    ``_is_parked_to_human`` returns False and the render lands on the
    ``not_waiting`` branch. This is the R-3 shape — "対象は在るが手番でない"
    — that the old fixed-mode re-render served the judgement form to.

    Deliberately does NOT include a ``human`` decide msg — that would
    push ``_classify_not_waiting`` into the ``answered`` sub-state before
    the tail-completeness check runs. This payload is shaped so that
    seeding material with ``head_msg_id="msg-9"`` yields the ``advanced``
    sub-state (heading: "その後スレッドが進んでいます").
    """
    return {
        "thread": {"title": "T-parked", "last_msg_id": "msg-11", "status": "active"},
        "messages": [
            {
                "author": "Bohr",
                "content": "please decide",
                "next_participant": "human",
                "msg_id": "msg-9",
            },
            {
                "author": "Bohr",
                "content": "actually handing to Einstein instead",
                "next_participant": "Einstein",
                "msg_id": "msg-10",
            },
            {
                "author": "Einstein",
                "content": "picked up",
                "next_participant": "Bohr",
                "msg_id": "msg-11",
            },
        ],
        "mode": "full",
    }


# ---------------------------------------------------------------------------
# A-1: heading is a function of derived world state, not a hardcoded constant.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a1_rerender_mode_is_derived_when_thread_is_still_parked():
    """A-1 (msg-186 §4): 描画時の world state から mode を導出する。

    Thread が依然 parked → 見出しは判断ページ ("判断 —"). old contract と
    偶然一致するセル: derive してもこの結果になる. 前ターン (旧実装) では
    「mode="judgement" の定数」で通っていたので、定数が戻っても緑になる。
    A-2 と併せて初めて「定数のセルが 1 つも無い」を担保する。
    """
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value=_parked_thread_payload())
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=_unknown_target_envelope()),
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
                "_freeform": "supporting reason",
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    # Parked → the judgement page's own <h1> heading is present. (The
    # HTML <title> block is a template constant that always contains
    # "判断 —"; asserting on <h1> specifically is what distinguishes
    # mode="judgement" from mode="not_waiting".)
    assert "<h1>判断 —" in r.text
    # The not_waiting heading must NOT appear (mutual exclusivity).
    assert "判断待ちではありません" not in r.text
    assert "この判断は回答済みです" not in r.text


# ---------------------------------------------------------------------------
# A-2: 「対象は在るが手番でない」 origin → 再描画は not_waiting mode になり、
# J-fresh の材料もフォームも描かれない。R-3 (msg-185 Q2 + msg-186) 本命。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2_rerender_derives_not_waiting_when_turn_has_moved(
    isolated_material_store,
):
    """★ A-2 (msg-186 §4 本命): 再描画で mode を derive したときの証拠。

    Setup: POST 時は parked (`typoName` を送って `_check_next_participant`
    で unknown envelope が返る) → D-31 の再描画に入る。**再描画時に fetch
    したら、handoff が起きていて last msg の ``next_participant`` は
    Einstein になっている** (advanced 状態)。material store には msg-9
    時点の材料が残っている ∴ ``_classify_not_waiting`` は "advanced" を
    返し、「その後スレッドが進んでいます」を出す。

    旧実装 (fixed ``mode="judgement"``): 「判断」の見出しと choice radio /
    submit button が並び、手番でない human に本物の判断フォームを描いていた。
    これが R-3 が塞いだ穴。

    新実装 (R-9 derive): 再描画は ``mode="not_waiting"`` を導き、
    "advanced" の 1 行を出す。判断フォームの主 UI は無く、material は
    1 文字も出ない。入力は「書き足す」form に温存される (A-3)。

    このテストが赤に戻るのは、mode 決定が再び derive でなくなった時
    (定数化 / hidden field による carry / POST snapshot fallback) だけ。
    """
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-9",
        signature=None,
        question="Should we keep going?",
        options=[{"id": "A", "label": "keep going", "gain": "", "loss": ""}],
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value=_not_waiting_advanced_payload())
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=_unknown_target_envelope()),
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
                "_freeform": "supporting reason",
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    # Judgement <h1> heading must NOT appear — that would be R-3.
    assert "<h1>判断 —" not in r.text
    # not_waiting was chosen; specifically the "advanced" sub-state
    # (someone else took the turn between submit and re-render).
    assert "その後スレッドが進んでいます" in r.text
    # ★ Material composer text must NOT be rendered — POST landed on a
    # state where the question doesn't apply to us. J-fresh's dedicated
    # composer heading (「何を聞かれているか」) MUST be absent here.
    assert "何を聞かれているか" not in r.text
    assert "Should we keep going?" not in r.text
    # ★ The choose-a-decision primary form marker MUST be absent — no
    # "submit a decision" button for someone whose turn it is not. The
    # radio group container from the judgement page is a reliable signal
    # (present on judgement mode only).
    # The judgement page uses a radio group for ``name="content"``; the
    # not_waiting "書き足す" form uses a hidden input for the same name.
    # Presence of the radio-with-name=content signals the judgement primary
    # form is on the page — that導線 must not exist when the turn has moved.
    assert '<input type="radio" name="content"' not in r.text


# ---------------------------------------------------------------------------
# A-3: 入力保存の番人。derive した先が not_waiting でも、freeform が temtales
# に届いて「書き足す」 form の textarea に復元されなければならない。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a3_input_is_preserved_when_derive_lands_on_not_waiting():
    """A-3 (msg-186 §4): mode 変化で入力が消えないこと。

    R-7 の (b) を採ったので、mode が not_waiting に derive された場合でも
    「書き足す」 form (`_freeform` textarea) に自由記述と select 選択値が
    そのまま復元される。 R-9 の refactor が入力保持経路を壊していないことの
    番人。
    """
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value=_not_waiting_advanced_payload())
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=_unknown_target_envelope()),
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
                "_freeform": "very long free text the human just typed on mobile",
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    # Freeform textarea contents are preserved (the whole D-31 raison d'être).
    assert "very long free text the human just typed on mobile" in r.text


@pytest.mark.asyncio
async def test_a3_input_is_preserved_when_derive_lands_on_judgement():
    """A-3 (companion): same guarantee on the parked-still-parked cell.

    Preserving inputs on the judgement branch is the pre-R-9 promise;
    this pin ensures the R-9 refactor did not silently drop it. Runs in
    parallel to the not_waiting-branch pin above so both cells are
    exercised by the same rule.
    """
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value=_parked_thread_payload())
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=_unknown_target_envelope()),
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
                "_freeform": "text preserved on judgement re-render",
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    assert "text preserved on judgement re-render" in r.text


# ---------------------------------------------------------------------------
# A-4: fetch が失敗するとき、どの mode も名乗らない (R-8 の番人)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a4_rerender_names_no_mode_when_state_refetch_fails():
    """A-4 (msg-186 §4 / R-8): fetch 失敗時は判断 / 判断待ち いずれの見出しも
    出さない。読めていない世界状態を名乗ることが最大の危険源で、そこは静か
    (`unavailable` = 「state を確認できません」) に留まる。

    Old fallback: re-fetch 例外時にも ``mode="judgement"`` を返し、参加者
    リスト空 / verification_unavailable を立てた最低限フォームを描いていた。
    ∴ mode を名乗っていた。R-8 はその名乗り自体を禁じる。
    """
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(
        side_effect=RuntimeError("conclair unreachable during re-render")
    )
    adapter.close = AsyncMock()

    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=_unknown_target_envelope()),
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
                "next_participant": "typoName",
            },
        )

    # D-31 の 400 は維持 (「POST が拒否された」の signal).
    assert r.status_code == 400
    # Neither mode heading appears.
    # The judgement <h1> heading must NOT appear (the <title> block is
    # constant and separately says "判断 — {{ thread_id }}", but the
    # judgement page body's <h1> is what tells the user THIS is the
    # decision form).
    assert "<h1>判断 —" not in r.text
    assert "判断待ちではありません" not in r.text
    assert "この判断は回答済みです" not in r.text
    assert "その後スレッドが進んでいます" not in r.text
    # The state-unknown copy IS what surfaces.
    assert "現在このスレッドの状態を確認できません" in r.text
    # And the primary form (radio group container) is absent — no導線
    # is offered while state is unknown.
    # The judgement page uses a radio group for ``name="content"``; the
    # not_waiting "書き足す" form uses a hidden input for the same name.
    # Presence of the radio-with-name=content signals the judgement primary
    # form is on the page — that導線 must not exist when the turn has moved.
    assert '<input type="radio" name="content"' not in r.text


# ---------------------------------------------------------------------------
# A-5: fetch 失敗時に (i) mode を名乗らず, (ii) 入力を echo し, (iii) 材料が
# 現れないことを同時に留める。両立が強制される 1 本 (BLOCKING の番人)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a5_rerender_echoes_submitted_input_when_state_refetch_fails():
    """A-5 (msg-186 §4 / msg-188 R-10 + ADVISORY): fetch 失敗時の echo。

    3 つの主張を 1 本で pin する:

    (i)  応答本文に ``judgement`` / ``not_waiting`` いずれの見出しも
         現れない (R-8 の再確認 — A-4 との重複は意図的).
    (ii) 送信された各フィールドの値が本文に含まれる (R-10).
    (iii) 材料が現れない (fetch が失敗している ∴ 材料も composer から
          読めていない — 「読めなかった」ものを名乗らない).

    さらに Einstein msg-188 ADVISORY: **submit button が存在しない**
    (判断材料がない場所には決定を送信する導線もまた存在してはならない).

    これで「入力を守るために mode を捏造する」実装も、「mode を守るために
    入力を捨てる」実装も、どちらも赤になる — 両立が強制される。
    """
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(
        side_effect=RuntimeError("conclair unreachable during re-render")
    )
    adapter.close = AsyncMock()

    # No apostrophes / brackets — those are HTML-escaped in the echo box
    # and would make a naive substring assertion miss the point of the
    # check. Any UTF-8 chars are fine and get through the pipeline as-is.
    submitted_freeform = (
        "the human just typed this whole paragraph on a phone "
        "and would lose it entirely if the page failed to echo it back"
    )
    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(
            chatroom_tools, "_check_next_participant",
            AsyncMock(return_value=_unknown_target_envelope()),
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
                "_freeform": submitted_freeform,
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    # (i) No mode heading.
    # The judgement <h1> heading must NOT appear (the <title> block is
    # constant and separately says "判断 — {{ thread_id }}", but the
    # judgement page body's <h1> is what tells the user THIS is the
    # decision form).
    assert "<h1>判断 —" not in r.text
    assert "判断待ちではありません" not in r.text
    assert "この判断は回答済みです" not in r.text
    assert "その後スレッドが進んでいます" not in r.text
    # (ii) Submitted fields echoed.
    assert submitted_freeform in r.text  # freeform
    assert "typoName" in r.text  # next_participant echo (also in error msg)
    assert "A: keep going" in r.text  # content echo
    # (iii) No material composer heading rendered.
    assert "何を聞かれているか" not in r.text
    # No submit button (Einstein ADVISORY): the actual <button ... class="decision-submit">
    # element must be absent — the class NAME itself is in the CSS block
    # always, but the button-element markup only appears when a form is
    # rendered. Same for the radio-with-name=content signal: absence
    # proves no decision導線 is on the page.
    assert '<button type="submit" class="decision-submit"' not in r.text
    assert '<input type="radio" name="content"' not in r.text
