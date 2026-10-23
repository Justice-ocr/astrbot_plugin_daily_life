import datetime
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import support  # noqa: F401
from core.archive import LifeArchive
from core.clock import TIMEZONE, now as life_now
from core.runtime.integration import ExternalActivityService
from core.runtime.sharegate import ShareContactGate
from core.runtime.sharememory import search_share_memories


class ShareRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_replay_preserves_time_and_durable_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = LifeArchive(Path(folder) / "life.db")
            capture, mark, memos = AsyncMock(), Mock(), Mock()
            service = ExternalActivityService(
                archive=lambda: archive,
                resolve_target=AsyncMock(),
                note_structured_bot=Mock(),
                capture_proactive_reply=capture,
                mark_proactive_sent=mark,
                note_proactive_bot=Mock(),
                schedule_memos=memos,
            )
            original = life_now() - datetime.timedelta(minutes=30)
            receipt = {
                "event_id": "send-1",
                "target_umo": "qq:FriendMessage:a",
                "content": "已发内容",
                "occurred_at": original.replace(tzinfo=TIMEZONE).isoformat(),
                "sync_memory": True,
            }
            try:
                self.assertTrue(await service.record_external_activity_receipt(receipt))
                self.assertTrue(await service.record_external_activity_receipt(receipt))
                self.assertEqual(
                    len(
                        await archive.get_durable_tasks(
                            kind="external_activity_receipt"
                        )
                    ),
                    1,
                )
                self.assertEqual(capture.await_args.kwargs["now"], original)
                mark.assert_called_once()
                self.assertEqual(mark.call_args.args[1], original)
                memos.assert_called_once()
                service._note_structured_bot.assert_not_called()
            finally:
                archive.close()

    async def test_public_history_never_reads_private_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = LifeArchive(Path(folder) / "life.db")
            try:
                await archive.record_external_activity_receipt(
                    {
                        "event_id": "private-a",
                        "target_umo": "qq:FriendMessage:a",
                        "content": "摄影私人资料",
                    }
                )
                await archive.record_external_activity_receipt(
                    {
                        "event_id": "private-b",
                        "target_umo": "qq:FriendMessage:b",
                        "content": "别人摄影资料",
                    }
                )
                await archive.record_public_activity(
                    {
                        "event_id": "public",
                        "scene": "qzone_reply",
                        "content": "公开摄影讨论",
                        "scope": "album_batch",
                        "post_context": {"content": "摄影原动态"},
                        "conversation": [
                            {"author_name": "评论者", "content": "摄影评论"}
                        ],
                    }
                )
                self.assertEqual(
                    [
                        x["event_id"]
                        for x in await archive.read_share_activity_receipts(
                            "qq:FriendMessage:a"
                        )
                    ],
                    ["private-a"],
                )
                result = await search_share_memories(
                    types.SimpleNamespace(archive=archive), "摄影", "qzone_broadcast"
                )
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0]["source_id"], "public")
                self.assertIn("摄影原动态", result[0]["text"])
                self.assertIn("摄影评论", result[0]["text"])
                self.assertNotIn("私人", str(result))
            finally:
                archive.close()

    async def test_memory_lookup_filters_wrong_scope_and_cancelled_entities(self):
        scope = "qq:FriendMessage:a"
        archive = types.SimpleNamespace(
            search_long_term_memories=AsyncMock(
                return_value=[
                    types.SimpleNamespace(
                        scope="qq:FriendMessage:b", status="active", content="摄影秘密"
                    ),
                    types.SimpleNamespace(
                        scope=scope, status="superseded", content="摄影旧记忆"
                    ),
                ]
            ),
            get_living_entities=AsyncMock(
                return_value=[
                    {
                        "scope": scope,
                        "kind": "shared_activity",
                        "state": "cancelled",
                        "title": "摄影活动取消",
                    },
                    {
                        "scope": scope,
                        "kind": "correction",
                        "state": "active",
                        "title": "摄影安排",
                        "content": "摄影改到周末",
                    },
                ]
            ),
            get_continuous_life=AsyncMock(return_value={}),
            read_share_activity_receipts=AsyncMock(return_value=[]),
        )
        result = await search_share_memories(
            types.SimpleNamespace(archive=archive), "摄影", scope
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "corrections")
        self.assertIn("改到周末", result[0]["text"])

    def gate(self):
        runtime = types.SimpleNamespace(
            archive=types.SimpleNamespace(
                get_continuous_life=AsyncMock(
                    return_value={
                        "run": {
                            "status": "running",
                            "action": {"action_id": "a", "target": "摄影"},
                        }
                    }
                ),
                get_conversation_receipt_before=AsyncMock(return_value=0),
            ),
            _media_chat_tokens={},
        )
        return runtime, ShareContactGate(runtime)

    async def test_same_scope_excludes_other_proactive_jobs(self):
        runtime, gate = self.gate()
        self.assertTrue((await gate.coordinate("a", owner="share"))["allowed"])
        self.assertFalse((await gate.coordinate("a", owner="other"))["allowed"])
        self.assertTrue((await gate.coordinate("b", owner="other"))["allowed"])
        self.assertFalse(gate.allow_life(types.SimpleNamespace(unified_msg_origin="a")))
        await gate.coordinate("a", owner="share", action="release")
        self.assertTrue(gate.allow_life(types.SimpleNamespace(unified_msg_origin="a")))

    async def test_new_chat_or_action_invalidates_prepared_share(self):
        runtime, gate = self.gate()
        first = await gate.coordinate("a", owner="share")
        runtime._media_chat_tokens[("a", "user")] = 2
        result = await gate.coordinate(
            "a", owner="share", action="check", expected=first["snapshot"]
        )
        self.assertTrue(result["stale"])
        self.assertNotIn("a", gate.leases)
        first = await gate.coordinate("a", owner="share")
        runtime.archive.get_continuous_life.return_value["run"]["status"] = "paused"
        result = await gate.coordinate(
            "a", owner="share", action="check", expected=first["snapshot"]
        )
        self.assertFalse(result["allowed"])

    async def test_recent_delivery_prevents_duplicate_contact_without_leaking_lease(
        self,
    ):
        runtime, gate = self.gate()
        runtime.archive.get_conversation_receipt_before.return_value = (
            life_now().replace(tzinfo=TIMEZONE).timestamp() - 30
        )
        result = await gate.coordinate("a", owner="share")
        self.assertFalse(result["allowed"])
        self.assertTrue(result["retryable"])
        self.assertNotIn("a", gate.leases)

    async def test_outgoing_fallback_does_not_invalidate_prepared_share(self):
        runtime, gate = self.gate()
        now = life_now()
        with patch("core.runtime.sharegate.life_now", return_value=now):
            first = await gate.coordinate("a", owner="share")
            # 长时间生成导致租约过期，期间由其他任务发送备用内容。
            gate.leases["a"] = ("share", 0)
            self.assertTrue(gate.allow_life(types.SimpleNamespace(unified_msg_origin="a")))
            gate.leases["a"] = (gate.leases["a"][0], 0)
            runtime.archive.get_conversation_receipt_before.return_value = (
                now.replace(tzinfo=TIMEZONE).timestamp() - 30
            )
            result = await gate.coordinate(
                "a", owner="share", action="check", expected=first["snapshot"]
            )
            self.assertFalse(result["allowed"])
            self.assertTrue(result["retryable"])
            self.assertFalse(result.get("stale"))
            self.assertNotIn("a", gate.leases)
        with patch(
            "core.runtime.sharegate.life_now",
            return_value=now + datetime.timedelta(seconds=91),
        ):
            runtime._media_chat_tokens[("b", "user")] = "other-chat"
            result = await gate.coordinate(
                "a", owner="share", action="check", expected=first["snapshot"]
            )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["snapshot"], first["snapshot"])

    async def test_busy_contact_can_retry_with_the_same_snapshot(self):
        runtime, gate = self.gate()
        first = await gate.coordinate("a", owner="share")
        await gate.coordinate("a", owner="share", action="release")
        await gate.coordinate("a", owner="other")
        result = await gate.coordinate(
            "a", owner="share", action="check", expected=first["snapshot"]
        )
        self.assertTrue(result["retryable"])
        self.assertEqual(gate.leases["a"][0], "other")
        await gate.coordinate("a", owner="other", action="release")
        self.assertTrue((await gate.coordinate(
            "a", owner="share", action="check", expected=first["snapshot"]
        ))["allowed"])

    async def test_new_incoming_message_still_invalidates_during_contact_cooldown(self):
        runtime, gate = self.gate()
        first = await gate.coordinate("a", owner="share")
        runtime.archive.get_conversation_receipt_before.return_value = (
            life_now().replace(tzinfo=TIMEZONE).timestamp() - 30
        )
        runtime._media_chat_tokens[("a", "user")] = "new-message"
        result = await gate.coordinate(
            "a", owner="share", action="check", expected=first["snapshot"]
        )
        self.assertTrue(result["stale"])
        self.assertFalse(result.get("retryable"))
        self.assertNotIn("a", gate.leases)
