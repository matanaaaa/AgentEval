"""测试 Evaluator：验证五层评测逻辑"""
import pytest
from runner.sse_parser import AgentTrace, SkillTrace, ToolTrace, BusinessResult
from evaluator.evaluator import AgentEvaluator


@pytest.fixture
def evaluator():
    return AgentEvaluator()


def _make_trace(
    skill_key="extract-data",
    tool_names=None,
    is_created=True,
    record_id=123456,
    object_apikey="contact",
    extracted_fields=None,
):
    """构造测试用 AgentTrace"""
    trace = AgentTrace()

    if skill_key:
        trace.skill = SkillTrace(
            skill_name=skill_key,
            skill_key=skill_key,
            status="complete",
            duration_ms=10000,
            match_method="semantic",
        )

    if tool_names is None:
        tool_names = ["extract-data", "save_record"]
    for name in tool_names:
        trace.tools.append(ToolTrace(
            tool_name=name,
            execution_type="tool",
            status="complete",
            run_id=f"run-{name}",
        ))

    if extracted_fields is None:
        extracted_fields = [
            {"apikey": "contactName", "label": "张三", "value": "张三"},
            {"apikey": "gender", "label": "男", "value": "1"},
            {"apikey": "mobile", "label": "13800138000", "value": "13800138000"},
        ]

    trace.business_result = BusinessResult(
        object_apikey=object_apikey,
        is_created=is_created,
        created_record_id=record_id,
        extracted_fields=extracted_fields,
        message="创建成功" if is_created else "",
    )

    trace.total_duration_ms = 10000
    trace.cost_credits = 17.0
    return trace


class TestLayerComposition:
    def test_exactly_four_case_level_layers(self, evaluator):
        """
        用例级只有四层。第五层「轮次状态」是附加层，由 main 按 state_assertion 追加。
        用例级评测不会为同一个 skill_key 重复计分。
        """
        result = evaluator.evaluate(_make_trace(), {"skill": "extract-data"})
        assert [lr.layer for lr in result.layers] == [
            "L1_Skill路由", "L2_Tool调用", "L3_字段抽取", "L4_业务结果",
        ]

    def test_no_intent_layer(self, evaluator):
        result = evaluator.evaluate(_make_trace(), {"skill": "extract-data"})
        assert not any("意图" in lr.layer for lr in result.layers)


class TestL1Skill:
    def test_skill_match(self, evaluator, layer_of):
        trace = _make_trace(skill_key="extract-data")
        result = evaluator.evaluate(trace, {"skill": "extract-data"})
        assert layer_of(result, "L1").passed is True

    def test_skill_mismatch(self, evaluator, layer_of):
        trace = _make_trace(skill_key="crm-query")
        result = evaluator.evaluate(trace, {"skill": "extract-data"})
        assert layer_of(result, "L1").passed is False

    def test_no_skill_fails(self, evaluator, layer_of):
        trace = _make_trace(skill_key=None)
        result = evaluator.evaluate(trace, {"skill": "extract-data"})
        layer = layer_of(result, "L1")
        assert layer.passed is False
        assert "未检测到" in layer.detail

    def test_no_skill_assertion_skips(self, evaluator, layer_of):
        trace = _make_trace(skill_key="whatever")
        result = evaluator.evaluate(trace, {})
        assert layer_of(result, "L1").passed is True

    def test_expected_skill_found_among_multiple(self, evaluator, layer_of):
        """意图切换用例会经过多个 Skill，期望的那个出现过即算通过"""
        trace = _make_trace(skill_key="crm-query")
        trace.skills = [
            SkillTrace(skill_key="crm-query", status="complete"),
            SkillTrace(skill_key="extract-data", status="complete"),
        ]
        result = evaluator.evaluate(trace, {"skill": "extract-data"})
        assert layer_of(result, "L1").passed is True

    def test_expected_skills_all_present(self, evaluator, layer_of):
        trace = _make_trace(skill_key="crm-query")
        trace.skills = [
            SkillTrace(skill_key="crm-query", status="complete"),
            SkillTrace(skill_key="extract-data", status="complete"),
        ]
        result = evaluator.evaluate(
            trace, {"expected_skills": ["crm-query", "extract-data"]}
        )
        assert layer_of(result, "L1").passed is True

    def test_expected_skills_partially_missing(self, evaluator, layer_of):
        trace = _make_trace(skill_key="extract-data")
        trace.skills = [SkillTrace(skill_key="extract-data", status="complete")]
        result = evaluator.evaluate(
            trace, {"expected_skills": ["crm-query", "extract-data"]}
        )
        layer = layer_of(result, "L1")
        assert layer.passed is False
        assert "crm-query" in layer.detail

    def test_skill_and_expected_skills_are_unioned(self, evaluator, layer_of):
        trace = _make_trace(skill_key="extract-data")
        trace.skills = [SkillTrace(skill_key="extract-data", status="complete")]
        result = evaluator.evaluate(
            trace, {"skill": "extract-data", "expected_skills": ["crm-query"]}
        )
        assert layer_of(result, "L1").passed is False


