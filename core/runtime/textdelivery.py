"""Optional text-delivery handoff; no imports from peer plugin packages."""

from __future__ import annotations

import asyncio
import copy
import inspect
from types import SimpleNamespace
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

PLUGIN_NAME = "astrbot_plugin_proactive_splitter"
EXTERNAL_OWNER_ATTR = "_daily_life_external_text_owner"


def _peer(runtime: Any, event: Any) -> Any:
    getter = getattr(getattr(runtime, "context", None), "get_registered_star", None)
    meta = getter(PLUGIN_NAME) if callable(getter) else None
    if meta is None or not getattr(meta, "activated", False):
        return None
    peer = getattr(meta, "star_cls", None)
    if not callable(getattr(peer, "external_reply_policy", None)) or not callable(
        getattr(peer, "process_external_reply", None)
    ):
        return None
    return peer


def external_text_available(runtime: Any, event: Any, result: Any = None) -> bool:
    try:
        peer = _peer(runtime, event)
        result = result if result is not None else event.get_result()
        policy = peer.external_reply_policy(event, result) if peer is not None else {}
        return policy.get("api_version") == 1 and policy.get("accepted") is True
    except Exception:
        return False


async def handoff_reply(
    runtime: Any, event: Any, result: Any, *, send: Any, is_current: Any,
    on_sent: Any = None,
) -> dict:
    messages = []
    peer = _peer(runtime, event)
    if peer is None or not external_text_available(runtime, event, result):
        return {"status": "not_handled", "sent_count": 0, "messages": messages}

    def current():
        return is_current() and _peer(runtime, event) is peer

    async def noted(message):
        messages.append(message)
        if on_sent is not None:
            value = on_sent(message)
            if inspect.isawaitable(value):
                await value

    attempted = False

    async def guarded_send(message):
        nonlocal attempted
        attempted = True
        return await send(message)

    try:
        outcome = await peer.process_external_reply(
            event, result, send=guarded_send, is_current=current,
            on_sent=noted, allow_tts=False,
        )
        if not isinstance(outcome, dict) or outcome.get("status") not in {
            "not_handled", "handled", "partial", "failed", "cancelled"
        }:
            outcome = {"status": "failed", "segment_count": 0, "reason": "invalid_receipt"}
        if outcome.get("reason") == "preparation_failed" and not attempted:
            outcome = dict(outcome, status="not_handled")
        if messages and outcome["status"] == "not_handled":
            outcome = dict(outcome, status="partial", reason="invalid_receipt")
    except Exception:
        outcome = {
            "status": "partial" if messages or attempted else "not_handled",
            "segment_count": 0,
            "reason": "external_error_after_send" if attempted else "preparation_failed",
        }
    outcome = dict(outcome, messages=messages, sent_count=len(messages))
    logger.info(
        "[日常生活] owner=unified_splitter "
        f"status={outcome['status']} segments={int(outcome.get('segment_count', 0))} "
        f"sent={len(messages)} reason={outcome.get('reason', 'external_receipt')}"
    )
    return outcome


def background_reply_event(runtime: Any, scope: str, text: str, source_event: Any, message_id: str):
    if source_event is not None and getattr(source_event, "unified_msg_origin", "") == scope:
        event = copy.copy(source_event)
    else:
        event = SimpleNamespace(unified_msg_origin=scope)
    getter = getattr(getattr(runtime, "context", None), "get_config", None)
    config = getter(scope) if callable(getter) else {}
    if not hasattr(event, "plugins_name"):
        event.plugins_name = config.get("plugin_set", ["*"])
    event.message_obj = SimpleNamespace(
        message_id=message_id,
        group_id=scope.rsplit(":", 1)[-1] if ":GroupMessage:" in scope else None,
    )
    setattr(event, "__unified_splitter_llm_reply", True)
    result = MessageChain().message(text)
    return event, result


async def try_send_background(
    runtime: Any, scope: str, text: str, *, source_event: Any = None,
    source_message_id: str = "", source: str = "background",
    raise_delivery_errors: bool = False, on_sent: Any = None,
) -> bool | None:
    event, result = background_reply_event(runtime, scope, text, source_event, source_message_id)
    if not external_text_available(runtime, event, result):
        return None
    if not isinstance(getattr(runtime, "_semantic_segment_revisions", None), dict):
        runtime._semantic_segment_init_state()
    revision = runtime._semantic_segment_revision(scope)
    epoch = runtime._semantic_segment_epoch(scope) + 1
    runtime._semantic_segment_epochs[scope] = epoch

    def current():
        if runtime._semantic_segment_revision(scope) != revision or runtime._semantic_segment_epoch(scope) != epoch:
            return False
        checker = getattr(runtime, "can_send_for_source", None)
        if callable(checker) and not checker(scope, source_event=source_event, source_message_id=source_message_id):
            return False
        guard = getattr(source_event, "_daily_life_reply_is_current", None)
        if callable(guard) and not guard():
            return False
        if getattr(source_event, "is_stopped", lambda: False)():
            return False
        if source in {"proactive", "proactive_voice_call"}:
            guard = getattr(runtime, "_proactive_send_is_current", None)
            if callable(guard) and not guard(source_event):
                return False
        guard = getattr(runtime, "media_request_is_current_turn", None)
        if source.endswith(("followup", "failure")) and callable(guard) and not guard(source_event):
            return False
        return True

    index = 0

    async def send(message):
        nonlocal index
        decorator = getattr(runtime, "decorate_group_addressing_chain", None)
        if callable(decorator):
            decorator(message, target_scope=scope, source_event=source_event,
                      source_message_id=source_message_id, segment_index=index, source=source)
        sent = await runtime.send_message_if_not_recalled(
            scope, message, source_event=source_event,
            source_message_id=source_message_id,
            raise_delivery_errors=raise_delivery_errors,
        )
        index += 1
        return sent

    def noted(message):
        sent_text = "".join(runtime._semantic_segment_text(item) for item in message.chain)
        if sent_text:
            if on_sent is not None:
                on_sent(sent_text)
            runtime.note_structured_bot_message(scope, sent_text, source_event=source_event)

    outcome = await handoff_reply(
        runtime, event, result, send=send, is_current=current, on_sent=noted,
    )
    if outcome["status"] == "not_handled":
        return None
    # Partial and cancelled sends already have per-unit receipts. Do not report
    # the original full text as delivered or let callers replay that text.
    return outcome["status"] == "handled" and outcome["sent_count"] > 0
