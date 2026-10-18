"""export_precise 续传保护：磁盘章产物被污染时不得当作「已完成」。"""
import json
import tempfile
import unittest
from pathlib import Path

import export_precise


def write_book_dir(root, chapters, catalog):
    """造一个 output/<id> 形状的中间产物目录，返回 chapters 目录路径。"""
    book_dir = Path(root)
    md_dir = book_dir / "chapters"
    raw_dir = book_dir / "raw"
    md_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i, (title, body) in enumerate(chapters, 1):
        (md_dir / f"{i:04d}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        (raw_dir / f"{i:04d}.json").write_text(
            json.dumps({"title": title, "images": [], "text_len": len(body)}),
            encoding="utf-8",
        )
    (book_dir / "_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
    )
    return str(md_dir)


class TestDiskChapterTitles(unittest.TestCase):
    def test_reads_titles_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(
                tmp,
                [("丁忧微臣", "甲"), ("秘阁校理", "乙")],
                ["丁忧微臣", "秘阁校理"],
            )
            self.assertEqual(
                export_precise.disk_chapter_titles(md_dir), ["丁忧微臣", "秘阁校理"]
            )

    def test_get_last_chapter_title_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, [("丁忧微臣", "甲"), ("参知政事", "乙")], [])
            self.assertEqual(
                export_precise.get_last_chapter_title(md_dir), ("参知政事", 2)
            )


