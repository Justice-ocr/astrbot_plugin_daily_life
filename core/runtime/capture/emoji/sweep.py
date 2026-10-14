from __future__ import annotations

import asyncio
import datetime
from pathlib import Path

from astrbot.api import logger

from ....clock import now as life_now
from ....models import EmojiAssetRecord
from ...markers import LOG_PREFIX

from typing import Any, Protocol


class EmojiSweepComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    EMOJI_ASSET_INACTIVE_STATUSES: Any
    EMOJI_ASSET_REJECTED_STATUSES: Any
    _describe_emoji_asset_with_vision: Any
    _download_emoji_asset: Any
    _emoji_asset_cache_dir: Any
    _emoji_asset_is_remote: Any
    _emoji_inactive_record_keep_days: Any
    _emoji_max_ready_count: Any
    _emoji_replace_when_full: Any
    _emoji_review_batch_size: Any
    _prune_extra_ready_emoji_assets: Any
    _prune_inactive_emoji_asset_records: Any
    _remote_source_from_asset: Any
    _revalidate_review_emoji_assets: Any
    archive: Any
    cleanup_emoji_asset_cache: Any


class EmojiSweepComponent:
    def __init__(self, context: EmojiSweepComponentContext) -> None:
        self.context = context

    async def maintain_emoji_assets(self) -> dict[str, int]:
        assets = await self.context.archive.get_emoji_assets(limit=0)
        updated = 0
        repaired = 0

        for asset in assets:
            status = str(getattr(asset, "status", "") or "pending")
            path_text = str(getattr(asset, "file_path", "") or "").strip()
            if status in self.context.EMOJI_ASSET_REJECTED_STATUSES:
                continue
            if (
                not path_text
                or self.context._emoji_asset_is_remote(path_text)
                or await asyncio.to_thread(Path(path_text).exists)
            ):
                continue

            source = self.context._remote_source_from_asset(asset)
            if source:
                cache_dir = self.context._emoji_asset_cache_dir()
                cached_path = (
                    await self.context._download_emoji_asset(source, asset.file_hash, cache_dir)
                    if cache_dir
                    else None
                )
                if cached_path:
                    await self.context.archive.upsert_emoji_asset(
                        EmojiAssetRecord(
                            id=asset.id,
                            file_hash=asset.file_hash,
                            file_path=str(cached_path),
                            label=asset.label,
                            description=asset.description,
                            emotions=asset.emotions,
                            source_scope=asset.source_scope,
                            source_message_id=asset.source_message_id,
                            source_url=asset.source_url,
                            source_kind=asset.source_kind,
                            asset_type=asset.asset_type,
                            confidence=asset.confidence,
                            sendable=asset.sendable,
                            rejected_reason=asset.rejected_reason,
                            status=status,
                            used_count=asset.used_count,
                            last_used_at=asset.last_used_at,
                        )
                    )
                    repaired += 1
                    continue

            await self.context.archive.upsert_emoji_asset(
                EmojiAssetRecord(
                    id=asset.id,
                    file_hash=asset.file_hash,
                    file_path=asset.file_path,
                    label=asset.label,
                    description=asset.description,
                    emotions=asset.emotions,
                    source_scope=asset.source_scope,
                    source_message_id=asset.source_message_id,
                    source_url=asset.source_url,
                    source_kind=asset.source_kind,
                    asset_type=asset.asset_type,
                    confidence=asset.confidence,
                    sendable=False,
                    rejected_reason="文件不存在",
                    status="missing",
                    used_count=asset.used_count,
                    last_used_at=asset.last_used_at,
                )
            )
            updated += 1

        revalidated = await self.context._revalidate_review_emoji_assets()
        deleted_records = await self.context._prune_extra_ready_emoji_assets()
        deleted_inactive_records = await self.context._prune_inactive_emoji_asset_records()
        deleted_files = await self.context.cleanup_emoji_asset_cache()
        result = {
            "missing_marked": updated,
            "repaired": repaired,
            "revalidated": revalidated,
            "deleted_records": deleted_records,
            "deleted_inactive_records": deleted_inactive_records,
            "deleted_files": deleted_files,
        }
        if any(result.values()):
            logger.info(
                f"{LOG_PREFIX} 表情素材维护完成：标记缺失 {updated} 个，修复 {repaired} 个，"
                f"复审历史素材 {revalidated} 条，裁剪记录 {deleted_records} 条，"
                f"清理不可用记录 {deleted_inactive_records} 条，"
                f"清理缓存 {deleted_files} 个"
            )
        return result

    async def _revalidate_review_emoji_assets(self) -> int:
        ready_assets = await self.context.archive.get_emoji_assets(
            limit=0, status="ready", sendable_only=True
        )
        candidates = [
            asset
            for asset in ready_assets
            if str(getattr(asset, "source_kind", "") or "") == "review"
        ][: self.context._emoji_review_batch_size()]
        if not candidates:
            return 0
        revalidated = 0
        for asset in candidates:
            review_data = asset.as_dict()
            review_data.update(status="reviewing", sendable=False)
            reviewing = EmojiAssetRecord.from_value(review_data)
            if not reviewing:
                continue
            await self.context._describe_emoji_asset_with_vision(
                reviewing, preserve_on_failure=True
            )
            saved = await self.context.archive.get_emoji_asset_by_hash(asset.file_hash)
            if saved and (
                saved.status != asset.status
                or saved.sendable != asset.sendable
                or saved.source_kind != asset.source_kind
            ):
                revalidated += 1
        return revalidated

    def _remote_source_from_asset(self, asset: EmojiAssetRecord) -> str:
        source = str(getattr(asset, "source_url", "") or "").strip()
        return source if self.context._emoji_asset_is_remote(source) else ""

    async def _prune_extra_ready_emoji_assets(self) -> int:
        if not self.context._emoji_replace_when_full():
            return 0
        ready_assets = await self.context.archive.get_emoji_assets(
            limit=0, status="ready", sendable_only=True
        )
        overflow = len(ready_assets) - self.context._emoji_max_ready_count()
        if overflow <= 0:
            return 0

        def rank(asset: EmojiAssetRecord) -> tuple[int, str, int]:
            return (
                int(getattr(asset, "used_count", 0) or 0),
                str(
                    getattr(asset, "last_used_at", "")
                    or getattr(asset, "updated_at", "")
                    or getattr(asset, "created_at", "")
                    or ""
                ),
                int(getattr(asset, "id", 0) or 0),
            )

        to_delete = sorted(ready_assets, key=rank)[:overflow]
        return await self.context.archive.delete_emoji_assets([item.id for item in to_delete])

    async def _prune_inactive_emoji_asset_records(self) -> int:
        keep_days = self.context._emoji_inactive_record_keep_days()
        cutoff = (life_now() - datetime.timedelta(days=keep_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        assets = await self.context.archive.get_emoji_assets(limit=0)
        stale_ids = []
        for asset in assets:
            status = str(getattr(asset, "status", "") or "").strip()
            if status not in self.context.EMOJI_ASSET_INACTIVE_STATUSES:
                continue
            changed_at = str(
                getattr(asset, "updated_at", "")
                or getattr(asset, "created_at", "")
                or ""
            ).strip()
            if changed_at and changed_at < cutoff:
                stale_ids.append(int(getattr(asset, "id", 0) or 0))
        return await self.context.archive.delete_emoji_assets(stale_ids)
