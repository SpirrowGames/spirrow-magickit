"""Unit tests for the "判断待ちではありません" 3-state split (N-1〜N-8).

Scope (msg-136 / msg-137 / Einstein Objection):

The ``not_waiting`` branch of ``GET /dashboard/decisions/{project}/{thread_id}``
was previously a single flat message that mixed two very different
situations — "I already answered" vs "I was never asked" — into one sentence.
That trained readers to ignore the notification, and the notification then
died along with any real judgement request that came through it later
(spirrow-mindwire T-human-terminal-overuse msg-882: "常時点灯する列は
読まれなくなる").

This test file pins the split into 3 states, each with evidence:

- **N-answered**: material.head_msg_id より後に human の ``type == "decide"``
  msg が窓内に存在 → 「この判断は回答済みです」+ 誰がいつ答えたか (根拠付き).
- **N-advanced**: head は動いたが window の末尾が権威と一致し、その間に
  human decide は無い → 「その後スレッドが進んでいます」.
- **N-undetermined**: 判定できない (材料が無い / 窓が届かない / 末尾不完全 /
  head が最終) → 「判断待ちではありません」+ reason ごとの 1 行.

**中心規律** (msg-137 §2 / §7 test 14): 肯定 (answered) は証拠が要る。
否定 (advanced) は完全性が要る。どちらも満たせないものは undetermined に落とす —
「回答済み」と根拠なく言わない、が本スレッドの一次規律の裏返し。

**Einstein Objection**: closed スレッドではフォームを描画しない (Conclair が
``ChatroomStateError`` で拒否する ∴ 送信可能な form を出すと嘘の導線になる).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web import decisions as decision_page


PROJECT = "spirrow-magickit"
THREAD = "T-not-waiting"


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


def _adapter_returning(payload: Any) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value=payload)
    adapter.close = AsyncMock()
    return adapter


async def _get(path: str) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.get(path)


def _msg(
    msg_id: str,
    *,
    author: str = "Bohr",
    type: str = "post",
    next_participant: str | None = None,
    content: str = "",
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Build a Conclair-shaped message dict.

    The field name for the type is deliberately ``"type"`` (not ``msg_type``);
    msg-137 §4 実測 (d): MCP の *引数名* が ``msg_type`` で、*保存・返却される
    field* が ``type``. Test 9 pins that difference.
    """
    m: dict[str, Any] = {
        "msg_id": msg_id,
        "author": author,
        "type": type,
        "content": content,
    }
    if next_participant is not None:
        m["next_participant"] = next_participant
    if timestamp is not None:
        m["timestamp"] = timestamp
    return m


