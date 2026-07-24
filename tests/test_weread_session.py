import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from weread_session import (
    has_cached_login_profile,
    is_login_url,
    prepare_browser_profile,
    resolve_headless,
)
import weread_session


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


class TestLaunchOptions(unittest.TestCase):
    def test_headful_viewport_uses_real_browser_window(self):
        opts = weread_session.build_launch_kwargs(
            headless=False,
            viewport={"width": 1200, "height": 900},
        )

        self.assertTrue(opts["no_viewport"])
        self.assertNotIn("viewport", opts)
        self.assertIn("--window-size=1200,900", opts["args"])

    def test_headless_keeps_fixed_viewport(self):
        opts = weread_session.build_launch_kwargs(
            headless=True,
            viewport={"width": 1200, "height": 900},
        )

        self.assertEqual(opts["viewport"], {"width": 1200, "height": 900})
        self.assertNotIn("no_viewport", opts)




class TestEnsureBrowserWindowSize(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_already_large_enough(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={"width": 1900, "height": 1700})
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock()

        out = await weread_session.ensure_browser_window_size(
            page, {"width": 1920, "height": 1746}
        )
        self.assertEqual(out, {"width": 1900, "height": 1700})
        page.context.new_cdp_session.assert_not_called()

    async def test_resizes_short_window_and_maximizes_if_needed(self):
        page = MagicMock()
        # 1) initial short  2) after set bounds still short  3) final after maximize
        page.evaluate = AsyncMock(
            side_effect=[
                {"width": 1024, "height": 496},
                {"width": 1100, "height": 520},
                {"width": 1800, "height": 1100},
            ]
        )
        client = MagicMock()
        client.send = AsyncMock(
            side_effect=[
                {"windowId": 7},
                None,  # set normal bounds
                None,  # maximize
            ]
        )
        client.detach = AsyncMock()
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock(return_value=client)

        with mock.patch("weread_session.asyncio.sleep", new=AsyncMock()) as sleep:
            out = await weread_session.ensure_browser_window_size(
                page, {"width": 1920, "height": 1746}
            )

        self.assertEqual(out, {"width": 1800, "height": 1100})
        self.assertTrue(sleep.await_count >= 1)
        calls = [c.args[0] for c in client.send.await_args_list]
        self.assertEqual(calls[0], "Browser.getWindowForTarget")
        self.assertEqual(calls[1], "Browser.setWindowBounds")
        self.assertEqual(calls[2], "Browser.setWindowBounds")
        bounds1 = client.send.await_args_list[1].args[1]["bounds"]
        self.assertEqual(bounds1["windowState"], "normal")
        self.assertGreaterEqual(bounds1["width"], 1920)
        self.assertGreaterEqual(bounds1["height"], 1746)
        bounds2 = client.send.await_args_list[2].args[1]["bounds"]
        self.assertEqual(bounds2["windowState"], "maximized")
        client.detach.assert_awaited()

    async def test_set_bounds_only_when_target_reached(self):
        page = MagicMock()
        page.evaluate = AsyncMock(
            side_effect=[
                {"width": 1024, "height": 496},
                {"width": 1900, "height": 1700},
                {"width": 1900, "height": 1700},
            ]
        )
        client = MagicMock()
        client.send = AsyncMock(side_effect=[{"windowId": 3}, None])
        client.detach = AsyncMock()
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock(return_value=client)

        with mock.patch("weread_session.asyncio.sleep", new=AsyncMock()):
            out = await weread_session.ensure_browser_window_size(
                page, {"width": 1920, "height": 1746}
            )

        self.assertEqual(out["height"], 1700)
        self.assertEqual(client.send.await_count, 2)  # getWindow + setBounds only
        client.detach.assert_awaited()

if __name__ == "__main__":
    unittest.main()
