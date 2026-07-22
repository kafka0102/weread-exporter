# AGENTS.md — Agent 协作规则

本文件约束在本仓库内工作的 AI / 自动化 Agent。与 `CLAUDE.md`、`docs/standards/` 一并遵守；更具体的规范以 `docs/standards/` 为准。

## 网页操作 sleep 必须可配置

凡脚本中**涉及点击网页、跳转页面、等待页面渲染**的 `sleep` / 固定等待，不得只写死在代码字面量里让用户无法感知。统一通过项目根目录 **`.env`**（及 `env_config.py`）暴露为环境变量。

### 规则

1. **单一配置源**：新增或修改浏览器节奏时，在 `env_config.py` 增加/使用 `SLEEP_*` 常量，默认值与根目录 `.env` 保持一致。
2. **本地可调**：直接修改根目录 `.env`；`.env` 入库作为默认配置。进程环境变量优先于 `.env` 文件。
3. **CLI 可覆盖**：若已有命令行参数（如 `--sleep`、`--author-interval`），参数优先于 `.env` 默认值。
4. **文档同步**：增删 `SLEEP_*` 时同步更新 `.env`、本文件下表，以及 README 中相关说明。
5. **非网页等待例外**：纯 HTTP 下载重试、CPU 计算轮询等与「点网页」无关的等待，可不强行纳入；若与风控节奏相关，仍建议可配置。

### 当前 `SLEEP_*` 一览（单位：秒）

| 变量 | 默认 | 使用位置 | 含义 |
|------|------|----------|------|
| `SLEEP_LOGIN_SWITCH_PC` | 1 | `weread_session.py` | 点击登录按钮后等待弹层 |
| `SLEEP_LOGIN_AFTER_GOTO` | 3 | `weread_session.py` | 打开书架后选择器超时的兜底等待 |
| `SLEEP_LOGIN_POLL` | 2 | `weread_session.py` | 等待扫码登录的轮询间隔 |
| `SLEEP_SHELF_AFTER_LOAD` | 3 | `fetch_shelf.py` | 书架页 `goto` 后首屏等待 |
| `SLEEP_SHELF_SCROLL` | 3 | `fetch_shelf.py` | 每次慢滚动后的间隔（CLI `--sleep` 可覆盖） |
| `SLEEP_BOOK_DETAIL_INTERVAL` | **5** | `fetch_shelf.py` | 作者为空时，打开下一本详情前的间隔（CLI `--author-interval` 可覆盖） |
| `SLEEP_BOOK_DETAIL_LOAD` | 3 | `fetch_shelf.py` | 打开阅读器详情后等待加载 |
| `SLEEP_BOOK_DETAIL_CATALOG` | 1.5 | `fetch_shelf.py` | 详情页点开目录面板后的等待 |
| `SLEEP_READER_AFTER_LOAD` | 5 | `export_precise.py` | 阅读器打开后等待 |
| `SLEEP_READER_CATALOG_OPEN` | 1.5 | `export_precise.py` | 点击目录按钮后 |
| `SLEEP_READER_CATALOG_SCROLL` | 1 | `export_precise.py` | 目录滚到顶部后 |
| `SLEEP_READER_CATALOG_CLICK` | 3 | `export_precise.py` | 点击目录首章后 |
| `SLEEP_READER_CATALOG_CLOSE` | 2 | `export_precise.py` | 关闭目录后 |
| `SLEEP_READER_AFTER_HOOK` | 0.5 | `export_precise.py` | 点击画布聚焦后 |
| `SLEEP_READER_PAGE_TURN` | 1 | `export_precise.py` | 翻页键后 |
| `SLEEP_READER_PAGE_RENDER` | 0.3 | `export_precise.py` | 抓取当前页前的短等待 |
| `SLEEP_READER_REOPEN` | 3 | `export_precise.py` | 会话重开前等待 |
| `SLEEP_READER_STABLE_POLL` | 0.5 | `export_precise.py` | 等待 Canvas 渲染稳定的轮询间隔 |
| `SLEEP_BOOK_INTERVAL` | **60** | `export_precise.py` | 批量导出相邻两本书之间的间隔 |
| `SLEEP_CHAPTER_PER_1K_CHARS` | 2 | `export_precise.py` | 章完成后每千字等待秒数 |
| `SLEEP_CHAPTER_MIN` | **1** | `export_precise.py` | 章间动态等待下限 |
| `SLEEP_CHAPTER_MAX` | **8** | `export_precise.py` | 章间动态等待上限 |

### 书架作者补全

- 列表阶段（DOM + shelf API）已能取到 `author` 的书：**不要**再打开详情。
- 仅当 `author` 为空时，才打开对应阅读器页补全作者。
- 相邻两本详情之间必须间隔 `SLEEP_BOOK_DETAIL_INTERVAL`（默认 5 秒），避免过快连点。
- 用户可用 `python fetch_shelf.py --no-enrich-author` 跳过补全；用 `--author-interval` 临时覆盖间隔。

### 实现入口

- 读取与默认值：`env_config.py`
- 项目配置：`.env`（入库）

## Skills（Claude Code / Codex 共用）

仓库内置 skill 同步放在两处，内容保持一致：

| 位置 | 用途 |
|------|------|
| `.claude/skills/<name>/SKILL.md` | Claude Code 自动发现 |
| `.agents/skills/<name>/SKILL.md` | Codex 自动发现（仓库 skill root） |

| 触发场景 | skill 文件 |
|----------|-----------|
| 书架书去重：判断 `data/shelf_books.txt` 的书是否已在 `data/ebook-info.json`，分别落到 `data/dup_books.txt` / `data/new_books.txt` | `.agents/skills/dedupe-shelf-books/SKILL.md`（同 `.claude/skills/dedupe-shelf-books/SKILL.md`） |

当用户说"书架去重 / 找重复书 / 找新书 / 生成 dup_books、new_books"等，读上述 skill 并按其流程执行。

## 修改完成后必须自动提交

Agent 在本仓库**每完成一次可独立交付的修改**后，必须主动创建本地 commit，**无需等待用户再说「提交」**。

### 何时提交

- 功能 / 修复 / 重构 / 文档 / 配置等任务达到可验收状态时，立即提交。
- 同一轮对话中有多个可独立交付的改动时，优先拆成多次小提交，而不是攒成一次大提交。
- 仅探索、只读分析、或改动尚未完成时不要提交。
- 用户明确说「先别提交 / 不要提交」时，本次对话内暂停自动提交，直到用户再次要求提交或取消该限制。

### 必须遵守

- 提交范围、验证、分支策略、message 格式一律遵循 `CLAUDE.md` 与 `docs/standards/git-workflow.md`。
- 只 stage 当前任务直接相关的文件；不要夹带无关改动。
- 提交前做与改动相关的最小验证；提交前后各检查一次工作区。
- 中文 Conventional Commits：`type: 简要描述`（禁止 scope）。
- 默认只做本地 commit；**不** `push` / `merge` / `rebase` / `tag` / 改写历史，除非用户明确要求。

### 成功标准

- 产生了对应 commit，且当前任务相关文件均已纳入提交。
- 工作区中不再残留本次任务相关的未提交改动。
- 向用户简要回报 commit hash 与标题即可。

## 其它

- Git 提交规范见 `CLAUDE.md` 与 `docs/standards/git-workflow.md`。
- 领域术语见根目录 `CONTEXT.md`。
