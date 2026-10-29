"""「聞くべきだったか」ラベル — 単位 A (T-decision-page-escalation-feedback-label).

正本: msg-1407 + msg-1411 + msg-1416 + msg-1445 + msg-1448 + msg-1451.
テスト名は spec が挙げたもの (msg-1395〜1405 / msg-1445 / msg-1448 /
msg-1451) をそのまま使う。廃止された ``test_label_skipped_when_target_moved``
(msg-1397) と ``test_verify_uses_same_resolver`` (msg-1448)、マーカー前提の
``test_verify_rejects_relay_stop_msg`` (msg-1451) は置かない — 後者は author
判定の ``test_verify_rejects_relay_msg_by_author`` に書き直してある。

外部到達 (:8443 で叩いて確かめる) はここでは測れない。msg-1409 の確認は
deploy 後に別に行う。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web import escalation_labels as el
from tests.route_table import sole_handler

PROJECT = "spirrow-magickit"
THREAD = "T-esc"
AUTHORS = frozenset({"bohr", "einstein", "heisenberg", "fermi"})
SRC = Path(__file__).resolve().parents[2] / "src" / "magickit"

SOURCE_COMMENT = "<!-- source: tools=0 · mcp=0 · tier=naysayer -->"
STOP_MARKER = '<!-- mindwire:stop {"kind":"conductor.stand_down"} -->'


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


@pytest.fixture(autouse=True)
def _authors(monkeypatch):
    """既定の許可リストに固定する (設定ファイルの中身に依存しない)。"""
    monkeypatch.setattr(el, "_label_authors", lambda: AUTHORS)


def m(msg_id: str, author: str, content: str, **extra: Any) -> dict[str, Any]:
    return {"msg_id": msg_id, "author": author, "content": content, **extra}


def esc(msg_id: str, author: str = "Einstein", *, comment: bool = True) -> dict[str, Any]:
    body = "判断をお願いします。\n\nNEXT: human"
    if comment:
        body += f"\n\n{SOURCE_COMMENT}"
    return m(msg_id, author, body)


def thread_result(messages: list[dict[str, Any]], **meta: Any) -> dict[str, Any]:
    thread = {
        "status": "active",
        "title": THREAD,
        "last_msg_id": messages[-1]["msg_id"] if messages else None,
        "msg_count": len(messages),
    }
    thread.update(meta)
    return {"thread": thread, "messages": messages}


def fake_adapter(messages: list[dict[str, Any]] | None, *, post_ok: bool = True, **meta: Any):
    adapter = AsyncMock()
    if messages is None:
        adapter.get_thread = AsyncMock(side_effect=RuntimeError("conclair down"))
    else:
        adapter.get_thread = AsyncMock(return_value=thread_result(messages, **meta))
    adapter.post_message = AsyncMock(
        return_value=(
            {"msg": {"msg_id": "msg-answer", "type": "decide"}}
            if post_ok
            else {"error_type": "ChatroomStateError", "error": "nope"}
        )
    )
    adapter.close = AsyncMock()
    return adapter


async def _request(method: str, path: str, **kw: Any) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.request(method, path, **kw)


def _passing_gate():
    return AsyncMock(return_value=chatroom_tools._RoleDecision(error=None, role=None))


async def decision_post(adapter, form: dict[str, Any]) -> httpx.Response:
    data = {
        "_decision_form": "1",
        "type": "decide",
        "author": "human",
        "content": "A: yes",
        "next_participant": "Bohr",
        **form,
    }
    with (
        patch.object(chatroom_tools, "_check_role_allowed", _passing_gate()),
        patch.object(chatroom_tools, "_check_next_participant", AsyncMock(return_value=None)),
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
    ):
        return await _request(
            "POST", f"/ui/projects/{PROJECT}/threads/{THREAD}/messages", data=data
        )


async def label_post(adapter, form: dict[str, Any]) -> httpx.Response:
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        return await _request(
            "POST", f"/dashboard/decisions/{PROJECT}/{THREAD}/label", data=form
        )


async def label_get(adapter, query: str = "") -> httpx.Response:
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        return await _request(
            "GET", f"/dashboard/decisions/{PROJECT}/{THREAD}/label{query}"
        )


async def page_get(adapter) -> httpx.Response:
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        return await _request("GET", f"/dashboard/decisions/{PROJECT}/{THREAD}")


async def history(store_factory, target: str) -> list[dict[str, Any]]:
    return await store_factory().history(
        project=PROJECT, thread_id=THREAD, target_msg_id=target
    )


async def put_material(material_store_factory, head: str, **kw: Any) -> None:
    await material_store_factory().put_material(
        project=PROJECT,
        thread_id=THREAD,
        head_msg_id=head,
        signature=None,
        question="q?",
        options=None,
        recommendation=None,
        recommendation_reason=None,
        unknowns=None,
        **kw,
    )


# ===========================================================================
# 述語 _is_escalation_msg (msg-1451)
# ===========================================================================


def test_escalation_predicate_reads_last_line_past_source_comments():
    assert el._is_escalation_msg(esc("msg-1"), AUTHORS)
    assert el._is_escalation_msg(esc("msg-1", comment=False), AUTHORS)


def test_escalation_predicate_requires_next_human_on_last_line():
    mid = m("msg-1", "Bohr", "NEXT: human\n\nbut then more text")
    assert not el._is_escalation_msg(mid, AUTHORS)
    other = m("msg-2", "Bohr", "x\n\nNEXT: Fermi")
    assert not el._is_escalation_msg(other, AUTHORS)


def test_relay_ending_next_human_without_marker_is_not_target():
    """msg-941 (pr-gate-relay verdict) / msg-7506 (conductor-relay guard (i))
    の形: stop マーカー無し、最終行 ``NEXT: human``。author で落ちる。"""
    verdict = m(
        "msg-941",
        "pr-gate-relay",
        "<!-- mindwire:verdict APPROVE -->\nOK\n\n"
        "MERGE-WAIT: … (Tier-C の判断依頼ではありません)\nNEXT: human",
    )
    guard = m("msg-7506", "conductor-relay", "Conductor stop — guard (i)\n\nNEXT: human")
    for relay in (verdict, guard):
        msgs = [m("msg-1", "Bohr", "x\n\nNEXT: Einstein"), relay]
        assert not el._is_escalation_msg(relay, AUTHORS)
        assert el._resolve_escalation_target(msgs, {"last_msg_id": relay["msg_id"]}, None, AUTHORS) is None
        verdict_, _ = el._verify_label_target(msgs, {}, relay["msg_id"], AUTHORS)
        assert verdict_ == el.VERIFY_REJECTED


def test_participant_quoting_stop_marker_still_target():
    quoting = m("msg-5", "Bohr", f"relay の本文:\n{STOP_MARKER}\n\nNEXT: human")
    msgs = [quoting]
    assert el._resolve_escalation_target(msgs, {"last_msg_id": "msg-5"}, None, AUTHORS) is quoting
    assert el._verify_label_target(msgs, {}, "msg-5", AUTHORS)[0] == el.VERIFY_FOUND


def test_author_allowlist_from_config(monkeypatch):
    monkeypatch.undo()  # the autouse _authors patch
    msg = esc("msg-1", author="Newton")
    monkeypatch.setattr(
        el, "get_settings", lambda: SimpleNamespace(escalation_label_authors=["Bohr"])
    )
    assert not el._is_escalation_msg(msg, el._label_authors())
    monkeypatch.setattr(
        el,
        "get_settings",
        lambda: SimpleNamespace(escalation_label_authors=["Bohr", "Newton"]),
    )
    assert el._is_escalation_msg(msg, el._label_authors())


def test_escalation_label_authors_default_is_the_three_roles_plus_fermi():
    assert Settings().escalation_label_authors == ["Bohr", "Einstein", "Heisenberg", "Fermi"]
    # 別キー: handoff 先の設定とは共有しない (msg-1451)。
    assert "human" not in Settings().escalation_label_authors


# ===========================================================================
# _resolve_escalation_target (msg-1445 §3.2''' + msg-1451)
# ===========================================================================


def test_target_skips_relay_stop():
    """head が stand_down の relay で、その前の participant が ``NEXT: Fermi``
    → ``None`` (msg-1413 / msg-1412 の形)。"""
    msgs = [
        m("msg-1412", "Einstein", "差し替え案\n\nNEXT: Fermi"),
        m("msg-1413", "pr-gate-relay", f"{STOP_MARKER}\nstand down\n\nNEXT: human"),
    ]
    material = {"head_msg_id": "msg-1413"}
    meta = {"last_msg_id": "msg-1413"}
    assert el._resolve_escalation_target(msgs, meta, material, AUTHORS) is None


def test_target_found_behind_relay():
    target = esc("msg-10")
    msgs = [target, m("msg-11", "conductor-relay", "stop\n\nNEXT: Einstein")]
    material = {"head_msg_id": "msg-11"}
    got = el._resolve_escalation_target(msgs, {"last_msg_id": "msg-11"}, material, AUTHORS)
    assert got is target
    assert got["author"].casefold() in AUTHORS  # escalated_by は必ず許可リスト内


def test_target_skips_non_participant_bot():
    target = esc("msg-20", author="Bohr")
    msgs = [target, m("msg-21", "ci-window-state", "window opened")]
    got = el._resolve_escalation_target(msgs, {"last_msg_id": "msg-21"}, None, AUTHORS)
    assert got is target


def test_target_stops_at_first_participant():
    msgs = [
        esc("msg-30"),
        m("msg-31", "Bohr", "続けます\n\nNEXT: Einstein"),
        m("msg-32", "pr-gate-relay", "verdict\n\nNEXT: human"),
    ]
    assert el._resolve_escalation_target(msgs, {"last_msg_id": "msg-32"}, None, AUTHORS) is None


def test_parked_without_material_targets_parked_msg():
    parked = esc("msg-40")
    msgs = [m("msg-39", "Bohr", "x\n\nNEXT: Einstein"), parked]
    assert el._resolve_escalation_target(msgs, {"last_msg_id": "msg-40"}, None, AUTHORS) is parked


def test_fresh_material_head_is_the_start_point():
    head = esc("msg-50")
    msgs = [head, m("msg-51", "Bohr", "x\n\nNEXT: Einstein")]
    # material の head は msg-50 だが thread の last は msg-51 → stale ∴ 末尾起点
    stale = el._resolve_escalation_target(
        msgs, {"last_msg_id": "msg-51"}, {"head_msg_id": "msg-50"}, AUTHORS
    )
    assert stale is None
    # head 固定 (P-L2'' (2)) なら head から
    fixed = el._resolve_escalation_target(
        msgs, {"last_msg_id": "msg-51"}, None, AUTHORS, start_msg_id="msg-50"
    )
    assert fixed is head


def test_label_target_is_not_the_answer_msg():
    """回答 POST 後の状態 (末尾が human の回答) でも、対象が回答 msg にならない。"""
    msgs = [esc("msg-60"), m("msg-61", "human", "A: yes", type="decide")]
    got = el._resolve_escalation_target(msgs, {"last_msg_id": "msg-61"}, None, AUTHORS)
    assert got is None or got["msg_id"] != "msg-61"


# ===========================================================================
# _verify_label_target (msg-1448)
# ===========================================================================


def test_resolver_and_verifier_share_predicate():
    cases = [
        [esc("msg-1")],
        [esc("msg-1"), m("msg-2", "pr-gate-relay", "v\n\nNEXT: human")],
        [esc("msg-1", author="Bohr"), m("msg-2", "ci-window-state", "w")],
        [m("msg-1", "Bohr", f"{STOP_MARKER}\n\nNEXT: human")],
    ]
    for msgs in cases:
        got = el._resolve_escalation_target(
            msgs, {"last_msg_id": msgs[-1]["msg_id"]}, None, AUTHORS
        )
        assert got is not None
        verdict, found = el._verify_label_target(msgs, {}, got["msg_id"], AUTHORS)
        assert verdict == el.VERIFY_FOUND and found is got


def test_verify_accepts_past_escalation_after_answer():
    msgs = [
        esc("msg-1"),
        m("msg-2", "human", "A: yes\n\nNEXT: Bohr", type="decide"),
        m("msg-3", "Bohr", "了解\n\nNEXT: Heisenberg"),
    ]
    assert el._resolve_escalation_target(msgs, {"last_msg_id": "msg-3"}, None, AUTHORS) is None
    verdict, found = el._verify_label_target(msgs, {"msg_count": 3}, "msg-1", AUTHORS)
    assert verdict == el.VERIFY_FOUND and found["author"] == "Einstein"


def test_verify_rejects_relay_msg_by_author():
    msgs = [m("msg-1413", "pr-gate-relay", f"{STOP_MARKER}\n\nNEXT: human")]
    assert el._verify_label_target(msgs, {}, "msg-1413", AUTHORS)[0] == el.VERIFY_REJECTED


def test_verify_rejects_participant_without_human_handoff():
    msgs = [m("msg-1412", "Einstein", "x\n\nNEXT: Fermi")]
    assert el._verify_label_target(msgs, {}, "msg-1412", AUTHORS)[0] == el.VERIFY_REJECTED


def test_verify_rejects_unknown_msg():
    msgs = [esc("msg-1")]
    assert el._verify_label_target(msgs, {"msg_count": 1}, "msg-999", AUTHORS)[0] == el.VERIFY_REJECTED


def test_verify_unverifiable_when_thread_unreadable_or_windowed():
    assert el._verify_label_target(None, {}, "msg-1", AUTHORS)[0] == el.VERIFY_UNVERIFIABLE
    windowed = el._verify_label_target([esc("msg-9")], {"msg_count": 50}, "msg-1", AUTHORS)
    assert windowed[0] == el.VERIFY_UNVERIFIABLE


# ===========================================================================
# _label_write_plan / tags / snapshot (pure)
# ===========================================================================


def _row(label: str, tags=(), note="", source=el.SNAPSHOT_ABSENT, **kw) -> dict[str, Any]:
    return {
        "label": label,
        "reason_tags": list(tags),
        "note": note,
        "snapshot_source": source,
        "rules_hash": kw.get("rules_hash"),
        "jev_p": kw.get("jev_p"),
        "jev_rule": kw.get("jev_rule"),
        "jev_model_version": kw.get("jev_model_version"),
    }


def test_write_plan_noop_cases():
    li = el.LabelInput
    assert el._label_write_plan(None, li(label=el.LABEL_NONE)) is False
    assert el._label_write_plan(None, li(label=el.LABEL_CLEARED)) is False
    cur = _row(el.LABEL_SHOULD_ASK, ["rule_1"], "n")
    assert el._label_write_plan(cur, li(label=el.LABEL_SHOULD_ASK, tags=["rule_1"], note="n")) is False
    assert el._label_write_plan(cur, li(label=el.LABEL_SHOULD_ASK, tags=["rule_2"], note="n")) is True
    assert el._label_write_plan(cur, li(label=el.LABEL_CLEARED)) is True
    assert el._label_write_plan(None, li(label=el.LABEL_BORDERLINE)) is True


def test_tags_mismatch_rules():
    assert el._tags_mismatch(el.LABEL_SHOULD_ASK, ["rule_1", "rule_5"]) is False
    assert el._tags_mismatch(el.LABEL_SHOULD_ASK, ["impl_detail"]) is True
    assert el._tags_mismatch(el.LABEL_SHOULD_NOT_ASK, ["proceed_check", "other"]) is False
    assert el._tags_mismatch(el.LABEL_SHOULD_NOT_ASK, ["rule_2"]) is True
    assert el._tags_mismatch(el.LABEL_BORDERLINE, ["rule_2", "other"]) is False
    assert el._tags_mismatch(el.LABEL_BORDERLINE, ["bogus"]) is True


def test_snapshot_material_none_is_absent():
    snap = el._snapshot_from_material(None, "msg-1")
    assert snap["snapshot_source"] == el.SNAPSHOT_ABSENT
    assert snap["rules_hash"] is None and snap["jev_p"] is None


def test_snapshot_head_unreadable_is_mismatch():
    for material in ({}, {"head_msg_id": ""}, {"head_msg_id": 7}, {"head_msg_id": "msg-2"}):
        snap = el._snapshot_from_material(material, "msg-1")
        assert snap["snapshot_source"] == el.SNAPSHOT_MISMATCH
        assert snap["jev_p"] is None and snap["rules_hash"] is None


def test_snapshot_fresh_copies_jev_and_rules_hash():
    material = {
        "head_msg_id": "msg-1",
        "rules_hash": "sha256:abc",
        "jev": {"should_ask_human": 0.66, "rule": "G3", "model_version": "v2"},
    }
    snap = el._snapshot_from_material(material, "msg-1")
    assert snap == {
        "snapshot_source": el.SNAPSHOT_FRESH,
        "rules_hash": "sha256:abc",
        "jev_p": 0.66,
        "jev_rule": "G3",
        "jev_model_version": "v2",
    }


def test_snapshot_never_downgrades():
    """S-1 を履歴列で検査し、fresh 行の値が最も古い fresh 行と一致すること (msg-1405)。"""
    fresh_mat = {"head_msg_id": "msg-1", "rules_hash": "h1",
                 "jev": {"should_ask_human": 0.2, "rule": "r", "model_version": "v"}}
    later_mat = {"head_msg_id": "msg-1", "rules_hash": "h2",
                 "jev": {"should_ask_human": 0.9, "rule": "r2", "model_version": "v2"}}
    sequence = [None, {"head_msg_id": "msg-x"}, fresh_mat, None, later_mat, {"head_msg_id": "msg-y"}]
    hist: list[dict[str, Any]] = []
    rank = {el.SNAPSHOT_ABSENT: 0, el.SNAPSHOT_MISMATCH: 0, el.SNAPSHOT_FRESH: 1}
    for material in sequence:
        snap = el._resolve_snapshot(hist, material, "msg-1")
        if hist:
            assert rank[snap["snapshot_source"]] >= rank[hist[-1]["snapshot_source"]]
        hist.append(_row(el.LABEL_BORDERLINE, **snap, source=snap["snapshot_source"]))
    fresh_rows = [r for r in hist if r["snapshot_source"] == el.SNAPSHOT_FRESH]
    oldest = fresh_rows[0]
    for r in fresh_rows:
        assert (r["rules_hash"], r["jev_p"], r["jev_rule"]) == (
            oldest["rules_hash"], oldest["jev_p"], oldest["jev_rule"]
        )
    assert oldest["rules_hash"] == "h1"


# ===========================================================================
# 判断ページ GET — ラベル欄
# ===========================================================================


@pytest.mark.asyncio
async def test_judgement_page_renders_label_fields_before_submit():
    r = await page_get(fake_adapter([m("msg-1", "Bohr", "x\n\nNEXT: Einstein"), esc("msg-2")]))
    assert r.status_code == 200
    html = r.text
    assert 'name="_esc_target" value="msg-2"' in html
    assert html.index('name="_esc_label"') < html.index('class="decision-submit"')
    # 未ラベルが既定 checked、取り消しは current が無いので出ない
    assert re.search(r'value="none"\s+checked', html)
    assert f'value="{el.LABEL_CLEARED}"' not in html


@pytest.mark.asyncio
async def test_judgement_page_has_no_label_fields_when_target_is_relay():
    msgs = [
        m("msg-1412", "Einstein", "x\n\nNEXT: Fermi"),
        m("msg-1413", "pr-gate-relay", f"{STOP_MARKER}\n\nNEXT: human"),
    ]
    r = await page_get(fake_adapter(msgs))
    assert r.status_code == 200
    assert "_esc_label" not in r.text


@pytest.mark.asyncio
async def test_cleared_radio_rendered_only_when_current_label_exists(isolated_label_store):
    msgs = [esc("msg-2")]
    r = await page_get(fake_adapter(msgs))
    assert f'value="{el.LABEL_CLEARED}"' not in r.text
    await isolated_label_store().append({
        "project": PROJECT, "thread_id": THREAD, "target_msg_id": "msg-2",
        "escalated_by": "Einstein", "label": el.LABEL_SHOULD_NOT_ASK,
        "reason_tags": ["impl_detail"], "note": "細部", "snapshot_source": el.SNAPSHOT_ABSENT,
    })
    r = await page_get(fake_adapter(msgs))
    assert f'value="{el.LABEL_CLEARED}"' in r.text
    # current の値が復元される
    assert re.search(rf'value="{el.LABEL_SHOULD_NOT_ASK}"\s+checked', r.text)
    assert re.search(r'value="impl_detail"\s+checked', r.text)
    assert 'value="細部"' in r.text
    # 回答後 (cleared の後) は再び出ない
    await isolated_label_store().append({
        "project": PROJECT, "thread_id": THREAD, "target_msg_id": "msg-2",
        "escalated_by": "Einstein", "label": el.LABEL_CLEARED, "snapshot_source": el.SNAPSHOT_ABSENT,
    })
    r = await page_get(fake_adapter(msgs))
    assert f'value="{el.LABEL_CLEARED}"' not in r.text


@pytest.mark.asyncio
async def test_not_waiting_page_links_to_label_page():
    msgs = [esc("msg-1"), m("msg-2", "human", "A: yes", type="decide"),
            m("msg-3", "Bohr", "x\n\nNEXT: Heisenberg")]
    r = await page_get(fake_adapter(msgs))
    assert r.status_code == 200
    assert f"/dashboard/decisions/{PROJECT}/{THREAD}/label" in r.text


def test_label_wire_values_single_source():
    """テンプレートは定数以外のラベル値 literal を持たない (msg-1401)。"""
    for name in ("partials/escalation_label_fields.html", "escalation_label.html",
                 "decisions_thread.html"):
        text = (SRC / "templates" / name).read_text(encoding="utf-8")
        for value in el.WIRE_VALUES:
            assert f'value="{value}"' not in text, (name, value)
        for value, _ in (*el.TAGS_SHOULD_ASK, *el.TAGS_SHOULD_NOT_ASK):
            assert f'"{value}"' not in text, (name, value)
        code = re.sub(r"\{#.*?#\}", "", text, flags=re.S)  # Jinja コメントは除く
        assert "|safe" not in code.replace(" ", "")


# ===========================================================================
# 判断ページ POST — 回答 → ラベル
# ===========================================================================


@pytest.mark.asyncio
async def test_decision_post_writes_label_after_answer(isolated_label_store, isolated_material_store):
    await put_material(
        isolated_material_store, "msg-2", rules_hash="sha256:r",
        jev={"should_ask_human": 0.66, "rule": "G3", "model_version": "v2"},
    )
    adapter = fake_adapter([m("msg-1", "Bohr", "x\n\nNEXT: Einstein"), esc("msg-2")])
    r = await decision_post(adapter, {
        "_esc_label": el.LABEL_SHOULD_NOT_ASK,
        "_esc_tags": ["proceed_check", "other"],
        "_esc_note": "聞くまでもない",
        "_esc_target": "msg-2",
    })
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/projects/{PROJECT}/threads/{THREAD}"
    adapter.post_message.assert_called_once()
    rows = await history(isolated_label_store, "msg-2")
    assert len(rows) == 1
    row = rows[0]
    assert row["label"] == el.LABEL_SHOULD_NOT_ASK
    assert row["reason_tags"] == ["proceed_check", "other"]
    assert row["note"] == "聞くまでもない"
    assert row["escalated_by"] == "Einstein"
    assert row["snapshot_source"] == el.SNAPSHOT_FRESH
    assert row["rules_hash"] == "sha256:r"
    assert row["jev_p"] == 0.66 and row["jev_rule"] == "G3" and row["jev_model_version"] == "v2"
    assert row["tags_mismatch"] is False and row["target_moved"] is False
    assert row["labeled_at"].endswith("Z")


@pytest.mark.asyncio
async def test_decision_post_unlabeled_does_not_fetch_or_write(isolated_label_store):
    adapter = fake_adapter([esc("msg-2")])
    r = await decision_post(adapter, {"_esc_label": el.LABEL_NONE, "_esc_target": "msg-2"})
    assert r.status_code == 303
    adapter.get_thread.assert_not_called()
    assert await history(isolated_label_store, "msg-2") == []


@pytest.mark.asyncio
async def test_label_written_to_seen_target_with_target_moved_flag(isolated_label_store):
    """人が見ていた msg-2 の後に別の escalation (msg-4) が来ていても、付け先は msg-2。"""
    msgs = [esc("msg-2"), m("msg-3", "Bohr", "x\n\nNEXT: Einstein"), esc("msg-4")]
    r = await decision_post(fake_adapter(msgs), {
        "_esc_label": el.LABEL_BORDERLINE, "_esc_target": "msg-2",
    })
    assert r.status_code == 303
    assert r.headers["location"] == f"/ui/projects/{PROJECT}/threads/{THREAD}"
    rows = await history(isolated_label_store, "msg-2")
    assert len(rows) == 1 and rows[0]["target_moved"] is True
    assert await history(isolated_label_store, "msg-4") == []


@pytest.mark.asyncio
async def test_label_rejected_when_esc_target_not_in_thread(isolated_label_store):
    for target in ("msg-999", "msg-3"):  # 無い id / human が書いた msg
        msgs = [esc("msg-2"), m("msg-3", "human", "A\n\nNEXT: human", type="decide")]
        adapter = fake_adapter(msgs)
        r = await decision_post(adapter, {"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": target})
        adapter.post_message.assert_called_once()  # 回答は成立
        assert r.status_code == 303
        loc = urlparse(r.headers["location"])
        assert loc.path == f"/dashboard/decisions/{PROJECT}/{THREAD}/label"
        assert parse_qs(loc.query)["error"] == [el.ERR_REJECTED]
        assert await history(isolated_label_store, target) == []


@pytest.mark.asyncio
async def test_decision_post_label_unwritten_never_303_to_success(isolated_label_store, monkeypatch):
    """L-1: 書くと判定したのに書けなかった (rejected / unverifiable / store /
    入力不正) ときは、成功の 303 (/ui) を返さない。"""
    success = f"/ui/projects/{PROJECT}/threads/{THREAD}"
    msgs = [esc("msg-2")]
    cases = [
        ({"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-404"}, el.ERR_REJECTED),
        ({"_esc_label": "bogus", "_esc_target": "msg-2"}, el.ERR_INVALID_LABEL),
        ({"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-2",
          "_esc_note": "x" * (el.NOTE_MAX + 1)}, el.ERR_NOTE_TOO_LONG),
    ]
    for form, err in cases:
        r = await decision_post(fake_adapter(msgs), form)
        assert r.status_code == 303 and r.headers["location"] != success
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == [err]
    # unverifiable: 取り直しの get_thread が落ちる (回答の post は成功)
    r = await decision_post(fake_adapter(None), {"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-2"})
    assert r.headers["location"] != success
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == [el.ERR_UNVERIFIABLE]


@pytest.mark.asyncio
async def test_snapshot_store_error_does_not_break_answer_post(monkeypatch):
    class Boom:
        async def history(self, **kw):
            raise RuntimeError("disk gone")

        async def append(self, row):
            raise RuntimeError("disk gone")

    monkeypatch.setattr(el, "_get_label_store", lambda: Boom())
    adapter = fake_adapter([esc("msg-2")])
    r = await decision_post(adapter, {
        "_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-2", "_esc_note": "メモ",
    })
    adapter.post_message.assert_called_once()
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == [el.ERR_STORE]
    assert q["msg"] == ["msg-2"] and q["label"] == [el.LABEL_SHOULD_ASK] and q["note"] == ["メモ"]


@pytest.mark.asyncio
async def test_tags_mismatch_saved_as_entered(isolated_label_store):
    r = await decision_post(fake_adapter([esc("msg-2")]), {
        "_esc_label": el.LABEL_SHOULD_ASK, "_esc_tags": ["impl_detail"], "_esc_target": "msg-2",
    })
    assert r.headers["location"] == f"/ui/projects/{PROJECT}/threads/{THREAD}"
    row = (await history(isolated_label_store, "msg-2"))[0]
    assert row["reason_tags"] == ["impl_detail"] and row["tags_mismatch"] is True


@pytest.mark.asyncio
async def test_d31_rerender_preserves_label_input(isolated_label_store):
    """回答が失敗したら D-31 で再描画し、ラベル入力も保持する (まだ書かない)。"""
    adapter = fake_adapter([esc("msg-2")], post_ok=False)
    r = await decision_post(adapter, {
        "_esc_label": el.LABEL_SHOULD_NOT_ASK, "_esc_tags": ["impl_detail"],
        "_esc_note": "保持して", "_esc_target": "msg-2",
    })
    assert r.status_code == 400
    assert re.search(rf'value="{el.LABEL_SHOULD_NOT_ASK}"\s+checked', r.text)
    assert re.search(r'value="impl_detail"\s+checked', r.text)
    assert 'value="保持して"' in r.text
    assert await history(isolated_label_store, "msg-2") == []


# ===========================================================================
# ラベル画面 (§3.4)
# ===========================================================================


@pytest.mark.asyncio
async def test_label_page_get_found_shows_form_and_history(isolated_label_store):
    msgs = [esc("msg-2"), m("msg-3", "human", "A", type="decide")]
    r = await label_get(fake_adapter(msgs), "?msg=msg-2")
    assert r.status_code == 200
    assert 'name="_esc_target" value="msg-2"' in r.text


@pytest.mark.asyncio
async def test_label_page_get_without_msg_uses_latest_labeled_then_resolver(isolated_label_store):
    msgs = [esc("msg-2")]
    r = await label_get(fake_adapter(msgs))
    assert 'name="_esc_target" value="msg-2"' in r.text


@pytest.mark.asyncio
async def test_label_page_get_carries_error_and_input_back():
    r = await label_get(
        fake_adapter([esc("msg-2")]),
        f"?msg=msg-2&error={el.ERR_STORE}&label={el.LABEL_BORDERLINE}&tags=other&note=hello",
    )
    assert "保存できませんでした" in r.text
    assert re.search(rf'value="{el.LABEL_BORDERLINE}"\s+checked', r.text)
    assert re.search(r'value="other"\s+checked', r.text)
    assert 'value="hello"' in r.text


@pytest.mark.asyncio
async def test_label_page_post_writes_and_303s_to_self(isolated_label_store):
    msgs = [esc("msg-2"), m("msg-3", "human", "A", type="decide")]
    r = await label_post(fake_adapter(msgs), {
        "_esc_label": el.LABEL_SHOULD_ASK, "_esc_tags": ["rule_3"], "_esc_target": "msg-2",
    })
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    assert loc.path == f"/dashboard/decisions/{PROJECT}/{THREAD}/label"
    assert parse_qs(loc.query)["msg"] == ["msg-2"]
    rows = await history(isolated_label_store, "msg-2")
    assert [r_["label"] for r_ in rows] == [el.LABEL_SHOULD_ASK]
    assert rows[0]["target_moved"] is False


@pytest.mark.asyncio
async def test_label_page_post_unverifiable_target_rerenders_400_with_input(isolated_label_store):
    # msg_count が窓より大きく、target が窓の外 → unverifiable
    adapter = fake_adapter([esc("msg-90")], msg_count=200)
    r = await label_post(adapter, {
        "_esc_label": el.LABEL_SHOULD_ASK, "_esc_note": "入力", "_esc_target": "msg-2",
    })
    assert r.status_code == 400
    assert el.ERROR_MESSAGES[el.ERR_UNVERIFIABLE] in r.text
    assert await history(isolated_label_store, "msg-2") == []


@pytest.mark.asyncio
async def test_label_page_post_human_authored_target_rejected_400(isolated_label_store):
    msgs = [esc("msg-2"), m("msg-3", "human", "A\n\nNEXT: human", type="decide")]
    r = await label_post(fake_adapter(msgs), {
        "_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-3",
    })
    assert r.status_code == 400
    assert el.ERROR_MESSAGES[el.ERR_REJECTED] in r.text
    assert await history(isolated_label_store, "msg-3") == []


@pytest.mark.asyncio
async def test_label_page_post_note_too_long_keeps_input(isolated_label_store):
    note = "あ" * (el.NOTE_MAX + 1)
    r = await label_post(fake_adapter([esc("msg-2")]), {
        "_esc_label": el.LABEL_SHOULD_ASK, "_esc_note": note, "_esc_target": "msg-2",
    })
    assert r.status_code == 400
    assert f'value="{note}"' in r.text


@pytest.mark.asyncio
async def test_label_page_clear_inserts_cleared_row(isolated_label_store):
    msgs = [esc("msg-2")]
    await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-2"})
    r = await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_CLEARED, "_esc_target": "msg-2"})
    assert r.status_code == 303
    rows = await history(isolated_label_store, "msg-2")
    assert [x["label"] for x in rows] == [el.LABEL_SHOULD_ASK, el.LABEL_CLEARED]
    assert rows[-1]["reason_tags"] == [] and rows[-1]["note"] is None
    assert el._current(rows) is None  # 未ラベルとして返る


@pytest.mark.asyncio
async def test_clear_without_current_label_is_noop(isolated_label_store):
    r = await label_post(fake_adapter([esc("msg-2")]), {"_esc_label": el.LABEL_CLEARED, "_esc_target": "msg-2"})
    assert r.status_code == 303
    assert await history(isolated_label_store, "msg-2") == []


@pytest.mark.asyncio
async def test_unchanged_resubmit_is_noop_no_duplicate_row(isolated_label_store):
    msgs = [esc("msg-2")]
    form = {"_esc_label": el.LABEL_SHOULD_ASK, "_esc_tags": ["rule_1"], "_esc_note": "n", "_esc_target": "msg-2"}
    await label_post(fake_adapter(msgs), form)
    r = await label_post(fake_adapter(msgs), form)
    assert r.status_code == 303
    assert len(await history(isolated_label_store, "msg-2")) == 1


@pytest.mark.asyncio
async def test_relabel_after_material_replaced_inherits_fresh_snapshot(isolated_label_store, isolated_material_store):
    msgs = [esc("msg-2")]
    await put_material(isolated_material_store, "msg-2", rules_hash="h-old",
                       jev={"should_ask_human": 0.3, "rule": "r", "model_version": "v"})
    await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-2"})
    await put_material(isolated_material_store, "msg-9", rules_hash="h-new")  # 次の escalation
    await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_BORDERLINE, "_esc_note": "直す", "_esc_target": "msg-2"})
    rows = await history(isolated_label_store, "msg-2")
    assert len(rows) == 2
    assert rows[1]["snapshot_source"] == el.SNAPSHOT_FRESH
    assert rows[1]["rules_hash"] == "h-old" and rows[1]["jev_p"] == 0.3


@pytest.mark.asyncio
async def test_cleared_row_inherits_snapshot(isolated_label_store, isolated_material_store):
    msgs = [esc("msg-2")]
    await put_material(isolated_material_store, "msg-2", rules_hash="h1")
    await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-2"})
    await put_material(isolated_material_store, "msg-9")
    await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_CLEARED, "_esc_target": "msg-2"})
    rows = await history(isolated_label_store, "msg-2")
    assert rows[-1]["label"] == el.LABEL_CLEARED
    assert rows[-1]["snapshot_source"] == el.SNAPSHOT_FRESH and rows[-1]["rules_hash"] == "h1"


@pytest.mark.asyncio
async def test_snapshot_upgrades_when_first_label_was_mismatch_and_material_now_fresh(isolated_label_store, isolated_material_store):
    msgs = [esc("msg-2")]
    await put_material(isolated_material_store, "msg-1")
    await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_SHOULD_ASK, "_esc_target": "msg-2"})
    await put_material(isolated_material_store, "msg-2", rules_hash="h2")
    await label_post(fake_adapter(msgs), {"_esc_label": el.LABEL_BORDERLINE, "_esc_target": "msg-2"})
    rows = await history(isolated_label_store, "msg-2")
    assert rows[0]["snapshot_source"] == el.SNAPSHOT_MISMATCH and rows[0]["rules_hash"] is None
    assert rows[1]["snapshot_source"] == el.SNAPSHOT_FRESH and rows[1]["rules_hash"] == "h2"


# ===========================================================================
# material PUT の rules_hash (§2 単位 A)
# ===========================================================================


@pytest.mark.asyncio
async def test_material_put_accepts_optional_rules_hash():
    path = f"/v1/decisions/{PROJECT}/{THREAD}/material"
    r = await _request("PUT", path, json={"head_msg_id": "msg-1", "rules_hash": "sha256:x"})
    assert r.status_code == 200
    r = await _request("GET", path)
    assert r.json()["rules_hash"] == "sha256:x"
    r = await _request("PUT", path, json={"head_msg_id": "msg-2"})
    assert r.status_code == 200
    assert (await _request("GET", path)).json()["rules_hash"] is None
    r = await _request("PUT", path, json={"head_msg_id": "msg-3", "rules_hash": 5})
    assert r.status_code == 400


# ===========================================================================
# §4 export / summary
# ===========================================================================


async def _seed(store_factory):
    s = store_factory()
    base = {"project": PROJECT, "thread_id": THREAD, "snapshot_source": el.SNAPSHOT_ABSENT}
    await s.append({**base, "target_msg_id": "msg-1", "escalated_by": "Einstein",
                    "label": el.LABEL_SHOULD_ASK, "reason_tags": ["rule_1"]})
    await s.append({**base, "target_msg_id": "msg-1", "escalated_by": "Einstein",
                    "label": el.LABEL_SHOULD_NOT_ASK, "reason_tags": ["impl_detail", "other"],
                    "rules_hash": "h1", "tags_mismatch": False})
    await s.append({**base, "target_msg_id": "msg-2", "escalated_by": "Bohr",
                    "label": el.LABEL_SHOULD_ASK, "reason_tags": ["impl_detail"],
                    "tags_mismatch": True, "target_moved": True})
    await s.append({**base, "target_msg_id": "msg-3", "escalated_by": "Bohr",
                    "label": el.LABEL_BORDERLINE})
    await s.append({**base, "target_msg_id": "msg-3", "escalated_by": "Bohr",
                    "label": el.LABEL_CLEARED})
    await s.append({**base, "project": "other", "target_msg_id": "msg-1",
                    "escalated_by": "Fermi", "label": el.LABEL_BORDERLINE})


@pytest.mark.asyncio
async def test_export_current_and_history(isolated_label_store):
    await _seed(isolated_label_store)
    r = await _request("GET", f"/v1/escalation-labels?project={PROJECT}")
    body = r.json()
    assert r.status_code == 200 and body["history"] is False
    assert [(x["target_msg_id"], x["label"]) for x in body["rows"]] == [
        ("msg-1", el.LABEL_SHOULD_NOT_ASK),
        ("msg-2", el.LABEL_SHOULD_ASK),
        ("msg-3", el.LABEL_CLEARED),
    ]
    row = body["rows"][0]
    for key in ("target_msg_id", "escalated_by", "reason_tags", "note", "rules_hash",
                "jev_p", "jev_rule", "jev_model_version", "snapshot_source",
                "tags_mismatch", "target_moved", "labeled_at"):
        assert key in row
    r = await _request("GET", f"/v1/escalation-labels?project={PROJECT}&history=1")
    assert r.json()["count"] == 5
    r = await _request("GET", "/v1/escalation-labels")
    assert r.json()["count"] == 4
    assert (await _request("GET", "/v1/escalation-labels?history=2")).status_code == 400
    r = await _request("GET", "/v1/escalation-labels?since=2999-01-01T00:00:00Z")
    assert r.json()["count"] == 0


@pytest.mark.asyncio
async def test_summary_axes(isolated_label_store):
    await _seed(isolated_label_store)
    r = await _request("GET", f"/v1/escalation-labels/summary?project={PROJECT}")
    s = r.json()
    assert s["total"] == 3
    assert s["by_label"] == {el.LABEL_SHOULD_NOT_ASK: 1, el.LABEL_SHOULD_ASK: 1, el.LABEL_CLEARED: 1}
    assert s["by_tag"] == {"impl_detail": 2, "other": 1}
    assert s["by_escalated_by"] == {"Einstein": 1, "Bohr": 1}
    assert s["by_rules_hash"] == {"h1": 1, "null": 1}
    assert s["tags_mismatch"] == 1 and s["target_moved"] == 1


def test_export_docstrings_state_no_auth_and_section_0():
    for fn in (el.export_escalation_labels, el.summarize_escalation_labels):
        doc = fn.__doc__ or ""
        assert "No auth here" in doc and "tailnet" in doc
        assert "Decider" in doc


def test_routes_registered_to_this_module():
    app = create_app()
    base = "/dashboard/decisions/{project}/{thread_id}/label"
    assert sole_handler(app, "GET", base) == "magickit.web.escalation_labels.label_page"
    assert sole_handler(app, "POST", base) == "magickit.web.escalation_labels.label_page_post"
    assert sole_handler(app, "GET", "/v1/escalation-labels") == (
        "magickit.web.escalation_labels.export_escalation_labels"
    )
    assert sole_handler(app, "GET", "/v1/escalation-labels/summary") == (
        "magickit.web.escalation_labels.summarize_escalation_labels"
    )


def test_chatroom_writes_carries_no_consumer_literal():
    """msg-072 §1: chatroom_writes.py に ``NEXT:`` 相当の literal を持ち込まない。"""
    text = (SRC / "web" / "chatroom_writes.py").read_text(encoding="utf-8")
    assert "NEXT: human" not in text
