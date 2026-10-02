"""判断ページの材料 (question / options / gain / loss / …) の SQLite storage.

spec: ``spec/slices/S5-decision-materials.md``.

**このモジュールに置くべき理由 (触る前に読む 2 行)**

- **`state_manager.py` に足さない**。判断材料は独立の subsystem で、既存の
  task / workspace / project 系の schema には属さない ∴ 別 module に分離
  して、`state_manager` の table 群を汚さない。同じ SQLite file を共有
  する (別 DB を作らない — deploy / backup の対象を増やさない)。
- **書き込みは `INSERT OR REPLACE` の単文**で、並行制御コードを持たない
  (Heisenberg F-A: `state_manager.py` L109/L551 と同方式)。**新規の
  concurrency 実装を書かない** — その必要性が実測で示されるまで先回りしない。

**契約** (spec §1):

- `put_material` は mindwire → magickit の push で呼ばれる。同一
  ``(project, thread_id)`` への並行 PUT は SQLite の
  ``UNIQUE(project, thread_id)`` に対して最終値で決着する。
- ``composer_status`` の検査は **HTTP 層で先に行い**、`"ok"` でない場合は
  400 で弾く (spec §1.3)。ここに到達する時点で `composer_status == "ok"`
  である ∴ ここでは保存しない (格納しない field を作らない、YAGNI)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from magickit.utils.logging import get_logger

logger = get_logger(__name__)


#: Columns added after the table first shipped, in the order they were
#: added. Names are literals here (never caller input), which is what makes
#: the f-string ``ALTER TABLE`` below safe.
_LATE_COLUMNS = (
    ("stop_reason", "TEXT"),
    ("parked_lane", "TEXT"),
    ("operator_task", "TEXT"),
    ("protocol_violation", "INTEGER"),
)


def _bool_or_none(value: Any) -> bool | None:
    """SQLite INTEGER 0/1/NULL → ``False`` / ``True`` / ``None``."""
    return None if value is None else bool(value)


class DecisionMaterialStore:
    """判断材料の SQLite storage (UPSERT).

    使用形は 1 request = 1 インスタンス (connection は open/close をここで
    行う。既存の ``StateManager`` のように長命 connection を保つ pattern
    と混ぜない — 判断ページの request 頻度は低く、connection pool を持たない
    aiosqlite の default の方が制御可能性が高い)。
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the store.

        Args:
            db_path: Path to the SQLite database file. Shared with StateManager.
        """
        self.db_path = db_path
        # Ensure directory exists (mirroring StateManager.initialize)
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

    async def _create_tables(self, conn: aiosqlite.Connection) -> None:
        """Create the decision_materials table if missing.

        Idempotent -- ``CREATE TABLE IF NOT EXISTS`` + a ``UNIQUE`` constraint
        that is either satisfied at creation or already present.

        **The one column added after the fact is ``stop_reason``**, and it
        is added here rather than by a migration tool because this app has
        no alembic (that is conclair). An earlier revision of this docstring
        said a future schema change "lives with alembic like the rest of the
        app"; there is no alembic in this app, so that sentence promised a
        ladder that does not exist. The honest shape for a nullable column
        on a table whose writer is a single UPSERT is the guarded
        ``ADD COLUMN`` below: it runs once, costs nothing after that, and
        every row written before it reads back as ``None`` -- which is
        exactly what "we did not record a reason for this one" means.

        The same guarded ``ADD COLUMN`` later carried the three parked-lane
        columns (``parked_lane`` / ``operator_task`` / ``protocol_violation``,
        spec §1.1 / §2.1). They follow the ``stop_reason`` precedent exactly
        and for the same reason: rows written before them read back as
        ``None``, and the board treats ``None`` as "判断" -- which is what
        every card showed before the classification was sent at all.
        """
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS decision_materials (
                project        TEXT NOT NULL,
                thread_id      TEXT NOT NULL,
                head_msg_id    TEXT NOT NULL,
                signature      TEXT,
                stop_reason    TEXT,
                parked_lane    TEXT,
                operator_task  TEXT,
                protocol_violation INTEGER,
                question       TEXT,
                options_json   TEXT,
                recommendation TEXT,
                recommendation_reason TEXT,
                unknowns_json  TEXT,
                stored_at      TEXT NOT NULL,
                UNIQUE(project, thread_id)
            )
        """)
        # Pre-existing databases (57 rows on production when this shipped)
        # were created without stop_reason. PRAGMA rather than catching the
        # duplicate-column error: a swallowed OperationalError here would
        # also swallow a genuinely broken table.
        cursor = await conn.execute("PRAGMA table_info(decision_materials)")
        columns = {row[1] for row in await cursor.fetchall()}
        for name, sql_type in _LATE_COLUMNS:
            if name not in columns:
                await conn.execute(
                    f"ALTER TABLE decision_materials ADD COLUMN {name} {sql_type}"
                )
        # No index on (project, thread_id) beyond UNIQUE -- SQLite creates an
        # implicit index for UNIQUE, and that is the only lookup pattern.
        await conn.commit()

    async def put_material(
        self,
        *,
        project: str,
        thread_id: str,
        head_msg_id: str,
        signature: str | None,
        question: str | None,
        stop_reason: str | None = None,
        parked_lane: str | None = None,
        operator_task: str | None = None,
        protocol_violation: bool | None = None,
        options: list[dict[str, Any]] | None,
        recommendation: str | None,
        recommendation_reason: str | None,
        unknowns: list[str] | None,
    ) -> dict[str, Any]:
        """UPSERT one material row for ``(project, thread_id)``.

        spec §1.1 / §2.2. Returns ``{"stored": True, "replaced": bool}``,
        where ``replaced`` reflects whether a prior row existed for the
        same ``(project, thread_id)`` key.

        **Idempotency is structural**: ``INSERT OR REPLACE`` on a
        ``UNIQUE`` constraint. No lock, no read-check-write. This is the
        same shape ``state_manager.save_task`` uses (L109) -- one place to
        remember the concurrency story, not two.
        """
        stored_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        options_json = json.dumps(options) if options is not None else None
        unknowns_json = json.dumps(unknowns) if unknowns is not None else None

        async with aiosqlite.connect(self.db_path) as conn:
            await self._create_tables(conn)
            # Was there a prior row? Used to report `replaced` truthfully.
            cursor = await conn.execute(
                "SELECT 1 FROM decision_materials WHERE project = ? AND thread_id = ?",
                (project, thread_id),
            )
            prior = await cursor.fetchone()
            replaced = prior is not None

            await conn.execute(
                """
                INSERT OR REPLACE INTO decision_materials (
                    project, thread_id, head_msg_id, signature, stop_reason,
                    parked_lane, operator_task, protocol_violation,
                    question, options_json, recommendation,
                    recommendation_reason, unknowns_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project,
                    thread_id,
                    head_msg_id,
                    signature,
                    stop_reason,
                    parked_lane,
                    operator_task,
                    None if protocol_violation is None else int(protocol_violation),
                    question,
                    options_json,
                    recommendation,
                    recommendation_reason,
                    unknowns_json,
                    stored_at,
                ),
            )
            await conn.commit()

        logger.info(
            "decision material stored",
            project=project,
            thread_id=thread_id,
            head_msg_id=head_msg_id,
            replaced=replaced,
        )
        return {"stored": True, "replaced": replaced}

    async def get_material(
        self, *, project: str, thread_id: str
    ) -> dict[str, Any] | None:
        """Read one material row, or ``None`` when absent.

        The return shape mirrors what ``put_material`` accepted, plus
        ``stored_at``. Absence is signalled with ``None`` -- the HTTP layer
        turns that into a 404 (spec §1.2), and the judgement page renderer
        turns it into **J-absent** (spec §3).
        """
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._create_tables(conn)
            cursor = await conn.execute(
                """
                SELECT project, thread_id, head_msg_id, signature, stop_reason,
                       parked_lane, operator_task, protocol_violation,
                       question, options_json, recommendation,
                       recommendation_reason, unknowns_json, stored_at
                FROM decision_materials
                WHERE project = ? AND thread_id = ?
                """,
                (project, thread_id),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return {
            "project": row["project"],
            "thread_id": row["thread_id"],
            "head_msg_id": row["head_msg_id"],
            "signature": row["signature"],
            "stop_reason": row["stop_reason"],
            "parked_lane": row["parked_lane"],
            "operator_task": row["operator_task"],
            "protocol_violation": _bool_or_none(row["protocol_violation"]),
            "question": row["question"],
            "options": (
                json.loads(row["options_json"]) if row["options_json"] else None
            ),
            "recommendation": row["recommendation"],
            "recommendation_reason": row["recommendation_reason"],
            "unknowns": (
                json.loads(row["unknowns_json"]) if row["unknowns_json"] else None
            ),
            "stored_at": row["stored_at"],
        }

    async def list_materials(self) -> list[dict[str, Any]]:
        """Every material row, newest first. Cross-project.

        ``get_material`` answers "what is the material for this one
        thread", which is what the judgement page asks. The board asks the
        opposite question -- "which threads have material at all" -- and
        answering it by walking projects and calling ``get_material`` per
        thread would be a round trip per thread to learn that most of them
        have nothing.

        **Rows here are not "waiting for a human".** The store never
        deletes: a row survives the decision it was written for. Freshness
        is ``head_msg_id`` vs the thread's ``last_msg_id`` and only the
        caller, holding live thread state, can decide it (spec
        ``S5-decision-materials.md`` §3.1). ``options`` / ``unknowns`` are
        deliberately not decoded here -- the board shows the question, and
        parsing JSON for every row to throw it away is work for nothing.
        """
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await self._create_tables(conn)
            cursor = await conn.execute(
                """
                SELECT project, thread_id, head_msg_id, signature, stop_reason,
                       parked_lane, operator_task, protocol_violation, question,
                       recommendation, stored_at
                FROM decision_materials
                ORDER BY stored_at DESC
                """
            )
            rows = await cursor.fetchall()
        materials = [dict(row) for row in rows]
        for material in materials:
            material["protocol_violation"] = _bool_or_none(
                material["protocol_violation"]
            )
        return materials


__all__ = ["DecisionMaterialStore"]
