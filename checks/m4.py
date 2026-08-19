"""M4 check: structured output. Certify targets/simple-langgraph-agent — a
fixture with a documented, known planted flaw (EXPECTED.md: "no error
handling around the API call in fetch_price") — and assert the run's
structured output parses into a `CertificationReport` containing at least
one `Finding` whose category is "error-handling".

Also asserts the fixture's git status stays clean, since this check runs
`run_certify` against a real, version-controlled target under `targets/`
(CLAUDE.md: "Do not modify anything under targets/") rather than a
disposable temp fixture.

Run with: python checks/m4.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit import harness  # noqa: E402
from agentaudit.schema import CertificationReport, FindingCategory  # noqa: E402

TARGET = ROOT / "targets" / "simple-langgraph-agent"
EXPECTED_CATEGORY = FindingCategory.ERROR_HANDLING


def run_git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    )


def main() -> int:
    assert TARGET.is_dir(), f"fixture target missing: {TARGET}"

    report = asyncio.run(harness.run_certify(TARGET))

    assert isinstance(report, CertificationReport), (
        f"run_certify did not return a CertificationReport: {type(report)}"
    )

    print(f"verdict={report.verdict.value!r}")
    for f in report.findings:
        print(f"  [{f.severity.value}] {f.category.value} (confidence={f.confidence}): {f.evidence[:100]}")

    # Provenance and target metadata must come from the harness, not the
    # model — see the design note in schema.py / harness.py. If either is
    # missing, the harness failed to stamp them after parsing Claude's
    # structured output.
    assert report.provenance is not None, "report.provenance was not filled in by the harness"
    assert report.provenance.session_id, "report.provenance.session_id is empty"
    assert report.target is not None and report.target.path == str(TARGET), (
        f"report.target does not point at the audited fixture: {report.target}"
    )

    matching = [f for f in report.findings if f.category == EXPECTED_CATEGORY]
    assert matching, (
        f"expected a Finding with category={EXPECTED_CATEGORY.value!r} (the planted "
        f"missing-error-handling bug in fetch_price), got categories="
        f"{[f.category.value for f in report.findings]}"
    )

    status = run_git("status", "--porcelain", "targets/simple-langgraph-agent")
    assert status.stdout == "", f"fixture not clean after run_certify:\n{status.stdout}"

    print(
        f"OK: CertificationReport parsed with a {EXPECTED_CATEGORY.value!r} finding, "
        f"fixture untouched"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
