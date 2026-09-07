"""
Agent Runner - 负责与 CopilotKit Agent 通信

多轮会话管理：
- 第一轮发送 conversation_id: null，从 RUN_STARTED 响应中获取后端分配的 conversation_id
- 后续轮次带上真实的 conversation_id，后端通过它恢复上下文
- 同一个多轮 case 全程复用同一个 threadId
- 每一轮生成新的 runId
- messages 里的 id 第一轮用前端生成的，后续用 current_message_id
"""

import os
import time
import uuid
import requests
from typing import List
from dataclasses import dataclass, field

import config
from runner.sse_parser import SSEParser, AgentTrace, extract_infra_trace_id


@dataclass
class ConversationState:
    """多轮会话状态"""
    thread_id: str = ""
    conversation_id: str = None  # None = 新对话，后端会分配
    messages: list = field(default_factory=list)
    current_message_id: int = 0


class AgentRunner:
    """Agent 执行器，支持单轮和多轮对话"""

    def __init__(self, url: str = None, headers: dict = None, record_dir: str = None,
                 record_tag: str = ""):
        self.url = url or config.AGENT_URL
        self.headers = headers or config.get_headers()
        self.parser = SSEParser()
        self._headers_dumped = False  # 只 dump 一次响应头（诊断用）

        # 录制：把每轮原始 SSE 落盘，供 --replay 离线复现
        self.record_dir = record_dir
        # 录制 tag：不为空时按 <record_dir>/<CASE_ID>/<tag>/T##.sse 分目录存放，
        # 避免同一 case 多次运行互相覆盖（与报告文件名的 tag 对应）
        self.record_tag = record_tag
        self.current_case_id = ""
        self._turn_index = 0

    def set_case(self, case_id: str):
        """
        切换当前用例（与 ReplayRunner 接口对齐）。

        只在录制时需要——用来决定录制文件的落盘路径。
        """
        self.current_case_id = case_id
        self._turn_index = 0

    def _run_turn(self, payload: dict) -> AgentTrace:
        """
        发送一轮请求并解析，同时记录时间窗和基础设施链路 ID。
        """
        started_at_ms = int(time.time() * 1000)
        lines, resp_headers = self._send_request(payload)

        # 录制时旁路抓取原始行；解析器提前 break 后未读到的行不会被录，
        # 这正好与「解析器实际看到的内容」一致，回放才能真实复现
        recorded_lines = [] if self.record_dir else None
        if recorded_lines is not None:
            lines = self._tee(lines, recorded_lines)

        trace = self.parser.parse(lines, timeout_seconds=config.SSE_TURN_TIMEOUT)
        trace.finished_at_ms = int(time.time() * 1000)

        self._turn_index += 1
        if recorded_lines is not None:
            self._write_recording(recorded_lines)
        trace.started_at_ms = started_at_ms
        trace.response_headers = {str(k): str(v) for k, v in dict(resp_headers or {}).items()}
        trace.infra_trace_id = extract_infra_trace_id(resp_headers)

        # 首轮打印一次完整响应头，便于确认日志平台 traceId 藏在哪个 header
        if config.DUMP_RESPONSE_HEADERS and not self._headers_dumped:
            self._headers_dumped = True
            print("\n    [诊断] Agent 响应头:")
            for k, v in trace.response_headers.items():
                print(f"      {k}: {v}")
            print(f"    [诊断] 解析到 infra_trace_id: {trace.infra_trace_id or '<未找到>'}\n")

        return trace

    def run_single(self, message: str) -> AgentTrace:
        """
        单轮对话：发送一条消息，返回完整 trace。
        """
        thread_id = self._generate_thread_id()
        run_id = self._generate_run_id()
        msg_id = self._generate_message_id()

        messages = [{
            "id": msg_id,
            "role": "user",
            "content": message,
            "type": "TextMessage",
        }]

        payload = self._build_payload(
            messages=messages,
            thread_id=thread_id,
            run_id=run_id,
            conversation_id=None,
        )

        return self._run_turn(payload)

    def run_multi(self, turns: List[dict]) -> List[AgentTrace]:
        """
        多轮对话：
        - 第一轮 conversation_id=null，从响应获取真实 ID
        - 后续轮次带上 conversation_id
        - 全程复用 threadId
        - 每轮新 runId
        - messages 累积完整历史

        Args:
            turns: [{"input": "...", "turn_id": "C01T01"}, ...]

        Returns:
            List[AgentTrace]，每轮一个独立 trace
        """
        session = ConversationState(
            thread_id=self._generate_thread_id(),
            conversation_id=None,  # 第一轮为 null
        )

        traces = []

        for i, turn in enumerate(turns):
            user_msg = turn["input"]
            run_id = self._generate_run_id()
            print(f"    [轮次 {i+1}/{len(turns)}] 发送中...", end="", flush=True)

            # 构建消息
            if i == 0:
                # 第一轮：用前端格式的 message id
                msg_id = self._generate_message_id()
            else:
                # 后续轮：用 current_message_id 自增（模拟前端行为）
                msg_id = session.current_message_id + 1 if session.current_message_id else self._generate_message_id()

            session.messages.append({
                "id": msg_id,
                "role": "user",
                "content": user_msg,
                "type": "TextMessage",
            })

            # 构建 payload
            payload = self._build_payload(
                messages=session.messages,
                thread_id=session.thread_id,
                run_id=run_id,
                conversation_id=session.conversation_id,
            )

            # 诊断：从第二轮起打印实际带出的 conversation_id。
            # 后续轮次靠它恢复上下文，为空说明会话没续上，
            # Agent 会把「确认创建」当成全新对话，导致本轮无有效输出。
            if i > 0:
                if session.conversation_id:
                    print(f"\n    [诊断] 本轮携带 conversation_id={session.conversation_id}")
                else:
                    print("\n    [诊断] 警告: conversation_id 为空，会话未续上，本轮将丢失上下文")
                print("    ", end="", flush=True)

            # 发送请求
            trace = self._run_turn(payload)
            traces.append(trace)
            elapsed = f" ({trace.total_duration_ms/1000:.1f}s)" if trace.total_duration_ms else ""
            print(f" 完成{elapsed}")

            # 获取 conversation_id：优先信 RUN_STARTED，拿不到就反查会话历史
            if not session.conversation_id:
                if trace.conversation_id:
                    session.conversation_id = str(trace.conversation_id)
                elif config.CONVERSATION_DISCOVERY_ENABLED:
                    discovered = self._discover_conversation_id(trace.started_at_ms)
                    if discovered:
                        session.conversation_id = discovered
                        print(f"    [会话发现] conversation_id={discovered}")
                    else:
                        print("    [会话发现] 未找到会话，后续轮次仍会丢失上下文")

            # 更新 current_message_id
            if trace.current_message_id:
                session.current_message_id = trace.current_message_id

            # 将 Agent 回复追加到消息历史
            if trace.reply_text:
                # 后续轮的 assistant message id 用 current_message_id
                assistant_msg_id = trace.current_message_id if trace.current_message_id else self._generate_message_id()
                session.messages.append({
                    "id": assistant_msg_id,
                    "role": "assistant",
                    "content": trace.reply_text,
                })

        return traces

    def _build_payload(
        self,
        messages: list,
        thread_id: str,
        run_id: str,
        conversation_id: str = None,
    ) -> dict:
        """
        构建请求 payload，对齐前端真实格式。
        """
        return {
            "threadId": thread_id,
            "runId": run_id,
            "messages": messages,
            "state": {},
            "tools": [],  # 前端会传可用 tools，但 Agent 后端也知道自己有什么 tool
            "context": [
                {
                    "description": "当前页面：AI Native CRM 工作台",
                    "value": '{"page":"workbench","view":"newChat"}' if conversation_id is None else '{"page":"workbench","view":"history"}',
                },
                {
                    "description": "当前页面：AI Native CRM 对话页",
                    "value": self._build_chat_context_value(conversation_id),
                },
            ],
            "forwardedProps": {
                "page": "home",
                "conversation_id": conversation_id,  # null 第一轮，真实 ID 后续轮
                "userId": "current-user",
                "tenantId": "tenant-001",
                "locale": "zh-CN",
                "bizLine": "ai-native-crm",
            },
        }

    def _send_request(self, payload: dict):
        """
        发送请求到 Agent，带重试逻辑。

        Returns:
            (lines_iterator, response_headers)
        """
        last_error = None
        for attempt in range(config.MAX_RETRIES + 1):
            try:
                response = requests.post(
                    self.url,
                    json=payload,
                    headers=self.headers,
                    stream=True,
                    timeout=(config.REQUEST_TIMEOUT, config.STREAM_READ_TIMEOUT),
                    verify=False,
                )
                response.raise_for_status()
                return response.iter_lines(), response.headers

            except requests.exceptions.Timeout as e:
                last_error = e
                print(f"  [重试 {attempt + 1}/{config.MAX_RETRIES + 1}] 超时: {e}")
            except requests.exceptions.HTTPError as e:
                last_error = e
                status = e.response.status_code if e.response else "unknown"
                print(f"  [重试 {attempt + 1}/{config.MAX_RETRIES + 1}] HTTP {status}: {e}")
                if status in (401, 403):
                    raise
            except requests.exceptions.RequestException as e:
                last_error = e
                print(f"  [重试 {attempt + 1}/{config.MAX_RETRIES + 1}] 请求异常: {e}")

            if attempt < config.MAX_RETRIES:
                time.sleep(config.RETRY_DELAY)

        raise RuntimeError(f"请求失败，已重试 {config.MAX_RETRIES} 次: {last_error}")

    # ============================================================
    # 录制
    # ============================================================

    @staticmethod
    def _tee(lines, sink: list):
        """边产出边收集原始行（解码成 str 存档，原样 yield 给解析器）"""
        for line in lines:
            if isinstance(line, bytes):
                sink.append(line.decode("utf-8", "replace"))
            else:
                sink.append(line)
            yield line

    def _write_recording(self, lines: list):
        """
        把本轮原始 SSE 写入录制文件。

        - 无 tag: <record_dir>/<CASE_ID>/T##.sse
        - 有 tag: <record_dir>/<CASE_ID>/<tag>/T##.sse
          带 tag 分目录，让同一 case 的多次运行不互相覆盖。
        """
        case_id = self.current_case_id or "UNKNOWN_CASE"
        case_dir = os.path.join(self.record_dir, case_id)
        if self.record_tag:
            case_dir = os.path.join(case_dir, self._safe_tag(self.record_tag))
        os.makedirs(case_dir, exist_ok=True)

        path = os.path.join(case_dir, f"T{self._turn_index:02d}.sse")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")
        print(f"    [录制] {path}")

    @staticmethod
    def _safe_tag(tag: str) -> str:
        """把 tag 清理成安全的目录名（去掉路径分隔符和空白等非法字符）"""
        import re
        return re.sub(r'[^\w.\-]+', '_', tag.strip()) or "untagged"

    # ============================================================
    # 会话发现
    # ============================================================

    def _discover_conversation_id(self, turn_started_at_ms: int) -> str:
        """
        反查后端隐式创建的会话 ID。

        后端 RUN_STARTED 不返回 conversation_id，会话由首个 run 请求隐式创建
        （实测 createdAt 比前端生成 threadId 晚约 700ms），因此首轮结束后拉一次
        会话历史，取「createdAt 不早于本轮发起时间」的最新一条。

        会话可能异步落库，故重试若干次。返回空串表示没找到。

        已知局限：若同时在浏览器里聊天，仍有抢到错误会话的可能；并发跑多个
        case 时该方案不成立，需要后端在 RUN_STARTED 里回传 conversation_id。
        """
        min_created_at = max(0, (turn_started_at_ms or 0) - config.CONVERSATION_MATCH_BUFFER_MS)

        for attempt in range(config.CONVERSATION_DISCOVERY_RETRIES):
            items = self._fetch_conversation_history()

            if items:
                # 基础过滤：未删除 + 目标 agent
                valid = [
                    it for it in items
                    if it.get("deleteFlg", 0) == 0
                    and (not it.get("agentApiKey") or it.get("agentApiKey") == config.AGENT_API_KEY)
                ]
                # 叠加时间窗过滤
                candidates = [
                    it for it in valid
                    if int(it.get("createdAt") or 0) >= min_created_at
                ]

                if candidates:
                    # 不依赖接口返回顺序（可能置顶优先），自己按 createdAt 降序取最新
                    newest = max(candidates, key=lambda it: int(it.get("createdAt") or 0))
                    return str(newest.get("id") or "")

                # 时间窗内没有候选：可能是本地与服务端时钟偏差过大。
                # 最后一次尝试时放宽时间窗，但仍保留基础过滤。
                if attempt == config.CONVERSATION_DISCOVERY_RETRIES - 1 and valid:
                    newest = max(valid, key=lambda it: int(it.get("createdAt") or 0))
                    print(
                        f"    [会话发现] 警告: 时间窗内无匹配会话（可能存在时钟偏差），"
                        f"回退到最新一条 id={newest.get('id')}"
                    )
                    return str(newest.get("id") or "")

            if attempt < config.CONVERSATION_DISCOVERY_RETRIES - 1:
                time.sleep(config.CONVERSATION_DISCOVERY_DELAY)

        return ""

    def _fetch_conversation_history(self) -> list:
        """
        拉取会话历史列表，返回 items。

        method 与请求体格式尚未确认，先试 POST + JSON，失败回退 GET + query。
        返回空列表表示拉取失败或无数据。
        """
        payload = {
            "pageNo": 1,
            "pageSize": config.CONVERSATION_HISTORY_PAGE_SIZE,
            "agentApiKey": config.AGENT_API_KEY,
        }

        for method in ("POST", "GET"):
            try:
                if method == "POST":
                    response = requests.post(
                        config.CONVERSATION_HISTORY_URL,
                        json=payload,
                        headers=self._json_headers(),
                        timeout=config.REQUEST_TIMEOUT,
                        verify=False,
                    )
                else:
                    response = requests.get(
                        config.CONVERSATION_HISTORY_URL,
                        params=payload,
                        headers=self._json_headers(),
                        timeout=config.REQUEST_TIMEOUT,
                        verify=False,
                    )

                if response.status_code != 200:
                    continue

                body = response.json()
                if str(body.get("code")) not in ("200", "0"):
                    continue

                items = (body.get("data") or {}).get("items") or []
                if items:
                    return items

            except Exception as e:
                print(f"    [会话发现] {method} 请求异常: {e}")

        return []

    def _json_headers(self) -> dict:
        """
        构建 JSON 接口请求头。

        复用 run 接口的认证信息，但 Accept 要换成 application/json
        （run 接口用的是 text/event-stream）。
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        for key in ("Cookie", "Authorization"):
            if self.headers.get(key):
                headers[key] = self.headers[key]
        return headers

    @staticmethod
    def _build_chat_context_value(conversation_id):
        """构建 chat context JSON 字符串"""
        import json
        return json.dumps({"page": "chat", "conversationId": conversation_id}, ensure_ascii=False)

    def _generate_thread_id(self) -> str:
        """生成 thread ID（模拟前端格式: workbench-{timestamp}-{random}）"""
        ts = int(time.time() * 1000)
        suffix = uuid.uuid4().hex[:6]
        return f"workbench-{ts}-{suffix}"

    def _generate_run_id(self) -> str:
        """每轮生成新的 run ID"""
        return str(uuid.uuid4())

    def _generate_message_id(self) -> str:
        """生成消息 ID（模拟前端格式: msg-{timestamp}）"""
        ts = int(time.time() * 1000)
        return f"msg-{ts}"
