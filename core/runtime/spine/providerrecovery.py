"""模型配置变化后恢复受阻的主动承诺，媒体任务仍遵循自身重试策略。"""

from __future__ import annotations

import hashlib
import inspect
import json
import datetime
from typing import Any

from ...clock import local_datetime, now as life_now


async def provider_fingerprint(runtime: Any, provider_id: str) -> str:
    context = getattr(runtime, "context", None)
    getter = getattr(context, "get_provider_by_id", None)
    if not callable(getter) or not provider_id:
        return ""
    try:
        provider = getter(provider_id)
        if inspect.isawaitable(provider):
            provider = await provider
        config = getattr(provider, "provider_config", None)
        settings = getattr(getattr(runtime, "config", None), "state", None)
        current = context.get_config() if callable(getattr(context, "get_config", None)) else {}
        if inspect.isawaitable(current):
            current = await current
        routing = (current.get("provider_settings") or {}).get("default_provider_id", "")
        if not isinstance(config, dict):
            # 原模型已删除也属于配置变化；保留当前路由作比较依据。
            config = {"missing_provider": provider_id}
    except Exception:
        return ""
    # Key 只参与不可逆摘要，不写入任务、面板或日志。
    data = [getattr(settings, "provider", ""), routing, config]
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


async def remember_provider_block(runtime: Any, task: Any, exc: Any, owner: str) -> None:
    if task.kind != "proactive_commitment":
        return
    fingerprint = await provider_fingerprint(runtime, exc.provider_id)
    if fingerprint:
        progress = dict((task.result or {}).get("progress") or {})
        progress["provider_block"] = {
            "provider_id": exc.provider_id, "fingerprint": fingerprint,
            "status": exc.status, "at": life_now().isoformat(),
        }
        await runtime.archive.update_durable_task_progress(task.id, progress, owner=owner)


async def recover_provider_commitments(runtime: Any) -> int:
    now = life_now()
    checked = getattr(runtime, "_provider_recovery_checked_at", None)
    if isinstance(checked, datetime.datetime) and now - checked < datetime.timedelta(seconds=30):
        return 0
    runtime._provider_recovery_checked_at = now
    tasks = await runtime.archive.get_durable_tasks(status="dead", kind="proactive_commitment", limit=100)
    fingerprints: dict[str, str] = {}
    recovered = 0
    for task in tasks:
        block = (task.result.get("progress") or {}).get("provider_block") or {}
        commitment = await runtime.archive.get_commitment(int(task.payload.get("commitment_id") or 0))
        if not commitment or commitment.status in {"done", "cancelled", "expired", "delivery_failed"}:
            continue
        expires = local_datetime(task.payload.get("expires_at"))
        if expires and life_now() > expires:
            await runtime.archive.set_commitment_status(commitment.id, "expired", life_now().isoformat())
            continue
        if not block.get("fingerprint"):
            continue
        provider_id = block["provider_id"]
        if provider_id not in fingerprints:
            fingerprints[provider_id] = await provider_fingerprint(runtime, provider_id)
        current = fingerprints[provider_id]
        if current and current != block["fingerprint"]:
            recovered += int(await runtime.archive.recover_provider_blocked_task(task.id, current))
    return recovered
