from __future__ import annotations

from typing import Any

from ...models import ExpressionProfileRecord, ExpressionReviewRecord
from ..expressionlearning import finite_confidence

from typing import Protocol


class ExpressionLearningComponentContext(Protocol):
    """此组件读取或更新的宿主接口。"""

    _batch_expression_scope: Any
    _dict_payloads: Any
    _save_experience_evidence: Any
    _save_expression_profile_payloads: Any
    archive: Any


class ExpressionLearningComponent:
    """在已有后台记忆批次中提炼有消息证据的表达习惯。"""

    def __init__(self, context: ExpressionLearningComponentContext) -> None:
        self.context = context

    @staticmethod
    def _batch_expression_scope(batch: dict[str, Any]) -> str:
        rows = batch.get("messages") or []
        last = rows[-1] if rows else {}
        return str(last.get("group_id") or batch.get("session_id") or "").strip()

    async def _batch_expression_context(self, batch: dict[str, Any]) -> dict[str, Any]:
        scope = self.context._batch_expression_scope(batch)
        participants = {
            str(row.get("sender_profile_id") or "")
            for row in batch.get("messages", [])
            if row.get("role", "user") == "user"
        }
        context_getter = getattr(self.context.archive, "get_expression_context", None)
        if callable(context_getter):
            profiles = await context_getter(limit=12, scope=scope)
            own = []
        else:
            profiles = await self.context.archive.get_expression_profiles(limit=12, scope=scope)
            own = await self.context.archive.get_expression_profiles(limit=12, scope="self")
        persona = await self.context.get_persona_text(str(batch.get("session_id") or ""))
        effects = await self.context.archive.get_reply_effects(
            limit=12, scope=str(batch.get("session_id") or "")
        )
        return {
            "expression_persona": str(persona or "")[:5000],
            "current_expression_profiles": [
                {**item.as_dict(), "evidence": ""} if item.scope == "self" else item.as_dict()
                for item in profiles + own
                if (item.scope == scope and item.profile_id in participants) or item.scope == "self"
            ],
            "recent_reply_effects": [
                item.as_dict()
                for item in effects
                if item.scope == batch.get("session_id")
                and item.outcome in {"positive", "negative"}
                and item.evidence
            ],
        }

    def _normalize_batch_expression_learning(
        self, payload: dict[str, Any], batch: dict[str, Any]
    ) -> None:
        rows = batch.get("messages") or []
        by_id = {
            str(row.get("message_id") or row.get("id") or ""): (index, row)
            for index, row in enumerate(rows)
        }
        effects = {
            str(item.get("id")): item
            for item in batch.get("recent_reply_effects", [])
            if isinstance(item, dict)
            and item.get("scope") == batch.get("session_id")
            and item.get("outcome") in {"positive", "negative"}
            and item.get("evidence")
        }
        scope = self.context._batch_expression_scope(batch)

        def references(raw: dict[str, Any], field: str) -> list[str]:
            value = raw.get(field)
            if not isinstance(value, list):
                return []
            return list(dict.fromkeys(str(item).strip() for item in value if item))[:12]

        def user_evidence(ids: list[str], profile_id: str) -> bool:
            return bool(ids) and all(
                item in by_id
                and by_id[item][1].get("role", "user") == "user"
                and by_id[item][1].get("sender_profile_id") == profile_id
                and not by_id[item][1].get("is_quoted")
                and str(by_id[item][1].get("message_text") or "").strip()
                for item in ids
            )

        profiles = []
        seen = set()
        for raw in self.context._dict_payloads(payload.get("expression_profiles"))[:4]:
            breadth = str(raw.get("breadth") or "situation")
            own_voice = breadth == "voice"
            if breadth not in {"voice", "relationship", "situation"}:
                continue
            if raw.get("breadth") == "voice" and not raw.get("profile_id"):
                owners = {str(by_id[mid][1].get("sender_profile_id") or "") for mid in references(raw, "source_message_ids")
                          if mid in by_id and by_id[mid][1].get("role") == "user"}
                if len(owners) == 1:
                    raw = {**raw, "profile_id": next(iter(owners))}
            record = ExpressionProfileRecord.from_value(raw)
            basis = str(raw.get("basis") or "")
            if (
                not record
                or not scope
                or (not own_voice and not record.profile_id)
                or not record.label
                or not (record.tone or record.habits or record.avoid)
                or not record.evidence
                or not finite_confidence(raw.get("confidence"), 0.55 if basis == "experience" else 0.78)
            ):
                continue
            user_ids = references(raw, "source_message_ids")
            if basis != "experience" and not user_evidence(user_ids, record.profile_id):
                continue
            if basis == "experience" and user_ids and not user_evidence(user_ids, record.profile_id):
                continue
            participants = {str(row.get("sender_profile_id") or "") for row in rows if row.get("role", "user") == "user"}
            if not own_voice and record.profile_id not in participants:
                continue
            if own_voice and basis != "experience":
                continue
            current = {str(item["id"]): item for item in batch.get("current_expression_profiles", []) if isinstance(item, dict)}
            if record.id:
                previous = current.get(str(record.id))
                if not previous or previous.get("scope") != ("self" if own_voice else scope) or previous.get("profile_id") != ("" if own_voice else record.profile_id):
                    continue
                # 旧的场景档案不能被一次模型总结直接扩大为对象通用偏好。
                if (previous.get("learning") or {}).get("breadth", "situation") != breadth:
                    continue
                record.label = previous["label"]
            elif own_voice:
                record.profile_id = ""
            if basis == "experience":
                reply_ids = references(raw, "reply_message_ids")
                if not reply_ids or not str(raw.get("persona_fit") or "").strip():
                    continue
                if any(item not in by_id or by_id[item][1].get("role") != "assistant"
                       or by_id[item][1].get("is_quoted")
                       or not str(by_id[item][1].get("message_text") or "").strip() for item in reply_ids):
                    continue
            if basis == "repeated":
                reply_ids = references(raw, "reply_message_ids")
                if any(
                    item not in by_id or by_id[item][1].get("role") != "assistant"
                    for item in reply_ids
                ):
                    continue
                effect_ids = references(raw, "reply_effect_ids")
                valid_effects = [
                    effects[item] for item in effect_ids if item in effects
                ]
                if len(valid_effects) != len(effect_ids):
                    continue
                if rows and rows[-1].get("is_group"):
                    valid_effects = [
                        item
                        for item in valid_effects
                        if user_evidence(
                            [str(item.get("target_message_id") or "")],
                            record.profile_id,
                        )
                    ]
                # 继续聊天只证明对话延续。稳定习惯需要独立、已判定的反馈，
                # 不能把三次普通接话或同一轮连发当作对某种语气的认可。
                positive = {
                    str(item.get("target_message_id") or item["id"])
                    for item in valid_effects
                    if item["outcome"] == "positive" and item.get("friction", 0) < 30
                }
                negative = {
                    str(item.get("target_message_id") or item["id"])
                    for item in valid_effects if item["outcome"] == "negative"
                }
                avoidance = len(negative) >= 3 and record.avoid and not record.habits
                if not avoidance and (len(positive) < 3 or negative):
                    continue
            elif basis not in {"explicit", "experience"}:
                continue
            key = (record.profile_id, record.label)
            if key in seen:
                continue
            seen.add(key)
            profiles.append(
                {
                    **raw,
                    **record.as_dict(),
                    "scope": "self" if own_voice else scope,
                    "profile_id": "" if own_voice else record.profile_id,
                    "source": "chat_expression",
                    "source_message_ids": user_ids,
                    "learning": {"breadth": breadth, "applicability": str(raw.get("applicability") or "")[:200]},
                    "_learning_event": {
                        "event_key": "chat:" + str(batch.get("session_id") or "") + ":" +
                            ",".join(sorted(user_ids + references(raw, "reply_message_ids"))),
                        "kind": "observation" if basis == "experience" else "explicit",
                        "source_scope": str(batch.get("session_id") or ""),
                        "evidence": {"source_message_ids": user_ids,
                            "reply_message_ids": references(raw, "reply_message_ids"),
                            "reply_effect_ids": references(raw, "reply_effect_ids"),
                            "basis": basis, "persona_fit": str(raw.get("persona_fit") or "")[:300]},
                    },
                }
            )
        payload["expression_profiles"] = profiles

        reviews = []
        for raw in self.context._dict_payloads(payload.get("expression_reviews"))[:3]:
            reply_id = str(raw.get("reply_message_id") or "")
            user_ids = references(raw, "source_message_ids")
            profile_id = str(raw.get("profile_id") or "")
            if (
                reply_id not in by_id
                or by_id[reply_id][1].get("role") != "assistant"
                or not user_evidence(user_ids, profile_id)
                or any(by_id[item][0] <= by_id[reply_id][0] for item in user_ids)
                or not isinstance(raw.get("passed"), bool)
                or not str(raw.get("reason") or "").strip()
            ):
                continue
            record = ExpressionReviewRecord.from_value(
                {
                    **raw,
                    "scope": str(batch.get("session_id") or ""),
                    "reply_text": by_id[reply_id][1].get("message_text") or "",
                    "source": "chat_expression",
                }
            )
            if record:
                reviews.append(
                    {**raw, **record.as_dict(), "source_message_ids": user_ids}
                )
        payload["expression_reviews"] = reviews

    async def _save_batch_expression_learning(
        self, payload: dict[str, Any], batch: dict[str, Any], meta: dict[str, str]
    ) -> None:
        saved = {"expression_profiles": []}
        await self.context._save_expression_profile_payloads(
            payload, meta, self.context._batch_expression_scope(batch), saved
        )
        by_id = {
            str(row.get("message_id") or row.get("id") or ""): row
            for row in batch.get("messages", [])
        }
        for profile, raw in zip(
            saved["expression_profiles"], saved.get("expression_profile_sources", [])
        ):
            existing = await self.context.archive.get_memory_evidence(
                target_type="expression_profile", target_id=str(profile.id), limit=30
            )
            known = {(item.source_table, item.source_id) for item in existing}
            for message_id in raw.get("source_message_ids", []):
                row = by_id[str(message_id)]
                source_id = str(row["id"])
                if ("chat_memory_messages", source_id) in known:
                    continue
                await self.context._save_experience_evidence(
                    "expression_profile",
                    str(profile.id),
                    profile.evidence,
                    {**meta, "message_id": str(message_id)},
                    evidence_type="expression_feedback",
                    source_table="chat_memory_messages",
                    source_id=source_id,
                    confidence=profile.confidence,
                )
        for raw in payload.get("expression_reviews", []):
            await self.context.archive.save_expression_review(
                ExpressionReviewRecord.from_value(raw)
            )
        # 表达学习独立于摘要是否值得保存；后续通用记忆入库不重复处理。
        payload["expression_profiles"] = []
        payload["expression_reviews"] = []
