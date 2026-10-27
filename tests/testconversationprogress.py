"""会话行动项跟随实际生活进展，并保留对外交付与说话人边界。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from componentfixtures import ChatExecutionComponent
from core.models import CommitmentRecord
from support import LifeArchive


class ConversationProgressTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.archive = LifeArchive(Path(self.temp.name) / "life.db")
        self.runtime = ChatExecutionComponent()
        self.runtime.archive = self.archive

    async def asyncTearDown(self):
        self.archive.close()
        self.temp.cleanup()

    async def prepare(self, *, owner="共同", role="user", status="done",
                      media_kind="none", date="", follow_up=None, group=False,
                      profile="friend-1", evidence="做好了，快来吃吧",
                      content="一起做面吃", original_text="我来做面，你帮我打下手"):
        scope = "group:1" if group else "private:1"
        original = {
            "event_key": "original", "session_id": scope, "message_id": "origin",
            "role": "user", "sender_profile_id": "friend-1", "is_group": group,
            "message_text": original_text, "occurred_at": "2026-10-24T22:36:00",
        }
        await self.archive.enqueue_chat_memory_message(original)
        row = {
            **original, "event_key": "progress", "message_id": "progress", "role": role,
            "sender_profile_id": profile, "message_text": evidence,
            "occurred_at": "2026-10-24T22:47:00",
        }
        row["id"], _ = await self.archive.enqueue_chat_memory_message(row)
        item = await self.archive.save_commitment(CommitmentRecord(
            content=content, kind="plan", owner=owner, media_kind=media_kind,
            trigger_date=date, source="chat_batch", source_session=scope,
            source_message_id="origin", source_message=original["message_text"],
            follow_up=follow_up or {},
        ))
        await self.archive.save_conversation_action_item({
            "commitment_id": item.id, "title": item.content, "owner": owner,
            "source_session": scope, "evidence": [original["message_text"]],
        })
        batch = {"session_id": scope, "messages": [row],
                 "open_commitments": [item.as_dict()]}
        payload = {"execution_updates": [{
            "commitment_ids": [item.id], "status": status,
            "source_message_id": "progress", "evidence": evidence,
        }]}
        return item, batch, payload

    async def test_shared_meal_and_users_own_action_accept_the_actual_speaker(self):
        for owner, role in (("共同", "user"), ("共同", "assistant"), ("说话人", "user")):
            with self.subTest(owner=owner, role=role):
                # 每个案例使用独立数据库，防止重复消息去重影响验证。
                await self.asyncTearDown()
                await self.asyncSetUp()
                item, batch, payload = await self.prepare(owner=owner, role=role)
                self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
                self.assertEqual((await self.archive.get_commitment(item.id)).status, "done")
                actions = await self.archive.get_conversation_action_items()
                self.assertEqual(actions[0]["status"], "done")
                self.assertIn("做好了，快来吃吧", actions[0]["evidence"])
                self.assertEqual(actions[0]["completed_at"], "2026-10-24 22:47:00")
                snapshot = await self.archive.get_domain_snapshot()
                self.assertEqual(snapshot["conversation_actions"][0]["completed_at"],
                                 actions[0]["completed_at"])
                self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 0)

    async def test_progress_is_visible_and_repeated_extraction_does_not_reset_it(self):
        item, batch, payload = await self.prepare(status="in_progress", evidence="我已经在煮面了")
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
        await self.archive.save_conversation_action_item({
            "commitment_id": item.id, "title": item.content, "status": "open",
        })
        action = (await self.archive.get_conversation_action_items())[0]
        self.assertEqual(action["status"], "active")
        self.assertIn("我已经在煮面了", action["evidence"])
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 0)

    async def test_later_extraction_does_not_reopen_terminal_commitment(self):
        for status in ("done", "cancelled", "expired"):
            with self.subTest(status=status):
                await self.asyncTearDown()
                await self.asyncSetUp()
                item, batch, payload = await self.prepare(status=status)
                self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
                replacement = CommitmentRecord.from_value(item.as_dict())
                replacement.id = 0
                replacement.source = "chat"
                saved = await self.archive.save_commitment(replacement)
                self.assertEqual(saved.id, item.id)
                self.assertEqual(saved.status, status)
                self.assertTrue(saved.completed_at)

    async def test_origin_remains_verifiable_after_many_intervening_messages(self):
        item, batch, payload = await self.prepare()
        for index in range(210):
            await self.archive.enqueue_chat_memory_message({
                "event_key": f"filler-{index}", "session_id": "private:1",
                "message_id": f"filler-{index}", "role": "user",
                "message_text": f"其他话题 {index}", "occurred_at": "2026-10-24T22:40:00",
            })
        row = dict(batch["messages"][0], message_id="later", event_key="later")
        row["id"], _ = await self.archive.enqueue_chat_memory_message(row)
        batch["messages"] = [row]
        payload["execution_updates"][0]["source_message_id"] = "later"
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)

    async def test_cancel_future_arrangement_is_reviewable_without_waiting_until_due(self):
        item, batch, payload = await self.prepare(
            date="2026-10-25", status="cancelled", evidence="明天不做面了，换成出去吃",
        )
        context = await self.runtime._chat_execution_context(batch)
        self.assertIn(item.id, [row["id"] for row in context["open_commitments"]])
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
        await self.archive.save_conversation_action_item({
            "commitment_id": item.id, "title": item.content, "status": "open",
        })
        self.assertEqual((await self.archive.get_conversation_action_items())[0]["status"], "cancelled")

    async def test_same_batch_new_arrangement_can_be_settled_by_later_evidence(self):
        item, batch, payload = await self.prepare()
        batch["open_commitments"] = []
        update = payload["execution_updates"][0]
        update["commitment_ids"] = []
        update["new_commitment_indexes"] = [1]
        payload["_saved_commitments"] = {"1": item.as_dict()}
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
        self.assertEqual((await self.archive.get_commitment(item.id)).status, "done")

    async def test_expired_arrangement_can_be_corrected_with_retained_completion_evidence(self):
        item, batch, payload = await self.prepare(date="2026-10-24")
        await self.archive.set_commitment_status(item.id, "expired", "2026-10-24 23:00:00")
        context = await self.runtime._chat_execution_context(batch)
        self.assertIn(item.id, [row["id"] for row in context["open_commitments"]])
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
        self.assertEqual((await self.archive.get_commitment(item.id)).status, "done")

    async def test_undated_action_expires_with_evidence_that_its_life_context_has_ended(self):
        item, batch, payload = await self.prepare(
            owner="当前角色", role="assistant", status="expired",
            content="把最后几张照片收尾后去洗漱",
            original_text="把最后几张照片收个尾就去洗漱",
            follow_up={"action": "none", "condition": "最后几张照片收尾后"},
            evidence="我已经躺下睡了，今天就到这里吧",
        )
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
        saved = await self.archive.get_commitment(item.id)
        self.assertEqual(saved.status, "expired")
        self.assertEqual(saved.completed_at, "2026-10-24 22:47:00")
        action = (await self.archive.get_domain_snapshot())["conversation_actions"][0]
        self.assertEqual(action["status"], "expired")
        self.assertIn(payload["execution_updates"][0]["evidence"], action["evidence"])
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 0)
        self.assertEqual((await self.archive.get_decision_traces())[0].stage, "conversation_expired")
        context = await self.runtime._chat_execution_context(batch)
        self.assertIn(item.id, [row["id"] for row in context["open_commitments"]])
        batch["messages"][0]["occurred_at"] = "2026-11-02T10:00:00"
        context = await self.runtime._chat_execution_context(batch)
        self.assertNotIn(item.id, [row["id"] for row in context["open_commitments"]])

    async def test_undated_conditional_action_does_not_expire_just_because_a_day_passed(self):
        item, _, _ = await self.prepare(
            follow_up={"action": "none", "condition": "等你有空再一起做"},
        )
        await self.archive.reconcile_scheduled_commitments(now="2026-10-26T10:00:00")
        self.assertEqual((await self.archive.get_commitment(item.id)).status, "active")

    async def test_undated_expired_action_cannot_restart_but_can_accept_retained_completion_proof(self):
        item, batch, payload = await self.prepare()
        await self.archive.set_commitment_status(item.id, "expired", "2026-10-24 23:00:00")
        context = await self.runtime._chat_execution_context(batch)
        self.assertIn(item.id, [row["id"] for row in context["open_commitments"]])
        payload["execution_updates"][0]["status"] = "in_progress"
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 0)
        self.assertEqual((await self.archive.get_commitment(item.id)).status, "expired")
        payload["execution_updates"][0]["status"] = "done"
        self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 1)
        self.assertEqual((await self.archive.get_commitment(item.id)).status, "done")

    async def test_media_and_outbound_contact_require_delivery_receipts(self):
        for options in ({"media_kind": "photo"}, {"media_kind": "video"},
                        {"follow_up": {"action": "contact_person"}},
                        {"follow_up": {"action": "remind_person"}}):
            with self.subTest(options=options):
                await self.asyncTearDown()
                await self.asyncSetUp()
                item, batch, payload = await self.prepare(**options)
                for status in ("done", "expired"):
                    payload["execution_updates"][0]["status"] = status
                    self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 0)
                self.assertEqual((await self.archive.get_commitment(item.id)).status, "active")

    async def test_wrong_speaker_other_group_member_and_future_completion_are_rejected(self):
        for options in ({"owner": "说话人", "role": "assistant"},
                        {"owner": "当前角色", "role": "user"},
                        {"group": True, "profile": "other-member"},
                        {"date": "2026-10-25"}):
            with self.subTest(options=options):
                await self.asyncTearDown()
                await self.asyncSetUp()
                item, batch, payload = await self.prepare(**options)
                for status in ("done", "expired"):
                    payload["execution_updates"][0]["status"] = status
                    self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 0)
                self.assertEqual((await self.archive.get_commitment(item.id)).status, "active")

    async def test_fabricated_or_quoted_database_evidence_cannot_settle_action(self):
        item, batch, payload = await self.prepare()
        conn = self.archive._conn
        for mutation in ("quote", "text", "origin"):
            with self.subTest(mutation=mutation):
                conn.execute("BEGIN")
                if mutation == "quote":
                    conn.execute("UPDATE chat_memory_messages SET is_quoted = 1 WHERE message_id = 'progress'")
                elif mutation == "text":
                    conn.execute("UPDATE chat_memory_messages SET message_text = '还没做好' WHERE message_id = 'progress'")
                else:
                    conn.execute("DELETE FROM chat_memory_messages WHERE message_id = 'origin'")
                conn.commit()
                self.assertEqual(await self.runtime._save_batch_execution_updates(payload, batch), 0)
                if mutation == "quote":
                    conn.execute("UPDATE chat_memory_messages SET is_quoted = 0")
                elif mutation == "text":
                    conn.execute("UPDATE chat_memory_messages SET message_text = '做好了，快来吃吧' WHERE message_id = 'progress'")
                conn.commit()
        self.assertEqual((await self.archive.get_commitment(item.id)).status, "active")

    async def test_trace_failure_rolls_back_both_commitment_and_action(self):
        item, batch, payload = await self.prepare()
        self.archive._conn.execute("""CREATE TRIGGER fail_progress_trace BEFORE INSERT ON decision_traces
            BEGIN SELECT RAISE(ABORT, 'trace unavailable'); END""")
        self.archive._conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            await self.runtime._save_batch_execution_updates(payload, batch)
        self.assertEqual((await self.archive.get_commitment(item.id)).status, "active")
        self.assertEqual((await self.archive.get_conversation_action_items())[0]["status"], "open")
