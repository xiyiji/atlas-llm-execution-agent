# Atlas LLM‑Execution‑Agent Platform MVP

> 项目目标：实现委员会式多智能体 MVP，具备任务规划、风险评估、人在回路审批、步骤重试、Verifier 校验重跑、双层记忆、审计日志、SSE 实时前端。

------

# 1 整体说明

## 1.1 项目简介

本项目是一套委员会式多智能体 MVP 系统，由中央调度器协调 5 个智能体：Planner、Safety、Coder、Browser、Verifier。 系统支持将用户目标拆解为可执行任务步骤，做风险评估、人工审批拦截、工具调用（网页检索、沙盒代码执行）、输出校验重跑；提供双层记忆、完整审计事件日志；FastAPI 后端配合原生前端，通过 SSE 推送任务实时状态。 整套系统支持 Demo 模拟模式，无需大模型密钥即可完整跑通端到端流程。

## 1.2 核心术语定义

| 术语                       | 说明                                                         |
| -------------------------- | ------------------------------------------------------------ |
| Committee 委员会           | 5 个 Agent 集合：planner /safety/coder /browser/verifier，由 orchestrator 统一调度，Agent 之间不直接互相调用 |
| Orchestrator 调度器        | 控制逻辑层，只负责流程、状态流转、事件分发、审批等待、失败重试；**不做任何 LLM 推理**；与 Agent 推理逻辑严格分离 |
| Event 事件                 | 系统唯一标准状态变更单元；所有状态变化必须产出 Event；同时写入审计日志、通过 SSE 推送到前端 |
| Rework 重跑                | Verifier 校验失败触发，**全局最多一轮重跑**，防止任务无限循环 |
| Demo Mode                  | `FORCE_DEMO`，不调用真实 LLM、不发起外部网络请求，完全内存模拟，用于演示、单元测试 |
| 工作记忆 Working Memory    | 内存存储，仅存活于单任务生命周期，进程重启丢失               |
| 情景记忆 Episodic Memory   | 磁盘 JSON 文件持久化，跨任务复用；MVP 不做向量检索，直接将最近历史摘要注入 Prompt |
| Human‑in‑the‑Loop 人在回路 | 风险评分超过阈值，任务进入 `awaiting_approval` 状态，等待前端人工确认后继续执行 |

## 1.3 项目文件清单

```
atlas/
├── app
│   ├── __init__.py
│   ├── models.py       #【最底层】全部Pydantic模型、枚举、id工具
│   ├── config.py       # 配置读取，环境变量/.env解析
│   ├── llm.py          # LLM抽象层，多服务商+demo模拟
│   ├── audit.py        # 审计日志读写
│   ├── memory.py       # 工作记忆 + 情景持久记忆
│   ├── risk.py         # 风险评分引擎（启发式+合并LLM风险结果）
│   ├── orchestrator.py  # 调度器核心，任务完整生命周期
│   ├── main.py         # FastAPI入口，REST + SSE
│   ├── agents
│   │   ├── __init__.py # 组装COMMITTEE智能体字典
│   │   ├── base.py     # Agent基类
│   │   ├── planner.py
│   │   ├── safety.py
│   │   ├── coder.py
│   │   ├── browser.py
│   │   └── verifier.py
│   └── tools
│       ├── __init__.py
│       ├── code_exec.py # Python轻量沙盒执行
│       └── web.py       # duckduckgo搜索+网页抓取
├── static
│   ├── index.html
│   ├── app.js
│   └── style.css
├── .env.example
├── requirements.txt
└── run.sh
```

## 1.4 模块依赖总览（单向依赖，严禁反向导入）

> 越靠上代表越底层，优先开发；下层可以 import 上层，上层不能 import 下层。

```
models.py（无内部依赖）
    ↓
config.py（只读os/path，不import其他业务py）
    ↓
audit.py / memory.py / risk.py
    ↓
llm.py（依赖config、models）
    ↓
agents/base.py（依赖llm,memory,models）
    ↓
agents/* 各个智能体（继承base，依赖llm、risk、tools）
    ↓
orchestrator.py（依赖models / audit / memory / risk / agents）
    ↓
main.py（只调用orchestrator，不被其他任何后端文件import）
```

------

# 2 📌 推荐开发实现顺序

遵循：数据模型 → 配置 → 底层支撑模块 → LLM 层 → Agent 体系 → tools 工具 → Orchestrator 调度器 → FastAPI 接口层 → 前端。

