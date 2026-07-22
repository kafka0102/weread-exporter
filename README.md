# weread-exporter

微信读书自动化工具集 — 基于 Playwright 持久化登录会话，提供**全书图文导出**（Markdown）与**书架书籍列表抓取**（JSON）等能力。文字与插图按阅读顺序精确交错。

## 原理

微信读书网页版用 Canvas 渲染书籍文字（而非 DOM 文本节点），插图则是 DOM `<img>` 元素。本工具：

1. **Playwright 自动化** — 启动 Chromium，持久化登录会话（扫码一次，后续自动复用）
2. **Canvas fillText Hook** — 注入钩子拦截所有 `CanvasRenderingContext2D.fillText()` 调用，捕获每个字符的 (x, y) 坐标
3. **双页拆分** — 微信读书在同一 Canvas 同时渲染当前页与下一页，通过检测 y 坐标重置点分离两页
4. **视口图片捕获** — 每页只取当前视口内可见的 `img[class*="wr_readerImage"]`（用 `getBoundingClientRect` 过滤掉预加载的下一页/下一章图片），解决图片归属偏移
5. **图文交错** — 把文字行和图片按屏幕 y 坐标排序，图片精确落在对应段落之间、正确章节里
6. **格式清理** — 合并 Canvas 渲染断行，还原自然段落

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

运行测试（可选）：`python -m unittest discover tests`

## 使用

> 所有脚本共享同一份缓存登录会话（由 `weread_session.py` 提供）：首次扫码后登录态保存在 `cache/browser_profile/`，后续任何脚本都免重复登录。切换账号需清空该目录。

### 1. 导出书籍（兼容 JSON + md 中间产物）

```bash
# 单本：传入 reader URL（推荐）或 book_id
python export_precise.py https://weread.qq.com/web/reader/d31323b0813abaf26g0137c2
python export_precise.py d31323b0813abaf26g0137c2

# 单本强制重导（默认若 ~/data/weixin/books 已有同 id 的 json 则跳过）
python export_precise.py d31323b0813abaf26g0137c2 --force

# 需要插图时再下载（默认不下载图片）
python export_precise.py d31323b0813abaf26g0137c2 --download-images

# 指定 JSON 输出目录（默认 ~/data/weixin/books，可用 BOOKS_DIR 或 --out-dir 覆盖）
python export_precise.py d31323b0813abaf26g0137c2 --out-dir ~/data/weixin/books

# 批量：不传 book_id，读取 data/new_books.txt 中尚未导出的书
# 书与书默认间隔 180s / 3 分钟（SLEEP_BOOK_INTERVAL）；任一本失败则停止
python export_precise.py
python export_precise.py --list data/new_books.txt
```

- 首次运行会弹出浏览器要求扫码登录，会话自动保存在 `cache/browser_profile/`，后续复用
- 自动跳到全书开头（原生点击目录首项），逐页翻到全书末尾自动停止
- **自动续传**：中途卡住会重开浏览器，从上次章节继续；中间产物在 `output/<book_id>/`
- **全书成功后**才写入 `~/data/weixin/books/<book_id>_<书名>.json`（字段对齐 dedao/json：纯文本 content、`has_content`、空元数据键；可用 `BOOKS_DIR` / `--out-dir` 覆盖）
- 章切换后按该章字数动态等待：`ceil(字数/1000)*SLEEP_CHAPTER_PER_1K_CHARS`，夹在 `SLEEP_CHAPTER_MIN`–`SLEEP_CHAPTER_MAX`（默认 2–15 秒）
- 默认不下载图片；需要时加 `--download-images`。正文 md 中间产物仍可含 `images/` 相对路径引用

### 2. 下载图片

```bash
python download_images.py d31323b0813abaf26g0137c2
```

- 从 `raw/*.json` 读取所有图片 URL，8 线程并发下载到 `images/`
- **强制 IPv4**：macOS 上 urllib 默认先试 IPv6，路由不通会每张图卡约 120 秒；强制 IPv4 后恢复秒级
- 已存在的图片自动跳过（可重复运行补齐失败项）

> 导出和下载分两步：翻页抓取时若同步下载大图会阻塞翻页，故先记录 URL、翻完后统一并发下载。

### 3. 抓取书架书籍列表

```bash
# 可见浏览器（首次需扫码登录）；网页操作 sleep 见根目录 .env
python fetch_shelf.py

# 复用缓存登录，无头运行
python fetch_shelf.py --headless

# 调整滚动节奏（默认见 .env SLEEP_SHELF_SCROLL，连续 3 次无新书停止）
python fetch_shelf.py --sleep 5 --max-no-new 4

# 跳过「作者为空时打开详情补全」
python fetch_shelf.py --no-enrich-author

# 临时覆盖详情补全间隔（默认 .env SLEEP_BOOK_DETAIL_INTERVAL=5）
python fetch_shelf.py --author-interval 8
```

