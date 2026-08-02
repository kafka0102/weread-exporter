"""export_precise 章节正文识别：词牌换行、章节边界、双页拆分。"""
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
        # 作者署名与章名相同时也要保留；词牌与正文不粘连
        self.assertIn("张志和\n\n渔父\n\n西塞山前白鹭飞", body)
        self.assertNotIn("渔父西塞山前", body)

    def test_keeps_author_line_same_as_chapter_title(self):
        """词牌下的作者行即使与章标题相同也必须保留。"""
        blocks = [
            {"type": "text", "text": "菩萨蛮"},
            {"type": "text", "text": "李白"},
            {"type": "text", "text": "平林漠漠烟如织，寒山一带伤心碧。"},
        ]
        body, _ = export_precise.render_chapter_md("李白", blocks, 5)
        self.assertIn("菩萨蛮\n\n李白\n\n平林漠漠烟如织", body)


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




class TestCatalogTitleCleaning(unittest.TestCase):
    def test_normalize_strips_progress_suffix(self):
        self.assertEqual(
            export_precise.normalize_catalog_title("王国维当前读到 99%"),
            "王国维",
        )
        self.assertEqual(export_precise.normalize_catalog_title("#"), "")

    def test_clean_catalog_titles_dedupes(self):
        titles = export_precise.clean_catalog_titles(
            ["版权信息", "版权信息", "导言", "王国维当前读到 99%", ""]
        )
        self.assertEqual(titles, ["版权信息", "导言", "王国维"])

    def test_catalog_index_fuzzy(self):
        catalog = ["导言", "李白", "张志和"]
        self.assertEqual(export_precise.catalog_index(catalog, "李白"), 1)
        self.assertEqual(export_precise.catalog_index(catalog, "不存在"), None)

    def test_display_chapter_title_fallback(self):
        self.assertEqual(export_precise.display_chapter_title("", 3), "0003")
        self.assertEqual(export_precise.display_chapter_title("李白", 3), "李白")



class TestHeaderChapterProgress(unittest.TestCase):
    """顶栏不得把已内容切章前进的目录进度回退。"""

    def test_should_not_follow_earlier_header_title(self):
        catalog = [
            "孟浩然 九首",
            "春晓",
            "王维 二十七首",
            "渭川田家",
            "宿郑州",
            "西施咏",
            "桃源行",
            "陇头吟",
        ]
        # 内容切章已到桃源行，顶栏仍停在作者小节名
        self.assertFalse(
            export_precise.should_follow_header_title(
                catalog, "桃源行", "王维 二十七首"
            )
        )
        self.assertFalse(
            export_precise.should_follow_header_title(
                catalog, "渭川田家", "王维 二十七首"
            )
        )

    def test_should_follow_forward_header_title(self):
        catalog = ["导言", "李白", "张志和", "刘禹锡"]
        self.assertTrue(
            export_precise.should_follow_header_title(catalog, "导言", "李白")
        )
        # 仅允许紧邻下一章；跨章顶栏不得直接跟随（否则会丢中间章）
        self.assertFalse(
            export_precise.should_follow_header_title(catalog, "李白", "刘禹锡")
        )
        self.assertTrue(
            export_precise.should_follow_header_title(catalog, "李白", "张志和")
        )
        self.assertFalse(
            export_precise.should_follow_header_title(catalog, "李白", "李白")
        )
        self.assertFalse(
            export_precise.should_follow_header_title(catalog, "张志和", "李白")
        )

    def test_should_not_follow_multi_skip_header_title(self):
        """顶栏从临洞庭湖直接跳到王维时，不得一口吞掉中间孟浩然诸篇。"""
        catalog = [
            "晚泊浔阳望香炉峰",
            "临洞庭湖赠张丞相",
            "广陵别薛八",
            "春晓",
            "王维 二十七首",
            "渭川田家",
        ]
        self.assertFalse(
            export_precise.should_follow_header_title(
                catalog, "临洞庭湖赠张丞相", "王维 二十七首"
            )
        )
        self.assertTrue(
            export_precise.should_follow_header_title(
                catalog, "临洞庭湖赠张丞相", "广陵别薛八"
            )
        )
        self.assertTrue(
            export_precise.should_follow_header_title(
                catalog, "春晓", "王维 二十七首"
            )
        )
    def test_fingerprint_stable_for_same_blocks(self):
        blocks = [
            {"type": "text", "text": "渭川田家"},
            {"type": "text", "text": "斜光照墟落，穷巷牛羊归。"},
        ]
        a = export_precise.chapter_blocks_fingerprint("渭川田家", blocks)
        b = export_precise.chapter_blocks_fingerprint("渭川田家", list(blocks))
        c = export_precise.chapter_blocks_fingerprint("宿郑州", blocks)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


    def test_page_blocks_fingerprint_ignores_identical_pages(self):
        blocks_a = [
            {"type": "text", "text": "渭川田家"},
            {"type": "text", "text": "斜光照墟落，穷巷牛羊归。"},
        ]
        blocks_b = [
            {"type": "text", "text": "渭川田家"},
            {"type": "text", "text": "斜光照墟落，穷巷牛羊归。"},
        ]
        blocks_c = [
            {"type": "text", "text": "宿郑州"},
            {"type": "text", "text": "朝与周人辞，暮投郑人宿。"},
        ]
        fa = export_precise.page_blocks_fingerprint(blocks_a)
        fb = export_precise.page_blocks_fingerprint(blocks_b)
        fc = export_precise.page_blocks_fingerprint(blocks_c)
        self.assertEqual(fa, fb)
        self.assertNotEqual(fa, fc)
        self.assertEqual(export_precise.page_blocks_fingerprint([]), "")


