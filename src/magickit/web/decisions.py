r"""判断 (decision) ページ ― S5 増分 2 + S5'' 材料.

Discord alert の通知本文が指す `/dashboard/decisions/{project}/{thread_id}`
を、増分 1 のリダイレクト stub から**判断 UI 本体**に差し替える。URL は増分 1
で敷いた契約のまま (`msg-087 §1` / spec `spec/slices/S5-decision-page.md`)、
ハンドラの中身だけを差し替える。**S5'' で、判断 UI 分岐に材料 (question /
options / gain / loss / …) を運び、3 状態 (J-fresh / J-stale / J-absent) に
切り分けて描画する** (spec ``spec/slices/S5-decision-materials.md``)。

このモジュールに置くべき理由 (触る前に読む 2 行)
--------------------------------------------------

- **この URL は出荷済みの契約である。** 動かさない。増分 1 と増分 2 の違いは
  「同じ URL のハンドラが何を返すか」だけであって、URL そのものではない。S5''
  も同じ規律 — 3 状態はハンドラ内部の切り分けであって URL の分割ではない。
- **CI はページが外から見えるかを知らない。** 到達確認は :8443 に curl -L で
  外から叩き、最終 200 とタイトルを確かめる。CI 緑は必要条件、十分条件ではない
  (msg-084 §5。「実装済みに見えて何も出さない」型の事故は CI を素通りする)。
  S5'' の受入基準 A-14 (材料の逐語 pin) はこの規律の実体である。

D-26' の 4 分岐 (spec ``S5-decision-page.md`` §1)
--------------------------------------------------

Bohr の 3 分岐は「答えが得られたときの分類」であり、実装は必ず 4 通りになる:

- 存在し、駐機中 → 200 判断 UI (S5'' で J-fresh / J-stale / J-absent の 3 状態に分割)
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

駐機の判定 (spec ``S5-decision-page.md`` §1 / msg-096 §2)
---------------------------------------------------------

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

S5'' — 判断材料の 3 状態 (spec ``S5-decision-materials.md`` §3)
----------------------------------------------------------------

駐機中 (mode=judgement) に到達した時、`decision_materials` テーブルを読み、
`thread.last_msg_id` と材料の `head_msg_id` を比較して 3 状態を決める:

- **J-fresh**: 材料あり ∧ 完全一致 → question / options / gain / loss / 推奨 /
  unknowns を描画。選択肢ボタンの `content` は composer の option から
  ``f"{id}: {label}"`` で導出 (spec §4.1)。
- **J-stale**: 材料あり ∧ 完全一致でない (または head_msg_id が読めない) →
  警告 + chatroom 導線 + 判断フォーム。**材料は 1 文字も描画しない** (I-14 /
  spec §4-1 / §4.2)。CSS で隠すのでは足りない — view-source / コピペ /
  スクリーンリーダに届く ∴ サーバ側で描画しない。
- **J-absent**: 材料なし → 「材料が用意されていません」+ chatroom 導線 +
  判断フォーム。**tail (末尾数通) を描画しない** (spec §4.3 / Einstein
  msg-112 §3)。

**fail-to-stale の既定** (spec §3.2): `thread.last_msg_id` が読めない
(欠落 / null / 空文字) ときは **J-stale に倒す**。J-fresh に落とさない。
「新しい」と主張してはならない (D-26' の 503 と同規律)。**P-9 が破れたとき
の症状を「J-fresh が永久に出ない」に固定する** — この既定の価値は P-9 が
真であることに依存しない (msg-118 §4 の Tier-C 指示)。

エスケープ (spec §4-2 / Tier-C msg-118 §3)
-------------------------------------------

`html.unescape()` を**入れない** (無条件)。保存本文にエンティティは無く
(Tier-C 実測)、混入があるなら描画経路 ∴ 直す場所は表示側ではない。
材料は raw text として template に渡し、Jinja autoescape に任せる。
``|safe`` は 1 か所も使わない (lint テストで拒否 — spec §5)。
"""

from __future__ import annotations

