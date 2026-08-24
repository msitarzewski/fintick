"""Tests for the initial FinTick command-line foundation."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fintick.cli import main


class CliTests(unittest.TestCase):
    def test_doctor_reports_ready(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["doctor"])

        self.assertEqual(result, 0)
        self.assertIn("Python runtime ready", output.getvalue())

    def test_offline_ingest_command_reports_counts(self) -> None:
        fixture = Path(__file__).parents[1] / "reference" / "feed_sample.json"
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(output):
            database = Path(tmp) / "fintick.db"
            result = main([
                "ingest", "--fixture", str(fixture), "--database", str(database)
            ])
            with sqlite3.connect(database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM posts").fetchone()[0]

        self.assertEqual(result, 0)
        self.assertEqual(count, 60)
        self.assertIn("fetched=60 new=60 pages=1", output.getvalue())


if __name__ == "__main__":
    unittest.main()
