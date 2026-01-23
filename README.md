# Spirrow-Magickit

オーケストレーションレイヤー for Spirrow Platform

## 概要

複数のMCPサーバを統合し、ローカルLLMによる知的なルーティングと最適化を行う司令塔。
タスク管理・依存関係解決・コンテキスト最適化を担当。

**Phase 2** では以下の機能を追加:
- マルチプロジェクト・ワークスペース管理
- JWT認証・RBAC権限管理
- 分散ロック機構
- Slack/Discord Webhook連携
- WebSocketリアルタイム更新
- WebUIダッシュボード

## アーキテクチャ

```
Claude Code / Client
        │
        ▼
    Magickit (:8004)
   ┌────┴────────────────┐
   │  ┌─────────────┐    │
   │  │ WebUI       │    │
   │  │ Dashboard   │    │
   │  └─────────────┘    │
   │  ┌─────────────┐    │
   │  │ Auth (JWT)  │    │
   │  └─────────────┘    │
   │  ┌─────────────┐    │
   │  │ Workspace/  │    │
   │  │ Project Mgr │    │
   │  └─────────────┘    │
   │  ┌─────────────┐    │
   │  │ Lock Mgr    │    │
   │  └─────────────┘    │
   │  ┌─────────────┐    │
   │  │ Notification│───────► Slack / Discord
   │  └─────────────┘    │
   └─────────┬───────────┘
             │
   ┌────┬────┼────┬────┐
   ▼    ▼    ▼    ▼    ▼
Lexora Cognilens Prismind UnrealWise
```

## 技術スタック

- Python 3.11+
- FastAPI
- SQLite (状態管理)
- httpx (非同期HTTPクライアント)
- Pydantic v2
- python-jose (JWT認証)
- passlib + bcrypt (パスワードハッシュ)
- websockets (リアルタイム通信)
- Jinja2 + HTMX (WebUI)

## 主要機能

### Phase 1 (基本機能)
| 機能 | 説明 |
|------|------|
| タスクキュー | 優先度・依存関係を考慮したタスク管理 |
| 依存関係グラフ | DAGによるタスク依存関係解決 |
| コンテキスト最適化 | Cognilens連携による圧縮・最適化 |
| サービスルーティング | LLMベースのタスク分類による知的ルーティング |

### Phase 2 (拡張機能)
| 機能 | 説明 |
|------|------|
| ワークスペース | チーム単位でのリソース管理 |
| プロジェクト | ワークスペース内でのタスクグループ化 |
| JWT認証 | ユーザー登録・ログイン・トークン管理 |
| RBAC | ロールベースアクセス制御 (owner/admin/member/viewer) |
| 分散ロック | リソースの排他制御（TTL対応） |
| Webhook通知 | Slack/Discordへのイベント通知 |
| WebSocket | タスク状態のリアルタイム更新 |
| ダッシュボード | タスク・プロジェクト管理UI |

## セットアップ

```bash
# 仮想環境作成
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 依存関係インストール
pip install -e ".[dev]"

# 環境変数設定
cp .env.example .env
# .env を編集して必要な値を設定（特に MAGICKIT_JWT_SECRET）

# 開発サーバー起動
uvicorn magickit.main:app --reload --port 8004
```

## セキュリティに関する注意

- **JWT_SECRET**: 本番環境では必ず強力なランダム文字列を設定してください
  ```bash
  # シークレットキー生成例
  openssl rand -hex 32
  ```
- **環境変数**: 機密情報は `.env` ファイルまたは環境変数で管理し、コードにハードコードしないでください
- **データベース**: `data/magickit.db` は `.gitignore` に含まれています。本番データをリポジトリにコミットしないでください

## 設定

環境変数で設定をカスタマイズ:

```bash
# サービスURL
MAGICKIT_LEXORA_URL=http://localhost:8001
MAGICKIT_COGNILENS_URL=http://localhost:8003
MAGICKIT_PRISMIND_URL=http://localhost:8002
MAGICKIT_PORT=8004

# データベース
MAGICKIT_DB_PATH=./data/magickit.db

# 認証 (Phase 2)
MAGICKIT_AUTH_ENABLED=true
MAGICKIT_JWT_SECRET=<YOUR_SECRET_KEY>  # 必須: 本番環境では強力なランダム文字列を設定
MAGICKIT_JWT_EXPIRE_MINUTES=60

# Webhook (Phase 2)
MAGICKIT_WEBHOOK_TIMEOUT=10.0
MAGICKIT_WEBHOOK_MAX_RETRIES=3
```

## APIエンドポイント

### 基本エンドポイント
| Method | Path | 説明 |
|--------|------|------|
| GET | `/health` | ヘルスチェック |
| GET | `/stats` | 統計情報 |