import re
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from magickit.config import get_settings
from magickit.core.decision_materials import DecisionMaterialStore
from magickit.mcp.tools import chatroom as chatroom_tools
from magickit.utils.logging import get_logger
from magickit.web.deps import templates

logger = get_logger(__name__)

router = APIRouter(tags=["decisions"])

#: S5'' の材料 store を作る factory. tests でここを patch すれば in-memory /
#: temp file の store を注入できる (module-level defaults を触らずに済む形)。
#: 通常運用では ``get_settings().db_path`` を読み、``StateManager`` と同じ
#: SQLite file を共有する。
def _get_material_store() -> DecisionMaterialStore:
    """Return a fresh ``DecisionMaterialStore`` bound to the configured db.

    Called per-request; the store itself is stateless in Python and opens
    a new aiosqlite connection per call. Tests patch this function to inject
    a temp-file store (see ``tests/unit/test_decision_materials_*.py``).
    """
    settings = get_settings()
    return DecisionMaterialStore(db_path=settings.db_path)

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


def _head_msg_id_from_thread(thread_meta: dict[str, Any]) -> str | None:
    """Return the head msg id (``thread.last_msg_id``) or ``None``.

    spec ``S5-decision-materials.md`` §3.1: freshness SOT is the thread
    rollup, not ``messages[-1].msg_id``. The rollup is mode-independent
    (``chatroom.py`` L1678-1682 docstring); using ``messages[-1]`` makes
    the classification depend on whether the messages list happens to be
    windowed, and a future caller changing ``mode=`` would silently
    invalidate freshness.

    Missing / null / empty string all collapse to ``None`` -- the caller
    must treat that as "we don't know" and **fall to J-stale, not J-fresh**
    (fail-to-stale, spec §3.2). The value of the fallback is that P-9
    ("last_msg_id exists in live") breaking never expresses as J-fresh
    silently returning an old material.
    """
    value = thread_meta.get("last_msg_id")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _classify_judgement_state(
    material: dict[str, Any] | None,
    head_msg_id: str | None,
) -> str:
    """Return one of ``"fresh"`` / ``"stale"`` / ``"absent"``.

    spec §3 ordering:

    1. No material stored → ``"absent"``.
    2. Material stored, but head unknown → ``"stale"`` (fail-to-stale).
    3. ``material.head_msg_id`` matches ``head_msg_id`` (byte-for-byte,
       no normalization) → ``"fresh"``.
    4. Otherwise → ``"stale"``.

    The comparison is a plain ``==`` on strings. Normalization is
    deliberately absent -- see spec §3.1 ("comparing after normalization
    would interpret the other side's internal representation, which
    msg-111 §3 forbids").
    """
    if material is None:
        return "absent"
    if head_msg_id is None:
        return "stale"
    material_head = material.get("head_msg_id")
    if not isinstance(material_head, str) or material_head != head_msg_id:
        return "stale"
    return "fresh"


# ---------------------------------------------------------------------------
# N-1〜N-8: 「判断待ちではありません」を 3 状態に割る (msg-136 / msg-137 / Einstein)
# ---------------------------------------------------------------------------
#
# 発端 msg-136: 「判断しろ」と言われて開いた先に何も無い、が続くと通知経路
# 全体が読まれなくなる ∴ 「判断待ちではありません」を 3 状態に割り、それぞれ
# 根拠のある文言を出す (spirrow-mindwire T-human-terminal-overuse msg-882 の
# 「常時点灯する列は読まれなくなる」原則の実体化)。
#
# 設計は Bohr msg-137、Einstein msg (Objection: closed thread) で closed 対応を
# 追加。判定は純関数 1 本に閉じる (テスト可能性・追加の外部 I/O 0)。

#: 判定できない reason enum. **UI 側で reason ごとに 1 行を出す** (msg-137 §5)。
_NW_STORE_UNAVAILABLE = "store_unavailable"
_NW_NO_MATERIAL = "no_material"
_NW_MATERIAL_HEAD_UNREADABLE = "material_head_unreadable"
_NW_NO_MESSAGES = "no_messages"
_NW_HEAD_NOT_IN_WINDOW = "head_not_in_window"
_NW_TAIL_INCOMPLETE = "tail_incomplete"
_NW_HEAD_IS_CURRENT = "head_is_current"


