from __future__ import annotations

import datetime
import zoneinfo

from .trail import parse as _part_history_parse_component
from .trail import scene as _part_history_scene_component
from typing import Any, Protocol


class RuntimeHistoryComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _astrbot_config: Any


class RuntimeHistoryComponent:
    def __init__(self, context: RuntimeHistoryComponentContext) -> None:
        self.context = context

    _OBSERVED_USER_HISTORY_MARKER = "daily_life_observed_user"
    _WEEKDAY_NAMES = (
        "星期一",
        "星期二",
        "星期三",
        "星期四",
        "星期五",
        "星期六",
        "星期日",
    )

    def _astrbot_now_for_scope(self, scope: str) -> datetime.datetime:
        timezone_name = str(
            self.context._astrbot_config(scope).get("timezone") or "Asia/Shanghai"
        ).strip()
        if timezone_name:
            try:
                return datetime.datetime.now(zoneinfo.ZoneInfo(timezone_name))
            except Exception:
                return datetime.datetime.now().astimezone()
        return datetime.datetime.now().astimezone()

    @staticmethod
    def _history_text_from_value(value: _part_history_parse_component.Any) -> str:
        return _part_history_parse_component.HistoryParseComponent._history_text_from_value(value)

    @classmethod
    def _history_text_from_message_item(cls, item: _part_history_parse_component.Any) -> str:
        return _part_history_parse_component.HistoryParseComponent._history_text_from_message_item.__func__(cls, item)

    @classmethod
    def _history_text_from_message_items(cls, items: list[_part_history_parse_component.Any]) -> str:
        return _part_history_parse_component.HistoryParseComponent._history_text_from_message_items.__func__(cls, items)

    @classmethod
    def _history_primary_text(cls, value: _part_history_parse_component.Any) -> str:
        return _part_history_parse_component.HistoryParseComponent._history_primary_text.__func__(cls, value)

    @staticmethod
    def _conversation_history_list(conversation: _part_history_parse_component.Any) -> list[dict]:
        return _part_history_parse_component.HistoryParseComponent._conversation_history_list(conversation)

    @staticmethod
    def _history_item_matches(item: _part_history_parse_component.Any, role: str, content: str) -> bool:
        return _part_history_parse_component.HistoryParseComponent._history_item_matches(item, role, content)

    @staticmethod
    def _history_user_item_matches(item: _part_history_parse_component.Any, expected: dict[str, _part_history_parse_component.Any]) -> bool:
        return _part_history_parse_component.HistoryParseComponent._history_user_item_matches(item, expected)

    @staticmethod
    def _history_content_has_media(content: _part_history_parse_component.Any) -> bool:
        return _part_history_parse_component.HistoryParseComponent._history_content_has_media(content)

    @staticmethod
    def _history_message_parts_for_user(text: str, media_label: str='') -> list[dict[str, str]]:
        return _part_history_parse_component.HistoryParseComponent._history_message_parts_for_user(text, media_label)

    @staticmethod
    async def _history_display_url(path: str) -> str:
        return await _part_history_parse_component.HistoryParseComponent._history_display_url(path)

    @classmethod
    async def _history_media_parts_from_event(cls, event: _part_history_parse_component.Any) -> list[dict[str, _part_history_parse_component.Any]]:
        return await _part_history_parse_component.HistoryParseComponent._history_media_parts_from_event.__func__(cls, event)

    @staticmethod
    def _history_media_type(kind: str) -> str:
        return _part_history_parse_component.HistoryParseComponent._history_media_type(kind)

    @staticmethod
    async def _history_media_payload(item: _part_history_parse_component.Any, media_type: str) -> dict[str, _part_history_parse_component.Any]:
        return await _part_history_parse_component.HistoryParseComponent._history_media_payload(item, media_type)

    _ASTRBOT_WEEKDAY_NAMES = _part_history_scene_component.HistorySceneComponent._ASTRBOT_WEEKDAY_NAMES

