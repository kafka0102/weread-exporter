# 02 - refactor-export-precise-login

Status: resolved (commit c9bc585)
Blocked by: 01

## 目标

`export_precise.py` 登录段（约 283–302 行）替换为复用 `weread_session.py`，消除重复登录逻辑。

## 任务

- 顶部 `from weread_session import launch_weread_context, ensure_logged_in, USER_DATA_DIR`（按需）。
- 替换 `run_session` 内 `launch_persistent_context` + login 轮询块为组件调用：
  - `ctx = await launch_weread_context(p, headless=False, viewport={"width":1200,"height":900})`
  - `ok = await ensure_logged_in(ctx)`；`ok` 为 False 时关 ctx 并按原逻辑返回。
  - 保留之后 `page = await ctx.new_page()` + `add_init_script(CANVAS_HOOK)` + `goto(reader)` + 导航逻辑不变。
- `USER_DATA_DIR` 统一用组件常量（删本地重复常量，或保留但同值；优先 import）。
- 验证：`python -m py_compile export_precise.py`；`python -c "import export_precise"`；`python -m unittest discover tests` 仍绿。
- 提交：`refactor: export_precise 登录逻辑复用通用会话组件`

## 验收

- 登录段无重复；py_compile / import / 单测通过；翻页导出逻辑未改。
- 端到端导出需用户登录态，留最终验收；本次做静态 + 结构等价验证。

## 备注

- `ensure_logged_in` 自带检测页并在 `close_check_page=True` 时关闭；之后新建主 page 注入 CANVAS_HOOK，与原流程等价。
