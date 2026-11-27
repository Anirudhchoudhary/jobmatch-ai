UV ?= uv
RUN = cd backend && $(UV) run
RUN2 = && $(UV) run

# Non-secret local dev DB URL (matches docker-compose.yml); used only when DATABASE_URL is unset.
DATABASE_URL ?= postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch
export DATABASE_URL

.PHONY: install lint fmt typecheck test test-integration env up down migrate db-shell setup run logs samples retrieval-eval retrieval-eval-live report

install:
	cd backend && $(UV) sync --locked

lint:
	$(RUN) ruff check . $(RUN2) ruff format --check .

fmt:
	$(RUN) ruff check --fix . $(RUN2) ruff format .

typecheck:
	$(RUN) mypy

test:
	$(RUN) pytest

test-integration:
	$(RUN) pytest -m integration

env:
	cd backend && $(UV) run python scripts/gen_env.py --secret-id $${SECRET_ID:-jobmatch/dev} --out ../.env

up:
	docker compose up -d --wait db

down:
	docker compose down

migrate:
	cd backend && $(UV) run alembic upgrade head

db-shell:
	docker compose exec db psql -U jobmatch jobmatch

setup: install up migrate

run:
	docker compose up -d --build --wait

logs:
	docker compose logs -f api

samples:
	$(RUN) python scripts/make_samples.py

# Offline providers (hash embedder, no reranker); prints the table, writes an ignored file.
retrieval-eval:
	$(RUN) python -m scripts.retrieval_eval --out ../var/retrieval_baseline.offline.json

# Latency, cost and guardrail report from the local DB (DAYS=30 for a longer window).
report:
	$(RUN) python -m scripts.report --days $${DAYS:-7}

# Real Bedrock embeddings + rerank: needs AWS credentials; costs fractions of a cent.
retrieval-eval-live:
	cd backend && EMBEDDER_PROVIDER=bedrock RERANKER_PROVIDER=bedrock $(UV) run python -m scripts.retrieval_eval --out ../evals/retrieval_baseline.json
