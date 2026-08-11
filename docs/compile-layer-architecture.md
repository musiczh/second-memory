# 编译层数据组织与存储架构设计

> 状态：V2.4 实施规范（继承 V2.3 事件与详情合同，覆盖 V2.2 主题成员模型）
> 适用版本：`KB_VERSION >= 2.4.0`
> 关联代码：`src/second_memory/compiler.py`、`retriever.py`、`recap.py`、`promptio.py`

## 0. V2.4 实体覆盖、主题提炼与理解层契约（优先级最高）

本节覆盖 0.1 中“`belongs_to` 必须依附内容更新动作”和 0.2 中“主题直属成员只能是 statement”的旧约束。V2.4 不改变 CompilePlan 顶层 schema，也不引入数据库、向量库、后台服务或新运行时依赖。

1. **来源关联与内容更新解耦**：已有 entity／event／statement 再次被来源支持时，Agent 必须先比较新来源与节点完整 summary、detail、evidence、uncertainties 和历史。source-only `reinforce` 仅允许出现在 incremental／rebuild replay；只有纯重复提及、不会新增历史、推翻旧不确定性、改变综合或让任一句过期时才可使用，否则必须完整 `refine` 并综合全部新旧来源。source-only 动作只含 `target_id`、`type`、`source_ids`，不接受旧 `sources` 别名，并配套 raw → node `belongs_to`；不得携带 `content`、summary、title、aliases、attrs、semantics、状态或演进字段。CLI 只并入来源，不重写页面知识。
2. **实体形成全库覆盖闭包**：每个耐久 mention 都必须解析到已有实体或创建新实体。Raw 直接提及形成直接来源；event／statement 通过 `involves`、`about`、`instance_of` 显式边指向实体时，其 Raw 来源形成关联来源。event `object_refs/location_ref` 只校验事件语义，不构成来源传播边。闭包只沿显式关系计算，不以共享关键词、日期或批次推断。
3. **主题高维不等于跨域**：主题的高维性来自它能组织多条独立证据、多个时间点和多种节点形成稳定阅读视图。`life_domain` 可聚合 AI 协作等长期领域，`longitudinal_arc` 可组织熬夜与睡眠等演变议题，只有真正跨领域复现同一机制时才使用 `cross_domain_pattern`。
4. **主题使用全节点成员**：每个主题至少 5 个直属成员、至少 2 个 statement、至少 3 个独立 Raw capture。成员可为 raw、entity、event、statement 或 child topic；每个 facet 至少 2 个成员。child topic 可有多个父主题，DAG 最大深度 3。
5. **成员必须独立贡献**：`facets[].member_refs` 完整分区直属成员，`member_rationales` 为每个成员给出 facet、独特贡献和来源可验证 excerpt。实体、事件和 Raw 不能作为配额填充；移除边缘成员后未达门槛则暂缓主题。
6. **每 10 条 Raw 触发全库审计**：队列计入所有成功编译的 Raw，不只计入产生新节点的 Raw。十条只决定审计时机，不决定主题边界或数量。每个重复讨论簇必须被物化为主题，或进入带稳定 ID、`pending|watching|rejected|materialized` 状态和理由的候选台账；不得静默消失。
7. **主题详情是结构化理解**：除通用 `content` 外，topic 必须保存 `attrs.topic_reading`：`core_understanding`、带来源的 `evolution`、`contradictions`、`open_questions`、`confidence`。矛盾和开放问题允许空数组，但字段必须存在，禁止为满足格式编造内容。
8. **主题优先阅读**：Wiki 默认进入主题视图；主题详情按核心理解、演变、矛盾、开放问题、证据、成员组织的顺序展示。实体页区分直接 Raw、经事件和经洞察三种来源路径。图谱与 Raw 仍作为证据下钻入口。
9. **心理状态只做有边界的综合**：允许总结记录中反复出现的敏感、低价值感、关系安全、主体性和回避模式，但必须标记证据、置信度、反例与待验证项，不输出临床诊断。
10. **验收关注召回与可读性**：新增实体来源召回、重复讨论簇主题覆盖、主题来源覆盖和无证据综合率检查。CLI 返回成功不是充分条件；必须由隔离上下文执行 Agent 生成产物，再由只读 Agent 按 Raw、图谱和 Wiki 独立审计。

V2.4 主题合同写入：

```json
{
  "attrs": {
    "topic_contract": {
      "topic_kind": "life_domain|cross_domain_pattern|longitudinal_arc",
      "organizing_question": "这个长期主题持续回答什么问题？",
      "facet_relationship": "不同侧面如何共同形成稳定阅读结构？",
      "boundary_rule": "哪些证据可加入，哪些相近证据不能加入？",
      "facets": [
        {"name": "侧面 A", "summary": "该侧面如何回答组织问题", "member_refs": ["statement-a", "event-b", "raw-c"]},
        {"name": "侧面 B", "summary": "该侧面如何回答组织问题", "member_refs": ["entity-d", "statement-e"]}
      ],
      "member_rationales": {
        "statement-a": {"facet": "侧面 A", "reason": "独特贡献", "supporting_excerpt": "成员自身的直接依据"}
      },
      "exclusions": []
    },
    "topic_reading": {
      "core_understanding": "可以独立阅读的当前综合理解",
      "evolution": [{"date": "YYYY-MM-DD", "state": "该阶段的理解或变化", "source_ids": ["raw-..."]}],
      "contradictions": [{"member_refs": ["statement-a", "statement-e"], "description": "证据之间的张力", "source_ids": ["raw-..."]}],
      "open_questions": [{"question": "仍未解决的问题", "basis": "为什么证据仍不足", "source_ids": ["raw-..."]}],
      "confidence": 0.8
    }
  }
}
```

## 0.1 V2.1 基础语义契约

本节覆盖本文后续章节中与其冲突的旧设计。V2.1 不引入向量索引；原料是唯一真相源，V1／旧 V2 编译产物不得成为 rebuild 输入。