class TestReaderChapterSync(unittest.TestCase):
    """内容切章后必须确认阅读器已落在目标章。"""

    def test_requires_sync_when_reader_still_on_previous_chapter(self):
        catalog = ["从军行", "卢照邻 一首", "长安古意"]
        self.assertTrue(
            export_precise.reader_needs_chapter_sync(
                catalog, "长安古意", "卢照邻 一首"
            )
        )

    def test_does_not_sync_when_reader_is_already_on_target_chapter(self):
        catalog = ["从军行", "卢照邻 一首", "长安古意"]
        self.assertFalse(
            export_precise.reader_needs_chapter_sync(
                catalog, "长安古意", "长安古意"
            )
        )


class TestFindChapterSplit(unittest.TestCase):
    def test_empty_current_finds_first_catalog_title_in_order(self):
        blocks = [
            {"type": "text", "text": "版权页正文"},
            {"type": "text", "text": "作者简介"},
            {"type": "text", "text": "陈引驰"},
            {"type": "text", "text": "导言"},
        ]
        catalog = ["版权信息", "作者简介", "导言", "李白"]
        found = export_precise.find_chapter_split(blocks, catalog, "")
        self.assertIsNotNone(found)
        nxt, before, after = found
        self.assertEqual(nxt, "作者简介")
        self.assertEqual(before[0]["text"], "版权页正文")
        self.assertEqual(after[0]["text"], "作者简介")

    def test_known_current_only_matches_immediate_next(self):
        blocks = [
            {"type": "text", "text": "导言正文"},
            {"type": "text", "text": "李白"},
            {"type": "text", "text": "菩萨蛮"},
        ]
        catalog = ["导言", "李白", "张志和"]
        found = export_precise.find_chapter_split(blocks, catalog, "导言")
        self.assertEqual(found[0], "李白")
        self.assertEqual(found[1][0]["text"], "导言正文")

    def test_infer_title_for_before_when_current_empty(self):
        catalog = ["版权信息", "作者简介", "导言"]
        self.assertEqual(
            export_precise.infer_title_for_blocks_before("作者简介", catalog, ""),
            "版权信息",
        )

    def test_is_last_catalog_chapter(self):
        catalog = ["导言", "李白", "秋瑾"]
        self.assertTrue(export_precise.is_last_catalog_chapter("秋瑾", catalog))
        self.assertFalse(export_precise.is_last_catalog_chapter("李白", catalog))


