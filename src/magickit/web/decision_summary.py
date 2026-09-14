"""判断材料の簡潔版を Lexora light に作らせる口。

``POST /dashboard/decisions/{project}/{thread_id}/_summary`` —— **3 セグメント**
なので判断ページ本体 (``/{project}/{thread_id}``、2 セグメント) とも板の
``/_board`` ``/_lane`` (1 セグメント) とも衝突しない。URL の分割は形であって
登録順ではない、という既存の規約 (``decisions.py`` / ``board.py`` の該当
コメント) をそのまま踏襲する。

**このページで HTMX を使うのはこのボタンだけ。** ``decisions_thread.html`` は
「form はプレーン HTML (HTMX なし)。JS 無しでも送信・エラー再描画が完結」を
設計として持っている。要約は**あれば便利なもの**で、無くても判断はできる ∴
JS が動かない環境ではボタンが効かないだけで、判断フォームは従来どおり動く。
この非対称は意図的で、逆 (判断フォームを HTMX 化する) は絶対にやらない。

**同期・上限つき。** ``chatroom_digest`` と同じ姿勢: 押した人は要約が欲しいの
であって約束が欲しいのではないし、~10 秒の処理のために job table と polling を
足すのは釣り合わない。ただし ``asyncio.wait_for`` で必ず縛る。

**失敗は必ず flash で返し、ページは壊さない。** 要約は導出物なので、無ければ
詳細を読めばよい。要約の失敗が判断そのものを止めてはいけない。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from magickit.adapters.lexora import LexoraAdapter
from magickit.config import get_settings
from magickit.core.decision_summaries import DecisionSummaryStore
from magickit.utils.logging import get_logger
from magickit.web import decisions
from magickit.web.deps import templates

logger = get_logger(__name__)

router = APIRouter(tags=["decisions"])

#: 要求する tier。``digest_producer.REQUESTED_TIER`` と同じ考え方で、これは
#: **要求であって観測ではない** —— Lexora が実際どのモデルに流したかは応答の
#: ``model`` を見る。
REQUESTED_TIER = "light"

#: 出力の形を**デコードの段階で**縛る。
#:
#: 実測 (2026-09-14, Qwen3.8-27B): prompt で「JSON のみ」と指示しただけでは
#: 4 選択肢のうち 4 つ目の ``"label"`` の後のカンマが欠けた JSON が返り、
#: 内容は正しいのに要約が丸ごと捨てられた。**散文を後から修復するのではなく
#: 構造を出させない**のが正しい対処 —— 修復は「壊れ方の種類」を当て続ける
#: 賭けになるし、当たらなかった日に黙って要約が消える。
#:
#: Lexora はこの field をそのまま vLLM へ通す (実測: ``json_object`` も
#: ``json_schema`` も 200)。通らない backend に切り替わったら例外が上がり、
#: ルート側の except が fragment を返す ∴ ページは壊れない。
_SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["question", "options"],
    "additionalProperties": False,
    "properties": {
        "question": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "label", "gain", "loss"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "gain": {"type": "string"},
                    "loss": {"type": "string"},
                },
            },
        },
    },
}

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "decision_summary", "schema": _SUMMARY_SCHEMA},
}

_SYSTEM_PROMPT = """\
あなたは判断材料の要約係です。決定しません。情報を足しません。

与えられた材料だけを使い、同じ構造のまま短くします。**材料に無いことは
書かないでください。** 分からない箇所は短いまま残してください。

出力は JSON のみ。{ で始まり } で終わること。

{
  "question": "向き合っている問題を 2 文以内で。経緯は書かない",
  "options": [
    {"id": "元の id をそのまま", "label": "何をするかを 40 文字以内で",
     "gain": "得るもの 1 文", "loss": "失うもの 1 文"}
  ]
}

規則:
1. option の id は元の材料と同じものを、同じ数だけ返します。増やさない、
   減らさない、振り直さない。
2. 短くするのは言い換えであって判断ではありません。どの選択肢が良いかを
   書かない。推奨を作らない。順番を変えない。
