"""短暂访问真实直播内容；只计算实际取到并理解的片段。"""
from __future__ import annotations

import asyncio
import re
import math
import time
import uuid
from urllib.parse import urljoin, urlparse

import aiohttp

from ..security import is_http_url_allowed_async
from ..sight.clip import SightClip
from ..sight.sample import SightFrame, _extract_frame, _video_duration, ffmpeg_executable, ffprobe_executable
from .feed import BiliReadError, clean


async def validate_stream_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
        or not any((parsed.hostname or "").endswith("." + domain) for domain in ("bilivideo.com", "biliapi.net", "bilivideo.cn"))
        or not await is_http_url_allowed_async(url)):
        raise BiliReadError("直播流地址未通过检查")


async def live_bytes(url, limit):
    """播放列表与每个分片分别验证地址，不让 ffmpeg 自行访问远端资源。"""
    await validate_stream_url(url)
    chunks, size = [], 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"}) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise BiliReadError("直播分片暂时无法读取")
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > limit:
                    raise BiliReadError("直播片段超过读取预算")
                chunks.append(chunk)
    return b"".join(chunks)


def hls_parts(raw, base):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BiliReadError("直播播放列表编码无效") from None
    if not text.lstrip().startswith("#EXTM3U"):
        raise BiliReadError("直播播放列表格式无效")
    variants, segments, init, duration, bandwidth = [], [], "", 0.0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line.partition(":")[2])
            bandwidth = int(match[1]) if match else 0
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line.partition(":")[2].split(",")[0])
                if not math.isfinite(duration) or not 0 < duration <= 30:
                    raise ValueError("直播分片时长超出范围")
            except ValueError:
                raise BiliReadError("直播分片时长无效") from None
        elif line.startswith("#EXT-X-KEY:") and "METHOD=NONE" not in line:
            raise BiliReadError("当前直播分片需要额外解密，尝试其他线路")
        elif line.startswith("#EXT-X-BYTERANGE:"):
            raise BiliReadError("当前直播分片使用字节范围，尝试其他线路")
        elif line.startswith("#EXT-X-DISCONTINUITY"):
            segments = []
        elif line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if not match or "BYTERANGE=" in line:
                raise BiliReadError("直播初始化分片格式暂不支持")
            init = urljoin(base, match[1])
        elif line and not line.startswith("#"):
            if bandwidth is not None:
                variants.append((bandwidth, urljoin(base, line)))
                bandwidth = None
            elif duration > 0:
                segments.append((urljoin(base, line), duration))
                duration = 0.0
    return variants, segments, init


async def capture_hls(url, path, seconds):
    for _ in range(3):
        variants, segments, init = hls_parts(await live_bytes(url, 256 * 1024), url)
        if not variants:
            break
        url = min(variants, key=lambda row: row[0])[1]
    else:
        raise BiliReadError("直播播放列表嵌套过深")
    selected, duration = [], 0.0
    for segment, length in reversed(segments[-12:]):
        selected.insert(0, segment)
        duration += length
        if duration >= seconds:
            break
    if not selected:
        raise BiliReadError("直播暂时没有可读取的分片")
    content = bytearray(await live_bytes(init, 1024 * 1024) if init else b"")
    for segment in selected:
        content.extend(await live_bytes(segment, 8 * 1024 * 1024 - len(content)))
    if len(content) < 1024:
        raise BiliReadError("没有取到有效的直播分片")
    await asyncio.to_thread(path.write_bytes, content)
    return min(seconds, duration)


async def capture_live(url, path, seconds=8):
    await validate_stream_url(url)
    if urlparse(url).path.lower().endswith(".m3u8"):
        return await asyncio.wait_for(capture_hls(url, path, seconds), timeout=seconds + 20)
    data, started = bytearray(), time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=seconds + 6), trust_env=False,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"}) as session:
            async with session.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    raise BiliReadError("直播片段暂时无法读取")
                async for chunk in response.content.iter_chunked(65536):
                    if len(data) + len(chunk) > 8 * 1024 * 1024:
                        break
                    data.extend(chunk)
                    if time.monotonic() - started >= seconds:
                        break
    except (aiohttp.ClientError, asyncio.TimeoutError):
        if not data:
            raise BiliReadError("直播流暂时中断") from None
    if len(data) < 1024 or not data.startswith(b"FLV"):
        raise BiliReadError("没有取到有效的直播画面片段")
    await asyncio.to_thread(path.write_bytes, data)
    return min(seconds, max(0, time.monotonic() - started))


