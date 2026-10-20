import datetime
import json
import unittest
from unittest.mock import AsyncMock, patch

from support import make_composer
from core.life.itinerary import EDIT_ACTION_RULES, apply_edited_timeline, compile_edited_actions
from core.models import CommitmentRecord, DayRecord, TimelineItem
from core.prompts import LIFE_ACTION_SCOPE_RULES


class ItineraryActionsTest(unittest.IsolatedAsyncioTestCase):
    async def test_compound_edit_preserves_order_and_individual_durations(self):
        composer, _, _, _ = make_composer([])
        timeline = [TimelineItem(time="18:00", activity="做饭后吃晚饭")]
        result = {
            "timeline_edits": [{"operation": "insert", "item": timeline[0].as_dict()}],
            "planned_actions": [
                {"edit_index": 0, "action_type": kind, "target": target, "duration_minutes": duration}
                for kind, target, duration in (("cook", "做饭", 20), ("meal", "吃饭", 15))
            ],
        }
        self.assertEqual(await compile_edited_actions(
            composer, result, timeline, date_str="2026-10-20", source="chat"
        ), "")
        steps = result["_planned_life_actions"]
        self.assertEqual([step["action_type"] for step in steps], ["cook", "meal"])
        self.assertEqual([step["timeline_index"] for step in steps], [0, 0])
        self.assertEqual([step["duration_minutes"] for step in steps], [20, 15])
        self.assertEqual(timeline[0].duration_minutes, 35)

    async def test_edit_keeps_timed_followup_outside_current_action_target(self):
        composer, _, _, archive = make_composer([])
        day = DayRecord(date="2026-10-14", timeline=[TimelineItem(time="15:40", activity="坐下喝水")])
        await archive.save_day(day)
        next_step = "16:00 再去散步"
        result = {
            "timeline_edits": [{"operation": "insert", "item": day.timeline[0].as_dict()}],
            "planned_actions": [{
                "edit_index": 0, "action_type": "drink", "target": "坐下喝水",
                "duration_minutes": 3, "payload": {"volume_ml": 250, "next_step": next_step},
            }],
        }
        self.assertIn(LIFE_ACTION_SCOPE_RULES, EDIT_ACTION_RULES)
        issue = await compile_edited_actions(composer, result, day.timeline, date_str=day.date, source="invite")
        self.assertEqual(issue, "")
        apply_edited_timeline(day, day.timeline, result)
        await archive.save_day(day)
        saved = await archive.get_day(day.date)
        action = json.loads(saved.meta["planned_life_actions"])[0]
        self.assertEqual(action["target"], "坐下喝水")
        self.assertEqual(action["payload"]["next_step"], next_step)
        self.assertEqual(saved.timeline[0].time, "15:40")
        self.assertEqual(saved.timeline[0].execution_state, "planned")
        self.assertEqual(await archive.get_life_action_outcomes(), [])

    def draft(self):
        return {
            "should_apply": True,
            "reason": "一起外出，需要换掉家居服",
            "timeline_edits": [
                {
                    "operation": "insert",
                    "item": {
                        "time": "15:40",
                        "activity": "换好出门的裙子",
                        "place_kind": "home",
                    },
                }
            ],
            "planned_actions": [
                {
                    "edit_index": 0,
                    "action_type": "change_outfit",
                    "target": "待校验的裙子",
                    "duration_minutes": 3,
                    "payload": {
                        "scene_category": "outdoor",
                        "catalog_reference_ids": [7],
                    },
                }
            ],
        }

    async def test_commitment_edit_produces_executable_catalog_outfit_without_changing_current(
        self,
    ):
        composer, _, _, archive = make_composer(
            [json.dumps(self.draft(), ensure_ascii=False)]
        )
        day = DayRecord(
            date="2026-10-14",
            outfit="家居服",
            timeline=[TimelineItem(time="14:00", activity="在家休息")],
        )
        await archive.save_day(day)
        with patch.object(
            composer,
            "_style_catalog_new_outfit_selection",
            AsyncMock(return_value=({"outfit": "衣橱中的碎花连衣裙"}, "")),
        ) as catalog:
            timeline, decision = await composer.reconcile_commitment_with_timeline(
                day.date,
                day.timeline,
                CommitmentRecord(content="一起出门", owner="共同"),
                datetime.datetime(2026, 10, 14, 15, 30),
            )
        self.assertIsNotNone(timeline)
        catalog.assert_awaited_once_with([7], scene_category="outdoor")
        apply_edited_timeline(day, timeline, decision)
        await archive.save_day(day)
        saved = await archive.get_day(day.date)
        action = json.loads(saved.meta["planned_life_actions"])[0]
        self.assertEqual(action["timeline_index"], 1)
        self.assertEqual(action["target"], "衣橱中的碎花连衣裙")
        self.assertEqual(saved.timeline[1].duration_minutes, 3)
        self.assertEqual(saved.timeline[1].execution_state, "planned")
        self.assertEqual(saved.outfit, "家居服")
        self.assertEqual(await archive.get_life_action_outcomes(), [])

    async def test_action_contract_rejects_missing_or_unbound_or_unowned_outfit(self):
        composer, _, _, _ = make_composer([])
        timeline = [TimelineItem(time="15:40", activity="换好出门的裙子")]
        for defect in ("missing", "index", "duration", "catalog"):
            with self.subTest(defect=defect):
                result = self.draft()
                if defect == "missing":
                    result.pop("planned_actions")
                elif defect == "index":
                    result["planned_actions"][0]["edit_index"] = 9
                elif defect == "duration":
                    result["planned_actions"][0]["duration_minutes"] = 0
                with patch.object(
                    composer,
                    "_style_catalog_new_outfit_selection",
                    AsyncMock(return_value=({}, "所选衣物当前不可用")),
                ):
                    issue = await compile_edited_actions(
                        composer,
                        result,
                        timeline,
                        date_str="2026-10-14",
                        source="invite",
                    )
                self.assertTrue(issue)
                self.assertNotIn("_planned_life_actions", result)

    async def test_elapsed_plan_cannot_become_completed_outfit_even_without_user_override(
        self,
    ):
        composer, _, _, archive = make_composer(
            [
                json.dumps(
                    {
                        "outfit_decision": "change",
                        "current_outfit_basis": "occurred_schedule",
                        "scene_category": "outdoor",
                        "style_pool": "outfit_styles",
                        "outfit": "浅蓝色短袖上衣配白色短裤",
                        "style": "清爽外出风",
                        "change_evidence": {
                            "kind": "explicit_outfit_change",
                            "source": "occurred_schedule",
                            "timeline_time": "15:40",
                            "quote": "换上出门裙子",
                        },
                    },
                    ensure_ascii=False,
                )
            ]
        )
        await archive.save_day(
            DayRecord(
                date="2026-10-14",
                outfit="居家服",
                meta={"outfit_fact_source": "daily_generation"},
                timeline=[
                    TimelineItem(
                        time="15:40", activity="换上出门裙子", execution_state="elapsed"
                    )
                ],
            )
        )
        await composer.update_outfit(
            "2026-10-14",
            "afternoon",
            current_time=datetime.datetime(2026, 10, 14, 15, 50),
        )
        self.assertEqual((await archive.get_day("2026-10-14")).outfit, "居家服")

    def test_edits_preserve_unrelated_action_identity_and_remove_replaced_actions(self):
        day = DayRecord(
            date="2026-10-14",
            timeline=[
                TimelineItem(time="15:40", activity="原准备"),
                TimelineItem(time="18:00", activity="晚饭"),
            ],
            meta={
                "planned_life_actions": json.dumps(
                    [
                        {"action_id": "old", "timeline_index": 0},
                        {"action_id": "dinner", "timeline_index": 1},
                        {"action_id": "spontaneous", "timeline_index": None},
                    ]
                )
            },
        )
        timeline = [
            TimelineItem(time="15:30", activity="新准备"),
            TimelineItem(time="16:00", activity="出发"),
            day.timeline[1],
        ]
        apply_edited_timeline(
            day,
            timeline,
            {
                "_planned_life_actions": [
                    {"action_id": "new", "timeline_index": 0},
                ]
            },
        )
        actions = {
            item["action_id"]: item
            for item in json.loads(day.meta["planned_life_actions"])
        }
        self.assertEqual(set(actions), {"dinner", "spontaneous", "new"})
        self.assertEqual(actions["dinner"]["timeline_index"], 2)

    async def test_location_audit_time_shift_keeps_action_bound_to_its_activity(self):
        composer, _, _, _ = make_composer([])
        result = self.draft()
        result["planned_actions"][0]["action_type"] = "chore"
        timeline = [TimelineItem(time="15:50", activity="换好出门的裙子")]
        self.assertEqual(
            await compile_edited_actions(
                composer, result, timeline, date_str="2026-10-14", source="invite"
            ),
            "",
        )
        self.assertEqual(result["_planned_life_actions"][0]["timeline_index"], 0)
