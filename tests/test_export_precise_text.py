"""export_precise 章节正文识别：词牌换行与章节边界切分。"""
import unittest

import export_precise


class TestRenderChapterMdLineBreaks(unittest.TestCase):
    def test_cipai_title_stays_on_own_paragraph(self):
        """词牌名（如「菩萨蛮」）应单独成段，不与首句粘连。"""
        blocks = [
            {"type": "text", "text": "菩萨蛮"},
            {"type": "text", "text": "平林漠漠烟如织，寒山一带伤心碧。"},
            {"type": "text", "text": "暝色入高楼，有人楼上愁。"},
        ]
        body, _ = export_precise.render_chapter_md("李白", blocks, 5)
        self.assertIn("菩萨蛮\n\n平林漠漠烟如织", body)
        self.assertNotIn("菩萨蛮平林漠漠烟如织", body)

    def test_soft_wrap_still_merges_long_lines(self):
        """canvas 软折行：上一行足够长且无句末标点时仍应接续。"""
        blocks = [
            {"type": "text", "text": "这是一句故意写得很长以便模拟阅读器软折行的正文前半没有句末"},
            {"type": "text", "text": "标点所以应当接到下一行组成完整段落。"},
        ]
        body, _ = export_precise.render_chapter_md("导言", blocks, 1)
        self.assertIn(
            "这是一句故意写得很长以便模拟阅读器软折行的正文前半没有句末标点所以应当接到下一行组成完整段落。",
            body,
        )

    def test_author_and_cipai_not_glued(self):
        blocks = [
            {"type": "text", "text": "张志和"},
            {"type": "text", "text": "渔父"},
            {"type": "text", "text": "西塞山前白鹭飞，桃花流水鳜鱼肥。"},
        ]
        body, _ = export_precise.render_chapter_md("张志和", blocks, 6)
        # 章标题行本身被跳过；词牌与正文不粘连
        self.assertIn("渔父\n\n西塞山前白鹭飞", body)
        self.assertNotIn("渔父西塞山前", body)


class TestSplitBlocksAtChapterStart(unittest.TestCase):
    def test_split_on_exact_next_title_line(self):
        blocks = [
            {"type": "text", "text": "个人的悲情又算得了什么呢？"},
            {"type": "text", "text": "张志和"},
            {"type": "text", "text": "渔父"},
            {"type": "text", "text": "西塞山前白鹭飞，桃花流水鳜鱼肥。"},
            {"type": "text", "text": "【赏析】"},
            {"type": "text", "text": "东汉的严子陵，曾经和刘秀同窗。"},
        ]
        before, after = export_precise.split_blocks_at_chapter_start(blocks, "张志和")
        self.assertEqual([b["text"] for b in before], ["个人的悲情又算得了什么呢？"])
        self.assertEqual(after[0]["text"], "张志和")
        self.assertEqual(after[-1]["text"], "东汉的严子陵，曾经和刘秀同窗。")

    def test_split_on_title_prefixed_line(self):
        """标题与词牌偶发同一 canvas 行时，按前缀切到下一章。"""
        blocks = [
            {"type": "text", "text": "终究容不下每一位小小的你我。"},
            {"type": "text", "text": "张志和渔父西塞山前白鹭飞，桃花流水鳜鱼肥。"},
        ]
        before, after = export_precise.split_blocks_at_chapter_start(blocks, "张志和")
        self.assertEqual(len(before), 1)
        self.assertEqual(after[0]["text"], "张志和渔父西塞山前白鹭飞，桃花流水鳜鱼肥。")

    def test_no_split_on_inline_mention(self):
        """正文中提及下一作者名不应误切。"""
        blocks = [
            {"type": "text", "text": "南唐中主李璟曾经如此调戏他的宰相。"},
            {"type": "text", "text": "足见二人关系。"},
        ]
        before, after = export_precise.split_blocks_at_chapter_start(blocks, "李璟")
        self.assertEqual(before, blocks)
        self.assertEqual(after, [])

    def test_next_catalog_title_helper(self):
        catalog = ["导言", "李白", "张志和", "刘禹锡"]
        self.assertEqual(export_precise.next_catalog_title(catalog, "李白"), "张志和")
        self.assertEqual(export_precise.next_catalog_title(catalog, "刘禹锡"), None)
        self.assertEqual(export_precise.next_catalog_title(catalog, "不存在"), None)


if __name__ == "__main__":
    unittest.main()
