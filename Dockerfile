# Paper-trading / research image. Live dependencies are NOT installed by default:
# build with --build-arg EXTRAS="--extra live --extra llm --extra mcp" only after
# reading docs/deployment.md "Live procedure".
FROM python:3.12-slim AS base

ARG EXTRAS="--extra llm --extra mcp"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

# uv from the official image, pinned to the version that produced uv.lock (no curl|sh).
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY configs ./configs
RUN uv sync --frozen --no-dev ${EXTRAS} \
 && useradd --create-home --uid 10001 bot \
 && mkdir -p /data && chown bot:bot /data

USER bot
VOLUME ["/data"]
# Never run with python -O: asserts document invariants (see pyproject bandit notes).
ENTRYPOINT ["python", "-m", "polymarket_bot.app", "--data-dir", "/data"]
CMD ["--config", "configs/paper.yaml", "paper"]
