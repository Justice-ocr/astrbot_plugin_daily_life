from __future__ import annotations

from typing import Any

from typing import Protocol


class StageAlbumComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _life_media_last_images: Any


class StageAlbumComponent:
    def __init__(self, context: StageAlbumComponentContext) -> None:
        self.context = context

    def _remember_life_image_for_scope(self, scope: str, path: Any) -> None:
        scope = str(scope or "").strip()
        path_text = str(path or "").strip()
        if not scope or not path_text:
            return
        cache = getattr(self.context, "_life_media_last_images", None)
        if not isinstance(cache, dict):
            cache = {}
            self.context._life_media_last_images = cache
        cache[scope] = path_text

    def _last_generated_life_image_path(self, scope: str = "") -> str:
        cache = getattr(self.context, "_life_media_last_images", None)
        if not isinstance(cache, dict):
            return ""
        return str(cache.get(str(scope or "").strip()) or "").strip()

    def _forget_last_generated_life_image_path(
        self, scope: str = "", expected_path: Any = ""
    ) -> None:
        cache = getattr(self.context, "_life_media_last_images", None)
        if not isinstance(cache, dict):
            return
        scope_text = str(scope or "").strip()
        if not scope_text:
            return
        expected = str(expected_path or "").strip()
        current = str(cache.get(scope_text) or "").strip()
        if expected and current != expected:
            return
        cache.pop(scope_text, None)
