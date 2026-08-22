r"""判断 (decision) ページ ― S5 増分 2 本体。

Discord alert の通知本文が指す `/dashboard/decisions/{project}/{thread_id}`
を、増分 1 のリダイレクト stub から**判断 UI 本体**に差し替える。URL は増分 1
で敷いた契約のまま (`msg-087 §1` / spec `spec/slices/S5-decision-page.md`)、
ハンドラの中身だけを差し替える。

このモジュールに置くべき理由 (触る前に読む 2 行)
--------------------------------------------------

- **この URL は出荷済みの契約である。** 動かさない。増分 1 と増分 2 の違いは
  「同じ URL のハンドラが何を返すか」だけであって、URL そのものではない。
- **CI はページが外から見えるかを知らない。** 到達確認は :8443 に curl -L で
  外から叩き、最終 200 とタイトルを確かめる。CI 緑は必要条件、十分条件ではない
  (msg-084 §5。「実装済みに見えて何も出さない」型の事故は CI を素通りする)。

D-26' の 4 分岐 (spec §1)
--------------------------

Bohr の 3 分岐は「答えが得られたときの分類」であり、実装は必ず 4 通りになる:

- 存在し、駐機中 → 200 判断 UI
- 存在するが駐機中でない → 200 「判断待ちではありません」+ chatroom 導線
- 存在しない (Conclair が明示的に「無い」と答えた) → **404**
- 取得できなかった (不達 / 例外 / それ以外の error envelope) → **503** + `/ui` 直リンク

**4 番目を 404 に丸めない。** 丸めると「Conclair が落ちている間、判断依頼の URL が
『そんなスレッドは無い』と言う」ことになる — 我々が知らないことを断定する形になり、
「存在しない」を「決着した」と言わない (msg-084 §2 D-26') と同種の誤りである。
`ChatroomAdapter` は error envelope を dict のまま返す ∴ 分岐は
`"error_type" in result` で行い、成功形の欠けた 200 を「該当なし」と読まない。

404 と 503 の判別 (Einstein msg-095 §3)
----------------------------------------

Conclair が「存在しない」と答えた場合も error envelope として届く ∴ `error_type`
の**中身**を見て切り分ける。NotFound 相当を含むもの (case-insensitive substring
match) を 404、それ以外の envelope・httpx 例外・タイムアウトは 503。envelope を
一律 503 にすると増分 2 で新設した 404 分岐が永遠に通らず、逆に一律 404 にすると
Conclair 障害を「存在しない」と偽装する。

駐機の判定 (spec §1 / msg-096 §2)
--------------------------------

**mindwire (`parked_humans.py`) が SOT。** magickit は単一スレッド判定のために
mindwire を呼ばない (repo 跨ぎの依存を作らない・msg-084 §4)。

かわりに magickit が答える述語は「駐機か」ではなく**「まだ誰も答えていないか」** —
より弱く、より安定した形にする:

1. 最終 msg の**構造化 `next_participant` field が `human`** を指すなら駐機。
   PR #28 で入った field を第一根拠に置くことで mindwire と magickit が同じ構造化
   データを見る形になり、正規表現の二重実装を主経路から外せる。
2. field を持たない旧 msg に限り、本文の**単独行 `^\s*NEXT:\s*human\s*$`**
   (case insensitive) を fallback で読む。この判定は `chatroom_writes` の D-30
   判定と**同一実装を共有**する (2 箇所で別々に書かない・spec §1)。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.utils.logging import get_logger
from magickit.web.deps import templates

logger = get_logger(__name__)

router = APIRouter(tags=["decisions"])

#: 増分 1 から引き継ぐ Cache-Control。ハンドラ本体が実ページに変わっても、
#: 302 の永久キャッシュを防ぐ趣旨は生きる (増分 3 で URL 契約が変わる余地を残す)。
_NO_STORE = "no-store, must-revalidate"

#: 「NotFound 相当」を判定する substring 群。Conclair の error_type 命名は
#: バージョンで揺れる ∴ `not_found` / `notfound` / `thread_not_found` などを
#: すべて捕捉する。逆に他の envelope (e.g. `ChatroomIntegrityError`) は 503 に
#: 落ちる。case-insensitive 比較。
_NOT_FOUND_MARKERS = ("not_found", "notfound", "does_not_exist", "doesnotexist")

#: D-30 と共有する単独行の handoff-target 判定。行頭の空白は許す (spec §1 / msg-094 §4)。
#: これも含めて **judgement page 内で consumer 用語を扱う helpers はこの module に
#: 集約する** — chatroom_writes.py には持ち込まない (msg-072 §1 の enforcement code
#: 3 ファイル制約: adapter / MCP tool / web/chatroom_writes.py に consumer 用語の
#: 直接 literal を書かない。judgement page は決定 UI 特化の別ファイル ∴ ここに置く
#: のが妥当。次の人が「なぜここに集めているのか」を読む手掛かり)。
_NEXT_KEYWORD = "NEXT:"
_NEXT_LINE_RE = re.compile(rf"^\s*{re.escape(_NEXT_KEYWORD)}\s", re.IGNORECASE)

#: 「駐機扱い」と読む human identity 名。CLAUDE.md の HUMAN_IDENTITY_NAMES と
#: 揃える (現状 `("human",)`)。ここに列挙する ∴ chatroom_tools 側が広がれば
#: そちらの定数を直接使うか、両者を揃える。
_HUMAN_IDENTITIES = frozenset(chatroom_tools.HUMAN_IDENTITY_NAMES)


def _is_error(result: dict[str, Any]) -> bool:
    """Conclair は failure を error envelope で返す (success flag ではない)。

    ChatroomAdapter の契約 (CLAUDE.md 冒頭): 4xx/5xx でも raise せず dict のまま
    返し、``"error_type" in result`` で分岐する。
    """
    return isinstance(result, dict) and "error_type" in result


def _is_not_found(envelope: dict[str, Any]) -> bool:
    """error envelope が「明示的な存在しない」に相当するか。

    Conclair の error_type の中身を見る。マッチしないものは「取得できなかった」
    (503) に落ちる — 我々が確認できなかったことを「存在しない」と偽装しない
    (msg-093 §2 の一般則を体現する箇所の 1 つ)。
    """
    error_type = str(envelope.get("error_type", "")).lower().replace("-", "_")
    return any(marker in error_type for marker in _NOT_FOUND_MARKERS)


def _has_standalone_next_line(body: str) -> bool:
    """本文に単独行の handoff-target 行が既にあるか。D-30 判定の共有実装。

    `chatroom_writes` の D-30 追記判定と同じ規則を共有する (spec §1 / msg-096 §2)。
    行頭の空白は許す。文中に該当キーワードが現れるだけの行は該当させない。

    **共有先**: `chatroom_writes.post_message` の opt-in 分岐がこれを import して
    使う (msg-072 §1 の enforcement-code vocabulary 制約を守るため、判定ロジック
    と literal はこの module に集める)。
    """
    for line in body.splitlines():
        if _NEXT_LINE_RE.match(line):
            return True
    return False


def _compose_decision_body(content: str, freeform: str) -> str:
    """判断ページ由来の POST で ``content`` と ``_freeform`` を 1 本にする。

    spec §3.3:
      - `content` が空 → `_freeform` のみ
      - `_freeform` が空 → `content` のみ (= 現行と同一の本文)
      - どちらも空 → `""` のまま下流に渡す (新たに拒否を作らない)
      - 両方 → ``f"{content}\\n\\n{freeform}"``

    I-6 (原文を保つ): trim しない。連結のみ。
    """
    if not content and not freeform:
        return ""
    if not content:
        return freeform
    if not freeform:
        return content
    return f"{content}\n\n{freeform}"


def _maybe_append_next(body: str, next_participant: str) -> str:
    """D-30: 単独行 handoff-target が無いときだけ末尾に空行 + 1 行追記する。

    - 人の文章そのものは 1 文字も変えない (I-6)。追記は末尾への連結のみ、trim もしない。
    - ``next_participant`` が空なら追記しない (追記する名前が無い)。
    - 単独行 handoff-target が既にあれば追記しない (last-wins parser を黙って上書きしない)。

    **判定と literal の集約先**: msg-072 §1 の enforcement-code 3 ファイル制約
    (adapter / MCP tool / chatroom_writes) に literal を持ち込まないため、この
    module に集約する (module docstring §「D-26' の 4 分岐」参照)。
    """
    if not next_participant:
        return body
    if _has_standalone_next_line(body):
        return body
    if not body:
        return f"{_NEXT_KEYWORD} {next_participant}"
    return f"{body}\n\n{_NEXT_KEYWORD} {next_participant}"


def _is_parked_to_human(last_msg: dict[str, Any]) -> bool:
    """最終 msg が human 宛の駐機を表しているか。

    1. 構造化 ``next_participant`` field が human を指す → True (第一根拠)。
    2. field を持たない旧 msg に限り、本文の単独行 handoff-target = human を fallback。

    第一根拠を優先することで、mindwire (``parked_humans.py``) と magickit が
    同じ構造化データを見る形になる ∴ 乖離余地は fallback 経路にだけ残る。
    """
    np = last_msg.get("next_participant")
    if isinstance(np, str) and np:
        # field が明示されているなら fallback を見に行かない。
        # human 宛でなければここで駐機ではない (別の identity 宛の handoff)。
        return np.lower() in _HUMAN_IDENTITIES
    # field 無し (= 旧 msg) → 本文 fallback
    body = str(last_msg.get("content") or "")
    # 単独行の handoff-target が human を名指ししているかを見る。
    fallback_re = re.compile(
        rf"^\s*{re.escape(_NEXT_KEYWORD)}\s*human\s*$", re.IGNORECASE
    )
    for line in body.splitlines():
        if fallback_re.match(line):
            return True
    return False


def _parked_author(last_msg: dict[str, Any]) -> str:
    """駐機 msg の著者 (select の既定値 / D-30 追記名)。

    Bohr §4: 「select の既定値は駐機 msg の著者で、D-30 が追記する名前と同じ値を
    使う (2 箇所で別々に決めない)」。ユーザが select を変えれば D-30 の値も追従する。
    """
    return str(last_msg.get("author") or "")


def _participant_choices(
    messages: list[dict[str, Any]], parked_author: str
) -> list[str]:
    """select に並べる identity 一覧。

    値域は登録済 identity のみ (msg-084 §2)。厳密には Prismind への問合せが要るが:

    - Prismind をクリティカルパスに置くと、Prismind 落ちで判断ページが描けなくなる。
    - 万一未登録の名前を選んでも、POST 時に ``NextParticipantUnknownError`` (未登録)
      で弾かれ、D-31 の入力保持再描画に落ちる ∴ 安全側の failure mode。

    ∴ 実装形は「thread に現れる distinct な author + ``human``」で近似する。
    thread に参加している actor は概ね登録済で、``human`` は必ず含める (人が
    自分に返すケースがある = 「まだ考え中」を残す判断)。``parked_author`` は
    先頭に置き、select の既定値と一致させる。``none`` / ``pr-review <ref>`` の
    reserved は出さない (magickit が弾く。終端は closes_thread で表現する)。
    """
    seen: dict[str, None] = {}
    if parked_author:
        seen[parked_author] = None
    for msg in messages:
        author = str(msg.get("author") or "").strip()
        if not author or author in seen:
            continue
        # reserved words を出さない (magickit が弾く。spec §2.3)。
        if author == "none" or author.startswith("pr-review"):
            continue
        seen[author] = None
    # human は必ず含める (parked_author が human なら既に入っている)。
    for h in _HUMAN_IDENTITIES:
        if h not in seen:
            seen[h] = None
    return list(seen.keys())


def _thread_page_url(project: str, thread_id: str) -> str:
    """`/ui` 側のスレッドページ URL を組み立てる。

    増分 1 と同じく ``quote(safe="")`` で各セグメントを自前 encode。
    ``RedirectResponse`` を使わないのも増分 1 の理由と同じ (D-C)。
    """
    return (
        f"/ui/projects/{quote(project, safe='')}"
        f"/threads/{quote(thread_id, safe='')}"
    )


async def _load_judgement_context(project: str, thread_id: str) -> dict[str, Any]:
    """判断 UI に必要な参考情報を Conclair から取り直す。

    D-31 のエラー再描画で、入力保持だけでなく thread 文脈 (parked msg / 参加者
    一覧 / タイトル) も可能な限り復元するために使う。取れなかった場合の fallback
    は呼び出し側が持つ ∴ ここでは例外を握らず raise させる (呼び出し側が最低限の
    再描画に落ちる)。
    """
    adapter = chatroom_tools._adapter()
    try:
        return await adapter.get_thread(
            project=project, thread_id=thread_id, mode="full"
        )
    finally:
        await adapter.close()


async def _render_decision_error_page(
    request: Request,
    *,
    project: str,
    thread_id: str,
    content_value: str,
    freeform_value: str,
    next_participant_value: str,
    error_message: str,
    status_code: int = 400,
) -> HTMLResponse:
    """判断 UI を D-31 のエラー再描画で使う。

    ``chatroom_writes`` の POST ハンドラが `_check_next_participant` 系エラーを
    受けた時にこれを呼ぶ。入力値をすべて保持したまま再描画する — モバイルで
    長文を打った直後に消えるのが最悪の失敗 (msg-094 §5)。

    thread 文脈 (parked author / 参加者一覧 / タイトル) は Conclair から取り直す。
    Conclair も落ちていれば、最低限の form (single-entry select + textarea) に
    フォールバックする ∴ 入力が消えることは無い。
    """
    # 文脈の復元は best-effort。落ちても入力保持が優先。
    parked_author = next_participant_value
    participant_choices = [next_participant_value] if next_participant_value else list(_HUMAN_IDENTITIES)
    parked_msg_content = ""
    thread_title = thread_id
    try:
        result = await _load_judgement_context(project, thread_id)
    except Exception as e:  # noqa: BLE001 - 復元失敗は最低限 form に落ちる
        logger.warning(
            "decision error re-render: get_thread raised, falling back",
            project=project, thread_id=thread_id, error=str(e),
        )
    else:
        if not _is_error(result):
            messages = result.get("messages") or []
            thread_meta = result.get("thread") or {}
            thread_title = thread_meta.get("title") or thread_id
            if messages:
                last = messages[-1]
                candidate_parked = _parked_author(last)
                if candidate_parked:
                    parked_author = candidate_parked
                parked_msg_content = str(last.get("content") or "")
                participant_choices = _participant_choices(messages, parked_author)
    # 「human」だけは常に含める。
    for h in _HUMAN_IDENTITIES:
        if h not in participant_choices:
            participant_choices.append(h)
    # next_participant_value も候補に無ければ加える (D-31 で消さない)。
    if next_participant_value and next_participant_value not in participant_choices:
        participant_choices.insert(0, next_participant_value)

    return templates.TemplateResponse(
        request,
        "decisions_thread.html",
        {
            "mode": "judgement",
            "project": project,
            "thread_id": thread_id,
            "thread_ui_url": _thread_page_url(project, thread_id),
            "thread_title": thread_title,
            "parked_author": parked_author,
            "parked_msg_content": parked_msg_content,
            "participant_choices": participant_choices,
            "content_value": content_value,
            "freeform_value": freeform_value,
            "next_participant_value": next_participant_value or parked_author,
            "error_message": error_message,
            # 選択肢セットは元 GET と同じ (差し戻された時に選び直せるように)。
            "choice_options": [
                {"label": "A: そのまま進める", "value": "A: そのまま進める"},
                {"label": "B: 一旦止める / 修正が要る", "value": "B: 一旦止める / 修正が要る"},
            ],
        },
        status_code=status_code,
        headers={"Cache-Control": _NO_STORE},
    )


@router.get("/dashboard/decisions/{project}/{thread_id}", response_class=HTMLResponse)
async def decision_page(
    request: Request, project: str, thread_id: str
) -> Response:
    """D-26' の 4 分岐で判断ページを返す (spec §1)。

    ``ChatroomAdapter.get_thread`` を叩き:

    - 例外 / それ以外の error envelope → **503** + `/ui` 直リンク
    - error envelope の中身が NotFound 相当 → **404**
    - 成功 (thread + messages) → 駐機なら判断 UI (200) / 駐機でないなら
      「判断待ちではありません」(200)

    404 と 503 の判別は `_is_not_found` に集約する。
    """
    thread_ui_url = _thread_page_url(project, thread_id)

    adapter = chatroom_tools._adapter()
    try:
        try:
            result = await adapter.get_thread(
                project=project, thread_id=thread_id, mode="full"
            )
        except Exception as e:  # noqa: BLE001 - 取得できなかったを 503 に丸める
            logger.warning(
                "decision page: get_thread raised",
                project=project,
                thread_id=thread_id,
                error=str(e),
            )
            return templates.TemplateResponse(
                request,
                "decisions_thread.html",
                {
                    "mode": "unavailable",
                    "project": project,
                    "thread_id": thread_id,
                    "thread_ui_url": thread_ui_url,
                    "error_message": f"Conclair に接続できませんでした ({e})",
                },
                status_code=503,
                headers={"Cache-Control": _NO_STORE},
            )
    finally:
        await adapter.close()

    if _is_error(result):
        if _is_not_found(result):
            # 「存在しない」の明示的回答 → 404 (spec §1 / D-26')。
            return templates.TemplateResponse(
                request,
                "decisions_thread.html",
                {
                    "mode": "not_found",
                    "project": project,
                    "thread_id": thread_id,
                    "thread_ui_url": thread_ui_url,
                    "error_message": (
                        f"スレッド {thread_id!r} は存在しません。"
                    ),
                },
                status_code=404,
                headers={"Cache-Control": _NO_STORE},
            )
        # NotFound 以外の envelope → 「取得できなかった」= 503。
        # 404 に丸めると「Conclair 障害中に『そんなスレッドは無い』と言う」ことになる
        # (msg-093 §2 の一般則の実体)。
        return templates.TemplateResponse(
            request,
            "decisions_thread.html",
            {
                "mode": "unavailable",
                "project": project,
                "thread_id": thread_id,
                "thread_ui_url": thread_ui_url,
                "error_message": (
                    f"Conclair からエラーが返りました: "
                    f"{result.get('error_type', '?')}: {result.get('error', '')}"
                ),
            },
            status_code=503,
            headers={"Cache-Control": _NO_STORE},
        )

    # 成功形。messages が無いのは駐機ではない (open されただけの thread など)。
    messages = result.get("messages") or []
    thread_meta = result.get("thread") or {}

    if not messages or not _is_parked_to_human(messages[-1]):
        return templates.TemplateResponse(
            request,
            "decisions_thread.html",
            {
                "mode": "not_waiting",
                "project": project,
                "thread_id": thread_id,
                "thread_ui_url": thread_ui_url,
                "thread_title": thread_meta.get("title") or thread_id,
            },
            status_code=200,
            headers={"Cache-Control": _NO_STORE},
        )

    # 駐機中 → 判断 UI (200)。
    last = messages[-1]
    parked_author = _parked_author(last)
    return templates.TemplateResponse(
        request,
        "decisions_thread.html",
        {
            "mode": "judgement",
            "project": project,
            "thread_id": thread_id,
            "thread_ui_url": thread_ui_url,
            "thread_title": thread_meta.get("title") or thread_id,
            "parked_author": parked_author,
            "parked_msg_content": str(last.get("content") or ""),
            "participant_choices": _participant_choices(messages, parked_author),
            # 空の初期状態 (D-31 再描画ではないので入力保持なし)。
            "content_value": "",
            "freeform_value": "",
            "next_participant_value": parked_author,
            "error_message": None,
            # 選択肢は将来「提示された選択肢」を Bohr の proposal から抽出して
            # 差し込む余地を残す。増分 2 では固定 3 択 (「そのまま進める」/
            # 「一旦止める」/ 空)。実運用では自由記述だけで送るケースが多い ∴
            # I-12 (空選択で送れる) が最優先で、選択肢セットは薄く保つ。
            "choice_options": [
                {"label": "A: そのまま進める", "value": "A: そのまま進める"},
                {"label": "B: 一旦止める / 修正が要る", "value": "B: 一旦止める / 修正が要る"},
            ],
        },
        status_code=200,
        headers={"Cache-Control": _NO_STORE},
    )


@router.get("/dashboard/decisions")
async def decisions_index_redirect() -> Response:
    """一覧 URL は据え置き (増分 3)。302 → `/dashboard`。

    増分 1 と同一の挙動。増分 3 で本物の一覧に差し替える。それまで判断待ちを
    見に来た人が少なくとも何かに着地するようにする (msg-084 §1)。
    """
    return Response(
        status_code=302,
        headers={"Location": "/dashboard", "Cache-Control": _NO_STORE},
    )


__all__ = [
    "router",
    "_is_parked_to_human",
    "_has_standalone_next_line",
    "_is_not_found",
    "_render_decision_error_page",
    "_thread_page_url",
    "_participant_choices",
]
