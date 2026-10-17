import json
import tempfile
import unittest
from pathlib import Path

import dedupe_shelf_books
from dedupe_shelf_books import (
    author_tokens,
    authors_match,
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

ZHCONV_AVAILABLE = dedupe_shelf_books.zhconv is not None


def occ(title: str, author: str, order=(0, 0), other_id: str = "other-id"):
    """造一条「占用」记录：同名同作者的另一本，默认顺序键排在当前这本之前。"""
    return {normalize_title(title): [(other_id, author_tokens(author), order)]}


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


class TestAuthorTokens(unittest.TestCase):
    """作者比对：去朝代标记与角色词，按人名 token 取交集。"""

    def test_strip_dynasty_and_role_words(self):
        self.assertIn("王英志", author_tokens("王英志选注"))
        self.assertIn("王英志", author_tokens("王英志 选注"))
        self.assertIn("纳兰容若", author_tokens("[清]纳兰容若"))
        self.assertIn("郭茂倩", author_tokens("【北宋】郭茂倩 编著；周兴陆 解注"))
        self.assertIn("叶元章", author_tokens("[清]朱彝尊著 叶元章 钟夏选注"))
        # 圆括号里的别名保留
        self.assertIn("六神磊磊", author_tokens("王晓磊（六神磊磊）"))
        self.assertEqual(author_tokens(""), frozenset())
        self.assertEqual(author_tokens(None), frozenset())
        # 单字/全是角色词 → 拿不到 token，退回按书名判重
        self.assertEqual(author_tokens("甲"), frozenset())
        self.assertEqual(author_tokens("选注"), frozenset())

    def test_match_rules(self):
        # 至少一人相同即算对得上（作者串写法不同也能匹配）
        self.assertTrue(
            authors_match(author_tokens("王英志选注"), author_tokens("王英志 选注"))
        )
        self.assertTrue(
            authors_match(
                author_tokens("[清]朱彝尊著 叶元章 钟夏选注"),
                author_tokens("朱彝尊 著；叶元章；钟夏 选注"),
            )
        )
        # 同名异作者：不同选注者/不同版本
        self.assertFalse(
            authors_match(author_tokens("王志清"), author_tokens("陈铁民 选注"))
        )
        # 任一侧没有可比对的 token 时退回「按书名判重」
        self.assertTrue(authors_match(author_tokens(""), author_tokens("陈铁民 选注")))
        self.assertTrue(authors_match(author_tokens("佚名"), author_tokens("")))

    def test_variant_and_typo_tolerance(self):
        # 三字以上、只差一个字：异体字/形近字（池北偶谈 王士禛 / 王士祯）
        self.assertTrue(
            authors_match(author_tokens("[清]王士禛"), author_tokens("[清]王士祯 尹小林 审校"))
        )
        self.assertTrue(
            authors_match(author_tokens("松原郎"), author_tokens("【日】松原朗"))
        )
        self.assertTrue(
            authors_match(author_tokens("钱钟书"), author_tokens("钱锺书 选注"))
        )
        # 两字姓名只差一字不认：李白 / 李贺 是两个人
        self.assertFalse(authors_match(author_tokens("李白"), author_tokens("李贺")))

    @unittest.skipUnless(ZHCONV_AVAILABLE, "未安装 zhconv")
    def test_traditional_to_simplified(self):
        self.assertTrue(
            authors_match(author_tokens("歐陽修"), author_tokens("欧阳修"))
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

    def test_ebook_same_title_and_author_is_dup(self):
        # 书名 + 作者都对得上才算重复（作者串写法不同也能匹配）
        bucket, reason = classify_book(
            "sid",
            "袁枚诗选",
            "王英志选注",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={"袁枚诗选": [{"bookName": "袁枚诗选", "authorName": "王英志 选注"}]},
        )
        self.assertEqual((bucket, reason), ("dup", "ebook-info"))

    def test_ebook_same_title_other_author_is_new(self):
        # 同名异书（不同选注者/不同版本）当新书
        bucket, reason = classify_book(
            "sid",
            "王维诗选",
            "王志清",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={"王维诗选": [{"bookName": "王维诗选", "authorName": "陈铁民 选注"}]},
        )
        self.assertEqual((bucket, reason), ("new", "new-diff-author"))

    def test_ebook_title_only_when_author_missing(self):
        # 任一侧拿不到作者 token 时退回按书名判重
        bucket, reason = classify_book(
            "sid",
            "古今词话",
            "",
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

    def test_shelf_title_dup_same_author(self):
        bucket, reason = classify_book(
            "sid2",
            "词品（珍藏本）",
            "甲某",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={},
            occupied=occ("词品", "甲某"),
            order=(1, 2),
        )
        self.assertEqual((bucket, reason), ("dup", "shelf-title-dup"))

    def test_shelf_title_other_author_is_new(self):
        bucket, reason = classify_book(
            "sid2",
            "词品（珍藏本）",
            "乙某",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={},
            occupied=occ("词品", "甲某"),
            order=(1, 2),
        )
        self.assertEqual((bucket, reason), ("new", "new-diff-author"))

    def test_shelf_title_same_book_kept_when_earliest(self):
        """同名同作者但对方排在后面（自己是最靠前那本）→ 不算重复。"""
        bucket, reason = classify_book(
            "sid2",
            "词品（珍藏本）",
            "甲某",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={},
            occupied=occ("词品", "甲某", order=(1, 9)),
            order=(1, 2),
        )
        self.assertEqual((bucket, reason), ("new", "new"))

    def test_shelf_title_stale_entry_blocks(self):
        """不在书架上的历史条目（顺序键 (0, n)）仍然占位。"""
        bucket, reason = classify_book(
            "sid2",
            "词品",
            "甲某",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={},
            occupied=occ("词品", "甲某", order=(0, 3)),
            order=(1, 2),
        )
        self.assertEqual((bucket, reason), ("dup", "shelf-title-dup"))

    def test_shelf_title_dup_with_variant_author(self):
        """已下载那本是「王士禛」，书架这本写「王士祯」→ 仍是同一本，判 dup。"""
        bucket, reason = classify_book(
            "sid2",
            "池北偶谈",
            "[清]王士祯 尹小林 审校",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys={},
            occupied=occ("池北偶谈", "[清]王士禛"),
            order=(1, 2),
        )
        self.assertEqual((bucket, reason), ("dup", "shelf-title-dup"))

    def test_shelf_title_dup_after_higher_priority(self):
        # forbid 优先于重名
        bucket, reason = classify_book(
            "sid",
            "词品",
            "甲某",
            forbid_ids={"sid"},
            downloaded_ids=set(),
            ebook_keys={},
            occupied=occ("词品", "甲某"),
            order=(1, 2),
        )
        self.assertEqual((bucket, reason), ("dup", "forbid"))

    def test_forbid_and_downloaded_first(self):
        bucket, reason = classify_book(
            "sid",
            "李太白全集",
            "[唐]李白",
            forbid_ids={"sid"},
            downloaded_ids=set(),
            ebook_keys={},
        )
        self.assertEqual((bucket, reason), ("dup", "forbid"))
        bucket, reason = classify_book(
            "sid2",
            "李太白全集",
            "[唐]李白",
            forbid_ids=set(),
            downloaded_ids={"sid2"},
            ebook_keys={},
        )
        self.assertEqual((bucket, reason), ("dup", "downloaded"))

    def test_downloaded_wins_over_head_new(self):
        """已下载的书不进 new_books.txt，前 N 本规则也不例外。"""
        bucket, reason = classify_book(
            "sid",
            "词品",
            "杨慎",
            forbid_ids=set(),
            downloaded_ids={"sid"},
            ebook_keys={},
            head_new_ids={"sid"},
        )
        self.assertEqual((bucket, reason), ("dup", "downloaded"))

    def test_head_new_ids_override_title_only_match(self):
        """书架前 N 本：同名异作者（书名对得上、作者对不上）也当新书。"""
        ebook_keys = {"词品": [{"bookName": "词品", "authorName": "杨慎"}]}
        bucket, reason = classify_book(
            "sid",
            "词品",
            "高林广",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys=ebook_keys,
            occupied=occ("词品", "沈雄"),
            order=(1, 2),
            head_new_ids={"sid"},
        )
        self.assertEqual((bucket, reason), ("new", "new-head"))

    def test_head_new_ids_do_not_override_same_book(self):
        """前 N 本里「同名同作者」仍判 dup。"""
        ebook_keys = {"词品": [{"bookName": "词品", "authorName": "杨慎"}]}
        bucket, reason = classify_book(
            "sid",
            "词品",
            "杨慎",
            forbid_ids=set(),
            downloaded_ids=set(),
            ebook_keys=ebook_keys,
            head_new_ids={"sid"},
        )
        self.assertEqual((bucket, reason), ("dup", "ebook-info"))

    def test_head_new_ids_still_forbid_first(self):
        bucket, reason = classify_book(
            "sid",
            "词品",
            "杨慎",
            forbid_ids={"sid"},
            downloaded_ids=set(),
            ebook_keys={},
            head_new_ids={"sid"},
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
                head_new_count=0,
                dry_run=False,
            )
            self.assertEqual(result["todo"], 6)
            # dup: forbid + downloaded + ebook-info(同名同作者)
            self.assertEqual(result["dup"], 3)
            # new: 同名异作者（古今词话 杨湜 vs 沈雄）、续词品、续词品（注释本）
            self.assertEqual(result["new"], 3)
            self.assertEqual(result["reasons"]["downloaded"], 1)
            self.assertEqual(result["reasons"]["ebook-info"], 1)
            # 古今词话（杨湜 vs 沈雄）、续词品 与 续词品（注释本）（杨夔生 vs 另一人）
            self.assertEqual(result["reasons"]["new-diff-author"], 3)
            dup_text = dup.read_text(encoding="utf-8")
            new_text = new.read_text(encoding="utf-8")
            self.assertIn("idforbid", dup_text)
            self.assertIn("iddl", dup_text)
            self.assertIn("idebook", dup_text)
            self.assertIn("idsame", new_text)
            self.assertIn("iddupname", new_text)
            self.assertIn("idnew", new_text)
            self.assertNotIn("idnew", dup_text)


    def test_migrate_shelf_title_dup_within_new(self):
        with tempfile.TemporaryDirectory() as td:
            new_p = Path(td) / "new.txt"
            dup_p = Path(td) / "dup.txt"
            new_p.write_text(
                "a,词品,甲某\nb,词品（珍藏本）,甲某\nc,词品（全本）,乙某\n",
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
            self.assertIn("a,词品,甲某", new_p.read_text(encoding="utf-8"))
            self.assertIn("c,词品（全本）,乙某", new_p.read_text(encoding="utf-8"))
            self.assertIn("b,词品（珍藏本）,甲某", dup_p.read_text(encoding="utf-8"))

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
            new_p.write_text("a,词品,甲某\n", encoding="utf-8")
            dup_p.write_text(
                "\n".join(
                    [
                        "b,词品（珍藏本）,乙某",
                        "c,励耘学刊（2019年第2辑/总第30辑）,杜桂萍",
                        "d,励耘学刊（2024年第1辑/总第39辑）,杜桂萍",
                        "e,文学概论讲义,老舍",
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
            # b：同名异作者（旧规则按书名误判）；c/d：期刊期数不同；e：无同名，仍留 dup
            self.assertEqual(moved_ids, {"b", "c", "d"})
            new_text = new_p.read_text(encoding="utf-8")
            dup_text = dup_p.read_text(encoding="utf-8")
            self.assertIn("c,励耘学刊（2019年第2辑/总第30辑）,杜桂萍", new_text)
            self.assertIn("d,励耘学刊（2024年第1辑/总第39辑）,杜桂萍", new_text)
            self.assertIn("b,词品（珍藏本）,乙某", new_text)
            self.assertIn("e,文学概论讲义,老舍", dup_text)
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

    def test_run_shelf_dup_needs_same_author(self):
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
                "id1,古今词话,[宋]杨湜\n"
                "id2,古今词话,杨湜\n"
                "id3,古今词话,沈雄\n"
                "id4,词品,甲\n",
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
                head_new_count=0,
                dry_run=False,
            )
            self.assertEqual(result["new"], 3)
            self.assertEqual(result["dup"], 1)
            self.assertEqual(result["reasons"]["shelf-title-dup"], 1)
            self.assertEqual(result["reasons"]["new-diff-author"], 1)
            new_text = new.read_text(encoding="utf-8")
            dup_text = dup.read_text(encoding="utf-8")
            self.assertIn("id1,古今词话,[宋]杨湜", new_text)
            self.assertIn("id4,词品,甲", new_text)
            # 同名同作者 → 只留第一本
            self.assertIn("id2,古今词话,杨湜", dup_text)
            # 同名异作者（杨湜 vs 沈雄 是两本不同的书）→ 都保留
            self.assertIn("id3,古今词话,沈雄", new_text)

    def _make_case(self, td_path: Path, shelf_text: str, ebook_entries: list[dict]):
        shelf = td_path / "shelf.txt"
        forbid = td_path / "forbid.txt"
        ebook = td_path / "ebook.json"
        dup = td_path / "dup.txt"
        new = td_path / "new.txt"
        books = td_path / "books"
        books.mkdir()
        shelf.write_text(shelf_text, encoding="utf-8")
        forbid.write_text("", encoding="utf-8")
        ebook.write_text(
            json.dumps(ebook_entries, ensure_ascii=False), encoding="utf-8"
        )
        dup.write_text("", encoding="utf-8")
        new.write_text("", encoding="utf-8")
        return shelf, forbid, ebook, dup, new, books

    def test_run_dedupes_same_book_and_keeps_other_author(self):
        """同名同作者判 dup；同名异作者（不同选注者）当新书。"""
        with tempfile.TemporaryDirectory() as td:
            paths = self._make_case(
                Path(td),
                "id1,王维诗选,王志清\nid2,袁枚诗选,王英志选注\n",
                [
                    {"bookName": "王维诗选", "authorName": "陈铁民 选注"},
                    {"bookName": "袁枚诗选", "authorName": "王英志 选注"},
                ],
            )
            shelf, forbid, ebook, dup, new, books = paths
            result = run(
                shelf_path=shelf,
                forbid_path=forbid,
                ebook_path=ebook,
                dup_path=dup,
                new_path=new,
                books_dir=books,
                head_new_count=0,
                dry_run=False,
            )
            self.assertEqual(result["reasons"]["new-diff-author"], 1)
            self.assertEqual(result["reasons"]["ebook-info"], 1)
            self.assertIn("id1,王维诗选,王志清", new.read_text(encoding="utf-8"))
            self.assertIn("id2,袁枚诗选,王英志选注", dup.read_text(encoding="utf-8"))

    def test_run_head_new_count_skips_downloaded(self):
        """书架前 N 本：非 forbid、非已下载的一律当新书；已下载仍进 dup。"""
        with tempfile.TemporaryDirectory() as td:
            paths = self._make_case(
                Path(td),
                "idforbid,禁书,人\nid1,已下载甲,人\nid2,未下载乙,人\n",
                [],
            )
            shelf, forbid, ebook, dup, new, books = paths
            forbid.write_text("idforbid\n", encoding="utf-8")
            (books / "id1_已下载甲.json").write_text("{}", encoding="utf-8")
            result = run(
                shelf_path=shelf,
                forbid_path=forbid,
                ebook_path=ebook,
                dup_path=dup,
                new_path=new,
                books_dir=books,
                head_new_count=3,
                dry_run=False,
            )
            self.assertEqual(result["reasons"]["new-head"], 1)
            self.assertEqual(result["reasons"]["forbid"], 1)
            self.assertEqual(result["reasons"]["downloaded"], 1)
            new_text = new.read_text(encoding="utf-8")
            dup_text = dup.read_text(encoding="utf-8")
            self.assertIn("id2,未下载乙,人", new_text)
            self.assertNotIn("id1,已下载甲,人", new_text)
            self.assertIn("id1,已下载甲,人", dup_text)

            # 关掉前 N 本规则后，未下载的那本仍是新书（没有任何规则拦它）
            result = run(
                shelf_path=shelf,
                forbid_path=forbid,
                ebook_path=ebook,
                dup_path=dup,
                new_path=new,
                books_dir=books,
                head_new_count=0,
                dry_run=False,
            )
            self.assertEqual(result["migrated_new_to_dup"], 0)
            self.assertEqual(result["migrated_dup_to_new"], 0)
            dup_text = dup.read_text(encoding="utf-8")
            new_text = new.read_text(encoding="utf-8")
            self.assertIn("id1,已下载甲,人", dup_text)
            self.assertIn("id2,未下载乙,人", new_text)

    def test_migrate_dup_to_new_for_head_and_other_author(self):
        """dup 里前 N 本、以及同名异作者的书，按新规则迁回 new。"""
        with tempfile.TemporaryDirectory() as td:
            new_p = Path(td) / "new.txt"
            dup_p = Path(td) / "dup.txt"
            new_p.write_text("", encoding="utf-8")
            dup_p.write_text(
                "head1,全唐诗,佚名\n"
                "suf1,王维诗选,王志清\n"
                "same1,王维诗选,陈铁民 选注\n"
                "dl1,已下载书,某人\n"
                "plain1,文学概论讲义,老舍\n",
                encoding="utf-8",
            )
            moved = migrate_stale_from_dup(
                dup_p,
                new_p,
                forbid_ids=set(),
                downloaded_ids={"dl1"},
                ebook_keys={
                    "王维诗选": [{"bookName": "王维诗选", "authorName": "陈铁民 选注"}]
                },
                head_new_ids={"head1"},
                dry_run=False,
            )
            moved_ids = {parse_shelf_line(line)[0] for line, _reason in moved}
            # head1：前 N 本；suf1：同名异作者（ebook 那本是陈铁民选注）
            self.assertEqual(moved_ids, {"head1", "suf1"})
            new_text = new_p.read_text(encoding="utf-8")
            dup_text = dup_p.read_text(encoding="utf-8")
            self.assertIn("head1,全唐诗,佚名", new_text)
            self.assertIn("suf1,王维诗选,王志清", new_text)
            # 同名同作者、已下载、无同名 → 仍留 dup
            self.assertIn("same1,王维诗选,陈铁民 选注", dup_text)
            self.assertIn("dl1,已下载书,某人", dup_text)
            self.assertIn("plain1,文学概论讲义,老舍", dup_text)

    def test_run_skips_empty_title_rows(self):
        """抓取残行（空书名）不写进 new。"""
        with tempfile.TemporaryDirectory() as td:
            paths = self._make_case(
                Path(td),
                "id1,词品,杨慎\n编,,\n",
                [],
            )
            shelf, forbid, ebook, dup, new, books = paths
            result = run(
                shelf_path=shelf,
                forbid_path=forbid,
                ebook_path=ebook,
                dup_path=dup,
                new_path=new,
                books_dir=books,
                head_new_count=0,
                dry_run=False,
            )
            self.assertEqual(result["skipped_empty_title"], 1)
            self.assertEqual(result["todo"], 1)
            self.assertNotIn("编,,", new.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
