"""
Scenario Planner

第一阶段：不直接写 Case，先让 LLM 沿
    Requirement → 功能点 → 风险点 → 测试维度 → Scenario Plan
逐层展开，产出结构化的 Scenario Plan。

Plan 是后续 Case 生成的输入，也是 Coverage Gate 的对照基准
（每个 scenario 是否都生成了 case、requirement 是否被覆盖）。

Plan 结构（dict）：
{
  "requirement": "...",
  "features": ["功能点1", ...],
  "risks": ["风险点1", ...],
  "dimensions": ["测试维度1", ...],
  "scenarios": [
    {"id": "S1", "name": "...", "feature": "...", "risk": "...",
     "dimension": "...", "priority": "P0", "intent": "一句话说明测什么"}
  ]
}
"""

from testgen.llm import LLMClient


MAX_SCENARIOS_LIMIT = 3

PLANNER_SYSTEM = """你是资深测试架构师，负责为 CRM「智能录入」Agent 设计测试方案。
不要直接写测试用例，只做测试规划：从需求逐层拆解到 Scenario Plan。

严格按以下步骤思考并输出：
1. features：从需求中抽取可测的功能点
2. risks：每个功能点可能出问题的风险点（歧义输入、边界值、非法值、上下文丢失、意图切换、重复数据等）
3. dimensions：需要覆盖的测试维度（字段抽取、多轮改写、确认流程、negative case、鲁棒性等）
4. scenarios：把上面收敛成一组具体、互不重复的测试场景

只输出 JSON，不要任何解释文字。"""

PLANNER_USER_TEMPLATE = """需求描述：
{requirement}

对象类型(object_type)：{object_type}

请输出如下 JSON：
{{
  "requirement": "对需求的一句话概述",
  "features": ["功能点..."],
  "risks": ["风险点..."],
  "dimensions": ["测试维度..."],
  "scenarios": [
    {{
      "id": "S1",
      "name": "场景简名",
      "feature": "对应功能点",
      "risk": "对应风险点",
      "dimension": "对应测试维度",
      "priority": "P0 或 P1",
      "intent": "一句话说明这个场景要验证什么"
    }}
  ]
}}

要求：
- scenarios 数量控制在 {max_scenarios} 个以内，优先覆盖 P0 风险
- 每个 scenario 的 id 形如 S1、S2，全局唯一
- 场景之间不要重复"""


def build_scenario_plan(requirement: str, object_type: str = "account",
                        max_scenarios: int = MAX_SCENARIOS_LIMIT,
                        llm: LLMClient = None) -> dict:
    """
    生成 Scenario Plan。

    Args:
        requirement: 自然语言需求描述
        object_type: 业务对象类型（account / contact ...）
        max_scenarios: 场景数量上限，控制规模
        llm: 可注入的 LLM 客户端（便于测试替换）

    Returns:
        Scenario Plan dict
    """
    if not 1 <= max_scenarios <= MAX_SCENARIOS_LIMIT:
        raise ValueError(
            f"max_scenarios 必须在 1-{MAX_SCENARIOS_LIMIT} 之间，实际为 {max_scenarios}"
        )

    client = llm or LLMClient()
    user = PLANNER_USER_TEMPLATE.format(
        requirement=requirement.strip(),
        object_type=object_type,
        max_scenarios=max_scenarios,
    )
    plan = client.chat_json(PLANNER_SYSTEM, user)

    # 补齐缺省字段，避免下游 KeyError
    plan.setdefault("requirement", requirement.strip())
    plan.setdefault("features", [])
    plan.setdefault("risks", [])
    plan.setdefault("dimensions", [])
    scenarios = plan.setdefault("scenarios", [])
    if not isinstance(scenarios, list):
        scenarios = []
    # 提示词限制不是硬约束；这里再次截断，保证模型即使不遵循指令也不会超过上限。
    plan["scenarios"] = scenarios[:max_scenarios]
    plan["object_type"] = object_type
    return plan
