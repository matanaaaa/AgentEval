"""FailureAnalyzer 在新五层评测体系下的分类测试。"""

import pytest

from evaluator.evaluator import EvalResult, FieldMetrics, LayerResult
from evaluator.failure_analyzer import FailureAnalyzer


@pytest.fixture
def analyzer():
    return FailureAnalyzer()


def _make_result(layers: list, field_metrics=None, case_id="TEST") -> EvalResult:
    return EvalResult(
        case_id=case_id,
        layers=layers,
        field_metrics=field_metrics,
        overall_pass=all(layer.passed for layer in layers),
    )


def _passing_layers():
    return [
        LayerResult(layer="L1_Skill路由", passed=True),
        LayerResult(layer="L2_Tool调用", passed=True),
        LayerResult(layer="L3_字段抽取", passed=True),
        LayerResult(layer="L4_业务结果", passed=True),
        LayerResult(layer="L5_轮次状态", passed=True),
    ]


def _result_with_failure(index: int, **layer_kwargs):
    layers = _passing_layers()
    layer = layers[index]
    layers[index] = LayerResult(layer=layer.layer, passed=False, **layer_kwargs)
    return _make_result(layers)


class TestLayerClassification:
    def test_skill_routing_error(self, analyzer):
        result = _result_with_failure(
            0, expected="['extract-data']", actual="['crm-query']",
            detail="缺失 Skill: ['extract-data']",
        )
        report = analyzer.analyze(result)
        assert report.primary_cause == "SKILL_ROUTING_ERROR"
        assert report.root_causes[0].severity == "critical"

    def test_tool_call_error(self, analyzer):
        result = _result_with_failure(
            1, expected="必须: ['save_record']", actual="['extract-data']",
            detail="缺失: ['save_record']",
        )
        report = analyzer.analyze(result)
        assert report.primary_cause == "TOOL_CALL_ERROR"
        assert report.root_causes[0].severity == "major"

    def test_state_assertion_error(self, analyzer):
        result = _result_with_failure(
            4, actual="通过 1/2", detail="C01T04: mobile 应已删除",
        )
        assert analyzer.analyze(result).primary_cause == "STATE_ASSERTION_ERROR"

    def test_text_assertion_error(self, analyzer):
        result = _make_result([
            LayerResult(
                layer="L5_文本响应",
                passed=False,
                expected="1 组文本断言",
                actual="通过 0/1",
                detail="T01: 缺少必须包含的文本: ['确认']",
            )
        ])

        report = analyzer.analyze(result)

        assert report.primary_cause == "TEXT_ASSERTION_ERROR"


class TestFieldErrors:
    def test_missing_fields(self, analyzer):
        metrics = FieldMetrics(
            precision=1.0,
            recall=0.5,
            f1=0.667,
            matched_fields=["contactName"],
            missing_fields=["mobile", "email"],
        )
        layers = _passing_layers()
        layers[2] = LayerResult(layer="L3_字段抽取", passed=False)
        report = analyzer.analyze(_make_result(layers, metrics))
        assert any(c.category == "FIELD_MISSING_ERROR" for c in report.root_causes)

    def test_wrong_fields(self, analyzer):
        metrics = FieldMetrics(
            precision=0.5,
            recall=0.5,
            f1=0.5,
            matched_fields=["mobile"],
            wrong_fields=[
                {"field": "contactName", "expected": "张三", "actual": "李四"}
            ],
        )
        layers = _passing_layers()
        layers[2] = LayerResult(layer="L3_字段抽取", passed=False)
        report = analyzer.analyze(_make_result(layers, metrics))
        assert any(c.category == "FIELD_VALUE_ERROR" for c in report.root_causes)

    def test_missing_and_wrong_are_both_reported(self, analyzer):
        metrics = FieldMetrics(
            precision=0.33,
            recall=0.33,
            f1=0.33,
            missing_fields=["email"],
            wrong_fields=[
                {"field": "contactName", "expected": "张三", "actual": "李四"}
            ],
        )
        layers = _passing_layers()
        layers[2] = LayerResult(layer="L3_字段抽取", passed=False)
        categories = {
            cause.category
            for cause in analyzer.analyze(_make_result(layers, metrics)).root_causes
        }
        assert {"FIELD_MISSING_ERROR", "FIELD_VALUE_ERROR"} <= categories


class TestBusinessErrors:
    @pytest.mark.parametrize(
        "detail,category",
        [
            ("message=信息不全", "BUSINESS_NOT_CREATED"),
            ("DB Check: 记录不存在！Agent 声称成功但数据库无记录", "DB_RECORD_MISSING"),
            ("DB Check: 字段不一致 mismatched=['gender']", "DB_FIELD_MISMATCH"),
        ],
    )
    def test_business_classification(self, analyzer, detail, category):
        result = _result_with_failure(
            3, expected="success=True", actual="isCreated=False", detail=detail,
        )
        assert analyzer.analyze(result).primary_cause == category


class TestReportBehavior:
    def test_pass_has_no_failures(self, analyzer):
        report = analyzer.analyze(_make_result(_passing_layers()))
        assert report.has_failures is False
        assert report.root_causes == []
        assert report.primary_cause == ""

    def test_critical_cause_wins_over_major(self, analyzer):
        layers = _passing_layers()
        layers[1] = LayerResult(layer="L2_Tool调用", passed=False, detail="缺失")
        layers[3] = LayerResult(layer="L4_业务结果", passed=False, detail="message=失败")
        report = analyzer.analyze(_make_result(layers))
        assert report.primary_cause == "BUSINESS_NOT_CREATED"

    def test_batch_stats(self, analyzer):
        results = [
            _make_result(_passing_layers(), case_id="CASE001"),
            _result_with_failure(0, expected="a", actual="b"),
            _result_with_failure(0, expected="x", actual="y"),
        ]
        batch = analyzer.analyze_batch(results)
        assert batch["category_stats"]["SKILL_ROUTING_ERROR"] == 2
        assert batch["primary_cause_stats"]["SKILL_ROUTING_ERROR"] == 2
        assert len(batch["reports"]) == 3

    def test_format_report(self, analyzer):
        result = _result_with_failure(
            1, expected="['save']", actual="[]", detail="缺失: ['save']",
        )
        result.case_id = "FMT001"
        formatted = analyzer.format_report(analyzer.analyze(result))
        assert "FMT001" in formatted
        assert "TOOL_CALL_ERROR" in formatted or "Tool 调用错误" in formatted
