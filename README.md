# Spirrow-Magickit

オーケストレーションレイヤー for Spirrow Platform

## 概要

複数のMCPサーバを統合し、ローカルLLMによる知的なルーティングと最適化を行う司令塔。
タスク管理・依存関係解決・コンテキスト最適化を担当。

**「指揮者 - 自分では演奏しない」** 各サービスへの委譲に徹する。

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

## 技術スタック

- Python 3.11+
- FastMCP (MCPサーバー)
- FastAPI (REST API)
- httpx (非同期HTTPクライアント)
- Pydantic v2

## 主要機能

### MCPツール

Magickitは複数サービスを組み合わせた高レベルなMCPツールを提供します。

| ツール | 説明 |
|--------|------|
| `service_health` | 全サービスのヘルス状態を一括確認 |
| `research_and_summarize` | Prismind検索 + Cognilens圧縮 |
| `analyze_documents` | ドキュメント検索 + エッセンス抽出 |
| `generate_with_context` | RAG強化コンテンツ生成 |
| `intelligent_route` | タスク分析と最適サービス推奨 |
| `orchestrate_workflow` | 複数サービスの連携ワークフロー |
| `begin_task` / `resume` | セッションコンテキスト復元 |
| `checkpoint` | 作業の中間保存 |
| `handoff` | セッション終了と引き継ぎ |
| `list_projects` / `init_project` | プロジェクト管理 |
| `get_project_status` | プロジェクト詳細ステータス |
| `smart_create_document` | スマートドキュメント作成（RAGセマンティックマッチング） |

### スマートドキュメント作成

未登録のドキュメントタイプをRAGベースのセマンティック検索（BGE-M3埋め込み）で自動マッチング。多言語対応。

```python
# 日本語入力でも既存の英語タイプにマッチ
smart_create_document(
    name="2024-01-15 Sprint Planning",
    doc_type="議事録",  # → "meeting_minutes" にマッチ
    content="...",
    phase_task="phase1-task2"
)
```

**処理フロー:**
1. RAGセマンティック検索で類似タイプを検索（閾値0.45）
2. マッチすれば既存タイプを使用（例: "議事録" ≈ "meeting_minutes"）
3. マッチしなければLLMでメタデータ生成 → グローバルとして登録
4. ドキュメント作成

### オーケストレーションワークフロー

`orchestrate_workflow`で複数サービスを連携したワークフローを実行。

```python
steps = [
    {"service": "prismind", "action": "search",
     "params": {"query": "AI best practices"}, "output_key": "search_results"},
    {"service": "cognilens", "action": "compress",
     "params": {"text": "${search_results}", "max_tokens": 500},
     "depends_on": [0], "output_key": "compressed"},
    {"service": "lexora", "action": "generate",
     "params": {"prompt": "Based on: ${compressed}\n\nWrite a summary."},
     "depends_on": [1]}
]
```

## セットアップ

```bash
# 仮想環境作成
python -m venv .venv
source .venv/bin/activate

# 依存関係インストール
pip install -e ".[dev]"

# 環境変数設定（オプション）
export MAGICKIT_LEXORA_URL=http://localhost:8001
export MAGICKIT_COGNILENS_URL=http://localhost:8003
export MAGICKIT_PRISMIND_URL=http://localhost:8002
export MAGICKIT_PORT=8004
```

## 起動方法

### MCPサーバーとして（推奨）

```bash
# mcp-proxyを使用してSSE経由で公開
npx mcp-proxy --port 8004 --host 0.0.0.0 -- python -m magickit.mcp_server
```

### REST APIサーバーとして

```bash
# 開発
uvicorn magickit.main:app --reload --port 8004

# 本番
python -m magickit.main
```

## 設定

環境変数で設定をカスタマイズ:

```bash
# サービスURL
MAGICKIT_LEXORA_URL=http://localhost:8001
MAGICKIT_COGNILENS_URL=http://localhost:8003
MAGICKIT_PRISMIND_URL=http://localhost:8002
MAGICKIT_PORT=8004

# タイムアウト
MAGICKIT_LEXORA_TIMEOUT=60.0
MAGICKIT_COGNILENS_TIMEOUT=30.0
MAGICKIT_PRISMIND_TIMEOUT=30.0
```

## プロジェクト構成

```
src/magickit/
├── main.py              # FastAPIアプリ
├── mcp_server.py        # MCPサーバエントリポイント
├── config.py            # 設定 (Pydantic Settings)
├── api/
│   ├── routes.py        # REST APIエンドポイント
│   └── models.py        # Request/Response
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
│   ├── mcp_base.py      # MCP Adapter 基底クラス
│   ├── lexora.py        # LLM呼び出し
│   ├── cognilens.py     # 圧縮 (MCP)
│   └── prismind.py      # RAG検索 (MCP)
├── core/
│   ├── task_queue.py    # タスクキュー
│   ├── dependency_graph.py  # 依存関係グラフ
│   └── context_manager.py   # コンテキスト最適化
└── utils/
    └── logging.py
```

## REST APIエンドポイント

| Method | Path | 説明 |
|--------|------|------|
| GET | `/health` | ヘルスチェック |
| GET | `/stats` | 統計情報 |
| POST | `/tasks` | タスク登録 |
| GET | `/tasks` | タスク一覧 |
| GET | `/tasks/next` | 次タスク取得 |
| POST | `/tasks/{id}/complete` | タスク完了 |
| POST | `/route` | LLMベースルーティング |
| POST | `/orchestrate` | 複合タスクオーケストレーション |

## MCP Adapter API

MCPサーバとの通信を抽象化する `MCPBaseAdapter` クラスを提供。

```python
from magickit.adapters.prismind import PrismindAdapter

adapter = PrismindAdapter(sse_url="http://localhost:8002/sse")

# 動的メソッドディスパッチ（推奨）
result = await adapter.list_projects()
result = await adapter.search_knowledge(query="test", limit=5)
result = await adapter.find_similar_document_type(type_query="議事録", threshold=0.45)

# または明示的にcall()
result = await adapter.call("search_knowledge", query="test", limit=5)
```

## テスト

```bash
# 全テスト実行
pytest tests/

# カバレッジ付き
pytest tests/ --cov=magickit --cov-report=html
```

## 依存サービス

Magickitは以下のサービスと連携します:

| サービス | ポート | 説明 |
|---------|--------|------|
| Lexora | 8001 | ローカルLLM（Qwen2.5など） |
| Prismind | 8002 | 知識管理・RAG検索 |
| Cognilens | 8003 | テキスト圧縮・要約 |

## ライセンス

[MIT License](LICENSE)
