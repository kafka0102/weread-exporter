#!/usr/bin/env python3
"""微信读书已下载电子书 —— 空叶子章节体检与增量修复。

背景：批量导出时个别叶子章节会抓到空正文（例如顶栏越过短诗/词牌章），
整本的正文却正常。这类问题不需要整本重下，只需补抓对应章节。

本脚本：

1. 来源确认：只处理微信读书来源的电子书。判定依据（见
   docs/reports/merged-chapters-2026-09-13.md）：
   - 该书 ID 出现在书架缓存 ``data/shelf_books.txt``；
   - 或存在 ``output/<book_id>/_catalog.json``。
   明确排除非微信来源：JSON 顶层带 ``source_url``（十点古籍 / 搜韵 / ctext）、
   带 ``kind``（搜韵诗话/词话）、或 ID 不是合法 reader ID（如
   ``pdwiki_qiezhong``）且无书架/目录证据。

2. 空叶子章检测：导出 book JSON 的 ``body`` 中，``content`` 为空、且
   **不是最后一章** 的叶子章节视为抓取失败；书前/书末无正文项
   （版权信息、封面、扉页、目录、索引等）默认豁免（``--strict`` 可纳入）。
   有 ``_catalog.json`` 时按目录叶子对齐：目录里有、导出章里没有的项只统计为
   ``missing`` 参考，不作为修复目标（补插章节会改变章节编号结构，超出本脚本
   “只更新内容”的范围，此类书应整本重下）。

3. 两种模式：
   - ``--dry-run``（默认）：只列出有问题书的 ID/书名与空章，不改任何文件。
   - ``--apply``：打开微信读书阅读器，只补抓空叶子章，**仅更新** books JSON
     中对应章的 ``content`` / ``has_content`` 与全书 ``word_count``，
     **不修改 chapter_id、不插入/删除章节**；同时同步写
     ``output/<book_id>/chapters/`` 中间产物。

用法：
    python repair_empty_chapters.py --dry-run
    python repair_empty_chapters.py --dry-run --book <book_id>
    python repair_empty_chapters.py --dry-run --json
    python repair_empty_chapters.py --tsv .scratch/empty-chapters.tsv
    python repair_empty_chapters.py --apply --headless --no-prefer-largest-screen --book <book_id>
    python repair_empty_chapters.py --refresh-shelf --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from playwright.async_api import async_playwright

import env_config  # noqa: F401  # 导入即加载 .env
from book_json import chapter_sleep_seconds, md_to_plain_content
from env_config import (
    BOOKS_DIR,
    SLEEP_BOOK_INTERVAL,
    SLEEP_READER_AFTER_HOOK,
    SLEEP_READER_AFTER_LOAD,
    SLEEP_READER_PAGE_RENDER,
    SLEEP_READER_PAGE_TURN,
)
from export_precise import (
    CANVAS_HOOK,
    CANVAS_RECTS_JS,
    VIEWPORT_IMGS_JS,
    blocks_are_catalog_page,
    build_page_blocks,
    catalog_index,
    clean_catalog_titles,
    ensure_configured_viewport,
    ensure_horizontal_paging_mode,
    ensure_reader_layout,
    fetch_book_title,
    focus_reader_for_keyboard,
    goto_catalog_chapter,
    is_non_content_catalog_title,
    next_catalog_title,
    normalize_catalog_title,
    page_blocks_fingerprint,
    read_chapter_title,
    read_reader_progress_percent,
    reader_viewport,
    recover_reader_text_after_nav,
    save_chapter,
    should_skip_chapter_line,
    split_blocks_at_chapter_start,
    turn_reader_page,
    wait_stable,
)
from fetch_shelf import is_reader_book_id
from weread_session import (
    ensure_logged_in,
    ensure_browser_window_size,
    launch_weread_context,
    resolve_headless,
)

DEFAULT_SHELF = Path("data") / "shelf_books.txt"
DEFAULT_CATALOG_ROOT = Path("output")
READER_URL_TMPL = "https://weread.qq.com/web/reader/{book_id}"


# ---------------------------------------------------------------------------
# 纯函数：来源判定
# ---------------------------------------------------------------------------

def load_shelf_ids(shelf_path) -> set[str]:
    """读取书架缓存 `ID,书名,作者` 行，返回 ID 集合；忽略空行与 # 注释。"""
    ids: set[str] = set()
    path = Path(shelf_path)
    if not path.is_file():
        return ids
    for line in path.read_text(encoding="utf-8").splitlines():
        text = (line or "").strip()
        if not text or text.startswith("#"):
            continue
        bid = text.split(",", 1)[0].strip()
        if bid:
            ids.add(bid)
    return ids


