"""验证普通回复、闲时续话和随心裁定之间的异步边界。"""

import asyncio
import datetime
import unittest
from unittest.mock import AsyncMock, patch

from runtimehelpers import Event, LifeSettings, RuntimeAsyncHelpers


class ProactiveTimingTest(RuntimeAsyncHelpers, unittest.IsolatedAsyncioTestCase):
    def private_event(self, text="刚才的电影还挺值得聊聊", message_id="m1"):
        event = Event(
            sender_id="10001",
            unified_msg_origin="aiocqhttp:FriendMessage:10001",
            message_id=message_id,
        )
        event.message_str = text
        return event

    async def test_normal_reply_replaces_timer_and_evaluates_once(self):
        runtime, _ = self._make_proactive_runtime([])
        runtime._runtime_initialized = True
        runtime._part_proactive_scope_component._proactive_candidate_delay_seconds = lambda *args: 0.02
        evaluated = asyncio.Event()
        observed = []

        async def evaluate(key, candidate, now):
            observed.append(dict(candidate))
            runtime._proactive_idle_candidates.pop(key)
            evaluated.set()

        runtime._evaluate_idle_candidate = evaluate
        event = self.private_event()
        key = event.unified_msg_origin
        base = datetime.datetime(2026, 5, 24, 12)
        try:
            runtime.note_proactive_activity(event, now=base)
            original_task = runtime._proactive_idle_tasks[key]
            runtime.note_proactive_bot_reply(event, now=base + datetime.timedelta(minutes=1))
            await asyncio.wait_for(evaluated.wait(), 1)
            self.assertTrue(original_task.cancelled())
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["state"], "after_ordinary_reply")
            self.assertEqual(observed[0]["pending_count"], 0)
            self.assertEqual(observed[0]["next_evaluation_at"], base + datetime.timedelta(minutes=61))
            self.assertEqual(runtime._proactive_idle_tasks, {})
        finally:
            await runtime._cancel_proactive_idle_tasks()

    async def test_short_follow_up_updates_context_and_silence_window(self):
        runtime, _ = self._make_proactive_runtime([])
        base = datetime.datetime(2026, 5, 24, 12)
        event = self.private_event()
        key = event.unified_msg_origin
        runtime.note_proactive_activity(self.private_event("嗯"), now=base)
        self.assertNotIn(key, runtime._proactive_idle_candidates)
        runtime.note_proactive_activity(event, now=base)
        runtime.note_proactive_bot_reply(event, now=base + datetime.timedelta(minutes=1))
        runtime.note_proactive_activity(self.private_event("嗯", "m2"), now=base + datetime.timedelta(minutes=30))
        candidate = runtime._proactive_idle_candidates[key]
        self.assertEqual(candidate["message"], "嗯")
        self.assertEqual(candidate["pending_count"], 1)
        self.assertEqual(candidate["recent_messages"][-1]["content"], "嗯")
        self.assertEqual(candidate["last_bot_reply_at"], base + datetime.timedelta(minutes=1))
        self.assertEqual(candidate["next_evaluation_at"], base + datetime.timedelta(minutes=90))
        self.assertFalse(runtime._part_proactive_flow_component._idle_candidate_ready(candidate, base + datetime.timedelta(minutes=70)))
        self.assertTrue(runtime._proactive_allowed_for_event(runtime._proactive_candidate_event(candidate), continuing=True))

    async def test_normal_reply_invalidates_inflight_idle_decision(self):
        runtime, _ = self._make_proactive_runtime([])
        event = self.private_event()
        key = event.unified_msg_origin
        base = datetime.datetime(2026, 5, 24, 12)
        runtime.note_proactive_activity(event, now=base)
        original = runtime._proactive_idle_candidates[key]

        async def decide(*args, **kwargs):
            runtime.note_proactive_bot_reply(event, now=base + datetime.timedelta(minutes=61))
            return {"should_reply": True, "decision": "reply", "reply_text": "已过时的续话"}

        runtime.evaluate_proactive_reply = decide
        runtime._send_proactive_message = AsyncMock(return_value=True)
        await runtime._evaluate_idle_candidate(key, original, base + datetime.timedelta(minutes=60))
        runtime._send_proactive_message.assert_not_awaited()
        self.assertIsNot(runtime._proactive_idle_candidates[key], original)
        self.assertEqual(runtime._proactive_lifecycle_snapshot(key)["state"], "candidate")

    async def test_old_send_completion_does_not_remove_new_candidate(self):
        for sent in (False, True):
            with self.subTest(sent=sent):
                runtime, _ = self._make_proactive_runtime([])
                event = self.private_event()
                key = event.unified_msg_origin
                base = datetime.datetime(2026, 5, 24, 12)
                runtime.note_proactive_activity(event, now=base)
                original = runtime._proactive_idle_candidates[key]
                runtime.evaluate_proactive_reply = AsyncMock(return_value={
                    "should_reply": True, "decision": "reply", "reply_text": "旧续话",
                })

                async def send(*args, **kwargs):
                    runtime.note_proactive_activity(self.private_event("另一件事", "m2"), now=base + datetime.timedelta(minutes=61))
                    return sent

                runtime._send_proactive_message = send
                runtime._commit_proactive_decision = AsyncMock()
                runtime._save_pending_reply_effect = AsyncMock()
                runtime._track_proactive_reply_effect = lambda *args: None
                await runtime._evaluate_idle_candidate(key, original, base + datetime.timedelta(minutes=60))
                self.assertEqual(runtime._proactive_idle_candidates[key]["message_id"], "m2")
                self.assertEqual(runtime._proactive_lifecycle_snapshot(key)["state"], "candidate")

    async def test_idle_reply_can_send_after_normal_reply(self):
        runtime, _ = self._make_proactive_runtime([
            '{"should_reply":true,"expression_review":{"passed":true},"confidence":0.95,'
            '"benefit":90,"timeliness":90,"continuity":90,"disruption":10,"uncertainty":10,'
            '"decision":"reply","reply_text":"刚看到一张那个电影的幕后照","reason":"有新的话题延续"}'
        ])
        event = self.private_event()
        base = datetime.datetime(2026, 5, 24, 12)
        runtime.note_proactive_activity(event, now=base)
        runtime.note_proactive_bot_reply(event, now=base + datetime.timedelta(minutes=1))
        await runtime.evaluate_idle_proactive_candidates(now=base + datetime.timedelta(minutes=60))
        self.assertEqual(runtime.context.sent_messages, [])
        await runtime.evaluate_idle_proactive_candidates(now=base + datetime.timedelta(minutes=62))
        self.assertEqual(len(runtime.context.sent_messages), 1)
        self.assertEqual(runtime.context.sent_messages[0][1].items, ["刚看到一张那个电影的幕后照"])
        self.assertNotIn(event.unified_msg_origin, runtime._proactive_idle_candidates)

    async def test_semantic_decision_survives_old_deadline_without_blocking_loop(self):
        runtime, _ = self._make_proactive_runtime([])
        runtime._init_response_gate_state()
        runtime.config = LifeSettings.from_dict({"response_gate_config": {"private_enabled": True}})
        progressed = asyncio.Event()

        async def model(*args, **kwargs):
            await asyncio.sleep(2.6)
            return '{"action":"observe","confidence":0.95,"reason":"话题已自然结束"}'

        async def other_conversation():
            await asyncio.sleep(0.02)
            progressed.set()

        runtime.call_text_model = model
        other = asyncio.create_task(other_conversation())
        task = asyncio.create_task(runtime.apply_response_gate_for_event(self.private_event()))
        await asyncio.wait_for(progressed.wait(), 0.5)
        self.assertFalse(task.done())
        decision = await task
        await other
        self.assertEqual(decision["action"], "observe")
        self.assertTrue(decision["semantic"])
        self.assertEqual(runtime._response_gate_semantic_metrics["timeout"], 0)
        self.assertEqual(runtime._response_gate_semantic_metrics["fallback"], 0)

    async def test_timeout_cancels_model_and_closes_session(self):
        runtime, _ = self._make_proactive_runtime([])
        runtime._init_response_gate_state()
        cancelled = asyncio.Event()

        async def model(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        runtime.call_text_model = model
        runtime.close_text_session = AsyncMock()
        with patch("core.runtime.choice.apply.RESPONSE_GATE_SEMANTIC_TIMEOUT_SECONDS", 0.02):
            result = await runtime._part_response_gate_apply_component._response_gate_semantic_decision(
                self.private_event(), score=0.9, reasons=[], pending_count=1, wait_state=None,
            )
        self.assertIsNone(result)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(runtime._response_gate_semantic_metrics["timeout"], 1)
        runtime.close_text_session.assert_awaited_once()

    async def test_stale_semantic_result_cannot_change_interaction(self):
        runtime, _ = self._make_proactive_runtime([])
        runtime._init_response_gate_state()
        runtime.apply_interaction_turn_decision = AsyncMock()

        async def model(*args, **kwargs):
            runtime._part_response_gate_apply_component._response_gate_superseded_decision = lambda event: {"superseded": True}
            return '{"action":"reply","confidence":0.95,"interaction":{"decision":"set","mode":"co_present","confidence":0.95}}'

        runtime.call_text_model = model
        result = await runtime._part_response_gate_apply_component._response_gate_semantic_decision(
            self.private_event(), score=0.9, reasons=[], pending_count=1, wait_state=None,
        )
        self.assertIsNone(result)
        runtime.apply_interaction_turn_decision.assert_not_awaited()

    async def test_semantic_prompt_includes_recent_assistant_reply(self):
        runtime, provider = self._make_proactive_runtime(['{"action":"observe","confidence":0.95}'])
        runtime._init_response_gate_state()
        runtime.structured_recent_history_messages = lambda *args, **kwargs: [
            {"role": "assistant", "name": "角色", "content": "那我们晚上再聊", "message_id": "a1"},
            {"role": "user", "content": "好", "message_id": "m1"},
        ]
        await runtime._part_response_gate_apply_component._response_gate_semantic_decision(
            self.private_event("好"), score=0.9, reasons=[], pending_count=1, wait_state=None,
        )
        self.assertIn('"role": "assistant"', provider.prompts[0])
        self.assertIn("那我们晚上再聊", provider.prompts[0])


if __name__ == "__main__":
    unittest.main()
