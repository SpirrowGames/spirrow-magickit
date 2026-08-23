# S5 判断 (decision) page ― 増分 2 spec

判断依頼通知 (Discord alert) から人が着地する `/dashboard/decisions/{project}/{thread_id}`
を、リダイレクト stub から**判断 UI 本体**に差し替える。URL は増分 1 で敷いた
契約のまま、ハンドラの中身だけを差し替える。

**★ 実機受入 (msg-139 / msg-140 / msg-146 / msg-147 / msg-151) 反映**:
Takahito が実機で 2 件の欠陥を報告 (D-35 / D-36〜D-38 / I-19〜I-21) — form の
選択と送信が分離できない件、宛先 select に "選ぶと必ず失敗する値" が出る件。
両件を 1 PR で修正 (1 増分の中の 2 項目, 実タップを 1 回で通す)。詳細は §13
「D-35 (radio+submit の分離) + D-36〜D-38 (registered-target filter)」以下。

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

### 3.2 G-1 opt-in トリガは **`_decision_form == "1"`** (msg-103 §2 — msg-097 §4.3 撤回)

**msg-097 §4.3 で書いた「トリガは `_freeform` の有無のみ」は実装不能だった** —
msg-102 の live 実タップで欠陥として表面化した (F-1: 空 `_freeform` と不在
`_freeform` はハンドラから区別できない、両方 `None` になる)。∴ トリガをデータ
(`_freeform`) から分離し、判断ページのテンプレートが**常に非空を送る専用
hidden field** に移す。

```html
<input type="hidden" name="_decision_form" value="1">
```

```python
decision_form: Annotated[str | None, Form(alias="_decision_form")] = None
```

- **判定は `decision_form == "1"`** (`is not None` ではなく値の一致)。空
  hidden (`_decision_form=`) は Form parse で `None` に潰れる (spec §11.4 で
  実測済) ∴ 値一致は「空 hidden が届いた」も「不在」も同じ「新コードを通さない」
  1 通の判定に落ちる (msg-103 §4-2 の防御)。
- `decision_form != "1"` → **新コードを 1 行も通さない** (バイト単位で既存挙動)。
- 既存 `/ui` の compose form は `_decision_form` を送らない ∴ 触れない。
- **default が `None`** (Einstein msg-099 §2 のガード): default 無しの必須
  param にすると `_decision_form` を送らない既存 form が 422 で弾かれ、
  early-return コードに届く前に FastAPI が拒否する ∴ G-1 が壊れる。同じ理由
  で `_freeform` も `Optional[str] = None` を維持する。
- **G-1 の回帰テスト**:
  - `_decision_form` 無しの POST が既存と同一の下流呼び出しになること。
  - **`_freeform` を送っていても `_decision_form` が無ければ opt-in を通らない**こと
    (トリガが完全に移ったことの assert / msg-103 §7)。
  - `_decision_form=""` (空 hidden) も opt-in を通らないこと (§4-2 の判定)。

**★ msg-102 回帰テスト (必須)**: `_decision_form=1` + `content="(自由記述のみ)"`
+ `_freeform=""` の入力で、本文が `NEXT: <著者>` になり sentinel が残らない
こと。**これは Takahito が実タップした入力そのもの** (msg-102 §1) ∴ 「人が
最初に使った 1 回」を pin する。

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

## 6. 一般則 — 教訓 4 本 (spec に固定する)

1. **リンクは外形で叩いて確かめる** (msg-084 §5)。
   外部から見える成果物は、外部から実際に叩いて確かめる。CI はページの存在を知らない。
2. **観測したのは結果であって原因ではない。人の行動は観測範囲の外にある** (msg-093 §2)。
   結果から原因を推論するときは、その原因を実際に観測したかを問う。観測していないなら
   「原因は未特定」と書く。「人が何もしなかった」は決して観測にならない。
3. ★ **前提が 1 か所で反証されたら、その前提に依存している他の箇所を数える**
   (human msg-102 §3 / Bohr msg-103 §0)。反証は、それが見つかった場所だけの
   事実ではない。**実体**: 本 spec §11 の前提表 — 依存箇所を数えられる形で
   置いておかないと、次も同じ場所で落ちる。抽象論として書くだけでなく、次に
   触る人が実際に埋めるべき表として残す。
   - **元事例**: msg-100 §3.1a が `content=""` → 422 を実測で反証したとき、
     反証は `content` 側にしか適用されず、`_freeform` 側の同じ前提が残った。
     msg-102 で live に人が最初に I-12 を使った 1 通で、その残った前提が
     欠陥として表面化した。片側だけ直された。
