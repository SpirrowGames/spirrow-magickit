# S5'' 判断ページに材料を運ぶ ― spec

判断ページに「何を聞かれているか」を出す。増分 2 (`S5-decision-page.md`) が
建てた `GET /dashboard/decisions/{project}/{thread_id}` の判断 UI 分岐に対し、
mindwire 側の composer が生成する材料 (問い / 選択肢 / 得るもの / 失うもの /
推奨 / 未確定事項) を **push** で運び、magickit 側に **UPSERT** で保存し、
ページ側で **3 状態** (J-fresh / J-stale / J-absent) に切り分けて描画する。

**この spec は本スレッド T-decision-page の凍結形 (Bohr msg-117 §5 / msg-118
Tier-C 承認 / msg-121 畳み込み 2 行 / msg-122 §5) を SOT として書き下ろした
ものである。** 議論のログ (msg-084 … msg-123) は決定の場、この file は仕様
の場 (T-decision-request-composer §20.2)。以降の実装は本 file の要件で判定する。

## 0. スコープ / 非スコープ

**スコープ (この増分)**
- Magickit 側の材料 storage: SQLite テーブル 1 本 (`decision_materials`)、
  `UNIQUE(project, thread_id)`、書き込みは `INSERT OR REPLACE`。
- 材料の受け口: `PUT /v1/decisions/{project}/{thread_id}/material` (mindwire
  の conductor が push する)。
- 材料の外形確認用 GET: `GET /v1/decisions/{project}/{thread_id}/material`
  (認可確認・自己診断・integration test 用)。
- `GET /dashboard/decisions/{project}/{thread_id}` の判断 UI 分岐 (mode=judgement)
  を **3 状態**に切り分けて描画:
  - **J-fresh**: 材料あり ∧ `material.head_msg_id == thread.last_msg_id`
  - **J-stale**: 材料あり ∧ 上記の完全一致でない (`thread.last_msg_id` が
    読めない場合を含む)
  - **J-absent**: 材料が保存されていない
- `decisions_thread.html` の judgement 分岐を 3 状態向けに書き直す (**J-stale
  では材料をサーバ側で描画しない** — I-14)。
- `\|safe` 禁止の lint テスト (F-B により greenfield ∴ 追加コストは lint 1 本)。

**非スコープ**
- mindwire 側の push 実装 / composer 生成ロジック / conductor スケジューラ。
- `/dashboard/decisions` の一覧 (増分 3)。
- ops dashboard の 2 軸 (msg-084 §4)。
- mindwire 側のリンク組み立て (msg-084 §4)。
- 認可: PUT 面の認証は現時点未実装。land 順序 3 で実測して測ってから設計する
  (§P-10)。**測る前に塞がない** (msg-122 §4)。
- markdown レンダリング: 新規依存を入れない。自前ダウンキャストも書かない
  (Einstein msg-112 §4 / Bohr msg-114 §2)。
- `html.unescape()` の追加: **入れない** (Tier-C msg-118 §3 実測: 保存本文に
  エンティティは無く、混入があるなら描画経路 ∴ 直す場所は表示側ではない)。

## 1. 契約 — PUT / GET

### 1.1 PUT — mindwire → magickit の push

```
PUT /v1/decisions/{project}/{thread_id}/material
Content-Type: application/json
```

**Body** (JSON):

| field | type | required | 意味 |
|---|---|---|---|
| `head_msg_id` | string | **必須** | 材料生成時の thread の head msg id (例 `"msg-2640"`)。**鮮度判定に使うキー** |
| `signature` | string | 任意 | composer の署名。**Magickit は parse しない**。保存のみ |
| `composer_status` | string | 任意 | `"ok"` 以外はエラー (§1.3) |
| `question` | string | 任意 | 判断の要旨 (散文) |
| `options` | array | 任意 | 選択肢の配列。要素は `{"id": "A", "label": "…", "gain": "…", "loss": "…"}` |
| `recommendation` | string | 任意 | 推奨する option の `id` |
| `recommendation_reason` | string | 任意 | 推奨理由 (散文) |
| `unknowns` | array of string | 任意 | 未確定事項 |

