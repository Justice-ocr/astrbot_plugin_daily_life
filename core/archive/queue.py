from __future__ import annotations

import datetime
import math
from typing import Any, Protocol

from ..clock import TIMEZONE


class ChatMemoryQueueArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _conn: Any
    _purge_completed_chat_memory_messages_unlocked: Any
    _run_db: Any
    _text: Any


class ChatMemoryQueueArchiveComponent:
    def __init__(self, context: ChatMemoryQueueArchiveComponentContext) -> None:
        self.context = context

    async def save_conversation_arrival(self, scope: str, received_at: float) -> None:
        if not scope or not math.isfinite(received_at) or received_at <= 0:
            return

        def write() -> None:
            self.context._conn.execute(
                "INSERT OR IGNORE INTO conversation_arrivals(scope, received_at) VALUES (?, ?)",
                (scope, received_at),
            )
            self.context._conn.execute(
                "DELETE FROM conversation_arrivals WHERE scope = ? AND received_at NOT IN "
                "(SELECT received_at FROM conversation_arrivals WHERE scope = ? ORDER BY received_at DESC LIMIT 32)",
                (scope, scope),
            )

        await self.context._run_db(write)

    async def get_conversation_arrival_before(
        self, scope: str, received_at: float
    ) -> float:
        def read() -> float:
            row = self.context._conn.execute(
                "SELECT received_at FROM conversation_arrivals WHERE scope = ? AND received_at < ? "
                "ORDER BY received_at DESC LIMIT 1",
                (scope, received_at),
            ).fetchone()
            if row:
                return float(row["received_at"])
            # 首次使用时，以已有档案作为事实基线。
            rows = self.context._conn.execute(
                "SELECT occurred_at FROM chat_memory_messages WHERE session_id = ? "
                "AND role = 'user' ORDER BY id DESC LIMIT 32",
                (scope,),
            ).fetchall()
            times = []
            for item in rows:
                try:
                    value = datetime.datetime.fromisoformat(item["occurred_at"])
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=TIMEZONE)
                    stamp = value.timestamp()
                    # 旧版消息档案可能将接收时间截断到整秒。
                    if 0 < stamp < math.floor(received_at):
                        times.append(stamp)
                except (ValueError, TypeError, OverflowError):
                    continue
            return max(times, default=0.0)

        return await self.context._run_db(read)

    async def save_conversation_receipt(self, scope: str, sent_at: float) -> None:
        if not scope or not math.isfinite(sent_at) or sent_at <= 0:
            return

        def write() -> None:
            self.context._conn.execute(
                "INSERT OR IGNORE INTO conversation_receipts(scope, sent_at) VALUES (?, ?)",
                (scope, sent_at),
            )
            self.context._conn.execute(
                "DELETE FROM conversation_receipts WHERE scope = ? AND sent_at NOT IN "
                "(SELECT sent_at FROM conversation_receipts WHERE scope = ? ORDER BY sent_at DESC LIMIT 32)",
                (scope, scope),
            )

        await self.context._run_db(write)

    async def get_conversation_receipt_before(self, scope: str, received_at: float) -> float:
        def read() -> float:
            row = self.context._conn.execute(
                "SELECT sent_at FROM conversation_receipts WHERE scope = ? AND sent_at <= ? "
                "ORDER BY sent_at DESC LIMIT 1",
                (scope, received_at),
            ).fetchone()
            if row:
                return float(row["sent_at"])
            # 首次启用时仅使用已有的真实发送归档，缺少记录就保持未知。
            rows = self.context._conn.execute(
                "SELECT occurred_at FROM chat_memory_messages WHERE session_id = ? "
                "AND role = 'assistant' ORDER BY id DESC LIMIT 32",
                (scope,),
            ).fetchall()
            times = []
            for item in rows:
                try:
                    value = datetime.datetime.fromisoformat(item["occurred_at"])
                    if value.tzinfo is None:
                        value = value.replace(tzinfo=TIMEZONE)
                    stamp = value.timestamp()
                    if 0 < stamp <= received_at:
                        times.append(stamp)
                except (ValueError, TypeError, OverflowError):
                    continue
            return max(times, default=0.0)

        return await self.context._run_db(read)

    async def get_conversation_contact_state(self, scope: str, now: float) -> dict:
        """读取同一对象的收发事实；跨插件共享，重启后仍能恢复。"""
        if not scope or not math.isfinite(now) or now <= 0:
            raise ValueError("联系记录需要有效会话与时间")
        arrival = await self.get_conversation_arrival_before(scope, now + 0.001)
        sent = await self.get_conversation_receipt_before(scope, now)

        def read() -> dict:
            rows = self.context._conn.execute(
                "SELECT sent_at FROM conversation_receipts WHERE scope = ? "
                "AND sent_at > ? AND sent_at <= ? ORDER BY sent_at",
                (scope, max(arrival, now - 6 * 3600), now),
            ).fetchall()
            receipts = [float(row["sent_at"]) for row in rows]
            if not receipts and sent > max(arrival, now - 6 * 3600):
                receipts = [sent]
            return {"last_received_at": arrival, "last_sent_at": sent, "receipts": receipts}

        return await self.context._run_db(read)

    def _purge_completed_chat_memory_messages_unlocked(
        self, *, cutoff: str = "", keep_recent: int = 200
    ) -> int:
        cutoff_filter = ""
        params: list[Any] = []
        if cutoff:
            cutoff_filter = (
                "AND COALESCE(NULLIF(m.occurred_at, ''), m.created_at) < ?"
            )
            params.append(self.context._text(cutoff))
        params.append(max(0, int(keep_recent)))
        cursor = self.context._conn.execute(
            f"""
            DELETE FROM chat_memory_messages
            WHERE id IN (
                SELECT m.id FROM chat_memory_messages m
                JOIN chat_memory_sessions s ON s.session_id = m.session_id
                WHERE m.id <= s.last_processed_row_id
                  {cutoff_filter}
                ORDER BY m.id DESC LIMIT -1 OFFSET ?
            )
            """,
            tuple(params),
        )
        return max(0, int(cursor.rowcount or 0))

    async def enqueue_chat_memory_message(
        self, snapshot: dict[str, Any]
    ) -> tuple[int, bool]:
        def write() -> tuple[int, bool]:
            # 在摘要、队列清理和上游会话压缩之前持久保留实际消息。
            if snapshot.get("role") in {"user", "assistant"} and snapshot.get("message_text"):
                self.context._conn.execute(
                    "INSERT OR IGNORE INTO conversation_originals VALUES(?,?,?,?,?,?)",
                    tuple(str(snapshot.get(k) or "") for k in
                          ("session_id", "event_key", "role", "sender_name", "occurred_at", "message_text")),
                )
            cursor = self.context._conn.execute(
                """
                INSERT OR IGNORE INTO chat_memory_messages(
                    event_key, session_id, role, message_id, sender_profile_id, sender_name,
                    platform, user_id, group_id, group_name, is_group, is_directed,
                    is_quoted, message_text, message_facts, quote_context,
                    structured_context, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.context._text(snapshot.get("event_key")),
                    self.context._text(snapshot.get("session_id")),
                    self.context._text(snapshot.get("role")) or "user",
                    self.context._text(snapshot.get("message_id")),
                    self.context._text(snapshot.get("sender_profile_id")),
                    self.context._text(snapshot.get("sender_name")),
                    self.context._text(snapshot.get("platform")),
                    self.context._text(snapshot.get("user_id")),
                    self.context._text(snapshot.get("group_id")),
                    self.context._text(snapshot.get("group_name")),
                    int(bool(snapshot.get("is_group"))),
                    int(bool(snapshot.get("is_directed"))),
                    int(bool(snapshot.get("is_quoted"))),
                    self.context._text(snapshot.get("message_text")),
                    self.context._text(snapshot.get("message_facts")),
                    self.context._text(snapshot.get("quote_context")),
                    self.context._text(snapshot.get("structured_context")),
                    self.context._text(snapshot.get("occurred_at")),
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                row_id = int(cursor.lastrowid)
                self.context._conn.execute(
                    """
                    INSERT INTO chat_memory_sessions(
                        session_id, is_group, pending_since, last_message_at, updated_at
                    ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(session_id) DO UPDATE SET
                        is_group = excluded.is_group,
                        pending_since = CASE
                            WHEN chat_memory_sessions.last_processed_row_id >= ? THEN excluded.pending_since
                            ELSE chat_memory_sessions.pending_since
                        END,
                        last_message_at = excluded.last_message_at,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self.context._text(snapshot.get("session_id")),
                        int(bool(snapshot.get("is_group"))),
                        self.context._text(snapshot.get("occurred_at")),
                        self.context._text(snapshot.get("occurred_at")),
                        row_id - 1,
                    ),
                )
                self.context._conn.commit()
                return row_id, True
            row = self.context._conn.execute(
                "SELECT id FROM chat_memory_messages WHERE event_key = ?",
                (self.context._text(snapshot.get("event_key")),),
            ).fetchone()
            return (int(row["id"]) if row else 0), False

        return await self.context._run_db(write)

    async def list_chat_memory_sessions(self) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            rows = self.context._conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM chat_memory_messages m
                        WHERE m.session_id = s.session_id AND m.id > s.last_processed_row_id) AS pending_count,
                       (SELECT COUNT(*) FROM chat_memory_messages m
                        WHERE m.session_id = s.session_id
                          AND m.id > s.last_processed_row_id
                          AND m.role = 'user') AS pending_user_count,
                       (SELECT COUNT(*) FROM chat_memory_messages m
                        WHERE m.session_id = s.session_id
                          AND m.id > s.last_processed_row_id
                          AND m.role = 'assistant') AS pending_assistant_count
                FROM chat_memory_sessions s
                WHERE EXISTS (
                    SELECT 1 FROM chat_memory_messages m
                    WHERE m.session_id = s.session_id AND m.id > s.last_processed_row_id
                )
                ORDER BY s.last_message_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

        return await self.context._run_db(read)

    async def begin_chat_memory_batch(
        self,
        session_id: str,
        *,
        max_messages: int,
        max_chars: int,
    ) -> dict[str, Any] | None:
        def write() -> dict[str, Any] | None:
            state = self.context._conn.execute(
                "SELECT last_processed_row_id FROM chat_memory_sessions WHERE session_id = ?",
                (self.context._text(session_id),),
            ).fetchone()
            if not state:
                return None
            rows = self.context._conn.execute(
                """
                SELECT * FROM chat_memory_messages
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (
                    self.context._text(session_id),
                    int(state["last_processed_row_id"]),
                    max(1, int(max_messages)),
                ),
            ).fetchall()
            selected = []
            total_chars = 0
            for row in rows:
                row_chars = len(str(row["message_text"] or "")) + len(
                    str(row["message_facts"] or "")
                )
                if selected and total_chars + row_chars > max(1, int(max_chars)):
                    break
                selected.append(dict(row))
                total_chars += row_chars
            if not selected:
                return None
            first_id, last_id = int(selected[0]["id"]), int(selected[-1]["id"])
            batch_key = f"{session_id}:{first_id}:{last_id}"
            existing = self.context._conn.execute(
                "SELECT * FROM chat_memory_batches WHERE batch_key = ?",
                (batch_key,),
            ).fetchone()
            if existing and str(existing["status"]) in {"completed", "dead"}:
                self.context._conn.execute(
                    "UPDATE chat_memory_sessions SET last_processed_row_id = MAX(last_processed_row_id, ?), updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                    (last_id, self.context._text(session_id)),
                )
                self.context._conn.commit()
                return None
            if existing:
                next_attempt_at = str(existing["next_attempt_at"] or "")
                if next_attempt_at:
                    try:
                        due_at = datetime.datetime.fromisoformat(next_attempt_at)
                        if due_at.tzinfo is None:
                            due_at = due_at.replace(tzinfo=datetime.timezone.utc)
                        if due_at > datetime.datetime.now(datetime.timezone.utc):
                            return None
                    except ValueError:
                        pass
                batch_id = int(existing["id"])
                attempt_count = int(existing["attempt_count"] or 1) + 1
                self.context._conn.execute(
                    "UPDATE chat_memory_batches SET status = 'processing', attempt_count = attempt_count + 1, error = '', next_attempt_at = '' WHERE id = ?",
                    (batch_id,),
                )
            else:
                cursor = self.context._conn.execute(
                    """
                    INSERT INTO chat_memory_batches(
                        batch_key, session_id, first_row_id, last_row_id, message_count, status
                    ) VALUES (?, ?, ?, ?, ?, 'processing')
                    """,
                    (
                        batch_key,
                        self.context._text(session_id),
                        first_id,
                        last_id,
                        len(selected),
                    ),
                )
                batch_id = int(cursor.lastrowid)
                attempt_count = 1
            self.context._conn.commit()
            return {
                "id": batch_id,
                "batch_key": batch_key,
                "session_id": session_id,
                "messages": selected,
                "attempt_count": attempt_count,
            }

        return await self.context._run_db(write)

    async def complete_chat_memory_batch(
        self, batch_id: int, summary_id: int = 0
    ) -> None:
        def write() -> None:
            batch = self.context._conn.execute(
                "SELECT session_id, last_row_id FROM chat_memory_batches WHERE id = ?",
                (int(batch_id),),
            ).fetchone()
            if not batch:
                return
            self.context._conn.execute(
                "UPDATE chat_memory_batches SET status = 'completed', summary_id = ?, error = '', completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (max(0, int(summary_id)), int(batch_id)),
            )
            self.context._conn.execute(
                """
                UPDATE chat_memory_sessions
                SET last_processed_row_id = MAX(last_processed_row_id, ?),
                    pending_since = CASE WHEN EXISTS(
                        SELECT 1 FROM chat_memory_messages m
                        WHERE m.session_id = chat_memory_sessions.session_id AND m.id > ?
                    ) THEN (
                        SELECT occurred_at FROM chat_memory_messages m
                        WHERE m.session_id = chat_memory_sessions.session_id AND m.id > ? ORDER BY m.id LIMIT 1
                    ) ELSE '' END,
                    last_completed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                (
                    int(batch["last_row_id"]),
                    int(batch["last_row_id"]),
                    int(batch["last_row_id"]),
                    str(batch["session_id"]),
                ),
            )
            self.context._conn.commit()

        await self.context._run_db(write)

    async def fail_chat_memory_batch(
        self,
        batch_id: int,
        error: str,
        *,
        retry_after_seconds: float = 0.0,
        max_attempts: int = 4,
        permanent: bool = False,
    ) -> str:
        """记录失败并把批次置于退避、或移动到可审计的死信终态。"""

        def write() -> str:
            batch = self.context._conn.execute(
                "SELECT session_id, last_row_id, attempt_count FROM chat_memory_batches WHERE id = ?",
                (int(batch_id),),
            ).fetchone()
            if not batch:
                return "missing"
            attempts = int(batch["attempt_count"] or 1)
            dead = bool(permanent) or attempts >= max(1, int(max_attempts))
            status = "dead" if dead else "retry_wait"
            retry_at = ""
            if not dead and float(retry_after_seconds or 0) > 0:
                retry_at = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(seconds=float(retry_after_seconds))
                ).isoformat(timespec="seconds")
            self.context._conn.execute(
                "UPDATE chat_memory_batches SET status = ?, error = ?, next_attempt_at = ?, failed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, self.context._text(error)[:1000], retry_at, int(batch_id)),
            )
            if dead:
                self.context._conn.execute(
                    "UPDATE chat_memory_sessions SET last_processed_row_id = MAX(last_processed_row_id, ?), pending_since = CASE WHEN EXISTS (SELECT 1 FROM chat_memory_messages m WHERE m.session_id = chat_memory_sessions.session_id AND m.id > ?) THEN (SELECT occurred_at FROM chat_memory_messages m WHERE m.session_id = chat_memory_sessions.session_id AND m.id > ? ORDER BY m.id LIMIT 1) ELSE '' END, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                    (
                        int(batch["last_row_id"]),
                        int(batch["last_row_id"]),
                        int(batch["last_row_id"]),
                        str(batch["session_id"]),
                    ),
                )
            self.context._conn.commit()
            return status

        return await self.context._run_db(write)

    async def purge_completed_chat_memory_messages(self, keep_recent: int = 200) -> int:
        def write() -> int:
            deleted = self.context._purge_completed_chat_memory_messages_unlocked(
                keep_recent=keep_recent
            )
            self.context._conn.commit()
            return deleted

        return await self.context._run_db(write)
