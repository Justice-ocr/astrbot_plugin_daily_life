"""模型上下文的无损整理与按事实分配预算，不参与生活语义裁定。"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from copy import deepcopy
from typing import Any


def compact_json(value: Any) -> str:
    """稳定字段顺序和紧凑编码，让相同资料保持相同前缀。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def token_estimate(text: str) -> int:
    """仅用于本地预算的保守估算；计费统计始终读取接口真实 usage。"""
    return math.ceil(sum(1 if ord(char) > 127 else 0.25 for char in text))


class EvidenceCache:
    """仅复用同一证据版本的成功结果；实例随所属服务释放。"""

    def __init__(self, limit: int = 64) -> None:
        self.limit = limit
        self._values: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> Any:
        if key not in self._values:
            return None
        self._values.move_to_end(key)
        return deepcopy(self._values[key])

    def put(self, key: str, value: Any) -> None:
        self._values[key] = deepcopy(value)
        self._values.move_to_end(key)
        while len(self._values) > self.limit:
            self._values.popitem(last=False)


def budget_context(text: str, *, tokens: int = 10_000) -> str:
    """保留当前事实与约束，按完整记录压缩可选历史，不截断事实句子。"""
    # 这里识别的是插件自己产生的结构标签，不从聊天关键词推断语义。
    optional = {
        "HiddenWorldMemory": 2, "HiddenLifeExperience": 3,
        "HiddenExternalMemory": 4, "HiddenGroupChatAwareness": 3,
    }
    optional.update(dict.fromkeys((
        "HiddenRelationships", "HiddenChatMemory", "HiddenPlaces", "HiddenEvents",
    ), 2))
    optional.update(dict.fromkeys((
        "HiddenLifeEpisodes", "HiddenBehaviorFeedback",
        "HiddenBehaviorPatterns", "HiddenBehaviorScenes", "HiddenEmotionArc",
        "HiddenPhysiologicalRhythm", "HiddenReplyEffects", "HiddenExpressionHabits",
        "HiddenExpressionReviews",
        "HiddenMidConversationSummary", "HiddenSceneTerms",
    ), 3))
    sections: list[tuple[str, list[str], int]] = []
    for line in text.splitlines(keepends=True):
        tag = line.lstrip().partition("]")[0].removeprefix("[")
        if (line.lstrip().startswith("[") and "]" in line
                or line.strip() in {"<daily_life>", "</daily_life>"}):
            sections.append((tag, [line], optional.get(tag, 0)))
        elif sections:
            sections[-1][1].append(line)
        else:
            sections.append(("", [line], 0))
    seen = set()
    unique = []
    for tag, lines, priority in sections:
        identity = (tag, "".join(lines))
        if priority and identity in seen:
            continue
        seen.add(identity)
        unique.append((tag, lines, priority))
    sections = unique
    cleaned = "".join("".join(lines) for _, lines, _ in sections)
    if token_estimate(cleaned) <= tokens:
        return cleaned
    protected_cost = sum(token_estimate("".join(lines)) for _, lines, priority in sections if priority == 0)
    remaining = max(0, tokens - protected_cost)
    output: dict[int, str] = {}
    # 相关性排序已由召回层完成，此处保留各部分排在前面的完整记录。
    for index in sorted(range(len(sections)), key=lambda i: sections[i][2]):
        _, lines, priority = sections[index]
        if priority == 0:
            output[index] = "".join(lines)
            continue
        allowance = min(remaining, max(256, tokens // 5))
        selected = [lines[0]]
        cost = token_estimate(lines[0])
        for line in lines[1:]:
            size = token_estimate(line)
            if cost + size <= allowance:
                selected.append(line)
                cost += size
        # 标签后可以直接带一条完整事实；空标题单独保留没有意义。
        inline = bool(lines[0].partition("]")[2].strip())
        if (len(selected) > 1 or inline) and cost <= min(allowance, remaining):
            output[index] = "".join(selected)
            remaining -= cost
    return "".join(output[i] for i in sorted(output))
