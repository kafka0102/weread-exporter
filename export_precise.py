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
from weread_session import USER_DATA_DIR, ensure_logged_in, launch_weread_context

DEFAULT_BOOKS_DIR = BOOKS_DIR
DEFAULT_NEW_BOOKS = Path("data") / "new_books.txt"

CANVAS_HOOK = """
(function() {
    window.__wr_chars = [];
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim())
            window.__wr_chars.push({t: text, x: Math.round(x*10)/10, y: Math.round(y*10)/10});
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


def split_spread(chars):
    """双页拆分：返回 [左页chars, 右页chars] 或 [单页chars]"""
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

    if len(pages) == 2:
        emit_page(pages[0], left_rect, left_imgs)
        emit_page(pages[1], right_rect, right_imgs)
    else:
        # 单页：图片全归这页，仍按 y 排
        emit_page(pages[0], left_rect, left_imgs + right_imgs)
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
        # 合并 canvas 断行为自然段：上一行不以句末标点结尾则接续
        merged = []
        for line in para:
            if line == ch_title:
                continue
            if merged and merged[-1] and merged[-1][-1] not in SENTENCE_END:
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


def load_last_catalog_title(catalog_path):
    try:
        with open(catalog_path) as f:
            titles = json.load(f)
        return titles[-1] if titles else ""
    except Exception:
        return ""


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
                      goto_first=False, catalog_path=None):
    reached_end = False
    last_cat_title = load_last_catalog_title(catalog_path) if catalog_path else ""
    async with async_playwright() as p:
        ctx = await launch_weread_context(
            p, headless=False, viewport={"width": 1200, "height": 900})
        if not await ensure_logged_in(ctx):
            await ctx.close()
            return "", "", 0, 0, start_idx, False

        page = await ctx.new_page()
        await page.add_init_script(CANVAS_HOOK)
        print("\n  打开阅读器...")
        await page.goto(f"https://weread.qq.com/web/reader/{book_id}",
                        wait_until="networkidle", timeout=30000)
        await asyncio.sleep(SLEEP_READER_AFTER_LOAD)

        book_title, book_author = await fetch_book_title(page)
        if goto_first:
            await goto_first_chapter(page, catalog_path)
            last_cat_title = load_last_catalog_title(catalog_path)

        await page.mouse.click(600, 450)
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

        # 首页
        await page.evaluate("() => window.__wr_reset()")
        await wait_stable(page, 0)
        await capture_current_page()

        while True:
            await page.evaluate("() => window.__wr_reset()")
            await page.mouse.click(600, 450)
            await page.keyboard.press("ArrowRight")
            await asyncio.sleep(SLEEP_READER_PAGE_TURN)
            await wait_stable(page, 0)

            new_chapter = await _title(page)
            if new_chapter and new_chapter != current_chapter:
                # 章节切换：保存上一章
                n, imgs = save_chapter(current_chapter, ch_blocks, ch_idx, md_dir, raw_dir)
                total_chars += n; total_imgs += len(imgs)
                note = f" +{len(imgs)}图" if imgs else ""
                print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} {n:6d}字 ({page_num}页){note}")
                is_last = bool(last_cat_title and current_chapter == last_cat_title)
                ch_idx += 1
                chapters_this_session += 1
                ch_blocks = []
                current_chapter = new_chapter
                page_num = 0
                stale = 0
                if not is_last:
                    wait_s = chapter_sleep_seconds(
                        n,
                        per_1k=SLEEP_CHAPTER_PER_1K_CHARS,
                        min_seconds=SLEEP_CHAPTER_MIN,
                        max_seconds=SLEEP_CHAPTER_MAX,
                    )
                    print(f"    … 章间等待 {wait_s:.1f}s（按 {n} 字）")
                    await asyncio.sleep(wait_s)
                await capture_current_page()
                if is_last:
                    reached_end = True
                    # 再存这最后一章
                    break
                continue

            got_new = await capture_current_page()
            if not got_new:
                stale += 1
                if stale >= 10:
                    n, imgs = save_chapter(current_chapter, ch_blocks, ch_idx, md_dir, raw_dir)
                    total_chars += n; total_imgs += len(imgs)
                    note = f" +{len(imgs)}图" if imgs else ""
                    if last_cat_title and current_chapter == last_cat_title:
                        reached_end = True; note += " [全书末尾]"
                    print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} {n:6d}字 ({page_num}页){note}")
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
            goto_first=goto_first, catalog_path=catalog_path)
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
    print(f"\n{'=' * 60}")
    print(f"  ✅ 全书导出完成!  📖 {book_title} — {book_author}")
    print(f"  📄 {len(total_files)} 章, word_count={book['word_count']},  🖼 {img_count} 张图")
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
    if args.book:
        book_id = resolve_book_id(args.book)
        print(f"  Book ID: {book_id}")
        status, msg = await export_one_book(
            book_id,
            out_dir=args.out_dir,
            force=args.force,
            download_images=args.download_images,
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
