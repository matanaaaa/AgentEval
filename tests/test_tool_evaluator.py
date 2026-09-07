"""
Tool Calling 参数级评测测试

覆盖：
- Tool name 匹配
- Arguments subset match（期望参数是实际的子集）
- 缺失参数 → FAIL
- 参数值错误 → FAIL
- 多余参数不影响（subset 匹配）
- int "1" vs 1 normalization
- None vs "" → FAIL
- 参数顺序不同 → PASS
- status 检查
- 多 tool 评测
- 向后兼容 tools_required 简单模式
"""
import pytest
from runner.sse_parser import AgentTrace, SkillTrace, ToolTrace, BusinessResult
from evaluator.evaluator import AgentEvaluator


@pytest.fixture
def evaluator():
    return AgentEvaluator()


def _make_trace_with_tools(tools_data: list) -> AgentTrace:
    """
    构造含 tool 参数的 trace。

    tools_data: [{"name": "x", "arguments": {...}, "status": "complete"}, ...]
    """
    trace = AgentTrace()
    trace.skill = SkillTrace(
        skill_name="extract-data", skill_key="extract-data",
        status="complete", duration_ms=1000, match_method="semantic",
    )
    for td in tools_data:
        trace.tools.append(ToolTrace(
            tool_name=td["name"],
            execution_type="tool",
            status=td.get("status", "complete"),
            run_id=f"run-{td['name']}-{id(td)}",
            arguments=td.get("arguments", {}),
            result=td.get("result"),
            error=td.get("error"),
        ))
    trace.business_result = BusinessResult(
        object_apikey="contact", is_created=False,
        extracted_fields=[], message="",
    )
    trace.total_duration_ms = 1000
    trace.cost_credits = 1.0
    return trace


# ============================================================
# 参数级评测 (expected_tool_calls)
# ============================================================
class TestToolArgEvaluation:
    """expected_tool_calls 参数级评测"""

    def test_exact_match_pass(self, evaluator):
        """参数完全一致 → PASS"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "张三", "company": "腾讯"}}
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三", "company": "腾讯"}}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True

    def test_subset_match_pass(self, evaluator):
        """期望参数是实际的子集 → PASS（多余参数不影响）"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "张三", "company": "腾讯", "phone": "138", "extra": "xxx"}}
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三", "company": "腾讯"}}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True

    def test_missing_argument_fail(self, evaluator):
        """期望参数在实际中不存在 → FAIL"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "张三"}}
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三", "company": "腾讯"}}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is False
        assert "company" in result.layers[1].detail

    def test_wrong_value_fail(self, evaluator):
        """参数值错误 → FAIL"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "李四", "company": "腾讯"}}
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三", "company": "腾讯"}}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is False

    def test_tool_not_called_fail(self, evaluator):
        """期望的 tool 根本没调用 → FAIL"""
        trace = _make_trace_with_tools([
            {"name": "other_tool", "arguments": {"x": 1}}
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三"}}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is False
        assert "未调用" in result.layers[1].detail

    def test_status_check_pass(self, evaluator):
        """status 匹配 → PASS"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "张三"}, "status": "complete"}
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三"}, "status": "complete"}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True

    def test_status_check_fail(self, evaluator):
        """status 不匹配 → FAIL"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "张三"}, "status": "error"}
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三"}, "status": "complete"}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is False
        assert "status" in result.layers[1].detail


# ============================================================
# 参数值规范化匹配
# ============================================================
class TestArgValueNormalization:

    @pytest.mark.parametrize("actual_val,expected_val,should_match", [
        # 完全一致
        ("张三", "张三", True),
        # int vs str
        (1, "1", True),
        ("1", 1, True),
        # float
        (1.0, 1, True),
        (3.14, "3.14", True),
        # None vs None
        (None, None, True),
        # None vs 非 None
        (None, "", False),
        ("", None, False),
        (None, "张三", False),
        # strip whitespace
        ("  张三  ", "张三", True),
        # 不同值
        ("张三", "李四", False),
        (100, 200, False),
    ])
    def test_arg_value_match(self, evaluator, actual_val, expected_val, should_match):
        result = evaluator._arg_value_match(actual_val, expected_val)
        assert result == should_match, (
            f"_arg_value_match({actual_val!r}, {expected_val!r}) = {result}, expected {should_match}"
        )


# ============================================================
# 多 Tool 评测
# ============================================================
class TestMultiToolEval:

    def test_multiple_tools_all_pass(self, evaluator):
        trace = _make_trace_with_tools([
            {"name": "resolve_entity", "arguments": {"entity": "腾讯"}},
            {"name": "create_contact", "arguments": {"name": "张三", "company_id": 123}},
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "resolve_entity", "arguments": {"entity": "腾讯"}},
                {"name": "create_contact", "arguments": {"name": "张三"}},
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True

    def test_multiple_tools_one_fails(self, evaluator):
        trace = _make_trace_with_tools([
            {"name": "resolve_entity", "arguments": {"entity": "腾讯"}},
            {"name": "create_contact", "arguments": {"name": "李四"}},  # 错误
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "resolve_entity", "arguments": {"entity": "腾讯"}},
                {"name": "create_contact", "arguments": {"name": "张三"}},
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is False

    def test_multiple_calls_same_tool_uses_last(self, evaluator):
        """同名 tool 被调用多次，取最后一次"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "错误"}},
            {"name": "create_contact", "arguments": {"name": "张三"}},
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三"}},
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True


# ============================================================
# 向后兼容：tools_required 简单模式
# ============================================================
class TestBackwardCompatibility:

    def test_tools_required_still_works(self, evaluator):
        """没有 expected_tool_calls 时退回简单模式"""
        trace = _make_trace_with_tools([
            {"name": "extract-data", "arguments": {}},
            {"name": "save_record", "arguments": {}},
        ])
        expected = {
            "skill": "extract-data",
            "tools_required": ["save_record"],
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True

    def test_tools_required_missing_tool(self, evaluator):
        trace = _make_trace_with_tools([
            {"name": "extract-data", "arguments": {}},
        ])
        expected = {
            "skill": "extract-data",
            "tools_required": ["save_record"],
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is False

    def test_expected_tool_calls_takes_priority(self, evaluator):
        """当 expected_tool_calls 存在时，忽略 tools_required"""
        trace = _make_trace_with_tools([
            {"name": "create_contact", "arguments": {"name": "张三"}},
        ])
        expected = {
            "skill": "extract-data",
            "tools_required": ["save_record"],  # 这个被忽略
            "expected_tool_calls": [
                {"name": "create_contact", "arguments": {"name": "张三"}}
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True


# ============================================================
# 无参数时只检查 tool 存在
# ============================================================
class TestNoArgumentsCheck:

    def test_no_arguments_in_expected_only_checks_name(self, evaluator):
        """expected_tool_calls 不指定 arguments 时只检查 tool 是否被调用"""
        trace = _make_trace_with_tools([
            {"name": "save_record", "arguments": {"whatever": "anything"}},
        ])
        expected = {
            "skill": "extract-data",
            "expected_tool_calls": [
                {"name": "save_record"}  # 没有 arguments
            ]
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True
