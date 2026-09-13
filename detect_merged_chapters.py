#!/usr/bin/env python3
"""体检已下载电子书：找出「正文被合并进单一章节」的书。

背景：书前若带印刷目录页，旧的导出逻辑会把目录里的章名当成章首，一路推进到目录
末章，导致前几十章只剩标题占位、整本正文堆在最后一章（详见
`.agents/skills/detect-merged-chapters/SKILL.md`）。

判定只看各章正文字数分布，另用「最大章里是否混入其他章名/其他章正文」做交叉印证：

- ≤2 章：豁免（诗词画册、单篇，单章一两万至两三万字属正常）
- 3—4 章：默认不判定（「版权信息 + 正文 + 附录」这类结构天然单章占比高）
- 5—9 章：单章 ≥70% 且其余多为小章才报，≥90% 为严重（上下卷各半属正常）
- ≥10 章：单章过半即不合理，≥80% 且其余多为小章为严重
- 另有「多章无正文」一档：空章 + 标题占位章占比过高

用法：
    python detect_merged_chapters.py
    python detect_merged_chapters.py --include-suspect --tsv .scratch/merged.tsv
    BOOKS_DIR=~/data/weixin/books python detect_merged_chapters.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Iterator

import env_config  # noqa: F401  # 导入即加载 .env
from env_config import BOOKS_DIR

ROOT_DIR = Path(__file__).resolve().parent

# 章节数下限：≤4 章的书（诗词画册/单篇/「正文+附录」）不做单章占比判定
MIN_CHAPTERS = 5
# 「章数不多」的分界：5—9 章时门槛收紧（上下两卷各半是正常结构）
MANY_CHAPTERS = 10
# 全书正文字数下限：过短的书样本不足，不做判定
MIN_TOTAL_CHARS = 5000
# 单章占比：≥10 章时过半即不合理；≥80% 视为严重
MAJOR_SHARE = 0.5
SEVERE_SHARE = 0.8
# 5—9 章时的收紧门槛
FEW_CHAPTERS_MAJOR_SHARE = 0.7
FEW_CHAPTERS_SEVERE_SHARE = 0.9
# 「小章」判定：绝对字数低于下限，或不足全书 1%
TINY_ABS_CHARS = 200
TINY_BOOK_SHARE = 0.01
# 小章占比下限：严重档 / 中度档 / 5—9 章档
TINY_RATIO_SEVERE = 0.5
TINY_RATIO_MAJOR = 0.3
TINY_RATIO_FEW_CHAPTERS = 0.5
# 空章 + 标题占位章占比达到此值 → 「多章无正文」
BLANK_RATIO = 0.5
# 交叉印证的章名最短长度（太短的名字在正文里天然会重复出现）
EVIDENCE_NAME_MIN_LEN = 4
# 交叉印证时其他章正文的抽样窗口（足够长才算重复，避免套话/固定句式误命中）
EVIDENCE_WINDOW = 24
EVIDENCE_STEP = 15
# 同一章 ≥ 此比例的抽样窗口出现在最大章内，才认定该章正文被合并
# （真实合并≈100%，偶然撞句多在 2% 以下）
EVIDENCE_DUP_RATIO = 0.5
# 太短的章窗口太少，不参与正文重复判定
EVIDENCE_DUP_MIN_CHARS = 200
# 强证据：最大章占比 ≥ 此比例，且混入其他章正文 ≥3 章或章名 ≥10 个 → 直接判合并
EVIDENCE_SHARE_FLOOR = 0.3
EVIDENCE_DUP_CHAPTERS = 2
EVIDENCE_LEAKED_NAMES = 10

# 书前/书末性质章名：这类章吞掉全书基本可确定是合并，正文性章名需人工复核
_BOOKEND_TITLE_RE = re.compile(
    r"^(版权信息|版权页|封面|封底|扉页|书名页|出版信息|出版说明|编辑说明|"
    r"目录|总目|索引|索引目录|附录|后记|编后记|跋|出版后记|参考文献|参考资料|"
    r"年谱|大事记|作者简介|内容提要|文前|文后)"
)

LEVEL_ORDER = {
    "严重": 0,
    "中度": 1,
    "章节缺失": 2,
    "可疑": 3,
    "多章无正文": 4,
}

_WS_RE = re.compile(r"[\s\u3000\u200b\u200c\u200d\ufeff\u2060]+")
# 引用文献里的书名/篇名：《标题》不算「标题出现在正文中」
_QUOTE_OPEN = "《"
_QUOTE_CLOSE = "》"
_CATALOG_TITLE_MIN_LEN = 3


def normalize_text(text: str) -> str:
    """去掉空白与零宽字符，便于按字数与片段比对。"""
    return _WS_RE.sub("", text or "")


def chapter_rows(body: Iterable[dict]) -> list[dict]:
    """把 JSON body 规整成 [{index, name, text, chars, blank, placeholder}]。"""
    rows: list[dict] = []
    for i, chapter in enumerate(body or [], 1):
        if not isinstance(chapter, dict):
            continue
        name = str(chapter.get("chapter_name") or "")
        text = normalize_text(str(chapter.get("content") or ""))
        rows.append(
            {
                "index": i,
                "name": name,
                "text": text,
                "chars": len(text),
                "blank": len(text) == 0,
                # 标题占位：正文只有章名本身（或更短）
                "placeholder": 0 < len(text) <= len(normalize_text(name)),
            }
        )
    return rows


def analyze_rows(
    rows: list[dict],
    *,
    catalog_titles=None,
    min_chapters: int = MIN_CHAPTERS,
    min_total_chars: int = MIN_TOTAL_CHARS,
    major_share: float = MAJOR_SHARE,
    severe_share: float = SEVERE_SHARE,
    few_major_share: float = FEW_CHAPTERS_MAJOR_SHARE,
    few_severe_share: float = FEW_CHAPTERS_SEVERE_SHARE,
    tiny_abs_chars: int = TINY_ABS_CHARS,
    blank_ratio: float = BLANK_RATIO,
) -> dict | None:
    """按字数分布给出体检结果；不满足判定条件（书太小/章太少）返回 None。"""
    chapters = len(rows)
    if chapters <= 2 or chapters < min_chapters:
        return None
    total = sum(r["chars"] for r in rows)
    if total < min_total_chars:
        return None
    top = max(rows, key=lambda r: r["chars"])
    others = [r for r in rows if r["index"] != top["index"]]
    tiny_limit = max(tiny_abs_chars, total * TINY_BOOK_SHARE)
    tiny = [r for r in others if r["chars"] < tiny_limit]
    tiny_ratio = len(tiny) / len(others) if others else 0.0
    blank = [r for r in rows if r["blank"]]
    placeholder = [r for r in rows if r["placeholder"]]
    blank_ratio_value = (len(blank) + len(placeholder)) / chapters
    top_share = top["chars"] / total if total else 0.0

    if chapters >= MANY_CHAPTERS:
        major, severe = major_share, severe_share
        tiny_for_major, tiny_for_severe = TINY_RATIO_MAJOR, TINY_RATIO_SEVERE
    else:
        # 5—9 章：门槛收紧，避免把「上下卷各半」「正文 + 附录」误判成合并
        major, severe = few_major_share, few_severe_share
        tiny_for_major = tiny_for_severe = TINY_RATIO_FEW_CHAPTERS

    evidence = content_overlap_evidence(rows, top)
    missing = missing_catalog_titles(rows, catalog_titles)
    levels: list[str] = []
    if top_share >= severe and tiny_ratio >= tiny_for_severe:
        levels.append("严重")
    elif top_share >= major and tiny_ratio >= tiny_for_major:
        levels.append("中度")
    elif top_share >= major:
        levels.append("可疑")
    # 占比没到阈值，但最大章里确实混入了其他章的正文/章名 → 也是合并
    if (
        top_share >= EVIDENCE_SHARE_FLOOR
        and (
            evidence["duplicated_chapters"] >= EVIDENCE_DUP_CHAPTERS
            or evidence["leaked_names"] >= EVIDENCE_LEAKED_NAMES
        )
    ):
        levels.append("中度")
    if missing:
        levels.append("章节缺失")
    if not levels and blank_ratio_value >= blank_ratio:
        levels.append("多章无正文")
    if not levels:
        return None

    return {
        "level": min(levels, key=lambda lv: LEVEL_ORDER[lv]),
        "chapters": chapters,
        "total": total,
        "top_index": top["index"],
        "top_name": top["name"],
        "top_chars": top["chars"],
        "top_share": top_share,
        "tiny": len(tiny),
        "tiny_ratio": tiny_ratio,
        "blank": len(blank),
        "placeholder": len(placeholder),
        "blank_ratio": blank_ratio_value,
        "leaked_names": evidence["leaked_names"],
        "duplicated_chapters": evidence["duplicated_chapters"],
        "bookend_top": is_bookend_title(top["name"]),
        "missing": len(missing),
        "missing_chapters": missing[:20],
    }


def content_overlap_evidence(rows: list[dict], top: dict) -> dict:
    """交叉印证：最大章正文里是否混入了其他章名 / 其他章正文片段。"""
    leaked_names = 0
    duplicated = 0
    for row in rows:
        if row["index"] == top["index"]:
            continue
        key = normalize_text(row["name"])
        if len(key) >= EVIDENCE_NAME_MIN_LEN and key in top["text"]:
            leaked_names += 1
        if row["chars"] < EVIDENCE_WINDOW * 3:
            continue
        if row["chars"] < EVIDENCE_DUP_MIN_CHARS:
            continue
        windows = [
            row["text"][pos:pos + EVIDENCE_WINDOW]
            for pos in range(0, row["chars"] - EVIDENCE_WINDOW, EVIDENCE_STEP)
        ]
        if not windows:
            continue
        hits = sum(1 for w in windows if w in top["text"])
        if hits / len(windows) >= EVIDENCE_DUP_RATIO:
            duplicated += 1
    return {"leaked_names": leaked_names, "duplicated_chapters": duplicated}


def is_bookend_title(title: str) -> bool:
    """章名是否为书前/书末项（版权信息、目录、后记、附录、索引…）。"""
    return bool(_BOOKEND_TITLE_RE.match(normalize_text(title)))


def appears_unquoted(text: str, key: str) -> bool:
    """key 是否以「非引用」形式出现在正文里（《key》只算文献引用）。"""
    if not key or key not in text:
        return False
    start = 0
    while True:
        pos = text.find(key, start)
        if pos < 0:
            return False
        quoted = (
            pos > 0
            and text[pos - 1] == _QUOTE_OPEN
            and text[pos + len(key):pos + len(key) + 1] == _QUOTE_CLOSE
        )
        if not quoted:
            return True
        start = pos + len(key)


def missing_catalog_titles(rows: list[dict], catalog_titles) -> list[str]:
    """目录里有、导出章节里没有，且标题以非引用形式出现在某章正文中的条目。

    这类条目说明该章正文被并进了别的章（导出时漏切章），是「章节缺失」的信号。
    """
    if not catalog_titles:
        return []
    names = {normalize_text(r["name"]) for r in rows}
    texts = [r["text"] for r in rows]
    missing: list[str] = []
    for raw in catalog_titles:
        title = str(raw or "").strip()
        key = normalize_text(title)
        if len(key) < _CATALOG_TITLE_MIN_LEN or key in names:
            continue
        if is_bookend_title(title):
            continue
        if any(appears_unquoted(text, key) for text in texts):
            missing.append(title)
    return missing


def load_catalog_titles(catalog_path: Path | None) -> list[str]:
    """读取导出时抓到的目录 _catalog.json；没有返回 []。"""
    if not catalog_path or not catalog_path.is_file():
        return []
    try:
        titles = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [str(t) for t in titles] if isinstance(titles, list) else []


def analyze_book_json(
    path: Path, *, catalog_path: Path | None = None, **kwargs
) -> dict | None:
    """读取成品 JSON 并体检；格式不符返回 None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    body = data.get("body")
    if not isinstance(body, list):
        return None
    catalog_titles = kwargs.pop("catalog_titles", None)
    if catalog_titles is None:
        catalog_titles = load_catalog_titles(catalog_path)
    result = analyze_rows(
        chapter_rows(body), catalog_titles=catalog_titles, **kwargs
    )
    if result is None:
        return None
    result["book_id"] = path.name.split("_", 1)[0]
    result["title"] = str(data.get("title") or "")
    result["author"] = str(data.get("author") or "")
    result["source"] = str(path)
    return result


