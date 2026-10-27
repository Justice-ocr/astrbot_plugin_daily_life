"""合影对象来自明确选择或私聊身份，不能靠昵称、提示词或档案数量猜测。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import support  # noqa: F401 - 先安装 AstrBot 测试替身。

# isort: split

from core.runtime.channel.perspective import (
    group_participants_required,
    resolve_group_participants,
)
from runtimehelpers import Context, DailyLifeRuntime, Event, LifeSettings, Provider


class GroupParticipantsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profiles = [{"profile_id": "10001", "display_name": "阿遥"}]
        self.host = SimpleNamespace(
            media=SimpleNamespace(
                image=SimpleNamespace(friend_reference_options=lambda: self.profiles)
            )
        )
        self.event = Event(
            sender_id="10001", unified_msg_origin="onebot-main:FriendMessage:10001"
        )

    async def test_omitted_participant_matches_sender_among_multiple_profiles(self):
        self.profiles.append({"profile_id": "10002", "display_name": "另一个好友"})
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), ["10001"]
        )
        self.assertEqual(
            await resolve_group_participants(
                self.host, SimpleNamespace(event=self.event), []
            ),
            ["10001"],
        )

    async def test_explicit_friend_and_multiple_selection_are_preserved(self):
        for participants in (["10002"], ["10001", "10002"]):
            with self.subTest(participants=participants):
                self.assertEqual(
                    await resolve_group_participants(
                        self.host, self.event, participants
                    ),
                    participants,
                )

    async def test_single_unrelated_profile_and_matching_name_are_insufficient(self):
        self.profiles[0]["profile_id"] = "10002"
        self.event._sender_name = "阿遥"
        self.event.message_str = "我就是阿遥，拍张我们两个的合影"
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), []
        )

    async def test_group_and_missing_private_identity_are_not_inferred(self):
        self.event.unified_msg_origin = "onebot-main:GroupMessage:10001"
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), []
        )
        self.event.unified_msg_origin = ""
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), []
        )

    async def test_private_origin_supplies_identity_when_sender_method_is_unavailable(
        self,
    ):
        self.event.get_sender_id = None
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), ["10001"]
        )

    async def test_relationship_account_resolves_alias_but_rejects_conflicts(self):
        self.profiles[0]["profile_id"] = "friend-profile"
        record = SimpleNamespace(user_id="10001", platform="onebot-main")
        self.host.archive = SimpleNamespace(
            get_relationship=AsyncMock(return_value=record)
        )
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []),
            ["friend-profile"],
        )
        record.platform = "different-onebot"
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), []
        )
        record.platform = "onebot-main"
        record.user_id = "10002"
        self.profiles[0]["profile_id"] = "10001"
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), []
        )

    async def test_multiple_profiles_for_same_account_remain_ambiguous(self):
        self.profiles.append({"profile_id": "friend-profile", "display_name": "阿遥"})
        self.host.archive = SimpleNamespace(
            get_relationship=AsyncMock(
                return_value=SimpleNamespace(user_id="10001", platform="onebot-main")
            )
        )
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), []
        )

    async def test_identity_lookup_failure_does_not_select_someone_else(self):
        self.host.archive = SimpleNamespace(
            get_relationship=AsyncMock(side_effect=RuntimeError("档案暂不可读"))
        )
        self.assertEqual(
            await resolve_group_participants(self.host, self.event, []), []
        )

    def test_missing_selection_returns_repairable_parameter_and_configured_options(
        self,
    ):
        value = json.loads(group_participants_required(self.host))
        self.assertEqual(value["status"], "needs_parameters")
        self.assertEqual(value["required_parameters"], ["participants"])
        self.assertEqual(value["available_participants"], self.profiles)
        self.assertNotIn("未配置", value["message"])


class GroupAdmissionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        self.runtime.context = Context(Provider([]))
        self.runtime.config = LifeSettings.from_dict({})
        self.runtime.data_path = self.root / "daily_life.db"
        self.runtime.media = SimpleNamespace(
            image=SimpleNamespace(
                friend_reference_options=lambda: [
                    {"profile_id": "10001", "display_name": "阿遥"}
                ]
            )
        )
        self.runtime._current_life_appearance_snapshot = AsyncMock(return_value="")
        self.runtime._resolve_life_identity_profiles = AsyncMock(return_value={})
        self.runtime._build_person_fact_injection_context = AsyncMock(return_value="")
        self.runtime._release_media_chat_turn = Mock()
        self.scheduled = []

        def schedule(coro, **kwargs):
            self.scheduled.append(coro)
            self.addCleanup(coro.close)
            return True

        self.runtime._schedule_background_task = schedule
        self.event = Event(
            sender_id="10001", unified_msg_origin="onebot-main:FriendMessage:10001"
        )
        self.look = {
            "friend_outfit": "深灰色家居短袖和长裤",
            "friend_hair": "自然黑色短发",
            "friend_scene_category": "home",
        }

    async def test_all_three_tools_reach_look_validation_when_participant_is_omitted(
        self,
    ):
        for name in (
            "life_image_generate",
            "life_photo_suite_generate",
            "life_video_generate",
        ):
            with self.subTest(tool=name):
                value = json.loads(
                    await getattr(self.runtime, name)(
                        self.event, "客厅双人合影", subject_route="group"
                    )
                )
                self.assertEqual(value["status"], "needs_parameters")
                self.assertEqual(
                    value["required_parameters"],
                    ["friend_scene_category", "friend_outfit", "friend_hair"],
                )
        self.assertEqual(self.scheduled, [])

    async def test_video_locks_resolved_person_in_background_request(self):
        component = self.runtime._part_runtime_video_media_component
        component._life_video_generate_background = AsyncMock()
        value = json.loads(
            await self.runtime.life_video_generate(
                self.event, "客厅双人短视频", subject_route="group", **self.look
            )
        )
        self.assertEqual(value["status"], "pending")
        request = component._life_video_generate_background.call_args.args[0]
        self.event._sender_id = "10002"
        self.assertEqual(request.participants, ("10001",))
        self.assertEqual(request.event.get_sender_id(), "10001")

    async def test_suite_manifest_keeps_resolved_person_and_reply_identity(self):
        value = json.loads(
            await self.runtime.life_photo_suite_generate(
                self.event, "客厅双人合影组照", subject_route="group", **self.look
            )
        )
        self.assertEqual(value["status"], "pending")
        manifests = list(
            (self.root / "generated/images/suites").glob("*/manifest.json")
        )
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["participants"], ["10001"])
        self.assertEqual(
            manifest["reply_perspective"]["画面人物对应"][1]["对话身份"],
            "当前聊天对象（你）",
        )

    async def test_existing_video_frame_does_not_infer_unidentified_people(self):
        self.runtime._resolve_life_image_reference_async = AsyncMock(
            return_value="/existing-group.png"
        )
        component = self.runtime._part_runtime_video_media_component
        component._life_video_generate_background = AsyncMock()
        value = json.loads(
            await self.runtime.life_video_generate(
                self.event, "让这张合影动起来", subject_route="group"
            )
        )
        self.assertEqual(value["status"], "pending")
        request = component._life_video_generate_background.call_args.args[0]
        self.assertEqual(request.participants, ())
        self.assertEqual(request.initial_reference_image, "/existing-group.png")
