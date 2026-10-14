import asyncio
import copy
import datetime
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import LifeArchive  # isort: skip

from core.life.achievement import apply_reviews, review_candidates
from core.life.goals import apply_goal_decisions, credit_goal, ready_steps
from core.life.interests import apply_interest_updates, bind_interest
from core.life.presence import (
    apply_action_reflections,
    apply_self_model_updates,
    record_action_outcome,
)
from core.models import CommitmentRecord, DayRecord, LifeState, LongTermMemoryRecord
from core.runtime.participation import LivingStateService
from componentfixtures import LayerChainComponent


class AchievementTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 10, 11, 18)
        self.world = {}
        apply_goal_decisions(
            self.world,
            {
                "new_goals": [
                    {
                        "owner": "self",
                        "source_id": "persona",
                        "title": "构图练习",
                        "reason": "喜欢摄影",
                        "steps": [
                            {
                                "id": "one",
                                "title": "比较构图",
                                "action_type": "study",
                                "required_minutes": 10,
                                "acceptance": "草稿中比较两种构图",
                                "evidence_kind": "artifact",
                            },
                            {
                                "id": "two",
                                "title": "继续练习",
                                "action_type": "study",
                                "required_minutes": 10,
                                "depends_on": ["one"],
                                "evidence_kind": "artifact",
                            },
                        ],
                    }
                ]
            },
            sources={"persona"},
            now=self.now,
        )
        self.goal = self.world["goals"][0]
        self.run = {
            "goal_id": self.goal["id"],
            "step_id": "one",
            "active_seconds": 600,
            "action": {
                "action_id": "exercise-one",
                "action_type": "study",
                "target": "比较构图",
            },
        }

    def outcome(self):
        credit_goal(self.world, self.run, self.now)
        return record_action_outcome(
            self.world,
            self.run["action"],
            status="committed",
            run=self.run,
            completed_at=self.now,
        )

    def artifact(self):
        self.outcome()
        apply_action_reflections(
            self.world,
            [
                {
                    "action_id": "exercise-one",
                    "summary": "整理比较",
                    "artifact": {
                        "kind": "practice_note",
                        "content": "比较居中构图与三分构图：前者稳定，后者留出视线空间。",
                    },
                }
            ],
            allowed_action_ids={"exercise-one"},
            now=self.now,
        )
        return review_candidates(self.world)[0]

    def review(self, candidate, **extra):
        return {
            "goal_id": self.goal["id"],
            "step_id": "one",
            "revision": candidate["revision"],
            "verdict": "passed",
            "reason": "草稿包含两种构图比较",
            "evidence": [
                {"action_id": "exercise-one", "quote": "前者稳定，后者留出视线空间"}
            ],
            **extra,
        }

    def test_elapsed_time_cannot_complete_goal_and_dependencies_stay_closed(self):
        self.outcome()
        self.assertEqual(self.goal["steps"][0]["status"], "awaiting_review")
        self.assertEqual([step["id"] for step in ready_steps(self.goal)], ["one"])
        self.assertEqual(review_candidates(self.world), [])

    def test_saved_artifact_must_be_reviewed_from_offered_snapshot(self):
        candidate = self.artifact()
        review = self.review(candidate)
        apply_reviews(self.world, [review], offered=[], now=self.now)
        self.assertEqual(self.goal["steps"][0]["status"], "awaiting_review")
        apply_reviews(self.world, [review], offered=[candidate], now=self.now)
        self.assertEqual(self.goal["steps"][0]["status"], "completed")
        self.assertEqual([step["id"] for step in ready_steps(self.goal)], ["two"])
        apply_reviews(self.world, [review], offered=[candidate], now=self.now)
        self.assertEqual(len(self.goal["steps"][0]["reviews"]), 1)

    def test_saved_artifact_remains_reviewable_after_recent_outcomes_are_trimmed(self):
        candidate = self.artifact()
        for index in range(81):
            record_action_outcome(
                self.world,
                {
                    "action_id": f"other-{index}",
                    "action_type": "study",
                    "target": "另一项练习",
                },
                status="committed",
                completed_at=self.now,
            )
        self.assertEqual(len(self.world["kernel"]["action_outcomes"]), 80)
        self.assertEqual(review_candidates(self.world)[0]["revision"], candidate["revision"])
        apply_reviews(self.world, [self.review(candidate)], offered=[candidate], now=self.now)
        self.assertEqual(self.goal["steps"][0]["status"], "completed")

    def test_forged_and_stale_evidence_cannot_pass(self):
        candidate = self.artifact()
        fake = self.review(
            candidate, evidence=[{"action_id": "exercise-one", "quote": "不存在的作品"}]
        )
        apply_reviews(self.world, [fake], offered=[candidate], now=self.now)
        self.assertEqual(self.goal["steps"][0]["status"], "awaiting_review")
        self.world["kernel"]["action_outcomes"][0]["artifact"]["content"] = (
            "已修改的草稿"
        )
        apply_reviews(
            self.world, [self.review(candidate)], offered=[candidate], now=self.now
        )
        self.assertEqual(self.goal["steps"][0]["status"], "awaiting_review")

    def test_failed_review_retains_practice_and_next_step(self):
        candidate = self.artifact()
        review = self.review(
            candidate, verdict="needs_practice", next_step="再比较留白"
        )
        apply_reviews(self.world, [review], offered=[candidate], now=self.now)
        self.assertEqual(self.goal["steps"][0]["practice_minutes"], 10)
        self.assertEqual(self.goal["steps"][0]["next_step"], "再比较留白")
        self.assertEqual(review_candidates(self.world), [])

    def test_replayed_action_cannot_credit_twice_after_run_flag_is_lost(self):
        self.outcome()
        self.run.pop("goal_credited")
        credit_goal(self.world, self.run, self.now)
        self.assertEqual(self.goal["steps"][0]["practice_minutes"], 10)

    def test_interest_links_actual_practice_and_survives_pause(self):
        apply_self_model_updates(
            self.world,
            [
                {
                    "owner": "self",
                    "field": "interest",
                    "id": "photo",
                    "text": "摄影",
                    "source_ids": ["persona"],
                    "reason": "角色本身喜欢",
                }
            ],
            sources={"persona"},
            now=self.now,
        )
        bind_interest(
            self.world, self.run, {"interest_id": "photo", "motive": "想试试留白"}
        )
        self.outcome()
        item = self.world["kernel"]["self_model"]["interests"][0]
        self.assertEqual(item["sessions"], 1)
        self.assertEqual(item["last_motive"], "想试试留白")
        apply_interest_updates(
            self.world,
            [
                {
                    "id": "photo",
                    "disposition": "paused",
                    "source_ids": ["state:one"],
                    "reason": "今天疲倦",
                    "next_step": "明天继续留白",
                }
            ],
            sources={"state:one"},
            now=self.now,
        )
        self.assertEqual(item["disposition"], "paused")
        self.assertEqual(item["sessions"], 1)
        unbound = {}
        bind_interest(self.world, unbound, {"interest_id": "photo", "motive": "再拍"})
        self.assertNotIn("interest_id", unbound)


class LivingIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "life.db"
        self.archive = LifeArchive(self.path)
        self.scope = "test:FriendMessage:one"
        self.other = "test:FriendMessage:two"
        self.commitment = await self.archive.save_commitment(
            CommitmentRecord(
                content="周末一起看电影",
                owner="共同",
                source="chat",
                trigger_date="2026-10-11",
                source_session=self.scope,
                source_message="周末一起看电影吧",
                status="scheduled",
            )
        )
        self.activity = await self.archive.ensure_shared_activity(self.commitment)
        async def mark(*args):
            pass

        self.runtime = LivingStateService(
            archive=lambda: self.archive,
            session_id=lambda event: event.unified_msg_origin,
            message_id=lambda event: event.message_id,
            event_was_recalled=lambda event: False,
            message_was_recalled=lambda scope, message_id: False,
            notify_status=mark,
        )

    async def asyncTearDown(self):
        await self.archive.aclose()
        self.directory.cleanup()

    async def transition(self, state, message, **extra):
        result = await self.archive.update_shared_activity(
            self.activity["id"],
            self.scope,
            state=state,
            detail=message,
            progress=extra.get("progress", ""),
            evidence_id=message,
            evidence=message,
            revision=extra.get("revision", self.activity["revision"]),
        )
        self.activity = result
        return result

    async def test_goal_review_evidence_survives_checkpoint_trim_and_restart(self):
        await self.archive.save_day(DayRecord(date="2026-10-11", state=LifeState()))
        now = datetime.datetime(2026, 10, 11, 18)

        def produce(_day, world):
            apply_goal_decisions(
                world,
                {
                    "new_goals": [
                        {
                            "owner": "self",
                            "source_id": "persona",
                            "title": "整理照片构图",
                            "reason": "喜欢摄影",
                            "steps": [
                                {
                                    "id": "one",
                                    "title": "写构图笔记",
                                    "action_type": "study",
                                    "required_minutes": 10,
                                    "evidence_kind": "artifact",
                                }
                            ],
                        }
                    ]
                },
                sources={"persona"},
                now=now,
            )
            goal = world["goals"][0]
            action = {
                "action_id": "photo-study",
                "action_type": "study",
                "target": "整理构图",
            }
            run = {
                "goal_id": goal["id"],
                "step_id": "one",
                "active_seconds": 600,
                "action": action,
            }
            credit_goal(world, run, now)
            record_action_outcome(
                world, action, status="committed", run=run, completed_at=now
            )
            apply_action_reflections(
                world,
                [
                    {
                        "action_id": "photo-study",
                        "summary": "笔记写好了",
                        "artifact": {
                            "kind": "practice_note",
                            "content": "居中构图更稳定，三分构图留白更多。",
                        },
                    }
                ],
                allowed_action_ids={"photo-study"},
                now=now,
            )
            for index in range(81):
                record_action_outcome(
                    world,
                    {
                        "action_id": f"unrelated-{index}",
                        "action_type": "study",
                        "target": "其他练习",
                    },
                    status="committed",
                    completed_at=now,
                )

        await self.archive.mutate_continuous_life("2026-10-11", produce)
        await self.archive.aclose()
        self.archive = LifeArchive(self.path)
        restored = await self.archive.get_continuous_life()
        candidates = review_candidates(restored)
        self.assertEqual(len(candidates), 1)
        self.assertIn("居中构图", candidates[0]["evidence"][0]["content"])

    async def test_shared_activity_seven_day_pause_recovery_and_finish(self):
        await self.transition("preparing", "先准备饮料")
        await self.transition("active", "准备好了，开始吧", progress="开头")
        await self.transition("paused", "等我洗个澡", progress="第18分钟")
        before = copy.deepcopy(self.activity)
        await self.archive.aclose()
        self.archive = LifeArchive(self.path)
        for day in range(12, 19):
            await self.archive.save_day(
                DayRecord(date=f"2026-10-{day}", state=LifeState())
            )
            restored = await self.archive.get_living_entity(
                self.activity["id"], self.scope
            )
            self.assertEqual(restored["state"], "paused")
            self.assertEqual(restored["progress"], "第18分钟")
            self.assertEqual(restored["revision"], before["revision"])
        await self.transition("active", "回来了，继续18分钟那里")
        await self.transition(
            "completed", "看完啦，结尾很喜欢", progress="用户确认看完"
        )
        self.assertEqual(
            (await self.archive.get_commitment(self.commitment.id)).status, "done"
        )
        with self.assertRaises(ValueError):
            await self.transition("active", "重放不能复用已结束经历")
        self.assertEqual(
            len(
                await self.archive.get_living_entities(
                    self.other, kind="shared_activity"
                )
            ),
            0,
        )

    async def test_concurrent_old_revision_and_replayed_message(self):
        old = self.activity["revision"]
        current = await self.transition("active", "开始吧")
        replay = await self.archive.update_shared_activity(
            current["id"],
            self.scope,
            state="active",
            detail="开始吧",
            progress="",
            evidence_id="开始吧",
            evidence="开始吧",
            revision=old,
        )
        self.assertEqual(replay["revision"], current["revision"])
        with self.assertRaises(ValueError):
            await self.transition("paused", "旧任务不能覆盖", revision=old)
        self.assertEqual(
            (await self.archive.get_living_entity(current["id"], self.scope))["state"],
            "active",
        )

    async def test_explicit_message_evidence_and_scope_are_required(self):
        event = SimpleNamespace(
            unified_msg_origin=self.scope, message_id="m1", message_str="等我洗个澡"
        )
        result = await self.runtime.manage_shared_activity(
            event,
            action="pause",
            activity_id=self.activity["id"],
            detail="暂时离开",
            evidence="洗个澡",
            revision=self.activity["revision"],
        )
        self.assertIn("完整原话", result)
        result = await self.runtime.manage_shared_activity(
            event,
            action="pause",
            activity_id=self.activity["id"],
            detail="暂时离开",
            evidence=event.message_str,
            revision=self.activity["revision"],
        )
        self.assertEqual(json.loads(result)["state"], "paused")

    async def test_cancelled_commitment_is_not_presented_as_resumable_activity(self):
        await self.transition("paused", "等明天再看")
        await self.archive.set_commitment_status(self.commitment.id, "cancelled")
        current = await self.archive.get_living_entity(self.activity["id"], self.scope)
        self.assertEqual(current["state"], "cancelled")
        self.assertIn("已取消", await self.runtime.living_context_for_scope(self.scope))
        with self.assertRaises(ValueError):
            await self.transition("active", "不能复活已取消的约定")

    async def test_object_keeps_location_progress_and_stale_update_cannot_erase_it(
        self,
    ):
        item = await self.archive.update_life_object(
            self.scope,
            entity_id="",
            title="正在读的书",
            state="in_use",
            location="书桌",
            progress="读到第3章",
            content="",
            evidence_id="book1",
            evidence="我读到第3章",
            revision=0,
        )
        edited = await self.archive.update_life_object(
            self.scope,
            entity_id=item["id"],
            title="",
            state="stored",
            location="书架",
            progress="",
            content="",
            evidence_id="book2",
            evidence="放回书架",
            revision=1,
        )
        self.assertEqual(edited["progress"], "读到第3章")
        self.assertEqual(edited["location"], "书架")
        with self.assertRaises(ValueError):
            await self.archive.update_life_object(
                self.other,
                entity_id=item["id"],
                title="错误归属",
                state="available",
                location="",
                progress="",
                content="",
                evidence_id="bad",
                evidence="bad",
                revision=2,
            )

    async def test_correction_is_immediate_scoped_and_cannot_be_revived_by_old_job(
        self,
    ):
        original = LongTermMemoryRecord(
            scope=self.scope,
            category="event",
            title="照片日期",
            content="照片是昨天拍的",
            source_table="events",
            source_id="event1",
        )
        saved = await self.archive.upsert_long_term_memory(original)
        await self.archive.correct_life_memory(
            self.scope,
            saved.id,
            "照片是前天拍的",
            evidence_id="c1",
            evidence="记错了，是前天拍的",
        )
        self.assertIsNone(await self.archive.upsert_long_term_memory(original))
        self.assertIsNone(
            await self.archive.upsert_long_term_memory(
                LongTermMemoryRecord(
                    scope=self.scope,
                    category="event",
                    title="照片日期",
                    content="照片今天拍的",
                    source_table="events",
                    source_id="event1",
                )
            )
        )
        hits = await self.archive.list_recent_long_term_memories(
            scopes=[self.scope], limit=20
        )
        self.assertNotIn("照片是昨天拍的", [item.content for item in hits])
        self.assertIn("照片是前天拍的", [item.content for item in hits])
        snapshot = await self.archive.get_context_snapshot(
            max_summaries=3, session_id=self.scope, experience_scope=self.scope
        )
        self.assertIn(
            "照片是前天拍的", LayerChainComponent._format_cognition_context(snapshot)
        )
        self.assertNotIn(
            "照片是前天拍的", await self.runtime.living_context_for_scope(self.other)
        )
        with self.assertRaises(ValueError):
            await self.archive.correct_life_memory(
                self.other, saved.id, "错误", evidence_id="bad", evidence="错误"
            )

    async def test_actual_notes_are_archived_once_and_plan_is_not_an_object(self):
        await self.archive.save_day(DayRecord(date="2026-10-11", state=LifeState()))

        def produce(day, world):
            world["body"] = {"energy": 60}
            outcome = record_action_outcome(
                world,
                {
                    "action_id": "a1",
                    "action_type": "study",
                    "target": "笔记",
                    "payload": {"artifact": "计划写笔记"},
                },
                status="committed",
            )
            self.assertIsNone(outcome["artifact"])

        await self.archive.mutate_continuous_life("2026-10-11", produce)
        self.assertEqual(
            await self.archive.get_living_entities(self.scope, kind="object"), []
        )

        def reflect(day, world):
            apply_action_reflections(
                world,
                [
                    {
                        "action_id": "a1",
                        "summary": "整理笔记",
                        "artifact": {
                            "kind": "practice_note",
                            "content": "今天比较了两种构图",
                        },
                    }
                ],
                allowed_action_ids={"a1"},
                now=datetime.datetime(2026, 10, 11, 20),
            )

        await self.archive.mutate_continuous_life("2026-10-11", reflect)
        await self.archive.mutate_continuous_life("2026-10-11", reflect)
        items = await self.archive.get_living_entities(self.scope, kind="object")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "今天比较了两种构图")

    async def test_multiple_objects_in_one_message_have_separate_stable_ids(self):
        async def record(title):
            return await self.archive.update_life_object(
                self.scope,
                entity_id="",
                title=title,
                state="available",
                location="书桌",
                progress="",
                content="",
                evidence_id="message:two-objects",
                evidence="书和笔记本都放在书桌了",
                revision=0,
            )

        book, notebook = await record("书"), await record("笔记本")
        self.assertNotEqual(book["id"], notebook["id"])
        self.assertEqual((await record("书"))["revision"], 1)

    async def test_correcting_a_correction_replaces_projection_and_rejects_stale_work(
        self,
    ):
        original = await self.archive.upsert_long_term_memory(
            LongTermMemoryRecord(
                scope=self.scope,
                category="event",
                title="拍摄日期",
                content="昨天拍的",
                source_table="events",
                source_id="dated-photo",
            )
        )
        first = await self.archive.correct_life_memory(
            self.scope,
            original.id,
            "前天拍的",
            evidence_id="date-1",
            evidence="是前天",
        )
        current = (
            await self.archive.list_recent_long_term_memories(scopes=[self.scope])
        )[0]
        second = await self.archive.correct_life_memory(
            self.scope,
            current.id,
            "三天前拍的",
            evidence_id="date-2",
            evidence="查了是三天前",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["original_content"], "昨天拍的")
        with self.assertRaises(ValueError):
            await self.archive.correct_life_memory(
                self.scope,
                original.id,
                "陈旧判断",
                evidence_id="stale",
                evidence="旧批次",
                expected_content="昨天拍的",
            )
        context = await self.runtime.living_context_for_scope(self.scope)
        self.assertIn("三天前拍的", context)
        self.assertNotIn("前天拍的", context)
        self.assertEqual(
            len(await self.archive.get_living_entities(self.scope, kind="correction")),
            1,
        )

    async def test_recalled_message_cannot_apply_background_correction(self):
        original = await self.archive.upsert_long_term_memory(
            LongTermMemoryRecord(
                scope=self.scope,
                category="event",
                content="旧记录",
            )
        )
        self.runtime._was_recalled = lambda scope, message_id: (
            message_id == "recalled"
        )
        await self.runtime.note_memory_corrections(
            [
                {
                    "memory_id": original.id,
                    "source_message_id": "recalled",
                    "evidence": "纠正一下",
                    "correction": "新记录",
                }
            ],
            {
                "session_id": self.scope,
                "messages": [
                    {
                        "role": "user",
                        "message_id": "recalled",
                        "message_text": "纠正一下",
                    }
                ],
                "correctable_memories": [{"id": original.id, "content": "旧记录"}],
            },
        )
        self.assertEqual(
            await self.archive.get_living_entities(self.scope, kind="correction"), []
        )

    async def test_v22_upgrade_preserves_memory_and_repairs_fts_updates(self):
        original = await self.archive.upsert_long_term_memory(
            LongTermMemoryRecord(
                scope=self.scope,
                category="event",
                title="date",
                content="old photo date",
            )
        )
        await self.archive.aclose()
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            DROP INDEX idx_continuous_entities_scope;
            UPDATE meta SET value='22' WHERE key='schema_version';
            DROP TRIGGER long_term_memories_au;
            CREATE TRIGGER long_term_memories_au AFTER UPDATE OF title,content,category,status ON long_term_memories
            BEGIN
                INSERT INTO long_term_memories_fts(long_term_memories_fts,rowid,title,content,category)
                VALUES('delete',old.id,old.title,old.content,old.category);
            END;
        """)
        conn.close()
        self.archive = LifeArchive(self.path)
        await self.archive.correct_life_memory(
            self.scope,
            original.id,
            "new photo date",
            evidence_id="migration-correction",
            evidence="actual date",
        )
        self.assertEqual(
            self.archive._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )
        self.assertEqual(
            self.archive._conn.execute(
                "SELECT count(*) FROM long_term_memories_fts WHERE content='old photo date'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.archive._conn.execute(
                "SELECT count(*) FROM long_term_memories_fts WHERE content='new photo date'"
            ).fetchone()[0],
            1,
        )

    async def test_media_objects_require_real_registered_artifacts(self):
        self.assertIsNone(await self.archive.record_media_object(999))
        task = await self.archive.enqueue_durable_task(
            "media:one",
            "media_delivery",
            {
                "scope": self.scope,
                "media_kind": "image",
                "artifacts": ["/tmp/actual-image.png"],
                "evidence": "构图练习成品",
                "source_message_id": "image1",
            },
        )
        item = await self.archive.record_media_object(task.id)
        again = await self.archive.record_media_object(task.id)
        self.assertEqual(item["id"], again["id"])
        self.assertEqual(item["resource"]["artifacts"], ["/tmp/actual-image.png"])

    async def test_archive_work_keeps_event_loop_responsive(self):
        ticks = 0

        async def beat():
            nonlocal ticks
            for _ in range(10):
                ticks += 1
                await asyncio.sleep(0.01)

        await asyncio.gather(self.archive._run_db(lambda: time.sleep(0.12)), beat())
        self.assertEqual(ticks, 10)
