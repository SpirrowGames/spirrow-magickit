"""Unit tests for the optional ``jev`` material field (2e-2b-1).

Spec: mindwire ``T-decider-conductor-hook`` msg-6651 (approved msg-6650),
mapped onto magickit in ``T-decider-decision-page-jev`` msg-1311 / msg-1313;
``spec/slices/S5-decision-materials.md`` §1.1 / §2.1 / §4.1.

Pinned here:

- A PUT without ``jev`` (or with ``jev: null``) keeps its meaning and is 2xx.
- A present ``jev`` is strict: object; ``should_ask_human`` a finite,
  non-bool number in [0, 1]; ``rule`` / ``model_version`` strings. Anything
  else is a 400 with nothing stored. Extra keys are dropped.
- Storage: late ``jev_json`` column on an old-schema DB; NULL reads back as
  ``None``; overwrite and clear both go through every SQL site (INSERT
  column list, placeholders, params, SELECT).
- Display: one fixed line on J-fresh only, p verbatim; nothing on absent
  jev or J-stale; the choices HTML is identical with and without jev.
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import httpx
import pytest

from magickit.config import Settings
from magickit.core.decision_materials import DecisionMaterialStore
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools

PROJECT = "spirrow-magickit"
THREAD = "T-jev"
MATERIAL = f"/v1/decisions/{PROJECT}/{THREAD}/material"
JEV = {"should_ask_human": 0.37, "rule": "rule-A", "model_version": "jev-0"}


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


async def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.request(method, path, **kwargs)


def _store_kwargs(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = dict(
        project=PROJECT, thread_id=THREAD, head_msg_id="msg-99",
        signature=None, question="Q?",
        options=[{"id": "A", "label": "go", "gain": "g", "loss": "l"},
                 {"id": "B", "label": "stop"}],
        recommendation="A", recommendation_reason="because", unknowns=None,
    )
    kw.update(over)
    return kw


# --- PUT: accepted ---------------------------------------------------------


@pytest.mark.asyncio
async def test_put_without_jev_succeeds():
    r = await _request("PUT", MATERIAL, json={"head_msg_id": "msg-1"})
    assert r.status_code == 200
    g = await _request("GET", MATERIAL)
    assert g.status_code == 200 and g.json()["jev"] is None


@pytest.mark.asyncio
async def test_put_with_null_jev_is_treated_as_absent():
    r = await _request("PUT", MATERIAL, json={"head_msg_id": "msg-1", "jev": None})
    assert r.status_code == 200
    assert (await _request("GET", MATERIAL)).json()["jev"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("p", [0, 1, 0.0, 1.0, 0.37, 0.123456789])
async def test_put_with_valid_jev_round_trips(p):
    jev = {**JEV, "should_ask_human": p}
    r = await _request("PUT", MATERIAL, json={"head_msg_id": "msg-1", "jev": jev})
    assert r.status_code == 200
    assert (await _request("GET", MATERIAL)).json()["jev"] == jev


@pytest.mark.asyncio
async def test_put_drops_extra_jev_keys():
    jev = {**JEV, "auc": 0.9, "extra": {"x": 1}}
    r = await _request("PUT", MATERIAL, json={"head_msg_id": "msg-1", "jev": jev})
    assert r.status_code == 200
    assert (await _request("GET", MATERIAL)).json()["jev"] == JEV


# --- PUT: refused (400, nothing stored) ------------------------------------


_BAD_JEVS = [
    pytest.param("0.4", id="jev-not-object-str"),
    pytest.param([0.4], id="jev-not-object-list"),
    pytest.param({**JEV, "should_ask_human": True}, id="p-bool-true"),
    pytest.param({**JEV, "should_ask_human": False}, id="p-bool-false"),
    pytest.param({**JEV, "should_ask_human": "0.4"}, id="p-string"),
    pytest.param({**JEV, "should_ask_human": None}, id="p-null"),
    pytest.param({k: v for k, v in JEV.items() if k != "should_ask_human"},
                 id="p-missing"),
    pytest.param({**JEV, "should_ask_human": float("nan")}, id="p-nan"),
    pytest.param({**JEV, "should_ask_human": float("inf")}, id="p-inf"),
    pytest.param({**JEV, "should_ask_human": 1.5}, id="p-above-1"),
    pytest.param({**JEV, "should_ask_human": -0.01}, id="p-below-0"),
    pytest.param({**JEV, "rule": 3}, id="rule-not-str"),
    pytest.param({k: v for k, v in JEV.items() if k != "rule"}, id="rule-missing"),
    pytest.param({**JEV, "model_version": 1}, id="model-version-not-str"),
    pytest.param({k: v for k, v in JEV.items() if k != "model_version"},
                 id="model-version-missing"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("jev", _BAD_JEVS)
async def test_put_with_invalid_jev_is_400_and_stores_nothing(jev):
    # Raw body: httpx's json= refuses NaN / Infinity, but a sender's JSON
    # encoder may not, and the server's parser accepts them.
    r = await _request(
        "PUT", MATERIAL,
        content=json.dumps({"head_msg_id": "msg-1", "jev": jev}, allow_nan=True),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error_type"] == "InvalidMaterialPayload"
    assert (await _request("GET", MATERIAL)).status_code == 404


# --- store: overwrite / clear / migration ----------------------------------


@pytest.mark.asyncio
async def test_second_put_overwrites_jev(isolated_material_store):
    store = isolated_material_store()
    await store.put_material(**_store_kwargs(jev=JEV))
    later = {"should_ask_human": 0.9, "rule": "rule-B", "model_version": "jev-1"}
    await store.put_material(**_store_kwargs(jev=later))
    got = await store.get_material(project=PROJECT, thread_id=THREAD)
    assert got is not None and got["jev"] == later


@pytest.mark.asyncio
async def test_put_without_jev_clears_previous_jev(isolated_material_store):
    """INSERT OR REPLACE replaces the row: a head without jev must not keep
    the previous head's p."""
    store = isolated_material_store()
    await store.put_material(**_store_kwargs(jev=JEV))
    await store.put_material(**_store_kwargs(head_msg_id="msg-100"))
    got = await store.get_material(project=PROJECT, thread_id=THREAD)
    assert got is not None and got["jev"] is None