> 每完成一个阶段，执行对应自测，不要一次性写完所有代码再调试。

| 阶段                            | 工作内容                                           | 阶段交付物 & 自测要点                                        |
| ------------------------------- | -------------------------------------------------- | ------------------------------------------------------------ |
| 阶段 1：基础数据与配置 (1‑2 天) | `models.py`、`config.py`                           | 模块可正常 import；Task、PlanStep、Event 实例可执行 model_dump_json，序列化无异常 |
| 阶段 2：基础支撑模块 (3‑4 天)   | `audit.py`、`memory.py`、`risk.py`                 | audit.jsonl 可正常追加写入；memory 读写、持久化文件生成；启发式风险函数可输出分数与风险因子列表 |
| 阶段 3：大模型抽象层 (5 天)     | `llm.py`                                           | 开启 FORCE_DEMO，`complete`、`complete_json` 返回模拟输出；JSON 容错解析逻辑正常生效 |
| 阶段 4：智能体体系 (6‑7 天)     | agents 全部文件                                    | Demo 模式可独立调用每个 agent.run ()；planner 输出合法的 PlanStep 步骤列表 |
| 阶段 5：工具层 (8 天)           | tools/code_exec.py、tools/web.py                   | 高危代码被黑名单拦截；正常代码沙盒执行返回结果；demo 模式搜索抓取返回模拟数据，不发起真实 HTTP 请求 |
| 阶段 6：核心调度器 (9‑11 天)    | `orchestrator.py`                                  | Demo 模式完整跑完任务全生命周期；审批阻塞、步骤重试、Verifier 有限重跑逻辑可触发；审计日志生成事件，情景记忆持久化落盘 |
| 阶段 7：Web 接口层 (12 天)      | `main.py` FastAPI 入口                             | Uvicorn 可正常启动；全部 REST 接口调用返回正确 JSON；SSE 长连接可推送事件流、心跳保活 |
| 阶段 8：前端页面 (13‑14 天)     | static html/js/css                                 | 浏览器访问页面，完整跑通任务：提交、规划、执行到结束；高风险任务渲染审批交互按钮；实时事件流正常渲染 |
| 阶段 9：辅助部署文件            | requirements.txt、.env.example、run.sh、Dockerfile | `./run.sh` 一键拉起服务；demo 模式无需密钥即可完整演示系统   |

------

# 3 逐个文件详细说明

> 每个文件包含：职责、设计 Rationale、对外暴露接口、关键逻辑、开发注意事项。

## 📄 app/models.py

**职责**：定义整个项目全部数据结构、枚举、ID 生成工具；**不 import 项目其他业务文件，仅依赖 pydantic/uuid/time**。

**设计 Rationale** 全部业务实体收敛到此文件，避免结构体散落在各个模块造成定义不一致；统一使用 Pydantic 序列化，保证审计日志、SSE、HTTP 接口输出格式统一；统一 ID 生成逻辑，禁止零散手写 UUID。

对外暴露：

- `new_id(prefix:str) -> str`：生成 `task_xxxxxx` / `step_xxxxxx` 格式 id
- 枚举类：`TaskStatus` / `StepStatus` / `RiskLevel`
- Pydantic 模型：`PlanStep`、`RiskAssessment`、`Task`、`CreateTaskRequest`、`Event`

开发注意：

1. 所有业务实体全部在这里定义，不要分散在各个 py 里重复定义结构体。
2. 全部使用`model_dump_json()`序列化，不要手写`json.dumps`。
3. 所有 id 统一用`new_id`生成，禁止手写 uuid。

## 📄 app/config.py

**职责**：统一读取环境变量、解析根目录`.env`；定义全局常量；自动创建 data 存储目录；**不 import models/agents/orchestrator**。

**设计 Rationale** 集中管理全部环境变量与常量，业务代码禁止直接访问`os.environ`；后续修改阈值、超时、模型参数只需要修改本文件；`.env.example`提供开箱可用模板。

对外暴露全部常量：

- LLM 服务商密钥、模型名称
- `FORCE_DEMO`强制 demo 模式开关
- `APPROVAL_THRESHOLD`风险审批阈值
- `MAX_STEP_RETRIES`步骤最大重试次数
- `CODE_TIMEOUT_SECONDS`代码沙盒超时
- `DATA_DIR`、`AUDIT_LOG`、`MEMORY_FILE` Path 对象

