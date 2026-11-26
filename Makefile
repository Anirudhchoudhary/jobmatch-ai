UV ?= uv
RUN = cd backend && $(UV) run
RUN2 = && $(UV) run

# Non-secret local dev DB URL (matches docker-compose.yml); used only when DATABASE_URL is unset.
DATABASE_URL ?= postgresql+psycopg://jobmatch:jobmatch@localhost:5432/jobmatch
export DATABASE_URL

.PHONY: install lint fmt typecheck test test-integration env up down migrate db-shell setup run logs

install:
	cd backend && $(UV) sync

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
