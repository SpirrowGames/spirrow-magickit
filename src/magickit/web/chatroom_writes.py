"""Gated write handlers for the browser-facing chatroom UI.

Conclair's ``/ui`` ships its own POST endpoints, and they call Conclair's
own ``/v1`` in-process. That path never touches Magickit, so none of the
role / naysayer / embodiment enforcement applies to it -- the gap recorded
in CLAUDE.md as "UI 直叩きへの効力は現時点要件外 (msg-003 D-2)".

This module closes that gap by claiming the three write routes before the
proxy forwards them. A browser write now runs the *same* helpers the MCP
tools run (``_check_role_allowed``, ``_enforce_close_policies``,
``_check_can_close``) and only then reaches Conclair, via the adapter.
GETs still proxy straight through -- reads have nothing to enforce, and so
does the loop control form post, which carries no role and no msg (see
``chatroom_proxy.chatroom_loop_control``).

Keeping enforcement here rather than in Conclair preserves the service
boundary: Conclair stays an append-only log that validates nothing and
knows nothing about Magickit.

The responses are HTMX fragments styled by Conclair's own stylesheet
(``alert-error`` / ``alert-success``), so a gate rejection renders in the
same flash slot as a Conclair-side validation error.
"""

from __future__ import annotations

import html
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.web.mojibake import first_mojibake
from magickit.utils.logging import get_logger

# 判断ページ (S5 増分 2) 由来の POST は decision-page 特化の helper に委譲する。
# 判定 literal (`NEXT:` 相当のキーワード) はそちらの module に集約しており、この
# enforcement-code file には持ち込まない (msg-072 §1 の vocabulary-boundary
# 制約: adapter / MCP tool / この chatroom_writes.py に consumer 用語の literal を
# 書かない)。judgement page は決定 UI 特化の別ファイル ∴ そこに集約するのが妥当。
from magickit.web.decisions import (
    _compose_decision_body,
    _maybe_append_next,
)

logger = get_logger(__name__)

router = APIRouter(tags=["chatroom-ui"])

# --- decision-page opt-in (S5 増分 2) ----------------------------------------
#
# 判断ページ (``web/decisions.py``) から届く POST だけを opt-in で拾い、
# ``content`` (選択肢ボタンが運ぶ) と ``_freeform`` (常設テキスト) を 1 本の本文
# に合成する。共有経路 (このハンドラ) を通るが、影響は ``_freeform`` を持つ
# POST に限定する (spec `spec/slices/S5-decision-page.md` §3 / msg-098 §4)。
#
# **合成は gate 群より前で行い、以降 `content` を参照する全箇所が合成後の本文
# を見る形にする** (spec §3.4 / msg-097 §4.2 の罠)。``_enforce_close_policies``
# は生の ``content`` を読む ∴ 合成結果を ``body_content`` にだけ代入する実装は、
# ``closes`` が真のとき自由記述を黙って捨てる (最も重要な 1 通で最も長く打った
# 文章が落ちる)。∴ ``content`` そのものを合成後の値へ差し替える。

#: I-12 sentinel: 判断ページの「自由記述だけで送る」ボタンが送る ``content`` 値。
#: 純粋な空文字 (``value=""``) は現行 FastAPI (0.128 / pydantic 2.12) で
#: ``content=`` が「missing」として 422 に落とされる — msg-097 §4.1 の
#: 「空文字は str として妥当 ∴ 422 にならない」が empirical に成立しない ∴
#: 既存 ``content`` param の signature を変えずに I-12 を成立させるための
#: sentinel を挟む。``_compose_decision_body`` に渡す前に空文字へ正規化する。
#: ボタンの ``value`` 属性は user が typo できず、button 由来値以外に content
#: が入る経路は無い ∴ 衝突しない (spec §3.1a の empirical finding)。
_FREEFORM_ONLY_SENTINEL = "(自由記述のみ)"


