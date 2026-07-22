import tempfile
import unittest
from pathlib import Path
from unittest import mock

from weread_session import (
    has_cached_login_profile,
    is_login_url,
    prepare_browser_profile,
    resolve_headless,
)


class TestIsLoginUrl(unittest.TestCase):
    def test_login_url_true(self):
        self.assertTrue(is_login_url("https://weread.qq.com/web/login"))
        self.assertTrue(is_login_url("https://weread.qq.com/#/login"))
        self.assertTrue(is_login_url("https://weread.qq.com/web/shelf?from=login"))
        self.assertTrue(is_login_url("https://weread.qq.com/web/shelf#login"))

    def test_case_insensitive(self):
        self.assertTrue(is_login_url("HTTPS://WEREAD.QQ.COM/WEB/LOGIN"))

    def test_non_login_url_false(self):
        self.assertFalse(is_login_url("https://weread.qq.com/web/shelf"))
        self.assertFalse(is_login_url("https://weread.qq.com/web/reader/d31323b0813abaf26g0137c2"))

    def test_empty_and_none(self):
        self.assertFalse(is_login_url(""))
        self.assertFalse(is_login_url(None))


class TestHasCachedLoginProfile(unittest.TestCase):
    def test_empty_dir_false(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(has_cached_login_profile(td))

    def test_cookies_nonzero_true(self):
        with tempfile.TemporaryDirectory() as td:
            cookies = Path(td) / "Default" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"cookie-data")
            self.assertTrue(has_cached_login_profile(td))

    def test_network_cookies_true(self):
        with tempfile.TemporaryDirectory() as td:
            cookies = Path(td) / "Default" / "Network" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"cookie-data")
            self.assertTrue(has_cached_login_profile(td))

    def test_empty_cookies_false(self):
        with tempfile.TemporaryDirectory() as td:
            cookies = Path(td) / "Default" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"")
            self.assertFalse(has_cached_login_profile(td))


class TestResolveHeadless(unittest.TestCase):
    def test_not_requested(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(resolve_headless(False, user_data_dir=td, announce=False))

    def test_requested_without_cache_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("builtins.print") as pr:
                self.assertFalse(resolve_headless(True, user_data_dir=td, announce=True))
                self.assertTrue(pr.called)

    def test_requested_with_cache_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            cookies = Path(td) / "Default" / "Cookies"
            cookies.parent.mkdir(parents=True)
            cookies.write_bytes(b"x")
            self.assertTrue(resolve_headless(True, user_data_dir=td, announce=False))



class TestPrepareBrowserProfile(unittest.TestCase):
    def test_creates_dir_and_clears_singleton_and_sync(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "SingletonLock").symlink_to("dead-pid")
            (root / "RunningChromeVersion").write_text("x")
            sync = root / "Default" / "Sync Data"
            sync.mkdir(parents=True)
            (sync / "LevelDB").write_text("junk")

            out = prepare_browser_profile(str(root))
            self.assertEqual(out, str(root))
            self.assertTrue(root.is_dir())
            self.assertFalse((root / "SingletonLock").exists())
            self.assertFalse((root / "RunningChromeVersion").exists())
            self.assertFalse(sync.exists())

    def test_missing_optional_paths_ok(self):
        with tempfile.TemporaryDirectory() as td:
            out = prepare_browser_profile(td)
            self.assertEqual(out, td)
            self.assertTrue(Path(td).is_dir())



if __name__ == "__main__":
    unittest.main()
