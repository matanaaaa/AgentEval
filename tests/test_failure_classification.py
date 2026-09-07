"""
失败归因分类回归测试

起因：一份真实报告里 L3 FAIL（precision=0.5，只多抽 4 个字段），
但 failure_analysis 的 category_stats / primary_cause_stats 全空——
失败形态没被任何分类规则覆盖，报告看着像没问题。
"""
import pytest
import requests.structures as rs

from evaluator.evaluator import EvalResult, FieldMetrics, LayerResult
from evaluator.failure_analyzer import FailureAnalyzer
from runner.sse_parser import extract_infra_trace_id


@pytest.fixture
def analyzer():
    return FailureAnalyzer()


def _result_with_field_metrics(**fm_kwargs) -> EvalResult:
    return EvalResult(
        case_id="C006",
        layers=[
            LayerResult(layer="L3_字段抽取", passed=False,
                        expected="4 个字段", actual="匹配 4, 缺失 0, 错误 0, 多抽 4",
                        detail="P=0.5 R=1.0 F1=0.6667"),
        ],
        field_metrics=FieldMetrics(**fm_kwargs),
        overall_pass=False,
    )


class TestExtraFieldsClassification:
    def test_extra_only_failure_is_classified(self, analyzer):
        """只多抽字段也必须产出归因（原本静默为空）"""
        result = _result_with_field_metrics(
            precision=0.5, recall=1.0, f1=0.6667,
            matched_fields=["contactName", "gender", "mobile", "email"],
            extra_fields=["depart", "post", "entityType", "contactRole"],
        )
        report = analyzer.analyze(result)
        assert report.has_failures
        assert report.primary_cause == "FIELD_EXTRA_ERROR"

    def test_extra_fields_listed_in_summary(self, analyzer):
        result = _result_with_field_metrics(
            precision=0.5, recall=1.0, f1=0.6667, extra_fields=["depart", "post"],
        )
        cause = analyzer.analyze(result).root_causes[0]
        assert "depart" in cause.summary

    def test_batch_stats_not_empty(self, analyzer):
        result = _result_with_field_metrics(
            precision=0.5, recall=1.0, f1=0.6667, extra_fields=["depart"],
        )
        batch = analyzer.analyze_batch([result])
        assert batch["category_stats"] == {"FIELD_EXTRA_ERROR": 1}
        assert batch["primary_cause_stats"] == {"FIELD_EXTRA_ERROR": 1}


class TestConstraintClassification:
    def test_absent_violation_classified_as_constraint(self, analyzer):
        result = _result_with_field_metrics(
            precision=0.5, recall=1.0, f1=0.6667,
            wrong_fields=[{"field": "mobile", "expected": "<absent>", "actual": "13800138000"}],
        )
        report = analyzer.analyze(result)
        assert report.primary_cause == "FIELD_CONSTRAINT_ERROR"
        assert "必须不存在" in report.root_causes[0].summary

    def test_present_violation_classified_as_constraint(self, analyzer):
        result = _result_with_field_metrics(
            precision=0.5, recall=1.0, f1=0.6667,
            wrong_fields=[{"field": "mobile", "expected": "<present>", "actual": "<缺失>"}],
        )
        cause = analyzer.analyze(result).root_causes[0]
        assert cause.category == "FIELD_CONSTRAINT_ERROR"
        assert "必须存在" in cause.summary

    def test_plain_value_error_still_classified_as_value_error(self, analyzer):
        """约束违反不能吃掉普通值错误的分类"""
        result = _result_with_field_metrics(
            precision=0.5, recall=1.0, f1=0.6667,
            wrong_fields=[{"field": "post", "expected": "总监", "actual": "经理"}],
        )
        assert analyzer.analyze(result).primary_cause == "FIELD_VALUE_ERROR"

    def test_mixed_constraint_and_value_errors(self, analyzer):
        result = _result_with_field_metrics(
            precision=0.4, recall=1.0, f1=0.57,
            wrong_fields=[
                {"field": "mobile", "expected": "<absent>", "actual": "138"},
                {"field": "post", "expected": "总监", "actual": "经理"},
            ],
        )
        categories = [c.category for c in analyzer.analyze(result).root_causes]
        assert "FIELD_CONSTRAINT_ERROR" in categories
        assert "FIELD_VALUE_ERROR" in categories


