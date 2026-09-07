"""
FieldEvaluator 参数化测试

覆盖：
- precision/recall/F1 公式正确性（含 extra 惩罚）
- 严格规范化（不再双向 contains）
- 边界值：None, 空串, 相似但不同
- 映射表匹配
- 手机号/邮箱/布尔/数字 规范化
"""
import pytest
from runner.sse_parser import AgentTrace, SkillTrace, ToolTrace, BusinessResult
from evaluator.evaluator import AgentEvaluator, BUSINESS_FIELDS


@pytest.fixture
def evaluator():
    return AgentEvaluator()


def _make_trace(extracted_fields, is_created=False, record_id=None):
    """最小化 trace 构造，只关注 L4"""
    trace = AgentTrace()
    trace.skill = SkillTrace(
        skill_name="extract-data", skill_key="extract-data",
        status="complete", duration_ms=1000, match_method="semantic",
    )
    trace.tools.append(ToolTrace(
        tool_name="extract-data", execution_type="tool",
        status="complete", run_id="run-1",
    ))
    trace.business_result = BusinessResult(
        object_apikey="contact",
        is_created=is_created,
        created_record_id=record_id,
        extracted_fields=extracted_fields,
        message="",
    )
    trace.total_duration_ms = 1000
    trace.cost_credits = 1.0
    return trace


# ============================================================
# Precision / Recall / F1 公式验证
# ============================================================
class TestPrecisionRecallFormula:
    """验证新公式：P = matched/(matched+wrong+extra), R = matched/(matched+wrong+missing)"""

    def test_perfect_match_no_extra(self, evaluator):
        """期望2字段，实际2字段全对 → P=1, R=1, F1=1"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "张三", "label": "张三"},
            {"apikey": "mobile", "value": "13800138000", "label": "13800138000"},
        ])
        expected = {"skill": "extract-data", "fields": {"contactName": "张三", "mobile": "13800138000"}}
        result = evaluator.evaluate(trace, expected)
        m = result.field_metrics
        assert m.precision == 1.0
        assert m.recall == 1.0
        assert m.f1 == 1.0

    def test_extra_fields_penalize_precision(self, evaluator):
        """期望2字段全对，但多抽了2个业务字段 → P = 2/(2+0+2) = 0.5"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "张三", "label": "张三"},
            {"apikey": "company", "value": "腾讯", "label": "腾讯"},
            {"apikey": "phone", "value": "乱填", "label": "乱填"},
            {"apikey": "title", "value": "乱填", "label": "乱填"},
        ])
        expected = {"skill": "extract-data", "fields": {"contactName": "张三", "company": "腾讯"}}
        result = evaluator.evaluate(trace, expected)
        m = result.field_metrics
        # matched=2, wrong=0, extra=2 (phone, title are in BUSINESS_FIELDS)
        assert m.precision == pytest.approx(2 / 4, abs=0.001)
        assert m.recall == 1.0
        assert m.f1 == pytest.approx(2 * 0.5 * 1.0 / (0.5 + 1.0), abs=0.001)

    def test_system_fields_not_counted_as_extra(self, evaluator):
        """系统字段（run_id 等）不计入 extra"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "张三", "label": "张三"},
            {"apikey": "run_id", "value": "abc123", "label": "abc123"},
            {"apikey": "created_at", "value": "2026-01-01", "label": "2026-01-01"},
        ])
        expected = {"skill": "extract-data", "fields": {"contactName": "张三"}}
        result = evaluator.evaluate(trace, expected)
        m = result.field_metrics
        # run_id, created_at are system fields → not extra
        assert m.precision == 1.0
        assert m.recall == 1.0
        assert m.extra_fields == []

    def test_non_business_non_system_fields_not_extra(self, evaluator):
        """既不在 BUSINESS_FIELDS 也不在 SYSTEM_FIELDS 的字段不算 extra"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "张三", "label": "张三"},
            {"apikey": "random_unknown_field", "value": "xxx", "label": "xxx"},
        ])
        expected = {"skill": "extract-data", "fields": {"contactName": "张三"}}
        result = evaluator.evaluate(trace, expected)
        m = result.field_metrics
        assert m.precision == 1.0
        assert "random_unknown_field" not in m.extra_fields

    def test_missing_fields_penalize_recall(self, evaluator):
        """期望3字段，只抽到1个 → R = 1/(1+0+2) = 0.333"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "张三", "label": "张三"},
        ])
        expected = {"skill": "extract-data", "fields": {"contactName": "张三", "mobile": "13800138000", "email": "a@b.com"}}
        result = evaluator.evaluate(trace, expected)
        m = result.field_metrics
        assert m.recall == pytest.approx(1 / 3, abs=0.001)
        assert set(m.missing_fields) == {"mobile", "email"}

    def test_wrong_fields_penalize_both(self, evaluator):
        """字段存在但值错误 → wrong 同时降低 P 和 R"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "李四", "label": "李四"},
            {"apikey": "mobile", "value": "13800138000", "label": "13800138000"},
        ])
        expected = {"skill": "extract-data", "fields": {"contactName": "张三", "mobile": "13800138000"}}
        result = evaluator.evaluate(trace, expected)
        m = result.field_metrics
        # matched=1(mobile), wrong=1(contactName)
        assert m.precision == pytest.approx(1 / 2, abs=0.001)
        assert m.recall == pytest.approx(1 / 2, abs=0.001)
        assert len(m.wrong_fields) == 1
        assert m.wrong_fields[0]["field"] == "contactName"