关键逻辑： 自动读取项目根目录`.env`，忽略注释行；使用`os.environ.setdefault()`，不覆盖已存在环境变量；自动 mkdir 创建 data 目录。

开发注意：业务代码禁止直接写`os.environ["XXX"]`，全部 import config 读取变量。

## 📄 app/audit.py

**职责**：追加式审计日志，只做事件落盘与读取；依赖 config + models。

**设计 Rationale** 只追加、永不修改历史日志；不在内存做日志缓存，每次读取直接读磁盘，系统重启后仍然支持历史任务回放。

对外函数：

- `record(event: Event) -> None`：接收 Event，append 写入 audit.jsonl，一行一个 json。
- `tail(limit:int=200) -> list[dict]`：读取日志末尾 N 行，跳过解析失败损坏行，返回字典列表。

开发注意：append 模式，永远不修改、不删除历史行，只追加，支持任务回放。不要做内存缓存，每次 tail 直接读磁盘文件。

## 📄 app/memory.py

**职责**：双层记忆：①工作记忆（内存，单任务生命周期）；②情景记忆（持久化 json 文件，跨任务）；依赖 config。

**设计 Rationale** 工作记忆存放于内存追求读写速度；情景记忆落盘实现跨任务上下文；增加线程锁防止多任务并发写破坏 JSON 文件；MVP 不引入向量数据库，降低部署复杂度，直接把最近 N 条记录送入 prompt。

对外函数：

- `working_write(task_id, agent, note)`：向任务的工作记忆追加笔记（内存字典保存，进程重启丢失）
- `working_read(task_id)`：读取该任务全部工作记忆列表
- `working_context(task_id, max_notes=6)`：拼接成 prompt 使用的文本字符串
- `episodic_store(goal, outcome, summary)`：存储一条完成任务摘要到 memory.json；加线程锁；只保留最近 100 条
- `episodic_recall(limit=5)`：读取最近 N 条历史情景记忆

开发注意： `_working`字典是进程内存，程序重启全部清空；只有 episodic 才落盘磁盘。当前没有向量检索 RAG，读取最近 N 条全量送入 prompt。

## 📄 app/risk.py

**职责**：风险评分引擎；两层风险机制：1）硬编码启发式规则；2）合并 LLM Safety Agent 输出风险结果；依赖 config、models。

**设计 Rationale** 大模型输出可以被提示词劫持，本地硬编码启发式规则作为安全底线；取启发式与 LLM 评估的最高分，只要任意一层判定高风险就触发审批流程。

对外函数：

- `heuristic_score(goal:str, steps:list[PlanStep]) -> tuple[int, list[str]]`：纯本地规则打分，不调用任何大模型。
- `merge(goal, steps, agent_score:int, agent_factors:list[str]) -> RiskAssessment`：把启发式分数和 LLM 输出的分数合并，取 max，去重风险因素，计算 level 和 requires_approval 布尔。

内置常量`_HEURISTICS`：关键词风险列表（delete、rm‑rf、password、payment 等）。

开发注意：安全关键点：不能只信任 LLM 输出风险，启发式规则必须保留，对抗 prompt 可以欺骗 LLM，但无法绕过硬编码关键词。

## 📄 app/llm.py

**职责**：LLM 统一访问抽象层，屏蔽各个大模型服务商差异，自动探测可用服务商，失败自动 fallback 进入 demo 模拟模式；依赖 config。

**设计 Rationale** 业务层不直接依赖各个 LLM SDK，隔离第三方库；Demo 模式实现零密钥完整演示；`complete_json`内置容错，解决大模型输出 markdown 包裹 JSON 的常见问题。

对外暴露函数：

- `detect_provider() -> str`：探测当前可用服务商，全局缓存，只探测一次。探测优先级：FORCE_DEMO > Anthropic > Cerebras > Gemini > Groq > Ollama > demo
- `provider()`、`is_demo()`、`model_name()`
- `async complete(system, prompt, max_tokens=1200) -> str`：普通单轮文本补全
- `async complete_json(system, prompt, max_tokens=1200) -> dict | list`：要求返回 JSON，内置容错逻辑：剥离 markdown 反引号，截取`{}/[]`子串，解析失败返回空 dict。