class BiliLiveVisit:
    def __init__(self, social):
        self.social = social
        self.runtime = social.runtime
        self.browsing = social.browsing
        self.started = False

    async def join(self, store, room):
        info = await self.social.platform.live_info(room["id"])
        if info.get("live_status") != 1:
            raise ValueError("这个直播间目前没有开播")
        now = self.browsing.now().isoformat(timespec="seconds")
        value = {"kind": "live", "scope": "global", "at": now, "status": "visiting", "room": room,
                 "seconds": 0, "last_tick": time.time(), "evidence": [], "text": "刚进入直播间，还没有理解内容"}
        value = await store.put("live:" + uuid.uuid4().hex, value)
        await store.update({"active_live": value})
        self.started = True
        return await self.tick(store)

    async def leave(self, store, reason):
        state = await store.state()
        active = state.get("active_live")
        if active:
            await store.finish(active, status="left", reason=reason)
            await store.update({"active_live": None})

    async def tick(self, store, blocked=""):
        state = await store.state()
        active = state.get("active_live")
        if not self.started:
            self.started = True
            if active:
                await self.leave(store, "运行曾中断，离线期间没有观看")
                return False
        if not active:
            return False
        date = self.browsing.now().date().isoformat()
        used = float(state.get("live_seconds") or 0) if state.get("live_date") == date else 0
        if blocked or time.time() - active["last_tick"] > 180 or used >= self.social.config.max_live_minutes * 60:
            await self.leave(store, blocked or "先离开直播间，未观察的间隔不计入观看")
            return False
        info = await self.social.platform.live_info(active["room"]["id"])
        if info.get("live_status") != 1:
            await self.leave(store, "直播已经结束")
            return False
        root = self.runtime._sight_cache_dir() / "bili_live"
        await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
        path = root / (uuid.uuid4().hex + ".flv")
        frames = []
        epoch = self.browsing._epoch
        try:
            probe = ffprobe_executable()
            if not probe:
                await self.leave(store, "暂时无法核实直播片段时长，先离开")
                return False
            failed = []
            deadline = time.monotonic() + 45
            for attempt in range(4):
                url = await self.social.platform.live_stream(active["room"]["id"], **({"exclude": failed} if failed else {}))
                if url in failed:
                    raise BiliReadError("直播备用线路也未能读取")
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BiliReadError("直播线路读取预算已用完")
                    seconds = await asyncio.wait_for(capture_live(url, path, seconds=min(8, self.social.config.max_live_minutes * 60 - used)), timeout=remaining)
                    decoded = await asyncio.to_thread(_video_duration, probe, path)
                    if decoded <= 0:
                        raise BiliReadError("当前线路的直播片段无法解码")
                    seconds = min(seconds, decoded, self.social.config.max_live_minutes * 60 - used)
                    break
                except (BiliReadError, aiohttp.ClientError, asyncio.TimeoutError):
                    failed.append(url)
                    if attempt == 3:
                        raise
            clip = SightClip(scope="global", source=str(path), name=active["room"]["title"], origin="bili_live",
                             metadata={"duration": seconds, "processed_window": [0, seconds], "room_id": active["room"]["id"]})
            evidence = []
            ffmpeg = ffmpeg_executable()
            if ffmpeg:
                for second in (0.5, 2.0, 5.0):
                    if second > seconds:
                        continue
                    frame = path.with_name(path.stem + f"_{second}.jpg")
                    if await asyncio.to_thread(_extract_frame, ffmpeg, path, frame, second):
                        frames.append(SightFrame(path=frame, second=second, label=f"刚刚的直播 {second}秒"))
                if frames:
                    notes = await self.runtime._describe_sight_frames(clip, frames)
                    evidence.extend({"source": "实际直播画面", "quote": clean(note, 700)} for note in getattr(notes, "notes", []) if note)
            try:
                prepared = await asyncio.wait_for(self.runtime._prepare_sight_clip_material(None, clip), timeout=45)
                result = prepared.get("text_result")
                evidence.extend({"source": "直播片段音频", "quote": clean(row.get("text"), 500)}
                    for row in (getattr(result, "metadata", {}) or {}).get("transcript_segments", [])[:12] if row.get("text"))
            except (RuntimeError, ValueError, asyncio.TimeoutError, BiliReadError):
                pass
            if not evidence:
                await self.leave(store, "没有可确认的画面或音频，不把弹幕当成直播内容")
                return False
            if self.browsing._closed or epoch != self.browsing._epoch or store.account != await self.browsing.reader.require_login():
                await self.leave(store, "被眼前的事情打断，保留已理解的经历")
                return False
            chat = await self.social.platform.live_history(active["room"]["id"])
            if self.browsing._closed or epoch != self.browsing._epoch:
                await self.leave(store, "交流打断了观看，未提交片段不计入进度")
                return False
            active.update(last_tick=time.time(), seconds=round(active["seconds"] + seconds, 2),
                          evidence=(active["evidence"] + evidence)[-12:], audience_unverified=chat,
                          text=clean(evidence[-1]["quote"], 500))
            await store.finish(active)
            await store.update({"active_live": active, "live_date": date, "live_seconds": used + seconds})
            return active
        except BaseException:
            await self.leave(store, "直播内容读取中断，先离开")
            raise
        finally:
            for file in [path, *(frame.path for frame in frames)]:
                await asyncio.to_thread(file.unlink, missing_ok=True)
