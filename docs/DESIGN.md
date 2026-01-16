# Spirrow-Magickit 設計ドキュメント

## 概要

| 項目 | 内容 |
|------|------|
| **名前** | Spirrow-Magickit |
| **世界観** | 魔法の道具箱 |
| **役割** | オーケストレーション（司令塔） |
| **コンセプト** | 「全てを束ねるオーケストレーター」 |

```
Magickit = Magic (魔法) + Kit (道具箱)

「指揮者 - 自分では演奏しない」
- 複数のMCPサーバを統合
- ローカルLLMによる知的なルーティング
- タスク管理・依存関係解決
- コンテキスト最適化
```

## 配置

- **場所**: AIサーバ
- **理由**: ローカルLLMを活用したルーティング処理

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────────┐
│                      Spirrow-Magickit                           │
│                    (魔法の道具箱)                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ Core (自前実装)                                           │ │
│  │ ├── TaskQueue (キュー管理)                                │ │
│  │ ├── DependencyGraph (依存関係)                            │ │
│  │ ├── StateManager (状態・チェックポイント)                 │ │
│  │ ├── ContextManager (コンテキスト最適化)                   │ │
│  │ ├── ProjectManager (マルチプロジェクト)                   │ │
│  │ ├── WorkspaceManager (チーム利用)                         │ │
│  │ ├── ConflictDetector (衝突検出)                           │ │
│  │ └── Scheduler (スケジューリング)                          │ │
│  └───────────────────────────────────────────────────────────┘ │
│                              │                                  │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │ Adapters (外部モジュール呼び出し)                         │ │
│  │ ├── LexoraAdapter (LLM基盤)                               │ │
│  │ ├── PrismindAdapter (知識検索・保存)                      │ │
│  │ ├── CognilensAdapter (圧縮・要約)                         │ │
│  │ ├── UnrealWiseAdapter (UE操作)                            │ │
│  │ └── ClaudeCodeAdapter (C++タスク通知)                     │ │
│  └───────────────────────────────────────────────────────────┘ │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Lexora     │    │  Cognilens   │    │  Prismind    │
│  (LLM基盤)   │    │   (圧縮)     │    │   (RAG)      │
└──────────────┘    └──────────────┘    └──────────────┘
        │
        ▼
┌──────────────┐
│    vLLM      │
└──────────────┘
```

## 主要機能

### 1. タスクキュー管理

Claude Codeからのタスクを受け取り、キューで管理する。

```python
# タスク登録
magickit.register_tasks(
    tasks=[
        {"id": "task1", "type": "cpp", "description": "...", "full_context": "..."},
        {"id": "task2", "type": "ue", "depends_on": ["task1"], ...},
    ]
)

# 次のタスク取得（必要情報付き）
task = magickit.get_next_task()

# タスク完了報告
magickit.complete_task(task_id="task1", result_summary="...")
```

### 2. 依存関係管理

タスク間の依存関係を管理し、正しい順序で実行されるよう制御する。

```
Task 1: C++ BaseTrap作成
    ↓
Task 2: BP_ExplosionTrap作成 ← Task1完了後に実行
    ↓
Task 3: Widget UI作成 ← Task2完了後に実行
```

### 3. コンテキスト最適化

タスク実行時に必要な情報を事前収集・整理し、コンテキストウィンドウを節約する。

```
┌─────────────────────────────────────────────────────────────┐
│ コンテキスト最適化フロー                                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. タスク受信                                              │
│     └→ 大量のMCP結果 + 会話履歴 (15,000トークン)           │
│                                                             │
│  2. Prismindで関連知識検索                                  │
│     └→ 過去の類似実装を取得                                │
│                                                             │
│  3. Cognilensで圧縮                                         │
│     └→ 重要度スコアリング                                  │
│     └→ 段階圧縮                                            │
│                                                             │
│  4. 最適化されたコンテキスト (6,000トークン)               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 4. インテリジェントルーティング

リクエスト内容に基づいて最適なサービスを選択。

```python
# ルーティング判断（Lexora経由でLLM使用）
route = magickit.route(
    query="敵AIにBehaviorTree追加して",
    available_services=["unrealwise", "prismind", "cognilens"]
)
# → unrealwise + prismind（過去事例参照）
```

### 5. マルチプロジェクト対応

```python
magickit.set_project("TrapxTrapCpp")
magickit.set_project("AnotherGame")
```

### 6. チームコラボレーション

#### ワークスペース分離
```
Project: TrapxTrapCpp
├── Workspace A (Takahito): トラップシステム
└── Workspace B (Partner): UI実装
```

#### リソースロック
```python
magickit.acquire_lock(
    resource="TrapManager.cpp",
    user="takahito",
    mode="exclusive"
)
```

#### 権限モデル
| ロール | 権限 |
|--------|------|
| Owner | 全権限、メンバー管理 |
| Member | タスク実行、知識ベース読み書き |
| Guest | 特定タスクのみ、読み取り専用 |

### 7. その他の機能

- **ドライラン / プレビュー**: 実行前に影響範囲を確認
- **チェックポイント**: 状態の保存・復元
- **ロールバック**: チェックポイントへの巻き戻し
- **フィードバックループ**: 失敗時の自動リトライ/代替案

## データモデル

### TaskMemory

