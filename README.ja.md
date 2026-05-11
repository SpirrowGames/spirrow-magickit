# Spirrow-Magickit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**Spirrow Platformのオーケストレーションレイヤー** - 複数のMCPサーバを統合し、知的なルーティングと最適化を行う司令塔。

[English README](README.md)

## 概要

複数のMCPサーバを統合し、ローカルLLMによる知的なルーティングと最適化を行う司令塔。
タスク管理・依存関係解決・コンテキスト最適化を担当。

**「指揮者 - 自分では演奏しない」** 各サービスへの委譲に徹する。

## アーキテクチャ

```
Claude Code / MCPクライアント
        │
        ▼
    Magickit (:8114 MCP Streamable HTTP / :8113 FastAPI)
        │
   ┌────┼────┬────┐
   ▼    ▼    ▼    ▼
Lexora Cognilens Prismind UnrealWise
(:8110)  (:8111)  (:8112)   (:8115)
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
| `smart_delete_document` | ドキュメントと関連knowledge一括削除 |
| `detect_orphan_documents` / `detect_orphan_knowledge` | 孤児データ検出 |
| `check_document_consistency` | 整合性チェック |
| `cleanup_documents` | バッチクリーンアップ |
| `add_task` / `list_tasks` | タスク管理 |
| `get_task` / `update_task` / `delete_task` | タスク操作（取得・更新・削除） |
| `start_task` / `complete_task` / `block_task` | タスクステータス管理 |
| `move_task_to_phase` / `set_task_priority` / `set_task_blockers` | ショートカット |
| `advance_phase` / `set_phase` | フェーズ遷移管理 |
| `add_milestone` / `list_milestones` | マイルストーン管理 |
| `get_burndown` / `estimate_completion` | 進捗追跡・完了予測 |
| `define_quality_gate` / `check_quality_gate` | 品質ゲート |
| `generate_status_report` / `generate_release_notes` | レポート生成 |

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

### ドキュメントメンテナンス

ドキュメント・knowledge・ドキュメントタイプの整合性管理とクリーンアップ。

| ツール | 説明 |
|--------|------|
| `smart_delete_document` | ドキュメントと関連knowledgeを一括削除（dry_run対応） |
| `detect_orphan_documents` | 孤児ドキュメント検出（削除済みプロジェクト、無効phase_task等） |
| `detect_orphan_knowledge` | 孤児knowledge検出（無効な参照） |
| `detect_unused_document_types` | 未使用・重複ドキュメントタイプ検出 |
| `check_document_consistency` | 包括的な整合性チェック |
| `cleanup_documents` | バッチクリーンアップ（dry_run + confirm必須） |

```python
# ドキュメント削除（プレビュー）
smart_delete_document(doc_id="doc-12345", dry_run=True)

# 整合性チェック
check_document_consistency(project="my-project")
# → {"summary": {"orphan_documents": 3, "orphan_knowledge": 5, ...}}

# クリーンアップ（安全確認必須）
cleanup_documents(
    cleanup_orphan_documents=True,
    dry_run=True  # まずプレビュー
)
```

**安全機能:**
- `dry_run=True`: デフォルトでプレビューモード
- `confirm=True`: 実削除には明示的確認が必要
- ドキュメントはゴミ箱移動（永久削除はオプション）

### プロジェクトライフサイクル管理

ゲーム開発など長期プロジェクトの立ち上げ→進捗管理→完了→アーカイブの全ライフサイクルをサポート。

#### フェーズ・マイルストーン管理

```python
# プロジェクト初期化（テンプレート使用）
init_project(project="my-game", template="game")

# マイルストーン追加
add_milestone(project="my-game", name="Alpha", target_date="2024-03-01", phase="production")
add_milestone(project="my-game", name="Beta", target_date="2024-05-01", phase="polish")

