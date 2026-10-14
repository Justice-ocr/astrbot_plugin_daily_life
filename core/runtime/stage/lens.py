from __future__ import annotations

import uuid
from typing import Any

from ...life.tools import extract_json_from_text

from typing import Protocol


def clean_director_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").replace("```json", "").replace("```", "").split())
    return text.strip(" ：:，,。")[:limit]


class StageLensComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _media_director_text_call: Any
    call_text_model: Any
    close_text_session: Any
    config: Any
    get_text_provider: Any


class StageLensComponent:
    def __init__(self, context: StageLensComponentContext) -> None:
        self.context = context

    def _media_image_director_provider_id(self) -> str:
        """读取图片导演专用模型；留空时沿用当前默认模型。"""
        config = getattr(self.context, "config", None)
        image_config = getattr(config, "image_generation", None)
        return str(
            getattr(image_config, "image_director_provider", "") or ""
        ).strip()

    async def _media_director_text_call(
        self, prompt: str, provider_id: str = ""
    ) -> str:
        provider_id = str(provider_id or "").strip()
        provider = await self.context.get_text_provider(provider_id)
        session_id = f"daily_life_media_{uuid.uuid4().hex[:8]}"
        try:
            text = await self.context.call_text_model(
                provider,
                prompt,
                session_id,
                empty_retries=0,
                primary_provider_id=provider_id,
            )
            return str(text or "").strip()
        finally:
            await self.context.close_text_session(session_id)

    async def _media_director_call(
        self, prompt: str, provider_id: str = ""
    ) -> dict[str, Any]:
        text = await self.context._media_director_text_call(prompt, provider_id=provider_id)
        if not text:
            return {}
        data = extract_json_from_text(text)
        return data if isinstance(data, dict) else {}
