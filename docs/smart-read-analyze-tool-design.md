---
id: spirrow-magickit:smart-read-analyze-tool-design
title: smart_read / smart_analyze Tool Design
product: spirrow-magickit
type: spec
status: active
version: 1.0
created: 2026-02-08
last_verified: 2026-09-10
supersedes: []
related: [spirrow-magickit:phanthand-integration-architecture, spirrow-magickit:phanthand-adapter-api-design]
keywords: [smart_read, smart_analyze, Cognilens, Lexora, glob展開, コンテキスト節約, MCP tool]
legacy_drive_id: [1LgD4u3LiNjkSEuQ1ISnc7TZC7X8uHtmHdfRGp5Z7k6c, 1gA_ofj8ap0-QVLbbkMXsZcQyRMdJejZR1yCj6pMrRH0]
---

# smart_read / smart_analyze Tool Design

## 配置

`src/magickit/mcp/tools/smart_read.py`

両ツールを同一ファイルに配置。`register_tools(mcp, settings)` で登録。

---

## smart_read

### 目的

開発PCのファイルをPhanthand経由で読み込み、Cognilensで処理してClaudeに返す。
Claudeのコンテキストには圧縮/要約結果のみが載る。

### シグネチャ

```python
async def smart_read(
    files: list[str],           # 読み込むファイルパス（絶対パス）
    mode: str = "summarize",    # 処理モード
    focus: str = "",            # 注目ポイント（essence/compressで有効）
    phanthand_url: str = "",    # Phanthand URL（必須）
    phanthand_api_key: str = "", # Phanthand API key（必須）
    project: str = "",          # プロジェクトID
    user: str = "",             # ユーザー
) -> dict:
```

### 処理モード

| モード | Cognilens機能 | ユースケース | focus使用 |
|--------|--------------|-------------|-----------|
| `raw` | なし | 小さいファイルをそのまま読む | No |
| `summarize` | summarize | 概要把握 | No |
| `essence` | extract_essence | 設計パターン・API構造の抽出 | Yes (focus_areas) |
| `compress` | compress | コンテキスト節約のための圧縮 | Yes (preserve) |

### 処理フロー

```
1. パラメータバリデーション（phanthand_url, files 必須チェック）
2. for each file in files:
   a. PhanthandAdapter.read_file(url, api_key, file_path)
   b. mode に応じた Cognilens 処理:
      - raw: そのまま返却
      - summarize: CognilensAdapter.summarize(content)
      - essence: CognilensAdapter.extract_essence(content, focus_areas=[focus])
      - compress: CognilensAdapter.compress(content, preserve=focus)
   c. 結果をリストに追加
3. 結果リストを返却
```

### レスポンス

```json
{
    "success": true,
    "mode": "essence",
    "results": [
        {
            "file": "src/auth.py",
            "size": 15234,
            "processed": "...Cognilens処理結果...",
            "mode": "essence"
        },
        {
            "file": "src/middleware.py",
            "size": 8421,
            "processed": "...Cognilens処理結果...",
            "mode": "essence"
        }
    ],
    "file_count": 2,
    "errors": []
}
```

### エラーハンドリング

- ファイル単位でエラーを収集（1ファイルの失敗で全体を止めない）
- Phanthand接続エラーは即座にエラー返却（全ファイル読めないため）
- Cognilens接続エラーはフォールバックでraw返却を検討

---

## smart_analyze

### 目的

複数ファイルを横断的に分析し、統合された回答を返す。
Cognilens + Lexoraのパイプライン。

### シグネチャ

```python
async def smart_analyze(
    files: list[str],            # ファイルパスまたはglobパターン
    question: str,               # 分析の質問
    phanthand_url: str = "",     # Phanthand URL（必須）
    phanthand_api_key: str = "", # Phanthand API key（必須）
    search_root: str = "",       # glob展開時のルートディレクトリ
    max_files: int = 20,         # 最大ファイル数（コスト制御）
    project: str = "",           # プロジェクトID
    save_to_knowledge: bool = False, # Prismindに分析結果を保存
    user: str = "",              # ユーザー
) -> dict:
```

