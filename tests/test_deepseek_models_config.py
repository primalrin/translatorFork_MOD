import json
import unittest
from pathlib import Path


class DeepSeekModelsConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config_path = Path(__file__).resolve().parents[1] / "config" / "api_providers.json"
        cls.providers = json.loads(config_path.read_text(encoding="utf-8"))

    def test_official_deepseek_models_use_unlimited_parallelism_and_100_rpm(self):
        models = self.providers["deepseek"]["models"]

        self.assertEqual(len(models), 6)
        for display_name, model_config in models.items():
            with self.subTest(display_name=display_name):
                self.assertEqual(model_config["rpm"], 100)
                self.assertEqual(model_config["max_concurrent_requests"], 0)


if __name__ == "__main__":
    unittest.main()