```python
@dataclass
class TaskMemory:
    task_id: str
    task_type: Literal["cpp", "ue", "other"]
    
    # 事前準備された情報
    context_summary: str      # Cognilensで圧縮済み
    references: list[str]     # 必要なドキュメント要約
    dependencies: list[str]   # 依存タスクID
    
    # 実行状態
    status: Literal["waiting", "ready", "running", "done", "failed"]
    result: str | None        # 完了後の成果物サマリ
    
    # メタ情報
    estimated_tokens: int
    priority: int
    created_at: datetime
    updated_at: datetime
```

### Project

```python
@dataclass
class Project:
    project_id: str
    name: str
    workspaces: list[Workspace]
    settings: ProjectSettings
```

## API設計

### エンドポイント

```
# タスク管理
POST /tasks                    # タスク登録
GET  /tasks                    # タスク一覧
GET  /tasks/{task_id}          # タスク詳細
GET  /tasks/next               # 次の実行可能タスク取得
POST /tasks/{task_id}/complete # タスク完了報告
POST /tasks/{task_id}/fail     # タスク失敗報告

# オーケストレーション
POST /orchestrate              # 総合オーケストレーション
POST /route                    # ルーティング判断
POST /compress-context         # コンテキスト圧縮

# プロジェクト
GET  /projects                 # プロジェクト一覧
POST /projects                 # プロジェクト作成
PUT  /projects/{project_id}    # プロジェクト切り替え

# 管理
GET  /health                   # ヘルスチェック
GET  /stats                    # 統計情報
```

## 設定ファイル

```yaml
# magickit_config.yaml

server:
  host: "0.0.0.0"
  port: 8004

services:
  lexora:
    url: "http://localhost:8001"
    timeout: 30
    
  cognilens:
    url: "http://localhost:8003"
    auto_compress: true
    compression_threshold: 4000  # tokens
    
  prismind:
    url: "http://localhost:8002"
    auto_rag: true
    relevance_threshold: 0.7
    
  unrealwise:
    url: "http://localhost:8005"

orchestration:
  max_context_tokens: 8000
  compression_target: 0.5
  
routing:
  strategy: "intelligent"  # intelligent / round-robin / priority
  fallback_enabled: true

queue:
  max_size: 100
  default_timeout: 300

storage:
  type: "sqlite"  # sqlite / postgres
  path: "./data/magickit.db"

logging:
  level: "INFO"
  format: "json"
```

## プロジェクト構成

```
spirrow-magickit/
├── docs/
│   ├── DESIGN.md           # この設計書
│   └── API.md              # API仕様書
├── src/
│   └── magickit/
│       ├── __init__.py
│       ├── main.py         # エントリーポイント
│       ├── config.py       # 設定読み込み
│       ├── api/
│       │   ├── __init__.py
│       │   ├── routes.py   # エンドポイント定義
│       │   └── models.py   # リクエスト/レスポンスモデル
│       ├── core/
│       │   ├── __init__.py
│       │   ├── task_queue.py
│       │   ├── dependency_graph.py
│       │   ├── state_manager.py
│       │   ├── context_manager.py
│       │   ├── project_manager.py
│       │   ├── workspace_manager.py
│       │   ├── conflict_detector.py
│       │   └── scheduler.py
│       ├── adapters/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── lexora.py
│       │   ├── cognilens.py
│       │   ├── prismind.py
│       │   └── unrealwise.py
│       └── utils/
│           ├── __init__.py
│           └── logging.py
├── tests/
│   └── ...
├── config/
│   └── magickit_config.yaml
├── pyproject.toml
├── README.md
└── CLAUDE.md
```

## 連携サービス

| サービス | 連携内容 |
|----------|----------|
| Lexora | ルーティング判断のためのLLM呼び出し |
| Cognilens | タスクコンテキストの圧縮 |
| Prismind | 過去パターン検索、知識保存 |
| UnrealWise | UEタスクの実行委譲 |
| Claude Code | C++タスクの通知、結果受信 |
| WatchDog | ヘルスチェック、障害監視 |

## 通信プロトコル

| 経路 | プロトコル |
|------|-----------|
| Claude Code → Magickit | MCP over HTTP/SSE |
| Magickit → UnrealWise | HTTP API |
| Magickit → Lexora/Cognilens/Prismind | HTTP API（内部） |

## ユースケース

### 1. UE5操作フロー

```
1. 「敵キャラクターにAI追加して」リクエスト
2. Prismindで過去の類似実装を検索
3. 関連ナレッジをコンテキストに付与
4. UnrealWiseへルーティング
5. 実行結果を返却
6. 結果をPrismindに蓄積（学習）
```

### 2. 複雑なタスクの分解

```
1. 「トラップシステム全体を実装して」
2. タスク分解:
   - Task 1: C++ BaseTrap クラス作成
   - Task 2: BP_ExplosionTrap 作成
   - Task 3: BP_FreezeTrap 作成
   - Task 4: TrapManager 実装
   - Task 5: UI Widget 作成
3. 依存関係を解決しながら順次実行
```

## 今後の拡張

- [ ] WebUIダッシュボード
- [ ] タスク進捗のリアルタイム可視化
- [ ] Slack/Discord連携強化
- [ ] A/Bテスト機能（ルーティング戦略比較）
- [ ] コスト最適化（LLM呼び出し削減）
- [ ] プラグインシステム（カスタムサービス追加）

---

*Document Version: 2.0*
*Last Updated: 2026-01-17*