def _thread_payload(
    messages: list[dict[str, Any]],
    *,
    last_msg_id: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """Compose a get_thread response for the not_waiting branch.

    ``last_msg_id`` defaults to ``messages[-1].msg_id`` (the "window is
    complete" case). Pass a different value explicitly to simulate a
    tail_incomplete situation.
    """
    if last_msg_id is None and messages:
        last_msg_id = messages[-1]["msg_id"]
    thread: dict[str, Any] = {"title": "T thread", "status": status}
    if last_msg_id is not None:
        thread["last_msg_id"] = last_msg_id
    return {"thread": thread, "messages": messages, "mode": "full"}


# ---------------------------------------------------------------------------
# pure function tests: _classify_not_waiting (msg-137 §1 の判定順序)
# ---------------------------------------------------------------------------


def test_classify_store_unavailable_when_material_ok_false():
    """(1) material_ok が偽 → store_unavailable。他の条件を検査しない。"""
    state, ev = decision_page._classify_not_waiting(
        material=None, material_ok=False,
        messages=[], thread_head=None,
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_STORE_UNAVAILABLE


def test_classify_no_material_when_material_is_none():
    """(2) material が無い → no_material。"""
    state, ev = decision_page._classify_not_waiting(
        material=None, material_ok=True,
        messages=[_msg("msg-1")], thread_head="msg-1",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_NO_MATERIAL


def test_classify_material_head_unreadable_when_head_missing():
    """(3) material.head_msg_id が非空 str でない → material_head_unreadable。

    schema 上 head_msg_id は NOT NULL 非空 ∴ 到達不能だが、暗黙の
    fall-through を作らない (総関数化)。"""
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": ""}, material_ok=True,
        messages=[_msg("msg-1")], thread_head="msg-1",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_MATERIAL_HEAD_UNREADABLE

    state, ev = decision_page._classify_not_waiting(
        material={}, material_ok=True,
        messages=[_msg("msg-1")], thread_head="msg-1",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_MATERIAL_HEAD_UNREADABLE


def test_classify_no_messages_when_window_empty():
    """(4) messages が空 → no_messages。"""
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-1"}, material_ok=True,
        messages=[], thread_head="msg-1",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_NO_MESSAGES


def test_classify_head_not_in_window_when_material_head_missing_from_msgs():
    """(5) 所属テスト: head が messages の msg_id に無い → head_not_in_window。

    ★ 「否定」ではなく「不明」を出す — 窓が届いていない状態で
    「回答済みでない」と言うと、それも嘘になる (msg-137 §2 の不変条件)。
    """
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[_msg("msg-105"), _msg("msg-106")], thread_head="msg-106",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_HEAD_NOT_IN_WINDOW


def test_classify_answered_when_human_decide_after_head():
    """(6) head の後ろに ``author=human, type=decide`` → answered。

    根拠 = 最初の 1 通。msg_id / author / (timestamp if any) を返す。
    """
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr", type="post"),
            _msg("msg-101", author="human", type="decide",
                 timestamp="2026-08-23T12:34:56Z"),
        ],
        thread_head="msg-101",
    )
    assert state == "answered"
    assert ev["msg_id"] == "msg-101"
    assert ev["author"] == "human"
    assert ev["timestamp"] == "2026-08-23T12:34:56Z"


def test_classify_answered_returns_first_decide_when_multiple():
    """複数の human decide がある場合、**最初の** 1 通を返す (msg-137 §1 の
    「最初の 1 通」)。UI に描画される根拠 msg を deterministic に固定。"""
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr", type="post"),
            _msg("msg-101", author="human", type="decide"),
            _msg("msg-102", author="human", type="decide"),
        ],
        thread_head="msg-102",
    )
    assert state == "answered"
    assert ev["msg_id"] == "msg-101"


def test_classify_answered_skips_head_msg_itself():
    """head 自身は「後ろ」に含めない (msg-137 §3: messages[head_index+1:])。

    head==human decide だった場合でも、「head 以降」= head の後 ∴ answered
    にならない (head 自体は通知の対象であって、回答ではない)。
    """
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        # head の位置に human decide があるが、後ろは空 (= head_is_current).
        messages=[_msg("msg-100", author="human", type="decide")],
        thread_head="msg-100",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_HEAD_IS_CURRENT


def test_classify_tail_incomplete_when_last_msg_id_does_not_match_rollup():
    """(7) 否定の主張の完全性: messages[-1].msg_id != thread.last_msg_id
    かつ human decide 無し → tail_incomplete。

    ★ advanced にも answered にも落ちない — 窓が末尾まで届いているという
    根拠が無い ∴ 「まだ答えていない」と言えない。
    """
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            _msg("msg-101", author="Bohr"),
        ],
        # rollup は先まで進んでいる ∴ 末尾不完全。
        thread_head="msg-105",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_TAIL_INCOMPLETE


def test_classify_advanced_when_head_moved_and_no_human_decide():
    """(8) head の後ろに msg あり ∧ 末尾完全 ∧ human decide 無し → advanced。"""
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            _msg("msg-101", author="Einstein", type="post"),
        ],
        thread_head="msg-101",
    )
    assert state == "advanced"
    assert ev["thread_head"] == "msg-101"
    assert ev["material_head"] == "msg-100"


def test_classify_head_is_current_when_head_is_last_message():
    """(9) head が最終 msg (後ろが空) ∧ 末尾完全 → head_is_current。

    通知が指していた msg が今も最新なのに、この分岐 (= 駐機中でない) に
    落ちているなら、その msg は判断を求める形ではなかった (故障で human に
    落ちた等)。要件 §2 の「そもそも判断ではなかった」を一次データで言える
    唯一の分岐。
    """
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[_msg("msg-100", author="Bohr")],
        thread_head="msg-100",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_HEAD_IS_CURRENT


# ---------------------------------------------------------------------------
# author / type discrimination (msg-137 §4)
# ---------------------------------------------------------------------------


def test_classify_answered_ignores_non_human_decide():
    """★ msg-137 §4: Bohr の decide は answered にしない — 「回答」は
    human の decide だけ。"""
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            _msg("msg-101", author="Bohr", type="decide"),  # Bohr の decide
        ],
        thread_head="msg-101",
    )
    # human decide が無いので answered にならない → advanced (末尾完全, 後ろあり)
    assert state == "advanced"


