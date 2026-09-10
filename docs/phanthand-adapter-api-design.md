---
id: spirrow-magickit:phanthand-adapter-api-design
title: PhanthandAdapter API Design
product: spirrow-magickit
type: spec
status: active
version: 1.0
created: 2026-02-08
last_verified: 2026-09-10
supersedes: []
related: [spirrow-magickit:phanthand-integration-architecture, spirrow-magickit:smart-read-analyze-tool-design, spirrow-phanthand:design-v0.2]
keywords: [PhanthandAdapter, httpx, Bearer token, 例外階層, ApiResponse, エラーハンドリング]
legacy_drive_id: [1cMwb2MqPIdtckX1khhBIx4z7toYa3AyH5vQXXJrKsaE, 1CwaL61lMgPL9_mikF7yInvbEpukmk1hs06pBnuQpMww]
---

# PhanthandAdapter API Design

## 配置

`src/magickit/adapters/phanthand.py`

## クラス設計

```python
class PhanthandAdapter:
    """開発PCファイルアクセスAPIクライアント。

    既存Adapterと異なり、BaseAdapterを継承しない独立クラス。
    接続先は開発者ごとに異なるため、メソッド呼び出し時にURL/API keyを受け取る。
    """

    def __init__(self, timeout: float = 30.0):
        """初期化。httpxクライアントの設定のみ。"""
        self._timeout = timeout

    async def health_check(self, url: str, api_key: str) -> dict:
        """Phanthandの稼働確認。"""

    async def read_file(self, url: str, api_key: str, path: str, encoding: str = "utf-8") -> dict:
        """ファイル読み込み。POST /files/read"""

    async def list_directory(self, url: str, api_key: str, path: str, pattern: str = "*", recursive: bool = False) -> dict:
        """ディレクトリ一覧。POST /files/list"""

    async def file_exists(self, url: str, api_key: str, path: str) -> dict:
        """存在確認。POST /files/exists"""

    async def file_info(self, url: str, api_key: str, path: str) -> dict:
        """ファイルメタデータ。POST /files/info"""

    async def tree(self, url: str, api_key: str, path: str, max_depth: int = 3, exclude_patterns: list[str] | None = None) -> dict:
        """ディレクトリツリー。POST /files/tree"""

    async def search(self, url: str, api_key: str, path: str, pattern: str, max_results: int = 100) -> dict:
        """ファイル検索。POST /files/search"""
```

## HTTPクライアント管理

```python
async def _request(self, url: str, api_key: str, endpoint: str, payload: dict) -> dict:
    """共通HTTPリクエスト処理。

    - httpx.AsyncClientを都度生成（接続先が毎回変わる可能性があるため）
    - Bearer token認証ヘッダ
    - タイムアウト処理
    - レスポンスのApiResponse形式パース
    - エラーハンドリング（接続失敗、認証エラー、パス不許可等）
    """
```

## レスポンス形式

Phanthandのレスポンスは統一形式:
```json
{
    "success": true,
    "data": { ... },
    "error": null
}
```

Adapterでは `data` 部分を返却。`success: false` の場合は例外を送出。

## エラーハンドリング

| エラー | 原因 | 処理 |
|--------|------|------|
| `httpx.ConnectError` | Phanthand未起動/到達不可 | PhanthandConnectionError |
| HTTP 401 | API key不正 | PhanthandAuthError |
| HTTP 403 | パス不許可 | PhanthandPathNotAllowedError |
| HTTP 404 | ファイル/ディレクトリ不存在 | PhanthandFileNotFoundError |
| HTTP 413/422 | ファイルサイズ超過等 | PhanthandRequestError |
| タイムアウト | 応答なし | PhanthandTimeoutError |

## 例外クラス階層

```python
class PhanthandError(Exception):
    """Base exception for Phanthand operations."""

class PhanthandConnectionError(PhanthandError):
    """Cannot connect to Phanthand server."""

class PhanthandAuthError(PhanthandError):
    """Authentication failed."""

class PhanthandPathNotAllowedError(PhanthandError):
    """Path is outside allowed directories."""

class PhanthandFileNotFoundError(PhanthandError):
    """File or directory not found."""

class PhanthandRequestError(PhanthandError):
    """General request error."""

class PhanthandTimeoutError(PhanthandError):
    """Request timed out."""
```

## 移行時の注記（2026-09-10）

Drive 原本の逐語移行。**Drive 上に 2 部あった**（`Spirrow Magickit` の `1cMwb2Mq...` と
`Magickit Phanthand Integration` の `1CwaL61l...`、題名・サイズ 3247 とも同一）。
規約 §2.0 に従い**両方の fileId を併記**。移行先の決め方は
[[spirrow-magickit:phanthand-integration-architecture]] の移行時の注記を参照。

逐語からの逸脱は無い（ホスト名・IP・サーバーパス・認証の姿勢のいずれも本書には現れない）。

### 実装と一致している箇所

`src/magickit/adapters/phanthand.py` と照合した:

| 本書 | 現物 |
|---|---|
| 7 メソッド（`health_check` + ファイル API 6 本） | **7 本とも実在**（`phanthand.py:193` 以降） |
| 例外 7 クラス（`PhanthandError` + 6 サブクラス） | **7 クラスとも実在**（`phanthand.py:22-46`）。名前も継承関係も一致 |
| `_request(url, api_key, endpoint, payload)` | 同一シグネチャ（`phanthand.py:84`） |
| ApiResponse 形式のパース / `success: false` で例外 | 一致 |
| エラー対応表（401/403/404/413/422） | 一致 |

### 実装が本書から動いた 2 箇所

1. **`__init__` のタイムアウトが 2 値に割れた。**

   ```python
   def __init__(self, timeout: float = 60.0, read_timeout: float = 240.0) -> None:
   ```

   現物は `httpx.Timeout(connect=timeout, read=read_timeout, write=timeout, pool=timeout)` を
   組む。**read だけ 4 倍長い理由はコードの docstring にある** ——
   "file transfers over WAN (e.g. Tailscale) can be slow for large files"。
   本書の単一 `timeout=30.0` では、tailnet 越しの大きいファイルで切れる。

2. **`health_check` から `api_key` が消えた。** 現物は `health_check(self, url)`。
   Phanthand 側の `GET /health` が認証不要であるため（[[spirrow-phanthand:design-v0.2]] §API設計）。
   本書のシグネチャで書くと、使わない引数を要求することになる。