# フェーズ遷移（完了条件チェック付き）
advance_phase(project="my-game")  # pre-production → production
```

#### 進捗追跡・予測

```python
# バーンダウンチャートデータ取得
get_burndown(project="my-game", phase="production", days=14)

# 完了予測（ベロシティベース）
estimate_completion(project="my-game")
# → {"estimated_date": "2024-03-15", "days_remaining": 30, "confidence": "medium"}

# ベロシティ記録
track_velocity(project="my-game", completed_today=3, notes="順調に進行中")

# リスク指標
get_risk_indicators(project="my-game")
# → {"overall_risk": "low", "risk_score": 25, "indicators": [...]}
```

#### 品質ゲート

```python
# 品質ゲート定義
define_quality_gate(
    project="my-game",
    phase="production",
    criteria=[
        {"type": "task_completion", "threshold": 80},
        {"type": "no_critical_blockers"},
        {"type": "milestone_achieved", "milestone": "Alpha"}
    ]
)

# ゲートチェック
check_quality_gate(project="my-game", phase="production")
# → {"passed": true, "results": [...]}
```

#### レポート・分析

```python
# ステータスレポート生成
generate_status_report(project="my-game", format="markdown")

# リリースノート自動生成
generate_release_notes(project="my-game", version="v1.0.0", from_phase="production")

# 振り返り分析（LLMによるインサイト生成）
analyze_project_performance(project="my-game", use_llm=True)
```

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

## マルチユーザー対応

Magickitは複数ユーザーが同時に利用できるマルチユーザー環境をサポートしています。

### ユーザー識別

ユーザーは以下の優先順位で自動識別されます：

1. **`SPIRROW_USER` 環境変数** - 明示的な指定（最優先）
2. **`git config user.email`** - Gitの設定から取得
3. **OSユーザー名** - フォールバック

```bash
# 明示的にユーザーを指定する場合
export SPIRROW_USER="alice@example.com"
```

### ツールでのユーザー指定

すべてのMCPツールは `user` パラメータをサポートしています。省略時は自動検出されます。

```python
# 自動検出（推奨）
begin_task(project="my-project")

# 明示的に指定
begin_task(project="my-project", user="alice@example.com")
```

### ユーザー別データ分離

- セッション状態はユーザーごとに分離されます
- `prismind:session:{project}:{user}` 形式でストレージキーが生成されます
- 異なるユーザーが同じプロジェクトで作業しても、セッション状態が干渉しません

### 対応ツール

以下のツールがマルチユーザーに対応しています：

| カテゴリ | ツール |
|---------|--------|
| セッション | `begin_task`, `checkpoint`, `handoff`, `resume` |
| タスク管理 | `add_task`, `list_tasks`, `start_task`, `complete_task`, `block_task` |
| プロジェクト | `get_project_status`, `clone_project`, `delete_project`, `restore_project` |
| リサーチ | `research_and_summarize`, `analyze_documents` |
| 生成 | `generate_with_context` |
| ドキュメント | `smart_create_document`, `smart_delete_document`, `detect_orphan_documents`, `detect_orphan_knowledge`, `detect_unused_document_types`, `check_document_consistency`, `cleanup_documents` |
| ワークフロー | `orchestrate_workflow` |
| ライフサイクル | `advance_phase`, `set_phase`, `get_phase_status`, `add_milestone`, `update_milestone`, `list_milestones`, `check_milestone_status` |
| 進捗追跡 | `get_burndown`, `estimate_completion`, `track_velocity`, `get_risk_indicators` |
| 品質ゲート | `define_quality_gate`, `check_quality_gate`, `list_quality_gates` |
| レポート | `generate_status_report`, `generate_release_notes`, `analyze_project_performance` |

## セットアップ

### 前提条件

- Python 3.11+
- 以下のサービスが起動していること:
  - [Lexora](https://github.com/spirrowgames/spirrow-lexora) - ローカルLLMゲートウェイ
  - [Prismind](https://github.com/spirrowgames/spirrow-prismind) - 知識管理・RAG検索
  - [Cognilens](https://github.com/spirrowgames/spirrow-cognilens) - テキスト圧縮・要約

### インストール

```bash
# リポジトリをクローン
git clone https://github.com/spirrowgames/spirrow-magickit.git
cd spirrow-magickit