4. ★ **外形で叩くのは「私が想定した入力」であって、人が実際に取る経路ではない**
   (human msg-102 §4)。上の §6.1 の 1 段深化。**実体**: msg-098 §8 のテスト
   仕様は `_freeform` 非空しか送っておらず、空 `_freeform` (人が textarea を
   触らないケース) を一度も測っていなかった ∴ 703 tests + CI 緑 + 私の外形
   curl 3 分岐緑を素通りして live で発現した。**空入力を送るテストを全種類に
   足す** (§12 の追加テスト群)。

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

## 10. ★ msg-102 の欠陥記録 (トリガの再設計)

**症状** (msg-102 §1): Takahito が判断ページで「自由記述だけで送る」ボタンを
自由記述欄が空のまま押した (msg-101 §7 の 3 番目の実タップ)。live で生まれた
msg の本文は `(自由記述のみ)` — I-12 sentinel が正規化されずに素通りした。
`next_participant` は構造フィールドとして正しく `Bohr` に入ったが、本文への
`NEXT: Bohr` 追記も行われなかった。

**根本原因** (human msg-102 §3 / Bohr msg-103 §2): 旧トリガ
`decision_freeform is not None` が「空 `_freeform`」と「不在 `_freeform`」を
区別できなかった (F-1)。starlette の form parse を経由すると、空値の Form
field は `Optional[str] = None` として `None` になる — 空文字は「不在」と
等価に潰れる。人が textarea を触らないケースで opt-in が起動せず、sentinel
正規化と D-30 追記が両方行われなかった。

**修復** (msg-103 §2): トリガをデータ (`_freeform`) から分離し、判断ページの
テンプレートが常に非空を送る専用 hidden field `_decision_form=1` に移した
(§3.2)。判定は `== "1"` の値一致 (§3.2 / msg-103 §4-2 の防御)。

**次に触る人へ**: `_decision_form` hidden を「1 本減らせる冗長なフィールド」
と読んで削らないこと。**これはトリガ本体である**。データフィールド
(`_freeform`) をトリガに再利用すると、この記録の症状がそのまま再発する。
`_freeform` は今もハンドラのパラメータに残っており、そこから「有無」で opt-in
を判定したくなる誘惑がある — 誘惑に負けたら msg-102 と同じ 1 通が live で
出る。

## 11. ★ この設計が依存している前提の一覧 (§6.3 の実体)

**「反証が効く箇所を数える」を、次に触る人ができる形で置く** (msg-103 §5)。
1 項目でも「破れたときの症状」を確認せず変更したら、msg-102 と同じ形で
落ちる。

| # | 前提 | 状態 | 破れたときの症状 |
|---|---|---|---|
| P-1 | 空値の Form field は欠落と等価に潰れる (`None`) | **実測済** (human msg-102 §2 / msg-100 §3.1a / 本 PR §11.4) | — |
| P-2 | 非空 hidden (`value="1"`) は `None` にならずに届く | **実測済 (本 PR §11.4)** | opt-in が一度も起動しない = **msg-102 欠陥の再発** |
| P-3 | 既存 `/ui` compose form は `_decision_form` を送らない | **実装時にテンプレートを読んで確認 (本 PR で確認済)** | 既存 write が新コードを通る (G-1 破壊) |
| P-4 | 必須 `content` param に空値を送ると 422 | **実測済** (msg-100 §3.1a / human msg-102 §2 の 4 行目 / 本 PR §11.4) | sentinel が不要になる (害はない) |
| P-5 | `_enforce_close_policies` は生の `content` を読む | **実測済** (msg-097 §4.2 の逐語) | close 時に自由記述が落ちる (最重要 1 通が壊れる) |
| P-6 | `chatroom_writes` を通るのはブラウザ由来の write のみ | **repo の import 関係のみ実測** (msg-097 §3)。**HTTP 直叩きの有無は未確認** (Tier-C 自身が明示) | 想定外の呼び手が新分岐に入り得る — ただし P-3 が真なら `_decision_form` を送らない限り入らない |
| P-7 | 下流 (Conclair) は空 `content` を crash せず処理する | **未測 (live Conclair 依存)** — spec §3.3 は「空のまま下流に渡す」を許すが、Conclair 側の耐性は本 PR の gate では観測できない (Einstein msg-104 §3) | 空 content 送信で 500 が返り、D-31 の再描画網をすり抜ける可能性 (稀ケース: I-12 かつ空 freeform かつ空 next_participant) |

### 11.4 §4-1 probe の結果 (本 PR で実測)

