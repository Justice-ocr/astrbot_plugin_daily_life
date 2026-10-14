from __future__ import annotations

import copy
import datetime
import json
import uuid
from typing import Any

from ..archive.timeline import rebind_planned_actions
from ..models import DayRecord, LIFE_ACTION_TYPES, LifeActionIntent, TimelineItem
from .dressing import wardrobe_conditions
from .engine import _resolve_planned_outfit_catalog


EDIT_ACTION_RULES = """日程编辑同时交付 planned_actions 数组，没有需要执行结算的动作时返回 []。
每项用 edit_index 绑定本轮 timeline_edits 中 insert/replace 的序号（从 0 开始），不要猜合并后的 timeline_index。
换装、穿脱鞋或外层、拿放随身配饰必须有 change_outfit；其他需要实际状态变化的准备、饮食、移动等也应填写对应动作，不能只写活动文案。
每个节点只绑定一项动作；多阶段行为拆成独立节点。duration_minutes 是实际耗时，不是自动完成的倒计时；效果要等执行回执确认。
change_outfit 从本轮衣橱候选选 catalog_reference_ids，填写 scene_category 和 outfit_components；局部调整保留主体服装。没有可用衣物依据时不要虚构换装。
planned_actions 格式：[{"edit_index":0,"action_type":"rest|drink|meal|cook|order_food|purchase|move|travel|work|study|chore|exercise|groom|change_outfit|social|chat|photo|video","target":"具体目标","duration_minutes":5,"preconditions":[],"effects":[],"payload":{},"evidence":"准备这项动作的依据"}]。
只安排计划，不把动作写成已完成；媒体和共同活动仍须真实交付或参与证据。"""


async def itinerary_wardrobe_context(
    host: Any, date_str: str, now: datetime.datetime
) -> str:
    day = await host.archive.get_day(date_str) or DayRecord(date=date_str)
    conditions = wardrobe_conditions(
        day,
        now,
        residence=str(getattr(host.config.domains, "home_address", "") or ""),
    )
    catalog = await host._style_catalog_context(limit=14, conditions=conditions)
    return f"当前实际穿搭：{day.outfit or '尚未确认'}\n当前穿戴组成：{day.meta.get('outfit_components', '')}\n{catalog}"


async def compile_edited_actions(
    host: Any, result: dict, timeline: list[TimelineItem], *, date_str: str, source: str
) -> str:
    """把本轮编辑中的动作绑定到审计后的节点，校验衣橱引用；不执行动作。"""
    raw_actions = result.get("planned_actions")
    if not isinstance(raw_actions, list):
        return "缺少结构化 planned_actions，尚不能确认日程的执行安排"
    edits = result.get("timeline_edits") or []
    compiled = []
    used = set()
    for raw in raw_actions:
        if not isinstance(raw, dict):
            return "日程动作格式无效"
        edit_index = raw.get("edit_index")
        if type(edit_index) is not int or not 0 <= edit_index < len(edits):
            return "动作没有绑定本轮有效的编辑序号"
        edit = edits[edit_index]
        if edit.get("operation") not in {"insert", "replace"}:
            return "动作不能绑定已移除的节点"
        original = TimelineItem.from_value(edit.get("item"))
        # 地图审计可能调整时间和地点，活动主体应保持一致。
        matches = [
            i for i, item in enumerate(timeline) if item.activity == original.activity
        ]
        exact = [
            i
            for i in matches
            if timeline[i].time == original.time
            and (timeline[i].day_offset or 0) == (original.day_offset or 0)
        ]
        matches = exact or matches
        if len(matches) != 1 or matches[0] in used:
            return "动作对应节点不唯一或已绑定其他动作"
        index = matches[0]
        used.add(index)
        action = LifeActionIntent.from_value(raw)
        if (
            action.action_type not in LIFE_ACTION_TYPES
            or not action.target
            or action.duration_minutes <= 0
        ):
            return "动作缺少有效类型、目标或实际耗时"
        action.action_id = f"{source}:{date_str}:{uuid.uuid4().hex}"
        action.timeline_index = index
        action.source = source
        compiled.append(action.as_dict())
    current = await host.archive.get_day(date_str)
    payload = {
        "timeline": [item.as_dict() for item in timeline],
        "planned_actions": compiled,
        "outfit_components": current.meta.get("outfit_components", "")
        if current
        else "",
    }
    issue = await _resolve_planned_outfit_catalog(host, payload, manual_extra=False)
    if issue:
        return issue
    for action in compiled:
        item = timeline[action["timeline_index"]]
        item.duration_minutes = action["duration_minutes"]
        item.execution_state = "planned"
        item.execution_reason = "等待结合当前生活状态实际执行"
    result["_planned_life_actions"] = compiled
    return ""


def apply_edited_timeline(
    day: DayRecord, timeline: list[TimelineItem], decision: dict
) -> None:
    """日程和动作一起提交，保留未改动动作的编号，解除已替换动作的绑定。"""
    try:
        previous = json.loads(day.meta.get("planned_life_actions") or "[]")
    except (ValueError, TypeError):
        previous = []
    previous = (
        [item for item in previous if isinstance(item, dict)]
        if isinstance(previous, list)
        else []
    )
    unbound = {
        item.get("action_id") for item in previous if item.get("timeline_index") is None
    }
    rebind_planned_actions(day.meta, day.timeline, timeline)
    try:
        rebound = json.loads(day.meta.get("planned_life_actions") or "[]")
    except (ValueError, TypeError):
        rebound = []
    additions = copy.deepcopy(decision.get("_planned_life_actions") or [])
    replaced = {item["timeline_index"] for item in additions}
    kept = (
        [
            item
            for item in rebound
            if isinstance(item, dict)
            and item.get("timeline_index") not in replaced
            and (
                item.get("timeline_index") is not None
                or item.get("action_id") in unbound
            )
        ]
        if isinstance(rebound, list)
        else []
    )
    day.meta["planned_life_actions"] = json.dumps(
        kept + additions, ensure_ascii=False, separators=(",", ":")
    )
    day.timeline = timeline
