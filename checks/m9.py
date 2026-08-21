"""M9 check: entry-point discovery without a spec file.

Most real target repos (confirmed live against langchain-ai/react-agent, M8's
end-to-end run) have no agentaudit.spec.json, so before M9 `load_target_spec`
always returned is_error=True for them and left the case-executor subagent to
*guess* an entry_point from common filenames on its own — untestable, and the
reason a real audit against react-agent came back "inconclusive" rather than
actually exercising the target. M9 replaces that guessing with deterministic
discovery inside `load_target_spec` itself, tried in priority order:
pyproject.toml (project.scripts/console_scripts) -> langgraph.json (graphs) ->
package.json (main) -> common filename conventions.

Five checks, no mocking of `load_target_spec` itself — real target repos or
real (if synthetic, self-authored) file structures throughout:

1. langgraph.json tier, against the real, live react-agent repo.
2. Fallback-convention tier, against the real, in-repo simple-langgraph-agent
   fixture (agent.py, no other signal present).
3. pyproject.toml tier + priority order (wins over a present langgraph.json),
   against a synthetic temp fixture — no real repo in this session uses
   project.scripts, so this is authored directly.
4. package.json tier, against a synthetic temp fixture — react-agent (our
   only live target) has no package.json, so this can only be proven not to
   crash and to resolve the field correctly, not validated against a real JS
   repo end to end.
5. Genuine failure: an empty target reports is_error=True with a reason that
   names what was actually checked, rather than fabricating a filename.

Run with: python checks/m9.py
"""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agentaudit.tools import load_target_spec  # noqa: E402

REACT_AGENT_TARGET = Path("/tmp/audit-target")
REACT_AGENT_URL = "https://github.com/langchain-ai/react-agent"

SIMPLE_LANGGRAPH_TARGET = ROOT / "targets" / "simple-langgraph-agent"


async def _load(target: Path) -> dict:
    return await load_target_spec.handler({"path": str(target)})


def _ensure_react_agent_clone() -> Path:
    if REACT_AGENT_TARGET.is_dir() and (REACT_AGENT_TARGET / "langgraph.json").is_file():
        return REACT_AGENT_TARGET
    subprocess.run(
        ["git", "clone", "--depth", "1", REACT_AGENT_URL, str(REACT_AGENT_TARGET)],
        check=True,
    )
    return REACT_AGENT_TARGET


async def check_langgraph_json_tier() -> None:
    target = _ensure_react_agent_clone()
    assert not (target / "agentaudit.spec.json").is_file(), (
        "fixture assumption broken: react-agent now has a spec file"
    )

    result = await _load(target)
    assert not result.get("is_error"), f"discovery failed against a real repo: {result}"
    spec = json.loads(result["content"][0]["text"])
    assert spec["entry_point"] == "src/react_agent/graph.py", (
        f"expected src/react_agent/graph.py, got {spec['entry_point']!r}"
    )
    print(f"OK: langgraph.json tier discovered {spec['entry_point']!r} via {spec['source']!r}")


async def check_fallback_convention_tier() -> None:
    assert SIMPLE_LANGGRAPH_TARGET.is_dir(), f"fixture missing: {SIMPLE_LANGGRAPH_TARGET}"
    assert not (SIMPLE_LANGGRAPH_TARGET / "agentaudit.spec.json").is_file()
    assert not (SIMPLE_LANGGRAPH_TARGET / "langgraph.json").is_file()
    assert not (SIMPLE_LANGGRAPH_TARGET / "pyproject.toml").is_file()

    result = await _load(SIMPLE_LANGGRAPH_TARGET)
    assert not result.get("is_error"), f"discovery failed against simple-langgraph-agent: {result}"
    spec = json.loads(result["content"][0]["text"])
    assert spec["entry_point"] == "agent.py", f"expected agent.py, got {spec['entry_point']!r}"
    print(f"OK: fallback-convention tier discovered {spec['entry_point']!r} via {spec['source']!r}")


async def check_pyproject_tier_and_priority() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        (target / "mypkg").mkdir()
        (target / "mypkg" / "cli.py").write_text("def main(): ...\n")
        (target / "pyproject.toml").write_text(
            '[project]\nname = "x"\n\n[project.scripts]\nmycli = "mypkg.cli:main"\n'
        )
        # A langgraph.json is also present and independently resolvable —
        # pyproject.toml must win, proving priority order, not just tier
        # isolation.
        (target / "another_file.py").write_text("graph = object()\n")
        (target / "langgraph.json").write_text(
            json.dumps({"graphs": {"agent": "./another_file.py:graph"}})
        )

        result = await _load(target)
        assert not result.get("is_error"), f"discovery failed: {result}"
        spec = json.loads(result["content"][0]["text"])
        assert spec["entry_point"] == "mypkg/cli.py", (
            f"expected pyproject.toml's mypkg/cli.py to win over langgraph.json, "
            f"got {spec['entry_point']!r}"
        )
        print(
            f"OK: pyproject.toml tier discovered {spec['entry_point']!r} via "
            f"{spec['source']!r}, correctly outranking a present langgraph.json"
        )


async def check_package_json_tier() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        (target / "index.js").write_text("console.log('hi');\n")
        (target / "package.json").write_text(json.dumps({"main": "index.js"}))

        result = await _load(target)
        assert not result.get("is_error"), f"discovery failed: {result}"
        spec = json.loads(result["content"][0]["text"])
        assert spec["entry_point"] == "index.js", f"expected index.js, got {spec['entry_point']!r}"
        print(f"OK: package.json tier discovered {spec['entry_point']!r} via {spec['source']!r}")


async def check_no_entry_point_found() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        result = await _load(target)
        assert result.get("is_error"), "expected is_error=True when nothing is discoverable"
        text = result["content"][0]["text"]
        assert "agentaudit.spec.json" in text, "error text should still name the spec it looked for"
        assert "none found" in text, "error text should say discovery found nothing, not fabricate a path"
        print(f"OK: empty target honestly reports discovery failure: {text!r}")


def main() -> int:
    asyncio.run(check_langgraph_json_tier())
    asyncio.run(check_fallback_convention_tier())
    asyncio.run(check_pyproject_tier_and_priority())
    asyncio.run(check_package_json_tier())
    asyncio.run(check_no_entry_point_found())
    return 0


if __name__ == "__main__":
    sys.exit(main())
