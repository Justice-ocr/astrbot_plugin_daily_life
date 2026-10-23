"""独立模型选择从配置解析到实际任务调用的回归验证。"""

import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import PLUGIN_ROOT, LifeArchive, LifeSettings

# isort: split

from core.life.climate import DailyClimateComponent
from core.models import CommitmentRecord, DayRecord, EmojiAssetRecord
from core.runtime.channel.stylist import RuntimeStyleCatalogComponent
from core.runtime.channel.summary import RuntimeMediaCommonComponent
from core.runtime.deliberation import ContinuousDecisionService
from core.runtime.meaning import MeaningService
from core.runtime.proactive.gesture import ProactiveGestureComponent
from core.runtime.proactive.rendezvous import SharedActivityContactComponent
from core.runtime.stage.lens import StageLensComponent
from core.runtime.stage.speech import StageVoiceComponent

ROUTES = {
    "video": ("video_generation_config", "prompt_provider", "video_generation"),
    "emoji": ("emoji_config", "selection_provider", "emoji"),
    "execution": ("life_domain_config", "action_provider", "domains"),
    "contact": ("proactive_config", "contact_provider", "proactive"),
    "rank": ("memory_config", "ranking_provider", "memory"),
    "failure": ("chat_style_config", "task_reply_provider", "chat_style"),
    "weather": ("state_config", "provider", "state"),
    "feedback": ("outfit_config", "provider", "outfit"),
}


class ModelRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = LifeArchive(Path(self.directory.name) / "life.db")
        self.addAsyncCleanup(self.archive.aclose)
        self.now = datetime.datetime(2026, 10, 23, 14, 0)
        self.day = DayRecord(date="2026-10-23", outfit="居家服")

    def test_model_selectors_parse_saved_values_and_default_to_current_model(self):
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text())
        saved = {}
        for name, (section, field, _) in ROUTES.items():
            spec = schema[section]["items"][field]
            self.assertEqual(spec["_special"], "select_provider")
            self.assertEqual(spec["default"], "")
            saved.setdefault(section, {})[field] = f"  {name}-model  "
        configured = LifeSettings.from_dict(saved)
        default = LifeSettings.from_dict({})
        for name, (_, field, attr) in ROUTES.items():
            with self.subTest(task=name):
                self.assertEqual(
                    getattr(getattr(configured, attr), field), f"{name}-model"
                )
                self.assertEqual(getattr(getattr(default, attr), field), "")

    async def run_routes(self, selections):
        saved = {
            "rhythm_config": {"llm_provider": "schedule-model"},
            "proactive_config": {"provider": "idle-model"},
            "memory_config": {"provider": "extraction-model"},
        }
        for name, (section, field, _) in ROUTES.items():
            saved.setdefault(section, {})[field] = selections[name]
        provider = object()
        replies = {
            "media": {"image": "窗边自然光里的生活片段"},
            "emoji_pick": {"emoji_id": 1},
            "execution": {"decision": "wait"},
            "activity_contact": {"decision": "skip", "reason": "已经商量好了"},
            "memory_rank": {"selected": {"memory": [1]}},
            "background_failure": {"reply_text": "这次没拍成，有点可惜。"},
            "weather": dict.fromkeys(
                ("is_rainy", "is_sunny", "is_cloudy", "is_foggy", "is_severe"), False
            ),
            "style_feedback": {"adjustments": [], "preference_points": []},
        }

        async def call_model(selected, prompt, session_id, **kwargs):
            self.assertIs(selected, provider)
            for prefix, reply in replies.items():
                if session_id.startswith(f"daily_life_{prefix}_"):
                    return json.dumps(reply, ensure_ascii=False)
            self.fail(f"意外的模型调用：{session_id}")

        ctx = SimpleNamespace(
            config=LifeSettings.from_dict(saved),
            archive=self.archive,
            get_text_provider=AsyncMock(return_value=provider),
            call_text_model=AsyncMock(side_effect=call_model),
            close_text_session=AsyncMock(),
            get_persona_text=AsyncMock(return_value="角色"),
            _media_director_current_day=AsyncMock(
                return_value=(self.day, self.now, False)
            ),
            _media_director_context_text=lambda *args: "正在家里休息",
            _media_director_emotion_context_text=AsyncMock(return_value=""),
            _media_director_recent_context_text=AsyncMock(
                return_value="拍一段窗边视频"
            ),
            _current_proactive_persona=AsyncMock(return_value="角色"),
            _format_recent_context_messages=lambda *args, **kwargs: "已经商量好啦",
            _proactive_voice_capability=lambda scope: "本次文字",
            _emoji_send_candidate_limit=lambda: 12,
            _emoji_asset_is_sendable=lambda asset: True,
            _emoji_recency_penalty=lambda *args: 0,
            _style_text=RuntimeStyleCatalogComponent._style_text,
            _parse_delivered_media_reply=RuntimeMediaCommonComponent._parse_delivered_media_reply,
            _media_error_summary=str,
            _task_provider_id=lambda provider_id: provider_id,
        )
        ctx._get_provider = ctx.get_text_provider
        ctx._call_llm_text = ctx.call_text_model
        ctx._cleanup_conversation = ctx.close_text_session
        lens = StageLensComponent(ctx)
        ctx._media_director_call = lens._media_director_call
        ctx._media_director_text_call = lens._media_director_text_call
        emoji = ProactiveGestureComponent(ctx)
        asset = await self.archive.upsert_emoji_asset(
            EmojiAssetRecord(
                file_hash="smile",
                file_path="https://example.com/smile.png",
                label="微笑",
                status="ready",
            )
        )
        self.assertIsNotNone(asset)
        emoji._semantic_emoji_candidates = AsyncMock(return_value=[asset])
        emoji._recent_sent_emoji_ids = AsyncMock(return_value=set())
        decision = ContinuousDecisionService(
            archive=lambda: self.archive,
            config=lambda: ctx.config,
            persona=AsyncMock(return_value="角色"),
            recent_chats=AsyncMock(return_value=""),
            scope_messages=AsyncMock(return_value=[]),
            text_provider=ctx.get_text_provider,
            call_text_model=ctx.call_text_model,
            close_text_session=ctx.close_text_session,
            wardrobe_context=AsyncMock(return_value=""),
        )
        meaning = MeaningService(
            embedding_providers=lambda: [],
            memory_config=lambda: ctx.config.memory,
            archive=lambda: self.archive,
            text_provider=ctx.get_text_provider,
            call_text_model=ctx.call_text_model,
            close_text_session=ctx.close_text_session,
            schedule_background=lambda *args, **kwargs: False,
        )
        results = await asyncio.gather(
            StageVoiceComponent(ctx)._direct_life_video_prompt(None, "拍一段窗边视频"),
            emoji._select_emoji_asset_for_intent({"emoji_intent": "轻松"}),
            decision.choose(self.day, {}, self.now),
            SharedActivityContactComponent(ctx)._evaluate_shared_activity_contact(
                commitment=CommitmentRecord(source_session="test:FriendMessage:1"),
                task=SimpleNamespace(payload={}, result={}),
                day=self.day,
                candidates=[],
                recent=[],
                relationship=None,
                interaction=None,
                now=self.now,
            ),
            meaning._meaning_rank_with_model(
                "电影",
                {"memory": [("1", "读书", "第一项"), ("2", "电影", "第二项")]},
                {"memory": 1},
            ),
            RuntimeMediaCommonComponent(ctx)._generate_background_failure_reply(
                "test:FriendMessage:1",
                task_name="拍照",
                request_text="拍张照",
                failure_reason="生成失败",
            ),
            DailyClimateComponent(ctx)._classify_weather_condition(
                {"ok": True, "data": {"weather": {"condition": "多云"}}}
            ),
            RuntimeStyleCatalogComponent(ctx)._style_feedback_decision(
                [], "喜欢柔软的衣服"
            ),
        )
        self.assertIn("窗边", results[0])
        self.assertEqual(results[1].id, 1)
        self.assertEqual(results[2]["decision"], "wait")
        self.assertEqual(results[3]["decision"], "skip")
        self.assertEqual(results[4], {"memory": ["第二项"]})
        self.assertEqual(results[5], "这次没拍成，有点可惜。")
        self.assertIs(results[6]["data"]["weather"]["is_severe"], False)
        self.assertEqual(results[7]["adjustments"], [])
        prefixes = dict(zip(ROUTES, replies))
        self.assertEqual(ctx.call_text_model.await_count, len(ROUTES))
        self.assertEqual(ctx.get_text_provider.await_count, len(ROUTES))
        calls = ctx.call_text_model.await_args_list
        self.assertEqual(len({call.args[2] for call in calls}), len(ROUTES))
        for name, prefix in prefixes.items():
            matches = [
                call
                for call in calls
                if call.args[2].startswith(f"daily_life_{prefix}_")
            ]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].kwargs["primary_provider_id"], selections[name])
        self.assertCountEqual(
            [call.args[0] for call in ctx.get_text_provider.await_args_list],
            list(selections.values()),
        )

    async def test_each_task_calls_its_selected_model(self):
        await self.run_routes({name: f"{name}-model" for name in ROUTES})

    async def test_blank_selections_use_current_default_for_each_task(self):
        await self.run_routes(dict.fromkeys(ROUTES, ""))

    async def test_same_model_still_has_separate_calls_and_sessions(self):
        await self.run_routes(dict.fromkeys(ROUTES, "shared-model"))
