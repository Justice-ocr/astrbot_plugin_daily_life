"""自动表情与同处场景主动表达的回归测试。"""

import asyncio
import datetime
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from runtimehelpers import EmojiAssetRecord, Event, RuntimeAsyncHelpers

# isort: split

from core.models import ExpressionIntentRecord
from core.runtime.reply import SemanticSegmentPlan


class AutomaticExpressionTest(RuntimeAsyncHelpers, unittest.IsolatedAsyncioTestCase):
    async def _emoji_runtime(self, response='{"emoji_id": 1}'):
        runtime, provider = self._make_proactive_runtime([response], context_config={"t2i": False})
        runtime._semantic_segment_init_state()
        runtime._init_continuous_turn_state()
        runtime.config.chat_style.private_casual_max_chars = 10
        runtime.config.emoji.send_cooldown_seconds = 600
        runtime._chat_style_natural_segment_delay_seconds = lambda *args, **kwargs: 0
        runtime._part_semantic_segment_runtime_component._semantic_segment_delay_seconds = lambda *args, **kwargs: 0
        asset = EmojiAssetRecord(
            id=1, file_hash="smile", file_path="https://example.com/smile.png",
            label="偷笑", description="捂嘴偷笑", emotions=["开心"], status="ready",
        )
        await runtime.archive.upsert_emoji_asset(asset)
        self.addAsyncCleanup(runtime._cancel_background_tasks)
        return runtime, provider, asset

    @staticmethod
    def _reply_event(runtime, message_id="m1", text="哼哼"):
        event = Event(message_id=message_id)
        event.message_str = "又被我逗笑了吧"
        runtime.note_continuous_turn_incoming(event)
        event.set_result(event.chain_result([types.SimpleNamespace(text=text)]))
        return event

    async def test_short_presplit_and_timeout_replies_all_judge_emoji(self):
        for case in ("short", "presplit", "timeout", "style_disabled"):
            with self.subTest(case=case):
                runtime, provider, _ = await self._emoji_runtime()
                event = self._reply_event(runtime)
                if case == "presplit":
                    event.set_result(event.chain_result([
                        types.SimpleNamespace(text="少贫嘴"), types.SimpleNamespace(text="你又逗我"),
                    ]))
                elif case == "timeout":
                    event.set_result(event.chain_result([
                        types.SimpleNamespace(text="少贫嘴，你又逗我开心啦。"),
                    ]))
                    runtime._part_semantic_segment_runtime_component._semantic_segment_plan_text = AsyncMock(return_value=SemanticSegmentPlan(
                        (), source="semantic_timeout", valid=False,
                        reason="表达判断达到配置上限，本轮保留文字。",
                    ))
                elif case == "style_disabled":
                    runtime.config.chat_style.enabled = False
                await runtime.apply_semantic_segment_before_send(event)
                sent = await runtime.send_semantic_segments_if_needed(event)
                sent = await runtime.send_chat_style_segments_if_needed(event) or sent
                if not sent:
                    await event.send(event.get_result())
                    self.assertTrue(await runtime.send_semantic_emoji_if_needed(event))
                # 自行投递的分段也必须进入表情判断，重复的框架回调不能再次投递。
                self.assertFalse(await runtime.send_semantic_emoji_if_needed(event))
                await asyncio.gather(*list(runtime._background_scheduler.tasks))
                self.assertEqual(len(provider.prompts), 1)
                self.assertIn("已经发送的回复：", provider.prompts[0])
                self.assertIn("又被我逗笑了吧", provider.prompts[0])
                self.assertEqual(event.sent_messages[-1].items, [
                    {"type": "image", "url": "https://example.com/smile.png"},
                ])

    async def test_slow_selection_does_not_hold_hook_and_new_turn_cancels_delivery(self):
        runtime, _, asset = await self._emoji_runtime()
        started, release = asyncio.Event(), asyncio.Event()

        async def select(*args, **kwargs):
            started.set()
            await release.wait()
            return asset

        runtime._part_proactive_gesture_component._select_emoji_asset_for_intent = select
        event = self._reply_event(runtime)
        self.assertTrue(await asyncio.wait_for(runtime.send_semantic_emoji_if_needed(event), 0.1))
        await asyncio.wait_for(started.wait(), 0.2)
        self._reply_event(runtime, "m2", "聊点别的")
        release.set()
        await asyncio.gather(*list(runtime._background_scheduler.tasks))
        self.assertEqual(event.sent_messages, [])
        self.assertEqual(await runtime.archive.get_expression_intents(10), [])

    async def test_selection_can_decline_without_sending_or_cooldown(self):
        runtime, provider, _ = await self._emoji_runtime('{"emoji_id": 0}')
        event = self._reply_event(runtime)
        await runtime.send_semantic_emoji_if_needed(event)
        await asyncio.gather(*list(runtime._background_scheduler.tasks))
        self.assertEqual(len(provider.prompts), 1)
        self.assertEqual(event.sent_messages, [])
        self.assertFalse(await runtime._part_proactive_gesture_component._emoji_send_cooldown_active(event.unified_msg_origin))

    async def test_recall_or_switch_change_during_selection_prevents_delivery(self):
        for condition in ("recall", "disabled"):
            with self.subTest(condition=condition):
                runtime, _, asset = await self._emoji_runtime()
                event = self._reply_event(runtime)

                async def select(*args, **kwargs):
                    if condition == "recall":
                        runtime.can_send_for_source = lambda *args, **kwargs: False
                    else:
                        runtime.config.emoji.send_on_regular_reply = False
                    return asset

                runtime._part_proactive_gesture_component._select_emoji_asset_for_intent = select
                await runtime.send_semantic_emoji_if_needed(event)
                await asyncio.gather(*list(runtime._background_scheduler.tasks))
                self.assertEqual(event.sent_messages, [])

    async def test_proactive_emoji_observes_full_scope_snapshot(self):
        runtime, _, asset = await self._emoji_runtime()
        event = self._reply_event(runtime)
        snapshot = runtime._snapshot_proactive_send_event(event.unified_msg_origin, event)

        async def select(*args, **kwargs):
            self._reply_event(runtime, "m2", "先别说这个")
            return asset

        runtime._part_proactive_gesture_component._select_emoji_asset_for_intent = select
        await runtime._send_proactive_emoji_if_needed(
            event.unified_msg_origin,
            {"reply_text": "你又逗我", "expression_intent": {"send_emoji": True}},
            source_event=snapshot,
        )
        self.assertEqual(event.sent_messages, [])

    async def test_idle_delivery_accepts_same_scene_and_rejects_changed_scene(self):
        now = datetime.datetime(2026, 5, 24, 12)
        for latest_mode in ("co_present", "remote"):
            with self.subTest(latest_mode=latest_mode):
                runtime, _ = self._make_proactive_runtime([])
                scope = "aiocqhttp:FriendMessage:10001"
                candidate = {
                    "target_scope": scope, "message": "一起看电影", "message_id": "m1",
                    "sender_id": "10001", "sender_name": "阿林", "platform_name": "aiocqhttp",
                    "is_group": False, "recent_messages": [], "pending_count": 1,
                    "last_activity_at": now - datetime.timedelta(hours=3),
                }
                runtime._proactive_idle_candidates[scope] = candidate
                runtime.evaluate_proactive_reply = AsyncMock(return_value={
                    "should_reply": True, "decision": "reply", "reply_text": "给我留个位置",
                    "_interaction_mode": "co_present",
                    "voice_call_intent": {"should_invite": True, "greeting": "一起看"},
                })
                runtime.resolve_interaction_context = AsyncMock(return_value=types.SimpleNamespace(mode=latest_mode))
                runtime.voice_call = types.SimpleNamespace(proactive_invite_available=lambda scope: True)
                runtime._send_proactive_message = AsyncMock(return_value=True)
                runtime._send_proactive_voice_call_invite = AsyncMock()
                await runtime._evaluate_idle_candidate(scope, candidate, now)
                self.assertEqual(runtime._send_proactive_message.await_count, int(latest_mode == "co_present"))
                runtime._send_proactive_voice_call_invite.assert_not_awaited()

    async def test_unsent_proposal_does_not_start_cooldown(self):
        runtime, _, _ = await self._emoji_runtime()
        scope = "aiocqhttp:FriendMessage:123456"
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        await runtime.archive.save_expression_intent(ExpressionIntentRecord.from_value({
            "scope": scope, "send_emoji": True, "emoji_id": 0, "created_at": now, "emoji_intent": "偷笑",
        }))
        self.assertFalse(await runtime._part_proactive_gesture_component._emoji_send_cooldown_active(scope))
        await runtime.archive.save_expression_intent(ExpressionIntentRecord.from_value({
            "scope": scope, "send_emoji": True, "emoji_id": 1, "created_at": now, "emoji_intent": "偷笑",
        }))
        self.assertTrue(await runtime._part_proactive_gesture_component._emoji_send_cooldown_active(scope))

    async def test_queued_or_slow_emoji_expires(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                runtime, _, _ = await self._emoji_runtime()
                runtime._part_proactive_gesture_component._AUTO_EMOJI_TIMEOUT_SECONDS = 0.01
                cancelled = asyncio.Event()

                async def select(*args, **kwargs):
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

                runtime._part_proactive_gesture_component._select_emoji_asset_for_intent = AsyncMock(side_effect=select)
                gate = runtime._background_scheduler.normal_gate
                if queued:
                    for _ in range(4):
                        await gate.acquire()
                event = self._reply_event(runtime)
                await runtime.send_semantic_emoji_if_needed(event)
                if queued:
                    await asyncio.sleep(0.02)
                    for _ in range(4):
                        gate.release()
                await asyncio.wait_for(asyncio.gather(*list(runtime._background_scheduler.tasks)), 0.3)
                self.assertEqual(event.sent_messages, [])
                self.assertEqual(runtime._part_proactive_gesture_component._select_emoji_asset_for_intent.await_count, 0 if queued else 1)
                self.assertEqual(cancelled.is_set(), not queued)

    async def test_concurrent_regular_and_proactive_emoji_share_cooldown(self):
        runtime, _, asset = await self._emoji_runtime()
        event = self._reply_event(runtime)
        selected = 0
        both_selected = asyncio.Event()

        async def select(*args, **kwargs):
            nonlocal selected
            selected += 1
            if selected == 2:
                both_selected.set()
            await both_selected.wait()
            return asset

        runtime._part_proactive_gesture_component._select_emoji_asset_for_intent = select
        await runtime.send_semantic_emoji_if_needed(event)
        await asyncio.wait_for(runtime._send_proactive_emoji_if_needed(
            event.unified_msg_origin,
            {"reply_text": "你又逗我", "expression_intent": {"send_emoji": True}},
            source_event=event, source_message_id="proactive-m1",
        ), 0.3)
        await asyncio.gather(*list(runtime._background_scheduler.tasks))
        self.assertEqual(len(event.sent_messages), 1)

    async def test_co_present_revisit_sends_and_changed_mode_cancels(self):
        for changed, should_reply in ((False, True), (True, True), (False, False)):
            with self.subTest(changed=changed, should_reply=should_reply):
                runtime, provider = self._make_proactive_runtime([json.dumps({
                    "should_reply": should_reply, "expression_review": {"passed": True},
                    "confidence": 0.95, "benefit": 90, "timeliness": 90, "continuity": 90,
                    "disruption": 5, "uncertainty": 5, "decision": "reply" if should_reply else "observe",
                    "reply_text": "给我留个位置" if should_reply else "", "reason": "想一起看电影",
                }, ensure_ascii=False)])
                runtime.config.chat_style.enabled = False
                runtime.config.proactive.private_revisit_enabled = True
                scope = "aiocqhttp:FriendMessage:10001"
                await runtime.archive.touch_relationship(
                    "10001", name="阿林", note="一起在客厅看电影", date_str="2026-05-24",
                    platform="aiocqhttp", user_id="10001", contact_type="friend", target_scope=scope,
                )
                mode = "co_present"

                async def interaction(**kwargs):
                    return types.SimpleNamespace(mode=mode, mode_label="同处现场", has_authoritative_mode=True)

                async def audit(**kwargs):
                    nonlocal mode
                    if changed:
                        mode = "remote"
                    return True, "场景一致"

                runtime.resolve_interaction_context = interaction
                runtime._audit_proactive_continuity = audit
                now = datetime.datetime(2026, 5, 24, 12)
                with patch("core.runtime.proactive.revisit.life_now", return_value=now):
                    await runtime.evaluate_private_revisit_candidates()
                self.assertEqual(len(provider.prompts), 1)
                self.assertIn("现实互动方式：同处现场", provider.prompts[0])
                self.assertEqual(len(runtime.context.sent_messages), int(should_reply and not changed))

