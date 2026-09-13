"""repair_empty_chapters.py 的纯函数与 CLI 单测（不依赖真实浏览器）。"""
from __future__ import annotations

import json

import pytest

from repair_empty_chapters import (
    classify_weread_source,
    find_empty_leaf_chapters,
    load_shelf_ids,
    parse_args,
    scan_all,
    scan_book_json,
    update_book_json_content,
    write_json_atomic,
    backup_book_json,
)


def make_book(body, *, book_id="abc123def456ghi789", title="测试书", author="作者"):
    return {
        "id": book_id,
        "title": title,
        "author": author,
        "press": "",
        "publication_date": "",
        "isbn": "",
        "word_count": 0,
        "body": body,
    }


def chapter(name, content, cid=None):
    return {
        "chapter_name": name,
        "chapter_id": cid or f"ch_{len(content):04d}",
        "content": content,
        "has_content": bool(content),
    }


class TestShelfIds:
    def test_load_ignores_comments_and_blank(self, tmp_path):
        p = tmp_path / "shelf.txt"
        p.write_text("# comment\n\nid1,书名,作者\nid2\n", encoding="utf-8")
        assert load_shelf_ids(p) == {"id1", "id2"}

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_shelf_ids(tmp_path / "nope.txt") == set()


class TestClassifySource:
    def test_source_url_excluded(self, tmp_path):
        book = {"source_url": "https://www.shidianguji.com/x", "body": []}
        ok, reason = classify_weread_source(book, "abc", set(), tmp_path)
        assert not ok and reason == "source_url"

    def test_kind_excluded(self, tmp_path):
        book = {"kind": "诗话", "body": []}
        ok, reason = classify_weread_source(book, "abc", set(), tmp_path)
        assert not ok and reason == "kind"

    def test_shelf_id_included(self, tmp_path):
        book = {"id": "abc", "body": []}
        ok, reason = classify_weread_source(book, "abc", {"abc"}, tmp_path)
        assert ok and reason == "shelf"

    def test_catalog_included(self, tmp_path):
        cat_dir = tmp_path / "shelfid" 
        cat_dir.mkdir()
        (cat_dir / "_catalog.json").write_text("[]", encoding="utf-8")
        book = {"id": "shelfid", "body": []}
        ok, reason = classify_weread_source(book, "shelfid", set(), tmp_path)
        assert ok and reason == "catalog"

    def test_invalid_id_without_evidence_excluded(self, tmp_path):
        book = {"id": "pdwiki_qiezhong", "body": []}
        ok, reason = classify_weread_source(book, "pdwiki_qiezhong", set(), tmp_path)
        assert not ok and reason == "invalid-id"

    def test_unknown_valid_id_without_evidence_excluded(self, tmp_path):
        book = {"id": "abcdef1234567890", "body": []}
        ok, reason = classify_weread_source(book, "abcdef1234567890", set(), tmp_path)
        assert not ok and reason == "no-evidence"


class TestEmptyLeafDetection:
    def test_middle_empty_flagged_last_ignored(self):
        body = [
            chapter("第一章", "正文一"),
            chapter("第二章", ""),
            chapter("第三章", ""),
        ]
        result = find_empty_leaf_chapters(body)
        assert [i["index"] for i in result["issues"]] == [2]
        assert result["skipped"] is None

    def test_too_few_chapters_skipped(self):
        body = [chapter("一", ""), chapter("二", "x")]
        result = find_empty_leaf_chapters(body)
        assert result["skipped"] == "too-few-chapters"
        assert result["issues"] == []

    def test_front_matter_exempt_by_default_strict_includes(self):
        body = [
            chapter("版权信息", ""),
            chapter("正文", "有字"),
            chapter("末章", ""),
        ]
        assert find_empty_leaf_chapters(body)["issues"] == []
        issues = find_empty_leaf_chapters(body, strict=True)["issues"]
        assert [i["index"] for i in issues] == [1]

    def test_catalog_alignment(self):
        body = [
            chapter("版权信息", "出版社……"),
            chapter("关雎", ""),
            chapter("卷耳", "采采卷耳"),
            chapter("末章", ""),
        ]
        catalog = ["版权信息", "关雎", "卷耳", "末章"]
        result = find_empty_leaf_chapters(body, catalog_titles=catalog)
        assert [i["index"] for i in result["issues"]] == [2]
        assert result["missing"] == []

    def test_short_title_fuzzy_not_matched(self):
        body = [
            chapter("春", ""),
            chapter("花", "正文"),
            chapter("末章", ""),
        ]
        catalog = ["春色满园关不住", "花", "末章"]
        # body「春」不是目录叶子的合法对齐项 → 不报
        assert find_empty_leaf_chapters(body, catalog_titles=catalog)["issues"] == []
        # 无目录时按 body 判空
        assert [i["index"] for i in find_empty_leaf_chapters(body)["issues"]] == [1]

    def test_missing_catalog_entries_only_reference(self):
        body = [
            chapter("正文一", "有内容"),
            chapter("正文二", ""),
            chapter("末章", ""),
        ]
        catalog = ["正文一", "独立篇章", "正文二", "末章"]
        result = find_empty_leaf_chapters(body, catalog_titles=catalog)
        assert [i["index"] for i in result["issues"]] == [2]
        assert result["missing"] == ["独立篇章"]


