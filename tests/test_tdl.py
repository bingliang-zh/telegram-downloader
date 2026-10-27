from telegram_downloader.config import AppPaths, DownloadConfig
from telegram_downloader.tdl import TdlRunner


def test_tdl_commands_use_persistent_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("TDL_RECONNECT_TIMEOUT", "10m")
    monkeypatch.setenv("TDL_NTP", "pool.ntp.org")
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    runner = TdlRunner(paths, executable="tdl-test")
    config = DownloadConfig(channel="@example", media_types="photo")

    assert runner.command("version")[:5] == [
        "tdl-test", "--ns", "default", "--storage",
        f"type=bolt,path={paths.tdl_storage_dir}",
    ]
    command = runner.command("version")
    assert "--disable-progress-ps" not in command
    assert command[command.index("--reconnect-timeout") + 1] == "10m"
    assert command[command.index("--ntp") + 1] == "pool.ntp.org"
    captured = {}

    def fake_run_interactive(args, *, config=None):
        captured["args"] = list(args)
        captured["config"] = config
        return 0

    runner.run_interactive = fake_run_interactive
    runner.export(config, tmp_path / "export.json")
    export = captured["args"]
    assert "--filter" in export
    assert 'Media.Name endsWith ".jpg"' in export[export.index("--filter") + 1]
    assert "--with-content" in export
    assert "--raw" in export
    assert "--all" in export
    assert captured["config"] is config


def test_list_chats_json_parses_tdl_dialog_array(tmp_path, monkeypatch):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    runner = TdlRunner(paths, executable="tdl-test")

    class Completed:
        returncode = 0
        stdout = '[{"id": 42, "type": "channel", "visible_name": "Example"}]'
        stderr = ""

    monkeypatch.setattr("telegram_downloader.tdl.subprocess.run", lambda *args, **kwargs: Completed())

    assert runner.list_chats_json() == [
        {"id": 42, "type": "channel", "visible_name": "Example"}
    ]


def test_topics_for_chat_reads_topic_titles(tmp_path, monkeypatch):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    runner = TdlRunner(paths, executable="tdl-test")
    monkeypatch.setattr(
        runner,
        "list_chats_json",
        lambda: [{"id": 42, "topics": [{"id": 7, "title": "Travel"}]}],
    )

    assert runner.topics_for_chat(42) == {7: "Travel"}


def test_export_days_uses_a_time_range(tmp_path, monkeypatch):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    runner = TdlRunner(paths, executable="tdl-test")
    config = DownloadConfig(channel="@example")
    captured = {}

    monkeypatch.setattr("telegram_downloader.tdl.time.time", lambda: 100000)

    def fake_run_interactive(args, *, config=None):
        captured["args"] = list(args)
        return 0

    runner.run_interactive = fake_run_interactive
    runner.export(config, tmp_path / "export.json", days=2)

    export = captured["args"]
    assert export[export.index("--type") + 1] == "time"
    assert export[export.index("--input") + 1] == "-72800,100000"


def test_namespace_data_can_be_checked_and_cleared(tmp_path):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    paths.tdl_storage_dir.mkdir(parents=True)
    runner = TdlRunner(paths, executable="tdl-test")
    runner.namespace_data_path.write_bytes(b"session")

    assert runner.namespace_data_exists() is True
    runner.clear_namespace_data()
    assert runner.namespace_data_exists() is False


def test_current_user_id_comes_from_tdl_saved_messages_export(tmp_path):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    runner = TdlRunner(paths, executable="tdl-test")

    def fake_run_interactive(args, *, config=None):
        output = args[args.index("--output") + 1]
        with open(output, "w", encoding="utf-8") as file:
            file.write('{"id": 123456789, "messages": []}')
        return 0

    runner.run_interactive = fake_run_interactive

    assert runner.current_user_id() == "123456789"
