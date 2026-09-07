"""
SSE Parser 属性测试（Property-based）

不变量：
1. Parser 不能 crash（任何输入）
2. 完整流（有 RUN_FINISHED）必须 finished=True
3. 缺少 RUN_FINISHED 必须 incomplete=True
4. 同一 toolRunId 状态只能正确更新对应 Tool
5. 非法 JSON 必须留下 parse_error，而不是静默丢失
6. 空 data 行不崩溃

覆盖场景：
- 正常事件序列: START → TOOL_START → TOOL_END → RUN_FINISHED
- 缺失事件: START → TOOL_START → EOF
- 重复事件: START → TOOL_START → TOOL_START → TOOL_END
- 乱序事件: TOOL_END → TOOL_START
- 非法 JSON
- 空 data
- 重复 toolRunId
"""
import pytest
from runner.sse_parser import SSEParser, AgentTrace


@pytest.fixture
def parser():
    return SSEParser()


# ============================================================
# 辅助函数：构造 SSE 行
# ============================================================
def _sse_run_started(run_id="run-001", thread_id="thread-001"):
    return [
        "event: RUN_STARTED",
        f'data: {{"type":"RUN_STARTED","run_id":"{run_id}","thread_id":"{thread_id}"}}',
    ]


def _sse_tool(tool_name, tool_run_id, status="complete", duration_ms=1000, arguments=None):
    args_json = ""
    if arguments:
        import json
        args_json = f',"arguments":{json.dumps(arguments)}'
    return [
        "event: ACTIVITY_SNAPSHOT",
        f'data: {{"type":"ACTIVITY_SNAPSHOT","messageId":"a2ui-thinking","activityType":"a2ui-surface","content":{{"operations":[{{"surfaceUpdate":{{"surfaceId":"thinking-step","components":[{{"id":"{tool_run_id}","component":{{"ThinkingExecutionProcessTool":{{"status":"{status}","toolName":"{tool_name}","toolLabel":"{tool_name}","toolRunId":"{tool_run_id}","executionType":"tool","durationMs":{duration_ms}{args_json}}}}}}}]}}}}]}}}}',
    ]


def _sse_run_finished(run_id="run-001", credits=10.0):
    return [
        "event: RUN_FINISHED",
        f'data: {{"type":"RUN_FINISHED","run_id":"{run_id}","message_cost_credits":{credits}}}',
    ]


def _sse_text(delta):
    return [
        "event: TEXT_MESSAGE_CONTENT",
        f'data: {{"type":"TEXT_MESSAGE_CONTENT","message_id":"msg1","delta":"{delta}"}}',
    ]


