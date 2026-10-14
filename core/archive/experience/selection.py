from __future__ import annotations

import sqlite3
from typing import Any

from ...models import LifeDecisionRecord
from ...models.coerce import compact_explanation_text

from typing import Protocol


class LifeDecisionArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _compose_life_decision: Any
    _conn: Any
    _link_decision_memory_candidates_unlocked: Any
    _run_db: Any
    _text: Any


class LifeDecisionArchiveComponent:
    def __init__(self, context: LifeDecisionArchiveComponentContext) -> None:
        self.context = context

    def _compose_life_decision(self, row: sqlite3.Row) -> LifeDecisionRecord:
        return LifeDecisionRecord(
            id=int(row["id"] or 0),
            date=row["date"],
            kind=row["kind"],
            subject=self.context._text(row["subject"]),
            decision=self.context._text(row["decision"]),
            reason=compact_explanation_text(row["reason"], 360),
            evidence=self.context._text(row["evidence"]),
            outcome=self.context._text(row["outcome"]),
            confidence=float(row["confidence"] or 0.0),
            source=row["source"],
            created_at=row["created_at"],
        )

    async def save_life_decision(
        self, decision: LifeDecisionRecord
    ) -> LifeDecisionRecord | None:
        item = LifeDecisionRecord.from_value(
            decision.as_dict() if isinstance(decision, LifeDecisionRecord) else decision
        )
        if not item:
            return None

        def dbwork():
            cursor = self.context._conn.execute(
                """
                INSERT INTO life_decisions(
                    date, kind, subject, decision, reason, evidence,
                    outcome, confidence, source, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    self.context._text(item.date),
                    self.context._text(item.kind) or "daily_plan",
                    self.context._text(item.subject),
                    self.context._text(item.decision),
                    self.context._text(item.reason),
                    self.context._text(item.evidence),
                    self.context._text(item.outcome),
                    max(0.0, min(float(item.confidence or 0.0), 1.0)),
                    self.context._text(item.source) or "autonomous_life",
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM life_decisions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            saved = self.context._compose_life_decision(row) if row else None
            linker = getattr(self.context, "_link_decision_memory_candidates_unlocked", None)
            if saved and callable(linker):
                linker(saved)
                self.context._conn.commit()
            return saved

        return await self.context._run_db(dbwork)

    async def get_life_decisions(
        self,
        limit: int = 20,
        *,
        kind: str = "",
        date: str = "",
    ) -> list[LifeDecisionRecord]:
        def dbwork():
            sql = "SELECT * FROM life_decisions"
            params: list[Any] = []
            clauses = []
            if kind:
                clauses.append("kind = ?")
                params.append(self.context._text(kind))
            if date:
                clauses.append("date = ?")
                params.append(self.context._text(date))
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_life_decision(row) for row in rows]

        return await self.context._run_db(dbwork)

    async def update_life_decision_outcome(self, decision_id: int, outcome: str) -> bool:
        text = self.context._text(outcome)
        if int(decision_id or 0) <= 0 or not text:
            return False

        def dbwork():
            cursor = self.context._conn.execute(
                "UPDATE life_decisions SET outcome = ? WHERE id = ?",
                (text, int(decision_id)),
            )
            self.context._conn.commit()
            return cursor.rowcount > 0

        return await self.context._run_db(dbwork)