class TestDualCanvasSplit(unittest.TestCase):
    """双页 canvas 局部坐标不得按 y 交错合并。"""

    def _char(self, t, x, y, cl, s=18):
        return {"t": t, "x": x, "y": y, "cl": cl, "ct": 73, "s": s}

    def test_group_chars_by_canvas_left_to_right(self):
        chars = [
            self._char("左", 10, 100, 190),
            self._char("右", 10, 100, 649),
            self._char("页", 28, 100, 190),
            self._char("页", 28, 100, 649),
        ]
        pages = export_precise.group_chars_by_canvas(chars)
        self.assertEqual(len(pages), 2)
        left = "".join(c["t"] for c in sorted(pages[0], key=lambda c: c["x"]))
        right = "".join(c["t"] for c in sorted(pages[1], key=lambda c: c["x"]))
        self.assertEqual(left, "左页")
        self.assertEqual(right, "右页")


    def test_group_chars_by_canvas_id_orders_left_to_right(self):
        # cl 都是 0 时旧逻辑会交错；cid 应仍能左右拆开
        chars = []
        for i, ch in enumerate("左页字"):
            chars.append({"t": ch, "x": 10 + i * 18, "y": 100, "cl": 0, "cid": 2})
        for i, ch in enumerate("右页字"):
            chars.append({"t": ch, "x": 10 + i * 18, "y": 100, "cl": 0, "cid": 1})
        # cid=1 若 cl 同为 0，按 cid 次序不稳定；给 cid1 更大 cl 模拟右页
        for c in chars:
            if c["cid"] == 1:
                c["cl"] = 600
            else:
                c["cl"] = 100
        pages = export_precise.group_chars_by_canvas(chars)
        self.assertEqual(len(pages), 2)
        left = "".join(c["t"] for c in sorted(pages[0], key=lambda c: c["x"]))
        right = "".join(c["t"] for c in sorted(pages[1], key=lambda c: c["x"]))
        self.assertEqual(left, "左页字")
        self.assertEqual(right, "右页字")

    def test_reader_chapter_matches_allows_next_header(self):
        catalog = ["秦王破阵", "玄武喋血", "致治贞观"]
        self.assertTrue(
            export_precise.reader_chapter_matches("玄武喋血", "玄武喋血", catalog)
        )
        self.assertTrue(
            export_precise.reader_chapter_matches("致治贞观", "玄武喋血", catalog)
        )
        self.assertFalse(
            export_precise.reader_chapter_matches("秦王破阵", "玄武喋血", catalog)
        )

    def test_build_page_blocks_does_not_interleave_spread(self):
        # 模拟：左页正文「至异…」，右页正文「萧史…」同一 y
        left_text = "至异"
        right_text = "萧史"
        chars = []
        for i, ch in enumerate(left_text):
            chars.append(self._char(ch, 10 + i * 18, 100, 190))
        for i, ch in enumerate(right_text):
            chars.append(self._char(ch, 10 + i * 18, 100, 649))
        for i, ch in enumerate("菩萨蛮"):
            chars.append(self._char(ch, 140 + i * 27, 200, 649, s=27))
        for i, ch in enumerate("李白"):
            chars.append(self._char(ch, 152 + i * 29, 240, 649, s=28.8))
        for i, ch in enumerate("平林"):
            chars.append(self._char(ch, 10 + i * 18, 280, 649, s=18))

        rects = [
            {"top": 73, "left": 190, "w": 361, "h": 770},
            {"top": 73, "left": 649, "w": 361, "h": 770},
        ]
        blocks = export_precise.build_page_blocks(chars, [], rects, set())
        texts = [b["text"] for b in blocks if b["type"] == "text"]
        joined = "\n".join(texts)
        self.assertNotIn("至萧异史", joined)
        self.assertIn("至异", texts)
        self.assertIn("萧史", texts)
        self.assertIn("菩萨蛮", texts)
        self.assertIn("李白", texts)

        body, _ = export_precise.render_chapter_md("李白", blocks, 5)
        self.assertIn("菩萨蛮\n\n李白", body)
        self.assertIn("李白\n\n平林", body)
        self.assertNotIn("菩萨蛮平林", body)
        self.assertNotIn("菩萨蛮李白平林", body)



class TestShortTitleChapterStart(unittest.TestCase):
    """短目录名不得前缀命中正文行。"""

    def test_short_title_not_prefix_of_prose(self):
        self.assertFalse(
            export_precise.is_chapter_start_text("云破月来花弄影", "云")
        )
        self.assertFalse(
            export_precise.is_chapter_start_text("云想衣裳花想容", "云")
        )
        self.assertFalse(
            export_precise.is_chapter_start_text("春日迟迟", "春日")
        )
        self.assertFalse(
            export_precise.is_chapter_start_text("感遇陈子昂", "感遇")
        )
        self.assertFalse(
            export_precise.is_chapter_start_text("雪消门外千山绿", "雪")
        )
        self.assertTrue(export_precise.is_chapter_start_text("云", "云"))
        self.assertTrue(export_precise.is_chapter_start_text("春日", "春日"))
        self.assertTrue(export_precise.is_chapter_start_text("感遇", "感遇"))

    def test_long_title_space_and_glue_still_ok(self):
        self.assertTrue(
            export_precise.is_chapter_start_text("沈佺期三首", "沈佺期 三首")
        )
        # 较长标题允许短粘连
        self.assertTrue(
            export_precise.is_chapter_start_text(
                "送杜少府之任蜀川五律", "送杜少府之任蜀川"
            )
        )

    def test_find_split_ignores_cloud_prose(self):
        catalog = ["来鹄 二首", "云", "蚕妇"]
        blocks = [
            {"type": "text", "text": "来鹄二首"},
            {"type": "text", "text": "云破月来花弄影"},
            {"type": "text", "text": "注释"},
        ]
        found = export_precise.find_chapter_split(blocks, catalog, "来鹄 二首")
        self.assertIsNone(found)
        blocks2 = [
            {"type": "text", "text": "来鹄正文"},
            {"type": "text", "text": "云"},
            {"type": "text", "text": "千形万象竟还空"},
        ]
        found2 = export_precise.find_chapter_split(blocks2, catalog, "来鹄 二首")
        self.assertIsNotNone(found2)
        self.assertEqual(found2[0], "云")

    def test_catalog_index_no_short_false_hit(self):
        catalog = ["春日", "野望", "春日京中有怀", "云", "宿云门寺阁"]
        self.assertEqual(export_precise.catalog_index(catalog, "春日"), 0)
        self.assertEqual(
            export_precise.catalog_index(catalog, "春日京中有怀"), 2
        )
        self.assertEqual(export_precise.catalog_index(catalog, "云"), 3)
        # 不应把含「云」的长标题解析成短章「云」
        self.assertEqual(
            export_precise.catalog_index(catalog, "宿云门寺阁"), 4
        )




