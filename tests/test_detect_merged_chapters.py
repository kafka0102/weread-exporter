"""章节合并体检：阈值判定、短书豁免、证据识别与目录扫描。"""
import json
import tempfile
import unittest
from pathlib import Path

import detect_merged_chapters as dm


def make_body(spec: list[tuple[str, int]]) -> list[dict]:
    """按 [(章名, 正文字数)] 造 body；每章填充文本互不相同，避免假重复。"""
    body = []
    for seq, (name, chars) in enumerate(spec, 1):
        text = "".join(
            f"{name}第{i}段正文内容。" for i in range(chars // 12 + 1)
        )[:chars]
        body.append(
            {"chapter_name": name, "content": text, "has_content": bool(chars)}
        )
    return body


class AnalyzeRowsTests(unittest.TestCase):
    def test_two_chapter_book_is_exempt(self):
        """诗词画册/单篇：只有一两章，单章一两万字属正常。"""
        body = make_body([("版权信息", 120), ("正文", 25000)])
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body)))

    def test_four_chapter_book_is_exempt(self):
        """「版权信息 + 正文 + 附录」这类 3—4 章结构默认不判定。"""
        body = make_body(
            [("版权信息", 120), ("正文", 60000), ("附录", 900), ("后记", 600)]
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body)))

    def test_many_chapters_balanced_is_clean(self):
        body = make_body([(f"第{i}章", 20000) for i in range(1, 13)])
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body)))

    def test_many_chapters_single_dominant_is_severe(self):
        """≥10 章：单章 ≥80% 且其余多为小章 → 严重。"""
        body = make_body(
            [("版权信息", 100)]
            + [(f"第{i}章", 200) for i in range(1, 11)]
            + [("后记", 200000)]
        )
        result = dm.analyze_rows(dm.chapter_rows(body))
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "严重")
        self.assertEqual(result["top_index"], 12)
        self.assertTrue(result["bookend_top"])

    def test_many_chapters_moderate_share(self):
        """≥10 章：单章过半、其余章多为小章 → 中度。"""
        body = make_body(
            [("版权信息", 100)]
            + [(f"小章{i}", 150) for i in range(1, 5)]
            + [(f"中章{i}", 3000) for i in range(1, 7)]
            + [("卷四", 28125)]
        )
        result = dm.analyze_rows(dm.chapter_rows(body))
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "中度")
        self.assertFalse(result["bookend_top"])
        self.assertGreater(result["tiny_ratio"], 0.3)

    def test_few_chapters_two_even_volumes_is_clean(self):
        """5—9 章：上下卷各半属正常，不应误报。"""
        body = make_body(
            [
                ("版权信息", 88),
                ("解题", 435),
                ("自序", 1108),
                ("卷上 唐五代北宋", 20638),
                ("卷下 南宋辽金元", 22576),
            ]
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body)))

    def test_few_chapters_dominant_is_reported(self):
        """5—9 章：单章 ≥90% 且其余几乎为空 → 严重。"""
        body = make_body(
            [
                ("版权信息", 72),
                ("作者简介", 262),
                ("苏东坡的可能性", 7772),
                ("归去来兮", 249788),
                ("附录 苏东坡大事年谱简编", 4399),
            ]
        )
        result = dm.analyze_rows(dm.chapter_rows(body))
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "严重")

    def test_evidence_overrides_low_share(self):
        """占比没到阈值，但最大章里混入了其他章正文 → 仍判合并。"""
        body = [
            {
                "chapter_name": "版权信息",
                "content": "书名页正文" + "甲" * 400,
            },
            {"chapter_name": "第一章 甲", "content": "甲" * 600},
            {"chapter_name": "第二章 乙", "content": "乙" * 600},
            {"chapter_name": "第三章 丙", "content": "丙" * 600},
            {"chapter_name": "第四章 丁", "content": "丁" * 600},
            {"chapter_name": "第五章 戊", "content": "戊" * 600},
        ]
        # 把其他章正文整段塞进首章 → 形成「正文重复」强证据
        body[0]["content"] += "".join(c["content"] for c in body[1:])
        result = dm.analyze_rows(dm.chapter_rows(body))
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "中度")
        self.assertGreaterEqual(result["duplicated_chapters"], 3)

    def test_blank_chapters_level(self):
        """空章 + 标题占位章占比过高 → 多章无正文。"""
        body = make_body([("版权信息", 100), ("目录", 0), ("出版说明", 0)])
        body += [
            {"chapter_name": f"第{i}讲", "content": f"第{i}讲", "has_content": True}
            for i in range(1, 8)
        ]
        body += [
            {"chapter_name": f"篇{i}", "content": f"篇{i}正文" * 500}
            for i in range(1, 4)
        ]
        result = dm.analyze_rows(dm.chapter_rows(body))
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "多章无正文")

    def test_short_book_is_skipped(self):
        body = make_body([("版权信息", 100)] + [(f"第{i}章", 200) for i in range(1, 8)])
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body)))

    def test_missing_catalog_chapter_is_flagged(self):
        """目录里有、导出里没有，且标题混在别章正文里 → 章节缺失。"""
        catalog = ["版权信息"] + [f"第{i}章" for i in range(1, 8)] + ["李白研究"]
        body = make_body(
            [("版权信息", 120)] + [(f"第{i}章", 3000) for i in range(1, 8)]
        )
        body.append(
            {
                "chapter_name": "第七章",
                "content": (
                    "第七章正文\n\n"
                    "本章讨论唐代文学研究的进展。\n\n"
                    "李白研究\n\n"
                    "□雷子帧本文综述李白研究进展。\n\n"
                    + "学界对李白的研究持续推进。\n\n" * 40
                ),
            }
        )
        result = dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog)
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "章节缺失")
        self.assertEqual(result["missing_chapters"], ["李白研究"])

    def test_quoted_title_is_not_missing_evidence(self):
        """《李白研究》这类文献引用不算「标题出现在正文中」。"""
        catalog = ["版权信息"] + [f"第{i}章" for i in range(1, 8)] + ["李白研究"]
        body = make_body(
            [("版权信息", 120)] + [(f"第{i}章", 3000) for i in range(1, 8)]
        )
        body.append(
            {
                "chapter_name": "第七章",
                "content": "第七章正文\n\n参见《李白研究》一书。\n\n" * 40,
            }
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog))

    def _book_with(self, tail_name: str, tail_content: str) -> list[dict]:
        """8 章正常书 + 一个承载目录条目正文的章节，用于缺章规则测试。"""
        body = make_body(
            [("版权信息", 120)] + [(f"第{i}章", 3000) for i in range(1, 8)]
        )
        body.append({"chapter_name": tail_name, "content": tail_content})
        return body

    def test_wrapped_title_is_not_missing_evidence(self):
        """长标题在正文里折行断开 → 章界错位，但正文完整，不算缺失。"""
        catalog = ["版权信息", "清明与孙都督伯泉出郊游迎恩隆禧二寺观郑尚书园池"]
        body = self._book_with(
            "第七章",
            "第七章正文\n\n"
            "清明与孙都督伯泉出郊游迎恩隆禧二寺观郑尚\n\n"
            "书园池\n\n"
            "将军宿卫未央下，岁改不知春草长。\n\n" + "正文继续。\n\n" * 40,
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog))

    def test_volume_heading_is_not_missing_evidence(self):
        """卷标题（卷一 乐府）是结构性标题，正文不单独成章属正常。"""
        catalog = ["版权信息", "卷一 乐府"]
        body = self._book_with(
            "版权信息",
            "版权信息\n\n书名：明诗评选\n\n卷一 乐府\n\n" + "版权说明。\n\n" * 40,
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog))

    def test_author_group_heading_is_not_missing_evidence(self):
        """作者分组行（柳永 八首）只是分组抬头，正文本来就不单独成章。"""
        catalog = ["版权信息", "柳永 八首"]
        body = self._book_with(
            "第七章",
            "第七章正文\n\n柳永 八首\n\n雨霖铃\n\n寒蝉凄切，对长亭晚。\n\n"
            + "词作赏析。\n\n" * 40,
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog))

    def test_inline_mention_is_not_missing_evidence(self):
        """标题只夹在句子中间（学会/机构名）→ 不是被并进邻章的信号。"""
        catalog = ["版权信息", "王维研究"]
        body = self._book_with(
            "第七章",
            "第七章正文\n\n中国王维研究会会长吴相洲教授指出，唐代文学研究持续推进。\n\n"
            + "正文继续。\n\n" * 40,
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog))

    def test_catalog_chapter_is_not_missing_evidence(self):
        """目录/索引类章节里逐行罗列篇名，不作为「章节缺失」证据。"""
        catalog = ["版权信息", "庚子书愤二首"]
        body = self._book_with(
            "篇目笔画索引",
            "庚子书愤二首\n\n贫居\n\n贫病有感\n\n" + "篇名\n\n" * 100,
        )
        self.assertIsNone(dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog))

    def test_swallowed_short_piece_is_still_flagged(self):
        """真正被并进邻章的短篇目（独立成行）仍要报出来。"""
        catalog = ["版权信息", "秋蕊香引", "王维研究"]
        body = self._book_with(
            "第七章",
            "第七章正文\n\n秋蕊香引\n\n留不得。光阴催促，奈芳兰歇。\n\n"
            "中国王维研究会会长吴相洲教授指出研究进展。\n\n" + "正文继续。\n\n" * 40,
        )
        result = dm.analyze_rows(dm.chapter_rows(body), catalog_titles=catalog)
        self.assertIsNotNone(result)
        self.assertEqual(result["level"], "章节缺失")
        self.assertEqual(result["missing_chapters"], ["秋蕊香引"])

    def test_title_body_form_classification(self):
        """标题在正文里的三种形态：独立整行 / 折行 / 句中。"""
        body = self._book_with(
            "第七章",
            "第七章正文\n\n代意二首\n\n梦兰前事悔成占。\n\n"
            "少游已矣，万人何赎——山抹微云君\n\n秦观\n\n"
            "中国王维研究会会长吴相洲教授指出。\n\n",
        )
        rows = dm.chapter_rows(body)
        self.assertEqual(dm.title_body_form(rows, dm.normalize_text("代意二首")), "line")
        self.assertEqual(
            dm.title_body_form(rows, dm.normalize_text("少游已矣，万人何赎——山抹微云君秦观")),
            "wrapped",
        )
        self.assertEqual(dm.title_body_form(rows, dm.normalize_text("王维研究")), "inline")
        self.assertEqual(dm.title_body_form(rows, "查无此标题"), "")


