"""
用例 Schema 校验（Pydantic v2）

存在的理由：用例里曾长期存在「哑字段」——写用例的人以为加了断言，但 Python
侧从未读取，断言静默失效。`extra="forbid"` 让这类拼错或未支持的字段在加载阶段
就报错，而不是安静地不生效。

已支持的断言字段：
    turns[].expected.text_assertions   all / any / not_any / iany
    turns[].expected.state_assertion   fields / absent（轮次级状态断言）
    evaluation.fields                  字段值断言（P/R/F1）
    evaluation.field_constraints       字段存在性约束（absent / present）

已移除：
    evaluation.expected_business_state  语义与 business_success 重叠，不再支持
"""

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class StrictModel(BaseModel):
    """禁止未知字段，避免哑字段静默失效"""
    model_config = ConfigDict(extra="forbid")


class TextAssertions(StrictModel):
    """文本断言"""
    all: List[str] = Field(default_factory=list, description="全部关键词都必须出现")
    any: List[str] = Field(default_factory=list, description="至少一个关键词出现")
    not_any: List[str] = Field(default_factory=list, description="全部关键词都不得出现")
    iany: List[str] = Field(default_factory=list, description="忽略大小写，至少一个出现")


class StateAssertion(StrictModel):
    """
    轮次级状态断言：检查这一轮结束时 Agent 已抽取的字段。

    fields: 必须存在且值匹配的字段
    absent: 必须不存在（或已被删除）的字段
    """
    fields: Dict[str, Any] = Field(default_factory=dict)
    absent: List[str] = Field(default_factory=list)


class TurnExpected(StrictModel):
    """单轮的期望"""
    text_assertions: Optional[TextAssertions] = None
    state_assertion: Optional[StateAssertion] = None


class Turn(StrictModel):
    """一轮对话"""
    turn_id: str = ""
    input: str
    expected: TurnExpected = Field(default_factory=TurnExpected)


class ExpectedToolCall(StrictModel):
    """Tool 调用期望（含参数级校验）"""
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    status: Optional[str] = None


class Evaluation(StrictModel):
    """用例级评测期望（五层）"""
    skill: str = ""
    expected_skills: List[str] = Field(default_factory=list)
    tools_required: List[str] = Field(default_factory=list)
    tools_forbidden: List[str] = Field(default_factory=list)
    expected_tool_calls: List[ExpectedToolCall] = Field(default_factory=list)
    object_type: str = ""
    business_success: Union[bool, Literal["pending"]] = True
    require_business_grounding: bool = False
    fields: Dict[str, Any] = Field(default_factory=dict)
    field_constraints: Dict[str, Literal["absent", "present"]] = Field(default_factory=dict)
    text_assertions: Optional[TextAssertions] = None

    @model_validator(mode="after")
    def _check_business_grounding(self) -> "Evaluation":
        """强制业务回查只适用于期望创建成功的最终态用例。"""
        if self.require_business_grounding and self.business_success is not True:
            raise ValueError(
                "require_business_grounding=true 时 business_success 必须为 true"
            )
        return self


class CaseMetadata(BaseModel):
    """用例元信息（允许自由扩展，仅用于报告分组统计）"""
    model_config = ConfigDict(extra="allow")

    scenario: str = ""
    priority: str = ""
    difficulty: str = ""
    tags: List[str] = Field(default_factory=list)


class Case(StrictModel):
    """单个测试用例"""
    id: str
    name: str = ""
    type: Literal["single", "multi", "multi_turn", "confirmation"] = "single"
    metadata: CaseMetadata = Field(default_factory=CaseMetadata)
    turns: List[Turn] = Field(default_factory=list)
    evaluation: Evaluation = Field(default_factory=Evaluation)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_expected(cls, data: Any) -> Any:
        """兼容旧格式：顶层 expected → evaluation"""
        if isinstance(data, dict) and "expected" in data:
            data = dict(data)
            legacy = data.pop("expected")
            data.setdefault("evaluation", legacy)
        return data

    @model_validator(mode="after")
    def _check_turns(self) -> "Case":
        if not self.turns:
            raise ValueError("turns 不能为空")
        if self.type == "single" and len(self.turns) != 1:
            raise ValueError(f"type=single 的用例应只有 1 轮，实际 {len(self.turns)} 轮")
        return self


class CaseValidationError(Exception):
    """用例校验失败，携带所有错误明细"""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def validate_cases(raw_cases: List[dict]) -> List[Case]:
    """
    校验一批用例，任一不合法即抛 CaseValidationError（一次性报告所有问题）。

    返回解析后的 Case 列表；调用方仍可继续用原始 dict 执行，
    Case 对象主要用于取出结构化的断言。
    """
    parsed: List[Case] = []
    errors: List[str] = []

    for i, raw in enumerate(raw_cases):
        case_id = raw.get("id") if isinstance(raw, dict) else None
        label = case_id or f"#{i}"
        try:
            parsed.append(Case.model_validate(raw))
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"]) or "<root>"
                errors.append(f"  [{label}] {loc}: {err['msg']}")

    if errors:
        raise CaseValidationError(errors)

    # ID 重复检查：重复 ID 会让回归对比按 case_id 匹配时错位
    seen = {}
    for case in parsed:
        seen[case.id] = seen.get(case.id, 0) + 1
    duplicates = [cid for cid, n in seen.items() if n > 1]
    if duplicates:
        raise CaseValidationError([f"  用例 ID 重复: {duplicates}"])

    return parsed
