"""测试文本断言逻辑"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from main import check_text_assertions, eval_text_assertions
from runner.sse_parser import AgentTrace


class TestAllAssertion:
    def test_all_present(self):
        result = check_text_assertions("联系人陈鑫已创建成功", {"all": ["陈鑫", "创建成功"]})
        assert result["passed"] is True

    def test_some_missing(self):
        result = check_text_assertions("联系人陈鑫", {"all": ["陈鑫", "创建成功"]})
        assert result["passed"] is False
        assert "创建成功" in result["detail"]


class TestAnyAssertion:
    def test_at_least_one_found(self):
        result = check_text_assertions("创建成功", {"any": ["成功", "完成", "已创建"]})
        assert result["passed"] is True

    def test_none_found(self):
        result = check_text_assertions("请确认信息", {"any": ["成功", "完成", "已创建"]})
        assert result["passed"] is False


class TestNotAnyAssertion:
    def test_none_present_passes(self):
        result = check_text_assertions("联系人陈鑫", {"not_any": ["创建人", "创建日期"]})
        assert result["passed"] is True

    def test_some_present_fails(self):
        result = check_text_assertions("创建人是张三", {"not_any": ["创建人", "创建日期"]})
        assert result["passed"] is False


class TestIanyAssertion:
    def test_case_insensitive_match(self):
        result = check_text_assertions("他是VP", {"iany": ["vp", "副总裁", "副总"]})
        assert result["passed"] is True

    def test_case_insensitive_no_match(self):
        result = check_text_assertions("他是经理", {"iany": ["vp", "副总裁", "副总"]})
        assert result["passed"] is False


class TestCombined:
    def test_multiple_assertions(self):
        text = "联系人陈鑫，VP，已确认"
        assertions = {
            "all": ["陈鑫"],
            "any": ["确认", "创建"],
            "iany": ["vp", "VP"],
            "not_any": ["错误", "失败"],
        }
        result = check_text_assertions(text, assertions)
        assert result["passed"] is True

    def test_empty_text(self):
        result = check_text_assertions("", {"all": ["陈鑫"]})
        assert result["passed"] is False

    def test_empty_assertions(self):
        result = check_text_assertions("任意文本", {})
        assert result["passed"] is True


class TestTextAssertionLayer:
    def test_turn_assertion_passes(self):
        turns = [{
            "turn_id": "T01",
            "expected": {"text_assertions": {"all": ["中铁十二局"]}},
        }]
        traces = [AgentTrace(reply_text="已识别客户中铁十二局，请确认")]

        layer = eval_text_assertions(turns, traces)

        assert layer.layer == "L5_文本响应"
        assert layer.passed is True

    def test_turn_assertion_failure_is_a_failed_layer(self):
        turns = [{
            "turn_id": "T01",
            "expected": {"text_assertions": {"all": ["中铁十二局", "确认"]}},
        }]
        traces = [AgentTrace(reply_text="已识别其他客户")]

        layer = eval_text_assertions(turns, traces)

        assert layer.passed is False
        assert "T01" in layer.detail
        assert "中铁十二局" in layer.detail

    def test_case_level_assertion_checks_combined_replies(self):
        turns = [{"turn_id": "T01", "expected": {}}]
        traces = [AgentTrace(reply_text="客户创建完成")]
        evaluation = {"text_assertions": {"any": ["成功", "完成"]}}

        layer = eval_text_assertions(turns, traces, evaluation)

        assert layer.passed is True
