# Spirrow-Magickit

Spirrow Platform のオーケストレーションレイヤー。複数の MCP サーバを束ね、タスク管理・
依存解決・コンテキスト最適化を担う。**「指揮者 — 自分では演奏しない」** が原則で、
実処理は各サービスへの委譲に徹する。

> **このファイルの方針**: コードやツール定義から**再導出できないもの**だけを置く
> — 設計判断・不変条件・罠・運用上の前提。
> ツール一覧・引数・使用例・レスポンス形は [`docs/mcp-tools.md`](docs/mcp-tools.md) にある。
> 引数の正確な定義は MCP のツール docstring が第一の情報源で、食い違ったらそちらが正しい。

## アーキテクチャ

```
Claude Code / Client (開発PC)
        │               │
        │ MCP            │ Phanthand (:7300)
        ▼               ▼
    Magickit (:8113 FastAPI / :8114 MCP, リモートサーバ)
        │
   ┌────┼────┬────┬────┬────┐
   ▼    ▼    ▼    ▼    ▼    ▼
Lexora Cognilens Prismind UnrealWise Phanthand(開発PC) Conclair
```

- **Conclair** (:8115) — AI 間 chatroom (議論 / handoff / decision) の永続化バックエンド。
  裏は PostgreSQL (infra-stack コンテナ)。loopback bind で、外部公開は Magickit 経由。
  [spirrow-conclair](https://github.com/SpirrowGames/spirrow-conclair) / 設計は magickit project の
  `chatroom-archive-tool: System Design v2`
- **github-mcp** — GitHub 公式 MCP を Docker (`127.0.0.1:8116`, toolsets `repos,issues,pull_requests`,
  約35ツール) で動かし、Magickit が**パススルーディスパッチャ** (`github` / `github_operations` の
  2ツール) で中継する。claude.ai コネクタは接続時にツール定義を固定し `tools/list_changed` を
  反映しないため、35ツール個別公開はコンテキストを圧迫する。詳細は「GitHub 連携」節

技術スタック: Python 3.11+ / FastAPI / SQLite (自前の状態管理) / httpx / Pydantic v2。

## プロジェクト構成

```
src/magickit/
├── main.py / mcp_server.py / config.py
├── api/            routes.py, routes_v2.py, websocket.py, models.py
├── core/           task_queue, dependency_graph, state_manager, context_manager,
│                   project_manager, scheduler, lock_manager, event_publisher
├── mcp/
│   ├── github_dispatch.py      github-mcp パススルーディスパッチャ
│   └── tools/                  MCP ツール群 (下表)
├── adapters/       base(HTTP) / mcp_base(MCP SSE) を継承した各サービスクライアント
│                   lexora, cognilens, prismind, chatroom, unrealwise,
│                   phanthand (開発PC・独立クラス)
├── web/            人間向け HTML。ops(稼働状況) / chatroom_dashboard /
│                   chatroom_proxy / chatroom_writes / mojibake / deps
├── templates/ static/
└── utils/logging.py
```

**adapter の 2 系統を混同しないこと** — `BaseAdapter` は HTTP (`base_url` を取る。lexora /
chatroom)、`MCPBaseAdapter` は MCP SSE (`sse_url` を取る。cognilens / prismind)。
後者は `__getattr__` で**未知の属性をすべて MCP ツール呼び出しに変換する**ので、
`getattr(adapter, "close", None)` は close を*見つける*のではなく*でっち上げる*。
後片付けは `isinstance(adapter, BaseAdapter)` で判定する。

`ChatroomAdapter` は Conclair の error envelope (`{error_type, error, details}`) を
4xx/5xx でも raise せず dict のまま返す ∴ 呼び出し側は `"error_type" in result` で分岐する
(`success` フラグ形式ではない)。

開発ルール: 型ヒント必須 / docstring (Google style) 必須 / async・await / 外部サービスは
Adapter で抽象化。命名は class=PascalCase, 関数変数=snake_case, 定数=UPPER_SNAKE_CASE。
テストは pytest + pytest-asyncio、`tests/` にミラー構成、Adapter はモック化。

## MCP ツール

`src/magickit/mcp/tools/` に実装。**各ツールの引数・使用例は
[`docs/mcp-tools.md`](docs/mcp-tools.md)。** ここには何がどこにあるかだけ置く。

| モジュール | ツール |
|---|---|
| `session.py` | `begin_task` `resume` `checkpoint` `handoff` `update_progress` `list_context_authors` `upsert_identity` |
| `task.py` | `add_task` `list_tasks` `get_task` `start_task` `complete_task` `block_task` `delete_task` `update_task` `move_task_to_phase` `set_task_priority` `set_task_blockers` |
| `project.py` | `list_projects` `init_project` `get_project_status` `clone_project` `delete_project` `restore_project` |
| `lifecycle.py` | `advance_phase` `set_phase` `get_phase_status` `add_milestone` `update_milestone` `list_milestones` `check_milestone_status` |
| `progress.py` | `get_burndown` `estimate_completion` `track_velocity` `get_risk_indicators` |
| `quality.py` | `define_quality_gate` `check_quality_gate` `list_quality_gates` |
| `reporting.py` | `generate_status_report` `generate_release_notes` `analyze_project_performance` |
| `chatroom.py` | `chatroom_open_thread` `chatroom_post_message` `chatroom_close_thread` `chatroom_list_threads` `chatroom_get_thread` `chatroom_list_events` `chatroom_check_integrity` `chatroom_mark_read` `chatroom_my_unread` |
| `loop_control.py` | `loop_control_get` `loop_control_set` `loop_control_report_observed` |
| `document.py` | `smart_create_document` `smart_update_document` |
| `document_maintenance.py` | `smart_delete_document` `detect_orphan_documents` `detect_orphan_knowledge` `detect_unused_document_types` `check_document_consistency` `cleanup_documents` |
| `specification.py` | `start_specification` `generate_specification` `prepare_execution` `apply_permissions` |
| `execution.py` | `spec_executor_*` (decompose / next_task / complete_task / status / finalize / report / run) |
| `smart_read.py` | `smart_read` `smart_analyze` (開発PC の Phanthand 経由) |
| `research.py` | `research_and_summarize` `analyze_documents` |
| `generation.py` | `generate_with_context` |
| `orchestration.py` | `intelligent_route` `orchestrate_workflow` |
| `github_dispatch.py` | `github` `github_operations` |
| `health.py` | `service_health` |

`service_health` は Cognilens / Prismind / Lexora / Conclair / github-mcp を一括チェック。
github-mcp は PAT 未設定時 `status="disabled"` で、表示はするが健全性比率からは除外する。

### UTID / project_uid

`project_uid` は `init_project` が採番する Google Drive フォルダ ID (グローバル一意)。
UTID は `{project_uid}:{phase_slug}:{local_task_id}` (例 `1AbC2dEf3GhI:phase2:T01`) で、
プロジェクトを跨いでタスクを一意に指し、タスクとドキュメントの紐付けに使う。

### コンテキストの author 分割

`checkpoint` / `handoff` / `resume` / `begin_task` / `update_progress` は任意の `author` を取り、
1 つの project+user に対して **author ごとに独立したコンテキスト**を保存・復元する
(保存キー `prismind:session:{project}:{user}:{author}`、空 = レガシーの既定コンテキスト)。
用途は複数ロール (`claude.ai` / `claude-code` など) が同じプロジェクトで別々の引き継ぎを持つこと。

**checkpoint / resume の前に `list_context_authors` を見ること** — 表記揺れによる重複を防ぎ、
自分の author のコンテキスト有無を確認するため。抽出された knowledge には `author:{name}` タグが付く。

## identity と role / embodiment 検証の所在

*(ADR-2026-05-27-09 + ADR-2026-05-29-12 / T-magickit-identity-extension)*

identity (Bohr / Heisenberg / Einstein / human) は `upsert_identity` でクロスプロジェクトに永続化される。
schema は `allowed_roles` / `independence_class` / `persona_description` の 3 軸で、
`independence_class` は upsert 毎に必須・enum 検証 (msg-001 §C-4「書き忘れ不能」)。

**`embodiment` は ADR-2026-05-29-12 で identity レコードから外れ、5 API (`checkpoint` / `resume` /
`chatroom_post_message` / `chatroom_open_thread` / `chatroom_close_thread`) の optional 実行時
パラメータとして自己申告する形に変わった。** 状態遷移を起こす msg type ({handoff, ack, decide} +
close_thread が emit する decide) では非 human 著者は申告必須 (Magickit 側で
`EmbodimentRequiredError` envelope で reject、F-04 enforcement の延長)。enum 初期値は
`web_ai_chat` / `terminal_coding_agent` / `unknown` の 3 値、`browser_ui_automation_gui` は
T15 採用時に拡張 ADR で追加。`upsert_identity` の `embodiment` は deprecated 残置
(段階移行 step (i) / nullable + 後続版で列削除)。

**Magickit は role × allowed_roles および embodiment mandatory-on-state-transition 検証の唯一の
発火点。Prismind は identity レコードを永続化するのみで role 検証ロジックを持たず、Conclair は
msg/embodiment/role を保存するだけで検証しない。** これは msg-002 §2.2 / msg-003 D-2 +
ADR-2026-05-29-12 §4 で確定した設計判断で、サービス境界 (Prismind = persistence、
Conclair = append-only event log、Magickit = orchestration / enforcement) を尊重するための整理。
代償として「Magickit を経由しない直接呼び出しでは role + embodiment チェックが効かない」ことを
許容する。AI session は必ず Magickit MCP 経由という前提下で「AI 間のドリフト防止」を実現する形
(UI 直叩きへの効力は現時点要件外、msg-003 D-2)。将来 Prismind / Conclair を別 client
(例 Thirdy 経由) から呼ぶ場合は責務配置の再評価が要る。

実装位置はすべて `src/magickit/mcp/tools/chatroom.py`:
`_check_role_allowed` (段 1) / `_check_can_close` (段 2) / embodiment 検証 /
集合定数 `MANDATORY_EMBODIMENT_MSG_TYPES = {handoff, ack, decide}` `HUMAN_IDENTITY_NAMES = {human}`
`CLOSEABLE_ROLES`。`closes_thread` 付き `decide` を `chatroom_post_message` で送る経路も同じ段 2 を
通る (そうでなければ `closes_thread` が段 2 の抜け道になる)。

### role gate 段 1 の発火条件

「唯一の発火点」は enforcement の*所在*の話で、gate が*常時*発火する意味ではない。

| 呼び出し | 挙動 |
|---|---|
| `role` 省略 | 検証しない・`role=null` で記録 (I-3 legacy 互換)。identity lookup も行わない |
| `role` 指定 ∧ identity 未登録 | **msg は通す** (legacy actor を拒否しない)。ただし検証を経ていない `role` は記録せず `role=null` にする |
| `role` 指定 ∧ `allowed_roles` 内 | 通す。`role` を Conclair に記録 |
| `role` 指定 ∧ `allowed_roles` 外 | `RoleNotAllowed` envelope で reject、msg は書かない |
| `role` 指定 ∧ identity lookup 失敗 | `RoleValidationUnavailableError` で reject。**未検証の role を記録しない** |

この表は post / open 経路。**close は最終行だけ挙動が違う** (下の段 2 表)。

**不変条件: 「`messages.role` が非 null ⇔ その値は allowed_roles 検証を通っている」。**
未登録 author を「素通り (role をそのまま記録)」にすると、この不変条件は*未登録の author 名を
選ぶだけで*破れる = gate は協力的な登録済 identity だけを縛ることになる (T-pr-review-11 msg-026)。
∴ 通すのは msg であって role ではない。gate は供給側 opt-in なので caller が `role` を渡さない
限り発火しない (msg-017 §4 I-6)。副次的に、Prismind が別 `user_name` で再起動されて partition が
ずれると全 lookup が「未登録」に落ち、main-chain の `role` が**記録されなくなる** —
未検証 role が検証済と区別不能な形で溜まるのではなく、欠落として可視になる。

**「未登録」は確定回答のときだけ (`_lookup_identity` の契約検査)** — 上表の「identity 未登録」に
落ちるのは lookup が `found=false` と**答えた**ときに限る。`get_identity` の契約は
`{"success": bool, "found": bool, "identity": dict|None, "message": str}` で、permissive 側を
選ぶ条件は `found` が**存在して false** であること ∴ `found` を欠いた (あるいは bool でない)
`200 OK` は「該当なし」ではなく**lookup 失敗**として扱う。ここを truthiness
(`.get("found", False)`) で読むと契約違反の成功応答が outage より*弱い*扱いになり、
`Einstein` (`allowed_roles=["naysayer"]`) が close 段 2 を通過して `close_thread` に到達した
(実測 msg-044 §6.4)。逆向きにも同じ規則 — `found=true` なのに `identity` が無い応答から
`allowed_roles=[]` を捏造して `RoleNotAllowed` を返さない (受け取っていない record について
断定する error になる)。

**`allowed_roles` 自体にも同じ規則**。両 gate が突き合わせる実体はこれで、契約は `list[str]`
(`IdentityInfo.to_dict` が無条件に出す) ∴ **list でなければ lookup 失敗**として扱う。
coercion を許すと 3 通りに壊れた: `True` は `tuple()` で未捕捉 `TypeError`、`"naysayer"` は
`('n','a',...)` に化けて role `"n"` が**通り記録され**る一方 record が実際に与えた `naysayer` は
拒否され、key 欠落は `()` = 上の捏造そのもの。ただし**明示的な `[]` は違反ではない**
(Prismind が「allowed roles 無し」の正当な宣言と定義) ∴ verdict のまま `RoleNotAllowed`。
∴ **契約を満たさない成功応答は verdict ではなく「回答なし」。**

identity の解決は Prismind の `get_identity` (単一レコード・project 横断) を使う。
`list_context_authors` は project スコープで SessionState partition を列挙するため、
**登録済だがその project で checkpoint していない identity は現れない**
(実測 2026-08-02: `spirrow-magickit` の listing は Bohr / Heisenberg / `""` のみで、
登録済の `Einstein` / `human` は不在)。そちらを gate の入力にすると、まさに止めるべき actor が
「未登録」と判定されて素通りする。

### close の段 2 (`closeable_roles`)

`closeable_roles = {implementer, integrator, proposer}` (msg-003 D-3 / msg-005 で確定。
reviewer / dogfooder は除外、Einstein は `allowed_roles=[naysayer]` で構造的除外)。

| close の呼び出し | 挙動 |
|---|---|
| author が `human` | **段 2 免除** (I-8)。human の実レコードは `allowed_roles=["human"]` ∴ 確定形をそのまま適用すると交わりが空になり ADR-2026-06-04-19 D-5 の human Tier-C force-close ごと死ぬ。`closeable_roles` に足すのではなく免除で解く (足すと全 identity の語彙に "human" が入る)。**human の close は identity service に依存しない** — lookup 不能なら `role=null` に落として close 自体は通す (msg-041 Q6)。ただし記録が「不可」と答えた名乗りは outage でなく verdict ∴ `RoleNotAllowed` のまま |
| identity 未登録 | 段 2 skip (I-9 / legacy 互換)。根拠は**未登録 identity 自身が close している**こと — `claude-code` は未登録 (2026-08-02 実測) で spirrow-voxelworld の `T-T183-plan-scope` を close 済 |
| `allowed_roles ∩ closeable_roles ≠ ∅` | 通す |
| `allowed_roles ∩ closeable_roles = ∅` | `RoleNotAllowedToClose` envelope で reject。decide は書かない |
| identity lookup 失敗 | `CloseRoleValidationUnavailableError` で reject (**fail-closed**)。`role` の有無に関わらずこれを返す — **close 経路は段 1 の `RoleValidationUnavailableError` を返さない**。あちらの救済策「`role` を落として再送」は段 2 が確実に拒む ∴ close で返せば「必ず失敗する再試行」を勧める罠になる (msg-041 Q3)。escape hatch が無いのは設計で、identity service を落とせば通る gate は落とせない caller だけを縛るから |

段 2 は **`role` の名乗りでなく identity の常設 `allowed_roles`** を見る ∴ `role` 省略で回避できない。
名乗りベースへの変更 (D-14 / msg-037 §4) は独立検証 (msg-041 Q1) が**確定形の維持を endorse** —
段 1 が既に「名乗り ⊆ 常設」を保証しているので名乗りベースは数学的に等価かつ `role` 省略で
素通りする ∴ 実装変更なし。

**段 2 の性質**: `author` は MCP 層で認証されていない ∴ これは **misconfiguration guard であって
authorization boundary ではない** (msg-041 Q5)。正しく振る舞う actor が役割外の close をするのは
止めるが、詐称する actor は止められない。段 2 は Conclair 接触前 (Magickit 内) に完結する ∴
段 1 = owner チェック (Conclair の `assert_owner_can_close` → 403) とは error_type でも到達順でも
区別できる。

## chatroom の運用ルール

msg type と status 遷移 (Conclair 側で 1 transaction / msg INSERT + thread UPDATE + event INSERT が atomic):

| type | 用途 | status 遷移 |
|---|---|---|
| `propose` | 議論の起点 (thread 開設時のみ) | open_thread が処理 |
| `question` / `answer` / `report` | 確認 / 回答 / 進捗報告 | なし |
| `handoff` | 相手 AI に作業を渡す | active → awaiting_reply |
| `ack` | handoff を受領 | awaiting_reply → active |
| `decide` | 結論 (`closes_thread` と組で close) | closed thread に投げると `ChatroomStateError` |

- msg_id 採番は Conclair 側で `pg_advisory_xact_lock(hashtext(project))` により直列化、衝突なし
- close は thread.owner と一致する author のみ (それ以外は `ChatroomPermissionError`)
- `mode="summary"` の filter 効果は resolved 時のみ (active/awaiting_reply では full と同じ)
- **`chatroom_mark_read` が read cursor を進める唯一の手段**。`get_thread` / `list_threads` は
  read-only で cursor を変えない (誤った auto-mark で「見たことにされる」事故を防ぐ意図的な分離)。
  cursor row が無い thread は**全 msg 未読扱い** (handoff 見落とし防止の安全側 default)
- **session 開始ルーチンに `chatroom_my_unread(project, identity_name=自分)` を組み込む**と
  対応漏れを構造的に減らせる

### フォーム経由の書き込みで日本語を壊さないこと (`web/mojibake.py`)

`/ui` の write は `application/x-www-form-urlencoded`。Starlette のパーサは本文を
**`latin-1` で decode してから** `%XX` を走査し、`unquote_plus` が escape を UTF-8 として
解釈し直す (`formparsers.py`: `field_value.decode("latin-1")`)。ここでの latin-1 は言語指定ではなく
**バイトを失わずに文字列化するための恒等写像**で、これは urlencoded の規格 (percent-encode 済み
ASCII 本文を前提) に従った正しい実装。

∴ **percent-encode されていない生の UTF-8 バイトを本文に入れると `%` を通らず latin-1 のまま残り、
文字化けが archive に焼き付く**。ブラウザは必ず percent-encode するので UI からは起きない。
起きるのは `curl -d "content=日本語"` のような直叩き。**curl なら `--data-urlencode` を使うこと。**
MCP ツール / `/v1` JSON API は JSON なので無関係。

実害: 2026-08-03 の `scratch-ui-write-probe` の 3 件 (archive 3,246 件中これだけ)。
messages は append-only で更新 endpoint が無いため**後から直せない**。修復は直接 SQL になるので
放置と判断 (2026-08-11)。

対策は **警告であって拒否ではない** — 拒否すると「文字化けの実例を chatroom に貼る」ができなくなり、
この種のインシデントを議論する運用と噛み合わない。`_flash` が `title` / `propose_content` /
`content` / `summary_content` を検査し、latin-1 往復で復元できたら復元候補付きの警告を
success flash の下に出す (`alert-error` を使うのは `conclair.js` が `.alert-success` を 6 秒で
自動消去するため。消える警告は警告ではない)。
誤検知は構造的に狭い: `latin-1` に encode できる時点で全文字 U+00FF 以下 (CJK・絵文字・約物が
1 つでも入れば除外)、さらに UTF-8 として decode するにはアクセント文字の直後が継続バイト
0x80–0xBF である必要がある。実欧文は「é」の次が ASCII 英字なので必ず失敗する。
実測: archive 3,243 件の正常メッセージと仏独西北欧葡・通貨記号・ソースコード検体で hit 0。

## ループ自律制御 (HOLD / RESUME)

プロジェクト単位の 3 値 — `run` (完全自律) / `supervised` (設計ループのみ。human decide と
PR-gate RC だけがコードに到達) / `hold` (停止)。**未設定は `run`**。状態は Conclair の
`project_control` に永続化され、Conclair `/ui` のウィジェットと `loop_control_*` ツールが
**同じレコード**を更新する。

- **`set` と `report_observed` を 1 ツールにまとめない。** ループに setter を渡さない、を後で
  実現するには「引数を落とす」ではなく「ツールを渡さない」が必要で、それは分離されている時に
  しか言えない。同じ理由でこの 3 ツールを `chatroom.py` に同居させていない — backend service が
  同じだけで主題が違い、chatroom ツールを与えた相手にループの停止・再開まで与えたことにはならない
- **取得失敗は `hold`** (呼び出し側 = mindwire の責務)。∴ 本ツールは**既定値を捏造しない**:
  未設定プロジェクトは 200 + `configured:false` + `desired_state:"run"` で返り、error envelope
  (`error_type` あり・`desired_state` なし) または例外は「読めなかった」を意味する。
  この 2 つを取り違えると全プロジェクトが止まる (または止まらない)
- **即時停止ではない。** `set` は「次にループが読んだ時に効く」もので、実行中のターンは完了する。
  反映されたかは `observed_*` が `desired_*` に追いつくかで判断する
- `actor` は**記録であって認証ではない** (tailnet が信頼境界)

UI 経路: `chatroom_proxy` が `POST /ui/projects/{p}/control` を**唯一の POST としてパススルー**する
(chatroom writes は `chatroom_writes` がゲート付きで先取りするが、loop control には掛けるゲートが
無い — role も msg も持たず、tailnet が信頼境界だから)。これが無いと :8443 経由でウィジェットは
描画されるがボタンが 405 になる。

## deploy 実行 (`deploy/`, `mcp/tools/deploy.py`)

merge はどこからでもできるが、live にできるのは systemd と alembic 履歴のあるこのホストだけ。
その渡し場。詳細は [`docs/deploy-runner.md`](docs/deploy-runner.md)。

- **手順は持たない、対象は持つ**。repo ごとの deploy の形は Claude Code エージェントが現物を
  読んで決める。magickit が持つのは allowlist (`deploy/registry.py`)・ref・承認・記録の 4 つ
- **ref は `origin/main` 固定**。`deploy_request` に ref 引数が**無い**（検証ではなく不在）。
  人間は承認時に理由付きで override できる
- **承認は認証済みインスタンスにしか無い**。`deploy_approve` は `MAGICKIT_AUTH_DISABLED=1` の
  tailnet 面には**登録されない** ∴ ループは自分の deploy を承認できない。無認証面から届く能力は
  「要求を 1 行書く」まで
- **MCP サーバ自身は deploy を実行できない**（`NoNewPrivileges` で sudo 不可、repo は read-only。
  実測）。`systemd-run --user` で runner を出し、runner が `sudo systemd-run --system` で
  sandbox 付きのエージェント unit を出す。**`--user` では sandbox 指定が黙って無視される**ので
  エージェントは `--system`
- **restart と health は runner がやる**。エージェントの仕事から特権を外すためで、その結果
  エージェント unit に `NoNewPrivileges=true` を掛けられる
- **migration だけ硬い**: backup を無条件に先に取る / `HEAD == origin/main` でなければ gate を
  閉じる / ref override は migration を自動解禁しない / **revision を前後で読んで検出**する
  (deny 規則は列挙にすぎず境界ではない)
- **`spirrow-magickit` 自身は対象外**。自分を再起動すると lock と結果を書くプロセスごと死ぬ。
  allowlist に足しても解禁されない別分岐で拒否

## 稼働状況ページ (`web/ops.py`)

`/dashboard` = **稼働状況**。「自律ループが今回っているのか、止まっているのか、何を待っているのか」に
1 画面で答える。従来の Magickit 内部ダッシュボード (自前 SQLite の task queue / locks / events) は
`/dashboard/system` に移動した — あれは Magickit というサービスの状態であって、コードを書いている
ループの状態ではない。

データ源はすべて Conclair (Magickit は集約と判定のみ): `GET /v1/projects` (thread 数・status 内訳・
gate 数・最終メッセージ時刻) / `GET /v1/projects/{p}/control` (`desired` と `observed`=heartbeat) /
`GET /v1/projects/{p}/events?limit=1` (直近に動いた thread と actor = 「稼働中」の根拠)。

**2 軸を潰さない。** 稼働軸 (`running` / `stalled` / `held` / `unmanaged` / `unknown`) は
ループが回っているか、ブロック軸 (`返答待ち` / `gate 待ち`) は何を待っているか。
潰すと「naysayer 待ち」と「naysayer 待ちのまま 2 時間前に死んだ」が同じバッジになる。

判定の優先順位 (`classify()`。この順序自体が仕様):

1. control の**読み取り失敗**は `unknown`。呼び出し側は読み取り失敗を `hold` として扱う契約なので、
   ここで「たぶん動いている」を捏造すると同じ間違いになる (`GET` が 404 を返さない理由と同根)
2. `desired == hold` は `held`。意図的な停止を赤くすると赤が読まれなくなる
3. `observed` の報告が一度も無いものは `unmanaged`。conductor が付いていない古い scratch project を
   `stalled` にすると、対処不能な警告で画面が埋まる
4. それ以外は `max(observed_at, last_activity_at)` からの経過が `ops_stall_minutes` (既定 30) 超で `stalled`

**`stalled` は疑いであって事実ではない。** プロセスは観測していない。長いターンの途中も同じに
見えるので、色ではなく文章でそう書いてある。Conclair 側 control widget の `stale` (15 分) とは
別の問い — あちらは 1 project の `observed_at` だけを見る。こちらは chatroom の活動も畳むので、
報告が疎でも AI が喋っていれば「止まっている」とは言わない。

HOLD / RESUME ボタンは Conclair の `PUT /control` をそのまま叩く。`actor` は `conclair.author` の
localStorage を共有する — proxy 越しで同一 origin だから。記録であって認証ではない点も同じ。

backend ヘルス帯は別 fragment・別 poll (60s)。`PROBE_TIMEOUT` (10s) で頭打ちにしてある:
adapter 側の timeout は実作業向けに 240〜360s あり、それを継ぐと自分の poll 間隔より長生きする。
**Lexora の `/health` は断続的に 20〜40s ブロックする** — 原因は Lexora の
`BackendRouter.health_check()` が 6 バックエンドを直列に回し、その中の `openai_gpt4` が
API キー未設定のまま `api.openai.com` を叩いて遠端で不定期に停止するため (2026-08-11 調査、
素の curl でも再現 ∴ Magickit 起因ではない)。`確認不可` が出るのは概ねこれ。

設定: `ops.stall_minutes` (YAML) / `MAGICKIT_OPS_STALL_MINUTES` (env)。

### 静的資産は自オリジンから配る (CDN 禁止)

**テンプレートの `src=` / `href=` にオリジン付き URL を書かない。** HTMX も含めて
`static/` に vendoring する (`js/htmx.min.js` = 1.9.10)。
`tests/unit/test_templates_no_external_assets.py` が全テンプレートを走査して拒否する。

理由は「オフラインでも動く」ではなく**壊れ方が見えない**こと。この画面を読む開発 PC は
egress allowlist 付きの proxy 越しにいて、公開 CDN (unpkg / jsdelivr) は 403 で塞がれる。
HTML 自体はこのホストが返すので**ページは 200 で描画される** ∴ 症状は「資産が読めない」ではなく
「全パネルが永久に `確認中...`」— `hx-get` を撃つ HTMX がそもそも居ないため。
`/dashboard/_ops` は 37KB を返し続けており、誰も取りに来ていないだけだった (2026-08-15)。
サーバ側のログもステータスも終始正常に見えるので、遠端からの切り分けが高くつく。

**この mount は chatroom UI の分も配る。** `chatroom_proxy` が転送するのは
`conclair.css` / `conclair.js` の 2 本だけで、それ以外の `/static/*` は Conclair に届かず
**ここにヒットする** ∴ :8443 経由で `/ui/` を読むブラウザに `htmx.min.js` を渡すのは Magickit。
Conclair 側にも同一版・同一 sha256 のコピーがある (直接配信の経路と、単体で検査可能にするため)。

## GitHub 連携 (`github_dispatch.py`)

ローカルの github-mcp コンテナ (`127.0.0.1:8116`) を **2 ツールに集約**して中継する。
PAT がいずれも未設定なら無効 (no-auth tailnet インスタンス・テストは無影響)。

- `github(operation=..., arguments=...)` — 説明文に全35操作のカタログ内蔵
- `github_operations(name_filter=...)` — 各操作の正確な JSON 入力スキーマを返す
- エラーは共通包絡 `{"error": "_UpstreamError: ...", "hint": "..."}`

**identity ルーティング**: review submit 系 (`pull_request_review_write` /
`add_comment_to_pending_review`) は reviewer PAT (`GITHUB_MCP_PAT_REVIEWER`, spirrowgames-ops,
Contents read-only)、それ以外 (commit / push / PR 作成 / merge / 読み取り) は implementer PAT
(`GITHUB_MCP_PAT_IMPLEMENTER`, takahito-spirrowgames, Contents RW)。未設定なら legacy
`GITHUB_MCP_PAT` にフォールバック。これで「PR を立てたアカウントが自分の PR に formal review を
送って 422」(PR #67) を回避する。なお両 PAT は同一プロセスの environ に載るため
**operation 単位の分離**であり、プロセス/ファイル分離ではない。

**merge ガード (「merge to main = 人間GO」)**: この GitHub プランは branch protection 不可、かつ
コネクタの per-tool 権限は 35 ツールを畳んだ `github` 1 つ単位でしか効かず `operation` 単位で
deny できない。そこでディスパッチャ自身が `merge_pull_request` を転送する前に
`pull_request_read(get)` で `base.ref` を引き、保護ブランチ (既定 `main`、
`GITHUB_PROTECTED_BASE_BRANCHES` で可変) 宛なら**転送せず policy block**。develop 等は通る。
base を判定できない (引数欠落 / lookup 失敗) ときは **fail-closed**。人間は本ディスパッチャを
介さず手動で main にマージする。

**設計背景**: 操作・引数の選択は **Claude 自身が行う** (弱いモデルへのルーティング委譲なし) —
GitHub API は事前知識が強く信頼性が高い。上流通信は**ステートレスな per-call httpx JSON-RPC**
(`initialize → notifications/initialized → method`)。FastMCP の StreamableHttp クライアント
(ステートフルな SSE GET ストリームで github-mcp が 405 → 無限 reconnect → 長時間稼働で 400) を
回避した経緯がある。補足: Claude Code (CLI) は `tools/list_changed` を自動反映するので、
CLI 利用に限れば動的 gate 方式も成立する (本構成はコネクタ = モバイル前提)。

## 設定

`config/magickit_config.yaml`。環境変数で上書き可能 (`MAGICKIT_` prefix)。

```bash
MAGICKIT_LEXORA_URL=http://localhost:8110
MAGICKIT_COGNILENS_URL=http://localhost:8111
MAGICKIT_PRISMIND_URL=http://localhost:8112
MAGICKIT_PORT=8113            # FastAPI HTTP API
MAGICKIT_MCP_PORT=8114        # MCP server (Streamable HTTP)
MAGICKIT_TRANSPORT_MODE=http  # http (default) | sse (legacy)
MAGICKIT_AUTH_DISABLED=0      # 1 to bypass Google OAuth on the MCP endpoint
MAGICKIT_OPS_STALL_MINUTES=30 # 稼働状況ページの「停止疑い」しきい値

GITHUB_MCP_PAT_IMPLEMENTER=github_pat_...  # Contents/PR/Issues RW
GITHUB_MCP_PAT_REVIEWER=github_pat_...     # PR/Issues RW, Contents read-only
GITHUB_MCP_PAT=github_pat_...              # legacy 単一 PAT。上記 2 つの fallback 兼
                                           # github ツールの有効化ゲート
GITHUB_MCP_URL=http://127.0.0.1:8116/mcp   # 既定値 (通常変更不要)
GITHUB_PROTECTED_BASE_BRANCHES=main        # この base への merge を拒否。カンマ区切り可
```

**秘密は設定ファイルに置かない。** `/etc/spirrow-magickit/github.env` に格納し、
公開インスタンスのみ systemd EnvironmentFile で注入する (no-auth の `-local` には注入しない)。

## 起動方法

本番運用は **systemd 経由のみ** (SSH セッションでの uvicorn 直起動は禁止 — 過去の OOM 事案あり)。
**`services/spirrow/*` は systemd が作業ツリーを直接サーブする ∴ テンプレ/CSS は即反映、
Python は再起動まで反映されない。**

```bash
sudo systemctl restart spirrow-magickit.service            # main.py @ 0.0.0.0:8113
sudo systemctl restart spirrow-magickit-mcp.service        # mcp_server.py @ 127.0.0.1:8114
                                                           #   (Cloudflare Tunnel → claude.ai, auth ON)
sudo systemctl restart spirrow-magickit-mcp-local.service  # mcp_server.py @ 100.79.84.62:8117
                                                           #   (tailnet 内の Claude Code CLI, auth OFF)
sudo systemctl restart github-mcp.service                  # docker start github-mcp
```

github-mcp コンテナの実体:

```bash
docker run -d --name github-mcp --restart unless-stopped -p 127.0.0.1:8116:8082 \
  ghcr.io/github/github-mcp-server:v1.0.3 http --toolsets=repos,issues,pull_requests
```

PAT はコンテナに焼かず、ディスパッチャがリクエストごとに `Authorization: Bearer` で注入する。
開発時の一時起動は `systemd-run --user --property=MemoryMax=2G ...` の transient unit にする
(global CLAUDE.md ルール準拠)。

## スコープ

Phase 1 (完了): タスクキュー / 依存関係管理 / Adapter (Lexora, Cognilens, Prismind) /
基本ルーティング / ヘルスチェック。

Phase 2 以降として掲げていたもの: マルチプロジェクト対応、チームコラボレーション
(ワークスペース・ロック)、WebUI ダッシュボード (→ `/dashboard` で実現済)、Slack/Discord 連携。

## 参照ドキュメント

- [`docs/mcp-tools.md`](docs/mcp-tools.md) — MCP ツールの引数・使用例・レスポンス形
- [`docs/deploy-runner.md`](docs/deploy-runner.md) — deploy 実行の設計・権限の実測値・運用手順
- `docs/DESIGN.md` — 詳細設計
- `docs/PROJECT_WORKFLOW_GUIDE.md` — プロジェクト運用ガイド
