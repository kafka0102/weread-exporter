"""export_precise 指针超前防护：逻辑章跑到前面时不得继续往里灌正文。"""
import unittest

import export_precise


def blocks(*lines):
    return [{"type": "text", "text": s} for s in lines]


class TestEarlierCatalogChapterInBlocks(unittest.TestCase):
    CATALOG = [
        "扉页",
        "版权信息",
        "总序",
        "代前言",
        "学术史中的诗歌",
        "在汉代诗学背景下看曹丕、曹植诗歌之比较",
        "历史阅读与诗歌创作",
        "历史记忆与左思《咏史》",
        "诗心与史胆的融合",
    ]

    def test_detects_earlier_chapter_when_pointer_ran_ahead(self):
        """逻辑章=诗心与史胆的融合，正文却回到「学术史中的诗歌」→ 判定指针超前。"""
        found = export_precise.earlier_catalog_chapter_in_blocks(
            blocks(
                "郭晏如和吴湛博士的两篇论文从个案分析来理解和探讨“诗与史”的关系",
                "学术史中的诗歌",
                "汪春泓和范子烨教授的两篇论文是从学术史的角度分别考察",
            ),
            self.CATALOG,
            "诗心与史胆的融合",
            "学术史中的诗歌",
        )
        self.assertEqual(found, "学术史中的诗歌")

    def test_ignores_when_header_not_behind(self):
        """顶栏没落后（或无法定位）时不回退，避免正常正文提及引起误退。"""
        blk = blocks("学术史中的诗歌", "汪春泓和范子烨教授的两篇论文")
        self.assertEqual(
            export_precise.earlier_catalog_chapter_in_blocks(
                blk, self.CATALOG, "诗心与史胆的融合", "诗心与史胆的融合"
            ),
            "",
        )
        self.assertEqual(
            export_precise.earlier_catalog_chapter_in_blocks(
                blk, self.CATALOG, "诗心与史胆的融合", None
            ),
            "",
        )

    def test_ignores_adjacent_previous_chapter(self):
        """只早 1 位的不算（可能是相邻章的正常重叠）。"""
        found = export_precise.earlier_catalog_chapter_in_blocks(
            blocks("历史记忆与左思《咏史》"),
            self.CATALOG,
            "诗心与史胆的融合",
            "历史记忆与左思《咏史》",
        )
        self.assertEqual(found, "")

    def test_ignores_short_titles(self):
        """太短的章名（≤3 字压缩键）在正文里天然重复，不能当证据。"""
        catalog = ["卷一", "序", "文", "甲篇正文", "乙篇正文"]
        found = export_precise.earlier_catalog_chapter_in_blocks(
            blocks("序", "文"), catalog, "乙篇正文", "序"
        )
        self.assertEqual(found, "")

    def test_matches_title_with_short_glued_tail(self):
        """章名后粘作者署名（canvas 常见）也要认出来。"""
        found = export_precise.earlier_catalog_chapter_in_blocks(
            blocks("在汉代诗学背景下看曹丕、曹植诗歌之比较汪春泓（香港岭南大学）"),
            self.CATALOG,
            "诗心与史胆的融合",
            "学术史中的诗歌",
        )
        self.assertEqual(found, "在汉代诗学背景下看曹丕、曹植诗歌之比较")

    def test_returns_closest_earlier_chapter(self):
        """缓冲里出现多个更早章名时，取最靠后（最接近当前章）那个。"""
        found = export_precise.earlier_catalog_chapter_in_blocks(
            blocks("学术史中的诗歌", "历史阅读与诗歌创作"),
            self.CATALOG,
            "诗心与史胆的融合",
            "学术史中的诗歌",
        )
        self.assertEqual(found, "历史阅读与诗歌创作")

    def test_ignores_heading_far_from_header(self):
        """canvas 残留的旧章标题不能把指针拽回去（刘克庄诗选事故的形状）。"""
        catalog = ["扉页", "版权信息"] + [
            f"第{i}首 诗题甲乙丙丁" for i in range(2, 40)
        ]
        found = export_precise.earlier_catalog_chapter_in_blocks(
            blocks(catalog[30]),
            catalog,
            catalog[38],
            catalog[37],
        )
        self.assertEqual(found, "")

    def test_still_accepts_heading_near_header(self):
        """顶栏附近章的章首出现在缓冲里 → 仍是「指针超前」证据。"""
        catalog = ["扉页", "版权信息"] + [
            f"第{i}首 诗题甲乙丙丁" for i in range(2, 40)
        ]
        found = export_precise.earlier_catalog_chapter_in_blocks(
            blocks(catalog[35]),
            catalog,
            catalog[38],
            catalog[37],
        )
        self.assertEqual(found, catalog[35])


if __name__ == "__main__":
    unittest.main()
