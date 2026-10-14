"""在生活事务内校验和结算物品；不推测未记录的库存与归属。"""

from __future__ import annotations

import math
from typing import Any

from .body import instant


def resource_properties(
    current: dict,
    *,
    owner: str = "",
    quantity: float | None = None,
    unit: str = "",
    available_at: str = "",
) -> dict:
    result = dict(current)
    if owner:
        if owner not in {"self", "user", "shared", "unknown"}:
            raise ValueError("物品归属无效。")
        result["owner"] = owner
    if quantity is not None:
        if (
            isinstance(quantity, bool)
            or not math.isfinite(float(quantity))
            or quantity < 0
        ):
            raise ValueError("物品数量必须是有限的非负数。")
        result["quantity"] = float(quantity)
    if unit:
        result["unit"] = unit.strip()[:30]
    if available_at:
        at = instant(available_at)
        if at is None:
            raise ValueError("物品可用时间无效。")
        result["available_at"] = at.isoformat()
    if result.get("quantity") is not None and not result.get("unit"):
        raise ValueError("有数量的物品需要明确单位。")
    return result


def uses(action: Any) -> list[dict]:
    raw = action.payload.get("resource_uses", [])
    if not isinstance(raw, list) or len(raw) > 8:
        raise ValueError("物品使用清单无效。")
    found, ids = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("物品使用参数无效。")
        key = str(row.get("object_id") or "")
        if not key or key in ids or row.get("mode") not in {"use", "consume"}:
            raise ValueError("物品使用编号重复或用途无效。")
        amount = row.get("quantity", 1)
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (float, int))
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise ValueError("物品使用数量必须是有限正数。")
        ids.add(key)
        found.append({**row, "quantity": float(amount)})
    return found


def reserve(archive: Any, action: Any, *, scope: str, location: str, now: Any) -> str:
    """先检查全部资源，再写入占用；调用者持有 SQLite 事务。"""
    try:
        rows = uses(action)
    except ValueError as exc:
        return str(exc)
    pending = []
    for row in rows:
        item = archive._life_entity_unlocked(row["object_id"])
        if (
            not item
            or item.get("kind") != "object"
            or item["scope"] not in {scope, "global"}
            or item.get("owner") != "self"
        ):
            return "所用物品没有明确属于角色的记录。"
        if item.get("revision") != row.get("revision"):
            return "所用物品已经变化，需要重新读取。"
        if item.get("state") not in {"available", "stored", "finished"} or item.get(
            "reserved_by"
        ):
            return "所用物品当前不可用或正在占用。"
        available = instant(item.get("available_at"))
        if available and available > now:
            return "所用物品尚未到可用时间。"
        if not item.get("location") or (
            item["location"] != "数字作品档案" and item["location"] != location
        ):
            return "所用物品不在当前地点，需先确认位置或移动。"
        if row["mode"] == "consume" or item.get("quantity") is not None:
            if (
                item.get("quantity") is None
                or item.get("unit") != row.get("unit")
                or item["quantity"] < row["quantity"]
            ):
                return "物品数量不足、未知或单位不一致。"
        pending.append((item, row))
    for item, row in pending:
        archive._save_life_entity_unlocked(
            {
                **item,
                "state": "in_use",
                "reserved_by": action.action_id,
                "reserved_state": item["state"],
                "reserved_use": row,
                "reserved_location": item.get("location"),
                "reserved_available_at": item.get("available_at"),
            },
            evidence_id=f"reserve:{action.action_id}:{item['id']}",
            evidence="自主行动实际开始占用物品。",
            expected_revision=item["revision"],
        )
    return ""


def settle(archive: Any, action: Any, *, commit: bool, check_only: bool = False) -> str:
    """完成时扣减一次；取消只释放，重复结算不会再次扣减。"""
    try:
        rows = uses(action)
    except ValueError as exc:
        return str(exc)
    pending = []
    for row in rows:
        item = archive._life_entity_unlocked(row["object_id"])
        receipt = f"resource_done:{action.action_id}:{row['object_id']}"
        if not item or receipt in item.get("evidence_ids", []):
            if commit and not item:
                return "所用物品记录已移除，无法结算。"
            continue
        if item.get("reserved_by") != action.action_id:
            if commit:
                return "物品占用已变化，不能提交完成。"
            continue
        if commit and (item.get("state") != "in_use" or item.get("owner") != "self"):
            return "所用物品的可用状态已被修改。"
        if commit and (
            item.get("location") != item.get("reserved_location")
            or item.get("available_at") != item.get("reserved_available_at")
        ):
            return "所用物品的位置或可用时间已变化。"
        if (
            commit
            and (row["mode"] == "consume" or item.get("quantity") is not None)
            and (
                item.get("quantity") is None
                or item["quantity"] < row["quantity"]
                or item.get("unit") != row.get("unit")
            )
        ):
            return "所用物品的数量或单位已变化。"
        pending.append((item, row, receipt))
    if check_only:
        return ""
    for item, row, receipt in pending:
        if item.get("state") == "in_use":
            item["state"] = item.get("reserved_state", "available")
        if commit and row["mode"] == "consume":
            item["quantity"] = round(item["quantity"] - row["quantity"], 6)
            if item["quantity"] <= 0:
                item["state"] = "unavailable"
        for key in (
            "reserved_by",
            "reserved_state",
            "reserved_use",
            "reserved_location",
            "reserved_available_at",
        ):
            item.pop(key, None)
        archive._save_life_entity_unlocked(
            item,
            evidence_id=receipt,
            evidence="行动完成，物品使用已结算。"
            if commit
            else "行动终止，释放物品占用。",
        )
    return ""
