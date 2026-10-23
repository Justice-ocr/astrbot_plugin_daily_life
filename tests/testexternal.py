"""协作插件的文本生成复用模型路由，不借用聊天会话。"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import support  # noqa: F401 - 安装隔离的 AstrBot 测试替身
from core.runtime.external import generate_text, model_route
from core.runtime.gateway import ModelCallOptions, ModelGateway


class ExternalModelTest(unittest.IsolatedAsyncioTestCase):
    def runtime(self):
        primary = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="shared", model="model-a")
        )
        fallback = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="default", model="model-b")
        )

        async def candidates(selected):
            self.assertEqual(selected, "shared")
            yield primary
            yield fallback

        return SimpleNamespace(
            config=SimpleNamespace(llm_provider="shared"),
            get_text_provider_candidates=candidates,
            call_text_model=AsyncMock(),
            close_text_session=AsyncMock(),
        )

    async def test_route_exports_only_ids_and_model_names(self):
        self.assertEqual(
            await model_route(self.runtime()),
            {
                "providers": [
                    {"id": "shared", "model": "model-a"},
                    {"id": "default", "model": "model-b"},
                ]
            },
        )

    async def test_primary_failure_or_empty_uses_fallback_and_cleans_internal_session(
        self,
    ):
        for failure in [
            RuntimeError("private upstream error"),
            "",
            " " * 5,
            "x" * 2001,
        ]:
            with self.subTest(failure=type(failure).__name__):
                runtime = self.runtime()
                runtime.call_text_model.side_effect = [failure, "  model-a 已恢复  "]
                text = await generate_text(
                    runtime,
                    "通知数据",
                    system_prompt="目标人格",
                    target_umo="bot:GroupMessage:1",
                )
                self.assertEqual(text, "model-a 已恢复")
                calls = runtime.call_text_model.await_args_list
                self.assertEqual(
                    [c.args[0].meta().id for c in calls], ["shared", "default"]
                )
                session = calls[0].args[2]
                self.assertTrue(session.startswith("daily_life:external:"))
                self.assertNotIn("GroupMessage", session)
                self.assertEqual(calls[1].args[2], session)
                self.assertEqual(calls[1].kwargs["system_prompt"], "目标人格")
                self.assertEqual(calls[1].kwargs["primary_provider_id"], "")
                runtime.close_text_session.assert_awaited_once_with(session)

    async def test_failure_and_cancellation_cleanup_without_leaking_errors(self):
        runtime = self.runtime()
        runtime.call_text_model.side_effect = RuntimeError("secret")
        with self.assertRaisesRegex(
            RuntimeError, "^External text providers unavailable$"
        ):
            await generate_text(runtime, "事实")
        runtime.close_text_session.assert_awaited_once()
        runtime = self.runtime()
        runtime.call_text_model.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await generate_text(runtime, "事实")
        self.assertEqual(runtime.call_text_model.await_count, 1)
        runtime.close_text_session.assert_awaited_once()

    async def test_each_call_uses_fresh_session(self):
        runtime = self.runtime()
        runtime.call_text_model.return_value = "正文"
        await generate_text(runtime, "事实", target_umo="bot:GroupMessage:1")
        await generate_text(runtime, "事实", target_umo="bot:FriendMessage:2")
        sessions = [c.args[2] for c in runtime.call_text_model.await_args_list]
        self.assertNotEqual(sessions[0], sessions[1])

    async def test_gateway_preserves_existing_default_and_forwards_explicit_persona(
        self,
    ):
        composer = SimpleNamespace(_call_llm_text=AsyncMock(return_value="正文"))
        gateway = ModelGateway(composer)
        await gateway.call(None, "通知", "internal", ModelCallOptions())
        self.assertNotIn("system_prompt", composer._call_llm_text.await_args.kwargs)
        await gateway.call(
            None, "通知", "internal", ModelCallOptions(system_prompt="目标人格")
        )
        self.assertEqual(
            composer._call_llm_text.await_args.kwargs["system_prompt"], "目标人格"
        )
