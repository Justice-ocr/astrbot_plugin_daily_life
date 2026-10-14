import base64
import datetime
import io
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import support  # noqa: F401
from PIL import Image

from core.archive import LifeArchive
from core.runtime.mirror.sharing import project_continuity, share_timing
from componentfixtures import RuntimeActionReceiptComponent
from core.sight.sharemedia import describe_share_video, prepare_share_image


class ShareContinuityTests(unittest.IsolatedAsyncioTestCase):
    def test_target_privacy_and_goal_review_evidence(self):
        private = "qq:FriendMessage:alice"
        items = [
            {
                "scope": private,
                "kind": "shared_activity",
                "title": "共同看电影",
                "state": "cancelled",
                "resource": "/private/secret",
            },
            {
                "scope": "qq:FriendMessage:bob",
                "kind": "correction",
                "content": "别人的私事",
            },
            {"scope": "global", "kind": "correction", "content": "未标公开的纠错"},
            {
                "scope": "qzone_broadcast",
                "kind": "object",
                "title": "公开作品",
                "delivery": "not_confirmed",
                "resource": "/private/asset.png",
            },
        ]
        world = {
            "kernel": {
                "self_model": {
                    "interests": [
                        {
                            "id": "camera",
                            "status": "supported",
                            "label": "摄影",
                            "disposition": "paused",
                            "evidence_ids": ["persona"],
                        },
                        {
                            "id": "secret",
                            "status": "supported",
                            "label": "私人偏好",
                            "evidence_ids": ["memory:x"],
                        },
                        {
                            "id": "mixed",
                            "status": "supported",
                            "label": "混合证据兴趣",
                            "evidence_ids": ["persona", "memory:x"],
                        },
                    ]
                }
            },
            "goals": [
                {
                    "title": "摄影练习",
                    "status": "completed",
                    "interest_id": "camera",
                    "steps": [{"title": "构图", "status": "completed", "reviews": []}],
                },
            ],
        }
        local = project_continuity(items, world, private)
        self.assertEqual(local["activities"][0]["state"], "cancelled")
        self.assertNotIn("别人的私事", json.dumps(local, ensure_ascii=False))
        public = project_continuity(items, world, "qzone_broadcast")
        rendered = json.dumps(public, ensure_ascii=False)
        for secret in ("共同看电影", "别人的私事", "未标公开", "私人偏好", "混合证据兴趣", "/private/"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(public["objects"][0]["delivery"], "not_confirmed")
        self.assertEqual(public["interests"][0]["state"], "paused")
        self.assertEqual(public["goals"][0]["state"], "active")
        self.assertEqual(public["goals"][0]["steps"][0]["state"], "awaiting_review")
        group = project_continuity(items, world, "qq:GroupMessage:group")
        self.assertEqual(group["activities"], [])
        self.assertEqual(group["corrections"], [])

    async def test_timing_keeps_shanghai_date_and_group_scope(self):
        archive = types.SimpleNamespace(
            get_conversation_receipt_before=AsyncMock(return_value=1791666000)
        )
        with patch(
            "core.runtime.mirror.sharing.life_now",
            return_value=datetime.datetime(2026, 10, 11, 8),
        ):
            result = await share_timing(
                types.SimpleNamespace(archive=archive), "qq:GroupMessage:g"
            )
        self.assertEqual(result["now"], "2026-10-11T08:00:00+08:00")
        self.assertIn("+08:00", result["last_sent_at"])
        self.assertEqual(result["scope_kind"], "group")
        self.assertEqual(
            await share_timing(
                types.SimpleNamespace(archive=archive), "qzone_broadcast"
            ),
            {},
        )

    async def test_media_ready_is_not_sent_and_delivery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = LifeArchive(Path(folder) / "life.db")
            try:
                runtime = RuntimeActionReceiptComponent()
                runtime.archive = archive
                runtime.note_conversation_sent = lambda *args: None
                scope = "qq:FriendMessage:alice"
                task = await archive.enqueue_durable_task(
                    runtime._share_asset_key("job:image", "image"),
                    "share_image_generation",
                    {
                        "external_owner": "daily_share",
                        "task_key": "job:image",
                        "scope": scope,
                    },
                    lease_owner="test-share-delivery",
                    lease_seconds=60,
                )
                self.assertIsNone(await archive.record_share_media_object(task.id))
                self.assertTrue(
                    await archive.complete_durable_task(
                        task.id,
                        {"artifact_path": "/tmp/generated.png"},
                        owner="test-share-delivery",
                    )
                )
                self.assertEqual(
                    (await runtime.get_share_image_task("job:image"))["status"], "ready"
                )
                obj = (await archive.get_living_entities(scope))[0]
                self.assertEqual(obj["delivery"], "not_confirmed")
                self.assertEqual(
                    await archive.get_conversation_receipt_before(scope, time.time()), 0
                )
                stamp = time.time() - 1
                receipt = {
                    "target_umo": scope,
                    "sent_at": stamp,
                    "media_tasks": {"image": "job:image"},
                }
                self.assertTrue(await runtime.record_share_delivery(receipt))
                self.assertTrue(await runtime.record_share_delivery(receipt))
                obj = (await archive.get_living_entities(scope))[0]
                self.assertEqual(obj["delivery"], "sent")
                self.assertEqual(obj["revision"], 2)
                self.assertEqual(
                    await archive.get_conversation_receipt_before(scope, time.time()),
                    stamp,
                )
                self.assertEqual(
                    await archive.get_living_entities("qq:FriendMessage:bob"), []
                )
                for invalid in (
                    float("nan"),
                    float("inf"),
                    time.time() + 86400,
                    0,
                    True,
                ):
                    self.assertFalse(
                        await runtime.record_share_delivery(
                            {**receipt, "sent_at": invalid}
                        )
                    )
            finally:
                archive.close()

    async def test_gif_all_frames_become_one_model_image(self):
        output = io.BytesIO()
        frames = [
            Image.new("RGB", (10, 10), color) for color in ("red", "blue", "green")
        ]
        frames[0].save(
            output, format="GIF", save_all=True, append_images=frames[1:], duration=100
        )
        source = "data:image/gif;base64," + base64.b64encode(output.getvalue()).decode()
        result = await prepare_share_image(source)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["source"].startswith("data:image/jpeg;base64,"))
        self.assertIn("3 个采样画面", result["hint"])
        self.assertIn("不是多张独立图片", result["hint"])

    async def test_video_respects_life_switch_and_does_not_send_or_read_chat(self):
        runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(
                sight=types.SimpleNamespace(auto_video_understanding=False)
            ),
            _native_sight_route=AsyncMock(return_value=object()),
            _understand_native_video=AsyncMock(
                return_value=types.SimpleNamespace(
                    status="ready", summary="猫跳到椅子上"
                )
            ),
        )
        self.assertEqual(
            (await describe_share_video(runtime, "https://example.com/a.mp4"))[
                "status"
            ],
            "disabled",
        )
        runtime._native_sight_route.assert_not_awaited()
        runtime.config.sight.auto_video_understanding = True
        with patch(
            "core.sight.sharemedia.is_http_url_allowed_async", AsyncMock(return_value=True)
        ):
            result = await describe_share_video(runtime, "https://example.com/a.mp4")
        self.assertEqual(result["summary"], "猫跳到椅子上")
        event, clip, _ = runtime._understand_native_video.await_args.args
        self.assertIsNone(event)
        self.assertEqual(clip.scope, "qzone_broadcast")
        self.assertNotIn("私聊内容", clip.text)
