from __future__ import annotations

import sqlite3
from typing import Any

from ..models import ChatSummaryRecord

from typing import Protocol


class SummaryArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _compose_chat_summary: Any
    _conn: Any
    _get_people_unlocked: Any
    _replace_people_unlocked: Any
    _run_db: Any
    _text: Any


class SummaryArchiveComponent:
    def __init__(self, context: SummaryArchiveComponentContext) -> None:
        self.context = context

    def _compose_chat_summary(self, row: sqlite3.Row) -> ChatSummaryRecord:
        return ChatSummaryRecord(
            id=int(row["id"] or 0),
            session_id=row["session_id"],
            date=row["date"],
            brief=self.context._text(row["brief"]),
            long_summary=self.context._text(row["long_summary"]),
            people=self.context._get_people_unlocked(
                "chat_summary_people", "summary_id", row["id"]
            ),
            source=row["source"],
            created_at=row["created_at"],
        )

    async def save_chat_summary(self, summary: ChatSummaryRecord) -> ChatSummaryRecord:
        item = ChatSummaryRecord.from_value(
            summary.as_dict() if isinstance(summary, ChatSummaryRecord) else summary
        )
        if not item:
            raise ValueError("聊天摘要不能为空")

        def write() -> ChatSummaryRecord:
            if item.id:
                cursor = self.context._conn.execute(
                    """
                    UPDATE chat_summaries
                    SET session_id = ?, date = ?, brief = ?, long_summary = ?, source = ?
                    WHERE id = ?
                    """,
                    (
                        self.context._text(item.session_id),
                        self.context._text(item.date),
                        self.context._text(item.brief),
                        self.context._text(item.long_summary),
                        self.context._text(item.source) or "chat",
                        int(item.id),
                    ),
                )
                summary_id = int(item.id)
                if cursor.rowcount <= 0:
                    item.id = 0
            if not item.id:
                cursor = self.context._conn.execute(
                    """
                    INSERT INTO chat_summaries(session_id, date, brief, long_summary, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        self.context._text(item.session_id),
                        self.context._text(item.date),
                        self.context._text(item.brief),
                        self.context._text(item.long_summary),
                        self.context._text(item.source) or "chat",
                    ),
                )
                summary_id = int(cursor.lastrowid)
            self.context._replace_people_unlocked(
                "chat_summary_people", "summary_id", summary_id, item.people
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM chat_summaries WHERE id = ?", (summary_id,)
            ).fetchone()
            return self.context._compose_chat_summary(row)

        return await self.context._run_db(write)

    async def get_recent_chat_summaries(
        self, limit: int = 8
    ) -> list[ChatSummaryRecord]:
        def read() -> list[ChatSummaryRecord]:
            sql = "SELECT * FROM chat_summaries ORDER BY id DESC"
            params: tuple[Any, ...] = ()
            if limit > 0:
                sql += " LIMIT ?"
                params = (limit,)
            rows = self.context._conn.execute(sql, params).fetchall()
            return [self.context._compose_chat_summary(row) for row in rows]

        return await self.context._run_db(read)

    async def get_chat_summaries_for_session(
        self, session_id: str, limit: int = 5
    ) -> list[ChatSummaryRecord]:
        scope = self.context._text(session_id)
        if not scope:
            return []

        def read() -> list[ChatSummaryRecord]:
            sql = "SELECT * FROM chat_summaries WHERE session_id = ? ORDER BY id DESC"
            params: list[Any] = [scope]
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_chat_summary(row) for row in rows]

        return await self.context._run_db(read)
