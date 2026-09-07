"""把确认后的真实失败用例脱敏沉淀为可回放 Regression Set。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from case_schema import CaseValidationError, validate_cases
from runner.sse_parser import SSEParser


CASES_FILENAME = "cases.json"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION = 1

ISSUE_TYPES = {"agent", "evaluator", "oracle"}
LIFECYCLE_STATUSES = {"known", "fixed"}
DEFAULT_EXPECTED_EXIT = {"known": 1, "fixed": 0}

PII_FIELDS = {
    "accountname", "contactname", "mobile", "phone", "email", "address",
    "company", "username", "password", "cookie", "authorization",
}
IDENTIFIER_FIELDS = {
    "run_id", "thread_id", "conversation_id", "trace_id", "task_id",
    "record_id", "created_record_id", "message_id", "current_message_id",
}
SECRET_FIELDS = {
    "token", "access_token", "refresh_token", "api_key", "llm_api_key",
}

EMAIL_PATTERN = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
BEARER_PATTERN = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{8,}")
API_KEY_PATTERN = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b")


class PromotionError(RuntimeError):
    """BadCase 无法安全沉淀。"""


@dataclass
class Sanitizer:
    """在 Case 与 SSE 之间保持稳定占位符的确定性脱敏器。"""

    manual: dict[str, str] = field(default_factory=dict)
    replacements: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        for original, replacement in self.manual.items():
            if not isinstance(original, str) or not original:
                raise PromotionError("脱敏映射的原值必须是非空字符串")
            if not isinstance(replacement, str) or not replacement:
                raise PromotionError(f"脱敏映射 {original!r} 的替换值必须是非空字符串")
            self.replacements[original] = replacement

    def register(self, value: Any, label: str) -> Any:
        if value is None or isinstance(value, bool):
            return value
        original = str(value)
        if not original:
            return value
        if original in self.replacements:
            return self.replacements[original]
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper() or "VALUE"
        self.counters[normalized] = self.counters.get(normalized, 0) + 1
        replacement = f"<{normalized}_{self.counters[normalized]}>"
        self.replacements[original] = replacement
        return replacement

    def collect(self, value: Any):
        """先收集结构化敏感值，使自由文本中的同值也能同步替换。"""
        if isinstance(value, dict):
            apikey = str(value.get("apikey", "")).lower()
            for key, item in value.items():
                normalized = str(key).lower()
                if normalized in PII_FIELDS | IDENTIFIER_FIELDS | SECRET_FIELDS:
                    self.register(item, normalized)
                elif apikey in PII_FIELDS and normalized in {"value", "label"}:
                    self.register(item, apikey)
                self.collect(item)
        elif isinstance(value, list):
            for item in value:
                self.collect(item)

    def text(self, value: str) -> str:
        result = value
        for original in sorted(self.replacements, key=len, reverse=True):
            result = result.replace(original, self.replacements[original])
        result = EMAIL_PATTERN.sub(lambda m: str(self.register(m.group(0), "email")), result)
        result = MOBILE_PATTERN.sub(lambda m: str(self.register(m.group(0), "mobile")), result)
        result = BEARER_PATTERN.sub(lambda m: str(self.register(m.group(0), "token")), result)
        result = API_KEY_PATTERN.sub(lambda m: str(self.register(m.group(0), "api_key")), result)
        return result

    def value(self, value: Any, parent_apikey: str = "") -> Any:
        if isinstance(value, dict):
            apikey = str(value.get("apikey", parent_apikey)).lower()
            sanitized = {}
            for key, item in value.items():
                normalized = str(key).lower()
                if normalized in PII_FIELDS | IDENTIFIER_FIELDS | SECRET_FIELDS:
                    sanitized[key] = self.register(item, normalized)
                elif apikey in PII_FIELDS and normalized in {"value", "label"}:
                    sanitized[key] = self.register(item, apikey)
                else:
                    sanitized[key] = self.value(item, apikey)
            return sanitized
        if isinstance(value, list):
            return [self.value(item, parent_apikey) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    @property
    def automatic_count(self) -> int:
        return max(0, len(self.replacements) - len(self.manual))


@dataclass
class PromotionResult:
    case_id: str
    target_dir: Path
    trace_count: int
    automatic_redactions: int
    manual_redactions: int
    expected_exit_code: int


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromotionError(f"读取文件失败: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PromotionError(f"JSON 格式错误: {path}: {exc}") from exc


def _load_redactions(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = _load_json(path)
    if not isinstance(data, dict):
        raise PromotionError("redactions 文件必须是 JSON 对象：{原值: 替换值}")
    return data


def _find_failed_result(report: dict, case_id: str) -> dict:
    for result in report.get("results", []):
        if result.get("case_id") == case_id:
            if result.get("overall_pass") is not False:
                raise PromotionError(f"报告中的用例 {case_id} 不是 FAIL，不能作为 BadCase 沉淀")
            return result
    raise PromotionError(f"报告中未找到用例: {case_id}")


def _find_source_case(cases_dir: Path, case_id: str) -> dict:
    matches = []
    for path in sorted(cases_dir.glob("*.json")):
        data = _load_json(path)
        cases = data if isinstance(data, list) else [data]
        matches.extend(case for case in cases if isinstance(case, dict) and case.get("id") == case_id)
    if not matches:
        raise PromotionError(f"在 {cases_dir} 中未找到源用例: {case_id}")
    if len(matches) > 1:
        raise PromotionError(f"源用例 ID {case_id} 在 {cases_dir} 中不唯一")
    return matches[0]


def _find_recording_dir(
    recorded_root: Path,
    case_id: str,
    report_tag: str,
    explicit_dir: Path | None,
) -> Path:
    if explicit_dir is not None:
        candidate = explicit_dir
    else:
        case_dir = recorded_root / case_id
        tagged = case_dir / report_tag if report_tag else None
        if tagged and tagged.is_dir():
            candidate = tagged
        elif list(case_dir.glob("T*.sse")):
            candidate = case_dir
        else:
            subdirs = sorted(path for path in case_dir.iterdir() if path.is_dir()) if case_dir.is_dir() else []
            if len(subdirs) != 1:
                raise PromotionError(
                    f"无法唯一确定 {case_id} 的录制目录；请用 --recording-dir 明确指定"
                )
            candidate = subdirs[0]
    if not candidate.is_dir():
        raise PromotionError(f"录制目录不存在: {candidate}")
    return candidate


def _parse_sse_data(text: str) -> list[Any]:
    values = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            values.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return values


def _sanitize_sse(text: str, sanitizer: Sanitizer) -> str:
    output = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                output.append(sanitizer.text(line))
            else:
                output.append("data: " + json.dumps(sanitizer.value(data), ensure_ascii=False))
        else:
            output.append(sanitizer.text(line))
    return "\n".join(output) + "\n"


def _residual_sensitive_kinds(text: str) -> list[str]:
    found = []
    for label, pattern in (
        ("email", EMAIL_PATTERN),
        ("mobile", MOBILE_PATTERN),
        ("bearer token", BEARER_PATTERN),
        ("API key", API_KEY_PATTERN),
    ):
        if pattern.search(text):
            found.append(label)
    return found


def promote_badcase(
    *,
    report_path: str | Path,
    case_id: str,
    root_cause: str,
    issue_type: str,
    confirmed: bool,
    recorded_root: str | Path = "fixtures/recorded",
    cases_dir: str | Path = "cases",
    target_root: str | Path = "fixtures/regression",
    recording_dir: str | Path | None = None,
    redactions_path: str | Path | None = None,
) -> PromotionResult:
    """确认失败、完成脱敏扫描并安全加入 Regression Set。"""
    if not confirmed:
        raise PromotionError("必须人工确认根因并授权沉淀；请显式传入 --confirm")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", case_id):
        raise PromotionError("case_id 只能包含字母、数字、点、下划线和连字符")
    if issue_type not in ISSUE_TYPES:
        raise PromotionError(f"issue_type 必须是 {sorted(ISSUE_TYPES)}")
    if not root_cause.strip():
        raise PromotionError("root_cause 不能为空")

    report_path = Path(report_path)
    report = _load_json(report_path)
    _find_failed_result(report, case_id)
    source_case = _find_source_case(Path(cases_dir), case_id)
    source_recording_dir = _find_recording_dir(
        Path(recorded_root), case_id, str(report.get("meta", {}).get("tag", "")),
        Path(recording_dir) if recording_dir else None,
    )
    trace_paths = sorted(source_recording_dir.glob("T*.sse"))
    if len(trace_paths) < len(source_case.get("turns", [])):
        raise PromotionError(
            f"录制只有 {len(trace_paths)} 轮，源用例需要 {len(source_case.get('turns', []))} 轮"
        )

    sanitizer = Sanitizer(_load_redactions(Path(redactions_path) if redactions_path else None))
    sanitizer.collect(source_case)
    raw_traces = [(path.name, path.read_text(encoding="utf-8")) for path in trace_paths]
    for _, text in raw_traces:
        for data in _parse_sse_data(text):
            sanitizer.collect(data)

    sanitized_case = sanitizer.value(source_case)
    metadata = sanitized_case.setdefault("metadata", {})
    metadata.update({
        "regression": True,
        "source": "live_badcase",
        "issue_type": issue_type,
    })
    try:
        validate_cases([sanitized_case])
    except CaseValidationError as exc:
        raise PromotionError(f"脱敏后的用例 Schema 不合法: {exc}") from exc

    sanitized_traces = [(name, _sanitize_sse(text, sanitizer)) for name, text in raw_traces]
    combined = json.dumps(sanitized_case, ensure_ascii=False) + "\n" + "\n".join(
        text for _, text in sanitized_traces
    )
    residual = _residual_sensitive_kinds(combined)
    if residual:
        raise PromotionError(f"脱敏后仍检测到敏感信息: {sorted(set(residual))}")
    parser = SSEParser()
    for name, text in sanitized_traces:
        try:
            parser.parse(text.splitlines())
        except Exception as exc:
            raise PromotionError(f"脱敏后的录制无法解析: {name}: {exc}") from exc

    target_root = Path(target_root)
    cases_path = target_root / CASES_FILENAME
    manifest_path = target_root / MANIFEST_FILENAME
    existing_cases = _load_json(cases_path) if cases_path.exists() else []
    manifest = _load_json(manifest_path) if manifest_path.exists() else {
        "version": MANIFEST_VERSION,
        "entries": [],
    }
    if not isinstance(existing_cases, list) or not isinstance(manifest.get("entries"), list):
        raise PromotionError("Regression Set 的 cases.json 或 manifest.json 结构错误")
    if any(case.get("id") == case_id for case in existing_cases):
        raise PromotionError(f"Regression Set 已存在用例: {case_id}")

    target_case_dir = target_root / case_id
    if target_case_dir.exists():
        raise PromotionError(f"目标录制目录已存在: {target_case_dir}")

    status = "known"
    expected = DEFAULT_EXPECTED_EXIT[status]
    entry = {
        "case_id": case_id,
        "source_report": report_path.name,
        "root_cause": root_cause.strip(),
        "issue_type": issue_type,
        "status": status,
        "blocking": status == "fixed",
        "expected_exit_code": expected,
        "live_regression_required": issue_type == "agent",
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "trace_files": [name for name, _ in sanitized_traces],
        "sanitization": {
            "automatic_redactions": sanitizer.automatic_count,
            "manual_redactions": len(sanitizer.manual),
            "residual_scan_passed": True,
        },
        "root_cause_confirmed": True,
    }

    target_case_dir.mkdir(parents=True)
    for name, text in sanitized_traces:
        (target_case_dir / name).write_text(text, encoding="utf-8")
    existing_cases.append(sanitized_case)
    manifest["version"] = MANIFEST_VERSION
    manifest["entries"].append(entry)
    cases_path.write_text(json.dumps(existing_cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verify_regression_set(target_root)
    return PromotionResult(
        case_id=case_id,
        target_dir=target_case_dir,
        trace_count=len(sanitized_traces),
        automatic_redactions=sanitizer.automatic_count,
        manual_redactions=len(sanitizer.manual),
        expected_exit_code=expected,
    )


def verify_regression_set(root: str | Path) -> int:
    """校验清单、Case Schema、录制完整性、可解析性和残余敏感模式。"""
    root = Path(root)
    cases_path = root / CASES_FILENAME
    manifest_path = root / MANIFEST_FILENAME
    if not cases_path.exists() or not manifest_path.exists():
        raise PromotionError(f"Regression Set 缺少 {CASES_FILENAME} 或 {MANIFEST_FILENAME}: {root}")
    cases = _load_json(cases_path)
    manifest = _load_json(manifest_path)
    if not isinstance(cases, list) or not isinstance(manifest.get("entries"), list):
        raise PromotionError("Regression Set 文件结构错误")
    try:
        validate_cases(cases)
    except CaseValidationError as exc:
        raise PromotionError(f"Regression Case Schema 校验失败: {exc}") from exc

    case_by_id = {case["id"]: case for case in cases}
    entry_by_id = {entry.get("case_id"): entry for entry in manifest["entries"]}
    if set(case_by_id) != set(entry_by_id):
        raise PromotionError("cases.json 与 manifest.json 的 case_id 不一致")

    parser = SSEParser()
    all_text = [cases_path.read_text(encoding="utf-8"), manifest_path.read_text(encoding="utf-8")]
    for case_id, case in case_by_id.items():
        case_dir = root / case_id
        traces = sorted(case_dir.glob("T*.sse")) if case_dir.is_dir() else []
        if len(traces) < len(case.get("turns", [])):
            raise PromotionError(f"{case_id} 的录制轮数不足")
        expected_exit = entry_by_id[case_id].get("expected_exit_code")
        if expected_exit not in {0, 1}:
            raise PromotionError(f"{case_id} 的 expected_exit_code 必须是 0 或 1")
        status = entry_by_id[case_id].get("status")
        blocking = entry_by_id[case_id].get("blocking")
        if status not in LIFECYCLE_STATUSES or not isinstance(blocking, bool):
            raise PromotionError(f"{case_id} 缺少合法的 status/blocking")
        if status == "fixed" and (blocking is not True or expected_exit != 0):
            raise PromotionError(f"{case_id} 已 fixed，必须 blocking=true 且 expected_exit_code=0")
        if status == "known" and (blocking is not False or expected_exit != 1):
            raise PromotionError(f"{case_id} 仍为 known，必须 blocking=false 且 expected_exit_code=1")
        for trace in traces:
            text = trace.read_text(encoding="utf-8")
            all_text.append(text)
            parser.parse(text.splitlines())

    residual = _residual_sensitive_kinds("\n".join(all_text))
    if residual:
        raise PromotionError(f"Regression Set 仍包含敏感信息: {sorted(set(residual))}")
    return len(cases)