1. **实体、事件、洞察独立抽取**：一条 raw 可以同时贡献多类节点，不做主类型三选一。
2. **实体是纯指代锚点**：支持 person、organization、place、work、product、tool、project、task、object、concept、emotion。书籍属于 work；「AI 协作」等方法或认知线程属于洞察。
3. **事件必须通过事实门**：用户相关、存在具体动作／经历／状态变化、具备时间锚点、事实性为 occurred／ongoing 或明确排期的 planned。raw 的 event_date 不能单独证明存在事件。
4. **洞察的存储类型仍为 statement**：产品和 Wiki 统一显示「洞察」；感受、判断、关系模式、目标、计划、决定和方法论均进入可演进的洞察线程。
5. **每个节点都有来源约束的综合内容**：`content` 固定包含 summary、detail、key_points、evidence、uncertainties。事实性结论必须绑定有效 source_id。
6. **timeline 只投影事件**：statement evolution 作为「认知演进」在洞察详情中独立展示，不能计入事件时间线。
7. **raw-only rebuild**：从空图按 created、raw_id 顺序逐条执行日常入库语义；每累计 10 条耐久 raw 就先执行一个有界 Consolidation 批次，再继续重放，主题只由该阶段重新形成。
8. **三通道与动作强绑定**：引用某条 raw 的 entity／event／statement action，必须分别由该 raw 的 mentions／occurrences／claims 非空通道支撑；`belongs_to` 必须指向同一条带 source_id 的节点动作。
9. **会话和重建保护完整输入**：session 指纹覆盖 raw 元数据、规则、边和 redirect；rebuild 对剔除编译字段后的完整 frontmatter 建指纹，并在源库锁内完成最终校验与切换。
10. **Consolidation 不改 raw 注解**：整理响应固定返回空 `raw_annotations`，只消费既有批次注解、重组编译节点和候选项。

## 0.2 V2.2 历史主题组织契约（被 V2.4 覆盖）

本节只改变 topic 生成与重组，不改变 entity、event、statement、raw 和 timeline。后文把「两条记录」「阅读对象」或局部批次直接提升为主题的规则均属于历史设计，不再作为实现依据。

1. **主题高于洞察**：topic 是贯穿多个洞察的稳定组织视角，不是把几个洞察连接后的更大结论。主题直接 `contains` statement；entity 和 event 通过洞察已有关系被间接组织，不能用于凑成员数量。
2. **全库判定而非批次判定**：10 条 Consolidation 队列只决定何时触发整理，不定义主题边界。Agent 必须基于全库 statement catalog 和现有主题成员审计后再判断诞生、替换或暂缓。
3. **允许不覆盖**：没有达到稳定性和一致性的洞察可以不属于任何主题。主题数量不设配额，宁可少建也不拼接。
4. **多维结构**：每个主题至少包含 4 个洞察、2 个 facet，每个 facet 至少 2 个洞察；至少来自 3 个独立 raw capture session。只有 `longitudinal_arc` 额外要求原料事件日期跨度至少 14 天；时间跨度本身不能弥补语义不一致。
5. **成员可解释**：主题必须声明一个 `organizing_question`、解释各侧面为何共同构成更高维整体的 `facet_relationship`，以及可操作的 `boundary_rule`；每个成员必须有唯一 facet、具体贡献理由和从该成员自身 current_state／detail／key_points／evolution、语义字段或 Raw 注解复制的 `supporting_excerpt`。成员自己的内容与 evidence 必须直接回答 organizing_question，并直接支持所分配 facet；rationale 只能解释已有证据，不能发明成员中不存在的领域、机制或桥接句。共享时间、情绪、来源批次、泛化词或一句事后桥接说明不构成归属依据。
6. **边界可证伪**：每个主题至少列出一个语义接近但被排除的洞察、该洞察最接近主题的 `nearby_excerpt` 及排除理由；必须检查完整 evolution，不能只挑较弱摘要来证明排除。
7. **抑制重复主题**：任一洞察最多直属 2 个主题；任意两个主题的共享成员数除以较小主题成员数必须小于 0.4。超过阈值应合并或重新划界。
8. **成员集合是完整替换**：Consolidation 的主题实质更新必须返回 `membership_mode=replace`。CLI 先移除该主题旧 `contains`，再验证并写入完整新集合，避免错误成员只能追加不能撤回。
   Topic action 固定返回 `source_ids=[]`；最终 `sources` 必须由本次完整成员集合的 statement sources 纯推导，不能并入旧成员遗留来源。
9. **独立主题刷新**：`second-memory topics` 在不读取 raw 正文的前提下，使用全库洞察详情、来源日期和旧主题审计信息，原子替换整个 topic 层及所有关联旧主题的边。实体、事件、洞察、演进、raw、pending 与 Consolidation 队列保持不变；与旧主题无关的 candidate／redirect 保留，引用已删除主题的治理记录清理掉，禁止悬空引用。
10. **独立直答优先于数量**：隐藏 topic title／facet／rationale 后，成员洞察自身的 current_state、content、key_points 与未被 supersede 的 evolution 仍须直接回答 organizing_question。移除不合格成员后重新计算全部门槛，不得用跨域通用机制补第四成员。
11. **正反双向完备性**：正向逐成员验证归属后，必须反向遍历所有未归类洞察，并对每个 organizing_question 再做一次独立直答测试。混合场景洞察中只要有未被 supersede 的内容直接回答该问题，就不能因为还包含其他关系类型而整条漏掉；其余未归类洞察至少要在最接近的主题 exclusions 中用最强 `nearby_excerpt` 留下排除决定。非空主题刷新存在“既未归属也未排除”的洞察时，CLI 拒绝 apply；这项审计用于防漏，不允许反过来追求覆盖率或强行归类。

主题契约存储于 `attrs.topic_contract`：

