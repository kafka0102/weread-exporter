import unittest

from fetch_shelf import (
    accumulate_dom_books,
    apply_existing_authors,
    books_missing_author,
    collect_books_from_json,
    extract_book_id_from_href,
    format_book_line,
    is_reader_book_id,
    merge_books,
    sanitize_csv_field,
    write_shelf_books,
    _total_unique,
)


class TestExtractBookIdFromHref(unittest.TestCase):
    def test_hash_reader_url(self):
        self.assertEqual(
            extract_book_id_from_href("#/reader/d31323b0813abaf26g0137c2"),
            "d31323b0813abaf26g0137c2")

    def test_full_web_url(self):
        self.assertEqual(
            extract_book_id_from_href("https://weread.qq.com/web/reader/abc123"),
            "abc123")

    def test_with_query_string(self):
        self.assertEqual(
            extract_book_id_from_href("https://weread.qq.com/#/reader/abc123?from=shelf"),
            "abc123")

    def test_non_reader_url(self):
        self.assertEqual(extract_book_id_from_href("https://weread.qq.com/web/shelf"), "")

    def test_empty_and_none(self):
        self.assertEqual(extract_book_id_from_href(""), "")
        self.assertEqual(extract_book_id_from_href(None), "")


class TestCollectBooksFromJson(unittest.TestCase):
    def test_nested_book_list(self):
        data = {"data": {"books": [
            {"bookId": "1", "title": "书一", "author": "作者甲"},
            {"bookId": "2", "title": "书二", "author": "作者乙"},
        ]}}
        out = collect_books_from_json(data)
        self.assertEqual(out["1"], {"title": "书一", "author": "作者甲"})
        self.assertEqual(out["2"], {"title": "书二", "author": "作者乙"})

    def test_book_id_snake_case(self):
        out = collect_books_from_json({"book_id": "9", "title": "X", "author": "Y"})
        self.assertEqual(out["9"], {"title": "X", "author": "Y"})

    def test_field_name_variants(self):
        out = collect_books_from_json({"bookId": "1", "bookName": "N", "authorName": "A"})
        self.assertEqual(out["1"], {"title": "N", "author": "A"})

    def test_ignores_non_book_objects(self):
        out = collect_books_from_json({"foo": [{"bar": 1}], "config": {"x": 1}})
        self.assertEqual(out, {})

    def test_accumulate_into_existing(self):
        out = {"1": {"title": "旧", "author": ""}}
        collect_books_from_json({"bookId": "2", "title": "新", "author": "Z"}, out)
        self.assertIn("1", out)
        self.assertEqual(out["2"]["author"], "Z")

    def test_empty(self):
        self.assertEqual(collect_books_from_json(None), {})
        self.assertEqual(collect_books_from_json([]), {})



    def test_does_not_clobber_title_with_empty_progress_entry(self):
        out = {}
        collect_books_from_json(
            {"bookId": "3300154203", "title": "古诗", "author": "张三"}, out)
        collect_books_from_json(
            {"bookId": "3300154203", "readUpdateTime": 1}, out)
        self.assertEqual(out["3300154203"], {"title": "古诗", "author": "张三"})

    def test_fills_missing_fields_on_later_full_entry(self):
        out = {}
        collect_books_from_json({"bookId": "1", "readUpdateTime": 1}, out)
        collect_books_from_json(
            {"bookId": "1", "title": "书", "author": "甲"}, out)
        self.assertEqual(out["1"], {"title": "书", "author": "甲"})


class TestAccumulateDomBooks(unittest.TestCase):
    def test_accumulates_unique_reader_ids(self):
        bid1 = "6b632d60813abb28bg015b4f"
        bid2 = "4f3328d0813abbac2g0186d4"
        acc = accumulate_dom_books({}, [{"id": bid1, "title": "A", "author": ""}])
        acc = accumulate_dom_books(acc, [
            {"id": bid1, "title": "A", "author": "甲"},
            {"id": bid2, "title": "B", "author": ""},
            {"id": "3300215708", "title": "数字id应丢弃", "author": ""},
        ])
        self.assertEqual(set(acc), {bid1, bid2})
        self.assertEqual(acc[bid1]["author"], "甲")
        self.assertEqual(acc[bid2]["title"], "B")


class TestTotalUnique(unittest.TestCase):
    def test_counts_numeric_api_ids(self):
        bid = "6b632d60813abb28bg015b4f"
        dom = [{"id": bid, "title": "A", "author": ""}]
        api = {"3300215708": {"title": "B", "author": "乙"}, bid: {"title": "A", "author": "甲"}}
        # DOM 1 + API 数字 id 1 + 与 DOM 重复的 reader id 不双计 = 2
        self.assertEqual(_total_unique(dom, api), 2)

    def test_api_only_numeric_counts(self):
        api = {"1": {"title": "A", "author": ""}, "2": {"title": "B", "author": ""}}
        self.assertEqual(_total_unique([], api), 2)

