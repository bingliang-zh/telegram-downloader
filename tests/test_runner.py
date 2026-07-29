import json
from pathlib import Path

from telegram_downloader.config import AppPaths, DownloadConfig, save_config
from telegram_downloader.runner import (
    _copy_without_overwrite,
    _prepare_manifest,
    _work_progress,
    run_once,
    run_with_retries,
)
from telegram_downloader.state import StateStore


def make_paths(tmp_path: Path) -> AppPaths:
    return AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")


def target_file(paths: AppPaths) -> Path:
    return paths.download_dir / "default" / "-100123" / "1_photo.jpg"


class FakeTdl:
    def __init__(self, *, download_return_code=0, content=b"media"):
        self.download_return_code = download_return_code
        self.content = content
        self.exports = 0
        self.downloads = 0

    def export(self, config, output):
        self.exports += 1
        output.write_text(
            json.dumps(
                {
                    "id": -100123,
                    "messages": [
                        {"id": 1, "type": "message", "file": "1_photo.jpg"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return 0

    def download(self, config, manifest, work_dir):
        self.downloads += 1
        if self.download_return_code == 0:
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "1_photo.jpg.tmp").unlink(missing_ok=True)
            (work_dir / "1_photo.jpg").write_bytes(self.content)
        else:
            work_dir.mkdir(parents=True, exist_ok=True)
            (work_dir / "1_photo.jpg.tmp").write_bytes(b"partial")
        return self.download_return_code


def test_prepare_manifest_excludes_completed_message(tmp_path):
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    (download_dir / "10_existing.jpg").write_bytes(b"done")
    source = tmp_path / "export.json"
    manifest = tmp_path / "manifest.json"
    source.write_text(
        json.dumps({"messages": [{"id": 10}, {"id": 11}]}),
        encoding="utf-8",
    )

    assert _prepare_manifest(source, manifest, download_dir) == 1
    assert [item["id"] for item in json.loads(manifest.read_text())["messages"]] == [11]


def test_copy_without_overwrite_preserves_existing_file(tmp_path):
    source = tmp_path / "source.bin"
    target = tmp_path / "target.bin"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    _copy_without_overwrite(source, target)

    assert target.read_bytes() == b"old"
    assert not source.exists()


def test_run_once_persists_state_and_does_not_download_again(tmp_path):
    paths = make_paths(tmp_path)
    state = StateStore(paths.state_db)
    config = DownloadConfig(channel="@example", download_dir=paths.download_dir)
    save_config(paths, config)
    fake = FakeTdl(content=b"first")

    assert run_once(paths, config, state, tdl=fake) == 0
    assert target_file(paths).read_bytes() == b"first"
    assert not paths.staging_dir.exists()
    first_run = state.latest_runs(1)[0]
    assert first_run["status"] == "completed"
    assert first_run["completed"] == 1
    assert first_run["bytes_completed"] == 5

    assert run_once(paths, config, state, tdl=fake) == 0
    assert target_file(paths).read_bytes() == b"first"
    assert fake.downloads == 1
    assert state.latest_runs(1)[0]["status"] == "completed"


def test_interrupted_run_can_resume(tmp_path):
    paths = make_paths(tmp_path)
    state = StateStore(paths.state_db)
    config = DownloadConfig(channel="@example", download_dir=paths.download_dir)
    interrupted = FakeTdl(download_return_code=130)

    assert run_once(paths, config, state, tdl=interrupted) == 130
    assert state.active_run()["status"] == "interrupted"

    resumed = FakeTdl(content=b"resumed")
    assert run_once(paths, config, state, tdl=resumed) == 0
    assert target_file(paths).read_bytes() == b"resumed"
    assert state.active_run() is None


def test_nonzero_tdl_download_is_resumed_after_retry_gap(tmp_path):
    paths = make_paths(tmp_path)
    state = StateStore(paths.state_db)
    config = DownloadConfig(channel="@example", download_dir=paths.download_dir)
    save_config(paths, config)

    class FlakyTdl(FakeTdl):
        def __init__(self):
            super().__init__()
            self.return_codes = [1, 0]

        def download(self, config, manifest, work_dir):
            self.download_return_code = self.return_codes.pop(0)
            return super().download(config, manifest, work_dir)

    fake = FlakyTdl()
    sleeps = []

    assert run_with_retries(
        paths,
        state,
        tdl=fake,
        retry_gap=5,
        sleep=sleeps.append,
    ) == 0
    assert sleeps == [5]
    assert fake.downloads == 2
    assert target_file(paths).read_bytes() == b"media"
    assert state.active_run() is None


def test_next_run_reuses_staging_from_previous_failed_run(tmp_path):
    paths = make_paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db)
    config = DownloadConfig(channel="@example", download_dir=paths.download_dir)

    legacy_work_dir = paths.staging_dir / "1"
    legacy_work_dir.mkdir(parents=True)
    (legacy_work_dir / "1_photo.jpg").write_bytes(b"already downloaded")
    old_source = paths.jobs_dir / "1" / "export.json"
    old_manifest = paths.jobs_dir / "1" / "manifest.json"
    old_run_id = state.create_run(
        channel=config.channel,
        media_types=",".join(config.media_types),
        source_path=old_source,
        manifest_path=old_manifest,
        work_dir=legacy_work_dir,
    )
    state.update_run(
        old_run_id,
        status="failed",
        stage="failed",
        return_code=1,
        last_error="tdl download exited with code 1",
    )

    class ObservingTdl(FakeTdl):
        def __init__(self):
            super().__init__()
            self.preexisting_file = False
            self.work_dir = None

        def download(self, config, manifest, work_dir):
            self.work_dir = work_dir
            self.preexisting_file = (work_dir / "1_photo.jpg").exists()
            return super().download(config, manifest, work_dir)

    fake = ObservingTdl()

    assert run_once(paths, config, state, tdl=fake) == 0
    assert fake.work_dir is None
    assert fake.preexisting_file is False
    assert target_file(paths).read_bytes() == b"already downloaded"
    assert not legacy_work_dir.exists()


def test_work_progress_counts_temporary_files(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "done.bin").write_bytes(b"123")
    (work_dir / "partial.bin.tmp").write_bytes(b"12")

    assert _work_progress(work_dir) == (1, 3, 1)