class TestScanBookAndAll:
    def test_scan_book_json_no_body(self):
        item = scan_book_json({"id": "x", "title": "t", "body": "oops"})
        assert item["result"]["skipped"] == "no-body"

    def test_scan_all_filters_sources(self, tmp_path):
        books = tmp_path / "books"
        books.mkdir()
        shelf = tmp_path / "shelf.txt"
        shelf.write_text("base1,书架书,甲\n", encoding="utf-8")

        weread = make_book(
            [chapter("一", "x"), chapter("二", ""), chapter("三", "")],
            book_id="base1",
            title="书架书",
        )
        (books / "base1_书架书.json").write_text(
            json.dumps(weread, ensure_ascii=False), encoding="utf-8"
        )
        shidian = make_book(
            [chapter("一", ""), chapter("二", ""), chapter("三", "")],
            book_id="shidian",
            title="古籍",
        )
        shidian["source_url"] = "https://www.shidianguji.com/book/x"
        (books / "shidian_古籍.json").write_text(
            json.dumps(shidian, ensure_ascii=False), encoding="utf-8"
        )
        weird = make_book(
            [chapter("一", ""), chapter("二", ""), chapter("三", "")],
            book_id="pdwiki_weird",
            title="异常ID",
        )
        (books / "pdwiki_weird_异常ID.json").write_text(
            json.dumps(weird, ensure_ascii=False), encoding="utf-8"
        )

        report = scan_all(
            books_dir=books,
            shelf_path=shelf,
            catalog_root=tmp_path / "output",
            book_ids=[],
            min_chapters=3,
            strict=False,
        )
        assert report["stats"]["scanned"] == 3
        assert report["stats"]["weread"] == 1
        assert report["stats"]["non_weread"] == 2
        assert [f["book_id"] for f in report["findings"]] == ["base1"]

    def test_book_filter(self, tmp_path):
        books = tmp_path / "books"
        books.mkdir()
        shelf_lines = "base1,书一,甲\nbase2,书二,乙\n"
        shelf = tmp_path / "shelf.txt"
        shelf.write_text(shelf_lines, encoding="utf-8")
        for bid, title in (("base1", "书一"), ("base2", "书二")):
            book = make_book(
                [chapter("一", "x"), chapter("二", ""), chapter("三", "")],
                book_id=bid,
                title=title,
            )
            (books / f"{bid}_{title}.json").write_text(
                json.dumps(book, ensure_ascii=False), encoding="utf-8"
            )
        report = scan_all(
            books_dir=books,
            shelf_path=shelf,
            catalog_root=tmp_path / "output",
            book_ids=["base2"],
            min_chapters=3,
            strict=False,
        )
        assert [f["book_id"] for f in report["findings"]] == ["base2"]


class TestUpdateContent:
    def test_only_content_changed_and_word_count_recomputed(self):
        book = make_book(
            [
                chapter("一", "abc", cid="ch_0001"),
                chapter("二", "", cid="ch_0002"),
                chapter("三", "def", cid="ch_0003"),
            ]
        )
        updated = update_book_json_content(book, {2: "新内容"})
        body = updated["body"]
        assert body[1]["content"] == "新内容"
        assert body[1]["has_content"] is True
        assert body[1]["chapter_id"] == "ch_0002"
        assert body[1]["chapter_name"] == "二"
        assert body[0] == book["body"][0]
        assert body[2] == book["body"][2]
        assert updated["word_count"] == 3 + 3 + 3
        # 原 book 未被就地修改
        assert book["body"][1]["content"] == ""

    def test_missing_id_preserved_when_not_updated(self):
        book = make_book([chapter("一", "x"), chapter("二", "y"), chapter("三", "z")])
        updated = update_book_json_content(book, {2: "yy"})
        assert [c["chapter_id"] for c in updated["body"]] == [
            c["chapter_id"] for c in book["body"]
        ]


class TestFileIo:
    def test_write_json_atomic(self, tmp_path):
        p = tmp_path / "a.json"
        write_json_atomic(p, {"k": "v"})
        assert json.loads(p.read_text(encoding="utf-8")) == {"k": "v"}
        assert not p.with_suffix(p.suffix + ".tmp").exists()

    def test_backup_book_json(self, tmp_path):
        p = tmp_path / "id_书.json"
        p.write_text("{}", encoding="utf-8")
        backup_dir = tmp_path / "bak"
        backup = backup_book_json(p, backup_dir)
        assert backup.is_file()
        assert backup.read_text(encoding="utf-8") == "{}"


class TestCli:
    def test_apply_requires_prefer_screen(self):
        with pytest.raises(SystemExit):
            parse_args(["--apply"])

    def test_default_is_dry_run(self):
        args = parse_args([])
        assert args.apply is False
        assert args.min_chapters == 3

    def test_dry_run_flag_accepted(self):
        args = parse_args(["--dry-run", "--book", "x1"])
        assert args.apply is False
        assert args.book_ids == ["x1"]

    def test_dry_run_and_apply_conflict(self):
        with pytest.raises(SystemExit):
            parse_args(["--dry-run", "--apply"])
