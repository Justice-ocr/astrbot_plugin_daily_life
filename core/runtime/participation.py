"""生活连续性工具入口；只读写已确认状态，不在普通聊天追加模型调用。"""

from __future__ import annotations

import json
from typing import Any

from ..life.entities import living_context


class LivingRuntimeMixin:
    def _living_evidence(self, event: Any, evidence: str) -> tuple[str, str]:
        scope = self._event_session_id(event)
        message_id = self._event_message_id(event)
        getter = getattr(event, "get_message_str", None)
        text = str(
            getter() if callable(getter) else getattr(event, "message_str", "")
        ).strip()
        if not scope or not message_id or not text or text != evidence.strip():
            raise ValueError(
                "请引用本轮用户完整原话作为证据，不能根据时间或推测更新经历。"
            )
        if self.event_was_recalled(event):
            raise ValueError("原消息已撤回，不再更新。")
        return scope, f"message:{scope}:{message_id}"

    async def living_context_for_scope(self, scope: str) -> str:
        reader = getattr(self.archive, "get_living_entities", None)
        return living_context(await reader(scope)) if callable(reader) else ""

    async def manage_shared_activity(
        self,
        event: Any,
        *,
        action: str,
        activity_id: str = "",
        detail: str = "",
        progress: str = "",
        evidence: str = "",
        revision: int = 0,
    ) -> str:
        scope = self._event_session_id(event)
        if action == "list":
            return json.dumps(
                await self.archive.get_living_entities(scope, kind="shared_activity"),
                ensure_ascii=False,
            )
        states = {
            "prepare": "preparing",
            "start": "active",
            "pause": "paused",
            "resume": "active",
            "finish": "completed",
            "cancel": "cancelled",
            "progress": "",
        }
        if action not in states:
            return "共同活动操作无效。"
        try:
            scope, source = self._living_evidence(event, evidence)
            current = await self.archive.get_living_entity(activity_id, scope)
            if not current or current["scope"] != scope:
                return "未找到本会话已经确认的共同活动；新邀约请先使用 accept_user_invite。"
            if action == "resume" and current["state"] != "paused":
                return "活动当前没有暂停，请先读取最新状态。"
            if action == "progress" and current["state"] != "active":
                return "共同活动尚未进行，不能记录进行进度。"
            result = await self.archive.update_shared_activity(
                activity_id,
                scope,
                state=states[action] or current["state"],
                detail=detail,
                progress=progress,
                evidence_id=source,
                evidence=evidence,
                revision=revision,
            )
            await self.mark_page_status_changed("continuous_life")
            return json.dumps(result, ensure_ascii=False)
        except ValueError as exc:
            return str(exc)

    async def manage_life_object(
        self,
        event: Any,
        *,
        action: str = "list",
        object_id: str = "",
        title: str = "",
        state: str = "",
        location: str = "",
        progress: str = "",
        content: str = "",
        evidence: str = "",
        revision: int = 0,
    ) -> str:
        scope = self._event_session_id(event)
        if action == "list":
            items = await self.archive.get_living_entities(
                scope, kind="object", limit=30
            )
            return json.dumps(items, ensure_ascii=False)
        if action not in {"record", "update"}:
            return "物品操作无效。"
        if action == "update" and not object_id:
            return "更新需要已有物品编号。"
        try:
            scope, source = self._living_evidence(event, evidence)
            result = await self.archive.update_life_object(
                scope,
                entity_id=object_id,
                title=title,
                state=state,
                location=location,
                progress=progress,
                content=content,
                evidence_id=source,
                evidence=evidence,
                revision=revision,
            )
            await self.mark_page_status_changed("continuous_life")
            return json.dumps(result, ensure_ascii=False)
        except ValueError as exc:
            return str(exc)

    async def correct_memory_from_event(
        self, event: Any, *, memory_id: int, correction: str, evidence: str
    ) -> str:
        try:
            scope, source = self._living_evidence(event, evidence)
            admin = getattr(event, "is_admin", None)
            result = await self.archive.correct_life_memory(
                scope,
                memory_id,
                correction,
                evidence_id=source,
                evidence=evidence,
                allow_global=bool(admin()) if callable(admin) else False,
            )
            await self.mark_page_status_changed("chat_memory")
            return json.dumps(result, ensure_ascii=False)
        except ValueError as exc:
            return str(exc)

    async def note_memory_corrections(self, corrections: list, batch: dict) -> None:
        rows = {
            str(row.get("message_id") or row.get("id")): row
            for row in batch.get("messages", [])
            if row.get("role") == "user" and not row.get("is_quoted")
        }
        allowed = {
            row["id"]: row["content"] for row in batch.get("correctable_memories", [])
        }
        for correction in corrections[:4]:
            if not isinstance(correction, dict):
                continue
            row = rows.get(str(correction.get("source_message_id") or ""))
            if (
                not row
                or correction.get("memory_id") not in allowed
                or correction.get("evidence") != row.get("message_text")
                or not str(correction.get("correction") or "").strip()
            ):
                continue
            recalled = getattr(self, "_message_was_recalled", None)
            if callable(recalled) and recalled(
                batch["session_id"], str(row.get("message_id") or row["id"])
            ):
                continue
            try:
                await self.archive.correct_life_memory(
                    batch["session_id"],
                    correction["memory_id"],
                    correction["correction"],
                    evidence_id=f"message:{batch['session_id']}:{row.get('message_id') or row['id']}",
                    evidence=correction["evidence"],
                    expected_content=allowed[correction["memory_id"]],
                )
            except ValueError:
                continue
