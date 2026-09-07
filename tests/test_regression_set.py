"""真实 BadCase 沉淀为 Replay Regression Set 的测试。"""

import json

import pytest

from regression_set.__main__ import _mark_fixed, _run_regressions
from regression_set.promoter import PromotionError, promote_badcase, verify_regression_set


CASE_ID = "BAD_ACCOUNT_001"


def _case() -> dict:
    return {
        "id": CASE_ID,
        "name": "真实失败客户录入",
        "type": "single",
        "metadata": {"scenario": "live_badcase", "tags": ["live"]},
        "turns": [{
            "turn_id": "T01",
            "input": "创建客户真实公司，邮箱 real.user@example.com，电话 13800138000",
        }],
        "evaluation": {
            "skill": "extract-data",
            "tools_required": ["save_record"],
            "object_type": "account",
            "business_success": True,
            "fields": {
                "accountName": "真实公司",
                "email": "real.user@example.com",
                "phone": "13800138000",
            },
        },
    }


def _prepare(tmp_path, *, passed: bool = False):
    cases_dir = tmp_path / "cases"
    recorded_root = tmp_path / "recorded"
    recording_dir = recorded_root / CASE_ID / "run-1"
    cases_dir.mkdir()
    recording_dir.mkdir(parents=True)
    (cases_dir / "account.json").write_text(
        json.dumps([_case()], ensure_ascii=False), encoding="utf-8"
    )
    report = {
        "meta": {"tag": "run-1"},
        "results": [{"case_id": CASE_ID, "overall_pass": passed}],
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    sse_lines = [
        "event: RUN_STARTED",
        "data: " + json.dumps({
            "type": "RUN_STARTED",
            "run_id": "run-secret-123",
            "thread_id": "thread-secret-456",
            "current_message_id": 987654,
        }),
        "",
        "event: TEXT_MESSAGE_CONTENT",
        "data: " + json.dumps({
            "type": "TEXT_MESSAGE_CONTENT",
            "message_id": "message-secret-1",
            "delta": "真实公司 real.user@example.com 13800138000",
        }, ensure_ascii=False),
        "",
        "event: RUN_FINISHED",
        "data: " + json.dumps({"type": "RUN_FINISHED", "run_id": "run-secret-123"}),
    ]
    (recording_dir / "T01.sse").write_text("\n".join(sse_lines) + "\n", encoding="utf-8")
    return report_path, cases_dir, recorded_root


def _promote(tmp_path, *, issue_type="agent", confirmed=True, passed=False):
    report, cases_dir, recorded_root = _prepare(tmp_path, passed=passed)
    return promote_badcase(
        report_path=report,
        case_id=CASE_ID,
        root_cause="Agent 未正确执行保存",
        issue_type=issue_type,
        confirmed=confirmed,
        recorded_root=recorded_root,
        cases_dir=cases_dir,
        target_root=tmp_path / "regression",
    )


def test_promotion_requires_human_confirmation(tmp_path):
    with pytest.raises(PromotionError, match="人工确认"):
        _promote(tmp_path, confirmed=False)


def test_promotion_rejects_case_that_is_not_failed(tmp_path):
    with pytest.raises(PromotionError, match="不是 FAIL"):
        _promote(tmp_path, passed=True)


def test_promotion_sanitizes_case_and_trace_and_builds_manifest(tmp_path):
    result = _promote(tmp_path)
    root = tmp_path / "regression"
    combined = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.*"))

    assert result.trace_count == 1
    assert result.expected_exit_code == 1
    assert "真实公司" not in combined
    assert "real.user@example.com" not in combined
    assert "13800138000" not in combined
    assert "run-secret-123" not in combined
    assert verify_regression_set(root) == 1

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert entry["issue_type"] == "agent"
    assert entry["status"] == "known"
    assert entry["blocking"] is False
    assert entry["live_regression_required"] is True
    assert entry["sanitization"]["residual_scan_passed"] is True
    assert entry["root_cause_confirmed"] is True


def test_evaluator_badcase_is_known_and_non_blocking_until_fixed(tmp_path):
    result = _promote(tmp_path, issue_type="evaluator")

    assert result.expected_exit_code == 1
    manifest = json.loads(
        (tmp_path / "regression" / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["entries"][0]
    assert entry["status"] == "known"
    assert entry["blocking"] is False


def test_duplicate_promotion_is_rejected(tmp_path):
    _promote(tmp_path)
    report = tmp_path / "report.json"

    with pytest.raises(PromotionError, match="已存在用例"):
        promote_badcase(
            report_path=report,
            case_id=CASE_ID,
            root_cause="重复沉淀",
            issue_type="agent",
            confirmed=True,
            recorded_root=tmp_path / "recorded",
            cases_dir=tmp_path / "cases",
            target_root=tmp_path / "regression",
        )


def test_verify_rejects_residual_sensitive_data(tmp_path):
    _promote(tmp_path)
    trace = tmp_path / "regression" / CASE_ID / "T01.sse"
    trace.write_text(trace.read_text(encoding="utf-8") + "data: leak@example.com\n", encoding="utf-8")

    with pytest.raises(PromotionError, match="敏感信息"):
        verify_regression_set(tmp_path / "regression")


def test_known_failure_is_run_but_does_not_block(tmp_path, monkeypatch, capsys):
    _promote(tmp_path)
    root = tmp_path / "regression"
    monkeypatch.setattr("regression_set.__main__._execute_case", lambda *_: 1)

    assert _run_regressions(str(root)) == 0
    assert "KNOWN FAIL" in capsys.readouterr().out


def test_known_case_recovery_prompts_for_upgrade_without_blocking(tmp_path, monkeypatch, capsys):
    _promote(tmp_path)
    root = tmp_path / "regression"
    monkeypatch.setattr("regression_set.__main__._execute_case", lambda *_: 0)

    assert _run_regressions(str(root)) == 0
    assert "RECOVERED" in capsys.readouterr().out


def test_known_case_infrastructure_error_still_blocks(tmp_path, monkeypatch):
    _promote(tmp_path)
    root = tmp_path / "regression"
    monkeypatch.setattr("regression_set.__main__._execute_case", lambda *_: 3)

    assert _run_regressions(str(root)) == 1


def test_fixed_case_failure_blocks_regression_run(tmp_path, monkeypatch):
    _promote(tmp_path)
    root = tmp_path / "regression"
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0].update({
        "status": "fixed",
        "blocking": True,
        "expected_exit_code": 0,
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("regression_set.__main__._execute_case", lambda *_: 1)

    assert _run_regressions(str(root)) == 1


def test_mark_fixed_upgrades_only_after_passing_replay(tmp_path, monkeypatch):
    _promote(tmp_path)
    root = tmp_path / "regression"
    monkeypatch.setattr("regression_set.__main__._execute_case", lambda *_: 0)

    assert _mark_fixed(str(root), CASE_ID) == 0
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["entries"][0]
    assert entry["status"] == "fixed"
    assert entry["blocking"] is True
    assert entry["expected_exit_code"] == 0
    assert entry["fixed_at"]


def test_mark_fixed_rejects_still_failing_case(tmp_path, monkeypatch):
    _promote(tmp_path)
    root = tmp_path / "regression"
    monkeypatch.setattr("regression_set.__main__._execute_case", lambda *_: 1)

    assert _mark_fixed(str(root), CASE_ID) == 1
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["entries"][0]["status"] == "known"
    assert manifest["entries"][0]["blocking"] is False
