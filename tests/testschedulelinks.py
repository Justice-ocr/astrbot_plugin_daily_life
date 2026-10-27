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

    async def test_elapsed_planned_knock_does_not_settle_unsent_contact(self):
        commitment, _ = await self.schedule()
        await self.archive.save_commitment_follow_up(commitment.id, {
            "action": "contact_person", "execute_at": "2026-10-23T11:32:00",
        })
        self.assertEqual(await self.archive.reconcile_scheduled_commitments(now=self.now), 0)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_contact_under_review_is_not_expired_with_the_day_plan(self):
        commitment, _ = await self.schedule()
        await self.archive.enqueue_durable_task(
            f"commitment_contact_review:{commitment.id}", "commitment_contact_review",
            {"commitment_id": commitment.id},
        )
        self.assertEqual(await self.archive.reconcile_scheduled_commitments(now=self.now), 0)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_active_or_future_steps_keep_whole_arrangement_open(self):
        for state in ("active", "planned"):
            with self.subTest(state=state):
                commitment, _ = await self.schedule(states=("elapsed", state))
                await self.archive.reconcile_scheduled_commitments(now=self.now)
                self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "scheduled")

    async def test_past_declared_window_closes_stale_planned_step_but_not_active_step(self):
        planned, _ = await self.schedule(states=("elapsed", "planned"))
        planned.trigger_date = "2026-10-23"
        planned.trigger_time = ""
        planned.time_window = "上午"
        planned.status = "active"
        await self.archive.save_commitment(planned)
        await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=13))
        self.assertEqual((await self.archive.get_commitment(planned.id)).status, "expired")

        active, _ = await self.schedule(states=("elapsed", "active"))
        active.trigger_date = "2026-10-23"
        active.trigger_time = ""
        active.time_window = "上午"
        active.status = "active"
        await self.archive.save_commitment(active)
        await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=13))
        self.assertEqual((await self.archive.get_commitment(active.id)).status, "active")

    async def test_started_conversation_keeps_receiving_schedule_close_updates(self):
        for state, final in (("elapsed", "expired"), ("cancelled", "cancelled")):
            with self.subTest(state=state):
                commitment, _ = await self.schedule(states=(state, state))
                await self.archive.set_commitment_status(commitment.id, "active")
                await self.archive.save_conversation_action_item({
                    "commitment_id": commitment.id, "title": commitment.content,
                    "status": "active", "evidence": ["已经出发"],
                })
                snapshot = await self.archive.get_domain_snapshot(limit=0)
                item = next(x for x in snapshot["conversation_actions"] if x["commitment_id"] == commitment.id)
                self.assertEqual(item["status"], final)
                self.assertIn("已经出发", item["evidence"])
                self.assertEqual((await self.archive.get_commitment(commitment.id)).status, final)

    async def test_day_link_alone_cannot_keep_a_started_arrangement_open_forever(self):
        for window, boundary in (("明天", "2026-10-24 00:00:00"), ("明早", "2026-10-23 12:00:00")):
            with self.subTest(window=window):
                commitment = await self.legacy_action(trigger_time="", time_window=window)
                await self.archive.save_day(DayRecord(date=commitment.trigger_date))
                await self.archive.link_commitments_to_day(commitment.trigger_date, [commitment.id])
                await self.archive.set_commitment_status(commitment.id, "active")
                await self.archive.save_conversation_action_item({
                    "commitment_id": commitment.id, "title": commitment.content,
                    "status": "active", "evidence": ["在园子里歇口气，等会儿再走走"],
                })
                later = datetime.datetime(2026, 10, 24, 10)
                with patch("core.archive.schedulelinks.life_now", return_value=later):
                    snapshot = await self.archive.get_domain_snapshot(limit=0)
                    item = next(x for x in snapshot["conversation_actions"] if x["commitment_id"] == commitment.id)
                    self.assertEqual(item["status"], "expired")
                    self.assertIn("在园子里歇口气，等会儿再走走", item["evidence"])
                    self.assertEqual((await self.archive.get_commitment(commitment.id)).completed_at, boundary)
                    self.assertFalse(any(x.id == commitment.id for x in await self.archive.get_commitments()))
                    self.assertEqual(await self.archive.reconcile_scheduled_commitments(now=later), 0)

    async def test_chat_and_due_readers_reconcile_without_opening_dashboard(self):
        for read in ("chat", "due"):
            with self.subTest(read=read):
                commitment = await self.legacy_action()
                await self.archive.save_day(DayRecord(date=commitment.trigger_date))
                await self.archive.link_commitments_to_day(commitment.trigger_date, [commitment.id])
                await self.archive.set_commitment_status(commitment.id, "active")
                with patch("core.archive.schedulelinks.life_now", return_value=self.now.replace(hour=19)):
                    if read == "chat":
                        snapshot = await self.archive.get_context_snapshot(max_summaries=2, session_id=commitment.source_session)
                        records = snapshot["commitments"]
                    else:
                        records = await self.archive.get_due_commitments(commitment.trigger_date)
                    self.assertNotIn(commitment.id, [item.id for item in records])
                    self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "expired")

    async def test_date_only_link_expiry_is_persisted_across_restart(self):
        commitment = await self.legacy_action(trigger_time="", time_window="")
        await self.archive.save_day(DayRecord(date=commitment.trigger_date))
        await self.archive.link_commitments_to_day(commitment.trigger_date, [commitment.id])
        await self.archive.reconcile_scheduled_commitments(now=datetime.datetime(2026, 10, 24))
        await self.archive.aclose()
        self.archive = LifeArchive(self.path)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "expired")
        self.assertEqual((await self.archive.get_conversation_action_items())[0]["status"], "expired")

    async def test_started_real_schedule_and_future_reschedule_are_not_aged_out(self):
        commitment, day = await self.schedule(states=("completed", "active"))
        commitment = await self.archive.get_commitment(commitment.id)
        commitment.trigger_date = day.date
        commitment.time_window = "上午"
        commitment.status = "active"
        await self.archive.save_commitment(commitment)
        await self.archive.reconcile_scheduled_commitments(now=self.now)
        self.assertEqual((await self.archive.get_commitment(commitment.id)).status, "active")
        future = await self.legacy_action()
        await self.archive.save_day(DayRecord(date="2026-10-24"))
        await self.archive.link_commitments_to_day("2026-10-24", [future.id])
        await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=19))
        self.assertEqual((await self.archive.get_commitment(future.id)).status, "scheduled")

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

    async def legacy_action(self, **overrides):
        values = {
            "content": "一点五十敲你门，一起出门散步",
            "owner": "当前角色", "source": "chat_batch",
            "source_session": "test:FriendMessage:1",
            "trigger_date": "2026-10-23", "trigger_time": "13:50",
            "time_window": "下午", "source_message": "一点五十敲你门",
        }
        commitment = await self.archive.save_commitment(CommitmentRecord(**{
            **values, **overrides,
        }))
        await self.archive.save_conversation_action_item({
            "commitment_id": commitment.id, "title": commitment.content,
            "owner": commitment.owner, "status": "open",
            "due_at": f"{commitment.trigger_date} {commitment.trigger_time}".strip(),
        })
        return commitment

    async def test_unqueued_legacy_actions_close_in_dashboard_context_and_after_restart(self):
        knock = await self.legacy_action()
        afternoon = await self.legacy_action(content="下午带对方出门散心", trigger_time="")
        future = await self.legacy_action(content="明天去看展", trigger_date="2026-10-24")
        later = self.now.replace(hour=19)
        with patch("core.archive.schedulelinks.life_now", return_value=later):
            snapshot = await self.archive.get_domain_snapshot(limit=0)
            states = {item["commitment_id"]: item["status"] for item in snapshot["conversation_actions"]}
            self.assertEqual(states, {knock.id: "expired", afternoon.id: "expired", future.id: "open"})
            domains = LifeDomainService(LifeDomainSettings(), self.archive)
            context = await domains.format_context()
            self.assertNotIn(knock.content, context)
            self.assertNotIn(afternoon.content, context)
            self.assertIn(future.content, context)
            self.assertEqual(await self.archive.reconcile_scheduled_commitments(now=later), 0)
            self.assertEqual(await self.archive.get_durable_tasks(limit=0), [])
            for item in (knock, afternoon):
                record = await self.archive.get_commitment(item.id)
                self.assertEqual(record.status, "expired")
                self.assertEqual(record.completed_at, "2026-10-23 18:00:00")
            traces = await self.archive.get_decision_traces(limit=0)
            self.assertEqual(len(traces), 2)
            self.assertTrue(all(trace.reason_code == "legacy_action_window_elapsed" for trace in traces))
            await self.archive.aclose()
            self.archive = LifeArchive(self.path)
            self.assertEqual((await self.archive.get_commitment(knock.id)).status, "expired")
            self.assertEqual((await self.archive.get_commitment(future.id)).status, "active")

    async def test_legacy_precise_time_does_not_expire_the_whole_outing_at_its_start(self):
        record = await self.legacy_action()
        for at in (self.now.replace(hour=13, minute=50), self.now.replace(hour=17, minute=59)):
            self.assertEqual(await self.archive.reconcile_scheduled_commitments(now=at), 0)
            self.assertEqual((await self.archive.get_commitment(record.id)).status, "active")
        self.assertEqual(await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=18)), 2)

    async def test_date_only_legacy_action_waits_until_the_date_has_ended(self):
        record = await self.legacy_action(trigger_time="", time_window="")
        await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=23, minute=59))
        self.assertEqual((await self.archive.get_commitment(record.id)).status, "active")
        await self.archive.reconcile_scheduled_commitments(now=datetime.datetime(2026, 10, 24))
        self.assertEqual((await self.archive.get_commitment(record.id)).status, "expired")

    async def test_window_expiry_keeps_uncertain_future_conditional_and_external_obligations(self):
        cases = (
            {"trigger_date": "2026-10-24"},
            {"trigger_date": ""}, {"trigger_date": "2026-99-99"},
            {"time_window": "等你下班"}, {"time_window": "weekend"},
            {"time_window": "以后"}, {"trigger_time": "25:70"},
            {"trigger_time": "20:00"},
            {"source": "manual"}, {"kind": "reminder"},
            {"media_kind": "photo"}, {"media_kind": "video"},
            {"follow_up": {"action": "contact_person", "execute_at": "2026-10-23T13:50:00"}},
            {"follow_up": {"action": "remind_person"}},
            {"trigger_date": "", "follow_up": {"action": "none", "condition": "等你下班"}},
            {"time_window": "next_time", "follow_up": {"action": "none", "condition": "等你下班"}},
            {"follow_up": {"action": "none", "execute_at": "2026-10-24T10:00:00"}},
            {"follow_up": {"action": "none", "expires_at": "2026-10-24T10:00:00"}},
            {"follow_up": {"reviewed": True}},
        )
        for case in cases:
            with self.subTest(case=case):
                record = await self.legacy_action(**case)
                await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=19))
                self.assertEqual((await self.archive.get_commitment(record.id)).status, case.get("status", "active"))

    async def test_structured_actions_expire_across_owners_and_scopes_at_the_declared_boundary(self):
        cases = (
            {"owner": "当前角色"}, {"owner": "共同"}, {"owner": "说话人"},
            {"status": "pending"}, {"status": "scheduled"},
            {"source_session": "test:GroupMessage:1"},
        )
        for case in cases:
            with self.subTest(case=case):
                record = await self.legacy_action(**case, follow_up={
                    "action": "none", "condition": "", "execute_at": "",
                    "message_goal": "", "check_after_minutes": 0,
                })
                await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=17, minute=59))
                self.assertEqual((await self.archive.get_commitment(record.id)).status, case.get("status", "active"))
                await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=19))
                saved = await self.archive.get_commitment(record.id)
                self.assertEqual(saved.status, "expired")
                self.assertEqual(saved.completed_at, "2026-10-23 18:00:00")
        traces = await self.archive.get_decision_traces(limit=0)
        self.assertEqual(len(traces), len(cases))
        self.assertTrue(all(trace.reason_code == "conversation_action_window_elapsed" for trace in traces))

    async def test_structured_date_only_and_local_window_labels_use_their_actual_life_date(self):
        for window, end_hour in (
            ("", 24), ("明天", 24), ("今晚", 24), ("明早", 12),
            ("清早", 9), ("清晨", 9),
        ):
            with self.subTest(window=window):
                record = await self.legacy_action(
                    trigger_time="", time_window=window, follow_up={"action": "none"},
                )
                boundary = self.now.replace(hour=0) + datetime.timedelta(hours=end_hour)
                await self.archive.reconcile_scheduled_commitments(now=boundary - datetime.timedelta(seconds=1))
                self.assertEqual((await self.archive.get_commitment(record.id)).status, "active")
                await self.archive.reconcile_scheduled_commitments(now=boundary + datetime.timedelta(days=2))
                saved = await self.archive.get_commitment(record.id)
                self.assertEqual(saved.status, "expired")
                self.assertEqual(saved.completed_at, boundary.strftime("%Y-%m-%d %H:%M:%S"))

    async def test_completed_invite_task_does_not_keep_a_past_day_action_open(self):
        completed = await self.legacy_action(
            source="invite", trigger_time="", time_window="", follow_up={"action": "none"},
        )
        completed_task = f"invite_contact:{completed.id}"
        await self.archive.enqueue_durable_task(completed_task, "proactive_commitment", {
            "commitment_id": completed.id,
        })
        self.archive._conn.execute(
            "UPDATE durable_tasks SET status = 'completed' WHERE task_key = ?", (completed_task,),
        )

        pending = await self.legacy_action(
            source="invite", trigger_time="", time_window="", follow_up={"action": "none"},
        )
        await self.archive.enqueue_durable_task(
            f"invite_contact:{pending.id}", "proactive_commitment", {"commitment_id": pending.id},
        )

        next_day = datetime.datetime(2026, 10, 24)
        await self.archive.reconcile_scheduled_commitments(now=next_day)
        self.assertEqual((await self.archive.get_commitment(completed.id)).status, "expired")
        self.assertEqual((await self.archive.get_commitment(pending.id)).status, "active")
        traces = await self.archive.get_decision_traces(limit=0)
        self.assertIn("invite_action_window_elapsed", [trace.reason_code for trace in traces])

    async def test_condition_does_not_extend_an_explicitly_bounded_arrangement_forever(self):
        record = await self.legacy_action(follow_up={"action": "none", "condition": "等你忙完"})
        await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=19))
        self.assertEqual((await self.archive.get_commitment(record.id)).status, "expired")

    async def test_window_expiry_respects_all_existing_task_states_and_payload_links(self):
        for status, follow_up in (
            (state, follow_up) for state in ("pending", "leased", "completed", "failed")
            for follow_up in ({}, {"action": "none"})
        ):
            with self.subTest(status=status, follow_up=follow_up):
                record = await self.legacy_action(follow_up=follow_up)
                key = f"proactive_commitment:{record.id}"
                await self.archive.enqueue_durable_task(key, "proactive_commitment", {"commitment_id": record.id})
                self.archive._conn.execute("UPDATE durable_tasks SET status = ? WHERE task_key = ?", (status, key))
                await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=19))
                self.assertEqual((await self.archive.get_commitment(record.id)).status, "active")
        record = await self.legacy_action()
        await self.archive.enqueue_durable_task("custom_contact", "proactive_commitment", {"commitment_id": str(record.id)})
        await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=19))
        self.assertEqual((await self.archive.get_commitment(record.id)).status, "active")

    async def test_legacy_repair_uses_declared_windows_and_local_timezone(self):
        for window in ("afternoon", "下午"):
            with self.subTest(window=window):
                record = await self.legacy_action(time_window=window)
                at = datetime.datetime(2026, 10, 23, 10, tzinfo=datetime.timezone.utc)
                await self.archive.reconcile_scheduled_commitments(now=at)
                self.assertEqual((await self.archive.get_commitment(record.id)).status, "expired")

    async def test_legacy_expiry_rolls_back_if_audit_cannot_be_written(self):
        record = await self.legacy_action()
        self.archive._conn.execute("""CREATE TRIGGER reject_legacy_audit
            BEFORE INSERT ON decision_traces BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END""")
        with self.assertRaisesRegex(Exception, "audit unavailable"):
            await self.archive.reconcile_scheduled_commitments(now=self.now.replace(hour=19))
        self.assertEqual((await self.archive.get_commitment(record.id)).status, "active")
        item = self.archive._conn.execute(
            "SELECT status FROM conversation_action_items WHERE commitment_id = ?", (record.id,)
        ).fetchone()
        self.assertEqual(item["status"], "open")
