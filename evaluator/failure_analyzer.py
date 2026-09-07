"""
P0: Failure Root Cause Analysis（失败归因）

自动分类失败原因，输出可直接提 bug 的结论。

失败分类按层归因：
- L1 Skill 路由  → SKILL_ROUTING_ERROR
- L2 Tool 调用   → TOOL_CALL_ERROR
- L3 字段抽取    → FIELD_MISSING / FIELD_VALUE / FIELD_EXTRA / FIELD_CONSTRAINT
- L4 业务结果    → BUSINESS_NOT_CREATED / DB_RECORD_MISSING / DB_FIELD_MISMATCH
- L5 轮次状态    → STATE_ASSERTION_ERROR
兜底：UNCLASSIFIED_FAILURE（任何失败层都必须产出至少一条归因）

路由失败只产出 SKILL_ROUTING_ERROR，避免同一个 skill_key
判定重复产生 critical 根因。
"""

from dataclasses import dataclass, field
from typing import List
from evaluator.evaluator import EvalResult


@dataclass
class FailureRootCause:
    """单个失败归因"""
    category: str  # 分类 key
    category_label: str  # 中文标签
    severity: str  # critical / major / minor
    summary: str  # 一句话总结
    details: list = field(default_factory=list)  # 详细信息


@dataclass
class FailureReport:
    """用例的失败分析报告"""
    case_id: str
    root_causes: List[FailureRootCause] = field(default_factory=list)
    primary_cause: str = ""  # 主要原因（第一个 critical 或最严重的）

    @property
    def has_failures(self) -> bool:
        return len(self.root_causes) > 0


