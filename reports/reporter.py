"""
评测报告生成器
输出格式：控制台表格 + JSON 文件 + HTML 报告
"""

import json
import os
from datetime import datetime
from typing import List

import config
from evaluator.evaluator import EvalResult


class Reporter:
    """生成评测报告"""

    def __init__(self, output_dir: str = None):
        self.output_dir = output_dir or config.REPORTS_DIR
        os.makedirs(self.output_dir, exist_ok=True)

    def print_result(self, result: EvalResult):
        """打印单个用例的评测结果到控制台"""
        status = "✓ PASS" if result.overall_pass else "✗ FAIL"
        print(f"\n{'='*60}")
        print(f"  {result.case_id}  [{status}]")
        print(f"{'='*60}")

        for layer in result.layers:
            icon = "✓" if layer.passed else "✗"
            print(f"  {icon} {layer.layer}")
            if not layer.passed:
                print(f"      期望: {layer.expected}")
                print(f"      实际: {layer.actual}")
                if layer.detail:
                    print(f"      详情: {layer.detail}")

        if result.field_metrics:
            m = result.field_metrics
            print(f"\n  字段指标: P={m.precision}  R={m.recall}  F1={m.f1}")
            if m.missing_fields:
                print(f"      缺失: {m.missing_fields}")
            if m.wrong_fields:
                print(f"      错误: {m.wrong_fields}")

        print(f"\n  耗时: {result.duration_ms}ms | 费用: {result.cost_credits} credits")

    def print_summary(self, results: List[EvalResult]):
        """打印批量评测汇总"""
        total = len(results)
        passed = sum(1 for r in results if r.overall_pass)
        failed = total - passed

        print(f"\n{'='*60}")
        print(f"  评测汇总")
        print(f"{'='*60}")
        print(f"  总用例数: {total}")
        print(f"  通过: {passed}  失败: {failed}")
        print(f"  通过率: {passed/total*100:.1f}%" if total > 0 else "  通过率: N/A")

        # 分层统计
        layer_stats = {}
        for r in results:
            for layer in r.layers:
                if layer.layer not in layer_stats:
                    layer_stats[layer.layer] = {"pass": 0, "fail": 0}
                if layer.passed:
                    layer_stats[layer.layer]["pass"] += 1
                else:
                    layer_stats[layer.layer]["fail"] += 1

        print(f"\n  分层通过率:")
        for layer_name, stats in layer_stats.items():
            layer_total = stats["pass"] + stats["fail"]
            rate = stats["pass"] / layer_total * 100 if layer_total > 0 else 0
            print(f"    {layer_name}: {rate:.1f}% ({stats['pass']}/{layer_total})")

        # 字段 F1 平均值
        f1_scores = [r.field_metrics.f1 for r in results if r.field_metrics]
        if f1_scores:
            avg_f1 = sum(f1_scores) / len(f1_scores)
            print(f"\n  字段抽取平均 F1: {avg_f1:.4f}")

        # 性能统计
        durations = [r.duration_ms for r in results if r.duration_ms > 0]
        if durations:
            avg_dur = sum(durations) / len(durations)
            max_dur = max(durations)
            print(f"\n  平均耗时: {avg_dur:.0f}ms | 最大耗时: {max_dur}ms")

        # 失败用例列表
        if failed > 0:
            print(f"\n  失败用例:")
            for r in results:
                if not r.overall_pass:
                    failed_layers = [lr.layer for lr in r.layers if not lr.passed]
                    print(f"    - {r.case_id}: {failed_layers}")

    def save_report(self, results: List[EvalResult], tag: str = "",
                   failure_analysis: dict = None, perf_report=None, cases: list = None,
                   gate_report=None, stability_report=None) -> str:
        """保存 JSON + HTML 报告"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag_suffix = f"_{tag}" if tag else ""

        # JSON 报告
        json_file = os.path.join(self.output_dir, f"report{tag_suffix}_{timestamp}.json")
        report_data = self._build_report_data(results, tag)

        # 追加失败归因和性能数据到 JSON
        if failure_analysis:
            report_data["failure_analysis"] = {
                "primary_cause_stats": failure_analysis.get("primary_cause_stats", {}),
                "category_stats": failure_analysis.get("category_stats", {}),
            }
        if perf_report:
            report_data["performance"] = {
                "avg_latency_ms": perf_report.avg_latency_ms,
                "p50_latency_ms": perf_report.p50_latency_ms,
                "p95_latency_ms": perf_report.p95_latency_ms,
                "p99_latency_ms": perf_report.p99_latency_ms,
                "max_latency_ms": perf_report.max_latency_ms,
                "total_cost_credits": perf_report.total_cost_credits,
                "avg_cost_per_case": perf_report.avg_cost_per_case,
                # 可靠性指标
                "request_success_rate": perf_report.request_success_rate,
                "task_success_rate": perf_report.task_success_rate,
                "timeout_rate": perf_report.timeout_rate,
                "tool_success_rate": perf_report.tool_success_rate,
                "tool_call_total": perf_report.tool_call_total,
                "tool_latencies": [
                    {
                        "tool_name": tl.tool_name,
                        "call_count": tl.call_count,
                        "avg_ms": round(tl.avg_ms, 1),
                        "p50_ms": round(tl.p50_ms, 1),
                        "p95_ms": round(tl.p95_ms, 1),
                        "p99_ms": round(tl.p99_ms, 1),
                        "max_ms": tl.max_ms,
                    }
                    for tl in perf_report.tool_latencies
                ],
            }
        if gate_report is not None:
            report_data["quality_gate"] = gate_report.to_dict()
        if stability_report is not None:
            report_data["stability"] = stability_report.to_dict()

        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(report_data, f, ensure_ascii=False, indent=2)

        # HTML 报告
        html_file = os.path.join(self.output_dir, f"report{tag_suffix}_{timestamp}.html")
        html_content = self._generate_html(report_data, results, cases=cases)
        with open(html_file, "w", encoding="utf-8") as f:
            f.write(html_content)

        print(f"\n  JSON 报告: {json_file}")
        print(f"  HTML 报告: {html_file}")
        return html_file

    def _build_report_data(self, results: List[EvalResult], tag: str) -> dict:
        """构建报告数据"""
        total = len(results)
        passed = sum(1 for r in results if r.overall_pass)

        return {
            "meta": {
                "timestamp": datetime.now().isoformat(),
                "tag": tag,
                "total_cases": total,
                "passed": passed,
                "failed": total - passed,
                "pass_rate": round(passed / total, 4) if total > 0 else 0,
            },
            "results": [r.summary() for r in results],
            "layer_stats": self._calc_layer_stats(results),
        }

    def _calc_layer_stats(self, results: List[EvalResult]) -> dict:
        """计算分层统计"""
        stats = {}
        for r in results:
            for layer in r.layers:
                if layer.layer not in stats:
                    stats[layer.layer] = {"pass": 0, "fail": 0, "rate": 0}
                if layer.passed:
                    stats[layer.layer]["pass"] += 1
                else:
                    stats[layer.layer]["fail"] += 1

        for layer_name, s in stats.items():
            total = s["pass"] + s["fail"]
            s["rate"] = round(s["pass"] / total, 4) if total > 0 else 0

        return stats

    def _generate_html(self, report_data: dict, results: List[EvalResult], cases: list = None) -> str:
        """生成完整的 HTML Dashboard 报告"""
        meta = report_data["meta"]
        layer_stats = report_data["layer_stats"]
        perf = report_data.get("performance", {})
        fa = report_data.get("failure_analysis", {})

        # 可靠性指标（来自 performance 段）
        req_success_rate = perf.get("request_success_rate", 1.0) * 100
        timeout_rate = perf.get("timeout_rate", 0.0) * 100
        _tsr = perf.get("tool_success_rate")
        tool_success_rate = _tsr * 100 if _tsr is not None else None

        # 稳定性卡片（仅在 --repeat 产生 stability 段时展示）
        stability_cards = self._render_stability_html(report_data.get("stability"))

        # 计算 Quality Score 指标
        f1_scores = [r.field_metrics.f1 for r in results if r.field_metrics]
        avg_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0
        durations = [r.duration_ms for r in results if r.duration_ms > 0]
        avg_duration = sum(durations) / len(durations) if durations else 0
        total_cost = sum(r.cost_credits for r in results)
        avg_cost = total_cost / len(results) if results else 0

        # 各层通过率
        layer_rates = {}
        for name, stats in layer_stats.items():
            layer_rates[name] = stats["rate"] * 100

        skill_acc = layer_rates.get("L1_Skill路由", 0)
        tool_acc = layer_rates.get("L2_Tool调用", 0)
        field_rate = layer_rates.get("L3_字段抽取", 0)
        biz_rate = layer_rates.get("L4_业务结果", 0)
        # 轮次状态是附加层：只在用例定义了 state_assertion 时才存在
        state_rate = layer_rates.get("L5_轮次状态")

        # Chart.js 雷达数据。原来的「意图理解」轴与「Skill 路由」共用同一个
        # skill_key 判定，两个顶点数值恒等，撑出一个不存在的维度，已移除。
        radar_axes = [
            ("Skill路由", skill_acc),
            ("Tool调用", tool_acc),
            ("字段抽取", field_rate),
            ("业务结果", biz_rate),
        ]
        if state_rate is not None:
            radar_axes.append(("轮次状态", state_rate))

        radar_labels = json.dumps([name for name, _ in radar_axes], ensure_ascii=False)
        radar_data = json.dumps([value for _, value in radar_axes])

        # 失败归因饼图数据
        primary_stats = fa.get("primary_cause_stats", {})
        from evaluator.failure_analyzer import FailureAnalyzer
        pie_labels = []
        pie_data = []
        pie_colors = []
        color_palette = ["#ef4444", "#f59e0b", "#6366f1", "#10b981", "#ec4899", "#8b5cf6", "#06b6d4", "#84cc16"]
        for i, (cause, count) in enumerate(sorted(primary_stats.items(), key=lambda x: -x[1])):
            label = FailureAnalyzer.CATEGORIES.get(cause, {}).get("label", cause)
            pie_labels.append(label)
            pie_data.append(count)
            pie_colors.append(color_palette[i % len(color_palette)])

        pie_labels_json = json.dumps(pie_labels, ensure_ascii=False)
        pie_data_json = json.dumps(pie_data)
        pie_colors_json = json.dumps(pie_colors)

        # 用例详情行（带 Trace 展开）
        case_rows = self._build_case_rows_html(results)

        # 分层进度条
        layer_bars = ""
        for layer_name, stats in layer_stats.items():
            rate = stats["rate"] * 100
            bar_class = "bar-pass" if rate >= 90 else ("bar-warn" if rate >= 70 else "bar-fail")
            short_name = layer_name.replace("_", " ")
            layer_bars += f"""
            <div class="layer-bar-row">
                <div class="layer-bar-label">{short_name}</div>
                <div class="layer-bar-track">
                    <div class="layer-bar-fill {bar_class}" style="width:{rate}%"></div>
                </div>
                <div class="layer-bar-value">{rate:.0f}%</div>
            </div>"""

        # 单独构建可选脚本，避免在外层 f-string 中嵌套 f-string。
        # 嵌套写法依赖 Python 3.12 的 PEP 701，与项目声明的 Python 3.10+ 不兼容。
        pie_chart_script = ""
        if pie_data:
            pie_chart_script = f"""
        if (document.getElementById('pieChart')) {{
            new Chart(document.getElementById('pieChart'), {{
                type: 'doughnut',
                data: {{
                    labels: {pie_labels_json},
                    datasets: [{{
                        data: {pie_data_json},
                        backgroundColor: {pie_colors_json},
                        borderColor: '#1e293b',
                        borderWidth: 3,
                    }}]
                }},
                options: {{
                    plugins: {{
                        legend: {{ position: 'bottom', labels: {{ color: '#94a3b8', padding: 16, font: {{ size: 12 }} }} }}
                    }}
                }}
            }});
        }}
        """

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Evaluation Dashboard{' - ' + meta['tag'] if meta.get('tag') else ''}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            line-height: 1.6;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 24px; }}

        /* Header */
        .header {{
            background: linear-gradient(135deg, #1e293b 0%, #334155 100%);
            border: 1px solid #334155;
            border-radius: 16px;
            padding: 36px;
            margin-bottom: 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .header-left h1 {{ font-size: 28px; font-weight: 700; color: #f8fafc; }}
        .header-left .subtitle {{ color: #94a3b8; font-size: 14px; margin-top: 4px; }}
        .header-right {{ text-align: right; }}
        .header-right .tag {{ background: #6366f1; color: white; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 500; }}
        .header-right .time {{ color: #64748b; font-size: 12px; margin-top: 8px; }}

        /* Quality Score */
        .quality-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 14px;
            margin-bottom: 24px;
        }}
        .q-card {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 18px;
            text-align: center;
        }}
        .q-card .q-label {{ font-size: 12px; color: #64748b; text-transform: uppercase; letter-spacing: 0.5px; }}
        .q-card .q-value {{ font-size: 32px; font-weight: 800; margin-top: 6px; }}
        .q-card .q-value.green {{ color: #10b981; }}
        .q-card .q-value.red {{ color: #ef4444; }}
        .q-card .q-value.blue {{ color: #6366f1; }}
        .q-card .q-value.amber {{ color: #f59e0b; }}

        /* Sections */
        .section {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
        }}
        .section h2 {{ font-size: 16px; font-weight: 600; color: #f1f5f9; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
        .section h2::before {{ content: ''; width: 4px; height: 18px; background: #6366f1; border-radius: 2px; }}

        /* Charts Grid */
        .charts-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 24px;
            margin-bottom: 24px;
        }}
        @media (max-width: 768px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
        .chart-container {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 24px;
        }}
        .chart-container h3 {{ font-size: 14px; color: #94a3b8; margin-bottom: 16px; }}
        .chart-wrapper {{ position: relative; height: 280px; }}

        /* Layer Bars */
        .layer-bar-row {{ display: flex; align-items: center; margin-bottom: 10px; }}
        .layer-bar-label {{ width: 130px; font-size: 13px; color: #94a3b8; }}
        .layer-bar-track {{ flex: 1; height: 28px; background: #334155; border-radius: 14px; overflow: hidden; }}
        .layer-bar-fill {{ height: 100%; border-radius: 14px; transition: width 0.8s ease; }}
        .bar-pass {{ background: linear-gradient(90deg, #10b981, #34d399); }}
        .bar-warn {{ background: linear-gradient(90deg, #f59e0b, #fbbf24); }}
        .bar-fail {{ background: linear-gradient(90deg, #ef4444, #f87171); }}
        .layer-bar-value {{ width: 50px; text-align: right; font-size: 14px; font-weight: 600; color: #e2e8f0; }}

        /* Table */
        .results-table {{ width: 100%; border-collapse: collapse; }}
        .results-table th {{ text-align: left; padding: 12px 14px; font-size: 12px; color: #64748b; border-bottom: 1px solid #334155; text-transform: uppercase; letter-spacing: 0.5px; }}
        .results-table td {{ padding: 12px 14px; border-bottom: 1px solid #1e293b; font-size: 14px; color: #cbd5e1; }}
        .results-table tr:hover {{ background: #334155; }}

        .status-badge {{ display: inline-block; padding: 3px 10px; border-radius: 6px; font-size: 11px; font-weight: 700; }}
        .status-badge.pass {{ background: #064e3b; color: #6ee7b7; }}
        .status-badge.fail {{ background: #7f1d1d; color: #fca5a5; }}
        .badge {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600; margin-right: 2px; }}
        .badge-pass {{ background: #064e3b; color: #6ee7b7; }}
        .badge-fail {{ background: #7f1d1d; color: #fca5a5; }}

        /* Trace Expand */
        .trace-toggle {{ cursor: pointer; user-select: none; }}
        .trace-toggle:hover {{ color: #6366f1; }}
        .trace-detail {{ display: none; padding: 12px 14px; background: #0f172a; border-bottom: 1px solid #334155; }}
        .trace-detail.open {{ display: table-row; }}
        .trace-content {{ font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 12px; color: #94a3b8; white-space: pre-wrap; padding: 12px; background: #1e293b; border-radius: 8px; border: 1px solid #334155; }}
        .trace-step {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
        .trace-step .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
        .trace-step .dot.green {{ background: #10b981; }}
        .trace-step .dot.red {{ background: #ef4444; }}
        .trace-step .dot.blue {{ background: #6366f1; }}

        /* Failure Cards */
        .failure-card {{
            border: 1px solid #7f1d1d; border-radius: 10px;
            padding: 16px; margin-bottom: 12px; background: #1c1917;
        }}
        .failure-title {{ font-weight: 600; color: #fca5a5; margin-bottom: 8px; }}
        .failure-item {{ margin-bottom: 8px; padding-left: 12px; border-left: 3px solid #ef4444; }}
        .failure-layer {{ font-weight: 500; font-size: 13px; color: #f87171; }}
        .failure-detail {{ font-size: 13px; color: #94a3b8; margin-top: 4px; }}
        .failure-detail .label {{ color: #64748b; }}

        /* Footer */
        .footer {{ text-align: center; padding: 32px; color: #475569; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <div class="header-left">
                <h1>Agent Evaluation Dashboard</h1>
                <div class="subtitle">CRM 智能录入 Agent 自动化评测</div>
            </div>
            <div class="header-right">
                {f'<div class="tag">{meta["tag"]}</div>' if meta.get("tag") else ""}
                <div class="time">{meta["timestamp"][:19].replace("T", " ")}</div>
            </div>
        </div>

        <!-- Agent Quality Score -->
        <div class="quality-grid">
            <div class="q-card">
                <div class="q-label">Task Success</div>
                <div class="q-value {'green' if meta['pass_rate'] >= 0.9 else 'red'}">{meta['pass_rate']*100:.0f}%</div>
            </div>
            <div class="q-card">
                <div class="q-label">Skill Accuracy</div>
                <div class="q-value {'green' if skill_acc >= 90 else 'amber'}">{skill_acc:.0f}%</div>
            </div>
            <div class="q-card">
                <div class="q-label">Tool Accuracy</div>
                <div class="q-value {'green' if tool_acc >= 90 else 'amber'}">{tool_acc:.0f}%</div>
            </div>
            <div class="q-card">
                <div class="q-label">Field F1</div>
                <div class="q-value {'green' if avg_f1 >= 0.9 else 'amber'}">{avg_f1:.2f}</div>
            </div>
            <div class="q-card">
                <div class="q-label">Business Success</div>
                <div class="q-value {'green' if biz_rate >= 90 else 'red'}">{biz_rate:.0f}%</div>
            </div>
            <div class="q-card">
                <div class="q-label">Avg Latency</div>
                <div class="q-value blue">{avg_duration/1000:.1f}s</div>
            </div>
            <div class="q-card">
                <div class="q-label">Cost / Task</div>
                <div class="q-value blue">{avg_cost:.1f}</div>
            </div>
            <div class="q-card">
                <div class="q-label">Total Cases</div>
                <div class="q-value blue">{meta['total_cases']}</div>
            </div>
            <div class="q-card">
                <div class="q-label">Request Success</div>
                <div class="q-value {'green' if req_success_rate >= 99 else 'red'}">{req_success_rate:.0f}%</div>
            </div>
            <div class="q-card">
                <div class="q-label">Tool Success</div>
                <div class="q-value {'green' if (tool_success_rate is None or tool_success_rate >= 95) else 'amber'}">{('%.0f%%' % tool_success_rate) if tool_success_rate is not None else 'N/A'}</div>
            </div>
            <div class="q-card">
                <div class="q-label">Timeout Rate</div>
                <div class="q-value {'green' if timeout_rate == 0 else 'red'}">{timeout_rate:.0f}%</div>
            </div>
        </div>
{stability_cards}

        <!-- Charts -->
        <div class="charts-grid">
            <div class="chart-container">
                <h3>Agent Capability Radar</h3>
                <div class="chart-wrapper"><canvas id="radarChart"></canvas></div>
            </div>
            <div class="chart-container">
                <h3>Failure Distribution</h3>
                <div class="chart-wrapper">
                    {'<canvas id="pieChart"></canvas>' if pie_data else '<p style="color:#64748b;text-align:center;padding-top:100px">No failures</p>'}
                </div>
            </div>
        </div>

        <!-- Quality Gate -->
        {self._render_quality_gate_html(report_data.get("quality_gate"))}

        <!-- Evaluation Layers -->
        <div class="section">
            <h2>Evaluation Layers</h2>
            {layer_bars}
        </div>

        <!-- Case Details with Trace -->
        <div class="section">
            <h2>Case Details</h2>
            <table class="results-table" id="caseTable">
                <thead>
                    <tr>
                        <th></th>
                        <th>Case ID</th>
                        <th>Status</th>
                        <th>Layers</th>
                        <th>F1</th>
                        <th>Latency</th>
                        <th>Cost</th>
                    </tr>
                </thead>
                <tbody>
                    {case_rows}
                </tbody>
            </table>
        </div>

        <!-- Performance -->
        <div class="section">
            <h2>Performance</h2>
            <div class="quality-grid" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
                <div class="q-card"><div class="q-label">P50</div><div class="q-value blue">{perf.get('p50_latency_ms',0)/1000:.1f}s</div></div>
                <div class="q-card"><div class="q-label">P95</div><div class="q-value {'amber' if perf.get('p95_latency_ms',0) > 30000 else 'blue'}">{perf.get('p95_latency_ms',0)/1000:.1f}s</div></div>
                <div class="q-card"><div class="q-label">P99</div><div class="q-value {'red' if perf.get('p99_latency_ms',0) > 60000 else 'blue'}">{perf.get('p99_latency_ms',0)/1000:.1f}s</div></div>
                <div class="q-card"><div class="q-label">Max</div><div class="q-value amber">{perf.get('max_latency_ms',0)/1000:.1f}s</div></div>
                <div class="q-card"><div class="q-label">Total Cost</div><div class="q-value blue">{perf.get('total_cost_credits',0):.1f}</div></div>
                <div class="q-card"><div class="q-label">Avg Cost</div><div class="q-value blue">{perf.get('avg_cost_per_case',0):.1f}</div></div>
            </div>
            {self._render_tool_latency_html(perf.get('tool_latencies', []))}
        </div>

        {self._render_stability_section_html(report_data.get('stability'))}

        <!-- Scenario Coverage -->
        <div class="section">
            <h2>Agent Capability Coverage</h2>
            {self._render_coverage_html(cases)}
        </div>

        <div class="footer">
            AgentEval - Agent Evaluation Platform | Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        </div>
    </div>

    <script>
        // Radar Chart
        new Chart(document.getElementById('radarChart'), {{
            type: 'radar',
            data: {{
                labels: {radar_labels},
                datasets: [{{
                    label: 'Accuracy %',
                    data: {radar_data},
                    backgroundColor: 'rgba(99, 102, 241, 0.15)',
                    borderColor: '#6366f1',
                    borderWidth: 2,
                    pointBackgroundColor: '#6366f1',
                    pointRadius: 4,
                }}]
            }},
            options: {{
                scales: {{
                    r: {{
                        beginAtZero: true,
                        max: 100,
                        ticks: {{ stepSize: 20, color: '#64748b', backdropColor: 'transparent' }},
                        grid: {{ color: '#334155' }},
                        angleLines: {{ color: '#334155' }},
                        pointLabels: {{ color: '#94a3b8', font: {{ size: 12 }} }}
                    }}
                }},
                plugins: {{ legend: {{ display: false }} }}
            }}
        }});

        // Pie Chart
        {pie_chart_script}

        // Trace toggle
        document.querySelectorAll('.trace-toggle').forEach(btn => {{
            btn.addEventListener('click', () => {{
                const id = btn.dataset.target;
                const row = document.getElementById(id);
                if (row) row.classList.toggle('open');
                btn.textContent = row.classList.contains('open') ? '▼' : '▶';
            }});
        }});
    </script>
</body>
</html>"""

        return html

    def _build_case_rows_html(self, results: List[EvalResult]) -> str:
        """构建用例表格行（含 Trace 展开）"""
        rows = ""
        for i, r in enumerate(results):
            status_class = "pass" if r.overall_pass else "fail"
            status_text = "PASS" if r.overall_pass else "FAIL"

            layer_badges = ""
            for lr in r.layers:
                badge_class = "badge-pass" if lr.passed else "badge-fail"
                layer_short = lr.layer.split("_")[0]
                layer_badges += f'<span class="badge {badge_class}" title="{lr.layer}">{layer_short}</span>'

            f1_text = f"{r.field_metrics.f1:.2f}" if r.field_metrics else "-"
            duration_text = f"{r.duration_ms/1000:.1f}s" if r.duration_ms else "-"

            # Trace 展开内容
            trace_content = self._build_trace_detail(r)
            trace_id = f"trace-{i}"

            rows += f"""
            <tr>
                <td><span class="trace-toggle" data-target="{trace_id}">▶</span></td>
                <td>{r.case_id}</td>
                <td><span class="status-badge {status_class}">{status_text}</span></td>
                <td>{layer_badges}</td>
                <td>{f1_text}</td>
                <td>{duration_text}</td>
                <td>{r.cost_credits:.1f}</td>
            </tr>
            <tr id="{trace_id}" class="trace-detail">
                <td colspan="7">{trace_content}</td>
            </tr>"""

        return rows

    def _build_trace_detail(self, result: EvalResult) -> str:
        """构建单个 case 的 Trace 展开内容"""
        lines = []

        # 各层结果
        for lr in result.layers:
            color = "green" if lr.passed else "red"
            status = "PASS" if lr.passed else "FAIL"
            lines.append(f'<div class="trace-step"><span class="dot {color}"></span><strong>{lr.layer}</strong>: {status}</div>')
            if not lr.passed:
                lines.append(f'<div style="padding-left:24px;color:#94a3b8;font-size:12px">Expected: {lr.expected}<br>Actual: {lr.actual}<br>Detail: {lr.detail}</div>')

        # 字段指标
        if result.field_metrics:
            m = result.field_metrics
            lines.append(f'<div class="trace-step" style="margin-top:8px"><span class="dot blue"></span><strong>Field Metrics</strong>: P={m.precision} R={m.recall} F1={m.f1}</div>')
            if m.matched_fields:
                lines.append(f'<div style="padding-left:24px;color:#6ee7b7;font-size:12px">Matched: {", ".join(m.matched_fields)}</div>')
            if m.missing_fields:
                lines.append(f'<div style="padding-left:24px;color:#f59e0b;font-size:12px">Missing: {", ".join(m.missing_fields)}</div>')
            # Extra 会直接拉低 precision，缺了它就会出现「指标掉了但报告里看不出原因」
            if m.extra_fields:
                lines.append(f'<div style="padding-left:24px;color:#f59e0b;font-size:12px">Extra (多抽，拉低 precision): {", ".join(m.extra_fields)}</div>')
            if m.wrong_fields:
                for wf in m.wrong_fields:
                    expected = wf.get("expected", "")
                    # 字段存在性约束违反用 <absent>/<present> 标记，单独措辞更好读
                    if expected in ("<absent>", "<present>"):
                        rule = "必须不存在" if expected == "<absent>" else "必须存在"
                        lines.append(
                            f'<div style="padding-left:24px;color:#ef4444;font-size:12px">'
                            f'Constraint: {wf["field"]} {rule}，实际 {wf.get("actual", "")}</div>'
                        )
                    else:
                        lines.append(
                            f'<div style="padding-left:24px;color:#ef4444;font-size:12px">'
                            f'Wrong: {wf["field"]} (expected: {expected}, actual: {wf.get("actual", "")})</div>'
                        )

        # 链路关联入口（日志平台深链 + 原始 ID）
        if result.trace_refs:
            lines.append('<div class="trace-step" style="margin-top:8px"><span class="dot blue"></span><strong>Trace &amp; Logs</strong></div>')
            for ref in result.trace_refs:
                link = ""
                if ref.get("log_url"):
                    link = f'<a href="{ref["log_url"]}" target="_blank" rel="noopener" style="color:#6366f1">查看后端日志</a>'
                ids = []
                if ref.get("run_id"):
                    ids.append(f'run_id={ref["run_id"]}')
                if ref.get("message_id"):
                    ids.append(f'message_id={ref["message_id"]}')
                if ref.get("ai_trace_path"):
                    ids.append(f'trace={ref["ai_trace_path"]}')
                if ref.get("infra_trace_id"):
                    ids.append(f'infra_trace_id={ref["infra_trace_id"]}')
                lines.append(
                    f'<div style="padding-left:24px;font-size:12px;color:#94a3b8">'
                    f'<strong>{ref.get("turn_id","")}</strong> {link}<br>{"<br>".join(ids)}</div>'
                )

        content = "\n".join(lines)
        return f'<div class="trace-content">{content}</div>'

    def _render_stability_html(self, stability: dict = None) -> str:
        """稳定性顶部提示卡片（仅 --repeat 时有数据）。放在 Quality 卡片下方一行。"""
        if not stability or stability.get("runs", 0) < 2:
            return ""
        flip = stability.get("flip_rate", 0) * 100
        f1_std = stability.get("avg_f1_std")
        lat_std = stability.get("avg_latency_std_ms", 0) / 1000
        f1_std_text = f"{f1_std:.3f}" if f1_std is not None else "N/A"
        return f"""
        <div class="quality-grid">
            <div class="q-card">
                <div class="q-label">Repeat Runs</div>
                <div class="q-value blue">{stability.get('runs', 0)}</div>
            </div>
            <div class="q-card">
                <div class="q-label">Flip Rate</div>
                <div class="q-value {'green' if flip == 0 else 'red'}">{flip:.0f}%</div>
            </div>
            <div class="q-card">
                <div class="q-label">Stable Pass</div>
                <div class="q-value green">{stability.get('stable_pass_cases', 0)}</div>
            </div>
            <div class="q-card">
                <div class="q-label">Avg F1 &#963;</div>
                <div class="q-value {'green' if (f1_std is not None and f1_std < 0.05) else 'amber'}">{f1_std_text}</div>
            </div>
            <div class="q-card">
                <div class="q-label">Avg Latency &#963;</div>
                <div class="q-value blue">{lat_std:.1f}s</div>
            </div>
        </div>"""

    def _render_tool_latency_html(self, tool_latencies: list) -> str:
        """Tool 级别耗时表格（含 P99）"""
        if not tool_latencies:
            return ""
        rows = ""
        for tl in tool_latencies:
            rows += f"""
                    <tr>
                        <td>{tl.get('tool_name', '')}</td>
                        <td>{tl.get('call_count', 0)}</td>
                        <td>{tl.get('avg_ms', 0)/1000:.1f}s</td>
                        <td>{tl.get('p50_ms', 0)/1000:.1f}s</td>
                        <td>{tl.get('p95_ms', 0)/1000:.1f}s</td>
                        <td>{tl.get('p99_ms', 0)/1000:.1f}s</td>
                        <td>{tl.get('max_ms', 0)/1000:.1f}s</td>
                    </tr>"""
        return f"""
            <h3 style="font-size:13px;color:#94a3b8;margin:20px 0 12px">Tool Latency</h3>
            <table class="results-table">
                <thead>
                    <tr><th>Tool</th><th>Calls</th><th>Avg</th><th>P50</th><th>P95</th><th>P99</th><th>Max</th></tr>
                </thead>
                <tbody>{rows}
                </tbody>
            </table>"""

    def _render_stability_section_html(self, stability: dict = None) -> str:
        """稳定性详情区块：翻转用例明细表"""
        if not stability or stability.get("runs", 0) < 2:
            return ""

        flip = stability.get("flip_rate", 0) * 100
        cases = stability.get("cases", [])
        flipped = [c for c in cases if c.get("flipped")]

        if flipped:
            rows = ""
            for c in sorted(flipped, key=lambda x: -x.get("fail_count", 0)):
                f1_std = c.get("f1_std")
                f1_text = f"{f1_std:.3f}" if f1_std is not None else "-"
                rows += f"""
                    <tr>
                        <td>{c.get('case_id', '')}</td>
                        <td>{c.get('pass_count', 0)}/{c.get('runs', 0)}</td>
                        <td>{c.get('pass_rate', 0)*100:.0f}%</td>
                        <td>{c.get('latency_std_ms', 0)/1000:.2f}s</td>
                        <td>{f1_text}</td>
                    </tr>"""
            body = f"""
            <table class="results-table">
                <thead>
                    <tr><th>Case ID</th><th>Pass/Runs</th><th>Pass Rate</th><th>Latency &#963;</th><th>F1 &#963;</th></tr>
                </thead>
                <tbody>{rows}
                </tbody>
            </table>"""
        else:
            body = '<p style="color:#6ee7b7">所有用例结果一致，无翻转</p>'

        return f"""
        <div class="section">
            <h2>Repeated-run Stability（{stability.get('runs', 0)} 遍｜翻转率 {flip:.0f}%）</h2>
            {body}
        </div>"""

    def _render_quality_gate_html(self, gate: dict = None) -> str:
        """渲染质量门禁区块（与 JSON 报告里的 quality_gate 段一致）"""
        if not gate:
            return ""

        if gate.get("skipped"):
            return f"""
        <div class="section">
            <h2>Quality Gate</h2>
            <p style="color:#64748b">已跳过: {gate.get('reason', '')}</p>
        </div>"""

        passed = gate.get("passed", False)
        badge = (
            '<span class="status-badge pass">PASS</span>' if passed
            else '<span class="status-badge fail">FAIL</span>'
        )
        reason = gate.get("reason", "")

        rows = ""
        for c in gate.get("checks", []):
            actual = c.get("actual")
            actual_text = "无数据" if actual is None else f"{actual:.4f}"
            ok = c.get("passed", False)
            cls = "badge-pass" if ok else "badge-fail"
            rows += f"""
                    <tr>
                        <td>{c.get('name', '')}</td>
                        <td>{actual_text}</td>
                        <td>{c.get('threshold', '')}</td>
                        <td><span class="badge {cls}">{'PASS' if ok else 'FAIL'}</span></td>
                    </tr>"""

        reason_html = (
            f'<p style="color:#fca5a5;margin-top:12px">{reason}</p>' if reason and not passed else ""
        )

        return f"""
        <div class="section">
            <h2>Quality Gate {badge}</h2>
            <table class="results-table">
                <thead>
                    <tr><th>门禁项</th><th>实际</th><th>阈值</th><th>结果</th></tr>
                </thead>
                <tbody>{rows}
                </tbody>
            </table>
            {reason_html}
        </div>"""

    def _render_failure_analysis_html(self, report_data: dict) -> str:
        """渲染失败归因 HTML（保留兼容）"""
        return ""

    def _render_performance_html(self, report_data: dict) -> str:
        """渲染性能统计 HTML（保留兼容）"""
        return ""

    def _render_coverage_html(self, cases: list = None) -> str:
        """渲染场景覆盖度 HTML"""
        if not cases:
            return "<p style='color:#64748b'>无场景标签数据（在 case 中添加 tags 字段）</p>"

        total_cases = len(cases)

        # 从 metadata.tags 或顶层 tags 读取
        tag_counts = {}
        scenario_counts = {}
        difficulty_counts = {}
        type_counts = {}
        for case in cases:
            metadata = case.get("metadata", {})
            tags = metadata.get("tags", case.get("tags", []))
            scenario = metadata.get("scenario", "")
            difficulty = metadata.get("difficulty", "")
            case_type = case.get("type", "unknown")
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            if scenario:
                scenario_counts[scenario] = scenario_counts.get(scenario, 0) + 1
            if difficulty:
                difficulty_counts[difficulty] = difficulty_counts.get(difficulty, 0) + 1
            type_counts[case_type] = type_counts.get(case_type, 0) + 1

        if not tag_counts and not scenario_counts:
            return "<p style='color:#64748b'>用例中未定义 tags/scenario 字段</p>"

        # Tag 标签映射（英文→中文展示）
        tag_labels = {
            "field_extraction": "字段抽取",
            "multi_turn": "多轮对话",
            "context_memory": "上下文记忆",
            "intent_switch": "意图切换",
            "confirmation_flow": "确认流程",
            "robustness": "鲁棒性",
            "positive_case": "正向用例",
            "negative_case": "负向用例",
            "reverse_description": "反向描述",
            "field_update": "字段更新",
            "field_delete": "字段删除",
            "missing_field": "缺失字段",
            "abnormal_input": "异常输入",
            "cross_skill": "跨Skill",
        }

        # Tags 进度条
        bars = "<h3 style='font-size:13px;color:#94a3b8;margin-bottom:12px'>Capability Tags</h3>"
        for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
            pct = count / total_cases * 100
            bar_class = "bar-pass" if pct >= 60 else ("bar-warn" if pct >= 30 else "bar-fail")
            label = tag_labels.get(tag, tag)
            bars += f"""
            <div class="layer-bar-row">
                <div class="layer-bar-label">{label}</div>
                <div class="layer-bar-track">
                    <div class="layer-bar-fill {bar_class}" style="width:{pct}%"></div>
                </div>
                <div class="layer-bar-value">{count}/{total_cases}</div>
            </div>"""

        # Scenarios
        if scenario_counts:
            bars += "<h3 style='font-size:13px;color:#94a3b8;margin:20px 0 12px'>Scenarios</h3>"
            for scenario, count in sorted(scenario_counts.items(), key=lambda x: -x[1]):
                pct = count / total_cases * 100
                bars += f"""
                <div class="layer-bar-row">
                    <div class="layer-bar-label">{scenario}</div>
                    <div class="layer-bar-track">
                        <div class="layer-bar-fill bar-pass" style="width:{pct}%"></div>
                    </div>
                    <div class="layer-bar-value">{count}</div>
                </div>"""

        # Type + Difficulty（横向卡片）
        type_labels = {"confirmation": "确认流程", "multi_turn": "多轮对话", "single": "单轮"}
        diff_labels = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}
        diff_colors = {"easy": "green", "medium": "amber", "hard": "red"}

        bars += '<div style="display:flex;gap:24px;margin-top:20px;flex-wrap:wrap">'
        # Type
        bars += '<div><h3 style="font-size:13px;color:#94a3b8;margin-bottom:8px">Case Type</h3><div style="display:flex;gap:8px">'
        for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            label = type_labels.get(t, t)
            bars += f'<div class="q-card" style="padding:12px 16px"><div class="q-label">{label}</div><div class="q-value blue" style="font-size:20px">{count}</div></div>'
        bars += '</div></div>'
        # Difficulty
        if difficulty_counts:
            bars += '<div><h3 style="font-size:13px;color:#94a3b8;margin-bottom:8px">Difficulty</h3><div style="display:flex;gap:8px">'
            for d in ["easy", "medium", "hard"]:
                count = difficulty_counts.get(d, 0)
                if count:
                    color = diff_colors.get(d, "blue")
                    bars += f'<div class="q-card" style="padding:12px 16px"><div class="q-label">{diff_labels.get(d, d)}</div><div class="q-value {color}" style="font-size:20px">{count}</div></div>'
            bars += '</div></div>'
        bars += '</div>'

        return bars
