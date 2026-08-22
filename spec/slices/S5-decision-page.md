# S5 判断 (decision) page ― 増分 2 spec

判断依頼通知 (Discord alert) から人が着地する `/dashboard/decisions/{project}/{thread_id}`
を、リダイレクト stub から**判断 UI 本体**に差し替える。URL は増分 1 で敷いた
契約のまま、ハンドラの中身だけを差し替える。

## 0. スコープ / 非スコープ

**スコープ (この増分)**
- `GET /dashboard/decisions/{project}/{thread_id}` — D-26' の **4 分岐**を実装
  (駐機中 / 駐機中でない / 存在しない / 取得できなかった)。
- `POST /ui/projects/{project}/threads/{thread_id}/messages` — 既存ハンドラに
  **`_freeform` opt-in 分岐**を足す (合成 / D-30 / D-31)。**POST 先を新設しない。**
- `src/magickit/templates/decisions_thread.html` — 判断 UI / 「判断待ちではありません」/
  取得不能 / D-31 エラー再描画 を同一テンプレートの分岐で。

**非スコープ**
- `/dashboard/decisions` (一覧) — 増分 3。現状 302 → `/dashboard` のまま (msg-096 §4)。
- ops dashboard の 2 軸を変えない (msg-084 §4)。
- mindwire 側のリンク組み立てを変えない (msg-084 §4)。
- `ops.py` / `run-conductor-scheduled.ps1` に触れない。
- `post_message` の既存 Form param を 1 つも変えない (msg-097 §4.1 一次照合)。

## 1. `/dashboard/decisions/{project}/{thread_id}` — 4 分岐

| 状態 | 判定 | 応答 |
|---|---|---|
| 存在し、駐機中 | thread が取れ、最終 msg が駐機条件に合致 | **200** 判断 UI |
| 存在するが駐機中でない | thread が取れ、合致しない | **200** +「判断待ちではありません」+ chatroom 導線 |
| **存在しない** | Conclair が**明示的に**「無い」と答えた | **404** |
| **取得できなかった** | Conclair 不達 / 例外 / それ以外の error envelope | **503** + `/ui/...` 直リンク |

- **分岐は `_is_error(result) = "error_type" in result` で最初に切る。** 成功形の欠けた
  200 を「該当なし」と読まない (msg-096 §2 / CLAUDE.md の `_lookup_identity`
  契約検査と同型)。`ChatroomAdapter` は error envelope を dict のまま返す。
- **404 と 503 の判別 (Einstein msg-095 §3 / naysayer 補強)**: Conclair の「存在しない」
  応答も error envelope として届く ∴ `error_type` の**中身**を見て切り分ける。
  「NotFound」相当を含むもの (case-insensitive substring: `not_found`, `notfound`,
  `thread_not_found` 等) を **404**、それ以外の envelope・httpx 例外・タイムアウトは
  **503**。envelope を一律 503 にすると増分 2 で新設した 404 分岐が永遠に通らなくなる。
- **駐機の判定 (msg-096 §2)**:
  1. thread の最終 msg の**構造化 `next_participant` field が `human` を指す**なら駐機。
     PR #28 で入った field を第一根拠に置くことで、mindwire (`parked_humans.py`) と
     magickit が**同じ構造化データ**を見る形になる ∴ 正規表現の二重実装を主経路から外せる。
  2. field を持たない旧 msg に限り、本文の**単独行 `^\s*NEXT:\s*human\s*$`** (case
     insensitive) を fallback で読む。D-30 の判定と**同一実装を共有**し、2 箇所に書かない。
- **駐機判定は magickit 内で行う** (単一スレッド判定; mindwire を呼ばない)。他 repo への
  依存を作らない (msg-094 §2 / msg-096 §2)。

## 2. 判断 UI (`decisions_thread.html`) の要件

### 2.1 単一 `<form>` — 選択肢は `content` を送る submit ボタン

`content` は既存 `post_message` の**必須 Form field** (default 無し) ∴ **`_choice`
という別 field は作らない** (msg-097 §4.1 一次照合)。選択肢ボタン自身が `content` の
value を運ぶ:

```html
<form method="post" action="/ui/projects/{p}/threads/{t}/messages">
  <input type="hidden" name="type"   value="decide">
  <input type="hidden" name="author" value="human">
  <select name="next_participant"> …登録済 identity のみ… </select>
  <textarea name="_freeform"></textarea>        <!-- 常設。選択に依存して現れない -->

  <button type="submit" name="content" value="A: …">A: …</button>
  <button type="submit" name="content" value="B: …">B: …</button>
  <button type="submit" name="content" value="">自由記述だけで送る</button>   <!-- I-12 -->
</form>
```

