"""供协作插件使用、受租约约束的轻量模型接口。"""

from __future__ import annotations

import asyncio
import uuid


async def model_route(runtime) -> dict:
    selected = str(runtime.config.llm_provider or "").strip()
    providers = []
    async for provider in runtime.get_text_provider_candidates(selected):
        meta = provider.meta()
        if meta.id and meta.id not in {item["id"] for item in providers}:
            providers.append({"id": meta.id, "model": meta.model or ""})
        if len(providers) == 2:
            break
    return {"providers": providers}


async def generate_text(
    runtime,
    prompt: str,
    *,
    system_prompt: str = "",
    target_umo: str = "",
    timeout_seconds: float = 20,
) -> str:
    selected = str(runtime.config.llm_provider or "").strip()
    session = "daily_life:external:" + uuid.uuid4().hex
    try:
        async with asyncio.timeout(min(45, max(1, timeout_seconds * 2 + 5))):
            async for provider in runtime.get_text_provider_candidates(selected):
                try:
                    text = await runtime.call_text_model(
                        provider,
                        prompt,
                        session,
                        empty_retries=0,
                        primary_provider_id="",
                        system_prompt=system_prompt,
                        timeout_seconds=min(20, max(1, timeout_seconds)),
                    )
                    if (
                        isinstance(text, str)
                        and text.strip()
                        and len(text.strip()) <= 2000
                    ):
                        return text.strip()
                except Exception:
                    # 共用网关保留熔断和用量统计。
                    # Provider 异常可能包含凭据，不得对外暴露。
                    continue
            raise RuntimeError("External text providers unavailable")
    finally:
        # 此处使用全新的内部会话，不使用用户会话的 UMO。
        await runtime.close_text_session(session)