**応答**: 200 OK、body に `{"stored": true, "replaced": bool}`。`replaced` は
同一 `(project, thread_id)` の既存レコードを上書きしたかを示す (UPSERT の証拠)。

### 1.2 GET — 保存材料の外形確認

```
GET /v1/decisions/{project}/{thread_id}/material
```

**応答**:
- 保存されている → 200 + 保存した body (`stored_at` を追加)
- 保存されていない → 404 (JSON error envelope)

**用途は「PUT した内容が読み戻せる」ことの実測** (P-10 認可の確認 —
「200 が返った」ではなく「書いた内容が読み戻せた」まで見る)。判断ページ本体
は SQLite を直接読むので、GET を経由しない。

### 1.3 composer_status が `"ok"` でないとき

PUT が `composer_status` を **含み、かつ `"ok"` でない**なら、PUT は 400 で
拒否する。**部分保存しない**。既存レコードがあれば触らない (UPSERT を起動
しない)。

- 供給側 (mindwire) だけで弾けるという反論に対して、受け側でも弾く理由:
  「供給側の実装に受け側の正しさを預けない」 — 本スレッドが 3 回踏んだ
  「受け口だけ出荷、供給経路なし」の裏返しへの備え (msg-122 §3、
  msg-109 §3 の反対方向)。
- **UI の状態は 3 のまま**: `composer_status != "ok"` は「有効な材料が存在
  しない」と等価 ∴ 保存されず、判断ページからは **J-absent** として見える
  (msg-115 §3 / msg-122 §3)。第 4 の UI 状態を作らない (YAGNI)。
- `composer_status` フィールドは**任意** ∴ 欠けている PUT は検査しない
  (`"ok"` と等価に扱う)。**リテラル `"ok"` との等値比較 1 回のみ**であって、
  `signature` のような文字列 parse ではない。

## 2. ストレージ

### 2.1 テーブル

```sql
CREATE TABLE IF NOT EXISTS decision_materials (
    project        TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    head_msg_id    TEXT NOT NULL,
    signature      TEXT,
    question       TEXT,
    options_json   TEXT,             -- JSON serialized list of option dicts
    recommendation TEXT,
    recommendation_reason TEXT,
    unknowns_json  TEXT,             -- JSON serialized list of strings
    stored_at      TEXT NOT NULL,
    UNIQUE(project, thread_id)
);
```

- `composer_status` は保存しない — PUT で `"ok"` 以外を弾いた後に到達する経路
  だけが書き込む ∴ 保存された材料は定義上「composer_status == ok」である。
  格納する必要が無い field を格納しない (YAGNI)。
- `options_json` / `unknowns_json` は `TEXT` に JSON serialize して置く。
  `NULL` 可 (`options` が未提供のケース)。読み出し時に `json.loads` する。

### 2.2 UPSERT

書き込みは `INSERT OR REPLACE INTO decision_materials (...) VALUES (...)` の
**単文**。同一 `(project, thread_id)` への並行 PUT は SQLite の
`UNIQUE(project, thread_id)` に対して最終値で決着する (Heisenberg F-A /
`state_manager.py` L109/L551 と同じ方式)。**新規の並行制御コードは書かない**
(P-8)。

### 2.3 `stored_at`

ISO 8601 UTC 文字列 (末尾 `Z`)。**用途は診断のみ**。鮮度判定は `head_msg_id`
の完全一致で行う ∴ `stored_at` を鮮度計算に使わない (§3)。

## 3. 鮮度判定 — 3 状態への切り分け (msg-117 §5 / msg-122 §1)

