"""
AI Testing Agent - 用例生成流水线

三阶段 + 门禁：
    Requirement → Scenario Plan → Case → Deterministic Gate → cases/

只有通过 Deterministic Gate 的用例才会落盘，保证进入 AgentEval 的都是合法用例。
"""
