from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import Runtime, build_application, handle_audio
from converter import MAX_INPUT_BYTES, ConversionError, convert_to_voice, resolve_tools


def create_mp3(path: Path) -> None:
    tools = resolve_tools()
    subprocess.run(
        [
            tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
            "-ac", "2", "-c:a", "libmp3lame", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="voice_bot_test_")
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.tools = resolve_tools()

    def test_real_mp3_becomes_playable_ogg_opus(self) -> None:
        source = self.folder / "тестовая музыка.mp3"
        destination = self.folder / "голосовое.ogg"
        create_mp3(source)
        result = convert_to_voice(source, destination, self.tools)
        self.assertEqual(destination.read_bytes()[:4], b"OggS")
        probe = subprocess.run(
            [self.tools.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(destination)],
            capture_output=True, text=True, check=True, timeout=15,
        )
        metadata = json.loads(probe.stdout)
        self.assertEqual(metadata["format"]["format_name"], "ogg")
        self.assertEqual(metadata["streams"][0]["codec_name"], "opus")
        self.assertEqual(metadata["streams"][0]["channels"], 1)
        self.assertAlmostEqual(float(metadata["format"]["duration"]), 1.2, delta=0.1)
        self.assertGreaterEqual(result.duration, 1)

    def test_corrupt_mp3_is_rejected(self) -> None:
        source = self.folder / "broken.mp3"
        source.write_bytes(b"This is not audio")
        destination = self.folder / "voice.ogg"
        with self.assertRaisesRegex(ConversionError, "прочитать аудио"):
            convert_to_voice(source, destination, self.tools)
        self.assertFalse(destination.exists())

    def test_large_file_is_rejected_before_decoding(self) -> None:
        source = self.folder / "large.mp3"
        with source.open("wb") as audio:
            audio.truncate(MAX_INPUT_BYTES + 1)
        with self.assertRaisesRegex(ConversionError, "слишком большой"):
            convert_to_voice(source, self.folder / "voice.ogg", self.tools)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="voice_bot_handler_")
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "source.mp3"
        create_mp3(self.source)
        self.runtime = Runtime(resolve_tools(), frozenset())
        self.status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        self.download_paths: list[Path] = []
        self.sent_voices: list[bytes] = []

        async def download(*, custom_path, **kwargs):
            self.download_paths.append(Path(custom_path))
            shutil.copyfile(self.source, custom_path)

        async def send_voice(**kwargs):
            self.sent_voices.append(kwargs["voice"].read())

        self.remote = SimpleNamespace(
            file_size=self.source.stat().st_size,
            download_to_drive=AsyncMock(side_effect=download),
        )
        self.client = SimpleNamespace(
            get_file=AsyncMock(return_value=self.remote),
            send_chat_action=AsyncMock(),
        )
        self.message = SimpleNamespace(
            audio=SimpleNamespace(file_id="fake_file_id", file_size=self.source.stat().st_size),
            voice=None, document=None, chat_id=123, message_thread_id=None,
            reply_text=AsyncMock(return_value=self.status),
            reply_voice=AsyncMock(side_effect=send_voice),
        )
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_message=self.message)
        self.context = SimpleNamespace(bot=self.client, bot_data={"runtime": self.runtime})

    async def test_audio_is_sent_as_voice_and_temporary_files_are_removed(self) -> None:
        await handle_audio(self.update, self.context)
        self.message.reply_voice.assert_awaited_once()
        self.assertEqual(self.sent_voices[0][:4], b"OggS")
        self.assertEqual(self.message.reply_voice.call_args.kwargs["filename"], "voice.ogg")
        self.assertTrue(self.message.reply_voice.call_args.kwargs["do_quote"])
        self.assertTrue(self.download_paths)
        self.assertFalse(self.download_paths[0].parent.exists())
        self.assertFalse(self.runtime.active_users)
        self.status.delete.assert_awaited_once()

    async def test_mp3_sent_as_document_is_converted(self) -> None:
        self.message.audio = None
        self.message.document = SimpleNamespace(
            file_id="document", file_size=self.source.stat().st_size,
            file_name="song.MP3", mime_type="application/octet-stream",
        )
        await handle_audio(self.update, self.context)
        self.message.reply_voice.assert_awaited_once()
        self.assertEqual(self.sent_voices[0][:4], b"OggS")

    async def test_conversion_failure_releases_slot_and_removes_files(self) -> None:
        self.source.write_bytes(b"broken mp3")
        await handle_audio(self.update, self.context)
        self.message.reply_voice.assert_not_awaited()
        self.assertIn("прочитать аудио", self.status.edit_text.call_args.args[0])
        self.assertFalse(self.runtime.active_users)
        self.assertFalse(self.download_paths[0].parent.exists())

    async def test_large_attachment_is_rejected_without_download(self) -> None:
        self.message.audio.file_size = MAX_INPUT_BYTES + 1
        await handle_audio(self.update, self.context)
        self.client.get_file.assert_not_awaited()
        self.assertIn("20 МБ", self.message.reply_text.call_args.args[0])

    async def test_non_audio_document_is_rejected_without_download(self) -> None:
        self.message.audio = None
        self.message.document = SimpleNamespace(file_name="report.pdf", mime_type="application/pdf")
        await handle_audio(self.update, self.context)
        self.client.get_file.assert_not_awaited()
        self.message.reply_voice.assert_not_awaited()

    async def test_second_file_from_busy_user_is_rejected(self) -> None:
        self.runtime.active_users.add(123)
        await handle_audio(self.update, self.context)
        self.client.get_file.assert_not_awaited()
        self.assertIn("ещё обрабатывается", self.message.reply_text.call_args.args[0])
        self.assertEqual(self.runtime.active_users, {123})

    async def test_bot_at_capacity_rejects_more_jobs(self) -> None:
        self.runtime.active_users.update({456, 789})
        await handle_audio(self.update, self.context)
        self.client.get_file.assert_not_awaited()
        self.assertEqual(self.runtime.active_users, {456, 789})

    async def test_access_limit_rejects_unknown_user(self) -> None:
        self.runtime.allowed_user_ids = frozenset({456})
        await handle_audio(self.update, self.context)
        self.client.get_file.assert_not_awaited()
        self.assertIn("владельцу", self.message.reply_text.call_args.args[0])

    async def test_application_builds_without_network_access(self) -> None:
        application = build_application("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_fake_token", self.runtime)
        self.assertIs(application.bot_data["runtime"], self.runtime)
        self.assertEqual(application.update_processor.max_concurrent_updates, 8)


if __name__ == "__main__":
    unittest.main()