```json
{
  "topic_kind": "life_domain|cross_domain_pattern|longitudinal_arc",
  "organizing_question": "这个稳定领域或跨域模式持续回答什么问题？",
  "facet_relationship": "不同侧面如何共同解释一个高于单条洞察的稳定结构？",
  "boundary_rule": "什么样的洞察可以加入，什么样的相近洞察必须排除？",
  "facets": [
    {"name": "维度 A", "summary": "该维度的组织作用", "statement_refs": ["statement-a", "statement-b"]},
    {"name": "维度 B", "summary": "该维度的组织作用", "statement_refs": ["statement-c", "statement-d"]}
  ],
  "member_rationales": {
    "statement-a": {"facet": "维度 A", "reason": "它对 organizing_question 的独特贡献", "supporting_excerpt": "洞察中的直接依据"}
  },
  "exclusions": [
    {"statement_id": "statement-nearby", "nearby_excerpt": "被排除洞察最相关的依据", "reason": "虽然相近，但仍不回答该主题的核心问题"}
  ]
}
```

## 0.3 V2.3 事件发生性与可读详情契约

本节只收紧 event 的诞生条件和所有节点的详情质量，不改变 raw、实体类型、洞察演进、主题成员门槛和事务结构。

### 事件是可回顾的发生，不是带日期的洞察

事件必须同时满足六道门：

1. **用户关系门**：主体是用户，或事情直接作用于用户。
2. **发生性门**：删掉反思、解释、情绪标签和结论后，仍剩下一个有边界的动作、经历或外部变化。
3. **时间门**：存在来源明确支持的 ISO 时间锚点；raw 的 `event_date` 不能单独证明事件。
4. **事实性门**：为 occurred／ongoing，或具有明确承诺和排期的 planned。
5. **时间线类别门**：`event_basis` 只能是 appointment、scheduled_commitment、incident、milestone、transaction、material_change。
6. **独立价值门**：`standalone_reason` 必须基于来源解释为什么即使拿掉洞察，这件事仍值得出现在月度时间线中。

两个反事实测试必须同时通过：

- 如果去掉「觉察、识别、反思、重构、复盘、理解、整合、思考、捕捉、感悟」等认知结果，是否仍有明确发生的事情？
- 一个月后只看发生的事情而不看洞察，它是否仍有回顾价值？

心理咨询、会议、旅行、交付、交易、里程碑、造成后果的错误和实质外部变化可以是事件；普通聊天、普通阅读、短暂感受、自我观察、尚未执行的决定和行为模式不构成事件。完成一本书或到达明确阅读里程碑可以是事件，但「阅读并反思」只能产生作品实体与洞察。

事件标题与 `semantics.action` 必须是同一条规范化后的「发生短语」，只写发生了什么，不夹带解释。`event_basis` 必须由可观察动作支撑：appointment 是参加／接受明确会面或咨询，incident 必须出现具体错误、结果、直接影响或有边界经历，milestone 必须出现完成／达成／开始／结束等阶段变化，transaction 与 material_change 同理。「发生／收到／遇到」等通用词不是正向事实锚点，`发生认知改变`、`收到启发` 仍是洞察。不能仅靠把普通聊天标成 incident 来通过事件门。例如同一 raw 应拆为：

- event：`参加了第 19 次心理咨询`；
- statement：`靠谱应以可验证的责任边界衡量，而不是以事事兜底衡量`；
- edge：用来源支持的关系把洞察关联到该次咨询。

`第 19 次心理咨询重构靠谱标准` 和 `与对象聊天时捕捉表达自我监控` 均不得作为事件标题；`在 7 月 13 日与对象聊天` 虽有日期，但规模和结果不足，不进入时间线；`发送周报时把周会时间写错` 有可观察错误，可按 incident 进入时间线。编译 Agent 必须结合完整短语判断，不能因名词「项目计划会」包含“计划”而误杀真实参会事件。

CLI 只确定性校验 title/action 一致、类别、时间、事实性、来源和结构，不用中文关键词黑白名单充当语义分类器：同一个词既可能是洞察谓词，也可能是「认知科学研讨会」等合法对象名。事件是否真正通过发生性与独立价值门，由编译 Agent 按 Skill 给出语义方案，并由不参与生成的只读审计 Agent 结合 raw 逐条复核；任何只证明 CLI 接受、却不符合发生语义的产物仍判失败。

当一条 raw 没有耐久实体、可回顾事件或可复用洞察时，允许三个 annotation channel、node_actions 和 `belongs_to` 全部为空。raw 仍被标记为已编译并保留在原料层，但不进入图谱与 Consolidation 队列。系统不得为了满足“每条 raw 至少挂一个节点”的形式约束制造微小事件。

### 详情是节点级综合，不是摘要复述

所有新建或实质更新节点的 `content.detail` 必须使用至少两个由空行分隔的实质段落、至少四个互不重复的实质句子，并提供至少三个互不重复且不少于 8 个非空白字符的 `key_points`。CLI 按节点类型执行最低有效字符数：entity 120、event 140、statement 160、topic 240；空白不计入字符数。内容必须覆盖节点全部有效来源中与该节点相关的历史，不能只描述最新 raw。每个详情句必须由该节点自己的 source、evidence、语义字段或历史直接支撑；不同节点不得复用同一段长泛化说明或多个相同长句充当详情。必要的短术语和逐字 evidence 引文可以重复，不参与跨节点模板判定。

| 节点 | 详情必须直接回答的问题 |
|-|-|
| entity | 它是什么？与用户有什么稳定关系？不同来源共同说明了哪些历史、属性和关联？ |
| event | 何时、在什么背景下发生了什么？直接结果是什么？哪些后续洞察与它有关但不属于事件事实？ |
| statement | 当前洞察是什么？哪些观察支持它？机制或认识如何变化？对后续判断和行动意味着什么？ |
| topic | 它用什么组织问题贯穿知识库？各 facet 如何协同？跨时间能看到什么结构？边界在哪里？ |

为让 Wiki 详情可以直接扫读，两个核心段落必须使用固定语义标签开头：entity 为「对象与关系：／历史与现状：」，event 为「发生与背景：／结果与关联：」，statement 为「洞察与依据：／演进与影响：」，topic 为「组织视角：／脉络与边界：」。这不是展示模板，而是内容角色合同；每段必须直接回答标签，不得跨类型复用同一套通用句。

