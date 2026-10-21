"""Entry point for repository adoption commands."""

import argparse
import sys


def run(args: argparse.Namespace) -> int:
    print(f"not implemented yet: {args.action}", file=sys.stderr)
    return 2