@pytest.mark.asyncio
async def test_jev_column_is_added_to_a_pre_existing_table(tmp_path):
    db = str(tmp_path / "old.db")
    async with aiosqlite.connect(db) as conn:
        # The schema as it stood before jev_json (parked-lane columns in).
        await conn.execute("""
            CREATE TABLE decision_materials (
                project TEXT NOT NULL, thread_id TEXT NOT NULL,
                head_msg_id TEXT NOT NULL, signature TEXT, stop_reason TEXT,
                parked_lane TEXT, operator_task TEXT, protocol_violation INTEGER,
                question TEXT, options_json TEXT, recommendation TEXT,
                recommendation_reason TEXT, unknowns_json TEXT,
                stored_at TEXT NOT NULL, UNIQUE(project, thread_id)
            )
        """)
        await conn.execute(
            "INSERT INTO decision_materials (project, thread_id, head_msg_id,"
            " stored_at) VALUES ('p', 'T-old', 'msg-1', '2026-10-01T00:00:00Z')"
        )
        await conn.commit()

    store = DecisionMaterialStore(db_path=db)
    old = await store.get_material(project="p", thread_id="T-old")
    assert old is not None and old["jev"] is None  # NULL never hits json.loads

    await store.put_material(**_store_kwargs(project="p", thread_id="T-new", jev=JEV))
    new = await store.get_material(project="p", thread_id="T-new")
    assert new is not None and new["jev"] == JEV
    # The board listing is out of scope and does not carry jev.
    for row in await store.list_materials():
        assert "jev" not in row and "jev_json" not in row


# --- display ---------------------------------------------------------------


def _parked(last_msg_id: str) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_thread = AsyncMock(return_value={
        "thread": {"title": THREAD, "status": "parked", "last_msg_id": last_msg_id},
        "messages": [{"author": "Bohr", "content": "please decide",
                      "next_participant": "human", "msg_id": last_msg_id}],
        "mode": "full",
    })
    adapter.close = AsyncMock()
    return adapter


async def _page(last_msg_id: str) -> httpx.Response:
    with patch.object(chatroom_tools, "_adapter", return_value=_parked(last_msg_id)):
        r = await _request("GET", f"/dashboard/decisions/{PROJECT}/{THREAD}")
    assert r.status_code == 200
    return r


def _choices_html(html: str) -> str:
    m = re.search(r'<table class="table table-stack decision-choice-table">.*?</table>',
                  html, re.S)
    assert m is not None
    return m.group(0)


@pytest.mark.asyncio
async def test_fresh_with_jev_renders_the_fixed_line_verbatim(isolated_material_store):
    store = isolated_material_store()
    jev = {"should_ask_human": 0.123456789, "rule": "rule-VERBATIM",
           "model_version": "jev-0"}
    await store.put_material(**_store_kwargs(jev=jev))
    r = await _page("msg-99")
    assert ('<div class="decision-jev">Jev: should_ask_human=0.123456789'
            '（未検証、AUC 未確立）— rule-VERBATIM</div>') in r.text
    assert r.text.count('class="decision-jev"') == 1


@pytest.mark.asyncio
async def test_fresh_without_jev_renders_nothing_extra(isolated_material_store):
    store = isolated_material_store()
    await store.put_material(**_store_kwargs())
    r = await _page("msg-99")
    assert "decision-jev" not in r.text
    assert "should_ask_human" not in r.text


@pytest.mark.asyncio
async def test_stale_with_jev_renders_no_jev(isolated_material_store):
    store = isolated_material_store()
    await store.put_material(**_store_kwargs(head_msg_id="msg-OLD", jev=JEV))
    r = await _page("msg-NEW")
    assert "decision-jev" not in r.text
    assert "should_ask_human" not in r.text
    assert "rule-A" not in r.text


@pytest.mark.asyncio
async def test_choices_html_is_identical_with_and_without_jev(isolated_material_store):
    """Display only: jev must not sort, hide, or alter the choices."""
    store = isolated_material_store()
    await store.put_material(**_store_kwargs())
    without = _choices_html((await _page("msg-99")).text)
    await store.put_material(**_store_kwargs(
        jev={"should_ask_human": 0.99, "rule": "rule-A", "model_version": "v"}))
    with_jev = _choices_html((await _page("msg-99")).text)
    assert with_jev == without