class TestIsReaderBookId(unittest.TestCase):
    def test_accepts_long_alphanumeric_ids(self):
        self.assertTrue(is_reader_book_id("6b632d60813abb28bg015b4f"))
        self.assertTrue(is_reader_book_id("4f3328d0813abbac2g0186d4"))
        self.assertTrue(is_reader_book_id("d31323b0813abaf26g0137c2"))

    def test_rejects_pure_numeric_ids(self):
        self.assertFalse(is_reader_book_id("3300215708"))
        self.assertFalse(is_reader_book_id("937571"))
        self.assertFalse(is_reader_book_id("1"))

    def test_rejects_empty_or_short(self):
        self.assertFalse(is_reader_book_id(""))
        self.assertFalse(is_reader_book_id(None))
        self.assertFalse(is_reader_book_id("abc123"))  # too short


class TestMergeBooks(unittest.TestCase):
    def test_api_preferred_for_title_and_author(self):
        bid = "6b632d60813abb28bg015b4f"
        dom = [{"id": bid, "title": "DOM脏标题", "author": ""}]
        api = {bid: {"title": "干净标题", "author": "作者甲"}}
        self.assertEqual(merge_books(dom, api),
                         [{"id": bid, "title": "干净标题", "author": "作者甲"}])

    def test_dom_fallback_when_no_api(self):
        bid = "6b632d60813abb28bg015b4f"
        dom = [{"id": bid, "title": "DOM标题", "author": ""}]
        self.assertEqual(merge_books(dom, {}),
                         [{"id": bid, "title": "DOM标题", "author": ""}])

    def test_api_only_books_appended(self):
        bid1 = "6b632d60813abb28bg015b4f"
        bid2 = "4f3328d0813abbac2g0186d4"
        dom = [{"id": bid1, "title": "A", "author": ""}]
        api = {bid1: {"title": "A", "author": "甲"},
               bid2: {"title": "B", "author": "乙"}}
        merged = merge_books(dom, api)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["id"], bid1)
        self.assertEqual(merged[1], {"id": bid2, "title": "B", "author": "乙"})

    def test_dedup_dom_duplicates(self):
        bid = "6b632d60813abb28bg015b4f"
        dom = [{"id": bid, "title": "A", "author": ""},
               {"id": bid, "title": "A", "author": ""}]
        self.assertEqual(merge_books(dom, {}),
                         [{"id": bid, "title": "A", "author": ""}])

    def test_drops_pure_numeric_ids(self):
        real = "6b632d60813abb28bg015b4f"
        dom = [{"id": real, "title": "静心诗词", "author": ""}]
        api = {
            real: {"title": "静心诗词", "author": ""},
            "3300215708": {"title": "静心诗词", "author": "拾木文化"},
        }
        merged = merge_books(dom, api)
        self.assertEqual(merged, [
            {"id": real, "title": "静心诗词", "author": "拾木文化"},
        ])

    def test_numeric_only_api_book_not_kept(self):
        api = {"3300215708": {"title": "静心诗词", "author": "拾木文化"}}
        self.assertEqual(merge_books([], api), [])

    def test_empty_inputs(self):
        self.assertEqual(merge_books([], {}), [])

    def test_skips_dom_without_id(self):
        dom = [{"id": "", "title": "x"}, {"title": "y"}]
        self.assertEqual(merge_books(dom, {}), [])




class TestBooksMissingAuthor(unittest.TestCase):
    def test_filters_empty_and_whitespace(self):
        books = [
            {"id": "1", "title": "A", "author": "甲"},
            {"id": "2", "title": "B", "author": ""},
            {"id": "3", "title": "C", "author": "  "},
            {"id": "4", "title": "D"},
        ]
        missing = books_missing_author(books)
        self.assertEqual([b["id"] for b in missing], ["2", "3", "4"])

    def test_preserves_order_and_identity(self):
        books = [
            {"id": "1", "title": "A", "author": ""},
            {"id": "2", "title": "B", "author": "乙"},
            {"id": "3", "title": "C", "author": ""},
        ]
        missing = books_missing_author(books)
        self.assertIs(missing[0], books[0])
        self.assertIs(missing[1], books[2])




class TestApplyExistingAuthors(unittest.TestCase):
    def test_fills_only_empty(self):
        books = [
            {"id": "1", "title": "A", "author": ""},
            {"id": "2", "title": "B", "author": "新"},
        ]
        apply_existing_authors(books, {"1": "旧甲", "2": "旧乙"})
        self.assertEqual(books[0]["author"], "旧甲")
        self.assertEqual(books[1]["author"], "新")



class TestShelfTxtFormat(unittest.TestCase):
    def test_sanitize_replaces_commas(self):
        self.assertEqual(sanitize_csv_field("甲,乙"), "甲 乙")
        self.assertEqual(sanitize_csv_field(""), "")
        self.assertEqual(sanitize_csv_field(None), "")

    def test_format_book_line(self):
        line = format_book_line({
            "id": "1",
            "title": "书,名",
            "author": "作,者",
        })
        self.assertEqual(line, "1,书 名,作 者")

    def test_write_shelf_books(self):
        import tempfile
        import os
        books = [
            {"id": "1", "title": "A,B", "author": "C"},
            {"id": "2", "title": "D", "author": "E,F"},
        ]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "shelf_books.txt")
            write_shelf_books(path, books)
            with open(path, encoding="utf-8") as f:
                content = f.read()
        self.assertEqual(content, "1,A B,C\n2,D,E F\n")



if __name__ == "__main__":
    unittest.main()
