from __future__ import annotations

import hashlib
import json
from typing import Any

from ..models import LongTermMemoryRecord

KIND = "bili_browse"
STATE_KEY = "bili_browse_state"
STATES = frozenset(
    {
        "queued",
        "watching",
        "paused",
        "unfinished",
        "completed",
        "abandoned",
        "unavailable",
    }
)


def record_id(bvid: str, page: int = 1, scope: str = "global") -> str:
    return "bili:" + hashlib.sha256(f"{scope}:{bvid}:{page}".encode()).hexdigest()[:32]


class BrowseArchive:
    """复用生活经历表，原子保存浏览检查点和发送回执。

    revision 防止耗时理解覆盖中途暂停、用户操作或另一轮发送回执。
    浏览记录属于 experience 存储分类，沿用现有备份、清理和作用域规则。
    """

    def __init__(self, archive: Any):
        self.archive = archive

    async def get(self, identity: str) -> dict | None:
        def read():
            row = self.archive._conn.execute(
                "SELECT payload_json FROM continuous_life_entries WHERE id=? AND kind=?",
                (identity, KIND),
            ).fetchone()
            return json.loads(row[0]) if row else None

        return await self.archive._run_db(read)

    async def list(
        self,
        *,
        scope: str | None = None,
        limit: int = 80,
        offset: int = 0,
        status: str = "",
    ) -> list[dict]:
        def read():
            clauses, params = ["kind=?"], [KIND]
            if scope is not None:
                clauses.append("scope IN ('global',?)")
                params.append(scope)
            if status:
                clauses.append("json_extract(payload_json,'$.status')=?")
                params.append(status)
            params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])
            rows = self.archive._conn.execute(
                f"SELECT payload_json FROM continuous_life_entries WHERE {' AND '.join(clauses)} ORDER BY occurred_at DESC,id DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

        return await self.archive._run_db(read)

    async def save(self, record: dict, *, expected: int = 0) -> dict | None:
        def write():
            conn = self.archive._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT payload_json FROM continuous_life_entries WHERE id=? AND kind=?",
                    (record["id"], KIND),
                ).fetchone()
                previous = json.loads(row[0]) if row else {}
                if int(previous.get("revision", 0)) != expected:
                    conn.rollback()
                    return None
                value = {**record, "revision": expected + 1}
                if value.get("status") not in STATES:
                    raise ValueError("无效的浏览状态")
                stamp = value["updated_at"]
                conn.execute(
                    "INSERT INTO continuous_life_entries(id,kind,date,scope,occurred_at,summary,payload_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET occurred_at=excluded.occurred_at,summary=excluded.summary,payload_json=excluded.payload_json",
                    (
                        value["id"],
                        KIND,
                        value["created_at"][:10],
                        value["scope"],
                        stamp,
                        str(
                            value.get("impression")
                            or value.get("reason")
                            or value.get("title")
                            or ""
                        )[:500],
                        json.dumps(value, ensure_ascii=False, allow_nan=False),
                    ),
                )
                # 只有带有已处理片段证据的、有意义的感受才进入长期记忆。
                if (
                    value.get("remember")
                    and value.get("evidence")
                    and value.get("impression")
                ):
                    evidence = value["evidence"][-1]
                    content = (
                        f"{value['created_at']} 浏览《{value.get('title', '')}》；"
                        f"已理解到 {value.get('position', 0):.0f} 秒，状态 {value['status']}。"
                        f"片段依据：{evidence.get('quote', '')}。"
                        f"自己的感受（主观）：{value['impression']}。"
                        f"未解问题：{value.get('question', '')}。{value.get('url', '')}"
                    )
                    self.archive._upsert_long_term_memory_unlocked(
                        LongTermMemoryRecord(
                            scope=value["scope"],
                            category="autobiography",
                            title=f"视频浏览：{value.get('title', '')}"[:120],
                            content=content[:1000],
                            source_table="continuous_life_entries",
                            source_id=value["id"],
                            date=stamp[:10],
                            confidence=0.8,
                        )
                    )
                conn.commit()
                return value
            except BaseException:
                conn.rollback()
                raise

        return await self.archive._run_db(write)

    async def state(self) -> dict:
        def read():
            row = self.archive._conn.execute(
                "SELECT value FROM meta WHERE key=?", (STATE_KEY,)
            ).fetchone()
            return json.loads(row[0]) if row else {}

        return await self.archive._run_db(read)

    async def update_state(self, patch: dict) -> dict:
        def write():
            conn = self.archive._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key=?", (STATE_KEY,)
                ).fetchone()
                value = {**(json.loads(row[0]) if row else {}), **patch}
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (STATE_KEY, json.dumps(value, ensure_ascii=False, allow_nan=False)),
                )
                conn.commit()
                return value
            except BaseException:
                conn.rollback()
                raise

        return await self.archive._run_db(write)

    async def delivered(self, scope: str, identities: list[str], at: str) -> None:
        # 在同一事务中补回执并递增 revision，耗时理解不能覆盖已送达事实。
        def write():
            conn = self.archive._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                for identity in dict.fromkeys(identities[:20]):
                    row = conn.execute(
                        "SELECT payload_json FROM continuous_life_entries WHERE id=? AND kind=?",
                        (identity, KIND),
                    ).fetchone()
                    record = json.loads(row[0]) if row else None
                    if not record or record.get("scope") not in {"global", scope}:
                        continue
                    sent = dict(record.get("shared_to") or {})
                    if scope in sent:
                        continue
                    sent[scope] = at
                    record.update(
                        shared_to=sent, revision=int(record.get("revision", 0)) + 1
                    )
                    conn.execute(
                        "UPDATE continuous_life_entries SET payload_json=? WHERE id=? AND kind=?",
                        (
                            json.dumps(record, ensure_ascii=False, allow_nan=False),
                            identity,
                            KIND,
                        ),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

        await self.archive._run_db(write)