详情不得靠同义反复、通用模板句或复制 raw 凑长度；CLI 确定性拒绝缺段、缺标签、长度不足、节点内完全重复／近似重复句、跨节点重复的长详情句／段和低信息 key point。detail 仍必须覆盖 summary 的中心概念，至少两条 key point 应复用具体概念；「记录当前节点的基本信息／保留相关内容／方便未来查阅」不构成节点知识。跨节点检测比较规范化后达到 24 字的非 evidence 实质句；低于 24 字仍视为必要短术语风险区，不据此单独判重。只有句子去掉白名单前缀「其直接依据是」或「它参与」后，剩余全文与该节点自身某条 `evidence.claim` 规范化后精确相等，才按 evidence 引文排除，不能用包含或后缀匹配放宽。历史存量命中项进入 `semantic_quality.weak_detail`，新 plan 的投影图一旦出现第二次复用则拒绝 apply。单个节点即使没有跨节点重复，只要详情出现「当前节点只确认」「节点仅保留」「节点不把」「后续若出现新的实质信息」「后续实质变化需要」等描述编译器取舍而非节点事实的政策措辞，也进入 `weak_detail`；不得把普通包含“节点”的事实句宽泛判弱。自然语言是否真正具体、是否综合全部来源同样由独立只读审计 Agent 逐节点复核，不能用易误杀的泛化词黑名单替代。缺失信息应进入 `uncertainties`。Wiki 将标签渲染为段落标题，并在健康检查中分别暴露弱详情和旧事件合同，防止“CLI 成功但人类不可读”。

实体 evidence 还必须具体指向该实体：当同一规范化 `evidence.claim` 被多个 entity 复用时，每个 entity 的 claim 必须直接点名其规范化 title、alias 或保守实体词形。不得用宽泛 2-gram、相邻上下文或其他实体的名称推定支撑；未被直接点名的实体进入 `semantic_quality.weak_evidence`。

当没有 rebuild、incremental 或正常 10 条 Consolidation，但 `semantic_quality.weak_detail` 检出跨节点重复详情或单节点编译政策措辞，或 `semantic_quality.weak_evidence` 检出共享却未点名实体的 claim 时，`update` 发出一次 `mode=consolidate`、`batch_size=0`、`quality_repair=true` 的最小质量修复请求，并分别显式列出 weak-detail 与 weak-evidence 节点 ID。该模式不消费 Consolidation 队列，只允许通过对应 session 的正式事务更新现有节点；投影图仍有任一跨节点重复详情、编译政策措辞或弱实体 evidence 时必须拒绝。普通手工空批 Consolidation 永远无效。调度优先级固定为 rebuild → incremental → 正常 Consolidation → quality-repair Consolidation → noop。

> 本文后续章节保留的是早期方案推演，用于理解设计取舍；凡涉及向量索引、V1 产物演进、两记录成主题或与 0／0.1／0.2 节冲突的内容均不属于当前实施范围，不得作为编码依据。

## 0.4 阅读前须知：历史设计假设

本设计在与需求方对齐时，以下三个关键决策被固定为默认假设，若与实际预期不符需先回退本章再改后续：

1. **检索路线 = 结构化关联图谱为主 + 可选本地向量索引为辅（混合方案）**。
   现有一级检索是纯关键词打分（见 [retriever.py](../src/second_memory/retriever.py) 的 `search_level1`），无法支撑「深度语义关联」。但直接引入外部向量库会破坏本项目三条既有哲学红线：CLI 绝不调用 LLM、数据全为本地 Markdown + git 可审计、相同输入产出稳定结果。因此本设计让 embedding 也走两段式协议（CLI 发出待向量化文本块 → Agent/宿主回灌向量 → CLI 落地为可 diff 的辅助索引），既补语义 gap 又不破坏哲学。

2. **本设计是「现有结构的演进」而非「推倒重来」**。
   现有 `raw/ + wiki/{entities,topics,timeline} + index.md + .kb/manifest.json` 骨架保留，本文在其上增量补齐：实体关系边、观点演化结构、聚合动态诞生规则、语义索引层。

3. **产品形态无关**。最终以 Skill + CLI 接入 Agent，但本设计只描述底层通用数据与算法，不绑定调用方形态。

---

## 1. 设计目标与约束

### 1.1 功能目标

| 编号 | 能力 | 本质要求 |
|-|-|-|
| G1 | 主题聚合 | 不是原料堆砌，而是对同一主题下原料做统一整理、总结、提炼，并呈现观点在时间线上的演化 |
| G2 | 实体提取 | 从原文抽离实体（人、书、概念、情绪、项目…）作为检索联想锚点 |
| G3 | 语义关联与智能检索 | 结合「历史记录 + 当下现状」给出真正懂用户的回答 |

### 1.2 架构约束（继承自现有项目哲学，不可降低）

- **C1 原料不可变**：`raw/` 写入后只读（`0o444` + CLI guard），任何修正以新增记录表达，不改历史。
- **C2 CLI 不调 LLM**：所有语义步骤（编译、实体抽取、向量化、深检索）走两段式 `--emit-request` / `--apply-response --stdin`。
- **C3 本地可审计**：所有编译产物是纯文本（Markdown / JSON），可被 `git diff` 审查，不引入黑盒二进制存储作为唯一真相源。
- **C4 确定性**：相同输入产出稳定 ID、稳定排序、稳定字段。
- **C5 可重建**：编译层是原料层的纯函数投影，任何时刻可从 `raw/` 全量重建（`rebuild` / `update --mode rebuild`）。这是版本演进的安全网。

> C5 是整个架构最重要的性质：**原料层是唯一真相源，编译层是缓存**。所有下述新增结构都必须满足「可从 raw 重新推导」，否则不能进编译层。

---

## 2. 分层数据模型总览

