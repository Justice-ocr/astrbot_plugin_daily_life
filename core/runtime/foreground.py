"""限制同一轮媒体增强的等待额度；语义表达判断使用独立配置超时。"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

FOREGROUND_ENRICHMENT_SECONDS = 8.0
_SPENT_ATTR = "_daily_life_enrichment_seconds"


@asynccontextmanager
async def foreground_budget(event: Any, *, maximum: float):
    spent = max(0.0, float(getattr(event, _SPENT_ATTR, 0.0) or 0.0))
    remaining = max(0.0, FOREGROUND_ENRICHMENT_SECONDS - spent)
    started = time.monotonic()
    try:
        async with asyncio.timeout(min(maximum, remaining)):
            yield
    finally:
        if event is not None:
            setattr(event, _SPENT_ATTR, spent + time.monotonic() - started)
