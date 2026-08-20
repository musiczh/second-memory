# 原料向量化召回实施计划

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 在完全保留现有关键词／图谱检索结果和顺序的前提下，为 Raw 原料增加可降级、可重建、可事务回滚的本地 BGE 向量召回，并将召回证据接入搜索、Topic 组织和 Wiki 展示。

**Architecture:** 编译请求先把不可变 Raw body 原子化，由 Agent 返回 `summary_segments` 与 `body_groups`，CLI 验证后持久化 `body_sections`。独立的向量层把 headline、summary segment 和 section 内正文切成不重复存储文本的定位单元，使用固定 CPU 的 FastEmbed BGE 模型生成归一化 float32 向量，写入 Git 忽略的 `.kb/vectors/` 缓存。核心编译事务在向量构建成功时一并切换缓存，失败时只标记 `pending/stale` 而不阻断核心 Apply；搜索保持原有关键词／图谱输出不变，仅追加 `supplemental_raw`。

**Tech Stack:** Python 3.11+、Typer、标准库 JSON／hashlib／pathlib、`fastembed==0.8.0`、`unittest`。

**Spec:** 飞书技术方案《第二记忆 · 原料向量化召回技术方案（V2.5）》revision 43：<https://bytedance.larkoffice.com/docx/Dl6tdJz4Eor79qxoyulcTRlDn2d>

---

## 全局约束与成功标准

- 只向量化 Raw；现有 entity／event／statement／topic 关键词和图谱检索不得改分数、改序或被向量结果替代。
- Raw body 字节不可变；所有 offset 使用 Python Unicode code point 索引并可回切原文。
- 默认模型固定为 `BAAI/bge-small-zh-v1.5`、512 维、CPU、float32、有限值、L2 归一化；运行时不得静默切换模型或远端 Provider。
- `vectors reindex` 可以下载模型；普通 Apply、search、status 必须 local-only。模型或缓存不可用时核心链路成功，向量状态明确降级。
- `.kb/vectors/` 和 `.kb/eval/` 不进入 Git 提交；核心事务仍只提交受控 Markdown 与 `.kb` 元数据。
- 每个任务都必须先运行指定测试观察失败，再实现并运行通过；完成后检查 `git diff`，新增文件执行 `git add`，按仓库历史风格提交中文 Conventional Commits。
- 不修改用户已有未跟踪文件，特别是 `uv.lock`、`description.md`、`.agents/` 与现有设计稿。

### Task 1：语义标注协议与确定性切片

**Files:**

- Create: `src/second_memory/chunking.py`
- Create: `tests/test_chunking.py`
- Modify: `src/second_memory/compiler.py`
- Modify: `src/second_memory/promptio.py`
- Modify: `tests/helpers.py`
- Modify: `tests/test_semantics.py`

**Step 1：写失败测试**

覆盖以下行为：

- Markdown 标题、段落、列表项优先形成 atom；句末标点作为二级边界；单 atom 超过 300 code point 时硬切。
- atom ID 由序号、offset 和文本 hash 稳定生成；拼接全部 atom 文本必须与原 body 完全一致。
- compile request 中每个 raw 携带 `body_atoms`，包含 `id/start/end/text`，offset 可精确切回原文。
- Raw annotation 必须包含 60～100 字符 headline `summary`、至少一个 50～300 字符的 `summary_segments`（全文过短例外）和覆盖全部 atom 的 `body_groups`。
- 显式 `body_groups` 必须连续、有序、无重复、无遗漏且不跨越 atom；空数组触发确定性分组。
- Apply 持久化 `summary_segments` 与只含 `start/end/source` 的 `body_sections`，且 body hash 前后相同。

运行：

```bash
.venv/bin/python -m unittest tests.test_chunking tests.test_semantics -v
```

预期：新测试因缺少模块、schema 字段或校验而失败。

**Step 2：实现 atomizer 与 section builder**

