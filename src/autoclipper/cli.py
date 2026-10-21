"""Command-line entry point: `autoclipper <command>`."""

from __future__ import annotations

import argparse

from autoclipper import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoclipper", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_subparsers(dest="command")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    return 0
