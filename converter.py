"""Convert local audio to an OGG/Opus voice message with FFmpeg."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_INPUT_BYTES = 20_000_000
MAX_OUTPUT_BYTES = 50_000_000
MAX_DURATION_SECONDS = 60 * 60
CONVERSION_TIMEOUT_SECONDS = 180
AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac", ".aiff", ".wma"
}


class ConversionError(Exception):
    """A conversion problem that can be explained directly to the user."""


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str


@dataclass(frozen=True)
class VoiceResult:
    path: Path
    duration: int


def is_audio_document(filename: str | None, mime_type: str | None) -> bool:
    extension = Path(filename or "").suffix.lower()
    return extension in AUDIO_EXTENSIONS or (mime_type or "").lower().startswith("audio/")


def resolve_tools(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> Tools:
    resolved = []
    for name, executable in (("FFmpeg", ffmpeg), ("FFprobe", ffprobe)):
        location = shutil.which(executable)
        if not location:
            raise ConversionError(
                f"{name} не найден. Установите FFmpeg или укажите путь в .env."
            )
        resolved.append(location)
    return Tools(*resolved)


def _run(arguments: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise ConversionError(
            "Обработка заняла слишком много времени. Попробуйте более короткую запись."
        ) from error
    except OSError as error:
        raise ConversionError("Не удалось запустить FFmpeg. Проверьте установку.") from error


def check_tools(tools: Tools) -> None:
    encoders = _run([tools.ffmpeg, "-hide_banner", "-encoders"], timeout=15)
    if encoders.returncode != 0 or "libopus" not in encoders.stdout:
        raise ConversionError("Нужна сборка FFmpeg с кодеком libopus.")
    probe = _run([tools.ffprobe, "-version"], timeout=15)
    if probe.returncode != 0:
        raise ConversionError("FFprobe установлен, но не запускается.")


def _duration(source: Path, tools: Tools) -> float:
    probe = _run(
        [
            tools.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
            "-show_entries", "format=duration:stream=codec_type,duration",
            "-of", "json", str(source.resolve()),
        ],
        timeout=30,
    )
    try:
        data = json.loads(probe.stdout)
        audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if probe.returncode != 0 or not audio_streams:
            raise ValueError("No readable audio stream")
        raw_duration = data.get("format", {}).get("duration")
        if raw_duration in (None, "N/A"):
            raw_duration = audio_streams[0].get("duration")
        duration = float(raw_duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Invalid duration")
        return duration
    except (ValueError, TypeError, KeyError) as error:
        raise ConversionError(
            "Не удалось прочитать аудио. Возможно, файл повреждён или это не аудиозапись."
        ) from error


def convert_to_voice(source: Path, destination: Path, tools: Tools) -> VoiceResult:
    """Blocking operation: callers in asyncio should use asyncio.to_thread."""
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise ConversionError("Файл слишком большой. Отправьте аудио до 20 МБ.")
    duration = _duration(source, tools)
    if duration > MAX_DURATION_SECONDS:
        raise ConversionError("Запись слишком длинная. Максимум — 60 минут.")

    result = _run(
        [
            tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file,pipe", "-i", str(source.resolve()),
            "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            "-c:a", "libopus", "-b:a", "64k", "-vbr", "on",
            "-application", "audio", "-ac", "1", "-ar", "48000",
            "-f", "ogg", str(destination.resolve()),
        ],
        timeout=CONVERSION_TIMEOUT_SECONDS,
    )
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        raise ConversionError("Не удалось преобразовать запись. Попробуйте другой аудиофайл.")
    if destination.stat().st_size > MAX_OUTPUT_BYTES:
        raise ConversionError("Голосовое получилось слишком большим. Отправьте запись короче.")
    return VoiceResult(destination, max(1, math.ceil(_duration(destination, tools))))
