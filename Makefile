.DEFAULT_GOAL := help
PY := .venv/bin/python
UV := uv

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Create the venv and install everything, including dev extras
	$(UV) venv --python 3.12
	$(UV) pip install -e ".[dev,api,bench,model]"

.PHONY: test
test: ## Run the test suite
	$(PY) -m pytest

.PHONY: test-thorough
test-thorough: ## Run the suite with the deep Hypothesis profile (what CI runs)
	$(PY) -m pytest --hypothesis-profile=thorough

.PHONY: cov
cov: ## Run tests with coverage and enforce the threshold
	$(PY) -m pytest --cov=settle --cov-report=term-missing --cov-fail-under=90

.PHONY: lint
lint: ## ruff + mypy strict
	$(PY) -m ruff check src tests bench
	$(PY) -m ruff format --check src tests bench
	$(PY) -m mypy

.PHONY: fmt
fmt: ## Apply formatting and safe fixes
	$(PY) -m ruff check --fix src tests bench
	$(PY) -m ruff format src tests bench

.PHONY: bench
bench: ## Regenerate the benchmark table and chart in bench/results/
	$(PY) -m bench.run

.PHONY: bench-check
bench-check: ## Verify the committed benchmark numbers still reproduce (what CI runs)
	$(PY) -m bench.check

.PHONY: demo
demo: ## Generate a sample ledger, reconcile it, and show the summary
	$(PY) -m bench.generate --out demo --invoices 60 --noise 0.3
	$(PY) -m settle.cli run demo/invoices.csv demo/bank.csv --out demo/out

.PHONY: serve
serve: ## Run the API locally
	$(PY) -m settle.cli serve --port 8000

.PHONY: docker
docker: ## Build the image
	docker build -t settle:latest .

.PHONY: clean
clean: ## Remove build and test artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov demo out dist build
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

.PHONY: all
all: lint cov bench-check ## What CI runs
