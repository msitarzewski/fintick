"""Concurrent opens must wait for the lock, not raise."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from fintick.storage import BUSY_TIMEOUT_SECONDS, open_database


class BusyTimeoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "fintick.db"

    def test_busy_timeout_is_raised_above_the_python_default(self) -> None:
        with open_database(self.database) as connection:
            timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        # Python's default is 5000ms, which is not enough for several workers opening at once.
        self.assertGreaterEqual(timeout, 30000)
        self.assertEqual(BUSY_TIMEOUT_SECONDS, 30.0)

    def test_wal_is_still_enabled(self) -> None:
        with open_database(self.database) as connection:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal"
            )

    def test_the_wal_switch_is_skipped_when_already_enabled(self) -> None:
        """The actual cause of the startup failure.

        Switching journal_mode needs an exclusive lock, and SQLite answers SQLITE_BUSY for it
        IMMEDIATELY rather than honouring the busy handler — so no timeout can rescue it. The
        switch was being re-issued on every open despite journal_mode being persistent, which
        is what four simultaneous workers actually collided on.
        """
        with open_database(self.database):
            pass  # first open sets WAL

        # sqlite3.Connection is an immutable C type, so the statements are captured with the
        # connection's own trace callback, attached as it is created.
        statements: list[str] = []
        real_connect = sqlite3.connect

        def traced(*args, **kwargs):  # type: ignore[no-untyped-def]
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch("fintick.storage.sqlite3.connect", traced):
            with open_database(self.database):
                pass

        writes = [s for s in statements if "journal_mode=" in s.replace(" ", "")]
        self.assertEqual(writes, [], f"re-issued the WAL switch unnecessarily: {writes}")

    def test_a_concurrent_open_waits_instead_of_raising(self) -> None:
        """Behavioural check: a held write lock delays an open rather than failing it.

        Note this does not by itself prove the fix — Python's 5s connect() default would
        also pass a 0.4s hold. The guard for the raised timeout is the pragma test above;
        the guard for the real cause is the WAL test above that.
        """
        with open_database(self.database):
            pass  # create the schema first

        holder = sqlite3.connect(self.database)
        holder.execute("PRAGMA busy_timeout=30000")
        holder.execute("BEGIN IMMEDIATE")  # take the write lock
        self.addCleanup(holder.close)

        failure: list[BaseException] = []

        def opener() -> None:
            try:
                with open_database(self.database):
                    pass
            except BaseException as error:  # noqa: BLE001 - recorded for the assertion
                failure.append(error)

        thread = threading.Thread(target=opener)
        thread.start()
        # Hold the lock briefly, then release it from THIS thread — a sqlite3 connection
        # may only be used by the thread that created it.
        time.sleep(0.4)
        holder.rollback()
        thread.join(timeout=20)

        self.assertFalse(thread.is_alive(), "the concurrent open never completed")
        self.assertEqual(failure, [], f"concurrent open raised instead of waiting: {failure}")


if __name__ == "__main__":
    unittest.main()
