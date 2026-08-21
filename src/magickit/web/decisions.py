"""判断 (decision) page redirect stubs -- S5' 増分 1.

The composer that fires the Discord alert for a parked decision writes a
link back to ``/dashboard/decisions/{project}/{thread_id}``. That path
was never served, so every alert Discord已に配ったリンクが 404 を返して
いた -- and because the URL is what the alert body carries, editing the
composer後付けで直しても、既送信の通知は生き返らない.

This module answers the same URL and 302s to the chatroom UI thread page
(``/ui/projects/{project}/threads/{thread_id}``), which does serve. The
already-shipped alerts start working again without a re-send, and 増分
2 will replace this handler body with the real judgement page on the
same URL -- the contract stays exactly one path.

Why this file exists, in two lines you have to remember when you touch it:

* **This URL is a shipped contract.** Do not move it. 増分 2 differs
  from 増分 1 only in what this handler returns, not in where it lives.
* **CI cannot see whether the URL reaches anything.** Confirm arrival by
  actually hitting :8443 from outside the process (curl -L, verify the
  final 200 and the page title). A green gate is necessary, not
  sufficient; 「実装済みに見えて何も出さない」型の事故は CI を素通り
  する (msg-084 §5).

Design points that a reviewer will ask about:

* **302 + ``Cache-Control: no-store``, not 301/308.** 増分 2 建てる先が
  この同じ URL である以上、恒久リダイレクトを掴んだモバイル/Service
  Worker は増分 2 リリース後もサーバ側から訂正不能な形で ``/ui`` に
  飛び続ける (Bohr D-B, Einstein Q1)。壊れ方が「404」ではなく「古い
  ページに静かに着く」になり、より検出が難しい。
* **``quote(..., safe="")`` で自前エンコード。** ``RedirectResponse``
  自身の再 quote は ``?`` ``&`` ``#`` を safe に含む ∴ それらを含む
  ``thread_id`` は query 境界に化ける。実データは ASCII 安全だが、
  要件は「エンコードを保つ」であって「依存先の safe 集合を信頼する」
  ではない (Bohr D-C)。
* **在否検証なし。** 単なるリダイレクトに Conclair 依存を挟むと、
  復活させたリンクを Conclair 瞬断で再び壊す。D-26' の 3 分岐 (駐機中
  / 駐機中でない / 存在しない) は実ページを持つ増分 2 の責務 (Bohr
  D-D)。「存在しない thread」の応答は ``/ui`` 側に委ねる。
* **``/dashboard/decisions`` (一覧) → ``/dashboard``。** 一覧の中身
  は増分 3。今はブロック軸との峻別 (msg-084 §3) がある ops 画面へ
  渡し、判断待ちを見に来た人が少なくとも何かに着地するようにする。
* **``ops.py`` に混ぜない。** この URL は stub より長生きする契約
  で、増分 2 は同じパスのハンドラ本体を差し替える形で建てる。ops の
  ブロック軸との統合を後で気をつけるのではなく、モジュール境界で
  守る (msg-084 §3 非目標)。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Response

router = APIRouter(tags=["decisions"])

#: Sent on both redirects. ``no-store`` is what actually plugs the hole
#: (nothing gets cached, so nothing can be served after 増分 2 lands).
#: ``must-revalidate`` is included for the operator reading response
#: headers -- it says "even if you did keep something, don't reuse it" --
#: but it is a no-op next to ``no-store`` and the tests assert the
#: presence of ``no-store``, which is the directive doing the work.
_NO_STORE = "no-store, must-revalidate"


def _redirect(location: str) -> Response:
    """Build a bare 302 with ``Location`` and ``Cache-Control`` set.

    A plain ``Response`` rather than ``fastapi.responses.RedirectResponse``:
    the latter re-quotes ``url`` with a permissive ``safe`` set (``?``,
    ``&``, ``#`` are preserved), and we have already quoted every segment
    with ``safe=""``. Handing it to ``RedirectResponse`` would either
    double-quote nothing (present data) or, once a thread id ever contains
    a query-boundary character, split the URL on it. Returning the string
    verbatim is the only way to keep the encoding this function chose.
    """
    return Response(
        status_code=302,
        headers={"Location": location, "Cache-Control": _NO_STORE},
    )


@router.get("/dashboard/decisions/{project}/{thread_id}")
async def decision_redirect(project: str, thread_id: str) -> Response:
    """Redirect the Discord-alert URL to the thread page on ``/ui``.

    Increment 1: no presence check, no query-string forwarding. The
    thread page (which does exist) answers 200 for a live thread and 404
    for a missing one, and either answer is more truthful than something
    we could invent here from the URL alone.
    """
    location = (
        f"/ui/projects/{quote(project, safe='')}"
        f"/threads/{quote(thread_id, safe='')}"
    )
    return _redirect(location)


@router.get("/dashboard/decisions")
async def decisions_index_redirect() -> Response:
    """Redirect the (yet-unbuilt) list URL to the ops dashboard.

    Increment 3 will build the real list. Until then this at least gives
    somebody who followed a link or typed the URL a page with signal on
    it (ops), rather than a 404 that reads as "the feature does not
    exist".
    """
    return _redirect("/dashboard")


__all__ = ["router"]
