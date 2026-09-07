"""测试 SSEParser：用真实 SSE 数据验证解析逻辑"""
import pytest
from runner.sse_parser import SSEParser


# 真实 SSE 响应数据（基于实际抓包）
SAMPLE_SSE_LINES = [
    'event: RUN_STARTED',
    'data: {"type":"RUN_STARTED","run_id":"45fa95d2-81fc-4cf8-a352-ad64a6943a06","thread_id":"workbench-1785485051455-lm69hc","trace_url":"/traces/7c0e9b3c","current_message_id":4435490992545355}',
    '',
    'event: ACTIVITY_SNAPSHOT',
    'data: {"type":"ACTIVITY_SNAPSHOT","messageId":"a2ui-thinking","activityType":"a2ui-surface","content":{"operations":[{"surfaceUpdate":{"surfaceId":"thinking-step","components":[{"id":"thinking-step","component":{"ThinkingExecutionProcess":{"status":"complete","msg":"智能录入 执行完成","processKey":"step-b7f47daa","skillName":"extract-data","skillKey":"extract-data","toolCount":2,"totalDurationMs":11707,"match_method":"semantic","exec_mode":"fork","children":{"explicitList":["tool-1","tool-2"]}}}},{"id":"tool-1","component":{"ThinkingExecutionProcessTool":{"status":"complete","msg":"智能录入 调用完成","toolName":"extract-data","toolLabel":"智能录入","toolRunId":"skill-extract-data-3a7db572","executionType":"skill","durationMs":11704}}},{"id":"tool-2","component":{"ThinkingExecutionProcessTool":{"status":"complete","msg":"保存记录 调用完成","toolName":"save_record","toolLabel":"保存记录","toolRunId":"019fb73d-d6b3-79d0-add1-aaa92e0de658","executionType":"tool","durationMs":2733}}}]}},{"beginRendering":{"surfaceId":"thinking-step","root":"thinking-step"}}]}}',
    '',
    'event: ACTIVITY_SNAPSHOT',
    'data: {"type":"ACTIVITY_SNAPSHOT","messageId":"a2ui-biz","activityType":"a2ui-surface","content":{"operations":[{"surfaceUpdate":{"surfaceId":"chat-inline","components":[{"id":"fallback-data","component":{"FallbackDataViewer":{"messageId":"a2ui-biz","dataKey":"extract_form_data","data":{"task_id":"ext_02921277d233","object_apikey":"contact","entity_type_apikey":"defaultBusiType","isCreated":true,"created_record_id":4435491201785437,"isCheckin":0,"entityInfo":{"apikey":"contact","label":"联系人"},"extracted":[{"apikey":"contactName","name":"名称","label":"张三","value":"张三","type":"text"},{"apikey":"gender","name":"性别","label":"男","value":"1","type":"text"},{"apikey":"mobile","name":"手机","label":"13800138000","value":"13800138000","type":"text"},{"apikey":"email","name":"邮箱","label":"test@example.com","value":"test@example.com","type":"text"}],"message":"创建成功"}}}}]}},{"beginRendering":{"surfaceId":"chat-inline","root":"fallback-data"}}]}}',
    '',
    'event: TEXT_MESSAGE_CONTENT',
    'data: {"type":"TEXT_MESSAGE_CONTENT","message_id":"msg1","delta":"联系人张三已"}',
    '',
    'event: TEXT_MESSAGE_CONTENT',
    'data: {"type":"TEXT_MESSAGE_CONTENT","message_id":"msg1","delta":"创建成功！"}',
    '',
    'event: RUN_FINISHED',
    'data: {"type":"RUN_FINISHED","run_id":"45fa95d2-81fc-4cf8-a352-ad64a6943a06","thread_id":"workbench-1785485051455-lm69hc","message_cost_credits":17.66}',
]


@pytest.fixture
def parser():
    return SSEParser()


@pytest.fixture
def trace(parser):
    return parser.parse(SAMPLE_SSE_LINES)


class TestRunMetadata:
    def test_run_id(self, trace):
        assert trace.run_id == "45fa95d2-81fc-4cf8-a352-ad64a6943a06"

    def test_thread_id(self, trace):
        assert trace.thread_id == "workbench-1785485051455-lm69hc"

    def test_cost(self, trace):
        assert trace.cost_credits == 17.66


class TestSkillParsing:
    def test_skill_name(self, trace):
        assert trace.skill is not None
        assert trace.skill.skill_name == "extract-data"
        assert trace.skill.skill_key == "extract-data"

    def test_skill_status(self, trace):
        assert trace.skill.status == "complete"

    def test_skill_duration(self, trace):
        assert trace.skill.duration_ms == 11707

    def test_skill_match_method(self, trace):
        assert trace.skill.match_method == "semantic"


class TestToolParsing:
    def test_tool_count(self, trace):
        assert len(trace.tools) == 2

    def test_first_tool_is_skill(self, trace):
        tool = trace.tools[0]
        assert tool.tool_name == "extract-data"
        assert tool.execution_type == "skill"
        assert tool.duration_ms == 11704

    def test_second_tool_is_save_record(self, trace):
        tool = trace.tools[1]
        assert tool.tool_name == "save_record"
        assert tool.execution_type == "tool"
        assert tool.duration_ms == 2733


class TestBusinessResult:
    def test_is_created(self, trace):
        assert trace.business_result is not None
        assert trace.business_result.is_created is True

    def test_record_id(self, trace):
        assert trace.business_result.created_record_id == 4435491201785437

    def test_object_type(self, trace):
        assert trace.business_result.object_apikey == "contact"

    def test_message(self, trace):
        assert trace.business_result.message == "创建成功"

    def test_extracted_fields(self, trace):
        fields = trace.business_result.extracted_fields
        assert len(fields) == 4

        # 验证字段内容
        field_map = {f["apikey"]: f for f in fields}
        assert "contactName" in field_map
        assert field_map["contactName"]["value"] == "张三"
        assert field_map["gender"]["value"] == "1"
        assert field_map["mobile"]["value"] == "13800138000"
        assert field_map["email"]["value"] == "test@example.com"


class TestTextReply:
    def test_reply_text_concatenation(self, trace):
        assert trace.reply_text == "联系人张三已创建成功！"


class TestEmptyInput:
    def test_empty_lines(self, parser):
        trace = parser.parse([])
        assert trace.run_id == ""
        assert trace.skill is None
        assert trace.tools == []
        assert trace.business_result is None
        assert trace.reply_text == ""

    def test_invalid_json(self, parser):
        lines = [
            "event: RUN_STARTED",
            "data: {invalid json}",
        ]
        trace = parser.parse(lines)
        assert trace.run_id == ""  # 解析失败不崩溃