class TestChapterFileOrdering(unittest.TestCase):
    """超过 9999 章的书章号是 5 位（`10000.md`），不能按字典序排。

    全宋词 12260 章的历史事故：字典序把 `9999.md` 当成最后一章，续传锚点
    回退到书中部（目录第 10039 项），随后把已导出的两千多章整段回放。
    """

    def write_wide_book(self, tmp, chapters):
        """造 5 位章号的产物目录；chapters 为 [(章号, 章名)]。"""
        md_dir = Path(tmp) / "chapters"
        raw_dir = Path(tmp) / "raw"
        md_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        for idx, title in chapters:
            (md_dir / f"{idx:04d}.md").write_text(
                f"# {title}\n\n正文。\n", encoding="utf-8"
            )
            (raw_dir / f"{idx:04d}.json").write_text(
                json.dumps({"title": title, "images": [], "text_len": 3}),
                encoding="utf-8",
            )
        return str(md_dir)

    def test_list_chapter_files_sorts_by_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.write_wide_book(
                tmp, [(1, "潘阆"), (1001, "陌上郎"), (9999, "踏莎行·秋思"),
                      (10000, "刘珙"), (12260, "曹休齐")]
            )
            self.assertEqual(
                export_precise.list_chapter_files(md_dir),
                ["0001.md", "1001.md", "9999.md", "10000.md", "12260.md"],
            )

    def test_last_chapter_is_largest_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.write_wide_book(
                tmp, [(9999, "踏莎行·秋思"), (12260, "曹休齐")]
            )
            self.assertEqual(
                export_precise.get_last_chapter_title(md_dir), ("曹休齐", 12260)
            )

    def test_disk_titles_follow_chapter_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.write_wide_book(
                tmp, [(1, "潘阆"), (1001, "陌上郎"), (10000, "刘珙")]
            )
            self.assertEqual(
                export_precise.disk_chapter_titles(md_dir),
                ["潘阆", "陌上郎", "刘珙"],
            )

    def test_catalog_alignment_does_not_cascade(self):
        """字典序会把 5 位章号排到最前，贪心对齐后整段对不上（9033 章误报缺失）。"""
        catalog = ["潘阆", "陌上郎", "踏莎行·秋思", "刘珙", "曹休齐"]
        chapters = [
            (1, "潘阆"), (1001, "陌上郎"), (9999, "踏莎行·秋思"),
            (10000, "刘珙"), (12260, "曹休齐"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.write_wide_book(tmp, chapters)
            self.assertEqual(
                export_precise.match_disk_chapters_to_catalog(md_dir, catalog),
                [(1, 0), (1001, 1), (9999, 2), (10000, 3), (12260, 4)],
            )
            self.assertEqual(
                export_precise.missing_catalog_positions(md_dir, catalog), []
            )


class TestStaleChapterArtifacts(unittest.TestCase):
    def test_healthy_export_is_not_flagged(self):
        catalog = ["扉页", "版权信息", "第一卷 起源", "第二卷 发展", "后记"]
        chapters = [
            ("版权信息", "书名：某书"),
            ("第一卷 起源", "正文甲"),
            ("第二卷 发展", "正文乙"),
            ("后记", "写完啦"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, chapters, catalog)
            self.assertIsNone(
                export_precise.stale_chapter_artifacts_evidence(md_dir, catalog)
            )
            # 正常产物不应中止
            export_precise.assert_resume_artifacts_trustworthy("abc", md_dir, catalog)

    def test_missing_catalog_chapters_are_flagged(self):
        """目录 11 项只剩 5 个章文件（范仲淹那本的形状）→ 必须报出来。"""
        catalog = [
            "扉页", "版权信息", "宋初新政名臣——范仲淹", "丁忧微臣", "秘阁校理",
            "右司谏、江淮安抚史", "右司谏", "贬逐之臣", "礼部员外郎", "陕西经略安抚",
            "参知政事",
        ]
        chapters = [
            ("版权信息", "书名：宋初新政名臣——范仲淹"),
            ("宋初新政名臣——范仲淹", "封面图"),
            ("丁忧微臣", "居丧越礼，冒哀上书。"),
            ("秘阁校理", "冬至节朝仪之争。"),
            ("参知政事", "昙花一现的庆历新政。"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, chapters, catalog)
            evidence = export_precise.stale_chapter_artifacts_evidence(md_dir, catalog)
            self.assertIsNotNone(evidence)
            self.assertIn("没有对应章文件", evidence)
            with self.assertRaises(RuntimeError):
                export_precise.assert_resume_artifacts_trustworthy("b0d3", md_dir, catalog)

    def test_single_missing_chapter_is_tolerated(self):
        catalog = ["卷一 起源", "卷二 发展", "卷三 高潮", "卷四 尾声", "后记"]
        chapters = [
            ("卷一 起源", "甲"),
            ("卷二 发展", "乙"),
            ("卷三 高潮", "丙"),
            ("后记", "丁"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, chapters, catalog)
            self.assertIsNone(
                export_precise.stale_chapter_artifacts_evidence(md_dir, catalog)
            )

    def test_merged_titles_in_dominant_chapter_are_flagged(self):
        """章文件都在，但最大章正文里独立成行地混进别的目录标题 → 是合并。"""
        catalog = ["第一章 起源", "第二章 发展", "第三章 高潮", "第四章 尾声"]
        big_body = "正文。" * 900
        chapters = [
            ("第一章 起源", "甲"),
            (
                "第二章 发展",
                f"{big_body}\n第三章 高潮\n\n{big_body}\n第四章 尾声\n\n{big_body}",
            ),
            ("第三章 高潮", "丙"),
            ("第四章 尾声", "丁"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, chapters, catalog)
            merged = export_precise.merged_titles_in_dominant_chapter(md_dir, catalog)
            self.assertEqual(merged, ["第三章 高潮", "第四章 尾声"])
            evidence = export_precise.stale_chapter_artifacts_evidence(md_dir, catalog)
            self.assertIsNotNone(evidence)
            self.assertIn("独立成行", evidence)

    def test_merged_check_needs_dominant_chapter(self):
        """各章体量均匀时，正文里出现别的章名不足以判定合并。"""
        catalog = ["第一章 起源", "第二章 发展", "第三章 高潮", "第四章 尾声"]
        body = "正文。" * 800
        chapters = [
            ("第一章 起源", body),
            ("第二章 发展", "正文。\n\n第三章 高潮\n\n第四章 尾声\n\n正文。"),
            ("第三章 高潮", body),
            ("第四章 尾声", body),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, chapters, catalog)
            self.assertEqual(
                export_precise.merged_titles_in_dominant_chapter(md_dir, catalog), []
            )
            self.assertIsNone(
                export_precise.stale_chapter_artifacts_evidence(md_dir, catalog)
            )

    def test_index_like_dominant_chapter_is_exempt(self):
        """目录/索引章本来就逐行罗列篇名，不能当合并证据。"""
        catalog = ["目录", "第一章 起源", "第二章 发展", "第三章 高潮"]
        listing = "正文。" * 900
        chapters = [
            ("目录", f"第一章 起源\n第二章 发展\n第三章 高潮\n{listing}"),
            ("第一章 起源", "甲"),
            ("第二章 发展", "乙"),
            ("第三章 高潮", "丙"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, chapters, catalog)
            self.assertEqual(
                export_precise.merged_titles_in_dominant_chapter(md_dir, catalog), []
            )
            self.assertIsNone(
                export_precise.stale_chapter_artifacts_evidence(md_dir, catalog)
            )

    def test_empty_dir_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                export_precise.stale_chapter_artifacts_evidence(
                    str(Path(tmp) / "chapters"), ["第一章", "第二章", "第三章"]
                )
            )

    def test_section_like_titles_are_exempt(self):
        """「卷一/卷二…」这类结构性标题本来就不单独成章，不算缺章。"""
        catalog = ["卷一", "卷二", "卷三", "卷一 乐府", "甲篇正文", "乙篇正文"]
        chapters = [("甲篇正文", "甲"), ("乙篇正文", "乙")]
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book_dir(tmp, chapters, catalog)
            self.assertEqual(
                export_precise.missing_catalog_chapters_on_disk(md_dir, catalog), []
            )
            self.assertIsNone(
                export_precise.stale_chapter_artifacts_evidence(md_dir, catalog)
            )


if __name__ == "__main__":
    unittest.main()
