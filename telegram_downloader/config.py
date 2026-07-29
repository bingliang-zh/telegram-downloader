from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


MEDIA_TYPES = ("all", "photo", "video", "audio", "voice", "document", "sticker")

# tdl exposes the media filename to its expression filter. Telegram photos are
# represented by tdl as <photo-id>.jpg; documents use their original filename
# or an extension inferred from the MIME type.
MEDIA_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "photo": (".jpg", ".jpeg"),
    "video": (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".flv"),
    "audio": (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma"),
    "voice": (".ogg", ".oga", ".opus"),
    "document": (
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
        ".csv", ".json", ".xml", ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
        ".xz", ".apk", ".ipa", ".epub", ".mobi", ".iso", ".bin", ".unknown",
    ),
    "sticker": (".webp", ".tgs"),
}

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _default_data_dir() -> Path:
    return Path(os.getenv("APP_DATA_DIR", "./data")).expanduser()


@dataclass(frozen=True)
class AppPaths:
    """All host-mounted application paths."""

    data_dir: Path
    download_dir: Path
    tdl_storage_dir: Path

    @classmethod
    def from_env(cls) -> "AppPaths":
        data_dir = _default_data_dir()
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "./downloads")).expanduser()
        tdl_storage_dir = Path(
            os.getenv("TDL_STORAGE_PATH", str(data_dir / "tdl"))
        ).expanduser()
        return cls(
            data_dir=data_dir,
            download_dir=download_dir,
            tdl_storage_dir=tdl_storage_dir,
        )

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def staging_dir(self) -> Path:
        return self.download_dir / ".telegram-downloader" / "runs"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.jobs_dir, self.download_dir, self.tdl_storage_dir):
            path.mkdir(parents=True, exist_ok=True)


def normalize_media_types(values: list[str] | tuple[str, ...] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        raw_values = values.split(",")
    else:
        raw_values = list(values)

    normalized: list[str] = []
    for value in raw_values:
        item = value.strip().lower()
        if not item:
            continue
        aliases = {"photos": "photo", "videos": "video", "audios": "audio", "voices": "voice", "docs": "document", "documents": "document", "stickers": "sticker"}
        item = aliases.get(item, item)
        if item not in MEDIA_TYPES:
            raise ValueError(
                f"Unknown media type {value!r}; choose from: {', '.join(MEDIA_TYPES)}"
            )
        if item not in normalized:
            normalized.append(item)

    if not normalized:
        raise ValueError("At least one media type is required")
    if "all" in normalized:
        return ("all",)
    return tuple(normalized)


def validate_daily_time(value: str) -> str:
    value = value.strip()
    if not _TIME_RE.fullmatch(value):
        raise ValueError("daily_time must use 24-hour HH:MM format, for example 03:30")
    # Validate through datetime as a second line of defense and to keep the
    # scheduler's assumptions explicit.
    datetime.strptime(value, "%H:%M")
    return value


@dataclass
class DownloadConfig:
    channel: str
    media_types: tuple[str, ...] = ("all",)
    daily_enabled: bool = False
    daily_time: str = "03:00"
    download_dir: Path = Path("./downloads")
    threads: int = 4
    concurrency: int = 2
    delay_seconds: float = 0.0
    takeout: bool = False
    telegram_user_id: str = ""

    def __post_init__(self) -> None:
        self.channel = self.channel.strip()
        self.telegram_user_id = self.telegram_user_id.strip()
        if not self.channel:
            raise ValueError("channel must not be empty")
        self.media_types = normalize_media_types(self.media_types)
        self.daily_time = validate_daily_time(self.daily_time)
        if self.threads < 1:
            raise ValueError("threads must be greater than 0")
        if self.concurrency < 1:
            raise ValueError("concurrency must be greater than 0")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds must be greater than or equal to 0")
        self.download_dir = Path(self.download_dir).expanduser()

    def filter_expression(self) -> str:
        if "all" in self.media_types:
            return "true"
        extensions = sorted({
            extension
            for media_type in self.media_types
            for extension in MEDIA_EXTENSIONS[media_type]
        })
        return " || ".join(
            f'Media.Name endsWith "{extension}"' for extension in extensions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "telegram_user_id": self.telegram_user_id,
            "media_types": list(self.media_types),
            "daily_enabled": self.daily_enabled,
            "daily_time": self.daily_time,
            "download_dir": str(self.download_dir),
            "threads": self.threads,
            "concurrency": self.concurrency,
            "delay_seconds": self.delay_seconds,
            "takeout": self.takeout,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DownloadConfig":
        return cls(
            channel=str(data.get("channel", "")),
            telegram_user_id=str(data.get("telegram_user_id", "")).strip(),
            media_types=tuple(data.get("media_types", ("all",))),
            daily_enabled=bool(data.get("daily_enabled", False)),
            daily_time=str(data.get("daily_time", "03:00")),
            download_dir=Path(data.get("download_dir", "./downloads")),
            threads=int(data.get("threads", 4)),
            concurrency=int(data.get("concurrency", 2)),
            delay_seconds=float(data.get("delay_seconds", 0.0)),
            takeout=bool(data.get("takeout", False)),
        )


def save_config(paths: AppPaths, config: DownloadConfig) -> None:
    paths.ensure_directories()
    temporary = paths.config_file.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, paths.config_file)


def load_config(paths: AppPaths) -> DownloadConfig:
    try:
        data = json.loads(paths.config_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Configuration not found at {paths.config_file}. Run 'telegram-downloader init' first."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid configuration file: {paths.config_file}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Configuration must be a JSON object: {paths.config_file}")
    return DownloadConfig.from_dict(data)
