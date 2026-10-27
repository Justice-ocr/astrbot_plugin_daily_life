from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from astrbot.api import logger

from ..clock import timestamp as life_timestamp
from ..paths import runtime_data_root
from ..runtime.markers import LOG_PREFIX

from typing import Any, Protocol


class MediaFileCleanupComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _media_keep_days: Any
    _prune_media_cache_tree: Any
    archive: Any
    config: Any
    data_path: Any


class MediaFileCleanupComponent:
    def __init__(self, context: MediaFileCleanupComponentContext) -> None:
        self.context = context

    def _media_file_cache_root(self) -> Path:
        return runtime_data_root(getattr(self.context, "data_path", None))

    @staticmethod
    def _media_keep_days(settings: object, key: str, default: int) -> int:
        try:
            return max(int(getattr(settings, key, default) or 0), 0)
        except (TypeError, ValueError):
            return max(int(default), 0)

    async def maintain_plugin_file_cache(self) -> dict[str, int]:
        settings = getattr(getattr(self.context, "config", None), "storage", None)
        root = self._media_file_cache_root()
        jobs = (
            (
                "generated",
                self.context._media_keep_days(settings, "generated_media_keep_days", 30),
            ),
            ("reverse", self.context._media_keep_days(settings, "reverse_cache_keep_days", 7)),
        )
        deleted_files = 0
        deleted_dirs = 0
        for dirname, keep_days in jobs:
            if keep_days <= 0:
                continue
            target = root / dirname
            if not await asyncio.to_thread(target.is_dir):
                continue
            cutoff = life_timestamp() - keep_days * 86400
            files, dirs = await asyncio.to_thread(
                self.context._prune_media_cache_tree, target, cutoff
            )
            deleted_files += files
            deleted_dirs += dirs

        reverse_keep_days = self.context._media_keep_days(
            settings, "reverse_cache_keep_days", 7
        )
        archive = getattr(self.context, "archive", None)
        cleanup_reverse = getattr(archive, "cleanup_reverse_prompts", None)
        deleted_reverse_rows = 0
        if reverse_keep_days > 0 and callable(cleanup_reverse):
            deleted_reverse_rows = await cleanup_reverse(reverse_keep_days)

        if deleted_files or deleted_dirs or deleted_reverse_rows:
            logger.debug(
                f"{LOG_PREFIX} 插件文件缓存清理完成：清理文件 {deleted_files} 个，"
                f"清理目录 {deleted_dirs} 个，清理反推记录 {deleted_reverse_rows} 条"
            )
        return {
            "deleted_files": deleted_files,
            "deleted_dirs": deleted_dirs,
            "deleted_reverse_rows": deleted_reverse_rows,
        }

    @staticmethod
    def _prune_media_cache_tree(root: Path, cutoff: float) -> tuple[int, int]:
        return (
            MediaFileCleanupComponent._prune_media_cache_files(root, cutoff),
            MediaFileCleanupComponent._prune_media_cache_dirs(root),
        )

    @staticmethod
    def _prune_media_cache_files(root: Path, cutoff: float) -> int:
        deleted = 0
        files = [path for path in root.rglob("*") if path.is_file()]
        for path in sorted(files, key=lambda item: len(item.parts), reverse=True):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            with contextlib.suppress(OSError):
                path.unlink()
                deleted += 1
        return deleted

    @staticmethod
    def _prune_media_cache_dirs(root: Path) -> int:
        deleted = 0
        dirs = [path for path in root.rglob("*") if path.is_dir()]
        for path in sorted(dirs, key=lambda item: len(item.parts), reverse=True):
            with contextlib.suppress(OSError):
                if path.exists() and not any(path.iterdir()):
                    path.rmdir()
                    deleted += 1
        return deleted