- 慢滚动触发懒加载，逐屏抓取书架上所有书籍
- 每本书提取 `id`、`title`、`author`，存为 `data/shelf_books.txt`（一行一条，逗号分隔：ID,书名,作者；书名/作者中的逗号替换为空格）
- 书架页禁用 F12 不影响抓取：`id`/`title` 取自页面 DOM，`author` 优先取自拦截的书架接口；**仍为空时再打开阅读器详情补全**（已有作者的书不打开；相邻两本默认间隔 5s）
- 参数：`--headless`、`--sleep` 滚动间隔、`--max-no-new`、`--out`、`--no-enrich-author`、`--author-interval`
- 所有点击/跳转类等待见 `.env` / `AGENTS.md`（`SLEEP_*`）

> 登录过期时 `--headless` 无法弹扫码页，去掉 `--headless` 重新扫一次即可。

## 输出

```
output/
├── <book_id>/               # 中间产物（续传用）
│   ├── _catalog.json        # 目录章节标题列表（用于判定全书末尾）
│   ├── chapters/            # 每章独立 Markdown
│   │   ├── 0001.md
│   │   └── ...
│   ├── images/              # 仅在 --download-images 时填充
│   └── raw/                 # 每章的图片 URL 记录 + 字数
│       ├── 0001.json
│       └── ...
└── 书名.md                  # 合并后的全本预览

data/
├── shelf_books.txt          # 书架书籍列表（一行一条：ID,书名,作者）
└── new_books.txt            # 去重后的新书清单（批量导出输入）

~/data/weixin/books/
└── <book_id>_<书名>.json    # 兼容 dedao/json 的最终书稿（默认；BOOKS_DIR/--out-dir 可改）
```

JSON 顶层字段：`id, title, author, press, publication_date, isbn, word_count, body`；
`body[]` 为 `chapter_name, chapter_id(ch_0001…), content(纯文本), has_content`。
拿不到的出版社/出版日/ISBN 写空串；`word_count` 为各章纯文本字数之和。

用 Typora / Obsidian 等打开全本 `.md` 可预览；导入下游请用 `~/data/weixin/books/*.json`。

书架抓取输出：

```
data/
└── shelf_books.txt          # 书架书籍列表（一行一条：ID,书名,作者）
```

## 扩展：复用登录会话

`weread_session.py` 是全仓唯一的登录入口组件（决策见 `docs/adr/0001-persistent-browser-profile-as-login-cache.md`），新脚本可直接复用，无需重复实现登录逻辑：

```python
import asyncio
from playwright.async_api import async_playwright
from weread_session import open_logged_in_page

async def main():
    async with async_playwright() as p:
        # 已登录返回 (context, page)，登录失败返回 (None, None)
        context, page = await open_logged_in_page(
            p, url="https://weread.qq.com/web/shelf")
        if context is None:
            return
        # ... 你的抓取逻辑 ...
        await context.close()

asyncio.run(main())
```

主要接口：

- `launch_weread_context(playwright, headless=False, viewport=None)` — 启动带持久化登录态的 Chromium context
- `ensure_logged_in(context)` — 确认已登录，未登录则等待扫码；成功返回 `True`
- `open_logged_in_page(playwright, url=None)` — 启动 + 登录 + 打开页面，一步到位
- `is_login_url(url)` — 判断 URL 是否为登录页（纯函数，已覆盖单测）

> 若需要在页面加载前监听网络（如拦截接口），改用 `launch_weread_context` + `ensure_logged_in` 自行管理 context 与 page，以便提前注册 `context.on("response", ...)`。`fetch_shelf.py` 即采用此模式。

## 限制

- 需要有效的微信读书账号，且对目标书籍有阅读权限（无限卡会员或已购买）
- 部分出版社限制网页端阅读（显示"去 App 阅读"），此类书籍无法导出
- 纯图廊章节图片密集时，图注与图的配对偶尔差一位；正文章节里图片相对段落的位置准确
- 导出速度受翻页等待限制，约每页 1-2 秒
- 书架抓取的 DOM 选择器为通用推断；若微信读书改版导致 `id`/`title` 抓取为空，仍可依赖接口拦截兜底 `author`，必要时按实际 DOM 调整 `fetch_shelf.py` 中的 `EXTRACT_BOOKS_JS`

## 工作流程

```
export_precise.py:
  浏览器登录 → 原生点击目录首项跳到开头 → 键盘 ArrowRight 逐页翻
    → 每页: Canvas Hook 捕获文字 + 视口内图片 URL
    → 双页拆分 → 文字/图片按 y 坐标交错 → 按章节切分输出 md
    → 翻到目录最后一章自动停止

download_images.py:
  读 raw/*.json 图片 URL → 强制 IPv4 + 8 线程并发下载 → images/

fetch_shelf.py:
  复用缓存登录打开书架 → 慢滚动触发懒加载
    → DOM 抓 id/title + 拦截书架接口取 author → 按 book_id 合并去重
    → author 仍为空则逐本打开阅读器补全（已有作者跳过；间隔 SLEEP_BOOK_DETAIL_INTERVAL）
    → 写 data/shelf_books.txt
```

## 声明

仅供个人学习研究使用。请勿用于商业用途或大规模传播，请尊重著作权。
