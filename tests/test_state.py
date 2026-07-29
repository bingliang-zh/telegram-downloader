from telegram_downloader.state import StateStore


def test_state_store_tracks_active_and_interrupted_runs(tmp_path):
    store = StateStore(tmp_path / "state.db")
    run_id = store.create_run(
        channel="@example",
        media_types="photo",
        source_path=tmp_path / "export.json",
        manifest_path=tmp_path / "manifest.json",
        work_dir=tmp_path / "work",
    )

    assert store.active_run()["id"] == run_id
    store.update_run(run_id, status="running", stage="downloading", completed=2)
    store.mark_running_as_interrupted()

    run = store.get_run(run_id)
    assert run["status"] == "interrupted"
    assert run["stage"] == "interrupted"
    assert run["completed"] == 2

    assert store.abandon_active() == run_id
    assert store.active_run() is None
