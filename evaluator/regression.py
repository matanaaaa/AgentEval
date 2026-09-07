"""
P1: Prompt Regression Testing（回归对比）

对比两次评测报告，自动发现退化：
- 哪些 case 从 PASS 变成了 FAIL
- 哪些层的通过率下降了
- F1 / 成功率是否退化
- 新增的失败 case

用法：
    python -m evaluator.regression reports/report_v1.0_xxx.json reports/report_v1.1_xxx.json
"""

import json
import sys
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CaseRegression:
    """单个 case 的回归对比"""
    case_id: str
    status: str  # "improved" | "regressed" | "unchanged" | "new" | "removed"
    baseline_pass: Optional[bool] = None
    current_pass: Optional[bool] = None
    detail: str = ""


@dataclass
class LayerRegression:
    """单层的回归对比"""
    layer: str
    baseline_rate: float = 0.0
    current_rate: float = 0.0
    delta: float = 0.0  # current - baseline，负数表示退化

    @property
    def regressed(self) -> bool:
        return self.delta < -0.01  # 超过 1% 视为退化


@dataclass
class RegressionReport:
    """回归对比报告"""
    baseline_tag: str = ""
    current_tag: str = ""
    baseline_pass_rate: float = 0.0
    current_pass_rate: float = 0.0
    pass_rate_delta: float = 0.0

    # 用例级别
    regressed_cases: List[CaseRegression] = field(default_factory=list)
    improved_cases: List[CaseRegression] = field(default_factory=list)
    new_failures: List[CaseRegression] = field(default_factory=list)

    # 层级别
    layer_regressions: List[LayerRegression] = field(default_factory=list)

    # F1 对比
    baseline_avg_f1: float = 0.0
    current_avg_f1: float = 0.0
    f1_delta: float = 0.0

    @property
    def has_regression(self) -> bool:
        return len(self.regressed_cases) > 0 or self.pass_rate_delta < -0.01


