# 领域上下文 - weread-exporter

## 术语表（Glossary）

- **浏览器持久化 profile（browser_profile）**：Playwright `launch_persistent_context` 写入 `cache/browser_profile/` 的用户数据目录，存放 cookie / localStorage / 登录态。是"缓存登录"的物理载体。已加入 `.gitignore`，含敏感凭据，不入库。
- **缓存登录会话（cached login session）**：首次扫码登录后，登录态落盘到 browser_profile；后续脚本复用该 profile 即免重复登录。`weread_session.py` 是全仓唯一登录入口组件。
- **书架（shelf）**：微信读书网页版 `https://weread.qq.com/web/shelf`，展示用户收藏/购买的书籍列表，长列表懒加载，需滚动触发后续内容。
- **reader**：书籍阅读器页 `https://weread.qq.com/web/reader/<book_id>`，Canvas 渲染文字（非 DOM 文本），插图是 DOM `<img>`。
- **book_id**：书籍唯一标识，出现在 reader URL 末段、书架链接 href、书架接口响应中。
- **shelf API**：书架页加载时浏览器请求的接口，返回书籍列表 JSON（含 bookId / title / author / cover）。页面禁用 F12 开发者模式，但 Playwright 通过 CDP 的 `page.evaluate` / `page.on("response")` 不受影响——是作者字段的可靠来源。
- **作者补全（author enrich）**：公版书等字母数字 book_id 在 shelf API/列表 DOM 常无 author；列表合并后对空 author 逐本打开 reader 详情补全，已有作者跳过，相邻间隔由 `SLEEP_BOOK_DETAIL_INTERVAL` 控制。
- **网页操作 sleep（.env）**：所有点击/跳转等待经 `env_config.py` 从 `.env` 读取，清单见 `AGENTS.md`。
- **页面禁用开发者模式**：weread 网页对**手动 F12** 做了反调试（debugger / 检测 devtools）；不影响 Playwright 的 CDP 级注入与网络监听。

## 关键决策

见 `docs/adr/`。
