#!/usr/bin/env python3
"""微信读书 - 书架书籍列表抓取。

复用 weread_session 的缓存登录会话打开书架页，慢滚动触发懒加载，
DOM 抓取书籍 id/title，同时通过 context 级网络拦截捕获书架接口响应
取 author（页面禁用 F12 不影响 Playwright 的 CDP 级监听），按 book_id
合并后写入 data/shelf_books.txt（一行一条，逗号分隔：ID,书名,作者）。
作者仍为空时再逐本打开阅读器详情补全，相邻两本默认间隔 5s
（见 .env / AGENTS.md）。

用法：
    python fetch_shelf.py                 # 可见浏览器
    python fetch_shelf.py --headless      # 无头（需已缓存登录态）
    python fetch_shelf.py --sleep 5 --max-no-new 4
    python fetch_shelf.py --no-enrich-author
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re

from playwright.async_api import async_playwright

import env_config  # noqa: F401  # 导入即加载 .env
from env_config import (
    SLEEP_BOOK_DETAIL_CATALOG,
    SLEEP_BOOK_DETAIL_INTERVAL,
    SLEEP_BOOK_DETAIL_LOAD,
    SLEEP_SHELF_AFTER_LOAD,
    SLEEP_SHELF_SCROLL,
)
from weread_session import SHELF_URL, ensure_logged_in, launch_weread_context

DATA_DIR = "data"
DEFAULT_OUT = os.path.join(DATA_DIR, "shelf_books.txt")
READER_URL_TMPL = "https://weread.qq.com/web/reader/{book_id}"

_READER_ID_RE = re.compile(r"/reader/([A-Za-z0-9]+)")

# 阅读器页提取作者：先看书信息区，再尝试目录面板常见节点。
EXTRACT_AUTHOR_JS = r"""
() => {
    const sels = [
        '.readerCatalog_bookInfo_author',
        '.bookInfo_author a',
        '.bookInfo_author',
        '.readerBookInfo_author',
        '[class*="bookInfo"][class*="author"]',
        '[class*="bookInfo_author"]',
    ];
    for (const s of sels) {
        const el = document.querySelector(s);
        const t = (el?.textContent || '').replace(/\s+/g, ' ').trim();
        if (t) return t;
    }
    return '';
}
"""


def extract_book_id_from_href(href):
    """从链接 href 中提取 book_id（reader URL 末段）。无匹配返回空串。"""
    if not href:
        return ""
    m = _READER_ID_RE.search(href)
    return m.group(1) if m else ""


def is_reader_book_id(book_id):
    """判断是否为可用于 web/reader URL 的真实 book_id。

    微信读书 reader 末段为字母数字串（常见 23–24 位，含字母）。
    纯数字短 id（如 3300215708）常见于 shelf API 的冗余字段，无法打开阅读器。
    """
    bid = str(book_id or "").strip()
    if not bid or not bid.isalnum():
        return False
    if bid.isdigit():
        return False
    return len(bid) >= 16


def collect_books_from_json(obj, out=None):
    """递归遍历 shelf API 的 JSON，收集含 bookId 的对象到 out[id] = {title, author}。

    兼容 bookId / book_id、title / bookName / name、author / authorName 等字段名。
    原地修改 out 并返回，便于在响应回调中累积。
    """
    if out is None:
        out = {}
    if isinstance(obj, dict):
        bid = obj.get("bookId") or obj.get("book_id")
        if bid:
            out[str(bid)] = {
                "title": obj.get("title") or obj.get("bookName") or obj.get("name") or "",
                "author": obj.get("author") or obj.get("authorName") or "",
            }
        for v in obj.values():
            collect_books_from_json(v, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_books_from_json(item, out)
    return out


def merge_books(dom_books, api_books):
    """合并 DOM 抓取与网络拦截的书籍，按 id 去重。

    dom_books: list[{id, title, author?}]（页面形式提取）
    api_books: dict[id -> {title, author}]（shelf API 拦截）
    返回 list[{id, title, author}]：DOM 顺序优先，API-only 追加其后。
    title / author 均优先取 API（更干净），缺失再回退 DOM。

    纯数字短 id 等无效 reader book_id 会被丢弃，但其 title/author 仍可按
    书名回填到对应的合法 id（shelf API 常同时返回两套 id）。
    """
    meta_by_title = {}
    for source in (api_books or {}).values():
        title = (source.get("title") or "").strip()
        if not title:
            continue
        entry = meta_by_title.setdefault(title, {"title": title, "author": ""})
        author = (source.get("author") or "").strip()
        if author and not entry["author"]:
            entry["author"] = author
    for b in dom_books or []:
        title = (b.get("title") or "").strip()
        if not title:
            continue
        entry = meta_by_title.setdefault(title, {"title": title, "author": ""})
        author = (b.get("author") or "").strip()
        if author and not entry["author"]:
            entry["author"] = author

    merged = []
    seen = set()
    for b in dom_books or []:
        bid = str(b.get("id") or "").strip()
        if not bid or bid in seen or not is_reader_book_id(bid):
            continue
        seen.add(bid)
        api = (api_books or {}).get(bid, {})
        title = (api.get("title") or b.get("title") or "").strip()
        author = (api.get("author") or b.get("author") or "").strip()
        if title:
            meta = meta_by_title.get(title) or {}
            title = (title or meta.get("title") or "").strip()
            if not author:
                author = (meta.get("author") or "").strip()
        merged.append({"id": bid, "title": title, "author": author})
    for bid, api in (api_books or {}).items():
        bid = str(bid or "").strip()
        if not bid or bid in seen or not is_reader_book_id(bid):
            continue
        seen.add(bid)
        title = (api.get("title") or "").strip()
        author = (api.get("author") or "").strip()
        if title:
            meta = meta_by_title.get(title) or {}
            if not author:
                author = (meta.get("author") or "").strip()
        merged.append({"id": bid, "title": title, "author": author})
    return merged


def books_missing_author(books):
    """返回 author 为空（或缺省）的书籍列表，保持原顺序。"""
    return [b for b in books if not (b.get("author") or "").strip()]


def sanitize_csv_field(value):
    """将字段中的逗号替换为空格，避免破坏逗号分隔格式。"""
    return (value or "").replace(",", " ")


def format_book_line(book):
    """格式化为一行：ID,书名,作者。"""
    return ",".join([
        book.get("id") or "",
        sanitize_csv_field(book.get("title")),
        sanitize_csv_field(book.get("author")),
    ])


def write_shelf_books(path, books):
    """将书籍列表写入 path，一行一条逗号分隔：ID,书名,作者。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for book in books:
            f.write(format_book_line(book) + "\n")