class ScanDirectoryTests(unittest.TestCase):
    def test_run_scans_json_books(self):
        with tempfile.TemporaryDirectory() as tmp:
            books = Path(tmp)
            broken = books / "aaaa1111_合并书.json"
            broken.write_text(
                json.dumps(
                    {
                        "title": "合并书",
                        "body": make_body(
                            [("版权信息", 100)]
                            + [(f"第{i}章", 200) for i in range(1, 11)]
                            + [("后记", 200000)]
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            clean = books / "bbbb2222_正常书.json"
            clean.write_text(
                json.dumps(
                    {
                        "title": "正常书",
                        "body": make_body([(f"第{i}章", 20000) for i in range(1, 13)]),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = dm.run(books_dir=books)
            self.assertEqual(result["scanned"], 2)
            self.assertEqual(len(result["findings"]), 1)
            self.assertEqual(result["findings"][0]["book_id"], "aaaa1111")
            self.assertEqual(result["findings"][0]["level"], "严重")

    def test_run_can_scan_output_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chapter_dir = root / "output" / "cccc3333" / "chapters"
            chapter_dir.mkdir(parents=True)
            for i in range(1, 12):
                chars = 200 if i > 1 else 100
                (chapter_dir / f"{i:04d}.md").write_text(
                    f"# 第{i}章\n\n" + "正文内容。" * (chars // 5),
                    encoding="utf-8",
                )
            (chapter_dir / "0012.md").write_text(
                "# 后记\n\n" + "正文内容。" * 40000, encoding="utf-8"
            )
            result = dm.run(
                books_dir=Path(tmp) / "empty-books",
                output_root=root / "output",
            )
            self.assertEqual(len(result["findings"]), 1)
            self.assertEqual(result["findings"][0]["book_id"], "cccc3333")
            self.assertEqual(result["findings"][0]["level"], "严重")

    def test_tsv_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.tsv"
            dm.write_tsv(
                out,
                [
                    {
                        "level": "严重",
                        "book_id": "x1",
                        "title": "书",
                        "chapters": 12,
                        "total": 200000,
                        "top_index": 12,
                        "top_name": "后记",
                        "top_share": 0.99,
                        "tiny": 10,
                        "tiny_ratio": 0.9,
                        "blank": 0,
                        "placeholder": 10,
                        "bookend_top": True,
                        "leaked_names": 5,
                        "duplicated_chapters": 2,
                        "source": "books/x1.json",
                    }
                ],
            )
            text = out.read_text(encoding="utf-8")
            self.assertIn("级别\tbook_id", text)
            self.assertIn("严重\tx1\t书", text)


if __name__ == "__main__":
    unittest.main()