class TestL2Tools:
    def test_required_tools_present(self, evaluator):
        trace = _make_trace(tool_names=["extract-data", "resolve_entity", "save_record"])
        expected = {"tools_required": ["extract-data"]}
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True

    def test_required_tool_missing(self, evaluator):
        trace = _make_trace(tool_names=["resolve_entity", "validate_extract"])
        expected = {"tools_required": ["extract-data", "save_record"]}
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is False

    def test_extra_tools_ok(self, evaluator):
        """有额外的中间 tool 不影响判断"""
        trace = _make_trace(tool_names=["extract-data", "resolve_entity", "get_field_schema", "save_record"])
        expected = {"tools_required": ["extract-data", "save_record"]}
        result = evaluator.evaluate(trace, expected)
        assert result.layers[1].passed is True


class TestL3Fields:
    def test_all_fields_match(self, evaluator):
        trace = _make_trace(extracted_fields=[
            {"apikey": "contactName", "label": "张三", "value": "张三"},
            {"apikey": "mobile", "label": "13800138000", "value": "13800138000"},
        ])
        expected = {
            "skill": "extract-data",
            "fields": {"contactName": "张三", "mobile": "13800138000"},
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[2].passed is True
        assert result.field_metrics.f1 == 1.0

    def test_missing_field(self, evaluator):
        trace = _make_trace(extracted_fields=[
            {"apikey": "contactName", "label": "张三", "value": "张三"},
        ])
        expected = {
            "skill": "extract-data",
            "fields": {"contactName": "张三", "mobile": "13800138000"},
        }
        result = evaluator.evaluate(trace, expected)
        assert result.field_metrics.recall < 1.0
        assert "mobile" in result.field_metrics.missing_fields

    def test_gender_mapping(self, evaluator):
        """gender=1 应该匹配 '男'"""
        trace = _make_trace(extracted_fields=[
            {"apikey": "gender", "label": "男", "value": "1"},
        ])
        expected = {
            "skill": "extract-data",
            "fields": {"gender": "男"},
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[2].passed is True

    def test_label_fallback_match(self, evaluator):
        """value 不匹配时应该尝试匹配 label"""
        trace = _make_trace(extracted_fields=[
            {"apikey": "entityType", "label": "默认业务类型", "value": "-11010000200001"},
        ])
        expected = {
            "skill": "extract-data",
            "fields": {"entityType": "默认业务类型"},
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[2].passed is True

    def test_no_fields_expected_skips(self, evaluator):
        """没有字段断言时 L4 自动通过"""
        trace = _make_trace()
        expected = {"skill": "extract-data"}
        result = evaluator.evaluate(trace, expected)
        assert result.layers[2].passed is True
        assert result.field_metrics is None


class TestL4Business:
    def test_created_success_without_required_grounding_is_backward_compatible(self, evaluator):
        trace = _make_trace(is_created=True, record_id=123456)
        expected = {"skill": "extract-data", "business_success": True, "object_type": "contact"}
        evaluator._db_check = lambda *args, **kwargs: None
        result = evaluator.evaluate(trace, expected)
        assert result.layers[3].passed is True

    def test_required_grounding_passes_only_with_verified_record(self, evaluator, layer_of):
        trace = _make_trace(is_created=True, record_id=123456)
        evaluator._db_check = lambda *args, **kwargs: {
            "exists": True,
            "fields_correct": True,
            "record": {"id": 123456},
            "mismatched": [],
            "missing_in_record": [],
        }
        result = evaluator.evaluate(trace, {
            "business_success": True,
            "object_type": "contact",
            "require_business_grounding": True,
        })
        layer = layer_of(result, "L4")
        assert layer.passed is True
        assert "DB Check: 验证通过" in layer.detail
        assert "跳过" not in layer.detail
        saved_layer = next(
            item for item in result.summary()["layer_details"]
            if item["layer"] == "L4_业务结果"
        )
        assert saved_layer["passed"] is True
        assert "DB Check: 验证通过" in saved_layer["detail"]

    def test_required_grounding_fails_when_check_is_unavailable(self, evaluator, layer_of):
        trace = _make_trace(is_created=True, record_id=123456)
        evaluator._db_check = lambda *args, **kwargs: None
        result = evaluator.evaluate(trace, {
            "business_success": True,
            "require_business_grounding": True,
        })
        layer = layer_of(result, "L4")
        assert layer.passed is False
        assert "要求 Business Grounding" in layer.detail

    @pytest.mark.parametrize("db_result,detail_fragment", [
        ({
            "exists": False,
            "fields_correct": False,
            "record": None,
            "query_error": "connection timeout",
        }, "查询失败"),
        ({"exists": False, "fields_correct": False, "record": None}, "记录不存在"),
        ({
            "exists": True,
            "fields_correct": False,
            "record": {"id": 123456},
            "mismatched": [{"field": "post"}],
            "missing_in_record": [],
        }, "字段不一致"),
    ])
    def test_required_grounding_fails_on_invalid_db_result(
        self, evaluator, layer_of, db_result, detail_fragment
    ):
        trace = _make_trace(is_created=True, record_id=123456)
        evaluator._db_check = lambda *args, **kwargs: db_result
        result = evaluator.evaluate(trace, {
            "business_success": True,
            "require_business_grounding": True,
        })
        layer = layer_of(result, "L4")
        assert layer.passed is False
        assert detail_fragment in layer.detail

    def test_not_created_when_expected(self, evaluator):
        trace = _make_trace(is_created=False, record_id=None)
        expected = {"skill": "extract-data", "business_success": True}
        result = evaluator.evaluate(trace, expected)
        assert result.layers[3].passed is False

    def test_wrong_object_type(self, evaluator):
        trace = _make_trace(object_apikey="customer")
        expected = {"skill": "extract-data", "business_success": True, "object_type": "contact"}
        result = evaluator.evaluate(trace, expected)
        assert result.layers[3].passed is False

    def test_pending_state(self, evaluator):
        """pending 状态跳过 L4"""
        trace = _make_trace(is_created=False, record_id=None)
        expected = {"skill": "extract-data", "business_success": "pending"}
        result = evaluator.evaluate(trace, expected)
        assert result.layers[3].passed is True


class TestOverall:
    def test_all_pass(self, evaluator):
        trace = _make_trace()
        expected = {
            "skill": "extract-data",
            "tools_required": ["extract-data"],
            "object_type": "contact",
            "business_success": True,
            "fields": {"contactName": "张三", "gender": "男", "mobile": "13800138000"},
        }
        # Mock DB check (unit test 不连真实 CRM)
        evaluator._db_check = lambda *args, **kwargs: None
        result = evaluator.evaluate(trace, expected, case_id="TEST001")
        assert result.overall_pass is True
        assert result.case_id == "TEST001"

    def test_any_fail_means_overall_fail(self, evaluator):
        trace = _make_trace(skill_key="wrong-skill")
        expected = {"skill": "extract-data", "business_success": True}
        result = evaluator.evaluate(trace, expected)
        assert result.overall_pass is False
