# telegram-downloader

[![CI](https://github.com/bingliang-zh/telegram-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/bingliang-zh/telegram-downloader/actions/workflows/ci.yml)

一个围绕 [tdl](https://github.com/iyear/tdl) 的 Docker wrapper。它使用 Telegram 用户登录，不依赖 Telethon 或 Telegram Bot API，适合把一个 channel 的媒体持续同步到宿主机目录。

wrapper 负责配置、任务状态、进度显示、每日调度和“已存在文件不覆盖”；Telegram 登录、channel 访问和媒体下载由 `tdl` 完成。

## 快速开始（Docker）

先准备宿主机目录和环境文件：

```bash
cp .env.example .env
mkdir -p data downloads
docker compose build
```

首次使用只需要运行一个交互式初始化向导：

```bash
docker compose run --rm -it telegram-downloader init
```

向导会按以下顺序执行：

1. 检测当前 tdl namespace 是否已有可用 session；如果有，选择直接使用、删除后重新登录或取消；
2. 没有可用 session 时，通过 QR（默认）登录 Telegram 用户账号；
3. 登录成功后自动获取账号可访问的 channel 和 group 列表；
4. 选择 channel/group；如果目标在 Archived Chats 中，可以选择列表底部的手动输入项，填写 username、消息链接或 numeric ID；
5. 选择媒体类型、是否开启每日下载以及运行时间；
6. 将全部设置保存到 `data/config.json`。

删除操作只删除当前 `TDL_NS` 对应的 tdl session，不会删除其他 namespace、下载文件或任务状态。

如果之前已经初始化过，需要重新走一遍向导时使用 `--force`。也可以通过参数减少交互：

```bash
docker compose run --rm telegram-downloader init \
  --channel '@example_channel' \
  --types photo,video \
  --daily \
  --daily-time 03:30
```

启动服务：

```bash
docker compose up -d
make logs
```

`make logs` 只在查看日志时过滤 tdl 的 `CPU / Memory / Goroutines` 资源监控行，Telegram 消息下载进度、错误和 wrapper 重试日志都会保留。需要查看未经筛选的原始日志时使用 `make logs-raw`。

服务会立即执行一次下载。如果配置开启每日下载，它会保持运行，并在配置的本地时间每天执行一次；否则成功完成一次下载后退出。tdl 单次下载成功后会正常退出，下一次定时任务启动时 wrapper 会重新启动一个 tdl 进程。

如果 tdl 因网络错误退出，wrapper 会保留当前任务状态和临时文件，等待 `DOWNLOAD_RETRY_GAP_SECONDS` 后使用 `--continue` 重试，不会覆盖已经完成的文件。这个重试会持续到本次下载成功；本地文件错误、无效 manifest 等异常会直接报告，不会被无限吞掉。

配置修改后，重启容器即可生效：

```bash
docker compose run --rm -it telegram-downloader configure
docker compose restart
```

## 运行与状态命令

```bash
docker compose run --rm telegram-downloader run
docker compose run --rm telegram-downloader status
docker compose run --rm telegram-downloader status --json
docker compose run --rm telegram-downloader tdl-version
docker compose run --rm telegram-downloader reset
```

`run` 会继续未完成的任务。`reset` 只放弃当前任务记录，不删除已经下载的文件；之后可以重新 `run`。

`login` 和 `channels` 仍然保留为排障用的底层命令，但首次配置不需要单独执行它们。

说明：Telegram 的 Archived Chats 使用独立的 folder，当前 tdl 的 `chat ls` 不会提供一个单独的 archived 列表。因此归档的私有群组需要使用消息链接或 numeric ID 手动输入；也可以暂时取消归档，完成初始化后再放回 Archived Chats。

可选媒体类型：`all`、`photo`、`video`、`audio`、`voice`、`document`、`sticker`。配置为 `all` 时导出 channel 中所有可下载媒体。

## 持久化与不覆盖策略

Compose 使用 folder mapper：

| 宿主机 | 容器 | 内容 |
| --- | --- | --- |
| `./data` | `/data` | `config.json`、`state.db`、tdl 登录存储、任务 manifest |
| `./downloads` | `/downloads` | 按账号和 group 分类的下载文件 |

下载文件直接写入 `/downloads/<telegram-user-id>/<telegram-group-id>/`，例如 `/downloads/123456789/-100123456789/`。账号 ID 由 tdl 的 Saved Messages peer 解析，group ID 由 tdl export 的顶层 `id` 解析；如果旧配置无法解析账号 ID，会回退到 `TDL_NS` 作为账号目录。最终文件名以 Telegram message ID 开头，例如 `12345_video.mp4`。`.tmp` 文件会留在同一个 group 目录中用于断点恢复；已存在的完整文件会按 message ID 跳过，不会覆盖。

SQLite 中会保存任务阶段、预计文件数、已完成文件数、字节数、临时文件数、manifest 和工作目录，容器重启后可以继续任务。

不要删除或分享 `data`：其中包含 tdl 用户登录凭据。若需要迁移，只需完整复制 `data` 和 `downloads` 两个目录。

## 配置

交互式 `init` 保存的配置在 `data/config.json`。Docker 环境变量只负责容器路径和 tdl 运行参数：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `./data` | 宿主机持久化数据目录 |
| `DOWNLOADS_DIR` | `./downloads` | 宿主机下载目录 |
| `TDL_NS` | `default` | tdl 账户 namespace；登录后不要随意修改 |
| `TDL_USER_ID` | 空 | 可选的 Telegram 用户 ID；通常由 `init` 自动保存，不需要手动设置 |
| `TDL_PROXY` | 空 | tdl 支持的代理地址 |
| `TDL_RECONNECT_TIMEOUT` | `10m` | 单次 tdl 进程内部的 Telegram 断线重连等待时间；`0` 表示无限等待 |
| `TDL_NTP` | 空 | 可选的 NTP server host；宿主机时间正常时通常不需要设置 |
| `DOWNLOAD_RETRY_GAP_SECONDS` | `300` | tdl 进程退出后，wrapper 两次重试之间的等待秒数 |
| `TDL_THREADS` | `4` | tdl 并行 worker 数 |
| `TDL_LIMIT` | `2` | tdl 并发限制 |
| `TDL_DELAY_SECONDS` | `0` | 请求间隔 |
| `TDL_TAKEOUT` | `false` | 是否用 Telegram takeout 会话下载 |
| `TZ` | `Asia/Shanghai` | 每日调度和日志使用的时区 |

`TDL_THREADS` 等参数在第一次 `init` 时写入 `config.json`；之后通过 `configure` 会保留原值。

## 本地开发

本地需要自行安装 `tdl`，或设置 `TDL_BIN` 指向它。Python wrapper 本身没有 Telegram 客户端依赖：

```bash
make install
cp .env.example .env
./.venv/bin/telegram-downloader init
./.venv/bin/telegram-downloader run
```

运行测试：

```bash
make test
```

## 许可说明

本项目采用 [GNU Affero General Public License v3.0](LICENSE)。wrapper 通过命令行调用 tdl；tdl 本身同样遵循其仓库声明的 AGPL-3.0 许可。使用 Telegram 下载内容时，请遵守 Telegram 条款、频道权限和内容版权要求。
