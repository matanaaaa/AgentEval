"""
SSE 事件解析器
基于 CopilotKit Agent 的真实 SSE 响应格式，提取结构化 trace 数据。

事件类型：
- RUN_STARTED: 运行开始，包含 run_id, thread_id, trace_url
- ACTIVITY_SNAPSHOT: 完整状态快照（Skill/Tool 执行状态、业务数据）
- ACTIVITY_DELTA: 增量状态更新（JSON Patch 格式）
- TEXT_MESSAGE_START/CONTENT/END: Agent 文本回复
- RUN_FINISHED: 运行结束，包含 cost 信息
"""

import json
import time
from dataclasses import dataclass, field
from typing import Optional


# ============================================================
# 基础设施链路 ID 候选响应头
# 日志平台（kt.sre）的 traceId 与 SSE 里的 trace_url 不是同一套，
# 通常由网关/APM 埋点写在响应头里。按优先级依次尝试。
# ============================================================
TRACE_ID_HEADERS = (
    # 实测命中：网关回写的 SkyWalking 链路 ID，格式与日志里的
    # traceId: t.<32hex>.<seq>.<epoch ms> 一致
    "x-sw-traceid",
    "sw-traceid",
    "x-trace-id",
    "trace-id",
    "traceid",
    "x-b3-traceid",
    "x-request-id",
    "request-id",
    "eagleeye-traceid",
    "x-amzn-trace-id",
)


def extract_infra_trace_id(headers) -> str:
    """
    从响应头中提取基础设施链路 ID。

    优先直接命中已知 header；否则解析 W3C traceparent
    （格式 00-<32hex traceid>-<16hex spanid>-01）和 SkyWalking 的 sw8。
    返回空串表示未找到。
    """
    if not headers:
        return ""

    # 统一小写 key（requests 的 headers 本身大小写不敏感，但 dict 不是）
    lower = {str(k).lower(): v for k, v in dict(headers).items()}

    for name in TRACE_ID_HEADERS:
        value = lower.get(name)
        if value:
            return str(value).strip()

    # W3C traceparent
    traceparent = lower.get("traceparent", "")
    if traceparent:
        parts = str(traceparent).split("-")
        if len(parts) >= 2 and parts[1]:
            return parts[1]

    # SkyWalking sw8: 第 2 段是 base64 编码的 trace id
    sw8 = lower.get("sw8", "")
    if sw8:
        parts = str(sw8).split("-")
        if len(parts) >= 2 and parts[1]:
            import base64
            try:
                return base64.b64decode(parts[1] + "==").decode("utf-8", "ignore")
            except Exception:
                return parts[1]

    return ""


@dataclass
class SkillTrace:
    """Skill 执行轨迹"""
    skill_name: str = ""
    skill_key: str = ""
    status: str = ""  # pending / complete
    duration_ms: int = 0
    is_slow: bool = False
    match_method: str = ""  # semantic / exact
    exec_mode: str = ""  # fork / inline


@dataclass
class ToolTrace:
    """Tool 调用轨迹"""
    tool_name: str = ""
    tool_label: str = ""
    execution_type: str = ""  # skill / tool
    status: str = ""  # pending / complete / error
    duration_ms: int = 0
    is_slow: bool = False
    run_id: str = ""
    arguments: dict = field(default_factory=dict)  # Tool 调用参数
    result: Optional[dict] = None  # Tool 返回结果
    error: Optional[str] = None  # Tool 执行错误信息


@dataclass
class BusinessResult:
    """业务执行结果"""
    task_id: str = ""
    object_apikey: str = ""  # contact / customer 等
    entity_type_apikey: str = ""
    is_created: bool = False
    created_record_id: Optional[int] = None
    is_checkin: int = 0
    entity_info: dict = field(default_factory=dict)
    extracted_fields: list = field(default_factory=list)  # 抽取到的字段列表
    message: str = ""