内部实现： `_anthropic()`；`_openai_compat()`通用 OpenAI 兼容接口；`_ollama()`；`_demo()`模拟模式。

开发注意：所有业务 /agent 禁止直接 import anthropic/groq sdk，全部调用`llm.complete / complete_json`。demo 模式不会调用任何真实网络 / 工具，只返回模拟文本。

## 📄 app/agents/base.py

**职责**：所有智能体的父类基类；定义统一接口`run(task, step)`；依赖 llm、memory、models。

**设计 Rationale** 统一 Agent 接口契约，调度器不需要感知各个 Agent 的内部实现；基类统一处理 prompt 上下文组装、写入工作记忆；子类仅重写`run`实现业务推理。

类 `Agent` 类属性：`name`（字符串，和 PlanStep.agent 字段一一对应）、`role`、`system`（system prompt） 方法：`async run(self, task:Task, step:PlanStep) -> str` 默认实现：拼接当前日期、goal、step 信息、working_context 上下文，调用 llm.complete，输出自动写入工作记忆。

开发注意：子类 Agent 可以重写`run()`方法，例如 Planner/Safety 需要 json 输出，内部调用 llm.complete_json ()；但是**函数签名必须保持完全一致**。

## 📄 app/agents/planner.py

**职责**：规划智能体；两个核心能力：①把用户 goal 拆解成 list [PlanStep] 任务图；②全部步骤结束后 synthesize 合成最终报告；继承 Agent 基类。

**设计 Rationale** 由 Planner 唯一负责任务拆解；输出的 Agent 名称必须与 COMMITTEE 字典 key 保持一致，保证调度器可以正常分发步骤。

对外：`class Planner(Agent)`，新增方法：

- `async plan(task:Task) -> list[PlanStep]`：调用 complete_json，解析得到 steps 列表。
- `async synthesize(task:Task) -> str`：汇总 task 所有 steps 输出、工作记忆，生成最终报告文本。

开发注意：plan 输出的每个 PlanStep.agent 必须是 COMMITTEE 字典里面存在的 key。

## 📄 app/agents/safety.py

**职责**：安全智能体；调用 LLM 得到风险评估 json，输出分数和风险因素；继承 Agent 基类。

**设计 Rationale** Agent 只负责推理输出风险评估结果；审批闸门逻辑放在 orchestrator，Agent 不感知审批流程，做到推理逻辑与控制流程解耦。

对外：`class Safety(Agent)`，方法：`async assess(task:Task, steps:list[PlanStep]) -> RiskAssessment` 内部调用 llm.complete_json 拿到 agent_score、agent_factors，交给 risk.merge () 合并启发式规则结果，返回 RiskAssessment 对象。

开发注意：不要在这里直接写审批逻辑，审批闸门逻辑放在 orchestrator 调度器。

## 📄 app/agents/coder.py

**职责**：编码智能体；生成 python 代码，调用 tools.code_exec.run_python 执行代码；继承 Agent 基类。

**设计 Rationale** Agent 仅负责生成业务代码；进程隔离、黑名单、超时等安全细节下沉至 tools 工具层。

对外：`class Coder(Agent)`，重写`async run(task, step)`：

1. LLM 生成 python 代码字符串
2. 调用 tools.code_exec.run_python (code) 执行沙盒
3. 把 stdout/stderr 结果组装成输出返回，写入工作记忆。

## 📄 app/agents/browser.py

**职责**：检索智能体；调用 tools.web 工具，做搜索、抓取网页；继承 Agent 基类。

**设计 Rationale** Agent 负责生成检索 query、整理结果；网络请求、HTML 解析下沉到 tools 层；demo 模式由 tools 直接返回模拟数据，避免发起真实请求。

对外：`class Browser(Agent)`，重写`async run(task, step)`：

1. LLM 提取搜索 query
2. 调用 tools.web.search () 拿到搜索结果
3. 按需调用 tools.web.fetch_page (url) 读取网页正文
4. 整理搜索 + 页面文本，组装输出返回，写入工作记忆。

开发注意：如果`llm.is_demo()`为 True，不要发起真实 http 请求。

## 📄 app/agents/verifier.py

**职责**：校验智能体；评估整套任务输出是否满足原始目标，决定是否需要重跑；继承 Agent 基类。

**设计 Rationale** Verifier 仅输出校验判断；重跑的控制逻辑在 orchestrator，强制限制最大重跑轮次，避免任务死循环。

