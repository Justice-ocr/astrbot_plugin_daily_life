import datetime
import types
import unittest
from unittest.mock import AsyncMock

from support import (
    ActionBot,
    ContactNameResolver,
    Event,
    PlatformHistoryManager,
    PlatformInstance,
    PlatformManager,
    make_composer,
)
from core.sources.dispatch import send_message_to_scope
from core.sources.platforms import get_onebot_client


class QQOfficialTest(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_official_ids_do_not_query_onebot_contacts(self):
        onebot = ActionBot({"get_stranger_info": {"nickname": "另一个平台的人"}})
        context = types.SimpleNamespace(platform_manager=PlatformManager(onebot))
        event = Event(
            sender_id="123456",
            sender_name="官方昵称",
            platform_name="qq_official",
            unified_msg_origin="qq_official:FriendMessage:123456",
        )
        resolver = ContactNameResolver(context, {})
        self.assertEqual(await resolver.resolve_event_sender(event), "官方昵称")
        self.assertIsNone(
            get_onebot_client(context, event.unified_msg_origin, event)
        )
        self.assertIsNone(get_onebot_client(context, event.unified_msg_origin))
        await resolver.close()
        self.assertEqual(onebot.calls, [])

    async def test_official_openid_is_not_used_as_display_name(self):
        resolver = ContactNameResolver(None, {})
        event = Event(
            sender_id="OFFICIAL_OPENID",
            sender_name="OFFICIAL_OPENID",
            platform_name="qq_official",
            unified_msg_origin="官方实例:FriendMessage:OFFICIAL_OPENID",
        )
        self.assertEqual(await resolver.resolve_event_sender(event), "用户")
        self.assertEqual(resolver.cached_event_sender(event), "用户")
        await resolver.close()

    async def test_full_session_alias_with_custom_instance_name(self):
        scope = "官方实例:FriendMessage:OFFICIAL_OPENID"
        resolver = ContactNameResolver(
            None, {"relationship_aliases": [f"{scope}:阿林"]}
        )
        event = Event(
            sender_id="OFFICIAL_OPENID",
            sender_name="",
            platform_name="qq_official",
            unified_msg_origin=scope,
        )
        self.assertEqual(await resolver.resolve_event_sender(event), "阿林")
        self.assertEqual(resolver.cached_event_sender(event), "阿林")
        self.assertEqual(resolver.get_relationship_alias(scope), "阿林")
        await resolver.close()

    async def test_official_reference_uses_saved_history_for_numeric_and_open_ids(self):
        for is_group in (False, True):
            for target_id in ("123456", "OFFICIAL_OPENID"):
                with self.subTest(group=is_group, target=target_id):
                    kind = "GroupMessage" if is_group else "FriendMessage"
                    scope = f"官方实例:{kind}:{target_id}"
                    onebot = ActionBot({"get_friend_msg_history": {"messages": []}})
                    composer, *_ = make_composer()
                    composer.context.platform_manager = PlatformManager(
                        instances=[
                            PlatformInstance(onebot),
                            PlatformInstance(object(), "官方实例", "qq_official"),
                        ]
                    )
                    record = types.SimpleNamespace(
                        content={"type": "user", "message": "明天一起去看展吧"},
                        sender_id=target_id,
                        sender_name="阿林",
                        created_at=datetime.datetime.now(),
                    )
                    composer.context.message_history_manager = PlatformHistoryManager(
                        {
                            ("官方实例", scope): [record],
                        }
                    )
                    composer.contact_resolver = ContactNameResolver(
                        composer.context, {}
                    )
                    result = await composer._get_recent_chats(
                        scope,
                        is_group=is_group,
                        hours=24,
                        max_count=10,
                    )
                    self.assertIn("阿林: 明天一起去看展吧", result)
                    self.assertEqual(onebot.calls, [])
                    await composer.contact_resolver.close()

    async def test_scoped_onebot_reference_uses_selected_account(self):
        first = ActionBot({})
        selected = ActionBot(
            {
                "get_friend_msg_history": {
                    "messages": [
                        {
                            "message_id": 1,
                            "time": int(datetime.datetime.now().timestamp()),
                            "sender": {"user_id": 123456},
                            "raw_message": "下午去书店吧",
                        }
                    ]
                },
                "get_stranger_info": {"remark": "正确朋友"},
            }
        )
        composer, *_ = make_composer()
        composer.context.platform_manager = PlatformManager(
            instances=[
                PlatformInstance(first, "第一个账号"),
                PlatformInstance(selected, "第二个账号"),
            ]
        )
        composer.contact_resolver = ContactNameResolver(composer.context, {})
        result = await composer._get_recent_chats(
            "第二个账号:FriendMessage:123456",
            is_group=False,
            hours=24,
            max_count=10,
        )
        self.assertIn("正确朋友: 下午去书店吧", result)
        self.assertEqual(first.calls, [])
        await composer.contact_resolver.close()

    async def test_background_delivery_preserves_official_scope_and_message_chain(self):
        for kind in ("FriendMessage", "GroupMessage"):
            with self.subTest(kind=kind):
                sender = AsyncMock(return_value=True)
                context = types.SimpleNamespace(send_message=sender)
                scope = f"官方实例:{kind}:OFFICIAL_OPENID"
                chain = types.SimpleNamespace(
                    items=[
                        {"type": "text", "text": "给你看看"},
                        {"type": "image", "file": "photo.png"},
                        {"type": "record", "file": "voice.wav"},
                        {"type": "video", "file": "video.mp4"},
                    ]
                )
                self.assertTrue(await send_message_to_scope(context, scope, chain))
                sender.assert_awaited_once_with(scope, chain)


if __name__ == "__main__":
    unittest.main()
