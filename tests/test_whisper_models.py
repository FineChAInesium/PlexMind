import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plexmind"))
from app import whisper_models


class WhisperModelDiscoveryTests(unittest.TestCase):
    def test_only_present_known_models_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"WHISPER_MODEL": "turbo"}, clear=True):
            Path(tmp, "large-v3-turbo.pt").write_bytes(b"0" * (1024 * 1024 + 1))
            Path(tmp, "unrelated.pt").touch()
            result = whisper_models.discover(tmp)
        self.assertEqual(result["models"], ["turbo"])
        self.assertEqual(result["configured_model"], "turbo")

    def test_configured_model_is_null_when_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"WHISPER_MODEL": "medium"}, clear=True):
            Path(tmp, "small.pt").write_bytes(b"0" * (1024 * 1024 + 1))
            result = whisper_models.discover(tmp)
        self.assertEqual(result["models"], ["small"])
        self.assertIsNone(result["configured_model"])


if __name__ == "__main__":
    unittest.main()