def test_classify_answered_ignores_human_non_decide():
    """★ msg-137 §4: human の answer / post は answered にしない。
    fallback を作らない ∴ 「type=answer」で答えた場合は advanced に過小申告
    される (安全側)。"""
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            _msg("msg-101", author="human", type="answer"),
            _msg("msg-102", author="human", type="post"),
        ],
        thread_head="msg-102",
    )
    assert state == "advanced"


def test_classify_answered_field_name_is_type_not_msg_type():
    """★ msg-137 実測 (d): field 名は ``type`` (``msg_type`` ではない)。

    MCP の引数名が ``msg_type`` で、保存・返却される field が ``type`` ∴
    ``{"msg_type": "decide"}`` だけの msg は検出しない。もし将来 field 名が
    変わったら、この test が赤で気付ける形にする。
    """
    # (a) type field: 検出する
    state_a, _ = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            {"msg_id": "msg-101", "author": "human", "type": "decide"},
        ],
        thread_head="msg-101",
    )
    assert state_a == "answered"

    # (b) msg_type field alone: 検出しない
    state_b, _ = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            {"msg_id": "msg-101", "author": "human", "msg_type": "decide"},
        ],
        thread_head="msg-101",
    )
    assert state_b == "advanced"


def test_classify_type_comparison_is_case_sensitive():
    """★ msg-137 §4: type は case 完全一致。Conclair は enum ∴ 大小が
    変わっていたら契約変更で、黙って吸収せず「回答済みでない」= 安全側に。"""
    state, _ = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            {"msg_id": "msg-101", "author": "human", "type": "DECIDE"},
        ],
        thread_head="msg-101",
    )
    assert state == "advanced"


def test_classify_author_comparison_is_case_insensitive():
    """★ msg-137 §4: author は既存 ``_is_parked_to_human`` と同じく ``lower()``
    で扱う (2 箇所で別の規則を持たない)。"""
    state, _ = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100"}, material_ok=True,
        messages=[
            _msg("msg-100", author="Bohr"),
            {"msg_id": "msg-101", "author": "Human", "type": "decide"},
        ],
        thread_head="msg-101",
    )
    assert state == "answered"


# ---------------------------------------------------------------------------
# ★ msg-137 §7 test 14: ordering — (5) が (6) に優先する
# ---------------------------------------------------------------------------


def test_classify_head_not_in_window_beats_apparent_answered():
    """★ msg-137 §7 test 14: 材料あり + head 不在 + human decide が窓内に
    ある、という組み合わせで **(5) が (6) に優先する**。

    窓が届いていないのに証拠らしきものを拾ってはいけない。順序自体が
    肯定/否定の完全性の要求を担保している。
    """
    state, ev = decision_page._classify_not_waiting(
        material={"head_msg_id": "msg-100-MISSING"}, material_ok=True,
        messages=[
            _msg("msg-105", author="human", type="decide"),
            _msg("msg-106", author="Bohr"),
        ],
        thread_head="msg-106",
    )
    assert state == "undetermined"
    assert ev["reason"] == decision_page._NW_HEAD_NOT_IN_WINDOW


# ---------------------------------------------------------------------------
# _msg_timestamp: gracefully degrades
# ---------------------------------------------------------------------------


def test_msg_timestamp_prefers_created_at():
    assert decision_page._msg_timestamp(
        {"created_at": "2026-08-23T00:00:00Z", "timestamp": "later"}
    ) == "2026-08-23T00:00:00Z"


def test_msg_timestamp_falls_back_to_timestamp():
    assert decision_page._msg_timestamp(
        {"timestamp": "2026-08-23T00:00:00Z"}
    ) == "2026-08-23T00:00:00Z"


def test_msg_timestamp_returns_none_when_no_known_field():
    """★ msg-137 §5 N-6: 取れない場合は「不明」と書かず None を返し UI 側で
    時刻欄ごと省く — 時刻の欠落は verdict を弱めない (証拠 msg は手にある)。"""
    assert decision_page._msg_timestamp(
        {"msg_id": "msg-1", "author": "human", "type": "decide"}
    ) is None
    # 空文字は「未取得」と等価
    assert decision_page._msg_timestamp({"created_at": ""}) is None


# ---------------------------------------------------------------------------
# _thread_is_closed (Einstein Objection)
# ---------------------------------------------------------------------------


