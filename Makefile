UV ?= uv
RUN = cd backend && $(UV) run
RUN2 = && $(UV) run

# Non-secret local dev DB URL (matches docker-compose.yml); used only when DATABASE_URL is unset.
DATABASE_URL ?= postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch
export DATABASE_URL

.PHONY: install lint fmt typecheck test test-integration test-evals env up down migrate db-shell setup run logs samples retrieval-eval retrieval-eval-live report eval eval-baseline eval-live eval-live-baseline

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

# Needs the optional `evals` group (DeepEval); offline, never calls a model.
test-evals:
	cd backend && $(UV) run --group evals pytest -p no:deepeval tests/unit/evals -q

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

# Offline eval suite (needs `make up migrate`); gates on evals/thresholds.yaml `offline`,
# with deltas against the naive baseline once `make eval-baseline` has been run.
eval:
	cd backend && $(UV) run python -m scripts.run_evals --profile offline --variant system --subset full --out ../var/evals/offline $$(test -f ../var/evals/offline-baseline/report.json && echo --baseline ../var/evals/offline-baseline/report.json)

# Naive baseline (vector-only, no rerank, no guardrails); never gated.
eval-baseline:
	cd backend && $(UV) run python -m scripts.run_evals --profile offline --variant baseline --subset full --out ../var/evals/offline-baseline --no-gate

# Real Anthropic judge and Bedrock: spends the Anthropic key; ask first. VARIANT/SUBSET override.
eval-live:
	cd backend && $(UV) run --group evals python -m scripts.run_evals --profile live --variant $${VARIANT:-system} --subset $${SUBSET:-ci} --out ../var/evals/live $$(test -f ../evals/baseline.json && echo --baseline ../evals/baseline.json)

# Writes the committed live baseline (naive variant, full set); spends the Anthropic key; ask first.
eval-live-baseline:
	cd backend && $(UV) run --group evals python -m scripts.run_evals --profile live --variant baseline --subset full --out ../var/evals/live-baseline --write-baseline ../evals/baseline.json --no-gate