### タスク管理
| Method | Path | 説明 |
|--------|------|------|
| POST | `/tasks` | タスク登録 |
| GET | `/tasks` | タスク一覧 |
| GET | `/tasks/next` | 次タスク取得 |
| POST | `/tasks/{id}/complete` | タスク完了 |
| POST | `/tasks/{id}/fail` | タスク失敗 |
| GET | `/tasks/{id}/events` | タスクイベント取得 |

### ルーティング
| Method | Path | 説明 |
|--------|------|------|
| POST | `/route` | LLMベースのタスク分類とサービスルーティング |
| POST | `/orchestrate` | 複合タスクのオーケストレーション |

### 認証 (Phase 2)
| Method | Path | 説明 |
|--------|------|------|
| POST | `/auth/register` | ユーザー登録 |
| POST | `/auth/login` | ログイン |
| GET | `/auth/me` | 現在のユーザー情報 |

### ワークスペース (Phase 2)
| Method | Path | 説明 |
|--------|------|------|
| POST | `/workspaces` | ワークスペース作成 |
| GET | `/workspaces` | 一覧取得 |
| GET | `/workspaces/{id}` | 詳細取得 |
| PUT | `/workspaces/{id}` | 更新 |
| DELETE | `/workspaces/{id}` | 削除 |
| POST | `/workspaces/{id}/members` | メンバー追加 |
| DELETE | `/workspaces/{id}/members/{user_id}` | メンバー削除 |

### プロジェクト (Phase 2)
| Method | Path | 説明 |
|--------|------|------|
| POST | `/workspaces/{ws_id}/projects` | プロジェクト作成 |
| GET | `/workspaces/{ws_id}/projects` | 一覧取得 |
| GET | `/projects/{id}` | 詳細取得 |
| PUT | `/projects/{id}` | 更新 |
| DELETE | `/projects/{id}` | 削除 |
| GET | `/projects/{id}/stats` | 統計取得 |

### ロック (Phase 2)
| Method | Path | 説明 |
|--------|------|------|
| POST | `/locks` | ロック取得 |
| DELETE | `/locks/{id}` | ロック解放 |
| GET | `/locks` | アクティブロック一覧 |

### Webhook (Phase 2)
| Method | Path | 説明 |
|--------|------|------|
| POST | `/workspaces/{id}/webhooks` | Webhook登録 |
| GET | `/workspaces/{id}/webhooks` | 一覧取得 |
| DELETE | `/webhooks/{id}` | 削除 |
| POST | `/webhooks/{id}/test` | テスト送信 |

### WebSocket (Phase 2)
| Protocol | Path | 説明 |
|----------|------|------|
| WS | `/ws/projects/{id}` | プロジェクトのリアルタイム更新 |

### ダッシュボード (Phase 2)
| Method | Path | 説明 |
|--------|------|------|
| GET | `/dashboard` | ダッシュボードUI |
| GET | `/dashboard/stats` | 統計API |

## WebUIダッシュボード

ブラウザで `http://localhost:8004/dashboard` にアクセス。

**機能:**
- タスク一覧・ステータス確認
- プロジェクト管理
- リアルタイム更新（WebSocket）
- 統計情報表示

## テスト

```bash
# 全テスト実行
pytest tests/

# ユニットテストのみ
pytest tests/unit/

# 統合テストのみ
pytest tests/integration/

# カバレッジ付き
pytest tests/ --cov=magickit --cov-report=html
```

## プロジェクト構成

```
src/magickit/
├── main.py                    # FastAPIアプリ
├── config.py                  # 設定
├── api/
│   ├── routes.py              # Phase 1 エンドポイント
│   ├── routes_v2.py           # Phase 2 エンドポイント
│   ├── models.py              # Pydanticモデル
│   └── websocket.py           # WebSocket
├── auth/                      # 認証モジュール (Phase 2)
│   ├── jwt.py                 # JWT処理
│   ├── middleware.py          # 認証ミドルウェア
│   ├── dependencies.py        # FastAPI依存性
│   └── permissions.py         # RBAC権限
├── core/
│   ├── task_queue.py          # タスクキュー
│   ├── dependency_graph.py    # 依存関係グラフ
│   ├── state_manager.py       # 状態管理
│   ├── context_manager.py     # コンテキスト最適化
│   ├── migrations.py          # DBマイグレーション (Phase 2)
│   ├── workspace_manager.py   # ワークスペース管理 (Phase 2)
│   ├── project_manager.py     # プロジェクト管理 (Phase 2)
│   ├── lock_manager.py        # ロック管理 (Phase 2)
│   ├── notification_manager.py # 通知管理 (Phase 2)
│   └── event_publisher.py     # イベント発行 (Phase 2)
├── adapters/
│   ├── base.py                # Adapter ABC
│   ├── mcp_base.py            # MCP Adapter 基底クラス
│   ├── lexora.py              # LLM呼び出し
│   ├── cognilens.py           # 圧縮 (MCP)
│   ├── prismind.py            # RAG検索 (MCP)
│   ├── slack.py               # Slack Webhook (Phase 2)
│   └── discord.py             # Discord Webhook (Phase 2)
├── templates/                 # Jinja2テンプレート (Phase 2)
│   ├── base.html
│   ├── dashboard.html
│   ├── projects.html
│   └── tasks.html
└── static/                    # 静的ファイル (Phase 2)
    ├── css/dashboard.css
    └── js/dashboard.js

tests/
├── unit/
│   ├── test_task_queue.py
│   ├── test_dependency_graph.py
│   ├── test_workspace_manager.py
│   ├── test_project_manager.py
│   ├── test_lock_manager.py
│   ├── test_notification.py
│   └── test_mcp_adapters.py
└── integration/
    ├── test_api_integration.py
    └── test_prismind_adapter.py
```