在 `chunking.py` 提供纯函数和不可变数据结构：

- `atomize_body(body: str) -> list[BodyAtom]`
- `validate_body_groups(atoms, groups) -> list[list[str]]`
- `default_body_groups(atoms, target=300, minimum=50, maximum=300) -> list[list[str]]`
- `sections_from_groups(body, atoms, groups) -> list[dict]`
- `annotation_hash(title, summary, summary_segments, body_sections) -> str`

所有算法仅依赖输入文本与配置；不得调用模型或文件系统。

**Step 3：扩展 CompilePlan 协议**

- `build_compile_request` 为每条 Raw 输出 atoms，并把 contract 版本更新为 V2.5。
- `compile_response_schema` 增加 `summary_segments` 与 `body_groups` 的必填声明。
- `RAW_COMPILED_FIELDS` 增加新字段；`validate_compile_plan` 校验 headline、segment 和 group；`build_raw_annotations` 将 groups 编译成 sections。
- 对测试 helper 和语义测试中的合法 annotation fixture 补齐新字段，不降低原有语义校验。

**Step 4：验证并提交**

```bash
.venv/bin/python -m unittest tests.test_chunking tests.test_semantics tests.test_integration -v
git diff --check
git add src/second_memory/chunking.py src/second_memory/compiler.py src/second_memory/promptio.py tests/test_chunking.py tests/helpers.py tests/test_semantics.py
git commit -m "feat(compile): 增加原料语义分段协议" -m "将 Raw body 确定性原子化并校验 Agent 返回的 summary_segments 与 body_groups。\nApply 持久化稳定 body_sections，同时保持原文 hash 不变。"
```

### Task 2：Embedding Provider 与 V2.5 配置

**Files:**

- Create: `src/second_memory/embedding.py`
- Create: `tests/test_embedding.py`
- Modify: `src/second_memory/config.py`
- Modify: `pyproject.toml`
- Modify: `scripts/setup.sh`（仅当现有 editable install 无法安装项目依赖时修改）
- Modify: `README.md`

**Step 1：写失败测试**

覆盖：

- `EmbeddingSpec` 固定 provider、model、dimension、dtype、normalization、runtime 和 model hash。
- fake backend 下 `embed_passages`／`embed_query` 返回 shape 正确的 512 维 float32 有限向量并做防御性 L2 归一化。
- 维度错误、NaN／Inf、零范数均抛出明确向量错误。
- `FastEmbedProvider(local_files_only=True)` 不允许下载，强制 `providers=["CPUExecutionProvider"]`，passage/query 分别调用 FastEmbed 对应 API。
- 默认配置包含技术方案列出的 10 个向量字段，`KB_VERSION == "2.5.0"`。

运行：

```bash
.venv/bin/python -m unittest tests.test_embedding -v
```

预期：缺少 provider 与配置时失败。

**Step 2：实现最小 Provider 抽象**

- 定义 `EmbeddingProvider` Protocol、`EmbeddingSpec` 和 `EmbeddingError`。
- `FastEmbedProvider` 延迟导入 `fastembed.TextEmbedding`，显式传入 CPU Provider 和 `local_files_only`。
- 使用固定版本内部已确认的模型目录定位实际 ONNX 文件并计算 SHA-256；若无法定位实际模型文件则拒绝把缓存标为 ready。
- 统一把输出转为 Python float／float32 可表示值，检查维度与有限性并归一化。

**Step 3：锁定依赖与配置**

- `pyproject.toml` 增加精确依赖 `fastembed==0.8.0`，不引入 `qdrant-client`、`sentence-transformers` 或远端 SDK。
- `default_config` 加入方案默认值；配置读取对旧库缺失字段使用默认值，但不覆盖用户显式配置。
- bump `KB_VERSION` 到 `2.5.0`。
- README 说明模型下载边界与 CPU 运行约束。