# 仮想環境作成
python -m venv .venv
source .venv/bin/activate

# 依存関係インストール
pip install -e ".[dev]"

# 環境変数設定（オプション - ローカル環境ではデフォルトで動作）
export MAGICKIT_LEXORA_URL=http://localhost:8110
export MAGICKIT_COGNILENS_URL=http://localhost:8111
export MAGICKIT_PRISMIND_URL=http://localhost:8112
```

## 起動方法

### MCPサーバーとして（推奨）

```bash
# Streamable HTTPサーバーとして起動（ポート8114）
python -m magickit.mcp_server
```

### REST APIサーバーとして

```bash
# 開発
uvicorn magickit.main:app --reload --port 8113

# 本番
python -m magickit.main
```

### Claude Code連携

`~/.claude/mcp.json` に追加:

```json
{
  "mcpServers": {
    "spirrow-magickit": {
      "type": "http",
      "url": "http://localhost:8114/mcp"
    }
  }
}
```

## 設定

環境変数で設定をカスタマイズ:

| 変数 | デフォルト | 説明 |
|------|-----------|------|
| `MAGICKIT_LEXORA_URL` | `http://localhost:8110` | Lexora URL |
| `MAGICKIT_COGNILENS_URL` | `http://localhost:8111` | Cognilens URL |
| `MAGICKIT_PRISMIND_URL` | `http://localhost:8112` | Prismind URL |
| `MAGICKIT_MCP_PORT` | `8114` | MCP Streamable HTTP サーバーポート |
| `MAGICKIT_PORT` | `8113` | FastAPI HTTP API サーバーポート |
| `MAGICKIT_TRANSPORT_MODE` | `http` | MCP transport: `http` (Streamable HTTP) または `sse` (旧式) |
| `MAGICKIT_AUTH_DISABLED` | `0` | `1` で Google OAuth をバイパス(ローカル限定デプロイ向け) |

またはファイルベースの設定: `config/magickit_config.yaml`

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
│       ├── document.py  # スマートドキュメント作成
│       ├── document_maintenance.py  # ドキュメント整合性・クリーンアップ
│       ├── task.py      # タスク管理
│       ├── lifecycle.py # フェーズ・マイルストーン管理
│       ├── progress.py  # 進捗追跡・予測
│       ├── quality.py   # 品質ゲート
│       └── reporting.py # レポート・分析
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
    ├── logging.py
    └── user.py           # マルチユーザー識別
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

adapter = PrismindAdapter(sse_url="http://localhost:8112/sse")

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

## 関連サービス

Magickitは以下のサービスと連携します:

| サービス | ポート | 説明 |
|---------|--------|------|
| [Lexora](https://github.com/spirrowgames/spirrow-lexora) | 8110 | ローカルLLMゲートウェイ（Qwen2.5など） |
| [Prismind](https://github.com/spirrowgames/spirrow-prismind) | 8112 | 知識管理・RAG検索 |
| [Cognilens](https://github.com/spirrowgames/spirrow-cognilens) | 8111 | テキスト圧縮・要約 |

## コントリビューション

コントリビューションを歓迎します！Pull Requestをお気軽にお送りください。

1. リポジトリをフォーク
2. フィーチャーブランチを作成 (`git checkout -b feature/amazing-feature`)
3. 変更をコミット (`git commit -m 'Add amazing feature'`)
4. ブランチにプッシュ (`git push origin feature/amazing-feature`)
5. Pull Requestを作成

## ライセンス

[MIT License](LICENSE)

## 謝辞

**Spirrow Platform** - AI駆動の開発ツールキットの一部です。