def _next_participant_error_message(envelope: dict[str, Any]) -> str:
    """D-31: 2 種のエラーを混ぜず、区別できるふりもしない (spec §3.6)。

    - ``NextParticipantUnknownError`` (未登録の確定回答) →「その名前は登録されていない」
    - ``NextParticipantValidationUnavailableError`` (Prismind 不達) →「今 identity を
      確認できませんでした」
    - それ以外 → envelope の error 文を素通し。区別できるふりをしない。
    """
    error_type = str(envelope.get("error_type", ""))
    detail = str(envelope.get("error", "")) or "unknown error"
    if error_type == "NextParticipantUnknownError":
        return f"指定された名前は登録されていません。{detail}"
    if error_type == "NextParticipantValidationUnavailableError":
        return f"今 identity の確認ができませんでした ({detail})。"
    # 区別できない envelope はそのまま出す (spec §3.6 の一般則)。
    return f"{error_type}: {detail}"


def _error_envelope_message(envelope: dict[str, Any]) -> str:
    """一般的な error envelope を D-31 の再描画向けに 1 行にまとめる。

    `_next_participant_error_message` の非 next_participant 版。gate 系エラー
    や adapter からの envelope も判断ページに戻すときにこれを使う。
    """
    error_type = str(envelope.get("error_type", "Error"))
    detail = str(envelope.get("error", "")) or "unknown error"
    return f"{error_type}: {detail}"


async def _render_decision_error(
    request: Request,
    *,
    project: str,
    thread_id: str,
    content_value: str,
    freeform_value: str,
    next_participant_value: str,
    error_message: str,
) -> HTMLResponse:
    """D-31: 判断ページを入力保持のまま再描画する。

    ``decisions.py._render_decision_error_page`` を呼ぶ ∴ 実装は 1 箇所。
    ここでは import 循環を避けるために遅延 import する。thread 文脈の復元は
    呼び先が best-effort で行う。
    """
    # 遅延 import: decisions.py が chatroom_writes を import する将来の可能性
    # に備え、モジュールロード時の相互依存を作らない。
    from magickit.web.decisions import _render_decision_error_page

    return await _render_decision_error_page(
        request,
        project=project,
        thread_id=thread_id,
        content_value=content_value,
        freeform_value=freeform_value,
        next_participant_value=next_participant_value,
        error_message=error_message,
        status_code=400,
    )


