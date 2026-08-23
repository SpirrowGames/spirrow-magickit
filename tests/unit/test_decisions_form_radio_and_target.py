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
        "thread": {"title": "T-d35"},
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
        "thread": {"title": "T-d35-checked"},
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
        "thread": {"title": "T-d35-fresh", "last_msg_id": "msg-1"},
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
        "thread": {"title": "T", "last_msg_id": "msg-9"},
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
        "thread": {"title": "T", "last_msg_id": "msg-99"},
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
        "thread": {"title": "T-d36"},
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
        "thread": {"title": "T-i19"},
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


def test_d37_resolve_default_target_returns_parked_when_registered():
    assert (
        decision_page._resolve_default_target("Bohr", ["Bohr", "human"])
        == "Bohr"
    )


def test_d37_resolve_default_target_demotes_when_parked_not_in_choices():
    """★ Critical case: parked author is ``pr-gate-relay`` (msg-131 shape).
    Default is "宛先を送らない" (empty), NOT ``human`` — a decision that
    "returned to itself" would look like the loop kept going when it did
    not (msg-146 §3 D-37 逐語)."""
    assert (
        decision_page._resolve_default_target("pr-gate-relay", ["Bohr", "human"])
        == decision_page.NO_TARGET_VALUE
    )


def test_d37_resolve_default_target_empty_parked_is_no_target():
    """Empty parked author (rare: last msg lacks author) → no target."""
    assert (
        decision_page._resolve_default_target("", ["Bohr", "human"])
        == decision_page.NO_TARGET_VALUE
    )


@pytest.mark.asyncio
async def test_d37_default_is_no_target_when_parked_author_is_pr_gate_relay():
    """End-to-end: a thread whose parked author is ``pr-gate-relay`` renders
    the select with "宛先を送らない" selected (not ``human``, not
    ``pr-gate-relay``). Direct fixture for the msg-131 case."""
    payload = {
        "thread": {"title": "T-d37"},
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


# --- D-38: fail-closed on UNKNOWN verdict --------------------------------


@pytest.mark.asyncio
async def test_d38_unknown_verdicts_drop_candidate_and_flag_verify_unavailable():
    """UNKNOWN (Prismind outage / timeout) → candidate is dropped and
    the template renders the "verification unavailable" notice."""
    payload = {
        "thread": {"title": "T-d38"},
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
        "thread": {"title": "T-d38-all"},
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
        "thread": {"title": "T"},
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
