#!/usr/bin/env python3
"""
微信读书导出 — 精确图文版 v3

逐页捕获：每翻一页，抓当前视口内的 canvas 文字 + 视口内图片，
按屏幕 y 坐标把文字行和图片交错排序，图片精确落在对应段落之间。
双页拆分(左页→右页)，按章节切分，自动续传，卡住重开。
"""
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

import env_config  # noqa: F401  # 导入即加载 .env
from book_json import (
    book_json_exists,
    build_book_json_from_chapter_mds,
    chapter_sleep_seconds,
    filter_pending_books,
    iter_batch_book_ids,
    load_chapters_from_export_dir,
    write_book_json,
)
from env_config import (
    BOOKS_DIR,
    READER_VIEWPORT_HEIGHT,
    READER_VIEWPORT_WIDTH,
    SLEEP_BOOK_INTERVAL,
    SLEEP_CHAPTER_MAX,
    SLEEP_CHAPTER_MIN,
    SLEEP_CHAPTER_PER_1K_CHARS,
    SLEEP_READER_AFTER_HOOK,
    SLEEP_READER_AFTER_LOAD,
    SLEEP_READER_CATALOG_CLICK,
    SLEEP_READER_CATALOG_CLOSE,
    SLEEP_READER_CATALOG_OPEN,
    SLEEP_READER_CATALOG_SCROLL,
    SLEEP_READER_PAGE_RENDER,
    SLEEP_READER_PAGE_TURN,
    SLEEP_READER_REOPEN,
    SLEEP_READER_STABLE_POLL,
)
from weread_session import (
    USER_DATA_DIR,
    ensure_logged_in,
    launch_weread_context,
    page_needs_login,
    resolve_headless,
)

DEFAULT_BOOKS_DIR = BOOKS_DIR


def format_elapsed(seconds):
    """把耗时格式化为秒或分钟（>=60 秒用分钟）。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        if abs(seconds - round(seconds)) < 0.05:
            return f"{int(round(seconds))} 秒"
        return f"{seconds:.1f} 秒"
    minutes = seconds / 60.0
    if abs(minutes - round(minutes)) < 0.05:
        return f"{int(round(minutes))} 分钟"
    return f"{minutes:.1f} 分钟"

DEFAULT_NEW_BOOKS = Path("data") / "new_books.txt"


def reader_viewport():
    """导出用阅读器视口。默认偏窄以强制单页（避免双页左右 canvas）。"""
    w = max(360, int(READER_VIEWPORT_WIDTH or 800))
    h = max(480, int(READER_VIEWPORT_HEIGHT or 900))
    return {"width": w, "height": h}


def viewport_focus_point(viewport=None):
    """点击聚焦阅读器内容区的坐标（视口中心略偏上）。"""
    vp = viewport or reader_viewport()
    return int(vp["width"] * 0.5), int(vp["height"] * 0.45)


async def count_reader_canvases(page):
    """可见正文 canvas 数量（高度足够的才算阅读页）。"""
    return await page.evaluate(
        """() => Array.from(document.querySelectorAll('canvas'))
            .map(c => c.getBoundingClientRect())
            .filter(r => r.height > 300 && r.width > 100).length"""
    )


async def ensure_single_page_reader(page, viewport):
    """若检测到双页布局，逐步收窄视口并刷新，尽量落到单页。

    微信读书 web 在宽视口下会并排渲染左右两页（两个 canvas）。
    返回实际采用的 viewport。
    """
    vp = dict(viewport)
    n = await count_reader_canvases(page)
    if n <= 1:
        if n == 1:
            print("  📄 阅读布局: 单页")
        return vp

    print(f"  ⚠️  检测到双页布局（canvas={n}），尝试收窄视口强制单页…")
    for w in (720, 640, 560, 480):
        if w >= int(vp.get("width") or 0):
            continue
        vp = {"width": w, "height": int(vp.get("height") or 900)}
        await page.set_viewport_size(vp)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"    reload 异常: {e}")
        await asyncio.sleep(SLEEP_READER_AFTER_LOAD)
        n = await count_reader_canvases(page)
        print(f"    视口 {w}x{vp['height']} → canvas={n}")
        if n <= 1:
            print("  📄 阅读布局: 单页")
            return vp

    print(f"  ⚠️  仍为双页（canvas={n}），将依赖按 canvas 拆页兜底")
    return vp


CANVAS_HOOK = r"""
(function() {
    window.__wr_chars = [];
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && String(text).trim()) {
            var cl = 0, ct = 0, s = null;
            try {
                var canvas = this.canvas;
                if (canvas && canvas.getBoundingClientRect) {
                    var r = canvas.getBoundingClientRect();
                    cl = Math.round(r.left);
                    ct = Math.round(r.top);
                }
            } catch (e) {}
            try {
                var m = /(\d+(?:\.\d+)?)px/.exec(this.font || '');
                if (m) s = parseFloat(m[1]);
            } catch (e) {}
            window.__wr_chars.push({
                t: String(text),
                x: Math.round(x * 10) / 10,
                y: Math.round(y * 10) / 10,
                cl: cl,
                ct: ct,
                s: s
            });
        }
        return origFill.apply(this, arguments);
    };
    window.__wr_reset = function() { window.__wr_chars = []; };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

