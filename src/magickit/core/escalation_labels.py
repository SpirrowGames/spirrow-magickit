"""判断ページの「聞くべきだったか」ラベルの SQLite storage.

thread: spirrow-magickit ``T-decision-page-escalation-feedback-label``
(正本 msg-1407 §1、改訂 msg-1445 / msg-1448 / msg-1451)。

**このモジュールに置くべき理由 (触る前に読む 2 行)**

- **SOT はここ。Conclair には何も書かない** (msg-1393 §1 / msg-1407 §1)。
  ラベルを Conclair の msg にすると ``last_msg_id`` が動き、駐機判定・
  freshness・board がすべて変わる。評価データを書いた副作用でループが動く
  形は採らない。対象 msg は ``target_msg_id`` で参照するだけ。
- **追記専用。行を更新も削除もしない。** 現在のラベルは
  ``(project, thread_id, target_msg_id)`` ごとの ``id`` が最大の行。
  取り消しは ``label='cleared'`` の行を足すことで表す。付け直した事実その
  ものがラベルの信頼度の情報になる (msg-1393 §1)。

``decision_materials`` と同じ SQLite file を共有する (別 DB を作らない —
deploy / backup の対象を増やさない)。並行制御コードは持たない: 書き込みは
単文 INSERT で、行同士が衝突しない。

スナップショットの引き継ぎ元は列で持たない (msg-1405): 同じ
``(project, thread_id, target_msg_id)`` のうち、``id`` が最小の
``material_fresh`` 行がそれである。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: 列の並び。SELECT と INSERT の両方がこれを読む (2 か所で別々に書かない)。
_COLUMNS = (
    "id",
    "project",
    "thread_id",
    "target_msg_id",
    "escalated_by",
    "label",
    "reason_tags_json",
    "note",
    "rules_hash",
    "jev_p",
    "jev_rule",
    "jev_model_version",
    "snapshot_source",
    "tags_mismatch",
    "target_moved",
    "labeled_at",
)
_INSERT_COLUMNS = _COLUMNS[1:]  # id は AUTOINCREMENT
_SELECT = ", ".join(_COLUMNS)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """DB 行 → API の形。``reason_tags_json`` は list に戻し、フラグは bool。"""
    out = {name: row[name] for name in _COLUMNS}
    raw = out.pop("reason_tags_json")
    out["reason_tags"] = json.loads(raw) if raw else []
    out["tags_mismatch"] = bool(out["tags_mismatch"])
    out["target_moved"] = bool(out["target_moved"])
    return out


class EscalationLabelStore:
    """``escalation_labels`` の追記専用 store。

    ``DecisionMaterialStore`` と同じく呼び出しごとに connection を開閉する。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def _create_tables(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS escalation_labels (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                project            TEXT NOT NULL,
                thread_id          TEXT NOT NULL,
                target_msg_id      TEXT NOT NULL,
                escalated_by       TEXT,
                label              TEXT NOT NULL,
                reason_tags_json   TEXT,
                note               TEXT,
                rules_hash         TEXT,
                jev_p              REAL,
                jev_rule           TEXT,
                jev_model_version  TEXT,
                snapshot_source    TEXT NOT NULL,
                tags_mismatch      INTEGER NOT NULL DEFAULT 0,
                target_moved       INTEGER NOT NULL DEFAULT 0,
                labeled_at         TEXT NOT NULL
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_escalation_labels_target "
            "ON escalation_labels (project, thread_id, target_msg_id, id)"
        )
        await conn.commit()

    async def append(self, row: dict[str, Any]) -> dict[str, Any]:
        """1 行を追記し、保存した行 (``id`` / ``labeled_at`` 付き) を返す。

        ``row`` は ``reason_tags`` (list) を持つ API の形で受ける。
        ``labeled_at`` はサーバ時刻で決める (呼び出し側の値は使わない)。
        """
        values = dict(row)
        tags = values.pop("reason_tags", None) or []
        values["reason_tags_json"] = json.dumps(list(tags), ensure_ascii=False)
        values["tags_mismatch"] = int(bool(values.get("tags_mismatch")))
        values["target_moved"] = int(bool(values.get("target_moved")))
        values["labeled_at"] = _now_iso()
        params = tuple(values.get(name) for name in _INSERT_COLUMNS)
        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._create_tables(conn)
            cursor = await conn.execute(
                f"INSERT INTO escalation_labels ({', '.join(_INSERT_COLUMNS)}) "
                f"VALUES ({placeholders})",
                params,
            )
            new_id = cursor.lastrowid
            await conn.commit()
            cursor = await conn.execute(
                f"SELECT {_SELECT} FROM escalation_labels WHERE id = ?", (new_id,)
            )
            stored = await cursor.fetchone()
        logger.info(
            "escalation label stored",
            project=values.get("project"),
            thread_id=values.get("thread_id"),
            target_msg_id=values.get("target_msg_id"),
            label=values.get("label"),
        )
        return _row_to_dict(stored)

    async def history(
        self, *, project: str, thread_id: str, target_msg_id: str
    ) -> list[dict[str, Any]]:
        """その target の全行を ``id`` 昇順で返す。最後の要素が current。"""
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._create_tables(conn)
            cursor = await conn.execute(
                f"SELECT {_SELECT} FROM escalation_labels "
                "WHERE project = ? AND thread_id = ? AND target_msg_id = ? "
                "ORDER BY id ASC",
                (project, thread_id, target_msg_id),
            )
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def latest_target(self, *, project: str, thread_id: str) -> str | None:
        """そのスレッドで最後にラベル行が書かれた target。無ければ ``None``。"""
        async with aiosqlite.connect(self.db_path) as conn:
            await self._create_tables(conn)
            cursor = await conn.execute(
                "SELECT target_msg_id FROM escalation_labels "
                "WHERE project = ? AND thread_id = ? ORDER BY id DESC LIMIT 1",
                (project, thread_id),
            )
            row = await cursor.fetchone()
        return row[0] if row else None

    async def list_labels(
        self,
        *,
        project: str | None = None,
        since: str | None = None,
        history: bool = False,
    ) -> list[dict[str, Any]]:
        """export 用。``history=False`` は target ごとの current 行だけ。

        ``since`` は ``labeled_at >= since`` の文字列比較 (ISO8601 UTC 同士
        なので辞書順 = 時刻順)。current モードでは current 行の時刻で絞る
        (古い行で絞ってから current を選ぶと、付け直し済みの古い値が出る)。
        current の ``cleared`` 行も返す: 読む側が「取り消された」と
        「付いたことが無い」を区別できるように。
        """
        where: list[str] = []
        params: list[Any] = []
        if project:
            where.append("project = ?")
            params.append(project)
        if not history:
            where.append(
                "id IN (SELECT MAX(id) FROM escalation_labels "
                "GROUP BY project, thread_id, target_msg_id)"
            )
        if since:
            where.append("labeled_at >= ?")
            params.append(since)
        sql = f"SELECT {_SELECT} FROM escalation_labels"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id ASC"
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._create_tables(conn)
            cursor = await conn.execute(sql, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]


__all__ = ["EscalationLabelStore"]
