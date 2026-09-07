"""
AI Testing Agent CLI 入口

用法:
    python -m testgen --requirement "录入客户支持工商候选、字段抽取、确认创建"
    python -m testgen --req-file req.txt --object-type account --max-scenarios 3
    python -m testgen --requirement "..." --dry-run     # 只看结果不落盘

需要配置 LLM：
    环境变量 LLM_API_KEY（必填），可选 LLM_BASE_URL / LLM_MODEL
    或在 credentials.yaml 里填 llm_api_key
"""

import argparse
import sys

from testgen.llm import LLMError
from testgen.pipeline import run_pipeline
from testgen.planner import MAX_SCENARIOS_LIMIT


def main():
    parser = argparse.ArgumentParser(description="AI Testing Agent - 用例生成流水线")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--requirement", type=str, help="需求描述（自然语言）")
    src.add_argument("--req-file", type=str, help="从文件读取需求描述")

    parser.add_argument("--object-type", type=str, default="account",
                        help="业务对象类型（默认 account）")
    parser.add_argument("--id-prefix", type=str, default="GEN_ACCOUNT_",
                        help="用例 id 前缀（默认 GEN_ACCOUNT_）")
    parser.add_argument(
        "--max-scenarios",
        type=int,
        choices=range(1, MAX_SCENARIOS_LIMIT + 1),
        default=MAX_SCENARIOS_LIMIT,
        help=f"场景数量上限（默认且最大 {MAX_SCENARIOS_LIMIT}）",
    )
    parser.add_argument("--out", type=str, default=None,
                        help="输出路径（默认 cases/<prefix>_generated.json）")
    parser.add_argument("--dry-run", action="store_true",
                        help="通过门禁也不落盘，仅打印结果")
    args = parser.parse_args()

    if args.req_file:
        try:
            with open(args.req_file, encoding="utf-8") as f:
                requirement = f.read().strip()
        except OSError as e:
            print(f"错误: 读取需求文件失败: {e}")
            return 3
    else:
        requirement = args.requirement.strip()

    if not requirement:
        print("错误: 需求描述为空")
        return 3

    try:
        output = run_pipeline(
            requirement,
            object_type=args.object_type,
            id_prefix=args.id_prefix,
            max_scenarios=args.max_scenarios,
            out_path=args.out,
            dry_run=args.dry_run,
        )
    except LLMError as e:
        print(f"错误: {e}")
        return 3

    # 门禁未过 → 非零退出，便于 CI 卡住
    return 0 if (output.gate and output.gate.passed) else 1


if __name__ == "__main__":
    sys.exit(main())