对外：`class Verifier(Agent)`，重写`async run(task, step)`： 接收 task 全部 steps 输出、用户 goal；llm 输出 json：`passed(bool), notes(str)`；赋值给`task.verified = passed`；`task.verification = notes`；orchestrator 读取 task.verified 判断是否触发 rework 重跑。

## 📄 app/agents/**init**.py

**职责**：实例化全部 Agent，组装全局字典 COMMITTEE，给 orchestrator 使用。

**设计 Rationale** 统一导出委员会字典，orchestrator 仅依赖此文件，不需要逐个导入 Agent 类；后续新增 Agent，仅需在此注册。

```
from .base import Agent
from .planner import Planner
from .safety import Safety
from .coder import Coder
from .browser import Browser
from .verifier import Verifier

COMMITTEE: dict[str, Agent] = {
    "planner": Planner(),
    "safety": Safety(),
    "coder": Coder(),
    "browser": Browser(),
    "verifier": Verifier(),
}
```

开发注意：key 名称必须和 PlanStep.agent 字段完全匹配。orchestrator 只 import 这个文件的 COMMITTEE，不单独 import 每个 agent 类。

## 📄 app/tools/code_exec.py

**职责**：MVP 轻量 Python 沙盒；隔离子进程执行模型生成的代码；依赖 config。

**设计 Rationale** MVP 不引入重量级容器；叠加多层防护：黑名单过滤、临时目录、Python 隔离模式、干净环境变量、执行超时，实现纵深安全防护。

对外函数：`async run_python(code:str) -> dict` 返回结构：`{"ok":bool, "stdout":str, "stderr":str}`

核心逻辑：

1. 正则黑名单`_DENY_RE`拦截高危关键字，命中直接拒绝执行。
2. `tempfile.TemporaryDirectory`临时目录。
3. `asyncio.create_subprocess_exec`使用`python -I`隔离模式，干净 env 环境变量。
4. `wait_for`设置超时时间，超时 kill 进程。
5. stdout/stderr 做长度截断，防止输出爆炸。

## 📄 app/tools/web.py

**职责**：网页检索工具，无 API‑key，使用 DuckDuckGo html 页面；demo 模式返回模拟结果。

**设计 Rationale** 工具层感知 demo 开关；demo 模式完全跳过 HTTP 请求；剥离 HTML 标签只返回纯文本，减少 LLM Token 消耗。

对外两个 async 函数：

- `search(query:str, max_results=5) -> list[dict]`：搜索，返回 title/url/snippet；demo 模式直接返回模拟数据。
- `fetch_page(url:str, max_chars=3500) -> str`：请求网页，剥离 script/style/html 标签，返回纯文本；抓取失败返回错误字符串。

开发注意：如果`llm.is_demo()`为 True，不要发起真实 http 请求。

## 📄 app/orchestrator.py

【系统最核心文件，业务调度中枢】 依赖：audit、config、memory、agents.COMMITTEE、models、risk；全局单例：`ORCHESTRATOR = Orchestrator()`

**设计 Rationale** 全部控制逻辑收敛于此；Agent 只负责推理，不能修改任务状态，不能发射事件；所有状态变更统一走`_emit`，保证审计日志与前端事件一致性；使用 Future 实现异步等待人工审批；强制限制 rework 最多一轮，避免无限循环。

类 Orchestrator 实例变量：

- `self.tasks: dict[str, Task]`：内存保存所有运行中任务
- `self._subscribers: dict[str, list[asyncio.Queue]]`：SSE 订阅队列，一个任务支持多个前端订阅
- `self._approvals: dict[str, asyncio.Future]`：保存等待人工审批的 Future 对象

对外公开方法（供 main.py 调用）

- `create_task(goal:str, auto_approve:bool) -> Task`
- `get(task_id:str) -> Optional[Task]`
- `resolve_approval(task_id:str, approved:bool) -> bool`
- `subscribe(task_id:str) -> asyncio.Queue`
- `unsubscribe(task_id:str, q:asyncio.Queue)`

内部私有方法：

- `async _run(task, auto_approve)`：完整任务生命周期主函数
- `async _run_step(task, step)`：执行单个 PlanStep；循环 MAX_STEP_RETRIES 次重试，指数退避 sleep
- `async _wait_for_approval(task) -> bool`：创建 Future，wait_for 600s 超时返回 False
- `_set_status(task, status)`：修改 task.status，并且 emit 事件
- `_emit(task, type_, agent, message, data)`：事件统一出口：构造 Event → audit.record 写入日志 → 推送到全部订阅队列。