**Step 4：安装本地项目依赖、验证并提交**

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest tests.test_embedding tests.test_update -v
git diff --check
git add src/second_memory/embedding.py src/second_memory/config.py tests/test_embedding.py pyproject.toml README.md
git commit -m "feat(vector): 接入本地 FastEmbed Provider" -m "锁定 fastembed 0.8.0 与 BGE 中文小模型，强制 CPU、512 维和归一化校验。\n补齐 V2.5 向量配置并限制普通运行只读取本地模型。"
```

### Task 3：向量缓存、索引状态与确定性召回

**Files:**

- Create: `src/second_memory/vectors.py`
- Create: `tests/test_vectors.py`
- Modify: `src/second_memory/compiler.py`
- Modify: `src/second_memory/config.py`

**Step 1：写失败测试**

使用 fake embedding provider 和临时知识库覆盖：

- headline、summary segment、body section 内按 target/max 300、min 50、15% overlap 切片，绝不跨 section；短全文例外。
- chunk ID 在相同输入下稳定；summary 单元使用 `segment_index`，body 单元使用 `start/end`，JSONL 不重复正文。
- manifest 记录 schema、provider/model/model hash/spec fingerprint、输入 annotation/body hash、raw 文件指纹与计数。
- 任一 JSONL 缺失、损坏、维度不匹配、输入 hash 或配置不一致时，整体状态分别为 `missing/corrupt/stale`，且不返回部分结果。
- query 结果按 score 降序、chunk_id 升序稳定排序；先 scan 30，再 threshold 0.35，再 unit 最多 10、Raw 去重最多 5。
- disabled／pending／stale／corrupt 时结果为明确状态和 reason，`units/raws` 均为空。

运行：

```bash
.venv/bin/python -m unittest tests.test_vectors -v
```

预期：缺少向量缓存实现时失败。

**Step 2：实现缓存数据模型与切片**

在 `vectors.py` 实现：

- `VectorCacheState`、`VectorUnit`、`VectorSearchResult`。
- 根据 Raw annotation 定位单元文本的纯函数；读取时用原 body + offset 还原文本。
- `.kb/vectors/manifest.json` 与 `.kb/vectors/raw/<raw_id>.jsonl` 的解析、校验和原子临时目录构建。
- JSONL 每行保存定位信息、向量与必要 hash，不保存 `text`。

**Step 3：实现 reindex、状态和搜索**

- `reindex_vectors(repo, provider, offline=False, raw_ids=None, destination=None)`：offline 时缺模型直接失败；普通 reindex 允许下载。
- `vector_status(repo)`：仅做本地文件与 fingerprint 校验，不初始化会下载的 provider。
- `search_vectors(repo, query, provider=None)`：只在 ready 时 local-only 初始化 provider；严格执行 scan／threshold／limit／dedupe。
- main manifest 的 `raw_hashes[raw_id]` 增加 `annotation_hash`，drift 检测能区分 body 与 annotation 变化。

**Step 4：验证并提交**

```bash
.venv/bin/python -m unittest tests.test_vectors tests.test_chunking tests.test_integration -v
git diff --check
git add src/second_memory/vectors.py src/second_memory/compiler.py src/second_memory/config.py tests/test_vectors.py
git commit -m "feat(vector): 增加可校验的原料向量缓存" -m "为 Raw headline、摘要段和正文切片建立稳定 JSONL 索引与 manifest 指纹。\n缓存缺失、陈旧或损坏时整体降级，召回结果执行固定阈值、扫描和去重规则。"
```

### Task 4：向量缓存事务与 Apply／Rebuild 接入

**Files:**

- Modify: `src/second_memory/transaction.py`
- Modify: `src/second_memory/compiler.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_update.py`
- Create: `tests/test_vector_transaction.py`

**Step 1：写失败测试**

覆盖：

- `KnowledgeTransaction` 支持 `vectors.next`／`vectors.previous`；成功 promote 原子切换，rollback 恢复旧缓存。
- 向量缓存原本不存在时 rollback 不残留新目录。
- 增量 Apply 只为本次变更 Raw 构建 delta，成功时与 Wiki／manifest 同事务提交。
- 模型缺失、推理异常、缓存构建失败不回滚核心 Apply，返回 `pending/stale` 和 reason，且不会保留半成品。
- 事务 journal 处于 prepared／promoting／promoted／corrupt 时 recovery 同时处理向量目录。
- raw-only rebuild 的逐条 workspace Apply 不生成向量；最终 promotion 后只尝试一次全量 reindex，失败仍完成核心 rebuild。

运行：

```bash
.venv/bin/python -m unittest tests.test_vector_transaction tests.test_integration tests.test_update -v
```

预期：现有事务不管理向量目录，测试失败。

**Step 2：扩展事务协议**

- `prepare(include_vectors=...)` 在需要时备份现有向量目录，建立 next 目录。
- 只有完整、ready 的 next 缓存可以参与 promote；目录切换使用同文件系统 `os.replace`。
- journal 记录向量原始存在性与是否已切换；rollback／corrupt recovery 依据实际目录状态恢复。
- `finalize` 只清理事务私有临时目录，不删除当前或 previous 中唯一可恢复副本。

**Step 3：接入 Apply 与 rebuild**

- 核心图谱、Raw annotation、manifest 先在内存确定，再 local-only 尝试构建向量 next。
- 成功则同事务 promote；失败则不触碰现有 ready 缓存，并让 annotation hash 变化自然使其变为 stale，或在无缓存时为 pending。
- Apply 返回 `vector_status` 和 reason；Git commit path 不包含 `.kb/vectors`。
- rebuild workspace 显式禁用逐条 reindex；finalize 完成核心 promotion 后调用一次全量 reindex，并返回结果。

**Step 4：验证并提交**

```bash
.venv/bin/python -m unittest tests.test_vector_transaction tests.test_integration tests.test_update -v
git diff --check
git add src/second_memory/transaction.py src/second_memory/compiler.py tests/test_vector_transaction.py tests/test_integration.py tests/test_update.py
git commit -m "feat(vector): 将向量缓存纳入知识库事务" -m "扩展事务目录切换和恢复逻辑，使完整向量缓存与核心投影原子晋升。\n模型或推理失败仅将向量层降级，不阻断 Compile Apply 与 raw-only rebuild。"
```

### Task 5：检索补充结果、CLI 与更新决策

**Files:**

- Modify: `src/second_memory/retriever.py`
- Modify: `src/second_memory/cli.py`
- Modify: `src/second_memory/compiler.py`
- Create: `tests/test_vector_cli.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_update.py`

**Step 1：写失败测试**

覆盖：

- `search_level1` 的既有 `candidates`、`hits` 与序列化顺序在启用／禁用向量时逐字节一致，只新增 `supplemental_raw`。
- `supplemental_raw` 固定包含 `status/reason/units/raws`，ready 时 Raw 元数据不泄露整篇 body。
- Level 2 request 加入 top vector units 和 bounded Raw metadata／命中片段，但保留原 candidate pages。
- `vectors reindex --json`、`vectors reindex --offline --json`、`vectors search --query ... --json` 输出统一 envelope 与错误码。
- `status --json` 增加 provider/model/dimension/cache state/count/reason。
- `determine_update_mode` 保持 rebuild > incremental > consolidate > repair > noop 的原优先级，并额外返回 `vector_reindex_required`，不得因为向量陈旧改变核心 mode。

运行：

```bash
.venv/bin/python -m unittest tests.test_vector_cli tests.test_integration tests.test_update -v
```

预期：CLI 子命令和 supplemental 字段缺失时失败。

**Step 2：实现兼容检索输出**

- 保留原关键词检索逻辑和排序代码不动，在返回前独立计算向量补充结果。
- Raw 聚合记录 `raw_id/title/event_date/best_score/matched_units`；units 记录定位、分数和不超过单元边界的 snippet。
- Level 2 只扩充可追溯证据，不把整个 Raw archive 发送给 Agent。

**Step 3：实现 CLI 和状态**

- 建立 `vectors_app = typer.Typer()` 并挂到主 app。
- reindex 默认允许首次下载，`--offline` 强制 local-only；search 始终 local-only。
- status 和 update 复用同一 `vector_status`，不重复实现状态判断。

**Step 4：验证并提交**

```bash
.venv/bin/python -m unittest tests.test_vector_cli tests.test_integration tests.test_update -v
git diff --check
git add src/second_memory/retriever.py src/second_memory/cli.py src/second_memory/compiler.py tests/test_vector_cli.py tests/test_integration.py tests/test_update.py
git commit -m "feat(search): 追加 Raw 向量召回与管理命令" -m "保持关键词和图谱候选完全不变，仅在搜索协议追加 supplemental_raw。\n增加 vectors reindex/search、status 状态和非阻断的重建提示。"
```

### Task 6：Topic 证据、Wiki 展示与 Skill 协议

**Files:**

- Modify: `src/second_memory/compiler.py`
- Modify: `src/second_memory/wiki.py`
- Modify: `src/second_memory/templates/wiki.html`
- Modify: `SKILL.md`
- Modify: `docs/compile-layer-architecture.md`
- Modify: `tests/test_topics.py`
- Modify: `tests/test_wiki.py`

**Step 1：写失败测试**

覆盖：

- consolidation/topic request 在核心 catalog 之外新增 `vector_support_catalog`，其内容仅来自 ready 缓存和 bounded top units。
- 向量候选不会自动进入 topic member、不会改变最少成员／statement／raw session 等 TopicContract 校验。
- 向量不可用时 catalog 明确为空并携带状态，不阻断请求。
- Wiki Raw 详情展示 headline 和有序 `summary_segments`，旧 Raw 缺字段时兼容单 summary。
- `SKILL.md` 明确规定：提出新的组织问题时，Host Agent 先执行 `second-memory vectors search`，召回只作为候选证据，成员仍须通过原 TopicContract。

运行：

```bash
.venv/bin/python -m unittest tests.test_topics tests.test_wiki -v
```

预期：协议、模板和断言缺失时失败。

**Step 2：接入 Topic 证据**

- 为现有 consolidation/topic request 构造器增加稳定的 `vector_support_catalog` 字段，不修改 `member_catalog/raw_catalog`。
- catalog 只携带 Raw ID、定位、短 snippet、score 和 cache status；不自动生成 action 或 edge。

**Step 3：更新 Wiki、Skill 与架构文档**

- Wiki 使用 headline 作为短标题摘要，segments 作为可扫描摘要正文。
- `SKILL.md` 更新 V2.5 compile response 字段、向量搜索步骤、降级边界和 Topic 使用规则。
- 架构文档补充 atomize → annotate → chunk → embed → cache → supplemental retrieval 流程和事务边界。

**Step 4：验证并提交**

```bash
.venv/bin/python -m unittest tests.test_topics tests.test_wiki tests.test_integration -v
git diff --check
git add src/second_memory/compiler.py src/second_memory/wiki.py src/second_memory/templates/wiki.html SKILL.md docs/compile-layer-architecture.md tests/test_topics.py tests/test_wiki.py
git commit -m "feat(topic): 接入向量候选证据与分段摘要" -m "为 Topic 与 Consolidation 增加只读 vector_support_catalog，不改变成员契约。\nWiki 展示 headline 和摘要段，并在 Skill 中固化新的检索与标注协议。"
```

### Task 7：离线评测与真实模型烟测

**Files:**

- Create: `src/second_memory/vector_eval.py`
- Create: `tests/test_vector_eval.py`
- Modify: `src/second_memory/cli.py`
- Modify: `README.md`

**Step 1：写失败测试**

覆盖：

- 读取 `.kb/eval/vector-gold.jsonl`，每行包含 query、relevant_raw_ids 和可选 expected_units。
- 对 keyword baseline、vector、union 分别计算 Recall@5、MRR、nDCG@5、noise rate、zero-result rate。
- 相同分数与输入产生确定性输出；空 gold、非法 raw ID、重复 query 返回明确校验错误。
- 支持关闭 headline／summary／body 三类单元做 ablation，不改变线上缓存。
- `vectors evaluate --gold ... --json` 只读知识库，不下载模型；缓存非 ready 时明确失败。

运行：

```bash
.venv/bin/python -m unittest tests.test_vector_eval -v
```

预期：评测模块与命令缺失时失败。

**Step 2：实现纯评测函数与 CLI**

- 指标函数与文件解析保持纯函数，线上 retriever 只作为输入来源。
- union 保留关键词排序，再追加未出现的向量 Raw；不得为了评测改线上排序。
- 输出每条 query 的排名证据和汇总指标，所有数值使用稳定小数精度。

**Step 3：真实模型 smoke test**

使用独立临时缓存初始化 `BAAI/bge-small-zh-v1.5`，验证 query/passages 均为 512 维、有限、归一化。若当前网络或模型缓存不可用，记录为环境阻塞，不能把 fake provider 测试当成真实模型通过。

```bash
.venv/bin/python -m unittest tests.test_vector_eval -v
.venv/bin/python - <<'PY'
from second_memory.embedding import FastEmbedProvider
p = FastEmbedProvider(local_files_only=False)
print(p.spec)
print(len(p.embed_query("如何改善睡眠拖延？")))
print(len(p.embed_passages(["睡前未完成工作的压力会延后入睡。"])[0]))
PY
```

**Step 4：提交**

```bash
git diff --check
git add src/second_memory/vector_eval.py src/second_memory/cli.py tests/test_vector_eval.py README.md
git commit -m "feat(vector): 增加离线召回评测" -m "对关键词、向量和并集输出 Recall、MRR、nDCG、噪声率与零结果率。\n支持按向量单元类型做消融，并保持评测流程只读且不触发模型下载。"
```

### Task 8：全量回归、代码收敛与交付审查

**Files:**

- Modify: 仅限前七个任务产生且经审查确认需要修正的文件
- Create: 不新增功能文件

**Step 1：运行静态与全量测试**

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests -v
```

