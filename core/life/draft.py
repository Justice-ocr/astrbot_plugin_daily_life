from __future__ import annotations

from ..models import WeekPlanRecord
from ..prompts import cache_friendly_prompt
from .covenant import build_daily_plan_contract
from .tools import format_text_list

from typing import Any, Protocol


class DailyDraftComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _build_contract_prompt: Any
    _contract_json_text: Any
    _coverage_contract: Any
    _timeline_prompt_memo_section: Any
    _timeline_prompt_persona_section: Any
    _timeline_prompt_repeat_section: Any
    _timeline_prompt_week_section: Any
    config: Any


class DailyDraftComponent:
    def __init__(self, context: DailyDraftComponentContext) -> None:
        self.context = context

    @staticmethod
    def _timeline_prompt_persona_section(persona: str) -> str:
        return f"## 👤 角色设定\n{persona}" if persona else "## 👤 角色设定\n无"

    @staticmethod
    def _timeline_prompt_week_section(
        week_plan: WeekPlanRecord | None,
        *,
        today_hint: str = "",
        today_suggested: str = "",
        schedule_intent: str = "",
    ) -> str:
        if not week_plan:
            return ""
        return (
            "## 📅 周计划参考"
            f"\n- 主题：{week_plan.theme or '常规周'}"
            f"\n- 目标：{format_text_list(week_plan.goals, default='无')}"
            f"\n- 今日提示：{today_hint or '按周主题安排'}"
            f"\n- 建议活动：{today_suggested or '无'}"
            "\n- 使用方式：作为生活连续性的软参考，不是必须照做的清单"
            f"\n- 日程倾向参考：{schedule_intent or '由生活决策决定'}"
        )

    def _timeline_prompt_chat_section(self, recent_chats: str) -> str:
        if not recent_chats or recent_chats == "无":
            return ""
        return f"## 💬 最近聊天参考\n{self.context.config.chat_prompt}\n{recent_chats}"

    @staticmethod
    def _timeline_prompt_memo_section(memo_str: str) -> str:
        if not memo_str:
            return ""
        return f"## 🔔 强制备忘录/用户指令\n以下是今天必须完成或加入日程的事项：\n{memo_str}"

    @staticmethod
    def _timeline_prompt_repeat_section(history_schedules_str: str) -> str:
        if not history_schedules_str:
            return ""
        return (
            "## 🚫 需要避免的重复内容\n"
            "以下只列出最近几天的日程骨架，用于识别重复；不要把它当作今天的素材池，"
            "今天应根据当前天气、状态、承诺和人物关系形成新的活动组合：\n"
            f"{history_schedules_str}"
        )

    def _timeline_prompt_fixed_contract(self, expected_coverage: str) -> str:
        return build_daily_plan_contract(
            self.context._build_contract_prompt(expected_coverage),
            self.context._contract_json_text(
                self.context._coverage_contract(expected_coverage)
            ).replace("\n", "\n  "),
            self.context.config.state_prompt,
            self.context.config.timeline_prompt,
            self.context.config.world_prompt,
        )

    def _timeline_prompt_dynamic_sections(
        self,
        *,
        date_str: str,
        period_cn: str,
        weather_section: str,
        constraint_section: str,
        inertia_section: str,
        previous_context: str,
        history_schedules_str: str,
        memo_str: str,
        persona: str,
        week_plan: WeekPlanRecord | None,
        today_hint: str,
        today_suggested: str,
        recent_chats: str,
        schedule_intent: str,
        world_context: str,
        lifecycle_context: str,
        autonomy_context: str,
        person_fact_context: str,
        replacement_context: str,
        current_time_text: str,
        temporal_context_text: str = "",
        calendar_context: str = "",
        season_context: str = "",
    ) -> list[str]:
        return [
            self.context._timeline_prompt_persona_section(persona),
            self.context._timeline_prompt_week_section(
                week_plan,
                today_hint=today_hint,
                today_suggested=today_suggested,
                schedule_intent=schedule_intent,
            ),
            f"目标日期：{date_str}",
            f"当前/目标时间线索：{period_cn}",
            f"当前/目标实际时间：{current_time_text}" if current_time_text else "",
            f"生活时间上下文：{temporal_context_text}" if temporal_context_text else "",
            f"日历：{calendar_context.strip()}" if calendar_context else "",
            f"季节：{season_context.strip()}" if season_context else "",
            weather_section.strip(),
            constraint_section.strip(),
            inertia_section.strip(),
            previous_context.strip(),
            self._timeline_prompt_chat_section(recent_chats),
            world_context.strip(),
            lifecycle_context.strip(),
            autonomy_context.strip(),
            person_fact_context.strip(),
            replacement_context.strip(),
            self.context._timeline_prompt_memo_section(memo_str),
            self.context._timeline_prompt_repeat_section(history_schedules_str),
        ]

    def _build_timeline_prompt(
        self,
        date_str: str,
        period_cn: str,
        weather_section: str,
        constraint_section: str,
        inertia_section: str,
        previous_context: str,
        history_schedules_str: str,
        memo_str: str,
        persona: str = "",
        week_plan: WeekPlanRecord | None = None,
        today_hint: str = "",
        today_suggested: str = "",
        recent_chats: str = "",
        schedule_intent: str = "",
        world_context: str = "",
        lifecycle_context: str = "",
        autonomy_context: str = "",
        person_fact_context: str = "",
        replacement_context: str = "",
        expected_coverage: str = "full_day",
        current_time_text: str = "",
        temporal_context_text: str = "",
        calendar_context: str = "",
        season_context: str = "",
    ) -> str:
        fixed = self._timeline_prompt_fixed_contract(expected_coverage)
        dynamic_sections = self._timeline_prompt_dynamic_sections(
            date_str=date_str,
            period_cn=period_cn,
            weather_section=weather_section,
            constraint_section=constraint_section,
            inertia_section=inertia_section,
            previous_context=previous_context,
            history_schedules_str=history_schedules_str,
            memo_str=memo_str,
            persona=persona,
            week_plan=week_plan,
            today_hint=today_hint,
            today_suggested=today_suggested,
            recent_chats=recent_chats,
            schedule_intent=schedule_intent,
            world_context=world_context,
            lifecycle_context=lifecycle_context,
            autonomy_context=autonomy_context,
            person_fact_context=person_fact_context,
            replacement_context=replacement_context,
            current_time_text=current_time_text,
            temporal_context_text=temporal_context_text,
            calendar_context=calendar_context,
            season_context=season_context,
        )
        dynamic = "\n\n".join(part for part in dynamic_sections if part)
        return cache_friendly_prompt(fixed, dynamic, dynamic_title="今日生活资料")


__all__ = ["DailyDraftComponent"]