判断ページの `mode=judgement` 分岐 (D-26' の駐機中枝) に到達したとき、
以下の順序で状態を決める:

1. `decision_materials` から `(project, thread_id)` の材料を読む。
   - **無い** → **J-absent**。§4-3 の描画。
2. あるなら `thread.last_msg_id` を取る (`chatroom.ChatroomAdapter.get_thread`
   の応答 `result["thread"]["last_msg_id"]`; §3.1)。
   - `thread.last_msg_id` が **無い / null / 空文字** → **J-stale に倒す**
     (§3.2 の fail-to-stale)。**J-fresh に落とさない**。
3. `material.head_msg_id == thread.last_msg_id` (文字列完全一致) なら
   **J-fresh**。§4-1 の描画。
4. そうでなければ **J-stale**。§4-2 の描画。

### 3.1 鮮度の SOT は `thread.last_msg_id` (Heisenberg F-C / Bohr msg-117 §1)

- 判断ページの現行ハンドラは既に `adapter.get_thread(mode="full")` を叩いて
  いる ∴ **新規呼び出しは要らない**。応答の `result["thread"]["last_msg_id"]`
  を 1 個読み足すだけ。
- `messages[-1]["msg_id"]` は使わない — 「今この応答に何通入っているか」に
  依存し、将来 mode / range を変えた人が鮮度判定を壊したことに気づかない。
  `thread.last_msg_id` は rollup で mode 非依存 (`chatroom.py` L1678-1682 の
  docstring)。
- 比較は**文字列の完全一致**。正規化しない (F-C 3: composer が push する
  `head_msg_id` と保存形が同じ `"msg-<N>"` 文字列 ∴ 正規化不要、かつ正規化は
  「相手の内部表現を解釈する」= msg-111 §3 で禁じた型)。

### 3.2 fail-to-stale の既定 (§3 の 2 番)

`thread.last_msg_id` が読めないときの倒し先は **J-stale** で、**J-fresh には
落とさない**。理由:

- materials が存在するのに head id が読めない場合、我々は鮮度を知らない ∴
  「新しい」と主張してはならない (D-26' の 503 と同じ規律 / msg-109 §7:
  持っていないものを持っているふりをしない)。
- P-9 (「`thread.last_msg_id` が live 応答に実在する」) が破れたときの症状を
  **「J-fresh が永久に出ない」に固定する** — 沈黙して古い材料を新品として
  出す、という壊れ方をしない。**この既定の価値は P-9 が真であることに依存
  しない**。

### 3.3 P-9 の状態 (Tier-C msg-118 §4 実測)

`thread.last_msg_id` は稼働中 Conclair の live 応答で 2/2 完全一致で確認
された:

| スレッド | `thread.last_msg_id` | `messages[-1].msg_id` | 一致 |
|---|---|---|---|
| `spirrow-magickit/T-decision-page` | `msg-117` | `msg-117` | ✅ |
| `spirrow-voxelworld/T-lod0-sliver-shards` | `msg-2619` | `msg-2619` | ✅ |

∴ 鮮度判定の SOT は live で使える。**ただし fail-to-stale の既定 (§3.2) は
撤去しない** — 2 件で実在したことは「常に実在する」を意味しない。

**実装時に 1 回だけ再確認する**: 判断ページのハンドラが実際に読む経路で、
`thread.last_msg_id` が実在し `messages[-1]["msg_id"]` と一致すること
(msg-122 §5 の内部順序 3)。一致しなければ止めて報告 (鮮度判定の前提が
崩れる)。

## 4. 描画 — 3 状態それぞれ

判断 UI (`decisions_thread.html` の mode=judgement) を、以下の 3 状態に
切り分ける。**共通は下部の判断フォーム 1 つ**で、3 状態すべてで

- `textarea name="_freeform"` (常設・空でも送れる — I-12)
- `select name="next_participant"` (§4-4)
- 「自由記述だけで送る」 submit ボタン (I-12 sentinel `(自由記述のみ)`)

を出す。**選択肢ボタンを出すのは J-fresh のときだけ**。

### 4.1 J-fresh — 材料を全部出す

描画順 (msg-117 §5 表):

1. `question` (最上部)
2. 選択肢カード (`options` の各要素について `label` / `gain` / `loss` +
   `<button type="submit" name="content" value="{id}: {label}">`)
3. `recommendation` + `recommendation_reason`
4. `unknowns` (list)
5. 自由記述 `<textarea>`
6. `next_participant` `<select>`
7. 送信ボタン群 (I-12 の「自由記述だけで送る」を含む)

汎用 2 択 (`"A: そのまま進める"` / `"B: 一旦止める / 修正が要る"`) は**廃止**
(msg-117 §5)。ボタンの `content` は composer の option から導出:
`f"{option.id}: {option.label}"`。id を落とさない (msg-098 §2 / msg-097 §4.1
一次照合)。

### 4.2 J-stale — 材料を 1 文字も描画しない (I-14)

**確定形** (Bohr msg-117 §2、Einstein msg-115 §2 全面採用):

- 最上部に 1 行の警告: 「この判断依頼の材料は `{material.head_msg_id}` 時点
  のもので、スレッドは `{thread.last_msg_id or "?"}` まで進んでいます。
  最新の議論は chatroom で確認してください。」+ chatroom 導線。
- `question` / `options` / `gain` / `loss` / `recommendation` / `unknowns`
  を**一切描画しない**。ボタンにしないだけでなく、**テキストとしても出力
  しない**。
- 判断フォーム (textarea + `next_participant` select + 「自由記述だけで送る」)
  は残す ∴ I-12 は 3 状態すべてで維持。**選択肢ボタンは出さない**
  (stale な option から `content` を作ると古い選択肢を現在の選択肢として
  送ることになる — msg-109 §7 違反)。

### 4.3 J-absent — 「材料が用意されていません」+ 導線 + フォーム

- 「判断材料が用意されていません。」+ chatroom 導線 + 判断フォームのみ。
- **tail (末尾数通) を描画しない** (Bohr msg-114 §1 / Einstein msg-112 §3
  全面採用)。「材料が無いときに、無理にチャットビューアの真似事をして文脈を
  捏造する」のは msg-109 §1(c) の既知欠陥の再輸入である。
- 判断フォームは残す (I-12 維持)。

## 4-1. 不変条件 I-14 — J-stale では材料をサーバ側で描画しない

**CSS (`display:none`) や `<details>` で隠すのではなく、サーバ側で描画しない**。

- 隠された文字列は view-source・コピー&ペースト・スクリーンリーダ・検索に
  届く ∴「隠す」は「無い」ではない (msg-117 §2)。
- テストで**「文字列が無い」**を pin する — 「ボタンが無い」ではなく。前者
  だけが I-14 を通す。J-stale で描画した HTML に、投入した stale な `label`
  / `gain` / `loss` / `question` の**いずれの文字列も含まれない**ことを
  assert する。

## 4-2. エスケープ — `html.unescape()` を入れない (無条件)

Tier-C msg-118 §3 実測: 稼働 Conclair の保存本文はエンティティを 1 つも
含まない (素の `"` = U+0022)。∴ 判断ページで `&#34;` が見えていたのは
描画経路での混入である。**直す場所は描画側であって表示側ではない**。

- `html.unescape()` を入れない。text として挿入 (Jinja autoescape に任せる)。
- **`|safe` 禁止** + lint テスト (§5)。F-B により template の既存使用は 0 件
  ∴ greenfield から始まる (追加コストは lint 1 本のみ)。
- 混入源は本 PR の時点では未特定 (msg-122 §2 の Q-5' 残問)。**が、S5'' が
  tail 描画を全状態から削除する** ∴ 混入が tail 専用経路にあるなら、S5'' が
  その経路ごと削除する ∴ 直すべきコードは存在しない (**消える経路にパッチを
  当てない**)。共有層 (handler / adapter / base template) にあるなら composer
  材料も同じ腐食を受ける ∴ 受入時 (A-14) の逐語 pin で必ず露見する。

## 4-3. markdown — 自前ダウンキャストを書かない (無条件)

`question` / `gain` / `loss` / `recommendation_reason` は composer 生成の
散文で、markdown 記法が入る保証も入らない保証もない。

- **自前縮約 (`**` 除去、見出し記号除去等) を書かない**。仕様の無い変換は
  「なぜこの記号だけ消えるのか」を誰も答えられなくなる (Einstein msg-112 §4)。
- **新規依存を入れない** (markdown-it 等)。
- そのまま text として挿入する (Jinja autoescape)。読みにくければそれは
  composer 側の出力の問題で、こちら側で隠すのは msg-109 §1 と同じ「見た目を
  繕う」対処になる。

## 4-4. `next_participant` の select (S5 増分 2 から不変)

- 値域は登録済 identity のみ (msg-084 §2)。
- **`none` と `pr-review <ref>` を出さない**。
- **既定値は駐機 msg の著者** (S5 増分 2 と同じ)。
- **I-13** (S5 増分 2): select に空 option を足さない。空 option を追加する
  ことは P-7 (`P-7` = 到達不能) の判定を無効化する (msg-110 Tier-C /
  msg-117 §5 §11.7)。

## 5. `\|safe` 禁止 lint

`tests/unit/test_templates_no_safe_filter.py` — `src/magickit/templates/` 配下
の全 `.html` に対して、`\|safe` (パイプ + safe) を含む行が無いことを assert
する。既存の `test_templates_no_external_assets.py` と同じ形。

- **絶対に必要な理由**: `|safe` が 1 か所でも入ると、その値は Jinja
  autoescape を通らず、`&`, `<`, `>`, `"`, `'` が生のまま HTML に落ちる。
  composer が生成する任意入力にこれを掛けると XSS になる。判断ページに
  限らず全 template に対して張る (greenfield ∴ 面倒でない)。
- **同ファイルに 1 件は「lint が動いている」証拠のケースを含める**
  (`test_the_check_catches_a_safe_filter` のように、literal に `{{ x|safe }}`
  を書いて捕まえられることを確認する)。

## 6. モバイル要件

- 縦 1 カラム。
- 選択肢は**カード** (表を使わない)。既存 `.decision-choice` を流用。
- **幅 390px で横スクロールを起こさず、判断フォームまで到達して送信できる
  こと** (A-18)。3 状態すべてで満たす。
- 判断ページの form は**プレーン HTML** (HTMX ではない)。既存 `S5 増分 2`
  の CSS を継承する (`.decision-container` に `max-width: 640px` /
  `box-sizing`)。

## 7. 受入基準 (A-14 〜 A-18) — 作業が成立したかで書く

**「HTML が要件表どおり」を ✅ にする項目は立てない** (msg-111 §7 / Tier-C 承認)。

| # | 基準 | 誰が |
|---|---|---|
| **A-14** | 実装後、`spirrow-voxelworld/T-lod0-sliver-shards` (Einstein の材料が存在するスレッド) に対して mindwire 側の push が起き、判断ページを開くと **question / options / gain / loss** が逐語で見える。実際の HTML を貼る (view-source ではなく render 結果) | Takahito / mindwire land 後 |
| **A-15** | 判断ページを **モバイル (幅 390px 実機)** で開き、**何を聞かれているか**が読めて、判断フォームまで到達して送信できる (A-18 と 1 回のタップで満たす) | Takahito |
| **A-16** | J-stale の 1 件を実際に作り (スレッドを進めた後の材料を残したまま新しい msg を追加)、判断ページを開くと**古い材料の文字列が 1 つも見えない** (view-source を検索して確認) — I-14 の外形確認 | Takahito / mindwire land 後 |
| **A-17** | J-absent の 1 件 (材料 push なし) を開くと「判断材料が用意されていません」+ chatroom 導線が出て、判断フォームが送信できる (tail が出ていないことも確認) | Takahito / mindwire land 後 |
| **A-18** | A-15 と**同一の 1 タップで**幅 390px 実機で横スクロールなし + 判断フォームまで到達 + 送信 (実タップは増やせない資源 ∴ 依頼に項目を足すほど各項目の精度が落ちる — msg-110 判断 2 / msg-122 §3) | Takahito |

**A-14 が検出器として働く** (材料の可視テキストを逐語で貼る) ∴ 共有層の
escape 混入があれば受入で必ず露見する。∴ 実装が Q-5' 混入源特定を落として
も、受入で気づく。

## 8. land 順序 (msg-111 §6 / msg-122 §5)

1. **PR #31 済** (Track A 完了 / msg-121)
2. **magickit** — 本 PR。material 受け口を建てる。**全ページ J-absent
   (mindwire push なし)。これを「実装済」と呼ばない** — 材料は 1 個も出ない
3. **mindwire の push** (別 repo / 別 PR)。**+ P-10 (認可) 実測**:
   - 経路: `sg-tomtebo-01 → magickit:8443` は既に実証済 (curl 群 / msg-118 §5)
   - 残: PUT に認証がかかるか / 我々を認可するか (未測)
   - 判定基準: 「200 が返った」ではなく「PUT した内容が GET で読み戻せた」
4. 外形実測 **A-14〜A-18** (§7)。**ここで初めて完了**

**本 PR で完了する範囲**: land 順序 2。全ページ J-absent。3 状態のうち 1
状態しか動かない。それを「実装済」と称する誘惑を退ける — 名前の悪用は
msg-111 §5 で警告済。

## 9. 未決 (この spec で片付けない)

- **Q-1a 経路**: 実測済 (閉じる)。
- **P-10 認可**: **未測**。land 順序 3 で測る (§8)。**本 PR では認可の
  設計をしない** — 測る前に塞ぐのは本スレッドが 3 回退けた形。
- **Q-5' 混入源 (エスケープ)**: 現時点で「消える経路にパッチを当てない」
  規律のもと、S5'' 完了で tail 描画が全状態から消える ∴ 経路が消えれば
  対処も不要になる (§4-2)。A-14 が共有層の混入を検出する。混入が共有層で
  発覚したら別 PR で対処。

## 10. 前提の一覧 (§S5-decision-page §11 の続き)

| # | 前提 | 状態 | 破れたときの症状 |
|---|---|---|---|
| P-8 | SQLite `INSERT OR REPLACE` が `UNIQUE(project, thread_id)` に対して冪等 | **実測済 (既存 `state_manager.py` L109/L551 で運用中)** (Heisenberg F-A) | UPSERT が別実装に化ける (本 PR では該当なし) |
| P-9 | `thread.last_msg_id` が live 応答に実在し `messages[-1].msg_id` と一致 | **契約実測済 + live 2/2 実測 (Tier-C msg-118 §4)** ただし fail-to-stale の既定は維持 (§3.2) | J-fresh が永久に出ない (§3.2 で固定) |
| P-10 | Magickit の PUT 面が我々を認可する (認証があれば通す / なければ 200) | **未測** (§9) | 全ページ J-absent。**嘘はつかないが無価値** |

## 11. 教訓 — 一般則 (S5-decision-page §6 の続き)

5. **反証が効くのは概念であって、実装ではない** — 「`unescape` を入れる」を
   Tier-C msg-118 §3 の実測で反証したが、これは「一度も入れない」を意味
   するのであって、「別の場所に入れて良い」を意味しない (msg-122 §2 の
   自己記録)。前提を確認してから設計を凍結する規律を、反対側の分岐にも
   一貫して当てる。
6. **消える経路にパッチを当てない** (Einstein msg-123 / Bohr msg-122 §2)。
   バグ発見時、そのコードが後続の変更で削除されるなら、直す前に消える方が
   優先。仕様の無い延命は YAGNI 違反。
7. **測る前に塞がない** (Einstein msg-123 §2 / Bohr msg-122 §4)。
   認証・認可・稀な失敗経路。実際に叩いて壊れることを見てから対処を設計
   する。想定でハイブリッド API 経路を作らない。
