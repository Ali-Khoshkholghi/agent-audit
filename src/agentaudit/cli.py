"""CLI entrypoint for agentaudit."""
import argparse
import asyncio
import sys
from pathlib import Path

from agentaudit import harness, telemetry


def _cmd_inspect(args: argparse.Namespace) -> int:
    target = Path(args.repo_path).resolve()
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 1

    summary, result = asyncio.run(harness.run_inspect(target))
    record = telemetry.record_run(target, result)

    print(summary)
    print(
        f"\n[session={record['session_id']} turns={record['num_turns']} "
        f"cost=${record['total_cost_usd'] or 0:.4f} "
        f"terminal_reason={record['terminal_reason']!r}]",
        file=sys.stderr,
    )

    return 0 if result.subtype == "success" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentaudit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Run one agent turn to summarize a target repo's architecture"
    )
    inspect_parser.add_argument("repo_path", help="Path to the target agent repository")
    inspect_parser.set_defaults(func=_cmd_inspect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
