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
        ])
        self.assertEqual(args.book, "abc123")
        self.assertTrue(args.force)
        self.assertTrue(args.download_images)
        self.assertEqual(args.out_dir, "tmp/books")

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


if __name__ == "__main__":
    unittest.main()
