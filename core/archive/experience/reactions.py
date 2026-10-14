from __future__ import annotations

import sqlite3
from typing import Any

from ...models import BehaviorFeedbackRecord, MemoryCorrectionRecord, ReplyEffectRecord

from typing import Protocol


class FeedbackArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _compose_behavior_feedback: Any
    _compose_memory_correction: Any
    _compose_reply_effect: Any
    _conn: Any
    _flag: Any
    _run_db: Any
    _text: Any


class FeedbackArchiveComponent:
    def __init__(self, context: FeedbackArchiveComponentContext) -> None:
        self.context = context

    def _compose_behavior_feedback(self, row: sqlite3.Row) -> BehaviorFeedbackRecord:
        return BehaviorFeedbackRecord(
            id=int(row["id"] or 0),
            date=row["date"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            scene=row["scene"],
            action=row["action"],
            feedback=self.context._text(row["feedback"]),
            result=row["result"],
            score=float(row["score"] or 0.0),
            reason=self.context._text(row["reason"]),
            source=row["source"],
            created_at=row["created_at"],
        )

    async def add_behavior_feedback(
        self, feedback: BehaviorFeedbackRecord
    ) -> BehaviorFeedbackRecord | None:
        item = BehaviorFeedbackRecord.from_value(
            feedback.as_dict()
            if isinstance(feedback, BehaviorFeedbackRecord)
            else feedback
        )
        if not item:
            return None
        values = (
            self.context._text(item.date),
            self.context._text(item.target_type) or "action",
            self.context._text(item.target_id),
            self.context._text(item.scene),
            self.context._text(item.action),
            self.context._text(item.feedback),
            self.context._text(item.result),
            self.context._text(item.reason),
            self.context._text(item.source) or "chat_memory",
        )

        def dbwork():
            existing = self.context._conn.execute(
                """
                SELECT * FROM behavior_feedback
                WHERE date = ?
                  AND target_type = ?
                  AND target_id = ?
                  AND scene = ?
                  AND action = ?
                  AND feedback = ?
                  AND result = ?
                  AND reason = ?
                  AND source = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                values,
            ).fetchone()
            if existing:
                return self.context._compose_behavior_feedback(existing)
            cursor = self.context._conn.execute(
                """
                INSERT INTO behavior_feedback(
                    date, target_type, target_id, scene, action, feedback,
                    result, score, reason, source, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    *values[:7],
                    max(-5.0, min(float(item.score or 0.0), 5.0)),
                    values[7],
                    values[8],
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM behavior_feedback WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self.context._compose_behavior_feedback(row) if row else None

        return await self.context._run_db(dbwork)

    async def get_behavior_feedback(
        self, limit: int = 20, *, target_id: str = ""
    ) -> list[BehaviorFeedbackRecord]:
        def dbwork():
            sql = "SELECT * FROM behavior_feedback"
            params: list[Any] = []
            if target_id:
                sql += " WHERE target_id = ?"
                params.append(self.context._text(target_id))
            sql += " ORDER BY date DESC, id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_behavior_feedback(row) for row in rows]

        return await self.context._run_db(dbwork)

    def _compose_reply_effect(self, row: sqlite3.Row) -> ReplyEffectRecord:
        return ReplyEffectRecord(
            id=int(row["id"] or 0),
            scope=row["scope"],
            target_message_id=row["target_message_id"],
            reply_text=self.context._text(row["reply_text"]),
            outcome=row["outcome"],
            warmth=int(row["warmth"] or 0),
            continuity=int(row["continuity"] or 0),
            friction=int(row["friction"] or 0),
            reason=self.context._text(row["reason"]),
            evidence=self.context._text(row["evidence"]),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def save_reply_effect(
        self, effect: ReplyEffectRecord
    ) -> ReplyEffectRecord | None:
        item = ReplyEffectRecord.from_value(
            effect.as_dict() if isinstance(effect, ReplyEffectRecord) else effect
        )
        if not item:
            return None

        def dbwork():
            cursor = self.context._conn.execute(
                """
                INSERT INTO reply_effects(
                    scope, target_message_id, reply_text, outcome, warmth, continuity,
                    friction, reason, evidence, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    self.context._text(item.scope),
                    self.context._text(item.target_message_id),
                    self.context._text(item.reply_text),
                    self.context._text(item.outcome) or "pending",
                    max(0, min(int(item.warmth or 0), 100)),
                    max(0, min(int(item.continuity or 0), 100)),
                    max(0, min(int(item.friction or 0), 100)),
                    self.context._text(item.reason),
                    self.context._text(item.evidence),
                    self.context._text(item.source) or "proactive_reply",
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM reply_effects WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self.context._compose_reply_effect(row) if row else None

        return await self.context._run_db(dbwork)

    async def update_reply_effect_outcome(
        self,
        effect_id: int,
        *,
        outcome: str,
        evidence: str = "",
        warmth: int | None = None,
        continuity: int | None = None,
        friction: int | None = None,
    ) -> bool:
        def dbwork():
            row = self.context._conn.execute(
                "SELECT * FROM reply_effects WHERE id = ?", (int(effect_id),)
            ).fetchone()
            if not row:
                return False
            cursor = self.context._conn.execute(
                """
                UPDATE reply_effects
                SET outcome = ?,
                    evidence = COALESCE(NULLIF(?, ''), evidence),
                    warmth = ?,
                    continuity = ?,
                    friction = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    self.context._text(outcome) or row["outcome"],
                    self.context._text(evidence),
                    max(0, min(int(row["warmth"] if warmth is None else warmth), 100)),
                    max(
                        0,
                        min(
                            int(
                                row["continuity"] if continuity is None else continuity
                            ),
                            100,
                        ),
                    ),
                    max(
                        0,
                        min(
                            int(row["friction"] if friction is None else friction), 100
                        ),
                    ),
                    int(effect_id),
                ),
            )
            self.context._conn.commit()
            return cursor.rowcount > 0

        return await self.context._run_db(dbwork)

    async def expire_stale_reply_effects(
        self,
        max_age_seconds: int,
        *,
        evidence: str = "回复后会话自然结束，没有足够证据判断好坏",
    ) -> int:
        max_age = max(int(max_age_seconds or 0), 1)

        def dbwork():
            cursor = self.context._conn.execute(
                """
                UPDATE reply_effects
                SET outcome = 'neutral',
                    evidence = COALESCE(
                        NULLIF(evidence, ''),
                        CASE
                            WHEN source = 'proactive_reply' THEN '闲时续话后会话自然结束，没有足够证据判断好坏'
                            WHEN source = 'private_revisit' THEN '私聊回访后会话自然结束，没有足够证据判断好坏'
                            ELSE ?
                        END
                    ),
                    warmth = 50,
                    continuity = 45,
                    friction = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE outcome = 'pending'
                  AND COALESCE(NULLIF(updated_at, ''), created_at) < datetime('now', ?)
                """,
                (self.context._text(evidence), f"-{max_age} seconds"),
            )
            self.context._conn.commit()
            return max(int(cursor.rowcount or 0), 0)

        return await self.context._run_db(dbwork)

    async def get_reply_effects(
        self,
        limit: int = 20,
        *,
        scope: str = "",
        outcome: str = "",
    ) -> list[ReplyEffectRecord]:
        def dbwork():
            sql = "SELECT * FROM reply_effects"
            params: list[Any] = []
            clauses = []
            if scope:
                clauses.append("scope = ?")
                params.append(self.context._text(scope))
            if outcome:
                clauses.append("outcome = ?")
                params.append(self.context._text(outcome))
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY updated_at DESC, id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_reply_effect(row) for row in rows]

        return await self.context._run_db(dbwork)

    def _compose_memory_correction(self, row: sqlite3.Row) -> MemoryCorrectionRecord:
        return MemoryCorrectionRecord(
            id=int(row["id"] or 0),
            target_type=row["target_type"],
            target_id=row["target_id"],
            correction=self.context._text(row["correction"]),
            evidence=self.context._text(row["evidence"]),
            confidence=float(row["confidence"] or 0.0),
            applied=bool(row["applied"]),
            source=row["source"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def save_memory_correction(
        self, correction: MemoryCorrectionRecord
    ) -> MemoryCorrectionRecord | None:
        item = MemoryCorrectionRecord.from_value(
            correction.as_dict()
            if isinstance(correction, MemoryCorrectionRecord)
            else correction
        )
        if not item:
            return None

        def dbwork():
            cursor = self.context._conn.execute(
                """
                INSERT INTO memory_corrections(
                    target_type, target_id, correction, evidence, confidence,
                    applied, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    self.context._text(item.target_type),
                    self.context._text(item.target_id),
                    self.context._text(item.correction),
                    self.context._text(item.evidence),
                    max(0.0, min(float(item.confidence or 0.0), 1.0)),
                    self.context._flag(item.applied),
                    self.context._text(item.source) or "chat_memory",
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM memory_corrections WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self.context._compose_memory_correction(row) if row else None

        return await self.context._run_db(dbwork)

    async def mark_memory_correction_applied(
        self, correction_id: int, applied: bool = True
    ) -> bool:
        def dbwork():
            cursor = self.context._conn.execute(
                "UPDATE memory_corrections SET applied = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (self.context._flag(applied), int(correction_id)),
            )
            self.context._conn.commit()
            return cursor.rowcount > 0

        return await self.context._run_db(dbwork)

    async def get_memory_corrections(
        self,
        limit: int = 20,
        *,
        target_type: str = "",
        target_id: str = "",
        unapplied_only: bool = False,
    ) -> list[MemoryCorrectionRecord]:
        def dbwork():
            sql = "SELECT * FROM memory_corrections"
            params: list[Any] = []
            clauses = []
            if target_type:
                clauses.append("target_type = ?")
                params.append(self.context._text(target_type))
            if target_id:
                clauses.append("target_id = ?")
                params.append(self.context._text(target_id))
            if unapplied_only:
                clauses.append("applied = 0")
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_memory_correction(row) for row in rows]

        return await self.context._run_db(dbwork)
