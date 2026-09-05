import json
import tempfile
import unittest
from pathlib import Path

from dedupe_shelf_books import (
    classify_book,
    load_downloaded_ids,
    load_ebook_title_keys,
    load_forbid_ids,
    migrate_downloaded_from_new,
    migrate_stale_from_dup,
    normalize_title,
    parse_shelf_line,
    run,
)


class TestNormalizeTitle(unittest.TestCase):
    def test_strip_fullwidth_parens(self):
        self.assertEqual(
            normalize_title("杜甫诗歌鉴赏辞典（珍藏本）"),
            "杜甫诗歌鉴赏辞典",
        )

    def test_strip_halfwidth_parens(self):
        self.assertEqual(
            normalize_title("纳兰词(插图注释版 全二册)"),
            "纳兰词",
        )

    def test_strip_series_brand_parens(self):
        self.assertEqual(
            normalize_title("骆玉明古诗词课（读客三个圈经典文库）"),
            "骆玉明古诗词课",
        )

    def test_strip_colon_subtitle(self):
        self.assertEqual(
            normalize_title("长安诗酒汴京花：全二册"),
            "长安诗酒汴京花",
        )
        self.assertEqual(
            normalize_title("香尘灭：宋词与宋人"),
            "香尘灭",
        )

    def test_strip_dash_subtitle(self):
        self.assertEqual(
            normalize_title("风止意难平——藏在古诗词里的遗憾"),
            "风止意难平",
        )
        self.assertEqual(
            normalize_title("唐诗选-上下"),
            "唐诗选",
        )

    def test_parens_then_subtitle_equivalent(self):
        a = normalize_title("长安诗酒汴京花：全二册")
        b = normalize_title("长安诗酒汴京花（全二册）")
        self.assertEqual(a, b)
        self.assertEqual(a, "长安诗酒汴京花")

    def test_multiple_parens(self):
        self.assertEqual(
            normalize_title("古诗十九首·玉台新咏（中华经典藏书）"),
            "古诗十九首玉台新咏",
        )

    def test_whitespace_and_empty(self):
        self.assertEqual(normalize_title("  词  品  "), "词品")
        self.assertEqual(normalize_title(""), "")
        self.assertEqual(normalize_title(None), "")

    def test_same_main_title_different_decoration(self):
        self.assertEqual(
            normalize_title("词品（中华经典名著全本全注全译丛书）"),
            normalize_title("词品"),
        )

    def test_keep_journal_issue_parens(self):
        a = normalize_title("励耘学刊（2019年第2辑/总第30辑）")
        b = normalize_title("励耘学刊（2024年第1辑/总第39辑）")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("励耘学刊"))
        self.assertIn("2019", a)
        self.assertIn("2", a)
        self.assertIn("2024", b)
        self.assertEqual(
            normalize_title("文学评论丛刊（第15卷第2期）"),
            normalize_title("文学评论丛刊(第15卷第2期)"),
        )
        self.assertIn("15", normalize_title("文学评论丛刊（第15卷第2期）"))
        self.assertIn("2", normalize_title("某学刊（第3本第2期）"))

    def test_keep_yearbook_year_parens(self):
        self.assertNotEqual(
            normalize_title("唐代文学研究年鉴（2016）"),
            normalize_title("唐代文学研究年鉴（2023）"),
        )
        self.assertEqual(
            normalize_title("中国李白研究（2023年）"),
            normalize_title("中国李白研究(2023)"),
        )

    def test_issue_punctuation_and_numerals_equivalent(self):
        self.assertEqual(
            normalize_title("励耘学刊（2024年第1辑/总第39辑）"),
            normalize_title("励耘学刊（2024年第1辑 总第39辑）"),
        )
        self.assertEqual(
            normalize_title("励耘学刊（2020年第2辑·总第32辑）"),
            normalize_title("励耘学刊（2020年第2辑/总第32辑）"),
        )
        self.assertEqual(
            normalize_title("明清文学与文献（第三辑）"),
            normalize_title("明清文学与文献（第3辑）"),
        )
        self.assertEqual(
            normalize_title("乐府学（第十一辑）"),
            normalize_title("乐府学（第11辑）"),
        )
        self.assertEqual(
            normalize_title("励耘学刊（2018年第1辑/总第二十七辑）"),
            normalize_title("励耘学刊（2018年第1辑/总第27辑）"),
        )

    def test_still_strip_edition_and_set_parens(self):
        self.assertEqual(normalize_title("文学写作（第2版）"), "文学写作")
        self.assertEqual(normalize_title("草木缘情（第二版）"), "草木缘情")
        self.assertEqual(normalize_title("纳兰词(插图注释版 全二册)"), "纳兰词")
        self.assertEqual(normalize_title("诗吟天下（套装共2册）"), "诗吟天下")
        self.assertEqual(
            normalize_title("拾荒小集（聚学文丛三辑）"),
            "拾荒小集",
        )

    def test_keep_volume_then_strip_subtitle(self):
        self.assertEqual(
            normalize_title("讲给孩子的国学经典（第四册）：文集诗薮"),
            normalize_title("讲给孩子的国学经典（第4册）"),
        )
        self.assertEqual(
            normalize_title(
                "唐诗之路研究(第二辑)——中国唐诗之路研究会首届年会暨第二次学术研讨会论文集"
            ),
            normalize_title("唐诗之路研究（第2辑）"),
        )


