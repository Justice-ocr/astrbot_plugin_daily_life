from __future__ import annotations

import sqlite3
from typing import Any

from ..models import (
    ActionDecisionRecord,
    GroupEnvironmentRecord,
    MessageVisibilityRecord,
)
from ..models.coerce import compact_explanation_text

from typing import Protocol

_COALESCED_ACTION_DECISION_KEYS = frozenset(
    {
        ("proactive_proposal_wait", "私聊回访/proposal"),
        ("proactive_proposal_wait", "闲时回复/proposal"),
    }
)


def _coalescible_action_decision(item: ActionDecisionRecord) -> bool:
    """判断是否可以合并重复的主动等待裁定。"""

    if not (item.session_id or item.sender_profile_id or item.group_id):
        return False
    return (item.action, item.scene_type) in _COALESCED_ACTION_DECISION_KEYS


class PerceptionArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _compose_action_decision: Any
    _compose_group_environment: Any
    _compose_message_visibility: Any
    _conn: Any
    _flag: Any
    _recent_scoped_rows_unlocked: Any
    _run_db: Any
    _text: Any


class PerceptionArchiveComponent:
    def __init__(self, context: PerceptionArchiveComponentContext) -> None:
        self.context = context

    def _recent_scoped_rows_unlocked(self, table: str, limit: int) -> list[sqlite3.Row]:
        sql = f"""
            SELECT *
            FROM {table}
            WHERE id IN (
                SELECT MAX(id)
                FROM {table}
                GROUP BY COALESCE(
                    NULLIF(group_id, ''),
                    NULLIF(session_id, ''),
                    NULLIF(sender_profile_id, ''),
                    CAST(id AS TEXT)
                )
            )
            ORDER BY id DESC
        """
        params: tuple[Any, ...] = ()
        if limit > 0:
            sql += " LIMIT ?"
            params = (limit,)
        return self.context._conn.execute(sql, params).fetchall()

    def _compose_group_environment(self, row: sqlite3.Row) -> GroupEnvironmentRecord:
        return GroupEnvironmentRecord(
            id=int(row["id"] or 0),
            session_id=row["session_id"],
            group_id=row["group_id"],
            group_name=row["group_name"],
            date=row["date"],
            atmosphere=row["atmosphere"],
            topic=self.context._text(row["topic"]),
            topic_owner=row["topic_owner"],
            active_users=int(row["active_users"] or 0),
            is_multithread=bool(row["is_multithread"]),
            is_spam=bool(row["is_spam"]),
            is_repetition=bool(row["is_repetition"]),
            is_discussing_bot=bool(row["is_discussing_bot"]),
            suitable_to_join=row["suitable_to_join"],
            bot_watch_state=row["bot_watch_state"],
            participation_desire=int(row["participation_desire"] or 0),
            complexity_score=int(row["complexity_score"] or 0),
            understanding_confidence=int(row["understanding_confidence"] or 0),
            deep_analysis_needed=bool(row["deep_analysis_needed"]),
            summary=self.context._text(row["summary"]),
            created_at=row["created_at"],
        )

    async def save_group_environment(
        self, environment: GroupEnvironmentRecord
    ) -> GroupEnvironmentRecord:
        item = GroupEnvironmentRecord.from_value(
            environment.as_dict()
            if isinstance(environment, GroupEnvironmentRecord)
            else environment
        )
        if not item:
            raise ValueError("群聊环境快照不能为空")

        def write() -> GroupEnvironmentRecord:
            cursor = self.context._conn.execute(
                """
                INSERT INTO group_environments(
                    session_id, group_id, group_name, date, atmosphere, topic, topic_owner,
                    active_users, is_multithread, is_spam, is_repetition, is_discussing_bot,
                    suitable_to_join, bot_watch_state, participation_desire, complexity_score,
                    understanding_confidence, deep_analysis_needed, summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.context._text(item.session_id),
                    self.context._text(item.group_id),
                    self.context._text(item.group_name),
                    self.context._text(item.date),
                    self.context._text(item.atmosphere),
                    self.context._text(item.topic),
                    self.context._text(item.topic_owner),
                    max(int(item.active_users or 0), 0),
                    self.context._flag(item.is_multithread),
                    self.context._flag(item.is_spam),
                    self.context._flag(item.is_repetition),
                    self.context._flag(item.is_discussing_bot),
                    self.context._text(item.suitable_to_join),
                    self.context._text(item.bot_watch_state),
                    max(int(item.participation_desire or 0), 0),
                    max(int(item.complexity_score or 0), 0),
                    max(int(item.understanding_confidence or 0), 0),
                    self.context._flag(item.deep_analysis_needed),
                    self.context._text(item.summary),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM group_environments WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self.context._compose_group_environment(row)

        return await self.context._run_db(write)

    async def get_recent_group_environments(
        self, limit: int = 8
    ) -> list[GroupEnvironmentRecord]:
        def read() -> list[GroupEnvironmentRecord]:
            sql = """
                SELECT *
                FROM group_environments
                WHERE group_id <> ''
                ORDER BY id DESC
            """
            params: tuple[Any, ...] = ()
            if limit > 0:
                sql += " LIMIT ?"
                params = (limit,)
            rows = self.context._conn.execute(sql, params).fetchall()
            return [self.context._compose_group_environment(row) for row in rows]

        return await self.context._run_db(read)

    def _compose_message_visibility(self, row: sqlite3.Row) -> MessageVisibilityRecord:
        return MessageVisibilityRecord(
            id=int(row["id"] or 0),
            session_id=row["session_id"],
            message_id=row["message_id"],
            sender_profile_id=row["sender_profile_id"],
            sender_name=row["sender_name"],
            group_id=row["group_id"],
            group_name=row["group_name"],
            date=row["date"],
            visibility=row["visibility"],
            attention_level=int(row["attention_level"] or 0),
            priority=row["priority"],
            is_directed_at_bot=bool(row["is_directed_at_bot"]),
            freshness=row["freshness"],
            psychological_freshness=int(row["psychological_freshness"] or 0),
            reactivated_from_id=int(row["reactivated_from_id"] or 0),
            reactivation_hint=self.context._text(row["reactivation_hint"]),
            reason=compact_explanation_text(row["reason"]),
            created_at=row["created_at"],
        )

    async def save_message_visibility(
        self, visibility: MessageVisibilityRecord
    ) -> MessageVisibilityRecord:
        item = MessageVisibilityRecord.from_value(
            visibility.as_dict()
            if isinstance(visibility, MessageVisibilityRecord)
            else visibility
        )
        if not item:
            raise ValueError("消息可见性记录不能为空")

        def write() -> MessageVisibilityRecord:
            cursor = self.context._conn.execute(
                """
                INSERT INTO message_visibility(
                    session_id, message_id, sender_profile_id, sender_name, group_id, group_name,
                    date, visibility, attention_level, priority, is_directed_at_bot,
                    freshness, psychological_freshness, reactivated_from_id, reactivation_hint, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.context._text(item.session_id),
                    self.context._text(item.message_id),
                    self.context._text(item.sender_profile_id),
                    self.context._text(item.sender_name),
                    self.context._text(item.group_id),
                    self.context._text(item.group_name),
                    self.context._text(item.date),
                    self.context._text(item.visibility) or "seen",
                    max(int(item.attention_level or 0), 0),
                    self.context._text(item.priority) or "normal",
                    self.context._flag(item.is_directed_at_bot),
                    self.context._text(item.freshness),
                    max(int(item.psychological_freshness or 0), 0),
                    max(int(item.reactivated_from_id or 0), 0),
                    self.context._text(item.reactivation_hint),
                    self.context._text(item.reason),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM message_visibility WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self.context._compose_message_visibility(row)

        return await self.context._run_db(write)

    async def get_recent_message_visibility(
        self, limit: int = 8
    ) -> list[MessageVisibilityRecord]:
        def read() -> list[MessageVisibilityRecord]:
            rows = self.context._recent_scoped_rows_unlocked("message_visibility", limit)
            return [self.context._compose_message_visibility(row) for row in rows]

        return await self.context._run_db(read)

    async def get_message_visibility_records(
        self, limit: int = 20
    ) -> list[MessageVisibilityRecord]:
        def read() -> list[MessageVisibilityRecord]:
            sql = "SELECT * FROM message_visibility ORDER BY id DESC"
            params: tuple[Any, ...] = ()
            if limit > 0:
                sql += " LIMIT ?"
                params = (limit,)
            rows = self.context._conn.execute(sql, params).fetchall()
            return [self.context._compose_message_visibility(row) for row in rows]

        return await self.context._run_db(read)

    def _compose_action_decision(self, row: sqlite3.Row) -> ActionDecisionRecord:
        return ActionDecisionRecord(
            id=int(row["id"] or 0),
            session_id=row["session_id"],
            message_id=row["message_id"],
            sender_profile_id=row["sender_profile_id"],
            sender_name=row["sender_name"],
            group_id=row["group_id"],
            group_name=row["group_name"],
            date=row["date"],
            action=row["action"],
            reason=compact_explanation_text(row["reason"]),
            confidence=float(row["confidence"] or 0.0),
            scene_type=row["scene_type"],
            topic_owner=row["topic_owner"],
            understanding=row["understanding"],
            deep_analysis=bool(row["deep_analysis"]),
            inner_monologue=self.context._text(row["inner_monologue"]),
            reply_strategy=self.context._text(row["reply_strategy"]),
            created_at=row["created_at"],
        )

    async def save_action_decision(
        self, decision: ActionDecisionRecord
    ) -> ActionDecisionRecord:
        item = ActionDecisionRecord.from_value(
            decision.as_dict()
            if isinstance(decision, ActionDecisionRecord)
            else decision
        )
        if not item:
            raise ValueError("动作裁定记录不能为空")

        def write() -> ActionDecisionRecord:
            if _coalescible_action_decision(item):
                existing = self.context._conn.execute(
                    """
                    SELECT id
                    FROM action_decisions
                    WHERE session_id = ?
                      AND sender_profile_id = ?
                      AND group_id = ?
                      AND date = ?
                      AND action = ?
                      AND scene_type = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        self.context._text(item.session_id),
                        self.context._text(item.sender_profile_id),
                        self.context._text(item.group_id),
                        self.context._text(item.date),
                        self.context._text(item.action),
                        self.context._text(item.scene_type),
                    ),
                ).fetchone()
                if existing:
                    self.context._conn.execute(
                        """
                        UPDATE action_decisions
                        SET message_id = ?, sender_name = ?, group_name = ?,
                            reason = ?, confidence = ?, topic_owner = ?,
                            understanding = ?, deep_analysis = ?,
                            inner_monologue = ?, reply_strategy = ?,
                            created_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            self.context._text(item.message_id),
                            self.context._text(item.sender_name),
                            self.context._text(item.group_name),
                            self.context._text(item.reason),
                            max(float(item.confidence or 0.0), 0.0),
                            self.context._text(item.topic_owner),
                            self.context._text(item.understanding),
                            self.context._flag(item.deep_analysis),
                            self.context._text(item.inner_monologue),
                            self.context._text(item.reply_strategy),
                            int(existing["id"]),
                        ),
                    )
                    self.context._conn.commit()
                    row = self.context._conn.execute(
                        "SELECT * FROM action_decisions WHERE id = ?",
                        (int(existing["id"]),),
                    ).fetchone()
                    return self.context._compose_action_decision(row)
            cursor = self.context._conn.execute(
                """
                INSERT INTO action_decisions(
                    session_id, message_id, sender_profile_id, sender_name, group_id, group_name,
                    date, action, reason, confidence, scene_type, topic_owner, understanding,
                    deep_analysis, inner_monologue, reply_strategy
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.context._text(item.session_id),
                    self.context._text(item.message_id),
                    self.context._text(item.sender_profile_id),
                    self.context._text(item.sender_name),
                    self.context._text(item.group_id),
                    self.context._text(item.group_name),
                    self.context._text(item.date),
                    self.context._text(item.action),
                    self.context._text(item.reason),
                    max(float(item.confidence or 0.0), 0.0),
                    self.context._text(item.scene_type),
                    self.context._text(item.topic_owner),
                    self.context._text(item.understanding),
                    self.context._flag(item.deep_analysis),
                    self.context._text(item.inner_monologue),
                    self.context._text(item.reply_strategy),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM action_decisions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self.context._compose_action_decision(row)

        return await self.context._run_db(write)

    async def get_recent_action_decisions(
        self, limit: int = 8
    ) -> list[ActionDecisionRecord]:
        def read() -> list[ActionDecisionRecord]:
            rows = self.context._recent_scoped_rows_unlocked("action_decisions", limit)
            return [self.context._compose_action_decision(row) for row in rows]

        return await self.context._run_db(read)

    async def get_action_decision_records(
        self, limit: int = 20
    ) -> list[ActionDecisionRecord]:
        sql = "SELECT * FROM action_decisions ORDER BY id DESC"
        params: tuple[Any, ...] = ()
        if limit > 0:
            sql += " LIMIT ?"
            params = (limit,)

        def read() -> list[ActionDecisionRecord]:
            rows = self.context._conn.execute(sql, params).fetchall()
            return [self.context._compose_action_decision(row) for row in rows]

        return await self.context._run_db(read)
