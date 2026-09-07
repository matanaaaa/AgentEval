"""
离线 SSE 回放

把录制好的 SSE 流当作 Agent 响应喂给现有解析与评测链路，全程不发任何网络请求。

用途：
- CI 里跑端到端冒烟（连不上内网 CRM 也能验证 parser → evaluator → reporter 全链路）
- 改 evaluator / reporter 时能立刻看到真实报告长什么样，而不只是单测断言
- 复现线上问题：把出问题那次的 SSE 存下来，反复回放调试

录制目录结构：
    <replay_dir>/
        cases.json          # 用例集（与 cases/ 下格式一致）
        <CASE_ID>/
            T01.sse         # 第一轮的原始 SSE 文本
            T02.sse         # 第二轮……按文件名排序对应轮次

.sse 文件就是原始响应体，逐行喂给 SSEParser，与真实请求走的是同一条解析路径。
"""

import glob
import os
import time
from typing import List

import config
from runner.sse_parser import SSEParser, AgentTrace

CASES_FILENAME = "cases.json"
RECORDING_SUFFIX = ".sse"


class ReplayError(RuntimeError):
    """回放数据缺失或不可用"""


class ReplayRunner:
    """
    AgentRunner 的离线替身。

    对外暴露与 AgentRunner 相同的 run_single / run_multi，因此 main.run_case
    不需要区分两者。额外提供 set_case()，用于告知当前回放哪个用例的录制。
    """

    def __init__(self, replay_dir: str, parser: SSEParser = None):
        if not os.path.isdir(replay_dir):
            raise ReplayError(f"回放目录不存在: {replay_dir}")

        self.replay_dir = replay_dir
        self.parser = parser or SSEParser()
        self.current_case_id = ""

    # ------------------------------------------------------------
    # 与 AgentRunner 对齐的接口
    # ------------------------------------------------------------

    def set_case(self, case_id: str):
        """切换当前回放的用例"""
        self.current_case_id = case_id

    def run_single(self, message: str) -> AgentTrace:
        """回放第一轮（单轮用例只有一份录制）"""
        recordings = self._recordings_for_case()
        return self._replay_one(recordings[0])

    def run_multi(self, turns: List[dict]) -> List[AgentTrace]:
        """
        逐轮回放。

        录制份数少于用例轮次时直接报错——静默少跑几轮会让报告失真，
        比缺数据更难排查。
        """
        recordings = self._recordings_for_case()

        if len(recordings) < len(turns):
            raise ReplayError(
                f"用例 {self.current_case_id} 有 {len(turns)} 轮，"
                f"但只找到 {len(recordings)} 份录制: "
                f"{[os.path.basename(p) for p in recordings]}"
            )

        traces = []
        for i, turn in enumerate(turns):
            print(f"    [回放 {i+1}/{len(turns)}] {os.path.basename(recordings[i])}")
            traces.append(self._replay_one(recordings[i]))
        return traces

    # ------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------

    def _recordings_for_case(self) -> List[str]:
        """按文件名排序返回当前用例的录制文件路径"""
        if not self.current_case_id:
            raise ReplayError("未指定用例 ID，请先调用 set_case()")

        case_dir = os.path.join(self.replay_dir, self.current_case_id)
        if not os.path.isdir(case_dir):
            available = sorted(
                d for d in os.listdir(self.replay_dir)
                if os.path.isdir(os.path.join(self.replay_dir, d))
            )
            raise ReplayError(
                f"未找到用例 {self.current_case_id} 的录制目录: {case_dir}"
                f"｜可用: {available}"
            )

        recordings = sorted(glob.glob(os.path.join(case_dir, f"*{RECORDING_SUFFIX}")))
        if not recordings:
            raise ReplayError(f"用例 {self.current_case_id} 的录制目录为空: {case_dir}")

        return recordings

    def _replay_one(self, path: str) -> AgentTrace:
        """回放单份录制"""
        started_at_ms = int(time.time() * 1000)

        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()

        trace = self.parser.parse(lines, timeout_seconds=config.SSE_TURN_TIMEOUT)
        trace.started_at_ms = started_at_ms
        trace.finished_at_ms = int(time.time() * 1000)
        return trace


def default_cases_path(replay_dir: str) -> str:
    """回放目录下的默认用例集路径"""
    return os.path.join(replay_dir, CASES_FILENAME)
