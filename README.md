# telegram-downloader

English | [简体中文](README.zh-CN.md)

[![CI](https://github.com/bingliang-zh/telegram-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/bingliang-zh/telegram-downloader/actions/workflows/ci.yml)

A Docker-friendly wrapper around [tdl](https://github.com/iyear/tdl). It logs in with a Telegram user account and does not depend on Telethon or the Telegram Bot API. It is designed for continuously syncing media from a Telegram channel or group to a host directory.

The wrapper manages configuration, job state, progress tracking, daily scheduling, and non-overwriting downloads. Telegram login, chat access, and media transfers are handled by `tdl`.

## Quick Start with Docker

Prepare the host directories and environment file:

```bash
cp .env.example .env
mkdir -p data downloads
docker compose build
```

For first-time setup, run the interactive initialization wizard:

```bash
docker compose run --rm -it telegram-downloader init
```

The wizard performs these steps:

1. Checks whether the current tdl namespace contains a usable session. If it does, you can reuse it, delete it and log in again, or cancel.
2. If no usable session exists, logs in to a Telegram user account using a QR code by default.
3. Loads the channels and groups accessible to the account.
4. Lets you select a channel or group. If the target is in Archived Chats, use the manual entry at the bottom of the list and provide a username, message link, or numeric ID.
5. Lets you select media types, enable or disable daily downloads, and choose the scheduled time.
6. Saves the complete configuration to `data/config.json`.

Deleting a session only removes the current `TDL_NS` session. It does not remove other namespaces, downloaded files, or job state.

If the application has already been initialized and you want to run the full wizard again, use `--force`. You can also reduce the number of prompts by passing options:

```bash
docker compose run --rm telegram-downloader init \
  --channel '@example_channel' \
  --types photo,video \
  --daily \
  --daily-time 03:30
```

Start the service:

```bash
docker compose up -d
make logs
```

`make logs` filters only tdl's `CPU / Memory / Goroutines` resource-monitor lines while you view the logs. Telegram message download progress, errors, and wrapper retry messages remain visible. Use `make logs-raw` to view unfiltered logs.

The service starts one download immediately. If daily downloads are enabled, it remains running and starts another download at the configured local time each day. Otherwise, it exits after the first successful download. A successful tdl download process exits normally, and the wrapper starts a new tdl process at the next scheduled time.

If tdl exits because of a network error, the wrapper preserves the current job state and temporary files, waits for `DOWNLOAD_RETRY_GAP_SECONDS`, and retries with `--continue`. Completed files are not overwritten. Retries continue until the current download succeeds; local filesystem errors, invalid manifests, and other non-retryable failures are reported directly.

Restart the container after changing the configuration:

```bash
docker compose run --rm -it telegram-downloader configure
docker compose restart
```

## Runtime and Status Commands

```bash
docker compose run --rm telegram-downloader run
docker compose run --rm telegram-downloader status
docker compose run --rm telegram-downloader status --json
docker compose run --rm telegram-downloader tdl-version
docker compose run --rm telegram-downloader reset
```

`run` continues unfinished work. `reset` abandons only the current job record and does not delete downloaded files; you can run `run` again afterward.

The lower-level `login` and `channels` commands remain available for troubleshooting, but they are not required during normal first-time setup.

Telegram Archived Chats use a separate folder. The current tdl `chat ls` command does not return a separate archived list, so archived private groups must be entered manually using a message link or numeric ID. Alternatively, temporarily unarchive the group, complete initialization, and archive it again.

Supported media types are `all`, `photo`, `video`, `audio`, `voice`, `document`, and `sticker`. Selecting `all` exports every downloadable media item in the selected channel or group.

## Persistence and Non-Overwriting Downloads

Docker Compose uses bind mounts:

| Host path | Container path | Contents |
| --- | --- | --- |
| `./data` | `/data` | `config.json`, `state.db`, tdl login storage, and job manifests |
| `./downloads` | `/downloads` | Downloaded files organized by account and group |

Files are written directly to `/downloads/<telegram-user-id>/<telegram-group-id>/`, for example `/downloads/123456789/-100123456789/`. The account ID is resolved from tdl's Saved Messages peer, and the group ID is read from the top-level `id` in the tdl export. If an older configuration cannot resolve the account ID, the wrapper falls back to `TDL_NS` as the account directory.

Final filenames begin with the Telegram message ID, for example `12345_video.mp4`. `.tmp` files remain in the same group directory for resumable downloads. Existing complete files are skipped by message ID and are not overwritten.

SQLite stores the job phase, expected and completed file counts, completed bytes, temporary file count, manifest path, and working directory. Interrupted work can continue after a container restart.

Do not delete or share `data`: it contains the tdl login credentials. To migrate the service, copy the complete `data` and `downloads` directories.

## Configuration

The interactive `init` command saves configuration to `data/config.json`. Docker environment variables control container paths and tdl runtime options:

| Variable | Default | Description |
| --- | --- | --- |
| `DATA_DIR` | `./data` | Persistent data directory on the host |
| `DOWNLOADS_DIR` | `./downloads` | Download directory on the host |
| `TDL_NS` | `default` | tdl account namespace; keep it stable after login |
| `TDL_USER_ID` | Empty | Optional Telegram user ID override; normally detected and saved by `init` |
| `TDL_PROXY` | Empty | Proxy URL supported by tdl |
| `TDL_RECONNECT_TIMEOUT` | `10m` | How long one tdl process retries a disconnected Telegram connection; `0` means indefinitely |
| `TDL_NTP` | Empty | Optional NTP server host; normally unnecessary when the host clock is correct |
| `DOWNLOAD_RETRY_GAP_SECONDS` | `300` | Delay in seconds between wrapper retries after tdl exits |
| `TDL_THREADS` | `4` | Number of tdl workers |
| `TDL_LIMIT` | `2` | tdl concurrency limit |
| `TDL_DELAY_SECONDS` | `0` | Delay between requests |
| `TDL_TAKEOUT` | `false` | Whether to use a Telegram takeout session for downloads |
| `TZ` | `Asia/Shanghai` | Time zone used for daily scheduling and log timestamps |

`TDL_THREADS` and related options are saved to `config.json` during the first `init`. Later runs of `configure` preserve their existing values.

## Local Development

Install `tdl` locally or set `TDL_BIN` to its executable. The Python wrapper itself has no Telegram client dependency:

```bash
make install
cp .env.example .env
./.venv/bin/telegram-downloader init
./.venv/bin/telegram-downloader run
```

Run the test suite:

```bash
make test
```

## License

This project is licensed under the [GNU Affero General Public License v3.0](LICENSE). The wrapper invokes tdl through its command-line interface; tdl is also distributed under AGPL-3.0. When downloading Telegram content, comply with Telegram's terms, chat permissions, and applicable copyright laws.
