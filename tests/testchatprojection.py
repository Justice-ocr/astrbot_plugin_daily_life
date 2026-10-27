"""聊天投影保留事实边界，后台数值和完整记录不随压缩而改变。"""

import copy
import datetime
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from runtimehelpers import DailyLifeRuntime, DayRecord, LifeSettings, LifeState, TimelineItem
from support import LifeArchive, ProviderRequest
from core.contextbudget import budget_chat_context, token_estimate
from core.life.chatview import chat_life_projection, read_execution
from core.models import CommitmentRecord
from core.runtime.meaning import MeaningService


def runtime():
    instance = DailyLifeRuntime.__new__(DailyLifeRuntime)
    instance.config = LifeSettings.from_dict({})
    return instance


class ChatProjectionTest(unittest.TestCase):
    def test_world_projection_uses_observed_changes_and_leaves_world_untouched(self):
        world = {
            "body": {"energy": 76, "sleepiness": 56},
            "kernel": {"environment": {"place": "客厅"}, "reflection": {"summary": "审计复盘"}},
            "history": [
                {"status": "completed", "action": {"target": "整理书架"}, "finished_at": "2026-10-26 19:00"},
                {"status": "failed", "action": {"target": "烧水"}, "finished_at": "2026-10-26 19:10"},
                {"status": "running", "action": {"target": "待确认动作"}},
            ],
        }
        before = copy.deepcopy(world)
        text = chat_life_projection(world)
        self.assertIn("客厅", text)
        self.assertIn("2026-10-26 19:10 烧水（未完成）", text)
        self.assertNotIn("待确认动作", text)
        self.assertNotIn("审计复盘", text)
        self.assertNotIn("76", text)
        self.assertEqual(world, before)

    def test_numeric_state_alone_does_not_invent_a_feeling(self):
        rt = runtime()
        day = DayRecord(date="2026-10-26", state=LifeState(energy=10, sleepiness=99))
        text = rt.build_hidden_life_context(day, datetime.datetime(2026, 10, 26, 20), False, include_rules=False)
        self.assertNotIn("[HiddenState]", text)
        self.assertNotIn("99", text)
        self.assertNotIn("该睡", text)
        self.assertEqual(day.state.sleepiness, 99)

    def test_feelings_keep_the_whole_sentence_including_negation(self):
        rt = runtime()
        summary = "刚放下手里的书，" + "还想再聊一会儿，" * 24 + "但还没有睡。"
        day = DayRecord(date="2026-10-26", state=LifeState(summary=summary))
        text = rt.build_hidden_life_context(day, datetime.datetime(2026, 10, 26, 20), False, include_rules=False)
        self.assertIn(summary, text)

    def test_old_report_is_not_used_as_chat_fallback(self):
        day = DayRecord(date="2026-10-26", meta={"continuous_life_context": "事后判断：未执行的细节"})
        text = runtime().build_hidden_life_context(day, datetime.datetime(2026, 10, 26, 20), False, include_rules=False)
        self.assertNotIn("未执行的细节", text)
        self.assertEqual(day.meta["continuous_life_context"], "事后判断：未执行的细节")

    def test_pending_observation_is_not_rendered_as_still_running(self):
        day = DayRecord(date="2026-10-26", meta={"continuous_execution": json.dumps({
            "status": "running", "observation_state": "pending", "action": {"target": "在门口等人"},
        })})
        text = runtime().build_hidden_life_context(day, datetime.datetime(2026, 10, 26, 20), False, include_rules=False)
        self.assertIn("在门口等人（最近确认，当前进展待确认）", text)
        self.assertNotIn("在门口等人（进行中）", text)

    def test_invalid_execution_record_does_not_claim_activity(self):
        for raw in ("invalid", "[]", "null"):
            self.assertEqual(read_execution(DayRecord(date="2026-10-26", meta={"continuous_execution": raw})), {})

    def test_recalled_details_are_available_but_not_automatically_injected(self):
        rt = runtime()
        day = DayRecord(date="2026-10-26", weather="室外有雨", memo="下午整理相册")
        args = (day, datetime.datetime(2026, 10, 26, 20), False)
        plain = rt.build_hidden_life_context(*args, include_rules=False)
        selected = rt.build_hidden_life_context(*args, include_rules=False, detail_context="室外有雨")
        self.assertNotIn("室外有雨", plain)
        self.assertIn("室外有雨", selected)
        self.assertNotIn("下午整理相册", selected)

    def test_commitment_keeps_owner_time_and_unfinished_status(self):
        day = DayRecord(date="2026-10-26")
        items = [CommitmentRecord(content="一起去散步", owner="共同", trigger_date=day.date, trigger_time="21:00"),
                 CommitmentRecord(content="已交付照片", status="done")]
        text = runtime().build_hidden_life_context(day, datetime.datetime(2026, 10, 26, 20), False,
                                                 commitments=items, include_rules=False)
        self.assertIn("一起去散步（共同；2026-10-26 21:00；尚未完成）", text)
        self.assertNotIn("已交付照片", text)


