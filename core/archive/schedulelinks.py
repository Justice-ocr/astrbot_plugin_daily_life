from __future__ import annotations

import datetime
import json
import sqlite3
from typing import Any

from ..clock import local_datetime
from ..clock import now as life_now

_CLOSED_TIMELINE_STATES = {
    "elapsed", "completed", "expired", "skipped", "cancelled",
}


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _linked_indexes(
    conn: sqlite3.Connection,
    commitment: sqlite3.Row,
    timeline: list[dict],
    actions: Any,
) -> set[int]:
    """只使用结构化关联；已有联系任务中的编辑记录也是排程证据。"""
    commitment_id = str(commitment["id"])
    indexes = set()
    found = False
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        payload = action.get("payload")
        if not isinstance(payload, dict):
            continue
        ids = payload.get("commitment_ids")
        if str(payload.get("schedule_commitment_id")) != commitment_id and not (
            isinstance(ids, list) and commitment_id in {str(value) for value in ids}
        ):
            continue
        found = True
        index = action.get("timeline_index")
        if type(index) is not int or not 0 <= index < len(timeline):
            return set()
        indexes.add(index)
    if found:
        return indexes

    task = conn.execute(
        "SELECT payload_json FROM durable_tasks WHERE task_key = ?",
        (f"invite_contact:{commitment_id}",),
    ).fetchone()
    payload = _json(task["payload_json"], {}) if task else {}
    if not isinstance(payload, dict) or (
        str(payload.get("commitment_id")) != commitment_id
        or payload.get("activity_date") != commitment["date"]
        or payload.get("scope") != commitment["source_session"]
    ):
        return set()
    edits = payload.get("timeline_edits")
    for edit in edits if isinstance(edits, list) else []:
        if not isinstance(edit, dict) or edit.get("operation") not in {"insert", "replace"}:
            continue
        item = edit.get("item")
        if not isinstance(item, dict) or not item.get("activity") or not item.get("time"):
            return set()
        matches = [
            index for index, current in enumerate(timeline)
            if current["time"] == item["time"]
            and current["activity"] == item["activity"]
            and (current["day_offset"] or 0) == (item.get("day_offset") or 0)
        ]
        # 节点被改约或替换后，旧编辑不能用于收束新安排。
        if len(matches) != 1:
            return set()
        indexes.add(matches[0])
    return indexes


def reconcile_scheduled_commitments(
    conn: sqlite3.Connection, *, now: datetime.datetime | None = None
) -> int:
    """收束关联日程已结束的普通约定，不以记录年龄推测履约或过期。"""
    now = local_datetime(now) if now is not None else life_now()
    if now is None:
        return 0
    rows = conn.execute(
        """SELECT c.*, MAX(d.date) AS date FROM commitments c
        JOIN day_commitments d ON d.commitment_id = c.id
        WHERE c.status = 'scheduled' AND c.kind = 'plan'
          AND c.media_kind IN ('', 'none')
        GROUP BY c.id"""
    ).fetchall()
    days: dict[str, tuple[list[dict], Any]] = {}
    changes = 0
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for commitment in rows:
            date = commitment["date"]
            if date > now.date().isoformat() or (
                commitment["trigger_date"] and commitment["trigger_date"] != date
            ):
                continue
            due = local_datetime(f"{date} {commitment['trigger_time']}")
            if due is not None and due > now:
                continue
            if date not in days:
                timeline = [dict(row) for row in conn.execute(
                    "SELECT * FROM timelines WHERE date = ? ORDER BY sort_order", (date,)
                ).fetchall()]
                meta = conn.execute(
                    "SELECT value FROM day_meta WHERE date = ? AND key = 'planned_life_actions'",
                    (date,),
                ).fetchone()
                days[date] = (timeline, _json(meta["value"], []) if meta else [])
            timeline, actions = days[date]
            indexes = _linked_indexes(conn, commitment, timeline, actions)
            if not indexes or any(
                timeline[index]["execution_state"] not in _CLOSED_TIMELINE_STATES
                for index in indexes
            ):
                continue
            status = "cancelled" if all(
                timeline[index]["execution_state"] == "cancelled" for index in indexes
            ) else "expired"
            cursor = conn.execute(
                """UPDATE commitments SET status = ?, completed_at = ?
                WHERE id = ? AND status = 'scheduled'""",
                (status, now.strftime("%Y-%m-%d %H:%M:%S"), commitment["id"]),
            )
            changes += max(0, cursor.rowcount)
        # 后台提炼可能晚于约定结算写入行动项，以权威约定的终态为准。
        cursor = conn.execute(
            """UPDATE conversation_action_items AS a
            SET status = (SELECT CASE c.status
                WHEN 'delivery_failed' THEN 'failed' ELSE c.status END
                FROM commitments c WHERE c.id = a.commitment_id),
                updated_at = CURRENT_TIMESTAMP
            WHERE a.status IN ('open', 'pending', 'active') AND EXISTS (
                SELECT 1 FROM commitments c WHERE c.id = a.commitment_id
                AND c.status IN ('done', 'cancelled', 'expired', 'delivery_failed'))"""
        )
        changes += max(0, cursor.rowcount)
    return changes
