.PHONY: install test lint fmt run build clean

install:            ## install dev environment
	uv sync --extra keyring

test:               ## run the test suite
	uv run pytest -q

lint:               ## lint
	uv run ruff check src tests

fmt:                ## auto-format / fix
	uv run ruff check --fix src tests

build:              ## build sdist + wheel into dist/
	uv build

run:                ## run the CLI (pass ARGS="status")
	uv run syt $(ARGS)

clean:
	rm -rf dist build .pytest_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