class RegressionComparer:
    """回归对比器"""

    def compare(self, baseline_path: str, current_path: str) -> RegressionReport:
        """
        对比两份 JSON 报告。

        Args:
            baseline_path: 基准报告 JSON 路径
            current_path: 当前报告 JSON 路径

        Returns:
            RegressionReport
        """
        baseline = self._load_report(baseline_path)
        current = self._load_report(current_path)

        report = RegressionReport(
            baseline_tag=baseline["meta"].get("tag", "baseline"),
            current_tag=current["meta"].get("tag", "current"),
        )

        # 通过率对比
        report.baseline_pass_rate = baseline["meta"].get("pass_rate", 0)
        report.current_pass_rate = current["meta"].get("pass_rate", 0)
        report.pass_rate_delta = report.current_pass_rate - report.baseline_pass_rate

        # 用例级别对比
        baseline_results = {r["case_id"]: r for r in baseline.get("results", [])}
        current_results = {r["case_id"]: r for r in current.get("results", [])}

        all_case_ids = set(list(baseline_results.keys()) + list(current_results.keys()))

        for case_id in sorted(all_case_ids):
            b = baseline_results.get(case_id)
            c = current_results.get(case_id)

            if b and c:
                b_pass = b.get("overall_pass", False)
                c_pass = c.get("overall_pass", False)

                if b_pass and not c_pass:
                    # 退化：之前通过现在失败
                    regression = CaseRegression(
                        case_id=case_id,
                        status="regressed",
                        baseline_pass=True,
                        current_pass=False,
                        detail=self._get_failed_layers(c),
                    )
                    report.regressed_cases.append(regression)
                elif not b_pass and c_pass:
                    # 改善：之前失败现在通过
                    regression = CaseRegression(
                        case_id=case_id,
                        status="improved",
                        baseline_pass=False,
                        current_pass=True,
                    )
                    report.improved_cases.append(regression)
            elif c and not b:
                # 新增 case
                if not c.get("overall_pass", False):
                    regression = CaseRegression(
                        case_id=case_id,
                        status="new",
                        current_pass=False,
                        detail=self._get_failed_layers(c),
                    )
                    report.new_failures.append(regression)

        # 层级别对比
        baseline_layers = baseline.get("layer_stats", {})
        current_layers = current.get("layer_stats", {})
        all_layers = set(list(baseline_layers.keys()) + list(current_layers.keys()))

        for layer in sorted(all_layers):
            b_rate = baseline_layers.get(layer, {}).get("rate", 0)
            c_rate = current_layers.get(layer, {}).get("rate", 0)
            report.layer_regressions.append(LayerRegression(
                layer=layer,
                baseline_rate=b_rate,
                current_rate=c_rate,
                delta=c_rate - b_rate,
            ))

        # F1 对比
        baseline_f1s = [r.get("field_metrics", {}).get("f1", 0) for r in baseline.get("results", []) if r.get("field_metrics")]
        current_f1s = [r.get("field_metrics", {}).get("f1", 0) for r in current.get("results", []) if r.get("field_metrics")]
        report.baseline_avg_f1 = sum(baseline_f1s) / len(baseline_f1s) if baseline_f1s else 0
        report.current_avg_f1 = sum(current_f1s) / len(current_f1s) if current_f1s else 0
        report.f1_delta = report.current_avg_f1 - report.baseline_avg_f1

        return report

    def print_report(self, report: RegressionReport):
        """打印回归对比报告"""
        print(f"\n{'='*60}")
        print(f"  回归对比报告")
        print(f"  {report.baseline_tag} → {report.current_tag}")
        print(f"{'='*60}")

        # 总体
        direction = "↑" if report.pass_rate_delta > 0 else ("↓" if report.pass_rate_delta < 0 else "→")
        print(f"\n  通过率: {report.baseline_pass_rate*100:.1f}% {direction} {report.current_pass_rate*100:.1f}% ({report.pass_rate_delta*100:+.1f}%)")
        print(f"  平均 F1: {report.baseline_avg_f1:.3f} → {report.current_avg_f1:.3f} ({report.f1_delta:+.3f})")

        # 退化的 case
        if report.regressed_cases:
            print(f"\n  ⚠️  退化用例 ({len(report.regressed_cases)}):")
            for c in report.regressed_cases:
                print(f"    ✗ {c.case_id}: PASS → FAIL | {c.detail}")

        # 改善的 case
        if report.improved_cases:
            print(f"\n  ✓ 改善用例 ({len(report.improved_cases)}):")
            for c in report.improved_cases:
                print(f"    ✓ {c.case_id}: FAIL → PASS")

        # 层回归
        regressed_layers = [lr for lr in report.layer_regressions if lr.regressed]
        if regressed_layers:
            print(f"\n  层级退化:")
            for lr in regressed_layers:
                print(f"    ↓ {lr.layer}: {lr.baseline_rate*100:.1f}% → {lr.current_rate*100:.1f}% ({lr.delta*100:+.1f}%)")

        # 结论
        if report.has_regression:
            print(f"\n  ❌ 结论: 存在回归退化，请检查")
        else:
            print(f"\n  ✅ 结论: 无回归退化")

    def _load_report(self, path: str) -> dict:
        """加载 JSON 报告"""
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _get_failed_layers(self, result: dict) -> str:
        """获取失败层的摘要"""
        layers = result.get("layers", {})
        failed = [k for k, v in layers.items() if not v]
        return ", ".join(failed) if failed else "unknown"


# 命令行入口
if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python -m evaluator.regression <baseline.json> <current.json>")
        print("示例: python -m evaluator.regression reports/report_v1.0_xxx.json reports/report_v1.1_xxx.json")
        sys.exit(1)

    comparer = RegressionComparer()
    report = comparer.compare(sys.argv[1], sys.argv[2])
    comparer.print_report(report)
