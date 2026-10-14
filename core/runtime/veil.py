from __future__ import annotations

from typing import Any


from .shade import render as _part_hidden_experience_component
from typing import Protocol


class InjectVeilComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    pass


class InjectVeilComponent:
    def __init__(self, context: InjectVeilComponentContext) -> None:
        self.context = context

    @staticmethod
    def _hidden_text(value: Any, limit: int = 120) -> str:
        return " ".join(str(value or "").strip().split())[:limit]

    @staticmethod
    def _hidden_lines(items: list[_part_hidden_experience_component.Any] | None, count: int, render: _part_hidden_experience_component.Callable[[_part_hidden_experience_component.Any], str]) -> list[str]:
        return _part_hidden_experience_component.HiddenExperienceComponent._hidden_lines(items, count, render)

    @staticmethod
    def _hidden_section(sections: list[str], title: str, lines: list[str], intro: str='') -> None:
        return _part_hidden_experience_component.HiddenExperienceComponent._hidden_section(sections, title, lines, intro)

