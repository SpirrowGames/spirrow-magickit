---
id: spirrow-magickit:phanthand-integration-architecture
title: Phanthand Integration Architecture
product: spirrow-magickit
type: design
status: active
version: 1.0
created: 2026-02-08
last_verified: 2026-09-10
supersedes: []
related: [spirrow-magickit:phanthand-adapter-api-design, spirrow-magickit:smart-read-analyze-tool-design, spirrow-phanthand:design-v0.2]
keywords: [Phanthand, Adapter, Cognilens, smart_read, smart_analyze, コンテキスト圧縮, ステートレス]
legacy_drive_id: [1ljnadmAfdMdT9Uz3EkYzynX_Aoq-_UN3joAJD4AWKDo, 16WMhvjnnQtvonkVgIK7Fz0v7NjmTC3iElZMIKVls4dI]
---

# Phanthand Integration Architecture

## 概要

Phanthand（開発PCファイルアクセスAPI）をMagickitの新しいAdapterとして統合し、
smart_read / smart_analyze ツールを通じてCognilensの圧縮・要約機能を活用する。

## 背景・課題

ClaudeはReadツールでファイルを直接読み込むため、コンテキストウィンドウを大量消費する。
特にリモート構成では、開発PCのファイルをCognilensで処理するパスが存在しない。

## アーキテクチャ

```
Claude Code (開発PC)
    │ smart_read(files=["src/big.py"], phanthand_url="http://...", ...)
    ▼
Magickit (:8004 リモートサーバ)
    │
    ├── PhanthandAdapter → Phanthand (開発PC :7300)  ← ファイル取得
    ├── CognilensAdapter → Cognilens (:8003)         ← 圧縮・要約
    ├── LexoraAdapter → Lexora (:8001)               ← 分析（smart_analyze用）
    └── PrismindAdapter → Prismind (:8002)           ← 知識保存（オプション）
    │
    ▼ 圧縮結果のみ返却
Claude Code ← コンテキストには圧縮結果のみ
```

## 設計原則

### PhanthandAdapterは独立クラス

- BaseAdapter/MCPBaseAdapterを継承しない
- 理由: 既存Adapterは「1サーバ:1接続先」だが、Phanthandは「開発者の数だけインスタンスがある」
- URL/API keyはメソッド呼び出し時に引数で受け取る（設定ファイル不要）
- Magickitはステートレスのまま

### ツール分離

- smart_read: ファイル単位の読み込み+Cognilens処理。常にファイル単位の結果を返す。
- smart_analyze: ファイル横断の統合分析。常に統合された1つの結果を返す。
- 理由: モードで挙動が変わるとAIが結果の形を予測しにくい。ツール自体で責務を分ける。

### クライアントから接続情報を渡す

- phanthand_url, phanthand_api_keyは毎回ツール呼び出し時に指定
- 利用者はAI（Claude）想定のため冗長さはデメリットにならない
- マルチユーザー環境で各開発者が自分のPhanthandを指定できる

## Phase 1 スコープ

- PhanthandAdapter（ファイルAPI全6エンドポイント対応）
- smart_read（raw/summarize/essence/compress モード）
- smart_analyze（glob展開+統合分析+Lexora回答）
- キャッシュなし（Phase 2で検討）

## Phase 2 検討事項（将来）

- Prismindキャッシュ + ファイル変更検知による更新追従
- Codebase Indexer（workspace_sync）
- DevAgent拡張（テスト実行、ビルド等）

## 移行時の注記（2026-09-10）

Drive 原本の逐語移行。**この文書は Drive 上に 2 部あった** ——
`Spirrow Magickit` フォルダ（`1ljnadmA...`）と `Magickit Phanthand Integration` フォルダ
（`16WMhvjn...`）。題名は同一、サイズは 2852 / 2850。規約 §2.0 に従い
**両方の fileId を `legacy_drive_id` に併記**してある（片方だけ記録すると、もう一方の
旧 ID からの解決が効かない）。

### 移行先を `spirrow-magickit` にした理由

[[platform:reconciliation-small-projects]] §4.2 は「移行先が `spirrow-magickit` か
`spirrow-phanthand` かは未決」としていた。**両 repo を grep して決めた** —— §4.1 で
効いた手順と同じ:

| repo | `PhanthandAdapter` の出現 |
|---|---|
| `spirrow-magickit` | `src/magickit/adapters/phanthand.py` / `adapters/__init__.py` / `mcp/tools/smart_read.py` / `tests/unit/test_phanthand_adapter.py` / `docs/mcp-tools.md` |
| `spirrow-phanthand` | **0 件** |

本書が設計しているのは「Magickit が Phanthand をどう呼ぶか」であって、Phanthand 自身の
設計ではない。Phanthand 側の設計書は [[spirrow-phanthand:design-v0.2]] に別途ある。

### 逐語からの逸脱

無い。ホスト名・IP・サーバーパス・認証の姿勢のいずれも本書には現れず、ポートは実値のまま
（規約 §3.1-3）。

### ポート番号を現状として読まないこと

§アーキテクチャ の図が書く 4 つのポートは **Magickit の設定ファイルの既定値**であって、
稼働中の値ではない:

| 図 | `src/magickit/config.py` の既定 | [[platform:infra-registry]] §3 |
|---|---|---|
| Magickit `:8004` | — | `8114`（MCP）/ `8117`（chatroom MCP） |
| Cognilens `:8003` | `cognilens_url = http://localhost:8003` | **未登録** |
| Lexora `:8001` | `lexora_url = http://localhost:8001` | `8110` |
| Prismind `:8002` | `prismind_url = http://localhost:8002` | `8112`（mcp-proxy / SSE） |

台帳の `8110` / `8112` は proxy 側、config の `8001` / `8002` は直結側、という読みが自然だが
**実測していない**。Phanthand の `:7300` だけは両者一致している。

### 実装との対応（2026-09-10 照合）

§Phase 1 スコープは 3 項目とも実在する:

| 本書 | 現物 |
|---|---|
| PhanthandAdapter（6 エンドポイント対応） | `src/magickit/adapters/phanthand.py` に `read_file` / `list_directory` / `file_exists` / `file_info` / `tree` / `search` + `health_check` |
| smart_read（4 モード） | `src/magickit/mcp/tools/smart_read.py` の `VALID_MODES` |
| smart_analyze（glob 展開 + 統合分析 + Lexora 回答） | 同ファイル |

**§設計原則 の 3 つは、コードを読んでも意図が出てこない部分である。** とくに
「BaseAdapter を継承しない」は、`adapters/phanthand.py` の docstring が
"Unlike other adapters, PhanthandAdapter does NOT inherit from BaseAdapter" と
**事実だけ**述べていて、理由（「Phanthand は開発者の数だけインスタンスがある」）は
本書にしかない。

§Phase 2 検討事項（Prismind キャッシュ / Codebase Indexer / DevAgent 拡張）は
**いずれも未実装**。