def has_catalog(catalog_root, book_id: str) -> bool:
    return (Path(catalog_root) / book_id / "_catalog.json").is_file()


def classify_weread_source(
    book: dict,
    book_id: str,
    shelf_ids: set[str],
    catalog_root,
) -> tuple[bool, str]:
    """判断 book JSON 是否微信读书来源，返回 (是否, 依据/排除原因)。"""
    if book.get("source_url"):
        return False, "source_url"
    if book.get("kind"):
        return False, "kind"
    if book_id in shelf_ids:
        return True, "shelf"
    if has_catalog(catalog_root, book_id):
        return True, "catalog"
    if not is_reader_book_id(book_id):
        return False, "invalid-id"
    return False, "no-evidence"


# ---------------------------------------------------------------------------
# 纯函数：空叶子章检测
# ---------------------------------------------------------------------------

def normalize_title_key(text: str) -> str:
    """归一章名：去空白与零宽字符。"""
    return normalize_catalog_title(text or "")


def leaf_title_matches(catalog_title: str, body_name: str) -> bool:
    """目录叶子标题与导出章名是否同一章。

    全等即可；非全等时要求两侧都超过 4 字（防「春」「云」这类短名前缀误命中
    正文），且只允许前后缀差 ≤ 6 的包含关系。
    """
    a = normalize_title_key(catalog_title)
    b = normalize_title_key(body_name)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) <= 4 or len(b) <= 4:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(long) - len(short) > 6:
        return False
    return long.startswith(short) or long.endswith(short)


def load_catalog_titles(catalog_path) -> list[str]:
    """读取 _catalog.json 的标题列表；读不到返回 []。"""
    path = Path(catalog_path) if catalog_path else None
    if not path or not path.is_file():
        return []
    try:
        titles = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return clean_catalog_titles(titles if isinstance(titles, list) else [])


def find_empty_leaf_chapters(
    body: list[dict],
    *,
    catalog_titles: Iterable[str] | None = None,
    min_chapters: int = 3,
    strict: bool = False,
) -> dict:
    """在导出 body 中找「内容为空的叶子章节」（最后一章豁免）。

    返回:
      issues: [{index, chapter_id, chapter_name, catalog_title}]
      missing: 目录里有、body 没有对应章的项（仅参考）
      skipped: 整书未检测的原因（None 表示已检测）
    """
    rows: list[dict] = []
    for i, ch in enumerate(body or [], start=1):
        if not isinstance(ch, dict):
            ch = {}
        rows.append(
            {
                "index": i,
                "chapter_id": str(ch.get("chapter_id") or ""),
                "chapter_name": str(ch.get("chapter_name") or ""),
                "content": str(ch.get("content") or ""),
            }
        )
    if len(rows) < min_chapters:
        return {"issues": [], "missing": [], "skipped": "too-few-chapters"}

    cat_list = list(catalog_titles or [])
    issues: list[dict] = []
    for row in rows:
        if row["index"] == len(rows):
            continue  # 最后一章豁免
        if row["content"].strip():
            continue
        if not strict and is_non_content_catalog_title(row["chapter_name"]):
            continue  # 版权/封面/扉页/目录等本就无正文
        matched_cat = ""
        if cat_list:
            for ci, cat in enumerate(cat_list):
                if ci == len(cat_list) - 1:
                    continue  # 目录末项豁免
                if leaf_title_matches(cat, row["chapter_name"]):
                    matched_cat = cat
                    break
            if not matched_cat:
                # 有目录但该空章不在目录叶子上：不是可修复的叶子，跳过
                continue
        issues.append(
            {
                "index": row["index"],
                "chapter_id": row["chapter_id"],
                "chapter_name": row["chapter_name"],
                "catalog_title": matched_cat,
            }
        )

    missing: list[str] = []
    if cat_list:
        body_keys = [normalize_title_key(r["chapter_name"]) for r in rows]
        seen_missing: set[str] = set()
        for ci, cat in enumerate(cat_list):
            if ci == len(cat_list) - 1:
                continue
            if is_non_content_catalog_title(cat):
                continue
            key = normalize_title_key(cat)
            if not key or key in seen_missing:
                continue
            hit = any(
                key == bk
                or (
                    len(key) > 4
                    and len(bk) > 4
                    and (key in bk or bk in key)
                    and abs(len(key) - len(bk)) <= 6
                )
                for bk in body_keys
            )
            if not hit:
                seen_missing.add(key)
                missing.append(cat)
    return {"issues": issues, "missing": missing, "skipped": None}


