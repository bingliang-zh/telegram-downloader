from __future__ import annotations

import json
import os
import re
import shutil
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, Callable

from .config import AppPaths, DownloadConfig, load_config, save_config
from .state import StateStore, utc_now
from .tdl import TdlError, TdlRunner


_MESSAGE_FILE_RE = re.compile(r"^(\d+)_")
_DEFAULT_RETRY_GAP_SECONDS = 300.0
_TOPIC_TEXT_WINDOW_SECONDS = 300
_TOPIC_TEXT_MAX_LENGTH = 80


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_export(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid or missing tdl export file: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise RuntimeError(f"Unexpected tdl export format: {path}")
    return data


def _message_id_from_filename(path: Path) -> int | None:
    match = _MESSAGE_FILE_RE.match(path.name)
    if match is None:
        return None
    return int(match.group(1))


def _existing_message_ids(download_dir: Path) -> set[int]:
    """Find completed wrapper files without treating staging files as done."""

    result: set[int] = set()
    if not download_dir.exists():
        return result
    staging_root = (download_dir / ".telegram-downloader").resolve()
    for path in download_dir.rglob("*"):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        try:
            path.resolve().relative_to(staging_root)
        except ValueError:
            pass
        else:
            continue
        message_id = _message_id_from_filename(path)
        if message_id is not None:
            result.add(message_id)
    return result


def _prepare_manifest(source: Path, manifest: Path, download_dir: Path) -> int:
    data = _read_export(source)
    existing_ids = _existing_message_ids(download_dir)
    messages = []
    for message in data["messages"]:
        if not isinstance(message, dict):
            continue
        file_name = message.get("file")
        if not isinstance(file_name, str) or not file_name.strip():
            continue
        message_id = message.get("id")
        if isinstance(message_id, int) and message_id in existing_ids:
            continue
        messages.append(message)
    data["messages"] = messages
    _write_json(manifest, data)
    return len(messages)


def _manifest_message_ids(manifest: Path) -> set[int]:
    return {
        int(message["id"])
        for message in _read_export(manifest)["messages"]
        if isinstance(message, dict) and isinstance(message.get("id"), int)
    }


def _raw_message(message: dict[str, Any]) -> dict[str, Any] | None:
    raw = message.get("raw")
    return raw if isinstance(raw, dict) else None


def _message_timestamp(message: dict[str, Any]) -> int | None:
    timestamp = _int_value(message.get("date"))
    if timestamp is not None:
        return timestamp
    raw = _raw_message(message)
    if raw is None:
        return None
    return _int_value(raw.get("date") or raw.get("Date"))


def _message_text(message: dict[str, Any]) -> str:
    raw = _raw_message(message)
    for source in (message, raw or {}):
        for key in ("text", "Text", "message", "Message"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _message_grouped_id(message: dict[str, Any]) -> int | None:
    raw = _raw_message(message)
    for source in (message, raw or {}):
        for key in ("grouped_id", "groupedId", "GroupedID"):
            grouped_id = _int_value(source.get(key))
            if grouped_id is not None:
                return grouped_id
    return None


def _message_has_file(message: dict[str, Any]) -> bool:
    file_name = message.get("file")
    return isinstance(file_name, str) and bool(file_name.strip())


def _is_text_topic_candidate(text: str) -> bool:
    return (
        bool(text)
        and len(text) <= _TOPIC_TEXT_MAX_LENGTH
        and "\n" not in text
        and "\r" not in text
    )


def _int_value(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _message_topic_id(message: dict[str, Any]) -> int | None:
    """Read a Telegram forum topic ID from tdl's raw message JSON."""

    raw = _raw_message(message)
    if raw is None:
        return _int_value(message.get("topic_id"))

    for key in ("topic_id", "topicId", "TopicID", "thread_id", "threadId"):
        topic_id = _int_value(raw.get(key))
        if topic_id is not None:
            return topic_id

    reply_to = raw.get("reply_to") or raw.get("replyTo") or raw.get("ReplyTo")
    if isinstance(reply_to, dict):
        forum_topic = reply_to.get("ForumTopic", reply_to.get("forum_topic"))
        if forum_topic is False:
            return _int_value(message.get("topic_id"))
        for key in (
            "reply_to_top_id", "replyToTopID", "reply_to_top_msg_id",
            "ReplyToTopID", "ReplyToTopMsgID",
        ):
            topic_id = _int_value(reply_to.get(key))
            if topic_id is not None:
                return topic_id
    return _int_value(message.get("topic_id"))


def _message_date_folder(message: dict[str, Any]) -> str:
    timestamp = _message_timestamp(message)
    if timestamp is None:
        return "unknown-date"
    try:
        # The container's TZ setting defines the archive's local calendar date.
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "unknown-date"


def _safe_folder_component(value: str, *, max_length: int = 80) -> str:
    value = unicodedata.normalize("NFKC", str(value)).strip()
    value = re.sub(r"[\x00-\x1f\x7f<>:\"/\\|?*]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:max_length].rstrip(" .") or "untitled")


def _message_download_directory(
    download_root: Path,
    message: dict[str, Any],
) -> Path:
    directory = _message_date_folder(message)
    topic_source = str(message.get("topic_source", "")).strip()
    topic_title = str(message.get("topic_title", "")).strip()
    if topic_source in {"caption", "text"} and topic_title:
        return download_root / f"{directory}_{_safe_folder_component(topic_title)}"
    topic_id = _message_topic_id(message)
    if topic_id is not None:
        title = topic_title or f"topic-{topic_id}"
        directory += f"_{topic_id}_{_safe_folder_component(title)}"
    return download_root / directory


def _enrich_export(source: Path, topic_titles: dict[int, str]) -> None:
    """Add stable date/topic fields to the tdl export used by the manifest.

    Telegram group albums carry their subject as a caption on one album
    message, while the other messages in the same ``GroupedID`` are blank.
    Plain text immediately before an uncaptioned media run is also accepted
    as a topic for a short, same-day window.
    """

    data = _read_export(source)
    messages = [message for message in data["messages"] if isinstance(message, dict)]
    changed = False

    def set_field(message: dict[str, Any], key: str, value: Any) -> None:
        nonlocal changed
        if value is None:
            if key in message:
                del message[key]
                changed = True
            return
        if message.get(key) != value:
            message[key] = value
            changed = True

    for message in messages:
        timestamp = _message_timestamp(message)
        if "date" not in message and timestamp is not None:
            set_field(message, "date", timestamp)
        topic_id = _message_topic_id(message)
        set_field(message, "topic_id", topic_id)
        if topic_id is not None:
            title = topic_titles.get(topic_id, f"topic-{topic_id}")
            set_field(message, "topic_title", title)
            set_field(message, "topic_source", "forum")

    ordered = sorted(
        messages,
        key=lambda item: (_message_timestamp(item) or 0, _int_value(item.get("id")) or 0),
    )
    albums: dict[int, list[dict[str, Any]]] = {}
    album_captions: dict[int, str] = {}
    for message in ordered:
        if not _message_has_file(message):
            continue
        grouped_id = _message_grouped_id(message)
        if grouped_id is not None:
            albums.setdefault(grouped_id, []).append(message)
            caption = _message_text(message)
            if caption and grouped_id not in album_captions:
                album_captions[grouped_id] = caption

    pending_text: tuple[int, str] | None = None
    handled_albums: set[int] = set()
    for message in ordered:
        timestamp = _message_timestamp(message)
        if not _message_has_file(message):
            text = _message_text(message)
            if timestamp is not None and _is_text_topic_candidate(text):
                pending_text = (timestamp, text)
            continue

        grouped_id = _message_grouped_id(message)
        if grouped_id is not None:
            if grouped_id in handled_albums:
                continue
            handled_albums.add(grouped_id)
            members = albums[grouped_id]
            caption = album_captions.get(grouped_id, "")
        else:
            members = [message]
            caption = _message_text(message)

        topic = caption
        source_name = "caption" if caption else ""
        if not topic and pending_text is not None and timestamp is not None:
            pending_timestamp, pending_topic = pending_text
            same_day = _message_date_folder(message) == _message_date_folder(
                {"date": pending_timestamp}
            )
            if same_day and 0 <= timestamp - pending_timestamp <= _TOPIC_TEXT_WINDOW_SECONDS:
                topic = pending_topic
                source_name = "text"

        if topic:
            for member in members:
                set_field(member, "topic_title", topic)
                set_field(member, "topic_source", source_name)
    if changed:
        _write_json(source, data)


def _topic_titles(tdl: TdlRunner, chat_id: str) -> dict[int, str]:
    getter = getattr(tdl, "topics_for_chat", None)
    if not callable(getter):
        return {}
    try:
        return getter(chat_id)
    except (TdlError, OSError, RuntimeError, ValueError) as exc:
        print(f"WARN: Could not load Telegram topic names ({exc}); using topic IDs.", flush=True)
        return {}


def _relocate_downloads(
    work_dir: Path,
    download_root: Path,
    manifest: Path,
) -> None:
    """Move completed flat tdl files into date/topic directories."""

    messages = {
        int(message["id"]): message
        for message in _read_export(manifest)["messages"]
        if isinstance(message, dict) and _int_value(message.get("id")) is not None
    }
    if not work_dir.is_dir():
        return
    for path in sorted(work_dir.iterdir()):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        message_id = _message_id_from_filename(path)
        if message_id is None or message_id not in messages:
            continue
        target = _message_download_directory(download_root, messages[message_id]) / path.name
        _copy_without_overwrite(path, target)

    if work_dir != download_root:
        for directory in sorted(
            (path for path in work_dir.rglob("*") if path.is_dir()),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            work_dir.rmdir()
        except OSError:
            pass
        for parent in (work_dir.parent, work_dir.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                pass


def _relocate_existing_downloads(download_root: Path, source: Path) -> None:
    """Organize already downloaded files covered by a new metadata export."""

    messages = {
        int(message["id"]): message
        for message in _read_export(source)["messages"]
        if isinstance(message, dict) and _int_value(message.get("id")) is not None
    }
    staging_root = (download_root / ".telegram-downloader").resolve()
    for path in sorted(download_root.rglob("*")):
        if not path.is_file() or path.suffix == ".tmp":
            continue
        try:
            path.resolve().relative_to(staging_root)
        except ValueError:
            pass
        else:
            continue
        message_id = _message_id_from_filename(path)
        if message_id is None or message_id not in messages:
            continue
        target = _message_download_directory(download_root, messages[message_id]) / path.name
        if path.resolve() == target.resolve():
            continue
        _copy_without_overwrite(path, target)


def _work_progress(
    work_dir: Path,
    tracked_message_ids: set[int] | None = None,
) -> tuple[int, int, int]:
    completed = 0
    bytes_completed = 0
    temp_files = 0
    if not work_dir.exists():
        return completed, bytes_completed, temp_files
    for path in work_dir.rglob("*"):
        if not path.is_file():
            continue
        match = _MESSAGE_FILE_RE.match(path.name)
        if tracked_message_ids is not None and (
            match is None or int(match.group(1)) not in tracked_message_ids
        ):
            continue
        if path.suffix == ".tmp":
            temp_files += 1
            continue
        completed += 1
        try:
            bytes_completed += path.stat().st_size
        except OSError:
            pass
    return completed, bytes_completed, temp_files


_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _safe_path_component(value: str, label: str) -> str:
    value = str(value).strip()
    if not value or not _PATH_COMPONENT_RE.fullmatch(value):
        raise RuntimeError(f"Invalid {label} for download path: {value!r}")
    return value


def _export_chat_id(source: Path) -> str:
    value = _read_export(source).get("id")
    if isinstance(value, int):
        return _safe_path_component(str(value), "Telegram chat ID")
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return _safe_path_component(value, "Telegram chat ID")
    raise RuntimeError(f"tdl export did not contain a valid Telegram chat ID: {source}")


def _resolve_account_id(
    paths: AppPaths,
    config: DownloadConfig,
    tdl: TdlRunner,
) -> str:
    configured = config.telegram_user_id.strip() or os.getenv("TDL_USER_ID", "").strip()
    if configured:
        return _safe_path_component(configured, "Telegram user ID")

    current_user_id = getattr(tdl, "current_user_id", None)
    if callable(current_user_id):
        try:
            configured = str(current_user_id()).strip()
        except (TdlError, OSError, RuntimeError) as exc:
            print(
                f"WARN: Could not resolve Telegram user ID through tdl ({exc}); "
                "using the tdl namespace as the account directory.",
                flush=True,
            )
        else:
            config.telegram_user_id = configured
            save_config(paths, config)
            return _safe_path_component(configured, "Telegram user ID")

    namespace = getattr(tdl, "namespace", None) or os.getenv("TDL_NS", "default")
    return _safe_path_component(str(namespace), "tdl namespace")


def _download_target(
    paths: AppPaths,
    config: DownloadConfig,
    tdl: TdlRunner,
    source: Path,
) -> Path:
    account_id = _resolve_account_id(paths, config, tdl)
    chat_id = _export_chat_id(source)
    target = config.download_dir / account_id / chat_id
    target.mkdir(parents=True, exist_ok=True)
    return target


def _copy_without_overwrite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        # The normal case is a hard link within the mapped downloads volume.
        # Unlike rename(), link() fails instead of replacing an existing file.
        os.link(source, target)
        source.unlink()
        return
    except FileExistsError:
        source.unlink()
        return
    except OSError:
        # Docker users may map the staging directory differently in the future.
        # Fall back to an exclusive copy that retains the same no-overwrite rule.
        pass

    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        source.unlink()
        return
    try:
        with os.fdopen(fd, "wb") as destination, source.open("rb") as original:
            shutil.copyfileobj(original, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        shutil.copystat(source, target, follow_symlinks=False)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    else:
        source.unlink()


def _merge_staging_directory(source: Path, target: Path) -> None:
    """Move files from an older staging directory into the shared one."""

    if source.resolve() == target.resolve() or not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for source_file in sorted(source.rglob("*")):
        if not source_file.is_file():
            continue
        target_file = target / source_file.relative_to(source)
        _copy_without_overwrite(source_file, target_file)

    for directory in sorted(
        (path for path in source.rglob("*") if path.is_dir()),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        source.rmdir()
    except OSError:
        pass


def _merge_matching_staging(
    paths: AppPaths,
    state: StateStore,
    config: DownloadConfig,
    target: Path,
) -> None:
    """Reuse old staging files belonging to this channel and media selection."""

    media_types = ",".join(config.media_types)
    staging_root = paths.staging_dir.resolve()
    target_resolved = target.resolve()
    for run in state.latest_runs(limit=1000):
        if run["channel"] != config.channel or run["media_types"] != media_types:
            continue
        candidate = Path(str(run["work_dir"]))
        candidate_resolved = candidate.resolve()
        if candidate_resolved == target_resolved or not candidate.is_dir():
            continue
        try:
            candidate_resolved.relative_to(staging_root)
        except ValueError:
            continue
        _merge_staging_directory(candidate, target)
    try:
        paths.staging_dir.rmdir()
    except OSError:
        pass


def _validate_active_config(active: dict[str, Any], config: DownloadConfig) -> None:
    media_types = ",".join(config.media_types)
    if active["channel"] != config.channel or active["media_types"] != media_types:
        raise RuntimeError(
            "An interrupted download has a different channel or media selection. "
            "Run 'telegram-downloader reset' before starting a new configuration."
        )


def run_once(
    paths: AppPaths,
    config: DownloadConfig,
    state: StateStore,
    *,
    tdl: TdlRunner | None = None,
    export_days: int | None = None,
) -> int:
    """Export one channel and download its not-yet-present media."""

    paths.ensure_directories()
    state.mark_running_as_interrupted()
    tdl = tdl or TdlRunner(paths)
    active = state.active_run()

    if active is not None:
        _validate_active_config(active, config)
        run_id = int(active["id"])
        source = Path(active["source_path"])
        manifest = Path(active["manifest_path"])
        work_dir = Path(active["work_dir"])
    else:
        run_id = 0
        source = manifest = work_dir = Path()

    if active is None:
        job_hint = paths.jobs_dir / f"new-{uuid.uuid4().hex}"
        job_hint.mkdir(parents=True, exist_ok=True)
        source = job_hint / "export.json"
        manifest = job_hint / "manifest.json"
        work_dir = paths.staging_dir / job_hint.name
        run_id = state.create_run(
            channel=config.channel,
            media_types=",".join(config.media_types),
            source_path=source,
            manifest_path=manifest,
            work_dir=work_dir,
        )
        # Rename the temporary job directory to the numeric run ID after the
        # insert. The state row is then stable and easy to inspect by hand.
        final_job_dir = paths.jobs_dir / str(run_id)
        job_hint.rename(final_job_dir)
        source = final_job_dir / "export.json"
        manifest = final_job_dir / "manifest.json"
        work_dir = paths.staging_dir / str(run_id)
        state.update_run(
            run_id,
            source_path=str(source),
            manifest_path=str(manifest),
            work_dir=str(work_dir),
        )

    try:
        if not source.exists():
            state.update_run(
                run_id,
                status="running",
                stage="exporting",
                started_at=utc_now(),
                ended_at=None,
                last_error=None,
            )
            if export_days is None:
                return_code = tdl.export(config, source)
            else:
                return_code = tdl.export(config, source, days=export_days)
            if return_code != 0:
                # tdl may leave a partial export file behind when the Telegram
                # connection is lost. It must not be reused on the next retry.
                source.unlink(missing_ok=True)
                state.update_run(
                    run_id,
                    status="interrupted",
                    stage="interrupted",
                    ended_at=utc_now(),
                    return_code=return_code,
                    last_error=f"tdl chat export exited with code {return_code}",
                )
                return return_code or 1

        # The export's top-level ID is the canonical Telegram peer ID. The
        # target is stable across daily runs and retries, unlike per-run
        # staging directories.
        chat_id = _export_chat_id(source)
        _enrich_export(source, _topic_titles(tdl, chat_id))
        download_root = _download_target(paths, config, tdl, source)
        _relocate_existing_downloads(download_root, source)
        if active is None:
            _merge_matching_staging(paths, state, config, download_root)

        # Rebuild from the immutable export on every retry. Existing files in
        # this account/chat directory are excluded by message ID.
        state.update_run(run_id, status="running", stage="preparing")
        expected = _prepare_manifest(source, manifest, download_root)
        state.update_run(run_id, expected=expected)

        if expected == 0:
            state.update_run(
                run_id,
                status="completed",
                stage="complete",
                started_at=utc_now(),
                ended_at=utc_now(),
                completed=0,
                bytes_completed=0,
                temp_files=0,
                return_code=0,
                last_error=None,
                work_dir=str(download_root),
            )
            return 0

        tracked_message_ids = _manifest_message_ids(manifest)

        state.update_run(
            run_id,
            status="running",
            stage="downloading",
            started_at=state.get_run(run_id).get("started_at") or utc_now(),
            ended_at=None,
            last_error=None,
        )

        stop_monitor = Event()

        def monitor() -> None:
            while not stop_monitor.is_set():
                completed, bytes_completed, temp_files = _work_progress(
                    work_dir,
                    tracked_message_ids,
                )
                state.update_run(
                    run_id,
                    completed=completed,
                    bytes_completed=bytes_completed,
                    temp_files=temp_files,
                )
                stop_monitor.wait(1.0)

        monitor_thread = Thread(target=monitor, name=f"progress-{run_id}", daemon=True)
        monitor_thread.start()
        try:
            return_code = tdl.download(config, manifest, work_dir)
        except KeyboardInterrupt:
            return_code = 130
        finally:
            stop_monitor.set()
            monitor_thread.join(timeout=2)

        completed, bytes_completed, temp_files = _work_progress(
            work_dir,
            tracked_message_ids,
        )
        state.update_run(
            run_id,
            completed=completed,
            bytes_completed=bytes_completed,
            temp_files=temp_files,
        )
        if return_code != 0:
            state.update_run(
                run_id,
                status="interrupted",
                stage="interrupted",
                ended_at=utc_now(),
                return_code=return_code,
                last_error=f"tdl download exited with code {return_code}",
            )
            return return_code or 1

        _relocate_downloads(work_dir, download_root, manifest)
        state.update_run(
            run_id,
            status="completed",
            stage="complete",
            ended_at=utc_now(),
            completed=completed,
            bytes_completed=bytes_completed,
            temp_files=temp_files,
            return_code=0,
            last_error=None,
            work_dir=str(download_root),
        )
        return 0
    except Exception as exc:
        state.update_run(
            run_id,
            status="failed",
            stage="failed",
            ended_at=utc_now(),
            last_error=str(exc),
        )
        raise


def retry_gap_seconds() -> float:
    """Return the delay between retrying failed tdl attempts."""

    raw_value = os.getenv(
        "DOWNLOAD_RETRY_GAP_SECONDS",
        str(int(_DEFAULT_RETRY_GAP_SECONDS)),
    ).strip()
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            "DOWNLOAD_RETRY_GAP_SECONDS must be a non-negative number of seconds"
        ) from exc
    if value < 0:
        raise ValueError("DOWNLOAD_RETRY_GAP_SECONDS must be non-negative")
    return value


def _sleep_retry_gap(
    seconds: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Sleep in short chunks so the daemon remains responsive to shutdown."""

    remaining = seconds
    while remaining > 0:
        interval = min(60.0, remaining)
        sleep(interval)
        remaining -= interval


def run_with_retries(
    paths: AppPaths,
    state: StateStore,
    *,
    tdl: TdlRunner,
    retry_gap: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Retry resumable tdl failures until one complete run succeeds.

    A non-zero tdl exit is kept as an interrupted run by ``run_once``. The
    next attempt therefore reuses the same export, manifest, staging files,
    and progress state. Exceptions are intentionally not swallowed because
    malformed data and local filesystem errors are not transient downloads.
    """

    gap = retry_gap if retry_gap is not None else retry_gap_seconds()
    if gap < 0:
        raise ValueError("retry_gap must be non-negative")

    attempt = 0
    while True:
        attempt += 1
        config = load_config(paths)
        result = run_once(paths, config, state, tdl=tdl)
        if result == 0:
            return 0
        if result in (130, -2):
            return result

        print(
            f"Download attempt {attempt} exited with code {result}; "
            f"retrying in {gap:g}s.",
            flush=True,
        )
        _sleep_retry_gap(gap, sleep=sleep)


def next_daily_run(daily_time: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    hour, minute = (int(part) for part in daily_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run_daemon(paths: AppPaths, state: StateStore, *, tdl: TdlRunner | None = None) -> int:
    """Run immediately, then repeat at the configured local time if enabled."""

    tdl = tdl or TdlRunner(paths)
    config = load_config(paths)
    result = run_with_retries(paths, state, tdl=tdl)
    if not config.daily_enabled:
        return result

    while True:
        config = load_config(paths)
        target = next_daily_run(config.daily_time)
        print(f"Next scheduled download: {target.isoformat(timespec='minutes')}", flush=True)
        while True:
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(60.0, remaining))
        try:
            result = run_with_retries(paths, state, tdl=tdl)
        except Exception as exc:
            print(f"Scheduled download failed: {exc}", flush=True)
            result = 1
        if result in (130, -2):
            return result
        if result:
            print(f"Scheduled download exited with code {result}; will retry at the next schedule.", flush=True)
