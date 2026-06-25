# Second Memory

面向 Agent 的个人第二记忆库 Skill + CLI。系统用本地 Markdown 文件和 git 管理用户记录，CLI 只做确定性文件、索引、校验和版本操作；需要 LLM 的步骤由宿主 Agent 读取 `SKILL.md` 后按两段式协议执行。

## 适用对象

- Codex、Hermes、Claude Code 等能读取 Agent Skill 文档并执行 shell 命令的 Agent。
- 需要把用户日记、复盘、感悟、长期偏好沉淀到本地知识库的个人场景。
- 需要回答前先参考用户历史记录，或做一周回顾、那年今日回顾的场景。

## 一键安装

```bash
git clone https://github.com/musiczh/second-memory.git
cd second-memory
scripts/setup.sh
```

`scripts/setup.sh` 会执行：

- 创建 `.venv` 并以 editable 模式安装 CLI。
- 安装命令入口到 `~/.local/bin/second-memory`。
- 安装 Codex skill 软链到 `${CODEX_HOME:-~/.codex}/skills/second-memory`。
- 运行 `second-memory --help` 做自检。

如果 shell 找不到 `second-memory`，把 `~/.local/bin` 加到 `PATH`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## 初始化运行时知识库

代码仓库和运行时知识库是两件事：

- 当前仓库：Skill + CLI 代码，用 `git pull` 更新能力。
- 运行时知识库：用户个人数据，默认在 `~/.second-memory/...`，由 CLI 初始化并用 git 管理。

全机共享一个知识库：

```bash
second-memory init --scope shared --json
```

仅当前 Agent 私有：

```bash
second-memory init --scope agent --agent codex --json
```

也可以显式指定知识库路径：

```bash
second-memory init --repo "$HOME/.second-memory/knowledge-base" --scope shared --json
export SECOND_MEMORY_REPO="$HOME/.second-memory/knowledge-base"
```

初始化后检查：

```bash
second-memory status --json
```

## Agent 使用方式

Agent 应先读取仓库根目录的 `SKILL.md`，再按其中工作流执行。核心约束：

- 用户明确说“记一下 / 存入知识库”时，直接入库。
- 日常聊天中发现值得沉淀的内容时，先询问用户确认，再入库。
- 回答可能依赖个人上下文的问题前，先执行 `search --level 1`。
- CLI 不直接调用 LLM；`compile`、`rebuild`、`review`、`update`、`search --level 2` 都使用 `--emit-request` / `--apply-response --stdin` 两段式。

主动入库示例：

```bash
printf '%s' "用户原文" | second-memory add \
  --title "工作复盘" \
  --event-date 2026-06-25 \
  --stdin \
  --json

second-memory compile --emit-request --json
```

Agent 读取 `data.llm_request`，自己生成符合 `response_schema` 的 JSON 后回灌：

```bash
printf '%s' "$LLM_RESPONSE_JSON" | second-memory compile --apply-response --stdin --json
```

回答前检索：

```bash
second-memory search --query "职业规划 焦虑" --level 1 --json
```

需要深度检索时：

```bash
second-memory search --query "职业规划 焦虑" --level 2 --emit-request --json
```

回顾：

```bash
second-memory review --range last-week --emit-request --json
second-memory review --on-this-day 2026-06-25 --emit-request --json
```

系统更新：

```bash
second-memory update --emit-request --json
```

## 更新 Skill / CLI 代码

在代码仓库中执行：

```bash
cd /path/to/second-memory
git pull --ff-only
scripts/setup.sh
second-memory --help
```

因为 Codex skill 是软链到代码仓库，`git pull` 后重新运行 `scripts/setup.sh` 即可更新 CLI 依赖和 `~/.local/bin/second-memory` 入口。

其他 Agent 如果从 GitHub 链接安装，也使用同样流程：拉取最新代码、重新运行 `scripts/setup.sh`、重新读取 `SKILL.md`。

## 更新或同步本地知识库数据

运行时知识库本身也是 git 仓库。先进入知识库目录：

```bash
cd "${SECOND_MEMORY_REPO:-$HOME/.second-memory/knowledge-base}"
git status
git log --oneline -5
```

如果用户想把个人知识库同步到自己的私有远程仓库，可在运行时知识库里配置单独 remote：

```bash
git remote add origin git@github.com:<owner>/<private-memory-repo>.git
git push -u origin rc/develop
```

之后更新本地知识库数据：

```bash
git pull --ff-only
second-memory status --json
```

注意：不要把用户个人知识库数据推到公开的 CLI/Skill 代码仓库。代码仓库只存工具代码；个人 raw/wiki 数据应保存在本地或用户自己的私有仓库。

## 目录与数据模型

运行时知识库结构：

```text
knowledge-base/
├── raw/                    # 原料层，用户原文，写入后不可修改
├── wiki/
│   ├── entities/           # 实体页
│   ├── topics/             # 主题页
│   └── timeline/           # 日级时间线，review 专用
├── index.md                # 一级检索入口，只包含 entity/topic
├── AGENTS.md               # 编译与检索规则
└── .kb/
    ├── config.yaml
    ├── pending.jsonl
    └── manifest.json
```

`timeline` 是一天一个页面，但不会进入 `index.md`；这样一级检索保持轻量，回顾流程仍可直接读取时间线。

## 验证命令

```bash
second-memory --help
second-memory status --json
python -m compileall -q src
```
