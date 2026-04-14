import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

import api.service as api_service


class Phase0StartupGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()

    def tearDown(self):
        self.stack.close()

    def test_app_startup_raises_when_required_environment_is_missing(self):
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "",
                    "DEEPSEEK_BASE_URL": "",
                },
                clear=False,
            )
        )

        with self.assertRaises(RuntimeError):
            with TestClient(api_service.app):
                pass

    def test_app_startup_succeeds_when_required_environment_is_present(self):
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "DEEPSEEK_API_KEY": "test-key",
                    "DEEPSEEK_BASE_URL": "https://example.local",
                },
                clear=False,
            )
        )

        with TestClient(api_service.app):
            pass


if __name__ == "__main__":
    unittest.main()
