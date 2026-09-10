---
id: spirrow-magickit:github-mcp-dispatcher-identity-and-merge-guard
title: 'GitHub MCP ディスパッチャ: implementer/reviewer 分離 と merge-to-main ガード'
product: spirrow-magickit
type: design
status: active
version: 1.0
created: 2026-05-24
last_verified: 2026-09-10
supersedes: []
related: [spirrow-magickit:phanthand-integration-architecture, platform:infra-registry]
keywords: [github MCP, ディスパッチャ, PAT, identity, merge ガード, branch protection, fail-closed]
legacy_drive_id: [1xzW_zfgREHv8Y1JVdqkluD1avAg2Hre18MewCRP70_k]
---

# GitHub MCP ディスパッチャ: implementer/reviewer 分離 と merge-to-main ガード

最終更新: 2026-05-24 / 対象: `src/magickit/mcp/github_dispatch.py`

## 背景・文脈

SpirrowGames の GitHub アイデンティティを 3 アカウントに整理:

| ロール | アカウント | トークン権限 | サーバ配置 |
|---|---|---|---|
| 人間・Org オーナー | takayan0908 | (merge GO / Org 管理) | 置かない（手元専用） |
| implementer（代理実装者） | takahito-spirrowgames | Contents/PR/Issues RW, Metadata R | `GITHUB_MCP_PAT_IMPLEMENTER` |
| reviewer（naysayer / Claude.ai） | spirrowgames-ops | PR/Issues RW, **Contents read-only**, Metadata R | `GITHUB_MCP_PAT_REVIEWER` |

Magickit の github MCP ディスパッチャは、公式 github-mcp コンテナ（`127.0.0.1:8116`, toolsets `repos,issues,pull_requests`）の 35 ツールを `github` / `github_operations` の **2 ツールに集約**して中継する（コネクタが接続時にツール定義を固定するため、コンテキスト節約目的）。従来は単一 `GITHUB_MCP_PAT` で全 operation を実行しており、commit も PR review も同一 identity だった。これが **PR #67 の「自分の PR に自分で formal review → 422 Unprocessable Entity」** の原因。

> 注: 当初の指示書が前提とした `mindwire` サービス / `MINDWIRE_*` 変数 / `NaysayerPrReviewAdapter` は実機に存在しない。GitHub を触る実体は (1) この Magickit github MCP（`GITHUB_MCP_PAT`）と (2) thirdy の `GITHUB_TOKEN`（別系統・docker・スコープ外）のみ。

## 決定1: identity ルーティング（operation 単位）

`github(operation, arguments)` の `operation` 名で、上流に転送する PAT を選ぶ。

| operation | 使用 PAT | identity |
|---|---|---|
| `pull_request_review_write`, `add_comment_to_pending_review` | `GITHUB_MCP_PAT_REVIEWER` | spirrowgames-ops（Contents RO） |
| 上記以外（commit / push / PR 作成 / merge / 読み取り / スキーマ照会 / health） | `GITHUB_MCP_PAT_IMPLEMENTER` | takahito-spirrowgames（Contents RW） |

- role 別 PAT が未設定なら legacy `GITHUB_MCP_PAT` にフォールバック → 単一 PAT 運用は無改変で動作（段階移行可）。
- allowlist 方式（reviewer 側のみ明示列挙）。新ツールが増えても未列挙なら自動的に implementer 扱い＝安全側デフォルト。**ただし新しい review-submit 系ツールが出たら `_REVIEWER_OPS` に手動追記が必要**（カタログコメントに同期注意あり）。
- 効果: PR を立てたアカウントが自分の PR に formal review を送る事故（422）を回避。
- **限界**: この分離がどこまでの強度かは [[platform:infra-registry]] §5 にある（規約 §3.1-4 により本書には書かない）。

## 決定2: merge-to-main ガード（merge to main = 人間 GO）

`merge_pull_request` の引数は `owner/repo/pullNumber` のみで **base ブランチ名を含まない**。そのため転送前に `pull_request_read(method=get)` で PR の `base.ref` を引き、保護ブランチ（既定 `main`、`GITHUB_PROTECTED_BASE_BRANCHES` でカンマ区切り可変）宛なら **上流に転送せず policy block** を返す。develop 等への merge は通過。base を判定できない（引数欠落 / lookup 失敗）ときは **fail-closed** で拒否。

採用理由（実機検証で確定）:
- `_classify_mcp` / `GIT_MERGE_TO_MAIN` / Tier C allowlist は**実機に実装されていない**（過去会話の設計議論にのみ存在）。
- ディスパッチャが 35 ツールを `github` 1 つに畳むため、コネクタ/Claude Code の **per-tool 権限では `operation` 単位で deny できない**。
- **このガードが唯一の防御線である理由**（GitHub 側に何が無いか）は [[platform:infra-registry]] §5（規約 §3.1-4）。
- → 対策として **ディスパッチャ自身が正しい層**で merge を止める。merge の identity は implementer のままで、これとは独立した policy 層。