`fastapi 0.128.0 / pydantic 2.12.5 / starlette 0.50.0` (deploy 済みバージョン
と同一) で、`decision_form` / `_freeform` / `content` の Form parse 挙動を測った
(script: `.git/mindwire-scratch/probe_decision_form.py`, 破棄可能)。

```
--- 1. _decision_form present, value='1' (judgement page normal send)
    body={'decision_form': "'1'", 'decision_form_eq_1': True}       → P-2 confirmed

--- 2. _decision_form absent (existing /ui compose form)
    body={'decision_form': 'None', 'decision_form_eq_1': False}     → P-3 supported

--- 3. _decision_form present, value='' (probe of Bohr §3 hypothesis)
    body={'decision_form': 'None', 'decision_form_eq_1': False}     → 空 hidden は
                                                                       None に潰れる
                                                                       (== "1" 判定
                                                                       が防御として
                                                                       効くことの証拠)

--- 4. empty content, required field (msg-100 §3.1a re-check)
    status=422  detail=[{'type': 'missing', 'input': null, ...}]    → P-4 confirmed

--- 5. _freeform present, value='' vs absent (F-1 confirmation)
    body={'decision_freeform': 'None'}                              → F-1 が今も生き
                                                                       ている (∴ トリガ
                                                                       を移したのは
                                                                       正しい)
```

### 11.7 P-7 (Einstein msg-104 §3) について

`_compose_decision_body("", "")` は `""` を返す (§3.3)。かつ
`_maybe_append_next("", "")` (next_participant も空) は `""` を返す。∴
理論上、判断ページから `content=(自由記述のみ)` + 空 `_freeform` + 空
`next_participant` が来ると下流に空 `content` が届く可能性がある。

**本 PR での対処**:
- unit test `test_all_empty_composed_body_is_empty_and_no_next_appended` で
  合成結果が `""` になり、AsyncMock adapter がそれを受けることを pin。
- **live Conclair が空 `content` を crash なく処理するかは、本 gate では
  観測できない** (Einstein msg-104 §3)。
- 完了条件 §8 の外形実測に「I-12 で `_freeform` を空・`next_participant` を
  空にした送信も 1 回試すこと」を追加。live で 500 が返れば D-31 の再描画は
  効かず、その時点で追加の防御 (合成結果が `""` になったら手前で reject) が
  必要。**先回りしてその防御を実装しない** — 前提が実測前に実装を歪めるのは
  msg-097 §4.1 と同じ癖である (Bohr msg-103 §0 の教訓)。

## 12. テスト — 空入力を送るケースを全種類に足す (msg-103 §7 / §6.4)

msg-102 §4 が名指しした欠陥: `tests/unit/test_decisions_form.py` の全 I-12
系ケースが `_freeform` **非空**しか送っておらず、CI 緑を素通りして live で
「人が最初に使った 1 回」で落ちた。

**本 PR で追加した回帰テスト** (詳細はコード参照):

- `test_msg102_regression_empty_freeform_with_sentinel_fires_opt_in` —
  Takahito 実タップと同じ入力を pin。**このテストが失敗するなら msg-102 の
  欠陥が再発している**。
- `test_choice_button_with_empty_freeform_fires_opt_in_and_appends_next` —
  選択肢ボタン + 空 `_freeform` で NEXT: 追記が入ること。
- `test_all_empty_composed_body_is_empty_and_no_next_appended` —
  §11.7 の corner (下流が空 content を見るケース) を pin。
- `test_g1_freeform_present_without_decision_form_stays_on_legacy_path` —
  ★ トリガが完全に移ったことの assert。`_freeform` を送っても
  `_decision_form` が無ければ opt-in を通らない。
- `test_decision_form_empty_string_does_not_fire_opt_in` —
  §4-2 の値一致判定 (`== "1"`) が空 hidden も除外することの pin。
- `test_judgement_ui_carries_decision_form_hidden_trigger`
  (`test_decisions_routes.py`) — テンプレートが `_decision_form=1` hidden を
  常に出すことの pin (これが消えると新コードが 1 行も動かない)。

**既存の I-12 / 合成 / D-31 / close+freeform テストはすべて `_decision_form=1`
を付けるよう更新した** (これらは判断ページ由来の入力を模しており、テンプレート
が常に送るこの field を欠かせない)。

## 13. ★ Takahito 実機受入で見つけた 2 欠陥 (msg-139 / msg-140) と修正

### 13.0 発端 (msg-139 / msg-140 逐語要旨)

** 欠陥 A (msg-139)**: 選択肢を押した瞬間に POST される ∴ 「選んで補足を
書いてから送る」ができない。携帯で誤タップが即確定投稿になる。

