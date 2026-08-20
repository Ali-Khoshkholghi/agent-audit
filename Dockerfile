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

# Editable install would need the source tree writable; a real install is
# simpler here since the image is rebuilt on every source change anyway.
RUN pip install --no-cache-dir .

# No ANTHROPIC_API_KEY, ever (see CLAUDE.md) — auth is CLAUDE_CODE_OAUTH_TOKEN,
# supplied at `docker run`/`docker compose` time, never baked into the image.

ENTRYPOINT ["python", "-m", "agentaudit"]
CMD ["--help"]
