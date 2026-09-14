"""「僕が処理すべきこと」の board (``/dashboard/decisions``).

稼働状況ページは *プロジェクト* が動いているかを答える。この board は
その隣の問い ——— **僕を待って止まっているものは何か** ——— を答える。
両者は別の問いで、片方の答えでもう片方を推し量ることはできない: 全部
「稼働中」でも僕への判断依頼が 8 件溜まっていることはあるし、その逆も
ある。

板に載るもの (3 種、実測の性質つき)
-----------------------------------
- **判断待ち** — 3 つ全部を満たすものだけ。**1 つでも落とすと板は
  「僕の番ではないもの」で埋まる** (実測: 鮮度だけ見ていた頃、5 枚中
  本当に僕の番だったのは 1 枚)。

  1. **鮮度** — 材料の ``head_msg_id`` がスレッドの ``last_msg_id`` と
     一致する。materials table は消えない (答えた後も行は残る) ので、
     これをせずに材料の存在だけで並べると回答済みが延々並ぶ。
  2. **スレッドが開いている** — ``decisions._thread_write_state`` が
     ``"closed"`` (``resolved`` / ``superseded``) でない。**鮮度だけでは
     resolved が落ちない**: 解決した msg がそのまま末尾になるので
     (``resolved_by_msg == last_msg_id``)、鮮度は構造的に一致し続ける。
  3. **僕がまだ答えていない** — 駐機 msg が human の ``decide`` ではない
     (``decisions._is_human_decide``)。自分の決裁が末尾に残っているだけの
     ものは、僕の番ではなく次の人の番。

  **``NEXT:`` 行を「誰の番か」として読んではいけない。** 一度そう実装して
  外した (2026-09-14): ``decisions._is_parked_to_human`` で絞ったところ、
  **本物の判断待ちを 3 件落とした**。``NEXT:`` は「次に動く *はず* の人」
  であって、誰の番かを決めているのは conductor の停止理由のほう。
  mindwire の ``guard_proposer_to_implementer`` (guard (i)) は
  **非 human の著者から implementer への handoff を design→implement の
  Tier-C ゲートとして人に差し戻す** (``conductor/core.py`` `_route`) ∴
  ``NEXT: Heisenberg`` と書いてあるスレッドがそのまま僕の判断待ちになる。
  材料が来ていること自体が「conductor が人で止まった」の言い換えで
  (push は ``$needsHuman`` な停止理由でしか発火しない)。**その停止理由は
  材料の ``stop_reason`` field で受け取り、カードの副題に出す** ———
  ``human`` (Tier-C) と ``round_cap`` / ``empty_thread`` (異常) が同じ顔で
  並ぶと、板は「何を待たれているか」を答えなくなる。``signature`` にも
  同じ値が混ざっているが、**あれは parse してはいけない**
  (``spec/slices/S5-decision-materials.md`` §1.1: 保存のみ)。
- **deploy 承認待ち** — ``status == pending_approval`` の deploy request。
  ローカルの file store ∴ Conclair が落ちていても読める。
- **止まったループ** — 稼働状況ページと **同じ** ``ops.classify`` で
  ``held`` / ``stalled`` と出たプロジェクト。判定を書き直さない: 2 箇所で
  別々に「停止」を定義したら、2 つのページが違うことを言い始める。

列は 2 種類の性質を持つ (ここが設計の要)
----------------------------------------
``新着 / 対応中 / 保留`` は **僕の状態** で、どこにも既存の表現が無い
∴ magickit が持つ (:mod:`magickit.core.board_lanes`)。ドラッグで動く。

``完了`` は **世界の状態** で、live 集合から落ちたことの言い換えでしか
ない ∴ ドラッグでは作れない。決裁すれば材料が stale になり、deploy を
承認すれば pending でなくなり、RESUME すれば held でなくなる ——— その
とき初めてカードが完了列に移る。「完了に置いたのに実際は誰も答えて
いない」という状態が**構造的に作れない**のはこのためで、これは板の
見た目ではなく板が信用できるかどうかの話。

カードは行き止まりにしない
--------------------------
板は「何が待っているか」までしか答えない ∴ **次の一手がある場所へは
カードから 1 クリックで行けること**。飛び先は種別ごとに違う:

- **判断** — 題名が判断ページへ。加えて、材料が PR を名指していれば
  その PR へのリンクを添える。PR 参照の切り出しは
  :func:`magickit.mcp.pr_gate_ledger.parse_pr_ref` を**呼ぶ**: PR の
  指し方を 2 箇所で定義すると、ledger が PR と認めない文字列を板が
  PR として指す (あるいはその逆) ということが起きる。
- **承認** — deploy 一覧の**その行**へ (``#deploy-<id>``)。あのページは
  サーバ描画なのでアンカーが効く。
- **ループ** — 題名は ``/dashboard`` (RESUME のボタンがある場所)、
  加えてそのプロジェクトの chatroom へ。**稼働状況ページに
  ``#<project>`` を張らないのは効かないから**: ops 表は HTMX の後読みで、
  ブラウザは表が届く前にスクロールを済ませてしまう。

**PR リンクだけが外向き**で、これは :mod:`tests.unit.test_templates_no_external_assets`
が禁じている「オリジン付き URL をテンプレートに書く」ことには当たらない
——— URL はここで組み立てて ``card.links`` に載せ、テンプレートは
相対・絶対の区別を知らないまま出す。あの規則が守っているのは**資産**
(HTMX や CSS) で、届かなければページが無言で死ぬもののこと。この
リンクは人が押すもので、押せなければ押した人に見える。

読めないものは空欄にしない
--------------------------
Conclair が読めないとき、判断待ちは「0 件」ではなく **判定不能**。
判断材料は手元にあるので「8 件あるかもしれない」までは言えるが、その
どれが今も僕の番かは last_msg_id 無しには決められない ∴ 列を空にせず、
何が読めなかったかを板の上に書く。稼働状況ページの既定の作法と同じ。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from magickit.adapters.chatroom import ChatroomAdapter
from magickit.config import Settings, get_settings
from magickit.core.board_lanes import DEFAULT_LANE, LANES, BoardLaneStore, SeenItem
from magickit.core.decision_materials import DecisionMaterialStore
from magickit.deploy import records
from magickit.mcp.pr_gate_ledger import parse_pr_ref
from magickit.utils.logging import get_logger
from magickit.web import decisions, identity, ops
from magickit.web.deps import parse_ts, templates

logger = get_logger(__name__)

router = APIRouter(tags=["board"])

#: 判断ページと同じ (302 の永久キャッシュを防ぐ趣旨をそのまま引き継ぐ)。
_NO_STORE = "no-store, must-revalidate"

#: 動かせる列 (id, 見出し, 見出しの下の 1 行)。順序がそのまま板の左右。
LANE_COLUMNS = (
    ("new", "新着", "まだ手を付けていないもの"),
    ("doing", "対応中", "自分で「今やっている」と置いたもの"),
    ("parked", "保留", "見たうえで後回しにしたもの"),
)

#: 完了列。lane 値ではない — live 集合から落ちたことの言い換え。
DONE_COLUMN = ("done", "完了", "board から落ちたもの（理由は各カードに）")

#: カード種別の見出しバッジ。
KIND_LABELS = {
    "decision": "判断",
    "deploy": "承認",
    "loop": "ループ",
}

#: 列の中の並び。deploy を先頭に固定するのは、承認待ちだけが「本番が
#: 止まって待っている」種類だから。同種の中は待たせている順 (古い順)。
_KIND_ORDER = {"deploy": 0, "decision": 1, "loop": 2}

#: 判断カードに載せる問いの長さ。稼働状況ページの digest と同じ考え方で、
#: 全文は ``title`` 属性に入れる。
_QUESTION_CHARS = 140

#: conductor の停止理由 → カードの副題に出す短い語。
#:
#: **語彙の持ち主は mindwire** (``conductor/core.py`` の ``StopReason``)。
#: ここにあるのは訳であって定義ではない ∴ **知らない値は訳さずそのまま出す**
#: (下の ``_stop_reason_label``)。知らない語を黙って捨てると、mindwire が
#: 新しい停止理由を足した日に、板は理由が無いふりをする。
#:
#: ``human`` は「明示的な ``NEXT: human``」と「guard (i) の Tier-C 差し戻し」の
#: **両方**を指す。magickit からこの 2 つは区別できない (どちらも同じ token で
#: 来る) ので、区別できるふりをしない文言にしてある。
_STOP_REASON_LABELS = {
    "human": "人の判断で停止",
    "no_handoff_to_human": "NEXT が読めず人へ",
    "no_progress_to_human": "応答が無く人へ",
    "self_handoff_to_human": "自分宛の NEXT で人へ",
    "round_cap": "ラウンド上限",
    "empty_thread": "スレッドが空",
}


def _stop_reason_label(material: dict[str, Any]) -> str:
    """停止理由の表示語。記録が無ければ空文字。

    3 状態あり、**どれも別の意味**なので同じ見た目にしない:

    - 既知の token → 訳語
    - 知らない token → **その token をそのまま**。mindwire が
      ``StopReason`` に足した新しい理由が、magickit の辞書を待たずに
      画面へ出る。読めない語が出るのは、理由が消えるより良い。
    - 記録が無い (``None`` / 空) → 空文字。``stop_reason`` field が
      できる前に保存された材料と、mindwire がまだ送っていない期間が
      これに当たる。**「不明」とは書かない** — 理由が不明なのではなく、
      この材料には理由が付いていない。
    """
    token = str(material.get("stop_reason") or "").strip()
    if not token:
        return ""
    return _STOP_REASON_LABELS.get(token, token)

def _actor(request: Request) -> str | None:
    """列を動かした人。名乗りが無ければ ``None`` で、``"unknown"`` とは書かない。

    ``identity.tailnet_name`` は最後の手段として ``"unknown"`` を返すが、
    それはカードに「対応中へ たった今（unknown）」と印字されるということ。
    誰も名乗っていないことと、``unknown`` という名前の人が動かしたことは
    別 ∴ 前者は行を出さない (テンプレートは ``moved_by`` の有無で分岐する)。
    """
    if identity.tailnet_login(request) is None:
        return None
    return identity.tailnet_name(request)


#: ループ状態のうち「僕が動かさないと進まない」もの。``unmanaged`` は
#: 板に出さない: conductor が居ないプロジェクト (古い scratch 等) が
#: 恒久的にカードとして居座り、板の意味を薄める。
_LOOP_ON_BOARD = ("held", "stalled")


#: GitHub の PR ページ。``parse_pr_ref`` が返した参照からだけ作る。
_PR_URL = "https://github.com/{owner}/{repo}/pull/{number}"


@dataclass(frozen=True)
class CardLink:
    """カードから出ていく脇道 1 本 (題名のリンクとは別)。

    ``external`` はホストの外へ出るかどうか。テンプレートは真のときだけ
    新しいタブで開き ``rel`` を付ける ——— 板は 20 秒ごとに描き直るので、
    同じタブで GitHub に出ると戻ってきたときに板が別物になっている。
    """

    href: str
    label: str
    title: str = ""
    external: bool = False


@dataclass
class Card:
    """板に載る 1 枚。live な項目だけがこれになる。"""

    key: str
    kind: str
    title: str
    href: str
    #: 待ち始めた時刻。判断=材料の保存時刻 / deploy=申請時刻 / ループ=最終活動。
    since: datetime | None = None
    #: 問い、あるいは「なぜ僕を待っているか」の 1 行。
    note: str = ""
    note_full: str = ""
    project: str | None = None
    thread_id: str | None = None
    #: 副題として出す短い語 (停止疑いの閾値など)。
    detail: str = ""
    #: 題名の飛び先とは別に添える脇道 (PR / chatroom など)。
    links: list[CardLink] = field(default_factory=list)

    #: 移動時点の同一性。次の描画で変わっていたら「更新あり」。
    fingerprint: str = ""

    # lane store から埋まる
    lane: str = DEFAULT_LANE
    moved_at: datetime | None = None
    moved_by: str | None = None
    changed: bool = False

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def subline(self) -> str:
        """title の下の 1 行。**title に出ている語は繰り返さない。**

        ループカードの title は project 名そのもの ∴ 副題にもう一度出すと
        カード 1 枚で同じ語を 2 回読ませることになる。判断カードの title は
        スレッドのタイトルなので project は新しい情報。
        """
        parts = [p for p in (self.project, self.detail) if p and p != self.title]
        return " · ".join(parts)


@dataclass
class DoneCard:
    """完了列の 1 枚。live ではない ∴ 既見記録と、判れば理由だけを持つ。"""

    key: str
    kind: str
    title: str
    href: str
    last_seen_at: datetime | None
    reason: str

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)


@dataclass
class _Live:
    """collect の中間結果。カードと、読めなかったものの申告。"""

    cards: list[Card] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    #: 完了カードの理由づけに使う (project, thread_id) → thread。
    threads: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    #: 判断待ちの判定ができた project 集合。ここに無い project の消えた
    #: 判断カードには「進んだ」と書けない (読めていないだけかもしれない)。
    decided_projects: set[str] = field(default_factory=set)
    #: 鮮度は通ったが末尾が僕自身の決裁だったカードの ``item_key``。完了列の
    #: 理由づけがこれを見ないと「スレッドが進みました」と嘘をつく
    #: ——— 進んでいない。僕が答えたまま止まっているだけ。
    already_answered: set[str] = field(default_factory=set)


def _pr_link(*texts: str) -> CardLink | None:
    """最初に PR を名指している文字列から、その PR へのリンクを 1 本。

    判断材料は「どの PR の話か」を ``question`` の本文に書く形で運んで
    くる (実測 57 件中 26 件)。専用の欄は無い ∴ ここは本文から拾う。
    拾えないときに**推測しない**のがこの関数の全部で、板が指す PR は
    必ず材料がその文字列で名指したものになる。
    """
    for text in texts:
        ref = parse_pr_ref(text or "")
        if ref is None:
            continue
        return CardLink(
            href=_PR_URL.format(owner=ref.owner, repo=ref.repo, number=ref.number),
            label=f"PR #{ref.number}",
            title=ref.slug,
            external=True,
        )
    return None


def _shorten(text: str, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


# --- 判断待ち --------------------------------------------------------------


async def _collect_decisions(
    adapter: ChatroomAdapter, materials: list[dict[str, Any]], live: _Live
) -> None:
    """材料 × live スレッドの照合で「まだ僕の番」の判断だけを出す。

    規則は module docstring の 3 つ (鮮度 / 開いている / 人宛)。2 と 3 は
    判断ページの関数を**呼ぶ** ——— 同じ問いに 2 つの実装を持たせない。

    **片方が読めないときは J-fresh に倒さない**という向きはそのまま:
    スレッドが引けなかった project はカードを出さず、読めなかったと板の
    上に書く。ここで「たぶん待っている」と出すと、板は答え済みの決裁で
    埋まって読まれなくなる。

    ただし**駐機 msg が読めなかったときだけは逆に倒す** (カードを残す)。
    宛先が確認できないことと、宛先が他人であることは別で、前者で消すと
    「僕への依頼が黙って board から消える」という、この板が存在する理由
    そのものを壊す向きの取りこぼしになる。
    """
    by_project: dict[str, list[dict[str, Any]]] = {}
    for material in materials:
        by_project.setdefault(str(material.get("project") or ""), []).append(material)
    by_project.pop("", None)
    if not by_project:
        return

    #: 鮮度とスレッド状態を通ったカード。宛先の照合はこの後でまとめて行う
    #: ——— 駐機 msg は ``list_threads`` に含まれず 1 本ずつ取りに行く必要が
    #: あるので、**ここまで絞ってから**取る。head == tail を満たす材料だけが
    #: 対象なので実測で 1 桁本 (板全体で 5 本) にしかならない。
    candidates: list[Card] = []

    # 材料を持つ project だけ引く。板の判断カードは材料が前提なので、
    # 材料の無い project のスレッドを読んでも 1 枚も増えない。
    projects = sorted(by_project)
    results = await asyncio.gather(
        *(adapter.list_threads(project=p, limit=500) for p in projects),
        return_exceptions=True,
    )

    for project, result in zip(projects, results):
        if isinstance(result, BaseException) or ops._is_error(result):
            reason = (
                str(result)
                if isinstance(result, BaseException)
                else str(result.get("error", "thread 一覧が読めません"))
            )
            live.notices.append(
                f"{project}: スレッドが読めないため判断待ちを判定できません ({reason})"
            )
            continue

        threads = {
            str(t.get("thread_id")): t for t in (result.get("items") or [])
        }
        live.decided_projects.add(project)
        for thread_id, thread in threads.items():
            live.threads[(project, thread_id)] = thread

        for material in by_project[project]:
            thread_id = str(material.get("thread_id") or "")
            thread = threads.get(thread_id)
            if thread is None:
                # 一覧に無い = resolved で落ちた等。待っていない。
                continue
            head = thread.get("last_msg_id")
            if not head or head != material.get("head_msg_id"):
                continue  # スレッドが進んだ ∴ もう僕の番ではない
            if decisions._thread_write_state(thread) == "closed":
                # resolved / superseded。判断ページはこのスレッドに対して
                # 投稿フォームすら描かない ∴ 板がこれを「待っている」と
                # 言うと、開いた先で「クローズされています」に出迎えられる。
                # 鮮度では絶対に落ちない (解決 msg が末尾になるため)。
                continue
            question = str(material.get("question") or "")
            # 材料 (question → recommendation) → スレッド題名 の順に見る。
            # 材料が先なのは、それが「今の問い」だから: 題名は古い PR を
            # 名指したまま残ることがあるが、材料は駐機のたびに書き直る。
            pr = _pr_link(
                question,
                str(material.get("recommendation") or ""),
                str(thread.get("title") or ""),
            )
            candidates.append(
                Card(
                    key=f"decision:{project}:{thread_id}",
                    kind="decision",
                    title=str(thread.get("title") or thread_id),
                    href=f"/dashboard/decisions/{project}/{thread_id}",
                    links=[pr] if pr else [],
                    since=parse_ts(material.get("stored_at")),
                    note=_shorten(question, _QUESTION_CHARS),
                    note_full=question,
                    project=project,
                    thread_id=thread_id,
                    # 副題は「project · 停止理由」。判断カードの title は
                    # スレッドの題名なので、どちらも新しい情報になる。
                    detail=_stop_reason_label(material),
                    fingerprint=str(head),
                )
            )

    await _drop_the_ones_i_already_answered(adapter, candidates, live)


async def _drop_the_ones_i_already_answered(
    adapter: ChatroomAdapter, candidates: list[Card], live: _Live
) -> None:
    """自分の決裁が末尾に残っているだけのカードを落とす。

    判定は判断ページの :func:`decisions._is_human_decide` (``author`` が
    human かつ ``type == "decide"``)。答えた後もスレッドが動いていなければ
    材料は鮮度を保ったままなので、鮮度だけでは落ちない。

    **ここで ``NEXT:`` 行や ``next_participant`` を見てはいけない。**
    ``NEXT: Heisenberg`` のスレッドは僕の判断待ちでありうる (module
    docstring の guard (i))。落とすのは「僕が既に答えた」の 1 形だけで、
    「誰に宛てられているか」ではない。

    駐機 msg は ``list_threads`` には入っていない ∴ 1 本ずつ取りに行く。
    対象は鮮度とスレッド状態を通ったものだけ (実測 5 本) で、板全体でも
    1 桁本にしかならない ——— ``head == last_msg_id`` を満たす材料の数が
    そのまま上限になる。

    **読めなかったら残す。** 答えたと分かることと、確認できないことは別で、
    後者で消すのは「僕への依頼が黙って消える」向きの取りこぼし。
    """
    if not candidates:
        return

    results = await asyncio.gather(
        *(
            adapter.get_thread(project=str(c.project), thread_id=str(c.thread_id))
            for c in candidates
        ),
        return_exceptions=True,
    )

    for card, result in zip(candidates, results):
        if isinstance(result, BaseException) or ops._is_error(result):
            logger.warning(
                "board: parked msg unreadable, keeping the card",
                project=card.project,
                thread_id=card.thread_id,
                error=str(result),
            )
            live.cards.append(card)
            continue
        messages = result.get("messages") or []
        if not messages:
            # 権威が空を返した。宛先を確認できていない ∴ 残す側に倒す。
            live.cards.append(card)
            continue
        if decisions._is_human_decide(messages[-1]):
            # 自分の決裁が末尾。答えは出ている ∴ 次は誰かの番で、僕のでは
            # ない。告知は出さない: これは故障ではなく通常の流れで、完了列
            # のカードが理由を持っている。
            live.already_answered.add(card.key)
        else:
            live.cards.append(card)


# --- deploy 承認待ち -------------------------------------------------------


def _collect_deploys(live: _Live) -> None:
    """承認されていない deploy request。ローカル file store から。

    Conclair の可用性に依存しない ∴ chatroom が落ちている間もこの列だけ
    は正しい。読めなかったときは 0 件ではなく申告する (この board の
    既定の作法)。
    """
    try:
        requests = records.get_store().list_requests(limit=50)
    except Exception as e:  # noqa: BLE001 - 板全体を殺さない
        logger.warning("board: deploy store unreadable", error=str(e))
        live.notices.append(f"deploy の申請が読めません ({e})")
        return

    for request in requests:
        if request.status != records.STATUS_PENDING:
            continue
        live.cards.append(
            Card(
                key=f"deploy:{request.request_id}",
                kind="deploy",
                title=f"{request.target} の deploy 承認",
                # 一覧の頭ではなく**その申請の行**へ。承認ボタンは行の中に
                # あるので、申請が溜まっているときに一覧の頭に落とされると
                # 板から来た意味が無い。deploys.html はサーバ描画 ∴ 効く。
                href=f"/dashboard/deploys#deploy-{request.request_id}",
                since=parse_ts(request.created_at),
                note=_shorten(request.reason or "", _QUESTION_CHARS),
                note_full=str(request.reason or ""),
                detail=f"申請 {request.requested_by or '不明'}",
                fingerprint=request.status,
            )
        )


# --- 止まったループ --------------------------------------------------------


async def _collect_loops(
    adapter: ChatroomAdapter,
    summaries: list[dict[str, Any]],
    live: _Live,
    *,
    settings: Settings,
    now: datetime,
) -> None:
    """HOLD / 停止疑いのプロジェクトを 1 枚ずつ。

    判定は稼働状況ページの ``ops.classify`` をそのまま呼ぶ。あちらが
    読む field (``observed_at`` / ``last_activity_at`` / ``desired`` /
    ``configured`` / ``control_error``) は summary + control で全部
    揃うので、event と digest の取得 (あちらの N+1 の重い側) は要らない
    ——— 板は「何を話しているか」ではなく「誰が僕を待っているか」の板。
    """
    rows: list[ops.ProjectOps] = []
    for entry in summaries:
        by_status = entry.get("threads_by_status") or {}
        rows.append(
            ops.ProjectOps(
                project=str(entry.get("project", "")),
                open_threads=ops._open_count(by_status),
                awaiting=int(by_status.get("awaiting_reply", 0) or 0),
                gated=int(entry.get("gated_thread_count", 0) or 0),
                last_activity_at=parse_ts(entry.get("last_activity_at")),
            )
        )

    controls = await asyncio.gather(
        *(adapter.get_loop_control(project=row.project) for row in rows),
        return_exceptions=True,
    )
    for row, control in zip(rows, controls):
        if isinstance(control, BaseException) or ops._is_error(control):
            row.control_error = (
                str(control)
                if isinstance(control, BaseException)
                else str(control.get("error", "control read failed"))
            )
        elif isinstance(control, dict) and "desired_state" in control:
            row.desired = control.get("desired_state")
            row.desired_actor = control.get("desired_actor")
            row.configured = bool(control.get("configured"))
            row.observed = control.get("observed_state")
            row.observed_at = parse_ts(control.get("observed_at"))
        else:
            row.control_error = "conclair が control を返しませんでした"

    stall_seconds = settings.ops_stall_minutes * 60
    for row in rows:
        ops.classify(row, stall_seconds=stall_seconds, now=now)
        if row.status not in _LOOP_ON_BOARD:
            continue
        if row.status == "held":
            note = "HOLD で止めてあります。RESUME しない限り進みません。"
            detail = f"設定 {row.desired_actor or '不明'}"
        else:
            note = (
                f"{settings.ops_stall_minutes} 分以上動きがありません。"
                "長いターンの途中でも同じに見えます。"
            )
            detail = row.blocked_note or ""
        # 状態は副題に置き、title は project 名だけにする。両方に入れると
        # カード 1 枚で同じ語を 2 回読ませることになり、板の走査が遅くなる。
        detail = f"{row.status_label}{' · ' + detail if detail else ''}"
        live.cards.append(
            Card(
                key=f"loop:{row.project}",
                kind="loop",
                title=row.project,
                href="/dashboard",
                since=row.heartbeat_at,
                note=note,
                note_full=note,
                project=row.project,
                detail=detail,
                # 題名は /dashboard (RESUME の場所)。何を言ったきり止まって
                # いるのかは chatroom にしかないので、そこへも 1 本。
                links=[
                    CardLink(
                        href=f"/ui/projects/{row.project}/threads",
                        label="チャットルーム",
                        title=f"{row.project} のスレッド一覧",
                    )
                ],
                fingerprint=f"{row.status}:{row.desired or ''}",
            )
        )


# --- 完了列 ----------------------------------------------------------------


def _gone_reason(row: dict[str, Any], live: _Live) -> str:
    """カードが板から落ちた理由を、判るときだけ言う。

    判らないときに「あなたが対応しました」と書かないのがこの関数の全部。
    落ちた理由は「僕が答えた」とは限らない ——— 別の誰かが答えた、
    スレッドが resolved になった、deploy が走った、どれもありうる。
    """
    kind = row.get("kind")
    project = row.get("project")
    thread_id = row.get("thread_id")

    if str(row.get("item_key", "")) in live.already_answered:
        # スレッドは動いていない。末尾が僕自身の決裁なだけ ∴ 下の
        # 「進みました」に落とすと、起きていないことを断定する。
        return "あなたが回答済みです（決裁がスレッドの末尾にあります）"

    if kind == "decision" and project and thread_id:
        thread = live.threads.get((str(project), str(thread_id)))
        if thread is not None:
            status = str(thread.get("status") or "")
            if status == "resolved":
                return "スレッドが resolved になりました"
            return "スレッドが進みました（駐機 msg の後に発言があります）"
        if str(project) in live.decided_projects:
            return "スレッドが一覧から外れました（resolved など）"
        return "板から外れました（理由は確認できていません）"

    if kind == "deploy":
        request_id = str(row.get("item_key", "")).split(":", 1)[-1]
        try:
            request = records.get_store().load(request_id)
        except Exception:  # noqa: BLE001 - 理由が判らないだけ
            return "板から外れました（理由は確認できていません）"
        return f"deploy が {request.status} になりました"

    if kind == "loop":
        return "ループが HOLD / 停止疑いでなくなりました"

    return "板から外れました（理由は確認できていません）"


# --- 収集 ------------------------------------------------------------------


def _sort_key(card: Card) -> tuple[int, float]:
    """種別の優先、その中は**待たせている順 (古いものが上)**。

    新着順ではない。板の目的は腐らせないことで、8 日前から待っている項目が
    新しい依頼に押し下げられて画面外に出るのが、まさに避けたい形。新着は
    件数と各カードの経過時間で分かる (どのカードにも「何日前」が出ている)。

    ``since`` を持たないカードは末尾。時刻が読めないことを「たった今」とも
    「大昔」とも解釈しない。
    """
    kind_rank = _KIND_ORDER.get(card.kind, len(_KIND_ORDER))
    age = card.since.timestamp() if card.since else float("inf")
    return (kind_rank, age)


async def collect(
    settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """板 1 枚ぶんの context を作る。

    **GET なのに書く**: live なカードは毎回 ``touch_seen`` される。完了列
    は「消えた項目」の列で、消えた後に控えることは原理的にできない
    (:mod:`magickit.core.board_lanes` の docstring)。書き込みは冪等な
    UPSERT で、失敗しても板は描く ——— 完了列が欠けるのは、板全体が
    出ないことより遥かに軽い。
    """
    now = now or datetime.now(timezone.utc)
    live = _Live()

    material_store = DecisionMaterialStore(db_path=settings.db_path)
    lane_store = BoardLaneStore(db_path=settings.db_path)

    try:
        materials = await material_store.list_materials()
    except Exception as e:  # noqa: BLE001
        logger.warning("board: material store unreadable", error=str(e))
        materials = []
        live.notices.append(f"判断材料が読めません ({e})")

    # Conclair に依存しない列を先に。chatroom が落ちていても承認待ちは出る。
    _collect_deploys(live)

    adapter = ChatroomAdapter(
        base_url=settings.conclair_url, timeout=settings.conclair_timeout
    )
    try:
        try:
            summaries = await adapter.list_project_summaries()
        except Exception as e:  # noqa: BLE001
            summaries = {"error_type": "Unreachable", "error": str(e)}

        if ops._is_error(summaries) or "items" not in summaries:
            detail = (
                summaries.get("error", "")
                if isinstance(summaries, dict)
                else ""
            )
            live.notices.append(
                "conclair が読めないため、判断待ちと停止ループを判定できません"
                + (f" ({detail})" if detail else "")
            )
        else:
            await asyncio.gather(
                _collect_decisions(adapter, materials, live),
                _collect_loops(
                    adapter, summaries["items"], live, settings=settings, now=now
                ),
            )
    finally:
        await adapter.close()

    live.cards.sort(key=_sort_key)

    # lane を貼る。行が無いカードは new のまま (既定は行の不在で表す)。
    try:
        lanes = await lane_store.read_lanes()
    except Exception as e:  # noqa: BLE001 - lane が読めなくても板は出す
        logger.warning("board: lane store unreadable", error=str(e))
        lanes = {}
        live.notices.append(f"列の記録が読めません。全部を新着として表示します ({e})")

    for card in live.cards:
        row = lanes.get(card.key)
        if not row:
            continue
        lane = str(row.get("lane") or DEFAULT_LANE)
        card.lane = lane if lane in LANES else DEFAULT_LANE
        card.moved_at = parse_ts(row.get("moved_at"))
        card.moved_by = row.get("moved_by")
        # 動かした後で中身が入れ替わったカード。「対応中」に置いたまま
        # 別の問いに化けていることがあるので、黙って同じ顔をさせない。
        stored = row.get("fingerprint")
        card.changed = bool(stored and card.fingerprint and stored != card.fingerprint)

    columns = {lane: [] for lane, _, _ in LANE_COLUMNS}
    for card in live.cards:
        columns[card.lane].append(card)

    live_keys = {card.key for card in live.cards}

    try:
        await lane_store.touch_seen(
            SeenItem(
                item_key=card.key,
                kind=card.kind,
                title=card.title,
                project=card.project,
                thread_id=card.thread_id,
                href=card.href,
            )
            for card in live.cards
        )
        gone = await lane_store.list_gone(
            live_keys=live_keys,
            since=now - timedelta(days=settings.board_done_days),
        )
        done_error: str | None = None
    except Exception as e:  # noqa: BLE001 - 完了列だけ諦める
        logger.warning("board: seen store unusable", error=str(e))
        gone, done_error = [], str(e)

    done = [
        DoneCard(
            key=str(row.get("item_key")),
            kind=str(row.get("kind") or ""),
            title=str(row.get("title") or row.get("item_key")),
            href=str(row.get("href") or "#"),
            last_seen_at=parse_ts(row.get("last_seen_at")),
            reason=_gone_reason(row, live),
        )
        for row in gone
    ]

    return {
        "columns": columns,
        "lane_columns": LANE_COLUMNS,
        "done_column": DONE_COLUMN,
        "done": done,
        "done_error": done_error,
        "done_days": settings.board_done_days,
        "notices": live.notices,
        "total": len(live.cards),
        "checked_at": now,
        "stall_minutes": settings.ops_stall_minutes,
        "flash_error": None,
    }


# --- routes ----------------------------------------------------------------


@router.get("/dashboard/decisions", response_class=HTMLResponse)
async def board_page(request: Request) -> HTMLResponse:
    """板そのもの。中身は下の fragment が運ぶ。

    URL は据え置き。ここは 302 の置き石だった (「増分 3 で本物の一覧に
    差し替える」) ので、判断待ちを見に来た人の着地点がそのまま板になる。
    """
    return templates.TemplateResponse(
        request,
        "board.html",
        {"active_page": "board", "done_days": get_settings().board_done_days},
        headers={"Cache-Control": _NO_STORE},
    )


@router.get("/dashboard/decisions/_board", response_class=HTMLResponse)
async def board_fragment(request: Request) -> HTMLResponse:
    """列 4 本 (HTMX の poll 先)。

    ``_board`` は 1 セグメント ∴ 判断ページの
    ``/dashboard/decisions/{project}/{thread_id}`` (2 セグメント) とは
    衝突しない。
    """
    context = await collect(get_settings())
    return templates.TemplateResponse(
        request, "partials/board_columns.html", context,
        headers={"Cache-Control": _NO_STORE},
    )


@router.post("/dashboard/decisions/_lane", response_class=HTMLResponse)
async def board_set_lane(
    request: Request,
    item_key: str = Form(...),
    lane: str = Form(...),
    fingerprint: str = Form(""),
) -> HTMLResponse:
    """カードを列に置いて、板を描き直す。

    **完了 (``done``) は受け付けない。** 受け付ければ「実際には誰も
    答えていないのに完了列にあるカード」が作れてしまい、この板が信用
    できるという性質がそこで終わる。完了は live 集合から落ちたことの
    言い換えでしかない (module docstring)。

    描き直す対象は 1 枚ではなく板全体: 移動はカードを別の列へ動かす操作
    なので、その場で差し替えると次の poll まで元の列に残る。

    ``actor`` は tailnet の identity を **記録として** 残すだけで、認可
    ではない。lane は僕用の付箋で、書けても壊れるものが無い ∴ deploy
    承認のような allowlist は置かない。CSRF だけは既存の判定を使う ———
    他所のページから飛んでくる POST に意味は無いので、通す理由が無い。
    """
    settings = get_settings()
    error: str | None = None

    if identity.cross_site(request):
        error = "別サイトからの操作は受け付けません"
    elif lane not in LANES:
        # done はここに来る。名指しで理由を返す (黙って弾かない)。
        error = (
            "完了列にはドラッグで置けません。"
            "決裁・承認・RESUME が実際に起きたときだけ移ります。"
            if lane == DONE_COLUMN[0]
            else f"知らない列です: {lane!r}"
        )
    else:
        store = BoardLaneStore(db_path=settings.db_path)
        try:
            await store.set_lane(
                item_key=item_key,
                lane=lane,
                fingerprint=fingerprint or None,
                actor=_actor(request),
            )
        except Exception as e:  # noqa: BLE001 - 板は描いたまま失敗を見せる
            logger.warning(
                "board: lane write failed", item_key=item_key, lane=lane, error=str(e)
            )
            error = f"列を保存できませんでした ({e})"

    context = await collect(settings)
    context["flash_error"] = error
    return templates.TemplateResponse(
        request, "partials/board_columns.html", context,
        headers={"Cache-Control": _NO_STORE},
    )


__all__ = [
    "router",
    "collect",
    "Card",
    "CardLink",
    "DoneCard",
    "LANE_COLUMNS",
    "DONE_COLUMN",
]