class TestHeaderMultiAheadRecovery(unittest.TestCase):
    """顶栏跨章且正文仍在灌入时，必须能识别越位并截断后续章。"""

    def test_catalog_index_delta(self):
        catalog = [
            "舟中晓望",
            "春晓",
            "王维 二十七首",
            "渭川田家",
        ]
        self.assertEqual(
            export_precise.catalog_index_delta(catalog, "舟中晓望", "渭川田家"),
            3,
        )
        self.assertEqual(
            export_precise.catalog_index_delta(catalog, "舟中晓望", "春晓"),
            1,
        )
        self.assertEqual(
            export_precise.catalog_index_delta(catalog, "舟中晓望", "舟中晓望"),
            0,
        )
        self.assertIsNone(
            export_precise.catalog_index_delta(catalog, "舟中晓望", "不存在")
        )

    def test_skipped_next_chapter_evidence(self):
        catalog = [
            "舟中晓望",
            "春晓",
            "王维 二十七首",
            "渭川田家",
        ]
        blocks = [
            {"type": "text", "text": "舟中晓望正文注释"},
            {"type": "text", "text": "王维 二十七首"},
            {"type": "text", "text": "渭川田家"},
            {"type": "text", "text": "斜光照墟落"},
        ]
        self.assertEqual(
            export_precise.skipped_next_chapter_evidence(
                blocks, catalog, "舟中晓望"
            ),
            "王维 二十七首",
        )
        # 下一章若在正文出现，则不算「越过」
        blocks_with_next = [
            {"type": "text", "text": "舟中晓望正文"},
            {"type": "text", "text": "春晓"},
            {"type": "text", "text": "春眠不觉晓"},
            {"type": "text", "text": "王维 二十七首"},
        ]
        self.assertEqual(
            export_precise.skipped_next_chapter_evidence(
                blocks_with_next, catalog, "舟中晓望"
            ),
            "",
        )

    def test_trim_blocks_before_future_catalog(self):
        catalog = [
            "舟中晓望",
            "春晓",
            "王维 二十七首",
            "渭川田家",
        ]
        blocks = [
            {"type": "text", "text": "挂席东南望"},
            {"type": "text", "text": "王维 二十七首"},
            {"type": "text", "text": "渭川田家"},
            {"type": "text", "text": "斜光照墟落"},
        ]
        trimmed, hit = export_precise.trim_blocks_before_future_catalog(
            blocks, catalog, "舟中晓望", min_ahead=1
        )
        self.assertEqual(hit, "王维 二十七首")
        self.assertEqual([b["text"] for b in trimmed], ["挂席东南望"])

    def test_find_chapter_split_still_only_next(self):
        """内容切章仍只认紧邻下一章，避免误吞中间项。"""
        catalog = [
            "舟中晓望",
            "春晓",
            "王维 二十七首",
            "渭川田家",
        ]
        blocks = [
            {"type": "text", "text": "舟中晓望正文"},
            {"type": "text", "text": "王维 二十七首"},
            {"type": "text", "text": "作者简介"},
        ]
        self.assertIsNone(
            export_precise.find_chapter_split(blocks, catalog, "舟中晓望")
        )



