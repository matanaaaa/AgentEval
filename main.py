"""
AgentEval - Agent 五层评测主入口

用法:
    python main.py                          # 运行所有 cases 目录下的用例
    python main.py --case CREATECONTACT001  # 运行指定用例
    python main.py --file cases/contact_create.json  # 运行指定文件
    python main.py --tag v2.1               # 带版本标签，用于报告对比
    python main.py --no-gate                # 跳过质量门禁（始终返回 0）

退出码:
    0  通过（含门禁达标）
    1  质量门禁未达标
    2  回归对比发现退化（--compare 模式）
    3  参数或用例加载错误
"""

import json
import glob
import argparse
import sys
import os

import config
from runner.agent_runner import AgentRunner
from runner.sse_parser import AgentTrace
from case_schema import CaseValidationError, validate_cases
from evaluator.evaluator import AgentEvaluator, EvalResult, LayerResult
from evaluator.quality_gate import (
    QualityGate,
    EXIT_OK,
    EXIT_GATE_FAILED,
    EXIT_REGRESSION,
    EXIT_USAGE_ERROR,
)
from reports.reporter import Reporter


def configure_console():
    """
    强制标准输出用 UTF-8。

    报告里用了 ✓ ✗ ⚠️ 🔴 等符号，Windows 控制台默认 GBK 编不了其中一部分
    （'✓' 侥幸能编，'✗' 不能），会在打印失败用例时抛 UnicodeEncodeError
    直接中断整轮评测。errors="replace" 兜底，保证再冷门的字符也不会中断流程。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def load_cases(file_path: str = None, case_id: str = None) -> list:
    """加载测试用例"""
    cases = []

    if file_path:
        files = [file_path]
    else:
        pattern = os.path.join(config.CASES_DIR, "*.json")
        files = glob.glob(pattern)

    for f in files:
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
            if isinstance(data, list):
                cases.extend(data)
            else:
                cases.append(data)

    if case_id:
        cases = [c for c in cases if c["id"] == case_id]
        if not cases:
            print(f"错误: 未找到用例 {case_id}")
            sys.exit(EXIT_USAGE_ERROR)

    return cases


def build_trace_refs(traces: list, turns: list = None) -> list:
    """
    构建每轮的链路关联信息。

    日志平台（kt.sre）的 traceId 与 SSE 的 trace_url 不同源，无法直接映射。
    但后端日志正文里会原样出现 run_id（如 surface=chat-inline-<run_id>-0）和
    current_message_id（如 message_id:<id> / trace_id:ft-<id>-...），
    因此用「请求时间窗 + run_id 关键字」定位日志，是可靠的关联方式。
    """
    refs = []
    for i, t in enumerate(traces):
        if turns and i < len(turns):
            turn_id = turns[i].get("turn_id") or f"T{i+1}"
        else:
            turn_id = f"T{i+1}"

        # 关键字优先用 run_id，退化到 message_id
        keyword = t.run_id or (str(t.current_message_id) if t.current_message_id else "")

        refs.append({
            "turn_id": turn_id,
            "run_id": t.run_id,
            "thread_id": t.thread_id,
            "message_id": t.current_message_id,
            "conversation_id": t.conversation_id,
            "ai_trace_path": t.trace_url,       # Langfuse 系路径 /{project}/traces/{id}
            "infra_trace_id": t.infra_trace_id,  # 来自响应头，可能为空
            "started_at_ms": t.started_at_ms,
            "finished_at_ms": t.finished_at_ms,
            "log_url": config.build_log_viewer_url(
                t.started_at_ms, t.finished_at_ms, keyword=keyword
            ) if t.started_at_ms else "",
        })
    return refs


def eval_state_assertions(evaluator: AgentEvaluator, turns: list, traces: list):
    """
    逐轮执行 state_assertion，汇总成一个附加层 L5_轮次状态。

    为什么单独成层：五层是用例级判定，而多轮改写/删除类用例的问题往往出在
    中间某一轮（比如第 4 轮要求删手机号但状态里还留着），只看最终结果发现不了。

    没有任何轮次定义 state_assertion 时返回 None（不引入空层）。
    """
    checks = []
    for i, (turn, trace) in enumerate(zip(turns, traces)):
        assertion = (turn.get("expected") or {}).get("state_assertion")
        if not assertion:
            continue

        turn_id = turn.get("turn_id") or f"T{i+1}"
        res = evaluator.check_state_assertion(trace, assertion)
        checks.append((turn_id, res))

        icon = "✓" if res["passed"] else "✗"
        print(f"    {icon} {turn_id} 状态断言: {res['detail']}")

    if not checks:
        return None

    failed = [(tid, res) for tid, res in checks if not res["passed"]]
    return LayerResult(
        layer="L5_轮次状态",
        passed=not failed,
        expected=f"{len(checks)} 轮状态断言",
        actual=f"通过 {len(checks) - len(failed)}/{len(checks)}",
        detail="; ".join(f"{tid}: {res['detail']}" for tid, res in failed) or "全部通过",
    )


def eval_text_assertions(turns: list, traces: list, evaluation: dict = None):
    """逐轮执行文本断言，并汇总成会影响用例成败的 L5 附加层。"""
    checks = []

    for i, (turn, trace) in enumerate(zip(turns, traces)):
        assertions = (turn.get("expected") or {}).get("text_assertions")
        if not assertions:
            continue

        turn_id = turn.get("turn_id") or f"T{i+1}"
        res = check_text_assertions(trace.reply_text, assertions)
        checks.append((turn_id, res))

        icon = "✓" if res["passed"] else "✗"
        print(f"    {icon} {turn_id} 文本断言: {res['detail']}")

    # 兼容用例级文本断言：对全部轮次回复的合并文本执行一次检查。
    case_assertions = (evaluation or {}).get("text_assertions")
    if case_assertions:
        combined_text = "\n".join(trace.reply_text for trace in traces)
        res = check_text_assertions(combined_text, case_assertions)
        checks.append(("用例整体", res))

        icon = "✓" if res["passed"] else "✗"
        print(f"    {icon} 用例整体文本断言: {res['detail']}")

    if not checks:
        return None

    failed = [(label, res) for label, res in checks if not res["passed"]]
    return LayerResult(
        layer="L5_文本响应",
        passed=not failed,
        expected=f"{len(checks)} 组文本断言",
        actual=f"通过 {len(checks) - len(failed)}/{len(checks)}",
        detail="; ".join(f"{label}: {res['detail']}" for label, res in failed) or "全部通过",
    )


def print_extracted_fields(turns: list, traces: list):
    """
    打印每轮 Agent 实际抽取的字段（apikey / value / label）。

    用途：写用例断言时需要知道内部编码到底是什么。像 contactRole、entityType
    这类字段，value 是内部 ID（如 "1" 或负数 ID），label 才是中文，
    光看报告里的字段名无法判断该断言写哪个值。
    """
    print(f"\n  {'─'*70}")
    print(f"  实际抽取字段（写用例断言时参照此处，不要凭猜测填内部编码）")
    print(f"  {'─'*70}")

    for i, trace in enumerate(traces):
        turn_id = turns[i].get("turn_id") or f"T{i+1}" if i < len(turns) else f"T{i+1}"
        biz = trace.business_result
        fields = biz.extracted_fields if biz else []

        print(f"\n  [{turn_id}] object={biz.object_apikey if biz else '-'} "
              f"entity_type={biz.entity_type_apikey if biz else '-'} "
              f"isCreated={biz.is_created if biz else '-'}")

        if not fields:
            print("      （本轮无抽取字段）")
            continue

        print(f"      {'apikey':<18}{'value':<28}label")
        for item in fields:
            apikey = str(item.get("apikey", ""))
            value = str(item.get("value", ""))
            label = str(item.get("label", ""))
            print(f"      {apikey:<18}{value:<28}{label}")


def run_case(runner: AgentRunner, evaluator: AgentEvaluator, case: dict,
             dump_fields: bool = False) -> EvalResult:
    """
    执行单个测试用例。

    评测原则：
    - 每轮独立保存 trace
    - L1 Skill 和 L2 Tool 使用所有轮次的合并结果
    - L3 字段抽取取 extracted_fields 最完整的那一轮
    - L4 业务结果只有 isCreated=true 且 created_record_id 非空才算通过
    - L5 轮次状态逐轮执行 state_assertion
    """
    case_id = case["id"]
    case_type = case.get("type", "single")
    turns = case.get("turns", [])
    # 兼容新格式 evaluation 和旧格式 expected
    expected = case.get("evaluation", case.get("expected", {}))

    print(f"\n  执行用例: {case_id} ({case.get('name', '')})")
    print(f"  类型: {case_type} | 轮次: {len(turns)}")

    # 回放执行器需要知道当前是哪个用例（真实 AgentRunner 没有这个方法）
    if hasattr(runner, "set_case"):
        runner.set_case(case_id)

    traces = []
    request_ok = True
    try:
        if case_type == "single":
            message = turns[0]["input"] if turns else case.get("input", "")
            trace = runner.run_single(message)
            traces = [trace]

            # 评测
            result = evaluator.evaluate(trace, expected, case_id=case_id)

        elif case_type in ("multi", "multi_turn", "confirmation"):
            # 多轮：按顺序发送，保存每轮独立 trace
            traces = runner.run_multi(turns)

            # 构建评测用的合并 trace
            merged_trace = _merge_traces_for_eval(traces)
            if merged_trace:
                result = evaluator.evaluate(merged_trace, expected, case_id=case_id)
            else:
                result = EvalResult(case_id=case_id)

        else:
            print(f"    未知用例类型: {case_type}")
            result = EvalResult(case_id=case_id)

    except Exception as e:
        # 请求彻底失败（重试耗尽 / 认证失败等）：标记为请求不成功，
        # 供性能层聚合 Request Success Rate。仍产出空结果以免中断整批。
        print(f"    执行异常: {e}")
        import traceback
        traceback.print_exc()
        request_ok = False
        result = EvalResult(case_id=case_id)

    # 运行态指标：Request 成功、Timeout、Tool 成功率的原始计数
    result.request_ok = request_ok
    _fill_runtime_metrics(result, traces)

    if dump_fields and traces:
        print_extracted_fields(turns, traces)

    # 文本与轮次状态断言均为硬断言，会影响 overall_pass。
    if traces:
        text_layer = eval_text_assertions(turns, traces, expected)
        if text_layer:
            result.layers.append(text_layer)

        state_layer = eval_state_assertions(evaluator, turns, traces)
        if state_layer:
            result.layers.append(state_layer)

        result.overall_pass = all(lr.passed for lr in result.layers)

    # 附加链路关联信息（即使评测异常，只要跑过就留下可排查的入口）
    if traces:
        result.trace_refs = build_trace_refs(traces, turns)

    # 把原始 traces 暂挂在结果上，供性能层做 Tool 级别耗时分析（不进 JSON 报告）
    result._traces = traces

    return result


def _fill_runtime_metrics(result: EvalResult, traces: list):
    """
    从各轮 trace 聚合运行态指标，写回 EvalResult。

    - timed_out: 任一轮 incomplete（单轮超时或未收到 RUN_FINISHED）即视为超时
    - tool_total / tool_success: 全轮次 Tool 调用数与非 error 的数量，
      供性能层计算 Tool Success Rate（trace.tools 已按 run_id 去重）
    """
    if not traces:
        return

    result.timed_out = any(getattr(t, "incomplete", False) for t in traces)

    total = 0
    success = 0
    for t in traces:
        for tool in getattr(t, "tools", []):
            total += 1
            if str(getattr(tool, "status", "")).lower() != "error" and not getattr(tool, "error", None):
                success += 1
    result.tool_total = total
    result.tool_success = success


def _merge_traces_for_eval(traces: list) -> AgentTrace:
    """
    合并多轮 traces 用于五层评测。

    规则：
    - L1 Skill: 收集所有轮次出现过的 Skill（按 skill_key 去重、保序）。
      trace.skill 仍保留第一个，只作为「主 Skill」用于展示 status/match_method。
    - L2 Tools: 取所有轮次 tool 的并集
    - L3 字段: 取「最后一个带 extracted 的轮次」的完整快照（Agent 每轮返回全量快照，
      能正确反映字段删除）
    - L4 业务结果: 取 isCreated=True 的那一轮（或最后一轮）
    - L3 和 L4 分开取，不互相干扰
    """
    if not traces:
        return None

    merged = AgentTrace()
    latest_biz_with_fields = None  # L3 用：最后一个带 extracted 且未落库的完整快照
    latest_created_with_fields = None  # L3 fallback：最后一个带 extracted 但已落库的快照
    success_biz = None  # L4 用：isCreated=True 的
    last_biz = None  # fallback

    seen_skill_keys = set()

    for trace in traces:
        # Skill: 主 Skill 取第一个出现的，供展示用
        if not merged.skill and trace.skill:
            merged.skill = trace.skill

        # Skill 集合: 跨轮次并集（按 skill_key 去重、保序）。
        # 意图切换类用例（先查客户 → 再建联系人）必须看全集才能正确评测。
        turn_skills = trace.skills or ([trace.skill] if trace.skill else [])
        for skill in turn_skills:
            if skill and skill.skill_key not in seen_skill_keys:
                merged.skills.append(skill)
                seen_skill_keys.add(skill.skill_key)

        # Tools: 并集（按 run_id 去重）
        existing_run_ids = {t.run_id for t in merged.tools}
        for tool in trace.tools:
            if tool.run_id not in existing_run_ids:
                merged.tools.append(tool)
                existing_run_ids.add(tool.run_id)

        # Business result
        if trace.business_result:
            last_biz = trace.business_result

            # L5: 记录创建成功的那一轮
            if trace.business_result.is_created and trace.business_result.created_record_id:
                success_biz = trace.business_result

            # L3: 取「最后一个带 extracted 且尚未落库(isCreated=false)的轮次」的完整快照。
            # 该 Agent 在待确认/校验阶段每轮 extracted 返回的都是当前完整快照（未变字段
            # 也会原样重复，round_count 递增可佐证），因此后一轮快照即当前真值。
            #
            # 旧的 latest-write-wins 逐字段覆盖只有「覆盖」没有「删除」语义：字段被
            # 清空/删除时，它在后续轮次的 extracted 里直接消失（而非以空值出现），
            # 累积逻辑无法感知「消失=删除」，会永久保留早期轮次的旧值。这会让所有
            # 「删除字段」类用例（如 CREATEACCOUNT027 的 phone:absent）被误判为约束
            # 违反。改用整轮快照后，删除能被正确反映。
            #
            # 但「确认创建」成功后返回的结果卡(isCreated=true)是精简摘要卡，只回显
            # 姓名/类型/创建时间等少数字段，并非完整抽取快照（见 CREATECONTACT001：
            # T01 完整 10 字段 → T02 结果卡仅 3 字段）。若直接取最后一个带 extracted
            # 的轮次会被这张精简卡冲掉完整快照，导致 L3 大面积误判缺失。因此 L3 优先
            # 取「未落库」的完整校验快照；仅当全程没有未落库快照时，才回退到已落库的。
            if trace.business_result.extracted_fields:
                if trace.business_result.is_created:
                    latest_created_with_fields = trace.business_result
                else:
                    latest_biz_with_fields = trace.business_result

        # 累加统计
        merged.total_duration_ms += trace.total_duration_ms
        merged.cost_credits += trace.cost_credits
        merged.reply_text += trace.reply_text

    # 构建最终 business_result：
    # extracted_fields 优先使用「最后一个未落库(isCreated=false)的完整校验快照」（给 L3 用），
    # 能正确反映字段删除、且不被确认成功后的精简结果卡冲掉；仅当全程无未落库快照时，
    # 才回退到已落库的快照。is_created / created_record_id 使用成功轮次的业务状态（给 L4 用）。
    l3_biz = latest_biz_with_fields or latest_created_with_fields
    if l3_biz:
        from runner.sse_parser import BusinessResult
        metadata_biz = success_biz or last_biz or l3_biz
        merged.business_result = BusinessResult(
            task_id=metadata_biz.task_id,
            object_apikey=metadata_biz.object_apikey,
            entity_type_apikey=metadata_biz.entity_type_apikey,
            extracted_fields=list(l3_biz.extracted_fields),
            entity_info=metadata_biz.entity_info,
            # L4: 用成功轮次的业务状态
            is_created=success_biz.is_created if success_biz else (last_biz.is_created if last_biz else False),
            created_record_id=success_biz.created_record_id if success_biz else (last_biz.created_record_id if last_biz else None),
            message=success_biz.message if success_biz else (last_biz.message if last_biz else ""),
        )
    elif last_biz:
        merged.business_result = last_biz

    return merged


def check_text_assertions(text: str, assertions: dict) -> dict:
    """
    检查文本断言。

    支持的断言类型:
        all: 所有关键词都必须出现
        any: 至少一个关键词出现
        not_any: 所有关键词都不能出现
        iany: 至少一个关键词出现（忽略大小写）

    Returns:
        {"passed": bool, "detail": str}
    """
    issues = []
    text_lower = text.lower()

    # [all] 所有都必须包含
    all_keywords = assertions.get("all", [])
    if all_keywords:
        missing = [kw for kw in all_keywords if kw not in text]
        if missing:
            issues.append(f"[all]缺失: {missing[:5]}{'...' if len(missing)>5 else ''}")

    # [any] 至少包含一个
    any_keywords = assertions.get("any", [])
    if any_keywords:
        found = any(kw in text for kw in any_keywords)
        if not found:
            issues.append(f"[any]均未出现: {any_keywords[:5]}")

    # [not_any] 都不能出现
    not_any_keywords = assertions.get("not_any", [])
    if not_any_keywords:
        found = [kw for kw in not_any_keywords if kw in text]
        if found:
            issues.append(f"[!any]不应出现: {found}")

    # [iany] 忽略大小写至少包含一个
    iany_keywords = assertions.get("iany", [])
    if iany_keywords:
        found = any(kw.lower() in text_lower for kw in iany_keywords)
        if not found:
            issues.append(f"[iany]均未出现: {iany_keywords[:5]}")

    passed = len(issues) == 0
    detail = "通过" if passed else "; ".join(issues)

    return {"passed": passed, "detail": detail}


def main():
    configure_console()

    parser = argparse.ArgumentParser(description="AgentEval - Agent 五层评测系统")
    parser.add_argument("--case", type=str, help="指定运行的用例 ID")
    parser.add_argument("--file", type=str, help="指定用例文件路径")
    parser.add_argument("--tag", type=str, default="", help="报告标签（如模型版本）")
    parser.add_argument("--no-report", action="store_true", help="不保存报告文件")
    parser.add_argument("--report-dir", type=str, default=None,
                        help=f"报告输出目录（默认 {config.REPORTS_DIR}）")
    parser.add_argument("--compare", type=str, nargs=2, metavar=("BASELINE", "CURRENT"),
                        help="回归对比模式：对比两份 JSON 报告")
    parser.add_argument("--no-gate", action="store_true",
                        help="跳过质量门禁判定（始终返回 0）")
    parser.add_argument("--replay", type=str, metavar="DIR",
                        help="离线回放模式：从录制目录读取 SSE，不发任何网络请求")
    parser.add_argument("--skip-validate", action="store_true",
                        help="跳过用例 Schema 校验")
    parser.add_argument("--record", type=str, metavar="DIR",
                        help="录制模式：把每轮原始 SSE 存到该目录，供 --replay 复现")
    parser.add_argument("--dump-fields", action="store_true",
                        help="打印每轮实际抽取的字段（apikey/value/label），用于校准用例断言")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="重复运行稳定性：同一批用例连续跑 N 遍，统计 pass 翻转率与 F1/延迟波动")
    args = parser.parse_args()

    # 回归对比模式
    if args.compare:
        from evaluator.regression import RegressionComparer
        comparer = RegressionComparer()
        try:
            report = comparer.compare(args.compare[0], args.compare[1])
        except (OSError, json.JSONDecodeError) as e:
            print(f"错误: 读取报告失败: {e}")
            return EXIT_USAGE_ERROR
        comparer.print_report(report)
        if args.no_gate:
            return EXIT_OK
        return EXIT_REGRESSION if report.has_regression else EXIT_OK

    # 初始化执行器：离线回放 or 真实请求
    cases_file = args.file
    if args.replay:
        from runner.replay import ReplayRunner, ReplayError, default_cases_path

        # 回放不查数据库，否则 CI 里会发网络请求
        config.DB_CHECK_ENABLED = False
        if not cases_file:
            cases_file = default_cases_path(args.replay)

        try:
            runner = ReplayRunner(args.replay)
        except ReplayError as e:
            print(f"错误: {e}")
            return EXIT_USAGE_ERROR
        print(f"[离线回放] 录制目录: {args.replay}（不发网络请求，跳过 DB Check）")
        if args.record:
            print("[提示] --record 在回放模式下无意义，已忽略")
    else:
        runner = AgentRunner(record_dir=args.record, record_tag=args.tag)
        if args.record:
            tag_hint = f"（按 tag 分目录: {args.tag}）" if args.tag else ""
            print(f"[录制] 原始 SSE 将写入: {args.record}{tag_hint}")
            print("[注意] 录制内容含真实业务数据（姓名/手机/邮箱），提交前请确认脱敏")

    # 加载用例
    try:
        cases = load_cases(file_path=cases_file, case_id=args.case)
    except OSError as e:
        print(f"错误: 读取用例失败: {e}")
        return EXIT_USAGE_ERROR
    except json.JSONDecodeError as e:
        print(f"错误: 用例 JSON 格式错误: {e}")
        return EXIT_USAGE_ERROR

    # Schema 校验：拼错或未支持的字段在这里就拦住，避免断言静默失效
    if not args.skip_validate:
        try:
            validate_cases(cases)
        except CaseValidationError as e:
            print(f"错误: 用例 Schema 校验失败（{len(e.errors)} 项）:")
            for line in e.errors:
                print(line)
            return EXIT_USAGE_ERROR

    print(f"加载了 {len(cases)} 个测试用例")

    evaluator = AgentEvaluator()
    reporter = Reporter(output_dir=args.report_dir)

    # 执行评测
    results = []
    for case in cases:
        result = run_case(runner, evaluator, case, dump_fields=args.dump_fields)
        results.append(result)
        reporter.print_result(result)

    # 重复运行稳定性：同一批用例多跑几遍，统计 pass 翻转率与 F1/延迟波动
    stability_report = None
    if args.repeat and args.repeat > 1:
        from evaluator.stability import StabilityAnalyzer
        stability = StabilityAnalyzer()
        stability.add_run(results)  # 第 1 遍复用上面已跑的结果
        for i in range(2, args.repeat + 1):
            print(f"\n{'#'*60}")
            print(f"  重复运行稳定性: 第 {i}/{args.repeat} 遍")
            print(f"{'#'*60}")
            run_results = []
            for case in cases:
                r = run_case(runner, evaluator, case, dump_fields=False)
                run_results.append(r)
            stability.add_run(run_results)
        stability_report = stability.analyze()
        stability.print_report(stability_report)

    # 失败归因分析
    from evaluator.failure_analyzer import FailureAnalyzer
    analyzer = FailureAnalyzer()
    failure_analysis = analyzer.analyze_batch(results)

    failed_reports = [r for r in failure_analysis["reports"] if r.has_failures]
    if failed_reports:
        print(f"\n{'='*60}")
        print(f"  失败归因分析")
        print(f"{'='*60}")
        for fr in failed_reports:
            print(analyzer.format_report(fr))

        # 失败分类统计
        if failure_analysis["primary_cause_stats"]:
            print(f"  主要失败原因分布:")
            for cause, count in sorted(failure_analysis["primary_cause_stats"].items(), key=lambda x: -x[1]):
                label = FailureAnalyzer.CATEGORIES.get(cause, {}).get("label", cause)
                print(f"    {label}: {count} 次")

    # 性能分析
    from evaluator.performance import PerformanceAnalyzer
    perf_analyzer = PerformanceAnalyzer()
    # 汇总各用例的原始 traces，用于 Tool 级别耗时（含 P99）分析
    all_traces = []
    for r in results:
        all_traces.extend(getattr(r, "_traces", []) or [])
    perf_report = perf_analyzer.analyze(results, traces=all_traces or None)
    perf_analyzer.print_report(perf_report)

    # 汇总
    reporter.print_summary(results)

    # 质量门禁
    gate = QualityGate()
    gate_report = gate.evaluate(results)
    gate.print_report(gate_report)

    # 保存报告
    if not args.no_report:
        reporter.save_report(results, tag=args.tag, failure_analysis=failure_analysis,
                           perf_report=perf_report, cases=cases,
                           gate_report=gate_report, stability_report=stability_report)

    if args.no_gate:
        return EXIT_OK
    return EXIT_OK if gate_report.passed else EXIT_GATE_FAILED


if __name__ == "__main__":
    sys.exit(main())