def scan_book_json(
    book: dict,
    *,
    catalog_root=None,
    min_chapters: int = 3,
    strict: bool = False,
) -> dict:
    """扫描单本 book JSON，返回 {book_id, title, author, result}。"""
    body = book.get("body")
    if not isinstance(body, list):
        return {
            "book_id": str(book.get("id") or ""),
            "title": str(book.get("title") or ""),
            "author": str(book.get("author") or ""),
            "result": {"issues": [], "missing": [], "skipped": "no-body"},
        }
    catalog_titles = None
    if catalog_root is not None:
        catalog_path = Path(catalog_root) / str(book.get("id") or "") / "_catalog.json"
        catalog_titles = load_catalog_titles(catalog_path) or None
    result = find_empty_leaf_chapters(
        body,
        catalog_titles=catalog_titles,
        min_chapters=min_chapters,
        strict=strict,
    )
    return {
        "book_id": str(book.get("id") or ""),
        "title": str(book.get("title") or ""),
        "author": str(book.get("author") or ""),
        "result": result,
    }


def iter_book_files(books_dir) -> Iterable[Path]:
    root = Path(books_dir)
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        if path.is_file():
            yield path


def load_book_file(path: Path) -> dict | None:
    try:
        book = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return book if isinstance(book, dict) else None


def find_book_path(books_dir, book_id: str) -> Path | None:
    root = Path(books_dir)
    if not root.is_dir():
        return None
    prefix = f"{book_id}_"
    for path in root.iterdir():
        if path.is_file() and path.name.startswith(prefix) and path.suffix.lower() == ".json":
            return path
    return None


# ---------------------------------------------------------------------------
# 纯函数：JSON 内容更新（只更新 content，不动 chapter_id）
# ---------------------------------------------------------------------------

def update_book_json_content(book: dict, updated: dict[int, str]) -> dict:
    """把 body 指定序号（1-based）的正文替换为 updated 内容。

    - 只改 content / has_content；
    - chapter_id、chapter_name、章节顺序、其它章逐字段保持不变；
    - 全书 word_count 按新正文重算。
    """
    out = copy.deepcopy(book)
    body = out.get("body")
    if not isinstance(body, list):
        raise ValueError("book JSON 缺少 body 列表")
    word_count = 0
    for i, ch in enumerate(body, start=1):
        if not isinstance(ch, dict):
            continue
        if i in updated:
            ch["content"] = str(updated[i])
        ch["content"] = str(ch.get("content") or "")
        ch["has_content"] = bool(ch["content"].strip())
        word_count += len(ch["content"])
    out["word_count"] = word_count
    return out


