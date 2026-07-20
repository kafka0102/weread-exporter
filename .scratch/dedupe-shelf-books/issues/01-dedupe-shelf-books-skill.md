# Ticket 01: dedupe-shelf-books skill

Status: ready-for-agent

## 目标

交付「书架去重」skill 本体 + 双 agent 入口，使 Claude Code 与 Codex 都能据其执行去重分析。

## 上下文

- Spec: `.scratch/dedupe-shelf-books/spec.md`
- 输入：`data/shelf_books.txt`（`ID,书名,作者名`）、`data/ebook-info.json`（1333 条数组，取 `bookName`/`authorName`）
- 输出：`data/dup_books.txt`、`data/new_books.txt`，每行 `ID,书名,作者名`，追加写入

## 任务

1. 新建 `.claude/skills/dedupe-shelf-books/SKILL.md`：
   - frontmatter：`name: dedupe-shelf-books` + `description`（覆盖触发词：书架去重 / shelf_books vs ebook-info / 找重复书与新书 / dup_books new_books）
   - 何时使用、输入文件、处理流程（读已处理→跳过→判断→追加→汇报）
   - 判断原则（纯 AI 语义；两条防错原则；命中定义；拿不准倾向 new）
   - 真实数据工作示例（至少覆盖：朝代标记 dup、多作者交集 dup、同名异作者 new、角色后缀 dup）
   - 边界注意（两套 ID 不可互通、只追加不覆盖、可重跑）
2. `AGENTS.md` 末尾加 skill 指针段（指向 `.claude/skills/dedupe-shelf-books/SKILL.md`，供 Codex 发现）。
3. `CONTEXT.md` 新增「电子书库（ebook-info）」「去重（dup/new）」两条术语。

## 验收

- 三处文件改动齐全；SKILL.md 工作示例与真实数据一致；双 agent 入口路径正确。
- 提交信息：中文 Conventional Commits、无 scope、停留当前分支、提交前后 `git status --short`。

Blocked by: —