# ============================================================
# 正常事件序列
# ============================================================
class TestNormalFlow:
    """START → TOOL → RUN_FINISHED"""

    def test_complete_flow_finished_true(self, parser):
        lines = (
            _sse_run_started()
            + _sse_tool("extract-data", "run-tool-1")
            + _sse_tool("save_record", "run-tool-2")
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert trace.finished is True
        assert trace.incomplete is False
        assert trace.run_id == "run-001"
        assert len(trace.tools) == 2

    def test_text_accumulation(self, parser):
        lines = (
            _sse_run_started()
            + _sse_text("你好")
            + _sse_text("世界")
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert trace.reply_text == "你好世界"
        assert trace.finished is True


# ============================================================
# 缺失事件: START → TOOL_START → EOF
# ============================================================
class TestMissingEvents:
    """缺少 RUN_FINISHED → incomplete=True"""

    def test_no_run_finished_means_incomplete(self, parser):
        lines = (
            _sse_run_started()
            + _sse_tool("extract-data", "run-tool-1", status="pending")
        )
        trace = parser.parse(lines)
        assert trace.finished is False
        assert trace.incomplete is True

    def test_only_run_started(self, parser):
        lines = _sse_run_started()
        trace = parser.parse(lines)
        assert trace.finished is False
        assert trace.incomplete is True
        assert trace.run_id == "run-001"

    def test_empty_input(self, parser):
        trace = parser.parse([])
        assert trace.finished is False
        assert trace.incomplete is True
        assert trace.run_id == ""
        assert trace.tools == []
        assert trace.parse_errors == []


# ============================================================
# 重复事件: 同一 tool 多次 snapshot
# ============================================================
class TestDuplicateEvents:
    """重复 tool snapshot 应通过 toolRunId 去重更新"""

    def test_duplicate_tool_updates_not_duplicates(self, parser):
        """同一 toolRunId 先 pending 后 complete，只保留最后一个"""
        lines = (
            _sse_run_started()
            + _sse_tool("extract-data", "run-tool-1", status="pending", duration_ms=0)
            + _sse_tool("extract-data", "run-tool-1", status="complete", duration_ms=5000)
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert len(trace.tools) == 1
        assert trace.tools[0].status == "complete"
        assert trace.tools[0].duration_ms == 5000

    def test_multiple_different_tools(self, parser):
        """不同 toolRunId 各自独立"""
        lines = (
            _sse_run_started()
            + _sse_tool("extract-data", "run-1")
            + _sse_tool("extract-data", "run-2")
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert len(trace.tools) == 2
        assert trace.tools[0].run_id == "run-1"
        assert trace.tools[1].run_id == "run-2"


# ============================================================
# 乱序事件: TOOL_END 在 TOOL_START 之前
# ============================================================
class TestOutOfOrder:
    """乱序不崩溃，tool 正常记录"""

    def test_tool_complete_before_start(self, parser):
        """直接收到 complete 的 tool（没有 pending），parser 正常处理"""
        lines = (
            _sse_tool("save_record", "run-tool-x", status="complete")
            + _sse_run_started()
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        # 不崩溃，tool 被记录
        assert len(trace.tools) == 1
        assert trace.tools[0].tool_name == "save_record"
        assert trace.tools[0].status == "complete"

    def test_run_finished_before_anything(self, parser):
        """RUN_FINISHED 作为第一个事件"""
        lines = _sse_run_finished()
        trace = parser.parse(lines)
        assert trace.finished is True
        assert trace.run_id == ""  # 没有 RUN_STARTED


# ============================================================
# 非法 JSON
# ============================================================
class TestInvalidJSON:
    """非法 JSON 必须记录到 parse_errors，不崩溃"""

    def test_invalid_json_records_error(self, parser):
        lines = [
            "event: RUN_STARTED",
            "data: {this is not valid json!!!}",
            "event: RUN_FINISHED",
            'data: {"type":"RUN_FINISHED","run_id":"x","message_cost_credits":1.0}',
        ]
        trace = parser.parse(lines)
        assert len(trace.parse_errors) == 1
        assert "error" in trace.parse_errors[0]
        assert trace.parse_errors[0]["event"] == "RUN_STARTED"
        # 后续事件仍正常处理
        assert trace.finished is True

    def test_multiple_invalid_json(self, parser):
        lines = [
            "event: FOO",
            "data: not json 1",
            "event: BAR",
            "data: not json 2",
            "event: BAZ",
            "data: not json 3",
        ]
        trace = parser.parse(lines)
        assert len(trace.parse_errors) == 3
        assert trace.incomplete is True

    def test_truncated_json(self, parser):
        lines = [
            "event: RUN_STARTED",
            'data: {"type":"RUN_STARTED","run_id":"abc',  # 截断
        ]
        trace = parser.parse(lines)
        assert len(trace.parse_errors) == 1
        assert trace.run_id == ""  # 没解析成功


# ============================================================
# 空 data 行
# ============================================================
class TestEmptyData:
    """空行和空 data 不崩溃"""

    def test_empty_data_field(self, parser):
        lines = [
            "event: RUN_STARTED",
            "data: ",  # 空 data
            "event: RUN_FINISHED",
            'data: {"type":"RUN_FINISHED","run_id":"x","message_cost_credits":0}',
        ]
        trace = parser.parse(lines)
        # 空字符串 json.loads 会失败 → parse_error
        assert len(trace.parse_errors) == 1
        assert trace.finished is True

    def test_only_empty_lines(self, parser):
        lines = ["", "", "", ""]
        trace = parser.parse(lines)
        assert trace.incomplete is True
        assert trace.parse_errors == []

    def test_data_with_only_whitespace(self, parser):
        lines = [
            "event: SOMETHING",
            "data:    ",
        ]
        trace = parser.parse(lines)
        assert len(trace.parse_errors) == 1


# ============================================================
# 重复 toolRunId 状态更新
# ============================================================
class TestToolRunIdUpsert:
    """同一 toolRunId 的多次更新只保留最新状态"""

    def test_pending_to_complete_upsert(self, parser):
        lines = (
            _sse_run_started()
            + _sse_tool("my_tool", "run-123", status="pending", duration_ms=0)
            + _sse_tool("my_tool", "run-123", status="complete", duration_ms=3000)
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert len(trace.tools) == 1
        assert trace.tools[0].status == "complete"
        assert trace.tools[0].duration_ms == 3000

    def test_complete_to_error_upsert(self, parser):
        """如果后续推送 error 状态，应更新"""
        lines = (
            _sse_run_started()
            + _sse_tool("my_tool", "run-456", status="complete", duration_ms=1000)
            + _sse_tool("my_tool", "run-456", status="error", duration_ms=1500)
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert len(trace.tools) == 1
        assert trace.tools[0].status == "error"

    def test_different_run_ids_stay_separate(self, parser):
        lines = (
            _sse_run_started()
            + _sse_tool("tool_a", "id-1", status="complete")
            + _sse_tool("tool_b", "id-2", status="complete")
            + _sse_tool("tool_a", "id-3", status="pending")
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert len(trace.tools) == 3


# ============================================================
# Tool arguments 解析
# ============================================================
class TestToolArguments:
    """验证 arguments 字段能正确从 SSE 解析"""

    def test_arguments_parsed(self, parser):
        lines = (
            _sse_run_started()
            + _sse_tool("create_contact", "run-arg-1", arguments={"name": "张三", "phone": "13800138000"})
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert trace.tools[0].arguments == {"name": "张三", "phone": "13800138000"}

    def test_no_arguments_defaults_empty(self, parser):
        lines = (
            _sse_run_started()
            + _sse_tool("simple_tool", "run-no-args")
            + _sse_run_finished()
        )
        trace = parser.parse(lines)
        assert trace.tools[0].arguments == {}


# ============================================================
# bytes 输入
# ============================================================
class TestBytesInput:
    """SSE 行可以是 bytes"""

    def test_bytes_lines(self, parser):
        lines = [line.encode("utf-8") for line in (
            _sse_run_started() + _sse_run_finished()
        )]
        trace = parser.parse(lines)
        assert trace.finished is True
        assert trace.run_id == "run-001"
