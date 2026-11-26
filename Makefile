UV ?= uv
RUN = cd backend && $(UV) run
RUN2 = && $(UV) run

.PHONY: install lint fmt typecheck test test-integration

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
