# Spirrow-Magickit

オーケストレーションレイヤー for Spirrow Platform

## 概要

複数のMCPサーバを統合し、ローカルLLMによる知的なルーティングと最適化を行う司令塔。
タスク管理・依存関係解決・コンテキスト最適化を担当。

## アーキテクチャ

```
Claude Code / Client
        │
        ▼
    Magickit (:8004)
        │
   ┌────┼────┬────┐
   ▼    ▼    ▼    ▼
Lexora Cognilens Prismind UnrealWise
```

## 技術スタック

- Python 3.11+
- FastAPI
- SQLite (状態管理)
- httpx (非同期HTTPクライアント)
- Pydantic v2

## セットアップ

```bash
# 仮想環境作成
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 依存関係インストール
pip install -r requirements.txt

# 開発サーバー起動
uvicorn magickit.main:app --reload --port 8004
```

## 設定

環境変数でサービスURLを設定:

```bash
MAGICKIT_LEXORA_URL=http://localhost:8001
MAGICKIT_COGNILENS_URL=http://localhost:8003
MAGICKIT_PRISMIND_URL=http://localhost:8002
MAGICKIT_PORT=8004
```

## ライセンス

Private