def _classify_not_waiting(
    material: dict[str, Any] | None,
    material_ok: bool,
    messages: list[dict[str, Any]],
    thread_head: str | None,
) -> tuple[str, dict[str, Any]]:
    """「判断待ちではありません」の 3 状態を返す (総関数)。

    Returns ``(state, evidence_or_reason)`` where ``state`` is one of:

    - ``"answered"``: material の ``head_msg_id`` より後ろに human の
      ``type == "decide"`` msg が窓内にある。``evidence`` は該当する
      **最初の** 1 通 (msg_id / author / timestamp を UI で描画)。
    - ``"advanced"``: head は動いたが window の末尾は権威と一致し、その
      間に human decide は無い → 「その後スレッドが進んでいます」。
    - ``"undetermined"``: 判定できない (材料が無い / 窓が届かない / 末尾
      不完全 / head が最終)。**現行の「判断待ちではありません」文言を
      出しつつ、reason ごとの 1 行を添える** (msg-137 §5)。

    判定順序自体が仕様 (msg-137 §1):

    1. ``material_ok`` が偽 → ``undetermined / store_unavailable``
    2. ``material`` が無い → ``undetermined / no_material``
    3. ``material.head_msg_id`` が非空 str でない → ``undetermined /
       material_head_unreadable`` (schema 上到達不能だが総関数化のため置く)
    4. ``messages`` が空 → ``undetermined / no_messages``
    5. head が ``messages`` の ``msg_id`` に存在しない → ``undetermined /
       head_not_in_window``
    6. head 以降に ``author ∈ HUMAN_IDENTITY_NAMES ∧ type == "decide"`` が
       あれば → ``answered`` (最初の 1 通)
    7. 6 の後は否定の主張 ∴ 窓の末尾完全性を要求する。
       ``messages[-1].msg_id != thread_head`` → ``undetermined /
       tail_incomplete``
    8. head の後ろに msg がある → ``advanced``
    9. head が最終 msg (後ろが空) → ``undetermined / head_is_current``

    **不変条件** (msg-137 §2 の中心規律):
        肯定の主張には証拠が要る (6)。否定の主張には完全性が要る (7)。
        どちらも満たせないものは全部 ``undetermined`` に落とす。
    """
    # (1) 材料ストアが読めなかった
    if not material_ok:
        return "undetermined", {"reason": _NW_STORE_UNAVAILABLE}
    # (2) 材料が無い
    if material is None:
        return "undetermined", {"reason": _NW_NO_MATERIAL}
    # (3) 材料の head_msg_id が読めない (schema 上到達不能だが暗黙 fall-through
    # を作らない)
    head = material.get("head_msg_id")
    if not isinstance(head, str) or not head:
        return "undetermined", {"reason": _NW_MATERIAL_HEAD_UNREADABLE}
    # (4) messages が空
    if not messages:
        return "undetermined", {"reason": _NW_NO_MESSAGES}
    # (5) 所属テスト: head が窓内に無い
    # msg_id はリストの契約 (Conclair は msg_id 昇順で返す) ∴ 「順序は契約から
    # 取り、値の意味は取らない」(msg-137 §3)。数値 parse をしない。
    head_index: int | None = None
    for i, msg in enumerate(messages):
        if str(msg.get("msg_id") or "") == head:
            head_index = i
            break
    if head_index is None:
        return "undetermined", {"reason": _NW_HEAD_NOT_IN_WINDOW}
    after = messages[head_index + 1 :]
    # (6) 肯定側: 証拠 msg があれば末尾完全性を要求せず answered
    for msg in after:
        if _is_human_decide(msg):
            return "answered", {
                "msg_id": str(msg.get("msg_id") or ""),
                "author": str(msg.get("author") or ""),
                "timestamp": _msg_timestamp(msg),
            }
    # (7) 否定側: 末尾完全性を要求する (msg-137 §1 の 7 の位置)
    tail = str(messages[-1].get("msg_id") or "")
    if not thread_head or not tail or tail != thread_head:
        return "undetermined", {"reason": _NW_TAIL_INCOMPLETE}
    # (8) 後ろに msg がある → advanced
    if after:
        return "advanced", {"thread_head": thread_head, "material_head": head}
    # (9) 後ろが空 → head が最終 = 通知が指していた msg が判断を求めていない
    return "undetermined", {"reason": _NW_HEAD_IS_CURRENT}


