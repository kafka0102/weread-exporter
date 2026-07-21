---
name: matt-dev-workflow
description: 把常规开发任务串成一条「前半交互、后半无人」的闭环 —— /grill-with-docs 厘清需求后，用户一句 go 即自动出 spec、拆 tracer-bullet tickets、按 frontier 逐张 /implement（/tdd + /code-review + commit），最后汇总交用户验收。
---

# Matt Dev Workflow

把 Matt Pocock 技能族的主流程（idea → ship）串成一条**前半交互、后半无人**的闭环：`/grill-with-docs` 厘清需求 → 用户一句 "go" → spec / tickets / 实现 / 提交全部自动完成 → 汇总交用户做最终验收。

适用**常规、单会话级**开发任务。超大雾化任务用 `/wayfinder`；只澄清无代码计划用 `/grill-me`。

## 前置

- 假设 `/setup-matt-pocock-skills` 已跑过（仓库存在 `docs/agents/issue-tracker.md` 或 `## Agent skills` 块）。没有 → **停下**，让用户先跑它，不自行补。
- tracker 形态（本地 `.scratch/` / GitHub / …）以 `docs/agents/issue-tracker.md` 为准，照其约定落盘。

## 阶段 0 — 交互式厘清（grill-with-docs）

运行 `/grill-with-docs`，完整跑完 `grilling` + `domain-modeling`：

- 一次一问、每问给推荐答案；代码能查的事实自己查，决策才问用户。
- 边问边把术语写进 `CONTEXT.md`、把难逆决策写成 ADR。
- **不实施**，直到用户确认「已达成共识」。

这是本流程**唯一的交互阶段**。grilling 中若冒出「需要跑一下才能回答」的问题（状态 / 逻辑 / UI 必须看），停下，建议 `/handoff` 出去 + `/prototype` 再 `/handoff` 回 —— 这是对无人化的合理打断，交用户决定。

**完成判定**：用户明确确认共识并给出 "开始 / go"。否则停在 grill。

## 切入无人阶段（go 之后）

用户一句 "go" 即把控制权交给自动阶段。**此后不再向用户提问**，除非命中「停止条件」。

进入前做两件一次性准备：

1. **落共识锚点**：把 grill 结论压成一句话目标 + 关键决策清单，作为 spec / tickets / 子代理的共同起点（spec 路径是它们的实际锚点）。
2. **定分支**：
   - 当前已在 **worktree 分支**（`git worktree list` 中 cwd 命中某 linked worktree 路径，即会话已处于独立分支）→ 就地提交，不开新分支。
   - 否则（在主仓库默认分支上）→ 先创建一个 feature 分支再开始，整条流程都在此分支提交。
   - 未经用户要求不 push / merge / rebase / tag / 改写历史。

## 阶段 1 — 自动出 spec（to-spec，非交互）

运行 `/to-spec` 的流程，但**覆盖其 "check seams with user"**：不停下问用户，按其规则自动决策 ——

- 选**最高**既有 seam，能用一个就不开新 seam；确需新 seam 时在最高点提议。
- 把所选 seam、理由、以及「若用户复核后想改可改」的假设写进 spec 的 Testing Decisions。

其余步骤（探索代码、用 glossary 词汇、遵守相关 ADR、套 spec 模板、发布、打 `ready-for-agent`）原样执行。

**完成判定**：`.scratch/<slug>/spec.md` 已落盘、含 seam 决策、已打 `ready-for-agent`。

## 阶段 2 — 自动拆 tickets（to-tickets，非交互）

运行 `/to-tickets` 的流程，但**覆盖其 "quiz the user"**：不停下问用户，按 tracer-bullet 规则自动决策 ——

- 每张是纵向完整切片（schema / API / UI / tests 全打通）、可独立验收、单上下文可装下。
- 先做 prefactoring 再切片；宽重构走 expand–contract。
- 自动给阻塞边，得出 frontier 顺序（blockers 全 done 的票优先；线性链即从 `01` 起）。
- 把粒度理由 + 假设写进各 ticket / 汇总。

其余步骤（glossary 词汇、遵守 ADR、一票一文件、`ready-for-agent`）原样执行。不关不改任何父 issue。

**完成判定**：`.scratch/<slug>/issues/NN-*.md` 全部落盘、阻塞边齐全、frontier 顺序已定。

## 阶段 3 — 按 frontier 逐张实现（implement，每票一新子代理）

frontier 上每张 ticket，**派一个全新子代理**（独立上下文，互不污染）实现：

- 读 `spec.md` + 本 ticket 文件出发。
- 按 spec 指定的 seam 走 `/tdd`（红-绿-重构，一片一片来）。
- 定期类型检查、定期单测、收尾全量测试。
- 跑 `/code-review`（Standards + Spec 两轴）。
- 在阶段 0 定的分支上 commit。
- 回报：交付了什么、测试状态、review 结论、commit、改动文件。

一张 green 后再起下一张；阻塞边未满足的票不启动。ticket 之间天然清上下文（一票一子代理即此意）。

**完成判定**：frontier 上每张票都回报 green（测试通过 + review 通过 + 已 commit）。

## 停止条件（命中即停下交用户，绝不强行推进）

- grill 决策需 runnable 答案 → 已在阶段 0 处理。
- 前置 setup 未就绪 → 停，让用户先跑 setup。
- grill → tickets 阶段逼近 smart zone（~120k）→ `/handoff`，在 fresh session 续跑，不降级硬撑。
- 某张 ticket：测试红且子代理无法在范围内修好，或 `/code-review` Standards / Spec 不过且子代理无法自决修复 → **停整个自动阶段**，上浮该票 + 失败原因 + 已 commit 的前序结果，交用户裁决。不跳过、不强提交坏代码、不留 `@ts-ignore` / `@ts-nocheck` / `eslint-disable` / 跳过测试。

## 最终验收（汇总交用户）

自动阶段结束（全部 green，或被停止条件打断）后，一次性汇总交用户做最终验收：

- spec 路径、tickets 清单 + 各自状态。
- 每张票：交付内容、测试 / review 结论、commit、改动文件。
- 整体 diff（`git diff <base>..HEAD`）与改动文件列表。
- 若中途停下：说明卡在哪、为何、建议下一步。

验收（合并 / 继续 / 返工）由用户决定；本流程不替用户做合并或推送。
