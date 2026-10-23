# weread-exporter

微信读书自动化工具集 — 基于 Playwright 持久化登录会话，提供**全书图文导出**（Markdown）与**书架书籍列表抓取**（JSON）等能力。文字与插图按阅读顺序精确交错。

## 原理

微信读书网页版用 Canvas 渲染书籍文字（而非 DOM 文本节点），插图则是 DOM `<img>` 元素。本工具：

1. **Playwright 自动化** — 启动 Chromium，持久化登录会话（扫码一次，后续自动复用）
2. **Canvas fillText Hook** — 注入钩子拦截所有 `CanvasRenderingContext2D.fillText()` 调用，捕获每个字符的 (x, y) 坐标
3. **全宽桌面排版优先** — 阅读器默认自动匹配目标单屏宽度（`READER_VIEWPORT_WIDTH=0`，再受 `READER_VIEWPORT_MAX_*=1600x1000` 限制）打开；多显示器时**必须**用 CLI 指定目标屏：**默认用 `--prefer-largest-screen`（外接大屏）**，需要笔记本内建屏才用 `--no-prefer-largest-screen`（大屏单屏字数多、翻页次数少，章界更不容易被并章）；如需旧版单页策略，可开启 `READER_FORCE_SINGLE_PAGE=1` 或使用 `--force-single-page`（失败会自动恢复宽视口）
4. **Canvas 按页归组** — `fillText` 坐标是 canvas 局部坐标；若仍出现多 canvas，按屏幕 left 拆页后再分行，防止左右页同 y 交错乱码
5. **视口图片捕获** — 每页只取当前视口内可见的 `img[class*="wr_readerImage"]`（用 `getBoundingClientRect` 过滤掉预加载的下一页/下一章图片），解决图片归属偏移
6. **图文交错** — 把文字行和图片按屏幕 y 坐标排序，图片精确落在对应段落之间、正确章节里
7. **格式清理** — 合并 Canvas 渲染断行，还原自然段落；词牌/短标题不与正文粘连

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
# 多显示器目标屏必须显式指定；默认优先大屏 --prefer-largest-screen（单屏字数多、翻页少、章界更稳）
python export_precise.py https://weread.qq.com/web/reader/d31323b0813abaf26g0137c2 --prefer-largest-screen
python export_precise.py d31323b0813abaf26g0137c2 --prefer-largest-screen

# 单本强制重导（默认若 ~/data/weixin/books 已有同 id 的 json 则跳过）
python export_precise.py d31323b0813abaf26g0137c2 --force --prefer-largest-screen

# 临时调整阅读器视口；默认 0=自动匹配目标单屏（.env READER_VIEWPORT_WIDTH/HEIGHT，受 MAX 上限）
# 目标屏默认用 --prefer-largest-screen（外接大屏）；仅在明确要笔记本内建屏时才用 --no-prefer-largest-screen
python export_precise.py d31323b0813abaf26g0137c2 --reader-width 0 --reader-height 0 --prefer-largest-screen
python export_precise.py d31323b0813abaf26g0137c2 --reader-width 1470 --reader-height 900 --no-prefer-largest-screen

# 需要强制单页时才收窄视口（失败会恢复宽视口；也可 .env READER_FORCE_SINGLE_PAGE=1）
python export_precise.py d31323b0813abaf26g0137c2 --force-single-page --prefer-largest-screen

# 需要插图时再下载（默认不下载图片）
python export_precise.py d31323b0813abaf26g0137c2 --download-images --prefer-largest-screen

# 指定 JSON 输出目录（默认 ~/data/weixin/books，可用 BOOKS_DIR 或 --out-dir 覆盖）
python export_precise.py d31323b0813abaf26g0137c2 --out-dir ~/data/weixin/books --prefer-largest-screen

# 批量：不传 book_id，读取 data/new_books.txt 中尚未导出的书
# 书与书默认间隔 6s（SLEEP_BOOK_INTERVAL）；任一本失败则停止
# data/forbid_books.txt 中的 weread ID 会被跳过（不算失败）
python export_precise.py --prefer-largest-screen
python export_precise.py --list data/new_books.txt --prefer-largest-screen

# 无头导出（仅当 cache/browser_profile 已有登录信息时真正生效；否则回退有头）
python export_precise.py d31323b0813abaf26g0137c2 --headless --prefer-largest-screen

