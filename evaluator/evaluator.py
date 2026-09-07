"""
五层 Agent 评测器

Layer 1: Skill 路由 - 必需 Skill 是否全部执行
Layer 2: Tool 调用 - Tool 名称、参数和状态是否正确
Layer 3: 字段抽取 - 抽取字段的 Precision / Recall / F1
Layer 4: 业务结果 - 是否真正创建成功并通过业务回查
Layer 5: 轮次状态 - 多轮改写和删除后的状态是否一致（由 main 追加）
"""

import re
from dataclasses import dataclass, field
from typing import Optional

import config
from runner.sse_parser import AgentTrace


# ============================================================
# 业务字段空间定义
# 只有在这个集合中的字段才会被计入 extra（多抽取惩罚）
# 系统元数据字段（run_id, created_at 等）不参与评测
# ============================================================
BUSINESS_FIELDS = {
    # 联系人
    "contactName", "gender", "mobile", "email", "phone", "depart", "post",
    "entityType", "contactRole", "company", "companyName", "address",
    "city", "province", "country", "zipCode", "fax", "website", "wechat",
    "qq", "title", "birthday", "remark", "source", "industry",
    # 客户
    "customerName", "customerType", "customerLevel", "customerSource",
    "customerIndustry", "customerScale", "customerAddress",
    # 通用
    "owner", "ownerName", "description", "tags", "status",
}

# 系统/元数据字段，明确排除不计入 extra
SYSTEM_FIELDS = {
    "run_id", "record_id", "created_at", "updated_at", "created_by",
    "updated_by", "object_apikey", "tenant_id", "org_id", "id",
    "_id", "version", "is_deleted", "delete_time",
}


# ============================================================
# 字段值映射表
# Agent 返回的是内部编码，期望值是人类可读的中文
# ============================================================
FIELD_VALUE_MAPPINGS = {
    "gender": {
        # 内部值 → 可接受的期望值
        "1": ["男", "男性", "先生"],
        "2": ["女", "女性", "女士"],
        "0": ["未知"],
    },
    "entityType": {
        "defaultBusiType": ["默认业务类型", "默认"],
    },
    "contactRole": {
        # 编码以 CRM labelKey `XdMDGPickLst.projectRole.N` 为准（已实测）：
        #   2 -> 审批者, 3 -> 评估者, 5 -> 权力支持者
        "1": ["决策者"],
        "2": ["审批者"],
        "3": ["评估者"],
        "4": ["权力支持者"],
        "5": ["权力支持者"],
    },
}

# 通用映射：某些字段的内部 ID 包含关键词时视为匹配
FIELD_KEYWORD_MAPPINGS = {
    "entityType": {
        "特殊": ["special", "特殊"],
        "默认业务类型": ["default", "默认"],
    }
}


@dataclass
class LayerResult:
    """单层评测结果"""
    layer: str
    passed: bool
    expected: str = ""
    actual: str = ""
    detail: str = ""


@dataclass
class FieldMetrics:
    """字段级评测指标"""
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    matched_fields: list = field(default_factory=list)
    missing_fields: list = field(default_factory=list)
    extra_fields: list = field(default_factory=list)
    wrong_fields: list = field(default_factory=list)


