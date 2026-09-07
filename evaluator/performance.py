"""
P2: Agent Performance Evaluation（性能评测）

基于已采集的 durationMs 和 cost_credits，输出：
- 总体延迟统计（平均 / P50 / P95 / P99 / 最大）
- Tool 级别耗时分析
- 成本统计（每次任务成本、总成本）
- 慢调用识别
"""

from dataclasses import dataclass, field
from typing import List, Optional
import statistics

from runner.sse_parser import AgentTrace
from evaluator.evaluator import EvalResult


@dataclass
class ToolLatency:
    """单个 Tool 的延迟统计"""
    tool_name: str
    call_count: int = 0
    total_ms: int = 0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: int = 0
    slow_count: int = 0  # 慢调用次数


@dataclass
class PerformanceReport:
    """性能评测报告"""
    # 总体延迟
    total_cases: int = 0
    avg_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    max_latency_ms: int = 0
    min_latency_ms: int = 0

    # 成本
    total_cost_credits: float = 0.0
    avg_cost_per_case: float = 0.0
    max_cost: float = 0.0

    # 可靠性指标
    request_success_rate: float = 1.0   # 成功拿到响应的用例占比
    request_success_count: int = 0
    task_success_rate: float = 0.0      # overall_pass 占比
    task_success_count: int = 0
    timeout_rate: float = 0.0           # 出现超时/不完整的用例占比
    timeout_count: int = 0
    tool_success_rate: Optional[float] = None  # 非 error 的 Tool 占比（无 Tool 时为 None）
    tool_call_total: int = 0
    tool_success_total: int = 0

    # Tool 级别
    tool_latencies: List[ToolLatency] = field(default_factory=list)

    # 慢调用
    slow_cases: list = field(default_factory=list)  # case_id + duration
    slow_threshold_ms: int = 30000  # 默认 30s 为慢调用


