"""Command-line entry point: `autoclipper <command>`."""

from __future__ import annotations

import argparse

from autoclipper import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoclipper", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    notify = sub.add_parser("notify", help="post a status line to the Discord test webhook")
    notify.add_argument("text")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
    elif args.command == "notify":
        from autoclipper.discord import post_text

        result = post_text(args.text)
        print(f"notify {result}")
        return 0 if result.ok else 1
    return 0