@dataclass
class EvalResult:
    """完整评测结果"""
    case_id: str = ""
    layers: list = field(default_factory=list)
    field_metrics: Optional[FieldMetrics] = None
    duration_ms: int = 0
    cost_credits: float = 0.0
    overall_pass: bool = False
    # 每轮的链路关联信息（run_id / message_id / 日志平台深链），由 main.run_case 填充
    trace_refs: list = field(default_factory=list)
    # 运行态指标（由 main.run_case 填充，供性能层聚合 Request/Tool 成功率与 Timeout 率）
    request_ok: bool = True        # HTTP 请求是否成功拿到响应（含重试后）
    timed_out: bool = False        # 是否有任一轮因单轮超时/未收到 RUN_FINISHED 而不完整
    tool_total: int = 0            # 本用例观察到的 Tool 调用总数
    tool_success: int = 0          # 其中 status 非 error 的数量

    def summary(self) -> dict:
        """输出摘要字典"""
        return {
            "case_id": self.case_id,
            "overall_pass": self.overall_pass,
            "layers": {lr.layer: lr.passed for lr in self.layers},
            # 除了三个指标，还要落明细：光有 P=0.5 无法回答“哪个字段错了”，
            # 而报告是排查失败的第一现场，不该逼人回去翻控制台。
            "field_metrics": {
                "precision": self.field_metrics.precision,
                "recall": self.field_metrics.recall,
                "f1": self.field_metrics.f1,
                "matched_fields": self.field_metrics.matched_fields,
                "missing_fields": self.field_metrics.missing_fields,
                "extra_fields": self.field_metrics.extra_fields,
                "wrong_fields": self.field_metrics.wrong_fields,
            } if self.field_metrics else None,
            "layer_details": [
                {
                    "layer": lr.layer,
                    "passed": lr.passed,
                    "expected": lr.expected,
                    "actual": lr.actual,
                    "detail": lr.detail,
                }
                for lr in self.layers
            ],
            "duration_ms": self.duration_ms,
            "cost_credits": self.cost_credits,
            "trace_refs": self.trace_refs,
            "request_ok": self.request_ok,
            "timed_out": self.timed_out,
            "tool_total": self.tool_total,
            "tool_success": self.tool_success,
        }


