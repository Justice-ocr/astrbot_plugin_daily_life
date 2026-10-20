from __future__ import annotations

import datetime
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

from ..models import DayRecord
from ..clock import local_datetime


def score(value: Any, default: float = 50.0) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return default
    return round(max(0.0, min(100.0, number)), 4) if math.isfinite(number) else default


def instant(value: Any) -> datetime.datetime | None:
    return local_datetime(value)


def nonnegative(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return default
    return max(0.0, number) if math.isfinite(number) else default


@dataclass(slots=True)
class ContinuousBody:
    energy: float = 65.0
    sleep_pressure: float = 30.0
    fatigue: float = 20.0
    hunger: float = 25.0
    thirst: float = 20.0
    social_battery: float = 65.0
    updated_at: str = ""
    uncertain_minutes: float = 0.0
    discomfort: float = 0.0

    @classmethod
    def from_value(cls, raw: Any, day: DayRecord | None = None) -> ContinuousBody:
        raw = raw if isinstance(raw, dict) else {}
        state = day.state if day else None
        return cls(
            energy=score(raw.get("energy"), score(getattr(state, "energy", None), 65)),
            sleep_pressure=score(
                raw.get("sleep_pressure"), score(getattr(state, "sleepiness", None), 30)
            ),
            fatigue=score(raw.get("fatigue"), 20),
            hunger=score(raw.get("hunger"), 25),
            thirst=score(raw.get("thirst"), 20),
            social_battery=score(
                raw.get("social_battery"),
                score(getattr(state, "interaction_capacity", None), 65),
            ),
            updated_at=str(raw.get("updated_at") or ""),
            uncertain_minutes=nonnegative(raw.get("uncertain_minutes")),
            discomfort=score(raw.get("discomfort"), 0),
        )

    def sync_condition(self, day: DayRecord) -> None:
        """采用已保存的不适负担，不根据身体数值诊断或自动宣布痊愈。"""
        if day.state is None:
            return
        condition = day.state.physiological_rhythm.body_condition
        self.discomfort = (
            score(condition.intensity, 0)
            if condition.burden_present and condition.phase != "resolved"
            else 0.0
        )

    @property
    def attention_capacity(self) -> float:
        return score(100 - self.fatigue * 0.3 - self.sleep_pressure * 0.25
                     - self.discomfort * 0.4)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def advance(
        self,
        minutes: float,
        *,
        activity: str = "idle",
        sleeping: bool = False,
        intensity: float = 1.0,
    ) -> None:
        minutes = nonnegative(minutes)
        effort = max(1.0, min(5.0, nonnegative(intensity, 1)))
        resting = activity == "rest"
        demanding = activity in {
            "work",
            "study",
            "exercise",
            "chore",
            "cook",
            "move",
            "travel",
        }
        energy_rate = (
            0.0
            if sleeping or resting
            else -0.065 * effort
            if demanding
            else -0.015
        )
        fatigue_rate = (
            0.0
            if sleeping or resting
            else 0.055 * effort
            if demanding
            else 0.006
        )
        social_rate = (
            -0.08
            if activity in {"chat", "social"}
            else 0.0
        )
        # 数值仅模拟角色的活动余力；持续不适增加活动负担、放慢恢复。
        burden = self.discomfort / 100
        # 恢复逐渐趋缓，避免睡眠和空闲无限累加成满分；解析积分与轮询频率无关。
        if sleeping or resting:
            recovery = (1 - burden * 0.45) * minutes
            ceiling, floor, tau = (92.0, 4.0, 180.0) if sleeping else (82.0, 12.0, 240.0)
            if self.energy < ceiling:
                self.energy = ceiling - (ceiling - self.energy) * math.exp(-recovery / tau)
            if self.fatigue > floor:
                self.fatigue = floor + (self.fatigue - floor) * math.exp(-recovery / tau)
        if activity not in {"chat", "social"}:
            ceiling = 90.0 if sleeping else 85.0 if resting else 75.0
            if self.social_battery < ceiling:
                self.social_battery = ceiling - (ceiling - self.social_battery) * math.exp(-minutes / 240)
        energy_rate *= 1 + burden if energy_rate < 0 else 1 - burden * 0.45
        fatigue_rate *= 1 + burden if fatigue_rate > 0 else 1 - burden * 0.45
        if activity in {"chat", "social"}:
            social_rate *= 1 + burden * 0.5
        for field, rate in (
            ("energy", energy_rate),
            ("sleep_pressure", -0.17 if sleeping else 0.065),
            ("fatigue", fatigue_rate),
            ("hunger", 0.03 if sleeping else 0.08),
            (
                "thirst",
                0.03 if sleeping else 0.10 + (0.035 * effort if demanding else 0),
            ),
            ("social_battery", social_rate),
        ):
            setattr(self, field, score(getattr(self, field) + rate * minutes))

    def complete(self, action_type: str, payload: dict[str, Any]) -> None:
        if action_type == "meal":
            self.hunger = score(self.hunger - 65)
            self.energy = score(self.energy + min(8, max(0, 100 - self.energy) / 8))
        if action_type == "drink":
            volume = min(1000, nonnegative(payload.get("volume_ml"), 250))
            self.thirst = score(self.thirst - min(80, volume * 0.26))


def project_body(day: DayRecord, body: ContinuousBody) -> None:
    if day.state is not None:
        day.state.energy = round(body.energy)
        day.state.sleepiness = round(body.sleep_pressure)
        day.state.interaction_capacity = round(min(body.social_battery, body.attention_capacity))
        day.state.physiological_rhythm.social_battery = round(body.social_battery)
    day.meta["continuous_body"] = json.dumps(body.as_dict(), ensure_ascii=False)


def body_context(raw: Any) -> str:
    if not isinstance(raw, dict) or not raw.get("updated_at"):
        return ""
    body = ContinuousBody.from_value(raw)
    return (
        f"持续身体需求（0-100）：体力 {body.energy:.0f}；困倦 {body.sleep_pressure:.0f}；"
        f"疲劳 {body.fatigue:.0f}；饥饿 {body.hunger:.0f}；口渴 {body.thirst:.0f}；"
        f"社交电量 {body.social_battery:.0f}；注意余力 {body.attention_capacity:.0f}；"
        f"不适负担 {body.discomfort:.0f}。这些是时间和已执行动作的累积结果，"
        "只用于自然判断当前感受与行动，不是疾病或必须向用户汇报的内容。"
    )
