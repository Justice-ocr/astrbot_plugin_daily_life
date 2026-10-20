"""撤回服务的隔离、投递、日志和生命周期回归。"""

import asyncio
import types
import unittest
from collections import deque
from unittest.mock import AsyncMock, Mock, patch

from support import DailyLifeRuntime, Event
from core.runtime.recall import RecallService
from core.runtime.scopes import RuntimeScopeState


def notice(scope: str, message_id: str) -> Event:
    event = Event(unified_msg_origin=scope)
    event.message_obj.raw_message = {
        "post_type": "notice", "notice_type": "friend_recall",
        "message_id": message_id,
    }
    return event


class RecallServiceTest(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        self.now = 100.0
        self.remove = Mock()
        self.send_scope = AsyncMock(return_value=True)
        self.log_outbound = AsyncMock(return_value=True)
        return RecallService(
            event_sources=lambda event: [event],
            session_id=lambda event: event.unified_msg_origin,
            message_id=DailyLifeRuntime._event_message_id,
            remove_context=(self.remove,),
            send_scope=self.send_scope, log_outbound=self.log_outbound,
            clock=lambda: self.now,
        )

    async def test_recall_is_scoped_expires_and_does_not_leak_between_instances(self):
        service = self.make_service()
        other = self.make_service()
        self.assertTrue(service.note_recalled_message(notice("scope-a", "42")))
        self.assertTrue(service.message_was_recalled("scope-a", "42"))
        self.assertFalse(service.message_was_recalled("scope-b", "42"))
        self.assertFalse(other.message_was_recalled("scope-a", "42"))
        self.now += 30 * 60
        self.assertFalse(service.message_was_recalled("scope-a", "42"))
        self.assertEqual(service._recalled_messages, {})

    async def test_message_text_cannot_be_mistaken_for_platform_recall_notice(self):
        service = self.make_service()
        event = Event()
        event.message_str = "notice friend_recall message_id 42，撤回这条消息"
        event.message_obj.raw_message = {"post_type": "message", "message_id": "42"}
        self.assertFalse(service.note_recalled_message(event))
        self.remove.assert_not_called()

    async def test_recalled_source_cancels_event_and_scope_delivery_without_receipt(self):
        service = self.make_service()
        event = Event(unified_msg_origin="scope-a", message_id="42")
        service.note_recalled_message(notice("scope-a", "42"))
        self.assertFalse(await service.send_message_if_not_recalled(
            "target-other", ["回复"], source_event=event,
        ))
        self.assertFalse(await service.send_message_if_not_recalled(
            "scope-a", ["回复"], source_message_id="42",
        ))
        self.assertEqual(event.sent_messages, [])
        self.send_scope.assert_not_awaited()
        self.log_outbound.assert_not_awaited()

    async def test_event_delivery_awaits_receipt_and_leaves_other_tasks_runnable(self):
        service = self.make_service()
        entered, release = asyncio.Event(), asyncio.Event()

        async def log(*args, **kwargs):
            entered.set()
            await release.wait()
            return True

        self.log_outbound.side_effect = log
        event = Event(unified_msg_origin="scope-a")
        chain = ["发送内容"]
        task = asyncio.create_task(service.send_message_if_not_recalled(
            "scope-a", chain, source_event=event,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(event.sent_messages, [chain])
            self.assertFalse(task.done())
            self.send_scope.assert_not_awaited()
        finally:
            release.set()
            await asyncio.wait_for(task, 1)
        self.log_outbound.assert_awaited_once_with(
            chain, scope="scope-a", source_event=event, source="direct",
        )

    async def test_failed_delivery_and_cancelled_send_do_not_record_receipts(self):
        service = self.make_service()
        self.send_scope.return_value = False
        self.assertFalse(await service.send_message_if_not_recalled("scope-a", []))
        self.log_outbound.assert_not_awaited()
        event = Event()
        event.send = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await service.send_message_if_not_recalled("scope-a", [], source_event=event)
        self.log_outbound.assert_not_awaited()

    async def test_background_snapshot_uses_scope_delivery_and_respects_log_option(self):
        service = self.make_service()
        event = Event(unified_msg_origin="scope-a")
        event._daily_life_media_scope_delivery = True
        chain = ["视频"]
        self.assertTrue(await service.send_message_if_not_recalled(
            "scope-a", chain, source_event=event, raise_delivery_errors=True,
        ))
        self.send_scope.assert_awaited_once_with(
            "scope-a", chain, raise_delivery_errors=True,
        )
        self.log_outbound.assert_awaited_once_with(chain, scope="scope-a", source="direct")
        self.assertEqual(event.sent_messages, [])
        self.log_outbound.reset_mock()
        self.assertTrue(await service.send_message_if_not_recalled(
            "scope-a", chain, log_outbound=False,
        ))
        self.log_outbound.assert_not_awaited()

    async def test_scope_eviction_clears_only_owned_recall_and_skip_logs(self):
        service = self.make_service()
        for scope in ("scope-a", "scope-b"):
            service.note_recalled_message(notice(scope, "42"))
            service.log_recalled_history_skip(Event(unified_msg_origin=scope, message_id="42"))
        service.evict_scopes({"scope-a"})
        self.assertFalse(service.message_was_recalled("scope-a", "42"))
        self.assertTrue(service.message_was_recalled("scope-b", "42"))
        self.assertEqual(set(service._skip_logged), {("scope-b", "42")})
        service.clear()
        self.assertEqual(service._recalled_messages, {})
        self.assertEqual(service._skip_logged, {})

    async def test_runtime_recall_preserves_other_session_and_notifies_context_owners(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.remove_recalled_sight_context = Mock()
        runtime._part_proactive_scope_component._cancel_proactive_idle_task = Mock()
        scope = "platform:FriendMessage:a"
        other = "platform:FriendMessage:b"
        current = Event(unified_msg_origin=scope, message_id="42")
        foreign = Event(unified_msg_origin=other, message_id="42")
        current.message_str = "当前会话的问题"
        foreign.message_str = "其他会话的问题"
        runtime.note_structured_incoming_message(current)
        runtime.note_structured_incoming_message(foreign)
        runtime._proactive_idle_candidates = {
            "current": {"target_scope": scope, "message_id": "42"},
            "following": {"target_scope": scope, "message_id": "43", "pending_count": 2,
                          "recent_messages": [{"message_id": "42"}, {"message_id": "43"}]},
            "other": {"target_scope": other, "message_id": "43", "pending_count": 1,
                      "recent_messages": [{"message_id": "42"}]},
        }
        runtime.note_recalled_message(notice(scope, "42"))
        self.assertEqual(list(runtime._structured_scope_messages(scope)), [])
        self.assertIsInstance(runtime._structured_scope_messages(scope), deque)
        self.assertEqual(len(runtime._structured_scope_messages(other)), 1)
        self.assertNotIn("current", runtime._proactive_idle_candidates)
        self.assertEqual(runtime._proactive_idle_candidates["following"]["pending_count"], 1)
        self.assertEqual(runtime._proactive_idle_candidates["other"]["recent_messages"],
                         [{"message_id": "42"}])
        runtime._part_proactive_scope_component._cancel_proactive_idle_task.assert_called_once_with("current")
        runtime.remove_recalled_sight_context.assert_called_once_with(scope, "42")

    async def test_scope_manager_evicts_recall_without_creating_unneeded_services(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.remove_recalled_sight_context = Mock()
        state = RuntimeScopeState(runtime, max_scopes=1, evict_services=runtime._evict_scope_services)
        first, second = Event(unified_msg_origin="scope-a"), Event(unified_msg_origin="scope-b")
        state.note_event(first)
        state.note_event(second)
        self.assertNotIn("recall", runtime.__dict__)
        state.note_event(first)
        runtime.note_recalled_message(notice("scope-a", "42"))
        runtime.recall.log_recalled_history_skip(Event(unified_msg_origin="scope-a", message_id="42"))
        state.note_event(second)
        self.assertFalse(runtime._message_was_recalled("scope-a", "42"))
        self.assertEqual(runtime.recall._skip_logged, {})

    async def test_runtime_send_uses_current_async_logger_after_service_creation(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.context = types.SimpleNamespace()
        service = runtime.recall
        runtime.log_outbound_message_async = AsyncMock(return_value=True)
        event = Event()
        self.assertTrue(await runtime.send_message_if_not_recalled(
            event.unified_msg_origin, ["内容"], source_event=event,
        ))
        self.assertIs(runtime.recall, service)
        runtime.log_outbound_message_async.assert_awaited_once()

    async def test_recall_skip_log_keys_do_not_collide_on_delimiters(self):
        service = self.make_service()
        with patch("core.runtime.recall.logger") as log:
            service.log_recalled_history_skip(Event(unified_msg_origin="a:b", message_id="c"))
            service.log_recalled_history_skip(Event(unified_msg_origin="a", message_id="b:c"))
            service.log_recalled_history_skip(Event(unified_msg_origin="a", message_id="b:c"))
        self.assertEqual(log.debug.call_count, 2)
