"""统一展示事实来源与不确定性，不从措辞匹配推断真假。"""

from __future__ import annotations

import math
from typing import Any

LABELS = {
    "observed": "执行或观测记录",
    "reported": "对方明确陈述",
    "inferred": "推测，尚未证实",
    "uncertain": "不确定，需确认",
}


def knowledge_label(item: Any) -> str:
    get = (
        item.get
        if isinstance(item, dict)
        else lambda name, default=None: getattr(item, name, default)
    )
    provenance = get("provenance", {}) or {}
    provenance = provenance if isinstance(provenance, dict) else {}
    status = provenance.get("epistemic_status")
    if status not in LABELS:
        status = {
            "action_receipt": "observed",
            "life_state": "observed",
            "chat_message": "reported",
        }.get(get("source_type"), "uncertain")
    try:
        confidence = float(get("confidence", 0) or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1, max(0, confidence)) if math.isfinite(confidence) else 0.0
    source = get("source_id") or get("source_table") or get("source") or "来源未记录"
    return f"{LABELS[status]}；置信度 {confidence:.2f}；依据 {source}"


def temporal_context(items: list) -> str:
    lines = []
    for item in items[:16]:
        lines.append(
            f"- {item.subject}.{item.predicate}={item.object_value}；"
            f"{knowledge_label(item)}；记录于 {item.observed_at or item.valid_from}"
        )
    return (
        "有来源的认识（推测不构成事实或行动授权）：\n" + "\n".join(lines)
        if lines
        else ""
    )
