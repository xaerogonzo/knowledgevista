"""The `kv` command: a thin entry point. Business logic lives in services, never here.

Milestone 0 only wires `--version`. The JSON envelope, exit codes and error codes arrive with milestone 1
(see docs/ARCHITECTURE.md, "Interfaces"), because they are a contract and are written once, with their tests.
"""

from __future__ import annotations

import argparse

from knowledgevista import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kv", description="Knowledge Vista: a local-first document library.")
    parser.add_argument("--version", action="version", version=f"KnowledgeVista {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
