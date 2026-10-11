"""将平台位置组件转换成可读消息；仅解析协议字段，不判断聊天意图。"""

from __future__ import annotations

import json
import math
from typing import Any

from .events import iter_event_sources, safe_invoke


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _kind(value: Any) -> str:
    kind = _field(value, "type") or _field(value, "kind") or value.__class__.__name__
    return str(getattr(kind, "value", kind)).lower()


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _coordinate(value: Any, bound: int) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and len(value) > 40:
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and abs(number) <= bound else None


def _location_fields(item: Any) -> dict[str, Any]:
    kind = _kind(item)
    if kind == "json":
        payload = _field(item, "data")
        for _ in range(3):
            if isinstance(payload, str):
                if len(payload) > 64 * 1024:
                    return {}
                try:
                    payload = json.loads(payload)
                except (ValueError, RecursionError):
                    return {}
            if not isinstance(payload, dict):
                return {}
            if "app" in payload:
                break
            payload = payload.get("data")
        if not isinstance(payload, dict) or payload.get("app") != "com.tencent.map":
            return {}
        meta = payload.get("meta")
        data = meta.get("Location.Search") if isinstance(meta, dict) else None
        if not isinstance(data, dict):
            return {}
        name, address = data.get("name"), data.get("address")
        lat, lon = data.get("lat"), data.get("lng")
    elif kind == "location":
        data = _field(item, "data")
        data = data if isinstance(data, dict) else item
        name, address = _field(data, "title"), _field(data, "content")
        lat, lon = _field(data, "lat"), _field(data, "lon")
    else:
        return {}
    result: dict[str, Any] = {}
    if name := _text(name, 100):
        result["地点"] = name
    if address := _text(address, 160):
        result["地址"] = address
    latitude, longitude = _coordinate(lat, 90), _coordinate(lon, 180)
    if latitude is not None and longitude is not None:
        result.update({"纬度": latitude, "经度": longitude})
    return result


def location_parts(items: Any, *, quoted: bool = False, depth: int = 0) -> list[str]:
    if not isinstance(items, (list, tuple)) or depth > 3:
        return []
    result: list[str] = []
    for item in items[:100]:
        fields = _location_fields(item)
        if fields:
            label = "引用的位置卡片" if quoted else "分享的位置卡片"
            result.append(f"[{label}] " + json.dumps(fields, ensure_ascii=False, separators=(",", ":")))
        if _kind(item) in {"reply", "quote"}:
            data = _field(item, "data")
            for source in (item, data):
                for key in ("chain", "message", "content"):
                    result.extend(location_parts(_field(source, key), quoted=True, depth=depth + 1))
        if len(result) >= 8:
            break
    return list(dict.fromkeys(result))[:8]


def event_location_parts(event: Any) -> list[str]:
    parts: list[str] = []
    for source in iter_event_sources(event):
        message = getattr(source, "message_obj", None)
        items = safe_invoke(getattr(source, "get_messages", None))
        parts.extend(location_parts(items))
        parts.extend(location_parts(getattr(message, "message", None)))
        raw = getattr(message, "raw_message", None) or getattr(source, "raw_message", None)
        parts.extend(location_parts(_field(raw, "message")))
        for key in ("quote", "reply", "reply_message"):
            quote = getattr(source, key, None)
            parts.extend(location_parts([quote], quoted=True))
            for child in ("chain", "message"):
                parts.extend(location_parts(_field(quote, child), quoted=True))
    return list(dict.fromkeys(parts))[:8]


def enrich_location_message(event: Any, request: Any = None) -> None:
    """在连续话轮与记忆收集前补齐正文；重复钩子和工具循环保持幂等。"""
    parts = event_location_parts(event)
    if not parts:
        return
    for source in iter_event_sources(event):
        original = str(getattr(source, "message_str", "") or "")
        additions = [part for part in parts if part not in original]
        if additions:
            content = "\n".join([original, *additions]).strip()
            source.message_str = content
            message = getattr(source, "message_obj", None)
            if message is not None:
                message.message_str = content
    if request is not None:
        original = str(getattr(request, "prompt", "") or "")
        additions = [part for part in parts if part not in original]
        if additions:
            request.prompt = "\n".join([original, *additions]).strip()