class PerformanceAnalyzer:
    """性能分析器"""

    def __init__(self, slow_threshold_ms: int = 30000):
        self.slow_threshold_ms = slow_threshold_ms

    def analyze(self, results: List[EvalResult], traces: List[AgentTrace] = None) -> PerformanceReport:
        """
        分析性能指标。

        Args:
            results: 评测结果列表（含 duration_ms 和 cost_credits）
            traces: 原始 trace 列表（含 tool 级别耗时，可选）

        Returns:
            PerformanceReport
        """
        report = PerformanceReport(slow_threshold_ms=self.slow_threshold_ms)
        report.total_cases = len(results)

        if not results:
            return report

        # 延迟统计
        durations = [r.duration_ms for r in results if r.duration_ms > 0]
        if durations:
            report.avg_latency_ms = statistics.mean(durations)
            report.p50_latency_ms = statistics.median(durations)
            report.p95_latency_ms = self._percentile(durations, 95)
            report.p99_latency_ms = self._percentile(durations, 99)
            report.max_latency_ms = max(durations)
            report.min_latency_ms = min(durations)

        # 成本统计
        costs = [r.cost_credits for r in results if r.cost_credits > 0]
        if costs:
            report.total_cost_credits = sum(costs)
            report.avg_cost_per_case = statistics.mean(costs)
            report.max_cost = max(costs)

        # 慢调用识别
        for r in results:
            if r.duration_ms > self.slow_threshold_ms:
                report.slow_cases.append({
                    "case_id": r.case_id,
                    "duration_ms": r.duration_ms,
                    "cost": r.cost_credits,
                })

        # 可靠性指标：Request / Task 成功率、Timeout 率、Tool 成功率
        n = len(results)
        report.request_success_count = sum(1 for r in results if getattr(r, "request_ok", True))
        report.request_success_rate = report.request_success_count / n

        report.task_success_count = sum(1 for r in results if r.overall_pass)
        report.task_success_rate = report.task_success_count / n

        report.timeout_count = sum(1 for r in results if getattr(r, "timed_out", False))
        report.timeout_rate = report.timeout_count / n

        report.tool_call_total = sum(getattr(r, "tool_total", 0) for r in results)
        report.tool_success_total = sum(getattr(r, "tool_success", 0) for r in results)
        report.tool_success_rate = (
            report.tool_success_total / report.tool_call_total
            if report.tool_call_total > 0 else None
        )

        # Tool 级别分析（如果有 traces）
        if traces:
            report.tool_latencies = self._analyze_tools(traces)

        return report

    def analyze_from_traces(self, traces: List[AgentTrace]) -> PerformanceReport:
        """从 traces 直接分析（不需要 EvalResult）"""
        report = PerformanceReport(slow_threshold_ms=self.slow_threshold_ms)
        report.total_cases = len(traces)

        durations = [t.total_duration_ms for t in traces if t.total_duration_ms > 0]
        if durations:
            report.avg_latency_ms = statistics.mean(durations)
            report.p50_latency_ms = statistics.median(durations)
            report.p95_latency_ms = self._percentile(durations, 95)
            report.p99_latency_ms = self._percentile(durations, 99)
            report.max_latency_ms = max(durations)
            report.min_latency_ms = min(durations)

        costs = [t.cost_credits for t in traces if t.cost_credits > 0]
        if costs:
            report.total_cost_credits = sum(costs)
            report.avg_cost_per_case = statistics.mean(costs)
            report.max_cost = max(costs)

        report.tool_latencies = self._analyze_tools(traces)
        return report

    def _analyze_tools(self, traces: List[AgentTrace]) -> List[ToolLatency]:
        """分析 Tool 级别耗时"""
        tool_data = {}  # tool_name → [duration_ms, ...]

        for trace in traces:
            for tool in trace.tools:
                if tool.tool_name not in tool_data:
                    tool_data[tool.tool_name] = []
                if tool.duration_ms > 0:
                    tool_data[tool.tool_name].append(tool.duration_ms)

        result = []
        for name, durations in sorted(tool_data.items(), key=lambda x: -sum(x[1])):
            if not durations:
                continue
            tl = ToolLatency(
                tool_name=name,
                call_count=len(durations),
                total_ms=sum(durations),
                avg_ms=statistics.mean(durations),
                p50_ms=statistics.median(durations),
                p95_ms=self._percentile(durations, 95),
                p99_ms=self._percentile(durations, 99),
                max_ms=max(durations),
                slow_count=sum(1 for d in durations if d > self.slow_threshold_ms),
            )
            result.append(tl)

        return result

    def print_report(self, report: PerformanceReport):
        """打印性能报告"""
        print(f"\n{'='*60}")
        print(f"  性能评测报告")
        print(f"{'='*60}")

        print(f"\n  延迟统计 ({report.total_cases} 个用例):")
        print(f"    平均: {report.avg_latency_ms/1000:.1f}s")
        print(f"    P50:  {report.p50_latency_ms/1000:.1f}s")
        print(f"    P95:  {report.p95_latency_ms/1000:.1f}s")
        print(f"    P99:  {report.p99_latency_ms/1000:.1f}s")
        print(f"    最大: {report.max_latency_ms/1000:.1f}s")

        print(f"\n  成本统计:")
        print(f"    总费用: {report.total_cost_credits:.2f} credits")
        print(f"    平均/case: {report.avg_cost_per_case:.2f} credits")
        print(f"    最高单次: {report.max_cost:.2f} credits")

        print(f"\n  可靠性指标 ({report.total_cases} 个用例):")
        print(f"    Request 成功率: {report.request_success_rate*100:.1f}% "
              f"({report.request_success_count}/{report.total_cases})")
        print(f"    Task 成功率:    {report.task_success_rate*100:.1f}% "
              f"({report.task_success_count}/{report.total_cases})")
        print(f"    Timeout 率:     {report.timeout_rate*100:.1f}% "
              f"({report.timeout_count}/{report.total_cases})")
        if report.tool_success_rate is not None:
            print(f"    Tool 成功率:    {report.tool_success_rate*100:.1f}% "
                  f"({report.tool_success_total}/{report.tool_call_total})")
        else:
            print(f"    Tool 成功率:    无 Tool 调用")

        if report.tool_latencies:
            print(f"\n  Tool 耗时分析:")
            print(f"    {'Tool':<20} {'调用次数':<8} {'平均':<10} {'P95':<10} {'P99':<10} {'最大':<10}")
            print(f"    {'-'*68}")
            for tl in report.tool_latencies:
                print(f"    {tl.tool_name:<20} {tl.call_count:<8} {tl.avg_ms/1000:.1f}s{'':<5} {tl.p95_ms/1000:.1f}s{'':<5} {tl.p99_ms/1000:.1f}s{'':<5} {tl.max_ms/1000:.1f}s")

        if report.slow_cases:
            print(f"\n  ⚠️  慢调用 (>{report.slow_threshold_ms/1000:.0f}s):")
            for sc in report.slow_cases:
                print(f"    - {sc['case_id']}: {sc['duration_ms']/1000:.1f}s ({sc['cost']:.2f} credits)")

    @staticmethod
    def _percentile(data: list, pct: int) -> float:
        """计算百分位数"""
        if not data:
            return 0.0
        sorted_data = sorted(data)
        idx = (pct / 100) * (len(sorted_data) - 1)
        lower = int(idx)
        upper = lower + 1
        if upper >= len(sorted_data):
            return sorted_data[-1]
        weight = idx - lower
        return sorted_data[lower] * (1 - weight) + sorted_data[upper] * weight
