# Common development tasks. Uses the virtualenv at .venv (see CONTRIBUTING.md).
PY ?= .venv/bin/python
BIOME = npx --yes @biomejs/biome@2.6.5

.PHONY: install lint format run check

install:
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(BIOME) lint

format:
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

run:
	$(PY) -m datalog.run_local

check: lint
	$(PY) -m compileall -q datalog examples benchmarks collector
