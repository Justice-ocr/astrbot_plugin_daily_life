from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from astrbot.api import logger

from .markers import LOG_PREFIX


class RecallService:
    """管理撤回记录与发送拦截，通过明确回调通知上下文所有者。"""

    _RECALL_NOTICE_TYPES = {"friend_recall", "group_recall"}
    _RECALL_TTL_SECONDS = 30 * 60

    def __init__(
        self,
        *,
        event_sources: Callable[[Any], Iterable[Any]],
        session_id: Callable[[Any], str],
        message_id: Callable[[Any], str],
        remove_context: tuple[Callable[[str, str], None], ...],
        send_scope: Callable[..., Awaitable[bool]],
        log_outbound: Callable[..., Awaitable[bool]],
        log_limit: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._event_sources = event_sources
        self._event_session_id = session_id
        self._event_message_id = message_id
        self._remove_context = remove_context
        self._send_scope = send_scope
        self._log_outbound = log_outbound
        self._log_limit = max(1, log_limit)
        self._clock = clock
        self._recalled_messages: dict[str, dict[str, float]] = {}
        self._skip_logged: dict[tuple[str, str], None] = {}

    def evict_scopes(self, scopes: set[str]) -> None:
        for scope in scopes:
            self._recalled_messages.pop(scope, None)
        for key in list(self._skip_logged):
            if key[0] in scopes:
                self._skip_logged.pop(key, None)

    def clear(self) -> None:
        self._recalled_messages.clear()
        self._skip_logged.clear()

    def _prune_recalled_messages(self) -> None:
        store = self._recalled_messages
        now = self._clock()
        for scope, items in list(store.items()):
            for message_id, expires_at in list(items.items()):
                if expires_at <= now:
                    items.pop(message_id, None)
            if not items:
                store.pop(scope, None)

    def _event_raw_payload(self, event: Any) -> dict[str, Any]:
        for source in self._event_sources(event):
            for current in (
                getattr(source, "raw_message", None),
                getattr(getattr(source, "message_obj", None), "raw_message", None),
            ):
                if isinstance(current, dict):
                    return current
        return {}

    def _event_is_recall_notice(self, event: Any) -> bool:
        raw = self._event_raw_payload(event)
        return (
            str(raw.get("post_type") or "").strip() == "notice"
            and str(raw.get("notice_type") or "").strip() in self._RECALL_NOTICE_TYPES
        )

    def note_recalled_message(self, event: Any) -> bool:
        if not self._event_is_recall_notice(event):
            return False
        raw = self._event_raw_payload(event)
        message_id = str(raw.get("message_id") or "").strip()
        if not message_id:
            return True
        scope = self._event_session_id(event)
        if not scope:
            return True
        self._prune_recalled_messages()
        self._recalled_messages.setdefault(scope, {})[message_id] = (
            self._clock() + self._RECALL_TTL_SECONDS
        )
        for remove in self._remove_context:
            remove(scope, message_id)
        logger.debug(f"{LOG_PREFIX} 已记录用户撤回消息：{scope}#{message_id}")
        return True

    def message_was_recalled(self, scope: str, message_id: str) -> bool:
        scope = str(scope or "").strip()
        message_id = str(message_id or "").strip()
        if not scope or not message_id:
            return False
        self._prune_recalled_messages()
        return message_id in self._recalled_messages.get(scope, {})

    def _event_message_was_recalled(self, event: Any) -> bool:
        return self.message_was_recalled(
            self._event_session_id(event), self._event_message_id(event)
        )

    def _remember_recalled_skip_log(self, key: tuple[str, str]) -> bool:
        if key in self._skip_logged:
            return False
        self._skip_logged[key] = None
        while len(self._skip_logged) > self._log_limit:
            self._skip_logged.pop(next(iter(self._skip_logged)))
        return True

    def log_recalled_history_skip(self, event: Any) -> None:
        scope = self._event_session_id(event)
        message_id = self._event_message_id(event)
        key = (scope, message_id if scope and message_id else f"event:{id(event)}")
        if not self._remember_recalled_skip_log(key):
            return
        logger.debug(f"{LOG_PREFIX} 原消息已撤回，已跳过本轮回复与历史沉淀。")

    def event_was_recalled(self, event: Any, *, log_skip: bool = False) -> bool:
        recalled = self._event_message_was_recalled(event)
        if recalled and log_skip:
            self.log_recalled_history_skip(event)
        return recalled

    def _send_source_was_recalled(
        self,
        scope: str,
        *,
        source_event: Any = None,
        source_message_id: str = "",
    ) -> bool:
        message_id = str(source_message_id or "").strip()
        if source_event is not None:
            event_scope = self._event_session_id(source_event) or scope
            message_id = message_id or self._event_message_id(source_event)
            return self.message_was_recalled(event_scope, message_id)
        return self.message_was_recalled(scope, message_id)

    def source_was_recalled(
        self,
        scope: str,
        *,
        source_event: Any = None,
        source_message_id: str = "",
    ) -> bool:
        return self._send_source_was_recalled(
            scope,
            source_event=source_event,
            source_message_id=source_message_id,
        )

    def can_send_for_source(
        self,
        scope: str,
        *,
        source_event: Any = None,
        source_message_id: str = "",
    ) -> bool:
        if self._send_source_was_recalled(
            scope,
            source_event=source_event,
            source_message_id=source_message_id,
        ):
            logger.debug(f"{LOG_PREFIX} 原消息已撤回，取消本次发送。")
            return False
        return True

    async def send_message_if_not_recalled(
        self,
        scope: str,
        chain: Any,
        *,
        source_event: Any = None,
        source_message_id: str = "",
        raise_delivery_errors: bool = False,
        log_outbound: bool = True,
        prefer_scope_send: bool = False,
    ) -> bool:
        if not self.can_send_for_source(
            scope, source_event=source_event, source_message_id=source_message_id
        ):
            return False
        event_sender = getattr(source_event, "send", None)
        prefer_scope_send = prefer_scope_send or bool(
            getattr(source_event, "_daily_life_media_scope_delivery", False)
        )
        if callable(event_sender) and not prefer_scope_send:
            await event_sender(chain)
            if log_outbound:
                await self._log_outbound(
                    chain, scope=scope, source_event=source_event, source="direct"
                )
            return True
        sent = await self._send_scope(
            scope, chain, raise_delivery_errors=raise_delivery_errors
        )
        if sent and log_outbound:
            await self._log_outbound(chain, scope=scope, source="direct")
        return sent

    def suppress_recalled_event_result(self, event: Any) -> bool:
        if not self._event_message_was_recalled(event):
            return False
        clearer = getattr(event, "clear_result", None)
        if callable(clearer):
            clearer()
        else:
            result = getattr(event, "get_result", lambda: None)()
            chain = getattr(result, "chain", None)
            if isinstance(chain, list):
                chain.clear()
        logger.debug(f"{LOG_PREFIX} 原消息已撤回，已取消本轮回复发送。")
        return True

    def stop_recalled_event_before_history(self, event: Any) -> bool:
        if not self._event_message_was_recalled(event):
            return False
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            stopper()
        # AstrBot 在停止没有结果的事件时会创建空结果；随后清除它，
        # 使 RespondStage 没有可发送或后处理的内容。
        clearer = getattr(event, "clear_result", None)
        if callable(clearer):
            clearer()
        self.log_recalled_history_skip(event)
        return True


__all__ = ["RecallService"]
