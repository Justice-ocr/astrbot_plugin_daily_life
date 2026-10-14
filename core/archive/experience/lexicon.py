from __future__ import annotations

import sqlite3
from typing import Any

from ...models import LifeTermRecord, MemoryBoundaryRecord
from .lines import _pack_lines, _unpack_lines

from typing import Protocol


class LexiconArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _compose_life_term: Any
    _compose_memory_boundary: Any
    _conn: Any
    _flag: Any
    _run_db: Any
    _text: Any


class LexiconArchiveComponent:
    def __init__(self, context: LexiconArchiveComponentContext) -> None:
        self.context = context

    def _compose_life_term(self, row: sqlite3.Row) -> LifeTermRecord:
        return LifeTermRecord(
            id=int(row["id"] or 0),
            term=row["term"],
            meaning=self.context._text(row["meaning"]),
            scope=row["scope"],
            scene=self.context._text(row["scene"]),
            examples=_unpack_lines(row["examples"]),
            familiarity=int(row["familiarity"] or 0),
            source=row["source"],
            confidence=float(row["confidence"] or 0.0),
            last_seen=row["last_seen"],
            evidence=self.context._text(row["evidence"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def upsert_life_term(self, term: LifeTermRecord) -> LifeTermRecord | None:
        item = LifeTermRecord.from_value(
            term.as_dict() if isinstance(term, LifeTermRecord) else term
        )
        if not item:
            return None

        def dbwork():
            self.context._conn.execute(
                """
                INSERT INTO life_terms(
                    term, meaning, scope, scene, examples, familiarity, source,
                    confidence, last_seen, evidence, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(term, scope) DO UPDATE SET
                    meaning = COALESCE(NULLIF(excluded.meaning, ''), life_terms.meaning),
                    scene = COALESCE(NULLIF(excluded.scene, ''), life_terms.scene),
                    examples = COALESCE(NULLIF(excluded.examples, ''), life_terms.examples),
                    familiarity = MAX(life_terms.familiarity, excluded.familiarity),
                    source = excluded.source,
                    confidence = MAX(life_terms.confidence, excluded.confidence),
                    last_seen = excluded.last_seen,
                    evidence = COALESCE(NULLIF(excluded.evidence, ''), life_terms.evidence),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self.context._text(item.term),
                    self.context._text(item.meaning),
                    self.context._text(item.scope),
                    self.context._text(item.scene),
                    _pack_lines(item.examples),
                    max(0, min(int(item.familiarity or 0), 100)),
                    self.context._text(item.source) or "chat_memory",
                    max(0.0, min(float(item.confidence or 0.0), 1.0)),
                    self.context._text(item.last_seen),
                    self.context._text(item.evidence),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM life_terms WHERE term = ? AND scope = ?",
                (self.context._text(item.term), self.context._text(item.scope)),
            ).fetchone()
            return self.context._compose_life_term(row) if row else None

        return await self.context._run_db(dbwork)

    async def get_life_terms(
        self, limit: int = 20, *, scope: str = ""
    ) -> list[LifeTermRecord]:
        def dbwork():
            sql = "SELECT * FROM life_terms"
            params: list[Any] = []
            if scope:
                sql += " WHERE scope = ? OR scope = ''"
                params.append(self.context._text(scope))
            sql += (
                " ORDER BY last_seen DESC, confidence DESC, familiarity DESC, id DESC"
            )
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_life_term(row) for row in rows]

        return await self.context._run_db(dbwork)

    def _compose_memory_boundary(self, row: sqlite3.Row) -> MemoryBoundaryRecord:
        return MemoryBoundaryRecord(
            id=int(row["id"] or 0),
            source_scope=row["source_scope"],
            target_scope=row["target_scope"],
            policy=row["policy"],
            reason=row["reason"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def set_memory_boundary(
        self, boundary: MemoryBoundaryRecord
    ) -> MemoryBoundaryRecord | None:
        item = MemoryBoundaryRecord.from_value(
            boundary.as_dict()
            if isinstance(boundary, MemoryBoundaryRecord)
            else boundary
        )
        if not item:
            return None

        def dbwork():
            self.context._conn.execute(
                """
                INSERT INTO memory_boundaries(
                    source_scope, target_scope, policy, reason, enabled,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(source_scope, target_scope) DO UPDATE SET
                    policy = excluded.policy,
                    reason = excluded.reason,
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    self.context._text(item.source_scope),
                    self.context._text(item.target_scope),
                    self.context._text(item.policy) or "ask",
                    self.context._text(item.reason),
                    self.context._flag(item.enabled),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM memory_boundaries WHERE source_scope = ? AND target_scope = ?",
                (self.context._text(item.source_scope), self.context._text(item.target_scope)),
            ).fetchone()
            return self.context._compose_memory_boundary(row) if row else None

        return await self.context._run_db(dbwork)

    async def get_memory_boundaries(
        self, limit: int = 20, enabled_only: bool = True
    ) -> list[MemoryBoundaryRecord]:
        def dbwork():
            sql = "SELECT * FROM memory_boundaries WHERE LOWER(TRIM(source_scope)) <> LOWER(TRIM(target_scope))"
            params: list[Any] = []
            if enabled_only:
                sql += " AND enabled = 1"
            sql += " ORDER BY updated_at DESC, id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            return [self.context._compose_memory_boundary(row) for row in rows]

        return await self.context._run_db(dbwork)
