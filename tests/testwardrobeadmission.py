import datetime
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401 -- 先安装共用的 AstrBot 测试桩

# isort: split

from core.life.contract import DailyContractComponent
from core.paths import STYLE_CATALOG_DIR_NAME
from support import LifeArchive
from testdashboard import PagePlugin


class WardrobeAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = LifeArchive(Path(self.tmp.name) / "life.db")
        self.addCleanup(self.archive.close)

    async def item(self, **overrides):
        return await self.archive.upsert_style_catalog_item(
            {
                "kind": "outfit",
                "source_image_hash": "a" * 64,
                "description": "米色针织上衣和长裤",
                "confidence": 0.95,
                **overrides,
            }
        )

    async def test_imported_unworn_clothes_are_immediately_owned(self):
        item = await self.item(source_kind="manual", source_scope="dashboard")
        self.assertEqual(item.attributes["wardrobe"]["ownership"], "owned")
        self.assertTrue(item.attributes["wardrobe"]["available"])
        self.assertFalse(item.attributes["wardrobe"]["currently_worn"])
        self.assertEqual(item.last_used_at, "")
        self.assertEqual((await self.archive.get_wardrobe_snapshot())["events"], [])

    async def test_legacy_candidate_is_repaired_without_resetting_care(self):
        item = await self.item()
        await self.archive._run_db(
            lambda: (
                self.archive._conn.execute(
                    "UPDATE wardrobe_units SET ownership='candidate',condition='dirty',"
                    "acquired_at='',revision=7,payload_json=?",
                    (json.dumps({"wear_count": 3, "wearing": False}),),
                ).rowcount
            )
        )
        first = (await self.archive.get_wardrobe_snapshot())["items"][str(item.id)]
        second = (await self.archive.get_wardrobe_snapshot())["items"][str(item.id)]
        self.assertEqual(first, second)
        self.assertEqual(first["ownership"], "owned")
        self.assertEqual(first["condition"], "dirty")
        self.assertFalse(first["available"])
        self.assertEqual(first["units"][0]["payload"]["wear_count"], 3)
        self.assertEqual(first["units"][0]["acquired_at"], item.created_at)
        self.assertEqual(
            (await self.archive.get_style_catalog_items())[0].last_used_at, ""
        )

    async def test_unverified_generation_stays_unavailable_after_reanalysis(self):
        item = await self.item(attributes={"wardrobe_job": "unverified"})
        self.assertEqual(item.attributes["wardrobe"]["ownership"], "candidate")
        await self.item(attributes={"wardrobe_job": "unverified", "colors": ["米色"]})
        state = (await self.archive.get_wardrobe_snapshot())["items"][str(item.id)]
        self.assertEqual(state["ownership"], "candidate")
        self.assertFalse(state["available"])

    async def test_legacy_restore_recovers_piece_facts_before_suit_summary(self):
        top = await self.item(
            kind="top",
            attributes={"wardrobe": {"ownership": "owned", "condition": "clean"}},
        )
        bottom = await self.item(
            kind="bottom",
            attributes={"wardrobe": {"ownership": "owned", "condition": "dirty"}},
        )
        await self.item(
            attributes={"wardrobe": {"ownership": "owned", "condition": "dirty"}}
        )
        await self.archive._run_db(
            lambda: (
                self.archive._conn.execute(
                    "UPDATE wardrobe_units SET ownership='candidate',condition='clean',"
                    "acquired_at='',changed_at='',revision=0,payload_json='{}'"
                ).rowcount
            )
        )
        state = (await self.archive.get_wardrobe_snapshot())["items"]
        self.assertEqual(state[str(top.id)]["ownership"], "owned")
        self.assertEqual(state[str(top.id)]["condition"], "clean")
        self.assertEqual(state[str(bottom.id)]["condition"], "dirty")
        self.assertFalse(state[str(bottom.id)]["available"])
        self.assertEqual(state, (await self.archive.get_wardrobe_snapshot())["items"])

    async def test_legacy_backup_recovers_verified_generated_clothes(self):
        item = await self.item(
            attributes={
                "wardrobe_job": "archived-job",
                "wardrobe": {"ownership": "owned", "condition": "washing"},
            }
        )
        state = item.attributes["wardrobe"]
        self.assertEqual(state["ownership"], "owned")
        self.assertEqual(state["condition"], "washing")
        self.assertFalse(state["available"])

    async def test_closet_zip_restores_piece_care_and_keeps_newer_local_facts(self):
        def plugin():
            result = PagePlugin()
            self.addCleanup(shutil.rmtree, result.runtime.data_path.parent)
            result.runtime.archive = LifeArchive(result.runtime.data_path)
            self.addCleanup(result.runtime.archive.close)
            return result

        source = plugin()
        image = b"\x89PNG\r\n\x1a\nwardrobe-backup"
        image_hash = hashlib.sha256(image).hexdigest()
        path = source.runtime.data_path.parent / STYLE_CATALOG_DIR_NAME / "outfit.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        items = {}
        # 套装先导出，必须先恢复两个单品，再处理别名。
        for kind in ("top", "bottom", "outfit"):
            items[kind] = await source.runtime.archive.upsert_style_catalog_item(
                {
                    "kind": kind,
                    "source_image_hash": image_hash,
                    "image_path": str(path),
                    "description": "米色针织上衣和长裤",
                    "confidence": 0.95,
                }
            )
        now = datetime.datetime(2026, 10, 8, 12)
        for decision in ("dirty", "washing"):
            self.assertTrue(
                await source.runtime.archive.start_wardrobe_care(
                    [items["top"].id],
                    decision=decision,
                    event_id=decision,
                    at=str(now),
                    observer="test",
                    duration_minutes=5,
                    reason="衣物需要清洗",
                )
            )
        self.assertTrue(
            await source.runtime.archive.start_wardrobe_care(
                [items["bottom"].id],
                decision="stored",
                event_id="store",
                at=str(now),
                observer="test",
                duration_minutes=0,
                reason="干净长裤收纳",
            )
        )
        original = {
            item.kind: item.attributes["wardrobe"]
            for item in await source.runtime.archive.get_style_catalog_items(limit=500)
        }
        backup = await source.page_closet_backup()
        target = plugin()
        target.upload_path = backup["data"]["path"]
        restored = await target.page_closet_restore()
        self.assertTrue(restored["ok"], restored)
        self.assertEqual(restored["data"]["restored"], 3)
        restored_items = {
            item.kind: item
            for item in await target.runtime.archive.get_style_catalog_items(limit=500)
        }
        for kind, item in restored_items.items():
            self.assertEqual(item.attributes["wardrobe"], original[kind])
            self.assertEqual(Path(item.image_path).read_bytes(), image)
        self.assertTrue(
            await target.runtime.archive.start_wardrobe_care(
                [restored_items["bottom"].id],
                decision="clean",
                event_id="retrieve",
                at=str(now),
                observer="test",
                duration_minutes=0,
                reason="取出长裤",
            )
        )
        self.assertTrue(
            await target.runtime.archive.start_wardrobe_care(
                [restored_items["bottom"].id],
                decision="dirty",
                event_id="new-dirt",
                at=str(now),
                observer="test",
                duration_minutes=0,
                reason="长裤刚刚弄脏",
            )
        )
        for minute in range(1, 7):
            await target.runtime.archive.advance_wardrobe_care(
                now=now + datetime.timedelta(minutes=minute), observer="test"
            )
        restored_again = await target.page_closet_restore()
        self.assertTrue(restored_again["ok"], restored_again)
        state = (await target.runtime.archive.get_wardrobe_snapshot())["items"]
        self.assertEqual(state[str(restored_items["top"].id)]["condition"], "drying")
        self.assertEqual(state[str(restored_items["bottom"].id)]["condition"], "dirty")


