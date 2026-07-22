import tempfile
import unittest
from pathlib import Path
from unittest import mock

from weread_session import (
    has_cached_login_profile,
    is_login_url,
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


if __name__ == "__main__":
    unittest.main()
