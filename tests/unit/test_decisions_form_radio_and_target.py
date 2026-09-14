"""Tests for the D-35 radio+submit split and the I-19/D-36/D-37/D-38
registered-target filter (msg-146).

Scope:

- **D-35 (choice / submit separation)**: the judgement page's choice
  cards are ``<input type="radio">`` inside ``<label>``, and the single
  ``<button type="submit">送信</button>`` is the only element that
  actually submits. Selecting a radio must NOT POST on its own; the
  human types free-text and then presses submit (msg-139 tap defect).
- **D-31 exhaustive fallback (Einstein msg-147 §3)**: on re-render,
  when the previously-submitted ``content`` does not appear in the
  current option set, the ``checked`` attribute lands on the I-12
  sentinel radio so the next submit does not trip the missing-``content``
  422.
- **I-19 / D-36 (msg-140 root cause / msg-146 §3)**: the select is
  populated by ``_participant_choices_registered`` — no ``pr-gate-relay``
  denylist, no denylist at all; the registry decides.
- **D-37 (default demotion)**: when the parked author fails the
  registry check, the default target is "宛先を送らない", NOT ``human``.
- **D-38 (fail-closed on Prismind outage)**: an UNKNOWN verdict drops
  the candidate rather than "let it through". The template surfaces a
  1-line notice when at least one drop happened.
- **I-20 (no target ⇒ body must carry NEXT:)**: the handler rejects a
  POST that carries an empty ``next_participant`` and no standalone
  ``NEXT:`` line in the composed body.
- **I-21 (no ``<a>`` inside ``<label>``)**: pinned as a template lint —
  entering the choice card via a link must not disturb the radio state
  the human already picked.

Where a test hits Prismind, it uses ``patch.object(chatroom_tools,
"_lookup_identity", ...)`` explicitly. The autouse ``stub_identity_registry``
fixture (see conftest) supplies a "registered by default" verdict for
tests that do not care about the filter.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.main import create_app
from magickit.web import decisions as decision_page


PROJECT = "spirrow-magickit"
THREAD = "T-x"


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


def _passing_gate(role: str | None = None):
    return AsyncMock(return_value=chatroom_tools._RoleDecision(error=None, role=role))


def _lookup(*, found: bool, unavailable_reason: str | None = None):
    return chatroom_tools._IdentityLookup(
        unavailable_reason=unavailable_reason,
        found=found,
        allowed_roles=(),
    )


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


async def _post(path: str, data: dict[str, str]) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        return await client.post(path, data=data)


# --- D-35: radio + single submit -----------------------------------------


@pytest.mark.asyncio
async def test_d35_choice_cards_are_radio_not_submit_buttons():
    """★ msg-139 実機欠陥修正: 選択肢カードは ``<button type="submit">`` では
    なく ``<input type="radio">`` である。押した瞬間に POST されない。"""
    adapter = _adapter_returning({
        "thread": {"title": "T-d35", "status": "active"},
        "messages": [{"author": "Bohr", "content": "please decide",
                       "next_participant": "human"}],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # No <button type="submit" name="content"> in the whole page — that
    # was the msg-139 defect shape.
    assert 'type="submit" name="content"' not in r.text
    # The I-12 sentinel is now a radio, not a submit button.
    assert 'type="radio" name="content" value="(自由記述のみ)"' in r.text
    # A single explicit submit button exists (no name= so no extra field).
    assert '<button type="submit" class="decision-submit">送信</button>' in r.text


@pytest.mark.asyncio
async def test_d35_i12_sentinel_radio_is_checked_by_default_on_fresh_render():
    """spec §3.1a (422 罠回避): 既定 checked の radio が常に 1 つある形。

    Fresh render (no D-31 error) → the sentinel radio carries ``checked``
    so ``<form>`` will always send a non-empty ``content=`` at submit.
    """
    adapter = _adapter_returning({
        "thread": {"title": "T-d35-checked", "status": "active"},
        "messages": [{"author": "Bohr", "content": "please decide",
                       "next_participant": "human"}],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # The sentinel radio is checked (order of attributes: value then checked).
    assert 'value="(自由記述のみ)"' in r.text
    # A checked attribute exists on the sentinel radio. Match tolerantly on
    # attribute order.
    sentinel_marker = 'value="(自由記述のみ)"'
    idx = r.text.find(sentinel_marker)
    assert idx != -1
    # Look ahead in the same tag for a ``checked`` attribute (Jinja emits it
    # after the value on the sentinel radio when checked_choice_value ==
    # freeform_only_value).
    tail = r.text[idx : idx + 400]
    assert "checked" in tail, tail



def _adapter_for_thread(*, last_msg_id: str = "msg-1") -> AsyncMock:
    """J-fresh を作る最小のスレッド (駐機 msg は human 宛)。"""
    return _adapter_returning({
        "thread": {
            "title": "T-table", "last_msg_id": last_msg_id, "status": "active",
        },
        "messages": [{
            "author": "Bohr", "content": "please decide",
            "next_participant": "human", "msg_id": last_msg_id,
        }],
        "mode": "full",
    })


@pytest.mark.asyncio
async def test_d35_choice_option_radios_carry_option_value(
    isolated_material_store,
):
    """J-fresh: composer 由来の option カードも radio になっている。
    value は spec §4.1 の ``f"{id}: {label}"``。"""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-1",
        signature=None,
        question="which one?",
        options=[
            {"id": "A", "label": "そのまま進める", "gain": "早い", "loss": "リスク"},
            {"id": "B", "label": "巻き戻す", "gain": "安全", "loss": "遅い"},
        ],
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    adapter = _adapter_returning({
        "thread": {"title": "T-d35-fresh", "last_msg_id": "msg-1", "status": "active"},
        "messages": [{
            "author": "Bohr", "content": "please decide",
            "next_participant": "human",
            "msg_id": "msg-1",
        }],
        "mode": "full",
    })
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert 'type="radio" name="content" value="A: そのまま進める"' in r.text
    assert 'type="radio" name="content" value="B: 巻き戻す"' in r.text
    # And the sentinel radio is still there for I-12.
    assert 'type="radio" name="content" value="(自由記述のみ)"' in r.text


# --- D-31 exhaustive fallback (Einstein msg-147 §3) ----------------------


def test_d31_pick_checked_choice_returns_content_value_when_matched():
    """Previous ``content`` matches one of the current option values →
    that option gets ``checked`` (input preserved)."""
    opts = [{"value": "A: yes"}, {"value": "B: no"}]
    assert decision_page._pick_checked_choice("A: yes", opts) == "A: yes"


def test_d31_pick_checked_choice_falls_back_to_sentinel_when_no_match():
    """★ Einstein §3 の要件: マッチしないなら sentinel に落とす。

    Options changed between submit and re-render (material was updated
    concurrently, or a garbage content string arrived somehow). The
    template must not render an empty radio group — force the sentinel.
    """
    opts = [{"value": "A: yes"}, {"value": "B: no"}]
    assert (
        decision_page._pick_checked_choice("Z: garbage", opts)
        == decision_page._FREEFORM_ONLY_VALUE
    )


def test_d31_pick_checked_choice_empty_content_falls_back_to_sentinel():
    """Empty content on re-render (no radio was checked when the error
    fired) → sentinel. Same reason: never emit no-checked radios."""
    assert (
        decision_page._pick_checked_choice("", [{"value": "A"}])
        == decision_page._FREEFORM_ONLY_VALUE
    )


def test_d31_pick_checked_choice_no_options_falls_back_to_sentinel():
    """J-stale / J-absent: choice_options is empty. Sentinel is the only
    radio ∴ it must be the checked one."""
    assert (
        decision_page._pick_checked_choice("A: yes", [])
        == decision_page._FREEFORM_ONLY_VALUE
    )


@pytest.mark.asyncio
async def test_d31_rerender_after_bad_target_checks_matching_option(
    isolated_material_store,
):
    """D-31 error re-render pins the user's picked option back onto the
    right radio. Requires the ``_pick_checked_choice`` value to end up
    in the template's ``checked`` slot on the matching option."""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-9",
        signature=None,
        question="which one?",
        options=[
            {"id": "A", "label": "そのまま", "gain": "", "loss": ""},
            {"id": "B", "label": "戻す", "gain": "", "loss": ""},
        ],
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    envelope = {
        "error_type": "NextParticipantUnknownError",
        "error": "typo",
        "details": {"next_participant": "typoName"},
    }
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value={
        "thread": {"title": "T", "last_msg_id": "msg-9", "status": "active"},
        "messages": [{
            "author": "Bohr", "content": "please decide",
            "next_participant": "human", "msg_id": "msg-9",
        }],
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
                "content": "A: そのまま",  # picked A
                "_freeform": "reason text",
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    # A radio carries the user's choice + is checked on re-render.
    marker = 'value="A: そのまま"'
    idx = r.text.find(marker)
    assert idx != -1
    tail = r.text[idx : idx + 400]
    assert "checked" in tail, tail
    # And the freeform text is preserved.
    assert "reason text" in r.text


@pytest.mark.asyncio
async def test_d31_rerender_when_option_disappeared_falls_back_to_sentinel(
    isolated_material_store,
):
    """★ Einstein msg-147 §3 の想定シナリオ: submit と re-render の間で
    material が更新され, 前 submit の content 値が現行 option 集合に
    無くなった場合。sentinel radio に checked が入り, 次の submit で
    422 に落ちないことを pin。
    """
    store = isolated_material_store()
    # New material: options are now C/D (no A/B).
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-99",
        signature=None,
        question="which one now?",
        options=[
            {"id": "C", "label": "第三案", "gain": "", "loss": ""},
            {"id": "D", "label": "第四案", "gain": "", "loss": ""},
        ],
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    envelope = {
        "error_type": "NextParticipantUnknownError",
        "error": "typo",
        "details": {},
    }
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value={
        "thread": {"title": "T", "last_msg_id": "msg-99", "status": "active"},
        "messages": [{
            "author": "Bohr", "content": "please decide",
            "next_participant": "human", "msg_id": "msg-99",
        }],
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
                "content": "A: そのまま",  # A no longer exists
                "_freeform": "supplementary text",
                "next_participant": "typoName",
            },
        )

    assert r.status_code == 400
    # The sentinel radio must be the one with ``checked`` (fallback).
    sentinel = 'value="(自由記述のみ)"'
    idx = r.text.find(sentinel)
    assert idx != -1
    tail = r.text[idx : idx + 400]
    assert "checked" in tail
    # And critically no C/D radio is pre-selected.
    for other in ('value="C: 第三案"', 'value="D: 第四案"'):
        other_idx = r.text.find(other)
        assert other_idx != -1
        assert "checked" not in r.text[other_idx : other_idx + 200]


