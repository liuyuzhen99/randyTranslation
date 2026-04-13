import unittest

from api.config import validate_startup_env


class Phase0ConfigValidationTests(unittest.TestCase):
    def test_validate_startup_env_raises_when_required_missing(self):
        with self.assertRaises(RuntimeError) as ctx:
            validate_startup_env({})
        self.assertIn("DEEPSEEK_API_KEY", str(ctx.exception))
        self.assertIn("DEEPSEEK_BASE_URL", str(ctx.exception))

    def test_validate_startup_env_accepts_required_values(self):
        validate_startup_env(
            {
                "DEEPSEEK_API_KEY": "demo",
                "DEEPSEEK_BASE_URL": "https://example.local",
            }
        )


if __name__ == "__main__":
    unittest.main()
