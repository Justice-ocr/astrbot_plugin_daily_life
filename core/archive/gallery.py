from __future__ import annotations

import datetime
import json
import sqlite3

from ..clock import today as life_today
from ..models import ReversePromptRecord

from typing import Any, Protocol


class MediaArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _compose_reverse_prompt: Any
    _conn: Any
    _run_db: Any
    _text: Any


class MediaArchiveComponent:
    def __init__(self, context: MediaArchiveComponentContext) -> None:
        self.context = context

    def _compose_reverse_prompt(self, row: sqlite3.Row) -> ReversePromptRecord:
        try:
            keywords = json.loads(row["keywords"] or "[]")
        except (TypeError, json.JSONDecodeError):
            keywords = []
        try:
            analysis = json.loads(row["analysis_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            analysis = {}
        return ReversePromptRecord(
            id=int(row["id"] or 0),
            scope=row["scope"],
            prompt=row["prompt"],
            image_path=row["image_path"],
            title=row["title"],
            keywords=[str(item).strip() for item in keywords if str(item).strip()][:12]
            if isinstance(keywords, list)
            else [],
            ratio=row["ratio"],
            usage=row["usage"],
            profile=row["profile"],
            source_prompt=row["source_prompt"],
            analysis=analysis if isinstance(analysis, dict) else {},
            source_image_hash=row["source_image_hash"],
            created_at=row["created_at"],
        )

    async def save_reverse_prompt(
        self, record: ReversePromptRecord | dict
    ) -> ReversePromptRecord | None:
        item = ReversePromptRecord.from_value(record)
        if not item or not item.scope:
            return None

        def write() -> ReversePromptRecord | None:
            cursor = self.context._conn.execute(
                """
                INSERT INTO reverse_prompts(
                    scope, prompt, image_path, title, keywords, ratio, usage, profile,
                    source_prompt, analysis_json, source_image_hash, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(NULLIF(?, ''), CURRENT_TIMESTAMP))
                """,
                (
                    self.context._text(item.scope),
                    self.context._text(item.prompt),
                    self.context._text(item.image_path),
                    self.context._text(item.title),
                    json.dumps(item.keywords, ensure_ascii=False),
                    self.context._text(item.ratio),
                    self.context._text(item.usage),
                    self.context._text(item.profile),
                    self.context._text(item.source_prompt),
                    json.dumps(item.analysis, ensure_ascii=False),
                    self.context._text(item.source_image_hash),
                    self.context._text(item.created_at),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM reverse_prompts WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            return self.context._compose_reverse_prompt(row) if row else None

        return await self.context._run_db(write)

    async def get_latest_reverse_prompt(self, scope: str) -> ReversePromptRecord | None:
        scope = self.context._text(scope)
        if not scope:
            return None

        def read() -> ReversePromptRecord | None:
            row = self.context._conn.execute(
                "SELECT * FROM reverse_prompts WHERE scope = ? ORDER BY id DESC LIMIT 1",
                (scope,),
            ).fetchone()
            return self.context._compose_reverse_prompt(row) if row else None

        return await self.context._run_db(read)

    async def get_reverse_prompt_by_fingerprint(
        self,
        scope: str,
        source_image_hash: str,
        profile: str,
        source_prompt: str = "",
    ) -> ReversePromptRecord | None:
        values = tuple(
            self.context._text(value)
            for value in (scope, source_image_hash, profile, source_prompt)
        )
        if not values[0] or not values[1] or not values[2]:
            return None

        def read() -> ReversePromptRecord | None:
            row = self.context._conn.execute(
                """
                SELECT * FROM reverse_prompts
                WHERE scope = ? AND source_image_hash = ? AND profile = ?
                  AND source_prompt = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                values,
            ).fetchone()
            return self.context._compose_reverse_prompt(row) if row else None

        return await self.context._run_db(read)

    async def cleanup_reverse_prompts(self, keep_days: int) -> int:
        try:
            retention = max(int(keep_days), 0)
        except (TypeError, ValueError):
            retention = 0
        if retention <= 0:
            return 0
        cutoff = (life_today() - datetime.timedelta(days=retention)).strftime(
            "%Y-%m-%d"
        )

        def write() -> int:
            cursor = self.context._conn.execute(
                "DELETE FROM reverse_prompts WHERE created_at < ?",
                (cutoff,),
            )
            self.context._conn.commit()
            return max(int(cursor.rowcount or 0), 0)

        return await self.context._run_db(write)
