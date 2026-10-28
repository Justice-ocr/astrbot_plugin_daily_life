import asyncio
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import support  # noqa: F401 -- install the lightweight AstrBot fixtures first
from runtimehelpers import Context, DailyLifeRuntime, DataManager, Event, Provider
from core.config.options import LifeSettings
from core.media.base import GeneratedVoice
from core.media.hub import LifeMediaService
from core.media.voice import MIMO_PLUGIN_NAME, MimoVoiceService, synthesize_voice
from core.media.volcengine import VolcengineVoiceService


class MimoVoiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "peer.wav"
        with wave.open(str(self.source), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(24000)
            stream.writeframes(b"\x00\x00" * 120)
        self.config = LifeSettings.from_dict({
            "voice_generation_config": {"enabled": True, "backend": "mimo"}
        })
        self.plugin = SimpleNamespace(
            synthesize_text=AsyncMock(return_value=self.source),
            close=AsyncMock(),
        )
        self.metadata = SimpleNamespace(activated=True, star_cls=self.plugin)
        self.context = SimpleNamespace(
            get_registered_star=lambda name: self.metadata if name == MIMO_PLUGIN_NAME else None
        )
        self.service = MimoVoiceService(
            self.config.voice_generation, self.root, self.context
        )

    async def test_synthesis_copies_peer_audio_and_routes_private_voice(self):
        generated = await synthesize_voice(
            self.service, "hello", target_umo="qq:FriendMessage:123",
            emotion="sleepy", emotion_category="happy", voice_style="soft"
        )
        self.assertIsInstance(generated, GeneratedVoice)
        self.assertEqual(generated.path.read_bytes(), self.source.read_bytes())
        self.assertNotEqual(generated.path, self.source)
        self.assertEqual(generated.path.parent, self.root / "generated" / "voices")
        kwargs = self.plugin.synthesize_text.await_args.kwargs
        self.assertEqual(kwargs["user_id"], "123")
        self.assertEqual(kwargs["group_id"], "qq:FriendMessage:123")
        self.assertEqual(kwargs["emotion"], "happy")
        self.assertIsNone(kwargs["voice_name"])
        self.assertIn("sleepy", kwargs["context"])
        self.assertEqual(list(generated.path.parent.glob("*.part")), [])
        await self.service.close()
        self.plugin.close.assert_not_awaited()
        self.assertTrue(self.source.exists())

    async def test_explicit_voice_and_group_sender_reach_peer(self):
        self.config.voice_generation.mimo_voice = "my-voice"
        event = SimpleNamespace(
            unified_msg_origin="qq:GroupMessage:456", get_sender_id=lambda: "123"
        )
        await synthesize_voice(
            self.service, "hello", target_umo=event.unified_msg_origin,
            source_event=event, voice_style="sad"
        )
        kwargs = self.plugin.synthesize_text.await_args.kwargs
        self.assertEqual(kwargs["voice_name"], "my-voice")
        self.assertEqual(kwargs["user_id"], "123")
        self.assertEqual(kwargs["group_id"], event.unified_msg_origin)
        self.assertEqual(kwargs["emotion"], "sad")

    async def test_foreign_source_does_not_leak_sender_into_target_routing(self):
        event = SimpleNamespace(
            unified_msg_origin="qq:FriendMessage:elsewhere", get_sender_id=lambda: "wrong"
        )
        await synthesize_voice(
            self.service, "hello", target_umo="qq:FriendMessage:target", source_event=event
        )
        self.assertEqual(self.plugin.synthesize_text.await_args.kwargs["user_id"], "target")

    async def test_missing_disabled_or_incompatible_peer_fails_without_synthesis(self):
        for metadata in (None, SimpleNamespace(activated=False, star_cls=self.plugin),
                         SimpleNamespace(activated=True, star_cls=None),
                         SimpleNamespace(activated=True, star_cls=object())):
            with self.subTest(metadata=metadata):
                self.metadata = metadata
                with self.assertRaises(RuntimeError):
                    await self.service.synthesize("hello")
        self.plugin.synthesize_text.assert_not_awaited()

    async def test_peer_instance_is_resolved_again_after_reload(self):
        await self.service.synthesize("first")
        replacement = SimpleNamespace(synthesize_text=AsyncMock(return_value=self.source))
        self.metadata = SimpleNamespace(activated=True, star_cls=replacement)
        await self.service.synthesize("second")
        self.plugin.synthesize_text.assert_awaited_once()
        replacement.synthesize_text.assert_awaited_once()

    async def test_invalid_output_is_rejected(self):
        empty = self.root / "empty.wav"
        empty.touch()
        bad = self.root / "not-a-wave.wav"
        bad.write_bytes(b"x" * 100)
        for output in (None, "", self.root / "missing.wav", empty, bad):
            with self.subTest(output=output):
                self.plugin.synthesize_text.return_value = output
                with self.assertRaises(RuntimeError):
                    await self.service.synthesize("hello")
        self.assertFalse(self.service.output_dir.exists())

    async def test_oversized_audio_is_rejected(self):
        with patch("core.media.voice._MAX_AUDIO_BYTES", 45):
            with self.assertRaises(RuntimeError):
                await self.service.synthesize("hello")
        self.assertTrue(self.source.exists())

    async def test_invalid_style_and_emotion_use_neutral(self):
        await self.service.synthesize("hello", emotion_category="unknown", voice_style="unknown")
        self.assertEqual(self.plugin.synthesize_text.await_args.kwargs["emotion"], "neutral")

    async def test_disabled_or_empty_input_does_not_call_peer(self):
        self.config.voice_generation.enabled = False
        with self.assertRaises(RuntimeError):
            await self.service.synthesize("hello")
        self.config.voice_generation.enabled = True
        with self.assertRaises(ValueError):
            await self.service.synthesize("  ")
        self.plugin.synthesize_text.assert_not_awaited()

    async def test_peer_error_is_not_retried(self):
        self.plugin.synthesize_text.side_effect = RuntimeError("peer unavailable")
        with self.assertRaisesRegex(RuntimeError, "peer unavailable"):
            await self.service.synthesize("hello")
        self.plugin.synthesize_text.assert_awaited_once()

    async def test_timeout_cancels_synthesis_without_retry(self):
        cancelled = asyncio.Event()

        async def slow(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.plugin.synthesize_text.side_effect = slow
        self.config.voice_generation.mimo_timeout_seconds = 0.01
        with self.assertRaises(TimeoutError):
            await self.service.synthesize("hello")
        self.assertTrue(cancelled.is_set())
        self.plugin.synthesize_text.assert_awaited_once()

    async def test_call_cancellation_propagates(self):
        started = asyncio.Event()

        async def slow(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.plugin.synthesize_text.side_effect = slow
        task = asyncio.create_task(self.service.synthesize("hello"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.output_dir.exists())

    async def test_existing_backend_keeps_original_call_signature(self):
        synthesize = AsyncMock(return_value=GeneratedVoice(self.source))
        service = SimpleNamespace(synthesize=synthesize)
        await synthesize_voice(
            service, "hello", target_umo="qq:FriendMessage:123", emotion="happy"
        )
        synthesize.assert_awaited_once_with("hello", emotion="happy")

    async def test_media_hub_selects_backend_without_requiring_volcengine_keys(self):
        media = LifeMediaService(self.config, self.root / "life.db", context=self.context)
        self.assertIsInstance(media.voice, MimoVoiceService)
        await media.voice.synthesize("hello")
        await media.close()
        self.plugin.close.assert_not_awaited()
        legacy = LifeMediaService(LifeSettings.from_dict({}), self.root / "life.db")
        self.assertIsInstance(legacy.voice, VolcengineVoiceService)
        await legacy.close()

    async def test_explicit_voice_tool_uses_peer_and_delivers_once(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.context = Context(Provider([]))
        runtime.config = self.config
        runtime.archive = DataManager()
        runtime.media = SimpleNamespace(voice=self.service)
        event = Event(unified_msg_origin="aiocqhttp:FriendMessage:10001")
        result = await runtime.life_voice_generate(event, "hello", user_requested=True)
        self.assertIsNone(result)
        self.assertEqual(len(event.sent_messages), 1)
        self.plugin.synthesize_text.assert_awaited_once()
        self.assertEqual(self.plugin.synthesize_text.await_args.kwargs["group_id"], event.unified_msg_origin)

    async def test_automatic_voice_uses_peer_and_keeps_text_on_peer_failure(self):
        from testreply import SemanticSegmentTest

        for fail in (False, True):
            with self.subTest(fail=fail):
                self.plugin.synthesize_text.reset_mock()
                self.plugin.synthesize_text.side_effect = RuntimeError("offline") if fail else None
                source = "晚安啦。"
                runtime, event, _ = SemanticSegmentTest()._voice_reply(
                    source, json.dumps({
                        "segments": [{"text": source}], "channel": "voice", "confidence": 0.99
                    })
                )
                runtime.config.voice_generation = self.config.voice_generation
                runtime.config.voice_generation.smart_switch_probability = 100
                runtime.media = SimpleNamespace(voice=self.service)
                runtime.mark_voice_switch_available(event)
                await runtime.apply_semantic_segment_before_send(event)
                original_result = event.get_result()
                changed = await runtime.apply_voice_switch_before_send(event)
                self.assertEqual(changed, not fail)
                self.plugin.synthesize_text.assert_awaited_once()
                self.assertEqual(self.plugin.synthesize_text.await_args.kwargs["group_id"], event.unified_msg_origin)
                if fail:
                    self.assertIs(event.get_result(), original_result)
                    self.assertFalse(hasattr(event, "_daily_life_pending_voice_receipt"))
                else:
                    self.assertEqual(event._daily_life_pending_voice_receipt["text"], source)

    async def test_proactive_contact_uses_peer_or_falls_back_to_one_text(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                self.plugin.synthesize_text.reset_mock()
                self.plugin.synthesize_text.side_effect = RuntimeError("offline") if fail else None
                self.config.voice_generation.proactive_enabled = True
                runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
                runtime.context = Context(Provider([]))
                runtime.config = self.config
                runtime.archive = DataManager()
                runtime.media = SimpleNamespace(voice=self.service)
                runtime._apply_proactive_send_timing = AsyncMock()
                runtime._send_proactive_emoji_if_needed = AsyncMock()
                runtime._part_proactive_send_component._mark_failed_proactive_contact = AsyncMock()
                scope = "aiocqhttp:FriendMessage:10001"
                sent = await runtime._send_proactive_message(
                    scope, "晚安啦", "send failed", send_payload={
                        "expression_intent": {
                            "channel": "voice", "confidence": 0.99,
                            "reason": "quiet conversation", "voice_style": "soft"
                        }
                    }
                )
                self.assertTrue(sent)
                self.assertEqual(len(runtime.context.sent_messages), 1)
                self.plugin.synthesize_text.assert_awaited_once()
                self.assertEqual(self.plugin.synthesize_text.await_args.kwargs["group_id"], scope)
                if fail:
                    self.assertEqual(runtime.context.sent_messages[0][1].items, ["晚安啦"])
                else:
                    self.assertTrue(any(
                        str(getattr(item, "file", "")).endswith(".wav")
                        for item in runtime.context.sent_messages[0][1].items
                    ))

    async def test_explicit_voice_tool_reports_peer_failure_without_delivery(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.context = Context(Provider([]))
        runtime.config = self.config
        runtime.archive = DataManager()
        runtime.media = SimpleNamespace(voice=self.service)
        self.metadata = None
        event = Event(unified_msg_origin="aiocqhttp:FriendMessage:10001")
        result = await runtime.life_voice_generate(event, "hello", user_requested=True)
        self.assertIsInstance(result, str)
        self.assertEqual(event.sent_messages, [])


class MimoSettingsTest(unittest.TestCase):
    def test_defaults_and_invalid_backend_preserve_existing_behavior(self):
        self.assertEqual(LifeSettings.from_dict({}).voice_generation.backend, "volcengine")
        config = LifeSettings.from_dict({"voice_generation_config": {"backend": "bad"}})
        self.assertEqual(config.voice_generation.backend, "volcengine")

    def test_mimo_values_are_normalized_and_bounded(self):
        config = LifeSettings.from_dict({"voice_generation_config": {
            "backend": " MIMO ", "mimo_voice": " voice ", "mimo_timeout_seconds": 9999
        }}).voice_generation
        self.assertEqual(config.backend, "mimo")
        self.assertEqual(config.mimo_voice, "voice")
        self.assertEqual(config.mimo_timeout_seconds, 600)

    def test_schema_and_dashboard_expose_mimo_configuration(self):
        root = Path(__file__).resolve().parents[1]
        items = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))["voice_generation_config"]["items"]
        self.assertEqual(items["backend"]["default"], "volcengine")
        self.assertIn("mimo", items["backend"]["options"])
        settings = (root / "pages/dashboard/ui/settings.js").read_text(encoding="utf-8")
        for field in ("backend", "mimo_voice", "mimo_timeout_seconds"):
            self.assertIn("voice_generation_config." + field, settings)