def _is_human_decide(msg: dict[str, Any]) -> bool:
    """``author ∈ HUMAN_IDENTITY_NAMES`` かつ ``type == "decide"`` か。

    msg-137 §4: **構造化 field のみ**。本文 fallback は作らない。「回答済み」
    に対応する本文規約は存在せず、fallback は「散文から意図を推定する」形
    になり、本スレッドが止めようとしている失敗そのもの (根拠のない断定)。

    - ``author`` は ``_is_parked_to_human`` と同じく ``lower()`` で扱う (2 箇所
      で別の規則を持たない)。
    - ``type`` は case 完全一致。Conclair は enum ∴ 大小が変わっていたら契約
      変更で、黙って吸収せず「回答済みでない」= 安全側に出す。
    - **field 名は ``type``** (msg-137 実測 (d): MCP 引数名は ``msg_type``、
      保存・返却 field は ``type``)。``msg_type`` だけの msg は検出しない。
    """
    author = str(msg.get("author") or "").strip().lower()
    if author not in _HUMAN_IDENTITIES:
        return False
    msg_type = str(msg.get("type") or "")
    return msg_type == "decide"


#: msg の投稿時刻を拾う候補 field。実運用値が確定していない ∴ 見つからない
#: 場合は None を返し UI 側で「時刻欄ごと省く」(msg-137 §5 N-6: 時刻の欠落は
#: verdict を弱めない — 証拠 msg は手にある)。順序は Conclair の他 API で見た
#: 頻度順 (`created_at` が listing で権威、`timestamp` は caller-supplied 表示)。
_MSG_TIMESTAMP_FIELDS = ("created_at", "timestamp")


def _msg_timestamp(msg: dict[str, Any]) -> str | None:
    """Return the msg's timestamp string, or ``None`` if no known field carries it.

    See ``_MSG_TIMESTAMP_FIELDS``. Returning ``None`` is the intended
    fallback — the UI drops the timestamp column rather than writing
    「不明」(msg-137 §5 N-6).
    """
    for f in _MSG_TIMESTAMP_FIELDS:
        v = msg.get(f)
        if isinstance(v, str) and v:
            return v
    return None


def _default_next_participant_for_not_waiting(
    messages: list[dict[str, Any]],
) -> str:
    """N-7: not_waiting のフォームの ``next_participant`` 既定値。

    msg-137 §5: 「いまスレッドが待っている相手」に返すのが正しい宛先で、
    駐機が無いこの分岐で ``_parked_author`` を流用すると意味がずれる ∴
    最終 msg の構造化 ``next_participant``、無ければ最終 msg の著者。
    どちらも空なら空文字 (template 側で participant_choices の先頭が
    selected される)。
    """
    if not messages:
        return ""
    last = messages[-1]
    np = last.get("next_participant")
    if isinstance(np, str) and np:
        return np
    return str(last.get("author") or "")


def _thread_is_closed(thread_meta: dict[str, Any]) -> bool:
    """Return True when the thread rollup marks the thread as closed.

    Einstein Objection (msg after Bohr's design): closed スレッドへの新規
    メッセージ書き込みは Conclair が ``ChatroomStateError`` で確実に拒否する
    ∴ closed に送信可能なフォームを描画すると「嘘の導線」になる。

    Conclair の状態 enum は ``active / awaiting_reply / resolved / superseded
    / parked`` (chatroom.py L1620-1621) ∴ 「決着した」の実体は ``resolved``
    と ``superseded``。field 名は listing の "status" (chatroom.py L1629)。
    ここでは case 完全一致で拾う (それ以外は "active" 相当として扱う =
    フォームを描画する = 安全側の default)。
    """
    status = str(thread_meta.get("status") or "")
    return status in ("resolved", "superseded")