class TestContentOverrunSplit(unittest.TestCase):
    """线性翻页：下一章缺失但后续章出现时，按正文切开且不依赖目录点击。"""

    def test_overrun_when_next_missing(self):
        catalog = ["舟中晓望", "春晓", "王维 二十七首", "渭川田家"]
        blocks = [
            {"type": "text", "text": "挂席东南望"},
            {"type": "text", "text": "王维 二十七首"},
            {"type": "text", "text": "作者简介"},
        ]
        hit = export_precise.content_overrun_split(blocks, catalog, "舟中晓望")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "王维 二十七首")
        self.assertEqual(hit[1][0]["text"], "挂席东南望")

    def test_no_overrun_when_next_present(self):
        catalog = ["舟中晓望", "春晓", "王维 二十七首", "渭川田家"]
        blocks = [
            {"type": "text", "text": "挂席东南望"},
            {"type": "text", "text": "春晓"},
            {"type": "text", "text": "春眠不觉晓"},
            {"type": "text", "text": "王维 二十七首"},
        ]
        self.assertIsNone(
            export_precise.content_overrun_split(blocks, catalog, "舟中晓望")
        )

if __name__ == "__main__":
    unittest.main()

class TestChapterStartSpaceTolerance(unittest.TestCase):
    def test_chapter_start_ignores_spaces(self):
        self.assertTrue(
            export_precise.is_chapter_start_text("沈佺期三首", "沈佺期 三首")
        )
        self.assertTrue(
            export_precise.is_chapter_start_text("沈佺期 三首", "沈佺期 三首")
        )

    def test_find_split_with_spaceless_canvas_title(self):
        catalog = ["赠苏绾书记", "沈佺期 三首", "杂诗"]
        blocks = [
            {"type": "text", "text": "赠苏正文"},
            {"type": "text", "text": "沈佺期三首"},
            {"type": "text", "text": "作者简介"},
        ]
        found = export_precise.find_chapter_split(blocks, catalog, "赠苏绾书记")
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "沈佺期 三首")

    def test_find_split_does_not_skip_missing_next(self):
        """中间目录项未出现在正文时，不得跳切到更后面的章。"""
        catalog = ["赠苏绾书记", "沈佺期 三首", "杂诗"]
        blocks = [
            {"type": "text", "text": "赠苏正文"},
            {"type": "text", "text": "杂诗"},
            {"type": "text", "text": "闻道黄龙戍"},
        ]
        found = export_precise.find_chapter_split(blocks, catalog, "赠苏绾书记")
        self.assertIsNone(found)

    def test_chapter_start_strips_zero_width_space(self):
        """canvas 标题中的 U+200B 不得阻断切章。"""
        title = "士与商：“贱商之子”李白与唐代政经制度"
        glued = "士与商：​“贱商之子”李白与唐代政经制度《大唐李白》出版以来"
        self.assertTrue(export_precise.is_chapter_start_text(glued, title))
        self.assertEqual(
            export_precise.normalize_catalog_title(
                "士与商：​“贱商之子”李白与唐代政经制度"
            ),
            title,
        )
        self.assertEqual(
            export_precise.compact_title_key("士与商：​“贱商之子”李白与唐代政经制度"),
            export_precise.compact_title_key(title),
        )

    def test_split_long_title_across_canvas_lines(self):
        """长目录标题被拆成多行时仍应在首行处切开。"""
        title = "士与商：“贱商之子”李白与唐代政经制度"
        catalog = [
            "附录 李白的学习年代与漫游年代",
            title,
            "仙与凡：“太白星”李白与道教上清派理想",
        ]
        blocks = [
            {"type": "text", "text": "从而解决成长小说的故事性、虚构性与时间的问题。"},
            {"type": "text", "text": "士与商：​“贱商之子”"},
            {
                "type": "text",
                "text": "李白与唐代政经制度《大唐李白》出版以来有三大讨论焦点",
            },
        ]
        before, after = export_precise.split_blocks_at_chapter_start(blocks, title)
        self.assertEqual(len(before), 1)
        self.assertEqual(after[0]["text"], "士与商：​“贱商之子”")
        found = export_precise.find_chapter_split(
            blocks, catalog, "附录 李白的学习年代与漫游年代"
        )
        self.assertIsNotNone(found)
        self.assertEqual(found[0], title)


