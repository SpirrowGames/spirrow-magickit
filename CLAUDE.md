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
│       └── document.py  # スマートドキュメント作成
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

| ツール | 用途 |
|--------|------|
| `smart_create_document` | 未知のdoc_typeを自動分類・登録してドキュメント作成 |

```python
# 使用例: 未登録のdoc_typeでも自動的にLexoraで分類→Prismindに登録→作成
smart_create_document(
    name="2024-01-15 Sprint Planning",
    doc_type="meeting_notes",  # 未登録でもOK
    content="...",
    phase_task="phase1-task2",
    project="trapxtrap"
)
```

**処理フロー:**
1. Prismindで既存doc_type一覧を取得
2. 未登録の場合、Lexoraで既存タイプとの意味的類似度をチェック
3. 類似タイプがあれば既存タイプを使用（例: "design" ≈ "spec"）
4. 類似タイプがなければ新規タイプを登録（フォルダ名は英語のみ）
5. ドキュメントを作成

**レスポンス:**
- `matched_existing: true` - 既存タイプにセマンティックマッチ
- `type_registered: true` - 新規タイプを登録

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