def analyze_chapter_mds(chapter_dir: Path, **kwargs) -> dict | None:
    """体检中间产物目录 output/<id>/chapters 下的 md（用于没有成品 JSON 的书）。"""
    paths = sorted(chapter_dir.glob("*.md"))
    if not paths:
        return None
    body = []
    title = ""
    for path in paths:
        lines = []
        name = ""
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("#") and not name:
                name = line.lstrip("#").strip()
                continue
            lines.append(line)
        title = title or name
        body.append({"chapter_name": name, "content": "\n".join(lines)})
    result = analyze_rows(chapter_rows(body), **kwargs)
    if result is None:
        return None
    result["book_id"] = chapter_dir.parent.name
    result["title"] = title
    result["author"] = ""
    result["source"] = str(chapter_dir)
    return result


def iter_json_books(books_dir: Path) -> Iterator[Path]:
    if not books_dir.is_dir():
        return
    for path in sorted(books_dir.glob("*.json")):
        if path.is_file():
            yield path


def run(
    *,
    books_dir: Path = BOOKS_DIR,
    output_root: Path | None = None,
    catalog_root: Path | None = Path("output"),
    include_suspect: bool = False,
    **kwargs,
) -> dict:
    """扫描 books_dir（可选叠加 output 中间产物与目录），返回体检结果。"""
    findings: list[dict] = []
    scanned = 0
    skipped_small = 0
    seen_ids: set[str] = set()
    for path in iter_json_books(books_dir):
        book_id = path.name.split("_", 1)[0]
        catalog_path = (
            Path(catalog_root) / book_id / "_catalog.json"
            if catalog_root is not None
            else None
        )
        result = analyze_book_json(path, catalog_path=catalog_path, **kwargs)
        seen_ids.add(book_id)
        scanned += 1
        if result is None:
            skipped_small += 1
            continue
        findings.append(result)
    if output_root is not None:
        for chapter_dir in sorted(Path(output_root).glob("*/chapters")):
            book_id = chapter_dir.parent.name
            if book_id in seen_ids:
                continue
            result = analyze_chapter_mds(chapter_dir, **kwargs)
            scanned += 1
            if result is None:
                skipped_small += 1
                continue
            findings.append(result)
    if not include_suspect:
        findings = [f for f in findings if f["level"] != "可疑"]
    findings.sort(key=lambda f: (LEVEL_ORDER[f["level"]], -f["top_share"]))
    return {
        "scanned": scanned,
        "skipped": skipped_small,
        "findings": findings,
    }