class RunawayChapterThresholdTests(unittest.TestCase):
    def test_soft_threshold_allows_long_multi_page_chapter(self):
        # 散文长章常见 700+ 行缓冲；旧阈值 600 会误杀
        self.assertFalse(export_precise.is_soft_runaway_chapter(721, 4))
        self.assertFalse(export_precise.is_hard_runaway_chapter(721, 4))

    def test_soft_threshold_triggers_only_when_large(self):
        self.assertTrue(
            export_precise.is_soft_runaway_chapter(
                export_precise.RUNAWAY_CHAPTER_LINES, 1
            )
        )
        self.assertTrue(
            export_precise.is_soft_runaway_chapter(
                10, export_precise.RUNAWAY_CHAPTER_PAGES
            )
        )
        self.assertFalse(
            export_precise.is_hard_runaway_chapter(
                export_precise.RUNAWAY_CHAPTER_LINES, 1
            )
        )

    def test_hard_threshold_for_dirty_discard_only(self):
        self.assertTrue(
            export_precise.is_hard_runaway_chapter(
                export_precise.HARD_RUNAWAY_CHAPTER_LINES, 1
            )
        )
        self.assertTrue(
            export_precise.is_hard_runaway_chapter(
                1, export_precise.HARD_RUNAWAY_CHAPTER_PAGES
            )
        )

    def test_chapter_text_line_count(self):
        blocks = [
            {"type": "text", "text": "a"},
            {"type": "img", "src": "x"},
            {"type": "text", "text": "b"},
        ]
        self.assertEqual(export_precise.chapter_text_line_count(blocks), 2)
        self.assertEqual(export_precise.chapter_text_line_count([]), 0)


class ChapterPageDedupeTests(unittest.TestCase):
    def test_chapter_text_line_set(self):
        blocks = [
            {"type": "text", "text": "hello world line"},
            {"type": "img", "src": "a"},
            {"type": "text", "text": "  "},
            {"type": "text", "text": "second long line here"},
        ]
        self.assertEqual(
            export_precise.chapter_text_line_set(blocks),
            {"hello world line", "second long line here"},
        )

    def test_should_skip_long_duplicate_not_short(self):
        seen = {"这是一行足够长的正文会被去重"}
        self.assertTrue(
            export_precise.should_skip_chapter_line(
                "这是一行足够长的正文会被去重", seen
            )
        )
        # 短行允许重复
        self.assertFalse(
            export_precise.should_skip_chapter_line("是的。", {"是的。"})
        )
        # 未见过的长行不跳过
        self.assertFalse(
            export_precise.should_skip_chapter_line("全新的一行长正文内容啊", seen)
        )


class TestCatalogIndexTopBarCombined(unittest.TestCase):
    def test_catalog_index_book_title_plus_chapter(self):
        """顶栏「书名 章名」应解析到目录章。"""
        catalog = [
            "晋阳宫史",
            "李渊来路",
            "晋阳宫变",
            "开国大唐",
            "煌煌太宗业，树立甚宏达——从玄武门之变到贞观之治",
        ]
        raw = "三百年长歌行：唐诗中的大唐兴亡与悲欢 开国大唐"
        self.assertEqual(export_precise.catalog_index(catalog, raw), 3)
        self.assertEqual(
            export_precise.resolve_chapter_title(raw, catalog),
            "开国大唐",
        )

    def test_classify_reader_paging_mode_vertical_vs_horizontal(self):
        vertical = {
            "scrollHeight": 14924,
            "innerHeight": 760,
            "canvases": [
                {"t": -6570, "l": 336, "w": 798, "h": 1973},
                {"t": -4566, "l": 336, "w": 798, "h": 1958},
            ],
        }
        horizontal = {
            "scrollHeight": 760,
            "innerHeight": 760,
            "canvases": [
                {"t": 73, "l": 217, "w": 469, "h": 630},
                {"t": 73, "l": 784, "w": 469, "h": 630},
            ],
        }
        self.assertEqual(
            export_precise.classify_reader_paging_mode(vertical),
            "vertical_scroll",
        )
        self.assertEqual(
            export_precise.classify_reader_paging_mode(horizontal),
            "horizontal",
        )


class TestDedupeCharsByPosition(unittest.TestCase):
    def test_dedupe_chars_keeps_last_paint(self):
        chars = [
            {"t": "导", "x": 10, "y": 20, "cid": 1, "cl": 0, "ct": 0},
            {"t": "语", "x": 30, "y": 20, "cid": 1, "cl": 0, "ct": 0},
            # second paint overwrites same positions with other page text
            {"t": "刚", "x": 10, "y": 20, "cid": 1, "cl": 0, "ct": 0},
            {"t": "落", "x": 30, "y": 20, "cid": 1, "cl": 0, "ct": 0},
        ]
        out = export_precise.dedupe_chars_by_position(chars)
        self.assertEqual([c["t"] for c in out], ["刚", "落"])

    def test_dedupe_chars_keeps_dual_page_without_cid(self):
        # 左右页局部坐标相同，仅 cl 不同：不得互删
        chars = [
            {"t": "至", "x": 10, "y": 100, "cl": 190},
            {"t": "异", "x": 28, "y": 100, "cl": 190},
            {"t": "萧", "x": 10, "y": 100, "cl": 649},
            {"t": "史", "x": 28, "y": 100, "cl": 649},
        ]
        out = export_precise.dedupe_chars_by_position(chars)
        self.assertEqual([c["t"] for c in out], ["至", "异", "萧", "史"])


