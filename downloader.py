from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import ffmpeg
import yt_dlp
from yt_dlp.utils import DownloadError

Mode = Literal["video", "audio"]

MAX_DURATION_SECONDS = 60 * 60
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv"}


class DownloadProblem(Exception):
    """An expected, user-facing download failure."""


class DurationLimitExceeded(DownloadProblem):
    pass


class FileTooLarge(DownloadProblem):
    pass


class PrivateOrUnavailable(DownloadProblem):
    pass


class ProcessingFailed(DownloadProblem):
    pass


def _base_options(output_dir: Path) -> dict[str, Any]:
    return {
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "cachedir": False,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 25,
        "max_filesize": MAX_UPLOAD_BYTES,
    }


def _estimated_size(info: dict[str, Any]) -> int | None:
    selected_formats = info.get("requested_formats")
    if isinstance(selected_formats, list) and selected_formats:
        sizes = [
            item.get("filesize") or item.get("filesize_approx")
            for item in selected_formats
        ]
        if all(isinstance(size, (int, float)) for size in sizes):
            return int(sum(sizes))

    size = info.get("filesize") or info.get("filesize_approx")
    return int(size) if isinstance(size, (int, float)) else None


def _check_duration(duration: Any) -> None:
    if isinstance(duration, (int, float)) and duration > MAX_DURATION_SECONDS:
        raise DurationLimitExceeded


def _find_media(output_dir: Path, mode: Mode) -> Path:
    allowed_extensions = {".mp3"} if mode == "audio" else VIDEO_EXTENSIONS
    candidates = [
        path
        for path in output_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in allowed_extensions
        and not path.name.endswith(".part")
    ]
    if not candidates:
        raise PrivateOrUnavailable
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _probe_duration(path: Path) -> float | None:
    try:
        metadata = ffmpeg.probe(str(path))
        value = metadata.get("format", {}).get("duration")
        return float(value) if value is not None else None
    except (ffmpeg.Error, OSError, ValueError):
        raise ProcessingFailed from None


def _ensure_final_size(path: Path) -> None:
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise FileTooLarge


def _watermark_video(source: Path, destination: Path) -> None:
    try:
        metadata = ffmpeg.probe(str(source))
        has_audio = any(
            stream.get("codec_type") == "audio"
            for stream in metadata.get("streams", [])
        )
        input_stream = ffmpeg.input(str(source))
        video_stream = input_stream.video.filter(
            "drawtext",
            text="Finix Bot",
            font="Sans",
            fontsize="max(15,min(24,h/32))",
            fontcolor="white@0.78",
            x="w-text_w-18",
            y="h-text_h-18",
            box=1,
            boxcolor="black@0.28",
            boxborderw=7,
            shadowcolor="black@0.35",
            shadowx=1,
            shadowy=1,
        )
        output_args: dict[str, Any] = {
            "vcodec": "libx264",
            "preset": "ultrafast",
            "crf": 26,
            "pix_fmt": "yuv420p",
            "movflags": "+faststart",
        }

        if has_audio:
            ffmpeg.output(
                video_stream,
                input_stream.audio,
                str(destination),
                acodec="aac",
                audio_bitrate="128k",
                **output_args,
            ).overwrite_output().run(capture_stdout=True, capture_stderr=True)
        else:
            ffmpeg.output(
                video_stream,
                str(destination),
                **output_args,
            ).overwrite_output().run(capture_stdout=True, capture_stderr=True)
    except (ffmpeg.Error, OSError, ValueError) as exc:
        raise ProcessingFailed from exc


def _friendly_download_error(error: DownloadError) -> DownloadProblem:
    message = str(error).lower()
    if any(
        phrase in message
        for phrase in ("max-filesize", "file is larger", "file size", "too large")
    ):
        return FileTooLarge()
    if any(
        phrase in message
        for phrase in (
            "private",
            "deleted",
            "not available",
            "unavailable",
            "login required",
            "sign in",
            "removed",
        )
    ):
        return PrivateOrUnavailable()
    return PrivateOrUnavailable()


def download_media(url: str, output_dir: str, mode: Mode) -> str:
    """Download and prepare media; designed to be called in a worker thread."""
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)

    metadata_options = _base_options(destination_dir)
    metadata_options["skip_download"] = True
    metadata_options["format"] = (
        "bestaudio/best" if mode == "audio" else "bestvideo*+bestaudio/best"
    )
    if mode == "video":
        metadata_options["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(metadata_options) as downloader:
            info = downloader.extract_info(url, download=False)
    except DownloadError as exc:
        raise _friendly_download_error(exc) from exc

    if not isinstance(info, dict):
        raise PrivateOrUnavailable

    _check_duration(info.get("duration"))
    estimated_size = _estimated_size(info)
    if estimated_size is not None and estimated_size > MAX_UPLOAD_BYTES:
        raise FileTooLarge

    download_options = _base_options(destination_dir)
    download_options["format"] = (
        "bestaudio/best" if mode == "audio" else "bestvideo*+bestaudio/best"
    )
    if mode == "video":
        download_options["merge_output_format"] = "mp4"
    else:
        download_options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    try:
        with yt_dlp.YoutubeDL(download_options) as downloader:
            downloader.download([url])
    except DownloadError as exc:
        raise _friendly_download_error(exc) from exc

    downloaded_path = _find_media(destination_dir, mode)
    _check_duration(_probe_duration(downloaded_path))

    if mode == "audio":
        _ensure_final_size(downloaded_path)
        return str(downloaded_path)

    watermarked_path = destination_dir / "finix-watermarked.mp4"
    _watermark_video(downloaded_path, watermarked_path)
    _ensure_final_size(watermarked_path)
    return str(watermarked_path)
