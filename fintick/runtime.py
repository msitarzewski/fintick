"""Shared lifecycle support for FinTick's unattended workers."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def timestamp() -> str:
    """Return a compact UTC timestamp for process logs."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def run_periodically(
    label: str,
    cycle: Callable[[], Any],
    interval: float,
    *,
    stop: threading.Event | None = None,
    install_signals: bool = True,
) -> None:
    """Run a resilient cycle immediately and then at a fixed interval.

    Cycle failures are logged and isolated so transient network, model, or database
    failures cannot kill a supervised worker. SIGTERM and SIGINT trigger a prompt,
    clean exit when this runs in the main thread.
    """
    if interval <= 0:
        raise ValueError("interval must be positive")
    stop = stop or threading.Event()

    if install_signals:
        def request_stop(signum: int, _frame: object) -> None:
            print(f"{timestamp()} {label} stopping signal={signum}", flush=True)
            stop.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

    print(f"{timestamp()} {label} worker started interval={interval:g}s", flush=True)
    while not stop.is_set():
        try:
            cycle()
        except Exception as error:  # a transient cycle failure must not kill the daemon
            print(
                f"{timestamp()} {label} cycle failed: {type(error).__name__}: {error}",
                flush=True,
            )
        if stop.wait(interval):
            break
    print(f"{timestamp()} {label} worker stopped", flush=True)
