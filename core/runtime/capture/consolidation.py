"""记忆批处理的证据原则；只提供本批可以使用的修订与群聊能力。"""

from __future__ import annotations

from typing import Any

from ...prompts import CORE_PERSONA_PRONOUN_RULES
from .actioncontract import COMMITMENT_ACTION_CONTRACT
from ..expressionlearning import LEARNING_RULES


def memory_batch_rules(source: dict[str, Any]) -> str:
    rules = [
        "把连续聊天整理成可复用记忆，只输出严格 JSON。区分事实、偏好、经历、纠正与未来安排；"
        "不保存寒暄、短暂情绪、无证据推断或重复信息。brief 是主题，long_summary 忠实概括批次，无法归属的信息不输出。",
        "role=user/assistant 只标记说话人，不表示双方现实分开。自然语言字段必须使用简体中文，保持说话人归属；"
        "人物使用输入 profile_id；不得复制 row_id、message_id、target_id 到自然语言字段，没有内容时字段留空。\n" + CORE_PERSONA_PRONOUN_RULES,
        "worth_saving 只控制长期摘要；无长期内容时为 false，其他有证据的感知、承诺、表达学习和行动进展仍可独立输出。"
        "visibility、action_decision 仅记录实际感知，其 reason 和生活片段 impact 用相对场景，"
        "不复述输入中的具体日期、钟点或时间轴编号，具体时间进入时间或证据字段。",
        COMMITMENT_ACTION_CONTRACT,
        "即时媒体请求属于当前工具，不另存未来承诺。media_kind=photo/video 仅用于角色明确承担的相应媒体交付，"
        "视频通话为 none；媒体的 follow_up.action=none，完整 trigger_date/trigger_time 才可自动执行，模糊时段不猜钟点。",
        "behavior_feedback 只保存角色回复之后、由后续用户消息证明的效果；life_terms 保存以后仍有助理解的用语。"
        "这两项有内容时也须有效摘要与 worth_saving=true。",
        LEARNING_RULES,
        "expression_profiles 学习角色面对该用户、该场景的表达习惯，不模仿用户或修改人设。"
        "明确稳定偏好或纠正用 basis=explicit，source_message_ids 引用用户反馈，可附先前 reply_message_ids；"
        "basis=repeated 须由 recent_reply_effects 中同对象同场景至少三次独立明确反馈支持，负反馈只支持 avoid。"
        "感谢、沉默、单次互动或角色自述不足为稳定习惯的证据。explicit/repeated 的 confidence 至少0.78，experience 至少0.55；"
        "同场景合并完整 tone/habits/avoid，保留未被否定内容，无变化不输出。"
        "按完整语义与当下表达需要判断，短句和充分解释均可自然，不套关键词或字数。"
        "expression_reviews 只评估后续用户明确证实的已发送回复效果，不写例行通过记录。",
        "execution_updates 对照 open_commitments 的目标、承担者、日期与来源：in_progress 已在做，done 整体达成，"
        "cancelled 明确取消或替换，expired 有消息证明原约定限定的情境结束。时间流逝、意向、准备、部分完成、否定与引用不作完成证据。"
        "双方只证明自己的行动，共同目标须整体达成；媒体和主动消息须实际交付回执，execution_candidates 只接受已发送 assistant 消息证明的虚拟动作。"
        "只引用输入 action_id/commitment_ids，同批新安排用 new_commitment_indexes 引用 commitments 的零起始索引；"
        "evidence 必须逐字复制 source_message_id 对应整条消息，无证据留空。",
        "temporal_facts 保存未来仍需按时间查询的事实，subject/predicate 使用稳定关系键。对照 current_temporal_facts："
        "新增 ADD，变化 UPDATE，明确失效 INVALIDATE，未变 NONE，不得省略历史变化而直接覆盖；"
        "注明 epistemic_status、逐字 evidence_quote 和本批 source_message_id，"
        "evidence_signal 为 reinforce/dispute。推测、不确定、愿望与假设不作 reported，不覆盖已确认事实；角色回复不证明用户现实状态。",
        '现实互动用 temporal_facts：subject 是人物 profile_id，predicate 固定为 interaction_mode，object_value 为 {"mode":"co_present"} 或 {"mode":"remote"}。'
        "只接受明确同处互动或远程交流的当前证据，不从平台、role、消息或语气推断。新证据即使 mode 相同也 UPDATE 刷新时间；"
        "仅沿用旧事实用 NONE，证据消失不等于模式改变。自然叙述按事实区分现场、远程和未知，未知用中性交流措辞。",
    ]
    if source.get("correctable_memories"):
        rules.append("episode_link 仅续接 correctable_memories 中同一经历，引用 memory_id、旧记录原文及本次用户整条原话，"
                     "主题相似不足为证。memory_corrections 仅记录用户明确纠正，保留原时间归属和未纠正部分，不把后来的变化抹成从未发生；无依据留空。")
    if source.get("scope", {}).get("type") == "group":
        rules.append("group_environment 只写本群实际感知。人物以输入 profile_id 归属；不能确认联系对象时 follow_up.action=none。"
                     "角色明确承担的群聊媒体承诺只向原群履行，不跨群投递。")
    return "\n".join(rules)
