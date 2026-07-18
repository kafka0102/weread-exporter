import unittest

from fetch_shelf import collect_books_from_json, extract_book_id_from_href, merge_books


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


class TestMergeBooks(unittest.TestCase):
    def test_api_preferred_for_title_and_author(self):
        dom = [{"id": "1", "title": "DOM脏标题", "author": ""}]
        api = {"1": {"title": "干净标题", "author": "作者甲"}}
        self.assertEqual(merge_books(dom, api),
                         [{"id": "1", "title": "干净标题", "author": "作者甲"}])

    def test_dom_fallback_when_no_api(self):
        dom = [{"id": "1", "title": "DOM标题", "author": ""}]
        self.assertEqual(merge_books(dom, {}),
                         [{"id": "1", "title": "DOM标题", "author": ""}])

    def test_api_only_books_appended(self):
        dom = [{"id": "1", "title": "A", "author": ""}]
        api = {"1": {"title": "A", "author": "甲"},
               "2": {"title": "B", "author": "乙"}}
        merged = merge_books(dom, api)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["id"], "1")
        self.assertEqual(merged[1], {"id": "2", "title": "B", "author": "乙"})

    def test_dedup_dom_duplicates(self):
        dom = [{"id": "1", "title": "A", "author": ""},
               {"id": "1", "title": "A", "author": ""}]
        self.assertEqual(merge_books(dom, {}),
                         [{"id": "1", "title": "A", "author": ""}])

    def test_empty_inputs(self):
        self.assertEqual(merge_books([], {}), [])

    def test_skips_dom_without_id(self):
        dom = [{"id": "", "title": "x"}, {"title": "y"}]
        self.assertEqual(merge_books(dom, {}), [])


if __name__ == "__main__":
    unittest.main()
