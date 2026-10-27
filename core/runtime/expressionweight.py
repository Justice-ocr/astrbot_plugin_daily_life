"""表达经验的上下文权重；原始档案与用户反馈始终保留。"""

from __future__ import annotations

import datetime
import math

from ..clock import local_datetime, now as life_now


def expression_weight(profile, *, now: datetime.datetime | None = None) -> float:
    confidence = float(getattr(profile, "confidence", 1) or 0)
    if not math.isfinite(confidence):
        return 0.0
    updated = local_datetime(getattr(profile, "updated_at", ""))
    days = max(0, ((now or life_now()) - updated).total_seconds() / 86400) if updated else 0
    # 旧经验逐渐降低约束力，避免一次反馈永远固化口吻；不删除历史偏好。
    status = (getattr(profile, "learning", {}) or {}).get("status", "stable")
    strength = {"emerging": 0.35, "cooling": 0.0}.get(status, 1.0)
    return round(max(0, min(1, confidence)) * math.exp2(-days / 180) * strength, 3)
