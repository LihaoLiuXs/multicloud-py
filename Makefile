PYTHON ?= python

.PHONY: help build install test test-unit test-conformance lint format typecheck package version-check version-sync clean

help:
	@echo "Targets:"
	@echo "  build            install + test (the default CI entry point)"
	@echo "  install          uv sync --frozen — provision .venv from uv.lock"
	@echo "  test             unit + conformance (replay) tests"
	@echo "  test-unit        unit tests only (no cloud creds needed)"
	@echo "  test-conformance conformance tests in replay mode"
	@echo "  lint             black --check + isort --check"
	@echo "  format           black + isort (autofix)"
	@echo "  typecheck        mypy on every package"
	@echo "  package          build wheels + sdists for every workspace member into dist/"
	@echo "  version-check    fail if any packages/*/*/pyproject.toml diverges from VERSION"
	@echo "  version-sync     rewrite every pyproject to match VERSION (run after bumping VERSION)"
	@echo "  clean            remove caches and build artifacts"

build: install version-check test-unit-cov

install:
	uv sync --frozen

test-unit:
	uv run pytest tests/ -m "not conformance"

test-unit-cov:
	uv run pytest tests/ \
	  -m "not conformance" \
	  --cov=packages \
	  --cov-branch \
	  --cov-report=xml:coverage.xml \
	  --cov-report=term-missing \
	  --junitxml=test-reports.xml \
	  --cov-fail-under=95 \
	  -v

test-conformance:
	uv run pytest tests/ -m conformance --record-mode=none

test: test-unit test-conformance

format:
	uv run black packages tests
	uv run isort packages tests

lint:
	uv run black --check packages tests
	uv run isort --check-only packages tests

typecheck:
	uv run mypy packages

package: install version-check
	rm -rf dist
	uv build --all-packages

version-check:
	uv run python scripts/sync_version.py --check

version-sync:
	uv run python scripts/sync_version.py --write

clean:
	find packages tests -type d -name __pycache__ -exec rm -rf {} +
	find packages -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache build dist coverage.xml test-reports.xml
