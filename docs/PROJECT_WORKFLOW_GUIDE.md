# Magickit プロジェクトワークフローガイド

Magickitを活用したゲーム開発プロジェクトの進行例です。

## 目次

1. [プロジェクト立ち上げ](#1-プロジェクト立ち上げ)
2. [日常の開発フロー](#2-日常の開発フロー)
3. [タスク管理](#3-タスク管理)
4. [進捗管理・予測](#4-進捗管理予測)
5. [フェーズ遷移](#5-フェーズ遷移)
6. [レポート・振り返り](#6-レポート振り返り)
7. [プロジェクト完了](#7-プロジェクト完了)

---

## 1. プロジェクト立ち上げ

### 1.1 プロジェクト初期化

```
ユーザー: 新しいゲームプロジェクト「SpaceRogue」を始めたい
```

Claude は以下を実行:

```python
# プロジェクト作成（ゲームテンプレート使用）
init_project(
    project="space-rogue",
    template="game",
    name="SpaceRogue",
    description="ローグライクスペースシューター"
)
```

**テンプレートで自動設定される内容:**
- カテゴリ: design, implementation, asset, bug, decision
- フェーズ: pre-production, production, polish, release

### 1.2 マイルストーン設定

```python
# マイルストーン追加
add_milestone(
    project="space-rogue",
    name="Prototype",
    target_date="2024-02-15",
    phase="pre-production",
    description="基本メカニクスの動作確認"
)

add_milestone(
    project="space-rogue",
    name="Alpha",
    target_date="2024-04-01",
    phase="production",
    description="コアループ完成、内部テスト開始"
)

add_milestone(
    project="space-rogue",
    name="Beta",
    target_date="2024-06-01",
    phase="polish",
    description="全機能実装、外部テスト開始"
)

add_milestone(
    project="space-rogue",
    name="Release",
    target_date="2024-07-15",
    phase="release",
    description="正式リリース"
)
```

### 1.3 品質ゲート定義

```python
# Pre-production完了条件
define_quality_gate(
    project="space-rogue",
    phase="pre-production",
    criteria=[
        {"type": "task_completion", "threshold": 90},
        {"type": "no_critical_blockers"},
        {"type": "milestone_achieved", "milestone": "Prototype"}
    ],
    name="Pre-production Gate",
    description="Production移行の条件"
)

# Production完了条件
define_quality_gate(
    project="space-rogue",
    phase="production",
    criteria=[
        {"type": "task_completion", "threshold": 85},
        {"type": "no_blockers"},
        {"type": "milestone_achieved", "milestone": "Alpha"}
    ]
)
```

---

## 2. 日常の開発フロー

### 2.1 セッション開始

毎回の作業開始時に前回のコンテキストを復元:

```
ユーザー: SpaceRogueの開発を再開したい
```

```python
# コンテキスト復元
resume(
    project="space-rogue",
    detail_level="standard",  # minimal / standard / full
    task_description="敵AIシステムの実装"
)
```

**返却される情報:**
- `last_summary`: 前回セッションのサマリー
- `next_action`: 推奨される次のアクション
- `current_phase`: 現在のフェーズ
- `current_task`: 作業中のタスク
- `blockers`: 既知のブロッカー
- `context`: 関連知識（圧縮済み）

### 2.2 作業中の中間保存

重要な決定や進捗があった場合:

```python
# チェックポイント保存
checkpoint(
    summary="敵AIの基本行動パターン実装完了",
    project="space-rogue",
    decisions=[
        "ステートマシンではなくBehavior Treeを採用",
        "敵タイプごとにBTを分離する設計に決定"
    ],
    current_phase="production",
    current_task="T05: 敵AIシステム",
    next_action="パトロール行動の実装"
)
```

### 2.3 軽量な進捗更新

タスク完了時など:

```python
# 進捗更新（軽量版）
update_progress(
    project="space-rogue",
    completed_task="T05: 敵AIシステム",
    current_task="T06: ボスAI実装"
)
```

### 2.4 セッション終了

作業終了時の引き継ぎ:

```python
# ハンドオフ
handoff(
    next_action="ボスAIの攻撃パターン3種を実装する",
    project="space-rogue",
    summary="敵AIシステム完成。Behavior Tree + Blackboard構成。",
    notes="参考: Enemy/AI/BT_EnemyBase.uasset",
    blockers=["ボスの第3形態デザインが未確定"],
    save_insights=True  # 学びをknowledgeに保存
)
```

### 2.5 ロール（author）別コンテキスト

複数のロール（例: 設計担当 `claude.ai` と実装担当 `claude-code`）が
同じプロジェクトで**独立した引き継ぎ**を持ちたい場合、
`resume` / `checkpoint` / `handoff` / `update_progress` に `author` を指定する。
1つの project に対し author ごとに独立したコンテキストが保存・復元される
（保存キー: `prismind:session:{project}:{user}:{author}`、`author` 空はデフォルト）。

```python
# まず保存済み author を確認（表記揺れ重複防止 / 自分の context 有無確認）
list_context_authors(project="space-rogue")
# -> {"authors": [
#      {"author": "claude.ai",   "current_task": "T05: 設計", "updated_at": "..."},
#      {"author": "claude-code", "current_task": "T06: 実装", "updated_at": "..."}
#    ], "total_count": 2}

# 自分のロールで保存
checkpoint(
    summary="敵AI設計レビュー完了",
    project="space-rogue",
    current_task="T05: 敵AI設計",
    author="claude.ai"
)

# 同じ author で復元（他ロールの context とは干渉しない）
resume(project="space-rogue", author="claude.ai")
```

> **注意**: 新しい author 名を作る前に必ず `list_context_authors` で既存名を確認し、
> `claude-code` と `claude_code` のような表記揺れによる重複を避けること。

---

## 3. タスク管理

### 3.1 タスク追加

```python
# タスク追加（自動ID生成）
add_task(
    name="プレイヤー移動システム",
    description="WASD移動、ダッシュ、宇宙空間での慣性制御",
    phase="production",
    priority="high",
    category="implementation",
    project="space-rogue"
)
# → T01が自動割り当て

add_task(
    name="武器システム基盤",
    description="武器の基底クラス、発射・リロード・弾薬管理",
    phase="production",
    priority="high",
    category="implementation",
    blocked_by=["T01"],  # プレイヤー移動に依存
    project="space-rogue"
)
# → T02が自動割り当て
```

### 3.2 タスク一覧と推奨

```python
# タスク一覧（推奨タスク付き）
list_tasks(
    project="space-rogue",
    phase="production",
    status="not_started"
)
```

**返却例:**
```json
{
  "tasks": [...],
  "recommended_task": {
    "task_id": "T01",
    "name": "プレイヤー移動システム",
    "reason": "高優先度、依存なし、ブロックなし"
  },
  "stats": {
    "total": 15,
    "not_started": 10,
    "in_progress": 3,
    "completed": 2,
    "blocked": 0
  }
}
```

### 3.3 タスク開始・完了

```python
# タスク開始（依存関係チェック付き）
start_task(
    task_id="T01",
    project="space-rogue"
)
# → 関連コンテキストと依存タスクの完了情報を取得

# タスク完了（学びを記録）
complete_task(
    task_id="T01",
    project="space-rogue",
    notes="Enhanced Input Systemで実装",
    learnings="宇宙空間の慣性はLinear Dampingで調整が効果的"
)
# → 次の推奨タスクと、新たにアンブロックされたタスクを通知
```

### 3.4 タスクブロック

```python
# ブロッカー記録（影響分析付き）
block_task(
    task_id="T08",
    reason="ボス第3形態のデザインが未確定",
    blocked_by=["アートチームの確認待ち"],
    project="space-rogue"
)
# → 影響を受ける下流タスクの一覧を表示
```

---

## 4. 進捗管理・予測

### 4.1 バーンダウンチャート

```python
# バーンダウンデータ取得
get_burndown(
    project="space-rogue",
    phase="production",
    days=14
)
```

**返却例:**
```json
{
  "data_points": [
    {"date": "2024-03-01", "remaining": 15},
    {"date": "2024-03-02", "remaining": 14},
    {"date": "2024-03-03", "remaining": 12},
    ...
  ],
  "total_tasks": 15,
  "completed_tasks": 8,
  "remaining_tasks": 7,
  "ideal_burndown": [...],
  "current_velocity": 1.2
}
```

### 4.2 完了予測

```python
# 完了日予測
estimate_completion(
    project="space-rogue",
    phase="production"
)
```

**返却例:**
```json
{
  "estimated_date": "2024-03-25",
  "days_remaining": 12,
  "remaining_tasks": 7,
  "current_velocity": 0.8,
  "confidence": "medium",
  "factors": [
    "週末を除外",
    "直近7日のベロシティを使用",
    "ブロッカー1件が解消されれば加速の可能性"
  ]
}
```

### 4.3 ベロシティ記録

作業日の終わりに記録:

```python
# ベロシティ追跡
track_velocity(
    project="space-rogue",
    completed_today=2,
    notes="武器システムとUI基盤を完了"
)
```

### 4.4 リスク指標

```python
# リスク分析
get_risk_indicators(project="space-rogue")
```

**返却例:**
```json
{
  "overall_risk": "medium",
  "risk_score": 45,
  "indicators": [
    {
      "type": "blocked_ratio",
      "severity": "low",
      "value": 6.7,
      "threshold": 20,
      "message": "ブロックタスク比率: 6.7%"
    },
    {
      "type": "velocity_trend",
      "severity": "medium",
      "value": -15,
      "message": "ベロシティが15%低下傾向"
    },
    {
      "type": "milestone_delay",
      "severity": "high",
      "value": 3,
      "message": "Alphaマイルストーンが3日遅延リスク"
    }
  ],
  "recommendations": [
    "T08のブロッカーを優先解消",
    "Alphaスコープの再確認を推奨"
  ]
}
```

---

## 5. フェーズ遷移

### 5.1 フェーズ状況確認

```python
# 現在フェーズの詳細
get_phase_status(
    project="space-rogue",
    phase="pre-production"
)
```

**返却例:**
```json
{
  "phase": "pre-production",
  "is_current": true,
  "stats": {
    "total": 8,
    "completed": 7,
    "in_progress": 1,
    "blocked": 0,
    "completion_percent": 87.5
  },
  "tasks": [...],
  "blockers": [],
  "in_progress": [{"task_id": "T03", "name": "プロトタイプ調整"}]
}
```

### 5.2 品質ゲートチェック

```python
# フェーズ完了条件確認
check_quality_gate(
    project="space-rogue",
    phase="pre-production"
)
```

**返却例:**
```json
{
  "passed": true,
  "phase": "pre-production",
  "results": [
    {"criterion": "task_completion >= 90%", "passed": true, "actual": 92},
    {"criterion": "no_critical_blockers", "passed": true},
    {"criterion": "milestone: Prototype", "passed": true}
  ],
  "passed_count": 3,
  "failed_count": 0,
  "message": "全ての品質ゲート条件を満たしています"
}
```

### 5.3 フェーズ遷移

```python
# 次フェーズへ進行
advance_phase(
    project="space-rogue",
    completion_threshold=80  # 80%以上で遷移可能
)
```

**返却例:**
```json
{
  "success": true,
  "previous_phase": "pre-production",
  "current_phase": "production",
  "completion_stats": {
    "completed": 8,
    "total": 8,
    "percent": 100
  },
  "message": "フェーズをpre-productionからproductionに進行しました"
}
```

### 5.4 マイルストーン確認

```python
# マイルストーン状況
check_milestone_status(project="space-rogue")
```

**返却例:**
```json
{
  "milestones": [
    {"name": "Prototype", "status": "completed", "actual_date": "2024-02-14"},
    {"name": "Alpha", "status": "in_progress", "days_remaining": 15, "on_track": true},
    {"name": "Beta", "status": "pending", "target_date": "2024-06-01"},
    {"name": "Release", "status": "pending", "target_date": "2024-07-15"}
  ],
  "at_risk": [],
  "overdue": [],
  "on_track": 2
}
```

---

## 6. レポート・振り返り

### 6.1 ステータスレポート

週次ミーティングやステークホルダー報告用:

```python
# ステータスレポート生成
generate_status_report(
    project="space-rogue",
    format="markdown",
    include_tasks=True,
    include_milestones=True,
    include_risks=True
)
```

**出力例:**
```markdown
# SpaceRogue ステータスレポート
生成日: 2024-03-15

## サマリー
- 現在フェーズ: production
- 全体進捗: 45%
- リスクレベル: 低

## マイルストーン
| マイルストーン | 目標日 | ステータス |
|---------------|--------|-----------|
| Prototype | 2024-02-15 | ✅ 完了 |
| Alpha | 2024-04-01 | 🔄 進行中 |
| Beta | 2024-06-01 | ⏳ 予定 |

## 今週の成果
- プレイヤー移動システム完成
- 武器システム基盤実装
- 敵AI基本行動パターン実装

## ブロッカー
- ボス第3形態デザイン未確定（アート待ち）

## 来週の予定
- ボスAI実装
- ステージ生成システム
```

### 6.2 リリースノート

```python
# リリースノート自動生成
generate_release_notes(
    project="space-rogue",
    version="v0.1.0-alpha",
    from_phase="production",
    include_tasks=True
)
```

**出力例:**
```markdown
# SpaceRogue v0.1.0-alpha リリースノート

## 新機能
- プレイヤー移動システム（WASD + ダッシュ + 慣性制御）
- 武器システム（レーザー、ミサイル）
- 敵AI（パトロール、追跡、攻撃）

## 改善
- 宇宙空間の物理挙動調整
- UI応答性向上

## バグ修正
- ダッシュ中の当たり判定修正
- 弾薬カウント表示の不具合修正
```

### 6.3 振り返り分析

フェーズ完了時やプロジェクト終了時:

```python
# パフォーマンス分析
analyze_project_performance(
    project="space-rogue",
    use_llm=True  # LLMによるナラティブ分析
)
```

**返却例:**
```json
{
  "insights": [
    "Pre-productionは予定通り完了",
    "Productionで15%のベロシティ低下が発生",
    "ブロッカーの平均解消時間: 2.3日"
  ],
  "metrics": {
    "total_tasks_completed": 25,
    "average_velocity": 1.1,
    "blocker_resolution_time_avg": 2.3,
    "on_time_milestones": 1,
    "delayed_milestones": 0
  },
  "recommendations": [
    "アートチームとの連携強化を推奨",
    "依存関係の事前確認プロセスの導入",
    "週次のリスクレビュー実施"
  ],
  "narrative": "SpaceRogueプロジェクトは全体として順調に進行しています。Pre-productionフェーズは計画通り完了し、Prototypeマイルストーンも達成しました。Productionフェーズではベロシティの一時的な低下が見られましたが、これは新規技術（Behavior Tree）の学習コストによるものと分析されます。今後はアートチームとの連携を強化し、デザイン確定の遅延を防ぐことが推奨されます。"
}
```

---

## 7. プロジェクト完了

### 7.1 最終レポート

```python
# 最終ステータスレポート
generate_status_report(
    project="space-rogue",
    format="markdown",
    include_tasks=True,
    include_milestones=True,
    include_risks=False  # 完了時はリスク不要
)

# 全バージョンのリリースノート
generate_release_notes(
    project="space-rogue",
    version="v1.0.0",
    from_phase="pre-production",  # 全フェーズ
    include_tasks=True
)
```

### 7.2 振り返り

```python
# 最終パフォーマンス分析
analyze_project_performance(
    project="space-rogue",
    use_llm=True
)
```

### 7.3 アーカイブ

```python
# プロジェクトアーカイブ（データ保持）
delete_project(
    project="space-rogue",
    mode="archive"  # archive / archive_and_delete / permanent
)
```

---

## 付録: よく使うコマンドパターン

### 朝の作業開始
```python
resume(project="space-rogue", detail_level="standard")
list_tasks(project="space-rogue", status="not_started")
```

### 作業中
```python
start_task(task_id="T05", project="space-rogue")
# ... 作業 ...
checkpoint(summary="進捗メモ", project="space-rogue")
```

### 作業終了
```python
complete_task(task_id="T05", project="space-rogue", learnings="学んだこと")
track_velocity(project="space-rogue", completed_today=1)
handoff(next_action="次にやること", project="space-rogue")
```

### 週次確認
```python
get_burndown(project="space-rogue", days=7)
check_milestone_status(project="space-rogue")
get_risk_indicators(project="space-rogue")
generate_status_report(project="space-rogue")
```

### フェーズ完了時
```python
check_quality_gate(project="space-rogue")
advance_phase(project="space-rogue")
generate_release_notes(project="space-rogue", version="vX.X.X")
analyze_project_performance(project="space-rogue")
```

---

*Document Version: 1.0*
*Last Updated: 2026-02-03*
