"""GIF 与回复分段共享可选增强的前台等待上限。"""

import asyncio
import types
import unittest

import support  # noqa: F401
from core.runtime.foreground import foreground_budget


class ForegroundBudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_previous_enrichment_reduces_remaining_wait(self):
        event = types.SimpleNamespace(_daily_life_enrichment_seconds=7.99)
        async with foreground_budget(event, maximum=3):
            await asyncio.sleep(0)
        with self.assertRaises(TimeoutError):
            async with foreground_budget(event, maximum=3):
                await asyncio.sleep(0.1)
        self.assertLess(event._daily_life_enrichment_seconds, 8.1)
