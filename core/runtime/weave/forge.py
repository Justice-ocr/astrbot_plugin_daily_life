from __future__ import annotations

import time
from typing import Any

from ...sources.platforms import parse_unified_origin
from ...sources.location import enrich_location_message
from ...sight.visual import IMAGE_SUMMARY_MAX_CHARS
from ..elapsed import RECEIVED_AT, SENT_AT, event_time
from .grain import StructuredMessage

from typing import Protocol


class StructuredForgeComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _STRUCTURED_EVENT_SEQUENCE_ATTR: Any
    _event_group_meta: Any
    _event_message_id: Any
    _event_message_items: Any
    _event_session_id: Any
    _proactive_is_self_message: Any
    _safe_event_call: Any
    _structured_content_with_media: Any
    _structured_extract_at_targets: Any
    _structured_extract_reply: Any
    _structured_find_message: Any
    _structured_resolve_talking_to: Any
    _structured_scope_messages: Any
    _structured_self_id: Any
    _structured_sender_card_from_event: Any
    _structured_text: Any
    _structured_upsert_message: Any


class StructuredForgeComponent:
    def __init__(self, context: StructuredForgeComponentContext) -> None:
        self.context = context

    def note_structured_incoming_message(self, event: Any) -> StructuredMessage | None:
        scope = self.context._event_session_id(event)
        if not scope or self.context._proactive_is_self_message(event):
            return None
        enrich_location_message(event)
        content = str(getattr(event, "message_str", "") or "").strip()
        items = self.context._event_message_items(event)
        if not content and not items:
            return None
        group_id, group_name = self.context._event_group_meta(event)
        sender_id = self.context._safe_event_call(event, "get_sender_id")
        sender_name = (
            self.context._safe_event_call(event, "get_sender_name") or sender_id or "对方"
        )
        message_id = self.context._event_message_id(event)
        reply = self.context._structured_extract_reply(event, scope)
        message = StructuredMessage(
            scope=scope,
            message_id=message_id,
            fallback_key=f"event:{id(event)}",
            sender_id=sender_id,
            sender_name=sender_name,
            sender_card=self.context._structured_sender_card_from_event(event),
            group_id=group_id,
            group_name=group_name,
            content=self.context._structured_content_with_media(content, items),
            timestamp=event_time(event, RECEIVED_AT) or time.time(),
            is_bot=False,
            reply_to_id=reply.get("message_id", ""),
            reply_to_sender_id=reply.get("sender_id", ""),
            reply_to_sender_name=reply.get("sender_name", ""),
            reply_to_content=reply.get("content", ""),
            at_targets=self.context._structured_extract_at_targets(event),
        )
        self.context._structured_resolve_talking_to(
            message, self.context._structured_self_id(event), source_event=event
        )
        message = self.context._structured_upsert_message(message)
        setattr(event, self.context._STRUCTURED_EVENT_SEQUENCE_ATTR, message.sequence)
        return message

    def note_structured_bot_message(
        self,
        scope: str,
        content: str,
        *,
        source_event: Any = None,
        media: str = "",
        reply_to_id: str = "",
    ) -> StructuredMessage | None:
        scope = str(scope or "").strip()
        content = str(content or "").strip()
        if not scope or not content:
            return None
        group_id = group_name = ""
        sender_id = sender_name = ""
        if source_event is not None:
            group_id, group_name = self.context._event_group_meta(source_event)
            sender_id = self.context._structured_self_id(source_event)
            sender_name = "我"
            reply_to_id = reply_to_id or self.context._event_message_id(source_event)
        elif "GroupMessage" in scope:
            _, group_id = parse_unified_origin(scope)
        target = self.context._structured_find_message(scope, reply_to_id)
        message = StructuredMessage(
            scope=scope,
            message_id=f"bot:{int(time.time() * 1000)}:{len(self.context._structured_scope_messages(scope))}",
            fallback_key=f"bot:{id(content)}",
            sender_id=sender_id or "bot",
            sender_name=sender_name or "我",
            sender_card="我",
            group_id=group_id,
            group_name=group_name,
            content=content,
            timestamp=event_time(source_event, SENT_AT) or time.time(),
            is_bot=True,
            media=media,
            reply_to_id=reply_to_id,
            reply_to_sender_id=target.sender_id if target else "",
            reply_to_sender_name=target.display_sender if target else "",
            reply_to_content=target.content if target else "",
        )
        self.context._structured_resolve_talking_to(message, sender_id)
        return self.context._structured_upsert_message(message)

    def update_structured_message_visual_summary(
        self,
        scope: str,
        message_key: str,
        summary: str,
    ) -> StructuredMessage | None:
        scope = str(scope or "").strip()
        message_key = str(message_key or "").strip()
        summary = self.context._structured_text(summary, IMAGE_SUMMARY_MAX_CHARS).strip(" ：:，,。")
        if not scope or not message_key or not summary:
            return None

        message = self.context._structured_find_message(scope, message_key)
        if not message:
            return None

        placeholder = "[图片]"
        visual = f"[图片：{summary}]"
        content = str(message.content or "").strip()
        if placeholder in content:
            message.content = content.replace(placeholder, visual, 1)
        elif "[图片：" not in content:
            message.content = f"{content} {visual}".strip()
        message.visual_summary = summary
        return message

    def update_structured_message_video_summary(
        self,
        scope: str,
        message_key: str,
        summary: str,
    ) -> StructuredMessage | None:
        scope = str(scope or "").strip()
        message_key = str(message_key or "").strip()
        summary = self.context._structured_text(summary, 120).strip(" ：:，,。")
        if not scope or not message_key or not summary:
            return None

        message = self.context._structured_find_message(scope, message_key)
        if not message:
            return None

        placeholder = "[视频]"
        visual = f"[视频：{summary}]"
        content = str(message.content or "").strip()
        if placeholder in content:
            message.content = content.replace(placeholder, visual, 1)
        elif "[视频：" not in content:
            message.content = f"{content} {visual}".strip()
        return message
