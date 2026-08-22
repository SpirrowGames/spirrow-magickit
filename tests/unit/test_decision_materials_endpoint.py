"""Unit tests for the S5'' PUT/GET material endpoints.

Scope: the two HTTP surfaces mindwire (or a diagnostic curl) uses:

- ``PUT /v1/decisions/{project}/{thread_id}/material`` — spec §1.1
- ``GET /v1/decisions/{project}/{thread_id}/material`` — spec §1.2

These tests do **not** cover freshness classification (that lives in
``test_decisions_routes.py`` and ``test_decision_materials_freshness.py``).
Here we pin the receiver's contract: shape validation, ``composer_status``
enforcement (spec §1.3), UPSERT behavior at the HTTP boundary, and the
``MaterialNotStored`` 404.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from magickit.config import Settings
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools


PROJECT = "spirrow-magickit"
THREAD = "T-decision-materials"


@pytest.fixture(autouse=True)
def _configured():
    chatroom_tools.configure(Settings())
    yield
    chatroom_tools._settings = None


async def _put(path: str, body: dict[str, Any]) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.put(path, json=body)


async def _get(path: str) -> httpx.Response:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        return await client.get(path)


# --- PUT: happy path -----------------------------------------------------


@pytest.mark.asyncio
async def test_put_material_stores_and_reports_first_write():
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {
            "head_msg_id": "msg-100",
            "signature": "opaque",
            "composer_status": "ok",
            "question": "A or B?",
            "options": [
                {"id": "A", "label": "keep going",
                 "gain": "throughput", "loss": "revert cost"},
            ],
            "recommendation": "A",
            "recommendation_reason": "cheaper to unwind if wrong",
            "unknowns": ["push encoding"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {"stored": True, "replaced": False}


@pytest.mark.asyncio
async def test_put_material_replaces_existing_row_and_reports_replaced():
    """★ P-8 pin at the HTTP boundary — spec §2.2 UPSERT."""
    await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": "msg-1", "question": "old q"},
    )
    r2 = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": "msg-2", "question": "new q"},
    )
    assert r2.status_code == 200
    assert r2.json() == {"stored": True, "replaced": True}

    # GET reflects the second value, not merged.
    got = await _get(f"/v1/decisions/{PROJECT}/{THREAD}/material")
    assert got.status_code == 200
    stored = got.json()
    assert stored["head_msg_id"] == "msg-2"
    assert stored["question"] == "new q"


@pytest.mark.asyncio
async def test_put_material_accepts_missing_composer_status():
    """spec §1.3: absent ``composer_status`` is treated as ``"ok"``.

    The receiver does not require the field; missing means "not
    supplied", not "not ok". We do not force a schema on the sender
    for a field that is defined as optional.
    """
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": "msg-x"},  # no composer_status at all
    )
    assert r.status_code == 200
    assert r.json()["stored"] is True


# --- PUT: composer_status enforcement (spec §1.3) ------------------------


@pytest.mark.asyncio
async def test_put_material_rejects_non_ok_composer_status_with_400():
    """★ spec §1.3: ``composer_status != "ok"`` → 400, do not persist.

    Rationale (spec §1.3): "供給側の実装に受け側の正しさを預けない". The
    push side (mindwire) is supposed to filter first, but the receiver
    fails closed regardless — this is the exact opposite shape of the
    3-times-repeated msg-109 §3 defect ("受け口だけ出荷、供給経路なし"),
    which is why the guard exists on the receive side.
    """
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {
            "head_msg_id": "msg-y",
            "composer_status": "failed",
            "question": "should not be stored",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error_type"] == "ComposerStatusNotOk"
    # No partial persistence: GET returns 404 (nothing was written).
    got = await _get(f"/v1/decisions/{PROJECT}/{THREAD}/material")
    assert got.status_code == 404


@pytest.mark.asyncio
async def test_put_material_400_does_not_touch_existing_row():
    """A rejected PUT must not delete or partially update a prior row.

    (spec §1.3: "既存レコードがあれば触らない".) This is a slightly
    stronger form of the previous test: the guard runs *before* the
    UPSERT, not around it.
    """
    # First: seed a valid row.
    await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": "msg-good", "question": "keep me"},
    )
    # Second: send composer_status=failed.
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": "msg-bad", "composer_status": "failed"},
    )
    assert r.status_code == 400
    # The good row is intact.
    got = await _get(f"/v1/decisions/{PROJECT}/{THREAD}/material")
    assert got.status_code == 200
    assert got.json()["head_msg_id"] == "msg-good"


# --- PUT: shape validation -----------------------------------------------


@pytest.mark.asyncio
async def test_put_material_rejects_missing_head_msg_id():
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"question": "orphan"},
    )
    assert r.status_code == 400
    assert r.json()["error_type"] == "InvalidMaterialPayload"


@pytest.mark.asyncio
async def test_put_material_rejects_empty_head_msg_id():
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": ""},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_material_rejects_non_string_head_msg_id():
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": 42},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_material_rejects_non_list_options():
    r = await _put(
        f"/v1/decisions/{PROJECT}/{THREAD}/material",
        {"head_msg_id": "msg-1", "options": "not-a-list"},
    )
    assert r.status_code == 400


# --- GET: presence / absence --------------------------------------------


@pytest.mark.asyncio
async def test_get_material_returns_404_when_absent():
    r = await _get("/v1/decisions/no-project/no-thread/material")
    assert r.status_code == 404
    assert r.json()["error_type"] == "MaterialNotStored"


@pytest.mark.asyncio
async def test_get_material_reads_back_stored_row():
    """★ P-10 measurement pattern — "not '200 came back' but the content
    I PUT reads back" (Tier-C msg-118 §5). This test pins that the
    reader agrees with the writer on shape and values.
    """
    put_body = {
        "head_msg_id": "msg-77",
        "signature": "opaque",
        "composer_status": "ok",
        "question": "should we?",
        "options": [{"id": "A", "label": "yes"}],
        "recommendation": "A",
        "recommendation_reason": "reason",
        "unknowns": ["u1", "u2"],
    }
    r = await _put(f"/v1/decisions/p/t/material", put_body)
    assert r.status_code == 200

    got = await _get("/v1/decisions/p/t/material")
    assert got.status_code == 200
    stored = got.json()
    # All PUT fields round-trip. ``composer_status`` is enforced at the
    # PUT boundary but not stored (spec §2.1) — legitimate absence in GET.
    assert stored["head_msg_id"] == put_body["head_msg_id"]
    assert stored["signature"] == put_body["signature"]
    assert stored["question"] == put_body["question"]
    assert stored["options"] == put_body["options"]
    assert stored["recommendation"] == put_body["recommendation"]
    assert stored["recommendation_reason"] == put_body["recommendation_reason"]
    assert stored["unknowns"] == put_body["unknowns"]
    assert "stored_at" in stored