class AgentEvaluator:
    """五层 Agent 评测器"""

    def evaluate(self, trace: AgentTrace, expected: dict, case_id: str = "") -> EvalResult:
        """
        执行五层评测。

        Args:
            trace: SSEParser 解析出的 AgentTrace
            expected: 测试用例中的期望值
            case_id: 用例 ID

        Returns:
            EvalResult
        """
        result = EvalResult(case_id=case_id)
        result.duration_ms = trace.total_duration_ms
        result.cost_credits = trace.cost_credits

        # Layer 1: Skill 路由
        result.layers.append(self._eval_skill(trace, expected))

        # Layer 2: Tool 调用
        result.layers.append(self._eval_tools(trace, expected))

        # Layer 3: 字段抽取
        layer3, field_metrics = self._eval_fields(trace, expected)
        result.layers.append(layer3)
        result.field_metrics = field_metrics

        # Layer 4: 业务结果
        result.layers.append(self._eval_business(trace, expected))

        # 总体判断
        result.overall_pass = all(lr.passed for lr in result.layers)

        return result

    def _eval_skill(self, trace: AgentTrace, expected: dict) -> LayerResult:
        """
        Layer 1: Skill 路由准确性

        判定依据是「期望的 Skill 是否出现在本次执行观察到的 Skill 集合中」，
        而不是「等于第一个 Skill」。原因：意图切换和跨 Skill 编排类用例
        （先查客户再建联系人）天然会经过多个 Skill，用相等判断必然误判。

        两种期望：
            skill           单个必需 Skill
            expected_skills 多个必需 Skill（子集校验，顺序无关）
        两者可同时存在，取并集。
        """
        expected_skill = expected.get("skill", "")
        expected_skills = list(expected.get("expected_skills", []) or [])
        if expected_skill and expected_skill not in expected_skills:
            expected_skills.append(expected_skill)

        observed = self._observed_skill_keys(trace)

        if not expected_skills:
            return LayerResult(
                layer="L1_Skill路由",
                passed=True,
                expected="无 Skill 断言",
                actual=str(observed),
                detail="用例未声明期望 Skill，跳过",
            )

        if not observed:
            return LayerResult(
                layer="L1_Skill路由",
                passed=False,
                expected=str(expected_skills),
                actual="无",
                detail="未检测到任何 Skill 执行",
            )

        missing = [s for s in expected_skills if s not in observed]
        passed = not missing

        primary = trace.skill
        status_detail = (
            f"status={primary.status}, match_method={primary.match_method}"
            if primary else ""
        )
        detail = status_detail if passed else f"缺失 Skill: {missing}"
        if not passed and status_detail:
            detail += f"｜{status_detail}"

        return LayerResult(
            layer="L1_Skill路由",
            passed=passed,
            expected=str(expected_skills),
            actual=str(observed),
            detail=detail,
        )

    @staticmethod
    def _observed_skill_keys(trace: AgentTrace) -> list:
        """本次执行观察到的 Skill key 列表（兼容只设置了 trace.skill 的旧数据）"""
        if trace.skills:
            return [s.skill_key for s in trace.skills if s.skill_key]
        if trace.skill and trace.skill.skill_key:
            return [trace.skill.skill_key]
        return []

    def _eval_tools(self, trace: AgentTrace, expected: dict) -> LayerResult:
        """
        Layer 2: Tool 调用准确性

        支持两种评测模式：
        1. 简单模式（向后兼容）：tools_required / tools_forbidden，只检查 tool name
        2. 参数模式：expected_tool_calls，检查 tool name + arguments subset match + status

        expected_tool_calls 格式:
        [
            {
                "name": "create_contact",
                "arguments": {"name": "张三", "company": "腾讯"},
                "status": "complete"  # 可选，默认不检查
            }
        ]
        """
        expected_tool_calls = expected.get("expected_tool_calls", [])

        # 如果定义了 expected_tool_calls，使用参数级评测
        if expected_tool_calls:
            return self._eval_tools_with_args(trace, expected_tool_calls)

        # === 简单模式（向后兼容）===
        # 必须调用的核心 tool
        required_tools = expected.get("tools_required", [])
        # 兼容旧格式
        if not required_tools:
            required_tools = expected.get("tools", [])
        if isinstance(required_tools, str):
            required_tools = [required_tools]
        if not required_tools and "tool" in expected:
            required_tools = [expected["tool"]]

        # 不允许出现的 tool
        forbidden_tools = expected.get("tools_forbidden", [])

        actual_tools = [t.tool_name for t in trace.tools]

        # 检查必须的 tool 是否被调用（只要出现就算通过，不要求是唯一的）
        missing = [t for t in required_tools if t not in actual_tools]
        forbidden_found = [t for t in forbidden_tools if t in actual_tools]

        passed = len(missing) == 0 and len(forbidden_found) == 0

        detail_parts = []
        if missing:
            detail_parts.append(f"缺失: {missing}")
        if forbidden_found:
            detail_parts.append(f"禁止调用但出现: {forbidden_found}")
        if not detail_parts:
            detail_parts.append("核心 Tool 均已调用")

        return LayerResult(
            layer="L2_Tool调用",
            passed=passed,
            expected=f"必须: {required_tools}",
            actual=str(actual_tools),
            detail="; ".join(detail_parts)
        )

    def _eval_tools_with_args(self, trace: AgentTrace, expected_tool_calls: list) -> LayerResult:
        """
        参数级 Tool 评测。

        对每个 expected_tool_call，在 trace.tools 中找到同名 tool，
        然后做 arguments subset match（期望的参数必须是实际参数的子集且值匹配）。

        Returns:
            LayerResult
        """
        results = []  # [(tool_name, passed, detail)]

        for exp_call in expected_tool_calls:
            exp_name = exp_call.get("name", "")
            exp_args = exp_call.get("arguments", {})
            exp_status = exp_call.get("status")  # 可选

            # 找到实际 trace 中同名的 tool（取最后一个，因为可能多次调用）
            matching_tools = [t for t in trace.tools if t.tool_name == exp_name]

            if not matching_tools:
                results.append((exp_name, False, "未调用"))
                continue

            # 取最后一次调用（多次调用的情况下以最终结果为准）
            actual_tool = matching_tools[-1]

            # 检查 status
            if exp_status and actual_tool.status != exp_status:
                results.append((exp_name, False, f"status 期望 {exp_status}，实际 {actual_tool.status}"))
                continue

            # 检查 arguments subset match
            if exp_args:
                arg_mismatches = self._check_args_subset(exp_args, actual_tool.arguments)
                if arg_mismatches:
                    results.append((exp_name, False, f"参数不匹配: {arg_mismatches}"))
                    continue

            results.append((exp_name, True, "匹配"))

        passed = all(r[1] for r in results)
        failed_tools = [(name, detail) for name, ok, detail in results if not ok]

        detail_parts = []
        if failed_tools:
            for name, detail in failed_tools:
                detail_parts.append(f"{name}: {detail}")
        else:
            detail_parts.append("所有 Tool 调用及参数匹配通过")

        return LayerResult(
            layer="L2_Tool调用",
            passed=passed,
            expected=f"{len(expected_tool_calls)} 个 Tool 调用",
            actual=f"匹配 {sum(1 for _, ok, _ in results if ok)}/{len(results)}",
            detail="; ".join(detail_parts)
        )

    def _check_args_subset(self, expected_args: dict, actual_args: dict) -> list:
        """
        检查 expected_args 是否为 actual_args 的子集（值匹配）。

        使用与 _field_match 类似的规范化逻辑。

        Returns:
            不匹配项列表，空列表表示全部匹配
        """
        mismatches = []

        for key, exp_val in expected_args.items():
            if key not in actual_args:
                mismatches.append({"field": key, "expected": exp_val, "actual": "<缺失>"})
            else:
                actual_val = actual_args[key]
                if not self._arg_value_match(actual_val, exp_val):
                    mismatches.append({"field": key, "expected": exp_val, "actual": actual_val})

        return mismatches

    def _arg_value_match(self, actual, expected) -> bool:
        """
        Tool 参数值匹配。

        规则：
        - 类型相同：直接比较
        - str vs str：strip() 后比较
        - int/float 宽松："1" == 1
        - None vs None：True
        - 其他：str() 后 strip 比较
        """
        if actual is None and expected is None:
            return True
        if actual is None or expected is None:
            return False

        # 同类型直接比较
        if type(actual) == type(expected):
            if isinstance(actual, str):
                return actual.strip() == expected.strip()
            return actual == expected

        # 数字宽松匹配
        try:
            if float(str(actual)) == float(str(expected)):
                return True
        except (ValueError, TypeError):
            pass

        # 最终：str 比较
        return str(actual).strip() == str(expected).strip()

    def _eval_fields(self, trace: AgentTrace, expected: dict) -> tuple:
        """
        Layer 4: 字段抽取正确性（支持值映射）

        公式：
            precision = matched / (matched + wrong + extra)
            recall    = matched / (matched + wrong + missing)
            F1        = 2 * P * R / (P + R)

        extra 只计算属于 BUSINESS_FIELDS 的多抽取字段，系统元数据不算。

        Returns:
            (LayerResult, FieldMetrics)
        """
        expected_fields = expected.get("fields", {})
        field_constraints = expected.get("field_constraints", {})

        if not expected_fields and not field_constraints:
            return LayerResult(
                layer="L3_字段抽取",
                passed=True,
                detail="无字段断言，跳过"
            ), None

        # 从 business_result 中提取实际字段
        actual_fields = {}
        actual_fields_label = {}  # 同时保存 label 用于匹配
        if trace.business_result and trace.business_result.extracted_fields:
            for field_item in trace.business_result.extracted_fields:
                apikey = field_item.get("apikey", "")
                value = field_item.get("value", "")
                label = field_item.get("label", "")
                if apikey:
                    actual_fields[apikey] = value
                    actual_fields_label[apikey] = label

        # 计算指标
        matched = []
        missing = []
        wrong = []

        for key, expected_val in expected_fields.items():
            if key not in actual_fields and key not in actual_fields_label:
                missing.append(key)
            else:
                actual_val = actual_fields.get(key, "")
                actual_label = actual_fields_label.get(key, "")
                if self._field_match(actual_val, expected_val, key) or self._field_match(actual_label, expected_val, key):
                    matched.append(key)
                else:
                    wrong.append({
                        "field": key,
                        "expected": expected_val,
                        "actual": actual_val,
                        "actual_label": actual_label,
                    })

        # 字段存在性约束（absent / present）。
        # absent 用于「删除字段」这类用例：Agent 抽过又被要求删掉，最终不该带上。
        constraint_violations = self._check_field_constraints(field_constraints, actual_fields)
        wrong.extend(constraint_violations)

        # 多抽取的业务字段（排除系统字段和非业务字段）
        # 被 field_constraints 显式约束的字段不算“多抽”，它已经单独判过了
        extra = [
            k for k in actual_fields
            if k not in expected_fields and k not in field_constraints
            and k in BUSINESS_FIELDS and k not in SYSTEM_FIELDS
        ]

        # Precision: matched / (matched + wrong + extra)
        precision_denom = len(matched) + len(wrong) + len(extra)
        precision = len(matched) / precision_denom if precision_denom > 0 else 0.0

        # Recall: matched / (matched + wrong + missing)
        recall_denom = len(matched) + len(wrong) + len(missing)
        recall = len(matched) / recall_denom if recall_denom > 0 else 0.0

        # F1
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        metrics = FieldMetrics(
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
            matched_fields=matched,
            missing_fields=missing,
            extra_fields=extra,
            wrong_fields=wrong,
        )

        # 通过条件：F1 >= 0.7 且无错误字段（含约束违反）
        # 只有约束、没有值断言时，F1 分母为 0 → 只看约束是否违反
        if expected_fields:
            passed = f1 >= 0.7 and len(wrong) == 0
        else:
            passed = len(constraint_violations) == 0

        # 只有 field_constraints、没有值断言的用例（如「缺必填字段」negative case），
        # 不产出 FieldMetrics：它的 F1 恒为 0 但毫无意义，若纳入平均 F1 会把
        # 门禁指标误拉到 0。返回 None 让这类用例不进入 F1 统计。
        return_metrics = metrics if expected_fields else None

        if expected_fields:
            detail = f"P={metrics.precision} R={metrics.recall} F1={metrics.f1}"
        else:
            detail = "仅字段存在性约束（不计 F1）"
        if constraint_violations:
            detail += f"｜字段约束违反 {len(constraint_violations)} 项"

        total_expected = len(expected_fields)
        expected_desc = f"{total_expected} 个字段"
        if field_constraints:
            expected_desc += f" + {len(field_constraints)} 项约束"

        return LayerResult(
            layer="L3_字段抽取",
            passed=passed,
            expected=expected_desc,
            actual=f"匹配 {len(matched)}, 缺失 {len(missing)}, 错误 {len(wrong)}, 多抽 {len(extra)}",
            detail=detail,
        ), return_metrics

    def _check_field_constraints(self, constraints: dict, actual_fields: dict) -> list:
        """
        检查字段存在性约束。

        absent  → 字段不得出现，且不得有非空值
        present → 字段必须出现且值非空

        Returns:
            违反项列表，结构与 wrong_fields 一致，便于失败归因直接复用
        """
        violations = []

        for key, rule in (constraints or {}).items():
            actual_val = actual_fields.get(key)
            has_value = key in actual_fields and str(actual_val).strip() not in ("", "None")

            if rule == "absent" and has_value:
                violations.append({
                    "field": key,
                    "expected": "<absent>",
                    "actual": actual_val,
                    "actual_label": "",
                })
            elif rule == "present" and not has_value:
                violations.append({
                    "field": key,
                    "expected": "<present>",
                    "actual": actual_val if key in actual_fields else "<缺失>",
                    "actual_label": "",
                })

        return violations

    def check_state_assertion(self, trace: AgentTrace, assertion: dict) -> dict:
        """
        轮次级状态断言：检查这一轮结束时 Agent 已抽取的字段状态。

        与 L3 的区别：L3 判的是整个用例最终的字段抽取质量，这里判的是
        某一轮的中间状态——多轮改写/删除类用例只有逐轮检查才能发现
        「第 4 轮明明要求删手机号，但状态里还留着」这种问题。

        Args:
            trace: 该轮的 AgentTrace
            assertion: {"fields": {...}, "absent": [...]}

        Returns:
            {"passed": bool, "detail": str, "violations": [...]}
        """
        expected_fields = (assertion or {}).get("fields", {}) or {}
        absent_keys = (assertion or {}).get("absent", []) or []

        actual_values = {}
        actual_labels = {}
        if trace.business_result and trace.business_result.extracted_fields:
            for item in trace.business_result.extracted_fields:
                apikey = item.get("apikey", "")
                if apikey:
                    actual_values[apikey] = item.get("value", "")
                    actual_labels[apikey] = item.get("label", "")

        violations = []

        for key, expected_val in expected_fields.items():
            if key not in actual_values and key not in actual_labels:
                violations.append(f"{key} 缺失（期望 {expected_val}）")
                continue
            if not (
                self._field_match(actual_values.get(key, ""), expected_val, key)
                or self._field_match(actual_labels.get(key, ""), expected_val, key)
            ):
                violations.append(
                    f"{key} 值不符（期望 {expected_val}，"
                    f"实际 {actual_values.get(key, '')}/{actual_labels.get(key, '')}）"
                )

        for key in absent_keys:
            value = actual_values.get(key)
            if key in actual_values and str(value).strip() not in ("", "None"):
                violations.append(f"{key} 应已删除，实际仍为 {value}")

        passed = not violations
        return {
            "passed": passed,
            "detail": "状态断言通过" if passed else "; ".join(violations),
            "violations": violations,
        }

    def _eval_business(self, trace: AgentTrace, expected: dict) -> LayerResult:
        """
        Layer 4: 业务结果验证

        两步验证：
        1. Agent trace 中 isCreated=true 且 record_id 非空
        2. Database Check: 调 CRM 查询接口确认记录真实存在且字段正确

        支持三种期望：
        - business_success: true  → 必须创建成功 + DB 验证
        - business_success: false → 期望创建失败
        - business_success: "pending" → 允许还在进行中
        """
        expect_success = expected.get("business_success", True)
        expected_object = expected.get("object_type", "")
        expected_fields = expected.get("fields", {})
        require_grounding = bool(expected.get("require_business_grounding", False))

        # "pending" 表示允许未完成
        if expect_success == "pending":
            return LayerResult(
                layer="L4_业务结果",
                passed=True,
                expected="pending（允许未完成）",
                actual=f"business_result={'有' if trace.business_result else '无'}",
                detail="多轮对话中间态，跳过业务结果检查"
            )

        if not trace.business_result:
            passed = not expect_success
            return LayerResult(
                layer="L4_业务结果",
                passed=passed,
                expected=f"success={expect_success}",
                actual="无业务结果返回",
                detail="Agent 未返回业务数据（可能在等用户确认或信息不全）"
            )

        biz = trace.business_result
        actual_success = biz.is_created and biz.created_record_id is not None

        # 基础判断：Agent 声称是否成功
        passed = actual_success == expect_success

        # 额外检查对象类型
        if passed and expected_object:
            if biz.object_apikey != expected_object:
                passed = False

        # Database Check：如果 Agent 声称创建成功，查数据库验证
        db_detail = ""
        if actual_success and expect_success and biz.created_record_id:
            db_result = self._db_check(
                record_id=biz.created_record_id,
                entity_type=biz.object_apikey or expected_object,
                expected_fields=expected_fields,
            )
            if db_result is not None:
                if db_result.get("query_error"):
                    passed = False
                    db_detail = f"DB Check: 查询失败 error={db_result['query_error']}"
                elif not db_result["exists"]:
                    passed = False
                    db_detail = "DB Check: 记录不存在！Agent 声称成功但数据库无记录"
                elif not db_result["fields_correct"]:
                    passed = False
                    mismatched = db_result.get("mismatched", [])
                    missing = db_result.get("missing_in_record", [])
                    db_detail = f"DB Check: 字段不一致 mismatched={mismatched}, missing={missing}"
                else:
                    db_detail = f"DB Check: 验证通过，记录存在且字段正确 (id={biz.created_record_id})"
            else:
                db_detail = "DB Check: 跳过（无法连接或未配置）"
                if require_grounding:
                    passed = False
                    db_detail += "；本用例要求 Business Grounding，禁止跳过"

        detail = f"message={biz.message}"
        if db_detail:
            detail += f" | {db_detail}"

        return LayerResult(
            layer="L4_业务结果",
            passed=passed,
            expected=(
                f"success={expect_success}, object={expected_object}, "
                f"grounding_required={require_grounding}"
            ),
            actual=f"isCreated={biz.is_created}, record_id={biz.created_record_id}, object={biz.object_apikey}",
            detail=detail,
        )

    def _db_check(self, record_id: int, entity_type: str, expected_fields: dict) -> dict:
        """
        执行 Database Check（懒加载 DBChecker 避免循环导入）。
        返回 None 表示无法执行检查；是否影响结果由 require_business_grounding 决定。
        返回 dict 表示检查结果。
        """
        if not getattr(config, "DB_CHECK_ENABLED", True):
            return None

        try:
            from evaluator.db_checker import DBChecker
            checker = DBChecker()
            result = checker.verify_record(record_id, entity_type, expected_fields)
            # 如果 record 为 None 且 query 本身出了问题，视为无法执行
            if result.get("record") is None and not result.get("exists"):
                # 区分"确认不存在"和"查询失败"
                # 如果能成功调通接口但返回 0 条，说明确认不存在
                # 这里依赖 DBChecker 内部的异常处理
                pass
            return result
        except ImportError:
            return None
        except Exception as e:
            print(f"    [DB Check] 执行异常（不影响评测）: {e}")
            return None

    def _field_match(self, actual, expected, field_key: str = "") -> bool:
        """
        字段值匹配（严格规范化，不再使用宽泛的双向 contains）

        规范化规则：
        - None ≠ ""
        - "" ≠ "张三"
        - "北京" ≠ "北京市海淀区"
        - 手机号：去空格、"-"
        - 邮箱：lower()
        - 字符串：strip()
        - 布尔：统一 true/false
        - 数字："1" == 1

        匹配顺序：
        1. 规范化后完全相等
        2. 通过映射表转换后匹配
        3. 关键词映射匹配（仅限已定义的字段）
        """
        # None 和空值严格区分
        if actual is None and expected is None:
            return True
        if actual is None or expected is None:
            return False

        actual_str = str(actual).strip()
        expected_str = str(expected).strip()

        # 空串不等于非空串
        if actual_str == "" and expected_str != "":
            return False
        if actual_str != "" and expected_str == "":
            return False

        # 1. 规范化后完全匹配
        norm_actual = self._normalize_value(actual_str, field_key)
        norm_expected = self._normalize_value(expected_str, field_key)

        if norm_actual == norm_expected:
            return True

        # 2. 通过映射表匹配（如 gender: 1 → 男）
        if field_key and field_key in FIELD_VALUE_MAPPINGS:
            mapping = FIELD_VALUE_MAPPINGS[field_key]
            if actual_str in mapping:
                acceptable = mapping[actual_str]
                if expected_str in acceptable:
                    return True

        # 3. 关键词映射匹配（仅限已定义的 entityType 等字段）
        if field_key and field_key in FIELD_KEYWORD_MAPPINGS:
            keyword_map = FIELD_KEYWORD_MAPPINGS[field_key]
            if expected_str in keyword_map:
                keywords = keyword_map[expected_str]
                if any(kw in actual_str.lower() for kw in keywords):
                    return True

        # 4. 数字类型宽松匹配: "1" == 1
        try:
            if float(norm_actual) == float(norm_expected):
                return True
        except (ValueError, TypeError):
            pass

        # 5. 布尔统一
        bool_map = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}
        if norm_actual.lower() in bool_map and norm_expected.lower() in bool_map:
            if bool_map[norm_actual.lower()] == bool_map[norm_expected.lower()]:
                return True

        return False

    def _normalize_value(self, value: str, field_key: str = "") -> str:
        """
        按字段类型进行规范化处理
        """
        if not value:
            return value

        # 手机号/电话：去空格、去 "-"
        if field_key in ("mobile", "phone", "fax"):
            return re.sub(r'[\s\-\(\)]+', '', value)

        # 邮箱：lower + strip
        if field_key in ("email",):
            return value.strip().lower()

        # 英文职位缩写不区分大小写，例如 VP / vp。
        # 仅作用于职位字段，避免改变姓名、内部编码等字段的严格语义。
        if field_key in ("post",):
            return value.strip().casefold()

        # 通用：strip
        return value.strip()
