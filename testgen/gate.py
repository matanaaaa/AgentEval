"""
Deterministic Gate

AI 生成的用例必须逐一通过确定性门禁，才允许进入 AgentEval。三道门：

1. Schema Gate  —— 复用项目 case_schema.validate_cases：
   type 合法、single 只 1 轮、字段无拼错（extra=forbid）、ID 不重复。

2. Coverage Gate —— 对照 Scenario Plan：
   - 每个 scenario 是否都有对应 case（按 metadata.scenario 关联 name/id）
   - requirement 是否被覆盖（至少要有 case）

3. Domain Gate  —— 最小 CRM 业务规则：
   - tools_required / tools_forbidden 只能用合法 tool 名
   - fields / field_constraints / state_assertion 里的 apikey 必须是该对象合法字段
   - 禁止的业务组合（如 business_success:true 却不调 save_record）

门禁是「确定性」的：不调用任何模型，同样输入永远同样结论。
"""

from dataclasses import dataclass, field
from typing import List

from case_schema import validate_cases, CaseValidationError


# ============================================================
# 最小 CRM 领域规则（可按需扩充）
# ============================================================
LEGAL_TOOLS = {"save_record"}

# 各对象类型的合法字段 apikey 白名单
LEGAL_FIELDS = {
    "account": {
        "accountName", "entityType", "customerLevel", "accountSource",
        "region", "province", "city", "district", "address",
        "employeeCount", "salesAmount", "registeredCapital",
        "foundDate", "listedDate", "phone", "email", "website", "remark",
        # 存在但通常不可提取/只读的字段：字段名合法，可提取性由用例的
        # absent 约束表达，不应被白名单拦掉
        "weibo", "fax",
    },
    "contact": {
        "contactName", "gender", "mobile", "email", "phone", "depart",
        "post", "contactRole", "entityType", "company", "remark",
    },
}


@dataclass
class GateIssue:
    """单条门禁违规"""
    gate: str          # Schema / Coverage / Domain
    case_id: str       # 涉及的用例（Coverage 的全局项用 "<batch>"）
    message: str


@dataclass
class GateResult:
    """门禁总结果"""
    passed: bool = False
    issues: List[GateIssue] = field(default_factory=list)
    passed_cases: List[dict] = field(default_factory=list)  # 通过的用例（仅 Schema+Domain 无违规者）

    def format(self) -> str:
        if self.passed:
            return f"✅ Deterministic Gate 通过（{len(self.passed_cases)} 个用例）"
        lines = [f"❌ Deterministic Gate 失败（{len(self.issues)} 项）:"]
        for it in self.issues:
            lines.append(f"  [{it.gate}] {it.case_id}: {it.message}")
        return "\n".join(lines)


class DeterministicGate:
    """三道确定性门禁"""

    def check(self, cases: List[dict], plan: dict = None,
              object_type: str = "account") -> GateResult:
        result = GateResult()

        # ---- Schema Gate ----
        schema_bad_ids = set()
        try:
            validate_cases(cases)
        except CaseValidationError as e:
            for line in e.errors:
                result.issues.append(GateIssue("Schema", self._id_from_error(line), line.strip()))
            # 记录哪些 case 有 schema 问题（无法可靠定位到 id 时按整体处理）
            schema_bad_ids = {self._id_from_error(line) for line in e.errors}

        # ---- Coverage Gate ----
        if plan:
            self._check_coverage(cases, plan, result)

        # ---- Domain Gate ----
        legal_fields = LEGAL_FIELDS.get(object_type, set())
        for case in cases:
            self._check_domain(case, legal_fields, result)

        # 通过的用例：不涉及任何 issue 的
        bad_ids = {it.case_id for it in result.issues} | schema_bad_ids
        result.passed_cases = [c for c in cases if c.get("id", "<no-id>") not in bad_ids]
        result.passed = len(result.issues) == 0
        return result

    # ------------------------------------------------------------
    # Domain Gate
    # ------------------------------------------------------------
    def _check_domain(self, case: dict, legal_fields: set, result: GateResult):
        cid = case.get("id", "<no-id>")
        evaluation = case.get("evaluation", {}) or {}

        # 1. tool 名合法性
        for key in ("tools_required", "tools_forbidden"):
            for tool in evaluation.get(key, []) or []:
                if tool not in LEGAL_TOOLS:
                    result.issues.append(GateIssue(
                        "Domain", cid,
                        f"{key} 含非法 tool 名 '{tool}'（合法: {sorted(LEGAL_TOOLS)}）"
                    ))

        # 2. 字段 apikey 合法性（evaluation.fields / field_constraints）
        if legal_fields:
            for apikey in (evaluation.get("fields", {}) or {}):
                if apikey not in legal_fields:
                    result.issues.append(GateIssue(
                        "Domain", cid, f"evaluation.fields 含非法字段 '{apikey}'"))
            for apikey in (evaluation.get("field_constraints", {}) or {}):
                if apikey not in legal_fields:
                    result.issues.append(GateIssue(
                        "Domain", cid, f"field_constraints 含非法字段 '{apikey}'"))

            # 轮次 state_assertion 里的字段
            for turn in case.get("turns", []) or []:
                sa = (turn.get("expected", {}) or {}).get("state_assertion", {}) or {}
                for apikey in list((sa.get("fields", {}) or {}).keys()) + list(sa.get("absent", []) or []):
                    if apikey not in legal_fields:
                        result.issues.append(GateIssue(
                            "Domain", cid,
                            f"{turn.get('turn_id','?')} state_assertion 含非法字段 '{apikey}'"))

        # 3. 禁止的业务组合
        biz = evaluation.get("business_success", True)
        tools_required = evaluation.get("tools_required", []) or []
        if biz is True and "save_record" not in tools_required:
            result.issues.append(GateIssue(
                "Domain", cid,
                "business_success=true 却未在 tools_required 声明 save_record"))
        if biz is False and "save_record" in tools_required:
            result.issues.append(GateIssue(
                "Domain", cid,
                "business_success=false 却要求调用 save_record（矛盾组合）"))

    # ------------------------------------------------------------
    # Coverage Gate
    # ------------------------------------------------------------
    def _check_coverage(self, cases: List[dict], plan: dict, result: GateResult):
        # requirement 覆盖：至少要有 case
        if plan.get("requirement") and not cases:
            result.issues.append(GateIssue(
                "Coverage", "<batch>", "requirement 未被任何用例覆盖（生成结果为空）"))
            return

        # scenario 覆盖：每个 plan.scenarios 是否有 case 关联到
        covered = set()
        for c in cases:
            scenario_ref = (c.get("metadata", {}) or {}).get("scenario", "")
            if scenario_ref:
                covered.add(str(scenario_ref))

        for sc in plan.get("scenarios", []) or []:
            sid = str(sc.get("id", ""))
            sname = str(sc.get("name", ""))
            # name 或 id 命中任一即视为覆盖
            if sid and sid in covered:
                continue
            if sname and sname in covered:
                continue
            # 宽松匹配：metadata.scenario 里包含 id/name
            if any((sid and sid in ref) or (sname and sname in ref) for ref in covered):
                continue
            result.issues.append(GateIssue(
                "Coverage", "<batch>",
                f"场景未覆盖: {sid or sname}（{sc.get('intent','')}）"))

    @staticmethod
    def _id_from_error(error_line: str) -> str:
        """从 case_schema 错误行 '  [CASE_ID] loc: msg' 里抽出 CASE_ID。"""
        import re
        m = re.search(r"\[([^\]]+)\]", error_line)
        return m.group(1) if m else "<unknown>"
