"""CLI entrypoint for agentaudit."""
import argparse
import asyncio
import sys
from pathlib import Path

from agentaudit import harness, telemetry
from agentaudit.schema import Case


def _cmd_inspect(args: argparse.Namespace) -> int:
    target = Path(args.repo_path).resolve()
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 1

    summary, result = asyncio.run(
        harness.run_inspect(target, max_budget_usd=args.max_budget_usd)
    )
    record = telemetry.record_run(target, result)

    print(summary)
    print(
        f"\n[session={record['session_id']} turns={record['num_turns']} "
        f"cost=${record['total_cost_usd'] or 0:.4f} "
        f"subtype={record['subtype']!r} "
        f"terminal_reason={record['terminal_reason']!r}]",
        file=sys.stderr,
    )

    return 0 if result.subtype == "success" else 1


def _cmd_audit(args: argparse.Namespace) -> int:
    target = Path(args.repo_path).resolve()
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 1

    case = None
    if args.case_json:
        case = Case.model_validate_json(Path(args.case_json).read_text())

    report = asyncio.run(
        harness.run_audit(target, args.run_id, case=case, max_budget_usd=args.max_budget_usd)
    )

    print(f"verdict={report.verdict.value}")
    for f in report.findings:
        print(f"  [{f.severity.value}] {f.category.value}: {f.evidence[:100]}")

    provenance = report.provenance
    print(
        f"\n[run_id={args.run_id!r} "
        f"session={provenance.session_id if provenance else ''} "
        f"turns={provenance.num_turns if provenance else 0} "
        f"cost=${(provenance.total_cost_usd or 0) if provenance else 0:.4f}]",
        file=sys.stderr,
    )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentaudit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Run one agent turn to summarize a target repo's architecture"
    )
    inspect_parser.add_argument("repo_path", help="Path to the target agent repository")
    inspect_parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=0.50,
        help="Cost cap; the run terminates with subtype=error_max_budget_usd if exceeded (default: 0.50)",
    )
    inspect_parser.set_defaults(func=_cmd_inspect)

    audit_parser = subparsers.add_parser(
        "audit",
        help="Run a resumable generator->executor->judge audit pipeline",
    )
    audit_parser.add_argument("repo_path", help="Path to the target agent repository")
    audit_parser.add_argument(
        "--run-id",
        required=True,
        help="Stable run id; re-running with the same id resumes an interrupted audit",
    )
    audit_parser.add_argument(
        "--case-json",
        default=None,
        help="Path to a JSON file matching the Case schema; skips the case-generator step",
    )
    audit_parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=0.50,
        help="Cost cap per pipeline step (default: 0.50)",
    )
    audit_parser.set_defaults(func=_cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
