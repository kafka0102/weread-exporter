#!/usr/bin/env python3
"""书架书去重：shelf_books vs forbid / 本地已下载 / ebook-info / 书架重名。

匹配 ebook-info 与书架内部重名时只比「归一化主书名」，不比作者：
- 去掉括号及括号内内容（半角/全角）
- 去掉横线/冒号后的副标题
- 压缩空白后全等 → 视为重复
- 书架（及 new/dup 已处理列表）中同主书名只保留第一本，其余进 dup

用法：
    python dedupe_shelf_books.py
    python dedupe_shelf_books.py --dry-run
    BOOKS_DIR=~/data/weixin/books python dedupe_shelf_books.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import env_config  # noqa: F401  # 导入即加载 .env
from env_config import BOOKS_DIR

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DEFAULT_SHELF = DATA_DIR / "shelf_books.txt"
DEFAULT_FORBID = DATA_DIR / "forbid_books.txt"
DEFAULT_EBOOK_INFO = DATA_DIR / "ebook-info.json"
DEFAULT_DUP = DATA_DIR / "dup_books.txt"
DEFAULT_NEW = DATA_DIR / "new_books.txt"

# 半角/全角括号内容（非嵌套，循环剥除）
_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
# 副标题分隔：破折号/横线/冒号（取主标题）
_SUBTITLE_RE = re.compile(r"\s*(?:——|—|－|–|-|：|:)\s*")
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str | None) -> str:
    """归一化书名：去括号修饰 + 去副标题，只保留主标题。"""
    s = str(title or "").strip()
    if not s:
        return ""
    # 反复去掉括号段，直到没有
    while True:
        nxt = _PAREN_RE.sub("", s)
        if nxt == s:
            break
        s = nxt
    # 横线/冒号后的副标题去掉，只留主标题
    s = _SUBTITLE_RE.split(s, maxsplit=1)[0]
    s = _WS_RE.sub("", s)  # 去空白，避免「词 品」vs「词品」
    # 统一常见全角标点残留
    s = s.replace("·", "").replace("・", "").replace(".", "")
    return s.strip()


def parse_shelf_line(line: str) -> tuple[str, str, str] | None:
    """解析 `ID,书名,作者`；书名/作者内不应再有逗号。"""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    parts = raw.split(",", 2)
    if len(parts) < 2:
        return None
    book_id = parts[0].strip()
    title = parts[1].strip()
    author = parts[2].strip() if len(parts) > 2 else ""
    if not book_id:
        return None
    return book_id, title, author


def format_shelf_line(book_id: str, title: str, author: str) -> str:
    return f"{book_id},{title},{author}"


def load_shelf_lines(path: Path) -> list[tuple[str, str, str, str]]:
    """返回 [(id, title, author, raw_line), ...]。"""
    if not path.is_file():
        return []
    out: list[tuple[str, str, str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_shelf_line(line)
        if not parsed:
            continue
        book_id, title, author = parsed
        out.append((book_id, title, author, line.rstrip("\n")))
    return out


def load_id_set_from_lines(path: Path) -> set[str]:
    ids: set[str] = set()
    for book_id, _, _, _ in load_shelf_lines(path):
        ids.add(book_id)
    return ids


def load_title_keys_from_lines(path: Path) -> set[str]:
    """从 shelf 格式文件收集归一化主书名（空标题跳过）。"""
    keys: set[str] = set()
    for _book_id, title, _author, _raw in load_shelf_lines(path):
        key = normalize_title(title)
        if key:
            keys.add(key)
    return keys


def load_forbid_ids(path: Path) -> set[str]:
    """每行一个 weread ID；含逗号则取首段；忽略空行与 # 注释。"""
    if not path.is_file():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        ids.add(s.split(",", 1)[0].strip())
    ids.discard("")
    return ids


def load_downloaded_ids(books_dir: Path) -> set[str]:
    """扫描 `{weread_id}_*.json`，取第一个 `_` 前的 id。"""
    if not books_dir.is_dir():
        return set()
    ids: set[str] = set()
    for p in books_dir.glob("*.json"):
        name = p.name
        if "_" not in name:
            continue
        ids.add(name.split("_", 1)[0].strip())
    ids.discard("")
    return ids


