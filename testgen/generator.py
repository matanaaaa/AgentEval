"""
Case Generator

第二阶段：把 Scenario Plan 里每个 scenario 变成符合项目 Pydantic Case Schema
的用例 dict。

设计要点：
- 一次让 LLM 基于整份 Plan 产出全部 case（保留 scenario 之间的呼应），
  但在 prompt 里把 Schema 讲清楚，降低 Gate 拒绝率
- 生成的 case 仍必须过 Deterministic Gate，这里不做「相信 LLM」的假设
- id 前缀由调用方给（如 GEN_ACCOUNT_），保证与既有用例不撞
"""

import json
from typing import Any, Optional

from testgen.llm import LLMClient

# 给 LLM 的 Schema 说明 + 一个 few-shot 样例（对齐 cases/account_create.json 风格）
SCHEMA_BRIEF = """项目用例 Schema（Pydantic，未知字段会被拒绝，务必严格遵守）：

顶层字段：
- id: 字符串，全局唯一
- name: 字符串，用例名
- type: 只能是 "single" | "multi_turn" | "confirmation"（single 必须恰好 1 轮）
- metadata: { scenario, priority(P0/P1), difficulty(easy/medium/hard), tags:[...] }
- turns: [ { turn_id, input, expected } ]
- evaluation: 用例级期望

turns[].expected 只能含：
- text_assertions: { all:[...], any:[...], not_any:[...], iany:[...] }
- state_assertion: { fields:{apikey:值}, absent:[apikey] }

evaluation 只能含以下键（不要多加字段）：
- skill: 字符串（本项目录入类固定用 "extract-data"）
- expected_skills: [...]
- tools_required: [...]         # 合法 tool 名见下
- tools_forbidden: [...]
- object_type: 字符串（如 "account"）
- business_success: true | false | "pending"
- fields: { apikey: 期望值 }
- field_constraints: { apikey: "absent" | "present" }

合法 tool 名：save_record
合法 account 字段 apikey：accountName, entityType, customerLevel, accountSource,
region, province, city, district, address, employeeCount, salesAmount,
registeredCapital, foundDate, listedDate, phone, email, website, remark, weibo, fax

约定：
- 过程/中间态或还需确认的用例用 business_success:"pending"
- 期望创建失败（缺必填/查重）用 business_success:false
- 明确确认创建成功用 business_success:true 且 tools_required:["save_record"]"""

FEWSHOT = """样例（confirmation 两轮）：
{
  "id": "GEN_ACCOUNT_EXAMPLE",
  "name": "录入客户：标准信息抽取与确认创建",
  "metadata": {"scenario":"extract_and_confirm_create","priority":"P0","difficulty":"medium","tags":["account","field_extraction","confirmation_flow"]},
  "type": "confirmation",
  "turns": [
    {"turn_id":"T01","input":"我想录入一个客户，客户名称叫中铁十二局，客户类型为默认业务类型",
     "expected":{"text_assertions":{"all":["中铁十二局"],"any":["默认业务类型","确认","工商"]},
                 "state_assertion":{"fields":{"accountName":"中铁十二局","entityType":"默认业务类型"}}}},
    {"turn_id":"T02","input":"确认创建",
     "expected":{"text_assertions":{"any":["创建成功","已创建","数据重复"]}}}
  ],
  "evaluation": {"skill":"extract-data","tools_required":["save_record"],"object_type":"account",
    "business_success":true,"fields":{"accountName":"中铁十二局","entityType":"默认业务类型"}}
}"""

GENERATOR_SYSTEM = """你是测试用例工程师。根据给定的 Scenario Plan，为每个 scenario 生成一个
符合项目 Schema 的测试用例。只输出 JSON，不要解释。""" + "\n\n" + SCHEMA_BRIEF + "\n\n" + FEWSHOT

GENERATOR_USER_TEMPLATE = """object_type：{object_type}
用例 id 前缀：{id_prefix}（每个 case 的 id 必须以此开头，后接 scenario 的 id，如 {id_prefix}S1）

Scenario Plan：
{plan_json}

请为上面 scenarios 中的每一个生成一个用例，输出如下 JSON：
{{
  "cases": [ {{...符合 Schema 的用例...}} ]
}}

要求：
- 每个 case 的 metadata.scenario 用对应 scenario 的 name 或 id，便于覆盖核对
- input 用真实、自然的中文用户表述，贴合 scenario 的 intent 和 risk
- 只使用合法的 tool 名与合法的 account 字段 apikey
- single 类型必须恰好 1 轮；多轮场景用 multi_turn 或 confirmation"""


def generate_cases(plan: dict[str, Any], id_prefix: str = "GEN_ACCOUNT_",
                   llm: Optional[LLMClient] = None) -> list[dict[str, Any]]:
    """
    基于 Scenario Plan 生成 case dict 列表。

    返回未经门禁的原始用例列表；调用方负责过 Deterministic Gate。
    """
    client = llm or LLMClient()
    object_type = plan.get("object_type", "account")
    user = GENERATOR_USER_TEMPLATE.format(
        object_type=object_type,
        id_prefix=id_prefix,
        plan_json=json.dumps(plan, ensure_ascii=False, indent=2),
    )
    result = client.chat_json(GENERATOR_SYSTEM, user)
    cases = result.get("cases", [])
    if not isinstance(cases, list):
        return []
    return cases
