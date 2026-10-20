import datetime
import json
import unittest
from unittest.mock import AsyncMock

from support import TimelineItem
import testcontinuouslife as fixture
from core.life.actionchain import actions
from core.life.planalignment import prepare_updates
from core.models import LifeActionIntent


class PlanAlignmentTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ContinuousLifeTest.asyncSetUp
    asyncTearDown = fixture.ContinuousLifeTest.asyncTearDown
    tick = fixture.ContinuousLifeTest.tick

    async def evening(self, kind="rest"):
        self.runtime.now = self.now.replace(hour=21, minute=56)
        day = await self.archive.get_day(self.runtime.date)
        day.timeline = [
            TimelineItem(time="21:30", activity="已实际洗漱", execution_state="completed"),
            TimelineItem(time="22:39", activity="靠在床头读书", duration_minutes=45),
            TimelineItem(time="23:24", activity="放下手机安稳入睡", duration_minutes=60),
        ]
        action = LifeActionIntent(action_id="planned-sleep", action_type=kind,
                                 target="放下手机安稳入睡", timeline_index=2, duration_minutes=60)
        day.meta["planned_life_actions"] = json.dumps([action.as_dict()], ensure_ascii=False)
        await self.archive.save_day(day)
        return action

    def edit(self, index, operation, **fields):
        return {"timeline_index": index, "operation": operation,
                "reason": "已完成准备，按实际状态调整后续安排", "source_ids": ["persona"], **fields}

    async def decide(self, **fields):
        day = await self.archive.get_day(self.runtime.date)
        world = await self.archive.get_continuous_life()
        await self.runtime.continuous_life._apply_continuous_decision(
            day, world, {"decision": "start", "reason": "依据真实生活状态作出选择",
                         "_sources": {"persona"}, **fields}, self.runtime.now,
        )
        return await self.archive.get_day(day.date), await self.archive.get_continuous_life()

    async def test_early_sleep_retimes_plan_and_skips_unexecuted_reading(self):
        action = await self.evening()
        await self.archive.mutate_day(self.runtime.date, lambda day: day.meta.update(schedule_anchors="旧时间轴锚点"))
        day, world = await self.decide(
            action_id=action.action_id, _candidates=[action.as_dict()], rest_kind="sleep",
            timeline_updates=[self.edit(1, "skip"), self.edit(2, "reschedule", at=str(self.runtime.now))],
        )
        self.assertTrue(world["sleeping"])
        index = world["run"]["action"]["timeline_index"]
        self.assertEqual(day.timeline[index].time, "21:56")
        self.assertEqual(day.timeline[index].execution_state, "active")
        self.assertEqual(day.timeline[0].execution_state, "completed")
        self.assertEqual(next(item for item in day.timeline if "读书" in item.activity).execution_state, "skipped")
        self.assertEqual(await self.archive.get_life_action_receipts(), [])
        self.assertTrue(json.loads(day.meta["schedule_alignment"])["changes"])
        self.assertNotIn("schedule_anchors", day.meta)
        await self.tick(1)
        world = await self.archive.get_continuous_life()
        self.assertEqual(world["run"]["status"], "running")
        self.assertEqual(world["run"]["active_seconds"], 60)

    async def test_future_plan_requires_explicit_retiming(self):
        action = await self.evening()
        day, world = await self.decide(action_id=action.action_id, _candidates=[action.as_dict()])
        self.assertNotIn("run", world)
        self.assertEqual(day.timeline[2].time, "23:24")

    async def test_early_meal_and_outing_use_same_plan_transition(self):
        for kind in ("meal", "move"):
            with self.subTest(kind=kind):
                action = await self.evening(kind)
                self.runtime.continuous_life._validate_action = AsyncMock(return_value=(True, ""))
                self.runtime.continuous_life._validate_contract = lambda *_: ""
                self.runtime.continuous_life._validate_preconditions = lambda *_: ""
                day, world = await self.decide(
                    action_id=action.action_id, _candidates=[action.as_dict()],
                    timeline_updates=[self.edit(2, "reschedule", at=str(self.runtime.now))],
                )
                self.assertEqual(world["run"]["action"]["action_type"], kind)
                self.assertEqual(day.timeline[world["run"]["action"]["timeline_index"]].time, "21:56")
                await self.archive.mutate_continuous_life(day.date, lambda _, stored: stored.pop("run", None))

    async def test_unbound_action_is_visible_and_remaining_steps_stay_unexecuted(self):
        await self.evening()
        day, world = await self.decide(new_actions=[
            {"owner": "self", "action_type": "groom", "target": "洗漱", "duration_minutes": 1},
            {"owner": "self", "action_type": "drink", "target": "喝水", "duration_minutes": 1},
        ])
        current = world["run"]["action"]
        self.assertEqual(day.timeline[current["timeline_index"]].activity, "洗漱")
        remaining = next(item for item in actions(day) if item.action_type == "drink")
        self.assertIsNone(remaining.timeline_index)
        self.assertEqual(await self.archive.get_life_action_receipts(), [])
        await self.tick()
        self.assertEqual(len(await self.archive.get_life_action_receipts()), 1)

    async def test_existing_unbound_run_is_reconciled_without_restarting_or_crediting_gap(self):
        await self.evening()
        started = self.runtime.now - datetime.timedelta(minutes=30)
        action = LifeActionIntent(action_id="already-running", action_type="rest", target="顺着困意入睡", duration_minutes=180)

        def seed(day, world):
            day.meta["planned_life_actions"] = json.dumps([action.as_dict()])
            world["run"] = {"date": day.date, "action": action.as_dict(), "status": "running",
                            "started_at": started.isoformat(), "active_seconds": 120,
                            "rest_kind": "sleep", "reason": "已有执行证据"}
            world["body"]["updated_at"] = self.runtime.now.isoformat()
        await self.archive.mutate_continuous_life(self.runtime.date, seed)
        day, world = await self.decide(decision="continue", timeline_updates=[self.edit(1, "skip"), self.edit(2, "skip")])
        self.assertEqual(world["run"]["started_at"], started.isoformat())
        self.assertEqual(world["run"]["active_seconds"], 120)
        self.assertEqual(world["run"]["action"]["action_id"], action.action_id)
        self.assertEqual(day.timeline[world["run"]["action"]["timeline_index"]].time, "21:26")
        self.assertEqual(await self.archive.get_life_action_receipts(), [])

    async def test_sleep_delay_cannot_be_bypassed_with_schedule_edits(self):
        action = await self.evening()
        await self.archive.mutate_day(self.runtime.date, lambda day: day.meta.update(rest_delay_until="2026-10-07T23:00:00"))
        day, world = await self.decide(
            action_id=action.action_id, _candidates=[action.as_dict()], rest_kind="sleep",
            timeline_updates=[self.edit(1, "skip"), self.edit(2, "reschedule", at=str(self.runtime.now))],
        )
        self.assertNotIn("run", world)
        self.assertEqual(day.timeline[1].execution_state, "planned")
        self.assertEqual(day.timeline[2].time, "23:24")

    async def test_invalid_action_rolls_back_all_schedule_edits(self):
        action = await self.evening()
        self.runtime.continuous_life._validate_preconditions = lambda *_: "条件尚未满足"
        day, world = await self.decide(
            action_id=action.action_id, _candidates=[action.as_dict()],
            timeline_updates=[self.edit(1, "skip"), self.edit(2, "reschedule", at=str(self.runtime.now))],
        )
        self.assertNotIn("run", world)
        self.assertEqual(day.timeline[1].execution_state, "planned")
        self.assertEqual(day.timeline[2].time, "23:24")
        self.assertNotIn("schedule_alignment", day.meta)

    async def test_history_commitments_and_media_are_protected(self):
        action = await self.evening()
        day = await self.archive.get_day(self.runtime.date)
        for field in ("commitment_links", "commitment_ids", "media"):
            with self.subTest(field=field):
                if field == "media":
                    action.action_type = "video"
                    action.payload = {}
                else:
                    action.payload = {field: [1]}
                day.meta["planned_life_actions"] = json.dumps([action.as_dict()])
                value = prepare_updates(day, {}, {"_sources": {"persona"}, "timeline_updates": [self.edit(2, "skip")]}, self.runtime.now)
                self.assertIsNone(value)
        for operation in ("skip", "cancel", "reschedule", "complete"):
            with self.subTest(operation=operation):
                self.assertIsNone(prepare_updates(day, {}, {"_sources": {"persona"}, "timeline_updates": [self.edit(0, operation)]}, self.runtime.now))

    async def test_invalid_evidence_and_partial_batch_cannot_change_plan(self):
        await self.evening()
        day, _ = await self.decide(decision="wait", timeline_updates=[self.edit(1, "skip"), self.edit(2, "skip", source_ids=["fabricated"])])
        self.assertTrue(all(item.execution_state == "planned" for item in day.timeline[1:]))

    async def test_confirmed_invite_and_commitment_edits_are_protected(self):
        action = await self.evening()
        for source in ("invite", "commitment"):
            with self.subTest(source=source):
                action.source = source
                await self.archive.mutate_day(self.runtime.date, lambda day: day.meta.update(planned_life_actions=json.dumps([action.as_dict()])))
                day, _ = await self.decide(decision="wait", timeline_updates=[self.edit(2, "skip")])
                self.assertEqual(day.timeline[2].execution_state, "planned")

    async def test_chat_interruption_can_defer_unstarted_activity(self):
        await self.evening()
        day, _ = await self.decide(decision="wait", timeline_updates=[self.edit(1, "reschedule", at="2026-10-07T23:00:00")])
        self.assertEqual(next(item for item in day.timeline if "读书" in item.activity).time, "23:00")
        self.assertEqual(day.timeline[0].execution_state, "completed")
        self.assertEqual(await self.archive.get_life_action_receipts(), [])

    async def test_concurrent_update_cannot_be_overwritten_by_older_action_decision(self):
        action = await self.evening()
        day = await self.archive.get_day(self.runtime.date)
        snapshot = await self.archive.get_continuous_life()
        await self.archive.mutate_day(day.date, lambda latest: setattr(latest.timeline[1], "activity", "新确认的阅读安排"))
        await self.runtime.continuous_life._apply_continuous_decision(
            day, snapshot, {"decision": "start", "reason": "提前休息", "_sources": {"persona"},
                            "action_id": action.action_id, "_candidates": [action.as_dict()],
                            "timeline_updates": [self.edit(1, "skip"), self.edit(2, "reschedule", at=str(self.runtime.now))]}, self.runtime.now,
        )
        latest = await self.archive.get_day(day.date)
        self.assertEqual(latest.timeline[1].activity, "新确认的阅读安排")
        self.assertEqual(latest.timeline[1].execution_state, "planned")
        self.assertNotIn("run", await self.archive.get_continuous_life())

    async def test_cross_midnight_retiming_preserves_business_day_and_action_binding(self):
        action = await self.evening()
        self.runtime.now = datetime.datetime(2026, 10, 8, 0, 30)
        await self.archive.mutate_day(self.runtime.date, lambda day: setattr(day.timeline[2], "day_offset", 1))
        day, world = await self.decide(
            action_id=action.action_id, _candidates=[action.as_dict()],
            timeline_updates=[self.edit(2, "reschedule", at=str(self.runtime.now))],
        )
        item = day.timeline[world["run"]["action"]["timeline_index"]]
        self.assertEqual((item.time, item.day_offset), ("00:30", 1))

    async def test_future_plan_beyond_one_hour_is_visible_without_extra_model_call(self):
        action = await self.evening()
        runtime = self.runtime
        runtime.composer._get_persona = AsyncMock(return_value="角色")
        runtime.composer._collect_recent_chat_context = AsyncMock(return_value="无")
        runtime.get_text_provider = AsyncMock(return_value=object())
        runtime.call_text_model = AsyncMock(return_value='{"decision":"wait","reason":"复核日程","timeline_updates":[]}')
        runtime.close_text_session = AsyncMock()
        day = await self.archive.get_day(runtime.date)
        world = await self.archive.get_continuous_life()
        result = await runtime.continuous_decision.choose(day, world, runtime.now)
        self.assertIn(action.action_id, [item["action_id"] for item in result["_candidates"]])
        inputs = json.loads(runtime.call_text_model.call_args.args[1].split("【眼前内容】\n", 1)[1])
        self.assertEqual(inputs["timeline"][-1]["at"], "2026-10-07T23:24:00")
        self.assertEqual(runtime.call_text_model.await_count, 1)