class FailureAnalyzer:
    """失败归因分析器"""

    # 失败分类定义
    CATEGORIES = {
        "SKILL_ROUTING_ERROR": {"label": "Skill 路由错误", "severity": "critical"},
        "TOOL_CALL_ERROR": {"label": "Tool 调用错误", "severity": "major"},
        "FIELD_MISSING_ERROR": {"label": "字段缺失", "severity": "major"},
        "FIELD_VALUE_ERROR": {"label": "字段值错误", "severity": "major"},
        "FIELD_EXTRA_ERROR": {"label": "多抽字段", "severity": "minor"},
        "FIELD_CONSTRAINT_ERROR": {"label": "字段约束违反", "severity": "major"},
        "BUSINESS_NOT_CREATED": {"label": "业务未创建", "severity": "critical"},
        "DB_RECORD_MISSING": {"label": "数据库记录不存在", "severity": "critical"},
        "DB_FIELD_MISMATCH": {"label": "数据库字段不一致", "severity": "major"},
        "STATE_ASSERTION_ERROR": {"label": "轮次状态错误", "severity": "major"},
        "TEXT_ASSERTION_ERROR": {"label": "文本响应错误", "severity": "major"},
        "TIMEOUT": {"label": "执行超时", "severity": "minor"},
        "UNCLASSIFIED_FAILURE": {"label": "未分类失败", "severity": "major"},
    }

    def analyze(self, result: EvalResult) -> FailureReport:
        """
        分析单个用例的失败原因。

        Args:
            result: 评测结果

        Returns:
            FailureReport
        """
        report = FailureReport(case_id=result.case_id)

        if result.overall_pass:
            return report

        for layer in result.layers:
            if layer.passed:
                continue

            causes = self._analyze_layer(layer, result)

            # 安全网：任何失败层都必须产出至少一条归因。
            # 否则会出现「用例 FAIL 但 failure_analysis 全空」——报告看着像没问题，
            # 实际是分类逻辑没覆盖到这种失败形态。
            if not causes:
                causes = [FailureRootCause(
                    category="UNCLASSIFIED_FAILURE",
                    category_label="未分类失败",
                    severity="major",
                    summary=f"{layer.layer} 失败，但未匹配到已知失败模式",
                    details=[
                        f"期望: {layer.expected}",
                        f"实际: {layer.actual}",
                        f"详情: {layer.detail}",
                        "请在 FailureAnalyzer 中补充该失败形态的分类规则",
                    ],
                )]

            report.root_causes.extend(causes)

        # 确定主要原因（第一个 critical，或第一个 major）
        if report.root_causes:
            critical = [c for c in report.root_causes if c.severity == "critical"]
            report.primary_cause = critical[0].category if critical else report.root_causes[0].category

        return report

    def analyze_batch(self, results: List[EvalResult]) -> dict:
        """
        批量分析，输出失败分类统计。

        Returns:
            {
                "reports": [FailureReport, ...],
                "category_stats": {"SKILL_ROUTING_ERROR": 2, "FIELD_MISSING_ERROR": 5, ...},
                "primary_cause_stats": {"FIELD_MISSING_ERROR": 3, ...},
            }
        """
        reports = []
        category_stats = {}
        primary_cause_stats = {}

        for result in results:
            report = self.analyze(result)
            reports.append(report)

            if report.has_failures:
                # 统计所有出现的失败类型
                for cause in report.root_causes:
                    category_stats[cause.category] = category_stats.get(cause.category, 0) + 1

                # 统计主要原因
                if report.primary_cause:
                    primary_cause_stats[report.primary_cause] = primary_cause_stats.get(report.primary_cause, 0) + 1

        return {
            "reports": reports,
            "category_stats": category_stats,
            "primary_cause_stats": primary_cause_stats,
        }

    def _analyze_layer(self, layer, result: EvalResult) -> List[FailureRootCause]:
        """
        根据失败层分析具体原因。

        用「_ 之前的前缀精确相等」分派，而不是 `"L1" in layer_name`——
        子串匹配会让 L1 命中 L10/L11 这类将来新增的层。
        """
        causes = []
        prefix = layer.layer.split("_", 1)[0]

        if prefix == "L1":
            causes.append(FailureRootCause(
                category="SKILL_ROUTING_ERROR",
                category_label="Skill 路由错误",
                severity="critical",
                summary=f"Skill 路由到 {layer.actual}，期望 {layer.expected}",
                details=[
                    f"期望: {layer.expected}",
                    f"实际: {layer.actual}",
                    f"匹配信息: {layer.detail}",
                    f"可能原因: Skill 路由 Prompt 规则不精确、语义匹配歧义",
                ],
            ))

        elif prefix == "L2":
            causes.append(FailureRootCause(
                category="TOOL_CALL_ERROR",
                category_label="Tool 调用错误",
                severity="major",
                summary=f"Tool 调用不符合预期",
                details=[
                    f"期望: {layer.expected}",
                    f"实际: {layer.actual}",
                    f"详情: {layer.detail}",
                    f"可能原因: Skill 内部 Tool 编排逻辑错误、条件分支判断异常",
                ],
            ))

        elif prefix == "L3":
            # 细分字段错误类型
            if result.field_metrics:
                fm = result.field_metrics

                # 约束违反（field_constraints）与普通值错误混在 wrong_fields 里，
                # 用 expected 的 <absent>/<present> 标记区分，归因措辞完全不同
                constraint_violations = [
                    wf for wf in fm.wrong_fields
                    if wf.get("expected") in ("<absent>", "<present>")
                ]
                value_errors = [
                    wf for wf in fm.wrong_fields
                    if wf.get("expected") not in ("<absent>", "<present>")
                ]

                for cv in constraint_violations:
                    rule = "必须不存在" if cv.get("expected") == "<absent>" else "必须存在"
                    causes.append(FailureRootCause(
                        category="FIELD_CONSTRAINT_ERROR",
                        category_label="字段约束违反",
                        severity="major",
                        summary=f"字段 {cv['field']} {rule}，实际为 \"{cv.get('actual', '')}\"",
                        details=[
                            f"字段: {cv['field']}",
                            f"约束: {rule}",
                            f"实际值: {cv.get('actual', '')}",
                            "可能原因: 删除/清空指令未被执行、多轮改写丢失、必填字段被漏抽",
                        ],
                    ))

                # 只多抽字段也会让 precision 掉到阈值以下并导致 L4 FAIL，
                # 之前这种形态不产出任何归因，报告里就成了「失败但没有原因」
                if fm.extra_fields:
                    causes.append(FailureRootCause(
                        category="FIELD_EXTRA_ERROR",
                        category_label="多抽字段",
                        severity="minor",
                        summary=f"多抽 {len(fm.extra_fields)} 个字段: {', '.join(fm.extra_fields[:5])}",
                        details=[
                            f"多抽字段: {fm.extra_fields}",
                            f"Precision: {fm.precision}（多抽会直接拉低 precision）",
                            "可能原因: Agent 主动补全了用户未提供的字段、"
                            "默认值被当成抽取结果、或用例期望字段列表不完整",
                            "处理建议: 若这些字段本就该抽，把它们补进用例的 fields 断言；"
                            "若不该抽，作为 Prompt 问题提单",
                        ],
                    ))

                if fm.missing_fields:
                    causes.append(FailureRootCause(
                        category="FIELD_MISSING_ERROR",
                        category_label="字段缺失",
                        severity="major",
                        summary=f"缺失 {len(fm.missing_fields)} 个字段: {', '.join(fm.missing_fields[:5])}",
                        details=[
                            f"缺失字段: {fm.missing_fields}",
                            f"Recall: {fm.recall}",
                            f"可能原因: 用户表述中该信息不明确、NLU 抽取能力不足、字段映射规则缺失",
                        ],
                    ))
                if value_errors:
                    for wf in value_errors:
                        causes.append(FailureRootCause(
                            category="FIELD_VALUE_ERROR",
                            category_label="字段值错误",
                            severity="major",
                            summary=f"字段 {wf['field']}: 期望 \"{wf['expected']}\"，实际 \"{wf.get('actual', '')}\"",
                            details=[
                                f"字段: {wf['field']}",
                                f"期望值: {wf['expected']}",
                                f"实际值: {wf.get('actual', '')}",
                                f"实际 label: {wf.get('actual_label', '')}",
                                f"可能原因: NLU 抽取错误、同义词映射缺失、实体消歧失败",
                            ],
                        ))
            else:
                causes.append(FailureRootCause(
                    category="FIELD_MISSING_ERROR",
                    category_label="字段抽取异常",
                    severity="major",
                    summary="无法获取字段指标",
                    details=[f"详情: {layer.detail}"],
                ))

        elif prefix == "L5" and "文本" in layer.layer:
            causes.append(FailureRootCause(
                category="TEXT_ASSERTION_ERROR",
                category_label="文本响应错误",
                severity="major",
                summary=f"Agent 回复未满足文本断言（{layer.actual}）",
                details=[
                    f"期望: {layer.expected}",
                    f"实际: {layer.actual}",
                    f"失败断言: {layer.detail}",
                    "可能原因: 回复遗漏关键信息、包含禁止内容或表达不符合预期",
                ],
            ))

        elif prefix == "L5":
            causes.append(FailureRootCause(
                category="STATE_ASSERTION_ERROR",
                category_label="轮次状态错误",
                severity="major",
                summary=f"轮次状态断言未通过（{layer.actual}）",
                details=[
                    f"期望: {layer.expected}",
                    f"实际: {layer.actual}",
                    f"失败轮次: {layer.detail}",
                    "可能原因: 多轮上下文丢失、字段改写未生效、删除指令未被执行",
                ],
            ))

        elif prefix == "L4":
            detail = layer.detail or ""
            if "记录不存在" in detail:
                causes.append(FailureRootCause(
                    category="DB_RECORD_MISSING",
                    category_label="数据库记录不存在",
                    severity="critical",
                    summary="Agent 声称创建成功，但数据库中无对应记录",
                    details=[
                        f"实际状态: {layer.actual}",
                        f"DB Check: {detail}",
                        f"可能原因: Tool 调用返回了假的 success、CRM 接口异步未落库、record_id 无效",
                    ],
                ))
            elif "字段不一致" in detail:
                causes.append(FailureRootCause(
                    category="DB_FIELD_MISMATCH",
                    category_label="数据库字段不一致",
                    severity="major",
                    summary="记录已创建但字段值与期望不一致",
                    details=[
                        f"实际状态: {layer.actual}",
                        f"DB Check: {detail}",
                        f"可能原因: Agent 抽取正确但写入时字段映射错误、CRM 接口字段转换 bug",
                    ],
                ))
            else:
                causes.append(FailureRootCause(
                    category="BUSINESS_NOT_CREATED",
                    category_label="业务未创建",
                    severity="critical",
                    summary="Agent 未成功完成创建操作",
                    details=[
                        f"期望: {layer.expected}",
                        f"实际: {layer.actual}",
                        f"详情: {detail}",
                        f"可能原因: 必填字段缺失导致 Agent 等待确认、公司/实体不存在、多轮上下文丢失",
                    ],
                ))

        return causes

    def format_report(self, report: FailureReport) -> str:
        """格式化单个失败报告为可读文本"""
        if not report.has_failures:
            return f"  {report.case_id}: PASS"

        lines = [f"\n  Case: {report.case_id}"]
        lines.append(f"  Primary Root Cause: {report.primary_cause}")
        lines.append("")

        for cause in report.root_causes:
            icon = "🔴" if cause.severity == "critical" else ("🟡" if cause.severity == "major" else "🔵")
            lines.append(f"    {icon} [{cause.category_label}] {cause.summary}")
            for detail in cause.details[:3]:  # 最多显示 3 行详情
                lines.append(f"       {detail}")
            lines.append("")

        return "\n".join(lines)