def write_json_atomic(path: Path, data: dict) -> None:
    """原子写 JSON：先写临时文件再 os.replace。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def backup_book_json(path: Path, backup_dir) -> Path:
    """把原 book JSON 复制到备份目录，返回备份路径。"""
    root = Path(backup_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = root / f"{path.stem}.{stamp}.bak.json"
    backup.write_bytes(path.read_bytes())
    return backup


# ---------------------------------------------------------------------------
# 浏览器：单章补抓
# ---------------------------------------------------------------------------

async def capture_one_chapter(
    page,
    *,
    catalog_titles: list[str],
    target_title: str,
    target_idx: int,
    seen_imgs: set[str],
    max_turns: int = 3000,
) -> list[dict]:
    """从目标章当前位置逐页抓取正文，到下一章边界/章尾停止。

    返回该章的 blocks（含文本与图片引用）。复用 export_precise 的
    页面块构建、目录页跳过与页级/行级去重逻辑。
    """
    nxt = next_catalog_title(catalog_titles, target_title) if catalog_titles else ""
    blocks: list[dict] = []
    last_page_fp = ""
    last_page_lines: set[str] = set()
    seen_page_fps: set[str] = set()
    chapter_seen_lines: set[str] = set()
    page_cycle_hits = 0
    blank_streak = 0
    turns = 0
    target_cat_index = catalog_index(catalog_titles, target_title) if catalog_titles else None

    def append_page(new_blocks: list[dict]) -> int:
        nonlocal last_page_fp, last_page_lines, seen_page_fps, chapter_seen_lines, page_cycle_hits
        page_fp = page_blocks_fingerprint(new_blocks)
        if page_fp and page_fp == last_page_fp:
            return 0
        if page_fp and page_fp in seen_page_fps:
            page_cycle_hits += 1
            last_page_fp = page_fp
            return 0
        page_line_set: set[str] = set()
        added = 0
        for b in new_blocks:
            if b.get("type") == "text":
                t = (b.get("text") or "").strip()
                if not t:
                    continue
                page_line_set.add(t)
                if t in last_page_lines:
                    continue
                if should_skip_chapter_line(t, chapter_seen_lines):
                    continue
                if blocks and blocks[-1].get("type") == "text" and (blocks[-1].get("text") or "").strip() == t:
                    continue
                blocks.append({"type": "text", "text": t})
                chapter_seen_lines.add(t)
                added += 1
            else:
                blocks.append(b)
                added += 1
        if page_fp:
            last_page_fp = page_fp
            last_page_lines = page_line_set
            if added > 0:
                seen_page_fps.add(page_fp)
                page_cycle_hits = 0
            elif page_line_set and page_fp in seen_page_fps:
                page_cycle_hits += 1
            elif page_line_set:
                seen_page_fps.add(page_fp)
        return added

    while turns < max_turns:
        turns += 1
        await asyncio.sleep(SLEEP_READER_PAGE_RENDER)
        rects = []
        imgs = []
        chars = 0
        try:
            chars = await page.evaluate("() => window.__wr_chars")
            rects = await page.evaluate(CANVAS_RECTS_JS)
            imgs = await page.evaluate(VIEWPORT_IMGS_JS)
        except Exception:
            blank_streak += 1
        new_blocks = build_page_blocks(chars, imgs, rects, seen_imgs) if chars else []

        if not new_blocks:
            blank_streak += 1
        else:
            if catalog_titles and blocks_are_catalog_page(new_blocks, catalog_titles):
                last_page_fp = page_blocks_fingerprint(new_blocks)
                last_page_lines = set()
                blank_streak = 0
            else:
                if nxt:
                    before, after = split_blocks_at_chapter_start(new_blocks, nxt)
                    if after:
                        append_page(before)
                        break
                added = append_page(new_blocks)
                blank_streak = 0 if added else blank_streak
            # 顶栏已前进到后续章：停止（双页顶栏偶尔提前显示下一章，做容错判断）
            try:
                header = await read_chapter_title(page, catalog_titles) if catalog_titles else ""
            except Exception:
                header = ""
            if header and target_cat_index is not None:
                h_idx = catalog_index(catalog_titles, header)
                if h_idx is not None and h_idx > target_cat_index and h_idx - target_cat_index <= 2:
                    break

        # 翻页
        turned = False
        for method in ("arrow", "arrow2", "click_midright"):
            try:
                await turn_reader_page(page, method=method)
                turned = True
                await asyncio.sleep(SLEEP_READER_PAGE_TURN)
                if await wait_stable(page, timeout=6) > 0:
                    break
            except Exception:
                continue
        if not turned:
            blank_streak += 1
        if turns > 3:
            try:
                pct = await read_reader_progress_percent(page)
            except Exception:
                pct = None
            if pct is not None and pct >= 99 and blank_streak >= 3:
                break
    return blocks


async def repair_one_book(
    book_id: str,
    *,
    issue: dict,
    books_dir,
    catalog_root,
    out_dir,
    headless: bool,
    prefer_largest: bool,
    max_chapters: int | None,
) -> dict:
    """补抓一本书的空叶子章并更新 books JSON；返回修复统计。"""
    book_path = find_book_path(books_dir, book_id)
    if book_path is None:
        return {"book_id": book_id, "status": "skipped", "detail": "book json not found"}
    book = load_book_file(book_path)
    if book is None:
        return {"book_id": book_id, "status": "skipped", "detail": "invalid book json"}

    targets = issue["result"]["issues"]
    if max_chapters:
        targets = targets[: int(max_chapters)]
    if not targets:
        return {"book_id": book_id, "status": "skipped", "detail": "no targets"}

    out_dir = Path(out_dir)
    book_dir = out_dir / book_id
    md_dir = book_dir / "chapters"
    raw_dir = book_dir / "raw"
    md_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = book_dir / "_catalog.json"
    catalog_titles = load_catalog_titles(catalog_path)
    repaired: dict[int, str] = {}
    seen_imgs: set[str] = set()
    for jf in raw_dir.iterdir():
        if jf.suffix == ".json":
            try:
                for img in json.loads(jf.read_text(encoding="utf-8")).get("images", []):
                    seen_imgs.add(img["url"])
            except Exception:
                pass

    viewport = reader_viewport(None, None, prefer_largest=prefer_largest)
    async with async_playwright() as p:
        ctx = await launch_weread_context(p, headless=headless, viewport=viewport)
        try:
            if not await ensure_logged_in(ctx, allow_interactive_login=not headless):
                return {
                    "book_id": book_id,
                    "status": "skipped",
                    "detail": "未登录/登录态失效（无头模式无法交互扫码）",
                }
            page = await ctx.new_page()
            await page.add_init_script(CANVAS_HOOK)
            try:
                await ensure_browser_window_size(page, viewport, prefer_largest=prefer_largest)
            except Exception:
                pass
            await page.goto(
                READER_URL_TMPL.format(book_id=book_id),
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await asyncio.sleep(SLEEP_READER_AFTER_LOAD)
            viewport = await ensure_configured_viewport(page, viewport)
            viewport = await ensure_reader_layout(page, viewport)
            await ensure_horizontal_paging_mode(page)
            book_title, book_author = await fetch_book_title(page)

            if not catalog_titles:
                catalog_titles = clean_catalog_titles(
                    await scrape_catalog(page)
                )
                catalog_path.parent.mkdir(parents=True, exist_ok=True)
                catalog_path.write_text(
                    json.dumps(catalog_titles, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            await focus_reader_for_keyboard(page)
            await asyncio.sleep(SLEEP_READER_AFTER_HOOK)

            for t in targets:
                title = t["catalog_title"] or t["chapter_name"]
                if not title:
                    continue
                landed = await goto_catalog_chapter(page, title, catalog_titles)
                if not landed:
                    continue  # 目录跳转失败：跳过该章，不误写空内容
                await recover_reader_text_after_nav(page)
                await focus_reader_for_keyboard(page)
                blocks = await capture_one_chapter(
                    page,
                    catalog_titles=catalog_titles,
                    target_title=landed or title,
                    target_idx=t["index"],
                    seen_imgs=seen_imgs,
                )
                text_len, img_records = save_chapter(
                    title, blocks, int(t["index"]), md_dir, raw_dir
                )
                if text_len or img_records:
                    content = ""
                    md_path = md_dir / f"{int(t['index']):04d}.md"
                    if md_path.is_file():
                        body_md = md_path.read_text(encoding="utf-8")
                        content = md_to_plain_content(body_md, chapter_name=title)
                    repaired[int(t["index"])] = content
                await asyncio.sleep(
                    chapter_sleep_seconds(text_len)
                )
        finally:
            try:
                await ctx.close()
            except Exception:
                pass

    if not repaired:
        return {"book_id": book_id, "status": "no-op", "detail": "无成功补抓的章节"}

    updated = update_book_json_content(book, repaired)
    backup_dir = out_dir / book_id / "backups"
    backup = backup_book_json(book_path, backup_dir)
    write_json_atomic(book_path, updated)
    return {
        "book_id": book_id,
        "status": "repaired",
        "detail": f"修复 {len(repaired)} 章；原文件备份 {backup}",
        "repaired": sorted(repaired),
    }


async def scrape_catalog(page) -> list[str]:
    """打开目录抓取标题列表（复用 export_precise 的目录滚动逻辑）。"""
    from export_precise import open_reader_catalog, scrape_catalog_titles

    if not await open_reader_catalog(page):
        return []
    try:
        return await scrape_catalog_titles(page)
    finally:
        try:
            from export_precise import close_reader_catalog

            await close_reader_catalog(page)
        except Exception:
            pass


async def run_repairs(
    issues: list[dict],
    *,
    books_dir,
    catalog_root,
    out_dir,
    headless: bool,
    prefer_largest: bool,
    limit: int | None,
    max_chapters: int | None,
    fail_fast: bool,
) -> list[dict]:
    """按清单逐本修复；默认单本失败继续，--fail-fast 改为失败即停。"""
    results: list[dict] = []
    pending = issues if not limit else issues[: int(limit)]
    for i, issue in enumerate(pending):
        print(f"\n##### 修复 {i + 1}/{len(pending)}  {issue['book_id']}  {issue['title']}")
        try:
            res = await repair_one_book(
                issue["book_id"],
                issue=issue,
                books_dir=books_dir,
                catalog_root=catalog_root,
                out_dir=out_dir,
                headless=headless,
                prefer_largest=prefer_largest,
                max_chapters=max_chapters,
            )
        except Exception as e:
            if fail_fast:
                raise
            res = {
                "book_id": issue["book_id"],
                "status": "failed",
                "detail": str(e),
            }
        results.append(res)
        print(f"  → {res['status']}: {res.get('detail', '')}")
        if i < len(pending) - 1:
            print(f"  书间等待 {SLEEP_BOOK_INTERVAL:.0f}s ...")
            await asyncio.sleep(SLEEP_BOOK_INTERVAL)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="微信读书已下载电子书空叶子章节体检与增量修复",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="空跑模式（默认）：只列出问题书与空章，不改任何文件",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="实际运行：打开阅读器补抓空章并更新 books JSON（默认 dry-run 只列出）",
    )
    parser.add_argument(
        "--book",
        dest="book_ids",
        action="append",
        default=[],
        help="只处理指定 book_id（可重复）；省略则遍历全部",
    )
    parser.add_argument(
        "--books-dir",
        default=str(BOOKS_DIR),
        help=f"已下载 books JSON 目录（默认 {BOOKS_DIR}）",
    )
    parser.add_argument(
        "--shelf",
        default=str(DEFAULT_SHELF),
        help=f"书架缓存清单（默认 {DEFAULT_SHELF}）",
    )
    parser.add_argument(
        "--catalog-root",
        default=str(DEFAULT_CATALOG_ROOT),
        help="导出中间产物目录，用于读 _catalog.json 与来源判定（默认 output）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_CATALOG_ROOT),
        help="apply 模式写 output/<book_id> 中间产物的根目录（默认 output）",
    )
    parser.add_argument(
        "--min-chapters",
        type=int,
        default=3,
        help="章节数下限（默认 3；≤2 章的书豁免）",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="把版权/封面/扉页/目录等书前书末无正文项也算作空章问题",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="apply 模式最多处理 N 本（按扫描出的问题顺序取前 N）",
    )
    parser.add_argument(
        "--max-chapters",
        type=int,
        default=None,
        help="每本最多补抓 N 个空章",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="apply 模式单本失败即中止（默认记日志继续）",
    )
    parser.add_argument(
        "--refresh-shelf",
        action="store_true",
        help="先运行 fetch_shelf.py 刷新书架缓存再扫描",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（需 cache/browser_profile 已有登录态；否则回退有头）",
    )
    parser.add_argument(
        "--prefer-largest-screen",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="apply 模式目标屏幕：--prefer-largest-screen / --no-prefer-largest-screen（必填）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="dry-run 结果以 JSON 输出到 stdout",
    )
    parser.add_argument(
        "--tsv",
        default=None,
        help="dry-run 空章明细写出到该 TSV 路径",
    )
    args = parser.parse_args(argv)
    if args.apply and args.prefer_largest_screen is None:
        parser.error("--apply 必须指定 --prefer-largest-screen 或 --no-prefer-largest-screen")
    return args


def refresh_shelf(headless: bool) -> None:
    """刷新书架缓存 data/shelf_books.txt；失败抛错中止。"""
    cmd = [sys.executable, "fetch_shelf.py"]
    if headless:
        cmd.append("--headless")
    print("  ⟳ 更新书架缓存:", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"fetch_shelf.py 失败（exit={proc.returncode}）：\n{proc.stderr[-2000:]}"
        )
    print("  ✅ 书架缓存已更新")


def scan_all(
    *,
    books_dir,
    shelf_path,
    catalog_root,
    book_ids: list[str],
    min_chapters: int,
    strict: bool,
) -> dict:
    shelf_ids = load_shelf_ids(shelf_path)
    wanted = set(book_ids)
    findings: list[dict] = []
    stats = {"scanned": 0, "weread": 0, "non_weread": 0, "excluded": 0}
    for path in iter_book_files(books_dir):
        book = load_book_file(path)
        if book is None:
            continue
        bid = str(book.get("id") or path.name.split("_", 1)[0])
        stats["scanned"] += 1
        if wanted and bid not in wanted:
            stats["excluded"] += 1
            continue
        ok, reason = classify_weread_source(book, bid, shelf_ids, catalog_root)
        if not ok:
            stats["non_weread"] += 1
            continue
        stats["weread"] += 1
        item = scan_book_json(
            book,
            catalog_root=catalog_root,
            min_chapters=min_chapters,
            strict=strict,
        )
        if item["result"]["issues"]:
            findings.append(item)
    findings.sort(key=lambda x: -len(x["result"]["issues"]))
    return {
        "stats": stats,
        "findings": findings,
        "total_issues": sum(len(f["result"]["issues"]) for f in findings),
        "missing": sum(len(f["result"]["missing"]) for f in findings),
    }


def format_issue_list(item: dict) -> str:
    issues = item["result"]["issues"]
    names = "；".join(
        f"#{i['index']} {i['chapter_name'] or i['chapter_id']}" for i in issues[:8]
    )
    if len(issues) > 8:
        names += f" …共 {len(issues)} 章"
    return f"{item['book_id']}  {item['title']}（{item['author']}） 空章 {len(issues)}：{names}"


def render_report(report: dict, *, strict: bool, min_chapters: int) -> str:
    st = report["stats"]
    lines = [
        "=" * 64,
        "微信读书空叶子章节体检（dry-run）",
        "=" * 64,
        f"扫描 books: {st['scanned']} 本 | 微信来源: {st['weread']} 本 | "
        f"非微信来源: {st['non_weread']} 本 | 未匹配: {st['excluded']} 本",
        f"口径: 内容为空且非末章；书前/书末项{'纳入' if strict else '豁免'}；"
        f"min_chapters={min_chapters}",
        f"命中: {len(report['findings'])} 本 / 空章 {report['total_issues']} 个"
        f"（另有目录缺失项 {report['missing']} 个，仅参考）",
    ]
    for item in report["findings"]:
        lines.append(format_issue_list(item))
    return "\n".join(lines)


def write_tsv(report: dict, path: str) -> None:
    rows = [
        ("book_id", "title", "author", "chapter_index", "chapter_name", "chapter_id")
    ]
    for item in report["findings"]:
        for i in item["result"]["issues"]:
            rows.append(
                (
                    item["book_id"],
                    item["title"],
                    item["author"],
                    str(i["index"]),
                    i["chapter_name"],
                    i["chapter_id"],
                )
            )
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write("\t".join(row) + "\n")


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.refresh_shelf:
            refresh_shelf(args.headless)
        report = scan_all(
            books_dir=args.books_dir,
            shelf_path=args.shelf,
            catalog_root=args.catalog_root,
            book_ids=args.book_ids,
            min_chapters=args.min_chapters,
            strict=args.strict,
        )
    except Exception as e:
        print(f"❌ {e}")
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_report(report, strict=args.strict, min_chapters=args.min_chapters))
    if args.tsv:
        write_tsv(report, args.tsv)
        print(f"\n📄 空章明细已写: {args.tsv}")

    if args.apply:
        if not report["findings"]:
            print("\n没有需要修复的空章。")
            return 0
        headless = resolve_headless(args.headless)
        results = asyncio.run(
            run_repairs(
                report["findings"],
                books_dir=args.books_dir,
                catalog_root=args.catalog_root,
                out_dir=args.out_dir,
                headless=headless,
                prefer_largest=bool(args.prefer_largest_screen),
                limit=args.limit,
                max_chapters=args.max_chapters,
                fail_fast=args.fail_fast,
            )
        )
        ok = sum(1 for r in results if r["status"] == "repaired")
        print(f"\n修复完成: {ok}/{len(results)} 本成功")
        for r in results:
            print(f"  {r['book_id']}  {r['status']}: {r.get('detail', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
