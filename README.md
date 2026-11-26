# JobMatch AI

JobMatch AI is a backend that matches resumes to jobs using LLMs and pgvector similarity search. This repository currently contains the Phase 1 skeleton: a FastAPI app with structured, PII-redacted logging, a Postgres + pgvector database, migrations, and a health endpoint.

## Prerequisites

- Docker (with Compose v2)
- [uv](https://docs.astral.sh/uv/)
- AWS CLI (only needed for `make env`)

## Quickstart

```sh
make setup   # install deps, start Postgres, run migrations
make run     # build and start db + api in Docker
curl localhost:8000/health
```

A healthy response looks like `{"status":"ok","version":"0.1.0","db":"ok","pgvector":"..."}`. If the database is unreachable the endpoint returns HTTP 503 with `"status":"degraded"`.

`make logs` tails the API logs; `make down` stops everything.

## Secrets

Every secret value lives in AWS Secrets Manager (`jobmatch/dev`, `jobmatch/prod`, `jobmatch/ci`). Nothing secret is committed or stored in GitHub. `make env` generates a git-ignored `.env` (mode 0600) from Secrets Manager; never hand-edit it. The only credentials in the repo are the non-secret, dev-only local Postgres ones in `docker-compose.yml`.

## Development

```sh
make lint typecheck test test-integration
```

Integration tests need the Compose Postgres (`make up`).
