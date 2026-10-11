"""共同经历、物品与纠正的结构化约束和统一事实展示。"""

from __future__ import annotations

from typing import Any

ACTIVITY_TRANSITIONS = {
    "planned": {"preparing", "active", "paused", "cancelled"},
    "preparing": {"active", "paused", "cancelled"},
    "active": {"paused", "completed", "cancelled"},
    "paused": {"preparing", "active", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
ACTIVITY_LABELS = {
    "planned": "已约定，尚未开始",
    "preparing": "准备中",
    "active": "进行中",
    "paused": "暂停，等待继续",
    "completed": "已结束",
    "cancelled": "已取消",
}
OBJECT_STATES = {"available", "in_use", "stored", "unavailable", "finished", "archived"}
OBJECT_LABELS = {
    "available": "可用",
    "in_use": "使用中",
    "stored": "已收纳",
    "unavailable": "暂不可用",
    "finished": "已完成",
    "archived": "已归档",
}


def activity_change(
    current: dict[str, Any], *, state: str, detail: str, progress: str = ""
) -> dict[str, Any]:
    previous = current.get("state", "planned")
    if state != previous and state not in ACTIVITY_TRANSITIONS.get(previous, set()):
        raise ValueError("共同活动状态已变化，请读取最新进度后再操作。")
    if previous in {"completed", "cancelled"}:
        raise ValueError("这次共同活动已经结束；新的活动需要新的约定。")
    if not detail.strip():
        raise ValueError("共同活动变化需要明确的本轮依据。")
    result = {**current, "state": state, "detail": detail.strip()[:600]}
    if progress.strip():
        result["progress"] = progress.strip()[:300]
    # 暂停没有默认结束时间；进度只由明确反馈推进。
    return result


def object_change(
    current: dict[str, Any],
    *,
    title: str = "",
    state: str = "",
    location: str = "",
    progress: str = "",
    content: str = "",
) -> dict[str, Any]:
    if state and state not in OBJECT_STATES:
        raise ValueError("物品状态无效。")
    result = dict(current)
    for field, value, limit in (
        ("title", title, 160),
        ("state", state, 30),
        ("location", location, 160),
        ("progress", progress, 300),
        ("content", content, 4000),
    ):
        if value.strip():
            result[field] = value.strip()[:limit]
    if not result.get("title"):
        raise ValueError("物品或作品需要明确名称。")
    result.setdefault("state", "available")
    return result


def living_context(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = ["持续生活记录（有来源与版本；只引用记录覆盖的事实）："]
    for item in items[:18]:
        kind = item.get("kind")
        prefix = f"{item.get('id')} v{item.get('revision', 0)}"
        if kind == "shared_activity":
            lines.append(
                f"- 共同活动 {prefix}：{item.get('title', '')}；"
                f"{ACTIVITY_LABELS.get(item.get('state'), '待确认')}；"
                f"进度 {item.get('progress') or '未确认'}；{item.get('detail', '')}"
            )
        elif kind == "object":
            lines.append(
                f"- 物品/作品 {prefix}：{item.get('title', '')}；"
                f"{OBJECT_LABELS.get(item.get('state'), '待确认')}；"
                f"位置 {item.get('location') or '未记录'}；"
                f"进度 {item.get('progress') or '未记录'}；"
                f"来源 {item.get('provenance', '')}；"
                f"{str(item.get('content') or '')[:240]}"
            )
        elif kind == "correction":
            lines.append(
                f"- 已确认纠正 {prefix}：{item.get('title', '')}；"
                f"应采用：{item.get('content', '')}；"
                f"原内容已撤下当前召回，历史外观和原始记录保留。"
            )
    return "\n".join(lines)
