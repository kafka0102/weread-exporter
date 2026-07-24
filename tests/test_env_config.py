import os
import tempfile
import unittest
from pathlib import Path

import env_config


class TestEnvConfig(unittest.TestCase):
    def test_load_dotenv_parses_and_strips_quotes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_text(
                '# comment\n'
                'FOO_BAR=1.5\n'
                'QUOTED="hello"\n'
                "SINGLE='x'\n"
                'EMPTY=\n'
                '\n',
                encoding="utf-8",
            )
            # clear first
            for k in ("FOO_BAR", "QUOTED", "SINGLE", "EMPTY"):
                os.environ.pop(k, None)
            loaded = env_config.load_dotenv(p, override=True)
            self.assertEqual(loaded, p)
            self.assertEqual(os.environ.get("FOO_BAR"), "1.5")
            self.assertEqual(os.environ.get("QUOTED"), "hello")
            self.assertEqual(os.environ.get("SINGLE"), "x")
            self.assertEqual(os.environ.get("EMPTY"), "")

    def test_load_dotenv_does_not_override_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / ".env"
            p.write_text("KEEP_ME=from_file\n", encoding="utf-8")
            os.environ["KEEP_ME"] = "from_env"
            env_config.load_dotenv(p, override=False)
            self.assertEqual(os.environ["KEEP_ME"], "from_env")
            env_config.load_dotenv(p, override=True)
            self.assertEqual(os.environ["KEEP_ME"], "from_file")
            os.environ.pop("KEEP_ME", None)

    def test_env_float_int_bool(self):
        os.environ["T_FLOAT"] = "2.5"
        os.environ["T_INT"] = "7"
        os.environ["T_BOOL"] = "yes"
        os.environ["T_BAD"] = "nope"
        self.assertEqual(env_config.env_float("T_FLOAT", 0), 2.5)
        self.assertEqual(env_config.env_int("T_INT", 0), 7)
        self.assertTrue(env_config.env_bool("T_BOOL", False))
        self.assertEqual(env_config.env_float("T_BAD", 9.0), 9.0)
        self.assertEqual(env_config.env_float("T_MISSING", 3.0), 3.0)
        self.assertFalse(env_config.env_bool("T_MISSING_BOOL", False))
        for k in ("T_FLOAT", "T_INT", "T_BOOL", "T_BAD"):
            os.environ.pop(k, None)

    def test_sleep_defaults_are_positive(self):
        self.assertGreater(env_config.SLEEP_BOOK_DETAIL_INTERVAL, 0)
        self.assertGreater(env_config.SLEEP_SHELF_SCROLL, 0)
        # 代码默认值（不依赖当前 .env 是否被改过）
        self.assertEqual(env_config.env_float("SLEEP_BOOK_DETAIL_INTERVAL_UNSET_X", 5.0), 5.0)
        self.assertEqual(env_config.SLEEP_BOOK_INTERVAL, 60.0)
        self.assertEqual(env_config.SLEEP_CHAPTER_PER_1K_CHARS, 1.0)
        self.assertEqual(env_config.SLEEP_CHAPTER_MIN, 1.0)
        self.assertEqual(env_config.SLEEP_CHAPTER_MAX, 3.0)


    def test_env_path_and_books_dir(self):
        os.environ.pop("BOOKS_DIR_TEST", None)
        p = env_config.env_path("BOOKS_DIR_TEST", "~/data/weixin/books")
        self.assertEqual(p, Path.home() / "data" / "weixin" / "books")
        os.environ["BOOKS_DIR_TEST"] = "~/tmp/custom-books"
        p2 = env_config.env_path("BOOKS_DIR_TEST", "~/data/weixin/books")
        self.assertEqual(p2, Path.home() / "tmp" / "custom-books")
        os.environ.pop("BOOKS_DIR_TEST", None)
        # 模块级默认（允许 .env 覆盖，仅校验类型与展开）
        self.assertIsInstance(env_config.BOOKS_DIR, Path)
        self.assertFalse(str(env_config.BOOKS_DIR).startswith("~"))

    def test_reader_viewport_defaults(self):
        # 0 表示自动匹配本机屏幕；显式正整数仍合法
        self.assertGreaterEqual(env_config.READER_VIEWPORT_WIDTH, 0)
        self.assertGreaterEqual(env_config.READER_VIEWPORT_HEIGHT, 0)
        self.assertFalse(env_config.READER_FORCE_SINGLE_PAGE)
        # 代码默认 0=auto；未设置时 env_int 回退由调用方指定
        self.assertEqual(env_config.env_int("READER_VIEWPORT_WIDTH_UNSET_X", 0), 0)
        self.assertEqual(env_config.env_int("READER_VIEWPORT_HEIGHT_UNSET_X", 0), 0)

    def test_detect_host_screen_size_returns_reasonable_pair(self):
        w, h = env_config.detect_host_screen_size()
        self.assertGreaterEqual(w, 800)
        self.assertGreaterEqual(h, 600)

    def test_resolve_reader_viewport_auto_and_explicit(self):
        auto = env_config.resolve_reader_viewport(0, 0)
        self.assertGreaterEqual(auto["width"], 800)
        self.assertGreaterEqual(auto["height"], 600)
        fixed = env_config.resolve_reader_viewport(1280, 720)
        self.assertEqual(fixed, {"width": 1280, "height": 720})
        # 仅宽 auto
        mixed = env_config.resolve_reader_viewport(0, 800)
        self.assertGreaterEqual(mixed["width"], 800)
        self.assertEqual(mixed["height"], 800)


if __name__ == "__main__":
    unittest.main()
