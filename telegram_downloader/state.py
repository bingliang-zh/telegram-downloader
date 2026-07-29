from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Small SQLite store for resumable jobs and observable progress."""

    _RUN_FIELDS = {
        "status", "stage", "started_at", "ended_at", "expected", "completed",
        "bytes_completed", "temp_files", "return_code", "last_error",
        "source_path", "manifest_path", "work_dir",
    }

    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    media_types TEXT NOT NULL,
                    expected INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    bytes_completed INTEGER NOT NULL DEFAULT 0,
                    temp_files INTEGER NOT NULL DEFAULT 0,
                    return_code INTEGER,
                    source_path TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    work_dir TEXT NOT NULL,
                    last_error TEXT
                )
                """
            )

    def create_run(
        self,
        *,
        channel: str,
        media_types: str,
        source_path: Path,
        manifest_path: Path,
        work_dir: Path,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs (
                    created_at, status, stage, channel, media_types,
                    source_path, manifest_path, work_dir
                ) VALUES (?, 'prepared', 'created', ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(), channel, media_types, str(source_path),
                    str(manifest_path), str(work_dir),
                ),
            )
            return int(cursor.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._RUN_FIELDS
        if unknown:
            raise ValueError(f"Unknown run fields: {', '.join(sorted(unknown))}")
        if not fields:
            return
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = list(fields.values()) + [run_id]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                values,
            )

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def active_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status IN ('prepared', 'running', 'interrupted')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def latest_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_running_as_interrupted(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = 'interrupted', stage = 'interrupted', ended_at = ?
                WHERE status = 'running'
                """,
                (utc_now(),),
            )

    def abandon_active(self) -> int | None:
        active = self.active_run()
        if active is None:
            return None
        self.update_run(
            int(active["id"]),
            status="abandoned",
            stage="abandoned",
            ended_at=utc_now(),
        )
        return int(active["id"])
