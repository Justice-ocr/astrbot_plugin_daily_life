from __future__ import annotations

import datetime
import math
import time
from typing import Any

from astrbot.api import logger

from ..clock import TIMEZONE

RECEIVED_AT = "_daily_life_received_at"
SENT_AT = "_daily_life_sent_at"
TURN_RECEIVED_AT = "_daily_life_turn_received_at"


def event_time(event: Any, attribute: str) -> float:
    value = getattr(event, attribute, None)
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return 0.0


def local_message_time(event: Any, attribute: str) -> datetime.datetime | None:
    stamp = event_time(event, attribute)
    return (
        datetime.datetime.fromtimestamp(stamp, TIMEZONE).replace(tzinfo=None)
        if stamp else None
    )


def format_elapsed(seconds: float) -> str:
    remaining = max(0, int(seconds))
    parts = []
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60), ("秒", 1)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts) or "不到1秒"


class ConversationTimingMixin:
    """使用收发回执感知对话间隔，不依赖记忆提炼或模型推测时间。"""

    def note_conversation_incoming(self, event: Any) -> None:
        if (
            event is None
            or getattr(event, "is_proactive_synthetic", False)
            or self._proactive_is_self_message(event)
            or event_time(event, RECEIVED_AT)
        ):
            return
        now = time.time()
        created = event_time(event, "created_at")
        # AstrBot 在事件构造时记录 created_at，优先使用它排除前置钩子的等待。
        setattr(event, RECEIVED_AT, created if 0 < created <= now else now)

    def clear_conversation_timing(self) -> None:
        self._conversation_receipts = {}

    def note_conversation_sent(
        self, scope: str, sent_at: float, source_event: Any = None
    ) -> None:
        if not scope or not math.isfinite(sent_at) or sent_at <= 0:
            return
        store = getattr(self, "_conversation_receipts", None)
        if not isinstance(store, dict):
            store = self._conversation_receipts = {}
        # 保留少量回执，使生成期间的新发送不会覆盖本轮到达前的时间锚点。
        store[scope] = sorted(set(store.pop(scope, []) + [sent_at]))[-32:]
        while len(store) > 512:
            store.pop(next(iter(store)))
        if source_event is not None:
            setattr(source_event, SENT_AT, max(sent_at, event_time(source_event, SENT_AT)))

    async def record_conversation_delivery(
        self, scope: str, sent_at: float, source_event: Any = None
    ) -> None:
        self.note_conversation_sent(scope, sent_at, source_event)
        writer = getattr(getattr(self, "archive", None), "save_conversation_receipt", None)
        if not scope or not callable(writer):
            return
        try:
            await writer(scope, sent_at)
        except Exception:
            # 收发事实记录失败不能把已经成功发送的消息变成投递失败。
            logger.warning("[对话时间] 发送时间持久化失败，本次继续使用内存回执。")

    async def build_conversation_timing_context(self, event: Any) -> str:
        if event is None or getattr(event, "is_proactive_synthetic", False):
            return ""
        received = event_time(event, RECEIVED_AT)
        if not received:
            return ""
        cached = getattr(event, "_daily_life_conversation_timing_context", None)
        if isinstance(cached, str):
            return cached
        scope = self._event_session_id(event)
        if not scope:
            return ""
        started = event_time(event, TURN_RECEIVED_AT) or received
        started = min(started, received)
        store = getattr(self, "_conversation_receipts", {})
        previous = max((t for t in store.get(scope, []) if t <= started), default=0.0)
        reader = getattr(getattr(self, "archive", None), "get_conversation_receipt_before", None)
        if not previous and callable(reader):
            try:
                previous = await reader(scope, started)
            except Exception:
                logger.warning("[对话时间] 暂时无法读取历史发送时间，不推测对话间隔。")
        def stamp(value: float) -> str:
            return datetime.datetime.fromtimestamp(value, TIMEZONE).isoformat(timespec="seconds")

        lines = ["\n\n[ConversationTiming]", f"本条消息到达：{stamp(received)}"]
        if started != received:
            lines.append(f"本轮连续消息开始到达：{stamp(started)}")
        if previous and 0 < previous <= started:
            lines.extend((
                f"本轮到达前，本会话上次成功发送：{stamp(previous)}",
                f"到本轮开始已过：{format_elapsed(started - previous)}（{int(started - previous)}秒）",
            ))
        else:
            lines.append("上次成功发送时间：无可核实记录，间隔未知")
        if self._safe_event_call(event, "get_group_id"):
            lines.append("范围：群聊消息记录，不能把群内发送间隔归因于当前发言者。")
        context = "\n".join(lines)
        setattr(event, "_daily_life_conversation_timing_context", context)
        return context
