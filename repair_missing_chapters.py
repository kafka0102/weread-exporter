#!/usr/bin/env python3
"""按目录章名把「被并进邻章」的缺失章从正文里切出来（不开浏览器，不重下）。

背景：体检报告里的「章节缺失」档——目录里有、导出章节里没有，标题以独立整行的
形式出现在别章正文中。这类书的正文其实都在文件里，只是章界错位，不必整本重下。
（repair_empty_chapters.py 只补「章在、正文空」的情况，不插/删章节，处理不了这类。）

做法：只切、不改字。

1. 读成品 JSON 的 body + output/<id>/_catalog.json；
2. 用 detect_merged_chapters.missing_catalog_titles() 求「缺失且独立成行」的目录项
   （已排除卷标题、作者分组行、目录/索引章、折行与句中提及）；
3. 在 output/<id>/chapters/*.md 里按行找这些标题（整行相等、每个标题只用一次、
   目录顺序单调递增），得到切点；
4. 在每个切点处把该章拆成多段：切点前那段保留原章名，其后每段用对应目录章名；
5. 校验「所有新 md 正文拼接 == 所有旧 md 正文拼接」（逐字节零丢失），再从 md 重新
   生成成品 JSON（与导出同一条管线，press/publication_date/isbn 从旧 JSON 继承）。

安全：

- 默认 --dry-run，只报告不写任何文件；--apply 才落盘。
- 落盘前把成品 JSON 与 chapters/、raw/ 一起备份到 output/<id>/backups/<时间戳>/。
- 只在「缺失数确实减少」时才认为修复成功；正文有任何丢失就整体放弃。
- 章号会重新编号（ch_0001…），因为确实插入了新章。

用法：
    python repair_missing_chapters.py --dry-run
    python repair_missing_chapters.py --dry-run --book cfa32be0721070d2cfa3782 --json
    python repair_missing_chapters.py --apply --book cfa32be0721070d2cfa3782
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import env_config  # noqa: F401  # 导入即加载 .env
from env_config import BOOKS_DIR

import export_precise
from book_json import md_to_plain_content, write_book_json
from detect_merged_chapters import (
    chapter_rows,
    missing_catalog_titles,
    normalize_text,
)

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG_ROOT = ROOT_DIR / "output"
# 目录章名最短长度（太短的名字在正文里天然会重复出现，不能当切点）
MIN_TITLE_LEN = 4


def load_catalog(catalog_path: Path) -> list[str]:
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(t) for t in data if str(t or "").strip()]


def _md_heading_and_body(md_text: str) -> tuple[str, str]:
    """拆出 md 的「# 章名」标题行与正文（正文不含标题行及其后的空行）。"""
    lines = md_text.split("\n")
    if lines and lines[0].startswith("#"):
        name = lines[0].lstrip("#").strip()
        idx = 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        return name, "\n".join(lines[idx:])
    return "", md_text


