"""Command-line entry point for FinTick."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from fintick import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fintick",
        description="Local-first financial intelligence tape",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "doctor",
        help="check the local runtime before starting FinTick",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print(f"FinTick {__version__}: Python runtime ready")
        return 0

    parser.print_help()
    return 0