```
┌─────────────────────────────────────────────────────────────┐
│ L3 语义索引层 (Semantic Index)  —— 可选、可重建、辅助召回      │
│   .kb/vectors/*.jsonl   实体/主题/时间线块的向量缓存           │
│   .kb/graph.json        关系边的物化快照（供快速遍历）          │
├─────────────────────────────────────────────────────────────┤
│ L2 编译层 (Compiled / Wiki)     —— 结构化知识，可审计可 diff   │
│   wiki/entities/   实体页（含关系边、别名）                    │
│   wiki/topics/     主题聚合页（含观点演化线）                  │
│   wiki/timeline/   日级时间线（review / on-this-day 用）       │
│   index.md         一级轻量语义入口                            │
├─────────────────────────────────────────────────────────────┤
│ L1 原料层 (Raw)                 —— 唯一真相源，不可变          │
│   raw/YYYY/MM/*.md  用户原文 + frontmatter                     │
├─────────────────────────────────────────────────────────────┤
│ L0 编译状态 (.kb)               —— 编译账本                    │
│   pending.jsonl    待编译队列                                  │
│   manifest.json    已编译 raw、页面 hash、kb_version           │
│   config.yaml      运行时配置                                  │
└─────────────────────────────────────────────────────────────┘
```

现状与本设计新增内容对照：

| 层 | 现状（已实现） | 本设计新增 |
|-|-|-|
| L1 | `raw/` 不可变原文 | 不变 |
| L2 | entities / topics / timeline / index | 实体关系边、主题观点演化结构、聚合诞生规则 |
| L3 | 无 | 向量缓存、关系图物化快照 |
| L0 | pending / manifest | 关系边与向量的账本字段 |

---

## 3. 编译层数据结构与存储方案（对应 G1、需求任务 1）

### 3.1 原料层 → 编译层的关联模型

现有关联是**单向 source 引用**：每个编译页在 frontmatter 里记 `sources: [raw-id...]`，指向它由哪些原料提炼而来（见 [compiler.py](../src/second_memory/compiler.py) 的 `upsert_page`）。

本设计保留 source 作为「编译页 → 原料」的**溯源边**，并补齐三类新关联，使编译层从「一堆孤立页面」变成「可遍历的知识图谱」：

```
                      derived_from (sources)
   ┌──────────┐  ────────────────────────────▶  ┌──────────┐
   │ 编译页    │                                  │  raw 原料 │
   │ entity/  │  ◀────────────────────────────   └──────────┘
   │ topic    │        (反向：raw 被哪些页引用)
   └──────────┘
        │  ▲
        │  │ relations (本设计新增：页 ↔ 页 横向边)
        ▼  │
   ┌──────────┐
   │ 其他编译页 │   entity↔entity / entity↔topic / topic↔topic
   └──────────┘
```

三类关联边：

1. **溯源边 `sources`（已有）**：编译页 → raw。保证 C5 可重建、C3 可审计。
2. **横向关系边 `relations`（新增）**：编译页 ↔ 编译页。这是「深度语义关联」的结构基础。
3. **演化边 `evolution`（新增）**：主题页内部，同一观点在不同时间点的版本序列。这是 G1「观点演化」的载体。

### 3.2 实体页数据结构（扩展 `wiki/entities/*.md`）

现有 frontmatter 字段：`id / type / title / aliases / summary / created / updated / sources / entity_kind`。

新增字段（全部可由 LLM 在编译期填充、可从 raw 重推、可 diff）：

```markdown
---
id: entity-charlie-munger
type: entity
entity_kind: person
title: 查理·芒格
aliases: ["Charlie Munger", "芒格", "查理芒格"]
summary: 用户长期阅读与引用的投资思想家，多次作为决策心智模型的来源
created: 2026-03-01T21:10:00+08:00
updated: 2026-07-20T09:00:00+08:00
sources: ["raw-20260301-2110-ab12", "raw-20260620-0900-cd34"]
relations: [{"target": "topic-multidisciplinary-mental-models", "kind": "relates_to", "weight": 5}, {"target": "entity-poor-charlies-almanack", "kind": "appears_in", "weight": 3}]
salience: 0.82
---
<!-- relations 为单行 JSON 数组（weight = 共现/引用强度，用于图谱扩散排序）；salience 为显著度，决定是否进 index.md（见 4.4）。均为单行值，兼容现有 frontmatter 解析器 -->


## 概述
（可复用的稳定事实、关系、用户视角、稳定结论；不复制大段原文）

## 用户视角
（用户对该实体的个人立场、评价、引用场景）
```

`entity_kind` 沿用现有四类并**建议扩展**为 person / project / concept / emotion / work / book（现有 schema 已约束前四类，落地时需在 `promptio.compile_response_schema` 与 `validate_compile_response` 同步扩展，属于 `KB_VERSION` bump）。

### 3.3 主题聚合页数据结构（扩展 `wiki/topics/*.md`）

主题页是 G1 的核心载体。现有结构只有 `summary + body_markdown + sources`，无法表达「观点演化」。新增**演化线（evolution timeline）**结构：

```markdown
---
id: topic-staying-up-late
type: topic
title: 熬夜
summary: 关于熬夜成因的认知，从"自制力问题"演化到"逃避次日工作"
created: 2026-01-05T23:40:00+08:00
updated: 2026-07-10T01:20:00+08:00
sources: ["raw-...", "raw-...", "raw-..."]
relations: [{"target": "entity-work-pressure", "kind": "relates_to", "weight": 4}]
aggregation_kind: cognition_evolution   # 聚合类型，见 3.4
salience: 0.75
---

## 概述
（当前对该主题的最新综合理解）

## 观点演化
<!-- evolution: 每个节点 = 一个时间点上的认知快照，refs 指向原料 -->
- 2026-01-05 | 认为熬夜是**自制力不足** | refs: raw-20260105-2340-xxxx
- 2026-04-12 | 归因转向**工作压力大** | refs: raw-20260412-0100-yyyy
- 2026-07-10 | 最终认知：**在逃避第二天的工作** | refs: raw-20260710-0120-zzzz

## 做法与结论
（用户在该主题下沉淀的可复用做法、结论）
```

「观点演化」以**结构化的 markdown 列表 + 机器可解析的行格式**存储（类似现有 `timeline_line` 的做法），既对人可读、对 git 可 diff，又能被 CLI 解析出结构供检索使用。

### 3.4 聚合类型（aggregation_kind）

需求列举的聚合场景抽象为几类，作为主题页的一个字段，指导 LLM 编译时采用不同的组织模板：

