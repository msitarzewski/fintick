"""Tests for the initial FinTick command-line foundation."""

from __future__ import annotations

import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fintick.aggregate import AggregateStats
from fintick.cli import main
from fintick.enrich import EnrichStats
from fintick.research import ResearchStats
from fintick.validate import ValidateStats


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
        self.assertIn("fetched=60 new=60 deduped=5 pages=1", output.getvalue())

    @mock.patch(
        "fintick.cli.aggregate_once",
        return_value=AggregateStats(selected=4, events=1, created=1, errored=0),
    )
    def test_aggregate_command_reports_event_counts(self, aggregate_once: mock.Mock) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["aggregate", "--database", "/tmp/events.db", "--limit", "4"])

        self.assertEqual(result, 0)
        aggregate_once.assert_called_once_with("/tmp/events.db", limit=4)
        self.assertIn("aggregate selected=4 events=1 new=1 errored=0", output.getvalue())

    @mock.patch(
        "fintick.cli.validate_pending",
        return_value=ValidateStats(selected=2, breaking=1, confirmed=1),
    )
    def test_validate_command_reports_status_counts(self, validate_pending: mock.Mock) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["validate", "--database", "/tmp/events.db", "--limit", "2"])

        self.assertEqual(result, 0)
        validate_pending.assert_called_once_with("/tmp/events.db", limit=2, min_age=900)
        self.assertIn(
            "validate selected=2 breaking=1 confirmed=1 contradicted=0 developing=0 errored=0",
            output.getvalue(),
        )

    def test_research_command_reports_no_eligible_items(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(output):
            database = Path(tmp) / "fintick.db"
            result = main(["research", "--database", str(database)])

        self.assertEqual(result, 0)
        self.assertIn("research selected=0 researched=0 errored=0", output.getvalue())

    @mock.patch("fintick.cli.run_periodically")
    @mock.patch("fintick.cli.enrich_pending", return_value=EnrichStats())
    def test_enrich_watch_bounds_each_cycle_to_one_item(
        self, enrich_pending: mock.Mock, run_periodically: mock.Mock
    ) -> None:
        run_periodically.side_effect = lambda _label, cycle, _interval: cycle()

        result = main(["enrich", "--watch", "--limit", "10"])

        self.assertEqual(result, 0)
        enrich_pending.assert_called_once_with(
            "data/fintick.db", limit=1, max_attempts=3
        )

    @mock.patch("fintick.cli.run_periodically")
    @mock.patch("fintick.cli.research_pending", return_value=ResearchStats())
    def test_research_watch_bounds_each_cycle_to_one_item(
        self, research_pending: mock.Mock, run_periodically: mock.Mock
    ) -> None:
        run_periodically.side_effect = lambda _label, cycle, _interval: cycle()

        result = main(["research", "--watch", "--limit", "5"])

        self.assertEqual(result, 0)
        research_pending.assert_called_once_with(
            "data/fintick.db", limit=1, threshold=3, max_attempts=3
        )


if __name__ == "__main__":
    unittest.main()