**欠陥 B (msg-140)**: 宛先 select に `pr-gate-relay` が並び、選ぶと必ず
`NextParticipantUnknownError` になる。UI が「選べる = 選べる」と読ませて、
選んだ人の側でエラーにする。

**観測と媒体 (msg-146 §0 教訓 (6) の適用)**:

| 欠陥 | 観測 | 媒体 | 確度 |
|---|---|---|---|
| A: 選択と送信が分離できない | Takahito 実機タップ (msg-139 §2) | 実機ブラウザ | 確定 |
| B: 宛先 select が失敗値を出す | 実機タップ + Tier-C ソース読解 (msg-140 §3) | 実機 + ソース | 失敗は確定/原因は A-23(ii) で確認 |

### 13.1 A-23(ii) — 実装前の premise 測定 (Einstein msg-147 §2)

**着手前に測った**: scratch project に `next_participant=pr-gate-relay` の
`decide` を POST し、raw error を記録。

```
=== A-23(ii): next_participant=pr-gate-relay ===
<div class="alert alert-error"><strong>NextParticipantUnknownError</strong>:
next_participant 'pr-gate-relay' is not a registered identity. …
```

**結論**: 拒否理由は「未登録」で確定。∴ 「registered で絞る」設計は的を外さない。
（もし `RoleNotAllowed` 等の別理由だったら D-36〜D-38 の設計は再考が必要だった。
測定を先に置いたのは msg-133 §1.1 の教訓の直接適用: 前提を確立してから設計する。）

**A-23(i)**: `next_participant=human` は受理を確認 (`human` を常に含める
根拠に使える定数であることの実測)。

### 13.2 D-35: radio + 単一 submit — 選択と送信の分離

**旧**: 選択肢は `<button type="submit" name="content" value="A: ...">` の
連打。押した瞬間に POST。

**新**: 選択肢は `<label>` の中に `<input type="radio" name="content" value="A: ...">`。
submit は `<button type="submit">送信</button>` 1 つに集約 (`name` 属性なし ∴
余分な field を送らない)。

**罠を、罠に触れずに通過する** (msg-146 §2 の要):
- `content` は共有 handler の必須 Form field ∴ 「未選択でブラウザが送らない」
  経路に落ちると FastAPI が 422 (spec §3.1a の実測)。
- **既定 checked の "自由記述だけで送る" radio が常に 1 つある** ∴ form は
  常に非空の `content=` を送る。既存 signature は変えない ∴ G-1 も無傷。

**画面順** (縦 1 カラム、上から): 問い → 選択肢カード群 → recommendation
/ unknowns (J-fresh のみ) → 自由記述 → 宛先 → **送信ボタン**。

### 13.3 D-31 exhaustive fallback (Einstein msg-147 §3)

D-35 で導入した radio group の落とし穴: 再描画時、以前の `content` が
現行 option 集合に無いと `checked` が付く radio が 0 個になり、次の submit で
FastAPI 422 に落ちる (msg-146 §5(a) が名指しした罠)。

**修正**: handler 側 `_pick_checked_choice(content_value, choice_options)`:
- content_value が現行 option の value と一致 → その value
- 一致しない (option が消えた/空文字/garbage) → sentinel `(自由記述のみ)`

**invariant**: **template が返す radio group には必ず 1 つ ``checked`` がある。**

### 13.4 I-19 / D-36 / D-37 / D-38: 宛先を receiver 権威で絞る

**I-19 (msg-146 §3 の中心規律)**: **select に並ぶ値 ⊆ POST 時に受理される値**。
判定は POST-time gate と同じ関数を通す (`chatroom_tools._lookup_identity`)。
描画側に別 lookup を書かない (drift を防ぐ; msg-044 §6.4 の contract check
を書き直す羽目にならない)。

**D-36**: 現行の `author == "none" or author.startswith("pr-review")` 拒否リストを
**削除**。候補生成 (thread の distinct author + `human`) は維持。それぞれを
`_lookup_identity` に通して verdict が "registered" のものだけを select に出す。
`pr-gate-relay` を Prismind に登録して直さない — reserved は reserved のまま
(judge を渡す先ではない)。

**D-37 (default demotion)**: select の既定値は駐機 msg の著者。ただし駐機著者
が registered filter を通らなかった場合、既定は "宛先を送らない" (empty)。
`human` に自動 fallback しない (「人の判断が人に返る = ループが静かに止まる」を
静かに起こさない)。

**D-38 (3 値 fail-closed)**:
- `REGISTERED` → 出す
- `UNREGISTERED` → 出さない
- `UNKNOWN` (Prismind 不達 / timeout / contract violation / render 予算超過)
  → 出さない (**fail-closed**; permissive fallback は「outage 中に select が
  全員通す」形になる = 元の欠陥の再演)

