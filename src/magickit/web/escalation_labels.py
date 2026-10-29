r"""判断ページの「聞くべきだったか」ラベル — 単位 A (spirrow-magickit).

thread: spirrow-magickit ``T-decision-page-escalation-feedback-label``.
正本は msg-1407 (最終まとめ) に、msg-1411 / msg-1416 / msg-1445 /
msg-1448 / msg-1451 の差し替えを重ねたもの。Tier-C (goal) は msg-1486 で
human が承認済み。

何をするか
----------

- 判断ページ (``/dashboard/decisions/{p}/{t}``) の回答フォームに、ラベル
  (``_esc_label``)・理由タグ (``_esc_tags``)・1 行メモ (``_esc_note``)・
  対象 (``_esc_target``) を足す。回答と同じ送信で付く (R1 手数最小)。
- 単独のラベル画面 ``/dashboard/decisions/{p}/{t}/label?msg=`` で、回答後に
  付ける・付け直す・取り消す。
- 読み出し ``GET /v1/escalation-labels`` と ``/v1/escalation-labels/summary``
  (R5)。

SOT は :mod:`magickit.core.escalation_labels` の追記専用テーブル。Conclair
には何も書かない。

対象 msg の決め方 (msg-1445 §3.2''' → msg-1448 → msg-1451)
-----------------------------------------------------------

「エスカレーション msg」の定義は :func:`_is_escalation_msg` 1 か所にだけ
置く: **author が** ``escalation_label_authors`` **に入っていて、最終行が**
``NEXT: human``。relay / bot (``pr-gate-relay`` / ``conductor-relay`` /
``ci-window-state`` …) は author で落ちる。stop マーカーは見ない
(msg-1451: relay の 3 通はマーカー無しで ``NEXT: human`` で終わっていた。
逆に 3 役がマーカーを本文で引用しても対象から外れない)。

使う側は 2 つで、どちらもこの述語を共有する:

- :func:`_resolve_escalation_target` — 判断ページのメインフォーム用。「今の
  対象」を探す。起点 (fresh な material の head、そうでなければ末尾) から
  古い方へ、許可リスト外の author の msg を飛ばして遡り、**最初に見つかった
  許可リスト内の msg 1 通だけ**に述語を当てる (それより先は見ない)。
- :func:`_verify_label_target` — ラベル画面と POST の書き込み直前用。指定
  された msg 1 通を、スレッドの先端からの距離を見ずに確かめる。回答後に
  過去のエスカレーションへ付け直せるのはこちらのおかげ (msg-1448)。

「最終行」の読み方
------------------

3 役の msg の本文は、``NEXT:`` 行の後ろに 1 行の HTML コメント
(``<!-- source: … -->`` / ``<!-- attest: … -->``) を伴うことがある。これは
投稿経路が付けるメタデータで、本文の最終行ではない ∴ 末尾の空行と、
**1 行で閉じた HTML コメントの行**を飛ばした後の行を最終行とする。複数行に
またがるコメントは飛ばさない (今その形の付記は無い。広げるのは実測してから)。

不変条件
--------

- **L-1** (msg-1401): 成功の 303 を返すのは「計画どおりに行を書けた」か
  「:func:`_label_write_plan` が no-op と判定した」ときだけ。書くと判定した
  のに書けなかった (rejected / unverifiable / store 例外 / 入力不正) ときは、
  必ず人に見える形で知らせる。
- **S-1** (msg-1403): 同じ target について、後から増えた行のスナップショット
  が前の行より弱くなることはない (:func:`_resolve_snapshot`)。
- 回答はラベルに左右されない: 判断ページからの POST は、ラベル側が何で
  失敗しても回答の成否を変えない。

chatroom_writes との境界
------------------------

``chatroom_writes.py`` は msg-072 §1 の語彙制約で consumer 用語の literal
(``NEXT:`` 相当) を持てない ∴ 判定はすべてこの module に置き、
``chatroom_writes`` は :func:`label_input_from_form` /
:func:`prepare_decision_label` / :func:`commit_decision_label` を呼ぶだけに
する。
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from magickit.config import get_settings
from magickit.core.escalation_labels import EscalationLabelStore
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.utils.logging import get_logger
from magickit.web import decisions as decisions_module
from magickit.web.deps import templates

logger = get_logger(__name__)

router = APIRouter(tags=["escalation-labels"])

_NO_STORE = "no-store, must-revalidate"

# ---------------------------------------------------------------------------
# §3.6 ワイヤ値 — 定数は 1 か所。テンプレートもハンドラもここから読む。
# ---------------------------------------------------------------------------

#: 変更しない (どちらの画面でも「書かない」)。
LABEL_NONE = "none"
LABEL_SHOULD_ASK = "should_ask"
LABEL_SHOULD_NOT_ASK = "should_not_ask"
LABEL_BORDERLINE = "borderline"
#: 取り消す (``label='cleared'`` の行を書く)。current があるときだけ描画する。
LABEL_CLEARED = "cleared"

#: ラベルを付ける 3 値 (表示順)。
LABEL_VALUES: tuple[str, ...] = (LABEL_SHOULD_ASK, LABEL_SHOULD_NOT_ASK, LABEL_BORDERLINE)
#: ワイヤ上で受け付ける全値。
WIRE_VALUES: frozenset[str] = frozenset((LABEL_NONE, *LABEL_VALUES, LABEL_CLEARED))

LABEL_DISPLAY: dict[str, str] = {
    LABEL_NONE: "未ラベル",
    LABEL_SHOULD_ASK: "聞くべきだった",
    LABEL_SHOULD_NOT_ASK: "聞かなくてよかった",
    LABEL_BORDERLINE: "微妙",
    LABEL_CLEARED: "ラベルを取り消す",
}

#: R2: ``should_not_ask`` のときの理由タグ。
TAGS_SHOULD_NOT_ASK: tuple[tuple[str, str], ...] = (
    ("not_in_rules", "五ヶ条のどれにも当たらない"),
    ("impl_detail", "実装の細部"),
    ("review_finding", "レビュー指摘への対応可否"),
    ("proceed_check", "聞くまでもない確認（「進めますか？」型）"),
    ("other", "その他"),
)
#: R2: ``should_ask`` のときの理由タグ (当たる条)。
TAGS_SHOULD_ASK: tuple[tuple[str, str], ...] = tuple(
    (f"rule_{n}", f"第 {n} 条") for n in range(1, 6)
)
_TAG_SET_FOR_LABEL: dict[str, frozenset[str]] = {
    LABEL_SHOULD_NOT_ASK: frozenset(v for v, _ in TAGS_SHOULD_NOT_ASK),
    LABEL_SHOULD_ASK: frozenset(v for v, _ in TAGS_SHOULD_ASK),
}
_ALL_TAGS: frozenset[str] = frozenset().union(*_TAG_SET_FOR_LABEL.values())

#: 1 行メモの上限 (msg-1407 §1 ``note(≤200)``)。
NOTE_MAX = 200

#: ``snapshot_source`` の 3 値 (msg-1393 §1)。
SNAPSHOT_FRESH = "material_fresh"
SNAPSHOT_MISMATCH = "material_mismatch"
SNAPSHOT_ABSENT = "material_absent"

#: ``_verify_label_target`` の 3 値 (msg-1407 §3.5 / msg-1448)。
VERIFY_FOUND = "found"
VERIFY_REJECTED = "rejected"
VERIFY_UNVERIFIABLE = "unverifiable"

#: ラベルが書けなかった理由のコード。ラベル画面の ``?error=`` にも載る。
ERR_REJECTED = VERIFY_REJECTED
ERR_UNVERIFIABLE = VERIFY_UNVERIFIABLE
ERR_STORE = "store"
ERR_INVALID_LABEL = "invalid_label"
ERR_NOTE_TOO_LONG = "note_too_long"

ERROR_MESSAGES: dict[str, str] = {
    ERR_REJECTED: (
        "この msg はラベルの対象 (3 役が human に上げた msg) ではありません。"
        "保存していません。"
    ),
    ERR_UNVERIFIABLE: (
        "この msg をスレッドの取得範囲内で確認できませんでした。保存していません。"
    ),
    ERR_STORE: "ラベルを保存できませんでした (store の例外)。回答は送信済みです。",
    ERR_INVALID_LABEL: "ラベルの値が不正です。保存していません。",
    ERR_NOTE_TOO_LONG: f"メモは {NOTE_MAX} 文字以内にしてください。保存していません。",
}


# ---------------------------------------------------------------------------
# store accessor (``decisions._get_material_store`` と同じ形)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _get_label_store() -> EscalationLabelStore:
    """process-singleton の ``EscalationLabelStore``。

    ``decisions._get_material_store`` と同じ理由で無引数・lru_cache。tests は
    この関数をまるごと差し替える (``tests/unit/conftest.py``)。
    """
    return EscalationLabelStore(db_path=get_settings().db_path)


def _label_authors() -> frozenset[str]:
    """``escalation_label_authors`` を casefold した集合。"""
    return frozenset(
        str(name).strip().casefold()
        for name in get_settings().escalation_label_authors
        if str(name or "").strip()
    )


# ---------------------------------------------------------------------------
# 述語と、それを共有する 2 つの使い手
# ---------------------------------------------------------------------------

_TRAILING_COMMENT_LINE_RE = re.compile(r"^\s*<!--.*-->\s*$")
_HUMAN_HANDOFF_LINE_RE = re.compile(
    rf"^\s*{re.escape(decisions_module._NEXT_KEYWORD)}\s*"
    rf"(?:{'|'.join(re.escape(h) for h in sorted(decisions_module._HUMAN_IDENTITIES))})\s*$",
    re.IGNORECASE,
)


def _final_line(content: str) -> str:
    """末尾の空行と 1 行 HTML コメントを飛ばした最終行 (module docstring 参照)。"""
    lines = content.splitlines()
    while lines and (not lines[-1].strip() or _TRAILING_COMMENT_LINE_RE.match(lines[-1])):
        lines.pop()
    return lines[-1] if lines else ""


def _msg_author(msg: dict[str, Any]) -> str:
    return str(msg.get("author") or "").strip()


def _is_label_author(msg: dict[str, Any], authors: frozenset[str]) -> bool:
    return _msg_author(msg).casefold() in authors


def _is_escalation_msg(msg: dict[str, Any], authors: frozenset[str]) -> bool:
    """3 役が human に上げた msg か (msg-1451 の述語、唯一の定義)。

    1. author が ``escalation_label_authors`` に入っている
    2. 最終行が ``NEXT: human``
    """
    if not isinstance(msg, dict) or not _is_label_author(msg, authors):
        return False
    return bool(_HUMAN_HANDOFF_LINE_RE.match(_final_line(str(msg.get("content") or ""))))


def _resolve_escalation_target(
    messages: list[dict[str, Any]],
    thread_meta: dict[str, Any],
    material: dict[str, Any] | None,
    authors: frozenset[str],
    *,
    start_msg_id: str | None = None,
) -> dict[str, Any] | None:
    """判断ページのメインフォームが今ラベルを付ける対象 msg (無ければ ``None``)。

    1. 起点: material が fresh ならその head、そうでなければ末尾 msg。
       ``start_msg_id`` を渡すとそれを起点にする (P-L2'' (2) の head 固定)。
       起点が窓の中に見つからなければ ``None`` (確認できないものを対象に
       しない)。
    2. 起点から古い方へ、author が許可リストに無い msg を飛ばす。
    3. 最初に見つかった許可リスト内の msg に :func:`_is_escalation_msg` を
       当て、真ならそれ、偽なら ``None``。それより先は見ない。
    """
    if not messages:
        return None
    if start_msg_id is None:
        head = decisions_module._head_msg_id_from_thread(thread_meta)
        if decisions_module._classify_judgement_state(material, head) == "fresh":
            start_msg_id = str((material or {}).get("head_msg_id") or "")
    if start_msg_id is None:
        start = len(messages) - 1
    else:
        start = next(
            (
                i
                for i, m in enumerate(messages)
                if str(m.get("msg_id") or "") == start_msg_id
            ),
            None,
        )
        if start is None:
            return None
    for msg in reversed(messages[: start + 1]):
        if not _is_label_author(msg, authors):
            continue
        return msg if _is_escalation_msg(msg, authors) else None
    return None


def _verify_label_target(
    messages: list[dict[str, Any]] | None,
    thread_meta: dict[str, Any] | None,
    target_msg_id: str,
    authors: frozenset[str],
) -> tuple[str, dict[str, Any] | None]:
    """指定された msg がラベルの対象かを 3 値で返す (msg-1448)。

    - ``found``: スレッドにあり、:func:`_is_escalation_msg` が真。
    - ``rejected``: スレッドに無い、または述語が偽。
    - ``unverifiable``: スレッドが取れない、または取れた messages が
      ``msg_count`` より少ない (窓で切られていて「無い」と断定できない)。

    先端からの距離は見ない: 回答してスレッドが進んだ後でも、過去の
    エスカレーションには付けられる。
    """
    if messages is None:
        return VERIFY_UNVERIFIABLE, None
    for msg in messages:
        if str(msg.get("msg_id") or "") == target_msg_id and target_msg_id:
            if _is_escalation_msg(msg, authors):
                return VERIFY_FOUND, msg
            return VERIFY_REJECTED, None
    msg_count = (thread_meta or {}).get("msg_count")
    if isinstance(msg_count, int) and not isinstance(msg_count, bool):
        if msg_count > len(messages):
            return VERIFY_UNVERIFIABLE, None
    return VERIFY_REJECTED, None


# ---------------------------------------------------------------------------
# 書く / 書かない、タグの整合、スナップショット
# ---------------------------------------------------------------------------


@dataclass
class LabelInput:
    """人が送ってきたラベル入力。手を加えない (I-6)。"""

    label: str
    tags: list[str] = field(default_factory=list)
    note: str = ""
    target: str = ""


def label_input_from_form(
    label: str | None,
    tags: list[str] | None,
    note: str | None,
    target: str | None,
) -> LabelInput | None:
    """フォームの生値から :class:`LabelInput` を作る。ラベル欄が無ければ ``None``。"""
    if label is None:
        return None
    return LabelInput(
        label=label,
        tags=[t for t in (tags or []) if t],
        note=note or "",
        target=target or "",
    )


def _tags_mismatch(label: str, tags: list[str]) -> bool:
    """§3.3': ラベルとタグが合っていないか。合っていなくても保存はする。

    - ``should_ask`` / ``should_not_ask``: 自分の組以外のタグがあれば不整合。
    - ``borderline``: R2 にタグの組が無い ∴ どちらの組のタグも矛盾しない。
      知らないタグだけを不整合とする。
    - 未知のタグはどのラベルでも不整合。
    """
    allowed = _TAG_SET_FOR_LABEL.get(label, _ALL_TAGS)
    return any(t not in allowed for t in tags)


def _current(history_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """有効な current 行。最新行が ``cleared`` なら ``None`` (未ラベル)。"""
    if not history_rows:
        return None
    last = history_rows[-1]
    return None if last.get("label") == LABEL_CLEARED else last


def _label_write_plan(current: dict[str, Any] | None, submitted: LabelInput) -> bool:
    """書くなら ``True``、no-op なら ``False`` (msg-1401 §3.6)。

    1. ``none`` → 書かない
    2. ``cleared`` だが current が無い → 書かない
    3. current と label・tags・note がすべて同じ → 書かない
    4. それ以外 → 書く
    """
    if submitted.label == LABEL_NONE:
        return False
    if submitted.label == LABEL_CLEARED:
        return current is not None
    if current is not None and (
        current.get("label") == submitted.label
        and list(current.get("reason_tags") or []) == list(submitted.tags)
        and (current.get("note") or "") == submitted.note
    ):
        return False
    return True


def _empty_snapshot(source: str) -> dict[str, Any]:
    return {
        "snapshot_source": source,
        "rules_hash": None,
        "jev_p": None,
        "jev_rule": None,
        "jev_model_version": None,
    }


def _snapshot_from_material(material: dict[str, Any] | None, target: str) -> dict[str, Any]:
    """今の material からスナップショットを求める。例外を投げない (msg-1399)。

    1. material が None → ``material_absent``
    2. ``head_msg_id`` が空でない str でない → ``material_mismatch``
    3. ``head_msg_id != target`` → ``material_mismatch``
    4. それ以外 → ``material_fresh``。jev / ``rules_hash`` はある場合だけ。
    """
    if not isinstance(material, dict):
        return _empty_snapshot(SNAPSHOT_ABSENT)
    head = material.get("head_msg_id")
    if not isinstance(head, str) or not head or head != target:
        return _empty_snapshot(SNAPSHOT_MISMATCH)
    snap = _empty_snapshot(SNAPSHOT_FRESH)
    rules_hash = material.get("rules_hash")
    if isinstance(rules_hash, str):
        snap["rules_hash"] = rules_hash
    jev = material.get("jev")
    if isinstance(jev, dict):
        p = jev.get("should_ask_human")
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            snap["jev_p"] = float(p)
        if isinstance(jev.get("rule"), str):
            snap["jev_rule"] = jev["rule"]
        if isinstance(jev.get("model_version"), str):
            snap["jev_model_version"] = jev["model_version"]
    return snap


def _resolve_snapshot(
    history_rows: list[dict[str, Any]], material: dict[str, Any] | None, target: str
) -> dict[str, Any]:
    """§3.7: 新しい行のスナップショット (S-1: 劣化しない)。

    1. 履歴に ``material_fresh`` 行があれば、最も古いその行から引き継ぐ。
    2. 無ければ今の material を :func:`_snapshot_from_material` で評価する
       (fresh ならそれ、そうでなければ mismatch / absent で値は NULL)。
    """
    for row in history_rows:  # id 昇順
        if row.get("snapshot_source") == SNAPSHOT_FRESH:
            return {
                "snapshot_source": SNAPSHOT_FRESH,
                "rules_hash": row.get("rules_hash"),
                "jev_p": row.get("jev_p"),
                "jev_rule": row.get("jev_rule"),
                "jev_model_version": row.get("jev_model_version"),
            }
    return _snapshot_from_material(material, target)


# ---------------------------------------------------------------------------
# 書き込みの共通経路 (判断ページの POST とラベル画面の POST が共有する)
# ---------------------------------------------------------------------------


@dataclass
class LabelOutcome:
    """L-1 の判定に使う結果。``ok`` は「書けた」か「no-op」。"""

    ok: bool
    wrote: bool = False
    error: str | None = None
    row: dict[str, Any] | None = None


async def _fetch_thread(
    project: str, thread_id: str
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """``get_thread(mode="full")``。取れなければ ``(None, {})``。"""
    adapter = chatroom_tools._adapter()
    try:
        result = await adapter.get_thread(project=project, thread_id=thread_id, mode="full")
    except Exception as e:  # noqa: BLE001 - 取れない = unverifiable
        logger.warning(
            "escalation label: get_thread raised",
            project=project, thread_id=thread_id, error=str(e),
        )
        return None, {}
    finally:
        await adapter.close()
    if not isinstance(result, dict) or decisions_module._is_error(result):
        return None, {}
    messages = result.get("messages")
    if not isinstance(messages, list):
        return None, {}
    return messages, result.get("thread") or {}


async def _write_label(
    *,
    project: str,
    thread_id: str,
    submitted: LabelInput,
    messages: list[dict[str, Any]] | None,
    thread_meta: dict[str, Any],
    material: dict[str, Any] | None,
    target_moved: bool,
) -> LabelOutcome:
    """入力を検査し、計画し、検証し、1 行書く。例外を投げない。"""
    if submitted.label not in WIRE_VALUES:
        return LabelOutcome(ok=False, error=ERR_INVALID_LABEL)
    if len(submitted.note) > NOTE_MAX:
        return LabelOutcome(ok=False, error=ERR_NOTE_TOO_LONG)
    if submitted.label == LABEL_NONE:
        return LabelOutcome(ok=True)
    target = submitted.target
    try:
        store = _get_label_store()
        history = (
            await store.history(project=project, thread_id=thread_id, target_msg_id=target)
            if target
            else []
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "escalation label: history read raised",
            project=project, thread_id=thread_id, target=target, error=str(e),
        )
        return LabelOutcome(ok=False, error=ERR_STORE)
    if not _label_write_plan(_current(history), submitted):
        return LabelOutcome(ok=True)
    verdict, found = _verify_label_target(messages, thread_meta, target, _label_authors())
    if verdict != VERIFY_FOUND or found is None:
        logger.warning(
            "escalation label: target not writable",
            project=project, thread_id=thread_id, target=target, verdict=verdict,
        )
        return LabelOutcome(ok=False, error=verdict)
    cleared = submitted.label == LABEL_CLEARED
    tags = [] if cleared else list(submitted.tags)
    row = {
        "project": project,
        "thread_id": thread_id,
        "target_msg_id": target,
        "escalated_by": _msg_author(found),
        "label": submitted.label,
        "reason_tags": tags,
        "note": None if cleared else submitted.note,
        "tags_mismatch": False if cleared else _tags_mismatch(submitted.label, tags),
        "target_moved": target_moved,
        **_resolve_snapshot(history, material, target),
    }
    try:
        stored = await store.append(row)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "escalation label: append raised",
            project=project, thread_id=thread_id, target=target, error=str(e),
        )
        return LabelOutcome(ok=False, error=ERR_STORE)
    return LabelOutcome(ok=True, wrote=True, row=stored)


# ---------------------------------------------------------------------------
# 判断ページの POST から呼ぶ入口 (chatroom_writes 用)
# ---------------------------------------------------------------------------


@dataclass
class PreparedLabel:
    """回答を書く前に取り直した状態。``None`` でなければ回答成功後に書く。"""

    project: str
    thread_id: str
    submitted: LabelInput
    messages: list[dict[str, Any]] | None
    thread_meta: dict[str, Any]
    material: dict[str, Any] | None
    target_moved: bool


async def prepare_decision_label(
    project: str, thread_id: str, submitted: LabelInput | None
) -> PreparedLabel | None:
    """回答 POST の**前**に thread と material を取り直す (msg-1407 §3.2'')。

    ``none`` (未ラベル) なら何も取らずに ``None``。取り直した thread で
    サーバ側の対象を求め直し、``_esc_target`` と違えば ``target_moved``。
    付け先は ``_esc_target`` のまま (msg-1448)。
    """
    if submitted is None or submitted.label == LABEL_NONE:
        return None
    messages: list[dict[str, Any]] | None = None
    thread_meta: dict[str, Any] = {}
    material: dict[str, Any] | None = None
    target_moved = False
    try:
        messages, thread_meta = await _fetch_thread(project, thread_id)
        material, _ok = await decisions_module._load_material(project, thread_id)
        if messages is not None:
            server = _resolve_escalation_target(
                messages, thread_meta, material, _label_authors()
            )
            server_id = str(server.get("msg_id") or "") if server else ""
            target_moved = server_id != submitted.target
    except Exception as e:  # noqa: BLE001 - 回答を止めない。書き込み時に unverifiable
        logger.warning(
            "escalation label: prepare raised",
            project=project, thread_id=thread_id, error=str(e),
        )
        messages = None
    return PreparedLabel(
        project=project,
        thread_id=thread_id,
        submitted=submitted,
        messages=messages,
        thread_meta=thread_meta,
        material=material,
        target_moved=target_moved,
    )


async def commit_decision_label(prepared: PreparedLabel | None) -> LabelOutcome:
    """回答成功**後**にラベルを書く。例外を投げない (回答の流れを止めない)。"""
    if prepared is None:
        return LabelOutcome(ok=True)
    try:
        return await _write_label(
            project=prepared.project,
            thread_id=prepared.thread_id,
            submitted=prepared.submitted,
            messages=prepared.messages,
            thread_meta=prepared.thread_meta,
            material=prepared.material,
            target_moved=prepared.target_moved,
        )
    except Exception as e:  # noqa: BLE001 - 回答は成立済み。ラベルだけ失敗扱い
        logger.warning(
            "escalation label: commit raised",
            project=prepared.project, thread_id=prepared.thread_id, error=str(e),
        )
        return LabelOutcome(ok=False, error=ERR_STORE)


def label_page_url(
    project: str,
    thread_id: str,
    target: str | None = None,
    *,
    error: str | None = None,
    submitted: LabelInput | None = None,
) -> str:
    """ラベル画面の URL。書けなかったときは ``error`` と入力を query で戻す。"""
    base = (
        f"/dashboard/decisions/{quote(project, safe='')}"
        f"/{quote(thread_id, safe='')}/label"
    )
    params: list[tuple[str, str]] = []
    if target:
        params.append(("msg", target))
    if error:
        params.append(("error", error))
    if submitted is not None:
        params.append(("label", submitted.label))
        params.extend(("tags", t) for t in submitted.tags)
        if submitted.note:
            params.append(("note", submitted.note))
    return f"{base}?{urlencode(params)}" if params else base


# ---------------------------------------------------------------------------
# 入力欄の描画 context (判断ページとラベル画面が共有する)
# ---------------------------------------------------------------------------


def _field_context(
    *,
    target_msg_id: str,
    escalated_by: str,
    current: dict[str, Any] | None,
    submitted: LabelInput | None,
) -> dict[str, Any]:
    """``partials/escalation_label_fields.html`` に渡す dict。

    初期選択: 送り直し (``submitted``) があればそれ。無ければ current の値
    (無ければ ``none``)。``cleared`` は current があるときだけ描画する。
    """
    if submitted is not None:
        selected = submitted.label if submitted.label in WIRE_VALUES else LABEL_NONE
        tags = list(submitted.tags)
        note = submitted.note
    elif current is not None:
        selected = str(current.get("label") or LABEL_NONE)
        tags = list(current.get("reason_tags") or [])
        note = str(current.get("note") or "")
    else:
        selected, tags, note = LABEL_NONE, [], ""
    show_cleared = current is not None
    if selected == LABEL_CLEARED and not show_cleared:
        selected = LABEL_NONE
    return {
        "target_msg_id": target_msg_id,
        "escalated_by": escalated_by,
        "none_value": LABEL_NONE,
        "cleared_value": LABEL_CLEARED,
        "labels": [(v, LABEL_DISPLAY[v]) for v in LABEL_VALUES],
        "display": LABEL_DISPLAY,
        "show_cleared": show_cleared,
        "selected_label": selected,
        "tags_should_not_ask": list(TAGS_SHOULD_NOT_ASK),
        "tags_should_ask": list(TAGS_SHOULD_ASK),
        "selected_tags": tags,
        "note": note,
        "note_max": NOTE_MAX,
        "current": current,
    }


async def build_judgement_label_fields(
    *,
    project: str,
    thread_id: str,
    messages: list[dict[str, Any]],
    thread_meta: dict[str, Any],
    material: dict[str, Any] | None,
    submitted: LabelInput | None,
) -> dict[str, Any] | None:
    """判断ページ (judgement) のラベル欄。対象が ``None`` なら欄を出さない。"""
    target = _resolve_escalation_target(messages, thread_meta, material, _label_authors())
    if target is None:
        return None
    target_id = str(target.get("msg_id") or "")
    current = None
    try:
        history = await _get_label_store().history(
            project=project, thread_id=thread_id, target_msg_id=target_id
        )
        current = _current(history)
    except Exception as e:  # noqa: BLE001 - 読めなくても回答フォームは出す
        logger.warning(
            "escalation label: current read raised on render",
            project=project, thread_id=thread_id, error=str(e),
        )
    return _field_context(
        target_msg_id=target_id,
        escalated_by=_msg_author(target),
        current=current,
        submitted=submitted,
    )


# ---------------------------------------------------------------------------
# §3.4 単独のラベル画面
# ---------------------------------------------------------------------------


async def _render_label_page(
    request: Request,
    *,
    project: str,
    thread_id: str,
    msg: str | None,
    error: str | None,
    submitted: LabelInput | None,
    status_code: int,
    saved: bool = False,
) -> Response:
    thread_ui_url = decisions_module._thread_page_url(project, thread_id)
    decision_url = (
        f"/dashboard/decisions/{quote(project, safe='')}/{quote(thread_id, safe='')}"
    )
    ctx: dict[str, Any] = {
        "project": project,
        "thread_id": thread_id,
        "thread_ui_url": thread_ui_url,
        "decision_url": decision_url,
        "error_message": ERROR_MESSAGES.get(error or "", error) if error else None,
        "saved": saved,
        "verdict": None,
        "target_msg_id": None,
        "history": [],
        "fields": None,
        "display": LABEL_DISPLAY,
        "post_url": label_page_url(project, thread_id),
        "fetch_failed": False,
    }
    messages, thread_meta = await _fetch_thread(project, thread_id)
    if messages is None:
        ctx["fetch_failed"] = True
        ctx["verdict"] = VERIFY_UNVERIFIABLE
        return templates.TemplateResponse(
            request, "escalation_label.html", ctx,
            status_code=503 if status_code == 200 else status_code,
            headers={"Cache-Control": _NO_STORE},
        )
    authors = _label_authors()
    store = _get_label_store()
    target = msg or ""
    try:
        if not target:
            # msg を省略したとき: 最新のラベル済み target、無ければ今の対象
            # (resolver)。msg-1393 §3.4 は「material の head」だったが、
            # head は大半が relay の停止 msg (msg-1444 P-L2) ∴ 対象の決め方を
            # 1 つにまとめた resolver を使う (msg-1445 §3.2''' 4)。
            target = await store.latest_target(project=project, thread_id=thread_id) or ""
        if not target:
            material, _ok = await decisions_module._load_material(project, thread_id)
            resolved = _resolve_escalation_target(messages, thread_meta, material, authors)
            target = str(resolved.get("msg_id") or "") if resolved else ""
        history = (
            await store.history(project=project, thread_id=thread_id, target_msg_id=target)
            if target
            else []
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "escalation label page: store raised",
            project=project, thread_id=thread_id, error=str(e),
        )
        ctx["error_message"] = ERROR_MESSAGES[ERR_STORE]
        return templates.TemplateResponse(
            request, "escalation_label.html", ctx,
            status_code=503 if status_code == 200 else status_code,
            headers={"Cache-Control": _NO_STORE},
        )
    ctx["target_msg_id"] = target or None
    ctx["history"] = list(reversed(history))
    verdict, found = (
        _verify_label_target(messages, thread_meta, target, authors)
        if target
        else (VERIFY_REJECTED, None)
    )
    ctx["verdict"] = verdict
    if verdict == VERIFY_FOUND and found is not None:
        ctx["fields"] = _field_context(
            target_msg_id=target,
            escalated_by=_msg_author(found),
            current=_current(history),
            submitted=submitted,
        )
    return templates.TemplateResponse(
        request, "escalation_label.html", ctx,
        status_code=status_code,
        headers={"Cache-Control": _NO_STORE},
    )


@router.get(
    "/dashboard/decisions/{project}/{thread_id}/label", response_class=HTMLResponse
)
async def label_page(
    request: Request,
    project: str,
    thread_id: str,
    msg: str | None = None,
    error: str | None = None,
    label: str | None = None,
    tags: Annotated[list[str] | None, Query()] = None,
    note: str | None = None,
    saved: str | None = None,
) -> Response:
    """ラベル画面 (GET)。判断ページから書けずに来たときは ``error`` と入力を戻す。"""
    submitted = (
        LabelInput(label=label, tags=list(tags or []), note=note or "", target=msg or "")
        if label is not None
        else None
    )
    return await _render_label_page(
        request,
        project=project,
        thread_id=thread_id,
        msg=msg,
        error=error,
        submitted=submitted,
        status_code=200,
        saved=saved == "1",
    )


@router.post(
    "/dashboard/decisions/{project}/{thread_id}/label", response_class=HTMLResponse
)
async def label_page_post(
    request: Request,
    project: str,
    thread_id: str,
    esc_label: Annotated[str, Form(alias="_esc_label")] = LABEL_NONE,
    esc_tags: Annotated[list[str] | None, Form(alias="_esc_tags")] = None,
    esc_note: Annotated[str | None, Form(alias="_esc_note")] = None,
    esc_target: Annotated[str | None, Form(alias="_esc_target")] = None,
) -> Response:
    """ラベル画面 (POST)。書けた / no-op なら自画面へ 303、書けなければ 400 で再描画。"""
    submitted = LabelInput(
        label=esc_label,
        tags=[t for t in (esc_tags or []) if t],
        note=esc_note or "",
        target=esc_target or "",
    )
    messages, thread_meta = await _fetch_thread(project, thread_id)
    material, _ok = await decisions_module._load_material(project, thread_id)
    outcome = await _write_label(
        project=project,
        thread_id=thread_id,
        submitted=submitted,
        messages=messages,
        thread_meta=thread_meta,
        material=material,
        target_moved=False,
    )
    if outcome.ok:
        location = label_page_url(project, thread_id, submitted.target)
        location += ("&" if "?" in location else "?") + "saved=1"
        return Response(
            status_code=303,
            headers={"Location": location, "Cache-Control": "no-store"},
        )
    return await _render_label_page(
        request,
        project=project,
        thread_id=thread_id,
        msg=submitted.target or None,
        error=outcome.error,
        submitted=submitted,
        status_code=400,
    )


# ---------------------------------------------------------------------------
# §4 R5 読み出し
# ---------------------------------------------------------------------------


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """current 行の集計。

    - ``by_label``: ``cleared`` を含む current のラベル別件数。
    - ``by_tag`` / ``by_escalated_by`` / ``by_rules_hash``: ``cleared`` を除く
      (取り消された件はラベルを持たない)。``rules_hash`` が NULL の行は
      ``"null"`` に数える (当時のルール版が不明)。
    - ``tags_mismatch`` / ``target_moved``: 別枠の件数 (主軸からは除かない —
      除外するかは読む側が選ぶ)。
    """
    def bump(d: dict[str, int], key: str) -> None:
        d[key] = d.get(key, 0) + 1

    by_label: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    by_escalated_by: dict[str, int] = {}
    by_rules_hash: dict[str, int] = {}
    tags_mismatch = 0
    target_moved = 0
    for row in rows:
        label = str(row.get("label"))
        bump(by_label, label)
        if label == LABEL_CLEARED:
            continue
        for tag in row.get("reason_tags") or []:
            bump(by_tag, str(tag))
        bump(by_escalated_by, str(row.get("escalated_by") or "null"))
        bump(by_rules_hash, str(row.get("rules_hash") or "null"))
        tags_mismatch += int(bool(row.get("tags_mismatch")))
        target_moved += int(bool(row.get("target_moved")))
    return {
        "total": len(rows),
        "by_label": by_label,
        "by_tag": by_tag,
        "by_escalated_by": by_escalated_by,
        "by_rules_hash": by_rules_hash,
        "tags_mismatch": tags_mismatch,
        "target_moved": target_moved,
    }


def _store_unavailable(e: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error_type": "EscalationLabelStoreUnavailable", "error": str(e), "details": {}},
    )


@router.get("/v1/escalation-labels")
async def export_escalation_labels(
    project: str | None = None,
    since: str | None = None,
    history: str = "0",
) -> JSONResponse:
    """ラベルの export (R5、replay 評価の正解データ)。

    ``history=0`` (既定) は ``(project, thread_id, target_msg_id)`` ごとの
    current 行、``history=1`` は全行。本文は含めない: 読む側が
    ``target_msg_id`` で Conclair を引く。

    **No auth here** (material API と同じ規約、tailnet 限定)。返すのは
    identity 名・ラベル・200 字以内の note で、material API より機微な
    ものは無い (msg-1445 §4)。

    **§0:** ``escalated_by`` は集計用。Decider の入力や重み付けに使わない。
    magickit からはこれを強制できない ∴ 担保は読む側 (mindwire 単位 C) の
    レビュー観点に置く。
    """
    if history not in ("0", "1"):
        return decisions_module._bad_request(
            "InvalidQuery", "history must be 0 or 1", history=history
        )
    try:
        rows = await _get_label_store().list_labels(
            project=project or None, since=since or None, history=history == "1"
        )
    except Exception as e:  # noqa: BLE001
        logger.error("escalation label export: store raised", error=str(e))
        return _store_unavailable(e)
    return JSONResponse(
        status_code=200,
        content={"history": history == "1", "count": len(rows), "rows": rows},
        headers={"Cache-Control": _NO_STORE},
    )


@router.get("/v1/escalation-labels/summary")
async def summarize_escalation_labels(
    project: str | None = None,
    since: str | None = None,
) -> JSONResponse:
    """current ラベルの集計 (label / tag / ``escalated_by`` / ``rules_hash``)。

    **No auth here** (material API と同じ規約、tailnet 限定)。

    **§0:** ``by_escalated_by`` は集計用。Decider の入力や重み付けに使わない。
    """
    try:
        rows = await _get_label_store().list_labels(
            project=project or None, since=since or None, history=False
        )
    except Exception as e:  # noqa: BLE001
        logger.error("escalation label summary: store raised", error=str(e))
        return _store_unavailable(e)
    return JSONResponse(
        status_code=200,
        content=_summarize(rows),
        headers={"Cache-Control": _NO_STORE},
    )


__all__ = [
    "router",
    "LABEL_NONE",
    "LABEL_SHOULD_ASK",
    "LABEL_SHOULD_NOT_ASK",
    "LABEL_BORDERLINE",
    "LABEL_CLEARED",
    "LABEL_VALUES",
    "WIRE_VALUES",
    "LabelInput",
    "LabelOutcome",
    "PreparedLabel",
    "label_input_from_form",
    "prepare_decision_label",
    "commit_decision_label",
    "label_page_url",
    "build_judgement_label_fields",
]