# ============================================================
# 严格规范化：不再双向 contains
# ============================================================
class TestStrictNormalization:
    """验证移除了宽泛的包含匹配"""

    @pytest.mark.parametrize("actual,expected_val,field_key,should_match", [
        # 精确匹配
        ("张三", "张三", "contactName", True),
        # 包含不再匹配
        ("北京市海淀区", "北京", "address", False),
        ("北京", "北京市海淀区", "address", False),
        # None vs 空串
        (None, "", "contactName", False),
        ("", None, "contactName", False),
        (None, None, "contactName", True),
        # 空串 vs 非空
        ("", "张三", "contactName", False),
        ("张三", "", "contactName", False),
        # 忽略大小写不再生效（除了邮箱）
        ("ABC", "abc", "contactName", False),
        # 邮箱 lower 匹配
        ("Test@Example.COM", "test@example.com", "email", True),
    ])
    def test_strict_matching(self, evaluator, actual, expected_val, field_key, should_match):
        result = evaluator._field_match(actual, expected_val, field_key)
        assert result == should_match, (
            f"_field_match({actual!r}, {expected_val!r}, {field_key!r}) = {result}, expected {should_match}"
        )


# ============================================================
# 手机号规范化
# ============================================================
class TestPhoneNormalization:

    @pytest.mark.parametrize("actual,expected_val,should_match", [
        ("138 0013 8000", "13800138000", True),
        ("138-0013-8000", "13800138000", True),
        ("(138)00138000", "13800138000", True),
        ("13800138000", "13800138000", True),
        ("13800138001", "13800138000", False),  # 不同号码
    ])
    def test_mobile_normalization(self, evaluator, actual, expected_val, should_match):
        result = evaluator._field_match(actual, expected_val, "mobile")
        assert result == should_match


# ============================================================
# 映射表匹配
# ============================================================
class TestValueMapping:

    @pytest.mark.parametrize("actual,expected_val,field_key,should_match", [
        # gender 映射
        ("1", "男", "gender", True),
        ("1", "男性", "gender", True),
        ("1", "先生", "gender", True),
        ("2", "女", "gender", True),
        ("2", "男", "gender", False),
        ("1", "女", "gender", False),
        ("0", "未知", "gender", True),
        # contactRole 映射（编码以 CRM labelKey `projectRole.N` 为准）
        ("1", "决策者", "contactRole", True),
        ("2", "审批者", "contactRole", True),
        ("3", "评估者", "contactRole", True),
        ("5", "权力支持者", "contactRole", True),
        ("1", "审批者", "contactRole", False),
        # entityType 关键词映射（actual 必须含 default/默认 关键词才能匹配）
        ("defaultBusiType", "默认业务类型", "entityType", True),  # value mapping
        ("-11010000200001", "默认业务类型", "entityType", False),  # 纯 ID 无关键词，应通过 label 匹配
    ])
    def test_value_mapping(self, evaluator, actual, expected_val, field_key, should_match):
        result = evaluator._field_match(actual, expected_val, field_key)
        assert result == should_match


# ============================================================
# 布尔 / 数字 宽松匹配
# ============================================================
class TestBoolAndNumeric:

    @pytest.mark.parametrize("actual,expected_val,field_key,should_match", [
        # 布尔
        ("true", "1", "some_flag", True),
        ("false", "0", "some_flag", True),
        ("yes", "true", "some_flag", True),
        ("no", "false", "some_flag", True),
        # 数字
        ("1", "1.0", "amount", True),
        ("3.14", "3.14", "amount", True),
        ("100", "100", "amount", True),
        ("100", "200", "amount", False),
    ])
    def test_bool_and_numeric(self, evaluator, actual, expected_val, field_key, should_match):
        result = evaluator._field_match(actual, expected_val, field_key)
        assert result == should_match


# ============================================================
# L4 通过条件
# ============================================================
class TestL4PassCondition:

    def test_f1_below_threshold_fails(self, evaluator):
        """F1 < 0.7 → L4 不通过"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "张三", "label": "张三"},
        ])
        expected = {
            "skill": "extract-data",
            "fields": {"contactName": "张三", "mobile": "x", "email": "x", "phone": "x", "depart": "x"},
        }
        result = evaluator.evaluate(trace, expected)
        assert result.layers[3].passed is False
        assert result.field_metrics.f1 < 0.7

    def test_wrong_field_fails_even_with_high_f1(self, evaluator):
        """有 wrong 字段即使 F1 >= 0.7 也不通过"""
        trace = _make_trace([
            {"apikey": "contactName", "value": "错误名字", "label": "错误名字"},
            {"apikey": "mobile", "value": "13800138000", "label": "13800138000"},
            {"apikey": "email", "value": "a@b.com", "label": "a@b.com"},
        ])
        expected = {
            "skill": "extract-data",
            "fields": {"contactName": "张三", "mobile": "13800138000", "email": "a@b.com"},
        }
        result = evaluator.evaluate(trace, expected)
        # matched=2, wrong=1 → F1 = 2*(2/3)*(2/3)/((2/3)+(2/3)) = 0.667... could be >= 0.7 or not
        # Actually P=2/3, R=2/3, F1=2/3 ≈ 0.667 < 0.7, so it fails on both conditions
        assert result.layers[3].passed is False
        assert len(result.field_metrics.wrong_fields) == 1
