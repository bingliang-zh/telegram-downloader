from telegram_downloader import cli
from telegram_downloader.config import AppPaths


def test_parser_exposes_wrapper_commands():
    parser = cli.build_parser()

    init_args = parser.parse_args(["init", "--login-type", "code"])
    assert init_args.command == "init"
    assert init_args.login_type == "code"
    assert parser.parse_args(["login", "--type", "code"]).type == "code"
    assert parser.parse_args(["status", "--json"]).json is True
    assert parser.parse_args(["run", "--days", "2"]).days == 2


def test_config_answers_are_serializable(tmp_path):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")

    config = cli._config_from_answers(
        paths,
        existing=None,
        channel="@example",
        media_types="photo,video",
        daily=True,
        daily_time="03:30",
    )

    assert config.channel == "@example"
    assert config.media_types == ("photo", "video")
    assert config.daily_enabled is True
    assert config.daily_time == "03:30"
    assert config.download_dir == paths.download_dir


def test_init_logs_in_before_listing_and_saves_selected_channel(tmp_path, monkeypatch):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    args = cli.build_parser().parse_args(
        ["init", "--types", "photo", "--no-daily", "--login-type", "qr"]
    )
    calls = []

    class FakeTdl:
        namespace = "default"

        def namespace_data_exists(self):
            return False

        def run_interactive(self, command):
            calls.append(("login", tuple(command)))
            return 0

        def list_chats_json(self):
            calls.append(("channels",))
            return [
                {"id": 10, "type": "group", "visible_name": "Not a channel"},
                {"id": 20, "type": "channel", "visible_name": "Private channel"},
                {"id": 30, "type": "channel", "visible_name": "Public channel", "username": "public"},
            ]

    monkeypatch.setattr(cli, "_prompt", lambda label, default=None: "3")

    assert cli._init(paths, args, tdl=FakeTdl()) == 0
    assert calls == [("login", ("login", "--type", "qr")), ("channels",)]
    assert cli.load_config(paths).channel == "@public"


def test_init_uses_existing_session_without_login(tmp_path, monkeypatch):
    paths = AppPaths(tmp_path / "data", tmp_path / "downloads", tmp_path / "data" / "tdl")
    args = cli.build_parser().parse_args(["init", "--types", "photo", "--no-daily"])
    calls = []

    class FakeTdl:
        namespace = "default"

        def namespace_data_exists(self):
            return True

        def probe_session(self):
            calls.append("probe")
            return True, [{"id": 30, "type": "group", "visible_name": "Existing group"}]

        def clear_namespace_data(self):
            calls.append("clear")

        def run_interactive(self, command):
            calls.append("login")
            return 0

        def list_chats_json(self):
            calls.append("channels")
            return []

    answers = iter(["use", "1"])
    monkeypatch.setattr(cli, "_prompt", lambda label, default=None: next(answers))

    assert cli._init(paths, args, tdl=FakeTdl()) == 0
    assert calls == ["probe"]
    assert cli.load_config(paths).channel == "30"


def test_existing_session_can_be_deleted_before_relogin(monkeypatch):
    calls = []

    class FakeTdl:
        namespace = "default"

        def namespace_data_exists(self):
            return True

        def probe_session(self):
            return True, []

        def clear_namespace_data(self):
            calls.append("clear")

    monkeypatch.setattr(cli, "_prompt", lambda label, default=None: "delete")

    assert cli._prepare_session(FakeTdl()) == (False, [])
    assert calls == ["clear"]


def test_channel_selection_can_use_a_requested_reference():
    channels = [
        {"id": 20, "type": "channel", "visible_name": "Private channel"},
        {"id": 30, "type": "channel", "visible_name": "Public channel", "username": "public"},
    ]

    assert cli._select_channel(channels, requested="30") == "@public"


def test_channel_selection_supports_groups_and_archived_manual_reference(monkeypatch):
    answers = iter(["1", "https://t.me/c/123456/789"])
    monkeypatch.setattr(cli, "_prompt", lambda label, default=None: next(answers))

    assert cli._channel_options(
        [{"id": 20, "type": "group", "visible_name": "Family group"}]
    )[0]["type"] == "group"
    assert cli._select_channel([]) == "https://t.me/c/123456/789"
