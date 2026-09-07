"""
生成流水线编排

    Requirement
        ↓ Planner
    Scenario Plan
        ↓ Generator
    AI Generated Cases
        ↓ Schema Gate
        ↓ Coverage Gate
        ↓ Domain Gate
      PASS ↓            FAIL → 打印违规，不落盘
    写入 cases/ → 交给 AgentEval

只有整批通过 Gate 才落盘，避免把半成品用例混进用例库。
"""

import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

from testgen.llm import LLMClient
from testgen.planner import build_scenario_plan
from testgen.generator import generate_cases
from testgen.gate import DeterministicGate, GateResult


@dataclass
class PipelineOutput:
    """一次生成的产物"""
    plan: dict = field(default_factory=dict)
    cases: List[dict] = field(default_factory=list)     # 生成的原始用例
    gate: Optional[GateResult] = None
    output_path: str = ""                                # 落盘路径（未落盘为空）


def run_pipeline(requirement: str, object_type: str = "account",
                 id_prefix: str = "GEN_ACCOUNT_", max_scenarios: int = 3,
                 out_path: str = None, llm: LLMClient = None,
                 dry_run: bool = False) -> PipelineOutput:
    """
    执行完整流水线。

    Args:
        requirement: 自然语言需求
        object_type: 业务对象类型
        id_prefix: 用例 id 前缀
        max_scenarios: 场景上限
        out_path: 落盘路径；None 时默认 cases/<id_prefix>generated.json
        llm: 可注入 LLM 客户端
        dry_run: True 时即使通过也不落盘（只看结果）

    Returns:
        PipelineOutput
    """
    client = llm or LLMClient()
    output = PipelineOutput()

    # 1. Requirement
    print("[1/8] Requirement")
    print(f"  {requirement.strip()}")

    # 2. Planner
    print("\n[2/8] Planner")
    print("  正在生成测试计划 ...")
    output.plan = build_scenario_plan(
        requirement, object_type=object_type,
        max_scenarios=max_scenarios, llm=client,
    )

    # 3. Scenario Plan
    print("\n[3/8] Scenario Plan")
    _print_plan(output.plan)

    # 4. Generator
    print("\n[4/8] Generator")
    print("  正在生成测试用例 ...")
    output.cases = generate_cases(output.plan, id_prefix=id_prefix, llm=client)
    print(f"  生成 {len(output.cases)} 个用例: {[c.get('id') for c in output.cases]}")

    # 5-7. Deterministic Gates
    gate = DeterministicGate()
    output.gate = gate.check(output.cases, plan=output.plan, object_type=object_type)
    _print_gate_stage(5, "Schema", output.gate)
    _print_gate_stage(6, "Coverage", output.gate)
    _print_gate_stage(7, "Domain", output.gate)

    # 8. 即使 Gate 失败也打印原始生成结果，便于定位模型输出问题。
    print("\n[8/8] Generated Cases")
    print(json.dumps(output.cases, ensure_ascii=False, indent=2))

    # 落盘：仅整批通过时
    if output.gate.passed and not dry_run:
        out_path = out_path or os.path.join("cases", f"{id_prefix.lower().rstrip('_')}_generated.json")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(output.cases, f, ensure_ascii=False, indent=2)
        output.output_path = out_path
        print(f"\n  ✅ 已写入: {out_path}（{len(output.cases)} 个用例）")
    elif output.gate.passed:
        print("\n  ✅ 全部门禁通过；dry-run 模式未写入文件。")
    elif not output.gate.passed:
        print("\n  ⚠️  未通过门禁，不落盘。请修正后重试（可调整需求描述或降低场景数量）。")

    return output


def _print_plan(plan: dict):
    print(f"  需求: {plan.get('requirement', '')}")
    print(f"  功能点: {plan.get('features', [])}")
    print(f"  风险点: {plan.get('risks', [])}")
    print(f"  测试维度: {plan.get('dimensions', [])}")
    scenarios = plan.get("scenarios", [])
    print(f"  场景 ({len(scenarios)}):")
    for sc in scenarios:
        print(f"    - [{sc.get('id')}] {sc.get('name')} "
              f"({sc.get('priority')}) : {sc.get('intent')}")


def _print_gate_stage(stage: int, gate_name: str, result: GateResult):
    """分别打印各道门禁结果，让通过和失败路径都可观察。"""
    issues = [issue for issue in result.issues if issue.gate == gate_name]
    status = "FAIL" if issues else "PASS"
    print(f"\n[{stage}/8] {gate_name} Gate  {status}")
    for issue in issues:
        print(f"  - {issue.case_id}: {issue.message}")
