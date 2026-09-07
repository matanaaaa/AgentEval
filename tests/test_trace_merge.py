"""多轮 Trace 合并测试。"""

from evaluator.evaluator import AgentEvaluator
from main import _merge_traces_for_eval
from runner.sse_parser import AgentTrace, BusinessResult, SkillTrace


def _trace_with_skills(*skill_keys: str) -> AgentTrace:
    trace = AgentTrace()
    trace.skills = [
        SkillTrace(skill_key=key, skill_name=key, status="complete")
        for key in skill_keys
    ]
    trace.skill = trace.skills[0] if trace.skills else None
    return trace


def _trace_with_fields(*fields: tuple[str, object]) -> AgentTrace:
    trace = AgentTrace()
    trace.business_result = BusinessResult(
        object_apikey="contact",
        extracted_fields=[
            {"apikey": key, "value": value, "label": value}
            for key, value in fields
        ],
    )
    return trace


def test_different_skills_from_t1_t2_are_merged_in_order():
    t1 = _trace_with_skills("crm-query")
    t2 = _trace_with_skills("extract-data")

    merged = _merge_traces_for_eval([t1, t2])

    assert [skill.skill_key for skill in merged.skills] == [
        "crm-query",
        "extract-data",
    ]
    assert merged.skill.skill_key == "crm-query"


def test_duplicate_skill_across_turns_is_only_collected_once():
    t1 = _trace_with_skills("crm-query", "extract-data")
    t2 = _trace_with_skills("extract-data")

    merged = _merge_traces_for_eval([t1, t2])

    assert [skill.skill_key for skill in merged.skills] == [
        "crm-query",
        "extract-data",
    ]


def test_expected_skills_are_evaluated_against_merged_skill_set():
    merged = _merge_traces_for_eval([
        _trace_with_skills("crm-query"),
        _trace_with_skills("extract-data"),
    ])

    result = AgentEvaluator().evaluate(
        merged,
        {"expected_skills": ["crm-query", "extract-data"]},
    )

    skill_layer = result.layers[0]
    assert skill_layer.layer == "L1_Skill路由"
    assert skill_layer.passed is True
    assert "crm-query" in skill_layer.actual
    assert "extract-data" in skill_layer.actual


def test_latest_uncreated_snapshot_is_used_for_fields():
    merged = _merge_traces_for_eval([
        _trace_with_fields(("depart", "技术部"), ("post", "总监"), ("contactRole", "权力支持者")),
        _trace_with_fields(
            ("depart", "技术部"),
            ("post", "总监"),
            ("contactRole", "权力支持者"),
            ("contactName", "陈鑫"),
        ),
        _trace_with_fields(
            ("depart", "技术部"),
            ("post", "vp"),
            ("contactRole", "决策者"),
            ("contactName", "陈鑫"),
        ),
    ])

    actual = {
        item["apikey"]: item["value"]
        for item in merged.business_result.extracted_fields
    }
    assert actual == {
        "depart": "技术部",
        "post": "vp",
        "contactRole": "决策者",
        "contactName": "陈鑫",
    }


def test_field_missing_from_latest_snapshot_is_treated_as_deleted():
    merged = _merge_traces_for_eval([
        _trace_with_fields(("contactName", "陈鑫"), ("mobile", "13800138000")),
        _trace_with_fields(("contactName", "陈鑫")),
    ])

    assert merged.business_result.extracted_fields == [
        {"apikey": "contactName", "value": "陈鑫", "label": "陈鑫"}
    ]


def test_latest_empty_value_overwrites_old_value_for_field_deletion():
    merged = _merge_traces_for_eval([
        _trace_with_fields(("mobile", "13800138000")),
        _trace_with_fields(("mobile", "")),
    ])

    assert merged.business_result.extracted_fields == [
        {"apikey": "mobile", "value": "", "label": ""}
    ]
