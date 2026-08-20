"""M8: render a CertificationReport (as printed by `agentaudit certify`) into
the markdown body of a PR comment. CI-only tooling, not part of the
installed package — the source of truth for the report shape is still
agentaudit.schema.CertificationReport, imported here rather than
re-parsed by hand.

Usage: python scripts/format_pr_comment.py <report.json> > comment.md
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit.schema import CertificationReport, Verdict  # noqa: E402

VERDICT_EMOJI = {
    Verdict.CERTIFIED: "✅",
    Verdict.CERTIFIED_WITH_FINDINGS: "⚠️",
    Verdict.NOT_CERTIFIED: "❌",
}


def format_comment(report: CertificationReport) -> str:
    lines = [
        f"## AgentAudit certification — {VERDICT_EMOJI[report.verdict]} `{report.verdict.value}`",
        "",
    ]
    if report.target:
        lines.append(f"Target: `{report.target.path}`")
    if report.provenance:
        p = report.provenance
        lines.append(
            f"Run: {p.num_turns} turns, ${p.total_cost_usd or 0:.4f}, "
            f"terminal_reason=`{p.terminal_reason}`"
        )
    lines.append("")

    if not report.findings:
        lines.append("No findings.")
    else:
        lines.append(f"### Findings ({len(report.findings)})")
        lines.append("")
        for f in report.findings:
            lines.append(f"- **[{f.severity.value}] {f.category.value}** (confidence {f.confidence:.2f})")
            lines.append(f"  {f.evidence}")

    return "\n".join(lines) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: format_pr_comment.py <report.json>", file=sys.stderr)
        return 1
    report = CertificationReport.model_validate_json(Path(sys.argv[1]).read_text())
    print(format_comment(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