def load_ebook_title_keys(path: Path) -> dict[str, list[dict]]:
    """ebook-info.json → {normalize_title(bookName): [entries...]}。"""
    if not path.is_file():
        raise FileNotFoundError(f"缺少电子书库: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"ebook-info 应为 JSON 数组: {path}")
    index: dict[str, list[dict]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        key = normalize_title(item.get("bookName") or "")
        if not key:
            continue
        index.setdefault(key, []).append(item)
    return index


def _append_dup_lines(dup_path: Path, lines: list[str]) -> None:
    """把行追加到 dup（跳过已有 ID）。"""
    if not lines:
        return
    existing_dup = load_id_set_from_lines(dup_path) if dup_path.is_file() else set()
    to_append: list[str] = []
    for line in lines:
        parsed = parse_shelf_line(line)
        if not parsed:
            continue
        if parsed[0] in existing_dup:
            continue
        to_append.append(line.rstrip("\n"))
        existing_dup.add(parsed[0])
    if to_append:
        with dup_path.open("a", encoding="utf-8") as f:
            for line in to_append:
                f.write(line + "\n")


def migrate_stale_from_new(
    new_path: Path,
    dup_path: Path,
    *,
    forbid_ids: set[str],
    downloaded_ids: set[str],
    ebook_keys: dict[str, list[dict]],
    dry_run: bool = False,
) -> list[tuple[str, str]]:
    """把 new 中按当前规则已属 dup 的书迁到 dup。

    含：forbid / 本地已下载 / ebook-info / 与已有 dup 或 new 内先前条目重名。
    返回 [(raw_line, reason), ...]。
    """
    if not new_path.is_file():
        return []
    lines = new_path.read_text(encoding="utf-8").splitlines()
    # 已在 dup 中的书名视为已占用；new 内按出现顺序只保留第一本
    seen_title_keys = load_title_keys_from_lines(dup_path)
    keep: list[str] = []
    moved: list[tuple[str, str]] = []
    for line in lines:
        parsed = parse_shelf_line(line)
        if not parsed:
            keep.append(line.rstrip("\n"))
            continue
        book_id, title, _author = parsed
        bucket, reason = classify_book(
            book_id,
            title,
            forbid_ids=forbid_ids,
            downloaded_ids=downloaded_ids,
            ebook_keys=ebook_keys,
            seen_title_keys=seen_title_keys,
        )
        if bucket == "dup":
            moved.append((line.rstrip("\n"), reason))
        else:
            keep.append(line.rstrip("\n"))
        key = normalize_title(title)
        if key:
            seen_title_keys.add(key)
    if not moved:
        return []

    if dry_run:
        return moved

    new_text = "\n".join(keep)
    if keep:
        new_text += "\n"
    new_path.write_text(new_text, encoding="utf-8")
    _append_dup_lines(dup_path, [line for line, _ in moved])
    return moved


def migrate_downloaded_from_new(
    new_path: Path,
    dup_path: Path,
    downloaded_ids: set[str],
    *,
    dry_run: bool = False,
) -> list[str]:
    """兼容旧接口：仅按本地已下载 ID 迁移 new → dup。"""
    moved = migrate_stale_from_new(
        new_path,
        dup_path,
        forbid_ids=set(),
        downloaded_ids=downloaded_ids,
        ebook_keys={},
        dry_run=dry_run,
    )
    return [line for line, _ in moved]


def classify_book(
    book_id: str,
    title: str,
    *,
    forbid_ids: set[str],
    downloaded_ids: set[str],
    ebook_keys: dict[str, list[dict]],
    seen_title_keys: set[str] | None = None,
) -> tuple[str, str]:
    """返回 (bucket, reason)。bucket 为 'dup' 或 'new'。

    seen_title_keys：已占用的归一化主书名（书架/new/dup 内先前条目）。
    命中时原因 shelf-title-dup；调用方负责在处理后把当前书名加入该集合。
    """
    if book_id in forbid_ids:
        return "dup", "forbid"
    if book_id in downloaded_ids:
        return "dup", "downloaded"
    key = normalize_title(title)
    if key and key in ebook_keys:
        return "dup", "ebook-info"
    if seen_title_keys is not None and key and key in seen_title_keys:
        return "dup", "shelf-title-dup"
    return "new", "new"


def append_lines(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line.rstrip("\n") + "\n")


def run(
    *,
    shelf_path: Path = DEFAULT_SHELF,
    forbid_path: Path = DEFAULT_FORBID,
    ebook_path: Path = DEFAULT_EBOOK_INFO,
    dup_path: Path = DEFAULT_DUP,
    new_path: Path = DEFAULT_NEW,
    books_dir: Path | None = None,
    dry_run: bool = False,
) -> dict:
    if not shelf_path.is_file():
        raise FileNotFoundError(f"缺少书架列表: {shelf_path}")
    if not ebook_path.is_file():
        raise FileNotFoundError(f"缺少电子书库: {ebook_path}")

    books_dir = Path(books_dir) if books_dir is not None else Path(BOOKS_DIR)
    forbid_ids = load_forbid_ids(forbid_path)
    downloaded_ids = load_downloaded_ids(books_dir)
    ebook_keys = load_ebook_title_keys(ebook_path)

    moved_pairs = migrate_stale_from_new(
        new_path,
        dup_path,
        forbid_ids=forbid_ids,
        downloaded_ids=downloaded_ids,
        ebook_keys=ebook_keys,
        dry_run=dry_run,
    )
    moved = [line for line, _ in moved_pairs]
    moved_reasons: dict[str, int] = {}
    for _line, reason in moved_pairs:
        moved_reasons[reason] = moved_reasons.get(reason, 0) + 1

    processed = load_id_set_from_lines(dup_path) | load_id_set_from_lines(new_path)
    # dry-run 时 migrate 未写盘，processed 需把即将迁出的 new 视作已处理
    if dry_run and moved:
        for line in moved:
            parsed = parse_shelf_line(line)
            if parsed:
                processed.add(parsed[0])

    # new/dup 已处理条目的归一化书名均已占用（migrate 只在两者间移动，集合不变）
    seen_title_keys = load_title_keys_from_lines(dup_path) | load_title_keys_from_lines(
        new_path
    )

    shelf = load_shelf_lines(shelf_path)
    todo = [row for row in shelf if row[0] not in processed]

    dup_lines: list[str] = []
    new_lines: list[str] = []
    reasons = {
        "forbid": 0,
        "downloaded": 0,
        "ebook-info": 0,
        "shelf-title-dup": 0,
        "new": 0,
    }

    for book_id, title, author, raw in todo:
        bucket, reason = classify_book(
            book_id,
            title,
            forbid_ids=forbid_ids,
            downloaded_ids=downloaded_ids,
            ebook_keys=ebook_keys,
            seen_title_keys=seen_title_keys,
        )
        reasons[reason] = reasons.get(reason, 0) + 1
        key = normalize_title(title)
        if key:
            seen_title_keys.add(key)
        # 写出原样行
        line = raw if raw.strip() else format_shelf_line(book_id, title, author)
        if bucket == "dup":
            dup_lines.append(line)
        else:
            new_lines.append(line)

    if not dry_run:
        append_lines(dup_path, dup_lines)
        append_lines(new_path, new_lines)

    return {
        "books_dir": str(books_dir),
        "books_dir_exists": books_dir.is_dir(),
        "forbid_count": len(forbid_ids),
        "downloaded_count": len(downloaded_ids),
        "migrated_new_to_dup": len(moved),
        "migrated_reasons": moved_reasons,
        "todo": len(todo),
        "dup": len(dup_lines),
        "new": len(new_lines),
        "reasons": reasons,
        "dry_run": dry_run,
        "dup_samples": dup_lines[:5],
        "new_samples": new_lines[:5],
        "migrated_samples": moved[:5],
    }


def _print_report(result: dict) -> None:
    print(
        f"禁止列表 {result['forbid_count']} 本 | "
        f"本地已下载 {result['downloaded_count']} 本"
        f"{'' if result['books_dir_exists'] else '（目录不存在，已跳过）'}"
    )
    print(f"books_dir: {result['books_dir']}")
    print(f"本轮 new→dup 迁移: {result['migrated_new_to_dup']} 本")
    mr = result.get("migrated_reasons") or {}
    if mr:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(mr.items()))
        print(f"  迁移原因: {detail}")
    if result["migrated_samples"]:
        for s in result["migrated_samples"]:
            print(f"  migrate: {s}")
    print(
        f"本轮 todo {result['todo']} 本 → "
        f"dup {result['dup']} / new {result['new']}"
    )
    r = result["reasons"]
    print(
        "  其中: "
        f"forbid={r.get('forbid', 0)}, "
        f"downloaded={r.get('downloaded', 0)}, "
        f"ebook-info={r.get('ebook-info', 0)}, "
        f"shelf-title-dup={r.get('shelf-title-dup', 0)}, "
        f"new={r.get('new', 0)}"
    )
    if result["dry_run"]:
        print("（dry-run，未写文件）")
    if result["dup_samples"]:
        print("dup 样例:")
        for s in result["dup_samples"]:
            print(f"  {s}")
    if result["new_samples"]:
        print("new 样例:")
        for s in result["new_samples"]:
            print(f"  {s}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="书架书去重（标题归一化匹配）")
    parser.add_argument("--shelf", type=Path, default=DEFAULT_SHELF)
    parser.add_argument("--forbid", type=Path, default=DEFAULT_FORBID)
    parser.add_argument("--ebook-info", type=Path, default=DEFAULT_EBOOK_INFO)
    parser.add_argument("--dup", type=Path, default=DEFAULT_DUP)
    parser.add_argument("--new", type=Path, default=DEFAULT_NEW)
    parser.add_argument(
        "--books-dir",
        type=Path,
        default=None,
        help=f"本地已导出目录（默认 {BOOKS_DIR}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只汇报，不写 dup/new",
    )
    args = parser.parse_args(argv)
    try:
        result = run(
            shelf_path=args.shelf,
            forbid_path=args.forbid,
            ebook_path=args.ebook_info,
            dup_path=args.dup,
            new_path=args.new,
            books_dir=args.books_dir,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
