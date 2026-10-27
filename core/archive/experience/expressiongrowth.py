"""表达经验的事件归档；观察、实际采用与反馈分别计数。"""

from __future__ import annotations

import json
from typing import Any

from ...models import ExpressionProfileRecord
from ...models.phrasing import expression_revision
from ...clock import local_datetime


def learning_value(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


async def apply_expression_learning(archive: Any, profile: ExpressionProfileRecord, event: dict[str, Any]) -> ExpressionProfileRecord | None:
    """原子保存经验和来源；同一证据重试不累计学习强度。"""
    item = ExpressionProfileRecord.from_value(profile)
    key = str(event.get("event_key") or "").strip()
    kind = str(event.get("kind") or "")
    outcome = str(event.get("outcome") or "neutral")
    if not item or not item.scope or not item.label or not key:
        return None
    if kind not in {"observation", "explicit", "exposure", "application", "feedback", "assessment"}:
        return None
    if outcome not in {"positive", "neutral", "negative"}:
        return None
    breadth = str(item.learning.get("breadth") or "situation")
    if breadth not in {"voice", "relationship", "situation"}:
        return None
    if breadth == "voice" and (item.scope != "self" or item.profile_id):
        return None
    source_scope = str(event.get("source_scope") or item.scope)
    evidence = event.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        return None

    def write():
        conn = archive._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            if item.id:
                row = conn.execute(
                    "SELECT * FROM expression_profiles WHERE id=? AND scope=? AND profile_id=?",
                    (item.id, item.scope, item.profile_id),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
            else:
                row = conn.execute(
                    "SELECT * FROM expression_profiles WHERE scope=? AND profile_id=? AND label=?",
                    (item.scope, item.profile_id, item.label),
                ).fetchone()
            if row is None:
                if kind in {"exposure", "application", "feedback", "assessment"}:
                    conn.rollback()
                    return None
                count = conn.execute(
                    "SELECT COUNT(*) FROM expression_profiles WHERE scope=? AND profile_id=?",
                    (item.scope, item.profile_id),
                ).fetchone()[0]
                if count >= 24 and kind == "observation":
                    conn.rollback()
                    return None
                cursor = conn.execute(
                    """INSERT INTO expression_profiles
                    (scope,profile_id,label,tone,habits,avoid,evidence,confidence,source,learning_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (item.scope, item.profile_id, item.label, item.tone,
                     "\n".join(item.habits), "\n".join(item.avoid), item.evidence,
                     min(item.confidence, 0.65) if kind == "observation" else item.confidence,
                     item.source, json.dumps({"breadth": breadth, "status": "emerging"}, ensure_ascii=False)),
                )
                row = conn.execute("SELECT * FROM expression_profiles WHERE id=?", (cursor.lastrowid,)).fetchone()
            profile_id = int(row["id"])
            if kind in {"exposure", "application", "feedback", "assessment"}:
                saved = archive._compose_expression_profile(row)
                current_revision = expression_revision(saved)
                if evidence.get("profile_revision") != current_revision:
                    conn.rollback()
                    return None
            insertion = conn.execute(
                """INSERT OR IGNORE INTO expression_learning_events
                (profile_id,event_key,kind,outcome,source_scope,evidence_json) VALUES (?,?,?,?,?,?)""",
                (profile_id, key, kind, outcome, source_scope, json.dumps(evidence, ensure_ascii=False)),
            )
            if not insertion.rowcount:
                conn.commit()
                return archive._compose_expression_profile(row)
            meta = learning_value(row["learning_json"])
            if kind == "exposure":
                meta["last_exposed_event"] = int(insertion.lastrowid)
            established = not learning_value(row["learning_json"]) or bool(meta.get("established"))
            existing_breadth = str(meta.get("breadth") or "situation")
            # 应用和反馈只能评价已有经验，不能顺带改写内容或扩大适用范围。
            if kind in {"observation", "explicit"}:
                if existing_breadth != breadth and meta:
                    conn.rollback()
                    return None
                meta["breadth"] = breadth
                changed = expression_revision(item) != expression_revision(archive._compose_expression_profile(row))
                revising = changed and meta.get("status") == "cooling"
                if kind == "explicit" or revising or (not established and not meta.get("observations")):
                    if changed:
                        meta["epoch_event_id"] = int(insertion.lastrowid)
                        established = False
                    meta["applicability"] = str(item.learning.get("applicability") or "")[:200]
                    conn.execute(
                        """UPDATE expression_profiles SET tone=?,habits=?,avoid=?,evidence=?,
                        confidence=?,source=? WHERE id=?""",
                        (item.tone, "\n".join(item.habits), "\n".join(item.avoid), item.evidence,
                         item.confidence if kind == "explicit" else min(item.confidence, 0.65),
                         item.source, profile_id),
                    )
            epoch = int(meta.get("epoch_event_id") or 0)
            stats = conn.execute(
                """SELECT kind,outcome,COUNT(*) AS n FROM expression_learning_events
                WHERE profile_id=? AND id>=? GROUP BY kind,outcome""", (profile_id, epoch),
            ).fetchall()
            counts = {(r["kind"], r["outcome"]): int(r["n"]) for r in stats}
            positive = counts.get(("feedback", "positive"), 0)
            negative = counts.get(("feedback", "negative"), 0)
            explicit = sum(n for (k, _), n in counts.items() if k == "explicit")
            contexts = conn.execute(
                """SELECT COUNT(DISTINCT source_scope || ':' || COALESCE(json_extract(evidence_json,'$.context'),'')) FROM expression_learning_events
                WHERE profile_id=? AND id>=? AND kind='feedback' AND outcome='positive'""", (profile_id, epoch),
            ).fetchone()[0]
            recent = conn.execute(
                """SELECT kind,outcome FROM expression_learning_events
                WHERE profile_id=? AND id>=? AND (kind='explicit' OR (kind='feedback' AND outcome<>'neutral'))
                ORDER BY id DESC LIMIT 3""", (profile_id, epoch),
            ).fetchall()
            recent_negative = sum(r["kind"] == "feedback" and r["outcome"] == "negative" for r in recent)
            stable = established or explicit > 0 or (positive >= 3 and (meta.get("breadth") != "voice" or contexts >= 2))
            status = "cooling" if recent_negative >= 2 else "stable" if stable else "emerging"
            if recent and recent[0]["kind"] == "explicit":
                status = "stable"
            meta.update(
                established=established,
                status=status,
                observations=sum(n for (k, _), n in counts.items() if k == "observation"),
                applications=sum(n for (k, _), n in counts.items() if k == "application"),
                exposures=sum(n for (k, _), n in counts.items() if k == "exposure"),
                positive=positive, negative=negative, feedback_contexts=int(contexts),
            )
            # 展示、采用和中性评估不刷新风格的形成时间，避免召回自我强化。
            touch = kind in {"observation", "explicit"} or (kind == "feedback" and outcome != "neutral")
            conn.execute(
                "UPDATE expression_profiles SET learning_json=?" +
                (",updated_at=CURRENT_TIMESTAMP" if touch else "") + " WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), profile_id),
            )
            result = conn.execute("SELECT * FROM expression_profiles WHERE id=?", (profile_id,)).fetchone()
            conn.execute("UPDATE long_term_memories SET status='superseded' WHERE source_table='expression_profiles' AND source_id=? AND status='active'", (str(profile_id),))
            conn.commit()
            return archive._compose_expression_profile(result)
        except Exception:
            conn.rollback()
            raise

    return await archive._run_db(write)


async def recover_expression_watch(archive: Any, *, scope: str, max_age_seconds: int = 1800) -> dict[str, Any] | None:
    """只恢复最近一轮尚未结算的观察，不让旧回复的反馈落到新表达上。"""
    def read():
        conn = archive._conn
        latest = conn.execute(
            """SELECT event_key FROM expression_learning_events
            WHERE kind='exposure' AND source_scope=?
              AND created_at>=datetime('now', ?) ORDER BY id DESC LIMIT 1""",
            (scope, f"-{max(1, int(max_age_seconds))} seconds"),
        ).fetchone()
        if latest is None:
            return None
        rows = conn.execute(
            "SELECT * FROM expression_learning_events WHERE event_key=? AND source_scope=? AND kind='exposure' ORDER BY id LIMIT 5",
            (latest["event_key"], scope),
        ).fetchall()
        watch = None
        for row in rows:
            evidence = learning_value(row["evidence_json"])
            token = str(evidence.get("reply_token") or "")
            sent_at = local_datetime(evidence.get("sent_at"))
            if not token or sent_at is None:
                continue
            settled = conn.execute(
                "SELECT 1 FROM expression_learning_events WHERE profile_id=? AND event_key=?",
                (row["profile_id"], f"assessed:{scope}:{token}"),
            ).fetchone()
            if settled:
                continue
            saved = conn.execute("SELECT * FROM expression_profiles WHERE id=?", (row["profile_id"],)).fetchone()
            if saved is None:
                continue
            profile = archive._compose_expression_profile(saved)
            if expression_revision(profile) != evidence.get("profile_revision"):
                continue
            if watch is None:
                watch = {
                    "sent_at": sent_at, "reply_token": token, "target_scope": scope,
                    "target_message_id": str(evidence.get("target_message_id") or ""),
                    "reply_text": str(evidence.get("reply_text") or ""),
                    "source": str(evidence.get("source") or "regular_reply"),
                    "expression_profiles": [],
                }
            payload = profile.as_dict()
            payload["evidence"] = ""
            watch["expression_profiles"].append(payload)
        if watch:
            effect = conn.execute("SELECT * FROM reply_effects WHERE scope=? ORDER BY id DESC LIMIT 1", (scope,)).fetchone()
            if effect is None or effect["outcome"] != "pending" or effect["reply_text"] != watch["reply_text"]:
                return None
            watch["reply_effect_id"] = int(effect["id"])
        return watch

    return await archive._run_db(read)


async def get_expression_context(archive: Any, *, scope: str, profile_id: str = "", limit: int = 16) -> list[ExpressionProfileRecord]:
    """个人风格与当前关系共同参与；不读取其他人的私聊偏好。"""
    def read():
        clauses = ["(scope='self' AND profile_id='')"]
        params: list[Any] = []
        if scope and scope != "self":
            if profile_id:
                clauses.append("(scope=? AND (profile_id='' OR profile_id=?))")
                params.extend((scope, profile_id))
            else:
                clauses.append("scope=?")
                params.append(scope)
        rows = archive._conn.execute(
            "SELECT * FROM expression_profiles WHERE " + " OR ".join(clauses) +
            " ORDER BY updated_at DESC,confidence DESC,id DESC", tuple(params),
        ).fetchall()
        items = [archive._compose_expression_profile(row) for row in rows]
        # 每层预留候选空间，避免新场景经验挤掉共同语言风格。
        groups = {"voice": [], "relationship": [], "situation": []}
        for item in items:
            if item.learning.get("status") == "cooling":
                continue
            groups.get(item.learning.get("breadth", "situation"), groups["situation"]).append(item)
        result = []
        while any(groups.values()) and len(result) < max(0, limit):
            for group in groups.values():
                if group and len(result) < max(0, limit):
                    result.append(group.pop(0))
        return result

    return await archive._run_db(read)
