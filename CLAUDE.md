# Spirrow-Magickit

オーケストレーションレイヤー for Spirrow Platform

## 概要

複数のMCPサーバを統合し、ローカルLLMによる知的なルーティングと最適化を行う司令塔。タスク管理・依存関係解決・コンテキスト最適化を担当。

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

**重要**: 「指揮者 - 自分では演奏しない」。各サービスへの委譲に徹する。

`Conclair` (:8115) は AI 間 chatroom (議論 / handoff / decision) の永続化バックエンド。`PostgreSQL` (infra-stack コンテナ) が裏側で動く。詳細は [spirrow-conclair](https://github.com/SpirrowGames/spirrow-conclair)、設計は magickit project の `chatroom-archive-tool: System Design v2`。

`github-mcp` は GitHub 公式 MCP サーバ ([github/github-mcp-server](https://github.com/github/github-mcp-server)) を Docker (`127.0.0.1:8116`, toolsets `repos,issues,pull_requests`, 約35ツール) で動かし、Magickit が **パススルーディスパッチャ** (`github` / `github_operations` の2ツール) で中継する。claude.ai コネクタは接続時にツール定義を固定し `tools/list_changed` を反映しないため、35ツールを個別公開せず2ツールに集約してコンテキストを節約する設計。詳細は「GitHub 連携 (`github_dispatch.py`)」節を参照。

## 技術スタック

- Python 3.11+
- FastAPI
- SQLite (状態管理)
- httpx (非同期HTTPクライアント)
- Pydantic v2

## プロジェクト構成

```
src/magickit/
├── main.py              # FastAPIアプリ
├── mcp_server.py        # MCPサーバエントリポイント
├── config.py            # 設定 (Pydantic Settings)
├── api/
│   ├── routes.py        # エンドポイント
│   └── models.py        # Request/Response
├── core/
│   ├── task_queue.py    # タスクキュー
│   ├── dependency_graph.py  # 依存関係グラフ
│   ├── state_manager.py # 状態管理
│   ├── context_manager.py   # コンテキスト最適化
│   ├── project_manager.py   # プロジェクト管理
│   └── scheduler.py     # スケジューラ
├── mcp/
│   ├── github_dispatch.py  # github-mcp パススルーディスパッチャ
│   └── tools/           # MCPツール
│       ├── health.py    # ヘルスチェック
│       ├── research.py  # 知識検索・要約
│       ├── orchestration.py  # ルーティング・ワークフロー
│       ├── generation.py     # RAG強化コンテンツ生成
│       ├── session.py   # セッション管理
│       ├── project.py   # プロジェクト管理
│       ├── document.py  # スマートドキュメント作成
│       ├── document_maintenance.py  # ドキュメント整合性・クリーンアップ
│       ├── specification.py  # AI駆動仕様策定
│       ├── execution.py  # タスク分解・実行管理
│       ├── task.py       # タスク管理（追加・更新・削除・移動）
│       ├── lifecycle.py  # フェーズ・マイルストーン管理
│       ├── progress.py   # 進捗追跡・予測
│       ├── quality.py    # 品質ゲート管理
│       ├── reporting.py  # レポート・分析
│       ├── smart_read.py # Phanthand連携ファイル読み込み・分析
│       └── chatroom.py   # spirrow-conclair 連携 chatroom 7 ツール
├── adapters/
│   ├── base.py          # Adapter ABC
│   ├── lexora.py        # LLM呼び出し
│   ├── cognilens.py     # 圧縮
│   ├── prismind.py      # RAG検索
│   ├── phanthand.py     # 開発PCファイルアクセス（独立クラス）
│   ├── chatroom.py      # spirrow-conclair (chatroom backend) 連携
│   └── unrealwise.py    # UE操作
└── utils/
    └── logging.py
```

## 開発ルール

### コーディング規約

- 型ヒント必須
- docstring必須（Google style）
- 非同期処理は async/await
- Adapterパターンで外部サービスを抽象化

### 命名規則

- クラス: PascalCase
- 関数/変数: snake_case
- 定数: UPPER_SNAKE_CASE

### テスト

- pytest + pytest-asyncio
- Adapterはモック化してテスト
- `tests/` にミラー構成

## 主要コンポーネント

### 1. TaskQueue (`core/task_queue.py`)

優先度・依存関係を考慮したタスクキュー。

```python
class TaskQueue:
    async def register(tasks: list[Task]) -> list[str]
    async def get_next() -> Task | None
    async def complete(task_id: str, result: str) -> None
    async def fail(task_id: str, error: str) -> None
```

### 2. DependencyGraph (`core/dependency_graph.py`)

タスク間の依存関係をDAGで管理。

```python
class DependencyGraph:
    def add_task(task: Task) -> None
    def get_ready_tasks() -> list[Task]
    def mark_complete(task_id: str) -> None
    def topological_sort() -> list[str]
```

### 3. ContextManager (`core/context_manager.py`)

Cognilens連携でコンテキスト最適化。

```python
class ContextManager:
    async def optimize(context: str, max_tokens: int) -> str
    async def enrich_with_rag(query: str, context: str) -> str
```

### 4. Adapters (`adapters/`)

各サービスへのクライアント。共通インターフェース。

```python
class BaseAdapter(ABC):
    @abstractmethod
    async def health_check() -> bool
    
class LexoraAdapter(BaseAdapter):
    async def generate(prompt: str, **kwargs) -> str

class CognilensAdapter(BaseAdapter):
    async def compress(text: str, ratio: float) -> str

class PrismindAdapter(BaseAdapter):
    async def search(query: str, n: int) -> list[Document]
    async def find_similar_document_type(type_query: str, threshold: float) -> dict

class ChatroomAdapter(BaseAdapter):
    # spirrow-conclair (port 8115) HTTP API のラッパー。
    # conclair の error envelope ({error_type, error, details}) は
    # 4xx/5xx 時にそのまま forward (raise せず dict 返却) されるので、
    # 上位 (MCP ツール) で error_type を見て分岐できる。
    async def open_thread(*, project, thread_id, title, owner, propose_content, ...) -> dict
    async def post_message(*, project, thread_id, type, author, content, ...) -> dict
    async def close_thread(*, project, thread_id, summary_content, author, ...) -> dict
    async def list_threads(*, project, status_filter=None, owner=None, ...) -> dict
    async def get_thread(*, project, thread_id, mode="full") -> dict
    async def list_events(*, project, thread_id=None, action=None, since=None, ...) -> dict
    async def check_integrity(*, project) -> dict
```

## API エンドポイント

```python
# タスク管理
POST /tasks              # タスク登録
GET  /tasks/next         # 次タスク取得
POST /tasks/{id}/complete

# オーケストレーション
POST /orchestrate        # 総合処理
POST /route              # ルーティング判断

# 管理
GET  /health
GET  /stats
```

## MCPツール

MCPサーバ経由で提供されるツール群。`src/magickit/mcp/tools/`に実装。

### セッション管理 (`session.py`)

Claudeセッション間でコンテキストを維持するためのツール。

| ツール | 用途 |
|--------|------|
| `begin_task` | タスク開始時にPrismindからコンテキストを復元 |
| `checkpoint` | 作業中の中間保存、決定事項をknowledgeとして保存 |
| `handoff` | セッション終了と次回への引き継ぎ情報保存 |
| `resume` | `begin_task`のエイリアス（detail_levelプリセット付き） |
| `update_progress` | 進捗更新（phase/task/blockers）軽量版 |
| `list_context_authors` | プロジェクトにコンテキストを保存済みの author 一覧 |
| `upsert_identity` | クロスプロジェクト identity レコード (actor 宣言) の作成/更新 |

**セッション引き継ぎの仕組み:**
- `handoff`で`summary`と`next_action`を保存
- 次回セッションで`begin_task`/`resume`時に`last_summary`と`next_action`を復元
- MCP Memory Serverに状態を永続化

**コンテキスト author（ロール）分割:**
- `checkpoint`/`handoff`/`resume`/`begin_task`/`update_progress` は任意の `author` を受け取り、1つの project+user に対して **author ごとに独立したコンテキスト** を保存・復元できる（保存キー: `prismind:session:{project}:{user}:{author}`）
- `author` 空＝デフォルト（レガシー）コンテキスト。後方互換のため既存のキー形式を維持
- 用途: 複数ロール（例 `claude.ai` / `claude-code`）が同じプロジェクトで別々の引き継ぎを持つ
- `list_context_authors` で保存済み author 一覧を取得し、**表記揺れによる重複を防止**・**自分の author のコンテキスト有無を確認** してから checkpoint/resume すること
- `checkpoint`/`handoff` が抽出する knowledge には `author:{name}` タグが付与される

```python
# 使用例
begin_task(project="trapxtrap", task_description="射撃システム実装")

# 中間保存（進捗情報も更新可能）
checkpoint(
    summary="基本実装完了",
    decisions=["弾丸はプールで管理"],
    current_phase="Phase 2",
    current_task="T01: 射撃システム",
    next_action="ダメージ計算を実装"
)

# 軽量な進捗更新
update_progress(
    current_task="T02: ダメージ計算",
    completed_task="T01: 射撃システム"
)

# セッション終了時の引き継ぎ
handoff(
    next_action="ダメージ計算の実装",
    summary="射撃システムの基本実装完了、弾丸プール方式を採用",
    project="trapxtrap",
    notes="参考: docs/shooting-design.md"
)

# 次回セッション開始
resume(project="trapxtrap", detail_level="standard")
# → last_summary, next_action が復元される

# --- author（ロール）分割の例 ---
# まず保存済み author を確認（表記揺れ防止 / 自分の context 有無確認）
list_context_authors(project="trapxtrap")
# → {"authors": [{"author": "claude.ai", ...}, {"author": "claude-code", ...}], ...}

# 自分のロールで保存
checkpoint(
    summary="設計レビュー完了",
    project="trapxtrap",
    current_task="T05: 設計",
    author="claude.ai"
)
# 同じ author で復元（他 author の context とは独立）
resume(project="trapxtrap", author="claude.ai")
```

**checkpointパラメータ:**
- `summary`: 作業サマリー
- `project`: プロジェクトID
- `decisions`: 決定事項リスト（knowledgeに保存）
- `blockers`: ブロッカーリスト
- `current_phase`: 現在のフェーズ（例: "Phase 2"）
- `current_task`: 現在のタスク（例: "T01: 機能実装"）
- `next_action`: 次にやること
- `author`: コンテキスト author（ロール）分割。空でデフォルト

**handoffパラメータ:**
- `next_action`: 次回セッションへの推奨アクション（必須）
- `project`: プロジェクトID
- `summary`: セッションのサマリー
- `notes`: 追加メモ
- `blockers`: ブロッカーリスト
- `save_insights`: インサイトをknowledgeに保存するか
- `author`: コンテキスト author（ロール）分割。空でデフォルト

**list_context_authorsパラメータ:**
- `project`: 対象プロジェクトID（必須）
- `user`: user フィルタ（空＝プロジェクトの全 user）
- 返り値: `authors`（`author`/`user`/`current_phase`/`current_task`/`updated_at` を含み、更新が新しい順）, `total_count`

**identity 第一級化と role 検証の所在 (ADR-2026-05-27-09 / T-magickit-identity-extension):**

identity (Bohr / Heisenberg / Einstein / human) は `upsert_identity` でクロスプロジェクトに永続化される。schema は `allowed_roles` / `embodiment` / `independence_class` / `persona_description` の 4 軸で、`embodiment` と `independence_class` は upsert 毎に必須・enum 検証 (msg-001 §C-4 「書き忘れ不能」)。

**Magickit は role × allowed_roles 検証の唯一の発火点。Prismind は identity レコードを永続化するのみで role 検証ロジックを持たない。** これは T-magickit-identity-extension msg-002 §2.2 / msg-003 D-2 で確定した設計判断で、サービス境界 (Prismind = persistence、Magickit = orchestration / enforcement) を尊重するための整理。代償として「Magickit を経由しない直接呼び出し (例: 別 client から Prismind を直叩き) では role × allowed_roles チェックが効かない」ことを許容する。AI session は必ず Magickit MCP 経由という前提下で「AI 間のドリフト防止」を実現する形 (UI 直叩きへの効力は現時点要件外、msg-003 D-2)。

将来 Spirrow Platform が Prismind を別 client (例: Thirdy 経由) から呼ぶケースが生じた場合、role 検証の責務配置を再評価する必要がある。アーキテクチャ上の明示的な単独 enforcement point として、本ドキュメントに記録しておく (Einstein F-04 反映)。

実装位置:
- `chatroom_post_message` の `role × allowed_roles` チェック → `src/magickit/mcp/tools/chatroom.py` (P2 実装予定)
- `chatroom_close_thread` の owner/role 段 2 チェック → 同上 (P3 実装予定)

`closeable_roles = {implementer, integrator, proposer}` は msg-003 D-3 / msg-005 で確定 (reviewer / dogfooder は除外、Einstein は `allowed_roles=[naysayer]` で構造的除外)。

### リサーチ (`research.py`)

知識検索と要約を組み合わせたツール。

| ツール | 用途 |
|--------|------|
| `research_and_summarize` | Prismind検索 + Cognilens圧縮 |
| `analyze_documents` | ドキュメント検索 + エッセンス抽出 |

### オーケストレーション (`orchestration.py`)

`orchestrate_workflow`で使用可能なサービス・アクション一覧。

#### Prismind アクション

| アクション | パラメータ | 説明 |
|-----------|-----------|------|
| `search` | query, category, project, tags, limit | knowledge検索 |
| `add` / `store` | content, category, project, tags, source | knowledge追加 |
| `get_document` | query, doc_id, doc_type | ドキュメント取得 |
| `get_progress` | project | プロジェクト進捗取得 |
| `add_task` | project, description, priority, category | タスク追加 |
| `get_task` | task_id, phase, project | 単一タスク取得 |
| `delete_task` | task_id, phase, project | タスク削除（blocked_by参照自動クリーンアップ） |
| `update_task` | task_id, phase, name, description, status, priority, category, blocked_by, blockers, new_phase, project | タスク包括更新（フェーズ移動対応） |
| `complete_task` | project, task_id, notes | タスク完了（→ update_task_status） |
| `start_task` | project, task_id, notes | タスク開始（→ update_task_status） |
| `block_task` | project, task_id, reason | タスクブロック（→ update_task_status） |
| `update_task_status` | project, task_id, status, notes | タスクステータス更新 |
| `setup_project` | project, name, description, phases, categories | プロジェクト初期化 |
| `list_projects` | include_archived | プロジェクト一覧 |
| `update_project` | project, ... | プロジェクト更新 |
| `delete_project` | project, confirm | プロジェクト削除 |
| `get_project_config` | project | プロジェクト設定取得 |
| `update_summary` | description, current_phase, completed_tasks, total_tasks, custom_fields | サマリー更新 |
| `create_document` | doc_type, name, content, phase_task, feature, keywords, auto_register_type | ドキュメント作成（未知のdoc_typeは自動登録） |
| `update_document` | doc_id, content, name, feature, keywords | ドキュメント更新 |
| `delete_document` | doc_id, project, delete_drive_file, permanent | ドキュメント削除（permanent=falseでゴミ箱移動） |
| `list_document_types` | - | ドキュメントタイプ一覧（グローバル+プロジェクト） |
| `register_document_type` | type_id, name, folder_name, scope, description | ドキュメントタイプ登録（scope: "global"/"project"） |
| `delete_document_type` | type_id, scope | ドキュメントタイプ削除（scope: "global"/"project"） |
| `find_similar_document_type` | type_query, threshold | RAGセマンティック検索で類似タイプを検索（多言語対応） |

#### Cognilens アクション

| アクション | パラメータ | 説明 |
|-----------|-----------|------|
| `compress` | text, ratio, preserve | テキスト圧縮 |
| `summarize` | text, style, max_tokens | 要約生成 |
| `extract_essence` | document, focus_areas | エッセンス抽出 |
| `optimize` | context, task_description, target_tokens | コンテキスト最適化 |

#### Lexora アクション

| アクション | パラメータ | 説明 |
|-----------|-----------|------|
| `generate` | prompt, max_tokens, temperature | テキスト生成 |
| `chat` | messages, max_tokens, temperature | チャット |

### プロジェクト管理 (`project.py`)

プロジェクトのライフサイクル管理ツール。プロジェクトUID（`project_uid`）を自動生成し、タスク-ドキュメント紐付けに使用。

| ツール | 用途 |
|--------|------|
| `list_projects` | プロジェクト一覧取得（アーカイブ含む/除外） |
| `init_project` | テンプレートからプロジェクト初期化（project_uid自動生成） |
| `get_project_status` | プロジェクトの詳細ステータス取得 |
| `clone_project` | 既存プロジェクトを複製 |
| `delete_project` | アーカイブ/エクスポート+削除/完全削除 |
| `restore_project` | アーカイブからの復元 |

```python
# 使用例
result = init_project(project="my-game", template="game", name="My Game")
# -> {"success": true, "project_uid": "1AbC2dEf3GhI", ...}
# project_uid はGoogle DriveフォルダIDで、UTIDの生成に使用される

get_project_status(project="my-game")
delete_project(project="old-project", mode="archive")
```

**project_uid:**
- `init_project`で自動生成されるGoogle DriveフォルダID
- グローバルに一意な値
- UTID（Unique Task ID）の生成に使用
- タスクとドキュメントの紐付けに必須

**テンプレート種類:**
- `game`: ゲーム開発（design, implementation, asset, bug, decision）
- `mcp-server`: MCPサーバ開発（architecture, tool, adapter, config）
- `web-app`: Webアプリ（frontend, backend, api, design）

### タスク管理 (`task.py`)

プロジェクトタスクの包括的な管理ツール。UTID（一意タスクID）、ファイル添付、依存関係の検証、knowledge連携、影響分析を含む。

| ツール | 用途 |
|--------|------|
| `add_task` | タスク追加（ID自動生成、UTID生成、ファイル添付、重複検出、依存関係検証） |
| `list_tasks` | タスク一覧（フィルタリング、スマートソート、推奨タスク） |
| `get_task` | 単一タスク詳細取得（関連knowledge含む） |
| `start_task` | タスク開始（依存関係チェック、コンテキスト取得、添付ファイル更新検出） |
| `complete_task` | タスク完了（learnings記録、アンブロック検出） |
| `block_task` | タスクブロック（影響分析、カスケード効果） |
| `delete_task` | タスク削除（依存関係クリーンアップ） |
| `update_task` | タスク更新（全フィールド対応、フェーズ移動） |
| `move_task_to_phase` | フェーズ間移動のショートカット |
| `set_task_priority` | 優先度設定のショートカット |
| `set_task_blockers` | 依存関係設定のショートカット |

**UTID（Unique Task ID）:**

タスクをグローバルに一意に識別するID。プロジェクトUID（Google DriveフォルダID）、フェーズ、ローカルタスクIDを組み合わせる。

```
形式: {project_uid}:{phase_slug}:{local_task_id}
例:   1AbC2dEf3GhI:phase2:T01
```

UTIDにより、異なるプロジェクト間でもタスクを一意に識別でき、タスクとドキュメントの紐付けに使用される。

```python
# 使用例: タスク追加（ID自動生成、UTID生成）
add_task(
    name="射撃システム実装",
    description="プレイヤーの射撃機能を実装",
    phase="Phase 2",
    priority="high",
    category="feature",
    blocked_by=["T01"],  # 依存タスク
    project="my-game"
)
# -> {"task_id": "T02", "utid": "1AbC2dEf:phase2:T02", ...}

# 使用例: ファイル添付付きタスク追加
add_task(
    name="射撃システム実装",
    description="プレイヤーの射撃機能を実装",
    attach_files=["src/shooting.cpp", "docs/shooting-design.md"],  # ファイル添付
    attach_docs=["doc-12345"],  # 既存ドキュメントリンク
    project="my-game"
)
# -> {
#   "task_id": "T02",
#   "utid": "1AbC2dEf:phase2:T02",
#   "attached_files": [
#     {"file_path": "/path/to/src/shooting.cpp", "file_hash": "abc123...", "doc_id": "doc-new-1"}
#   ],
#   "linked_docs": ["doc-12345"],
#   ...
# }

# 使用例: タスク一覧取得（フィルタリング）
list_tasks(
    phase="Phase 2",
    status="not_started",
    priority="high",
    project="my-game"
)
# -> {"tasks": [...], "recommended": {...}, "stats": {...}}

# 使用例: タスク開始（添付ファイル更新検出）
start_task(
    task_id="T01",
    project="my-game",
    refresh_attachments=True  # 添付ファイルの変更を検出・更新
)
# -> {
#   "task": {...},
#   "context": {...},
#   "attachment_status": {
#     "updated": [{"file_name": "shooting.cpp", "old_hash": "abc...", "new_hash": "xyz..."}],
#     "unchanged": [{"file_name": "design.md"}],
#     "deleted": []
#   }
# }

# 使用例: タスク取得
get_task(task_id="T01", include_related_knowledge=True)
# -> {"task": {...}, "related_knowledge": [...]}

# 使用例: タスク更新（名前・優先度変更）
update_task(
    task_id="T01",
    name="New Name",
    priority="high",
    project="my-game"
)

# 使用例: フェーズ移動
move_task_to_phase(
    task_id="T01",
    from_phase="Phase 1",
    to_phase="Phase 2"
)

# 使用例: タスク削除（依存タスクの参照を自動クリーンアップ）
delete_task(
    task_id="T01",
    cascade_unblock=True,  # blocked_by参照を自動削除
    project="my-game"
)
# -> {"dependent_tasks_updated": ["T02", "T03"]}
```

**ファイル添付機能:**

`attach_files`パラメータでソースコードや設計ドキュメントをタスクに紐付け。

処理フロー:
1. ファイルのバリデーション（存在確認、サイズ上限100KB、秘匿ファイル除外）
2. SHA256ハッシュ計算
3. Cognilensで要約生成
4. smart_create_documentでドキュメント作成（phase_task=UTID）
5. knowledgeにメタデータ保存

除外される秘匿ファイル:
- `.env`, `.env.*`, `credentials*.json`, `secret*`
- `*.key`, `*.pem`, `*.p12`, `*.pfx`
- `.git/`, `__pycache__/`, `node_modules/`, `.venv/`

**添付ファイルリフレッシュ:**

`start_task`で`refresh_attachments=True`（デフォルト）を指定すると:
- 添付ファイルのハッシュを比較
- 変更があればCognilensで再要約
- 削除されたファイルは警告
- `attachment_status`で変更内容を報告

**スマートソート:**
- ブロックされていないタスク優先
- 依存タスクがないタスク優先
- 優先度順（high → medium → low）

**推奨タスク検出:**
- ステータスが`not_started`
- 全ての依存タスク（blocked_by）が`completed`
- 最も高い優先度

### ライフサイクル管理 (`lifecycle.py`)

ゲーム開発プロジェクトのフェーズ遷移とマイルストーン管理。

| ツール | 用途 |
|--------|------|
| `advance_phase` | 次フェーズへ進行（完了条件チェック付き） |
| `set_phase` | フェーズを手動設定 |
| `get_phase_status` | フェーズの詳細状況取得 |
| `add_milestone` | マイルストーン追加（Alpha, Beta, Release等） |
| `update_milestone` | マイルストーン更新（日付、ステータス） |
| `list_milestones` | マイルストーン一覧取得 |
| `check_milestone_status` | 達成状況確認、遅延警告 |

```python
# 使用例: フェーズ管理
# フェーズ進行（完了率80%以上が必要）
advance_phase(project="my-game", completion_threshold=80.0)
# -> {"success": true, "previous_phase": "pre-production", "current_phase": "production"}

# フェーズ強制進行
advance_phase(project="my-game", force=True)

# フェーズ状態確認
get_phase_status(project="my-game", phase="production")
# -> {"phase": "production", "stats": {"total": 10, "completed": 7}, "blockers": [...]}

# 使用例: マイルストーン管理
add_milestone(
    project="my-game",
    name="Alpha",
    target_date="2024-03-01",
    phase="production",
    description="Initial playable version"
)

update_milestone(
    project="my-game",
    name="Alpha",
    status="completed",
    actual_date="2024-03-05"
)

# マイルストーン遅延チェック
check_milestone_status(project="my-game")
# -> {"at_risk": [...], "overdue": [...], "on_track": 2}
```

**advance_phaseパラメータ:**
- `project`: プロジェクトID（必須）
- `force`: 完了条件を無視して進行
- `completion_threshold`: 最低完了率（デフォルト80%）

**マイルストーンステータス:**
- `pending`: 未開始
- `in_progress`: 進行中
- `completed`: 完了
- `delayed`: 遅延

### 進捗追跡 (`progress.py`)

バーンダウンチャート、完了予測、リスク分析。

| ツール | 用途 |
|--------|------|
| `get_burndown` | バーンダウンチャートデータ取得 |
| `estimate_completion` | 完了予測日計算（ベロシティベース） |
| `track_velocity` | タスク完了速度の追跡・記録 |
| `get_risk_indicators` | リスク指標（遅延警告、ブロッカー数等） |

```python
# バーンダウンデータ取得
get_burndown(project="my-game", phase="production", days=14)
# -> {
#   "data_points": [{"date": "2024-01-15", "remaining": 25, "completed_today": 3}, ...],
#   "current_velocity": 2.5,
#   "ideal_burndown": [...]
# }

# 完了予測
estimate_completion(project="my-game")
# -> {
#   "estimated_date": "2024-03-15",
#   "days_remaining": 45,
#   "current_velocity": 2.1,
#   "confidence": "high",
#   "factors": ["Based on 7+ days of data"]
# }

# 毎日のベロシティ記録（作業終了時に呼び出し）
track_velocity(project="my-game", completed_today=3, notes="集中できた日")
# -> {"rolling_average": 2.8, "remaining_tasks": 22}

# リスク分析
get_risk_indicators(project="my-game")
# -> {
#   "overall_risk": "medium",
#   "risk_score": 35,
#   "indicators": [
#     {"type": "blocked_tasks", "severity": "medium", "value": "2/20"},
#     {"type": "velocity_stable", "severity": "low", "value": "2.5/day"}
#   ],
#   "recommendations": ["Address blocked tasks before they become critical"]
# }
```

**リスクレベル:**
- `low` (0-19): 健全な状態
- `medium` (20-39): 注意が必要
- `high` (40-59): 対策が必要
- `critical` (60+): 即座の対応が必要

**リスク指標:**
- ブロックされたタスク比率
- ベロシティトレンド（前週比）
- マイルストーン遅延
- フェーズ完了状況

### 品質ゲート (`quality.py`)

フェーズ遷移前の品質チェック条件を定義・評価。

| ツール | 用途 |
|--------|------|
| `define_quality_gate` | フェーズ完了条件の定義 |
| `check_quality_gate` | 条件達成チェック |
| `list_quality_gates` | 定義済みゲート一覧 |

```python
# 品質ゲート定義
define_quality_gate(
    project="my-game",
    phase="production",
    name="Production Ready Gate",
    criteria=[
        {"type": "task_completion", "threshold": 90, "description": "90% tasks completed"},
        {"type": "no_blockers", "description": "No blocked tasks"},
        {"type": "milestone_achieved", "milestone": "Alpha", "description": "Alpha milestone achieved"}
    ]
)

# 品質ゲートチェック
check_quality_gate(project="my-game", phase="production")
# -> {
#   "passed": false,
#   "results": [
#     {"type": "task_completion", "passed": true, "details": "92% complete (threshold: 90%)"},
#     {"type": "no_blockers", "passed": false, "details": "2 blocked task(s)"},
#     {"type": "milestone_achieved", "passed": true, "details": "Milestone status: completed"}
#   ],
#   "passed_count": 2,
#   "failed_count": 1
# }
```

**条件タイプ:**
- `task_completion`: タスク完了率（threshold指定）
- `no_blockers`: ブロックされたタスクがない
- `no_critical_blockers`: 高優先度のブロッカーがない
- `all_bugs_resolved`: バグカテゴリのタスクが全て完了
- `milestone_achieved`: 指定マイルストーンが完了
- `custom`: 手動確認が必要なカスタム条件

**デフォルトゲート（未定義時）:**
- pre-production: 80%完了、クリティカルブロッカーなし
- production: 90%完了、ブロッカーなし
- polish: 95%完了、バグ解決、ブロッカーなし
- release: 100%完了、ブロッカーなし、Releaseマイルストーン達成

### レポート・分析 (`reporting.py`)

ステータスレポート、リリースノート、振り返り分析。

| ツール | 用途 |
|--------|------|
| `generate_status_report` | ステークホルダー向けレポート |
| `generate_release_notes` | リリースノート自動生成 |
| `analyze_project_performance` | 振り返り分析、教訓抽出 |

```python
# ステータスレポート生成
generate_status_report(
    project="my-game",
    format="markdown",  # "markdown" / "text" / "json"
    include_tasks=True,
    include_milestones=True,
    include_risks=True
)
# -> {
#   "report": "# Project Status Report: my-game\n\n## Overview\n- **Current Phase:** production\n...",
#   "metrics": {"total_tasks": 50, "completed_tasks": 35, "completion_percent": 70.0}
# }

# リリースノート生成
generate_release_notes(
    project="my-game",
    version="v1.0.0",
    from_phase="production"  # このフェーズ以降の完了タスクを含む
)
# -> {
#   "release_notes": "# Release Notes - v1.0.0\n\n## New Features\n- **射撃システム**\n...",
#   "features_count": 5,
#   "fixes_count": 3
# }

# 振り返り分析
analyze_project_performance(project="my-game", use_llm=True)
# -> {
#   "insights": [
#     "Velocity improved significantly over time",
#     "5 blockers were recorded during the project",
#     "Most common task category: implementation (25 tasks)"
#   ],
#   "metrics": {"completion_rate": 85.0, "average_velocity": 2.5},
#   "recommendations": ["Implement better blocker prevention"],
#   "narrative": "The project showed strong progress..."  # LLM生成
# }
```

**レポートフォーマット:**
- `markdown`: GitHub-flavored Markdown（推奨）
- `text`: プレーンテキスト
- `json`: 構造化データ

**リリースノートのカテゴリ分類:**
- `feature` / `implementation` → New Features
- `bug` → Bug Fixes
- `refactor` / `improvement` / `polish` → Improvements
- その他 → Other Changes

### スマートファイル読み込み・分析 (`smart_read.py`)

Phanthand（開発PCファイルアクセスAPI）とCognilens/Lexoraを組み合わせたツール。
ファイル内容はPhanthand→Cognilensで処理され、Claudeのコンテキストには圧縮結果のみが載る。

**前提**: 開発PCでPhanthandが稼働していること（https://github.com/SpirrowGames/spirrow-phanthand）

| ツール | 用途 |
|--------|------|
| `smart_read` | 開発PCのファイルをCognilensで処理して読み込み（コンテキスト節約） |
| `smart_analyze` | 複数ファイルを横断分析し、質問に回答 |

**PhanthandAdapter の特徴:**
- BaseAdapterを継承しない独立クラス（`adapters/phanthand.py`）
- 接続先は開発者ごとに異なるため、`phanthand_url`/`phanthand_api_key`はツール呼び出し時に指定
- Magickit側に設定ファイルの変更は不要

```python
# smart_read: ファイル単位の読み込み+Cognilens処理
smart_read(
    files=["D:/Projects/my-app/src/auth.py", "D:/Projects/my-app/src/middleware.py"],
    mode="essence",           # raw / summarize / essence / compress
    focus="認証フロー",        # essence/compressで注目ポイント指定
    phanthand_url="http://192.168.1.10:7300",
    phanthand_api_key="your-secret-key",
    project="my-project"
)
# -> {
#   "success": true,
#   "mode": "essence",
#   "results": [
#     {"file": "D:/.../auth.py", "size": 15234, "processed": "...Cognilens処理結果...", "mode": "essence"},
#     {"file": "D:/.../middleware.py", "size": 8421, "processed": "...Cognilens処理結果...", "mode": "essence"}
#   ],
#   "file_count": 2,
#   "errors": []
# }

# smart_analyze: 複数ファイルを横断分析
smart_analyze(
    files=["src/api/*.py"],    # globパターン対応
    question="エラーハンドリングのパターンは？",
    phanthand_url="http://192.168.1.10:7300",
    phanthand_api_key="your-secret-key",
    search_root="D:/Projects/my-app",  # glob展開のルートディレクトリ
    max_files=20,              # 最大ファイル数（コスト制御）
    save_to_knowledge=True,    # 分析結果をPrismindに保存
    project="my-project"
)
# -> {
#   "success": true,
#   "question": "エラーハンドリングのパターンは？",
#   "answer": "...Lexoraによる分析回答...",
#   "files_analyzed": ["src/api/auth.py", "src/api/users.py", "src/api/errors.py"],
#   "file_count": 3,
#   "summary": "...Cognilens統合要約...",
#   "knowledge_saved": true,
#   "errors": []
# }
```

**smart_read 処理モード:**

| モード | Cognilens機能 | ユースケース | focus使用 |
|--------|--------------|-------------|-----------|
| `raw` | なし | 小さいファイルをそのまま読む | No |
| `summarize` | summarize | 概要把握 | No |
| `essence` | extract_essence | 設計パターン・API構造の抽出 | Yes |
| `compress` | compress | コンテキスト節約のための圧縮 | Yes |

**smart_analyze 処理フロー:**
```
1. ファイルリスト解決（globパターンはPhanthand searchで展開）
2. 各ファイルをPhanthand経由で読み込み
3. Cognilens unify_summaries で統合要約
4. Lexora で質問に回答
5. オプション: Prismindにknowledgeとして保存
```

**エラーハンドリング:**
- Phanthand接続エラー → 即座に全体停止
- ファイル単位のエラー（未存在、パス不許可等）→ スキップして他ファイルは継続
- Lexora失敗 → Cognilens要約だけ返却（部分成功）

## ゲーム開発ワークフロー例

完全なプロジェクトライフサイクルの例：

```python
# 1. プロジェクト立ち上げ
init_project(project="my-game", template="game")
add_milestone(project="my-game", name="Alpha", target_date="2024-03-01", phase="production")
add_milestone(project="my-game", name="Beta", target_date="2024-05-01", phase="polish")
add_milestone(project="my-game", name="Release", target_date="2024-06-15", phase="release")

# 2. 品質ゲート設定
define_quality_gate(
    project="my-game",
    phase="pre-production",
    criteria=[
        {"type": "task_completion", "threshold": 80},
        {"type": "no_critical_blockers"}
    ]
)

# 3. 日々の作業
begin_task(project="my-game", task_description="射撃システム実装")
# ... 作業 ...
track_velocity(project="my-game", completed_today=3)
checkpoint(summary="射撃システム基本実装完了", project="my-game")

# 4. 進捗確認
get_burndown(project="my-game", phase="production")
estimate_completion(project="my-game")
get_risk_indicators(project="my-game")

# 5. フェーズ遷移
check_quality_gate(project="my-game", phase="pre-production")
advance_phase(project="my-game")  # pre-production → production

# 6. マイルストーン確認
check_milestone_status(project="my-game")

# 7. レポート
generate_status_report(project="my-game", format="markdown")
generate_release_notes(project="my-game", version="v0.1.0-alpha")

# 8. 完了・振り返り
analyze_project_performance(project="my-game", use_llm=True)
delete_project(project="my-game", mode="archive")
```

### chatroom 連携 (`chatroom.py`)

AI 間 chatroom (議論 / handoff / decision) の永続化を spirrow-conclair (FastAPI + PostgreSQL) に委譲する MCP ツール群。conclair の HTTP API を `ChatroomAdapter` 経由でそのまま wrap し、conclair の error envelope (`error_type` / `error` / `details`) はツール戻り値に passthrough される。

| ツール | 用途 |
|--------|------|
| `chatroom_open_thread` | 新規 thread を立てる (propose msg を 1 transaction で同時 INSERT) |
| `chatroom_post_message` | thread に msg を追加 (status 自動遷移: handoff→awaiting_reply / ack→active / decide+closes_thread→resolved) |
| `chatroom_close_thread` | thread を resolved 化 (owner only、shortcut for decide+closes_thread) |
| `chatroom_list_threads` | thread 一覧 (status / owner filter, pagination) |
| `chatroom_get_thread` | thread + msgs (mode=full|summary、resolved & summary なら decide msg のみ) |
| `chatroom_list_events` | audit log (action / thread_id / since/until filter) |
| `chatroom_check_integrity` | invariant audit report (常に 200) |

```python
# 使用例: thread を立てて議論を開始
chatroom_open_thread(
    project="spirrow-voxelworld",
    thread_id="T-D4-radius",
    title="radius 値の検討",
    owner="claude.ai",
    propose_content="radius を 5 にする案を検討したい",
    tags=["design"]
)
# → {"thread": {...status='active'...}, "msg": {...msg_id='msg-001'...}}

# 使用例: handoff -> awaiting_reply
chatroom_post_message(
    project="spirrow-voxelworld",
    thread_id="T-D4-radius",
    type="handoff",
    author="claude.ai",
    content="claude-code に実装を依頼"
)
# → {"msg": {...}, "thread_status_changed_to": "awaiting_reply"}

# 使用例: ack で active に戻す
chatroom_post_message(
    project="spirrow-voxelworld", thread_id="T-D4-radius",
    type="ack", author="claude-code", content="着手します"
)

# 使用例: thread を close (owner のみ)
chatroom_close_thread(
    project="spirrow-voxelworld",
    thread_id="T-D4-radius",
    summary_content="## Resolution\n\n結論: radius=5 採用",
    author="claude.ai",
    affects_threads=["T-D5-vocabulary"]
)
# → {"thread": {...status='resolved'...}, "decide_msg": {...}}

# 使用例: session 開始時の context 復元
chatroom_list_threads(
    project="spirrow-voxelworld",
    status_filter=["active", "awaiting_reply"],
    owner="claude.ai"
)
chatroom_list_threads(
    project="spirrow-voxelworld",
    status_filter=["awaiting_reply"]  # 自分宛の handoff 待ち
)

# 使用例: resolved thread を最小 context で読む
chatroom_get_thread(
    project="spirrow-voxelworld",
    thread_id="T-D4-radius",
    mode="summary"  # decide msg だけ返却 (token 節約)
)
```

**重要な動作:**
- 全 write 系ツールは conclair 側で 1 transaction で完結 (msg INSERT + thread UPDATE + event INSERT を atomic)
- msg_id 採番は conclair 側で `pg_advisory_xact_lock(hashtext(project))` により直列化、衝突なし
- error envelope は `success: bool` 形式ではなく **`error_type` 文字列の有無**で判定: `if "error_type" in result: ... else: ... `
- close は thread.owner と一致する author のみ実行可能 (それ以外は `error_type=ChatroomPermissionError`)
- summary mode は resolved 時のみ filter 効果あり (active/awaiting_reply では full と同じ)

**msg type 一覧** (chatroom 仕様より):
| type | 用途 | status 遷移 |
|---|---|---|
| `propose` | 議論の起点 (thread 開設時のみ) | (open_thread が処理) |
| `question` / `answer` | 確認 / 回答 | なし |
| `report` | 進捗・結果報告 | なし |
| `handoff` | 相手 AI に作業を渡す | active → awaiting_reply |
| `ack` | handoff を受領 | awaiting_reply → active |
| `decide` | 結論 (closes_thread と組で thread close) | (closed thread に投げると `ChatroomStateError`) |

詳細は spirrow-conclair の `docs/api-design.md` および `docs/usage-cheatsheet.md`、設計判断は magickit project の `chatroom-archive-tool: System Design v2`。

### ドキュメント管理 (`document.py`)

未登録のドキュメントタイプを自動処理するスマートドキュメント作成。
RAGベースのセマンティック検索（BGE-M3埋め込み）で多言語マッチングをサポート。

| ツール | 用途 |
|--------|------|
| `smart_create_document` | 未知のdoc_typeをRAGセマンティック検索で自動マッチ・登録してドキュメント作成 |
| `smart_update_document` | ドキュメント更新（content置換/append、doc_type自動解決）。native Google Docs と text/markdown・text/plain の両方に対応 |

**mimeType 対応（更新時）:**
- 実体の更新は Prismind 側で対象ファイルの mimeType を見て分岐する。native Google Docs (`application/vnd.google-apps.document`) は Docs API、`text/markdown` / `text/plain` 等は Drive media upload（`files.update`、**doc_id 維持**）で全文置換する。
- そのため markdown ファイルでも `smart_update_document` で in-place 更新でき、ADR を markdown で Drive に置くワークフローでも doc_id 参照（decide msg / cross-project link）が壊れない。
- `append=True` の場合、非ネイティブファイルは既存内容をダウンロードして連結してから再アップロードする。
- Sheets/Slides 等の他の Google ネイティブ型はテキスト更新非対応として partial write せず明示エラーを返す。

```python
# 使用例: 未登録のdoc_typeでもRAGセマンティック検索でマッチ→Prismindに登録→作成
smart_create_document(
    name="2024-01-15 Sprint Planning",
    doc_type="api仕様",  # 多言語対応: "api_spec"にマッチ
    content="...",
    phase_task="phase1-task2",
    project="trapxtrap"
)
```

**処理フロー:**
1. Prismindで既存doc_type一覧を取得（グローバル+プロジェクト）
2. 未登録の場合、RAGセマンティック検索で類似タイプを検索（閾値0.45）
3. 類似タイプがあれば既存タイプを使用（例: "api仕様" ≈ "api_spec"、多言語対応）
4. 類似タイプがなければLexoraでメタデータ生成 → グローバルとして登録（フォルダ名は英語のみ）
5. ドキュメントを作成

**セマンティックマッチング:**
- BGE-M3埋め込みによる多言語対応（日本語 ↔ 英語も可）
- 閾値: 0.45（BGE-M3 のセマンティックマッチは概ね 0.5〜0.7 帯に出るためデフォルトは緩め。設定可能）
- 例: "api仕様" → "api_spec", "設計ドキュメント" → "design"

**レスポンス:**
- `matched_existing: true` - 既存タイプにRAGセマンティックマッチ
- `type_registered: true` - 新規グローバルタイプを登録

**DocumentType スコープ:**
- `global`: 全プロジェクトで共有（~/.prismind_global_doc_types.json に保存）
- `project`: 特定プロジェクトのみ（ProjectConfig.document_types に保存）
- 同じtype_idが両方に存在する場合、プロジェクト側が優先される

### ドキュメントメンテナンス (`document_maintenance.py`)

ドキュメント・knowledge・ドキュメントタイプの整合性管理とクリーンアップツール。

| ツール | 用途 |
|--------|------|
| `smart_delete_document` | ドキュメントと関連knowledgeを一括削除（dry_run対応） |
| `detect_orphan_documents` | 孤児ドキュメント検出（削除済みプロジェクト、無効phase_task、未登録doc_type） |
| `detect_orphan_knowledge` | 孤児knowledge検出（無効なドキュメント・タスク参照） |
| `detect_unused_document_types` | 未使用・重複ドキュメントタイプ検出 |
| `check_document_consistency` | 包括的な整合性チェック（全検出ツールを実行） |
| `cleanup_documents` | バッチクリーンアップ（dry_run + confirm必須） |

```python
# 使用例: ドキュメント削除（関連knowledge含む）
smart_delete_document(
    doc_id="doc-12345",
    project="my-project",
    delete_related_knowledge=True,
    dry_run=True  # まずプレビュー
)
# -> {"would_delete": {"document": "doc-12345", "knowledge_entries": [...]}}

# 使用例: 孤児ドキュメント検出
detect_orphan_documents(project="my-project")
# -> {
#   "orphans": [
#     {"doc_id": "xxx", "reasons": ["deleted_project"]},
#     {"doc_id": "yyy", "reasons": ["invalid_phase_task", "missing_doc_type"]}
#   ],
#   "total_orphans": 2
# }

# 使用例: 包括的整合性チェック
check_document_consistency(project="my-project")
# -> {
#   "summary": {
#     "orphan_documents": 3,
#     "orphan_knowledge": 5,
#     "unused_document_types": 2,
#     "semantic_duplicate_types": 1,
#     "total_issues": 11
#   }
# }

# 使用例: クリーンアップ実行
cleanup_documents(
    cleanup_orphan_documents=True,
    cleanup_orphan_knowledge=True,
    project="my-project",
    dry_run=True  # まずプレビュー
)
# -> {"deleted": {"documents": [...], "knowledge": [...]}, "dry_run": true}

# 実際に削除
cleanup_documents(
    cleanup_orphan_documents=True,
    confirm=True,  # 安全確認必須
    dry_run=False
)
```

**安全機能:**
- `dry_run=True`: デフォルトでプレビューモード
- `confirm=True`: 実削除には明示的な確認が必要
- `permanent=False`: ドキュメントはゴミ箱移動（デフォルト）

**孤児検出の種類:**
- `deleted_project`: 削除/アーカイブ済みプロジェクトのドキュメント
- `invalid_phase_task`: 存在しないphase_taskを参照
- `missing_doc_type`: 未登録のドキュメントタイプを使用
- `invalid_document_ref`: 存在しないドキュメントを参照するknowledge
- `invalid_task_ref`: 存在しないタスクを参照するknowledge

**セマンティック重複検出:**
- RAGベースで類似度0.75以上のドキュメントタイプペアを検出
- 例: "api_spec" と "api_specification" が重複候補として検出

### 仕様策定 (`specification.py`)

AI駆動の仕様策定と自動実行準備ツール。曖昧な要望から質問を生成し、回答を元に仕様書を作成、実行権限を準備。

| ツール | 用途 |
|--------|------|
| `start_specification` | 仕様策定を開始、LLMが動的に質問を生成 |
| `generate_specification` | 回答から仕様書を生成、必要な権限リストも出力 |
| `prepare_execution` | 仕様書から必要な権限を分析、allowedPrompts形式に変換 |
| `apply_permissions` | 権限を設定ファイル形式で出力（session/project スコープ） |

```python
# 使用例: 仕様策定→自動実行フロー
# Step 1: 質問を生成
result = start_specification(
    target="src/api/cache.py",
    initial_request="APIレスポンスにキャッシュを追加したい",
    feature_type="cache"  # オプション: テンプレート検索用
)
# -> {"session_id": "spec-abc12345", "questions": [...], "status": "questions_ready"}

# Step 2: Claudeが AskUserQuestion で質問を提示

# Step 3: 回答から仕様書を生成
spec = generate_specification(
    session_id="spec-abc12345",
    answers={"cache_type": "memory", "ttl": "300", "invalidation": "on_update"}
)
# -> {"success": true, "specification": {...}, "required_permissions": {...}}

# Step 4: 実行権限を準備
exec_info = prepare_execution(specification=spec)
# -> {"allowed_prompts": [{"tool": "Bash", "prompt": "edit src/api/cache.py"}, ...]}

# Step 5: 権限適用設定を生成
config = apply_permissions(
    allowed_prompts=exec_info["allowed_prompts"],
    scope="session"  # または "project" で永続化
)
# -> {"apply_method": "exit_plan_mode", "config": {"allowedPrompts": [...]}}

# Step 6: ExitPlanModeで権限を要求して実装開始
```

**処理フロー:**
1. `start_specification`: 要望を分析 → LLMが3-5個の質問を動的生成
2. Claudeが `AskUserQuestion` で質問を提示
3. `generate_specification`: 回答を元に仕様書を生成
4. `prepare_execution`: 仕様書から権限を抽出・変換
5. `apply_permissions`: 適用方法に応じた設定を生成
6. `ExitPlanMode`: 権限を一括承認して実装開始

**出力される仕様書:**
- `title`: 機能名
- `purpose`: 目的
- `target_files`: 変更対象ファイル
- `requirements`: 実装要件（箇条書き）
- `constraints`: 制約・注意点
- `test_points`: テスト観点

**権限リスト出力:**
- `edit`: 編集が必要なファイルパス
- `bash`: 実行が必要なコマンドパターン

**権限スコープ:**
- `session`: 現在のプラン実行中のみ有効（ExitPlanMode経由）
- `project`: プロジェクト設定に永続化（.claude/settings.local.json）

### SpecExecutor - 実行パイプライン (`execution.py`)

仕様書をタスクに分解し、依存関係を考慮した実行順序を管理するパイプライン。

| ツール | 用途 |
|--------|------|
| `spec_executor_decompose` | 仕様書をLLMで分析し、実行可能なタスクリストに分解 |
| `spec_executor_next_task` | 依存関係を考慮して次の実行可能タスクを取得 |
| `spec_executor_complete_task` | タスクを完了/失敗としてマーク、次タスクを取得 |
| `spec_executor_status` | 実行セッション全体の進捗状況を取得 |
| `spec_executor_finalize` | 実行完了処理、結果をknowledgeに保存、ハンドオフ情報生成 |
| `spec_executor_report` | 実行レポート生成（markdown/changelog/brief形式） |
| `spec_executor_run` | 仕様策定→実行準備を一括実行（便利ツール） |

```python
# 使用例: タスク分解と実行ループ
# Step 1: 仕様書をタスクに分解
result = spec_executor_decompose(
    specification=spec,  # generate_specificationの出力
    granularity="medium"  # "fine" / "medium" / "coarse"
)
# -> {"execution_id": "exec-abc123", "tasks": [...], "task_count": 5}

# Step 2: タスクを順番に実行
while True:
    task_info = spec_executor_next_task(execution_id="exec-abc123")
    if not task_info["has_task"]:
        break

    # タスクを実行（Claudeが実際のコード変更を行う）
    task = task_info["task"]
    # ... 実装 ...

    # Step 3: タスク完了を記録
    result = spec_executor_complete_task(
        execution_id="exec-abc123",
        task_id=task["id"],
        success=True,
        result="Implemented caching in api.py"
    )
    # -> {"next_task": {...}, "progress": "2/5", "is_complete": False}

# Step 4: 進捗確認
status = spec_executor_status(execution_id="exec-abc123")
# -> {"progress": {"completed": 5, "total": 5, "percent": 100.0}}
```

**タスク分解の粒度:**
- `fine`: 細かく分割（各関数レベル、複雑な変更向け）
- `medium`: バランス良く分割（デフォルト）
- `coarse`: 大きく分割（シンプルな変更向け）

**タスクの状態:**
- `pending`: 実行待ち
- `in_progress`: 実行中
- `completed`: 完了
- `failed`: 失敗

**依存関係管理:**
- タスクは`dependencies`配列で依存先を指定
- 依存タスクが完了するまで次タスクはブロック
- 依存関係はLLMが仕様書から自動推論

**実行完了後の処理:**

```python
# 実行完了後の処理
result = spec_executor_finalize(
    execution_id="exec-abc123",
    project="my-project",
    save_to_knowledge=True  # 結果をPrismindに保存
)
# -> {"summary": "...", "knowledge_saved": 3, "handoff": {...}}

# レポート生成（ドキュメント用）
report = spec_executor_report(
    execution_id="exec-abc123",
    format="changelog"  # "markdown" / "changelog" / "brief"
)
# -> {"report": "## [Add Caching] - 2024-01-15\n### Added\n- ..."}

# ワンショット実行（仕様策定→実行準備を一括）
workflow = spec_executor_run(
    target="src/api.py",
    request="キャッシュを追加したい",
    project="my-project",
    auto_approve=True  # 質問スキップ（デフォルト値で仕様生成）
)
# -> {"execution_plan": {...}, "permissions": [...], "next_action": {...}}
```

**知識の蓄積:**
- `spec_executor_finalize`で実行結果をPrismindに保存
- カテゴリ: `実装記録`, `実装詳細`
- 次回セッションで`resume`時に参照可能

### GitHub 連携 (`github_dispatch.py`)

ローカルの github-mcp コンテナ (`127.0.0.1:8116`, toolsets `repos,issues,pull_requests`) を **2ツールに集約**して中継する。PAT がいずれも未設定のときは無効 (no-auth tailnet インスタンス・テストは無影響)。

**identity ルーティング:** review submit 系操作 (`pull_request_review_write` / `add_comment_to_pending_review`) は reviewer PAT (`GITHUB_MCP_PAT_REVIEWER`, spirrowgames-ops, Contents read-only)、それ以外 (commit / push / PR 作成 / merge / 読み取り) は implementer PAT (`GITHUB_MCP_PAT_IMPLEMENTER`, takahito-spirrowgames, Contents RW) で上流に転送する。role 別 PAT が未設定なら legacy `GITHUB_MCP_PAT` にフォールバックし、単一 PAT 運用は従来どおり動く。これにより PR を立てたアカウントが自分の PR に formal review を送って 422 になる事故 (PR #67) を回避する。なお両 PAT は同一プロセスの environ に載るため **operation 単位の分離**であり、プロセス/ファイル分離ではない (真の隔離はディスパッチャ 2 インスタンス化が必要)。

**merge ガード ("merge to main = 人間GO"):** この GitHub プランは branch protection 不可、かつコネクタの per-tool 権限は 35 ツールを畳んだ `github` 1 つ単位でしか効かず `operation` 単位で deny できない。そこでディスパッチャ自身が `merge_pull_request` を上流転送する前に `pull_request_read(get)` で PR の `base.ref` を引き、保護ブランチ (既定 `main`、`GITHUB_PROTECTED_BASE_BRANCHES` で可変) 宛なら **転送せず policy block** を返す。develop 等への merge は通る。base を判定できない (引数欠落 / lookup 失敗) ときは **fail-closed** で拒否。人間は本ディスパッチャを介さず手動で main にマージする。

| ツール | 用途 |
|--------|------|
| `github` | GitHub 操作を実行。`operation` に操作名、`arguments` にその引数を渡す。説明文に全35操作のカタログ内蔵 |
| `github_operations` | 各操作の正確な JSON 入力スキーマを返す。`name_filter` で部分一致フィルタ |

```python
# スキーマ確認（複雑な引数のとき）
github_operations(name_filter="pull_request")
# -> [{"operation": "create_pull_request", "input_schema": {...required: [owner,repo,title,head,base]}}, ...]

# 操作実行
github(operation="search_repositories", arguments={"query": "user:spirrowgames-ops"})
github(operation="list_issues", arguments={"owner": "SpirrowGames", "repo": "spirrow-magickit"})

# エラー時は github / github_operations 共通の包絡
# -> {"error": "_UpstreamError: ...(上流HTTPステータス+本文)", "hint": "..."}
```

**設計背景:**
- claude.ai コネクタは接続時にツール定義を固定し `tools/list_changed` を会話中に反映しない (新セッションで再取得)。よって動的 toolset 展開は不可、35ツール個別公開はコンテキストを圧迫するため2ツールに集約
- 操作・引数の選択は **Claude 自身が行う** (弱いモデルへのルーティング委譲なし)。GitHub API は Claude の事前知識が強く信頼性が高い
- 上流通信は **ステートレスな per-call httpx JSON-RPC** (`initialize → notifications/initialized → method`)。FastMCP の StreamableHttp クライアント (ステートフルな SSE GET ストリームで github-mcp が 405 → 無限 reconnect → 長時間稼働で 400) を回避した経緯あり
- 補足: Claude Code (CLI) は `tools/list_changed` を自動反映するため、CLI 利用に限れば動的 gate 方式も成立する (本構成はコネクタ=モバイル前提なのでディスパッチャを採用)

### ヘルスチェック (`health.py`)

全サービスのヘルス状態を一括確認。

| ツール | 用途 |
|--------|------|
| `service_health` | Cognilens / Prismind / Lexora / Conclair / github-mcp の稼働状況を一括チェック |

```python
# 使用例
service_health()
# -> {"status": "healthy", "services": {
#      "cognilens": {...}, "prismind": {...}, "lexora": {...}, "conclair": {...},
#      "github_mcp": {"status": "healthy", "operation_count": 35, ...}}}
# github_mcp は GITHUB_MCP_PAT 未設定時 status="disabled"（表示はするが健全性比率からは除外）
```

### コンテンツ生成 (`generation.py`)

RAG強化によるコンテンツ生成。

| ツール | 用途 |
|--------|------|
| `generate_with_context` | Prismind検索 + Cognilens圧縮 + Lexora生成 |

```python
# 使用例
generate_with_context(
    task="射撃システムの設計書を書いて",
    context_query="射撃 弾丸 ダメージ",
    project="trapxtrap",
    max_context_tokens=1500,
    max_output_tokens=1000
)
```

## 設定

`config/magickit_config.yaml` を参照。環境変数でオーバーライド可能。

```bash
MAGICKIT_LEXORA_URL=http://localhost:8110
MAGICKIT_COGNILENS_URL=http://localhost:8111
MAGICKIT_PRISMIND_URL=http://localhost:8112
MAGICKIT_PORT=8113            # FastAPI HTTP API
MAGICKIT_MCP_PORT=8114        # MCP server (Streamable HTTP)
MAGICKIT_TRANSPORT_MODE=http  # http (default) | sse (legacy)
MAGICKIT_AUTH_DISABLED=0      # 1 to bypass Google OAuth on the MCP endpoint

# GitHub 連携 (github_dispatch.py)
# implementer/reviewer の identity を operation 種別で切り替える。review submit 系
# (pull_request_review_write / add_comment_to_pending_review) は reviewer PAT、
# それ以外 (commit / push / PR 作成 / merge / 読み取り) は implementer PAT を使用。
# どちらも未設定のときは legacy の GITHUB_MCP_PAT にフォールバック（旧来の単一 PAT 運用を維持）。
GITHUB_MCP_PAT_IMPLEMENTER=github_pat_... # implementer (takahito-spirrowgames): Contents/PR/Issues RW
GITHUB_MCP_PAT_REVIEWER=github_pat_...    # reviewer (spirrowgames-ops): PR/Issues RW, Contents read-only
GITHUB_MCP_PAT=github_pat_... # legacy 単一 PAT。上記 2 つの fallback 兼 github ツールの有効化ゲート。
                              # いずれも未設定なら github ツール無効。秘密は設定ファイルに置かず
                              # /etc/spirrow-magickit/github.env に格納し、公開インスタンスのみ
                              # systemd EnvironmentFile で注入（no-auth の -local には注入しない）
GITHUB_MCP_URL=http://127.0.0.1:8116/mcp  # 既定値（通常変更不要）
GITHUB_PROTECTED_BASE_BRANCHES=main       # この base への merge_pull_request を拒否(人間GO)。
                                          # カンマ区切りで複数可。既定 main。develop 等は通る。
```

## 起動方法

本番運用は **systemd 経由のみ**(SSH セッションでの uvicorn 直起動は禁止 — 過去の OOM 事案あり)。

```bash
# FastAPI HTTP API
sudo systemctl restart spirrow-magickit.service          # main.py @ 0.0.0.0:8113

# MCP server (Cloudflare Tunnel 経由で claude.ai web に公開、auth ON)
sudo systemctl restart spirrow-magickit-mcp.service      # mcp_server.py @ 127.0.0.1:8114

# MCP server (tailnet 内の Claude Code CLI 用、auth OFF)
sudo systemctl restart spirrow-magickit-mcp-local.service # mcp_server.py @ 100.79.84.62:8117

# github-mcp コンテナ (Docker, 127.0.0.1:8116)。公開インスタンスがディスパッチャ経由で中継
sudo systemctl restart github-mcp.service                 # docker start github-mcp
```

github-mcp コンテナの実体:
```bash
docker run -d --name github-mcp --restart unless-stopped -p 127.0.0.1:8116:8082 \
  ghcr.io/github/github-mcp-server:v1.0.3 http --toolsets=repos,issues,pull_requests
```
PAT はコンテナに焼かず、Magickit ディスパッチャがリクエストごとに `Authorization: Bearer` で注入する (`/etc/spirrow-magickit/github.env` → 公開 `spirrow-magickit-mcp.service` の drop-in EnvironmentFile)。no-auth の `-local` には注入しない。

開発時にローカル一時起動が必要なら `systemd-run --user --property=MemoryMax=2G python -m magickit.mcp_server` のように transient unit にする(global CLAUDE.md ルール準拠)。

## Phase 1 スコープ

1. タスクキュー（登録・取得・完了）
2. 依存関係管理
3. Adapter実装（Lexora, Cognilens, Prismind）
4. 基本的なルーティング
5. ヘルスチェック

## 将来の拡張（Phase 2以降）

- マルチプロジェクト対応
- チームコラボレーション（ワークスペース、ロック）
- WebUIダッシュボード
- Slack/Discord連携

## 参照ドキュメント

- `docs/DESIGN.md` - 詳細設計