class TestNoFailureLeftUnclassified:
    def test_unknown_layer_gets_fallback_cause(self, analyzer):
        result = EvalResult(
            case_id="X",
            layers=[LayerResult(layer="L9_未知层", passed=False, detail="something")],
            overall_pass=False,
        )
        report = analyzer.analyze(result)
        assert report.primary_cause == "UNCLASSIFIED_FAILURE"

    def test_l3_failure_without_metrics_still_classified(self, analyzer):
        result = EvalResult(
            case_id="X",
            layers=[LayerResult(layer="L3_字段抽取", passed=False)],
            overall_pass=False,
        )
        assert analyzer.analyze(result).has_failures

    @pytest.mark.parametrize("layer_name", [
        "L1_Skill路由", "L2_Tool调用", "L3_字段抽取",
        "L4_业务结果", "L5_轮次状态", "L7_臆造层",
    ])
    def test_every_failing_layer_yields_at_least_one_cause(self, analyzer, layer_name):
        result = EvalResult(
            case_id="X",
            layers=[LayerResult(layer=layer_name, passed=False)],
            overall_pass=False,
        )
        assert len(analyzer.analyze(result).root_causes) >= 1

    def test_passing_result_yields_no_causes(self, analyzer):
        result = EvalResult(
            case_id="X",
            layers=[LayerResult(layer="L3_字段抽取", passed=True)],
            overall_pass=True,
        )
        assert analyzer.analyze(result).has_failures is False


class TestSummaryFieldDetail:
    def test_summary_includes_field_lists(self):
        result = _result_with_field_metrics(
            precision=0.5, recall=1.0, f1=0.6667,
            matched_fields=["contactName"], missing_fields=["email"],
            extra_fields=["depart"], wrong_fields=[{"field": "post", "expected": "总监"}],
        )
        fm = result.summary()["field_metrics"]
        assert fm["matched_fields"] == ["contactName"]
        assert fm["missing_fields"] == ["email"]
        assert fm["extra_fields"] == ["depart"]
        assert fm["wrong_fields"][0]["field"] == "post"

    def test_summary_includes_layer_details(self):
        result = _result_with_field_metrics(precision=0.5, recall=1.0, f1=0.6667)
        details = result.summary()["layer_details"]
        assert details[0]["layer"] == "L3_字段抽取"
        assert details[0]["detail"] == "P=0.5 R=1.0 F1=0.6667"

    def test_summary_without_field_metrics(self):
        result = EvalResult(case_id="X", layers=[LayerResult(layer="L1_Skill路由", passed=True)])
        summary = result.summary()
        assert summary["field_metrics"] is None
        assert len(summary["layer_details"]) == 1


class TestSkyWalkingTraceHeader:
    def test_x_sw_traceid_extracted(self):
        headers = rs.CaseInsensitiveDict({
            "x-sw-traceId": "t.3affaf040991412bbeb51d73419024bb.849.1786707096636"
        })
        assert extract_infra_trace_id(headers) == "t.3affaf040991412bbeb51d73419024bb.849.1786707096636"

    def test_x_sw_traceid_takes_priority_over_generic(self):
        headers = rs.CaseInsensitiveDict({
            "x-sw-traceId": "t.aaa.1.1",
            "x-request-id": "req-999",
        })
        assert extract_infra_trace_id(headers) == "t.aaa.1.1"

    def test_case_insensitive_plain_dict(self):
        assert extract_infra_trace_id({"X-SW-TraceId": "t.bbb.2.2"}) == "t.bbb.2.2"