# --- I-19 / D-36 (registered-target filter) ------------------------------


@pytest.mark.asyncio
async def test_d36_pr_gate_relay_is_absent_from_the_select():
    """★ msg-140 の欠陥修正: ``pr-gate-relay`` が select に出ない。

    A-24 (msg-146 §5): live 判断ページで pr-gate-relay を持つスレッドを
    取得し, option を全部数えて含まれていないことを confirm。unit 版は
    per-name verdict を ``_lookup_identity`` にセットして測る。
    """
    # Simulate a thread whose distinct authors include the orchestrator.
    payload = {
        "thread": {"title": "T-d36", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "propose", "next_participant": "human"},
            {"author": "pr-gate-relay", "content": "review posted",
             "next_participant": "human"},
        ],
        "mode": "full",
    }
    adapter = _adapter_returning(payload)

    async def per_name(name: str, **_):
        # Bohr and human are registered; pr-gate-relay is not.
        if name == "pr-gate-relay":
            return _lookup(found=False)
        return _lookup(found=True)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(decision_page, "_resolve_identity", side_effect=per_name),
    ):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # The reserved / orchestrator identity must not appear in any option.
    assert 'value="pr-gate-relay"' not in r.text
    # But real identities do.
    assert 'value="Bohr"' in r.text
    assert 'value="human"' in r.text