# 只补漏：已有 output/<id>/chapters 里有目录章缺失时，目录跳转补抓并重建 JSON
# （正常导出结束后也会自动跑一次补漏；插章时章号自动顺延）
python export_precise.py d31323b0813abaf26g0137c2 --fill-missing --prefer-largest-screen
```

- 首次运行会弹出浏览器要求扫码登录，会话自动保存在 `cache/browser_profile/`，后续复用
- `--headless`：cache 有登录痕迹才启用无头；无头下若出现登录页会报错并立即终止（需去掉 `--headless` 重新扫码）
- 阅读器默认自动匹配目标单屏宽度（可 `READER_VIEWPORT_MAX_*` 限制）；多显示器时必须用 `--prefer-largest-screen` / `--no-prefer-largest-screen` 指定目标屏（不传会报错提示），检测到双页时按 canvas 拆页抓取；`--force-single-page` / `--no-force-single-page` 可临时覆盖 `.env` 的 `READER_FORCE_SINGLE_PAGE`（强制失败会恢复宽视口）
- 自动跳到全书开头（原生点击目录首项），逐页翻到全书末尾自动停止
- **自动续传**：中途卡住会重开浏览器，从上次章节继续；中间产物在 `output/<book_id>/`。
  续传前会核对中间产物与 `_catalog.json`（缺章过多、或最大章正文里独立成行混进其他目录标题），
  产物被污染时直接报错中止——**重抓前请把成品和 `output/<book_id>/` 一起删掉**，
  只删成品会让重跑直接复用旧 md（`--force` 也绕不过）
- **全书成功后**才写入 `~/data/weixin/books/<book_id>_<书名>.json`（字段对齐 dedao/json：纯文本 content、`has_content`、空元数据键；可用 `BOOKS_DIR` / `--out-dir` 覆盖）
- 章切换后按该章字数动态等待：`ceil(字数/2000)*SLEEP_CHAPTER_PER_2K_CHARS`，夹在 `SLEEP_CHAPTER_MIN`–`SLEEP_CHAPTER_MAX`（默认 0.3–1 秒）
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
python fetch_shelf.py --sleep 5 --max-no-new 6

# 连续 10 本本地已有就停止翻页；0 表示滚完全部
python fetch_shelf.py --known-streak 10

# 跳过「作者为空时打开详情补全」
python fetch_shelf.py --no-enrich-author

# 临时覆盖详情补全间隔（默认 .env SLEEP_BOOK_DETAIL_INTERVAL=5）
python fetch_shelf.py --author-interval 8
```

- 慢滚动触发懒加载（`/web/shelf/syncBook` 约每批 100 本），跨屏**累积** DOM reader id，避免只抓到首批
- 本地 `shelf_books.txt` 已有的书按从上到下计数：当前屏里连续 10 本都已收录，或视口较小、跨屏把连续计数凑满 10，就不再往下滚。本次抓到的书和没翻到的旧书合并写回，避免半截结果覆盖整个书架。`--known-streak 0` 关闭该提前停止
- 默认 `--max-no-new 5`；书特别多时可加大 `--max-scrolls`
- 每本书提取 `id`、`title`、`author`，存为 `data/shelf_books.txt`（一行一条，逗号分隔：ID,书名,作者；书名/作者中的逗号替换为空格）
- 文件里历史遗留的脏行（首字段不是 reader book_id，如早期格式残留的 `主编,,`）在读取时丢弃、落盘前清理，不会再被拿去打开无效的详情页
- 书架页禁用 F12 不影响抓取：`id`/`title` 取自页面 DOM，`author` 优先取自拦截的书架接口；**仍为空时再打开阅读器详情补全**（已有作者的书不打开；相邻两本默认间隔 5s）
- 参数：`--headless`、`--sleep` 滚动间隔、`--max-no-new`、`--known-streak`、`--out`、`--no-enrich-author`、`--author-interval`
- 所有点击/跳转类等待见 `.env` / `AGENTS.md`（`SLEEP_*`）

> 登录过期时 `--headless` 无法弹扫码页，去掉 `--headless` 重新扫一次即可。

### 4. 空叶子章节体检与增量修复

