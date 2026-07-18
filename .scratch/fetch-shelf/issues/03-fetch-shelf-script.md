# 03 - fetch-shelf-script

Status: resolved (commits ec44831, 7717366)
Blocked by: 01

## 目标

新建 `fetch_shelf.py`，慢滚动抓书架 `{id, title, author}` 存 `data/shelf_books.json`。

## 任务

- 复用 `weread_session.open_logged_in_page`（或 `launch_weread_context` + `ensure_logged_in`）打开 shelf 页（`https://weread.qq.com/web/shelf`）。
- 注册 `page.on("response")` 捕获 shelf API 响应（JSON 含 bookId / title / author / cover），按 `bookId` 索引；URL 模式按实际抓取确认（如 `weread.qq.com` 含 `shelf` 的 json 响应）。
- 慢滚动：每次 `scrollBy(0, viewportHeight)` -> `asyncio.sleep(3)` -> 连续 3 次无新书停止；sleep / 阈值参数可配。
- DOM 提取：`page.evaluate` 抓每本书的 `book_id`（从链接 href 或 data 属性）+ `title`；提取为纯函数 `extract_book_id_from_href(href)`。
- 合并：纯函数 `merge_books(dom_books, api_books)` 按 `book_id` 合并；`title` 优先 DOM、其次 API；`author` 优先 API、缺失则 DOM、再缺失留空字符串。
- 写 `data/shelf_books.json`：数组 `[{id, title, author}]`，`ensure_ascii=False`，缩进 2；去重 by id。
- 纯函数单测 `tests/test_fetch_shelf.py`：
  - `extract_book_id_from_href`：reader URL 末段、`#reader/<id>`、含 query、空值等形态。
  - `merge_books`：合并 / 缺失 / 冲突 / 去重。
- 验证：`python -m py_compile fetch_shelf.py`；`python -m unittest discover tests` 绿。
- 提交：`feat: 新增书架书籍列表抓取脚本与单测`

## 验收

- 脚本静态检查通过；纯函数单测绿；产出 JSON 结构正确 `[{id,title,author}]`；commit 合规。
- 实际产出 `data/shelf_books.json` 需用户登录态；若 `cache/browser_profile/` 已缓存登录，可 headless 试跑，否则留最终验收。
