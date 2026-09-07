"""轮次状态断言（state_assertion）与字段存在性约束（field_constraints）测试"""
import pytest

from evaluator.evaluator import AgentEvaluator
from runner.sse_parser import AgentTrace, BusinessResult


def _trace(extracted: list) -> AgentTrace:
    trace = AgentTrace()
    trace.business_result = BusinessResult(
        object_apikey="contact",
        extracted_fields=extracted,
    )
    return trace


def _field(apikey, value, label=None):
    return {"apikey": apikey, "value": value, "label": label if label is not None else value}


@pytest.fixture
def evaluator():
    return AgentEvaluator()


class TestStateAssertionFields:
    def test_all_fields_match(self, evaluator):
        trace = _trace([_field("depart", "技术部"), _field("post", "总监")])
        res = evaluator.check_state_assertion(trace, {"fields": {"depart": "技术部", "post": "总监"}})
        assert res["passed"] is True

    def test_missing_field_fails(self, evaluator):
        trace = _trace([_field("depart", "技术部")])
        res = evaluator.check_state_assertion(trace, {"fields": {"post": "总监"}})
        assert res["passed"] is False
        assert "post" in res["detail"]

    def test_wrong_value_fails(self, evaluator):
        trace = _trace([_field("post", "经理")])
        res = evaluator.check_state_assertion(trace, {"fields": {"post": "总监"}})
        assert res["passed"] is False
        assert "值不符" in res["detail"]

    def test_matches_via_label_when_value_is_internal_code(self, evaluator):
        """gender 内部值是 1，label 是「男」，期望值写中文也应匹配"""
        trace = _trace([_field("gender", "1", "男")])
        res = evaluator.check_state_assertion(trace, {"fields": {"gender": "男"}})
        assert res["passed"] is True

    def test_post_abbreviation_is_case_insensitive(self, evaluator):
        trace = _trace([_field("post", "vp")])
        res = evaluator.check_state_assertion(trace, {"fields": {"post": "VP"}})
        assert res["passed"] is True

    def test_no_business_result_fails_when_fields_expected(self, evaluator):
        res = evaluator.check_state_assertion(AgentTrace(), {"fields": {"post": "总监"}})
        assert res["passed"] is False

    def test_empty_assertion_passes(self, evaluator):
        assert evaluator.check_state_assertion(AgentTrace(), {})["passed"] is True


class TestStateAssertionAbsent:
    def test_absent_satisfied_when_field_missing(self, evaluator):
        trace = _trace([_field("email", "a@b.com")])
        res = evaluator.check_state_assertion(trace, {"absent": ["mobile"]})
        assert res["passed"] is True

    def test_absent_violated_when_field_present(self, evaluator):
        trace = _trace([_field("mobile", "13800138000")])
        res = evaluator.check_state_assertion(trace, {"absent": ["mobile"]})
        assert res["passed"] is False
        assert "应已删除" in res["detail"]

    def test_absent_satisfied_when_value_emptied(self, evaluator):
        """字段还在但值被清空，视为已删除"""
        trace = _trace([_field("mobile", "")])
        res = evaluator.check_state_assertion(trace, {"absent": ["mobile"]})
        assert res["passed"] is True

    def test_fields_and_absent_combined(self, evaluator):
        trace = _trace([_field("contactName", "陈鑫"), _field("mobile", "13800138000")])
        res = evaluator.check_state_assertion(
            trace, {"fields": {"contactName": "陈鑫"}, "absent": ["mobile"]}
        )
        assert res["passed"] is False
        assert len(res["violations"]) == 1


class TestFieldConstraints:
    def test_absent_constraint_satisfied(self, evaluator):
        trace = _trace([_field("contactName", "陈鑫")])
        layer, _ = evaluator._eval_fields(
            trace, {"fields": {"contactName": "陈鑫"}, "field_constraints": {"mobile": "absent"}}
        )
        assert layer.passed is True

    def test_absent_constraint_violated_fails_layer(self, evaluator):
        trace = _trace([_field("contactName", "陈鑫"), _field("mobile", "13800138000")])
        layer, metrics = evaluator._eval_fields(
            trace, {"fields": {"contactName": "陈鑫"}, "field_constraints": {"mobile": "absent"}}
        )
        assert layer.passed is False
        assert "字段约束违反" in layer.detail
        assert any(w["field"] == "mobile" for w in metrics.wrong_fields)

    def test_present_constraint_violated(self, evaluator):
        trace = _trace([_field("contactName", "陈鑫")])
        layer, _ = evaluator._eval_fields(
            trace, {"fields": {"contactName": "陈鑫"}, "field_constraints": {"mobile": "present"}}
        )
        assert layer.passed is False

    def test_constraints_only_without_value_assertions(self, evaluator):
        """只有约束、没有 fields 时也要生效（F1 分母为 0 不应放行）"""
        trace = _trace([_field("mobile", "13800138000")])
        layer, _ = evaluator._eval_fields(trace, {"field_constraints": {"mobile": "absent"}})
        assert layer.passed is False

    def test_no_assertions_skips_layer(self, evaluator):
        layer, metrics = evaluator._eval_fields(_trace([]), {})
        assert layer.passed is True
        assert metrics is None

    def test_constrained_field_not_counted_as_extra(self, evaluator):
        """被约束的字段已单独判过，不该再算进 extra 拉低 precision"""
        trace = _trace([_field("contactName", "陈鑫"), _field("mobile", "13800138000")])
        _, metrics = evaluator._eval_fields(
            trace, {"fields": {"contactName": "陈鑫"}, "field_constraints": {"mobile": "absent"}}
        )
        assert "mobile" not in metrics.extra_fields


class TestStateLayerAggregation:
    """main.eval_state_assertions 把逐轮结果汇总成 L5_轮次状态 层"""

    def test_returns_none_when_no_assertions(self, evaluator):
        import main

        turns = [{"turn_id": "T01", "input": "a"}]
        assert main.eval_state_assertions(evaluator, turns, [_trace([])]) is None

    def test_layer_passes_when_all_turns_pass(self, evaluator):
        import main

        turns = [{
            "turn_id": "T01", "input": "a",
            "expected": {"state_assertion": {"fields": {"depart": "技术部"}}},
        }]
        layer = main.eval_state_assertions(evaluator, turns, [_trace([_field("depart", "技术部")])])
        assert layer.layer == "L5_轮次状态"
        assert layer.passed is True

    def test_layer_fails_and_names_failing_turn(self, evaluator):
        import main

        turns = [
            {"turn_id": "C01T01", "input": "a",
             "expected": {"state_assertion": {"fields": {"depart": "技术部"}}}},
            {"turn_id": "C01T04", "input": "b",
             "expected": {"state_assertion": {"absent": ["mobile"]}}},
        ]
        traces = [
            _trace([_field("depart", "技术部")]),
            _trace([_field("mobile", "13800138000")]),
        ]
        layer = main.eval_state_assertions(evaluator, turns, traces)
        assert layer.passed is False
        assert "C01T04" in layer.detail
        assert layer.actual == "通过 1/2"

    def test_failure_analyzer_classifies_state_layer(self, evaluator):
        from evaluator.evaluator import EvalResult, LayerResult
        from evaluator.failure_analyzer import FailureAnalyzer

        result = EvalResult(
            case_id="X",
            layers=[LayerResult(layer="L5_轮次状态", passed=False, detail="C01T04: mobile 应已删除")],
            overall_pass=False,
        )
        report = FailureAnalyzer().analyze(result)
        assert report.primary_cause == "STATE_ASSERTION_ERROR"
