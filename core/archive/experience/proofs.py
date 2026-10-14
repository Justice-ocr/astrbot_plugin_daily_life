from __future__ import annotations

import sqlite3
from typing import Any

from ...models import MemoryEvidenceRecord

from typing import Protocol


class EvidenceArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _compact_life_decision_evidence: Any
    _compose_memory_evidence: Any
    _conn: Any
    _is_life_decision_evidence: Any
    _run_db: Any
    _text: Any


class EvidenceArchiveComponent:
    def __init__(self, context: EvidenceArchiveComponentContext) -> None:
        self.context = context

    @staticmethod
    def _is_life_decision_evidence(item: MemoryEvidenceRecord) -> bool:
        return (
            str(item.target_type or "").strip() == "life_decision"
            and str(item.evidence_type or "").strip() == "decision"
        )

    def _compose_memory_evidence(self, row: sqlite3.Row) -> MemoryEvidenceRecord:
        return MemoryEvidenceRecord(
            id=int(row["id"] or 0),
            target_type=row["target_type"],
            target_id=row["target_id"],
            evidence_type=row["evidence_type"],
            source_table=row["source_table"],
            source_id=row["source_id"],
            session_id=row["session_id"],
            message_id=row["message_id"],
            date=row["date"],
            summary=self.context._text(row["summary"]),
            confidence=float(row["confidence"] or 0.0),
            status=row["status"],
            created_at=row["created_at"],
        )

    def _compact_life_decision_evidence(self, keep_id: int) -> int:
        if not keep_id:
            return 0
        cursor = self.context._conn.execute(
            """
            DELETE FROM memory_evidence
            WHERE target_type = 'life_decision'
              AND evidence_type = 'decision'
              AND id <> ?
            """,
            (keep_id,),
        )
        return int(cursor.rowcount or 0)

    async def save_memory_evidence(
        self, evidence: MemoryEvidenceRecord
    ) -> MemoryEvidenceRecord | None:
        item = MemoryEvidenceRecord.from_value(
            evidence.as_dict()
            if isinstance(evidence, MemoryEvidenceRecord)
            else evidence
        )
        if not item:
            return None

        def dbwork():
            cursor = self.context._conn.execute(
                """
                INSERT INTO memory_evidence(
                    target_type, target_id, evidence_type, source_table, source_id,
                    session_id, message_id, date, summary, confidence, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    self.context._text(item.target_type),
                    self.context._text(item.target_id),
                    self.context._text(item.evidence_type) or "observation",
                    self.context._text(item.source_table),
                    self.context._text(item.source_id),
                    self.context._text(item.session_id),
                    self.context._text(item.message_id),
                    self.context._text(item.date),
                    self.context._text(item.summary),
                    max(float(item.confidence or 0.0), 0.0),
                    self.context._text(item.status) or "active",
                ),
            )
            evidence_id = int(cursor.lastrowid or 0)
            if self.context._is_life_decision_evidence(item):
                self.context._compact_life_decision_evidence(evidence_id)
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM memory_evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
            return self.context._compose_memory_evidence(row) if row else None

        return await self.context._run_db(dbwork)

    async def get_memory_evidence(
        self,
        target_type: str = "",
        target_id: str = "",
        limit: int = 30,
    ) -> list[MemoryEvidenceRecord]:
        def dbwork():
            sql = "SELECT * FROM memory_evidence"
            params: list[Any] = []
            clauses = []
            if target_type:
                clauses.append("target_type = ?")
                params.append(self.context._text(target_type))
            if target_id:
                clauses.append("target_id = ?")
                params.append(self.context._text(target_id))
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_memory_evidence(row) for row in rows]

        return await self.context._run_db(dbwork)
