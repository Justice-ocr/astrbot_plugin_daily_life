from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger

from ..epistemic import temporal_context
from ..life.achievement import review_candidates
from ..life.body import instant
from ..life.actionchain import ready as action_ready
from ..life.planalignment import protected_indexes
from ..life.tools import extract_json_from_text, timeline_deferred_until, timeline_item_datetime
from ..models import INTERNAL_SIMULATED_ACTION_TYPES
from ..prompts import LIFE_ACTION_SCOPE_RULES, cache_friendly_prompt
from .continuous import planned_actions, _TERMINAL_STATES
from .markers import LOG_PREFIX
from .goalreview import review_goals
from .decisioncontext import execution_context, run_context, skill_context


class ContinuousDecisionService:
    """仅根据已确认的生活证据选择行动，不负责执行与归档。"""

    def __init__(
        self,
        *,
        archive: Callable[[], Any],
        config: Callable[[], Any],
        persona: Callable[[], Awaitable[str]],
        recent_chats: Callable[[str], Awaitable[str]],
        scope_messages: Callable[[str], Awaitable[list[dict[str, Any]]]],
        text_provider: Callable[[str], Awaitable[Any]],
        call_text_model: Callable[..., Awaitable[str]],
        close_text_session: Callable[[str], Awaitable[None]],
        wardrobe_context: Callable[..., Awaitable[str]],
    ) -> None:
        self._archive = archive
        self._config = config
        self._persona = persona
        self._recent_chats = recent_chats
        self._scope_messages = scope_messages
        self._text_provider = text_provider
        self._call_text_model = call_text_model
        self._close_text_session = close_text_session
        self._wardrobe_context = wardrobe_context

    async def choose(self, day, world, now) -> dict[str, Any]:
        candidates = []
        for action in planned_actions(day).values():
            if (
                action.action_type not in INTERNAL_SIMULATED_ACTION_TYPES
                or not action_ready(day, action)
            ):
                continue
            if action.timeline_index is None:
                if (action.payload.get("execution_chain_unbound") is True
                        and action.action_id in world.get("pending_action_ids", [])):
                    candidates.append({**action.as_dict(), "execution_state": "planned"})
                continue
            if not 0 <= action.timeline_index < len(day.timeline):
                continue
            item = day.timeline[action.timeline_index]
            at = timeline_item_datetime(item, day.date, meta=day.meta)
            deferred = timeline_deferred_until(item, day.date, day.meta)
            if (
                at is None
                or (deferred and deferred > now)
                or item.execution_state in _TERMINAL_STATES
            ):
                continue
            duration = action.duration_minutes or item.duration_minutes
            if duration <= 0:
                continue
            action.duration_minutes = duration
            candidates.append(
                {
                    **action.as_dict(), "activity": item.activity, "place": item.place,
                    "planned_at": at.isoformat(timespec="seconds"),
                    "execution_state": item.execution_state,
                    "delay_minutes": max(0, int((now - at).total_seconds() / 60)),
                }
            )
        persona = await self._persona()
        focus = await self._archive().get_focus_slots(limit=6)
        preferences = await self._archive().get_preferences(limit=12)
        sources = {"persona", f"state:{day.date}:{day.revision}"}
        for run in [*(world.get("history") or [])[-4:], world.get("run") or {}]:
            if isinstance(run, dict) and isinstance(run.get("action"), dict) and run["action"].get("action_id"):
                sources.add(f"action:{run['action']['action_id']}")
        sources.update(f"focus:{item.id}" for item in focus)
        sources.update(f"preference:{item.id}" for item in preferences)
        kernel = world.get("kernel") if isinstance(world.get("kernel"), dict) else {}
        model = kernel.get("self_model") if isinstance(kernel.get("self_model"), dict) else {}
        sources.update(
            f"interest:{item['id']}"
            for item in model.get("interests", [])
            if isinstance(item, dict) and item.get("id") and item.get("status") == "supported"
            and item.get("disposition", "active") == "active"
        )
        unreflected_outcomes = [
            item
            for item in kernel.get("action_outcomes", [])
            if isinstance(item, dict)
            and not item.get("reflection")
            and item.get("observed_minutes", 0) > 0
        ][-2:]
        sources.update(f"action:{item['action_id']}" for item in unreflected_outcomes)
        sources.add(f"persona:{hashlib.sha256(str(persona).encode()).hexdigest()[:16]}")
        latest_exchange = []
        chat_scope = world.get("last_chat_scope")
        if chat_scope:
            try:
                messages = await asyncio.wait_for(self._scope_messages(chat_scope), timeout=3)
            except Exception as exc:
                logger.debug(
                    f"{LOG_PREFIX} 当前会话暂未读取完成，继续依据已有生活事实：{type(exc).__name__}"
                )
                messages = []
            latest_exchange = [
                {
                    "role": item.get("role"),
                    "name": item.get("name"),
                    "content": str(item.get("content") or "")[:1200],
                    "message_id": item.get("message_id"),
                }
                for item in messages[-6:]
                if isinstance(item, dict)
            ]
            if latest_exchange and world.get("chat_events"):
                sources.add(f"chat:{world['chat_events'][-1]}")
        goal_source_key = hashlib.sha256(
            json.dumps(
                [
                    persona,
                    [item.as_dict() for item in focus],
                    [item.as_dict() for item in preferences],
                ],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        last_review = instant(world.get("goal_review_at"))
        goal_review_due = (
            goal_source_key != world.get("goal_source_key")
            or last_review is None
            or now - last_review >= datetime.timedelta(hours=6)
        )
        achievement_candidates = review_candidates(world)
        last_attempt = instant(world.get("achievement_review_at"))
        revisions = [item["revision"] for item in achievement_candidates]
        if (last_attempt and now - last_attempt < datetime.timedelta(minutes=20)
                and revisions == world.get("achievement_review_revisions")):
            achievement_candidates = []
        living_objects = await self._archive().get_living_entities(
            str(chat_scope or "global"), kind="object", limit=24
        )
        # 仅传当前资源与作品摘要，避免每轮重复携带完整修改历史。
        living_objects = [{key: item[key] for key in (
            "id", "revision", "title", "state", "location", "owner", "quantity", "unit",
            "available_at", "reserved_by", "artifact_version", "series_id", "progress", "next_step",
            "content", "provenance",
        ) if key in item} for item in living_objects]
        for item in living_objects:
            if "content" in item:
                item["content"] = str(item["content"])[:1800]
        knowledge = await self._archive().get_temporal_facts(scope=str(chat_scope or "global"), limit=16, include_uncertain=True)
        # 同一组事实先决定动作，再校验；模型不直接写入执行结果或身体数值。
        fixed = """为当前角色选择接下来实际要执行的一项内部数字生活行动，并维护少量能持续数周的自身目标。
只输出 JSON：
{"decision":"start|continue|pause|resume|cancel|wait", "action_id":"候选动作编号或空", "new_actions":[{"owner":"self","action_type":"内部动作类型","target":"本步骤的具体行动","timeline_index":null,"duration_minutes":10,"payload":{}}], "rest_kind":"break|sleep", "reason":"当前事实依据", "goal_id":"可选已有目标编号", "step_id":"可选可继续阶段编号", "new_goals":[{"owner":"self","source_id":"来源编号","title":"自身长期目标","reason":"为什么想做","skill":"可选练习能力名称","steps":[{"id":"阶段编号","title":"具体练习阶段","action_type":"内部动作类型","required_minutes":60,"depends_on":[]}]}], "goal_updates":[{"goal_id":"已有目标编号","status":"active|blocked|abandoned","evidence_id":"来源编号","reason":"原因","remaining_steps":[]}]}
模型只选择和解释，执行器负责时间、前置条件、库存、完成回执及进度。时间轴是计划，不能因为时间过去就声称完成。
有运行中动作时优先 continue；新证据使其不合适时 pause/cancel。暂停动作可 resume，未观测的停机时间不算执行时间。
start 优先引用候选 action_id；即时行动用 new_actions 按实际先后拆成独立类型与耗时的步骤，每个 target 只描述本步骤，后续步骤仍是未执行计划、开始前重新判断。已有计划用其 action_id，不重复创建同一节点的动作；即时行动没有节点时 timeline_index 填 null，执行器会把实际开始的步骤加入时间轴。所有行动只属于当前角色，不替用户执行，不发消息、不生图、不拍视频、不调用现实采购或支付。
同一次判断同步审视实际行动与剩余日程：timeline_updates=[{"timeline_index":0,"operation":"reschedule|skip|cancel","at":"改期的完整本地日期时间","reason":"依据事实解释调整","source_ids":["allowed_source_ids 中的编号"]}]，无需调整返回 []。候选包含未来计划，提前执行须先将对应节点 reschedule 到现在；自主行动改变作息或安排时，同步顺延、跳过或取消受影响且未执行的节点，避免再重复执行。已有运行中行动也需复核后续安排。只调整 protected=false 的节点，不改变活动内容，不修改已发生事实，不把未做事项记为完成；已确认约定和延后休息安排保留，实际行动应与之协调。
内部类型：rest, drink, meal, cook, order_food, purchase, move, travel, work, study, chore, exercise, groom, change_outfit。
候选可能已延误，delay_minutes 仅表示原计划与现在的间隔，不证明执行过；先判断此刻是否仍有必要，过时的准备不要机械补做，也不要因错过时段就遗忘仍需要的准备。
动作不由时钟、关键词或固定轮换表选择。结合实际身体需求、当前地点、聊天打断、未完成约定及角色兴趣；需求数值只是依据，不是到阈值必做。
睡眠用 rest_kind=sleep，普通休息用 break；尊重已确认的延后休息安排。未明确决定睡眠时不能把普通休息当睡觉。
new_actions 每步保留明确领域参数：cook.ingredients 必须来自现有库存；drink 可选 volume_ml；exercise.intensity 为1-5。换装使用 change_outfit，从 wardrobe_candidates 取 catalog_reference_ids，并填写 scene_category、outfit_components；洗漱不包含换装效果。新移动、旅行仍须引用已有候选，不能凭空改地点。
cook 只完成食物准备，order_food 只完成点餐；meal 才表示实际进食。准备、下单和未确认的送达不能当作已经吃过，也不产生饱腹收益。
长期目标只属于角色自身。来源必须是明确支持角色兴趣、意愿或已经确认的自身目标的 persona、preference 或 focus 编号；不要把用户的愿望、猜测或普通话题当成角色目标。没有依据可以不建目标。
最多三个进行中目标，不要求每天创建；已有目标自然延续。阶段写可实际执行的练习与时间要求，依赖只能引用同一目标的阶段编号。练习累计表示做过，不等于已掌握技能或产出外部成果。
仅当所选行动确实推进已有可继续阶段时填写 goal_id/step_id；不得关联尚未建立的目标、已完成阶段或依赖未完成阶段。
仅在 goal_review_due=true 时提出新目标。已有目标出现本轮状态或对话证据支持的阻碍、意愿变化时，可以暂停、放弃或恢复；remaining_steps 仅替换未开始的阶段，可以依赖保留的已完成阶段，不重写已经发生的练习、完成证据或能力记录。
普通闲聊不必停止内部活动；需要集中回应、共同活动、已确认延后休息或明确换装变化时审视当前行动。保持自然简短，不输出要发给用户的旁白。"""
        fixed += "\n" + LIFE_ACTION_SCOPE_RULES
        fixed += """
统一生命内核是聊天、主动回访、状态刷新和自主行动共同读取的同一事实快照。只把已观察的真实对话、行动回执、身体变化和复盘写入经历；不得把模型提议、时间轴到点或计划中的成果写成已发生。
如果 open_threads 中有尚未完成的自身事项，优先考虑自然延续；使用 payload.thread_id，保持同一事项跨日衔接。payload.obstacle 和 payload.artifact 此时都是预期，不能当作已发生。
可增加两个 JSON 数组，没有充分依据时返回空数组：
"action_reflections":[{"action_id":"仅可引用 unreflected_outcomes 中的编号","summary":"对真实执行过程的事后判断","obstacle":"有证据的困难，没有则空","next_step":"可执行的后续计划","close_thread":false,"artifact":{"kind":"practice_note|work_draft","content":"本轮实际整理的练习笔记或工作草稿，允许留空"}}]。
每次最多整理两项行动。时间和回执只能证明练习过，不能宣称已经掌握，也不能杜撰拍过的照片、看过的材料或写过的外部文件。生成的笔记或草稿是本轮数字成果，不能叙述为此前已经产生的作品。已完成的单次事项可以 close_thread=true，仍需继续的兴趣和长期练习保持打开。
"self_model_updates":[{"owner":"self","field":"interest|value|boundary","id":"稳定编号","text":"自己的兴趣、价值或边界","source_ids":["allowed_source_ids 中的完整编号"],"reason":"证据怎样支持这一认识"}]。
仅在 goal_review_due=true 时审视自我模型，每次最多四项，小步变化。自我认识必须属于当前角色，普通话题和用户自身愿望不构成角色兴趣。新认知可以暂定，持续的不同证据才逐步稳定；不得把练习时间直接解释成能力认证。
"""
        fixed += """
选择 start 时可填 expectation={"reason":"预期依据","body_change":{"energy":-3}}；行动复盘可填 adjustment，保存基于执行结果准备尝试的调整，不将一次经历概括成定律。
需要物品时 payload.resource_uses=[{"object_id":"living_objects 中的编号","revision":1,"mode":"use|consume","quantity":1,"unit":"件"}]；只用 owner=self 且当前地点可用的物品，缺少库存依据时不能编造。延续数字作品时 payload.artifact_parent_id 引用已有作品编号，实际新正文在后续复盘中保存成新版本。
目标阶段另填 acceptance（具体成果验收条件）、evidence_kind（artifact 数字作品或 action 实际执行回执）。练习时间只记录投入；数字作品型目标必须有已保存作品才能验收。
可使用已支持的 interest 来源建立目标，并在行动选择中填写 interest_id、motive（自身动机）；允许保持偏好、商量或休息，不为讨好对方重置兴趣。
可用 interest_updates=[{"id":"已有兴趣编号","disposition":"active|paused|retired","source_ids":["本轮实际证据编号"],"reason":"自己的意愿变化","next_step":"下次具体想继续什么"}] 维护持续兴趣；沉默不表示失去兴趣。

"""
        try:
            recent_chats = await asyncio.wait_for(
                self._recent_chats(persona), timeout=5
            )
        except asyncio.TimeoutError:
            recent_chats = "参考会话暂未读取完成，优先依据当前已确认对话和生活事实"
        protected = protected_indexes(day, world)
        dynamic = {
            "persona": persona,
            "preferences": [item.as_dict() for item in preferences],
            "focus": [item.as_dict() for item in focus],
            "self_model": {key: value for key, value in (kernel.get("self_model") or {}).items()
                           if key != "capabilities"},
            "allowed_source_ids": sorted(sources),
            "life_kernel": execution_context(world),
            "unreflected_outcomes": unreflected_outcomes,
            "living_objects": living_objects,
            "knowledge": temporal_context(knowledge),
            "resource_blocked": world.get("resource_blocked", ""),
            "plan_alignment_blocked": world.get("plan_alignment_blocked", ""),
            "open_threads": [
                item
                for item in (
                    world.get("kernel", {}).get("open_threads", [])
                    if isinstance(world.get("kernel"), dict)
                    else []
                )
                if isinstance(item, dict) and item.get("status") == "open"
            ][-12:],
            "state": day.state.as_dict() if day.state else {},
            "weather": day.weather,
            "current_place": day.meta.get("current_place", ""),
            "outfit": day.outfit,
            "wardrobe_context": day.meta.get("wardrobe_context", ""),
            "wardrobe_candidates": await self._wardrobe_context(day, now),
            "timeline": [
                {"timeline_index": index, "at": at.isoformat(), "activity": item.activity,
                 "execution_state": item.execution_state, "place": item.place,
                 "duration_minutes": item.duration_minutes,
                 "protected": index in protected}
                for index, item in enumerate(day.timeline)
                if item.execution_state not in _TERMINAL_STATES
                and (at := timeline_item_datetime(item, day.date, meta=day.meta))
            ],
            "rest_delay_until": day.meta.get("rest_delay_until", ""),
            "candidates": candidates,
            "world": {
                "body": world.get("body"),
                "run": run_context(world),
                "goals": [
                    {
                        **{
                            key: goal.get(key)
                            for key in (
                                "id",
                                "title",
                                "status",
                                "reason",
                                "skill",
                                "obstacle",
                            )
                        },
                        "steps": [
                            {
                                key: step.get(key)
                                for key in (
                                    "id",
                                    "title",
                                    "status",
                                    "action_type",
                                    "required_minutes",
                                    "practice_minutes",
                                    "depends_on",
                                    "acceptance",
                                    "evidence_kind",
                                    "next_step",
                                    "review_state",
                                    "review_pending",
                                )
                            }
                            for step in goal["steps"]
                        ],
                    }
                    for goal in world.get("goals", [])
                    if goal.get("status") in {"active", "blocked"}
                ],
                "skills": skill_context(world),
            },
            "recent_internal_actions": world.get("history", [])[-4:],
            "recent_chats": recent_chats[-3000:],
            "pantry": await self._archive().get_pantry_items(limit=30),
            "latest_exchange": latest_exchange,
            "latest_exchange_at": world.get("last_chat_at", ""),
            "latest_exchange_scope": chat_scope or "",
            "recent_state_evidence": day.state_log[-4:],
            "goal_review_due": goal_review_due,
            "now": now.isoformat(),
        }
        provider = await self._text_provider(self._config().state.provider)
        if provider is None:
            return {}
        session = f"daily_life_execution_{uuid.uuid4().hex[:8]}"
        try:
            decision_call = self._call_text_model(
                provider,
                cache_friendly_prompt(fixed, json.dumps(dynamic, ensure_ascii=False)),
                session,
                empty_retries=0,
                primary_provider_id=self._config().state.provider,
                timeout_seconds=min(45, self._config().llm_timeout_seconds),
            )
            text, reviews = await asyncio.gather(
                decision_call,
                review_goals(provider, achievement_candidates, call=self._call_text_model,
                             close=self._close_text_session,
                             provider_id=self._config().state.provider,
                             timeout=self._config().llm_timeout_seconds),
                return_exceptions=True,
            )
            payload = extract_json_from_text(text) if isinstance(text, str) else None
            reviews = reviews if isinstance(reviews, list) else []
            if not isinstance(payload, dict) and reviews:
                payload = {"decision": "wait", "reason": "先保存已经完成的阶段复核，稍后重新选择行动"}
            if isinstance(payload, dict):
                payload["achievement_reviews"] = reviews
                payload["_sources"] = sources
                payload["_candidates"] = candidates
                payload["_goal_review_due"] = goal_review_due
                payload["_goal_source_key"] = goal_source_key
                payload["_reflection_action_ids"] = {
                    item["action_id"] for item in unreflected_outcomes
                }
                payload["_achievement_candidates"] = achievement_candidates
                payload["_self_model"] = {
                    "persona": persona,
                    "preferences": dynamic["preferences"],
                    "focus": dynamic["focus"],
                    "skills": dynamic["world"].get("skills") or {},
                    "evidence_ids": sorted(sources),
                }
                return payload
            return {}
        finally:
            await self._close_text_session(session)


__all__ = ["ContinuousDecisionService"]