class NaturalHomeClothingTest(unittest.TestCase):
    def payload(self):
        return {
            "life_decision": {"outfit": {"style_pool": "sleep_styles"}},
            "timeline": [
                {"place_kind": "home", "activity": "吃早餐"},
                {"place_kind": "home", "activity": "整理房间并读书"},
                {"place_kind": "home", "activity": "在家工作"},
            ],
            "planned_actions": [
                {"action_type": "chore", "timeline_index": 1, "target": "整理房间"},
                {"action_type": "work", "timeline_index": 2, "target": "工作"},
            ],
        }

    def test_comfortable_home_activities_can_continue_sleepwear(self):
        self.assertEqual(
            DailyContractComponent._daytime_outfit_transition_issue(
                self.payload(), expected_coverage="full_day"
            ),
            "",
        )

    def test_outing_requires_change_before_departure(self):
        payload = self.payload()
        payload["timeline"].append({"place_kind": "transit", "activity": "出门去书店"})
        payload["planned_actions"].append(
            {"action_type": "change_outfit", "timeline_index": 4, "target": "外出穿搭"}
        )
        self.assertIn(
            "首次外出",
            DailyContractComponent._daytime_outfit_transition_issue(
                payload, expected_coverage="full_day"
            ),
        )
        payload["planned_actions"][-1]["timeline_index"] = 3
        self.assertEqual(
            DailyContractComponent._daytime_outfit_transition_issue(
                payload, expected_coverage="full_day"
            ),
            "",
        )
