from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from .config import (
    MEDIA_TYPES,
    AppPaths,
    DownloadConfig,
    load_config,
    save_config,
)
from .runner import run_daemon, run_once
from .state import StateStore
from .tdl import TdlError, TdlRunner, ensure_tdl_available


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-downloader",
        description="Docker-friendly tdl wrapper for scheduled Telegram downloads.",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="run the complete login and setup wizard")
    init.add_argument(
        "--channel",
        help="optional channel reference for non-interactive setup; normally choose from the list",
    )
    init.add_argument("--types", help="comma-separated media types")
    init.add_argument("--daily", action=argparse.BooleanOptionalAction, default=None)
    init.add_argument("--daily-time", help="daily run time in local HH:MM")
    init.add_argument(
        "--login-type",
        choices=("qr", "code", "desktop"),
        default="qr",
        help="tdl login mode used by the wizard",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing config")

    configure = commands.add_parser("configure", help="edit channel, media, and schedule")
    configure.add_argument("--channel", help="channel username, link, or numeric ID")
    configure.add_argument("--types", help="comma-separated media types")
    configure.add_argument("--daily", action=argparse.BooleanOptionalAction, default=None)
    configure.add_argument("--daily-time", help="daily run time in local HH:MM")

    login = commands.add_parser("login", help="login through tdl")
    login.add_argument(
        "--type", choices=("qr", "code", "desktop"), default="qr",
        help="tdl login mode; QR is the default for Docker",
    )

    commands.add_parser("channels", help="list chats/channels from the logged-in account")
    commands.add_parser("tdl-version", help="show the installed tdl version")

    commands.add_parser("run", help="run one download job, resuming an interrupted job")

    commands.add_parser("daemon", help="run now and repeat daily when enabled")

    status = commands.add_parser("status", help="show configuration and download progress")
    status.add_argument("--json", action="store_true", help="print machine-readable JSON")

    commands.add_parser("reset", help="abandon the active job without deleting downloads")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def _prompt_bool(label: str, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    value = input(f"{label} ({default_text}): ").strip().lower()
    if not value:
        return default
    if value in {"y", "yes", "true", "1"}:
        return True
    if value in {"n", "no", "false", "0"}:
        return False
    raise ValueError("Please answer yes or no")


def _channel_options(raw_channels: list[dict[str, object]]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for item in raw_channels:
        chat_type = str(item.get("type", "")).lower()
        if chat_type not in {"channel", "group"}:
            continue

        raw_id = item.get("id")
        if not isinstance(raw_id, (int, str)) or not str(raw_id).strip():
            continue
        channel_id = str(raw_id).strip()
        raw_username = str(item.get("username", "")).strip()
        username = "" if raw_username in {"", "-"} else raw_username.lstrip("@")
        reference = f"@{username}" if username else channel_id
        visible_name = str(item.get("visible_name", "")).strip() or "Unnamed channel"
        options.append(
            {
                "id": channel_id,
                "name": visible_name,
                "type": chat_type,
                "username": username,
                "reference": reference,
            }
        )
    return options


def _select_channel(raw_channels: list[dict[str, object]], requested: str | None = None) -> str:
    options = _channel_options(raw_channels)
    print("\nAvailable Telegram channels and groups:")
    if not options:
        print("  No non-archived channel/group chats were returned by tdl.")
    for index, option in enumerate(options, start=1):
        username = f"@{option['username']}" if option["username"] else "private"
        print(
            f"  {index}. {option['name']} "
            f"({option['type']}, {username}, id={option['id']})"
        )

    if requested:
        normalized = requested.strip().lstrip("@").lower()
        for option in options:
            candidates = {
                option["id"].lower(),
                option["reference"].lstrip("@").lower(),
                option["username"].lower(),
            }
            if normalized in candidates:
                print(f"Selected channel: {option['reference']}")
                return option["reference"]
        print(f"Using requested chat reference: {requested.strip()}")
        return requested.strip()

    manual_index = len(options) + 1
    print(f"  {manual_index}. Enter a chat username, link, or numeric ID manually")
    while True:
        selected = _prompt("Select chat number", "1")
        try:
            index = int(selected)
        except ValueError:
            index = 0
        if index == manual_index:
            reference = _prompt("Chat username/link/numeric ID")
            if reference:
                return reference
            print("A chat reference is required.")
            continue
        if 1 <= index <= len(options):
            choice = options[index - 1]
            print(f"Selected channel: {choice['reference']}")
            return choice["reference"]
        print(f"Please enter a number from 1 to {manual_index}.")


def _prepare_session(tdl: TdlRunner) -> tuple[bool, list[dict[str, object]]]:
    """Return existing chats when the current tdl session is usable."""

    if not tdl.namespace_data_exists():
        return False, []

    usable, chats = tdl.probe_session()
    if usable:
        print(f"Existing usable tdl session found in namespace '{tdl.namespace}'.")
        while True:
            action = _prompt(
                "Use it, delete it and login again, or cancel? (use/delete/cancel)",
                "use",
            ).lower()
            if action in {"use", "u", "yes", "y"}:
                return True, chats
            if action in {"delete", "d", "reset", "relogin"}:
                tdl.clear_namespace_data()
                return False, []
            if action in {"cancel", "c", "no", "n"}:
                raise RuntimeError("Initialization cancelled; existing tdl session was kept.")
            print("Please answer use, delete, or cancel.")

    print(
        f"Existing tdl data was found in namespace '{tdl.namespace}', "
        "but the session could not be verified."
    )
    if _prompt_bool("Delete it and login again", False):
        tdl.clear_namespace_data()
        return False, []
    raise RuntimeError("Initialization cancelled; existing tdl data was kept.")


def _config_from_answers(
    paths: AppPaths,
    *,
    existing: DownloadConfig | None,
    channel: str | None,
    media_types: str | None,
    daily: bool | None,
    daily_time: str | None,
    account_id: str | None = None,
) -> DownloadConfig:
    current = existing
    selected_channel = channel or _prompt(
        "Telegram channel username/link/ID",
        current.channel if current else None,
    )
    selected_types = media_types or _prompt(
        f"Media types ({', '.join(MEDIA_TYPES)})",
        ",".join(current.media_types) if current else "all",
    )
    selected_daily = (
        daily
        if daily is not None
        else _prompt_bool("Enable daily automatic download", current.daily_enabled if current else False)
    )
    selected_time = daily_time
    if selected_daily and selected_time is None:
        selected_time = _prompt("Daily run time (HH:MM)", current.daily_time if current else "03:00")
    if selected_time is None:
        selected_time = current.daily_time if current else "03:00"

    return DownloadConfig(
        channel=selected_channel,
        telegram_user_id=(
            account_id
            or (current.telegram_user_id if current else "")
            or os.getenv("TDL_USER_ID", "")
        ),
        media_types=selected_types,
        daily_enabled=selected_daily,
        daily_time=selected_time,
        download_dir=current.download_dir if current else paths.download_dir,
        threads=current.threads if current else int(os.getenv("TDL_THREADS", "4")),
        concurrency=current.concurrency if current else int(os.getenv("TDL_LIMIT", "2")),
        delay_seconds=current.delay_seconds if current else float(os.getenv("TDL_DELAY_SECONDS", "0")),
        takeout=current.takeout if current else os.getenv("TDL_TAKEOUT", "false").lower() in {"1", "true", "yes"},
    )


def _init(paths: AppPaths, args: argparse.Namespace, *, tdl: TdlRunner | None = None) -> int:
    if paths.config_file.exists() and not args.force:
        raise RuntimeError(
            f"Configuration already exists at {paths.config_file}; use 'configure' or init --force."
        )
    tdl = tdl or TdlRunner(paths)
    print("\nStep 1/4: Checking tdl session")
    use_existing_session, channels = _prepare_session(tdl)
    if use_existing_session:
        print("Using the existing tdl session; no login data will be overwritten.")
    else:
        print("Logging in to Telegram through tdl")
        login_return_code = tdl.run_interactive(("login", "--type", args.login_type))
        if login_return_code != 0:
            return login_return_code or 1
        channels = tdl.list_chats_json()

    print("\nStep 2/4: Loading your Telegram chats")
    selected_channel = _select_channel(channels, args.channel)

    current_user_id = getattr(tdl, "current_user_id", None)
    try:
        account_id = current_user_id() if callable(current_user_id) else None
        if not account_id:
            raise RuntimeError("tdl runner does not expose current_user_id")
    except (RuntimeError, TdlError, OSError) as exc:
        account_id = os.getenv("TDL_USER_ID", "").strip() or tdl.namespace
        print(
            f"WARN: Could not resolve the Telegram user ID ({exc}); "
            f"using account directory {account_id!r}.",
            file=sys.stderr,
        )

    print("\nStep 3/4: Download settings")
    config = _config_from_answers(
        paths,
        existing=None,
        account_id=account_id,
        channel=selected_channel,
        media_types=args.types,
        daily=args.daily,
        daily_time=args.daily_time,
    )
    save_config(paths, config)
    print(f"\nStep 4/4: Saved configuration to {paths.config_file}")
    print("Setup complete. Start the service with: docker compose up -d")
    return 0


def _configure(paths: AppPaths, args: argparse.Namespace) -> int:
    existing = load_config(paths)
    config = _config_from_answers(
        paths,
        existing=existing,
        account_id=None,
        channel=args.channel,
        media_types=args.types,
        daily=args.daily,
        daily_time=args.daily_time,
    )
    save_config(paths, config)
    print(f"Updated configuration at {paths.config_file}")
    return 0


def _status(paths: AppPaths, state: StateStore, as_json: bool) -> int:
    config: dict[str, Any] | None = None
    if paths.config_file.exists():
        config = load_config(paths).to_dict()
    payload = {"config": config, "active": state.active_run(), "recent_runs": state.latest_runs()}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if config is None:
        print("Configuration: not initialized")
    else:
        print(
            f"Configuration: channel={config['channel']} "
            f"types={','.join(config['media_types'])} "
            f"daily={'on at ' + config['daily_time'] if config['daily_enabled'] else 'off'}"
        )
    active = payload["active"]
    if active is not None:
        print(
            f"Active job #{active['id']}: {active['status']} / {active['stage']} "
            f"progress={active['completed']}/{active['expected']} "
            f"bytes={active['bytes_completed']} temp_files={active['temp_files']}"
        )
        if active.get("last_error"):
            print(f"Last error: {active['last_error']}")
    else:
        print("Active job: none")
    print("Recent jobs:")
    for run in payload["recent_runs"]:
        print(
            f"  #{run['id']} {run['status']:<11} "
            f"{run['completed']}/{run['expected']} "
            f"{run['channel']} ({run['created_at']})"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.log_level)
    paths = AppPaths.from_env()
    state = StateStore(paths.state_db)

    try:
        if args.command == "init":
            tdl = TdlRunner(paths)
            ensure_tdl_available(paths)
            return _init(paths, args, tdl=tdl)
        if args.command == "configure":
            return _configure(paths, args)

        tdl = TdlRunner(paths)
        if args.command == "login":
            ensure_tdl_available(paths)
            return tdl.run_interactive(("login", "--type", args.type))
        if args.command == "channels":
            ensure_tdl_available(paths)
            return tdl.list_chats()
        if args.command == "tdl-version":
            ensure_tdl_available(paths)
            return tdl.version()
        if args.command == "status":
            return _status(paths, state, args.json)
        if args.command == "reset":
            run_id = state.abandon_active()
            print(f"Abandoned active job #{run_id}. Existing downloads were not removed." if run_id else "No active job.")
            return 0

        config = load_config(paths)
        ensure_tdl_available(paths)
        if args.command == "run":
            return run_once(paths, config, state, tdl=tdl)
        if args.command == "daemon":
            return run_daemon(paths, state, tdl=tdl)
    except (KeyboardInterrupt, EOFError):
        print("Interrupted.", file=sys.stderr)
        return 130
    except (RuntimeError, TdlError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