@pytest.mark.asyncio
async def test_d36_uses_shared_lookup_identity_not_a_new_registry(monkeypatch):
    """I-19: the filter must call ``chatroom_tools._lookup_identity`` —
    the exact function the POST-time gate calls. A second implementation
    would drift (msg-140 §4 pattern).

    Two independent handles catch a rename / re-implementation:
    * **Structural**: ``_resolve_identity`` is a thin wrapper that
      references ``chatroom_tools._lookup_identity`` — verified by source
      inspection so a re-write that adds a second registry surface
      (Prismind alternative / local cache with its own contract) is
      caught in code review.
    * **Runtime**: with the wrapper restored to its real body (bypassing
      the autouse stub), a spy on ``chatroom_tools._lookup_identity``
      receives the exact candidate names.
    """
    # Verify the wrapper structure — the source file (not the module
    # attribute, which the autouse fixture replaces) references the
    # shared registry function. Reading the file rather than
    # ``inspect.getsource(decision_page._resolve_identity)`` because the
    # autouse ``stub_decision_identity_lookup`` has already replaced the
    # module attribute by the time this test runs.
    from pathlib import Path
    src = Path("src/magickit/web/decisions.py").read_text(encoding="utf-8")
    assert "async def _resolve_identity" in src
    # Within a small window after the definition, the body must delegate
    # to chatroom_tools._lookup_identity — a second registry surface
    # would fail this pin.
    def_idx = src.find("async def _resolve_identity")
    window = src[def_idx : def_idx + 800]
    assert "chatroom_tools._lookup_identity" in window, window

    # Restore the real wrapper for this test (undo autouse stub) so the
    # runtime spy on ``chatroom_tools._lookup_identity`` sees the calls.
    async def _real_resolve(name: str):
        return await chatroom_tools._lookup_identity(name)

    monkeypatch.setattr(decision_page, "_resolve_identity", _real_resolve)

    payload = {
        "thread": {"title": "T-i19", "status": "active"},
        "messages": [{"author": "Bohr", "content": "propose",
                       "next_participant": "human"}],
        "mode": "full",
    }
    adapter = _adapter_returning(payload)
    spy = AsyncMock(return_value=_lookup(found=True))
    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(chatroom_tools, "_lookup_identity", spy),
    ):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")
    assert r.status_code == 200
    # The exact function was called (Bohr + human = 2 lookups minimum).
    called_names = {c.args[0] for c in spy.await_args_list}
    assert "Bohr" in called_names
    assert "human" in called_names


