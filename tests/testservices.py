"""独立服务的隔离、清理和跨配置任务租约回归。"""

import asyncio
import datetime
import types
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from support import DailyLifeRuntime, Event
from core.runtime.elapsed import ConversationTimingService, RECEIVED_AT
from core.runtime.scopes import RuntimeScopeState
from testauditrepairs import _services
from testgeneration import GenerationRuntime
from testoutbound import new_service


class ServiceLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_cancels_generation_and_rejects_new_work(self):
        runtime = GenerationRuntime()
        date = datetime.datetime(2026, 7, 14)
        caller = asyncio.create_task(runtime.run_daily_generation(
            date=date, source="dashboard_reset", force=True, use_web=True,
        ))
        await asyncio.wait_for(runtime.composer.search_started.wait(), 1)
        await runtime.daily_generation.close()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(runtime.daily_generation._active, {})
        self.assertFalse(runtime.daily_generation_status("2026-07-14")["running"])
        self.assertEqual(runtime.composer.generate_calls, [])
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            await runtime.run_daily_generation(date=date, source="startup_seed", force=False)
        await runtime.daily_generation.close()

    async def test_close_before_task_starts_still_records_terminal_status(self):
        runtime = GenerationRuntime()
        caller = asyncio.create_task(runtime.run_daily_generation(
            date=datetime.datetime(2026, 7, 14), source="dashboard_reset", force=True,
        ))
        await asyncio.sleep(0)
        self.assertTrue(runtime.daily_generation_status("2026-07-14")["running"])
        await runtime.daily_generation.close()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(runtime.daily_generation_status("2026-07-14")["running"])
        self.assertEqual(runtime.daily_generation._active, {})
        self.assertEqual(runtime.composer.generate_calls, [])

    async def test_failed_lease_does_not_leave_generation_running(self):
        runtime = GenerationRuntime()

        @asynccontextmanager
        async def unavailable_lease():
            raise RuntimeError("服务不可用")
            yield

        runtime.daily_generation._lease_factory = unavailable_lease
        with self.assertRaisesRegex(RuntimeError, "服务不可用"):
            await runtime.run_daily_generation(
                date=datetime.datetime(2026, 7, 14), source="startup_seed", force=False,
            )
        self.assertEqual(runtime.daily_generation_status("2026-07-14")["phase"], "failed")
        self.assertEqual(runtime.daily_generation._active, {})

    async def test_generation_history_is_bounded_and_instances_are_isolated(self):
        first, second = GenerationRuntime(), GenerationRuntime()
        first.daily_generation.HISTORY_LIMIT = 2
        for offset in range(3):
            await first.run_daily_generation(
                date=datetime.datetime(2026, 7, 14) + datetime.timedelta(days=offset),
                source="dashboard_reset", force=True,
            )
        self.assertEqual(first.daily_generation_status("2026-07-14"), {})
        self.assertEqual(first.daily_generation_status("2026-07-16")["phase"], "completed")
        self.assertEqual(second.daily_generation_status("2026-07-16"), {})

    async def test_receipt_eviction_uses_service_callback_and_preserves_other_scope(self):
        timing = ConversationTimingService(
            archive=lambda: None,
            is_self_message=lambda event: False,
            session_id=lambda event: event.unified_msg_origin,
            is_group=lambda event: False,
        )
        scopes = RuntimeScopeState(
            types.SimpleNamespace(), max_scopes=1, evict_services=timing.evict_scopes,
        )
        first = Event(unified_msg_origin="test:FriendMessage:1")
        second = Event(unified_msg_origin="test:FriendMessage:2")
        scopes.note_event(first)
        timing.note_conversation_sent(first.unified_msg_origin, 100)
        timing.note_conversation_sent(second.unified_msg_origin, 100)
        scopes.note_event(second)
        for event in (first, second):
            setattr(event, RECEIVED_AT, 160)
        self.assertIn("间隔未知", await timing.build_conversation_timing_context(first))
        self.assertIn("1分钟（60秒）", await timing.build_conversation_timing_context(second))
        scopes.clear()
        fresh = Event(unified_msg_origin=second.unified_msg_origin)
        setattr(fresh, RECEIVED_AT, 170)
        self.assertIn("间隔未知", await timing.build_conversation_timing_context(fresh))

    async def test_outbound_identity_cache_is_local_and_can_be_cleared(self):
        first, second = new_service(), new_service()
        event = Event(self_id="bot")
        event.get_self_name = lambda: "名字甲"
        self.assertEqual(await first.prepare_outbound_bot_name(source_event=event), "名字甲/bot")
        event.get_self_name = lambda: "名字乙"
        self.assertEqual(await second.prepare_outbound_bot_name(source_event=event), "名字乙/bot")
        self.assertEqual(await first.prepare_outbound_bot_name(source_event=event), "名字甲/bot")
        first.clear()
        self.assertEqual(await first.prepare_outbound_bot_name(source_event=event), "名字乙/bot")

    async def test_scope_eviction_clears_all_owned_services_without_creating_them(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.context = types.SimpleNamespace()
        runtime.scope_state = RuntimeScopeState(
            runtime, max_scopes=1, evict_services=runtime._evict_scope_services,
        )
        first = Event(unified_msg_origin="test:FriendMessage:1")
        second = Event(unified_msg_origin="test:FriendMessage:2")
        runtime.scope_state.note_event(first)
        runtime.scope_state.note_event(second)
        self.assertNotIn("conversation_timing", runtime.__dict__)
        self.assertNotIn("text_forward", runtime.__dict__)

        runtime.scope_state.note_event(first)
        runtime.log_outbound_message_async = AsyncMock(return_value=True)
        for event in (first, second):
            runtime.note_conversation_sent(event.unified_msg_origin, 100)
            event._daily_life_t2i_default_send = True
            event._daily_life_t2i_source_text = event.unified_msg_origin
            event.set_result(event.chain_result([{"type": "image", "file": "result.png"}]))
            self.assertTrue(runtime.note_t2i_image_sent(event))

        runtime.scope_state.note_event(second)
        self.assertNotIn(first.unified_msg_origin, runtime.text_forward._records)
        self.assertNotIn(first.unified_msg_origin, runtime.conversation_timing._receipts)
        self.assertIn(second.unified_msg_origin, runtime.text_forward._records)
        self.assertIn(second.unified_msg_origin, runtime.conversation_timing._receipts)
        runtime.scope_state.clear()
        self.assertEqual(runtime.text_forward._records, {})
        self.assertEqual(runtime.conversation_timing._receipts, {})

    async def test_runtime_text_forward_facade_logs_only_successful_delivery(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.context = types.SimpleNamespace(get_config=lambda scope: {
            "t2i": True, "t2i_word_threshold": 50,
        })
        event = Event()
        text = "原始长文本" * 20
        event.set_result(event.chain_result([text]))
        self.assertTrue(runtime.capture_t2i_source_before_send(event))
        event.set_result(event.chain_result([{"type": "image", "file": "result.png"}]))
        self.assertTrue(runtime.note_t2i_image_sent(event))
        runtime.log_outbound_message_async = AsyncMock(return_value=True)

        await runtime.forward_t2i_text(event)

        self.assertEqual(str(event.sent_messages[0].chain[0].nodes[0].content[0]), text)
        runtime.log_outbound_message_async.assert_awaited_once()

    async def test_generation_keeps_old_services_after_frontend_cancellation_and_swap(self):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        old, new = _services("07:00", True), _services("09:30")
        runtime._install_runtime_services(old)
        runtime.raw_config = {"rhythm_config": {"schedule_time": "07:00"}}
        runtime.generation_lock = asyncio.Lock()
        runtime._prune_disabled_proactive_candidates = lambda: None
        runtime._build_runtime_services = lambda *_: new
        runtime.archive = types.SimpleNamespace(get_day=AsyncMock(return_value=None))
        runtime.mark_page_status_changed = AsyncMock()
        runtime.get_persona_text = AsyncMock(return_value="测试角色")

        async def close(services):
            services.media.closed = True

        runtime._close_runtime_services = close
        entered, release = asyncio.Event(), asyncio.Event()

        async def search(*args, **kwargs):
            entered.set()
            await release.wait()
            self.assertIs(runtime.composer, old.composer)
            self.assertFalse(old.media.closed)
            return "生活灵感"

        day = types.SimpleNamespace(date="2026-07-14", timeline=[], meta={})
        old.composer = types.SimpleNamespace(
            search=types.SimpleNamespace(inspiration=search),
            generate_daily=AsyncMock(return_value=day),
        )
        new.composer = types.SimpleNamespace(generate_daily=AsyncMock())

        async def frontend():
            async with runtime.runtime_service_lease():
                return await runtime.run_daily_generation(
                    date=datetime.datetime(2026, 7, 14),
                    source="dashboard_reset", force=True, use_web=True,
                )

        caller = asyncio.create_task(frontend())
        await asyncio.wait_for(entered.wait(), 1)
        backend = runtime.daily_generation._active[day.date].task
        try:
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            await asyncio.wait_for(runtime.apply_config({
                "rhythm_config": {"schedule_time": "09:30"},
            }), 1)
            self.assertFalse(old.media.closed)
            self.assertIs(runtime.composer, new.composer)
            release.set()
            result = await asyncio.wait_for(backend, 1)
            self.assertIs(result.day, day)
            old.composer.generate_daily.assert_awaited_once()
            new.composer.generate_daily.assert_not_awaited()
            self.assertTrue(old.media.closed)
            self.assertEqual(runtime._service_users, 0)
        finally:
            release.set()
            await runtime.daily_generation.close()
