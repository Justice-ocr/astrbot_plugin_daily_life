from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import uuid

import aiohttp

from astrbot.api import logger

from ..sight.clip import SightClip
from ..sight.sample import (
    SightFrame,
    _extract_frame,
    ffmpeg_executable,
    prepare_sample_video_source,
)
from .feed import BiliLoginRequired, BiliReadError
from .storyboards import scene_seconds, storyboard, subtitle_strip


class BrowseMaterial:
    """素材可提前缓存，只有实际处理的时间窗口能进入浏览经历。"""

    def __init__(self, runtime, reader):
        self.runtime = runtime
        self.reader = reader
        self.cache = {}

    async def load(self, metadata):
        await self.reader.require_login()
        key = f"{metadata['bvid']}:{metadata['page']}"
        if key in self.cache:
            return self.cache[key]
        clip = SightClip(
            scope="bili_material",
            source=metadata["url"],
            name=metadata["title"],
            origin="bilibili",
            metadata=metadata,
        )
        segments, source_path = [], None
        try:
            segments = await self.reader.subtitles(metadata)
        except BiliLoginRequired:
            raise
        except BiliReadError:
            pass
        if not segments:
            await self.reader.require_login()
            # 复用现有下载、大小限制、ASR 路由和磁盘缓存，不启动另一套模型客户端。
            try:
                prepared = await asyncio.wait_for(
                    self.runtime._prepare_sight_clip_material(None, clip),
                    timeout=self.runtime.config.sight.total_timeout_seconds,
                )
                result = prepared.get("text_result")
                segments = [
                    dict(item, source="音频转写")
                    for item in (getattr(result, "metadata", {}) or {}).get(
                        "transcript_segments", []
                    )
                ]
                source_path = prepared.get("source_path")
            except Exception as exc:
                logger.debug(
                    f"[日常生活] 音频暂未理解成功，继续尝试片段画面：{type(exc).__name__}"
                )
        audience = {"comments": [], "danmaku": []}
        if self.runtime.config.browsing.audience_enabled:
            audience = await self.reader.audience(metadata)
        value = {"segments": segments, "source_path": source_path, **audience}
        # 小型内存缓存不进入聊天、备份或浏览完成判断。
        if len(self.cache) >= 4:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = value
        return value

    async def window(self, record, start, end):
        material = await self.load(record)
        evidence = []
        for row in material["segments"]:
            try:
                lo, hi = float(row["start"]), float(row["end"])
            except (ValueError, TypeError, KeyError):
                continue
            if lo < hi <= end and hi > start and str(row.get("text") or "").strip():
                evidence.append(
                    {
                        "start": lo,
                        "end": hi,
                        "quote": str(row["text"])[:500],
                        "source": row.get("source", "字幕"),
                    }
                )
        # 有画面能力时补充当前片段的真实画面，不能据标题臆测画面。
        await self.reader.require_login()
        frame_notes = await self.frames(record, material, start, end)
        evidence.extend(frame_notes)
        danmaku = [row for row in material["danmaku"] if start <= row["at"] < end][:8]
        return evidence, {"comments": material["comments"], "danmaku": danmaku}

    async def frames(self, record, material, start, end):
        if not self.runtime.config.sight.frame_provider and material["segments"]:
            return []
        source_path = material.get("source_path")
        root = None
        try:
            root = self.runtime._sight_cache_dir() / "browsing_frames" / uuid.uuid4().hex
            await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
            frames = []
            if getattr(self.runtime.config.browsing, "enhanced_vision", True):
                try:
                    frames = await storyboard(self.reader, record, root, start, end)
                except (BiliReadError, ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError):
                    pass
            if source_path is None:
                source_path = None if frames else await prepare_sample_video_source(
                    record["url"],
                    self.runtime._sight_cache_dir(),
                    max_video_mb=self.runtime.config.sight.video_download_max_mb,
                    download_timeout_seconds=self.runtime.config.sight.video_download_timeout_seconds,
                )
                material["source_path"] = source_path
            ffmpeg = ffmpeg_executable()
            if not frames and (not source_path or not ffmpeg):
                return []
            second = round(start + (end - start) / 2, 2)
            if source_path and ffmpeg:
                seconds = [second]
                if getattr(self.runtime.config.browsing, "enhanced_vision", True):
                    seconds += await asyncio.to_thread(scene_seconds, ffmpeg, source_path, start, end)
                    seconds += [start + min(1, (end-start)/4), end - min(1, (end-start)/4)]
                for moment in dict.fromkeys(round(s, 2) for s in seconds if start <= s < end):
                    path = root / f"{record['page']}_{moment:.2f}.jpg"
                    if await asyncio.to_thread(_extract_frame, ffmpeg, Path(source_path), path, moment):
                        frames.append(SightFrame(path=path, second=moment, label=f"{moment:.1f}秒"))
                    if len(frames) >= 5:
                        break
            if not frames:
                return []
            frames.sort(key=lambda frame: frame.second)
            if getattr(self.runtime.config.browsing, "enhanced_vision", True):
                path = root / f"{record['page']}_{start:.1f}_subtitles.jpg"
                try:
                    if await asyncio.to_thread(subtitle_strip, frames, path):
                        frames.append(SightFrame(path=path, second=second, label="采样画面底部按时间排列；识别看得清的字幕或画面文字，保留原文，看不清的部分留空"))
                except (OSError, ValueError):
                    pass
            clip = SightClip(
                scope=record["scope"],
                source=record["url"],
                name=record["title"],
                origin="bilibili",
                metadata={
                    "duration": record["duration"],
                    "processed_window": [start, end],
                },
            )
            notes = await self.runtime._describe_sight_frames(clip, frames)
            assets = getattr(notes, "assets", None) or []
            indexed = {str(frame.path): frame for frame in frames}
            result = []
            for asset in assets:
                frame = indexed.get(str(asset.get("path") or ""))
                if frame and str(asset.get("note") or "").strip():
                    text_region = frame.path.name.endswith("_subtitles.jpg")
                    result.append({"start": min(f.second for f in frames) if text_region else frame.second,
                                   "end": max(f.second for f in frames) if text_region else frame.second,
                                   "quote": str(asset["note"])[:700], "source": "采样画面文字" if text_region else "片段画面理解"})
            if not result:
                result = [{"start": frames[0].second, "end": frames[0].second, "quote": str(note)[:700], "source": "片段画面理解"}
                          for note in (getattr(notes, "notes", None) or []) if str(note).strip()]
            return result[:8]
        except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
            return []
        finally:
            if root:
                await asyncio.to_thread(shutil.rmtree, root, ignore_errors=True)
