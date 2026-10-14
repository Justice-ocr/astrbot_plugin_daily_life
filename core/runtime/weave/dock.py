from __future__ import annotations

from collections import deque

from .grain import StructuredMessage

from typing import Any, Protocol


class StructuredDockComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _STRUCTURED_CONTEXT_LIMIT: Any
    _structured_messages: Any
    _structured_scope_messages: Any
    _structured_sequence_counters: Any
    _structured_store: Any


class StructuredDockComponent:
    def __init__(self, context: StructuredDockComponentContext) -> None:
        self.context = context

    def remove_recalled_structured_message(self, scope: str, message_id: str) -> None:
        """仅移除所属会话的撤回消息，并保留上下文容量与排序。"""
        store = self.context._structured_store()
        messages = store.get(scope)
        if not messages:
            return
        store[scope] = deque(
            (
                item for item in messages
                if message_id not in {item.message_id, item.fallback_key, item.key}
            ),
            maxlen=messages.maxlen,
        )

    def _structured_store(self) -> dict[str, deque[StructuredMessage]]:
        store = getattr(self.context, "_structured_messages", None)
        if not isinstance(store, dict):
            store = {}
            self.context._structured_messages = store
        return store

    def _structured_scope_messages(self, scope: str) -> deque[StructuredMessage]:
        scope = str(scope or "").strip()
        store = self.context._structured_store()
        messages = store.get(scope)
        if not isinstance(messages, deque):
            messages = deque(maxlen=self.context._STRUCTURED_CONTEXT_LIMIT)
            store[scope] = messages
        return messages

    def _structured_upsert_message(
        self, message: StructuredMessage
    ) -> StructuredMessage:
        messages = self.context._structured_scope_messages(message.scope)
        key = message.key
        existing = next((item for item in messages if key and item.key == key), None)
        counters = getattr(self.context, "_structured_sequence_counters", None)
        if not isinstance(counters, dict):
            counters = {}
            self.context._structured_sequence_counters = counters
        current_counter = max(
            int(counters.get(message.scope, 0) or 0),
            max((int(getattr(item, "sequence", 0) or 0) for item in messages), default=0),
        )
        if existing is not None and int(getattr(existing, "sequence", 0) or 0) > 0:
            message.sequence = int(existing.sequence)
        elif int(getattr(message, "sequence", 0) or 0) <= 0:
            current_counter += 1
            message.sequence = current_counter
        counters[message.scope] = max(current_counter, int(message.sequence or 0))
        if key:
            kept = [item for item in messages if item.key != key]
            kept.append(message)
            self.context._structured_store()[message.scope] = deque(
                kept[-self.context._STRUCTURED_CONTEXT_LIMIT :],
                maxlen=self.context._STRUCTURED_CONTEXT_LIMIT,
            )
            return message
        messages.append(message)
        return message