def _parse_csv(value: str) -> list[str]:
    """Split a comma-separated form field, mirroring Conclair's helper."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _mojibake_notice(field: str, recovered: str) -> str:
    """Warn that the write landed but its text looks mis-decoded.

    Rendered as ``alert-error`` rather than a warning class of its own:
    Conclair's stylesheet has only error and success, and -- more to the
    point -- ``conclair.js`` auto-dismisses ``.alert-success`` after six
    seconds. A warning that disappears on its own is no warning, and this
    one cannot be acted on later: the message is already in an append-only
    log with no update endpoint. The copy leads with the success so the
    red box is not read as a failed post.
    """
    return (
        '<div class="alert alert-error">'
        "<strong>投稿は成功しました</strong>が、"
        f"<code>{html.escape(field)}</code> が文字化けしている可能性があります"
        "（UTF-8 が latin-1 として読まれた形）。<br>"
        f"復元候補: <code>{html.escape(recovered[:200])}</code><br>"
        "フォーム本文の percent-encode 漏れが原因です"
        "（curl なら <code>--data-urlencode</code> を使ってください）。"
        "<strong>メッセージは append-only なので後から修正できません。</strong>"
        "</div>"
    )


def _flash(
    message: str,
    *,
    status_code: int = 200,
    text_fields: dict[str, str] | None = None,
) -> Response:
    """Render a success flash into Conclair's alert markup.

    ``text_fields`` are the free-text fields the caller submitted; if any
    looks mis-decoded, the warning is appended below the success. The
    check runs on what the author typed, not on what was stored, so a body
    rewritten by the close policies is not what gets inspected.
    """
    body = f'<div class="alert alert-success">{html.escape(message)}</div>'
    hit = first_mojibake(text_fields or {})
    if hit is not None:
        field, recovered = hit
        logger.warning(
            "Mojibake detected on a chatroom write", field=field, recovered=recovered
        )
        body += _mojibake_notice(field, recovered)
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/html; charset=utf-8",
        headers={"HX-Trigger": "messagePosted"},
    )


def _error_flash(envelope: dict[str, Any], *, status_code: int = 200) -> Response:
    """Render an error envelope into Conclair's alert markup.

    Handles both shapes that reach here: Magickit's gate envelopes and
    Conclair's upstream ``{error_type, error, details}``. They already share
    a schema, which is why one renderer covers both.
    """
    error_type = html.escape(str(envelope.get("error_type", "Error")))
    error = html.escape(str(envelope.get("error", "")))
    body = f'<div class="alert alert-error"><strong>{error_type}</strong>: {error}'
    details = envelope.get("details")
    if details:
        body += f"<pre>{html.escape(str(details))}</pre>"
    body += "</div>"
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/html; charset=utf-8",
    )


def _is_error(result: dict[str, Any]) -> bool:
    """Conclair signals failure by the presence of ``error_type``, not a flag."""
    return "error_type" in result


@router.post("/ui/projects/{project}/threads")
async def open_thread(
    request: Request,
    project: str,
    thread_id: Annotated[str, Form()],
    title: Annotated[str, Form()],
    owner: Annotated[str, Form()],
    propose_content: Annotated[str, Form()],
    tags: Annotated[str, Form()] = "",
    commit_ref: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    embodiment: Annotated[str, Form()] = "",
    next_participant: Annotated[str, Form()] = "",
) -> Response:
    """Open a thread from the browser, through the role gate.

    The gate validates ``role`` against ``owner``, who authors the propose
    msg -- the same pairing ``chatroom_open_thread`` uses.

    ``next_participant`` (T-handoff-target-structured-field) is validated
    against Prismind's identity registry just as in the MCP tool: an
    unknown name refuses the write with ``NextParticipantUnknownError``.
    """
    gate = await chatroom_tools._check_role_allowed(author=owner, role=role)
    if gate.error is not None:
        return _error_flash(gate.error)

    next_error = await chatroom_tools._check_next_participant(next_participant)
    if next_error is not None:
        return _error_flash(next_error)

    adapter = _adapter()
    try:
        result = await adapter.open_thread(
            project=project,
            thread_id=thread_id,
            title=title,
            owner=owner,
            propose_content=propose_content,
            tags=_parse_csv(tags),
            commit_ref=commit_ref or None,
            embodiment=embodiment or None,
            role=gate.role,
            next_participant=next_participant or None,
        )
    finally:
        await adapter.close()

    if _is_error(result):
        return _error_flash(result)
    return _flash(
        f"opened {thread_id}",
        text_fields={"title": title, "propose_content": propose_content},
    )


@router.post("/ui/projects/{project}/threads/{thread_id}/messages")
async def post_message(
    request: Request,
    project: str,
    thread_id: str,
    type: Annotated[str, Form()],
    author: Annotated[str, Form()],
    content: Annotated[str, Form()],
    reply_to: Annotated[str, Form()] = "",
    references_threads: Annotated[str, Form()] = "",
    related_tasks: Annotated[str, Form()] = "",
    closes_thread: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    commit_ref: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    embodiment: Annotated[str, Form()] = "",
    naysayer_override_reason: Annotated[str, Form()] = "",
    owner_override_reason: Annotated[str, Form()] = "",
    next_participant: Annotated[str, Form()] = "",
    # S5 増分 2 opt-in: 判断ページから届いた POST の合成トリガ。
    # **default `None` は Einstein msg §2 の必須ガード**: `Annotated[str, Form()]`
    # を default 無しで足すと必須になり、`_freeform` を送らない既存 form が 422 で
    # 弾かれる (early-return コードに届く前に FastAPI が拒否する) ∴ G-1 が壊れる。
    # spec `spec/slices/S5-decision-page.md` §3.1。
    #
    # ワイヤ名は ``_freeform`` を維持 (spec / Bohr の凍結形どおり)。Python 側の
    # パラメータ名は ``decision_freeform`` — Pydantic v2 が leading-underscore を
    # field 名として拒否するため。``alias`` でワイヤ側の名前だけ ``_freeform`` に
    # 戻す (仕様の名前を保つ + Pydantic の禁則を回避 の両立)。
    decision_freeform: Annotated[str | None, Form(alias="_freeform")] = None,
) -> Response:
    """Post a message from the browser, through every gate that applies.

    Order matters and mirrors ``chatroom_post_message``: embodiment first
    (cheapest, no IO), then the role gate, then -- only for a ``decide``
    that closes -- the close policies and the second-stage close check.
    A ``closes_thread`` here must not be a way around the close gates.

    **判断ページ由来 (`_freeform is not None`) は opt-in の合成分岐に入る**:
    `content` (選択肢ボタンが運ぶ) と `_freeform` (常設テキスト) を 1 本の本文
    に合成し、D-30 の `NEXT:` 追記を判定してから gate 群に渡す。合成は gate より
    前で `content` そのものを差し替えることで、`_enforce_close_policies` が読む
    生の `content` も合成後になる (spec §3.4 の罠回避)。

    G-1 (spec §3.2): `_freeform is None` の POST は下の分岐に入らず、既存挙動を
    バイト単位で維持する。既存 `/ui` の compose form は `_freeform` を送らない。
    """
    # --- 判断ページ由来の opt-in 分岐 (S5 増分 2) --------------------------
    #
    # トリガは `_freeform` の有無ただ 1 つ (spec §3.2 / msg-097 §4.3)。以下 3 点
    # を GATE 群より前で完了させ、以降 `content` を参照するコードは合成後の値を
    # 見る (spec §3.4 / msg-097 §4.2):
    #   1. 合成 (spec §3.3)
    #   2. D-30 の NEXT: 追記 (spec §3.5)
    #   3. エラー時に再描画するための入力保存 (spec §3.6 D-31)
    is_decision_post = decision_freeform is not None
    # D-31 の入力保持用 (選択肢の value を再描画に残す)。sentinel は user 視点で
    # 「選択肢なし」を意味する ∴ 復元時も空を渡す (元のボタン value ではない)。
    original_content = "" if content == _FREEFORM_ONLY_SENTINEL else content
    original_freeform = decision_freeform or ""
    if is_decision_post:
        # I-12 sentinel 正規化: 「自由記述だけで送る」ボタンは
        # ``content=(自由記述のみ)`` を送る ∴ 合成前に空文字へ戻す。純粋な
        # ``value=""`` が現行 FastAPI で 422 に落ちる回避策 (spec §3.1)。
        working_content = "" if content == _FREEFORM_ONLY_SENTINEL else content
        composed = _compose_decision_body(working_content, original_freeform)
        composed = _maybe_append_next(composed, next_participant)
        # ★ content 自体を差し替える。以降のコード (embodiment / role gate /
        # `_enforce_close_policies` の生 `content` 引数 / adapter.post_message の
        # body_content) はすべて合成後を見る。
        content = composed

    if _embodiment_missing(msg_type=type, author=author, embodiment=embodiment):
        envelope = chatroom_tools._embodiment_required_error(msg_kind=type)
        if is_decision_post:
            # human は embodiment 免除 ∴ この分岐は通常来ないが、type が変な値
            # だと落ちる余地はある。D-31 で入力を返す。
            return await _render_decision_error(
                request,
                project=project, thread_id=thread_id,
                content_value=original_content,
                freeform_value=original_freeform,
                next_participant_value=next_participant,
                error_message=_error_envelope_message(envelope),
            )
        return _error_flash(envelope)

    # A `decide` that closes must go through the close gate, which runs both
    # role stages off a single identity lookup. Calling the stage-1 helper
    # first would not just cost an extra lookup -- on an outage it answers
    # `RoleValidationUnavailableError`, whose documented remedy ("retry
    # without role") stage 2 is guaranteed to refuse. The close path owes the
    # caller the stage-2 envelope instead (msg-041 Q3).
    closes = type == "decide" and bool(closes_thread)
    gate = await (
        chatroom_tools._check_close_permitted(author=author, role=role)
        if closes
        else chatroom_tools._check_role_allowed(author=author, role=role)
    )
    if gate.error is not None:
        if is_decision_post:
            return await _render_decision_error(
                request,
                project=project, thread_id=thread_id,
                content_value=original_content,
                freeform_value=original_freeform,
                next_participant_value=next_participant,
                error_message=_error_envelope_message(gate.error),
            )
        return _error_flash(gate.error)

    # Structured handoff target check -- same rule as the MCP tool. Runs
    # after the role gate so the two rejection classes stay distinguishable
    # (both are fail-fast pre-write; no traffic exists in which one masks
    # the other).
    next_error = await chatroom_tools._check_next_participant(next_participant)
    if next_error is not None:
        if is_decision_post:
            # D-31 の本命エラー経路。文面は種別で区別する (Unknown vs Unavailable)。
            return await _render_decision_error(
                request,
                project=project, thread_id=thread_id,
                content_value=original_content,
                freeform_value=original_freeform,
                next_participant_value=next_participant,
                error_message=_next_participant_error_message(next_error),
            )
        return _error_flash(next_error)

    adapter = _adapter()
    try:
        body_content = content
        owner_override = False
        resolved_override_reason: str | None = None

        if closes:
            # ★ ここに渡す ``body_content=content`` は既に合成後の値である
            # (spec §3.4 の罠回避 / msg-097 §4.2): opt-in 分岐で ``content``
            # 自体を差し替えたので、`_enforce_close_policies` が生の
            # `content` を読んでも自由記述は保たれる。
            decision = await chatroom_tools._enforce_close_policies(
                adapter,
                project=project,
                thread_id=thread_id,
                author=author,
                body_content=content,
                naysayer_override_reason=naysayer_override_reason,
                owner_override_reason=owner_override_reason,
            )
            if decision["action"] == "block":
                if is_decision_post:
                    return await _render_decision_error(
                        request,
                        project=project, thread_id=thread_id,
                        content_value=original_content,
                        freeform_value=original_freeform,
                        next_participant_value=next_participant,
                        error_message=_error_envelope_message(decision["envelope"]),
                    )
                return _error_flash(decision["envelope"])
            body_content = decision["content"]
            owner_override = decision.get("owner_override", False)
            resolved_override_reason = decision.get("owner_override_reason")

        result = await adapter.post_message(
            project=project,
            thread_id=thread_id,
            type=type,
            author=author,
            content=body_content,
            reply_to=reply_to or None,
            references_threads=_parse_csv(references_threads),
            related_tasks=_parse_csv(related_tasks),
            closes_thread=closes_thread or None,
            tags=_parse_csv(tags),
            commit_ref=commit_ref or None,
            embodiment=embodiment or None,
            role=gate.role,
            owner_override=owner_override,
            owner_override_reason=resolved_override_reason,
            next_participant=next_participant or None,
        )
    finally:
        await adapter.close()

    if _is_error(result):
        if is_decision_post:
            return await _render_decision_error(
                request,
                project=project, thread_id=thread_id,
                content_value=original_content,
                freeform_value=original_freeform,
                next_participant_value=next_participant,
                error_message=_error_envelope_message(result),
            )
        return _error_flash(result)

    if is_decision_post:
        # 判断ページからの成功: HTMX ではなくプレーン form ∴ 303 で /ui の
        # スレッドページへ着地。ブラウザは自然に GET で開く (spec §3.7)。
        # 遅延 import: `decisions._thread_page_url` は循環回避のためここで解決。
        from magickit.web.decisions import _thread_page_url

        return Response(
            status_code=303,
            headers={
                "Location": _thread_page_url(project, thread_id),
                "Cache-Control": "no-store",
            },
        )

    msg = result.get("msg", {})
    text = f"posted {msg.get('msg_id', '?')} ({msg.get('type', type)})"
    if result.get("thread_status_changed_to"):
        text += f" — status → {result['thread_status_changed_to']}"
    return _flash(text, text_fields={"content": content})


@router.post("/ui/projects/{project}/threads/{thread_id}/close")
async def close_thread(
    request: Request,
    project: str,
    thread_id: str,
    author: Annotated[str, Form()],
    summary_content: Annotated[str, Form()],
    affects_threads: Annotated[str, Form()] = "",
    related_tasks: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    embodiment: Annotated[str, Form()] = "",
    naysayer_override_reason: Annotated[str, Form()] = "",
    owner_override_reason: Annotated[str, Form()] = "",
) -> Response:
    """Close a thread from the browser, through both close gates.

    ``_check_close_permitted`` is the second stage (``closeable_roles``),
    which fails closed on an identity-lookup outage by design -- there is
    deliberately no escape hatch, so no fallback is attempted here.
    """
    if _embodiment_missing(msg_type="decide", author=author, embodiment=embodiment):
        return _error_flash(
            chatroom_tools._embodiment_required_error(msg_kind="decide")
        )

    # Both role stages on one identity lookup. Deliberately not preceded by
    # the stage-1 helper: see the note in `post_message` -- on a lookup
    # outage stage 1 would hand back an envelope whose suggested retry the
    # close path is certain to reject.
    gate = await chatroom_tools._check_close_permitted(author=author, role=role)
    if gate.error is not None:
        return _error_flash(gate.error)

    adapter = _adapter()
    try:
        decision = await chatroom_tools._enforce_close_policies(
            adapter,
            project=project,
            thread_id=thread_id,
            author=author,
            body_content=summary_content,
            naysayer_override_reason=naysayer_override_reason,
            owner_override_reason=owner_override_reason,
        )
        if decision["action"] == "block":
            return _error_flash(decision["envelope"])

        result = await adapter.close_thread(
            project=project,
            thread_id=thread_id,
            summary_content=decision["content"],
            author=author,
            affects_threads=_parse_csv(affects_threads),
            related_tasks=_parse_csv(related_tasks),
            tags=_parse_csv(tags),
            embodiment=embodiment or None,
            role=gate.role,
            owner_override=decision.get("owner_override", False),
            owner_override_reason=decision.get("owner_override_reason"),
        )
    finally:
        await adapter.close()

    if _is_error(result):
        return _error_flash(result)
    return _flash(
        f"closed {thread_id} — status → resolved",
        text_fields={"summary_content": summary_content},
    )


def _adapter() -> ChatroomAdapter:
    """Build a chatroom adapter using the tools module's bound settings."""
    return chatroom_tools._adapter()


def _embodiment_missing(*, msg_type: str, author: str, embodiment: str) -> bool:
    """Whether this write needs an embodiment declaration and lacks one.

    Same rule as the MCP tools: mandatory on state-transitioning msg types,
    and humans are exempt (they are the above-loop approval layer, not a
    calling agent).
    """
    if author in chatroom_tools.HUMAN_IDENTITY_NAMES:
        return False
    if msg_type not in chatroom_tools.MANDATORY_EMBODIMENT_MSG_TYPES:
        return False
    return not embodiment
