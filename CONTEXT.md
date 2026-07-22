# 领域上下文 - weread-exporter

## 术语表（Glossary）

- **浏览器持久化 profile（browser_profile）**：Playwright `launch_persistent_context` 写入 `cache/browser_profile/` 的用户数据目录，存放 cookie / localStorage / 登录态。是"缓存登录"的物理载体。已加入 `.gitignore`，含敏感凭据，不入库。
- **缓存登录会话（cached login session）**：首次扫码登录后，登录态落盘到 browser_profile；后续脚本复用该 profile 即免重复登录。`weread_session.py` 是全仓唯一登录入口组件。
- **书架（shelf）**：微信读书网页版 `https://weread.qq.com/web/shelf`，展示用户收藏/购买的书籍列表，长列表懒加载，需滚动触发后续内容。
- **reader**：书籍阅读器页 `https://weread.qq.com/web/reader/<book_id>`，Canvas 渲染文字（非 DOM 文本），插图是 DOM `<img>`。
- **book_id**：书籍唯一标识，出现在 reader URL 末段、书架链接 href、书架接口响应中。可用 reader id 为字母数字长串（常见 23–24 位）；shelf API 偶发的纯数字短 id（如 `3300215708`）不能打开阅读器，抓取时应丢弃。
- **shelf API**：书架页加载时浏览器请求的接口，返回书籍列表 JSON（含 bookId / title / author / cover）。页面禁用 F12 开发者模式，但 Playwright 通过 CDP 的 `page.evaluate` / `page.on("response")` 不受影响——是作者字段的可靠来源。
- **作者补全（author enrich）**：公版书等字母数字 book_id 在 shelf API/列表 DOM 常无 author；列表合并后对空 author 逐本打开 reader 详情补全，已有作者跳过，相邻间隔由 `SLEEP_BOOK_DETAIL_INTERVAL` 控制。
- **网页操作 sleep（.env）**：所有点击/跳转等待经 `env_config.py` 从 `.env` 读取，清单见 `AGENTS.md`。
- **页面禁用开发者模式**：weread 网页对**手动 F12** 做了反调试（debugger / 检测 devtools）；不影响 Playwright 的 CDP 级注入与网络监听。
- **电子书库（ebook-info）**：外部电子书数据 `data/ebook-info.json`，JSON 数组，每条含 `id`（电子书体系整数 id）、`bookName`、`authorName` 等。其 `id` 与 weread `book_id` **不可互通**，比对只能靠书名 + 作者。
- **去重（dedupe / dup / new）**：把书架书 `data/shelf_books.txt` 与电子书库比对——同一作品且作者至少一人相同判为 `dup`（已存在，落 `data/dup_books.txt`），否则 `new`（落 `data/new_books.txt`）。以 weread book ID 为去重 key，可重复运行，只追加不覆盖。skill 见 `.claude/skills/dedupe-shelf-books/SKILL.md`。


- **兼容 JSON 书稿（book json）**：写出到 `~/data/weixin/books/`（可配 `BOOKS_DIR`）的单本书 JSON，字段对齐 dedao/json 样例（id/title/author/press/publication_date/isbn/word_count/body）。用于后续导入电子书库；与微信读书中间产物 `output/<book_id>/` 分离。
- **books 目录（books dir）**：默认 `~/data/weixin/books/`（环境变量 `BOOKS_DIR` / CLI `--out-dir` 可覆盖），存放已成功导出的 book json。判断“已导出”时按文件名 `book_id_` 前缀匹配，不依赖书名是否变化。
- **章间动态 sleep（chapter sleep）**：一章抓取完成后，按该章纯文本字数计算等待秒数：`clamp(ceil(chars/1000)*SLEEP_CHAPTER_PER_1K_CHARS, SLEEP_CHAPTER_MIN, SLEEP_CHAPTER_MAX)`，再开始下一章。
- **书间间隔（book interval）**：批量导出相邻两本书之间的固定等待，配置项 `SLEEP_BOOK_INTERVAL`（默认 180 秒 / 3 分钟）。

## 关键决策

见 `docs/adr/`。