## MCP Adapter API

MCPサーバとの通信を抽象化する `MCPBaseAdapter` クラスを提供。

### 基本的な使い方

```python
from magickit.adapters.prismind import PrismindAdapter

adapter = PrismindAdapter(sse_url="http://localhost:8112/sse")

# 方法1: call() メソッド
result = await adapter.call("list_projects")
result = await adapter.call("search_knowledge", query="test", limit=5)

# 方法2: 動的メソッドディスパッチ（推奨）
result = await adapter.list_projects()
result = await adapter.search_knowledge(query="test", limit=5)
```

### 利用可能なメソッド

| メソッド | 説明 |
|---------|------|
| `call(tool_name, **kwargs)` | 任意のMCPツールを呼び出し |
| `list_tools()` | 利用可能なツール名一覧を取得 |
| `get_tool_schemas()` | ツールスキーマ（名前・説明・入力スキーマ）を取得 |
| `batch_call(operations, parallel=True)` | 複数ツールを一括実行（並列/逐次） |
| `health_check()` | サービスの疎通確認 |

### 動的メソッドディスパッチ

`__getattr__` により、任意のMCPツールをメソッドとして呼び出し可能:

```python
# これらは等価
await adapter.call("start_session", project="foo")
await adapter.start_session(project="foo")

await adapter.call("update_task_status", task_id="T01", status="done")
await adapter.update_task_status(task_id="T01", status="done")
```

### バッチ実行

```python
# 並列実行（デフォルト）
results = await adapter.batch_call([
    ("list_projects", {}),
    ("get_setup_status", {}),
    ("search_knowledge", {"query": "test", "limit": 3}),
], parallel=True)

# 逐次実行
results = await adapter.batch_call(operations, parallel=False)
```

## LLMベースルーティング

Lexoraの新しいタスク分類API (`/v1/classify-task`) を使用して、キーワードマッチングではなくLLMベースの知的ルーティングを実現。

### タスク分類フロー

```
ユーザークエリ
      │
      ▼
  Lexora API (/v1/classify-task)
      │
      ├─► task_type: code, reasoning, analysis → Lexora
      ├─► task_type: summarization → Cognilens
      ├─► task_type: search, retrieval → Prismind
      │
      ▼
  サービス実行
```

### タスクタイプとサービスマッピング

| task_type | サービス | 説明 |
|-----------|---------|------|
| `code` | Lexora | コード生成・修正 |
| `reasoning` | Lexora | 論理的推論 |
| `analysis` | Lexora | 分析タスク |
| `summarization` | Cognilens | 要約・圧縮 |
| `translation` | Lexora | 翻訳 |
| `simple_qa` | Lexora | 簡単な質問応答 |
| `general` | Lexora | 一般タスク |
| `search` | Prismind | 検索 |
| `retrieval` | Prismind | 情報取得 |

### フォールバック

LexoraのAPIが利用できない場合、従来のキーワードベースヒューリスティクスにフォールバック:

- `search`, `find`, `lookup` → Prismind
- `compress`, `summarize`, `shorten` → Cognilens
- `unreal`, `blueprint`, `actor` → UnrealWise
- その他 → Lexora (デフォルト)

### LexoraAdapter API

```python
from magickit.adapters.lexora import LexoraAdapter

async with LexoraAdapter(base_url, timeout) as adapter:
    # モデル能力情報取得
    capabilities = await adapter.get_model_capabilities()
    # → {"models": [...], "available_capabilities": [...], ...}

    # タスク分類
    classification = await adapter.classify_task("Pythonでクイックソートを実装して")
    # → {"recommended_model": "...", "task_type": "code", "confidence": 0.95, ...}
```

## ライセンス

[MIT License](LICENSE)
