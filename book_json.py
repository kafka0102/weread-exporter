#!/usr/bin/env python3
"""兼容 dedao/json 的书稿 JSON 组装与导出辅助函数。

纯函数为主，便于单测；浏览器抓取仍由 export_precise 负责。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterable

# 文件名禁止字符（跨平台）
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MD_IMAGE = re.compile(r"!?\[[^\]]*\]\([^)]*\)")
_MD_HEADING = re.compile(r"^#{1,6}\s+")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|\`)")


def safe_book_title_for_filename(title: str) -> str:
    """书名用于文件名时的安全化：去路径非法字符并压缩空白。"""
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", (title or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    return cleaned or "untitled"


def book_json_filename(book_id: str, title: str) -> str:
    """返回 `id_书名.json`。"""
    return f"{book_id}_{safe_book_title_for_filename(title)}.json"


def book_json_exists(out_dir: str | Path, book_id: str) -> bool:
    """out_dir 下是否已有以 `{book_id}_` 开头的 json（不依赖书名是否变化）。"""
    root = Path(out_dir)
    if not root.is_dir():
        return False
    prefix = f"{book_id}_"
    for path in root.iterdir():
        if path.is_file() and path.name.startswith(prefix) and path.suffix.lower() == ".json":
            return True
    return False


def find_existing_book_json(out_dir: str | Path, book_id: str) -> Path | None:
    """返回匹配的第一个已存在 json 路径；没有则 None。"""
    root = Path(out_dir)
    if not root.is_dir():
        return None
    prefix = f"{book_id}_"
    matches = sorted(
        p
        for p in root.iterdir()
        if p.is_file() and p.name.startswith(prefix) and p.suffix.lower() == ".json"
    )
    return matches[0] if matches else None


def chapter_id_for_index(index: int) -> str:
    """1-based 章节序号 → ch_0001。"""
    if index < 1:
        raise ValueError("chapter index must be >= 1")
    return f"ch_{index:04d}"


def md_to_plain_content(md_text: str, *, chapter_name: str = "") -> str:
    """把导出用的 chapter markdown 转成 dedao 风格纯文本。

    - 去掉 markdown 标题行（# ...）
    - 去掉图片 / 链接语法
    - 若首段等于 chapter_name 则去掉（避免与 chapter_name 字段重复）
    - 保留段落（双换行）
    """
    if not md_text:
        return ""

    paragraphs: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        paragraphs.append("\n".join(buf).strip() if len(buf) > 1 else buf[0].strip())
        buf = []

    for raw in md_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        if _MD_HEADING.match(line):
            # 标题行不进入 content；也作为段落边界
            flush()
            continue
        stripped = _MD_IMAGE.sub("", line).strip()
        if not stripped:
            # 纯图片行：当作段落边界，不写入正文
            flush()
            continue
        stripped = _MD_EMPHASIS.sub("", stripped).strip()
        if stripped:
            buf.append(stripped)
    flush()

    name = (chapter_name or "").strip()
    if name and paragraphs and paragraphs[0].strip() == name:
        paragraphs = paragraphs[1:]

    content = "\n\n".join(p for p in paragraphs if p)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    return content


def chapter_entry(
    chapter_name: str,
    index: int,
    content: str,
    *,
    chapter_id: str | None = None,
) -> dict[str, Any]:
    """构造 body 中的一章。"""
    plain = content if content is not None else ""
    plain = plain.strip()
    # strip 后内部段落空白保留：仅去掉首尾
    # 重新用原 plain 但 strip 首尾
    text = (content or "").strip()
    return {
        "chapter_name": chapter_name or "",
        "chapter_id": chapter_id or chapter_id_for_index(index),
        "content": text,
        "has_content": bool(text),
    }


def build_book_json(
    *,
    book_id: str,
    title: str,
    author: str = "",
    chapters: Iterable[dict[str, Any]],
    press: str = "",
    publication_date: str = "",
    isbn: str = "",
) -> dict[str, Any]:
    """组装完整书稿 JSON 对象。"""
    body: list[dict[str, Any]] = []
    word_count = 0
    for i, ch in enumerate(chapters, start=1):
        if "chapter_name" in ch and "content" in ch and "has_content" in ch and "chapter_id" in ch:
            entry = {
                "chapter_name": ch["chapter_name"],
                "chapter_id": ch["chapter_id"],
                "content": ch.get("content") or "",
                "has_content": bool(ch.get("has_content")),
            }
            # 以 content 为准校正 has_content
            entry["has_content"] = bool(str(entry["content"]).strip())
        else:
            entry = chapter_entry(
                ch.get("chapter_name") or ch.get("title") or "",
                i,
                ch.get("content") or "",
                chapter_id=ch.get("chapter_id"),
            )
        body.append(entry)
        word_count += len(entry["content"])

    return {
        "id": str(book_id),
        "title": title or str(book_id),
        "author": author or "",
        "press": press or "",
        "publication_date": publication_date or "",
        "isbn": isbn or "",
        "word_count": int(word_count),
        "body": body,
    }


def build_book_json_from_chapter_mds(
    *,
    book_id: str,
    title: str,
    author: str,
    chapter_files: Iterable[tuple[str, str]],
    press: str = "",
    publication_date: str = "",
    isbn: str = "",
) -> dict[str, Any]:
    """从 (chapter_name, md_text) 序列组装书稿。

    chapter_name 优先用调用方提供（通常来自 md 首行标题或 raw json title）。
    """
    chapters: list[dict[str, Any]] = []
    for idx, (name, md_text) in enumerate(chapter_files, start=1):
        chapter_name = (name or "").strip() or f"第{idx}章"
        content = md_to_plain_content(md_text, chapter_name=chapter_name)
        chapters.append(chapter_entry(chapter_name, idx, content))
    return build_book_json(
        book_id=book_id,
        title=title,
        author=author,
        chapters=chapters,
        press=press,
        publication_date=publication_date,
        isbn=isbn,
    )


def load_chapters_from_export_dir(book_dir: str | Path) -> list[tuple[str, str]]:
    """从 output/<book_id>/chapters + raw 读取章节 (title, md)。

    标题优先 raw/*.json 的 title，否则取 md 首个一级标题，再否则文件名。
    """
    root = Path(book_dir)
    md_dir = root / "chapters"
    raw_dir = root / "raw"
    if not md_dir.is_dir():
        return []

    files = sorted(p for p in md_dir.iterdir() if p.is_file() and p.suffix == ".md")
    result: list[tuple[str, str]] = []
    for md_path in files:
        md_text = md_path.read_text(encoding="utf-8")
        title = ""
        raw_path = raw_dir / f"{md_path.stem}.json"
        if raw_path.is_file():
            try:
                meta = json.loads(raw_path.read_text(encoding="utf-8"))
                title = str(meta.get("title") or "").strip()
            except (json.JSONDecodeError, OSError):
                title = ""
        if not title:
            for line in md_text.splitlines():
                m = re.match(r"^#\s+(.+)$", line.strip())
                if m:
                    title = m.group(1).strip()
                    break
        if not title:
            title = md_path.stem
        result.append((title, md_text))
    return result


def write_book_json(
    out_dir: str | Path,
    book: dict[str, Any],
    *,
    replace_existing: bool = True,
) -> Path:
    """写入 id_书名.json，返回路径。目录不存在则创建。

    replace_existing=True 时删除同 book_id 前缀的旧 json，避免 force 后书名变化留下多份。
    """
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    book_id = str(book["id"])
    path = root / book_json_filename(book_id, str(book.get("title") or book_id))
    if replace_existing:
        prefix = f"{book_id}_"
        for old in root.iterdir():
            if (
                old.is_file()
                and old.suffix.lower() == ".json"
                and old.name.startswith(prefix)
                and old.resolve() != path.resolve()
            ):
                old.unlink()
    path.write_text(json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def chapter_sleep_seconds(
    char_count: int,
    *,
    per_1k: float = 2.0,
    min_seconds: float = 2.0,
    max_seconds: float = 15.0,
) -> float:
    """按章字数计算等待秒数：ceil(chars/1000)*per_1k，夹在 [min,max]。"""
    chars = max(0, int(char_count or 0))
    units = max(1, math.ceil(chars / 1000)) if chars > 0 else 1
    # 即使 0 字也至少 min（调用方在章切换后等待，避免连点）
    raw = units * float(per_1k)
    if chars == 0:
        raw = float(min_seconds)
    return float(min(max(raw, float(min_seconds)), float(max_seconds)))


def parse_shelf_line(line: str) -> tuple[str, str, str] | None:
    """解析 `ID,书名,作者` 行；非法则 None。"""
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split(",", 2)
    if len(parts) < 2:
        return None
    book_id = parts[0].strip()
    title = parts[1].strip() if len(parts) > 1 else ""
    author = parts[2].strip() if len(parts) > 2 else ""
    if not book_id:
        return None
    return book_id, title, author


def iter_batch_book_ids(list_path: str | Path) -> list[tuple[str, str, str]]:
    """读取 new_books/shelf 清单，返回 [(id, title, author), ...]。"""
    path = Path(list_path)
    if not path.is_file():
        return []
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_shelf_line(line)
        if not parsed:
            continue
        book_id, title, author = parsed
        if book_id in seen:
            continue
        seen.add(book_id)
        items.append((book_id, title, author))
    return items


def filter_pending_books(
    books: Iterable[tuple[str, str, str]],
    out_dir: str | Path,
) -> list[tuple[str, str, str]]:
    """剔除 out_dir 中已存在 json 的书。"""
    return [b for b in books if not book_json_exists(out_dir, b[0])]
