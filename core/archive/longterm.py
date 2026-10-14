from __future__ import annotations

import sqlite3
from typing import Any

from ..models import (
    LongTermMemoryRecord,
    MemoryConflictRecord,
    MemoryDecisionLinkRecord,
    MemoryEntityRecord,
    MemoryEpisodeClusterRecord,
)

from typing import Protocol


class LongTermMemoryArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _ENTITY_CATEGORY_MAP: Any
    _apply_memory_lifecycle_unlocked: Any
    _compose_long_term_memory: Any
    _compose_memory_cluster: Any
    _compose_memory_conflict: Any
    _compose_memory_decision_link: Any
    _compose_memory_entity: Any
    _conn: Any
    _decision_memory_query: Any
    _long_term_memory_scope_clause: Any
    _maintain_long_term_memory_quality_unlocked: Any
    _memory_cluster_title: Any
    _memory_entity_name: Any
    _memory_entity_type: Any
    _memory_lifecycle_kind: Any
    _memory_search_query: Any
    _memory_search_terms: Any
    _record_memory_conflicts_unlocked: Any
    _run_db: Any
    _search_long_term_memory_fts_unlocked: Any
    _search_long_term_memory_like_unlocked: Any
    _text: Any
    _upsert_long_term_memory_unlocked: Any
    _upsert_memory_cluster_unlocked: Any
    _upsert_memory_decision_link_unlocked: Any
    _upsert_memory_entity_unlocked: Any


