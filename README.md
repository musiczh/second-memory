# Second Memory

面向 Agent 的个人第二记忆库 Skill + CLI。系统使用本地 Markdown 和 Git 保存不可变原文与可重建知识图谱；CLI 负责确定性解析、校验、投影和事务，宿主 Agent 通过 `CompilePlan v2` 两阶段协议完成语义推理。

## 设计边界

- `raw/` 保存用户原文，正文哈希不可变。
- 编译层包含 entity、event、statement、topic 四类节点及显式边；产品层将 statement 展示为「洞察」。
- `index.md` 与 timeline 是图谱投影，不由 Agent 直接撰写。
- CLI 不调用 LLM，也不依赖数据库、向量库或后台服务。
- 普通检索与 Consolidation 不发送整个 raw 归档。
- 每次 apply 使用 session 校验和完整 staging，拒绝过期响应与半写入。

## 安装

```bash
git clone https://github.com/musiczh/second-memory.git
cd second-memory
scripts/setup.sh
```

`scripts/setup.sh` 创建 `.venv`、以 editable 模式安装 CLI，并把当前仓库链接为 Codex Skill。

如果 shell 找不到 `second-memory`，将 `~/.local/bin` 加入 `PATH`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 初始化知识库

全机共享：

```bash
second-memory init --scope shared --json
```

指定 Agent：

```bash
second-memory init --scope agent --agent codex --json
```

隔离或测试时必须显式传入知识库路径：

```bash
second-memory init --repo /tmp/second-memory-test --scope agent --agent test --json
export SECOND_MEMORY_REPO=/tmp/second-memory-test
```

代码仓库与运行时知识库是两套独立 Git 仓库。Skill 更新需要在代码仓库显式执行 `git pull --ff-only`；`update --emit-request` 不会隐式修改代码仓库。

## Agent 工作流

Agent 应先完整读取 `SKILL.md`。

### 显式入库

```bash
printf '%s' "用户原文" | second-memory add \
  --title "记录标题" \
  --event-date 2026-08-05 \
  --stdin --json

second-memory compile --emit-request --json
```

Agent 读取 `data.llm_request`，输出严格匹配 `response_schema` 的 V2.4 契约 `CompilePlan v2`，并原样保留 `schema_version`、`session_id` 和 `mode`：

```bash
printf '%s' "$COMPILE_PLAN_JSON" | \
  second-memory compile --apply-response --stdin --json
```

apply 成功后，Agent 还必须根据 `data.recap_request` 向用户提供关联历史的回顾，不能只回复“已保存”。

### 个人上下文检索

```bash
second-memory search --query "职业规划" --level 1 --json
```

需要读取候选节点和有限 source snippet 时：

```bash
second-memory search --query "职业规划" --level 2 --emit-request --json
```

### 回顾

```bash
second-memory recap --json
second-memory recap --raw-id "$RAW_ID" --json
second-memory review --range last-week --emit-request --json
second-memory review --on-this-day 2026-08-05 --emit-request --json
```

### Wiki HTML

```bash
second-memory wiki --json
second-memory wiki --output /tmp/second-memory-wiki.html --json
second-memory wiki --open --json
```

`wiki` 是确定性的只读导出命令，不调用 LLM，也不改动知识库。默认输出到 `<repo>/../wiki.html`，生成的单文件 HTML 可直接双击打开，包含：

- 默认主题优先阅读；entity、event、洞察（`type=statement`）、topic 四类节点及详细综合内容。
- topic 的核心理解、演变、矛盾、开放问题、证据和混合类型成员。
- entity 的直接 Raw、经事件、经洞察三类显式来源路径。
- event 状态／日期历史、洞察 evolution 和事件 timeline 投影。
- 显式出边、反向关联、raw 来源与注解。
- manifest schema、redirect、candidate、Consolidation 队列、drift 和事务恢复状态。

生成文件会预渲染编译结果作为静态首屏，再由 JavaScript 增强筛选和详情路由；即使本地预览器禁用脚本，也不会出现空白页面。源码模板未注入知识库数据时只显示用途提示，不能作为导出产物使用。

HTML 保留 v1 entity／topic／timeline 兼容，便于在 v2 rebuild 前浏览旧知识库副本。

### Consolidation

每次增量编译会把已消费 raw ID 加入 Consolidation 队列。满 10 条后可执行：

```bash
second-memory consolidate --emit-request --json
printf '%s' "$CONSOLIDATION_PLAN_JSON" | \
  second-memory consolidate --apply-response --stdin --json
```