批量导出偶发“目录里有、但抓到的正文为空”的叶子章节（最后一章豁免）。
这类问题不需要整本重下，用 `repair_empty_chapters.py` 只补抓空章：

```bash
# 空跑：列出问题书 ID/书名与空章（默认模式，不改任何文件）
python repair_empty_chapters.py --dry-run
python repair_empty_chapters.py --dry-run --book <book_id>

# 只补抓空叶子章：不改 chapter_id、不插入/删除章节，仅更新 content/word_count
python repair_empty_chapters.py --apply --no-prefer-largest-screen --book <book_id> \
  --catalog-root output

# 先刷新书架缓存再扫描（fetch_shelf.py 需可登录）
python repair_empty_chapters.py --refresh-shelf --dry-run
```

- 只处理微信读书来源：ID 在 `data/shelf_books.txt`，或 `--catalog-root` 下有
  `_catalog.json`；排除带 `source_url` / `kind`（十点古籍/搜韵/ctext）及非法 ID 的书。
- 口径：内容为空的非末章叶子；书前/书末无正文项默认豁免（`--strict` 可含）；
  章节数 ≤2 默认豁免（`--min-chapters` 可调）。
- `--apply` 可加 `--limit N` / `--max-chapters N` 控制规模，逐本失败默认继续，
  `--fail-fast` 改为失败即停；写 JSON 前自动备份到 `output/<id>/backups/`。
- 本脚本只补「章在、正文空」；「目录里有、却被并进邻章」的章节缺失用下一节的
  `repair_missing_chapters.py` 重切，不必整本重下。

### 5. 章节缺失重切（把并进邻章的章切回来）

体检报告里「目录里有、导出章节里没有、标题在某章正文中独立成行」的书，正文其实都在
文件里，只是章界错位。`repair_missing_chapters.py` 按目录章名在 md 里找切点，只切不改字：

```bash
# 空跑：列出可重切的书、补回章数与切点（默认模式，不改任何文件）
python repair_missing_chapters.py --dry-run
python repair_missing_chapters.py --dry-run --book <book_id> --json

# 落盘：备份 → 重写 chapters/raw → 用同一管线重建成品 JSON
python repair_missing_chapters.py --apply --book <book_id>
```

- 切点要求：标题整行相等、每章只用一次、目录顺序单调递增；卷标题、作者分组行、
  折行标题与句中提及由 `detect_merged_chapters.missing_catalog_titles()` 排除。
- 安全校验：切分后正文逐行必须与切分前**完全一致**，有任何丢失即整本放弃；
  原本就存在的空章（只有标题的占位章）原样保留，不会因重切被吞掉。
- 会重新编号章号（`ch_0001…`，因为确实插入了新章）；成品 JSON 与 `chapters/`、`raw/`
  一起备份到 `output/<id>/backups/<时间戳>/`，出版信息从旧 JSON 继承。
- 只在切点确实存在时才动文件，扫描口径与 `detect_merged_chapters.py` 的「章节缺失」一致。

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
├── forbid_books.txt         # 禁止下载的 weread ID（每行一个；批量导出跳过，去重记 dup）
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
  浏览器登录 → 打开目录抓取章名列表（仅开篇/续传时点一次目录）
    → 键盘 ArrowRight / 点阅读区中右 线性逐页翻（中途不再频繁点目录）
    → 每页: Canvas Hook 捕获文字 + 视口内图片 URL
    → 按正文/顶栏切章；长章持续翻页抓取，仅停滞时重开阅读器续传
    → 翻到目录最后一章自动停止

download_images.py:
  读 raw/*.json 图片 URL → 强制 IPv4 + 8 线程并发下载 → images/

fetch_shelf.py:
  复用缓存登录打开书架 → 慢滚动触发懒加载
    → 当前屏连续已有 N 本则停止翻页，否则滚到没有新书
    → DOM 抓 id/title + 拦截书架接口取 author → 按 book_id 合并去重
    → 提前停止时保留未翻到的旧书
    → author 仍为空则逐本打开阅读器补全（已有作者跳过；间隔 SLEEP_BOOK_DETAIL_INTERVAL）
    → 写 data/shelf_books.txt
```

## 声明

仅供个人学习研究使用。请勿用于商业用途或大规模传播，请尊重著作权。