def read_md_files(md_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted(p for p in md_dir.iterdir() if p.suffix == ".md"):
        text = path.read_text(encoding="utf-8")
        name, body = _md_heading_and_body(text)
        out.append({"path": path, "name": name, "body": body})
    return out


def plan_book(
    book_id: str,
    book: dict,
    md_files: list[dict],
    catalog_titles: list[str],
) -> dict | None:
    """算这本书的切章方案；没有可切点返回 None。"""
    if not md_files or not catalog_titles:
        return None
    missing = missing_catalog_titles(chapter_rows(book.get("body") or []), catalog_titles)
    missing_keys: dict[str, str] = {}
    for title in missing:
        key = normalize_text(title)
        if len(key) >= MIN_TITLE_LEN:
            missing_keys.setdefault(key, title)
    if not missing_keys:
        return None
    cat_index: dict[str, int] = {}
    for i, title in enumerate(catalog_titles):
        cat_index.setdefault(normalize_text(title), i)

    used: set[str] = set()
    last_idx = -1
    cuts: list[dict] = []
    for file in md_files:
        for pos, line in enumerate(file["body"].split("\n")):
            key = normalize_text(line)
            title = missing_keys.get(key)
            if not title or key in used:
                continue
            idx = cat_index.get(key)
            if idx is None or idx <= last_idx:
                continue
            cuts.append({"file": file["path"], "pos": pos, "title": title, "index": idx})
            used.add(key)
            last_idx = idx
    if not cuts:
        return None

    cuts_by_file: dict[Path, list[dict]] = {}
    for cut in cuts:
        cuts_by_file.setdefault(cut["file"], []).append(cut)

    chapters: list[dict] = []
    for file in md_files:
        file_cuts = sorted(cuts_by_file.get(file["path"], []), key=lambda c: c["pos"])
        lines = file["body"].split("\n")
        bounds = [0] + [c["pos"] for c in file_cuts] + [len(lines)]
        names = [file["name"]] + [c["title"] for c in file_cuts]
        for k in range(len(names)):
            seg = lines[bounds[k] : bounds[k + 1]]
            if not seg and k == 0:
                # 切点落在正文首行：原章名下没有任何正文行，不造空章。
                # 注意只丢「零行」段——原本就存在的空章（如只有标题的占位章）
                # 必须原样保留，否则行序列对不齐、且会凭空少一章。
                continue
            body = "\n".join(seg)
            chapters.append(
                {
                    "name": names[k],
                    "body": body,
                    "source": str(file["path"]),
                    "carry_assets": k == 0,
                }
            )
    if len(chapters) <= len(md_files):
        return None

    # 逐行比对：切点只搬动行的归属，行序列必须一模一样（含空行）
    old_lines = [line for f in md_files for line in f["body"].split("\n")]
    new_lines = [line for c in chapters for line in c["body"].split("\n")]
    return {
        "book_id": book_id,
        "chapters": chapters,
        "cuts": cuts,
        "missing_before": missing,
        "text_preserved": old_lines == new_lines,
        "chapters_before": len(md_files),
        "chapters_after": len(chapters),
    }


def _read_raw_meta(raw_path: Path) -> dict:
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def apply_plan(
    book_id: str,
    plan: dict,
    old_book: dict,
    *,
    book_dir: Path,
    out_dir: Path,
) -> dict:
    """把方案落盘：备份 → 写新 md/raw → 从 md 重新生成成品 JSON。"""
    if not plan.get("text_preserved"):
        raise RuntimeError(f"{book_id}: 切分后正文不一致，放弃修复")
    md_dir = book_dir / "chapters"
    raw_dir = book_dir / "raw"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = book_dir / "backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in ("chapters", "raw"):
        src = book_dir / name
        if src.is_dir():
            shutil.move(str(src), str(backup_dir / name))
    md_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    old_json_path = next(iter(sorted(out_dir.glob(f"{book_id}_*.json"))), None)
    if old_json_path:
        shutil.copy2(old_json_path, backup_dir / old_json_path.name)

    # 原章按序号取图；切出来的新段不带图（图无法按行归属）
    source_images: list[list] = []
    backup_raw = backup_dir / "raw"
    for i in range(1, plan["chapters_before"] + 1):
        raw_path = backup_raw / f"{i:04d}.json"
        meta = _read_raw_meta(raw_path) if raw_path.is_file() else {}
        source_images.append(meta.get("images") or [])

    for idx, chapter in enumerate(plan["chapters"], 1):
        name = chapter["name"] or f"第{idx}章"
        md_text = f"# {name}\n\n\n{chapter['body']}"
        (md_dir / f"{idx:04d}.md").write_text(md_text, encoding="utf-8")
        plain = md_to_plain_content(md_text, chapter_name=name)
        images: list = []
        if chapter.get("carry_assets"):
            origin = int(Path(chapter["source"]).stem)
            if 1 <= origin <= len(source_images):
                images = source_images[origin - 1]
        (raw_dir / f"{idx:04d}.json").write_text(
            json.dumps(
                {"title": name, "images": images, "text_len": len(normalize_text(plain))},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    with tempfile.TemporaryDirectory() as tmp:
        _path, book = export_precise.finalize_book_json(
            book_id,
            old_book.get("title") or book_id,
            old_book.get("author") or "",
            str(book_dir),
            tmp,
        )
    # 出版信息等元数据 finalize 不产出，从旧 JSON 继承
    for key in ("id", "title", "author", "press", "publication_date", "isbn"):
        if key in old_book:
            book[key] = old_book[key]
    path = write_book_json(str(out_dir), book)
    return {
        "json_path": str(path),
        "chapters": len(book["body"]),
        "word_count": book["word_count"],
    }


def iter_books(books_dir: Path, catalog_root: Path, only: set[str] | None):
    for path in sorted(books_dir.glob("*.json")):
        book_id = path.name.split("_", 1)[0]
        if only and book_id not in only:
            continue
        catalog_path = catalog_root / book_id / "_catalog.json"
        md_dir = catalog_root / book_id / "chapters"
        if not catalog_path.is_file() or not md_dir.is_dir():
            continue
        yield book_id, path, catalog_path, md_dir


def scan_and_repair(args) -> dict:
    books_dir = Path(args.books_dir).expanduser()
    catalog_root = Path(args.catalog_root)
    only = set(args.book or []) or None
    findings: list[dict] = []
    applied: list[dict] = []
    scanned = 0
    for book_id, json_path, catalog_path, md_dir in iter_books(books_dir, catalog_root, only):
        if args.limit and not args.apply and len(findings) >= args.limit:
            break
        scanned += 1
        try:
            book = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  跳过 {book_id}：读取成品失败（{exc}）")
            continue
        plan = plan_book(book_id, book, read_md_files(md_dir), load_catalog(catalog_path))
        if not plan:
            continue
        entry = {
            "book_id": book_id,
            "title": str(book.get("title") or ""),
            "chapters_before": plan["chapters_before"],
            "chapters_after": plan["chapters_after"],
            "recovered": len(plan["cuts"]),
            "titles": [c["title"] for c in plan["cuts"]],
            "missing_before": len(plan["missing_before"]),
            "text_preserved": plan["text_preserved"],
        }
        if args.apply:
            result = apply_plan(
                book_id, plan, book, book_dir=catalog_root / book_id, out_dir=books_dir
            )
            entry["json_path"] = result["json_path"]
            entry["applied"] = True
            applied.append(entry)
        findings.append(entry)
    return {"scanned": scanned, "findings": findings, "applied": applied}


def render(report: dict, *, dry_run: bool) -> str:
    findings = report["findings"]
    total_cuts = sum(f["recovered"] for f in findings)
    mode = "dry-run（未改动任何文件）" if dry_run else "apply（已落盘）"
    lines = [
        f"扫描 {report['scanned']} 本（有目录+中间产物），可重切 {len(findings)} 本，"
        f"共补回 {total_cuts} 章；模式：{mode}",
        "",
    ]
    for f in sorted(findings, key=lambda x: -x["recovered"]):
        mark = "OK" if f["text_preserved"] else "!!"
        lines.append(
            f"  [{mark}] {f['book_id']} {f['title'][:24]:<26} "
            f"章 {f['chapters_before']}→{f['chapters_after']} "
            f"补回 {f['recovered']}（原缺 {f['missing_before']}）"
        )
        preview = "、".join(t[:14] for t in f["titles"][:5])
        if preview:
            lines.append(f"       切出：{preview}{' …' if len(f['titles']) > 5 else ''}")
    if report.get("applied"):
        lines.append("")
        lines.append(f"已写回成品 JSON：{len(report['applied'])} 本")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="按目录章名重切被并进邻章的缺失章（不开浏览器）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只报告（默认）")
    mode.add_argument("--apply", action="store_true", help="落盘修复（带备份）")
    parser.add_argument("--book", action="append", help="只处理指定 book_id，可重复")
    parser.add_argument("--books-dir", default=str(BOOKS_DIR), help="成品 JSON 目录")
    parser.add_argument(
        "--catalog-root", default=str(DEFAULT_CATALOG_ROOT), help="中间产物根目录（默认 output）"
    )
    parser.add_argument("--tsv", help="把明细写成 TSV")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--limit", type=int, default=0, help="最多报告多少本（0=不限）")
    return parser.parse_args(argv)


def write_tsv(path: str, findings: list[dict]) -> None:
    cols = [
        "book_id", "title", "chapters_before", "chapters_after",
        "recovered", "missing_before", "text_preserved",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        for f in findings:
            fh.write("\t".join(str(f.get(c, "")) for c in cols) + "\n")


def main(argv=None) -> int:
    args = parse_args(argv)
    report = scan_and_repair(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report, dry_run=not args.apply))
    if args.tsv:
        write_tsv(args.tsv, report["findings"])
        print(f"\n已写出 {args.tsv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