def parse_shelf_line(line):
    """解析一行 ID,书名,作者；字段不足时右侧补空。"""
    parts = (line or "").rstrip("\n\r").split(",", 2)
    while len(parts) < 3:
        parts.append("")
    return {
        "id": parts[0].strip(),
        "title": parts[1].strip(),
        "author": parts[2].strip(),
    }


def load_shelf_books(path):
    """读取 shelf txt，返回 list[{id, title, author}]。文件不存在或失败返回 []。"""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except Exception:
        return []
    books = []
    for line in lines:
        book = parse_shelf_line(line)
        if book["id"]:
            books.append(book)
    return books


def load_existing_authors(path):
    """读取已有 shelf 文件，返回 {id: author}（仅非空 author）。文件不存在则 {}。"""
    out = {}
    for b in load_shelf_books(path):
        bid = b.get("id")
        author = (b.get("author") or "").strip()
        if bid and author:
            out[str(bid)] = author
    return out


def apply_existing_authors(books, existing):
    """列表抓取 author 为空时，回填历史文件中已有的 author（避免重跑冲掉补全结果）。"""
    if not existing:
        return books
    for b in books:
        if not (b.get("author") or "").strip():
            prev = existing.get(b.get("id") or "")
            if prev:
                b["author"] = prev
    return books




