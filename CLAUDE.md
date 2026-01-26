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
│       ├── orchestration.py  # ルーティング
│       ├── generation.py     # コンテンツ生成
│       └── session.py   # セッション管理
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

### その他

- `health.py` - サービスヘルスチェック
- `orchestration.py` - インテリジェントルーティング
- `generation.py` - RAG強化コンテンツ生成

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
