# 01 - formalize-login-component

Status: ready-for-agent
Blocked by: (none)

## 目标

`weread_session.py` 作为全仓唯一登录入口正式入库，并补 `is_login_url` 单测。

## 任务

- 新建 `tests/test_weread_session.py`，用 stdlib `unittest` 测 `is_login_url`：
  - login URL（含 `login`，大小写）-> True
  - shelf / reader URL -> False
  - 空 / None -> False
- 确认 `weread_session.py` 无需改动（已完整）；如发现小问题可修。
- `python -m py_compile weread_session.py` 通过；`python -m unittest discover tests` 通过。
- 提交（中文 Conventional Commits，无 scope，停留 main）：
  - `feat: 落地微信读书通用登录会话组件与单测`
  - 范围：`weread_session.py` + `tests/test_weread_session.py`（+ `tests/__init__.py` 如需）

## 验收

- `weread_session.py` 入库；`is_login_url` 单测绿；commit 合规（提交前后 `git status --short` 检查）。