预期：全部通过，且原 172 个基线测试无回归。

**Step 2：运行 CLI 全链路 smoke**

在临时知识库执行 init → add → compile emit request；构造合法 V2.5 response 后 Apply；执行 status、search level 1、vectors search、vectors reindex --offline。检查：

- Raw body hash 不变，annotation hash 存在。
- 关键词 candidates/hits 与关闭向量时一致。
- 无模型时 Apply 仍成功并返回降级状态；有模型时缓存 ready 且向量结果可回切原文。
- `.kb/vectors/` 没有进入 Git staged/commit path。

**Step 3：审查全部本地改动**

```bash
git status --short
git log --oneline --decorate -10
git diff origin/master...HEAD --stat
git diff origin/master...HEAD -- src tests README.md SKILL.md docs/compile-layer-architecture.md pyproject.toml
```

逐项确认：无无关格式化、无新远端依赖、无主线程／CLI 隐式下载、无 body 泄漏、无 partial cache 召回、无 Topic 自动入会、无用户未跟踪文件被纳入提交。

**Step 4：处理审查意见并形成最终修复提交**

仅在存在实际问题时新增提交：

```bash
git add <reviewed-files>
git commit -m "fix(vector): 收敛向量召回全链路边界" -m "根据全量回归与代码审查修正已确认的问题，保持核心编译和关键词检索兼容。"
```

**Step 5：最终验收证据**

再次运行：

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests -v
git status --short
```

记录测试数量、耗时、真实模型 smoke 结果、分支提交列表、未跟踪文件保持情况以及无法验证项。
