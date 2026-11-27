UV ?= uv
RUN = cd backend && $(UV) run
RUN2 = && $(UV) run

# Non-secret local dev DB URL (matches docker-compose.yml); used only when DATABASE_URL is unset.
DATABASE_URL ?= postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch
export DATABASE_URL

.PHONY: install lint fmt typecheck test test-integration test-evals env up down migrate db-shell setup run logs samples retrieval-eval retrieval-eval-live report eval eval-baseline eval-live eval-live-baseline secrets-set migrate-prod deploy deploy-frontend teardown smoke-prod fe-install fe-dev fe-test fe-build fe-e2e

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

# ---- AWS deploy (us-west-2; CloudFront/ACM/Route 53 in us-east-1).
# Order: secrets-set -> migrate-prod -> deploy -> deploy-frontend -> smoke-prod.
# BUDGET_EMAIL and secret values are never committed; state lives in git-ignored var/deploy/.
PROD_SECRET_ID = jobmatch/prod
DEPLOY_STATE = var/deploy/state.json

# Prompts for each prod secret key (values are never echoed) and writes AWS Secrets Manager.
secrets-set:
	cd backend && $(UV) run python scripts/secrets_set.py --secret-id $(PROD_SECRET_ID)

# Runs migrations against the prod DATABASE_URL read from the secret.
migrate-prod:
	cd backend && SECRETS_SOURCE=aws SECRET_ID=$(PROD_SECRET_ID) $(UV) run alembic upgrade head

# Needs BUDGET_EMAIL in the environment: BUDGET_EMAIL=you@example.com make deploy
deploy:
	infra/aws/deploy.sh

# Builds frontend/ (npm ci, npm run build) and syncs out/ to the site bucket, then invalidates
# CloudFront. Refuses to run until make deploy has recorded the distribution.
deploy-frontend:
	infra/aws/deploy-frontend.sh

# Deletes everything deploy created, and the secret after a typed confirmation.
# TEARDOWN_ARGS=--keep-secret keeps the secret; TEARDOWN_ARGS=--yes skips the confirmation.
teardown:
	infra/aws/teardown.sh $(TEARDOWN_ARGS)

# GET <Function URL>/health from the state file and require "status": "ok".
smoke-prod:
	@url=$$(jq -r '.function_url // empty' $(DEPLOY_STATE) 2>/dev/null); \
	test -n "$$url" || { echo "no Function URL in $(DEPLOY_STATE): run make deploy first" >&2; exit 1; }; \
	curl -fsS --max-time 60 "$${url%/}/health" | jq -e '.status == "ok"' >/dev/null \
	  && echo "smoke-prod ok: $${url%/}/health" \
	  || { echo "smoke-prod failed: $${url%/}/health did not report status ok" >&2; exit 1; }

fe-install:
	cd frontend && npm ci

fe-dev:
	cd frontend && npm run dev

fe-test:
	cd frontend && npm run lint && npm run typecheck && npm test

fe-build:
	cd frontend && npm run build

# Needs the offline API on :8000, in this order: `make up migrate`, then `docker compose up -d --build --wait api`.
# (The API healthcheck fails until migrated.) Starts next dev itself.
fe-e2e:
	cd frontend && npx playwright test
