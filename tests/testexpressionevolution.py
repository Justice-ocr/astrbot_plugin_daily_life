"""用真实 SQLite 验证持续表达学习的证据、隔离、晋升和撤回。"""

import copy
import datetime
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import LifeSettings
from core.archive import LifeArchive
from core.archive.migrations import _migrate_expression_evolution
from core.models import ExpressionProfileRecord, ReplyEffectRecord
from core.clock import now as life_now
from core.runtime.expressionlearning import (bounded_profiles, learn_from_browsing,
    record_exposures, revision, select_profiles, settle_expression_feedback)
from core.runtime.expressionweight import expression_weight


class ExpressionEvolutionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "life.db"
        self.db = LifeArchive(self.path)
        self.runtime = SimpleNamespace(archive=self.db, config=LifeSettings.from_dict({}))

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    def profile(self, **changes):
        return ExpressionProfileRecord(
            **{"scope": "private-a", "profile_id": "a", "label": "闲聊衔接",
               "tone": "轻松而直接", "habits": ["先回应当下感受，再补一点自己的想法"],
               "evidence": "来自已发送表达的观察", "confidence": 0.9,
               "learning": {"breadth": "relationship"}, **changes})

    async def learn(self, profile=None, *, key="m1", kind="observation", outcome="neutral", scope="private-a", context="日常闲聊"):
        profile = profile or self.profile()
        return await self.db.apply_expression_learning(profile, {
            "event_key": key, "kind": kind, "outcome": outcome, "source_scope": scope,
            "evidence": {"source_id": key, "profile_revision": revision(profile), "context": context},
        })

    async def test_observation_is_weak_and_retry_does_not_reinforce(self):
        first = await self.learn()
        again = await self.learn()
        self.assertEqual(first.id, again.id)
        self.assertEqual(again.learning["observations"], 1)
        self.assertEqual(again.learning["status"], "emerging")
        self.assertLess(expression_weight(again), 0.25)
        for i in range(5):
            again = await self.learn(again, key=f"observe-{i}")
        self.assertEqual(again.learning["status"], "emerging")

    async def test_feedback_promotes_then_repeated_negative_cools(self):
        item = await self.learn()
        for i in range(3):
            item = await self.learn(item, key=f"positive-{i}", kind="feedback", outcome="positive")
        self.assertEqual(item.learning["status"], "stable")
        for i in range(2):
            item = await self.learn(item, key=f"negative-{i}", kind="feedback", outcome="negative")
        self.assertEqual(item.learning["status"], "cooling")
        self.assertEqual(await self.db.get_expression_context(scope="private-a"), [])
        item.avoid = ["不要每轮都补自己的经历"]
        fixed = await self.learn(item, key="correction", kind="explicit")
        self.assertEqual(fixed.learning["status"], "stable")
        self.assertEqual(fixed.avoid, item.avoid)

    async def test_personal_voice_requires_feedback_in_distinct_contexts(self):
        item = await self.learn(self.profile(scope="self", profile_id="", learning={"breadth": "voice"}))
        for i in range(3):
            item = await self.learn(item, key=f"same-{i}", kind="feedback", outcome="positive")
        self.assertEqual(item.learning["status"], "emerging")
        item = await self.learn(item, key="different-context", kind="feedback", outcome="positive", context="认真解释")
        self.assertEqual(item.learning["status"], "stable")

    async def test_unadopted_or_general_feedback_never_reinforces(self):
        item = await self.learn()
        watch = {"target_scope": "private-a", "message_id": "sent-1", "reply_text": "我也想慢慢聊", "expression_profiles": [item.as_dict()]}
        await record_exposures(self.db, watch)
        value = {"profile_id": item.id, "adopted": False, "outcome": "positive", "confidence": 0.9, "reason": "只是接话"}
        await settle_expression_feedback(self.db, watch, {"expression_effects": [value]}, "u2")
        read = (await self.db.get_expression_profiles())[0]
        self.assertEqual(read.learning["exposures"], 1)
        self.assertEqual(read.learning["applications"], 0)
        self.assertEqual(read.learning["positive"], 0)
        value.update(adopted=True, outcome="neutral")
        await settle_expression_feedback(self.db, watch, {"expression_effects": [value]}, "u3")
        read = (await self.db.get_expression_profiles())[0]
        self.assertEqual(read.learning["applications"], 1)
        self.assertEqual(read.learning["positive"], 0)

    async def test_old_feedback_cannot_change_corrected_profile(self):
        old = await self.learn()
        changed = copy.deepcopy(old)
        changed.tone = "需要解释时说完整"
        await self.learn(changed, key="fix", kind="explicit")
        self.assertIsNone(await self.learn(old, key="late", kind="feedback", outcome="negative"))
        current = (await self.db.get_expression_profiles())[0]
        self.assertEqual(current.learning["negative"], 0)

    async def test_correction_starts_new_evidence_epoch(self):
        item = await self.learn()
        for i in range(3):
            item = await self.learn(item, key=f"before-{i}", kind="feedback", outcome="positive")
        item.tone = "解释时完整，闲聊时简短"
        fixed = await self.learn(item, key="explicit-fix", kind="explicit")
        self.assertEqual(fixed.learning["positive"], 0)
        self.assertEqual(fixed.learning["status"], "stable")
        self.assertGreater(fixed.learning["epoch_event_id"], 0)

    async def test_cooled_voice_can_be_revised_without_inheriting_old_support(self):
        item = await self.learn(self.profile(scope="self", profile_id="", learning={"breadth": "voice"}))
        for i in range(2):
            item = await self.learn(item, key=f"negative-{i}", kind="feedback", outcome="negative")
        self.assertEqual(item.learning["status"], "cooling")
        item.tone = "轻松但不抢着解释"
        revised = await self.learn(item, key="new-understanding")
        self.assertEqual(revised.learning["status"], "emerging")
        self.assertEqual(revised.learning["negative"], 0)
        self.assertEqual(revised.tone, item.tone)

    async def test_exposure_does_not_refresh_style_recency(self):
        item = await self.learn()
        await self.db._run_db(lambda: (self.db._conn.execute("UPDATE expression_profiles SET updated_at='2020-01-01 00:00:00' WHERE id=?", (item.id,)), self.db._conn.commit()))
        exposed = await self.learn(item, key="shown", kind="exposure")
        self.assertEqual(exposed.updated_at, "2020-01-01 00:00:00")

    async def test_pending_observation_recovers_after_restart_and_settles_once(self):
        item = await self.learn()
        watch = {"target_scope": "private-a", "reply_token": "sent-1",
                 "target_message_id": "u1", "sent_at": life_now(),
                 "source": "regular_reply", "reply_text": "慢慢聊呀",
                 "expression_profiles": [item.as_dict()]}
        await self.db.save_reply_effect(ReplyEffectRecord(scope="private-a", target_message_id="u1", reply_text=watch["reply_text"], outcome="pending"))
        await record_exposures(self.db, watch)
        self.db.close()
        self.db = LifeArchive(self.path)
        restored = await self.db.recover_expression_watch(scope="private-a")
        self.assertIsInstance(restored["sent_at"], datetime.datetime)
        self.assertEqual(restored["reply_token"], "sent-1")
        self.assertEqual(restored["expression_profiles"][0]["evidence"], "")
        self.assertIsNone(await self.db.recover_expression_watch(scope="private-b"))
        await settle_expression_feedback(self.db, restored, {"expression_effects": []}, "u2")
        self.assertIsNone(await self.db.recover_expression_watch(scope="private-a"))

    async def test_multiple_feedback_messages_cannot_reinforce_one_reply_twice(self):
        item = await self.learn()
        watch = {"target_scope": "private-a", "reply_token": "sent-1", "reply_text": "慢慢聊", "expression_profiles": [item.as_dict()]}
        assessment = {"expression_effects": [{"profile_id": item.id, "adopted": True, "outcome": "positive", "confidence": 0.9, "reason": "用户明确认可这种节奏"}]}
        for feedback_id in ("u1", "u2", "u1"):
            await settle_expression_feedback(self.db, watch, assessment, feedback_id)
        read = (await self.db.get_expression_profiles())[0]
        self.assertEqual(read.learning["positive"], 1)
        self.assertEqual(read.learning["applications"], 1)

    async def test_private_preference_is_scoped_while_own_voice_is_shared(self):
        await self.learn()
        own = await self.learn(self.profile(scope="self", profile_id="", learning={"breadth": "voice"}))
        other = await self.db.get_expression_context(scope="private-b", profile_id="b")
        self.assertEqual([p.id for p in other], [own.id])
        await self.learn(self.profile(scope="group", profile_id="a"), key="group-a")
        self.assertEqual([p.id for p in await self.db.get_expression_context(scope="group", profile_id="b")], [own.id])

    async def test_selection_keeps_general_preferences_and_uses_semantics_for_scene(self):
        general = await self.learn(kind="explicit")
        await self.learn(self.profile(label="睡前", learning={"breadth": "situation"}), key="sleep", kind="explicit")
        work = await self.learn(self.profile(label="工作", learning={"breadth": "situation"}), key="work", kind="explicit")
        self.runtime.rank_semantic_groups = AsyncMock(return_value={"expression": [work]})
        selected = await select_profiles(self.runtime, scope="private-a", profile_id="a", query="刚忙完工作")
        self.assertEqual([p.id for p in selected], [general.id, work.id])
        self.assertEqual(len(bounded_profiles([self.profile(id=i, learning={"status": "emerging"}) for i in range(1, 5)])), 1)

    async def test_browsing_only_learns_grounded_self_expression(self):
        raw = {"label": "把感受说具体", "tone": "平实", "habits": ["用自己注意到的小细节表达感受"],
               "evidence": "片段表达观察", "persona_fit": "符合细腻但直接的性格", "confidence": 0.8}
        record = {"id": "bili:1", "scope": "private-a", "position": 30}
        await learn_from_browsing(self.runtime, raw, record, [{"text": "内容证据"}])
        self.assertEqual(await self.db.get_expression_profiles(), [])
        record["scope"] = "global"
        await learn_from_browsing(self.runtime, raw, record, [])
        self.assertEqual(await self.db.get_expression_profiles(), [])
        await learn_from_browsing(self.runtime, raw, record, [{"text": "内容证据"}])
        item = (await self.db.get_expression_profiles())[0]
        self.assertEqual((item.scope, item.profile_id, item.learning["status"]), ("self", "", "emerging"))

    async def test_state_and_deduplication_survive_reopening_database(self):
        item = await self.learn()
        self.db.close()
        self.db = LifeArchive(self.path)
        again = await self.learn()
        self.assertEqual(again.id, item.id)
        self.assertEqual(again.learning["observations"], 1)

    def test_v30_upgrade_preserves_profiles(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE expression_profiles(id INTEGER PRIMARY KEY, label TEXT)")
        conn.execute("INSERT INTO expression_profiles VALUES (1,'已有风格')")
        _migrate_expression_evolution(conn)
        _migrate_expression_evolution(conn)
        self.assertEqual(conn.execute("SELECT label, learning_json FROM expression_profiles").fetchone(), ("已有风格", "{}"))
        conn.close()
