import pathlib
import unittest

from api.config import KNOWN_ENV_VARS


class EnvTemplateContractTests(unittest.TestCase):
    def test_env_example_covers_known_environment_variables(self):
        env_example = (
            pathlib.Path(__file__).resolve().parents[1] / ".env.example"
        ).read_text(encoding="utf-8")

        for env_var in KNOWN_ENV_VARS:
            self.assertIn(f"{env_var}=", env_example)


if __name__ == "__main__":
    unittest.main()
