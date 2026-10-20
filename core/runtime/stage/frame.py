from __future__ import annotations

import datetime
from typing import Any

from astrbot.api import logger

from ...clock import now as life_now
from ...life.appearance import current_appearance_values
from ...life.condition import format_state_prompt
from ...life.tools import (
    build_time_context,
    format_timeline_to_text,
    get_current_timeline_status,
    timeline_item_datetime,
)
from ...life.wardrobe import (
    format_outfit_component_ledger,
    format_outfit_components,
    outfit_scene_category_label,
    project_outfit_components_for_scene,
    scene_category_for_place_kind,
)
from ...sources.history import SavedHistoryReader
from ..markers import LOG_PREFIX

from typing import Protocol


class StageFrameComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _event_session_id: Any
    _media_director_format_emotion_arcs: Any
    _media_director_format_recent_messages: Any
    _safe_event_attr: Any
    archive: Any
    config: Any
    context: Any
    format_structured_message_context: Any
    living_context_for_scope: Any
    structured_recent_history_messages: Any


class StageFrameComponent:
    def __init__(self, context: StageFrameComponentContext) -> None:
        self.context = context

    async def _media_director_current_day(
        self,
    ) -> tuple[Any | None, datetime.datetime, bool]:
        now = life_now()
        time_context = build_time_context(
            now, getattr(self.context.config, "schedule_time", "07:00")
        )
        target_date = time_context.business_date_text
        using_extended_night = time_context.extended_night
        day = (
            await self.context.archive.get_day(target_date)
            if getattr(self.context, "archive", None)
            else None
        )
        today = now.strftime("%Y-%m-%d")
        if day is None and target_date != today and getattr(self.context, "archive", None):
            day = await self.context.archive.get_day(today)
        if day and getattr(self.context, "archive", None):
            # 媒体上下文需要知道生命内核此刻实际执行到哪一步；时间轴本身只是计划，
            # 不能在计划节点尚未执行时把它当成已经发生的取景地点。
            try:
                world = await self.context.archive.get_continuous_life()
            except Exception:
                world = {}
            kernel = world.get("kernel") if isinstance(world, dict) else {}
            environment = kernel.get("environment") if isinstance(kernel, dict) else {}
            run = world.get("run") if isinstance(world, dict) else {}
            if not isinstance(run, dict) or str(run.get("date") or "") != str(day.date):
                run = {}
            actual_place = str(
                (getattr(day, "meta", {}) or {}).get("current_place")
                or (environment or {}).get("place")
                or ""
            ).strip()
            actual_action = run.get("action") if isinstance(run, dict) else {}
            if not isinstance(actual_action, dict):
                actual_action = {}
            day.meta = dict(getattr(day, "meta", {}) or {})
            day.meta["_media_director_actual_place"] = actual_place
            day.meta["_media_director_actual_action"] = {
                "status": str(run.get("status") or "").strip()
                if isinstance(run, dict)
                else "",
                "action_type": str(actual_action.get("action_type") or "").strip(),
                "target": str(actual_action.get("target") or "").strip(),
                "timeline_index": actual_action.get("timeline_index"),
            }
        return day, now, using_extended_night if target_date != today else False

    def _media_director_context_text(
        self, day: Any | None, now: datetime.datetime, using_extended_night: bool
    ) -> str:
        if not day:
            return f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}；暂无今日生活记录。"
        current_item, next_item = get_current_timeline_status(
            getattr(day, "timeline", []) or [],
            now,
            getattr(day, "date", None),
            meta=getattr(day, "meta", None),
        )
        state = getattr(day, "state", None)
        state_dict = (
            state.as_dict()
            if hasattr(state, "as_dict")
            else state
            if isinstance(state, dict)
            else {}
        )
        meta = getattr(day, "meta", {}) or {}
        actual_place = str(meta.get("_media_director_actual_place") or "").strip()
        if not actual_place:
            actual_place = str(meta.get("current_place") or "").strip()
        actual_action = meta.get("_media_director_actual_action")
        if not isinstance(actual_action, dict):
            actual_action = {}
        timeline_items = getattr(day, "timeline", []) or []
        actual_item = None
        actual_index = actual_action.get("timeline_index")
        if (
            actual_action.get("status") in {"running", "paused", "ready", "settling"}
            and isinstance(actual_index, int)
            and 0 <= actual_index < len(timeline_items)
        ):
            actual_item = timeline_items[actual_index]
        elif actual_place:
            for item in reversed(timeline_items):
                item_time = timeline_item_datetime(item, day.date, meta=day.meta)
                item_state = (
                    getattr(item, "execution_state", "")
                    if hasattr(item, "execution_state")
                    else item.get("execution_state", "")
                )
                if item_state not in {"active", "completed"} or item_time is None or item_time > now:
                    continue
                item_place = (
                    getattr(item, "place", "")
                    if hasattr(item, "place")
                    else item.get("place", "")
                )
                if str(item_place or "").strip() == actual_place:
                    actual_item = item
                    break
        weather = str(getattr(day, "weather", "") or "").strip()
        appearance = current_appearance_values(day)
        scene_category = ""
        if actual_item is not None:
            item_place = (
                getattr(actual_item, "place", "")
                if hasattr(actual_item, "place")
                else actual_item.get("place", "")
            )
            if not actual_place or str(item_place or "").strip() == actual_place:
                item_kind = (
                    getattr(actual_item, "place_kind", "")
                    if hasattr(actual_item, "place_kind")
                    else actual_item.get("place_kind", "")
                )
                scene_category = scene_category_for_place_kind(item_kind, default="")
            if not scene_category and actual_place == "家":
                scene_category = scene_category_for_place_kind("home", default="")
        projected_components = project_outfit_components_for_scene(
            meta.get("outfit_components"), scene_category
        )
        component_ledger = format_outfit_component_ledger(projected_components)
        visible_outfit = format_outfit_components(projected_components)
        if projected_components.get("main_clothing", {}).get("state") in {
            "worn",
            "carried",
            "removed",
            "staged",
        }:
            outfit = visible_outfit
        else:
            outfit = appearance["outfit"]

        parts = [
            f"生活记录范围：{'延续昨日生活记录' if using_extended_night else '今日生活记录'}",
            f"天气：{weather or '未知'}",
            f"当前实际场景：{outfit_scene_category_label(scene_category) if scene_category else '未知'}",
            f"当前穿搭：{outfit or meta.get('outfit_decision') or '未知'}",
            f"穿搭组成状态：{component_ledger}" if component_ledger else "",
            f"当前穿搭风格：{appearance['style'] or '未知'}",
            f"当前发型名称：{appearance['hair_style'] or '未知'}",
            f"当前发型细节：{appearance['hair'] or '未知'}",
            f"当前妆容名称：{appearance['makeup_style'] or '未知'}",
            f"当前妆容细节：{appearance['makeup'] or '未知'}",
            f"当前美甲名称：{appearance['nails_style'] or '未知'}",
            f"当前美甲细节：{appearance['nails'] or '未知'}",
            f"心情色彩：{meta.get('mood') or '未知'}",
            f"日程类型：{meta.get('theme') or meta.get('schedule_intent') or '未知'}",
            f"日程基调：{meta.get('life_mode') or '未知'}",
            "计划节点只作后续安排参考；没有实际地点或执行记录时，不把计划当成已经发生。",
        ]
        if actual_place:
            parts.append(f"当前实际地点：{actual_place}")
        if actual_action.get("status") in {"running", "paused", "ready", "settling"}:
            action_target = str(actual_action.get("target") or "").strip()
            if action_target:
                parts.append(f"正在执行的实际行动：{action_target}")
        if actual_item:
            current_item = actual_item
            place = (
                getattr(current_item, "place", "")
                if hasattr(current_item, "place")
                else current_item.get("place", "")
            )
            if place:
                parts.append(f"当前地点：{place}")
            activity = (
                getattr(current_item, "activity", "")
                if hasattr(current_item, "activity")
                else current_item.get("activity", "")
            )
            status = (
                getattr(current_item, "status", "")
                if hasattr(current_item, "status")
                else current_item.get("status", "")
            )
            parts.append(f"当前活动：{activity or '未知'}{f'（{status}）' if status else ''}")
        elif current_item:
            planned_activity = (
                getattr(current_item, "activity", "")
                if hasattr(current_item, "activity")
                else current_item.get("activity", "")
            )
            parts.append(f"当前活动：{planned_activity or '未知'}（计划，尚未确认已发生）")
        if next_item:
            next_time = timeline_item_datetime(next_item, day.date, meta=day.meta)
            time_text = next_time.strftime("%m-%d %H:%M") if next_time else "待定"
            activity = (
                getattr(next_item, "activity", "")
                if hasattr(next_item, "activity")
                else next_item.get("activity", "")
            )
            parts.append(f"下一段安排：{time_text} {activity}".strip())
        if state_dict:
            parts.append(f"身体与情绪状态：{format_state_prompt(state_dict)}")
        timeline = format_timeline_to_text(getattr(day, "timeline", []) or [], timeline_date=day.date, meta=day.meta)
        if timeline:
            parts.append(f"全天日程背景（连续性参考）：\n{timeline}")
        parts.append(f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}")
        return "\n".join(parts)

    @staticmethod
    def _media_director_compact_text(value: Any, limit: int = 100) -> str:
        text = " ".join(str(value or "").split())
        if len(text) > limit:
            return text[:limit].rstrip() + "..."
        return text

    @classmethod
    def _media_director_format_emotion_arcs(
        cls, arcs: list[Any], limit: int = 4
    ) -> str:
        lines: list[str] = []
        for item in list(arcs or [])[:limit]:
            label = cls._media_director_compact_text(getattr(item, "label", ""), 60)
            if not label:
                continue
            parts = [
                f"强度 {getattr(item, 'intensity', 0)}/100",
                f"正负向 {getattr(item, 'valence', 0)}",
            ]
            evidence = cls._media_director_compact_text(
                getattr(item, "evidence", ""), 100
            )
            influence = cls._media_director_compact_text(
                getattr(item, "influence", ""), 100
            )
            if evidence:
                parts.append(f"依据：{evidence}")
            if influence:
                parts.append(f"影响：{influence}")
            lines.append(f"- {label}：" + "；".join(parts))
        if not lines:
            return ""
        return "近期情绪脉络（短期状态参考）：\n" + "\n".join(lines)

    async def _media_director_emotion_context_text(
        self,
        day: Any | None = None,
        event: Any = None,
        limit: int = 4,
    ) -> str:
        getter = getattr(getattr(self.context, "archive", None), "get_emotion_arcs", None)
        if not callable(getter):
            return ""
        scope = ""
        resolver = getattr(self.context, "_event_session_id", None)
        if callable(resolver) and event is not None:
            try:
                scope = str(resolver(event) or "").strip()
            except Exception:
                scope = ""
        date = str(getattr(day, "date", "") or "").strip()
        try:
            arcs = await getter(
                limit=limit, scope=scope, date=date, include_global=True
            )
            if not arcs and date:
                arcs = await getter(limit=limit, scope=scope, include_global=True)
            return self.context._media_director_format_emotion_arcs(arcs, limit=limit)
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 读取媒体导演情绪脉络失败：{exc}")
            return ""

    def _media_director_current_user_message(self, event: Any) -> str:
        if not event:
            return ""
        getter = getattr(self.context, "_safe_event_attr", None)
        if callable(getter):
            return str(getter(event, "message_str") or "").strip()
        return str(getattr(event, "message_str", "") or "").strip()

    async def _media_director_recent_context_text(
        self, event: Any, limit: int = 8
    ) -> str:
        limit = max(1, int(limit or 8))
        resolver = getattr(self.context, "_event_session_id", None)
        scope = ""
        if callable(resolver):
            try:
                scope = str(resolver(event) or "").strip()
            except Exception:
                scope = ""
        if not scope:
            return "暂无可读取的最近对话片段。"
        structured_reader = getattr(self.context, "structured_recent_history_messages", None)
        messages = (
            structured_reader(scope, limit=limit) if callable(structured_reader) else []
        )
        if not messages:
            try:
                messages = await SavedHistoryReader(self.context.context, LOG_PREFIX).fetch(
                    scope,
                    max_count=limit,
                    hours=12,
                    prefer_conversation=True,
                )
            except Exception as exc:
                logger.debug(f"{LOG_PREFIX} 读取媒体生成最近对话片段失败：{exc}")
                messages = []
        current_message = self._media_director_current_user_message(event)
        if current_message and not any(
            str(item.get("role") or "").strip() == "user"
            and str(item.get("content") or "").strip() == current_message
            for item in messages[-4:]
        ):
            messages.append({"role": "user", "content": current_message, "name": ""})
        text_context = self.context._media_director_format_recent_messages(messages[-limit:])
        living_reader = getattr(self.context, "living_context_for_scope", None)
        if callable(living_reader):
            text_context += "\n\n" + await living_reader(scope)
        structured = self.context.format_structured_message_context(event, limit=8)
        if structured:
            return f"{text_context}\n\n结构化消息流（优先判断谁对谁说话、引用和@关系）：\n{structured}"
        return text_context

    @staticmethod
    def _media_director_message_label(message: dict[str, str]) -> str:
        role = str(message.get("role") or "").strip().lower()
        name = str(message.get("name") or "").strip()
        label = "我" if role == "assistant" else name or "对方"
        target = str(message.get("talking_to_name") or "").strip()
        target_id = str(message.get("talking_to_id") or "").strip()
        if role != "assistant" and target and target_id not in {"", "group", "private"}:
            label = f"{label} -> {target}"
        return label

    @staticmethod
    def _media_director_message_content(
        message: dict[str, str], limit: int = 160
    ) -> str:
        content = " ".join(str(message.get("content") or "").split())
        quote = " ".join(str(message.get("reply_to_content") or "").split())
        reply_sender = str(message.get("reply_to_sender_name") or "").strip()
        if quote:
            prefix = f"引用{reply_sender}: " if reply_sender else "引用: "
            content = (
                f"{content}（{prefix}{quote}）" if content else f"（{prefix}{quote}）"
            )
        if len(content) > limit:
            content = content[:limit].rstrip() + "..."
        return content

    @staticmethod
    def _media_director_format_recent_messages(messages: list[dict[str, str]]) -> str:
        lines: list[str] = []
        for message in messages:
            label = StageFrameComponent._media_director_message_label(message)
            content = StageFrameComponent._media_director_message_content(
                message, limit=160
            )
            if content:
                lines.append(f"- {label}: {content}")
        return "\n".join(lines) if lines else "暂无可读取的最近对话片段。"
