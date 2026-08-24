"""Tests for resilient long-running worker lifecycle support."""

from __future__ import annotations

import contextlib
import io
import threading
import unittest

from fintick.runtime import run_periodically


class RuntimeTests(unittest.TestCase):
    def test_worker_recovers_from_cycle_failure_and_stops_cleanly(self) -> None:
        stop = threading.Event()
        attempts = 0

        def cycle() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary failure")
            stop.set()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run_periodically(
                "test", cycle, 0.001, stop=stop, install_signals=False
            )

        self.assertEqual(attempts, 2)
        self.assertIn("cycle failed: RuntimeError: temporary failure", output.getvalue())
        self.assertIn("worker stopped", output.getvalue())

    def test_worker_rejects_nonpositive_interval(self) -> None:
        with self.assertRaisesRegex(ValueError, "interval must be positive"):
            run_periodically("test", lambda: None, 0, install_signals=False)


if __name__ == "__main__":
    unittest.main()
