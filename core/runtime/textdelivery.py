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
    plugins = getattr(event, "plugins_name", None)
    if plugins is not None and plugins != ["*"] and PLUGIN_NAME not in plugins:
        return None
    peer = getattr(meta, "star_cls", None)
    if not callable(getattr(peer, "_get_unified_splitter_config", None)) or not callable(
        getattr(peer, "_build_unified_segments", None)
    ) or not callable(getattr(peer, "_post_clean_segments", None)):
        return None
    return peer


def external_text_available(runtime: Any, event: Any, result: Any = None) -> bool:
    try:
        peer = _peer(runtime, event)
        result = result if result is not None else event.get_result()
        policy = _legacy_policy(peer, event, result) if peer is not None else {}
        return policy.get("accepted") is True
    except Exception:
        return False


def _legacy_policy(peer: Any, event: Any, result: Any) -> dict:
    settings = peer._get_unified_splitter_config(event)
    reason = ""
    if not result or not getattr(result, "chain", None):
        reason = "empty"
    elif not settings.get("enable", True):
        reason = "disabled"
    else:
        plugins = getattr(event, "plugins_name", None)
        if plugins is not None and plugins != ["*"] and PLUGIN_NAME not in plugins:
            reason = "plugin_scope"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        blacklist = settings.get("conversation_blacklist", []) or []
        whitelist = settings.get("conversation_whitelist", []) or []
        if umo in blacklist or (whitelist and umo not in whitelist):
            reason = "conversation_scope"
        group = getattr(getattr(event, "message_obj", None), "group_id", None)
        if (group or ":GroupMessage:" in umo) and not settings.get("enable_group_split", True):
            reason = "group_disabled"
        is_model = getattr(peer, "_is_model_result", None)
        if settings.get("split_scope", "llm_only") == "llm_only" and callable(is_model):
            if not is_model(event, result):
                reason = "result_scope"
    return {"accepted": not bool(reason), "reason": reason or "applicable"}


async def _legacy_process(
    peer: Any, event: Any, result: Any, *, send: Any, is_current: Any,
    on_sent: Any = None,
) -> dict:
    policy = _legacy_policy(peer, event, result)
    if not policy["accepted"]:
        return {"status": "not_handled", "sent_count": 0, "segment_count": 0,
                "reason": policy["reason"]}
    settings = peer._get_unified_splitter_config(event)
    hook = getattr(peer, "unified_on_decorating_result")
    globals_ = getattr(getattr(hook, "__func__", hook), "__globals__", {})
    plain_type = globals_.get("Plain")
    filter_output = globals_.get("filter_output")
    chain = [copy.copy(item) for item in result.chain]
    is_model = getattr(peer, "_is_model_result", lambda *_: False)
    if settings.get("output_filter_enabled", False) and is_model(event, result) and callable(filter_output):
        merged = peer._merge_adjacent_plain(chain)
        chain = []
        for component in merged:
            if plain_type and isinstance(component, plain_type):
                component.text = filter_output(component.text, settings)
            if not plain_type or not isinstance(component, plain_type) or component.text.strip():
                chain.append(component)
    try:
        segments = await peer._build_unified_segments(event, chain, settings)
        peer._post_clean_segments(segments, settings)
        segments = [segment for segment in segments if peer._segment_has_content(segment)]
    except Exception:
        return {"status": "failed", "sent_count": 0, "segment_count": 0,
                "reason": "preparation_failed"}
    globals_ = getattr(getattr(hook, "__func__", hook), "__globals__", {})
    reply_type = globals_.get("Reply")
    source_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
    if segments and settings.get("enable_reply", True) and source_id and reply_type:
        if not any(isinstance(component, reply_type) for component in segments[0]):
            segments[0].insert(0, reply_type(id=source_id))
    count = 0
    attempted = False
    reason = "complete"
    try:
        for index, segment in enumerate(segments):
            if not is_current() or not _legacy_policy(peer, event, result)["accepted"]:
                reason = "stale_or_disabled"
                return {"status": "cancelled", "sent_count": count,
                        "segment_count": len(segments), "reason": reason}
            if index:
                delay = peer._unified_delay(segment, settings, event)
                if delay > 0:
                    await asyncio.sleep(delay)
            if not is_current() or not _legacy_policy(peer, event, result)["accepted"]:
                reason = "stale_or_disabled"
                return {"status": "cancelled", "sent_count": count,
                        "segment_count": len(segments), "reason": reason}
            message = MessageChain()
            message.chain = segment
            attempted = True
            sent = await send(message)
            if sent is False:
                raise RuntimeError("delivery refused")
            count += 1
            if on_sent is not None:
                value = on_sent(message)
                if inspect.isawaitable(value):
                    await value
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"status": "partial" if count else "failed", "sent_count": count,
                "segment_count": len(segments),
                "reason": "send_or_receipt_failed" if attempted else "preparation_failed"}
    return {"status": "handled", "sent_count": count, "segment_count": len(segments),
            "reason": reason}


async def handoff_reply(
    runtime: Any, event: Any, result: Any, *, send: Any, is_current: Any,
    on_sent: Any = None,
) -> dict:
    messages = []
    try:
        peer = _peer(runtime, event)
        applicable = peer is not None and external_text_available(runtime, event, result)
    except Exception:
        peer, applicable = None, False
    if not applicable:
        logger.info("[日常生活] owner=daily_life segments=0 sent=0 reason=external_unavailable_or_inapplicable")
        return {"status": "not_handled", "sent_count": 0, "messages": messages}

    def current():
        return is_current() and _peer(runtime, event) is peer

    async def noted(message):
        messages.append(message)
        if on_sent is not None:
            value = on_sent(message)
            if inspect.isawaitable(value):
                await value

    try:
        outcome = await _legacy_process(
            peer, event, result, send=send, is_current=current, on_sent=noted,
        )
        valid = isinstance(outcome, dict) and outcome.get("status") in {
            "not_handled", "handled", "partial", "failed", "cancelled"
        }
        if not valid:
            outcome = {"status": "failed", "segment_count": 0, "reason": "invalid_receipt"}
        if messages and outcome["status"] == "not_handled":
            outcome = {"status": "partial", "segment_count": 0, "reason": "invalid_receipt"}
    except Exception:
        # An exception after acceptance must not trigger a whole-message replay.
        outcome = {"status": "partial" if messages else "failed", "segment_count": 0, "reason": "external_error"}
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
