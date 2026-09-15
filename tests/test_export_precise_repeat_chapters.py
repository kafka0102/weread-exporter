"""export_precise 回退重抓防护：已导出章被重抓时不得再占章号。"""
import os
import tempfile
import unittest
import json
from pathlib import Path

import export_precise


def blocks(*lines):
    return [{"type": "text", "text": s} for s in lines]


def write_book(tmp, chapters):
    """造一个 output/<id> 形状的章节目录，返回 chapters 目录路径。"""
    md_dir = Path(tmp) / "chapters"
    (Path(tmp) / "raw").mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)
    for i, (title, body_lines) in enumerate(chapters, 1):
        body = "\n\n".join([title, "刘克庄", *body_lines])
        (md_dir / f"{i:04d}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return str(md_dir)


class TestExportedChapterTexts(unittest.TestCase):
    def test_reads_disk_chapters_by_title_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book(
                tmp,
                [
                    ("沁园春·莫羡渠侬", ["莫羡渠侬，白玉成楼，黄金筑台。"]),
                    ("朝中措（艮翁生日）", ["仙风道骨北山翁。万卷著胸中。"]),
                ],
            )
            texts = export_precise.exported_chapter_texts(md_dir)
            self.assertEqual(
                sorted(texts), ["朝中措（艮翁生日）", "沁园春·莫羡渠侬"]
            )
            self.assertIn(
                "莫羡渠侬，白玉成楼，黄金筑台。",
                texts["沁园春·莫羡渠侬"]["lines"],
            )
            self.assertIn(
                "莫羡渠侬，白玉成楼，黄金筑台。",
                texts["沁园春·莫羡渠侬"]["norm"],
            )

    def test_missing_dir_is_empty(self):
        self.assertEqual(export_precise.exported_chapter_texts(""), {})


class TestRepeatExportedChapterEvidence(unittest.TestCase):
    """诗词选集旧账：0173/0174/0134/0135 这四个重复章必须被判重复。"""

    def setUp(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = write_book(
                tmp,
                [
                    ("沁园春·莫羡渠侬", ["莫羡渠侬，白玉成楼，黄金筑台。"]),
                    ("朝中措（艮翁生日）", ["仙风道骨北山翁。万卷著胸中。"]),
                    ("早行", ["马上续残梦，不知朝日升。"]),
                ],
            )
            self.texts = export_precise.exported_chapter_texts(md_dir)

    def test_empty_same_title_chapter_is_repeat(self):
        why = export_precise.repeat_exported_chapter_evidence(
            "沁园春·莫羡渠侬", [], self.texts
        )
        self.assertEqual(why, "同名空章")

    def test_same_body_with_placeholder_diff_is_repeat(self):
        """canvas 少一个字（□）不算改动，仍是同一章。"""
        why = export_precise.repeat_exported_chapter_evidence(
            "朝中措（艮翁生日）",
            blocks("朝中措（艮翁生日）", "刘克庄", "仙风道骨北山翁。万卷著胸□中。"),
            self.texts,
        )
        self.assertTrue(why)

    def test_body_plus_stale_fragments_is_repeat(self):
        """正文是已导出章 + 上几章残留碎片（0174 的形状）→ 仍算重复。"""
        why = export_precise.repeat_exported_chapter_evidence(
            "朝中措（艮翁生日）",
            blocks(
                "朝中措（艮翁生日）",
                "刘克庄",
                "仙风道骨北山翁。万卷著胸中。",
                "沁园春·我羡君归",
                "刘克庄",
                "楼百尺，谁共登梯。",
                "红。",
                "纱笼。",
            ),
            self.texts,
        )
        self.assertTrue(why)

    def test_new_chapter_is_not_repeat(self):
        self.assertEqual(
            export_precise.repeat_exported_chapter_evidence(
                "早行", blocks("马上续残梦，不知朝日升。"), self.texts
            ),
            "",
        )

    def test_same_title_other_poem_is_not_repeat(self):
        """同名但正文不同的章（组诗同名）必须照常落盘。"""
        self.assertEqual(
            export_precise.repeat_exported_chapter_evidence(
                "沁园春·莫羡渠侬",
                blocks("莫羡渠侬", "刘克庄", "此生相见定何年，且醉花前。"),
                self.texts,
            ),
            "",
        )

    def test_short_titles_are_ignored(self):
        """太短的章名在正文里天然重复，不能当重抓证据。"""
        texts = {"早行": ("早行马上续残梦", {"马上续残梦"})}
        self.assertEqual(
            export_precise.repeat_exported_chapter_evidence(
                "早行", blocks("马上续残梦，不知朝日升。"), texts
            ),
            "",
        )

    def test_unknown_title_is_not_repeat(self):
        self.assertEqual(
            export_precise.repeat_exported_chapter_evidence(
                "西江月（腰痛）", blocks("思邈方书去失，休文老病来攻。"), self.texts
            ),
            "",
        )


class TestMissingChapterFill(unittest.TestCase):
    """补漏：目录里有、章文件里没有的正文项要能定位并插回原位。"""

    CATALOG = ["扉页", "版权信息", "甲诗", "乙诗", "丙诗", "念奴娇（木犀）", "念奴娇"]

    def build(self, tmp):
        """照着刘克庄诗选的实际形状造产物：开头多一个书名页、中间漏两章。"""
        return write_book(
            tmp,
            [
                ("刘克庄诗选", ["书名页，不在目录里"]),
                ("版权信息", ["书名：刘克庄诗选"]),
                ("甲诗", ["甲诗正文。"]),
                ("乙诗", ["乙诗正文。"]),
                ("念奴娇", ["念奴娇正文，与木犀那首不是一章。"]),
            ],
        )

    def test_title_match_key_strips_ordinal_but_keeps_parenthetical(self):
        self.assertEqual(
            export_precise.chapter_title_match_key("第2章 甲诗"),
            export_precise.chapter_title_match_key("甲诗"),
        )
        self.assertNotEqual(
            export_precise.chapter_title_match_key("念奴娇"),
            export_precise.chapter_title_match_key("念奴娇（木犀）"),
        )

    def test_matches_disk_chapters_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.build(tmp)
            mapping = export_precise.match_disk_chapters_to_catalog(
                md_dir, self.CATALOG)
            self.assertEqual(
                mapping, [(1, None), (2, 1), (3, 2), (4, 3), (5, 6)]
            )

    def test_reports_missing_catalog_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.build(tmp)
            self.assertEqual(
                export_precise.missing_catalog_positions(md_dir, self.CATALOG),
                [(4, "丙诗"), (5, "念奴娇（木犀）")],
            )

    def test_insertion_index_points_before_next_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.build(tmp)
            self.assertEqual(
                export_precise.insertion_file_index(md_dir, self.CATALOG, 4), 5
            )
            self.assertEqual(
                export_precise.insertion_file_index(md_dir, self.CATALOG, 5), 5
            )

    def test_shift_moves_chapter_and_raw_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.build(tmp)
            raw_dir = str(Path(tmp) / "raw")
            for i, title in ((4, "乙诗"), (5, "念奴娇")):
                (Path(raw_dir) / f"{i:04d}.json").write_text(
                    json.dumps({"title": title, "images": [], "text_len": 1}),
                    encoding="utf-8",
                )
            moved = export_precise.shift_chapter_files(md_dir, raw_dir, 5)
            self.assertEqual(moved, 1)
            self.assertFalse(os.path.exists(os.path.join(md_dir, "0005.md")))
            self.assertTrue(os.path.exists(os.path.join(md_dir, "0006.md")))
            self.assertTrue(os.path.exists(os.path.join(raw_dir, "0006.json")))
            self.assertEqual(
                export_precise.disk_chapter_titles(md_dir)[-1], "念奴娇"
            )

    def test_shift_from_first_chapter_keeps_numbers_contiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.build(tmp)
            raw_dir = str(Path(tmp) / "raw")
            export_precise.shift_chapter_files(md_dir, raw_dir, 1)
            self.assertEqual(
                sorted(f for f in os.listdir(md_dir) if f.endswith(".md")),
                ["0002.md", "0003.md", "0004.md", "0005.md", "0006.md"],
            )

    def test_renumber_closes_gaps_in_order(self):
        """补漏插章留下的空号要重排成连续章号，顺序不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.build(tmp)
            raw_dir = str(Path(tmp) / "raw")
            # 制造空号：把 0003 挪到 0009
            os.replace(os.path.join(md_dir, "0003.md"), os.path.join(md_dir, "0009.md"))
            titles_before = [
                open(os.path.join(md_dir, f), encoding="utf-8").read().splitlines()[0]
                for f in sorted(os.listdir(md_dir)) if f.endswith(".md")
            ]
            export_precise.renumber_chapter_files(md_dir, raw_dir)
            files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
            self.assertEqual(
                files, ["0001.md", "0002.md", "0003.md", "0004.md", "0005.md"]
            )
            titles_after = [
                open(os.path.join(md_dir, f), encoding="utf-8").read().splitlines()[0]
                for f in files
            ]
            self.assertEqual(titles_before, titles_after)

    def test_renumber_keeps_raw_in_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            md_dir = self.build(tmp)
            raw_dir = str(Path(tmp) / "raw")
            (Path(raw_dir) / "0009.json").write_text(
                json.dumps({"title": "念奴娇", "images": [], "text_len": 3}),
                encoding="utf-8",
            )
            moved_md = os.path.join(md_dir, "0005.md")
            os.replace(moved_md, os.path.join(md_dir, "0009.md"))
            export_precise.renumber_chapter_files(md_dir, raw_dir)
            self.assertFalse(os.path.exists(os.path.join(md_dir, "0009.md")))
            self.assertTrue(os.path.exists(os.path.join(md_dir, "0005.md")))
            self.assertTrue(os.path.exists(os.path.join(raw_dir, "0005.json")))


class TestSplitRewindBlocks(unittest.TestCase):
    def test_rewind_keeps_current_chapter_text(self):
        """回退重抓时当前章正文挂在残留章首之前 → 必须保留（健忘一首事故）。"""
        keep, tail = export_precise.split_rewind_blocks(
            blocks(
                "健忘一首",
                "刘克庄",
                "昏昏健忘身，兀兀坐忘心。",
                "杂咏一百首·项羽",
                "刘克庄",
                "顿无英霸气，尚有妇儿仁。",
            ),
            "杂咏一百首·项羽",
        )
        self.assertEqual(
            [b["text"] for b in keep],
            ["健忘一首", "刘克庄", "昏昏健忘身，兀兀坐忘心。"],
        )
        self.assertEqual(tail[0]["text"], "杂咏一百首·项羽")

    def test_rewind_without_split_point_discards_all(self):
        """找不到更早章首时保持原行为：整页丢弃，不按当前章落盘。"""
        keep, tail = export_precise.split_rewind_blocks(
            blocks("健忘一首", "刘克庄"), "杂咏一百首·项羽"
        )
        self.assertEqual(keep, [])
        self.assertEqual(tail, [])

    def test_rewind_without_prefix_keeps_tail(self):
        """整页都属于更早章（指针超前）→ 不保留前缀，尾巴仍交给回退章。"""
        keep, tail = export_precise.split_rewind_blocks(
            blocks("杂咏一百首·项羽", "刘克庄", "顿无英霸气，尚有妇儿仁。"),
            "杂咏一百首·项羽",
        )
        self.assertEqual(keep, [])
        self.assertEqual(tail[0]["text"], "杂咏一百首·项羽")


class TestCountRepeatedChapter(unittest.TestCase):
    def test_any_catalog_title_splits_off_later_chapters(self):
        """补漏时正文里紧接着别的目录章（不是目录下一章）也要切掉。"""
        catalog = ["橘花", "健忘一首", "病后访梅九绝", "杂咏一百首·项羽"]
        before, after = export_precise.split_blocks_at_any_catalog_title(
            blocks(
                "健忘一首",
                "刘克庄",
                "昏昏健忘废专精，默坐空斋忽自惊。",
                "病后访梅九绝",
                "刘克庄",
                "梦得因桃数左迁，长源为柳忤当权。",
            ),
            catalog,
            exclude_title="健忘一首",
        )
        self.assertEqual(len(before), 3)
        self.assertEqual(after[0]["text"], "病后访梅九绝")

    def test_any_catalog_title_ignores_target_itself(self):
        """本章章名（含简写差异）不能当成下一章边界。"""
        catalog = ["哨遍", "念奴娇（木犀）", "念奴娇"]
        before, after = export_precise.split_blocks_at_any_catalog_title(
            blocks("念奴娇（木犀）", "刘克庄", "绕篱寻菊，菊犹迟、舍北芙蓉浑未。"),
            catalog,
            exclude_title="念奴娇（木犀）",
        )
        self.assertEqual(after, [])
        self.assertEqual(len(before), 3)

    def test_split_off_keeps_chapter_when_title_is_prefix_of_own_heading(self):
        """「念奴娇」是「念奴娇（木犀）」的前缀：切分不能把本章自己切没。"""
        catalog = ["哨遍", "念奴娇（木犀）", "念奴娇"]
        kept, boundary = export_precise.split_off_next_chapter(
            blocks(
                "念奴娇（木犀）",
                "刘克庄",
                "绕篱寻菊，菊犹迟、舍北芙蓉浑未。",
            ),
            catalog,
            "念奴娇（木犀）",
            "念奴娇",
        )
        self.assertFalse(boundary)
        self.assertEqual([b["text"] for b in kept][:1], ["念奴娇（木犀）"])
        self.assertEqual(len(kept), 3)

    def test_split_off_cuts_at_later_catalog_chapter(self):
        """正文里紧接着别的目录章时切掉，并报告已碰到下一章。"""
        catalog = ["橘花", "健忘一首", "病后访梅九绝", "杂咏一百首·项羽"]
        kept, boundary = export_precise.split_off_next_chapter(
            blocks(
                "上一章残留",
                "健忘一首",
                "刘克庄",
                "昏昏健忘废专精，默坐空斋忽自惊。",
                "病后访梅九绝",
                "刘克庄",
                "梦得因桃数左迁，长源为柳忤当权。",
            ),
            catalog,
            "健忘一首",
            "杂咏一百首·项羽",
        )
        self.assertTrue(boundary)
        self.assertEqual(
            [b["text"] for b in kept],
            ["健忘一首", "刘克庄", "昏昏健忘废专精，默坐空斋忽自惊。"],
        )

    def test_same_chapter_accumulates(self):
        state = {}
        self.assertEqual(
            export_precise.count_repeated_chapter(state, "踏莎行（巧夕）"), 1
        )
        self.assertEqual(
            export_precise.count_repeated_chapter(state, "踏莎行（巧夕）"), 2
        )

    def test_chapter_change_resets(self):
        """回退重抓逐章回放时不能累加成死循环。"""
        state = {}
        for title in ("沁园春·莫羡渠侬", "朝中措（艮翁生日）", "卜算子"):
            export_precise.count_repeated_chapter(state, title)
        self.assertEqual(export_precise.count_repeated_chapter(state, "卜算子"), 2)

    def test_state_is_per_chapter_key(self):
        state = {}
        export_precise.count_repeated_chapter(state, "卜算子")
        self.assertEqual(
            export_precise.count_repeated_chapter(state, "卜算子（曹守生朝）"), 1
        )


if __name__ == "__main__":
    unittest.main()
