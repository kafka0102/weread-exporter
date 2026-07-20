# Spec: dedupe-shelf-books

Status: ready-for-agent

## 目标

在本仓库新增一个 skill（一份 markdown 说明 + Codex 入口指针），让 Claude Code 与 Codex 两个 coding agent 都能执行「书架书 vs 电子书库去重」：读 `data/shelf_books.txt`，判断每本书是否已存在于 `data/ebook-info.json`，分别落到 `data/dup_books.txt` / `data/new_books.txt`，且可重复运行（只处理未处理过的书）。

## 背景

- `shelf_books.txt` 由 `fetch_shelf.py` 产出（94 行，`ID,书名,作者名`；ID 为 weread bookId）。
- `ebook-info.json` 为外部电子书库（1333 条数组，每条含 `id`/`bookName`/`authorName` 等；`id` 为电子书体系整数 id，与 weread bookId **不可互通**）。
- 两边作者字符串都"脏"：含朝代前缀（`[宋]`/`【清】`）、编校角色后缀（`译注`/`评注`/`著`/`校点` 等）、多种分隔符（空格/`；`/`、`）。书名可能带版本/丛书括号修饰（`（精装版）`/`（中华经典诗话）` 等）。
- 术语见 `CONTEXT.md`（本次新增「电子书库」「去重」两条）。

## 决策

1. **交付形态 = skill（markdown 指令），非脚本**。由用户明确要求"写一个 skill""让 AI 分析"。
2. **匹配 = 纯 AI 语义判断**（用户拍板，不做归一化脚本）。skill 只给判断原则，不塞正则算法。
3. **两条判断原则**（防低级错，非算法）：
   - 圈候选时按"是否同一作品"判断，不要求书名完全相等；括号内版本/丛书修饰不影响同一作品判定。
   - 判断作者是否同一人时，朝代标记与编校角色是**注解不是名字**：`[宋]周密`≡`周密`，`洪亮吉著`≡`洪亮吉`。
4. **命中定义**：书名同一作品 **且** 作者至少一人相同 → dup；否则 new。拿不准时**倾向 new**（漏判 dup 会丢书，比多判 new 更糟）。
5. **去重 key = weread book ID**。运行时先读两个输出文件收集已处理 ID，跳过；输出文件不存在视为空、首次创建。**追加**写入，不覆盖。
6. **输出行格式** = `ID,书名,作者名`（三段，与输入一致，自描述、可重跑）。
7. **双 agent 入口**：主体 `.claude/skills/dedupe-shelf-books/SKILL.md`（Claude 自动发现）；`AGENTS.md` 末尾加指针段让 Codex 复用同一份。

## 范围

In:
- 新建 `.claude/skills/dedupe-shelf-books/SKILL.md`（含触发条件、输入输出、处理流程、判断原则、真实数据工作示例、边界注意）
- `AGENTS.md` 末尾新增 skill 指针段
- `CONTEXT.md` 新增「电子书库」「去重（dup/new）」术语

Out:
- 不写任何 Python 脚本（用户明确要纯 AI 判断）
- 不改 `shelf_books.txt` / `ebook-info.json`
- 不实际跑一次去重产出 dup/new（那是用户验收时由 agent 按 skill 执行；本任务只交付 skill 本身）

## Testing Decisions / Seam

- 交付物是 markdown 说明，**无代码 seam**，不适用 TDD / unittest。
- 验证方式：回读 SKILL.md 检查（a）双 agent 入口齐全且路径正确；（b）去重/追加/输出格式逻辑自洽；（c）工作示例与真实数据一致（已用一次性脚本核验 `词品`/`绝妙好词`/`古今词话`/`北江诗话` 四例）。
- 假设：用户复核后若想把"纯 AI 判断"换成"确定性归一化脚本"，可另起 ticket。

## 验收标准

- `.claude/skills/dedupe-shelf-books/SKILL.md` 存在，含 frontmatter（`name`/`description`），description 能让 Claude 在用户说"书架去重/找重复书/dup_books"类话术时命中。
- `AGENTS.md` 含指向该 SKILL.md 的指针段，Codex 据此可发现并复用。
- `CONTEXT.md` 含两条新术语。
- SKILL.md 内工作示例与真实数据吻合；流程覆盖「读已处理 → 跳过 → 判断 → 追加 → 汇报」全链路。
- 按 `docs/standards/git-workflow.md` 提交：中文 Conventional Commits、无 scope、停留当前 worktree 分支、提交前后检查 `git status`。
- 本仓库不使用 GitHub Issues（见 `docs/agents/issue-tracker.md`），故本次提交**不**加 `Refs:` trailer（若用户另有 GitHub Issue 编号再补）。

## Tickets

- `01-dedupe-shelf-books-skill` — 写 SKILL.md + AGENTS.md 指针 + CONTEXT.md 术语

Frontier: 01
