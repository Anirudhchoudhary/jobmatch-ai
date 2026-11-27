FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.10 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY prompts ./prompts

# Upload blobs live under the app dir (the repo-root default resolves to / in the image).
ENV UPLOAD_DIR=/app/var/uploads
RUN useradd --system --no-create-home appuser \
    && mkdir -p /app/var/uploads \
    && chown -R appuser /app/var
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
