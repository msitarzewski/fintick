"""Reversible database operations for the rootless service handoff."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path


def database_identity(database: str | Path) -> str:
    """Return an opaque identity for the exact database file on this host."""
    metadata = os.stat(Path(database))
    value = f"{metadata.st_dev}:{metadata.st_ino}".encode("ascii")
    return hashlib.sha256(value).hexdigest()


def is_fintick_worker_argv(argv: Sequence[str]) -> bool:
    """Return whether an argv belongs to a persistent FinTick worker."""
    if not argv:
        return False
    executable = Path(argv[0]).name
    command_arguments: Sequence[str]
    if executable.startswith("python"):
        module_index = next(
            (
                index
                for index in range(1, len(argv) - 1)
                if argv[index] == "-m" and argv[index + 1] == "fintick"
            ),
            None,
        )
        if module_index is None:
            return False
        command_arguments = argv[module_index + 2 :]
    elif executable == "fintick":
        command_arguments = argv[1:]
    else:
        return False
    if not command_arguments:
        return False
    command = command_arguments[0]
    if command == "serve":
        return True
    return command in {"ingest", "aggregate", "validate"} and "--watch" in command_arguments[1:]


def has_running_fintick_workers(proc_root: str | Path = "/proc") -> bool:
    """Inspect process argv records without relying on shell command-line regexes."""
    root = Path(proc_root)
    try:
        processes = tuple(root.iterdir())
    except OSError:
        return True
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            with (process / "cmdline").open("rb") as stream:
                raw = stream.read(131_072)
        except OSError:
            continue
        argv = [
            value.decode("utf-8", errors="surrogateescape")
            for value in raw.split(b"\0")
            if value
        ]
        if is_fintick_worker_argv(argv):
            return True
    return False


def snapshot_database(source: str | Path, destination: str | Path) -> None:
    """Create a self-contained, integrity-checked SQLite snapshot, including WAL data."""
    source_path = Path(source).resolve()
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.unlink(missing_ok=True)
    source_uri = f"{source_path.as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_connection:
        with sqlite3.connect(destination_path) as destination_connection:
            source_connection.backup(destination_connection)
            integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        destination_path.unlink(missing_ok=True)
        raise RuntimeError("SQLite handoff snapshot failed integrity check")


def restore_database(snapshot: str | Path, destination: str | Path) -> None:
    """Atomically restore a verified snapshot after removing stale WAL state."""
    snapshot_path = Path(snapshot)
    destination_path = Path(destination)
    with sqlite3.connect(f"{snapshot_path.resolve().as_uri()}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite handoff restore source failed integrity check")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.restore-",
        dir=destination_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    family = (
        destination_path,
        Path(f"{destination_path}-wal"),
        Path(f"{destination_path}-shm"),
    )
    family_backup = Path(tempfile.mkdtemp(
        prefix=f".{destination_path.name}.family-",
        dir=destination_path.parent,
    ))
    try:
        shutil.copy2(snapshot_path, temporary_path)
        with sqlite3.connect(temporary_path) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite handoff restore copy failed integrity check")
        original_members: dict[Path, Path] = {}
        for member in family:
            if member.exists():
                backup = family_backup / member.name
                shutil.copy2(member, backup)
                original_members[member] = backup
        try:
            family[1].unlink(missing_ok=True)
            family[2].unlink(missing_ok=True)
            os.replace(temporary_path, destination_path)
        except Exception as error:
            recovery_errors: list[OSError] = []
            for member in family:
                backup = original_members.get(member)
                try:
                    if backup is None:
                        member.unlink(missing_ok=True)
                    else:
                        shutil.copy2(backup, member)
                except OSError as recovery_error:
                    recovery_errors.append(recovery_error)
            if recovery_errors:
                raise RuntimeError(
                    "SQLite handoff restore failed and original database family "
                    "could not be fully recovered"
                ) from error
            raise
    finally:
        temporary_path.unlink(missing_ok=True)
        shutil.rmtree(family_backup, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """Run one snapshot or restore operation for the shell installer."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 2 and arguments[0] == "identity":
        print(database_identity(arguments[1]))
        return 0
    if len(arguments) == 2 and arguments[0] == "workers":
        return 0 if has_running_fintick_workers(arguments[1]) else 1
    if len(arguments) != 3 or arguments[0] not in {"snapshot", "restore"}:
        print(
            "usage: service_handoff.py identity DATABASE | "
            "workers PROC_ROOT | "
            "{snapshot|restore} SOURCE DESTINATION",
            file=sys.stderr,
        )
        return 2
    operation, source, destination = arguments
    if operation == "snapshot":
        snapshot_database(source, destination)
    else:
        restore_database(source, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