# --- D-37: default demotion when parked author is not registered --------





@pytest.mark.asyncio
async def test_d37_default_is_no_target_when_parked_author_is_pr_gate_relay():
    """End-to-end: a thread whose parked author is ``pr-gate-relay`` renders
    the select with "宛先を送らない" selected (not ``human``, not
    ``pr-gate-relay``). Direct fixture for the msg-131 case."""
    payload = {
        "thread": {"title": "T-d37", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "propose", "next_participant": "human"},
            {"author": "pr-gate-relay", "content": "review posted",
             "next_participant": "human"},
        ],
        "mode": "full",
    }
    adapter = _adapter_returning(payload)

    async def per_name(name: str, **_):
        if name == "pr-gate-relay":
            return _lookup(found=False)
        return _lookup(found=True)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(decision_page, "_resolve_identity", side_effect=per_name),
    ):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # The "no target" option is present and is the ``selected`` one.
    marker = 'value=""'
    # There should be an option with value="" and it should be selected.
    # Look for the specific option element.
    assert '<option value=""' in r.text
    # The "no target" option carries the ``selected`` attribute.
    idx = r.text.find(decision_page.NO_TARGET_LABEL)
    assert idx != -1
    # Look back for the ``<option`` tag and verify ``selected`` is between.
    open_idx = r.text.rfind("<option", 0, idx)
    assert open_idx != -1
    assert "selected" in r.text[open_idx:idx]


