from __future__ import annotations

import asyncio
import hashlib
import math
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_MODEL_TASK: ContextVar[tuple[str, str]] = ContextVar("daily_life_model_task", default=("", ""))


@contextmanager
def model_task(purpose: str = "", *, stage: str = ""):
    previous = _MODEL_TASK.get()
    token = _MODEL_TASK.set((purpose or previous[0], stage or previous[1]))
    try:
        yield
    finally:
        _MODEL_TASK.reset(token)


def model_task_identity(session_id: str) -> tuple[str, str]:
    purpose, stage = _MODEL_TASK.get()
    if not purpose:
        # 只使用已知业务前缀，不能把会话、用户编号或随机任务号记入统计维度。
        for name in ("memory_target_calibration", "memory_calibration", "memory_batch",
                     "wardrobe", "execution", "outfit", "state", "preference",
                     "proactive", "idle", "reaction", "timeline", "daily", "weekly"):
            if str(session_id).startswith("daily_life_" + name):
                purpose = name
                break
    return purpose or "other_internal", stage or "request"


def output_usage(response: Any) -> int | None:
    raw = _field(response, "raw_completion") or response
    usage = _field(raw, "usage") or _field(raw, "usage_metadata") or _field(raw, "usageMetadata")
    for name in ("completion_tokens", "output_tokens", "candidates_token_count", "candidatesTokenCount"):
        value = _count(_field(usage, name))
        if value is not None:
            return value
    return _count(_field(_field(response, "usage"), "output"))


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def model_request_identity(provider: Any, provider_id: str = "") -> str:
    """在发起请求前确定统计身份，成功与失败不会因返回别名而拆组。"""
    try:
        meta = provider.meta()
    except (AttributeError, TypeError, ValueError, KeyError):
        meta = None
    provider_id = str(_field(meta, "id") or provider_id or "").strip()
    model = str(_field(meta, "model") or getattr(provider, "model_name", "") or "").strip()
    return " / ".join(part for part in (provider_id, model) if part)


def cache_prefix_version(prompt: object, *, has_dynamic_section: bool = False) -> str:
    """只保留固定前缀的短摘要，避免保存原文或把动态内容当作版本。"""
    text = str(prompt or "")
    if has_dynamic_section:
        fixed, separator, _ = text.partition("\n\n【")
        if not separator:
            return ""
        text = fixed
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] if text else ""


def cache_usage(response: Any) -> dict[str, int | None]:
    """读取真实用量；接口未报告命中数时保持未知，不伪装成零命中。"""
    raw = _field(response, "raw_completion") or response
    usage = _field(raw, "usage") or _field(raw, "usage_metadata") or _field(raw, "usageMetadata")
    total = None
    input_field = ""
    cached = None
    for name in ("prompt_tokens", "input_tokens", "prompt_token_count", "promptTokenCount"):
        total = _count(_field(usage, name))
        if total is not None:
            input_field = name
            break
    for name in ("prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_content_token_count", "cachedContentTokenCount"):
        cached = _count(_field(usage, name))
        if cached is not None:
            break
    if cached is None:
        details = _field(usage, "prompt_tokens_details") or _field(usage, "input_tokens_details")
        cached = _count(_field(details, "cached_tokens"))
    if input_field == "input_tokens" and _field(usage, "cache_read_input_tokens") is not None and total is not None:
        # 这类 API 的 input_tokens 不包含缓存读取和缓存写入。
        total += (cached or 0) + (_count(_field(usage, "cache_creation_input_tokens")) or 0)
    normalized = _field(response, "usage")
    other = _count(_field(normalized, "input_other"))
    normalized_cached = _count(_field(normalized, "input_cached"))
    if total is None and other is not None and normalized_cached is not None:
        total = other + normalized_cached
    if cached is None and normalized_cached is not None and normalized_cached > 0:
        cached = normalized_cached
    if cached is not None and total is not None:
        cached = min(cached, total)
    return {"input_tokens": total, "cached_tokens": cached}


