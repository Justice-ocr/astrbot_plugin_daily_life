"""记忆的原话、时效校验、关联召回与无模型后台整理。"""

from __future__ import annotations

import datetime
import json
from typing import Any, Protocol

from ..clock import now as life_now
from .longterm import LongTermMemoryArchiveComponent
from .memorypolicy import unprotected_memory_clause


class RecollectionArchiveContext(Protocol):
    _conn: Any
    _run_db: Any
    _compose_long_term_memory: Any
    _long_term_memory_scope_clause: Any


class RecollectionArchiveComponent:
    def __init__(self, context: RecollectionArchiveContext) -> None:
        self.context = context

    def _transaction(self, operation):
        with self.context._conn:
            if not self.context._conn.in_transaction:
                self.context._conn.execute("BEGIN IMMEDIATE")
            return operation()

    def _save_sources(self, summary_id: int, messages: list[dict]) -> None:
        conn = self.context._conn
        summary = conn.execute(
            "SELECT session_id FROM chat_summaries WHERE id = ?", (summary_id,)
        ).fetchone()
        if not summary:
            return
        session = summary["session_id"]
        for row in messages:
            if row.get("session_id") != session:
                continue
            text = str(row.get("message_text") or "")
            event_key = str(row.get("event_key") or row.get("id") or "")
            role = str(row.get("role") or "")
            if not text or not event_key or role not in {"user", "assistant"}:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO memory_source_messages "
                "(summary_id, event_key, session_id, message_id, role, sender_name, "
                "occurred_at, message_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (summary_id, event_key, session, str(row.get("message_id") or row.get("id") or ""),
                 role, str(row.get("sender_name") or ""), str(row.get("occurred_at") or ""), text),
            )
        # 只连接同会话真实消息所支撑的事实；NONE 返回的旧事实也必须核对来源。
        conn.execute(
            "INSERT OR IGNORE INTO memory_fact_links(memory_id, fact_id) "
            "SELECT m.id, f.id FROM long_term_memories m "
            "JOIN memory_source_messages s ON s.summary_id = ? "
            "JOIN temporal_facts f ON f.scope = s.session_id "
            "AND f.source_type = 'chat_message' AND f.source_id = s.message_id "
            "WHERE m.source_table = 'chat_summaries' AND m.source_id = CAST(? AS TEXT) "
            "AND (m.scope = s.session_id OR m.session_id = s.session_id)",
            (summary_id, summary_id),
        )

    async def save_memory_sources(
        self, summary_id: int, messages: list[dict], *, fact_ids: list[int] | None = None
    ) -> None:
        """只保留已沉淀摘要的实际消息，队列清理后仍能按需查证。"""
        def write():
            self._save_sources(int(summary_id), messages)
            for fact_id in dict.fromkeys((fact_ids or [])[:12]):
                self.context._conn.execute(
                    "INSERT OR IGNORE INTO memory_fact_links(memory_id, fact_id) "
                    "SELECT m.id, f.id FROM long_term_memories m "
                    "JOIN chat_summaries s ON CAST(s.id AS TEXT) = m.source_id "
                    "JOIN temporal_facts f ON f.scope = s.session_id AND f.id = ? "
                    "WHERE s.id = ? AND m.source_table = 'chat_summaries' "
                    "AND (m.scope = s.session_id OR m.session_id = s.session_id)",
                    (int(fact_id), int(summary_id)),
                )
        await self.context._run_db(self._transaction, write)

    async def get_memory_recall_context(
        self, memory_ids: list[int], *, scopes: list[str], as_of: str = ""
    ) -> dict[int, str]:
        if not memory_ids or not scopes:
            return {}
        point = as_of or life_now().isoformat(sep=" ", timespec="seconds")

        def read():
            conn = self.context._conn
            clause, params = self.context._long_term_memory_scope_clause(scopes, include_self=True)
            result = {}
            for memory_id in dict.fromkeys(memory_ids[:36]):
                memory = conn.execute(
                    f"SELECT m.* FROM long_term_memories m WHERE m.id = ? {clause}",
                    (int(memory_id), *params),
                ).fetchone()
                if not memory:
                    continue
                facts = conn.execute(
                    "SELECT f.* FROM temporal_facts f JOIN memory_fact_links l "
                    "ON l.fact_id = f.id WHERE l.memory_id = ? "
                    "UNION SELECT f.* FROM temporal_facts f "
                    "JOIN chat_memory_messages q ON q.session_id = f.scope "
                    "AND f.source_type = 'chat_message' AND f.source_id = q.message_id "
                    "JOIN chat_memory_batches b ON q.session_id = b.session_id "
                    "AND q.id BETWEEN b.first_row_id AND b.last_row_id "
                    "WHERE ? = 'chat_summaries' AND CAST(b.summary_id AS TEXT) = ? "
                    "AND b.session_id = ? ORDER BY id LIMIT 12",
                    (memory_id, memory["source_table"], memory["source_id"], memory["session_id"]),
                ).fetchall()
                notes = []
                for fact in facts:
                    old = self._fact_text(fact)
                    provenance = json.loads(fact["provenance_json"] or "{}")
                    if fact["status"] == "uncertain" or provenance.get("epistemic_status") in {"inferred", "uncertain"}:
                        notes.append(f"未确认：{old}")
                        continue
                    current = conn.execute(
                        "SELECT * FROM temporal_facts WHERE scope = ? AND subject = ? "
                        "AND predicate = ? AND status <> 'uncertain' AND life_timestamp(valid_from) <= life_timestamp(?) "
                        "AND COALESCE(json_extract(provenance_json, '$.epistemic_status'), '') NOT IN ('inferred', 'uncertain') "
                        "AND (valid_to = '' OR life_timestamp(valid_to) > life_timestamp(?)) "
                        "ORDER BY life_timestamp(valid_from) DESC, id DESC LIMIT 1",
                        (fact["scope"], fact["subject"], fact["predicate"], point, point),
                    ).fetchone()
                    if current and current["id"] == fact["id"]:
                        continue
                    end = fact["valid_to"] or fact["valid_from"]
                    note = f"历史事实（{fact['valid_from']} 至 {end}）：{old}"
                    if current:
                        note += f"；截至 {point}：{self._fact_text(current)}"
                    else:
                        note += "；当前未确认有效"
                    notes.append(note)
                if notes:
                    result[int(memory_id)] = (
                        "摘要包含历史或未确认信息，不得作为当前事实：" + "；".join(dict.fromkeys(notes))
                    )
            return result
        return await self.context._run_db(read)

    @staticmethod
    def _fact_text(row) -> str:
        try:
            value = json.loads(row["object_json"])
        except (ValueError, TypeError):
            value = None
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return f"{row['subject']} / {row['predicate']} = {text[:240]}"

    async def get_related_long_term_memories(
        self, memory_ids: list[int], *, scopes: list[str], categories: list[str], limit: int = 12
    ) -> list[Any]:
        """沿已存的经历和人物、地点链接补充候选，不根据文本猜测关系。"""
        if not memory_ids or not scopes:
            return []

        def read():
            clause, params = self.context._long_term_memory_scope_clause(scopes, include_self=True)
            seed_ids = list(dict.fromkeys(int(value) for value in memory_ids[:8]))
            placeholders = ",".join("?" for _ in seed_ids)
            # 种子和邻居均校验可见范围，防止通过关联跨会话读取。
            seeds = self.context._conn.execute(
                f"SELECT m.id FROM long_term_memories m WHERE m.id IN ({placeholders}) "
                f"AND m.status = 'active' {clause}", (*seed_ids, *params),
            ).fetchall()
            if not seeds:
                return []
            seed_ids = [row["id"] for row in seeds]
            placeholders = ",".join("?" for _ in seed_ids)
            category_clause = ""
            if categories:
                category_clause = f" AND m.category IN ({','.join('?' for _ in categories)})"
            rows = self.context._conn.execute(
                f"""
                WITH neighbors AS (
                    SELECT b.memory_id, 3.0 AS strength
                    FROM memory_episode_cluster_items a
                    JOIN memory_episode_cluster_items b ON b.cluster_id = a.cluster_id
                    JOIN memory_episode_clusters c ON c.id = a.cluster_id AND c.status = 'active'
                    WHERE a.memory_id IN ({placeholders})
                    UNION ALL
                    SELECT b.memory_id, 1.0 AS strength FROM memory_entity_links a
                    JOIN memory_entity_links b ON b.entity_id = a.entity_id
                    JOIN memory_entities e ON e.id = a.entity_id
                    WHERE a.memory_id IN ({placeholders}) AND e.status = 'active'
                      AND e.entity_type IN ('person', 'place')
                )
                SELECT m.*, SUM(n.strength) AS relation_score FROM neighbors n
                JOIN long_term_memories m ON m.id = n.memory_id
                WHERE m.id NOT IN ({placeholders}) AND m.status = 'active'
                  AND (m.expires_at = '' OR m.expires_at >= DATE('now', 'localtime'))
                  {clause} {category_clause}
                  {LongTermMemoryArchiveComponent._active_source_revision_clause()}
                GROUP BY m.id ORDER BY relation_score DESC, m.date DESC, m.id DESC LIMIT ?
                """,
                (*seed_ids, *seed_ids, *seed_ids, *params, *categories, max(1, min(limit, 24))),
            ).fetchall()
            return [self.context._compose_long_term_memory(row) for row in rows]
        return await self.context._run_db(read)

    async def read_memory_evidence(
        self, memory_id: int, *, scopes: list[str], offset: int = 0, limit: int = 4000
    ) -> dict:
        """原话按字符分页，明确区分摘要、用户原话和角色原话。"""
        if not scopes:
            return {"status": "unavailable"}
        offset, limit = max(0, int(offset)), max(200, min(int(limit), 6000))

        def read():
            conn = self.context._conn
            clause, params = self.context._long_term_memory_scope_clause(scopes, include_self=True)
            memory = conn.execute(
                f"SELECT m.* FROM long_term_memories m WHERE m.id = ? {clause}",
                (int(memory_id), *params),
            ).fetchone()
            if not memory:
                return {"status": "unavailable"}
            if memory["session_id"] and memory["session_id"] not in scopes:
                return {"status": "unavailable"}
            rows = []
            if memory["source_table"] == "chat_summaries":
                rows = conn.execute(
                    "SELECT role, sender_name, occurred_at, message_id, message_text "
                    "FROM memory_source_messages WHERE summary_id = ? AND session_id = ? "
                    "ORDER BY occurred_at, rowid",
                    (memory["source_id"], memory["session_id"]),
                ).fetchall()
                if not rows:
                    rows = conn.execute(
                        "SELECT q.role, q.sender_name, q.occurred_at, q.message_id, q.message_text "
                        "FROM chat_memory_messages q JOIN chat_memory_batches b "
                        "ON b.session_id = q.session_id AND q.id BETWEEN b.first_row_id AND b.last_row_id "
                        "WHERE b.summary_id = ? AND b.session_id = ? ORDER BY q.id",
                        (memory["source_id"], memory["session_id"]),
                    ).fetchall()
            if not rows and memory["message_id"] and memory["session_id"]:
                rows = conn.execute(
                    "SELECT role, sender_name, occurred_at, message_id, message_text "
                    "FROM chat_memory_messages WHERE session_id = ? AND message_id = ? ORDER BY id",
                    (memory["session_id"], memory["message_id"]),
                ).fetchall()
            if not rows:
                return {"status": "source_unavailable", "memory_id": memory_id,
                        "message": "原始消息未留存，摘要不能代替原话。"}
            text = "\n".join(
                f"[{row['occurred_at']} / {row['role']} / {row['sender_name']} / "
                f"消息 {row['message_id']}]\n{row['message_text']}" for row in rows
            )
            end = min(len(text), offset + limit)
            return {"status": "ok", "memory_id": memory_id, "source": "chat_message",
                    "text": text[offset:end], "offset": offset,
                    "next_offset": end if end < len(text) else None, "total_chars": len(text)}
        return await self.context._run_db(read)

    async def consolidate_living_memories(self, *, today: str = "") -> dict[str, int]:
        """后台整理经历脉络，弱化过期琐事；不因重复召回增强事实。"""
        day = datetime.date.fromisoformat(today or life_now().date().isoformat())
        cutoff = (day - datetime.timedelta(days=30)).isoformat()

        def write():
            conn = self.context._conn
            # 有原始队列的既有摘要可补齐，已清理的原话不伪造恢复。
            batches = conn.execute(
                "SELECT b.* FROM chat_memory_batches b JOIN chat_summaries s ON s.id = b.summary_id "
                "WHERE b.summary_id > 0 AND NOT EXISTS (SELECT 1 FROM memory_source_messages ms "
                "WHERE ms.summary_id = b.summary_id) ORDER BY b.id DESC LIMIT 100"
            ).fetchall()
            for batch in batches:
                rows = conn.execute(
                    "SELECT * FROM chat_memory_messages WHERE session_id = ? AND id BETWEEN ? AND ?",
                    (batch["session_id"], batch["first_row_id"], batch["last_row_id"]),
                ).fetchall()
                self._save_sources(batch["summary_id"], [dict(row) for row in rows])
            clusters = conn.execute(
                "SELECT c.id, c.summary FROM memory_episode_clusters c WHERE c.status = 'active' "
                "AND c.memory_count > 1 ORDER BY c.last_date DESC, c.id DESC LIMIT 100"
            ).fetchall()
            consolidated = 0
            for cluster in clusters:
                rows = conn.execute(
                    "SELECT m.id, m.date, m.content FROM long_term_memories m "
                    "JOIN memory_episode_cluster_items i ON i.memory_id = m.id "
                    "WHERE i.cluster_id = ? AND m.status = 'active' "
                    "ORDER BY m.date DESC, m.id DESC LIMIT 6", (cluster["id"],),
                ).fetchall()
                # 摘取原记录而非生成新事实，原始内容与来源始终保留。
                summary = " → ".join(
                    f"{row['date']} #{row['id']} {row['content'][:120]}" for row in reversed(rows)
                )
                if len(rows) > 1 and summary != cluster["summary"]:
                    conn.execute("UPDATE memory_episode_clusters SET summary = ? WHERE id = ?",
                                 (summary, cluster["id"]))
                    consolidated += 1
            # 只收起已过期且低权重的短期事项。纠正、重要经历、有效事实和未完约定均保留。
            cursor = conn.execute(
                f"""
                UPDATE long_term_memories AS m SET status = 'dormant'
                WHERE m.id IN (
                    SELECT m.id FROM long_term_memories m
                    WHERE m.status = 'active' AND m.category IN ('short_term', 'focus')
                      AND m.weight < 2 AND m.expires_at <> '' AND m.expires_at < ?
                      AND {unprotected_memory_clause()}
                    ORDER BY m.id LIMIT 500
                )
                """, (cutoff,),
            )
            return {"consolidated": consolidated, "dormant": cursor.rowcount}
        return await self.context._run_db(self._transaction, write)


__all__ = ["RecollectionArchiveComponent"]
