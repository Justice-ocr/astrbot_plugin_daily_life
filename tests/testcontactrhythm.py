"""验证跨插件联系回执、持续主动性与公开上下文边界。"""

import datetime
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import support  # noqa: F401

from core.archive import LifeArchive
from core.clock import local_timestamp
from core.runtime.contact import contact_rhythm
from core.runtime.sharegate import ShareContactGate


class ContactRhythmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "life.db"
        self.archive = LifeArchive(self.path)
        self.scope = "qq:FriendMessage:alice"
        self.now = datetime.datetime(2026, 10, 27, 15)
        self.stamp = local_timestamp(self.now)
        self.runtime = types.SimpleNamespace(
            archive=self.archive,
            config=types.SimpleNamespace(proactive=types.SimpleNamespace(
                private_talk_frequency=0.85, talk_frequency=0.35,
                private_cooldown_minutes=3,
            )),
        )

    async def asyncTearDown(self):
        self.archive.close()
        self.folder.cleanup()

    async def test_segments_and_media_count_as_one_contact(self):
        await self.archive.save_conversation_arrival(self.scope, self.stamp - 1000)
        for delay in (300, 295, 290):
            await self.archive.save_conversation_receipt(self.scope, self.stamp - delay)
        rhythm = await contact_rhythm(self.runtime, self.scope, self.now)
        self.assertEqual(rhythm["unanswered_contacts"], 1)
        self.assertTrue(rhythm["ready"])

    async def test_unanswered_contacts_slow_down_across_plugins(self):
        await self.archive.save_conversation_arrival(self.scope, self.stamp - 1000)
        for delay in (500, 495, 100):
            await self.archive.save_conversation_receipt(self.scope, self.stamp - delay)
        rhythm = await contact_rhythm(self.runtime, self.scope, self.now)
        self.assertEqual(rhythm["unanswered_contacts"], 2)
        self.assertEqual(rhythm["retry_after"], 260)
        self.assertFalse(rhythm["ready"])
        other = await contact_rhythm(self.runtime, "qq:FriendMessage:bob", self.now)
        self.assertTrue(other["ready"])
        self.assertEqual(other["unanswered_contacts"], 0)

    async def test_user_response_resets_unanswered_contact_pressure(self):
        for delay in (1000, 700, 400):
            await self.archive.save_conversation_receipt(self.scope, self.stamp - delay)
        await self.archive.save_conversation_arrival(self.scope, self.stamp - 200)
        rhythm = await contact_rhythm(self.runtime, self.scope, self.now)
        self.assertEqual(rhythm["unanswered_contacts"], 0)
        self.assertTrue(rhythm["ready"])

    async def test_contact_pressure_survives_reload(self):
        await self.archive.save_conversation_arrival(self.scope, self.stamp - 1000)
        for delay in (500, 100):
            await self.archive.save_conversation_receipt(self.scope, self.stamp - delay)
        before = await contact_rhythm(self.runtime, self.scope, self.now)
        self.archive.close()
        self.archive = LifeArchive(self.path)
        self.runtime.archive = self.archive
        self.assertEqual(await contact_rhythm(self.runtime, self.scope, self.now), before)

    async def test_public_context_contains_only_initiative(self):
        await self.archive.save_conversation_receipt(self.scope, self.stamp - 20)
        self.assertEqual(
            await contact_rhythm(self.runtime, "qzone_broadcast", self.now),
            {"willingness": 0.85},
        )
        self.assertEqual(
            await contact_rhythm(self.runtime, "qq:GroupMessage:g", self.now),
            {"willingness": 0.35},
        )

    async def test_old_and_future_receipts_do_not_build_current_pressure(self):
        for delay in (7 * 3600, -60):
            await self.archive.save_conversation_receipt(self.scope, self.stamp - delay)
        rhythm = await contact_rhythm(self.runtime, self.scope, self.now)
        self.assertEqual(rhythm["unanswered_contacts"], 0)
        self.assertTrue(rhythm["ready"])

    async def test_shared_gate_applies_same_private_contact_pressure(self):
        for delay in (500, 100):
            await self.archive.save_conversation_receipt(self.scope, self.stamp - delay)
        gate = ShareContactGate(self.runtime)
        with patch("core.runtime.sharegate.life_now", return_value=self.now):
            result = await gate.coordinate(self.scope, owner="daily_share:test")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["retry_after"], 260)
        self.assertNotIn(self.scope, gate.leases)
