from __future__ import annotations

import asyncio
import datetime

from astrbot.api import logger

from ...clock import now as life_now
from ...models import BehaviorSceneRecord, MemoryMaintenanceRecord
from ..generation import DAILY_REFRESH_GENERATED_DATE
from ..locks import operation_lock
from ..markers import LOG_PREFIX

from typing import Any, Protocol


class SpinePulseComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _proactive_idle_enabled: Any
    _settle_stale_reply_effects: Any
    _target_datetime_for_command: Any
    _virtual_life_metrics: Any
    archive: Any
    composer: Any
    config: Any
    evaluate_idle_proactive_candidates: Any
    evaluate_private_revisit_candidates: Any
    maintain_emoji_assets: Any
    maintain_plugin_file_cache: Any
    maintain_sight_cache: Any
    mark_page_status_changed: Any
    resolve_injection_target: Any
    run_daily_generation: Any
    run_memory_maintenance: Any


class SpinePulseComponent:
    def __init__(self, context: SpinePulseComponentContext) -> None:
        self.context = context

    async def run_daily_refresh(self) -> None:
        logger.info(f"{LOG_PREFIX} 正在执行每日日常生活背景刷新……")
        now = life_now()
        target_date, _ = await self.context.resolve_injection_target(now)
        target_dt = self.context._target_datetime_for_command(target_date, now)
        yesterday = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        existing = await self.context.archive.get_day(target_date)
        existing_meta = getattr(existing, "meta", {}) if existing is not None else {}
        already_generated = bool(
            isinstance(existing_meta, dict)
            and str(existing_meta.get(DAILY_REFRESH_GENERATED_DATE) or "")
            == target_date
        )
        generation_task = None
        if not already_generated:
            # 先登记当日生成任务，避免复盘期间的手动重生再启动第二个写入任务。
            generation_task = asyncio.create_task(
                self.context.run_daily_generation(
                    date=target_dt,
                    source="daily_refresh",
                    force=True,
                )
            )
        try:
            async with operation_lock(self.context, f"review:{yesterday}"):
                await self.context.composer.compose_daily_review(yesterday)
            if generation_task is not None:
                await generation_task
        # 复盘流程取消时同步收束配套生成任务，避免后台遗留写入。
        except BaseException:
            if generation_task is not None and not generation_task.done():
                generation_task.cancel()
            if generation_task is not None:
                await asyncio.gather(generation_task, return_exceptions=True)
            raise
        await self.context.archive.cleanup_by_storage_policy(self.context.config.storage)
        await self.context.maintain_sight_cache()
        await self.context.maintain_emoji_assets()
        await self.context.maintain_plugin_file_cache()

    async def run_nightly_review(self) -> None:
        logger.info(f"{LOG_PREFIX} 正在执行夜间复盘与记忆沉淀……")
        today = life_now().strftime("%Y-%m-%d")
        async with operation_lock(self.context, f"review:{today}"):
            await self.context.composer.compose_daily_review()
            await self.context.run_memory_maintenance()
            await self.context.mark_page_status_changed("nightly_review")

    async def run_memory_maintenance(self) -> None:
        today_str = life_now().strftime("%Y-%m-%d")
        await self.context._settle_stale_reply_effects()
        corrections = await self.context.archive.get_memory_corrections(limit=100)
        reply_effects = await self.context.archive.get_reply_effects(limit=100)
        emoji_assets = await self.context.archive.get_emoji_assets(limit=100)
        proactive_scene_count = await self._consolidate_proactive_behavior_scenes(
            today_str
        )
        recollection = {"consolidated": 0, "dormant": 0}
        consolidator = getattr(self.context.archive, "consolidate_living_memories", None)
        if callable(consolidator):
            recollection = await consolidator(today=today_str)
        applied_count = sum(
            1 for item in corrections if bool(getattr(item, "applied", False))
        )
        pending_count = sum(
            1 for item in corrections if not bool(getattr(item, "applied", False))
        )
        pending_reply_count = sum(
            1
            for item in reply_effects
            if str(getattr(item, "outcome", "") or "") == "pending"
        )
        ready_emoji_count = sum(
            1
            for item in emoji_assets
            if str(getattr(item, "status", "") or "") == "ready"
            and getattr(item, "sendable", True)
        )
        summary = (
            f"已应用记忆纠错 {applied_count} 条，未应用纠错 {pending_count} 条；"
            f"待观察回复效果 {pending_reply_count} 条；可用表情素材 {ready_emoji_count} 个；"
            f"行为经验归纳 {proactive_scene_count} 组；"
            f"经历整理 {recollection['consolidated']} 组，过期琐事收起 {recollection['dormant']} 条。"
        )
        saved = await self.context.archive.save_memory_maintenance(
            MemoryMaintenanceRecord(
                date=today_str,
                summary=summary,
                corrected_count=applied_count,
                merged_count=recollection["consolidated"],
                pruned_count=recollection["dormant"],
                reason="夜间复盘后自动检查长期记忆、回复效果与表达素材。",
            )
        )
        await self.context.archive.cleanup_by_storage_policy(self.context.config.storage)
        await self.context.maintain_sight_cache()
        await self.context.maintain_emoji_assets()
        await self.context.maintain_plugin_file_cache()
        metrics = getattr(self.context, "_virtual_life_metrics", None)
        if isinstance(metrics, dict) and metrics:
            metric_order = (
                "turn_received",
                "turn_reply_now",
                "turn_observe",
                "idle_scheduled",
                "idle_observe",
                "idle_reply_sent",
                "idle_send_failed",
                "turn_closed_by_reply",
                "turn_abandoned",
            )
            metric_text = "；".join(
                f"{name}={int(metrics.get(name, 0))}" for name in metric_order
            )
            logger.debug(f"{LOG_PREFIX} 智能生活轮次统计：{metric_text}")
        if saved:
            logger.info(f"{LOG_PREFIX} 长期记忆维护完成：{summary}")

    async def _consolidate_proactive_behavior_scenes(self, date_str: str) -> int:
        feedback_getter = getattr(self.context.archive, "get_behavior_feedback", None)
        scene_getter = getattr(self.context.archive, "get_behavior_scenes", None)
        scene_upserter = getattr(self.context.archive, "upsert_behavior_scene", None)
        if (
            not callable(feedback_getter)
            or not callable(scene_getter)
            or not callable(scene_upserter)
        ):
            return 0
        try:
            feedback_items = await feedback_getter(limit=120)
        except Exception as exc:
            logger.debug(f"{LOG_PREFIX} 闲时回复行为经验读取失败：{exc}")
            return 0

        groups: dict[str, list[object]] = {}
        for item in feedback_items:
            if str(getattr(item, "source", "") or "") != "proactive_reply":
                continue
            if str(getattr(item, "scene", "") or "") != "闲时回复读空气":
                continue
            if str(getattr(item, "date", "") or "") != date_str:
                continue
            scope = str(getattr(item, "target_id", "") or "").strip()
            if scope:
                groups.setdefault(scope, []).append(item)

        consolidated = 0
        for scope, items in groups.items():
            existing = await scene_getter(limit=20, scope=scope)
            if any(
                str(getattr(item, "scene", "") or "") == "闲时回复读空气"
                and str(getattr(item, "source", "") or "") == "proactive_feedback"
                and str(getattr(item, "last_seen", "") or "") == date_str
                for item in existing
            ):
                continue
            positives = sum(
                1
                for item in items
                if str(getattr(item, "result", "") or "") == "positive"
            )
            negatives = sum(
                1
                for item in items
                if str(getattr(item, "result", "") or "") == "negative"
            )
            if positives <= 0 and negatives <= 0:
                continue
            preferred_action = "reply" if positives >= negatives else "observe"
            avoid_action = "reply" if negatives > positives else ""
            hint = (
                "这段会话里闲时续话更容易被接住"
                if positives >= negatives
                else "这段会话里闲时续话收到过明确负向反馈，先观察更自然"
            )
            saved = await scene_upserter(
                BehaviorSceneRecord(
                    scope=scope,
                    scene="闲时回复读空气",
                    cues=[f"正反馈 {positives} 次", f"负反馈 {negatives} 次"],
                    preferred_action=preferred_action,
                    avoid_action=avoid_action,
                    outcome_hint=hint,
                    confidence=min(1.0, 0.45 + min(len(items), 6) * 0.08),
                    support_count=max(1, len(items)),
                    last_seen=date_str,
                    source="proactive_feedback",
                )
            )
            if saved:
                consolidated += 1
        return consolidated

    async def run_weekly_refresh(self) -> None:
        logger.info(f"{LOG_PREFIX} 正在执行周计划刷新……")
        week = life_now().strftime("%G-W%V")
        async with operation_lock(self.context, f"week:{week}"):
            await self.context.composer.generate_week_plan()
            await self.context.mark_page_status_changed("weekly_refresh")

    async def run_private_revisit_check(self) -> None:
        if not self.context.config.proactive.private_revisit_enabled:
            return
        await self.context.evaluate_private_revisit_candidates()

    async def run_proactive_idle_check(self) -> None:
        if not self.context._proactive_idle_enabled():
            return
        await self.context.evaluate_idle_proactive_candidates()
