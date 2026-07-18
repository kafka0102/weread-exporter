import unittest

from weread_session import is_login_url


class TestIsLoginUrl(unittest.TestCase):
    def test_login_url_true(self):
        self.assertTrue(is_login_url("https://weread.qq.com/web/login"))
        self.assertTrue(is_login_url("https://weread.qq.com/#/login"))
        self.assertTrue(is_login_url("https://weread.qq.com/web/shelf?from=login"))

    def test_case_insensitive(self):
        self.assertTrue(is_login_url("HTTPS://WEREAD.QQ.COM/WEB/LOGIN"))

    def test_non_login_url_false(self):
        self.assertFalse(is_login_url("https://weread.qq.com/web/shelf"))
        self.assertFalse(is_login_url("https://weread.qq.com/web/reader/d31323b0813abaf26g0137c2"))

    def test_empty_and_none(self):
        self.assertFalse(is_login_url(""))
        self.assertFalse(is_login_url(None))


if __name__ == "__main__":
    unittest.main()