def _thread_page_url(project: str, thread_id: str) -> str:
    """`/ui` 側のスレッドページ URL を組み立てる。

    増分 1 と同じく ``quote(safe="")`` で各セグメントを自前 encode。
    ``RedirectResponse`` を使わないのも増分 1 の理由と同じ (D-C)。
    """
    return (
        f"/ui/projects/{quote(project, safe='')}"
        f"/threads/{quote(thread_id, safe='')}"
    )


async def _load_material(
    project: str, thread_id: str
) -> tuple[dict[str, Any] | None, bool]:
    """材料 store の 1 read を try/except に包む共通ヘルパ (msg-137 §6)。

    Returns ``(material, ok)``:

    - ``ok=True, material=dict``: 材料あり
    - ``ok=True, material=None``: 材料なし (row 不在)
    - ``ok=False, material=None``: store が例外 (fail-to-<something>)

    判断 UI 側 (``material_state``) は既存挙動を保つため ``ok`` を読まない
    (store 例外 → J-absent のまま)。新分岐 (``_classify_not_waiting``) だけが
    ``ok`` を読んで ``store_unavailable`` を区別する。既存の J-* テストが
    behavior-preserving の回帰ガード。
    """
    try:
        store = _get_material_store()
        material = await store.get_material(project=project, thread_id=thread_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "decision page: material lookup raised",
            project=project, thread_id=thread_id, error=str(e),
        )
        return None, False
    return material, True


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


