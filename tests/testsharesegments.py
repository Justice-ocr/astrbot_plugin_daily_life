"""外部自然表达复用原生分段、停顿、中断及实际送达回执。"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from support import Context, Provider
import testreply


class ShareSegmentsTest(unittest.IsolatedAsyncioTestCase):
    def runtime(self, parts, *, enabled=True, response=None):
        runtime = testreply.SemanticSegmentTest()._runtime(
            response
            if response is not None
            else json.dumps(
                {"segments": [{"text": part, "pause": "normal"} for part in parts]},
                ensure_ascii=False,
            )
        )
        runtime.config.chat_style.enabled = enabled
        runtime.context = Context(Provider([]), config={"t2i": False})
        runtime.send_message_if_not_recalled = AsyncMock(return_value=True)
        runtime.note_structured_bot_message = lambda *args, **kwargs: None
        runtime.log_outbound_message_async = AsyncMock()
        runtime.decorate_group_addressing_chain = lambda *args, **kwargs: None
        return runtime

    async def test_semantic_segments_keep_current_scope_and_actual_receipt(self):
        parts = ["这告警还挺执着", "连着 214 轮都没歇", "先让它亮着吧"]
        runtime = self.runtime(parts)
        result = await runtime.send_share_expression(
            "".join(parts), target_umo="bot:GroupMessage:42"
        )
        self.assertEqual(result, {"sent_texts": parts})
        calls = runtime.send_message_if_not_recalled.await_args_list
        self.assertEqual([call.args[0] for call in calls], ["bot:GroupMessage:42"] * 3)
        self.assertEqual(
            [
                getattr(call.args[1].chain[0], "text", call.args[1].chain[0])
                for call in calls
            ],
            parts,
        )
        self.assertEqual(len(runtime.composer.prompts), 1)

    async def test_disabled_style_sends_once_without_model_call(self):
        text = "先让它亮着吧 我这边已经困了"
        runtime = self.runtime([text], enabled=False)
        self.assertEqual(
            await runtime.send_share_expression(
                text, target_umo="bot:FriendMessage:42"
            ),
            {"sent_texts": [text]},
        )
        runtime.send_message_if_not_recalled.assert_awaited_once()
        self.assertEqual(runtime.composer.prompts, [])

    async def test_invalid_semantic_plan_uses_existing_natural_fallback(self):
        text = "这告警还挺执着。先让它亮着吧。"
        runtime = self.runtime([], response="{}")
        runtime.config.chat_style.proactive_max_chars = 8
        result = await runtime.send_share_expression(
            text, target_umo="bot:FriendMessage:42"
        )
        self.assertGreater(len(result["sent_texts"]), 1)
        self.assertIn("先让它亮着吧", "".join(result["sent_texts"]))
        self.assertEqual(runtime._semantic_segment_metrics["fallback_natural"], 1)

    async def test_partial_failure_returns_only_delivered_prefix(self):
        parts = ["先让它亮着吧", "我这边已经困了", "明天再聊"]
        runtime = self.runtime(parts)
        runtime.send_message_if_not_recalled.side_effect = [True, False]
        result = await runtime.send_share_expression(
            "".join(parts), target_umo="bot:GroupMessage:42"
        )
        self.assertEqual(result, {"sent_texts": parts[:1]})
        self.assertEqual(runtime.send_message_if_not_recalled.await_count, 2)

    async def test_new_message_interrupts_remaining_segments_after_configured_pause(
        self,
    ):
        parts = ["先让它亮着吧", "我这边已经困了"]
        runtime = self.runtime(parts)
        runtime.config.chat_style.segment_min_delay_seconds = 0.2
        runtime.config.chat_style.segment_max_delay_seconds = 0.2

        async def incoming(_delay):
            runtime._semantic_segment_revisions["bot:GroupMessage:42"] = 1

        with patch("core.runtime.reply.asyncio.sleep", side_effect=incoming) as sleep:
            result = await runtime.send_share_expression(
                "".join(parts), target_umo="bot:GroupMessage:42"
            )
        self.assertEqual(result, {"sent_texts": parts[:1]})
        sleep.assert_awaited_once_with(0.2)
        runtime.send_message_if_not_recalled.assert_awaited_once()

    async def test_post_send_logging_failure_keeps_delivered_receipt(self):
        text = "先让它亮着吧"
        runtime = self.runtime([text])
        runtime.log_outbound_message_async.side_effect = RuntimeError("测试日志失败")
        self.assertEqual(
            await runtime.send_share_expression(text, target_umo="bot:GroupMessage:42"),
            {"sent_texts": [text]},
        )
        runtime.send_message_if_not_recalled.assert_awaited_once()

    async def test_cancellation_propagates_without_resending(self):
        runtime = self.runtime(["先歇一会儿"])
        runtime.send_message_if_not_recalled.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await runtime.send_share_expression(
                "先歇一会儿", target_umo="bot:GroupMessage:42"
            )
        runtime.send_message_if_not_recalled.assert_awaited_once()
