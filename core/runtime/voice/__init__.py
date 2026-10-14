from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ..markers import LOG_PREFIX

from . import limit as _part_voice_switch_gate_component
from . import preface as _part_silent_tool_preface_component
from . import judge as _part_voice_switch_judge_component
from . import trace as _part_voice_switch_record_component
from typing import Protocol


class VoiceSwitchComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _SEMANTIC_SEGMENT_PENDING_ATTR: Any
    _append_turn_history: Any
    _event_user_history_text: Any
    _is_active_agent_intermediate_result: Any
    _judge_voice_switch_channel: Any
    _mark_voice_switch_channel: Any
    _note_voice_expression_decision: Any
    _prune_voice_switch_rounds: Any
    _replace_result_with_voice: Any
    _semantic_segment_revision: Any
    _voice_allowed_for_scope: Any
    _voice_switch_auto_gate: Any
    _voice_switch_reply_text_from_event: Any
    _voice_switch_round_store: Any
    _voice_switch_scope_key: Any
    config: Any
    continuous_turn_event_is_current: Any
    event_was_recalled: Any
    mark_structured_pending_bot_text: Any
    media: Any


class VoiceSwitchComponent:
    def __init__(self, context: VoiceSwitchComponentContext) -> None:
        self.context = context

    async def apply_voice_switch_before_send(self, event: Any) -> bool:
        scope = self.context._voice_switch_scope_key(event)
        if not scope:
            return False
        self.context._prune_voice_switch_rounds()
        item = self.context._voice_switch_round_store().get(scope)
        if (
            not isinstance(item, dict)
            or item.get("used_voice")
            or item.get("pre_send_checked")
        ):
            return False
        reply_text = self.context._voice_switch_reply_text_from_event(event)
        if not reply_text:
            return False
        voice_config = getattr(self.context.config, "voice_generation", None)
        if (
            not voice_config
            or not voice_config.enabled
            or not getattr(voice_config, "smart_switch_enabled", True)
        ):
            return False
        if not self.context._voice_allowed_for_scope(event):
            return False
        item["pre_send_checked"] = True
        if self.context._is_active_agent_intermediate_result(event):
            item["text_reason"] = (
                "工具还在执行中，这句只是调用前的临时说明，保留文字更合适。"
            )
            return False
        payload = self.context._judge_voice_switch_channel(event, reply_text)
        channel = str(payload.get("channel") or "text").strip().lower()
        reason = str(payload.get("reason") or "").strip()
        emotion = str(payload.get("emotion") or "").strip()
        emotion_category = str(payload.get("emotion_category") or "").strip()
        voice_style = str(payload.get("voice_style") or "").strip().lower()
        try:
            revision_getter = getattr(self.context, "_semantic_segment_revision", None)
            revision = revision_getter(scope) if callable(revision_getter) else None
            confidence = float(payload.get("confidence", 1.0) or 1.0)
        except (TypeError, ValueError):
            confidence = 1.0
        if channel != "voice":
            item["text_reason"] = reason or "我想把这轮话打出来，留在屏幕上更清楚。"
            return False
        allowed, gate_reason = self.context._voice_switch_auto_gate(
            event, reply_text, confidence
        )
        if not allowed:
            item["text_reason"] = gate_reason
            return False
        try:
            voice_kwargs = {
                "emotion": emotion,
                "emotion_category": emotion_category,
            }
            if voice_style and voice_style != "neutral":
                voice_kwargs["voice_style"] = voice_style
            generated = await self.context.media.voice.synthesize(reply_text, **voice_kwargs)
            current = getattr(self.context, "continuous_turn_event_is_current", None)
            recalled = getattr(self.context, "event_was_recalled", None)
            if (
                getattr(event, "is_stopped", lambda: False)()
                or (callable(current) and not current(event))
                or (callable(recalled) and recalled(event))
                or (callable(revision_getter) and revision_getter(scope) != revision)
            ):
                getattr(event, "set_result", lambda value: None)(None)
                return False
            self.context.mark_structured_pending_bot_text(event, reply_text, media="语音")
            self.context._replace_result_with_voice(event, str(generated.path))
            setattr(event, self.context._SEMANTIC_SEGMENT_PENDING_ATTR, [])
            item["used_voice"] = True
            event._daily_life_pending_voice_receipt = {
                "scope": scope,
                "text": reply_text,
                "reason": reason or "我觉得这句话更适合直接说出来。",
                "emotion": emotion,
                "emotion_category": emotion_category,
                "confidence": confidence,
            }
            return True
        except Exception as exc:
            item["text_reason"] = (
                f"{reason or '我原本想直接说出来'}；但语音生成失败，改用文字发送：{exc}"
            )
            logger.debug(f"{LOG_PREFIX} 发送前语音智能切换失败，保留文字发送：{exc}")
            return False

    async def note_voice_switch_message_sent(self, event: Any) -> bool:
        receipt = getattr(event, "_daily_life_pending_voice_receipt", None)
        if not isinstance(receipt, dict):
            return False
        delattr(event, "_daily_life_pending_voice_receipt")
        await self.context._append_turn_history(
            receipt["scope"],
            event,
            self.context._event_user_history_text(event),
            receipt["text"],
        )
        await self.context._note_voice_expression_decision(
            event=event,
            channel="语音",
            source="普通聊天",
            result="已发送",
            **{key: value for key, value in receipt.items() if key != "scope"},
        )
        self.context._mark_voice_switch_channel(event, "语音")
        return True

    _VOICE_SWITCH_CADENCE_TTL_SECONDS = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_CADENCE_TTL_SECONDS

    _VOICE_SWITCH_COOLDOWN_SECONDS = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_COOLDOWN_SECONDS

    _VOICE_SWITCH_CHAIN_WINDOW_SECONDS = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_CHAIN_WINDOW_SECONDS

    _VOICE_SWITCH_MAX_CONSECUTIVE_VOICE = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_MAX_CONSECUTIVE_VOICE

    _VOICE_SWITCH_CHAIN_LIMIT_CHOICES = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_CHAIN_LIMIT_CHOICES

    _VOICE_SWITCH_TEXT_TURNS_AFTER_VOICE = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_TEXT_TURNS_AFTER_VOICE

    _VOICE_SWITCH_LONG_TEXT_LIMIT = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_LONG_TEXT_LIMIT

    _VOICE_SWITCH_LONG_LINE_LIMIT = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_LONG_LINE_LIMIT

    _VOICE_SWITCH_MAX_SHORT_LINES = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_MAX_SHORT_LINES

    _VOICE_SWITCH_MIN_CONFIDENCE = _part_voice_switch_gate_component.VoiceSwitchGateComponent._VOICE_SWITCH_MIN_CONFIDENCE

    @classmethod
    def _tool_name(cls, tool: _part_silent_tool_preface_component.Any) -> str:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._tool_name.__func__(cls, tool)

    @staticmethod
    def _copy_result_chain(result: _part_silent_tool_preface_component.Any) -> _part_silent_tool_preface_component.Any:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._copy_result_chain(result)

    @staticmethod
    def _normalize_visible_text(value: _part_silent_tool_preface_component.Any) -> str:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._normalize_visible_text(value)

    @classmethod
    def _same_visible_text(cls, left: _part_silent_tool_preface_component.Any, right: _part_silent_tool_preface_component.Any) -> bool:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._same_visible_text.__func__(cls, left, right)

    @staticmethod
    def _plain_send_message_tool_text(event: _part_silent_tool_preface_component.Any, tool_args: _part_silent_tool_preface_component.Any) -> str:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._plain_send_message_tool_text(event, tool_args)

    @staticmethod
    def _plain_tool_preface_text(chain: _part_silent_tool_preface_component.Any) -> str:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._plain_tool_preface_text(chain)

    @staticmethod
    def _follow_up_module() -> _part_silent_tool_preface_component.Any:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._follow_up_module()

    @staticmethod
    def _voice_switch_reply_text_from_event(event: _part_silent_tool_preface_component.Any) -> str:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._voice_switch_reply_text_from_event(event)

    @classmethod
    def _coerce_tool_names(cls, value: _part_silent_tool_preface_component.Any) -> set[str]:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._coerce_tool_names.__func__(cls, value)

    @staticmethod
    def _is_llm_result_object(result: _part_silent_tool_preface_component.Any) -> bool:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._is_llm_result_object(result)

    @staticmethod
    def _runner_is_done(runner: _part_silent_tool_preface_component.Any) -> bool:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._runner_is_done(runner)

    @staticmethod
    def _active_agent_runner(event: _part_silent_tool_preface_component.Any) -> _part_silent_tool_preface_component.Any | None:
        return _part_silent_tool_preface_component.SilentToolPrefaceComponent._active_agent_runner(event)

    _VOICE_SWITCH_MIN_NATURAL_SCORE = _part_voice_switch_judge_component.VoiceSwitchJudgeComponent._VOICE_SWITCH_MIN_NATURAL_SCORE

    @staticmethod
    def _voice_switch_text_decision(reason: str, confidence: float) -> dict[str, _part_voice_switch_judge_component.Any]:
        return _part_voice_switch_judge_component.VoiceSwitchJudgeComponent._voice_switch_text_decision(reason, confidence)

    @staticmethod
    def _voice_switch_has_path_shape(text: str) -> bool:
        return _part_voice_switch_judge_component.VoiceSwitchJudgeComponent._voice_switch_has_path_shape(text)

    @staticmethod
    def _voice_switch_has_list_shape(lines: list[str]) -> bool:
        return _part_voice_switch_judge_component.VoiceSwitchJudgeComponent._voice_switch_has_list_shape(lines)

    @staticmethod
    def _voice_switch_has_dense_foreign_shape(text: str) -> bool:
        return _part_voice_switch_judge_component.VoiceSwitchJudgeComponent._voice_switch_has_dense_foreign_shape(text)

    @staticmethod
    def _voice_switch_has_dense_number_shape(text: str) -> bool:
        return _part_voice_switch_judge_component.VoiceSwitchJudgeComponent._voice_switch_has_dense_number_shape(text)

    @staticmethod
    def _voice_record_component(item: _part_voice_switch_record_component.Any) -> bool:
        return _part_voice_switch_record_component.VoiceSwitchRecordComponent._voice_record_component(item)

    @staticmethod
    def _strip_voice_record_caption(item: _part_voice_switch_record_component.Any) -> _part_voice_switch_record_component.Any:
        return _part_voice_switch_record_component.VoiceSwitchRecordComponent._strip_voice_record_caption(item)

    @staticmethod
    def _voice_expression_log_title(channel: str, source: str) -> str:
        return _part_voice_switch_record_component.VoiceSwitchRecordComponent._voice_expression_log_title(channel, source)



__all__ = ["VoiceSwitchComponent"]