# --- D-37 (the *line*): Prismind is UP, parked author is UNREGISTERED ----
# Bohr msg-155 §5 gap: the D-37 default-demotion tests above check the
# *selected value* but never that the user is told WHY. The 1-line reason
# was frozen in msg-146 §3 ("理由を画面に 1 行出す") and the earlier round
# of A-24 fixtures did not touch it (the fixture's parked author was not
# pr-gate-relay ∴ D-37 never fired). These tests close that gap 0-tap.







@pytest.mark.asyncio
async def test_d37_line_stays_silent_when_parked_author_is_registered():
    """Negative side of the pin: no D-37 line when the parked author IS
    registered. A false-positive here would make the page shout at users
    for a normal handoff — much worse than staying silent."""
    payload = {
        "thread": {"title": "T-d37-silent", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "propose", "next_participant": "human"},
        ],
        "mode": "full",
    }
    adapter = _adapter_returning(payload)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(
            decision_page, "_resolve_identity",
            AsyncMock(return_value=_lookup(found=True)),
        ),
    ):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # No D-37 markup, no D-37 copy.
    assert 'class="decision-parked-unregistered"' not in r.text
    assert "登録済 identity ではありません" not in r.text


@pytest.mark.asyncio
async def test_d37_line_stays_silent_when_prismind_is_unavailable():
    """★ D-37 vs D-38 orthogonality end-to-end: on a Prismind outage the
    D-38 "verification unavailable" line is what fires. The D-37 line
    would be a false accusation (we did not measure anything for the
    parked author) ∴ MUST NOT appear even when the default landed on
    NO_TARGET for reasons that look adjacent.
    """
    payload = {
        "thread": {"title": "T-d37-vs-d38", "status": "active"},
        "messages": [
            {"author": "pr-gate-relay", "content": "x", "next_participant": "human"},
        ],
        "mode": "full",
    }
    adapter = _adapter_returning(payload)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(
            decision_page, "_resolve_identity",
            AsyncMock(return_value=_lookup(found=False, unavailable_reason="down")),
        ),
    ):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # D-38 line: yes.
    assert "宛先候補を検証できませんでした" in r.text
    # D-37 line: no (we did not measure).
    assert 'class="decision-parked-unregistered"' not in r.text
    assert "登録済 identity ではありません" not in r.text


# --- D-38: fail-closed on UNKNOWN verdict --------------------------------


@pytest.mark.asyncio
async def test_d38_unknown_verdicts_drop_candidate_and_flag_verify_unavailable():
    """UNKNOWN (Prismind outage / timeout) → candidate is dropped and
    the template renders the "verification unavailable" notice."""
    payload = {
        "thread": {"title": "T-d38", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "propose", "next_participant": "human"},
            {"author": "Heisenberg", "content": "reply",
             "next_participant": "human"},
        ],
        "mode": "full",
    }
    adapter = _adapter_returning(payload)

    async def per_name(name: str, **_):
        if name == "Heisenberg":
            return _lookup(found=False, unavailable_reason="prismind down")
        return _lookup(found=True)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(decision_page, "_resolve_identity", side_effect=per_name),
    ):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # Heisenberg was UNKNOWN → dropped.
    assert 'value="Heisenberg"' not in r.text
    # Registered ones remain.
    assert 'value="Bohr"' in r.text
    # The degradation notice fired.
    assert "宛先候補を検証できませんでした" in r.text