**実装**:
- 1 lookup あたりの timeout: `_LOOKUP_TIMEOUT_S = 1.0s` (adapter default
  240--360s は継がない; CLAUDE.md 記載の Lexora `/health` が 20-40s
  ブロックする実例に照らして継承は危険)。
- render 全体の予算: `_RENDER_BUDGET_S = 4.0s`。予算超過分は Prismind を呼ばず
  UNKNOWN 扱い。順序は sequential (総 wall-clock を予算にキャップする; gather
  だと 4 × N 秒に膨らむ)。

**degradation 表示**: `verification_unavailable=True` のとき template に 1 行
「宛先候補を検証できませんでした。表示している候補は限定的です; 本文の
`NEXT:` 行で指定するか, 後で再読込してください.」を出す。

### 13.5 I-20: "宛先を送らない" を選んだときの handler 側規律

**要件** (msg-146 §3): "宛先を送らない" 選択 (wire は `next_participant=""`) は、
本文に単独行 `NEXT: <名前>` が無ければ **送信を拒否**、D-31 で入力を保持
したまま再描画。理由の明示 (「宛先が指定されていません…」)。

**なぜ寛容側にしないか**: 代替は「宛先が 1 つも無い投稿」= 「誰も呼ばれない
まま止まる」— 本スレッドが 4 回踏んだ静かな失敗の族そのもの。ここだけ検証側を
選ぶ (I-12 の「自由記述だけで送れる」は content 側の要件で、これは宛先側の
規律 ∴ 矛盾しない — Einstein msg-147 §1 が確定)。

### 13.6 I-21: `<label>` の内側に `<a>` を置かない (template lint)

**要件** (msg-146 §3): 選択肢カードは全体が `<label>` ∴ 内側に `<a>` を置くと
タップがラベル選択に吸われる、あるいは「リンクを踏んだつもりで選択が変わる」。
`spirrow-mindwire` 側で「URL を材料に載せる」作業が進行中であり、URL 到着後に
気づけば実タップを 1 回焼く ∴ **先に pin する**。

**実装**: `tests/unit/test_decisions_form_radio_and_target.py::test_i21_no_
anchor_tag_inside_decision_choice_label` が template を読んで
`<label class="...decision-choice...">` ブロック内に `<a` を含まないことを assert。

### 13.7 I-18: 既に払った実タップの pin — 書き換えの前に固定

**規律** (msg-146 §4): msg-121 の実タップで確認済の 3 挙動を、**書き換える前に**
characterization test で固定する:

1. 空欄送信は 422 で拒否 (msg-121 fix)
2. 自由記述だけの wire bytes 安定 (msg-121 tap)
3. G-1 バイト同一 POST (`_decision_form` なしは既存挙動)

**実装**: `tests/unit/test_decisions_form_choice_characterization.py`。順序を
逆にする (書いてから test) と、書き換え後の挙動を "正解" として固定してしまう。

### 13.8 前提表 (Bohr msg-146 §6 継承 + 追記)

| 前提 | 状態 | 破れたときの症状 |
|---|---|---|
| P-13 | `content=` 空文字は現行 FastAPI で 422 | 実測 (msg-102 / spec §11.4) — 本 PR は既定 checked radio で常に非空を送る形で回避 |
| P-14 | 選択 + 自由記述の合成は本番未走行 | msg-121 以前は radio 未実装 ∴ 併用不可能 — A-27 の実タップで初測 |
| P-15 | 登録済 identity の全列挙 API が無い | CLAUDE.md 実測 — 候補生成 (thread author + human) の維持根拠 |
| P-16 (新規) | `pr-gate-relay` の拒否理由は "未登録" | **A-23(ii) 実測済 (2026-08-24)** — `NextParticipantUnknownError` |
| Q-6 (未解決) | D-30 が未登録名を本文行として吐き得る | 本 PR で扱わず記録のみ (Tier-C msg-151 判断) |

### 13.9 スコープ外 (混ぜない)

- A-19 / A-21 / A-22 (`T-decision-material-push` 側の受入、ループ側で実行)。
- 問いの平易化 / URL を材料に載せる件 (`spirrow-mindwire` / `T-decision-request-composer`)。
- 通知ヘッダの文面 (`spirrow-mindwire` / `T-park-alert-says-judgement-when-it-is-a-fault`)。
- `_get_material_store()` の毎リクエスト `mkdir` (msg-131 非ブロッキング指摘)。
- D-30 の未登録名追記 (Q-6 - 別増分)。
