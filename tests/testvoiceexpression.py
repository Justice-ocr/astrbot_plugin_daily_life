"""语义分段与语音判断的独立模型、并发、超时及取消回归。"""

import asyncio
import copy
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from support import LifeSettings
import testreply
from core.runtime.expression import remember_expression_context
from core.runtime.reply import SegmentPart, SemanticSegmentPlan
from core.runtime.voice.modality import VoiceExpressionPlan, VoiceExpressionPlanner


class VoiceExpressionTests(unittest.IsolatedAsyncioTestCase):
    def setup_reply(self):
        source = "我也已经躺下啦。想听你说晚安。"
        runtime, event, synthesize = testreply.SemanticSegmentTest()._voice_reply(
            source
        )
        runtime.config.chat_style.semantic_provider = "segment-model"
        runtime.config.voice_generation.expression_provider = "voice-model"
        runtime.config.voice_generation.smart_switch_probability = 100
        runtime.config.chat_style.punctuation_cleanup_enabled = False
        event.message_str = "耳机戴好了"
        runtime.mark_voice_switch_available(event)
        return source, runtime, event, synthesize

    async def test_voice_receipt_does_not_rewrite_framework_conversation(self):
        for saved_state in ("saved", "newer_turn", "not_saved"):
            with self.subTest(saved_state=saved_state):
                source, runtime, event, synthesize = self.setup_reply()
                manager = runtime.context.conversation_manager
                scope = event.unified_msg_origin
                saved_history = [
                    {"role": "user", "content": event.message_str},
                    {"role": "assistant", "content": source},
                ]
                if saved_state == "newer_turn":
                    saved_history.append({"role": "user", "content": "那你早点睡"})
                if saved_state != "not_saved":
                    manager.current_ids[scope] = "current"
                    manager.conversations[scope] = types.SimpleNamespace(
                        history=copy.deepcopy(saved_history)
                    )
                # 发送形式经过清洗、分段，和框架保存的原始回复不同。
                spoken = "我也已经躺下啦\n想听你说晚安"
                event.set_result(event.chain_result([types.SimpleNamespace(text=spoken)]))
                event._daily_life_expression_plan = VoiceExpressionPlan(
                    channel="voice", confidence=0.95
                )
                runtime._note_voice_expression_decision = AsyncMock()

                self.assertTrue(await runtime.apply_voice_switch_before_send(event))
                self.assertTrue(await runtime.note_voice_switch_message_sent(event))
                self.assertFalse(await runtime.note_voice_switch_message_sent(event))

                synthesize.assert_awaited_once()
                self.assertEqual(synthesize.await_args.args, (spoken,))
                runtime._note_voice_expression_decision.assert_awaited_once()
                self.assertEqual(manager.calls, [])
                if saved_state == "not_saved":
                    self.assertEqual(manager.conversations, {})
                else:
                    self.assertEqual(manager.conversations[scope].history, saved_history)

    async def test_same_provider_still_receives_two_concurrent_distinct_requests(self):
        source, runtime, event, synthesize = self.setup_reply()
        runtime.config.voice_generation.expression_provider = "segment-model"
        remember_expression_context(
            event, state="困倦，已经躺下", conversation="对方：耳机戴好了"
        )
        both_started = asyncio.Event()
        calls = []

        async def model(provider, prompt, scope, **options):
            calls.append((prompt, scope, options))
            if len(calls) == 2:
                both_started.set()
            await both_started.wait()
            if "待判断表达" in prompt:
                self.assertIn("困倦，已经躺下", prompt)
                self.assertEqual(prompt.count(event.message_str), 1)
                self.assertNotIn('"segments"', prompt)
                return '{"channel":"voice","confidence":0.95,"voice_style":"soft"}'
            self.assertNotIn('"channel"', prompt)
            self.assertNotIn("困倦，已经躺下", prompt)
            return json.dumps(
                {"segments": [{"text": "我也已经躺下啦。"}, {"text": "想听你说晚安。"}]}
            )

        runtime.composer._call_llm_text = model
        await asyncio.wait_for(runtime.apply_semantic_segment_before_send(event), 0.5)
        self.assertEqual(
            runtime.composer.provider_ids, ["segment-model", "segment-model"]
        )
        self.assertEqual(len(calls), 2)
        for _, scope, options in calls:
            self.assertEqual(scope, event.unified_msg_origin)
            self.assertEqual(options["primary_provider_id"], "segment-model")
            self.assertEqual(options["empty_retries"], 0)
        self.assertEqual(
            "".join(item.text for item in event.get_result().chain), source
        )
        self.assertTrue(await runtime.apply_voice_switch_before_send(event))
        self.assertEqual(synthesize.await_args.kwargs["voice_style"], "soft")

    async def test_each_selector_routes_to_its_own_provider_including_default(self):
        for expression_provider in ("voice-model", ""):
            with self.subTest(expression_provider=expression_provider):
                source, runtime, event, _ = self.setup_reply()
                runtime.config.voice_generation.expression_provider = (
                    expression_provider
                )

                async def model(provider, prompt, scope, **options):
                    expected = (
                        expression_provider
                        if "待判断表达" in prompt
                        else "segment-model"
                    )
                    self.assertEqual(options["primary_provider_id"], expected)
                    return (
                        '{"channel":"text","confidence":0.9}'
                        if "待判断表达" in prompt
                        else json.dumps({"segments": [{"text": source}]})
                    )

                runtime.composer._call_llm_text = model
                await runtime.apply_semantic_segment_before_send(event)
                self.assertEqual(
                    runtime.composer.provider_ids,
                    ["segment-model", expression_provider],
                )
                self.assertTrue(
                    runtime._semantic_expression_plan_from_event(event).valid
                )

    async def test_segmentation_failure_does_not_discard_valid_voice_judgment(self):
        for failure in ("invalid", "timeout", "exception"):
            with self.subTest(failure=failure):
                _, runtime, event, synthesize = self.setup_reply()

                async def model(provider, prompt, scope, **options):
                    if "待判断表达" in prompt:
                        return '{"channel":"voice","confidence":0.95}'
                    if failure == "timeout":
                        await asyncio.Event().wait()
                    if failure == "exception":
                        raise RuntimeError("segment unavailable")
                    return '{"segments":[{"text":"篡改原文"}]}'

                runtime.composer._call_llm_text = model
                with patch(
                    "core.runtime.reply._semantic_timeout_seconds", return_value=0.01
                ):
                    await asyncio.wait_for(
                        runtime.apply_semantic_segment_before_send(event), 0.5
                    )
                self.assertTrue(await runtime.apply_voice_switch_before_send(event))
                synthesize.assert_awaited_once()
                self.assertFalse(
                    hasattr(event, "_daily_life_expression_unavailable_reason")
                )

    async def test_voice_failure_keeps_valid_segments_without_probability(self):
        for failure in ("invalid", "timeout", "exception"):
            with self.subTest(failure=failure):
                _, runtime, event, synthesize = self.setup_reply()
                expected = ["我也已经躺下啦。", "想听你说晚安。"]

                async def model(provider, prompt, scope, **options):
                    if "待判断表达" not in prompt:
                        return json.dumps(
                            {"segments": [{"text": text} for text in expected]}
                        )
                    if failure == "timeout":
                        await asyncio.Event().wait()
                    if failure == "exception":
                        raise RuntimeError("voice unavailable")
                    return '{"segments":[],"channel":"voice"}'

                runtime.composer._call_llm_text = model
                with (
                    patch(
                        "core.runtime.voice.modality.expression_timeout_seconds",
                        return_value=0.01,
                    ),
                    patch("core.runtime.voice.limit.random.random") as draw,
                ):
                    await asyncio.wait_for(
                        runtime.apply_semantic_segment_before_send(event), 0.5
                    )
                    self.assertFalse(
                        await runtime.apply_voice_switch_before_send(event)
                    )
                self.assertEqual(
                    [item.text for item in event.get_result().chain], expected
                )
                self.assertEqual(
                    runtime.semantic_segment_status()["metrics"]["planning_failed"], 0
                )
                self.assertTrue(await runtime.send_semantic_segments_if_needed(event))
                self.assertEqual(
                    [
                        runtime._semantic_segment_text(message.chain[0])
                        for message in event.sent_messages
                    ],
                    expected,
                )
                synthesize.assert_not_awaited()
                draw.assert_not_called()

    async def test_cancellation_cancels_both_requests_and_preserves_original_reply(
        self,
    ):
        source, runtime, event, _ = self.setup_reply()
        both_started = asyncio.Event()
        started, cancelled = [], []

        async def model(provider, prompt, scope, **options):
            started.append(options["primary_provider_id"])
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(options["primary_provider_id"])

        runtime.composer._call_llm_text = model
        task = asyncio.create_task(runtime.apply_semantic_segment_before_send(event))
        await asyncio.wait_for(both_started.wait(), 0.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertCountEqual(cancelled, ["segment-model", "voice-model"])
        self.assertEqual(event.get_result().chain[0].text, source)
        self.assertIsNone(runtime._semantic_expression_plan_from_event(event))
        self.assertEqual(event.sent_messages, [])

    async def test_voice_deadline_includes_provider_lookup(self):
        _, runtime, event, _ = self.setup_reply()
        runtime.composer._get_provider = AsyncMock(side_effect=lambda *args: None)
        cancelled = []

        async def lookup(provider_id):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(provider_id)

        runtime.composer._get_provider.side_effect = lookup
        with patch(
            "core.runtime.voice.modality.expression_timeout_seconds",
            return_value=0.01,
        ):
            plan = await asyncio.wait_for(
                VoiceExpressionPlanner(runtime).plan("晚安", scope="test"), 0.5
            )
        self.assertFalse(plan.valid)
        self.assertEqual(plan.reason, "表达判断超时。")
        self.assertEqual(cancelled, ["voice-model"])
        self.assertEqual(runtime.composer.prompts, [])

    async def test_model_deadlines_are_independent_of_prior_waits(self):
        source, runtime, event, _ = self.setup_reply()
        runtime.config.chat_style.semantic_timeout_seconds = 2
        runtime.config.voice_generation.expression_timeout_seconds = 11
        event._daily_life_enrichment_seconds = 25

        async def model(provider, prompt, scope, **options):
            return (
                '{"channel":"text","confidence":0.9}'
                if "待判断表达" in prompt
                else json.dumps({"segments": [{"text": source}]})
            )

        runtime.composer._call_llm_text = model
        with patch(
            "core.runtime.reply.asyncio.timeout", wraps=asyncio.timeout
        ) as timeout:
            await runtime.apply_semantic_segment_before_send(event)
        self.assertCountEqual(
            [call.args[0] for call in timeout.call_args_list], [2, 11]
        )
        self.assertEqual(event._daily_life_enrichment_seconds, 25)

    async def test_disabling_either_feature_skips_only_its_request(self):
        for disabled in ("segmentation", "voice"):
            with self.subTest(disabled=disabled):
                source, runtime, event, _ = self.setup_reply()
                if disabled == "segmentation":
                    runtime.config.chat_style.enabled = False
                    runtime.composer._call_llm_text = AsyncMock(
                        return_value='{"channel":"voice","confidence":0.9}'
                    )
                    expected_provider = "voice-model"
                else:
                    runtime.config.voice_generation.smart_switch_enabled = False
                    runtime.composer._call_llm_text = AsyncMock(
                        return_value=json.dumps({"segments": [{"text": source}]})
                    )
                    expected_provider = "segment-model"
                await runtime.apply_semantic_segment_before_send(event)
                runtime.composer._call_llm_text.assert_awaited_once()
                self.assertEqual(runtime.composer.provider_ids, [expected_provider])
                if disabled == "segmentation":
                    self.assertEqual(
                        [item.text for item in event.get_result().chain], [source]
                    )
                    self.assertIsNotNone(
                        runtime._semantic_expression_plan_from_event(event)
                    )

    async def test_segmentation_result_cannot_masquerade_as_voice_judgment(self):
        _, runtime, event, _ = self.setup_reply()
        setattr(
            event,
            runtime._SEMANTIC_SEGMENT_PLAN_ATTR,
            SemanticSegmentPlan((SegmentPart("晚安"),)),
        )
        self.assertIsNone(runtime._semantic_expression_plan_from_event(event))
        event._daily_life_expression_plan = VoiceExpressionPlan(
            channel="voice", confidence=0.9
        )
        self.assertEqual(
            runtime._semantic_expression_plan_from_event(event).channel, "voice"
        )

    def test_voice_result_requires_valid_channel_and_finite_confidence(self):
        for payload in (
            {},
            {"channel": [], "confidence": 1},
            {"channel": "voice"},
            {"channel": "voice", "confidence": True},
            *(
                {"channel": "voice", "confidence": value}
                for value in (float("nan"), float("inf"), -0.1, 1.1)
            ),
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(VoiceExpressionPlanner.validate_payload(payload))
        plan = VoiceExpressionPlanner.validate_payload(
            {"channel": "voice", "confidence": 0.9}
        )
        self.assertEqual(plan.channel, "voice")

    def test_voice_config_defaults_and_bounds(self):
        settings = LifeSettings.from_dict({})
        self.assertEqual(settings.voice_generation.expression_provider, "")
        self.assertEqual(settings.voice_generation.expression_timeout_seconds, 8)
        settings = LifeSettings.from_dict(
            {"voice_generation_config": {"expression_timeout_seconds": -5}}
        )
        self.assertEqual(settings.voice_generation.expression_timeout_seconds, 1)
