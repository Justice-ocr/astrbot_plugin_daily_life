from __future__ import annotations

import datetime
from collections import deque
from typing import Any

from ...life.tools import build_time_context

from typing import Protocol


class ResponseGateStateComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _RESPONSE_GATE_CONTINUATION_SECONDS: Any
    _RESPONSE_GATE_CONTINUATION_TURNS: Any
    _continuous_turn_trim_messages: Any
    _observed_user_history_keys: Any
    _observed_user_history_order: Any
    _response_gate_attention: Any
    _response_gate_backoff_until: Any
    _response_gate_clear_wait: Any
    _response_gate_continuation: Any
    _response_gate_continuation_store: Any
    _response_gate_first_seen_at: Any
    _response_gate_last_reply_at: Any
    _response_gate_last_seen_at: Any
    _response_gate_no_reply_count: Any
    _response_gate_note_attention: Any
    _response_gate_pending_count: Any
    _response_gate_reply_times: Any
    _response_gate_semantic_metrics: Any
    _response_gate_wait_state: Any
    _response_gate_waiting: Any
    archive: Any
    config: Any


class ResponseGateStateComponent:
    def __init__(self, context: ResponseGateStateComponentContext) -> None:
        self.context = context

    _RESPONSE_GATE_CONTINUATION_SECONDS = 8 * 60
    _RESPONSE_GATE_CONTINUATION_TURNS = 2
    _MESSAGE_DEDUP_LIMIT = 4096

    def _init_response_gate_state(self) -> None:
        self.context._response_gate_last_reply_at: dict[str, datetime.datetime] = {}
        self.context._response_gate_last_seen_at: dict[str, datetime.datetime] = {}
        self.context._response_gate_first_seen_at: dict[str, datetime.datetime] = {}
        self.context._response_gate_pending_count: dict[str, int] = {}
        self.context._response_gate_reply_times: dict[str, list[datetime.datetime]] = {}
        self.context._response_gate_no_reply_count: dict[str, int] = {}
        self.context._response_gate_backoff_until: dict[str, datetime.datetime] = {}
        self.context._response_gate_continuation: dict[str, dict[str, Any]] = {}
        self.context._response_gate_waiting: dict[str, dict[str, Any]] = {}
        self.context._response_gate_attention: dict[str, Any] = {}
        self.context._observed_user_history_keys: set[str] = set()
        self.context._observed_user_history_order: deque[str] = deque()
        self.context._response_gate_semantic_metrics: dict[str, int] = {
            "accepted": 0,
            "fallback": 0,
            "timeout": 0,
            "invalid": 0,
        }

    def _response_gate_wait_state(
        self, key: str, now: datetime.datetime
    ) -> dict[str, Any] | None:
        store = getattr(self.context, "_response_gate_waiting", None)
        if not isinstance(store, dict):
            self.context._response_gate_waiting = {}
            return None
        item = store.get(key)
        if not isinstance(item, dict):
            return None
        until = item.get("until")
        if not isinstance(until, datetime.datetime) or now >= until:
            store.pop(key, None)
            return None
        return item

    def _response_gate_mark_wait(
        self,
        key: str,
        now: datetime.datetime,
        *,
        reason: str = "",
        message_id: str = "",
        message_text: str = "",
    ) -> None:
        current = self.context._response_gate_wait_state(key, now)
        rounds = int(current.get("rounds") or 0) + 1 if current else 1
        messages = list(current.get("messages") or []) if current else []
        text = str(message_text or "").strip()
        if text:
            messages.append(text)
        messages, _ = self.context._continuous_turn_trim_messages(messages, [""] * len(messages))
        self.context._response_gate_waiting[key] = {
            "until": now + datetime.timedelta(seconds=90),
            "rounds": rounds,
            "reason": str(reason or "").strip(),
            "message_id": str(message_id or "").strip(),
            "messages": messages,
        }

    def _response_gate_clear_wait(self, key: str) -> None:
        store = getattr(self.context, "_response_gate_waiting", None)
        if isinstance(store, dict):
            store.pop(key, None)

    def _response_gate_continuation_store(self) -> dict[str, dict[str, Any]]:
        store = getattr(self.context, "_response_gate_continuation", None)
        if not isinstance(store, dict):
            self.context._response_gate_continuation = {}
            store = self.context._response_gate_continuation
        return store

    def _response_gate_mark_continuation(
        self,
        key: str,
        now: datetime.datetime,
        *,
        reason: str = "",
        turns: int | None = None,
        seconds: int | None = None,
    ) -> None:
        if not key:
            return
        try:
            turn_count = int(
                turns if turns is not None else self.context._RESPONSE_GATE_CONTINUATION_TURNS
            )
        except (TypeError, ValueError):
            turn_count = self.context._RESPONSE_GATE_CONTINUATION_TURNS
        turn_count = max(1, min(turn_count, 3))
        try:
            window_seconds = int(
                seconds
                if seconds is not None
                else self.context._RESPONSE_GATE_CONTINUATION_SECONDS
            )
        except (TypeError, ValueError):
            window_seconds = self.context._RESPONSE_GATE_CONTINUATION_SECONDS
        window_seconds = max(60, min(window_seconds, 15 * 60))
        store = self.context._response_gate_continuation_store()
        current = store.get(key)
        if isinstance(current, dict):
            until = current.get("until")
            current_turns = int(current.get("turns") or 0)
            if (
                isinstance(until, datetime.datetime)
                and now < until
                and current_turns > 0
            ):
                return
        store[key] = {
            "until": now + datetime.timedelta(seconds=window_seconds),
            "turns": turn_count,
            "reason": reason,
        }
        no_reply_count = getattr(self.context, "_response_gate_no_reply_count", None)
        if not isinstance(no_reply_count, dict):
            self.context._response_gate_no_reply_count = {}
            no_reply_count = self.context._response_gate_no_reply_count
        backoff_until = getattr(self.context, "_response_gate_backoff_until", None)
        if not isinstance(backoff_until, dict):
            self.context._response_gate_backoff_until = {}
            backoff_until = self.context._response_gate_backoff_until
        no_reply_count[key] = 0
        backoff_until.pop(key, None)

    def _response_gate_continuation_reason(
        self, key: str, now: datetime.datetime
    ) -> str:
        item = self.context._response_gate_continuation_store().get(key)
        if not isinstance(item, dict):
            return ""
        until = item.get("until")
        turns = int(item.get("turns") or 0)
        if not isinstance(until, datetime.datetime) or now >= until or turns <= 0:
            self.context._response_gate_continuation_store().pop(key, None)
            return ""
        item["turns"] = turns - 1
        if item["turns"] <= 0:
            self.context._response_gate_continuation_store().pop(key, None)
        reason = str(item.get("reason") or "").strip()
        return reason or "闲时回复刚被用户接住，先顺势继续聊一会儿"

    async def _response_gate_current_state(self, now: datetime.datetime) -> Any | None:
        archive = getattr(self.context, "archive", None)
        if archive is None:
            return None
        try:
            today = now.strftime("%Y-%m-%d")
            target_date = build_time_context(
                now, getattr(self.context.config, "schedule_time", "07:00")
            ).business_date_text
            day = await archive.get_day(target_date)
            if day is None and target_date != today:
                day = await archive.get_day(today)
            return getattr(day, "state", None) if day is not None else None
        except Exception:
            return None

    def _response_gate_record_seen(self, key: str, now: datetime.datetime) -> int:
        previous_seen_at = self.context._response_gate_last_seen_at.get(key)
        if (
            not isinstance(previous_seen_at, datetime.datetime)
            or (now - previous_seen_at).total_seconds() > 180
        ):
            pending_count = 1
            self.context._response_gate_first_seen_at[key] = now
        else:
            pending_count = self.context._response_gate_pending_count.get(key, 0) + 1
        self.context._response_gate_last_seen_at[key] = now
        self.context._response_gate_pending_count[key] = pending_count
        return pending_count

    def _response_gate_record_reply(self, key: str, now: datetime.datetime) -> None:
        self.context._response_gate_clear_wait(key)
        self.context._response_gate_last_reply_at[key] = now
        reply_times = self.context._response_gate_reply_times.setdefault(key, [])
        reply_times.append(now)
        self.context._response_gate_reply_times[key] = [
            item for item in reply_times[-8:] if (now - item).total_seconds() <= 10 * 60
        ]
        self.context._response_gate_pending_count[key] = 0
        self.context._response_gate_first_seen_at.pop(key, None)
        self.context._response_gate_no_reply_count[key] = 0
        self.context._response_gate_backoff_until.pop(key, None)
        note_attention = getattr(self.context, "_response_gate_note_attention", None)
        if callable(note_attention):
            note_attention(key, now, replied=True)

    def _response_gate_record_no_reply(self, key: str, now: datetime.datetime) -> None:
        count = self.context._response_gate_no_reply_count.get(key, 0) + 1
        self.context._response_gate_no_reply_count[key] = count
        note_attention = getattr(self.context, "_response_gate_note_attention", None)
        if callable(note_attention):
            note_attention(key, now, replied=False)
        config = self.context.config.response_gate
        base = max(0, int(config.no_reply_backoff_seconds or 0))
        cap = max(0, int(config.no_reply_backoff_cap_seconds or 0))
        start_count = max(1, int(config.no_reply_backoff_start_count or 1))
        if base <= 0 or cap <= 0 or count < start_count:
            return
        seconds = min(cap, base * (2 ** max(0, count - start_count)))
        self.context._response_gate_backoff_until[key] = now + datetime.timedelta(
            seconds=seconds
        )

    def _response_gate_in_backoff(
        self, key: str, now: datetime.datetime, pending_count: int
    ) -> bool:
        config = self.context.config.response_gate
        bypass = max(0, int(config.bypass_pending_count or 0))
        if bypass and pending_count >= bypass:
            self.context._response_gate_backoff_until.pop(key, None)
            return False
        until = self.context._response_gate_backoff_until.get(key)
        if not isinstance(until, datetime.datetime):
            return False
        if now >= until:
            self.context._response_gate_backoff_until.pop(key, None)
            return False
        return True
