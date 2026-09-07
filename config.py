"""
AgentEval 配置文件

认证优先级：
1. 环境变量 AGENT_TOKEN / AGENT_COOKIE
2. credentials.yaml 文件
"""

import os

# Agent 服务地址
AGENT_BASE_URL = "https://crm-cd.xiaoshouyi.com"
AGENT_RUN_PATH = "/rest/data/v2.0/ai/agui/copilotkit/agent/default/run"
AGENT_URL = AGENT_BASE_URL + AGENT_RUN_PATH

# 会话历史列表接口。后端不在 RUN_STARTED 里返回 conversation_id，
# 会话是首个 run 请求隐式创建的，因此首轮结束后靠这个接口反查会话 ID。
CONVERSATION_HISTORY_PATH = "/rest/data/v2.0/ai/conversation/query-history"
CONVERSATION_HISTORY_URL = AGENT_BASE_URL + CONVERSATION_HISTORY_PATH
AGENT_API_KEY = "default"

# 会话发现配置
CONVERSATION_DISCOVERY_ENABLED = True
CONVERSATION_DISCOVERY_RETRIES = 3      # 会话可能异步落库，重试几次
CONVERSATION_DISCOVERY_DELAY = 1.5      # 每次重试间隔（秒）
CONVERSATION_HISTORY_PAGE_SIZE = 10
# 用「会话 createdAt >= 首轮发起时间」过滤，避免抓到浏览器里手开的会话。
# 缓冲值要能覆盖本地时钟与服务端时钟的偏差。
CONVERSATION_MATCH_BUFFER_MS = 60_000

# --- 认证 ---
# 延迟加载 Cookie（首次访问 HEADERS 时自动登录）
def _get_headers() -> dict:
    """构建请求头，自动获取 Cookie"""
    from auth.browser_login import get_cookie

    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    # 环境变量优先
    token = os.getenv("AGENT_TOKEN", "")
    cookie = os.getenv("AGENT_COOKIE", "")

    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif cookie:
        headers["Cookie"] = cookie
    else:
        # 自动登录获取 cookie
        headers["Cookie"] = get_cookie()

    return headers


# 懒加载：在 AgentRunner 初始化时调用
HEADERS = None  # 由 get_headers() 动态生成


def get_headers() -> dict:
    """获取带认证信息的请求头"""
    global HEADERS
    if HEADERS is None:
        HEADERS = _get_headers()
    return HEADERS

# 超时配置（秒）
REQUEST_TIMEOUT = 60
STREAM_READ_TIMEOUT = 180
SSE_TURN_TIMEOUT = 240  # 单轮 SSE 读取超时（Agent 不返回 RUN_FINISHED 时生效）

# 重试配置
MAX_RETRIES = 2
RETRY_DELAY = 3  # 秒

# --- 链路追踪 / 日志平台 ---
# 日志平台（appLogViewer）：按 envCode + appName + 时间窗 + 关键字检索。
# 注意它的 traceId（t.<hex>.<seq>.<epoch>）与 SSE 的 trace_url（Langfuse 系）不同源，
# 无法直接映射；关联靠 run_id / message_id 关键字（后端日志正文里原样出现）。
LOG_PLATFORM_BASE_URL = "https://kt.sre.ingageapp.com"
LOG_PLATFORM_ENV_CODE = "crm-cd"  # 与 AGENT_BASE_URL 的环境保持一致
LOG_PLATFORM_APP_NAME = "neo-apps-ai-agent-service"
LOG_PLATFORM_LIMIT = 1000
# 生成日志链接时在请求时间窗两侧各留的缓冲（毫秒），避免边界日志被截掉
LOG_TIME_PADDING_MS = 30_000
# 首轮 dump Agent 响应头（infra trace id 的备用来源，非必需路径）
DUMP_RESPONSE_HEADERS = os.getenv("DUMP_RESPONSE_HEADERS", "1") == "1"


def build_log_viewer_url(started_at_ms: int, finished_at_ms: int = 0,
                         keyword: str = "", env_code: str = None,
                         app_name: str = None, limit: int = None) -> str:
    """
    构建日志平台深链。

    参数格式已按平台实际 URL 校准：
        #/logManage/appLogViewer?envCode=&appName=&keyword=&limit=&startTime=&endTime=
    keyword 会被 URL 编码（中文关键字同样适用）。
    """
    import urllib.parse

    start = max(0, started_at_ms - LOG_TIME_PADDING_MS)
    end = (finished_at_ms or started_at_ms) + LOG_TIME_PADDING_MS

    params = {
        "envCode": env_code or LOG_PLATFORM_ENV_CODE,
        "appName": app_name or LOG_PLATFORM_APP_NAME,
        "limit": limit or LOG_PLATFORM_LIMIT,
        "startTime": start,
        "endTime": end,
    }
    if keyword:
        params["keyword"] = keyword

    query = urllib.parse.urlencode(params)
    return f"{LOG_PLATFORM_BASE_URL}/#/logManage/appLogViewer?{query}"


# --- Database Check ---
# 关闭后 L5 只看 Agent 自己声称的结果，不再查 CRM 验证。
# 离线回放（--replay）会自动关掉它；单测用 conftest 里的 autouse fixture 关掉，
# 避免评测单测意外发出真实网络请求。
DB_CHECK_ENABLED = os.getenv("AGENTEVAL_DB_CHECK", "1") == "1"

# 测试用例目录
CASES_DIR = "cases"

# 报告输出目录
REPORTS_DIR = "reports"

# 评测阈值
THRESHOLDS = {
    "skill_accuracy": 0.95,      # Skill 路由准确率阈值
    "tool_accuracy": 0.95,       # Tool 调用准确率阈值
    "field_f1": 0.90,            # 字段抽取 F1 阈值
    "business_success": 0.90,    # 业务成功率阈值
    "slow_call_ms": 10000,       # 慢调用阈值（毫秒）
}


# --- AI Testing Agent（testgen 用例生成流水线）---
# OpenAI 兼容接口：Requirement → Scenario Plan → Case → Deterministic Gate。
# key 优先取环境变量，其次 credentials.yaml 里的 llm_api_key。
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
# requests 使用 (connect timeout, read timeout) 二元组：连接失败尽快返回，
# 模型生成则允许更长等待。LLM_TIMEOUT 保留原环境变量名，表示 read timeout。
LLM_CONNECT_TIMEOUT = int(os.getenv("LLM_CONNECT_TIMEOUT", "30"))
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "240"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.4"))


def get_llm_api_key() -> str:
    """
    取 LLM API key：环境变量 LLM_API_KEY 优先，其次 credentials.yaml 的 llm_api_key。
    返回空串表示未配置（调用方应据此报错，而不是发一次注定 401 的请求）。
    """
    key = os.getenv("LLM_API_KEY", "")
    if key:
        return key
    try:
        import yaml
        with open("credentials.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return str(data.get("llm_api_key", "") or "")
    except (OSError, ImportError):
        return ""
