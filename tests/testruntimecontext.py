import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import DailyLifeRuntime, Event

from core.runtime.context import (
    ContextSnapshotRepository,
    InteractionContextService,
    interaction_fact_is_current,
)


class InteractionFixture:
    def __init__(self, facts):
        self.writes = []
        self.archive = SimpleNamespace(
            get_temporal_facts=lambda **kwargs: self._facts(facts, kwargs),
            get_current_temporal_fact=lambda *args: self._current_fact(facts, args),
            write_temporal_fact=self._write_fact,
        )
        self.service = InteractionContextService(
            archive=lambda: self.archive,
            session_id=self._event_session_id,
            profile_id=self._event_profile_id,
            message_id=lambda event: str(getattr(event, "message_id", "") or ""),
            is_group=self._event_is_group_message,
            turn_messages=lambda event: getattr(event, "turn_messages", ()),
        )

    @staticmethod
    async def _facts(facts, kwargs):
        return list(facts)

    @staticmethod
    async def _current_fact(facts, args):
        return facts[0] if facts else None

    async def _write_fact(self, operation, payload):
        self.writes.append((operation, payload))
        return SimpleNamespace(**payload)

    @staticmethod
    def _event_session_id(event):
        return str(getattr(event, "unified_msg_origin", "") or "")

    @staticmethod
    def _event_profile_id(event):
        return str(getattr(event, "sender_id", "") or "")

    @staticmethod
    def _event_is_group_message(event):
        return ":GroupMessage:" in str(getattr(event, "unified_msg_origin", "") or "")


class ContextSnapshotRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_repository_delegates_to_archive_contract(self):
        class Archive:
            def __init__(self):
                self.calls = []

            async def get_context_snapshot(self, **kwargs):
                self.calls.append(kwargs)
                return {"relationships": []}

        archive = Archive()
        repository = ContextSnapshotRepository(archive)

        result = await repository.read(
            max_summaries=6,
            experience_scope="test-scope",
            session_id="test-session",
        )

        self.assertEqual(result, {"relationships": []})
        self.assertEqual(
            archive.calls,
            [
                {
                    "max_summaries": 6,
                    "experience_scope": "test-scope",
                    "session_id": "test-session",
                }
            ],
        )

    async def test_repository_rejects_archive_without_snapshot_contract(self):
        with self.assertRaisesRegex(TypeError, "缺少上下文快照读取能力"):
            ContextSnapshotRepository(object())

    async def test_interaction_context_separates_transport_from_real_world_mode(self):
        fact = SimpleNamespace(
            predicate="interaction_mode",
            subject="u1",
            object_value={"mode": "co_present"},
            confidence=0.92,
            observed_at="2026-08-13 12:00:00",
        )
        runtime = InteractionFixture([fact])
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1",
            sender_id="u1",
        )

        context = await runtime.service.resolve(
            event=event,
            now=datetime.datetime(2026, 8, 13, 12, 30),
        )

        self.assertEqual(context.transport, "私聊")
        self.assertEqual(context.mode, "co_present")
        prompt = context.format_for_generation()
        self.assertIn("只描述平台承载方式，不代表双方现实距离", prompt)
        self.assertIn("按面对面交流回应", prompt)

    async def test_proactive_replay_does_not_turn_latest_fact_into_pending(self):
        fact = SimpleNamespace(
            predicate="interaction_mode",
            subject="u1",
            object_value={"mode": "co_present"},
            confidence=0.92,
            observed_at="2026-08-13 12:00:00",
        )
        runtime = InteractionFixture([fact])
        replay = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1",
            sender_id="u1",
            message_str="在忙什么呢",
            is_proactive_synthetic=True,
        )

        context = await runtime.service.resolve(
            event=replay,
            now=datetime.datetime(2026, 8, 13, 12, 30),
        )

        self.assertEqual(context.mode, "co_present")
        self.assertFalse(context.pending_current)

    async def test_stale_interaction_fact_falls_back_to_unknown(self):
        fact = SimpleNamespace(
            predicate="interaction_mode",
            subject="u1",
            object_value={"mode": "co_present"},
            confidence=0.95,
            observed_at="2026-08-13 08:00:00",
        )
        runtime = InteractionFixture([fact])

        context = await runtime.service.resolve(
            target_scope="aiocqhttp:FriendMessage:u1",
            now=datetime.datetime(2026, 8, 13, 12, 1),
        )

        self.assertEqual(context.mode, "unknown")
        self.assertFalse(
            interaction_fact_is_current(
                fact,
                now=datetime.datetime(2026, 8, 13, 12, 1),
            )
        )

    async def test_current_turn_does_not_treat_previous_remote_fact_as_current(self):
        fact = SimpleNamespace(
            predicate="interaction_mode",
            subject="u1",
            object_value={"mode": "remote"},
            confidence=0.9,
            observed_at="2026-08-13 12:00:00",
        )
        runtime = InteractionFixture([fact])
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1",
            sender_id="u1",
            message_str="测试对象补充了当前场景",
        )

        context = await runtime.service.resolve(
            event=event,
            now=datetime.datetime(2026, 8, 13, 12, 30),
        )

        self.assertEqual(context.mode, "unknown")
        self.assertEqual(context.previous_mode, "remote")
        self.assertTrue(context.pending_current)
        prompt = context.format_for_generation()
        self.assertIn("这不是当前话轮的结论", prompt)
        self.assertIn("待根据当前完整话轮的语义确认", prompt)
        self.assertIn("历史 assistant 回复只是已经说过的话", prompt)
        self.assertIn("会话摘要和关系记忆只用于回忆话题与关系", prompt)

    async def test_current_semantic_decision_overrides_and_persists_remote_fact(self):
        fact = SimpleNamespace(
            predicate="interaction_mode",
            subject="u1",
            object_value={"mode": "remote"},
            confidence=0.9,
            observed_at="2026-08-13 12:00:00",
        )
        runtime = InteractionFixture([fact])
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1",
            sender_id="u1",
            message_id="synthetic-message",
            message_str="测试对象明确说明双方已经在同一现场",
        )

        decision = await runtime.service.apply_turn_decision(
            event,
            {
                "decision": "set",
                "mode": "co_present",
                "confidence": 0.94,
                "reason": "当前完整话轮明确说明双方同处现场",
            },
            now=datetime.datetime(2026, 8, 13, 12, 31),
        )
        context = await runtime.service.resolve(
            event=event,
            now=datetime.datetime(2026, 8, 13, 12, 31),
        )

        self.assertEqual(decision["mode"], "co_present")
        self.assertEqual(context.mode, "co_present")
        self.assertIn("按面对面交流回应", context.format_for_generation())
        self.assertEqual(runtime.writes[0][0], "UPDATE")
        self.assertEqual(runtime.writes[0][1]["object_value"], {"mode": "co_present"})
        self.assertEqual(runtime.writes[0][1]["source"], "chat_turn_semantic")

    async def test_continuous_turn_content_requires_current_semantic_decision(self):
        fixture = InteractionFixture([])
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1",
            sender_id="u1", message_str="", turn_messages=("补充前半句话",),
        )

        context = await fixture.service.resolve(event=event)

        self.assertTrue(context.pending_current)
        self.assertEqual(context.mode, "unknown")

    async def test_decision_is_local_to_event_and_clear_invalidates_fact(self):
        fact = SimpleNamespace(
            predicate="interaction_mode", subject="u1",
            object_value={"mode": "remote"}, confidence=0.9,
            observed_at="2026-08-13 12:00:00",
        )
        fixture = InteractionFixture([fact])
        first = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1", sender_id="u1",
            message_str="前一轮", message_id="m1",
        )
        second = SimpleNamespace(**first.__dict__)
        now = datetime.datetime(2026, 8, 13, 12, 30)
        await fixture.service.apply_turn_decision(
            first, {"decision": "clear", "confidence": 0.9}, now=now,
        )

        cleared = await fixture.service.resolve(event=first, now=now)
        fresh = await fixture.service.resolve(event=second, now=now)

        self.assertFalse(cleared.pending_current)
        self.assertEqual(cleared.mode, "unknown")
        self.assertTrue(fresh.pending_current)
        self.assertEqual(fresh.previous_mode, "remote")
        operation, payload = fixture.writes[0]
        self.assertEqual(operation, "INVALIDATE")
        self.assertEqual((payload["scope"], payload["subject"], payload["source_id"]),
                         (first.unified_msg_origin, "u1", "m1"))

    async def test_snapshot_filters_profile_without_reading_archive(self):
        fixture = InteractionFixture([])
        fixture.archive.get_temporal_facts = AsyncMock(side_effect=AssertionError("不应重读快照"))
        facts = [
            {"predicate": "interaction_mode", "subject": profile,
             "object_value": {"mode": mode}, "confidence": 0.9,
             "observed_at": observed}
            for profile, mode, observed in (
                ("u2", "remote", "2026-08-13 12:25:00"),
                ("u1", "co_present", "2026-08-13 12:00:00"),
            )
        ]

        context = await fixture.service.resolve(
            target_scope="aiocqhttp:GroupMessage:g1", profile_id="u1",
            snapshot={"temporal_facts": facts},
            now=datetime.datetime(2026, 8, 13, 12, 30),
        )

        self.assertEqual(context.transport, "群聊")
        self.assertEqual(context.mode, "co_present")
        fixture.archive.get_temporal_facts.assert_not_awaited()

    async def test_rejected_decision_does_not_persist_or_resolve_current_turn(self):
        fixture = InteractionFixture([])
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1", sender_id="u1",
            message_str="目前位置不明确",
        )

        decision = await fixture.service.apply_turn_decision(
            event, {"decision": "set", "mode": "remote", "confidence": 0.6},
        )
        context = await fixture.service.resolve(event=event)

        self.assertIsNone(decision)
        self.assertEqual(fixture.writes, [])
        self.assertTrue(context.pending_current)

    async def test_write_failure_keeps_event_decision_and_bad_timestamp_uses_fallback(self):
        fixture = InteractionFixture([])
        fixture.archive.write_temporal_fact = AsyncMock(side_effect=RuntimeError("归档写入失败"))
        event = SimpleNamespace(
            unified_msg_origin="aiocqhttp:FriendMessage:u1", sender_id="u1",
            message_str="同处现场", timestamp={"invalid": True},
        )
        now = datetime.datetime(2026, 8, 13, 12, 30)

        await fixture.service.apply_turn_decision(
            event, {"decision": "set", "mode": "co_present", "confidence": 0.9}, now=now,
        )
        context = await fixture.service.resolve(event=event, now=now)

        self.assertEqual(context.mode, "co_present")
        fixture.archive.write_temporal_fact.assert_awaited_once()
        self.assertEqual(fixture.archive.write_temporal_fact.await_args.args[1]["observed_at"],
                         "2026-08-13T12:30:00")

    async def test_runtime_facade_uses_current_archive_after_service_creation(self):
        fixtures = [InteractionFixture([
            SimpleNamespace(
                predicate="interaction_mode", subject="u1",
                object_value={"mode": mode}, confidence=0.9,
                observed_at="2026-08-13 12:00:00",
            ),
        ]) for mode in ("remote", "co_present")]
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.archive = fixtures[0].archive
        event = Event(unified_msg_origin="aiocqhttp:FriendMessage:u1", sender_id="u1")
        event.message_str = ""
        now = datetime.datetime(2026, 8, 13, 12, 30)
        first = await runtime.resolve_interaction_context(event=event, now=now)
        service = runtime.interaction_context

        runtime.archive = fixtures[1].archive
        second = await runtime.resolve_interaction_context(event=event, now=now)
        await runtime.apply_interaction_turn_decision(
            event, {"decision": "set", "mode": "remote", "confidence": 0.9}, now=now,
        )

        self.assertIs(runtime.interaction_context, service)
        self.assertEqual((first.mode, second.mode), ("remote", "co_present"))
        self.assertEqual(fixtures[0].writes, [])
        self.assertEqual(fixtures[1].writes[0][1]["object_value"], {"mode": "remote"})


if __name__ == "__main__":
    unittest.main()
