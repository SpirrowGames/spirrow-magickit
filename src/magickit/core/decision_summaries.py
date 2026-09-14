"""判断材料の**簡潔版**。Lexora の light tier が材料そのものから作る。

## 何のためにあるか

composer が書く材料は詳しい。詳しいこと自体は意図的で、D-48 rev2 が
「圧縮圧をかけると読めないものが出る」を実測して長さ目標を外している。
一方、読み手 (Takahito) の要求はこうだった:

    向き合っている問題を可能な限り明瞭簡潔に / 選択肢とそのメリット・
    デメリット / メリット・デメリットは比較出来るように表にしてほしい

この 2 つは両立しない ——— **片方を選ばなければ、ではなく両方出せばよい**。
詳細が正本、要約は導出物。並べて置けば、要約が何を落としたかを読み手が
その場で検算できる。これが「要約だけ作って詳細を捨てる」との決定的な違い
で、composer 側に圧縮を強いなくて済む理由でもある。

## 入力は材料であってスレッドではない

**画面に出ている材料そのもの**を要約する。スレッドから独立に書き直させる
と「同じ問いに対する 2 つの答え」になり、食い違ったときにどちらが正しいか
読み手には決められない ——— 出どころが別だから。材料を要約するなら、両者は
同じ文章の 2 つの見え方で、突き合わせは目で完結する。

## 保存するもの

``(project, thread_id)`` に 1 行。``head_msg_id`` を一緒に持つので、材料が
更新されれば **キーが合わなくなって自然に無効化される** ——— 明示的な
invalidation を書かない。判断ページは合致したときだけキャッシュを使う。

このアプリに alembic は無い (あるのは conclair) ∴ :mod:`decision_materials`
/ :mod:`board_lanes` と同じ規約: ``core/`` に自分のモジュールを持ち、自分の
``_create_tables`` を公開メソッドの先頭で呼び、**同じ SQLite ファイル**を
使う (deploy と backup の対象を増やさない)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


class DecisionSummaryStore:
    """``(project, thread_id)`` ごとに簡潔版を 1 つ持つ。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def _create_tables(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS decision_summaries (
                project      TEXT NOT NULL,
                thread_id    TEXT NOT NULL,
                head_msg_id  TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                tier         TEXT NOT NULL,
                model        TEXT,
                generated_at TEXT NOT NULL,
                UNIQUE(project, thread_id)
            )
        """)
        await conn.commit()

    async def put(
        self,
        *,
        project: str,
        thread_id: str,
        head_msg_id: str,
        payload: dict[str, Any],
        tier: str,
        model: str | None,
    ) -> None:
        """UPSERT。``head_msg_id`` は**キャッシュの鍵であって飾りではない**。"""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        async with aiosqlite.connect(self.db_path) as conn:
            await self._create_tables(conn)
            await conn.execute(
                """
                INSERT OR REPLACE INTO decision_summaries (
                    project, thread_id, head_msg_id, payload_json,
                    tier, model, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project,
                    thread_id,
                    head_msg_id,
                    json.dumps(payload, ensure_ascii=False),
                    tier,
                    model,
                    now,
                ),
            )
            await conn.commit()
        logger.info(
            "decision summary stored",
            project=project,
            thread_id=thread_id,
            head_msg_id=head_msg_id,
        )

    async def get(
        self, *, project: str, thread_id: str, head_msg_id: str
    ) -> dict[str, Any] | None:
        """``head_msg_id`` が一致する行だけ返す。

        一致しない = 材料が更新された = この要約は**別の問いの要約**。返さ
        ないのが正しい。古い要約を新しい材料の隣に置くのは、両者が同じ文章の
        2 つの見え方だという前提そのものを壊す。
        """
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._create_tables(conn)
            cursor = await conn.execute(
                """
                SELECT payload_json, tier, model, generated_at, head_msg_id
                FROM decision_summaries
                WHERE project = ? AND thread_id = ?
                """,
                (project, thread_id),
            )
            row = await cursor.fetchone()

        if row is None or row["head_msg_id"] != head_msg_id:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            # 書いたのは自分なので通常起きない。起きたら「無い」と同じに
            # 扱う ——— 壊れた行で判断ページを落とさない。
            logger.warning(
                "decision summary payload is not JSON",
                project=project,
                thread_id=thread_id,
            )
            return None
        if not isinstance(payload, dict):
            return None
        payload["tier"] = row["tier"]
        payload["model"] = row["model"]
        payload["generated_at"] = row["generated_at"]
        return payload


__all__ = ["DecisionSummaryStore"]