def test_thread_is_closed_true_for_resolved():
    assert decision_page._thread_is_closed({"status": "resolved"}) is True


def test_thread_is_closed_true_for_superseded():
    assert decision_page._thread_is_closed({"status": "superseded"}) is True


def test_thread_is_closed_false_for_active():
    assert decision_page._thread_is_closed({"status": "active"}) is False


def test_thread_is_closed_false_for_awaiting_reply():
    assert decision_page._thread_is_closed({"status": "awaiting_reply"}) is False


def test_thread_is_closed_false_when_status_missing():
    """Unknown status = "active" と等価に扱う (Einstein 安全側の default:
    フォームを描画する)。「クローズされたと言えない」= 描画するのが正しい。"""
    assert decision_page._thread_is_closed({}) is False


# ---------------------------------------------------------------------------
# _default_next_participant_for_not_waiting (msg-137 §5 N-7)
# ---------------------------------------------------------------------------


def test_default_next_participant_uses_structured_field_when_present():
    """N-7: 最終 msg の構造化 ``next_participant`` を第一候補にする。"""
    assert decision_page._default_next_participant_for_not_waiting([
        _msg("msg-1", author="Bohr", next_participant="Einstein"),
    ]) == "Einstein"


def test_default_next_participant_falls_back_to_last_author():
    """N-7: structured field が無ければ最終 msg の author。"""
    assert decision_page._default_next_participant_for_not_waiting([
        _msg("msg-1", author="Bohr"),  # no next_participant
    ]) == "Bohr"


def test_default_next_participant_empty_when_messages_empty():
    assert decision_page._default_next_participant_for_not_waiting([]) == ""


# ---------------------------------------------------------------------------
# Integration: ASGI GET — page renders the correct state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_page_shows_answered_verdict_with_evidence(isolated_material_store):
    """★ test 1 (msg-137 §7): answered branch renders 「この判断は回答済み
    です」and includes evidence (msg_id / author). 「判断待ちではありません」
    is NOT rendered as the heading."""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    payload = _thread_payload([
        _msg("msg-100", author="Bohr", next_participant="human"),
        # last msg is not parked to human (thread moved on) — but there IS
        # a human decide in between.
        _msg("msg-101", author="human", type="decide",
             timestamp="2026-08-23T12:34:56Z", next_participant="none"),
        _msg("msg-102", author="Bohr", next_participant="Einstein"),
    ])
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "この判断は回答済みです" in r.text
    # 根拠 (N-6): msg id と author が本文に出る。断定だけを出さない。
    assert "msg-101" in r.text
    assert "human" in r.text
    # timestamp があるので表示される
    assert "2026-08-23T12:34:56Z" in r.text
    # 見出しに「判断待ちではありません」は出ない — 3 状態は排他的。
    assert "判断待ちではありません" not in r.text


@pytest.mark.asyncio
async def test_page_shows_advanced_verdict_with_thread_head(isolated_material_store):
    """★ test 2 (msg-137 §7): advanced branch renders 「その後スレッドが
    進んでいます」and shows the current head."""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    payload = _thread_payload([
        _msg("msg-100", author="Bohr", next_participant="human"),
        _msg("msg-107", author="Einstein", next_participant="Bohr"),
    ])
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "その後スレッドが進んでいます" in r.text
    # 通知が指していた head と現在の head が両方見える。
    assert "msg-100" in r.text
    assert "msg-107" in r.text
    assert "この判断は回答済みです" not in r.text


@pytest.mark.asyncio
async def test_page_shows_undetermined_head_not_in_window_does_not_claim_answered(
    isolated_material_store,
):
    """★ test 3 (msg-137 §7): head が窓に無い → undetermined。**本文に
    「回答済み」の文字列が出ない** (肯定の主張には証拠が要る、の実体)。"""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",  # not in window below
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    payload = _thread_payload([
        _msg("msg-500", author="Bohr"),
        _msg("msg-501", author="Bohr"),
    ])
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "判断待ちではありません" in r.text
    # ★ The verdict heading MUST NOT claim "answered" — the guidance body
    # may still contain the word 回答済み as part of "check whether it's
    # answered in chatroom", which is exactly the honest deferral we want.
    assert "この判断は回答済みです" not in r.text
    assert "その後スレッドが進んでいます" not in r.text