## 実装・テスト・デプロイ状態

- コード: `github_dispatch.py`（`_pat_for_operation` / `_resolve_pat` / `_pr_base_ref` / `_merge_block_reason` / `_execute_operation`）。
- テスト: `tests/unit/test_github_dispatch.py` 34 件（identity ルーティング、fallback、role 欠如→`_UpstreamError`、merge into main → block & 非転送、develop → 通過、base 不明 → fail-closed、保護ブランチ可変）。unit 全体 326 緑、ruff 緑。実機 live でも base=main PR の block・merge 非転送を確認。
- 秘密: `/etc/spirrow-magickit/github.env`（`{{USER_SERVICES}}:{{USER_SERVICES}} 0600`）に implementer/reviewer の 2 PAT。legacy `GITHUB_MCP_PAT` 行は削除。`spirrow-magickit-mcp.service` の drop-in `github.conf` が EnvironmentFile で注入。**どの unit に注入し、どの unit に注入しないか**は [[platform:infra-registry]] §5。
- identity 検証: read-only `/user` で implementer→takahito-spirrowgames、reviewer→spirrowgames-ops（取り違え無し）。
- デプロイ: identity 分離は main（commit 4a2a67b）で **稼働中**。merge ガードは branch `feat/github-block-merge-to-main`（commit dc8046c）＋ docs（630e48a）で **未マージ・未デプロイ**（マージ + restart で有効化）。

## 既知の限界・フォローアップ

1. **reviewer の Contents read-only が実証できていない件**は [[platform:infra-registry]] §5。次回の実 PR ワークフローで実証する。
2. `_REVIEWER_OPS` の手動同期が単一障害点（新 review-submit ツール追加時の追記漏れに注意）。
3. `add_reply_to_pull_request_comment` は implementer 側に置いている（実装者も使う会話的操作のため）。naysayer がレビュー中の行内コメントに返信すると takahito 名義になる小さなズレ。reviewer 一貫が必要なら `_REVIEWER_OPS` に 1 行追加で切替可能。
4. `GIT_AUTHOR_*` env は github MCP の API 経由コミットには無効。commit author の takahito 固定は **implementer PAT = takahito であること自体**で達成。
5. 真のプロセス/ファイル分離が必要になったら、implementer / reviewer をそれぞれ別ディスパッチャ・別 unit・別 EnvironmentFile の 2 インスタンスに分ける。

## 移行時の注記（2026-09-10）

Drive 原本（`1xzW_zfgREHv8Y1JVdqkluD1avAg2Hre18MewCRP70_k`）の移行。

### 逐語からの逸脱

| 箇所 | 対応 | 根拠 |
|---|---|---|
| §実装・テスト・デプロイ状態 の秘密ファイル所有者 | `{{USER_SERVICES}}` に置換 | 規約 §3.1-2 |
| **§決定1 の「限界」** | [[platform:infra-registry]] §5 への参照に置換 | **§3.1-4（置換ではなく移動）** |
| **§決定2 の採用理由 3 点目** | 同上 | 同上 |
| **§実装 の EnvironmentFile 注入先の但し書き** | 同上 | 同上 |
| **§既知の限界 1.** | 同上 | 同上 |

**移した内容の要点**: この repo の `main` に GitHub 側の保護が掛かっていないこと、2 つの PAT が同一プロセスの環境変数に同居していること、reviewer の権限縮小がまだ実証されていないこと。**いずれも「この経路は素通りできる」型の情報**で、public repo に置いたままにすると規約 §3.1-4 に真正面から抵触する。台帳側には根拠ごと記録してある。

ポート `8116`（github-mcp コンテナ）は実値のまま（§3.1-3）。台帳 §3 に登録済み。
アカウント名 3 つは GitHub 上の公開 identity なので伏せていない（台帳 §4 と同じ扱い）。

### 設計は実装と一致している（2026-09-10 照合）

| 本書 | 現物（`src/magickit/mcp/github_dispatch.py`） |
|---|---|
| operation 単位の PAT 選択 | `_pat_for_operation`（`_REVIEWER_OPS` に無ければ implementer） |
| reviewer allowlist | `_REVIEWER_OPS = frozenset(...)` |
| 保護ブランチ既定 `main`、env で可変 | `_DEFAULT_PROTECTED_BASE = "main"` / `_PROTECTED_BASE_ENV = "GITHUB_PROTECTED_BASE_BRANCHES"` |
| merge 前に base を引く / 判定不能なら fail-closed | `_pr_base_ref` / `_merge_block_reason` |

**∴ §デプロイ の「merge ガードは未マージ・未デプロイ」は古い。** 現在の `main` に入っている。
`GITHUB_PROTECTED_BASE_BRANCHES` は呼び出しごとに `os.environ` から読み直す実装なので、
再起動なしで保護ブランチを増やせる。
