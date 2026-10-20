from __future__ import annotations

from typing import Any


from . import persona as _part_capture_persona_component
from .tune import packet as _part_packet_component
from .tune import target as _part_target_component
from typing import Protocol


class CaptureRelationComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _calibrate_relationship_profile: Any
    _list_payload: Any
    _relationship_contact_payload: Any
    _str_payload: Any
    archive: Any


class CaptureRelationComponent:
    def __init__(self, context: CaptureRelationComponentContext) -> None:
        self.context = context

    def _subjective_impression_data(self, payload: dict) -> dict[str, Any]:
        impression = payload.get("subjective_impression")
        impression = impression if isinstance(impression, dict) else {}
        return {
            "subjective_name": self.context._str_payload(impression.get("subjective_name")),
            "subjective_tags": [
                self.context._str_payload(item)
                for item in self.context._list_payload(
                    impression.get("tags") or impression.get("subjective_tags")
                )
                if self.context._str_payload(item)
            ][:8],
            "relationship_story": self.context._str_payload(
                impression.get("relationship_story")
            ),
            "note": self.context._str_payload(impression.get("impression_delta")),
        }

    async def _save_subjective_impression(
        self,
        payload: dict,
        meta: dict[str, str],
        *,
        note_fallback: str = "",
        source: str = "chat_impression",
        persona_hint: str = "",
    ) -> None:
        visibility = (
            payload.get("visibility")
            if isinstance(payload.get("visibility"), dict)
            else {}
        )
        level = self.context._str_payload(visibility.get("level")).lower()
        if level in {"unseen", "missed"}:
            return

        impression_data = self._subjective_impression_data(payload)
        note = impression_data["note"] or note_fallback
        if not any(
            [
                note,
                impression_data["subjective_name"],
                impression_data["subjective_tags"],
                impression_data["relationship_story"],
            ]
        ):
            return

        await self.context.archive.touch_relationship(
            meta["sender_profile_id"],
            name=meta["sender_name"],
            note=note,
            date_str=meta["date"],
            source=source,
            platform=meta["platform"],
            user_id=meta["user_id"],
            alias=meta["sender_name"],
            persona_hint=persona_hint,
            subjective_name=impression_data["subjective_name"],
            subjective_tags=impression_data["subjective_tags"],
            relationship_story=impression_data["relationship_story"],
            **self.context._relationship_contact_payload(meta),
        )
        await self.context._calibrate_relationship_profile(
            meta["sender_profile_id"], persona_hint, meta["date"]
        )

    _PERSONA_HINT_CACHE_LIMIT = _part_capture_persona_component.CapturePersonaComponent._PERSONA_HINT_CACHE_LIMIT

    @staticmethod
    def _persona_hint_cache_text(value: _part_capture_persona_component.Any) -> str:
        return _part_capture_persona_component.CapturePersonaComponent._persona_hint_cache_text(value)

    _MEMORY_PAYLOAD_CALIBRATION_KEYS = _part_packet_component.PacketComponent._MEMORY_PAYLOAD_CALIBRATION_KEYS

    _MEMORY_TARGET_CALIBRATION_KEYS = _part_target_component.TargetComponent._MEMORY_TARGET_CALIBRATION_KEYS

