from __future__ import annotations

import datetime
from typing import Any

from ..expression import remember_expression_context

from ...config.options.basis import format_chat_style_prompt
from ...life.appearance import format_current_appearance_context
from ...life.calendar import format_calendar_context, format_season_context
from ...life.continuity import observation_pending, run_status_label
from ...life.chatview import chat_projection_from_day, read_execution
from ...life.restdelay import format_rest_delay_hint, is_rest_activity
from ...life.tools import (
    build_time_context,
    format_timeline_travel,
    get_current_timeline_status,
    timeline_execution_run,
    timeline_deferred_until,
    timeline_item_datetime,
)
from ...models import CommitmentRecord, DayRecord
from ...prompts import CORE_HIDDEN_CONTEXT_RULES

from typing import Protocol


class LayerTextComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _chat_style_enabled: Any
    _format_hidden_schedule_window: Any
    _get_time_status: Any
    _hidden_text: Any
    _semantic_segment_enabled: Any
    build_hidden_activity_hint: Any
    build_hidden_chat_style_hint: Any
    build_hidden_expression_channel_hint: Any
    config: Any


class LayerTextComponent:
    def __init__(self, context: LayerTextComponentContext) -> None:
        self.context = context

    def build_hidden_chat_style_hint(self) -> str:
        style = getattr(getattr(self.context, "config", None), "chat_style", None)
        checker = getattr(self.context, "_chat_style_enabled", None)
        if not style or (callable(checker) and not checker()):
            return ""
        if not callable(checker) and not bool(getattr(style, "enabled", False)):
            return ""

        if self.context._semantic_segment_enabled():
            style = getattr(getattr(self.context, "config", None), "chat_style", None)
            try:
                casual_limit = int(getattr(style, "casual_max_chars", 50) or 50)
            except (TypeError, ValueError):
                casual_limit = 0
            prompt = format_chat_style_prompt(
                getattr(style, "casual_short_prompt", "")
            )
            prompt_hint = f"\n- {self.context._hidden_text(prompt, 360)}" if prompt else ""
            length_hint = (
                f"\n- 日常闲聊参考长度约 {casual_limit} 字左右；只作节奏参考，"
                "普通闲聊整轮保持简短，不因分条而扩写；必要的解释和情绪支持说完整，不按字数删减。"
                if casual_limit > 0
                else ""
            )
            return (
                "\n[HiddenChatStyle] 回复先判断目标、表达动作和信息深度，再自然组织成完整表达；"
                "不要按固定字数截断，不要为了制造停顿拆开一个完整意思。"
                "一句只放一个主要意思，能自然停住就停住。"
                "客观事实先看依据；信息会变化或拿不准时先核验，不能确认就坦白边界。"
                + prompt_hint
                + length_hint
            )

        lines = ["\n[HiddenChatStyle] 默认聊天表达节奏:"]
        prompt = format_chat_style_prompt(
            getattr(style, "casual_short_prompt", "")
        )
        if prompt:
            lines.append(f"- {self.context._hidden_text(prompt, 360)}")

        casual_max = getattr(style, "casual_max_chars", None)
        try:
            casual_max_int = int(casual_max)
        except (TypeError, ValueError):
            casual_max_int = 0
        if casual_max_int > 0:
            lines.append(
                f"- 日常闲聊参考长度约 {casual_max_int} 字左右；只作节奏参考，"
                "普通闲聊整轮保持简短，不因分条而扩写；必要的解释和情绪支持说完整，不按字数删减。"
            )

        lines.append("- 轻松接话保持短气口；一句只放一个主要意思，能自然停住就停住。")
        lines.append(
            "- 客观事实先看依据；信息会变化或拿不准时先核验，不能确认就坦白边界。"
        )

        return "\n".join(lines)

    def _format_hidden_state_compact(self, state: Any) -> str:
        state_dict = (
            state.as_dict()
            if hasattr(state, "as_dict")
            else (state if isinstance(state, dict) else {})
        )
        if not state_dict:
            return ""

        # 数值、打断门槛和睡眠分层用于生活决策；聊天只读取已生成的感受。
        summary = " ".join(str(state_dict.get("summary") or "").split())
        mood = " ".join(str(state_dict.get("mood") or "").split())
        # 按完整感受保留；不裁去句尾的否定、条件或时间限定。
        summary = summary if len(summary) <= 360 else ""
        mood = mood if len(mood) <= 32 else ""
        lines = [f"- 此刻感受：{summary}"] if summary else []
        if mood:
            lines.append(f"- 心情：{mood}")
        return "[HiddenState]\n" + "\n".join(lines) if lines else ""

    def _format_timeline_item_compact(
        self,
        item: Any,
        limit: int = 44,
        *,
        previous_place: str = "",
        include_travel: bool = False,
        timeline_date: Any = None,
        meta: dict | None = None,
    ) -> str:
        time_text = self.context._hidden_text(getattr(item, "time", ""), 8)
        planned = timeline_item_datetime(item, timeline_date)
        if planned and (getattr(item, "day_offset", 0) or 0):
            time_text = planned.strftime("%m-%d %H:%M")
        deferred = timeline_deferred_until(item, timeline_date, meta)
        if deferred:
            time_text += f" → 顺延 {deferred:%m-%d %H:%M}"
        activity = self.context._hidden_text(getattr(item, "activity", ""), limit)
        status = self.context._hidden_text(getattr(item, "status", ""), 16)
        status_text = f" [{status}]" if status else ""
        text = f"{time_text} {activity}{status_text}".strip()
        if include_travel:
            travel = format_timeline_travel(
                item,
                previous_place=previous_place,
                include_provider=False,
            )
            if travel:
                text += f"；出行：{travel}"
        return text

    def _format_hidden_schedule_window(
        self, timeline: list[Any], now: datetime.datetime,
        *, timeline_date: Any = None, meta: dict | None = None,
    ) -> str:
        if not timeline:
            return ""

        date = timeline_date or now.date()
        current, _ = get_current_timeline_status(timeline, now, date, meta=meta)
        run = timeline_execution_run(meta, date)
        run_index = run["action"]["timeline_index"] if run else None
        run_item = timeline[run_index] if run_index is not None and run_index < len(timeline) else None
        timed = sorted(
            (time, index, item) for index, item in enumerate(timeline)
            if (time := timeline_item_datetime(item, date, meta=meta)) is not None
        )
        if not timed:
            return ""

        current_pos = -1
        for pos, (time, _, _) in enumerate(timed):
            if time <= now:
                current_pos = pos
            else:
                break

        lines = []
        index_text = "；".join(
            part
            for part in (
                self._format_timeline_item_compact(item, limit=24, timeline_date=date, meta=meta)
                for _, _, item in timed
            )
            if part
        )
        if index_text:
            lines.append(f"- 全天索引: {self.context._hidden_text(index_text, 420)}")

        for pos in range(max(0, current_pos - 1), min(len(timed), current_pos + 3)):
            item = timed[pos][2]
            execution_state = getattr(item, "execution_state", "planned")
            if execution_state == "completed":
                label = "已确认完成"
            elif execution_state in {"skipped", "cancelled", "expired"}:
                label = "已跳过或取消"
            elif item is run_item and observation_pending(run):
                label = "最近确认，当前进展待确认"
            elif item is run_item and run["status"] == "running":
                label = "正在进行"
            elif item is run_item and run["status"] == "paused":
                label = "已暂停"
            elif item is run_item:
                label = "待结算行动"
            elif item is current:
                label = "当前计划"
            else:
                label = "已过计划" if timed[pos][0] <= now else "接下来"
            previous_item = timed[pos - 1][2] if pos > 0 else None
            previous_place = (
                str(getattr(previous_item, "place", "") or "").strip()
                if previous_item is not None
                else ""
            )
            compact = self._format_timeline_item_compact(
                item,
                previous_place=previous_place,
                include_travel=True,
                timeline_date=date,
                meta=meta,
            )
            current_marker = "（已记录开始）" if item is run_item and run["status"] == "running" else ""
            lines.append(f"- {label}: {compact}{current_marker}")

        return (
            "[HiddenScheduleWindow]\n"
            "普通聊天只参考当前窗口；用户明确询问全天安排、时间冲突或邀约时，再按全天索引自然回答。\n"
            "当前计划、已过计划和全天索引都不是执行证据；只有已确认完成的节点可当作发生过。小动作可以依当前场景自然展开，不能照未来文案声称已睡着、已换衣或已经和对方做过某事。\n"
            + "\n".join(lines)
        )

    def build_hidden_activity_hint(
        self,
        data: DayRecord,
        now: datetime.datetime,
        using_extended_night: bool,
    ) -> tuple[str, str, str]:
        time_context = build_time_context(
            now, getattr(self.context.config, "schedule_time", "07:00")
        )
        period_cn = time_context.period_cn
        if using_extended_night:
            meta = data.meta or {}
            life_mode = meta.get("life_mode", "")
            sleep_mode = meta.get("sleep_mode", "")
            delay_hint = format_rest_delay_hint(data, now)
            if life_mode in {
                "awake",
                "late_night",
                "all_nighter",
                "mixed",
            } or sleep_mode in {"late_night", "all_nighter"}:
                activity = (
                    f"🌙 今日生成基调: {life_mode or sleep_mode}，当前是否清醒仍按实时状态和时间轴判断"
                )
                if delay_hint:
                    activity += f"；{delay_hint}"
                return (
                    f"深夜/凌晨，日程基调 {life_mode or sleep_mode}",
                    activity,
                    period_cn,
                )
            activity = (
                "💤 今日生成基调偏休息/低活动，结合实时状态与时间线自然判断是否清醒、困倦或已休息"
            )
            if delay_hint:
                activity += f"；{delay_hint}"
            return (
                f"深夜/凌晨，日程基调 {life_mode or sleep_mode or '延续昨日状态'}",
                activity,
                period_cn,
            )

        status_desc = self.context._get_time_status(now)
        curr_act, next_act = get_current_timeline_status(data.timeline, now, data.date, meta=data.meta)
        run = timeline_execution_run(data.meta, data.date)
        run_index = run["action"]["timeline_index"] if run else None
        run_act = data.timeline[run_index] if run_index is not None and run_index < len(data.timeline) else None
        next_time = timeline_item_datetime(next_act, data.date, meta=data.meta)
        next_hint = f" | 🔜 待办: {next_time:%m-%d %H:%M} {next_act.activity}" if next_act and next_time else ""
        if run_act is not None and observation_pending(run):
            activity = f"最近确认的行动: {run_act.activity}；当前进展待确认"
        elif run_act is not None and run["status"] == "running":
            if is_rest_activity(run_act):
                activity = f"当前休息进程: {run_act.activity}；是否已入睡应结合实时状态和最新对话，不能仅凭日程断言"
            else:
                activity = f"📍 正在: {run_act.activity} (状态: {run_act.status or '平和'})"
        elif run_act is not None and run["status"] == "paused":
            activity = f"当前行动已暂停: {run_act.activity}"
        elif run_act is not None:
            activity = f"当前行动待结算: {run_act.activity}；结果尚未确认"
        elif curr_act:
            if is_rest_activity(curr_act):
                activity = f"当前休息计划: {curr_act.activity}；是否已入睡应结合实时状态和最新对话，不能按钟点断言"
            else:
                activity = f"当前计划: {curr_act.activity}；尚无执行证据"
        else:
            activity = "⏳ 碎片时间 (无特定安排)"
        activity += next_hint
        delay_hint = format_rest_delay_hint(data, now)
        if delay_hint:
            activity += f" | 🕒 {delay_hint}"
        return status_desc, activity, period_cn

    def build_hidden_life_context(
        self,
        data: DayRecord,
        now: datetime.datetime,
        using_extended_night: bool,
        world_context: str = "",
        group_awareness_context: str = "",
        commitments: list[CommitmentRecord] | None = None,
        experience_context: str = "",
        memos_context: str = "",
        structured: str = "",
        recent_video: str = "",
        expression_event: Any = None,
        include_rules: bool = True,
        detail_context: str = "",
    ) -> str:
        meta = data.meta or {}
        residence_context_stale = (
            str(meta.get("residence_context_stale") or "").strip().lower() == "true"
        )
        if residence_context_stale:
            period_cn = build_time_context(
                now, getattr(self.context.config, "schedule_time", "07:00")
            ).period_cn
            status_desc = "居住地已变化，当前生活状态等待新记录确认"
            activity = "当前地点、穿搭、天气和时间轴暂不引用旧记录"
        else:
            status_desc, activity, period_cn = self.context.build_hidden_activity_hint(
                data,
                now,
                using_extended_night,
            )
            execution = read_execution(data)
            if execution.get("status") in {"running", "paused", "ready", "settling"}:
                target = (execution.get("action") or {}).get("target")
                if target:
                    activity = f"{target}（{run_status_label(execution)}）"
        parts = ["\n\n<daily_life>"]
        if include_rules:
            parts.append("\n[UseRule] 以下内容是角色日常生活背景。按当前话题自然引用有依据的细节，"
                         "无需逐项汇报，也不要把未来计划说成已经发生。")
            parts.append(f"\n[HiddenContextRules] {CORE_HIDDEN_CONTEXT_RULES}")
        style_hint = self.context.build_hidden_chat_style_hint() if include_rules else ""
        if style_hint:
            parts.append(style_hint)

        if residence_context_stale:
            parts.append(
                "\n[HiddenResidenceRefresh] 居住地刚刚变化，旧记录仍作为历史保留，"
                "但不得把其中的地点、天气、穿搭、心情、状态或日程视为当前事实；"
                "新生活记录完成前，对这些问题只能自然表示暂未确定。"
            )
        else:
            continuous_context = chat_projection_from_day(data)
            if continuous_context:
                parts.append(f"\n[HiddenContinuousLife]\n{continuous_context}")
            appearance = format_current_appearance_context(data)
            if appearance:
                # 当前衣着只保留一条事实，妆发、组成账本等随话题召回。
                outfit = appearance.splitlines()[0]
                parts.append(
                    f"\n[HiddenAppearanceHint]\n{outfit}"
                )
            if detail_context:
                parts.append(f"\n[HiddenRelevantLife]\n{detail_context}")

            delay_hint = format_rest_delay_hint(data, now)
            if delay_hint:
                parts.append(
                    "\n[HiddenRestDelay] "
                    + delay_hint
                    + "；这是当前对话形成的临时安排，不能把原定节点当成已经发生。"
                )

        lines = [
            f"- {item.content}（{item.owner}；{' '.join(filter(None, (item.trigger_date, item.trigger_time, item.time_window)))}；尚未完成）"
            for item in (commitments or [])[:5]
            if getattr(item, "content", "") and getattr(item, "status", "active") == "active"
        ]
        if lines:
            parts.append(
                "\n[HiddenCommitmentHint] 未完成承诺/约定，仅在用户问起、涉及计划或自然续聊时参考:\n"
                + "\n".join(lines)
            )

        if world_context:
            parts.append(
                "\n[HiddenWorldMemory] 关系、地点与事件记忆，按话题相关性自然引用，旧事不可当作当前事实:\n"
                f"{world_context}"
            )
        if experience_context:
            parts.append(
                "\n[HiddenLifeExperience] 生活片段、关注目标、行为反馈、语言和记忆边界，仅用于长期一致性与智能判断；"
                "禁止直接暴露为系统字段或后台记录:\n"
                f"{experience_context}"
            )
        if memos_context:
            parts.append(
                "\n[HiddenExternalMemory] MemOS 外部长期记忆参考，只用于补足长期事实和偏好；"
                "若与当前人设线索或本插件已校准记忆冲突，以人设线索和本插件当前结构化记忆为准:\n"
                f"{memos_context}"
            )

        parts.append(f"\n[HiddenActivityHint] {activity}")
        parts.append(f"\n[HiddenTime] {now.strftime('%Y-%m-%d %H:%M')} ({period_cn})")
        state_context = ""
        if not residence_context_stale:
            state_context = self._format_hidden_state_compact(data.state)
            if state_context:
                parts.append(f"\n{state_context}")

        if group_awareness_context:
            parts.append(
                "\n[HiddenGroupChatAwareness] 最近群聊感知、消息留意和动作裁定，仅用于判断是否自然参与、是否需要观察或深度分析；"
                "禁止把分数、标签或内心旁白直接说给用户:\n"
                f"{group_awareness_context}"
            )
        if recent_video:
            parts.append(
                "\n[HiddenRecentVideoUnderstanding] 近期真实视频理解结果，优先级高于生活背景和长期记忆；"
                "用户追问视频内容、画面、动作或引用视频时优先参考，不确定处要自然说明:\n"
                f"{recent_video}"
            )
        if structured:
            parts.append(
                "\n[HiddenStructuredConversation] 最近真实消息结构，优先级高于长期记忆；"
                "用于判断群聊里谁在对谁说话、是否引用或@了我，避免把别人之间的对话误当成问我:\n"
                f"{structured}"
            )

        remember_expression_context(
            expression_event,
            time=now.isoformat(timespec="minutes"),
            activity=activity,
            state=state_context,
        )
        parts.append("\n</daily_life>")
        channel_hint = self.context.build_hidden_expression_channel_hint(expression_event)
        if channel_hint:
            parts.append(channel_hint)
        return "".join(parts)

    def build_missing_life_context(
        self,
        now: datetime.datetime,
        target_date_str: str,
        using_extended_night: bool,
        event: Any = None,
        memos_context: str = "",
        recent_video: str = "",
        include_rules: bool = True,
    ) -> str:
        period_cn = build_time_context(
            now, getattr(self.context.config, "schedule_time", "07:00")
        ).period_cn
        date_hint = "凌晨延续时段" if using_extended_night else "当前日期"
        external = (
            "\n[HiddenExternalMemory] MemOS 外部长期记忆参考，只用于补足长期事实和偏好；"
            "若与当前人设线索或本插件已校准记忆冲突，以人设线索和本插件当前结构化记忆为准:\n"
            f"{memos_context}"
            if memos_context
            else ""
        )
        video_context = (
            "\n[HiddenRecentVideoUnderstanding] 近期真实视频理解结果，优先级高于生活背景和长期记忆；"
            "用户追问视频内容、画面、动作或引用视频时优先参考，不确定处要自然说明:\n"
            f"{recent_video}"
            if recent_video
            else ""
        )
        rules_context = (
            f"\n[HiddenContextRules] {CORE_HIDDEN_CONTEXT_RULES}"
            f"{self.context.build_hidden_chat_style_hint()}"
            if include_rules else ""
        )
        remember_expression_context(
            event,
            time=now.isoformat(timespec="minutes"),
            activity="暂无已确认的生活状态与活动，不推断此刻是否入睡或忙碌。",
            state="",
        )
        return (
            "\n\n<daily_life>"
            "\n[UseRule] 当前还没有可用的日常生活记录；这只是一条防止编造的隐藏约束，不是聊天话题。"
            "\n[AntiFabricationRule] 在确认记录形成前，禁止编造今天正在做什么、穿什么、在哪里、天气如何、睡眠如何或接下来有什么安排；"
            "如果用户明确询问这些内容，只能用角色口吻自然表示今天的安排还没整理清楚或暂时不确定，不要提及后台、系统或记录生成。"
            "普通闲聊时不要主动提及这段缺失。"
            f"{rules_context}"
            f"\n[HiddenScheduleUnavailable] {date_hint} {target_date_str} 暂无已确认的日程、穿搭、地点、天气、生活状态或时间轴。"
            f"{external}"
            f"{video_context}"
            f"\n[HiddenTime] {now.strftime('%Y-%m-%d %H:%M')} ({period_cn})"
            f"\n[HiddenCalendar] {format_calendar_context(now)}"
            f"\n[HiddenSeason] {format_season_context(now)}"
            "\n</daily_life>"
            f"{self.context.build_hidden_expression_channel_hint(event)}"
        )