3. 元が空の欄は空のままにします。埋めない。
"""


def build_user_prompt(material: dict[str, Any]) -> str:
    """材料を要約の入力文にする。**スレッドは渡さない。**

    渡すのは画面に出ているものと同じ材料 ∴ 要約と詳細は同じ文章の 2 つの
    見え方になり、読み手が突き合わせて検算できる。スレッドから独立に書き
    直させると「同じ問いに対する 2 つの答え」になり、食い違ったときにどちら
    が正しいか読み手には決められない。
    """
    lines = [f"問い:\n{material.get('question') or '(無し)'}", "", "選択肢:"]
    for opt in material.get("options") or []:
        if not isinstance(opt, dict):
            continue
        lines.append(f"- id: {opt.get('id')}")
        lines.append(f"  label: {opt.get('label')}")
        lines.append(f"  得るもの: {opt.get('gain')}")
        lines.append(f"  失うもの: {opt.get('loss')}")
    return "\n".join(lines)


def parse_summary(text: str, material: dict[str, Any]) -> dict[str, Any] | None:
    """モデルの応答を要約に変換する。**信用しないで照合する。**

    捨てるのは次の場合:

    - JSON が見つからない / object でない
    - ``options`` が元の材料に無い id を含む —— 要約が**存在しない選択肢を
      作った**ということで、並べて比較する相手が居ない。これを黙って通すと
      「詳細と突き合わせて検算できる」という前提が崩れる。

    元にあって要約に無い id は**落とさずに埋める**のではなく、そのまま欠けた
    状態で返す。埋めると要約が元と同じ長さになり意味が無いし、モデルが落と
    した事実を隠すことにもなる。テンプレート側が「元 N 件のうち M 件」と出す。
    """
    match = re.search(r"\{.*\}", text or "", re.S)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    known = {
        str(o.get("id"))
        for o in (material.get("options") or [])
        if isinstance(o, dict) and o.get("id")
    }
    options: list[dict[str, str]] = []
    for opt in payload.get("options") or []:
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("id") or "")
        if oid not in known:
            logger.warning("decision summary invented an option id", option_id=oid)
            return None
        options.append(
            {
                "id": oid,
                "label": str(opt.get("label") or ""),
                "gain": str(opt.get("gain") or ""),
                "loss": str(opt.get("loss") or ""),
            }
        )

    question = str(payload.get("question") or "").strip()
    if not question and not options:
        return None
    return {"question": question, "options": options, "source_option_count": len(known)}


def _fragment(request: Request, context: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/decision_summary.html",
        context,
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@router.post("/dashboard/decisions/{project}/{thread_id}/_summary")
async def generate_decision_summary(
    request: Request, project: str, thread_id: str
) -> HTMLResponse:
    """材料の簡潔版を作って fragment で返す。"""
    settings = get_settings()

    if not settings.decision_summary_enabled:
        # 404 ではなく fragment。描画されたボタンが 404 を返すと、設定では
        # なくページの不具合に見える (CLAUDE.md の loop-control 405 の罠)。
        return _fragment(request, {"error": "要約は無効化されています"})

    material, _ok = await decisions._load_material(project, thread_id)
    if not material:
        return _fragment(request, {"error": "材料がありません"})

    head_msg_id = str(material.get("head_msg_id") or "")
    store = DecisionSummaryStore(db_path=settings.db_path)

    cached = await store.get(
        project=project, thread_id=thread_id, head_msg_id=head_msg_id
    )
    if cached is not None:
        return _fragment(request, {"summary": cached, "cached": True})

    lexora = LexoraAdapter(
        base_url=settings.lexora_url,
        # `lexora_timeout` (既定 240s) は重い文書処理向けの天井。要約は人が
        # ボタンを押して待っているので継承しない —— digest_producer が同じ
        # 理由で cognilens_timeout を継承していないのと同じ。
        timeout=settings.decision_summary_timeout_seconds,
    )
    try:
        text = await asyncio.wait_for(
            lexora.chat(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(material)},
                ],
                max_tokens=settings.decision_summary_max_tokens,
                temperature=0,
                model=REQUESTED_TIER,
                response_format=RESPONSE_FORMAT,
            ),
            timeout=settings.decision_summary_timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "decision summary timed out", project=project, thread_id=thread_id
        )
        return _fragment(
            request,
            {
                "error": (
                    f"{int(settings.decision_summary_timeout_seconds)} 秒以内に"
                    "要約が返りませんでした"
                )
            },
        )
    except Exception as e:  # noqa: BLE001 - 要約の失敗で判断を止めない
        logger.error(
            "decision summary failed",
            project=project,
            thread_id=thread_id,
            error=str(e),
        )
        return _fragment(request, {"error": f"要約を作れませんでした ({e})"})
    finally:
        await lexora.close()

    summary = parse_summary(text, material)
    if summary is None:
        return _fragment(request, {"error": "要約の形が読めませんでした"})

    summary["tier"] = REQUESTED_TIER
    summary["model"] = None
    await store.put(
        project=project,
        thread_id=thread_id,
        head_msg_id=head_msg_id,
        payload=summary,
        tier=REQUESTED_TIER,
        model=None,
    )
    stored = await store.get(
        project=project, thread_id=thread_id, head_msg_id=head_msg_id
    )
    return _fragment(request, {"summary": stored or summary, "cached": False})


__all__ = [
    "router",
    "build_user_prompt",
    "parse_summary",
    "REQUESTED_TIER",
    "RESPONSE_FORMAT",
]
