"""Focused tests for reversible service-handoff state operations."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fintick.service_handoff import (
    is_fintick_worker_argv,
    restore_database,
    snapshot_database,
)


class DatabaseSnapshotTests(unittest.TestCase):
    def test_snapshot_includes_committed_wal_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            snapshot = Path(tmp) / "snapshot.db"
            keeper = sqlite3.connect(source)
            try:
                keeper.execute("PRAGMA journal_mode=WAL")
                keeper.execute("PRAGMA wal_autocheckpoint=0")
                keeper.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                keeper.execute("INSERT INTO marker VALUES ('committed-in-wal')")
                keeper.commit()

                snapshot_database(source, snapshot)

                with sqlite3.connect(snapshot) as connection:
                    rows = connection.execute("SELECT value FROM marker").fetchall()
            finally:
                keeper.close()

        self.assertEqual(rows, [("committed-in-wal",)])

    def test_restore_replaces_database_and_removes_stale_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            snapshot = Path(tmp) / "snapshot.db"
            target = Path(tmp) / "target.db"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker VALUES ('rollback-state')")
            snapshot_database(source, snapshot)
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE unwanted (value TEXT NOT NULL)")
            Path(f"{target}-wal").write_bytes(b"stale wal")
            Path(f"{target}-shm").write_bytes(b"stale shm")

            restore_database(snapshot, target)

            with sqlite3.connect(target) as connection:
                rows = connection.execute("SELECT value FROM marker").fetchall()
                unwanted = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name='unwanted'"
                ).fetchone()[0]

        self.assertEqual(rows, [("rollback-state",)])
        self.assertEqual(unwanted, 0)
        self.assertFalse(Path(f"{target}-wal").exists())
        self.assertFalse(Path(f"{target}-shm").exists())

    def test_failed_final_replace_preserves_original_database_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            snapshot = Path(tmp) / "snapshot.db"
            target = Path(tmp) / "target.db"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker VALUES ('rollback-state')")
            snapshot_database(source, snapshot)
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE original (value TEXT NOT NULL)")
                connection.execute("INSERT INTO original VALUES ('live-state')")
            wal = Path(f"{target}-wal")
            shm = Path(f"{target}-shm")
            wal.write_bytes(b"original wal")
            shm.write_bytes(b"original shm")
            original = {
                target: target.read_bytes(),
                wal: wal.read_bytes(),
                shm: shm.read_bytes(),
            }
            real_replace = __import__("os").replace

            def fail_final_replace(source_path: str | Path, destination_path: str | Path) -> None:
                if Path(destination_path) == target and ".restore-" in Path(source_path).name:
                    raise OSError("forced final replace failure")
                real_replace(source_path, destination_path)

            with mock.patch(
                "fintick.service_handoff.os.replace",
                side_effect=fail_final_replace,
            ):
                with self.assertRaisesRegex(OSError, "forced final replace failure"):
                    restore_database(snapshot, target)

            restored = {
                path: path.read_bytes() if path.exists() else None
                for path in original
            }

        self.assertEqual(restored, original)


class WorkerProcessTests(unittest.TestCase):
    def test_detects_python_options_and_console_entrypoint_workers(self) -> None:
        workers = (
            ["python3", "-u", "-m", "fintick", "aggregate", "--watch"],
            ["/usr/bin/python3", "-X", "dev", "-m", "fintick", "ingest", "--watch"],
            ["/usr/local/bin/fintick", "validate", "--watch"],
            ["fintick", "serve", "--host", "127.0.0.1"],
        )
        for argv in workers:
            with self.subTest(argv=argv):
                self.assertTrue(is_fintick_worker_argv(argv))

    def test_rejects_non_workers_and_one_shot_commands(self) -> None:
        non_workers = (
            ["python3", "-m", "fintick", "aggregate"],
            ["fintick", "validate"],
            ["python3", "-m", "other", "aggregate", "--watch"],
            ["bash", "-c", "python3 -m fintick aggregate --watch"],
        )
        for argv in non_workers:
            with self.subTest(argv=argv):
                self.assertFalse(is_fintick_worker_argv(argv))


if __name__ == "__main__":
    unittest.main()
