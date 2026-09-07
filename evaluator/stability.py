"""
Repeated-run Stability（重复运行稳定性）

同一批用例连续跑 N 遍，衡量 Agent 输出的可复现性：
- pass 翻转率（flip rate）：某用例在 N 次里既出现过 PASS 又出现过 FAIL 的占比，
  以及全体运行的 PASS 一致性。翻转多说明结果不稳定，最影响回归判断的可信度。
- F1 波动：每个用例 N 次 F1 的标准差，反映字段抽取的稳定程度。
- 延迟波动：每个用例 N 次耗时的标准差，反映性能抖动。

用法：由 main.py 在 --repeat N 时驱动，逐遍 add_run(results)，最后 analyze()。
"""

import statistics
from dataclasses import dataclass, field
from typing import List, Optional

from evaluator.evaluator import EvalResult


@dataclass
class CaseStability:
    """单个用例在 N 次运行中的稳定性"""
    case_id: str
    runs: int = 0
    pass_count: int = 0
    fail_count: int = 0
    flipped: bool = False           # 是否出现过 PASS/FAIL 翻转
    pass_rate: float = 0.0          # 该用例 N 次的通过占比
    f1_values: List[float] = field(default_factory=list)
    f1_mean: Optional[float] = None
    f1_std: Optional[float] = None
    latency_values: List[int] = field(default_factory=list)
    latency_mean_ms: float = 0.0
    latency_std_ms: float = 0.0


@dataclass
class StabilityReport:
    """稳定性总报告"""
    runs: int = 0
    total_cases: int = 0
    flipped_cases: int = 0
    flip_rate: float = 0.0          # 翻转用例数 / 用例数
    stable_pass_cases: int = 0      # N 次全 PASS 的用例数
    stable_fail_cases: int = 0      # N 次全 FAIL 的用例数
    avg_f1_std: Optional[float] = None      # 各用例 F1 标准差的平均
    avg_latency_std_ms: float = 0.0         # 各用例延迟标准差的平均
    cases: List[CaseStability] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "runs": self.runs,
            "total_cases": self.total_cases,
            "flipped_cases": self.flipped_cases,
            "flip_rate": round(self.flip_rate, 4),
            "stable_pass_cases": self.stable_pass_cases,
            "stable_fail_cases": self.stable_fail_cases,
            "avg_f1_std": round(self.avg_f1_std, 4) if self.avg_f1_std is not None else None,
            "avg_latency_std_ms": round(self.avg_latency_std_ms, 1),
            "cases": [
                {
                    "case_id": c.case_id,
                    "runs": c.runs,
                    "pass_count": c.pass_count,
                    "fail_count": c.fail_count,
                    "flipped": c.flipped,
                    "pass_rate": round(c.pass_rate, 4),
                    "f1_mean": round(c.f1_mean, 4) if c.f1_mean is not None else None,
                    "f1_std": round(c.f1_std, 4) if c.f1_std is not None else None,
                    "latency_mean_ms": round(c.latency_mean_ms, 1),
                    "latency_std_ms": round(c.latency_std_ms, 1),
                }
                for c in self.cases
            ],
        }


class StabilityAnalyzer:
    """按用例聚合多次运行的结果，输出稳定性报告"""

    def __init__(self):
        # case_id -> list of EvalResult（每次运行一个）
        self._runs: List[List[EvalResult]] = []

    def add_run(self, results: List[EvalResult]):
        """追加一遍完整运行的结果列表"""
        self._runs.append(results)

    def analyze(self) -> StabilityReport:
        report = StabilityReport(runs=len(self._runs))
        if not self._runs:
            return report

        # 按 case_id 归拢每一遍的结果
        by_case = {}  # case_id -> [EvalResult, ...]
        for run in self._runs:
            for r in run:
                by_case.setdefault(r.case_id, []).append(r)

        report.total_cases = len(by_case)
        f1_stds = []
        latency_stds = []

        for case_id, rs in by_case.items():
            cs = CaseStability(case_id=case_id, runs=len(rs))
            cs.pass_count = sum(1 for r in rs if r.overall_pass)
            cs.fail_count = cs.runs - cs.pass_count
            cs.flipped = cs.pass_count > 0 and cs.fail_count > 0
            cs.pass_rate = cs.pass_count / cs.runs if cs.runs else 0.0

            # F1 波动（只统计有 field_metrics 的运行）
            cs.f1_values = [r.field_metrics.f1 for r in rs if r.field_metrics]
            if cs.f1_values:
                cs.f1_mean = statistics.mean(cs.f1_values)
                cs.f1_std = statistics.pstdev(cs.f1_values) if len(cs.f1_values) > 1 else 0.0
                f1_stds.append(cs.f1_std)

            # 延迟波动
            cs.latency_values = [r.duration_ms for r in rs if r.duration_ms > 0]
            if cs.latency_values:
                cs.latency_mean_ms = statistics.mean(cs.latency_values)
                cs.latency_std_ms = (
                    statistics.pstdev(cs.latency_values) if len(cs.latency_values) > 1 else 0.0
                )
                latency_stds.append(cs.latency_std_ms)

            if cs.flipped:
                report.flipped_cases += 1
            elif cs.pass_count == cs.runs:
                report.stable_pass_cases += 1
            else:
                report.stable_fail_cases += 1

            report.cases.append(cs)

        report.flip_rate = report.flipped_cases / report.total_cases if report.total_cases else 0.0
        report.avg_f1_std = statistics.mean(f1_stds) if f1_stds else None
        report.avg_latency_std_ms = statistics.mean(latency_stds) if latency_stds else 0.0

        return report

    def print_report(self, report: StabilityReport):
        print(f"\n{'='*60}")
        print(f"  重复运行稳定性报告")
        print(f"{'='*60}")

        if report.runs < 2:
            print(f"\n  运行次数不足（{report.runs} 次），无法评估稳定性")
            return

        print(f"\n  运行遍数: {report.runs} | 用例数: {report.total_cases}")
        print(f"  翻转率(flip rate): {report.flip_rate*100:.1f}% "
              f"（{report.flipped_cases}/{report.total_cases} 个用例出现 PASS/FAIL 翻转）")
        print(f"  稳定通过: {report.stable_pass_cases} | 稳定失败: {report.stable_fail_cases}")
        if report.avg_f1_std is not None:
            print(f"  平均 F1 标准差: {report.avg_f1_std:.4f}")
        print(f"  平均延迟标准差: {report.avg_latency_std_ms/1000:.2f}s")

        # 只列出不稳定（翻转）的用例，便于排查
        flipped = [c for c in report.cases if c.flipped]
        if flipped:
            print(f"\n  ⚠️  翻转用例明细:")
            for c in sorted(flipped, key=lambda x: -x.fail_count):
                f1_desc = f", F1σ={c.f1_std:.3f}" if c.f1_std is not None else ""
                print(f"    - {c.case_id}: PASS {c.pass_count}/{c.runs}"
                      f"（通过率 {c.pass_rate*100:.0f}%）"
                      f", 延迟σ={c.latency_std_ms/1000:.2f}s{f1_desc}")
        else:
            print(f"\n  ✅ 全部用例结果一致，无翻转")
