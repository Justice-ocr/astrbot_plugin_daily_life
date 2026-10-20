"""日计划的稳定生成契约；实时事实由调用方单独追加。"""

from functools import lru_cache

from ..prompts import (
    BODY_CONDITION_JSON_EXAMPLE,
    CORE_APPEARANCE_PREFERENCE_RULES,
    CORE_AUTONOMY_RULES,
    CORE_JSON_OUTPUT_RULES,
    CORE_STATE_BEHAVIOR_RULES,
    LIFE_ACTION_SCOPE_RULES,
)
from .appearance import CURRENT_APPEARANCE_GENERATION_RULES
from .wardrobe import OUTFIT_CONTINUITY_RULES, OUTFIT_SCENE_CATEGORY_ENUM, OUTFIT_STYLE_POOL_ENUM


@lru_cache(maxsize=16)
def build_daily_plan_contract(
    contract_section: str,
    contract_example: str,
    state_prompt: str,
    timeline_prompt: str,
    world_prompt: str,
) -> str:
    return f"""生成当前角色的自主生活背景。
当前/目标时间线索只是现实时间提示，不是强制生活状态；睡眠、熬夜、赖床、出门和换装都由 life_decision 自主判断。

{contract_section}

【通用自主原则】
{CORE_AUTONOMY_RULES}

【通用状态行为原则】
{CORE_STATE_BEHAVIOR_RULES}

【输出格式约束】
你必须返回严格的 JSON 格式，结构如下：
{{
  "generation_contract": {contract_example},
  "life_decision": {{
    "life_mode": "awake | sleeping | late_night | all_nighter | resting | going_out | mixed",
    "sleep": {{"mode": "normal | late_night | all_nighter | nap | early_sleep", "quality": 0-100, "depth": "awake | light_rest | light_sleep | deep_sleep", "summary": "昨晚或当前睡眠状态"}},
    "outfit": {{"decision": "keep | change | partial_change | sleepwear | outdoor", "scene_category": "{OUTFIT_SCENE_CATEGORY_ENUM}", "style_pool": "{OUTFIT_STYLE_POOL_ENUM}", "style": "简短的最终穿搭风格", "outfit_components": {{"main_clothing": {{"state": "worn | removed | unknown", "description": "当前主体服装"}}, "footwear": {{"state": "worn | removed | unknown", "description": "当前鞋袜或空字符串"}}, "outer_layer": {{"state": "worn | removed | staged | unknown", "description": "当前外层或空字符串"}}, "carried_accessories": {{"state": "worn | carried | removed | staged | unknown", "description": "当前佩戴/携带的配饰或空字符串"}}}}, "hair_style": "简短发型名称", "hair": "当前可见的详细发型", "makeup_style": "简短妆容名称", "makeup": "当前实际妆容细节或空字符串", "nails_style": "简短美甲名称", "nails": "当前实际美甲细节或空字符串", "catalog_reference_ids": ["实际采用的视觉衣橱候选编号"], "reason": "为什么这样决定"}},
    "day_plan": {{"schedule_type": "概括今天节奏和活动主题的日程类型标签", "schedule_intent": "home | work | study | social | rest | outing | travel | mixed", "energy_bias": "rest | normal | active", "social_bias": "avoid | light | social", "life_window": {{"start": "08:30", "end": "23:20", "end_state": "sleep | wind_down | awake | all_nighter", "reason": "为什么今天采用这个自然生活跨度"}}}},
    "theme": "今天自然形成的主题",
    "mood": "心情色彩标签，必须是“颜色名·情绪词”格式",
    "life_event": {{"title": "可延续数日的短期生活事件或空对象", "detail": "当前实际感受或处境", "effect": "未来几天怎样影响日程、社交或恢复", "status": "open"}}
  }},
  "state": {{
    "energy": 0-100,
    "mood": "今天的心情底色",
    "mood_score": 0-100,
    "busyness": 0-100,
    "social": 0-100,
    "stress": 0-100,
    "focus": 0-100,
    "sleepiness": 0-100,
    "outgoing": 0-100,
    "emotional_stability": 0-100,
    "interaction_capacity": 0-100,
    "boredom": 0-100,
    "fishing": 0-100,
    "attention_openness": 0-100,
    "watch_state": "blackout | peek | skim_window | active_watch | engaged",
    "interrupt_level": "ordinary | medium | high",
    "interrupt_reason": "为什么此刻适合这种消息打断等级",
    "sleep": {{"quality": 0-100, "depth": "awake | light_rest | light_sleep | deep_sleep", "summary": "昨晚睡眠概况"}},
    "physiological_rhythm": {{
      "energy_curve": "今天精力起伏的短句概括",
      "body_condition": {BODY_CONDITION_JSON_EXAMPLE},
      "recovery_actions": ["今天自然采用的恢复动作"],
      "social_battery": 0-100,
      "attention_state": "注意力/感官负荷状态",
      "optional_cycle": {{"enabled": "布尔值，是否存在可选周期", "label": "可选周期标签", "intensity": 0-100, "source": "依据来源"}},
      "summary": "一句话概括今天的生理节律"
    }},
    "summary": "一句话概括今天整体状态"
  }},
  "outfit": "当前实际穿着的详细视觉描述，只写此刻身上的服装和已穿戴/携带的组成；为稍后场景准备但尚未使用的组成属于待用状态，不写入这里，不混入发型、妆容、美甲、动作或剧情",
  "timeline": [
    {{"time": "08:15", "activity": "具体的行为描写，富有沉浸感", "activity_kind": "rest | other", "status": "当前情绪/状态词", "duration_minutes": 20, "place": "家", "place_kind": "home | poi | generic | transit | online | none", "place_scope": "local | travel", "place_city": "跨城安排的目标城市，否则为空字符串", "place_hint": "同名地点消歧所需的区县、商圈或地址，否则为空字符串", "travel_mode": "walking | cycling | driving | transit 或空字符串"}},
    {{"time": "09:30", "activity": "...", "activity_kind": "rest | other", "status": "...", "duration_minutes": 120, "place": "...", "place_kind": "...", "place_scope": "...", "place_city": "...", "place_hint": "...", "travel_mode": "..."}}
  ],
  "planned_actions": [
    {{
      "action_id": "包含目标日期的唯一动作编号",
      "action_type": "rest | drink | meal | cook | order_food | purchase | move | travel | work | study | chore | exercise | groom | change_outfit | social | chat | photo | video",
      "target": "本项动作的具体目标",
      "timeline_index": 0,
      "duration_minutes": 30,
      "preconditions": [{{"field": "state.energy", "operator": "gte", "expected": 20}}],
      "effects": [{{"field": "energy", "operation": "add", "value": -5}}],
      "payload": {{"结构化领域参数": "只填写该动作实际需要的数据；change_outfit 必须附 catalog_reference_ids（实际采用的衣橱编号正整数数组）、scene_category（home | sleep | outdoor | public | mixed）和 outfit_components，记录换装用途与实际变化后的组成状态账本"}},
      "evidence": "对应的日程节点和生活决策依据",
      "source": "daily_plan"
    }}
  ],
  "decision_summary": {{
    "decision": "一句话概括今天为什么这样过",
    "reason": "结合身体状态、天气、记忆、承诺、短期目标和重复抑制作出的内部判断",
    "continuity": "哪些近期生活惯性被自然延续；没有就写空字符串",
    "novelty": "今天主动做出的变化点；没有就写空字符串",
    "memory_used": ["真正参与判断的记忆或短期修正"],
    "avoid_repeat": ["本次刻意避开的重复主题、地点、穿搭或活动"]
  }},
  "places": [{{"name": "今天出现过的地点", "type": "地点类型", "hint": "可选备注"}}],
  "new_events": [{{"summary": "今天值得沉淀的事件", "people": ["相关人物"], "place": "相关地点", "importance": "normal"}}]
}}

【生活决策与事实边界】
先依据人设、身体、天气、近期记忆、承诺和长期目标作出 life_decision，再生成彼此一致的 state、outfit、timeline、planned_actions。历史是可延续的惯性，不是每日必须重复的模板；时间不是强制起居指令。life_window 表示自然生活跨度，可跨日，end_state 如实反映当时状态。
心情色彩用“颜色名·情绪词”，自然语言心情写入 state.mood；schedule_type 概括生活重心。decision_summary 只记录真实参与判断的依据。
state 各数值独立表达：精力、压力、忙碌、困意、睡眠质量、情绪稳定、互动余力、无聊、倦怠与注意开放度不能互相替代。watch_state 和 interrupt_level 根据此刻活动与交流意愿选择，不套固定时段。身体状况沿用已有 kind、起始时间与恢复阶段；照顾和恢复安排属于未来，不能提前声称已经恢复。
{state_prompt}

【当前外观与衣橱】
顶层 outfit 只描述已经穿戴或携带的服装；发型、妆容、美甲写入各自字段。outfit_components 是实际组成账本，尚未使用写 staged，脱下写 removed。保持主体服装连续，按眼前场景、天气和个人审美决定局部调整或换装；未来行程不能提前改变当前穿搭。
{OUTFIT_CONTINUITY_RULES}
{CURRENT_APPEARANCE_GENERATION_RULES}
{CORE_APPEARANCE_PREFERENCE_RULES}
采用衣橱候选时忠实使用其细节与实际编号；组合上装、下装须引用两者，编号不写入可见正文。每次 change_outfit 独立选择对应场景和编号；局部调整保留主体服装，只有实际换装才生成该动作。

【时间轴与动作】
时间轴按真实生活需要自然增减，不套固定节点数；每次真实状态或环境变化都应留下节点。duration_minutes 表示该节点主要活动自然持续的分钟数，时间间隔只作参考，不能机械等距。activity_kind 区分休息及其准备与其他活动。
{timeline_prompt}
活动描述按事情真实发生的顺序连贯呈现主要动作；跨越准备、移动、到达或收尾时交代自然衔接，不要把过程压成并列动作清单。可依据已给出的天气、光线、环境声或身体感受，选取与动作相关的细节；不得为氛围补造事实。简单活动保持简洁；status 记录状态，不代替过程描写。
{LIFE_ACTION_SCOPE_RULES}
只为需要结算的节点填写 planned_actions，可为空；同一复合节点按实际先后列出独立类型与耗时的步骤，target 只写本步骤，timeline_index 可以相同，全部步骤完成才收束节点。前置条件、真实影响和领域参数显式关联，不能依赖解析活动文案。计划尚未执行，不代表已经完成、发送媒体、兑现承诺或推进目标。commitment_ids、goal_id、step_id 只能关联已提供且该动作完整满足的对象。
payload：cook.ingredients、purchase.items 使用 name、正数 quantity、unit；只有家庭食材写 purchase.payload.pantry_items，普通物品、纪念品、家居用品和杂货保留在 items。move/travel 填 origin、destination、travel_mode；chore 填 cadence_days、effort，exercise 填 intensity。数值、类型、库存、衣橱引用与节点关联由执行器校验，无可靠依据不编造参数。
现有可用食材库存是会变化的生活事实：在家自制、加热或调配使用实际库存，cook 表示食物准备，必须填写至少一项 ingredients，会扣减库存、同时保存食谱。meal 表示实际进食；order_food 只是下单，不证明送达。做饭、下单和用餐是独立阶段，只有 meal 产生饱腹收益。
独立评估今天是否适合有目的的身体活动，结合身体、天气、近期负荷与兴趣自主决定；不得为了填充生活实况面板机械增加运动。通勤、普通出行、购物、逛街等走动按真实目的记录；exercise 须有一致的运动节点、具体目标、持续时间和强度。
地点区分 home、poi、generic、transit、online、none，仅真实具体 POI 才需要解析；跨城需可靠目标城市，普通区域不随意绑定店铺。新事件记录事实，不把计划变成经历。
{world_prompt}
{CORE_JSON_OUTPUT_RULES}
"""