- 選択肢ボタン → `content="A: …"` + `_freeform="…"`
- **I-12** → `content=""` + `_freeform="…"` (空文字は `str` として妥当 ∴ 422 にならない)
- 追加する Form param は `_freeform` **ただ 1 つ**。既存 param は 1 つも変えない。

**I-12 の担保が「必須にしない」では足りない**: `<form>` は押されたボタンの
name/value しか送らない ∴ 選択肢を押さずに送る submit が UI 上に無ければ、自由記述
だけの送信は物理的に不可能。3 つ目のボタンがその実体であり、これを消す実装は差し戻す
(msg-084 §2 / msg-094 §3)。

### 2.2 モバイル

- 縦 1 カラム。
- 選択肢は**カード** (表を使わない)。
- **幅 390px で横スクロールを起こさず、送信まで到達できること**。
- 既存 `static/` の資産のみ (**CDN 禁止**は CLAUDE.md の規則で増分 1 と同じ)。
- 判断ページの form は**プレーン HTML** (HTMX ではない) ∴ JS 無しでも送信できる。
  D-31 のエラー再描画も full-page response なので JS 不要。

### 2.3 `next_participant` の select

- 値域は登録済 identity のみ (msg-084 §2)。
- **`none` と `pr-review <ref>` を出さない** (magickit が `NextParticipantUnknownError`
  で弾く)。終端は `closes_thread` で表現する。
- **既定値は駐機 msg の著者**。D-30 が追記する名前と**同じ値**を使う (2 箇所で別々に決めない)。
  ユーザが select を変えれば D-30 の値も追従する。
- 登録済 identity 一覧の実装形: thread の messages に現れる distinct な `author` の集合 +
  `human`。thread に参加している actor は概ね登録済で、万一未登録なら D-31 で弾かれる。
  Prismind 落ちても select は描ける (Prismind をクリティカルパスに置かない)。

## 3. POST — `_freeform` opt-in 分岐 (`chatroom_writes.post_message`)

### 3.1 追加 param は 1 つだけ (Einstein §2 の必須ガード)

```python
decision_freeform: Annotated[str | None, Form(alias="_freeform")] = None,
```

**`Optional[str] = None` にすること** — default 無しの `Annotated[str, Form()]` にすると
FastAPI の Pydantic バリデーションで**既存 `/ui` フォームが 422 になる** (実行が
early-return コードに届く前に弾かれる)。G-1 の「既存挙動を変えない」が壊れる。
`decision_freeform is None` = 判断ページ由来ではない = 新コードを 1 行も通さない。

**ワイヤ側の field 名は `_freeform`** (msg-097 §4 の凍結形どおり)。Pydantic v2 は
先頭アンダースコアを field 名として拒否する ∴ Python パラメータ名は
`decision_freeform` にし、`Form(alias="_freeform")` でワイヤ名だけ復元する。仕様
文言を保つ + Pydantic の禁則を回避 の両立。

### 3.1a ★ 実装時の empirical finding: I-12 の sentinel

**msg-097 §4.1 の 1 点が現行 FastAPI で empirical に成立しなかった**:

> I-12 → `<button name="content" value="">` で `content=""` が送られる（空文字は
> `str` として妥当 ∴ 422 にならない）

実測 (FastAPI 0.128 / pydantic 2.12 / starlette 0.50): `content=` (empty value) を
required Form field に送ると `{"type":"missing","input":null}` の 422 に落ちる
(`x=` を single string field に送っても再現)。∴ `<button name="content" value="">`
のままだと I-12 経路が使えない。

**選択した対処 (既存 param signature を変えない sentinel 方式)**:

- 「自由記述だけで送る」ボタンの value を **sentinel `(自由記述のみ)`** にする。
- handler は合成の直前に `content == "(自由記述のみ)"` を空文字に正規化する。
- ボタンの value 属性は user が typo できず、button 由来値以外に content が入る
  経路は無い ∴ 衝突しない。

**代替案として却下したもの**:

- `content: Annotated[str, Form()] = ""` に緩める → 既存 param signature を変え、
  msg-097 §4.1 / msg-098 §7 の「触らない」を破る。
- JS で content を組み立ててから送る → msg-096 で (a) 案として撤回済み。

**Tier-C への報告事項**: この sentinel は msg-097 §4.1 の empirical assumption
と食い違う対処である。次に触る人が spec 文言と実装を照合したときの摩擦を減らす
ために明記する。設計判断が (a) 「sentinel で良い」/ (b) 「代わりに content を
optional にする」/ (c) 「別の道」のどれかで、次のスレッドで訂正されうる。

### 3.2 G-1 opt-in トリガは `_freeform` の有無のみ (msg-097 §4.3)