def format_finding(item: dict) -> str:
    evidence = []
    if item["bookend_top"]:
        evidence.append("最大章为书前/书末项")
    if item.get("missing"):
        evidence.append(f"目录缺失{int(item['missing'])}章并入他章")
    if item["leaked_names"]:
        evidence.append(f"混入其他章名{item['leaked_names']}个")
    if item["duplicated_chapters"]:
        evidence.append(f"其他章正文重复{item['duplicated_chapters']}章")
    if item["blank"]:
        evidence.append(f"空章{item['blank']}个")
    if item["placeholder"]:
        evidence.append(f"标题占位章{item['placeholder']}个")
    evidence_text = "、".join(evidence) or "—"
    return (
        f"[{item['level']}] {item['book_id']} {item['title'][:28]:28s} "
        f"章{item['chapters']:5d} 字{item['total']:7d} "
        f"最大章#{item['top_index']}/{item['chapters']}"
        f"「{item['top_name'][:16]}」占{item['top_share'] * 100:3.0f}% "
        f"其余小章{item['tiny']}({item['tiny_ratio'] * 100:3.0f}%) | {evidence_text}"
    )


def print_report(result: dict, *, limit: int = 0) -> None:
    findings = result["findings"]
    print(
        f"扫描 {result['scanned']} 本（章节数不足/字数过少跳过 {result['skipped']} 本），"
        f"命中 {len(findings)} 本"
    )
    if not findings:
        return
    counts: dict[str, int] = {}
    for item in findings:
        counts[item["level"]] = counts.get(item["level"], 0) + 1
    print("分档：" + "、".join(f"{k} {v} 本" for k, v in counts.items()))
    print()
    current = ""
    shown = 0
    for item in findings:
        if item["level"] != current:
            current = item["level"]
            print(f"== {current}")
            shown = 0
        if limit and shown >= limit:
            continue
        print("  " + format_finding(item))
        shown += 1


