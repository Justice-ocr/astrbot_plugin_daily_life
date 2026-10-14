import asyncio
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from support import DayRecord, LifeArchive, TimelineItem
from testwardrobelife import Runtime
from core.life.dressing import digest, wardrobe_fact_text
from core.life.curation import current_wardrobe_choice, validate_wardrobe_choice


class WardrobeChoiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = LifeArchive(Path(self.tmp.name) / "life.db")
        self.runtime = Runtime(self.archive)
        self.runtime.wardrobe_life._run_wardrobe_job = AsyncMock()
        self.day = DayRecord(date="2026-10-08", outfit="喜欢的旧裙子")
        await self.archive.save_day(self.day)
        self.item = await self.archive.upsert_style_catalog_item(
            {
                "kind": "outfit",
                "source_image_hash": "a" * 64,
                "description": "旧的针织裙",
                "confidence": 0.95,
            }
        )
        await self.archive.adopt_wardrobe_items(
            [self.item.id],
            event_id="owned",
            at=str(self.runtime.now),
            reason="已有衣物",
        )
        self.choice = {
            "decision": "generate",
            "motive": "interest",
            "key": "soft-layering",
            "requirement": "可叠穿的浅色针织外套配半身裙完整搭配",
            "reason": "喜欢针织的层次，想尝试能和旧裙子交替穿的新组合",
            "difference": "已有的是一体裙，想尝试分层穿搭，可分别搭配复用",
            "item_ids": [self.item.id],
            "evidence_ids": [
                "persona:" + digest(await self.runtime.get_persona_text())
            ],
        }

    async def asyncTearDown(self):
        await self.runtime.background.cancel_all()
        await self.archive.aclose()
        self.tmp.cleanup()

    async def review(self, choice=None):
        service = self.runtime.wardrobe_life
        day = await self.archive.get_day(self.day.date)
        snapshot = await self.archive.get_wardrobe_snapshot()
        conditions = service._wardrobe_conditions(day, self.runtime.now)
        inventory = digest(
            {
                k: {field: v.get(field) for field in ("ownership", "condition")}
                for k, v in snapshot["items"].items()
            }
        )
        self.runtime.call_text_model.return_value = json.dumps(
            {
                "suitability": [
                    {"item_id": self.item.id, "verdict": "suitable", "score": 0.9}
                ],
                "acquisition": self.choice if choice is None else choice,
            },
            ensure_ascii=False,
        )
        await service._review_wardrobe(day, conditions, snapshot, inventory, digest([]))
        await asyncio.gather(*list(self.runtime.background.tasks))
        return await self.archive.get_wardrobe_snapshot()

    async def finish_job(self, job_id, status="completed"):
        await self.archive.claim_wardrobe_job(job_id, "test", now=self.runtime.now)
        await self.archive.update_wardrobe_job(
            job_id,
            "test",
            status=status,
            progress={"submitted_at": str(self.runtime.now)},
            at=str(self.runtime.now),
            release=True,
        )

    async def test_aesthetic_interest_generates_despite_suitable_owned_clothes(self):
        state = await self.review()
        self.assertEqual(state["profile"]["acquisition"]["status"], "queued")
        self.assertEqual(len(state["jobs"]), 1)
        payload = state["jobs"][0]["payload"]
        self.assertEqual(payload["motive"], "interest")
        self.assertEqual(payload["generation_mode"], "image_to_image")
        self.assertEqual(
            (await self.archive.get_day(self.day.date)).outfit, "喜欢的旧裙子"
        )
        self.assertEqual(await self.archive.get_life_action_receipts(), [])

    async def test_latest_ownership_and_laundry_are_part_of_model_context(self):
        await self.archive.start_wardrobe_care(
            [self.item.id],
            decision="dirty",
            event_id="dirty",
            at=str(self.runtime.now),
            observer="test",
            duration_minutes=10,
            reason="实际穿后待洗",
        )
        await self.review({"decision": "keep", "reason": "先穿现有合适的衣物"})
        prompt = self.runtime.call_text_model.call_args.args[1]
        data = json.loads(prompt.split("【眼前内容】\n", 1)[1])
        garment = next(i for i in data["wardrobe"] if i["id"] == self.item.id)
        self.assertEqual(garment["attributes"]["wardrobe"]["condition"], "dirty")
        self.assertFalse(garment["attributes"]["wardrobe"]["available"])
        self.assertEqual(garment["attributes"]["wardrobe"]["ownership"], "owned")

    async def test_keep_and_consider_do_not_generate_or_accumulate_duplicate_history(
        self,
    ):
        for decision in ("keep", "consider"):
            state = await self.review({**self.choice, "decision": decision})
            self.assertEqual(state["jobs"], [])
            size = len(state["profile"]["acquisition_history"])
            self.runtime.now += datetime.timedelta(minutes=31)
            state = await self.review({**self.choice, "decision": decision})
            self.assertEqual(len(state["profile"]["acquisition_history"]), size)
        self.runtime.wardrobe_life._run_wardrobe_job.assert_not_awaited()

    async def test_same_completed_idea_is_not_regenerated_after_cooldown(self):
        first = await self.review()
        job_id = first["jobs"][0]["job_id"]
        await self.finish_job(job_id)
        self.runtime.now += datetime.timedelta(days=2)
        second = await self.review({**self.choice, "reason": "同一个想法换个说法"})
        self.assertEqual(second["jobs"], [])
        self.assertEqual(len(second["recent_jobs"]), 1)
        self.assertEqual(second["profile"]["acquisition"]["status"], "already_prepared")
        self.assertIn("已有生成记录", wardrobe_fact_text(second))

    async def test_new_idea_is_remembered_when_generation_frequency_is_limited(self):
        first = await self.review()
        await self.finish_job(first["jobs"][0]["job_id"])
        second = await self.review({**self.choice, "key": "another-idea"})
        self.assertEqual(second["profile"]["acquisition"]["status"], "cooldown")
        self.assertEqual(second["profile"]["acquisition"]["key"], "another-idea")
        self.assertEqual(len(second["recent_jobs"]), 1)

    async def test_disabled_generation_keeps_reason_without_queueing(self):
        self.runtime.config.image_generation.creative_wardrobe.enabled = False
        state = await self.review()
        self.assertEqual(state["profile"]["acquisition"]["status"], "disabled")
        self.assertEqual(state["jobs"], [])

    async def test_fabricated_evidence_cannot_trigger_generation(self):
        for invalid in (["invented"], [], [{"bad": "type"}]):
            state = await self.review({**self.choice, "evidence_ids": invalid})
            self.assertEqual(state["profile"]["acquisition"]["status"], "unverified")
            self.assertEqual(state["jobs"], [])

    async def test_occasion_uses_upcoming_plan_and_cancellation_invalidates_review(
        self,
    ):
        service = self.runtime.wardrobe_life
        day = await self.archive.get_day(self.day.date)
        day.timeline = [TimelineItem(time="18:00", activity="去看已约好的展览")]
        await self.archive.save_day(day)

        async def decide(provider, prompt, *args, **kwargs):
            data = json.loads(prompt.split("【眼前内容】\n", 1)[1])
            ref = next(
                k for k, v in data["sources"].items() if v["kind"] == "upcoming_plan"
            )
            await self.archive.mutate_day(
                day.date, lambda latest: latest.timeline.clear()
            )
            return json.dumps(
                {
                    "acquisition": {
                        **self.choice,
                        "motive": "occasion",
                        "evidence_ids": [ref],
                    }
                }
            )

        self.runtime.call_text_model.side_effect = decide
        state = await self.review()
        self.assertEqual(state["jobs"], [])
        self.assertEqual(state["profile_revision"], 0)
        service._run_wardrobe_job.assert_not_awaited()

    async def test_stale_review_cannot_create_generation_task(self):
        state = await self.archive.get_wardrobe_snapshot()
        await self.archive.save_wardrobe_review(
            {"aesthetics": []},
            expected_revision=state["profile_revision"],
            at=str(self.runtime.now),
        )
        accepted = await self.archive.save_wardrobe_review(
            {"acquisition": self.choice},
            expected_revision=state["profile_revision"],
            at=str(self.runtime.now),
            generation={"job_id": "stale", "payload": {"automatic": True}},
        )
        self.assertFalse(accepted)
        self.assertEqual((await self.archive.get_wardrobe_snapshot())["jobs"], [])

    async def test_uncertain_job_prevents_automatic_duplicate_spending(self):
        state = await self.review()
        await self.finish_job(state["jobs"][0]["job_id"], "uncertain")
        self.runtime.now += datetime.timedelta(days=2)
        state = await self.review({**self.choice, "key": "new-key"})
        self.assertEqual(state["profile"]["acquisition"]["status"], "waiting")
        self.assertEqual(len(state["jobs"]), 1)

    async def test_finished_job_display_uses_receipt_not_old_queued_label(self):
        state = await self.review()
        await self.finish_job(state["jobs"][0]["job_id"])
        state = await self.archive.get_wardrobe_snapshot()
        self.assertEqual(state["profile"]["acquisition"]["status"], "queued")
        self.assertEqual(current_wardrobe_choice(state)["status"], "already_prepared")

    def test_feedback_alone_does_not_become_own_interest(self):
        result = validate_wardrobe_choice(
            {**self.choice, "item_ids": [], "evidence_ids": ["feedback"]},
            {"feedback": {"kind": "feedback"}},
            {},
        )
        self.assertEqual(result["status"], "unverified")

    def test_remix_requires_available_existing_clothes(self):
        result = validate_wardrobe_choice(
            {**self.choice, "decision": "remix", "evidence_ids": []},
            {},
            {str(self.item.id): {"available": False}},
        )
        self.assertEqual(result["status"], "unverified")
