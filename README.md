# AgentEval

**面向 CRM「智能录入」Agent 的五层自动化评测系统**

从用户的一句自然语言，一路验证到 CRM 真实落库——覆盖 Skill 路由、Tool 调用、字段抽取、业务结果与多轮状态。既能连真实 CRM 环境执行，也能用脱敏 SSE 离线回放，后者不依赖内网、账号或 CRM 权限，可直接进 CI。

<p>
  <img alt="python" src="https://img.shields.io/badge/python-3.10+-blue.svg">
  <img alt="tests" src="https://img.shields.io/badge/tests-pytest-green.svg">
  <img alt="mode" src="https://img.shields.io/badge/mode-live%20%7C%20offline%20replay-orange.svg">
</p>

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [五层评测体系](#五层评测体系)
- [30 秒上手](#30-秒上手)
- [核心能力](#核心能力)
- [CLI 速查](#cli-速查)
- [Case Schema](#case-schema)
- [录制、脱敏与回放](#录制脱敏与回放)
- [BadCase → 回归集](#badcase--回归集)
- [AI 用例生成](#ai-用例生成testgen)
- [质量门禁与退出码](#质量门禁与退出码)
- [回归对比](#回归对比)
- [报告](#报告)
- [项目结构](#项目结构)
- [开发与测试](#开发与测试)

---

## 它解决什么问题

评测一个「会话式录入」Agent 的难点在于：一次对话里既有**技能路由**、又有**工具编排**、还要**抽准字段**、最终**真的把记录建出来**，多轮之间还要**状态自洽**。任何一环出错，笼统的"通过/失败"都说不清坏在哪。

AgentEval 把这条链路拆成五层独立判定，每层给出结构化结论并**按层归因**，让一次失败能直接定位到「是路由错了、还是字段抽漏了、还是没落库」，产出可直接提 bug 的结论。

## 五层评测体系

| 层级 | 评测内容 | 数据来源 | 判定方式 |
|---|---|---|---|
| **L1 Skill 路由** | 必需 Skill 是否全部执行 | `trace.skills[].skill_key` | `skill` 与 `expected_skills` 的并集是实际 Skill 集合的**子集**（顺序无关） |
| **L2 Tool 调用** | Tool 名称、参数与状态是否正确 | `toolName` / `arguments` / `status` | 必须、禁止及参数子集校验 |
| **L3 字段抽取** | 结构化字段是否准确 | `extracted_fields` | Precision / Recall / **F1**、字段存在性约束 |
| **L4 业务结果** | 任务是否真正完成 | `isCreated` / `created_record_id` / CRM 查询接口 | 创建状态、对象类型及落库字段校验 |
| **L5 轮次状态** | 多轮补充、改写和删除后的状态是否一致 | 每轮 `business_result.extracted_fields` | `state_assertion.fields` / `state_assertion.absent` |

L1–L4 是用例级基础层；L5 在用例声明轮次状态断言时追加，并参与用例总体成败判定。

### 多轮 Skill 判定

每轮 SSE 可能产生多个 Skill，Parser 记录到 `trace.skills`，多轮合并时按 `skill_key` 去重并保留首次出现顺序。用例可同时声明单个必需 Skill 与多个必需 Skill，两者取**并集**：

```json
{
  "skill": "extract-data",
  "expected_skills": ["crm_query"]
}
```

上例要求实际链路中同时出现 `crm_query` 与 `extract-data`，顺序不影响判定。判定是「期望 ⊆ 实际观察集」而非「等于第一个 Skill」——因为意图切换、跨 Skill 编排（先查客户再建联系人）天然会经过多个 Skill。

> 若某个 negative case 中 Agent 选择纯对话追问、不进入任何 Skill 是合理行为，可把 `skill` 与 `expected_skills` **都置空**，此时 L1 判为「无 Skill 断言」直接通过。

## 30 秒上手

```bash
# 1. 环境
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate         # Linux / macOS
python -m pip install -r requirements.txt

# 2. 单元测试
python -m pytest

# 3. 离线 Demo（不发网络请求，自动跳过 CRM 回查）
python main.py --replay fixtures/replay/demo --case DEMO_PASS --no-report
```

跑真实用例前，复制凭证模板并填写：

```bash
cp credentials.example.yaml credentials.yaml   # 填账号/密码/租户
python main.py --file cases/contact_create.json
python main.py --case CREATECONTACT006 --tag prompt-v2
```

> `credentials.yaml`、`.cookie_cache.json` 及原始录制数据**不得提交到 Git**。

## 核心能力

| 能力 | 说明 |
|---|---|
| **五层评测** | Skill / Tool / 字段 / 业务 / 轮次状态，逐层独立判定 |
| **双执行模式** | 真实 CRM 执行；或脱敏 SSE 离线回放（无需内网/凭证，适配 CI） |
| **失败归因** | 按层自动分类（路由 / 工具 / 字段缺失·值错·多抽 / 业务未建 / 状态不一致），输出可提 bug 的结论 |
| **质量门禁** | Skill、Tool、字段 F1、业务成功率四项阈值判定，非零退出卡 CI |
| **回归对比** | baseline ↔ candidate 两份报告对比，识别新增失败与恢复用例 |
| **稳定性分析** | `--repeat N` 连跑多遍，统计 pass 翻转率与 F1/延迟波动 |
| **性能分析** | 延迟分位、成本、Tool 级耗时（含 P99）、慢调用告警 |
| **AI 用例生成** | `testgen` 流水线：需求 → 场景规划 → 用例 → 确定性门禁 |
| **BadCase 回归集** | 真实缺陷脱敏沉淀，`known`（不阻断）→ `fixed`（阻断）状态机 |
| **一致性脱敏** | `scripts/sanitize_replay.py` 跨用例文件与逐字 delta 分片同值脱敏 |

## CLI 速查

`python main.py [选项]`

| 选项 | 说明 |
|---|---|
| `--file <path>` | 指定用例文件（默认读 `cases/`；回放模式默认取回放目录下 `cases.json`） |
| `--case <ID>` | 只跑指定用例 |
| `--replay <DIR>` | 离线回放：从录制目录读 SSE，不发网络请求，自动关 DB Check |
| `--record <DIR>` | 录制：把每轮原始 SSE 存到该目录，供 `--replay` 复现 |
| `--tag <str>` | 报告标签（如模型/Prompt 版本），体现在报告文件名与内容 |
| `--repeat <N>` | 同批用例连跑 N 遍，输出稳定性分析 |
| `--dump-fields` | 打印每轮实际抽取字段（apikey/value/label），用于校准断言 |
| `--compare <A> <B>` | 回归对比两份 JSON 报告 |
| `--report-dir <DIR>` | 报告输出目录（默认 `reports/`） |
| `--no-report` | 不保存报告文件 |
| `--no-gate` | 跳过质量门禁判定（始终返回 0） |
| `--skip-validate` | 跳过用例 Schema 校验 |

## Case Schema

用例在执行前经过 Pydantic v2 校验：未知字段、重复 ID、空轮次及不合法类型会直接返回用法错误（退出码 3）。

需要把 CRM 业务记录回查作为**硬性**通过条件时，在最终态用例中声明：

```json
{
  "business_success": true,
  "require_business_grounding": true
}
```

启用后，L4 只有在记录存在且落库字段正确时才通过；记录不存在、字段不一致、查询失败、DB Check 被关闭或无法执行都会使 L4 失败。JSON 报告的 `layer_details` 会保留 `DB Check: 验证通过` 或具体失败原因，避免只看到 `L4=true` 却无法确认是否真的完成回查。离线回放不配置该字段并自动关闭 DB Check，因此仍可在无内网、无凭证环境运行。

<details>
<summary>完整用例示例（先查询再创建联系人）</summary>

```json
[
  {
    "id": "CREATECONTACT_DEMO",
    "name": "先查询再创建联系人",
    "type": "multi_turn",
    "turns": [
      {
        "turn_id": "T01",
        "input": "先帮我查一下某客户",
        "expected": { "state_assertion": { "fields": {} } }
      },
      {
        "turn_id": "T02",
        "input": "给这个客户创建联系人陈鑫",
        "expected": { "state_assertion": { "fields": { "contactName": "陈鑫" } } }
      }
    ],
    "evaluation": {
      "expected_skills": ["crm_query", "extract-data"],
      "expected_tool_calls": [
        { "name": "save_record", "arguments": { "contactName": "陈鑫" }, "status": "complete" }
      ],
      "object_type": "contact",
      "business_success": true,
      "require_business_grounding": true,
      "fields": { "contactName": "陈鑫" }
    }
  }
]
```

</details>

> **negative case 写法**：对「缺必填字段应阻止创建」这类用例，只验「没落库 + 追问」即可——`state_assertion` 用 `absent` 声明缺失字段（如 `"absent": ["contactName"]`），不要强求抽到其他字段值，否则断言过强会误判 Agent 的合理行为。

## 录制、脱敏与回放

**录制真实 SSE：**

```bash
python main.py --case CREATECONTACT006 --record fixtures/recorded
```

原始录制可能包含姓名、手机、邮箱、业务记录 ID 和内部 Trace ID，**必须脱敏后**才能移入 `fixtures/replay/`。

**一致性脱敏（`scripts/sanitize_replay.py`）：**

回放数据的脱敏不能只做整值替换——Agent 的推理叙述是逐字流式输出的，敏感值会被切成多个 SSE `delta` 分片（如手机号 `139032`+`08506`、企业名 `中铁十二`+`局集团`），整值替换吃不到、但 Parser 拼接后仍会还原。脚本因此做两类处理：

- **整值替换**：命中 JSON 结构里的完整值（快照 `extracted[]`、CRM 卡片 `records[].name` 等）与 `cases.json` 断言。
- **分片替换**：按 `message_id` 聚合逐字 delta → 拼接 → 映射替换 → 按原分片长度切回，保证脱敏彻底且不改变 SSE 事件结构。

schema 结构键（`XdMDItm.*`、`labelKey`、枚举整数、`message_id` 等）一律不动，`cases.json` 与 `.sse` **跨文件同值替换**，回放评测结果不受影响。

```bash
python scripts/sanitize_replay.py --dry-run   # 预览将发生的替换
python scripts/sanitize_replay.py             # 就地脱敏
```

**回放：**

```bash
python main.py --replay fixtures/replay/demo --case DEMO_PASS
```

回放目录结构：

```text
<replay_dir>/
├── cases.json          # 用例集（与 cases/ 同格式）
└── <CASE_ID>/
    ├── T01.sse         # 第一轮原始 SSE
    └── T02.sse         # 第二轮……按文件名排序对应轮次
```

## BadCase → 回归集

真实运行发现 FAIL 后，先人工确认属于 Agent、Evaluator 还是 Oracle 问题，再把对应录制脱敏沉淀到 `fixtures/regression/`（含 `cases.json`、`manifest.json` 和逐轮 SSE），可直接进 CI；原始 `fixtures/recorded/` 仍禁止提交。可用本地 `*.redactions.json` 补充自动脱敏无法识别的自由文本。

```bash
# 确认并沉淀
python -m regression_set promote \
  --report reports/report_xxx.json \
  --case CREATEACCOUNT018 \
  --root-cause "Evaluator 对字段删除语义判断错误" \
  --issue-type evaluator \
  --redactions local.redactions.json \
  --confirm

# 校验 / 运行整个集合
python -m regression_set verify --root fixtures/regression
python -m regression_set run   --root fixtures/regression

# 缺陷修复后升级为阻断型回归（会先重放，通过才升级）
python -m regression_set mark-fixed --root fixtures/regression --case CREATEACCOUNT018
```

新沉淀的 BadCase 默认状态 `known`：每次 CI 都回放，但已知失败只记 `KNOWN FAIL` 不阻断流水线；若回放已通过则记 `RECOVERED` 提示人工确认。配置/Schema/文件错误等异常退出仍会阻断，避免基础设施问题被"已知缺陷"掩盖。升级为 `fixed`（`blocking=true`）后，同一历史缺陷再次出现会阻断 CI。

## AI 用例生成（testgen）

`testgen` 是一条 LLM 流水线：**需求 → 场景规划 → 用例 → 确定性门禁**，走 OpenAI 兼容接口。

```bash
python -m testgen --requirement "录入客户支持工商候选、字段抽取、确认创建"
python -m testgen --req-file req_account.txt --object-type account --max-scenarios 3
python -m testgen --requirement "..." --dry-run        # 通过门禁也不落盘
```

需配置 LLM：环境变量 `LLM_API_KEY`（必填），可选 `LLM_BASE_URL` / `LLM_MODEL`；或在 `credentials.yaml` 填 `llm_api_key`。默认输出到 `cases/<prefix>_generated.json`。

> 生成的用例是评测的**起点而非终点**：AI 可能把 negative case 的断言写得过强或与既有写法不一致，首次跑建议配合 `--dump-fields` 校准，再按实际 Agent 行为收敛断言。

## 质量门禁与退出码

门禁对 Skill 准确率、Tool 准确率、字段 F1 和业务成功率执行阈值判定（定义在 `config.py` 的 `THRESHOLDS`）：

| 门禁项 | 阈值 |
|---|---|
| Skill 路由准确率 | ≥ 0.95 |
| Tool 调用准确率 | ≥ 0.95 |
| 字段抽取平均 F1 | ≥ 0.90 |
| 业务成功率 | ≥ 0.90 |

| 退出码 | 含义 |
|---:|---|
| 0 | 评测通过，或未检测到回归退化 |
| 1 | 质量门禁失败 |
| 2 | baseline/candidate 对比发现退化 |
| 3 | 参数、文件或 Schema 错误 |

```bash
python main.py --replay fixtures/replay/demo --no-gate   # 跳过门禁
```

## 回归对比

先为两个 Agent/Prompt 版本分别生成 JSON 报告，再对比：

```bash
python main.py --compare reports/baseline.json reports/candidate.json
```

对比包括总体通过率、分层通过率、字段 F1、新增失败用例和恢复用例。

## CI

`.github/workflows/ci.yml` 在 **push / pull_request** 时自动执行（Python 3.10 与 3.12 矩阵，`AGENTEVAL_DB_CHECK=0` 全程离线、不发网络请求）：

1. **Pytest** 单元与回归测试
2. **BadCase 回归 fixtures 校验**（`regression_set verify`）
3. **脱敏 Replay 冒烟**：全链路跑通 `DEMO_PASS`，期望退出码 0
4. **Quality Gate 拦截**：`DEMO_ROUTING_FAIL` 必须被门禁拦下，期望退出码 1
5. **回归对比 & BadCase 回放**：基线 vs 退化应识别为退化（退出码 2），并按 manifest 逐条回放 BadCase 集

退出码语义：

| 码 | 含义 |
|---:|---|
| 0 | 通过 |
| 1 | 质量门禁失败 |
| 2 | 版本回归退化 |
| 3 | 配置 / Schema 错误 |

任一阻断条件满足时 Workflow 失败；报告作为构建产物上传。

## 报告

默认在 `reports/` 下生成：

- **JSON**：用例结果、分层明细、字段明细、Trace refs、门禁与回归数据。
- **HTML**：通过率、分层趋势、字段指标、失败分布、性能和 Trace 入口。

`--no-report` 可不保存；`--report-dir` 可改输出目录。

## 项目结构

```text
AgentEval/
├── .github/
│   └── workflows/
│       └── ci.yml              # push/PR 自动跑：pytest + 回放冒烟 + 门禁 + 回归
├── main.py                     # CLI、用例执行、多轮 Trace 合并、门禁
├── case_schema.py              # Pydantic v2 Case Schema
├── config.py                   # URL、超时、门禁阈值、LLM 配置
├── auth/
│   └── browser_login.py        # CRM 登录与 Cookie 缓存
├── runner/
│   ├── agent_runner.py         # 真实 Agent 执行与 SSE 录制
│   ├── replay.py               # 脱敏 SSE 离线回放
│   └── sse_parser.py           # SSE Trace 解析（逐字 delta 拼接）
├── evaluator/
│   ├── evaluator.py            # 五层评测逻辑
│   ├── failure_analyzer.py     # 失败按层归因
│   ├── quality_gate.py         # 质量门禁
│   ├── regression.py           # baseline/candidate 回归对比
│   └── performance.py          # 延迟与成本分析
├── testgen/                    # AI 用例生成流水线（planner/generator/gate/pipeline）
├── regression_set/             # BadCase → Replay 回归集（promote/verify/run/mark-fixed）
├── scripts/
│   └── sanitize_replay.py      # 回放数据一致性脱敏
├── cases/                      # 真实与生成用例
├── fixtures/
│   ├── recorded/               # 原始录制（含真实数据，禁止提交）
│   ├── replay/                 # 脱敏回放数据
│   └── regression/             # BadCase 回归集
├── reports/                    # JSON/HTML 报告
└── tests/                      # 单元与回归测试
```

## 开发与测试

```bash
python -m pytest                        # 全部单测
python -m pytest tests/test_evaluator.py -v
```

测试覆盖五层评测、SSE 解析（含 property-based）、字段/工具/文本/状态断言、失败归因与分类、多轮 Trace 合并、录制回放往返、Schema 校验、用例生成与回归集。评测单测通过 `conftest.py` 的 autouse fixture 关闭 DB Check，确保不发真实网络请求。
