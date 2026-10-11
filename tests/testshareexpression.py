import json
import unittest
from unittest.mock import AsyncMock

from support import Context, DailyLifeRuntime, Event, LifeSettings, Provider


class ShareExpressionTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, **settings):
        runtime = DailyLifeRuntime.__new__(DailyLifeRuntime)
        runtime.config = LifeSettings.from_dict({"chat_style_config": settings})
        runtime.context = Context(Provider([]), config={"t2i_word_threshold": 150})
        runtime.get_text_provider = AsyncMock(return_value=None)
        return runtime

    async def test_scene_lengths_match_chat_and_current_config(self):
        runtime = self.runtime(
            casual_max_chars=40, private_casual_max_chars=12, group_casual_max_chars=25
        )
        runtime.get_text_provider.return_value = object()
        runtime.call_text_model = AsyncMock(
            return_value=json.dumps(
                {
                    "text": "去歇会儿。",
                    "facts_preserved": True,
                    "format_preserved": True,
                }
            )
        )
        for scene, kind, expected in (
            ("share_private", "FriendMessage", 12),
            ("share_group", "GroupMessage", 25),
        ):
            scope = f"bot:{kind}:123"
            self.assertEqual(
                runtime._chat_style_limit_for_event(Event(unified_msg_origin=scope)),
                expected,
            )
            self.assertIn(
                f"约 {expected} 字", runtime.get_share_chat_style(scene=scene)["prompt"]
            )
            result = await runtime.prepare_share_expression(
                "准备去歇会儿。", scene=scene, requirements={"target_umo": scope}
            )
            self.assertEqual(result["text"], "去歇会儿")
            self.assertIn(
                f"约 {expected} 字", runtime.call_text_model.await_args.args[1]
            )
        runtime.config.chat_style.private_casual_max_chars = 20
        self.assertIn(
            "约 20 字", runtime.get_share_chat_style(scene="share_private")["prompt"]
        )

    async def test_cleanup_runs_without_model_and_uses_current_character_set(self):
        runtime = self.runtime()
        text = "早呀，喝点水。今天不赶时间！"
        result = await runtime.prepare_share_expression(text, scene="share_private")
        self.assertEqual(result["text"], "早呀 喝点水 今天不赶时间")
        runtime.get_text_provider.return_value = object()
        runtime.call_text_model = AsyncMock(side_effect=TimeoutError())
        runtime.config.chat_style.punctuation_cleanup_chars = "！"
        self.assertEqual(
            (await runtime.prepare_share_expression(text, scene="share_group"))["text"],
            "早呀，喝点水。今天不赶时间",
        )
        runtime.config.chat_style.punctuation_cleanup_enabled = False
        self.assertEqual(
            (await runtime.prepare_share_expression(text, scene="share_private"))[
                "text"
            ],
            text,
        )
        runtime.config.chat_style.punctuation_cleanup_enabled = True
        runtime.config.chat_style.enabled = False
        runtime.get_text_provider.reset_mock()
        self.assertEqual(
            (await runtime.prepare_share_expression(text, scene="share_private"))[
                "text"
            ],
            text,
        )
        runtime.get_text_provider.assert_not_awaited()

    async def test_cleanup_preserves_titles_links_numbers_newlines_and_code(self):
        runtime = self.runtime(punctuation_cleanup_chars="，。！？；、,.!?;【】")
        text = "【你好，世界】\n\n  更新到 v1.2，售价 19.9 元。\n看看 https://example.com/a?q=1，代码 `a,b`。"
        result = await runtime.prepare_share_expression(
            text,
            scene="share_private",
            requirements={
                "required_terms": ["【你好，世界】"],
                "output_format": "保留段落",
            },
        )
        self.assertEqual(
            result["text"],
            "【你好，世界】\n\n  更新到 v1.2 售价 19.9 元\n看看 https://example.com/a?q=1 代码 `a,b`",
        )

    async def test_share_cleanup_ignores_chat_threshold_and_preserves_structure(self):
        runtime = self.runtime()
        scopes = []

        def config(scope=None):
            scopes.append(scope)
            return {"t2i_word_threshold": 50 if scope == "bot:GroupMessage:1" else 150}

        runtime.context.get_config = config
        text = "这是含有必要背景的分享内容。" * 6
        for scope in ("bot:GroupMessage:1", "bot:FriendMessage:2"):
            result = await runtime.prepare_share_expression(
                text,
                scene="share_group" if "Group" in scope else "share_private",
                requirements={"target_umo": scope},
            )
            self.assertEqual(
                result["text"],
                runtime._semantic_segment_clean_punctuation(
                    text, runtime.config.chat_style.punctuation_cleanup_chars
                ),
            )
        self.assertEqual(scopes, [])
        for text in (
            "- 第一项，重要。\n- 第二项，保留。",
            "```py\nprint('a,b')\n```",
            "。！？",
        ):
            self.assertEqual(
                (await runtime.prepare_share_expression(text, scene="share_private"))[
                    "text"
                ],
                text,
            )

    async def test_necessary_long_text_is_not_cut_and_public_posts_are_cleaned(self):
        runtime = self.runtime(private_casual_max_chars=10)
        text = "【蜂蜜】蜂蜜虽然能保存较久，但需要密封防潮，而且并非所有人都适合食用。"
        for scene in ("share_private", "qzone_post", "share_voice"):
            result = await runtime.prepare_share_expression(text, scene=scene)
            expected = (
                runtime._semantic_segment_clean_punctuation(
                    text, runtime.config.chat_style.punctuation_cleanup_chars
                )
                if scene in {"share_private", "qzone_post"}
                else text
            )
            self.assertEqual(result["text"], expected)
            self.assertGreater(len(result["text"]), 10)

    async def test_public_comment_and_reply_cleanup_preserves_mention(self):
        runtime = self.runtime()
        mention = "@{uin:123,nick:小明,auto:1}"
        text = f"{mention}，好看！"
        for scene in ("qzone_comment", "qzone_reply"):
            self.assertEqual(
                runtime.clean_share_punctuation(text, scene=scene),
                f"{mention} 好看",
            )
        self.assertEqual(
            runtime.clean_share_punctuation(text, scene="share_voice"), text
        )
        runtime.config.chat_style.punctuation_cleanup_enabled = False
        self.assertEqual(
            runtime.clean_share_punctuation(text, scene="qzone_reply"), text
        )