# 当前视口内可见的书籍插图，带屏幕坐标
VIEWPORT_IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' &&
            getComputedStyle(i).display !== 'none') {
            out.push({src, top: Math.round(r.top), left: Math.round(r.left),
                      w: i.naturalWidth||i.width, h: i.naturalHeight||i.height});
        }
    });
    return out;
}
"""

# reader 的两个 canvas 的屏幕位置
CANVAS_RECTS_JS = """
() => Array.from(document.querySelectorAll('canvas')).map(c => {
    const r = c.getBoundingClientRect();
    return {top: r.top, left: r.left, w: Math.round(r.width), h: Math.round(r.height)};
}).filter(r => r.h > 300)
"""

MEASURE_RE = re.compile(r'^[a-zA-Z0-9`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]+$')
SENTENCE_END = set("。！？；：」）】》…—")
# canvas 软折行合并阈值：短于此长度的行（词牌/作者名等）不与邻行粘连
SOFT_WRAP_MIN_LEN = 16
# 标题前缀后若接这些成分，视为正文提及而非新章起始
_NOT_CHAPTER_START_REST = re.compile(
    r"^(的|与|和|在|是|了|也|都|就|还|曾|并|便|则|却|又|已|将|会|能|要|"
    r"把|被|让|从|向|对|比|因|而|但|曾经|这首|早在|不过|与他|便是)"
)


def is_title_like_line(line: str) -> bool:
    """短标题行（词牌、作者小标题、【赏析】等）不应参与软折行粘连。"""
    s = (line or "").strip()
    if not s:
        return False
    if s.startswith("【") and s.endswith("】"):
        return True
    if len(s) <= 10 and not any(ch in SENTENCE_END or ch in "，、,." for ch in s):
        return True
    return False


def should_merge_soft_wrap(prev: str, cur: str) -> bool:
    """是否把 cur 接到 prev 末尾（canvas 软折行）。"""
    if not prev or not cur:
        return False
    if is_title_like_line(prev) or is_title_like_line(cur):
        return False
    if prev[-1] in SENTENCE_END:
        return False
    # 仅当上一行足够长，才更像折行而非独立短行/标题
    return len(prev) >= SOFT_WRAP_MIN_LEN


def is_chapter_start_text(text: str, chapter_title: str) -> bool:
    """判断一行文字是否为下一章起始（目录标题行或标题+词牌粘连）。"""
    t = (text or "").strip()
    title = (chapter_title or "").strip()
    if not t or not title:
        return False
    if t == title:
        return True
    if not t.startswith(title):
        return False
    rest = t[len(title):]
    if not rest:
        return True
    if rest[0] in "，、,;；。！？":
        return False
    if _NOT_CHAPTER_START_REST.match(rest):
        return False
    # 软折行误切：作者名出现在段中换行处，后接散文动词/虚词已在上面过滤；
    # 其余允许「张志和渔父…」「范仲淹苏幕遮…」这类标题+词牌同行。
    return True


def split_blocks_at_chapter_start(blocks, chapter_title: str):
    """在 blocks 中按 chapter_title 章首切分为 (before, after)。

    after 为空表示未找到章首；before 可能为空（整页已属新章）。
    """
    title = (chapter_title or "").strip()
    if not title or not blocks:
        return list(blocks or []), []
    for i, b in enumerate(blocks):
        if b.get("type") != "text":
            continue
        if is_chapter_start_text(b.get("text") or "", title):
            return blocks[:i], blocks[i:]
    return list(blocks), []


def next_catalog_title(catalog_titles, current_title: str):
    """返回目录中 current_title 的下一章标题；找不到则 None。"""
    if not catalog_titles or not current_title:
        return None
    try:
        idx = catalog_titles.index(current_title)
    except ValueError:
        return None
    if idx + 1 >= len(catalog_titles):
        return None
    return catalog_titles[idx + 1]


def group_chars_by_canvas(chars):
    """按 canvas 屏幕 left(cl) 把字符分到各页，从左到右返回。

    fillText 的 x/y 是 canvas 局部坐标；双页时左右页 y 区间重叠，
    若不按 canvas 拆开再分行，会把左右页同一 y 的字交错拼成乱码。
    """
    if not chars:
        return []
    buckets: dict[int | None, list] = {}
    for c in chars:
        cl = c.get("cl")
        if cl is None:
            buckets.setdefault(None, []).append(c)
            continue
        try:
            cl_v = float(cl)
        except (TypeError, ValueError):
            buckets.setdefault(None, []).append(c)
            continue
        key = None
        for k in buckets:
            if k is not None and abs(k - cl_v) <= 40:
                key = k
                break
        if key is None:
            key = int(round(cl_v))
        buckets.setdefault(key, []).append(c)

    ordered_keys = sorted(k for k in buckets if k is not None)
    pages = [buckets[k] for k in ordered_keys]
    if None in buckets:
        if pages:
            # 无 cl 的旧数据：并入左页，避免丢字
            pages[0].extend(buckets[None])
        else:
            pages = [buckets[None]]
    return pages


def split_spread(chars):
    """双页拆分：优先按 canvas 屏幕位置；否则回退 y 回跳启发式。

    返回 [左页chars, 右页chars, ...] 或 [单页chars]
    """
    if not chars:
        return [chars]

    pages = group_chars_by_canvas(chars)
    if len(pages) >= 2:
        return pages

    # 兼容未带 cl 的旧捕获：按绘制顺序的 y 回跳切分
    if len(chars) < 20:
        return [chars]
    singles = [(i, c) for i, c in enumerate(chars) if len(c["t"]) == 1]
    if len(singles) < 10:
        return [chars]
    for j in range(1, len(singles)):
        if singles[j - 1][1]["y"] > 400 and singles[j][1]["y"] < 200:
            return [chars[:singles[j][0]], chars[singles[j][0]:]]
    return [chars]


def chars_to_lines(chars):
    """把单页字符按 y 分行，返回 [{y, text}]（未合并段落）"""
    real = [c for c in chars if len(c["t"]) == 1 or not MEASURE_RE.match(c["t"])]
    if not real:
        return []
    rows = {}
    for c in real:
        y_key = round(c["y"] / 3) * 3
        rows.setdefault(y_key, []).append(c)
    lines = []
    for yk in sorted(rows):
        line = "".join(c["t"] for c in sorted(rows[yk], key=lambda c: c["x"]))
        if line.strip():
            lines.append({"y": yk, "text": line.strip()})
    return lines


def build_page_blocks(chars, images, canvas_rects, seen_imgs):
    """把一次渲染(可能双页)拆成有序块列表: [{type:'text'/'img', ...}]
       文字行和图片按屏幕 y 交错；左页整页在前，右页在后。"""
    blocks = []
    pages = split_spread(chars)

    # 判定左右 canvas
    rects = sorted(canvas_rects, key=lambda r: r["left"])
    left_rect = rects[0] if rects else {"top": 0, "left": 0}
    right_rect = rects[1] if len(rects) > 1 else left_rect
    mid_x = (left_rect["left"] + right_rect["left"]) / 2 + 180 if len(rects) > 1 else 99999

    # 图片按左右分组
    left_imgs = [im for im in images if im["left"] < mid_x]
    right_imgs = [im for im in images if im["left"] >= mid_x]

    def emit_page(page_chars, page_rect, page_imgs):
        lines = chars_to_lines(page_chars)
        items = []
        for ln in lines:
            items.append(("text", page_rect["top"] + ln["y"], ln["text"]))
        for im in page_imgs:
            if im["src"] in seen_imgs:
                continue
            items.append(("img", im["top"], im))
        items.sort(key=lambda t: t[1])
        for typ, _y, payload in items:
            if typ == "text":
                blocks.append({"type": "text", "text": payload})
            else:
                seen_imgs.add(payload["src"])
                blocks.append({"type": "img", "src": payload["src"],
                                "w": payload["w"], "h": payload["h"]})

    if len(pages) >= 2:
        # 多 canvas：左→右逐页输出；首屏图归左，其余可见图归右/末页
        page_rects = list(rects) if len(rects) >= len(pages) else [left_rect, right_rect]
        while len(page_rects) < len(pages):
            page_rects.append(page_rects[-1])
        for i, page_chars in enumerate(pages):
            if i == 0:
                pimgs = left_imgs
            elif i == len(pages) - 1:
                pimgs = right_imgs
            else:
                pimgs = []
            emit_page(page_chars, page_rects[i], pimgs)
    else:
        # 单页：图片全归这页，仍按 y 排
        emit_page(pages[0] if pages else [], left_rect, left_imgs + right_imgs)
    return blocks


def img_filename(url, ch_idx, seq):
    ext = "jpg"
    m = re.search(r'\.(jpg|jpeg|png|gif|webp)', url.lower())
    if m:
        ext = m.group(1).replace("jpeg", "jpg")
    return f"ch{ch_idx:04d}_img{seq:02d}.{ext}"


def render_chapter_md(ch_title, blocks, ch_idx):
    """把有序块渲染成 Markdown：文字行合并成段落，图片就地插入"""
    out = [f"# {ch_title}\n"]
    para = []
    img_records = []
    img_seq = 0

    def flush_para():
        nonlocal para
        if not para:
            return
        # 合并 canvas 软折行；词牌/短标题/【赏析】等保持独立段落。
        # 注意：不得因 line==ch_title 丢弃正文行——词下作者署名常与章名相同（如「李白」）。
        merged = []
        for line in para:
            if merged and should_merge_soft_wrap(merged[-1], line):
                merged[-1] += line
            else:
                merged.append(line)
        for m in merged:
            if m.strip():
                out.append(m.strip())
        para = []

    for b in blocks:
        if b["type"] == "text":
            para.append(b["text"])
        else:
            flush_para()
            img_seq += 1
            fname = img_filename(b["src"], ch_idx, img_seq)
            out.append(f"![图](images/{fname})")
            img_records.append({"url": b["src"], "file": fname})
    flush_para()

    body = "\n\n".join(out) + "\n"
    return body, img_records


async def wait_stable(page, prev_count, timeout=8):
    """等页面渲染稳定，返回稳定后的字符数"""
    last = -1
    poll = SLEEP_READER_STABLE_POLL
    steps = max(1, int(timeout / poll)) if poll > 0 else 1
    for _ in range(steps):
        c = await page.evaluate("() => window.__wr_count()")
        if c == last:
            return c
        last = c
        await asyncio.sleep(poll)
    return last


def get_last_chapter_title(md_dir):
    if not os.path.exists(md_dir):
        return None, 0
    files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    if not files:
        return None, 0
    idx = int(files[-1].replace(".md", ""))
    with open(os.path.join(md_dir, files[-1])) as f:
        title = f.readline().strip().replace("# ", "")
    return title, idx


def load_catalog_titles(catalog_path):
    """读取目录标题列表；失败返回 []。"""
    try:
        with open(catalog_path) as f:
            titles = json.load(f)
        return titles if isinstance(titles, list) else []
    except Exception:
        return []


def load_last_catalog_title(catalog_path):
    titles = load_catalog_titles(catalog_path)
    return titles[-1] if titles else ""


async def _title(page):
    return await page.evaluate(
        "() => document.querySelector('.renderTargetPageInfo_header_chapterTitle')?.textContent?.trim() || ''")


async def fetch_book_title(page):
    info = await page.evaluate("""() => {
        const title = document.querySelector('.readerCatalog_bookInfo_title_txt, .bookInfo_right_header_title')
            ?.textContent?.trim() || document.title.replace(/-.*$/, '').trim();
        const author = document.querySelector('.readerCatalog_bookInfo_author, .bookInfo_author a')
            ?.textContent?.trim() || '';
        return {title, author};
    }""")
    return info.get("title", "未知"), info.get("author", "")


async def goto_first_chapter(page, catalog_path=None):
    first_title = ""
    try:
        await page.click("button.readerControls_item.catalog", timeout=5000)
        await asyncio.sleep(SLEEP_READER_CATALOG_OPEN)
        titles = await page.evaluate("""() => Array.from(
            document.querySelectorAll('.readerCatalog_list_item')).map(el => el.textContent.trim())""")
        if titles and catalog_path:
            with open(catalog_path, "w") as f:
                json.dump(titles, f, ensure_ascii=False)
        await page.evaluate("""() => {
            const sc = document.querySelector('.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]');
            if (sc) sc.scrollTop = 0;
        }""")
        await asyncio.sleep(SLEEP_READER_CATALOG_SCROLL)
        item = page.locator(".readerCatalog_list_item").first
        first_title = (await item.text_content() or "").strip()
        await item.click(timeout=4000)
        await asyncio.sleep(SLEEP_READER_CATALOG_CLICK)
        try:
            await page.click("button.readerControls_item.catalog", timeout=2000)
        except Exception:
            await page.keyboard.press("Escape")
        await asyncio.sleep(SLEEP_READER_CATALOG_CLOSE)
    except Exception as e:
        print(f"  ⚠️  目录跳转异常: {e}")
    print(f"  ✅ 已跳到全书开头，当前:「{await _title(page)}」(点击首项「{first_title}」)")


def save_chapter(ch_title, blocks, ch_idx, md_dir, raw_dir):
    body, img_records = render_chapter_md(ch_title, blocks, ch_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    if text_len == 0 and not img_records:
        return 0, []
    with open(os.path.join(md_dir, f"{ch_idx:04d}.md"), "w") as f:
        f.write(body)
    with open(os.path.join(raw_dir, f"{ch_idx:04d}.json"), "w") as f:
        json.dump({"title": ch_title, "images": img_records, "text_len": text_len},
                  f, ensure_ascii=False)
    return text_len, img_records


async def run_session(book_id, md_dir, raw_dir, start_idx, seen_imgs,
                      goto_first=False, catalog_path=None, headless=False):
    reached_end = False
    catalog_titles = load_catalog_titles(catalog_path) if catalog_path else []
    last_cat_title = catalog_titles[-1] if catalog_titles else ""
    async with async_playwright() as p:
        viewport = reader_viewport()
        ctx = await launch_weread_context(
            p, headless=headless, viewport=viewport)
        if not await ensure_logged_in(
                ctx, allow_interactive_login=not headless):
            await ctx.close()
            if headless:
                raise RuntimeError(
                    "无头模式下检测到未登录/登录页，已终止。"
                    "请去掉 --headless 扫码登录，或确认 cache/browser_profile 登录态有效后重试。"
                )
            return "", "", 0, 0, start_idx, False

        page = await ctx.new_page()
        await page.add_init_script(CANVAS_HOOK)
        print("\n  打开阅读器...")
        await page.goto(f"https://weread.qq.com/web/reader/{book_id}",
                        wait_until="networkidle", timeout=30000)
        await asyncio.sleep(SLEEP_READER_AFTER_LOAD)
        if headless and await page_needs_login(page):
            await ctx.close()
            raise RuntimeError(
                "无头模式下打开阅读器后出现登录页，已终止。"
                "请去掉 --headless 扫码登录后再试。"
            )

        viewport = await ensure_single_page_reader(page, viewport)
        fx, fy = viewport_focus_point(viewport)

        book_title, book_author = await fetch_book_title(page)
        if goto_first:
            await goto_first_chapter(page, catalog_path)
            catalog_titles = load_catalog_titles(catalog_path) if catalog_path else catalog_titles
            last_cat_title = catalog_titles[-1] if catalog_titles else ""

        await page.mouse.click(fx, fy)
        await asyncio.sleep(SLEEP_READER_AFTER_HOOK)

        current_chapter = await _title(page)
        print(f"  📖 {book_title} — {book_author}")
        print(f"  会话开始:「{current_chapter}」\n")

        ch_idx = start_idx
        ch_blocks = []
        total_chars = total_imgs = 0
        chapters_this_session = 0
        stale = 0
        page_num = 0

        async def capture_current_page():
            """抓当前页的有序块，累加到 ch_blocks；返回是否有新内容"""
            nonlocal ch_blocks
            await asyncio.sleep(SLEEP_READER_PAGE_RENDER)
            chars = await page.evaluate("() => window.__wr_chars")
            rects = await page.evaluate(CANVAS_RECTS_JS)
            imgs = await page.evaluate(VIEWPORT_IMGS_JS)
            before = len(ch_blocks)
            new_blocks = build_page_blocks(chars, imgs, rects, seen_imgs)
            # 文字去重：同一页可能重复捕获，按文本行内容去重
            for b in new_blocks:
                if b["type"] == "text":
                    if ch_blocks and ch_blocks[-1].get("type") == "text" and ch_blocks[-1]["text"] == b["text"]:
                        continue
                ch_blocks.append(b)
            return len(ch_blocks) > before

        async def commit_chapter(title, blocks, *, note_suffix=""):
            """落盘一章并累计统计；返回 (text_len, imgs)。"""
            nonlocal total_chars, total_imgs, chapters_this_session
            n, imgs = save_chapter(title, blocks, ch_idx, md_dir, raw_dir)
            total_chars += n
            total_imgs += len(imgs)
            note = f" +{len(imgs)}图" if imgs else ""
            note += note_suffix
            print(f"  [{ch_idx:4d}] {title[:32]:32s} {n:6d}字 ({page_num}页){note}")
            chapters_this_session += 1
            return n, imgs

        async def sleep_between_chapters(n_chars):
            wait_s = chapter_sleep_seconds(
                n_chars,
                per_1k=SLEEP_CHAPTER_PER_1K_CHARS,
                min_seconds=SLEEP_CHAPTER_MIN,
                max_seconds=SLEEP_CHAPTER_MAX,
            )
            print(f"    … 章间等待 {wait_s:.1f}s（按 {n_chars} 字）")
            await asyncio.sleep(wait_s)

        async def split_if_next_chapter_started():
            """目录下一章标题已出现在正文块中时，提前切章（修复标题栏滞后导致的窜章）。

            返回 True 表示发生了切章。
            """
            nonlocal ch_blocks, current_chapter, ch_idx, page_num, stale, reached_end
            nxt = next_catalog_title(catalog_titles, current_chapter)
            if not nxt:
                return False
            before, after = split_blocks_at_chapter_start(ch_blocks, nxt)
            if not after:
                return False
            is_last = bool(last_cat_title and current_chapter == last_cat_title)
            if before:
                n, _imgs = await commit_chapter(
                    current_chapter, before, note_suffix=" [内容切章]")
                ch_idx += 1
                if is_last:
                    ch_blocks = after
                    current_chapter = nxt
                    page_num = 0
                    stale = 0
                    reached_end = True
                    return True
                await sleep_between_chapters(n)
            # before 为空：上一章已落盘，仅把块归属切到目录下一章
            ch_blocks = after
            current_chapter = nxt
            page_num = 0
            stale = 0
            return True

        # 首页
        await page.evaluate("() => window.__wr_reset()")
        await wait_stable(page, 0)
        await capture_current_page()
        while await split_if_next_chapter_started():
            if reached_end:
                break

        while not reached_end:
            await page.evaluate("() => window.__wr_reset()")
            await page.mouse.click(fx, fy)
            await page.keyboard.press("ArrowRight")
            await asyncio.sleep(SLEEP_READER_PAGE_TURN)
            await wait_stable(page, 0)

            new_chapter = await _title(page)
            if new_chapter and new_chapter != current_chapter:
                # 标题栏切换：先把已窜入上一章末尾的新章内容剥回
                before, after = split_blocks_at_chapter_start(ch_blocks, new_chapter)
                if after:
                    ch_blocks = before
                is_last = bool(last_cat_title and current_chapter == last_cat_title)
                n, _imgs = await commit_chapter(current_chapter, ch_blocks)
                ch_idx += 1
                ch_blocks = after
                current_chapter = new_chapter
                page_num = 0
                stale = 0
                if not is_last:
                    await sleep_between_chapters(n)
                await capture_current_page()
                # 当前页也可能继续跨到再下一章
                while await split_if_next_chapter_started():
                    if reached_end:
                        break
                if is_last:
                    reached_end = True
                    break
                continue

            got_new = await capture_current_page()
            split = False
            while await split_if_next_chapter_started():
                split = True
                if reached_end:
                    break
            if reached_end:
                break
            if split:
                continue
            if not got_new:
                stale += 1
                if stale >= 10:
                    note = ""
                    if last_cat_title and current_chapter == last_cat_title:
                        reached_end = True
                        note = " [全书末尾]"
                    await commit_chapter(current_chapter, ch_blocks, note_suffix=note)
                    break
            else:
                stale = 0
            page_num += 1

        # reached_end 时把最后一章存下
        if reached_end and ch_blocks:
            n, imgs = save_chapter(current_chapter, ch_blocks, ch_idx, md_dir, raw_dir)
            if n or imgs:
                total_chars += n; total_imgs += len(imgs)
                print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} {n:6d}字 [末章]")
                chapters_this_session += 1

        await page.close(); await ctx.close()
        return book_title, book_author, chapters_this_session, total_chars, ch_idx, reached_end


def download_all_images(raw_dir, img_dir):
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    for jf in sorted(os.listdir(raw_dir)):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                tasks.append((img["url"], img["file"]))
    if not tasks:
        print("  (无图片)"); return 0
    print(f"\n  下载 {len(tasks)} 张图片...")
    ok = 0
    for url, fname in tasks:
        fp = os.path.join(img_dir, fname)
        if os.path.exists(fp) and os.path.getsize(fp) > 1000:
            ok += 1; continue
        try:
            req = urllib.request.Request(url, headers={
                "Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                open(fp, "wb").write(raw); ok += 1
                if ok % 20 == 0:
                    print(f"    {ok}/{len(tasks)}...")
        except Exception as e:
            print(f"    ⚠️  {fname} 失败: {e}")
    print(f"  ✅ 图片下载完成 {ok}/{len(tasks)}")
    return ok


def finalize_book_json(book_id, book_title, book_author, book_dir, out_dir):
    """从中间产物组装并写出兼容 JSON；返回写出路径。"""
    pairs = load_chapters_from_export_dir(book_dir)
    if not pairs:
        raise RuntimeError(f"未找到可写出的章节：{book_dir}/chapters")
    book = build_book_json_from_chapter_mds(
        book_id=book_id,
        title=book_title or book_id,
        author=book_author or "",
        chapter_files=pairs,
    )
    path = write_book_json(out_dir, book)
    return path, book


async def export_one_book(
    book_id,
    *,
    out_dir=DEFAULT_BOOKS_DIR,
    force=False,
    download_images=False,
    shelf_title="",
    shelf_author="",
    headless=False,
):
    """导出单本：中间产物写 output/<id>，成功后写 out_dir JSON。

    返回 ("skipped"|"ok", message)
    """
    out_dir = Path(out_dir)
    if not force and book_json_exists(out_dir, book_id):
        msg = f"已存在 JSON，跳过 {book_id}（使用 --force 可重导）"
        print(f"  ⏭  {msg}")
        return "skipped", msg

    print("=" * 60)
    print("  weread-exporter — 精确图文导出 v3 + JSON")
    print("=" * 60)
    started_at = time.monotonic()
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    book_dir = os.path.join("output", book_id)
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    for d in (md_dir, raw_dir, img_dir):
        os.makedirs(d, exist_ok=True)

    seen_imgs = set()
    for jf in os.listdir(raw_dir):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                seen_imgs.add(img["url"])

    catalog_path = os.path.join(book_dir, "_catalog.json")
    book_title = shelf_title or ""
    book_author = shelf_author or ""
    session = 0
    while True:
        session += 1
        last_title, last_idx = get_last_chapter_title(md_dir)
        start_idx = last_idx + 1 if last_idx > 0 else 1
        print(f"\n--- 会话 {session} ---")
        print(f"  上次: {last_title or '(无)'}, 编号: {last_idx}")
        goto_first = (session == 1 and last_idx == 0)
        title, author, added, chars_added, end_idx, reached_end = await run_session(
            book_id, md_dir, raw_dir, start_idx, seen_imgs,
            goto_first=goto_first, catalog_path=catalog_path, headless=headless)
        if title:
            book_title = title
        if author:
            book_author = author
        print(f"\n  本次: +{added} 章, +{chars_added:,} 字")
        if reached_end:
            print("\n  ✅ 已到全书最后一章，导出完成。")
            break
        if added == 0:
            # 若已有章节产物则视为完成（续传场景）
            if any(fn.endswith(".md") for fn in os.listdir(md_dir)):
                print("\n  无新章节，按已有产物收尾。")
                break
            raise RuntimeError(f"未能导出任何章节：{book_id}")
        print(f"  {SLEEP_READER_REOPEN} 秒后自动重开继续...")
        await asyncio.sleep(SLEEP_READER_REOPEN)

    if download_images:
        download_all_images(raw_dir, img_dir)
    else:
        print("  （跳过图片下载；需要时加 --download-images）")

    total_files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    if not total_files:
        raise RuntimeError(f"章节目录为空，无法写 JSON：{md_dir}")
    if not book_title:
        book_title = book_id

    # 仍写出合并 md，便于人工预览
    safe = re.sub(r'[<>:"/\\|?*]', '_', book_title)
    merged = os.path.join("output", f"{safe}.md")
    with open(merged, "w", encoding="utf-8") as out:
        out.write(f"# {book_title}\n\n**{book_author}**\n\n---\n\n")
        for fn in total_files:
            out.write(open(os.path.join(md_dir, fn), encoding="utf-8").read())
            out.write("\n\n---\n\n")

    json_path, book = finalize_book_json(
        book_id, book_title, book_author, book_dir, out_dir)
    img_count = len([f for f in os.listdir(img_dir) if not f.startswith(".")]) if os.path.isdir(img_dir) else 0
    elapsed = time.monotonic() - started_at
    word_count = int(book.get("word_count") or 0)
    print(f"\n{'=' * 60}")
    print(f"  ✅ 全书导出完成!  📖 {book_title} — {book_author}")
    print(f"  📄 {len(total_files)} 章, word_count={word_count},  🖼 {img_count} 张图")
    print(f"  📝 共 {word_count:,} 字，耗时 {format_elapsed(elapsed)}")
    print(f"  📦 md: {merged}")
    print(f"  📦 json: {json_path}")
    print(f"{'=' * 60}")
    return "ok", str(json_path)


async def export_batch(
    *,
    list_path=DEFAULT_NEW_BOOKS,
    out_dir=DEFAULT_BOOKS_DIR,
    force=False,
    download_images=False,
    book_interval=None,
    headless=False,
):
    """批量导出 new_books：跳过已存在；失败即停。"""
    out_dir = Path(out_dir)
    list_path = Path(list_path)
    interval = SLEEP_BOOK_INTERVAL if book_interval is None else float(book_interval)
    books = iter_batch_book_ids(list_path)
    if not books:
        print(f"  清单为空或不存在：{list_path}")
        return 0
    pending = books if force else filter_pending_books(books, out_dir)
    print(f"  清单 {len(books)} 本，待处理 {len(pending)} 本 → {out_dir}")
    if not pending:
        print("  没有待导出的书。")
        return 0

    for i, (book_id, title, author) in enumerate(pending):
        print(f"\n##### 批量 {i + 1}/{len(pending)}  {book_id}  {title} — {author}")
        status, msg = await export_one_book(
            book_id,
            out_dir=out_dir,
            force=force,
            download_images=download_images,
            shelf_title=title,
            shelf_author=author,
            headless=headless,
        )
        if status == "ok" and i < len(pending) - 1:
            print(f"  书间等待 {interval:.0f}s ...")
            await asyncio.sleep(interval)
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="微信读书导出为兼容 dedao/json 的书稿 JSON（并保留 md 中间产物）",
    )
    parser.add_argument(
        "book",
        nargs="?",
        default=None,
        help="book_id 或 weread reader URL；省略则批量处理 data/new_books.txt",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使默认 books 目录已存在同 id 的 JSON 也强制重导",
    )
    parser.add_argument(
        "--download-images",
        action="store_true",
        help="导出结束后下载插图（默认不下载）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_BOOKS_DIR),
        help=f"JSON 输出目录（默认 {DEFAULT_BOOKS_DIR})",
    )
    parser.add_argument(
        "--list",
        dest="list_path",
        default=str(DEFAULT_NEW_BOOKS),
        help=f"批量清单路径（默认 {DEFAULT_NEW_BOOKS}）",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（仅当 cache/browser_profile 已有登录信息时生效；否则回退有头）",
    )
    return parser.parse_args(argv)


def resolve_book_id(raw: str) -> str:
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise ValueError("empty book id")
    if "weread.qq.com" in text:
        return text.split("/")[-1]
    return text


async def async_main(argv=None):
    args = parse_args(argv)
    headless = resolve_headless(args.headless)
    if args.headless and headless:
        print("  🕶️  无头模式已启用（检测到 cache 登录信息）")
    if args.book:
        book_id = resolve_book_id(args.book)
        print(f"  Book ID: {book_id}")
        status, msg = await export_one_book(
            book_id,
            out_dir=args.out_dir,
            force=args.force,
            download_images=args.download_images,
            headless=headless,
        )
        if status == "skipped":
            return 0
        return 0
    # batch
    await export_batch(
        list_path=args.list_path,
        out_dir=args.out_dir,
        force=args.force,
        download_images=args.download_images,
        headless=headless,
    )
    return 0


def main(argv=None):
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception as e:
        print(f"\n❌ 导出失败: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
