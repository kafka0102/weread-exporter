"""export_precise 回退重抓防护：已导出章被重抓时不得再占章号。"""
import tempfile
import unittest
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
                "莫羡渠侬，白玉成楼，黄金筑台。", texts["沁园春·莫羡渠侬"][1]
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


class TestCountRepeatedChapter(unittest.TestCase):
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
