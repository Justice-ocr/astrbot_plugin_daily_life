from __future__ import annotations

import json
import math
from typing import Any

from ..clock import today as life_today

from typing import Protocol


class DomainArchiveComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _conn: Any
    _dedupe_chore_definitions: Any
    _domain_json: Any
    _domain_row: Any
    _domain_unified_timeline: Any
    _domain_value: Any
    _flag: Any
    _get_domain_rows: Any
    _int: Any
    _pantry_quantity: Any
    _pantry_row: Any
    _run_db: Any
    _save_domain_action_record: Any
    _text: Any
    get_domain_snapshot: Any


class DomainArchiveComponent:
    """持久化活动、饮食、家务、运动和出行记录。"""

    def __init__(self, context: DomainArchiveComponentContext) -> None:
        self.context = context

    _PANTRY_QUANTITY_DECIMALS = 6
    _PANTRY_ZERO_EPSILON = 1e-9

    @classmethod
    def _pantry_quantity(cls, value: Any) -> float:
        number = float(value or 0)
        if not math.isfinite(number):
            raise ValueError("库存数量必须是有限数字")
        normalized = round(number, cls._PANTRY_QUANTITY_DECIMALS)
        return 0.0 if abs(normalized) <= cls._PANTRY_ZERO_EPSILON else normalized

    @classmethod
    def _pantry_row(cls, row: Any) -> dict[str, Any]:
        result = dict(row)
        result["quantity"] = cls._pantry_quantity(result.get("quantity"))
        result["minimum_quantity"] = max(
            0.0,
            cls._pantry_quantity(result.get("minimum_quantity")),
        )
        return result

    @staticmethod
    def _domain_json(value: Any, fallback: Any) -> str:
        payload = value if value is not None else fallback
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _domain_value(value: Any, fallback: Any) -> Any:
        try:
            parsed = json.loads(str(value or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
        return parsed

    @classmethod
    def _domain_row(cls, row: Any) -> dict[str, Any]:
        result = dict(row)
        for key in (
            "evidence_json",
            "metadata_json",
            "ingredients_json",
            "tags_json",
        ):
            if key in result:
                target = key.removesuffix("_json")
                default = {} if key == "metadata_json" else []
                result[target] = cls._domain_value(result.pop(key), default)
        return result

    async def upsert_activity_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        action_id = self.context._text(payload.get("action_id"))
        if not action_id:
            raise ValueError("活动会话缺少动作编号")

        def dbwork() -> dict[str, Any]:
            self.context._conn.execute(
                """
                INSERT INTO activity_sessions(
                    action_id, date, scope, activity_type, title, status,
                    started_at, ended_at, last_heartbeat_at, duration_seconds,
                    source, evidence_json, metadata_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(action_id) DO UPDATE SET
                    date = excluded.date,
                    scope = excluded.scope,
                    activity_type = excluded.activity_type,
                    title = excluded.title,
                    status = excluded.status,
                    started_at = COALESCE(NULLIF(activity_sessions.started_at, ''), excluded.started_at),
                    ended_at = excluded.ended_at,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    duration_seconds = excluded.duration_seconds,
                    source = excluded.source,
                    evidence_json = excluded.evidence_json,
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    action_id,
                    self.context._text(payload.get("date")),
                    self.context._text(payload.get("scope")) or "global",
                    self.context._text(payload.get("activity_type")),
                    self.context._text(payload.get("title")),
                    self.context._text(payload.get("status")) or "active",
                    self.context._text(payload.get("started_at")),
                    self.context._text(payload.get("ended_at")),
                    self.context._text(payload.get("last_heartbeat_at")),
                    max(0, self.context._int(payload.get("duration_seconds"))),
                    self.context._text(payload.get("source")) or "daily_plan",
                    self.context._domain_json(payload.get("evidence"), []),
                    self.context._domain_json(payload.get("metadata"), {}),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM activity_sessions WHERE action_id = ?", (action_id,)
            ).fetchone()
            return self.context._domain_row(row)

        return await self.context._run_db(dbwork)

    async def get_activity_sessions(
        self, *, limit: int = 20, status: str = ""
    ) -> list[dict[str, Any]]:
        def dbwork() -> list[dict[str, Any]]:
            params: list[Any] = []
            sql = "SELECT * FROM activity_sessions"
            if status:
                sql += " WHERE status = ?"
                params.append(self.context._text(status))
            sql += " ORDER BY COALESCE(NULLIF(ended_at, ''), started_at) DESC, id DESC"
            if limit > 0:
                sql += " LIMIT ?"
                params.append(int(limit))
            return [
                self.context._domain_row(row)
                for row in self.context._conn.execute(sql, tuple(params)).fetchall()
            ]

        return await self.context._run_db(dbwork)

    async def reconcile_activity_sessions(
        self,
        date: str,
        active_action_ids: set[str],
    ) -> int:
        """移除同日日程替换后不再有效的未完成活动会话。"""

        date_text = self.context._text(date)
        keep_ids = sorted(
            {
                self.context._text(action_id)
                for action_id in active_action_ids
                if self.context._text(action_id)
            }
        )
        if not date_text:
            return 0

        def dbwork() -> int:
            params: list[Any] = [date_text]
            sql = """
                DELETE FROM activity_sessions
                WHERE date = ?
                  AND source = 'daily_plan'
                  AND status IN ('planned', 'active')
            """
            if keep_ids:
                placeholders = ", ".join("?" for _ in keep_ids)
                sql += f" AND action_id NOT IN ({placeholders})"
                params.extend(keep_ids)
            sql += """
                  AND NOT EXISTS (
                      SELECT 1
                      FROM life_action_receipts receipt
                      WHERE receipt.action_id = activity_sessions.action_id
                        AND receipt.status IN ('confirmed', 'failed', 'cancelled')
                  )
            """
            cursor = self.context._conn.execute(sql, tuple(params))
            self.context._conn.commit()
            return max(0, int(cursor.rowcount or 0))

        return await self.context._run_db(dbwork)

    async def upsert_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        origin = self.context._text(payload.get("origin_name"))
        destination = self.context._text(payload.get("destination_name"))
        mode = self.context._text(payload.get("travel_mode")) or "walking"
        if not origin or not destination:
            raise ValueError("路线缓存缺少起点或终点")

        def dbwork() -> dict[str, Any]:
            self.context._conn.execute(
                """
                INSERT INTO route_cache(
                    origin_name, destination_name, travel_mode, distance_meters,
                    duration_seconds, provider, travel_detail, confidence,
                    fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin_name, destination_name, travel_mode) DO UPDATE SET
                    distance_meters = excluded.distance_meters,
                    duration_seconds = excluded.duration_seconds,
                    provider = excluded.provider,
                    travel_detail = excluded.travel_detail,
                    confidence = excluded.confidence,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
                """,
                (
                    origin,
                    destination,
                    mode,
                    float(payload.get("distance_meters") or 0),
                    max(0, self.context._int(payload.get("duration_seconds"))),
                    self.context._text(payload.get("provider")) or "fallback",
                    self.context._text(payload.get("travel_detail")),
                    max(0.0, min(1.0, float(payload.get("confidence") or 0))),
                    self.context._text(payload.get("fetched_at")),
                    self.context._text(payload.get("expires_at")),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                """
                SELECT * FROM route_cache
                WHERE origin_name = ? AND destination_name = ? AND travel_mode = ?
                """,
                (origin, destination, mode),
            ).fetchone()
            return dict(row)

        return await self.context._run_db(dbwork)

    async def get_route(
        self, origin: str, destination: str, mode: str = "walking"
    ) -> dict[str, Any] | None:
        def dbwork() -> dict[str, Any] | None:
            row = self.context._conn.execute(
                """
                SELECT * FROM route_cache
                WHERE origin_name = ? AND destination_name = ? AND travel_mode = ?
                """,
                (self.context._text(origin), self.context._text(destination), self.context._text(mode)),
            ).fetchone()
            return dict(row) if row else None

        return await self.context._run_db(dbwork)

    async def delete_routes_for_place(self, name: str) -> int:
        """删除起点或终点包含指定地点的路线缓存。"""

        place_name = self.context._text(name)
        if not place_name:
            return 0

        def dbwork() -> int:
            cursor = self.context._conn.execute(
                """
                DELETE FROM route_cache
                WHERE origin_name = ? OR destination_name = ?
                """,
                (place_name, place_name),
            )
            self.context._conn.commit()
            return max(0, int(cursor.rowcount or 0))

        return await self.context._run_db(dbwork)

    async def reset_residence_context(
        self,
        *,
        changed_at: str,
        week_id: str = "",
    ) -> None:
        """重置只对当前居住地有效的索引，同时保留历史每日记录。"""

        boundary = self.context._text(changed_at)
        current_week = self.context._text(week_id)

        def dbwork() -> None:
            self.context._conn.execute("BEGIN IMMEDIATE")
            try:
                self.context._conn.execute("DELETE FROM route_cache")
                self.context._conn.execute("DELETE FROM places")
                if current_week:
                    self.context._conn.execute(
                        "DELETE FROM week_plans WHERE week_id = ?",
                        (current_week,),
                    )
                self.context._conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("residence_context_changed_at", boundary),
                )
                self.context._conn.commit()
            except Exception:
                self.context._conn.rollback()
                raise

        await self.context._run_db(dbwork)

    async def get_residence_context_boundary(self) -> str:
        """返回最近一次居住地变化时间。"""

        def dbwork() -> str:
            row = self.context._conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                ("residence_context_changed_at",),
            ).fetchone()
            return self.context._text(row["value"] if row else "")

        return await self.context._run_db(dbwork)

    async def update_place_coordinates(
        self,
        name: str,
        latitude: float,
        longitude: float,
        *,
        source: str = "manual",
        updated_at: str = "",
    ) -> bool:
        def dbwork() -> bool:
            self.context._conn.execute(
                """
                INSERT INTO places(
                    name, latitude, longitude, coordinate_source,
                    coordinate_updated_at, source
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    coordinate_source = excluded.coordinate_source,
                    coordinate_updated_at = excluded.coordinate_updated_at
                """,
                (
                    self.context._text(name),
                    float(latitude),
                    float(longitude),
                    self.context._text(source),
                    self.context._text(updated_at),
                    self.context._text(source) or "manual",
                ),
            )
            self.context._conn.commit()
            return True

        return await self.context._run_db(dbwork)

    async def upsert_recipe(self, payload: dict[str, Any]) -> dict[str, Any]:
        recipe_id = self.context._text(payload.get("id"))
        name = self.context._text(payload.get("name"))
        if not recipe_id or not name:
            raise ValueError("食谱缺少编号或名称")

        def dbwork() -> dict[str, Any]:
            self.context._conn.execute(
                """
                INSERT INTO recipes(id, name, meal_type, ingredients_json, tags_json, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    meal_type = excluded.meal_type,
                    ingredients_json = excluded.ingredients_json,
                    tags_json = excluded.tags_json,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    recipe_id,
                    name,
                    self.context._text(payload.get("meal_type")),
                    self.context._domain_json(payload.get("ingredients"), []),
                    self.context._domain_json(payload.get("tags"), []),
                    self.context._text(payload.get("source")) or "manual",
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
            ).fetchone()
            return self.context._domain_row(row)

        return await self.context._run_db(dbwork)

    async def get_recipes(self, limit: int = 20) -> list[dict[str, Any]]:
        def dbwork() -> list[dict[str, Any]]:
            sql = "SELECT * FROM recipes ORDER BY updated_at DESC, name"
            params: tuple[Any, ...] = ()
            if limit > 0:
                sql += " LIMIT ?"
                params = (int(limit),)
            return [
                self.context._domain_row(row)
                for row in self.context._conn.execute(sql, params).fetchall()
            ]

        return await self.context._run_db(dbwork)

    async def adjust_pantry_item(
        self,
        name: str,
        delta: float,
        *,
        unit: str = "",
        minimum_quantity: float = 0,
        expires_at: str = "",
        reason: str = "",
        action_id: str = "",
        occurred_at: str = "",
        source: str = "life_action",
    ) -> dict[str, Any]:
        item_name = self.context._text(name)
        if not item_name:
            raise ValueError("库存物品名称不能为空")

        def dbwork() -> dict[str, Any]:
            row = self.context._conn.execute(
                "SELECT * FROM pantry_items WHERE name = ?", (item_name,)
            ).fetchone()
            current = self.context._pantry_quantity(row["quantity"] if row else 0)
            movement = self.context._pantry_quantity(delta)
            updated = max(0.0, self.context._pantry_quantity(current + movement))
            stored_unit = self.context._text(unit) or (self.context._text(row["unit"]) if row else "")
            stored_minimum = max(0.0, self.context._pantry_quantity(minimum_quantity))
            if row and stored_minimum == 0:
                stored_minimum = max(
                    0.0,
                    self.context._pantry_quantity(row["minimum_quantity"]),
                )
            stored_expiry = self.context._text(expires_at) or (
                self.context._text(row["expires_at"]) if row else ""
            )
            stored_source = self.context._text(source) or "life_action"
            if updated > 0:
                self.context._conn.execute(
                    """
                    INSERT INTO pantry_items(
                        name, quantity, unit, minimum_quantity, expires_at, source, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(name) DO UPDATE SET
                        quantity = excluded.quantity,
                        unit = excluded.unit,
                        minimum_quantity = excluded.minimum_quantity,
                        expires_at = excluded.expires_at,
                        source = excluded.source,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        item_name,
                        updated,
                        stored_unit,
                        stored_minimum,
                        stored_expiry,
                        stored_source,
                    ),
                )
            else:
                self.context._conn.execute(
                    "DELETE FROM pantry_items WHERE name = ?",
                    (item_name,),
                )
            self.context._conn.execute(
                """
                INSERT INTO pantry_movements(
                    item_name, delta, unit, reason, action_id, occurred_at, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_name,
                    movement,
                    stored_unit,
                    self.context._text(reason),
                    self.context._text(action_id),
                    self.context._text(occurred_at),
                    stored_source,
                ),
            )
            self.context._conn.commit()
            saved = self.context._conn.execute(
                "SELECT * FROM pantry_items WHERE name = ?", (item_name,)
            ).fetchone()
            if saved:
                return self.context._pantry_row(saved)
            return {
                "name": item_name,
                "quantity": 0.0,
                "unit": stored_unit,
                "minimum_quantity": stored_minimum,
                "expires_at": stored_expiry,
                "source": stored_source,
            }

        return await self.context._run_db(dbwork)

    async def get_pantry_items(self, limit: int = 50) -> list[dict[str, Any]]:
        def dbwork() -> list[dict[str, Any]]:
            rows = [
                self.context._pantry_row(row)
                for row in self.context._conn.execute(
                    "SELECT * FROM pantry_items "
                    "ORDER BY expires_at, updated_at DESC, name"
                ).fetchall()
            ]
            visible = [row for row in rows if row["quantity"] > 0]
            return visible[: int(limit)] if limit > 0 else visible

        return await self.context._run_db(dbwork)

    async def save_meal_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.context._save_domain_action_record(
            "meal_records",
            payload,
            fields=(
                "date",
                "meal_type",
                "name",
                "recipe_id",
                "status",
                "ingredients_json",
                "place",
                "source",
                "evidence_json",
                "occurred_at",
            ),
            json_fields={
                "ingredients_json": "ingredients",
                "evidence_json": "evidence",
            },
        )

    async def upsert_chore(self, payload: dict[str, Any]) -> dict[str, Any]:
        chore_id = self.context._text(payload.get("id"))
        name = self.context._text(payload.get("name"))
        if not chore_id or not name:
            raise ValueError("家务事项缺少编号或名称")

        def dbwork() -> dict[str, Any]:
            self.context._conn.execute(
                """
                INSERT INTO chores(
                    id, name, cadence_days, effort, last_completed_at,
                    next_due_at, enabled, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    cadence_days = excluded.cadence_days,
                    effort = excluded.effort,
                    last_completed_at = excluded.last_completed_at,
                    next_due_at = excluded.next_due_at,
                    enabled = excluded.enabled,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chore_id,
                    name,
                    max(0, self.context._int(payload.get("cadence_days"))),
                    max(1, min(5, self.context._int(payload.get("effort"), 1))),
                    self.context._text(payload.get("last_completed_at")),
                    self.context._text(payload.get("next_due_at")),
                    self.context._flag(payload.get("enabled", True)),
                    self.context._text(payload.get("source")) or "daily_plan",
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM chores WHERE id = ?", (chore_id,)
            ).fetchone()
            return dict(row)

        return await self.context._run_db(dbwork)

    async def save_chore_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.context._save_domain_action_record(
            "chore_records",
            payload,
            fields=(
                "chore_id",
                "name",
                "status",
                "duration_minutes",
                "evidence_json",
                "occurred_at",
                "source",
            ),
            json_fields={"evidence_json": "evidence"},
        )

    async def save_fitness_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.context._save_domain_action_record(
            "fitness_records",
            payload,
            fields=(
                "date",
                "activity",
                "duration_minutes",
                "intensity",
                "load_score",
                "status",
                "source",
                "evidence_json",
                "occurred_at",
            ),
            json_fields={"evidence_json": "evidence"},
        )

    async def has_domain_action_record(self, action_type: str, action_id: str) -> bool:
        """检查可派生领域副作用是否已经完成持久化。"""

        normalized_type = self.context._text(action_type).lower()
        table = {
            "meal": "meal_records",
            "cook": "meal_records",
            "order_food": "meal_records",
            "purchase": "pantry_movements",
            "chore": "chore_records",
            "exercise": "fitness_records",
        }.get(normalized_type)
        normalized_id = self.context._text(action_id)
        if not table or not normalized_id:
            return False

        def dbwork() -> bool:
            row = self.context._conn.execute(
                f"SELECT 1 FROM {table} WHERE action_id = ? LIMIT 1",
                (normalized_id,),
            ).fetchone()
            return row is not None

        return await self.context._run_db(dbwork)

    async def _save_domain_action_record(
        self,
        table: str,
        payload: dict[str, Any],
        *,
        fields: tuple[str, ...],
        json_fields: dict[str, str],
    ) -> dict[str, Any]:
        action_id = self.context._text(payload.get("action_id"))
        if not action_id:
            raise ValueError("领域记录缺少动作编号")

        def dbwork() -> dict[str, Any]:
            values: list[Any] = []
            for field in fields:
                if field in json_fields:
                    values.append(
                        self.context._domain_json(payload.get(json_fields[field]), [])
                    )
                else:
                    values.append(payload.get(field, ""))
            columns = ", ".join(("action_id", *fields))
            placeholders = ", ".join("?" for _ in range(len(fields) + 1))
            assignments = ", ".join(f"{field} = excluded.{field}" for field in fields)
            self.context._conn.execute(
                f"""
                INSERT INTO {table}({columns}) VALUES ({placeholders})
                ON CONFLICT(action_id) DO UPDATE SET {assignments}
                """,
                (action_id, *values),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                f"SELECT * FROM {table} WHERE action_id = ?", (action_id,)
            ).fetchone()
            return self.context._domain_row(row)

        return await self.context._run_db(dbwork)

    async def get_chores(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.context._get_domain_rows(
            "chores", "enabled DESC, next_due_at, updated_at DESC", limit
        )

    async def get_chore_records(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.context._get_domain_rows(
            "chore_records", "occurred_at DESC, id DESC", limit
        )

    async def get_meal_records(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.context._get_domain_rows(
            "meal_records", "date DESC, occurred_at DESC, id DESC", limit
        )

    async def get_fitness_records(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self.context._get_domain_rows(
            "fitness_records", "date DESC, occurred_at DESC, id DESC", limit
        )

    async def _get_domain_rows(
        self, table: str, order_by: str, limit: int
    ) -> list[dict[str, Any]]:
        def dbwork() -> list[dict[str, Any]]:
            sql = f"SELECT * FROM {table} ORDER BY {order_by}"
            params: tuple[Any, ...] = ()
            if limit > 0:
                sql += " LIMIT ?"
                params = (int(limit),)
            return [
                self.context._domain_row(row)
                for row in self.context._conn.execute(sql, params).fetchall()
            ]

        return await self.context._run_db(dbwork)

    async def save_conversation_action_item(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        commitment_id = max(0, self.context._int(payload.get("commitment_id")))
        title = self.context._text(payload.get("title"))
        if not commitment_id or not title:
            raise ValueError("会话行动项缺少承诺编号或标题")

        def dbwork() -> dict[str, Any]:
            self.context._conn.execute(
                """
                INSERT INTO conversation_action_items(
                    commitment_id, title, owner, due_at, status, source_session,
                    source_message, evidence_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(commitment_id) DO UPDATE SET
                    title = excluded.title,
                    owner = excluded.owner,
                    due_at = excluded.due_at,
                    status = excluded.status,
                    source_session = excluded.source_session,
                    source_message = excluded.source_message,
                    evidence_json = excluded.evidence_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    commitment_id,
                    title,
                    self.context._text(payload.get("owner")),
                    self.context._text(payload.get("due_at")),
                    self.context._text(payload.get("status")) or "open",
                    self.context._text(payload.get("source_session")),
                    self.context._text(payload.get("source_message")),
                    self.context._domain_json(payload.get("evidence"), []),
                ),
            )
            self.context._conn.commit()
            row = self.context._conn.execute(
                "SELECT * FROM conversation_action_items WHERE commitment_id = ?",
                (commitment_id,),
            ).fetchone()
            return self.context._domain_row(row)

        return await self.context._run_db(dbwork)

    async def get_conversation_action_items(
        self, limit: int = 20
    ) -> list[dict[str, Any]]:
        return await self.context._get_domain_rows(
            "conversation_action_items", "status, due_at, id DESC", limit
        )

    async def get_unified_life_timeline(self, limit: int = 60) -> list[dict[str, Any]]:
        snapshot = await self.context.get_domain_snapshot(limit=max(0, int(limit)))
        return list(snapshot.get("timeline", []))[: max(0, int(limit))]

    async def get_domain_snapshot(self, limit: int = 20) -> dict[str, Any]:
        """在一次数据库锁定中读取生活领域面板数据。"""

        def dbwork() -> dict[str, Any]:
            def rows(table: str, order_by: str, row_limit: int) -> list[dict[str, Any]]:
                sql = f"SELECT * FROM {table} ORDER BY {order_by}"
                params: tuple[Any, ...] = ()
                if row_limit > 0:
                    sql += " LIMIT ?"
                    params = (int(row_limit),)
                return [
                    self.context._domain_row(row)
                    for row in self.context._conn.execute(sql, params).fetchall()
                ]

            planned_action_ids: dict[str, set[str]] = {
                self.context._text(row["date"]): set()
                for row in self.context._conn.execute("SELECT date FROM days").fetchall()
                if self.context._text(row["date"])
            }
            for row in self.context._conn.execute(
                "SELECT date, value FROM day_meta WHERE key = 'planned_life_actions'"
            ).fetchall():
                date_text = self.context._text(row["date"])
                values = self.context._domain_value(row["value"], [])
                if not date_text or not isinstance(values, list):
                    continue
                planned_action_ids[date_text] = {
                    self.context._text(item.get("action_id"))
                    for item in values
                    if isinstance(item, dict) and self.context._text(item.get("action_id"))
                }
            trusted_action_ids = {
                self.context._text(row["action_id"])
                for row in self.context._conn.execute(
                    """
                    SELECT DISTINCT action_id
                    FROM life_action_receipts
                    WHERE status IN ('confirmed', 'failed', 'cancelled')
                    """
                ).fetchall()
                if self.context._text(row["action_id"])
            }
            current_date = life_today().isoformat()
            superseded_action_ids = {
                self.context._text(row["action_id"])
                for row in self.context._conn.execute(
                    """
                    SELECT action_id, date
                    FROM activity_sessions
                    WHERE source = 'daily_plan'
                    """
                ).fetchall()
                if self.context._text(row["date"]) in planned_action_ids
                and self.context._text(row["action_id"])
                not in planned_action_ids[self.context._text(row["date"])]
                and self.context._text(row["action_id"]) not in trusted_action_ids
            }
            candidate_limit = max(int(limit) * 4, 80) if limit > 0 else 0

            def visible_limit(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return items[: int(limit)] if limit > 0 else items

            def is_visible_plan_record(
                item: dict[str, Any],
                *,
                date_value: str,
                action_key: str = "action_id",
            ) -> bool:
                action_id = self.context._text(item.get(action_key))
                date_text = self.context._text(date_value)[:10]
                if (
                    not action_id
                    or not date_text
                    or date_text not in planned_action_ids
                ):
                    return True
                if (
                    action_id in planned_action_ids[date_text]
                    or action_id in trusted_action_ids
                ):
                    return not (
                        date_text < current_date
                        and action_id not in trusted_action_ids
                        and self.context._text(item.get("status"))
                        in {"expired", "skipped", "failed"}
                    )
                return self.context._text(item.get("source")) not in {
                    "daily_plan",
                    "life_action_simulation",
                    "timeline_simulation",
                }

            activity_sessions = visible_limit(
                [
                    item
                    for item in rows(
                        "activity_sessions",
                        "COALESCE(NULLIF(ended_at, ''), started_at) DESC, id DESC",
                        candidate_limit,
                    )
                    if is_visible_plan_record(item, date_value=item.get("date", ""))
                ]
            )
            meals = visible_limit(
                [
                    item
                    for item in rows(
                        "meal_records",
                        "date DESC, occurred_at DESC, id DESC",
                        candidate_limit,
                    )
                    if is_visible_plan_record(item, date_value=item.get("date", ""))
                ]
            )
            chore_records = visible_limit(
                [
                    item
                    for item in rows(
                        "chore_records",
                        "occurred_at DESC, id DESC",
                        candidate_limit,
                    )
                    if is_visible_plan_record(
                        item, date_value=self.context._text(item.get("occurred_at"))[:10]
                    )
                ]
            )
            fitness = visible_limit(
                [
                    item
                    for item in rows(
                        "fitness_records",
                        "date DESC, occurred_at DESC, id DESC",
                        candidate_limit,
                    )
                    if is_visible_plan_record(item, date_value=item.get("date", ""))
                ]
            )
            chores = self.context._dedupe_chore_definitions(
                [
                    item
                    for item in rows(
                        "chores",
                        "enabled DESC, next_due_at, updated_at DESC",
                        candidate_limit,
                    )
                    if not (
                        self.context._text(item.get("source")) == "life_action_simulation"
                        and self.context._text(item.get("id")) in superseded_action_ids
                    )
                ]
            )
            chores = visible_limit(chores)

            pantry = [
                self.context._pantry_row(item)
                for item in rows(
                    "pantry_items", "expires_at, updated_at DESC, name", 0
                )
            ]
            pantry = visible_limit(
                [item for item in pantry if item["quantity"] > 0]
            )

            return {
                "activity_sessions": activity_sessions,
                "pantry": pantry,
                "recipes": rows("recipes", "updated_at DESC, name", limit),
                "meals": meals,
                "chores": chores,
                "chore_records": chore_records,
                "fitness": fitness,
                "conversation_actions": rows(
                    "conversation_action_items", "status, due_at, id DESC", limit
                ),
            }

        snapshot = await self.context._run_db(dbwork)
        snapshot["timeline"] = self.context._domain_unified_timeline(snapshot, max(limit, 40))
        return snapshot

    @staticmethod
    def _dedupe_chore_definitions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按名称合并历史家务定义，保留当前有效且最近更新的一条。"""

        selected: dict[str, dict[str, Any]] = {}

        def preference(item: dict[str, Any]) -> tuple[int, str, str, str]:
            enabled = str(item.get("enabled") or "").strip().lower()
            enabled_score = int(enabled in {"1", "true", "yes", "on"})
            return (
                enabled_score,
                str(item.get("updated_at") or ""),
                str(item.get("last_completed_at") or ""),
                str(item.get("id") or ""),
            )

        for item in items:
            name = " ".join(str(item.get("name") or "").split()).strip()
            key = name.casefold() or f"id:{item.get('id') or len(selected)}"
            current = selected.get(key)
            if current is None or preference(item) > preference(current):
                selected[key] = item
        return list(selected.values())

    @staticmethod
    def _domain_unified_timeline(
        snapshot: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        represented_action_ids: set[str] = set()
        for kind, key, title_key in (
            ("meal", "meals", "name"),
            ("chore", "chore_records", "name"),
            ("fitness", "fitness", "activity"),
            ("conversation_action", "conversation_actions", "title"),
        ):
            for item in snapshot.get(key, []):
                action_id = str(item.get("action_id") or "").strip()
                if action_id and action_id in represented_action_ids:
                    continue
                values.append(
                    {
                        "kind": kind,
                        "title": item.get(title_key),
                        "status": item.get("status"),
                        "date": item.get("date")
                        or str(
                            item.get("occurred_at")
                            or item.get("due_at")
                            or item.get("created_at")
                            or ""
                        )[:10],
                        "occurred_at": item.get("occurred_at")
                        or item.get("due_at")
                        or item.get("created_at"),
                        "source": item.get("source") or "conversation",
                        "action_id": action_id,
                        "details": item,
                    }
                )
                if action_id:
                    represented_action_ids.add(action_id)
        for item in snapshot.get("activity_sessions", []):
            action_id = str(item.get("action_id") or "").strip()
            if action_id and action_id in represented_action_ids:
                continue
            values.append(
                {
                    "kind": "activity",
                    "title": item.get("title") or item.get("activity_type"),
                    "status": item.get("status"),
                    # 结束时间通常是日结算时间，不代表活动实际发生的日期。
                    # 总览按开始时间排序和筛选，避免昨天的记录在次日结算时重复出现。
                    "date": item.get("date"),
                    "occurred_at": item.get("started_at") or item.get("ended_at"),
                    "source": item.get("source"),
                    "action_id": action_id,
                    "details": item.get("metadata", {}),
                }
            )
            if action_id:
                represented_action_ids.add(action_id)
        values.sort(key=lambda item: str(item.get("occurred_at") or ""), reverse=True)
        return values[: max(0, int(limit))]


__all__ = ["DomainArchiveComponent"]
