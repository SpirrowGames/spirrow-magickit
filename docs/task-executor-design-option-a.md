---
id: spirrow-magickit:task-executor-design-option-a
title: タスク自動実行機能 - 設計検討案A (Executor方式)
product: spirrow-magickit
type: design
status: archived
version: 1.0
created: 2026-02-08
last_verified: 2026-09-10
supersedes: []
related: [spirrow-magickit:smart-read-analyze-tool-design]
keywords: [Executor, タスクキュー, 自動実行, Claude API, セキュリティモデル, 設計検討案]
legacy_drive_id: [12lixODlL92Q8EhnwRx3bNb2OTd3YnpePrZbNOgChM74]
---

# タスク自動実行機能 - 設計検討案A (Executor方式)

## 概要

複数の実装タスクをキューイングし、バックグラウンドで自動実行する機能の設計案。
ユーザーは計画・設計に集中し、実装・テストはシステムが自動で処理する。

## 背景・課題

### 現状の問題
- Claude Codeは各操作（Bash, ファイル書き込み等）で権限確認が必要
- タスクを自動実行しても、権限確認で止まってしまう
- ユーザーが承認のために待機する必要があり、脳みそのリソースが遊ぶ

### 理想のワークフロー
```
プロジェクトA: 計画検討 → [自動実装+テスト] → 完了通知
                              ↓ 並行して
プロジェクトB: 計画検討 → [自動実装+テスト] → ...
```

## アーキテクチャ

### 全体構成
```
ユーザー ←→ Claude Code ←→ Magickit (計画・指示)
                               │
                               ▼
                          Task Queue
                               │
                               ▼
                     ┌─────────────────┐
                     │  Executor       │ ← 新コンポーネント
                     │  (自律実行)     │
                     └─────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
          ファイル操作      テスト実行      Git操作
```

### コンポーネント役割

| コンポーネント | 役割 |
|---------------|------|
| Claude Code | ユーザーとの対話、計画立案 |
| Magickit | タスク分割、キュー管理、オーケストレーション |
| Claude API | コード生成（高品質LLM） |
| Executor | ファイル操作・テスト実行（権限不要の実行環境） |
| Prismind | 状態・結果の記録 |

## 処理フロー

### 1. 計画フェーズ (Claude Code + ユーザー)
```
「この機能を実装して」
     ↓
タスク分割 → [T1: モデル追加] [T2: 関数実装] [T3: テスト]
     ↓
各タスクの仕様書作成 → キューに登録
```

### 2. 実行フェーズ (Executor - バックグラウンド)
```
Queue → Claude API (コード生成) → Executor (書き込み・テスト)
```

## タスク仕様の構造

```python
task = {
    "id": "T1",
    "project": "spirrow-magickit",
    "spec": """
        ## 目的
        research_and_summarize にキャッシュ機能を追加

        ## 対象ファイル
        src/magickit/mcp/tools/research.py

        ## 要件
        - 同じクエリは5分間キャッシュ
        - TTLはConfigurable
        - キャッシュキーはquery + project + max_tokens

        ## 既存コード
        (現在のファイル内容)
    """,
    "target_file": "src/magickit/mcp/tools/research.py",
    "allowed_commands": ["pytest"],
    "depends_on": []
}
```

## Executor実装イメージ

```python
async def execute_task(task):
    # 1. Claude API でコード生成 (強力なLLM)
    response = await claude_api.generate(
        prompt=f"以下の仕様に基づいてコードを生成:\n{task['spec']}"
    )
    generated_code = response.text

    # 2. ファイルに書き込み (単純な操作)
    write_file(task['target_file'], generated_code)

    # 3. テスト実行
    result = run_command("pytest", task['project'])

    # 4. 結果を記録
    await prismind.add_knowledge(
        content=f"タスク{task['id']}完了: {result}",
        project=task['project']
    )

    return result
```

## セキュリティモデル

| 制限 | 内容 |
|------|------|
| パス制限 | 許可されたディレクトリのみアクセス |
| コマンド制限 | ホワイトリスト形式で許可 |
| 時間制限 | タスクごとにタイムアウト |
| リソース制限 | CPU/メモリ上限 |
| ブランチ制限 | main直接pushは禁止、PR作成のみ |

## メリット・デメリット

### メリット
- ユーザーは計画・設計に集中できる
- 複数プロジェクトの並行作業が可能
- Claude APIの高品質なコード生成を活用

### デメリット
- Claude API利用によるコスト発生（従量課金）
- 新コンポーネント（Executor）の開発が必要
- Claude Code経由ではないため、セッション外の管理が必要

## 未解決の課題

1. **コスト管理**: 大量タスク実行時のAPI費用
2. **エラーハンドリング**: 生成コードが不正な場合の対処
3. **コンテキスト共有**: Executorにプロジェクトの文脈をどう渡すか
4. **レビュー機構**: 自動生成コードの品質担保

## ステータス

検討中 - 他の案との比較検討が必要

## 移行時の注記（2026-09-10）

Drive 原本（`12lixODlL92Q8EhnwRx3bNb2OTd3YnpePrZbNOgChM74`）の逐語移行。
逐語からの逸脱は無い（ホスト名・IP・サーバーパス・認証の姿勢のいずれも本書には現れない）。

### `status: archived` の理由 —— 案A は採られなかった

本書は末尾で自ら「検討中 - 他の案との比較検討が必要」と書いており、その比較の結果
**本書の Executor は作られていない**。`src/magickit/` に `executor` に当たる
コンポーネントは無い。

**ただし「何も作られなかった」のではない。別の形で一部が実現している。**

| 本書 | 現物 |
|---|---|
| タスク分割 + キュー管理 | `src/magickit/mcp/tools/execution.py` の `SpecExecutor`（`spec_executor_decompose` / `_next_task` / `_complete_task` / `_run` / `_status` / `_report` / `_finalize`） |
| コード生成に **Claude API** | **不一致** —— `LexoraAdapter` 経由。Lexora が LLM の front になっている |
| Executor がファイル書き込み・テスト実行 | **無い**。実行は Claude Code 側に残った |
| §セキュリティモデル の 5 制限 | **無い**（実行主体が居ないため置き場が無い） |
| 状態を Prismind に記録 | 一部。`_execution_sessions` は **in-memory dict** |

∴ 実現したのは「計画側」だけで、**本書の核心である「権限確認で止まらない実行環境」は作られていない**。
§背景・課題 が挙げた問題（Claude Code が各操作で権限確認を要する）は、Executor を作ることではなく
**権限モードの運用**で解かれている。

### それでも捨てずに移行した理由

**§セキュリティモデル の 5 制限が、後から別の場所で必要になっている。** 「main 直接 push は禁止、
PR 作成のみ」は、後に別経路で
[[spirrow-magickit:github-mcp-dispatcher-identity-and-merge-guard]] の merge-to-main ガードとして
実装された。**同じ制約に 2 度たどり着いている**という記録である。

§未解決の課題 4 点（コスト管理 / 生成コードが不正な場合 / コンテキスト共有 / レビュー機構）も、
このあと三者ループ（proposer / naysayer / implementer）が答えを出していく問いと重なる。
**案A が解けなかった問題が、そのまま次の設計の出発点になっている。**