- `_freeform is None` → 新コードを 1 行も通さない (バイト単位で既存挙動)。
- 既存 `/ui` の compose form は `_freeform` を送らない ∴ 触れない。
- **G-1 の回帰テスト**: `_freeform` 無しの POST が既存と同一の下流呼び出しになること。

### 3.3 合成規則 (msg-098 §2)

```
{content}

{_freeform}
```

- `content` が空 → `_freeform` のみ。
- `_freeform` が空 → `content` のみ (= 現行と同一の本文)。
- **どちらも空 → 現行どおり `content=""` のまま下流に渡す** (新たに拒否を作らない)。
- **人の文章は 1 文字も変えない (I-6)**。trim もしない。連結のみ。

### 3.4 ★ 合成の位置 — gate 群より前 (msg-097 §4.2 の罠)

`_enforce_close_policies` は生の `content` を読む:

```python
if closes:
    decision = await chatroom_tools._enforce_close_policies(
        adapter, ..., body_content=content, ...   # ← 生の content
    )
    body_content = decision["content"]
```

∴ **合成結果を `body_content` にだけ代入する実装は、`closes` が真のとき自由記述を
黙って捨てる**。判断ページで人が下す決定は decide でしばしば close を伴う ∴ **最も
重要な 1 通で、最も長く打った文章が落ちる** — D-31 が守ろうとしたものに真正面から反する。

**実装形**: `_freeform` を検出したら early-return 判定の直後に `content` 自体を
合成後の値へ差し替え、以降のコードは合成を意識しない。「後から `body_content` を直す」
形にしない — **穴を塞ぐのではなく、穴が開く場所を無くす**。

### 3.5 G-2 D-30 の `NEXT:` 追記 — 判断ページ由来にだけ

- トリガは §3.2 と同じ (`_freeform` の有無)。共有経路に無条件で置かない。
- 判定対象は**合成後の本文**。行に分割し、`^\s*NEXT:` で始まる**単独行**が 1 つでも
  あれば → **一切追記しない** (parser は last-wins ∴ 無条件追記は人の指定を黙って上書きする)。
- 無ければ → 末尾に空行 + `NEXT: <next_participant>` を 1 行。
- **人の文章そのものは 1 文字も変えない**。追記は末尾への連結のみ、trim もしない。
- `next_participant` が空なら追記しない (追記する名前が無い)。

### 3.6 D-31 — 検証失敗で入力を失わせない

- `_check_next_participant` は 2 種のエラーを返す:
  - `NextParticipantUnknownError` (未登録の確定回答)
  - `NextParticipantValidationUnavailableError` (Prismind 不達 = 確認不能)
- **エラー文面を画面に出し、入力内容を保持したまま再描画する** (`_freeform` / 選択 /
  select の値をすべて再描画に残す)。
- **文面は上の 2 種を別扱い**にする — 「その名前が登録されていない」と「今 identity を
  確認できない」を混ぜない (msg-093 §2 の一般則: 我々が確認できなかったことを、相手が
  不正だったことにしない)。もし API がこの 2 つを区別できない形でしか返さないなら、
  区別できるふりをしない — 実装は API の返り値どおりに文面を切り分ける。
- **モバイルで長文を打った直後に消えるのが最悪の失敗** ∴ 実装の手を抜かない。

### 3.7 成功時の応答 — 303 → `/ui/projects/{p}/threads/{t}`

判断ページからの POST は HTMX ではない ∴ 成功時は HTTP 303 See Other でスレッド
ページへ。ブラウザは自然に GET で着地する。エラー時は 200 で判断ページ本体を返す
(入力保持 + エラー banner)。

## 4. 二重管理の負債 (Einstein msg-095 Q2 応答 / Principle 2 警告受諾)

**SOT は mindwire 側の `parked_humans.py`。** magickit 側で駐機を再計算するのは
**別 repo API 呼び出しを増やさない**ためだが、これは二重実装 ∴ 将来のドリフトの温床。
面積を減らすため:

- magickit が答える述語を「駐機か」ではなく**「まだ誰も答えていないか」** という、
  より弱く、より安定した形にする (msg-096 §2)。
- **構造化 `next_participant` field を第一根拠**にする ∴ mindwire と magickit が
  同じ構造化データを見る (乖離余地は fallback 経路にだけ残る)。
- 症状は「一覧に出るのにページが『判断待ちではありません』と言う」= **増分 3 の
  受入に「一覧の在否とページの判定が一致すること」を 1 項目立てる**。

## 5. 一次照合の結果 (Tier-C msg-097 §3 / §4)

**次に触る人が同じ罠を踏まないために書く**:

