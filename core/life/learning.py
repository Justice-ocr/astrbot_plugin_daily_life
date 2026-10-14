"""将事前预期、执行观测和事后解释分开，供后续选择参考。"""

from __future__ import annotations

import math
from typing import Any

METRICS = ("energy", "fatigue", "sleep_pressure", "hunger", "thirst", "social_battery")


def expectation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    changes = {}
    raw = value.get("body_change")
    for key, amount in raw.items() if isinstance(raw, dict) else []:
        if key not in METRICS or isinstance(amount, bool):
            continue
        try:
            number = float(amount)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            changes[key] = round(max(-100, min(100, number)), 2)
    return {
        "reason": str(value.get("reason") or "").strip()[:240],
        "body_change": changes,
        "source": "before_action_prediction",
    }


def observe_outcome(world: dict, outcome: dict, run: dict | None) -> None:
    """只能在首次写入执行回执时记录，数值变化不等同于单一因果证明。"""
    if not run or run.get("action", {}).get("action_id") != outcome["action_id"]:
        return
    before = run.get("body_before") or {}
    after = world.get("body") or {}
    changes = {
        key: round(after[key] - before[key], 2)
        for key in METRICS
        if isinstance(before.get(key), (int, float))
        and isinstance(after.get(key), (int, float))
    }
    predicted = expectation(run.get("expectation"))
    outcome["experience"] = {
        "expectation": predicted,
        "observed_body_change": changes,
        "prediction_error": {
            key: round(changes[key] - value, 2)
            for key, value in predicted.get("body_change", {}).items()
            if key in changes
        },
        "body_before": {key: before.get(key) for key in (*METRICS, "discomfort")},
        "observation_gap": bool(run.get("observation_gap")),
        "source": "execution_interval",
    }
    # 有观测缺口的动作仅保留原始记录，不用于效果统计。
    if run.get("observation_gap") or outcome["observed_minutes"] <= 0:
        return
    bucket = f"{outcome['action_type']}:{run.get('rest_kind', 'break')}"
    ledger = world.setdefault("action_learning", {})
    item = ledger.setdefault(
        bucket,
        {"action_type": outcome["action_type"], "samples": [], "adjustments": []},
    )
    if any(row["action_id"] == outcome["action_id"] for row in item["samples"]):
        return
    item["samples"].append(
        {
            "action_id": outcome["action_id"],
            "at": outcome["at"],
            "status": outcome["status"],
            "minutes": outcome["observed_minutes"],
            **outcome["experience"],
        }
    )
    item["samples"] = item["samples"][-12:]
    item["updated_at"] = outcome["at"]


def learn_adjustment(world: dict, outcome: dict, raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    adjustment = str(raw.get("adjustment") or "").strip()[:300]
    if not adjustment or not outcome.get("experience"):
        return
    for item in world.get("action_learning", {}).values():
        if not any(row["action_id"] == outcome["action_id"] for row in item["samples"]):
            continue
        if any(row["action_id"] == outcome["action_id"] for row in item["adjustments"]):
            continue
        item["adjustments"].append(
            {
                "action_id": outcome["action_id"],
                "text": adjustment,
                "at": outcome["at"],
                "status": "tentative",
                "context": outcome["experience"]["body_before"],
            }
        )
        item["adjustments"] = item["adjustments"][-4:]


def learning_context(world: dict) -> str:
    items = sorted(
        world.get("action_learning", {}).values(),
        key=lambda row: row.get("updated_at", ""),
        reverse=True,
    )[:6]
    if not items:
        return ""
    lines = ["行动经验（执行区间变化，含其他影响；调整是待后续验证的做法）："]
    for item in items:
        samples = item["samples"]
        if not samples:
            continue
        last = samples[-1]
        lines.append(
            f"- {item['action_type']}：已观测 {len(samples)} 次；最近 "
            f"{last['minutes']} 分钟；身体变化 {last['observed_body_change']}；"
            f"预期差异 {last['prediction_error']}；"
            + "；".join(row["text"] for row in item["adjustments"][-2:])
        )
    return "\n".join(lines)