class ChatBudgetTest(unittest.TestCase):
    def test_unknown_sections_cannot_bypass_the_hard_budget(self):
        facts = "[HiddenTime] 2026-10-26 20:00\n[HiddenCommitmentHint]\n- 21:00 一起散步，尚未完成\n"
        reports = "".join(f"[CustomReport{i}]\n" + "后台报告" * 300 + "\n" for i in range(160))
        text = budget_chat_context(reports + facts)
        self.assertIn("21:00 一起散步，尚未完成", text)
        self.assertIn("2026-10-26 20:00", text)
        self.assertLessEqual(len(text), 6000)
        self.assertLessEqual(token_estimate(text), 5000)
        self.assertEqual(text.count("<daily_life>"), 1)
        self.assertTrue(text.endswith("</daily_life>"))

    def test_full_records_never_lose_their_final_negation(self):
        long_fact = "- " + "尚待查证的记录" * 170 + "，并没有完成。"
        short_fact = "- 等照片发送成功才算完成。"
        text = budget_chat_context(f"[HiddenCommitmentHint]\n{long_fact}\n{short_fact}")
        self.assertNotIn("尚待查证", text)
        self.assertIn(short_fact, text)

    def test_repeated_sections_do_not_duplicate_evidence(self):
        record = "[HiddenState]\n- 此刻感受：想再聊一会儿。\n"
        text = budget_chat_context(record * 8)
        self.assertEqual(text.count("想再聊一会儿"), 1)

    def test_different_records_under_the_same_tag_keep_their_headers(self):
        text = budget_chat_context("[HiddenEvents]\n- 昨天看了电影。\n[HiddenEvents]\n- 今天计划散步。")
        self.assertIn("[HiddenEvents]\n- 昨天看了电影。", text)
        self.assertIn("[HiddenEvents]\n- 今天计划散步。", text)

    def test_tiny_budget_is_empty_and_normal_budget_preserves_boundary(self):
        self.assertEqual(budget_chat_context("事实", chars=1), "")
        text = budget_chat_context("[HiddenInteractionContext]\n双方通过消息交流，尚无共同在场证据。\n" +
                                   "[HiddenLifeEpisodes]\n" + "旧事" * 2000, chars=350, tokens=250)
        self.assertIn("尚无共同在场证据", text)
        self.assertLessEqual(len(text), 350)
        self.assertLessEqual(token_estimate(text), 250)

    def test_final_injection_keeps_persona_history_tools_and_is_idempotent(self):
        rt = runtime()
        history = [{"role": "user", "content": "昨天的对话"}]
        req = ProviderRequest(system_prompt="原本人设", prompt="现在聊聊")
        req.contexts = copy.deepcopy(history)
        req.func_tool = object()
        tools = req.func_tool
        text = "[HiddenState]\n- 此刻感受：想坐会儿。\n" + "[HugeReport]\n" + "报表" * 80000
        rt._apply_life_request_context(req, text)
        rt._apply_life_request_context(req, text)
        self.assertEqual(req.contexts, history)
        self.assertEqual(req.prompt, "现在聊聊")
        self.assertIs(req.func_tool, tools)
        self.assertTrue(req.system_prompt.startswith("原本人设"))
        self.assertEqual(req.system_prompt.count("<daily_life_policy>"), 1)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(req.extra_user_content_parts[0].model_dump_for_context()["_no_save"])
        self.assertLessEqual(req._daily_life_context_sizes["life_chars"], 6000)
        self.assertGreater(req._daily_life_context_sizes["life_before_chars"], 150000)


class ChatSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_optional_groups_do_not_fall_back_to_unrelated_records(self):
        meaning = MeaningService.__new__(MeaningService)
        meaning._closed = False
        meaning._memory_config = lambda: SimpleNamespace(semantic_ranking_model_enabled=False)
        meaning._meaning_rank_with_embeddings = AsyncMock(return_value=None)
        groups = {"chat_details": [("1", "晴天", "天气")], "relationships": [("2", "朋友", "朋友") ]}
        result = await meaning.rank_semantic_groups("今天电影怎么样", groups,
                                                    {"chat_details": 2, "relationships": 2}, optional_groups={"chat_details"})
        self.assertEqual(result, {"chat_details": [], "relationships": ["朋友"]})

    def test_optional_embedding_results_need_semantic_relevance(self):
        groups = {"chat_details": [("1", "有关", "有关"), ("2", "无关", "无关")],
                  "relationships": [("3", "朋友", "朋友")]}
        vectors = {("chat_details", "1"): [1, 0], ("chat_details", "2"): [0, 1], ("relationships", "3"): [0, 1]}
        result = MeaningService._score_vectors([1, 0], groups, vectors,
                                              {"chat_details": 2, "relationships": 2}, {"chat_details": 0.62})
        self.assertEqual(result, {"chat_details": ["有关"], "relationships": ["朋友"]})

    async def test_selection_uses_user_message_once_and_preserves_next_day_date(self):
        rt = runtime()
        captured = []

        async def choose(query, groups, limits, **kwargs):
            captured.append((query, groups, kwargs))
            return {key: [values[-1][2]] if key == "chat_details" else [] for key, values in groups.items()}

        rt.rank_semantic_groups = choose
        day = DayRecord(date="2026-10-26", memo="自己的困意", meta={"theme": "状态表"},
                        timeline=[TimelineItem(time="01:30", day_offset=1, activity="计划散步")])
        snapshot = {}
        await rt._part_layer_chain_component._select_life_memory_contexts(snapshot, day, "明天一起做什么？")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0], "明天一起做什么？")
        self.assertEqual(captured[0][2]["optional_groups"], {"chat_details"})
        self.assertIn("2026-10-27 01:30 计划散步（计划，完成尚未确认）", snapshot["selected_chat_details"])
        self.assertNotIn("自己的困意", snapshot["selected_chat_details"])

    async def test_timeout_drops_optional_reports(self):
        rt = runtime()
        rt.rank_semantic_groups = AsyncMock(side_effect=TimeoutError)
        snapshot = {}
        await rt._part_layer_chain_component._select_life_memory_contexts(
            snapshot, DayRecord(date="2026-10-26", weather="本轮无关天气"), "电影如何")
        self.assertEqual(snapshot["selected_chat_details"], "")

    async def test_commitment_scope_is_applied_before_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = LifeArchive(f"{directory}/life.db")
            try:
                for scope, content in (("other", "他人的约定"), ("current", "本轮约定"), ("", "自己的安排")):
                    await archive.save_commitment(CommitmentRecord(content=content, source_session=scope,
                                                                   trigger_date="2099-10-26"))
                selected = await archive.get_commitments(source_session="current", limit=2)
                self.assertEqual({item.content for item in selected}, {"本轮约定", "自己的安排"})
                snapshot = await archive.get_context_snapshot(max_summaries=2, session_id="current", experience_scope="current")
                self.assertNotIn("他人的约定", {item.content for item in snapshot["commitments"]})
            finally:
                await archive.aclose()