@dataclass
class AgentTrace:
    """Agent 完整执行轨迹"""
    run_id: str = ""
    thread_id: str = ""
    conversation_id: str = ""  # 后端分配的会话 ID
    current_message_id: int = 0  # 当前消息 ID
    skills: list = field(default_factory=list)  # 本次执行观察到的全部 Skill（按出现顺序）
    trace_url: str = ""  # AI 应用侧 trace 路径（如 /traces/7c0e9b3c）
    infra_trace_id: str = ""  # 基础设施链路 ID（来自响应头，用于日志平台检索）
    response_headers: dict = field(default_factory=dict)  # Agent 请求的响应头（诊断用）
    started_at_ms: int = 0  # 本轮请求发起时间（epoch ms，用于日志平台时间窗）
    finished_at_ms: int = 0  # 本轮读流结束时间（epoch ms）
    skill: Optional[SkillTrace] = None
    tools: list = field(default_factory=list)  # List[ToolTrace]
    business_result: Optional[BusinessResult] = None
    reply_text: str = ""  # Agent 最终回复文本
    total_duration_ms: int = 0
    cost_credits: float = 0.0
    agent_state: dict = field(default_factory=dict)  # Agent state（传递到下一轮）
    events_raw: list = field(default_factory=list)  # 原始事件（调试用）
    finished: bool = False  # 是否收到 RUN_FINISHED
    incomplete: bool = False  # 是否因超时/EOF 而不完整
    parse_errors: list = field(default_factory=list)  # JSON 解析错误记录