def write_tsv(path: Path, findings: list[dict]) -> None:
    header = (
        "级别\tbook_id\t书名\t章数\t总字数\t最大章序号\t最大章名\t占比\t"
        "其余小章数\t小章占比\t空章数\t占位章数\t最大章为书末项\t混入章名\t正文重复章"
        "\t目录缺失章\t来源\n"
    )
    lines = [
        "\t".join(
            [
                f["level"],
                f["book_id"],
                f["title"],
                str(f["chapters"]),
                str(f["total"]),
                str(f["top_index"]),
                f["top_name"],
                f"{f['top_share']:.3f}",
                str(f["tiny"]),
                f"{f['tiny_ratio']:.3f}",
                str(f["blank"]),
                str(f["placeholder"]),
                "是" if f["bookend_top"] else "否",
                str(f["leaked_names"]),
                str(f["duplicated_chapters"]),
                str(f.get("missing", 0)),
                f["source"],
            ]
        )
        for f in findings
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="体检已下载电子书：找出正文被合并进单一章节 / 多章无正文的书",
    )
    parser.add_argument(
        "--books-dir",
        type=Path,
        default=Path(BOOKS_DIR),
        help=f"成品 JSON 目录（默认 {BOOKS_DIR}）",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("output"),
        help="中间产物根目录，配 --include-output 使用（默认 output）",
    )
    parser.add_argument(
        "--catalog-root",
        type=Path,
        default=Path("output"),
        help="用 <root>/<book_id>/_catalog.json 交叉核对章节是否缺失（默认 output）",
    )
    parser.add_argument(
        "--include-output",
        action="store_true",
        help="同时体检 output/<id>/chapters 中间产物（仅限没有成品 JSON 的书）",
    )
    parser.add_argument(
        "--include-suspect",
        action="store_true",
        help="连「可疑」档一起列出（≥5 章且单章占比过半、但其余章不小）",
    )
    parser.add_argument("--min-chapters", type=int, default=MIN_CHAPTERS)
    parser.add_argument("--min-chars", type=int, default=MIN_TOTAL_CHARS)
    parser.add_argument("--major-share", type=float, default=MAJOR_SHARE)
    parser.add_argument("--severe-share", type=float, default=SEVERE_SHARE)
    parser.add_argument(
        "--few-major-share",
        type=float,
        default=FEW_CHAPTERS_MAJOR_SHARE,
        help="5—9 章时的单章占比门槛（默认 0.7）",
    )
    parser.add_argument(
        "--few-severe-share",
        type=float,
        default=FEW_CHAPTERS_SEVERE_SHARE,
        help="5—9 章时的严重档门槛（默认 0.9）",
    )
    parser.add_argument("--tiny-abs", type=int, default=TINY_ABS_CHARS)
    parser.add_argument("--blank-ratio", type=float, default=BLANK_RATIO)
    parser.add_argument("--limit", type=int, default=0, help="每档最多列出多少本（0=不限）")
    parser.add_argument("--tsv", type=Path, default=None, help="把结果写成 TSV")
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出结果（便于其它脚本消费）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(
        books_dir=args.books_dir,
        output_root=args.output_root if args.include_output else None,
        catalog_root=args.catalog_root,
        include_suspect=args.include_suspect,
        min_chapters=args.min_chapters,
        min_total_chars=args.min_chars,
        major_share=args.major_share,
        severe_share=args.severe_share,
        few_major_share=args.few_major_share,
        few_severe_share=args.few_severe_share,
        tiny_abs_chars=args.tiny_abs,
        blank_ratio=args.blank_ratio,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result, limit=args.limit)
    if args.tsv:
        write_tsv(args.tsv, result["findings"])
        print(f"\n已写出 {args.tsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