| aggregation_kind | 对应需求场景 | 组织重点 |
|-|-|-|
| `reading_note` | 书籍/人物笔记（查理芒格） | 按书/人聚合，提炼观点与引用 |
| `reflection` | 心理感悟 | 归拢零散感悟，提炼反复出现的主题 |
| `periodic_tracking` | 定期行为（心理咨询） | 按次序追踪，每次一条，呈现连续性 |
| `cognition_evolution` | 认知与体会演化（熬夜） | 强制填充「观点演化」段 |
| `domain_topic` | 特定技术/工作主题（AI 编程） | 按子话题梳理整合 |

这不是硬编码分类，而是给编译期 LLM 的**组织提示**。类型本身也可由 LLM 判定并写回，属于可重建数据。

### 3.5 存储与序列化原则

- 所有编译页仍是 `frontmatter + markdown body`，复用现有 [frontmatter.py](../src/second_memory/frontmatter.py)。注意现有 frontmatter 是**扁平 kv + JSON 值**的极简格式，`relations`/`evolution` 这类嵌套结构需以 JSON 数组存于 frontmatter 值，或以约定行格式存于 body（演化线走 body、关系边走 frontmatter JSON 值）。
- **不引入 YAML 嵌套**：保持现有解析器简单性。关系边用 `relations: [{"target":...,"kind":...,"weight":...}]` 的 JSON 单行值即可被现有 `_parse_value` 处理。
- L3 语义索引层（向量、图快照）存于 `.kb/`，视为**可删除缓存**，不是真相源。删掉后 `rebuild` 能重建。

---

## 4. 主题聚合的生成、更新与演化机制（对应需求任务 2）

### 4.1 聚合的生命周期

聚合不是预先定义的，而是**从原料中动态涌现**。生命周期四阶段：

```
  诞生(Birth) ──▶ 成长(Growth) ──▶ 演化(Evolution) ──▶ 合并/分裂(Merge/Split)
    │               │                  │                    │
  首条相关 raw     新增 raw 命中      观点/认知发生        主题过宽或过窄
  触及新主题       已有聚合           转变                 时触发重组
```

### 4.2 诞生：聚合如何动态产生

沿用现有两段式编译流水线（`add` → `pending.jsonl` → `compile --emit-request` → Agent 推理 → `--apply-response`）：

1. 用户 `add` 一条原料，进入 `pending.jsonl`。
2. `compile --emit-request` 把 pending 原料 + 现有 index 打包成 `llm_request`。
3. **Agent（编译期 LLM）判定**：这条原料应创建或更新哪些 entity、event、statement，并记录不确定的 merge/split candidate。增量阶段不直接创建 topic。
4. `--apply-response` 落地，`manifest.json` 记录消费的 raw，`pending` 出队。

每累计 10 条已编译 raw，由 Consolidation 在有界上下文中判断稳定主题：已有 topic 则补充关系；达到阈值的新主题才创建 `topic-<slug>`；证据不足则继续等待，避免主题爆炸。

关键点：**诞生的决策权在编译期 LLM，CLI 只做确定性的落地与校验**（符合 C2）。CLI 侧通过 `validate_compile_response` 保证 ID 前缀、sources 合法、summary/body 非空。

### 4.3 聚合诞生阈值（避免主题爆炸与碎片化）

给编译期 LLM 的启发式规则（写入知识库的 `AGENTS.md` 编译规则，可调）：

- **书籍/人物**：出现即建实体页；相关记录 ≥ 2 条时，为其建 `reading_note` 主题聚合。
- **认知类主题**：同一主题出现 ≥ 2 个**不同时间点**的记录，且观点有差异 → 建 `cognition_evolution` 聚合并填演化线。
- **单次性内容**：只进 timeline + 实体页，不建主题。
- 阈值以「是否有复用价值、是否会被再次检索」为判断本质，而非机械计数。

### 4.4 显著度 salience 与一级索引

现有 `index.md` 收录所有 entity/topic（见 `list_index_pages`），随着记录增长会膨胀，稀释一级检索质量。本设计引入 `salience`（显著度）：

- salience 由被引用次数、关系边数、时间跨度、最近活跃度综合得出（编译期 LLM 给分或 CLI 按可确定规则算分）。
- `index.md` 只收录 `salience >= 阈值` 的高价值聚合，低显著度页面仍存在于 `wiki/` 但不进一级索引，靠二级检索/图谱扩散触达。
- 这样一级检索保持轻量（呼应现有「index.md 是紧凑语义入口」的设计意图）。

### 4.5 更新与演化

- **增量更新（成长）**：新 raw 命中已有聚合，`upsert` 追加 sources、刷新 summary、必要时向演化线追加一个节点。现有 `apply_response` 的增量路径已支持 upsert，只需扩展演化线的合并逻辑（类似 `upsert_timeline` 的 line 去重合并）。
- **演化（Evolution）**：当新 raw 表达的观点与聚合中已有观点**发生转变**，编译期 LLM 在「观点演化」段追加一个带时间戳的节点，而非覆盖旧观点——保留认知变迁全过程，这正是需求「熬夜认知从 A→B→C」的诉求。
- **合并/分裂（Merge/Split）**：低频但必要的重组。当两个聚合实为一体（别名未识别）或一个聚合过宽，由 Consolidation 在有界上下文中执行。增量编译和 rebuild 重放只能记录候选，不能直接改变结构。

### 4.6 版本化重建作为演化安全网

现有 `manifest.json` 记录 `kb_version` 与页面 content hash，`version_drift` / `manifest_drift` 检测漂移（见 [compiler.py](../src/second_memory/compiler.py) 的 `version_drift`）。当编译规则升级（如新增演化线格式、salience 算法），bump `KB_VERSION`，下次 `update` 自动走 raw-only sequential rebuild。**旧编译产物不进入新编译上下文，演进不留历史包袱**。

版本判断之前必须先同步规则来源。`update --emit-request` 在 Skill／CLI 代码仓库执行 `git pull --ff-only origin master`，并要求更新后的本地 HEAD 与 `origin/master` 完全一致；HEAD 发生变化时通过一次受保护的 re-exec 重新加载 `KB_VERSION` 和编译实现。只有新代码进程可以比较 manifest 版本并决定 `rebuild`。拉取失败、历史分叉或目标 commit 无法确认时返回 `code_update_failed`，不得用旧代码降级执行，也不得修改知识库。代码 commit 变化但 `KB_VERSION` 不变时继续正常调度，不触发无意义重建。

