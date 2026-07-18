# Spec: fetch-shelf

Status: ready-for-agent

## 目标

确认并补齐"持久化 profile 缓存登录"为全仓唯一登录入口，新增书架抓取脚本 `fetch_shelf.py`，慢滚动提取 `{id, title, author}` 存入 `data/shelf_books.json`。

## 背景

- 现有 `weread_session.py`（未提交）已是可复用登录组件，但 `export_precise.py` 仍内联重复登录逻辑（约 283–302 行）。
- 无书架抓取脚本。
- 术语见 `CONTEXT.md`；登录缓存架构决策见 `docs/adr/0001-persistent-browser-profile-as-login-cache.md`。

## 决策

1. 登录缓存：持久化 profile `cache/browser_profile/` + `weread_session.py` 全仓唯一入口（ADR-0001）。
2. 重构 `export_precise.py`：内联登录段替换为复用 `weread_session.py`。
3. 作者提取：DOM 抓 id/title + 网络拦截 shelf API 取 author，按 book_id 合并。
4. 输出：`data/shelf_books.json`，数组 `[{id, title, author}]`；`data/` 入版本控制。
5. 滚动：每屏 sleep 3s，连续 3 次无新书即停；参数可配置。

## 范围

In:
- 提交 `weread_session.py` 为正式组件 + 单测（`is_login_url`）
- 重构 `export_precise.py` 登录段复用组件（仅登录段，翻页/抓取/导出逻辑不动）
- 新建 `fetch_shelf.py`：复用登录 -> 慢滚动 -> DOM + 网络拦截 -> 合并 -> 落 JSON
- `fetch_shelf.py` 纯函数单测（book_id 提取、合并逻辑）
- `tests/` 目录（stdlib `unittest`，无新依赖）

Out:
- 不改 `export_precise.py` 的翻页 / 抓取 / 导出逻辑
- 不引入 `pytest` 等新依赖
- 不做端到端浏览器自动化测试（需用户登录态，留最终验收）

## Testing Decisions / Seam

- 仓库无既有测试 seam（无 `tests/`，`requirements.txt` 仅 `playwright`）。
- 选定 seam：**stdlib `unittest`**，放在 `tests/`，仅测纯函数（无浏览器依赖）：
  - `is_login_url`（`weread_session.py`）
  - `extract_book_id_from_href`、`merge_books`（`fetch_shelf.py` 新增纯函数）
- 浏览器依赖逻辑（登录、滚动、DOM 抓取、网络拦截）以"运行脚本产出 JSON"为集成验证，留最终验收由用户执行（需其 weread 登录态）。
- 假设：用户复核后若想改 seam（如引入 pytest / 加 browser fixtures）可改。

## 验收标准

- `weread_session.py` 已提交，`is_login_url` 有单测且通过。
- `export_precise.py` 登录段改为复用 `weread_session.py`；`python -m py_compile` 通过；`import` 通过；结构等价（登录 -> 复用缓存或扫码 -> 进入 reader）。
- `fetch_shelf.py` 新建：复用 `weread_session.py`；慢滚动 + sleep；DOM 抓 id/title + 网络拦截 author + 按 book_id 合并；写 `data/shelf_books.json`；纯函数有单测且通过。
- `python -m unittest discover tests` 全绿。
- 按 `docs/standards/git-workflow.md` 提交：中文 Conventional Commits、无 scope、停留 main 分支、提交前后检查 `git status`。

## Tickets

- `01-formalize-login-component` — 提交 weread_session.py + is_login_url 单测
- `02-refactor-export-precise-login` — export_precise.py 登录段复用组件（Blocked by 01）
- `03-fetch-shelf-script` — 新建 fetch_shelf.py + 纯函数单测（Blocked by 01）

Frontier: 01 -> 02 -> 03
