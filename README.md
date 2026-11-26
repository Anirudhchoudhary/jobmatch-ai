# JobMatch AI

JobMatch AI is a backend that matches resumes to jobs using LLMs and pgvector similarity search. This repository currently contains Phases 1 and 2: a FastAPI app with structured, PII-redacted logging, a Postgres + pgvector database, migrations and a health endpoint (Phase 1), plus resume/JD ingestion, hybrid retrieval with reranking and streamed, cited chat (Phase 2).

## Prerequisites

- Docker (with Compose v2)
- [uv](https://docs.astral.sh/uv/)
- AWS credentials (only needed for `make env`; it uses boto3, so e.g. `aws sso login` or `aws login` works; the AWS CLI binary itself is not required)

## Quickstart

```sh
make setup   # install deps, start Postgres, run migrations
make run     # build and start db + api in Docker
curl localhost:8000/health
```

A healthy response looks like `{"status":"ok","version":"0.1.0","db":"ok","pgvector":"..."}`. If the database is unreachable the endpoint returns HTTP 503 with `"status":"degraded"`.

`make logs` tails the API logs; `make down` stops everything.

## Secrets

Every secret value lives in AWS Secrets Manager (`jobmatch/dev`, `jobmatch/prod`, `jobmatch/ci`). Nothing secret is committed or stored in GitHub. `make env` generates a git-ignored `.env` (mode 0600) from Secrets Manager; never hand-edit it. The Makefile and `docker-compose.yml` point local commands and the api container at the local Docker Postgres regardless of the `DATABASE_URL` in the generated `.env`. The only credentials in the repo are the non-secret, dev-only local Postgres ones in `docker-compose.yml`.

## Development

```sh
make lint typecheck test test-integration
```

Integration tests need the Compose Postgres (`make up`). They always use the local Compose database; set `JOBMATCH_ALLOW_NONLOCAL_DB=1` to opt out deliberately.

## Phase 2: try it locally

Ingestion, hybrid retrieval and streamed chat run with offline defaults (`LLM_PROVIDER=fake`, `EMBEDDER_PROVIDER=hash`, `RERANKER_PROVIDER=none`), so no cloud access is needed. Locally every request is the built-in dev user.

```sh
make setup run
```

Upload a resume PDF (ticket, then PUT the bytes) and ingest it:

```sh
TICKET=$(curl -s localhost:8000/api/uploads -H 'Content-Type: application/json' \
  -d "{\"content_type\":\"application/pdf\",\"size_bytes\":$(wc -c < resume.pdf)}")
KEY=$(echo "$TICKET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
curl -s -X PUT "localhost:8000/api/uploads/$KEY" -H 'Content-Type: application/pdf' --data-binary @resume.pdf

# progress events, then a final "done" event
curl -N localhost:8000/api/stream/documents -H 'Content-Type: application/json' \
  -d "{\"doc_type\":\"resume\",\"upload_key\":\"$KEY\"}"

# a job description is pasted as text (50 to 20000 characters)
curl -N localhost:8000/api/stream/documents -H 'Content-Type: application/json' \
  -d '{"doc_type":"jd","text":"<paste the job description here>"}'
```

No resume handy? Ingest the bundled fictional samples (Alex Rivera and a Senior Backend Engineer job description) in one call, then ask a question:

```sh
curl -N -X POST localhost:8000/api/samples
curl -N localhost:8000/api/stream/chat -H 'Content-Type: application/json' \
  -d '{"question":"Does Alex have Kubernetes experience?"}'
```

The chat stream emits `progress` events (`retrieving`, `reranking`, `generating`), then `token` events with the answer text, then one `done` event carrying the full answer, its `citations` (labels `r<n>` for resume chunks and `j<n>` for the job description), `refused` and the `request_id`. With real providers, a question the documents cannot answer gets a fixed refusal when every reranked chunk scores below `MIN_RELEVANCE`. Offline the no-op reranker never scores and the fake model always answers, so the refusal only appears when you have no documents yet (or none embedded by the current embedder).

### Real providers

Docker (`make run`) is the offline demo path: the container runs with the offline defaults above and has no AWS credentials. Run the API on the host to use the real providers:

```sh
# 1. Add an ANTHROPIC_API_KEY entry to the jobmatch/dev secret's JSON (AWS console, or
#    `aws secretsmanager put-secret-value` with the existing keys plus the new one).
#    Never write the key into a file in the repo.

# 2. Generate the git-ignored .env from Secrets Manager (it supplies ANTHROPIC_API_KEY)
make env

# 3. Start Postgres, migrate, then run the API on the host with the real providers selected.
#    Bedrock uses your host's normal AWS credentials (e.g. `aws sso login`). DATABASE_URL
#    points at the local Compose Postgres, as the Makefile does for its own targets.
docker compose stop api   # frees port 8000 if the Docker api is running
make up migrate
cd backend && LLM_PROVIDER=anthropic EMBEDDER_PROVIDER=bedrock RERANKER_PROVIDER=bedrock \
  DATABASE_URL=postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch \
  uv run uvicorn app.main:app --port 8000
```

Each document records which embedder produced its vectors. After switching embedders, retrieval ignores documents embedded by the other one until you ingest them again (re-ingesting the same file re-embeds it instead of reusing it).

Bedrock calls use region `us-west-2` and need these IAM actions:

- `bedrock:InvokeModel` on `arn:aws:bedrock:us-west-2::foundation-model/amazon.titan-embed-text-v2:0`
- `bedrock:Rerank` and `bedrock:InvokeModel` on `arn:aws:bedrock:us-west-2::foundation-model/amazon.rerank-v1:0`

### Retrieval baseline

`make retrieval-eval` ingests the samples for a throwaway user, runs the questions in `evals/retrieval/questions.json` against `vector`, `hybrid` and `hybrid+rerank`, and prints recall@5 and MRR. Offline it uses the hash embedder (not semantic, so numbers are only a smoke test) and skips rerank; the result goes to the git-ignored `var/retrieval_baseline.offline.json`. `make retrieval-eval-live` uses Bedrock Titan and Rerank (needs AWS credentials, costs fractions of a cent) and writes the committed `evals/retrieval_baseline.json`, which Phase 6 evals build on.

| mode | recall@5 | MRR | n |
|---|---|---|---|
| vector | 0.917 | 0.660 | 12 |
| hybrid | 1.000 | 0.743 | 12 |
| hybrid+rerank | 0.917 | 0.778 | 12 |

Live run with Titan embeddings and Amazon Rerank over the bundled samples (12 questions, 11 chunks). The corpus is tiny, so these numbers are indicative only. The script refuses to write under `evals/` unless `EMBEDDER_PROVIDER=bedrock`.
