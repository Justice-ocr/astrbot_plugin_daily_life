from __future__ import annotations

from dataclasses import dataclass

from .cast import as_bool, as_int, as_str


@dataclass(slots=True)
class BiliBrowseSettings:
    enabled: bool = True
    natural_link_reply: bool = True
    provider: str = ""
    discovery_enabled: bool = True
    max_daily_minutes: int = 90
    audience_enabled: bool = True
    enhanced_vision: bool = True
    favorites_enabled: bool = True
    comments_enabled: bool = True
    dm_enabled: bool = True
    live_enabled: bool = True
    dynamics_enabled: bool = True
    max_daily_interactions: int = 12
    max_live_minutes: int = 20

    @staticmethod
    def from_dict(data):
        data = data if isinstance(data, dict) else {}
        return BiliBrowseSettings(
            enabled=as_bool(data.get("enabled", True), True),
            natural_link_reply=as_bool(data.get("natural_link_reply", True), True),
            provider=as_str(data.get("provider", "")).strip(),
            discovery_enabled=as_bool(data.get("discovery_enabled", True), True),
            max_daily_minutes=as_int(data.get("max_daily_minutes", 90), 90, 5, 240),
            audience_enabled=as_bool(data.get("audience_enabled", True), True),
            enhanced_vision=as_bool(data.get("enhanced_vision", True), True),
            favorites_enabled=as_bool(data.get("favorites_enabled", True), True),
            comments_enabled=as_bool(data.get("comments_enabled", True), True),
            dm_enabled=as_bool(data.get("dm_enabled", True), True),
            live_enabled=as_bool(data.get("live_enabled", True), True),
            dynamics_enabled=as_bool(data.get("dynamics_enabled", True), True),
            max_daily_interactions=as_int(data.get("max_daily_interactions", 12), 12, 1, 60),
            max_live_minutes=as_int(data.get("max_live_minutes", 20), 20, 1, 90),
        )