开发注意： 所有状态变更必须走`_emit`，保证审计日志和前端事件完全一致，支持回放。rework 限制：max_rounds=2（原始一轮 + 最多一次重跑，避免无限循环）。

## 📄 app/main.py

**职责**：FastAPI Web 服务入口，只做 http 参数校验、转发调用 orchestrator 单例；不写业务逻辑。

**设计 Rationale** Web 层与业务逻辑解耦，未来可替换为其他 web 框架；SSE 下发快照保证晚接入前端拿到当前状态，增加心跳包规避网关长连接超时断开。

内容：

- FastAPI app 实例
- 各个接口路由实现
- GET /api/health
- POST /api/tasks
- GET /api/tasks/{task_id}
- POST /api/tasks/{task_id}/approval
- GET /api/tasks/{task_id}/events：SSE 长连接生成器 gen ()
- GET /api/audit
- GET /api/memory
- app.mount ("/static", StaticFiles (...)) 挂载静态前端资源
- GET / 返回 index.html 文件响应

SSE 重点逻辑： 连接建立，首先下发 snapshot 快照事件；25 秒发送: keepalive\n\n 心跳包，防止网关断开长连接；收到 stream.end 事件，退出循环；finally 块调用 unsubscribe 清理订阅队列。

开发注意：main.py 不要写任何业务逻辑，全部交给 ORCHESTRATOR 单例处理；便于后期更换 web 框架。

## 📄 static/index.html

前端页面 UI 结构： 左侧：任务输入框、auto‑approve 开关、Run Task 按钮、Episodic Memory 展示区 右上：ACTIVE GOAL、委员会状态指示器、Risk 分数、Task Graph 步骤列表 右下：Activity Stream 事件流、Final Report 结果展示区

## 📄 static/app.js

前端业务 JS，无框架原生 JS： 请求 /api/memory 加载历史记忆；点击 Run Task：POST /api/tasks 创建任务，拿到 task_id；建立 SSE 连接；解析事件更新 DOM；审批按钮交互；收到 stream.end 关闭 EventSource。

## 📄 static/style.css

页面样式文件。

------

# 4 开发过程自测校验点

写完 models.py+config.py

> 代码可以 import 不报错；实例化 Task、PlanStep、Event，看能否正常 model_dump_json 序列化。

写完 audit + memory + risk

> audit.record 写入 audit.jsonl；tail 读取返回列表；memory 读写；heuristic_score 给样例 goal 可以输出分数。

写完 llm.py

> 设置 FORCE_DEMO=1，调用 complete /complete_json 拿到模拟返回。

写完 agents 全部

> demo 模式，单独调用每个 agent.run ()，看返回输出；plan 返回合法 list [PlanStep]。

写完 tools

> run_python 执行正常代码返回 ok=True；黑名单危险代码被拦截；web.search 在 demo 返回模拟结果。

写完 orchestrator.py

> 在 python 交互环境导入 ORCHESTRATOR，create_task，demo 模式完整跑完任务；检查 audit.jsonl 产生事件；memory.json 写入 episodic 记录；验证审批阻塞逻辑。

写完 main.py

> uvicorn 启动，curl 调用各个 http 接口，验证返回；SSE 接口可以返回事件流。

写完 static 前端

> 浏览器访问页面，完整跑任务：提交→规划→执行→完成，看到实时事件流；高风险任务弹出审批按钮。

# 5 整体集成校验清单

- 确认没有循环 import（agents 不要 import orchestrator；main 不被其他 py 导入）
- 所有配置读取都来自 config，没有散落 os.environ 硬编码。
- 所有状态变更都走`_emit()`，audit 日志每条任务都产生完整事件序列。
- demo 模式下，不需要任何密钥、不需要 ollama，整套系统前后端完整可演示。
- 高风险任务触发 awaiting_approval，等待审批；auto‑approve=True 跳过闸门。
- verifier 校验失败，触发 rework，step 重置 pending，重跑一轮；最多一轮重跑。
- 步骤失败触发 MAX_STEP_RETRIES 重试；重试耗尽任务标记 failed。
- 多任务同时运行，memory.json 并发写入不会损坏文件（线程锁生效）。

