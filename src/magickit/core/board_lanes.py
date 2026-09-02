"""Board の「列」と「一度見た項目」の SQLite storage.

判断待ち / deploy 承認待ち / 停止したループ ——— board に並ぶ項目は
**すべて他所の状態から導出される**。このモジュールが持つのは、その導出
からは出てこない 2 つだけ:

- ``board_lanes`` — 「僕が今それに手を付けているか」。新着 / 対応中 /
  保留 の 3 値で、**どこにも既存の表現が無い**。Conclair の thread status
  は「AI 側が誰の番だと思っているか」で、僕が着手したかどうかとは別の
  問い ∴ ここで持つ。
- ``board_seen`` — 「この項目を board に載せたことがある」という事実。
  完了列を描くために要る: 項目が live 集合から消えた瞬間に、その項目に
  ついて言えることも同時に消える ∴ 消える前に控えておく以外に方法がない。

**完了は保存しない。** 完了列は「``board_seen`` にあるが live 集合には
無い」から毎回導出する。ドラッグで完了に落とせないのはこのためで、
`done` という lane 値は存在しない — 実状態が動いていないのに完了列に
座っているカード、という嘘が構造的に作れない。

置き場所は ``state_manager`` の table 群ではなく独立 module (同じ SQLite
file は共有する)。理由は ``decision_materials.py`` と同じ ——— 別 subsystem
の schema を task/workspace の table に混ぜない。並行制御も同様に持たない
(``INSERT OR REPLACE`` の単文で最終値決着)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from magickit.utils.logging import get_logger

logger = get_logger(__name__)

#: ドラッグで行き来できる列。``done`` はここに **無い** (module docstring)。
LANES = ("new", "doing", "parked")

#: lane 行が無い項目の既定。「まだ触っていない」を行の不在で表すので、
#: board に出ただけで書き込みが起きることはない。
DEFAULT_LANE = "new"

#: ``board_seen`` の保持期間 (日)。完了列の窓 (既定 7 日) より十分長く、
#: かつ無限に伸びない値。書き込みのたびに刈る。
_SEEN_RETENTION_DAYS = 60


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SeenItem:
    """``board_seen`` に控える 1 項目。live 集合から消えた後もこれだけは残る。"""

    item_key: str
    kind: str
    title: str
    project: str | None = None
    thread_id: str | None = None
    href: str | None = None


class BoardLaneStore:
    """Board の lane と既見記録。1 request = 1 インスタンス。

    ``DecisionMaterialStore`` と同じ使用形 (connection を open/close する)。
    board の描画頻度は低く、長命 connection を持つ理由がない。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    async def _create_tables(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS board_lanes (
                item_key    TEXT PRIMARY KEY,
                lane        TEXT NOT NULL,
                fingerprint TEXT,
                moved_at    TEXT NOT NULL,
                moved_by    TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS board_seen (
                item_key      TEXT PRIMARY KEY,
                kind          TEXT NOT NULL,
                project       TEXT,
                thread_id     TEXT,
                title         TEXT,
                href          TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL
            )
        """)
        # 完了列は last_seen_at の降順スキャン ∴ index はそこにだけ張る。
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_board_seen_last "
            "ON board_seen (last_seen_at DESC)"
        )
        await conn.commit()

    # --- lanes -----------------------------------------------------------

    async def read_lanes(self) -> dict[str, dict[str, Any]]:
        """``item_key`` → lane 行。行の無い項目は呼び出し側で ``new`` 扱い。

        全行返す: board は常に全項目を描くので、key ごとに引くと N 回の
        往復になる。行数は「僕が手を付けた項目」の総数で、数十のオーダー。
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await self._create_tables(conn)
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT item_key, lane, fingerprint, moved_at, moved_by "
                "FROM board_lanes"
            )
            rows = await cursor.fetchall()
        return {row["item_key"]: dict(row) for row in rows}

    async def set_lane(
        self,
        *,
        item_key: str,
        lane: str,
        fingerprint: str | None,
        actor: str | None,
    ) -> None:
        """項目を列に置く。``new`` は行を **消す** (既定に戻すのが正しい)。

        ``fingerprint`` は移動時点の項目の同一性 (判断待ちなら
        ``head_msg_id``)。次に描くときこれが変わっていれば、カードは僕が
        動かした後に中身が入れ替わっている ∴ board が「更新あり」を出す。
        照合のために保存するだけで、ここでは何も判定しない。

        ``lane`` の検査は呼び出し側 (HTTP 境界) の仕事。ここに来る時点で
        ``LANES`` の値である。
        """
        async with aiosqlite.connect(self.db_path) as conn:
            await self._create_tables(conn)
            if lane == DEFAULT_LANE:
                await conn.execute(
                    "DELETE FROM board_lanes WHERE item_key = ?", (item_key,)
                )
            else:
                await conn.execute(
                    "INSERT OR REPLACE INTO board_lanes "
                    "(item_key, lane, fingerprint, moved_at, moved_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (item_key, lane, fingerprint, _utcnow(), actor),
                )
            await conn.commit()

    # --- seen ------------------------------------------------------------

    async def touch_seen(self, items: Iterable[SeenItem]) -> None:
        """live な項目を「見た」と記録する。board を描くたびに呼ばれる。

        **GET で書く**のは承知の上。完了列は「消えた項目」を出す列で、
        消えてから記録することは原理的にできない ∴ 見えている間に控える
        以外にない。書き込みは冪等な UPSERT で、``first_seen_at`` は
        初回だけ入る (``COALESCE`` で既存値を守る)。

        ``title`` / ``href`` も毎回上書きする: 完了列は項目が消えた後に
        描かれるので、そこに出る文言は **最後に生きていたときの姿** で
        なければならない。
        """
        items = list(items)
        if not items:
            return
        now = _utcnow()
        async with aiosqlite.connect(self.db_path) as conn:
            await self._create_tables(conn)
            await conn.executemany(
                """
                INSERT INTO board_seen
                    (item_key, kind, project, thread_id, title, href,
                     first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    kind         = excluded.kind,
                    project      = excluded.project,
                    thread_id    = excluded.thread_id,
                    title        = excluded.title,
                    href         = excluded.href,
                    last_seen_at = excluded.last_seen_at
                """,
                [
                    (
                        it.item_key, it.kind, it.project, it.thread_id,
                        it.title, it.href, now, now,
                    )
                    for it in items
                ],
            )
            await self._prune(conn)
            await conn.commit()

    async def list_gone(
        self, *, live_keys: set[str], since: datetime
    ) -> list[dict[str, Any]]:
        """``since`` 以降に最後に見た項目のうち、いま live でないもの。

        これが完了列。「僕が片付けた」ではなく「board から外れた」であり、
        外れた理由は項目ごとに違う (スレッドが進んだ / 誰かが答えた /
        deploy が走った) ∴ 理由は呼び出し側が live 側の材料から付ける。
        ここが言えるのは「もう待っていない」と「最後に見たのはいつか」
        だけ。
        """
        cutoff = since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        async with aiosqlite.connect(self.db_path) as conn:
            await self._create_tables(conn)
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT item_key, kind, project, thread_id, title, href, "
                "first_seen_at, last_seen_at FROM board_seen "
                "WHERE last_seen_at >= ? ORDER BY last_seen_at DESC",
                (cutoff,),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows if r["item_key"] not in live_keys]

    async def _prune(self, conn: aiosqlite.Connection) -> None:
        """保持期間を過ぎた既見記録と、その lane 行を落とす。

        lane 行を道連れにするのは、live でも既見でもない key の lane を
        残しておく意味が無いため。live な項目は毎描画で ``touch_seen``
        されるので、この条件に落ちることはない。
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_SEEN_RETENTION_DAYS)
        ).isoformat().replace("+00:00", "Z")
        await conn.execute("DELETE FROM board_seen WHERE last_seen_at < ?", (cutoff,))
        await conn.execute(
            "DELETE FROM board_lanes WHERE item_key NOT IN "
            "(SELECT item_key FROM board_seen)"
        )


__all__ = ["BoardLaneStore", "SeenItem", "LANES", "DEFAULT_LANE"]