@pytest.mark.asyncio
async def test_d38_all_unknown_reduces_select_to_no_target_only():
    """Extreme case (whole Prismind outage): every candidate is UNKNOWN
    ∴ dropped. The select has only the "宛先を送らない" option, and the
    notice fires."""
    payload = {
        "thread": {"title": "T-d38-all", "status": "active"},
        "messages": [
            {"author": "Bohr", "content": "propose", "next_participant": "human"},
        ],
        "mode": "full",
    }
    adapter = _adapter_returning(payload)

    with (
        patch.object(chatroom_tools, "_adapter", return_value=adapter),
        patch.object(
            decision_page, "_resolve_identity",
            AsyncMock(return_value=_lookup(found=False, unavailable_reason="down")),
        ),
    ):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # No candidate ``<option>`` remains — the select body contains only
    # the no-target option. Assert on the option markup specifically so
    # the hidden ``author=human`` input does not confuse the check.
    assert '<option value="Bohr"' not in r.text
    assert '<option value="human"' not in r.text
    assert '<option value=""' in r.text  # the no-target option
    assert "宛先候補を検証できませんでした" in r.text


@pytest.mark.asyncio
async def test_d38_per_lookup_timeout_is_bounded_and_yields_unknown():
    """★ Prismind が遅い日でも判断ページは開ける — 1 lookup が
    ``_LOOKUP_TIMEOUT_S`` を超えたら UNKNOWN として扱う (D-38 の実体)。"""

    async def slow(name: str, **_):
        # Deliberately slower than the per-lookup budget.
        await asyncio.sleep(decision_page._LOOKUP_TIMEOUT_S + 0.5)
        return _lookup(found=True)

    with patch.object(decision_page, "_resolve_identity", side_effect=slow):
        # Deadline just past a single lookup so the second call cannot fit.
        import time as _time
        deadline = _time.monotonic() + decision_page._LOOKUP_TIMEOUT_S + 0.1
        verdict = await decision_page._lookup_one_with_budget("Bohr", deadline)
        # Even though the lookup would eventually say "registered", we time
        # it out.
        assert verdict == decision_page._LookupVerdict.UNKNOWN


@pytest.mark.asyncio
async def test_d38_lookup_raise_yields_unknown_not_registered_or_unregistered():
    """Adapter transport errors (connection refused, malformed schema) →
    UNKNOWN. The exception is swallowed at the boundary (log + drop).
    Regression: a permissive fallback here would silently disarm D-38."""

    async def boom(name: str, **_):
        raise RuntimeError("prismind connection refused")

    import time as _time
    with patch.object(decision_page, "_resolve_identity", side_effect=boom):
        deadline = _time.monotonic() + 10
        verdict = await decision_page._lookup_one_with_budget("Bohr", deadline)
        assert verdict == decision_page._LookupVerdict.UNKNOWN


# --- I-20: no target ⇒ body must carry NEXT: -----------------------------


@pytest.mark.asyncio
async def test_i20_no_target_and_no_body_next_line_is_rejected():
    """Already pinned in ``test_i20_all_empty_next_participant_and_empty_body_is_rejected``;
    this test asserts the same rule from the other side: a body with
    substantive freeform text but no standalone ``NEXT:`` line still
    triggers I-20 when the select is set to "宛先を送らない"."""
    adapter = AsyncMock()
    adapter.post_message = AsyncMock(
        return_value={"msg": {"msg_id": "unused", "type": "decide"}}
    )
    adapter.get_thread = AsyncMock(return_value={
        "thread": {"title": "T", "status": "active"},
        "messages": [{"author": "Bohr", "content": "please decide",
                       "next_participant": "human"}],
        "mode": "full",
    })
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
                "_freeform": "just prose, no directive here",
                "next_participant": "",
            },
        )

    assert r.status_code == 400
    adapter.post_message.assert_not_called()
    # Error message names both remedies (select or body).
    assert "select" in r.text or "宛先" in r.text
    assert "NEXT:" in r.text


# --- I-21: no <a> inside a decision-choice <label> -----------------------