@pytest.mark.asyncio
async def test_page_shows_undetermined_tail_incomplete_not_advanced(
    isolated_material_store,
):
    """★ test 4 (msg-137 §7): messages[-1].msg_id != thread.last_msg_id
    かつ human decide 無し → undetermined **ではなく** advanced ではない。

    否定の主張に完全性を要求していることの pin。
    """
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    payload = _thread_payload(
        [
            _msg("msg-100", author="Bohr"),
            _msg("msg-101", author="Bohr"),
        ],
        # rollup 先まで進んでいる ∴ 末尾不完全。
        last_msg_id="msg-999",
    )
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "判断待ちではありません" in r.text
    assert "その後スレッドが進んでいます" not in r.text


@pytest.mark.asyncio
async def test_page_shows_undetermined_no_material(isolated_material_store):
    """★ test 5: 材料無し → undetermined / no_material。既存の「判断待ちでは
    ありません」文言を維持しつつ、reason を 1 行添える。"""
    # No put_material call — material absent.
    payload = _thread_payload([
        _msg("msg-100", author="Bohr", next_participant="Einstein"),
    ])
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "判断待ちではありません" in r.text
    assert "材料が残っていません" in r.text


@pytest.mark.asyncio
async def test_page_shows_undetermined_store_unavailable_stays_200(
    isolated_material_store, monkeypatch,
):
    """★ test 6: material store が例外 → 500 に落とさず 200 で
    undetermined / store_unavailable を出す (D-31 の fail-to-<something>
    と同じ精神)。"""
    from magickit.web import decisions as decisions_module

    class BrokenStore:
        async def get_material(self, **_):
            raise RuntimeError("simulated db outage")

    monkeypatch.setattr(
        decisions_module, "_get_material_store", lambda: BrokenStore()
    )

    payload = _thread_payload([
        _msg("msg-100", author="Bohr", next_participant="Einstein"),
    ])
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "判断待ちではありません" in r.text
    assert "材料ストアに一時的に接続できませんでした" in r.text


@pytest.mark.asyncio
async def test_page_shows_undetermined_head_is_current(isolated_material_store):
    """★ test 7: head が最終 msg (後ろ空) → undetermined / head_is_current。

    通知が指していた msg が今も最新なのに not_waiting 分岐に来ているなら
    その msg は判断を求める形ではなかった — 「そもそも判断ではなかった」を
    一次データで言える唯一の分岐 (msg-137 §1 の 9)。
    """
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    payload = _thread_payload([
        # 最終 msg = head, かつ human parking ではない (故障で来た pattern).
        _msg("msg-100", author="Bohr", next_participant="Einstein"),
    ])
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "判断待ちではありません" in r.text
    assert "人の判断を求める形になっていません" in r.text


