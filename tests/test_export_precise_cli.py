import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import export_precise
import book_json


class TestExportPreciseCli(unittest.TestCase):
    def test_parse_args_single_and_flags(self):
        args = export_precise.parse_args([
            "abc123",
            "--force",
            "--download-images",
            "--out-dir",
            "tmp/books",
            "--headless",
            "--reader-width",
            "1280",
            "--reader-height",
            "900",
            "--force-single-page",
        ])
        self.assertEqual(args.book, "abc123")
        self.assertTrue(args.force)
        self.assertTrue(args.download_images)
        self.assertEqual(args.out_dir, "tmp/books")
        self.assertTrue(args.headless)
        self.assertEqual(args.reader_width, 1280)
        self.assertEqual(args.reader_height, 900)
        self.assertTrue(args.force_single_page)

    def test_parse_args_reader_layout_defaults(self):
        args = export_precise.parse_args([])
        self.assertEqual(args.reader_width, export_precise.READER_VIEWPORT_WIDTH)
        self.assertEqual(args.reader_height, export_precise.READER_VIEWPORT_HEIGHT)
        self.assertIsNone(args.force_single_page)

    def test_parse_args_can_disable_force_single_page(self):
        args = export_precise.parse_args(["abc123", "--no-force-single-page"])
        self.assertFalse(args.force_single_page)

    def test_parse_args_headless_default_false(self):
        args = export_precise.parse_args([])
        self.assertFalse(args.headless)

    def test_parse_args_batch_default(self):
        args = export_precise.parse_args([])
        self.assertIsNone(args.book)
        self.assertEqual(args.list_path, str(export_precise.DEFAULT_NEW_BOOKS))
        self.assertEqual(args.out_dir, str(export_precise.DEFAULT_BOOKS_DIR))
        import env_config
        self.assertEqual(export_precise.DEFAULT_BOOKS_DIR, env_config.BOOKS_DIR)

    def test_resolve_book_id_from_url(self):
        self.assertEqual(
            export_precise.resolve_book_id("https://weread.qq.com/web/reader/abc123/"),
            "abc123",
        )
        self.assertEqual(export_precise.resolve_book_id("xyz"), "xyz")


    def test_format_elapsed(self):
        self.assertEqual(export_precise.format_elapsed(0), "0 秒")
        self.assertEqual(export_precise.format_elapsed(45), "45 秒")
        self.assertEqual(export_precise.format_elapsed(45.2), "45.2 秒")
        self.assertEqual(export_precise.format_elapsed(59.9), "59.9 秒")
        self.assertEqual(export_precise.format_elapsed(60), "1 分钟")
        self.assertEqual(export_precise.format_elapsed(90), "1.5 分钟")
        self.assertEqual(export_precise.format_elapsed(180), "3 分钟")
        self.assertEqual(export_precise.format_elapsed(125), "2.1 分钟")

    def test_export_one_skips_existing_without_force(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                out = Path(td)
                (out / "bid_旧.json").write_text("{}", encoding="utf-8")
                with mock.patch.object(export_precise, "run_session", new=mock.AsyncMock()) as rs:
                    status, msg = await export_precise.export_one_book(
                        "bid", out_dir=out, force=False)
                    self.assertEqual(status, "skipped")
                    rs.assert_not_called()
        asyncio.run(run())

    def test_finalize_book_json_writes_compat_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            book_dir = root / "output" / "bid"
            md = book_dir / "chapters"
            raw = book_dir / "raw"
            md.mkdir(parents=True)
            raw.mkdir(parents=True)
            (md / "0001.md").write_text("# 章一\n\n正文甲。\n", encoding="utf-8")
            (raw / "0001.json").write_text(
                json.dumps({"title": "章一", "images": [], "text_len": 3}, ensure_ascii=False),
                encoding="utf-8",
            )
            out_dir = root / "books"
            path, book = export_precise.finalize_book_json(
                "bid", "测试书", "作者", book_dir, out_dir)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "bid_测试书.json")
            self.assertEqual(book["body"][0]["content"], "正文甲。")
            self.assertEqual(book["author"], "作者")
            self.assertEqual(book["press"], "")

    def test_batch_stops_on_failure(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                lst = root / "new_books.txt"
                lst.write_text("a1,书A,作A\na2,书B,作B\n", encoding="utf-8")
                out = root / "books"
                out.mkdir()

                calls = []

                async def fake_export(book_id, **kwargs):
                    calls.append(book_id)
                    if book_id == "a1":
                        raise RuntimeError("boom")
                    return "ok", "x"

                with mock.patch.object(
                    export_precise, "export_one_book", side_effect=fake_export
                ):
                    with self.assertRaises(RuntimeError):
                        await export_precise.export_batch(
                            list_path=lst, out_dir=out, force=True, book_interval=0)
                self.assertEqual(calls, ["a1"])
        asyncio.run(run())

    def test_batch_skips_existing_and_intervals(self):
        async def run():
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                lst = root / "new_books.txt"
                lst.write_text("a1,书A,作A\na2,书B,作B\n", encoding="utf-8")
                out = root / "books"
                out.mkdir()
                (out / "a1_书A.json").write_text("{}", encoding="utf-8")
                sleeps = []

                async def fake_export(book_id, **kwargs):
                    return "ok", book_id

                async def fake_sleep(sec):
                    sleeps.append(sec)

                with mock.patch.object(export_precise, "export_one_book", side_effect=fake_export):
                    with mock.patch.object(export_precise.asyncio, "sleep", side_effect=fake_sleep):
                        await export_precise.export_batch(
                            list_path=lst, out_dir=out, force=False, book_interval=60)
                # only a2 pending; single book => no interval sleep after last
                self.assertEqual(sleeps, [])
        asyncio.run(run())

    def test_reader_viewport_helper(self):
        vp = export_precise.reader_viewport()
        self.assertIn("width", vp)
        self.assertIn("height", vp)
        self.assertGreaterEqual(vp["width"], 1000)
        x, y = export_precise.viewport_focus_point(vp)
        self.assertGreater(x, 0)
        self.assertGreater(y, 0)

    def test_reader_viewport_overrides(self):
        vp = export_precise.reader_viewport(1280, 720)
        self.assertEqual(vp, {"width": 1280, "height": 720})

    def test_chapter_saved_requires_text_or_images(self):
        self.assertFalse(export_precise.chapter_saved(0, []))
        self.assertTrue(export_precise.chapter_saved(1, []))
        self.assertTrue(export_precise.chapter_saved(0, [{"file": "cover.jpg"}]))

    def test_page_viewport_uses_browser_inner_size(self):
        async def run():
            page = mock.AsyncMock()
            page.evaluate.return_value = {"width": 1004, "height": 477}

            vp = await export_precise.page_viewport(
                page, {"width": 1200, "height": 900})

            self.assertEqual(vp, {"width": 1004, "height": 477})

        asyncio.run(run())

    def test_ensure_configured_viewport_keeps_match(self):
        async def run():
            page = mock.AsyncMock()
            page.evaluate.return_value = {"width": 1200, "height": 900}
            desired = {"width": 1200, "height": 900}

            vp = await export_precise.ensure_configured_viewport(page, desired)

            self.assertEqual(vp, desired)
            page.set_viewport_size.assert_not_called()

        asyncio.run(run())

    def test_ensure_configured_viewport_forces_mismatch(self):
        async def run():
            page = mock.AsyncMock()
            # first read: mismatch; after set_viewport_size: match
            page.evaluate.side_effect = [
                {"width": 1024, "height": 497},
                {"width": 1200, "height": 900},
            ]
            desired = {"width": 1200, "height": 900}

            with mock.patch.object(export_precise.asyncio, "sleep", new=mock.AsyncMock()):
                vp = await export_precise.ensure_configured_viewport(page, desired)

            self.assertEqual(vp, desired)
            page.set_viewport_size.assert_awaited_once_with(desired)

        asyncio.run(run())

    def test_ensure_configured_viewport_falls_back_on_set_error(self):
        async def run():
            page = mock.AsyncMock()
            page.evaluate.return_value = {"width": 1024, "height": 497}
            page.set_viewport_size.side_effect = RuntimeError("boom")
            desired = {"width": 1200, "height": 900}

            vp = await export_precise.ensure_configured_viewport(page, desired)

            self.assertEqual(vp, {"width": 1024, "height": 497})

        asyncio.run(run())

    def test_turn_reader_page_focuses_without_clicking_content(self):
        async def run():
            page = mock.AsyncMock()
            # dismiss_reader_search / blur / focus 都会 evaluate
            page.evaluate = mock.AsyncMock(return_value=False)

            await export_precise.turn_reader_page(page)

            self.assertGreaterEqual(page.evaluate.await_count, 1)
            page.keyboard.press.assert_awaited_with("ArrowRight")
            page.mouse.click.assert_not_called()

        asyncio.run(run())

    def test_turn_reader_page_never_clicks_for_arrow2(self):
        async def run():
            page = mock.AsyncMock()
            page.evaluate = mock.AsyncMock(return_value=False)

            await export_precise.turn_reader_page(page, method="arrow2")

            self.assertEqual(page.keyboard.press.await_count, 2)
            page.keyboard.press.assert_any_await("ArrowRight")
            page.mouse.click.assert_not_called()

        asyncio.run(run())

    def test_close_reader_catalog_does_nothing_when_already_hidden(self):
        async def run():
            page = mock.Mock()
            page.keyboard.press = mock.AsyncMock()
            page.click = mock.AsyncMock()
            page.evaluate = mock.AsyncMock(return_value=False)

            closed = await export_precise.close_reader_catalog(page)

            self.assertFalse(closed)
            page.keyboard.press.assert_not_called()

        asyncio.run(run())

    def test_close_reader_catalog_uses_escape_when_still_visible(self):
        async def run():
            page = mock.Mock()
            page.keyboard.press = mock.AsyncMock()
            page.click = mock.AsyncMock()
            # 前几次 is_open=True 才会走进 Esc 流程；之后关闭
            opens = [True, True, True, False]
            page.evaluate = mock.AsyncMock(
                side_effect=lambda *a, **k: (
                    opens.pop(0) if opens else False
                )
            )

            closed = await export_precise.close_reader_catalog(page)

            self.assertTrue(closed)
            page.keyboard.press.assert_any_await("Escape")

        asyncio.run(run())

    def test_reader_layout_keeps_desktop_width_unless_forced(self):
        async def run():
            page = mock.AsyncMock()
            page.evaluate.return_value = 2
            vp = {"width": 1200, "height": 900}

            actual = await export_precise.ensure_reader_layout(
                page, vp, force_single_page=False)

            self.assertEqual(actual, vp)
            page.set_viewport_size.assert_not_called()
            page.reload.assert_not_called()

        asyncio.run(run())

    def test_get_last_chapter_title_prefers_raw_and_strips_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            md = root / "chapters"
            raw = root / "raw"
            md.mkdir(); raw.mkdir()
            (md / "0001.md").write_text("# \n\n正文\n", encoding="utf-8")
            (raw / "0001.json").write_text(
                json.dumps({"title": "", "images": [], "text_len": 2}, ensure_ascii=False),
                encoding="utf-8",
            )
            title, idx = export_precise.get_last_chapter_title(str(md))
            self.assertEqual(idx, 1)
            # 空 title 时 normalize 后为 None
            self.assertIsNone(title)

            (raw / "0001.json").write_text(
                json.dumps({"title": "李白", "images": [], "text_len": 2}, ensure_ascii=False),
                encoding="utf-8",
            )
            (md / "0001.md").write_text("# 李白\n\n正文\n", encoding="utf-8")
            title, idx = export_precise.get_last_chapter_title(str(md))
            self.assertEqual((title, idx), ("李白", 1))


if __name__ == "__main__":
    unittest.main()
