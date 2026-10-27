"""故事板、场景变化与字幕区域；每张图保留实际时间，不等同看完整片。"""
from __future__ import annotations

import asyncio
import io
import math
import re
import struct
import subprocess
from urllib.parse import urlparse

import aiohttp

from ..security import is_http_url_allowed_async
from ..sight.sample import SightFrame
from .feed import API, BiliReadError


def snapshot_entries(data, duration):
    rows = []
    for index, item in enumerate((data.get("index") or [])[:20000]):
        try:
            second = float(item.get("timestamp") if isinstance(item, dict) else item)
            if math.isfinite(second) and second >= 0:
                rows.append((second, index))
        except (ValueError, TypeError):
            continue
    scale = 1000 if duration > 0 and rows and max(t for t, _ in rows) > duration * 1.5 else 1
    return [(t / scale, index) for t, index in rows if t / scale <= duration]


async def public_bytes(url):
    if str(url).startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}
        or not any((parsed.hostname or "").endswith("." + domain) for domain in ("hdslb.com", "bilibili.com"))
        or not await is_http_url_allowed_async(url)):
        raise BiliReadError("B站图片地址未通过检查")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise BiliReadError("故事板图片暂时无法读取")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise BiliReadError("故事板图片过大")
                chunks.append(chunk)
    return b"".join(chunks)


def tile(raw, index, width, height, target):
    from PIL import Image
    image = Image.open(io.BytesIO(raw))
    if image.width * image.height > 20_000_000 or not 16 <= width <= 1920 or not 16 <= height <= 1080:
        raise ValueError("故事板尺寸异常")
    columns, rows = image.width // width, image.height // height
    if columns < 1 or rows < 1 or not 0 <= index < columns * rows:
        raise ValueError("故事板位置越界")
    x, y = index % columns * width, index // columns * height
    image.crop((x, y, x + width, y + height)).convert("RGB").save(target, "JPEG", quality=85)
    return columns * rows


async def storyboard(reader, record, root, start, end):
    # videoshot 对不同分P的覆盖并不可靠；其他分P只用实际视频解码。
    if int(record.get("page", 1)) != 1:
        return []
    data = await reader._request(API + "/x/player/videoshot", {"bvid": record["bvid"], "cid": record["cid"], "index": 1})
    entries = snapshot_entries(data, float(record["duration"]))
    if not entries and data.get("pvdata"):
        raw = await public_bytes(data["pvdata"])
        if len(raw) % 2 == 0 and len(raw) <= 40000:
            entries = [(float(second), i) for i, second in enumerate(struct.unpack(f">{len(raw)//2}H", raw))
                       if second <= float(record["duration"])]
    entries = [(second, index) for second, index in entries if start <= second < end]
    images = data.get("image") or []
    if not entries or not images:
        return []
    width, height = int(data.get("img_x_size") or 480), int(data.get("img_y_size") or 270)
    raw = await public_bytes(images[0])
    first = root / "story_geometry.jpg"
    capacity = await asyncio.to_thread(tile, raw, 0, width, height, first)
    await asyncio.to_thread(first.unlink, missing_ok=True)
    picked, cache, frames = set(), {0: raw}, []
    for target in (start + (end-start)/2, start, end - 0.1):
        second, index = min(entries, key=lambda row: abs(row[0] - target))
        if index in picked:
            continue
        picked.add(index)
        number, offset = divmod(index, capacity)
        if number >= len(images):
            continue
        if number not in cache:
            cache[number] = await public_bytes(images[number])
        path = root / f"story_{record['page']}_{second:.2f}.jpg"
        await asyncio.to_thread(tile, cache[number], offset, width, height, path)
        frames.append(SightFrame(path=path, second=second, label=f"故事板实际采样 {second:.1f}秒"))
    return frames


def scene_seconds(ffmpeg, source, start, end):
    command = [ffmpeg, "-hide_banner", "-nostdin", "-ss", str(start), "-i", str(source), "-t", str(min(120, end-start)),
               "-an", "-vf", "select='gt(scene,0.25)',showinfo", "-frames:v", "3", "-f", "null", "-"]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=18, check=False)
        return [start + float(value) for value in re.findall(rb"pts_time:([0-9.]+)", result.stderr[-20000:])][:3]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []


def subtitle_strip(frames, target):
    from PIL import Image
    parts = []
    for frame in sorted(frames, key=lambda frame: frame.second)[:5]:
        image = Image.open(frame.path).convert("RGB")
        if image.width * image.height > 20_000_000:
            continue
        crop = image.crop((0, int(image.height * .78), image.width, image.height))
        crop.thumbnail((1280, 300))
        parts.append(crop)
    if not parts:
        return False
    image = Image.new("RGB", (max(p.width for p in parts), sum(p.height for p in parts)), "black")
    top = 0
    for part in parts:
        image.paste(part, (0, top))
        top += part.height
    image.save(target, "JPEG", quality=88)
    return True
