from __future__ import annotations

import datetime
import html
from typing import Any

from ...clock import TIMEZONE
from ...sight.visual import IMAGE_SUMMARY_MAX_CHARS
from .grain import StructuredMessage

from typing import Protocol


class StructuredGlanceComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _event_session_id: Any
    _structured_result_text: Any
    _structured_scope_messages: Any
    _structured_text: Any
    note_structured_bot_message: Any


class StructuredGlanceComponent:
    def __init__(self, context: StructuredGlanceComponentContext) -> None:
        self.context = context

    def _structured_result_text(self, event: Any) -> tuple[str, str]:
        pending = str(
            getattr(event, "_daily_life_structured_pending_bot_text", "") or ""
        ).strip()
        pending_media = str(
            getattr(event, "_daily_life_structured_pending_bot_media", "") or ""
        ).strip()
        if pending:
            return pending, pending_media
        result = getattr(event, "get_result", lambda: None)()
        chain = getattr(result, "chain", None)
        if not isinstance(chain, list):
            return "", ""
        texts: list[str] = []
        media: list[str] = []
        for comp in chain:
            if isinstance(comp, str):
                text = comp
                kind = "text"
            elif isinstance(comp, dict):
                kind = str(comp.get("type") or "").lower()
                text = str(comp.get("text") or comp.get("content") or "")
            else:
                kind = comp.__class__.__name__.lower()
                text = str(
                    getattr(comp, "text", "") or getattr(comp, "content", "") or ""
                )
            text = text.strip()
            if text:
                texts.append(text)
            if "record" in kind or "voice" in kind:
                media.append("语音")
            elif "image" in kind:
                media.append("图片")
            elif "video" in kind:
                media.append("视频")
        if texts:
            return "\n".join(texts).strip(), ""
        if media:
            label = "、".join(dict.fromkeys(media))
            return f"[{label}]", label
        return "", ""

    def mark_structured_pending_bot_text(
        self, event: Any, text: str, media: str = ""
    ) -> None:
        if event is None:
            return
        setattr(
            event, "_daily_life_structured_pending_bot_text", str(text or "").strip()
        )
        setattr(
            event, "_daily_life_structured_pending_bot_media", str(media or "").strip()
        )

    def note_structured_sent_result(self, event: Any) -> StructuredMessage | None:
        if hasattr(event, "_has_send_oper") and not event._has_send_oper:
            return None
        scope = self.context._event_session_id(event)
        if not scope:
            return None
        text, media = self.context._structured_result_text(event)
        if not text:
            return None
        return self.context.note_structured_bot_message(
            scope, text, source_event=event, media=media
        )

    def format_structured_message_context(
        self, event_or_scope: Any, limit: int = 8
    ) -> str:
        scope = (
            event_or_scope
            if isinstance(event_or_scope, str)
            else self.context._event_session_id(event_or_scope)
        )
        scope = str(scope or "").strip()
        if not scope:
            return ""
        messages = list(self.context._structured_scope_messages(scope))[
            -max(1, int(limit or 8)) :
        ]
        if not messages:
            return ""
        lines = [
            "<structured_conversation>",
            "  <note>最近真实消息结构，优先用于判断群聊里谁在对谁说话；不是长期记忆。</note>",
        ]
        for item in messages:
            lines.append(self._format_structured_message_line(item))
        lines.append("</structured_conversation>")
        return "\n".join(lines)

    def _format_structured_message_line(self, item: StructuredMessage) -> str:
        attrs = {
            "id": item.message_id or item.fallback_key,
            "time": datetime.datetime.fromtimestamp(
                item.timestamp, TIMEZONE
            ).isoformat(timespec="seconds") if item.timestamp > 0 else "",
            "role": "assistant" if item.is_bot else "user",
            "user_id": item.sender_id,
            "user": item.display_sender,
            "group_card": item.sender_card,
            "talking_to": item.talking_to_name or item.talking_to_id,
            "reply_to": item.reply_to_id,
        }
        attr_text = " ".join(
            f'{key}="{html.escape(str(value), quote=True)}"'
            for key, value in attrs.items()
            if str(value or "").strip()
        )
        content = self.context._structured_text(
            item.content, IMAGE_SUMMARY_MAX_CHARS + 180 if item.visual_summary else 180
        )
        if item.media and item.media not in content:
            content = f"[{item.media}] {content}".strip()
        at_targets = "、".join(
            target.name or target.user_id for target in item.at_targets
        )
        quote = self.context._structured_text(item.reply_to_content, 100)
        extras = []
        if at_targets:
            extras.append(f"@{at_targets}")
        if quote:
            extras.append(f"引用：{quote}")
        body = content
        if extras:
            body = f"{body}（{'；'.join(extras)}）" if body else "；".join(extras)
        return f"  <message {attr_text}>{html.escape(body)}</message>"

    def structured_recent_history_messages(
        self, event_or_scope: Any, limit: int = 6
    ) -> list[dict[str, str]]:
        scope = (
            event_or_scope
            if isinstance(event_or_scope, str)
            else self.context._event_session_id(event_or_scope)
        )
        scope = str(scope or "").strip()
        if not scope:
            return []
        messages = list(self.context._structured_scope_messages(scope))[
            -max(1, int(limit or 6)) :
        ]
        return [
            self._structured_history_payload(item)
            for item in messages
            if item.content or item.media
        ]

    def _structured_history_payload(self, item: StructuredMessage) -> dict[str, str]:
        return {
            "role": "assistant" if item.is_bot else "user",
            "message_id": item.message_id,
            "content": str(item.content or "").strip(),
            "user_id": item.sender_id,
            "name": item.display_sender,
            "group_id": item.group_id,
            "group_name": item.group_name,
            "talking_to_id": item.talking_to_id,
            "talking_to_name": item.talking_to_name,
            "reply_to_id": item.reply_to_id,
            "reply_to_sender_id": item.reply_to_sender_id,
            "reply_to_sender_name": item.reply_to_sender_name,
            "reply_to_content": str(item.reply_to_content or "").strip(),
            "media": item.media,
            "timestamp": str(item.timestamp or ""),
        }