### 4.7 Raw-only sequential rebuild

rebuild 是对正常入库链路的确定性重放，不是旧编译页的数据迁移：

1. 只读取 `raw/` 正文和基础元数据，按 `created`、`raw_id` 稳定排序；`event_date` 不参与重放排序。
2. 第一条 raw 的请求使用空图谱，不提供旧 entity、topic、edge、redirect、candidate 或编译注解。
3. 第一条响应 apply 到隔离的 rebuild workspace；workspace 中所有 raw 的编译器注解会先清空，正文哈希必须保持不变，原知识库此时保持不变。
4. 每轮只处理一条 raw。下一轮的 `existing_nodes` 只来自本次 rebuild 已成功落地的前序轮次，因此实体复用、陈述演化和事件状态变化与真实逐条入库一致。
5. rebuild workspace 的 manifest 保存 ordered raw、已完成 raw 和当前进度；中断后从下一条恢复。`update` 在 rebuild 完成前始终优先返回 `mode=rebuild`。
6. rebuild 重放与 incremental 约束一致：只能创建／更新 entity、event、statement，不能创建 topic 或执行 merge/split。
7. 重放过程中每累计 10 条进入队列的耐久 raw，下一次 rebuild emit 就切换为一个 Consolidation 批次；批次成功消费后再继续下一条 raw。若中断时积压超过一批，则连续消费最早批次。10 条只决定常规审查时机，不保证必须创建 topic；全部 raw 重放完成后，若仍有 1—9 条尾批，必须在隔离 workspace 中再执行一次 final-tail Consolidation，使最终主题阅读审计覆盖全部 raw。普通增量 Consolidation 仍严格要求 10 条，只有 rebuild 的最终尾批允许部分批次。
8. 若最后一次 apply 已写入 workspace、但正式库提升前中断，`update --emit-request` 返回 `mode=finalize` 与 `ready_to_finalize=true`，由 Agent 显式执行 `update --finalize`；emit 本身保持只读，也不得回灌旧 session。
9. 重放完成且 Consolidation 队列清空后，才通过一次事务把完整 workspace 提升为正式 Wiki；尾批审查失败或 session 过期时保持 workspace 与队列不变。最终 manifest 不继承旧 redirect、candidate、memo、page 或 edge。
10. 最终事务还会从 V2 默认值重建 `AGENTS.md`、`.gitignore` 与 `.kb/config.yaml`，仅保留 scope、agent、backend 等部署语义，并把 path 绑定到当前知识库，避免副本继续指向真实 V1。

兼容旧版已提前提升的尾批时，只允许一个可判定恢复分支：manifest 的 rebuild 必须已 `complete` 且 cursor 等于 total，`compiled_raw` 必须与 `ordered_raw_ids` 表示同一完整集合，当前 raw 归档仍与该 ordered 列表一致，Consolidation 队列必须恰为 ordered 列表的 1—9 条后缀，并且 `.kb/pending.jsonl` 为空。此时 `update`／`consolidate` 可补发一次 final-tail Consolidation；成功消费队列后条件自然失效。普通不足 10 条队列，以及 rebuild 后新增或增量编译过 raw 的知识库，不得进入该恢复分支。

验收必须证明新图谱的首轮上下文为空、manifest 无旧 redirect、旧编译页没有被复用，并逐条比对 rebuild 前后的 raw 正文。仅验证最终 CLI 返回成功不算通过。

---

## 5. 实体提取与语义关联、智能检索架构（对应 G2、G3、需求任务 3）

### 5.1 实体提取

实体提取在编译期由 LLM 完成（C2），CLI 负责确定性落地与校验：

- **抽取**：`compile --emit-request` 已把原文交给 LLM，LLM 按 `compile_response_schema` 输出 `entities`，每个含 `entity_kind / title / aliases / summary / sources`。
- **归一（别名消歧）**：同一实体的不同写法（「芒格」「查理·芒格」「Charlie Munger」）通过 `aliases` 归并到同一 `entity-<slug>`。slug 由确定性 `slugify` 生成，保证 C4。编译期 LLM 负责识别别名并复用已有实体 ID（emit_request 里带 `existing_index` 供其对齐）。
- **校验**：CLI 侧 `validate_compile_response` 校验 entity_kind 枚举、id 前缀、sources 合法性。

### 5.2 语义关联：结构化关系图谱（主路径）

「深度语义关联」的第一支柱是**显式关系图谱**，比纯向量更可解释、可审计、可 diff：

- 关系边存于各页 frontmatter 的 `relations`（见 3.2 / 3.3）。
- 图的**物化快照** `.kb/graph.json` 由 CLI 在 apply 阶段从所有页的 relations 聚合生成，供检索时快速遍历（避免每次读全部 md）。graph.json 是可重建缓存（C5）。

```json
{
  "schema": 1,
  "nodes": {
    "entity-charlie-munger": {"type": "entity", "kind": "person", "salience": 0.82},
    "topic-multidisciplinary-mental-models": {"type": "topic", "salience": 0.6}
  },
  "edges": [
    {"src": "entity-charlie-munger", "dst": "topic-multidisciplinary-mental-models", "kind": "relates_to", "weight": 5}
  ]
}
```

- **图谱扩散检索**：给定一个命中的实体，可沿 relations 扩散到相邻实体/主题（1~2 跳），把「聊到查理芒格」自动关联到「多元思维模型」「Poor Charlie's Almanack」。这是纯确定性的图遍历，不需 LLM。

### 5.3 语义关联：可选本地向量索引（辅助路径）

关系图谱解决「已建立关联」的召回，但**用户问法与记录用词不同**时（问「婚姻」，记录写的是「和伴侣的相处」）关键词与图谱都可能漏召回。这里引入向量语义召回作为补充，且**严格遵守 C2/C3**：

**两段式向量化流程**（不破坏「CLI 不调 LLM」）：

