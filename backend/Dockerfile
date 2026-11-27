FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /uvx /bin/
# Lambda Web Adapter (pinned): inert outside Lambda, proxies Function URL / pass-through events to uvicorn.
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0 /lambda-adapter /opt/extensions/lambda-adapter

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY prompts ./prompts

# Lambda's filesystem is read-only except /tmp: uploads, HOME and caches all live there.
ENV UPLOAD_DIR=/tmp/uploads \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    AWS_LWA_PORT=8000 \
    AWS_LWA_READINESS_CHECK_PATH=/livez \
    AWS_LWA_INVOKE_MODE=response_stream
# Lambda ignores USER for container images; it is kept for docker compose.
RUN useradd --system --no-create-home appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
