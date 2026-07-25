# 领域上下文 - weread-exporter

## 术语表（Glossary）

- **浏览器持久化 profile（browser_profile）**：Playwright `launch_persistent_context` 写入 `cache/browser_profile/` 的用户数据目录，存放 cookie / localStorage / 登录态。是"缓存登录"的物理载体。已加入 `.gitignore`，含敏感凭据，不入库。
- **缓存登录会话（cached login session）**：首次扫码登录后，登录态落盘到 browser_profile；后续脚本复用该 profile 即免重复登录。`weread_session.py` 是全仓唯一登录入口组件。
- **书架（shelf）**：微信读书网页版 `https://weread.qq.com/web/shelf`，展示用户收藏/购买的书籍列表，长列表懒加载，需滚动触发后续内容。
- **reader**：书籍阅读器页 `https://weread.qq.com/web/reader/<book_id>`，Canvas 渲染文字（非 DOM 文本），插图是 DOM `<img>`。
- **单页/双页布局**：web 阅读器视口较宽时并排两个 canvas（双页）；导出默认自动匹配本机屏幕（`READER_VIEWPORT_WIDTH/HEIGHT=0`）贴近手动全宽浏览器，并按 canvas 位置拆页。需要旧版窄视口时，用 `READER_FORCE_SINGLE_PAGE=1` 或 CLI `--force-single-page`；强制失败会恢复宽视口。
- **左右翻页 vs 上下滚动**：微信读书桌面端可在「双栏/普通阅读」与「上下滚动阅读」间切换。上下滚动时长文档 scrollHeight 远大于视口，目录跳转易落到视口外锚点，ArrowRight 会在两页间空转且 fillText 不随 scroll 重绘。`export_precise.ensure_horizontal_paging_mode` 在会话开始检测到滚动模式时会自动点切换按钮切回左右翻页；已是左右模式时不点击，避免误切回滚动。
- **book_id**：书籍唯一标识，出现在 reader URL 末段、书架链接 href、书架接口响应中。可用 reader id 为字母数字长串（常见 23–24 位）；shelf API 偶发的纯数字短 id（如 `3300215708`）不能打开阅读器，抓取时应丢弃。
- **shelf API**：书架页懒加载时请求 `https://weread.qq.com/web/shelf/syncBook`（POST，约每批 100 本），JSON 含 bookId / title / author / cover。其中 bookId 多为纯数字短 id，不能直接打开 `/web/reader`；页面链接里的字母数字 reader id 才是导出用 id。抓取须滚动加载完全部批次，并跨屏累积 DOM。页面禁用 F12 不影响 Playwright CDP 拦截。
- **作者补全（author enrich）**：公版书等字母数字 book_id 在 shelf API/列表 DOM 常无 author；列表合并后对空 author 逐本打开 reader 详情补全，已有作者跳过，相邻间隔由 `SLEEP_BOOK_DETAIL_INTERVAL` 控制。
- **网页操作 sleep（.env）**：所有点击/跳转等待经 `env_config.py` 从 `.env` 读取，清单见 `AGENTS.md`。
- **页面禁用开发者模式**：weread 网页对**手动 F12** 做了反调试（debugger / 检测 devtools）；不影响 Playwright 的 CDP 级注入与网络监听。
- **电子书库（ebook-info）**：外部电子书数据 `data/ebook-info.json`，JSON 数组，每条含 `id`（电子书体系整数 id）、`bookName`、`authorName` 等。其 `id` 与 weread `book_id` **不可互通**，比对只能靠书名 + 作者。
- **去重（dedupe / dup / new）**：把书架书 `data/shelf_books.txt` 与电子书库（书名+作者语义）以及本地已导出目录（默认 `~/data/weixin/books`，按文件名 weread ID 前缀）比对——已下载或库中同一作品且作者至少一人相同判为 `dup`（落 `data/dup_books.txt`），否则 `new`（落 `data/new_books.txt`）。重跑时会把 new 里已下载的书迁到 dup。以 weread book ID 为去重 key，只追加不覆盖（清理 new 时例外）。skill 见 `.claude/skills/dedupe-shelf-books/SKILL.md`。


- **兼容 JSON 书稿（book json）**：写出到 `~/data/weixin/books/`（可配 `BOOKS_DIR`）的单本书 JSON，字段对齐 dedao/json 样例（id/title/author/press/publication_date/isbn/word_count/body）。用于后续导入电子书库；与微信读书中间产物 `output/<book_id>/` 分离。
- **books 目录（books dir）**：默认 `~/data/weixin/books/`（环境变量 `BOOKS_DIR` / CLI `--out-dir` 可覆盖），存放已成功导出的 book json。判断“已导出”时按文件名 `book_id_` 前缀匹配，不依赖书名是否变化。
- **章间动态 sleep（chapter sleep）**：一章抓取完成后，按该章纯文本字数计算等待秒数：`clamp(ceil(chars/2000)*SLEEP_CHAPTER_PER_2K_CHARS, SLEEP_CHAPTER_MIN, SLEEP_CHAPTER_MAX)`，再开始下一章。
- **书间间隔（book interval）**：批量导出相邻两本书之间的固定等待，配置项 `SLEEP_BOOK_INTERVAL`（默认 60 秒）。

## 关键决策

见 `docs/adr/`。