class TestParseAndLoad(unittest.TestCase):
    def test_parse_shelf_line(self):
        self.assertEqual(
            parse_shelf_line("abc,书名,作者甲"),
            ("abc", "书名", "作者甲"),
        )
        self.assertIsNone(parse_shelf_line(""))
        self.assertIsNone(parse_shelf_line("# comment"))

    def test_load_forbid_ids(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "forbid.txt"
            p.write_text("# c\nid1\nid2,书,人\n\n", encoding="utf-8")
            self.assertEqual(load_forbid_ids(p), {"id1", "id2"})

    def test_load_downloaded_ids(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "bid1_某书.json").write_text("{}", encoding="utf-8")
            (d / "bid2_另一本.json").write_text("{}", encoding="utf-8")
            (d / "nounderscore.json").write_text("{}", encoding="utf-8")
            self.assertEqual(load_downloaded_ids(d), {"bid1", "bid2"})

    def test_load_ebook_title_keys(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ebook.json"
            p.write_text(
                json.dumps(
                    [
                        {"bookName": "词品（珍藏本）", "authorName": "杨慎"},
                        {"bookName": "香尘灭：宋词与宋人", "authorName": "李让眉"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            keys = load_ebook_title_keys(p)
            self.assertIn("词品", keys)
            self.assertIn("香尘灭", keys)


class TestClassify(unittest.TestCase):
    def test_priority_forbid(self):
        bucket, reason = classify_book(
            "x",
            "任意",
            forbid_ids={"x"},
            downloaded_ids=set(),
            ebook_keys={},
        )
        self.assertEqual((bucket, reason), ("dup", "forbid"))

    def test_priority_downloaded(self):
        bucket, reason = classify_book(
            "x",
            "任意",
            forbid_ids=set(),
            downloaded_ids={"x"},
            ebook_keys={},
        )
        self.assertEqual((bucket, reason), ("dup", "downloaded"))

    def test_ebook_title_only_ignores_author(self):
        # 同名异作者也算 dup
        bucket, reason = classify_book(
            "sid",
            "古今词话",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={"古今词话": [{"bookName": "古今词话", "authorName": "沈雄"}]},
        )
        self.assertEqual((bucket, reason), ("dup", "ebook-info"))

    def test_ebook_normalized_title_match(self):
        bucket, reason = classify_book(
            "sid",
            "长安诗酒汴京花：全二册",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={"长安诗酒汴京花": [{"bookName": "长安诗酒汴京花（全二册）"}]},
        )
        self.assertEqual((bucket, reason), ("dup", "ebook-info"))

    def test_new_when_no_match(self):
        bucket, reason = classify_book(
            "sid",
            "续词品",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={"词品": [{"bookName": "词品"}]},
        )
        self.assertEqual((bucket, reason), ("new", "new"))

    def test_shelf_title_dup(self):
        bucket, reason = classify_book(
            "sid2",
            "词品（珍藏本）",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={},
            seen_title_keys={"词品"},
        )
        self.assertEqual((bucket, reason), ("dup", "shelf-title-dup"))

    def test_shelf_title_dup_after_higher_priority(self):
        # forbid 优先于重名
        bucket, reason = classify_book(
            "sid",
            "词品",
            forbid_ids={"sid"},
            downloaded_ids=set(),
            ebook_keys={},
            seen_title_keys={"词品"},
        )
        self.assertEqual((bucket, reason), ("dup", "forbid"))


class TestMigrateAndRun(unittest.TestCase):
    def test_migrate_downloaded_from_new(self):
        with tempfile.TemporaryDirectory() as td:
            new_p = Path(td) / "new.txt"
            dup_p = Path(td) / "dup.txt"
            new_p.write_text(
                "a,书A,作者A\nb,书B,作者B\n",
                encoding="utf-8",
            )
            dup_p.write_text("", encoding="utf-8")
            moved = migrate_downloaded_from_new(
                new_p, dup_p, {"a"}, dry_run=False
            )
            self.assertEqual(len(moved), 1)
            self.assertEqual(new_p.read_text(encoding="utf-8").strip(), "b,书B,作者B")
            self.assertIn("a,书A,作者A", dup_p.read_text(encoding="utf-8"))

    def test_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            shelf = td_path / "shelf.txt"
            forbid = td_path / "forbid.txt"
            ebook = td_path / "ebook.json"
            dup = td_path / "dup.txt"
            new = td_path / "new.txt"
            books = td_path / "books"
            books.mkdir()

            shelf.write_text(
                "\n".join(
                    [
                        "idforbid,禁书,某人",
                        "iddl,已下载书,某人",
                        "idebook,长安诗酒汴京花：全二册,随园散人",
                        "idsame,古今词话,[宋]杨湜",
                        "idnew,续词品,杨夔生",
                        "iddupname,续词品（注释本）,另一人",
                        "idprocessed,旧书,人",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            forbid.write_text("idforbid\n", encoding="utf-8")
            (books / "iddl_已下载书.json").write_text("{}", encoding="utf-8")
            ebook.write_text(
                json.dumps(
                    [
                        {
                            "bookName": "长安诗酒汴京花（全二册）",
                            "authorName": "随园散人",
                        },
                        {"bookName": "古今词话", "authorName": "沈雄"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            dup.write_text("idprocessed,旧书,人\n", encoding="utf-8")
            new.write_text("", encoding="utf-8")

            result = run(
                shelf_path=shelf,
                forbid_path=forbid,
                ebook_path=ebook,
                dup_path=dup,
                new_path=new,
                books_dir=books,
                dry_run=False,
            )
            self.assertEqual(result["todo"], 6)
            # forbid+dl+ebook+ebook-same-title+shelf-title-dup
            self.assertEqual(result["dup"], 5)
            self.assertEqual(result["new"], 1)
            self.assertEqual(result["reasons"]["downloaded"], 1)
            self.assertEqual(result["reasons"]["shelf-title-dup"], 1)
            dup_text = dup.read_text(encoding="utf-8")
            new_text = new.read_text(encoding="utf-8")
            self.assertIn("idforbid", dup_text)
            self.assertIn("iddl", dup_text)
            self.assertIn("idebook", dup_text)
            self.assertIn("idsame", dup_text)
            self.assertIn("iddupname", dup_text)
            self.assertIn("idnew", new_text)
            self.assertNotIn("idnew", dup_text)


    def test_migrate_shelf_title_dup_within_new(self):
        with tempfile.TemporaryDirectory() as td:
            new_p = Path(td) / "new.txt"
            dup_p = Path(td) / "dup.txt"
            new_p.write_text(
                "a,词品,甲\nb,词品（珍藏本）,乙\n",
                encoding="utf-8",
            )
            dup_p.write_text("", encoding="utf-8")
            from dedupe_shelf_books import migrate_stale_from_new

            moved = migrate_stale_from_new(
                new_p,
                dup_p,
                forbid_ids=set(),
                downloaded_ids=set(),
                ebook_keys={},
                dry_run=False,
            )
            self.assertEqual(len(moved), 1)
            self.assertEqual(moved[0][1], "shelf-title-dup")
            self.assertEqual(new_p.read_text(encoding="utf-8").strip(), "a,词品,甲")
            self.assertIn("b,词品（珍藏本）,乙", dup_p.read_text(encoding="utf-8"))

    def test_classify_keeps_distinct_journal_issues(self):
        ebook_keys = {
            normalize_title("励耘学刊（2025年第1辑 总第41辑）"): [
                {"bookName": "励耘学刊（2025年第1辑 总第41辑）"}
            ]
        }
        bucket, reason = classify_book(
            "sid",
            "励耘学刊（2019年第2辑/总第30辑）",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys=ebook_keys,
        )
        self.assertEqual((bucket, reason), ("new", "new"))
        bucket, reason = classify_book(
            "sid2",
            "励耘学刊（2025年第1辑/总第41辑）",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys=ebook_keys,
        )
        self.assertEqual((bucket, reason), ("dup", "ebook-info"))

    def test_migrate_false_journal_dups_back_to_new(self):
        with tempfile.TemporaryDirectory() as td:
            new_p = Path(td) / "new.txt"
            dup_p = Path(td) / "dup.txt"
            new_p.write_text("a,词品,甲\n", encoding="utf-8")
            dup_p.write_text(
                "\n".join(
                    [
                        "b,词品（珍藏本）,乙",
                        "c,励耘学刊（2019年第2辑/总第30辑）,杜桂萍",
                        "d,励耘学刊（2024年第1辑/总第39辑）,杜桂萍",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ebook_keys = {
                normalize_title("励耘学刊（2025年第1辑 总第41辑）"): [
                    {"bookName": "励耘学刊（2025年第1辑 总第41辑）"}
                ]
            }
            moved = migrate_stale_from_dup(
                dup_p,
                new_p,
                forbid_ids=set(),
                downloaded_ids=set(),
                ebook_keys=ebook_keys,
                dry_run=False,
            )
            moved_ids = {parse_shelf_line(line)[0] for line, _reason in moved}
            self.assertEqual(moved_ids, {"c", "d"})
            new_text = new_p.read_text(encoding="utf-8")
            dup_text = dup_p.read_text(encoding="utf-8")
            self.assertIn("c,励耘学刊（2019年第2辑/总第30辑）,杜桂萍", new_text)
            self.assertIn("d,励耘学刊（2024年第1辑/总第39辑）,杜桂萍", new_text)
            self.assertIn("b,词品（珍藏本）,乙", dup_text)
            self.assertNotIn("励耘学刊", dup_text)

    def test_migrate_dup_skips_non_issue_stale_entries(self):
        with tempfile.TemporaryDirectory() as td:
            new_p = Path(td) / "new.txt"
            dup_p = Path(td) / "dup.txt"
            new_p.write_text("", encoding="utf-8")
            dup_p.write_text(
                "x,文学概论讲义,老舍\ny,励耘学刊（2019年第2辑/总第30辑）,杜桂萍\n",
                encoding="utf-8",
            )
            moved = migrate_stale_from_dup(
                dup_p,
                new_p,
                forbid_ids=set(),
                downloaded_ids=set(),
                ebook_keys={},
                dry_run=False,
            )
            moved_ids = {parse_shelf_line(line)[0] for line, _reason in moved}
            self.assertEqual(moved_ids, {"y"})
            self.assertIn("x,文学概论讲义,老舍", dup_p.read_text(encoding="utf-8"))
            self.assertIn("y,励耘学刊（2019年第2辑/总第30辑）,杜桂萍", new_p.read_text(encoding="utf-8"))

    def test_run_keeps_first_shelf_title_only(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            shelf = td_path / "shelf.txt"
            forbid = td_path / "forbid.txt"
            ebook = td_path / "ebook.json"
            dup = td_path / "dup.txt"
            new = td_path / "new.txt"
            books = td_path / "books"
            books.mkdir()

            shelf.write_text(
                "id1,古今词话,[宋]杨湜\nid2,古今词话,沈雄\nid3,词品,甲\n",
                encoding="utf-8",
            )
            forbid.write_text("", encoding="utf-8")
            ebook.write_text("[]", encoding="utf-8")
            dup.write_text("", encoding="utf-8")
            new.write_text("", encoding="utf-8")

            result = run(
                shelf_path=shelf,
                forbid_path=forbid,
                ebook_path=ebook,
                dup_path=dup,
                new_path=new,
                books_dir=books,
                dry_run=False,
            )
            self.assertEqual(result["new"], 2)
            self.assertEqual(result["dup"], 1)
            self.assertEqual(result["reasons"]["shelf-title-dup"], 1)
            new_text = new.read_text(encoding="utf-8")
            dup_text = dup.read_text(encoding="utf-8")
            self.assertIn("id1,古今词话,[宋]杨湜", new_text)
            self.assertIn("id3,词品,甲", new_text)
            self.assertIn("id2,古今词话,沈雄", dup_text)


if __name__ == "__main__":
    unittest.main()