1. **`chatroom_writes` はブラウザ由来の write のみを通す**。MCP 経由 (AI role の投稿) は
   `chatroom.py` の別実体を通り、`chatroom_writes` を一度も踏まない。依存は一方向
   (`chatroom_writes` → `chatroom_tools`、逆はない)。∴ 「AI が投げる全メッセージの本文を
   magickit が書き換え始める」影響は**この経路では構造的に起こり得ない**。
   ただし repo 内に import が無いことは、この HTTP route を**外部から直接叩く者が居ない**
   ことを意味しない ∴ G-1 の early-return は「今は他に呼び手が居ないから安全」ではなく、
   **呼び手が誰であっても既存挙動を変えないための構造**として置く (msg-098 §1)。
2. **`content` は必須 Form field** (default 無し) ∴ 送らない POST は FastAPI が 422 を
   返し、ハンドラのコードは 1 行も走らない。∴ `_choice` は作れず、作る必要も無い。
   選択肢ボタン自身が `name="content"` を運ぶ形で一本化する。
3. **`_enforce_close_policies` は生の `content` を読む** ∴ 合成は gate 群より前で
   `content` 自体を差し替える (§3.4)。

## 6. 一般則 — 教訓 2 本 (spec に固定する)

1. **リンクは外形で叩いて確かめる** (msg-084 §5)。
   外部から見える成果物は、外部から実際に叩いて確かめる。CI はページの存在を知らない。
2. **観測したのは結果であって原因ではない。人の行動は観測範囲の外にある** (msg-093 §2)。
   結果から原因を推論するときは、その原因を実際に観測したかを問う。観測していないなら
   「原因は未特定」と書く。「人が何もしなかった」は決して観測にならない。

**§1 の 503 分岐**と**§3.6 のエラー文面**が、上記 2 の一般則の**実体**である
(取得できなかったことを「存在しない」に丸めない / 確認できなかったことを「不許可だった」に
丸めない)。抽象論として書くだけでなく、コードのこの 2 箇所が実装として体現する。

## 7. 運用事実 (msg-093 §3)

**`spirrow-magickit` に自動 deploy 経路は無い。手動 deploy。merge ≠ live。**

∴ 本 repo の PR 運用:
- **merge 依頼と deploy 依頼は 1 セットで出す**。「merge してください」だけでは反映されない。
- merge 後に外形で確認し、404 のままでも「反映待ち」と解釈しない — deploy がまだ
  行われていないだけである可能性が高い。
- 増分 2 は Python の新規ハンドラ + テンプレート ∴ **テンプレート部分は作業ツリー
  直サーブで即時、Python は手動 deploy まで出ない**。混在する ∴ 完了判定は外形実測に置く。

## 8. 完了条件 (D-F、msg-098 §9 のまま)

**merge では閉じない。**

1. PR を main に merge (人手、保護ブランチ)。
2. **merge 依頼と deploy 依頼を 1 セットで出す**。
3. 外形で **4 分岐**を叩き、**出力そのもの**を貼る:
   - 駐機中スレッド → 200 + 判断 UI (title / form の存在確認)
   - 駐機中でないスレッド → 200 + 「判断待ちではありません」
   - **存在しないスレッド → 404**
   - Conclair 不達 / 障害時 → **503**

   **404 と 503 は増分 2 で新設する分岐 ∴ 必ず測る**。新しく作った分岐を測らずに項目
   だけ消化するのが A-13 の事故そのものだった (msg-084 §0)。
4. **390px の実タップ**で送信まで到達することを Takahito に確認いただく。

## 9. テスト (unit の範囲を明示する)

- **4 分岐**それぞれ (駐機中 / 駐機中でない / **404** / **503**)。error envelope を注入
  して 503 になり、**404 に丸めないこと**。逆に NotFound 相当は 404 にすること。
- **G-1 回帰**: `_freeform` 無しの POST が既存と同一の下流呼び出しになること。
  さらに `_freeform` を送らない既存 form は FastAPI が 422 で弾かないこと (Einstein §2)。
- **§3.4**: `closes_thread` 付き `decide` で `_freeform` が最終本文に残ること。
  合成が gate 群より前で走った証拠として、`_enforce_close_policies` に渡る
  `body_content` が既に合成後であること。
- **G-2 / D-30**: 単独行 `NEXT:` があれば追記しない / 無ければ空行 + 1 行だけ /
  **人の文章が 1 文字も変わらない**こと (前方一致で厳密比較)。
- **I-12**: `content=""` + `_freeform` のみで本文が空にならないこと。
- **D-31**: 検証失敗時に自由記述・選択・select の値が再描画に残ること。

**これらは「ページが外から見える」ことを一切保証しない** (増分 1 で実証済み) ∴
完了判定は §8 の外形実測に置く。