```
compile/embed --emit-request
  └─▶ CLI 输出待向量化的文本块清单 (page_id, chunk_id, text)
        │
        ▼
     Agent/宿主调用 embedding 模型，回灌向量
        │
        ▼
embed --apply-response --stdin
  └─▶ CLI 把向量写入 .kb/vectors/<page_id>.jsonl（每行一个 chunk 的 id + 向量）
```

- 向量存为**文本化 JSONL**（float 数组），可被 git 追踪、可删除重建（C3/C5）。虽不适合逐字 diff，但满足「文本、可审计存在性、可重建」。
- **检索时**：`search --level 2` 的 emit_request 增加一步——CLI 先对 query 做同样两段式取到 query 向量（或由 Agent 提供），再在本地 `.kb/vectors/` 做余弦相似度 top-k 召回候选页。相似度计算是纯 Python，无外部依赖、确定性。
- 向量索引是**可选**的：未启用时检索降级为「关键词 + 图谱扩散」，功能不缺失，只是语义召回弱一些。通过 `config.yaml` 的开关控制。

### 5.4 三层混合检索架构（G3 核心）

把现有 level1/level2 演进为三级漏斗，兼顾轻量与深度：

```
Query
  │
  ▼
┌─ Level 1  轻量召回（纯本地、无 LLM、毫秒级）────────────────┐
│  ① 关键词/别名打分（现有 search_level1）                    │
│  ② 图谱扩散（命中实体 → relations 1 跳邻居）  ← 新增        │
│  ③ 向量 top-k（若启用向量索引）              ← 新增(可选)   │
│  → 合并去重，产出候选页 + salience 排序                     │
└────────────────────────────────────────────────────────────┘
  │  （候选足够回答简单问题时到此为止）
  ▼
┌─ Level 2  深度归纳（两段式，Agent 推理）────────────────────┐
│  取 top-N 候选页全文 + 观点演化线 + 必要 raw 片段            │
│  emit_request → Agent 归纳「历史能为当前问题提供的上下文」   │
│  （现有 search_level2_request 演进：候选来源改为三路合并）   │
└────────────────────────────────────────────────────────────┘
  │
  ▼
┌─ Level 3  结合当下现状作答（Agent 侧，本库只供上下文）──────┐
│  Agent 拿 Level 2 的历史上下文 + 当前对话现状，综合作答      │
│  （呼应需求"结合历史记录 + 当下现状，给出懂我的回答"）       │
└────────────────────────────────────────────────────────────┘
```

- Level 1 三路召回全部**本地确定性**，不调 LLM，满足回答前快速检索的低延迟诉求。
- Level 2 才动用 LLM 归纳，控制成本（呼应 SKILL.md「Never send the whole raw archive to the model」）。
- Level 3 是 Agent 的职责，本库只保证「把最相关的个人上下文准确、精炼地喂给它」。

### 5.5 检索质量的关键：写入即索引

每次 `apply_response` 落地时，CLI 同步刷新：`index.md`（现有）、`graph.json`（新增）、`vectors/`（新增、可选）。保证检索面永远与编译层一致，无需单独的重建索引步骤（除非 `rebuild`）。

---

## 6. 演进路径与迁移

分阶段落地，每阶段独立可用、独立可 `rebuild`：

| 阶段 | 内容 | KB_VERSION | 风险 |
|-|-|-|-|
| P1 | 主题页加「观点演化」段 + aggregation_kind + 诞生阈值规则 | bump minor | 低，纯 schema 扩展 |
| P2 | 实体/主题加 relations + graph.json 物化 + Level1 图谱扩散 | bump minor | 中，需扩展 apply/validate |
| P3 | salience + index.md 按显著度收敛 | bump minor | 中，影响一级检索面 |
| P4 | 可选向量索引 + Level1 向量召回 + Level2 三路合并 | bump minor | 中高，引入 embedding 依赖（仅 Agent 侧） |

- 每阶段落地即 bump `KB_VERSION`，用户下次 `update` 自动 `rebuild`，从不可变 raw 平滑迁移到新组织方式，**无数据迁移脚本**——这是 C5 带来的最大工程红利。
- 向后兼容：老编译页缺新字段时，检索侧按缺省值处理（无 relations 即无扩散、无 salience 即默认收录），不阻断。

---

## 7. 与现有代码的落点对照（便于实现）

| 设计项 | 现有代码落点 | 改动性质 |
|-|-|-|
| 观点演化线 | `compiler.upsert_timeline` 的 line 合并可参考 | 新增 topic body 解析/合并 |
| aggregation_kind / relations / salience | `promptio.compile_response_schema`、`validate_compile_response`、`page_from_topic/entity` | 扩展 schema 与校验 |
| graph.json 物化 | `apply_response` 末尾 `refresh_index` 旁 | 新增 `refresh_graph` |
| 图谱扩散召回 | `retriever.search_level1` | 新增第二路召回 |
| 向量两段式 | 新增 `embed` 子命令，复用 `promptio.llm_request` 模式 | 新增 CLI 命令 + `.kb/vectors/` |
| 三路合并 Level2 | `retriever.search_level2_request` | 候选来源改为三路合并 |
| 版本化重建 | `version_drift` / `update` 的 rebuild 分支 | 无需改，天然支持 |

---

## 8. 验证方式

落地任一阶段后，至少通过：

```bash
# 结构自检
python -m compileall -q src
second-memory --help
second-memory status --json          # 核对 kb_version / compiled_kb_version / version_drift

# 可重建性验证（C5 的核心回归）：逐条 emit/apply 直到 rebuild_complete
second-memory rebuild --emit-request --json
# ... Agent 回灌 ...
second-memory rebuild --apply-response --stdin --json

# raw 重放完成后，rebuild 会继续发出有界 Consolidation 请求并最终事务提升
second-memory rebuild --emit-request --json
second-memory status --json          # manifest_drift 应为空

# 检索验证
second-memory search --query "熬夜" --level 1 --json     # 应召回演化聚合
second-memory search --query "婚姻" --level 2 --emit-request --json
```

关键回归点：**同一份 raw 两次 rebuild 产出的 manifest pages hash 应稳定**（验证 C4 确定性）。
