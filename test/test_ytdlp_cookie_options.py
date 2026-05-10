import os
import unittest
from unittest.mock import patch

from core.hipHopProducer import HipHopAutoProject


class YtDlpCookieOptionsTest(unittest.TestCase):
    def test_uses_cookie_file_when_configured(self):
        with patch.dict(os.environ, {"YTDLP_COOKIES_FILE": "/tmp/youtube-cookies.txt"}, clear=False):
            self.assertEqual(
                HipHopAutoProject()._cookie_options(),
                {"cookiefile": "/tmp/youtube-cookies.txt"},
            )

    def test_uses_browser_cookie_source_when_configured(self):
        with patch.dict(os.environ, {"YTDLP_COOKIES_FROM_BROWSER": "chrome:Default"}, clear=False):
            self.assertEqual(
                HipHopAutoProject()._cookie_options(),
                {"cookiesfrombrowser": ("chrome", "Default")},
            )


if __name__ == "__main__":
    unittest.main()