# ---------------------------------------------------------------------------
# I-12: フォームは 3 状態すべてで残す (msg-136 §4 / msg-137 §5 N-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_form_is_present_in_all_three_states(isolated_material_store):
    """★ test 10 (msg-137 §7): 3 状態すべてで opt-in hidden + type=decide +
    author=human + sentinel submit が描画される。"""

    async def _fetch_states() -> list[str]:
        """Return the response text for the 3 states in one flow."""
        texts: list[str] = []
        # answered
        store = isolated_material_store()
        await store.put_material(
            project=PROJECT, thread_id=THREAD,
            head_msg_id="msg-100",
            signature=None, question=None, options=None,
            recommendation=None, recommendation_reason=None, unknowns=None,
        )
        payload_answered = _thread_payload([
            _msg("msg-100", author="Bohr", next_participant="human"),
            _msg("msg-101", author="human", type="decide",
                 next_participant="none"),
        ])
        with patch.object(
            chatroom_tools, "_adapter",
            return_value=_adapter_returning(payload_answered),
        ):
            texts.append((await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")).text)

        # advanced
        payload_advanced = _thread_payload([
            _msg("msg-100", author="Bohr"),
            _msg("msg-107", author="Einstein", next_participant="Bohr"),
        ])
        with patch.object(
            chatroom_tools, "_adapter",
            return_value=_adapter_returning(payload_advanced),
        ):
            texts.append((await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")).text)

        # undetermined (head_is_current, active thread — form should render)
        payload_undet = _thread_payload([
            _msg("msg-100", author="Bohr", next_participant="Einstein"),
        ])
        with patch.object(
            chatroom_tools, "_adapter",
            return_value=_adapter_returning(payload_undet),
        ):
            texts.append((await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")).text)

        return texts

    for text in await _fetch_states():
        assert 'name="_decision_form" value="1"' in text
        assert 'name="type" value="decide"' in text
        assert 'name="author" value="human"' in text
        assert 'value="(自由記述のみ)"' in text


# ---------------------------------------------------------------------------
# Einstein Objection: closed thread must not render the form
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_form_is_not_rendered_when_thread_is_closed(isolated_material_store):
    """★ Einstein Objection: closed スレッドではフォームを描画しない
    (Conclair が ChatroomStateError で拒否 ∴ 嘘の導線になる)。"""
    payload = _thread_payload(
        [
            _msg("msg-100", author="Bohr", next_participant="Einstein"),
        ],
        status="resolved",
    )
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # フォームの opt-in トリガと sentinel submit が両方描画されないこと。
    assert 'name="_decision_form"' not in r.text
    assert 'value="(自由記述のみ)"' not in r.text
    # 代わりに、なぜ描画しないかの理由を書く。
    assert "クローズ" in r.text or "closed" in r.text.lower()


@pytest.mark.asyncio
async def test_form_still_rendered_for_active_thread(isolated_material_store):
    """★ Einstein Objection の裏: active スレッドでは通常どおり描画される
    (I-12 の要件と衝突しないことの pin)。"""
    payload = _thread_payload(
        [
            _msg("msg-100", author="Bohr", next_participant="Einstein"),
        ],
        status="active",
    )
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert 'name="_decision_form" value="1"' in r.text


# ---------------------------------------------------------------------------
# Escape (msg-137 §7 test 12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answered_evidence_is_html_escaped(isolated_material_store):
    """★ test 12 (msg-137 §7): author に ``<script>`` を含む msg を answered
    の根拠にしても escape される (|safe 禁止の lint と併せて)。"""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    payload = _thread_payload([
        _msg("msg-100", author="Bohr", next_participant="human"),
        _msg("msg-101", author="Human<script>alert(1)</script>",
             type="decide", next_participant="none"),
    ])
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # Raw script tag must not survive into the HTML.
    assert "<script>alert(1)</script>" not in r.text
    # Escaped form is present (proof the escape happened, not the string
    # was dropped).
    assert "&lt;script&gt;" in r.text


# ---------------------------------------------------------------------------
# ★ N-8: the old "(最終メッセージが `NEXT: human` ではない)" line is dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_body_only_explanation_line_is_gone(isolated_material_store):
    """★ msg-137 §5 N-8: 現行の 「(最終メッセージが ``NEXT: human`` ではない)」
    は本文規約だけを言っていて、構造化 field 経路を説明していない ∴
    削除される (この分岐の嘘を 1 つ減らす)。

    3 状態のどれでもこの literal は出ない。
    """
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    for payload in (
        # answered
        _thread_payload([
            _msg("msg-100", author="Bohr", next_participant="human"),
            _msg("msg-101", author="human", type="decide",
                 next_participant="none"),
        ]),
        # advanced
        _thread_payload([
            _msg("msg-100", author="Bohr"),
            _msg("msg-107", author="Einstein", next_participant="Bohr"),
        ]),
        # undetermined (head_is_current)
        _thread_payload([
            _msg("msg-100", author="Bohr", next_participant="Einstein"),
        ]),
    ):
        with patch.object(
            chatroom_tools, "_adapter",
            return_value=_adapter_returning(payload),
        ):
            r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")
        assert "NEXT: human" not in r.text


# ---------------------------------------------------------------------------
# Regression: chatroom link stays present (all 3 states)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chatroom_link_present_in_all_states(isolated_material_store):
    """要件 §4: 3 状態すべてで chatroom 導線を残す (「回答済み」だと確認
    したくなるし、「その後進んでいる」なら chatroom を開く動線が必要)。"""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-100",
        signature=None, question=None, options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    for payload in (
        _thread_payload([
            _msg("msg-100", author="Bohr", next_participant="human"),
            _msg("msg-101", author="human", type="decide",
                 next_participant="none"),
        ]),
        _thread_payload([
            _msg("msg-100", author="Bohr"),
            _msg("msg-107", author="Einstein", next_participant="Bohr"),
        ]),
        _thread_payload([
            _msg("msg-100", author="Bohr", next_participant="Einstein"),
        ]),
    ):
        with patch.object(
            chatroom_tools, "_adapter",
            return_value=_adapter_returning(payload),
        ):
            r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")
        assert f"/ui/projects/{PROJECT}/threads/{THREAD}" in r.text
