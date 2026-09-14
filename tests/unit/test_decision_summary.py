"""判断材料の簡潔版 (Lexora light)。

要約は**導出物**で、正本は画面の下にある詳細。このファイルの主張は 2 つ:

1. 要約が詳細と**突き合わせ可能**であること —— 入力は材料そのもので、
   option の id は元と同じ。存在しない選択肢を作ったら捨てる。
2. 要約の失敗が**判断を止めない**こと —— 何が起きても fragment が返り、
   ページは壊れない。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from magickit.config import Settings
from magickit.core.decision_summaries import DecisionSummaryStore
from magickit.main import create_app
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web import decision_summary

PROJECT = "spirrow-magickit"
THREAD = "T-summary"

_MATERIAL = {
    "head_msg_id": "msg-9",
    "question": "長い問い。" * 40,
    "options": [
        {"id": "A", "label": "長いラベル A " * 10, "gain": "得 A " * 20, "loss": "失 A " * 20},
        {"id": "B", "label": "長いラベル B " * 10, "gain": "得 B " * 20, "loss": "失 B " * 20},
    ],
}

_GOOD = (
    '{"question": "短い問い", "options": ['
    '{"id": "A", "label": "短い A", "gain": "得A", "loss": "失A"},'
    '{"id": "B", "label": "短い B", "gain": "得B", "loss": "失B"}]}'
)


# --- 純関数 ---------------------------------------------------------------


def test_the_input_is_the_material_not_the_thread():
    """**突き合わせが成立する条件。**

    スレッドから独立に書き直させると「同じ問いに対する 2 つの答え」になり、
    食い違ったときにどちらが正しいか読み手には決められない。材料を要約する
    なら両者は同じ文章の 2 つの見え方で、検算が目で完結する。
    """
    prompt = decision_summary.build_user_prompt(_MATERIAL)

    assert _MATERIAL["question"] in prompt
    assert "id: A" in prompt and "id: B" in prompt
    assert "得 A" in prompt and "失 B" in prompt


def test_an_invented_option_id_is_rejected():
    """要約が**存在しない選択肢**を作ったら捨てる。

    並べて比較する相手が居ない ∴ 黙って通すと「詳細と突き合わせられる」と
    いう前提そのものが崩れる。
    """
    bad = '{"question": "q", "options": [{"id": "Z", "label": "l", "gain": "g", "loss": "x"}]}'

    assert decision_summary.parse_summary(bad, _MATERIAL) is None


def test_a_dropped_option_is_kept_visible_not_back_filled():
    """元にあって要約に無い id は**埋めない**。

    埋めると要約が元と同じ長さになって意味が無いし、モデルが落とした事実を
    隠すことにもなる。件数を持ち帰り、テンプレートが「元 N 件のうち M 件」
    と出す。
    """
    partial = '{"question": "q", "options": [{"id": "A", "label": "l", "gain": "g", "loss": "x"}]}'

    got = decision_summary.parse_summary(partial, _MATERIAL)

    assert got is not None
    assert [o["id"] for o in got["options"]] == ["A"]
    assert got["source_option_count"] == 2


def test_non_json_is_rejected_rather_than_shown():
    assert decision_summary.parse_summary("すみません、できません", _MATERIAL) is None
    assert decision_summary.parse_summary("", _MATERIAL) is None


def test_json_wrapped_in_prose_is_still_read():
    """モデルが前後に喋っても JSON 部分を拾う。"""
    got = decision_summary.parse_summary(f"はい:\n{_GOOD}\n以上", _MATERIAL)

    assert got is not None
    assert [o["label"] for o in got["options"]] == ["短い A", "短い B"]


# --- キャッシュ -----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cache_is_keyed_on_the_material_head(tmp_path):
    """材料が更新されれば**キーが合わなくなって自然に無効化**される。

    古い要約を新しい材料の隣に置くのは、両者が同じ文章の 2 つの見え方だと
    いう前提を壊す ——— それは別の問いの要約。
    """
    store = DecisionSummaryStore(db_path=str(tmp_path / "s.db"))
    await store.put(
        project=PROJECT, thread_id=THREAD, head_msg_id="msg-9",
        payload={"question": "q", "options": []}, tier="light", model=None,
    )

    assert await store.get(
        project=PROJECT, thread_id=THREAD, head_msg_id="msg-9"
    ) is not None
    assert await store.get(
        project=PROJECT, thread_id=THREAD, head_msg_id="msg-10"
    ) is None


@pytest.mark.asyncio
async def test_provenance_survives_the_round_trip(tmp_path):
    store = DecisionSummaryStore(db_path=str(tmp_path / "s.db"))
    await store.put(
        project=PROJECT, thread_id=THREAD, head_msg_id="msg-9",
        payload={"question": "q", "options": []}, tier="light", model=None,
    )

    got = await store.get(project=PROJECT, thread_id=THREAD, head_msg_id="msg-9")

    assert got is not None
    assert got["tier"] == "light"
    assert got["generated_at"].endswith("Z")


# --- ルート ---------------------------------------------------------------


async def _post(monkeypatch, *, chat, material=_MATERIAL, settings=None):
    lexora = AsyncMock()
    lexora.chat = chat
    monkeypatch.setattr(decision_summary, "LexoraAdapter", lambda **_: lexora)

    async def _load(project, thread_id):
        return (material, True)

    monkeypatch.setattr(decision_summary.decisions, "_load_material", _load)
    if settings is not None:
        monkeypatch.setattr(decision_summary, "get_settings", lambda: settings)

    chatroom_tools.configure(Settings())
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        return await c.post(f"/dashboard/decisions/{PROJECT}/{THREAD}/_summary")


@pytest.mark.asyncio
async def test_the_summary_renders_as_a_second_comparable_table(monkeypatch, tmp_path):
    s = Settings(db_path=str(tmp_path / "m.db"))
    r = await _post(monkeypatch, chat=AsyncMock(return_value=_GOOD), settings=s)

    assert r.status_code == 200
    assert "簡潔版" in r.text
    assert "短い A" in r.text and "短い B" in r.text
    assert 'data-label="得るもの"' in r.text
    # 導出物であることが画面に残る
    assert "light" in r.text
    assert "判断の根拠は下の詳細です" in r.text


@pytest.mark.asyncio
async def test_light_tier_is_requested_explicitly(monkeypatch, tmp_path):
    """``LexoraAdapter.chat`` の既定は ``"medium"`` ∴ 明示しないと重い方へ行く。"""
    chat = AsyncMock(return_value=_GOOD)
    await _post(monkeypatch, chat=chat, settings=Settings(db_path=str(tmp_path / "m.db")))

    assert chat.await_args.kwargs["model"] == "light"


@pytest.mark.asyncio
async def test_a_timeout_returns_a_fragment_not_a_500(monkeypatch, tmp_path):
    """**要約の失敗が判断を止めない。** 下の詳細はそのまま読める。"""
    async def _slow(**_):
        await asyncio.sleep(10)

    s = Settings(db_path=str(tmp_path / "m.db"), decision_summary_timeout_seconds=0.01)
    r = await _post(monkeypatch, chat=_slow, settings=s)

    assert r.status_code == 200
    assert "要約が返りませんでした" in r.text
    assert "下の詳細はそのまま読めます" in r.text


@pytest.mark.asyncio
async def test_lexora_raising_returns_a_fragment_not_a_500(monkeypatch, tmp_path):
    chat = AsyncMock(side_effect=httpx.ConnectError("lexora down"))
    s = Settings(db_path=str(tmp_path / "m.db"))

    r = await _post(monkeypatch, chat=chat, settings=s)

    assert r.status_code == 200
    assert "要約を作れませんでした" in r.text


@pytest.mark.asyncio
async def test_disabled_returns_a_fragment_not_a_404(monkeypatch, tmp_path):
    """描画されたボタンが 404 を返すと、設定ではなくページの不具合に見える。"""
    s = Settings(db_path=str(tmp_path / "m.db"), decision_summary_enabled=False)

    r = await _post(monkeypatch, chat=AsyncMock(return_value=_GOOD), settings=s)

    assert r.status_code == 200
    assert "無効化されています" in r.text


@pytest.mark.asyncio
async def test_the_second_press_is_served_from_cache(monkeypatch, tmp_path):
    """同じ材料を 2 度要約しない (GPU は 1 枚)。"""
    chat = AsyncMock(return_value=_GOOD)
    s = Settings(db_path=str(tmp_path / "m.db"))

    first = await _post(monkeypatch, chat=chat, settings=s)
    second = await _post(monkeypatch, chat=chat, settings=s)

    assert chat.await_count == 1
    assert "簡潔版" in first.text and "簡潔版" in second.text
    assert "前回の結果" in second.text


def test_the_summary_endpoint_is_three_segments():
    """2 セグメントは判断ページ本体と衝突する (URL の分割は形であって登録順ではない)。"""
    paths = [r.path for r in decision_summary.router.routes]  # type: ignore[attr-defined]

    assert paths == ["/dashboard/decisions/{project}/{thread_id}/_summary"]


def test_the_output_shape_is_constrained_at_decode_time(monkeypatch, tmp_path):
    """**prompt で「JSON のみ」と言うだけでは足りない。**

    実測 (2026-09-14, Qwen3.8-27B): 4 選択肢のうち 4 つ目の ``"label"`` の
    後のカンマが欠けた JSON が返り、内容は正しいのに要約が丸ごと捨てられた。
    散文を後から修復するのは「壊れ方の種類」を当て続ける賭けで、外した日に
    黙って要約が消える ∴ デコードの段階で構造を縛る。
    """
    chat = AsyncMock(return_value=_GOOD)

    asyncio.run(
        _post(monkeypatch, chat=chat, settings=Settings(db_path=str(tmp_path / "m.db")))
    )

    fmt = chat.await_args.kwargs["response_format"]
    assert fmt["type"] == "json_schema"
    schema = fmt["json_schema"]["schema"]
    assert schema["required"] == ["question", "options"]
    item = schema["properties"]["options"]["items"]
    assert item["required"] == ["id", "label", "gain", "loss"]
    # 余計な鍵を作らせない (id を振り直す余地を消す)
    assert item["additionalProperties"] is False
