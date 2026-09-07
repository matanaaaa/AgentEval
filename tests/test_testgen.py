"""AI 用例生成模块的离线回归测试。"""

from copy import deepcopy

import pytest
import requests

import config
from testgen.gate import DeterministicGate
from testgen.generator import generate_cases
from testgen.llm import LLMClient, LLMError
from testgen.pipeline import run_pipeline
from testgen.planner import build_scenario_plan


class FakeLLM:
    """按顺序返回预设 JSON，记录调用但不访问真实模型。"""

    def __init__(self, *responses: dict):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, system: str, user: str) -> dict:
        self.calls.append({"system": system, "user": user})
        if not self.responses:
            raise AssertionError("FakeLLM 没有剩余响应")
        return deepcopy(self.responses.pop(0))


def _plan(*scenario_ids: str) -> dict:
    return {
        "requirement": "创建客户前需要确认",
        "features": ["字段抽取", "确认创建"],
        "risks": ["未确认就创建"],
        "dimensions": ["确认流程"],
        "object_type": "account",
        "scenarios": [
            {
                "id": sid,
                "name": f"场景{sid}",
                "feature": "确认创建",
                "risk": "未确认就创建",
                "dimension": "确认流程",
                "priority": "P0",
                "intent": f"验证{sid}",
            }
            for sid in scenario_ids
        ],
    }


def _case(scenario: str = "S1", **evaluation_overrides) -> dict:
    evaluation = {
        "skill": "extract-data",
        "tools_required": ["save_record"],
        "object_type": "account",
        "business_success": True,
        "fields": {"accountName": "示例客户"},
    }
    evaluation.update(evaluation_overrides)
    return {
        "id": f"GEN_ACCOUNT_{scenario}",
        "name": "创建客户",
        "type": "single",
        "metadata": {
            "scenario": scenario,
            "priority": "P0",
            "difficulty": "easy",
            "tags": ["generated"],
        },
        "turns": [
            {
                "turn_id": "T01",
                "input": "创建客户示例客户",
                "expected": {
                    "state_assertion": {
                        "fields": {"accountName": "示例客户"}
                    }
                },
            }
        ],
        "evaluation": evaluation,
    }


def test_planner_accepts_fake_llm_output_and_adds_object_type():
    fake = FakeLLM(_plan("S1"))

    plan = build_scenario_plan(
        "  创建客户前需要确认  ",
        object_type="account",
        max_scenarios=3,
        llm=fake,
    )

    assert plan["object_type"] == "account"
    assert plan["scenarios"][0]["id"] == "S1"
    assert len(fake.calls) == 1
    assert "创建客户前需要确认" in fake.calls[0]["user"]
    assert "3 个以内" in fake.calls[0]["user"]


def test_planner_never_returns_more_than_three_scenarios():
    fake = FakeLLM(_plan("S1", "S2", "S3", "S4"))

    plan = build_scenario_plan("创建客户前需要确认", llm=fake)

    assert [scenario["id"] for scenario in plan["scenarios"]] == ["S1", "S2", "S3"]
    assert "3 个以内" in fake.calls[0]["user"]


def test_planner_rejects_requested_limit_above_hard_cap():
    with pytest.raises(ValueError, match="max_scenarios 必须在 1-3 之间"):
        build_scenario_plan("创建客户", max_scenarios=4, llm=FakeLLM(_plan("S1")))


def test_generator_returns_cases_from_fake_llm():
    generated = _case("S1")
    fake = FakeLLM({"cases": [generated]})

    cases = generate_cases(_plan("S1"), id_prefix="GEN_ACCOUNT_", llm=fake)

    assert cases == [generated]
    assert len(fake.calls) == 1
    assert "GEN_ACCOUNT_" in fake.calls[0]["user"]
    assert '"id": "S1"' in fake.calls[0]["user"]


def test_pipeline_runs_end_to_end_with_observable_stages(capsys):
    fake = FakeLLM(_plan("S1"), {"cases": [_case("S1")]})

    output = run_pipeline(
        "创建客户前需要确认",
        object_type="account",
        llm=fake,
        dry_run=True,
    )

    assert len(fake.calls) == 2
    assert output.gate is not None and output.gate.passed is True
    assert output.output_path == ""

    stdout = capsys.readouterr().out
    stage_markers = [
        "[1/8] Requirement",
        "[2/8] Planner",
        "[3/8] Scenario Plan",
        "[4/8] Generator",
        "[5/8] Schema Gate  PASS",
        "[6/8] Coverage Gate  PASS",
        "[7/8] Domain Gate  PASS",
        "[8/8] Generated Cases",
    ]
    positions = [stdout.index(marker) for marker in stage_markers]
    assert positions == sorted(positions)
    assert '"id": "GEN_ACCOUNT_S1"' in stdout
    assert "dry-run 模式未写入文件" in stdout


def test_schema_gate_rejects_bad_case():
    bad_case = _case("S1")
    bad_case["unknown_top_level_field"] = True

    result = DeterministicGate().check([bad_case], object_type="account")

    assert result.passed is False
    assert any(issue.gate == "Schema" for issue in result.issues)


def test_coverage_gate_rejects_missing_scenario():
    result = DeterministicGate().check(
        [_case("S1")],
        plan=_plan("S1", "S2"),
        object_type="account",
    )

    assert result.passed is False
    assert any(
        issue.gate == "Coverage" and "S2" in issue.message
        for issue in result.issues
    )


@pytest.mark.parametrize("field", ["unknownField", "contactName"])
def test_domain_gate_rejects_illegal_or_cross_entity_field(field):
    result = DeterministicGate().check(
        [_case("S1", fields={field: "非法值"})],
        object_type="account",
    )

    assert result.passed is False
    assert any(
        issue.gate == "Domain" and field in issue.message
        for issue in result.issues
    )


def test_llm_client_rejects_invalid_json_without_network(monkeypatch):
    client = LLMClient(api_key="fake-key")
    monkeypatch.setattr(client, "_chat", lambda system, user: "这不是 JSON")

    with pytest.raises(LLMError, match="无法从 LLM 回复中解析 JSON"):
        client.chat_json("system", "user")


def test_llm_client_retries_after_first_request_failure(monkeypatch, capsys):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    attempts = []

    def fake_post(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) == 1:
            raise requests.ConnectionError("temporary failure")
        return FakeResponse()

    monkeypatch.setattr(config, "LLM_MAX_RETRIES", 1)
    monkeypatch.setattr(config, "LLM_CONNECT_TIMEOUT", 30)
    monkeypatch.setattr(config, "LLM_TIMEOUT", 240)
    monkeypatch.setattr("testgen.llm.requests.post", fake_post)
    monkeypatch.setattr("testgen.llm.time.sleep", lambda _: None)

    result = LLMClient(api_key="fake-key").chat_json("system", "user")

    assert result == {"ok": True}
    assert len(attempts) == 2
    assert all(kwargs["timeout"] == (30, 240) for _, kwargs in attempts)
    assert "read timeout=240s" in capsys.readouterr().out
