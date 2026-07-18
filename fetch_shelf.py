#!/usr/bin/env python3
"""微信读书 - 书架书籍列表抓取。

复用 weread_session 的缓存登录会话打开书架页，慢滚动触发懒加载，
DOM 抓取书籍 id/title，同时通过 context 级网络拦截捕获书架接口响应
取 author（页面禁用 F12 不影响 Playwright 的 CDP 级监听），按 book_id
合并后写入 data/shelf_books.json。

用法：
    python fetch_shelf.py                 # 可见浏览器，sleep 3s
    python fetch_shelf.py --headless      # 无头（需已缓存登录态）
    python fetch_shelf.py --sleep 5 --max-no-new 4
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re

from playwright.async_api import async_playwright

from weread_session import SHELF_URL, ensure_logged_in, launch_weread_context

DATA_DIR = "data"
DEFAULT_OUT = os.path.join(DATA_DIR, "shelf_books.json")

_READER_ID_RE = re.compile(r"/reader/([A-Za-z0-9]+)")


def extract_book_id_from_href(href):
    """从链接 href 中提取 book_id（reader URL 末段）。无匹配返回空串。"""
    if not href:
        return ""
    m = _READER_ID_RE.search(href)
    return m.group(1) if m else ""


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
    """
    merged = []
    seen = set()
    for b in dom_books:
        bid = b.get("id")
        if not bid or bid in seen:
            continue
        seen.add(bid)
        api = api_books.get(bid, {})
        title = (api.get("title") or b.get("title") or "").strip()
        author = (api.get("author") or b.get("author") or "").strip()
        merged.append({"id": bid, "title": title, "author": author})
    for bid, api in api_books.items():
        if bid in seen:
            continue
        seen.add(bid)
        merged.append({"id": bid,
                       "title": (api.get("title") or "").strip(),
                       "author": (api.get("author") or "").strip()})
    return merged


# 从当前页面 DOM 提取书籍列表 [{id, title, author}]。
# 尽量兼容多种结构：a[href*="reader"] 链接、[data-book-id] 元素。
EXTRACT_BOOKS_JS = r"""
() => {
    const out = [];
    const seen = new Set();
    const push = (id, title, author) => {
        if (!id || seen.has(id)) return;
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
    ids = {b["id"] for b in dom_books if b.get("id")}
    ids |= set(api_books.keys())
    return len(ids)


async def fetch_shelf(*, headless=False, sleep_seconds=3.0, max_no_new=3,
                      out_path=DEFAULT_OUT):
    """抓取书架书籍列表并写入 out_path，返回合并后的书籍列表。"""
    api_books = {}

    async with async_playwright() as p:
        context = await launch_weread_context(p, headless=headless)

        async def on_response(response):
            try:
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
        await page.goto(SHELF_URL, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

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

        await context.close()

    merged = merge_books(dom_books, api_books)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    with_author = sum(1 for b in merged if b["author"])
    print(f"\n  ✅ 共 {len(merged)} 本（有作者 {with_author}/{len(merged)}）-> {out_path}")
    return merged


def main():
    parser = argparse.ArgumentParser(description="抓取微信读书书架书籍列表")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（需已缓存登录态）")
    parser.add_argument("--sleep", type=float, default=3.0,
                        help="每次滚动后暂停秒数（默认 3）")
    parser.add_argument("--max-no-new", type=int, default=3,
                        help="连续无新书停止阈值（默认 3）")
    parser.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    args = parser.parse_args()
    asyncio.run(fetch_shelf(headless=args.headless, sleep_seconds=args.sleep,
                            max_no_new=args.max_no_new, out_path=args.out))


if __name__ == "__main__":
    main()
