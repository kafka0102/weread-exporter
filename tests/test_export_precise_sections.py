"""目录分段：《苏东坡说》译文卷 + 原文卷这种「整段重复前文」的目录。

历史事故（2026-10-22）：`dc0320a0813ab8c29g0104f7《苏东坡说：苏东坡诗文中的故事》`
目录 168 项 = 前 86 项（扉页…译文卷 81 篇）+ 后 82 项（原文卷：卷名 + 同样的 81 个
篇名重排一遍）。会话按标题去重后视图里后段整段消失，于是：

- `is_last_catalog_chapter("原文")` / `is_export_terminal_chapter("原文")` 判成真 →
  越位灌末章防护把正常翻页判成「章节失同步」，连续重开 7 次后 RuntimeError；
- 正文里也找不到「后面的章名」，旧启发式只能把原文卷整段灌进单章。
"""
import unittest
from pathlib import Path

import export_precise


def bilingual_catalog():
    """造《苏东坡说》形状的目录：主段（含卷名「译文」）+ 整段重复的后段。"""
    stories = ["家族往事", "高祖父拒学法术", "祖父豁达不羁", "结发妻子王弗"]
    main = ["扉页", "版权信息", "写在前面", "文前", "译文"] + stories
    tail = ["原文"] + stories
    return main + tail


class TestRepeatSectionStart(unittest.TestCase):
    def test_detects_volume_repeated_tail(self):
        catalog = bilingual_catalog()
        self.assertEqual(catalog[4], "译文")
        self.assertEqual(catalog.index("原文"), 9)
        self.assertEqual(
            export_precise.repeat_section_start(catalog), catalog.index("原文"))

    def test_ignores_single_trailing_duplicate(self):
        """书末恰好同名的相邻两项（短诗、词牌重复）不当成新卷。"""
        catalog = ["扉页", "踏莎行", "相见欢", "相见欢"]
        self.assertIsNone(export_precise.repeat_section_start(catalog))

    def test_ignores_catalog_without_repeat_tail(self):
        catalog = ["扉页", "迎神", "奠币", "酌献", "送神"]
        self.assertIsNone(export_precise.repeat_section_start(catalog))


class TestSessionCatalogView(unittest.TestCase):
    def test_tail_anchor_gets_tail_section(self):
        catalog = bilingual_catalog()
        volume_idx = catalog.index("原文")
        view = export_precise.session_catalog_view(catalog, volume_idx)
        self.assertEqual(view, catalog[volume_idx:])
        self.assertEqual(view[0], "原文")
        self.assertEqual(
            len({export_precise.chapter_title_match_key(t) for t in view}),
            len(view),
        )

    def test_main_anchor_gets_main_section(self):
        """主段视图不含后段卷名：主段末尾正常收尾，不会飘进原文卷。"""
        catalog = bilingual_catalog()
        view = export_precise.session_catalog_view(
            catalog, catalog.index("原文") - 1)
        self.assertEqual(view, catalog[:catalog.index("原文")])
        self.assertNotIn("原文", view)
        self.assertTrue(
            export_precise.is_export_terminal_chapter("结发妻子王弗", view)
        )

    def test_missing_repeat_tail_keeps_deduped_view(self):
        """没有重复后段时行为不变：仍用按标题去重的视图。"""
        catalog = ["扉页", "迎神", "奠币", "迎神", "送神", "送神"]
        self.assertEqual(
            export_precise.session_catalog_view(catalog, 0),
            export_precise.dedupe_catalog_titles(catalog),
        )

    def test_repeated_titles_inside_tail_falls_back(self):
        """后段内部标题仍重复（词牌大量重复的曲选）时不切段，保持原视图。"""
        catalog = ["扉页", "一半儿", "山坡羊", "一半儿", "山坡羊", "一半儿"]
        self.assertEqual(
            export_precise.session_catalog_view(catalog, 3),
            export_precise.dedupe_catalog_titles(catalog),
        )


class TestTailSectionDecisions(unittest.TestCase):
    """后段锚点下，末章判定/推进目标必须落到原文卷内部（事故的直接原因）。"""

    def setUp(self):
        self.catalog = bilingual_catalog()
        self.view = export_precise.session_catalog_view(
            self.catalog, self.catalog.index("原文"))

    def test_volume_row_is_not_terminal(self):
        self.assertFalse(
            export_precise.is_last_catalog_chapter("原文", self.view)
        )
        self.assertFalse(
            export_precise.is_export_terminal_chapter("原文", self.view)
        )

    def test_advance_target_moves_into_volume(self):
        self.assertEqual(
            export_precise.resolve_stale_advance_target("原文", self.view),
            "家族往事",
        )

    def test_overrun_guard_stays_armed_at_volume_end(self):
        """段内末章仍受「越位灌末章」防护——防护本身不能因为分段失效。"""
        self.assertTrue(
            export_precise.is_export_terminal_chapter(
                "结发妻子王弗", self.view)
        )


class TestSessionAnchorIndex(unittest.TestCase):
    def test_anchor_is_next_unexported_row(self):
        import json
        import tempfile

        catalog = bilingual_catalog()
        volume_idx = catalog.index("原文")
        with tempfile.TemporaryDirectory() as tmp:
            book_dir = Path(tmp)
            md_dir = book_dir / "chapters"
            raw_dir = book_dir / "raw"
            md_dir.mkdir(parents=True)
            raw_dir.mkdir(parents=True)
            # 译文卷（含卷名、81 篇）已导出，接下来该抓卷名「原文」那一项
            for i, title in enumerate(catalog[:volume_idx], 1):
                (md_dir / f"{i:04d}.md").write_text(
                    f"# {title}\n\n正文。\n", encoding="utf-8")
                (raw_dir / f"{i:04d}.json").write_text(
                    json.dumps({"title": title, "images": [], "text_len": 3}),
                    encoding="utf-8")
            self.assertEqual(
                export_precise.session_anchor_index(str(md_dir), catalog),
                volume_idx,
            )
            self.assertEqual(
                export_precise.session_catalog_view(
                    catalog,
                    export_precise.session_anchor_index(str(md_dir), catalog),
                ),
                catalog[volume_idx:],
            )

    def test_fresh_book_anchors_at_zero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            md_dir = Path(tmp) / "chapters"
            md_dir.mkdir(parents=True)
            self.assertEqual(
                export_precise.session_anchor_index(
                    str(md_dir), bilingual_catalog()),
                0,
            )


if __name__ == "__main__":
    unittest.main()