def test_i21_no_anchor_tag_inside_decision_choice_label():
    """★ Template lint (I-21, msg-146 §2): decision-choice ``<label>`` の
    内側に ``<a>`` を置かない。カード全体が label である以上, ``<a>`` を
    踏むとタップがラベル選択に吸われて "リンクを踏んだつもりで選択が変わる"。

    A template rewrite that later adds an anchor inside the choice cards
    (e.g. an auto-linkified URL from composer material) will trip this
    lint and force a UX conversation instead of shipping the trap.
    """
    from pathlib import Path
    import re

    src = Path("src/magickit/templates/decisions_thread.html").read_text(
        encoding="utf-8"
    )
    # A crude but effective scan: every ``<label class="decision-choice"...``
    # block up to its closing ``</label>`` must not contain an ``<a`` tag.
    label_pattern = re.compile(
        r'<label[^>]*class="[^"]*decision-choice[^"]*"[^>]*>(.*?)</label>',
        re.DOTALL,
    )
    for match in label_pattern.finditer(src):
        body = match.group(1)
        assert "<a " not in body and "<a>" not in body, (
            "Decision-choice label contains an anchor tag (I-21 forbid). "
            "Move the link outside the label."
        )


# --- I-20 shared helper visible from decisions module --------------------


def test_no_target_value_and_label_are_defined_on_decisions_module():
    """A rename that leaves the template referring to a symbol the
    handler no longer exports would 500 on render. This pin makes the
    contract explicit."""
    assert decision_page.NO_TARGET_VALUE == ""
    assert isinstance(decision_page.NO_TARGET_LABEL, str)
    assert decision_page.NO_TARGET_LABEL  # non-empty


# ---------------------------------------------------------------------------
# 宛先候補は設定から (2026-09-14)
# ---------------------------------------------------------------------------
#
# 以前は「スレッドの発言者 + human」だった。実測でそこには pr-gate-relay /
# orchestrator / operator-lane が混ざり、人の判断を機械側の役に手渡せた。逆に
# Fermi はどのスレッドでも発言しないので永久に選べなかった。カードが人に回った
# 時点でその人は誰にでも回せる ∴ 出どころはスレッドの状態ではなく設定。
#
# これに伴い D-37（駐機著者を既定にし、置けないときだけ降格する）を撤去した。
# 既定は常に sentinel ＝ 降格が起きない ∴ 降格の理由行も要らない。


def test_candidates_come_from_settings_not_from_who_spoke(monkeypatch):
    """発言者は候補にならない。設定に無い identity は出ない。"""
    monkeypatch.setattr(
        decision_page, "get_settings",
        lambda: SimpleNamespace(
            decision_next_participant_choices=["Bohr", "Heisenberg", "human"]
        ),
    )
    messages = [
        {"author": "pr-gate-relay", "content": "review posted"},
        {"author": "orchestrator", "content": "opened"},
    ]

    got = decision_page._candidate_authors(messages, parked_author="pr-gate-relay")

    assert got == ["Bohr", "Heisenberg", "human"]
    assert "pr-gate-relay" not in got
    assert "orchestrator" not in got


def test_a_configured_identity_is_offered_even_if_it_never_spoke(monkeypatch):
    """`Fermi` はどのスレッドでも発言しない ∴ 発言者由来では永久に選べない。"""
    monkeypatch.setattr(
        decision_page, "get_settings",
        lambda: SimpleNamespace(decision_next_participant_choices=["Fermi"]),
    )

    assert decision_page._candidate_authors([{"author": "Bohr"}], "Bohr") == ["Fermi"]


def test_the_default_target_is_always_the_sentinel():
    """既定は誰でもない。**選ぶのは人。**

    駐機著者を既定にすると、`NEXT: Heisenberg` のスレッドで既定が著者の Bohr に
    なる ——— 何も触らずに送るとスレッドが名指していない相手に回る。sentinel なら
    本文の `NEXT:` 行がそのまま効く ＝「触らなければスレッドの指示どおり」。
    """
    assert (
        decision_page._resolve_default_target("Bohr", ["Bohr", "human"])
        == decision_page.NO_TARGET_VALUE
    )
    assert (
        decision_page._resolve_default_target("pr-gate-relay", ["Bohr", "human"])
        == decision_page.NO_TARGET_VALUE
    )
    assert (
        decision_page._resolve_default_target("", ["Bohr"])
        == decision_page.NO_TARGET_VALUE
    )