def _choice_options_from_material(material: dict[str, Any] | None) -> list[dict[str, str]]:
    """Derive the judgement page's choice buttons from composer material.

    spec §4.1: the hardcoded 2-choice fallback is **abolished** (msg-117 §5).
    Choice buttons only appear in J-fresh, and their ``content`` value is
    ``f"{id}: {label}"`` -- ``id`` is preserved so the received msg carries
    which option was selected, not just the text.

    Returns ``[]`` when material is absent, options are missing, or any
    option lacks an id/label. Empty return means "render no choice
    buttons"; the "自由記述だけで送る" I-12 sentinel button is separate
    and stays in the template for all 3 states.
    """
    if not material:
        return []
    options = material.get("options")
    if not isinstance(options, list):
        return []
    built: list[dict[str, str]] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        opt_id = opt.get("id")
        label = opt.get("label")
        if not isinstance(opt_id, str) or not opt_id:
            continue
        if not isinstance(label, str) or not label:
            continue
        built.append({
            "id": opt_id,
            "label": label,
            "gain": str(opt.get("gain") or ""),
            "loss": str(opt.get("loss") or ""),
            # Value on the wire mirrors what the human sees. Chatroom
            # parser expects the choice content to be the exact label
            # the button showed (spec §4.1, matches msg-121 実タップ形).
            "value": f"{opt_id}: {label}",
        })
    return built


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

    S5'' 更新: material も取り直す。J-fresh / J-stale / J-absent の 3 状態は
    D-31 でも維持する — POST 中に material 側が更新される可能性があり、
    「送信時は J-fresh、再描画時は J-stale」という遷移が起こりうる。
    その場合は再描画側の状態を出す (**入力は残す**)。
    """
    # 文脈の復元は best-effort。落ちても入力保持が優先。
    parked_author = next_participant_value
    participant_choices = [next_participant_value] if next_participant_value else list(_HUMAN_IDENTITIES)
    thread_title = thread_id
    head_msg_id: str | None = None
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
            head_msg_id = _head_msg_id_from_thread(thread_meta)
            if messages:
                last = messages[-1]
                candidate_parked = _parked_author(last)
                if candidate_parked:
                    parked_author = candidate_parked
                participant_choices = _participant_choices(messages, parked_author)
    # 「human」だけは常に含める。
    for h in _HUMAN_IDENTITIES:
        if h not in participant_choices:
            participant_choices.append(h)
    # next_participant_value も候補に無ければ加える (D-31 で消さない)。
    if next_participant_value and next_participant_value not in participant_choices:
        participant_choices.insert(0, next_participant_value)

    # Material lookup is best-effort. A store outage falls to J-absent
    # (safer than pretending we have material) rather than 500. ``ok`` is
    # unused here — the judgement UI keeps the pre-N-* behavior (store outage
    # is J-absent). msg-137 §6: the shared helper deliberately does not change
    # observable behavior for the judgement branch.
    material, _material_ok = await _load_material(project, thread_id)

    material_state = _classify_judgement_state(material, head_msg_id)
    # J-stale 描画では材料テキストを template に渡さない (I-14 / spec §4-1)。
    # サーバ側で描画しないと言うことは、テンプレートに文字列を渡さないこと
    # で担保する (テンプレート側の if で「隠す」実装は I-14 を通さない)。
    material_for_render = material if material_state == "fresh" else None
    choice_options = _choice_options_from_material(material_for_render)

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
            "participant_choices": participant_choices,
            "content_value": content_value,
            "freeform_value": freeform_value,
            "next_participant_value": next_participant_value or parked_author,
            "error_message": error_message,
            "material_state": material_state,
            "material": material_for_render,
            "material_head_msg_id": (material or {}).get("head_msg_id"),
            "thread_head_msg_id": head_msg_id,
            "choice_options": choice_options,
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
        # N-*: 「判断待ちではありません」を 3 状態に割る (msg-136 / msg-137).
        # 材料と messages を元に、answered / advanced / undetermined を判定して
        # 根拠を添える。判定できないもの (材料が無い・窓が届かない・末尾不完全
        # など) は全部 undetermined に落ちる — 「回答済み」を根拠なく言わない、
        # という中心規律 (msg-137 §2 の不変条件)。
        material, material_ok = await _load_material(project, thread_id)
        thread_head = _head_msg_id_from_thread(thread_meta)
        nw_state, nw_evidence = _classify_not_waiting(
            material=material,
            material_ok=material_ok,
            messages=messages,
            thread_head=thread_head,
        )
        # Einstein Objection: closed スレッドではフォームを描画しない
        # (Conclair が ChatroomStateError で拒否する ∴ 送信可能な form を
        # 出すと嘘の導線になる)。
        thread_closed = _thread_is_closed(thread_meta)
        return templates.TemplateResponse(
            request,
            "decisions_thread.html",
            {
                "mode": "not_waiting",
                "project": project,
                "thread_id": thread_id,
                "thread_ui_url": thread_ui_url,
                "thread_title": thread_meta.get("title") or thread_id,
                "not_waiting_state": nw_state,
                "not_waiting_evidence": nw_evidence,
                "thread_closed": thread_closed,
                "thread_status": str(thread_meta.get("status") or ""),
                # I-12: フォームは 3 状態すべてで残す (msg-136 §4)。ただし
                # closed スレッドでは template 側で描画しない。既定は最終
                # msg の next_participant、無ければ最終 msg の著者
                # (msg-137 §5 N-7)。
                "next_participant_value": _default_next_participant_for_not_waiting(
                    messages
                ),
                "participant_choices": _participant_choices(
                    messages, _default_next_participant_for_not_waiting(messages)
                ),
            },
            status_code=200,
            headers={"Cache-Control": _NO_STORE},
        )

    # 駐機中 → 判断 UI (200)。S5'' で 3 状態に切り分ける。
    last = messages[-1]
    parked_author = _parked_author(last)
    head_msg_id = _head_msg_id_from_thread(thread_meta)

    # Material lookup is best-effort. spec §3.2 fail-to-stale の一貫適用:
    # store が落ちても J-absent (「材料が無い」) に倒す。J-fresh には決して
    # 落ちない — 我々が確認できなかったことを「新しい」と言わない
    # (msg-093 §2 一般則 / msg-109 §7)。判断 UI 側は ``ok`` を読まない
    # (既存挙動を維持) — store 例外は J-absent のまま。
    material, _material_ok = await _load_material(project, thread_id)

    material_state = _classify_judgement_state(material, head_msg_id)
    # I-14 (spec §4-1): J-stale では材料テキストを template に渡さない。
    # 「隠す」ではなく「渡さない」で担保する — 隠された文字列は view-source
    # / コピペ / スクリーンリーダに届く。J-fresh のみ渡す。
    material_for_render = material if material_state == "fresh" else None
    choice_options = _choice_options_from_material(material_for_render)

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
            "participant_choices": _participant_choices(messages, parked_author),
            # 空の初期状態 (D-31 再描画ではないので入力保持なし)。
            "content_value": "",
            "freeform_value": "",
            "next_participant_value": parked_author,
            "error_message": None,
            "material_state": material_state,
            "material": material_for_render,
            "material_head_msg_id": (material or {}).get("head_msg_id"),
            "thread_head_msg_id": head_msg_id,
            # 汎用 2 択は廃止 (spec §4.1 / msg-117 §5). J-fresh のときだけ
            # composer の option 由来のカードを出す。J-stale / J-absent は
            # 空配列 ∴ template は「自由記述だけで送る」ボタンだけを出す。
            "choice_options": choice_options,
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


# --- S5'' 材料 API (spec spec/slices/S5-decision-materials.md §1) ---------

#: composer_status のリテラル "ok" (spec §1.3)。等値比較 1 回のみ ∴
#: parse ではない (`_decision_form == "1"` と同型)。
_COMPOSER_STATUS_OK = "ok"


def _bad_request(error_type: str, error: str, **details: Any) -> JSONResponse:
    """Return a Conclair-style error envelope as JSON (400).

    The judgement page and MCP tools both talk in ``{error_type, error,
    details}`` envelopes; mindwire's push is a callback from the loop,
    which reads envelopes in the same shape. Same schema, one renderer.
    """
    return JSONResponse(
        status_code=400,
        content={
            "error_type": error_type,
            "error": error,
            "details": details,
        },
    )


@router.put("/v1/decisions/{project}/{thread_id}/material")
async def put_decision_material(
    project: str,
    thread_id: str,
    body: Annotated[dict[str, Any], Body()],
) -> JSONResponse:
    """UPSERT the composer material for ``(project, thread_id)`` (spec §1.1).

    Wire shape (spec §1.1 table). ``head_msg_id`` is the only required
    field; ``composer_status``, if present, must equal ``"ok"`` (spec
    §1.3 -- we fail closed on the receive side even though mindwire is
    supposed to filter first). All other fields are optional.

    **Idempotency is structural**: SQLite ``INSERT OR REPLACE`` on
    ``UNIQUE(project, thread_id)``. Response includes ``replaced`` so
    callers can distinguish first-write from update.

    **No auth here** (spec §9 / P-10). Auth is a land-order-3 concern
    measured after the endpoint exists -- "測る前に塞がない"
    (msg-122 §4). When P-10 is measured, add the auth check *at this
    boundary*, not by inventing a hybrid API path.
    """
    if not isinstance(body, dict):
        return _bad_request(
            "InvalidMaterialPayload", "request body must be a JSON object"
        )

    head_msg_id = body.get("head_msg_id")
    if not isinstance(head_msg_id, str) or not head_msg_id:
        return _bad_request(
            "InvalidMaterialPayload",
            "head_msg_id is required and must be a non-empty string",
            head_msg_id=head_msg_id,
        )

    composer_status = body.get("composer_status")
    # spec §1.3: 存在 ∧ "ok" 以外 → 400 で拒否、部分保存しない。
    # 欠けている場合は検査しない (欠けている = "ok" と等価。過剰な要求を
    # 供給側に押し付けない)。
    if composer_status is not None and composer_status != _COMPOSER_STATUS_OK:
        return _bad_request(
            "ComposerStatusNotOk",
            f"composer_status must be {_COMPOSER_STATUS_OK!r} to persist",
            composer_status=composer_status,
        )

    # Optional fields with light shape validation. We do not validate
    # inner shapes (e.g. option dict keys) beyond "is it a list" -- the
    # renderer is defensive (`_choice_options_from_material` filters
    # malformed rows), and rejecting on shape here would create a
    # coupling where mindwire's next composer field addition requires a
    # magickit schema bump. Keep the receiver permissive; keep the
    # renderer strict (spec §4.1 filter).
    signature = body.get("signature")
    if signature is not None and not isinstance(signature, str):
        return _bad_request(
            "InvalidMaterialPayload",
            "signature must be a string when present",
        )
    question = body.get("question")
    if question is not None and not isinstance(question, str):
        return _bad_request(
            "InvalidMaterialPayload",
            "question must be a string when present",
        )
    options = body.get("options")
    if options is not None and not isinstance(options, list):
        return _bad_request(
            "InvalidMaterialPayload",
            "options must be a list when present",
        )
    recommendation = body.get("recommendation")
    if recommendation is not None and not isinstance(recommendation, str):
        return _bad_request(
            "InvalidMaterialPayload",
            "recommendation must be a string when present",
        )
    recommendation_reason = body.get("recommendation_reason")
    if recommendation_reason is not None and not isinstance(
        recommendation_reason, str
    ):
        return _bad_request(
            "InvalidMaterialPayload",
            "recommendation_reason must be a string when present",
        )
    unknowns = body.get("unknowns")
    if unknowns is not None and not isinstance(unknowns, list):
        return _bad_request(
            "InvalidMaterialPayload",
            "unknowns must be a list when present",
        )

    store = _get_material_store()
    try:
        result = await store.put_material(
            project=project,
            thread_id=thread_id,
            head_msg_id=head_msg_id,
            signature=signature,
            question=question,
            options=options,
            recommendation=recommendation,
            recommendation_reason=recommendation_reason,
            unknowns=unknowns,
        )
    except Exception as e:  # noqa: BLE001 - report the store outage instead of 500
        logger.error(
            "decision material PUT: store raised",
            project=project, thread_id=thread_id, error=str(e),
        )
        return JSONResponse(
            status_code=503,
            content={
                "error_type": "MaterialStoreUnavailable",
                "error": str(e),
                "details": {"project": project, "thread_id": thread_id},
            },
        )

    return JSONResponse(status_code=200, content=result)


@router.get("/v1/decisions/{project}/{thread_id}/material")
async def get_decision_material(project: str, thread_id: str) -> JSONResponse:
    """Read back the stored material (spec §1.2).

    **Purpose is external-facing verification** — the loop reads the
    judgement page's own SQLite (no HTTP round-trip needed). This
    endpoint exists so P-10 (auth) can be measured with "write then read"
    (msg-118 §5 Tier-C direction: not "200 came back" but "the content I
    PUT reads back").

    404 (`MaterialNotStored`) when the row is missing; 200 with the
    stored body otherwise.
    """
    store = _get_material_store()
    try:
        material = await store.get_material(project=project, thread_id=thread_id)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "decision material GET: store raised",
            project=project, thread_id=thread_id, error=str(e),
        )
        return JSONResponse(
            status_code=503,
            content={
                "error_type": "MaterialStoreUnavailable",
                "error": str(e),
                "details": {"project": project, "thread_id": thread_id},
            },
        )

    if material is None:
        return JSONResponse(
            status_code=404,
            content={
                "error_type": "MaterialNotStored",
                "error": "no material stored for this (project, thread_id)",
                "details": {"project": project, "thread_id": thread_id},
            },
        )
    return JSONResponse(status_code=200, content=material)


__all__ = [
    "router",
    "_is_parked_to_human",
    "_has_standalone_next_line",
    "_is_not_found",
    "_render_decision_error_page",
    "_thread_page_url",
    "_participant_choices",
    "_head_msg_id_from_thread",
    "_classify_judgement_state",
    "_choice_options_from_material",
    "_get_material_store",
    "_COMPOSER_STATUS_OK",
    # N-1〜N-8: 「判断待ちではありません」の 3 状態分類 (msg-137)
    "_classify_not_waiting",
    "_is_human_decide",
    "_msg_timestamp",
    "_load_material",
    "_default_next_participant_for_not_waiting",
    "_thread_is_closed",
    "_NW_STORE_UNAVAILABLE",
    "_NW_NO_MATERIAL",
    "_NW_MATERIAL_HEAD_UNREADABLE",
    "_NW_NO_MESSAGES",
    "_NW_HEAD_NOT_IN_WINDOW",
    "_NW_TAIL_INCOMPLETE",
    "_NW_HEAD_IS_CURRENT",
]
