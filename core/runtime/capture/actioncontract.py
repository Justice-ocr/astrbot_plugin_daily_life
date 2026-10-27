"""承诺提取与恢复共用的结构化动作契约。"""

COMMITMENT_ACTION_CONTRACT = """commitments 只保存尚待履行且目标有限的未来安排；当下已说出的提醒、正在做的动作和长期表达偏好不新建待办，分别记录实际进展或偏好。execution_mode 分别填 immediate、future 或 standing，只有 future 入库。
owner 按实际承担者填写：当前角色、说话人、共同或未定。
follow_up.action 只记录当前角色明确承担的未来联系或提醒，分别为 contact_person、remind_person；其余为 none，与共同活动及现实互动方式分别判断。
execute_at 填有证据的完整本地日期时间；仅有触发条件时留空，填写 condition 和 5 到 60 分钟的 check_after_minutes，不猜钟点。
message_goal 记录行动目的，不预写回复；媒体交付填写 media_kind 并交由专用任务，follow_up.action=none。"""
