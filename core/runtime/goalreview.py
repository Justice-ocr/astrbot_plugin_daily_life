"""独立验收已保存的阶段成果，不与当前动作选择争用输出职责。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..life.tools import extract_json_from_text
from ..prompts import cache_friendly_prompt

GOAL_REVIEW_PROMPT = """根据已保存的成果复核角色自身的目标阶段，只输出 JSON：
{"achievement_reviews":[{"goal_id":"候选编号","step_id":"候选编号","revision":"候选 revision","verdict":"passed|needs_practice|uncertain","reason":"判断依据","evidence":[{"action_id":"证据编号","quote":"逐字引用证据"}],"next_step":"后续具体做法"}]}。
逐一审阅给出的候选。artifact 类型引用 content，action 类型引用 result。
投入时间只代表练习，不代表掌握；验收仅判断给定条件是否有证据支持，不授予技能认证。
证据不足用 uncertain，已经复核未通过则说明需要补足的具体成果；不能补写作品或编造引用。"""


async def review_goals(
    provider: Any, candidates: list[dict], *, call: Callable[..., Awaitable[str]],
    close: Callable[[str], Awaitable[None]], provider_id: str, timeout: float,
) -> list[dict]:
    if not candidates:
        return []
    session = f"daily_life_goal_review_{uuid.uuid4().hex[:8]}"
    try:
        async with asyncio.timeout(min(30, timeout)):
            text = await call(
                provider, cache_friendly_prompt(GOAL_REVIEW_PROMPT,
                    json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))),
                session, empty_retries=0, primary_provider_id=provider_id,
                timeout_seconds=min(30, timeout),
            )
        payload = extract_json_from_text(text)
        reviews = payload.get("achievement_reviews") if isinstance(payload, dict) else None
        return reviews if isinstance(reviews, list) else []
    except Exception:
        # 保留待复核状态，下次后台巡检重试；不向聊天打印模型异常正文。
        return []
    finally:
        await close(session)
