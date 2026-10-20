"""自主决策的紧凑投影；同一事实只发送一次，稳定资料排在实时信息之前。"""

from __future__ import annotations


def execution_context(world: dict) -> dict:
    kernel = world.get("kernel") or {}
    return {
        "attention": kernel.get("attention") or {},
        "reflection": kernel.get("reflection") or {},
        "recent_events": [
            {key: item.get(key) for key in ("id", "kind", "at", "summary", "evidence_ids")}
            for item in kernel.get("events", [])[-4:] if isinstance(item, dict)
        ],
        "body_observations": world.get("body_observations", [])[-4:],
    }


def skill_context(world: dict) -> dict:
    return {
        name: {key: value for key, value in skill.items() if key != "assessments"}
              | {"assessments": skill.get("assessments", [])[-2:]}
        for name, skill in (world.get("skills") or {}).items() if isinstance(skill, dict)
    }


def run_context(world: dict) -> dict | None:
    run = world.get("run")
    if not isinstance(run, dict):
        return None
    return {key: run[key] for key in (
        "status", "action", "started_at", "active_seconds", "changed_at", "resumed_at",
        "goal_id", "step_id", "rest_kind", "reason",
    ) if key in run}