### 処理フロー

```
1. パラメータバリデーション
2. ファイルリスト解決:
   a. 絶対パスはそのまま使用
   b. globパターン（*.py等を含む）→ PhanthandAdapter.search() で展開
   c. max_filesで上限チェック
3. 各ファイルをPhanthandAdapter.read_file()で読み込み
4. CognilensAdapter.unify_summaries() で統合要約を生成
5. LexoraAdapter.generate() で質問に回答:
   - prompt: 統合要約 + question
6. オプション: PrismindAdapterで分析結果をknowledgeに保存
7. 結果を返却
```

### レスポンス

```json
{
    "success": true,
    "question": "エラーハンドリングのパターンは？",
    "answer": "...Lexoraによる分析回答...",
    "files_analyzed": ["src/api/auth.py", "src/api/users.py", "src/api/errors.py"],
    "file_count": 3,
    "summary": "...Cognilens統合要約...",
    "knowledge_saved": false,
    "errors": []
}
```

### glob展開の判定

パスに `*`, `?`, `[` を含む場合はglobパターンとして扱う:
- `src/api/*.py` → search_root + pattern
- `src/auth.py` → 絶対パスとして直接読み込み
- 混在可: `["src/auth.py", "src/api/*.py"]`

### ファイル数制限

- デフォルト max_files=20
- glob展開で大量ヒットした場合はtruncateして警告
- 理由: Cognilens/Lexoraへの負荷とコスト制御

## 移行時の注記（2026-09-10）

Drive 原本の逐語移行。**Drive 上に 2 部あった**（`Spirrow Magickit` の `1LgD4u3L...` と
`Magickit Phanthand Integration` の `1gA_ofj8...`、題名・サイズ 4238 とも同一）。
規約 §2.0 に従い**両方の fileId を併記**。移行先の決め方は
[[spirrow-magickit:phanthand-integration-architecture]] の移行時の注記を参照。

逐語からの逸脱は無い（ホスト名・IP・サーバーパス・認証の姿勢のいずれも本書には現れない）。

### 実装と一致している箇所

`src/magickit/mcp/tools/smart_read.py` と照合した:

| 本書 | 現物 |
|---|---|
| 配置 `src/magickit/mcp/tools/smart_read.py` に両ツール、`register_tools(mcp, settings)` で登録 | 一致 |
| 4 モード（`raw` / `summarize` / `essence` / `compress`） | `VALID_MODES` に 4 つ。既定は `summarize` |
| smart_read のレスポンス（`success` / `mode` / `results` / `file_count` / `errors`） | docstring の Returns と一致 |
| ファイル単位でエラーを収集し、1 ファイルの失敗で全体を止めない | 一致（`errors` リストに積む） |
| smart_analyze の `max_files=20` / `search_root` / `save_to_knowledge` | 一致 |

### 実装が本書から動いた 1 箇所

**`phanthand_url` / `phanthand_api_key` が必須引数になった。** 本書は
「（必須）」とコメントしつつ既定値 `""` を与え、実行時バリデーションで弾く設計だが、
現物は**既定値を持たず**、引数の並びも 2 番目・3 番目に繰り上がっている:

```python
async def smart_read(
    files: list[str],
    phanthand_url: str,
    phanthand_api_key: str,
    mode: str = "summarize",
    ...
```

実行時バリデーション（`"phanthand_url is required"` 等）は**残っている**ので、
本書の §エラーハンドリング はそのまま有効。変わったのは「呼び出し側が省略できるか」だけ。

### 本書にしかないもの

**§処理モード の表の「focus使用」列と、focus が Cognilens のどの引数に落ちるか** ——
`essence` は `focus_areas=[focus]`、`compress` は `preserve=focus`。
コードを読めば分かるが、**なぜ 4 モードなのか / どのモードをいつ使うか**（ユースケース列）は
本書にしかない。

**§ファイル数制限 の理由**（Cognilens/Lexora への負荷とコスト制御）も同様。
現物には `max_files: int = 20` という数字だけがある。
