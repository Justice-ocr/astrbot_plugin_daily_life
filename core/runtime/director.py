from __future__ import annotations

from typing import Any

from .stage.error import MediaPromptExtractionError


def _clean_director_text(value: Any, limit: int = 180) -> str:
    from .stage.lens import clean_director_text

    return clean_director_text(value, limit)


__all__ = [
    "MediaPromptExtractionError",
    "_clean_director_text",
]
