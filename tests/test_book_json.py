import json
import math
import tempfile
import unittest
from pathlib import Path

import book_json


class TestBookJsonHelpers(unittest.TestCase):
    def test_safe_filename_and_book_json_name(self):
        self.assertEqual(
            book_json.book_json_filename("abc123", '词学/十讲?'),
            "abc123_词学_十讲.json",
        )
        self.assertTrue(book_json.book_json_filename("id", "").endswith("_untitled.json"))

    def test_book_json_exists_by_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "bid_旧书名.json").write_text("{}", encoding="utf-8")
            self.assertTrue(book_json.book_json_exists(root, "bid"))
            self.assertFalse(book_json.book_json_exists(root, "other"))
            self.assertEqual(
                book_json.find_existing_book_json(root, "bid").name,
                "bid_旧书名.json",
            )

    def test_md_to_plain_content_strips_title_images_and_keeps_paragraphs(self):
        md = "# 第一章\n\n第一段内容。\n\n![图](images/a.jpg)\n\n第二段**加粗**内容。\n"
        plain = book_json.md_to_plain_content(md, chapter_name="第一章")
        self.assertEqual(plain, "第一段内容。\n\n第二段加粗内容。")
        self.assertNotIn("第一章", plain)
        self.assertNotIn("images/", plain)
        self.assertNotIn("**", plain)

    def test_chapter_entry_has_content_flag(self):
        filled = book_json.chapter_entry("绪论", 1, "有字")
        empty = book_json.chapter_entry("封面", 2, "  \n")
        self.assertEqual(filled["chapter_id"], "ch_0001")
        self.assertTrue(filled["has_content"])
        self.assertEqual(empty["chapter_id"], "ch_0002")
        self.assertFalse(empty["has_content"])
        self.assertEqual(empty["content"], "")

    def test_build_book_json_word_count_and_keys(self):
        book = book_json.build_book_json(
            book_id="e98326f0",
            title="续词品",
            author="杨夔生",
            chapters=[
                {"chapter_name": "封面", "content": ""},
                {"chapter_name": "正文", "content": "一二三四五"},
            ],
        )
        self.assertEqual(
            set(book.keys()),
            {"id", "title", "author", "press", "publication_date", "isbn", "word_count", "body"},
        )
        self.assertEqual(book["press"], "")
        self.assertEqual(book["publication_date"], "")
        self.assertEqual(book["isbn"], "")
        self.assertEqual(book["word_count"], 5)
        self.assertEqual(book["body"][0]["chapter_id"], "ch_0001")
        self.assertFalse(book["body"][0]["has_content"])
        self.assertTrue(book["body"][1]["has_content"])

    def test_build_from_export_dir_and_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            book_dir = root / "output" / "bid"
            md_dir = book_dir / "chapters"
            raw_dir = book_dir / "raw"
            md_dir.mkdir(parents=True)
            raw_dir.mkdir(parents=True)
            (md_dir / "0001.md").write_text("# 绪论\n\n这是绪论正文。\n", encoding="utf-8")
            (raw_dir / "0001.json").write_text(
                json.dumps({"title": "绪论", "images": [], "text_len": 6}, ensure_ascii=False),
                encoding="utf-8",
            )
            (md_dir / "0002.md").write_text("# 空章\n\n", encoding="utf-8")
            (raw_dir / "0002.json").write_text(
                json.dumps({"title": "空章", "images": [], "text_len": 0}, ensure_ascii=False),
                encoding="utf-8",
            )

            pairs = book_json.load_chapters_from_export_dir(book_dir)
            self.assertEqual([t for t, _ in pairs], ["绪论", "空章"])
            book = book_json.build_book_json_from_chapter_mds(
                book_id="bid",
                title="测试书",
                author="作者",
                chapter_files=pairs,
            )
            out_dir = root / "books"
            path = book_json.write_book_json(out_dir, book)
            self.assertEqual(path.name, "bid_测试书.json")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["body"][0]["content"], "这是绪论正文。")
            self.assertTrue(loaded["body"][0]["has_content"])
            self.assertFalse(loaded["body"][1]["has_content"])
            self.assertEqual(loaded["word_count"], len("这是绪论正文。"))

    def test_chapter_sleep_seconds_formula(self):
        self.assertEqual(book_json.chapter_sleep_seconds(0), 2.0)
        self.assertEqual(book_json.chapter_sleep_seconds(1), 2.0)
        self.assertEqual(book_json.chapter_sleep_seconds(2000), 2.0)
        self.assertEqual(book_json.chapter_sleep_seconds(2001), 4.0)
        self.assertEqual(book_json.chapter_sleep_seconds(5000), 6.0)
        self.assertEqual(book_json.chapter_sleep_seconds(30000), 15.0)  # clamp max
        self.assertEqual(
            book_json.chapter_sleep_seconds(3000, per_2k=2.0, min_seconds=2.0, max_seconds=15.0),
            4.0,
        )

    def test_batch_list_and_filter_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lst = root / "new_books.txt"
            lst.write_text(
                "a1,书A,作者A\n"
                "a2,书B,作者B\n"
                "# comment\n"
                "a1,书A重复,作者A\n",
                encoding="utf-8",
            )
            books = book_json.iter_batch_book_ids(lst)
            self.assertEqual([b[0] for b in books], ["a1", "a2"])
            out = root / "books"
            out.mkdir()
            (out / "a1_书A.json").write_text("{}", encoding="utf-8")
            pending = book_json.filter_pending_books(books, out)
            self.assertEqual([b[0] for b in pending], ["a2"])


    def test_write_book_json_replaces_old_prefix_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = root / "bid_旧名.json"
            old.write_text("{}", encoding="utf-8")
            book = book_json.build_book_json(
                book_id="bid", title="新名", author="A",
                chapters=[{"chapter_name": "c", "content": "字"}],
            )
            path = book_json.write_book_json(root, book)
            self.assertEqual(path.name, "bid_新名.json")
            self.assertTrue(path.exists())
            self.assertFalse(old.exists())


if __name__ == "__main__":
    unittest.main()
