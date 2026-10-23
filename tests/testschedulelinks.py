import datetime
import json
import tempfile
import unittest
from unittest.mock import patch

from core.config.options import LifeDomainSettings
from core.life.domain import LifeDomainService
from core.models import CommitmentRecord, DayRecord, TimelineItem
from support import LifeArchive


class ScheduledCommitmentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = f"{self.tempdir.name}/daily_life.db"
        self.archive = LifeArchive(self.path)
        self.now = datetime.datetime(2026, 10, 23, 13, 0)
        self.clock = patch("core.archive.schedulelinks.life_now", return_value=self.now)
        self.clock.start()

    async def asyncTearDown(self):
        self.clock.stop()
        await self.archive.aclose()
        self.tempdir.cleanup()

    async def schedule(self, *, date="2026-10-23", media_kind="none", states=("elapsed", "skipped"), linked=True):
        commitment = await self.archive.save_commitment(CommitmentRecord(
            content="一起去吃午饭", owner="共同", source="chat_batch",
            source_session="test:FriendMessage:1", media_kind=media_kind,
        ))
        timeline = [TimelineItem(
            time=time, activity=activity, execution_state=state, duration_minutes=duration,
        ) for time, activity, state, duration in zip(
            ("11:32", "11:35"), ("一起走到餐馆", "坐下吃午饭"), states, (3, 35),
        )]
        actions = [{
            "action_id": f"{date}:meal:{index}", "action_type": "meal",
            "timeline_index": index,
            "payload": {"schedule_commitment_id": commitment.id},
        } for index in range(len(timeline))] if linked else []
        day = DayRecord(date=date, timeline=timeline, meta={
            "planned_life_actions": json.dumps(actions, ensure_ascii=False),
        })
        await self.archive.save_day(day, replace=True)
        await self.archive.link_commitments_to_day(date, [commitment.id])
        await self.archive.save_conversation_action_item({
            "commitment_id": commitment.id, "title": commitment.content,
            "status": "pending", "owner": "共同",
        })
        return commitment, day

    async def test_ended_schedule_closes_action_in_overview_list_and_chat_context(self):
        commitment, _ = await self.schedule()
        await self.archive.save_conversation_action_item({
            "commitment_id": 900, "title": "明天还要确认的安排", "status": "open",
        })
        snapshot = await self.archive.get_domain_snapshot(limit=0)
        actions = snapshot["conversation_actions"]
        self.assertEqual([item["status"] for item in actions], ["open", "expired"])
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "expired")
        self.assertEqual(snapshot["timeline"][-1]["status"], "expired")
        domains = LifeDomainService(LifeDomainSettings(), self.archive)
        context = await domains.format_context()
        self.assertIn("明天还要确认的安排", context)
        self.assertNotIn("一起去吃午饭", context)
        self.assertEqual(await self.archive.reconcile_scheduled_commitments(now=self.now), 0)

    async def test_recorded_contact_edits_recover_missing_action_links(self):
        commitment, day = await self.schedule(linked=False)
        await self.archive.enqueue_durable_task(
            f"invite_contact:{commitment.id}", "proactive_commitment", {
                "commitment_id": commitment.id,
                "scope": commitment.source_session,
                "activity_date": day.date,
                "timeline_edits": [{"operation": "replace", "item": item.as_dict()} for item in day.timeline],
            },
        )
        actions = await self.archive.get_conversation_action_items()
        self.assertEqual(actions[0]["status"], "expired")
        self.assertNotEqual((await self.archive.get_commitment(commitment.id)).status, "done")
        await self.archive.aclose()
        self.archive = LifeArchive(self.path)
        self.assertEqual((await self.archive.get_conversation_action_items())[0]["status"], "expired")

    async def test_active_or_future_steps_keep_whole_arrangement_open(self):
        for state in ("active", "planned"):
            with self.subTest(state=state):
                commitment, _ = await self.schedule(states=("elapsed", state))
                await self.archive.reconcile_scheduled_commitments(now=self.now)
                self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_next_day_and_undated_unlinked_promises_are_not_aged_out(self):
        for date in ("2026-10-22", "2026-10-24"):
            with self.subTest(date=date):
                commitment, _ = await self.schedule(date=date, linked=False)
                await self.archive.reconcile_scheduled_commitments(now=self.now)
                self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_media_delivery_does_not_expire_with_simulated_life_activity(self):
        for kind in ("photo", "video"):
            with self.subTest(kind=kind):
                commitment, _ = await self.schedule(media_kind=kind)
                await self.archive.reconcile_scheduled_commitments(now=self.now)
                self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_rescheduled_promise_ignores_old_day_links(self):
        commitment, day = await self.schedule()
        await self.archive.reschedule_commitment(commitment.id, "2026-10-24", trigger_time="12:00")
        await self.archive.link_commitments_to_day("2026-10-24", [commitment.id])
        future = DayRecord(date="2026-10-24", timeline=day.timeline, meta=day.meta)
        await self.archive.save_day(future, replace=True)
        await self.archive.reconcile_scheduled_commitments(now=self.now)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")
        self.assertEqual((await self.archive.get_conversation_action_items())[0]["status"], "pending")

    async def test_same_day_new_time_is_not_closed_by_old_plan(self):
        commitment, _ = await self.schedule()
        await self.archive.reschedule_commitment(commitment.id, "2026-10-23", trigger_time="18:00")
        await self.archive.link_commitments_to_day("2026-10-23", [commitment.id])
        await self.archive.reconcile_scheduled_commitments(now=self.now)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_replaced_contact_edit_cannot_close_different_activity(self):
        commitment, day = await self.schedule(linked=False)
        edits = [{"operation": "replace", "item": item.as_dict()} for item in day.timeline]
        edits[0]["item"]["activity"] = "已被替换的旧安排"
        await self.archive.enqueue_durable_task(
            f"invite_contact:{commitment.id}", "proactive_commitment", {
                "commitment_id": commitment.id, "scope": commitment.source_session,
                "activity_date": day.date, "timeline_edits": edits,
            },
        )
        await self.archive.reconcile_scheduled_commitments(now=self.now)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_cancelled_schedule_stays_cancelled_and_done_is_not_overwritten(self):
        commitment, _ = await self.schedule(states=("cancelled", "cancelled"))
        await self.archive.reconcile_scheduled_commitments(now=self.now)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "cancelled")
        done, _ = await self.schedule()
        await self.archive.set_commitment_status(done.id, "done", "2026-10-23 12:10:00")
        await self.archive.save_conversation_action_item({
            "commitment_id": done.id, "title": done.content, "status": "pending",
        })
        items = await self.archive.get_conversation_action_items()
        item = next(item for item in items if item["commitment_id"] == done.id)
        self.assertEqual(item["status"], "done")
        self.assertEqual((await self.archive.get_commitment(done.id)).completed_at, "2026-10-23 12:10:00")

    async def test_background_reconciliation_works_without_activity_tracking(self):
        commitment, day = await self.schedule()
        domains = LifeDomainService(LifeDomainSettings(activity_tracking_enabled=False), self.archive)
        await domains.sync_activity_sessions(day, now=self.now)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "expired")

    async def test_daily_action_commitment_ids_also_close_the_pending_item(self):
        commitment, day = await self.schedule()
        actions = json.loads(day.meta["planned_life_actions"])
        for action in actions:
            action["payload"] = {"commitment_ids": [commitment.id]}
        day.meta["planned_life_actions"] = json.dumps(actions)
        await self.archive.save_day(day)
        await self.archive.reconcile_scheduled_commitments(now=self.now)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "expired")
