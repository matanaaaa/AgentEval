"""用例 Schema 校验测试"""
import json
import os

import pytest

from case_schema import Case, CaseValidationError, validate_cases

REPO_ROOT = os.path.dirname(os.path.dirname(__file__))


def _case(**overrides) -> dict:
    base = {
        "id": "C001",
        "name": "示例",
        "type": "single",
        "turns": [{"turn_id": "T01", "input": "创建联系人陈鑫"}],
        "evaluation": {"skill": "extract-data"},
    }
    base.update(overrides)
    return base


class TestValidCases:
    def test_minimal_case(self):
        parsed = validate_cases([_case()])
        assert parsed[0].id == "C001"
        assert parsed[0].evaluation.skill == "extract-data"

    def test_defaults_are_empty_not_none(self):
        parsed = validate_cases([_case()])
        ev = parsed[0].evaluation
        assert ev.tools_required == []
        assert ev.fields == {}
        assert ev.field_constraints == {}
        assert ev.business_success is True
        assert ev.require_business_grounding is False

    def test_state_assertion_parsed(self):
        raw = _case(
            type="multi_turn",
            turns=[
                {"turn_id": "T01", "input": "a",
                 "expected": {"state_assertion": {"fields": {"depart": "技术部"},
                                                  "absent": ["mobile"]}}},
                {"turn_id": "T02", "input": "b"},
            ],
        )
        parsed = validate_cases([raw])
        assertion = parsed[0].turns[0].expected.state_assertion
        assert assertion.fields == {"depart": "技术部"}
        assert assertion.absent == ["mobile"]

    def test_field_constraints_parsed(self):
        raw = _case(evaluation={"field_constraints": {"mobile": "absent"}})
        parsed = validate_cases([raw])
        assert parsed[0].evaluation.field_constraints == {"mobile": "absent"}

    def test_business_success_pending(self):
        parsed = validate_cases([_case(evaluation={"business_success": "pending"})])
        assert parsed[0].evaluation.business_success == "pending"

    def test_business_grounding_required(self):
        parsed = validate_cases([_case(evaluation={
            "business_success": True,
            "require_business_grounding": True,
        })])
        assert parsed[0].evaluation.require_business_grounding is True

    def test_metadata_allows_extra_keys(self):
        raw = _case(metadata={"scenario": "s", "tags": ["t"], "owner": "qa-team"})
        parsed = validate_cases([raw])
        assert parsed[0].metadata.tags == ["t"]

    def test_legacy_expected_migrated_to_evaluation(self):
        raw = _case()
        raw.pop("evaluation")
        raw["expected"] = {"skill": "extract-data", "fields": {"contactName": "陈鑫"}}
        parsed = validate_cases([raw])
        assert parsed[0].evaluation.fields == {"contactName": "陈鑫"}


class TestRejectedCases:
    def test_unknown_evaluation_key_rejected(self):
        """哑字段必须被拦住，这是引入 schema 的核心目的"""
        raw = _case(evaluation={"expected_business_state": "pending_input"})
        with pytest.raises(CaseValidationError) as exc:
            validate_cases([raw])
        assert "expected_business_state" in str(exc.value)

    def test_unknown_top_level_key_rejected(self):
        with pytest.raises(CaseValidationError):
            validate_cases([_case(typo_field=1)])

    def test_typo_in_text_assertion_rejected(self):
        raw = _case(turns=[{"input": "a", "expected": {"text_assertions": {"al": ["x"]}}}])
        with pytest.raises(CaseValidationError):
            validate_cases([raw])

    def test_missing_id_rejected(self):
        raw = _case()
        raw.pop("id")
        with pytest.raises(CaseValidationError):
            validate_cases([raw])

    def test_empty_turns_rejected(self):
        with pytest.raises(CaseValidationError):
            validate_cases([_case(turns=[])])

    def test_single_type_with_multiple_turns_rejected(self):
        raw = _case(turns=[{"input": "a"}, {"input": "b"}])
        with pytest.raises(CaseValidationError):
            validate_cases([raw])

    def test_invalid_case_type_rejected(self):
        with pytest.raises(CaseValidationError):
            validate_cases([_case(type="triple")])

    def test_invalid_field_constraint_value_rejected(self):
        raw = _case(evaluation={"field_constraints": {"mobile": "maybe"}})
        with pytest.raises(CaseValidationError):
            validate_cases([raw])

    @pytest.mark.parametrize("business_success", [False, "pending"])
    def test_grounding_requires_success_final_state(self, business_success):
        raw = _case(evaluation={
            "business_success": business_success,
            "require_business_grounding": True,
        })
        with pytest.raises(CaseValidationError) as exc:
            validate_cases([raw])
        assert "require_business_grounding" in str(exc.value)

    def test_duplicate_ids_rejected(self):
        with pytest.raises(CaseValidationError) as exc:
            validate_cases([_case(), _case()])
        assert "重复" in str(exc.value)

    def test_all_errors_reported_at_once(self):
        """一次报出所有问题，避免改一个跑一次"""
        bad = [_case(id="A", type="triple"), _case(id="B", turns=[])]
        with pytest.raises(CaseValidationError) as exc:
            validate_cases(bad)
        assert len(exc.value.errors) >= 2


class TestRepoCasesAreValid:
    """仓库里现有的用例文件必须能通过校验"""

    @pytest.mark.parametrize("rel_path", [
        "cases/contact_create.json",
        "fixtures/replay/cases.json",
    ])
    def test_shipped_cases_validate(self, rel_path):
        with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
            raw = json.load(f)
        parsed = validate_cases(raw)
        assert len(parsed) == len(raw)