def test_the_registry_filter_still_drops_an_unregistered_configured_name(
    monkeypatch,
):
    """設定は候補の**出どころ**であって認可ではない。D-36 / D-38 は不変。"""
    async def per_name(name: str, **_):
        return _lookup(found=name != "Ghost")

    monkeypatch.setattr(decision_page, "_resolve_identity", per_name)
    monkeypatch.setattr(
        decision_page, "get_settings",
        lambda: SimpleNamespace(
            decision_next_participant_choices=["Bohr", "Ghost", "human"]
        ),
    )

    choices, unknown, any_unknown = asyncio.run(
        decision_page._participant_choices_registered([], parked_author="")
    )

    assert choices == ["Bohr", "human"]
    assert unknown == set()
    assert any_unknown is False


# ---------------------------------------------------------------------------
# 比較表 (2026-09-14)
# ---------------------------------------------------------------------------


def test_i21_lint_actually_matches_something():
    """**lint が空振りしていないこと。**

    ``test_i21_no_anchor_tag_inside_decision_choice_label`` は match を
    回すだけなので、**マッチ 0 件でも緑になる**。選択肢をカードから表へ
    書き換えたときクラス名が変われば、あの lint は誰にも何も言わずに
    無効化されていた。件数をここで留める。
    """
    from pathlib import Path
    import re

    src = Path("src/magickit/templates/decisions_thread.html").read_text(
        encoding="utf-8"
    )
    labels = re.findall(
        r'<label[^>]*class="[^"]*decision-choice[^"]*"[^>]*>(.*?)</label>',
        src,
        re.DOTALL,
    )
    # 選択肢行のラベルと sentinel の 2 つ。増えるぶんには構わないが、
    # 0 や 1 は「選択に使う label が lint の外に出た」という意味。
    assert len(labels) >= 2, f"I-21 lint covers only {len(labels)} label(s)"


@pytest.mark.asyncio
async def test_options_render_as_a_comparison_table(isolated_material_store):
    """行=選択肢 / 列=選択肢・得るもの・失うもの。

    縦積みのカードだと同じ次元を選択肢間で見比べられない。表の目的は
    「横に読むと 1 案、縦に読むと同じ次元の比較」。
    """
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD, head_msg_id="msg-1",
        signature=None, question="どう解くか",
        options=[
            {"id": "A", "label": "スカラへ射影する", "gain": "有界", "loss": "情報損失"},
            {"id": "B", "label": "sidecar に全文", "gain": "忠実", "loss": "sink 増"},
        ],
        recommendation="A", recommendation_reason="安いため", unknowns=None,
    )
    adapter = _adapter_for_thread(last_msg_id="msg-1")
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert 'class="table table-stack decision-choice-table"' in r.text
    for header in ("選択肢", "得るもの", "失うもの"):
        assert f'data-label="{header}"' in r.text or f">{header}<" in r.text
    # radio の配線は表になっても不変 (単一 group / value は "id: label")
    assert 'type="radio" name="content" value="A: スカラへ射影する"' in r.text
    assert 'type="radio" name="content" value="(自由記述のみ)"' in r.text
    # 推奨に印
    assert "推奨" in r.text


@pytest.mark.asyncio
async def test_the_table_is_absent_when_there_are_no_options(
    isolated_material_store,
):
    """選択肢が無いとき (J-stale / J-absent) は表ごと出さない。

    見出しだけの空表は「選択肢がある」と誤読させる。sentinel radio は残る
    ——— あれが無いと form が空の ``content`` を送って 422 になる (I-12)。
    """
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD, head_msg_id="msg-1",
        signature=None, question="選択肢の無い問い", options=None,
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    adapter = _adapter_for_thread(last_msg_id="msg-1")
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # クラス名は <style> に常時あるので、表の *マークアップ* で見る。
    assert '<table class="table table-stack decision-choice-table"' not in r.text
    assert 'type="radio" name="content" value="(自由記述のみ)"' in r.text
