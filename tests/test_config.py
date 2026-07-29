from pathlib import Path

import pytest

from telegram_downloader.config import (
    AppPaths,
    DownloadConfig,
    load_config,
    normalize_media_types,
    save_config,
)


def make_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")


def test_media_types_normalize_aliases_and_all():
    assert normalize_media_types("photos, videos") == ("photo", "video")
    assert normalize_media_types("photo,all,video") == ("all",)


def test_media_types_reject_unknown_value():
    with pytest.raises(ValueError, match="Unknown media type"):
        normalize_media_types("photo,archive")


def test_filter_expression_uses_tdl_media_names():
    config = DownloadConfig(channel="@example", media_types="photo,video")

    expression = config.filter_expression()

    assert 'Media.Name endsWith ".jpg"' in expression
    assert 'Media.Name endsWith ".mp4"' in expression
    assert " || " in expression


def test_config_round_trip(tmp_path):
    paths = make_paths(tmp_path)
    original = DownloadConfig(
        channel="-100123",
        telegram_user_id="123456789",
        media_types="document",
        daily_enabled=True,
        daily_time="23:45",
        download_dir=paths.download_dir,
        threads=8,
        concurrency=3,
        delay_seconds=1.5,
        takeout=True,
    )

    save_config(paths, original)
    loaded = load_config(paths)

    assert loaded.to_dict() == original.to_dict()


def test_config_requires_valid_daily_time():
    with pytest.raises(ValueError, match="HH:MM"):
        DownloadConfig(channel="@example", daily_time="25:00")
