"""Tests for the initial FinTick command-line foundation."""

from __future__ import annotations

import contextlib
import io
import unittest

from fintick.cli import main


class CliTests(unittest.TestCase):
    def test_doctor_reports_ready(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["doctor"])

        self.assertEqual(result, 0)
        self.assertIn("Python runtime ready", output.getvalue())


if __name__ == "__main__":
    unittest.main()