class LongTermMemoryArchiveComponent:
    def __init__(self, context: LongTermMemoryArchiveComponentContext) -> None:
        self.context = context

    _ENTITY_CATEGORY_MAP = {
        "relationship": "person",
        "place": "place",
        "event": "event",
        "episode": "event",
        "chat_summary": "topic",
        "short_term": "constraint",
        "correction": "correction",
    }

    def _compose_long_term_memory(
        self, row: sqlite3.Row, score: float = 0.0
    ) -> LongTermMemoryRecord:
        return (
            LongTermMemoryRecord.from_value(
                {
                    "id": row["id"],
                    "scope": row["scope"],
                    "category": row["category"],
                    "title": row["title"],
                    "content": row["content"],
                    "source_table": row["source_table"],
                    "source_id": row["source_id"],
                    "session_id": row["session_id"],
                    "message_id": row["message_id"],
                    "date": row["date"],
                    "confidence": row["confidence"],
                    "weight": row["weight"],
                    "status": row["status"],
                    "expires_at": row["expires_at"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "score": score,
                }
            )
            or LongTermMemoryRecord()
        )

    def _compose_memory_cluster(self, row: sqlite3.Row) -> MemoryEpisodeClusterRecord:
        return (
            MemoryEpisodeClusterRecord.from_value(dict(row))
            or MemoryEpisodeClusterRecord()
        )

    def _compose_memory_entity(self, row: sqlite3.Row) -> MemoryEntityRecord:
        data = dict(row)
        data["aliases"] = [
            item for item in str(data.get("aliases") or "").split(",") if item
        ]
        return MemoryEntityRecord.from_value(data) or MemoryEntityRecord()

    def _compose_memory_conflict(self, row: sqlite3.Row) -> MemoryConflictRecord:
        return MemoryConflictRecord.from_value(dict(row)) or MemoryConflictRecord()

    def _compose_memory_decision_link(
        self, row: sqlite3.Row
    ) -> MemoryDecisionLinkRecord:
        return (
            MemoryDecisionLinkRecord.from_value(dict(row)) or MemoryDecisionLinkRecord()
        )

    @staticmethod
    def _memory_search_query(text: str) -> str:
        tokens = [
            token.strip().replace('"', " ").replace("'", " ")
            for token in str(text or "")
            .replace("，", " ")
            .replace("。", " ")
            .replace("；", " ")
            .replace("：", " ")
            .replace("、", " ")
            .replace("|", " ")
            .replace("｜", " ")
            .split()
            if token.strip()
        ]
        if not tokens:
            return ""
        return " OR ".join(f'"{token}"' for token in tokens[:12])

    @staticmethod
    def _memory_search_terms(text: str) -> list[str]:
        normalized = str(text or "")
        for separator in "，。；：、！？|｜,.;:!?()（）[]【】<>《》\n\r\t":
            normalized = normalized.replace(separator, " ")
        chunks = [item.strip() for item in normalized.split() if item.strip()]
        terms: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = value.strip()
            if value and value not in seen:
                seen.add(value)
                terms.append(value)

        for chunk in chunks:
            add(chunk)
            compact = "".join(character for character in chunk if not character.isspace())
            if len(compact) >= 4:
                for index in range(len(compact) - 1):
                    add(compact[index : index + 2])
                    if len(terms) >= 16:
                        break
            if len(terms) >= 16:
                break
        return terms[:16]

    def _upsert_long_term_memory_unlocked(
        self,
        memory: LongTermMemoryRecord,
        *,
        replacing_correction: bool = False,
    ) -> LongTermMemoryRecord | None:
        if not memory.content:
            return None
        # 来源已被用户纠正时，迟到的后台任务即使改写措辞也不能重新确立旧事实。
        # 再次纠正同一条记录只能由 correct_life_memory 的事务显式放行。
        source_table = self.context._text(memory.source_table)
        source_id = self.context._text(memory.source_id)
        if not replacing_correction and source_table and source_id:
            invalidated = self.context._conn.execute(
                """SELECT 1 FROM long_term_memories WHERE scope=? AND source_table=?
                AND source_id=? AND status='invalidated' LIMIT 1""",
                (memory.scope, source_table, source_id),
            ).fetchone()
            if invalidated:
                return None
        # 无来源标识的记录仍按原文阻止被纠正版本复活。
        invalidated = self.context._conn.execute(
            """SELECT 1 FROM long_term_memories WHERE scope=? AND source_table=? AND source_id=?
            AND content=? AND status='invalidated' LIMIT 1""",
            (memory.scope, memory.source_table, memory.source_id, memory.content),
        ).fetchone()
        if invalidated and not replacing_correction:
            return None
        self.context._conn.execute(
            """
            INSERT INTO long_term_memories(
                scope, category, title, content, source_table, source_id,
                session_id, message_id, date, confidence, weight, status, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, category, content, source_table, source_id) DO UPDATE SET
                title = COALESCE(NULLIF(excluded.title, ''), long_term_memories.title),
                session_id = COALESCE(NULLIF(excluded.session_id, ''), long_term_memories.session_id),
                message_id = COALESCE(NULLIF(excluded.message_id, ''), long_term_memories.message_id),
                date = COALESCE(NULLIF(excluded.date, ''), long_term_memories.date),
                confidence = excluded.confidence,
                weight = MAX(long_term_memories.weight, excluded.weight),
                status = excluded.status,
                expires_at = COALESCE(NULLIF(excluded.expires_at, ''), long_term_memories.expires_at),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                self.context._text(memory.scope),
                self.context._text(memory.category) or "general",
                self.context._text(memory.title),
                self.context._text(memory.content),
                self.context._text(memory.source_table),
                self.context._text(memory.source_id),
                self.context._text(memory.session_id),
                self.context._text(memory.message_id),
                self.context._text(memory.date),
                float(memory.confidence),
                float(memory.weight),
                self.context._text(memory.status) or "active",
                self.context._text(memory.expires_at),
            ),
        )
        # UPSERT 更新已有行时 lastrowid 仍可能指向上一次插入的其他记录。
        row = self.context._conn.execute(
            """
            SELECT * FROM long_term_memories
            WHERE scope = ? AND category = ? AND content = ?
              AND source_table = ? AND source_id = ?
            """,
            (
                self.context._text(memory.scope),
                self.context._text(memory.category) or "general",
                self.context._text(memory.content),
                self.context._text(memory.source_table),
                self.context._text(memory.source_id),
            ),
        ).fetchone()
        saved = self.context._compose_long_term_memory(row) if row else None
        if saved:
            self.context._maintain_long_term_memory_quality_unlocked(saved)
        return saved

    def _memory_lifecycle_kind(self, category: str) -> str:
        category = self.context._text(category).lower()
        if category == "short_term" or category.startswith("focus"):
            return "short_term"
        if category == "correction" or "correction" in category:
            return "correction"
        if category.startswith("preference") or category in {
            "relationship",
            "place",
            "fact",
        }:
            return "long_term"
        return "episode"

    def _apply_memory_lifecycle_unlocked(self, memory: LongTermMemoryRecord) -> None:
        kind = self.context._memory_lifecycle_kind(memory.category)
        weight = float(memory.weight or 1.0)
        expires_at = self.context._text(memory.expires_at)
        if kind == "short_term" and not expires_at:
            expires_at = self.context._conn.execute(
                "SELECT DATE('now', '+7 days', 'localtime') AS day"
            ).fetchone()["day"]
            weight = max(weight, 1.2)
        elif kind in {"long_term", "correction"}:
            weight = max(weight, 2.0 if kind == "correction" else 1.5)
        elif kind == "episode":
            weight = max(0.6, weight)
        self.context._conn.execute(
            """
            UPDATE long_term_memories
            SET weight = ?, expires_at = COALESCE(NULLIF(?, ''), expires_at),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (weight, expires_at, int(memory.id)),
        )
        memory.weight = weight
        if expires_at:
            memory.expires_at = expires_at

    def _memory_entity_name(self, memory: LongTermMemoryRecord) -> str:
        if memory.title:
            return memory.title[:80]
        content = (
            memory.content.replace("，", " ").replace("。", " ").replace("；", " ")
        )
        return (content.split() or [memory.category or "记忆"])[0][:80]

    def _memory_entity_type(self, memory: LongTermMemoryRecord) -> str:
        category = self.context._text(memory.category).lower()
        if category.startswith("preference"):
            return "preference"
        prefix = category.split(":", 1)[0]
        return self.context._ENTITY_CATEGORY_MAP.get(prefix, "topic")

    def _upsert_memory_entity_unlocked(
        self, memory: LongTermMemoryRecord
    ) -> MemoryEntityRecord | None:
        name = self.context._text(self.context._memory_entity_name(memory))
        if not name:
            return None
        entity_type = self.context._memory_entity_type(memory)
        row = self.context._conn.execute(
            """
            SELECT * FROM memory_entities
            WHERE scope = ? AND entity_type = ? AND name = ?
            """,
            (self.context._text(memory.scope), entity_type, name),
        ).fetchone()
        if row:
            self.context._conn.execute(
                """
                UPDATE memory_entities
                SET first_seen = CASE WHEN first_seen = '' OR (? <> '' AND ? < first_seen) THEN ? ELSE first_seen END,
                    last_seen = CASE WHEN ? <> '' AND ? > last_seen THEN ? ELSE last_seen END,
                    confidence = MAX(confidence, ?),
                    status = 'active',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    self.context._text(memory.date),
                    self.context._text(memory.date),
                    self.context._text(memory.date),
                    self.context._text(memory.date),
                    self.context._text(memory.date),
                    self.context._text(memory.date),
                    float(memory.confidence or 1.0),
                    int(row["id"]),
                ),
            )
            entity_id = int(row["id"])
        else:
            cursor = self.context._conn.execute(
                """
                INSERT INTO memory_entities(
                    scope, entity_type, name, first_seen, last_seen,
                    mention_count, confidence, status
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, 'active')
                """,
                (
                    self.context._text(memory.scope),
                    entity_type,
                    name,
                    self.context._text(memory.date),
                    self.context._text(memory.date),
                    float(memory.confidence or 1.0),
                ),
            )
            entity_id = int(cursor.lastrowid)
        self.context._conn.execute(
            """
            INSERT OR REPLACE INTO memory_entity_links(entity_id, memory_id, relation, weight)
            VALUES (?, ?, 'mentions', ?)
            """,
            (entity_id, int(memory.id), float(memory.weight or 1.0)),
        )
        self.context._conn.execute(
            """UPDATE memory_entities SET mention_count =
            (SELECT COUNT(*) FROM memory_entity_links WHERE entity_id = ?)
            WHERE id = ?""",
            (entity_id, entity_id),
        )
        row = self.context._conn.execute(
            "SELECT * FROM memory_entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self.context._compose_memory_entity(row) if row else None

    def _memory_cluster_title(self, memory: LongTermMemoryRecord) -> str:
        category = self.context._text(memory.category).split(":", 1)[0] or "general"
        if category in {"short_term", "correction"}:
            return memory.title or memory.content[:60]
        return f"{category}:{memory.title or memory.content[:40]}"

    def _upsert_memory_cluster_unlocked(
        self, memory: LongTermMemoryRecord
    ) -> MemoryEpisodeClusterRecord | None:
        if self.context._memory_lifecycle_kind(memory.category) not in {
            "episode",
            "short_term",
        }:
            return None
        title = self.context._memory_cluster_title(memory)[:120]
        summary = memory.content
        category = self.context._text(memory.category).split(":", 1)[0] or "life"
        # 同一来源的修订使用已有经历链，不依赖摘要标题是否恰好相同。
        row = None
        if memory.source_table and memory.source_id:
            row = self.context._conn.execute(
                """SELECT c.* FROM memory_episode_clusters c
                JOIN memory_episode_cluster_items i ON i.cluster_id = c.id
                JOIN long_term_memories m ON m.id = i.memory_id
                WHERE c.scope = ? AND c.category = ?
                  AND m.source_table = ? AND m.source_id = ?
                ORDER BY c.id LIMIT 1""",
                (memory.scope, category, memory.source_table, memory.source_id),
            ).fetchone()
        if row is None:
            row = self.context._conn.execute(
                """SELECT * FROM memory_episode_clusters
                WHERE scope = ? AND title = ? AND category = ?""",
                (self.context._text(memory.scope), title, category),
            ).fetchone()
        if row:
            cluster_id = int(row["id"])
            self.context._conn.execute(
                """
                UPDATE memory_episode_clusters
                SET summary = COALESCE(NULLIF(?, ''), summary),
                    first_date = CASE WHEN first_date = '' OR (? <> '' AND ? < first_date) THEN ? ELSE first_date END,
                    last_date = CASE WHEN ? <> '' AND ? > last_date THEN ? ELSE last_date END,
                    weight = MAX(weight, ?),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    summary,
                    memory.date,
                    memory.date,
                    memory.date,
                    memory.date,
                    memory.date,
                    memory.date,
                    float(memory.weight or 1.0),
                    cluster_id,
                ),
            )
        else:
            cursor = self.context._conn.execute(
                """
                INSERT INTO memory_episode_clusters(
                    scope, title, summary, category, first_date, last_date,
                    memory_count, weight, status
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, 'active')
                """,
                (
                    self.context._text(memory.scope),
                    title,
                    summary,
                    self.context._text(memory.category).split(":", 1)[0] or "life",
                    self.context._text(memory.date),
                    self.context._text(memory.date),
                    float(memory.weight or 1.0),
                ),
            )
            cluster_id = int(cursor.lastrowid)
        self.context._conn.execute(
            """
            INSERT OR IGNORE INTO memory_episode_cluster_items(cluster_id, memory_id, sort_order)
            VALUES (?, ?, COALESCE((SELECT MAX(sort_order) + 1 FROM memory_episode_cluster_items WHERE cluster_id = ?), 0))
            """,
            (cluster_id, int(memory.id), cluster_id),
        )
        self.context._conn.execute(
            """UPDATE memory_episode_clusters SET memory_count =
            (SELECT COUNT(*) FROM memory_episode_cluster_items WHERE cluster_id = ?),
            summary = COALESCE((SELECT m.content FROM memory_episode_cluster_items i
                JOIN long_term_memories m ON m.id = i.memory_id
                WHERE i.cluster_id = ? AND m.status = 'active'
                  AND (m.expires_at = '' OR m.expires_at >= DATE('now', 'localtime'))
                ORDER BY m.date DESC, m.id DESC LIMIT 1), '')
            WHERE id = ?""",
            (cluster_id, cluster_id, cluster_id),
        )
        row = self.context._conn.execute(
            "SELECT * FROM memory_episode_clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
        return self.context._compose_memory_cluster(row) if row else None

    def _record_memory_conflicts_unlocked(
        self, memory: LongTermMemoryRecord
    ) -> list[MemoryConflictRecord]:
        category = self.context._text(memory.category)
        if self.context._memory_lifecycle_kind(category) not in {"short_term", "correction"}:
            return []
        rows = self.context._conn.execute(
            """
            SELECT * FROM long_term_memories
            WHERE id <> ? AND status = 'active'
              AND scope = ?
              AND (category LIKE 'preference:%' OR category = 'relationship' OR category = 'place')
            ORDER BY weight DESC, id DESC
            LIMIT 8
            """,
            (int(memory.id), self.context._text(memory.scope)),
        ).fetchall()
        conflicts: list[MemoryConflictRecord] = []
        conflict_type = (
            "override" if self.context._memory_lifecycle_kind(category) == "correction"
            else "temporary_tension"
        )
        for row in rows:
            other = self.context._compose_long_term_memory(row)
            summary = f"{memory.content} / {other.content}"
            self.context._conn.execute(
                """
                INSERT OR IGNORE INTO memory_conflicts(
                    scope, memory_id, related_memory_id, conflict_type,
                    summary, resolution, status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    self.context._text(memory.scope),
                    int(memory.id),
                    int(other.id),
                    conflict_type,
                    summary[:300],
                    "优先按时间范围和来源强度判断，不直接覆盖长期记忆。",
                ),
            )
            conflict_row = self.context._conn.execute(
                """SELECT * FROM memory_conflicts
                WHERE scope = ? AND memory_id = ? AND related_memory_id = ? AND conflict_type = ?""",
                (self.context._text(memory.scope), int(memory.id), int(other.id), conflict_type),
            ).fetchone()
            if conflict_row:
                conflicts.append(self.context._compose_memory_conflict(conflict_row))
        return conflicts

    def _maintain_long_term_memory_quality_unlocked(
        self, memory: LongTermMemoryRecord
    ) -> None:
        self.context._apply_memory_lifecycle_unlocked(memory)
        self.context._upsert_memory_entity_unlocked(memory)
        self.context._upsert_memory_cluster_unlocked(memory)
        self.context._record_memory_conflicts_unlocked(memory)

    async def upsert_long_term_memory(
        self, memory: LongTermMemoryRecord | dict[str, Any]
    ) -> LongTermMemoryRecord | None:
        record = LongTermMemoryRecord.from_value(memory)
        if not record:
            return None

        def write() -> LongTermMemoryRecord | None:
            saved = self.context._upsert_long_term_memory_unlocked(record)
            self.context._conn.commit()
            return saved

        return await self.context._run_db(write)

    async def upsert_long_term_memories(
        self,
        memories: list[LongTermMemoryRecord | dict[str, Any]],
    ) -> list[LongTermMemoryRecord]:
        records = [
            record
            for item in memories
            if (record := LongTermMemoryRecord.from_value(item))
        ]
        if not records:
            return []

        def write() -> list[LongTermMemoryRecord]:
            saved = [
                item
                for record in records
                if (item := self.context._upsert_long_term_memory_unlocked(record))
            ]
            self.context._conn.commit()
            return saved

        return await self.context._run_db(write)

    def _long_term_memory_scope_clause(
        self, scopes: list[str], *, include_self: bool = False
    ) -> tuple[str, list[str]]:
        clean = [self.context._text(scope) for scope in scopes if self.context._text(scope)]
        if not clean:
            return "", []
        placeholders = ", ".join("?" for _ in clean)
        self_clause = (
            " OR (m.scope = 'global' AND m.category = 'autobiography'"
            " AND m.source_table = 'continuous_life_entries')"
            if include_self else ""
        )
        return f" AND (m.scope = '' OR m.scope IN ({placeholders}){self_clause})", clean

    def _search_long_term_memory_fts_unlocked(
        self,
        query: str,
        *,
        scopes: list[str],
        categories: list[str],
        limit: int,
    ) -> list[LongTermMemoryRecord]:
        match = self.context._memory_search_query(query)
        if not match:
            return []
        scope_clause, scope_params = self.context._long_term_memory_scope_clause(scopes, include_self=True)
        category_params = [self.context._text(item) for item in categories if self.context._text(item)]
        category_clause = ""
        if category_params:
            category_clause = (
                f" AND m.category IN ({', '.join('?' for _ in category_params)})"
            )
        sql = f"""
            SELECT m.*, bm25(long_term_memories_fts) AS bm25_score
            FROM long_term_memories_fts f
            JOIN long_term_memories m ON m.id = f.rowid
            WHERE long_term_memories_fts MATCH ?
              AND m.status = 'active'
              AND (m.expires_at = '' OR m.expires_at >= DATE('now', 'localtime'))
              {scope_clause}
              {category_clause}
            ORDER BY bm25_score ASC, m.weight DESC, m.updated_at DESC, m.id DESC
            LIMIT ?
        """
        rows = self.context._conn.execute(
            sql, [match, *scope_params, *category_params, max(1, int(limit))]
        ).fetchall()
        return [
            self.context._compose_long_term_memory(row, score=-float(row["bm25_score"] or 0.0))
            for row in rows
        ]

    def _search_long_term_memory_like_unlocked(
        self,
        query: str,
        *,
        scopes: list[str],
        categories: list[str],
        limit: int,
    ) -> list[LongTermMemoryRecord]:
        tokens = self.context._memory_search_terms(query)[:12]
        if not tokens:
            return []
        conditions = " OR ".join(
            "(m.title LIKE ? OR m.content LIKE ? OR m.category LIKE ?)" for _ in tokens
        )
        params: list[Any] = []
        for token in tokens:
            pattern = f"%{token}%"
            params.extend([pattern, pattern, pattern])
        scope_clause, scope_params = self.context._long_term_memory_scope_clause(scopes, include_self=True)
        category_params = [self.context._text(item) for item in categories if self.context._text(item)]
        category_clause = ""
        if category_params:
            category_clause = (
                f" AND m.category IN ({', '.join('?' for _ in category_params)})"
            )
        rows = self.context._conn.execute(
            f"""
            SELECT m.* FROM long_term_memories m
            WHERE m.status = 'active'
              AND (m.expires_at = '' OR m.expires_at >= DATE('now', 'localtime'))
              AND ({conditions})
              {scope_clause}
              {category_clause}
            ORDER BY m.weight DESC, m.updated_at DESC, m.id DESC
            LIMIT ?
            """,
            [*params, *scope_params, *category_params, max(1, int(limit))],
        ).fetchall()
        return [self.context._compose_long_term_memory(row, score=0.1) for row in rows]

    async def search_long_term_memories(
        self,
        query: str,
        *,
        scopes: list[str] | None = None,
        categories: list[str] | None = None,
        limit: int = 5,
    ) -> list[LongTermMemoryRecord]:
        query = self.context._text(query)
        if not query:
            return []

        def read() -> list[LongTermMemoryRecord]:
            try:
                hits = self.context._search_long_term_memory_fts_unlocked(
                    query,
                    scopes=scopes or [],
                    categories=categories or [],
                    limit=limit,
                )
                if hits:
                    return hits
            except sqlite3.Error:
                pass
            return self.context._search_long_term_memory_like_unlocked(
                query,
                scopes=scopes or [],
                categories=categories or [],
                limit=limit,
            )

        return await self.context._run_db(read)

    async def list_recent_long_term_memories(
        self,
        *,
        scopes: list[str] | None = None,
        categories: list[str] | None = None,
        limit: int = 8,
    ) -> list[LongTermMemoryRecord]:
        """列出有效记忆；limit=0 用于后台索引及全历史语义检索。"""
        def read() -> list[LongTermMemoryRecord]:
            scope_clause, scope_params = self.context._long_term_memory_scope_clause(
                scopes or [], include_self=True
            )
            category_params = [
                self.context._text(item) for item in (categories or []) if self.context._text(item)
            ]
            category_clause = ""
            if category_params:
                category_clause = (
                    f" AND category IN ({', '.join('?' for _ in category_params)})"
                )
            rows = self.context._conn.execute(
                f"""
                SELECT * FROM long_term_memories m
                WHERE status = 'active'
                  AND (expires_at = '' OR expires_at >= DATE('now', 'localtime'))
                  {scope_clause}
                  {category_clause}
                ORDER BY weight DESC, updated_at DESC, id DESC
                LIMIT ?
                """,
                [*scope_params, *category_params, int(limit) if limit > 0 else -1],
            ).fetchall()
            return [self.context._compose_long_term_memory(row) for row in rows]

        return await self.context._run_db(read)

    async def get_memory_episode_clusters(
        self,
        *,
        scopes: list[str] | None = None,
        limit: int = 8,
    ) -> list[MemoryEpisodeClusterRecord]:
        def read() -> list[MemoryEpisodeClusterRecord]:
            scope_clause, scope_params = self.context._long_term_memory_scope_clause(
                scopes or []
            )
            rows = self.context._conn.execute(
                f"""
                SELECT * FROM memory_episode_clusters m
                WHERE status = 'active'
                  {scope_clause}
                ORDER BY weight DESC, last_date DESC, updated_at DESC, id DESC
                LIMIT ?
                """,
                [*scope_params, max(1, int(limit))],
            ).fetchall()
            return [self.context._compose_memory_cluster(row) for row in rows]

        return await self.context._run_db(read)

    async def get_memory_entities(
        self,
        *,
        scopes: list[str] | None = None,
        entity_type: str = "",
        limit: int = 12,
    ) -> list[MemoryEntityRecord]:
        def read() -> list[MemoryEntityRecord]:
            scope_clause, scope_params = self.context._long_term_memory_scope_clause(
                scopes or []
            )
            params: list[Any] = [*scope_params]
            type_clause = ""
            clean_type = self.context._text(entity_type)
            if clean_type:
                type_clause = " AND entity_type = ?"
                params.append(clean_type)
            rows = self.context._conn.execute(
                f"""
                SELECT * FROM memory_entities m
                WHERE status = 'active'
                  {scope_clause}
                  {type_clause}
                ORDER BY mention_count DESC, confidence DESC, last_seen DESC, id DESC
                LIMIT ?
                """,
                [*params, max(1, int(limit))],
            ).fetchall()
            return [self.context._compose_memory_entity(row) for row in rows]

        return await self.context._run_db(read)

    async def get_memory_conflicts(
        self,
        *,
        scopes: list[str] | None = None,
        status: str = "open",
        limit: int = 8,
    ) -> list[MemoryConflictRecord]:
        def read() -> list[MemoryConflictRecord]:
            scope_clause, scope_params = self.context._long_term_memory_scope_clause(
                scopes or []
            )
            params: list[Any] = [*scope_params]
            status_clause = ""
            clean_status = self.context._text(status)
            if clean_status:
                status_clause = " AND status = ?"
                params.append(clean_status)
            rows = self.context._conn.execute(
                f"""
                SELECT * FROM memory_conflicts m
                WHERE 1 = 1
                  {scope_clause}
                  {status_clause}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                [*params, max(1, int(limit))],
            ).fetchall()
            return [self.context._compose_memory_conflict(row) for row in rows]

        return await self.context._run_db(read)

    def _upsert_memory_decision_link_unlocked(
        self,
        decision_id: int,
        memory_id: int,
        *,
        influence: str = "",
        weight: float = 1.0,
    ) -> MemoryDecisionLinkRecord | None:
        if not decision_id or not memory_id:
            return None
        self.context._conn.execute(
            """
            INSERT INTO memory_decision_links(decision_id, memory_id, influence, weight)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(decision_id, memory_id) DO UPDATE SET
                influence = COALESCE(NULLIF(excluded.influence, ''), memory_decision_links.influence),
                weight = MAX(memory_decision_links.weight, excluded.weight)
            """,
            (
                int(decision_id),
                int(memory_id),
                self.context._text(influence),
                max(0.0, min(float(weight or 1.0), 10.0)),
            ),
        )
        row = self.context._conn.execute(
            "SELECT * FROM memory_decision_links WHERE decision_id = ? AND memory_id = ?",
            (int(decision_id), int(memory_id)),
        ).fetchone()
        return self.context._compose_memory_decision_link(row) if row else None

    def _decision_memory_query(self, decision: Any) -> str:
        data = (
            decision.as_dict() if hasattr(decision, "as_dict") else dict(decision or {})
        )
        return " ".join(
            self.context._text(data.get(key))
            for key in ("decision", "reason", "evidence", "outcome", "subject")
            if self.context._text(data.get(key))
        )

    def _link_decision_memory_candidates_unlocked(
        self,
        decision: Any,
        *,
        limit: int = 3,
    ) -> list[MemoryDecisionLinkRecord]:
        data = (
            decision.as_dict() if hasattr(decision, "as_dict") else dict(decision or {})
        )
        decision_id = int(data.get("id") or 0)
        query = self.context._decision_memory_query(data)
        if not (decision_id and query):
            return []
        try:
            memories = self.context._search_long_term_memory_fts_unlocked(
                query,
                scopes=[],
                categories=[],
                limit=limit,
            )
        except sqlite3.Error:
            memories = []
        if not memories:
            memories = self.context._search_long_term_memory_like_unlocked(
                query,
                scopes=[],
                categories=[],
                limit=limit,
            )
        links: list[MemoryDecisionLinkRecord] = []
        for memory in memories[: max(1, int(limit))]:
            link = self.context._upsert_memory_decision_link_unlocked(
                decision_id,
                int(memory.id),
                influence=memory.title or memory.content[:80],
                weight=max(float(memory.weight or 1.0), float(memory.score or 0.0)),
            )
            if link:
                links.append(link)
        return links

    async def link_decision_memories(
        self,
        decision_id: int,
        memories: list[int | LongTermMemoryRecord | dict[str, Any]],
        *,
        influence: str = "",
        weight: float = 1.0,
    ) -> list[MemoryDecisionLinkRecord]:
        def write() -> list[MemoryDecisionLinkRecord]:
            links: list[MemoryDecisionLinkRecord] = []
            for memory in memories:
                if isinstance(memory, int):
                    memory_id = memory
                elif isinstance(memory, dict):
                    memory_id = int(memory.get("id") or 0)
                else:
                    memory_id = int(getattr(memory, "id", 0) or 0)
                link = self.context._upsert_memory_decision_link_unlocked(
                    int(decision_id),
                    memory_id,
                    influence=influence,
                    weight=weight,
                )
                if link:
                    links.append(link)
            self.context._conn.commit()
            return links

        return await self.context._run_db(write)

    async def get_memory_decision_links(
        self,
        decision_id: int,
        *,
        limit: int = 8,
    ) -> list[MemoryDecisionLinkRecord]:
        def read() -> list[MemoryDecisionLinkRecord]:
            rows = self.context._conn.execute(
                """
                SELECT * FROM memory_decision_links
                WHERE decision_id = ?
                ORDER BY weight DESC, id DESC
                LIMIT ?
                """,
                (int(decision_id), max(1, int(limit))),
            ).fetchall()
            return [self.context._compose_memory_decision_link(row) for row in rows]

        return await self.context._run_db(read)

    async def get_memory_decision_sources(
        self, decision_id: int, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        def read() -> list[dict[str, Any]]:
            rows = self.context._conn.execute(
                """
                SELECT l.*, m.scope, m.category, m.title, m.content, m.source_table, m.source_id, m.date
                FROM memory_decision_links l
                JOIN long_term_memories m ON m.id = l.memory_id
                WHERE l.decision_id = ? AND m.status = 'active'
                ORDER BY l.weight DESC, l.id DESC
                LIMIT ?
                """,
                (int(decision_id), max(1, int(limit))),
            ).fetchall()
            return [
                {
                    "id": int(row["id"] or 0),
                    "decision_id": int(row["decision_id"] or 0),
                    "memory_id": int(row["memory_id"] or 0),
                    "influence": self.context._text(row["influence"]),
                    "weight": float(row["weight"] or 0.0),
                    "scope": self.context._text(row["scope"]),
                    "category": self.context._text(row["category"]),
                    "title": self.context._text(row["title"]),
                    "content": self.context._text(row["content"]),
                    "source_table": self.context._text(row["source_table"]),
                    "source_id": self.context._text(row["source_id"]),
                    "date": self.context._text(row["date"]),
                    "created_at": self.context._text(row["created_at"]),
                }
                for row in rows
            ]

        return await self.context._run_db(read)

    async def cleanup_long_term_memories(self, cutoff_date: str) -> int:
        def write() -> int:
            deleted = 0
            for sql, params in (
                (
                    """
                    DELETE FROM long_term_memories
                    WHERE status <> 'active'
                       OR (expires_at <> '' AND expires_at < DATE('now', 'localtime'))
                       OR (date <> '' AND date < ? AND weight < 3)
                    """,
                    (cutoff_date,),
                ),
            ):
                cursor = self.context._conn.execute(sql, params)
                deleted += max(int(cursor.rowcount or 0), 0)
            self.context._conn.commit()
            return deleted

        return await self.context._run_db(write)
