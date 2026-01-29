# Spirrow-Magickit

オーケストレーションレイヤー for Spirrow Platform

## 概要

複数のMCPサーバを統合し、ローカルLLMによる知的なルーティングと最適化を行う司令塔。タスク管理・依存関係解決・コンテキスト最適化を担当。

## アーキテクチャ

```
Claude Code / Client
        │
        ▼
    Magickit (:8004)
        │
   ┌────┼────┬────┐
   ▼    ▼    ▼    ▼
Lexora Cognilens Prismind UnrealWise
```

**重要**: 「指揮者 - 自分では演奏しない」。各サービスへの委譲に徹する。

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
│   └── tools/           # MCPツール
│       ├── health.py    # ヘルスチェック
│       ├── research.py  # 知識検索・要約
│       ├── orchestration.py  # ルーティング・ワークフロー
│       ├── generation.py     # RAG強化コンテンツ生成
│       ├── session.py   # セッション管理
│       ├── project.py   # プロジェクト管理
│       ├── document.py  # スマートドキュメント作成
│       ├── specification.py  # AI駆動仕様策定
│       └── execution.py  # タスク分解・実行管理
├── adapters/
│   ├── base.py          # Adapter ABC
│   ├── lexora.py        # LLM呼び出し
│   ├── cognilens.py     # 圧縮
│   ├── prismind.py      # RAG検索
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

```python
# 使用例
begin_task(project="trapxtrap", task_description="射撃システム実装")
checkpoint(summary="基本実装完了", decisions=["弾丸はプールで管理"])
handoff(next_action="ダメージ計算の実装", notes="...")
resume(project="trapxtrap", detail_level="standard")
```

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

プロジェクトのライフサイクル管理ツール。

| ツール | 用途 |
|--------|------|
| `list_projects` | プロジェクト一覧取得（アーカイブ含む/除外） |
| `init_project` | テンプレートからプロジェクト初期化 |
| `get_project_status` | プロジェクトの詳細ステータス取得 |
| `clone_project` | 既存プロジェクトを複製 |
| `delete_project` | アーカイブ/エクスポート+削除/完全削除 |
| `restore_project` | アーカイブからの復元 |

```python
# 使用例
init_project(project="my-game", template="game", name="My Game")
get_project_status(project="my-game")
delete_project(project="old-project", mode="archive")
```

**テンプレート種類:**
- `game`: ゲーム開発（design, implementation, asset, bug, decision）
- `mcp-server`: MCPサーバ開発（architecture, tool, adapter, config）
- `web-app`: Webアプリ（frontend, backend, api, design）

### ドキュメント管理 (`document.py`)

未登録のドキュメントタイプを自動処理するスマートドキュメント作成。
RAGベースのセマンティック検索（BGE-M3埋め込み）で多言語マッチングをサポート。

| ツール | 用途 |
|--------|------|
| `smart_create_document` | 未知のdoc_typeをRAGセマンティック検索で自動マッチ・登録してドキュメント作成 |

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
2. 未登録の場合、RAGセマンティック検索で類似タイプを検索（閾値0.75）
3. 類似タイプがあれば既存タイプを使用（例: "api仕様" ≈ "api_spec"、多言語対応）
4. 類似タイプがなければLexoraでメタデータ生成 → グローバルとして登録（フォルダ名は英語のみ）
5. ドキュメントを作成

**セマンティックマッチング:**
- BGE-M3埋め込みによる多言語対応（日本語 ↔ 英語も可）
- 閾値: 0.75（設定可能）
- 例: "api仕様" → "api_spec", "設計ドキュメント" → "design"

**レスポンス:**
- `matched_existing: true` - 既存タイプにRAGセマンティックマッチ
- `type_registered: true` - 新規グローバルタイプを登録

**DocumentType スコープ:**
- `global`: 全プロジェクトで共有（~/.prismind_global_doc_types.json に保存）
- `project`: 特定プロジェクトのみ（ProjectConfig.document_types に保存）
- 同じtype_idが両方に存在する場合、プロジェクト側が優先される

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

### ヘルスチェック (`health.py`)

全サービスのヘルス状態を一括確認。

| ツール | 用途 |
|--------|------|
| `service_health` | Cognilens, Prismind, Lexoraの稼働状況を一括チェック |

```python
# 使用例
service_health()
# -> {"status": "healthy", "services": {"cognilens": {...}, "prismind": {...}, "lexora": {...}}}
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
MAGICKIT_LEXORA_URL=http://localhost:8001
MAGICKIT_COGNILENS_URL=http://localhost:8003
MAGICKIT_PRISMIND_URL=http://localhost:8002
MAGICKIT_PORT=8004
```

## 起動方法

```bash
# 開発
uvicorn magickit.main:app --reload --port 8004

# 本番
python -m magickit.main
```

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
