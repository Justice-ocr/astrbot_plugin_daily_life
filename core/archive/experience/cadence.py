from __future__ import annotations

import copy
import datetime
import json
import sqlite3
from dataclasses import replace
from typing import Any

from ...models import PhysiologicalRhythmLogRecord
from ...models.primitive import optional_bool
from ...models.vitals import normalize_body_burden_intensity

from typing import Protocol


class PhysiologicalRhythmArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _RHYTHM_LIFECYCLE_DAYS: Any
    _RHYTHM_SINGLE_ACTIVE_LIFECYCLES: Any
    _compact_active_lifecycle_rhythm_logs: Any
    _compose_physiological_rhythm_log: Any
    _conn: Any
    _invalidate_physiological_rhythm_trend_cache: Any
    _latest_active_rhythm_logs: Any
    _loads_texts: Any
    _parse_rhythm_time: Any
    _physiological_rhythm_trend_cache: Any
    _physiological_rhythm_trend_cache_key: Any
    _physiological_rhythm_trend_revision: Any
    _rhythm_lifecycle_kind: Any
    _rhythm_log_status: Any
    _rhythm_now_text: Any
    _rhythm_score: Any
    _rhythm_weight: Any
    _run_db: Any
    _text: Any


class PhysiologicalRhythmArchiveComponent:
    def __init__(self, context: PhysiologicalRhythmArchiveComponentContext) -> None:
        self.context = context

    _RHYTHM_SINGLE_ACTIVE_LIFECYCLES = {"short_term", "sustained"}
    _RHYTHM_LIFECYCLE_DAYS = {
        "transient": 1,
        "short_term": 3,
        "sustained": 7,
    }

    @staticmethod
    def _rhythm_now_text() -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _invalidate_physiological_rhythm_trend_cache(self) -> None:
        self.context._physiological_rhythm_trend_revision = (
            int(getattr(self.context, "_physiological_rhythm_trend_revision", 0) or 0) + 1
        )
        self.context._physiological_rhythm_trend_cache = {}

    def _physiological_rhythm_trend_cache_key(
        self,
        *,
        days: int,
        limit: int,
        now: datetime.datetime,
    ) -> str:
        revision = int(getattr(self.context, "_physiological_rhythm_trend_revision", 0) or 0)
        return f"{now.date().isoformat()}:{max(days, 1)}:{max(limit, 1)}:{revision}"

    @staticmethod
    def _parse_rhythm_time(value: Any) -> datetime.datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        for candidate in (text, text.replace("T", " ")):
            for fmt, size in (
                ("%Y-%m-%d %H:%M:%S", 19),
                ("%Y-%m-%d %H:%M", 16),
                ("%Y-%m-%d", 10),
            ):
                try:
                    return datetime.datetime.strptime(candidate[:size], fmt)
                except ValueError:
                    continue
        try:
            return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            return None

    @staticmethod
    def _rhythm_score(value: Any, default: int = 0) -> int:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            number = default
        return max(0, min(number, 100))

    @staticmethod
    def _loads_texts(value: Any) -> list[str]:
        if isinstance(value, list):
            raw_items = value
        else:
            text = str(value or "").strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = []
            raw_items = parsed if isinstance(parsed, list) else []
        result: list[str] = []
        for item in raw_items:
            text = " ".join(str(item or "").split())[:50]
            if text and text not in result:
                result.append(text)
            if len(result) >= 6:
                break
        return result

    def _compose_physiological_rhythm_log(
        self, row: sqlite3.Row
    ) -> PhysiologicalRhythmLogRecord:
        body_intensity = normalize_body_burden_intensity(
            row["body_intensity"],
            burden_present=row["body_burden_present"],
        )
        lifecycle_kind = self.context._text(row["lifecycle_kind"]) or "transient"
        if (
            lifecycle_kind in self.context._RHYTHM_SINGLE_ACTIVE_LIFECYCLES
            and body_intensity < 20
            and self.context._rhythm_score(row["social_battery"], 50) > 40
            and not bool(row["optional_cycle_enabled"])
        ):
            lifecycle_kind = "transient"
        return PhysiologicalRhythmLogRecord(
            id=int(row["id"] or 0),
            date=self.context._text(row["date"]),
            source=self.context._text(row["source"]) or "state",
            energy_curve=self.context._text(row["energy_curve"]),
            body_label=self.context._text(row["body_label"]),
            body_intensity=body_intensity,
            body_burden_present=optional_bool(row["body_burden_present"]),
            body_source=self.context._text(row["body_source"]),
            body_expires_at=self.context._text(row["body_expires_at"]),
            recovery_actions=self.context._loads_texts(row["recovery_actions"]),
            social_battery=self.context._rhythm_score(row["social_battery"], 50),
            attention_state=self.context._text(row["attention_state"]),
            optional_cycle_enabled=bool(row["optional_cycle_enabled"]),
            optional_cycle_label=self.context._text(row["optional_cycle_label"]),
            optional_cycle_intensity=self.context._rhythm_score(
                row["optional_cycle_intensity"]
            ),
            optional_cycle_source=self.context._text(row["optional_cycle_source"]),
            summary=self.context._text(row["summary"]),
            lifecycle_kind=lifecycle_kind,
            weight=max(0.0, min(float(row["weight"] or 0.0), 3.0)),
            status=self.context._text(row["status"]) or "active",
            created_at=self.context._text(row["created_at"]),
            updated_at=self.context._text(row["updated_at"]),
        )

    def _rhythm_log_status(
        self,
        item: PhysiologicalRhythmLogRecord,
        now: datetime.datetime | None = None,
    ) -> PhysiologicalRhythmLogRecord:
        if item.status != "active":
            return item
        now = now or datetime.datetime.now()
        days = self.context._RHYTHM_LIFECYCLE_DAYS.get(item.lifecycle_kind, 1)
        anchor = self.context._parse_rhythm_time(item.date) or self.context._parse_rhythm_time(
            item.updated_at
        )
        if anchor and anchor + datetime.timedelta(days=days) < now:
            return replace(item, status="expired", weight=0.0)
        return item

    @staticmethod
    def _rhythm_lifecycle_kind(
        item: PhysiologicalRhythmLogRecord, recent: list[PhysiologicalRhythmLogRecord]
    ) -> str:
        if item.lifecycle_kind == "sustained":
            return item.lifecycle_kind
        repeated_body = (
            item.body_intensity >= 20
            and bool(item.body_label)
            and sum(
                1
                for log in recent
                if log.body_label
                and log.body_label == item.body_label
                and log.body_intensity >= 20
            )
            >= 2
        )
        repeated_low_social = (
            item.social_battery <= 40
            and sum(1 for log in recent if log.social_battery <= 40) >= 2
        )
        repeated_body_load = (
            item.body_intensity >= 35
            and sum(1 for log in recent if log.body_intensity >= 35) >= 2
        )
        if repeated_body or repeated_low_social or repeated_body_load:
            return "sustained"
        if (
            item.body_intensity >= 35
            or item.social_battery <= 40
            or item.recovery_actions
            or item.optional_cycle_enabled
        ):
            return "short_term"
        return "transient"

    @staticmethod
    def _rhythm_weight(kind: str) -> float:
        return {
            "transient": 0.6,
            "short_term": 1.0,
            "sustained": 1.5,
        }.get(kind, 0.6)

    @classmethod
    def _latest_active_rhythm_logs(
        cls,
        items: list[PhysiologicalRhythmLogRecord],
    ) -> list[PhysiologicalRhythmLogRecord]:
        seen: set[str] = set()
        result: list[PhysiologicalRhythmLogRecord] = []
        for item in items:
            kind = item.lifecycle_kind
            if item.status == "active" and kind in cls._RHYTHM_SINGLE_ACTIVE_LIFECYCLES:
                if kind in seen:
                    continue
                seen.add(kind)
            result.append(item)
        return result

    def _compact_active_lifecycle_rhythm_logs(self) -> int:
        rows = self.context._conn.execute(
            """
            SELECT id, lifecycle_kind FROM physiological_rhythm_logs
            WHERE status = 'active'
              AND lifecycle_kind IN ('short_term', 'sustained')
            ORDER BY lifecycle_kind ASC, date DESC, updated_at DESC, id DESC
            """
        ).fetchall()
        keep: set[str] = set()
        expired_ids: list[int] = []
        for row in rows:
            kind = self.context._text(row["lifecycle_kind"])
            rhythm_id = int(row["id"] or 0)
            if not kind or rhythm_id <= 0:
                continue
            if kind in keep:
                expired_ids.append(rhythm_id)
            else:
                keep.add(kind)
        if not expired_ids:
            return 0
        placeholders = ",".join("?" for _ in expired_ids)
        cursor = self.context._conn.execute(
            f"""
            UPDATE physiological_rhythm_logs
            SET status = 'superseded',
                weight = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE status = 'active'
              AND id IN ({placeholders})
            """,
            tuple(expired_ids),
        )
        return int(cursor.rowcount or 0)

    async def save_physiological_rhythm_log(
        self,
        record: PhysiologicalRhythmLogRecord | dict,
    ) -> PhysiologicalRhythmLogRecord | None:
        item = PhysiologicalRhythmLogRecord.from_value(
            record.as_dict()
            if isinstance(record, PhysiologicalRhythmLogRecord)
            else record
        )
        if not item:
            return None
        now_text = self.context._rhythm_now_text()
        if not item.created_at:
            item.created_at = now_text
        if not item.updated_at:
            item.updated_at = now_text

        def dbwork():
            recent_rows = self.context._conn.execute(
                """
                SELECT * FROM physiological_rhythm_logs
                WHERE date >= date(?, '-6 days')
                ORDER BY date DESC, updated_at DESC, id DESC
                LIMIT 12
                """,
                (self.context._text(item.date),),
            ).fetchall()
            recent = [
                self.context._compose_physiological_rhythm_log(row) for row in recent_rows
            ]
            item.lifecycle_kind = self.context._rhythm_lifecycle_kind(item, recent)
            item.weight = self.context._rhythm_weight(item.lifecycle_kind)

            existing = None
            if item.id:
                existing = self.context._conn.execute(
                    "SELECT * FROM physiological_rhythm_logs WHERE id = ?",
                    (int(item.id),),
                ).fetchone()
            if existing is None:
                existing = self.context._conn.execute(
                    """
                    SELECT * FROM physiological_rhythm_logs
                    WHERE date = ?
                      AND source = ?
                      AND energy_curve = ?
                      AND body_label = ?
                      AND attention_state = ?
                      AND summary = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        self.context._text(item.date),
                        self.context._text(item.source) or "state",
                        self.context._text(item.energy_curve),
                        self.context._text(item.body_label),
                        self.context._text(item.attention_state),
                        self.context._text(item.summary),
                    ),
                ).fetchone()
            values = (
                self.context._text(item.date),
                self.context._text(item.source) or "state",
                self.context._text(item.energy_curve),
                self.context._text(item.body_label),
                self.context._rhythm_score(item.body_intensity),
                (
                    1
                    if item.body_burden_present is True
                    else 0
                    if item.body_burden_present is False
                    else None
                ),
                self.context._text(item.body_source),
                self.context._text(item.body_expires_at),
                json.dumps(list(item.recovery_actions or [])[:6], ensure_ascii=False),
                self.context._rhythm_score(item.social_battery, 50),
                self.context._text(item.attention_state),
                1 if item.optional_cycle_enabled else 0,
                self.context._text(item.optional_cycle_label),
                self.context._rhythm_score(item.optional_cycle_intensity),
                self.context._text(item.optional_cycle_source),
                self.context._text(item.summary),
                self.context._text(item.lifecycle_kind) or "transient",
                float(item.weight or 0.0),
                self.context._text(item.status) or "active",
            )
            if existing:
                rhythm_id = int(existing["id"] or item.id or 0)
                self.context._conn.execute(
                    """
                    UPDATE physiological_rhythm_logs
                    SET date = ?, source = ?, energy_curve = ?, body_label = ?,
                        body_intensity = ?, body_burden_present = ?,
                        body_source = ?, body_expires_at = ?,
                        recovery_actions = ?, social_battery = ?, attention_state = ?,
                        optional_cycle_enabled = ?, optional_cycle_label = ?,
                        optional_cycle_intensity = ?, optional_cycle_source = ?,
                        summary = ?, lifecycle_kind = ?, weight = ?, status = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (*values, rhythm_id),
                )
                self.context._compact_active_lifecycle_rhythm_logs()
                self.context._conn.commit()
                self.context._invalidate_physiological_rhythm_trend_cache()
                row = self.context._conn.execute(
                    "SELECT * FROM physiological_rhythm_logs WHERE id = ?",
                    (rhythm_id,),
                ).fetchone()
                return self.context._compose_physiological_rhythm_log(row) if row else None

            cursor = self.context._conn.execute(
                """
                INSERT INTO physiological_rhythm_logs(
                    date, source, energy_curve, body_label, body_intensity,
                    body_burden_present, body_source, body_expires_at,
                    recovery_actions, social_battery,
                    attention_state, optional_cycle_enabled, optional_cycle_label,
                    optional_cycle_intensity, optional_cycle_source, summary,
                    lifecycle_kind, weight, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                values,
            )
            rhythm_id = int(cursor.lastrowid or 0)
            self.context._compact_active_lifecycle_rhythm_logs()
            self.context._conn.commit()
            self.context._invalidate_physiological_rhythm_trend_cache()
            row = self.context._conn.execute(
                "SELECT * FROM physiological_rhythm_logs WHERE id = ?",
                (rhythm_id,),
            ).fetchone()
            return self.context._compose_physiological_rhythm_log(row) if row else None

        return await self.context._run_db(dbwork)

    async def get_physiological_rhythm_logs(
        self,
        limit: int = 20,
        *,
        date: str = "",
        active_only: bool = True,
    ) -> list[PhysiologicalRhythmLogRecord]:
        def dbwork():
            sql = "SELECT * FROM physiological_rhythm_logs"
            clauses = []
            params: list[Any] = []
            if date:
                clauses.append("date = ?")
                params.append(self.context._text(date))
            if active_only:
                clauses.append("status = 'active'")
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY date DESC, updated_at DESC, id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.context._conn.execute(sql, tuple(params)).fetchall()
            now = datetime.datetime.now()
            items = [
                self.context._rhythm_log_status(
                    self.context._compose_physiological_rhythm_log(row), now
                )
                for row in rows
            ]
            if active_only:
                items = [item for item in items if item.status == "active"]
                items = self.context._latest_active_rhythm_logs(items)
            return items

        return await self.context._run_db(dbwork)

    async def get_physiological_rhythm_trend(
        self, days: int = 7, *, limit: int = 20
    ) -> dict[str, Any]:
        days = max(int(days or 7), 1)
        limit = max(int(limit or 20), 1)
        now = datetime.datetime.now()
        cutoff = (now.date() - datetime.timedelta(days=days - 1)).strftime("%Y-%m-%d")
        cache_key = self.context._physiological_rhythm_trend_cache_key(
            days=days, limit=limit, now=now
        )

        def dbwork():
            cache = getattr(self.context, "_physiological_rhythm_trend_cache", None)
            if not isinstance(cache, dict):
                self.context._physiological_rhythm_trend_cache = {}
                cache = self.context._physiological_rhythm_trend_cache
            if cache_key in cache:
                return copy.deepcopy(cache[cache_key])

            # 短期和持续节律在同一业务日可以同时有效。按日期只取一条会
            # 把其中一类误删，导致趋势摘要看不到另一条有效证据。短期/
            # 持续按生命周期各取最新一条，瞬时记录仍按日期取最新一条。
            rows = self.context._conn.execute(
                """
                SELECT *
                FROM physiological_rhythm_logs
                WHERE date >= ? AND status = 'active'
                ORDER BY date DESC, updated_at DESC, id DESC
                """,
                (cutoff,),
            ).fetchall()
            selected_rows: list[sqlite3.Row] = []
            seen_lifecycles: set[str] = set()
            seen_transient_dates: set[str] = set()
            for row in rows:
                lifecycle = self.context._text(row["lifecycle_kind"]) or "transient"
                date_text = self.context._text(row["date"])
                if lifecycle in self.context._RHYTHM_SINGLE_ACTIVE_LIFECYCLES:
                    if lifecycle in seen_lifecycles:
                        continue
                    seen_lifecycles.add(lifecycle)
                else:
                    if date_text in seen_transient_dates:
                        continue
                    seen_transient_dates.add(date_text)
                selected_rows.append(row)
                if len(selected_rows) >= limit:
                    break
            rows = selected_rows
            logs = [
                item
                for item in (
                    self.context._rhythm_log_status(
                        self.context._compose_physiological_rhythm_log(row), now
                    )
                    for row in rows
                )
                if item.status == "active"
            ]
            logs = self.context._latest_active_rhythm_logs(logs)

            if not logs:
                result = {
                    "days": days,
                    "logs": [],
                    "summary": "",
                    "updated_at": "",
                    "trend_cache_key": cache_key,
                }
                cache[cache_key] = copy.deepcopy(result)
                return result

            avg_body = round(sum(item.body_intensity for item in logs) / len(logs), 1)
            avg_social = round(sum(item.social_battery for item in logs) / len(logs), 1)
            body_labels: dict[str, int] = {}
            cycle_labels: dict[str, int] = {}
            for item in logs:
                if item.body_burden_present is True and item.body_label:
                    body_labels[item.body_label] = (
                        body_labels.get(item.body_label, 0) + 1
                    )
                if item.optional_cycle_enabled and item.optional_cycle_label:
                    cycle_labels[item.optional_cycle_label] = (
                        cycle_labels.get(item.optional_cycle_label, 0) + 1
                    )
            top_body = sorted(
                body_labels.items(), key=lambda pair: (-pair[1], pair[0])
            )[:3]
            top_cycle = sorted(
                cycle_labels.items(), key=lambda pair: (-pair[1], pair[0])
            )[:3]
            high_body_days = len(
                {item.date for item in logs if item.body_intensity >= 35}
            )
            low_social_days = len(
                {item.date for item in logs if item.social_battery <= 40}
            )
            sustained = [item for item in logs if item.lifecycle_kind == "sustained"]
            parts = [
                f"近{days}天平均身体负荷 {avg_body:g}/100",
                f"平均社交电量 {avg_social:g}/100",
            ]
            if top_body:
                parts.append(
                    "常见状态："
                    + "、".join(f"{label}×{count}" for label, count in top_body)
                )
            if high_body_days:
                parts.append(f"身体负荷偏高 {high_body_days} 天")
            if low_social_days:
                parts.append(f"社交电量偏低 {low_social_days} 天")
            if sustained:
                parts.append(
                    "有持续节律："
                    + "、".join(
                        item.summary or item.body_label
                        for item in sustained[:2]
                        if item.summary or item.body_label
                    )
                )
            result = {
                "days": days,
                "logs": [item.as_dict() for item in logs[:limit]],
                "average_body_intensity": avg_body,
                "average_social_battery": avg_social,
                "high_body_days": high_body_days,
                "low_social_days": low_social_days,
                "body_labels": [
                    {"label": label, "count": count} for label, count in top_body
                ],
                "cycle_labels": [
                    {"label": label, "count": count} for label, count in top_cycle
                ],
                "summary": "；".join(part for part in parts if part),
                "updated_at": max(
                    (item.updated_at for item in logs if item.updated_at), default=""
                ),
                "trend_cache_key": cache_key,
            }
            cache[cache_key] = copy.deepcopy(result)
            return result

        return await self.context._run_db(dbwork)
