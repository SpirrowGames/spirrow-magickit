# Spirrow-Magickit 仕様設計書

## 概要

| 項目 | 内容 |
|------|------|
| **名前** | Spirrow-Magickit |
| **世界観** | 魔法の道具箱 |
| **役割** | オーケストレーション（司令塔） |
| **コンセプト** | 「全てを束ねるオーケストレーター」 |

## 目的

複数のMCPサーバーとAIサービスを統合し、インテリジェントなルーティング・圧縮・最適化を行う。
AI開発ワークフローの中心として、各サービスを効率的に連携させる。

## 機能要件

### コア機能

1. **インテリジェントルーティング**
   - リクエスト内容に基づく最適なサービス選択
   - 負荷分散
   - フェイルオーバー

2. **コンテキスト圧縮統合**
   - Cognilensを活用した自動圧縮
   - コンテキストウィンドウ使用量の最適化
   - 圧縮レベルの動的調整

3. **RAG連携**
   - Prismindとの自動連携
   - 関連知識の自動付与
   - 過去の会話・決定事項の参照

4. **MCP統合**
   - 複数MCPサーバーの統一インターフェース
   - ツール呼び出しの最適化
   - 結果のキャッシュ

### API エンドポイント

```
POST /orchestrate
  - request: オーケストレーションリクエスト
  - context: 現在のコンテキスト
  - options: 処理オプション

POST /route
  - query: ルーティング対象クエリ
  - available_services: 利用可能サービス一覧

POST /compress-context
  - context: 圧縮対象コンテキスト
  - target_size: 目標サイズ

GET /services
  - 登録サービス一覧

GET /health
  - ヘルスチェック
```

## 技術仕様

### 依存関係

| サービス | 用途 |
|----------|------|
| Spirrow-Lexora | LLM推論基盤 |
| Spirrow-Cognilens | 情報圧縮 |
| Spirrow-Prismind | RAG検索 |
| Spirrow-UnrealWise | UE5操作 |

### 技術スタック

- **言語**: Python 3.11+
- **フレームワーク**: FastAPI
- **非同期処理**: asyncio
- **キャッシュ**: Redis (オプション)

### ポート

- デフォルト: `8004`

## アーキテクチャ

```
                    ┌─────────────────────────────────────────┐
                    │           Spirrow-Magickit              │
                    │         (魔法の道具箱)                  │
                    ├─────────────────────────────────────────┤
                    │  ┌─────────────────────────────────┐    │
                    │  │     Orchestration Engine        │    │
 ユーザー/Claude    │  │  ├── Request Analyzer           │    │
        │           │  │  ├── Service Router             │    │
        ▼           │  │  ├── Context Manager            │    │
   ┌─────────┐      │  │  └── Response Aggregator        │    │
   │ Request │─────▶│  └─────────────────────────────────┘    │
   └─────────┘      │                  │                       │
                    │     ┌────────────┼────────────┐          │
                    │     ▼            ▼            ▼          │
                    │  ┌──────┐   ┌──────┐   ┌──────┐         │
                    │  │Cogni │   │Prism │   │Unreal│         │
                    │  │lens  │   │mind  │   │Wise  │         │
                    │  │Client│   │Client│   │Client│         │
                    │  └──────┘   └──────┘   └──────┘         │
                    └─────────────────────────────────────────┘
                              │         │         │
                              ▼         ▼         ▼
                    ┌──────────┐ ┌──────────┐ ┌──────────┐
                    │Cognilens │ │ Prismind │ │UnrealWise│
                    └──────────┘ └──────────┘ └──────────┘
                              │
                              ▼
                        ┌──────────┐
                        │  Lexora  │
                        │(LLM基盤) │
                        └──────────┘
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
    priority: 1
    
  cognilens:
    url: "http://localhost:8003"
    auto_compress: true
    compression_threshold: 4000  # tokens
    
  prismind:
    url: "http://localhost:8002"
    auto_rag: true
    rag_threshold: 0.7  # relevance score
    
  unrealwise:
    url: "http://localhost:8005"

orchestration:
  max_context_tokens: 8000
  compression_target: 0.5
  cache_ttl: 300  # seconds
  
routing:
  strategy: "intelligent"  # intelligent / round-robin / priority
  fallback_enabled: true
```

## ワークフロー例

### 1. 標準オーケストレーション

```
1. リクエスト受信
2. Prismindで関連知識検索
3. コンテキスト + 関連知識を統合
4. サイズ超過の場合、Cognilensで圧縮
5. 適切なサービスへルーティング
6. 結果を整形して返却
```

### 2. UE5操作フロー

```
1. 「敵キャラクターにAI追加して」リクエスト
2. Prismindで過去の類似実装を検索
3. 関連ナレッジをコンテキストに付与
4. UnrealWiseへルーティング
5. 実行結果を返却
6. 結果をPrismindに蓄積（学習）
```

## ユースケース

### 1. コンテキスト最適化
```
入力: 大量のMCP結果 + 会話履歴 (15,000トークン)
処理: 
  - 重要度スコアリング
  - Cognilensで段階圧縮
  - 最新コンテキストを優先
出力: 最適化されたコンテキスト (6,000トークン)
```

### 2. 知識強化リクエスト
```
入力: 「BehaviorTreeのベストプラクティスは？」
処理:
  - Prismindで関連ドキュメント検索
  - 過去の実装例を抽出
  - Lexoraで回答生成
出力: 知識に基づいた詳細な回答
```

### 3. マルチサービス連携
```
入力: 「敵AIを作成して、過去の実装を参考に」
処理:
  - Prismind: 過去のAI実装を検索
  - Cognilens: 大量の結果を圧縮
  - UnrealWise: Blueprint作成
出力: 統合された実行結果
```

## 今後の拡張

- [ ] プラグインシステム（カスタムサービス追加）
- [ ] ワークフロー定義機能（YAML/JSON）
- [ ] 実行履歴の可視化ダッシュボード
- [ ] A/Bテスト機能（ルーティング戦略比較）
- [ ] コスト最適化（LLM呼び出し削減）

---

*Document Version: 1.0*
*Last Updated: 2026-01-16*
