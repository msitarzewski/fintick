"""Deployment integration tests for rootless FinTick services."""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SETUP = ROOT / "setup-fintick-services.sh"


class ServiceSetupTests(unittest.TestCase):
    def test_dry_run_generates_portable_user_systemd_units(self) -> None:
        environment = os.environ.copy()
        environment["FINTICK_PYTHON"] = "/usr/bin/python3"
        environment["FINTICK_HERMES"] = "/opt/hermes/bin/hermes"
        completed = subprocess.run(
            ["bash", str(SETUP), "--dry-run"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )

        output = completed.stdout
        self.assertIn("fintick-aggregate.service", output)
        self.assertIn("systemctl --user", output)
        self.assertIn("--provider hermes", output)
        self.assertIn("--model gpt-5.6-luna", output)
        self.assertIn("/opt/hermes/bin/hermes", output)
        self.assertEqual(
            output.count("EnvironmentFile=-%h/.config/fintick/environment"), 1
        )
        self.assertNotIn("supervisor", output.lower())
        self.assertNotIn("michael", SETUP.read_text(encoding="utf-8").lower())

    def test_preflight_rejects_an_existing_dashboard_process(self) -> None:
        process = subprocess.Popen([
            "bash", "-c",
            'exec -a "python3 -m fintick serve --port 8137" sleep 30',
        ])
        try:
            completed = subprocess.run(
                ["bash", str(SETUP), "--preflight"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                },
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            process.terminate()
            process.wait(timeout=5)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Existing FinTick workers", completed.stderr)


if __name__ == "__main__":
    unittest.main()
