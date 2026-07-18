# CLAUDE.md — 仓库说明与协作规范

## Git 提交规范（含自动提交）

本仓库所有 Git 提交——无论是人工提交、AI 自动提交，还是 delegation 场景下的子 Agent 提交——都必须遵守 [`docs/standards/git-workflow.md`](docs/standards/git-workflow.md)。

该文档是本仓库 Git 提交的**正式规范**，优先级高于口头习惯、历史示例或任何外部 skill；外部 skill 仅作为执行提示，不替代该文档。

### 自动提交必须遵守的硬性约束

- **提交范围最小化**：一个 commit 只包含当前任务直接相关的文件。仓库中存在其他未提交改动时，只能 stage 与当前任务直接相关的文件，不得把用户已有的无关改动、顺手修复或脚手架噪音混入同一提交。
- **先验证再提交**：提交前完成与改动相关的最小验证（文档至少检查路径与引用，代码至少跑相关 lint / typecheck / test）。执行过 `format` / `lint` / `typecheck` / `test` 等可能改写文件的命令后，必须把这些命令前后的工作区变化纳入提交范围检查，不能只凭最初 diff 估计提交文件集合。
- **提交前后各检查一次工作区**：`git add` 前必须再执行一次 `git status --short`，确认没有遗漏由格式化或验证命令带出的任务相关文件；提交后再次确认工作区中不存在本次格式化 / 校验 / 验证命令遗留的当前任务相关改动。
- **成功标准**：自动提交成功不能只看「产生了一个新 commit」或「HEAD 发生变化」。若提交后仍存在当前任务相关的未提交文件，该次提交视为未完成，必须继续处理或明确中止，不得直接宣布成功。
- **默认停留在当前分支**：未经用户要求，不创建、不切换、不删除分支；不执行 `git push`、`merge`、`rebase`、`tag`，也不改写历史（`commit --amend`、强推、`reset`），这些操作仅在用户明确要求时执行。
- **中文 Conventional Commits**：提交标题格式为 `type: 简要描述`。`type` 用英文小写（`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `style` / `perf` / `build` / `ci` / `revert`），描述用简体中文，聚焦「做了什么」。**禁止使用 scope**，统一为 `type:`，模块 / 范围信息体现在描述中。
- **Issue 关联**：任务直接来自 Issue 时，在 commit 正文尾部追加 `Refs: #<issue-id>`，单独成行。

完整规则、好 / 坏示例与边界说明见 [`docs/standards/git-workflow.md`](docs/standards/git-workflow.md)。
