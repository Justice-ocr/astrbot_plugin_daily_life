from __future__ import annotations

import datetime
from typing import Any

from typing import Protocol


class SnapshotChatComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _run_autonomous_life_check: Any
    contact_resolver: Any
    event_was_recalled: Any
    maybe_collect_emoji_assets_from_event: Any


class SnapshotChatComponent:
    def __init__(self, context: SnapshotChatComponentContext) -> None:
        self.context = context

    async def _refresh_state_for_chat_background(
        self,
        target_date_str: str,
        now: datetime.datetime,
        source_event: Any = None,
    ) -> None:
        if source_event is not None and self.context.event_was_recalled(
            source_event, log_skip=True
        ):
            return
        await self.context._run_autonomous_life_check(
            target_date_str,
            now,
            source="chat",
            detail="收到消息准备回复：按刷新间隔在后台检查实时状态。",
            status_reason="chat_state_refresh",
            respect_quiet_hours=False,
            update_weather=False,
            source_event=source_event,
        )

    async def _collect_emoji_context_background(
        self,
        event: Any,
        now: datetime.datetime,
    ) -> None:
        if self.context.event_was_recalled(event, log_skip=True):
            return
        sender_name = await self.context.contact_resolver.resolve_event_sender(event)
        if self.context.event_was_recalled(event, log_skip=True):
            return
        await self.context.maybe_collect_emoji_assets_from_event(
            event, now, sender_name=sender_name
        )