# 从当前页面 DOM 提取书籍列表 [{id, title, author}]。
# 尽量兼容多种结构：a[href*="reader"] 链接、[data-book-id] 元素。
EXTRACT_BOOKS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    const push = (id, title, author) => {
        if (!id || seen.has(id)) return;
        // 纯数字 id 无法作为 web/reader 路径打开，跳过
        if (/^\d+$/.test(id)) return;
        seen.add(id);
        out.push({id, title: (title || '').trim(), author: (author || '').trim()});
    };
    document.querySelectorAll('a[href*="reader"]').forEach(a => {
        const m = (a.getAttribute('href') || '').match(/\/reader\/([A-Za-z0-9]+)/);
        if (!m) return;
        const title = a.getAttribute('title')
            || a.querySelector('[class*="title"]')?.textContent
            || a.textContent || '';
        const author = a.querySelector('[class*="author"]')?.textContent || '';
        push(m[1], title, author);
    });
    document.querySelectorAll('[data-book-id]').forEach(el => {
        const id = el.getAttribute('data-book-id');
        if (!id) return;
        const title = el.querySelector('[class*="title"]')?.textContent
            || el.getAttribute('title') || '';
        const author = el.querySelector('[class*="author"]')?.textContent || '';
        push(id, title, author);
    });
    return out;
}
"""


async def extract_dom_books(page):
    """从当前页面 DOM 提取书籍列表 [{id, title, author}]。"""
    return await page.evaluate(EXTRACT_BOOKS_JS)


def _total_unique(dom_books, api_books):
    ids = {b["id"] for b in dom_books if is_reader_book_id(b.get("id"))}
    ids |= {bid for bid in api_books.keys() if is_reader_book_id(bid)}
    return len(ids)


def _author_from_api_payload(data, book_id):
    """从接口 JSON 中按 book_id 取 author；找不到返回空串。"""
    found = {}
    collect_books_from_json(data, found)
    info = found.get(str(book_id)) or {}
    return (info.get("author") or "").strip()


async def fetch_author_from_reader(page, book_id, *, load_wait=None, catalog_wait=None):
    """打开阅读器页补全作者。优先接口字段，其次 DOM（必要时点开目录）。

    返回 author 字符串（可能为空）。
    """
    load_wait = SLEEP_BOOK_DETAIL_LOAD if load_wait is None else load_wait
    catalog_wait = SLEEP_BOOK_DETAIL_CATALOG if catalog_wait is None else catalog_wait
    author_box = {"author": ""}

    async def on_response(response):
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            if "weread.qq.com" not in (response.url or ""):
                return
            if "json" not in (response.headers.get("content-type") or ""):
                return
            data = await response.json()
        except Exception:
            return
        got = _author_from_api_payload(data, book_id)
        if got and not author_box["author"]:
            author_box["author"] = got

    page.on("response", on_response)
    try:
        await page.goto(
            READER_URL_TMPL.format(book_id=book_id),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(load_wait)

        if not author_box["author"]:
            author_box["author"] = (await page.evaluate(EXTRACT_AUTHOR_JS) or "").strip()

        if not author_box["author"]:
            try:
                await page.click("button.readerControls_item.catalog", timeout=5000)
                await asyncio.sleep(catalog_wait)
                author_box["author"] = (
                    await page.evaluate(EXTRACT_AUTHOR_JS) or ""
                ).strip()
            except Exception:
                pass
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    return author_box["author"]


async def enrich_missing_authors(
    page,
    books,
    *,
    out_path=None,
    interval=None,
    load_wait=None,
    catalog_wait=None,
):
    """对 author 为空的书逐本打开详情补全；已有作者的跳过。

    每补全一本（无论成败后的状态）若提供 out_path 则落盘，便于长任务断点续跑观感。
    相邻两本之间 sleep ``interval``（默认 SLEEP_BOOK_DETAIL_INTERVAL=5）。
    返回补全成功本数。
    """
    interval = SLEEP_BOOK_DETAIL_INTERVAL if interval is None else interval
    missing = books_missing_author(books)
    if not missing:
        print("  作者已齐，无需打开详情")
        return 0

    print(f"  需打开详情补全作者: {len(missing)}/{len(books)} 本"
          f"（间隔 {interval}s）")
    filled = 0
    for i, book in enumerate(missing):
        if i > 0:
            await asyncio.sleep(interval)
        bid = book["id"]
        title = book.get("title") or bid
        print(f"  [{i + 1}/{len(missing)}] 打开《{title}》...")
        try:
            author = await fetch_author_from_reader(
                page, bid, load_wait=load_wait, catalog_wait=catalog_wait)
        except Exception as e:
            print(f"    ⚠️  失败: {e}")
            author = ""
        if author:
            book["author"] = author
            filled += 1
            print(f"    -> {author}")
        else:
            print("    -> (未取到作者)")
        if out_path:
            write_shelf_books(out_path, books)
    return filled


async def fetch_shelf(
    *,
    headless=False,
    sleep_seconds=None,
    max_no_new=3,
    out_path=DEFAULT_OUT,
    enrich_author=True,
    author_interval=None,
):
    """抓取书架书籍列表并写入 out_path，返回合并后的书籍列表。"""
    if sleep_seconds is None:
        sleep_seconds = SLEEP_SHELF_SCROLL
    if author_interval is None:
        author_interval = SLEEP_BOOK_DETAIL_INTERVAL

    api_books = {}

    async with async_playwright() as p:
        context = await launch_weread_context(p, headless=headless)

        async def on_response(response):
            try:
                if response.request.resource_type not in ("xhr", "fetch"):
                    return
                if "weread.qq.com" not in (response.url or ""):
                    return
                if "json" not in (response.headers.get("content-type") or ""):
                    return
                data = await response.json()
            except Exception:
                return
            collect_books_from_json(data, api_books)

        # context 级监听：确保 ensure_logged_in 的检测页与主页的 shelf 接口都被捕获，
        # 不会因 handler 注册晚于首次加载而漏掉作者数据。
        context.on("response", on_response)

        if not await ensure_logged_in(context):
            print("  ❌ 登录失败，退出")
            await context.close()
            return []

        page = await context.new_page()
        print("\n  打开书架页...")
        # 用 domcontentloaded + 固定等待，避免书架书籍较多时 networkidle 长时间不空闲而超时；
        # shelf 接口由 context 级 on_response 捕获，不依赖此处等待。
        await page.goto(SHELF_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(SLEEP_SHELF_AFTER_LOAD)

        dom_books = await extract_dom_books(page)
        prev = _total_unique(dom_books, api_books)
        print(f"  首屏: DOM {len(dom_books)} 本 / API {len(api_books)} 本 / 合计 {prev}")

        no_new = 0
        scroll = 0
        while no_new < max_no_new:
            scroll += 1
            # 慢滚动：模拟鼠标滚轮 + 滚动窗口，触发懒加载
            await page.mouse.move(600, 400)
            await page.mouse.wheel(0, 900)
            await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(sleep_seconds)
            dom_books = await extract_dom_books(page)
            cur = _total_unique(dom_books, api_books)
            grew = cur - prev
            print(f"  滚动 #{scroll}: DOM {len(dom_books)} / API {len(api_books)} / "
                  f"合计 {cur}（+{grew}）")
            no_new = 0 if grew > 0 else no_new + 1
            prev = cur

        merged = merge_books(dom_books, api_books)
        apply_existing_authors(merged, load_existing_authors(out_path))
        write_shelf_books(out_path, merged)
        with_author = sum(1 for b in merged if b["author"])
        print(f"\n  列表阶段完成: {len(merged)} 本（有作者 {with_author}/{len(merged)}）"
              f" -> {out_path}")

        if enrich_author:
            await enrich_missing_authors(
                page,
                merged,
                out_path=out_path,
                interval=author_interval,
            )

        await context.close()

    with_author = sum(1 for b in merged if b["author"])
    print(f"\n  ✅ 共 {len(merged)} 本（有作者 {with_author}/{len(merged)}）-> {out_path}")
    return merged


async def enrich_authors_from_file(
    *,
    headless=False,
    out_path=DEFAULT_OUT,
    author_interval=None,
):
    """仅对已有 shelf 文件中 author 为空的书打开详情补全（不重新滚书架）。"""
    if author_interval is None:
        author_interval = SLEEP_BOOK_DETAIL_INTERVAL
    if not os.path.isfile(out_path):
        print(f"  ❌ 找不到 {out_path}，请先完整抓取书架")
        return []
    books = load_shelf_books(out_path)
    if not books:
        print(f"  ❌ {out_path} 为空或无法解析")
        return []

    async with async_playwright() as p:
        context = await launch_weread_context(p, headless=headless)
        if not await ensure_logged_in(context):
            print("  ❌ 登录失败，退出")
            await context.close()
            return books
        page = await context.new_page()
        await enrich_missing_authors(
            page,
            books,
            out_path=out_path,
            interval=author_interval,
        )
        await context.close()

    with_author = sum(1 for b in books if (b.get("author") or "").strip())
    print(f"\n  ✅ 共 {len(books)} 本（有作者 {with_author}/{len(books)}）-> {out_path}")
    return books


def main():
    parser = argparse.ArgumentParser(description="抓取微信读书书架书籍列表")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（需已缓存登录态）")
    parser.add_argument(
        "--sleep", type=float, default=None,
        help=f"每次滚动后暂停秒数（默认 .env SLEEP_SHELF_SCROLL={SLEEP_SHELF_SCROLL})",
    )
    parser.add_argument("--max-no-new", type=int, default=3,
                        help="连续无新书停止阈值（默认 3）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出 txt 路径（一行一条：ID,书名,作者）")
    parser.add_argument(
        "--no-enrich-author", action="store_true",
        help="跳过「作者为空时打开详情补全」步骤",
    )
    parser.add_argument(
        "--enrich-only", action="store_true",
        help="不重新滚书架，仅对已有文件中空作者打开详情补全",
    )
    parser.add_argument(
        "--author-interval", type=float, default=None,
        help=("打开下一本缺作者详情前的间隔秒数"
              f"（默认 .env SLEEP_BOOK_DETAIL_INTERVAL={SLEEP_BOOK_DETAIL_INTERVAL}）"),
    )
    args = parser.parse_args()
    sleep_seconds = args.sleep if args.sleep is not None else SLEEP_SHELF_SCROLL
    author_interval = (
        args.author_interval if args.author_interval is not None
        else SLEEP_BOOK_DETAIL_INTERVAL
    )
    if args.enrich_only:
        asyncio.run(enrich_authors_from_file(
            headless=args.headless,
            out_path=args.out,
            author_interval=author_interval,
        ))
    else:
        asyncio.run(fetch_shelf(
            headless=args.headless,
            sleep_seconds=sleep_seconds,
            max_no_new=args.max_no_new,
            out_path=args.out,
            enrich_author=not args.no_enrich_author,
            author_interval=author_interval,
        ))


if __name__ == "__main__":
    main()