class TestEndOfBookStaleLimits(unittest.TestCase):
    def test_last_chapter_stale_limit_stricter_than_normal(self):
        self.assertLess(
            export_precise.LAST_CHAPTER_STALE_LIMIT,
            export_precise.STALE_PAGE_LIMIT,
        )
        self.assertGreaterEqual(export_precise.LAST_CHAPTER_EMPTY_STREAK, 1)

    def test_is_last_catalog_chapter(self):
        cat = ["序", "附录", "《唐诗选》 初版前言", "唐五代诗概述"]
        self.assertFalse(export_precise.is_last_catalog_chapter("附录", cat))
        self.assertFalse(
            export_precise.is_last_catalog_chapter("《唐诗选》 初版前言", cat)
        )
        self.assertTrue(
            export_precise.is_last_catalog_chapter("唐五代诗概述", cat)
        )

    def test_is_end_matter_title(self):
        self.assertTrue(export_precise.is_end_matter_title("附录"))
        self.assertTrue(
            export_precise.is_end_matter_title(
                "附录 把韵律安排得更艺术些——论传统诗歌的声调和新诗的格律性问题"
            )
        )
        self.assertTrue(export_precise.is_end_matter_title("后记"))
        self.assertTrue(export_precise.is_end_matter_title("编后记"))
        self.assertTrue(export_precise.is_end_matter_title("致谢"))
        self.assertFalse(export_precise.is_end_matter_title("第十讲 散曲的滋味"))
        self.assertFalse(export_precise.is_end_matter_title("《高祖还乡》的喜剧性"))

    def test_non_content_catalog_and_complete_after_back_cover(self):
        """正文末章后仅剩封底：续传应视为全书完成，不再强跳封底。"""
        cat = [
            "论梦窗词气味描写的艺术",
            "封底",
        ]
        last = "论梦窗词气味描写的艺术"
        self.assertTrue(export_precise.is_non_content_catalog_title("封底"))
        self.assertTrue(export_precise.is_non_content_catalog_title("版权页"))
        self.assertTrue(export_precise.is_non_content_catalog_title("封面"))
        self.assertFalse(
            export_precise.is_non_content_catalog_title("论梦窗词气味描写的艺术")
        )
        self.assertFalse(export_precise.is_last_catalog_chapter(last, cat))
        self.assertTrue(export_precise.is_export_complete_after(last, cat))
        self.assertTrue(export_precise.is_export_terminal_chapter(last, cat))
        self.assertIsNone(
            export_precise.resolve_stale_advance_target(last, cat)
        )
        self.assertEqual(
            export_precise.catalog_titles_after(cat, last),
            ["封底"],
        )

    def test_export_complete_after_only_when_remaining_non_content(self):
        cat = ["正文一", "正文二", "封底", "版权页"]
        self.assertFalse(
            export_precise.is_export_complete_after("正文一", cat)
        )
        self.assertTrue(
            export_precise.is_export_complete_after("正文二", cat)
        )
        self.assertTrue(
            export_precise.is_export_terminal_chapter("正文二", cat)
        )
        # 后记后剩封底：后记仍可当文末，正文中段不行
        cat2 = ["正文", "后记", "封底"]
        self.assertTrue(export_precise.is_export_terminal_chapter("后记", cat2))
        self.assertTrue(export_precise.is_export_complete_after("后记", cat2))

    def test_non_content_wenhou_variants_complete_export(self):
        """诗词选集末尾「文后1/文后2」视为无正文；正文末章后应直接完成。"""
        cat = [
            "黄昏登高徒惆怅",
            "相见时难别亦难",
            "文后1",
            "文后2",
        ]
        last = "相见时难别亦难"
        self.assertTrue(export_precise.is_non_content_catalog_title("文后"))
        self.assertTrue(export_precise.is_non_content_catalog_title("文后1"))
        self.assertTrue(export_precise.is_non_content_catalog_title("文后2"))
        self.assertTrue(export_precise.is_non_content_catalog_title("文前"))
        self.assertTrue(export_precise.is_non_content_catalog_title("文前1"))
        self.assertFalse(export_precise.is_non_content_catalog_title(last))
        self.assertTrue(export_precise.is_export_complete_after(last, cat))
        self.assertTrue(export_precise.is_export_terminal_chapter(last, cat))
        self.assertIsNone(
            export_precise.resolve_stale_advance_target(last, cat)
        )
        self.assertTrue(export_precise.is_non_content_catalog_title(
            export_precise.next_catalog_title(cat, last)
        ))

    def test_parse_reader_progress_percent(self):
        self.assertEqual(
            export_precise.parse_reader_progress_percent("王国维当前读到 99%"),
            99,
        )
        self.assertEqual(
            export_precise.parse_reader_progress_percent("已读到100%"),
            100,
        )
        self.assertEqual(
            export_precise.parse_reader_progress_percent("进度 87%"),
            87,
        )
        self.assertIsNone(
            export_precise.parse_reader_progress_percent("无进度")
        )
        self.assertIsNone(
            export_precise.parse_reader_progress_percent("读到 101%")
        )
        self.assertEqual(export_precise.NEAR_END_PROGRESS_PERCENT, 99)
        self.assertGreaterEqual(export_precise.NEAR_END_FAIL_LIMIT, 2)

    def test_export_terminal_final_appendix_before_houji(self):
        """最后的附录后仅剩后记：附录卡住应按书末收尾，不再反复重开。"""
        cat = [
            "第十讲 散曲的滋味",
            "《高祖还乡》的喜剧性",
            "附录 把韵律安排得更艺术些——论传统诗歌的声调和新诗的格律性问题",
            "后记",
        ]
        appendix = cat[2]
        self.assertFalse(export_precise.is_last_catalog_chapter(appendix, cat))
        self.assertTrue(export_precise.is_export_terminal_chapter(appendix, cat))
        self.assertTrue(export_precise.is_export_terminal_chapter("后记", cat))
        self.assertFalse(
            export_precise.is_export_terminal_chapter("《高祖还乡》的喜剧性", cat)
        )

    def test_export_terminal_not_when_body_follows_appendix(self):
        """附录后还有正文时，不能当书末提前结束。"""
        cat = ["正文一", "附录 资料", "第十一讲 续篇", "后记"]
        self.assertFalse(
            export_precise.is_export_terminal_chapter("附录 资料", cat)
        )
        self.assertTrue(export_precise.is_export_terminal_chapter("后记", cat))

    def test_resolve_stale_advance_target_mid_book_short_poem(self):
        """中间短诗停滞：应前进到下一章，而不是反复重开同一章。"""
        cat = [
            "单父东楼秋夜送族弟沈之秦",
            "送陆判官往琵琶峡",
            "第二章 谪仙风华",
            "后记",
            "附录",
        ]
        self.assertEqual(
            export_precise.resolve_stale_advance_target("送陆判官往琵琶峡", cat),
            "第二章 谪仙风华",
        )
        self.assertIsNone(
            export_precise.resolve_stale_advance_target("附录", cat)
        )
        self.assertIsNone(
            export_precise.resolve_stale_advance_target("后记", cat)
        )