class SSEParser:
    """解析 SSE 流，提取结构化 Agent Trace"""

    def parse(self, lines, timeout_seconds: int = 120) -> AgentTrace:
        """
        解析 SSE 行流，返回结构化的 AgentTrace。

        Args:
            lines: 可迭代的 SSE 行（bytes 或 str）
            timeout_seconds: 单轮最大等待时间（秒），超时自动停止

        Returns:
            AgentTrace 对象
        """
        trace = AgentTrace()
        current_event_type = None
        start_time = time.time()

        for line in lines:
            # 超时检查
            if time.time() - start_time > timeout_seconds:
                trace.reply_text += " [TIMEOUT: 单轮超时]"
                trace.incomplete = True
                break

            if not line:
                continue

            # 解码
            if isinstance(line, bytes):
                text = line.decode("utf-8")
            else:
                text = line

            text = text.strip()

            # 解析 event: 行
            if text.startswith("event:"):
                current_event_type = text.replace("event:", "").strip()
                continue

            # 解析 data: 行
            if text.startswith("data:"):
                data_str = text[5:].strip()
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError as e:
                    trace.parse_errors.append({
                        "event": current_event_type,
                        "raw": data_str[:200],
                        "error": str(e),
                    })
                    continue

                trace.events_raw.append({
                    "event": current_event_type,
                    "data": data
                })

                self._process_event(trace, current_event_type, data)

                # 收到 RUN_FINISHED 后停止读取（避免连接不关闭导致阻塞）
                if data.get("type") == "RUN_FINISHED":
                    trace.finished = True
                    break

        # 如果没收到 RUN_FINISHED 且不是超时，标记为不完整
        if not trace.finished and not trace.incomplete:
            trace.incomplete = True

        return trace

    def _process_event(self, trace: AgentTrace, event_type: str, data: dict):
        """根据事件类型分发处理"""
        data_type = data.get("type", "")

        if data_type == "RUN_STARTED":
            self._handle_run_started(trace, data)
        elif data_type == "RUN_FINISHED":
            self._handle_run_finished(trace, data)
        elif data_type in ("ACTIVITY_SNAPSHOT", "ACTIVITY_DELTA"):
            self._handle_activity(trace, data)
        elif data_type == "TEXT_MESSAGE_CONTENT":
            self._handle_text_content(trace, data)
        elif data_type == "STATE_SNAPSHOT":
            # CopilotKit 可能通过此事件推送 state
            state = data.get("snapshot", data.get("state", {}))
            if state:
                trace.agent_state = state
        elif data_type == "STATE_DELTA":
            # 增量 state 更新
            delta = data.get("delta", {})
            if delta:
                trace.agent_state.update(delta)

    def _handle_run_started(self, trace: AgentTrace, data: dict):
        trace.run_id = data.get("run_id", "")
        trace.thread_id = data.get("thread_id", "")
        trace.conversation_id = data.get("conversation_id", "")
        trace.current_message_id = data.get("current_message_id", 0)
        trace.trace_url = data.get("trace_url", "")

    def _handle_run_finished(self, trace: AgentTrace, data: dict):
        trace.cost_credits = data.get("message_cost_credits", 0.0)
        # 提取 state（供多轮传递）
        state = data.get("state", {})
        if state:
            trace.agent_state = state

    def _handle_activity(self, trace: AgentTrace, data: dict):
        """处理 ACTIVITY_SNAPSHOT 和 ACTIVITY_DELTA"""
        # ACTIVITY_SNAPSHOT 包含完整的组件状态
        content = data.get("content", {})
        if content:
            self._extract_from_content(trace, content)

        # ACTIVITY_DELTA 包含 JSON Patch
        patches = data.get("patch", [])
        for patch in patches:
            value = patch.get("value", {})
            if isinstance(value, dict) and "component" in value:
                self._extract_from_component(trace, value.get("id", ""), value["component"])

    def _extract_from_content(self, trace: AgentTrace, content: dict):
        """从 ACTIVITY_SNAPSHOT content 中提取信息"""
        operations = content.get("operations", [])
        for op in operations:
            surface_update = op.get("surfaceUpdate", {})
            components = surface_update.get("components", [])
            for comp in components:
                comp_id = comp.get("id", "")
                component = comp.get("component", {})
                self._extract_from_component(trace, comp_id, component)

    def _extract_from_component(self, trace: AgentTrace, comp_id: str, component: dict):
        """从组件数据中提取 Skill/Tool/Business 信息"""

        # 提取 Skill 执行过程
        if "ThinkingExecutionProcess" in component:
            process = component["ThinkingExecutionProcess"]
            skill = SkillTrace(
                skill_name=process.get("skillName", ""),
                skill_key=process.get("skillKey", ""),
                status=process.get("status", ""),
                duration_ms=process.get("totalDurationMs", 0),
                match_method=process.get("match_method", ""),
                exec_mode=process.get("exec_mode", ""),
            )
            trace.skill = skill
            trace.total_duration_ms = skill.duration_ms
            # 同时记进 skills：一次执行可能触发多个 Skill（意图切换 / 跨 Skill 编排），
            # 只保留一个会让「先查询再录入」这类用例无法评测
            self._upsert_skill(trace, skill)

        # 提取 Tool 调用
        if "ThinkingExecutionProcessTool" in component:
            tool_data = component["ThinkingExecutionProcessTool"]
            tool = ToolTrace(
                tool_name=tool_data.get("toolName", ""),
                tool_label=tool_data.get("toolLabel", ""),
                execution_type=tool_data.get("executionType", ""),
                status=tool_data.get("status", ""),
                duration_ms=tool_data.get("durationMs", 0),
                is_slow=tool_data.get("slow", False) if isinstance(tool_data.get("slow"), bool) else tool_data.get("slow") == "slow",
                run_id=tool_data.get("toolRunId", ""),
                arguments=tool_data.get("arguments", {}),
                result=tool_data.get("result"),
                error=tool_data.get("error"),
            )
            # 更新或添加 tool（通过 run_id 去重）
            self._upsert_tool(trace, tool)

        # 提取业务结果（FallbackDataViewer）
        if "FallbackDataViewer" in component:
            viewer = component["FallbackDataViewer"]
            biz_data = viewer.get("data", {})
            if biz_data:
                result = BusinessResult(
                    task_id=biz_data.get("task_id", ""),
                    object_apikey=biz_data.get("object_apikey", ""),
                    entity_type_apikey=biz_data.get("entity_type_apikey", ""),
                    is_created=biz_data.get("isCreated", False),
                    created_record_id=biz_data.get("created_record_id"),
                    is_checkin=biz_data.get("isCheckin", 0),
                    entity_info=biz_data.get("entityInfo", {}),
                    extracted_fields=biz_data.get("extracted", []),
                    message=biz_data.get("message", ""),
                )
                trace.business_result = result

    @staticmethod
    def _upsert_skill(trace: AgentTrace, skill: SkillTrace):
        """按 skill_key 更新或追加（同一 Skill 的多次状态推送取最新）"""
        for i, existing in enumerate(trace.skills):
            if existing.skill_key == skill.skill_key:
                trace.skills[i] = skill
                return
        trace.skills.append(skill)

    def _upsert_tool(self, trace: AgentTrace, tool: ToolTrace):
        """更新或插入 Tool（同一个 toolRunId 的取最新状态）"""
        for i, existing in enumerate(trace.tools):
            if existing.run_id == tool.run_id:
                trace.tools[i] = tool
                return
        trace.tools.append(tool)

    def _handle_text_content(self, trace: AgentTrace, data: dict):
        """累积 Agent 回复文本"""
        delta = data.get("delta", "")
        trace.reply_text += delta
