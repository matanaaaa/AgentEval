"""
质量门禁（Quality Gate）

把 config.THRESHOLDS 里的阈值接到评测结果上，给出明确的通过/失败判定，
供 CI 用非零退出码卡住不合格的 Prompt 改动。

门禁项与阈值 key 的对应：
    skill_accuracy   → L1_Skill路由 通过率
    tool_accuracy    → L2_Tool调用 通过率
    field_f1         → 字段抽取平均 F1
    business_success → L4_业务结果 通过率

L5_轮次状态 不设门禁项：它是附加层，只在用例定义 state_assertion 时才存在，
覆盖率不稳定，拿它算阈值会随用例集变化而漂移。

注：config.THRESHOLDS 里的 slow_call_ms 是「慢调用标注阈值」，用于性能报告里
打标，不作为门禁项——否则一次正常的 60s 多轮对话就会卡住 CI。
"""

from dataclasses import dataclass, field
from typing import List, Optional

import config
from evaluator.evaluator import EvalResult


# 退出码约定（CI 依赖这些语义）
EXIT_OK = 0
EXIT_GATE_FAILED = 1        # 质量门禁未达标
EXIT_REGRESSION = 2         # 回归对比发现退化
EXIT_USAGE_ERROR = 3        # 参数/用例加载错误

# 门禁项定义：(阈值 key, 展示名, 取值方式)
#   layer:<前缀>  → 取该层通过率
#   metric:avg_f1 → 取字段抽取平均 F1
GATE_ITEMS = (
    ("skill_accuracy", "Skill 路由准确率", "layer:L1"),
    ("tool_accuracy", "Tool 调用准确率", "layer:L2"),
    ("field_f1", "字段抽取平均 F1", "metric:avg_f1"),
    ("business_success", "业务成功率", "layer:L4"),
)


@dataclass
class GateCheck:
    """单个门禁项的判定结果"""
    name: str
    threshold_key: str
    actual: Optional[float]  # None 表示无数据
    threshold: float
    passed: bool
    detail: str = ""


@dataclass
class GateReport:
    """门禁总报告"""
    checks: List[GateCheck] = field(default_factory=list)
    total_cases: int = 0
    pass_rate: float = 0.0
    skipped: bool = False       # 是否因无数据而跳过判定
    reason: str = ""            # 跳过或失败的整体原因

    @property
    def passed(self) -> bool:
        if self.skipped:
            return True
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> List[GateCheck]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "skipped": self.skipped,
            "reason": self.reason,
            "total_cases": self.total_cases,
            "pass_rate": round(self.pass_rate, 4),
            "checks": [
                {
                    "name": c.name,
                    "threshold_key": c.threshold_key,
                    "actual": c.actual,
                    "threshold": c.threshold,
                    "passed": c.passed,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }


class QualityGate:
    """按阈值判定评测结果是否达标"""

    def __init__(self, thresholds: dict = None):
        self.thresholds = thresholds if thresholds is not None else config.THRESHOLDS

    def evaluate(self, results: List[EvalResult]) -> GateReport:
        """
        执行门禁判定。

        无数据时（results 为空）标记 skipped 而不是失败——空跑不该被当成退化，
        但会在 reason 里写明，避免静默放行。
        """
        report = GateReport(total_cases=len(results))

        if not results:
            report.skipped = True
            report.reason = "无评测结果，跳过门禁判定"
            return report

        passed_cases = sum(1 for r in results if r.overall_pass)
        report.pass_rate = passed_cases / len(results)

        layer_rates = self._layer_pass_rates(results)
        avg_f1 = self._avg_f1(results)

        for key, name, source in GATE_ITEMS:
            if key not in self.thresholds:
                continue

            threshold = float(self.thresholds[key])

            if source.startswith("layer:"):
                prefix = source.split(":", 1)[1]
                actual = layer_rates.get(prefix)
                detail = f"层前缀 {prefix}"
            else:
                actual = avg_f1
                detail = "所有含字段断言用例的 F1 均值"

            if actual is None:
                # 无对应数据：不判失败，但明确记录
                report.checks.append(GateCheck(
                    name=name, threshold_key=key, actual=None,
                    threshold=threshold, passed=True,
                    detail=f"{detail}｜无数据，跳过",
                ))
                continue

            report.checks.append(GateCheck(
                name=name, threshold_key=key, actual=round(actual, 4),
                threshold=threshold, passed=actual >= threshold,
                detail=detail,
            ))

        if not report.passed:
            names = "、".join(c.name for c in report.failed_checks)
            report.reason = f"未达阈值: {names}"

        return report

    def _layer_pass_rates(self, results: List[EvalResult]) -> dict:
        """按层前缀（L1~L5）统计通过率"""
        stats = {}
        for r in results:
            for layer in r.layers:
                prefix = layer.layer.split("_", 1)[0]
                bucket = stats.setdefault(prefix, {"pass": 0, "total": 0})
                bucket["total"] += 1
                if layer.passed:
                    bucket["pass"] += 1

        return {
            prefix: b["pass"] / b["total"]
            for prefix, b in stats.items() if b["total"] > 0
        }

    def _avg_f1(self, results: List[EvalResult]) -> Optional[float]:
        """字段抽取平均 F1（只统计有字段断言的用例）"""
        scores = [r.field_metrics.f1 for r in results if r.field_metrics]
        if not scores:
            return None
        return sum(scores) / len(scores)

    def print_report(self, report: GateReport):
        """打印门禁报告"""
        print(f"\n{'='*60}")
        print(f"  质量门禁")
        print(f"{'='*60}")

        if report.skipped:
            print(f"\n  ⏭  跳过: {report.reason}")
            return

        print(f"\n  {'门禁项':<22}{'实际':<12}{'阈值':<12}结果")
        print(f"  {'-'*56}")
        for c in report.checks:
            actual_text = "无数据" if c.actual is None else f"{c.actual:.4f}"
            icon = "✓" if c.passed else "✗"
            print(f"  {c.name:<20}{actual_text:<14}{c.threshold:<14}{icon}")

        print(f"\n  用例通过率: {report.pass_rate*100:.1f}% ({report.total_cases} 个用例)")

        if report.passed:
            print(f"\n  ✅ 门禁通过")
        else:
            print(f"\n  ❌ 门禁失败: {report.reason}")
            for c in report.failed_checks:
                actual_text = "无数据" if c.actual is None else f"{c.actual:.4f}"
                print(f"      {c.name}: {actual_text} < {c.threshold}")
