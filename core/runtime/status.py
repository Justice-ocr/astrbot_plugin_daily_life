import copy
import datetime
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..archive import DayRevisionConflict
from ..clock import now as life_now
from ..life.condition import (
    format_daily_variability_prompt,
    format_state_prompt,
    normalize_state,
    state_is_stale,
    state_log_entry,
)
from ..life.restdelay import format_rest_delay_hint
from ..life.body import ContinuousBody, project_body
from ..life.signals import physiological_rhythm_log_from_state
from ..life.tools import (
    extract_json_from_text,
    get_current_timeline_status,
    timeline_deferred_until,
    timeline_item_datetime,
)
from ..models import DayRecord, EmotionArcRecord, LifeState
from ..prompts import (
    BODY_CONDITION_JSON_EXAMPLE,
    CORE_AUTONOMY_RULES,
    CORE_JSON_OUTPUT_RULES,
    CORE_STATE_BEHAVIOR_RULES,
    cache_friendly_prompt,
)
from .markers import LOG_PREFIX


@dataclass(frozen=True, slots=True)
class _StateRefreshSpec:
    date_str: str
    now: datetime.datetime
    source: str
    detail: str
    source_event: Any
    notify_page: bool


class StateRefreshService:
    """通过显式依赖刷新实时状态，并同步连续生活和状态旁记录。"""

    def __init__(
        self,
        *,
        config: Callable[[], Any],
        archive: Callable[[], Any],
        compute_sleep: Callable[[DayRecord | None, DayRecord], tuple[float, float, float]],
        replan: Callable[..., Any],
        sync_world_facts: Callable[..., Awaitable[Any]],
        text_provider: Callable[[str], Awaitable[Any]],
        call_text_model: Callable[..., Awaitable[str]],
        close_text_session: Callable[[str], Awaitable[None]],
        event_was_recalled: Callable[..., bool],
        sync_kernel: Callable[..., Awaitable[None]],
        notify_status: Callable[[str], Awaitable[Any]],
    ) -> None:
        self._config = config
        self._archive = archive
        self._compute_sleep = compute_sleep
        self._replan = replan
        self._sync_world_facts = sync_world_facts
        self._text_provider = text_provider
        self._call_text_model = call_text_model
        self._close_text_session = close_text_session
        self._event_was_recalled = event_was_recalled
        self._sync_kernel = sync_kernel
        self._notify_status = notify_status

    def _build_state_update_prompt(
        self,
        data: DayRecord,
        now: datetime.datetime,
        source: str,
        detail: str = "",
        emotion_context: str = "",
        rhythm_context: str = "",
    ) -> str:
        curr_act, next_act = get_current_timeline_status(data.timeline, now, data.date, meta=data.meta)
        activity_text = curr_act.activity if curr_act else "碎片时间"
        next_time = timeline_item_datetime(next_act, data.date, meta=data.meta)
        next_text = f"{next_time:%m-%d %H:%M} {next_act.activity}" if next_act and next_time else "无"
        rest_delay_hint = format_rest_delay_hint(data, now)
        state_log = data.state_log
        state_log_text = "\n".join(str(item) for item in state_log[-6:]) or "无"
        lifecycle_text = ""
        meta = data.meta or {}
        deferred_ids = {
            f"{data.date}:{index}" for index, item in enumerate(data.timeline)
            if (until := timeline_deferred_until(item, data.date, meta)) and until > now
        }
        future_anchors = []
        for index, item in enumerate(data.timeline):
            item_time = timeline_item_datetime(item, data.date)
            if (
                item_time is None
                or f"{data.date}:{index}" in deferred_ids
                or item_time <= now.replace(tzinfo=None)
                or item.execution_state != "planned"
            ):
                continue
            future_anchors.append(
                {
                    "anchor_id": f"{data.date}:{index}",
                    "time": item.time,
                    "activity": item.activity,
                    "status": item.status,
                }
            )
            if len(future_anchors) >= 4:
                break
        pending_replan = str(meta.get("schedule_replan_pending") or "").strip()
        near_term_text = str(meta.get("near_term_anchors") or "").strip()
        try:
            near_term = json.loads(near_term_text) if near_term_text else []
        except (TypeError, ValueError, json.JSONDecodeError):
            near_term = []
        if deferred_ids and isinstance(near_term, list):
            near_term = [
                item for item in near_term
                if isinstance(item, dict) and item.get("anchor_id") not in deferred_ids
            ]
        lifecycle_text = (
            "\n连续生活参数："
            f"\n- 日程基调 life_mode: {meta.get('life_mode') or '未知'}"
            f"\n- 生成睡眠倾向 sleep_mode: {meta.get('sleep_mode') or '未知'}"
            f"\n- sleep_debt: {meta.get('sleep_debt') or '0'}/10"
            f"\n- energy_carryover: {meta.get('energy_carryover') or '未知'}/100"
        )
        if rest_delay_hint:
            lifecycle_text += (
                f"\n- 临时休息安排：{rest_delay_hint}。"
                "状态判断应把用户明确的延后要求视为当前证据，不要只按原始钟点强行切换为睡眠。"
            )
        fixed = f"""更新当前角色此刻的身体和情绪状态。日程默认不变；只有新的状态、天气或已失败动作明确使后续安排不可行时，才提出局部重排。

【通用自主原则】
{CORE_AUTONOMY_RULES}

【通用状态行为原则】
{CORE_STATE_BEHAVIOR_RULES}

请只输出 JSON 对象，不要 Markdown，不要解释：
{{
  "energy": 0-100,
  "mood": "此刻心情底色",
  "mood_score": 0-100,
  "busyness": 0-100,
  "social": 0-100,
  "stress": 0-100,
  "focus": 0-100,
  "sleepiness": 0-100,
  "outgoing": 0-100,
  "emotional_stability": 0-100,
  "interaction_capacity": 0-100,
  "boredom": 0-100,
  "fishing": 0-100,
  "attention_openness": 0-100,
  "watch_state": "blackout|peek|skim_window|active_watch|engaged",
  "interrupt_level": "ordinary|medium|high",
  "interrupt_reason": "当前为什么只适合被这种等级的消息打断",
  "sleep": {{"quality": 0-100, "depth": "awake|light_rest|light_sleep|deep_sleep", "summary": "睡眠影响是否仍在"}},
  "physiological_rhythm": {{
    "energy_curve": "此刻到接下来一段时间的精力起伏",
    "body_condition": {BODY_CONDITION_JSON_EXAMPLE},
    "recovery_actions": ["自然恢复动作"],
    "social_battery": 0-100,
    "attention_state": "注意力/感官负荷状态",
    "optional_cycle": {{"enabled": "布尔值，是否存在可选周期", "label": "可选周期标签", "intensity": 0-100, "source": "依据来源"}},
    "summary": "一句话概括此刻生理节律"
  }},
  "summary": "一句话概括此刻状态",
  "source": "触发来源原样写入",
  "emotion_arc": {{"label": "此刻最主要的情绪脉络", "valence": -100到100, "arousal": 0-100, "intensity": 0-100, "stability": 0-100, "trigger": "触发点", "evidence": "依据", "influence": "会怎样轻微影响生活判断"}},
  "schedule_replan": {{"should_replan": false, "reason": "明确的新证据或空字符串", "replacements": [{{"replaces_anchor_id": "下方未来锚点的 anchor_id", "time": "HH:MM", "activity": "调整后的自然活动", "status": "短状态", "evidence": "本次触发依据"}}]}}
}}

要求：
{CORE_JSON_OUTPUT_RULES}
- 这是实时微调，不是重新生成一天。变化要自然、克制，不要大起大落。
- schedule_replan 默认 should_replan=false 且 replacements 为空。只有下方给出的待处理原因或本轮状态明确使未来锚点不可行时才填写；只能替换给出的未来锚点，不能新增、删除、改写已开始项目，也不能仅为了丰富日程而重排。
- 根据当前活动、时间、天气、触发信息和原状态推断。
- 时间轴是安排，时间经过不等于执行完成；只有执行状态 completed 且有执行证据，或本轮明确已发生事实，才能说某事已经做完。不要把未执行计划写成完成事实，也不要用本轮自行生成的 summary 证明计划完成。
- 体力低时 summary 可以体现“不太想出门/更想低强度互动”；社交意愿低时体现慢热和低负担。
- mood_score 表示当下心情正向程度，不等同于 emotional_stability；情绪稳定但低落、开心但容易波动都可以存在。
- stress 是主观压力感，busyness 是客观忙碌度；sleepiness 是实时困倦度，sleep.quality 是昨晚/近期睡眠质量。
- outgoing 表示是否愿意外出，interaction_capacity 表示当前场景下回应、接话、继续交流的意愿与余力。
- boredom 表示低刺激下想找点新鲜内容的倾向；fishing 表示持续低价值刺激后懒得看、懒得理、想退出的倾向。
- attention_openness 表示此刻愿意让外界消息进入主体注意力的开放度。
- watch_state 表示群聊观看姿态：blackout=基本不看，peek=偶尔瞥见，skim_window=扫读一小段，active_watch=持续关注，engaged=已经参与。
- interrupt_level 表示当前可打断等级：ordinary=普通消息也可自然进入注意，medium=熟悉用户/相关话题/异常热闹才进入，high=只有@、引用、提到我、高风险冲突或强相关事件才进入。
- watch_state、boredom、fishing、attention_openness 和 interrupt_level 是同一个主观注意力状态机；要根据体力、困倦、忙碌、社交意愿、当前活动和触发信息自主判断，不要套固定时间或固定文本规则。
- sleep.depth 是此刻睡眠/休息层级：awake=清醒，light_rest=浅休息但仍可能留意，light_sleep=浅睡眠且普通消息难进入，deep_sleep=深度睡眠只可能被强打断信号影响。它由能量、困意、当前活动、昨日睡眠债、时间线索和消息打断等级共同判断，不要由固定时间直接决定。
- 用户希望晚点休息时，结合当前对话与疲惫程度调整节奏；顺延结束只是再次评估的时间，不代表已经入睡。困倦可以和继续聊天并存，不能因为陪聊把体力、睡眠债或困倦直接清零。
- physiological_rhythm 是通用身体节律：包括精力曲线、身体状态、恢复动作、社交电量、注意力状态和可选周期字段。
- 结合下方“状态因果线索”、近期身体感受及实际活动评估当前阶段；用户关心、一次开心的聊天或复核时间到达，都不能直接清除生病感。
- 生病感减轻、持续或恢复都要与当前身体事实一致；明显影响未来安排时使用 schedule_replan 调低强度或改约，已执行的安排保持不变。
- 不要输出 meta.life_mode 或 meta.sleep_mode；它们属于今日生成的日程基调，不属于实时状态刷新。
- emotion_arc 只记录当前情绪脉络的结构化摘要；没有新触发时可以延续原状态并降低强度。
- 实时刷新只能更新此刻身体、情绪和未来日程可行性，不得推导或保存长期偏好、关系事实与生活事件。
"""
        rhythm_section = f"\n近期生理节律：\n{rhythm_context}" if rhythm_context else ""
        dynamic = f"""生活日程日期：{data.date or "未知"}
天气：{data.weather or "未知"}
当前活动：{activity_text}
当前活动执行状态：{curr_act.execution_state if curr_act else "未知"}；执行证据：{curr_act.execution_evidence if curr_act else "无"}
下一项安排：{next_text}
当前状态：{format_state_prompt(data.state)}
持续生活事实：{data.meta.get("continuous_life_context") or "尚未建立"}
近期状态变化：
{state_log_text}
近期情绪脉络：
{emotion_context or "暂无"}
{rhythm_section}
{lifecycle_text}
当前时间：{now.strftime("%Y-%m-%d %H:%M")}
触发来源：{source}
触发信息：{detail or "无"}
状态因果线索：{format_daily_variability_prompt(data.date, state=data.state)}
近期锚点细化：{json.dumps(near_term[:4] if isinstance(near_term, list) else [], ensure_ascii=False)}
可局部重排的未来锚点：{json.dumps(future_anchors, ensure_ascii=False)}
待处理重排原因：{pending_replan or "无"}"""
        return cache_friendly_prompt(fixed, dynamic)

    @staticmethod
    def _state_score(value: Any, default: int = 50) -> int:
        try:
            score = int(float(value))
        except (TypeError, ValueError):
            score = default
        return max(0, min(score, 100))

    @classmethod
    def _emotion_arc_from_state_payload(
        cls,
        result: dict,
        state: LifeState,
        *,
        date_str: str,
        now: datetime.datetime,
        source: str,
        detail: str = "",
    ) -> EmotionArcRecord | None:
        raw_arc = result.get("emotion_arc")
        payload = raw_arc if isinstance(raw_arc, dict) else {}
        label = str(payload.get("label") or state.mood or "").strip()
        evidence = str(payload.get("evidence") or state.summary or "").strip()
        trigger = str(payload.get("trigger") or detail or source or "").strip()
        influence = str(payload.get("influence") or "").strip()
        if not (label or evidence or trigger or influence):
            return None

        mood_score = cls._state_score(state.mood_score, 60)
        sleepiness = cls._state_score(state.sleepiness, 35)
        focus = cls._state_score(state.focus, 55)
        interaction = cls._state_score(state.interaction_capacity, 55)
        default_valence = max(-100, min((mood_score - 50) * 2, 100))
        default_arousal = max(0, min(round((sleepiness + focus) / 2), 100))
        default_intensity = max(
            10, min(round(abs(default_valence) * 0.45 + interaction * 0.25 + 20), 100)
        )
        expires_at = (now + datetime.timedelta(hours=18)).strftime("%Y-%m-%d %H:%M:%S")
        return EmotionArcRecord.from_value(
            {
                **payload,
                "scope": "",
                "date": date_str,
                "label": label,
                "valence": payload.get("valence", default_valence),
                "arousal": payload.get("arousal", default_arousal),
                "intensity": payload.get("intensity", default_intensity),
                "stability": payload.get("stability", state.emotional_stability),
                "trigger": trigger,
                "evidence": evidence,
                "influence": influence or state.interrupt_reason or state.summary,
                "expires_at": payload.get("expires_at") or expires_at,
                "source": source or "state",
            },
            date=date_str,
            source=source or "state",
        )

    @staticmethod
    def _format_emotion_arcs_for_state_prompt(arcs: list[Any]) -> str:
        lines = []
        for item in list(arcs or [])[:4]:
            label = str(getattr(item, "label", "") or "").strip()
            if not label:
                continue
            parts = [
                f"强度 {getattr(item, 'intensity', 0)}/100",
                f"正负向 {getattr(item, 'valence', 0)}",
                str(getattr(item, "evidence", "") or "").strip(),
                str(getattr(item, "influence", "") or "").strip(),
            ]
            lines.append(f"- {label}: " + "；".join(part for part in parts if part))
        return "\n".join(lines)

    @staticmethod
    def _format_rhythm_trend_for_state_prompt(trend: dict[str, Any]) -> str:
        if not isinstance(trend, dict):
            return ""
        summary = str(trend.get("summary") or "").strip()
        logs = trend.get("logs") if isinstance(trend.get("logs"), list) else []
        lines = [f"- {summary}"] if summary else []
        for item in logs[:3]:
            if not isinstance(item, dict):
                continue
            parts = [
                str(item.get("body_label") or item.get("summary") or "").strip(),
                f"身体负荷 {item.get('body_intensity')}/100"
                if item.get("body_intensity") is not None
                else "",
                f"社交电量 {item.get('social_battery')}/100"
                if item.get("social_battery") is not None
                else "",
                str(item.get("lifecycle_kind") or "").strip(),
            ]
            line = "；".join(part for part in parts if part)
            if line:
                lines.append(f"- {item.get('date') or '近期'}：{line}")
        return "\n".join(lines)

    def _state_refresh_recalled(self, source_event: Any) -> bool:
        return bool(
            source_event is not None
            and self._event_was_recalled(source_event, log_skip=True)
        )

    async def _state_refresh_prompt(
        self,
        data: DayRecord,
        now: datetime.datetime,
        source: str,
        detail: str,
    ) -> str:
        archive = self._archive()
        emotion_context = self._format_emotion_arcs_for_state_prompt(
            await archive.get_emotion_arcs(limit=4, scope="", include_global=True)
        )
        rhythm_context = self._format_rhythm_trend_for_state_prompt(
            await archive.get_physiological_rhythm_trend(days=7, limit=6)
        )
        return self._build_state_update_prompt(
            data,
            now,
            source,
            detail,
            emotion_context=emotion_context,
            rhythm_context=rhythm_context,
        )

    @staticmethod
    def _apply_state_continuity(
        data: DayRecord,
        state: LifeState,
        debt: float,
        delta: float,
        carryover: float,
    ) -> None:
        data.state = state
        data.meta["sleep_debt"] = f"{debt:.2f}".rstrip("0").rstrip(".")
        data.meta["sleep_debt_delta"] = f"{delta:.2f}".rstrip("0").rstrip(".")
        data.meta["energy_carryover"] = f"{carryover:.1f}".rstrip("0").rstrip(".")

    async def _persist_state_side_records(
        self,
        result: dict[str, Any],
        state: LifeState,
        spec: _StateRefreshSpec,
        previous_state: LifeState | None = None,
    ) -> None:
        tracked_fields = (
            "energy",
            "mood_score",
            "stress",
            "focus",
            "sleepiness",
            "social",
            "interaction_capacity",
        )
        significant_change = previous_state is None or any(
            abs(
                self._state_score(getattr(state, field_name, 50))
                - self._state_score(getattr(previous_state, field_name, 50))
            )
            >= 10
            for field_name in tracked_fields
        )
        if previous_state is not None:
            current_body = state.physiological_rhythm.body_condition
            previous_body = previous_state.physiological_rhythm.body_condition
            significant_change = significant_change or (
                abs(
                    self._state_score(current_body.intensity, 0)
                    - self._state_score(previous_body.intensity, 0)
                )
                >= 10
                or (
                    max(
                        self._state_score(current_body.intensity, 0),
                        self._state_score(previous_body.intensity, 0),
                    )
                    >= 20
                    and str(current_body.label or "").strip()
                    != str(previous_body.label or "").strip()
                )
                or current_body.kind != previous_body.kind
                or current_body.phase != previous_body.phase
                or current_body.burden_present != previous_body.burden_present
            )
        if not significant_change:
            return
        archive = self._archive()
        rhythm_log = physiological_rhythm_log_from_state(
            state, date=spec.date_str, source=spec.source
        )
        if rhythm_log:
            await archive.save_physiological_rhythm_log(rhythm_log)
        arc = self._emotion_arc_from_state_payload(
            result, state, date_str=spec.date_str, now=spec.now,
            source=spec.source, detail=spec.detail,
        )
        if arc:
            await archive.save_emotion_arc(arc)

    async def _commit_state_refresh(
        self,
        data: DayRecord,
        result: dict[str, Any],
        spec: _StateRefreshSpec,
    ) -> DayRecord:
        archive = self._archive()
        previous_date = (spec.now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        previous_day = await archive.get_day(previous_date)
        if self._state_refresh_recalled(spec.source_event):
            return data
        previous_state = data.state
        state = data.state
        revision = None
        committed = False
        raw_replan = result.get("schedule_replan")
        replan_payload = raw_replan if isinstance(raw_replan, dict) else {}
        replacements = replan_payload.get("replacements")

        def apply(latest: DayRecord, *, continuous: bool = False) -> bool:
            nonlocal state, previous_state, revision, committed
            if self._state_refresh_recalled(spec.source_event):
                return False
            previous_state = copy.deepcopy(latest.state)
            state = LifeState.from_value(normalize_state(
                result.get("state", result), now=spec.now, source=spec.source,
                previous=latest.state, max_score_delta=12,
            ))
            latest.state = state
            debt, delta, carryover = self._compute_sleep(previous_day, latest)
            self._apply_state_continuity(latest, state, debt, delta, carryover)
            if continuous:
                project_body(latest, ContinuousBody.from_value(
                    json.loads(latest.meta["continuous_body"]), latest
                ))
                if previous_state is not None:
                    state.sleep.depth = previous_state.sleep.depth
            if replan_payload.get("should_replan") is True and isinstance(replacements, list):
                revision = self._replan(latest, replacements, now=spec.now)
                if revision.status == "applied":
                    latest.meta.pop("schedule_replan_pending", None)
                    latest.meta["schedule_replan_reason"] = str(
                        replan_payload.get("reason") or revision.reason
                    ).strip()[:240]
            latest.state_log = [*latest.state_log, state_log_entry(state, spec.now)][-10:]
            committed = True
            return True

        if data.meta.get("continuous_body"):
            current = await archive.mutate_day(
                data.date, lambda latest: apply(latest, continuous=True)
            )
            if current is not None:
                data.apply_persisted(current)
            else:
                return data
        else:
            if not apply(data):
                return data
            await archive.save_day(data)
        if not committed or self._state_refresh_recalled(spec.source_event):
            return data
        await self._sync_world_facts(
            data, observed_at=spec.now.strftime("%Y-%m-%d %H:%M:%S"),
            source="state_refresh",
            source_id=f"state:{spec.date_str}:{spec.now.strftime('%H%M')}",
            evidence=state.summary,
        )
        if revision is not None:
            await archive.save_decision_trace({
                "trace_id": f"schedule_replan:{data.date}:{spec.now.strftime('%H%M')}",
                "scope": f"day:{data.date}",
                "stage": "committed" if revision.status == "applied" else "rejected",
                "reason_code": "state_schedule_replan",
                "decision": revision.status,
                "evidence": [
                    item for item in [
                        str(replan_payload.get("reason") or "").strip(),
                        *[str(item.get("evidence") or "").strip()
                          for item in replacements if isinstance(item, dict)],
                    ] if item
                ],
                "outcome": revision.reason,
            })
        await self._persist_state_side_records(result, state, spec, previous_state)
        await self._sync_kernel(data, now=spec.now)
        if self._state_refresh_recalled(spec.source_event):
            return data
        if spec.notify_page:
            await self._notify_status("state")
        return data

    async def refresh_state_for_day(
        self,
        date_str: str,
        data: DayRecord | None = None,
        now: datetime.datetime | None = None,
        source: str = "idle",
        detail: str = "",
        force: bool = False,
        source_event: Any = None,
        notify_page: bool = True,
    ) -> DayRecord | None:
        if self._state_refresh_recalled(source_event):
            return data or await self._archive().get_day(date_str)

        now = now or life_now()
        data = data or await self._archive().get_day(date_str)
        if not data:
            return None

        if not self._config().enabled:
            return data

        if not force and not state_is_stale(
            data.state,
            now,
            self._config().refresh_minutes,
        ):
            return data

        provider = await self._text_provider(self._config().provider)
        if not provider:
            return data

        session_id = f"daily_life_state_{uuid.uuid4().hex[:8]}"
        try:
            prompt = await self._state_refresh_prompt(data, now, source, detail)
            provider_id = self._config().provider
            text = await self._call_text_model(
                provider,
                prompt,
                session_id,
                empty_retries=0,
                primary_provider_id=provider_id,
            )
            result = extract_json_from_text(text)
            if not result:
                return data
            if self._state_refresh_recalled(source_event):
                return data
            return await self._commit_state_refresh(
                data,
                result,
                _StateRefreshSpec(
                    date_str=date_str,
                    now=now,
                    source=source,
                    detail=detail,
                    source_event=source_event,
                    notify_page=notify_page,
                ),
            )
        except DayRevisionConflict as exc:
            logger.debug(f"{LOG_PREFIX} 实时状态结果已过期，保留较新的生活状态：{exc}")
            return await self._archive().get_day(date_str)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 更新实时状态失败：{e}")
            return data
        finally:
            await self._close_text_session(session_id)


__all__ = ["StateRefreshService"]
