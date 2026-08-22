"""Unit tests for S5'' judgement-page material states.

Scope: the 3 states of ``GET /dashboard/decisions/{project}/{thread_id}``
when the thread is parked to human (mode=judgement):

- **J-fresh**: material stored ∧ ``material.head_msg_id == thread.last_msg_id``
- **J-stale**: material stored ∧ mismatch (OR ``last_msg_id`` unreadable)
- **J-absent**: no material stored

The 4-branch behavior (parked / not_waiting / not_found / unavailable)
is covered in ``test_decisions_routes.py``. Here we assume the parked
branch is entered and pin how the 3 states shape the rendered page.

**★ I-14 test** (spec §4-1 / msg-117 §2): J-stale must not emit the
material's text into the HTML **at all** — not hidden, not folded, not
in a ``<details>``. Assert "the string is not present", not "the button
is not present" (the latter would pass with hidden text and defeat the
whole rule).
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
THREAD = "T-judgement"


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


def _parked_thread_payload(
    last_msg_id: str | None,
    author: str = "Bohr",
    include_last: bool = True,
) -> dict[str, Any]:
    """Build a Conclair `get_thread` response for a parked-to-human thread.

    ``last_msg_id=None, include_last=False`` produces a thread rollup with
    the field missing (the fail-to-stale case, spec §3.2).
    """
    thread: dict[str, Any] = {"title": "T-judgement"}
    if include_last:
        thread["last_msg_id"] = last_msg_id
    return {
        "thread": thread,
        "messages": [
            {
                "author": author,
                "content": "please decide",
                "next_participant": "human",
                "msg_id": last_msg_id or "msg-fallback",
            }
        ],
        "mode": "full",
    }


# --- pure helper unit tests ---------------------------------------------


def test_head_msg_id_from_thread_reads_last_msg_id_string():
    assert decision_page._head_msg_id_from_thread({"last_msg_id": "msg-9"}) == "msg-9"


def test_head_msg_id_from_thread_returns_none_when_missing():
    assert decision_page._head_msg_id_from_thread({}) is None


def test_head_msg_id_from_thread_returns_none_when_null():
    assert decision_page._head_msg_id_from_thread({"last_msg_id": None}) is None


def test_head_msg_id_from_thread_returns_none_when_empty():
    """Spec §3.2 fail-to-stale: empty string collapses to None → J-stale."""
    assert decision_page._head_msg_id_from_thread({"last_msg_id": ""}) is None
    assert decision_page._head_msg_id_from_thread({"last_msg_id": "   "}) is None


def test_head_msg_id_from_thread_returns_none_for_non_string():
    """A future producer of an int/float ``last_msg_id`` should not fool
    us into a bad ``==`` comparison. ``None`` = fall-to-stale."""
    assert decision_page._head_msg_id_from_thread({"last_msg_id": 42}) is None


def test_classify_absent_when_no_material():
    assert decision_page._classify_judgement_state(None, "msg-1") == "absent"


def test_classify_stale_when_head_unknown_but_material_present():
    """Fail-to-stale: if we can't read the head, we don't call it fresh."""
    material = {"head_msg_id": "msg-1"}
    assert decision_page._classify_judgement_state(material, None) == "stale"


def test_classify_fresh_on_exact_match():
    material = {"head_msg_id": "msg-42"}
    assert decision_page._classify_judgement_state(material, "msg-42") == "fresh"


def test_classify_stale_on_any_mismatch():
    material = {"head_msg_id": "msg-42"}
    assert decision_page._classify_judgement_state(material, "msg-43") == "stale"


def test_classify_no_normalization_prefix_or_case_treated_as_mismatch():
    """★ spec §3.1: no normalization. Byte-for-byte equality only.

    The two sides are strings owned by different producers (composer's
    push value / Conclair's rollup). "Normalizing" would mean guessing
    the other side's encoding; if the shapes ever diverge we want the
    freshness check to say "stale" (safer default), not silently paper
    over the mismatch.
    """
    material = {"head_msg_id": "MSG-42"}
    assert decision_page._classify_judgement_state(material, "msg-42") == "stale"
    material2 = {"head_msg_id": "msg-42 "}  # trailing space
    assert decision_page._classify_judgement_state(material2, "msg-42") == "stale"


def test_classify_stale_when_material_lacks_head_msg_id():
    """A corrupt row (no head_msg_id) → stale (we can't prove freshness)."""
    assert decision_page._classify_judgement_state({}, "msg-1") == "stale"


# --- choice_options derivation ------------------------------------------


def test_choice_options_from_none_material_is_empty():
    """J-stale / J-absent set material=None ∴ empty list = no choice cards."""
    assert decision_page._choice_options_from_material(None) == []


def test_choice_options_from_material_without_options_is_empty():
    assert decision_page._choice_options_from_material({"question": "?"}) == []


def test_choice_options_from_material_returns_id_label_gain_loss_value():
    """Value format is ``f"{id}: {label}"`` — matches msg-121 実タップ形
    ("A: そのまま進める") so downstream regex / display continues to work.
    """
    got = decision_page._choice_options_from_material({
        "options": [
            {"id": "A", "label": "keep going",
             "gain": "speed", "loss": "risk"},
            {"id": "B", "label": "pause"},
        ],
    })
    assert got == [
        {"id": "A", "label": "keep going",
         "gain": "speed", "loss": "risk", "value": "A: keep going"},
        {"id": "B", "label": "pause", "gain": "", "loss": "",
         "value": "B: pause"},
    ]


def test_choice_options_filters_malformed_rows():
    """A row missing id/label is dropped silently. Renderer is strict
    (spec §4.1 filter) so a slightly-wrong composer payload does not
    crash the page — one dropped option, others still render."""
    got = decision_page._choice_options_from_material({
        "options": [
            {"id": "A", "label": "ok"},
            {"label": "no id"},          # dropped
            {"id": "B"},                  # dropped
            "not a dict",                 # dropped
            {"id": "C", "label": "yes"},
        ],
    })
    ids = [opt["id"] for opt in got]
    assert ids == ["A", "C"]


# --- integration: 3 states end-to-end (via ASGI GET) --------------------


@pytest.mark.asyncio
async def test_j_absent_renders_no_material_message_and_form(isolated_material_store):
    """J-absent: no material stored → «判断材料が用意されていません» + form.

    **★ spec §4.3**: J-absent must NOT render a "tail" (末尾数通 as
    material). This test also pins that the parked msg's text does NOT
    appear on the page (the ``content`` field of the last message,
    which the old ``parked_msg_content`` rendering carried).
    """
    adapter = _adapter_returning(_parked_thread_payload("msg-99"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "判断材料が用意されていません" in r.text
    # Form remains (I-12 stays in all 3 states).
    assert 'name="_decision_form" value="1"' in r.text
    assert 'name="_freeform"' in r.text
    # ★ spec §4.3: tail is NOT rendered. The parked msg's content text
    # must not appear on the page.
    assert "please decide" not in r.text


@pytest.mark.asyncio
async def test_j_absent_does_not_render_composer_two_choice_fallback(isolated_material_store):
    """★ spec §4.1 abolition of the hardcoded 2-choice fallback (msg-117 §5).

    Before S5'', the judgement page always drew "A: そのまま進める" /
    "B: 一旦止める / 修正が要る". S5'' removes those; buttons come from
    composer material only. In J-absent we must NOT see them.
    """
    adapter = _adapter_returning(_parked_thread_payload("msg-99"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")
    assert "そのまま進める" not in r.text
    assert "一旦止める" not in r.text
    # The I-12 sentinel button remains (only way to send in J-absent).
    assert "自由記述だけで送る" in r.text


@pytest.mark.asyncio
async def test_j_fresh_renders_all_material_fields(isolated_material_store):
    """J-fresh: question / options (as buttons) / recommendation /
    recommendation_reason / unknowns are all in the HTML, verbatim."""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-99",  # match thread.last_msg_id below
        signature="sig",
        question="Q-VERBATIM: which slice next?",
        options=[
            {"id": "A", "label": "L-VERBATIM-A",
             "gain": "G-VERBATIM-A", "loss": "LX-VERBATIM-A"},
            {"id": "B", "label": "L-VERBATIM-B",
             "gain": "G-VERBATIM-B", "loss": "LX-VERBATIM-B"},
        ],
        recommendation="A",
        recommendation_reason="R-REASON-VERBATIM",
        unknowns=["U-VERBATIM-1", "U-VERBATIM-2"],
    )

    adapter = _adapter_returning(_parked_thread_payload("msg-99"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "Q-VERBATIM: which slice next?" in r.text
    assert "L-VERBATIM-A" in r.text
    assert "L-VERBATIM-B" in r.text
    assert "G-VERBATIM-A" in r.text
    assert "LX-VERBATIM-A" in r.text
    assert "R-REASON-VERBATIM" in r.text
    assert "U-VERBATIM-1" in r.text
    assert "U-VERBATIM-2" in r.text
    # Choice buttons carry `content="{id}: {label}"` (spec §4.1).
    assert 'value="A: L-VERBATIM-A"' in r.text
    assert 'value="B: L-VERBATIM-B"' in r.text


@pytest.mark.asyncio
async def test_j_stale_renders_warning_and_does_not_render_material_text(
    isolated_material_store,
):
    """★★★ I-14 test (spec §4-1 / msg-117 §2 / msg-118 §2 Tier-C 承認).

    Assertions:
      1. Warning banner shows both the material's head_msg_id AND the
         thread's current head_msg_id.
      2. **Not a single character of the stored material's user-facing
         strings appears in the HTML.** Not hidden. Not folded. Server
         must not emit them. This is what makes I-14 different from
         "the button isn't clickable" — hidden text is still text.

    If this test fails, someone has re-introduced the fold-visible-but-
    disabled variant that Bohr proposed and Einstein rejected in
    msg-114/115. Do not "fix" this by adding CSS display:none.
    """
    store = isolated_material_store()
    STALE_STRINGS = {
        "question": "STALE-QUESTION-DO-NOT-RENDER",
        "options": [
            {"id": "A", "label": "STALE-LABEL-A",
             "gain": "STALE-GAIN-A", "loss": "STALE-LOSS-A"},
            {"id": "B", "label": "STALE-LABEL-B",
             "gain": "STALE-GAIN-B", "loss": "STALE-LOSS-B"},
        ],
        "recommendation_reason": "STALE-REASON",
        "unknowns": ["STALE-UNKNOWN-1"],
    }
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-OLD",  # ≠ thread head below
        signature=None,
        question=STALE_STRINGS["question"],
        options=STALE_STRINGS["options"],
        recommendation="A",
        recommendation_reason=STALE_STRINGS["recommendation_reason"],
        unknowns=STALE_STRINGS["unknowns"],
    )

    adapter = _adapter_returning(_parked_thread_payload("msg-NEW"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    # Warning banner shows both ids (this is how J-stale is distinguished
    # from J-absent — text alone, not the presence of hidden material).
    assert "msg-OLD" in r.text
    assert "msg-NEW" in r.text
    assert "この判断依頼の材料は古くなっています" in r.text

    # ★★★ I-14: material strings must NOT appear anywhere in the HTML.
    for s in (
        STALE_STRINGS["question"],
        "STALE-LABEL-A", "STALE-LABEL-B",
        "STALE-GAIN-A", "STALE-GAIN-B",
        "STALE-LOSS-A", "STALE-LOSS-B",
        "STALE-REASON",
        "STALE-UNKNOWN-1",
    ):
        assert s not in r.text, f"I-14 violated: {s!r} was emitted in J-stale"


@pytest.mark.asyncio
async def test_j_stale_form_has_no_choice_buttons_only_freeform_submit(
    isolated_material_store,
):
    """In J-stale, choice buttons must not exist — sending an old option's
    label as the current decision was Einstein's original worry
    (msg-112 §2 / msg-115 §2). ``choice_options=[]`` ∴ the template's
    for-loop is empty ∴ the only submit remaining is I-12.
    """
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-OLD",
        signature=None,
        question="q",
        options=[
            {"id": "A", "label": "STALE-BTN-A"},
            {"id": "B", "label": "STALE-BTN-B"},
        ],
        recommendation=None, recommendation_reason=None, unknowns=None,
    )
    adapter = _adapter_returning(_parked_thread_payload("msg-NEW"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    # I-12 sentinel button is still there — sending is still possible.
    assert 'value="(自由記述のみ)"' in r.text
    # No stale option buttons: their label is not rendered anywhere,
    # not just missing from a button.
    assert "STALE-BTN-A" not in r.text
    assert "STALE-BTN-B" not in r.text


@pytest.mark.asyncio
async def test_fail_to_stale_when_thread_last_msg_id_missing(
    isolated_material_store,
):
    """★ spec §3.2 fail-to-stale: material present but ``last_msg_id``
    missing from the thread rollup → J-stale, NOT J-fresh.

    The value of this default is that when P-9 breaks in the future, the
    page never silently emits an old material as "fresh". A no-attribute
    (missing ``last_msg_id`` key) is our unknown, and unknowns collapse
    to stale."""
    store = isolated_material_store()
    await store.put_material(
        project=PROJECT, thread_id=THREAD,
        head_msg_id="msg-STORED",
        signature=None,
        question="STALE-Q",
        options=None, recommendation=None,
        recommendation_reason=None, unknowns=None,
    )
    # No ``last_msg_id`` in the thread rollup — simulate P-9 break.
    payload = _parked_thread_payload(None, include_last=False)
    adapter = _adapter_returning(payload)
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "この判断依頼の材料は古くなっています" in r.text
    # Fresh material's text still hidden (fail-to-stale means J-stale
    # rules apply — no material rendered).
    assert "STALE-Q" not in r.text


@pytest.mark.asyncio
async def test_material_store_outage_falls_to_j_absent(
    isolated_material_store, monkeypatch
):
    """A raise from the store must NOT 500 the page. It must land on
    J-absent (safer than pretending we have material)."""
    from magickit.web import decisions as decisions_module

    class BrokenStore:
        async def get_material(self, **_):
            raise RuntimeError("simulated db outage")

    monkeypatch.setattr(
        decisions_module, "_get_material_store", lambda: BrokenStore()
    )

    adapter = _adapter_returning(_parked_thread_payload("msg-1"))
    with patch.object(chatroom_tools, "_adapter", return_value=adapter):
        r = await _get(f"/dashboard/decisions/{PROJECT}/{THREAD}")

    assert r.status_code == 200
    assert "判断材料が用意されていません" in r.text
