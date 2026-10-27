"""为语音保留本轮清洗前的正文，文字投递继续使用清洗后的结果。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

_VOICE_REPLY_TEXT_ATTR = "_daily_life_voice_reply_text"


@dataclass(frozen=True, slots=True)
class _VoiceReplyText:
    source: str
    rendered: str
    result: Any


def voice_reply_text(event: Any, current_text: str) -> str:
    """仅在当前结果仍与清洗结果一致时，供 TTS 读取原句。"""
    saved = getattr(event, _VOICE_REPLY_TEXT_ATTR, None)
    result = getattr(event, "get_result", lambda: None)()
    if (
        isinstance(saved, _VoiceReplyText)
        and saved.result is result
        and saved.rendered == current_text
    ):
        return saved.source
    return current_text


def _plain_reply_text(event: Any, text_from_item: Callable[[Any], str]) -> str:
    result = getattr(event, "get_result", lambda: None)()
    chain = getattr(result, "chain", None)
    if not isinstance(chain, list) or not chain:
        return ""
    texts = [text_from_item(item).strip() for item in chain]
    return "\n".join(texts) if all(texts) else ""


@contextmanager
def preserve_voice_reply_text(
    event: Any, text_from_item: Callable[[Any], str]
) -> Iterator[None]:
    """跨越分段与标点清洗保存原文；取消、清空或改写后不沿用旧内容。"""
    source = voice_reply_text(event, _plain_reply_text(event, text_from_item))
    if event is not None:
        setattr(event, _VOICE_REPLY_TEXT_ATTR, None)
    yield
    rendered = _plain_reply_text(event, text_from_item)
    if source and rendered:
        setattr(
            event,
            _VOICE_REPLY_TEXT_ATTR,
            _VoiceReplyText(source, rendered, event.get_result()),
        )