Consolidation 向 Agent 提供批次注解、compact index、候选、一跳关系，以及不含 raw 正文的全库 member catalog、raw annotation catalog 和 source dates。所有成功编译 Raw 都参与计数，即使该条 Raw 没有产生耐久节点。成功事务消费最早 10 条；失败时队列不变。

批次只决定整理触发时机，不决定主题边界。主题必须在全库成员目录上满足高维组织契约：至少 5 个直属成员、2 个洞察、2 个维度和 3 个独立 raw capture session；成员可为 Raw、entity、event、statement 或 child topic，只有纵向演进主题额外要求 14 天跨度。高维不等于跨域：AI 协作等稳定领域可形成 `life_domain`，睡眠等长期变化可形成 `longitudinal_arc`；仅在同一机制确实跨领域复现时使用 `cross_domain_pattern`。每个成员必须给出 facet、纳入理由和成员自身的直接摘录；成员自己的 content／evidence 必须直接回答 organizing question 并支持该 facet，rationale 不得发明成员中不存在的桥接。未成熟的重复讨论簇进入稳定 topic candidate，而不是被静默遗漏。

需要整体审查或重建主题层时，不必重放 raw：

```bash
second-memory topics --emit-request --json
printf '%s' "$TOPIC_PLAN_JSON" | second-memory topics --apply-response --stdin --json
```

该命令使用全库节点详情、Raw 注解、来源日期、旧主题和候选审计信息，原子替换 topic 与 `contains`；entity、event、statement、evolution、raw、pending 和 Consolidation 队列保持不变。与旧主题无关的 candidate／redirect 保留，引用已删除主题的治理记录同步清理，避免悬空引用。

### 维护调度

```bash
second-memory update --emit-request --json
```

模式优先级固定为：

1. `rebuild`：知识库版本变化、编译页／raw 正文漂移，或已有逐条重建尚未完成。
2. `incremental`：存在未编译 raw。
3. `consolidate`：队列至少 10 条。
4. `consolidate` quality repair：没有更高优先级工作，但存量图存在跨节点重复详情、单节点编译政策措辞，或同一 claim 被多个 entity 复用且未直接点名其中部分实体；请求固定 `batch_size=0`、`quality_repair=true`，不消费队列。
5. `noop`：无待处理工作。

apply 不接受 Agent 自行切换模式：CLI 以相同优先级验证返回 mode，并用覆盖 raw 元数据、规则、图谱与治理状态的 session 指纹拒绝过期响应：

```bash
printf '%s' "$PLAN_JSON" | second-memory update --apply-response --stdin --json
```

## CompilePlan v2

顶层字段固定为：

```json
{
  "schema_version": 2,
  "session_id": "session-...",
  "mode": "incremental|rebuild|consolidate|topics",
  "raw_annotations": [],
  "node_actions": [],
  "out_edges": [],
  "candidates": [],
  "consolidation_memo": ""
}
```

关键规则：

