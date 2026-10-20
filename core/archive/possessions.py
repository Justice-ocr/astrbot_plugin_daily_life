"""保存有版本的共同活动、物品及显式纠正。"""
from __future__ import annotations


from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from ..clock import now as life_now
from ..models import LongTermMemoryRecord

from typing import Protocol


class LivingArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _conn: Any
    _life_entity_unlocked: Any
    _living_projection_unlocked: Any
    _run_db: Any
    _save_life_entity_unlocked: Any
    _upsert_long_term_memory_unlocked: Any
    life_entity_id: Any
    living_revision: Any


class LivingArchiveComponent:
    def __init__(self, context: LivingArchiveComponentContext) -> None:
        self.context = context

    def _living_projection_unlocked(self, item: dict[str, Any]) -> dict[str, Any]:
        """约定的最终状态优先于活动检查点，避免取消后仍显示可继续。"""
        if item.get("kind") != "shared_activity":
            return item
        row = self.context._conn.execute(
            "SELECT status FROM commitments WHERE id=?", (item.get("commitment_id"),)
        ).fetchone()
        status = row[0] if row else "missing"
        if status in {"done", "cancelled", "expired", "delivery_failed", "missing"}:
            state = "completed" if status == "done" else "cancelled"
            if item.get("state") != state:
                return {
                    **item,
                    "state": state,
                    "detail": "原约定已结束。"
                    if status == "done"
                    else "原约定已取消或失效，需要重新确认。",
                }
        return item

    def _life_entity_unlocked(self, entity_id: str) -> dict[str, Any] | None:
        row = self.context._conn.execute(
            "SELECT payload_json FROM continuous_life_entries WHERE id=? AND kind IN ('shared_activity','object','correction')",
            (entity_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def life_entity_id(kind: str, scope: str, source: str) -> str:
        return (
            f"{kind}:" + hashlib.sha256(f"{scope}:{source}".encode()).hexdigest()[:24]
        )

    def _save_life_entity_unlocked(
        self,
        item: dict[str, Any],
        *,
        evidence_id: str,
        evidence: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        if (
            item.get("kind") not in {"shared_activity", "object", "correction"}
            or not item.get("scope")
            or not evidence_id
        ):
            raise ValueError("生活记录缺少有效种类、归属或证据。")
        previous = self.context._life_entity_unlocked(item["id"])
        if previous and previous["scope"] != item["scope"]:
            raise ValueError("当前会话无权修改该生活记录。")
        if previous and evidence_id in previous.get("evidence_ids", []):
            return previous
        revision = int((previous or {}).get("revision", 0))
        if expected_revision is not None and expected_revision != revision:
            raise ValueError("生活记录已有更新，请读取最新版本后再操作。")
        at = life_now().replace(tzinfo=None).isoformat(timespec="seconds")
        result = copy.deepcopy(item)
        result.update(
            revision=revision + 1,
            at=at,
            created_at=(previous or {}).get("created_at") or at,
        )
        result["evidence_ids"] = list(
            dict.fromkeys([*(previous or {}).get("evidence_ids", []), evidence_id])
        )[-128:]
        result["history"] = [
            *(previous or {}).get("history", []),
            {
                "revision": revision + 1,
                "at": at,
                "evidence_id": evidence_id,
                "evidence": evidence[:1200],
                "before": {
                    key: (previous or {}).get(key)
                    for key in ("state", "location", "progress", "next_step", "content", "detail", "owner", "quantity", "unit", "available_at")
                },
            },
        ][-20:]
        date = (
            at[:10]
            if result.get("state") in {"completed", "cancelled", "archived"}
            else ""
        )
        self.context._conn.execute(
            """INSERT INTO continuous_life_entries(id,kind,date,scope,occurred_at,summary,payload_json)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            date=excluded.date,occurred_at=excluded.occurred_at,summary=excluded.summary,payload_json=excluded.payload_json""",
            (
                result["id"],
                result["kind"],
                date,
                result["scope"],
                at,
                result.get("title", ""),
                json.dumps(result, ensure_ascii=False, allow_nan=False),
            ),
        )
        return result

    async def get_living_entities(
        self, scope: str, *, kind: str = "", limit: int = 18
    ) -> list[dict[str, Any]]:
        def read():
            clauses = [
                "(scope=? OR (scope='global' AND kind IN ('object','correction')))"
            ]
            params: list[Any] = [scope]
            if kind:
                clauses.append("kind=?")
                params.append(kind)
            else:
                clauses.append("kind IN ('object','shared_activity','correction')")
            params.append(max(1, min(limit, 128)))
            rows = self.context._conn.execute(
                f"SELECT payload_json FROM continuous_life_entries WHERE {' AND '.join(clauses)} "
                "ORDER BY CASE kind WHEN 'correction' THEN 0 WHEN 'shared_activity' THEN 1 ELSE 2 END, "
                "CASE WHEN date='' THEN 0 ELSE 1 END, occurred_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
            return [
                self.context._living_projection_unlocked(json.loads(row[0])) for row in rows
            ]

        return await self.context._run_db(read)

    async def get_living_entity(
        self, entity_id: str, scope: str
    ) -> dict[str, Any] | None:
        def read():
            item = self.context._life_entity_unlocked(entity_id)
            return (
                self.context._living_projection_unlocked(item)
                if item and item["scope"] in {scope, "global"}
                else None
            )

        return await self.context._run_db(read)

    async def ensure_shared_activity(self, commitment: Any) -> dict[str, Any] | None:
        if (
            not commitment.id
            or not commitment.source_session
            or (commitment.owner != "共同" and commitment.source != "invite")
            or commitment.media_kind != "none"
            or commitment.status not in {"active", "scheduled"}
        ):
            return None

        def write():
            entity_id = self.context.life_entity_id(
                "shared_activity", commitment.source_session, str(commitment.id)
            )
            existing = self.context._life_entity_unlocked(entity_id)
            if existing:
                return existing
            result = self.context._save_life_entity_unlocked(
                {
                    "id": entity_id,
                    "kind": "shared_activity",
                    "scope": commitment.source_session,
                    "title": commitment.content,
                    "state": "planned",
                    "commitment_id": commitment.id,
                    "scheduled_date": commitment.trigger_date,
                    "scheduled_time": commitment.trigger_time,
                    "progress": "",
                    "detail": "双方已有约定，实际开始等待交流确认。",
                },
                evidence_id=f"commitment:{commitment.id}",
                evidence=commitment.source_message or commitment.content,
            )
            self.context._conn.commit()
            self.context.living_revision = getattr(self.context, "living_revision", 0) + 1
            return result

        return await self.context._run_db(write)

    async def update_shared_activity(
        self,
        entity_id: str,
        scope: str,
        *,
        state: str,
        detail: str,
        progress: str,
        evidence_id: str,
        evidence: str,
        revision: int,
    ) -> dict[str, Any]:
        from ..life.entities import activity_change

        def write():
            current = self.context._life_entity_unlocked(entity_id)
            if (
                not current
                or current["kind"] != "shared_activity"
                or current["scope"] != scope
            ):
                raise ValueError("未找到本会话的共同活动。")
            if evidence_id in current.get("evidence_ids", []):
                return current
            commitment = self.context._conn.execute(
                "SELECT status FROM commitments WHERE id=?", (current["commitment_id"],)
            ).fetchone()
            if not commitment or commitment[0] in {
                "done",
                "cancelled",
                "expired",
                "delivery_failed",
            }:
                raise ValueError("原约定已取消或失效，请重新确认活动。")
            item = activity_change(
                current, state=state, detail=detail, progress=progress
            )
            self.context._conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.context._save_life_entity_unlocked(
                    item,
                    evidence_id=evidence_id,
                    evidence=evidence,
                    expected_revision=revision,
                )
                if state in {"completed", "cancelled"}:
                    status = "done" if state == "completed" else "cancelled"
                    self.context._conn.execute(
                        "UPDATE commitments SET status=?,completed_at=? WHERE id=?",
                        (status, result["at"], current["commitment_id"]),
                    )
                    self.context._conn.execute(
                        "UPDATE conversation_action_items SET status=?,updated_at=CURRENT_TIMESTAMP WHERE commitment_id=?",
                        (status, current["commitment_id"]),
                    )
                self.context._conn.execute(
                    "UPDATE long_term_memories SET status='superseded' WHERE source_table='continuous_life_entries' AND source_id=? AND status='active'",
                    (entity_id,),
                )
                self.context._upsert_long_term_memory_unlocked(
                    LongTermMemoryRecord(
                        scope=scope,
                        category="shared_activity",
                        title=result["title"],
                        content=f"{result['title']}；状态={state}；已确认进度={result.get('progress') or '未知'}；{detail}",
                        source_table="continuous_life_entries",
                        source_id=entity_id,
                        session_id=scope,
                    )
                )
                self.context._conn.commit()
            except BaseException:
                self.context._conn.rollback()
                raise
            self.context.living_revision = getattr(self.context, "living_revision", 0) + 1
            return result

        return await self.context._run_db(write)

    async def update_life_object(
        self,
        scope: str,
        *,
        entity_id: str,
        title: str,
        state: str,
        location: str,
        progress: str,
        content: str,
        evidence_id: str,
        evidence: str,
        revision: int,
        owner: str = "",
        quantity: float | None = None,
        unit: str = "",
        available_at: str = "",
    ) -> dict[str, Any]:
        from ..life.entities import object_change
        from ..life.resources import resource_properties

        def write():
            existing = self.context._life_entity_unlocked(entity_id) if entity_id else None
            if entity_id and (
                not existing
                or existing["scope"] != scope
                or existing["kind"] != "object"
            ):
                raise ValueError("未找到本会话的物品或作品。")
            current = existing or {
                "id": self.context.life_entity_id(
                    "object", scope, f"{evidence_id}:{title.strip()}"
                ),
                "kind": "object",
                "scope": scope,
                "provenance": "本会话明确记录",
            }
            result = object_change(
                current,
                title=title,
                state=state,
                location=location,
                progress=progress,
                content=content,
            )
            # 对真实媒体和领域物品的引用不可由自然语言伪造或覆盖。
            result = resource_properties(result, owner=owner, quantity=quantity,
                                         unit=unit, available_at=available_at)
            if existing and existing.get("resource"):
                result["resource"] = existing["resource"]
            self.context._conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.context._save_life_entity_unlocked(
                    result,
                    evidence_id=evidence_id,
                    evidence=evidence,
                    expected_revision=revision,
                )
                self.context._conn.commit()
            except BaseException:
                self.context._conn.rollback()
                raise
            self.context.living_revision = getattr(self.context, "living_revision", 0) + 1
            return result

        return await self.context._run_db(write)

    def _archive_life_artifact_unlocked(self, outcome: dict[str, Any]) -> None:
        artifact = outcome.get("artifact") or {}
        if outcome.get("status") != "committed" or not artifact.get("content"):
            return
        action_id = outcome["action_id"]
        entity_id = self.context.life_entity_id("object", "global", f"artifact:{action_id}")
        if self.context._life_entity_unlocked(entity_id):
            return
        parent = outcome.get("artifact_parent") or {}
        parent_item = self.context._life_entity_unlocked(parent.get("id", ""))
        if parent_item and parent_item.get("artifact_version"):
            series_id = parent_item["series_id"]
            row = self.context._conn.execute("SELECT MAX(json_extract(payload_json,'$.artifact_version')) FROM continuous_life_entries WHERE kind='object' AND scope='global' AND json_extract(payload_json,'$.series_id')=?", (series_id,)).fetchone()
            version = int(row[0] or 0) + 1
        else:
            series_id, version, parent_item = entity_id, 1, None
        self.context._save_life_entity_unlocked(
            {
                "id": entity_id,
                "kind": "object",
                "scope": "global",
                "state": "available",
                "title": outcome.get("target") or "数字笔记",
                "content": artifact["content"],
                "progress": f"已保存第 {version} 版",
                "next_step": outcome.get("next_step") or "",
                "location": "数字作品档案",
                "provenance": "行动后实际生成的数字笔记",
                "resource": {"action_id": action_id},
                "owner": "self",
                "series_id": series_id,
                "artifact_version": version,
                "parent_id": parent_item["id"] if parent_item else "",
                "improvement": (outcome.get("reflection") or {}).get("summary", ""),
            },
            evidence_id=f"artifact:{action_id}",
            evidence=artifact["content"],
        )

    async def get_artifact_versions(self, entity_id: str, scope: str) -> list[dict]:
        def read():
            item = self.context._life_entity_unlocked(entity_id)
            if not item or item["scope"] not in {scope, "global"} or not item.get("series_id"):
                return []
            rows = self.context._conn.execute(
                "SELECT payload_json FROM continuous_life_entries WHERE kind='object' AND scope=? AND json_extract(payload_json,'$.series_id')=? ORDER BY json_extract(payload_json,'$.artifact_version') DESC LIMIT 40",
                (item["scope"], item["series_id"]),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        return await self.context._run_db(read)

    async def correct_life_memory(
        self,
        scope: str,
        memory_id: int,
        content: str,
        *,
        evidence_id: str,
        evidence: str,
        allow_global: bool = False,
        expected_content: str | None = None,
    ) -> dict[str, Any]:
        if not content.strip() or not evidence.strip():
            raise ValueError("纠正需要明确内容和原消息证据。")

        def write():
            row = self.context._conn.execute(
                "SELECT * FROM long_term_memories WHERE id=?", (memory_id,)
            ).fetchone()
            if not row or (
                row["scope"] != scope
                and not (allow_global and row["scope"] == "global")
            ):
                raise ValueError("未找到本会话可修改的记忆。")
            target_scope = row["scope"]
            parent = (
                self.context._life_entity_unlocked(row["source_id"])
                if row["source_table"] == "continuous_life_entries"
                else None
            )
            entity_id = (
                parent["id"]
                if parent and parent.get("kind") == "correction"
                else self.context.life_entity_id(
                    "correction",
                    target_scope,
                    f"{row['source_table']}:{row['source_id']}:{memory_id}",
                )
            )
            current = self.context._life_entity_unlocked(entity_id)
            if current and evidence_id in current.get("evidence_ids", []):
                return current
            if row["status"] != "active" or (
                expected_content is not None and row["content"] != expected_content
            ):
                raise ValueError("记忆已有新的版本，请重新检索后再纠正。")
            self.context._conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.context._save_life_entity_unlocked(
                    {
                        **(current or {}),
                        "id": entity_id,
                        "kind": "correction",
                        "scope": target_scope,
                        "title": row["title"],
                        "content": content.strip()[:2000],
                        "state": "active",
                        "memory_id": (current or {}).get("memory_id", memory_id),
                        "source_table": (current or {}).get(
                            "source_table", row["source_table"]
                        ),
                        "source_id": (current or {}).get("source_id", row["source_id"]),
                        "original_content": (current or {}).get(
                            "original_content", row["content"]
                        ),
                    },
                    evidence_id=evidence_id,
                    evidence=evidence,
                )
                if result["source_table"] and result["source_id"]:
                    self.context._conn.execute(
                        "UPDATE long_term_memories SET status='invalidated',updated_at=CURRENT_TIMESTAMP WHERE scope=? AND source_table=? AND source_id=?",
                        (target_scope, result["source_table"], result["source_id"]),
                    )
                else:
                    self.context._conn.execute(
                        "UPDATE long_term_memories SET status='invalidated',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (result["memory_id"],),
                    )
                self.context._conn.execute(
                    "UPDATE long_term_memories SET status='superseded' WHERE source_table='continuous_life_entries' AND source_id=? AND status='active'",
                    (entity_id,),
                )
                # 纠正只替换当前事实解释，原始时间线、历史造型及媒体文件保持历史证据。
                self.context._upsert_long_term_memory_unlocked(
                    LongTermMemoryRecord(
                        scope=target_scope,
                        category="correction",
                        title=row["title"],
                        content=content.strip()[:2000],
                        source_table="continuous_life_entries",
                        source_id=entity_id,
                        session_id=scope,
                        message_id=evidence_id,
                        weight=2.0,
                    ),
                    replacing_correction=True,
                )
                self.context._conn.commit()
            except BaseException:
                self.context._conn.rollback()
                raise
            self.context.living_revision = getattr(self.context, "living_revision", 0) + 1
            return result

        return await self.context._run_db(write)

    async def record_share_media_object(
        self, task_id: int, *, delivered_at: float = 0
    ) -> dict[str, Any] | None:
        """分享成品在原目标名下归档，成功投递有独立事实与证据。"""
        from ..clock import TIMEZONE
        from datetime import datetime

        def write():
            row = self.context._conn.execute(
                "SELECT * FROM durable_tasks WHERE id=? AND status='completed' "
                "AND kind IN ('share_image_generation','share_video_generation')",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            media_result = json.loads(row["result_json"])
            scope = payload.get("scope")
            kind = "image" if row["kind"] == "share_image_generation" else "video"
            artifact = media_result.get("artifact_path" if kind == "image" else "artifact_url")
            if payload.get("external_owner") != "daily_share" or not scope or not artifact:
                return None
            entity_id = self.context.life_entity_id("object", scope, f"share-media:{task_id}")
            previous = self.context._life_entity_unlocked(entity_id)
            if previous and (not delivered_at or previous.get("delivery") == "sent"):
                return previous
            item = previous or {
                "id": entity_id, "kind": "object", "scope": scope,
                "state": "available", "title": "分享生成的视频" if kind == "video" else "分享生成的图片",
                "location": "媒体档案", "provenance": "已取得真实分享成品",
                "delivery": "not_confirmed", "delivered_at": "",
                "resource": {"generation_task_id": task_id, "media_kind": kind, "artifacts": [artifact]},
            }
            if delivered_at:
                item = {**item, "delivery": "sent", "delivered_at": datetime.fromtimestamp(delivered_at, TIMEZONE).isoformat(timespec="seconds")}
            if scope == "qzone_broadcast":
                item = {**item, "visibility": "public"}
            result = self.context._save_life_entity_unlocked(
                item,
                evidence_id=f"share-media:{task_id}:" + ("sent" if delivered_at else "ready"),
                evidence="分享媒体已确认发送" if delivered_at else "已取得分享成品，发送尚未确认",
            )
            self.context._conn.commit()
            self.context.living_revision = getattr(self.context, "living_revision", 0) + 1
            return result

        return await self.context._run_db(write)

    async def record_media_object(self, task_id: int) -> dict[str, Any] | None:
        """只从已登记的真实媒体产物建立物品，计划和用户填写的路径不能入库。"""

        def write():
            row = self.context._conn.execute(
                "SELECT * FROM durable_tasks WHERE id=? AND kind='media_delivery'",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            payload = json.loads(row["payload_json"])
            scope, artifacts = payload.get("scope"), payload.get("artifacts")
            if not scope or not isinstance(artifacts, list) or not artifacts:
                return None
            entity_id = self.context.life_entity_id("object", scope, f"media:{task_id}")
            previous = self.context._life_entity_unlocked(entity_id)
            if previous:
                return previous
            result = self.context._save_life_entity_unlocked(
                {
                    "id": entity_id,
                    "kind": "object",
                    "scope": scope,
                    "state": "available",
                    "title": payload.get("evidence")
                    or (
                        "已生成视频"
                        if payload.get("media_kind") == "video"
                        else "已生成照片"
                    ),
                    "location": "媒体档案",
                    "provenance": "已取得媒体成品，投递状态另行记录",
                    "resource": {
                        "delivery_task_id": task_id,
                        "media_kind": payload.get("media_kind"),
                        "artifacts": artifacts,
                        "source_message_id": payload.get("source_message_id"),
                    },
                    "progress": "",
                    "content": "",
                },
                evidence_id=f"media:{task_id}",
                evidence="已登记可投递的生成成品",
            )
            self.context._conn.commit()
            self.context.living_revision = getattr(self.context, "living_revision", 0) + 1
            return result

        return await self.context._run_db(write)
