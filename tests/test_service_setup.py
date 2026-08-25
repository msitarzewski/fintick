"""Deployment integration tests for rootless FinTick services."""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from fintick.storage import insert_post, open_database


ROOT = Path(__file__).parents[1]
SETUP = ROOT / "setup-fintick-services.sh"


class ServiceSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._supervisor_directory = tempfile.TemporaryDirectory()
        cls._proc_directory = tempfile.TemporaryDirectory()
        cls._runtime_directory = tempfile.TemporaryDirectory()
        cls._previous_supervisor_directory = os.environ.get(
            "FINTICK_SUPERVISOR_CONFIG_DIR"
        )
        cls._previous_proc_directory = os.environ.get("FINTICK_PROC_ROOT")
        cls._previous_runtime_directory = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["FINTICK_SUPERVISOR_CONFIG_DIR"] = cls._supervisor_directory.name
        os.environ["FINTICK_PROC_ROOT"] = cls._proc_directory.name
        os.environ["XDG_RUNTIME_DIR"] = cls._runtime_directory.name

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._previous_supervisor_directory is None:
            os.environ.pop("FINTICK_SUPERVISOR_CONFIG_DIR", None)
        else:
            os.environ["FINTICK_SUPERVISOR_CONFIG_DIR"] = (
                cls._previous_supervisor_directory
            )
        if cls._previous_proc_directory is None:
            os.environ.pop("FINTICK_PROC_ROOT", None)
        else:
            os.environ["FINTICK_PROC_ROOT"] = cls._previous_proc_directory
        if cls._previous_runtime_directory is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = cls._previous_runtime_directory
        cls._supervisor_directory.cleanup()
        cls._proc_directory.cleanup()
        cls._runtime_directory.cleanup()

    def test_dry_run_generates_portable_user_systemd_units(self) -> None:
        environment = os.environ.copy()
        environment["FINTICK_PYTHON"] = "/usr/bin/python3"
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
        self.assertIn("-m fintick aggregate", output)
        # Untethered from Hermes: no OAuth subprocess route in the generated units.
        self.assertNotIn("--provider", output)
        self.assertNotIn("hermes", output.lower())
        # Both aggregate and validate read the config/secrets env file.
        self.assertEqual(
            output.count("EnvironmentFile=-%h/.config/fintick/environment"), 2
        )
        self.assertNotIn("supervisor", output.lower())
        self.assertNotIn("michael", SETUP.read_text(encoding="utf-8").lower())

    def test_preflight_rejects_python_options_and_console_workers(self) -> None:
        workers = (
            ["python3", "-u", "-m", "fintick", "aggregate", "--watch"],
            ["/usr/local/bin/fintick", "serve", "--port", "8137"],
        )
        for index, argv in enumerate(workers, start=1):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fake_bin = root / "bin"
                fake_bin.mkdir()
                pgrep = fake_bin / "pgrep"
                pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
                pgrep.chmod(0o755)
                proc = root / "proc"
                process = proc / str(index)
                process.mkdir(parents=True)
                (process / "cmdline").write_bytes(
                    b"\0".join(value.encode() for value in argv) + b"\0"
                )
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                completed = subprocess.run(
                    ["bash", str(SETUP), "--preflight"],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "FINTICK_PROC_ROOT": str(proc),
                        "FINTICK_PYTHON": "/usr/bin/python3",
                        "FINTICK_HERMES": "/bin/true",
                        "FINTICK_DASHBOARD_PORT": str(port),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("Existing FinTick workers", completed.stderr)

    def test_preflight_rejects_non_fintick_listener_on_dashboard_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                completed = subprocess.run(
                    ["bash", str(SETUP), "--preflight"],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "FINTICK_PYTHON": "/usr/bin/python3",
                        "FINTICK_HERMES": "/bin/true",
                        "FINTICK_DASHBOARD_PORT": str(port),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("dashboard port", completed.stderr.lower())

    def test_preflight_rejects_dormant_supervisor_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            supervisor = root / "supervisor"
            supervisor.mkdir()
            (supervisor / "fintick-aggregate.conf").write_text(
                "[program:fintick-aggregate]\nautostart=true\nautorestart=true\n",
                encoding="utf-8",
            )
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP), "--preflight"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "FINTICK_SUPERVISOR_CONFIG_DIR": str(supervisor),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                },
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("Supervisor configuration", completed.stderr)
        self.assertIn("fintick-aggregate.conf", completed.stderr)

    def test_preflight_rejects_permissive_validation_environment_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            environment_file = root / ".config" / "fintick" / "environment"
            environment_file.parent.mkdir(parents=True)
            environment_file.write_text(
                "FINTICK_COMMON_VISION_TOKEN=do-not-print\n",
                encoding="utf-8",
            )
            environment_file.chmod(0o644)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP), "--preflight"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                },
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("mode 0600", completed.stderr)
        self.assertNotIn("do-not-print", completed.stdout + completed.stderr)

    def test_failed_activation_restores_units_and_keeps_rollback_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "pgrep").write_text(
                "#!/usr/bin/env bash\nexit 1\n", encoding="utf-8"
            )
            (fake_bin / "systemctl").write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *'--property=ActiveState'* ]]; then printf 'inactive\\n'; exit 0; fi\n"
                "if [[ $* == *is-enabled* && $* == *fintick-dashboard.service* ]]; then printf 'disabled\\n'; exit 1; fi\n"
                "if [[ $* == *is-enabled* ]]; then printf 'not-found\\n'; exit 4; fi\n"
                "if [[ $* == *is-active* ]]; then exit 1; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            (fake_bin / "journalctl").write_text(
                "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
            )
            for executable in fake_bin.iterdir():
                executable.chmod(0o755)

            config_home = root / "config"
            unit_dir = config_home / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            existing_unit = unit_dir / "fintick-dashboard.service"
            existing_unit.write_text("original dashboard unit\n", encoding="utf-8")
            data_dir = root / "data"
            data_dir.mkdir()
            database = data_dir / "fintick.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker VALUES ('rollback database fixture')")

            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            backups = list((root / "state" / "fintick").glob("handoff-*"))
            self.assertNotEqual(completed.returncode, 0)
            self.assertNotIn("Installed and started", completed.stdout)
            self.assertEqual(
                existing_unit.read_text(encoding="utf-8"),
                "original dashboard unit\n",
            )
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0] / "fintick.db") as connection:
                backup_marker = connection.execute("SELECT value FROM marker").fetchone()[0]
            with sqlite3.connect(database) as connection:
                restored_marker = connection.execute("SELECT value FROM marker").fetchone()[0]
            self.assertEqual(backup_marker, "rollback database fixture")
            self.assertEqual(restored_marker, "rollback database fixture")
            self.assertEqual(
                len(list((backups[0] / "journals").glob("*.log"))),
                4,
            )
            self.assertEqual(
                (backups[0] / "units" / "fintick-dashboard.service").read_text(
                    encoding="utf-8"
                ),
                "original dashboard unit\n",
            )

    def _assert_replacement_state_blocks_database_restore(self, reported_state: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            journalctl.chmod(0o755)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *'enable --now'* ]]; then\n"
                "  \"$FINTICK_PYTHON\" - \"$FINTICK_DATA_DIR/fintick.db\" <<'PY'\n"
                "import sqlite3, sys\n"
                "with sqlite3.connect(sys.argv[1]) as connection:\n"
                "    connection.execute(\"UPDATE marker SET value = 'live-write'\")\n"
                "PY\n"
                "  exit 1\n"
                "fi\n"
                "if [[ $* == *'disable --now'* ]]; then exit 1; fi\n"
                "if [[ $* == *'--property=ActiveState'* ]]; then "
                "printf '%s\\n' \"$FAKE_REPLACEMENT_STATE\"; exit 0; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            data_dir = root / "data"
            data_dir.mkdir()
            database = data_dir / "fintick.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                connection.execute("INSERT INTO marker VALUES ('before-handoff')")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                    "FAKE_REPLACEMENT_STATE": reported_state,
                },
                capture_output=True,
                text=True,
                check=False,
            )
            with sqlite3.connect(database) as connection:
                marker = connection.execute("SELECT value FROM marker").fetchone()[0]
            backups = list((root / "state" / "fintick").glob("handoff-*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(marker, "live-write")
        self.assertEqual(len(backups), 1)
        self.assertIn("Rollback incomplete", completed.stderr)
        self.assertNotIn("Rollback restored", completed.stderr)

    def test_rollback_refuses_database_restore_while_new_unit_remains_active(self) -> None:
        self._assert_replacement_state_blocks_database_restore("active")

    def test_rollback_refuses_database_restore_when_new_unit_state_is_unknown(self) -> None:
        self._assert_replacement_state_blocks_database_restore("unknown")

    def test_database_restore_failure_keeps_prior_writer_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            systemctl_calls = root / "systemctl.log"
            for name, body in (
                ("pgrep", "#!/usr/bin/env bash\nexit 1\n"),
                ("journalctl", "#!/usr/bin/env bash\nexit 0\n"),
                (
                    "systemctl",
                    "#!/usr/bin/env bash\n"
                    f"printf '%s\\n' \"$*\" >>{systemctl_calls}\n"
                    "if [[ $* == *'is-enabled fintick-dashboard.service'* ]]; then printf 'enabled\\n'; exit 0; fi\n"
                    "if [[ $* == *is-enabled* ]]; then printf 'not-found\\n'; exit 1; fi\n"
                    "if [[ $* == *'enable --now'* ]]; then exit 1; fi\n"
                    "if [[ $* == *'--property=ActiveState'* ]]; then\n"
                    f"  count=$(grep -c -- '--property=ActiveState' {systemctl_calls})\n"
                    "  if (( count == 1 )); then printf 'active\\n'; else printf 'inactive\\n'; fi\n"
                    "  exit 0\n"
                    "fi\n"
                    "exit 0\n",
                ),
            ):
                executable = fake_bin / name
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)
            python = fake_bin / "python"
            python.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == */service_handoff.py && $2 == restore ]]; then exit 1; fi\n"
                "exec /usr/bin/python3 \"$@\"\n",
                encoding="utf-8",
            )
            python.chmod(0o755)

            unit_dir = root / "config" / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            (unit_dir / "fintick-dashboard.service").write_text(
                "original dashboard unit\n", encoding="utf-8"
            )

            data_dir = root / "data"
            data_dir.mkdir()
            database = data_dir / "fintick.db"
            with open_database(database):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": str(python),
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            backups = list((root / "state" / "fintick").glob("handoff-*"))
            calls = systemctl_calls.read_text(encoding="utf-8")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(len(backups), 1)
        self.assertIn("database restoration failed", completed.stderr)
        self.assertIn("Rollback incomplete", completed.stderr)
        self.assertNotIn("Rollback restored", completed.stderr)
        self.assertNotIn("--user start fintick-dashboard.service", calls)

    def _assert_restore_prerequisite_failure_keeps_prior_writer_stopped(
        self, failure: str
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            systemctl_calls = root / "systemctl.log"
            reload_count = root / "reload-count"
            active_count = root / "active-count"
            cp_count = root / "cp-count"

            journalctl = fake_bin / "journalctl"
            journalctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            journalctl.chmod(0o755)
            cp = fake_bin / "cp"
            cp.write_text(
                "#!/usr/bin/env bash\n"
                f"count=0; [[ -f {cp_count} ]] && read -r count <{cp_count}\n"
                f"count=$((count + 1)); printf '%s' \"$count\" >{cp_count}\n"
                + ("if (( count > 1 )); then exit 1; fi\n" if failure == "unit" else "")
                + "exec /usr/bin/cp \"$@\"\n",
                encoding="utf-8",
            )
            cp.chmod(0o755)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$*\" >>{systemctl_calls}\n"
                "if [[ $* == *'is-enabled fintick-dashboard.service'* ]]; then printf 'enabled\\n'; exit 0; fi\n"
                "if [[ $* == *is-enabled* ]]; then printf 'not-found\\n'; exit 1; fi\n"
                "if [[ $* == *daemon-reload* ]]; then\n"
                f"  count=0; [[ -f {reload_count} ]] && read -r count <{reload_count}\n"
                f"  count=$((count + 1)); printf '%s' \"$count\" >{reload_count}\n"
                + ("  if (( count > 1 )); then exit 1; fi\n" if failure == "reload" else "")
                + "  exit 0\n"
                "fi\n"
                "if [[ $* == *'enable --now'* ]]; then exit 1; fi\n"
                "if [[ $* == *'--property=ActiveState'* ]]; then\n"
                f"  count=0; [[ -f {active_count} ]] && read -r count <{active_count}\n"
                f"  count=$((count + 1)); printf '%s' \"$count\" >{active_count}\n"
                "  if (( count == 1 )); then printf 'active\\n'; else printf 'inactive\\n'; fi\n"
                "  exit 0\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            unit_dir = root / "config" / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            (unit_dir / "fintick-dashboard.service").write_text(
                "original dashboard unit\n", encoding="utf-8"
            )
            data_dir = root / "data"
            data_dir.mkdir()
            with open_database(data_dir / "fintick.db"):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            calls = systemctl_calls.read_text(encoding="utf-8")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Rollback incomplete", completed.stderr)
        self.assertNotIn("Rollback restored", completed.stderr)
        self.assertNotIn("--user start fintick-dashboard.service", calls)

    def test_unit_restore_failure_keeps_prior_writer_stopped(self) -> None:
        self._assert_restore_prerequisite_failure_keeps_prior_writer_stopped("unit")

    def test_daemon_reload_failure_keeps_prior_writer_stopped(self) -> None:
        self._assert_restore_prerequisite_failure_keeps_prior_writer_stopped("reload")

    def test_failed_activation_snapshot_includes_committed_wal_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name, body in (
                ("pgrep", "#!/usr/bin/env bash\nexit 1\n"),
                (
                    "systemctl",
                    "#!/usr/bin/env bash\n"
                    "if [[ $* == *is-active* ]]; then exit 1; fi\n"
                    "exit 0\n",
                ),
                ("journalctl", "#!/usr/bin/env bash\nexit 0\n"),
            ):
                executable = fake_bin / name
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)

            data_dir = root / "data"
            data_dir.mkdir()
            database = data_dir / "fintick.db"
            keeper = sqlite3.connect(database)
            try:
                keeper.execute("PRAGMA journal_mode=WAL")
                keeper.execute("PRAGMA wal_autocheckpoint=0")
                keeper.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                keeper.execute("INSERT INTO marker VALUES ('committed-in-wal')")
                keeper.commit()
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                completed = subprocess.run(
                    ["bash", str(SETUP)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "HOME": str(root),
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_STATE_HOME": str(root / "state"),
                        "FINTICK_DATA_DIR": str(data_dir),
                        "FINTICK_PYTHON": "/usr/bin/python3",
                        "FINTICK_HERMES": "/bin/true",
                        "FINTICK_DASHBOARD_PORT": str(port),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                backup = next((root / "state" / "fintick").glob("handoff-*"))
                with sqlite3.connect(backup / "fintick.db") as connection:
                    marker = connection.execute("SELECT value FROM marker").fetchone()[0]
            finally:
                keeper.close()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(marker, "committed-in-wal")

    def test_rollback_restores_prior_unit_enabled_and_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            journalctl.chmod(0o755)
            systemctl_log = root / "systemctl.log"
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >>\"$FAKE_SYSTEMCTL_LOG\"\n"
                "if [[ $* == *'disable --now'* ]]; then touch \"$FAKE_DISABLED\"; exit 0; fi\n"
                "if [[ $* == *'start fintick-dashboard.service'* ]]; then touch \"$FAKE_STARTED\"; exit 0; fi\n"
                "if [[ $* == *'--property=ActiveState'* ]]; then\n"
                "  if [[ $* == *fintick-dashboard.service* && ( ! -f $FAKE_DISABLED || -f $FAKE_STARTED ) ]]; then printf 'active\\n'; else printf 'inactive\\n'; fi\n"
                "  exit 0\n"
                "fi\n"
                "if [[ $* == *is-enabled* && $* == *fintick-dashboard.service* ]]; then printf 'enabled\\n'; exit 0; fi\n"
                "if [[ $* == *is-enabled* ]]; then printf 'not-found\\n'; exit 4; fi\n"
                "if [[ $* == *is-active* && $* == *fintick-dashboard.service* ]]; then exit 0; fi\n"
                "if [[ $* == *is-active* ]]; then exit 1; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            config_home = root / "config"
            unit_dir = config_home / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            (unit_dir / "fintick-dashboard.service").write_text(
                "original dashboard unit\n", encoding="utf-8"
            )
            data_dir = root / "data"
            data_dir.mkdir()
            with open_database(data_dir / "fintick.db"):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                    "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
                    "FAKE_DISABLED": str(root / "disabled"),
                    "FAKE_STARTED": str(root / "started"),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            commands = systemctl_log.read_text(encoding="utf-8").splitlines()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--user enable fintick-dashboard.service", commands)
        self.assertIn("--user start fintick-dashboard.service", commands)

    def _assert_masked_unit_restored(self, *, runtime: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            journalctl = fake_bin / "journalctl"
            journalctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            journalctl.chmod(0o755)

            unit_dir = root / "config" / "systemd" / "user"
            runtime_unit_dir = root / "runtime" / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            runtime_unit_dir.mkdir(parents=True)
            unit_path = unit_dir / "fintick-dashboard.service"
            runtime_unit_path = runtime_unit_dir / "fintick-dashboard.service"
            if runtime:
                unit_path.write_text("original dashboard unit\n", encoding="utf-8")
                mask_path = runtime_unit_path
            else:
                mask_path = unit_path
            mask_path.symlink_to("/dev/null")
            activation_marker = root / "mask-at-activation"

            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *is-enabled* ]]; then\n"
                "  if [[ $* == *fintick-dashboard.service* ]]; then\n"
                f"    if [[ -L {runtime_unit_path} ]]; then printf 'masked-runtime\\n'; exit 1; fi\n"
                f"    if [[ -L {unit_path} ]]; then printf 'masked\\n'; exit 1; fi\n"
                f"    if [[ -f {unit_path} ]]; then printf 'disabled\\n'; exit 1; fi\n"
                "  fi\n"
                "  printf 'not-found\\n'; exit 1\n"
                "fi\n"
                "if [[ $* == *'--property=ActiveState'* ]]; then printf 'inactive\\n'; exit 0; fi\n"
                "if [[ $* == *'enable --now'* ]]; then\n"
                f"  if [[ -L {mask_path} ]]; then printf 'present\\n' >{activation_marker}; "
                f"else printf 'removed\\n' >{activation_marker}; fi\n"
                "  exit 1\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            data_dir = root / "data"
            data_dir.mkdir()
            with open_database(data_dir / "fintick.db"):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            mask_during_activation = activation_marker.read_text(encoding="utf-8").strip()
            restored_mask_target = os.readlink(mask_path) if mask_path.is_symlink() else None
            restored_unit = unit_path.read_text(encoding="utf-8") if runtime else None

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(mask_during_activation, "removed")
        self.assertEqual(restored_mask_target, "/dev/null")
        if runtime:
            self.assertEqual(restored_unit, "original dashboard unit\n")
        self.assertIn("Rollback restored", completed.stderr)
        self.assertNotIn("Rollback incomplete", completed.stderr)

    def test_rollback_preserves_persistent_masked_unit(self) -> None:
        self._assert_masked_unit_restored(runtime=False)

    def test_rollback_preserves_runtime_masked_unit(self) -> None:
        self._assert_masked_unit_restored(runtime=True)

    def test_rollback_final_unknown_unit_state_cannot_claim_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            phase = root / "activation-attempted"
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            journalctl.chmod(0o755)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *'enable --now'* ]]; then touch \"$FAKE_PHASE\"; exit 1; fi\n"
                "if [[ $* == *'--property=ActiveState'* ]]; then printf 'inactive\\n'; exit 0; fi\n"
                "if [[ $* == *is-enabled* ]]; then\n"
                "  if [[ -f $FAKE_PHASE ]]; then printf 'unknown\\n'; exit 4; fi\n"
                "  printf 'disabled\\n'; exit 1\n"
                "fi\n"
                "if [[ $* == *is-active* ]]; then\n"
                "  if [[ -f $FAKE_PHASE ]]; then printf 'unknown\\n'; exit 4; fi\n"
                "  printf 'inactive\\n'; exit 3\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            config_home = root / "config"
            unit_dir = config_home / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            (unit_dir / "fintick-dashboard.service").write_text(
                "original dashboard unit\n", encoding="utf-8"
            )
            data_dir = root / "data"
            data_dir.mkdir()
            with open_database(data_dir / "fintick.db"):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                    "FAKE_PHASE": str(phase),
                },
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Rollback incomplete", completed.stderr)
        self.assertNotIn("Rollback restored", completed.stderr)

    def test_unknown_prior_unit_state_aborts_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            systemctl_log = root / "systemctl.log"
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >>\"$FAKE_SYSTEMCTL_LOG\"\n"
                "if [[ $* == *is-enabled* ]]; then printf 'unknown\\n'; exit 4; fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            config_home = root / "config"
            unit_dir = config_home / "systemd" / "user"
            unit_dir.mkdir(parents=True)
            (unit_dir / "fintick-dashboard.service").write_text(
                "original dashboard unit\n", encoding="utf-8"
            )
            data_dir = root / "data"
            data_dir.mkdir()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                    "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            commands = systemctl_log.read_text(encoding="utf-8").splitlines()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Cannot determine prior enabled state", completed.stderr)
        self.assertFalse(any("enable --now" in command for command in commands))

    def test_active_units_without_healthy_dashboard_are_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name, body in (
                ("pgrep", "#!/usr/bin/env bash\nexit 1\n"),
                ("systemctl", "#!/usr/bin/env bash\nexit 0\n"),
                ("journalctl", "#!/usr/bin/env bash\nexit 0\n"),
            ):
                executable = fake_bin / name
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)

            config_home = root / "config"
            data_dir = root / "data"
            data_dir.mkdir()
            with open_database(data_dir / "fintick.db"):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            completed = subprocess.run(
                ["bash", str(SETUP)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_STATE_HOME": str(root / "state"),
                    "FINTICK_DATA_DIR": str(data_dir),
                    "FINTICK_PYTHON": "/usr/bin/python3",
                    "FINTICK_HERMES": "/bin/true",
                    "FINTICK_DASHBOARD_PORT": str(port),
                    "FINTICK_VERIFY_TIMEOUT": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("Installed and started", completed.stdout)
        self.assertIn("restoring rollback snapshot", completed.stderr)

    def test_same_cardinality_wrong_database_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name, body in (
                ("pgrep", "#!/usr/bin/env bash\nexit 1\n"),
                ("journalctl", "#!/usr/bin/env bash\nexit 0\n"),
            ):
                executable = fake_bin / name
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *'enable --now'* ]]; then\n"
                "  \"$FINTICK_PYTHON\" -m fintick serve --database \"$FAKE_DATABASE\" "
                "--host 127.0.0.1 --port \"$FAKE_PORT\" >/dev/null 2>&1 &\n"
                "  printf '%s' $! >\"$FAKE_PIDFILE\"\n"
                "elif [[ $* == *'disable --now'* ]] && [[ -f $FAKE_PIDFILE ]]; then\n"
                "  read -r pid <\"$FAKE_PIDFILE\"\n"
                "  kill \"$pid\" 2>/dev/null || true\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            data_dir = root / "data"
            data_dir.mkdir()
            operational_database = data_dir / "fintick.db"
            wrong_database = root / "wrong.db"
            with open_database(operational_database), open_database(wrong_database):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            pidfile = root / "dashboard.pid"

            try:
                completed = subprocess.run(
                    ["bash", str(SETUP)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "HOME": str(root),
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_STATE_HOME": str(root / "state"),
                        "FINTICK_DATA_DIR": str(data_dir),
                        "FINTICK_PYTHON": "/usr/bin/python3",
                        "FINTICK_HERMES": "/bin/true",
                        "FINTICK_DASHBOARD_PORT": str(port),
                        "FINTICK_VERIFY_TIMEOUT": "2",
                        "FAKE_DATABASE": str(wrong_database),
                        "FAKE_PORT": str(port),
                        "FAKE_PIDFILE": str(pidfile),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text(encoding="utf-8")), 15)
                    except (ProcessLookupError, ValueError):
                        pass

        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("Installed and started", completed.stdout)
        self.assertIn("database identity mismatch", completed.stderr)

    def test_units_must_remain_active_after_api_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name, body in (
                ("pgrep", "#!/usr/bin/env bash\nexit 1\n"),
                ("journalctl", "#!/usr/bin/env bash\nexit 0\n"),
            ):
                executable = fake_bin / name
                executable.write_text(body, encoding="utf-8")
                executable.chmod(0o755)
            active_count = root / "active-count"
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *'enable --now'* ]]; then\n"
                "  \"$FINTICK_PYTHON\" -m fintick serve --database \"$FAKE_DATABASE\" "
                "--host 127.0.0.1 --port \"$FAKE_PORT\" >/dev/null 2>&1 &\n"
                "  printf '%s' $! >\"$FAKE_PIDFILE\"\n"
                "elif [[ $* == *is-active* ]]; then\n"
                "  count=0; [[ -f $FAKE_ACTIVE_COUNT ]] && read -r count <\"$FAKE_ACTIVE_COUNT\"\n"
                "  count=$((count + 1)); printf '%s' \"$count\" >\"$FAKE_ACTIVE_COUNT\"\n"
                "  (( count <= 4 )) && exit 0 || exit 1\n"
                "elif [[ $* == *'disable --now'* ]] && [[ -f $FAKE_PIDFILE ]]; then\n"
                "  read -r pid <\"$FAKE_PIDFILE\"\n"
                "  kill \"$pid\" 2>/dev/null || true\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            data_dir = root / "data"
            data_dir.mkdir()
            database = data_dir / "fintick.db"
            with open_database(database):
                pass
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            pidfile = root / "dashboard.pid"

            try:
                completed = subprocess.run(
                    ["bash", str(SETUP)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "HOME": str(root),
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_STATE_HOME": str(root / "state"),
                        "FINTICK_DATA_DIR": str(data_dir),
                        "FINTICK_PYTHON": "/usr/bin/python3",
                        "FINTICK_HERMES": "/bin/true",
                        "FINTICK_DASHBOARD_PORT": str(port),
                        "FINTICK_VERIFY_TIMEOUT": "3",
                        "FAKE_DATABASE": str(database),
                        "FAKE_PORT": str(port),
                        "FAKE_PIDFILE": str(pidfile),
                        "FAKE_ACTIVE_COUNT": str(active_count),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text(encoding="utf-8")), 15)
                    except (ProcessLookupError, ValueError):
                        pass

        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("Installed and started", completed.stdout)
        self.assertIn("restoring rollback snapshot", completed.stderr)

    def test_fresh_install_creates_database_and_retains_bounded_journal_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text(
                "#!/usr/bin/env bash\nprintf 'journal evidence: %s\\n' \"$*\"\n",
                encoding="utf-8",
            )
            journalctl.chmod(0o755)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *'enable --now'* ]]; then\n"
                "  \"$FINTICK_PYTHON\" -m fintick serve --database \"$FAKE_DATABASE\" "
                "--host 127.0.0.1 --port \"$FAKE_PORT\" >/dev/null 2>&1 &\n"
                "  printf '%s' $! >\"$FAKE_PIDFILE\"\n"
                "elif [[ $* == *'disable --now'* ]] && [[ -f $FAKE_PIDFILE ]]; then\n"
                "  read -r pid <\"$FAKE_PIDFILE\"\n"
                "  kill \"$pid\" 2>/dev/null || true\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            data_dir = root / "data"
            data_dir.mkdir()
            database = data_dir / "fintick.db"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            pidfile = root / "dashboard.pid"

            try:
                completed = subprocess.run(
                    ["bash", str(SETUP)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "HOME": str(root),
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_STATE_HOME": str(root / "state"),
                        "FINTICK_DATA_DIR": str(data_dir),
                        "FINTICK_PYTHON": "/usr/bin/python3",
                        "FINTICK_HERMES": "/bin/true",
                        "FINTICK_DASHBOARD_PORT": str(port),
                        "FINTICK_VERIFY_TIMEOUT": "3",
                        "FAKE_DATABASE": str(database),
                        "FAKE_PORT": str(port),
                        "FAKE_PIDFILE": str(pidfile),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                backup = next((root / "state" / "fintick").glob("handoff-*"))
                journals = sorted((backup / "journals").glob("*.log"))
                evidence = [path.read_text(encoding="utf-8") for path in journals]
                database_created = database.exists()
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text(encoding="utf-8")), 15)
                    except (ProcessLookupError, ValueError):
                        pass

        self.assertEqual(completed.returncode, 0)
        self.assertIn("Installed and started", completed.stdout)
        self.assertTrue(database_created)
        self.assertEqual(len(journals), 4)
        self.assertTrue(all("journal evidence" in value for value in evidence))

    def test_static_backlog_without_decision_progress_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            journalctl.chmod(0o755)
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $* == *'enable --now'* ]]; then\n"
                "  \"$FINTICK_PYTHON\" -m fintick serve --database \"$FAKE_DATABASE\" "
                "--host 127.0.0.1 --port \"$FAKE_PORT\" >/dev/null 2>&1 &\n"
                "  printf '%s' $! >\"$FAKE_PIDFILE\"\n"
                "elif [[ $* == *'disable --now'* ]] && [[ -f $FAKE_PIDFILE ]]; then\n"
                "  kill \"$(cat \"$FAKE_PIDFILE\")\" 2>/dev/null || true\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)

            config_home = root / "config"
            data_dir = root / "data"
            data_dir.mkdir()
            database = data_dir / "fintick.db"
            with open_database(database) as connection:
                insert_post(connection, {
                    "uri": "at://stream/handoff/pending",
                    "cid": "cid-handoff-pending",
                    "record": {
                        "text": "Pending handoff fixture",
                        "createdAt": "2026-08-25T15:00:00+00:00",
                    },
                })
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            pidfile = root / "dashboard.pid"

            try:
                completed = subprocess.run(
                    ["bash", str(SETUP)],
                    cwd=ROOT,
                    env={
                        **os.environ,
                        "PATH": f"{fake_bin}:{os.environ['PATH']}",
                        "XDG_CONFIG_HOME": str(config_home),
                        "XDG_STATE_HOME": str(root / "state"),
                        "FINTICK_DATA_DIR": str(data_dir),
                        "FINTICK_PYTHON": "/usr/bin/python3",
                        "FINTICK_HERMES": "/bin/true",
                        "FINTICK_DASHBOARD_PORT": str(port),
                        "FINTICK_VERIFY_TIMEOUT": "2",
                        "FAKE_DATABASE": str(database),
                        "FAKE_PORT": str(port),
                        "FAKE_PIDFILE": str(pidfile),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                if pidfile.exists():
                    try:
                        os.kill(int(pidfile.read_text(encoding="utf-8")), 15)
                    except (ProcessLookupError, ValueError):
                        pass

        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("Installed and started", completed.stdout)
        self.assertIn("backlog did not move", completed.stderr)


if __name__ == "__main__":
    unittest.main()
