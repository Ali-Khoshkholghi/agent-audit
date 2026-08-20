# M8: containerize the harness for CI / headless invocation.
#
# Session-pattern (see agent-sdk/hosting.md): "ephemeral sessions" — one
# container runs one `agentaudit` invocation to completion and exits. No
# server, no port, no session persistence: `agentaudit audit --run-id`
# already persists resumable state to a bind-mounted `runs/` directory (see
# docker-compose.m8.yml), not to anything container-local that would need
# its own SessionStore adapter.
FROM python:3.11-slim

# bubblewrap: the M6 sandbox.py Linux backend for execute_case_against_target.
# Installed unconditionally so `agentaudit audit`'s case-executor step can
# run inside this image; `agentaudit certify`/`inspect` never call it.
RUN apt-get update && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY targets ./targets
COPY .claude ./.claude
# .claude/skills/severity-rubric is a symlink to ../../plugin/skills/
# severity-rubric (see M7) — must be copied in too or that symlink
# dangles and the judge's Skill-tool lookup silently fails.
COPY plugin ./plugin

# Editable (PEP 660), not a real install: a real install copies the
# package into site-packages, so `Path(__file__)`-based root resolution
# (REPO_ROOT in harness.py, RUNS_DIR in tools/__init__.py — both walk up
# from their own module's __file__ the same way local dev's editable
# .venv install does) no longer points back at /app, silently breaking
# severity-rubric skill discovery and the case/audit ledgers inside the
# container. An editable install keeps __file__ pointing at ./src, which
# stays present at this same path for the image's whole lifetime, so it
# costs nothing here despite the image being rebuilt on every change.
RUN pip install --no-cache-dir -e .

# No ANTHROPIC_API_KEY, ever (see CLAUDE.md) — auth is CLAUDE_CODE_OAUTH_TOKEN,
# supplied at `docker run`/`docker compose` time, never baked into the image.

ENTRYPOINT ["python", "-m", "agentaudit"]
CMD ["--help"]
