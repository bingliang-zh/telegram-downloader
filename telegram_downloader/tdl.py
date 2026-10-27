from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from .config import AppPaths, DownloadConfig


class TdlError(RuntimeError):
    pass


class TdlRunner:
    """Command adapter for the pinned tdl binary."""

    def __init__(self, paths: AppPaths, executable: str | None = None):
        self.paths = paths
        self.executable = executable or os.getenv("TDL_BIN", "tdl")
        self.namespace = os.getenv("TDL_NS", "default").strip() or "default"
        self.proxy = os.getenv("TDL_PROXY", "").strip()
        self.reconnect_timeout = os.getenv("TDL_RECONNECT_TIMEOUT", "10m").strip()
        self.ntp = os.getenv("TDL_NTP", "").strip()

    def _common_args(self, config: DownloadConfig | None = None) -> list[str]:
        args = [
            "--ns", self.namespace,
            "--storage", f"type=bolt,path={self.paths.tdl_storage_dir}",
        ]
        if self.proxy:
            args.extend(("--proxy", self.proxy))
        if self.reconnect_timeout:
            args.extend(("--reconnect-timeout", self.reconnect_timeout))
        if self.ntp:
            args.extend(("--ntp", self.ntp))
        if config is not None:
            args.extend(("--threads", str(config.threads), "--limit", str(config.concurrency)))
            if config.delay_seconds:
                args.extend(("--delay", f"{config.delay_seconds:g}s"))
        return args

    def command(self, *args: str, config: DownloadConfig | None = None) -> list[str]:
        return [self.executable, *self._common_args(config), *args]

    @property
    def namespace_data_path(self) -> Path:
        """The Bolt file used by tdl for the current namespace."""

        return self.paths.tdl_storage_dir / self.namespace

    def namespace_data_exists(self) -> bool:
        try:
            return self.namespace_data_path.is_file() and self.namespace_data_path.stat().st_size > 0
        except OSError:
            return False

    def clear_namespace_data(self) -> None:
        """Remove only the current namespace, leaving other tdl accounts intact."""

        path = self.namespace_data_path
        if path.parent != self.paths.tdl_storage_dir:
            raise TdlError(f"Unsafe tdl namespace path: {path}")
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise TdlError(f"Unable to delete tdl namespace data: {path}") from exc

    def run_interactive(self, args: Sequence[str], *, config: DownloadConfig | None = None) -> int:
        command = self.command(*args, config=config)
        try:
            completed = subprocess.run(command, check=False)
        except FileNotFoundError as exc:
            raise TdlError(
                f"tdl executable not found: {self.executable}. Build the Docker image first "
                "or set TDL_BIN to a tdl binary."
            ) from exc
        return int(completed.returncode)

    def export(
        self,
        config: DownloadConfig,
        output: Path,
        *,
        days: int | None = None,
    ) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        if days is not None and days < 1:
            raise ValueError("days must be greater than 0")
        now = int(time.time())
        export_type = "id"
        export_input = "0,2147483647"
        if days is not None:
            export_type = "time"
            export_input = f"{now - days * 86400},{now}"
        return self.run_interactive(
            (
                "chat", "export",
                "--chat", config.channel,
                "--type", export_type,
                "--input", export_input,
                "--filter", config.filter_expression(),
                "--all",
                "--with-content",
                "--raw",
                "--output", str(output),
            ),
            config=config,
        )

    def topics_for_chat(self, chat_id: str | int) -> dict[int, str]:
        """Return the known forum topic titles for a chat."""

        wanted = str(chat_id).strip()
        for dialog in self.list_chats_json():
            if str(dialog.get("id", "")).strip() != wanted:
                continue
            topics = dialog.get("topics", [])
            if not isinstance(topics, list):
                return {}
            result: dict[int, str] = {}
            for topic in topics:
                if not isinstance(topic, dict):
                    continue
                try:
                    topic_id = int(topic.get("id"))
                except (TypeError, ValueError):
                    continue
                title = str(topic.get("title", "")).strip()
                if title:
                    result[topic_id] = title
            return result
        return {}

    def download(self, config: DownloadConfig, manifest: Path, work_dir: Path) -> int:
        work_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "download",
            "--file", str(manifest),
            "--dir", str(work_dir),
            "--template", "{{ .MessageID }}_{{ filenamify .FileName }}",
            "--skip-same",
            "--continue",
            "--rewrite-ext",
        ]
        if config.takeout:
            args.append("--takeout")
        return self.run_interactive(args, config=config)

    def list_chats(self) -> int:
        return self.run_interactive(("chat", "ls", "--output", "json"))

    def probe_session(self) -> tuple[bool, list[dict[str, object]]]:
        """Check whether the existing namespace can access Telegram."""

        if not self.namespace_data_exists():
            return False, []
        command = self.command("chat", "ls", "--output", "json")
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, OSError):
            return False, []
        if completed.returncode != 0:
            return False, []
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return False, []
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            return False, []
        return True, result

    def list_chats_json(self) -> list[dict[str, object]]:
        """Return tdl's machine-readable dialog list for the init wizard."""

        command = self.command("chat", "ls", "--output", "json")
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise TdlError(
                f"tdl executable not found: {self.executable}. Build the Docker image first "
                "or set TDL_BIN to a tdl binary."
            ) from exc

        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        if completed.returncode != 0:
            raise TdlError(
                f"tdl chat ls exited with code {completed.returncode}"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise TdlError("tdl chat ls did not return valid JSON") from exc
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TdlError("Unexpected JSON returned by tdl chat ls")
        return result

    def current_user_id(self) -> str:
        """Resolve the logged-in user's Telegram ID through tdl's Saved Messages peer."""

        self.paths.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.paths.data_dir) as temporary_dir:
            output = Path(temporary_dir) / "self.json"
            return_code = self.run_interactive(
                (
                    "chat", "export",
                    "--type", "last",
                    "--input", "0",
                    "--filter", "false",
                    "--output", str(output),
                )
            )
            if return_code != 0:
                raise TdlError(
                    f"tdl chat export for the current user exited with code {return_code}"
                )
            try:
                data = json.loads(output.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise TdlError("tdl did not return a valid current-user export") from exc
            user_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(user_id, int) and user_id > 0:
                return str(user_id)
            if isinstance(user_id, str) and user_id.strip().isdigit():
                return user_id.strip()
            raise TdlError("tdl current-user export did not contain a valid user ID")

    def version(self) -> int:
        return self.run_interactive(("version",))


def ensure_tdl_available(paths: AppPaths) -> None:
    """Fail early with a useful message before an interactive command starts."""

    executable = os.getenv("TDL_BIN", "tdl")
    if shutil.which(executable) is None and not Path(executable).exists():
        raise TdlError(
            f"tdl executable not found: {executable}. The Docker image should include it; "
            "for local development install tdl or set TDL_BIN."
        )