class ModelCacheMetrics:
    """仅保留有上限的用量聚合，不保存提示词、消息、凭据或会话编号。"""

    def __init__(self, limit: int = 32):
        self.limit = max(1, limit)
        self.entries: OrderedDict[tuple[str, str, str], dict[str, int]] = OrderedDict()
        self.durations: dict[tuple[str, str, str], deque] = {}
        self.loop_delays: deque = deque(maxlen=600)
        self.tasks: OrderedDict[tuple[str, str], dict[str, int]] = OrderedDict()

    def record(self, response: Any, *, kind: str, model: str = "", elapsed_ms: float | None = None,
               failed: bool = False, cancelled: bool = False,
               prefix_version: str = "", purpose: str = "", stage: str = "request",
               attempt: int = 0) -> dict[str, int | None]:
        usage = cache_usage(response)
        output = output_usage(response)
        if not model:
            raw = _field(response, "raw_completion") or response
            model = str(_field(raw, "model") or "当前模型")
        role = _field(response, "role")
        request_failed = failed or bool(_field(response, "is_error")) or getattr(role, "value", role) == "err"
        task_key = (purpose or kind, model[:120])
        task = self.tasks.setdefault(task_key, {
            "requests": 0, "failed_requests": 0, "retry_requests": 0,
            "repair_requests": 0, "input_tokens": 0, "cached_tokens": 0,
            "output_tokens": 0, "input_reported_requests": 0,
            "cached_reported_requests": 0, "output_reported_requests": 0,
        })
        self.tasks.move_to_end(task_key)
        while len(self.tasks) > self.limit:
            self.tasks.popitem(last=False)
        task["requests"] += 1
        task["failed_requests"] += int(request_failed)
        task["retry_requests"] += int(attempt > 0)
        task["repair_requests"] += int(stage == "json_repair")
        if usage["input_tokens"] is not None:
            task["input_reported_requests"] += 1
            task["input_tokens"] += usage["input_tokens"]
        if usage["cached_tokens"] is not None:
            task["cached_reported_requests"] += 1
            task["cached_tokens"] += usage["cached_tokens"]
        if output is not None:
            task["output_reported_requests"] += 1
            task["output_tokens"] += output
        key = (
            str(kind or "internal")[:20], str(model or "当前模型")[:120],
            str(prefix_version or "")[:12],
        )
        item = self.entries.setdefault(key, {
            "requests": 0, "reported_requests": 0, "unreported_requests": 0,
            "input_tokens": 0, "cached_tokens": 0, "requests_with_hits": 0,
            "failed_requests": 0, "cancelled_requests": 0,
        })
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            old_key, _ = self.entries.popitem(last=False)
            self.durations.pop(old_key, None)
        item["requests"] += 1
        item["cancelled_requests"] += int(cancelled)
        if elapsed_ms is not None and math.isfinite(elapsed_ms) and elapsed_ms >= 0:
            self.durations.setdefault(key, deque(maxlen=128)).append(elapsed_ms)
        role = _field(response, "role")
        role = getattr(role, "value", role)
        if failed or bool(_field(response, "is_error")) or role == "err":
            item["failed_requests"] += 1
            return usage
        if usage["input_tokens"] is not None and usage["cached_tokens"] is not None:
            item["reported_requests"] += 1
            item["input_tokens"] += usage["input_tokens"]
            item["cached_tokens"] += usage["cached_tokens"]
            item["requests_with_hits"] += int(usage["cached_tokens"] > 0)
        else:
            item["unreported_requests"] += 1
        return usage

    def record_loop_delay(self, milliseconds: float) -> None:
        if math.isfinite(milliseconds):
            self.loop_delays.append(max(0, milliseconds))

    @staticmethod
    def distribution(values) -> dict:
        values = sorted(values)
        if not values:
            return {"samples": 0, "mean_ms": None, "p95_ms": None, "max_ms": None}
        return {"samples": len(values), "mean_ms": round(sum(values) / len(values), 2),
                "p95_ms": round(values[math.ceil(len(values) * 0.95) - 1], 2),
                "max_ms": round(values[-1], 2)}

    def snapshot(self) -> dict[str, Any]:
        rows = []
        for (kind, model, prefix_version), values in self.entries.items():
            total = values["input_tokens"]
            rows.append({
                "kind": kind, "model": model, "prefix_version": prefix_version, **values,
                "cached_token_ratio": round(values["cached_tokens"] / total, 4) if total else None,
                "latency": self.distribution(self.durations.get((kind, model, prefix_version), [])),
            })
        return {"since": "当前插件运行期", "models": rows,
                "tasks": [{"purpose": purpose, "model": model, **counts,
                           **{f"{name}_tokens": counts[f"{name}_tokens"]
                              if counts[f"{name}_reported_requests"] else None
                              for name in ("input", "cached", "output")}}
                          for (purpose, model), counts in self.tasks.items()],
                "event_loop_delay": self.distribution(self.loop_delays)}


class LoopDelayProbe:
    """以事件循环自身时钟测量调度延迟，不创建线程或占用聊天任务。"""

    def __init__(self, report):
        self.report = report
        self.handle = None

    def start(self):
        self.stop()
        if not callable(self.report):
            return
        loop = asyncio.get_running_loop()

        def schedule():
            expected = loop.time() + 1
            self.handle = loop.call_at(expected, sample, expected)

        def sample(expected):
            self.report(max(0, loop.time() - expected) * 1000)
            schedule()

        schedule()

    def stop(self):
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None
