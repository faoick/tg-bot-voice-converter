from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="voice_launcher_test_")
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.env_example = "# Settings\nBOT_TOKEN=\n\n# FFmpeg\nFFMPEG_PATH=ffmpeg\n"
        (self.folder / ".env.example").write_text(self.env_example, encoding="utf-8")
        python = self.folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.parent.mkdir(parents=True)
        python.touch()

    def test_first_launch_prompts_for_token_and_preserves_other_settings(self) -> None:
        with (
            patch.object(launcher, "PROJECT_DIR", self.folder),
            patch.dict(os.environ, {"BOT_TOKEN": ""}),
            patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
            patch.object(launcher.getpass, "getpass", return_value="123456789:test_token") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(launcher.main(), 0)
        prompt.assert_called_once()
        saved = (self.folder / ".env").read_text(encoding="utf-8")
        self.assertIn("BOT_TOKEN=123456789:test_token\n", saved)
        self.assertIn("\n\n# FFmpeg\nFFMPEG_PATH=ffmpeg\n", saved)

    def test_existing_token_does_not_prompt_or_overwrite_configuration(self) -> None:
        existing = self.env_example.replace("BOT_TOKEN=\n", "BOT_TOKEN=123456789:existing_token\n")
        (self.folder / ".env").write_text(existing, encoding="utf-8")
        with (
            patch.object(launcher, "PROJECT_DIR", self.folder),
            patch.dict(os.environ, {"BOT_TOKEN": ""}),
            patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
            patch.object(launcher.getpass, "getpass") as prompt,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(launcher.main(), 0)
        prompt.assert_not_called()
        self.assertEqual((self.folder / ".env").read_text(encoding="utf-8"), existing)


if __name__ == "__main__":
    unittest.main()
