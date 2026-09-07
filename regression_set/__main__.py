"""Regression Set CLI。"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from regression_set.promoter import PromotionError, promote_badcase, verify_regression_set


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BadCase → Replay Regression Set")
    sub = parser.add_subparsers(dest="command", required=True)

    promote = sub.add_parser("promote", help="确认、脱敏并沉淀一个真实失败用例")
    promote.add_argument("--report", required=True, help="真实失败 JSON 报告")
    promote.add_argument("--case", required=True, dest="case_id", help="失败 Case ID")
    promote.add_argument("--root-cause", required=True, help="人工确认后的根因")
    promote.add_argument("--issue-type", required=True, choices=["agent", "evaluator", "oracle"])
    promote.add_argument("--confirm", action="store_true", help="确认根因已人工审核，并授权写入脱敏结果")
    promote.add_argument("--recorded-root", default="fixtures/recorded")
    promote.add_argument("--recording-dir", help="明确指定包含 T*.sse 的录制目录")
    promote.add_argument("--cases-dir", default="cases")
    promote.add_argument("--target", default="fixtures/regression")
    promote.add_argument("--redactions", help="本地 JSON 脱敏映射文件（不要提交）")

    verify = sub.add_parser("verify", help="校验 Regression Set 可回放且无常见敏感模式")
    verify.add_argument("--root", default="fixtures/regression")

    run = sub.add_parser("run", help="按 manifest 的期望退出码逐条执行回放")
    run.add_argument("--root", default="fixtures/regression")

    mark_fixed = sub.add_parser("mark-fixed", help="回放通过后升级为阻断型回归")
    mark_fixed.add_argument("--root", default="fixtures/regression")
    mark_fixed.add_argument("--case", required=True, dest="case_id")
    return parser


def _execute_case(root_path: Path, case_id: str) -> int:
    root_path = Path(root_path)
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(project_root / "main.py"),
            "--replay", str(root_path.resolve()),
            "--case", case_id,
            "--no-report",
        ],
        cwd=project_root,
        check=False,
    )
    return completed.returncode


def _run_regressions(root: str) -> int:
    count = verify_regression_set(root)
    if count == 0:
        print("Regression Set 为空，跳过回放。")
        return 0

    root_path = Path(root)
    manifest = json.loads((root_path / "manifest.json").read_text(encoding="utf-8"))
    failed = []
    known_failed = 0
    recovered = 0
    for entry in manifest["entries"]:
        case_id = entry["case_id"]
        expected = entry["expected_exit_code"]
        blocking = entry["blocking"]
        status = entry["status"]
        print(f"\n[Regression] {case_id} | status={status} | blocking={blocking}")
        actual = _execute_case(root_path, case_id)
        if blocking and actual != expected:
            failed.append((case_id, expected, actual))
        elif not blocking and actual == 0:
            recovered += 1
            print(f"RECOVERED: {case_id} 已通过；人工确认后可执行 mark-fixed 升级为阻断型回归。")
        elif not blocking and actual == 1:
            known_failed += 1
            print(f"KNOWN FAIL: {case_id} 仍失败，本次不阻断 CI。")
        elif not blocking:
            # 用例失败(1)可以非阻断；配置、Schema、文件等基础错误不能被已知 Bug 掩盖。
            failed.append((case_id, "0 or 1", actual))
    if failed:
        for case_id, expected, actual in failed:
            print(f"FAIL: {case_id} expected={expected} actual={actual}")
        return 1
    print(
        f"\nRegression Set 执行完成：{count} 个用例，"
        f"known_fail={known_failed}，recovered={recovered}。"
    )
    return 0


def _mark_fixed(root: str, case_id: str) -> int:
    verify_regression_set(root)
    root_path = Path(root)
    manifest_path = root_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next((item for item in manifest["entries"] if item.get("case_id") == case_id), None)
    if entry is None:
        raise PromotionError(f"Regression Set 中未找到用例: {case_id}")
    actual = _execute_case(root_path, case_id)
    if actual != 0:
        print(f"未升级：{case_id} 当前回放退出码为 {actual}，只有通过后才能标记 fixed。")
        return 1
    entry.update({
        "status": "fixed",
        "blocking": True,
        "expected_exit_code": 0,
        "fixed_at": datetime.now(timezone.utc).isoformat(),
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_regression_set(root_path)
    print(f"已升级：{case_id} → fixed / blocking。")
    return 0


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "promote":
            result = promote_badcase(
                report_path=args.report,
                case_id=args.case_id,
                root_cause=args.root_cause,
                issue_type=args.issue_type,
                confirmed=args.confirm,
                recorded_root=args.recorded_root,
                cases_dir=args.cases_dir,
                target_root=args.target,
                recording_dir=args.recording_dir,
                redactions_path=args.redactions,
            )
            print(
                f"已沉淀 {result.case_id}: traces={result.trace_count}, "
                f"automatic_redactions={result.automatic_redactions}, "
                f"manual_redactions={result.manual_redactions}, "
                f"expected_exit={result.expected_exit_code}"
            )
            return 0
        if args.command == "verify":
            count = verify_regression_set(args.root)
            print(f"Regression Set 校验通过：{count} 个用例。")
            return 0
        if args.command == "mark-fixed":
            return _mark_fixed(args.root, args.case_id)
        return _run_regressions(args.root)
    except PromotionError as exc:
        print(f"错误: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
