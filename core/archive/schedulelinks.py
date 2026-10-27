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

# 这里只解释结构化记录已经声明的时段边界，不从行动文案补造时间。
_WINDOW_END_HOURS = {
    "dawn": 6, "凌晨": 6,
    "morning": 9, "早晨": 9, "清早": 9, "清晨": 9,
    "forenoon": 12, "上午": 12,
    "早上": 12, "今早": 12, "明早": 12,
    "noon": 14, "中午": 14,
    "afternoon": 18, "下午": 18,
    "daytime": 18, "白天": 18,
    "evening": 20, "傍晚": 20,
    "night": 24, "晚上": 24, "今晚": 24, "明晚": 24,
    "late_night": 24, "深夜": 24,
    "": 24, "今天": 24, "明天": 24, "后天": 24,
}


def _json(value: Any, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _action_window_end(commitment: sqlite3.Row) -> datetime.datetime | None:
    """只解释明确日期及有限时段，不为未知时段猜截止时间。"""
    try:
        date = datetime.date.fromisoformat(commitment["trigger_date"])
    except (TypeError, ValueError):
        return None
    hour = _WINDOW_END_HOURS.get(commitment["time_window"].strip().lower())
    if hour is None:
        return None
    midnight = datetime.datetime.combine(date, datetime.time())
    end = midnight + datetime.timedelta(hours=hour)
    if commitment["trigger_time"]:
        due = local_datetime(f"{date.isoformat()} {commitment['trigger_time']}")
        if due is None:
            return None
        # 精确钟点与粗略时段冲突时保留到当天结束，不提前收束。
        if due >= end:
            end = midnight + datetime.timedelta(days=1)
    return end


def _expire_unqueued_actions(
    conn: sqlite3.Connection, now: datetime.datetime,
    days: dict[str, tuple[list[dict], Any]],
) -> int:
    """有明确有效时段且没有独立执行器的会话行动，时段结束后同步归档。"""
    rows = conn.execute(
        """SELECT c.* FROM commitments c
        WHERE c.status IN ('active', 'pending', 'scheduled') AND c.kind = 'plan'
          AND c.media_kind IN ('', 'none')
          AND c.source IN ('chat', 'chat_batch', 'invite')
          AND c.trigger_date != '' AND c.trigger_date <= ?
          AND c.source_session != ''
          AND EXISTS (SELECT 1 FROM conversation_action_items a
              WHERE a.commitment_id = c.id AND a.status IN ('open', 'pending', 'active'))""",
        (now.date().isoformat(),),
    ).fetchall()
    changes = 0
    for row in rows:
        follow_up = _json(row["follow_up_json"], None)
        if not isinstance(follow_up, dict) or (follow_up and (
            follow_up.get("action") != "none"
            or follow_up.get("execute_at")
            or follow_up.get("expires_at")
        )):
            continue
        end = _action_window_end(row)
        if end is None or now < end:
            continue
        # 关联到某一天不等于仍有执行器；只有改约到未来的日期才能延长当前承诺。
        scheduled = False
        for linked in conn.execute(
            "SELECT date FROM day_commitments WHERE commitment_id = ?", (row["id"],)
        ).fetchall():
            date = linked["date"]
            if date > row["trigger_date"] and date >= now.date().isoformat():
                scheduled = True
                break
            if date == row["trigger_date"] and now.date().isoformat() == row["trigger_date"]:
                timeline, actions = _day_schedule(conn, date, days)
                indexes = _linked_indexes(conn, {**dict(row), "date": date}, timeline, actions)
                # 明确处于进行中的节点仍代表正在发生的生活，不按计划时段中断。
                if any(timeline[index]["execution_state"] == "active" for index in indexes):
                    scheduled = True
                    break
        if scheduled:
            continue
        task = conn.execute(
            """SELECT status FROM durable_tasks WHERE task_key IN (?, ?, ?, ?, ?)
            OR (json_valid(payload_json) AND
                CAST(json_extract(payload_json, '$.commitment_id') AS TEXT) = ?)
            ORDER BY CASE WHEN status IN ('pending', 'leased') THEN 0 ELSE 1 END
            LIMIT 1""",
            (
                f"proactive_commitment:{row['id']}",
                f"commitment_contact_review:{row['id']}",
                f"commitment_photo:{row['id']}",
                f"commitment_video:{row['id']}",
                f"invite_contact:{row['id']}", str(row["id"]),
            ),
        ).fetchone()
        # 普通会话中任何已关联任务都可能仍代表待办；邀请类只等待仍在投递的任务，
        # 已完成的邀约投递不能让当天的生活安排永久停留在“进行中”。
        if task and (
            row["source"] != "invite" or task["status"] in {"pending", "leased"}
        ):
            continue
        cursor = conn.execute(
            """UPDATE commitments SET status = 'expired', completed_at = ?
            WHERE id = ? AND status IN ('active', 'pending', 'scheduled')""",
            (end.strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
        )
        changes += max(0, cursor.rowcount)
        conn.execute(
            """INSERT INTO decision_traces
            (trace_id, scope, stage, reason_code, decision, evidence_json, outcome)
            VALUES (?, ?, 'expired', ?, 'expired', ?, ?)
            ON CONFLICT(trace_id, stage) DO NOTHING""",
            (
                f"commitment:{row['id']}", row["source_session"],
                (
                    "invite_action_window_elapsed" if row["source"] == "invite"
                    else "conversation_action_window_elapsed" if follow_up
                    else "legacy_action_window_elapsed"
                ),
                json.dumps([{
                    "trigger_date": row["trigger_date"],
                    "trigger_time": row["trigger_time"],
                    "time_window": row["time_window"],
                    "window_end": end.isoformat(timespec="seconds"),
                    "observed_at": now.isoformat(timespec="seconds"),
                    "source_message_id": row["source_message_id"],
                }], ensure_ascii=False),
                "会话行动项的约定时段已过，没有关联执行任务；保留历史并标记过期",
            ),
        )
    return changes


def _day_schedule(
    conn: sqlite3.Connection, date: str,
    cache: dict[str, tuple[list[dict], Any]],
) -> tuple[list[dict], Any]:
    if date not in cache:
        timeline = [dict(row) for row in conn.execute(
            "SELECT * FROM timelines WHERE date = ? ORDER BY sort_order", (date,),
        ).fetchall()]
        meta = conn.execute(
            "SELECT value FROM day_meta WHERE date = ? AND key = 'planned_life_actions'",
            (date,),
        ).fetchone()
        cache[date] = (timeline, _json(meta["value"], []) if meta else [])
    return cache[date]


def _linked_indexes(
    conn: sqlite3.Connection,
    commitment: sqlite3.Row | dict[str, Any],
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
    """收束已结束的日程和有效时段已过的行动项，不将时间流逝视为履约。"""
    now = local_datetime(now) if now is not None else life_now()
    if now is None:
        return 0
    rows = conn.execute(
        """SELECT c.*, MAX(d.date) AS date FROM commitments c
        JOIN day_commitments d ON d.commitment_id = c.id
        WHERE c.status IN ('active', 'pending', 'scheduled') AND c.kind = 'plan'
          AND c.media_kind IN ('', 'none')
        GROUP BY c.id"""
    ).fetchall()
    days: dict[str, tuple[list[dict], Any]] = {}
    changes = 0
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for commitment in rows:
            follow_up = _json(commitment["follow_up_json"], {})
            if isinstance(follow_up, dict) and follow_up.get("action") in {"contact_person", "remind_person"}:
                # 日程结束只是生活进展，不能替代明确联系动作的实际投递。
                continue
            contact_task = conn.execute(
                "SELECT 1 FROM durable_tasks WHERE task_key IN (?, ?) AND status IN ('pending', 'leased')",
                (f"proactive_commitment:{commitment['id']}", f"commitment_contact_review:{commitment['id']}"),
            ).fetchone()
            if contact_task:
                continue
            date = commitment["date"]
            if date > now.date().isoformat() or (
                commitment["trigger_date"] and commitment["trigger_date"] != date
            ):
                continue
            due = local_datetime(f"{date} {commitment['trigger_time']}")
            if due is not None and due > now:
                continue
            timeline, actions = _day_schedule(conn, date, days)
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
                WHERE id = ? AND status IN ('active', 'pending', 'scheduled')""",
                (status, now.strftime("%Y-%m-%d %H:%M:%S"), commitment["id"]),
            )
            changes += max(0, cursor.rowcount)
        changes += _expire_unqueued_actions(conn, now, days)
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
