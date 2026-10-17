#!/usr/bin/env python3
"""书架书去重：shelf_books vs forbid / 本地已下载 / ebook-info / 书架重名。

判定「重复」的口径是**书名 + 作者都对得上**：
- 去掉装饰性括号（珍藏本、全二册、丛书名等）
- 保留期刊/学刊期数括号（第N期、第N辑、第N卷、第N本第N期、年份+期数等）
- 去掉横线/冒号后的副标题
- 压缩空白与期数标点后全等 = 书名相同
- 作者按人名 token 比对（去朝代标记与「著/编/选注/主编」等角色词，至少一人相同）
- **书名 + 作者都对得上**才算重复；同名异作者（不同选注者/不同版本）当新书
- 任一侧拿不到作者 token 时退回「按书名判重」
- 书架（及 new/dup 已处理列表）中同书名同作者只保留第一本，其余进 dup

判定优先级：

1. forbid（禁止下载）
2. 本地已下载（`BOOKS_DIR` 里已有 `{id}_*.json`）——**已下载的书永远不进 new_books.txt**
3. ebook-info 同名 + 作者对得上 → dup
4. 书架内更早的同名同作者条目 → dup
5. 书架前 HEAD_NEW_COUNT 本（默认 500，按 shelf_books.txt 顺序）→ new
6. 其余 → new

用法：
    python dedupe_shelf_books.py
    python dedupe_shelf_books.py --dry-run
    python dedupe_shelf_books.py --head-new-count 0   # 关闭前 N 本规则
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

# 书架前 N 本：不在 forbid 里就一律当新书（按 shelf_books.txt 顺序数）
HEAD_NEW_COUNT = 500

# 半角/全角括号内容（非嵌套，循环处理；期数括号保留内文）
_PAREN_RE = re.compile(r"[（(]([^（）()]*)[）)]")
# 副标题分隔：破折号/横线/冒号（取主标题）
_SUBTITLE_RE = re.compile(r"\s*(?:——|—|－|–|-|：|:)\s*")
_WS_RE = re.compile(r"\s+")
_CN_NUM = r"[零〇○一二三四五六七八九十百千两0-9]"
# 纯年份：年鉴/年刊（2016）（2023年）
_YEAR_ONLY_RE = re.compile(r"^\d{4}年?$")
# 期数/卷次标记：2019年、第N期/辑/卷/册/本、总第N辑、第N本第N期
_ISSUE_MARK_RE = re.compile(
    rf"(?:"
    rf"\d{{4}}\s*年"
    rf"|总\s*第?\s*{_CN_NUM}+\s*(?:卷|期|辑|册|本)"
    rf"|第{_CN_NUM}+\s*(?:卷|期|辑|册|本)"
    rf")"
)
_ISSUE_NUM_RE = re.compile(
    rf"(总?第)({_CN_NUM}+)(卷|期|辑|册|本)"
)
_YEAR_TAIL_RE = re.compile(r"(\d{4})年")
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "○": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_PUNCT_DROP = ("·", "・", ".", "/", "／", "、", ",", "，")
# 作者比对：去朝代/国别标记（[宋]【北宋】）与角色词（著/编/选注/主编…）；
# 圆括号里的别名（如「王晓磊（六神磊磊）」）保留内容，只去括号本身。
_AUTHOR_ERA_RE = re.compile(r"[\[【][^\]】]*[\]】]")
_AUTHOR_PAREN_RE = re.compile(r"[（()）]")
_AUTHOR_ROLE_RE = re.compile(
    r"(著|编著|编选|选编|选注|评注|注评|笺注|校注|校点|点校|整理|主编|副主编|"
    r"导读|译注|注释|集评|审校|修订|编译|编撰|解注|笺释|选评|评析|编注|校笺|"
    r"疏证|考释|辑录|辑校|校订|注译|全译|等|编|注|选|译|校|辑)"
)
_AUTHOR_SPLIT_RE = re.compile(r"[\s,，、；;·/／]+")


def _parse_cn_or_arabic_int(token: str) -> int | None:
    """把期数里的中文/阿拉伯数字转成 int；无法解析则返回 None。"""
    if not token:
        return None
    if token.isdigit():
        return int(token)
    if any(ch not in _CN_DIGITS and ch not in _CN_UNITS for ch in token):
        return None
    total = 0
    current = 0
    for ch in token:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
            continue
        total += (current or 1) * _CN_UNITS[ch]
        current = 0
    return total + current


def _arabicize_issue_numbers(s: str) -> str:
    """第十四辑 / 总第二十七辑 → 第14辑 / 总第27辑。"""

    def repl(match: re.Match[str]) -> str:
        prefix, num, unit = match.group(1), match.group(2), match.group(3) or ""
        parsed = _parse_cn_or_arabic_int(num)
        if parsed is None:
            return match.group(0)
        return f"{prefix}{parsed}{unit}"

    return _ISSUE_NUM_RE.sub(repl, s)


def _is_issue_paren(inner: str) -> bool:
    """括号内是否为期刊期数/卷次/年份，而非「珍藏本」「全二册」等装饰。"""
    text = inner.strip()
    if not text:
        return False
    if _YEAR_ONLY_RE.match(text):
        return True
    return _ISSUE_MARK_RE.search(text) is not None


def _replace_paren(match: re.Match[str]) -> str:
    inner = match.group(1)
    return inner if _is_issue_paren(inner) else ""


def _title_keeps_issue_paren(title: str) -> bool:
    """原书名是否含应保留的期数括号。"""
    return any(_is_issue_paren(m.group(1)) for m in _PAREN_RE.finditer(str(title or "")))


def author_tokens(author: str | None) -> frozenset[str]:
    """作者串 → 人名 token 集合（去朝代标记与「著/编/选注/主编」等角色词，丢单字）。"""
    s = _AUTHOR_ERA_RE.sub(" ", str(author or ""))
    s = _AUTHOR_PAREN_RE.sub(" ", s)
    s = _AUTHOR_ROLE_RE.sub(" ", s)
    return frozenset(t for t in _AUTHOR_SPLIT_RE.split(s) if len(t) >= 2)


def authors_match(want: frozenset[str], have: frozenset[str]) -> bool:
    """作者是否对得上（至少一人相同）。

    任一侧拿不到 token（作者缺失/全是角色词）时退回「按书名判重」，
    避免因作者信息不全把重复书当新书。
    """
    if not want or not have:
        return True
    return bool(want & have)


def normalize_title(title: str | None) -> str:
    """归一化书名：去装饰括号与副标题，保留期刊期数。"""
    s = str(title or "").strip()
    if not s:
        return ""
    # 反复处理括号段：装饰性删除，期数保留内文
    while True:
        nxt = _PAREN_RE.sub(_replace_paren, s)
        if nxt == s:
            break
        s = nxt
    # 横线/冒号后的副标题去掉，只留主标题
    s = _SUBTITLE_RE.split(s, maxsplit=1)[0]
    s = _arabicize_issue_numbers(s)
    s = _YEAR_TAIL_RE.sub(r"\1", s)
    s = _WS_RE.sub("", s)  # 去空白，避免「词 品」vs「词品」
    for ch in _PUNCT_DROP:
        s = s.replace(ch, "")
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


def load_title_author_keys(path: Path) -> dict[str, set[frozenset[str]]]:
    """从 shelf 格式文件收集 {归一化主书名: {作者 token 集, ...}}（空标题跳过）。"""
    out: dict[str, set[frozenset[str]]] = {}
    for _book_id, title, author, _raw in load_shelf_lines(path):
        key = normalize_title(title)
        if not key:
            continue
        out.setdefault(key, set()).add(author_tokens(author))
    return out


def merge_title_author_keys(
    *maps: dict[str, set[frozenset[str]]],
) -> dict[str, set[frozenset[str]]]:
    out: dict[str, set[frozenset[str]]] = {}
    for m in maps:
        for key, authors in m.items():
            out.setdefault(key, set()).update(authors)
    return out


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


def _append_unique_id_lines(path: Path, lines: list[str]) -> None:
    """把行追加到 shelf 格式文件（跳过已有 ID）。"""
    if not lines:
        return
    existing = load_id_set_from_lines(path) if path.is_file() else set()
    to_append: list[str] = []
    for line in lines:
        parsed = parse_shelf_line(line)
        if not parsed:
            continue
        if parsed[0] in existing:
            continue
        to_append.append(line.rstrip("\n"))
        existing.add(parsed[0])
    if to_append:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for line in to_append:
                f.write(line + "\n")


def _append_dup_lines(dup_path: Path, lines: list[str]) -> None:
    """把行追加到 dup（跳过已有 ID）。"""
    _append_unique_id_lines(dup_path, lines)


def _rewrite_shelf_file(path: Path, lines: list[str]) -> None:
    text = "\n".join(line.rstrip("\n") for line in lines)
    if lines:
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def migrate_stale_from_new(
    new_path: Path,
    dup_path: Path,
    *,
    forbid_ids: set[str],
    downloaded_ids: set[str],
    ebook_keys: dict[str, list[dict]],
    head_new_ids: set[str] | None = None,
    dry_run: bool = False,
) -> list[tuple[str, str]]:
    """把 new 中按当前规则已属 dup 的书迁到 dup。

    含：forbid / 本地已下载（已下载的书不进 new）/ ebook-info 同名同作者 /
    与已有 dup 或 new 内先前条目同名同作者。
    返回 [(raw_line, reason), ...]。
    """
    if not new_path.is_file():
        return []
    lines = new_path.read_text(encoding="utf-8").splitlines()
    # 已在 dup 中的书名视为已占用；new 内按出现顺序只保留第一本
    seen = load_title_author_keys(dup_path)
    keep: list[str] = []
    moved: list[tuple[str, str]] = []
    for line in lines:
        parsed = parse_shelf_line(line)
        if not parsed:
            keep.append(line.rstrip("\n"))
            continue
        book_id, title, author = parsed
        bucket, reason = classify_book(
            book_id,
            title,
            author,
            forbid_ids=forbid_ids,
            downloaded_ids=downloaded_ids,
            ebook_keys=ebook_keys,
            seen_title_authors=seen,
            head_new_ids=head_new_ids,
        )
        if bucket == "dup":
            moved.append((line.rstrip("\n"), reason))
        else:
            keep.append(line.rstrip("\n"))
        key = normalize_title(title)
        if key:
            seen.setdefault(key, set()).add(author_tokens(author))
    if not moved:
        return []

    if dry_run:
        return moved

    _rewrite_shelf_file(new_path, keep)
    _append_dup_lines(dup_path, [line for line, _ in moved])
    return moved


def migrate_stale_from_dup(
    dup_path: Path,
    new_path: Path,
    *,
    forbid_ids: set[str],
    downloaded_ids: set[str],
    ebook_keys: dict[str, list[dict]],
    head_new_ids: set[str] | None = None,
    dry_run: bool = False,
) -> list[tuple[str, str]]:
    """把 dup 中按当前规则不再算重复的书迁回 new。

    救回三类误判：括号内带期数/卷次/年份的条目、同名但作者对不上（旧规则按书名
    误判为重复）、书架前 N 本。其余普通书仍留 dup，避免把手工/历史录入的条目一并捞出。
    forbid / 本地已下载（已下载的书不进 new）仍留 dup。
    返回 [(raw_line, reason), ...]。
    """
    if not dup_path.is_file():
        return []
    lines = dup_path.read_text(encoding="utf-8").splitlines()
    seen = load_title_author_keys(new_path)
    keep: list[str] = []
    moved: list[tuple[str, str]] = []
    for line in lines:
        parsed = parse_shelf_line(line)
        if not parsed:
            keep.append(line.rstrip("\n"))
            continue
        book_id, title, author = parsed
        bucket, reason = classify_book(
            book_id,
            title,
            author,
            forbid_ids=forbid_ids,
            downloaded_ids=downloaded_ids,
            ebook_keys=ebook_keys,
            seen_title_authors=seen,
            head_new_ids=head_new_ids,
        )
        raw = line.rstrip("\n")
        if bucket == "new" and (
            _title_keeps_issue_paren(title)
            or reason == "new-diff-author"
            or (head_new_ids is not None and book_id in head_new_ids)
        ):
            moved.append((raw, reason))
        else:
            keep.append(raw)
        key = normalize_title(title)
        if key:
            seen.setdefault(key, set()).add(author_tokens(author))
    if not moved:
        return []
    if dry_run:
        return moved
    _rewrite_shelf_file(dup_path, keep)
    _append_unique_id_lines(new_path, [line for line, _ in moved])
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
    author: str = "",
    *,
    forbid_ids: set[str],
    downloaded_ids: set[str],
    ebook_keys: dict[str, list[dict]],
    seen_title_authors: dict[str, set[frozenset[str]]] | None = None,
    head_new_ids: set[str] | None = None,
) -> tuple[str, str]:
    """返回 (bucket, reason)。bucket 为 'dup' 或 'new'。

    判定重复的口径是**书名 + 作者都对得上**：

    1. forbid → dup
    2. 本地已下载 → dup（已下载的书不进 new）
    3. ebook-info 同名 + 作者对得上 → dup（ebook-info）
    4. 书架内更早的同名同作者条目 → dup（shelf-title-dup）
    5. 书架前 N 本 → new（new-head）
    6. 其余 → new；其中「同名但作者对不上」给原因 new-diff-author

    seen_title_authors：已占用的 {归一化主书名: {作者 token 集}}（书架/new/dup 内
    先前条目）。调用方负责在处理后把当前书名与作者加入该映射。
    head_new_ids：书架前 N 本的 ID（非 forbid、非已下载就一律当新书）。
    """
    if book_id in forbid_ids:
        return "dup", "forbid"
    if book_id in downloaded_ids:
        return "dup", "downloaded"
    key = normalize_title(title)
    want = author_tokens(author)
    same_title_only = False
    if key and key in ebook_keys:
        if any(
            authors_match(want, author_tokens(item.get("authorName")))
            for item in ebook_keys[key]
        ):
            return "dup", "ebook-info"
        same_title_only = True
    if seen_title_authors is not None and key and key in seen_title_authors:
        if any(authors_match(want, have) for have in seen_title_authors[key]):
            return "dup", "shelf-title-dup"
        same_title_only = True
    if head_new_ids and book_id in head_new_ids:
        return "new", "new-head"
    if same_title_only:
        return "new", "new-diff-author"
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
    head_new_count: int = HEAD_NEW_COUNT,
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
    all_shelf_rows = load_shelf_lines(shelf_path)
    # 空书名行（抓取时被换行拆散的残行）既不能导出也不能参与判重，直接跳过
    shelf = [row for row in all_shelf_rows if row[1].strip()]
    skipped_empty_title = len(all_shelf_rows) - len(shelf)
    # 书架前 N 本（不在 forbid 里的一律当新书）
    head_new_ids = (
        {row[0] for row in shelf[:head_new_count]} if head_new_count > 0 else set()
    )

    moved_pairs = migrate_stale_from_new(
        new_path,
        dup_path,
        forbid_ids=forbid_ids,
        downloaded_ids=downloaded_ids,
        ebook_keys=ebook_keys,
        head_new_ids=head_new_ids,
        dry_run=dry_run,
    )
    rescued_pairs = migrate_stale_from_dup(
        dup_path,
        new_path,
        forbid_ids=forbid_ids,
        downloaded_ids=downloaded_ids,
        ebook_keys=ebook_keys,
        head_new_ids=head_new_ids,
        dry_run=dry_run,
    )
    moved = [line for line, _ in moved_pairs]
    moved_reasons: dict[str, int] = {}
    for _line, reason in moved_pairs:
        moved_reasons[reason] = moved_reasons.get(reason, 0) + 1
    rescued = [line for line, _ in rescued_pairs]
    rescued_reasons: dict[str, int] = {}
    for _line, reason in rescued_pairs:
        rescued_reasons[reason] = rescued_reasons.get(reason, 0) + 1

    processed = load_id_set_from_lines(dup_path) | load_id_set_from_lines(new_path)
    # dry-run 时 migrate 未写盘，processed 需把即将迁出的 new 视作已处理
    if dry_run and moved:
        for line in moved:
            parsed = parse_shelf_line(line)
            if parsed:
                processed.add(parsed[0])
    if dry_run and rescued:
        for line in rescued:
            parsed = parse_shelf_line(line)
            if parsed:
                processed.add(parsed[0])

    # new/dup 已处理条目的书名 + 作者均已占用（migrate 只在两者间移动，映射不变）
    seen_title_authors = merge_title_author_keys(
        load_title_author_keys(dup_path), load_title_author_keys(new_path)
    )

    todo = [row for row in shelf if row[0] not in processed]

    dup_lines: list[str] = []
    new_lines: list[str] = []
    reasons = {
        "forbid": 0,
        "downloaded": 0,
        "ebook-info": 0,
        "shelf-title-dup": 0,
        "new-head": 0,
        "new-diff-author": 0,
        "new": 0,
    }

    for book_id, title, author, raw in todo:
        bucket, reason = classify_book(
            book_id,
            title,
            author,
            forbid_ids=forbid_ids,
            downloaded_ids=downloaded_ids,
            ebook_keys=ebook_keys,
            seen_title_authors=seen_title_authors,
            head_new_ids=head_new_ids,
        )
        reasons[reason] = reasons.get(reason, 0) + 1
        key = normalize_title(title)
        if key:
            seen_title_authors.setdefault(key, set()).add(author_tokens(author))
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
        "head_new_count": head_new_count,
        "head_new_ids": len(head_new_ids),
        "skipped_empty_title": skipped_empty_title,
        "migrated_new_to_dup": len(moved),
        "migrated_reasons": moved_reasons,
        "migrated_dup_to_new": len(rescued),
        "rescued_reasons": rescued_reasons,
        "todo": len(todo),
        "dup": len(dup_lines),
        "new": len(new_lines),
        "reasons": reasons,
        "dry_run": dry_run,
        "dup_samples": dup_lines[:5],
        "new_samples": new_lines[:5],
        "migrated_samples": moved[:5],
        "rescued_samples": rescued[:5],
    }


def _print_report(result: dict) -> None:
    print(
        f"禁止列表 {result['forbid_count']} 本 | "
        f"本地已下载 {result['downloaded_count']} 本"
        f"{'' if result['books_dir_exists'] else '（目录不存在，已跳过）'}"
    )
    print(f"books_dir: {result['books_dir']}")
    print(
        f"书架前 {result.get('head_new_count', 0)} 本规则: "
        f"{result.get('head_new_ids', 0)} 个 ID（不在 forbid 里的一律当新书）"
    )
    if result.get("skipped_empty_title"):
        print(f"跳过空书名残行: {result['skipped_empty_title']} 行")
    print(f"本轮 new→dup 迁移: {result['migrated_new_to_dup']} 本")
    mr = result.get("migrated_reasons") or {}
    if mr:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(mr.items()))
        print(f"  迁移原因: {detail}")
    if result["migrated_samples"]:
        for s in result["migrated_samples"]:
            print(f"  migrate: {s}")
    print(f"本轮 dup→new 迁回: {result.get('migrated_dup_to_new', 0)} 本")
    rr = result.get("rescued_reasons") or {}
    if rr:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(rr.items()))
        print(f"  迁回原因: {detail}")
    if result.get("rescued_samples"):
        for s in result["rescued_samples"]:
            print(f"  rescue: {s}")
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
        f"new-head={r.get('new-head', 0)}, "
        f"new-diff-author={r.get('new-diff-author', 0)}, "
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
        "--head-new-count",
        type=int,
        default=HEAD_NEW_COUNT,
        help=f"书架前 N 本不在 forbid 里就一律当新书（默认 {HEAD_NEW_COUNT}，0=关闭）",
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
            head_new_count=args.head_new_count,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    _print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