- create 使用 plan-local `ref`，CLI 生成稳定 ID；更新已有节点使用 `target_id`。
- 每条 raw annotation 显式返回 `mentions`、`occurrences`、`claims` 三个数组，使实体提及、事件事实和洞察线程可以独立审计。
- entity／event／statement action 必须分别由同一 source raw 的非空 mentions／occurrences／claims 支撑；`belongs_to` 不能脱离带该 source_id 的节点动作独立挂靠。source-only `reinforce` 仅用于 incremental／rebuild；只有新来源完全重复、不会新增历史、推翻旧不确定性、改变综合或让详情过期时才可使用，否则必须用完整 `refine` 综合全部新旧来源。source-only 只允许 `target_id`、`type`、`source_ids`，不接受 `sources` 别名，也不得改写节点内容。
- 每个耐久 entity mention 都必须解析或创建。entity 来源由直接 Raw mention，以及经显式 `involves|about|instance_of` 关联的 event／statement 来源确定性合并；共享关键词或日期不能推导来源。
- entity 只做稳定锚点，覆盖人、组织、地点、作品、产品、工具、项目、任务、对象、概念和情绪；event 必须通过用户关系、发生性、时间、事实性、时间线类别和独立回顾价值六道门，并声明 `event_basis` 与 `standalone_reason`；statement 保存洞察的 `current_state` 与追加式 `evolution`。
- 一条 raw 可以同时产生实体、事件和洞察。节点 create 或实质更新必须携带来源约束的 `content`，包含 summary、detail、key_points、evidence 和 uncertainties。
- 三个抽取通道都没有耐久内容时允许零节点完成编译；raw 保留，但不为满足挂靠要求制造微小事件或空泛洞察，并仍计入每 10 条一次的 Consolidation 审计。
- event 标题与 `semantics.action` 使用同一条发生短语；咨询、会议等发生事实与其中形成的反思拆为 event＋statement。普通阅读、普通聊天、短暂自我观察和未执行的决定不因带日期或被标为 incident 就成为事件。
- `content.detail` 必须是可独立阅读的多段综合，而不是摘要扩写；按节点类型使用「对象与关系／历史与现状」「发生与背景／结果与关联」「洞察与依据／演进与影响」「组织视角／脉络与边界」两段标签，至少四个实质句和三个具体关键点。每句必须由该节点自己的 source／evidence／语义历史支撑；规范化后达到 24 字的非 evidence 实质句不得跨节点精确复用，低于 24 字不据此单独判重。evidence 引文豁免只允许剥离「其直接依据是」「它参与」后与本节点 claim 全文精确相等，不能做包含／后缀匹配。「当前节点只确认」「节点仅保留」「节点不把」「后续若出现新的实质信息」「后续实质变化需要」等编译政策措辞即使只出现于单节点也标记 `weak_detail`，但普通包含“节点”的事实句不受影响。新 plan 投影出现上述问题时拒绝 apply，存量命中由 quality repair 修复。
- 同一规范化 `evidence.claim` 被多个 entity 复用时，claim 必须通过规范化 title、alias 或保守实体词形直接点名每个实体；不得用宽泛 2-gram 推定。未被点名的实体进入 `semantic_quality.weak_evidence`。零批次 quality-repair Consolidation 必须在同一投影图中同时清除重复详情和弱实体 evidence。
- 增量模式不得创建 topic 或执行 merge/split，只能记录候选。
- topic 只在 consolidate 或独立 topics refresh 中创建，直接 contains 至少 5 个成员和 2 个 statement，并满足多维、跨来源、侧面关系与成员依据契约；成员可为 Raw、entity、event、statement 或 child topic，最大深度三层。
- topic 除通用 `content` 外还必须保存 `attrs.topic_reading`，包含核心理解、带来源的演变、真实矛盾、开放问题和置信度；无证据时矛盾或开放问题使用空数组，不能编造。
- rebuild 不迁移旧实体、主题、边、redirect 或候选项。它按 `created`、`raw_id` 顺序每次只重放一条 raw；第一步使用空图谱，后续步骤只能复用本次 rebuild 已生成的节点。
- rebuild 每累计 10 条已编译 raw 就先消费一个 Consolidation 批次，再继续顺序重放；10 条只触发全库主题审查，不强制创建 topic。全部 raw 重放完成后，最终 1—9 条尾批会在隔离 workspace 中执行一次 partial Consolidation，只有队列清空后才允许提升；普通 consolidate 仍严格要求 10 条。
- 旧版若已把带 1—9 条尾批的 rebuild workspace 提升，只有在 rebuild 已完成、compiled raw 与 recorded ordered raw 完全一致、队列恰为 ordered 后缀且没有待编译输入时，才补发一次 final-tail Consolidation。普通短队列以及 rebuild 后新增／增量编译过 raw 的知识库不会触发该兼容恢复。
- consolidate 响应固定返回空 `raw_annotations`，不能越过当前批次改写 raw 注解。
- 最终提升时同时从 V2 默认值重建 `AGENTS.md`、`.gitignore` 与 `.kb/config.yaml`；只保留 scope／agent／backend 等部署设置，路径重新绑定到副本自身。
- 最终提升在源库锁内重新校验 raw 正文和非编译型 frontmatter，重建期间任何原料变化都会使 session 过期而不是被覆盖。
- raw 使用 `belongs_to` 指向节点；对称边由 CLI 按 ID 规范化。

## 运行时结构

```text
knowledge-base/
├── raw/                    # 用户原文，正文哈希不可变
├── wiki/
│   ├── entities/
│   ├── events/
│   ├── statements/
│   ├── topics/
│   └── timeline/
├── index.md
├── AGENTS.md
└── .kb/
    ├── config.yaml
    ├── pending.jsonl
    ├── manifest.json       # schema 2、哈希、边、redirect、候选和批次状态
    └── transaction.json    # 仅在未完成事务存在时出现
```

## 状态与验证

```bash
second-memory status --json
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src
.venv/bin/second-memory --help
```

`status` 会返回节点／边计数、pending、candidate、Consolidation 状态、manifest drift、版本漂移和事务恢复状态。

## 数据安全

- 不要把个人知识库推送到公开的 Skill／CLI 仓库。
- 不要直接编辑 `raw/`；需要修正时新增一条记录并通过洞察 evolution 表达变化。
- 测试和预发布验收必须使用独立临时知识库，并同时指定工作区 Skill 与工作区 CLI，避免误用全局安装版本。
