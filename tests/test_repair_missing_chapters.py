"""repair_missing_chapters：按目录章名重切被并进邻章的缺失章。"""
import json
import tempfile
import unittest
from pathlib import Path

import repair_missing_chapters as rmc


CATALOG = ["版权信息", "甲篇正文", "乙篇正文", "丙篇正文"]
MERGED_BODY = (
    "甲篇正文\n\n甲篇自己的正文。\n\n"
    "乙篇正文\n\n乙篇的正文内容。\n\n"
    "丙篇正文\n\n丙篇的正文内容。\n"
)


def write_book_dir(root: Path, chapters: list[tuple[str, str]]) -> Path:
    """造 output/<id> 形状的中间产物；返回 book_dir。"""
    book_dir = root / "book"
    md_dir = book_dir / "chapters"
    raw_dir = book_dir / "raw"
    md_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i, (name, body) in enumerate(chapters, 1):
        (md_dir / f"{i:04d}.md").write_text(f"# {name}\n\n\n{body}", encoding="utf-8")
        (raw_dir / f"{i:04d}.json").write_text(
            json.dumps({"title": name, "images": [], "text_len": len(body)}),
            encoding="utf-8",
        )
    (book_dir / "_catalog.json").write_text(
        json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8"
    )
    return book_dir


def book_json(body: list[tuple[str, str]]) -> dict:
    return {
        "id": "abc123",
        "title": "测试书",
        "author": "某人",
        "press": "某社",
        "publication_date": "2020-01-01",
        "isbn": "9787000000000",
        "word_count": 0,
        "body": [
            {
                "chapter_name": name,
                "chapter_id": f"ch_{i:04d}",
                "content": content,
                "has_content": bool(content.strip()),
            }
            for i, (name, content) in enumerate(body, 1)
        ],
    }


class TestPlanBook(unittest.TestCase):
    def test_splits_merged_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            book_dir = write_book_dir(
                Path(tmp),
                [("版权信息", "书名：测试书"), ("甲篇正文", MERGED_BODY)],
            )
            plan = rmc.plan_book(
                "abc123",
                book_json([("版权信息", "书名：测试书"), ("甲篇正文", MERGED_BODY)]),
                rmc.read_md_files(book_dir / "chapters"),
                CATALOG,
            )
            self.assertIsNotNone(plan)
            self.assertTrue(plan["text_preserved"])
            self.assertEqual(plan["chapters_before"], 2)
            self.assertEqual(plan["chapters_after"], 4)
            self.assertEqual([c["title"] for c in plan["cuts"]], ["乙篇正文", "丙篇正文"])
            self.assertEqual(
                [c["name"] for c in plan["chapters"]],
                ["版权信息", "甲篇正文", "乙篇正文", "丙篇正文"],
            )

    def test_healthy_book_has_no_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            chapters = [
                ("版权信息", "书名：测试书"),
                ("甲篇正文", "甲篇自己的正文。"),
                ("乙篇正文", "乙篇的正文内容。"),
                ("丙篇正文", "丙篇的正文内容。"),
            ]
            book_dir = write_book_dir(Path(tmp), chapters)
            plan = rmc.plan_book(
                "abc123", book_json(chapters),
                rmc.read_md_files(book_dir / "chapters"), CATALOG,
            )
            self.assertIsNone(plan)

    def test_inline_mention_is_not_a_cut(self):
        """标题只夹在句子中间时不切（missing 判定只认独立整行）。"""
        with tempfile.TemporaryDirectory() as tmp:
            body = "甲篇正文\n\n文中提到乙篇正文这一篇，但不是章首。\n"
            chapters = [("版权信息", "书名"), ("甲篇正文", body)]
            book_dir = write_book_dir(Path(tmp), chapters)
            plan = rmc.plan_book(
                "abc123", book_json(chapters),
                rmc.read_md_files(book_dir / "chapters"), CATALOG,
            )
            self.assertIsNone(plan)

    def test_empty_placeholder_chapter_is_kept(self):
        """只有标题、没有正文的占位章要保留：它本就存在于成品里，不是切出来的。"""
        with tempfile.TemporaryDirectory() as tmp:
            chapters = [
                ("版权信息", "书名：测试书"),
                ("空章", ""),
                ("甲篇正文", MERGED_BODY),
            ]
            book_dir = write_book_dir(Path(tmp), chapters)
            plan = rmc.plan_book(
                "abc123", book_json(chapters),
                rmc.read_md_files(book_dir / "chapters"), CATALOG,
            )
            self.assertTrue(plan["text_preserved"])
            self.assertEqual(
                [c["name"] for c in plan["chapters"]],
                ["版权信息", "空章", "甲篇正文", "乙篇正文", "丙篇正文"],
            )


class TestApplyPlan(unittest.TestCase):
    def test_apply_writes_json_and_keeps_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_dir = write_book_dir(
                root, [("版权信息", "书名：测试书"), ("甲篇正文", MERGED_BODY)]
            )
            out_dir = root / "books"
            out_dir.mkdir()
            old_book = book_json(
                [("版权信息", "书名：测试书"), ("甲篇正文", MERGED_BODY)]
            )
            (out_dir / "abc123_测试书.json").write_text(
                json.dumps(old_book, ensure_ascii=False), encoding="utf-8"
            )
            plan = rmc.plan_book(
                "abc123", old_book, rmc.read_md_files(book_dir / "chapters"), CATALOG
            )
            rmc.apply_plan(
                "abc123", plan, old_book, book_dir=book_dir, out_dir=out_dir
            )
            written = json.loads((out_dir / "abc123_测试书.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [c["chapter_name"] for c in written["body"]],
                ["版权信息", "甲篇正文", "乙篇正文", "丙篇正文"],
            )
            self.assertIn("乙篇的正文内容", written["body"][2]["content"])
            # 元数据保留
            self.assertEqual(written["press"], "某社")
            self.assertEqual(written["isbn"], "9787000000000")
            # 中间产物同步 + 备份
            self.assertEqual(len(list((book_dir / "chapters").glob("*.md"))), 4)
            self.assertTrue(list((book_dir / "backups").glob("*/chapters")))
            self.assertTrue(
                any((book_dir / "backups").glob(f"*/{written['title']}_*.json"))
                or list((book_dir / "backups").glob("*/abc123_*.json"))
            )


if __name__ == "__main__":
    unittest.main()
