UV ?= uv
RUN = cd backend && $(UV) run
RUN2 = && $(UV) run

.PHONY: install lint fmt typecheck test test-integration env

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