class TestFarAheadOverrunAndStaleAdvance(unittest.TestCase):
    """顶栏跨到文末、逻辑仍停在前言时：扩大越章窗口，并允许停滞前进。"""

    def _long_catalog(self):
        # 前言后隔很多章才到附录歌曲（复现 唐诗新译初探 结构）
        mid = [f"第{i}首" for i in range(1, 50)]
        return ["致谢", "前言", *mid, "附录歌曲"]

    def test_overrun_default_window_misses_far_appendix(self):
        cat = self._long_catalog()
        blocks = [
            {"type": "text", "text": "前言正文很长"},
            {"type": "text", "text": "附录歌曲"},
            {"type": "text", "text": "歌词一"},
        ]
        self.assertIsNone(
            export_precise.content_overrun_split(blocks, cat, "前言")
        )

    def test_overrun_far_ahead_needs_large_max_ahead(self):
        cat = self._long_catalog()
        blocks = [
            {"type": "text", "text": "前言正文很长"},
            {"type": "text", "text": "附录歌曲"},
            {"type": "text", "text": "歌词一"},
        ]
        hit = export_precise.content_overrun_split(
            blocks, cat, "前言", max_ahead=80
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "附录歌曲")
        self.assertEqual([b["text"] for b in hit[1]], ["前言正文很长"])

    def test_stale_advance_from_preface_not_terminal(self):
        cat = self._long_catalog()
        self.assertEqual(
            export_precise.resolve_stale_advance_target("前言", cat),
            "第1首",
        )
        self.assertFalse(
            export_precise.is_export_terminal_chapter("前言", cat)
        )
        self.assertTrue(
            export_precise.is_export_terminal_chapter("附录歌曲", cat)
        )
        self.assertTrue(export_precise.is_end_matter_title("附录歌曲"))

